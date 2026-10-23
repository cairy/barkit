"""tests/test_todo：到点提醒的待办清单（条件触发询问机制批 4 的第一个真用例）。

守的是这条链路上**会静默出错**的四处：

- **登记形状**：一条条目翻成一条 ``daily_at`` 规则、去重档必须是 ``once_per_day``
  —— 换成 clock 那条 ``until_answered_day`` 会让没答的待办每次启动再弹一遍，
  而那恰恰是「待办」这个用例最不需要的行为（今天没答 ＝ 这件事今天就算过了）；
- **登记不进去的条目**：禁用的 / 时间写歪的 / 还没保存的都不许登记 —— 登记了却永远
  触发不了是最难自查的那种坏（设置页看着是好的）；
- **完成记录是数据不是配置**：落数据文件、进 ``resources()``、**不进配置段**
  （每天写一次用户配置会触发 ``config.changed`` 广播，还会被「恢复默认配置」冲掉）；
- **「拉」的那条路永远能写**：用户自己勾完成（``set_done``）不查去重账 —— 去重账挡的是
  弹窗，不挡用户自己动手（方案 §2.2b）。

条件求值 / 去重账 / 排队那半在 ``test_interaction*.py``，本文件不重复测它们。
"""

from __future__ import annotations

import json

from datetime import datetime

from barkit import paths
from barkit.core.config import Config
from barkit.core.interaction import (
    COND_DAILY_AT,
    DEDUPE_DAY,
    PROMPT_CHOICE,
    RESULT_ANSWERED,
    RESULT_DISMISSED,
    RESULT_LATER,
    RESULT_TIMEOUT,
    InteractionResult,
)
from barkit.core.plugin import PluginContext
from barkit.plugins.todo.day_log import AGENDA_FILENAME, KEEP_DAYS
from barkit.plugins.todo.plugin import (
    DONE_OPTION,
    Plugin,
)


def _item(**kw) -> dict:
    base = {"id": "td1", "enabled": True, "at": "09:30", "title": "喝水"}
    base.update(kw)
    return base


class _FakeCenter:
    """中枢替身：只记「交上来了什么」，不做任何窗口动作。"""

    def __init__(self) -> None:
        self.registered: "list[tuple]" = []
        self.manual: "list[tuple]" = []
        self.unregistered: "list[str]" = []
        #: key → 「这条提醒弹出来了」的回调（见 ``shell.interaction`` 的 ``register``）。
        self.shown: "dict[str, object]" = {}

    def register(self, spec, on_result=None, on_shown=None) -> None:
        self.registered.append((spec, on_result))
        # 「提醒登场」那一枚单收一处：现有的断言都在数 ``(spec, on_result)`` 两元组，
        # 往里塞第三格会让三十来处解包全要改（而它们与这条新回调毫无关系）。
        if on_shown is not None:
            self.shown[spec.prompt.key] = on_shown

    def ask_now(self, spec, on_result=None) -> bool:
        self.manual.append((spec, on_result))
        return True

    def unregister(self, plugin_id: str) -> None:
        self.unregistered.append(plugin_id)

    def note_answered(self, key: str) -> None:
        pass


def _plugin(tmp_path, items=None, center=None) -> Plugin:
    ctx = PluginContext(config=Config(user_path=tmp_path / "c.yaml"), events=None)
    if center is not None:
        ctx.interaction = center
    plugin = Plugin(ctx)
    if items is not None:
        assert plugin.save_items(items) is True
    return plugin


def _write_log(days: dict) -> None:
    """直接写一份「当日完成记录」（跨天 / 去尾这两条只能这么造）。"""
    path = paths.APP_SUPPORT / AGENDA_FILENAME
    path.write_text(json.dumps({"days": days}, ensure_ascii=False), encoding="utf-8")


# ── 登记形状：一条条目 ⇒ 一条 daily_at 规则 ────────────────────────────

def test_each_enabled_item_becomes_one_daily_at_rule(tmp_path):
    center = _FakeCenter()
    _plugin(tmp_path, [_item(), _item(id="td2", at="18:00", title="下班")], center)
    assert center.unregistered == ["todo"], "整批重登记必须先撤干净，不留孤儿规则"
    assert len(center.registered) == 2, "两个启用条目 ⇒ 两条规则"

    spec, callback = center.registered[0]
    assert spec.plugin_id == "todo", "归属由插件自己填（框架不再兜）"
    assert spec.prompt.key == "todo.item.td1", "去重键必须按条目分开，否则两条共享一笔账"
    assert spec.prompt.kind == PROMPT_CHOICE
    assert spec.prompt.options == (DONE_OPTION,)
    assert spec.prompt.title == "喝水"
    (cond,) = spec.conditions
    assert cond.kind == COND_DAILY_AT
    assert cond.at == "09:30", "条目的时刻必须原样搬进条件，不能是别的条目的"
    assert callable(callback)


def test_dedupe_is_once_per_day_not_until_answered(tmp_path):
    """**本用例与 clock 的分歧点**：待办是「弹过即记」，不是「答了才记」。

    这条判据看着只是个常量比对，实际钉的是行为：换成 ``until_answered_day`` 之后，
    今天关掉没答的那条待办会在**每次启动**再弹一遍 —— 一天问五遍同一件没做的事。
    """
    center = _FakeCenter()
    _plugin(tmp_path, [_item()], center)
    assert center.registered[0][0].dedupe == DEDUPE_DAY


def test_disabled_illegal_and_unsaved_items_are_not_registered(tmp_path):
    """三类**登记了也永远触发不了**的条目必须挡在登记之前。"""
    center = _FakeCenter()
    _plugin(
        tmp_path,
        [
            _item(id="off", enabled=False),
            _item(id="bad", at="25:00"),
            _item(id="empty", at=""),
            {"enabled": True, "at": "08:00", "title": "没有 id"},
        ],
        center,
    )
    keys = [spec.prompt.key for spec, _ in center.registered]
    assert keys == [], f"这些条目一个都不该登记，实际登记了 {keys}"


def test_rewriting_items_reregisters_from_scratch(tmp_path):
    """改配置 ⇒ 先撤再登，且**新登记的内容真的换了**（不是把旧的留着）。"""
    center = _FakeCenter()
    plugin = _plugin(tmp_path, [_item()], center)
    assert len(center.registered) == 1

    assert plugin.save_items([_item(at="07:15")]) is True
    assert center.unregistered == ["todo", "todo"]
    assert len(center.registered) == 2, "旧那条必须已被撤掉（队列里不该留旧声明）"
    assert center.registered[1][0].conditions[0].at == "07:15"


def test_no_center_means_registration_is_a_no_op(tmp_path):
    """未接线（测试桩 / 无中枢）时不许抛 —— 规则登记退化为空操作。"""
    plugin = _plugin(tmp_path, [_item(), _item(id="td2")])
    assert plugin.items()[0]["id"] == "td1"


# ── 完成记录：数据文件，不进配置段 ────────────────────────────────────

def test_resources_declare_the_log_file_only(tmp_path):
    plugin = _plugin(tmp_path)
    resources = plugin.resources()
    assert AGENDA_FILENAME in resources.data_files


def test_the_log_never_lands_in_the_config_section(tmp_path):
    """当日记账是**数据**不是配置：写完之后配置段里一个相关键都不许有。

    漏进配置段的代价不是多一行 —— 每天写一次用户配置会触发 ``config.changed`` 广播
    （全框架跟着重算一遍），还会被「恢复默认配置」冲掉。这条判据原先挂在旧翻页钟那
    一侧（它也是每天写一次）；批 5 载体换成这里：**账表的键一个都不许出现在这一层**。
    """
    plugin = _plugin(tmp_path, [_item()])
    assert plugin.set_done("td1") is True

    section = plugin.ctx.config.user_plugin_section(plugin.id)
    assert "days" not in section and "done" not in section, (
        f"当日记账漏进了配置段：{sorted(section)}"
    )
    assert section.get("items"), "条目表仍应在配置里（那本来就是配置）"


def test_answer_writes_the_record_and_marks_the_time(tmp_path):
    center = _FakeCenter()
    plugin = _plugin(tmp_path, [_item()], center)
    _, callback = center.registered[0]

    callback(InteractionResult(True, DONE_OPTION, RESULT_ANSWERED))
    assert plugin.done_at("td1") != "", "答了「完成」就该记上"
    assert plugin.done_today() == {"td1": plugin.done_at("td1")}


def test_other_answers_do_not_write_anything(tmp_path):
    """没理 / 超时 / 被关 / 稍后**一律不写** —— 那几种的去重账各自由框架记过了。"""
    center = _FakeCenter()
    plugin = _plugin(tmp_path, [_item()], center)
    _, callback = center.registered[0]

    callback(InteractionResult(False, None, RESULT_TIMEOUT))
    callback(InteractionResult(False, None, RESULT_DISMISSED))
    callback(InteractionResult(False, None, RESULT_LATER))
    callback(InteractionResult(True, "别的东西", RESULT_ANSWERED))
    assert plugin.done_at("td1") == ""


def test_yesterdays_record_does_not_count_as_todays(tmp_path):
    """跨天归零：昨天完成过，今天照样要从 0 算起。"""
    plugin = _plugin(tmp_path, [_item()])
    _write_log({"2026-01-01": {"td1": "09:00"}})
    assert plugin.done_today() == {}
    assert plugin.done_at("td1") == ""
    assert plugin._snapshot().state == "pending"


def test_log_is_trimmed_to_the_recent_days(tmp_path):
    plugin = _plugin(tmp_path, [_item()])
    old = {f"2026-01-{i:02d}": {"td1": "09:00"} for i in range(1, 41)}
    _write_log(old)
    assert plugin.set_done("td1") is True

    saved = plugin._load_log()
    assert len(saved) == KEEP_DAYS
    assert "2026-01-40" in saved, "最近的那几天必须留着"
    assert "2026-01-01" not in saved


def test_corrupt_log_reads_as_empty_not_crash(tmp_path):
    """记录坏了（手改 / 断电）⇒ 按空处理并继续说：最坏是今天多问一次，不是崩。"""
    (paths.APP_SUPPORT / AGENDA_FILENAME).write_text("{不是 JSON", encoding="utf-8")
    plugin = _plugin(tmp_path, [_item()])
    assert plugin.done_today() == {}
    assert plugin.set_done("td1") is True
    assert plugin.done_at("td1") != ""


def test_clearing_the_record_undoes_todays_mark(tmp_path):
    """``set_done(id, "")`` ＝ 撤销（设置页那枚按钮的「撤销今天完成」那一档）。"""
    plugin = _plugin(tmp_path, [_item()])
    assert plugin.set_done("td1") is True
    assert plugin.done_at("td1") != ""
    assert plugin.set_done("td1", "") is True
    assert plugin.done_at("td1") == ""


# ── 面板那一行 ────────────────────────────────────────────────────────

def test_snapshot_has_three_distinct_states(tmp_path):
    """三档：没有待办 / 有但没做完 / 今天全做完。**三档都不上 WARN**（待办没做完是常态）。

    文案是**新口径**（设计文档 §4.6 B）：报「未完成 N 项」，不再报「今天完成 X/Y」——
    分母（一共几条）已经不进摘要了。``now`` 是**注入**的：这个摘要带「已过期」，而那是
    与真实时钟比出来的 —— 不注入的话，同一条用例在上午跑与下午跑会得出两个结论。
    """
    early = datetime(2026, 10, 10, 8, 0)          # 两条都还没到点
    plugin = _plugin(tmp_path, [])
    assert plugin._snapshot(early).state == "empty"

    plugin.save_items([_item(), _item(id="td2", at="18:00")])
    assert plugin._snapshot(early).state == "pending"
    assert plugin._snapshot(early).summary == "未完成 2 项"

    plugin.set_done("td1")
    assert plugin._snapshot(early).summary == "未完成 1 项"
    plugin.set_done("td2")
    assert plugin._snapshot(early).state == "done"
    assert plugin._snapshot(early).summary == "全部完成"


def test_overdue_wins_over_undone(tmp_path):
    """过了点还没做 ⇒ 报「N 项已过期」，**优先于**「未完成 N 项」（§4.6 B 那张表）。

    两条同权重的话，最该被看见的那一条会被「未完成 2 项」盖掉。
    """
    plugin = _plugin(tmp_path, [_item(), _item(id="td2", at="18:00")])
    noon = datetime(2026, 10, 10, 12, 0)          # 09:30 那条已经过点了
    assert plugin._snapshot(noon).summary == "1 项已过期"
    # 完成之后过期那一格**不再计**（完成的事没有「过期」这回事）。
    plugin.set_done("td1")
    assert plugin._snapshot(noon).summary == "未完成 1 项"


def test_disabled_items_are_out_of_the_denominator(tmp_path):
    """关掉的条目不算数 —— 否则「未完成 3 项」里有两条永远做不完。"""
    plugin = _plugin(tmp_path, [_item(), _item(id="td2", enabled=False), _item(id="td3")])
    early = datetime(2026, 10, 10, 8, 0)
    assert plugin._snapshot(early).summary == "未完成 2 项"


def test_the_row_declares_no_on_click_so_the_detail_entry_survives(tmp_path):
    """面板行**只报状态**：``on_click`` 优先于框架合成的「打开详情页」（决议 D28）⇒
    在这一行挂一个点击动作，等于永久放弃详情入口（旧行为：点整行 ＝ 立刻问下一条）。

    这不是「换了个写法」而是一条**契约**：``hosts_guard`` / ``route_guard`` 都因为同一个
    理由把 ``on_click`` 摘了 —— 摘漏的那一个会让详情页整页点不开，而且不报错。
    """
    plugin = _plugin(tmp_path, [_item()])
    (row,) = plugin.row_specs()
    assert row.key == "main"
    assert row.on_click is None, "整行点击会盖掉框架合成的详情入口"
    assert row.get_clickable is None, "「可点」的判据跟着 on_click 一起走"
    assert plugin.detail_entry() is not None


# ── 用户自己动手那条路（「拉」） ──────────────────────────────────────

def test_ask_now_registers_a_manual_rule_with_the_same_prompt(tmp_path):
    center = _FakeCenter()
    plugin = _plugin(tmp_path, [_item()], center)
    assert plugin.ask_now(_item()) is None, "None ＝ 已经弹出去了"
    spec, callback = center.manual[0]
    assert spec.plugin_id == "todo"
    assert spec.prompt.key == "todo.item.td1"
    assert callable(callback)
    assert center.manual[0][0].prompt.title == center.registered[0][0].prompt.title, (
        "手动问与到点问必须是同一份问法（两处各写一遍迟早不一样）"
    )


def test_ask_now_refuses_an_unsaved_item(tmp_path):
    center = _FakeCenter()
    plugin = _plugin(tmp_path, [], center)
    reason = plugin.ask_now(_item(id=""))
    assert reason, "没保存的条目没有 id，答案没处落 —— 必须给一句实话"
    assert center.manual == []


def test_ask_now_says_so_when_the_center_is_absent(tmp_path):
    plugin = _plugin(tmp_path, [_item()])
    assert plugin.ask_now(_item())


# ── 预览这一条（详情页编辑屏那枚键） ──────────────────────────────────

def test_preview_says_so_when_the_overlay_is_not_up(tmp_path):
    """「预览这一条」在弹不出来时回的是一句**事实**，不是布尔。

    「算不出可显示的时刻」与「浮层没接管」是两件不同的事：挤成一个 ``False`` 之后界面
    只能说一句「失败了」，用户看不出该改条目还是该重启。
    """
    plugin = _plugin(tmp_path, [])
    assert "算不出" in plugin.preview_entry(_item(at="")), "空时刻算不出一条可显示的"
    assert "还没接管" in plugin.preview_entry(_item()), "有时刻了，但浮层不在"


def test_detail_entry_is_a_window_without_the_save_contract(tmp_path):
    """详情页是 window，但**不挂** ``save``（决议 D26；D35 的「攒改动再保存」留给设置窗）。

    待办是独立数据管理：每条改完（「确定」／删除）立刻写盘，没有「攒着等窗口保存」
    这回事 —— 所以窗口页脚只有「完成」、关窗也不用问「还有没保存的改动」。漏掉 ``save``
    在这里是**故意的**；这条守着「别把设置窗的攒改动模型误用到待办」。
    """
    from barkit.contribution.specs import DETAIL_PRESENTATION_WINDOW

    spec = _plugin(tmp_path, []).detail_entry()
    assert spec.key == "main"
    assert spec.presentation == DETAIL_PRESENTATION_WINDOW
    assert spec.build is not None
    assert spec.save is None, "待办不该用设置窗那套「攒改动再保存」"


# ── 2026-10 审查修复 ───────────────────────────────────────────────

from barkit.plugins.todo.detail_view import _assign_id


def test_assign_id_never_reuses_deleted_id():
    """删掉 td2 后再加一条，新 id 应是 td3 而非复用的 td2；复用会让新条目继承
    旧条目去重账里的 last_asked=today，表现为「新增当天不再弹提醒」（P1）。"""
    items = [{"id": "td1"}, {"id": "td2"}]
    new = {}
    _assign_id(new, items)
    assert new["id"] == "td3"

    items2 = [{"id": "td1"}]
    new2 = {}
    _assign_id(new2, items2)
    assert new2["id"] == "td2"


class _RecordingCenter(_FakeCenter):
    def __init__(self):
        super().__init__()
        self.noted = []

    def note_answered(self, key: str) -> None:
        self.noted.append(key)


def test_set_done_marks_dedupe_answered(tmp_path):
    """在详情页勾掉「完成」（那枚「标记今天完成」），当天到点不应再弹同一件事；
    不补 note_answered 就只有真弹过才记账（P1）。"""
    center = _RecordingCenter()
    plugin = _plugin(tmp_path, [_item()], center)
    assert plugin.set_done("td1") is True
    assert center.noted == ["todo.item.td1"]


# 「下一条取时间最早」那条 P2 判据随 ``_next_item`` / ``ask_next`` 一起退了场（合并方案
# §4.2：整行不再自带动作，条目由用户在详情页自己挑）。内核那边的窗口排序仍按**秒数**
# （``scheduler`` 里 ``windows.sort(key=...at_s...)``），不存在「单位数小时被字符串序
# 排到 10:00 之后」那个问题 —— 判据不是丢了，是它守的那条路没有了。
