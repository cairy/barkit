"""core/variables/rules：**结构化规则**的值判定（设计文档 §4.2）。

「任务在什么条件下生效」与「ask 变量什么时候问」是**同一种东西**（§2.5 明说「与任务生效
规则同构」），故判定只写这一份 —— 两处各写一遍的结果是「问了却不显示」或「没问却显示了」
那类自相矛盾。

刻意**不用表达式引擎**（§1 的分工）：规则要用户用表单填（勾选 / 下拉），零语法、不会写错；
表达式引擎只管「派生变量怎么算」。两者的判据面因此不同：规则是「若干条比较的合取」，
表达式是「一个值」。

两侧取值的方向（本仓那条「读侧必须比校验侧宽」）：比较的右侧是**配置里写下来的裸值**
（YAML 里没有类型标记），统一按左侧变量声明的类型折算（``expr.coerce``）——
折算不出来就**不猜**（见 :func:`matches` 的返回值说明）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from barkit.core.variables import expr

#: 允许的比较运算。``in`` 的右侧是一个列表（``value: [1,2,3]``），其余是标量。
OPS: "tuple[str, ...]" = ("==", "!=", ">", "<", ">=", "<=", "in")

#: 只有这几类值能比大小；文本 / 枚举 / 是否**仅等性比较**（§3.2 的同一条口径）。
_ORDERED_TYPES = (expr.T_TIME, expr.T_DURATION, expr.T_NUMBER, expr.T_WEEKDAY, expr.T_DATE)


@dataclass(frozen=True)
class RuleCondition:
    """一条 ``{ var, op, value }``。``value`` 保持配置里那个裸值（折算在判定时做）。"""

    var: str
    op: str
    value: object


def parse_one(raw: object) -> "tuple[RuleCondition | None, str | None]":
    """把一条裸 dict 解析成 :class:`RuleCondition`；``(None, 原因)`` ＝ 这一条不合法。

    只判**形状**（有 var / op 认识 / 有 value）：变量名存不存在归调用方（它才知道当前
    有哪些变量）—— 两处各判一次会出现「解析说行、求值说没有这个变量」。
    """
    if not isinstance(raw, dict):
        return None, "条件得是一个 { var, op, value } 的映射"
    name = raw.get("var")
    if not isinstance(name, str) or not name.strip():
        return None, "条件缺少 var"
    op = raw.get("op")
    if op not in OPS:
        return None, f"条件里的 op 只能是 {' / '.join(OPS)}（读到 {op!r}）"
    if "value" not in raw:
        return None, "条件缺少 value"
    return RuleCondition(name.strip(), str(op), raw["value"]), None


def parse_conditions(
    raw: object,
) -> "tuple[tuple[RuleCondition, ...] | None, str | None]":
    """解析一整段 ``conditions``（一个列表）。``None`` ＝ 这段整体不合法。

    缺省 / 空列表 ⇒ 空元组（**恒为真**）。这不是「没有规则」与「规则为空」的两态问题：
    扁平 AND 的空合取在数学上就是真，调用方拿它当「无条件」用（§2.5 明说空条件集恒真，
    故 ask 那边要挡在登记之前）。
    """
    if raw is None:
        return (), None
    if not isinstance(raw, (list, tuple)):
        return None, "conditions 得是一个列表"
    out: "list[RuleCondition]" = []
    for i, item in enumerate(raw):
        cond, problem = parse_one(item)
        if cond is None:
            return None, f"第 {i + 1} 条条件不合法：{problem}"
        out.append(cond)
    return tuple(out), None


def matches(
    cond: RuleCondition,
    read: "Callable[[str], expr.Value | None]",
    type_of: "Callable[[str], str | None]",
) -> "bool | None":
    """这一条成不成立。**``None`` ＝ 判不出来**（不是「不成立」）。

    三种判不出来的情形，都由调用方按「不满足」处理并记一条日志：

    - 变量当刻没有值（如派生变量算不出来）；
    - 右侧的裸值折算不成左侧那个类型（``value: 办公室`` 拿去比时间）；
    - 对一个只能等性比较的类型用了 ``>`` / ``<``。

    为什么不在这里直接回 ``False``：那样「配错了」与「还没到条件」在日志与界面上长得
    一样，而这一整块的存在意义就是让用户看得懂「为什么现在没提醒」。
    """
    left = read(cond.var)
    if left is None:
        return None
    if cond.op == "in":
        if not isinstance(cond.value, (list, tuple)):
            return None
        return any(
            _equal(left, _coerce(item, left.type)) for item in cond.value
        )
    right = _coerce(cond.value, left.type)
    if right is None:
        return None
    if cond.op == "==":
        return _equal(left, right)
    if cond.op == "!=":
        equal = _equal(left, right)
        return None if equal is None else not equal
    if left.type not in _ORDERED_TYPES or right.type != left.type:
        return None
    lhs, rhs = left.raw, right.raw
    if cond.op == ">":
        return bool(lhs > rhs)                            # type: ignore[operator]
    if cond.op == "<":
        return bool(lhs < rhs)                            # type: ignore[operator]
    if cond.op == ">=":
        return bool(lhs >= rhs)                           # type: ignore[operator]
    return bool(lhs <= rhs)                               # type: ignore[operator]


def matches_all(
    conditions: "tuple[RuleCondition, ...]",
    read: "Callable[[str], expr.Value | None]",
    type_of: "Callable[[str], str | None]",
) -> "bool | None":
    """扁平 AND：全部成立才成立；**任一条判不出来 ⇒ 整体判不出来**。

    空集合 ⇒ ``True``（无条件 ＝ 恒成立），与 :func:`parse_conditions` 那条口径一致。

    为什么「判不出来」要往上传染而不是当成不成立：调用方要**分别**处置这两件事
    （一个继续等信号，一个该在界面上说「这个变量没值」），合成一个 ``False`` 之后
    就再也分不开了。
    """
    verdict: "bool | None" = True
    for cond in conditions:
        one = matches(cond, read, type_of)
        if one is False:
            return False
        if one is None:
            verdict = None
    return verdict


def _coerce(raw: object, type_name: str) -> "expr.Value | None":
    return expr.coerce(raw, type_name)


def _equal(left: "expr.Value", right: "expr.Value | None") -> "bool | None":
    """等性比较：**两侧同型**才比（折算已经保证同型；不同类型一律判不出来）。

    ``enum`` 与 ``string`` 视作同一族：变量声明成 ``enum``（组件暴露的离散值，如场景名），
    而用户在规则里把 ``value`` 写成不带引号的 ``办公室``（YAML 读出来是 ``str``）——
    那是最常见的写法，判它们不同型等于让「场景 == 办公室」这条永远判不出来。
    """
    if right is None:
        return None
    if left.type == right.type:
        return left.raw == right.raw
    texts = (expr.T_STRING, expr.T_ENUM)
    if left.type in texts and right.type in texts:
        return str(left.raw) == str(right.raw)
    return None
