"""``PluginManager`` 本体：只留 ``__init__``；实现分散在包内九枚混合类。

``plugin_manager.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Callable,
)

from barkit.core.resources import SystemMutation
from barkit.core.deferred_cleanup import DeferredCleaner
from barkit import paths

if TYPE_CHECKING:
    from barkit.core.config import Config
    from barkit.core.contributions import ContributionRegistry
    from barkit.core.events import EventBus
    from barkit.core.hotkeys import HotkeyManager
    from barkit.core.registry import Registry
    from barkit.core.scheduler import Scheduler
    from barkit.shell.panel import StatusPanel

from .archive import _ArchiveMixin
from .hotkeys import _HotkeyMixin
from .install import _InstallMixin
from .inventory import _InventoryMixin
from .lifecycle import _LifecycleMixin
from .notify import _NotifyMixin
from .order import _OrderMixin
from .revert import _RevertMixin
from .subscribe import _SubscriptionMixin
from .uninstall import _UninstallMixin


class PluginManager(
    _InventoryMixin,
    _LifecycleMixin,
    _SubscriptionMixin,
    _InstallMixin,
    _UninstallMixin,
    _RevertMixin,
    _OrderMixin,
    _ArchiveMixin,
    _HotkeyMixin,
    _NotifyMixin,
):
    # 混合类按**职责**分开在包内各子模块，方法经 ``self`` 取用实例状态，
    # 故 MRO 顺序不影响变量解析（混合类之间无继承、无重名）。本体只留 ``__init__``
    # —— 编排那条链路（enable / disable / uninstall / install）由包内那几族分着管。

    def __init__(
        self,
        registry: "Registry",
        contributions: "ContributionRegistry",
        scheduler: "Scheduler",
        hotkeys: "HotkeyManager",
        events: "EventBus",
        panel: "StatusPanel | None",
        config: "Config",
        on_state_update: Callable[[object, object], None],
        external_base: "Path | None" = None,
        heartbeat=None,
    ) -> None:
        self._registry = registry
        self._contributions = contributions
        self._scheduler = scheduler
        self._hotkeys = hotkeys
        self._events = events
        self._panel = panel
        self._config = config
        self._on_state_update = on_state_update
        # 外部插件根目录（卸载时删目录用）；默认固定路径，测试可覆盖。
        self._external_base = external_base or paths.EXTERNAL_PLUGINS_DIR
        # 延迟收尾（§9.1 第 4 步，2026-09-17）：有外部消费方的数据文件不能在卸载当场删，
        # 要等消费方收敛（`resources().deferred`）。时钟用框架心跳 —— 卸载跑在界面线程上，
        # 等一个外部进程的轮次绝不能阻塞界面。未接线（heartbeat=None）时退回「立即收尾」。
        self._deferred_cleanup = DeferredCleaner(heartbeat)
        # 已订阅的状态处理器：(plugin_id, topic) -> handler
        self._state_handlers: dict[tuple[str, str], Callable] = {}
        # 已订阅的守护观测：plugin_id -> (handler, 该插件声明并订阅成功的领域名集)。
        # 未接线（无会话）时 ``subjects()`` 声明被忽略（等价于「不订阅任何领域」）。
        self._subject_handlers: dict[str, tuple[Callable, frozenset]] = {}
        # 已登记的系统事件订阅（§8.3.1 通用订阅）：plugin_id -> [(topic, handler), ...]
        # 与领域订阅分开记：两者的撤销路径不同（领域那条要顺带反订阅会话）。
        self._topic_handlers: dict[str, list[tuple[str, Callable]]] = {}
        # 插件启用/禁用/卸载后回调（如通知设置窗重建分页），由 app 接线；
        # 仅作「变更已发生」的通知，不感知具体 UI。None 表示无人监听。
        self.on_plugins_changed: Callable[[], None] | None = None
        # 卸载确认回调（§9.1）：uninstall 在执行任何变更前，把插件的
        # system_mutations 交给 UI 展示；返回 False 中止卸载。None = 无 UI 确认
        # （CLI/测试语义，仅记日志）。
        self.on_uninstall_confirm: Callable[[str, list[SystemMutation]], bool] | None = None
        # 卸载第 4b 步（A 类无状态对账，§9.3）：入参 = 卸载后重算的 wanted 名集，
        # 返回是否完成。具体实现是 root 授权动作、在插件侧 daemon（app 唯一授权
        # 入口）—— core 不得依赖 plugins（分层门禁），故此处只留回调槽。
        # None = 未接线（仅记日志，与 on_uninstall_confirm 同款）。
        self.on_reap_root_binaries: Callable[[frozenset], bool] | None = None
        # 延迟收尾的**异步**结果：入参 (插件 id, 未完成原因；空串 = 已收尾)。
        # 有延迟时 uninstall() 早已返回，结论只能事后通知；None = 无人听（仅记日志）。
        self.on_uninstall_settled: Callable[[str, str], None] | None = None
