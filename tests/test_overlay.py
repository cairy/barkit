"""tests/test_overlay：框架悬浮层（``shell/overlay.py``）的行为用例。

守的是**窗口那一半**：显示时长到点退场、暂停留住剩余、固定不再自动消失、退场 reason 的
四种来由不混、拖动生效且按在按钮上不拖、透明度走 ``windowOpacity``。

**这里的等待一律走 ``qtbot.wait``**（它会转事件循环）—— 浮层的倒计时是 ``QTimer``，
不转事件循环永远等不到超时，而 ``time.sleep`` 会让用例变成「只是睡了一觉」的假绿。
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QCursor, QGuiApplication, QMouseEvent
from PySide6.QtWidgets import QPushButton

from barkit.shell.design import metrics
from barkit.shell.overlay import (
    DISMISS_CLOSED,
    DISMISS_TIMEOUT,
    Overlay,
    _hits_interactive,
    live_docked,
    live_toasts,
)

_DURATION_MS = 400


def _mouse(kind: QEvent.Type, local: QPointF, globe: QPointF) -> QMouseEvent:
    """造一个左键鼠标事件（Qt 6 的构造子要 local + global 两个点）。"""
    return QMouseEvent(
        kind,
        local,
        globe,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


def _press(local=(5, 5), globe=(5, 5)) -> QMouseEvent:
    return _mouse(QEvent.Type.MouseButtonPress, QPointF(*local), QPointF(*globe))


def _move(local=(105, 85), globe=(105, 85)) -> QMouseEvent:
    return _mouse(QEvent.Type.MouseMove, QPointF(*local), QPointF(*globe))


def _release(local=(105, 85), globe=(105, 85)) -> QMouseEvent:
    return _mouse(QEvent.Type.MouseButtonRelease, QPointF(*local), QPointF(*globe))


def _spawn(qtbot, **kwargs) -> tuple[Overlay, list[str]]:
    """建一个浮层并挂上 reason 收集器。返回 ``(浮层, 收到的 reason 列表)``。"""
    ov = Overlay(size=(200, 120), **kwargs)
    qtbot.addWidget(ov)
    seen: list[str] = []
    ov.dismissed.connect(seen.append)
    return ov, seen


# ---------- 显示时长 ----------


def test_duration_dispatches_timeout_when_it_runs_out(qtbot):
    """设了显示时长 ⇒ 到点自己退场，reason = timeout。"""
    ov, seen = _spawn(qtbot, duration_ms=40)
    ov.show_and_run()
    qtbot.wait(200)
    assert seen == [DISMISS_TIMEOUT]


def test_without_duration_the_overlay_never_leaves_on_its_own(qtbot):
    """没设显示时长 ⇒ 不许自己消失（什么时候走由业务说了算）。"""
    ov, seen = _spawn(qtbot)
    ov.show_and_run()
    qtbot.wait(200)
    assert seen == []
    assert ov.remaining_ms == 0


def test_pause_holds_the_clock_and_resume_continues_from_the_remainder(qtbot):
    """暂停要把剩余留住，恢复**接着剩余走**（不是从头再来）。

    判据落在 ``remaining_ms`` 上：暂停后读到的必须是「剩下的那一截」，且明显短于原时长。
    """
    ov, seen = _spawn(qtbot, duration_ms=_DURATION_MS)
    ov.show_and_run()
    qtbot.wait(60)
    assert ov.pause() is True
    assert ov.paused is True
    left = ov.remaining_ms
    assert 0 < left < _DURATION_MS, f"暂停应留住剩余，实得 {left}"

    qtbot.wait(_DURATION_MS)          # 已经超过原定时长：暂停中的不该退场
    assert seen == []
    assert ov.paused is True

    assert ov.resume() is True
    assert ov.paused is False
    qtbot.wait(left + 250)
    assert seen == [DISMISS_TIMEOUT]


def test_pin_cancels_auto_dismiss_for_good(qtbot):
    """固定 = 从此不再自动消失。固定之后「暂停 / 继续」都没有意义，一律拒绝。"""
    ov, seen = _spawn(qtbot, duration_ms=60)
    ov.show_and_run()
    assert ov.pin() is True
    assert ov.pinned is True
    qtbot.wait(200)
    assert seen == []
    assert ov.remaining_ms == 0
    assert ov.pause() is False
    assert ov.resume() is False
    assert ov.pin() is False      # 已经固定 ⇒ 再说一次不是「又做了一次」


# ---------- 退场口径 ----------


def test_dismiss_reports_the_reason_exactly_once(qtbot):
    """退场是一次性的：reason 只认第一次，重复 dismiss 不重复发信号。"""
    ov, seen = _spawn(qtbot)
    ov.show()
    ov.dismiss("cancelled")
    ov.dismiss(DISMISS_TIMEOUT)
    assert seen == ["cancelled"]
    assert ov.isHidden() is True


def test_being_closed_from_outside_reports_closed(qtbot):
    """被外部关掉（app 退出等）与业务取消是两种来由，reason 分开带出去。"""
    ov, seen = _spawn(qtbot)
    ov.show()
    ov.close()
    assert seen == [DISMISS_CLOSED]


# ---------- 拖动 ----------


def test_drag_moves_the_window(qtbot):
    """按在空白处拖 ⇒ 窗口跟着走（位移 = 鼠标的位移）。"""
    ov, _seen = _spawn(qtbot)
    ov.show()
    ov.move(0, 0)
    ov.mousePressEvent(_press())
    ov.mouseMoveEvent(_move())
    assert ov.pos() == QPoint(100, 80)
    ov.mouseReleaseEvent(_release())
    assert ov.pos() == QPoint(100, 80)


def test_press_on_a_button_does_not_start_dragging(qtbot):
    """按在按钮上不许拖 —— 那次按下是给按钮的，不是给窗口的。

    判据是「谁会自己处理鼠标按下」（``_hits_interactive``），不是逐个点名业务控件：
    今天浮层里只有一枚按钮，将来放输入框 / 下拉时同一条判定照样成立。
    """
    ov, _seen = _spawn(qtbot)
    btn = QPushButton("取消")
    ov.content_layout.addWidget(btn)
    ov.show()
    # 定死起点：``show()`` 之后窗口落在哪儿由平台决定（offscreen 会把新窗居中），
    # 下面那条 ``ov.pos() == QPoint(0, 0)`` 才成立 —— 与 ``test_drag_moves_the_window``
    # 同一口径，不把「平台把窗放哪」混进断言。
    ov.move(0, 0)
    center = btn.geometry().center()
    center_f = QPointF(center)

    assert _hits_interactive(ov, center) is True
    assert _hits_interactive(ov, QPoint(2, 2)) is False

    ov.mousePressEvent(_press(local=(center_f.x(), center_f.y()),
                              globe=(center_f.x(), center_f.y())))
    assert ov._dragging is False
    ov.mouseMoveEvent(_move())
    assert ov.pos() == QPoint(0, 0)


def test_draggable_false_ignores_the_drag(qtbot):
    """关掉拖动（纯提示类浮层）⇒ 按哪儿都不移位。"""
    ov, _seen = _spawn(qtbot, draggable=False)
    ov.show()
    ov.move(0, 0)
    ov.mousePressEvent(_press())
    ov.mouseMoveEvent(_move())
    assert ov.pos() == QPoint(0, 0)


# ---------- 透明度 ----------


def test_opacity_goes_through_window_opacity_not_a_graphics_effect(qtbot):
    """透明度走 ``windowOpacity``（窗口服务器合成，Qt 侧零重绘），**不挂** QGraphicsEffect。

    ``QGraphicsOpacityEffect`` 每帧把整窗重画进离屏位图（实测 +0.56ms/帧），且不透明度
    存在 Qt 侧 —— 一旦没人拉回来，整窗就画成透明（2026-09-21 设置窗「打开一片空」）。
    """
    ov, _seen = _spawn(qtbot, opacity=0.6)
    assert ov.windowOpacity() == pytest.approx(0.6)
    assert ov.graphicsEffect() is None


def test_full_opacity_leaves_the_window_alone(qtbot):
    """不透明度 1.0 是默认 ⇒ 不动它（免得为一个无变化的值白跑一次窗口服务器往返）。"""
    ov, _seen = _spawn(qtbot)
    assert ov.windowOpacity() == pytest.approx(1.0)


# ---------- 不打断用户操作（点得到，但不激活 app） ----------
#
# 这一档最容易做歪成「水印」：WindowTransparentForInput 让事件直接穿透，浮层就点不着了。
# 要的是「按钮照常被点，但 BarKit 不被激活，用户原来在哪个输入框打字还在哪儿打」。


def test_overlay_is_a_tool_window_so_the_nonactivating_mask_can_apply(qtbot):
    """浮层必须是 ``Qt.Tool``（macOS 上即 NSPanel）。

    「点得到但不激活 app」用的是 ``NSWindowStyleMaskNonactivatingPanel``，而**这个 mask
    只对 NSPanel 有效** —— 换成普通 ``Qt.Window``，那一行原生设置会静默无效，表现为
    「点了浮层按钮，正在打字的窗口失焦」：真机上极难联想到窗口类型。这条钉的就是前提。
    """
    ov, _seen = _spawn(qtbot)
    assert ov.windowType() == Qt.WindowType.Tool

    # 判窗口类型必须用 windowType() 判等，**不能按位与**：Qt::Tool = Popup | Dialog，
    # 它的位里是含 Popup 位的 —— 按位与会把 Tool 误判成 Popup（实测 windowFlags() 读出
    # Window|...|Dialog|Popup|Tool）。下面那条「不是 Popup」同理。


def test_overlay_takes_clicks_it_is_not_a_watermark(qtbot):
    """浮层点得动 ⇒ 不许挂 ``WindowTransparentForInput`` / ``WA_TransparentForMouseEvents``。

    那两样是「水印」的做法：事件直接穿透，浮层成了看得见摸不着的摆设。
    """
    ov, _seen = _spawn(qtbot)
    assert not (ov.windowFlags() & Qt.WindowType.WindowTransparentForInput)
    assert ov.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents) is False


def test_overlay_does_not_grab_the_mouse_like_a_popup(qtbot):
    """不许是 ``Qt.Popup``：Popup 会 grab 鼠标并在点外部时自动关闭。

    浮层的语义是「留在那儿，可被拖动 / 可被固定」，跟「一点别处就没」直接冲突 ——
    Popup 那套是给右键菜单用的，不是给浮层用的。
    """
    ov, _seen = _spawn(qtbot)
    assert ov.windowType() != Qt.WindowType.Popup


@pytest.mark.parametrize("exit_name", ["show_and_run", "show_toast", "show_docked"])
def test_showing_the_overlay_never_activates_the_app(qtbot, monkeypatch, exit_name):
    """三个出口都**不许**调 ``activateWindow()``，也**不许**调 ``raise_()``。

    两者都会激活 app、把用户正准备输入的那個窗口的焦点抢走。这是「不打断」里唯一能在
    非真机环境守住的一条：激活动作在 Qt 侧，数得到次数。（原生那半
    ``apply_nonactivating`` 只能真机验，见 ``docs/current-task`` §4。）

    **``raise_()`` 那一条是 2026-10-15 补的**：此前本仓断言「``raise_()`` 只抬 z-order、
    不激活」—— 那是错的。Qt 6 的 ``QCocoaWindow::raise()`` 在窗口可见时会紧接着调
    ``[NSApp activateIgnoringOtherApps:YES]``；真机实测（Accessory 策略、前台是别的 app）
    显示浮层后 **42ms** 内前台就变成 BarKit、浮层成了 key window，用户在那个 app 里的
    输入框当场失焦。抬到最前改由 ``make_floating_topmost`` 的原生 ``orderFront:`` 做。
    """
    calls: list[str] = []
    monkeypatch.setattr(Overlay, "activateWindow", lambda _self: calls.append("activateWindow"))
    monkeypatch.setattr(Overlay, "raise_", lambda _self: calls.append("raise_"))
    ov, _seen = _spawn(qtbot)
    getattr(ov, exit_name)()
    try:
        assert calls == [], f"{exit_name} 调了这些激活动作：{calls}"
    finally:
        ov.dismiss(DISMISS_CLOSED)


def test_tool_window_is_marked_to_survive_app_deactivation(qtbot):
    """Tool 窗口默认「app 失活就隐藏」——浮层的用法恰恰是「用户切走，浮层还在」。

    ``WA_MacAlwaysShowToolWindow`` 就是抵这一项（native 侧再补一次
    ``hidesOnDeactivate=False``，那里只能真机验）。
    """
    ov, _seen = _spawn(qtbot)
    assert ov.testAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow) is True


def test_overlay_is_marked_to_show_without_activating(qtbot):
    """``WA_ShowWithoutActivating`` —— 「不激活」的**第一道口**，管住 Qt 侧的 ``show()``。

    ``apply_nonactivating``（native mask）只管「**点浮层**那一下」；**显示那一下**归 Qt，
    而 app 处在 **Regular** 策略时 Qt 会把 ``show()`` 翻成 ``makeKeyAndOrderFront:``
    ⇒ 前台换成 BarKit、用户正在别的 app 里打字的输入框失焦。真机实测（2026-10-15，
    前置＝前台是别的 app）：Regular 下裸 Tool 窗**只** ``show()`` 就在 56ms 内抢到激活
    并成为 key window；打上这个属性后 Regular / Accessory 两种策略都不抢。
    Regular 不是假想档位：模态任务流弹出时框架会把策略切到 Regular 再切回 Accessory。
    """
    ov, _seen = _spawn(qtbot)
    assert ov.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating) is True


def test_show_without_activating_is_already_set_when_show_runs(qtbot, monkeypatch):
    """判据打在**时刻**上：这个属性必须在 ``show()`` 执行的那一刻就已置上。

    Qt 是在 ``setVisible`` 里读它的 —— ``show()`` 之后再设已经晚了（窗已上屏、激活已抢走）。
    故这里不用「属性为真」当判据，而是在 ``show`` 内部回读。

    控制组（把这条翻红）：删掉构造期那行 ``setAttribute(WA_ShowWithoutActivating)`` ⇒
    ``seen`` 收到 False、断言失败。
    """
    from PySide6.QtWidgets import QWidget

    real_show = QWidget.show
    seen: list[bool] = []

    def spy(self):
        if isinstance(self, Overlay):
            seen.append(bool(self.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)))
        return real_show(self)

    monkeypatch.setattr(QWidget, "show", spy)
    ov, _seen = _spawn(qtbot, duration_ms=None)
    ov.show_and_run()

    assert seen == [True], f"show() 那一刻该属性还没置上：{seen}"


def test_native_nonactivating_is_a_noop_without_a_real_mac(qtbot):
    """原生「不激活 app」开关在 offscreen 下必须是 no-op，且**承认**自己没设上。

    判据：``_native_enabled()`` 把 offscreen 一并排除（无真实 NSWindow），故返回 False。
    返回 bool 而不是静默装成功，是为了让真机上一眼能看出「到底设没设」。
    """
    from barkit.shell.macos_window import apply_nonactivating, make_floating_topmost

    ov, _seen = _spawn(qtbot)
    ov.show()
    assert apply_nonactivating(ov) is False
    make_floating_topmost(ov)  # 不抛即可


# ---------- 置顶（原生层级） ----------
#
# 2026-09-22 真机事故：浮层弹出时确实在最前（``kCGWindowLayer=3``），但 BarKit 一旦失活
# （用户点进目标 app 聚焦输入框 —— 一键输入的必经一步）就被清成 ``layer=0``，也就是变回
# 普通层窗口，于是被目标窗口压住。根因是层级值本身站不住（见 ``macos_window.OVERLAY_LEVEL``
# 那段注释），而**设层级的时机**也在其中一半：Qt 会把 ``WindowStaysOnTopHint`` 重新落一遍。
# 离屏只能守住这两条的**形状**（取值与次序），真实层级只能真机验（见任务板 §4）。


def test_native_insurance_runs_after_showing_the_window(qtbot, monkeypatch):
    """原生保险必须排在 ``show()`` **之后** —— ``show`` 会把层级重新落一遍。

    2026-10-15 起这条轨道上不再有 ``raise_()``（它会激活 app，见上一条用例），判据随之
    改成「``show`` 在前、原生保险在后」。

    控制组（把这条翻红）：把 ``show_and_run`` 里那两行换回收归时的次序
    （``make_floating_topmost`` 在前、``show`` 在后）⇒ 断言立刻失败。
    """
    order: list[str] = []
    monkeypatch.setattr(
        "barkit.shell.overlay.make_floating_topmost", lambda _w: order.append("native")
    )
    monkeypatch.setattr(Overlay, "show", lambda _self: order.append("show"))

    ov, _seen = _spawn(qtbot, duration_ms=None)
    ov.show_and_run()

    assert order == ["show", "native"], f"次序不对：{order}"


def test_overlay_module_never_calls_qt_raise():
    """源码门禁：``overlay.py`` 里不许出现调用形状的 ``.raise_(``。

    用例能守行为，这条守「有人下次顺手加回来」—— ``raise_()`` 的激活副作用在离屏下
    完全看不出来，只会在真机上表现成「浮层一显示、目标输入框就失焦」。
    """
    from pathlib import Path

    import barkit.shell.overlay as overlay_mod

    src = Path(overlay_mod.__file__).read_text(encoding="utf-8")
    assert ".raise_(" not in src, "浮层不许调 Qt 的 raise_()：它会 activateIgnoringOtherApps"


def test_overlay_level_sits_above_the_clobbered_floating_level(qtbot):
    """浮层层级取 25（``NSStatusWindowLevel``），**不许退回 ``NSFloatingWindowLevel``(3)**。

    3 这个值在这套环境里会被反复改写（真机观测到被顶回 Qt 自己的 8、也被清成 0），
    20 / 25 / 101 / 201 逐档验过没人动，故取其中最低的一档 25。分层不变式：
    浮层(25) < 面板(101) < 二级窗口(201) —— 顺序错了浮层就会盖住面板或设置窗。
    """
    from barkit.shell import macos_window as mw

    assert mw.OVERLAY_LEVEL == 25, f"浮层层级被改成了 {mw.OVERLAY_LEVEL}"
    assert mw.OVERLAY_LEVEL > 3, "3 就是那个会被清成 0 的取值"
    assert mw.OVERLAY_LEVEL < mw.PANEL_LEVEL < mw.SETTINGS_LEVEL


def test_the_package_reexports_every_name_it_promises():
    """包 ``__init__`` 再导出的每个名字，在**非 objc 兜底**里也必须绑得上。

    2026-10-03 拆包时漏了 ``NSPopUpMenuWindowLevel`` / ``NSStatusWindowLevel``
    两个裸名字：它们在 ``macos_base`` 里只出现在 objc 分支，而 ``__init__`` 是
    **无条件**再导出的 ⇒ 非 darwin 机器上 import 本包即 ImportError。

    判据走 ``__all__`` 全量点名，不逐个列名字 —— 漏一个就是一整批测试文件的
    静默死亡（实测 13 个文件、839 个节点收集不了，而 macOS 上因为 PyObjC 是
    硬依赖、根本不显形）。
    """
    from barkit.shell import macos_window as mw

    missing = [name for name in mw.__all__ if not hasattr(mw, name)]
    assert missing == [], f"再导出了这些名字、当前平台却没有绑定：{missing}"


# ---------- 提示：框架统一落位（右上角 ＋ 叠放 ＋ 重排）----------
#
# 2026-09-23 用户诉求：「浮层提示统一显示在右上角、由框架层统一控制」。此前提示与交互浮层
# 共用 ``show_and_run``（水平居中、垂直 30%），位置写死在浮层基类里，调用方改不了 ——
# 所以「统一」的实体是**多一个出口**：提示走 ``show_toast``（框架定位 ＋ 入栈），
# 交互浮层照旧自己定（它要注意力，不该缩在屏角，也不该被提示顶开）。


def _avail() -> "object":
    """光标所在屏的可用区（与浮层落位同源）。"""
    screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
    return screen.availableGeometry()


def _toast(qtbot, size=(200, 40), **kwargs):
    """建一条提示并弹出去（走框架那个出口）。"""
    ov, seen = _spawn(qtbot, **kwargs)
    ov.setFixedSize(*size)      # _spawn 里那份是默认尺寸，这里按用例要的高矮改
    ov.show_toast()
    qtbot.wait(1)
    return ov, seen


def test_a_toast_lands_in_the_top_right_corner(qtbot):
    """提示要落在**可用区右上角**（离屏边一档），不是居中。"""
    ag = _avail()
    ov, _seen = _toast(qtbot)
    try:
        assert ov.x() == ag.x() + ag.width() - ov.width() - metrics.OVERLAY_EDGE_GAP
        assert ov.y() == ag.y() + metrics.OVERLAY_EDGE_GAP
        assert ov.geometry().bottom() <= ag.bottom() and ov.geometry().right() <= ag.right()
    finally:
        ov.dismiss(DISMISS_CLOSED)
    assert live_toasts() == []


def test_toasts_stack_downwards_instead_of_piling_on_each_other(qtbot):
    """两条提示**依次下移**，不叠在同一处（叠住＝前一条白弹了）。"""
    ag = _avail()
    first, _ = _toast(qtbot)
    second, _ = _toast(qtbot)
    try:
        assert first.y() == ag.y() + metrics.OVERLAY_EDGE_GAP, "先来的贴住屏角，不该被后来的推开"
        assert second.y() == first.y() + first.height() + metrics.OVERLAY_STACK_GAP
        assert first.geometry().bottom() < second.y(), "两块提示不许重叠"
    finally:
        for ov in (first, second):
            ov.dismiss(DISMISS_CLOSED)


def test_the_ones_below_move_up_when_a_toast_above_leaves(qtbot):
    """上面那条走了，下面的要**补位**（否则栈中间留个洞，看着像漏弹了一条）。"""
    ag = _avail()
    first, _ = _toast(qtbot)
    second, _ = _toast(qtbot)
    try:
        first.dismiss(DISMISS_CLOSED)
        qtbot.wait(1)
        assert second.y() == ag.y() + metrics.OVERLAY_EDGE_GAP, f"没补位：{second.y()}"
        assert live_toasts() == [second], "退场的那条要出栈，剩下的不该多也不该少"
    finally:
        second.dismiss(DISMISS_CLOSED)
    assert live_toasts() == []


def test_a_stack_taller_than_the_screen_is_pulled_back_inside_it(qtbot):
    """栈排到屏底**不许探出去**：探出去的那条等于没弹（用户只看到半块）。"""
    ag = _avail()
    h = 120
    # 造出「必定超出一屏」的条数：按档位算，不写死数字（屏幕尺寸换了判据照样成立）
    n = (ag.height() - 2 * metrics.OVERLAY_EDGE_GAP) // (h + metrics.OVERLAY_STACK_GAP) + 2
    made: list = []
    try:
        for _ in range(n):
            ov, _seen = _toast(qtbot, size=(200, h))
            made.append(ov)
        last = made[-1]
        assert last.geometry().bottom() <= ag.bottom(), f"探出屏底了：{last.geometry()}"
        assert last.y() == ag.y() + ag.height() - h, "超出的那些要夹回屏内沿，不许排到屏外"
    finally:
        for ov in made:
            ov.dismiss(DISMISS_CLOSED)


def test_an_interactive_overlay_keeps_its_own_place_and_stays_out_of_the_stack(qtbot):
    """业务浮层（一键输入倒计时卡）**不进提示栈**、位置也不许被统一到屏角。

    它是要盯着看、能拖能点的操作卡：居中偏上才对，且不该被一条 2 秒的提示顶开位置。
    """
    ag = _avail()
    ov, _seen = _spawn(qtbot, duration_ms=None)
    ov.show_and_run()
    try:
        assert live_toasts() == [], "业务浮层进了提示栈（会被后来的提示挤走）"
        assert ov.x() == ag.x() + (ag.width() - ov.width()) // 2, "业务浮层仍是水平居中，不跟提示同位置"
    finally:
        ov.dismiss(DISMISS_CLOSED)


# ---------- 常驻：框架统一落位（右上角 ＋ 钉住 ＋ 与提示共用一列）----------
#
# 2026-09-23 用户诉求：「时钟要在右上角（桌面应用，不是手机），且要与其他浮窗和谐相处」。
# 与提示栈共用右上角一列：常驻钉最上、提示从其下方排 —— 谁都不许把对方压住。


def _dock(qtbot, size=(200, 40), **kwargs):
    """建一个常驻浮层并钉到右上角（走框架那个出口）。"""
    ov, seen = _spawn(qtbot, **kwargs)
    ov.setFixedSize(*size)
    ov.show_docked()
    qtbot.wait(1)
    return ov, seen


def test_a_docked_overlay_lands_in_the_top_right_corner(qtbot):
    """常驻浮层要落在**可用区右上角**（离屏边一档），不是居中、也不进提示栈。"""
    ag = _avail()
    ov, _seen = _dock(qtbot)
    try:
        assert live_toasts() == [], "常驻浮层进了提示栈（会被后来的提示挤走）"
        assert live_docked() == [ov]
        assert ov.x() == ag.x() + ag.width() - ov.width() - metrics.OVERLAY_EDGE_GAP
        assert ov.y() == ag.y() + metrics.OVERLAY_EDGE_GAP
        assert ov.geometry().bottom() <= ag.bottom() and ov.geometry().right() <= ag.right()
    finally:
        ov.dismiss(DISMISS_CLOSED)
    assert live_docked() == []


def test_a_toast_stacks_below_a_docked_overlay_instead_of_piling_on_it(qtbot):
    """有常驻在右上角时，提示要从它**下方**开始排 —— 叠住＝提示白弹。"""
    ag = _avail()
    dock, _ = _dock(qtbot, size=(200, 60))
    toast, _ = _toast(qtbot, size=(200, 40))
    try:
        assert dock.y() == ag.y() + metrics.OVERLAY_EDGE_GAP, "常驻钉最上，不该被提示推开"
        assert toast.y() == dock.y() + dock.height() + metrics.OVERLAY_STACK_GAP
        assert dock.geometry().bottom() < toast.y(), "常驻与提示不许重叠"
    finally:
        for ov in (dock, toast):
            ov.dismiss(DISMISS_CLOSED)


def test_the_toast_column_moves_up_when_the_dock_leaves(qtbot):
    """常驻走了，提示要**补位**回到栏首（它让出的空间得收回来）。"""
    ag = _avail()
    dock, _ = _dock(qtbot, size=(200, 60))
    toast, _ = _toast(qtbot, size=(200, 40))
    try:
        dock.dismiss(DISMISS_CLOSED)
        qtbot.wait(1)
        assert toast.y() == ag.y() + metrics.OVERLAY_EDGE_GAP, f"没补位：{toast.y()}"
        assert live_docked() == []
    finally:
        toast.dismiss(DISMISS_CLOSED)


def test_a_docked_overlay_is_not_pushed_by_a_toast(qtbot):
    """反证：提示来了，常驻**不动**（一次也不许被顶开位置）。"""
    ag = _avail()
    dock, _ = _dock(qtbot, size=(200, 60))
    before = dock.pos()
    toast, _ = _toast(qtbot, size=(200, 40))
    try:
        assert dock.pos() == before, "常驻被提示顶开了位置"
        assert dock.y() == ag.y() + metrics.OVERLAY_EDGE_GAP
    finally:
        for ov in (dock, toast):
            ov.dismiss(DISMISS_CLOSED)


def test_redocking_after_a_resize_keeps_the_column_tight(qtbot):
    """常驻**尺寸变了**要重新钉位（右沿对齐 ＋ 提示仍在它下方）。"""
    ag = _avail()
    dock, _ = _dock(qtbot, size=(200, 60))
    toast, _ = _toast(qtbot, size=(200, 40))
    try:
        dock.setFixedSize(320, 100)
        dock.redock()
        qtbot.wait(1)
        assert dock.x() == ag.x() + ag.width() - dock.width() - metrics.OVERLAY_EDGE_GAP
        assert toast.y() == dock.y() + dock.height() + metrics.OVERLAY_STACK_GAP
    finally:
        for ov in (dock, toast):
            ov.dismiss(DISMISS_CLOSED)
