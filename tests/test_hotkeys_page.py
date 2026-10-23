"""tests/test_hotkeys_page：设置窗「快捷键」页（2026-09-23 新增）。

覆盖三件事：**目录合成**（三类来源各是什么形状、谁能删）、**页面行为**（搜索 /
两个筛选 / 开关 / 改键）、**改键的两条失败路径**（撞车不落盘、挂不上要说话）。

桩只补「页面真正消费」的口（``get`` / ``set_hotkey`` / ``remove_hotkeys`` 与
``hotkeys()`` / ``components()``）—— 真实 ``Config`` / ``ContributionRegistry`` 都有，
桩不给就会测到一个不存在的契约（本仓反复吃过这条）。
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from barkit.contribution.specs import ComponentSpec, HotkeySpec, IconName
from barkit.core.hotkey_command import CommandResult
from barkit.shell.hotkey_capture import ComboButton, event_combo
from barkit.shell.hotkey_catalog import (
    FEEDBACK_NONE,
    FEEDBACK_NOTIFY,
    FRAMEWORK_ACTIONS,
    REBIND_BOUND,
    REBIND_FAILED,
    REBIND_UNBOUND,
    SOURCE_CUSTOM,
    SOURCE_FRAMEWORK,
    SOURCE_PLUGIN,
    CustomSpec,
    collect,
    display_combo,
    next_custom_name,
    remove_custom,
    write_combo,
    write_custom,
    write_feedback,
)
from barkit.shell.hotkey_editor import HotkeyEditor
from barkit.shell.hotkeys_page import HotkeysPage
from barkit.shell.ui_kit import Card, HoverRow, RecordRow, Switch, _SectionHeader


class _Cfg:
    """配置桩：与真实 Config 同签名的那几个口，并记录写侧动作。

    **自建条目与通知开关是真存下来的**（不是各返回一个空 dict 了事）：那一屏的
    新增 / 保存 / 删除全靠这三个口，桩不落东西就成了「写入了但读不回来」的假测试。
    """

    def __init__(self, values=None, customs=None):
        self.values = dict(values or {})
        self.customs = dict(customs or {})
        self.feedback: dict[str, str] = {}
        self.writes: list[tuple[str, str]] = []
        self.removes: list[str] = []

    def get(self, path, default=None):
        return self.values.get(path, default)

    def set_hotkey(self, name, combo):
        self.writes.append((name, combo))
        self.values[f"hotkeys.{name}"] = combo

    def remove_hotkeys(self, names):
        self.removes.extend(names)
        for name in names:
            self.values.pop(f"hotkeys.{name}", None)

    def custom_hotkeys(self):
        return dict(self.customs)

    def set_custom_hotkey(self, name, spec):
        self.customs[name] = dict(spec)

    def remove_custom_hotkey(self, name):
        self.customs.pop(name, None)

    def hotkey_feedback(self, name, default="none"):
        return self.feedback.get(name, default)

    def set_hotkey_feedback(self, name, value, default="none"):
        if value == default:
            self.feedback.pop(name, None)
        else:
            self.feedback[name] = value


class _Stub:
    """贡献注册中心桩：只给页面消费的两个门面。"""

    def __init__(self, hotkeys=(), components=()):
        self._hotkeys = list(hotkeys)
        self._components = list(components)

    def hotkeys(self):
        return self._hotkeys

    def components(self):
        return self._components


def _spec(name="recognize", label="截图识别", default="ctrl+alt+o", plugin_id="recognize"):
    return HotkeySpec(name=name, label=label, default_combo=default, plugin_id=plugin_id)


def _page(qtbot, *, hotkeys=(), components=(), values=None, rebind=True, conflict=None,
          actions=FRAMEWORK_ACTIONS, customs=None, run_command=None, failure_of=None):
    cfg = _Cfg(values, customs)
    page = HotkeysPage(
        config=cfg,
        contributions=_Stub(hotkeys, components),
        actions=actions,
        on_rebind=lambda _name: rebind,
        conflict_of=conflict,
        run_command=run_command,
        failure_of=failure_of,
    )
    qtbot.addWidget(page)
    return page, cfg


def _buttons(page, text):
    """按文案找按钮（这一页的按钮没有 objectName，文案就是它的身份）。"""
    QApplication.processEvents()
    return [b for b in page.findChildren(QPushButton) if b.text() == text]


def _pills(page):
    """当前**真的还在**列表里的键位药丸。

    必须先 ``processEvents``：换列表时旧行走 ``deleteLater()``（异步销毁），不跑一遍
    事件循环 ``findChildren`` 会把已经拆掉的行也算进来 —— 数量永远是第一次画的那个。
    """
    QApplication.processEvents()
    return page.findChildren(ComboButton)


# ---------- 目录：显示与写盘口径 ----------


def test_display_combo_uses_system_glyphs_and_order():
    assert display_combo("ctrl+alt+o") == "⌃⌥O"
    assert display_combo("cmd+shift+b") == "⇧⌘B"
    assert display_combo("") == "未设置"
    assert display_combo("cmd+comma") == "⌘,"


def test_write_combo_drops_the_key_when_it_equals_the_factory_value():
    """等于出厂值 ⇒ **删键**（默认值不落盘），而不是把默认值抄一遍进用户配置。"""
    cfg = _Cfg()
    write_combo(cfg, "recognize", "ctrl+alt+o", "ctrl+alt+o")
    assert cfg.writes == [] and cfg.removes == ["recognize"]


def test_write_combo_keeps_an_empty_value_because_it_means_disabled():
    """空串 = 用户明确停用，**必须落盘**：它与「没改过」的区别只靠「键在不在」表达。"""
    cfg = _Cfg()
    write_combo(cfg, "recognize", "", "ctrl+alt+o")
    assert cfg.writes == [("recognize", "")] and cfg.removes == []


def test_builtin_entries_are_not_deletable(qtbot):
    page, _cfg = _page(qtbot, hotkeys=[_spec()])
    entries = page._entries
    assert [e.source for e in entries] == [SOURCE_FRAMEWORK, SOURCE_FRAMEWORK, SOURCE_PLUGIN]
    assert all(not e.deletable for e in entries)


def test_custom_entries_are_deletable():
    from barkit.shell.hotkey_catalog import HotkeyEntry

    assert HotkeyEntry("x", "x", SOURCE_CUSTOM, "我的", "", "cmd+alt+x").deletable


# ---------- 键位录制 ----------


def test_event_combo_reads_modifiers_by_macos_names():
    """Qt 在 macOS 把 ⌘ 报成 ControlModifier、⌃ 报成 MetaModifier —— 名字按 macOS 口径。"""
    ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_O, Qt.KeyboardModifier.ControlModifier)
    assert event_combo(ev) == "cmd+o"
    ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_O, Qt.KeyboardModifier.MetaModifier)
    assert event_combo(ev) == "ctrl+o"


def test_event_combo_plain_escape_cancels_but_modified_escape_is_a_combo():
    plain = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    assert event_combo(plain) == ""
    with_cmd = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.ControlModifier
    )
    assert event_combo(with_cmd) == "cmd+esc"


def test_event_combo_ignores_keys_that_cannot_be_registered():
    ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_MediaPlay, Qt.KeyboardModifier.NoModifier)
    assert event_combo(ev) is None


# ---------- 页面：列表与筛选 ----------


def test_page_lists_builtin_and_plugin_hotkeys(qtbot):
    page, _cfg = _page(qtbot, hotkeys=[_spec()])
    # 两条内置 + 一条组件声明
    assert [e.title for e in page._entries] == ["打开面板", "打开设置", "截图识别"]
    assert len(_pills(page)) == 3


def test_component_without_label_falls_back_to_the_component_name(qtbot):
    """label 是可选的：没写就把**组件名**当标题，别把配置键名摆给用户看。"""
    page, _cfg = _page(
        qtbot,
        hotkeys=[_spec(name="recognize", label="", plugin_id="recognize")],
        components=[ComponentSpec(plugin_id="recognize", name="识别")],
    )
    assert page._entries[-1].title == "识别"


def test_search_matches_both_the_glyph_and_the_raw_combo(qtbot):
    page, _cfg = _page(qtbot, hotkeys=[_spec()])
    page._search.setText("⌃⌥O")
    assert len(_pills(page)) == 1
    page._search.setText("ctrl+alt+o")
    assert len(_pills(page)) == 1
    page._search.setText("没有这个")
    assert _pills(page) == []


def test_state_and_source_filters_narrow_the_list(qtbot):
    page, cfg = _page(qtbot, hotkeys=[_spec()], values={"hotkeys.recognize": ""})
    # 识别那条被停用；「打开设置」出厂就没占键 ⇒ 真正启用着的只有「打开面板」
    page._state_box.setCurrentIndex(1)
    assert len(_pills(page)) == 1
    page._state_box.setCurrentIndex(2)
    assert len(_pills(page)) == 2
    # 来源选「组件」⇒ 只剩识别（停用态无关）
    page._state_box.setCurrentIndex(0)
    page._source_box.setCurrentIndex(2)
    assert len(_pills(page)) == 1
    assert cfg.values.get("hotkeys.recognize") == ""


def test_list_screen_rows_and_feedback_share_one_section_card(qtbot):
    """行与反馈槽同属一张分区卡（2026-09-24：改前三者全裸在页面背景上）。

    判据取「反馈槽与行**同一张卡**」而不是「这一页里有卡」：后者在「卡里塞了别的、
    列表留在卡外」时也成立 —— 那正是要防的那一版。往上找卡而不是看直接父对象：
    卡的内容区中间还隔着一层容器，直接父对象是布局给的，不是分组给的。
    """
    page, _cfg = _page(qtbot, hotkeys=[_spec()])
    QApplication.processEvents()

    def card_of(widget):
        while widget is not None:
            if isinstance(widget, Card):
                return widget
            widget = widget.parentWidget()
        return None

    rows = page.findChildren(HoverRow)
    assert rows, "列表里该有行"
    host = card_of(page._fb)
    assert host is not None, "反馈槽不在任何分区卡里"
    assert host is card_of(rows[0]), "反馈槽与行不在同一张卡里"
    # 这张卡带标题栏（分区卡的那一款）：这一组是什么，卡自己说得出来
    assert [h.title_text for h in host.findChildren(_SectionHeader)] == ["键位"]


# ---------- 行的长相：与主面板的组件行同源 ----------


def test_row_is_built_on_the_panel_row_skeleton(qtbot):
    """快捷键行 = **面板组件行那份骨架**（``ui_kit.RecordRow``），不是另造一套长相。

    判的是**骨架本身**，不是「长得像」—— 后者在有人重抄一份实现之后照样成立，
    而重抄正是这条要防的（面板的状态行与自绘行曾各写一遍，改一处就漂一半）。
    """
    from barkit.shell.panel import _StatusRowView
    from barkit.shell.ui_kit import RecordRow, StatusRow

    page, _cfg = _page(qtbot, hotkeys=[_spec()])
    rows = page.findChildren(RecordRow)
    assert rows, "列表行不建在共用骨架上"
    assert issubclass(_StatusRowView, RecordRow), "面板行脱离了这份骨架"
    assert issubclass(StatusRow, RecordRow), "面板自绘行脱离了这份骨架"


def test_source_is_a_title_badge_and_the_subtitle_gives_the_full_text(qtbot):
    """来源（框架 / 组件名 / 我的）是**主标题后面那枚徽标**，副标题只说它是干什么的。

    两条一起判：徽标上写的就是归属名（不是「组件的名字挤在副标题开头、用「·」连成
    一句话」）；副标题的**悬停提示**必须留着完整那句 —— 它是单行省略的窄条，截掉的
    部分只能从提示里看回来（只判显示文本等于说明是判不出来的：那是原句的前半截）。
    """
    page, _cfg = _page(qtbot, hotkeys=[_spec(label="截图识别")], components=(
        ComponentSpec(plugin_id="recognize", name="识别"),
    ))
    rows = page.findChildren(RecordRow)
    assert len(rows) == len(page._entries) == 3
    for row, entry in zip(rows, page._entries):
        assert row._title_badge is not None, f"「{entry.title}」的标题行里没有来源徽标"
        assert row._title_badge.text() == entry.owner
        assert row._sub.toolTip() == entry.description, "副标题的完整那句没进悬停提示"
        assert "·" not in row._sub.toolTip(), "副标题里还留着「来源 · 说明」的老写法"


def test_icon_shape_says_who_and_colour_says_which_group(qtbot):
    """行首图标：**形状 = 是谁给的，颜色 = 哪一类**（2026-09-24）。

    「是谁」那一半：组件声明的键位画该组件自己的主图标（这行是识别给的，就画识别那枚），
    框架内置画齿轮、用户自建画键盘 —— 一眼看出这条打哪儿来，不必去读标题后那枚徽标。

    「哪一类」那一半：三档色由 ``SOURCE_TONES`` 定。这一半单独判是因为它**会静默退化** ——
    三档写成同一个色值时页面对得很，只是「颜色区分来源」这件事没发生。

    图标的名字补在图标盒的悬停提示里（图标只分得出类别，说不出具体是谁）。
    """
    page, _cfg = _page(
        qtbot,
        hotkeys=[_spec(label="截图识别")],
        components=(ComponentSpec(plugin_id="recognize", name="识别", icon=IconName.SCAN),),
        customs={"custom.1": {"title": "打开工作台", "command": "open -a workbench"}},
        actions=FRAMEWORK_ACTIONS[:1],          # 框架那两条里留一条，三档各一行看得清
    )
    rows = {row._name.text(): row for row in page.findChildren(RecordRow)}
    assert set(rows) == {"打开面板", "截图识别", "打开工作台"}, f"三类来源各一条，实际 {set(rows)}"

    assert rows["打开面板"]._icon_name is IconName.GEAR, "框架内置没画齿轮"
    assert rows["截图识别"]._icon_name is IconName.SCAN, "组件那条没画该组件自己的图标"
    assert rows["打开工作台"]._icon_name is IconName.KEYBOARD, "自建那条没画键盘"

    tones = {name: rows[name]._icon_tone for name in rows}
    assert tones == {"打开面板": "neutral", "截图识别": "accent", "打开工作台": "solid"}, \
        f"三档来源色退了（{tones}）"

    assert rows["截图识别"]._ico_box.toolTip() == "来源：识别", "图标盒上没写来源名"


def _min_widths(qtbot, title: str, description: str) -> "tuple[int, int]":
    """一段文案下的（页面最小宽, 行最小宽）。

    **两个都要量**：这一页的最小宽其实是被上面那条工具条顶着的（搜索框 150 ＋ 两个定宽 96
    的下拉 ＋ 新增 58 ＋ 间距 ⇒ 424，加卡与页的内边距 484），行自己只要 344。只看页面会漏掉
    「行的最小宽涨了、但还没超过工具条」那一档 —— 那一档同样会把行尾那几枚控件挤出视口，
    而这一页的横向滚动是关的。
    """
    from barkit.shell.hotkey_catalog import FrameworkAction

    page, _cfg = _page(qtbot, actions=(FrameworkAction(
        name="framework.panel", title=title, description=description,
        default_combo="cmd+shift+b",
    ),))
    QApplication.processEvents()
    return (page.minimumSizeHint().width(),
            page.findChildren(RecordRow)[0].minimumSizeHint().width())


def test_long_copy_does_not_widen_the_page(qtbot):
    """超长标题 / 超长说明都不许把这一页顶宽。

    设置页主区的横向滚动是**关**的（``settings._scrolled``）⇒ 页面一旦比视口宽，
    行尾那几枚控件就被裁掉、且没有滚动条可救。判据取「换一段超长文案，宽度不变」——
    只量一次宽度什么也证明不了（文案短的时候本来就不宽）；页面与**行**两个都量，理由见
    ``_min_widths``。
    """
    short_page, short_row = _min_widths(qtbot, "打开面板", "开关面板")
    long_page, long_row = _min_widths(qtbot, "打" * 40, "说" * 200)
    assert long_row == short_row, "行被长文案顶宽了（行里的标签不可压）"
    assert long_page == short_page, "长文案把页面顶宽了"


# ---------- 交互：开关与改键 ----------


def test_toggling_off_writes_an_empty_combo(qtbot):
    page, cfg = _page(qtbot, hotkeys=[_spec()], values={"hotkeys.recognize": "ctrl+alt+o"})
    sw = page.findChildren(Switch)[-1]
    sw.clicked.emit()
    assert cfg.writes == [("recognize", "")]


def test_toggling_on_drops_the_key_back_to_the_factory_value(qtbot):
    page, cfg = _page(qtbot, hotkeys=[_spec()], values={"hotkeys.recognize": ""})
    page.findChildren(Switch)[-1].clicked.emit()
    assert cfg.removes == ["recognize"]


def test_toggling_on_without_a_factory_combo_refuses_and_says_why(qtbot):
    """出厂没给键位的内置动作：启用 = 无从启用，得让用户先录一个，而不是写个空值。"""
    page, cfg = _page(qtbot, values={"hotkeys.framework.settings": ""})
    page.findChildren(Switch)[1].clicked.emit()
    assert cfg.writes == [] and cfg.removes == []
    assert "先点" in page._fb.text()


def test_captured_combo_is_written_and_rebound(qtbot):
    page, cfg = _page(qtbot, hotkeys=[_spec()])
    _pills(page)[-1].captured.emit("cmd+alt+r")
    assert cfg.writes == [("recognize", "cmd+alt+r")]
    # ⌥ 在 ⌘ 前（系统约定顺序是 ⌃⌥⇧⌘，不是字母序）
    assert "⌥⌘R" in page._fb.text()


def test_conflicting_combo_is_not_written_at_all(qtbot):
    """撞车 ⇒ **一个字都不写**（不是写了再回滚），并把占用者点名。"""
    page, cfg = _page(
        qtbot, hotkeys=[_spec()], conflict=lambda _name, _combo: "打开面板",
    )
    _pills(page)[-1].captured.emit("cmd+shift+b")
    assert cfg.writes == [] and cfg.removes == []
    assert "打开面板" in page._fb.text()


def test_saved_but_not_bound_says_so_instead_of_pretending_success(qtbot):
    """写进配置了却挂不上 —— 用户看到的是「按了没反应」，这一条必须说出来。"""
    page, cfg = _page(qtbot, hotkeys=[_spec()], rebind=REBIND_FAILED)
    _pills(page)[-1].captured.emit("cmd+alt+r")
    assert cfg.writes  # 盘已写
    assert "没能挂上" in page._fb.text()


@pytest.mark.parametrize("result", [REBIND_BOUND, REBIND_UNBOUND])
def test_a_bind_that_went_as_asked_is_not_reported_as_a_failure(qtbot, result):
    """控制组：**撤掉**与**真挂上**都是达成了配置要求的那一头，不许报「没能挂上」。

    旧口径是布尔，这两档与失败挤在一个 ``False`` 里 ⇒ 停用也会被说成挂不上。
    """
    page, cfg = _page(qtbot, hotkeys=[_spec()], rebind=result)
    _pills(page)[-1].captured.emit("cmd+alt+r")
    assert "没能挂上" not in page._fb.text()


@pytest.mark.parametrize("available", [True, False])
def test_page_shows_up_even_when_hotkeys_are_unavailable(qtbot, available):
    """热键不可用也要出页（藏了用户不知道为什么没有），只是多一条警示。"""
    cfg = _Cfg()
    page = HotkeysPage(config=cfg, contributions=_Stub(), hotkeys_available=available)
    qtbot.addWidget(page)
    labels = [w.text() for w in page.findChildren(QLabel)]
    assert any("辅助功能" in t for t in labels) is not available


# ---------- 目录：用户自建 ----------

def _custom_cfg():
    return _Cfg(
        values={"hotkeys.custom.1": "ctrl+alt+b"},
        customs={"custom.1": {"title": "重启蓝牙", "command": "/usr/bin/pkill -HUP bluetoothd"}},
    )


def test_custom_entry_shows_command_and_can_be_deleted():
    """自建条目：副行是命令（它唯一需要被核对的东西），且**只有它能删**。"""
    entries = collect(config=_custom_cfg(), hotkeys=[_spec()])
    mine = [e for e in entries if e.source == SOURCE_CUSTOM]
    assert len(mine) == 1
    assert mine[0].title == "重启蓝牙"
    assert "bluetoothd" in mine[0].description
    assert mine[0].deletable and not mine[0].resettable
    assert all(not e.deletable for e in entries if e.source != SOURCE_CUSTOM)


def test_feedback_defaults_follow_the_source():
    """成功后反馈的默认值：内置（自带可见结果）不通知、自建（跑命令没动静）发通知。"""
    entries = collect(config=_custom_cfg(), hotkeys=[_spec()], actions=FRAMEWORK_ACTIONS)
    by_source = {e.source: e for e in entries}
    assert by_source[SOURCE_FRAMEWORK].feedback == FEEDBACK_NONE
    assert by_source[SOURCE_PLUGIN].feedback == FEEDBACK_NONE
    assert by_source[SOURCE_CUSTOM].feedback == FEEDBACK_NOTIFY


def test_resettable_only_when_the_combo_differs_from_factory():
    """「初始化」只在**改过**之后才可用：没改过还让它可点，是在骗人有个动作可做。"""
    pristine = collect(config=_Cfg(), actions=FRAMEWORK_ACTIONS)[0]
    assert not pristine.resettable
    moved = collect(config=_Cfg({"hotkeys.framework.panel": "ctrl+alt+p"}),
                    actions=FRAMEWORK_ACTIONS)[0]
    assert moved.resettable


def test_next_custom_name_skips_used_slots():
    """删掉 2 之后下一条仍是 3：复用空号会让同一个配置键指过两个东西。"""
    cfg = _custom_cfg()
    cfg.customs["custom.2"] = {"title": "x", "command": "y"}
    assert next_custom_name(cfg) == "custom.3"


def test_write_custom_spreads_across_its_three_sources():
    """键位归 ``hotkeys``、反馈归 ``hotkey_feedback``，标题与命令归 ``custom_hotkeys``。"""
    cfg = _Cfg()
    name = write_custom(cfg, CustomSpec(title="开日历", command="/usr/bin/open -a 日历",
                                        combo="ctrl+alt+k", feedback=FEEDBACK_NOTIFY))
    assert name == "custom.1"
    assert cfg.customs["custom.1"]["command"] == "/usr/bin/open -a 日历"
    assert cfg.values["hotkeys.custom.1"] == "ctrl+alt+k"
    assert cfg.feedback == {}      # 等于默认值（notify）⇒ 不落盘


def test_write_feedback_drops_the_key_when_it_matches_the_default():
    cfg = _Cfg()
    write_feedback(cfg, "framework.panel", FEEDBACK_NONE, FEEDBACK_NONE)
    assert cfg.feedback == {}
    write_feedback(cfg, "framework.panel", FEEDBACK_NOTIFY, FEEDBACK_NONE)
    assert cfg.feedback == {"framework.panel": "notify"}


def test_remove_custom_clears_all_three_sections():
    cfg = _custom_cfg()
    remove_custom(cfg, "custom.1")
    assert cfg.customs == {}
    assert "hotkeys.custom.1" not in cfg.values


# ---------- 页面：初始化 / 详情屏 / 删除 ----------

def test_initialize_button_restores_the_factory_combo(qtbot):
    """初始化 = 删差异项（不是写一遍默认值），出厂键位随即生效。"""
    page, cfg = _page(qtbot, values={"hotkeys.framework.panel": "ctrl+alt+p"})
    btn = _buttons(page, "初始化")[0]
    assert btn.isEnabled()
    btn.click()
    assert cfg.removes == ["framework.panel"]
    assert "出厂键位" in page._fb.text()


def test_initialize_is_disabled_while_the_combo_is_untouched(qtbot):
    page, _cfg = _page(qtbot)
    assert not _buttons(page, "初始化")[0].isEnabled()


def test_add_opens_the_editor_instead_of_a_dialog(qtbot):
    """新增走**页内换屏**：不开窗、不叠层（零模态）。"""
    page, _cfg = _page(qtbot)
    _buttons(page, "+ 新增")[0].click()
    editor = page._stack.current()
    assert isinstance(editor, HotkeyEditor)
    assert editor is not page._list_page


def test_saving_a_new_custom_writes_everything(qtbot):
    page, cfg = _page(qtbot)
    _buttons(page, "+ 新增")[0].click()
    editor = page._stack.current()
    editor._title_edit.setText("开日历")
    editor._cmd_edit.setText("/usr/bin/open -a 日历")
    editor._pill.captured.emit("ctrl+alt+k")
    editor._save_btn.click()
    assert cfg.customs["custom.1"]["title"] == "开日历"
    assert cfg.values["hotkeys.custom.1"] == "ctrl+alt+k"
    assert page._stack.current() is page._list_page   # 保存完回列表


def test_new_custom_cannot_be_saved_without_a_command(qtbot):
    """名称与命令缺一不可 —— 一条不知道跑什么的快捷键是没有意义的。"""
    page, cfg = _page(qtbot)
    _buttons(page, "+ 新增")[0].click()
    editor = page._stack.current()
    assert not editor._save_btn.isEnabled()
    editor._title_edit.setText("开日历")
    assert not editor._save_btn.isEnabled()      # 还差命令
    editor._cmd_edit.setText("/usr/bin/open")
    assert editor._save_btn.isEnabled()
    editor._save_btn.click()
    assert cfg.customs


def test_delete_takes_two_clicks(qtbot):
    """删除是两段式（**零模态**：不弹框问），第一次点击只把按钮变成确认。"""
    page, cfg = _page(qtbot, customs={"custom.1": {"title": "重启蓝牙", "command": "c"}},
                      values={"hotkeys.custom.1": "ctrl+alt+b"})
    entry = [e for e in page._entries if e.source == SOURCE_CUSTOM][0]
    page._open_editor(entry)
    editor = page._stack.current()
    editor._del_btn.click()
    assert cfg.customs, "第一次点击不删"
    assert editor._del_btn.text() == "确认删除"
    editor._del_btn.click()
    assert cfg.customs == {}


def test_feedback_dropdown_is_written_per_entry(qtbot):
    """「成功后反馈」是**逐条**的：内置条目也能单独设为系统通知（落盘 notify）。"""
    page, cfg = _page(qtbot)
    page._open_editor(page._entries[0])
    editor = page._stack.current()
    # 用户在下拉里选「系统通知」：改当前项 ⇒ 发 currentIndexChanged
    idx = editor._feedback_combo.findData(FEEDBACK_NOTIFY)
    editor._feedback_combo.setCurrentIndex(idx)
    editor._save_btn.click()
    assert cfg.feedback == {"framework.panel": "notify"}


def test_test_button_runs_the_draft_command_and_shows_the_result(qtbot):
    """测试用**草稿**里的命令跑一次（不保存也会跑），结果就地显示。"""
    seen = {}
    page, _cfg = _page(qtbot, run_command=lambda cmd, done: (
        seen.setdefault("cmd", cmd),
        done(CommandResult(True, 0, 12, "ok")),
    ))
    _buttons(page, "+ 新增")[0].click()
    editor = page._stack.current()
    editor._cmd_edit.setText("/usr/bin/open")
    editor._test_btn.click()
    assert seen["cmd"] == "/usr/bin/open"
    assert "已完成" in editor._result.text()


def test_test_failure_is_shown_in_place(qtbot):
    page, _cfg = _page(qtbot, run_command=lambda cmd, done: done(
        CommandResult(False, None, 0, reason="找不到这个程序：/nope")))
    _buttons(page, "+ 新增")[0].click()
    editor = page._stack.current()
    editor._cmd_edit.setText("/nope")
    editor._test_btn.click()
    assert "找不到" in editor._result.text()


# ── 没挂上要写在行上（2026-10-03）─────────────────────────────────────────────


def test_row_says_why_a_hotkey_is_not_bound(qtbot):
    """启动那批热键注册失败时没有界面在场 ⇒ 事后翻到这一页，那行要写着原因。

    这是「按了没反应」唯一能自查的落点：日志用户不会去看，而这一页正是他会翻到的
    地方。判据写在**副标题**上（不是另起一枚徽标：出处那枚徽标刻意走中性色）。
    """
    reason = "CGEventTap 不可用（检查辅助功能权限）"
    page, _cfg = _page(
        qtbot, hotkeys=[_spec()], components=(
            ComponentSpec(plugin_id="recognize", name="识别"),
        ),
        failure_of=lambda name: reason if name == "recognize" else None,
    )
    rows = page.findChildren(RecordRow)
    assert len(rows) == len(page._entries) == 3
    row = next(r for r, e in zip(rows, page._entries) if e.name == "recognize")
    assert "没挂上" in row._sub.text(), f"没挂上的那一行没说出口：{row._sub.text()!r}"
    assert reason in row._sub.toolTip(), "完整原因只在提示里也该在（副标题是单行省略的窄条）"


def test_disabled_hotkeys_are_never_reported_as_unbound(qtbot):
    """控制组：键位为空 = 用户主动停用，**不是**失败 ⇒ 不许写成「没挂上」。

    反过来的假红是这一页最容易犯的：问「挂上没」而不先问「配置要不要它挂」，
    于是所有停用条目都变成「没挂上」，用户分不清哪一个才是真出问题。
    """
    # 打桩**对每条都给原因**（包括旁边那两条启用的）⇒ 判据只能是「配置要不要它占键」，
    # 不是「桩给了什么」—— 否则这条自证就是自证自。
    page, _cfg = _page(
        qtbot, hotkeys=[_spec(default="")],
        failure_of=lambda _name: "任何原因都该被忽略",
    )
    rows = page.findChildren(RecordRow)
    assert len(rows) == len(page._entries) == 3
    off = next(r for r, e in zip(rows, page._entries) if e.name == "recognize")
    assert not page._entries[[e.name for e in page._entries].index("recognize")].enabled
    assert "没挂上" not in off._sub.text(), f"停用条目被报成失败：{off._sub.text()!r}"
    # 反向控制组：同一份桩下，启用那两条**该**报 —— 证明上面那条不是因为桩没生效
    on = [r for r, e in zip(rows, page._entries) if e.name != "recognize" and e.enabled]
    assert on and all("没挂上" in r._sub.text() for r in on), "桩没生效 ⇒ 上面那条是空转"
