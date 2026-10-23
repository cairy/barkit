"""ui/panel：托盘 Popover 状态面板（refined 设计，2026-08 重构；U2 §10.3 折叠重做）。

左键点托盘 → 在图标下方弹出。结构：
- 标题栏：品牌标记（可点击展开 / 收起状态区）+ 组件图标条
- 状态区（可折叠）：各插件状态行（由贡献注册中心注入）
- 操作区：首要动作按钮（由贡献注册中心注入）
- 次级：设置 / 退出

折叠交互（U2 §10.3）：移除独立折叠箭头，改为「点品牌 logo / 点图标」展开与收起，
保留 200ms 高度动画与状态区最大高度上界复位逻辑。点击面板外由
``macos_window.PanelDismissController`` 关闭（多显示器安全）。
设计系统：颜色取自 barkit.shell.styles.TOKENS；自绘控件在 ``.widgets``。

**本包按职责切分**（2026-10-01，原 ``shell/panel.py`` 2370 行）：``StatusPanel`` 本体
在 ``panel.py``；其余方法按职责分进 ``chrome`` / ``state`` / ``actions`` / ``fold`` /
``place`` 五个混合类（与本体同体，经 ``self`` 取用实例状态）；模块级小控件在
``widgets``，状态行视图在 ``row``，常量与全族共用的日志名在 ``constants``。
这里**全量再导出**：调用方继续 ``from barkit.shell.panel import X``，包括原先就能从本
模块拿到的 Qt / specs 名字。
"""

from __future__ import annotations

# --- 原 ``shell/panel.py`` 的全部外部导入，原样再导出（引用面零改动）---
import logging
import time
import traceback
from PySide6.QtCore import (
    Qt,
    QPoint,
    QRect,
    QRectF,
    QSize,
    QTimer,
    Signal,
    Property,
    QPropertyAnimation,
    QEasingCurve,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QPainter,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from barkit.shell import placement, styles
from barkit.shell.macos_window import create_dismiss_controller, set_window_level
from barkit.shell.ui_kit import (
    Card,
    CandidateMenu,
    Divider,
    FeedbackBar,
    LogoWidget,
    MenuButton,
    ModeBadge,
    ProfileSelector,
    RecordRow,
    Switch,
    _theme_tokens,
    apply_text_role,
    draw_badge_glyph,
    draw_glyph,
    glyph_pixmap,
    relay_action_result,
    tool_button,
)
from barkit.contribution.specs import (
    ActionOutcome,
    ActionResult,
    ActionSpec,
    ButtonSpec,
    ChoiceSpec,
    ComponentSpec,
    IconName,
    MenuItem,
    MenuSpec,
    ModeBadgeSpec,
    StateReport,
    StatusRowSpec,
    SwitchSpec,
    as_action_result,
)
from barkit.shell.design import metrics
from barkit.shell.design.levels import LEVEL_COLOR, as_level
from barkit.shell.renderers.representative import representative
from barkit.shell.renderers.state_view import resolve

# --- 包内各子模块 ---
from .constants import (
    N_VIS,
    QWIDGETSIZE_MAX,
    _BADGE_LEVELS,
    _BUTTON_KIND,
    _LEVEL_TOKEN,
    _RESULT_AUTO_HIDE_MS,
    _SEV,
    log,
)
from .widgets import (
    _ClickableLogo,
    _OverflowCluster,
    _TitleBarIcon,
    _ValueBadge,
)
from .row import (
    _StatusRowView,
    row_subtitle_text,
)
from .actions import _ActionsMixin
from .chrome import _ChromeMixin
from .fold import _FoldMixin
from .place import _PlaceMixin
from .state import _StateMixin
from .panel import StatusPanel

__all__ = [
    "ActionOutcome",
    "ActionResult",
    "ActionSpec",
    "ButtonSpec",
    "CandidateMenu",
    "Card",
    "ChoiceSpec",
    "ComponentSpec",
    "Divider",
    "FeedbackBar",
    "IconName",
    "LEVEL_COLOR",
    "LogoWidget",
    "MenuButton",
    "MenuItem",
    "MenuSpec",
    "ModeBadge",
    "ModeBadgeSpec",
    "N_VIS",
    "ProfileSelector",
    "Property",
    "QColor",
    "QEasingCurve",
    "QFont",
    "QFrame",
    "QGuiApplication",
    "QHBoxLayout",
    "QIcon",
    "QLabel",
    "QPainter",
    "QPoint",
    "QPropertyAnimation",
    "QRect",
    "QRectF",
    "QScrollArea",
    "QSize",
    "QTimer",
    "QVBoxLayout",
    "QWIDGETSIZE_MAX",
    "QWidget",
    "Qt",
    "RecordRow",
    "Signal",
    "StateReport",
    "StatusPanel",
    "StatusRowSpec",
    "Switch",
    "SwitchSpec",
    "_ActionsMixin",
    "_BADGE_LEVELS",
    "_BUTTON_KIND",
    "_ChromeMixin",
    "_ClickableLogo",
    "_FoldMixin",
    "_LEVEL_TOKEN",
    "_OverflowCluster",
    "_PlaceMixin",
    "_RESULT_AUTO_HIDE_MS",
    "_SEV",
    "_StateMixin",
    "_StatusRowView",
    "_TitleBarIcon",
    "_ValueBadge",
    "_theme_tokens",
    "apply_text_role",
    "as_action_result",
    "as_level",
    "create_dismiss_controller",
    "draw_badge_glyph",
    "draw_glyph",
    "glyph_pixmap",
    "log",
    "logging",
    "metrics",
    "placement",
    "relay_action_result",
    "representative",
    "resolve",
    "row_subtitle_text",
    "set_window_level",
    "styles",
    "time",
    "tool_button",
    "traceback",
]
