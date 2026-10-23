"""core/variables/spec：变量**声明**的形状、校验与加载（只有"它是什么"，没有值）。

声明与值分离是本架构的一条硬约束（设计文档 §2.2）：``config.variables`` 只存声明
（id / 来源 / 触发 / 表达式），运行期的值一律不进配置 —— ``ask`` 的回答按日落在
``variables_state.json``（§2.5）、``derived`` 现算、``component`` 由组件实时发布、
``framework`` 内置。这样配置可以安全地被设置窗回写，迁移 / 恢复默认也不会丢用户填的值。

两个层次的校验，粒度不同（**不是同一件事的两种实现**，改一处要一起看）：

- :func:`check_section`：**整段**的形状问题 —— 顶层不是列表、某项不是映射、id 缺失 /
  不是合法标识符 / 重复。这些会让「哪个声明说的是哪一个变量」失去唯一答案，故**整份配置
  拒掉**（``core/config/schema.validate`` 转成 ``ConfigError``，与坏 ``plugins:`` 段同款）。
- :func:`parse_all`：**单条**的语义问题 —— 来源不认识、缺必填字段、表达式写错、引用了
  自己、引用了不存在的变量。这些只让**这一条**不生效（记一条 problem，其余照常加载）——
  与 §2.3「环形依赖报错并跳过该变量」同一条处置。

方向（本仓那条「读侧必须比校验侧宽」，严 ⊂ 宽）：本模块是**严**的那一份，``pool`` 与
``expr`` 是**宽**的那一份。反过来（引擎比声明窄）的症状是「存得进去、算不出来」——
变量在设置页看着是好的，运行时恒「未定义」且不报错。

``framework`` 一类的变量（``now`` / ``date`` / ``weekday``）由框架**内置**，不在配置里
声明（§2.2）。配置里写一条 ``source: framework`` 只会是一条没有提供者的死变量，故这里
**跳过并说明**，而不是收下。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from barkit.core.variables import expr, rules

#: 能在配置里声明的来源。``framework`` 不在其中（见模块 docstring）。
SOURCES: "tuple[str, ...]" = ("component", "ask", "derived")

#: 内置变量（框架提供，无需声明也不许被同名声明顶掉）。
BUILTIN_IDS: "tuple[str, ...]" = ("now", "date", "weekday")

#: ``trigger.when`` 的三个时机（设计文档 §2.5）。
WHEN: "tuple[str, ...]" = ("startup", "daily", "variable_changed")

#: ``component`` 型能取 ``StateReport`` 的哪两个字段。取快照而不是取组件私有方法：
#: 插件之间只经总线，框架不认组件的内部函数名。
FIELDS: "tuple[str, ...]" = ("state", "summary")

#: ``component`` 型没写 ``type`` 时的默认类型。状态机的取值是**离散值**
#: （§2.4 的 ``enum`` 举的例子就是场景名），故默认 enum 而不是 string。
DEFAULT_COMPONENT_TYPE = expr.T_ENUM

_ID_RE = re.compile(rf"^{expr.ID_PATTERN}$")


class VariableSpecError(Exception):
    """整段声明的形状问题（调用方转成 ``ConfigError``）。"""


@dataclass(frozen=True)
class AskTrigger:
    """``ask`` 变量的触发条件（§2.5）。

    ``when`` 是时机集合，``conditions`` 是**依赖其他变量**的结构化规则（与任务生效规则
    同构）。``only_if_unfilled`` 缺省 **True**：这一格问的是「已填就不再问」，而它缺省
    为 False 的后果是「每次启动都再问一遍已经答过的值」。
    """

    when: "tuple[str, ...]" = ()
    conditions: "tuple[rules.RuleCondition, ...]" = ()
    only_if_unfilled: bool = True


@dataclass(frozen=True)
class VariableSpec:
    """一条变量声明。"""

    id: str
    name: str
    source: str
    type: str
    expr: str = ""
    trigger: "AskTrigger | None" = None
    prompt: str = ""
    component: str = ""
    key: str = ""
    field: str = ""

    def depends_on(self) -> "tuple[str, ...]":
        """这一条依赖哪些别的变量（拓扑排序与环形检测读它，**唯一一处**）。

        - ``derived`` ⇒ 表达式里引用到的变量；
        - ``ask`` ⇒ 触发条件里引用到的变量（如「命中办公室才问」依赖 ``scene``）；
        - ``component`` ⇒ 没有（它的值由组件推来）。

        两条各写一遍的后果是环检只看得见其中一类依赖 —— 那样一个
        「ask 依赖 derived、derived 又依赖该 ask」的环会静默通过。
        """
        if self.source == "derived" and self.expr:
            # 表达式语法错在 parse_all 里已经拦下（那一条不会走到这里）。
            return expr.referenced_variables(self.expr)
        if self.source == "ask" and self.trigger is not None:
            return tuple(cond.var for cond in self.trigger.conditions)
        return ()


def check_section(value: object) -> None:
    """整段 ``config.variables`` 的形状校验。

    :raises VariableSpecError: 形状不合法（调用方转 ``ConfigError``）。

    只判「哪个声明说的是哪一个变量」这件事：顶层得是列表、每项得是映射、``id`` 得是
    非空且合法的标识符、且**不得重复**。其余一律交给 :func:`parse_all` 逐条跳过 ——
    一条写错的变量不该让整份配置回滚（那会让用户在界面上看到的是**上一版**配置，
    而报错只留在日志里）。
    """
    if value is None:
        return
    if not isinstance(value, (list, tuple)):
        raise VariableSpecError("variables 必须是列表")
    seen: "set[str]" = set()
    for i, item in enumerate(value):
        where = f"variables[{i}]"
        if not isinstance(item, dict):
            raise VariableSpecError(f"{where} 必须是 mapping")
        name = item.get("id")
        if not isinstance(name, str) or not name.strip():
            raise VariableSpecError(f"{where}.id 必须是非空字符串")
        if not _ID_RE.match(name.strip()):
            raise VariableSpecError(
                f"{where}.id 只能是字母 / 数字 / 下划线且不以数字开头（读到 {name!r}）"
            )
        if name.strip() in seen:
            raise VariableSpecError(f"{where}.id 与前面的重复：{name.strip()}")
        seen.add(name.strip())


def parse_all(
    value: object,
) -> "tuple[tuple[VariableSpec, ...], tuple[str, ...]]":
    """逐条解析声明；返回 ``(收下的声明, 跳过原因)``。

    **不抛异常**（形状问题已由 :func:`check_section` 挡在配置层）：单条写错只让这一条
    不生效，其余照常 —— 一条写坏的派生表达式不该把整个变量池拉掉。
    """
    if not isinstance(value, (list, tuple)):
        return (), ()
    specs: "list[VariableSpec]" = []
    problems: "list[str]" = []
    for i, raw in enumerate(value):
        spec, problem = _parse_one(raw, i)
        if spec is None:
            problems.append(problem or f"variables[{i}] 解析不出来")
            continue
        specs.append(spec)
    return tuple(specs), tuple(problems)


def _parse_one(raw: object, index: int) -> "tuple[VariableSpec | None, str | None]":
    if not isinstance(raw, dict):
        return None, f"variables[{index}] 不是 mapping"
    name = str(raw.get("id") or "").strip()
    source = str(raw.get("source") or "").strip()
    label = name or f"variables[{index}]"
    if not name:
        return None, f"variables[{index}] 缺少 id"
    if name in BUILTIN_IDS:
        return None, f"{label}：{name} 是框架内置变量，不需要声明"
    if source == "framework":
        return None, f"{label}：framework 变量由框架内置，不需要在配置里声明"
    if source not in SOURCES:
        return None, f"{label}：source 只能是 {' / '.join(SOURCES)}（读到 {source!r}）"
    declared = raw.get("type")
    if declared is not None and declared not in expr.TYPES:
        return None, f"{label}：type 不认识：{declared!r}"
    if source == "derived":
        return _parse_derived(raw, name, label, declared)
    if source == "ask":
        return _parse_ask(raw, name, label, declared)
    return _parse_component(raw, name, label, declared)


def _parse_derived(
    raw: dict, name: str, label: str, declared: object
) -> "tuple[VariableSpec | None, str | None]":
    text = str(raw.get("expr") or "").strip()
    if not text:
        return None, f"{label}：derived 变量必须写 expr（怎么算）"
    # 语法错在这一层就拦下（类型错要等所有变量都解析完才能判，见 pool 的拓扑排序）。
    try:
        expr.referenced_variables(text)
    except expr.ExprError as e:
        return None, f"{label}：表达式写错了 —— {e}"
    type_name = str(declared) if declared else ""
    # 类型留空 = 「随表达式推」。推不出来是**正常**的（引用的变量还没解析完），
    # 故这里不报错、留空串，由 pool 在拓扑排序之后补。
    return VariableSpec(name, _name_of(raw, name), "derived", type_name, expr=text), None


def _parse_ask(
    raw: dict, name: str, label: str, declared: object
) -> "tuple[VariableSpec | None, str | None]":
    if declared is None:
        return None, f"{label}：ask 变量必须写 type（要收集的是什么）"
    trigger_raw = raw.get("trigger")
    if trigger_raw is None:
        # 不默认一个时机：ask 变量的全部意义就是「什么时候问」，替用户猜一个的结果是
        # 它会在他没预期的时刻弹（而界面上那块显示的是他以为的那一个）。
        return None, f"{label}：ask 变量必须写 trigger（什么时候问）"
    if not isinstance(trigger_raw, dict):
        return None, f"{label}：trigger 得是一个 mapping"
    when_raw = trigger_raw.get("when") or []
    if isinstance(when_raw, str):
        when_raw = [when_raw]
    if not isinstance(when_raw, (list, tuple)):
        return None, f"{label}：trigger.when 得是一个列表"
    when: "list[str]" = []
    for item in when_raw:
        token = str(item).strip()
        if token not in WHEN:
            return None, f"{label}：trigger.when 只能是 {' / '.join(WHEN)}（读到 {item!r}）"
        if token not in when:
            when.append(token)
    conditions, problem = rules.parse_conditions(trigger_raw.get("conditions"))
    if conditions is None:
        return None, f"{label}：trigger.conditions —— {problem}"
    for cond in conditions:
        if cond.var == name:
            return None, f"{label}：触发条件不能引用变量自身（{name}）"
    unfilled = trigger_raw.get("only_if_unfilled", True)
    if not isinstance(unfilled, bool):
        return None, f"{label}：only_if_unfilled 得是布尔值"
    return (
        VariableSpec(
            name,
            _name_of(raw, name),
            "ask",
            str(declared),
            trigger=AskTrigger(tuple(when), conditions, unfilled),
            prompt=str(raw.get("prompt") or ""),
        ),
        None,
    )


def _parse_component(
    raw: dict, name: str, label: str, declared: object
) -> "tuple[VariableSpec | None, str | None]":
    component = str(raw.get("component") or "").strip()
    key = str(raw.get("key") or "").strip()
    field = str(raw.get("field") or "").strip()
    if not component or not key:
        return None, f"{label}：component 变量必须写 component（哪一位组件）与 key（哪台状态机）"
    if field not in FIELDS:
        return None, f"{label}：field 只能是 {' / '.join(FIELDS)}（读到 {field!r}）"
    type_name = str(declared) if declared else DEFAULT_COMPONENT_TYPE
    return (
        VariableSpec(
            name,
            _name_of(raw, name),
            "component",
            type_name,
            component=component,
            key=key,
            field=field,
        ),
        None,
    )


def _name_of(raw: dict, name: str) -> str:
    """显示名；没写就用 id（**不编一个中文名**：编出来的名字与用户在配置里写的对不上）。"""
    label = str(raw.get("name") or "").strip()
    return label or name


def builtin_specs() -> "tuple[VariableSpec, ...]":
    """三个内置变量（§2.1）：不需要声明，任何任务都能直接引用。"""
    return (
        VariableSpec("now", "当前时间", "framework", expr.T_TIME),
        VariableSpec("date", "日期", "framework", expr.T_DATE),
        VariableSpec("weekday", "星期", "framework", expr.T_WEEKDAY),
    )


def type_map(specs: "Iterable[VariableSpec]") -> "dict[str, str]":
    """``id → type``（校验与规则折算共用一份）。后出现的同名项不覆盖先出现的 ——
    重名在 :func:`check_section` 已经拒了，这里只是别让它静默决定胜负。"""
    out: "dict[str, str]" = {}
    for spec in specs:
        out.setdefault(spec.id, spec.type)
    return out
