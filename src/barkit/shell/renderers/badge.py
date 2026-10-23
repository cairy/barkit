"""shell/renderers/badge：托盘队列（决议 D17 / 契约规范 §2.5）。

**托盘只有一枚菜单栏项**，结构 = 「底图 + 右上角一枚角标」。角标画谁、画什么，
由本模块决定，三步规则：

1. **候选** —— 每个**组件**取它的代表态（``representative`` ＋ ``state_view.resolve``
   解析出级别 / 取值图标 / 提醒意图）。用户的「关掉某组件的托盘显示」属候选层的
   过滤，归显示位开关那一批 —— 本模块只接受已经过滤好的候选。
2. **触发** —— 该取值的作者意图 ``alerts`` 为真，且级别不低于全局阈值 ``min_level``
   （出厂档 ``warn``，值域归 ``contribution.specs.TRAY_MIN_LEVELS``，
   用户在「设置 → 通用 → 菜单栏」里改）。**``ERR`` 一律露出**：用户阈值最多收紧到
   ``warn``，屏蔽不了 err。
3. **取胜** —— 触发者里级别最坏的**那一级**是唯一允许上屏的级别；同级内按
   ``plugin_order`` 排队，界面每 3 秒轮播（轮播归 shell，本模块只给队列）。

两条不变式（改之前先读）：

- **队列只有一个级别**：更轻级别的触发者被**压制**，不上屏也不排队；最坏那一级
  消失（被处理掉或降级）才整队更换。``ERR`` 与 ``WARN`` 永不混在同一队列里。
- **顺序 = ``plugin_order``**，不随机、不按名字 —— 与标题栏图标条、设置页同一个
  顺序源（§6.4）。

熔断（组件抛异常被调度器摘掉）走**即时通道**：不等 ``stale_after`` 超时，直接按
ERR 进队 —— 「出事一定看得见」是这条显示位的承诺（用户裁定 2026-09-15）。

纯函数、零 Qt 依赖，可单测。渲染（画圆盘与字形）在 ``shell/status_item.py``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Sequence

from barkit.contribution.specs import TRAY_MIN_LEVEL_DEFAULT, TRAY_MIN_LEVELS

if TYPE_CHECKING:                                    # 仅为类型标注；本模块不 import Qt
    from barkit.shell.design.icons import IconName

#: 级别（str 键）→ 严重度。调用方给的是**已解析**的级别字符串（``state_view.resolve``
#: 的产物），与 ``shell/design/levels.SEVERITY``（``StateLevel`` 键）同序 ——
#: 改一处必须同步改另一处。
_SEVERITY: dict[str, int] = {"ok": 0, "idle": 1, "off": 2, "warn": 3, "err": 4}

#: 熔断文案。框架级兜底，不进组件声明 —— 它说的不是组件自己的某个业务状态，
#: 与 ``state_view.STALE_TEXT`` 同类。
BROKEN_TEXT = "运行失败已熔断"


@dataclass(frozen=True)
class Candidate:
    """一个**组件**在托盘上的候选资格：它代表谁、现在什么级别、该画哪枚取值图标。"""

    plugin_id: str
    level: str                      # 代表态级别（已解析）
    alerts: bool                    # 该取值的作者提醒意图（``StateOption.alerts_on``）
    icon: "IconName | None"         # 代表态取值图标；None → 画该级别的通用标记
    tooltip: str                    # 悬停提示（组件名 + 代表态文案，调用方合成）
    broken: bool = False            # 组件已熔断：级别与提醒意图都按 ERR 论，不等失联


def tray_queue(
    candidates: Iterable[Candidate],
    order: Sequence[str],
    *,
    min_level: str = TRAY_MIN_LEVEL_DEFAULT,
) -> tuple[Candidate, ...]:
    """算出托盘队列；空元组 = 这一拍不画角标。

    Args:
        candidates: 各组件代表态的候选（顺序不重要，出队前按 ``order`` 排）。
        order: 插件顺序（``registry.plugin_order()``）；不在册的排到最后，
            并保留传入顺序（``sorted`` 稳定）。
        min_level: 全局阈值，只认 ``TRAY_MIN_LEVELS``（用户阈值只有两档：「屏蔽
            err」不是可选项，``ERR`` 一律露出）；非法值按出厂档 ``warn``（宁可多露
            一点，也不静默调低可见性）。
    """
    threshold = (
        _SEVERITY[min_level]
        if min_level in TRAY_MIN_LEVELS
        else _SEVERITY[TRAY_MIN_LEVEL_DEFAULT]
    )
    triggering = [
        c for c in candidates
        # 熔断不是「某个取值的作者意图」能表达的状态 ⇒ 两条都按 ERR 论，
        # 不收 alerts 与用户阈值的约束（ERR 一律露出）。
        if (True if c.broken else c.alerts)
        and (_SEVERITY.get(c.level, 0) >= threshold or c.broken or c.level == "err")
    ]
    if not triggering:
        return ()
    worst = max(_SEVERITY.get(c.level, 0) for c in triggering)
    same_level = [
        c for c in triggering
        if (c.broken and worst == _SEVERITY["err"]) or _SEVERITY.get(c.level, 0) == worst
    ]
    rank = {pid: i for i, pid in enumerate(order)}
    return tuple(sorted(same_level, key=lambda c: rank.get(c.plugin_id, len(rank))))
