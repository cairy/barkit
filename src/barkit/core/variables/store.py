"""core/variables/store：变量运行值的**按日账**（``variables_state.json``）。

这是框架级变量池唯一一处落盘（设计文档 §2.5）。形状与 ``todo/day_log.py`` 的
``agenda_log.json`` **逐字同款**（``{"days": {"<日期>": {"<变量 id>": "<裸值>"}}}``）：
那份是「今天的事实」，去尾 30 天、坏文件当空、原子替换 —— 两条理由都成立（一个常驻
app 的痕迹不该长成一份日志；今天的事实读不出来只该让今天从头算）。

**为什么与 ``agenda_log.json`` 分成两份而不是复用一份**（设计文档 §2.5 的裁定）：

- ``agenda_log.json`` 是 **todo 插件**的数据文件：它进 ``plugin.resources()`` 的清单，
  卸载时随插件删、导出用户数据时按插件走。框架级的变量值住进去，会在那个组件被卸载 /
  停用 / 恢复默认时一起消失；
- 反过来，变量池是**框架**级的（跨组件共享），它的存活不该取决于任何单个组件。

**为什么不经过配置门面**：这是**数据不是配置**（与 ``day_log`` 同一条）。每天写一次
用户配置会触发 ``config.changed`` 广播，还会被「恢复默认配置」冲掉。

**文件名常量在这里而不是 ``paths.py``**：本模块是唯一读写方，与 ``interaction_state.json``
（``core/interaction.LEDGER_FILENAME``）、``agenda_log.json``（``todo/day_log.AGENDA_FILENAME``）
同款。它进 ``doctor/checks_data.USER_JSON_FILES``：改名时那条对拍会转红，不会静默指向
一个不存在的文件。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

#: 运行值文件名（数据根下）。``doctor`` 的 D2 按这个名字检查它读不读得出来。
STATE_FILENAME = "variables_state.json"

#: 只留最近这么多天（与 ``day_log.KEEP_DAYS`` 同一个量级与同一条理由）。
KEEP_DAYS = 30

#: 天表里那层包装的键名（与 ``day_log`` 同款；换形状要连 doctor 一起看）。
_ROOT_KEY = "days"


def path() -> Path:
    """这份文件在哪。**调用期**取根 —— 把 ``paths.APP_SUPPORT`` 写进默认参数值会在
    定义期冻结路径，测试的隔离（``BARKIT_DATA_DIR`` / 模块属性补丁）当场失效。"""
    from barkit import paths

    return Path(paths.APP_SUPPORT) / STATE_FILENAME


def read_days() -> "dict[str, dict[str, str]]":
    """读整份天表；读不出来（没有 / 坏了 / 形状不对）一律**当空**。

    这份文件是「今天用户填了什么」：丢了只会让今天重新问一遍，不该让变量池起不来。
    损坏记一条 warning 留痕（``doctor`` 的 D2 会把「解析不了」顶到用户眼前，这里不重复报）。
    """
    try:
        raw = path().read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("变量运行值文件损坏，按空处理：%s", path())
        return {}
    days = data.get(_ROOT_KEY) if isinstance(data, dict) else None
    if not isinstance(days, dict):
        return {}
    out: "dict[str, dict[str, str]]" = {}
    for day, values in days.items():
        if isinstance(values, dict):
            out[str(day)] = {str(k): str(v) for k, v in values.items()}
    return out


def read_day(day: str) -> "dict[str, str]":
    """某一天的整表（``{变量 id: 裸值}``）；没有就是空表。"""
    return dict(read_days().get(day) or {})


def write_days(days: "dict[str, dict[str, str]]") -> bool:
    """写回并去尾。``False`` ＝ 没写进去（怎么说话由调用方决定，这里不弹框）。"""
    kept = dict(sorted(days.items())[-KEEP_DAYS:])
    target = path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(
            json.dumps({_ROOT_KEY: kept}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, target)
    except OSError:
        log.exception("写变量运行值失败（本次没存下）：%s", target)
        return False
    return True


def write_value(var_id: str, raw: str, day: str) -> bool:
    """写一个变量在某一天的裸值（**唯一写口**：读—改—写一次完成）。空串 ＝ 删掉这一笔。

    为什么要有这个口而不是让调用方自己 ``read_days`` → 改 → ``write_days``：
    那样「读整份 → 改一格 → 写整份」这段就成了每个调用点各写一遍的样板，而其中任一处
    少写一次写回，症状是「值填了、重启就没了」。
    """
    if not var_id:
        return False
    days = read_days()
    today = dict(days.get(day) or {})
    if raw:
        today[var_id] = str(raw)
    else:
        today.pop(var_id, None)
    if today:
        days[day] = today
    else:
        days.pop(day, None)
    return write_days(days)
