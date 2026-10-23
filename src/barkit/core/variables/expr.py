"""core/variables/expr：派生变量表达式的 tokenizer ＋ 递归下降求值器（纯函数，零 IO）。

这是**唯一**一处把表达式文本变成值的地方（设计文档 §3）。三个消费方共用它：变量池求
``derived`` 变量、任务字段的 ``{var}`` 插值结果再解析成时刻（§4.3）、框架「变量」页的
保存前校验（§2.6）。三条路各写一遍的结果是「设置页存得进去、运行时算不出来」。

硬约束（§3.1，改之前先读）：

- **禁 ``eval`` / ``exec`` / ``compile``**。手写 tokenizer ＋ 递归下降 —— 表达式是**用户
  数据**（来自 config.yaml），能写表达式就等于能写代码。
- **禁属性访问、禁函数调用、禁导入、禁赋值**。语法里没有这些形状（`:` 只出现在时间字面
  量 ``9:00`` 里），不是靠黑名单拦下来的。
- v1 **没有函数调用**（§3.3）：``now`` / ``date`` / ``weekday`` 是 **framework 变量**，
  直接当变量引用 —— 引擎因此最小：只有 `+ - * /`、括号、字面量与变量。

两处规格冲突的裁法（v1.2 钉死，别再两边猜）：

- §3.2 的散文列了「比较运算（``== != > < >= <=``）→ bool」，而 §3.3 的语法里 **没有**比较
  这一层。**以语法为准**：v1 表达式**不含比较**。比较归任务生效规则那一侧（§4.2 的结构化
  ``conditions``，不是表达式），故这里没有它的用处。
- §3.3 的语法有 ``term := factor (('*'|'/') factor)*``，而 §3.2 的类型规则**只**列了
  ``time`` 与 ``duration`` 的加减。**以类型规则为准**：``*`` / ``/`` 能解析、但一律报类型
  错误（消息里点名「v1 不支持乘除」）—— 比直接删掉语法好：报的是**类型**错误而不是让人
  以为括号写错了。

读侧与校验侧的方向（本仓那条「读侧必须比校验侧宽」，严 ⊂ 宽）：**校验侧（编辑器 /
``validate()``）是严的那一份，引擎是宽的那一份**。引擎比编辑器窄的症状是「存得进去、算不
出来」——变量在设置页看着是好的，运行时恒为「未定义」，且不报错。所以引擎认的字面量形状
是编辑器认的那一份的**超集**（如 ``9:30`` 这种单位数小时、带秒的 ``09:30:15``）。

时间一律以**当日秒数**为基准（:data:`DAY_S` 那个圆）：``time + duration`` 可以越过午夜
（夜班 17:45 ＋ 9h ＝ 次日 02:45），引擎**不回卷**（回卷会把「加了 9 小时」这件事丢掉），
格式化成 ``HH:MM`` 时才按 :data:`DAY_S` 取模。这与 ``todo/scheduler.py`` 的 ``fmt_hhmm``
同一口径（那边是插件内核、不许 import，故两边各有一份）—— 本模块只回卷**显示**，不算术。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

# ── 变量值类型（设计文档 §2.4）─────────────────────────────────────────
T_TIME = "time"
T_DATE = "date"
T_WEEKDAY = "weekday"
T_DURATION = "duration"
T_NUMBER = "number"
T_STRING = "string"
T_ENUM = "enum"
T_BOOL = "bool"

#: 取值闭集。声明里写了别的字面量 = 声明写错了（由 ``spec.parse_all`` 拒）。
TYPES: "tuple[str, ...]" = (
    T_TIME,
    T_DATE,
    T_WEEKDAY,
    T_DURATION,
    T_NUMBER,
    T_STRING,
    T_ENUM,
    T_BOOL,
)

#: 一天的秒数。**只用于显示取模**（见模块 docstring 最后一段）。
DAY_S = 86_400

#: 时间字面量识别的三种形状（都是**读侧**的样子；编辑器只放行第一种）：
#: ``9:30``（单位数小时 —— 编辑器也放行的那种）、``09:30``、``09:30:15``。
#: 带秒的交给 :func:`time.fromisoformat` 那一支。
_TIME_SHAPE = ("%H:%M", "%H:%M:%S")

#: 时长字面量：``N h`` / ``N m`` 写成**紧挨着**的 ``9h`` / ``30m`` / ``1.5h``。
#: **不许写空格**（``9 h``）：那会与「数字 9 后面跟着一个名叫 h 的变量」撞车，
#: 而变量名允许是单个字母。宁可要求用户贴着写，也不要造一个靠空格区分的语法。
_DURATION_UNITS = {"h": 3600, "m": 60}

#: 变量名（= 变量 id）的形状。**id 必须匹配它**，否则表达式里引用不到自己声明的那个变量
#: —— 那种「声明得好好的、求值永远未定义」是静默失败，故由 ``spec`` 在加载时按同一份形
#: 状拒掉（本常量是那一处判据的唯一真源）。
ID_PATTERN = r"[A-Za-z_][A-Za-z0-9_]*"


class ExprError(Exception):
    """表达式的语法 / 类型 / 未定义变量。**消息是给用户看的一句话**（中文）。"""


@dataclass(frozen=True)
class Value:
    """一个求值结果：**类型 ＋ 裸值**。

    类型必须显式带着：``9``（number）与 ``9:00``（time）的裸值都是整数秒/数字，
    只看裸值分不出来，而运算规则全按类型走（§3.2 的规则表）。

    ``raw`` 的形态按类型固定：``time`` 当日秒数 ``int``（可为负 / 可越过 :data:`DAY_S`）、
    ``duration`` 秒 ``int``、``weekday`` ``0..6`` 的 ``int``、``number`` ``int | float``、
    ``date`` ``"YYYY-MM-DD"``、``string`` / ``enum`` ``str``、``bool`` ``bool``。
    """

    type: str
    raw: object

    def render(self) -> str:
        """给用户看的那一份文本（变量池与设置页都读它，**只此一处**）。

        ``bool`` 写「是 / 否」而不是 ``True``：变量页列的是中文文案，冒出个 ``True``
        是拿实现细节当界面。``weekday`` 写数字（0 = 周一，与 ``datetime.weekday()``
        同口径）—— 写成「周一」就得替用户选一个起点（周日还是周一），那是替用户决定。
        """
        if self.type == T_TIME:
            return fmt_time(self.raw)                 # type: ignore[arg-type]
        if self.type == T_DURATION:
            return fmt_duration(self.raw)             # type: ignore[arg-type]
        if self.type == T_BOOL:
            return "是" if self.raw else "否"
        if self.type == T_NUMBER:
            raw = self.raw
            if isinstance(raw, float) and raw.is_integer():
                return str(int(raw))
            return str(raw)
        return str(self.raw)


def fmt_time(seconds: object) -> str:
    """当日秒数 → ``HH:MM``（按 :data:`DAY_S` 取模）。**唯一**的时间格式化口。"""
    total = int(seconds) % DAY_S                       # type: ignore[arg-type]
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}"


def fmt_duration(seconds: object) -> str:
    """秒 → ``H小时M分`` / ``M分`` / ``H小时``（整分则省掉「0 分」）。

    负时长保留符号（``-30m`` 这种中间量会出现）：写成绝对值会让用户看到
    「30 分」而实际是往前挪 —— 一个看得出来的错总比一个看不出来的错好。
    """
    total = int(seconds)                                # type: ignore[arg-type]
    sign = "-" if total < 0 else ""
    total = abs(total)
    hours, minutes = divmod(total // 60, 60)
    if hours and minutes:
        return f"{sign}{hours}小时{minutes}分"
    if hours:
        return f"{sign}{hours}小时"
    return f"{sign}{minutes}分"


def time_to_seconds(text: object) -> "int | None":
    """``"HH:MM"`` / ``"HH:MM:SS"`` → 当日秒数；读不出来返回 ``None``。

    这是**读侧**那一份（宽）：三种形状都认。编辑器的校验侧是 ``spec`` / 变量页那一份
    （严），两者反向就是「存得进去、算不出来」，见模块 docstring。
    """
    if not isinstance(text, str):
        return None
    value = text.strip()
    for shape in _TIME_SHAPE:
        try:
            parsed = datetime.strptime(value, shape).time()
        except ValueError:
            continue
        return parsed.hour * 3600 + parsed.minute * 60 + parsed.second
    return None


# ── 规则表：类型运算的唯一判定处（设计文档 §3.2 逐条落地）──────────────
#: ``(运算, 左类型, 右类型) → 结果类型``。**没有列出的组合一律报错**（「无隐式类型转换」
#: 那条硬约束就落在这张表的完整性上）。两个读者共用它：:func:`_result_type`（纯类型判定，
#: 给校验用）与 :func:`_apply`（先经前者判、再算值）—— 各写一遍必然漂。
_OP_TYPES: "dict[tuple[str, str, str], str]" = {
    ("+", T_TIME, T_DURATION): T_TIME,
    ("-", T_TIME, T_DURATION): T_TIME,
    ("-", T_TIME, T_TIME): T_DURATION,
    ("+", T_DURATION, T_DURATION): T_DURATION,
    ("-", T_DURATION, T_DURATION): T_DURATION,
}


def _result_type(op: str, left: str, right: str) -> str:
    """:raises ExprError: 这条组合没定义。消息点名两侧类型与该运算，便于用户改。"""
    found = _OP_TYPES.get((op, left, right))
    if found is None:
        raise ExprError(
            f"「{type_word(left)} {op} {type_word(right)}」没有定义"
            f"（v1 只有 时间±时长、时间−时间、时长±时长）"
        )
    return found


#: 报错时说人话的类型名。混进实现用的英文标记（``time`` / ``duration``）会让用户
#: 在界面上读到一份他没见过的词表。**只有这一份**（:func:`type_word` 之外没人再建表）。
_TYPE_WORDS = {
    T_TIME: "时间",
    T_DATE: "日期",
    T_WEEKDAY: "星期",
    T_DURATION: "时长",
    T_NUMBER: "数字",
    T_STRING: "文本",
    T_ENUM: "枚举",
    T_BOOL: "是否",
}


def type_word(type_name: str) -> str:
    """类型标记 → 界面上那个词（``T_TIME`` → ``时间``）。

    公开的理由：校验侧要说的那句事实里带类型名（「这串算出来是**数字**，不是时刻」），
    而那张表在这儿 —— 调用方各建一份的话，同一次类型错误会在两处读出两个词。
    """
    return _TYPE_WORDS.get(type_name, type_name)


def _apply(op: str, left: Value, right: Value) -> Value:
    """按规则表算值。类型判定只走 :func:`_result_type`（**不在此处再判一遍**）。"""
    result = _result_type(op, left.type, right.type)
    lhs = int(left.raw)                                 # type: ignore[arg-type]
    rhs = int(right.raw)                                # type: ignore[arg-type]
    return Value(result, lhs + rhs if op == "+" else lhs - rhs)


# ── tokenizer ────────────────────────────────────────────────────────


def _tokenize(text: str) -> "list[tuple[str, object]]":
    """文本 → ``(种类, 值)`` 列表；末尾补 ``("eof", "")``。

    种类：``num`` / ``time`` / ``dur`` / ``str`` / ``var`` / ``op``。
    """
    out: "list[tuple[str, object]]" = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch in "+-*/()":
            out.append(("op", ch))
            i += 1
            continue
        if ch == '"':
            end = text.find('"', i + 1)
            if end < 0:
                raise ExprError("字符串少了收尾的双引号")
            out.append(("str", text[i + 1 : end]))
            i = end + 1
            continue
        if ch.isdigit():
            # 数字 / 时间 / 时长三选一：先看紧跟着的字符决定是哪一个。
            j = i
            while j < n and (text[j].isdigit() or text[j] == "."):
                j += 1
            head = text[i:j]
            if j < n and text[j] == ":":
                out.append(("time", _read_time_literal(text, i, n)))
                i = _time_end(text, i, n)
                continue
            if j < n and text[j] in _DURATION_UNITS:
                out.append(("dur", _read_duration_literal(head, text[j])))
                i = j + 1
                continue
            out.append(("num", _read_number(head)))
            i = j
            continue
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            out.append(("var", text[i:j]))
            i = j
            continue
        raise ExprError(f"认不出的字符 {ch!r}")
    out.append(("eof", ""))
    return out


def _read_time_literal(text: str, start: int, n: int) -> int:
    """时间字面量 → 当日秒数。``raise`` 而不是返回 ``None``：非法输入必须变成一句
    用户看得懂的话，静默取 0 等于把 ``25:70`` 当成 ``00:00``。"""
    end = _time_end(text, start, n)
    seconds = time_to_seconds(text[start:end])
    if seconds is None:
        raise ExprError(f"时间字面量写错了：{text[start:end]!r}（应为 HH:MM）")
    return seconds


def _time_end(text: str, start: int, n: int) -> int:
    """时间字面量的结束位置（``HH:MM`` 或 ``HH:MM:SS``）。"""
    j = start
    while j < n and (text[j].isdigit() or text[j] == ":"):
        j += 1
    return j


def _read_duration_literal(head: str, unit: str) -> int:
    """``"9"`` ＋ 单位 → 秒。小数时长（``1.5h``）按秒取整（向下），别留个 5400.0 秒
    让后面的整数运算去猜。"""
    try:
        amount = float(head)
    except ValueError as e:  # pragma: no cover —— 调用点已保证 head 是数字串
        raise ExprError(f"时长写错了：{head!r}") from e
    return int(amount * _DURATION_UNITS[unit])


def _read_number(head: str) -> object:
    """数字字面量：整数字面量给 ``int``（``9`` 不该显示成 ``9.0``）。

    读不出来就地报错（``1.2.3`` 这种）：返回 ``None`` 会让它在后面某一处变成
    「类型不对」，而用户看到的是自己写的数字被说成别的东西。
    """
    try:
        if "." in head:
            return float(head)
        return int(head)
    except ValueError as e:
        raise ExprError(f"数字写错了：{head!r}") from e


# ── 语法分析（递归下降，落地设计文档 §3.3 的文法）────────────────────


def _parse(tokens: "list[tuple[str, object]]") -> object:
    """``expr := additive``；返回 AST（嵌套元组）。"""
    pos = 0

    def peek() -> "tuple[str, object]":
        return tokens[pos]

    def take(kind: str) -> "tuple[str, object]":
        nonlocal pos
        token = tokens[pos]
        if token[0] != kind:
            raise ExprError(f"这里应该是个{_token_word(kind)}（读到 {token[1]!r}）")
        pos += 1
        return token

    def additive() -> object:
        node = term()
        while peek() == ("op", "+") or peek() == ("op", "-"):
            op = take("op")[1]
            node = ("bin", op, node, term())
        return node

    def term() -> object:
        node = factor()
        while peek() == ("op", "*") or peek() == ("op", "/"):
            op = take("op")[1]
            node = ("bin", op, node, factor())
        return node

    def factor() -> object:
        kind, value = peek()
        if kind == "op" and value == "(":
            take("op")
            node = additive()
            closing = peek()
            if closing != ("op", ")"):
                raise ExprError("括号没有关上")
            take("op")
            return node
        if kind == "op" and value == "-":
            # 一元负号**不在文法里**（§3.3）。就地报错而不是静默当减号用 ——
            # ``-30m`` 那种写法在没有左操作数时是另一件事。
            raise ExprError("这里不能直接写负号（v1 没有一元负号）")
        if kind in ("num", "time", "dur", "str", "var"):
            take(kind)
            return (kind, value)
        raise ExprError(f"这里应该是个数字、时刻、时长、变量或括号（读到 {value!r}）")

    node = additive()
    if peek()[0] != "eof":
        raise ExprError(f"表达式末尾多了东西：{peek()[1]!r}")
    return node


_TOKEN_WORDS = {"op": "运算符", "num": "数字", "time": "时刻", "dur": "时长", "str": "文本", "var": "变量"}


def _token_word(kind: str) -> str:
    return _TOKEN_WORDS.get(kind, kind)


# ── 三个入口：校验（严）／求值（宽）／宽容求值（读侧兜底）────────────


def validate(text: str, type_of: "Callable[[str], str | None]") -> "str | None":
    """保存前校验：``None`` ＝ 没意见；否则是一句**事实**（给界面显示）。

    :param type_of: 变量 id → 类型；``None`` ＝ 没有这个变量（引用不到的变量是错，
        不是「空值」—— 空值会让派生变量静默变「未定义」，用户在界面上看不出为什么）。

    **这是严的那一份**（编辑器 / 保存都走它）；引擎那一侧（:func:`evaluate_or_none`）
    必须至少和它一样宽，见模块 docstring 的方向说明。

    判定只有一份实现（:func:`result_type`）—— 这里再写一遍症状是「设置页说行、
    变量池说不行」，而用户只看得见其中一处。
    """
    if not isinstance(text, str) or not text.strip():
        return "表达式是空的"
    try:
        result_type(text, type_of)
    except ExprError as e:
        return str(e)
    return None


def result_type(text: str, type_of: "Callable[[str], str | None]") -> str:
    """静态推出表达式的**结果类型**（不碰值），设计文档 §2.4「派生表达式的类型由运算
    规则推出」。

    :param type_of: 变量 id → 类型；``None`` ＝ 没有这个变量（与 :func:`validate` 同款）。
    :raises ExprError: 语法 / 类型错、或引用了 ``type_of`` 认不出的变量。

    **为什么要有这个口**：``derived`` 声明里的 ``type`` 可以留空（"随表达式推"，见
    ``spec._parse_derived``），而类型是**折算右侧裸值的唯一依据**（:func:`coerce`）——
    留空的话，规则里写「``off_time >= 17:00``」时右侧折算不出目标类型，条件恒判「判不出来」
    且不报错，界面上那块显示的却是用户以为的那个时刻。变量池在加载时用本口补这一格。
    """
    return _check(_parse(_tokenize(text)), type_of)


def evaluate(text: str, resolve: "Callable[[str], Value | None]") -> Value:
    """求值。

    :raises ExprError: 语法 / 类型错、或被引用的变量当刻「未定义」（``resolve`` 给
        ``None``）。**不吞异常**：调用方决定「未定义」怎么呈现（变量池取「该变量取值未
        定义」，任务侧按「条件不满足」处理）。
    """
    return _eval(_parse(_tokenize(text)), resolve)


def evaluate_or_none(
    text: str, resolve: "Callable[[str], Value | None]"
) -> "Value | None":
    """宽容版：算不出来就 ``None``（＝「未定义」）。

    **读侧的兜底口**：用户手改 YAML 写坏了表达式，症状应当是「这个变量没值、引用它的
    任务按条件不满足处理」（§3.1），而不是整个配置加载失败、或每拍抛一次异常把 tick
    打红。算不出来的原因由调用方记日志（这里不记：纯函数不碰 IO）。
    """
    try:
        return evaluate(text, resolve)
    except ExprError:
        return None


def _check(node: object, type_of: "Callable[[str], str | None]") -> str:
    """静态类型检查：返回表达式的结果类型。**只判类型**，不碰值。"""
    kind = node[0]                                       # type: ignore[index]
    if kind == "var":
        name = str(node[1])                              # type: ignore[index]
        found = type_of(name)
        if found is None:
            raise ExprError(f"没有名为「{name}」的变量")
        return found
    if kind == "num":
        return T_NUMBER
    if kind == "time":
        return T_TIME
    if kind == "dur":
        return T_DURATION
    if kind == "str":
        return T_STRING
    op = str(node[1])                                    # type: ignore[index]
    if op in ("*", "/"):
        # 语法里有、类型规则里没有（见模块 docstring 第二条裁法）。这里显式拦一句，
        # 否则报出来的是「数字 × 数字没有定义」，用户会以为是自己括号的问题。
        raise ExprError(f"v1 不支持「{op}」（只有加减）")
    left = _check(node[2], type_of)                      # type: ignore[index]
    right = _check(node[3], type_of)                     # type: ignore[index]
    return _result_type(op, left, right)


def _eval(node: object, resolve: "Callable[[str], Value | None]") -> Value:
    """求值（与 :func:`_check` 同一棵 AST、同一张规则表）。"""
    kind = node[0]                                       # type: ignore[index]
    if kind == "var":
        name = str(node[1])                              # type: ignore[index]
        found = resolve(name)
        if found is None:
            raise ExprError(f"变量「{name}」当刻没有值")
        return found
    if kind == "num":
        return Value(T_NUMBER, node[1])                  # type: ignore[index]
    if kind == "time":
        return Value(T_TIME, node[1])                    # type: ignore[index]
    if kind == "dur":
        return Value(T_DURATION, node[1])                # type: ignore[index]
    if kind == "str":
        return Value(T_STRING, node[1])                  # type: ignore[index]
    op = str(node[1])                                    # type: ignore[index]
    if op in ("*", "/"):
        raise ExprError(f"v1 不支持「{op}」（只有加减）")
    return _apply(
        op, _eval(node[2], resolve), _eval(node[3], resolve)   # type: ignore[index]
    )


def tokens_of(text: str) -> "Iterable[tuple[str, object]]":
    """调试 / 测试用：看一眼 tokenizer 认成了什么（生产路径不读它）。"""
    return tuple(_tokenize(text))


def referenced_variables(text: str) -> "tuple[str, ...]":
    """表达式里引用了哪些变量（按出现顺序、去重）。**只解析，不求值**。

    依赖图（拓扑排序、环形检测、变化传播）靠它：三处各遍历一遍 AST 就会有一处漏掉
    ``("bin", …)`` 里递归下去的那一层，而漏掉的表现正是「环检不出来」。

    :raises ExprError: 语法错 —— 调用方按「这个变量加载不进来」处理（见 ``spec``）。
    """
    seen: "list[str]" = []

    def walk(node: object) -> None:
        kind = node[0]                                   # type: ignore[index]
        if kind == "var":
            name = str(node[1])                          # type: ignore[index]
            if name not in seen:
                seen.append(name)
            return
        if kind == "bin":
            walk(node[2])                                # type: ignore[index]
            walk(node[3])                                # type: ignore[index]

    walk(_parse(_tokenize(text)))
    return tuple(seen)


#: ``bool`` 型的文本写法（按日运行值文件里什么都存成字符串 ⇒ 读侧必须认这几种）。
_TRUE_WORDS = ("true", "yes", "on", "1", "是", "真")
_FALSE_WORDS = ("false", "no", "off", "0", "否", "假")


def coerce(raw: object, type_name: str) -> "Value | None":
    """把一个**裸值**（YAML 的 ``value:``、按日运行值文件里的字符串）折算成某个类型的
    :class:`Value`；折算不出来返回 ``None``。

    为什么需要它：结构化规则里的比较是「变量 vs 一个字面量」，而字面量的类型在 YAML 里
    不带标记（``value: 办公室`` 与 ``value: "09:00"`` 都是字符串）。要比就得先把右侧折成
    左侧那个类型 —— 拿 ``"09:00"`` 与时间比大小、拿 ``办公室`` 与枚举比相等，是同一件事
    的两面。

    **文本形态一律要认**（本仓那条「读侧必须比校验侧宽」的一个具体落点）：``ask`` 的运行值
    在盘上就是字符串（``variables_state.json`` 把什么都写成一串字），故本函数读得出来的
    形态必须覆盖写侧写得出的每一个 —— 读得比写得窄的症状是「填了 08:27，重启后变量恒
    未定义」，而文件里明明有那个值。

    折算不出来返回 ``None`` ⇒ 调用方按「判不出来 / 未定义」处理（**不**当成 0、空串、
    ``False`` —— 那三个都是**有意义的值**，拿它们代替「不知道」就是把不知道说成了知道）。
    """
    if type_name == T_TIME:
        if isinstance(raw, str):
            seconds = time_to_seconds(raw)
            return Value(T_TIME, seconds) if seconds is not None else None
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        if isinstance(raw, float) and not raw.is_integer():
            return None                     # ``17.5`` 不是某个时刻
        return Value(T_TIME, int(raw))      # 裸数字当**当日秒数**（本类型自己的单位）
    if type_name == T_DURATION:
        if isinstance(raw, bool):
            return None
        if isinstance(raw, (int, float)):
            return Value(T_DURATION, int(raw))
        return _dur_from_text(raw)
    if type_name in (T_NUMBER, T_WEEKDAY):
        number = _as_number(raw)
        if number is None:
            return None
        return Value(type_name, number)
    if type_name == T_BOOL:
        return _as_bool(raw)
    if type_name == T_DATE:
        return Value(T_DATE, raw) if isinstance(raw, str) and raw.strip() else None
    if type_name in (T_STRING, T_ENUM):
        if isinstance(raw, bool) or raw is None:
            return None
        return Value(type_name, str(raw))
    return None


def _as_number(raw: object) -> "int | float | None":
    """数字（或能读成数字的文本）。``bool`` 先挡掉 —— 它是 ``int`` 的子类，
    放过去会把 ``true`` 变成 1（同一个坑 ``todo/scheduler._work_hours_of`` 也踩过）。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw) if float(raw).is_integer() else float(raw)
    if isinstance(raw, str):
        text = raw.strip()
        try:
            number = float(text)
        except ValueError:
            return None
        return int(number) if number.is_integer() else number
    return None


def _as_bool(raw: object) -> "Value | None":
    if isinstance(raw, bool):
        return Value(T_BOOL, raw)
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in _TRUE_WORDS:
            return Value(T_BOOL, True)
        if text in _FALSE_WORDS:
            return Value(T_BOOL, False)
    return None


def _dur_from_text(raw: object) -> "Value | None":
    """时长文本：``"30m"`` / ``"1h"``（本仓常见的写法）或 ``"09:00"``（上一版配置里
    「9 小时」的写法）。纯数字文本按**秒**（与裸数字同一条口径，见 :func:`coerce`）。"""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    unit = text[-1]
    if unit in _DURATION_UNITS:
        try:
            return Value(T_DURATION, int(float(text[:-1]) * _DURATION_UNITS[unit]))
        except ValueError:
            return None
    number = _as_number(text)
    if number is not None and ":" not in text:
        return Value(T_DURATION, int(number))
    seconds = time_to_seconds(text)
    if seconds is None:
        return None
    return Value(T_DURATION, seconds)
