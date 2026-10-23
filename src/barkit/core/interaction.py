"""core/interaction：条件触发询问的**规则与账目**（纯逻辑，零 Qt）。

框架要新增的那一层是「条件成立 ⇒ 弹窗问一次 ⇒ 收回答案」。本模块只管其中**不碰窗口**的
两半：**条件怎么算**与**问过没问过**；浮层与排队在 ``shell``（批 2），业务规则在插件。

**为什么这两半必须在框架**：让每个插件自己判「今天问过没」的结果是四份各写一遍的日期比较，
且它们不会一致（有人按自然日、有人按启动次数）。**去重是交互的通用属性，不是业务属性。**

三件事的边界（改之前先读）：

- **条件**：具名 + 参数（``Condition``），是可配置的数据，不是一个 ``should_ask()`` 回调。
  回调写法让判据散在插件里、不可见也不可测；具名条件下「当场景 = Office 时问我」是一条数据，
  将来 todo 应用存配置即可复用。
- **锁存（latch）合取**：条件不是「某一刻同时为真」。启动时路由场景还是 ``unknown``，要等
  ``route_guard`` 首轮 tick 才变成 ``active/Office`` —— 只在启动时求值一次，那条规则**永远
  触发不了**。故事件型条件各自锁存，任一信号到达刷对应那一项、再合取求值（:class:`Latches`）。
- **去重只挡"推"，不挡"拉"**：账目挡的是**弹窗**，不是**用户自己动手填**。用户关掉浮层后
  想补填，走的是插件自己的入口（面板行 / 设置页），那条路不查本账 —— 详见方案 §2.2b。

刻意不做的：**不存业务答案**。账目只记「弹过 / 答过 / 稍后到几点」，答案归插件自己
（否则 todo 应用的数据就长在框架里了）。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# ── 条件类型 ──────────────────────────────────────────────────────────
# 分成两类，区别在「真值要不要锁存」：
#   事件型（EVENT_KINDS）：真值由一次信号决定，信号过去仍为真 ⇒ 锁存。
#   即时型：每次求值现算（``daily_at`` 看钟、``predicate`` 问业务），不锁存。
COND_ON_START = "on_start"          # 程序启动过（一次性，由中枢在装配完成后置位）
COND_STATE_IS = "state_is"          # 某状态机取值等于…（topic + state + 可选副标题）
COND_PANEL_OPENED = "panel_opened"  # 面板被打开过
COND_DAILY_AT = "daily_at"          # 当天到点之后为真（at="HH:MM"）
COND_PREDICATE = "predicate"        # 逃生舱：自定义谓词（评审口子，别默认用它）

EVENT_KINDS = (COND_ON_START, COND_STATE_IS, COND_PANEL_OPENED)

# ── 去重档位 ──────────────────────────────────────────────────────────
DEDUPE_ALWAYS = "always"                # 每次条件成立都问（只给「每次都必须知道」的场合）
DEDUPE_SESSION = "once_per_session"     # 本次运行只问一次（不落盘）
DEDUPE_DAY = "once_per_day"             # 每天一次（默认；弹过即记，含关掉 / 超时）
DEDUPE_ANSWERED = "until_answered"      # 直到答过一次为止（跨天仍不再问）
# 「当天答过为止」：与 ``once_per_day`` 只差一条 —— **关掉 / 超时不算答过**。
# 两件事本来是两个事实，「弹过即记」把它们当成一件（2026-10-01 用户报的正是这个：
# 他关掉没答的那天，翻页钟照样按旧值算出「下班时间 19:00」）。没答的话**每次启动**
# 都还留一次机会；已答（含用户自己在设置页补填）当天就不再问。
# 「每次启动」由**内存**里那笔账实现（不落盘）⇒ 同一进程里不会因为场景反复切换、
# 或规则重登记而反复弹（见 :class:`DedupeLedger` 的 ``_session``）。
DEDUPE_ANSWERED_DAY = "until_answered_day"

# ── 问法 ──────────────────────────────────────────────────────────────
#: 问法只有这两种 —— 浮层那一侧（``shell/interaction.py``）只认这两个 kind，
#: 其它值一律走「不画控件 ＋ 记 warning」那一支。这里**不声明**没实现的问法：
#: 声明出来就像这个问法今天能用，实际弹出来是个没有输入框也没有按钮的窗。
PROMPT_CHOICE = "choice"   # 几个按钮选一个
PROMPT_TIME = "time"       # 填一个时间

# ── 答案来源（区分「填了」与「没理」）──────────────────────────────────
RESULT_ANSWERED = "answered"
RESULT_TIMEOUT = "timeout"      # 显示时长到点
RESULT_DISMISSED = "dismissed"  # 被关掉（含 app 退出）
RESULT_LATER = "later"          # 主动选了「稍后」

#: 每天最多「稍后」几次。上限不是防用户，是防「稍后」变成无限骚扰的合法外衣 ——
#: 点第三次还不想填，那就是不想填，当天到此为止。
SNOOZE_LIMIT_PER_DAY = 3

#: 账目文件名（落在 APP_SUPPORT 下）。**不在 paths.py 加常量**：本模块是唯一读写方，
#: 加常量会让「受管路径名单」多一项却只有一个消费者（门禁按 vars(paths) 派生，改它要动两处）。
LEDGER_FILENAME = "interaction_state.json"


@dataclass(frozen=True)
class Condition:
    """一条具名条件。

    :param kind: :data:`COND_*` 之一。
    :param topic: ``state_is`` 用的状态 topic（如 ``plugin.route_guard.status.profile``）。
    :param state: 要比对的取值名（如 ``"active"``）。
    :param detail: 可选的副标题比对（如场景名 ``"Office"``）；空串 = 不比副标题。
    :param at: ``daily_at`` 的时刻 ``"HH:MM"``。
    :param predicate: ``predicate`` 的回调，无参、返回真值。**不得做 IO**：求值发生在
        主线程、且会被频繁调用（任一信号到达都重算一遍合取）。
    """

    kind: str
    topic: str = ""
    state: str = ""
    detail: str = ""
    at: str = ""
    predicate: "Callable[[], bool] | None" = None


@dataclass(frozen=True)
class PromptSpec:
    """问什么（不含「怎么画」——控件归 ``shell``）。

    :param key: 去重键，全局唯一，建议 ``"<plugin>.<业务>.<id>"``。同一个 key 的两次
        触发共享一份账目（今天弹过就不再弹）。
    :param later_minutes: 给不给「稍后」按钮；``0`` = 不给。给了就要接受它会**清掉当天
        的「弹过」标记**（否则同一天里「稍后」永远等不到第二次弹窗），见 :meth:`DedupeLedger.snooze`。
    :param initial: 预填值（``PROMPT_TIME`` 用）⇒ 返回 ``"HH:MM"``；``None`` / 空串 =
        不预填（浮层回落到当前时刻）。**必须是可调用**：规则在**启动时**登记、弹窗可能
        几小时后才弹 —— 传一个算好的字符串，弹出来的就是「登记那一刻」的时间。
    :param preview: 给一个值、返回**一句预览文本**（空串 ⇒ 那一行留白）。浮层拿它画
        「填完会变成什么样」那一行；**文案归插件**（只有它知道填了 09:00 之后意味着什么），
        框架只管画、且**不猜**：插件不给就不画。
    """

    key: str
    title: str
    message: str = ""
    kind: str = PROMPT_TIME
    options: "tuple[str, ...]" = ()
    timeout_s: int = 60
    later_minutes: int = 0
    initial: "Callable[[], str] | None" = None
    preview: "Callable[[object], str] | None" = None


@dataclass(frozen=True)
class TriggerSpec:
    """何时问 + 问什么。插件只声明这个，求值 / 去重 / 弹窗全归框架。"""

    plugin_id: str
    prompt: PromptSpec
    conditions: "tuple[Condition, ...]" = ()
    dedupe: str = DEDUPE_DAY


@dataclass(frozen=True)
class InteractionResult:
    """一次询问的结论。``answered`` 之外还要 ``kind``：业务要知道「没填」是**没理**
    还是**主动推迟**，两者的后续处理不一样（前者当天算了，后者还要再来一次）。"""

    answered: bool
    value: "object" = None
    kind: str = RESULT_ANSWERED


class Latches:
    """一个触发器的一组建锁存位（**只在内存**：进程重启后信号本就会再来一遍）。

    只有事件型条件占锁存位（:data:`EVENT_KINDS`）；即时型每次现算，不占。
    """

    def __init__(self) -> None:
        self._bits: dict[int, bool] = {}

    def set(self, index: int, value: bool = True) -> None:
        self._bits[index] = value

    def reset(self) -> None:
        """清空（插件被禁用 / 重登记时用：旧锁存位不该让新实例一上来就触发）。"""
        self._bits.clear()

    def get(self, index: int) -> bool:
        return bool(self._bits.get(index))


def topics_of(trigger: TriggerSpec) -> "set[str]":
    """该触发器依赖的 topic（给中枢订阅用；多个触发器可共享同一份订阅）。

    **凡填了 ``topic`` 的条件都要订阅**，不只看 ``state_is``。判据是「填了 topic 就说明
    这条规则要在那个信号到达时重算」，与它是哪一种求值方式（锁存 / 现算）无关：``state_is``
    靠信号**刷锁存位**，``predicate`` 靠信号**触发重算**（它自己不认 topic，就是个回调）
    —— 不订阅的话后者只剩中枢心跳那一路（30 秒一次），条件成立与实际弹窗之间差半分钟。

    今天这么用的只有变量池：它把「有变量变了」发成
    ``app.variables.changed``，ask 规则的条件写成 ``predicate`` ＋ 那条 topic
    （见 ``core/variables/host.py``）。
    """
    return {c.topic for c in trigger.conditions if c.topic}


def evaluate(trigger: TriggerSpec, latches: Latches, now: "datetime | None" = None) -> bool:
    """条件是否全部成立。**任一信号到达都重算一遍**（不是只在启动时算一次）。

    事件型读锁存位，即时型现算；``predicate`` 抛异常按**不成立**处理并记日志 —— 一个插件的
    判据写错不该把别的规则的求值一起拖垮。
    """
    for index, cond in enumerate(trigger.conditions):
        if cond.kind in EVENT_KINDS:
            if not latches.get(index):
                return False
        elif cond.kind == COND_DAILY_AT:
            if not _daily_at_reached(cond.at, now):
                return False
        elif cond.kind == COND_PREDICATE:
            if cond.predicate is None:
                return False
            try:
                if not cond.predicate():
                    return False
            except Exception:  # noqa: BLE001 —— 单条判据异常不拖垮整轮求值
                log.exception("条件谓词异常（按不成立处理）：%s", trigger.prompt.key)
                return False
        else:
            # 未知类型：宁可不触发也不乱触发（弹一个没人要的窗比不弹更糟）。
            log.warning("未知条件类型 %s（%s），按不成立处理", cond.kind, trigger.prompt.key)
            return False
    return True


def _daily_at_reached(at: str, now: "datetime | None") -> bool:
    """当天时刻是否已过 ``at``（``"HH:MM"``）。非法值 = 不成立（不猜）。"""
    if not at:
        return False
    try:
        target = datetime.strptime(at.strip(), "%H:%M").time()
    except ValueError:
        log.warning("daily_at 的时刻非法：%r", at)
        return False
    now = now or datetime.now()
    return (now.hour, now.minute) >= (target.hour, target.minute)


def state_matches(cond: Condition, report) -> bool:
    """``state_is`` 的取值比对：状态名相等，且（给了 detail 时）副标题也相等。

    ``report`` 是总线上的 ``StateReport``（取其 ``state`` / ``summary``）。**只读属性、
    不认类型**：core 不该为了比两个字符串去 import 那个 Spec 类。
    """
    if cond.kind != COND_STATE_IS:
        return False
    if getattr(report, "state", None) != cond.state:
        return False
    if cond.detail and str(getattr(report, "summary", "") or "") != cond.detail:
        return False
    return True


class DedupeLedger:
    """「问过没问过」的账目（落盘）。**只挡弹窗，不挡用户自己动手填。**

    每条账目：``{"last_asked": "YYYY-MM-DD", "answered": bool,
    "answered_date": "YYYY-MM-DD", "snooze_until": "ISO", "snooze_date": "YYYY-MM-DD",
    "snooze_count": int}``。

    **两笔账是两个事实，别互相读**：``last_asked`` 是「弹过」（弹的瞬间就记，不等回答），
    ``answered_date`` 是「哪天答的」（答 / 补填才写）。``once_per_day`` 读前者、
    ``until_answered_day`` 读后者 —— 把前者当后者用，就成了「我问过你 ⇒ 你填过了」，
    而用户 2026-10-01 报的正是这个：他关掉没答，系统却按旧值当成已知。

    **弹的瞬间就记账**（不等回答）：浮层是零模态的，用户可能直接走开；弹过不记账会变成
    「每次唤醒都弹一次」的骚扰。代价是「关掉了想补填」得走插件自己的入口 —— 那本来就该由
    「拉」的那条路解决，不是靠把弹窗改烦人。

    坏 JSON / 读盘失败：记日志后按**空账**继续（最坏是多弹一次，不是崩）。写盘用原子替换，
    免得断电留下半个文件把账目全丢。
    """

    def __init__(self, path: "Path | None" = None) -> None:
        self._path = path
        # 本次运行的「已问过」（once_per_session / until_answered_day）：不落盘 —— 进程
        # 重启后本就该重新问一次。**存的是「哪天问的」而不是一个布尔**：常驻进程跨天是
        # 常态，只记「问过没」的话，第二天会被昨天那笔内存账整个挡掉（一个集合挡不住
        # 「昨天」与「今天」是两笔）。
        self._session: "dict[str, str]" = {}
        self._data: dict = {}

    # ---------- 落盘 ----------

    @property
    def path(self) -> Path:
        # 调用期读（**禁止**写进默认参数值：那会在定义期冻结路径，测试重定向即失效）。
        if self._path is None:
            from barkit import paths

            self._path = Path(paths.APP_SUPPORT) / LEDGER_FILENAME
        return self._path

    def load(self) -> dict:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            log.warning("交互账目损坏，按空账处理：%s", self.path)
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            log.exception("写交互账目失败（本次记账丢失，最坏是多弹一次）：%s", self.path)

    def _entry(self, key: str) -> dict:
        if not self._data:
            self._data = self.load()
        item = self._data.get(key)
        return dict(item) if isinstance(item, dict) else {}

    def _put(self, key: str, **fields) -> None:
        if not self._data:
            self._data = self.load()
        item = self._data.get(key)
        item = dict(item) if isinstance(item, dict) else {}
        item.update(fields)
        self._data[key] = item
        self._save(self._data)

    # ---------- 判定 ----------

    def should_ask(self, key: str, dedupe: str = DEDUPE_DAY, now: "datetime | None" = None) -> bool:
        """此刻该不该弹。**只回答"该不该弹"这一件事**，不掺条件判断（那是 evaluate 的事）。"""
        if dedupe == DEDUPE_ALWAYS:
            return True
        now = now or datetime.now()
        today = now.date().isoformat()
        item = self._entry(key)
        if self._snooze_active(item, now):
            return False
        if dedupe == DEDUPE_SESSION:
            return key not in self._session
        if dedupe == DEDUPE_DAY:
            return item.get("last_asked") != today
        if dedupe == DEDUPE_ANSWERED_DAY:
            # 今天答过（含补填）⇒ 当天不再问；否则**每次启动**都还有一次机会
            # （内存那笔账只挡「这次运行」——重启即重新开始，见 :data:`DEDUPE_ANSWERED_DAY`）。
            return item.get("answered_date") != today and self._session.get(key) != today
        if dedupe == DEDUPE_ANSWERED:
            return not bool(item.get("answered"))
        log.warning("未知去重档位 %s（%s），按不弹处理", dedupe, key)
        return False

    @staticmethod
    def _snooze_active(item: dict, now: datetime) -> bool:
        until = item.get("snooze_until") or ""
        if not until:
            return False
        try:
            return datetime.fromisoformat(until) > now
        except ValueError:
            return False

    # ---------- 记账 ----------

    def mark_asked(self, key: str, now: "datetime | None" = None) -> None:
        """**弹的瞬间**记一笔（当天不再弹）。不动 ``answered`` —— 弹过 ≠ 填过。"""
        now = now or datetime.now()
        self._session[key] = now.date().isoformat()
        self._put(key, last_asked=now.date().isoformat())

    def mark_answered(self, key: str, now: "datetime | None" = None) -> None:
        """答过了（含**补填**：用户自己打开入口填的也算）。``until_answered`` 那档靠它。

        **顺带记下「哪天答的」**（``answered_date``）：``until_answered_day`` 要判的是
        「**今天**答过没」，而 ``answered`` 那一栏是「答过没有」—— 跨天仍为真。两件事
        挤在一栏里的结果，是常驻进程到了第二天还以为今天问过了。
        """
        now = now or datetime.now()
        today = now.date().isoformat()
        self._session[key] = today
        self._put(
            key,
            last_asked=today,
            answered=True,
            answered_date=today,
            snooze_until="",
        )

    def can_snooze(self, key: str, now: "datetime | None" = None) -> bool:
        """还能不能再「稍后」一次 —— 中枢据此决定**画不画**那个按钮。

        判据与 :meth:`snooze` 共用 :meth:`_snooze_count`：**两处各判一次必然漂移**，
        漂移的表现是「按钮画出来了、点了没反应」（点了才发现超限，那是骗人）。
        """
        now = now or datetime.now()
        return self._snooze_count(self._entry(key), now.date().isoformat()) < SNOOZE_LIMIT_PER_DAY

    def snooze(self, key: str, minutes: int, now: "datetime | None" = None) -> bool:
        """选了「稍后」：清掉当天的「弹过」标记，改记一个到期时刻。

        **必须清 ``last_asked``**：不清的话 ``once_per_day`` 当天仍然挡着，「稍后」等于没点。
        **也要撤掉内存里那笔「本次运行问过」**（``_session``）：``once_per_session`` 与
        ``until_answered_day`` 两档的门都在那儿，不撤则同样是个点了没用的按钮（这两档读的
        是内存、不是 ``last_asked``，只清盘上那笔挡不住它们）。
        每天限 :data:`SNOOZE_LIMIT_PER_DAY` 次（按自然日重置），超出返回 ``False`` ——
        中枢据此不给「稍后」按钮（画一个点了没用的按钮是骗人）。
        """
        now = now or datetime.now()
        today = now.date().isoformat()
        item = self._entry(key)
        count = self._snooze_count(item, today)
        if count >= SNOOZE_LIMIT_PER_DAY:
            return False
        self._session.pop(key, None)
        self._put(
            key,
            last_asked="",
            snooze_until=(now + timedelta(minutes=minutes)).isoformat(timespec="seconds"),
            snooze_date=today,
            snooze_count=count + 1,
        )
        return True

    @staticmethod
    def _snooze_count(item: dict, today: str) -> int:
        """当天已「稍后」过几次（换天自动归零）。空 / 脏值按 0 —— 宁可多给一次，
        也不该因为一条坏数据把按钮永久掐掉。"""
        if item.get("snooze_date") != today:
            return 0
        try:
            return max(0, int(item.get("snooze_count") or 0))
        except (TypeError, ValueError):
            return 0

    def pending_snoozes(self, now: "datetime | None" = None) -> "list[str]":
        """「稍后」已到期、该再弹一次的 key（给中枢的心跳扫）。"""
        now = now or datetime.now()
        return [
            key
            for key, item in (self._data or self.load()).items()
            if isinstance(item, dict) and not self._snooze_active(item, now)
            and (item.get("snooze_until") or "")
        ]

    def forget(self, key: str) -> None:
        """整条账目作废（插件卸载 / 规则删除时用，免得留下孤儿账）。"""
        if not self._data:
            self._data = self.load()
        if self._data.pop(key, None) is not None:
            self._save(self._data)
        self._session.pop(key, None)


@dataclass
class Trigger:
    """一条已登记的规则：声明 + 运行期状态（锁存位）。中枢持有一份。"""

    spec: TriggerSpec
    latches: Latches = field(default_factory=Latches)

    @property
    def key(self) -> str:
        return self.spec.prompt.key

    def due(self, now: "datetime | None" = None) -> bool:
        """条件是否成立（不含去重 —— 两件事分开判，混在一起就没法测了）。"""
        return evaluate(self.spec, self.latches, now)
