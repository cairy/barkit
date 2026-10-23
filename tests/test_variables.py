"""core/variables 测试（变量池与任务解耦 · Phase 1）。

对应设计文档 ``内部计划（变量池架构）`` 的 §2（变量池模型）、
§3（表达式引擎）、§2.5（ask 的按日运行值）。分节与包内模块一一对应：``expr`` /
``spec`` / ``rules`` / ``store`` / ``pool``，最后一节是配置段的接入。

**不自己设数据根**：``tests/conftest.py`` 的 ``_isolate_barkit_data_dir`` 每个用例都把
``barkit.paths.APP_SUPPORT`` 指到 per-test tmp，而 ``store.path()`` 是**调用期**取根
（不是 import 期常量）。故本文件里不要再补 ``store.path`` 之类的补丁 —— 多一份补丁
就多一处会失效的隔离，而失效的症状是往真实用户目录里写文件。
"""

from __future__ import annotations

import datetime
import json
from dataclasses import fields as dataclass_fields

import pytest
import yaml

from barkit.core import variables as V
from barkit.core.variables import expr, rules, spec as spec_mod, store

TODAY = "2026-10-10"


# ── 测试替身 ────────────────────────────────────────────────────────


class _FakeClock:
    """可控时钟：显式改 ``moment``，并数**被问了几次**（"一轮一个世界"那条判据要它）。"""

    def __init__(self, moment: datetime.datetime) -> None:
        self.moment = moment
        self.calls = 0

    def __call__(self) -> datetime.datetime:
        self.calls += 1
        return self.moment


class _FakeReport:
    """替身状态快照：只带变量池要取的两个字段（与 ``StateReport`` 同形的那部分）。"""

    def __init__(self, state: str = "", summary: str = "") -> None:
        self.state = state
        self.summary = summary


def _decls() -> list:
    """一份覆盖四类来源的声明：场景（组件）→ 上班时间（ask）→ 两个派生。"""
    return [
        {"id": "scene", "name": "当前场景", "source": "component",
         "component": "route_guard", "key": "profile", "field": "summary"},
        {"id": "start_time", "name": "上班时间", "source": "ask", "type": "time",
         "trigger": {"when": ["daily"],
                     "conditions": [{"var": "scene", "op": "==", "value": "办公室"}]},
         "prompt": "今天几点上班？"},
        {"id": "work_span", "source": "derived", "expr": "9h"},
        {"id": "off_time", "name": "下班时间", "source": "derived",
         "expr": "start_time + work_span"},
    ]


def _pool(decls=None, moment=None):
    """建池 → 加载 → 开一轮。返回 ``(池, 时钟)``。"""
    clock = _FakeClock(moment or datetime.datetime(2026, 10, 10, 8, 27, 0))
    pool = V.VariablePool(clock=clock)
    pool.load(_decls() if decls is None else decls)
    pool.begin_pass()
    return pool, clock


def _scene_report(pool, summary: str) -> None:
    """喂一份「场景」快照（topic 由声明折出来，不手写字符串）。"""
    pool.on_status(V.status_topic_of("route_guard", "profile"), _FakeReport(summary=summary))


# ══ 表达式引擎（expr） ══════════════════════════════════════════════


def test_time_arithmetic_follows_the_one_type_table():
    """§3.2 的类型规则表：时间±时长→时间、时间−时间→时长、时长±时长→时长。"""
    values = {
        "start": expr.Value(expr.T_TIME, 9 * 3600),
        "span": expr.Value(expr.T_DURATION, 2 * 3600),
    }
    assert expr.evaluate("start + span", values.get).raw == 11 * 3600
    assert expr.evaluate("start - span", values.get).raw == 7 * 3600
    assert expr.evaluate("start - start", values.get) == expr.Value(expr.T_DURATION, 0)
    assert expr.evaluate("span + span", values.get).raw == 4 * 3600


def test_multiplication_parses_but_is_rejected_as_a_type_error():
    """§3.3 的文法里有 ``*`` / ``/``，§3.2 的类型规则里没有 —— **以类型规则为准**。

    报的是**类型**错（点名「v1 不支持」），不是「括号写错了」：直接删掉语法会让人以为
    是自己括错了地方。
    """
    said = expr.validate("2 * 3", lambda _n: None)
    assert said and "不支持" in said
    assert expr.evaluate_or_none("2 * 3", lambda _n: None) is None


@pytest.mark.parametrize(
    "text",
    [
        "a.b",                # 属性访问 —— 文法里没有这个形状
        "now()",              # 函数调用
        "__import__('os')",   # 导入
        "a = 1",              # 赋值
        "a; b",               # 语句
        "a and b",            # 逻辑词
        "'a'",                # 字符串只认双引号
        "-30m",               # 一元负号不在文法里
        "9 h",                # 时长不许写空格（会与「数字 9 + 变量 h」撞车）
        "1 +",
        "(1 + 2",
        "1.2.3",
        '"unclosed',
        "25:70",              # 非法时刻不得静默变成 00:00
        "1 @ 2",
        "",
    ],
)
def test_hostile_expression_shapes_are_all_rejected(text):
    """**注入面**：表达式是用户数据（``config.yaml``），能写表达式就等于能写代码。

    这一条按**行为**守，不是扫源码找 ``eval`` 字样：上面每一种形状都必须变成一句中文
    错误，而不是一个值。拦它们的是文法的**缺席**，不是黑名单 —— 故「合规不误报」那一半
    由 :func:`test_the_editor_and_the_engine_share_one_judgement` 里那批正常表达式守。
    """
    assert expr.validate(text, lambda _n: None)
    with pytest.raises(expr.ExprError):
        expr.evaluate(text, lambda _n: None)


def test_the_engine_reads_a_wider_set_of_time_literals_than_the_editor():
    """**读侧必须比校验侧宽**（严 ⊂ 宽）。窄的方向（引擎比编辑器严）的症状是
    **存得进去、算不出来**：设置页里那个变量看着是好的，运行期恒「未定义」，且不报错。"""
    nine_thirty = 9 * 3600 + 30 * 60
    assert expr.time_to_seconds("9:30") == nine_thirty          # 单位数小时
    assert expr.time_to_seconds("09:30") == nine_thirty
    assert expr.time_to_seconds("09:30:15") == nine_thirty + 15  # 带秒
    assert expr.time_to_seconds(" 09:30 ") == nine_thirty        # 两头空格
    assert expr.time_to_seconds("25:70") is None
    assert expr.time_to_seconds(None) is None
    assert expr.evaluate("9:30 + 30m", lambda _n: None).raw == 10 * 3600


def test_time_crossing_midnight_is_not_wrapped_arithmetically():
    """夜班：17:45 ＋ 9h 是**次日 02:45**。算术不回卷（回卷＝把「加了 9 小时」丢掉），
    只有**显示**那一层按一天的秒数取模。"""
    got = expr.evaluate("17:45 + 9h", lambda _n: None)
    assert got.raw == 26 * 3600 + 45 * 60
    assert got.render() == "02:45"
    assert expr.DAY_S == 86_400


def test_time_display_stops_at_minutes_but_the_value_keeps_seconds():
    """显示到分、值到秒 —— **显示不是存储**。反过来（拿显示当存储）会让「17:45:30」
    在界面上过一趟就变成「17:45」。"""
    got = expr.coerce("17:45:30", expr.T_TIME)
    assert got is not None and got.raw == 17 * 3600 + 45 * 60 + 30
    assert got.render() == "17:45"


def test_duration_rendering_keeps_the_sign_and_drops_a_zero_minute_part():
    assert expr.fmt_duration(5400) == "1小时30分"
    assert expr.fmt_duration(7200) == "2小时"
    assert expr.fmt_duration(1800) == "30分"
    assert expr.fmt_duration(-1800) == "-30分"      # 负时长看得见（不写成「30分」）
    assert expr.fmt_duration(0) == "0分"


def test_referenced_variables_walks_the_whole_tree():
    """依赖图（拓扑排序 / 环检 / 传播）共用这一处遍历；漏掉 ``bin`` 那一层递归的症状
    正是**环检不出来**。"""
    assert expr.referenced_variables("a + (b + c) - a") == ("a", "b", "c")
    assert expr.referenced_variables("9h") == ()
    with pytest.raises(expr.ExprError):
        expr.referenced_variables("a +")


def test_text_variables_reads_braces_and_keeps_the_inner_name():
    """``{变量}`` 那条长相的**唯一**实现（插值也是它）。

    这里钉的是「交出来的是内层那个名字」：带上花括号（``"{a}"``）的版本不报任何错，只会
    让下游 ``spec_of`` 查不到 —— 界面上写「``{a}`` 未定义」，用户看不出是谁写错了。
    """
    assert V.text_variables("{off_time} 前提交") == ("off_time",)
    assert V.text_variables("{a}{b} 和 {a}") == ("a", "b"), "去重、按出现顺序"
    assert V.text_variables("没有占位符") == ()
    assert V.text_variables("") == ()
    assert V.text_variables(None) == ()
    # 内核自己那几个中文占位符不是变量（它们不在这条长相里）。
    assert V.text_variables("{到点} 该走了（现在 {现在}）") == ()


def test_interpolate_and_text_variables_agree_on_the_same_shape():
    """**一对**：「插值换掉的那些名字」必须正好是「扫出来的那些」。

    两处各写一条正则时，漂开的那一刻不会报错 —— 症状是「变量条里列着它、插值却不换它」，
    读起来像两个不相干的 bug。这一条把它们焊在一起。
    """
    text = "{a} 到 {b}，{到点} 提醒（{a} 已经算过了）"
    names = V.text_variables(text)
    assert names == ("a", "b")

    p = V.VariablePool(clock=lambda: datetime.datetime(2026, 10, 10, 9, 0))
    p.load([
        {"id": "a", "name": "甲", "source": "derived", "expr": "9:00"},
        {"id": "b", "name": "乙", "source": "derived", "expr": "10:00"},
    ])
    p.begin_pass()
    swapped = p.interpolate(text)
    assert "{" not in swapped.replace("{到点}", ""), f"还有没换掉的变量：{swapped}"
    assert "{到点}" in swapped, "内核的占位符得原样留着（它由渲染那一刻填）"
    for name in names:
        assert "{" + name + "}" not in swapped, f"{name} 被扫出来了却没被换掉：{swapped}"


def test_a_time_gap_is_judged_by_the_engine_not_by_a_shape_rule():
    """「这一格能不能算成一个时刻」只有一份判据：**表达式引擎**那一份。

    §3.2 的类型表本来就认 ``时间 ± 时长``（``{off_time}-30m`` / ``18:00+30m``），而界面
    从前自己写了一条只认 ``HH:MM`` 与整串 ``{名字}`` 的形状正则 ⇒ 内核算得出来的写法被
    白挡，且界面上没有一句话说得出为什么（它只会重复「要填 HH:MM 或 {变量名}」，而用户
    写的正是那两样之一）。这里钉住：那种写法**读得出来**，而真读不出来的几种各自有一句
    点名原因的话 —— 笼统一句「格式不对」等于把用户丢回原地。
    """
    pool, _clock = _pool()
    assert pool.set_ask_value("start_time", "08:27")       # ⇒ off_time = 17:27
    pool.begin_pass()

    for ok in ("09:30", "9:30", "{off_time}", "{off_time}-30m", "{off_time} - 30m",
               "18:00+30m", "{off_time}+4h", "", "   "):
        assert pool.time_ref_problem(ok) is None, ok

    # 用户那次的原话：``{off_time}-20`` —— 少了单位 ⇒ 引擎的规则表里「时间 − 数字」没有定义。
    missed_unit = pool.time_ref_problem("{off_time}-20")
    assert "没有定义" in missed_unit, missed_unit
    # 只有一个数字：读得出来，但不是个时刻。
    assert "不是时刻" in pool.time_ref_problem("20")
    # 引用了不存在的变量：点名那个名字（写错一个字是最常见的一种）。
    assert "nope" in pool.time_ref_problem("{nope}")


def test_a_time_gap_with_no_value_yet_is_still_readable():
    """当刻没有值的变量按**声明类型**判 —— 值的有无是运行期的事。

    ``{off_time}`` 由 ``start_time`` 算出来，而后者要「今天在办公室」才问 ⇒ 不在办公室
    那天它当刻没有值。按值判的话这一格会是「读不出来」，用户连改都改不了 —— 而那份配置
    一点错都没有。
    """
    pool, _clock = _pool()
    assert "{" in pool.interpolate("{off_time}"), "前提：当刻确实取不到值"
    assert pool.type_of("off_time") == expr.T_TIME, "前提：它声明出来就是个时间型"
    assert pool.time_ref_problem("{off_time}") is None
    assert pool.time_ref_problem("{off_time}-30m") is None


def test_static_type_is_inferred_without_running_the_expression():
    """§2.4：派生表达式的类型**由运算规则推出**（不取值）。"""
    types = {"start": expr.T_TIME, "span": expr.T_DURATION}
    assert expr.result_type("start + span", types.get) == expr.T_TIME
    assert expr.result_type("start - start", types.get) == expr.T_DURATION
    assert expr.result_type("9h", lambda _n: None) == expr.T_DURATION
    assert expr.result_type('"办公室"', lambda _n: None) == expr.T_STRING
    with pytest.raises(expr.ExprError):
        expr.result_type('1 + "a"', lambda _n: None)      # 无隐式类型转换
    with pytest.raises(expr.ExprError):
        expr.result_type("缺席的变量 + 1h", lambda _n: None)


def test_the_editor_and_the_engine_share_one_judgement():
    """**自证**：同一个判定不许有第二份实现。

    设置页走 ``validate``（静态类型检查那条走法），求值走 ``evaluate``（算值那条走法）
    —— 两条走法各有一份 AST 遍历。谁把运算符加进其中一份而忘了另一份，这条就红
    （症状会是「设置页存得进去、运行期求不出值」）。"""
    catalogue = ["1h", "9:30 + 30m", "17:45 + 9h", "2 * 3", "缺席的变量 + 1h", "1 +", ""]
    for text in catalogue:
        said_ok = expr.validate(text, lambda _name: None) is None
        got_a_value = expr.evaluate_or_none(text, lambda _name: None) is not None
        assert said_ok == got_a_value, text


# ── 裸值折算（「严格 ⊂ 宽松」那条方向的落点） ─────────────────────


#: 「盘上/配置里能写出来的形态」→ 折算结果。**每种类型至少一个**，形态取自本仓真在用的
#: 那几种写法（``30m`` / ``9h`` 是时长，``09:00`` 既是时刻也是上一版配置里「9 小时」的写法，
#: ``true`` / ``否`` 是按日运行值文件里那种「什么都存成字符串」的样子）。
_STORED_FORMS = [
    (expr.T_TIME, "09:00", 9 * 3600),
    (expr.T_TIME, "17:45:30", 17 * 3600 + 45 * 60 + 30),   # 到秒（显示只到分，见下一条）
    (expr.T_TIME, " 9:30 ", 9 * 3600 + 30 * 60),           # 两头空格 + 单位数小时
    (expr.T_DURATION, "30m", 1800),
    (expr.T_DURATION, "9h", 9 * 3600),
    (expr.T_DURATION, "5400", 5400),                       # 裸数字 = 秒
    (expr.T_DURATION, "09:00", 9 * 3600),                  # 上一版配置里的「9 小时」
    (expr.T_NUMBER, "3", 3),
    (expr.T_WEEKDAY, "5", 5),
    (expr.T_STRING, "办公室", "办公室"),
    (expr.T_ENUM, "办公室", "办公室"),
    (expr.T_BOOL, "true", True),
    (expr.T_BOOL, "否", False),
    (expr.T_DATE, TODAY, TODAY),
]


@pytest.mark.parametrize("type_name,raw,expected", _STORED_FORMS)
def test_every_stored_form_reads_back_to_the_same_value(type_name, raw, expected):
    """按日运行值文件里什么都存成字符串（``json`` 里就是一个 ``str``），故**写侧写得出的
    每一个形态读侧都要读得回来**。读得比写得窄的症状是「今天填了值、重启后这个变量恒
    未定义」，而文件里明明有那个值。"""
    got = expr.coerce(raw, type_name)
    assert got is not None, f"{type_name} 读不回 {raw!r}"
    assert got.raw == expected


def test_display_text_is_not_a_storage_form():
    """显示的那一份**不是**存储的那一份：``30分`` / ``1小时30分`` 只是给人看的中间形态，
    ``coerce`` 不认（它认的形态见 :data:`_STORED_FORMS`）。

    为什么要把这条边界写下来：拿渲染值回填输入框再存一次，就是「界面上看着有值、写回去
    变成未定义」。输入框要回填的是 :meth:`VariablePool.ask_raw` 出来的那串**用户当时填的
    文本**，不是 :meth:`VariablePool.render` 出来的那份。
    """
    assert expr.coerce("30分", expr.T_DURATION) is None
    assert expr.coerce("1小时30分", expr.T_DURATION) is None
    assert expr.coerce("17:45", expr.T_TIME) is not None   # 时刻这一侧显示形态与存储形态同形


def test_a_bare_number_means_the_unit_of_its_own_type():
    """裸数字 = **该类型自己的单位**：时长是秒、时刻是当日秒数。分钟一律写 ``30m``
    （本仓以分钟为单位的格子键名都带 ``_min`` —— 单位写在名字上）。"""
    assert expr.coerce(5400, expr.T_DURATION).raw == 5400
    assert expr.coerce("5400", expr.T_DURATION).raw == 5400
    assert expr.coerce("30m", expr.T_DURATION).raw == 1800
    assert expr.coerce("09:00", expr.T_DURATION).raw == 9 * 3600   # 上一版配置里「9 小时」的写法
    assert expr.coerce(9 * 3600, expr.T_TIME).raw == 9 * 3600


def test_unreadable_values_are_none_not_zero_or_empty():
    """折算不出来一律 ``None``，**不猜**：``0`` / ``""`` / ``False`` 三个都是**有意义的
    值**，拿它们代替「不知道」就是把不知道说成了知道。"""
    assert expr.coerce("办公室", expr.T_TIME) is None
    assert expr.coerce("17.5", expr.T_TIME) is None    # 半点不是某一个时刻
    assert expr.coerce(True, expr.T_NUMBER) is None    # bool 是 int 的子类，放过去会变成 1
    assert expr.coerce(True, expr.T_STRING) is None
    assert expr.coerce("", expr.T_DATE) is None
    assert expr.coerce(None, expr.T_ENUM) is None
    assert expr.coerce("maybe", expr.T_BOOL) is None
    assert expr.coerce("", expr.T_DURATION) is None


# ══ 声明（spec） ════════════════════════════════════════════════════


def test_a_malformed_section_is_rejected_whole():
    """**整段**形状问题一律拒（"哪个声明说的是哪一个变量"没有唯一答案时不该放行）。
    与 :func:`spec_mod.parse_all` 的粒度**不同**，不是同一件事的两种实现。"""
    for bad in (
        {"id": "a"},                    # 顶层不是列表
        ["不是 mapping"],
        [{"name": "缺 id"}],
        [{"id": "   "}],
        [{"id": "1a"}],                 # 数字开头
        [{"id": "有 空格"}],
        [{"id": "a"}, {"id": "a"}],     # 重复
    ):
        with pytest.raises(spec_mod.VariableSpecError):
            spec_mod.check_section(bad)
    spec_mod.check_section(None)        # 缺省 = 没有声明
    spec_mod.check_section([])


def test_a_single_bad_declaration_only_skips_itself_and_says_why():
    """单条写坏的语义问题只让**这一条**不生效，其余照常加载；而且要说得出为什么
    （静默跳过在界面上就是「没有它」，没有任何线索）。"""
    specs, problems = spec_mod.parse_all([
        {"id": "good", "source": "derived", "expr": "1h"},
        {"id": "bad_source", "source": "telepathy"},
        {"id": "no_expr", "source": "derived"},
        {"id": "no_type", "source": "ask", "trigger": {"when": ["daily"]}},
        {"id": "no_trigger", "source": "ask", "type": "time"},
        {"id": "bad_when", "source": "ask", "type": "time", "trigger": {"when": ["凌晨"]}},
        {"id": "framework_in_config", "source": "framework"},
        {"id": "now"},
        {"id": "asks_self", "source": "ask", "type": "time",
         "trigger": {"when": ["daily"],
                     "conditions": [{"var": "asks_self", "op": "==", "value": "1"}]}},
        {"id": "bad_cond", "source": "ask", "type": "time",
         "trigger": {"when": ["daily"],
                     "conditions": [{"var": "scene", "op": "≈", "value": "1"}]}},
        {"id": "bad_unfilled", "source": "ask", "type": "time",
         "trigger": {"when": ["daily"], "only_if_unfilled": "yes"}},
    ])
    assert [s.id for s in specs] == ["good"]
    joined = "\n".join(problems)
    for needle in (
        "source 只能是",
        "必须写 expr",
        "必须写 type",
        "必须写 trigger",
        "trigger.when 只能是",
        "不需要在配置里声明",     # source: framework
        "框架内置变量",           # 顶掉内置 id
        "不能引用变量自身",
        "op 只能是",
        "only_if_unfilled 得是布尔值",
    ):
        assert needle in joined, needle
    assert len(problems) == 10


def test_ask_trigger_conditions_count_as_dependencies():
    """``depends_on`` 是拓扑排序与环检的**唯一一处**。它漏掉 ask 的条件依赖，
    「ask 依赖派生、派生又依赖该 ask」那个环就会静默通过。"""
    specs, problems = spec_mod.parse_all([
        {"id": "ask_a", "source": "ask", "type": "time",
         "trigger": {"when": ["daily"], "conditions": [
             {"var": "scene", "op": "==", "value": "办公室"},
             {"var": "now", "op": ">=", "value": "09:00"},
         ]}},
    ])
    assert problems == ()
    assert specs[0].depends_on() == ("scene", "now")


def test_component_variables_read_a_published_snapshot_and_default_to_enum():
    """组件变量的取值口 = 组件**已发布的状态快照**（框架不认组件私有方法名）。"""
    specs, problems = spec_mod.parse_all([
        {"id": "scene", "name": "当前场景", "source": "component",
         "component": "route_guard", "key": "profile", "field": "summary"},
    ])
    assert problems == ()
    got = specs[0]
    assert (got.component, got.key, got.field) == ("route_guard", "profile", "summary")
    assert got.type == spec_mod.DEFAULT_COMPONENT_TYPE == expr.T_ENUM
    assert got.name == "当前场景"
    assert got.depends_on() == ()                  # 值由组件推来，不依赖别的变量
    _, problems = spec_mod.parse_all([
        {"id": "x", "source": "component", "component": "c", "key": "k", "field": "内部字段"},
    ])
    assert problems and "field 只能是" in problems[0]
    _, problems = spec_mod.parse_all([{"id": "x", "source": "component", "field": "state"}])
    assert problems and "必须写 component" in problems[0]


def test_builtins_are_declared_nowhere_and_carry_the_three_framework_types():
    assert spec_mod.BUILTIN_IDS == ("now", "date", "weekday")
    assert [s.id for s in spec_mod.builtin_specs()] == ["now", "date", "weekday"]
    assert spec_mod.type_map(spec_mod.builtin_specs()) == {
        "now": expr.T_TIME, "date": expr.T_DATE, "weekday": expr.T_WEEKDAY,
    }


def test_the_display_name_falls_back_to_the_id_and_is_never_invented():
    """没写 ``name`` 就用 id —— **不编一个中文名**（编出来的名字与用户在配置里写的对不上）。"""
    specs, _ = spec_mod.parse_all([{"id": "off_time", "source": "derived", "expr": "1h"}])
    assert specs[0].name == "off_time"


# ══ 结构化规则（rules） ═════════════════════════════════════════════


def test_rules_say_undecided_instead_of_guessing():
    """``None`` ＝ **判不出来**（不是「不成立」）。三种情形，调用方要按「还没到时候」
    处置并记一条日志 —— 直接回 ``False`` 的话，「配错了」与「还没到条件」在界面上长得
    一模一样，而这一整块的存在意义就是让用户看得懂「为什么现在没提醒」。"""
    read = {"scene": expr.Value(expr.T_ENUM, "办公室")}.get
    type_of = {"scene": expr.T_ENUM}.get
    assert rules.matches(rules.RuleCondition("scene", "==", "办公室"), read, type_of) is True
    assert rules.matches(rules.RuleCondition("scene", "!=", "家"), read, type_of) is True
    assert rules.matches(rules.RuleCondition("scene", "in", ["办公室", "会议室"]),
                         read, type_of) is True
    assert rules.matches(rules.RuleCondition("缺席", "==", "x"), read, type_of) is None
    assert rules.matches(rules.RuleCondition("scene", ">", "办公室"), read, type_of) is None
    assert rules.matches(rules.RuleCondition("scene", "in", "办公室"),
                         read, type_of) is None        # in 的右侧得是列表


def test_rules_compare_order_only_between_comparable_types():
    read = {"t": expr.Value(expr.T_TIME, 9 * 3600)}.get
    type_of = {"t": expr.T_TIME}.get
    assert rules.matches(rules.RuleCondition("t", ">=", "08:30"), read, type_of) is True
    assert rules.matches(rules.RuleCondition("t", "<", "08:30"), read, type_of) is False
    assert rules.matches(rules.RuleCondition("t", ">=", "乱写"), read, type_of) is None


def test_enum_and_string_compare_as_one_family():
    """组件暴露的离散值声明成 ``enum``，而用户在规则里把 ``value`` 写成不带引号的
    ``办公室``（YAML 读出来是 ``str``）—— 判它们不同型等于让「场景 == 办公室」这条
    永远判不出来。"""
    read = {"scene": expr.Value(expr.T_ENUM, "办公室")}.get
    assert rules.matches(rules.RuleCondition("scene", "==", "办公室"),
                         read, {"scene": expr.T_ENUM}.get) is True


def test_an_empty_condition_set_is_true_and_a_broken_one_is_not_a_list():
    """空合取恒真（数学上如此）。故「无条件」这件事的代价由 **ask 那边**付：空条件集
    意味着**永远该问**，登记方必须挡在登记之前。"""
    assert rules.matches_all((), {"x": expr.Value(expr.T_STRING, "1")}.get, lambda _n: None) is True
    assert rules.parse_conditions(None) == ((), None)
    assert rules.parse_conditions([]) == ((), None)
    _, problem = rules.parse_conditions("scene == 办公室")
    assert problem and "列表" in problem


def test_the_rule_parser_judges_shape_only_and_leaves_existence_to_the_caller():
    """只判形状（有 var / op 认识 / 有 value）：变量名存不存在归调用方 —— 两处各判一次
    会出现「解析说行、求值说没有这个变量」。"""
    got, problem = rules.parse_one({"var": "没人声明过的", "op": "==", "value": 1})
    assert problem is None and got is not None and got.var == "没人声明过的"
    assert rules.parse_one({"op": "==", "value": 1})[1]
    assert rules.parse_one({"var": "x", "op": "≈", "value": 1})[1]
    assert rules.parse_one({"var": "x", "op": "=="})[1]


# ══ 按日运行值（store） ═════════════════════════════════════════════


def test_the_state_file_keeps_the_same_shape_as_the_agenda_log():
    """与 ``todo/day_log`` 的 ``agenda_log.json`` **逐字同款**：``{"days": {日期: {id: 裸值}}}``。

    跨模块对拍：拿**它**的读法读**我**写的那份文件。形状漂开不会有任何报错，只会
    「今天填的值读不回来」。"""
    assert store.write_value("start_time", "09:00", TODAY) is True
    raw = json.loads(store.path().read_text(encoding="utf-8"))
    assert raw == {"days": {TODAY: {"start_time": "09:00"}}}
    assert store.STATE_FILENAME == "variables_state.json"

    from barkit.plugins.todo import day_log

    assert day_log.read_days(store.STATE_FILENAME) == {TODAY: {"start_time": "09:00"}}


def test_a_broken_state_file_reads_as_empty():
    """这份文件是「今天用户填了什么」：丢了只该让今天重新问一遍，不该让变量池起不来。"""
    store.path().parent.mkdir(parents=True, exist_ok=True)
    store.path().write_text("{ 这不是 json", encoding="utf-8")
    assert store.read_days() == {}
    assert store.read_day(TODAY) == {}


def test_only_the_last_thirty_days_are_kept():
    """去尾：这份文件每天写一行，不去尾就会长成一份日志（一个常驻 app 的痕迹不该长成
    日志）。"""
    days = {f"2026-09-{i:02d}": {"v": "1"} for i in range(1, 31)}
    days.update({f"2026-10-{i:02d}": {"v": "1"} for i in range(1, 11)})
    assert store.write_days(days) is True
    kept = store.read_days()
    assert len(kept) == store.KEEP_DAYS == 30
    assert "2026-09-10" not in kept and "2026-09-11" in kept
    assert "2026-10-10" in kept


def test_writing_an_empty_value_deletes_that_one_entry():
    store.write_value("a", "1", TODAY)
    assert store.write_value("a", "", TODAY) is True
    assert store.read_day(TODAY) == {}
    assert store.write_value("", "1", TODAY) is False      # 空 id 不写


# ══ 变量池（pool） ══════════════════════════════════════════════════


def test_the_pool_resolves_all_four_sources_in_one_pass():
    pool, _clock = _pool()
    assert pool.problems() == ()
    assert [s.id for s in pool.specs()] == [
        "now", "date", "weekday", "scene", "start_time", "work_span", "off_time",
    ]                                                     # 拓扑序：被依赖的在前
    assert [s.id for s in pool.declared()] == ["scene", "start_time", "work_span", "off_time"]
    assert pool.spec_of("now").source == "framework"

    assert pool.render("now") == "08:27"
    assert pool.render("date") == TODAY
    assert pool.render("weekday") == "5"                  # 0 = 周一，与 datetime.weekday() 同口径
    assert pool.render("work_span") == "9小时"
    assert pool.render("start_time") == V.PLACEHOLDER_ASK_UNFILLED
    assert pool.render("off_time") == V.PLACEHOLDER_UNDEFINED
    assert pool.render("没有这个变量") == V.PLACEHOLDER_UNDEFINED


def test_the_three_placeholder_words_say_three_different_things():
    """「待询问」＝还有件事没做、「未收到」＝等组件、「未定义」＝配置或数据有问题。
    混成一句，界面上就再也看不出「这个变量为什么没值」。"""
    pool, _clock = _pool()
    assert V.PLACEHOLDER_ASK_UNFILLED == "待询问"
    assert V.PLACEHOLDER_COMPONENT_ABSENT == "未收到"
    assert V.PLACEHOLDER_UNDEFINED == "未定义"
    assert len({V.PLACEHOLDER_ASK_UNFILLED, V.PLACEHOLDER_COMPONENT_ABSENT,
                V.PLACEHOLDER_UNDEFINED}) == 3
    assert pool.render("scene") == V.PLACEHOLDER_COMPONENT_ABSENT   # 组件还没报过
    assert pool.render("start_time") == V.PLACEHOLDER_ASK_UNFILLED  # 还没问
    assert pool.render("off_time") == V.PLACEHOLDER_UNDEFINED       # 上游没值 ⇒ 算不出来


def test_a_component_value_comes_from_the_snapshot_field_the_declaration_names():
    pool, _clock = _pool()
    topic = V.status_topic_of("route_guard", "profile")
    assert pool.watched_topics() == (topic,)

    assert pool.on_status(topic, _FakeReport(state="office", summary="办公室")) is True
    assert pool.render("scene") == "办公室"
    assert pool.on_status("plugin.别人.status.main", _FakeReport(summary="x")) is False
    assert pool.on_status(topic, None) is True            # 空快照收下，取不到值就是「未收到」
    assert pool.render("scene") == V.PLACEHOLDER_COMPONENT_ABSENT

    # 换 field 取的是另一个字段 —— 声明是唯一真源，不是「有快照就行」
    pool.load([{"id": "scene", "source": "component", "component": "route_guard",
                "key": "profile", "field": "state"}])
    pool.begin_pass()
    pool.on_status(topic, _FakeReport(state="office", summary="办公室"))
    assert pool.render("scene") == "office"


def test_the_component_declaration_points_at_a_real_state_machine_and_field():
    """组件变量那三格（组件 / 状态机 / 字段）必须指得到真东西：指不到的后果是**永远
    「未收到」**且不报错（订阅了一条没人发的 topic，或取了一个不存在的字段）。"""
    from barkit.contribution.specs_report import StateReport
    from barkit.plugins.route_guard.plugin import Plugin as RouteGuardPlugin

    assert set(spec_mod.FIELDS) <= {f.name for f in dataclass_fields(StateReport)}

    probe = RouteGuardPlugin.__new__(RouteGuardPlugin)     # 不跑 __init__（那会绑 ctx）
    assert "profile" in {s.key for s in probe.state_specs()}
    assert V.status_topic_of("route_guard", "profile") == probe.status_topic("profile")
    assert V.status_topic_of("route_guard", "main") == probe.status_topic()


def test_the_pool_asks_the_clock_once_per_pass():
    """**一轮一个世界**：同一轮里所有变量看到的是同一个"此刻"。读一次问一次时钟的症状是
    「面板说 17:27、清单说 17:28」—— 同一份 ``off_time`` 两个答案。"""
    pool, clock = _pool()
    assert clock.calls == 1                                # begin_pass 那一次
    for _ in range(5):
        pool.render("now")
        pool.render("off_time")
    assert clock.calls == 1
    pool.begin_pass()
    assert clock.calls == 2


def test_ask_values_are_stored_per_day_and_reset_at_midnight():
    """「按日」这条语义落在池里（跨天自动重读），不靠任何调用方记得去做 —— 靠调用方的
    结果是「昨天填的今天还生效」，而界面上写的是「今天几点上班」。"""
    pool, clock = _pool()
    assert pool.set_ask_value("start_time", "09:00") is True
    pool.begin_pass()
    assert pool.render("start_time") == "09:00"
    assert pool.render("off_time") == "18:00"
    assert store.read_day(TODAY) == {"start_time": "09:00"}

    clock.moment = datetime.datetime(2026, 10, 11, 8, 30, 0)
    pool.begin_pass()
    assert pool.render("start_time") == V.PLACEHOLDER_ASK_UNFILLED
    assert pool.render("off_time") == V.PLACEHOLDER_UNDEFINED
    assert store.read_day(TODAY) == {"start_time": "09:00"}   # 昨天那一笔还在盘上


def test_a_failed_write_does_not_change_the_in_memory_value(monkeypatch):
    """**写盘成功才动内存**。反过来的后果是「界面说填好了、盘上没有」，重启就没了 ——
    而用户已经在别的界面上看见那个值了。"""
    pool, _clock = _pool()
    monkeypatch.setattr(store, "write_value", lambda *_a, **_k: False)
    assert pool.set_ask_value("start_time", "09:00") is False
    assert pool.ask_raw("start_time") == ""
    assert pool.render("start_time") == V.PLACEHOLDER_ASK_UNFILLED


def test_the_ask_verdict_has_four_states_not_two():
    """四种结论**不是布尔**：登记方要分开处置 —— 「条件不成立」与「条件判不出来」在界面
    上说句话完全不同，合成一个 ``False`` 之后「等条件成立」那条路就永远等不到。"""
    assert (V.ASK_READY, V.ASK_FILLED, V.ASK_BLOCKED, V.ASK_UNDECIDED) == (
        "ready", "filled", "blocked", "undecided",
    )
    pool, _clock = _pool()
    assert pool.ask_ready("start_time") == V.ASK_UNDECIDED   # 场景还没报过 ⇒ 判不出来
    _scene_report(pool, "家")
    assert pool.ask_ready("start_time") == V.ASK_BLOCKED     # 今天不在办公室
    _scene_report(pool, "办公室")
    assert pool.ask_ready("start_time") == V.ASK_READY
    pool.set_ask_value("start_time", "09:00")
    assert pool.ask_ready("start_time") == V.ASK_FILLED      # only_if_unfilled 缺省 True
    assert pool.ask_ready("off_time") is None                # 不是 ask 变量
    assert pool.ask_ready("没有这个变量") is None


def test_interpolation_keeps_what_it_does_not_know():
    """``text`` 里还有内核自己的占位符（``{到点}`` / ``{现在}`` / ``{剩余}``），它们不是
    变量；值当刻未定义的变量也**整片**保留 —— 换成空串之后「8:00 前交日报」会变成
    「 前交日报」，读起来像文案写错了，而事实是这个变量还没有值。"""
    pool, _clock = _pool()
    assert pool.interpolate("{到点} 前提交日报（{没有的变量}）") == "{到点} 前提交日报（{没有的变量}）"
    assert pool.interpolate("{off_time} 下班") == "{off_time} 下班"     # 上游没值
    assert pool.interpolate("") == ""
    pool.set_ask_value("start_time", "09:00")
    pool.begin_pass()
    assert pool.interpolate("{off_time} 下班（{到点} 提醒）") == "18:00 下班（{到点} 提醒）"


def test_the_fingerprint_follows_the_rendered_text():
    """指纹比的是**渲染文本**：变了之后消费方要重画的就是文本；两种裸值写法在文本上
    一样时不该触发一次重画。"""
    pool, _clock = _pool()
    before = pool.fingerprint()
    assert dict(before)["start_time"] == V.PLACEHOLDER_ASK_UNFILLED
    assert pool.fingerprint() == before                     # 没变就是没变
    pool.set_ask_value("start_time", "09:00")
    pool.begin_pass()
    after = pool.fingerprint()
    assert after != before
    assert dict(after)["off_time"] == "18:00"


def test_a_derived_variable_without_a_declared_type_gets_one_inferred():
    """**守的是「存得进去、算不出来」**：``derived`` 的 ``type`` 可以留空（随表达式推），
    而类型是折算右侧裸值的唯一依据 —— 留空的话「``off_time >= 17:00``」这类规则恒判
    「判不出来」，界面上却照显示用户以为的那个时刻。

    判据落在**中间量**（``type_of`` 不空）上，不落在结局上：结局（值是 18:00）在类型留空
    时也是对的，看不出这条是不是真生效。"""
    pool, _clock = _pool()
    assert pool.type_of("work_span") == expr.T_DURATION
    assert pool.type_of("off_time") == expr.T_TIME
    pool.set_ask_value("start_time", "09:00")
    pool.begin_pass()
    assert rules.matches(rules.RuleCondition("off_time", ">=", "17:00"),
                         pool.value, pool.type_of) is True
    assert rules.matches(rules.RuleCondition("off_time", ">=", "19:00"),
                         pool.value, pool.type_of) is False


def test_declaring_a_type_that_contradicts_the_expression_is_reported_but_survives():
    """声明类型与表达式算出来的不一致：**变量还在**（运行期的值照算得出来），但界面上
    该说得出这一处不对。类型标注错了不该让变量消失。"""
    pool, _clock = _pool([{"id": "half", "source": "derived", "expr": "1h", "type": "number"}])
    assert pool.render("half") == "1小时"
    assert pool.type_of("half") == expr.T_DURATION          # 以算出来的那个为准
    assert any("声明成" in p for p in pool.problems())


def test_a_reference_to_a_variable_that_does_not_exist_is_reported_not_silent():
    """引用不存在的变量 ⇒ 跳过这一条并说清为什么。放着不管的后果是它**静默恒为
    「未定义」**，界面上那一格只显示「未定义」，看不出是自己把名字写错了。"""
    pool, _clock = _pool([
        {"id": "lonely", "source": "derived", "expr": "1h"},
        {"id": "typo", "source": "derived", "expr": "nope + 1h"},
        {"id": "after_typo", "source": "derived", "expr": "typo + 1h"},
    ])
    assert [s.id for s in pool.specs()] == ["now", "date", "weekday", "lonely"]
    joined = "\n".join(pool.problems())
    assert "引用了不存在的变量「nope」" in joined
    # 依赖它的那一条要说得出**自己依赖的那个名字**（不是转述上游那句、也不是静默）
    said = [p for p in pool.problems() if p.startswith("after_typo")]
    assert said and "typo" in said[0] and "nope" not in said[0]
    assert pool.render("lonely") == "1小时"


def test_cyclic_dependencies_are_dropped_whole_and_their_dependents_say_why():
    """环上的变量**整条跳过**（§2.3），且跳过谁不取决于遍历顺序 —— 用户改一个字被扔掉
    的变量就换一个的话，界面上只说「未定义」，看不出是环。"""
    pool, _clock = _pool([
        {"id": "a", "source": "derived", "expr": "b + 1h"},
        {"id": "b", "source": "derived", "expr": "a + 1h"},
        {"id": "lonely", "source": "derived", "expr": "1h"},
        {"id": "after", "source": "derived", "expr": "a + 1h"},
    ])
    assert [s.id for s in pool.specs()] == ["now", "date", "weekday", "lonely"]
    joined = "\n".join(pool.problems())
    assert "环形依赖" in joined
    assert "a" in joined and "b" in joined
    assert "after" in joined                                # 依赖环的那条要说得出为什么
    assert pool.render("lonely") == "1小时"                  # 不相干的变量不受牵连


def test_an_ask_variable_may_cycle_through_its_own_conditions():
    """``ask`` 的条件依赖也算边：这样「ask 依赖派生、派生又依赖该 ask」的环能被抓住
    （只数表达式依赖的那一份实现看不见它）。"""
    pool, _clock = _pool([
        {"id": "shift", "source": "ask", "type": "time",
         "trigger": {"when": ["daily"],
                     "conditions": [{"var": "off_time", "op": ">=", "value": "18:00"}]}},
        {"id": "off_time", "source": "derived", "expr": "shift + 9h"},
    ])
    assert [s.id for s in pool.specs()] == ["now", "date", "weekday"]
    assert len(pool.problems()) == 2


# ══ 配置段接入 ══════════════════════════════════════════════════════


def test_the_config_carries_the_declarations_and_nothing_else(tmp_path):
    """``config.variables`` 只存**声明**：运行值一律不进配置（``ask`` 按日落在
    ``variables_state.json``、``derived`` 现算、``component`` 实时推）—— 这样设置页
    回写配置时碰不到用户今天填过的东西。"""
    from barkit.core.config import Config

    user = tmp_path / "config.yaml"
    user.write_text(yaml.safe_dump(
        {"variables": [
            {"id": "start_time", "source": "ask", "type": "time",
             "trigger": {"when": ["daily"]}, "prompt": "今天几点上班？"},
        ]},
        allow_unicode=True,
    ), encoding="utf-8")
    got = Config(user_path=user).variables()
    assert [v["id"] for v in got] == ["start_time"]
    assert "value" not in got[0]


def test_the_shipped_default_declares_scene_and_the_user_layer_replaces_it(tmp_path):
    """出厂那一份给了 ``scene``（开箱即有，来自 ``route_guard`` 的 ``profile``）；而
    **用户在用户配置里写了 ``variables`` 就整段盖掉它**。

    后一条要钉住，因为它反直觉：加一条自己的变量会把出厂那条挤掉。这与
    ``plugin_order`` 同款（这也是一个列表、按**整体覆盖**合并，不逐条合并），
    ``config.default.yaml`` 里那句「要保留它，请把它一起抄过去」说的就是这条语义 ——
    用例守的是它真的成立。
    """
    from barkit.core.config import Config

    fresh = Config(user_path=tmp_path / "config.yaml")
    assert [v["id"] for v in fresh.variables()] == ["scene"]
    assert fresh.variables()[0]["component"] == "route_guard"

    user = tmp_path / "config.yaml"
    user.write_text(yaml.safe_dump(
        {"variables": [{"id": "mine", "source": "derived", "expr": "1"}]},
        allow_unicode=True,
    ), encoding="utf-8")
    assert [v["id"] for v in Config(user_path=user).variables()] == ["mine"]

    # 用户段里没有这一节 ⇒ 出厂那份照旧（「没写过」不是「写成空」）
    user.write_text("{}\n", encoding="utf-8")
    assert [v["id"] for v in Config(user_path=user).variables()] == ["scene"]


def test_a_bad_section_is_rejected_at_load_time(tmp_path):
    """整段形状不合法 ⇒ ``ConfigError``（配置加载失败会沿用上一份有效快照并广播
    ``error``，用户看得见 —— 而不是「有一半变量悄悄没生效」）。"""
    from barkit.core.config import Config, ConfigError

    user = tmp_path / "config.yaml"
    user.write_text(yaml.safe_dump({"variables": [{"id": "a"}, {"id": "a"}]}), encoding="utf-8")
    with pytest.raises(ConfigError):
        Config(user_path=user)
