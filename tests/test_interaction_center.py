"""tests/test_interaction_center：条件触发询问的**中枢与浮层**（``shell/interaction``）。

守的四件事，每件对应一条今天真实会走歪的路：

- **同一时刻只弹一个**（两条规则同时成立 ⇒ 排队串行，不是并排弹两个窗）；
- **锁存合取**（启动时场景还是 unknown ⇒ 只在启动时求值一次的规则永远触发不了）；
- **超时 / 被关 = 未回答**（零模态：不阻塞，没人理就自己没了）；
- **「稍后」是推迟不是拒绝**（清掉当天标记 ⇒ 到点再来一次；且当天有上限）。

``core`` 那一半（条件求值 / 去重账）在 ``test_interaction.py``，两个文件不重叠。
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QTime
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QPushButton, QTimeEdit

from barkit.core import interaction as core
from barkit.core.events import PANEL_OPENED_TOPIC, EventBus, ROUTE_PROFILE_TOPIC
from barkit.core.heartbeat import Heartbeat
from barkit.shell.design import metrics
from barkit.shell.interaction import (
    MANUAL_KEY_SUFFIX,
    InteractionCenter,
    PromptOverlay,
)
from barkit.shell.overlay import DISMISS_CLOSED, DISMISS_TIMEOUT, Overlay, live_docked, live_toasts

from barkit.contribution.specs import StateReport


def _trigger(key, conditions, dedupe=core.DEDUPE_DAY, later=0, kind=core.PROMPT_TIME,
             initial=None, preview=None):
    return core.TriggerSpec(
        plugin_id="clock",
        prompt=core.PromptSpec(
            key=key,
            title="今天几点上班？",
            kind=kind,
            options=("现在",),
            later_minutes=later,
            initial=initial,
            preview=preview,
        ),
        conditions=conditions,
        dedupe=dedupe,
    )


def _report(state="active", summary="Office"):
    return StateReport(state, summary, updated_at=0.0)


def _overlay_of(center: InteractionCenter):
    """中枢此刻在屏上那个浮层（None = 没在弹）。"""
    return None if center._current is None else center._current[1]


def _buttons(overlay: PromptOverlay) -> "list[str]":
    return [
        b.text()
        for b in overlay.findChildren(QPushButton)
    ]


@pytest.fixture(autouse=True)
def _clean_overlays(qtbot):
    """每条用例收干净它弹出来的浮层 —— 询问自 2026-09-27 起**进提示栈**了。

    提示栈（``_TOASTS``）与常驻列（``_DOCKED``）是**模块级**状态：漏一条不收，后面弹出来
    的东西就排在它下面 —— 实测把后续每条提示顶到屏底（``y=760``），而且**跨文件也串**
    （``test_overlay`` 的叠放 / 补位断言按「栈里只有它自己」算位置，先跑本文件再跑它必红）。

    依赖 ``qtbot`` 只有一个目的：夹具的拆除是「后建先拆」⇒ 本夹具先于 ``qtbot`` 拆，
    那时浮层还活着（``qtbot`` 一拆，注册过的控件已被销毁，再 ``dismiss`` 会 C++ 已删）。

    **收到空为止、不是收一遍**：中枢是串行的，关掉在屏上那条会让队里下一条**当场补位**
    弹出来（``_pump``），只遍历一次快照就会把它漏在栈里 —— 那正是本夹具要防的东西。
    上限只是防手滑写死循环，正常最多两轮（队里剩几条就几轮）。
    """
    yield
    for _ in range(20):
        live = live_toasts() + live_docked()
        if not live:
            break
        for ov in live:
            ov.dismiss(DISMISS_CLOSED)


# ── 排队：同一时刻只弹一个 ────────────────────────────────────────────

def test_two_rules_fire_at_once_but_only_one_overlay(qtbot, tmp_path):
    bus = EventBus()
    center = InteractionCenter(bus, ledger=core.DedupeLedger(tmp_path / "s.json"))
    center.register(_trigger("a", (core.Condition(core.COND_ON_START),)))
    center.register(_trigger("b", (core.Condition(core.COND_ON_START),)))
    center.mark_started()

    assert center.current_key == "a"
    assert center.queue == ["b"]

    # 前一条退场（答了）⇒ 后一条补位，且此刻仍然只有一个在屏上
    first = _overlay_of(center)
    qtbot.addWidget(first)
    first._answer(core.RESULT_ANSWERED, "09:12")
    assert center.current_key == "b"
    assert center.queue == []


def test_same_key_does_not_queue_twice(qtbot, tmp_path):
    """同一条规则重复求值（多个信号连来）不该在队里排两次 —— 那会变成「答完又弹一遍」。

    两个前提，缺一条就变成**假测试**（2026-09-24 反向验证抓到两次）：

    1. **必须走「每次都问」那档**：默认的「每天一次」在弹出瞬间就记了账，第二次求值被
       **去重账**挡掉，根本到不了入队那一步 —— 那样判的是去重不是排队。
    2. **被重复求值的那个 key 必须在队里、且不在屏上**：还在屏上时挡住它的是另一条
       防护（「正在弹的不入队」），同样走不到「队里排两次」这一步。

    故这里是两条规则：a 在弹、b 在队，再给 b 来一次信号。
    """
    bus = EventBus()
    center = InteractionCenter(bus, ledger=core.DedupeLedger(tmp_path / "s.json"))
    center.register(
        _trigger("a", (core.Condition(core.COND_ON_START),), dedupe=core.DEDUPE_ALWAYS)
    )
    center.register(
        _trigger("b", (core.Condition(core.COND_ON_START),), dedupe=core.DEDUPE_ALWAYS)
    )
    center.mark_started()
    assert center.current_key == "a", "前置：a 在弹、b 在队"
    assert center.queue == ["b"]

    center._evaluate("b")  # b 已在队里，同一个信号又来一次
    assert center.queue == ["b"], "队里排两次 ⇒ 答完 b 还要再弹一遍 b"


def test_failed_overlay_does_not_block_the_queue(monkeypatch, tmp_path):
    """弹不出来必须让位：占着 ``_current`` ⇒ 此后**所有**询问一条都弹不出来。

    判据写在「中枢有没有空出来」上，不去查内部字段：真实的症状是屏上什么都没有、
    而队列里那几条（以及之后任何一天的任何一条）再也不会出现，且去重账已经记了
    「今天问过」，连重试的机会都没有。
    """

    def _boom(self):
        raise RuntimeError("没有可用屏幕（离屏 / 屏幕被拔了）")

    monkeypatch.setattr(PromptOverlay, "show_toast", _boom)
    bus = EventBus()
    center = InteractionCenter(bus, ledger=core.DedupeLedger(tmp_path / "s.json"))
    center.register(_trigger("a", (core.Condition(core.COND_ON_START),)))
    center.register(_trigger("b", (core.Condition(core.COND_ON_START),)))
    center.mark_started()

    assert center.current_key is None, "弹失败后仍占着位 ⇒ 队列被永久堵死"
    assert center.queue == []


# ── 落点：中枢弹的东西，位置归框架 ────────────────────────────────────

def _avail():
    """光标所在屏的可用区（与浮层落位同源，不写死分辨率）。"""
    screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
    return screen.availableGeometry()


def test_the_prompt_lands_in_the_top_right_column(qtbot, tmp_path):
    """询问浮层落在**右上角那一列**（常驻钟之下），不是居中偏上 30%。

    2026-09-27 改的口径：原先走 ``show_and_run``（居中偏上），那是「位置由**业务**自己定」
    的出口 —— 而这条浮层根本没有业务在定它的位置，它是框架中枢弹的；且它占屏最久
    （2 分钟 timeout），落在正中偏上正压着用户在读的那一块。

    两件事一起守：**落位**（右上角、跳开常驻钟）与**它确实进了提示栈**（没进栈 ⇒
    下一次重排就漏掉它，落位又回到调用方手里）。
    """
    ag = _avail()
    docked = Overlay(size=(300, 80))
    docked.show_docked()
    bus = EventBus()
    center = InteractionCenter(bus, ledger=core.DedupeLedger(tmp_path / "s.json"))
    center.register(_trigger("a", (core.Condition(core.COND_ON_START),)))
    center.mark_started()
    overlay = _overlay_of(center)
    assert overlay is not None, "前置：条件成立就该真弹出来"
    try:
        assert live_toasts() == [overlay], "询问没进提示栈（落位会退回调用方自己摆）"
        assert overlay.x() == (
            ag.x() + ag.width() - overlay.width() - metrics.OVERLAY_EDGE_GAP
        )
        assert overlay.y() == docked.y() + docked.height() + metrics.OVERLAY_STACK_GAP
        assert overlay.x() != ag.x() + (ag.width() - overlay.width()) // 2, "退回居中了"
        assert docked.y() == ag.y() + metrics.OVERLAY_EDGE_GAP, "常驻钟被询问顶开了"
    finally:
        overlay.dismiss(DISMISS_CLOSED)
        docked.dismiss(DISMISS_CLOSED)
    assert live_toasts() == [] and live_docked() == []


# ── 锁存合取 ──────────────────────────────────────────────────────────

def test_latch_conjunction_needs_both_signals(qtbot, tmp_path):
    """启动时场景还是 unknown ⇒ 只在启动那一刻求值，这条规则**永远**触发不了。"""
    bus = EventBus()
    center = InteractionCenter(bus, ledger=core.DedupeLedger(tmp_path / "s.json"))
    center.register(
        _trigger(
            "a",
            (
                core.Condition(core.COND_ON_START),
                core.Condition(core.COND_STATE_IS, topic=ROUTE_PROFILE_TOPIC,
                               state="active", detail="Office"),
            ),
        )
    )
    center.mark_started()
    assert center.current_key is None, "场景还没到，不该弹"

    bus.publish(ROUTE_PROFILE_TOPIC, _report("active", "Home"))
    assert center.current_key is None, "场景不对，不该弹"

    bus.publish(ROUTE_PROFILE_TOPIC, _report("active", "Office"))
    assert center.current_key == "a"


def test_latch_is_sticky_not_current_state(qtbot, tmp_path):
    """锁存记的是「发生过」：到家后场景变回 home，白天那次「到过 Office」不该被抹掉。

    不再重弹由**去重账**负责（下面那条用例），两件事各管一段 —— 混在锁存里就会变成
    「来回切换场景反复弹」。
    """
    bus = EventBus()
    center = InteractionCenter(bus, ledger=core.DedupeLedger(tmp_path / "s.json"))
    center.register(
        _trigger(
            "a",
            (
                core.Condition(core.COND_ON_START),
                core.Condition(core.COND_STATE_IS, topic=ROUTE_PROFILE_TOPIC,
                               state="active", detail="Office"),
            ),
        )
    )
    center.mark_started()
    bus.publish(ROUTE_PROFILE_TOPIC, _report("active", "Office"))
    bus.publish(ROUTE_PROFILE_TOPIC, _report("none", ""))
    assert center._triggers["a"].due(), "锁存不该被后面的信号清掉"


def test_panel_opened_latches(qtbot, tmp_path):
    bus = EventBus()
    center = InteractionCenter(bus, ledger=core.DedupeLedger(tmp_path / "s.json"))
    center.register(_trigger("a", (core.Condition(core.COND_PANEL_OPENED),)))
    bus.publish(PANEL_OPENED_TOPIC, None)
    assert center.current_key == "a"


# ── 答案口径 ──────────────────────────────────────────────────────────

def test_timeout_is_not_answered(qtbot, tmp_path):
    """超时 = 未回答（零模态，不阻塞）。"""
    bus = EventBus()
    center = InteractionCenter(bus, ledger=core.DedupeLedger(tmp_path / "s.json"))
    seen: list = []
    center.register(_trigger("a", (core.Condition(core.COND_ON_START),)), seen.append)
    center.mark_started()
    overlay = _overlay_of(center)
    qtbot.addWidget(overlay)
    # 直接走「显示时长到点」那条退场路径（真实等待留给真机；这里守的是口径映射）
    overlay.dismiss("timeout")
    assert seen and seen[0].answered is False and seen[0].kind == core.RESULT_TIMEOUT


def test_answered_value_reaches_callback(qtbot, tmp_path):
    bus = EventBus()
    center = InteractionCenter(bus, ledger=core.DedupeLedger(tmp_path / "s.json"))
    seen: list = []
    center.register(_trigger("a", (core.Condition(core.COND_ON_START),)), seen.append)
    center.mark_started()
    overlay = _overlay_of(center)
    qtbot.addWidget(overlay)
    overlay._answer(core.RESULT_ANSWERED, "09:30")
    assert seen[0].answered is True and seen[0].value == "09:30"
    assert center._ledger.should_ask("a") is False, "答过当天不再弹"


def test_dismissed_does_not_come_back_same_day(qtbot, tmp_path):
    """关掉 = 当天不再来（用户唯一能表达「别烦我」的动作）。"""
    bus = EventBus()
    center = InteractionCenter(bus, ledger=core.DedupeLedger(tmp_path / "s.json"))
    center.register(_trigger("a", (core.Condition(core.COND_ON_START),)))
    center.mark_started()
    _overlay_of(center)._answer(core.RESULT_DISMISSED, None)
    center._on_beat()
    assert center.current_key is None and center.queue == []


# ── 关掉 ≠ 答过：下次启动还得再问一次（用户 2026-10-01 定案）──────────
#
# 点「关闭」＝「现在不想看」，不是「我答过了」。原来那档把两件事当成一件，于是关掉的那天
# 等于整天没有提醒（而翻页钟还按旧值当真）。这一节守两端：**重启后那次机会要还给他**，
# 以及**答过之后不许再问第二遍**；顺带把用户那句「启动后场景第一次命中才弹、反复切换不算」
# 逐条钉住。
# ────────────────────────────────────────────────────────────────────

def _answered_day_trigger():
    """「今天几点上班」那类规则：启动 ＋ 到某场景，去重取「当天答过为止」那档。"""
    return _trigger(
        "k",
        (
            core.Condition(core.COND_ON_START),
            core.Condition(core.COND_STATE_IS, topic=ROUTE_PROFILE_TOPIC, state="active"),
        ),
        dedupe=core.DEDUPE_ANSWERED_DAY,
    )


def _launch(bus, path, scene: bool = True) -> InteractionCenter:
    """一次「启动」：新中枢 ＋ 同一份账目文件（内存那笔账本就该随进程消失）。

    ``scene=True`` ＝ 启动之后场景才第一次命中 —— 这是**常态**（启动时路由还是 unknown），
    也是用户那句「启动后场景第一次命中就要弹」的场景。
    """
    center = InteractionCenter(bus, ledger=core.DedupeLedger(path))
    center.register(_answered_day_trigger())
    center.mark_started()
    if scene:
        center._on_topic(ROUTE_PROFILE_TOPIC, _report())
    return center


def test_launch_alone_waits_for_the_scene(qtbot, tmp_path):
    """启动本身不算数（条件是个合取）：场景还没命中时不许弹。"""
    bus = EventBus()
    center = _launch(bus, tmp_path / "s.json", scene=False)
    assert center.current_key is None, "还没到那条场景就弹 ⇒ 人不在公司也会被问"

    center._on_topic(ROUTE_PROFILE_TOPIC, _report())
    assert center.current_key == "k", "场景命中之后才该弹"
    qtbot.addWidget(_overlay_of(center))


def test_dismissed_rule_asks_again_after_a_restart(qtbot, tmp_path):
    """关掉 ⇒ 这次运行到此为止，重启之后（今天仍没填）再问一次。"""
    path = tmp_path / "s.json"
    bus = EventBus()
    first = _launch(bus, path)
    overlay = _overlay_of(first)
    qtbot.addWidget(overlay)
    overlay._answer(core.RESULT_DISMISSED, None)          # 用户点的正是这个
    first._on_beat()
    assert first.current_key is None

    assert _overlay_of(_launch(bus, path)) is not None, (
        "关了之后重启就不再问 ⇒ 那天等于没有提醒（用户要的是把这次机会还给他）"
    )


def test_timeout_also_comes_back_next_launch(qtbot, tmp_path):
    """超时（没人理、浮层自己没了）与关掉同档：都没答过 ⇒ 下次启动再问。"""
    path = tmp_path / "s.json"
    bus = EventBus()
    first = _launch(bus, path)
    overlay = _overlay_of(first)
    qtbot.addWidget(overlay)
    overlay.dismiss(DISMISS_TIMEOUT)                     # 走真实的退场路径
    assert _overlay_of(_launch(bus, path)) is not None


def test_answered_rule_stays_quiet_after_a_restart(qtbot, tmp_path):
    """答过 ⇒ 当天重启也不再问（一天被问两次是用户会判成「有病」的那类重复）。"""
    path = tmp_path / "s.json"
    bus = EventBus()
    first = _launch(bus, path)
    overlay = _overlay_of(first)
    qtbot.addWidget(overlay)
    overlay._answer(core.RESULT_ANSWERED, "09:12")

    assert _overlay_of(_launch(bus, path)) is None


def test_manual_fill_also_counts_as_answered(qtbot, tmp_path):
    """「拉」的那条路（用户在设置页自己补填，插件叫一声 note_answered）也叫答过。

    两笔账必须记在**同一个 key** 上：否则用户自己填完，重启还要被问一遍。
    """
    path = tmp_path / "s.json"
    bus = EventBus()
    first = _launch(bus, path)
    qtbot.addWidget(_overlay_of(first))
    first.note_answered("k")

    assert _overlay_of(_launch(bus, path)) is None


def test_only_one_prompt_per_launch_however_often_the_scene_flips(qtbot, tmp_path):
    """同一次运行里，场景来回切也只弹一次 —— 用户那句「启动后场景反复切换不算」。"""
    bus = EventBus()
    center = _launch(bus, tmp_path / "s.json")
    overlay = _overlay_of(center)
    qtbot.addWidget(overlay)
    overlay._answer(core.RESULT_DISMISSED, None)

    for _ in range(3):
        center._on_topic(ROUTE_PROFILE_TOPIC, _report("none", ""))
        center._on_topic(ROUTE_PROFILE_TOPIC, _report())
        center._on_beat()
    assert center.current_key is None, "场景每切一次就问一次 ⇒ 那不是用户要的「第一次命中」"


# ── 「稍后」 ──────────────────────────────────────────────────────────

def test_later_clears_today_mark_and_comes_back(qtbot, tmp_path):
    """选「稍后」⇒ 清掉当天标记 ⇒ 到点中枢再弹一次（不清的话「稍后」等于没点）。"""
    bus = EventBus()
    ledger = core.DedupeLedger(tmp_path / "s.json")
    center = InteractionCenter(bus, ledger=ledger)
    center.register(_trigger("a", (core.Condition(core.COND_ON_START),), later=30))
    center.mark_started()
    _overlay_of(center)._answer(core.RESULT_LATER, None)

    assert ledger.should_ask("a") is False, "稍后未到期 ⇒ 现在不该弹"
    # 到期（负的分钟数 = 到期时刻已过）⇒ 复检时重新入队
    ledger.snooze("a", -1)
    center._on_beat()
    assert center.current_key == "a"


def test_snooze_button_hidden_when_over_daily_limit(tmp_path):
    """超了当天上限就不画「稍后」—— 画一个点了没用的按钮是骗人。"""
    overlay = PromptOverlay(
        core.PromptSpec("k", "问一句", later_minutes=30), can_snooze=False
    )
    assert not [t for t in _buttons(overlay) if t.startswith("稍后")]
    allowed = PromptOverlay(
        core.PromptSpec("k", "问一句", later_minutes=30), can_snooze=True
    )
    assert [t for t in _buttons(allowed) if t.startswith("稍后")]


# ── 浮层控件 ──────────────────────────────────────────────────────────

def test_time_prompt_prefills_and_answers(qtbot):
    """时间问法：**预填当前时刻**（最常见的答案就是「现在」），按确定即作答。"""
    overlay = PromptOverlay(core.PromptSpec("k", "今天几点上班？", kind=core.PROMPT_TIME))
    qtbot.addWidget(overlay)
    seen: list = []
    overlay.finished.connect(lambda kind, value: seen.append((kind, value)))
    from PySide6.QtCore import QTime

    overlay._editor.setTime(QTime(9, 12))
    [b for b in overlay.findChildren(QPushButton) if b.text() == "确定"][0].click()
    assert seen == [(core.RESULT_ANSWERED, "09:12")]


def test_choice_prompt_option_is_the_answer(qtbot):
    overlay = PromptOverlay(
        core.PromptSpec("k", "现在方便吗？", kind=core.PROMPT_CHOICE,
                        options=("方便", "不方便"))
    )
    qtbot.addWidget(overlay)
    seen: list = []
    overlay.finished.connect(lambda kind, value: seen.append((kind, value)))
    [b for b in overlay.findChildren(QPushButton) if b.text() == "不方便"][0].click()
    assert seen == [(core.RESULT_ANSWERED, "不方便")]


def test_overlay_auto_height_is_not_collapsed(qtbot):
    """只钉宽、不钉高 ⇒ 高度由内容给。**留着防「布局没算出来 ⇒ 一个空壳窗」。**"""
    overlay = PromptOverlay(
        core.PromptSpec("k", "今天几点上班？", kind=core.PROMPT_CHOICE,
                        options=("方便", "不方便"))
    )
    qtbot.addWidget(overlay)
    overlay.show()
    assert overlay.width() > 0 and overlay.height() > 80


# ── 登记 / 撤销 / 收尾 ────────────────────────────────────────────────

def test_unregister_closes_live_overlay(qtbot, tmp_path):
    bus = EventBus()
    center = InteractionCenter(bus, ledger=core.DedupeLedger(tmp_path / "s.json"))
    center.register(_trigger("a", (core.Condition(core.COND_ON_START),)))
    center.mark_started()
    overlay = _overlay_of(center)
    qtbot.addWidget(overlay)
    center.unregister("clock")
    assert center.current_key is None
    assert center._topic_handlers == {}


def test_heartbeat_ticker_is_removed_on_dispose(tmp_path):
    bus = EventBus()
    beat = Heartbeat()
    center = InteractionCenter(bus, heartbeat=beat)
    assert beat.ticker_count() == 1
    center.dispose()
    assert beat.ticker_count() == 0


def test_register_before_started_does_not_fire(qtbot, tmp_path):
    """中枢没叫 mark_started 之前，on_start 那一条件不成立 —— 装配期不该弹窗。"""
    bus = EventBus()
    center = InteractionCenter(bus, ledger=core.DedupeLedger(tmp_path / "s.json"))
    center.register(_trigger("a", (core.Condition(core.COND_ON_START),)))
    center._on_beat()
    assert center.current_key is None


# ── 「用户自己按的那一次」：ask_now ───────────────────────────────────
#
# 翻页时钟设置里那枚「现在问我」要能**立刻**弹出询问浮层：条件（启动 / 到某场景）与
# 「每天一次」那笔账都是为「系统来打扰你」设计的，用户自己按的那一侧两道闸都得绕开。
# 绕开归绕开，**真 key 那笔账一个字都不能动** —— 否则「点一次测试 ⇒ 今天真正的那次
# 不弹了」，那是用户只会判成「这功能有病」的那类 bug。
# ────────────────────────────────────────────────────────────────────

def test_ask_now_pops_without_any_condition(qtbot, tmp_path):
    """条件一个都没成立（还没 mark_started、也没到场景）⇒ 照旧弹。"""
    bus = EventBus()
    center = InteractionCenter(bus, ledger=core.DedupeLedger(tmp_path / "s.json"))
    got = []
    center.ask_now(
        _trigger("k", (core.Condition(core.COND_ON_START),)),
        lambda result: got.append(result),
    )

    assert center.current_key == f"k{MANUAL_KEY_SUFFIX}", "没弹 ⇒ 用户在设置里点了没反应"
    overlay = _overlay_of(center)
    assert overlay is not None
    qtbot.addWidget(overlay)

    # 答案照旧走登记时那个回调（中枢不认业务 ⇒ 「算不算数」由插件那一侧定）
    overlay._answer(core.RESULT_ANSWERED, "09:12")
    assert [r.value for r in got] == ["09:12"], "答案没回到插件 ⇒ 填了等于没填"


def test_ask_now_does_not_spend_the_real_rules_allowance(qtbot, tmp_path):
    """点一次「现在问我」，今天**真正的**那次额度必须还在（临时 key 与真 key 两笔账）。"""
    bus = EventBus()
    center = InteractionCenter(bus, ledger=core.DedupeLedger(tmp_path / "s.json"))
    center.register(_trigger("k", (core.Condition(core.COND_ON_START),)))

    center.ask_now(_trigger("k", ()), lambda result: None)
    assert center.current_key == f"k{MANUAL_KEY_SUFFIX}"
    overlay = _overlay_of(center)
    qtbot.addWidget(overlay)
    overlay._answer(core.RESULT_ANSWERED, "09:12")   # 答掉临时那次

    center.mark_started()                            # 真规则的条件此刻才成立
    assert center.current_key == "k", (
        "真规则那笔账被「现在问我」用掉了 ⇒ 今天它再也不会问（用户只看到「它没问我」）"
    )


def test_ask_now_trigger_is_torn_down_after_the_answer(qtbot, tmp_path):
    """答案收完就拆：留着就是一条**永远成立**的规则（每次心跳都求值、还会再弹一遍）。"""
    bus = EventBus()
    ledger = core.DedupeLedger(tmp_path / "s.json")
    center = InteractionCenter(bus, ledger=ledger)
    center.ask_now(_trigger("k", ()), lambda result: None)
    overlay = _overlay_of(center)
    qtbot.addWidget(overlay)
    overlay._answer(core.RESULT_ANSWERED, "09:12")

    assert center._manual == set(), "临时 key 没拆 ⇒ 心跳扫到它又弹一次"
    assert f"k{MANUAL_KEY_SUFFIX}" not in center._triggers
    assert f"k{MANUAL_KEY_SUFFIX}" not in center._callbacks
    assert ledger._entry(f"k{MANUAL_KEY_SUFFIX}") == {}, "账本里留了一串没人读的临时账"


# ── 填时间那一格：一行 ＋ 预览行 ──────────────────────────────────────
#
# 版面是用户提的（「时间和确定一排显示」＋「下方预览填写后的提示文本」），守的是两件
# 会静默退化的事：① 拆成上下两行（把「填 × 按确定」的一次动作走成两步）；② 预填值
# 是**登记那一刻**算好的字符串 —— 规则在启动时登记、几小时后才弹，弹出来的就是旧时间。
# ────────────────────────────────────────────────────────────────────

def test_time_field_and_ok_button_share_one_row(qtbot, tmp_path):
    """时间格与「确定」必须**同一行**（不是上下两行）。"""
    bus = EventBus()
    center = InteractionCenter(bus, ledger=core.DedupeLedger(tmp_path / "s.json"))
    center.ask_now(_trigger("k", (), kind=core.PROMPT_TIME), lambda result: None)
    overlay = _overlay_of(center)
    assert overlay is not None
    qtbot.addWidget(overlay)

    editor = overlay.findChild(QTimeEdit)
    ok = next((b for b in overlay.findChildren(QPushButton) if b.text() == "确定"), None)
    assert editor is not None and ok is not None, "前置：时间格与确定都在"
    assert abs(editor.geometry().center().y() - ok.geometry().center().y()) <= 4, (
        "确定没与时间格同排 ⇒ 一次动作被拆成两步"
    )
    assert ok.geometry().x() > editor.geometry().x(), "确定该在时间格右边（先填后按）"


def test_prompt_prefills_from_the_initial_callback_at_pop_time(qtbot, tmp_path):
    """预填值必须**弹的那一刻**才问：登记到弹出之间隔了几小时，字符串会变成旧时间。"""
    bus = EventBus()
    center = InteractionCenter(bus, ledger=core.DedupeLedger(tmp_path / "s.json"))
    box = ["08:00"]
    center.register(
        _trigger("k", (core.Condition(core.COND_ON_START),), initial=lambda: box[0])
    )

    box[0] = "11:30"          # 登记之后、弹出之前，「现在」已经变了
    center.mark_started()
    overlay = _overlay_of(center)
    qtbot.addWidget(overlay)

    assert overlay._editor.time().toString("HH:mm") == "11:30", (
        "预填的是登记那一刻的时间 ⇒ 用户每次都要先改一遍"
    )


def test_prompt_preview_row_follows_the_value(qtbot, tmp_path):
    """下方那行预览：跟着此刻填的值走；插件不给 ⇒ 这一行留白（不猜）。"""
    bus = EventBus()
    center = InteractionCenter(bus, ledger=core.DedupeLedger(tmp_path / "s.json"))
    seen = []
    center.ask_now(
        _trigger("k", (), kind=core.PROMPT_TIME,
                 initial=lambda: "09:00",
                 preview=lambda value: seen.append(value) or f"下班 {value}"),
        lambda result: None,
    )
    overlay = _overlay_of(center)
    qtbot.addWidget(overlay)

    assert overlay._preview_row.text() == "下班 09:00", "刚弹出就该按预填值算一遍"
    overlay._editor.setTime(QTime(10, 15))
    assert overlay._preview_row.text() == "下班 10:15", "改了时间预览没跟着变 ⇒ 这一行是摆设"
    assert seen == ["09:00", "10:15"], "预览不是每次改值都问 ⇒ 会停在第一次的结果上"


def test_prompt_without_a_preview_callback_shows_no_preview_text(qtbot, tmp_path):
    """插件没给预览 ⇒ 那一行空着（框架不替业务编一句）。"""
    bus = EventBus()
    center = InteractionCenter(bus, ledger=core.DedupeLedger(tmp_path / "s.json"))
    center.ask_now(_trigger("k", (), kind=core.PROMPT_TIME), lambda result: None)
    overlay = _overlay_of(center)
    qtbot.addWidget(overlay)
    assert overlay._preview_row.text() == ""


def test_prompt_rows_share_one_card_alignment(qtbot, tmp_path):
    """三行内容左右边缘对齐成一张卡：输入格**撑满**、底行动作**靠右**与「确定」同列。

    守的是两处会静默退回的排法：输入格缩在左、右边留一大块空白（像没排完）；
    底行居中 —— 与上面各行的左对齐打架，两套对齐叠一张卡就是「丑」的来源。
    """
    bus = EventBus()
    center = InteractionCenter(bus, ledger=core.DedupeLedger(tmp_path / "s.json"))
    center.ask_now(_trigger("k", (), kind=core.PROMPT_TIME), lambda result: None)
    overlay = _overlay_of(center)
    assert overlay is not None
    qtbot.addWidget(overlay)

    editor = overlay.findChild(QTimeEdit)
    ok = next((b for b in overlay.findChildren(QPushButton) if b.text() == "确定"), None)
    close = next((b for b in overlay.findChildren(QPushButton) if b.text() == "关闭"), None)
    assert editor is not None and ok is not None and close is not None, "前置：三样都在"
    overlay.show()
    for _ in range(5):
        QGuiApplication.processEvents()

    # 输入格撑满：它的右缘要贴到「确定」（中间没有大块空白）
    assert editor.geometry().right() < ok.geometry().x() <= editor.geometry().right() + 30, (
        f"输入格与确定之间隔着空白（editor.right={editor.geometry().right()}, "
        f"ok.x={ok.geometry().x()}）⇒ 这一行没排满"
    )
    # 底行靠右：「关闭」的右缘与「确定」的右缘同列（±4px）
    assert abs(close.geometry().right() - ok.geometry().right()) <= 4, (
        f"底行没靠右（close.right={close.geometry().right()}, "
        f"ok.right={ok.geometry().right()}）⇒ 两套对齐打架"
    )


def test_reregister_replaces_the_subscription_too(tmp_path):
    """重登记 = 整条替换，**订阅面**也要换：被换掉的条件再来信号不该弹这一条。

    回归（2026-10-06，潜伏项）：``register`` 只换 ``_triggers[key]``，订阅是**纯增量**
    ⇒ 「重登记 = 整条替换」这句承诺对订阅面不成立。旧 topic 的 ``_keys_by_topic`` 里
    仍留着这个 key（它还活着，``_prune_topics`` 按「key 还在不在」清，清不到它），
    于是**已经被换掉的那个条件**再来信号时，中枢会拿**新声明**去求值并弹一次 ——
    用户看到的是「我把触发条件改了，它还是按老条件弹」。

    今天所有调用点都是「先 ``unregister`` 再整批 ``register``」，把这条掩盖住了；
    任何将来直接调 ``register`` 做重登记的调用点都会踩到。
    """
    bus = EventBus()
    center = InteractionCenter(bus, ledger=core.DedupeLedger(tmp_path / "s.json"))
    # 先按「开面板」触发登记，再把同一条规则重登记成「无条件」（空条件恒成立）。
    center.register(
        _trigger("k", (core.Condition(core.COND_PANEL_OPENED),), dedupe=core.DEDUPE_ALWAYS)
    )
    assert "k" in center._keys_by_topic.get(PANEL_OPENED_TOPIC, set()), "前置：已订阅开面板"
    center.register(_trigger("k", (), dedupe=core.DEDUPE_ALWAYS))

    assert "k" not in center._keys_by_topic.get(PANEL_OPENED_TOPIC, set()), (
        "重登记后仍挂在旧 topic 上 ⇒ 换掉的条件还会再弹一次"
    )
    bus.publish(PANEL_OPENED_TOPIC, None)
    assert center.current_key is None and center.queue == [], (
        f"被换掉的条件仍在弹（current={center.current_key}, queue={center.queue}）"
    )


# ── 「提醒弹出来了」这个时刻（``on_shown``） ──────────────────────────
#
# 与 ``on_result`` 是两个时刻，不是一件事的两种写法：那个是**答案**，这个是**登场**。
# 今天唯一的使用方是待办那条「到点执行一条命令」—— 它要的正是「提醒把我叫醒」而不是
# 「我答了什么」（答「关闭」是最常见的一档，而它在 ``on_result`` 里与「app 退出时被
# 强制收走」同形）。四条判据各守一件事：**该响时响**、**不该响时不响**、**响了不重复**、
# **它炸了不许把中枢带走**。

def test_the_shown_callback_fires_at_the_moment_the_overlay_pops(qtbot, tmp_path):
    seen: "list[str]" = []
    bus = EventBus()
    center = InteractionCenter(bus, ledger=core.DedupeLedger(tmp_path / "s.json"))
    center.register(
        _trigger("a", (core.Condition(core.COND_ON_START),)),
        on_result=lambda _r: seen.append("result"),
        on_shown=lambda: seen.append("shown"),
    )
    assert seen == [], "登记本身不是「弹出来了」——那会儿还没到点"

    center.mark_started()
    assert seen == ["shown"]
    qtbot.addWidget(_overlay_of(center))

    # ★ 答了 / 超时 / 关掉都只有 ``on_result`` 那一笔 —— 「登场」只发一次。
    _overlay_of(center)._answer(core.RESULT_ANSWERED, "09:12")
    assert seen == ["shown", "result"], "「登场」不该跟着每种结局重发一遍"


def test_a_rule_the_ledger_blocks_is_never_shown(qtbot, tmp_path):
    """去重账挡掉的那一次**没有登场** ⇒ 不许发 ``on_shown``。

    这一条是「该响时响」的反面：没有它，把 ``_notify_shown`` 挪到 ``should_ask`` 判定
    之前也照样绿 —— 而那样一来「今天已经问过了」的每一拍心跳都会重跑一次旁路动作。
    """
    seen: "list[str]" = []
    ledger = core.DedupeLedger(tmp_path / "s.json")
    ledger.mark_asked("a")
    center = InteractionCenter(EventBus(), ledger=ledger)
    center.register(
        _trigger("a", (core.Condition(core.COND_ON_START),)),
        on_shown=lambda: seen.append("shown"),
    )
    center.mark_started()
    assert center.current_key is None and seen == []


def test_a_manual_ask_does_not_count_as_the_reminder_popping(qtbot, tmp_path):
    """「现在问我」是**用户自己按**的：没有「提醒把我叫醒」这回事。

    临时 key 上不挂 ``on_shown`` ⇒ 真 key 那一枚不会被顺带叫起来。少了这道隔离，
    「点一次『现在问我』当测试」就会真的跑一条命令 —— 而用户以为自己只是在试。
    """
    seen: "list[str]" = []
    center = InteractionCenter(EventBus(), ledger=core.DedupeLedger(tmp_path / "s.json"))
    spec = _trigger("a", (core.Condition(core.COND_ON_START),))
    center.register(spec, on_shown=lambda: seen.append("shown"))
    center.ask_now(spec)

    assert center.current_key == f"a{MANUAL_KEY_SUFFIX}"
    assert seen == []


def test_a_broken_shown_callback_does_not_take_the_center_down(qtbot, tmp_path):
    """旁路回调炸了：这条提醒照弹、后面排队的照补位。

    调用点在 ``_pump`` 里 —— 让它抛出去，赔的是**整个中枢**（``_pump`` 之后那几行不执行，
    而它已经在去重账上记了「今天问过」）。
    """
    def _boom() -> None:
        raise RuntimeError("旁路动作炸了")

    center = InteractionCenter(EventBus(), ledger=core.DedupeLedger(tmp_path / "s.json"))
    center.register(_trigger("a", (core.Condition(core.COND_ON_START),)), on_shown=_boom)
    center.register(_trigger("b", (core.Condition(core.COND_ON_START),)))
    center.mark_started()

    assert center.current_key == "a", "旁路回调炸了，但它自己该照弹"
    qtbot.addWidget(_overlay_of(center))
    _overlay_of(center)._answer(core.RESULT_ANSWERED, "09:12")
    assert center.current_key == "b", "b 该照常补位"


def test_the_shown_callback_is_dropped_with_its_rule(tmp_path):
    """与 ``_callbacks`` 同生共死（否则撤下来的规则仍会在下次登台时跑旁路动作）。"""
    center = InteractionCenter(EventBus(), ledger=core.DedupeLedger(tmp_path / "s.json"))
    center.register(
        _trigger("a", (core.Condition(core.COND_ON_START),)),
        on_shown=lambda: None,
    )
    assert center._on_shown, "前置：登记时确实收下了"
    center.unregister("clock")
    assert center._on_shown == {}
