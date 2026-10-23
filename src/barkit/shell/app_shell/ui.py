"""面板 / 管理器 / 设置窗 / 菜单栏状态项的构造与接线。

``app_shell.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtGui import (
    QColor,
    QCursor,
    QIcon,
)
from PySide6.QtWidgets import QSystemTrayIcon

from barkit.contribution.specs import IconName
from barkit.core.events import PANEL_OPENED_TOPIC
from barkit.core.plugin_manager import PluginManager
from barkit.paths import APP_NAME
from barkit.shell.hotkey_catalog import HotkeyHost
from barkit.shell import placement
from barkit.shell.panel import StatusPanel
from barkit.shell.settings import SettingsWindow
from barkit.shell.status_item import StatusItem
from barkit.shell.ui_kit import (
    tray_base_pixmap,
    tray_render_dpr,
)

from .consts import log


class _UIMixin:
    """面板 / 插件管理器 / 设置窗 / 菜单栏状态项的构造与信号接线，外加锚与托盘图标的取法。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    def _build_panel(self, scheduler, events) -> StatusPanel:
        panel = StatusPanel()
        panel.set_scheduler(scheduler)  # F.3：熔断状态 + “重试”清零计数
        # 面板操作区那些按钮按下后**面板先收起来**（它们开的是别的窗）⇒ 结果回来时承载
        # 结果的那一块已经关了，得另有一个出口（见 `_report_off_panel`）。不接这个出口的话，
        # `ActionSpec.on_click` 一旦开始返回 `ActionResult`，那条路就是「说了没人听得见」。
        panel.set_off_panel_feedback(self._report_off_panel)
        # 「面板刚被打开」→ 框架事件（§8.2.1 第三类 topic，形态 ``app.<event>``）。
        # 面板只报告自己打开了，发不发、发给谁由这里决定：接的是**事件总线**，所以
        # 任何按需型组件只要声明 ``app.panel_opened`` 就会在每次开面板时被重跑一轮
        # （§8.3.1 通用订阅）。为什么需要它：按需型没有节拍，面板那一行画的是最后一次
        # ``tick()`` 的结论 ⇒ 屏幕一直亮着时掉线，那一行会一直停在旧结论上。
        # 载荷不带内容 —— 这一刻发生的事本身就是全部信息，没有「因为什么」可给。
        panel.set_open_notifier(lambda: events.publish(PANEL_OPENED_TOPIC))
        # 用户层显示位开关（决议 D4）：面板不读配置，只问「这一位用户关了吗」——
        # 绑定的是 Config 的方法而非值，用户改完当场生效，不需要重新装配。
        if self._config is not None:
            panel.set_ui_visibility(self._config.plugin_ui_visible)
            # 状态机优先级（用户覆盖声明顺序）：同款注入 —— 绑的是 Config 的方法而非值，
            # 用户在设置里调完 ▲▼ 当场生效，不需重新装配。
            panel.set_state_priorities(self._config.plugin_state_priorities)
        # 须早于 PluginManager：订阅回调与首屏绘制都要用 panel
        self.panel = panel
        return panel

    def _build_manager(
        self, registry, contributions, scheduler, hotkeys, events, panel, config,
        heartbeat,
    ) -> PluginManager:
        """运行期插件编排（E3 ②）：统一 enable / disable / uninstall。"""
        manager = PluginManager(
            registry, contributions, scheduler, hotkeys, events, panel, config,
            on_state_update=self.on_state_update,
            # 卸载收尾的时钟（§9.1 第 4 步）：有外部消费方的数据文件要等消费方收敛才删。
            # 用框架心跳而不是另起定时器/线程 —— 它已是唯一时钟源，且插件停摆后仍在跑。
            heartbeat=heartbeat,
        )
        # 卸载第 4b 步（A 类无状态对账，§9.3）：root 授权动作只此一处，实现在
        # `capabilities/helper_client.py`（**app 唯一的 root 授权入口**，helper
        # 安装/卸载与 A 类部署/回收都走它）。装配器是组合根，只负责接线。
        # 2026-09-14 收口：它已从 `plugins/route_guard/daemon.py` 上提，shell 不再
        # 感知具体插件包（此前的 TODO 已消除）。
        from barkit.capabilities.helper_client import reconcile_root_binaries

        def _reap(wanted) -> bool:
            ok, detail = reconcile_root_binaries(wanted)
            log.info("A 类对账（卸载后 wanted=%s）：%s", sorted(wanted), detail)
            return ok

        manager.on_reap_root_binaries = _reap
        manager.subscribe_all_active()
        return manager

    def _build_settings(self, contributions, manager) -> SettingsWindow:
        # parent=None：避免 QDialog 在首次 show() 时自动居中到（顶部锚定的）panel，
        # 否则 380px 高的设置窗会被推到屏幕上方触发「outside any known screen」警告。
        # 置顶/焦点由 WindowStaysOnTopHint + set_window_level + activateWindow 保证，
        # 不依赖 parent。
        settings = SettingsWindow(
            parent=None,
            registry=contributions,
            manager=manager,
            config=self._config,
            on_tray_base_changed=self._apply_tray_base,
            # 托盘阈值改档后当场重算角标队列（同款：程序内写配置不发 changed）
            on_badge_level_changed=self._push_badge,
            # 显示位开关改完当场重画三处（程序内写配置不发 changed，必须显式回调）
            on_display_changed=self._refresh_display_slots,
            # 状态机优先级调完同款：重算代表态（哪台机上标题栏 / 托盘）
            on_state_priority_changed=self._refresh_display_slots,
            # 快捷键页：内置动作名单 + 改键重挂 + 撞车查询（页面不自己去翻注册表）
            hotkey_host=HotkeyHost(
                actions=self._framework_actions(),
                rebind=self._rebind_hotkey,
                conflict_of=self._hotkey_conflict,
                available=bool(self._hotkeys is not None and self._hotkeys.available),
                feedback=self._feedback,
                # **惰性**取：热键管理器可能还没建好（装配顺序），而失败原因是「此刻」的
                # 事实 —— 页面每次重画都该问到最新的那一份，不是装配那一次的快照。
                failure_of=(lambda: None if self._hotkeys is None
                            else self._hotkeys.failure_of),
            ),
            # 变量页：列清单 / 现算一次 / 弹一次询问 / 加载问题 / 改完通知谁（页面不自己去翻变量池）
            variables_host=self._variables_view(),
        )
        # 状态机「当前生效值」的取数口（topic -> 当前报告）：绑的是 dict 的 get 而非快照，
        # 故状态变化后设置窗刷新即可拿到最新值，不必重新装配。
        settings.set_state_provider(self._state_reports.get)
        # 失联判据**直接问面板**（``_check_stale`` 是全仓唯一判失联的地方）；这里不另立
        # 一份阈值，否则又是一处会漂移的副本。面板可能还没建好 ⇒ 用惰性 lambda 兜住。
        settings.set_state_stale(
            lambda topic: bool(self.panel is not None and self.panel.is_stale(topic))
        )
        # 守护连接状态 / 版本变化 → 重算设置页「装置运维」警示（重装后红字及时退场）。
        # 会话是长驻单例、设置窗也是单例，连一次即可。
        if self._helper_session is not None:
            self._helper_session.stateChanged.connect(settings.refresh_helper_notices)
        # 运行期启用/禁用/卸载插件后，通知设置窗重建分页（无需重开）
        manager.on_plugins_changed = settings.rebuild_pages
        # 卸载收尾的**异步**结论（延迟删除那一段可能在几拍之后才跑完/放弃）：
        # 结果落回插件页的行内反馈槽 —— 用户刚点完「删除」，消息就该出现在同一处。
        manager.on_uninstall_settled = settings.notify_uninstall_settled
        return settings

    def _build_status_item(self, panel: StatusPanel, settings: SettingsWindow) -> None:
        """菜单栏图标 + 面板/设置 的信号接线。

        菜单栏图标用 macOS 原生 NSStatusItem（template 图自适应 + 真实坐标锚定弹层），
        非 macOS 或创建异常时回退 QSystemTrayIcon。
        """
        self.panel = panel
        panel.quitRequested.connect(self.app.quit)

        status_item = StatusItem(
            on_click=lambda: (
                status_item.redraw(),  # 安全网：点图标时按实时外观重绘，
                                       # 规避换肤监听偶发不触发
                panel.on_status_item_click(self._panel_anchor()),
            ),
            pixmap=tray_base_pixmap(None, 24, QColor(0, 0, 0), tray_render_dpr()),
        )
        self.status_item = status_item

        # 菜单栏点击关闭：面板显示时由「失活关闭」控制器另挂全局鼠标监视，点面板外
        # （含菜单栏）即关。仅原生状态项可用且关闭控制器存在时接线；全局监视需用户
        # 授权「辅助功能」，未授权时静默不触发，面板退回「点窗口/桌面关、菜单栏不关」
        # 的原行为（不更差）。
        if status_item.available and panel._dismiss is not None:
            try:
                panel._dismiss.set_status_item(status_item)
            except Exception as e:  # noqa: BLE001
                log.debug("面板关闭控制器未接状态项（仅影响菜单栏关闭）：%s", e)

        if status_item.available:
            status_item.set_tooltip(APP_NAME)
        else:
            tray = QSystemTrayIcon(self._make_tray_icon())
            self._tray = tray
            tray.setToolTip(APP_NAME)
            # 非 macOS 上左右键不可区分，统一点击弹面板。托盘图标矩形由各平台给出，
            # 认不出屏就退到「光标所在屏的可用区右上角」（`place_window` 那条退路）。
            tray.activated.connect(
                lambda reason: panel.on_status_item_click(
                    placement.Anchor.of_rect(tray.geometry())
                )
                if reason == QSystemTrayIcon.ActivationReason.Trigger
                else None
            )
            tray.show()

        # settings 的定位锚点**跟面板用同一个**：设置窗从面板里开出来（点它那一刻光标
        # 在面板上、不在图标上），再回读一次图标在哪既多余又可能读到坏值 —— 面板落在
        # 哪儿，它就落哪儿（贴图标那一档，两者本来就同源）。
        panel.settingsRequested.connect(
            lambda: settings.show_and_raise(panel.anchor)
        )
        # 「帮助」那枚已随底部栏重排删除（2026-09-17，用户裁定）：它做的是「打开设置窗并
        # 定位到『关于』页」—— 与「设置」按钮开的是**同一个窗**，只差一次点击，在 320px
        # 宽的面板上白占一格。「关于」页本身仍在设置窗里，需要时从侧栏进。
        # 详情页（§5.4）：点 L2 整行 → 框架按 DetailEntrySpec 打开该插件的任务流，
        # 与设置窗同样**跟面板用同一个锚**（从面板附近弹出，而非居中屏幕）。
        panel.detailRequested.connect(
            lambda pid: self._open_detail(pid, panel.anchor)
        )

    def _panel_anchor(self) -> "placement.Anchor | None":
        """点菜单栏图标那一刻的锚。

        **由点击点推**（点图标时光标必然在图标上）—— 这是唯一可靠的来源：一个图标在
        每块屏各有一枚原生按钮窗口，回读到哪一枚与「用户点了哪块屏」无关，系统还没把
        它摆好时那枚窗口停在屏幕外的初始位（换算出来是个假锚）。点击点取不到（热键 /
        命令这类没有点击的来路）才退到回读那个口。详见 ``shell/placement``。
        """
        item = self.status_item
        if item is None:
            return None
        return placement.anchor_at_click(
            QCursor.pos(), item.button_size()
        ) or placement.anchor_of_status_item(item)

    @staticmethod
    def _make_tray_icon(icon: IconName | None = None) -> QIcon:
        """菜单栏托盘图标：BarKit 条形码字形（设计源见 docs/design/barkit-glyph.svg）。

        ``icon`` 非空时按底图来源渲染该取值图标（P0③；仅非 macOS 回退路径用，
        macOS 由 ``StatusItem`` 自理）。尺寸/颜色与历史默认一致：22px / 深灰。
        """
        return QIcon(tray_base_pixmap(icon, 22, QColor(40, 40, 45), tray_render_dpr()))

    def _open_detail(self, pid: str, anchor) -> None:
        """点组件整行 → 打开详情页，并收起菜单栏面板。

        详情窗是独立原生窗（presentation=window 时居中、可缩放），面板留着只会
        挡在它后面、还占着菜单栏那块屏（2026-09-18 真机：弹窗出来面板还开着）。
        只在打开成功时才收起 —— 没声明详情页时不该把面板藏掉。
        """
        if self._detail_host.open(pid, anchor):
            self.panel.dismiss()
