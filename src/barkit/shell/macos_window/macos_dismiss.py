"""面板显示期间的原生行为控制器（激活 app ／ 失活即关闭 ／ 不活跃时补投鼠标位置）与其工厂。

``macos_window.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations


from .macos_base import NSApplication, NSApplicationDidResignActiveNotification, NSEvent, NSLeftMouseDownMask, NSNotificationCenter, NSObject, NSRightMouseDownMask, _HAS_OBJC, _native_enabled, app_is_active, log
from .macos_cursor import cursor_inside_widget, cursor_on_panel_or_icon, cursor_pos
from .macos_hover import create_hover_relay
from .macos_verdict import VERDICT_OK, VERDICT_STUCK, panel_mouse_dead_verdict, should_close_on_deactivate


#: 面板显示后「屏蔽失活关闭」的时长（ms）：显示 / 激活切换本身会带来一次瞬时失活，此时
#: 关掉面板等于刚开就关。屏蔽期一过就按 :func:`app_is_active` 复核一次（见控制器文档串）。
_GATE_MS = 200
#: 复核「重新激活有没有生效」的间隔（ms）：激活请求经一轮事件循环才落地，别当场就读结果。
_RECHECK_MS = 200


if _HAS_OBJC:

    class PanelDismissController(NSObject):
        """「显示时激活 app + 失活即关闭」控制器（实现点外部关闭，无需辅助功能授权）。

        - start()：激活 app（让面板成为前台 key 窗口），并订阅
          NSApplicationDidResignActiveNotification。打开后 :data:`_GATE_MS` 内屏蔽关闭，
          避开显示/激活切换的瞬时失活竞态；闸门到期时**核一次激活结果**。
        - stop()：取消订阅、停两枚定时器、撤掉鼠标补投的那两条监视。
        - 本类还持着 :class:`macos_hover.PanelHoverRelay`（由本类起停，它自己不管生命周期）：
          「面板可见期间」要盯的四件事 —— 激活、失活关闭、点外部关闭、不活跃时补投鼠标 ——
          起停时机完全相同，分成两个持有点就会出现「两个生命周期各说各话」。
        - appDidResignActive_：app 失活（用户点了别的 App / 桌面 / 菜单栏）且面板仍
          可见且已过屏蔽期 → 关闭面板。点击面板内不触发（app 仍激活）。

        **闸门到期为什么要复核激活结果**（2026-09-24 补）：闸门期内的失活通知是有意吞掉的，
        而 ``appDidResignActive_`` 只在**跃变**时来 —— 若这 200 ms 里 app 真的失活、此后再
        没回来，就再没有任何通知会到，面板会停在「可见但本进程不是活跃 app」上。那个状态在
        macOS 上等于整个窗口收不到鼠标事件（见 :func:`app_is_active`）⇒ 用户看到的就是
        「面板悬停不亮、tooltip 也不出」，而且**不会自愈**。激活请求本身也从未被核对过
        （``activateIgnoringOtherApps_`` 是「请求」，不是「保证」，且这条链上原先静默吃过异常）
        ⇒ 这里补一条 :func:`panel_mouse_dead_verdict` 的复核链：不是活跃 app 就重新激活一次，
        再复核一次，仍不行就报一条能直接对上现象的日志。

        注意：本类用标准 alloc().init() 创建，panel 等引用作为 Python 属性挂到实例上
        （PyObjC 支持），避免在 __init__ 里调 NSObject.init 的 super 兼容坑。
        """

        def start(self) -> None:
            if not _native_enabled():
                return
            if self._registered:
                return
            self._registered = True
            self.panel._can_close_on_deactivate = False
            try:
                self._activate()
                center = NSNotificationCenter.defaultCenter()
                center.addObserver_selector_name_object_(
                    self,
                    "appDidResignActive:",
                    NSApplicationDidResignActiveNotification,
                    None,
                )
            except Exception as e:  # noqa: BLE001
                # 原先这里静默 pass：激活失败与订阅失败都无声，而前者的后果（面板没有
                # 鼠标反馈）要等用户报上来才知道。至少要留下一条。
                log.warning("面板「激活 + 订阅失活」未完成：%s", e)
            # 另挂全局鼠标监视，兜底「点菜单栏关闭」（失活关闭对 accessory app 漏这块）。
            self._register_global_click()
            # 不活跃时的悬停反馈：激活成不成不掌握在我们手里，故另起一路自己补投鼠标位置
            # （见 ``macos_hover`` 模块 docstring）。与上面那条全局监视同起同停。
            if self._hover is not None:
                self._hover.start()
            self._ensure_timers()
            self._gate.start(_GATE_MS)

        def _ensure_timers(self) -> None:
            """两枚随面板生命周期、**只造一次**的单发定时器。

            必须挂父对象（``self.panel``）：无父的 QTimer 会被当局部对象回收、回调永不触发
            （与 ``QPropertyAnimation`` 那个静默坑同源）。也不每次 start() 重造 —— 挂在面板上
            的旧定时器不会回收，重开一次面板就积两枚。
            """
            from PySide6.QtCore import QTimer

            if self._gate is None:
                self._gate = QTimer(self.panel)
                self._gate.setSingleShot(True)
                self._gate.timeout.connect(self._on_gate_expired)
            if self._recheck is None:
                self._recheck = QTimer(self.panel)
                self._recheck.setSingleShot(True)
                self._recheck.timeout.connect(self._on_recheck)

        def _activate(self) -> None:
            """请求激活 app（顺带把面板顶成 key 窗口）。失败只记日志，不改控制流。"""
            try:
                NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
                self.panel.activateWindow()
            except Exception as e:  # noqa: BLE001
                log.warning("请求激活 app 失败：%s", e)

        def _on_gate_expired(self) -> None:
            """闸门到期：放开「失活即关闭」，并核一次「到底激活成功没有」。"""
            self.panel._can_close_on_deactivate = True
            self._check_activation(retried=False)

        def _on_recheck(self) -> None:
            """重新激活之后再核一次：还不是活跃 app 就只能留痕（这一支不对上了）。"""
            self._check_activation(retried=True)

        def _check_activation(self, retried: bool) -> None:
            """按 :func:`panel_mouse_dead_verdict` 的结论执行：激活就停 / 重激活 / 留痕。

            自备定时器（``_ensure_timers`` 幂等）—— 这个方法不只在 start() 那条链上被调，
            别让「复核要用的那枚表」依赖某个更早的调用点先跑过。
            """
            self._ensure_timers()
            verdict = panel_mouse_dead_verdict(
                bool(self.panel.isVisible()), app_is_active(), retried
            )
            if verdict == VERDICT_OK:
                return
            if verdict == VERDICT_STUCK:
                log.warning(
                    "面板可见但不是活跃 app（已重新激活过一次仍未生效）：macOS 上 Qt 只在"
                    "「app 是活跃 app」时分发鼠标事件，所以这个面板会没有悬停样式、也不出"
                    "tooltip —— 点一下面板即可恢复。反复出现请带上本条日志。"
                )
                return
            # 这一支是「看见并已动手修」⇒ 记 info。**只有复核仍失败（STUCK）才是用户此刻
            # 正看着的故障，那一支才配 warning** —— 否则「激活比 200ms 慢一拍」的正常机器
            # 每次开面板都会刷一条警告（刷警告这件事用户零容忍，见 2026-09-24 的翻页动画）。
            log.info(
                "面板可见但不是活跃 app：重新激活一次再复核（不修的话整个面板没有鼠标反馈）"
            )
            self._activate()
            self._recheck.start(_RECHECK_MS)

        def set_status_item(self, status_item) -> None:
            """注入状态项（供全局点击监视判定「点在图标上」时排除，交给定时器 toggle）。"""
            self._status_item = status_item

        def _register_global_click(self) -> None:
            """显示时另挂全局鼠标监视，兜底「点菜单栏关闭」。

            失活关闭（``appDidResignActive``）对 accessory app 漏了「点菜单栏」——菜单栏
            点击不会让本 app 失活，故关不掉。全局监视器覆盖系统一切鼠标按下（含菜单栏
            图标 / 控制中心 / 时钟等），点「面板 ∪ 图标」之外即关，正好补上。两块「不关区」
            与失活关闭那条路读的是**同一个判据**（:func:`cursor_on_panel_or_icon`）。

            代价：现代 macOS 下全局监视需用户授权「辅助功能」。未授权时
            ``addGlobalMonitor…`` 静默不触发，面板退回「点窗口 / 桌面关、菜单栏不关」原行为，
            不更差。本地监视器（``addLocalMonitor``）无需授权，但只收得到**本 app** 事件，
            而菜单栏点击属于别的 app，收不到 —— 故只能用全局这一支。监视只在面板可见时
            判定关闭，隐藏后点击只做廉价空转，无副作用。
            """
            if not _native_enabled() or self._global_obs is not None:
                return
            try:
                self._global_obs = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                    NSLeftMouseDownMask | NSRightMouseDownMask,
                    lambda ev: self._on_global_click(ev),
                )
            except Exception as e:  # noqa: BLE001
                # 未授权 / 调用失败：静默退回失活关闭（原行为），不影响面板显示
                log.debug("面板全局点击监视未启用（点菜单栏关闭不可用）：%s", e)
                self._global_obs = None

        def _icon_rect_qt(self):
            """菜单栏图标此刻的 Qt 矩形（取不到给 None，那一格不参与判定）。"""
            si = self._status_item
            if si is None or not getattr(si, "available", False):
                return None
            try:
                return si.button_rect_qt()
            except Exception:  # noqa: BLE001 —— 量不到就当没有这一格，不影响面板本身
                return None

        @staticmethod
        def _event_point(event):
            """事件自带的那点（只为日志）；读不到给 None。"""
            try:
                p = event.locationInWindow()
                return (round(float(p[0]), 1), round(float(p[1]), 1))
            except Exception:  # noqa: BLE001
                return None

        def _on_global_click(self, event) -> None:
            """全局鼠标按下：落点在「面板 ∪ 菜单栏图标」之外 ⇒ 关面板。

            判据是 :func:`cursor_on_panel_or_icon`（鼠标位置这个**单一**事实）。事件自带的
            坐标只进日志 —— 它是事发后唯一还能说明「那一下到底点在哪儿」的东西，而这块屏
            上只有它和 Qt 两套读数，出分歧时必须以日志为准，不能靠推断（2026-09-27 的两轮
            误判就是拿一个同类结局当成了用户那条路）。
            """
            if not self.panel.isVisible():
                return
            cursor = cursor_pos()
            if cursor is None:
                # 读不到鼠标位置 ⇒ **不关**：判据缺一半时宁可留着面板（用户点一下就能关），
                # 也不要因为「读不出来」把用户正在操作的面板关掉。
                return
            panel_rect = self.panel.frameGeometry()
            icon_rect = self._icon_rect_qt()
            if cursor_on_panel_or_icon(cursor, panel_rect, icon_rect):
                return  # 落在面板 / 图标上：这不是「点别处」
            log.info(
                "面板关闭［点到面板外］：鼠标=(%d,%d) 面板=%s 图标=%s 事件点=%s",
                cursor.x(), cursor.y(), panel_rect, icon_rect, self._event_point(event),
            )
            self.panel.hide()

        def stop(self) -> None:
            if not self._registered:
                return
            self._registered = False
            try:
                NSNotificationCenter.defaultCenter().removeObserver_(self)
            except Exception:
                pass
            if self._global_obs is not None:
                try:
                    NSEvent.removeMonitor_(self._global_obs)
                except Exception:  # noqa: BLE001
                    pass
                self._global_obs = None
            if self._hover is not None:
                self._hover.stop()
            for timer in (self._gate, self._recheck):
                if timer is not None:
                    timer.stop()
            self.panel._can_close_on_deactivate = False

        def appDidResignActive_(self, note) -> None:
            """本进程失去前台：只有「鼠标不在面板上」时才关掉面板。

            2026-09-27 用户报「程序启动后第一次点标题栏里的图标，整个面板消失」，追下来
            这条是**唯一不需要用户点某个特定控件就可能发生**的关闭路径（其余各条都要先
            点菜单栏图标 / 点面板外 / 点动作栏）。而真机会话里 ``app_is_active()`` 反复
            报 False，那种会话里每一次前台跃变都不是用户点的 —— 判据因此改成看**鼠标
            位置**这个与激活状态无关的事实，理由与取舍见
            :func:`should_close_on_deactivate`。
            """
            armed = bool(getattr(self.panel, "_can_close_on_deactivate", False))
            inside = cursor_inside_widget(self.panel)
            if should_close_on_deactivate(self.panel.isVisible(), armed, inside):
                log.info("面板关闭［失去前台］：鼠标在面板外（视作用户点了别处）")
                self.panel.hide()
            elif armed and self.panel.isVisible():
                log.info("失去前台，但面板留下：鼠标还在面板内")

else:

    class PanelDismissController:
        def __init__(self, panel):
            self.panel = panel

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def set_status_item(self, status_item) -> None:
            pass


def create_dismiss_controller(panel) -> "PanelDismissController | None":
    """创建「失活即关闭」控制器；非原生环境返回 None（面板跳过原生关闭逻辑）。"""
    if not _HAS_OBJC:
        return None
    ctrl = PanelDismissController.alloc().init()
    ctrl.panel = panel
    ctrl._registered = False
    ctrl._gate = None
    ctrl._recheck = None
    ctrl._status_item = None
    ctrl._global_obs = None
    ctrl._hover = create_hover_relay(panel)
    return ctrl
