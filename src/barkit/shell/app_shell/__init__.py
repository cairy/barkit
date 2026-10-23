"""app_shell：装配器（方案 v3 §4，替代原 app.py 的「上帝接线」）。

本包按职责切分（2026-10-02），这里**全量再导出**：调用方继续
``from barkit.shell.app_shell import AppShell``，包括原先就能从本模块拿到的全部名字。

⚠️ 混合类的方法体里的全局名解析的是**它自己那个子模块** ⇒ 往本包上
``monkeypatch`` 任何一个被方法引用的名字（``StatusItem`` / ``HotkeyManager`` /
``effective_combo`` 这些）都是打进空气、判据纹丝不动；打桩点一律落真身子模块
（``barkit.shell.app_shell.ui`` / ``.core`` / ``.hotkeys``）。
"""

from __future__ import annotations

# --- 原 ``app_shell.py`` 的全部外部导入，原样再导出（引用面零改动）---
import logging
import time
from dataclasses import replace
from PySide6.QtGui import QColor, QCursor, QIcon
from PySide6.QtWidgets import QApplication, QSystemTrayIcon
from barkit.contribution.specs import IconName, StateReport
from barkit.core import thread_reap
from barkit.core.audio import AudioDeviceSource
from barkit.core.config import Config
from barkit.core.contributions import ContributionRegistry
from barkit.core.events import PANEL_OPENED_TOPIC, EventBus
from barkit.core.heartbeat import Heartbeat
from barkit.core.hotkey_command import CommandRunner
from barkit.core.hotkeys import HotkeyManager
from barkit.core.plugin_manager import PluginManager
from barkit.core.power import PowerSource
from barkit.core.registry import Registry
from barkit.core.scheduler import Scheduler
from barkit.paths import APP_NAME
from barkit.shell.detail import DetailHost
from barkit.shell.hotkey_catalog import (
    FEEDBACK_DEFAULTS,
    FEEDBACK_NONE,
    FEEDBACK_NOTIFY,
    FRAMEWORK_ACTIONS,
    REBIND_BOUND,
    REBIND_FAILED,
    REBIND_UNBOUND,
    FrameworkAction,
    HotkeyHost,
    SOURCE_CUSTOM,
    SOURCE_FRAMEWORK,
    SOURCE_PLUGIN,
    effective_combo,
    read_customs,
)
from barkit.shell.hotkey_feedback import HotkeyFeedback, outcome_of, wrap_hotkey_callback
from barkit.shell.helper_session import HelperSession
from barkit.shell.interaction import InteractionCenter
from barkit.shell.macos_window import POLICY_ACCESSORY, set_activation_policy
from barkit.shell import placement
from barkit.shell.panel import StatusPanel
from barkit.shell.renderers.badge import BROKEN_TEXT, Candidate, tray_queue
from barkit.shell.renderers.representative import representative
from barkit.shell.renderers.state_view import resolve
from barkit.shell.settings import SettingsWindow
from barkit.shell.status_item import StatusItem
from barkit.shell.ui_kit import tray_base_pixmap, tray_render_dpr

# --- 包内各子模块 ---
from .consts import (
    FRAMEWORK_NOTIFY_ID,
    _EXIT_GRACE_MS,
    log,
)
from .core import _CoreMixin
from .hotkeys import _HotkeyMixin
from .feedback import _FeedbackMixin
from .ui import _UIMixin
from .tray import _TrayMixin
from .app_shell import AppShell

__all__ = [
    "APP_NAME",
    "AppShell",
    "AudioDeviceSource",
    "BROKEN_TEXT",
    "Candidate",
    "CommandRunner",
    "Config",
    "ContributionRegistry",
    "DetailHost",
    "EventBus",
    "FEEDBACK_DEFAULTS",
    "FEEDBACK_NONE",
    "FEEDBACK_NOTIFY",
    "FRAMEWORK_ACTIONS",
    "FRAMEWORK_NOTIFY_ID",
    "FrameworkAction",
    "Heartbeat",
    "HelperSession",
    "HotkeyFeedback",
    "HotkeyHost",
    "HotkeyManager",
    "IconName",
    "InteractionCenter",
    "PANEL_OPENED_TOPIC",
    "POLICY_ACCESSORY",
    "PluginManager",
    "PowerSource",
    "QApplication",
    "QColor",
    "QCursor",
    "QIcon",
    "QSystemTrayIcon",
    "REBIND_BOUND",
    "REBIND_FAILED",
    "REBIND_UNBOUND",
    "Registry",
    "SOURCE_CUSTOM",
    "SOURCE_FRAMEWORK",
    "SOURCE_PLUGIN",
    "Scheduler",
    "SettingsWindow",
    "StateReport",
    "StatusItem",
    "StatusPanel",
    "_CoreMixin",
    "_EXIT_GRACE_MS",
    "_FeedbackMixin",
    "_HotkeyMixin",
    "_TrayMixin",
    "_UIMixin",
    "effective_combo",
    "log",
    "logging",
    "outcome_of",
    "placement",
    "read_customs",
    "replace",
    "representative",
    "resolve",
    "set_activation_policy",
    "thread_reap",
    "time",
    "tray_base_pixmap",
    "tray_queue",
    "tray_render_dpr",
    "wrap_hotkey_callback",
]
