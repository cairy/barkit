"""屏变化窗口期（``shell/screen_settle``）：判据本身 ＋ 面板首次上屏的让路。

这些用例守的是一条**没有替代品**的约束：屏参数正在变的那一段里首次上屏的窗会被窗口
服务器钉在 ``(0,0)``，且进程内救不回来（2026-10-14 真机把十种手段逐条试过，全无效且
永久）。既然修不了，就只能不撞上去 —— 判据一坏，那个故障原样回来。
"""
from __future__ import annotations

import pathlib
import time

import pytest

from barkit.shell import screen_settle

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "barkit"


@pytest.fixture(autouse=True)
def _clean_gate():
    screen_settle.reset()
    yield
    screen_settle.reset()


# ---------- 判据本身 ----------


def test_the_gate_is_open_when_no_screen_signal_ever_arrived():
    """判据不适用（从没收到过屏信号）⇒ 放行。

    违规必抓：把 ``is_settling`` 写成「没收到过信号 ⇒ 拦」，本例第一句即红 —— 那会让
    非 GUI 环境、以及"监听还没挂上"的启动早期永远开不出面板。
    """
    assert screen_settle.is_settling() is False


def test_one_screen_signal_closes_the_gate():
    screen_settle.note_change("screenAdded")
    assert screen_settle.is_settling() is True


def test_the_gate_opens_only_after_the_signal_stream_goes_quiet():
    """门开的条件是「**静默**了 SETTLE_MS」，不是「收到第一个信号之后过了 SETTLE_MS」。

    违规必抓：把 ``is_settling`` 里的「最后一次信号」改成「第一次信号」，本用例第二句
    会红 —— 而真实的一次屏变化正是**一串**信号（``screenAdded`` → 逐块屏的
    ``geometryChanged`` / ``availableGeometryChanged`` …），只认第一个等于变化还没走完
    就把门开了。
    """
    screen_settle.note_change("screenAdded")
    screen_settle._last_signal_at = (
        time.monotonic() - screen_settle.SETTLE_MS / 1000.0 - 0.5
    )
    assert screen_settle.is_settling() is False, "静默够了却还拦着"

    screen_settle.note_change("geometryChanged")  # 串子还在来 ⇒ 必须重新计时
    assert screen_settle.is_settling() is True, "新信号没有重置计时，门提前开了"


def test_install_is_idempotent(qtbot):
    """重复 ``install()`` 不该把监听挂第二遍（挂了就会重复计时、也重复记日志）。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    screen_settle.install(app)
    attached = set(screen_settle._attached)

    screen_settle.install(app)

    assert screen_settle._installed is True
    assert screen_settle._attached == attached, "重复 install 又挂了一遍屏监听"


def test_each_screen_is_watched_once(qtbot):
    from PySide6.QtWidgets import QApplication

    scr = QApplication.instance().primaryScreen()
    screen_settle._attach(scr)
    count = len(screen_settle._attached)

    screen_settle._attach(scr)

    assert len(screen_settle._attached) == count, "同一块屏被挂了两遍几何监听"


def test_the_screen_change_verdict_has_exactly_one_door():
    """「屏正在变」的判据只许 ``screen_settle`` 那一处监听实现。

    违规必抓：在别处再写一份 ``screenAdded.connect``（两份实现里坏的那份不报错 ——
    取窗那条已经吃过一次这种亏），本用例会红。
    """
    hits = sorted(
        f"{p.relative_to(_SRC)}:{i}"
        for p in _SRC.rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if "screenAdded.connect(" in line
    )
    assert len(hits) == 1 and hits[0].startswith("shell/screen_settle.py"), (
        f"屏变化监听长出了第二份实现（只许 screen_settle.py 里那一处）：{hits}"
    )


# ---------- 面板首次上屏让路 ----------


def test_panel_defers_its_first_show_while_the_screen_settles(qtbot, monkeypatch):
    """屏正在变、而面板**还没上过屏** ⇒ 先别显示。

    违规必抓：把 ``_request_show`` 里那句 ``screen_settle.is_settling()`` 去掉，本例
    「not panel.isVisible()」立刻变红。
    """
    from barkit.shell.panel import StatusPanel

    monkeypatch.setattr(screen_settle, "is_settling", lambda: True)
    panel = StatusPanel()
    qtbot.addWidget(panel)
    try:
        panel.toggle_at(None)
        qtbot.wait(0)

        assert not panel.isVisible(), "屏正在变，面板的首次上屏却没有让路"
        assert getattr(panel, "_show_pending", False) is True, "没有把这次显示排进队"
    finally:
        panel.hide()


def test_panel_shows_as_soon_as_the_screen_is_quiet(qtbot, monkeypatch):
    """门一开就该显示 —— 等待不能变成丢失。"""
    from barkit.shell.panel import StatusPanel

    monkeypatch.setattr(screen_settle, "is_settling", lambda: True)
    panel = StatusPanel()
    qtbot.addWidget(panel)
    try:
        panel.toggle_at(None)
        qtbot.wait(0)
        assert not panel.isVisible()

        monkeypatch.setattr(screen_settle, "is_settling", lambda: False)
        panel._retry_show_after_settle()
        qtbot.wait(0)

        assert panel.isVisible(), "门已经开了，面板却还在等"
        assert getattr(panel, "_show_pending", False) is False, "排队标志没清掉"
    finally:
        panel.hide()


def test_panel_never_waits_once_it_has_been_on_screen(qtbot, monkeypatch):
    """已经上过屏的窗不受屏变化影响（服务器早给它记好位置了）⇒ **不拦**。"""
    from barkit.shell.panel import StatusPanel

    monkeypatch.setattr(screen_settle, "is_settling", lambda: False)
    panel = StatusPanel()
    qtbot.addWidget(panel)
    try:
        panel.toggle_at(None)
        qtbot.wait(0)
        assert panel.isVisible()
        panel.hide()
        qtbot.wait(0)

        monkeypatch.setattr(screen_settle, "is_settling", lambda: True)
        panel.toggle_at(None)
        qtbot.wait(0)

        assert panel.isVisible(), "已经上过屏的窗不该再为屏变化让路"
    finally:
        panel.hide()


def test_repeated_clicks_while_waiting_queue_only_once(qtbot, monkeypatch):
    """等门期间连点几下：只排一次队，不会排出一串重复显示。"""
    from barkit.shell.panel import StatusPanel

    monkeypatch.setattr(screen_settle, "is_settling", lambda: True)
    panel = StatusPanel()
    qtbot.addWidget(panel)
    try:
        panel.toggle_at(None)
        panel.toggle_at(None)
        panel.toggle_at(None)
        qtbot.wait(0)

        assert not panel.isVisible()
        assert getattr(panel, "_show_pending", False) is True
    finally:
        panel.hide()
