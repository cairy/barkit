"""tests/test_todo_model：合并方案**第 1 批（条目模型）**的判据。

这一批把「条目 → 一条规则」从**一个写死的形状**改成**按条目推导的三件事**
（什么时候问 / 问法 / 去重档）。守的就是「改完之后老行为一字未动」＋
「新形状是旧形状的推广，不是另一套」：

- **旧条目行为保持**：``ask`` 键**不在**的条目必须与今天逐字段相同地登记
  （``daily_at`` ＋ ``once_per_day`` ＋ 一枚「完成」按钮）—— 缺省值换一个，
  所有老条目的弹窗都会变样，而那是用户唯一看得见的那一层；
- **新条目按问法推导**：问 ``time`` 的**没有**「时刻」这个条件（它本来就没有
  一个说得出的固定时刻），触发靠 ``on_start``；去重取 ``until_answered_day``
  （答出来的值还要被显示面读 ⇒ 答不出来就没有可用值，必须不屈不挠）；
- **等价性结论（已固化）**：第 1 批做这两条时是拿**旧翻页钟组件**当尺子（让它真登记
  一遍、逐字段比）；批 5 那个组件退场，**参照物没了** ⇒ 当年比出来的那份期望值写死在
  用例里。判据守的东西一个字没变 —— 只有它用来比的那把尺子换了；
- **其余四型在合并模型里根本不问**：固定 / 循环 / 番茄都只开显示面，「推算下班」的
  「固定时间」那一支等价于一条普通倒计时条目。

条件求值 / 去重账 / 排队那半在 ``test_interaction*.py``，本文件不重复测它们。
"""

from __future__ import annotations

from datetime import datetime

from barkit.core.config import Config
from barkit.core.events import ROUTE_PROFILE_TOPIC
from barkit.core.interaction import (
    COND_DAILY_AT,
    COND_ON_START,
    COND_STATE_IS,
    DEDUPE_ANSWERED_DAY,
    DEDUPE_DAY,
    PROMPT_CHOICE,
    PROMPT_TIME,
    RESULT_ANSWERED,
    InteractionResult,
)
from barkit.core.plugin import PluginContext
from barkit.plugins.todo.plugin import DONE_OPTION, Plugin


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
        self.shown: "dict[str, object]" = {}
        self.answered: "list[str]" = []

    def register(self, spec, on_result=None, on_shown=None) -> None:
        self.registered.append((spec, on_result))
        # 中枢的第三个参数（「提醒登场」）在旧模型下恒为空 —— 这里收下即可，见
        # ``test_todo._FakeCenter`` 那条注释。
        if on_shown is not None:
            self.shown[spec.prompt.key] = on_shown

    def ask_now(self, spec, on_result=None) -> bool:
        self.manual.append((spec, on_result))
        return True

    def unregister(self, plugin_id: str) -> None:
        self.unregistered.append(plugin_id)

    def note_answered(self, key: str) -> None:
        self.answered.append(key)


def _plugin(tmp_path, items=None, center=None) -> Plugin:
    ctx = PluginContext(config=Config(user_path=tmp_path / "c.yaml"), events=None)
    if center is not None:
        ctx.interaction = center
    plugin = Plugin(ctx)
    if items is not None:
        assert plugin.save_items(items) is True
    return plugin


def _conds(conditions) -> "tuple":
    """条件的**可比形状**（``Condition`` 里那个 ``predicate`` 回调不参与比较）。"""
    return tuple(
        (c.kind, c.topic, c.state, c.detail, c.at) for c in conditions
    )


# ── 旧条目：ask 缺省 ＝ 今天的行为，一字不差 ──────────────────────────

def test_absent_ask_registers_todays_shape(tmp_path):
    """``ask`` 键不在（迁移之前的所有老条目）⇒ 与今天逐字段相同。"""
    center = _FakeCenter()
    _plugin(tmp_path, [_item()], center)
    spec, callback = center.registered[0]

    assert len(spec.conditions) == 1, "一条条目只出一条条件"
    assert (spec.conditions[0].kind, spec.conditions[0].at) == (COND_DAILY_AT, "09:30")
    assert spec.dedupe == DEDUPE_DAY, "老条目是「弹过即记」，不是「答了才记」"
    assert spec.prompt.kind == PROMPT_CHOICE
    assert spec.prompt.options == (DONE_OPTION,)
    assert spec.prompt.message == "计划 09:30"
    assert callable(callback)


def test_explicit_choice_equals_absent_ask(tmp_path):
    """显式写 ``ask="choice"`` 与**不写**这个键，登记的规则逐字段相同。

    迁移（第 4 批）会给老条目补上 ``ask="choice"``；这一条钉的就是「补完之后
    行为没变」—— 否则迁移那天所有老条目一起变样。
    """
    center_old = _FakeCenter()
    _plugin(tmp_path, [_item()], center_old)
    center_new = _FakeCenter()
    _plugin(tmp_path, [_item(ask="choice")], center_new)

    old, new = center_old.registered[0][0], center_new.registered[0][0]
    assert _conds(old.conditions) == _conds(new.conditions)
    assert old.dedupe == new.dedupe
    assert (old.prompt.key, old.prompt.kind, old.prompt.message, old.prompt.options) == (
        new.prompt.key, new.prompt.kind, new.prompt.message, new.prompt.options,
    )


def test_empty_ask_means_this_item_does_not_ask(tmp_path):
    """``ask`` 在且为**空串** ＝ 这一条不问（只开显示面的条目）⇒ 不登记。

    「键不在」与「键在且空」是两回事（本仓的无值两态口径）：前者是旧条目，
    后者是用户明说了「这条别问我」。
    """
    center = _FakeCenter()
    _plugin(tmp_path, [_item(ask=""), _item(id="td2", ask="只看不动")], center)
    # 不认识的值回落到 choice（宁可问错一种，也不静默变成「不问」）
    assert [spec.prompt.key for spec, _ in center.registered] == ["todo.item.td2"]


def test_illegal_time_is_still_blocked_before_registration(tmp_path):
    """时刻写歪的条目照旧不登记 —— 登记了也永远触发不了，是最难自查的那种坏。"""
    center = _FakeCenter()
    _plugin(tmp_path, [_item(at="25:00"), _item(id="td2", at="")], center)
    assert center.registered == []


# ── 新条目：问 time 的（推算下班那种） ────────────────────────────────

def test_time_item_asks_on_start_with_answered_dedupe(tmp_path):
    """问 ``time`` 的：触发靠 ``on_start``、去重取「当天答过为止」、问法 kind = time。"""
    center = _FakeCenter()
    _plugin(tmp_path, [_item(id="td1", at="", ask="time", title="今天几点上班")], center)
    spec, _ = center.registered[0]

    assert _conds(spec.conditions) == ((COND_ON_START, "", "", "", ""),), (
        "没有说得出的固定时刻 ⇒ 不该有 daily_at 条件"
    )
    assert spec.dedupe == DEDUPE_ANSWERED_DAY
    assert spec.prompt.kind == PROMPT_TIME
    assert spec.prompt.options == (), "time 问法没有按钮"
    assert spec.prompt.message == ""


def test_time_answer_lands_in_the_day_value_not_in_done(tmp_path):
    """答案写进**当日值**；它不算「今天完成」，也不占面板分母。

    存成完成会让面板凭空多出一条永远做不完的待办（方案 §4.1）。
    """
    center = _FakeCenter()
    plugin = _plugin(tmp_path, [_item(id="td1", at="", ask="time", title="今天几点上班")], center)
    _, callback = center.registered[0]

    callback(InteractionResult(True, "09:12", RESULT_ANSWERED))
    assert plugin.day_value_of("td1") == "09:12", "填了等于没填 —— 显示面拿不到值"
    assert plugin.done_at("td1") == "", "答案不是「完成」"
    assert plugin.done_today() == {}
    assert center.answered == ["todo.item.td1"], "答了当天不该再弹"
    assert plugin._snapshot().state == "empty", "只问 time 的条目不进面板分母"


def test_time_answer_that_is_not_a_time_is_dropped(tmp_path):
    """答非时刻的丢掉 —— 存一个谁都用不上的值，只是给显示面多一种脏数据要防。"""
    center = _FakeCenter()
    plugin = _plugin(tmp_path, [_item(id="td1", at="", ask="time")], center)
    _, callback = center.registered[0]

    callback(InteractionResult(True, "不是时间", RESULT_ANSWERED))
    assert plugin.day_value_of("td1") == ""


def test_dismissing_a_time_prompt_writes_nothing(tmp_path):
    """没理 / 稍后一律不写（那两种的去重账各自由框架记过了）。"""
    center = _FakeCenter()
    plugin = _plugin(tmp_path, [_item(id="td1", at="", ask="time")], center)
    _, callback = center.registered[0]

    callback(InteractionResult(False, None, "timeout"))
    callback(InteractionResult(False, None, "later"))
    assert plugin.day_value_of("td1") == ""


def test_time_item_does_not_shadow_a_real_todo_in_the_panel(tmp_path):
    """一条 choice ＋ 一条 time：**只有 choice 那条算数**，完成进度不受 time 型影响。

    旧模型的 ``ask=time`` 条目是「问一个值」，不是一件事 —— 它没有「完成」这个动作，
    算进「未完成 N 项」会让面板上永远挂着一条做不掉的待办。新模型没有这个问题：那个
    「值」已经是一等公民（变量），任务只剩引用它的那几条。
    """
    center = _FakeCenter()
    plugin = _plugin(
        tmp_path,
        [_item(id="td1", at="09:30"), _item(id="td2", at="", ask="time")],
        center,
    )
    early = datetime(2026, 10, 10, 8, 0)
    assert plugin._snapshot(early).summary == "未完成 1 项"
    plugin.set_done("td1")
    assert plugin._snapshot(early).state == "done"


# ── 「只在某场景」那一格 ───────────────────────────────────────────────

def test_scene_adds_a_state_condition_with_on_start(tmp_path):
    """填了场景 ⇒ 多一条 ``state_is``，**并且必须一起带上 ``on_start``**。

    启动那一刻场景还是 ``unknown``，只判 ``state_is`` 的条件永远触发不了
    （`内部计划（条件触发询问）` §2.1）。
    """
    center = _FakeCenter()
    _plugin(tmp_path, [_item(scene="Office")], center)
    spec, _ = center.registered[0]

    assert _conds(spec.conditions) == (
        (COND_ON_START, "", "", "", ""),
        (COND_DAILY_AT, "", "", "", "09:30"),
        (COND_STATE_IS, ROUTE_PROFILE_TOPIC, "active", "Office", ""),
    )
    assert spec.dedupe == DEDUPE_DAY, "场景不改变去重档（那由问法定）"


def test_scene_on_a_time_item_does_not_duplicate_on_start(tmp_path):
    """``time`` ＋ 场景：``on_start`` 只出一条（两条同种条件是同一个事实写两遍）。"""
    center = _FakeCenter()
    _plugin(tmp_path, [_item(at="", ask="time", scene="Office")], center)
    spec, _ = center.registered[0]

    assert _conds(spec.conditions) == (
        (COND_ON_START, "", "", "", ""),
        (COND_STATE_IS, ROUTE_PROFILE_TOPIC, "active", "Office", ""),
    )
    assert spec.dedupe == DEDUPE_ANSWERED_DAY


# ── 等价性结论（第 1 批的对拍结论，已固化成期望值） ────────────────────
#
# 第 1 批做这两条时是「让**真的**旧翻页钟组件把条目登记一遍、两边逐字段比」—— 那时
# 迁移还没做，只有对拍能验。批 5 那个组件退场，参照物没了 ⇒ 对拍换成把当年比出来的
# 那份期望值**写在这里**。守的东西一个字没变（还是「问 time 的条目长什么样」），
# 变的是它不再需要一个已经不存在的组件来当尺子。
#
# 当年核出来的两条（与旧内核登记出来的逐字段相同）：
#   1. 一条「推算下班（每天问我）」＝ on_start ＋（有场景时再一条 state_is），去重
#      ``until_answered_day``，问法 ``time``；
#   2. 固定 / 循环 / 番茄 / 推算下班的「固定时间」那一支，旧组件自己**一条都不登记**
#      （纯报时的显示条目）⇒ 合并模型里也不问（迁移给它们落的是 ``ask=""``）。

def test_a_time_entry_registers_the_shape_the_old_component_did(tmp_path):
    """问 ``time`` 的条目：条件形状 / 去重档 / 问法与旧翻页钟当年登记的**逐字段相同**。

    比的是**行为面**（条件 / 去重档 / 问法种类）。去重键与标题**本该不同** ——
    合并后这一条是本组件自己的一条条目，键与名字都归它（``todo.item.tdX``）。
    """
    for scene in ("", "Office"):
        center = _FakeCenter()
        _plugin(
            tmp_path,
            [_item(id="td1", at="", ask="time", text="下班", scene=scene)],
            center,
        )
        spec, _ = center.registered[0]

        expected = [(COND_ON_START, "", "", "", "")]
        if scene:
            expected.append((COND_STATE_IS, ROUTE_PROFILE_TOPIC, "active", scene, ""))
        assert _conds(spec.conditions) == tuple(expected), f"场景={scene!r} 时条件形状变了"
        assert spec.dedupe == DEDUPE_ANSWERED_DAY
        assert spec.prompt.kind == PROMPT_TIME


def test_entries_that_do_not_ask_register_nothing(tmp_path):
    """只开显示面的条目**一条都不登记** —— 当年那四型迁移过来都是这一支。

    旧组件的固定 / 循环 / 番茄 / 推算下班（写死上班时间）自己一条都不登记；迁移把它们
    一律落成 ``ask=""``（见 ``migrate.py`` 的落位表）⇒ 这里同样不登记。**判据只在合并
    模型这一侧量**：旧组件已退场，尺子换成了 ``ask`` 这一格 —— 它一空，登记就该空。
    """
    center = _FakeCenter()
    _plugin(
        tmp_path,
        [
            _item(id="a", at="08:30", ask="", title="打卡"),          # ← 旧 fixed
            _item(id="b", at="09:00", ask="", title="循环", interval_min=60),   # ← 旧 loop
            _item(id="c", at="", ask="", title="下班", work_hours=9),           # ← 旧 derived
        ],
        center,
    )
    assert center.registered == [], "只开显示面的条目登记了规则 ⇒ 迁移那天会开始弹窗"
