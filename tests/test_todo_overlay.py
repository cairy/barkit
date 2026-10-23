"""tests/test_todo_overlay：合并方案**第 2 批（浮层重设计）**的**运行期面判据**。

``plugins/todo/clock_view.py`` 是那一半「画」：翻页数字卡 / 三型钟面 / 浮层（钟面区 ＋
结构化条目行）/ 主线程控制器。这一批守五条**容易被写错且不报错**的事：

1. **实时钟不接外部喂的值**（``ClockOverlay.apply`` 在时钟型上刻意不喂）—— 那一刻几点
   只许由钟面自己判定；喂一遍就是第二个判定者，两条 1s 节拍的相位差落进 300ms 翻页
   动画里会把同一个数字翻两次。
2. **锚点两项同生共死**（``FlipClock.set_display``）—— 只来一项就整体作废；存下那半份
   会被实时节拍读成 0，钟面一路显示 ``00:00``（**静默错值**，不报错）。
3. **行没变就不重建**（``ClockOverlay._set_rows``）—— 稳态下每秒都进 ``apply``，
   不拦截就会一秒重建一次行里的按钮（点着点着消失）＋ 可见抖动。
4. **行上的按钮要回得到插件**（``ClockController`` → ``plugin.on_row_action``）——
   控制器不认识任何一条业务规则，只报「谁按了哪个动作」。
5. **一行的文字顶只由边距决定，且那条边距把行盒摆进行高正中**（``_RowWidget`` ＋
   ``_text_top_margin``）—— 交给 ``QLabel`` 默认的居中（把**整个文本块**摆在盒中央）时，
   标题一换行首行就被推下去，与同一行的状态点 / 时刻错开；而边距若换回「手算一条公共
   基线」（按 ``ascent + descent`` 的行盒、或按 ``tightBoundingRect`` 的墨迹），整行会
   整体抬高 0.35~1.5px —— 真机截图看得见，而 Linux 参考字体恰好不偏，所以只有真机能发现。

浮层还守「关过一次不再自动重开、但窗口结束／条目换了一批要能再开」（抑制集合**带窗口
维度**）—— 漏了它固定类条目每天 ``active_ids`` 相同，表现成「关过一次就再也不弹」。

内核（登场 / 退场 / 钟面优先级 / 上限截断）在 ``test_todo_scheduler.py``。
"""

from __future__ import annotations

import time

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QFontMetricsF
from PySide6.QtWidgets import QLabel, QPushButton

from barkit.plugins.todo.clock_view import (
    _DOT_ACTIONABLE,
    _DOT_PLAIN,
    ClockController,
    ClockOverlay,
    FlipClock,
    FlipDigit,
    _RowWidget,
)
from barkit.plugins.todo.scheduler import (
    ACTION_DONE,
    ACTION_SNOOZE,
    FACE_CLOCK,
    FACE_COUNTDOWN,
    FACE_COUNTUP,
    Row,
)
from barkit.shell.design import metrics
from barkit.shell.overlay import DISMISS_CLOSED, live_docked


def _row(eid="e1", title="喝水", text="", when="09:30", actions=(), at_s=34200) -> Row:
    return Row(eid, title, text, when, tuple(actions), at_s)


def _display(
    *,
    show=True,
    face=FACE_CLOCK,
    value="09:30:00",
    rows=None,
    more="",
    size="medium",
    ids=("e1",),
    anchor_s=None,
    anchor_mono=None,
    test=False,
) -> dict:
    """一份**整屏显示态**（``plugin._compute_display()`` 交出去的那个 dict 的形状）。"""
    return {
        "show": show,
        "face": face,
        "value": value,
        "rows": [_row()] if rows is None else rows,
        "more_text": more,
        "size": size,
        "active_ids": list(ids),
        "anchor_s": anchor_s,
        "anchor_mono": anchor_mono,
        # 「预览这一条」（详情页那枚键）的标记：它盖过常态、且**不理会关闭抑制**。
        "test": test,
    }


def _overlay(qtbot, tier="medium", gesture="either") -> ClockOverlay:
    ov = ClockOverlay(tier, gesture)
    qtbot.addWidget(ov)
    return ov


def _current_row_labels(ov) -> list:
    """行区**当前布局里**那些控件上的文本。

    只看布局：旧行控件虽然被 ``deleteLater``，但在事件循环转起来之前仍是子控件，
    ``findChildren`` 会把它们一起收进来（那会让「换了一批」的判据假绿）。
    """
    lay = ov._rows.layout()
    out: list = []
    for i in range(lay.count()):
        w = lay.itemAt(i).widget()
        if w is not None:
            out.extend(lbl.text() for lbl in w.findChildren(QLabel))
    return out


def _labels_in_row(w: _RowWidget) -> list:
    """一行里的文本标签，**按布局顺序**（状态点 / 标题 / 副文本 / 时刻，跳过多余的 1 号弹簧）。

    按布局取、不按 ``findChildren``：后者是「谁先建」的顺序，跟落位判据没关系。
    """
    lay = w.layout()
    out: list = []
    for i in range(lay.count()):
        item = lay.itemAt(i).widget()
        if isinstance(item, QLabel):
            out.append(item)
    return out


def _drawn_text_top(lbl: QLabel) -> float:
    """Qt **将把**这枚标签的文字画在盒里的哪一行（相对行顶）。

    读法是 ``QFontMetricsF.boundingRect(rect, flags, text)``：flags 那版重载返回的是
    「按 flags 摆进 rect 之后」的文字外接框 —— 也就是 Qt 自己算出来的落位，**不必渲染
    取像素**（契约清单禁像素级门禁：离屏与真机的渲染有差异，噪声会盖住真回归）。
    于是「换行有没有把首行推下去」这件事可以直接断言，不用管 ``place()`` 用的是哪种对齐。
    """
    m = lbl.contentsMargins()
    inner = lbl.rect().adjusted(m.left(), m.top(), -m.right(), -m.bottom())
    flags = int(lbl.alignment())
    if lbl.wordWrap():
        flags |= int(Qt.TextFlag.TextWordWrap)
    box = QFontMetricsF(lbl.font()).boundingRect(QRectF(inner), flags, lbl.text())
    return lbl.geometry().y() + box.top()


# ── 翻页数字卡：同值不重翻 ────────────────────────────────────────────


def test_setting_the_same_digit_does_not_flip_again(qtbot):
    """守卫**只看值，不看动画跑到哪了** —— 动画中途喂同一个值也不许再翻一次。"""
    d = FlipDigit("medium", digit="5")
    qtbot.addWidget(d)
    d.set_value("5")
    assert d._p == 1.0, "同值（静止态）什么都不该做"

    d.set_value("7")
    assert d._prev == "5" and d._cur == "7"

    d._p = 0.3                      # 假装动画正跑到一半
    d.set_value("7")                # 同一个值
    assert d._p == 0.3, "动画中途喂同一个值，被当成了「要再翻一次」"


# ── 三型钟面：锚点 ────────────────────────────────────────────────────


def test_a_countdown_takes_a_full_anchor_and_walks_from_it(qtbot):
    clock = FlipClock("medium")
    qtbot.addWidget(clock)
    clock.set_display(FACE_COUNTDOWN, "30:00", anchor_s=1800, anchor_at=time.monotonic())

    assert clock.mode == FACE_COUNTDOWN
    assert clock._anchor_s == 1800 and clock._anchor_at is not None
    assert clock._current == "30:00"


def test_a_countup_walks_forward_from_its_anchor(qtbot):
    """正计时与倒计时**只差方向**：同一个锚点字段、同一个格式，往大走。"""
    clock = FlipClock("medium")
    qtbot.addWidget(clock)
    clock.set_display(FACE_COUNTUP, "10:00", anchor_s=600, anchor_at=time.monotonic())

    assert clock.mode == FACE_COUNTUP
    assert clock._direction == 1
    assert clock._current == "10:00"


def test_a_half_anchor_is_discarded_whole(qtbot):
    """只来一项（有秒数、没有时刻）⇒ 整体作废、回落成直接显示 ``value``。

    存下那半份会被实时节拍读成 0 ⇒ 钟面一路显示 ``00:00``（静默错值）。
    """
    clock = FlipClock("medium")
    qtbot.addWidget(clock)
    clock.set_display(FACE_COUNTDOWN, "30:00", anchor_s=1800, anchor_at=None)

    assert clock._anchor_s is None
    assert clock._anchor_at is None
    assert clock._current == "30:00"


def test_the_live_clock_ignores_whatever_the_framework_feeds_it(qtbot):
    """时钟型：模式要切回来，但**值不采纳** —— 那一刻几点只许钟面自己判定。"""
    ov = _overlay(qtbot)
    fed: list = []
    ov._clock.set_display = lambda *a, **k: fed.append((a, k))  # type: ignore[method-assign]

    ov.apply("medium", FACE_COUNTDOWN, "30:00", [], anchor_s=1800,
             anchor_mono=time.monotonic())
    ov.apply("medium", FACE_CLOCK, "99:99:99", [])

    assert ov._clock.mode == FACE_CLOCK, "模式必须真的切回来（否则继续走一个已结束的倒计时）"
    assert len(fed) == 1, "时钟型那一次不该走 set_display"
    assert ov._clock._anchor_s is None and ov._clock._anchor_at is None
    assert ov._clock._current != "99:99:99"


# ── 浮层的行区 ────────────────────────────────────────────────────────


def test_unchanged_rows_are_not_rebuilt(qtbot):
    """稳态下每秒都进 ``apply``：行没变就跳过重建（否则按钮一秒重建一次）。"""
    ov = _overlay(qtbot)
    rows = [_row()]
    ov.apply("medium", FACE_CLOCK, "", rows)
    first = ov._rows.layout().itemAt(0).widget()

    ov.apply("medium", FACE_CLOCK, "", [_row()])   # 内容相同、对象不同

    assert ov._rows.layout().itemAt(0).widget() is first, "同一份行被重建了"


def test_changed_rows_are_swapped_in(qtbot):
    ov = _overlay(qtbot)
    ov.apply("medium", FACE_CLOCK, "", [_row(title="喝水")])
    ov.apply("medium", FACE_CLOCK, "", [_row(title="交周报")])

    labels = _current_row_labels(ov)
    assert "交周报" in labels
    assert "喝水" not in labels, "旧行没被换掉"


def test_the_rows_area_hides_when_there_is_nothing_to_list(qtbot):
    ov = _overlay(qtbot)
    ov.apply("medium", FACE_CLOCK, "", [_row()])
    assert ov._rows.isVisibleTo(ov) is True

    ov.apply("medium", FACE_CLOCK, "", [])
    assert ov._rows.isVisibleTo(ov) is False


def test_the_status_dot_says_whether_the_row_is_actionable(qtbot):
    ov = _overlay(qtbot)
    ov.apply("medium", FACE_CLOCK, "", [_row(actions=(ACTION_DONE,))])
    assert _DOT_ACTIONABLE in _current_row_labels(ov)

    ov.apply("medium", FACE_CLOCK, "", [_row(eid="e2")])
    assert _DOT_PLAIN in _current_row_labels(ov), "不可操作的行要画实心点"


def test_a_rows_button_reports_who_was_pressed_and_what(qtbot):
    """按条目配动作：配了「完成」才画那枚按钮，按下后报 ``(entry_id, action)``。"""
    w = _RowWidget(_row(eid="td9", actions=(ACTION_DONE,)))
    qtbot.addWidget(w)
    seen: list = []
    w.action.connect(lambda eid, act: seen.append((eid, act)))

    btn = next(b for b in w.findChildren(QPushButton) if b.text() == "完成")
    btn.click()

    assert seen == [("td9", ACTION_DONE)]


def test_a_row_without_actions_draws_no_buttons(qtbot):
    """**画一个点了没用的按钮是骗人** —— 不配就不画。"""
    w = _RowWidget(_row())
    qtbot.addWidget(w)
    assert w.findChildren(QPushButton) == []


def test_a_row_button_only_offers_what_the_entry_asked_for(qtbot):
    w = _RowWidget(_row(actions=(ACTION_SNOOZE,)))
    qtbot.addWidget(w)
    assert sorted(b.text() for b in w.findChildren(QPushButton)) == ["稍后"]


def test_row_text_top_is_pinned_by_its_margin(qtbot):
    """一行的文字顶**只由边距决定**：边距 = (行高 − 自身行盒) / 2，行高钉在 inline 档。

    判据钉的是这条等式，不是「有没有 AlignTop」—— 出问题的那两版也都是 AlignTop，差就差在
    上边距是按「公共基线 − 自身 ascent」手算出来的：按 ``ascent + descent`` 的行盒算会整体
    抬高 0.35px，按 ``tightBoundingRect`` 的墨迹算反而抬高 1.25~1.5px（那个墨迹带把汉字下方
    的空白也算进去了）。真机上看到的是**整行文字比右侧按钮的文字高**。
    期望值在本用例里独立重写一遍（不问被测代码要那条式子）。
    """
    w = _RowWidget(
        _row(title="交周报", text="显示文本", when="今天 18:00",
             actions=(ACTION_DONE, ACTION_SNOOZE))
    )
    qtbot.addWidget(w)
    labels = [lbl for lbl in w.findChildren(QLabel)]
    assert len(labels) == 4, "状态点 / 标题 / 副文本 / 时刻各一枚"

    for lbl in labels:
        lbl.ensurePolished()
        fm = QFontMetricsF(lbl.font())
        want = max(0, round((metrics.BTN_INLINE - fm.height()) / 2))
        assert lbl.alignment() & Qt.AlignmentFlag.AlignTop, f"{lbl.text()!r} 不是左上对齐"
        got = lbl.contentsMargins().top()
        assert got == want, (
            f"{lbl.text()!r} 的上边距 {got} ≠ 行盒半高 {want} —— 又按手算的公共基线摆了"
        )
        # 最小高度也要钉住：「带按钮的行」与「不带的」才一样高（22px）。
        assert lbl.minimumHeight() == metrics.BTN_INLINE


def test_a_wrapped_title_does_not_move_the_first_line(qtbot):
    """标题换行时**首行留在原处**：同一行的状态点 / 副文本 / 时刻都不跟着挪。

    这是「左上对齐 ＋ 上边距」换来的性质 —— 文字顶 = 盒顶 ＋ 边距，与盒高无关。若退回
    ``QLabel`` 默认的居中（把**整个文本块**摆在盒中央），标题折成两行时首行就被推走，
    与同一行的状态点错开（cocoa 实测：边距留着差 1.5px、边距也清了差 3.5px），也就是真机上
    「左边这排字跟右边时刻不在一条线上」。

    判据用 :func:`_drawn_text_top`（Qt 自己算的落位），**不看对齐旗** —— 否则这条与上一条
    只是同一句话说两遍。那个读数口按 ``boundingRect`` 口径、绝对值与真机渲染差一点点
    （离屏与真机的行盒高度本来就有差），所以只比**同一枚标签在两行之间的差**：合规时它精确
    等于边距（两行都相同），一旦谁把落位交给盒高，两行当场不等。
    """
    short = _RowWidget(_row(title="交周报", text="显示文本", when="18:27"))
    long = _RowWidget(_row(title="交一份很长很长的周报，长到必须换行才能放下",
                           text="显示文本", when="18:27"))
    qtbot.addWidget(short)
    qtbot.addWidget(long)
    short.adjustSize()
    long.adjustSize()

    s, lng = _labels_in_row(short), _labels_in_row(long)
    assert len(s) == len(lng) == 4, "两行应当是同一副骨架（状态点/标题/副文本/时刻）"
    assert lng[1].height() > metrics.BTN_INLINE, (
        "标题压根没换行 ⇒ 这条判据是空转（先换个够长的标题）"
    )

    for i, name in enumerate(("状态点", "标题（首行）", "副文本", "时刻")):
        was, now = _drawn_text_top(s[i]), _drawn_text_top(lng[i])
        assert now == was, f"{name} 的文字顶换行后从 {was} 挪到了 {now}"


def test_changing_the_size_rebuilds_the_clock(qtbot):
    ov = _overlay(qtbot, tier="small")
    first = ov._clock
    ov.apply("large", FACE_CLOCK, "", [])
    assert ov._clock is not first
    assert ov._clock._tier == "large"


def test_a_button_press_does_not_start_dragging_the_overlay(qtbot):
    """行上的按钮是子控件、自己吃掉按下 ⇒ 点按钮不会把浮层拖走。"""
    ov = _overlay(qtbot)
    ov.apply("medium", FACE_CLOCK, "", [_row(actions=(ACTION_DONE,))])
    btn = next(b for b in ov._rows.findChildren(QPushButton) if b.text() == "完成")

    btn.click()                      # 若浮层把按下当拖动，这里会抛 / 位移
    assert ov._dragging is False


# ── 主线程控制器 ──────────────────────────────────────────────────────


class _FakePlugin:
    """控制器只认这四样（``_display`` 读、``get_close_gesture`` 读、``on_row_action`` 写、
    ``end_test`` 写），替身就按这个面做。"""

    def __init__(self, display: dict, testing: bool = False) -> None:
        self._display = display
        self.actions: list = []
        #: 是不是「预览这一条」态；``end_test`` 会把它收掉（与真插件同款）。
        self.testing = testing
        self.ended: list = []

    def get_close_gesture(self) -> str:
        return "either"

    def on_row_action(self, entry_id: str, action: str) -> None:
        self.actions.append((entry_id, action))

    def end_test(self) -> bool:
        """返回**刚才是不是在测试态**（控制器据此决定要不要记关闭抑制）。"""
        self.ended.append(True)
        was, self.testing = self.testing, False
        return was


def test_the_controller_puts_the_overlay_up_when_show_is_true(qtbot):
    ctl = ClockController(_FakePlugin(_display(show=True)))
    ctl.on_display()
    try:
        assert ctl._overlay is not None
        assert ctl._overlay.isVisible()
        assert live_docked() == [ctl._overlay], "常驻浮层要走框架那条「钉在右上角」的路"
    finally:
        ctl.dispose()
    assert live_docked() == []


def test_the_controller_keeps_everything_off_when_show_is_false(qtbot):
    ctl = ClockController(_FakePlugin(_display(show=False)))
    ctl.on_display()
    assert ctl._overlay is None
    assert live_docked() == []


def test_the_controller_takes_the_overlay_down_when_the_window_ends(qtbot):
    plugin = _FakePlugin(_display(show=True))
    ctl = ClockController(plugin)
    ctl.on_display()
    assert ctl._overlay is not None

    plugin._display = _display(show=False)
    ctl.on_display()

    assert ctl._overlay is None
    assert live_docked() == []
    ctl.dispose()


def test_the_controller_pushes_the_rows_into_the_overlay(qtbot):
    plugin = _FakePlugin(_display(show=True, rows=[_row(title="交周报")]))
    ctl = ClockController(plugin)
    ctl.on_display()
    try:
        assert "交周报" in _current_row_labels(ctl._overlay)
    finally:
        ctl.dispose()


def test_a_row_action_reaches_the_plugin(qtbot):
    """控制器不认识业务规则：只把「谁按了哪个动作」交回插件。"""
    plugin = _FakePlugin(_display(show=True))
    ctl = ClockController(plugin)
    ctl.on_display()
    try:
        ctl._overlay.row_action.emit("td9", ACTION_SNOOZE)
        assert plugin.actions == [("td9", ACTION_SNOOZE)]
    finally:
        ctl.dispose()


def test_closing_the_overlay_by_hand_keeps_it_closed_for_the_same_batch(qtbot):
    """用户自己关掉（双击 / 右键）之后，**同一批条目**不许每拍又弹回来。"""
    plugin = _FakePlugin(_display(show=True, ids=("e1",)))
    ctl = ClockController(plugin)
    ctl.on_display()
    first = ctl._overlay

    first.user_closed.emit()
    first.dismiss(DISMISS_CLOSED)
    qtbot.wait(1)                    # 让 deleteLater 落地（Python 壳变死壳）

    ctl.on_display()
    assert ctl._overlay is None, "关过之后同一批条目又被重开了"

    plugin._display = _display(show=True, ids=("e2",))
    ctl.on_display()                 # 换了一批 ⇒ 该重开
    assert ctl._overlay is not None and ctl._overlay is not first
    ctl.dispose()


def test_the_suppression_clears_when_the_window_ends(qtbot):
    """抑制集合必须**带窗口维度**：窗口一结束就清掉。

    漏了它，固定类条目每天的 ``active_ids`` 完全相同（``{e1}``）⇒
    ``ids == self._suppressed`` 永久命中早退，表现成「关过一次就再也不弹」。
    """
    plugin = _FakePlugin(_display(show=True, ids=("e1",)))
    ctl = ClockController(plugin)
    ctl.on_display()
    ov = ctl._overlay
    ov.user_closed.emit()
    ov.dismiss(DISMISS_CLOSED)
    qtbot.wait(1)

    plugin._display = _display(show=False)
    ctl.on_display()
    assert ctl._suppressed == frozenset(), "窗口结束没清抑制集合"

    plugin._display = _display(show=True, ids=("e1",))   # 明天同一个 id 又进窗口
    ctl.on_display()
    assert ctl._overlay is not None, "窗口结束之后还压着不放（再也不弹）"
    ctl.dispose()


def test_closing_a_preview_ends_the_test_without_suppressing(qtbot):
    """预览态下关掉浮层 ＝ **预览的终点**：结束测试态，且**不记进关闭抑制**。

    抑制那笔账说的是「这一批条目今天别再自动弹」—— 预览是用户自己点开的，与它无关；
    记进去之后那条**真的到点该弹**时判据已经被污染（``ids == _suppressed`` 命中早退）。
    """
    plugin = _FakePlugin(_display(show=True, ids=("e1",), test=True), testing=True)
    ctl = ClockController(plugin)
    ctl.on_display()
    ov = ctl._overlay
    assert ov is not None, "测试态自己就要求显示，这里没建浮层后面无从关起"

    ov.user_closed.emit()
    ov.dismiss(DISMISS_CLOSED)
    qtbot.wait(1)                    # 让 deleteLater 落地（Python 壳变死壳）

    assert plugin.ended == [True], "关掉浮层没结束测试态（下一拍又按这条强行重画）"
    assert ctl._suppressed == frozenset(), "预览被记进了关闭抑制"


def test_a_preview_shows_even_after_the_user_closed_that_batch(qtbot):
    """预览**不受关闭抑制**：用户此前关过这一条的浮层，点「预览这一条」仍要看得到。"""
    plugin = _FakePlugin(_display(show=True, ids=("e1",)))
    ctl = ClockController(plugin)
    ctl.on_display()
    first = ctl._overlay
    assert first is not None

    first.user_closed.emit()         # 常态：这一批今天别再自动弹
    first.dismiss(DISMISS_CLOSED)
    qtbot.wait(1)
    ctl.on_display()
    assert ctl._overlay is None

    plugin._display = _display(show=True, ids=("e1",), test=True)
    ctl.on_display()
    assert ctl._overlay is not None, "被关闭抑制压着 ⇒ 点预览什么都不出现"
    ctl.dispose()


def test_the_controller_survives_a_dead_overlay_shell(qtbot):
    """用户关掉的浮层：``deleteLater`` 之后 C++ 本体没了、Python 壳还在。

    直接对死壳调 ``isVisible()`` 会抛 ``RuntimeError`` ⇒ 那一拍中断、浮层永不重建
    （实机 2026-09-28「下班通知不弹、日志每拍报 Internal C++ object already deleted」）。
    """
    plugin = _FakePlugin(_display(show=True, ids=("e1",)))
    ctl = ClockController(plugin)
    ctl.on_display()
    ctl._overlay.dismiss(DISMISS_CLOSED)   # 不走 user_closed：壳留着，本体没了
    qtbot.wait(1)

    plugin._display = _display(show=True, ids=("e2",))
    ctl.on_display()                       # 不许抛
    assert ctl._overlay is not None
    ctl.dispose()
