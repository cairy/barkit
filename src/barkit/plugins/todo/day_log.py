"""todo/day_log：按天记账那一份数据文件（**纯数据小件**：零 Qt、零配置、零业务）。

合并方案 §6.2：合并前两个组件各写一份按天记账文件 —— ``clock_workday.json`` 存
「今天几点上班」、``todo_log.json`` 存「今天哪几条做完了」。两份的形状**完全一样**
（``{"days": {"2026-10-05": {"td1": "09:12"}}}``），连去尾 30 天的写法都逐行相同，
于是并成 ``agenda_log.json`` 一份，读法收在这里。

**为什么单独一个模块**：它是本组件唯一一处**不经过配置门面**的落盘（数据 ≠ 配置：
每天写一次用户配置会触发 ``config.changed`` 广播，且会被「恢复默认配置」冲掉）。
把它摊在 ``plugin.py`` 里，「这个文件会不会写用户配置」就只能在读完全文之后才敢回答。
本模块只认「文件名 → 天表」这一件事。

天表的形状：``{日期: {条目 id: "HH:MM"}}``。值同时装两样东西 —— 问 ``choice`` 的条目
存的是**完成时刻**，问 ``time`` 的条目存的是**答案本身**（如「今天几点上班」）。两样的
形状本来就一样，哪条属于哪样由条目自己的 ``ask`` 决定（见 ``plugin.done_today``）。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

#: 合并后的那一份（唯一的按天记账文件）。它进 ``resources().data_files``：卸载与
#: 「导出 / 导入用户数据」按那份清单走。
AGENDA_FILENAME = "agenda_log.json"

#: 合并前那两份（读它们即并它们，读完删掉；两个源之间按「先到者优先」）。
#: 顺序照抄方案 §6.2 那张表，没有别的含义 —— 两个组件的条目 id 前缀本来就不一样
#: （``clk`` / ``td``），同一天同一条真撞上的可能性可以忽略。
LEGACY_FILENAMES = ("clock_workday.json", "todo_log.json")

#: 只留最近这么多天：这份文件每天写一行，不去尾就会无限长（一个常驻 app 的痕迹不该
#: 长成一份日志）。
KEEP_DAYS = 30


def path_of(filename: str) -> Path:
    """数据根下的一个文件。**调用期**取根（写进默认参数值会在定义期冻结路径，
    测试的路径重定向即失效）。"""
    from barkit import paths

    return Path(paths.APP_SUPPORT) / filename


def read_days(filename: str) -> "dict[str, dict]":
    """读一份天表；读不出来（没有 / 坏了 / 形状不对）一律**当空**。

    这份文件是「今天的事实」：丢了只会让今天从头算，不该让组件起不来。损坏记一条
    warning 留痕 —— ``doctor`` 的 D2 会把「解析不了」顶到用户眼前，这里不重复报。
    """
    path = path_of(filename)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("按天记账文件损坏，按空处理：%s", path)
        return {}
    days = data.get("days") if isinstance(data, dict) else None
    return days if isinstance(days, dict) else {}


def write_days(filename: str, days: dict) -> bool:
    """写回并去尾。``False`` ＝ 没写进去（怎么说话由调用方决定，这里不弹框）。"""
    kept = dict(sorted(days.items())[-KEEP_DAYS:])
    path = path_of(filename)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"days": kept}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)
    except OSError:
        log.exception("写按天记账文件失败（本次没存下）：%s", path)
        return False
    return True


def merge_legacy() -> bool:
    """把合并前那两份旧文件并进 :data:`AGENDA_FILENAME`，**并完删旧**（方案 §6.2）。

    :return: 有没有真做过合并。``False`` ＝ 没有旧文件（或写失败），什么都没落。

    **幂等**：旧文件读完就删，第二次调用直接返回 ``False``。写失败时**不删**旧的 ——
    下次启动重来，不会出现「新的没写成、旧的已经没了」。

    两个源之间按「先到者优先」：``agenda_log.json`` 里已有的那份最优先，然后是
    :data:`LEGACY_FILENAMES` 的顺序。两个源用不同的条目 id 前缀（``clk`` / ``td``），
    真撞上的只可能是「已经迁过一半」的中途 —— 那时 agenda 里那份才是新的。

    **条目 id 一个字不动**：配置那一半按 id 跳过已存在的（见 ``migrate.migrate_items``），
    所以新表里旧条目的 id 与原 id 相同，这份账的键天然接得上。哪天真要改名，这里得
    跟着改 —— 一个 id 变了、账还挂在旧 id 上，用户看到的是「迁移完今天的值没了」。
    """
    legacy = [name for name in LEGACY_FILENAMES if path_of(name).exists()]
    if not legacy:
        return False
    merged = read_days(AGENDA_FILENAME)
    for name in legacy:
        merged = _overlay(merged, read_days(name))
    if not write_days(AGENDA_FILENAME, merged):
        return False  # 写失败就不删旧的：下次启动重来
    for name in legacy:
        _remove(path_of(name))
    log.info("已把 %d 份旧按天记账文件并进 %s", len(legacy), AGENDA_FILENAME)
    return True


def _overlay(base: dict, extra: dict) -> dict:
    """把 ``extra`` 里**缺的那些**补进 ``base``（已有的一个字不动）。"""
    out = {date: dict(day) for date, day in base.items() if isinstance(day, dict)}
    for date, day in extra.items():
        if not isinstance(day, dict):
            continue
        target = out.setdefault(date, {})
        for key, value in day.items():
            target.setdefault(key, value)
    return out


def _remove(path: Path) -> None:
    """删掉一个已经并走的旧文件 —— **删不掉不算失败**（并已经完成，下次启动再试）。"""
    try:
        path.unlink()
    except OSError:
        log.warning("旧数据文件合并后没能删掉（下次启动再试）：%s", path)
