"""core/variables/host：把变量池接到**总线**与**心跳**上（设计文档 §2.3 / §2.5）。

池本身（:class:`~barkit.core.variables.pool.VariablePool`）只算值：谁喂它快照、什么时候
开新一轮、变了告诉谁 —— 那三件事在本模块。分开是因为它们的**生命周期不一样**：
池是纯数据（构造即可用、单测里直接造），宿主握着一堆要撤的东西（订阅面、心跳句柄、
询问规则），只有它需要 ``dispose()``。

四件事：

1. **订阅**：按 ``pool.watched_topics()`` 订阅组件状态 topic，把快照喂给池；
   声明变了（设置页改完）⇒ 订阅面**整条重来**（:meth:`VariableHost.load`）。
2. **节拍**：挂在框架唯一时钟源 ``Heartbeat`` 上（**不新起 QTimer**，与
   ``InteractionCenter`` 同款）。这一拍干三件事：开新一轮（跨天时 ask 值自动回到
   「未填」）、兜底复核、把变化广播出去。
3. **广播**：渲染值指纹与上一拍不同 ⇒ 发 ``app.variables.changed``（常量在
   ``core/events.py``，理由见那里）。
4. **ask 的询问登记**（§2.5）：给每条 ask 变量向交互中枢登记一条规则，答案写回池。

**为什么不挂 1 秒的节拍**：``now`` / ``date`` 这两个 framework 变量**每秒**都在变，1 秒
的节拍等于每秒广播一次 —— 消费方（变量页、任务重算）跟着每秒重画。30 秒那一档足够
「跨天归零」与「按需型兜底」（与 ``InteractionCenter.BEAT_INTERVAL_S`` 同档）。真需要
秒级刷新的地方（浮层倒计时）自己读 ``pool``，不必等广播。

**"变了"的判据是渲染值**（``pool.fingerprint()``，见那里的说明）：比渲染文本而不是裸值，
``17:27`` 与 ``17:27:00`` 这两种写法的秒表示在文本上一样时不该触发一次重画。
"""

from __future__ import annotations

import logging
from datetime import datetime
from functools import partial
from typing import Callable

from barkit.core import interaction as core_interaction
from barkit.core.events import VARIABLES_CHANGED_TOPIC

from . import expr, rules
from .pool import VariablePool

log = logging.getLogger(__name__)

#: 变量池的节拍（秒）。理由见模块 docstring。
BEAT_INTERVAL_S = 30.0

#: 变量池那条询问规则的 ``plugin_id``。**不是某个组件**：变量是框架级的，询问随框架
#: 存在、不随引用它的组件启停（2026-10-10 裁定③）。空串也不在任何组件的名下，
#: ``InteractionCenter.unregister(plugin_id)`` 撤组件规则时撤不到它。
FRAMEWORK_OWNER = ""


def ask_key(var_id: str) -> str:
    """一条 ask 变量在交互中枢那本账上的 key（**唯一一处**：登记与撤销都读它）。"""
    return f"variables.{var_id}"


class VariableHost:
    """变量池的宿主：订阅 ＋ 节拍 ＋ 广播 ＋ ask 登记。

    构造参数全是注入（``events`` / ``heartbeat`` / ``interaction`` 都可缺省）：
    缺省时对应那一路**整体不发生**（不广播、不登记），池本身照常能取值 ——
    单测里造一个光秃秃的池不该被迫搭一条总线。
    """

    def __init__(
        self,
        events=None,
        heartbeat=None,
        *,
        clock: "Callable[[], datetime] | None" = None,
        interaction=None,
    ) -> None:
        self._events = events
        self._heartbeat = heartbeat
        self._interaction = interaction
        self.pool = VariablePool(clock=clock)
        #: topic → 本宿主挂上去的 handler（退订要用**同一个对象**）
        self._topics: "dict[str, Callable]" = {}
        #: 交互中枢那本账上的 key → 这条声明的 id（撤销时要按 key 摘）
        self._ask_keys: "set[str]" = set()
        self._ticker = None
        self._last: "tuple[tuple[str, str], ...]" = ()

    # ---------- 生命周期 ----------

    def start(self, declarations: object = None) -> None:
        """装配期入口：加载声明 ＋ 订阅 ＋ 挂节拍 ＋ 登记询问。

        首轮**不广播**：这一刻消费方（设置页、面板）都还没建好，发出去只有本宿主自己
        的日志看得见；指纹照记，好让下一次广播比的是「与此刻的差别」。
        """
        self.load(declarations)
        self.pool.begin_pass()
        self._last = self.pool.fingerprint()
        if self._heartbeat is not None:
            self._ticker = self._heartbeat.add_ticker(
                BEAT_INTERVAL_S, self._on_beat, key="variables"
            )

    def load(self, declarations: object = None) -> None:
        """加载 / 重载声明：**订阅面与询问规则都跟着重来一遍**。

        配置改了（设置页写回、或用户手改 YAML 触发热重载）就走这里。池自己会把
        跳过的那几条记进 ``pool.problems()``，本方法不重复报。
        """
        self.pool.load(declarations)
        self._resubscribe()
        self._register_asks()

    def dispose(self) -> None:
        """退出收尾：撤订阅、摘心跳句柄、（在屏上的询问归中枢自己收）。

        **心跳那条必须显式摘**：心跳持有回调引用，只把本地句柄置空等于
        「宿主以为自己退了、心跳还在每 30 秒叫它一次」（与 ``InteractionCenter.dispose``
        同一条理由）。
        """
        self._unsubscribe_all()
        if self._heartbeat is not None and self._ticker is not None:
            self._heartbeat.remove_ticker(self._ticker)
        self._ticker = None
        self._heartbeat = None

    # ---------- 订阅面 ----------

    def _resubscribe(self) -> None:
        """按当前的声明重挂订阅（**先全撤再全挂**：少了「先撤」，用户删掉一条 component
        变量之后，那个 topic 上仍挂着我们 —— 它每报一次就白喂一次）。"""
        self._unsubscribe_all()
        if self._events is None:
            return
        for topic in self.pool.watched_topics():
            handler = partial(self._on_status, topic)
            self._events.subscribe(topic, handler)
            self._topics[topic] = handler

    def _unsubscribe_all(self) -> None:
        if self._events is not None:
            for topic, handler in self._topics.items():
                self._events.unsubscribe(topic, handler)
        self._topics.clear()

    def _on_status(self, topic: str, payload: object) -> None:
        """组件报来一份状态快照（只在池声明听它时池才收，见 ``pool.on_status``）。"""
        if not self.pool.on_status(topic, payload):
            return
        self.pool.begin_pass()
        self._publish_if_changed()

    # ---------- 节拍与广播 ----------

    def _on_beat(self) -> None:
        """一拍：开新一轮（跨天归零落在 :meth:`VariablePool.begin_pass` 里）＋ 广播变化。"""
        self.pool.begin_pass()
        self._publish_if_changed()

    def _publish_if_changed(self, force: bool = False) -> bool:
        """指纹与上一拍不同 ⇒ 发 ``app.variables.changed``；返回「发了没」。

        ``force`` 给「刚写进一个 ask 值」那条路用：那一刻的变化**必须**广播出去，
        哪怕指纹恰好与上一拍相同（用户把 09:00 改成 09:00 之后又改回来的那一类），
        否则消费方停在旧文案上，而这个写是用户刚刚亲手做的。
        """
        if self._events is None:
            return False
        now = self.pool.fingerprint()
        if not force and now == self._last:
            return False
        self._last = now
        self._events.publish(VARIABLES_CHANGED_TOPIC, now)
        return True

    # ---------- ask 询问登记（§2.5） ----------

    def _register_asks(self) -> None:
        """给每条 ask 变量登记一条询问规则，并撤掉不再存在的那几条。

        **登记方是框架**（``plugin_id=""``）：变量池是框架级的，组件停了只停自己的
        显示与提醒（裁定③）。故这些规则不随任何组件启停。
        """
        if self._interaction is None:
            return
        wanted: "set[str]" = set()
        for spec in self.pool.declared():
            if spec.source != "ask" or spec.trigger is None:
                continue
            key = ask_key(spec.id)
            trigger = self._trigger_of(spec)
            if trigger is None:
                continue
            wanted.add(key)
            self._interaction.register(trigger, self._answer_writer(spec.id))
        for key in sorted(self._ask_keys - wanted):
            self._interaction.unregister_key(key)
        self._ask_keys = wanted
        if wanted:
            # 刚登记的规则里可能带着 `on_start` 条件（``when: [startup]``），而中枢那位
            # 锁存位是**一次性**的（``mark_started`` 在装配收尾就置过）。不重跑一遍的话，
            # 运行期新加 / 改过的 ask 变量要等到**下次启动**才会问 —— 用户当场看不到任何
            # 反应。重跑是安全的：`_evaluate` 出口有去重账挡着（当天问过的不再弹）。
            self._interaction.mark_started()

    def _trigger_of(self, spec) -> "core_interaction.TriggerSpec | None":
        """把一条 ask 声明翻成交互中枢认的 ``TriggerSpec``；**翻不出来返回 ``None``**。

        翻不出来的两种（各自记一条日志，不当成错误崩掉）：
        - ``when`` 与 ``conditions`` 都是空的 —— 空条件集恒为真，登记进去就是**立刻弹一次**，
          这不是用户写的那个意思（§2.5 明写「登记不进去」）；
        - 类型不是 ``time`` —— 浮层今天只实现了「填一个时刻」这一种问法
          （``core.interaction`` 只声明了 ``PROMPT_TIME``），别的类型弹出来是个没有输入框的窗。
        """
        when = spec.trigger.when if spec.trigger is not None else ()
        conditions = spec.trigger.conditions if spec.trigger is not None else ()
        if not when and not conditions:
            log.warning("变量 %s：触发时机与条件都是空的，不登记（空条件会立刻弹一次）", spec.id)
            return None
        if spec.type != expr.T_TIME:
            log.warning("变量 %s：%s 型的询问浮层还没有（今天只有时间型），这条不弹",
                        spec.id, spec.type)
            return None
        marks: "list[core_interaction.Condition]" = []
        if "startup" in when:
            marks.append(core_interaction.Condition(core_interaction.COND_ON_START))
        if conditions:
            # 条件那一路用**一个谓词**，而不是把每条 condition 翻成 ``state_is``：变量条件
            # 比的是**变量的值**（可能是派生算出来的），而 ``state_is`` 比的是状态机的取值
            # —— 两者只在「直接引一个 component 变量」时才重合，绕一层派生（
            # `off_time = start_time + 9h` 再拿它比）就翻不过去了。
            #
            # 重算时机挂在 `app.variables.changed` 上：`core.interaction.topics_of` 对
            # 填了 topic 的 predicate 条件照样订阅（那条注释写了为什么）。
            marks.append(core_interaction.Condition(
                core_interaction.COND_PREDICATE,
                topic=VARIABLES_CHANGED_TOPIC,
                predicate=self._predicate_of(spec),
            ))
        return core_interaction.TriggerSpec(
            plugin_id=FRAMEWORK_OWNER,
            prompt=core_interaction.PromptSpec(
                key=ask_key(spec.id),
                title=spec.prompt or f"{spec.name}？",
                kind=core_interaction.PROMPT_TIME,
                # 预填值走**可调用**（规则在启动时登记、窗可能几小时后才弹，见
                # ``PromptSpec`` 那条参数）：此刻的值必须此刻问，不能登记时算好。
                # **不挂 ``preview``**（2026-10-10 用户裁定）：询问就是询问，不带试算 ——
                # 旧模型那行「填 09:00 ⇒ 下班时间 17:27」是给翻页钟条目看的，
                # 挪进变量询问卡之后，弹「上班时间」却写着「下班时间 17:27」，
                # 用户读成「这条变量在替我算下班」。
                initial=partial(self.pool.ask_raw, spec.id),
            ),
            conditions=tuple(marks),
            # 「今天答过就不再问」：关掉没答的话每次启动都还留一次机会（用户口径是
            # 「每天问一次」，而 `once_per_day` 会把它变成「弹过就问不到了」，见
            # `core.interaction.DEDUPE_ANSWERED_DAY` 的说明）。
            dedupe=core_interaction.DEDUPE_ANSWERED_DAY,
        )

    def _predicate_of(self, spec) -> "Callable[[], bool]":
        """这一条 ask 的「条件成立了吗」——**每次问变量池**，判不出来按不成立。

        判不出来（依赖的变量当刻没值）在 ``pool`` 那侧是第三种结论（``ASK_UNDECIDED``），
        而交互中枢的条件只有真假两态 —— 这里取「不成立」：真值到了会有
        ``app.variables.changed`` 再来一次，那时它自然会变成成立。
        """
        def check() -> bool:
            self.pool.begin_pass()
            verdict = rules.matches_all(
                spec.trigger.conditions, self.pool.value, self.pool.type_of
            ) if spec.trigger is not None else None
            return verdict is True

        return check

    def _answer_writer(self, var_id: str) -> "Callable[[object], None]":
        """答案回写：写进按日账 ⇒ 开新一轮 ⇒ 强制广播（用户刚做的那一次必须看得见）。"""
        def write(result: object) -> None:
            if not getattr(result, "answered", False):
                return
            raw = str(getattr(result, "value", "") or "")
            if not self.pool.set_ask_value(var_id, raw):
                log.error("变量 %s 的值没能写进按日账（盘上没变，本次回答只活在内存里）", var_id)
                return
            self.pool.begin_pass()
            self._publish_if_changed(force=True)

        return write

    # ---------- 给界面 ----------

    def ask_now(self, var_id: str) -> bool:
        """用户按「测试」撞上 ask 型：**立刻弹一次**、不看条件也不看去重账。

        走中枢的 ``ask_now``（临时 key）：那条路本就是为「用户自己按出来的」设计的
        （见 ``InteractionCenter.ask_now``），真 key 那笔账一个字不动 ——
        「点一次测试 ⇒ 今天真正的那次不弹了」是用户会直接判成有病的重复。
        """
        if self._interaction is None:
            return False
        spec = self.pool.spec_of(var_id)
        if spec is None or spec.source != "ask" or spec.trigger is None:
            return False
        trigger = self._trigger_of(spec)
        if trigger is None:
            return False
        return bool(self._interaction.ask_now(trigger, self._answer_writer(var_id)))

    def probe(self, var_id: str) -> str:
        """**现在**把这一条算一遍，把结果（或算不出来的原因）答回来。

        与 :meth:`declared_rows` 差的正是**时机**：那一份取的是上一轮 ``tick`` 留下的
        结论，而「测试」要回答的是「此刻算出来是多少」—— 用户往往是刚改完表达式就按它，
        复读上一轮的缓存等于拿一个还没生效的旧值答他（同 ``plugin_config`` 那条「取数口
        一律现问」的纪律）。故先 ``begin_pass()`` 清掉这一轮的缓存再问。

        算不出来时**不返回空串**：由 :meth:`VariablePool.render` 给「待询问 / 未收到 /
        未定义」这三个词之一 —— 它们说的是「为什么没有值」，三句都不一样。
        """
        self.pool.begin_pass()
        return self.pool.render(var_id)

    def declared_rows(self) -> "list[tuple[object, str]]":
        """设置页列清单要的那份：``[(声明, 此刻的渲染值)]``，**内置三只排在最后**。

        渲染值在这里一次取齐（而不是让页面逐条回问池）：页面画的每一行都是同一刻的值，
        逐条问会在跨秒那一刻画出「上半屏是 17:27、下半屏是 17:28」。
        """
        rows = [(s, self.pool.render(s.id)) for s in self.pool.declared()]
        rows += [(s, self.pool.render(s.id)) for s in self.pool.specs() if s.source == "framework"]
        return rows
