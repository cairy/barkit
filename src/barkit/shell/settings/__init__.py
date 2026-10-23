"""ui/settings：二级「设置」窗口（refined 设计，2026-08）。

本包按职责切分（2026-10-01，原 ``shell/settings.py`` 1930 行）：``SettingsWindow``
本体在 ``window.py``；其余方法按职责分进 ``pages`` / ``plugins`` / ``plugin_actions`` /
``framework`` / ``state`` / ``general`` / ``maintenance`` 七个混合类（与本体同体，经
``self`` 取用实例状态）；模块级常量在 ``constants``，小件在 ``helpers``。
这里**全量再导出**：调用方继续 ``from barkit.shell.settings import X``，包括原先就能
从本模块拿到的 Qt / permissions / specs 名字。
"""

from __future__ import annotations

# --- 原 ``settings.py`` 的全部外部导入，原样再导出（引用面零改动）---
import logging
from typing import TYPE_CHECKING, Callable
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from barkit.contribution.specs import (
    SettingsPageSpec,
    TRAY_MIN_LEVEL_DEFAULT,
    TRAY_MIN_LEVELS,
)
from barkit.core.config import PLUGIN_UI_SLOTS, slot_default_visible
from barkit.core.permissions import (
    ACCESSIBILITY_GUIDE,
    accessibility_trusted,
    available,
    open_accessibility_pane,
    request_accessibility,
)
from barkit.core.plugin import RUN_ON_DEMAND
from barkit.core.plugin_manager import PluginInfo, version_label
from barkit.core import logging_setup
from barkit.paths import APP_NAME
from barkit.shell.autostart_row import AutostartRow
from barkit.shell.daemon_ops import build_daemon_ops
from barkit.shell.data_ops import data_dir_button
from barkit.shell.hotkey_catalog import HotkeyHost
from barkit.shell.hotkeys_page import HotkeysPage
from barkit.shell import placement
from barkit.shell.macos_window import set_window_level, SETTINGS_LEVEL
from barkit.shell.order_editor import OrderEditor
from barkit.shell.state_machines_section import StateMachinesSection
from barkit.shell.ui_kit import (
    Card,
    ChoiceSelector,
    FeedbackBar,
    Switch,
    apply_text_role,
    form_row,
    insert_page_slot,
    insert_page_tail,
    is_standard_shell,
    note,
    settings_page_shell,
    settings_section,
    tool_button,
    play_entrance,
)
from barkit.shell.design import metrics

# --- 包内各子模块 ---
from .constants import (
    _BADGE_LEVEL_LABELS,
    _LOG_LEVEL_CHOICES,
    _LOG_LEVEL_LABELS,
    _POLL_LADDER,
    _SLOT_LABELS,
    log,
)
from .helpers import (
    _apply_chip,
    _badge_level_choices,
    _poll_choices,
    _poll_text,
    _same_poll,
    _scrolled,
)
from .pages import _PagesMixin
from .plugins import _PluginsMixin
from .plugin_actions import _PluginActionsMixin
from .framework import _FrameworkMixin
from .state import _StateMixin
from .general import _GeneralMixin
from .maintenance import _MaintenanceMixin
from .window import SettingsWindow

__all__ = [
    "ACCESSIBILITY_GUIDE",
    "APP_NAME",
    "AutostartRow",
    "Callable",
    "Card",
    "ChoiceSelector",
    "FeedbackBar",
    "HotkeyHost",
    "HotkeysPage",
    "OrderEditor",
    "PLUGIN_UI_SLOTS",
    "PluginInfo",
    "QDialog",
    "QFileDialog",
    "QFrame",
    "QHBoxLayout",
    "QLabel",
    "QListWidget",
    "QScrollArea",
    "QStackedWidget",
    "QVBoxLayout",
    "QWidget",
    "Qt",
    "RUN_ON_DEMAND",
    "SETTINGS_LEVEL",
    "SettingsPageSpec",
    "SettingsWindow",
    "StateMachinesSection",
    "Switch",
    "TRAY_MIN_LEVELS",
    "TRAY_MIN_LEVEL_DEFAULT",
    "TYPE_CHECKING",
    "_BADGE_LEVEL_LABELS",
    "_FrameworkMixin",
    "_GeneralMixin",
    "_LOG_LEVEL_CHOICES",
    "_LOG_LEVEL_LABELS",
    "_MaintenanceMixin",
    "_POLL_LADDER",
    "_PagesMixin",
    "_PluginActionsMixin",
    "_PluginsMixin",
    "_SLOT_LABELS",
    "_StateMixin",
    "_apply_chip",
    "_badge_level_choices",
    "_poll_choices",
    "_poll_text",
    "_same_poll",
    "_scrolled",
    "accessibility_trusted",
    "apply_text_role",
    "available",
    "build_daemon_ops",
    "data_dir_button",
    "form_row",
    "insert_page_slot",
    "insert_page_tail",
    "is_standard_shell",
    "log",
    "logging",
    "logging_setup",
    "metrics",
    "note",
    "open_accessibility_pane",
    "placement",
    "play_entrance",
    "request_accessibility",
    "set_window_level",
    "settings_page_shell",
    "settings_section",
    "slot_default_visible",
    "tool_button",
    "version_label",
]
