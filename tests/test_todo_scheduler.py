"""tests/test_todo_scheduler：合并方案**第 2 批（浮层重设计）**的**内核判据**。

``plugins/todo/scheduler.py`` 是**纯函数内核**（零 Qt、零读盘、每次调用重算全部结论），
它把「条目表 ＋ 此刻 ＋ 当日值 ＋ 场景」收成一份**整屏显示态**。这一批守五件事：

1. **三种登场算法一个不漏**（``docs/plans/2026-10-08-翻页钟与待办合并.md`` §2.3）：
   循环整点（旧 ``loop``）／作答＋工作时长（旧 ``derived`` 的「每天问我」）／字面 ``at``。
   「没有当日值就不登场」是刻意的 —— 不答就不启动，兜底值已在方案里砍掉。
2. **退场与当天末**：``end`` 留空 ⇒ 挂到当天末，且 :data:`DAY_END_S` 取 ``86399``
   而不是 ``86400`` —— 取后者时闭区间会在 00:00:00 那一秒把条目又判回窗口里。
3. **钟面只出一面**（倒计时 > 正计时 > 时钟）：多条同时命中时其余条目只贡献行。
4. **行**：按登场时刻排序、超上限截断 ＋ 一行「还有 k 条」、被收起的条目不上屏、
   场景门禁、空标题兜底。
5. **「预览这一条」按哪一刻算**：此刻真在窗口里就按此刻，否则按**窗口起点**（＝到点
   减提前量）—— 拿 ``_span_of`` 的第一个返回值当登场时刻会让推算下班预览出 00:00。
6. **详情页列表行那两句**（``row_time_label`` / ``row_summary``）：时刻要算得出来
   （「作答 ＋ 工作时长」那一条两个字面键都是空，直接读就是恒显 ``--:--``），
   摘要要说清**这一条怎么响** —— 且必须按内核**真正会走**的那条算法说。

画那一半（翻页动画 / 浮层增删 / 行上按钮）在 ``test_todo_overlay.py``；
条目怎么变成规则在 ``test_todo_model.py``。本文件只碰纯函数。
"""

from __future__ import annotations

import dataclasses
from datetime import datetime

import pytest

from barkit.plugins.todo.scheduler import (
    ACTION_DONE,
    ACTION_SNOOZE,
    DAY_END_S,
    DAY_S,
    FACE_CLOCK,
    FACE_COUNTDOWN,
    FACE_COUNTUP,
    FALLBACK_TITLE,
    Row,
    compute_display,
    fmt_hhmm,
    fmt_mmss,
    preview_entry,
    render_text,
    row_time_label,
)


def _e(**kw) -> dict:
    base = {"id": "e1", "enabled": True, "title": "喝水", "at": "09:30"}
    base.update(kw)
    return base


def _at(h: int, m: int, s: int = 0, day: int = 8) -> datetime:
    return datetime(2026, 10, day, h, m, s)


def _show(entries, when: datetime, **kw):
    return compute_display(entries, when, **kw)


# ── 时间与文案（纯函数；浮层在两次 tick 之间走的也是这一份） ────────────


def test_mmss_pads_and_can_go_past_an_hour():
    """``MM:SS``：补零、分钟可超过 99（长倒计时不触顶）、负数不外推。"""
    assert fmt_mmss(0) == "00:00"
    assert fmt_mmss(90) == "01:30"
    assert fmt_mmss(6000) == "100:00"
    assert fmt_mmss(-5) == "00:00", "倒计时过点不许显示 -00:01"


def test_hhmm_wraps_across_midnight():
    """当日秒数这个**圆**：目标时刻可能落在明天，文案按 24 小时取模。"""
    assert fmt_hhmm(0) == "00:00"
    assert fmt_hhmm(9 * 3600 + 30 * 60) == "09:30"
    assert fmt_hhmm(DAY_S + 3600 + 180) == "01:03"


def test_render_text_fills_this_entrys_values():
    out = render_text("现在 {现在}，{到点} 结束，还剩 {剩余}", _at(9, 30),
                      at_s=10 * 3600, remaining_s=1800)
    assert out == "现在 09:30，10:00 结束，还剩 30:00"


def test_render_text_keeps_what_it_cannot_compute_verbatim():
    """算不出的那一项**原样保留**（含拼错的占位符）—— 用户一眼看得出写错了，
    比静默吞掉（显示成空白）好得多。"""
    assert render_text("{到点} {剩余} {现在}", _at(9, 30)) == "{到点} {剩余} 09:30"
    assert render_text("{下班} 了", _at(9, 30)) == "{下班} 了"


# ── 登场：字面 at（end 留空 ⇒ 当天末） ────────────────────────────────


def test_a_literal_entry_shows_at_its_at_and_is_gone_at_midnight():
    """字面 ``at``：到点登场；``end`` 留空 ⇒ 挂到**当天末**，零点那一秒就退场。

    这条同时钉住 :data:`DAY_END_S` 的取值：改成 ``86400`` 之后，
    ``(0 - show_at) % DAY_S`` 恰好等于窗口长度 ⇒ 00:00:00 那一秒又判回窗口里，
    浮层会在零点闪一下再消失。
    """
    assert DAY_END_S == 86399
    assert _show([_e(at="09:30")], _at(9, 30)).show is True
    assert _show([_e(at="09:30")], _at(23, 59, 59)).show is True
    assert _show([_e(at="09:30")], _at(0, 0, 0, day=9)).show is False


def test_an_entry_is_not_shown_before_its_at():
    assert _show([_e(at="09:30")], _at(9, 29, 59)).show is False


def test_before_min_moves_the_entrance_earlier():
    e = _e(at="09:30", before_min=10)
    assert _show([e], _at(9, 20)).show is True
    assert _show([e], _at(9, 19, 59)).show is False


def test_time_label_prefers_the_literal_value():
    """行右侧那个时刻**字面值优先**（用户自己写的比格式化出来的更可信）。"""
    st = _show([_e(at="09:30", end="17:00")], _at(9, 30))
    assert st.rows[0].time_label == "17:00"


# ── 登场：循环整点（旧 loop） ─────────────────────────────────────────


def test_an_interval_entry_is_timed_from_the_last_completion():
    """循环条的**下一轮 ＝ 上一次完成时刻 ＋ N 分钟**（用户口径 2026-10-11）。

    旧算法是 ``(now_s // step) * step`` —— 从**零点起的固定网格**（09:44 时登场 09:30），
    与上一次什么时候做的毫无关系：九点零五做完和九点二十九做完，下一轮都落在九点半。
    ``day_values`` 里那一格既承载着旧模型的「完成时刻」、也承载着当日值，**在这里就是
    上一次完成**。
    """
    e = _e(at="", interval_min=30)
    done = {"e1": "09:12"}
    assert _show([e], _at(9, 41), day_values=done).show is False
    st = _show([e], _at(9, 42), day_values=done)
    assert st.show is True
    assert st.rows[0].at_s == 9 * 3600 + 42 * 60
    assert st.rows[0].time_label == "09:42", "循环条没有字面 at ⇒ 报算出来的这一轮"


def test_an_interval_entry_shows_from_the_start_when_never_done_today():
    """今天一次都没做 ⇒ **此刻就该出现**（任务开着本身就是「还没做」）。

    给成 ``零点 + N 分钟`` 的话，零点刚过那会儿它反倒不出现 —— 而那正是今天头一次
    该被提醒的时候。取 00:45 是因为它正好落在旧算法「零点后的第一个整点」里：那里旧
    算法报登场 00:30，看着也是显示了，错位就藏过去了。
    """
    st = _show([_e(at="", interval_min=30)], _at(0, 45))
    assert st.show is True
    assert st.rows[0].at_s == 0, "今天还没做过 ⇒ 从零点起就已经到期了"


def test_an_interval_entry_says_nothing_before_it_has_a_base():
    """循环条右侧那格：**今天还没做过 ⇒ 空着**，不报那个推出来的 ``00:00``。

    它的登场时刻是「上一次完成 ＋ N 分钟」，**今天没做过**就是没有基准 —— 算式里那
    个 0 说的是「按零点算、它此刻就该出现」，不是一个报得出的钟点。报 ``00:00`` 会让
    一行整点报时看着像「零点的报时」，而这类任务（``track_done=False``）天生写不进
    完成那一笔 ⇒ 那一格永远错着，且看不出是错。

    **做过之后那一半才是承重的一半**：只断言「没做过 ⇒ 空」，那么「一律返回空」的写
    法也照样能过。
    """
    e = _e(at="", interval_min=30)
    assert _show([e], _at(14, 30)).rows[0].time_label == ""
    assert row_time_label(e, _at(14, 30), {}) == "", "详情页与浮层同一份口径"
    done = {"e1": "09:12"}
    assert _show([e], _at(9, 42), day_values=done).rows[0].time_label == "09:42"
    assert row_time_label(e, _at(9, 42), done) == "09:42"


def test_a_finished_interval_entry_says_the_next_round_not_the_day_is_over():
    """刚做完一轮（下一轮还没到）⇒ 报**下一轮的时刻**，不是「今天已过」（2026-10-11 修）。

    「循环条没有窗口」有两种截然不同的缘故，原先共用一句「今天已过」：09:12 做完的
    喝水在 09:30 会被列表说成「今天已过」，而它 09:42 还会再来。第三、四行则是**真**
    的过完了今天（过了收工时刻 / 下一轮已经落在收工之后），那一句才是对的。

    四行是**同一条条目在四个时刻的四个答案**，一个都不许并：写成常量，或者把「还没到
    下一轮」并回「今天已过」，都会有一行红。
    """
    e = _e(at="", interval_min=30)
    assert row_time_label(e, _at(9, 30), {"e1": "09:12"}) == "09:42", "刚做完 ⇒ 报下一轮"
    assert row_time_label(e, _at(9, 44), {"e1": "09:12"}) == "09:42", "窗口里报的是同一个数"
    late = _e(at="", interval_min=30, end="18:00")
    assert row_time_label(late, _at(19, 0), {"e1": "09:12"}) == "今天已过", "过了收工时刻"
    assert row_time_label(late, _at(17, 45), {"e1": "17:30"}) == "今天已过", "下一轮落在收工之后"


def test_a_loop_with_a_window_reports_the_next_round_not_the_closing_time():
    """循环条＋生效时段 ⇒ 右侧那格报**这一轮**，不是收工时刻（2026-10-12 修）。

    循环条的 ``end`` 在旧内核里是「今天循环到几点为止」（``resolve_item`` 把
    ``window.to`` 写进这一格），字面值优先会把它当成这一轮的时刻报出去 ——
    于是填了时段的循环条，浮层与详情页右侧恒显收工时刻，「没完成过 ⇒ 空着」
    与「报下一轮」两条口径都被它挡死。

    两半都要断言：只断言「不报 18:00」的话，「循环条一律返回空」也照样能过。
    """
    e = _e(at="", interval_min=30, end="18:00")
    st = _show([e], _at(10, 0), day_values={"e1": "09:12"})
    assert st.show is True, "10:00 落在 [09:42, 18:00) 里"
    assert st.rows[0].time_label == "09:42", "窗口里 ⇒ 报这一轮（＝下一轮）的时刻"
    assert row_time_label(e, _at(10, 0), {"e1": "09:12"}) == "09:42", "详情页同一份口径"
    assert _show([e], _at(10, 0)).rows[0].time_label == "", "今天没做过 ⇒ 空着"


def test_an_interval_entry_comes_back_after_each_completion():
    """一轮一轮接着来：09:12 → 09:42 → 10:12 —— 每次都从刚刚写下的那一笔算起。"""
    e = _e(at="", interval_min=30)
    for done, too_early, its_turn in (
        ("09:12", (9, 41), (9, 42)),
        ("09:42", (10, 11), (10, 12)),
        ("10:12", (10, 41), (10, 42)),
    ):
        dv = {"e1": done}
        assert _show([e], _at(*too_early), day_values=dv).show is False
        assert _show([e], _at(*its_turn), day_values=dv).show is True


def test_a_loop_that_is_never_completed_anchors_at_the_start_of_the_day():
    """★ 「不需要完成」的循环条（整点报时）**永远没有完成那一笔**。

    而新的基准正是那一笔 ⇒ 它恒按零点算 ⇒ 从零点起就在屏幕上，一直挂到 ``end`` 收工。
    与旧版本等价（旧文档那句「首尾相接 ⇒ 它一直在浮层上」说的就是它），但这个组合此前
    没有用例盯着，而这次动的正是同一个算式 —— 改动前先从这儿问一遍「它会怎么样」。
    """
    e = _e(at="", interval_min=60, end="18:00")
    st = _show([e], _at(0, 5))
    assert st.show is True
    assert st.rows[0].at_s == 0, "没完成过 ⇒ 从零点就已经到期"
    assert _show([e], _at(17, 59)).show is True
    assert _show([e], _at(18, 0)).show is False, "到收工时刻为止"


def test_an_interval_entry_stays_until_you_do_it():
    """这一轮的事**一直挂着**到你做为止 —— 不像旧的整点那样到点自己退场。

    退场定在自己那一轮的末尾的话，「忘了做」与「做完了」在浮层上长得一样：下一轮按
    完成时刻算，两者都会立刻再来，用户看不出到底差在哪一步。
    """
    e = _e(at="", interval_min=30)
    before = {"e1": "09:12"}
    late = _at(12, 30)
    assert _show([e], late, day_values=before).show is True
    # 做完之后这一轮才退场，到下一轮再回来
    assert _show([e], late, day_values={"e1": "12:05"}).show is False


def test_an_interval_entry_stops_after_its_end():
    """``end`` 对循环条说的是「今天循环到几点为止」。"""
    assert _show([_e(at="", interval_min=60, end="09:00")], _at(9, 30)).show is False
    assert _show([_e(at="", interval_min=60, end="12:00")], _at(9, 30)).show is True


# ── 登场：作答 ＋ 工作时长（旧 derived 的「每天问我」） ────────────────


def test_a_derived_entry_uses_todays_answer_plus_work_hours():
    e = _e(at="", work_hours=9, face=FACE_COUNTDOWN)
    st = _show([e], _at(18, 0), day_values={"e1": "09:00"})
    assert st.show is True
    assert st.countdown_s == 0
    assert st.rows[0].time_label == "18:00", "推算法没有字面 end ⇒ 报算出来的下班时刻"


def test_a_derived_entry_does_not_show_without_a_day_value():
    """没答（或答案是脏值）⇒ **不登场** —— 不答就不启动计时，兜底值已砍掉（§2.3）。"""
    e = _e(at="", work_hours=9)
    assert _show([e], _at(18, 0)).show is False
    assert _show([e], _at(18, 0), day_values={"e1": "不是时间"}).show is False


def test_work_hours_true_is_not_read_as_one_hour():
    """``bool`` 是 ``int`` 的子类：``work_hours: true`` 不许被当成「1 小时」。"""
    e = _e(at="", work_hours=True)
    assert _show([e], _at(10, 0), day_values={"e1": "09:00"}).show is False


# ── 窗口可以跨午夜 ────────────────────────────────────────────────────


def test_a_window_can_cross_midnight():
    e = _e(at="23:00", end="01:00")
    assert _show([e], _at(23, 30)).show is True
    assert _show([e], _at(0, 30, day=9)).show is True
    assert _show([e], _at(2, 0, day=9)).show is False


def test_a_countdown_crossing_midnight_counts_the_whole_span():
    """★ 跨午夜窗口的倒计时要数**整段**，不能在跨过零点之前恒显 ``00:00``。

    ``23:00 → 01:00`` 共两小时：23:30 那一刻还剩 90 分钟。窗口判定（``_inside_window``）
    是按日圆取模的，故这一条确实在窗口里；而读数若按直线减，``end_s - now_s`` 是负数、
    被 ``max(0, …)`` 夹成 0 —— 判定说「还在窗口里」、钟面说「还剩 0」，同一件事两个答案。
    三个采样点分跨零点两侧，任一侧算错都会红。
    """
    e = _e(at="23:00", end="01:00", face=FACE_COUNTDOWN, text="剩 {剩余}")
    assert _show([e], _at(23, 0)).countdown_s == 2 * 3600
    assert _show([e], _at(23, 30)).countdown_s == 90 * 60
    after = _show([e], _at(0, 30, day=9))
    assert after.countdown_s == 30 * 60
    assert after.rows[0].text == "剩 30:00", "行里那句 {剩余} 与钟面同源，不许两样"


def test_a_countup_crossing_midnight_keeps_elapsing():
    """与上面那条对称：正计时过了零点**继续往上走**，不是归零重来。"""
    e = _e(at="23:00", end="01:00", face=FACE_COUNTUP)
    assert _show([e], _at(23, 30)).countup_s == 30 * 60
    assert _show([e], _at(0, 30, day=9)).countup_s == 90 * 60


def test_the_window_verdict_is_the_same_anywhere_inside_a_minute():
    """同一分钟内任何一秒判定相同 —— 一拍 60s 的组件要能撞上窗口，不许靠相位碰运气。

    分辨点只在**窗口末尾那一分钟**：按秒判的窗口到 ``dismiss_at`` 那一刻就结束，于是
    ``10:00:00`` 在里、``10:00:01`` 就出来了 —— 同一分钟里两个答案，而某拍撞上哪一秒是
    随机的。按分钟判则这一整分钟答案相同（窗口覆盖到 ``10:00:59``），下一分钟整分钟都不在。
    """
    e = _e(at="09:30", end="10:00")
    assert [_show([e], _at(10, 0, s)).show for s in (0, 17, 59)] == [True] * 3
    assert [_show([e], _at(10, 1, s)).show for s in (0, 17, 59)] == [False] * 3


def test_a_zero_length_window_covers_its_whole_minute():
    """``end == at``（没填「延后退出」）的窗口在分钟粒度下是**一整分钟**，不是一瞬。

    按秒判它只在 ``at`` 那**一秒**为真 ⇒ 60 秒一拍基本必然错过它 —— 而这正是**默认配置**
    （``lead = trail = 0``）。判据两头都取：``at`` 那一分钟里三秒全中，前后各一分钟全不中
    （少了后半句，把窗口开成两分钟也照样绿）。
    """
    e = _e(at="09:30", end="09:30")
    assert [_show([e], _at(9, 30, s)).show for s in (0, 30, 59)] == [True] * 3
    assert _show([e], _at(9, 29, 59)).show is False
    assert _show([e], _at(9, 31, 0)).show is False


# ── 「预览这一条」按哪一刻算（旧组件的「测试这一条」） ──────────────────
#
# 2026-10-08 用户真机上报：配了一条「每天问上班时间 ＋ 10 小时 ⇒ 倒计时」的推算下班
# 条目（提前 8 分钟登场），点「预览这一条」浮层上写着 **00:00**。根因是预览取的
# 「登场时刻」拿了 `_span_of` 的第一个返回值 —— 那一支给的是 `(下班, 下班)`，
# 于是倒计时算成「下班 − 下班」。常态化那一侧一直是对的，只有预览这一处错。


def test_previewing_a_derived_entry_shows_the_countdown_from_its_entrance():
    """预览推算下班：倒计时从**它登场那一刻**起算，不是 00:00。"""
    e = _e(id="e1", at="", work_hours=10, before_min=8, face=FACE_COUNTDOWN)
    st = preview_entry(e, _at(17, 34), "small", {"e1": "09:00"})
    assert st.show is True
    assert st.face == FACE_COUNTDOWN
    assert st.countdown_s == 8 * 60, "登场 = 下班 − 提前 8 分 ⇒ 倒计时该从 08:00 起"
    assert st.value == "08:00"
    assert st.rows[0].time_label == "19:00", "行右侧仍报下班时刻"


def test_preview_while_it_is_really_showing_reports_the_real_remaining():
    """此刻真在窗口里 ⇒ 预览报的必须是**此刻真的剩余**，不是整段时长。"""
    e = _e(id="e1", at="", work_hours=10, before_min=8, face=FACE_COUNTDOWN)
    # 上班 09:00 ⇒ 下班 19:00；提前 8 分 ⇒ 窗口 [18:52, 19:00]。此刻 18:55 在窗口里。
    st = preview_entry(e, _at(18, 55), "small", {"e1": "09:00"})
    assert st.countdown_s == 5 * 60
    assert st.value == "05:00"


def test_preview_of_a_literal_entry_uses_its_own_at():
    """字面 ``at`` 的条目（没填提前量）：登场时刻就是它 ⇒ 报整段时长。"""
    e = _e(at="17:00", end="18:00", face=FACE_COUNTDOWN)
    st = preview_entry(e, _at(10, 0), "small")
    assert st.countdown_s == 60 * 60


def test_preview_moves_with_before_min():
    """同一条字面条目，提前量把**窗口起点**往前挪 ⇒ 预览跟着挪（与 :func:`_entry_windows` 同一口径）。"""
    e = _e(at="17:00", end="18:00", before_min=10, face=FACE_COUNTDOWN)
    st = preview_entry(e, _at(10, 0), "small")
    assert st.countdown_s == 70 * 60


def test_preview_of_a_derived_entry_without_an_answer_still_says_so():
    """今天还没答上班时间 ⇒ 没有窗口，预览**不说谎也不硬凑**（``show=False``）。"""
    e = _e(id="e1", at="", work_hours=10, before_min=8, face=FACE_COUNTDOWN)
    assert preview_entry(e, _at(17, 34), "small", {}).show is False


# ── 钟面：多条同时命中只出一面（倒计时 > 正计时 > 时钟） ──────────────


def test_only_one_clock_and_countdown_beats_countup():
    """倒计时优先于正计时；另一条**只贡献行**、不贡献钟面。"""
    down = _e(id="d", at="09:00", end="10:00", face=FACE_COUNTDOWN)
    up = _e(id="u", at="09:00", face=FACE_COUNTUP)
    st = _show([up, down], _at(9, 30))

    assert st.face == FACE_COUNTDOWN
    assert st.countdown_s == 1800
    assert st.countup_s is None, "两型的秒数不许同时有值"
    assert len(st.rows) == 2


def test_countup_beats_the_plain_clock():
    up = _e(id="u", at="09:00", face=FACE_COUNTUP)
    st = _show([up, _e(id="c", at="09:00")], _at(9, 30))
    assert st.face == FACE_COUNTUP
    assert st.countup_s == 1800


def test_falls_back_to_the_live_clock_when_nothing_opens_a_face():
    """没有任何条目开 ``face`` ⇒ 走实时钟（钟面区一直在，§3.1）。"""
    st = _show([_e(at="09:30")], _at(9, 30))
    assert st.face == FACE_CLOCK
    assert st.value == "09:30:00"
    assert st.countdown_s is None and st.countup_s is None


def test_an_unrecognized_face_lists_a_row_without_taking_the_clock():
    """认不出的 ``face`` 按「不上钟面」处理 —— **不回落成 clock**（那会让一个拼错的
    ``countdown`` 静默变成一面实时钟），但仍照常列一行。"""
    st = _show([_e(at="09:30", face="countdwn")], _at(9, 30))
    assert st.face == FACE_CLOCK
    assert len(st.rows) == 1


# ── 行：排序 / 上限 / 收起 / 文案 ─────────────────────────────────────


def test_rows_are_ordered_by_their_entrance_time():
    late = _e(id="late", at="10:00")
    early = _e(id="early", at="09:00")
    st = _show([late, early], _at(10, 30))
    assert [r.entry_id for r in st.rows] == ["early", "late"]


def test_rows_are_capped_with_a_remaining_count():
    """到上限只显示前 N 条 ＋ 一行「还有 k 条」；但 ``active_ids`` 仍是**全部**。"""
    entries = [_e(id=f"e{i}", at=f"0{i}:00") for i in range(1, 5)]
    st = _show(entries, _at(9, 0), max_rows=2)

    assert len(st.rows) == 2
    assert st.more_text == "还有 2 条"
    assert len(st.active_ids) == 4, "被折叠的也要在 active_ids 里（抑制判据得认全这一批）"


def test_max_rows_zero_means_no_cap():
    """设置页那一格填 0 ＝ 不限（别截断）。"""
    entries = [_e(id=f"e{i}", at=f"0{i}:00") for i in range(1, 5)]
    st = _show(entries, _at(9, 0), max_rows=0)
    assert len(st.rows) == 4
    assert st.more_text == ""


def test_exactly_at_the_cap_has_no_remaining_line():
    entries = [_e(id=f"e{i}", at=f"0{i}:00") for i in range(1, 3)]
    st = _show(entries, _at(9, 0), max_rows=2)
    assert len(st.rows) == 2
    assert st.more_text == ""


def test_hidden_entries_are_kept_off_the_screen():
    """点了「稍后」或今天已勾完的：内核只认「这一轮收起来」这件事，不重复判一遍。"""
    a, b = _e(id="a", at="09:00"), _e(id="b", at="09:00")
    st = _show([a, b], _at(9, 30), hidden_ids=frozenset({"a"}))
    assert [r.entry_id for r in st.rows] == ["b"]
    assert st.active_ids == frozenset({"b"})


def test_hiding_the_only_entry_turns_the_overlay_off():
    st = _show([_e(id="a", at="09:00")], _at(9, 30), hidden_ids=frozenset({"a"}))
    assert st.show is False
    assert st.rows == []
    assert st.active_ids == frozenset()


def test_a_blank_title_falls_back_to_a_known_sentence():
    st = _show([_e(title="   ")], _at(9, 30))
    assert st.rows[0].title == FALLBACK_TITLE


def test_actions_are_filtered_to_the_two_known_ones():
    """认不出的动作丢掉；**按条目配**（不配就不画那枚按钮）。"""
    st = _show([_e(actions=["done", "bogus", "snooze"])], _at(9, 30))
    assert st.rows[0].actions == (ACTION_DONE, ACTION_SNOOZE)
    assert _show([_e(id="e2", actions="snooze")], _at(9, 30)).rows[0].actions == (ACTION_SNOOZE,)
    assert _show([_e(id="e3")], _at(9, 30)).rows[0].actions == ()
    assert _show([_e(id="e4", actions=["bogus"])], _at(9, 30)).rows[0].actions == ()


def test_row_text_renders_placeholders_at_render_time():
    e = _e(text="剩 {剩余}（{到点} 开始）", at="09:00", end="10:00")
    st = _show([e], _at(9, 30))
    assert st.rows[0].text == "剩 30:00（09:00 开始）"


# ── 入口过滤：禁用 / 场景 / 没有 id / 空表 ────────────────────────────


def test_disabled_entries_do_not_show():
    assert _show([_e(enabled=False)], _at(9, 30)).show is False


def test_the_scene_gate_blocks_until_its_scene_is_current():
    """填了场景的条目：只有此刻命中的场景与它同名才放行；**「还没读到」判成不放行**。"""
    e = _e(scene="Office")
    assert _show([e], _at(9, 30)).show is False
    assert _show([e], _at(9, 30), scene="Home").show is False
    assert _show([e], _at(9, 30), scene="Office").show is True


def test_an_entry_without_a_scene_is_not_gated():
    """没填场景 ⇒ 不限（与加门禁之前一字不差）。"""
    assert _show([_e()], _at(9, 30), scene="Whatever").show is True


def test_an_entry_without_an_id_is_skipped():
    """没有 id 的条目画不成行、也点不了 ⇒ 内核直接丢。"""
    assert _show([{"enabled": True, "at": "09:30"}], _at(9, 30)).show is False


def test_no_entries_means_the_overlay_is_off():
    st = _show([], _at(9, 30))
    assert st.show is False
    assert st.face == FACE_CLOCK
    assert st.value == ""


# ── Row 要能跨线程也能比对 ────────────────────────────────────────────


def test_row_is_frozen_so_the_overlay_can_compare_and_skip_rebuilds():
    """``Row`` 跨线程（worker 算出、主线程画），浮层靠 ``==`` 跳过「没变」的重建。
    故它必须 frozen（只读）且可比较。"""
    r = Row("e1", "喝水", "", "09:30", (), 34200)
    assert r == Row("e1", "喝水", "", "09:30", (), 34200)
    assert r != Row("e1", "喝水", "", "09:31", (), 34200)
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.title = "改了"  # type: ignore[misc]
