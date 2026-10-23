"""todo/scheduler：待办提醒的**纯调度内核**（无 Qt 依赖、零读盘，可独立单测）。

合并组件只有**一种条目**，一条条目按需要开三样东西（合并方案 §1.2）：问面 ``ask``、
显示面、钟面 ``face``。**问面那条线归框架的询问中枢**（``core/interaction.py`` ＋
``shell/interaction.py``），不在本模块；本模块只算后两样。

- **显示面**：一条条目在 ``at``（可提前 ``before_min``）登场、在 ``end`` 退场；
  ``end`` 留空就是挂到当天末，跨天归零（§3.2）。填了 ``work_hours`` 的（问 ``time``
  型，如「推算下班」）退场 = **当日值 ＋ N 小时**，且**没有当日值就不登场** ——
  不答就不启动，兜底值已在方案里砍掉（§2.3）。
- **钟面**：``clock``（此刻 HH:MM:SS）/ ``countdown``（距退场还剩 MM:SS）/
  ``countup``（自登场已过 MM:SS）。多条同时命中**只出一面钟**（倒计时 > 正计时 > 时钟），
  其余条目只贡献行（§2.2）。
- **条目行**：一条一行（状态点 ＋ 标题 ＋ 右侧时刻 ＋ 可选动作）。旧翻页钟那一版是
  ``texts: list[str]`` 的纯字符串 —— 没 id、没状态、点不了，装不下一条待办（§3）。
  超出行数上限（设置页 ``max_rows``）就只显示前 N 条 ＋ 一行「还有 k 条」，浮层不许
  一路长到屏幕外面去。

纯函数是刻意的：**读盘在 ``plugin.py``、画在 ``clock_view.py``**。跨线程那两处
（``tick()`` 在 worker、浮层在主线程）各自持有的东西也不在这里 —— 本模块每次调用
重算全部结论，不认识「上一次」。

时间一律以「当日秒数」为基准（:data:`DAY_S` 那个圆），窗口可以跨午夜。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Iterable

from barkit.core.interaction import PROMPT_CHOICE, PROMPT_TIME

#: 一天的秒数。当日秒数在 ``0..86399`` 这个**圆**上转：目标时刻可能落在明天
#: （夜班 17:45 + 9h = 次日 02:45），而 ``_seconds_since_midnight`` 只给今天的秒数。
#: 文案侧 :func:`fmt_hhmm` 按这个圆取模，窗口判定必须跟它同一口径。
DAY_S = 86_400

#: 「当天末」取 23:59:59 而不是 86400：窗口判定是**闭区间**（``_inside_window`` 里的
#: ``<=``），取 86400 会让「没填退场时刻」的条目在**零点那一秒**又落回窗口里
#: （``(0 - show_at) % DAY_S`` 恰好等于 window span）⇒ 浮层闪一下再消失。
DAY_END_S = DAY_S - 1

#: 一分钟的秒数。**窗口判定按分钟做**（见 :func:`_inside_window`）：显示面的三个时刻
#: （提醒 / 提前登场 / 延后退出）全是分钟粒度，而组件一拍 60s ⇒ 判定必须与拍同粒度，
#: 否则「这一拍落在窗口里没有」取决于拍撞上的相位，同一个窗口时有时无。
MINUTE_S = 60

#: 钟面三型（合并方案 §2.2）。``face`` 留空 ⇒ 这一条**只列行、不占钟面**。
FACE_CLOCK = "clock"
FACE_COUNTDOWN = "countdown"
FACE_COUNTUP = "countup"
FACES: "tuple[str, ...]" = (FACE_CLOCK, FACE_COUNTDOWN, FACE_COUNTUP)

#: 一行的两个可选动作（§3.3）。**按条目配**（``actions``）：不配就不画那枚按钮 ——
#: 画一个点了没用的按钮是骗人。
ACTION_DONE = "done"
ACTION_SNOOZE = "snooze"
ACTIONS: "tuple[str, ...]" = (ACTION_DONE, ACTION_SNOOZE)

#: 浮层最多显示几行（设置页那一格 ``max_rows`` 的默认值）。到上限之后
#: 只显示前 N 条 ＋ 一行「还有 k 条」。
DEFAULT_MAX_ROWS = 10

#: 条目没填标题时的兜底文案。—— 空标题在浮层上是一行什么都没有的空行，用户不知道
#: 这条要做什么。**问面那一侧也用它**（``plugin._prompt_spec``），故只此一份。
FALLBACK_TITLE = "该做这件事了"

#: 「这一条怎么问」的**短名**。两个消费方共用这一份（详情页列表行那句摘要、编辑屏那排
#: 单选按钮）—— 同一个概念两处各写一遍，改一处必有一处对不上。
#: 取值域 ＝ :func:`_ask_of` 的三个可能返回值，别在这里另立第四个键。
#:
#: 措辞按「用户会做什么」写，不按机制写（2026-10-09 用户裁定「简单好理解」）：
#: ``choice`` 是弹一枚「完成」按钮让他点，``time`` 是弹一格让他填时刻，空串＝什么都不弹。
#: 展开说那几句**不写进标签**，挂在每枚按钮的悬停提示上（``detail_view._ASK_HINTS``）。
#:
#: ``time`` 那枚**只能说「程序启动时」**：它的触发条件是 ``on_start``（见
#: ``plugin._conditions_of``），全程没有 ``daily_at`` —— 写「到点」就是在描述一条
#: 内核里不存在的路（2026-10-09 用户读出来的，同一批改掉标签与悬停释义）。
ASK_WORDS: "dict[str, str]" = {
    PROMPT_CHOICE: "到点弹「完成」",
    PROMPT_TIME: "程序启动时问我",
    "": "不弹提醒",
}

#: 「钟面」的**短名**（同上）。空 ＝ 不占钟面，那一档在摘要里干脆不提。
FACE_WORDS: "dict[str, str]" = {
    FACE_CLOCK: "时钟",
    FACE_COUNTDOWN: "倒计时",
    FACE_COUNTUP: "正计时",
}

#: 「测试这一条」给**未保存**的条目补的假 id。内核按 id 去重 / 排序，空 id 的条目
#: 连窗口都算不出来（``_entry_windows`` 头一件就是 ``if not eid``）⇒ 预览必须先补上它。
#: 用一个不可能与真条目撞车的字符串（真 id 形如 ``td1``）。
TEST_ENTRY_ID = "__test__"

def item_defaults() -> "dict[str, object]":
    """一条条目**什么都没说**时，每个字段各是什么（方案 §2.1 的十三格全给）。

    值与内核各处的缺省**逐条对齐**（:func:`_ask_of` 认 ``choice``、:func:`_actions_of`
    对缺键回空、:func:`_face_of` 认空串）—— 换句话说，它描述的是「这条目什么都没配时
    会怎样」，不是「新建一条时该预填什么」。要预填什么由调用方在这个返回值上改。

    两个消费方都要它（**只此一份**）：
    - 详情页把老条目补成编辑屏的完整形状（``detail_view._normalize``）—— 列表里的字典
      没有各自的 ``FieldSpec``，没有别的地方替它补默认，少补一个键那一格就读不到值；
    - 迁移把旧 ``clock`` 条目翻成新形状（``migrate.py``）—— 两处各写一遍，加一个字段
      就会漏一处，而漏的那处**不报错**（静默读到空）。

    ``actions`` 给**空表**而不是 ``("done",)``：老条目在浮层上本来就没有「完成」那枚
    按钮，补成 ``("done",)`` 等于用户按一次「确定」就凭空长出一枚（见
    ``detail_view._new_item`` 为何要在它之上改这一格）。
    """
    return {
        "id": "",
        "enabled": True,
        "title": "",
        "text": "",
        "at": "",
        "end": "",
        "before_min": 0,
        "interval_min": 0,
        "work_hours": 0,
        "face": "",
        "actions": [],
        "ask": PROMPT_CHOICE,
        "scene": "",
    }


#: 「显示文本」里可用的三个占位符（设置页那一格下面写着同一份清单，改动要同步）。
#: 认不出的占位符**原样保留** —— 这是刻意的，见 :func:`render_text`。
PLACEHOLDER_AT = "{到点}"
PLACEHOLDER_NOW = "{现在}"
PLACEHOLDER_LEFT = "{剩余}"

#: 钟面优先级：多条同时命中时只出一面钟。数值只在 ``_clock_of`` 内部比大小，
#: **不当契约外传** —— 旧翻页钟那一版留过一对 ``_PRIO_*`` 常量，全仓零读者，只会让人
#: 以为还有第二条判据。
_FACE_PRIO = {FACE_CLOCK: 0, FACE_COUNTUP: 1, FACE_COUNTDOWN: 2}


# ---------------------------------------------------------------------------
# 时间与文案（纯函数；浮层在两次 tick 之间自己往后续算，也走这里同一份格式）
# ---------------------------------------------------------------------------


def fmt_mmss(total_s: int) -> str:
    """秒 → ``MM:SS``（分钟可超过 99，长倒计时不会触顶）。

    **公开**：运行期面在两次 ``tick`` 之间自己往后续算剩余 / 已过秒，必须产出同一种
    文本 —— 格式只留这一处，两侧分头写就会在位数 / 补零上分叉。
    """
    total_s = max(0, int(total_s))
    m, s = divmod(total_s, 60)
    return f"{m:02d}:{s:02d}"


def fmt_hhmm(total_s: int) -> str:
    """当日秒数 → ``HH:MM``（跨午夜按 24 小时取模）。"""
    total_s = int(total_s) % DAY_S
    return f"{total_s // 3600:02d}:{(total_s % 3600) // 60:02d}"


def _hhmm_to_sec(value: object) -> "int | None":
    """``"HH:MM"`` → 当日秒数；非法 / 空返回 ``None``。

    两种写法都认：``"%H:%M"``（含**单位数小时** ``"9:30"`` —— 编辑器放行的那种）与
    ``time.fromisoformat`` 那一套（更宽，带秒也认）。

    **读侧必须比编辑器宽**，方向不能反：读侧比校验窄的后果是 ``"9:30"`` 这种「设置页
    存得进去」的值在内核里算不出窗口 —— 条目在设置页看着是好的，实际永远不登场
    （2026-10-08 实测；旧模型不拿它算窗口，故这个缺口是合并模型才暴露的）。见
    :func:`_valid_hhmm`。
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        t = datetime.strptime(text, "%H:%M").time()
    except ValueError:
        try:
            t = time.fromisoformat(text)
        except ValueError:
            return None
    return t.hour * 3600 + t.minute * 60 + t.second


def _valid_hhmm(value: object) -> bool:
    """``"HH:MM"`` 合法性 —— **编辑器按它把关**（详情页那几个输入框）。

    **比 :func:`_hhmm_to_sec` 严**（只认 ``"%H:%M"``，不认带秒的那种）：严 ⊂ 宽 保证
    「编辑器放行的输入，内核一定读得出来」。方向反过来就是上面说的哑状态 —— 存得进去、
    却算什么都不发生。

    它不属于本组件（询问中枢求值 ``daily_at`` 时还会再判一次，非法值按「不成立」处理）。
    放在这里是因为**编辑器与内核读的是同一批字面值**：两份口径迟早分叉，而分叉的表现
    就是上面那种哑状态。
    """
    if not isinstance(value, str):
        return False
    try:
        datetime.strptime(value.strip(), "%H:%M")
    except ValueError:
        return False
    return True


def _seconds_since_midnight(dt: datetime) -> int:
    return dt.hour * 3600 + dt.minute * 60 + dt.second


def _inside_window(now_s: int, show_at: int, dismiss_at: int) -> bool:
    """当日秒数这个圆上：``now_s`` 是否落在 ``[show_at, dismiss_at]`` 内（可跨午夜）。

    **按分钟判**（2026-10-11）：``now_s`` 先下取整到它所在那一分钟的起点，窗口长度不足
    一分钟时补到 59 秒。两条理由，缺一条都会露出来：

    - 组件一拍 **60s**，而窗口两端本来就都是分钟粒度（``at`` 是 ``HH:MM``、``lead`` /
      ``trail`` 是分钟、``end`` 由 ``fmt_hhmm`` 回来）—— 按秒判的话「这一拍落在窗口里
      没有」取决于拍撞上哪个相位，同一个窗口有时看得到、有时整整错过一拍；按分钟判则
      **同一分钟内任何一拍答案相同**，与拍无关。
    - ``lead = trail = 0``（**默认配置**）的窗口是 ``[at, at]`` 一个**零长**区间，按秒判
      只有零点那一秒为真 ⇒ 60 秒一拍基本必然错过它。补到 59 秒 ⇒ 它覆盖 ``at`` 那一整
      分钟，「到点该出现」与「下一分钟该退场」都落得下来。

    拿 ``show_at`` 当地轴原点、只**正着**数：窗口跨午夜时（``dismiss_at < show_at``）
    长度是负的，折回同一圈就变成正确的那个正数；窗口长过一整天时 ``span`` 恒大于任何
    前进量 ⇒ 全天都算窗口，那是配置本就荒唐时的退化方向。
    """
    span = dismiss_at - show_at
    if span < 0:
        span += DAY_S
    if span < MINUTE_S:
        span = MINUTE_S - 1
    return (now_s - now_s % MINUTE_S - show_at) % DAY_S <= span


def render_text(
    text: str,
    now: datetime,
    at_s: "int | None" = None,
    remaining_s: "int | None" = None,
) -> str:
    """把行文本里的占位符换成**这一条本次算出来的值**；认不出的占位符**照原样**。

    三个占位符：``{到点}`` 这一条算出来的时刻（固定条＝目标时刻、循环条＝本轮整点、
    推算下班＝下班时刻）、``{现在}`` 此刻的 ``HH:MM``、``{剩余}`` 倒计时剩余。

    **说不出来的那一项原样保留**：拼错的 ``{下班}`` 会连着花括号显示在浮层上 ——
    用户一眼看得出自己写错了，比静默吞掉（显示成空白）好得多。
    """
    if not text:
        return text
    out = text
    if PLACEHOLDER_NOW in out:
        out = out.replace(PLACEHOLDER_NOW, now.strftime("%H:%M"))
    if at_s is not None and PLACEHOLDER_AT in out:
        out = out.replace(PLACEHOLDER_AT, fmt_hhmm(at_s))
    if remaining_s is not None and PLACEHOLDER_LEFT in out:
        out = out.replace(PLACEHOLDER_LEFT, fmt_mmss(remaining_s))
    return out


# ---------------------------------------------------------------------------
# 显示态
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """浮层上的一行（一条此刻在自己时间里的条目）。

    ``frozen`` 是刻意的：它要**跨线程**（worker 算出、主线程画），也要能被
    ``==`` 比 —— 浮层靠这个比对跳过「内容没变」的重建（每秒重排一次会看得见的抖）。
    """

    entry_id: str
    title: str
    text: str          # 渲染后的文本；空 ⇒ 这一行只显示 title
    time_label: str    # 右侧时刻
    actions: tuple     # ("done",) / ("snooze",) / () —— 空 ⇒ 这行不可操作（实心点）
    at_s: int          # 登场时刻（当日秒数）：排序键，也决定浮层上从上到下的次序


@dataclass
class DisplayState:
    """合并后的整屏显示态（多条重叠也只产一份）。"""

    show: bool
    face: str                      # 三型之一；无条目开 face 时也走 ``clock``（实时钟）
    value: str                     # "HH:MM:SS" / "MM:SS"
    rows: "list[Row]"              # 已按行数上限截断
    more_text: str                 # 超上限那一行「还有 k 条」；空 ⇒ 不显示这一行
    size: str
    active_ids: "frozenset[str]"   # 此刻在时间里的**全部**条目 id（含被折叠的那些）
    #: 倒计时 / 正计时**此刻**的秒数（未格式化，供运行期面在两次 tick 之间自行续算）。
    #: 对面那一型为 ``None``；格式化只走 :func:`fmt_mmss`，两边同源。
    countdown_s: "int | None" = None
    countup_s: "int | None" = None


@dataclass
class _Window:
    """一条条目在 ``now_s`` 此刻的激活态（内部用，出模块前收成 :class:`Row`）。"""

    entry_id: str
    title: str
    text: str
    time_label: str
    actions: tuple
    face: str
    at_s: int
    end_s: int


# ---------------------------------------------------------------------------
# 条目 → 窗口
# ---------------------------------------------------------------------------


def _ask_of(entry: dict) -> str:
    """这一条问什么：``PROMPT_CHOICE`` / ``PROMPT_TIME``，或**空串 ＝ 不问**。

    **键不在与键在且空是两回事**（本仓的无值两态口径）：

    - ``ask`` 键**不在** ⇒ 旧条目（合并迁移之前的那批），按 ``choice`` 走 —— 缺省即
      它们原来的问法，这是**行为保持**的关键，换一个默认值会让所有老条目的弹窗变样；
    - 键在且为**空串** ⇒ 这一条不问（只开显示面的条目）；
    - 不认识的值 ⇒ 回落到 ``choice``：宁可问错一种，也不要静默变成「不问」——
      后者在设置页看着是好的，实际永远不弹。

    放在本模块（而不是 ``plugin`` 里）是因为它读的**就是**条目自己的字段，与
    :func:`_face_of` / :func:`_actions_of` / :func:`_scene_allows` 同一类。写两份的
    后果很具体：条目字段的读法一旦分家，「迁移搬到新模型的那一条问不问」与「迁移前
    它问不问」会给出两个答案，而两边都不报错。
    """
    if "ask" not in entry:
        return PROMPT_CHOICE
    value = str(entry.get("ask") or "").strip()
    if not value:
        return ""
    return value if value in (PROMPT_CHOICE, PROMPT_TIME) else PROMPT_CHOICE


def _face_of(entry: dict) -> str:
    """这一条要不要占钟面、占哪一型。**认不出的值按「不上钟面」处理**。

    「认不出」不回落成 ``clock``：那会让一个拼错的 ``countdown`` 静默变成一面实时钟
    （看着像用户配错了），而「只列行」至少是这条条目本来就允许的一种形态（§1.2）。
    取值域由编辑器把关（``choices``），内核不猜。
    """
    value = str(entry.get("face") or "").strip()
    return value if value in FACES else ""


def _actions_of(entry: dict) -> tuple:
    """这一行画哪几枚动作按钮；认不出的动作丢掉，空 ⇒ 这行不可操作。"""
    raw = entry.get("actions")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(a for a in (str(x).strip() for x in raw) if a in ACTIONS)


def _scene_allows(entry: dict, scene: "str | None") -> bool:
    """场景门禁：这一条此刻该不该显示。

    ``scene`` 说的是「这一条属于哪个网络」；**没填 ⇒ 不限**（与加门禁之前一字不差），
    填了 ⇒ 只有此刻命中的场景与它**同名**才放行。「还没读到」判成不放行是有意的：
    宁可晚一拍出现，也不要在认不出的网络里弹一条本来只该在公司出现的提醒。

    场景名怎么来的由调用方负责（插件从状态总线上的副标题取），内核只是纯函数。
    """
    want = str(entry.get("scene") or "").strip()
    if not want:
        return True
    return want == (scene or "")


def _work_hours_of(entry: dict) -> float:
    """这一条的「作答后 N 小时」（旧 ``derived`` 那一支）；``0.0`` ＝ 没配或配得不合法。

    ``bool`` 是 ``int`` 的子类 —— 别让 ``work_hours: true`` 变成「1 小时」。
    单列一个口是因为**有两个判据要读它**（:func:`_span_of` 算窗口、
    :func:`row_time_label` 说「今天还没答」），各写一遍必然漂。
    """
    hours = entry.get("work_hours")
    if isinstance(hours, (int, float)) and not isinstance(hours, bool) and hours > 0:
        return float(hours)
    return 0.0


def _interval_min_of(entry: dict) -> int:
    """这一条的「每 N 分钟」（旧 ``loop``）；``0`` ＝ 不是循环条。

    单列一个口是因为**有三处**要判它（:func:`_span_of` 挑算法、:func:`_time_label` 挑
    怎么说、:func:`row_time_label` 报「今天已循环到点了」），各写一遍必然漂。

    表达式**一字不改地搬过来**（含它对脏值的那份严厉：``"半小时"`` 这种值到这里仍然是
    抛，不是静默当 0）—— 这里做的是给同一事实收一个判定口，不是顺手加固。
    """
    minutes = int(entry.get("interval_min", 0) or 0)
    return minutes if minutes > 0 else 0


def _day_value_s(entry: dict, day_values: "dict[str, str] | None") -> "int | None":
    """这条条目今天的**当日值**（上一次完成 / 今天答过）的当日秒数；今天还没 ⇒ ``None``。

    ``None`` 与 ``0`` 是**两件事**，别拿 ``or 0`` 抹平，两支的用法正好相反：循环支
    ``None`` ⇒ 折成零点（此刻就该出现），推算支 ``None`` ⇒ 今天没答，不登场。
    """
    return _hhmm_to_sec((day_values or {}).get(str(entry.get("id") or "")) or "")


def _loop_window_s(
    entry: dict, day_values: "dict[str, str] | None"
) -> "tuple[int | None, int]":
    """循环条这一轮的「下一轮该做的时刻」与「今天循环到几点为止」（当日秒数）。

    第一项 ``None`` ＝ **今天还没完成过** ⇒ 这个基准根本不存在（不是「零点该做」——
    那是 :func:`_span_of` 折出来的说法；:func:`row_time_label` 据它说「说不出时刻」）。
    第二项：``end`` 不填 ⇒ 当天末。

    算式与 ``None``/``0`` 的分别都只在这一处 —— 两个消费方（:func:`_span_of` 判此刻
    在不在窗口里、:func:`row_time_label` 说那一格该写什么）都读它，各算一遍必然漂。
    """
    last = _day_value_s(entry, day_values)
    step = _interval_min_of(entry) * 60
    stop = _hhmm_to_sec(entry.get("end"))
    return (None if last is None else last + step), (DAY_END_S if stop is None else stop)


def _span_of(
    entry: dict, now_s: int, day_values: "dict[str, str]"
) -> "tuple[int, int] | None":
    """这一条的「登场 → 退场」当日秒数区间；``None`` ＝ **这条此刻算不出窗口**。

    三种登场算法（合并方案 §2.3，旧四型一个不漏）：

    - ``interval_min > 0``（旧 ``loop``）：**下一轮 = 上一次完成时刻 ＋ N 分钟**，今天没
      完成过就按零点算（⇒ 此刻就该出现）；退场是「今天循环到几点为止」（``end``，不填则
      当天结束）—— 这一轮的事一直挂着到你做为止。``day_values`` 里那一格既是旧模型的
      「完成时刻」、也是新模型的当日值，**在这里就是上一次完成**。
    - 有 ``work_hours``（旧 ``derived`` 的「每天问我」那支）：当日值 ＋ N 小时。
      **没有当日值 ⇒ 没有窗口**（不答就不启动倒计时）。
    - 字面 ``at``：登场就是它，退场取字面 ``end``，空 ⇒ 当天末。

    顺序即优先级：一条条目同时写了 ``interval_min`` 与 ``at`` 时按循环算。
    """
    repeat_min = _interval_min_of(entry)
    if repeat_min > 0:
        # 循环条的时间轴**锚在「上一次完成」上**（用户口径 2026-10-11）：下一轮 = 完成时刻
        # ＋ N 分钟。今天一次都没做过 ⇒ 从零点起算 ⇒ 它此刻就该出现（开着这条任务就是
        # 「还没做」），做完那一刻写下这次的时刻，三十分钟后再来。
        #
        # 旧的算法是 ``(now_s // step) * step`` —— 从**零点起的固定网格**（09:12 时登场
        # 09:00、退场 09:30），与上一次什么时候做的毫无关系：九点零五做完和九点二十九做
        # 完，下一轮都落在九点半。
        #
        # 退场也跟着变：旧版本是 ``boundary + step``（这一轮的整点），因为没有「做没做」
        # 参与，只能靠每 N 分钟重新登场来提醒；既然现在计时基准是上一次完成，这一轮该做的
        # 事就**一直挂着**到你做为止 —— 到点就退场的话，「忘了做」与「做完了」在浮层上
        # 长得一模一样，而下一轮是按完成时刻算的，两者都会立刻再来。
        start, stop = _loop_window_s(entry, day_values)
        # 今天没做过的那一轮要给准**零**，不能写成 ``(last or 0) + step`` —— 那是「零点
        # ＋ N 分钟」才到期，于是零点刚过那会儿它反倒不出现，而那正是「今天头一次还没做」。
        boundary = 0 if start is None else start
        if now_s >= stop:
            return None                     # 今天已经过了「循环到几点为止」
        if now_s < boundary:
            return None                     # 这一轮还没到点（下一轮落在收工之后也走这条）
        return boundary, stop

    hours = _work_hours_of(entry)
    if hours > 0:
        start = _day_value_s(entry, day_values)
        if start is None:
            return None                     # 今天没答（或答案是脏值）⇒ 不登场
        off = start + int(hours * 3600)
        return off, off

    at_s = _hhmm_to_sec(entry.get("at"))
    if at_s is None:
        return None
    end_s = _hhmm_to_sec(entry.get("end"))
    return at_s, (DAY_END_S if end_s is None else end_s)


def _time_label(
    entry: dict, at_s: int, end_s: int, day_values: "dict[str, str] | None"
) -> str:
    """行右侧那个时刻（§3.1）：按 :func:`_span_of` 挑算法那三条**同序**报
    （循环 > 作答时长 > 字面），没有字面值就按算出来的那个报。

    **循环条必须排在字面值之前**：它的 ``end`` 在旧内核里不是「这一轮的时刻」而是
    「今天循环到几点为止」（``task.resolve_item`` 把新模型的 ``window.to`` 写进这
    一格）—— 照字面报就是把收工时刻当成这一轮的时刻，而用户口径（2026-10-11）是
    「报下一轮、没完成过就空着」，两条都被它挡死。字面 ``end`` / ``at`` 之所以在
    其余条目上优先，是因为那是用户自己写的原样（``"09:30"``），比格式化出来的更
    可信。

    **循环条今天还没完成过 ⇒ 这一格留空**（2026-10-11）：它的登场时刻是「上一次完成
    ＋ N 分钟」推出来的，没有完成记录就锚在零点，报 ``00:00`` 会让一行整点报时看着像
    是「零点的报时」—— 而这一类任务天生写不进完成那一笔。宁可空着：渲染侧本来就判空
    （``clock_view`` 那一格）。

    第三行那个判据走 :func:`_work_hours_of`（不是就地判真假）：``work_hours: -1``
    这种脏值在 :func:`_span_of` 那里**走不到**推算那一支（要求 ``> 0``），这里要是
    按真假判就会去报一个根本没算出来的 ``end_s``。同一个量三处各判一遍必然漂，
    故一律经那个口。
    """
    if _interval_min_of(entry) > 0:
        start, _stop = _loop_window_s(entry, day_values)
        if start is None:
            return ""
        return fmt_hhmm(at_s)
    for key in ("end", "at"):
        literal = str(entry.get(key) or "").strip()
        if literal:
            return literal
    if _work_hours_of(entry) > 0:
        return fmt_hhmm(end_s)
    return fmt_hhmm(at_s)


def _entry_windows(
    entry: dict, now_s: int, day_values: "dict[str, str]", hidden_ids: "frozenset[str]"
) -> "list[_Window]":
    """一条条目在 ``now_s`` 此刻的激活窗口（通常 0 或 1 个）。"""
    eid = str(entry.get("id") or "")
    if not eid:
        return []
    if eid in hidden_ids:
        return []                            # 点了「稍后」，或今天已经勾完了
    span = _span_of(entry, now_s, day_values)
    if span is None:
        return []
    at_s, end_s = span
    before = int(entry.get("before_min", 0) or 0) * 60
    if not _inside_window(now_s, at_s - before, end_s):
        return []
    return [
        _Window(
            entry_id=eid,
            title=str(entry.get("title") or "").strip() or FALLBACK_TITLE,
            text=(entry.get("text") or "").strip(),
            time_label=_time_label(entry, at_s, end_s, day_values),
            actions=_actions_of(entry),
            face=_face_of(entry),
            at_s=at_s,
            end_s=end_s,
        )
    ]


def _clock_of(
    windows: "list[_Window]", now: datetime, now_s: int
) -> "tuple[str, str, int | None, int | None]":
    """从所有激活窗口里选出**一面钟**（倒计时 > 正计时 > 时钟）。

    没有任何窗口开 ``face`` ⇒ 走实时钟（钟面区一直在，§3.1）。返回
    ``(face, value, countdown_s, countup_s)``。

    **两处差值都要按 ``DAY_S`` 取模**（与 ``_inside_window`` 同一条口径）：窗口可以
    跨午夜（``23:00 → 01:00`` 是很自然的形状 —— 提醒在深夜、留到后半夜才退），那时
    ``end_s < at_s``，直接减出的**负数**会被 ``max(0, …)`` 夹成 ``0`` ⇒ 跨午夜那一半
    钟面恒显示 ``00:00``（倒计时停在跨过午夜**之前**、正计时停在**之后**），而同一瞬间
    这一行确实在窗口里、由 ``_inside_window`` 判进来的。同一个窗口，判定用日圆、
    读数用直线 —— 一个概念两套算法，差额全落在跨午夜那一半上。
    """
    best: "_Window | None" = None
    for w in windows:
        if not w.face:
            continue
        if best is None or _FACE_PRIO[w.face] > _FACE_PRIO[best.face]:
            best = w
    if best is None or best.face == FACE_CLOCK:
        return FACE_CLOCK, now.strftime("%H:%M:%S"), None, None
    if best.face == FACE_COUNTDOWN:
        remaining = (best.end_s - now_s) % DAY_S
        return FACE_COUNTDOWN, fmt_mmss(remaining), remaining, None
    elapsed = (now_s - best.at_s) % DAY_S
    return FACE_COUNTUP, fmt_mmss(elapsed), None, elapsed


def _merge(
    entries: "Iterable[dict]",
    now_s: int,
    now: datetime,
    size: str,
    day_values: "dict[str, str]",
    scene: "str | None",
    max_rows: int,
    hidden_ids: "frozenset[str]",
) -> DisplayState:
    """收窗口 → 去重排序 → 选钟面 → 截断，合成一份整屏显示态。

    ``now_s`` 决定**哪几条在窗口里**、``now`` 决定钟面与 ``{现在}`` 说几点。今天的
    调用方传的是同一个「此刻」（内核不做测试态），两者分开只是为了让它们的来源显式。
    """
    windows: "list[_Window]" = []
    for entry in entries:
        if not entry.get("enabled", True):
            continue
        if not _scene_allows(entry, scene):
            continue
        windows.extend(_entry_windows(entry, now_s, day_values, hidden_ids))

    if not windows:
        return DisplayState(False, FACE_CLOCK, "", [], "", size, frozenset())

    face, value, countdown_s, countup_s = _clock_of(windows, now, now_s)
    windows.sort(key=lambda w: (w.at_s, w.entry_id))
    rows = [
        Row(
            entry_id=w.entry_id,
            title=w.title,
            # ``{剩余}`` 与钟面那一面同源（``_clock_of``）：同样按 ``DAY_S`` 取模，
            # 否则跨午夜那条的行文案写着「剩 00:00」而它还有两个小时才退。
            text=render_text(
                w.text, now, at_s=w.at_s, remaining_s=(w.end_s - now_s) % DAY_S
            ),
            time_label=w.time_label,
            actions=w.actions,
            at_s=w.at_s,
        )
        for w in windows
    ]
    limit = int(max_rows)
    if limit > 0 and len(rows) > limit:
        hidden = len(rows) - limit
        rows = rows[:limit]
        more_text = f"还有 {hidden} 条"
    else:
        more_text = ""
    return DisplayState(
        True,
        face,
        value,
        rows,
        more_text,
        size,
        frozenset(w.entry_id for w in windows),
        countdown_s=countdown_s,
        countup_s=countup_s,
    )


def compute_display(
    entries: "Iterable[dict]",
    now: datetime,
    size: str = "medium",
    day_values: "dict[str, str] | None" = None,
    scene: "str | None" = None,
    max_rows: int = DEFAULT_MAX_ROWS,
    hidden_ids: "frozenset[str]" = frozenset(),
) -> DisplayState:
    """把启用的条目合并成一份整屏显示态；没有条目在自己的时间里时 ``show=False``。

    :param day_values: 当日值表（条目 id → ``"HH:MM"``），喂「作答 ＋ 工作时长」那一型。
        由插件侧注入，内核**不知道它从哪来** —— 保持纯函数。
    :param scene: 此刻命中的场景名；缺省 ``None`` ＝ 没有命中任何场景 ⇒ **填了场景的
        条目一律不显示**。
    :param max_rows: 浮层最多显示几行；``<= 0`` ＝ 不限（设置页那一格留空时的口径）。
    :param hidden_ids: 此刻**被收起**的条目 id（§3.2 的两种退场：点了「稍后 N 分钟」的
        临时收起，与今天已经勾完「完成」的）。判定在插件侧 —— 那边才知道「稍后」到点
        没有（用的是单调时钟，免掉墙钟跳变）、也才知道哪条今天已经完成（当日值那笔账）。
        内核只认「这一轮要收起来」这件事，不重复判一遍。
    """
    return _merge(
        entries,
        _seconds_since_midnight(now),
        now,
        size,
        day_values or {},
        scene,
        max_rows,
        hidden_ids,
    )


# ---------------------------------------------------------------------------
# 详情页列表行那个时刻
# ---------------------------------------------------------------------------

#: 算不出时刻时说的三句**事实**。不留空：空白在列表里读成「这一行坏了」，而这三件
#: 各是一句说得明白的话（时刻还没产生 / 今天已经过去 / 本来就没配时刻）。
NO_TIME_NEEDS_ANSWER = "待作答"
NO_TIME_LOOP_DONE = "今天已过"
NO_TIME_UNSET = "未设时间"


def row_time_label(
    entry: dict, now: datetime, day_values: "dict[str, str] | None" = None
) -> str:
    """列表行右端那个时刻 —— 与浮层那一行**同一份口径**（都走 :func:`_time_label`）。

    与浮层那条路的差别只有「算不出窗口时说什么」：浮层那边整条不显示，而列表这一行
    **本来就在**（用户要在列表里认出这是哪一条），故给一句事实而不是留空。

    ★ 2026-10-08 修：详情页原先直接读 ``at`` / ``end`` 两个字面键 —— 「作答 ＋ 工作时长」
    那一条的时刻是**算出来**的（作答 ＋ N 小时），两个字面键都空 ⇒ 列表里恒显 ``--:--``，
    用户看不出这条几点提醒。

    ★ 2026-10-11 修：循环条**没有窗口**有两种完全不同的缘故，原先共用一句「今天已过」——
    刚做完一轮（下一轮还没到）也这么说。现在分开：过了收工时刻（或下一轮已经落在收工
    之后，今天不会再来）才说「今天已过」，否则报**下一轮的时刻**。
    """
    now_s = _seconds_since_midnight(now)
    span = _span_of(entry, now_s, day_values or {})
    if span is not None:
        return _time_label(entry, span[0], span[1], day_values)
    if _work_hours_of(entry) > 0:
        return NO_TIME_NEEDS_ANSWER       # 时刻由每天的作答算出来，今天还没答
    if _interval_min_of(entry) > 0:
        start, stop = _loop_window_s(entry, day_values)
        if now_s >= stop or (start is not None and start >= stop):
            return NO_TIME_LOOP_DONE      # 今天已经循环到 end 了 / 下一轮落在收工之后
        if start is None:
            return ""                     # 今天还没做过 ⇒ 没有基准，说不出时刻（与浮层同款）
        return fmt_hhmm(start)            # 这一轮还没到点 ⇒ 报下一轮
    return NO_TIME_UNSET


def row_summary(entry: dict, ask: str) -> str:
    """列表行第二行那句话：**这一条是怎么工作的**（2026-10-08）。

    为什么要有它：列表行此前只有「时刻 ＋ 标题 ＋ 已完成 / 已关」四格，读得出一条
    待办**叫什么、几点**，却读不出它**怎么响** —— 同样是「09:30 喝水」的两条，一条
    到点弹一枚「完成」、一条启动时问一个时刻再算出下班时间，在列表上一模一样。用户点进去
    才知道是哪一种，而这正是他最常要问的那件事。

    ``ask`` 由调用方从 :func:`_ask_of` 取（**不在这里重判**）：那是「键不在 ⇒ 按
    choice 走」这条无值两态口径的家，本函数只是个拼句子的纯函数。

    顺序不是随意的，按「这条待办会怎么打扰我」排：先问法（它决定其余各项在不在），
    再时间上的形状（答后多久 / 每 N 分钟 / 提前量），最后才是显示面（钟面 / 场景）。
    **空的那几项一个都不提** —— 摘要里写「不提前」等于给每一行都加一句废话。

    ★ 时间那几项**按内核挑窗口的优先级报**（:func:`_span_of`：循环 > 作答时长 >
    字面时刻）。这三个键可以在同一条上**同时有值** —— 界面切问法时只灰不收
    （``detail_view`` 的 ``_sync_enabled`` 一条条说了为什么），而内核只认优先级最高的
    那一个。照「有没有值」报，就会报出一条条目**根本不会走**的路：既有「每 30 分钟
    一次」又有「下班＝答后 10 小时」时，后者是死的（``_span_of`` 走不到 ``work_hours``
    那一支），报出来等于骗人。

    也正因为这里说的是**机制**，「今天还响不响」不在这里判 —— 那是左端那一格
    （:func:`row_time_label`）的事。同一个量两处各判一遍必然漂。
    """
    parts: "list[str]" = []
    if not entry.get("enabled", True):
        # 「已关」这件事在行尾那一格**会被「已完成」顶掉**（那是一行只能放一件事的
        # 老规矩），而它比「今天做完了没」更值得一眼看见：关着的条目根本不会响。
        parts.append("已停用")
    parts.append(ASK_WORDS.get(ask, ASK_WORDS[PROMPT_CHOICE]))
    repeat = int(entry.get("interval_min", 0) or 0)
    hours = _work_hours_of(entry)
    if repeat > 0:
        parts.append(f"每 {repeat} 分钟一次")     # 循环优先 ⇒ 此刻 ``hours`` 是死的
    elif hours > 0:
        parts.append(f"下班＝答后 {hours:g} 小时")
    before = int(entry.get("before_min", 0) or 0)
    if before > 0:
        parts.append(f"提前 {before} 分登场")
    face = _face_of(entry)
    if face:
        parts.append(FACE_WORDS[face])
    scene = str(entry.get("scene") or "").strip()
    if scene:
        parts.append(f"只在「{scene}」")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# 「测试这一条」（详情页编辑屏那枚按钮）
# ---------------------------------------------------------------------------


def _test_moment_s(
    entry: dict, now_s: int, day_values: "dict[str, str]"
) -> "int | None":
    """「测试这一条」按哪一刻算；``None`` ＝ 这一条**算不出窗口**。

    两条规则，按优先级：

    1. **此刻真在窗口里 ⇒ 就按此刻算** —— 预览所见即此刻真实所见（倒计时就是真的
       剩余量）；
    2. 否则按这一条**本轮的登场时刻**（窗口起点）算 —— 条目只在自己那段时间里显示，
       此刻多半不在窗口内，拿「此刻」去问窗口的话什么都不会出现，而用户点预览要看的
       正是「它一出现长什么样」。

    认不出的（时刻没填全 / 问上班时间的条目今天还没答 / 循环条今天已过终点）⇒ ``None``。
    与常态**同一个答案** —— 测试态改的是「此刻人在不在窗口里」，不是「这一条有没有
    窗口」：后者是这条条目自己的事实，测试态替它编一个才是骗人。

    ★ **登场时刻是「窗口起点」，不是 :func:`_span_of` 的第一个返回值**。``_span_of``
    报的是这一条的**「到点」时刻 ＋ 退场时刻**，而 :func:`_entry_windows` 把「提前登场」
    （``before_min``）算在**窗口起点**上（``at_s - before``）；两者不是一回事：

    - 字面 ``at`` 条：到点 ＝ ``at``、窗口起点 ＝ ``at - 提前量``；
    - 作答 ＋ 工作时长条：到点 ＝ 下班、退场 ＝ 下班 ⇒ ``_span_of`` 给 ``(下班, 下班)``，
      窗口起点 ＝ **下班 − 提前量**，与到点**恰好差一个提前量**。

    拿 ``span[0]`` 当登场时刻，第二条就预览出「下班 − 下班 ＝ 0」＝ **倒计时 00:00**
    （2026-10-08 用户真机上报；常态那一侧一直是对的，只有预览取值这一处错）。
    """
    span = _span_of(entry, now_s, day_values)
    if span is None:
        return None
    at_s, end_s = span
    before = int(entry.get("before_min", 0) or 0) * 60
    start_s = at_s - before
    if _inside_window(now_s, start_s, end_s):
        return now_s
    return start_s


def preview_entry(
    entry: dict,
    now: datetime,
    size: str = "medium",
    day_values: "dict[str, str] | None" = None,
) -> DisplayState:
    """「测试这一条」的显示态：按这一条**本轮的登场时刻**算一份（此刻不在窗口里也算得出来）。

    与常态不同的四件事，都是「测试」这个词自己要求的（与旧翻页钟那一版同款）：

    - **强制启用**：条目开关关着也要看得见 —— 测试正是要在保存之前先看一眼；
    - **不受场景门禁**：人不在那条场景里（多半就是这样 —— 正在详情页上配它）也得看得见，
      否则「只在公司显示」的那一条永远测不出来；
    - **补 id**：未保存的新条目还没有 id（:data:`TEST_ENTRY_ID`）；
    - **不受「今天勾完了 / 点了稍后」影响**：那两条收起理由说的是「别自动弹」，
      而这一次是用户自己点开看的。

    钟面仍走 ``now``（实时钟的钟面本来就是现在）。

    返回 ``show=False`` ＝ 这一条算不出可显示的时刻，**不说谎也不硬凑**：由调用方
    把这件事告诉用户。
    """
    values = day_values or {}
    now_s = _seconds_since_midnight(now)
    at_s = _test_moment_s(entry, now_s, values)
    if at_s is None:
        return DisplayState(False, FACE_CLOCK, "", [], "", size, frozenset())
    probe = dict(entry)
    probe["enabled"] = True
    probe["id"] = str(entry.get("id") or "") or TEST_ENTRY_ID
    # 摘掉场景：与上面那句「强制启用」同一套路 —— 测试态看的是**这一条长什么样**，
    # 不是「此刻它在不在该在的地方」。
    probe.pop("scene", None)
    # 以「登场那一刻」当 ``now_s`` 走同一个 ``_merge`` ⇒ 这一条正好落在自己窗口的起点上。
    # ``max_rows`` 传 0（不截断）：测试看的就是一条，没有「还有 k 条」这一说。
    return _merge([probe], at_s, now, size, values, None, 0, frozenset())


def preview_text_of(entry: dict, value: "object", now: datetime) -> str:
    """「填完这个值之后会显示什么」那一句（问 ``time`` 型条目的询问浮层用）。

    **纯函数**，且**只有这一条自己能算**：填了 09:00 意味着几点下班，靠的是这一条
    自己那个工作时长。填的不是时刻 / 这一条没填时长 ⇒ 空串（浮层那一行留白；
    框架只管画、**不猜**）。

    与 :func:`preview_entry` 各看一面：那个看**显示面**（浮层上这条长什么样），
    这个看**问面**（填完之后会变成什么样）。
    """
    text = value.strip() if isinstance(value, str) else ""
    start_s = _hhmm_to_sec(text)
    if start_s is None:
        return ""
    hours = _work_hours_of(entry)
    if hours <= 0:
        return ""
    off_s = start_s + int(hours * 3600)
    label = str(entry.get("text") or "").strip()
    return render_text(label, now, at_s=off_s) or f"下班 {fmt_hhmm(off_s)}"
