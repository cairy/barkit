"""shell/design/levels：StateLevel → 色彩映射（方案 v3 §6.5 / UI 规范 §2）。

语义色只在此处定义一次，全框架引用；本文件存 **token 名**而非色值——
实际颜色经 TOKENS 解析，双主题自动换肤，杜绝硬编码。
"""

from __future__ import annotations

from typing import Iterable

from barkit.contribution.specs import StateLevel
from barkit.shell.design.tokens import token

# level -> (前景色 token, 背景色 token)
LEVEL_COLOR: dict[StateLevel, tuple[str, str]] = {
    StateLevel.OK: ("green", "green_soft"),
    StateLevel.WARN: ("orange", "orange_soft"),
    StateLevel.ERR: ("red", "red_soft"),
    StateLevel.IDLE: ("text3", "surface3"),
    StateLevel.OFF: ("text3", "surface3"),
}

#: level -> **文字档** token 名（与上面那张表的「前景」**不是同一档**，别混用）。
#:
#: ``LEVEL_COLOR`` 的前景是**图形档**（`green` / `orange` / `red`），判据是「压得住
#: 白**图形**」（与白 ≥3.0，见 tokens.py 模块 docstring 那张四档表）；拿它写 11.5px
#: 的小字，压在卡片底色上只有 3.5:1，读不动 —— 这正是四档拆分要根治的「一符两用」。
#: 所以**文字**一律走前景档；`IDLE` / `OFF` 用 `text2`（`text3` 是装饰档，写不成字）。
LEVEL_TEXT_COLOR: dict[StateLevel, str] = {
    StateLevel.OK: "green_text",
    StateLevel.WARN: "orange_text",
    StateLevel.ERR: "red_text",
    StateLevel.IDLE: "text2",
    StateLevel.OFF: "text2",
}

# 严重度序：代表态取「最坏」时用（§6.5 / UI 规范 §4.3）。
# ERR > WARN > OFF（功能关着）> IDLE（开着但无事）> OK。
# 公开导出：shell/renderers/representative 的代表态派生复用同一份序，禁止另立副本。
SEVERITY: dict[StateLevel, int] = {
    StateLevel.OK: 0,
    StateLevel.IDLE: 1,
    StateLevel.OFF: 2,
    StateLevel.WARN: 3,
    StateLevel.ERR: 4,
}


def as_level(name: "str | None") -> "StateLevel | None":
    """str 级别 → ``StateLevel``；空值 / 认不出来的名字返回 None。

    框架内部大量场景按 **str 级别**传（信号、事件总线、跨层数据），只有必须查
    枚举表时（图标库的级别兜底、级别色）才在这里折一次 —— 折算点只有这一个，
    别在各处各写一遍 try/except。
    """
    if not name:
        return None
    try:
        return StateLevel(name)
    except ValueError:
        return None


def level_colors(level: StateLevel, theme: str = "light") -> tuple[str, str]:
    """解析某级别在某主题下的 (前景色, 背景色) 实际色值。"""
    fg_name, bg_name = LEVEL_COLOR[level]
    return token(fg_name, theme), token(bg_name, theme)


def worst_level(levels: Iterable[StateLevel]) -> StateLevel | None:
    """取最坏级别；空集合返回 None（调用方决定兜底，不替它做语义假设）。"""
    result: StateLevel | None = None
    for lv in levels:
        if result is None or SEVERITY[lv] > SEVERITY[result]:
            result = lv
    return result
