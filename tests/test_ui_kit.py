"""组件盒冒烟 + 目录完整性。

两条判据：

1. ``ui_kit.__all__`` 里**每个公开名**都在这篇里有去处（构造过一次，或被某条用例直接
   跑过）—— 全集减已覆盖集必须为空。以后往 ``__all__`` 加件而没补这里 ⇒ 红。
2. 构造得出来的件要能**渲染**（``show()`` ＋ ``grab()``），不是「导入得动」。

为什么要这两条：``LogoWidget`` / ``apply_button_tier`` / ``relay_action_result`` /
``insert_page_slot`` / ``insert_page_tail`` 五个公开件一度**零测试引用** —— 库里有、
但没人知道能用，也就没人发现它们还能不能用（等价于没有）。
用法文档见 `docs/guides/组件盒用法.md`。
"""

from __future__ import annotations

import pytest

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QComboBox, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from barkit.contribution.specs import (
    ActionOutcome,
    ActionResult,
    IconName,
    MenuItem,
    StateLevel,
)
from barkit.shell import ui_kit
from barkit.shell.design import metrics


# ──────────────────────────────────────────────
# 1 目录完整性：__all__ 里每个公开名都要有去处
# ──────────────────────────────────────────────

#: 公开名 → 「它在本篇哪儿被跑到」。**不是注释**：键集合与 ``__all__`` 的公开子集必须
#: 逐字相等（见下一条用例）。新加一件却忘了写进来 ⇒ 红，逼作者至少想一次「怎么验它」。
_COVERED: dict[str, str] = {
    # 常量与日志器：没有可构造的东西，只有值本身
    "log": "模块日志器，不是控件",
    "PLAIN_ZONE": "分区名常量（字符串）",
    "PAGE_SLOT": "标题在布局里的下标，test_insert_page_slot_lands_below_the_title",
    "ICON_TONES": "图标底色档位表，test_status_row_reads_its_icon_tone",
    "ICON_TONE_DEFAULT": "同上（缺省档名）",
    # 一页怎么搭
    "settings_page_shell": "test_insert_page_slot_lands_below_the_title",
    "settings_section": "test_insert_page_slot_lands_below_the_title",
    "form_group": "test_form_group_has_no_face_of_its_own",
    "FormGroup": "form_group 造出来的容器（上面那条顺带断言类型）",
    "is_standard_shell": "test_slot_helpers_refuse_a_page_without_layout",
    "insert_page_slot": "test_insert_page_slot_lands_below_the_title",
    "insert_page_tail": "test_insert_page_tail_skips_trailing_stretch",
    # 表单
    "form_row": "test_form_row_keeps_the_label_column_aligned",
    "form_row_stacked": "test_form_row_stacked_puts_the_label_on_top",
    "form_label": "test_form_row_keeps_the_label_column_aligned",
    "note": "test_note_does_not_grow_with_the_window",
    "choice_hint": "test_choice_hint_rejects_a_missing_key",
    # 取值控件
    "ChoiceSelector": "test_public_widgets_construct_and_paint",
    "ChoiceBar": "test_public_widgets_construct_and_paint",
    "ProfileSelector": "test_public_widgets_construct_and_paint",
    "ComboField": "test_public_widgets_construct_and_paint",
    "NumberField": "test_public_widgets_construct_and_paint",
    "DecimalField": "test_public_widgets_construct_and_paint",
    "Switch": "test_public_widgets_construct_and_paint",
    "check_field": "test_check_field_hands_back_the_new_value",
    "search_field": "test_search_field_hands_back_the_raw_text",
    "apply_combo_style": "test_apply_combo_style_is_a_no_op_on_a_plain_combo",
    # 行
    "StatusRow": "test_public_widgets_construct_and_paint",
    "RecordRow": "test_public_widgets_construct_and_paint",
    "HoverRow": "test_public_widgets_construct_and_paint",
    "EmptyState": "test_public_widgets_construct_and_paint",
    # 按钮
    "tool_button": "test_apply_button_tier_writes_both_axes",
    "apply_button_tier": "test_apply_button_tier_writes_both_axes",
    "MenuButton": "test_public_widgets_construct_and_paint",
    # 反馈
    "FeedbackBar": "test_feedback_bar_walks_its_five_tones",
    # 结果回传
    "relay_action_result": "test_relay_action_result_delivers_to_host",
    "present_to_host": "test_relay_action_result_delivers_to_host",
    "defer_result_to_host": "test_defer_result_to_host_waits_one_turn",
    "clear_layout": "test_clear_layout_takes_widgets_in_sub_layouts_too",
    # 编辑屏
    "EditorShell": "test_public_widgets_construct_and_paint",
    "EditScreen": "test_edit_screen_owns_its_action_bar",
    "EditStack": "test_edit_stack_swaps_screens",
    "EditorActionBar": "test_edit_screen_owns_its_action_bar",
    # 底层自绘与文本
    "LogoWidget": "test_logo_widget_draws_bars_at_its_size",
    "HStatChip": "test_public_widgets_construct_and_paint",
    "ElideLabel": "test_public_widgets_construct_and_paint",
    "ModeBadge": "test_public_widgets_construct_and_paint",
    "Card": "test_public_widgets_construct_and_paint",
    "Divider": "test_public_widgets_construct_and_paint",
    "apply_text_role": "test_apply_text_role_rejects_a_typo",
    # 图标与入场
    "glyph_pixmap": "test_glyphs_and_tray_pixmaps_paint",
    "draw_glyph": "test_glyphs_and_tray_pixmaps_paint",
    "badge_glyph_pixmap": "test_glyphs_and_tray_pixmaps_paint",
    "draw_badge_glyph": "test_glyphs_and_tray_pixmaps_paint",
    "paint_bar_glyph": "test_glyphs_and_tray_pixmaps_paint",
    "make_tray_pixmap": "test_glyphs_and_tray_pixmaps_paint",
    "tray_base_pixmap": "test_glyphs_and_tray_pixmaps_paint",
    "tray_render_dpr": "test_glyphs_and_tray_pixmaps_paint",
    "play_entrance": "test_play_entrance_is_a_no_op_offscreen",
    # 菜单
    "CandidateMenu": "test_candidate_menu_picks_by_index",
    "qt_alive": "test_qt_alive_tells_a_dead_widget_from_a_live_one",
}


def _public_names() -> set[str]:
    return {n for n in ui_kit.__all__ if not n.startswith("_")}


def test_every_public_name_has_a_place_in_this_file():
    """``__all__`` 的公开子集必须与 ``_COVERED`` 的键**逐字相等**。

    这条不是「覆盖率报表」，是**防腐**：往组件盒加一件却不写验法，今天没人发现，
    半年后就变成「库里有、没人知道能用」的那五个件（本篇的由来）。
    """
    public = _public_names()
    assert not (public - set(_COVERED)), (
        "组件盒新增了公开件却没在这里登记验法：%s" % sorted(public - set(_COVERED)))
    assert not (set(_COVERED) - public), (
        "这里登记了 ui_kit 已经不导出的名字（改名/删件后没跟着改）：%s"
        % sorted(set(_COVERED) - public))


# ──────────────────────────────────────────────
# 2 冒烟：造得出来，也画得出来
# ──────────────────────────────────────────────

_WIDGETS: dict[str, "object"] = {
    "LogoWidget": lambda p: ui_kit.LogoWidget(parent=p),
    "HStatChip": lambda p: ui_kit.HStatChip("运行中", "idle", parent=p),
    "ElideLabel": lambda p: ui_kit.ElideLabel("很长很长的一条标题" * 6, p),
    "Switch": lambda p: ui_kit.Switch(parent=p),
    "ModeBadge": lambda p: ui_kit.ModeBadge("自动", parent=p),
    "Card": lambda p: ui_kit.Card(parent=p),
    "Divider": lambda p: ui_kit.Divider(parent=p),
    "EmptyState": lambda p: ui_kit.EmptyState("还没有记录", "先加一条", p),
    "HoverRow": lambda p: ui_kit.HoverRow(lambda: None, parent=p),
    "RecordRow": lambda p: ui_kit.RecordRow(
        title="条目", subtitle="副标题", icon=IconName.SHIELD, parent=p),
    "StatusRow": lambda p: ui_kit.StatusRow(
        IconName.SHIELD, "状态", controls=(ui_kit.ModeBadge("自动", parent=p),), parent=p),
    "FeedbackBar": lambda p: ui_kit.FeedbackBar(parent=p),
    "MenuButton": lambda p: ui_kit.MenuButton("下拉", p),
    "ComboField": lambda p: ui_kit.ComboField(parent=p),
    "NumberField": lambda p: ui_kit.NumberField(parent=p),
    "DecimalField": lambda p: ui_kit.DecimalField(parent=p),
    "ProfileSelector": lambda p: ui_kit.ProfileSelector(["拼音", "ABC"], "拼音", parent=p),
    "ChoiceSelector": lambda p: ui_kit.ChoiceSelector(
        [("a", "甲"), ("b", "乙")], "a", parent=p),
    "ChoiceBar": lambda p: ui_kit.ChoiceBar(
        [("a", "甲"), ("b", "乙")], "a", parent=p,
        hints={"a": "甲的释义", "b": "乙的释义"}),
    "EditorShell": lambda p: ui_kit.EditorShell("编辑标题", p),
    "EditScreen": lambda p: ui_kit.EditScreen(title="编辑", parent=p),
}


@pytest.mark.parametrize("name", sorted(_WIDGETS))
def test_public_widgets_construct_and_paint(qtbot, name):
    """逐个构造并渲染一次：``show()`` 之后再 ``grab()``。

    ``grab()`` 会真的走一遍 ``paintEvent`` —— 只 import、只构造的冒烟放得过
    「画的时候才炸」的那一类（自绘件全是这种）。
    """
    host = QWidget()
    qtbot.addWidget(host)
    widget = _WIDGETS[name](host)
    assert widget is not None, name
    widget.show()
    assert not widget.grab().isNull(), "%s 画不出来" % name


def test_status_row_reads_its_icon_tone():
    """``RecordRow`` 的 ``icon_tone`` 必须取自 ``ICON_TONES``；写错当场报错。"""
    for tone in ui_kit.ICON_TONES:
        ui_kit.RecordRow(title="t", icon=IconName.SHIELD, icon_tone=tone)
    assert ui_kit.ICON_TONE_DEFAULT in ui_kit.ICON_TONES


# ──────────────────────────────────────────────
# 3 五个曾经零引用的件
# ──────────────────────────────────────────────

def test_logo_widget_draws_bars_at_its_size(qtbot):
    """``LogoWidget``：尺寸就是它自己的 ``size``，且真画出了不透明像素。"""
    w = ui_kit.LogoWidget(size=32)
    qtbot.addWidget(w)
    w.show()
    pm = w.grab()
    assert (pm.width(), pm.height()) == (32, 32)
    img = pm.toImage()
    assert any(img.pixelColor(x, y).alpha() > 0
               for x in range(32) for y in range(32)), "画出来是一张全透明的图"


def test_apply_button_tier_writes_both_axes(qtbot):
    """档位两条轴（``tier`` 属性 ＋ 钉高）必须一起写；没登记的档名当场报错。"""
    btn = ui_kit.tool_button("确定", "primary", size="inline")
    qtbot.addWidget(btn)
    assert btn.property(metrics.TIER_PROPERTY) == "inline"
    assert btn.height() == metrics.BUTTON_HEIGHTS["inline"]
    # 直接对一枚裸按钮用（`MenuButton` 走的就是这条路）
    plain = QPushButton("下拉")
    assert ui_kit.apply_button_tier(plain, "chip") == metrics.BUTTON_HEIGHTS["chip"]
    assert plain.height() == metrics.BUTTON_HEIGHTS["chip"]
    with pytest.raises(ValueError):
        ui_kit.apply_button_tier(QPushButton("x"), "huge")


def _host_class():
    """带 ``present_action_result`` 的最小宿主（鸭子类型：有这个方法就能接结果）。"""
    class _Host(QWidget):
        def __init__(self):
            super().__init__()
            self.got: list = []

        def present_action_result(self, result):
            self.got.append(result)
    return _Host


def test_relay_action_result_delivers_to_host(qtbot):
    """当场中继：回调返回的结局要落到**最近的**宿主上，且 ``None`` 也照送。"""
    host = _host_class()()
    qtbot.addWidget(host)
    child = QWidget(host)
    outcome = ActionResult(ActionOutcome.FAILED, "没连上")

    ui_kit.relay_action_result(child, lambda: outcome)
    assert host.got == [outcome]

    # 返回值不是 ActionResult（少写一个 return 的 `Signal.emit` 会返回 True）⇒ 归一成 None
    ui_kit.relay_action_result(child, lambda: True)
    assert host.got[-1] is None


def test_defer_result_to_host_waits_one_turn(qtbot):
    """整行点击走这条：**晚一拍**才回访宿主（不能就在控件的事件栈里）。"""
    host = _host_class()()
    qtbot.addWidget(host)
    child = QWidget(host)
    outcome = ActionResult(ActionOutcome.OK, "已切换")

    ui_kit.defer_result_to_host(child, outcome)
    assert host.got == [], "当场就回访了 —— 这条存在的理由就是别当场"
    qtbot.waitUntil(lambda: host.got == [outcome], timeout=1000)


def test_present_to_host_is_a_silent_no_op_without_a_host(qtbot):
    """往上找不到宿主 ⇒ 什么都不发生（不抛、不记错）。"""
    orphan = QWidget()
    qtbot.addWidget(orphan)
    ui_kit.present_to_host(orphan, ActionResult(ActionOutcome.OK, "没人接"))


# ──────────────────────────────────────────────
# 4 一页怎么搭：两个插槽口
# ──────────────────────────────────────────────

def test_insert_page_slot_lands_below_the_title(qtbot):
    """框架自动段（状态机）插在**标题之下、组件内容之上**，不是页首也不是页尾。"""
    page, lay = ui_kit.settings_page_shell("组件名")
    qtbot.addWidget(page)
    card, _body = ui_kit.settings_section("组件自己的分组")
    lay.addWidget(card)

    marker = QWidget()
    assert ui_kit.insert_page_slot(page, marker) is True
    assert lay.indexOf(marker) == ui_kit.PAGE_SLOT == 1
    assert lay.itemAt(0).widget().text() == "组件名", "标题被挤下去了"


def test_insert_page_tail_skips_trailing_stretch(qtbot):
    """页尾插槽要**跳过末尾连续的 stretch** —— 否则有 stretch 的页里框架区长在页面中部。"""
    page, lay = ui_kit.settings_page_shell("组件名")
    qtbot.addWidget(page)
    card, _body = ui_kit.settings_section("组件自己的分组")
    lay.addWidget(card)
    lay.addStretch(1)

    marker = QWidget()
    assert ui_kit.insert_page_tail(page, marker) is True
    assert lay.indexOf(marker) == lay.count() - 2, "没插在 stretch 之上"
    assert lay.itemAt(lay.count() - 1).spacerItem() is not None, "末尾那根 stretch 被顶掉了"


def test_slot_helpers_refuse_a_page_without_layout():
    """不是标准外壳 ⇒ 两个口都返回 ``False``（调用方自行兜底），不许静默吞掉。"""
    bare = QWidget()
    assert ui_kit.is_standard_shell(bare) is False
    assert ui_kit.insert_page_slot(bare, QWidget()) is False
    assert ui_kit.insert_page_tail(bare, QWidget()) is False


def test_form_group_has_no_face_of_its_own(qtbot):
    """``form_group`` 只靠标题与间距分段：它**不铺底**（面由外面那层容器给）。"""
    host = QWidget()
    qtbot.addWidget(host)
    group, body = ui_kit.form_group("第一组", host)
    assert isinstance(group, ui_kit.FormGroup)
    assert group.findChildren(ui_kit.Card) == [], "面里有面"
    assert body.count() == 1, "只有标题那一行"
    _second, body2 = ui_kit.form_group("第二组", host, divider_above=True)
    assert body2 is not None


# ──────────────────────────────────────────────
# 5 表单与取值控件
# ──────────────────────────────────────────────

def test_form_row_keeps_the_label_column_aligned(qtbot):
    """标签定宽（``metrics.LABEL_W``）⇒ 同一页里各行的控件左缘对齐。"""
    host = QWidget()
    qtbot.addWidget(host)
    row = ui_kit.form_row("端口", ui_kit.NumberField(), host)
    assert row.count() == 2
    label = row.itemAt(0).widget()
    assert label.text() == "端口"
    assert label.maximumWidth() == metrics.LABEL_W
    assert label.wordWrap(), "长文案要把这一行撑宽，而不是被截断"


def test_form_row_stacked_puts_the_label_on_top(qtbot):
    """长标签用另一种排法：标签在上、控件在下（不是 `form_row` 的特例）。"""
    host = QWidget()
    qtbot.addWidget(host)
    stacked = ui_kit.form_row_stacked(
        "监控网卡 / 匹配网关（都写 = 同时满足才命中）", ui_kit.ComboField(), host)
    lay = stacked.layout()
    assert lay.count() == 2
    assert lay.itemAt(0).widget().text().startswith("监控网卡")


def test_note_does_not_grow_with_the_window(qtbot):
    """小字说明竖直方向钉住：窗口变高不该把它撑成一大块空白（折行高度仍按宽度算）。"""
    host = QWidget()
    qtbot.addWidget(host)
    label = ui_kit.note("这是一句说明", host)
    assert label.wordWrap()
    policy = label.sizePolicy().verticalPolicy()
    assert policy in (QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)


def test_choice_hint_rejects_a_missing_key(qtbot):
    """释义以**取值**（itemData）为键；缺一个当场报错（空释义比没有更糟）。"""
    host = QWidget()
    qtbot.addWidget(host)
    combo = ui_kit.ComboField(host)
    for value, label in (("a", "甲"), ("b", "乙")):
        combo.addItem(label, value)
    combo.setCurrentIndex(0)

    hint = ui_kit.choice_hint(combo, {"a": "甲的释义", "b": "乙的释义"}, host)
    assert hint.text() == "甲的释义"
    # 切到「乙」但释义表里没有它 ⇒ 当场报错，而不是留一句空话
    combo.setCurrentIndex(1)
    with pytest.raises(KeyError):
        ui_kit.choice_hint(combo, {"a": "只写了一个"}, host)


def test_search_field_hands_back_the_raw_text(qtbot):
    """「输入即筛选」的唯一口：回调拿**原文**（不 strip、不转小写），口径归调用方。"""
    seen: list[str] = []
    field = ui_kit.search_field("搜索", on_changed=seen.append)
    qtbot.addWidget(field)
    assert field.isClearButtonEnabled()
    field.setText("  Office  ")
    assert seen == ["  Office  "]


def test_check_field_hands_back_the_new_value(qtbot):
    """勾选框由 Qt 自己翻并 emit；回调拿到的就是**翻转后**的新值。"""
    seen: list[bool] = []
    box = ui_kit.check_field("开机自启", on_toggled=seen.append)
    qtbot.addWidget(box)
    box.setChecked(True)
    assert seen == [True]
    assert box.isChecked()


def test_apply_combo_style_is_a_no_op_on_a_plain_combo(qtbot):
    """统一下拉样式：给一枚裸 ``QComboBox`` 套上，不抛且样式表非空。"""
    combo = QComboBox()
    qtbot.addWidget(combo)
    ui_kit.apply_combo_style(combo)
    assert combo.styleSheet()


def test_apply_text_role_rejects_a_typo(qtbot):
    """角色名拼错当场抛错 —— 静默跳过会得到「颜色不对但没人报错」的界面。"""
    label = ui_kit.form_label("标签")
    qtbot.addWidget(label)
    ui_kit.apply_text_role(label, "caption")
    assert "font-size" in label.styleSheet()
    with pytest.raises(KeyError):
        ui_kit.apply_text_role(label, "captionn")
    with pytest.raises(KeyError):
        ui_kit.apply_text_role(label, "caption", color="nope_not_a_token")


# ──────────────────────────────────────────────
# 6 反馈、结果清理、编辑屏、菜单
# ──────────────────────────────────────────────

def test_feedback_bar_walks_its_five_tones(qtbot):
    """五档各一次：``tone()`` 跟着档走，``ask`` 起来时 ``is_pending()``。"""
    bar = ui_kit.FeedbackBar()
    qtbot.addWidget(bar)
    for tone, text in (("info", "已保存"), ("warn", "还差一步"),
                       ("error", "没成"), ("hint", "先勾一项")):
        getattr(bar, tone)(text)
        assert (bar.tone(), bar.text()) == (tone, text)
    bar.ask("要恢复默认吗？", "恢复", lambda: None, confirm_kind="danger")
    assert bar.is_pending() and bar.tone() == "ask"
    bar.cancel()
    assert bar.is_pending() is False


def test_clear_layout_takes_widgets_in_sub_layouts_too(qtbot):
    """重画一整组：子布局里的控件也要摘干净（只删 `item.widget()` 会留下漂浮控件）。"""
    host = QWidget()
    qtbot.addWidget(host)
    lay = QVBoxLayout(host)
    label = ui_kit.form_label("标签")
    control = QPushButton("控件")
    lay.addLayout(ui_kit.form_row("标签", control, host))
    lay.addWidget(label)
    assert len(host.findChildren(QPushButton)) == 1

    ui_kit.clear_layout(lay)
    assert lay.count() == 0
    assert host.findChildren(QWidget) == [], "子布局里的控件还挂在宿主上"


def test_edit_screen_owns_its_action_bar(qtbot):
    """编辑屏把动作行的全部成员交出来（宿主壳接管后重排用）。"""
    screen = ui_kit.EditScreen(title="编辑", save_label="保存")
    qtbot.addWidget(screen)
    group, body = screen.add_group("基本信息")
    body.addLayout(ui_kit.form_row("名称", ui_kit.search_field(), group))
    save = screen.add_action("保存", lambda: None)
    drop = screen.add_destructive("删除", lambda: None)
    assert save.text() == "保存" and drop.text() == "删除"

    bar = screen.take_action_bar()
    assert isinstance(bar, ui_kit.EditorActionBar)
    assert bar.primaries and bar.destructive and bar.feedback is not None


def test_edit_stack_swaps_screens(qtbot):
    """列表屏 ⇄ 编辑屏：``push`` 进去、``back`` 回来，信号各发一次。"""
    list_page = QWidget()
    stack = ui_kit.EditStack(list_page)
    qtbot.addWidget(stack)
    seen: list = []
    stack.screen_changed.connect(lambda *_: seen.append(stack.current()))
    screen = ui_kit.EditScreen(title="编辑")
    stack.push(screen)
    assert stack.current() is screen
    stack.back()
    assert stack.current() is list_page
    assert len(seen) == 2


def test_candidate_menu_picks_by_index(qtbot):
    """候选菜单：``pick`` 返回那一项并 emit；``MenuItem`` 是冻结值对象（不塞闭包）。"""
    picked: list[MenuItem] = []
    items = (MenuItem("office", lambda: None, "10.99.1.0/24"),
             MenuItem("home", lambda: None, "192.168.11.0/24"))
    menu = ui_kit.CandidateMenu(items, current="office", on_picked=picked.append)
    qtbot.addWidget(menu)
    assert menu.pick(1) is items[1]
    assert picked == [items[1]]


# ──────────────────────────────────────────────
# 7 图标、入场、判活
# ──────────────────────────────────────────────

def test_glyphs_and_tray_pixmaps_paint(qtbot):
    """字形三个出口都真画一次：pixmap 版、自绘画布版、托盘底图。"""
    host = QWidget()
    qtbot.addWidget(host)
    color = QColor("#3366ff")
    canvas = QPixmap(64, 64)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    try:
        ui_kit.paint_bar_glyph(painter, QRectF(0, 0, 64, 24), color)
        ui_kit.draw_glyph(painter, IconName.SHIELD, QRectF(0, 24, 24, 24), color)
        ui_kit.draw_badge_glyph(painter, IconName.SHIELD, StateLevel.OK,
                                QRectF(24, 24, 12, 12), color)
    finally:
        painter.end()

    assert not ui_kit.make_tray_pixmap(22).isNull()
    assert not ui_kit.tray_base_pixmap(IconName.SHIELD, 22, color).isNull()
    assert not ui_kit.glyph_pixmap(host, IconName.SHIELD, 18, color).isNull()
    assert not ui_kit.badge_glyph_pixmap(host, IconName.SHIELD, StateLevel.OK,
                                        18, color).isNull()
    assert ui_kit.tray_render_dpr() > 0


def test_play_entrance_is_a_no_op_offscreen(qtbot):
    """入场动画只在原生 cocoa 生效；离屏 / Windows 一律 no-op（不许在测试里真起动画）。"""
    win = QWidget()
    qtbot.addWidget(win)
    ui_kit.play_entrance(win)


def test_qt_alive_tells_a_dead_widget_from_a_live_one(qtbot):
    """``qt_alive``：``None`` 不算活；删掉 C++ 本体的也不算活（晚一拍回执前靠它判）。"""
    live = QWidget()
    qtbot.addWidget(live)
    assert ui_kit.qt_alive(live) is True
    assert ui_kit.qt_alive(None) is False

    dying = QWidget()
    dying.deleteLater()
    qtbot.wait(10)
    assert ui_kit.qt_alive(dying) is False
