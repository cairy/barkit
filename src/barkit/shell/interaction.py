"""shell/interaction：条件触发询问的**中枢与浮层**（方案批 2）。

``core/interaction`` 管「条件怎么算」与「问过没问过」（纯逻辑、零 Qt）；这里管剩下
两件**碰窗口**的事：**什么时候弹**与**弹出来长什么样**。

三件事的边界（改之前先读）：

- **中枢**（:class:`InteractionCenter`）：订阅信号 → 刷锁存位 → 合取求值 → 入队 →
  串行弹。**同一时刻只弹一个**：两条规则同时成立时并排弹两个窗，用户只看得见先到的
  那个、后一个的答案永远拿不到，而他还以为自己答完了。
- **排队是中枢的事，不是落点的事**：询问浮层走既有的 :meth:`Overlay.show_toast`
  （与 :meth:`Overlay.show_and_run`、``show_docked`` 三个出口互不干扰），**不为它新开
  第四个出口** —— 落点一旦分散到调用方手里，就又变回各摆各的。
  **出口的选定 2026-09-27 改过一次**：此前走 ``show_and_run``（居中偏上 30%），那是
  「位置由业务自己定」的那一档，而询问浮层根本没有业务在定它的位置 —— 它是**框架中枢**
  弹的；且它占屏最久（2 分钟），落在正中偏上正压着用户在读的那一块。改走提示出口后
  落在右上角那一列（常驻钟之下）。判据 → ``docs/log/2026-09-27-询问浮层落点归右上角.md``。
- **问什么由插件给**（``PromptSpec``），**控件由本模块用 ``ui_kit`` 画**：插件不拼
  样式、不自弹 ``QMessageBox``（零模态）。
- **两个时刻、两个回调**：``on_result`` 是「用户怎么答的」，``on_shown`` 是「这条提醒
  真的弹出来了」（2026-10-10 加，给「到点跑一条命令」这类**属于提醒本身而不是答案**的
  副作用用）。四种结局里没有一样能反过来证「弹过」—— 尤其「关闭」这一点上两者相反，
  见 :meth:`InteractionCenter.register`。

**去重只挡弹窗（推），不挡用户自己动手（拉）**：本中枢只管「系统来打扰你」那一侧。
用户关掉浮层之后想补填，走的是插件自己的入口（设置页 / 面板行），那条路**不经过这里**，
也不查去重账 —— 详见方案 §2.2b。
"""

from __future__ import annotations

import logging
from datetime import datetime
from functools import partial
from typing import Callable

from PySide6.QtCore import Qt, QTime, Signal
from PySide6.QtWidgets import QAbstractSpinBox, QHBoxLayout, QLabel, QTimeEdit, QVBoxLayout

from barkit.core import interaction as core
from barkit.core.events import PANEL_OPENED_TOPIC
from barkit.shell.design import metrics
from barkit.shell.overlay import (
    DISMISS_CLOSED,
    DISMISS_COMPLETED,
    DISMISS_TIMEOUT,
    Overlay,
)
from barkit.shell.ui_kit import apply_text_role, tool_button

log = logging.getLogger(__name__)

#: 询问浮层的宽度。**只钉宽、不钉高** —— 高度由内容（几行选项）决定，写死会在选项
#: 多出一行时把按钮挤出可视区。落位只用到宽（水平居中），故不钉高不影响定位。
_PROMPT_WIDTH = 360

#: 中枢的复检节律（秒）：``daily_at`` 到点与「稍后」到期都靠它扫。挂在框架唯一的
#: 心跳上（不新起定时器）—— 30 秒的粒度对「稍后 30 分钟」足够，也不必每秒重算。
BEAT_INTERVAL_S = 30.0

#: 用户自己按出来的那一次（:meth:`InteractionCenter.ask_now`）在真 key 后面加的后缀。
#: **临时 key 必须与真 key 分开**：两笔账一旦共用，「点一次测试 ⇒ 今天真正的那次不弹了」
#: —— 用户只会判成「这功能有病」，而看代码的人查的是另一条规则。
MANUAL_KEY_SUFFIX = "#now"


class PromptOverlay(Overlay):
    """一次询问的浮层（零模态：不阻塞、不激活 app、到点自己没）。

    ``finished(kind, value)``：**只发一次**，四种来由（答了 / 超时 / 被关 / 稍后）
    都归到这一个出口，业务读 ``kind`` 区分。超时与被关由基类的退场信号兜住 ——
    让调用方自己接两个信号，迟早会漏接一个。

    :param can_snooze: 还让不让「稍后」（由中枢按当天的「稍后」次数给）：
        超限时不画那个按钮 —— **画一个点了没用的按钮是骗人**。
    """

    finished = Signal(str, object)

    def __init__(self, spec: "core.PromptSpec", can_snooze: bool = True, parent=None) -> None:
        duration = spec.timeout_s * 1000 if spec.timeout_s and spec.timeout_s > 0 else None
        super().__init__(parent, duration_ms=duration)
        self.setFixedWidth(_PROMPT_WIDTH)
        self._spec = spec
        self._can_snooze = can_snooze
        self._emitted = False
        # 基类的退场信号先接上，超时 / 被外部关闭才有出口（连在 _build_ui 之前：
        # 构造期任何一步失败都不该让这次询问「弹出来了却没人收答案」）。
        self.dismissed.connect(self._on_dismissed)
        self._build_ui()

    # ---------- UI ----------

    def _build_ui(self) -> None:
        spec = self._spec
        layout = self.content_layout

        title = QLabel(spec.title, self)
        apply_text_role(title, "overlay_title")
        title.setWordWrap(True)
        layout.addWidget(title)

        if spec.message:
            msg = QLabel(spec.message, self)
            apply_text_role(msg, "overlay_body")
            msg.setWordWrap(True)
            layout.addWidget(msg)

        if spec.kind == core.PROMPT_TIME:
            self._build_time()
        elif spec.kind == core.PROMPT_CHOICE:
            # **没有选项的 choice 是一张纯告知卡**（「不需要完成」的任务到点只报一下、
            # 没有一个可点的「完成」）：它是合法形状，不画按钮即可 —— 出口由页脚那两枚
            # （稍后 / 关闭）兜住，不是「问法没实现」。
            if spec.options:
                self._build_options()
        else:
            # 第一版只做 time / choice（方案待裁决 2）：其余问法今天没人用，不做控件，
            # 但**不许静默当没发生过** —— 记日志，并按「只有确定可点」画完，
            # 免得弹出一个没有任何出口的窗。
            log.warning("未实现的询问类型 %r（%s），按「只能确定」处理", spec.kind, spec.key)
        self._build_foot()

    def _build_time(self) -> None:
        """填一个时间：**时间格与「确定」同一行** —— 「填 × 按确定」是一次动作，拆成
        上下两行是把一步走成两步。

        预填值问 ``spec.initial``（**构造这一刻才求值**，见 :class:`PromptSpec`）；没给就
        回落到当前时刻 —— 「今天几点上班」最常见的答案就是「现在」，让用户先去改一个
        空框是多余的一步。

        ``options`` 是快捷值（点一下即**直接作答** —— 它本身就是完整答案，不该再要一次
        确认）；今天没人给，留着是给「几个候选值」那类问法用的。
        """
        row = QHBoxLayout()
        row.setSpacing(metrics.GAP_SM)
        self._editor = QTimeEdit(self)
        self._editor.setDisplayFormat("HH:mm")
        # 不画步进箭头：值已预填、直接改数字就行，箭头挤在右缘与「确定」打架
        # （且它只调时或只调分，点两下才够一分钟 —— 有它没它都别扭，那就去掉）。
        self._editor.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self._editor.setFixedHeight(metrics.CONTROL_H)
        # 这一格只要数字：给输入框架构一个明示的提示，多数输入法会据此直接落数字、
        # 不起中文组字（中文输入法下填时间不至于被候选窗吃掉）。只是**提示**，不是开关。
        self._editor.setInputMethodHints(Qt.InputMethodHint.ImhDigitsOnly)
        self._editor.setTime(self._initial_time())
        self._editor.timeChanged.connect(self._refresh_preview)
        # 输入格**撑满**这一行、确定钉在右端：三行内容（标题 / 输入 / 预览）左右边缘
        # 对齐成一张卡；输入格缩在左侧、右边留一大块空白，看起来像没排完。
        row.addWidget(self._editor, 1)
        ok = tool_button("确定", "primary", parent=self)
        ok.clicked.connect(self._on_ok)
        row.addWidget(ok)
        self.content_layout.addLayout(row)

        # 预览行：**常建**（没文本也占着那一行）—— 它一出现就把浮层撑高，而提示栈是
        # 按「已在屏上那些的高度」叠位置的，弹出来之后再长一截会压到下面那条。
        self._preview_row = QLabel("", self)
        apply_text_role(self._preview_row, "caption")
        self._preview_row.setWordWrap(False)
        self._preview_row.setFixedHeight(max(self._preview_row.sizeHint().height(), 16))
        self.content_layout.addWidget(self._preview_row)
        self._refresh_preview()

    def _initial_time(self) -> "QTime":
        """预填值：问 ``spec.initial``（**此刻**求值），拿不到 / 不是时刻 ⇒ 当前时刻。"""
        raw = ""
        if self._spec.initial is not None:
            try:
                raw = self._spec.initial() or ""
            except Exception:  # noqa: BLE001 —— 预填值算不出来不该弹不出窗
                log.exception("预填值回调异常（按当前时刻处理）：%s", self._spec.key)
        parsed = _parse_hhmm(raw)
        if parsed is None:
            now = datetime.now()
            return QTime(now.hour, now.minute)
        return QTime(*parsed)

    def _refresh_preview(self) -> None:
        """预览行：拿此刻填的值问插件「填完会变成什么样」；空串 ⇒ 这一行留白。"""
        if self._spec.preview is None or not hasattr(self, "_preview_row"):
            return
        value = self._editor.time().toString("HH:mm") if hasattr(self, "_editor") else ""
        try:
            text = self._spec.preview(value) or ""
        except Exception:  # noqa: BLE001 —— 预览是附加信息，算不出来就留白
            log.exception("预览回调异常（这一行留白）：%s", self._spec.key)
            text = ""
        self._preview_row.setText(str(text))

    def _build_options(self) -> None:
        """几个按钮选一个：每个选项一枚按钮，**竖排**（横排在文案长度不一时会挤成一团）。"""
        box = QVBoxLayout()
        box.setSpacing(metrics.GAP_SM)
        for opt in self._spec.options:
            btn = tool_button(opt, "pill-ghost", expanding=True, parent=self)
            btn.clicked.connect(partial(self._answer, core.RESULT_ANSWERED, opt))
            box.addWidget(btn)
        self.content_layout.addLayout(box)

    def _build_foot(self) -> None:
        # 退场动作**靠右**：与输入行右端的「确定」同列（一张卡的动作都在右缘），
        # 居中的话与上面各行的左对齐打架 —— 两套对齐叠在一张卡上就是「丑」的来源。
        row = QHBoxLayout()
        row.addStretch(1)
        if self._spec.later_minutes > 0 and self._snooze_allowed:
            later = tool_button(
                f"稍后 {self._spec.later_minutes} 分钟", "pill-ghost", size="inline", parent=self
            )
            later.clicked.connect(self._on_later)
            row.addWidget(later)
        close = tool_button("关闭", "pill-ghost", size="inline", parent=self)
        close.clicked.connect(partial(self._answer, core.RESULT_DISMISSED, None))
        row.addWidget(close)
        self.content_layout.addLayout(row)

    # ---------- 作答 ----------

    @property
    def _snooze_allowed(self) -> bool:
        """中枢在构造期给的结论（超了当天上限就不画那个按钮），见类 docstring。"""
        return self._can_snooze

    def _on_ok(self) -> None:
        value = self._editor.time().toString("HH:mm") if hasattr(self, "_editor") else None
        self._answer(core.RESULT_ANSWERED, value)

    def _on_later(self) -> None:
        self._answer(core.RESULT_LATER, None)

    def _answer(self, kind: str, value: "object") -> None:
        if self._emitted:
            return
        self._emitted = True
        self.dismiss(DISMISS_COMPLETED)
        self.finished.emit(kind, value)

    def _on_dismissed(self, reason: str) -> None:
        """退场兜底：超时 / 被外部关掉（app 退出）也要把结论发出去。

        **超时与被关都算未回答**，但**「稍后」不算** —— 那是用户主动推迟，中枢要据此
        记一笔到期时刻、到时候再弹一次。
        """
        if self._emitted:
            return
        self._emitted = True
        kind = core.RESULT_TIMEOUT if reason == DISMISS_TIMEOUT else core.RESULT_DISMISSED
        self.finished.emit(kind, None)


def _parse_hhmm(value: str) -> "tuple[int, int] | None":
    """``"HH:MM"`` → ``(时, 分)``；非法返回 ``None``（不猜）。"""
    if not isinstance(value, str) or len(value) < 4 or ":" not in value:
        return None
    hh, _, mm = value.partition(":")
    try:
        hour, minute = int(hh), int(mm[:2])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


class InteractionCenter:
    """条件触发询问的中枢：**订阅 → 求值 → 排队 → 串行弹**。

    插件只交一个 ``TriggerSpec``（何时问 + 问什么）和一个答案回调；求值 / 去重 /
    排队 / 弹窗全在这里，插件不碰窗口。

    - **求值时机**：不是「启动时查一次」，而是**任一信号到达就重算一遍合取**
      （启动时路由场景还是 ``unknown``，只查一次的话「场景 = Office」永远成立不了）。
    - **锁存是「发生过」不是「此刻仍是」**：到家之后场景变回 ``home``，白天那次
      「到过 Office」不该被抹掉 —— 不再重弹由**去重账**负责，两件事各管一段。
    - **排队**：同一 key 不重复入队；前一条退场（答了 / 超时 / 关掉 / 稍后）才弹下一条。
    """

    def __init__(
        self,
        events,
        heartbeat=None,
        ledger: "core.DedupeLedger | None" = None,
    ) -> None:
        self._events = events
        self._ledger = ledger or core.DedupeLedger()
        self._triggers: "dict[str, core.Trigger]" = {}
        self._callbacks: "dict[str, Callable]" = {}
        #: key → 「这条提醒弹出来了」的回调（与 ``_callbacks`` 同生共死，见 :meth:`register`）。
        self._on_shown: "dict[str, Callable]" = {}
        self._queue: "list[str]" = []
        self._current: "tuple[str, PromptOverlay] | None" = None
        #: 「用户自己按出来的」那些临时 key（见 :meth:`ask_now`）：答案收完即拆，
        #: 不留在 ``_triggers`` 里变成一条永远成立、每次心跳都要求值的幽灵规则。
        self._manual: "set[str]" = set()
        #: topic → 中枢自己那个 handler（退订要用同一个对象）
        self._topic_handlers: "dict[str, Callable]" = {}
        #: topic → 关心它的 key 集合（一个 topic 多个规则共享一份订阅）
        self._keys_by_topic: "dict[str, set]" = {}
        self._heartbeat = heartbeat
        self._ticker = None
        if heartbeat is not None:
            self._ticker = heartbeat.add_ticker(
                BEAT_INTERVAL_S, self._on_beat, key="interaction"
            )

    # ---------- 登记 ----------

    def register(
        self,
        spec: "core.TriggerSpec",
        on_result: "Callable | None" = None,
        on_shown: "Callable | None" = None,
    ) -> None:
        """登记 / 重登记一条规则（重登记 = 整条替换，锁存位归零）。

        **锁存位必须归零**：插件改了配置（如换触发场景）后重登记，带着旧锁存位的实例
        会「一上来就触发」—— 那个旧信号对应的是**已经不存在的条件**。

        ``on_shown`` 与 ``on_result`` 是**两个时刻**，不是一件事的两种写法：

        - ``on_result`` ＝ 「用户怎么答的」（答了 / 超时 / 关掉 / 稍后）—— 答案。
        - ``on_shown`` ＝ 「这条提醒**真的弹出来了**」—— 登过场。它在
          :meth:`_pump` 里与 ``mark_asked`` 同一处发出。

        为什么不能拿 ``on_result`` 顶替：**四种结局里没有一样能反过来证「弹过」**。
        「关掉」这一点上两者甚至相反 —— 用户点「关闭」是「看过但不想答」，而 app 退出 /
        组件被撤下时中枢也会 ``dismiss(DISMISS_CLOSED)`` 收走那条浮层（用户根本没看见），
        两者在 ``on_result`` 里是同一个 ``RESULT_DISMISSED``。要么漏跑、要么在退出那一刻
        误跑一条旁路动作，两条都不该。

        **手动那一次不在此列**：:meth:`ask_now` 的临时 key 不挂 ``on_shown``
        （用户自己按出来的，没有「提醒把我叫醒」这回事）。
        """
        key = spec.prompt.key
        self._triggers[key] = core.Trigger(spec)
        if on_result is not None:
            self._callbacks[key] = on_result
        if on_shown is not None:
            self._on_shown[key] = on_shown
        # 队列里那条若是同一个 key，它绑的是旧声明：丢掉，等下一次求值重排。
        if key in self._queue:
            self._queue.remove(key)
        # 订阅面也要**整条替换**：先摘掉该 key 此前挂在哪些 topic 上，再按新声明重新挂。
        # 少了这一步，「重登记 = 整条替换」只对 ``_triggers`` 成立 —— 旧 topic 的
        # ``_keys_by_topic`` 里仍留着这个 key（它仍然活着，``_prune_topics`` 不会清），
        # 于是**已经被换掉的那个条件**再来信号时，仍会拿新声明去求值并弹一次。
        for topic in list(self._keys_by_topic):
            self._keys_by_topic[topic].discard(key)
        self._prune_topics()
        self._sync_topics(key, spec)

    def ask_now(self, spec: "core.TriggerSpec", on_result: "Callable | None" = None) -> bool:
        """**立刻弹一次**：不看条件、也不看去重账 —— 给「现在问我」这类**用户自己按**的入口。

        两道闸都是为「系统来打扰你」设计的：条件的信号（启动 / 到某场景）用户按按钮时
        等不到，而「每天一次」那笔账挡的是**推**、不挡**拉**（见模块 docstring）。要绕开
        就绕开得干净：**临时 key**（真 key ＋ :data:`MANUAL_KEY_SUFFIX`）挂一条「无条件 +
        每次都问」的规则，弹过 / 答过都记在临时 key 上，真 key 那笔账一个字不动。

        答案**照旧走登记时那个回调**（本中枢不认业务），故「测试的答案算不算数」由插件
        那一侧决定 —— 它给的回调写盘，就算数。

        :return: 恒 ``True``（入队即算接住；屏上已有询问时排在它后面，见 :meth:`_enqueue`）。
        """
        key = f"{spec.prompt.key}{MANUAL_KEY_SUFFIX}"
        self._triggers[key] = core.Trigger(
            core.TriggerSpec(
                plugin_id=spec.plugin_id,
                prompt=spec.prompt,
                conditions=(),                  # 无条件 ⇒ 恒成立（evaluate 空条件为真）
                dedupe=core.DEDUPE_ALWAYS,      # 不看账 ⇒ 一定放行
            )
        )
        if on_result is not None:
            self._callbacks[key] = on_result
        self._manual.add(key)
        self._enqueue(key)
        return True

    def _forget_manual(self, key: str) -> None:
        """拆掉一条临时规则（答案收完 / 插件停用 / 中枢收尾）。"""
        if key not in self._manual:
            return
        self._manual.discard(key)
        self._triggers.pop(key, None)
        self._callbacks.pop(key, None)
        self._on_shown.pop(key, None)
        # 临时 key 那笔账一起作废：留着会在账本里攒一串 ``…#now``，谁也读不到它。
        self._ledger.forget(key)

    def unregister_key(self, key: str) -> None:
        """撤下**一条**规则（按 key）。

        与 :meth:`unregister` 的分工：那个按 ``plugin_id`` 整批撤（组件停用 / 卸载），
        这个撤单条 —— 给「规则还在、但它挂的那个声明没了」这一族用（变量池重载就是：
        用户删掉一个 ask 变量，它那条询问规则得跟着走）。

        **正在弹的那一条不关**：它可能是用户此刻正在填的窗，而「声明改了」不该把用户
        填了一半的东西吞掉。撤的只是规则本身（此后不再重算、不再入队），答案回来时
        :meth:`_on_finished` 照旧记账、照旧找回调 —— 找得到就调，找不到就什么也不做。
        """
        self._triggers.pop(key, None)
        self._callbacks.pop(key, None)
        self._on_shown.pop(key, None)
        self._manual.discard(key)
        if key in self._queue:
            self._queue.remove(key)
        self._prune_topics()

    def unregister(self, plugin_id: str) -> None:
        """撤下某插件的全部规则（禁用 / 卸载时调用，免得留下孤儿规则与悬空回调）。

        正在弹的那一条**当场关掉**：插件都停了，它的答案已经没人接 —— 留一个没人收的
        窗在屏上，比不弹更糟。
        """
        keys = [k for k, t in self._triggers.items() if t.spec.plugin_id == plugin_id]
        for key in keys:
            self._triggers.pop(key, None)
            self._callbacks.pop(key, None)
            self._on_shown.pop(key, None)
            self._manual.discard(key)
            if key in self._queue:
                self._queue.remove(key)
            if self._current is not None and self._current[0] == key:
                self._current[1].dismiss(DISMISS_CLOSED)
        self._prune_topics()

    def _sync_topics(self, key: str, spec: "core.TriggerSpec") -> None:
        for topic in core.topics_of(spec):
            self._subscribe(topic, key)
        if any(c.kind == core.COND_PANEL_OPENED for c in spec.conditions):
            self._subscribe(PANEL_OPENED_TOPIC, key)

    def _subscribe(self, topic: str, key: str) -> None:
        if topic not in self._topic_handlers:
            handler = partial(self._on_topic, topic)
            self._events.subscribe(topic, handler)
            self._topic_handlers[topic] = handler
        self._keys_by_topic.setdefault(topic, set()).add(key)

    def _prune_topics(self) -> None:
        """撤掉不再有规则的订阅（§8.2.4 无泄漏：退订与登记成对）。"""
        alive = {k for k in self._triggers}
        for topic in list(self._keys_by_topic):
            self._keys_by_topic[topic] &= alive
            if self._keys_by_topic[topic]:
                continue
            handler = self._topic_handlers.pop(topic, None)
            if handler is not None:
                self._events.unsubscribe(topic, handler)
            self._keys_by_topic.pop(topic, None)

    # ---------- 信号 ----------

    def mark_started(self) -> None:
        """程序装配完成（一次性）：置位所有 ``on_start`` 条件并重算。

        由组合根在**插件都 start 过之后**调用 —— 早了会漏掉插件在 ``start()`` 里
        登记的规则（那些规则还没进中枢，这一位就没人在听）。
        """
        for key, trig in self._triggers.items():
            for index, cond in enumerate(trig.spec.conditions):
                if cond.kind == core.COND_ON_START:
                    trig.latches.set(index)
            self._evaluate(key)

    def _on_topic(self, topic: str, payload) -> None:
        """任一订阅信号到达：刷对应的锁存位，重算**关心这个 topic 的**那些规则。"""
        for key in tuple(self._keys_by_topic.get(topic, ())):
            trig = self._triggers.get(key)
            if trig is None:
                continue
            for index, cond in enumerate(trig.spec.conditions):
                if cond.kind == core.COND_STATE_IS and cond.topic == topic:
                    if core.state_matches(cond, payload):
                        trig.latches.set(index)
                elif cond.kind == core.COND_PANEL_OPENED and topic == PANEL_OPENED_TOPIC:
                    trig.latches.set(index)
            self._evaluate(key)

    def _on_beat(self) -> None:
        """心跳复检：``daily_at`` 到点与「稍后」到期都没有信号可听，只能按时扫。"""
        for key in tuple(self._triggers):
            self._evaluate(key)

    # ---------- 求值 / 排队 ----------

    def _evaluate(self, key: str) -> None:
        """条件成立 **且** 去重账放行 ⇒ 入队。两件事分开判（混在一起就没法测了）。"""
        trig = self._triggers.get(key)
        if trig is None or not trig.due():
            return
        if not self._ledger.should_ask(key, trig.spec.dedupe):
            return
        self._enqueue(key)

    def _enqueue(self, key: str) -> None:
        if key in self._queue:
            return
        if self._current is not None and self._current[0] == key:
            return
        self._queue.append(key)
        self._pump()

    def _pump(self) -> None:
        """屏上没有询问浮层时才弹下一个。**出队时再判一次**（入队到出队之间条件可能
        已经不成立，或另一条已经把它答掉了），不成立就丢、继续往下找。"""
        while self._current is None and self._queue:
            key = self._queue.pop(0)
            trig = self._triggers.get(key)
            if trig is None or not trig.due():
                continue
            if not self._ledger.should_ask(key, trig.spec.dedupe):
                continue
            break
        else:
            return
        # 弹的瞬间记账（不等回答）：浮层是零模态的，用户可能直接走开。
        self._ledger.mark_asked(key)
        # 「弹出来了」与记账同一处（见 ``register`` 的 ``on_shown``）：两条都是「这一条
        # 要弹了」的结论，故同在 ``show_toast`` **之前**。异常在 :meth:`_notify_shown`
        # 里就地隔离 —— 一个插件的旁路回调（如"到点跑一条命令"）炸了，不该把这条提醒
        # 本身也赔进去（与 ``_on_finished`` 里那个 ``except`` 同一条口径）。
        self._notify_shown(key)
        overlay = PromptOverlay(
            trig.spec.prompt,
            can_snooze=self._ledger.can_snooze(key),
        )
        overlay.finished.connect(partial(self._on_finished, key))
        self._current = (key, overlay)
        #: 落点走**提示出口**（右上角那一列、排在常驻钟之下）：位置归框架，与
        #: autotype 倒计时卡那种「业务自己定位置」的浮层分开（见模块 docstring）。
        #: 它自己只剩「宽」是业务口径（``_PROMPT_WIDTH``）—— 高度由内容给，
        #: 与落位无关（落位只用宽；叠放只读**已在屏上**那些浮层的高度，那时已量准）。
        #
        # 弹不出来必须把位置让出来：``_current`` 一占住，``_pump`` 的准入
        # （``while self._current is None``）就永远不成立，此后**所有**询问一条都
        # 弹不出来；而去重账上一步已经记了「今天问过」，连重试的机会都没有。
        try:
            overlay.show_toast()
        except Exception:  # noqa: BLE001
            log.exception("询问浮层弹出失败，让位给下一条：%s", key)
            overlay.deleteLater()
            self._current = None
            self._pump()

    def _notify_shown(self, key: str) -> None:
        """告诉插件「这一条提醒弹出来了」（见 :meth:`register` 的 ``on_shown``）。

        **就地隔离异常**：调用点在主线程的心跳 / 信号回调链上，让一个插件的旁路动作把
        异常抛出去，赔的是**整个中枢**（``_pump`` 之后那条路全部不执行）。与
        :meth:`_on_finished` 里那个 ``except`` 同一条口径。
        """
        callback = self._on_shown.get(key)
        if callback is None:
            return
        try:
            callback()
        except Exception:  # noqa: BLE001 —— 旁路动作不该拖垮提醒本身
            log.exception("提醒登场回调异常：%s", key)

    # ---------- 收答案 ----------

    def _on_finished(self, key: str, kind: str, value: "object") -> None:
        self._current = None
        trig = self._triggers.get(key)
        if kind == core.RESULT_ANSWERED:
            self._ledger.mark_answered(key)
        elif kind == core.RESULT_LATER and trig is not None:
            self._ledger.snooze(key, trig.spec.prompt.later_minutes)
        callback = self._callbacks.get(key)
        if callback is not None:
            try:
                callback(core.InteractionResult(kind == core.RESULT_ANSWERED, value, kind))
            except Exception:  # noqa: BLE001 —— 单个插件的回调不该拖垮整条队列
                log.exception("交互回调异常：%s", key)
        # 用户自己按的那一次：答案收完就拆（**在 `_pump` 之前**：留着它就是一条永远
        # 成立的规则，下一拍求值又会弹一遍）。
        self._forget_manual(key)
        self._pump()

    def note_answered(self, key: str) -> None:
        """「拉」的那条路用：用户自己在插件入口补填完成 ⇒ 当天不再弹。

        与浮层答完那次记的是**同一笔账**，只是入口不同（那边是中枢收的答案，这边是
        插件自己的入口）。补填之后又被弹一遍，是用户会直接判为「这功能有病」的那类
        重复 —— 故补填这一侧必须也叫一声。
        """
        self._ledger.mark_answered(key)

    # ---------- 收尾 / 自证 ----------

    @property
    def queue(self) -> "list[str]":
        """此刻排在队里的 key（给测试与诊断读，生产不消费）。"""
        return list(self._queue)

    @property
    def current_key(self) -> "str | None":
        return None if self._current is None else self._current[0]

    def dispose(self) -> None:
        """退出收尾：关掉在屏上的那条、撤订阅、摘心跳。

        心跳那条**必须显式摘**（``remove_ticker``）：心跳持有回调引用，只把本地句柄
        置空等于「中枢以为自己退了、心跳还在每 30 秒叫它一次」。
        """
        if self._current is not None:
            self._current[1].dismiss(DISMISS_CLOSED)
            self._current = None
        self._queue.clear()
        self._triggers.clear()
        self._callbacks.clear()
        self._on_shown.clear()
        self._manual.clear()
        for topic in list(self._topic_handlers):
            self._events.unsubscribe(topic, self._topic_handlers.pop(topic))
        self._keys_by_topic.clear()
        if self._heartbeat is not None and self._ticker is not None:
            self._heartbeat.remove_ticker(self._ticker)
        self._heartbeat = None
        self._ticker = None
