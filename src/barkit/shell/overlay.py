"""shell/overlay：通用悬浮层（框架能力，2026-09-22 从 autotype 倒计时浮层收归）。

只管「一个悬浮窗作为窗口」的那半件事：无边框置顶、显示时不激活 app、自由拖动、跟随
光标所在屏、显示时长（可暂停 / 固定）、以及统一的退场口径。**内容由业务填进
``content_layout``，框架不认识任何插件** —— 浮层里出现什么字、按下去干什么，都不是这里的事。

**退场只有一个出口** ``dismiss(reason)``：``reason`` 取本模块的 ``DISMISS_*`` 常量。
「取消」是业务语义、框架只负责把它带出去（浮层内一律不弹模态框 —— 零模态门禁扫整个
``shell/``）。业务想区分「用户取消了」还是「到点自己没了」，读那个 reason 即可。

刻意不做的一件（是「今天做不得」，不是漏了）：

- **毛玻璃**。真模糊只能借 ``NSVisualEffectView``（Qt 不暴露 vibrancy），本仓**从未真机验过**，
  方案封存在 ``内部记录（liquid-glass）``。要接它，第一步是让
  :meth:`paintEvent` 别填实心底 —— 半透明压在实心 ``surface`` 上等于同色混合（``Card`` 的
  玻璃模式 2026-09-21 因此整体删除，实测只差 3/255）。

**浮层有三个出口，区别只在「谁定位置」**（2026-09-23 定案；后两条源自用户诉求
「提示统一右上角、由框架统一控制」与「时钟要右上角、且与其他浮窗和谐相处」）：

- :meth:`Overlay.show_and_run` —— **业务自己定**：水平居中、垂直落在可用高度 30% 处。
  给「要盯着看、能拖能点」的交互浮层用（今天是一键输入那块倒计时卡）：它需要注意力，
  居中偏上比缩在屏角合适。
- :meth:`Overlay.show_toast` —— **框架统一**：可用区右上角，多条**纵向叠放**并自动重排。
  给「看一眼就走」的提示用（今天是热键反馈那一行字）。调用方**不许**自己 ``move()`` ——
  位置一旦回到调用方手里，就又变回各摆各的（此前三个编辑屏的页脚正是由此长歪的）。
- :meth:`Overlay.show_docked` —— **框架统一（常驻）**：可用区右上角**钉住**，不随提示重排
  而移动。给「常驻在那儿、要一直看得见」的浮层用（今天是翻页时钟）。与提示共用右上角这
  一列：**常驻钉最上、提示从它下方开始排** —— 常驻的不许被一条 2 秒的提示顶开位置，提示
  也不许压在常驻的上头。

提示栈只认走 :meth:`show_toast` 进来的那些：交互浮层**不进栈**（它不该被提示顶开，
也不该占提示的位置）；常驻浮层同样不进栈，但它**占的那段竖向空间**归框架记账，提示要让开。

**「不打断用户操作」是哪一档，说清楚**（这条最容易被做歪，2026-09-22 补）：

浮层要的是「**点得到，但不激活 app**」—— 浮层里的按钮照常被点、照常有反应，但 BarKit
不会被激活，用户原来在哪个输入框打字就还在哪儿打，鼠标键盘都不被打断。它**不是水印**：
水印是 ``Qt.WindowTransparentForInput``，事件直接穿透、浮层根本点不着。

原生能力在 :func:`barkit.shell.macos_window.apply_nonactivating`（``NSWindowStyleMaskNonactivatingPanel``，
输入法候选窗与 Spotlight 面板用的就是它）。该 mask **只对 NSPanel 有效**，所以浮层必须是
``Qt.Tool``（macOS 上 Tool 窗口即 NSPanel）而不是普通 ``Qt.Window`` —— 这是本文件唯一一处
「为了一个原生能力而选窗口类型」的地方，改之前先读那段注释。

**「不激活」有三道口**（2026-10-15 钉死，缺一不可）：① 构造期 ``WA_ShowWithoutActivating``
—— 浮层**成不了 key window**，键盘焦点拿不走；② ``apply_nonactivating`` —— 管住**点击浮层**
那一下（NSPanel 的 native mask，点得动但不激活）；③ **全程不调 ``raise_()``**（Qt 会在它
内部 ``[NSApp activateIgnoringOtherApps:YES]``，照抢），抬最前改由
:func:`barkit.shell.macos_window.make_floating_topmost` 里的原生 ``orderFrontRegardless``。
已探明的系统边界：app 处于 **Regular** 策略时，「窗口可见」本身就会把 app 拉成 active
（mask ＋ orderFrontRegardless 都拦不住，探针 N/P/Q 臂钉死）—— 常态 accessory 下整条链路
干净，Regular 只在模态任务流开着时出现。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QPoint, QRectF, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QAbstractButton, QVBoxLayout, QWidget

from barkit.shell import placement
from barkit.shell.design import metrics
from barkit.shell.macos_window import make_floating_topmost
from barkit.shell.ui_kit import _theme_tokens, qt_alive

#: 退场原因。**「取消」不在这里** —— 取消是业务语义（用户按了那个按钮），
#: 框架只认「浮层没了」这件事的四种来由，业务要区分就在自己的回调里再判。
DISMISS_TIMEOUT = "timeout"      # 显示时长到点
DISMISS_CLOSED = "closed"        # 被外部关闭（如 app 退出、closeEvent）
DISMISS_CANCELLED = "cancelled"  # 业务主动中止（一键输入按了「取消 / 停止」）
DISMISS_COMPLETED = "completed"  # 业务正常走完

#: 内容区间距：沿用收归前浮层手写的 10。``metrics.GAP_*`` 没有这一档，不为它单独开档位 ——
#: 改这个数就是改观感，得走真机目视，不该藏在「统一标尺」的名义下发生。
CONTENT_SPACING = 10

#: 拖动判定：按下的点落在这些控件（或其子部件）上就**不拖** —— 它们要自己吃掉这次按下。
#: 判据是「谁会自己处理鼠标按下」，不是逐个点名业务控件（那样每加一种控件就得回来改这里）。
#: 今天只有按钮一种；将来浮层里放了输入框 / 下拉，在这里补一行类型即可，别去子类重写
#: ``mousePressEvent``。
_DRAG_BLOCKING_TYPES = (QAbstractButton,)

#: 走 :meth:`Overlay.show_toast` 进来、此刻还在屏上的提示，**按进栈先后**排。
#: 它同时是**持有引用的那份**（提示是顶层窗、没有父控件替它续命）：退场即出栈，
#: 不留残引用 —— Toast 悄悄不出现是最难自查的一类，所以这一份不许挪到调用方去。
_TOASTS: "list[Overlay]" = []


def live_toasts() -> "list[Overlay]":
    """此刻还在屏上的提示（按叠放次序）。给需要清理 / 核对落位的那一侧读。"""
    return list(_TOASTS)


#: 走 :meth:`Overlay.show_docked` 进来、此刻钉在右上角的**常驻**浮层，**按垂直次序**排。
#: 与 :data:`_TOASTS` 分开：提示是「看一眼就走」，常驻是「钉在那儿不走」。两者共用右上角
#: 这一列却不叠住 —— 常驻在栏首、提示从其下方排（见 :func:`_docked_reserved`）。
_DOCKED: "list[Overlay]" = []


def live_docked() -> "list[Overlay]":
    """此刻钉在右上角的常驻浮层（按垂直次序）。给需要清理 / 核对落位的那一侧读。"""
    return list(_DOCKED)


def overlay_alive(overlay: "Overlay | None") -> bool:
    """这枚浮层的 **C++ 本体还在不在**（``None`` ＝ 没有）。

    退场走 ``dismiss`` ⇒ ``deleteLater``：事件循环跑完删除后，C++ 对象没了、**Python 壳
    还活着** —— 对死壳调任何方法（``isVisible`` / ``dismiss``…）都抛 ``RuntimeError``。
    长期持有浮层引用的一方（如翻页钟控制器捏着常驻那枚）在**用之前**必须先过这一判：
    死壳当作没有，让它照常走「重建」的路。

    判活的真源是 ``ui_kit.qt_alive``（``shiboken6.isValid``，PySide6 没有 ``QPointer`` 时
    的标准替代）—— 这里只是那一个判据的**浮层语义包装**（``None`` ＝ 没有），
    因为 ``ui_kit`` 反向 import 不了本模块（它被本模块 import）。
    """
    return qt_alive(overlay)


def _docked_reserved(screen=None) -> int:
    """常驻浮层在右上角占掉的竖向空间（块高 ＋ 间距），提示栈要从它下面开始排。

    只算**同一块屏**上的常驻：多屏时提示落在光标屏，不该被另一块屏上的常驻白白顶下去。
    """
    return sum(
        d.height() + metrics.OVERLAY_STACK_GAP
        for d in _DOCKED
        if screen is None or d._toast_screen is screen
    )


def _hits_interactive(widget: QWidget, pos) -> bool:
    """按下的点是否落在「自己要处理点击」的控件上（含其子部件）。"""
    child: QWidget | None = widget.childAt(pos)
    while child is not None:
        if isinstance(child, _DRAG_BLOCKING_TYPES):
            return True
        child = child.parentWidget()
        if child is widget:
            return False
    return False


class Overlay(QWidget):
    """通用悬浮层。业务继承它、往 ``content_layout`` 里填内容。

    :param size: 固定尺寸 ``(w, h)``；``None`` 则交给布局自适应（内容长的浮层用它）。
    :param duration_ms: 显示时长；``None`` = 不自动消失（由业务决定什么时候走）。
    :param draggable: 是否可拖动（纯提示类浮层可以关掉，免得手一碰就移位）。
    :param opacity: 整窗不透明度 0.0–1.0。**走 Qt 的 ``windowOpacity``**（落到 macOS 的
        ``NSWindow.alphaValue``，由窗口服务器合成，Qt 侧零重绘）—— 绝不用
        ``QGraphicsOpacityEffect``，那玩意儿每帧把整窗重画进离屏位图（实测 +0.56ms/帧），
        而且不透明度存在 Qt 侧，一旦没人拉回来整窗就画成透明。判据见
        ``ui_kit.play_entrance`` 的注释（2026-09-21 重写时量出来的）。
    """

    #: 退场信号，带 :data:`DISMISS_*` 之一。业务接它做收尾（停自己的时钟、写日志…）。
    dismissed = Signal(str)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        size: tuple[int, int] | None = None,
        duration_ms: int | None = None,
        draggable: bool = True,
        opacity: float = 1.0,
    ) -> None:
        super().__init__(
            parent,
            # Tool（**不是**普通 Window）：macOS 上 Tool 窗口是 NSPanel，而「点得到但不
            # 激活 app」那一档（NSWindowStyleMaskNonactivatingPanel）**只对 NSPanel 有效**。
            # 用普通 Window 的话，点一下浮层就会把用户正在打字的 app 顶掉 —— 浮层也就废了
            # （一键输入的场景里，用户此刻的焦点就该在目标输入框上）。
            # 代价：Tool 默认「app 失活就隐藏」，用 WA_MacAlwaysShowToolWindow 抵掉，
            # native 侧再补一次 hidesOnDeactivate=False（见 macos_window.make_floating_topmost）。
            # 不用 Qt.Popup：Popup 会 grab 鼠标并在点外部时自动关闭，与「可拖动 / 可固定」
            # 直接冲突（浮层要的是留在那儿，不是一点别处就没）。
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        # Tool 窗口默认随 app 失活一起隐藏；浮层的用法恰恰是「用户切走，浮层还在」。
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        # 「浮层**不许成为 key window**」—— 与上面那条（失活就隐藏）是一对，两个方向都得堵。
        # 少了它：Qt 在 `setVisible` 里对可成 key 的窗一律 `makeKeyAndOrderFront:`，
        # regular 策略下裸 Tool 窗只 `show()` 就在 ~56ms 内既激活 app 又成为 key window
        # （真机实测 2026-10-15，前台是别的 app）。打上这一条后：任何策略下浮层都**成不了
        # key**（Qt 的 shouldRefuseKeyWindowAndFirstResponder 会拒），键盘焦点拿不走。
        # ⚠️ 已知边界（同日探针 N/P/Q 臂钉死）：app 处于 **Regular** 策略时「窗口变得可见」
        # 这件事本身就会把 app 拉成 active —— nonactivating mask ＋ 原生
        # `orderFrontRegardless` 都拦不住，无窗空跑则干净 ⇒ 这是 macOS 对 regular app 的
        # 系统行为，本仓不与之对抗；**常态 accessory（LSUIElement）下整条链路完全干净**。
        # Regular 只在模态任务流开着时出现（见 docs/architecture/UI交互分层规范-导读.md
        # 「弹出时显示 Dock 图标」）。
        # 必须在**构造期**设：Qt 在 `setVisible` 里读它，`show()` 之后再设已经晚了。
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setObjectName("overlay")

        self._duration_ms = duration_ms
        self._left_ms = duration_ms
        self._draggable = draggable
        self._paused = False
        self._pinned = False
        self._done = False

        self._dragging = False
        self._drag_pos = None

        #: 提示栈里钉住的那块屏（首落位时定，见 :meth:`show_toast`）：重排**不许换屏**，
        #: 否则光标一移到另一块屏，屏上那几条提示就集体搬家。
        self._toast_screen: "object | None" = None

        if size is not None:
            self.setFixedSize(*size)
        if opacity < 1.0:
            self.setWindowOpacity(max(0.0, min(1.0, opacity)))

        self.content_layout = QVBoxLayout(self)
        self.content_layout.setContentsMargins(*metrics.PAD_OVERLAY)
        self.content_layout.setSpacing(CONTENT_SPACING)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        # 连接只做一次：到点就是「超时退场」这一件事，暂停 / 恢复只动启停，不重连。
        self._timer.timeout.connect(lambda: self.dismiss(DISMISS_TIMEOUT))

    # ---------- 显示时长：暂停 / 继续 / 固定 ----------

    def _start_timer(self) -> None:
        """起显示时长（只在真显示了之后才起，构造完不显示不该开始倒计时）。"""
        if self._duration_ms is None or self._pinned:
            return
        self._timer.start(self._left_ms if self._left_ms else self._duration_ms)

    @property
    def paused(self) -> bool:
        """是否处在「暂停」中（倒计时停住、剩余时间留着）。"""
        return self._paused

    @property
    def pinned(self) -> bool:
        """是否被「固定」（从此不再自动消失）。"""
        return self._pinned

    @property
    def remaining_ms(self) -> int:
        """离自动消失还剩多少毫秒。没设显示时长 / 已固定 / 已退场 = 0。

        暂停时读的是**留住的那一份**（不是「时钟还在走」的假象）—— 断言「resume 是按剩余
        继续、不是从头再来」靠的就是它。
        """
        if self._pinned or self._done:
            return 0
        if self._paused:
            return int(self._left_ms or 0)
        return int(self._timer.remainingTime()) if self._timer.isActive() else 0

    def pause(self) -> bool:
        """暂停自动消失的倒计时，留住剩余时间。**返回暂停后是否真的停住了**。"""
        if self._pinned or not self._timer.isActive():
            return False
        self._left_ms = self._timer.remainingTime()
        self._timer.stop()
        self._paused = True
        return True

    def resume(self) -> bool:
        """接着 :meth:`pause` 的剩余时间继续。**返回恢复后是否真的在跑**。"""
        if self._pinned or not self._paused:
            return False
        self._paused = False
        self._timer.start(self._left_ms if self._left_ms else self._duration_ms)
        return self._timer.isActive()

    def pin(self) -> bool:
        """固定：不再自动消失（倒计时作废，不可逆 —— 「固定」之后再自己没了是骗人）。"""
        if self._pinned:
            return False
        self._timer.stop()
        self._pinned = True
        self._paused = False
        self._left_ms = None
        return True

    # ---------- 退场 ----------

    def dismiss(self, reason: str) -> None:
        """关掉浮层并把 ``reason`` 带出去。重复调用只认第一次（退场是一次性的）。"""
        if self._done:
            return
        self._done = True
        self._timer.stop()
        self.hide()
        self.deleteLater()
        self.dismissed.emit(reason)

    # ---------- 显示 ----------

    def show_and_run(self) -> None:
        """显示 → 浮层保险（抬起 ＋ 置顶 ＋ 不激活 app）→ 核对落位 → 起显示时长。

        **全程不调 ``activateWindow()``，也不调 ``raise_()``**（2026-10-15 更正）：
        两者都会激活 app、把用户正准备输入的那個窗口的焦点抢走。本文件此前写着
        「``raise_()`` 只抬 z-order、不激活」—— **那是错的**：Qt 6 的
        ``QCocoaWindow::raise()`` 在窗口可见时先 ``orderFront:``、紧接着
        ``[NSApp activateIgnoringOtherApps:YES]``，真机表现为「浮层一显示、目标输入框就失焦」。
        抬到最前那一件事改由 :func:`make_floating_topmost` 里的原生 ``orderFrontRegardless`` 做。

        ``show()`` **自己那一下**由构造期打的 ``WA_ShowWithoutActivating`` 兜住 key 那一半
        （Qt 在 ``setVisible`` 里本会对可成 key 的窗调 ``makeKeyAndOrderFront:``；有了这一条，
        浮层任何策略下都成不了 key）。app 被拉成 active 那一半在 regular 策略下拦不住
        （macOS 系统行为，见类文档），常态 accessory 下整条链路干净。

        次序（2026-09-22 修正 ＋ 2026-10-15 更正）：``show()`` 在前、原生保险紧随其后。
        层级必须压在 ``show()`` **之后** —— ``show`` 会把 ``WindowStaysOnTopHint`` 重新落
        一遍，原生层级排在它之后才站得住，否则浮层会掉成普通层窗口、被目标窗口压住。
        判据见 ``macos_window.OVERLAY_LEVEL`` 的注释与 ``内部记录（浮层置顶）``。

        落位排在**最后**（2026-09-29）：位置是唯一一件「别的动作可能顺手改掉」的事，
        排在末尾才没人能覆盖它；随后 :func:`placement.verify_placement` 回读原生窗口核对。
        """
        self.show()
        make_floating_topmost(self)
        pt, _ = placement.place_window(self, placement.Intent.CENTER_TOP)
        placement.verify_placement(self, pt)
        self._start_timer()

    # ---------- 提示：框架定位置（右上角 ＋ 叠放 ＋ 重排）----------

    def show_toast(self) -> None:
        """提示类浮层的**唯一出口**：右上角、多条纵向叠放、有退场就重排。

        与 :meth:`show_and_run` 的次序（显示 → 原生保险 → 落位 → 核对 → 起时长）
        完全相同，只有落位那一档换成 ``placement.Intent.TOP_RIGHT`` —— 次序是
        2026-09-22 修过的（原生层级必须压在 ``show()`` 之后），两处不许各自演进；
        落位排在最后是 2026-09-29 定的（位置排末尾才没人覆盖得了）。
        **也不调 ``raise_()``**：它会激活 app，理由见 :meth:`show_and_run`。
        """
        _TOASTS.append(self)
        self.dismissed.connect(lambda _reason, ov=self: _drop_toast(ov))
        self._toast_screen = (
            QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        )
        self.show()
        make_floating_topmost(self)
        pt = self._move_into_stack()
        placement.verify_placement(self, pt)
        self._start_timer()

    def _move_into_stack(self) -> "QPoint":
        """按自己在栈里的序位落位（常驻区 ＋ 前面那些提示占掉的竖向空间都要跳过去）。

        **在 show() 之后调**：叠放读的是各条自己的高度，而提示的高度由内容给 ——
        显示之前量到的是过期值（``PromptOverlay`` 就没有固定高度）。
        """
        above = _docked_reserved(self._toast_screen) + sum(
            t.height() + metrics.OVERLAY_STACK_GAP for t in _TOASTS[: _TOASTS.index(self)]
        )
        pt, _ = placement.place_window(
            self, placement.Intent.TOP_RIGHT, screen=self._toast_screen, above=above
        )
        return pt

    # ---------- 常驻：框架定位置（右上角 ＋ 钉住 ＋ 与提示共用一列）----------

    def show_docked(self) -> None:
        """常驻类浮层的**唯一出口**：右上角钉住、不随提示重排而移动。

        与 :meth:`show_and_run` / :meth:`show_toast` 的次序（显示 → 原生保险 → 落位 →
        核对 → 起时长）完全相同，只有落位那一档相同而**不入提示栈**，并把自己
        登记进常驻区 —— 提示栈据此跳过它占的竖向空间（两者共用右上角这一列却不叠住）。
        **也不调 ``raise_()``**：它会激活 app，理由见 :meth:`show_and_run`。

        常驻浮层的尺寸往往随内容变（如翻页钟切档），尺寸变了调 :meth:`redock` 重新钉位。
        """
        _DOCKED.append(self)
        self.dismissed.connect(lambda _reason, ov=self: _drop_docked(ov))
        self._toast_screen = (
            QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        )
        self.show()
        make_floating_topmost(self)
        pt = self._move_into_dock()
        placement.verify_placement(self, pt)
        self._start_timer()

    def _move_into_dock(self) -> "QPoint":
        """钉到右上角自己那一行（前面还有常驻就往下让）。"""
        above = sum(
            d.height() + metrics.OVERLAY_STACK_GAP for d in _DOCKED[: _DOCKED.index(self)]
        )
        pt, _ = placement.place_window(
            self, placement.Intent.TOP_RIGHT, screen=self._toast_screen, above=above
        )
        return pt

    def redock(self) -> None:
        """常驻浮层**尺寸变了**之后重新钉位：把这一列（常驻 ＋ 提示）整列重排一遍。

        **不重选屏** —— 与提示重排同一条纪律：重排不许换屏，否则光标一动整列就搬家。
        """
        if self not in _DOCKED:
            return
        for docked in _DOCKED:
            docked._move_into_dock()
        for toast in _TOASTS:
            toast._move_into_stack()

    # ---------- 绘制：圆角卡片 ----------

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        t = _theme_tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), metrics.RADIUS_LG, metrics.RADIUS_LG)
        painter.setBrush(QColor(t["surface"]))
        painter.setPen(QPen(QColor(t["border"]), 1))
        painter.drawPath(path)
        super().paintEvent(_event)

    # ---------- 自由拖动 ----------

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if (
            self._draggable
            and event.button() == Qt.MouseButton.LeftButton
            and not _hits_interactive(self, event.position().toPoint())
        ):
            self._dragging = True
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            # globalPos() 在 Qt 6 已废弃：取 globalPosition()（QPointF）再转回 QPoint。
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._dragging and self._drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if self._dragging:
            self._dragging = False
            self._drag_pos = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        super().mouseReleaseEvent(event)

    # ---------- 关闭 ----------

    def closeEvent(self, event) -> None:  # type: ignore[override]
        # 被外部关掉（app 退出等）与业务取消是两回事，reason 分开带出去。
        self.dismiss(DISMISS_CLOSED)
        super().closeEvent(event)


def _drop_toast(overlay: "Overlay") -> None:
    """提示退场后出栈，并把剩下的**重排**（否则中间那条走了会留一个洞）。

    模块级而不是方法：栈是**跨实例**的那份状态（谁走了要让别人知道），挂在单个浮层
    上就会变成「自己收拾自己」，剩下的那些永远不会补位。
    """
    if overlay in _TOASTS:
        _TOASTS.remove(overlay)
    for toast in _TOASTS:
        toast._move_into_stack()


def _drop_docked(overlay: "Overlay") -> None:
    """常驻退场后出列，并把后面的常驻与**整列提示**重排（它让出的空间要补回来）。

    与 :func:`_drop_toast` 同源：这一列是跨实例的状态，缺了它上面的常驻一走、
    下面的提示就永远停在半空。
    """
    if overlay in _DOCKED:
        _DOCKED.remove(overlay)
    for docked in _DOCKED:
        docked._move_into_dock()
    for toast in _TOASTS:
        toast._move_into_stack()
