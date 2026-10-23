"""``AppShell`` 本体：类声明、实例状态、``run`` / ``assemble``（组合根）与 ``_shutdown``；实现分散在包的其余子模块。

``app_shell.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QApplication,
    QSystemTrayIcon,
)

from barkit.contribution.specs import StateReport
from barkit.core.audio import AudioDeviceSource
from barkit.core.config import Config
from barkit.core.contributions import ContributionRegistry
from barkit.core.heartbeat import Heartbeat
from barkit.core.hotkeys import HotkeyManager
from barkit.core.plugin import verify_context
from barkit.core.plugin_manager import PluginManager
from barkit.core.power import PowerSource
from barkit.core.registry import Registry
from barkit.core.scheduler import Scheduler
from barkit.core.variables import VariableHost
from barkit.shell.detail import DetailHost
from barkit.shell.hotkey_feedback import HotkeyFeedback
from barkit.shell.helper_session import HelperSession
from barkit.shell.interaction import InteractionCenter
from barkit.shell.panel import StatusPanel
from barkit.shell.renderers.badge import Candidate
from barkit.shell.settings import SettingsWindow
from barkit.shell.status_item import StatusItem

from .core import _CoreMixin
from .feedback import _FeedbackMixin
from .hotkeys import _HotkeyMixin
from .tray import _TrayMixin
from .ui import _UIMixin
from .variables import _VariablesMixin


class AppShell(
    _CoreMixin,
    _HotkeyMixin,
    _FeedbackMixin,
    _UIMixin,
    _VariablesMixin,
    _TrayMixin,
):
    # 混合类按**职责**分开在包内各子模块，方法经 ``self`` 取用实例状态，
    # 故 MRO 顺序不影响变量解析（混合类之间无继承、无重名）。本体只留
    # 「组合根那三件」：``__init__`` / ``run`` / ``assemble`` / ``_shutdown``。

    """BarKit 装配器：持有全部运行时对象，并把它们接成一条完整链路。"""

    def __init__(self, app: QApplication) -> None:
        self.app = app
        # 托盘队列（决议 D17）：这一拍允许上屏的触发者，按 plugin_order 排队。
        # 每拍由 _push_badge 重算——级别变化、失联翻转、熔断都会让它重新进队。
        self._tray_queue: tuple[Candidate, ...] = ()
        # 状态 topic → 最近一次报告（供托盘底图解析**取值图标**，P0③）
        self._state_reports: dict[str, StateReport] = {}
        # 已熔断的插件 id 集合（scheduler.broken/recovered 维护）
        self._broken_ids: set[str] = set()
        # 下列对象由 assemble() 按依赖顺序装配（先 panel 后 manager，先 status_item 后徽标）
        self._contributions: ContributionRegistry | None = None
        self._manager: PluginManager | None = None
        self._settings: SettingsWindow | None = None
        self.panel: StatusPanel | None = None
        self.status_item: StatusItem | None = None
        # 非 macOS 的回退托盘（QSystemTrayIcon）；macOS 下恒为 None
        self._tray: QSystemTrayIcon | None = None
        # 详情页托管器（§5.4）：按 plugin_id 打开插件声明的 L3 任务流
        self._detail_host = DetailHost(None)
        # 框架唯一时钟源（批次 8 §8.3.3 层 2）：_build_core 创建，装配器持有
        self._heartbeat: Heartbeat | None = None
        # 与 root helper 的那条会话（2026-09-29）：_build_core 创建，装配器持有；
        # 退出时 stop() —— 关连接即对端收到 EOF，依附型意图（隧道）随之撤回。
        self._helper_session: HelperSession | None = None
        # 框架统一电源事件源（§8.2.1）：系统与显示器两族共四条（`system.power.*`）；
        # 非 macOS 为 None
        self._power: PowerSource | None = None
        # 框架统一音频设备事件源（§8.2.1）：插拔 / 默认设备漂移 → system.audio.device_changed
        self._audio: AudioDeviceSource | None = None
        # 条件触发询问的中枢（批 2）：插件在 start() 里向它登记规则，装配收尾时
        # mark_started() 跑首轮求值
        self._interaction: InteractionCenter | None = None
        # 框架级变量池的宿主（设计文档 §2.3）：订阅组件状态 topic、挂心跳、广播变化、
        # 登记 ask 询问。由 `_build_variables` 建，装配器持有以便退出时 dispose。
        self._variables: VariableHost | None = None
        # core 四件套：assemble() 装配后持有（类职责即「持有全部运行时对象」，
        # 由它们支撑 _shutdown；也让冒烟测试能在装配后直接断言/收尾）
        self._config: Config | None = None
        self._scheduler: Scheduler | None = None
        self._registry: Registry | None = None
        self._hotkeys: HotkeyManager | None = None
        # 装配完成标志：_shutdown 的幂等闸门（aboutToQuit 与手动收尾可能都触发）
        self._assembled = False
        #: 自建热键名 → 回调。**必须缓存**：热键注册表的冲突判定用 ``==`` 比回调，
        #: 每次现造一个 lambda 的话两次不等，重挂时会被判成「撞了别的回调」（即撞自己）。
        self._custom_callbacks: dict = {}
        #: 正在跑命令的线程（**持有引用**，否则 Python 侧回收后线程还在跑 ⇒ 崩）
        self._runners: list = []
        #: 热键触发之后的反馈分派（成功按条目选 none/overlay/notify，失败一律通知）
        self._feedback = HotkeyFeedback(notify=self._send_hotkey_notice)

    # ---------- 装配主流程 ----------

    def run(self) -> int:
        """装配 + 进入事件循环；返回 ``QApplication.exec()`` 的退出码。"""
        self.assemble()
        return self.app.exec()

    def assemble(self) -> None:
        """完成**全部装配但不进入事件循环**（《重构-开工手册》§4.3 的 ⑧ 的可测入口）。

        拆出来是为了让装配链路可被测试完整走一遍：``run()`` 的后半段
        ``app.exec()`` 会永久阻塞，在此之前的所有接线（core → 插件 → 贡献注册中心
        → 热键 → 面板/设置/菜单栏 → 徽标 → 退出回调）才是组合根的全部职责，
        也正是「无测试覆盖」的那一段。

        装配完把 ``config/scheduler/registry/hotkeys`` 存为实例属性：本类职责即
        「持有全部运行时对象」，``_shutdown()`` 因而无参可调。
        """
        self._set_accessory_policy()

        config, events, scheduler, registry, hotkeys, heartbeat, helper_session, power, audio = \
            self._build_core()
        self._config = config
        self._scheduler = scheduler
        self._registry = registry
        self._hotkeys = hotkeys
        self._heartbeat = heartbeat
        self._helper_session = helper_session
        self._power = power
        self._audio = audio
        # 交互中枢必须在插件 start() **之前**挂进 ctx：插件正是在 start() 里登记规则
        # （晚了那一批规则就没人收）。``mark_started`` 则在整个装配结束后才叫（§条件
        # 触发询问 批 2）—— 那时才轮得到「程序已启动」这一位。
        self._interaction = self._build_interaction(registry, events, heartbeat)
        # 变量池（§2.3）：建在插件 start() **之前** —— 它的 ask 规则要赶在
        # `mark_started()` 之前登记完（那一轮就是「程序已启动」这一位唯一一次被置位的
        # 时刻）；它的订阅面只认 topic 名字，也就不需要等组件先起。
        self._build_variables(config, events, heartbeat, self._interaction)
        # 挂进 ctx 的是**池**而不是宿主：组件要问的是「此刻这个变量是多少」（§4），
        # 不是「声明从哪来、怎么重载」——后者是框架与设置页的事。
        registry.ctx.variables = self._variables.pool
        # 注入口的形状校验放在**插件实例化之前**：interaction / helper / variables 三个
        # 字段此前只靠组件侧 ``getattr`` 探测 ＋ 静默降级，装配接错对象要到某组件的某条
        # 路径上才显形（且无声）。此刻除 ``open_detail`` 外全部注入口已挂；未挂的是
        # ``None``，按契约允许的降级态跳过。
        verify_context(registry.ctx)

        plugins = registry.load_all()
        # §9.5 配置默认值单一真源：注入各插件 config_schema()，使默认值由
        # schema 合成层提供，取代 config.default.yaml 的插件段（enabled 除外）。
        # 必须在 load_all 之后、任何读取 plugin_config 的行为之前。
        self._config.set_plugin_schemas({p.id: p.config_schema() for p in plugins})
        contributions = self._build_contributions(plugins, registry, scheduler)
        self._contributions = contributions
        self._detail_host = DetailHost(contributions)
        # 插件主动打开自己详情窗的出口（S18）：与 mark_dirty / notify 同款「框架注入、
        # 组件只管用」。签名带 plugin_id（ctx 是共享单例、拿不到调用者身份），由
        # ``Plugin.open_detail`` 包装填上。必须在插件 start() 之前挂好。
        registry.ctx.open_detail = self._detail_host.open
        self._register_hotkeys(contributions, config, hotkeys)

        panel = self._build_panel(scheduler, events)
        manager = self._build_manager(
            registry, contributions, scheduler, hotkeys, events, panel, config,
            heartbeat,
        )
        settings = self._build_settings(contributions, manager)
        self._manager = manager
        self._settings = settings
        self._build_status_item(panel, settings)
        self._build_badge(contributions, scheduler, panel)
        # 会话**最后才连**：此刻全部意图（``load_all`` 期的声明）与全部订阅
        # （``subscribe_all_active``）都已记在框架侧，一连上就一次性断言出去，
        # 少一批「连上了才开始报」的往返。连不上也无所谓 —— 重连定时器接着试。
        if self._helper_session is not None:
            self._helper_session.start()
        # 框架内置热键最后注册：它们的回调要碰菜单栏图标与设置窗（此刻才存在）
        self._register_framework_hotkeys()
        # 自建条目同样在最后：它们跑的命令与配置同批，此时配置已注入插件 schema
        self._register_custom_hotkeys()
        # 手改 YAML 也即时生效（设置窗路径另有回调，见 _build_settings）：用户在配置里
        # 能改的东西有两类 —— 托盘底图来源、三个显示位的开关。
        config.changed.connect(self._on_config_changed)
        self._paint_initial_states(contributions)
        self._apply_tray_base()

        # 装配完成 ⇒ 置位「程序已启动」这一条件并跑首轮求值（插件的规则此刻都已登记）。
        self._interaction.mark_started()
        # 插件的 start() 里可能改过**顶层**配置（今天只有一种：待办提醒把旧条目表一次性
        # 迁成「变量 ＋ 任务」，那一步要往 ``variables:`` 段里加几条声明）。变量池是在
        # 插件 start() **之前**按那一刻的声明建的 ⇒ 不在这里再读一次，迁移刚写出来的那
        # 几个变量在**这一整次会话**里都不存在（任务的提醒时刻恒算不出来，而界面上只是
        # 那一条不出现）。放在 mark_started 之后：这一步会重登记 ask 规则，而
        # ``VariableHost._register_asks`` 自己会再叫一次 ``mark_started`` 把「程序已启动」
        # 这一位补上，于是不必让浮层赶在装配收尾之前弹。
        self._on_variables_config_changed()

        self._assembled = True
        self.app.aboutToQuit.connect(self._shutdown)

    # ---------- 退出 ----------

    def _shutdown(self) -> None:
        """退出清理（无参实例方法，幂等）。

        顺序即依赖顺序，**不得重排**：先停调度（不再有新的 tick 派发）→ 停长驻服务
        （join 在飞的 worker）→ 撤热键 → 关会话 / 释放电源观察者（§8.2.1 懒加载的反向：
        全撤即卸载）→ 停配置轮询 → 最后停表（前面各步可能仍依赖到期回调）。

        幂等闸门 ``_assembled``：``aboutToQuit`` 与测试里的手动收尾可能都会调到。
        """
        if not self._assembled:
            return
        self._assembled = False
        self._scheduler.stop_all()
        self._registry.stop_all()
        # 交互中枢先收：它在屏上可能还留着一个询问浮层，插件都停了那答案就没人接了。
        if self._interaction is not None:
            self._interaction.dispose()
        # 变量池紧随其后：撤订阅 + 摘心跳句柄（两条**都要显式做** —— 心跳持有回调引用，
        # 只把本地句柄置空等于「以为自己退了、心跳还在每 30 秒叫它一次」）。
        if self._variables is not None:
            self._variables.dispose()
        self._hotkeys.unregister_all()
        # 收还在跑的命令线程（热键触发，最长 TIMEOUT_SEC）：退出路上它们若被回收时
        # 仍在跑，就是「线程还在跑就析构 QThread」⇒ Qt qFatal（见 thread_reap 的成因）。
        self._stop_runners()
        # 关会话：对端立刻收到 EOF ⇒ 依附型意图（隧道）当场撤回。必须在插件停摆之后
        # ——停摆过程里组件可能还要下发最后一版「全撤」意图。
        if self._helper_session is not None:
            self._helper_session.stop()
        self._power.close()
        self._audio.close()
        self._config.stop_watching()
        self._heartbeat.stop()

        # ---- UI 层收尾（此前完全没碰，遗留定时器 / 原生观察者 / 悬空连接）----
        if self.panel is not None:
            # 面板自身的定时器 / 主题信号 / 原生 dismiss 控制器在 teardown 内拆
            self.panel.teardown()
        if self.status_item is not None:
            self.status_item.teardown()
        if self._settings is not None:
            try:
                self._settings.close()
            except Exception:  # noqa: BLE001
                pass
        self._detail_host.close()
        # config / app / scheduler 是长跑对象，其上的连接不会随 widget 销毁自动消失：
        # 显式断开，避免 AppShell 被信号闭包长期持有、或回调进已拆的 UI。
        if self._config is not None:
            try:
                self._config.changed.disconnect(self._on_config_changed)
            except Exception:  # noqa: BLE001
                pass
        try:
            self.app.aboutToQuit.disconnect(self._shutdown)
        except Exception:  # noqa: BLE001
            pass
        if self.panel is not None:
            try:
                self.panel.stateLevelChanged.disconnect(self._on_state_level_changed)
            except Exception:  # noqa: BLE001
                pass
        if self._scheduler is not None:
            try:
                self._scheduler.broken.disconnect(self._sched_broken_slot)
                self._scheduler.recovered.disconnect(self._sched_recovered_slot)
            except Exception:  # noqa: BLE001
                pass
