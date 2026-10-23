"""tests/test_todo_detail：合并方案**第 3 批（界面落点重排）**的判据。

三处落点各守一件事（方案 §4）：

- **面板行**只报状态 —— 整行点击归框架合成的详情入口（那条在 ``test_todo.py``）；
- **详情页**（``detail_view.py``）＝ 全部条目管理：两级导航能进能回、每条改完
  （「确定」／删除）**立刻写盘**、窗口页脚只有「完成」、关窗不用问「还有没保存的改动」
  （独立数据管理，与设置窗的攒改动模型是两码事）；
- **设置页**（``settings.py``）＝ **只剩三格配置**：条目编辑器与「常驻预览」开关都不许
  还在这里（同一件事两个落点必然漂，两份互不感知）。

这里守的都是**不报错**的那类错：改动悄悄没写下去、打开一条老条目就把它的问法
换掉、补形状时把用户没要的按钮或别人写的键丢掉。
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtWidgets import QComboBox, QLabel, QLineEdit, QPushButton

from barkit.core.config import Config
from barkit.core.interaction import PROMPT_CHOICE, PROMPT_TIME
from barkit.core.plugin import PluginContext
from barkit.core.variables.pool import VariablePool
from barkit.plugins.todo.detail_view import _DOT_WORDS, _status_dot, build_detail_page
from barkit.plugins.todo.plugin import Plugin
from barkit.plugins.todo.task import SHOWS, STATUS_DONE, STATUS_OVERDUE, STATUS_PENDING
from barkit.shell.ui_kit import (
    Card,
    ChoiceBar,
    ChoiceSelector,
    ElideLabel,
    HoverRow,
    NumberField,
    Switch,
)


def _item(**kw) -> dict:
    base = {
        "id": "td1",
        "enabled": True,
        "title": "喝水",
        "at": "09:30",
        "ask": PROMPT_CHOICE,
    }
    base.update(kw)
    return base


def _plugin(tmp_path, items=None) -> Plugin:
    ctx = PluginContext(config=Config(user_path=tmp_path / "c.yaml"), events=None)
    plugin = Plugin(ctx)
    if items is not None:
        assert plugin.save_items(items) is True
    return plugin


def _manager(qtbot, tmp_path, items=None):
    """造一份详情页内容面（``DetailEntrySpec.build()`` 零参工厂产出的那个 widget）。"""
    plugin = _plugin(tmp_path, items)
    m = build_detail_page(plugin)
    qtbot.addWidget(m)
    m.show()
    return plugin, m


def _choose(selector, value: str) -> None:
    """按**取值**驱动一个取值控件（编辑屏的 ``ChoiceBar`` / 设置页的 ``ChoiceSelector``）
    —— 走真实那条路。

    ``ChoiceSelector`` 点它里面那枚 ``QComboBox``；``ChoiceBar`` **点那枚芯片**。
    两条都不许直接调 ``set_current``：那是**不发信号**的（``__init__`` 只负责首屏那一次），
    用它驱动用例会「什么也没测到还全绿」；``test_route_guard.py`` 的 ``_set_net_mode``
    是同一个先例。
    """
    if isinstance(selector, ChoiceBar):
        for btn, chip_value in selector._btns:
            if chip_value == value:
                btn.click()
                return
        raise AssertionError(
            f"单选组里没有取值 {value!r}：{[v for _b, v in selector._btns]}"
        )
    combo = selector.findChild(QComboBox)
    combo.setCurrentIndex(combo.findData(value))


def _menu_labels(m) -> list:
    """「⋯ 更多」此刻会列出哪几项（顺序原样）。

    ``_more_items()`` 是**现造**的（状态是活的：今天做完了没、保存了没），故每次都重新
    问一遍 —— 缓存住一份就等于把判据钉在「按下它的那一刻」。
    """
    return [i.label for i in m._more_items()]


def _today_label(m) -> str:
    """菜单里那枚「标记 / 撤销今天完成」此刻叫什么名字。"""
    labels = [lbl for lbl in _menu_labels(m) if "今天完成" in lbl]
    assert labels, f"菜单里没有「标记今天完成」这一项（实际 {_menu_labels(m)}）"
    return labels[0]


# ── 两级导航（列表 ⇄ 编辑屏） ────────────────────────────────────────

def test_the_detail_page_goes_into_an_entry_and_back(qtbot, tmp_path):
    """点一条进编辑屏、返回回列表 —— 这一批把「条目管理」整个搬进来，导航是它的门。"""
    _p, m = _manager(qtbot, tmp_path, [_item()])
    assert m._stack.current() is m._stack.list_page()

    m._open_edit(m._items[0]["id"])
    assert m._stack.current() is m._edit

    m._on_back()
    assert m._stack.current() is m._stack.list_page()


def test_adding_an_entry_assigns_an_id_and_returns_to_the_list(qtbot, tmp_path):
    """「＋ 新增」走的是同一条两级导航；新条目当场拿到 id（答案要落到它身上）。"""
    _p, m = _manager(qtbot, tmp_path, [_item()])
    m._on_add()
    assert m._stack.current() is m._edit

    m._w["title"].setText("站起来走走")
    m._w["at"].setText("11:00")
    m._commit_form()

    assert m._stack.current() is m._stack.list_page()
    assert [i["id"] for i in m._items] == ["td1", "td2"]
    assert m._items[1]["title"] == "站起来走走"
    assert _p.items()[1]["title"] == "站起来走走", "新增也该立刻写进配置"


def test_a_bad_time_is_refused_before_anything_is_written(qtbot, tmp_path):
    """校验没过 ⇒ **一个字都不许落**，红字落本屏（``EditScreen.on_validate`` 那道门）。"""
    _p, m = _manager(qtbot, tmp_path, [_item()])
    m._on_add()
    m._w["title"].setText("时间写歪的")
    m._w["at"].setText("25:99")

    m._commit_form()

    assert m._stack.current() is m._edit, "校验没过不该回列表"
    assert len(m._items) == 1, "校验没过却把这条收进了列表"
    assert m._edit_fb.text(), "校验没过要落一句红字（零模态的出口就是这条槽）"


# ── 独立数据管理：每条改完立刻写盘（无「攒着等窗口保存」） ──────────────

def test_the_detail_entry_has_no_window_level_save(qtbot, tmp_path):
    """待办是独立数据管理：详情页**不挂** ``save``（没有窗口「保存」、关窗不问
    「还有没保存的改动」）。这条守着「别把设置窗的攒改动模型误用到这里」。"""
    plugin = _plugin(tmp_path, [_item()])
    entry = plugin.detail_entry()
    assert entry.save is None, "待办不该用设置窗那套「攒改动再保存」"


def test_confirm_writes_the_entry_to_disk_immediately(qtbot, tmp_path):
    """「确定」就是写盘本身：收进列表的同时**立刻存进配置**，不用再按窗口那枚「保存」。"""
    plugin, m = _manager(qtbot, tmp_path, [_item()])
    m._open_edit(m._items[0]["id"])
    m._w["title"].setText("改过的标题")
    m._commit_form()

    assert m._items[0]["title"] == "改过的标题"
    assert plugin.items()[0]["title"] == "改过的标题", "「确定」就该把改动写进配置"


def test_a_successful_save_leaves_its_receipt_on_the_list_screen(qtbot, tmp_path):
    """保存成功的回执落在**列表屏**那条槽，不是编辑屏 —— 此刻编辑屏已经切走，说在那边
    等于没说。

    六处 ``EditScreen`` 消费方里，这一处原先唯独静默（成功路径只 ``dismiss()`` 清空、
    一个字都不说）。落点与其余消费方一致：``autotype/settings.py`` 与
    ``notify_guard/settings_edit.py`` 都把回执说在列表屏。
    """
    _plugin_, m = _manager(qtbot, tmp_path, [_item()])
    m._open_edit(m._items[0]["id"])
    m._w["title"].setText("改过的标题")

    m._commit_form()

    assert m._stack.current() is m._stack.list_page()
    assert "改过的标题" in m._list_fb.text(), "回执要落在列表屏那条槽、带上这一条的名字"


def test_a_failed_write_keeps_the_user_on_the_edit_screen(qtbot, tmp_path, monkeypatch):
    """写盘失败 ⇒ 红字留在编辑屏、不让回列表，内存那一步也撤回去（不谎报已存）。"""
    plugin, m = _manager(qtbot, tmp_path, [_item()])

    monkeypatch.setattr(plugin, "save_items", lambda _items: False)

    m._on_add()
    m._w["title"].setText("写不进去的")
    m._commit_form()

    assert m._stack.current() is m._edit, "写盘失败不该回列表"
    assert len(m._items) == 1, "写盘失败不该把这条收进列表"
    assert m._edit_fb.text(), "写盘失败要落一句红字"


def test_reconfirming_an_old_entry_does_not_change_its_meaning(qtbot, tmp_path):
    """老条目（盘上只有五六个键）打开后按「确定」—— 形状补成十三格但**语义等价**，
    且不认识的键原样保留（那是别人写的数据，一次「确定」清掉就是丢数据）。"""
    old = {"id": "td1", "enabled": True, "title": "喝水", "at": "09:30", "secret": "x"}
    plugin, m = _manager(qtbot, tmp_path, [old])
    m._open_edit(m._items[0]["id"])

    assert m._w["act_done"].isChecked() is False, "老条目本来就没有「完成」按钮"
    assert m._w["act_snooze"].isChecked() is False

    m._commit_form()
    saved = plugin.items()[0]
    assert saved["actions"] == [], "补形状补出了一个用户没要的按钮"
    assert saved["ask"] == PROMPT_CHOICE, "老条目的缺省问法就是「到点弹『完成』」"
    assert saved.get("secret") == "x", "不认识的键被清掉了（数据丢失）"


def test_deleting_an_entry_writes_through_immediately(qtbot, tmp_path):
    """删这一条**立即写盘**—— 列表里少一条、盘上也少一条，没有「攒着等保存」这回事。"""
    plugin, m = _manager(qtbot, tmp_path, [_item(), _item(id="td2", at="18:00")])
    m._open_edit(m._items[0]["id"])
    m._on_delete()

    assert [i["id"] for i in m._items] == ["td2"]
    assert [i["id"] for i in plugin.items()] == ["td2"], "删完盘上还留着，就是没写进去"


def test_a_failed_delete_keeps_the_entry(qtbot, tmp_path, monkeypatch):
    """删除写盘失败 ⇒ 原样放回列表、留在编辑屏，红字说明。"""
    plugin, m = _manager(qtbot, tmp_path, [_item(), _item(id="td2", at="18:00")])
    monkeypatch.setattr(plugin, "save_items", lambda _items: False)

    m._open_edit(m._items[0]["id"])
    m._on_delete()

    assert [i["id"] for i in m._items] == ["td1", "td2"], "删失败不该把这条拿掉"
    assert m._stack.current() is m._edit, "删失败不该回列表"


# ── 老条目：打开一次不许把它的问法换掉 ────────────────────────────────

def test_opening_an_old_entry_does_not_silently_change_its_ask(qtbot, tmp_path):
    """**没有 ``ask`` 键**的老条目打开一次，问法不许从「到点弹『完成』」变成「不弹提醒」。

    单选组的初始值若直接取 ``draft.get("ask")``，空串正好命中「不弹提醒」那一枚 ⇒ 用户
    什么都没改、一按「确定」就把这条待办**静默改成不提醒**（而它在界面上看着还是好的）。
    """
    old = {"id": "td1", "enabled": True, "title": "喝水", "at": "09:30"}
    _p, m = _manager(qtbot, tmp_path, [old])
    m._open_edit(m._items[0]["id"])
    assert m._ask() == PROMPT_CHOICE, "老条目的缺省问法就是「到点弹『完成』」"

    m._commit_form()
    assert m._items[0]["ask"] == PROMPT_CHOICE


# ── 哪几格此刻说得上话 ──────────────────────────────────────────────

def test_the_time_field_follows_the_ask_choice(qtbot, tmp_path):
    """问时刻的条目靠**作答**算时刻，没有一个说得出的固定时刻 ⇒ ``at`` 灰掉、
    「结束＝作答后」亮起来。

    判据与内核同一份口径（``scheduler._span_of`` 的 ``work_hours`` 分支也不看 ``at``）
    —— 界面里另立一套的结果是「界面说能填、实际永不生效」。
    """
    _p, m = _manager(qtbot, tmp_path, [_item()])
    m._open_edit(m._items[0]["id"])
    assert m._w["at"].isEnabled() is True
    assert m._w["work_hours"].isEnabled() is False

    _choose(m._w["ask"], PROMPT_TIME)

    assert m._w["at"].isEnabled() is False
    assert m._w["work_hours"].isEnabled() is True


def test_the_edit_form_selects_with_no_popup_window(qtbot, tmp_path):
    """编辑屏那两处取值（问法 / 钟面）是**摊开的单选组**，不是下拉。

    ``QComboBox`` 的弹出列表是一个**独立顶层窗口**，挂着 OpenDisplay 虚拟屏时会被 macOS
    归位到那块屏（2026-10-09 真机：窗口在内建屏、列表弹在 iPad 屏；六条抢回手段实测
    全部无效，抢回本身还会变成弹跳）。改回 ``ChoiceSelector`` 就等于把那个缺陷放回来
    —— 本用例转红。

    ``QComboBox`` 在编辑屏里**一个都不该有**：两处取值就是全部的下拉。
    """
    _p, m = _manager(qtbot, tmp_path, [_item()])
    m._open_edit(m._items[0]["id"])

    assert isinstance(m._w["ask"], ChoiceBar)
    assert isinstance(m._w["face"], ChoiceBar)
    assert m._edit.findChildren(QComboBox) == [], "编辑屏里还有下拉 ⇒ 又有弹层会跑屏"


def test_the_more_menu_lists_only_what_this_entry_can_do(qtbot, tmp_path):
    """「⋯ 更多」里给哪几件事，按「这一条此刻做不做得出那件事」定（顺序即菜单顺序）。

    「预览这一条」恒在（未保存的条目也预览得出来，内核会给它补一个假 id）；另外两枚
    只对**已经保存**的条目 —— 没保存的条目答案没处落，先画出来是骗人。

    ★ 2026-10-08：这三枚原先各占编辑屏动作行的一格（``_preview_btn`` / ``_ask_btn`` /
    ``_today_btn``）。用户裁定「次动作收进菜单」，判据随之从「三枚按钮各自的可见性」
    改成「菜单里列出哪几项」—— **「按做不做得出过滤」这件事还在**（整枚「⋯ 更多」的
    显隐由 ``_sync_actions`` 管），只是颗粒度从三枚收成一份清单。
    """
    _p, m = _manager(qtbot, tmp_path, [_item()])
    m._open_edit(m._items[0]["id"])
    assert _menu_labels(m) == ["预览这一条", "现在问我", "标记今天完成"]

    m._on_add()                             # 未保存的新条目
    assert _menu_labels(m) == ["预览这一条"], (
        "没保存的条目答案没处落，除了「预览」不该先画出来"
    )


def test_the_more_menu_is_a_non_blocking_popup_holding_its_own_reference(qtbot, tmp_path):
    """「⋯ 更多」走 ``popup()`` 不是 ``exec()``，且弹层被**持引用**替它保命。

    ``exec()`` 会在置顶窗上再开一条模态循环（面板那枚「⋯」踩过这条，见
    ``ui_kit/panel/actions.py`` 的注释）；非阻塞弹层一出作用域就可能被回收。
    这条刻意**不 patch** ``popup_at`` —— 正因如此才顺带证得了「弹完这一句就返回」。
    """
    _p, m = _manager(qtbot, tmp_path, [_item()])
    m._open_edit(m._items[0]["id"])
    assert m._more_menu is None, "还没点开，不该有浮层"

    m._on_more()                            # 非阻塞：这一句立刻就回来了

    assert m._more_menu is not None, "弹层没被持住引用 ⇒ 下次事件循环就可能被回收"
    assert m._more_menu.isVisible(), "点了「⋯ 更多」却没弹出"
    m._more_menu.close()


def test_marking_done_from_the_detail_page_writes_today_only(qtbot, tmp_path):
    """「标记今天完成」＝ 用户自己动手那条路（不查去重账），再按一次是撤销。

    菜单里那枚的**名字**跟着 ``done_at`` 翻转：它是这个状态唯一露脸的地方（行尾那一格
    只在列表里，编辑屏看不见），故这条路既验写盘、也验名字。
    """
    plugin, m = _manager(qtbot, tmp_path, [_item()])
    m._open_edit(m._items[0]["id"])
    assert plugin.done_at("td1") == ""
    assert _today_label(m) == "标记今天完成"

    m._on_toggle_today()
    assert plugin.done_at("td1") != "", "点了「标记今天完成」却没有记上"
    assert _today_label(m) == "撤销今天完成"

    m._on_toggle_today()
    assert plugin.done_at("td1") == ""
    assert _today_label(m) == "标记今天完成"


# ── 设置页：只剩三格配置 ────────────────────────────────────────────

def test_the_settings_page_keeps_only_the_three_config_cards(qtbot, tmp_path):
    """设置页 = **三张配置卡 ＋ 一张入口卡**：条目编辑器与「常驻预览」开关都不许还在这里。

    留一份编辑器就是同一件事的第二个真源 —— 两份互不感知，一份看不到另一份刚做的改动。
    第 4 张「任务管理」卡里**只有一枚开窗的按钮**（没有任何输入控件），它开的是框架
    ``DetailHost`` 托管的那一份内容面 —— 与点面板整行同一个，不是第二个落点。

    ⇒ 卡片数从 3 变 4 是**故意的**；这一条的职责随之是「除那张入口卡外，别的东西一个
    都不许再长回来」，故下面仍旧逐项数控件。
    """
    plugin = _plugin(tmp_path, [_item()])
    page = plugin.settings_page().build()
    qtbot.addWidget(page)
    page.show()
    qtbot.wait(1)

    titles = [lbl.text() for lbl in page.findChildren(QLabel)]
    for title in ("浮层大小", "关闭方式", "最多显示几条", "任务管理"):
        assert title in titles, f"少了一张卡：{title}（实际 {titles}）"
    assert "条目" not in titles, "条目编辑器还在设置页（那是详情页的事）"
    assert len(page.findChildren(Card)) == 4, "3 张配置卡 ＋ 1 张入口卡，多出来的是没搬走的"
    assert page.findChildren(Switch) == [], "设置页不该有开关（「常驻预览」已删）"
    # ``QSpinBox`` 内部自带一个 ``QLineEdit``（行编辑）—— 那不是「自由文本框」，
    # 它是「最多显示几条」那一格的零件。
    free_inputs = [
        e for e in page.findChildren(QLineEdit)
        if e.objectName() != "qt_spinbox_lineedit"
    ]
    assert free_inputs == [], "设置页不该有自由文本框"
    assert page.findChild(NumberField) is not None, "行数上限那一格该在"


def test_the_settings_page_writes_through_the_plugin(qtbot, tmp_path):
    """三格各自走插件的写口 —— 界面**不碰 ``Config``**（门禁 ``test_no_config_io_in_ui``）。"""
    plugin = _plugin(tmp_path, [])
    page = plugin.settings_page().build()
    qtbot.addWidget(page)
    page.show()

    _choose(page.findChildren(ChoiceSelector)[0], "large")
    assert plugin.get_size() == "large", "改「浮层大小」没写进去"

    _choose(page.findChildren(ChoiceSelector)[1], "right_click")
    assert plugin.get_close_gesture() == "right_click", "改「关闭方式」没写进去"

    spin = page.findChild(NumberField)
    spin.setValue(7)
    spin.editingFinished.emit()
    assert plugin.get_max_rows() == 7, "改「最多显示几条」没写进去"


def _manage_button(page) -> QPushButton:
    """设置页那张入口卡里的按钮（按文案找；找不到就把实际有哪几枚列进断言消息）。"""
    for btn in page.findChildren(QPushButton):
        if btn.text() == "打开任务管理":
            return btn
    raise AssertionError(
        f"设置页里没有「打开任务管理」：{[b.text() for b in page.findChildren(QPushButton)]}"
    )


def test_the_settings_page_opens_the_detail_window_through_the_plugin(qtbot, tmp_path):
    """设置页那枚「打开任务管理」**只请求开窗**，不自己画一份内容。

    判据落在「组件被问了什么」上：真开窗那一半归 ``shell/detail.py`` 的用例（测试里没有
    框架）。这里要钉的是**它没有绕开框架** —— 自己 new 一个对话框就会与面板整行入口各开
    一份、互不感知，那正是 ``hosts_guard`` 那枚「管理条目…」按钮被删掉的原因。
    """
    plugin = _plugin(tmp_path, [_item()])
    asked: "list[str]" = []

    def _fake_open_detail() -> bool:
        asked.append("open_detail")
        return True

    plugin.open_detail = _fake_open_detail
    page = plugin.settings_page().build()
    qtbot.addWidget(page)
    page.show()

    _manage_button(page).click()
    assert asked == ["open_detail"], "按钮没走 Plugin.open_detail（自己开窗了？）"


def test_the_settings_page_says_so_when_the_window_cannot_open(qtbot, tmp_path):
    """框架没接线时（``open_detail()`` 返回 ``False``）**要出声**，不能点了没反应。

    重新装回一个「按下去什么也不发生」的入口按钮，症状是用户以为界面坏了 —— 而这一枚
    与面板整行的区别本就在于「不用先知道面板上哪一行是我自己的」。
    """
    plugin = _plugin(tmp_path, [_item()])
    plugin.open_detail = lambda: False
    page = plugin.settings_page().build()
    qtbot.addWidget(page)
    page.show()

    _manage_button(page).click()
    said = [lbl.text() for lbl in page.findChildren(QLabel)]
    assert any("打不开" in s for s in said), f"开不了窗却一声不吭：{said}"


# ---------- 主窗体清单（设计文档 §4.5） ----------
#
# 这一节守的是**界面自己又算了一遍**那类错 —— 它们全都不报错、界面看着还挺对：
#
# - 页眉那两个数不跟面板那行摘要同源 ⇒「面板说 3 项、窗口说 2 项」，用户没法判断信谁；
# - 变量条把池里**全部**变量铺出来（等于把框架设置页搬进组件窗），
#   或者反过来一个变量都没引用时仍挂着一条空壳；
# - 行内动作按**列表下标**找人 —— 那下标会随「今天 / 全部」那一档变；
# - 新模型的任务点了「修改」**不出声也没反应**（那块编辑屏还只认旧条目表的形状）；
# - 三态状态点塌成一态（三种情形同一个颜色 / 同一句悬停）。

#: 两条声明：一个「问出来」的上班时间，一个由它**算出**的下班时间。
#: 引用的只有后一个 ⇒ 变量条该只列 ``off_time`` 这一条。
_DECLS = [
    {"id": "start_time", "name": "上班时间", "source": "ask", "type": "time",
     "trigger": {"when": ["daily"]}, "prompt": "今天几点上班？"},
    {"id": "off_time", "name": "下班时间", "source": "derived", "expr": "start_time + 9h"},
]

#: 引用 ``{off_time}`` 的一条（占浮层一行、点「完成」那一刻跑命令）。
_TASK = {
    "id": "report",
    "title": "提交日报",
    "text": "{off_time} 前提交",
    "display": {"show": "row", "reminder": {"at": "{off_time}"}},
    "command": "echo done",
    "command_at": "done",
}

#: 一个变量都不引用的一条。
_PLAIN = {"id": "stretch", "title": "起来动一动"}

#: 用户停用的那一条 —— 「今天」视图不该列它，「全部」该列。
#:
#: 判据用 ``enabled=False`` 而不是「时段没到 / 星期不对」：后两者要挂一个假时钟，而
#: ``_refresh_list`` 里的 ``now`` 是从 ``datetime.now()`` 来的（组件自己取），挂桩点就
#: 跑到组件外面去了 —— 那样子测的是那条桩，不是这一页。
_OFF = {"id": "weekend", "title": "周末整理", "enabled": False}

#: 「只显示、做不掉」的那一条（整点时钟）：占钟面、不需要完成。
_CLOCK = {
    "id": "clock",
    "title": "整点时钟",
    "track_done": False,
    "display": {"show": "clock"},
}


def _task_pool(*, start: str = "08:27") -> VariablePool:
    """带上当日值的池（``off_time`` 由它算出 ``17:27``）。"""
    pool = VariablePool()
    pool.load(_DECLS)
    pool.begin_pass()
    assert pool.set_ask_value("start_time", start)
    pool.begin_pass()
    return pool


def _task_plugin(tmp_path, tasks, pool=None) -> Plugin:
    ctx = PluginContext(config=Config(user_path=tmp_path / "c.yaml"), events=None)
    if pool is not None:
        ctx.variables = pool
    plugin = Plugin(ctx)
    plugin.cfg.set("tasks", [dict(t) for t in tasks])
    return plugin


def _task_manager(qtbot, tmp_path, tasks, pool=None):
    plugin = _task_plugin(tmp_path, tasks, pool)
    m = build_detail_page(plugin)
    qtbot.addWidget(m)
    m.show()
    return plugin, m


def _row_titles(m) -> "list[str]":
    """清单里此刻每行的标题（按屏幕顺序）。

    按 ``HoverRow`` 取 —— 空清单那句 ``note`` 是裸 ``QLabel``，不会混进来。每行第一个
    ``ElideLabel`` 是标题（第二个是摘要）。**不用 ``m._rows``**：那是数据，下面要证的
    恰恰是「数据到屏幕上真的落成了这几行」。
    """
    out: "list[str]" = []
    for row in m._rows_box.findChildren(HoverRow):
        labels = row.findChildren(ElideLabel)
        if labels:
            out.append(labels[0].text())
    return out


def _row_summary(m, title: str) -> str:
    """标题是 ``title`` 那一行的第二行（那句摘要）。"""
    for row in m._rows_box.findChildren(HoverRow):
        labels = row.findChildren(ElideLabel)
        if labels and labels[0].text() == title:
            return labels[1].text()
    raise AssertionError(f"清单里没有标题为 {title!r} 的那一行：{_row_titles(m)}")


def _buttons_of(m, title: str) -> "dict[str, QPushButton]":
    """标题是 ``title`` 那一行上的按钮（文字 → 按钮）。

    ``clear_layout`` 用 ``setParent(None)`` 把旧一组立刻摘出控件树，故重画之后
    ``findChildren`` 只看得到新那一组（旧的不会与新的混在同一份结果里）。
    """
    for row in m._rows_box.findChildren(HoverRow):
        labels = row.findChildren(ElideLabel)
        if labels and labels[0].text() == title:
            return {b.text(): b for b in row.findChildren(QPushButton)}
    raise AssertionError(f"清单里没有标题为 {title!r} 的那一行：{_row_titles(m)}")


def _dot_of(m, title: str):
    """标题是 ``title`` 那一行上的状态点（没有 ⇒ ``None``）。

    状态点是那枚文字为 ``●`` 的 ``QLabel`` —— 标题 / 摘要都是 ``ElideLabel``（也是
    ``QLabel`` 的子类），故**按文字筛**，不按类型。
    """
    for row in m._rows_box.findChildren(HoverRow):
        labels = row.findChildren(ElideLabel)
        if labels and labels[0].text() == title:
            dots = [w for w in row.findChildren(QLabel) if w.text() == "●"]
            return dots[0] if dots else None
    raise AssertionError(f"清单里没有标题为 {title!r} 的那一行：{_row_titles(m)}")


def test_the_header_counts_the_same_rows_the_panel_does(qtbot, tmp_path):
    """★ 页眉那两个数与面板那行摘要是**同一个源**（都走 ``_count_rows``）。

    两个数分开算的结果是「面板说未完成 3、窗口说未完成 2」，而两边都看着对 —— 用户
    没法判断信谁。这里同时钉住两条：分子只数生效的（停用的不算），以及两处的数一致。
    """
    plugin, m = _task_manager(qtbot, tmp_path, [_TASK, _PLAIN, _OFF], _task_pool())

    assert plugin.header_counts() == (2, 0), "停用的那条不该进未完成数"
    assert "未完成 2" in m._head.text(), m._head.text()
    assert "已完成 0" in m._head.text(), m._head.text()

    plugin.set_done("report")
    m._refresh_list()
    assert plugin.header_counts() == (1, 1)
    assert "未完成 1" in m._head.text(), m._head.text()
    assert "已完成 1" in m._head.text(), m._head.text()
    # 与面板同源：同一批计数行 ⇒ 说出来的句子也对得上（``now`` 钉住以免「过期」那一档
    # 在下午把措辞换掉 —— 那与这里要证的「两个数同源」不是一回事）。
    assert plugin._snapshot(datetime(2026, 10, 10, 15, 0)).summary == "未完成 1 项"


def test_the_variable_strip_stays_hidden_when_nothing_is_referenced(qtbot, tmp_path):
    """一条变量都没引用时整条隐身。

    一个空壳「变量：」挂在窗口顶上只会让人问「这里怎么是空的」，而答案是「这一页的
    任务本来就不依赖变量」。**这一条与下面那条是一对**：一个证明该出来的出来了，
    一个证明不该出来的没出来 —— 只写后者的话，``refresh`` 里写成恒隐也照样绿。
    """
    _plugin_, m = _task_manager(qtbot, tmp_path, [_PLAIN], _task_pool())
    assert m._vstrip.isVisible() is False

    _plugin2, m2 = _task_manager(qtbot, tmp_path, [_TASK], _task_pool())
    assert m2._vstrip.isVisible() is True


def test_the_variable_strip_is_collapsed_and_lists_only_the_used_ones(qtbot, tmp_path):
    """变量条：**默认折叠**、折叠那行报「名字 值」、展开后才逐条列出来源。

    只列**被引用**的：把池里全部变量铺在主窗体上等于把框架设置页搬进组件窗口，而用户
    在这一页要回答的是「这几条任务依赖的那些值现在是多少」。
    """
    _plugin_, m = _task_manager(qtbot, tmp_path, [_TASK], _task_pool())

    assert m._vstrip._open is False, "默认该是折叠的（用户不是每次都改共享变量）"
    assert m._vstrip._body.isVisible() is False
    brief = m._vstrip._summary.text()
    assert "下班时间" in brief and "17:27" in brief, brief
    assert "上班时间" not in brief, "没被任何任务引用到的变量不该出现在这里"

    m._vstrip._toggle()
    assert m._vstrip._open is True
    assert m._vstrip._body.isVisible() is True, "展开之后那几行得真的看得见"
    labels = [w.text() for w in m._vstrip._body.findChildren(QLabel)]
    assert "下班时间" in labels and "17:27" in labels, labels
    assert "算出" in labels, f"没标出「它是哪来的」：{labels}"
    assert any("设置 → 变量" in s for s in labels), (
        f"这一批不做「改」按钮 ⇒ 必须指路到框架那页：{labels}"
    )


def test_the_scope_switch_reveals_tasks_that_are_not_in_force(qtbot, tmp_path):
    """「今天 / 全部」：没生效的任务默认那一眼看不见它，但也不能因此就找不着。

    ``_rows`` 恒是**全部**（不带筛选的那一份数据），筛选只发生在往屏幕上落的时候 ——
    两处都筛的结果是「切回『今天』时那几条再也回不来」。
    """
    _plugin_, m = _task_manager(qtbot, tmp_path, [_TASK, _PLAIN, _OFF], _task_pool())

    assert [r["id"] for r in m._rows] == ["report", "stretch", "weekend"], "数据该是全部"
    assert m._scope_value == "today"
    assert _row_titles(m) == ["提交日报", "起来动一动"], "停用的那条在「今天」里不该出现"

    _choose(m._scope, "all")
    assert m._scope_value == "all"
    assert _row_titles(m) == ["提交日报", "起来动一动", "周末整理"]

    _choose(m._scope, "today")
    assert _row_titles(m) == ["提交日报", "起来动一动"]


def test_the_three_states_do_not_collapse_into_one(qtbot, tmp_path):
    """三态状态点（灰＝没到 / 绿＝今天已完成 / 红＝过期没处理）**不许塌成一态**。

    塌了不会有任何报错 —— 三种情形同一枚灰点，界面上只是「少了一层信息」。
    """
    assert set(_DOT_WORDS) == {STATUS_DONE, STATUS_OVERDUE, STATUS_PENDING}
    assert len({token for _glyph, token in _DOT_WORDS.values()}) == 3, "三态得三个色档"
    tips = set()
    for status in (STATUS_DONE, STATUS_OVERDUE, STATUS_PENDING):
        dot = _status_dot(status, None)
        tips.add(dot.toolTip())
    assert len(tips) == 3, f"三态的悬停释义不许重样：{tips}"
    # 未知状态**回落**到「还没到点」而不是抛异常：状态是从计数行来的，界面上多一档
    # 不该把整页带下去。
    assert _status_dot("", None).toolTip() == _status_dot(STATUS_PENDING, None).toolTip()


def test_the_inline_done_button_writes_todays_ledger(qtbot, tmp_path):
    """行内「完成 / 撤销」直接写今天的账，**不进编辑屏**。

    顺便钉住另一件事：已完成那一行**只剩「撤销 / 修改」** —— 「稍后」换的是浮层上那一行
    的去留，而那条早就收了。
    """
    plugin, m = _task_manager(qtbot, tmp_path, [_PLAIN], _task_pool())
    assert set(_buttons_of(m, "起来动一动")) == {"完成", "稍后", "修改"}

    _buttons_of(m, "起来动一动")["完成"].click()
    assert plugin.done_at("stretch"), "行内「完成」该当场写今天的账"

    assert set(_buttons_of(m, "起来动一动")) == {"撤销", "修改"}, "已完成的不该再有「稍后」"

    _buttons_of(m, "起来动一动")["撤销"].click()
    assert not plugin.done_at("stretch"), "「撤销」该把今天的账清掉"


def test_the_snooze_button_goes_through_the_plugin(qtbot, tmp_path):
    """行内「稍后」走组件的写口（不是界面自己记一个数）—— 收起 ``LATER_MINUTES`` 分钟。"""
    from barkit.plugins.todo.plugin import LATER_MINUTES

    plugin, m = _task_manager(qtbot, tmp_path, [_PLAIN], _task_pool())
    seen: "list[tuple]" = []
    plugin.snooze = lambda item_id, minutes=LATER_MINUTES: seen.append((item_id, minutes)) or True

    _buttons_of(m, "起来动一动")["稍后"].click()
    assert seen == [("stretch", LATER_MINUTES)], seen


# ── 「只显示、做不掉」那一条（不需要完成） ────────────────────────────

def test_a_task_that_needs_no_completion_has_no_dot_or_done_button(qtbot, tmp_path):
    """★ 「不需要完成」那一行：**不画状态点、不画「完成」**，只留「稍后 / 修改」。

    灰 / 绿 / 红这一族整族以「完成」为轴，显示类一个都不适用 —— 留个灰点会被读成
    「还没到点」。对照组是同页一条普通任务（这三样都在）：只写「没有」的话，
    把整行删掉、或把状态点全禁掉，用例照样绿。
    """
    _p, m = _task_manager(qtbot, tmp_path, [_TASK, _CLOCK], _task_pool())

    assert _dot_of(m, "整点时钟") is None, "「不需要完成」的不该有状态点"
    assert set(_buttons_of(m, "整点时钟")) == {"稍后", "修改"}

    assert _dot_of(m, "提交日报") is not None, "普通那条该照旧有状态点"
    assert {"完成", "稍后", "修改"} <= set(_buttons_of(m, "提交日报"))


def test_a_task_that_needs_no_completion_offers_no_today_toggle(qtbot, tmp_path):
    """「⋯ 更多」里不给「标记今天完成」—— 它压根没有「完成」这回事。

    另一半是普通那条**照旧给**（同 ``test_the_more_menu_of_a_task_has_no_ask_now``
    那对「只写新模型这一半会退化成恒空」的对照）。
    """
    _p, m = _task_manager(qtbot, tmp_path, [_CLOCK], _task_pool())
    m._open_edit("clock")
    assert _menu_labels(m) == ["预览这一条"]

    _p2, m2 = _task_manager(qtbot, tmp_path, [_TASK], _task_pool())
    m2._open_edit("report")
    assert _menu_labels(m2) == ["预览这一条", "标记今天完成"]


def test_the_need_completion_switch_round_trips(qtbot, tmp_path):
    """编辑屏那枚「需要完成」：**默认开**；关掉保存 ⇒ 盘上落 ``track_done: false``，
    重新打开还是关的（回填那一侧也认它）。"""
    plugin, m = _task_manager(qtbot, tmp_path, [_TASK], _task_pool())
    m._open_edit("report")
    assert m._w["track_done"].isChecked() is True, "默认该是「需要完成」"

    m._w["track_done"].setChecked(False)
    m._commit_form()
    assert plugin.tasks()[0].get("track_done") is False, plugin.tasks()

    m._open_edit("report")
    assert m._w["track_done"].isChecked() is False, "回填没认出关闭那一档"


def test_turning_off_need_completion_greys_the_done_action(qtbot, tmp_path):
    """勾掉「需要完成」⇒「行上「完成」」当场变灰；命令那两格**不跟着灰**。

    命令与「完成」是正交的两件事（``command_at`` 那一格里本来就有「点击完成」这一档，
    而另外三档与「完成」无关）。这道闸顺带守住那枚开关**真的会翻**：``Switch`` 自己不会
    翻（``setChecked`` 只写状态、不发信号，见 ``ui_kit/primitives.py``），翻的那一下归
    消费者 —— 编辑屏里原先有八枚是死的。判据是「翻完这一下立刻的状态」，不重开屏、不重填。
    """
    _p_, m = _task_manager(qtbot, tmp_path, [_TASK], _task_pool())
    m._open_edit("report")
    assert m._w["act_done"].isEnabled() is True, "默认（需要完成）时行上那枚该是活的"

    m._w["track_done"].clicked.emit()      # 点一下：置反那一步归消费者，故只发信号
    assert m._w["track_done"].isChecked() is False, "点了不翻 ⇒ 又是一处死开关"
    assert m._w["act_done"].isEnabled() is False
    assert m._w["command"].isEnabled() is True, "「到点执行」与「完成」无关，不该跟着灰"
    assert m._w["command_at"].isEnabled() is True


def test_the_command_field_explains_variable_references(qtbot, tmp_path):
    """「到点执行」那格底下要有「可写 {变量名}」这句交代（``_VAR_NOTE``）。

    命令走的本来就是那个插值口（``task.command_of``），先前只有这一格没在界面上写出来，
    用户便读成「命令不支持变量」。判据落在**组 3 那一块**里出现了这句。
    """
    _p_, m = _task_manager(qtbot, tmp_path, [_TASK], _task_pool())
    m._open_edit("report")
    texts = [lb.text() for lb in m._boxes[2].findChildren(QLabel)]
    assert any("引用变量" in t for t in texts), texts


def test_a_task_that_needs_completion_writes_no_track_done_key(qtbot, tmp_path):
    """默认 **开** ⇒ 盘上一个字都不多写：打开又「确定」⇒ 逐字段相同。

    多写一个 ``track_done: true`` 不会报错，但「用户什么都没动」这句话就不成立了
    （与 ``command_at`` 只写非缺省那一半同一口径）。
    """
    plugin, m = _task_manager(qtbot, tmp_path, [_TASK], _task_pool())
    m._open_edit("report")
    m._commit_form()
    assert plugin.tasks() == [dict(_TASK)], plugin.tasks()


def test_a_new_model_task_opens_the_task_form(qtbot, tmp_path):
    """新模型的任务点「修改」⇒ 进编辑屏，且屏上是**任务那一套格子**（不是旧十三格）。

    判据落在「哪几个 keys 在 ``_w`` 里」上：拿旧表单去编辑新任务会把 ``effective`` /
    ``display`` 整份丢掉、写回一份两份模型都不认的形状，而界面显示「已保存」。三个
    退场的格子（``card`` / ``overlay`` / ``clock``）也要在**不在**的那一半里 —— 留着
    它们等于把「到点弹卡」与旧的两键显示当作还在的档位。
    """
    _plugin_, m = _task_manager(qtbot, tmp_path, [_TASK], _task_pool())
    _buttons_of(m, "提交日报")["修改"].click()

    assert m._stack.current() is m._edit, "该进编辑屏"
    assert m._draft is not None and m._draft["id"] == "report"
    assert {"enabled", "track_done", "title", "text", "at", "lead_min", "trail_min",
            "every", "win_from", "win_to", "scene", "show", "act_done", "act_snooze",
            "command", "command_at"} <= set(m._w)
    assert not ({"card", "overlay", "clock"} & set(m._w)), "退场的那三格不该还在"
    assert not ({"ask", "work_hours", "end", "interval_min"} & set(m._w)), (
        "旧条目表那几格一个都不该在"
    )
    assert m._w["at"].text() == "{off_time}", "变量引用要按原样回填"


def test_opening_a_task_and_confirming_changes_nothing(qtbot, tmp_path):
    """打开一条任务、一个字不改直接「确定」⇒ 盘上那份**逐字段相同**。

    这是「回填」与「读回」两处的对拍：写出去的键与读回来的键一旦对不齐（多一个默认档、
    少一个字段），用户每打开一次任务就会静默改一次配置 —— 而他什么都没动。
    """
    plugin, m = _task_manager(qtbot, tmp_path, [_TASK], _task_pool())
    m._open_edit("report")
    m._commit_form()

    assert plugin.tasks() == [dict(_TASK)], plugin.tasks()


def test_the_add_button_makes_a_task_not_an_old_entry(qtbot, tmp_path):
    """「＋ 新增」在新模型下造的是**任务**：写 ``tasks``，一个字都不往 ``items`` 段写。

    写错表不会报错（`save_*` 那一头才有闸），症状是「新增了、重开又没了」。
    """
    plugin, m = _task_manager(qtbot, tmp_path, [_TASK], _task_pool())
    m._on_add()

    assert m._stack.current() is m._edit, "该进编辑屏"
    assert m._draft is not None and m._draft.get("id") == ""
    assert m._tasks == [dict(_TASK)], "还没确定，内存那张表不该动"

    m._w["title"].setText("站起来走走")
    m._commit_form()

    assert [t["id"] for t in plugin.tasks()] == ["report", "td1"]
    assert plugin.tasks()[1]["title"] == "站起来走走"
    assert plugin.cfg.value("items") == [], "旧表一个字都不该写"
    assert m._stack.current() is not m._edit, "存完回列表"


def test_a_new_model_save_leaves_the_same_receipt_on_the_list(qtbot, tmp_path):
    """新模型那条提交路径（``_commit_task_form``）的回执**与旧模型同款**：同一个落点、
    同一句话形。

    两条路径是两段各自独立的代码（各写一遍 ``back`` / ``_refresh_list`` /
    ``dismiss``），只给一条补回执，另一条就会继续静默 —— 这正是要一条一条钉住的原因。
    """
    _plugin_, m = _task_manager(qtbot, tmp_path, [_TASK], _task_pool())
    m._open_edit("report")

    m._commit_form()

    assert m._stack.current() is m._stack.list_page()
    assert "提交日报" in m._list_fb.text(), "回执要落在列表屏那条槽"


def test_the_old_model_still_reaches_the_edit_screen(qtbot, tmp_path):
    """旧模型那条路**照旧**通 —— 上面那两道闸不能顺手把老条目也一起挡住。

    （条目要在**建页之前**就写好：``_TodoManager._items`` 是构造那一刻从 ``plugin.items()``
    取的那份内存表，建完页再往配置里塞是另一条路。）
    """
    _plugin_, m = _manager(qtbot, tmp_path, [_item()])
    assert _row_titles(m) == ["喝水"]

    _buttons_of(m, "喝水")["修改"].click()
    assert m._stack.current() is m._edit, "旧条目点「修改」该进编辑屏"

    m._stack.back()
    m._on_add()
    assert m._stack.current() is m._edit, "旧条目在「＋ 新增」上也该进编辑屏"


# ── 新模型的编辑屏：引用变量 / 校验 / 删除 / 场景（Phase 5） ──────────


def test_the_variable_picker_lists_the_declared_variables(qtbot, tmp_path):
    """「选变量 ▾」列的是**已声明的变量**（出厂那份也在里面），选中就把 ``{id}`` 写进格子。

    写值进去是错的：``{变量名}`` 是**引用**（§4.3 先插值再求值），写成一个当时的值会让
    改变量之后再改这条任务的人把它固化成字面时刻，而界面上看不出区别。
    """
    _plugin_, m = _task_manager(qtbot, tmp_path, [_TASK], _task_pool())
    m._open_edit("report")

    items = m._var_items(m._w["at"])
    assert [i.label for i in items] == ["{scene}"], [i.label for i in items]
    items[0].on_click()
    assert m._w["at"].text() == "{scene}"


def test_the_task_form_refuses_what_the_kernel_could_not_read(qtbot, tmp_path):
    """三条前置门：没名字 / 时刻那几格读不出来 / 要上浮层却没填时刻 —— 一律**一个字不落盘**。"""
    plugin, m = _task_manager(qtbot, tmp_path, [_TASK], _task_pool())
    m._on_add()

    m._w["title"].setText("")
    assert "名字" in (m._validate_form() or "")
    m._commit_form()
    assert len(plugin.tasks()) == 1, "没通过校验就不许写盘"

    m._w["title"].setText("喝水")
    m._w["at"].setText("25:99")
    assert "HH:MM" in (m._validate_form() or "")

    # 「显示」与「提醒时间」的依赖：选了要上浮层就得先有时刻（窗口由它算出来），而时刻一旦
    # 被清掉、**选过的那一档还在**（清格 ≠ 改档）⇒ 门必须再响一次。
    m._w["at"].setText("09:30")
    _choose(m._w["show"], "row")
    m._w["at"].setText("")
    assert "提醒时间" in (m._validate_form() or ""), "要上浮层就得先有时刻"


def test_a_task_can_be_deleted_from_the_edit_screen(qtbot, tmp_path):
    """「删除这条」在新模型下删的是**任务表**里那一条（删错表＝看起来成功、重开又回来）。"""
    plugin, m = _task_manager(qtbot, tmp_path, [_TASK, dict(_PLAIN)], _task_pool())
    m._open_edit("stretch")
    m._on_delete()

    assert [t["id"] for t in plugin.tasks()] == ["report"]
    assert m._stack.current() is not m._edit, "删完回列表"


def test_the_scene_field_is_the_condition_the_migration_writes(qtbot, tmp_path):
    """「只在某场景」写出来的是 ``{var: scene, op: "=="}`` —— 与迁移搬过来的**同一条**，
    并且原样回填（填进去的值读不回来 ⇒ 用户每打开一次就被改一次，而他什么都没动）。"""
    plugin, m = _task_manager(qtbot, tmp_path, [_TASK], _task_pool())
    m._open_edit("report")
    m._w["scene"].setText("办公室")
    m._commit_form()

    assert plugin.tasks()[0]["effective"]["conditions"] == [
        {"var": "scene", "op": "==", "value": "办公室"}
    ]
    m._open_edit("report")
    assert m._w["scene"].text() == "办公室", "回填"


def test_a_hand_written_condition_survives_an_edit(qtbot, tmp_path):
    """表单管不着的那几条条件（手写的 ``weekday``）在改别的字段时**原样留着**。

    整份重造的后果是「改一次提醒时间，顺手把星期条件删了」—— 而界面上没有任何迹象。
    """
    task = dict(_TASK, effective={"conditions": [
        {"var": "weekday", "op": "in", "value": [1, 2, 3, 4, 5]},
    ]})
    plugin, m = _task_manager(qtbot, tmp_path, [task], _task_pool())
    m._open_edit("report")
    m._w["title"].setText("提交日报（改过）")
    m._commit_form()

    conditions = plugin.tasks()[0]["effective"]["conditions"]
    assert {"var": "weekday", "op": "in", "value": [1, 2, 3, 4, 5]} in conditions


def test_the_greyed_fields_follow_the_reminder_time(qtbot, tmp_path):
    """没有提醒时刻 ⇒ 「提前 / 延迟」两格灰掉（灰掉 ≠ 删掉，值还在）。

    这两格框的正是 ``[at − lead, at + trail]``：没有 ``at`` 就没有「到点」这回事。
    生效规则那几格**不跟着灰** —— 循环 / 时段 / 场景与提醒在新模型里是正交的；
    「到点执行」与「执行时机」也不跟着灰（2026-10-11 用户裁定：那两格恒可编辑，
    前三个时刻档缺了时刻由保存前那道门来说）。
    """
    _plugin_, m = _task_manager(qtbot, tmp_path, [dict(_PLAIN)], _task_pool())
    m._open_edit("stretch")

    assert not m._w["lead_min"].isEnabled()
    assert not m._w["trail_min"].isEnabled()
    assert m._w["every"].isEnabled(), "循环与提醒正交，不该被牵连"
    assert m._w["command"].isEnabled() and m._w["command_at"].isEnabled(), (
        "「到点执行」与「执行时机」恒可编辑"
    )
    m._w["at"].setText("09:30")
    assert m._w["lead_min"].isEnabled() and m._w["trail_min"].isEnabled()


def test_the_loop_interval_survives_an_untouched_round_trip(qtbot, tmp_path):
    """「循环」那格填着 30 分钟 ⇒ 打开又「确定」盘上仍是 30 分钟（这一块最容易静默丢）。

    盘上写的形状是 ``{"kind": "interval", "every": "30m"}``，而这一格原先按
    ``.get("minutes")`` 直读 —— 那个键**从来没被写过**。于是它回填成「不循环」，用户
    什么都没改按一次「确定」就把整块删掉，界面还显示「已保存」（实测三种形状全中，
    其中「不显示 + 循环」那种连保存门都拦不住，因为 ``show`` 是 ``none``）。

    判据落在**盘上那份逐字段相同**：只看那一格的值会漏掉「写回去把别的键挤掉了」，
    只看它有没有被删又会漏掉「它回的其实是另一个数」。
    """
    task = {
        "id": "drink",
        "title": "喝一次水",
        "display": {"show": "row"},
        "effective": {"loop": {"kind": "interval", "every": "30m"}},
    }
    plugin, m = _task_manager(qtbot, tmp_path, [task], _task_pool())
    m._open_edit("drink")
    assert m._w["every"].value() == 30, "回填没认出盘上那块的形状"
    assert m._validate_form() is None, "这一刻它不算「两样都空着」"

    m._commit_form()
    assert plugin.tasks() == [task], plugin.tasks()


# ── 生效时段：界面上说得出「它有什么用」 ──────────────────────────────────
#
# 「生效时段」从前在 UI 上只有两个输入格：清单行摘要写「限时段」两个糊词（读不出限的
# 是哪一段），编辑屏没有任何字告诉用户「此刻这条算不算数」。下面几条钉住三件事：
# 摘要写出时段本身、编辑屏有一行随输入变的「此刻」状态、两端留空说成「全天候」。

#: 只在白天算数的一条。
_WINDOWED = {
    "id": "daytime",
    "title": "白天的事",
    "effective": {"window": {"from": "09:00", "to": "18:00"}},
}


def test_the_list_row_summary_says_which_hours_the_window_is(qtbot, tmp_path):
    """清单行摘要写出**时段本身**（不再是「限时段」两个糊词）。

    从前的症状：行上读得出「这条限了时段」，读不出限的是哪一段 —— 用户既看不出它限了
    什么，也看不出它有没有起作用。
    """
    _plugin_, m = _task_manager(qtbot, tmp_path, [_WINDOWED], _task_pool())
    assert "09:00–18:00 生效" in _row_summary(m, "白天的事")


def test_the_window_fields_say_whether_the_task_is_in_force_now(qtbot, tmp_path):
    """编辑屏「生效时段」两格下面那一行：**此刻这条算不算数**（注入时刻钉住判据）。

    「它有什么用」从前在界面上一个字都没有 —— 配了 09:00–18:00 的用户在晚上打开这扇窗，
    没有任何字告诉他这条现在不作数。判据与文案走内核（``plugin.window_status``）：
    界面自己再判一遍的话，「清单说不生效、编辑屏说生效」那种不一致不报错。
    """
    _plugin_, m = _task_manager(qtbot, tmp_path, [_WINDOWED], _task_pool())
    m._open_edit("daytime")
    # 进编辑屏那一刻就要有结论（不等用户去动格子）—— 这一句守着重建末尾那次自动同步。
    assert m._win_state is not None
    assert m._win_state.text().startswith("此刻"), m._win_state.text()

    m._sync_window_status(datetime(2026, 10, 12, 21, 3))
    assert m._win_state.text() == "此刻不在时段内（09:00–18:00 生效，现在 21:03）"
    m._sync_window_status(datetime(2026, 10, 12, 12, 0))
    assert m._win_state.text() == "此刻在时段内（09:00–18:00 生效）"


def test_the_window_status_line_follows_what_is_being_typed(qtbot, tmp_path):
    """那一行跟着格子变：打一半的输入就地说明，两端留空说成「全天候」。

    两条文案都与时钟无关（不注入 ``now`` 也成立）—— 同一条用例在上午跑与晚上跑必须
    是同一个结论。
    """
    _plugin_, m = _task_manager(qtbot, tmp_path, [_WINDOWED], _task_pool())
    m._open_edit("daytime")

    m._w["win_from"].setText("9:")
    assert "读不出来" in m._win_state.text(), m._win_state.text()

    m._w["win_from"].setText("")
    m._w["win_to"].setText("")
    assert "全天候" in m._win_state.text(), m._win_state.text()


def test_the_window_fields_round_trip_through_the_edit_screen(qtbot, tmp_path):
    """填进去的两端原样回填、原样落盘（编辑屏重排这两格时最容易静默丢的就是它）。"""
    plugin, m = _task_manager(qtbot, tmp_path, [_WINDOWED], _task_pool())
    m._open_edit("daytime")
    assert (m._w["win_from"].text(), m._w["win_to"].text()) == ("09:00", "18:00")

    m._w["win_from"].setText("07:30")
    m._w["win_to"].setText("21:00")
    m._commit_form()
    assert plugin.tasks()[0]["effective"]["window"] == {"from": "07:30", "to": "21:00"}

    m._open_edit("daytime")
    assert (m._w["win_from"].text(), m._w["win_to"].text()) == ("07:30", "21:00"), "回填"
    # 只填一端也回填（空着的那一头的语义由占位符说，不写成一段猜不出意思的词）
    m._w["win_to"].setText("")
    m._commit_form()
    assert plugin.tasks()[0]["effective"]["window"] == {"from": "07:30"}


def test_a_time_field_takes_a_variable_plus_or_minus_a_duration(qtbot, tmp_path):
    """「时刻」那几格收的是**一段算得出时刻的式子**，不只「一个字面时刻」或「一个变量」。

    用户的原话：「所有能设置变量的地方，不应该都能使用变量增加或者减少时间吗？」——
    内核本来就认（§3.2 的类型表里有 ``时间 ± 时长``），把它挡在外面的只是界面自己那条
    形状正则。这一条钉两头：存得下来，且那一刻的状态行**算得出**它指的是几点。
    """
    plugin, m = _task_manager(qtbot, tmp_path, [_WINDOWED], _task_pool())
    m._open_edit("daytime")
    m._w["win_from"].setText("{off_time}-30m")     # 17:27 − 30m
    m._w["win_to"].setText("{off_time}+1h")        # 17:27 + 1h

    m._sync_window_status(datetime(2026, 10, 12, 17, 0))
    assert "16:57–18:27 生效" in m._win_state.text(), m._win_state.text()

    m._commit_form()
    assert plugin.tasks()[0]["effective"]["window"] == {
        "from": "{off_time}-30m",
        "to": "{off_time}+1h",
    }


def test_an_unreadable_time_field_says_what_the_kernel_said(qtbot, tmp_path):
    """读不出来时**点名原因**（内核那句话），而不是只回一句「要填 HH:MM 或 {变量名}」。

    用户写的 ``{off_time}-20`` 恰恰**就是**「变量」——旧文案把他说对的那半句又念了一遍，
    读完仍不知道自己错在哪。现在这一句来自引擎：「时间 − 数字」没有定义（少了个单位，
    规则表里只有 ``时间±时长``）。同一句话落在两处：状态行、以及保存前那道门。
    """
    plugin, m = _task_manager(qtbot, tmp_path, [_WINDOWED], _task_pool())
    m._open_edit("daytime")
    before = plugin.tasks()
    m._w["win_to"].setText("{off_time}-20")

    assert "没有定义" in m._win_state.text(), m._win_state.text()
    assert "没有定义" in (m._validate_form() or ""), m._validate_form()
    m._commit_form()
    assert plugin.tasks() == before, "没通过校验就不许写盘"

    # 对照组：同一格补上单位 ⇒ 那条门就该放行（诊断不是「一律拒绝长式子」）。
    m._w["win_to"].setText("{off_time}-20m")
    assert m._validate_form() is None, m._validate_form()


# ── 新模型的编辑屏：到点跑一条命令（Phase 5 尾批） ──────────────────


def test_the_command_rows_are_always_editable(qtbot, tmp_path):
    """命令那两格**不跟着任何东西灰**（2026-10-11 用户裁定：「到点执行」应一直允许输入）。

    原先它们跟着「到点弹卡」走 —— 没选那张卡的人连一串命令都打不进去，而界面上没有任何
    线索说明为什么（症状是「这格点了没反应」，与那八枚死开关同一个用户感受）。逐档翻一轮
    「显示」，两格恒亮、那串字也原样还在。

    另一半是**门还在**：三个时刻档缺了「提醒时间」才是真配不出来，那一条由保存前那道门
    说（``test_a_reminder_moment_command_needs_a_reminder_time``）。
    """
    task = {
        "id": "report",
        "title": "提交日报",
        "text": "{off_time} 前提交",
        "display": {"show": "row", "reminder": {"at": "{off_time}"}},
        "command": "open https://example.com",
    }
    _plugin_, m = _task_manager(qtbot, tmp_path, [task], _task_pool())
    m._open_edit("report")

    for face in SHOWS:
        _choose(m._w["show"], face)
        assert m._w["command"].isEnabled(), f"「{face}」那一档下命令格不该灰"
        assert m._w["command_at"].isEnabled(), f"「{face}」那一档下「执行时机」不该灰"

    assert m._w["command"].text() == "open https://example.com", "翻一轮显示不该动那串字"


def test_a_reminder_moment_command_needs_a_reminder_time(qtbot, tmp_path):
    """「执行时机」挑了那三个**时刻**档之一、却没有「提醒时间」⇒ 挡在保存前，一个字不落盘。

    这是「命令恒可编辑」的配套：灰格退场之后，那串**永远算不出时刻**的命令只剩这道门能
    拦。放它过去，用户在浮层上等一辈子（而配置里看着是配好的）。判据要与上面那道
    「显示要有登场时刻」的门分开 —— 故这一条把 ``show`` 留空（默认「不显示」）：
    两边都能说的话，这条测的就成了隔壁那一道。

    「点击完成」那一档不归这道门管（它压根不需要提醒时刻），对照组与上面那条同一个理由。
    """
    task = {
        "id": "report",
        "title": "提交日报",
        "text": "{off_time} 前提交",
        "command": "open https://example.com",
        "command_at": "lead",
    }
    plugin, m = _task_manager(qtbot, tmp_path, [task], _task_pool())
    m._open_edit("report")

    assert "提醒时间" in (m._validate_form() or ""), m._validate_form()
    m._commit_form()
    assert plugin.tasks() == [task], "没通过校验就不许写盘"

    # 对照组：同一串命令、同样空着的时刻，只把「执行时机」换成「点击完成」⇒ 门就不该响。
    _choose(m._w["command_at"], "done")
    assert m._validate_form() is None, m._validate_form()


def test_a_command_survives_an_untouched_round_trip(qtbot, tmp_path):
    """带命令的任务打开又「确定」⇒ 盘上那份逐字段相同（``command_at`` 也要守恒）。

    ``command_at`` 的缺省是 ``reminder`` ⇒ 盘上没这个键的那一份写回去不许凭空多出它
    （多出来不会报错，但「用户什么都没动」这句话就不成立了）；写了 ``done`` 的那一份
    则要原样回来 —— 只有一个方向都对，用户才不必每次进编辑屏重挑一遍。
    """
    explicit = {
        "id": "report",
        "title": "提交日报",
        "text": "{off_time} 前提交",
        "display": {"show": "row", "reminder": {"at": "{off_time}"}},
        "command": "open https://example.com",
        "command_at": "done",
    }
    plugin, m = _task_manager(qtbot, tmp_path, [explicit], _task_pool())
    m._open_edit("report")
    assert m._w["command_at"].current() == "done", "非缺省那一档要按原样回填"
    m._commit_form()
    assert plugin.tasks() == [explicit], plugin.tasks()

    default = dict(explicit)
    default.pop("command_at")
    plugin2, m2 = _task_manager(qtbot, tmp_path, [default], _task_pool())
    m2._open_edit("report")
    assert m2._w["command_at"].current() == "reminder", "缺省档回落到第一项"
    m2._commit_form()
    assert plugin2.tasks() == [default], plugin2.tasks()


def test_the_task_form_refuses_a_command_it_could_not_split(qtbot, tmp_path):
    """引号不配对的命令**切不出参数** ⇒ 挡在保存前，一个字都不落盘。

    放它过去的后果：运行期那边只记一条 WARNING（离用户很远），而用户看到的是
    「它没跑」；用户会去查网络、查权限，不会去数引号。切分口径就是真跑时用的那一份
    （``core.hotkey_command.split_command``），不是界面自己又写一遍的近似。
    """
    plugin, m = _task_manager(qtbot, tmp_path, [_TASK], _task_pool())
    m._open_edit("report")
    m._w["command"].setText('open "https://example.com')

    assert "引号" in (m._validate_form() or ""), m._validate_form()
    m._commit_form()
    assert plugin.tasks() == [dict(_TASK)], "没通过校验就不许写盘"

    # 合规形状：带引号参数的命令是**正常**写法（空格靠引号分组），不许误报。
    m._w["command"].setText('open -a "Safari" https://example.com')
    assert m._validate_form() is None


def test_the_more_menu_of_a_task_has_no_ask_now(qtbot, tmp_path):
    """新模型下「⋯ 更多」= **预览这一条 ＋ 标记今天完成**，且**没有「现在问我」**。

    「现在问我」在旧条目上是有的（那一条自己带 ``ask`` 那一格，见上面
    ``test_the_more_menu_lists_only_what_this_entry_can_do``）；新模型里**询问不归任何
    任务** —— 它是变量自己的触发条件（§4.3），任务只是引用那个变量。留一枚在这里要么
    问错对象、要么得往任务里再养一条询问，两条路都与那笔解耦相反。

    做成对照组的，是**旧模型那一份菜单照旧带「现在问我」**：只写新模型这一半的话，
    把 ``_task_more_items`` 写成 ``return []`` 也绿。
    """
    _old_p, old_m = _manager(qtbot, tmp_path, [_item()])
    old_m._open_edit(old_m._items[0]["id"])
    assert "现在问我" in _menu_labels(old_m), "旧条目那一份菜单不该被这次改动牵连"

    plugin, m = _task_manager(qtbot, tmp_path, [_TASK], _task_pool())
    m._open_edit("report")

    assert _menu_labels(m) == ["预览这一条", "标记今天完成"]
    assert not any("现在问" in lbl for lbl in _menu_labels(m)), "新模型里询问不是任务的事"

    plugin.set_done("report")
    assert _menu_labels(m) == ["预览这一条", "撤销今天完成"], "做完之后那枚该翻成「撤销」"


def test_an_unsaved_task_offers_only_the_preview(qtbot, tmp_path):
    """还没保存的任务只剩「预览这一条」—— 「标记今天完成」要往 ``done_at(id)`` 写，没 id
    就写不进去，先画出来是**骗人**（与旧条目那一条同一口径：按「此刻做不做得出」过滤）。

    「预览」恒在：未保存的也预览得出来（内核给它补一个假 id），这正是「不用保存」那句
    hint 的底气。
    """
    _plugin_, m = _task_manager(qtbot, tmp_path, [], _task_pool())
    m._on_add()
    assert m._draft is not None and not m._draft.get("id")

    assert _menu_labels(m) == ["预览这一条"]
