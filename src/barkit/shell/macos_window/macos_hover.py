"""「面板可见、但本进程不是活跃 app」时的鼠标补投：AppKit 侧只说「鼠标动了」，Qt 侧负责补投回去。

为什么需要它（判据出处见 :func:`macos_base.app_is_active`）：本进程不是活跃 app 时，Qt cocoa
插件把鼠标移动 / 进入 / 离开 / 光标更新四类事件全部丢掉，于是悬停样式、光标形状、tooltip
**三者一起**失效，而点按照常 —— 用户看到的就是「面板点得动、但鼠标经过毫无反应」。

上一轮走的是「重新激活 app」那条路（:func:`macos_verdict.panel_mouse_dead_verdict` 与
``PanelDismissController`` 的复核链），那条保留、各管一段；本模块走的是**另一条**：
不指望激活成功，自己把「鼠标此刻在哪」补投给 Qt。这么定的依据是真机实测，见
``docs/log/2026-09-28-面板鼠标反馈诊断-现场与机制.md``：同一场景连试六次激活请求全部落空，
且「点到面板外」之后再开面板就必好 —— 说明激活能不能成不掌握在我们手里。

两条监视（本地 + 全局）都挂，因为「事件到底送到本进程还是前台 app」这一环在真机上没有定论：
本地监视收「送到本 app 的」（Qt 的 tracking area 用 ``NSTrackingActiveAlways``，回调会送到
本 app、只是被 Qt 丢掉），全局监视收「送到别的 app 的」。两者互斥（一个事件只走其中一条），
不会重复补投 —— 所以不必先知道答案。

补投方式：向面板的 ``QWindow`` 送一条合成 ``MouseMove``，位置取 :func:`macos_cursor.cursor_pos`
这个**唯一**可信的鼠标位置源（事件自带坐标的坐标系是一条没验过的注释假设，见
:func:`macos_cursor.cursor_on_panel_or_icon`）。Qt 收到后自己会做命中测试、派发
Enter / HoverEnter / HoverMove、并把控件自己的光标应用下去 —— 这三件在同一段处理里，
所以只要事件进得去，三个症状一起好（离屏探针逐条量过：Enter ＋ HoverEnter ＋ HoverMove ＋
重绘都到，``QWindow`` 的光标也从箭头变成手型）。
"""
from __future__ import annotations


from .macos_base import (
    NSEvent,
    NSLeftMouseDraggedMask,
    NSMouseEnteredMask,
    NSMouseExitedMask,
    NSMouseMovedMask,
    NSObject,
    NSRightMouseDraggedMask,
    _HAS_OBJC,
    _native_enabled,
    app_is_active,
    log,
)
from .macos_cursor import cursor_inside_widget, cursor_pos
from .macos_verdict import should_reinject_motion


#: 要盯的鼠标事件掩码：移动与拖动是主体，进 / 出也一起收 —— 否则「光标从面板上离开」那一下
#: 没人知道，最后悬停的那一行会一直亮着、手型也会粘在别处。
_MOTION_MASK = (
    NSMouseMovedMask
    | NSLeftMouseDraggedMask
    | NSRightMouseDraggedMask
    | NSMouseEnteredMask
    | NSMouseExitedMask
) if _HAS_OBJC else 0


if _HAS_OBJC:

    class PanelHoverRelay(NSObject):
        """面板可见期间把鼠标位置补投给 Qt（只在「本进程不是活跃 app」时真投）。

        生命周期由 ``PanelDismissController`` 持（与「失活即关闭」同起同停），本类不自己起
        时钟、不订阅通知 —— 只在两条鼠标监视的回调里读一次鼠标位置。理由：面板可见期间要
        盯的四件事（激活、失活关闭、点外部关闭、鼠标补投）起停时机完全相同，分散到两处
        就是同一个生命周期有两个持有点。
        """

        def start(self) -> None:
            """挂上两条鼠标监视。已挂过就直接返回（每次显示都会调到 ``start``）。"""
            if not _native_enabled():
                return
            if self._local is None:
                try:
                    self._local = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                        _MOTION_MASK, self._on_local_event
                    )
                except Exception as e:  # noqa: BLE001
                    log.debug("本地鼠标监视未启用（不活跃时靠全局那条兜）：%s", e)
                    self._local = None
            if self._global is None:
                try:
                    self._global = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                        _MOTION_MASK, lambda ev: self._reinject()
                    )
                except Exception as e:  # noqa: BLE001
                    log.debug("全局鼠标监视未启用（不活跃时靠本地那条兜）：%s", e)
                    self._global = None

        def _on_local_event(self, event):
            """本地监视必须把事件**原样交回**：我们只是旁听，不拦不吞。

            返回 ``None`` 会被 AppKit 当成「吃掉这条事件」—— 那会让面板连点按都收不到，
            比现在更糟。
            """
            self._reinject()
            return event

        def _reinject(self) -> None:
            """读一次鼠标位置并补投；只在「面板可见 ＋ 本进程不活跃」时真投。

            两条离屏实测出来的事实决定了这里的写法（2026-10-04）：

            1. **落在窗口外的合成移动，Qt 直接不理** —— 所以「指针离开面板」不能靠再投一条
               移动把 Leave 带出来，只能自己把 ``Leave`` 送给最后压下过的那个控件；
            2. **悬停规则可能挂在祖先上**（面板行就是这么写的）⇒ ``Leave`` 要给整条祖先链，
               只给最深那个控件的话，行的悬停底色会一直亮着。
            """
            try:
                panel = self.panel
                if not should_reinject_motion(bool(panel.isVisible()), app_is_active()):
                    return
                cursor = cursor_pos()
                if cursor is None:
                    return
                if cursor_inside_widget(panel):
                    self._hovered = self._widget_under(cursor)
                    self._inside = True
                    self._send_motion(cursor)
                    return
                if not self._inside:
                    # 光标在面板外、而且本来就在外面：没有要撤的悬停，不投。
                    # （否则光标在屏幕别处移动时，我们每一动都在给面板送一条事件。）
                    return
                self._inside = False
                hovered, self._hovered = self._hovered, None
                if hovered is not None:
                    self._send_leave(hovered)
            except Exception as e:  # noqa: BLE001
                # 这条链在 AppKit 的鼠标回调里跑，异常抛出去是崩溃级后果 ⇒ 一律吞掉并留痕；
                # 只报一次，免得鼠标一动就刷一条。
                if not self._warned:
                    self._warned = True
                    log.warning("不活跃时补投鼠标位置失败（这一路悬停反馈不再生效）：%s", e)

        def _widget_under(self, cursor):
            """鼠标此刻压在哪个控件上（判不出来给 None，当「没有要撤的悬停」）。

            用 ``childAt`` 而不是自己挨个比矩形：它和 Qt 派发时用的是同一套命中规则
            （跳过 ``WA_TransparentForMouseEvents`` 的那些），两处不会分叉。
            """
            try:
                return self.panel.childAt(self.panel.mapFromGlobal(cursor))
            except Exception:  # noqa: BLE001
                return None

        def _send_motion(self, cursor) -> None:
            """把「鼠标此刻在 cursor」这件事送进 Qt（命中测试与派发归 Qt）。"""
            from PySide6.QtCore import QEvent, QPointF, Qt
            from PySide6.QtGui import QMouseEvent
            from PySide6.QtWidgets import QApplication

            handle = self.panel.windowHandle()
            if handle is None:
                return
            QApplication.sendEvent(
                handle,
                QMouseEvent(
                    QEvent.Type.MouseMove,
                    QPointF(self.panel.mapFromGlobal(cursor)),
                    QPointF(cursor),
                    Qt.MouseButton.NoButton,
                    Qt.MouseButton.NoButton,
                    Qt.KeyboardModifier.NoModifier,
                ),
            )

        def _send_leave(self, widget) -> None:
            """把「指针已经离开」告诉 Qt：整条祖先链各送一条 ``Leave``。

            为什么要连祖先一起送：悬停规则通常挂在**行**上、而鼠标往往压在行里面的某个
            ``QLabel`` 上（离屏实测：只给最深那个控件送 ``Leave``，行的悬停底色照样亮着）。
            链一直走到顶层窗口为止 —— 这与补投 Enter 时 Qt 自己走的链是对称的。

            只送 ``Leave``、不送 ``HoverLeave``：离屏量过，``HoverLeave`` 单独送不撤底色。
            """
            from PySide6.QtCore import QEvent
            from PySide6.QtWidgets import QApplication

            node = widget
            while node is not None:
                QApplication.sendEvent(node, QEvent(QEvent.Type.Leave))
                node = node.parentWidget()

        def stop(self) -> None:
            """撤掉两条监视（每次面板隐藏都会调到）。"""
            for attr in ("_local", "_global"):
                obs = getattr(self, attr)
                if obs is None:
                    continue
                try:
                    NSEvent.removeMonitor_(obs)
                except Exception:  # noqa: BLE001
                    pass
                setattr(self, attr, None)
            self._inside = False
            self._hovered = None

else:

    class PanelHoverRelay:
        """非原生环境的空壳（与 ``PanelDismissController`` 同形）：什么都不做。"""

        def __init__(self, panel):
            self.panel = panel

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass


def create_hover_relay(panel) -> "PanelHoverRelay | None":
    """创建「不活跃时补投鼠标位置」的中继；非原生环境返回 None（面板跳过这一路）。"""
    if not _HAS_OBJC:
        return None
    relay = PanelHoverRelay.alloc().init()
    relay.panel = panel
    relay._local = None
    relay._global = None
    relay._inside = False
    relay._hovered = None
    relay._warned = False
    return relay
