"""原生窗口层级与几何：取窗（全仓唯一入口）、设层级、读写原生左上角、浮层置顶三件套与 nonactivating 开关。

``macos_window.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtCore import QPoint
from PySide6.QtGui import QGuiApplication

from .macos_base import NONACTIVATING_PANEL_MASK, OVERLAY_LEVEL, PANEL_LEVEL, _native_enabled, log
from .macos_base import objc as objc


def nswindow_of(widget):
    """取 ``widget`` 的原生 NSWindow —— **全仓唯一的取窗入口**（取不到返回 ``None``）。

    为什么必须收成一处：此前取窗口有**两条**路各走各的（见下），而只有其中一条在
    生产实例上真的通 —— 走不通的那条只知道「返回 None」，上游把 None 当「没有原生
    读数」放行，于是**落位只写进了 Qt 的账本**（2026-09-30 真机：面板原生 frame 在
    ``(0,0)``，Qt 账本与日志都是 ``(771,42)``，核对却一声不吭）。一个「取不到窗」的
    私有实现能把整条落位链变成空转，这种失效只能靠**收口 ＋ 失败出声**来防。

    两条路都试，先成先得：

    1. ``winId()`` 当 NSView 指针 → ``.window()``：与 :func:`set_window_level` 同源。
       真机实证它在本机生产实例上有效 —— 面板的窗口层确实是 101（只有这条路设得上去）。
    2. 遍历 ``NSApp.windows()`` 用 ``contentView`` 指针比对：浮层那一路，留作回退
       （某些窗口下第 1 条的指针包装会失效，那时只有这一条可用）。

    只在 :func:`_native_enabled` 为真时调用。
    """
    if not _native_enabled():
        return None
    try:
        win = objc.objc_object(c_void_p=widget.winId()).window()
        if win is not None:
            return win
    except Exception:  # noqa: BLE001 —— 这条路的指针包装在部分窗口上会失效，换下一条
        pass
    try:
        import objc
        from AppKit import NSApp

        my_view = int(widget.winId())
        for w in NSApp.windows():
            cv = w.contentView()
            if cv is not None and objc.pyobjc_id(cv) == my_view:
                return w
    except Exception:  # noqa: BLE001
        pass
    return None


def set_window_level(widget, level: int = PANEL_LEVEL) -> None:
    """把窗口的 NSWindow 层级设为 level（浮于全屏 Space 之上）。非原生环境 no-op。"""
    if not _native_enabled():
        return
    try:
        win = nswindow_of(widget)
        if win is not None:
            win.setLevel_(level)
    except Exception:
        # 原生调用失败不应影响主流程（最多层级不对，面板仍可用）
        pass


def native_top_left(widget) -> "QPoint | None":
    """窗口**原生体**此刻的真实左上角（Qt 全局坐标）；非原生 / 取不到窗给 ``None``。

    读的是 ``NSWindow.frame()``，这是**进程内**唯一能直接问「窗口现在在哪」的口：

    - ``QWidget.pos()`` / ``frameGeometry()`` / ``screen()`` 只是 Qt 自己的账本 ——
      两者分叉时它报的是**算出来的**位置，而用户看到的是另一个（2026-09-29 实测：
      窗口实际在屏幕角上，Qt 却报 ``(0, 503)``）；
    - 窗口服务器（``CGWindowListCopyWindowInfo``）**对本进程的窗口也给真值**：
      2026-09-30 用探针自证过 —— 窗口显示时它报的 bounds 与这里读到的 ``NSWindow.frame()``
      逐字相同，隐藏之后也不变成占位值。旧注释写的「给 ``(0,0,0,0)`` 占位值」**是错的**，
      按那条错的判据，当年正是「面板其实在 (0,0)、日志却写着落位 (771,42)」这件事
      查不出来的原因 —— 进程外读窗口是**唯一能直接看到用户所见**的取证口，别放弃它。
      注意它的 ``kCGWindowIsOnscreen`` 恒为 ``None``（判不了可见性），但位置可信。

    Cocoa 全局（原点主屏左下、y 向上）→ Qt 全局（原点主屏左上、y 向下）只有**一个**
    全局变换 ``qt_y = H_primary - cocoa_y``（H_primary 恒为主屏高），与屏幕排布无关
    —— 与 ``status_item.flip_constant`` 同一条。
    """
    if not _native_enabled():
        return None
    try:
        w = nswindow_of(widget)
        if w is None:
            return None
        frame = w.frame()
        primary = QGuiApplication.primaryScreen()
        h_primary = primary.geometry().height() if primary is not None else 0
        top = h_primary - (float(frame.origin.y) + float(frame.size.height))
        return QPoint(int(round(float(frame.origin.x))), int(round(top)))
    except Exception as e:  # noqa: BLE001 —— 读不到就当「没有原生读数」，不打断落位
        log.debug("读原生窗口位置失败：%s", e)
        return None


def native_top_left_on_screen(widget) -> "QPoint | None":
    """窗口服务器（``CGWindowListCopyWindowInfo``）里这扇窗的左上角（Qt 全局坐标）。

    与 :func:`native_top_left` 读的是**两个互不相干**的源：那一口读 ``NSWindow.frame()``
    —— AppKit 自己记的账；这一口问窗口服务器「你到底把这扇窗摆在哪」。两条会分叉，而且
    2026-10-14 定案的落位故障**就是**这种形态：同一扇窗（同一个 ``windowNumber``）
    ``NSWindow.frame()`` 报 ``(934,42)``，窗口服务器把它摆在 ``(0,0)``、并且此后
    ``setFrameOrigin_`` / ``setFrame_display_`` / ``setFrameTopLeftPoint_`` / Qt ``move``
    / ``orderFrontRegardless`` 五种手段**一个都改不动它**（真机逐条验过）。只回读前者
    **结构上不可能**发现这件事 —— 所以落位核对必须换到这一口。

    认窗用 ``windowNumber``（进程内唯一，与 ``kCGWindowNumber`` 同号）。坐标与 Qt 全局
    同系（原点主屏左上、y 向下）：``kCGWindowBounds`` 就是这个系，2026-10-14 拿健康实例
    逐窗比对过（②③ 两读数逐像素相同）。

    非原生 / 取不到窗 / **还没上屏**（窗口服务器里暂无这枚 ``windowNumber``）都给 ``None``
    —— 最后那种是「判据此刻不适用」，不是「确认位置错了」，调用方必须分开对待。
    """
    if not _native_enabled():
        return None
    try:
        import Quartz

        w = nswindow_of(widget)
        if w is None:
            return None
        num = int(w.windowNumber())
        if num <= 0:
            return None
        infos = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID
        )
        for info in infos or ():
            if int(info.get(Quartz.kCGWindowNumber, -1)) != num:
                continue
            bounds = info.get(Quartz.kCGWindowBounds) or {}
            return QPoint(int(round(float(bounds["X"]))), int(round(float(bounds["Y"]))))
    except Exception as e:  # noqa: BLE001 —— 读不到就当「没有独立读数」，不打断落位
        log.debug("读窗口服务器位置失败：%s", e)
    return None


def set_native_top_left(widget, x: int, y: int) -> bool:
    """绕过 Qt 把原生窗口挪到 ``(x, y)``（Qt 全局坐标）。返回是否真的设上了。

    **为什么需要它**：``QWidget.move()`` 在「Qt 自己记的坐标已经等于目标」时直接返回，
    而原生窗口可能并不在那儿（显示前 move 那次就是它造成的）—— 两边一旦分叉，
    之后在 Qt 侧 ``move()`` 多少次都修不回来。这一口走原生 ``setFrameOrigin_``，
    绕开那道短路；AppKit 事后会发 ``windowDidMove``，Qt 的账本跟着回到一致。
    """
    if not _native_enabled():
        return False
    try:
        from Foundation import NSPoint

        w = nswindow_of(widget)
        if w is None:
            return False
        frame = w.frame()
        primary = QGuiApplication.primaryScreen()
        h_primary = primary.geometry().height() if primary is not None else 0
        cocoa_y = h_primary - float(y) - float(frame.size.height)
        w.setFrameOrigin_(NSPoint(float(x), float(cocoa_y)))
        return True
    except Exception as e:  # noqa: BLE001
        log.debug("原生设置窗口位置失败：%s", e)
        return False


def make_floating_topmost(widget) -> None:
    """把窗口设成「切走也不消失、点得到也不抢焦点、且真的浮在最前」的浮层。四件事：

    1. 关 ``hidesOnDeactivate`` —— Qt 的 Tool 窗口（macOS 上是 NSPanel）默认「app 失活
       就隐藏」，而浮层的用法恰恰是「用户切到别的 App 去操作，浮层还留在屏幕上」，
       不关这一项就是那个「切走就没了」的 bug；
    2. 原生 ``orderFrontRegardless`` 抬到最前 —— 见下面那段「为什么不能是 Qt 的 ``raise_()``」；
    3. :func:`apply_nonactivating` —— 让浮层「点得到但不激活 app」；
    4. 落 :data:`OVERLAY_LEVEL` —— **必须排在最后**（理由见下面那段次序注释）。

    调用点也要配合：``Overlay`` 的三个显示出口把它排在 ``show()`` **之后** —— 次序不是
    随手写的，别把它挪到 ``show()`` 旁边去。

    **为什么不能是 Qt 的 ``raise_()``**（2026-10-15 真机实测，别改回去）：Qt 6 的
    ``QCocoaWindow::raise()`` 在窗口可见时干两件事 —— 先 ``orderFront:``，紧接着
    ``if (raiseProcess) [NSApp activateIgnoringOtherApps:YES]``（源码里那句由
    ``QT_MAC_SET_RAISE_PROCESS`` 控制，默认开）。**第二件事就是把 BarKit 拉成前台 app**
    —— 用户正在别的 app 里打字的那个输入框当场失焦，这正是浮层最不该干的事，
    也等于把第 3 项自己顶掉。本仓**全程不调 ``raise_()``**，只做它那第一件事。

    **不在这里调 ``activateWindow``** —— 同上，激活 app 就把第 3 项顶掉。
    非原生 / offscreen 环境整体 no-op。
    """
    if not _native_enabled():
        return
    try:
        w = nswindow_of(widget)
        if w is not None:
            w.setHidesOnDeactivate_(False)
            # 抬前用 orderFrontRegardless —— AppKit 文档点名的「**抬前不激活**」原语
            # （orderFront: / makeKeyAndOrderFront: 会激活 app）。真机已探明的系统边界：
            # regular 策略下「窗口可见」本身就会把 app 拉成 active，连它都拦不住
            # （探针 N/P/Q 臂，见 Overlay 类文档）；它守的是 accessory 常态这一档。
            w.orderFrontRegardless()
    except Exception as e:  # noqa: BLE001
        log.debug("浮层置顶保险未应用（回退 Qt 标志）：%s", e)
    # 次序：① 先打 mask（只改 styleMask，不碰层级）；② **层级压在最后一步**。
    # 曾经把层级跟 hidesOnDeactivate 一起设、排在 mask 之前，结果是那个层级值被后续
    # 动作反复改写（真机表现为「浮层被目标窗口压住」）—— 详见 OVERLAY_LEVEL 的注释。
    apply_nonactivating(widget)
    _apply_overlay_level(widget)


def _apply_overlay_level(widget) -> None:
    """把浮层的窗口层级落到 :data:`OVERLAY_LEVEL`（原生不可用 / 取不到窗时静默跳过）。"""
    try:
        w = nswindow_of(widget)
        if w is not None:
            w.setLevel_(OVERLAY_LEVEL)
    except Exception as e:  # noqa: BLE001
        log.debug("浮层层级未应用（回退 Qt 标志）：%s", e)


def apply_nonactivating(widget) -> bool:
    """让窗口「**点得到，但不激活 app**」——不打断用户正在进行的鼠标 / 键盘操作。

    这不是「水印」：水印是 ``Qt.WindowTransparentForInput``，事件直接穿透，浮层根本
    点不着。要的是「事件照常送到浮层里的按钮，但 BarKit 不被激活，用户原来在哪个
    输入框打字就还在哪儿打」。macOS 原生专门有这一档：``NSWindowStyleMaskNonactivatingPanel``
    —— 输入法候选窗、Spotlight 面板就是它：点得动，焦点不换人。

    两个硬前提，缺一个就白做（本函数在前提不满足时会告警并返回 False，不静默装成功）：

    1. **窗口必须是 NSPanel**（macOS 上对应 ``Qt.Tool``）—— 这个 mask 对普通 NSWindow
       **无效**，这也是浮层不能用 ``Qt.Window`` 的原因；
    2. **不许调 ``activateWindow``** —— 否则等于自己把 mask 顶掉。

    键盘那半不用额外做什么：app 不被激活 ⇒ 它不是 active app ⇒ 系统不会把 key window
    （键盘焦点）交给它，是这条链路的自然结果。

    返回是否真的设上了（非 macOS / offscreen / 不是 NSPanel / 调用失败 = False）。
    """
    if not _native_enabled():
        return False
    try:
        import AppKit

        w = nswindow_of(widget)
        if w is None:
            return False
        cls = str(w.className())
        if "NSPanel" not in cls:
            # 静默无效是这个 mask 最坏的一面：以为设了，其实没设，表现为「点了按钮，
            # 正在打字的窗口失焦」，在真机上很难联想到这一行。故这里必须吵出来。
            log.warning("浮层「点得到但不激活 app」未生效：窗口类是 %s（须为 NSPanel，即 Qt.Tool）", cls)
            return False
        mask = int(getattr(AppKit, "NSWindowStyleMaskNonactivatingPanel", NONACTIVATING_PANEL_MASK))
        w.setStyleMask_(int(w.styleMask()) | mask)
        w.setBecomesKeyOnlyIfNeeded_(True)
        w.setHidesOnDeactivate_(False)
        return True
    except Exception as e:  # noqa: BLE001
        log.debug("浮层 nonactivating 未应用（回退 Qt 标志）：%s", e)
        return False
