"""core/plugin_manager：运行期插件编排（E3 ② 免重启启用 / 禁用 / 删除）。

持有 registry / contributions / scheduler / hotkeys / events / panel / config，统一编排单个
插件的启用 / 禁用 / 卸载，使这些操作**无需重启** app 即生效：

- enable(id)    ：实例化（若未实例）→ start → 注册热键 → 加入贡献注册中心 →
                  订阅状态 → 刷新面板 → 持久化 ``enabled=true``
- disable(id)   ：stop → 移除热键 → 移出贡献注册中心 → 反订阅状态 → 刷新面板 →
                  持久化 ``enabled=false``
- uninstall(id) （仅外部）：disable 后删外部插件目录 + 删配置段 + 从发现结果遗忘
- install(path) （仅外部）：目录 / 压缩包 → 预检（认身份、比版本）→ 原子换目录 →
  重扫发现 → 按原启用态起新实例（决议 D5；**覆盖确认归调用方**，见 preview_install）

状态订阅 / 首屏绘制经 ``on_state_update(spec, payload)`` 回调交给 app 层（托盘徽标逻辑留在
app），manager 只负责「何时订阅 / 反订阅」与生命周期编排。

文件 / 目录监视**已随会话通道一起下线**（2026-09-29）：插件不再声明路径，改为经
``Plugin.subjects()`` 声明**领域名**（``route`` / ``tunnel`` / ``hosts``）。框架把这份声明交给
会话（``subscribe``），守护推来的 ``system.helper.observed`` 按插件声明过滤后转交
``Plugin.on_observed``，并在禁用 / 卸载时反订阅（§8.2.4 无泄漏）。推的来源是
``shell/helper_session``（装配层），本模块只负责「谁订了哪几个领域」。

本包按职责切分（2026-10-02），`__init__.py` 里**全量再导出**那四枚外部名字
（``PluginManager`` / ``PluginInfo`` / ``version_label`` / ``InstallPreview``），
其余编排方法分散在九个混合类里，方法经 ``self`` 取用实例状态 ⇒ 零行为变更。

⚠️ 与 `shell/app_shell` 同款（形态 G）：**包属性是导入时那一份快照** ——
往本包上 ``monkeypatch`` 任何一个被方法引用的名字都是打进空气、判据纹丝不动；
打桩点一律落真身子模块（``barkit.core.plugin_manager.<子模块>``）。
"""

from __future__ import annotations

# --- 原 ``plugin_manager.py`` 的全部外部导入，原样再导出（引用面零改动）---
import logging
import shutil
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from barkit.core.resources import DeferredRemoval, PluginResources, SystemMutation
from barkit.contribution.points import SUPPORTED_API_VERSION
from barkit.contribution.specs import HotkeySpec, StateReport
from barkit.core.deferred_cleanup import DeferredCleaner
from barkit.core.events import HELPER_OBSERVED_TOPIC
from barkit.core.plugin import RUN_ON_DEMAND, RUN_PERIODIC, Plugin
from barkit.core.scheduler import poll_default_of
from barkit import paths  # 数据路径走模块属性，测试经 conftest/monkeypatch 重定向生效

# --- 包内各子模块 ---
from .consts import log
from .inventory import (
    PluginInfo,
    _InventoryMixin,
)
from .install import (
    InstallPreview,
    _InstallMixin,
    version_label,
)
from .uninstall import _UninstallMixin
from .revert import _RevertMixin
from .lifecycle import _LifecycleMixin
from .subscribe import _SubscriptionMixin
from .order import _OrderMixin
from .archive import _ArchiveMixin
from .hotkeys import _HotkeyMixin
from .notify import _NotifyMixin
from .plugin_manager import PluginManager

__all__ = [
    "Callable",
    "DeferredCleaner",
    "DeferredRemoval",
    "HELPER_OBSERVED_TOPIC",
    "HotkeySpec",
    "InstallPreview",
    "Path",
    "Plugin",
    "PluginInfo",
    "PluginManager",
    "PluginResources",
    "RUN_ON_DEMAND",
    "RUN_PERIODIC",
    "SUPPORTED_API_VERSION",
    "StateReport",
    "SystemMutation",
    "TYPE_CHECKING",
    "_ArchiveMixin",
    "_HotkeyMixin",
    "_InstallMixin",
    "_InventoryMixin",
    "_LifecycleMixin",
    "_NotifyMixin",
    "_OrderMixin",
    "_RevertMixin",
    "_SubscriptionMixin",
    "_UninstallMixin",
    "dataclass",
    "log",
    "logging",
    "partial",
    "paths",
    "poll_default_of",
    "shutil",
    "time",
    "version_label",
]
