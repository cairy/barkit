"""shell/variables_page 测试（变量池与任务解耦 · Phase 2 的框架「变量」页）。

判据分两面：**页面怎么写回配置**（``set_variables`` 收到的那份是不是用户在界面上填的
那份）与**页面怎么读**（列出来的声明与值来自注入的那个口，不是页面自己去翻全局）。
「值从哪来」这一条尤其要守：页面**没有** host 时画破折号 —— 那正是「不假装有值」。

页面里那几组字段的显隐只断言 ``isHidden()``：控件还没 ``show()`` 时 ``isVisible()``
恒为 False，拿它判显隐会把「显式隐藏了」与「整页还没上屏」混成一件事。
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QLabel, QPushButton

from barkit.core.variables import spec as spec_mod
from barkit.shell.ui_kit import RecordRow
from barkit.shell.variables_page import VariablesPage, VariablesHost, _VariableEditor

DECLS = [
    {"id": "start_time", "name": "上班时间", "source": "ask", "type": "time",
     "trigger": {"when": ["daily"],
                 "conditions": [{"var": "scene", "op": "==", "value": "办公室"}]},
     "prompt": "今天几点上班？"},
    {"id": "off_time", "name": "下班时间", "source": "derived", "expr": "start_time + 9h"},
    {"id": "scene", "name": "当前场景", "source": "component",
     "component": "route_guard", "key": "profile", "field": "summary"},
]


class _Config:
    """配置桩：只留「变量」页读写的那两个口（``variables`` / ``set_variables``）。"""

    def __init__(self, declarations=None):
        self._items = [dict(x) for x in (declarations or [])]
        self.writes: list = []

    def variables(self):
        return list(self._items)

    def set_variables(self, declarations):
        self._items = [dict(x) for x in declarations]
        self.writes.append([dict(x) for x in declarations])


class _Host:
    """取值口桩：行数据、**现算一次**、弹询问三件都记账。

    ``probe_value`` 刻意与 ``rows`` 里那份**不一样**：页面该报的是现算出来的那一份，
    两处同值的话「测试」退化成一枚复读按钮也照样绿（见
    ``test_the_test_button_computes_now_instead_of_repeating_the_row``）。
    """

    def __init__(self, rows=None, probe_value="（桩算出来的）"):
        self._rows = list(rows or [])
        self._probe_value = probe_value
        self.asked: list = []
        self.probed: list = []
        self.changed = 0

    def rows(self):
        return list(self._rows)

    def probe(self, var_id):
        self.probed.append(var_id)
        return self._probe_value

    def ask_now(self, var_id):
        self.asked.append(var_id)
        return True

    def problems(self):
        return ("off_time：引用了不存在的变量「nope」，本条已跳过",)

    def on_changed(self):
        self.changed += 1


def _page(qtbot, *, config=None, host=None, declarations=None):
    """建页。注入的取值口一律**包成真的** :class:`VariablesHost`。

    桩只管记账，字段名却必须与生产那份一致 —— 包一道就等于让「改名 / 少一格」在这批
    用例里当场转红（页面按名字调那四个，写错一个只会静默失效）。
    """
    if host is not None:
        host = VariablesHost(
            rows=host.rows,
            probe=host.probe,
            ask_now=host.ask_now,
            problems=host.problems,
            on_changed=host.on_changed,
        )
    page = VariablesPage(
        config=config or _Config(DECLS if declarations is None else declarations),
        host=host,
    )
    qtbot.add_widget(page)
    return page


def _rows(page) -> "list[RecordRow]":
    return page._list.findChildren(RecordRow)


def _texts(widget) -> str:
    """这一屏「说出来的话」：标签正文 **＋ 悬停提示**。

    副标题是省略标签（``ElideLabel``），全文按 ``RecordRow.set_subtitle`` 的契约放在
    悬停提示里 —— 而离屏那点宽度会把「``off_time · start_time + 9h``」截成
    「``off_time · start…``」。只读正文等于在断言**截断后的那一份**，量到的是布局宽度，
    不是这一行说了什么。
    """
    return " ".join(
        f"{lbl.text()} {lbl.toolTip()}".strip() for lbl in widget.findChildren(QLabel)
    )


def _button(root, text: str) -> "QPushButton | None":
    return next((b for b in root.findChildren(QPushButton) if b.text() == text), None)


# ── 列表屏 ──────────────────────────────────────────────────────────


def test_without_a_host_it_lists_the_declarations_and_fakes_no_value(qtbot):
    """没注入取值口（未装配 / 单测）⇒ 照列声明，**值那格画破折号**。

    「不假装有值」这条要守：画一个空串会被读成「这个变量的值是空的」，而事实是
    「这一页此刻读不到值」——两件事在排查时指向完全不同的方向。
    """
    page = _page(qtbot)
    assert len(_rows(page)) == 3
    assert "—" in _texts(page)
    assert "start_time" in _texts(page)


def test_with_a_host_it_lists_the_rows_it_gives_including_builtins(qtbot):
    specs, _ = spec_mod.parse_all(DECLS)
    rows = [(s, {"start_time": "09:00", "off_time": "18:00", "scene": "办公室"}[s.id])
            for s in specs]
    rows += [(s, {"now": "11:24:05", "date": "2026-10-10", "weekday": "5"}[s.id])
             for s in spec_mod.builtin_specs()]
    page = _page(qtbot, host=_Host(rows))
    assert len(_rows(page)) == 6
    assert "09:00" in _texts(page)
    assert "18:00" in _texts(page)


def test_the_derived_row_shows_the_expression_next_to_its_value(qtbot):
    """算出型那一行把表达式一起亮出来 —— 用户要能对上「这个值是怎么来的」。"""
    specs, _ = spec_mod.parse_all(DECLS)
    page = _page(qtbot, host=_Host([(s, "18:00" if s.id == "off_time" else "") for s in specs]))
    assert "start_time + 9h" in _texts(page)


def test_a_broken_declaration_is_told_on_the_page_not_just_in_the_log(qtbot):
    """加载时跳过的那几条**如实列在页首**：只在日志里说等于没说 —— 用户在配置里写错
    一条，界面上什么也看不出来，只有那一条静静地不生效。"""
    page = _page(qtbot, host=_Host([]))
    assert "引用了不存在的变量" in page._warn.text()
    assert not page._warn.isHidden()


# ── 编辑屏：新增与保存 ───────────────────────────────────────────────


def test_adding_a_variable_writes_only_the_declaration(qtbot):
    config, host = _Config(DECLS), _Host()
    page = _page(qtbot, config=config, host=host)
    _button(page, "+ 新增").click()

    editor = page._stack.current()
    assert isinstance(editor, _VariableEditor)
    editor._id.setText("work_span")
    editor._name.setText("工作时长")
    editor._expr.setText("9h")
    editor.screen.save_button.click()

    assert len(config.writes) == 1
    added = [d for d in config.writes[0] if d["id"] == "work_span"][0]
    assert added == {"id": "work_span", "name": "工作时长", "source": "derived", "expr": "9h"}
    assert host.changed == 1, "写回之后要通知变量池按新声明重来一遍"
    assert page._stack.current() is page._stack.list_page(), "保存后回列表屏"


def test_saving_an_edited_variable_replaces_the_old_one(qtbot):
    config = _Config(DECLS)
    page = _page(qtbot, config=config)
    spec, _ = spec_mod.parse_all([DECLS[1]])
    page._open_editor(spec[0])

    editor = page._stack.current()
    editor._expr.setText("start_time + 8h")
    editor.screen.save_button.click()

    saved = config.writes[-1]
    assert [d["id"] for d in saved] == ["start_time", "off_time", "scene"]
    assert [d for d in saved if d["id"] == "off_time"][0]["expr"] == "start_time + 8h"


def test_renaming_takes_the_old_id_away(qtbot):
    """改名 ⇒ 旧 id 那条**摘掉**（不是留两条：一条旧 id、一条新的），且留在原位。"""
    config = _Config(DECLS)
    page = _page(qtbot, config=config)
    spec, _ = spec_mod.parse_all([DECLS[1]])
    page._open_editor(spec[0])

    editor = page._stack.current()
    editor._id.setText("off_at")
    editor.screen.save_button.click()

    ids = [d["id"] for d in config.writes[-1]]
    assert ids == ["start_time", "off_at", "scene"]


def test_renaming_an_ask_variable_drives_an_untouched_prompt_with_it(qtbot):
    """问句还是**按旧名自动生成的那一句**、名字被改 ⇒ 问句跟着名字走。

    2026-10-10 用户报的场景：第一版迁移把问句写成「下班时间？」，用户把名字改成
    「上班时间」后弹窗仍问下班 —— 改的是名不是问句，旧问句留在盘上就成了另一个
    变量的名字。这条规则只认「问句 == 按旧名生成的那一句」这一种形状。
    """
    decl = {"id": "start_time", "name": "下班时间", "source": "ask", "type": "time",
            "trigger": {"when": ["startup"]}, "prompt": "下班时间？"}
    config = _Config([decl])
    page = _page(qtbot, config=config)
    spec, _ = spec_mod.parse_all([decl])
    page._open_editor(spec[0])

    editor = page._stack.current()
    editor._name.setText("上班时间")
    editor.screen.save_button.click()

    saved = [d for d in config.writes[-1] if d["id"] == "start_time"][0]
    assert saved["prompt"] == "上班时间？"


def test_a_hand_written_prompt_survives_a_rename(qtbot):
    """问句是**用户自己写的**（不是按名生成的那句）⇒ 改名时一个字不动。"""
    decl = {"id": "start_time", "name": "上班时间", "source": "ask", "type": "time",
            "trigger": {"when": ["startup"]}, "prompt": "几点到岗的？"}
    config = _Config([decl])
    page = _page(qtbot, config=config)
    spec, _ = spec_mod.parse_all([decl])
    page._open_editor(spec[0])

    editor = page._stack.current()
    editor._name.setText("到岗时间")
    editor.screen.save_button.click()

    saved = [d for d in config.writes[-1] if d["id"] == "start_time"][0]
    assert saved["prompt"] == "几点到岗的？"


# ── 编辑屏：校验那一道门 ─────────────────────────────────────────────


@pytest.mark.parametrize("bad_id,expect", [
    ("", "不能空"),
    ("9abc", "字母 / 数字 / 下划线"),
    ("now", "内置变量"),
    ("off_time", "已经有叫"),
])
def test_a_bad_id_is_refused_before_anything_is_written(qtbot, bad_id, expect):
    config = _Config(DECLS)
    page = _page(qtbot, config=config)
    _button(page, "+ 新增").click()
    editor = page._stack.current()
    editor._expr.setText("1h")
    editor._id.setText(bad_id)
    editor.screen.save_button.click()

    assert expect in editor.feedback.text()
    assert config.writes == [], "校验没过却写了盘"


@pytest.mark.parametrize("expr_text,expect", [
    ("", "必须写表达式"),
    ("start_time +", "表达式写错了"),
    ("nope + 1h", "引用了不存在的变量"),
])
def test_a_bad_expression_is_refused(qtbot, expr_text, expect):
    config = _Config(DECLS)
    page = _page(qtbot, config=config)
    _button(page, "+ 新增").click()
    editor = page._stack.current()
    editor._id.setText("brand_new")
    editor._expr.setText(expr_text)
    editor.screen.save_button.click()

    assert expect in editor.feedback.text()
    assert config.writes == []


def test_an_ask_with_neither_a_moment_nor_a_condition_is_refused(qtbot):
    """时机与条件都空 ⇒ 一登记就会弹一次 —— 拦在保存前，别让它落到盘上。"""
    config = _Config(DECLS)
    page = _page(qtbot, config=config)
    _button(page, "+ 新增").click()
    editor = page._stack.current()
    editor._id.setText("when_ever")
    editor._source.setCurrentIndex(1)                    # ask
    assert editor._derived.isHidden() and not editor._ask.isHidden()

    editor.screen.save_button.click()
    assert "至少选一个时机" in editor.feedback.text()
    assert config.writes == []


# ── 编辑屏：来源切换 ─────────────────────────────────────────────────


def test_only_the_group_of_the_current_source_is_shown(qtbot):
    page = _page(qtbot)
    _button(page, "+ 新增").click()
    editor = page._stack.current()

    editor._source.setCurrentIndex(1)                    # ask
    assert not editor._ask.isHidden()
    assert editor._derived.isHidden() and editor._component.isHidden()

    editor._source.setCurrentIndex(2)                    # component
    assert not editor._component.isHidden()
    assert editor._derived.isHidden() and editor._ask.isHidden()


def test_component_declaration_carries_the_three_fields(qtbot):
    config = _Config(DECLS)
    page = _page(qtbot, config=config)
    _button(page, "+ 新增").click()
    editor = page._stack.current()
    editor._id.setText("vpn")
    editor._source.setCurrentIndex(2)
    editor._component_id.setText("vpn_guard")
    editor._component_key.setText("link")
    editor.screen.save_button.click()

    added = [d for d in config.writes[0] if d["id"] == "vpn"][0]
    assert added["component"] == "vpn_guard" and added["key"] == "link"
    assert added["field"] in spec_mod.FIELDS


def test_an_ask_keeps_its_trigger_shape(qtbot):
    """ask 那三格（时机 / 条件 / 已填不问）原样落进声明 —— 形状由 ``spec`` 一侧定义。"""
    config = _Config(DECLS)
    page = _page(qtbot, config=config)
    _button(page, "+ 新增").click()
    editor = page._stack.current()
    editor._id.setText("lunch_at")
    editor._source.setCurrentIndex(1)
    editor._prompt.setText("几点吃午饭？")
    editor._whens["daily"].setChecked(True)
    editor.screen.save_button.click()

    added = [d for d in config.writes[0] if d["id"] == "lunch_at"][0]
    assert added["type"] == "time"
    assert added["prompt"] == "几点吃午饭？"
    assert added["trigger"] == {"when": ["daily"]}


# ── 删除 ────────────────────────────────────────────────────────────


def test_deleting_asks_inline_and_writes_only_after_the_answer(qtbot):
    """删除不可撤销 ⇒ **行内确认**（零模态），且答之前一个字节都不动。"""
    config = _Config(DECLS)
    page = _page(qtbot, config=config)
    spec, _ = spec_mod.parse_all([DECLS[2]])
    page._open_editor(spec[0])
    editor = page._stack.current()

    _button(editor, "删除这条").click()
    assert editor.feedback.is_pending()
    assert config.writes == [], "还没确认就写了"

    editor.feedback.accept()
    assert [d["id"] for d in config.writes[-1]] == ["start_time", "off_time"]


def test_the_builtin_variables_cannot_be_edited_or_deleted(qtbot):
    """内置那三只：保存键与删除键都不出现（点进去只是**看**它的来源与取值口径）。"""
    page = _page(qtbot)
    page._open_editor(spec_mod.builtin_specs()[0])
    editor = page._stack.current()

    assert editor.screen.save_button.isHidden()
    assert _button(editor, "删除这条") is None
    assert editor._source.currentData() == "framework"


# ── 「测试」 ─────────────────────────────────────────────────────────


def test_the_test_button_on_an_ask_row_asks_instead_of_computing(qtbot):
    """ask 型按「测试」＝**弹一次询问卡**（原「刷新询问」那一路），不是去算。

    它不看条件、也不看去重账（走中枢的临时 key）：用户是「现在就想填一个值」才按的。
    """
    host = _Host([(s, "") for s in spec_mod.parse_all(DECLS)[0]])
    page = _page(qtbot, host=host)
    ask = _button(page, "测试")
    assert ask is not None
    ask.click()
    assert host.asked == ["start_time"]
    assert host.probed == [], "ask 型不该去现算 —— 它的值只能问出来"
    assert "弹出来了" in page._fb.text()


def test_no_test_button_without_a_placeholder_for_getting_a_value(qtbot):
    """没注入取值口（未装配）⇒ 不画那枚按钮：**画一个点了没反应的按钮是骗人**。"""
    page = _page(qtbot, host=None)
    assert _button(page, "测试") is None


def test_the_test_button_on_an_ask_row_reports_a_refusal_instead_of_pretending(qtbot):
    class _Refusing(_Host):
        def ask_now(self, var_id):
            return False

    page = _page(qtbot, host=_Refusing([(s, "") for s in spec_mod.parse_all(DECLS)[0]]))
    _button(page, "测试").click()
    assert "弹不出来" in page._fb.text()


def test_the_test_button_computes_now_instead_of_repeating_the_row(qtbot):
    """非 ask 型按「测试」＝**现在算一遍**并把结果说出来。

    判据落在「两处值不同」上（行数据 ``09:00``、现算答 ``17:27``）：只断言「有反馈」
    的话，把现算换成复读行上那份旧的也能绿 —— 而「复读」正是这枚键要免掉的东西
    （用户常常是刚改完表达式就来按它）。
    """
    rows = [(s, "09:00") for s in spec_mod.parse_all(DECLS)[0]]
    host = _Host(rows, probe_value="17:27")
    page = _page(qtbot, host=host)
    off_row = next(r for r in _rows(page) if "off_time" in _texts(r))
    _button(off_row, "测试").click()
    assert host.probed == ["off_time"], "问的是这一行那一条"
    assert "17:27" in page._fb.text()
    assert "09:00" not in page._fb.text(), "报的必须是现算的那份，不是行上那份"


# ── 返回（列表屏 ⇄ 编辑屏） ──────────────────────────────────────────


def test_the_back_button_returns_to_the_list_and_takes_the_editor_away(qtbot):
    """「返回」要**回到列表**、且把编辑屏**摘掉**（2026-10-10 用户报「点了没反应」）。

    只断言「当前屏变回列表」抓不住真机那个症状：``EditStack.back()`` 也能切回去，但编辑屏
    还留在 ``QStackedWidget`` 里 —— 而它取**所有页的最大高度**，于是列表被撑在编辑屏那个
    高度上、设置窗外层滚动区的内容不缩回去，用户滚到下面时看到的是同一片空白，读起来就是
    「这按钮坏了」。判据落在**根因**上（那一个实例有没有被摘掉），不去量高度 —— 高度是
    「两屏谁高」的函数，变量少的时候列表本来就矮，量出来的差值是场景相关的、不稳
    （离屏未 ``show`` 时两屏的 ``sizeHint`` 高度实测恰好相等，拿它当判据就是一条假绿）。
    """
    page = _page(qtbot, host=None)
    page._open_editor(None)
    editor = page._stack.current()
    assert editor is not page._stack.list_page()

    back = _button(editor, "‹ 返回")
    assert back is not None, "编辑屏顶上该有一枚返回"
    back.click()

    assert page._stack.current() is page._stack.list_page(), "点了返回该回列表"
    assert page._stack._stack.count() == 1, "编辑屏该被摘掉，不许一个叠一个留在栈里"


def test_going_in_and_out_does_not_pile_up_screens(qtbot):
    """反复进出不许往栈里叠屏：每次进都是**新实例**，旧的若留着就是几个叠在一起
    （高度取最大 ⇒ 返回后屏不会矮；实例与信号也一起漏）。"""
    page = _page(qtbot, host=None)
    for _ in range(3):
        page._open_editor(None)
        _button(page._stack.current(), "‹ 返回").click()
    assert page._stack._stack.count() == 1


def test_the_source_groups_are_laid_out_and_back_is_really_clickable(qtbot):
    """三个来源组必须**进版面**，「返回」必须**真被鼠标点得到**（2026-10-10 用户真机报障）。

    第一版用 ``form_group(title, 宿主, …)`` 手搓那三组 —— 造出来的分组只挂在宿主上、
    **从没进过** ``EditScreen`` 的内容布局：真机上整组叠在 (0,0) 的默认几何上把顶栏
    「‹ 返回」盖死（鼠标全落在那块盒子上 ＝「点击无效」），「问什么」输入框更悬空、
    永不显示。此前的用例全拿 ``button.click()`` 直呼 —— **绕过鼠标命中测试**，所以
    一直绿。故这里两道判据都取真机形状：先 ``show()`` 再断 ``isVisible()``，再用
    ``QApplication.widgetAt`` 量返回键中心的最顶层 widget。
    """
    from PySide6.QtWidgets import QApplication, QLineEdit

    page = _page(qtbot, config=_Config(DECLS))
    page.resize(620, 640)
    page.show()
    spec, _ = spec_mod.parse_all([DECLS[0]])
    page._open_editor(spec[0])
    editor = page._stack.current()

    assert editor._prompt.isVisible(), "「问什么」必须真的在屏上（曾经整组没进版面）"
    assert editor._prompt.text() == "今天几点上班？"
    assert editor._conditions.isVisible() and editor._conditions.height() > 0
    assert editor._whens["daily"].isVisible()
    # 三个组互斥显隐，且**都在版面里**：来源切到 derived，「怎么算」那组要现出来。
    editor._source.setCurrentIndex(0)  # derived 在列表最前（_build 的顺序）
    assert editor._expr.isVisible(), "「表达式」那组也得进版面"
    assert len(editor.findChildren(QLineEdit)) >= 5

    back = next(b for b in editor.findChildren(QPushButton) if "返回" in b.text())
    top = QApplication.widgetAt(back.mapToGlobal(back.rect().center()))
    assert top is back or back.isAncestorOf(top), (
        f"返回键被 {type(top).__name__} 盖住了 —— 真机上点鼠标就落在它身上"
    )
