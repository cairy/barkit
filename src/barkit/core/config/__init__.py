"""core/config：YAML 配置读取、深度合并、热重载、原子写（方案 §4.5，任务 P0 0.3）。

本包按职责切分（2026-10-03），这里**全量再导出**：调用方继续
``from barkit.core.config import Config``，包括原先就能从本模块拿到的全部名字
（11 处导入逐字照旧，含 ``_topic_to_state_id`` 与
``PLUGIN_SECTION_FRAMEWORK_KEYS`` 两个私有名）。

⚠️ 混合类的方法体里的全局名解析的是**它自己那个子模块** ⇒ 往本包上
``monkeypatch`` 任何一个被方法引用的名字都是打进空气、判据纹丝不动；
打桩点一律落真身子模块（本批全仓零模块级打桩，已逐条核过）。
"""

from __future__ import annotations

# --- 原 ``config.py`` 的全部外部导入，原样再导出（引用面零改动）---
import logging
import os
from pathlib import Path
from typing import Callable
import yaml
from PySide6.QtCore import QObject, QTimer, Signal
from barkit import paths
from barkit.contribution.specs import TRAY_MIN_LEVEL_DEFAULT, TRAY_MIN_LEVELS

# --- 包内各子模块 ---
from .consts import (
    DEFAULT_CONFIG_PATH,
    PLUGIN_SECTION_FRAMEWORK_KEYS,
    PLUGIN_UI_SLOTS,
    PLUGIN_UI_SLOT_DEFAULT,
    SUPPORTED_CONFIG_SCHEMA_VERSION,
    log,
    slot_default_visible,
)
from .schema import (
    ConfigError,
    deep_merge,
    validate,
)
from .store import (
    _load_yaml,
    atomic_write,
)
from .read import _ReadMixin
from .plugins import _PluginMixin
from .shellcfg import (
    _ShellCfgMixin,
    _topic_to_state_id,
)
from .hotkeys import _HotkeyMixin
from .tuning import _TuningMixin
from .persist import _PersistMixin
from .config import Config

__all__ = [
    "Callable",
    "Config",
    "ConfigError",
    "DEFAULT_CONFIG_PATH",
    "PLUGIN_SECTION_FRAMEWORK_KEYS",
    "PLUGIN_UI_SLOTS",
    "PLUGIN_UI_SLOT_DEFAULT",
    "Path",
    "QObject",
    "QTimer",
    "SUPPORTED_CONFIG_SCHEMA_VERSION",
    "Signal",
    "TRAY_MIN_LEVELS",
    "TRAY_MIN_LEVEL_DEFAULT",
    "_HotkeyMixin",
    "_PersistMixin",
    "_PluginMixin",
    "_ReadMixin",
    "_ShellCfgMixin",
    "_TuningMixin",
    "_load_yaml",
    "_topic_to_state_id",
    "atomic_write",
    "deep_merge",
    "log",
    "logging",
    "os",
    "paths",
    "slot_default_visible",
    "validate",
    "yaml",
]
