"""tests/test_todo_task：任务声明层与求值层（变量池与任务解耦 · Phase 3，设计文档 §4）。

守两件事：

- **声明层**（``parse_all`` / ``check_section``）：一条写错的任务只让**这一条**不生效，
  其余照常；整段的形状问题（id 重复那种）才拒整份。
- **求值层**（``resolve_item``）：一条任务 ＋ 此刻的变量池 → 内核认的条目。这一层是
  新模型与旧内核之间的**唯一**接口，故最后一节拿真的 ``scheduler.compute_display``
  对拍一次 —— 只断言 dict 的键对得上，证明不了浮层真认它。

变量池用**真的** ``VariablePool``（不写替身）：这一层要的正是「与池接得上」，
而假池只会把接不上的地方一起假装掉。
"""

from __future__ import annotations

import datetime

import pytest

from barkit.core.events import ROUTE_PROFILE_TOPIC
from barkit.core.variables.pool import VariablePool
from barkit.plugins.todo import scheduler
from barkit.plugins.todo import task as task_mod
from barkit.plugins.todo.task import TaskSpec, parse_all

#: 此刻用的那个「今天」。周六/周内的判据各要一个，故常量与工厂分开。
MOMENT = datetime.datetime(2026, 10, 10, 15, 0, 0)      # 周六
WEEKDAY_MOMENT = datetime.datetime(2026, 10, 9, 15, 0, 0)  # 周五

DECLS = [
    {"id": "scene", "name": "当前场景", "source": "component",
     "component": "route_guard", "key": "profile", "field": "summary"},
    {"id": "start_time", "name": "上班时间", "source": "ask", "type": "time",
     "trigger": {"when": ["daily"]}, "prompt": "今天几点上班？"},
    {"id": "off_time", "name": "下班时间", "source": "derived", "expr": "start_time + 9h"},
]


class _FakeReport:
    """组件状态快照的替身（池只取 ``state`` / ``summary`` 两个字段）。"""

    def __init__(self, state: str = "active", summary: str = "办公室") -> None:
        self.state = state
        self.summary = summary


def _pool(*, moment: datetime.datetime = MOMENT, start: "str | None" = None,
          scene: "str | None" = "办公室") -> VariablePool:
    pool = VariablePool(clock=lambda: moment)
    pool.load(DECLS)
    pool.begin_pass()
    if scene is not None:
        pool.on_status("plugin.route_guard.status.profile", _FakeReport(summary=scene))
    if start is not None:
        assert pool.set_ask_value("start_time", start)
    pool.begin_pass()
    return pool


def _one(raw: dict) -> TaskSpec:
    specs, problems = parse_all([raw])
    assert not problems, problems
    assert len(specs) == 1
    return specs[0]


# ── 声明层：整段形状 ────────────────────────────────────────────────


def test_check_section_takes_a_well_formed_list():
    task_mod.check_section([{"id": "a"}, {"id": "b_2"}])


@pytest.mark.parametrize(
    "value",
    [
        {"id": "a"},                      # 不是列表
        ["a"],                            # 某项不是 mapping
        [{"title": "没有 id"}],            # id 缺失
        [{"id": "1a"}],                   # id 不以数字开头
        [{"id": "a"}, {"id": "a"}],        # id 重复
    ],
)
def test_check_section_rejects_bad_shapes(value):
    with pytest.raises(task_mod.TaskSpecError):
        task_mod.check_section(value)


def test_check_section_lets_none_through():
    """没写过这一段 ⇒ 不是错（与 ``variables`` 那条同款）。"""
    task_mod.check_section(None)


# ── 声明层：逐条语义 ────────────────────────────────────────────────


def test_a_minimal_task_takes_every_default():
    """``id`` ＋ ``title`` 是最小合法声明；显示面默认**不显示**（§4.4 那张表）。"""
    spec = _one({"id": "water", "title": "喝水"})
    assert (spec.id, spec.title) == ("water", "喝水")
    assert spec.enabled is True
    assert spec.text == ""
    assert spec.loop.kind == task_mod.LOOP_DAILY
    assert spec.window is None
    assert spec.display.show == task_mod.SHOW_NONE
    assert spec.display.reminder.at == ""
    assert spec.display.buttons == ()
    assert spec.clock_of() == ""            # 内核那一型：不上钟面
    assert spec.shows_on_overlay() is False


def test_a_task_needs_completion_unless_it_says_otherwise():
    """``track_done`` 默认 **开**（＝现在所有条目的行为）；写 ``false`` 才是「只显示、做不掉」。"""
    assert _one({"id": "water", "title": "喝水"}).track_done is True
    assert _one({"id": "clock", "title": "整点", "track_done": False}).track_done is False


def test_a_non_boolean_track_done_is_reported():
    """``track_done`` 得像 ``enabled`` 那样认布尔 —— 写下 ``"no"`` 只让这一条不生效，
    其余照常（与「一条写错不拖垮整份」同款）。"""
    specs, problems = parse_all([
        {"id": "good", "title": "好的"},
        {"id": "clock", "title": "整点", "track_done": "no"},
    ])
    assert [s.id for s in specs] == ["good"]
    assert len(problems) == 1 and "track_done" in problems[0]


def test_one_bad_task_does_not_take_the_others_down():
    specs, problems = parse_all([
        {"id": "good", "title": "好的"},
        {"id": "bad"},                       # 缺 title
        {"id": "also_good", "title": "也好"},
    ])
    assert [s.id for s in specs] == ["good", "also_good"]
    assert len(problems) == 1 and "title" in problems[0]


def test_conditions_become_rule_objects():
    spec = _one({
        "id": "report", "title": "提交日报",
        "effective": {"conditions": [
            {"var": "scene", "op": "==", "value": "办公室"},
            {"var": "weekday", "op": "in", "value": [0, 1, 2]},
        ]},
    })
    assert [(c.var, c.op) for c in spec.conditions] == [("scene", "=="), ("weekday", "in")]


def test_a_bad_condition_is_reported():
    specs, problems = parse_all([{
        "id": "x", "title": "X",
        "effective": {"conditions": [{"var": "scene", "op": "~~", "value": 1}]},
    }])
    assert specs == ()
    assert len(problems) == 1 and "op" in problems[0]


@pytest.mark.parametrize(
    ("loop", "expect"),
    [
        ("daily", task_mod.LOOP_DAILY),
        ("weekday", task_mod.LOOP_WEEKDAY),
        ({"kind": "interval", "every": "30m"}, task_mod.LOOP_INTERVAL),
    ],
)
def test_loop_accepts_shorthand_and_mapping(loop, expect):
    spec = _one({"id": "x", "title": "X", "effective": {"loop": loop}})
    assert spec.loop.kind == expect


def test_interval_loop_folds_every_into_minutes():
    spec = _one({"id": "x", "title": "X",
                 "effective": {"loop": {"kind": "interval", "every": "2h"}}})
    assert spec.loop.minutes == 120


@pytest.mark.parametrize("loop", [
    {"kind": "interval"},                     # 缺 every
    {"kind": "interval", "every": "0m"},      # 不是正数
    {"kind": "interval", "every": "30"},      # 没有单位
    {"kind": "hourly"},                       # 不认识
])
def test_bad_loops_are_rejected(loop):
    specs, problems = parse_all([{"id": "x", "title": "X", "effective": {"loop": loop}}])
    assert specs == ()
    assert len(problems) == 1


def test_window_takes_either_end_alone():
    """只填一端**不是错**：空着的那头由求值层放开到日的边界（用户口径：不填＝不限）。

    旧口径要求两端都写，缺一端就把**整条任务**丢掉 —— 用户所见是它从清单里消失且
    没有任何线索，而编辑屏那两格的占位符写着「留空 ＝ 从零点起 / 到当天结束」。
    """
    spec = _one({"id": "x", "title": "X", "effective": {"window": {"from": "09:00"}}})
    assert spec.window == task_mod.TaskWindow("09:00", "")


def test_an_empty_window_block_means_no_window():
    """两端都空 ⇒ 与不写这一块**同形**（``window is None``）。

    同形这一半承重：摘要的时段那几个字与循环条的收工时刻都判 ``is not None``，若留一个
    空壳 ``TaskWindow("","")`` 在那里，摘要会说出一个时段、而它其实全天候。
    """
    for win in ({}, {"from": "  ", "to": ""}):
        assert _one({"id": "x", "title": "X",
                     "effective": {"window": win}}).window is None


def test_window_takes_variable_references():
    spec = _one({"id": "x", "title": "X",
                 "effective": {"window": {"from": "{off_time}", "to": "23:00"}}})
    assert spec.window == task_mod.TaskWindow("{off_time}", "23:00")


def test_an_unknown_show_is_rejected():
    """``display.show`` 是五选一，认不出的取值当场报出来（那条任务整条不生效）。"""
    specs, problems = parse_all([{"id": "x", "title": "X", "display": {"show": "timer"}}])
    assert specs == ()
    assert len(problems) == 1 and "show" in problems[0]


def test_the_legacy_overlay_and_clock_fold_into_show():
    """★ 旧的两键（``overlay`` ＋ ``clock``）在**读的那一刻**折成 ``show``。

    折叠规则就是当时**实际生效**的语义，逐条钉住 —— 这四行就是「老盘上那一条打开之后
    该显示成什么」，改错哪一行都会让用户的显示面在无人察觉时换一档：

    - ``overlay`` 不在 / 是假 ⇒ ``none``。那一格里就算写了 ``clock`` 也**从来没人读**
      （没有窗口就选不出钟面，见 ``SHOW_*`` 的说明）⇒ 丢掉它与保留它行为相同，故脏的
      ``clock`` 在这一支上**不报**（它本来就没生效过）；
    - ``overlay`` 真 ＋ ``clock`` 是三型之一 ⇒ 取那一型；
    - ``overlay`` 真 ＋ ``clock`` 空 / ``none`` ⇒ ``row``；
    - ``overlay`` 真 ＋ ``clock`` 是别的字符串 ⇒ **报出来**（静默降级成 ``row`` 会让用户
      以为自己配的那一档还在）。
    """
    def _show(raw):                          # noqa: ANN001 —— 测试内小工具
        return _one({"id": "x", "title": "X", "display": raw}).display.show

    assert _show({}) == task_mod.SHOW_NONE
    assert _show({"clock": "timer"}) == task_mod.SHOW_NONE
    assert _show({"overlay": False, "clock": "clock"}) == task_mod.SHOW_NONE
    assert _show({"overlay": True}) == task_mod.SHOW_ROW
    assert _show({"overlay": True, "clock": ""}) == task_mod.SHOW_ROW
    assert _show({"overlay": True, "clock": "none"}) == task_mod.SHOW_ROW
    assert _show({"overlay": True, "clock": "countdown"}) == scheduler.FACE_COUNTDOWN

    specs, problems = parse_all([{
        "id": "x", "title": "X", "display": {"overlay": True, "clock": "timer"},
    }])
    assert specs == ()
    assert len(problems) == 1 and "clock" in problems[0]

    # 新键在场 ⇒ **不再看旧键**（新键是权威）：两键并存时旧键一律被忽略。
    assert _show({"show": "row", "overlay": True, "clock": "clock"}) == task_mod.SHOW_ROW


def test_show_of_reads_the_folded_value():
    """编辑屏那一格读的是 :func:`task.show_of`（折叠之后的值），不是 ``raw["show"]``。

    直读新键的症状：盘上写成旧两键的那一条，打开时那一格显示成「不显示」，而用户什么都
    没改、按一次「确定」就真把它改成不显示了 —— 正是这一格最初要避免的那类静默改写。
    """
    assert task_mod.show_of({"overlay": True, "clock": "countdown"}) \
        == scheduler.FACE_COUNTDOWN
    assert task_mod.show_of({"overlay": True}) == task_mod.SHOW_ROW
    assert task_mod.show_of({}) == task_mod.SHOW_NONE
    assert task_mod.show_of(None) == task_mod.SHOW_NONE      # 整段不是 mapping
    assert task_mod.show_of({"overlay": True, "clock": "timer"}) == task_mod.SHOW_NONE


def test_loop_minutes_of_reads_the_written_shape():
    """编辑屏那一格读的是 :func:`task.loop_minutes_of`（解析层折出来的分钟数）。

    盘上那一块写的是 ``{"kind": "interval", "every": "30m"}`` —— 没有任何地方写过
    ``minutes`` 这个键。直读 ``loop["minutes"]`` 的症状：那一格永远显示「不循环」，
    用户什么都没改按一次「确定」就把整块删掉（界面还显示「已保存」）。
    两条读法各取一支：``every`` 的两种单位都要认，简写 ``loop: daily`` 没有分钟数。
    """
    assert task_mod.loop_minutes_of({"loop": {"kind": "interval", "every": "30m"}}) == 30
    assert task_mod.loop_minutes_of({"loop": {"kind": "interval", "every": "2h"}}) == 120
    assert task_mod.loop_minutes_of({"loop": "daily"}) == 0        # 不是按间隔循环
    assert task_mod.loop_minutes_of({}) == 0
    assert task_mod.loop_minutes_of(None) == 0                     # 整段不是 mapping
    assert task_mod.loop_minutes_of({"loop": {"kind": "interval", "every": "歪"}}) == 0


def test_a_bare_time_string_is_the_reminder_shorthand():
    spec = _one({"id": "x", "title": "X", "display": {"reminder": "09:00"}})
    assert spec.display.reminder.at == "09:00"


def test_a_bad_time_text_is_rejected_at_parse_time():
    """拼错的时刻要**当场**看得见：留到运行期的表现是「它从来不出现在浮层上」。"""
    specs, problems = parse_all([{"id": "x", "title": "X",
                                  "display": {"reminder": {"at": "25:99"}}}])
    assert specs == ()
    assert len(problems) == 1 and "at" in problems[0]


def test_a_variable_time_text_passes_without_values():
    """含变量的那一支判得**宽**（严 ⊂ 宽）：值要等求值才知道。"""
    spec = _one({"id": "x", "title": "X",
                 "display": {"reminder": {"at": "{off_time} + 30m"}}})
    assert spec.display.reminder.at == "{off_time} + 30m"


def test_buttons_are_checked_and_deduped():
    spec = _one({"id": "x", "title": "X",
                 "display": {"buttons": ["完成", "稍后", "完成"]}})
    assert spec.display.buttons == ("完成", "稍后")
    assert spec.actions_of() == (scheduler.ACTION_DONE, scheduler.ACTION_SNOOZE)

    specs, problems = parse_all([{"id": "y", "title": "Y",
                                  "display": {"buttons": ["done"]}}])
    assert specs == ()
    assert len(problems) == 1 and "buttons" in problems[0]


def test_actions_of_drops_done_when_the_task_needs_no_completion():
    """``track_done=False`` ⇒ 不给 ``done``：那枚按钮点下去只会记一笔空账（不进那两个数、
    不画状态点、行内也不画它），留着就是一格空转。

    ``snooze`` 不跟着它走 —— 「稍后」换的是浮层上那一行的去留，与「完成」是两件事。
    """
    spec = _one({"id": "x", "title": "X", "track_done": False,
                 "display": {"buttons": ["完成", "稍后"]}})
    assert spec.actions_of() == (scheduler.ACTION_SNOOZE,)


def test_a_task_that_needs_no_completion_cannot_be_marked_done(tmp_path):
    """``track_done=False`` 的条目在 ``set_done`` 这道**唯一写口**上被拒。

    上游（行内 / 菜单 / 浮层）已经不画入口，这道闸是兜底 —— 记一笔也哪儿都不显示。
    """
    plugin = _plugin(tmp_path, [{"id": "clock", "title": "整点时钟", "track_done": False}])
    assert plugin.set_done("clock") is False
    assert plugin.done_at("clock") == ""


@pytest.mark.parametrize("value", [-1, True, "30"])
def test_lead_minutes_must_be_a_non_negative_int(value):
    specs, problems = parse_all([{"id": "x", "title": "X",
                                  "display": {"lead_min": value}}])
    assert specs == ()
    assert len(problems) == 1


def test_the_command_is_taken_as_is_and_its_moment_defaults_to_the_reminder(caplog):
    """``command`` 原样收下（**这一层不切分、不执行**，见 ``parse_item`` 那段注释）；
    「在哪一刻跑」默认 ``reminder`` —— 两条都要钉。

    默认值钉在这里而不是留给界面：盘上手写的那一份（没人过界面）读出来必须同样落在
    ``reminder``，否则「我只想在提醒那一刻跑」在盘上会变成「还在别处又跑一次」。
    """
    with caplog.at_level("WARNING", logger="barkit.plugins.todo.task"):
        spec = _one({"id": "x", "title": "X", "command": "open https://example.com"})
    assert spec.command == "open https://example.com"
    assert spec.command_at == task_mod.COMMAND_AT_REMINDER
    assert not [r for r in caplog.records if "command" in r.message], (
        "解析这一层不再对 command 说任何话（执行与切分的结论归 .command）"
    )

    kept, _ = parse_all([
        {"id": "a", "title": "A", "command": "open http://x", "command_at": "done"},
        {"id": "b", "title": "B", "command": "open http://y"},
    ])
    assert [(s.id, s.command_at) for s in kept] == [
        ("a", task_mod.COMMAND_AT_DONE),
        ("b", task_mod.COMMAND_AT_REMINDER),
    ]


def test_an_unknown_command_at_is_rejected():
    specs, problems = parse_all([{"id": "x", "title": "X", "command_at": "whenever"}])
    assert specs == ()
    assert len(problems) == 1 and "command_at" in problems[0]


def test_the_retired_command_on_done_key_is_not_folded():
    """旧盘上那枚 ``command_on_done`` **不参与折叠**。

    它当年说的是「到点之外**再**跑一次」，而四选一里没有「两个点」这一档 —— 折成 ``done``
    会把用户原本就有的「到点跑」整个换掉（那是比「少跑一次」更重的静默改写）。故那个键
    只躺在盘上（没人读、也不报错），取值仍是缺省。
    """
    spec = _one({"id": "x", "title": "X", "command": "open http://x",
                 "command_on_done": True})
    assert spec.command_at == task_mod.COMMAND_AT_REMINDER


def test_a_variable_used_only_in_the_command_still_counts():
    """命令里引用的变量要算进 ``referenced_variables``。

    漏了它的症状有两层：主窗体那条变量条不列它（用户看不到这一条依赖什么），
    以及「变量变了要重算什么」的传播点漏掉这一类字段 —— 改了变量这一条不跟着变，
    而界面上没有线索指向配置。
    """
    spec = _one({"id": "x", "title": "X", "command": "open https://x/?t={off_time}"})
    assert task_mod.referenced_variables(spec) == ("off_time",)


def test_referenced_variables_gives_bare_ids():
    """★ 回来的必须是**裸 id**，不是 ``{id}`` 那一片花括号。

    写错这一格不报任何错：``pool.text_variables`` 若照原样把 ``{off_time}`` 交出来，下游
    ``spec_of("{off_time}")`` 查不到 ⇒ 变量条上写「``{off_time}`` 未定义」（用户看不出是
    谁写错了），Phase 4 的迁移还会**照这个名字建一个变量**。故这里连形状一起钉住。
    """
    spec = _one({
        "id": "report",
        "title": "日报",
        "text": "{off_time} 前提交",
        "effective": {
            "conditions": [{"var": "scene", "op": "==", "value": "办公室"}],
            "window": {"from": "09:00", "to": "{off_time}"},
        },
        "display": {"show": "row", "reminder": {"at": "{off_time}"}},
    })
    got = task_mod.referenced_variables(spec)
    assert got == ("off_time", "scene"), got
    assert all("{" not in name and "}" not in name for name in got), got


def test_referenced_variables_covers_every_field_and_dedupes():
    """五处字段都要扫到、且去重（``window.to`` 与 ``reminder.at`` 是同一个变量）。

    漏一处不会报错，只会让「改了那个变量这一条没跟着变」—— 而界面上没有线索指向配置。

    顺序是**字段顺序 × 字段内出现顺序**（不是全局首次出现）：这条顺序唯一服务的事是主窗体
    那条变量条按什么次序列，而字段顺序对用户可预期。故这里连顺序一起钉住 —— 顺手把字段
    扫描重排的人会当场看到这条转红。
    """
    spec = _one({
        "id": "x",
        "title": "X",
        "text": "{a}{b}",
        "effective": {"window": {"from": "{c}", "to": "{a}"}},
        "display": {"show": "row", "reminder": {"at": "{d}"}},
    })
    assert task_mod.referenced_variables(spec) == ("a", "b", "d", "c")


def test_referenced_variables_ignores_kernel_placeholders():
    """内核自己那几个中文占位符（``{到点}`` / ``{现在}`` / ``{剩余}``）**不是变量**。

    它们交给 ``scheduler`` 在渲染那一刻填；当成变量去池里查会查不到，于是主窗体那条
    变量条上会凭空多出几个「未定义」。
    """
    spec = _one({"id": "x", "title": "X", "text": "{到点} 该走了（现在是 {现在}）"})
    assert task_mod.referenced_variables(spec) == ()


# ── 求值层：时刻解析 ────────────────────────────────────────────────


def test_resolve_time_reads_a_literal():
    assert task_mod.resolve_time("17:27", _pool()) == 17 * 3600 + 27 * 60


def test_resolve_time_reads_a_variable():
    pool = _pool(start="08:27")
    assert task_mod.resolve_time("{off_time}", pool) == 17 * 3600 + 27 * 60


def test_resolve_time_reads_a_variable_plus_a_duration():
    pool = _pool(start="08:27")
    assert task_mod.resolve_time("{off_time} + 30m", pool) == 17 * 3600 + 57 * 60


def test_resolve_time_is_none_when_the_variable_has_no_value():
    """没答上班时间 ⇒ 「下班」没有值。**不是 00:00** —— 后者会在凌晨把人叫起来。"""
    pool = _pool(start=None)
    assert task_mod.resolve_time("{off_time}", pool) is None


def test_resolve_time_ignores_an_unrelated_placeholder():
    """认不出的花括号原样留着（池那条口径）⇒ 引擎报语法错 ⇒ 没有时刻。"""
    assert task_mod.resolve_time("{到点}", _pool()) is None


def test_resolve_time_wraps_past_midnight():
    """``00:30`` 这类跨了午夜的时刻按当日秒数收口（与 ``scheduler`` 同一口径）。"""
    pool = _pool(start="17:00")                     # ＋9h ⇒ 次日 02:00
    assert task_mod.resolve_time("{off_time}", pool) == 2 * 3600


# ── 求值层：条目换算 ────────────────────────────────────────────────


def _task(raw: dict) -> TaskSpec:
    return _one(raw)


def test_enabled_is_false_when_the_user_turned_it_off():
    spec = _task({"id": "x", "title": "X", "enabled": False,
                  "display": {"show": "row", "reminder": "09:00"}})
    assert task_mod.resolve_item(spec, _pool(), MOMENT)["enabled"] is False


def test_enabled_follows_the_conditions():
    spec = _task({"id": "x", "title": "X", "enabled": True,
                  "effective": {"conditions": [{"var": "scene", "op": "==", "value": "办公室"}]}})
    assert task_mod.resolve_item(spec, _pool(scene="办公室"), MOMENT)["enabled"] is True
    assert task_mod.resolve_item(spec, _pool(scene="家里"), MOMENT)["enabled"] is False


def test_enabled_is_false_when_the_condition_cannot_be_judged():
    """「判不出来」按不生效处理（§3.1 那条口径），并且**记一条日志**说清是为什么。"""
    spec = _task({"id": "x", "title": "X", "enabled": True,
                  "effective": {"conditions": [{"var": "scene", "op": "==", "value": "办公室"}]}})
    assert task_mod.resolve_item(spec, _pool(scene=None), MOMENT)["enabled"] is False


def test_a_task_with_no_display_gets_no_window():
    """「不显示」：不给窗口 ⇒ 旧内核的 ``_span_of`` 返回 ``None`` ⇒ 不上浮层。"""
    spec = _task({"id": "x", "title": "X", "display": {"reminder": "09:00"}})
    item = task_mod.resolve_item(spec, _pool(), MOMENT)
    assert item["face"] == ""
    assert (item["at"], item["end"], item["interval_min"], item["before_min"]) == ("", "", 0, 0)


def test_a_shown_task_with_a_reminder_gets_the_leads_and_trails():
    """窗口 = ``[at − lead_min, at + trail_min]``（§4.3）：两格都是相对量。"""
    spec = _task({"id": "x", "title": "X", "display": {
        "show": "row", "reminder": {"at": "17:00"},
        "lead_min": 30, "trail_min": 45,
    }})
    item = task_mod.resolve_item(spec, _pool(), MOMENT)
    assert item["at"] == "17:00"
    assert item["end"] == "17:45"
    assert item["before_min"] == 30


def test_a_shown_task_without_a_trail_ends_at_the_reminder_moment():
    spec = _task({"id": "x", "title": "X", "display": {"show": "row", "reminder": "17:00"}})
    assert task_mod.resolve_item(spec, _pool(), MOMENT)["end"] == "17:00"


def test_an_interval_task_uses_the_window_end_as_its_stop():
    """循环条的 ``end`` 在旧内核里是「今天循环到几点为止」，新模型对应 ``window.to``。"""
    spec = _task({"id": "x", "title": "X", "effective": {
        "loop": {"kind": "interval", "every": "30m"},
        "window": {"from": "09:00", "to": "18:00"},
    }, "display": {"show": "row"}})
    item = task_mod.resolve_item(spec, _pool(), MOMENT)
    assert item["interval_min"] == 30
    assert item["end"] == "18:00"


def test_the_window_gate_keeps_a_task_out_side_its_hours():
    spec = _task({"id": "x", "title": "X",
                  "effective": {"window": {"from": "09:00", "to": "10:00"}}})
    at_15 = datetime.datetime(2026, 10, 10, 15, 0)
    at_09 = datetime.datetime(2026, 10, 10, 9, 30)
    assert task_mod.resolve_item(spec, _pool(), at_15)["enabled"] is False
    assert task_mod.resolve_item(spec, _pool(), at_09)["enabled"] is True


def test_the_window_gate_handles_a_cross_midnight_span():
    spec = _task({"id": "x", "title": "X",
                  "effective": {"window": {"from": "22:00", "to": "02:00"}}})
    inside = datetime.datetime(2026, 10, 10, 23, 30)
    outside = datetime.datetime(2026, 10, 10, 12, 0)
    assert task_mod.resolve_item(spec, _pool(), inside)["enabled"] is True
    assert task_mod.resolve_item(spec, _pool(), outside)["enabled"] is False


def test_a_window_with_only_a_start_runs_to_the_end_of_the_day():
    """只写「起 09:00」⇒ 9 点之后一直有效（止那一头放开到当天结束）。

    这一条抓的是**判定那一半**：只改解析层的话，任务不会被丢掉了，但 8:59 也照样在
     「时段内」 —— 写着「起 09:00」却全天候生效。
    """
    spec = _task({"id": "x", "title": "X", "effective": {"window": {"from": "09:00"}}})
    before = datetime.datetime(2026, 10, 10, 8, 59)
    late = datetime.datetime(2026, 10, 10, 23, 30)
    assert task_mod.resolve_item(spec, _pool(), before)["enabled"] is False
    assert task_mod.resolve_item(spec, _pool(), late)["enabled"] is True


def test_a_window_with_only_an_end_starts_at_midnight():
    """只写「止 18:00」⇒ 零点起有效、过点退场（起那一头放开到零点）。

    与上面那条是一对：单改一层都不算数 —— 解析层放行了而判定层仍放掉另一头的话，
    这两条里必有一条红，且不红的那条没有任何提示。
    """
    spec = _task({"id": "x", "title": "X", "effective": {"window": {"to": "18:00"}}})
    early = datetime.datetime(2026, 10, 10, 0, 30)
    past = datetime.datetime(2026, 10, 10, 18, 1)
    assert task_mod.resolve_item(spec, _pool(), early)["enabled"] is True
    assert task_mod.resolve_item(spec, _pool(), past)["enabled"] is False


# ── 求值层：时段的用户话（清单摘要 / 编辑屏状态行） ──────────────────────


def test_the_window_summary_says_which_hours_it_is():
    """摘要写出**时段本身**，不是「限时段」两个糊词。

    从前的症状：清单行上读得出「这条限了时段」，读不出限的是哪一段 —— 用户既看不出
    它限了什么，也看不出它有没有起作用。
    """
    spec = _task({"id": "x", "title": "X",
                  "effective": {"window": {"from": "09:00", "to": "18:00"}}})
    assert "09:00–18:00 生效" in task_mod.summary_of(spec, _pool())


def test_the_window_summary_says_which_end_is_open():
    """只填一端时，空着的那一头的语义写进词里（与 ``_inside_window`` 同一口径）。"""
    spec = _task({"id": "x", "title": "X", "effective": {"window": {"from": "09:00"}}})
    assert "09:00 起生效" in task_mod.summary_of(spec, _pool())
    spec = _task({"id": "x", "title": "X", "effective": {"window": {"to": "18:00"}}})
    assert "18:00 前生效" in task_mod.summary_of(spec, _pool())


def test_a_cross_midnight_window_says_next_day():
    """起晚于止 ⇒ 跨午夜，词里点明「次日」（不点明的那一行读起来像 22:00 到 02:00
    是一段不存在的时间）。"""
    spec = _task({"id": "x", "title": "X",
                  "effective": {"window": {"from": "22:00", "to": "02:00"}}})
    assert "22:00–次日 02:00 生效" in task_mod.summary_of(spec, _pool())


def test_a_window_end_that_is_still_unset_says_pending():
    """引用了变量而当刻没值的那一端**不写进词里**（只由求得出的那一端说话）。

    写成「待定」的是**两端都**求不出来的情形 —— 与摘要里「提醒时刻待定」同款：
    一端有值时说整条待定，等于把已经说得准的那半句也藏起来。

    本用例不写任何 ask 值：当日账按测试隔离（``conftest`` 的 ``BARKIT_DATA_DIR``），
    ``_pool()`` 读到的就是「今天还没填」那一份。
    """
    spec = _task({"id": "x", "title": "X", "effective": {
        "window": {"from": "{off_time}", "to": "23:00"}}})
    assert task_mod.window_word(spec.window, _pool()) == "23:00 前生效"
    both = _task({"id": "x", "title": "X", "effective": {
        "window": {"from": "{off_time}", "to": "{off_time}"}}})
    assert task_mod.window_word(both.window, _pool()) == "生效时段待定"
    assert task_mod.window_status(both.window, _pool(), MOMENT) == (
        "生效时段待定——引用的变量此刻没有值"
    )


def test_a_resolved_variable_end_lands_in_the_summary():
    """变量填了 ⇒ 那一端按求出来的值说（与前一条是一对：单改一层都不算数）。"""
    spec = _task({"id": "x", "title": "X", "effective": {
        "window": {"from": "{off_time}", "to": "23:00"}}})
    assert task_mod.window_word(spec.window, _pool(start="08:27")) == "17:27–23:00 生效"


def test_the_window_status_says_whether_it_is_in_force_now():
    """编辑屏那一行状态：此刻在不在时段内。

    判据借 :func:`_inside_window`（不另立一套）——「清单说不生效、编辑屏说生效」
    那种不一致不报错，而用户只看得到其中一处。
    """
    spec = _task({"id": "x", "title": "X",
                  "effective": {"window": {"from": "09:00", "to": "18:00"}}})
    pool = _pool()
    at_noon = datetime.datetime(2026, 10, 10, 12, 0)
    at_night = datetime.datetime(2026, 10, 10, 21, 3)
    assert task_mod.window_status(spec.window, pool, at_noon) == "此刻在时段内（09:00–18:00 生效）"
    assert task_mod.window_status(spec.window, pool, at_night) == (
        "此刻不在时段内（09:00–18:00 生效，现在 21:03）"
    )


def test_the_window_status_still_names_the_span_when_the_ends_are_variables():
    """两格填 ``{变量}`` 时，状态行**仍要把解析出的时段写出来**。

    只报「现在几点」的话，用户在这扇窗里读不到 ``{off_time}`` 当刻算成几点 —— 他
    既不知道时段是什么，也不知道为什么此刻不作数（变量填了 ⇒ 17:27–23:00 生效，
    ``MOMENT`` 的 15:00 落在它前面）。
    """
    spec = _task({"id": "x", "title": "X", "effective": {
        "window": {"from": "{off_time}", "to": "23:00"}}})
    pool = _pool(start="08:27")
    assert task_mod.window_status(spec.window, pool, MOMENT) == (
        "此刻不在时段内（17:27–23:00 生效，现在 15:00）"
    )


def test_the_window_status_of_an_unset_variable_end():
    """两端引用的变量当刻都没值 ⇒ 说清是「没有值」，别显示成一个求值故障。"""
    spec = _task({"id": "x", "title": "X", "effective": {
        "window": {"from": "{off_time}", "to": "{off_time}"}}})
    assert task_mod.window_status(spec.window, _pool(), MOMENT) == (
        "生效时段待定——引用的变量此刻没有值"
    )
    # 没配时段 ⇒ 空串（「全天候」那句是界面文案，由编辑屏自己说）
    assert task_mod.window_status(None, _pool(), MOMENT) == ""


def test_the_text_is_interpolated_but_keeps_the_kernel_placeholders():
    spec = _task({"id": "x", "title": "提交日报", "text": "{off_time} 前提交（还剩{剩余}）"})
    item = task_mod.resolve_item(spec, _pool(start="08:27"), MOMENT)
    assert item["text"] == "17:27 前提交（还剩{剩余}）"


def test_clock_and_actions_are_translated_to_kernel_words():
    spec = _task({"id": "x", "title": "X", "display": {
        "show": "countdown", "buttons": ["完成"],
    }})
    item = task_mod.resolve_item(spec, _pool(), MOMENT)
    assert item["face"] == scheduler.FACE_COUNTDOWN
    assert item["actions"] == (scheduler.ACTION_DONE,)


def test_only_the_three_face_choices_reach_the_kernel_clock():
    """``show`` 五选一里只有那三档上钟面：``none`` / ``row`` 一律给空串。

    这一格是「并成一格」之后唯一的换算口（``TaskSpec.clock_of``）—— 漏判一档的症状是
    「选了『浮层』却多出一面钟」或反过来（钟面那一档不显示钟）。
    """
    for show, expect in (
        (task_mod.SHOW_NONE, ""),
        (task_mod.SHOW_ROW, ""),
        (scheduler.FACE_CLOCK, scheduler.FACE_CLOCK),
        (scheduler.FACE_COUNTDOWN, scheduler.FACE_COUNTDOWN),
        (scheduler.FACE_COUNTUP, scheduler.FACE_COUNTUP),
    ):
        spec = _task({"id": "x", "title": "X", "display": {"show": show}})
        assert spec.clock_of() == expect, show
        assert spec.shows_on_overlay() is (show != task_mod.SHOW_NONE), show


@pytest.mark.parametrize(
    ("day", "expect"), [("2026-10-10", False), ("2026-10-09", True)]
)
def test_loop_allows_weekdays_only(day, expect):
    moment = datetime.datetime.fromisoformat(f"{day}T15:00:00")
    spec = _task({"id": "x", "title": "X", "effective": {"loop": "weekday"}})
    assert task_mod.loop_allows(spec, moment) is expect


def test_interval_and_daily_loops_always_allow():
    """循环是「今天响几次」，不是「今天有没有」—— 两种都恒允许。"""
    saturday = datetime.datetime(2026, 10, 10, 15, 0)
    for loop in ("daily", {"kind": "interval", "every": "30m"}):
        spec = _task({"id": "x", "title": "X", "effective": {"loop": loop}})
        assert task_mod.loop_allows(spec, saturday) is True


def test_the_kernel_really_accepts_a_resolved_item():
    """★ 接口对拍：算出来的条目喂**真的** ``compute_display``，它得认出这一条。

    只断言 dict 的键对得上证明不了这件事 —— 那正是「键名写错一个字、浮层恒空」这类
    故障的形状（两边各自都「看着对」）。
    """
    spec = _task({"id": "x", "title": "喝水", "display": {
        "show": "countdown", "reminder": "17:00", "buttons": ["完成"],
    }})
    item = task_mod.resolve_item(spec, _pool(), MOMENT)
    state = scheduler.compute_display([item], datetime.datetime(2026, 10, 10, 17, 0), "medium")
    assert state.show is True
    assert [row.entry_id for row in state.rows] == ["x"]
    assert state.rows[0].title == "喝水"
    assert state.rows[0].actions == (scheduler.ACTION_DONE,)
    assert state.face == scheduler.FACE_COUNTDOWN


def test_a_task_outside_its_window_stays_out_of_the_layer():
    """窗口外 → 浮层里没有它（这条与上面那条是**一对**：一个证明进得去，一个证明出得来）。"""
    spec = _task({"id": "x", "title": "喝水", "display": {
        "show": "row", "reminder": "17:00", "lead_min": 10, "trail_min": 10,
    }})
    item = task_mod.resolve_item(spec, _pool(), MOMENT)
    state = scheduler.compute_display([item], datetime.datetime(2026, 10, 10, 12, 0), "medium")
    assert state.show is False


# ── 接线：真组件 ＋ 真变量池 ────────────────────────────────────────
#
# 上面那一节测的是 ``task.py`` 自己那一层。这一节测**它接进了组件**没有 —— 一条声明
# 从配置到浮层那一行中间有四道手（``items()`` 分派、``_compute_display``、
# ``_sync_interaction``、``_snapshot``），只测模块本身的话，这四道里断了任何一道
# 它都照样全绿。


class _Center:
    """询问中枢替身：只记「交上来了什么、撤过谁、谁答过」。

    **没有 ``on_shown`` 那一栏**（2026-10-11）：任务的「到点弹卡」整条链路退场 ⇒ 组件不再
    给中枢交任何回调，那一栏留着也只会让下一个人以为还有一条「卡登场」的路可挂。
    """
    def __init__(self) -> None:
        self.registered: "list[tuple]" = []
        self.unregistered: "list[str]" = []
        self.answer_keys: "list[str]" = []

    def register(self, spec, on_result=None, on_shown=None) -> None:
        self.registered.append((spec, on_result))

    def unregister(self, plugin_id: str) -> None:
        # 真的撤：注销之后重登，规则表里是新的那一批（组件那边就是靠这一条换规则的）。
        self.unregistered.append(plugin_id)
        self.registered.clear()

    def note_answered(self, key: str) -> None:
        self.answer_keys.append(key)


#: 一条「在浮层上占一行、带一个提醒时刻」的任务。
TASK = {
    "id": "report",
    "title": "提交日报",
    "text": "{off_time} 前提交",
    "display": {
        "show": "row",
        "reminder": {"at": "{off_time}"},
        "trail_min": 30,
        "buttons": ["完成", "稍后"],
    },
}

#: 一条「不显示」的任务：不上浮层，只在主窗体清单里。
PLAIN = {"id": "stretch", "title": "起来动一动"}


class _Now(datetime.datetime):
    """固定「此刻」的 ``datetime``（钉住浮层那一拍的时刻）。"""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 10, 17, 30, 0)


def _At(text: str):
    """造一个「此刻恒为某时刻」的 ``datetime`` 替身（钉住命令那条边沿跨越）。

    边沿判据要**两拍**才看得出来（第一拍只记基线），故它得能一拍一个时刻 —— 那个
    2026-10-10 的固定 ``_Now`` 做不到这件事。
    """
    moment = datetime.datetime.fromisoformat(text)

    class _Fixed(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    return _Fixed


def _plugin(tmp_path, tasks=None, *, pool=None, center=None):
    from barkit.core.config import Config
    from barkit.core.plugin import PluginContext
    from barkit.plugins.todo.plugin import Plugin as TodoPlugin

    ctx = PluginContext(config=Config(user_path=tmp_path / "c.yaml"), events=None)
    if center is not None:
        ctx.interaction = center
    if pool is not None:
        ctx.variables = pool
    plugin = TodoPlugin(ctx)
    if tasks is not None:
        plugin.cfg.set("tasks", [dict(t) for t in tasks])
    return plugin


def test_tasks_take_over_from_the_old_items_table(tmp_path):
    """盘上写了非空的 ``tasks`` ⇒ **整份**走新模型，旧 ``items`` 一个字都不读。"""
    plugin = _plugin(tmp_path, [TASK], pool=_pool(start="08:27"))
    plugin.cfg.set("items", [{"id": "old", "title": "旧条目", "at": "09:00"}])
    assert [i["id"] for i in plugin.items()] == ["report"]


def test_a_non_empty_old_table_is_still_the_authority(tmp_path):
    """旧表非空 ⇒ 旧表说话（哪怕 ``tasks`` 是空的）。

    这是原判据（「``tasks`` 非空才算新模型」）**保留下来的那一半意图**：用户手上真在用的
    那张表不许被 ``tasks`` 抢走 —— 空表当成「配了但一条都没有」会让他的条目整批消失。
    ``start()`` 里的迁移会把这批条目迁成新模型，迁完旧表就被 pop 掉。
    """
    plugin = _plugin(tmp_path, [], pool=_pool())
    plugin.cfg.set("items", [{"id": "old", "title": "旧条目", "at": "09:00"}])
    assert plugin.uses_tasks() is False
    assert [i["id"] for i in plugin.items()] == ["old"]


def test_a_disk_with_no_table_at_all_is_the_new_model(tmp_path):
    """两张表都空 ⇒ **新模型**（全新装出来的盘 / 把任务删光之后的盘）。

    原判据在这里给旧模型，两个后果都是用户看得见的：全新的盘上第一个「＋ 新增」用的是
    旧十三格那张表单（而下一次启动又被迁移换成新模型），以及**把最后一条任务删掉**会让
    同一个按钮换回旧形状 —— 一个按钮两次按出两种表单，只因为中间删空过。
    这两件事都由「没有旧表就是新模型」消掉。
    """
    plugin = _plugin(tmp_path, [], pool=_pool())
    assert plugin.uses_tasks() is True
    assert plugin.items() == []


def test_an_empty_disk_can_still_get_an_old_table(tmp_path):
    """空盘上 ``save_items`` **照旧放行** —— 写完之后旧表非空，判据当场翻成旧模型。

    写口那道闸问的是「**写了会不会被读**」，不是「此刻走哪条路」。照着 ``uses_tasks()``
    的值拒掉的话，空盘上就再也写不出第一张旧表了（而这正是升级路上那些老测试播种的方式，
    也是「用户从更旧的版本升上来、盘上还没有旧表」那一格）。
    """
    plugin = _plugin(tmp_path, [], pool=_pool())
    assert plugin.save_items([{"id": "old", "title": "旧条目", "at": "09:00"}]) is True
    assert plugin.uses_tasks() is False
    assert [i["id"] for i in plugin.items()] == ["old"]


def test_a_task_reaches_the_floating_layer(tmp_path, monkeypatch):
    """★ 端到端：声明一条任务 ⇒ 浮层里真的有它这一行。

    判据落在**这一层**：单测 ``resolve_item`` 只看得到它自己那一段，`items()` 那条
    分派接不上时它照样全绿（而用户看到的是浮层里空空如也）。
    """
    from barkit.plugins.todo import plugin as plugin_mod

    monkeypatch.setattr(plugin_mod, "datetime", _Now)
    plugin = _plugin(tmp_path, [TASK], pool=_pool(start="08:27"))
    plugin.tick()
    assert plugin._display is not None
    assert [row.entry_id for row in plugin._display["rows"]] == ["report"]
    assert plugin._display["rows"][0].text == "17:27 前提交"


def test_a_task_with_no_display_stays_out_of_the_layer(tmp_path, monkeypatch):
    """「不显示」那条**不在**浮层上 —— 与上面那条是一对（一个证明进得去，一个出得来）。"""
    from barkit.plugins.todo import plugin as plugin_mod

    monkeypatch.setattr(plugin_mod, "datetime", _Now)
    plugin = _plugin(tmp_path, [PLAIN], pool=_pool(start="08:27"))
    plugin.tick()
    assert plugin._display is not None
    assert plugin._display["show"] is False


def test_the_panel_counts_undone_tasks(tmp_path):
    """面板摘要走新口径：**未完成 N 项**（不再报「今天完成 X/Y」）。"""
    pool = _pool(start="08:27")
    plugin = _plugin(tmp_path, [TASK, PLAIN], pool=pool)
    # 08:27 ＋ 9h = 17:27；在 15:00 这一刻它还没到点 ⇒ 未完成 2 项、无过期。
    assert plugin._snapshot(MOMENT).summary == "未完成 2 项"
    plugin.set_done("report")
    assert plugin._snapshot(MOMENT).summary == "未完成 1 项"


def test_the_panel_reports_overdue_tasks(tmp_path):
    """过了提醒时刻还没做 ⇒ 「N 项已过期」（§4.6 B：过期优先于未完成）。"""
    pool = _pool(start="08:27")
    plugin = _plugin(tmp_path, [TASK], pool=pool)
    late = datetime.datetime(2026, 10, 10, 18, 0)      # 17:27 已经过了
    assert plugin._snapshot(late).summary == "1 项已过期"


#: 一条「每 30 分钟」的循环任务 —— 顺手带了字面提醒时刻（这正是上一轮那三面三答的组合）。
LOOP_TASK = {
    "id": "water",
    "title": "喝水",
    "effective": {"loop": {"kind": "interval", "every": "30m"}},
    "display": {"show": "row", "reminder": {"at": "09:00"}, "buttons": ["完成", "稍后"]},
}


def test_a_looping_task_is_due_before_its_first_round(tmp_path):
    """循环条**不认字面提醒时刻**（四个判定者里 ``_task_counts`` 是唯一还读它的那个）。

    填了 ``reminder.at: 09:00`` 的循环任务：浮层、摘要、行那一侧都按「每 30 分钟」走，
    只有面板拿 09:00 判过期 —— 摘要写着「每 30 分钟」而状态点是红的。取 08:00 这一刻
    是因为两个算法在这里给出相反的结论（字面说没到点、按完成时刻说今天头一次早该做了）。
    """
    plugin = _plugin(tmp_path, [LOOP_TASK], pool=_pool())
    counted = {r.task_id: r for r in plugin._count_rows(datetime.datetime(2026, 10, 10, 8, 0))}
    assert counted["water"].overdue is True


def test_a_looping_task_stays_reachable_after_the_first_completion(tmp_path):
    """循环条不因「今天勾过完成」被永久收起 —— 它的下一轮还要再来（用户口径）。

    整着留在 ``_hidden_ids`` 里的话，「每 N 分钟」第一次做完就再也不出现，而那正是它
    唯一的意义。退场改由窗口自己判（``_span_of`` 认上一次完成时刻）。
    """
    plugin = _plugin(tmp_path, [LOOP_TASK], pool=_pool())
    assert plugin.set_done("water", at="09:12") is True
    assert "water" in plugin.done_today(), "这一笔照旧进面板那两个数"
    assert "water" not in plugin._hidden_ids(), "但浮层的退场交给窗口自己判"


def test_a_task_that_is_never_done_stays_out_of_the_counts(tmp_path):
    """★ 「不需要完成」的**循环条**（整点报时）：压根不进任何待办判据。

    它比普通那条「只显示」的任务更容易出事：循环的新基准正是**上一次完成那一笔**，而
     ``track_done=False`` 的任务永远写不进那一笔（``set_done`` 拒）⇒ 按零点算。这条
    钉的是这个组合不会因此把计数带歪 —— 已有的
    :func:`test_a_display_only_task_is_not_a_todo` 用的是**不带循环**的那一条，正好把
    这个组合漏在外面。
    """
    chime = {"id": "chime", "title": "整点报时", "track_done": False,
             "effective": {"loop": {"kind": "interval", "every": "60m"}},
             "display": {"show": "clock"}}
    pool = _pool(start="08:27")
    plugin = _plugin(tmp_path, [TASK, chime], pool=pool)
    assert plugin._snapshot(MOMENT).summary == "未完成 1 项", "只有普通那条算数"
    assert plugin.header_counts(MOMENT) == (1, 0)
    # 那一笔根本写不进去 ⇒ 「= 已经没有完成时刻了」这条路径必须练到，而不是绕过去。
    assert plugin.set_done("chime", at="09:12") is False
    late = datetime.datetime(2026, 10, 10, 18, 0)
    counted = {r.task_id: r for r in plugin._count_rows(late)}
    assert counted["chime"].overdue is False, "没过期、也没完成 —— 它压根不是待办"
    assert counted["chime"].done is False


def test_a_display_only_task_is_not_a_todo(tmp_path):
    """★ 「只显示、做不掉」那条（``track_done=False``，如整点时钟）**不进**任何待办判据。

    它照旧列在主窗体清单里、上钟面，但不该让面板挂着「未完成 / 已过期」—— 那是给它记了
    一笔永远还不掉的账。两条对照：① 同页那条普通任务照旧算（不是「筛掉了一切」的退化绿）；
    ② 连过了它的提醒时刻也不报过期（过期那一格也按 ``track_done`` 前置）。
    """
    chime = {"id": "clock", "title": "整点时钟", "track_done": False,
             "display": {"show": "clock"}}
    pool = _pool(start="08:27")
    plugin = _plugin(tmp_path, [TASK, chime], pool=pool)
    assert plugin._snapshot(MOMENT).summary == "未完成 1 项", "只有普通那条算数"
    assert plugin.header_counts(MOMENT) == (1, 0)

    # ② 带一个已过的提醒时刻：只显示那条也不该报「已过期」。
    late_chime = dict(chime, display={"show": "row", "reminder": {"at": "09:00"}})
    only = _plugin(tmp_path, [late_chime], pool=_pool(start="08:27"))
    late = datetime.datetime(2026, 10, 10, 18, 0)
    assert only._snapshot(late).state == "empty", "它过期了也不该报"


def test_a_task_is_out_of_the_panel_when_its_condition_fails(tmp_path):
    """生效规则不过 ⇒ 不算数（§4.6 A′ 的那一半）。"""
    conditional = dict(TASK, effective={
        "conditions": [{"var": "scene", "op": "==", "value": "办公室"}],
    })
    pool = _pool(start="08:27", scene="家里")
    plugin = _plugin(tmp_path, [conditional], pool=pool)
    assert plugin._snapshot(MOMENT).state == "empty"
    assert plugin.items()[0]["enabled"] is False


def test_the_new_model_registers_no_rules_at_all(tmp_path):
    """★ 新模型一条规则都不登 —— 「到点弹卡」整条链路退场（2026-10-11）。

    这是那张卡退场之后**最容易悄悄长回来**的一格：注册规则那一段还在（旧条目表那一支
    仍要走它），于是「顺手把任务也登进去」看起来像补全，实际是把刚拆掉的弹卡链路又装
    回去 —— 而用户那边看到的是「说好了不弹卡，它又弹了」。故判据落在**登记条数**上。

    对照组走旧条目表那一支：防的是「整个登记口被拆了」的退化绿。
    """
    center = _Center()
    plugin = _plugin(tmp_path, [TASK, PLAIN], pool=_pool(start="08:27"), center=center)
    plugin._sync_interaction()
    assert center.registered == []

    # 对照组走旧条目表那一支：防的是「整个登记口被拆了」的退化绿。**另起一个盘** ——
    # 同一个盘上 `tasks` 已经写进去了，判据会当场翻成新模型，对照就落空了。
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    old = _plugin(legacy, None, pool=_pool(), center=center)
    old.cfg.set("items", [{"id": "td1", "title": "喝水", "at": "09:30"}])
    assert old.uses_tasks() is False
    old._sync_interaction()
    assert [spec.prompt.key for spec, _ in center.registered] == ["todo.item.td1"]


def test_a_command_still_runs_for_a_task_that_needs_no_completion(tmp_path, monkeypatch):
    """★ 「不需要完成」的任务（``track_done=False``）**照旧跑命令**。

    这条是原先那条「到点那张卡上仍留着命令」的直译：不管那一档怎么改，「不需要完成」
    说的都是「做不掉」（不进计数、不画状态点），不是「到点别做事」。把命令一并收掉的
    改法最省事，代价是这类任务（整点报时那种）的命令永远不跑 —— 且没有任何线索。

    判据落在**真的跨过了那一刻**上：先喂一拍基线（``_cmd_last_m`` 的记法），再喂到点
    的那一拍。
    """
    from barkit.plugins.todo import command as command_mod
    from barkit.plugins.todo import plugin as plugin_mod

    fired: "list[str]" = []
    monkeypatch.setattr(command_mod, "fire", lambda text: fired.append(text) or True)
    chime = dict(
        TASK, id="chime", track_done=False,
        display={"show": "row", "reminder": {"at": "09:00"}},
        command="open https://example.com",
    )
    plugin = _plugin(tmp_path, [chime], pool=_pool(start="08:27"))
    monkeypatch.setattr(plugin_mod, "datetime", _At("2026-10-10T08:59:00"))
    plugin.tick()
    assert fired == [], "到点之前一拍都不该跑"
    monkeypatch.setattr(plugin_mod, "datetime", _At("2026-10-10T09:00:30"))
    plugin.tick()
    assert fired == ["open https://example.com"]


def test_the_new_model_writes_nothing_into_the_dedupe_ledger(tmp_path):
    """★ 新模型不往去重账里写任何键 —— 它一条规则都没登，写了也没人读。

    两个具体代价：``interaction_state.json`` 会随每条任务常年长下去；以及 id 被复用
    （删掉 td3 再建一个 td3）时新任务会继承旧任务那本**没人读的**账 —— 今天看起来没事，
    等哪一天弹卡那条链路回来，症状就是「新建的那条当天不弹」。

    对照组走旧条目表那一支：防的是「整条 ``note_answered`` 被删了」的退化绿。
    """
    center = _Center()
    plugin = _plugin(tmp_path, [TASK], pool=_pool(start="08:27"), center=center)
    assert plugin.set_done("report") is True
    assert center.answer_keys == []

    # 对照组走旧条目表那一支（**另起一个盘**，理由同上面那条）：防的是「整条
    # ``note_answered`` 被删了」的退化绿。
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    old = _plugin(legacy, None, pool=_pool(), center=center)
    old.cfg.set("items", [{"id": "td1", "title": "喝水", "at": "09:30"}])
    assert old.uses_tasks() is False
    assert old.set_done("td1") is True
    assert center.answer_keys == ["todo.item.td1"]


def test_the_command_runs_at_the_moment_it_crosses(tmp_path, monkeypatch):
    """命令在**跨过那一刻**跑（边沿），不是在「当下正好等于那一刻」时跑。

    判据分两拍：第一拍只记基线（重启后不补跑那一条），第二拍跨过去才跑。两个方向都钉住 ——
    只钉「跑了」的话，一个「每拍都跑」的实现也会绿。
    """
    from barkit.plugins.todo import command as command_mod
    from barkit.plugins.todo import plugin as plugin_mod

    fired: "list[str]" = []
    monkeypatch.setattr(command_mod, "fire", lambda text: fired.append(text) or True)
    plugin = _plugin(
        tmp_path,
        [dict(TASK, command="open https://example.com",
              display={"show": "row", "reminder": {"at": "09:00"}})],
        pool=_pool(start="08:27"),
    )

    monkeypatch.setattr(plugin_mod, "datetime", _At("2026-10-10T08:59:30"))
    plugin.tick()
    assert fired == [], "第一拍只记基线 —— 到点之前不许跑，重启也不补跑过去的那一刻"

    monkeypatch.setattr(plugin_mod, "datetime", _At("2026-10-10T09:00:20"))
    plugin.tick()
    assert fired == ["open https://example.com"]

    fired.clear()
    monkeypatch.setattr(plugin_mod, "datetime", _At("2026-10-10T09:01:20"))
    plugin.tick()
    assert fired == [], "跨过去之后不许每拍再跑一遍"


def test_a_slept_through_moment_is_still_caught_up(tmp_path, monkeypatch):
    """睡过头（一拍跨了两分钟）照跑：只要那一刻还没被跨过去，醒来后第一拍补上。"""
    from barkit.plugins.todo import command as command_mod
    from barkit.plugins.todo import plugin as plugin_mod

    fired: "list[str]" = []
    monkeypatch.setattr(command_mod, "fire", lambda text: fired.append(text) or True)
    plugin = _plugin(
        tmp_path,
        [dict(TASK, command="open https://example.com",
              display={"show": "row", "reminder": {"at": "09:00"}})],
        pool=_pool(start="08:27"),
    )
    monkeypatch.setattr(plugin_mod, "datetime", _At("2026-10-10T08:58:00"))
    plugin.tick()
    monkeypatch.setattr(plugin_mod, "datetime", _At("2026-10-10T09:03:00"))
    plugin.tick()
    assert fired == ["open https://example.com"]


def test_the_other_two_moments_shift_the_command_off_the_reminder(tmp_path, monkeypatch):
    """``lead`` / ``trail`` 把命令挪到窗口的两端（``at ∓ lead/trail``）。"""
    from barkit.plugins.todo import command as command_mod
    from barkit.plugins.todo import plugin as plugin_mod

    fired: "list[str]" = []
    monkeypatch.setattr(command_mod, "fire", lambda text: fired.append(text) or True)

    def _run(cmd_at: str, before: str, after: str) -> "list[str]":
        fired.clear()
        plugin = _plugin(
            tmp_path,
            [dict(TASK, command="open https://example.com", command_at=cmd_at, display={
                "show": "row", "reminder": {"at": "09:00"},
                "lead_min": 30, "trail_min": 45,
            })],
            pool=_pool(start="08:27"),
        )
        monkeypatch.setattr(plugin_mod, "datetime", _At(before))
        plugin.tick()
        monkeypatch.setattr(plugin_mod, "datetime", _At(after))
        plugin.tick()
        return list(fired)

    assert _run(task_mod.COMMAND_AT_LEAD, "2026-10-10T08:29:00",
                "2026-10-10T08:30:10") == ["open https://example.com"]
    assert _run(task_mod.COMMAND_AT_TRAIL, "2026-10-10T09:44:00",
                "2026-10-10T09:45:10") == ["open https://example.com"]
    # 「提醒时间」那一档在这两个时刻都**不该**跑（它认得的是 09:00）。
    assert _run(task_mod.COMMAND_AT_REMINDER, "2026-10-10T08:29:00",
                "2026-10-10T08:30:10") == []


def test_a_task_that_is_not_in_effect_does_not_run_its_command(tmp_path, monkeypatch):
    """★ 这一刻**不生效**的任务不跑命令 —— 停用 / 条件不满足 / 不在时段内三样都算数。

    这道门原先由中枢那张卡站着（条件不成立卡不弹 ⇒ 命令不跑）；卡片退场之后得有人替它
    站。漏了它的症状很实在：一条「只在办公室」的任务，你在家它照样把那个 webhook 打出去。
    对照组（条件成立的那一份照跑）防的是「把所有命令都掐了」的退化绿。
    """
    from barkit.plugins.todo import command as command_mod
    from barkit.plugins.todo import plugin as plugin_mod

    fired: "list[str]" = []
    monkeypatch.setattr(command_mod, "fire", lambda text: fired.append(text) or True)
    body = dict(TASK, command="open https://example.com", effective={
        "conditions": [{"var": "scene", "op": "==", "value": "办公室"}],
    })

    home = _plugin(tmp_path, [body], pool=_pool(start="08:27", scene="家里"))
    monkeypatch.setattr(plugin_mod, "datetime", _At("2026-10-10T08:59:00"))
    home.tick()
    monkeypatch.setattr(plugin_mod, "datetime", _At("2026-10-10T17:28:00"))
    home.tick()
    assert fired == [], "条件不成立的那一刻不该跑"

    office = _plugin(tmp_path, [body], pool=_pool(start="08:27", scene="办公室"))
    monkeypatch.setattr(plugin_mod, "datetime", _At("2026-10-10T17:26:00"))
    office.tick()
    monkeypatch.setattr(plugin_mod, "datetime", _At("2026-10-10T17:27:10"))
    office.tick()
    assert fired == ["open https://example.com"], "条件成立的那一份照跑"


def test_the_command_runs_on_done_only_for_that_choice(tmp_path, monkeypatch):
    """``command_at = done`` 才在点「完成」时跑；其余三档**永远不在那一刻跑**。

    撤销那一档（写空串）**永远不跑**：撤销不是做事，是「我刚才记错了」—— 为它跑一条命令
    不但多余，而且不可逆。
    """
    from barkit.plugins.todo import command as command_mod

    fired: "list[str]" = []
    monkeypatch.setattr(command_mod, "fire", lambda text: fired.append(text) or True)

    plain = _plugin(tmp_path, [dict(TASK, command="open https://example.com")],
                    pool=_pool(start="08:27"))
    assert plain.set_done("report") is True
    assert fired == [], "默认那一档是「提醒时间」，点完成不该跑"

    done = _plugin(tmp_path,
                   [dict(TASK, command="open https://example.com",
                         command_at=task_mod.COMMAND_AT_DONE)],
                   pool=_pool(start="08:27"))
    assert done.set_done("report") is True
    assert fired == ["open https://example.com"]

    fired.clear()
    assert done.set_done("report", "") is True, "撤销本身要成功"
    assert fired == [], "撤销不该跑命令"


def test_the_command_is_interpolated_before_it_is_cut(tmp_path, monkeypatch):
    """命令里的 ``{变量}`` 先插值、`再切` —— 与 ``text`` 那一格同一个口。

    不插值的症状是「URL 里那个时刻永远停在我当初写下的那一个」，而界面上没有任何线索
    （用户会以为变量坏了）。
    """
    from barkit.plugins.todo import command as command_mod
    from barkit.plugins.todo import plugin as plugin_mod

    fired: "list[str]" = []
    monkeypatch.setattr(command_mod, "fire", lambda text: fired.append(text) or True)
    plugin = _plugin(
        tmp_path,
        [dict(TASK, command="open https://x/?t={start_time}",
              display={"show": "row", "reminder": {"at": "09:00"}})],
        pool=_pool(start="08:27"),
    )
    monkeypatch.setattr(plugin_mod, "datetime", _At("2026-10-10T08:59:00"))
    plugin.tick()
    monkeypatch.setattr(plugin_mod, "datetime", _At("2026-10-10T09:00:10"))
    plugin.tick()
    assert fired == ["open https://x/?t=08:27"]


def test_a_task_that_is_not_effective_again_starts_its_baseline_over(tmp_path, monkeypatch):
    """「这一刻不生效」会把基线清掉 ⇒ 重新生效那一刻从头数，不补做那一段里错过的点。

    与「错过不补」是同一条取舍的两面：机器关着错过的那一刻不补，**不生效**错过的那一刻
    同样不补 —— 那一段里它本来就不算数。
    """
    from barkit.plugins.todo import command as command_mod
    from barkit.plugins.todo import plugin as plugin_mod

    fired: "list[str]" = []
    monkeypatch.setattr(command_mod, "fire", lambda text: fired.append(text) or True)
    body = dict(TASK, command="open https://example.com", effective={
        "conditions": [{"var": "scene", "op": "==", "value": "办公室"}],
    })
    pool = _pool(start="08:27", scene="家里")
    plugin = _plugin(tmp_path, [body], pool=pool)

    monkeypatch.setattr(plugin_mod, "datetime", _At("2026-10-10T16:00:00"))
    plugin.tick()                                    # 不生效：基线不记
    monkeypatch.setattr(plugin_mod, "datetime", _At("2026-10-10T17:28:00"))
    plugin.tick()                                    # 仍旧不生效
    assert fired == []
    pool.on_status(ROUTE_PROFILE_TOPIC, _FakeReport(summary="办公室"))
    monkeypatch.setattr(plugin_mod, "datetime", _At("2026-10-10T17:29:00"))
    plugin.tick()                                    # 刚生效 ⇒ 只记基线
    assert fired == [], "刚生效那一拍只记基线，不补跑刚过去的那一刻"


def test_a_missing_pool_says_so_instead_of_silently_emptying(tmp_path, caplog):
    """配了新模型却没接变量池 ⇒ 空表 ＋ **一条日志**（一拍一次会把日志刷满，故只报第一拍）。"""
    plugin = _plugin(tmp_path, [TASK])
    with caplog.at_level("WARNING", logger="barkit.plugins.todo.plugin"):
        assert plugin.items() == []
        plugin.items()
    assert len([r for r in caplog.records if "变量池未接线" in r.message]) == 1


def test_a_condition_that_cannot_be_judged_speaks_on_the_flip_only(tmp_path, caplog):
    """那条「判不出来」的 INFO 按**判决翻转**出声，不是每拍一条。

    周期型组件每拍都重判条件，而「判不出来」常是**长期**状态（引用的变量整天没值）——
    一拍一条在真机上就是刷屏。三次连续调用只出一条（同一拍里 ``items()`` 本来就要走两遍：
    ``done_today`` 与 ``_compute_display`` 各一次），**且条件好了再坏要重新说**：
    只说第一遍不叫「翻转」，而「恢复可判之后又坏了」正是用户唯一需要再看一次的时机。
    """
    spec = {
        "id": "clk2",
        "title": "X",
        "effective": {"conditions": [{"var": "scene", "op": "==", "value": "办公室"}]},
    }
    pool = _pool(scene=None)                      # 依赖的变量当刻没值
    plugin = _plugin(tmp_path, [spec], pool=pool)

    def blank_lines() -> int:
        return len([r for r in caplog.records if "判不出来" in r.message])

    with caplog.at_level("INFO", logger="barkit.plugins.todo.task"):
        assert [i["enabled"] for i in plugin.items()] == [False]
        plugin.items()
        plugin.items()
        assert blank_lines() == 1

        caplog.clear()
        pool.on_status(ROUTE_PROFILE_TOPIC, _FakeReport(summary="办公室"))
        assert [i["enabled"] for i in plugin.items()] == [True], "条件恢复可判"
        assert blank_lines() == 0

        caplog.clear()
        pool.on_status(ROUTE_PROFILE_TOPIC, _FakeReport(summary=""))   # 空串 = 无值
        assert [i["enabled"] for i in plugin.items()] == [False]
        assert blank_lines() == 1, "又判不出来了 —— 只报第一遍不算「判决翻转」"


def test_a_scene_change_asks_for_a_tick_and_a_repeat_does_not(tmp_path):
    """场景**变了**才点火。两半都要，缺哪半都能造成一种错。

    本组件一拍 60s（2026-10-11 由 1s 改）：场景变了却不点火，浮层最多 60 秒才跟着翻 ——
    而「只在某场景」这条门禁恰恰是用户换环境那一刻期待立刻看见的。反过来，
    ``route_guard`` 每拍都发一份场景广播，**无条件**点火会把节拍又拖回 30 秒一次。
    """
    plugin = _plugin(tmp_path, [TASK], pool=_pool(start="08:27"))
    fired: "list[str]" = []
    plugin.ctx.mark_dirty = lambda pid, reason="": fired.append(reason)

    plugin.on_system_event(ROUTE_PROFILE_TOPIC, _FakeReport(summary="办公室"))
    assert fired == ["scene"], "换场景必须点火"
    plugin.on_system_event(ROUTE_PROFILE_TOPIC, _FakeReport(summary="办公室"))
    assert fired == ["scene"], "同一个场景反复广播不该反复点火"
    plugin.on_system_event(ROUTE_PROFILE_TOPIC, _FakeReport(summary="家里"))
    assert fired == ["scene", "scene"]


def test_a_task_that_never_prompts_can_still_be_done(tmp_path):
    """新模型下 agenda_log 只剩「完成」一种语义 ⇒ 「只列不弹」的任务也记得住完成。"""
    pool = _pool(start="08:27")
    plugin = _plugin(tmp_path, [PLAIN], pool=pool)
    assert plugin.set_done("stretch") is True
    assert plugin.done_at("stretch") != ""
    assert plugin._snapshot(MOMENT).state == "done"


# ── 任务表的写口与预览口（Phase 5） ──────────────────────────────────


def test_save_tasks_writes_the_task_table(tmp_path):
    """写进 ``tasks`` 段 ⇒ 组件当场就是新模型（判据读的是盘上那份，不是内存标记）。"""
    plugin = _plugin(tmp_path, pool=_pool())
    assert plugin.uses_tasks() is True
    assert plugin.save_tasks([dict(TASK)]) is True
    assert [t["id"] for t in plugin.tasks()] == ["report"]
    assert [i["id"] for i in plugin.items()] == ["report"]


def test_save_tasks_is_refused_while_the_old_table_is_authoritative(tmp_path):
    """旧表非空 ⇒ 写 ``tasks`` 一个字都不会被读 ⇒ 拒掉（而不是让它「看起来保存了」）。"""
    plugin = _plugin(tmp_path, pool=_pool())
    assert plugin.save_items([{"id": "old", "title": "旧条目", "at": "09:00"}]) is True
    assert plugin.save_tasks([dict(TASK)]) is False
    assert plugin.tasks() == []
    assert [i["id"] for i in plugin.items()] == ["old"]


def test_deleting_the_last_task_does_not_flip_the_model(tmp_path):
    """把任务删光之后仍在**新模型** —— 否则「＋ 新增」那个按钮会换一套表单。

    两段都空在盘上同形（``cfg.set`` 等于默认值就删键），故这一格只能靠「**没有旧表**
    就是新模型」来钉；否则「删除最后一条任务」会让编辑屏当场换回旧十三格。
    """
    plugin = _plugin(tmp_path, pool=_pool())
    assert plugin.save_tasks([dict(TASK)]) is True
    assert plugin.save_tasks([]) is True
    assert plugin.uses_tasks() is True
    assert plugin.items() == []


def test_declared_variables_are_read_from_the_config(tmp_path):
    """编辑屏那两个「引用变量」下拉读的是顶层**声明**（取声明不取值）。

    出厂那份也在里面（``config.default.yaml`` 声明了场景变量）—— 这正是「用户自己一条都
    没配也引用得到 ``{scene}``」那一格；而 ``variables:`` 段是**整段覆盖**的，写进去一份
    就是全部（与 ``plugin_order`` 同款）。
    """
    plugin = _plugin(tmp_path, pool=_pool())
    assert [d["id"] for d in plugin.declared_variables()] == ["scene"]
    plugin.ctx.config.set_variables([
        {"id": "off_time", "name": "下班时间", "source": "derived", "type": "time",
         "expr": "start_time + 540m"},
    ])
    assert [d["id"] for d in plugin.declared_variables()] == ["off_time"]


class _FakeController:
    """只认 ``on_display()`` 的浮层控制器替身（预览那一路只叫它这一口，见 ``_refresh_now``）。"""

    def __init__(self):
        self.shown = 0

    def on_display(self):
        self.shown += 1


def test_preview_task_resolves_before_handing_it_to_the_layer(tmp_path):
    """预览前必须 ``resolve_item``：交给浮层的是**内核形状**（``at`` ＝ 17:27）不是声明。

    不换算的话交出去的是那份 ``effective`` / ``display`` 声明 —— 浮层认不出它（恒不显示），
    而用户在界面上看到的是「点了预览没反应」。故判据落在 ``at`` 上：那个键只有换算过才有。
    """
    plugin = _plugin(tmp_path, [TASK], pool=_pool(start="08:27"))
    plugin._controller = _FakeController()
    assert plugin.preview_task(dict(TASK)) is None
    assert plugin._test is not None
    assert plugin._test["at"] == "17:27", "声明里的 {off_time} 得先被求值"
    assert plugin._test["title"] == "提交日报"
    assert plugin._controller.shown == 1


def test_preview_task_says_what_is_missing(tmp_path):
    """算不出 / 没接线各自说一句**事实**（不是一句「失败了」）—— 与 ``preview_entry`` 同口径。"""
    plugin = _plugin(tmp_path, pool=_pool())
    assert "必须写 title" in plugin.preview_task({"id": "x"}), "过不了声明层就说清是为什么"
    assert "变量池" in _plugin(tmp_path, pool=None).preview_task(dict(TASK))
