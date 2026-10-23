"""tests/test_hotkey_feedback：热键**触发之后**的反馈分派（逐条三选一 ＋ 失败一律通知）。

为什么这一条链非有不可：热键按下去时设置窗多半是关着的，**这条链产出的东西是用户唯一
看得见的结果**。它在 2026-09-23 之前**零用例覆盖**（`report` 三个分支 ＋ 默认轻浮层都没人
跑过），于是「轻浮层」那一档里写着一个不存在的排版角色名，表现是「按了键、日志里一条
traceback、屏幕上什么都没弹」—— 而且它**从来没弹出过**，是用户当场报上来的。

三条口径各有用例：三个档**字面即行为**（不通知就真的不显示）、失败**不受**档位影响、
以及「轻浮层」这一档真的弹出一个看得见的窗（不是「没抛错就算过」）。
"""

from __future__ import annotations

from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QLabel

from barkit.contribution.specs import ActionOutcome, ActionResult, as_action_result
from barkit.shell.design import metrics
from barkit.shell.hotkey_catalog import (
    FEEDBACK_NONE,
    FEEDBACK_NOTIFY,
    FEEDBACK_OVERLAY,
)
from barkit.shell.hotkey_feedback import HotkeyFeedback, outcome_of, wrap_hotkey_callback
from barkit.shell.overlay import DISMISS_CANCELLED, Overlay, live_toasts


def _avail():
    """光标所在屏的可用区（提示落位的基准）。"""
    screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
    return screen.availableGeometry()


def _feedback() -> tuple[HotkeyFeedback, list[tuple]]:
    """一个反馈器 ＋ 系统通知的收集器（真发通知要 bundle 身份，离屏也没有通知中心）。"""
    sent: list[tuple] = []
    fb = HotkeyFeedback(notify=lambda name, title, body: sent.append((name, title, body)))
    return fb, sent


def _drain_flashes() -> None:
    """把还在屏上的轻浮层收干净（用例失败半途也不会留窗给后面的用例）。"""
    for overlay in live_toasts():
        overlay.dismiss(DISMISS_CANCELLED)


def _live_overlays() -> list[Overlay]:
    return [w for w in live_toasts() if isinstance(w, Overlay)]


def test_overlay_mode_pops_a_visible_flash_and_cleans_up_after_itself(qtbot):
    """「轻浮层」这一档要**真的弹出来**（可见的窗 ＋ 文案），退场后不留引用。

    「没抛异常」不算数：2026-09-23 那个 bug 的表现正是「调用链上抛了 KeyError，
    而用户那一侧看起来只是『什么都没弹』」—— 所以判据落在窗上：可见、字对。
    """
    fb, sent = _feedback()
    try:
        fb.report(name="framework.panel", title="打开面板", ok=True, mode=FEEDBACK_OVERLAY)
        qtbot.wait(1)                      # 让 show 落定（离屏也要转一次事件循环）

        flashes = _live_overlays()
        assert len(flashes) == 1, f"该弹出一个轻浮层，实际 {len(flashes)}"
        overlay = flashes[0]
        assert overlay.isVisible(), "轻浮层建出来了却没显示"
        assert [lbl.text() for lbl in overlay.findChildren(QLabel)] == ["打开面板"]
        assert sent == [], "轻浮层这一档不该同时发系统通知（标签即行为）"

        # 明细那一半：带上 detail 时是「标题：明细」一句
        fb.report(name="recognize", title="截图识别", ok=True, detail="已识别 3 处文字",
                  mode=FEEDBACK_OVERLAY)
        qtbot.wait(1)
        texts = [lbl.text() for ov in _live_overlays() for lbl in ov.findChildren(QLabel)]
        assert "截图识别：已识别 3 处文字" in texts, f"带明细时文案不对：{texts}"
    finally:
        _drain_flashes()
    assert live_toasts() == [], "浮层退场后引用要同步摘掉（发后不管的出口不能留残引用）"


def test_none_mode_stays_silent(qtbot):
    """「不通知」= 成功时什么都不显示（既不弹浮层，也不发通知）。"""
    fb, sent = _feedback()
    try:
        fb.report(name="framework.panel", title="打开面板", ok=True, mode=FEEDBACK_NONE)
        qtbot.wait(1)
        assert sent == []
        assert _live_overlays() == [], "选了「不通知」却弹了轻浮层"
    finally:
        _drain_flashes()


def test_notify_mode_sends_a_system_notice(qtbot):
    """「系统通知」这一档发一条通知，带上那一条的名字（通知中枢按名字去重）。"""
    fb, sent = _feedback()
    fb.report(name="custom.1", title="备份", ok=True, detail="已完成",
              mode=FEEDBACK_NOTIFY)
    assert len(sent) == 1
    name, title, body = sent[0]
    assert name == "custom.1" and title == "备份 已执行" and body == "已完成"


def test_failure_notifies_no_matter_which_mode_was_chosen(qtbot):
    """失败**不受**档位影响：一律发系统通知（失败必须被看见）。"""
    fb, sent = _feedback()
    try:
        for mode in (FEEDBACK_NONE, FEEDBACK_OVERLAY, FEEDBACK_NOTIFY):
            sent.clear()
            fb.report(name="x", title="备份", ok=False, detail="退出码 1", mode=mode)
            qtbot.wait(1)
            assert len(sent) == 1, f"mode={mode!r} 时失败没发通知"
            assert sent[0][1] == "备份 没跑成"
        assert _live_overlays() == [], "失败路径不该弹轻浮层"
    finally:
        _drain_flashes()


def test_last_of_remembers_the_latest_outcome():
    """「上次触发」只存内存、按名字分开记（详情屏事后靠它回看）。"""
    fb, _sent = _feedback()
    assert fb.last_of("custom.1") is None, "没触发过就该说没有，不能编一条空的"
    fb.report(name="custom.1", title="备份", ok=True, detail="ok", mode=FEEDBACK_NONE)
    fb.report(name="custom.2", title="同步", ok=False, detail="退出码 1", mode=FEEDBACK_NONE)
    fb.report(name="custom.1", title="备份", ok=False, detail="退出码 2", mode=FEEDBACK_NONE)
    assert fb.last_of("custom.1") == (False, "退出码 2"), "同名该记**最近一次**"
    assert fb.last_of("custom.2") == (False, "退出码 1")


def test_the_flash_lands_where_the_framework_puts_toasts(qtbot):
    """轻浮层的落位是**框架给的**（右上角），本模块不许自己定位置。

    这一条守的是「提示统一由框架控制」那半句：本模块只管那一句话，出现在哪儿由提示栈
    说了算 —— 哪天有人在这里手写 ``move()``，位置就又变回各弹各的。
    """
    fb, _sent = _feedback()
    ag = _avail()
    try:
        fb.report(name="framework.panel", title="打开面板", ok=True, mode=FEEDBACK_OVERLAY)
        qtbot.wait(1)
        flashes = _live_overlays()
        assert len(flashes) == 1, f"该弹出一条，实际 {len(flashes)}"
        ov = flashes[0]
        assert ov.x() == ag.x() + ag.width() - ov.width() - metrics.OVERLAY_EDGE_GAP
        assert ov.y() == ag.y() + metrics.OVERLAY_EDGE_GAP
    finally:
        _drain_flashes()


# ── 触发链的另一半：回调的返回值怎么读（2026-09-28 随 `ActionResult` 扩宽同批）──
#
# 为什么这一段必须**在本机可测**：宿主那条链（`shell/app_shell`）牵起 PyObjC，Windows 上
# import 不到 ⇒ 判定留在那儿就只能靠真机验。判定与包装收进 `hotkey_feedback` 之后，
# 「返回什么算什么」这件事在离屏就能逐格钉住。


def test_outcome_of_silent_success_for_a_callback_that_says_nothing():
    """``None`` ＝ 老写法（回调只管做事）⇒ 与扩宽之前**逐字相同**的表现。

    这是「纯扩宽、零破坏」那句话的判据：现存回调一个字都不改，报出来的东西也不变。
    """
    assert outcome_of(None) == (True, "")


def test_outcome_of_treats_partial_as_must_be_seen():
    """``FAILED`` 与 ``PARTIAL`` 都算「必须被看见」（``report`` 对失败一律发通知）。

    ``PARTIAL`` 归到这一档是刻意的：用户同样看不出「哪一半没走通」。
    """
    assert outcome_of(ActionResult(ActionOutcome.FAILED, "退出码 1")) == (False, "退出码 1")
    assert outcome_of(ActionResult(ActionOutcome.PARTIAL, "3 条里 2 条没走通")) == (
        False, "3 条里 2 条没走通",
    )
    assert outcome_of(ActionResult(ActionOutcome.OK, "已执行")) == (True, "已执行")
    assert outcome_of(ActionResult(ActionOutcome.OK)) == (True, ""), "空文案＝没明细"


def test_outcome_of_survives_a_return_that_breaks_the_contract(caplog):
    """回调漏了一个 ``return`` 漏出别的东西 ⇒ 当「没话说」，**不是**崩。

    实测两种真实来源：``Signal.emit`` 返回 ``True``；``(a(), b())`` 返回元组。
    宿主那边要是照直读 ``result.outcome``，热键一按就是这个长相：屏幕上什么都没多，
    日志里一条 ``AttributeError``。
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="barkit.contribution.specs"):
        assert outcome_of(True) == (True, "")          # type: ignore[arg-type]
        assert outcome_of((None, None)) == (True, "")  # type: ignore[arg-type]
    assert any("bool" in r.message or "tuple" in r.message for r in caplog.records), (
        "规整掉可以，但要留一条点名类型的痕迹 —— 否则「插件笔误」会永远查不出来"
    )


def test_as_action_result_passes_real_results_through():
    """规整函数对**合契约**的两种取值必须原样放行（别把好数据也吃掉）。"""
    r = ActionResult(ActionOutcome.FAILED, "x")
    assert as_action_result(r) is r
    assert as_action_result(None) is None


def test_wrap_hotkey_callback_reports_what_the_callback_returned():
    """包装后的回调：跑回调 → 把 ``ActionResult`` 变成那个档位的反馈。"""
    fb, sent = _feedback()
    cb = wrap_hotkey_callback(
        lambda: ActionResult(ActionOutcome.FAILED, "没找到网络服务"),
        name="custom.1", feedback=fb,
        title_of=lambda: "切换网络", mode_of=lambda: FEEDBACK_NOTIFY,
    )

    cb()

    assert sent == [("custom.1", "切换网络 没跑成", "没找到网络服务")]


def test_wrap_hotkey_callback_turns_a_crash_into_a_visible_failure():
    """回调抛异常**不再静默消失**：按失败上报（否则「按了没反应、又没提示」最难自查）。"""
    fb, sent = _feedback()

    def _boom():
        raise RuntimeError("炸了")

    cb = wrap_hotkey_callback(
        _boom, name="custom.1", feedback=fb,
        title_of=lambda: "备份", mode_of=lambda: FEEDBACK_NONE,
    )

    cb()

    assert len(sent) == 1, "挂了必须发通知（档位选了「不通知」也一样）"
    assert sent[0][1] == "备份 没跑成" and sent[0][2] == "触发时出错"


def test_wrap_hotkey_callback_reads_title_and_mode_at_trigger_time():
    """标题与档位是**读取口**，在触发那一刻才读 —— 不在登记时定死。

    为什么：标题随设置里改键位而变化，档位是用户逐条选的。登记时定死的话，用户改完
    设置必须重启才生效 —— 而这条链的用例正好是「用户按了键，看到的却是旧的那句话」。
    """
    fb, sent = _feedback()
    state = {"title": "旧标题", "mode": FEEDBACK_NONE}
    cb = wrap_hotkey_callback(
        lambda: ActionResult(ActionOutcome.FAILED, "没跑成"),
        name="custom.1", feedback=fb,
        title_of=lambda: state["title"], mode_of=lambda: state["mode"],
    )

    state["title"], state["mode"] = "新标题", FEEDBACK_NOTIFY
    cb()

    assert sent and sent[0][1] == "新标题 没跑成", "读了登记时的旧值 ⇒ 改设置不生效"
