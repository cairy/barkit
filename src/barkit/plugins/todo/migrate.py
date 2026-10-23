"""todo/migrate：两代一次性迁移（都是**纯映射**：零 Qt、零盘、零配置）。

- **第一代**（下面这一段）：合并前那套配置 → 合并后的条目表（``migrate_items``）；
- **第二代**（文件末尾那一大段）：旧条目表 → 新模型的「顶层变量 ＋ 任务表」
  （``migrate_to_tasks``，设计文档 §5）。两代串着跑，一台从两代之前升上来的机器也能
  一次到位。

合并方案 §6.1：``plugins.clock.entries[]``（旧四型）＋ 旧 ``plugins.todo.items[]``
合成一张表。规则写在 §2.3「旧四种条目怎么落位」那张表里，本模块只是把它写成代码：

| 旧 ``type`` | 新条目 |
|---|---|
| ``fixed`` | 字面 ``at`` ＋ 字面 ``end``（``end`` = ``time ＋ after``），钟面 ``clock`` |
| ``loop`` | ``interval_min``（循环条的 ``at`` 是算出来的整点，没有字面值），钟面 ``clock`` |
| ``pomodoro`` | ``interval_min`` = 工作 ＋ 休息；``mode="timer"`` 上倒计时钟面，否则上实时钟 |
| ``derived``（写死上班时间） | 普通倒计时条目：``at`` = ``end`` = **下班时刻**（上班 ＋ 时长） |
| ``derived``（每天问我） | ``work_hours`` ＋ ``ask="time"``（**没答不登场**，§2.3） |

前四行的 ``ask`` 一律是**空串**（只开显示面）：旧 ``clock`` 只给「``derived`` ＋
``work_start_source == "ask"``」那一条登记询问，别的条目**从来没弹过任何东西**。新模型
里 ``ask`` 缺省按 ``choice`` 走（那是对旧 ``todo`` 条目的行为保持），照搬过来会让每条
报时条目从迁移那天起每天准点弹一个「完成」框。旧 ``ask_scene`` 搬成 ``scene`` —— 它在
旧内核里同样既管询问又管**显示**（``clock/scheduler.py::_scene_allows``），两处同义。

**它不认识盘**：读旧段、写新段、删旧段都由 ``plugin.py`` 做（那是配置门面的事）。
本模块只回答「这两张表合成什么样」。

三处**有损**（旧模型表达得出来、新模型的字段表 §2.1 里没有对应格子），如实列在这里：

- **秒级余量**：旧条目能写「提前 30 秒 / 延后 10 秒出现」，新模型只有分钟粒度。折算
  规则一律朝「窗口更大」的方向取整（提前量向上凑整、退场时刻向上凑整），宁可多显示
  一会儿，也不要让一条提醒变成只闪一瞬。旧秒值不在结果里，读不回来。
- **循环条的 ``after``**：旧 ``loop`` 每轮从「到点」起挂 N 秒；新模型的循环条是
  **首尾相接**（每轮挂满一个间隔，见 ``scheduler._span_of``），没有「只闪一下」这个
  自由度 —— 旧 ``after_*`` 丢掉。
- **``derived`` 的 ``after``**：旧窗口是 ``[下班 － 提前量, 下班 ＋ 延后]``；新模型里
  「推算下班」的退场**就是下班那一刻**（§2.3：登场 = 下班 － 提前量、退场 = 下班），
  且这与「每天问我」那一支同形（``_span_of`` 的 ``work_hours`` 分支给 ``(下班, 下班)``，
  那里根本表达不出延后）—— 两支要么都丢、要么分叉，取前者。提前量一字不丢。
- **询问的预填偏好**：旧 ``ask_initial`` / ``ask_initial_offset``（「预填现在 / 上次填的
  / 现在偏移」）在新模型里没有格子，丢掉。新口径是「今天已经填过的优先，没有就用此刻」
  （``plugin._initial_of``）—— 它服务的是「回来改」这个更常见的场景。
"""

from __future__ import annotations

from dataclasses import dataclass

from barkit.core.interaction import PROMPT_CHOICE, PROMPT_TIME
from barkit.core.variables import expr
from barkit.plugins.todo import task as task_mod
from barkit.plugins.todo.scheduler import (
    DAY_END_S,
    DAY_S,
    FACE_CLOCK,
    FACE_COUNTDOWN,
    FALLBACK_TITLE,
    _actions_of,
    _ask_of,
    _hhmm_to_sec,
    _valid_hhmm,
    _work_hours_of,
    fmt_hhmm,
    item_defaults,
)

#: 旧组件的组件 id —— 它的配置段（``plugins.<这个>``）合并后整段删（方案 §6.1）。
#: 数据侧两份旧文件的名字在 ``day_log`` 里（那一半归它）。
LEGACY_PLUGIN_ID = "clock"

#: 旧文本里可用的三个占位符（与 ``scheduler`` 同一份清单，那边是写死的那三个字面量）。
_PLACEHOLDERS = ("{到点}", "{现在}", "{剩余}")


def migrate_items(current, legacy_entries) -> "list[dict]":
    """两张表合成一张。

    :param current: 现在盘上的 ``items``（缺 ``ask`` 的会补显式 ``choice``，方案 §6.1）。
    :param legacy_entries: 旧 ``plugins.clock.entries``；``None`` / 不是列表 ⇒ 只做补
        ``ask`` 那一半（没有旧条目可搬，或那份配置本来就没写过）。
    :return: 合并后的整张表。

    **幂等靠「id 已在表里就跳过」**（方案 §6.1 那条「id 冲突时给 clock 的条目改名」
    在这一版收口成了它）：两个来源——「上一次迁移已经搬过这一条」与「用户自己就有一条
    同 id 的」——在盘上**分不开**（新形状里没有「这条是从 clock 来的」这种痕迹），
    而两件事要的动作相反。取「跳过」是因为它在两种情形下都安全：重跑不再多出一条，
    罕见的手写撞名也只是少搬一条、不会凭空多出一条（多出来的那条用户得自己认出来删）。

    两个组件的 id 前缀本来就不同（``clk`` / ``td``），所以首次迁移几乎不会撞；
    真正会撞的只有「两次写盘之间断掉」那种重跑（写自己段与删旧段不是一个事务）。
    """
    items = [dict(i) for i in current if isinstance(i, dict)]
    for item in items:
        # 显式写出来只为「不再走隐式分支」：`_ask_of` 对缺键本来就按 choice 走
        # （批 1 有等价性判据钉着），补与不补行为相同。
        item.setdefault("ask", PROMPT_CHOICE)
    taken = {str(i.get("id") or "") for i in items}
    added: "list[dict]" = []
    for raw in legacy_entries if isinstance(legacy_entries, list) else []:
        # 撞车（已经搬过 / 用户自己占了同名 id）由 ``_item_from_entry`` 一处判掉 ——
        # 这里再判一遍是**同一个判定的第二份**，删掉它没有任何行为差别（门禁自证实测：
        # 摘掉外层这道，用例照绿 ⇒ 它守不住任何东西）。
        item = _item_from_entry(raw, taken)
        if item is None:
            continue
        taken.add(item["id"])
        added.append(item)
    return items + added


def _item_from_entry(entry: object, taken: set) -> "dict | None":
    """一条旧 ``clock`` 条目 → 新条目；``None`` ＝ 这条搬不过来（不进新表）。"""
    if not isinstance(entry, dict):
        return None
    eid = str(entry.get("id") or "").strip()
    if not eid or eid in taken:
        # **两道都在这一处判**：没有 id 的条目在新模型里连窗口都算不出来
        # （``_entry_windows`` 头一件事就是查 id）；``eid in taken`` 挡的是「已经搬过
        # 的那条」（写自己段与删旧段不是一个事务，中途断掉会重跑）与「用户自己就占了
        # 那个 id」—— 两种来源在盘上分不开（新形状里没有「这条是从 clock 来的」这种
        # 痕迹），而取「跳过」在两种情形下都安全。
        return None
    item = item_defaults()
    item["id"] = eid
    # **旧 ``clock`` 的条目一条都不问**，只有下面 ``derived`` 那一支会把它改成 ``time``。
    # 旧内核只给「``type == "derived"`` 且 ``work_start_source == "ask"``」登记询问
    # （``clock/plugin.py::_sync_interaction`` 那两条 ``continue``），其余全是纯报时的
    # 显示条目。若照 ``item_defaults`` 的 ``choice`` 落下去，用户盘上那几条报时条目会从
    # 迁移那天起每天准点弹一个「完成」框 —— 那是迁移凭空造出来的行为，用户没提过这个要求。
    item["ask"] = ""
    item["enabled"] = bool(entry.get("enabled", True))
    item["title"], item["text"] = _split_text(entry.get("text"))
    item["scene"] = str(entry.get("ask_scene") or "").strip()
    # 提前量：秒一律向上凑整到分钟（宁可早出现，也不要晚出现）。
    item["before_min"] = _ceil_minutes(_before_s(entry))

    etype = str(entry.get("type") or "fixed").strip()
    if etype == "loop":
        step = int(entry.get("interval_min") or 0)
        if step <= 0:
            return None  # 没有间隔的循环条永远不登场（旧内核同样给空窗口）
        item["interval_min"] = step
        item["face"] = FACE_CLOCK
        return item
    if etype == "pomodoro":
        work = int(entry.get("work_min") or 0)
        if work <= 0:
            return None
        item["interval_min"] = work + max(0, int(entry.get("break_min") or 0))
        item["face"] = (
            FACE_COUNTDOWN if str(entry.get("mode") or "timer") == "timer" else FACE_CLOCK
        )
        return item
    if etype == "derived":
        return _derived_item(entry, item)

    # fixed（以及认不出的 type：旧内核也只认那四种，认不出的一律不给窗口）
    at = str(entry.get("time") or "").strip()
    if not _valid_hhmm(at):
        return None
    item["at"] = at
    item["end"] = _plus_minutes(at, _ceil_minutes(_after_s(entry)))
    item["face"] = FACE_CLOCK
    return item


def _derived_item(entry: dict, item: dict) -> "dict | None":
    """「推算下班」那一支 —— 两支各落一处（方案 §2.3 / §8）。"""
    hours = entry.get("work_hours")
    if not isinstance(hours, (int, float)) or isinstance(hours, bool) or hours <= 0:
        return None
    item["face"] = FACE_COUNTDOWN
    if str(entry.get("work_start_source") or "fixed").strip() == "ask":
        # 「每天问我」：只留时长 ＋ 问面。旧那个「没答时用的兜底上班时间」**丢掉** ——
        # 方案 §8 定的是「不答就不启动」（留着它，没答的那天照样会按兜底值出倒计时）。
        item["work_hours"] = hours
        item["ask"] = PROMPT_TIME
        return item
    # 「固定时间」那一支 ＝ 一条普通倒计时条目（字面 at ＋ 字面 end），不再单列。
    # **窗口以下班时刻为中心**（``at = end = 下班``，提前量另算）：旧内核算的是
    # ``target = 上班 ＋ 时长`` 再取 ``[target - before, target + after]``
    # （``clock/scheduler.py::_entry_windows``），而 ``{到点}`` 在旧模型里报的也是
    # **下班时刻**。把 ``at`` 落成上班时刻会让这一行写成「下班时间09:00」，且窗口
    # 从上班一直挂到下班 —— 与上面「每天问我」那一支（``_span_of`` 的 ``work_hours``
    # 分支给 ``(下班, 下班)``）同一个形状才对：同一条「推算下班」，问不问不该改变它
    # 长什么样。
    start = str(entry.get("work_start") or "").strip()
    if not _valid_hhmm(start):
        return None
    off_s = int(float(hours) * 3600)
    base = _hhmm_to_sec(start)
    if base is None:
        return None
    item["at"] = item["end"] = fmt_hhmm(base + off_s)
    return item


def _split_text(raw: object) -> "tuple[str, str]":
    """旧的一栏 ``text`` → 新的 ``(title, text)`` 两栏（方案 §2.1）。

    旧的显示文本只有一栏（可以带 ``{到点}`` 这类占位符），新的分两栏：``title`` 是**行
    文字**、``text`` 是行上的**模板**（占位符照渲染，见 ``scheduler.render_text``）。拆法：

    - **带占位符** ⇒ 两栏都给：``title`` 取去掉占位符的那半（说不出具体时刻的那半），
      ``text`` 原样保留 ⇒ 渲染出来的时刻一个不少；
    - **不带** ⇒ 只给 ``title``：两栏都给会让同一句话在浮层上出现两遍。

    认不出的占位符（用户拼错的）原样留在 ``title`` 里 —— 与内核「认不出就照原样显示」
    同一条口径，用户一眼看得出自己写错了。
    """
    text = str(raw or "").strip()
    if not text:
        return "", ""
    head = text
    for token in _PLACEHOLDERS:
        head = head.replace(token, "")
    head = head.strip()
    if head == text:
        return text, ""
    return head, text


def _before_s(entry: dict) -> int:
    return int(entry.get("before_min") or 0) * 60 + int(entry.get("before_sec") or 0)


def _after_s(entry: dict) -> int:
    return int(entry.get("after_min") or 0) * 60 + int(entry.get("after_sec") or 0)


def _ceil_minutes(seconds: int) -> int:
    """秒数向上凑整到分钟（``0`` ⇒ ``0``，不凭空造出一分钟的窗口）。"""
    seconds = int(seconds)
    return 0 if seconds <= 0 else -(-seconds // 60)


def _plus_minutes(at: str, minutes: int) -> str:
    """``"HH:MM"`` ＋ 分钟 → ``"HH:MM"``（跨午夜按 24 小时取模，与 ``fmt_hhmm`` 同口径）。"""
    base = _hhmm_to_sec(at)
    if base is None:
        return ""
    return fmt_hhmm(base + int(minutes) * 60)


# ---------------------------------------------------------------------------
# 第二代：旧条目表 → 新「变量 ＋ 任务」（设计文档 §5）
# ---------------------------------------------------------------------------
#
# 与上一代同一条纪律：**它不认识盘**（不读配置、不写文件），只回答「这张表翻成什么」；
# 谁写盘、什么时候写由 ``plugin`` 做（``Plugin._migrate_to_tasks``）。
#
# 逐条**行为守恒**（§5 最后那一条）落在四件事上：
#
# - **条目 id 一个字不动** ⇒ 「今天已完成」的记录、以及面板上那份按 id 的账天然接得上
#   （与上一代迁移同一条理由：id 一改，用户看到的是「迁移完今天的完成记录没了」）；
# - **登场点 / 退场点**：旧内核那三条窗口算法（:func:`_span_of`：循环 > 作答时长 >
#   字面时刻）一条不漏，只是改写成新模型的 ``effective.loop`` / ``window`` 与
#   ``display.reminder`` / ``lead_min`` / ``trail_min``；
# - **弹卡**：旧 ``ask == choice`` 那一档（到点弹一枚「完成」，``daily_at`` ＋
#   ``once_per_day``）**整条链路已退场**（2026-10-11）⇒ 迁移不再产出对应的字段。
#   它原来管的那件事现在由浮层行那枚「完成」按钮或清单右键菜单承担（``display.buttons``）；
# - **询问**：旧 ``ask == time`` 那一支（启动时问、答过就不再问）在新模型里**不属于任何
#   任务**，而是**变量的触发条件** —— 这正是整个重构的要点（问一次、处处用）。
#
# 四处**有损**（旧模型表达得出来、新模型的字段表里没有对应格子），每一条都记进
# :attr:`TaskMigration.notes` 与日志（用户该知道的那一份），不静默略过：
#
# - **空 ``end``**（旧：一直显示到当天末 23:59:59）：新模型只有相对量 ``trail_min``，
#   按分钟折算成「退场 23:59」。差不到一分钟，落在当天最后一分钟内，用户看不见；
# - **秒级余量**：新模型的时间分辨率是**分钟**（旧窗口是秒级）⇒ 派生变量按分钟写
#   （``work_hours`` 是浮点小时，如 7.5 ⇒ ``start_time + 450m``），不足一分钟的部分丢掉。
#   **折算必须照抄内核那一步**（``int(h * 3600) // 60``，见 :func:`_off_declaration`）——
#   就地写 ``int(h * 60)`` 会在 ``8.7`` 这类值上差一分钟（浮点误差方向恰好反着）；
# - **场景不一致的共享询问**：多条 ``ask=time`` 条目写的是不同场景时，共享变量只能有一个
#   触发条件 ⇒ **去掉场景条件**并出声（宁可多问一次，也不要静默地只在某一个场景问，
#   那会让另一条从迁移那天起再也不问）；
# - **「问了却没有下家」的条目**：``ask=time`` 而 ``work_hours`` 没填（或 ``work_hours``
#   填了而问法不是问时刻）—— 那一格在旧内核里要用当日值算窗口，而那样的条目本来就没有
#   当日值、一次都没显示过。两种都出声。

#: 共享的「上班时间」（§5 那个例子）。**ask 型**：旧 ``work_start_source == "ask"``
#: 那一支的询问搬到了它身上。
VAR_START = "start_time"

#: 派生的「下班时间」前缀。只有一种 ``work_hours`` 时用这个裸名，多于一种时按「几小时」
#: 各建一个（``off_time_9`` / ``off_time_10``）—— §5 那条「不要硬塞成单一共享 off_time，
#: 那会丢失差异」。
VAR_OFF = "off_time"

#: 场景变量：旧条目的 ``scene`` 一格（「只在某个网络里显示 / 询问」）在新模型里**只能**
#: 由变量条件表达。
VAR_SCENE = "scene"

#: ``effective.window`` 的起点。旧模型没有「生效起点」这个概念（循环条的 ``end`` 说的是
#: 「今天循环到几点为止」）⇒ 起点只能补。补零点是**显式**，留空按 §4.2 也是同一个结果
#: （起那格空 ⇒ 从零点起）—— 写出来只是让迁移后的任务表读得出「它从几点开始算」。
WINDOW_FROM = "00:00"

#: 场景变量的**出厂声明**（``config.default.yaml`` 里那一条），照抄在这里只为「用户的
#: ``variables:`` 段没把它抄过去时补上一条」这一个场景。两份字面量不许漂 ——
#: ``tests/test_todo_migrate.py`` 有一条对拍拿 ``config.default.yaml`` 钉着它。
SCENE_DECLARATION: "dict" = {
    "id": VAR_SCENE,
    "name": "当前场景",
    "source": "component",
    "component": "route_guard",
    "key": "profile",
    "field": "summary",
}

#: 共享询问变量的显示名与问句。**写死、不取旧条目的标题**（2026-10-10 修）：
#: 这一条问的是**上班时刻**，而旧条目十有八九叫「下班提醒 / 下班时间」（那正是它提醒的
#: 那件事）—— 拿标题当名字，界面上就会出现一条「下班时间」的**询问**变量、而真正由它
#: 算出来的派生变量也叫「下班时间」，用户看到两条同名、且该答的那条挂着错的语义
#: （用户原话：「询问变量应该是『上班时间』，而不是『下班时间』」）。
ASK_NAME = "上班时间"
ASK_PROMPT = "今天几点上班？"


@dataclass(frozen=True)
class TaskMigration:
    """一次「旧条目表 → 新模型」的结果（**纯数据**：盘与配置都不在这里碰）。

    - :attr:`variables`：要**新增**进顶层 ``variables:`` 段的声明（已经扣掉撞名的那些）；
    - :attr:`tasks`：整张新任务表（与旧条目表一一对应、id 不变）；
    - :attr:`answer_moves`：``(旧条目 id, 新变量 id)``——「今天填过的那一笔」要搬的那几对
      （谁去读那笔值、搬完要不要清掉旧账，由 ``plugin`` 决定）；
    - :attr:`notes`：用户该知道的那些话（丢了什么、在哪里退让了一步），逐条都是一句
      完整的中文，调用方只负责记日志 / 显示。
    """

    variables: tuple = ()
    tasks: tuple = ()
    answer_moves: tuple = ()
    notes: tuple = ()


def migrate_to_tasks(items, declared=()) -> TaskMigration:
    """旧条目表 → 新模型（§5 那张表逐行）。

    :param items: 盘上那份 ``items``（旧模型的十三格）。非 mapping 的项跳过（与内核
        「认不出的条目不给窗口」同一条口径）。
    :param declared: 顶层 ``variables:`` 段的**原样内容**（``config.variables()`` 那份）。
        这里只从里面取 id：撞名时不新增、直接引用已有那一条 —— ``variables`` 段里 id
        重复会让整份配置被拒（``variables.spec.check_section``），而用户自己声明过的
        那一条就是他要的那个（两种来源在盘上分不开，与上一代迁移的「id 已在表里就跳过」
        同一条理由）。撞名只记一条 note，不当失败：任务照样引用它。
    """
    rows = (
        [dict(i) for i in items if isinstance(i, dict)]
        if isinstance(items, (list, tuple))
        else []
    )
    taken = _ids_of(declared)
    notes: "list[str]" = []

    # 第一遍：算出共享变量要建哪几个。旧的 ``ask=time`` ＋ ``work_hours`` 那一支是**唯一**
    # 需要变量的地方 —— 只有它拿「今天填的时刻」去算窗口。
    asked = [r for r in rows if _ask_of(r) == PROMPT_TIME and _work_hours_of(r) > 0]
    hours_order: "list[float]" = []
    for row in asked:
        hours = _work_hours_of(row)
        if hours not in hours_order:
            hours_order.append(hours)
    # §5：多条 ``ask=time`` 的 ``work_hours`` 不同 ⇒ 按不同值各建一个派生变量；只有一种
    # 值时才用裸名 ``off_time``（那正是 §5 举的例子）。**先数一遍再决定名字**，是为了让
    # 名字与条目的先后顺序无关（否则「谁排第一」会决定谁拿到裸名）。
    multi = len(hours_order) > 1
    var_of_hours = {
        h: (f"{VAR_OFF}_{_hours_key(h)}" if multi else VAR_OFF) for h in hours_order
    }

    wanted: "list[dict]" = []
    if asked:
        wanted.append(_start_declaration(asked, notes))
    for hours in hours_order:
        wanted.append(_off_declaration(var_of_hours[hours], hours))
    # 场景门禁在新模型里只能由变量条件表达（旧模型直接从总线上的副标题取，不依赖变量表）。
    # 而用户那份 ``variables:`` 段是**整段覆盖**合并的 —— 没把出厂那条 ``scene`` 抄过去
    # 就没有它，缺一条声明不会报错，症状是「凡判场景的任务从此恒不生效」⇒ 需要时补上它。
    if any(str(r.get("scene") or "").strip() for r in rows):
        wanted.append(dict(SCENE_DECLARATION))

    variables: "list[dict]" = []
    for decl in wanted:
        if decl["id"] in taken:
            notes.append(f"变量 {decl['id']} 已经声明过了，直接引用它（不覆盖）")
            continue
        taken.add(decl["id"])
        variables.append(decl)

    tasks = [_task_of(row, var_of_hours, notes) for row in rows]
    # 要搬的是**答案**（旧账里那一笔是「今天几点上班」），落到那个 **ask** 变量上 ——
    # 派生变量是算出来的，往它身上写值只会被下一次求值覆盖掉。
    moves = tuple((str(r.get("id") or "").strip(), VAR_START) for r in asked)
    return TaskMigration(tuple(variables), tuple(tasks), moves, tuple(notes))


def _ids_of(declared) -> "set[str]":
    """``variables:`` 段里已经存在的 id（段已过 :func:`spec.check_section`：每项都是
    mapping 且 ``id`` 非空 ⇒ 这里与变量池读出来的是同一批 id）。"""
    out: "set[str]" = set()
    for item in declared if isinstance(declared, (list, tuple)) else []:
        if isinstance(item, dict):
            name = str(item.get("id") or "").strip()
            if name:
                out.add(name)
    return out


def _start_declaration(asked: "list[dict]", notes: "list[str]") -> dict:
    """共享的「上班时间」那一格（ask 型）：旧 ``ask=time`` 的询问搬到它身上。

    触发条件照旧模型那一支：**程序启动时问一次、答过今天就不再问**
    （``COND_ON_START`` ＋ ``until_answered_day`` ⇒ ``when: [startup]`` ＋
    ``only_if_unfilled``；去重档由变量池按 ask 变量固定给 ``DEDUPE_ANSWERED_DAY``）。

    名字与问句取 :data:`ASK_NAME` / :data:`ASK_PROMPT` 两个常量，**不看旧条目的标题** ——
    理由见那两个常量的注释（标题说的是「这条任务提醒什么」，不是「它要问哪个量」）。
    """
    scenes: "list[str]" = []
    for row in asked:
        scene = str(row.get("scene") or "").strip()
        if scene and scene not in scenes:
            scenes.append(scene)
    trigger: "dict" = {"when": ["startup"], "only_if_unfilled": True}
    if len(scenes) == 1:
        trigger["conditions"] = [{"var": VAR_SCENE, "op": "==", "value": scenes[0]}]
    elif scenes:
        # 共享变量只能有一个触发条件。取其中一条都会让另一条**静默地**不再问，故两条都
        # 不取并出声 —— 这条文案是「迁移做了什么」的唯一说明，别压短。
        notes.append(
            "旧配置里这几条要问时刻的条目分别只在 "
            + " / ".join(scenes)
            + " 才问，而共享的询问变量只能有一个触发条件 ⇒ 已去掉场景条件（不再只在某个"
            "场景里问）。要按场景分开，请在「设置 → 变量」里另建一条询问变量，并把相应"
            "任务的提醒时刻改成引用它"
        )
    return {
        "id": VAR_START,
        "name": ASK_NAME,
        "source": "ask",
        "type": expr.T_TIME,
        "trigger": trigger,
        "prompt": ASK_PROMPT,
    }


def _off_declaration(var_id: str, hours: float) -> dict:
    """派生的「下班时间」：``start_time ＋ N 分钟``（§5 那个 ``start_time + 9h``）。

    折算成**分钟**而不是小时，是因为 ``work_hours`` 允许小数（旧编辑器那一格是一位小数，
    ``DecimalField``）。而分钟数必须**照抄旧内核那一步**：

        off_s = start_s + int(hours * 3600)        # scheduler._span_of
        minutes = int(hours * 3600) // 60

    不能就地写 ``int(hours * 60)`` —— 两条式子在浮点下**不总是相等**，差的那几个值恰好是
    用户真会填的（实测 0.05~16.00 步长 0.01 里有三个：``4.35 / 8.7 / 8.95``）。以 ``8.7``
    为例：``8.7 * 3600`` 落在 31319.999999999996 上 ⇒ 521 分（内核报 17:41），而
    ``8.7 * 60`` 落在 522.0 上 ⇒ 522 分（报 17:42）—— 下班时刻**晚一分钟**，而且只在这
    几个值上晚，用户看到的是「填 8.7 那天下班晚了一分钟」这种查不出来的现象。
    """
    minutes = int(hours * 3600) // 60
    name = "下班时间" if var_id == VAR_OFF else f"下班时间（{hours:g} 小时后）"
    return {
        "id": var_id,
        "name": name,
        "source": "derived",
        "type": expr.T_TIME,
        "expr": f"{VAR_START} + {minutes}m",
    }


def _task_of(row: dict, var_of_hours: dict, notes: "list[str]") -> dict:
    """一条旧条目 → 一条新任务。

    字段的落位照 §5 那张表；窗口那三条算法照旧内核 :func:`_span_of` 的**优先级**
    （循环 > 作答时长 > 字面时刻）—— 优先级搬错的表现是「同一条条目换个形状出现」，
    而两边的配置都看着合理。
    """
    iid = str(row.get("id") or "").strip()
    task: "dict" = {
        "id": iid,
        # 标题**必填**（新模型那一条任务总要在清单里有个名字）。旧条目可以不写标题，
        # 那时内核显示的是 ``FALLBACK_TITLE`` —— 这里就落它，而不是留空：
        # 留空的那条会被新模型整条跳过（「必须写 title」），用户看到的是「迁移完这条没了」。
        "title": str(row.get("title") or "").strip() or FALLBACK_TITLE,
        "text": str(row.get("text") or ""),
    }
    if not bool(row.get("enabled", True)):
        # 等于默认值的那一格**不写**（``enabled`` 缺省为真）：落盘那份因此只剩差异项，
        # 与上述「等于默认就删键」同一口径。
        task["enabled"] = False

    scene = str(row.get("scene") or "").strip()
    effective: "dict" = {}
    if scene:
        effective["conditions"] = [{"var": VAR_SCENE, "op": "==", "value": scene}]

    display: "dict" = {}
    interval = _int_of(row.get("interval_min"))
    hours = _work_hours_of(row)
    ask = _ask_of(row)
    at = str(row.get("at") or "").strip()
    at_s = _hhmm_to_sec(at)
    before = _int_of(row.get("before_min"))

    if interval > 0:
        # 循环条：旧内核里它的登场是「此刻所在的间隔整点」、退场是下一轮（首尾相接），
        # 而 ``end`` 说的是「今天循环到几点为止」—— 那个量正好是新模型的 ``window.to``。
        effective["loop"] = {"kind": task_mod.LOOP_INTERVAL, "every": f"{interval}m"}
        stop = str(row.get("end") or "").strip()
        if _valid_hhmm(stop):
            effective["window"] = {"from": WINDOW_FROM, "to": stop}
        display["show"] = task_mod.SHOW_ROW
    elif hours > 0:
        # 「作答 ＋ N 小时」那一支：新模型里提醒时刻**引用派生变量**（问一次、多条任务
        # 处处用），退场就是那一刻（旧内核给 ``(下班, 下班)``）⇒ ``trail_min`` 留空即 0。
        # **只有真的在问时刻**（``ask == time``）才算得出窗口 —— 它的起点是当日值，而
        # 当日值只有这一种条目会有。别的问法配了 ``work_hours`` 是旧设置面「切问法时只灰
        # 不收」留下的残值，旧内核里它也一次都没显示过（见下面那条 note）。
        if ask == PROMPT_TIME:
            display["show"] = task_mod.SHOW_ROW
            display["reminder"] = {"at": "{" + var_of_hours[hours] + "}"}
    elif at_s is not None:
        display["show"] = task_mod.SHOW_ROW
        display["reminder"] = {"at": at}
    else:
        # 旧内核里它一个窗口都算不出来（没有循环、没有作答时长、``at`` 也不合法）⇒
        # 一次都没在浮层上出现过。新模型照搬成「只列不弹」（主窗体清单里还在，与旧详情
        # 页那行「未设时间」同一个位置）。
        pass

    if hours > 0 and ask != PROMPT_TIME:
        notes.append(
            f"{iid or '（没有 id）'}：旧配置里填了「作答后几小时」而问法不是「问一个时刻」"
            f"（{ask or '不问'}）—— 那一格要用当日值算窗口，而这样的条目没有询问、也就"
            "从来没有当日值，一次都没显示过。这一格没有搬"
        )
    if ask == PROMPT_TIME and hours <= 0:
        notes.append(
            f"{iid or '（没有 id）'}：旧配置里它会问一个时刻却没有填「作答后几小时」，"
            "答案本来就没有下家 ⇒ 新模型不建这条询问"
        )

    if display.get("show"):
        if before > 0:
            display["lead_min"] = before
        trail = _trail_minutes(interval, hours, at_s, row)
        if trail:
            display["trail_min"] = trail

    face = str(row.get("face") or "").strip()
    if face in task_mod.FACES:
        # 旧「钟面」那一格并进 ``show``（旧内核里它本来就寄生在浮层行上：没有窗口
        # 就选不出钟面 ⇒ 单独写 ``clock`` 而 ``overlay`` 假的条目一次都没显示过）。
        display["show"] = face

    # 行内那两枚按钮：旧 ``actions`` 是内核的动作名，新模型写中文（``BUTTON_ACTIONS``）。
    # **空表就空着**，不补一枚「完成」—— 旧条目的按钮由 ``actions`` 管，没有就是没有，
    # 补上等于用户按一次「确定」就凭空长出一枚（与上一代迁移里那条理由一字不差）。
    buttons = [task_mod.button_of(a) for a in _actions_of(row)]
    buttons = [b for b in buttons if b]
    if buttons:
        display["buttons"] = buttons

    # 旧模型里「问 ``choice`` 的条目到点弹一枚『完成』」这一档（``daily_at`` ＋
    # ``once_per_day``）整条链路已退场（见 ``task.Reminder``）⇒ 这里不再产出任何
    # 对应的键。它的「完成」现在由浮层行那枚按钮或右键菜单承担，与 ``buttons`` 同一条路。
    if effective:
        task["effective"] = effective
    if display:
        task["display"] = display
    return task


def _trail_minutes(interval: int, hours: float, at_s: "int | None", row: dict) -> int:
    """``trail_min``：退场时刻 − 提醒时刻（分钟）。循环条与推算法各自另有归宿 ⇒ 0。

    旧模型的退场取三处之一：循环条由内核按间隔算、推算法是下班那一刻、字面时刻那条取
    字面 ``end``（**空则挂到当天末 23:59:59**）。第三种要折算成分钟，跨午夜时按
    :data:`DAY_S` 那个圆取模（与内核 :func:`_inside_window` 同口径）。
    """
    if interval > 0 or hours > 0 or at_s is None:
        return 0
    end_s = _hhmm_to_sec(str(row.get("end") or "").strip())
    if end_s is None:
        return (DAY_END_S - at_s) // 60
    return ((end_s - at_s) % DAY_S) // 60


def _int_of(value: object) -> int:
    """旧模型那几格裸整数（``interval_min`` / ``before_min``）。

    **宽容**：读不出来按 0。旧内核那边是 ``int(entry.get(...) or 0)``（手写一个
    ``interval_min: "半小时"`` 会让它当场抛），而迁移这一侧不该被一条坏值拉停 ——
    整张表搬不动比那一格按 0 算糟得多。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0
    try:
        return int(float(str(value).strip()))
    except ValueError:
        return 0


def _hours_key(hours: float) -> str:
    """``work_hours`` → 派生变量名里那一截（``9`` / ``7_5``）。

    ``%g``（六位有效数字）：``9`` 与 ``9.0`` 都得 ``9``（同一个量不该有两个名字），
    ``7.5`` 得 ``7.5`` ⇒ 换成合法标识符就是把点换成下划线。第七位才不同的两个值会并成
    一个 —— 那点差在毫秒级，不值得为它立个更丑的名字。
    """
    return f"{float(hours):g}".replace(".", "_")


def repair_ask_labels(declarations: "list[dict]") -> "tuple[list[dict], list[str]]":
    """把第一版迁移写错的那两格修回来（2026-10-10 用户报；**只认精确的错误产物**）。

    第一版迁移把旧条目的标题当共享询问变量的显示名与问句（``label = title or …``），
    于是 ``start_time`` 这条 **上班时间** 的询问变量在盘上顶着「下班时间」的名字、
    弹着「下班时间？」的问句。修名字那版改掉的只是**今后**的迁移 —— 已经迁过的盘
    ``tasks`` 非空、幂等门直接返回，错的那两格会一直留在盘上 ⇒ 需要这一步定点修复。

    **只动两种精确形状**（多一点都不碰 —— 用户手写的名字与问句不是这里的辖域）：
    ``id == start_time`` 且 ``prompt == "下班时间？"`` ⇒ 问句改回 :data:`ASK_PROMPT`；
    ``id == start_time`` 且 ``name == "下班时间"`` ⇒ 名字改回 :data:`ASK_NAME`。

    纯函数：**返回新列表**（声明字典也是浅拷贝后改），调用方比对后自行落盘。
    """
    out: "list[dict]" = []
    notes: "list[str]" = []
    for decl in declarations:
        row = dict(decl)
        if row.get("id") == VAR_START and row.get("source") == "ask":
            if row.get("prompt") == "下班时间？":
                row["prompt"] = ASK_PROMPT
                notes.append("询问变量的问句从迁移错写的「下班时间？」改回"
                             f"「{ASK_PROMPT}」（名字与问句本该问的是上班时间）")
            if row.get("name") == "下班时间":
                row["name"] = ASK_NAME
                notes.append("询问变量的名字从迁移错写的「下班时间」改回"
                             f"「{ASK_NAME}」（它问的是上班时间；下班时间由它推算）")
        out.append(row)
    return out, notes
