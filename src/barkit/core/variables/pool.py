"""core/variables/pool：全局变量池 —— 加载声明、按依赖求值、缓存与变化传播（设计文档 §2）。

四类来源（§2.1）在这一个类里各有自己的取法：

======================  ==========================================================
``framework``          内置三只（``now`` / ``date`` / ``weekday``），从**注入的时钟**现算；
                       不落盘、不声明。
``component``          组件发布的**状态快照**（``plugin.<组件>.status.<key>`` 那条 topic 的
                       ``StateReport``），由 :meth:`on_status` 喂进来，取 ``state`` /
                       ``summary`` 之一。
``ask``                用户填的回答，**按日**落在 ``variables_state.json``（§2.5）。
``derived``            由表达式从其他变量算出（``expr`` 引擎）。
======================  ==========================================================

三条口径（改之前先读）：

- **一"轮"一个世界**：:meth:`begin_pass` 清掉上一轮的缓存并**只取一次时钟**。同一轮里
  所有变量看到的是同一个"此刻" —— 否则 ``off_time = start_time + 9h`` 一次求值能算出
  两个答案（跨过整秒的那一刻），而消费方（面板文案与主窗体清单）各读一次就会出现
  「面板说 17:27、清单说 17:28」。
- **值只有一处写法**：一切读值都经 :meth:`value`，它按 :meth:`spec_of` 的来源分派。
  在别处再写一遍「这个变量怎么取」（比如设置页自己判 ask 有没有填）必然会漂。
- **「未定义」不是错误**：算不出来的变量取值 ``None``（§3.1），引用它的任务按「条件不
  满足」处理。**不抛异常**：一个手改坏的表达式不该把每拍 tick 打红。

``ask`` 的**询问**不在这里做（那是 Phase 2 的中枢登记），此处只提供 :meth:`ask_ready`
这一个**纯判据**（§2.5：时机 ＋ 条件 ＋ 未填）。
"""

from __future__ import annotations

import dataclasses
import logging
import re
from datetime import datetime
from typing import Callable, Iterable

from barkit.core.variables import expr, rules, spec as spec_mod, store

log = logging.getLogger(__name__)

#: ``{变量名}`` 的形状（与 ``expr.ID_PATTERN`` 同一份真源；只换花括号不认别的形状）。
#:
#: **插值与「这段文案引用了哪些变量」共用它**（见 :func:`text_variables`）：两处各写一遍
#: 的结果是某次改动里悄悄漂开（一处认 ``{ a }``、另一处不认），而症状看着像两个 bug ——
#: 「变量条里列着它、插值却不换它」。
_INTERP_RE = re.compile(r"\{(" + expr.ID_PATTERN + r")\}")

#: 组件变量的状态 topic 形态。与 ``Plugin.status_topic`` **同源**（那一处是唯一真源，
#: 这里是 core 侧按契约拼 —— 由 ``tests/test_variables.py`` 的对拍用例钉住两者相等）。
_STATUS_TOPIC = "plugin.{component}.status.{key}"


def status_topic_of(component: str, key: str) -> str:
    """某组件某台状态机的状态 topic。见 :data:`_STATUS_TOPIC`。"""
    return _STATUS_TOPIC.format(component=component, key=key)


#: 取不到值时给界面看的三个词（**只此一处**：设置页与主窗体都读它，各写一遍迟早一处
#: 说「未填」一处说「待询问」）。它们说的是**为什么没有值**这件事，三句都不一样：
#: 「还没问你」（待询问）／「问了但算不出来 / 引用不到」（未定义）／「组件还没报过」（未收到）。
PLACEHOLDER_ASK_UNFILLED = "待询问"
PLACEHOLDER_UNDEFINED = "未定义"
PLACEHOLDER_COMPONENT_ABSENT = "未收到"

#: :meth:`VariablePool.ask_ready` 的四种结论。**只此一处**（登记方按常量比，别比字面量）。
ASK_READY = "ready"            #: 现在就该问
ASK_FILLED = "filled"          #: 今天已经填过（``only_if_unfilled``），不用再问
ASK_BLOCKED = "blocked"        #: 触发条件不成立（如「今天不在办公室」）
ASK_UNDECIDED = "undecided"    #: 条件判不出来（依赖的变量当刻没值）—— 等信号，别当「不该问」


def text_variables(text: str) -> "tuple[str, ...]":
    """一段**带占位符的文案**里引用到的变量名（去重、按首次出现）。

    ``"{off_time} 前提交"`` → ``("off_time",)``。**只认标识符形状**的名字
    （``[A-Za-z_][A-Za-z0-9_]*``）：内核自己那套占位符（``{到点}`` / ``{现在}``）是中文字，
    本来就不在这条长相里。

    与 :func:`expr.referenced_variables` 不是一件事，**别混用**：那个走表达式的语法树
    （``a + (b + c)``），这个走文案里的 ``{}``。写错一个只会得到「另一边的引用少了几条」，
    而两边都不报错。

    「认不认得出这个名字」**不在这里判**（那要问 :meth:`VariablePool.spec_of`）：收下所有
    形状对的名字，让调用方能如实说「你写的那个 id 池子里没有」。
    """
    out: "list[str]" = []
    for name in _INTERP_RE.findall(text or ""):
        if name not in out:
            out.append(name)
    return tuple(out)


class VariablePool:
    """全局变量池。构造后先 :meth:`load`，之后每轮 :meth:`begin_pass`。"""

    def __init__(self, clock: "Callable[[], datetime] | None" = None) -> None:
        self._clock = clock or datetime.now
        #: 声明的真源（框架「变量」页读写它）。
        self._declared: "tuple[spec_mod.VariableSpec, ...]" = ()
        self._specs: "tuple[spec_mod.VariableSpec, ...]" = ()
        self._problems: "tuple[str, ...]" = ()
        self._order: "tuple[str, ...]" = ()
        self._types: "dict[str, str]" = {}
        #: 今天填的 ask 值（内存那份）。``None`` ＝ 还没加载过。
        self._ask: "dict[str, str] | None" = None
        self._ask_day: str = ""
        #: 组件推来的最新快照（topic → StateReport）。**由 :meth:`on_status` 写**。
        self._reports: "dict[str, object]" = {}
        #: 本池关心的状态 topic（加载时算一次，见 :meth:`load`）。
        self._watched: "tuple[str, ...]" = ()
        #: 本轮缓存（变量 id → 值 / None）。:meth:`begin_pass` 清。
        self._cache: "dict[str, expr.Value | None]" = {}
        self._now: "datetime | None" = None

    # ---------- 加载（配置变了就重来一遍） ----------

    def load(self, declarations: object = None) -> None:
        """加载声明（``config.yaml`` 的 ``variables:`` 段的原样内容）。

        单条写坏只跳过那一条并记进 :meth:`problems`（见 ``spec.parse_all``）——
        一条坏声明不该让整池变量消失。

        三段处置按顺序：解析（``spec.parse_all``）→ 去环（:func:`_drop_cycles`）→
        拓扑排序 ＋ 推类型（:func:`_order_and_infer`）。第三段不是排场：``derived`` 的
        ``type`` 允许留空，不补的话它这一格是空串，规则判定折算右侧裸值时没有目标类型。
        """
        parsed, problems = spec_mod.parse_all(declarations)
        self._declared = parsed
        specs = list(spec_mod.builtin_specs()) + list(parsed)
        kept, more_problems, gone = _drop_cycles(specs)
        kept, infer_problems = _order_and_infer(kept, gone)
        self._specs = tuple(kept)
        self._problems = (
            tuple(problems) + tuple(more_problems) + tuple(infer_problems)
        )
        self._types = spec_mod.type_map(self._specs)
        self._order = tuple(s.id for s in self._specs)
        # 订阅面在**加载时算一次**：:meth:`on_status` 是**每来一份快照**都要跑的口，
        # 在那儿现扫一遍全部声明是每份快照一次白扫（且它跑在总线线程上）。
        self._watched = tuple(
            sorted(
                {
                    status_topic_of(s.component, s.key)
                    for s in self._specs
                    if s.source == "component"
                }
            )
        )
        self._cache.clear()
        self._now = None

    def problems(self) -> "tuple[str, ...]":
        """加载时跳过 / 报错的那些声明（给日志与设置页；**空元组 ＝ 全都收下了**）。

        两类混在一起、顺序即处理顺序：**致命**的（这一条没加载进来）与**只记**的
        （加载了，但有一处该说出来，如声明类型与表达式算出来的不一致）。每一条都是一句
        完整的中文，直接可显示 ⇒ 消费方不必再按前缀分类（那样两边迟早对不上）。
        """
        return self._problems

    def specs(self) -> "tuple[spec_mod.VariableSpec, ...]":
        """全部变量（含内置三只），顺序＝拓扑序（被依赖的在前）。"""
        return self._specs

    def declared(self) -> "tuple[spec_mod.VariableSpec, ...]":
        """**配置里声明**的那些（设置页列的清单就是它；内置那三只不在这里）。"""
        return self._declared

    def spec_of(self, var_id: str) -> "spec_mod.VariableSpec | None":
        for item in self._specs:
            if item.id == var_id:
                return item
        return None

    def type_of(self, var_id: str) -> "str | None":
        return self._types.get(var_id)

    # ---------- 一轮（清缓存 + 取一次时钟） ----------

    def begin_pass(self) -> None:
        """开始新一轮：清缓存、跨天时重读 ask 表、取一次时钟。"""
        self._cache.clear()
        self._now = self._clock()
        self._ask_values()

    def _today(self) -> str:
        return (self._now or self._clock()).date().isoformat()

    def _ask_values(self, force: bool = False) -> "dict[str, str]":
        """今天填的那份（内存副本）。跨天自动重读 —— 「到新的一天该变量回到未填」这条
        语义就落在这里，不靠任何调用方记得去做。"""
        today = self._today()
        if force or self._ask is None or self._ask_day != today:
            self._ask = store.read_day(today)
            self._ask_day = today
            self._cache.clear()
        return self._ask if self._ask is not None else {}

    def ask_raw(self, var_id: str) -> str:
        """这个 ask 变量今天填的**裸文本**；``""`` ＝ 今天还没填（不是 00:00，别当时间用）。"""
        if not var_id:
            return ""
        return str(self._ask_values().get(var_id) or "")

    def set_ask_value(self, var_id: str, raw: str) -> bool:
        """记下这个变量今天填的值（``""`` ＝ 清掉）。``False`` ＝ 没写进盘。

        **写盘成功才动内存**：先写盘、再刷副本 —— 反过来的后果是「界面说填好了、
        盘上没有」，重启就没了，而用户已经在别的界面上看见那个值了。
        """
        if not var_id:
            return False
        day = self._today()
        if not store.write_value(var_id, raw, day):
            return False
        current = dict(self._ask_values())
        if raw:
            current[var_id] = str(raw)
        else:
            current.pop(var_id, None)
        self._ask = current
        self._ask_day = day
        self._cache.clear()
        return True

    # ---------- 组件变量 ----------

    def on_status(self, topic: str, payload: object) -> bool:
        """某组件发布了一份状态快照（**由总线订阅转交**，只在快照的 ``topic`` 属于本池
        声明过的组件变量时才收）。返回「有没有一条声明在听它」。

        只存**最新那份**：变量池要的是"此刻场景是什么"，不是历史。键用 topic，
        因为声明里那三格（组件 / 状态机 / 字段）折出来的正是它。
        """
        if not topic:
            return False
        if topic not in self._watched:
            return False
        self._reports[topic] = payload
        self._cache.clear()
        return True

    def watched_topics(self) -> "tuple[str, ...]":
        """本池要订阅哪几条 topic（装配层按它订阅；**只声明不接线**）。"""
        return self._watched

    # ---------- 读值 ----------

    def value(self, var_id: str) -> "expr.Value | None":
        """此刻的值；``None`` ＝ **未定义**（不是 0，也不是空串）。

        缓存只在 :meth:`begin_pass` 后的这一轮里有效（同一轮一个世界，见模块 docstring）。
        """
        if var_id in self._cache:
            return self._cache[var_id]
        found = self._resolve(var_id)
        self._cache[var_id] = found
        return found

    def _resolve(self, var_id: str) -> "expr.Value | None":
        spec = self.spec_of(var_id)
        if spec is None:
            return None
        if spec.source == "framework":
            return self._framework(var_id)
        if spec.source == "ask":
            raw = self.ask_raw(var_id)
            if not raw:
                return None
            return expr.coerce(raw, spec.type)
        if spec.source == "component":
            report = self._reports.get(status_topic_of(spec.component, spec.key))
            if report is None:
                return None
            raw = getattr(report, spec.field, None)
            if raw is None or raw == "":
                return None
            return expr.coerce(str(raw), spec.type)
        return expr.evaluate_or_none(spec.expr, self.value)

    def _framework(self, var_id: str) -> "expr.Value | None":
        moment = self._now or self._clock()
        if var_id == "now":
            return expr.Value(expr.T_TIME, moment.hour * 3600 + moment.minute * 60 + moment.second)
        if var_id == "date":
            return expr.Value(expr.T_DATE, moment.date().isoformat())
        if var_id == "weekday":
            return expr.Value(expr.T_WEEKDAY, moment.weekday())
        return None

    def render(self, var_id: str) -> str:
        """给界面看的那一份文本；没有值时是 :data:`PLACEHOLDER_UNDEFINED` 那类的词。

        **三句占位文案的区别**（不是同义反复）：ask 没填说「待询问」（还有个动作没做）、
        组件没报过说「未收到」（等它）、其余算不出来说「未定义」（配置或数据有问题）。
        """
        found = self.value(var_id)
        if found is not None:
            return found.render()
        spec = self.spec_of(var_id)
        if spec is not None and spec.source == "ask" and not self.ask_raw(var_id):
            return PLACEHOLDER_ASK_UNFILLED
        if spec is not None and spec.source == "component":
            return PLACEHOLDER_COMPONENT_ABSENT
        return PLACEHOLDER_UNDEFINED

    def interpolate(self, text: str) -> str:
        """把 ``{变量}`` 换成值（**认不出的原样保留**）。

        认不出就留着是刻意的（与 ``todo/scheduler.render_text`` 对 ``{到点}`` 那条同款）：
        ``text`` 里还有内核自己的占位符（``{到点}`` / ``{现在}`` / ``{剩余}``），它们不是
        变量；把认不出的整片吃掉，用户就再也看不出自己写错了哪个字。

        值当刻未定义 ⇒ **整片原样保留**（不是换成空串）：换成空串之后「8:00 前交日报」
        会变成「 前交日报」，读起来像文案写错了，而事实是这个变量还没值。
        """
        if not text or "{" not in text:
            return text

        def _swap(match: "object") -> str:
            name = match.group(1)                          # type: ignore[attr-defined]
            if self.spec_of(name) is None:
                return match.group(0)                      # type: ignore[attr-defined]
            found = self.value(name)
            return found.render() if found is not None else match.group(0)

        return _INTERP_RE.sub(_swap, text)

    def time_ref_problem(self, text: str) -> "str | None":
        """这一格填的字**能不能算成一个时刻**（形状 ＋ 类型，**不取值**）。``None`` ＝ 能。

        「能填什么」只有一份判据 —— **表达式引擎**那一份（:func:`expr.result_type`），
        与 ``todo.task.resolve_time`` 真求值时用的是同一个引擎、同一张类型表。先前这条
        判据长在界面上（一条只认 ``HH:MM`` 与整串 ``{名字}`` 的形状正则），比引擎**窄**：
        ``{off_time}-30m`` / ``18:00+30m`` 这些内核算得出来的写法被白挡，而界面只会说
        「要填 HH:MM 或 {变量名}」—— 用户填的正是那两种之一，读完更不知道自己错在哪。

        两级、通过即停：

        1. **真值**一级 —— 先 ``interpolate`` 一次再求值，那正是内核求值走的那条路
           （变量当刻的值是什么类型，就按什么类型算）；
        2. **声明**一级 —— 当刻取不到值的变量按它**声明的类型**判，好让「今天还没填、
           而它本来就是个时间型变量」的格子照旧放行：值的有无是运行期的事，在这里拦的
           症状是「今天的配置存不下去，明天就好了」。

        空串 ⇒ ``None``（「没填」不是错：不提醒 / 不限，由各自的语义接手）。
        """
        raw = str(text or "").strip()
        if not raw:
            return None

        # ① 先按真值走一遍内核求值那条路。插值后还留着 ``{`` ⇒ 有变量当刻没值，
        #    这一级判不了，落到下一级（它只会**多认**，不会把上面认过的否掉）。
        live = self.interpolate(raw)
        if "{" not in live:
            found = expr.evaluate_or_none(live, self.value)
            if found is not None and found.type == expr.T_TIME:
                return None

        # ② 声明一级。``{名字}`` 是**面向用户**的写法（插值口认的那一个），而引擎里变量
        #    是裸名字 ⇒ 先换掉再判；共用 :data:`_INTERP_RE`，不另一条长相。
        as_expr = _INTERP_RE.sub(lambda m: m.group(1), raw)

        def type_of(name: str) -> "str | None":
            found = self.value(name)
            return found.type if found is not None else self.type_of(name)

        try:
            found_type = expr.result_type(as_expr, type_of)
        except expr.ExprError as e:
            return str(e)                    # 引擎那句话（点名了类型与运算）就是给用户看的
        if found_type != expr.T_TIME:
            return f"这串算出来是「{expr.type_word(found_type)}」，不是时刻"
        return None

    # ---------- ask 的纯判据（§2.5；询问登记在 Phase 2） ----------

    def ask_ready(self, var_id: str) -> "str | None":
        """这一刻这条 ask 变量该不该问。``None`` ＝ **它不是 ask 变量**（没有这条声明，
        或来源是别的三类）。

        四种结论是四个常量、**不是布尔**：登记方要分开处置它们，而合成一个 True/False
        之后「还没到时候」与「判不出来」就再也分不开了 —— 这两件事在界面上说的话完全
        不同（前者继续等信号，后者该说「它依赖的那个变量当刻没值」）。

        ``trigger.conditions`` 判不出来（:data:`ASK_UNDECIDED`）**不当成「不该问」**：
        依赖的变量还没值的那一刻它也会判不出来，把它并进 ``blocked`` 会让「等条件成立」
        这条语义永远等不到（登记方据此就不再重试了）。
        """
        spec = self.spec_of(var_id)
        if spec is None or spec.source != "ask" or spec.trigger is None:
            return None
        verdict = rules.matches_all(spec.trigger.conditions, self.value, self.type_of)
        if verdict is False:
            return ASK_BLOCKED
        if verdict is None:
            return ASK_UNDECIDED
        if spec.trigger.only_if_unfilled and self.ask_raw(var_id):
            return ASK_FILLED
        return ASK_READY

    # ---------- 变化传播（Phase 2 广播用） ----------

    def fingerprint(self) -> "tuple[tuple[str, str], ...]":
        """所有变量当前值的指纹（id → 渲染值）。**只用来判"变没变"**。

        比的是渲染文本而不是裸值：变了之后消费方要重画的是**文本**，而裸值的两种写法
        （``17:27`` 与 ``17:27:00`` 的秒表示）在文本上一样时不该触发一次重画。
        """
        return tuple((s.id, self.render(s.id)) for s in self._specs)


def _drop_cycles(
    specs: "Iterable[spec_mod.VariableSpec]",
) -> "tuple[list[spec_mod.VariableSpec], list[str]]":
    """环形依赖检测：**环上的变量整条跳过**（§2.3「环则报错并跳过该变量」）。

    深度优先、按三色标记找**回边**：撞到一条指回「当前递归栈上」的边时，把栈里从那个
    节点到此处这一段全标掉。只跳其中一个也能求值，但「谁被跳掉」会取决于遍历顺序 ——
    用户改一个字被扔掉的变量就换一个，而界面上只说「未定义」，看不出是环。

    自引用（``a`` 依赖 ``a``）在 :func:`spec._parse_ask` 那一层已经拒了；派生表达式的
    自引用到这儿才会显形（``expr: "a + 1h"`` 里的 ``a`` 就是自己），故这里也按环处理。
    """
    known = {s.id for s in specs}
    edges: "dict[str, tuple[str, ...]]" = {
        s.id: tuple(d for d in s.depends_on() if d in known) for s in specs
    }
    state: "dict[str, int]" = {}          # 1 = 在栈上，2 = 已完成
    path: "list[str]" = []
    cyclic: "set[str]" = set()

    def visit(node: str) -> None:
        state[node] = 1
        path.append(node)
        for dep in edges.get(node, ()):
            here = state.get(dep)
            if here == 1:
                cyclic.update(path[path.index(dep) :])
            elif here is None:
                visit(dep)
        path.pop()
        state[node] = 2

    for name in edges:
        if state.get(name) is None:
            visit(name)
    kept = [s for s in specs if s.id not in cyclic]
    gone = {
        s.id: f"{s.id}：变量之间有环形依赖（互相引用），本条已跳过"
        for s in specs
        if s.id in cyclic
    }
    return kept, list(gone.values()), gone


def _order_and_infer(
    specs: "Iterable[spec_mod.VariableSpec]",
    pre_gone: "dict[str, str] | None" = None,
) -> "tuple[list[spec_mod.VariableSpec], list[str]]":
    """拓扑排序（被依赖的在前）＋ 给 ``derived`` 补类型（设计文档 §2.4）。

    两个问题在同一遍里解，因为它们需要**同一份顺序**：类型只能按依赖顺序推 ——
    ``off_time = start_time + 9h`` 的结果类型取决于 ``start_time`` 的类型，而
    ``start_time`` 自己可能也是一条没写类型的派生。

    三条处置（都只影响**这一条**，与 §2.3「报错并跳过该变量」同款）：

    - 引用了**不存在的**变量 ⇒ 跳过。不能放着不管：它会让这条变量静默恒为「未定义」，
      而界面上那一格只显示「未定义」，看不出是自己把名字写错了。
    - 引用了**已被跳过**的变量 ⇒ 一并跳过（不这么做的话，一条坏声明的波及面取决于
      它在配置里的位置）。
    - 表达式**类型错**（``1 + "a"``）⇒ 跳过。语法错在 ``spec.parse_all`` 已经拦过。

    另外有一处**只记不跳**：声明了 ``type`` 而表达式算出来是另一个 —— 类型标注错了不该
    让变量消失（运行时值是照样算得出来的），但也不能静默：记一条 problem 让变量页显示出来，
    并且**以算出来的那个为准**（引擎那一侧是宽的那一份，见 ``expr`` 的模块 docstring）。
    """
    by_id = {s.id: s for s in specs}
    order: "list[str]" = []
    seen: "set[str]" = set()

    def walk(name: str) -> None:
        """深度优先的**后序**排序：先把依赖走完，再记自己。去环已经跑过，这里不会绕回来。"""
        if name in seen:
            return
        seen.add(name)
        for dep in by_id[name].depends_on():
            if dep in by_id:
                walk(dep)
        order.append(name)

    for item in specs:
        walk(item.id)

    types: "dict[str, str]" = {s.id: s.type for s in specs}
    #: 已知不可用的变量（``pre_gone`` ＝ 刚被环检拿走的那批）。带着它进来是为了让**依赖
    #: 它们**的那一条说得出「我依赖的那个加载不进来」，而不是转述一句「变量不存在」。
    dropped: "dict[str, str]" = dict(pre_gone or {})
    problems: "list[str]" = []

    def type_of(name: str) -> "str | None":
        return types.get(name) or None

    for name in order:
        spec = by_id[name]
        deps = spec.depends_on()
        # 先查「已判定不可用」（含环检拿走的）：它们不在 ``by_id`` 里，故必须排在下面那条
        # 「不存在」之前 —— 否则环的受害者会被说成「变量不存在」，而它是存在过的。
        gone = [d for d in deps if d in dropped]
        unknown = [d for d in deps if d not in by_id and d not in dropped]
        if gone or unknown:
            why = (
                f"依赖的变量「{gone[0]}」加载不进来"
                if gone
                else f"引用了不存在的变量「{unknown[0]}」"
            )
            dropped[name] = f"{name}：{why}，本条已跳过"
            problems.append(dropped[name])
            continue
        if spec.source != "derived":
            continue
        try:
            computed = expr.result_type(spec.expr, type_of)
        except expr.ExprError as e:
            dropped[name] = f"{name}：表达式 —— {e}"
            problems.append(dropped[name])
            continue
        if spec.type and spec.type != computed:
            problems.append(
                f"{name}：声明成「{spec.type}」，表达式算出来是「{computed}」（按算出来的用）"
            )
        types[name] = computed

    kept = [
        dataclasses.replace(by_id[n], type=types[n]) if by_id[n].source == "derived"
        else by_id[n]
        for n in order
        if n not in dropped
    ]
    return kept, problems
