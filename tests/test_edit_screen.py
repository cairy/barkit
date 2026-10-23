"""tests/test_edit_screen：编辑屏的两件框架能力（``EditScreen`` / ``EditStack``）。

来历：2026-09-27 把「快捷键编辑弹窗」那套版面抽成框架口，好让四处「列表屏 → 编辑屏」
（快捷键 / 录屏通知规则 / 一键输入输入项 / 翻页时钟条目）＋ hosts 管理窗两个二级页共用
一份。

这两件东西是**框架能力**，用它们的四处只是间接走到 ⇒ 没有专门用例时，它们的判据
（「不过关不许落盘」「同一屏 push 两次不许登记两遍」「摘屏要真摘掉」）**一处都没守**。
故本篇按判据分层：

- **保存那一道门**（``EditScreen``）：先 ``on_validate``、后 ``on_commit``，不过关时
  **一个字都不许落盘**。这一层有一条**反证**——历史上它写成可覆写方法时，消费方那份
  ``commit`` 一次都调不到（组合件，``self`` 永远是本类实例），症状是**保存键亮着、点了
  什么都不发生、盘上没字**。故除了行为判据，另有一条**结构性判据**把那条死路钉死：
  ``EditScreen`` 不许再长出可覆写方法。
- **换屏**（``EditStack``）：进屏 / 回列表 / 摘屏三件事，各一条。
- **余高归属**（``stretch_content``）：默认余高落在内容区下方的留白里；带滚动区的屏调
  它之后余高归内容区。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from barkit.shell import ui_kit
from barkit.shell.design import metrics
from barkit.shell.ui_kit import EditScreen, EditStack, FormGroup


# ---------- 拼装 ----------


def _lay_out(widget: QWidget) -> None:
    """显示并跑一遍事件循环：几何要落定之后才量得到（离屏下同样成立）。"""
    widget.show()
    QApplication.processEvents()


def _screen(qtbot, *, on_validate=None, on_commit=None, size=(440, 560)) -> EditScreen:
    screen = EditScreen(
        title="编辑这一条", on_validate=on_validate, on_commit=on_commit
    )
    qtbot.addWidget(screen)
    screen.resize(*size)
    return screen


def _y(screen: EditScreen, widget: QWidget) -> int:
    """把 widget 顶边换算到**那一块面**的坐标系（量位置先过这一道，理由同 test_editor_shell）。"""
    return widget.mapTo(screen.shell().panel(), QPoint(0, 0)).y()


def _x(screen: EditScreen, widget: QWidget) -> int:
    return widget.mapTo(screen.shell().panel(), QPoint(0, 0)).x()


# ---------- 一、保存那一道门 ----------


def test_the_save_button_runs_validate_then_commit(qtbot):
    """点**真实那枚**保存键：先过 ``on_validate``、过了交 ``on_commit``。

    走按钮而不是直调 ``_on_save()``：直调验证不到**接线**（构造参数有没有真被接上）。
    历史上一版把两端写成可覆写方法，直调照样过、按钮却什么都不做。
    """
    seen: list[str] = []
    screen = _screen(
        qtbot,
        on_validate=lambda: (seen.append("validate"), None)[1],
        on_commit=lambda: seen.append("commit"),
    )
    _lay_out(screen)
    screen.save_button.click()
    assert seen == ["validate", "commit"], f"保存键没按「先过关再落盘」跑：{seen}"


def test_a_failed_validation_writes_nothing_to_the_ledger(qtbot):
    """不过关时 ``on_commit`` **一次都不许被调**，且错误文案红字落本屏。

    这是四处编辑屏唯一完全相同的保存逻辑（名称空了 / 重名 / 该填的没填）：门开在框架
    里，消费方只管「说哪句话、去哪一屏」。
    """
    committed: list[bool] = []
    screen = _screen(
        qtbot,
        on_validate=lambda: "名称不能重复",
        on_commit=lambda: committed.append(True),
    )
    _lay_out(screen)
    screen.save_button.click()
    assert committed == [], "校验没过却落了盘 —— 这一道门是摆设"
    assert screen.feedback.text() == "名称不能重复", (
        f"没过关时该把理由红字落本屏，实际反馈条说：{screen.feedback.text()!r}"
    )
    assert screen.feedback.isVisible(), "反馈条没被唤出来 ⇒ 用户看不到为什么保存没生效"


def test_a_screen_without_the_gate_still_commits(qtbot):
    """没传 ``on_validate``（没有额外约束的屏）时保存键照样落盘。"""
    committed: list[bool] = []
    screen = _screen(qtbot, on_commit=lambda: committed.append(True))
    _lay_out(screen)
    screen.save_button.click()
    assert committed == [True], "不传校验回调的屏，保存键点了没落盘"


def test_the_gate_has_no_overridable_hooks():
    """**结构性判据**：``EditScreen`` 不许再长出可覆写的 ``validate`` / ``commit``。

    这是**用历史缺陷反推出来的门**（2026-09-27 实测）：本类是**组合件**，消费方
    ``self._screen = EditScreen(...)`` 时 ``self`` 永远是本类实例 ⇒ 消费方在自己那边
    覆写出来的 ``commit`` **一次都调不到**，症状是**保存键亮着、点了什么都不发生、盘上
    没字**（看着像「保存坏了」，其实是接线断在无声处）。

    行为判据（上面三条）在**注入**这一版上永远是绿的 —— 它们抓不到「有人把覆写口加
    回来」。故另落这一条判据：只要那个口回来了，这条当场红，且它**不依赖**有没有人真
    去覆写。判据是名字，不是实现。
    """
    for name in ("commit", "validate"):
        assert not hasattr(ui_kit.EditScreen, name), (
            f"EditScreen 长出了可覆写的 {name}() —— 组合件里消费方覆写它一次都调不到"
            "（保存键亮着、点了什么都不发生）。保存两端只走构造参数注入。"
        )


# ---------- 二、版面（只有四条口） ----------


def test_add_group_is_the_only_way_into_the_content_zone(qtbot):
    """``add_group`` 放进内容区的是**分组**（不是裸行）—— 内容区只放卡是壳的既有判据。"""
    screen = _screen(qtbot)
    screen.add_group("这条规则")
    screen.add_group("进程匹配")
    widgets = screen.shell().content_widgets()
    assert len(widgets) == 2, f"内容区该有两组，实际 {len(widgets)}"
    assert all(isinstance(w, FormGroup) for w in widgets), (
        f"内容区混进了非分组的一级子件：{[type(w).__name__ for w in widgets]}"
    )


def test_a_destructive_action_sits_left_and_the_save_button_sits_right(qtbot):
    """破坏性动作贴**左缘**、保存键贴**右缘** —— 位置在这里是身份，不是偏好。"""
    screen = _screen(qtbot)
    screen.add_group("这条")
    delete = screen.add_destructive("删除这条", lambda: None)
    _lay_out(screen)

    panel = screen.shell().panel()
    left = metrics.PAD_CARD[0]
    right = panel.width() - metrics.PAD_CARD[2] - screen.save_button.width()
    assert _x(screen, delete) == left, (
        f"破坏性动作没贴住内容左缘（它在 {_x(screen, delete)}，该在 {left}）"
    )
    assert _x(screen, screen.save_button) == right, (
        f"保存键没贴住内容右缘（它在 {_x(screen, screen.save_button)}，该在 {right}）"
    )


def test_set_title_rewrites_the_nav_band(qtbot):
    """``set_title`` 换导航带里的标题（正在编辑的那一条改了名字 / 换了对象）。"""
    screen = _screen(qtbot)
    screen.add_group("这条")
    _lay_out(screen)
    labels = [lbl.text() for lbl in screen.shell().panel().findChildren(QLabel)]
    assert "编辑这一条" in labels, f"导航带里没找到初始标题：{labels}"
    screen.set_title("改成这条")
    labels = [lbl.text() for lbl in screen.shell().panel().findChildren(QLabel)]
    assert "改成这条" in labels and "编辑这一条" not in labels, (
        f"标题没被改写：{labels}"
    )


def test_the_feedback_slot_is_the_shell_s_own(qtbot):
    """``screen.feedback`` 就是壳里那条槽（不是另开的一条）。"""
    screen = _screen(qtbot)
    assert screen.feedback is screen.shell().feedback, (
        "编辑屏的反馈槽与壳里那条不是同一个 ⇒ 消费方说的话落到了别处"
    )


# ---------- 三、余高归属 ----------


def test_the_extra_height_goes_to_the_content_with_or_without_stretch_content(qtbot):
    """余高**本来就归内容区**（它自带滚动区）—— 调不调 ``stretch_content`` 都一样。

    ◷ 2026-10-16：内容区自带滚动区之后，余高的归属由**结构**定死（滚动区是
    ``Expanding``、尾部那段具名 spacer 退成 ``Minimum``），那个方法退化成**兼容口**
    —— 本仓两处消费方（录屏通知的进程匹配、hosts 的批量导入多行框）仍在调它。

    旧判据（「调了它之后余高**等量**从留白挪进内容区」）在新结构下量的是两个相同的数，
    等于空转，故换成这一条：调与不调，内容区（滚动区）的高度逐个相同。它同时还守着
    「别把余高的归属又交回那段留白」—— 那件事一旦发生，两个数就不再相同。
    """
    def room(*, stretch: bool) -> int:
        screen = _screen(qtbot, size=(440, 640))
        screen.add_group("这组要吃掉余高")
        if stretch:
            screen.stretch_content()
        _lay_out(screen)
        return screen.shell().content_scroll().height()

    assert room(stretch=False) == room(stretch=True), (
        "调 stretch_content 之后内容区高度变了 ⇒ 余高的归属又被交给它了。"
        "内容区自带滚动区之后，余高本来就归它（见 EditorShell ② 那段）。"
    )


# ---------- 四、换屏 ----------


def _stack(qtbot) -> "tuple[EditStack, QWidget]":
    list_page = QWidget()
    stack = EditStack(list_page)
    qtbot.addWidget(stack)
    stack.resize(440, 420)
    return stack, list_page


def test_the_list_page_is_on_stage_first(qtbot):
    stack, list_page = _stack(qtbot)
    assert stack.current() is list_page, "刚造出来的换屏容器不在列表屏上"
    assert stack.list_page() is list_page, "列表屏读口指错了对象"


def test_push_shows_the_screen_and_back_returns_to_the_list(qtbot):
    stack, list_page = _stack(qtbot)
    editor = _screen(qtbot)
    stack.push(editor)
    _lay_out(stack)
    assert stack.current() is editor, "push 之后没切到那一屏"

    stack.back()
    _lay_out(stack)
    assert stack.current() is list_page, "back 之后没回列表屏"


def test_pushing_the_same_screen_again_does_not_disturb_the_stage(qtbot):
    """重复进同一屏（快捷键页那种「一屏复用」）时，**台上的屏不许抖一下**。

    ``push`` 里那道 ``indexOf < 0`` 判的承重点**不是「页数」** —— Qt 的
    ``QStackedWidget.addWidget`` 对已在栈里的页本就不再重复登记（页数不涨，探针实测
    2026-09-27）。它真正防的是：重复 ``addWidget`` 会把那一页**重新插到末尾、并把当前
    页退回第一页** —— 于是「再 push 一次」会让台上的屏先闪回列表、再被
    ``setCurrentWidget`` 拉回来，顺带**白发一次 ``currentChanged``**。故判据落在信号
    次数上：重复 push 那一趟一次都不该发。

    （只数页数守不住这条：去掉那道判，页数照样是 2、``current()`` 也照样对 —— 那样
    的判据「去掉防护照样绿」，等于没测。）
    """
    stack, _list_page = _stack(qtbot)
    editor = _screen(qtbot)
    inner = _inner_stack(stack)
    seen: list[int] = []
    inner.currentChanged.connect(seen.append)

    stack.push(editor)
    _lay_out(stack)
    assert inner.currentWidget() is editor, "第一趟 push 没把编辑屏推上台 ⇒ 下面那条是空转的"
    assert len(seen) >= 1, "换到编辑屏一次 currentChanged 都没发 ⇒ 判据量错了信号"
    before = len(seen)

    stack.push(editor)
    _lay_out(stack)
    assert inner.currentWidget() is editor, "重复 push 之后台上那屏不是它了"
    assert len(seen) == before, (
        f"重复 push 同一屏时当前页抖了一下（currentChanged 多发 {len(seen) - before} 次）"
        "⇒ Qt 把那一页重新插到末尾、当前页退回列表了，缺 indexOf<0 那道判"
    )
    assert _stack_page_count(stack) == 2, "同一屏被登记了两页（列表 ＋ 编辑屏该是 2）"


def test_drop_takes_the_screen_off_the_stack_and_destroys_it(qtbot):
    """``drop`` 摘掉并销毁那一屏（「开一个造一个」那条路的收尾），并回列表屏。

    ``setParent(None)`` 与 ``deleteLater()`` 两件都要：只写后者的话，重建后那一帧
    新旧两套同时可见（2026-09-27 在 ``clear_layout`` 上踩过同一个坑）。
    """
    stack, list_page = _stack(qtbot)
    editor = _screen(qtbot)
    stack.push(editor)
    _lay_out(stack)

    stack.drop(editor)
    assert stack.current() is list_page, "drop 之后没回列表屏"
    assert editor.parent() is None, "drop 之后那一屏还挂在父件上（没真摘掉）"
    assert _stack_page_count(stack) == 1, "drop 之后栈里还留着那一页"


def _inner_stack(stack: EditStack):
    """换屏容器里那个 ``QStackedWidget``（判据读口没给这个数，测试自己取）。"""
    from PySide6.QtWidgets import QStackedWidget

    inner = stack.findChild(QStackedWidget)
    assert inner is not None, "换屏容器里没有 QStackedWidget ⇒ 它没按框架那一版装"
    return inner


def _stack_page_count(stack: EditStack) -> int:
    """换屏容器里当前有几页。"""
    return _inner_stack(stack).count()


def test_the_stack_reports_the_size_of_the_page_on_stage(qtbot):
    """``EditStack`` 按**台上那页**报尺寸 —— 列表页再高也顶不动编辑屏（2026-10-16）。

    Qt 原生的 ``QStackedLayout`` 取**所有页的最大值**（含被 ``hide()`` 掉的那几页）。实测
    （todo 详情页）：列表页自然高 572，而编辑屏在台上时 ``sizeHint`` 也被顶成 572 ⇒ 详情
    窗那层滚动区多出一条 318px 的滚动条，编辑屏的返回键跟着滚出窗口（``_TopPageStack``
    这个子类就是为了改掉它）。

    这条与 ``test_detail_form`` 那条「矮窗下返回键仍在窗内」是**同一次改动的两端**：这里
    钉尺寸上报的口径，那里钉实际观感。
    """
    list_page = QWidget()
    list_page.setMinimumHeight(600)
    stack = EditStack(list_page)
    qtbot.addWidget(stack)
    short = _screen(qtbot)
    stack.push(short)
    _lay_out(stack)

    assert stack.sizeHint().height() < 600, (
        f"台上是那块矮编辑屏，EditStack 还是按 600 高的列表页报尺寸"
        f"（{stack.sizeHint().height()}）⇒ 它会把外层窗体顶高、把编辑屏的头尾顶出视口"
    )


def test_the_stack_keeps_its_width_when_the_page_changes(qtbot):
    """台上换页时 ``EditStack`` 的**宽度不变** —— 只有高度跟着当前页走。

    2026-10-16 实测踩到过：连宽度一起按当前页报，顶层 widget 就会随台上那页变窄 ——
    ``todo`` 列表页的行因此被 elide 成空串，``tests/test_todo_detail.py`` 七条用例转红，
    而报错信息只说「清单里没有标题为 X 的那一行：['']」，**指不到这里**。故单独钉一条：
    换屏容器不该因为此刻在台上的是哪一页而忽宽忽窄。

    量的是**换页前后两个数相不相等**（不是「宽 ≥ 某个值」）：两块屏各自的自然宽都可能
    很大，只判下限会漏掉「换过去之后宽变小了」。量的对象是 ``minimumSizeHint`` ——
    承重的是它（顶层 widget 的宽由最小尺寸兜底；只动 ``sizeHint`` 的宽度实测不显形）。
    """
    list_page = QWidget()
    list_page.setMinimumWidth(600)
    stack = EditStack(list_page)
    qtbot.addWidget(stack)
    narrow = _screen(qtbot)
    _lay_out(stack)
    before = stack.minimumSizeHint().width()

    stack.push(narrow)
    _lay_out(stack)
    after = stack.minimumSizeHint().width()
    assert after == before, (
        f"换到另一页之后 EditStack 的**最小宽**从 {before} 变成了 {after}"
        "⇒ 它会随台上那页忽宽忽窄（列表页的行会被 elide 成空串）"
    )
