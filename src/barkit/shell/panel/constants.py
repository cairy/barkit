"""面板的模块级常量与全族共用的日志名。

``shell/panel.py`` 拆包（2026-10-01）：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import logging

from barkit.shell.design.levels import LEVEL_COLOR


#: 面板全族共用的日志名（`logging_setup` 的 `%(name)s` 会写进日志文件）。
#: 写死父包名而不是 `__name__`：子模块各自的 `__name__` 会把日志外观改掉。
log = logging.getLogger("barkit.shell.panel")


# Qt 的 QWIDGETSIZE_MAX（= 2^24-1，"无上界"）。PySide6 未导出这个 C 宏，故自建同值常量。
QWIDGETSIZE_MAX = (1 << 24) - 1


#: ``ButtonSpec.variant`` → QSS objectName（styles.py 里三族：primary / danger / pill-ghost）
_BUTTON_KIND: dict[str, str] = {
    "ghost": "pill-ghost",
    "primary": "primary",
    "danger": "danger",
}

# 级别严重度（str 键版）：与 shell/design/levels.SEVERITY / renderers/badge._SEVERITY_STR
# 同序，改一处须同步另两处。
_SEV: dict[str, int] = {"ok": 0, "idle": 1, "off": 2, "warn": 3, "err": 4}

# 级别（str 键）→ 前景色 token：**全框架只剩这一张色表**（决议 D16「两张色表合一」），
# 直接取自共享的 shell/design/levels.LEVEL_COLOR —— 标题栏图标染色与列表行状态角标
# 同源，两处不可能算出不同颜色。ok 也在表里（标题栏必须画出「一切正常」的那枚图标）。
_LEVEL_TOKEN: dict[str, str] = {lv.value: fg for lv, (fg, _bg) in LEVEL_COLOR.items()}

#: 列表行**画角标**的级别（决议 D16 / UI 规范 §5.3「ok 隐」）：健康不抢眼，
#: 「没有状态源」（None）同样不画 —— 角标是「有事要说」的位置。
_BADGE_LEVELS = ("idle", "off", "warn", "err")

#: `ActionResult(outcome=OK)` 自动退场的时限。成功那句话的存在理由是「确认听到了」，
#: 而状态本身（开关翻了 / 副标题变了）已经在接着讲同一件事 ⇒ 留久了只剩噪音。
#: 失败不设时限：它多半是「用户以为做成了」的唯一线索，等人看见再说（见
#: `present_action_result`）。
_RESULT_AUTO_HIDE_MS = 5000


# 标题栏图标条可见上限（U2 §5.2）：超出的插件收为末端溢出簇，留一个位置给溢出簇本身。
N_VIS = 7
