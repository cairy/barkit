"""todo/task：任务**声明**的形状、校验与求值（设计文档 §4）。

**写什么**：``plugins.todo.tasks`` 那一段的形状（:class:`TaskSpec`）、逐条校验
（:func:`parse_all`）、以及「一条任务 ＋ 此刻的变量池 = 内核认的条目」这个换算
（:func:`resolve_item`）。

**不写什么**：不认识盘、不认识 Qt、不认识 ``Config``。声明从哪来由 ``plugin.py`` 给，
算出来的条目喂给 ``scheduler.compute_display``。**它也不认识变量池的类型**
（:class:`~barkit.core.variables.pool.VariablePool`）——只用鸭子类型的三个口
（``value`` / ``type_of`` / ``interpolate``），与 ``rules.matches`` 同款。

与旧 ``items`` 的关系（分阶段实现，§6）：

- 本模块描述的是**新模型**。旧模型的条目表（``items``）与它并存到 Phase 4 迁移
  为止；Phase 4 把 ``items`` 落成 ``tasks`` ＋ ``variables``，旧路才删。
- 故 :func:`resolve_item` 的输出刻意是**旧内核那一份 dict**（``scheduler`` 认的
  ``at`` / ``end`` / ``before_min`` / ``interval_min`` / ``face`` / ``actions`` /
  ``enabled``）—— 这样浮层那一路（``compute_display``）一行都不用改。

两层校验，粒度不同（与 ``core/variables/spec.py`` 同款，**不是同一件事的两种实现**）：

- :func:`check_section`：**整段**的形状问题（顶层不是列表、某项不是映射、``id`` 缺失 /
  不合法 / 重复）。这些会让「哪条任务说的是哪一个」失去唯一答案 ⇒ **整份配置拒掉**。
- :func:`parse_all`：**单条**的语义问题（来源不认识、缺必填、表达式写错、取值域外）。
  这些只让**这一条**不生效（记一条 problem，其余照常）—— 一条写错的任务不该把整个
  组件拉掉。

方向（本仓那条「读侧必须比校验侧宽」，严 ⊂ 宽）：本模块的 :func:`parse_all` 是**严**
的那一份，:func:`resolve_item` 是**宽**的那一份。反过来（求值比校验窄）的症状是
「存得进去、算不出来」—— 任务在设置页看着是好的，运行期恒不显示且不报错。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable

from barkit.core.variables import expr, rules, text_variables
from barkit.plugins.todo.scheduler import (
    ACTION_DONE,
    ACTION_SNOOZE,
    FACES,
    fmt_hhmm,
)

log = logging.getLogger(__name__)

#: 显示面**五选一**（2026-10-11 合并）。``none`` ＝ 不上浮层（只列在主窗体清单里）、
#: ``row`` ＝ 只占一行，其余三档是**上钟面**的三种。
#:
#: **为什么原先的「浮层占一行」＋「钟面」两格并成一格**：钟面寄生在浮层行上 ——
#: ``resolve_item`` 对 ``overlay`` 假的那一条**一个窗口都不给**，而 ``scheduler`` 只从
#: 窗口里选钟面 ⇒ 那一格写了也没人读。分成两格只会让人以为它们可以各配各的。
#: 换算成内核那一份 dict 的两格（``overlay`` / ``face``）在 :func:`resolve_item` 一处收口。
SHOW_NONE = "none"
SHOW_ROW = "row"
SHOWS: "tuple[str, ...]" = (SHOW_NONE, SHOW_ROW) + FACES

#: 命令的**触发点**（2026-10-11，取代原先那枚 ``command_on_done`` 开关）。
#: 前三个是时刻 —— 都由「提醒时间」与它前后两个偏移量算出来；``done`` 是用户自己点完成那一刻。
#: 只能挑一个：原先那枚开关表达的是「到点 ＋ 点完成」这一种组合，而需求是「四个点里挑一个」。
COMMAND_AT_REMINDER = "reminder"      # 提醒时间那一刻（``at``）
COMMAND_AT_LEAD = "lead"              # 提前登场那一刻（``at − lead_min``）
COMMAND_AT_TRAIL = "trail"            # 延后退出那一刻（``at + trail_min``）
COMMAND_AT_DONE = "done"              # 用户点「完成」那一刻
COMMAND_ATS: "tuple[str, ...]" = (
    COMMAND_AT_REMINDER,
    COMMAND_AT_LEAD,
    COMMAND_AT_TRAIL,
    COMMAND_AT_DONE,
)

#: 重复节奏（§4.2）。
LOOP_DAILY = "daily"
LOOP_WEEKDAY = "weekday"
LOOP_INTERVAL = "interval"
LOOPS: "tuple[str, ...]" = (LOOP_DAILY, LOOP_WEEKDAY, LOOP_INTERVAL)

#: 主窗体行内那两枚动作的**配置面文案**（§4.1 写的是中文）。与内核的动作名一对一，
#: 表就是单一真源 —— 两处各写一份的后果是改了一处、另一处继续认旧词。
BUTTON_DONE = "完成"
BUTTON_SNOOZE = "稍后"
BUTTON_ACTIONS: "dict[str, str]" = {
    BUTTON_DONE: ACTION_DONE,
    BUTTON_SNOOZE: ACTION_SNOOZE,
}

_ID_RE = re.compile(rf"^{expr.ID_PATTERN}$")

#: ``loop.every`` 的形状：``30m`` / ``2h``。**只认这两个单位**（与表达式引擎的时长
#: 字面量同一套词），分钟是它折出来的唯一消费者（``scheduler`` 的循环支按分钟算）。
_EVERY_RE = re.compile(r"^\s*(\d+)\s*([hm])\s*$")

#: 一周里算「工作日」的那几天（``datetime.weekday()``：周一 = 0）。
WEEKDAYS: "frozenset[int]" = frozenset({0, 1, 2, 3, 4})

#: 主窗体那一行的状态点三态（设计文档 §4.5 的原型：灰 / 绿 / 红）。
STATUS_PENDING = "pending"     # 灰：还没到点（未触发）
STATUS_DONE = "done"           # 绿：今日已完成
STATUS_OVERDUE = "overdue"     # 红：已过期未处理


class TaskSpecError(Exception):
    """整段声明的形状问题（调用方转成 ``ConfigError``，与 :class:`VariableSpecError` 同款）。"""


@dataclass(frozen=True)
class TaskWindow:
    """生效时段（§4.2）。两格都可以是字面 ``HH:MM`` 或 ``{var}``，求值时才算。

    **一端允许为空**：空着的那一头不设限（起空 ⇒ 从零点起，止空 ⇒ 到当天结束）；
    两端都空的情形在 :func:`_window_of` 里已经折成 ``window is None``，故本类的实例
    至少有一端写了内容。"""

    start: str = ""
    end: str = ""


@dataclass(frozen=True)
class TaskLoop:
    """重复节奏。``minutes`` 只在 ``kind == interval`` 时有意义（由 ``every`` 折出来）。"""

    kind: str = LOOP_DAILY
    every: str = ""
    minutes: int = 0


@dataclass(frozen=True)
class Reminder:
    """提醒（§4.3）。``at`` 空 ＝ **不提醒** —— 这正是「设了才提醒」那条口径的落点。

    **2026-10-11 去掉原先的 ``card``**：那一档（到点弹一枚「完成」卡）整条链路退场 ——
    显示面统一由 :class:`TaskDisplay` 的 ``show`` 加时间窗表达，「完成」在浮层行那枚按钮
    或右键菜单里做。盘上遗留的 ``card`` 键由解析层**静默忽略**（只读认得的键）。
    """

    at: str = ""


@dataclass(frozen=True)
class TaskDisplay:
    """显示面（§4.4 那张表）。默认 ``show = none``：不上浮层、不占钟面。

    ``show`` 五选一（:data:`SHOWS`）；``lead_min`` / ``trail_min`` 框出**时间窗**
    ``[提醒 − 提前, 提醒 ＋ 延后]``（窗口在任何一档下都成立 —— 显示只决定画不画，
    命令的触发点还要拿它算时刻）；``buttons`` 是浮层行上那两枚动作。
    """

    show: str = SHOW_NONE
    reminder: Reminder = field(default_factory=Reminder)
    buttons: "tuple[str, ...]" = ()
    lead_min: int = 0
    trail_min: int = 0


@dataclass(frozen=True)
class TaskSpec:
    """一条任务声明。"""

    id: str
    title: str
    text: str = ""
    enabled: bool = True
    #: 这条任务要不要「完成」这个概念（默认 **要**）。关掉之后它照旧列在主窗体清单里、
    #: 到点显示 / 占浮层行 / 上钟面 / 跑命令，但**不进**「未完成 / 已完成」两个数、
    #: 不出状态点、行内不长「完成」、菜单里也没有「标记今天完成」。整点时钟这类
    #: 「只显示、做不掉」的任务用它 —— 默认 ``True`` 就是现在的行为，故老配置零迁移。
    track_done: bool = True
    command: str = ""
    #: 命令在哪一刻跑（:data:`COMMAND_ATS` 四选一）。缺省 ``reminder`` ＝「提醒时间」
    #: 那一刻，与「到点执行」这个字段名一致。**取代了原先的 ``command_on_done``**：那枚
    #: 开关只能表达「到点 ＋ 点完成」这一种组合，而需求是「四个点里挑一个」。
    command_at: str = COMMAND_AT_REMINDER
    conditions: "tuple[rules.RuleCondition, ...]" = ()
    loop: TaskLoop = field(default_factory=TaskLoop)
    window: "TaskWindow | None" = None
    display: TaskDisplay = field(default_factory=TaskDisplay)

    def clock_of(self) -> str:
        """内核那一型的钟面取值（``""`` ＝ 不上钟面）。换算**只此一处**。"""
        show = self.display.show
        return show if show in FACES else ""

    def shows_on_overlay(self) -> bool:
        """这一条要不要在浮层上出现（``show != none``）。内核那一份 dict 里对应「给不给
        窗口」—— 不给窗口的条目 ``_span_of`` 一律返回 ``None``，自然不上浮层。"""
        return self.display.show != SHOW_NONE

    def actions_of(self) -> "tuple[str, ...]":
        """内核那一型的动作名（``done`` / ``snooze``）。认不出的文案在解析期已经丢掉。

        **「不需要完成」的条目不给 ``done``**（``track_done=False``）：那枚按钮点下去
        只会记一笔、却哪儿都不显示（不进计数、不画状态点、行内也不画它），留着就是一格
        空转。``snooze`` 不受它管 —— 「稍后」换的是浮层上那一行的去留，与「完成」不是
        一回事（与行内、菜单两处的取舍同一条口径）。
        """
        return tuple(
            BUTTON_ACTIONS[b]
            for b in self.display.buttons
            if b in BUTTON_ACTIONS and (self.track_done or b != BUTTON_DONE)
        )


# ---------------------------------------------------------------------------
# 加载（整段形状 / 逐条语义）
# ---------------------------------------------------------------------------


def check_section(value: object) -> None:
    """整段 ``plugins.todo.tasks`` 的形状校验。

    :raises TaskSpecError: 形状不合法（调用方转 ``ConfigError``）。

    只判「哪条任务说的是哪一个」：顶层得是列表、每项得是映射、``id`` 得是非空且合法的
    标识符、且**不得重复**。其余一律交给 :func:`parse_all` 逐条跳过。
    """
    if value is None:
        return
    if not isinstance(value, (list, tuple)):
        raise TaskSpecError("tasks 必须是列表")
    seen: "set[str]" = set()
    for i, item in enumerate(value):
        where = f"tasks[{i}]"
        if not isinstance(item, dict):
            raise TaskSpecError(f"{where} 必须是 mapping")
        name = item.get("id")
        if not isinstance(name, str) or not name.strip():
            raise TaskSpecError(f"{where}.id 必须是非空字符串")
        if not _ID_RE.match(name.strip()):
            raise TaskSpecError(
                f"{where}.id 只能是字母 / 数字 / 下划线且不以数字开头（读到 {name!r}）"
            )
        if name.strip() in seen:
            raise TaskSpecError(f"{where}.id 与前面的重复：{name.strip()}")
        seen.add(name.strip())


def parse_all(value: object) -> "tuple[tuple[TaskSpec, ...], tuple[str, ...]]":
    """逐条解析任务；返回 ``(收下的任务, 跳过原因)``。**不抛异常**。"""
    if not isinstance(value, (list, tuple)):
        return (), ()
    specs: "list[TaskSpec]" = []
    problems: "list[str]" = []
    for i, raw in enumerate(value):
        spec, problem = _parse_one(raw, i)
        if spec is None:
            problems.append(problem or f"tasks[{i}] 解析不出来")
            continue
        specs.append(spec)
    return tuple(specs), tuple(problems)


def _parse_one(raw: object, index: int) -> "tuple[TaskSpec | None, str | None]":
    if not isinstance(raw, dict):
        return None, f"tasks[{index}] 不是 mapping"
    name = str(raw.get("id") or "").strip()
    if not name:
        return None, f"tasks[{index}] 缺少 id"
    label = name
    title = str(raw.get("title") or "").strip()
    if not title:
        # 不编一个中文名（与 ``variables`` 的 ``_name_of`` 同一条口径）：编出来的标题与
        # 用户在配置里写的那条对不上，界面上就成了另一样东西。
        return None, f"{label}：必须写 title（这一行显示什么）"

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        return None, f"{label}：enabled 得是布尔值"

    track_done = raw.get("track_done", True)
    if not isinstance(track_done, bool):
        return None, f"{label}：track_done 得是布尔值"

    conditions, problem = _conditions_of(raw.get("effective"), label)
    if conditions is None:
        return None, problem
    loop, problem = _loop_of(raw.get("effective"), label)
    if loop is None:
        return None, problem
    window, problem = _window_of(raw.get("effective"), label)
    if window is None and problem:
        return None, problem
    display, problem = _display_of(raw.get("display"), label)
    if display is None:
        return None, problem

    command = str(raw.get("command") or "").strip()
    command_at, problem = _command_at_of(raw, label)
    if command_at is None:
        return None, problem
    # 这一层**不判「切得开吗」**：那要 ``shlex``（一处实现，在 ``core.hotkey_command``），
    # 而这一层是纯声明/求值、零 I/O 零第三方。切不开的那一串（引号不配对）在真该跑的
    # 那一刻由 :mod:`.command` 报出来并记 WARNING —— 那是「必须出声」的位置；界面上
    # 同一件事挡在保存前（``detail_view`` 的校验门），于是两条路都有人管。
    return (
        TaskSpec(
            id=name,
            title=title,
            text=str(raw.get("text") or ""),
            enabled=enabled,
            track_done=track_done,
            command=command,
            command_at=command_at,
            conditions=conditions,
            loop=loop,
            window=window,
            display=display,
        ),
        None,
    )


def _command_at_of(raw: dict, label: str) -> "tuple[str | None, str | None]":
    """``command_at`` 四选一。旧盘上那枚 ``command_on_done`` **不参与折叠**。

    它当年表达的是「在『到点』之外**再**跑一次」；四选一里没有「两个点」这一档，而折叠成
    ``done`` 会把用户原本就有的「到点跑」整个换掉 —— 那是更重的静默改写。故取缺省
    ``reminder``，额外那一次不再有（见 :data:`COMMAND_ATS` 那条注释：这是简化的代价）。
    """
    value = raw.get("command_at")
    if value is None:
        return COMMAND_AT_REMINDER, None
    text = str(value).strip()
    if text not in COMMAND_ATS:
        return None, f"{label}：command_at 只能是 {' / '.join(COMMAND_ATS)}（读到 {text!r}）"
    return text, None


def _effective_of(raw: object, label: str) -> "tuple[dict | None, str | None]":
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return None, f"{label}：effective 得是一个 mapping"
    return raw, None


def _conditions_of(
    effective: object, label: str
) -> "tuple[tuple[rules.RuleCondition, ...] | None, str | None]":
    block, problem = _effective_of(effective, label)
    if block is None:
        return None, problem
    conditions, why = rules.parse_conditions(block.get("conditions"))
    if conditions is None:
        return None, f"{label}：effective.conditions —— {why}"
    return conditions, None


def _loop_of(effective: object, label: str) -> "tuple[TaskLoop | None, str | None]":
    block, problem = _effective_of(effective, label)
    if block is None:
        return None, problem
    raw = block.get("loop")
    if raw is None:
        return TaskLoop(), None
    if isinstance(raw, str):
        # 简写：``loop: daily``（与 ``when: startup`` 那种简写同款）。
        kind, every = raw.strip(), ""
    elif isinstance(raw, dict):
        kind = str(raw.get("kind") or "").strip()
        every = str(raw.get("every") or "").strip()
    else:
        return None, f"{label}：effective.loop 得是字符串或 mapping"
    if kind not in LOOPS:
        return None, f"{label}：effective.loop.kind 只能是 {' / '.join(LOOPS)}（读到 {kind!r}）"
    minutes = 0
    if kind == LOOP_INTERVAL:
        match = _EVERY_RE.match(every)
        if match is None:
            return None, f"{label}：循环任务必须写 every（形如 30m / 2h，读到 {every!r}）"
        count = int(match.group(1))
        if count <= 0:
            return None, f"{label}：every 得是正数（读到 {every!r}）"
        minutes = count * (60 if match.group(2) == "h" else 1)
    return TaskLoop(kind, every, minutes), None


def _window_of(effective: object, label: str) -> "tuple[TaskWindow | None, str | None]":
    """``effective.window``：``None`` ＝ **不限时段**（全天候）。

    两格**允许只填一端**：空着的那一头由 :func:`_inside_window` 放开到日的边界（「起」
    空 ⇒ 从零点起，「止」空 ⇒ 一直到当天结束）。这里不许再要求写全 —— 那样一条**时段
    没写全**的任务会被整条丢掉，用户看到的是它从清单里消失且**没有任何线索**（而 UI
    那两格的占位符明明写着「留空 ＝ 从零点起 / 到当天结束」）。

    两端都空 ⇒ 回 ``None``：与不写这一块**同形**，故所有 ``task.window is not None``
    的判据（摘要的时段那几个字、循环条的收工时刻）跟着一起不说话。

    第二项非空只表示**不合法**：解析失败时返回 ``(None, 原因)``，与「合法但没配」的
    ``(None, None)`` 靠**第二项**区分 —— 调用方判的是第二项。
    """
    block, problem = _effective_of(effective, label)
    if block is None:
        return None, problem
    raw = block.get("window")
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        return None, f"{label}：effective.window 得是一个 mapping"
    start = str(raw.get("from") or "").strip()
    end = str(raw.get("to") or "").strip()
    if not start and not end:
        return None, None
    return TaskWindow(start, end), None


def _display_of(raw: object, label: str) -> "tuple[TaskDisplay | None, str | None]":
    if raw is None:
        return TaskDisplay(), None
    if not isinstance(raw, dict):
        return None, f"{label}：display 得是一个 mapping"
    show, problem = _show_of(raw, label)
    if show is None:
        return None, problem

    reminder, problem = _reminder_of(raw.get("reminder"), label)
    if reminder is None:
        return None, problem

    buttons, problem = _buttons_of(raw.get("buttons"), label)
    if buttons is None:
        return None, problem

    lead, problem = _minutes_of(raw.get("lead_min"), label, "lead_min")
    if lead is None:
        return None, problem
    trail, problem = _minutes_of(raw.get("trail_min"), label, "trail_min")
    if trail is None:
        return None, problem

    return TaskDisplay(show, reminder, buttons, lead, trail), None


def _show_of(raw: dict, label: str) -> "tuple[str | None, str | None]":
    """``display.show`` 五选一；**读时折叠旧形状**（``overlay`` ＋ ``clock``）。

    2026-10-11 之前那两格只可能来自旧表单或旧迁移，折叠规则就是当时**实际生效**的语义：

    - ``overlay`` 不在 / 是假 ⇒ ``none``。那一格里就算写了 ``clock`` 也**从来没人读**
      （没有窗口就选不出钟面，见 ``SHOW_*`` 的说明）⇒ 丢掉它与「保留它」行为相同。
    - ``overlay`` 真 ⇒ ``clock`` 是三型之一就取它，否则 ``row``（只占一行，不上钟面）。

    **不重写盘上字节**：用户下次从界面保存这一条时才落成新形状。与「配置键 / 形状变化不
    迁移（判据：有没有人真手改过）」同一条口径 —— 这里也不需要迁移代码，因为折叠在读取
    那一处就完成了。旧 ``clock`` 的**坏值照样报出来**（静默降级成 ``row`` 会让用户以为
    自己配的那一档还在）。
    """
    value = raw.get("show")
    if value is not None:
        text = str(value).strip()
        if text not in SHOWS:
            return None, f"{label}：display.show 只能是 {' / '.join(SHOWS)}（读到 {text!r}）"
        return text, None

    overlay = raw.get("overlay", False)
    if not isinstance(overlay, bool):
        return None, f"{label}：display.overlay 得是布尔值"
    if not overlay:
        return SHOW_NONE, None
    clock = str(raw.get("clock") or "").strip()
    if clock in FACES:
        return clock, None
    if clock in ("", SHOW_NONE):
        return SHOW_ROW, None
    return None, (
        f"{label}：display.clock 只能是 {SHOW_NONE} / {' / '.join(FACES)}（读到 {clock!r}）"
    )


def show_of(display_raw: object) -> str:
    """一段 ``display`` 块此刻**实际生效**的显示档（读不出来按 ``none``）。

    给编辑屏用：它手里是盘上那一份**原始** dict，而这一格可能还写成旧的两键
    （``overlay`` / ``clock``）—— 直接读 ``raw["show"]`` 会把「时钟」显示成「不显示」，
    而用户什么都没改、按一次「确定」就真的把它改成不显示了（静默改写，正是这一格最初
    要避免的那类事故）。判据与解析层**同一份**（:func:`_show_of`），两处不会漂。

    读不出来（旧 ``clock`` 是个坏值）时回 ``none``：解析层那边已经把整条报成 problem、
    这一条本来就不生效，界面上再把它显示成一个「不显示」不会多损失什么；而给一个
    候选里没有的取值，``ChoiceBar`` 仍然会回落第一项 —— 绕一圈回到同一个地方。
    """
    if not isinstance(display_raw, dict):
        return SHOW_NONE
    show, _problem = _show_of(display_raw, "display")
    return show or SHOW_NONE


def loop_minutes_of(effective: object) -> int:
    """一段 ``effective`` 块里「循环」此刻**实际生效**的分钟数（读不出来按 ``0``）。

    与 :func:`show_of` 同一个理由，只是它防的错更贵：盘上那一块写的是
    ``{"kind": "interval", "every": "30m"}``（见编辑屏的写侧），而那一格原先按
    ``.get("minutes")`` 直读 —— 于是**永远**回填成「不循环」，用户什么都没改按一次
    「确定」就把这个块整份删掉，界面还显示「已保存」。判据与解析层**同一份**
    （:func:`_loop_of`），两处不会漂。
    """
    loop, _problem = _loop_of(effective, "effective")
    return 0 if loop is None else int(loop.minutes)


def window_of(effective_raw: object) -> "TaskWindow | None":
    """一段 ``effective`` 块里「生效时段」此刻**实际生效**的那一段（没有 ⇒ ``None``）。

    与 :func:`show_of` / :func:`loop_minutes_of` 同一个理由：编辑屏手里是盘上那一份
    **原始** dict，而「两端都空」与「不写这一块」在盘上同形（都回 ``None``，见
    :func:`_window_of`）—— 判据与解析层**同一份**，两处不会漂。
    """
    window, _problem = _window_of(effective_raw, "effective")
    return window


def _reminder_of(raw: object, label: str) -> "tuple[Reminder | None, str | None]":
    if raw is None:
        return Reminder(), None
    if isinstance(raw, str):
        raw = {"at": raw}
    if not isinstance(raw, dict):
        return None, f"{label}：display.reminder 得是 mapping 或一个时刻字符串"
    at = str(raw.get("at") or "").strip()
    if at and not _time_text_ok(at):
        return None, f"{label}：display.reminder.at 不是个时刻（{at!r}）"
    # 旧盘上的 ``card`` 键**不读也不报** —— 它是个已退场的档位（见 :class:`Reminder`），
    # 认不得的键当场报错会让所有老配置启动即整条不生效。
    return Reminder(at), None


def _buttons_of(raw: object, label: str) -> "tuple[tuple[str, ...] | None, str | None]":
    if raw is None:
        return (), None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return None, f"{label}：display.buttons 得是一个列表"
    out: "list[str]" = []
    for item in raw:
        name = str(item).strip()
        if name not in BUTTON_ACTIONS:
            return None, (
                f"{label}：display.buttons 只能是 "
                f"{' / '.join(BUTTON_ACTIONS)}（读到 {item!r}）"
            )
        if name not in out:
            out.append(name)
    return tuple(out), None


def _minutes_of(raw: object, label: str, key: str) -> "tuple[int | None, str | None]":
    if raw is None:
        return 0, None
    # ``bool`` 是 ``int`` 的子类 —— 别让 ``lead_min: true`` 变成「提前 1 分钟」。
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None, f"{label}：display.{key} 得是整数分钟"
    if raw < 0:
        return None, f"{label}：display.{key} 不能是负数（读到 {raw}）"
    return int(raw), None


def _time_text_ok(text: str) -> bool:
    """这串文本**有可能**是个时刻吗（不看变量值）。

    含 ``{`` 的一律放行 —— 那一路的值要等求值才知道（「严 ⊂ 宽」：校验侧不能比求值侧
    窄，否则 ``at: "{off_time} + 30m"`` 会被这里拒掉，而它是算得出来的）。不含变量的
    就当场试算一次，算不出来直接报 problem（``at: "25:99"`` 这种拼错要当场看得见，
    不能等运行期静默不提醒）。
    """
    if "{" in text:
        return True
    return expr.evaluate_or_none(text, lambda _name: None) is not None


# ---------------------------------------------------------------------------
# 求值（一条任务 ＋ 此刻的变量池 → 内核认的条目）
# ---------------------------------------------------------------------------


#: 「生效条件判不出来」那句话的**唯一一份文案**。谁决定要不要出声由调用方给（见
#: :func:`resolve_item` 的 ``on_blank``）—— 组件那边要按「判决翻转」去重，文案却只该有
#: 一处，否则两处各写一遍，改一处忘一处。
BLANK_CONDITION = "%s：生效条件判不出来（依赖的变量当刻没值），这一轮按不生效处理"


def report_blank(task_id: str) -> None:
    """把「这条任务的生效条件判不出来」说出来（默认的 ``on_blank``）。

    **要不要出声不在这里** —— 这里就是出声本身。周期型组件每拍都要重判一遍条件，
    每拍打一条会把日志刷满（``clk2`` 那种：引用的变量整天没值 ⇒ 一天十几万行），
    而这条日志又是「用户在界面上只看到它没出现」的唯一线索，不能删。故组件侧传一个
    **按判决翻转去重**的回调进来，本函数只管怎么把话说出来。
    """
    log.info(BLANK_CONDITION, task_id)


def resolve_item(
    task: TaskSpec, pool, now, *, on_blank: "Callable[[str], None] | None" = report_blank
) -> dict:
    """把一条任务算成**内核认的条目形状**（``scheduler`` 那一份 dict）。

    :param pool: 变量池（鸭子类型：只用 ``value`` / ``type_of`` / ``interpolate``）。
    :param now: 此刻（**必传**）。它只用来判 ``effective.window`` —— 判据的输入要显式，
        从池里摸一个私有属性出来的话，调用方换了池的实现（或换了一轮）就静默变样。
    :param on_blank: 条件**判不出来**时怎么出声；缺省＝直接打一条 INFO（一次性调用方，
        如编辑屏的预览，一次只说一次，没有去重的必要）。周期型调用方传一个带状态的
        回调进来，把「每拍都打」压成「判决翻转才打」。

    ``enabled`` 这一格是**四个判据的合取**（都是「不显示」）：用户停用、条件不通过、
    不在生效时段内、以及——都没问题时才为真。合成一格是有意的：``_merge`` 只认这一格，
    多分一格出来它也不看。

    条件**判不出来**（依赖的变量当刻没值）按不通过处理并出声：与 §3.1「解析失败 ⇒
    引用它的任务按条件不满足处理」同一条口径。出声是把这句话说出来的唯一地方 ——
    不然用户在界面上只看到「它没出现」。
    """
    verdict = rules.matches_all(task.conditions, pool.value, pool.type_of)
    if verdict is None:
        on_blank(task.id)
    ok = task.enabled and verdict is True
    if ok and task.window is not None:
        ok = _inside_window(task.window, pool, now)

    item = {
        "id": task.id,
        "title": task.title,
        "text": pool.interpolate(task.text),
        "enabled": ok,
        "face": task.clock_of(),
        "actions": task.actions_of(),
        # 场景门禁不再是这一格的事（``_scene_allows`` 读它）：新模型里「只在办公室」
        # 写成 ``conditions`` 的一条，由上面那个合取判掉。留着空串是为了内核那边
        # 那条判据原样跑（空 ⇒ 不限）。
        "scene": "",
        "at": "",
        "end": "",
        "before_min": 0,
        "interval_min": 0,
    }
    if not task.shows_on_overlay():
        # 不上浮层（§4.4 的「只列不弹」）：**不给窗口**——``_span_of`` 见到三个键
        # 都空就返回 ``None``，这一条自然不上浮层。它在主窗体清单里的存在、
        # 以及它那条命令（按 ``command_at`` 在时刻上跑）都不受影响。
        return item

    at_s = resolve_time(task.display.reminder.at, pool)
    trail_s = int(task.display.trail_min) * 60
    if task.loop.kind == LOOP_INTERVAL and task.loop.minutes > 0:
        item["interval_min"] = task.loop.minutes
        # 循环条的 ``end`` 在旧内核里是「今天循环到几点为止」（不是退场时刻）：
        # 新模型里那个量正好是 ``window.to``。
        end_s = resolve_time(task.window.end, pool) if task.window is not None else None
        item["end"] = fmt_hhmm(end_s) if end_s is not None else ""
    elif at_s is not None:
        item["at"] = fmt_hhmm(at_s)
        # 退场 = 提醒时刻 ＋ 延迟退出分钟（§4.3：窗口 = [at − lead, at + trail]）。
        # 没写 ``trail_min`` ⇒ 退场就是提醒时刻那一刻（与旧「推算下班」那一支同形）。
        item["end"] = fmt_hhmm(at_s + trail_s)
    item["before_min"] = int(task.display.lead_min)
    return item


def resolve_time(raw: object, pool) -> "int | None":
    """字面 / ``{var}`` / 表达式 → **当日秒数**；算不出 ⇒ ``None``。

    三步，顺序不能换：先插值（``{var}`` → 值），再交给表达式引擎（它本来就认字面
    ``17:27``，故两条路合成一条），最后要 ``time`` 型。

    ``{var}`` 当刻没有值 ⇒ 池的 :meth:`interpolate` **原样保留**那片花括号 ⇒ 引擎
    报语法错 ⇒ 这里回 ``None``（任务此刻「没有提醒时刻」）。这与「静默当成 0 点」是
    两回事：后者会在凌晨把用户叫起来。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        text = pool.interpolate(text)
    except Exception as e:  # noqa: BLE001 —— 插值失败只该让这一条没有时刻
        log.warning("插值失败（%s）：%s", raw, e)
        return None
    found = expr.evaluate_or_none(text, pool.value)
    if found is None or found.type != expr.T_TIME:
        return None
    return int(found.raw) % expr.DAY_S


def command_due_s(task: TaskSpec, pool) -> "int | None":
    """这一条任务按 ``command_at`` **该在当日的哪一秒**跑命令；算不出来 ⇒ ``None``。

    三个「时刻」触发点都由提醒时间与它前后那两个偏移量算出来（窗口
    ``[at − lead_min, at + trail_min]``）：``reminder`` 是 ``at`` 本身、``lead`` 是窗口
    起点、``trail`` 是终点。``done`` 不是时刻（由用户那一按决定）⇒ 同样回 ``None``。

    ``at`` 没填、或它引用的变量当刻还没有值 ⇒ 三个时刻一个都算不出来（与「没有提醒时刻
    就没有它前后那两个偏移量」同一条口径，见 ``detail_view._sync_task_enabled``）。
    """
    if task.command_at == COMMAND_AT_DONE:
        return None
    at_s = resolve_time(task.display.reminder.at, pool)
    if at_s is None:
        return None
    if task.command_at == COMMAND_AT_LEAD:
        return (at_s - int(task.display.lead_min) * 60) % expr.DAY_S
    if task.command_at == COMMAND_AT_TRAIL:
        return (at_s + int(task.display.trail_min) * 60) % expr.DAY_S
    return at_s


def _inside_window(window: TaskWindow, pool, now) -> bool:
    """此刻在不在生效时段内。

    **两端各管各的**：空着的（或引用了当刻没有值的变量的）那一头**不拦**，只由填了的那一
    头收边。「任一端算不出来 ⇒ 整个时段放开」是错的：那样「只填了止 18:00」会退化成全天
    候 —— 用户在那一格里明明写了字、却不生效，且没有任何线索说明它为什么没生效。

    分端处理的成本为零：留空与「变量当刻没值」走到同一个 ``None``（``resolve_time`` 两边
    统一），于是两种情形同形 —— 都不拿那一头设限。这与原先那条「宁可放行也别让任务凭空
    消失」是同一个方向，只是不连带把另一端的要求一起放掉。
    """
    if now is None:
        return True
    now_s = now.hour * 3600 + now.minute * 60 + now.second
    start = resolve_time(window.start, pool)
    end = resolve_time(window.end, pool)
    if start is None and end is None:
        return True
    if start is None:
        return now_s <= end
    if end is None:
        return now_s >= start
    if start <= end:
        return start <= now_s <= end
    # 跨午夜（如 22:00 → 02:00）：两端之间那一段**之外**才在窗口里。
    return now_s >= start or now_s <= end


def loop_allows(task: TaskSpec, now) -> bool:
    """今天这个 ``loop`` 允许它出现吗（每周一~五那一档）。**与 window 无关**。

    ``interval`` 与 ``daily`` 恒真：循环是「今天响几次」的节奏，不是「今天有没有」。
    """
    if task.loop.kind != LOOP_WEEKDAY:
        return True
    if now is None:
        return True
    return now.weekday() in WEEKDAYS


def referenced_variables(task: TaskSpec) -> "tuple[str, ...]":
    """这一条任务引用到的变量 id（去重；顺序＝**字段顺序**，每个字段内按出现顺序）。

    不是「全局首次出现」的顺序 —— 那要跨六个字段记位置，而这条顺序只服务一件事：主窗体
    那条变量条按什么次序列。字段顺序（``text`` → ``command`` → ``reminder.at`` →
    ``window.from`` → ``window.to`` → ``conditions``）对用户本来就是可预期的。

    三处要看它：主窗体那条变量条（列哪些变量）、Phase 4 的迁移（该建哪些变量）、以及
    「变量变了要重算什么」的传播点。分头扫的结果是某一处漏掉一类字段（``window.from``
    最容易被忘），而症状是「改了那个变量这一条没跟着变」—— 界面上没有线索指向配置。

    认名字这件事**只有一处实现**（``pool.text_variables``，与插值共用同一正则）：
    这里再写一条的结果是两处悄悄漂开，而症状是「池替它插了值，而这里漏算了依赖」。
    """
    out: "list[str]" = []

    def _take(text: object) -> None:
        for name in text_variables(str(text or "")):
            if name not in out:
                out.append(name)

    _take(task.text)
    _take(task.command)          # 与 ``text`` 同一类：先插值、再拿结果去用
    _take(task.display.reminder.at)
    if task.window is not None:
        _take(task.window.start)
        _take(task.window.end)
    for cond in task.conditions:
        if cond.var not in out:
            out.append(cond.var)
    return tuple(out)


def command_of(task: TaskSpec, pool) -> str:
    """这一条此刻该跑的**那一串字**（``{变量}`` 已插值）。没有命令 ⇒ 空串。

    走 ``pool.interpolate``，与 :func:`resolve_item` 里 ``text`` 那一格**同一个口**：
    各写一遍的下场是「显示文本里的 ``{date}`` 会变、命令里的不会」—— 而后者看起来像
    「这条命令没生效」，没有任何线索指向「那串字根本没被插值」。

    ``pool`` **必传**（鸭子类型：只用 ``interpolate``）：调用方（``Plugin._fire_command``）
    在池缺席时**不跑**这条命令 —— 新模型没有池本来就整个不成立（规则一条都登不上），
    在这里猜一份「原样拿去跑」只会把装配问题变成一条跑错参数的命令。
    """
    if not task.command:
        return ""
    return pool.interpolate(task.command)


def status_of(state: dict) -> str:
    """三条事实 → 状态点三态。**唯一**的换算处。

    优先级：完成 > 过期 > 未到。两处各判一遍的话，「已过期但今天补做了」那一条在面板
    与清单里会得出两个颜色（而它们看着都合理）。
    """
    if state.get("done"):
        return STATUS_DONE
    if state.get("overdue"):
        return STATUS_OVERDUE
    return STATUS_PENDING


def _window_span(window: "TaskWindow", pool) -> str:
    """时段两端**求得出**的那几个字（不含「生效」二字）：``09:00–18:00`` / ``09:00 起`` /
    ``18:00 前`` / ``22:00–次日 02:00``；两端都求不出 ⇒ ``待定``。

    与 :func:`_inside_window` 的「两端各管各的」同一条口径：空着的（或引用的变量当刻
    没值的）那一头不写进词里 —— 写成「09:00–18:00」会让用户以为没填的那一头也有个值。
    """
    start = resolve_time(window.start, pool) if pool is not None else None
    end = resolve_time(window.end, pool) if pool is not None else None
    if start is None and end is None:
        return "待定"
    if start is None:
        return f"{fmt_hhmm(end)} 前"
    if end is None:
        return f"{fmt_hhmm(start)} 起"
    if start > end:
        return f"{fmt_hhmm(start)}–次日 {fmt_hhmm(end)}"
    return f"{fmt_hhmm(start)}–{fmt_hhmm(end)}"


def window_word(window: "TaskWindow | None", pool) -> str:
    """生效时段的**用户话**（清单行摘要用）；``window is None`` ⇒ 空串。

    形态就是 :func:`_window_span` 加两个字：``09:00–18:00 生效`` / ``09:00 起生效`` /
    ``18:00 前生效``；两端都求不出 ⇒ ``生效时段待定``（与摘要里「提醒时刻待定」同款）。

    从前这里写的是「限时段」两个字 —— 配了 09:00–18:00 的任务在清单上读不出时段本身，
    用户既看不出它限了什么，也看不出它有没有起作用。
    """
    if window is None:
        return ""
    span = _window_span(window, pool)
    if span == "待定":
        return "生效时段待定"
    if span.endswith(("起", "前")):
        return f"{span}生效"      # 「09:00 起生效」/「18:00 前生效」
    return f"{span} 生效"         # 「09:00–18:00 生效」


def window_status(window: "TaskWindow | None", pool, now) -> str:
    """生效时段**此刻**算不算数的一句话（编辑屏「生效时段」两格下面那行状态）。

    「它有什么用」只在「此刻生不生效」上体现得出来 —— 配了 09:00–18:00 的用户在
    晚上打开编辑屏，从前界面上没有任何字告诉他这条现在不作数。三种回答：

    - 两端引用的变量当刻都没值 ⇒ ``生效时段待定——引用的变量此刻没有值``；
    - 此刻在时段内 ⇒ ``此刻在时段内（09:00–18:00 生效）``；
    - 不在 ⇒ ``此刻不在时段内（09:00–18:00 生效，现在 21:03）``。

    **两种情形都把时段本身写出来**：两格填的是 ``{off_time}`` 时，用户在这扇窗里
    读不到那个变量当刻算成了几点（清单行的摘要里有，但他正盯着这扇窗）。只报「现在
    几点」的话，他既不知道时段是什么，也不知道为什么此刻不作数。判据是
    :func:`_inside_window`、时段那几个字是 :func:`window_word` —— 两者都是内核那一份，
    **不在这里再判一遍再渲染一遍**：清单说不生效、编辑屏说生效那种不一致不报错，
    而用户只能看到其中一处。
    """
    if window is None:
        return ""
    word = window_word(window, pool)
    if word == "生效时段待定":
        return "生效时段待定——引用的变量此刻没有值"
    if not _inside_window(window, pool, now):
        return f"此刻不在时段内（{word}，现在 {now:%H:%M}）"
    return f"此刻在时段内（{word}）"


def summary_of(task: TaskSpec, pool) -> str:
    """主窗体那一行的第二行：这一条**怎么响**（设计文档 §4.5.③）。

    与旧模型 ``scheduler.row_summary`` 是同一件事（那个随旧条目表一起退场）：列表上读得
    出一条任务叫什么、几点，却读不出它**怎么响** —— 而后者正是用户点进去之前最想知道的。

    顺序按「它会怎么打扰我」排：先时刻（它决定其余各项在不在），再生效条件，最后是显示
    面。**空的那几项一个都不提** —— 摘要里写「不提前」等于给每一行加一句废话。
    """
    parts: "list[str]" = []
    if not task.enabled:
        parts.append("已停用")
    if task.loop.kind == LOOP_INTERVAL and task.loop.minutes > 0:
        parts.append(f"每 {task.loop.minutes} 分钟")
    else:
        at_s = resolve_time(task.display.reminder.at, pool) if pool is not None else None
        if at_s is not None:
            parts.append(f"{fmt_hhmm(at_s)} 提醒")
        elif task.display.reminder.at:
            parts.append("提醒时刻待定")      # 引用了变量，而它当刻还没有值
        else:
            parts.append("不提醒")
    if task.loop.kind == LOOP_WEEKDAY:
        parts.append("工作日")
    if task.window is not None:
        parts.append(window_word(task.window, pool))
    for cond in task.conditions:
        parts.append(condition_word(cond))
    if task.display.show == SHOW_NONE:
        parts.append("只列清单")
    elif task.display.show in FACES:
        parts.append("上钟面")
    return " · ".join(parts)


def condition_word(cond: "rules.RuleCondition") -> str:
    """一条生效条件的一句话。场景那一条说得最省（``仅办公室``）—— 它是最常见的一种，
    而 ``scene == 办公室`` 这种写法在列表上读起来像配置而不是需求。"""
    if cond.var == "scene" and cond.op == "==":
        return f"仅{cond.value}"
    if cond.op == "in":
        values = cond.value if isinstance(cond.value, (list, tuple)) else [cond.value]
        return f"{cond.var} ∈ {'/'.join(str(v) for v in values)}"
    return f"{cond.var} {cond.op} {cond.value}"


def button_of(action: str) -> str:
    """内核动作名 → 配置面文案（:meth:`TaskDisplay.actions_of` 的反向）；``""`` ＝ 认不出。

    反向表就从 :data:`BUTTON_ACTIONS` 那一份里查 —— 另写一张的后果是「编辑屏存进去的
    文案」与「迁移搬过来的文案」各认一半，而两处都不报错。
    """
    for label, name in BUTTON_ACTIONS.items():
        if name == action:
            return label
    return ""
