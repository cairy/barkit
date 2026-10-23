"""tests/test_thread_reap：``QThread`` 的安全收尾（``core/thread_reap``）。

**这些用例守的是一条真机崩溃**（2026-09-29，报告
``~/Library/Logs/DiagnosticReports/Python-2026-09-29-155751.ips``：``SIGABRT`` /
``abort() called``，触发线程 ``_DiagWorker``）：隧道诊断那页关窗时用 ``quit()`` +
``wait(1000)`` 收线程，一秒后就把引用丢了 —— 线程还在跑，``~QThread()`` 随即执行，
Qt 直接 ``qFatal`` ⇒ **整个进程死**，不是异常、不是某次回调失败。

崩溃本身在 pytest 里**抓不到**（abort 直接杀进程），所以判据写在它**独有的中间量**上：
「调用方丢掉引用之后，这条线程对象**还活着、还在被代持**」。活着 ⇒ 不会命中那条
``qFatal``；被代持 ⇒ 它结束后一定会被回收。
"""

from __future__ import annotations

import threading
import time

import pytest
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from barkit.core import thread_reap
from barkit.plugins.ipad_tunnel import settings
from barkit.plugins.ipad_tunnel.diagnostics import INFO, DiagResult


class _Stubborn(QThread):
    """模拟「正卡在一条外部命令上」：既不查中断标志，也不会很快返回。

    现实中就是 ``curl`` / ``dig`` 卡在网络上那十几秒 —— 收尾能不能扛住，看的正是这一档。
    """

    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        #: 只有显式放它走它才结束（模拟外部命令终于返回）
        self.release = threading.Event()

    def run(self) -> None:  # type: ignore[override]
        self.started.set()
        self.release.wait(10)


class _Polite(QThread):
    """查中断标志的线程：收到 ``requestInterruption()`` 就很快退出。"""

    def run(self) -> None:  # type: ignore[override]
        while not self.isInterruptionRequested():
            time.sleep(0.01)


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    """等到条件成立，**期间持续跑事件循环**。

    必须跑：``finished`` 是从工作线程**排队**回主线程的，主线程干等着（``sleep``）是
    收不到的 —— 那样所有「等它结束」的断言都会假红。真实 app 里事件循环一直在跑，
    不存在这一档。
    """
    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        if app is not None:
            app.processEvents()
        time.sleep(0.01)
    return predicate()


@pytest.fixture(autouse=True)
def _gui_app():
    """**跨线程信号排队回主线程要靠事件循环**，诊断页又是 ``QWidget`` —— 两处都需要
    一个真的 GUI 应用实例；没有它，「等它结束」全都会假红。"""
    app = QApplication.instance()
    return app if app is not None else QApplication([])


@pytest.fixture(autouse=True)
def _no_leaked_threads():
    """兜底清理：用例跑完不该还有代持的线程（残留会污染后一个用例）。

    断言留在用例里 —— 这里只负责「给它一条退路」，免得一条失败拖红后面一片。
    """
    yield
    for w in thread_reap.lingering():
        w.requestInterruption()
        release = getattr(w, "release", None)
        if isinstance(release, threading.Event):
            release.set()      # 「卡住的外部命令」终于返回
        w.wait(3000)
    _wait_until(lambda: not thread_reap.lingering(), timeout=1.0)


def test_stop_returns_true_for_a_thread_that_is_not_running():
    """没起过的线程不必代持 —— 收得住就别往代持集合里塞。"""
    w = _Stubborn()
    assert thread_reap.stop(w, grace_ms=10) is True
    assert thread_reap.lingering() == frozenset()


def test_stop_returns_true_when_the_thread_stops_on_request():
    """查中断标志的线程：要求它停，它就停 ⇒ 调用方可以放手。"""
    w = _Polite()
    w.start()
    assert thread_reap.stop(w, grace_ms=1000) is True
    assert w not in thread_reap.lingering()


def test_stop_keeps_holding_a_thread_that_will_not_stop():
    """**这条是崩溃的正面**：收不住时必须**继续持有**，不许让调用方把引用丢成悬空。

    丢引用那一刻就是 ``~QThread()`` 执行、Qt ``qFatal`` 的那一刻 —— 故断言的是
    「调用方已经不要它了，它还活着（仍在代持里、仍可查询状态）」。
    """
    w = _Stubborn()
    w.start()
    assert w.started.wait(2), "线程没能跑起来，这条用例没测到东西"

    assert thread_reap.stop(w, grace_ms=10) is False
    assert w in thread_reap.lingering()

    # 调用方此刻「死了」：真实场景是页面被销毁、``self._worker = None``
    del w
    still = next(iter(thread_reap.lingering()))
    assert still.isRunning() is True      # 对象还活着 ⇒ 没有命中 Qt 那条 qFatal

    # 线程终于结束 ⇒ 代持放手
    still.release.set()
    assert still.wait(3000), "线程没能结束"
    assert _wait_until(lambda: not thread_reap.lingering())
    assert thread_reap.lingering() == frozenset()


def test_own_releases_the_thread_after_it_finishes():
    """代持不是泄漏：线程自己跑完就放手。"""
    w = _Polite()
    assert thread_reap.own(w) is w
    assert w in thread_reap.lingering()
    w.start()
    w.requestInterruption()
    assert w.wait(3000)
    assert _wait_until(lambda: w not in thread_reap.lingering())


def test_stop_is_idempotent_when_called_twice():
    """同一个线程被要求停两次：只挂一条连接，不重复代持。"""
    w = _Stubborn()
    w.start()
    assert w.started.wait(2)
    try:
        assert thread_reap.stop(w, grace_ms=10) is False
        assert thread_reap.stop(w, grace_ms=10) is False
        assert len(thread_reap.lingering()) == 1
    finally:
        w.release.set()
        w.wait(3000)
    assert _wait_until(lambda: not thread_reap.lingering())


# ──────────────────────────────────────────────
# 隧道诊断那条线程（崩溃现场那一处）
# ──────────────────────────────────────────────

def _slow_steps(stop_flag: threading.Event):
    """每步都慢、且步数没完的生成器：让线程**停在步与步之间**（中断查得到）。"""

    def _gen(*_args, **_kwargs):
        i = 0
        while not stop_flag.is_set():
            i += 1
            time.sleep(0.05)
            yield DiagResult(i, f"第 {i} 步", "cmd", "out", INFO, "")

    return _gen


def _stuck_in_one_command(stop_flag: threading.Event):
    """**卡在一条命令里**（第一步都还没产出）的生成器：中断**查不到**，正是真机上
    ``curl`` 卡住那一档 —— 收尾能不能扛住，看的就是它。"""

    def _gen(*_args, **_kwargs):
        while not stop_flag.is_set():
            time.sleep(0.05)
        yield DiagResult(1, "第 1 步", "cmd", "out", INFO, "")

    return _gen


def test_diag_worker_stops_at_the_next_step_and_always_reports_finished(monkeypatch):
    """关窗 ⇒ 不再往下跑，且 **``finished`` 一定发**（代持方靠它回收本线程）。"""
    stop_flag = threading.Event()
    monkeypatch.setattr(settings, "run_diagnostics", _slow_steps(stop_flag))

    worker = settings._DiagWorker("8.8.8.8", "example.com")
    seen: list[int] = []
    done: list[int] = []
    worker.step_ready.connect(lambda r: seen.append(r.step))
    worker.finished.connect(lambda: done.append(1))
    worker.start()

    assert _wait_until(lambda: len(seen) >= 1), "第一步都没跑出来"
    worker.requestInterruption()
    assert worker.wait(3000), "线程没能在中断后退出"
    stop_flag.set()

    first = seen[0]
    assert seen == [first], f"中断后不该再往下跑：{seen}"
    # ``finished`` 排队回主线程 ⇒ 要跑事件循环才收得到（上面 ``wait`` 只是等它跑完）。
    # 只问「至少发了一次」：本类自己声明了一个 ``finished``，与 ``QThread`` 内建那个
    # 同名，实测一次结束会触发两次 —— 次数不是这里要守的东西，**发没发**才是。
    assert _wait_until(lambda: len(done) >= 1), "``finished`` 必须发，否则代持方永不放手"


def test_diag_pane_hands_the_running_thread_over_instead_of_dropping_it(qtbot, monkeypatch):
    """**收不住时页面不许丢引用** —— 这条跑的就是崩溃现场那条路：关窗、线程还在跑。"""
    stop_flag = threading.Event()
    monkeypatch.setattr(settings, "run_diagnostics", _stuck_in_one_command(stop_flag))

    pane = settings.build_diag_page()
    qtbot.addWidget(pane)
    pane._on_run()
    worker = pane._worker
    assert worker is not None and worker.isRunning()

    pane._stop_worker()      # 关窗 / 销毁走的正是这一条
    assert pane._worker is None            # 页面不再持有
    assert worker in thread_reap.lingering()   # 但**有人**持有 ⇒ 不会被析构 ⇒ 不崩

    stop_flag.set()
    assert worker.wait(3000)
    assert _wait_until(lambda: not thread_reap.lingering())
