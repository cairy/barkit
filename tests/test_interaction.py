"""条件触发询问（core/interaction）的行为判据。

覆盖的是**这层机制存在的理由**，不是数据结构的字段清点：

- 锁存合取：只在启动时求值一次 ⇒ 「路由命中」这类条件永远触发不了（方案 §2.1 那条坑）；
- 去重五档与「弹过即记」（防骚扰）；
- 「关掉 ≠ 答过」那一档（今天答过才不问；没答的话重启再给一次机会）；
- 「稍后」必须真的能再弹一次（不清标记就是假功能）与其每日上限；
- 账目只挡弹窗、不挡补填（方案 §2.2b）。

时间一律注入固定值，不读系统钟：这批判据全在「跨天」「到点」上。
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from barkit.core.interaction import (
    COND_DAILY_AT,
    COND_ON_START,
    COND_PANEL_OPENED,
    COND_PREDICATE,
    COND_STATE_IS,
    DEDUPE_ALWAYS,
    DEDUPE_ANSWERED,
    DEDUPE_ANSWERED_DAY,
    DEDUPE_DAY,
    DEDUPE_SESSION,
    PROMPT_TIME,
    SNOOZE_LIMIT_PER_DAY,
    Condition,
    DedupeLedger,
    InteractionResult,
    Latches,
    PromptSpec,
    Trigger,
    TriggerSpec,
    evaluate,
    state_matches,
    topics_of,
)

_NOW = datetime(2026, 9, 24, 9, 30, 0)
_TOMORROW = datetime(2026, 9, 25, 9, 30, 0)


def _prompt(key: str = "clock.workday.e1", **kw) -> PromptSpec:
    return PromptSpec(key=key, title="今天几点上班？", kind=PROMPT_TIME, **kw)


def _trigger(*conditions, dedupe: str = DEDUPE_DAY, key: str = "clock.workday.e1") -> Trigger:
    return Trigger(TriggerSpec(
        plugin_id="clock", prompt=_prompt(key), conditions=conditions, dedupe=dedupe,
    ))


class _Report:
    """总线上的 StateReport 替身（只读两个属性，够比对用）。"""

    def __init__(self, state: str, summary: str = "") -> None:
        self.state = state
        self.summary = summary


# ── 锁存合取：条件不是「某一刻同时为真」────────────────────────────────

def test_partial_signals_do_not_trigger():
    """只有启动、场景还没命中 ⇒ 不成立。这正是「启动时求值一次」的做法会漏掉的情形。"""
    t = _trigger(Condition(COND_ON_START), Condition(COND_STATE_IS, topic="p", state="active"))
    t.latches.set(0)
    assert t.due() is False


def test_all_signals_then_trigger():
    t = _trigger(Condition(COND_ON_START), Condition(COND_STATE_IS, topic="p", state="active"))
    t.latches.set(0)
    assert t.due() is False
    t.latches.set(1)          # 场景命中姗姗来迟（首轮 tick 之后）
    assert t.due() is True


def test_latch_reset_clears_everything():
    t = _trigger(Condition(COND_ON_START))
    t.latches.set(0)
    assert t.due() is True
    t.latches.reset()
    assert t.due() is False


def test_unknown_condition_never_triggers():
    """未知类型宁可不弹：弹一个没人要的窗比不弹更糟。"""
    t = _trigger(Condition("nonsense"))
    assert t.due() is False


# ── 即时型条件 ──────────────────────────────────────────────────────

@pytest.mark.parametrize("at,now,due", [
    ("09:00", _NOW, True),
    ("10:00", _NOW, False),
    ("09:30", _NOW, True),
    ("not-a-time", _NOW, False),
    ("", _NOW, False),
])
def test_daily_at(at, now, due):
    t = _trigger(Condition(COND_DAILY_AT, at=at))
    assert t.due(now) is due


def test_predicate_exception_counts_as_false():
    """一条判据写错不该把整轮求值拖垮（也不该静默当成「成立」）。"""

    def boom() -> bool:
        raise RuntimeError("业务判据炸了")

    t = _trigger(Condition(COND_PREDICATE, predicate=boom))
    assert t.due() is False


def test_predicate_none_is_false():
    t = _trigger(Condition(COND_PREDICATE))
    assert t.due() is False


def test_predicate_true_alone_triggers():
    t = _trigger(Condition(COND_PREDICATE, predicate=lambda: True))
    assert t.due() is True


# ── 状态取值比对 ────────────────────────────────────────────────────

def test_state_matches_state_and_detail():
    cond = Condition(COND_STATE_IS, topic="p", state="active", detail="Office")
    assert state_matches(cond, _Report("active", "Office")) is True
    assert state_matches(cond, _Report("active", "Home")) is False
    assert state_matches(cond, _Report("none", "Office")) is False


def test_state_matches_without_detail_ignores_summary():
    cond = Condition(COND_STATE_IS, topic="p", state="active")
    assert state_matches(cond, _Report("active", "anywhere")) is True


def test_state_matches_rejects_wrong_kind():
    assert state_matches(Condition(COND_ON_START), _Report("active")) is False


def test_topics_of_collects_state_topics_only():
    t = _trigger(
        Condition(COND_ON_START),
        Condition(COND_STATE_IS, topic="a.b.c", state="active"),
        Condition(COND_STATE_IS, topic="a.b.c", state="other"),
        Condition(COND_PANEL_OPENED),
    )
    assert topics_of(t.spec) == {"a.b.c"}


# ── 去重五档 ────────────────────────────────────────────────────────

def test_always_asks_every_time(tmp_path):
    led = DedupeLedger(tmp_path / "s.json")
    assert led.should_ask("k", DEDUPE_ALWAYS) is True
    led.mark_asked("k", _NOW)
    assert led.should_ask("k", DEDUPE_ALWAYS) is True


def test_once_per_session_is_memory_only(tmp_path):
    """不落盘：进程重启后本就该重新问一次。"""
    led = DedupeLedger(tmp_path / "s.json")
    assert led.should_ask("k", DEDUPE_SESSION) is True
    led.mark_asked("k", _NOW)
    assert led.should_ask("k", DEDUPE_SESSION) is False
    assert DedupeLedger(tmp_path / "s.json").should_ask("k", DEDUPE_SESSION) is True


def test_once_per_day_asked_is_enough(tmp_path):
    """弹过即记（含关掉 / 超时）：这是「防骚扰」那一档的正条。"""
    led = DedupeLedger(tmp_path / "s.json")
    led.mark_asked("k", _NOW)
    assert led.should_ask("k", DEDUPE_DAY, _NOW) is False
    assert led.mark_answered is not None  # 答过另有入口，未答不影响「当天不再弹」


def test_once_per_day_resets_tomorrow(tmp_path):
    led = DedupeLedger(tmp_path / "s.json")
    led.mark_asked("k", _NOW)
    assert led.should_ask("k", DEDUPE_DAY, _TOMORROW) is True


def test_until_answered_survives_days(tmp_path):
    led = DedupeLedger(tmp_path / "s.json")
    led.mark_asked("k", _NOW)
    assert led.should_ask("k", DEDUPE_ANSWERED, _TOMORROW) is True   # 弹过不算答过
    led.mark_answered("k", _TOMORROW)
    assert led.should_ask("k", DEDUPE_ANSWERED, _TOMORROW) is False


# ── 「关掉 ≠ 答过」（用户 2026-10-01 定的那一档）──────────────────────
#
# 原来那一档把两个事实当成一个：「我问过你」⇒「你填过了」。用户当天的现场正是它 ——
# 关掉没答、当天也确实没填，系统却按旧值当真（翻页钟照算「下班时间 19:00」）。
# 这一档只认**答案**：答过（含补填）当天就不问；没答则每次启动再给一次机会。
# ────────────────────────────────────────────────────────────────────

def test_until_answered_day_asks_again_after_a_restart(tmp_path):
    """关掉 ⇒ 同一次运行里不再问，但**下次启动还会再问一次**。"""
    p = tmp_path / "s.json"
    led = DedupeLedger(p)
    led.mark_asked("k", _NOW)                                   # 弹出来，用户点了「关闭」
    assert led.should_ask("k", DEDUPE_ANSWERED_DAY, _NOW) is False, "同一次运行里别反复烦他"
    assert DedupeLedger(p).should_ask("k", DEDUPE_ANSWERED_DAY, _NOW) is True, (
        "重启后账上只有「问过」没有「答过」⇒ 那次机会要还给他（不然关了就等于当天没有提醒）"
    )


def test_until_answered_day_stays_quiet_after_the_answer(tmp_path):
    """答过 ⇒ 当天不再问，重启也不问（每天被问两次是用户会判成「有病」的那类重复）。"""
    p = tmp_path / "s.json"
    led = DedupeLedger(p)
    led.mark_answered("k", _NOW)                                # 含用户在设置页自己补填
    assert led.should_ask("k", DEDUPE_ANSWERED_DAY, _NOW) is False
    assert DedupeLedger(p).should_ask("k", DEDUPE_ANSWERED_DAY, _NOW) is False


def test_until_answered_day_answered_yesterday_is_not_today(tmp_path):
    """「答过没有」与「**今天**答过没」是两件事：昨天答过，今天照问。"""
    p = tmp_path / "s.json"
    led = DedupeLedger(p)
    led.mark_answered("k", _NOW)
    assert led.should_ask("k", DEDUPE_ANSWERED_DAY, _TOMORROW) is True
    assert DedupeLedger(p).should_ask("k", DEDUPE_ANSWERED_DAY, _TOMORROW) is True


def test_until_answered_day_reopens_tomorrow_in_a_running_process(tmp_path):
    """常驻进程跨天是常态：昨天那笔内存账不许把今天一整天挡掉。"""
    led = DedupeLedger(tmp_path / "s.json")
    led.mark_asked("k", _NOW)
    assert led.should_ask("k", DEDUPE_ANSWERED_DAY, _TOMORROW) is True


def test_snooze_reopens_the_session_tiers_too(tmp_path):
    """「稍后」撤的是**内存**那笔账：once_per_session / until_answered_day 读的正是它，
    只清盘上那笔 ``last_asked`` 挡不住 ⇒ 按钮点了没反应。"""
    for dedupe in (DEDUPE_SESSION, DEDUPE_ANSWERED_DAY):
        led = DedupeLedger(tmp_path / f"{dedupe}.json")
        led.mark_asked("k", _NOW)
        assert led.should_ask("k", dedupe, _NOW) is False
        assert led.snooze("k", 30, _NOW) is True
        assert led.should_ask("k", dedupe, _NOW) is False                    # 还没到点
        assert led.should_ask("k", dedupe, datetime(2026, 9, 24, 10, 5)) is True


def test_unknown_dedupe_does_not_ask(tmp_path):
    led = DedupeLedger(tmp_path / "s.json")
    assert led.should_ask("k", "whenever") is False


# ── 「稍后」：必须真的能再弹一次 ──────────────────────────────────────

def test_snooze_clears_the_day_mark_so_it_can_come_back(tmp_path):
    """不清当天标记的话，「稍后」在 once_per_day 下永远等不到第二次 —— 那是假功能。"""
    led = DedupeLedger(tmp_path / "s.json")
    led.mark_asked("k", _NOW)
    assert led.should_ask("k", DEDUPE_DAY, _NOW) is False
    assert led.snooze("k", 30, _NOW) is True
    assert led.should_ask("k", DEDUPE_DAY, _NOW) is False          # 还没到点
    assert led.should_ask("k", DEDUPE_DAY, datetime(2026, 9, 24, 10, 5)) is True


def test_snooze_has_a_daily_limit(tmp_path):
    """点第三次还不想填 = 不想填。「稍后」不能变成无限骚扰的合法外衣。"""
    led = DedupeLedger(tmp_path / "s.json")
    for _ in range(SNOOZE_LIMIT_PER_DAY):
        assert led.snooze("k", 30, _NOW) is True
    assert led.snooze("k", 30, _NOW) is False


def test_can_snooze_matches_the_real_limit(tmp_path):
    """画不画「稍后」按钮，判据必须与真去点的那次**同一处** —— 两处各判一次必然漂移，
    漂移的表现是「按钮画出来了、点了没反应」。"""
    led = DedupeLedger(tmp_path / "s.json")
    assert led.can_snooze("k", _NOW) is True
    for _ in range(SNOOZE_LIMIT_PER_DAY):
        assert led.can_snooze("k", _NOW) is True
        led.snooze("k", 30, _NOW)
    assert led.can_snooze("k", _NOW) is False
    assert led.can_snooze("k", _TOMORROW) is True, "换天归零"


def test_snooze_limit_resets_tomorrow(tmp_path):
    led = DedupeLedger(tmp_path / "s.json")
    for _ in range(SNOOZE_LIMIT_PER_DAY):
        led.snooze("k", 30, _NOW)
    assert led.snooze("k", 30, _TOMORROW) is True


def test_answered_clears_snooze(tmp_path):
    led = DedupeLedger(tmp_path / "s.json")
    led.snooze("k", 30, _NOW)
    led.mark_answered("k", _NOW)
    assert led.pending_snoozes(_NOW) == []


def test_pending_snoozes_reports_due_keys(tmp_path):
    led = DedupeLedger(tmp_path / "s.json")
    led.snooze("a", 30, _NOW)
    led.snooze("b", 30, _NOW)
    assert led.pending_snoozes(_NOW) == []
    assert sorted(led.pending_snoozes(datetime(2026, 9, 24, 10, 5))) == ["a", "b"]


# ── 账目健壮性 ──────────────────────────────────────────────────────

def test_corrupt_ledger_is_treated_as_empty(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{ 不是 json", encoding="utf-8")
    led = DedupeLedger(p)
    assert led.should_ask("k", DEDUPE_DAY, _NOW) is True


def test_missing_file_is_treated_as_empty(tmp_path):
    assert DedupeLedger(tmp_path / "nope.json").should_ask("k", DEDUPE_DAY, _NOW) is True


def test_ledger_persists_across_instances(tmp_path):
    p = tmp_path / "s.json"
    DedupeLedger(p).mark_asked("k", _NOW)
    assert DedupeLedger(p).should_ask("k", DEDUPE_DAY, _NOW) is False
    assert json.loads(p.read_text(encoding="utf-8"))["k"]["last_asked"] == "2026-09-24"


def test_forget_drops_the_entry(tmp_path):
    led = DedupeLedger(tmp_path / "s.json")
    led.mark_asked("k", _NOW)
    led.forget("k")
    assert led.should_ask("k", DEDUPE_DAY, _NOW) is True


# ── 结果：区分「没理」与「主动推迟」────────────────────────────────────

def test_result_carries_why_not_answered():
    assert InteractionResult(True, "09:12").answered is True
    assert InteractionResult(False, kind="timeout").answered is False
    assert InteractionResult(False, kind="later").answered is False


# ── 反证：把判据改回去就得红 ──────────────────────────────────────────

def test_evaluate_ignores_latch_for_immediate_conditions():
    """即时型条件不占锁存位：给它置位也不该成立（否则「到点」会变成「到过一次点就永远成立」）。"""
    spec = TriggerSpec("clock", _prompt(), (Condition(COND_DAILY_AT, at="10:00"),))
    latches = Latches()
    latches.set(0)
    assert evaluate(spec, latches, _NOW) is False
