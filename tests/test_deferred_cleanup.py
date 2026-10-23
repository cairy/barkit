"""core/deferred_cleanup 单测：卸载收尾「等外部消费方收敛」的三条出口。

时钟与心跳都注入（``clock`` + ``_FakeHeartbeat``），用例直接调 ``poll()`` 推进判据 ——
不依赖真实等待，也不会因为机器忙而假红。

三条出口各有用例：**当场收尾**（判据一开始就为真）、**等到为真再收尾**、**超时放弃**
（什么都不撤）。另有一条守住「未接线 ⇒ 调用方自己立即收尾」这个回退语义。
"""

from __future__ import annotations

from barkit.core.deferred_cleanup import DeferredCleaner


class _FakeHeartbeat:
    """只记「谁订了、谁摘了」，不真的计时（推进靠用例显式调 ``poll()``）。"""

    def __init__(self) -> None:
        self.tickers: list = []
        self.removed: list = []

    def add_ticker(self, interval, on_due, *, key="", phase=None):
        tk = (key, interval, on_due)
        self.tickers.append(tk)
        return tk

    def remove_ticker(self, ticker):
        self.removed.append(ticker)
        if ticker in self.tickers:
            self.tickers.remove(ticker)


class _Clock:
    """可控时间轴（``time.time`` 的替身）。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


class _Recorder:
    """记账三件事：提交了几次、放弃了几次、判据收到过什么 watermark。"""

    def __init__(self) -> None:
        self.commits = 0
        self.give_ups = 0
        self.watermarks: list = []

    def schedule(self, cleaner, *, ready, timeout_s=5.0, watermark=900.0) -> None:
        cleaner.schedule(
            label="demo",
            ready=lambda wm: (self.watermarks.append(wm), ready())[1],
            watermark=watermark,
            timeout_s=timeout_s,
            on_commit=self._commit,
            on_give_up=self._give_up,
        )

    def _commit(self) -> None:
        self.commits += 1

    def _give_up(self) -> None:
        self.give_ups += 1


def test_unwired_returns_false_and_touches_nothing():
    """未接时钟 ⇒ schedule 返回 False（调用方据此**立即**收尾），且不碰任何回调。"""
    rec = _Recorder()
    cleaner = DeferredCleaner(None, clock=_Clock())

    assert cleaner.hooked is False
    assert cleaner.schedule(
        label="demo", ready=lambda wm: True, watermark=0.0, timeout_s=5.0,
        on_commit=rec._commit, on_give_up=rec._give_up,
    ) is False
    assert (rec.commits, rec.give_ups, cleaner.pending()) == (0, 0, 0)


def test_ready_at_once_commits_without_waiting():
    """判据当场为真 ⇒ 立即收尾，且**不留**心跳订阅（不留孤儿定时）。"""
    rec = _Recorder()
    hb = _FakeHeartbeat()
    cleaner = DeferredCleaner(hb, clock=_Clock())

    rec.schedule(cleaner, ready=lambda: True)
    assert (rec.commits, rec.give_ups, cleaner.pending()) == (1, 0, 0)
    assert hb.tickers == [] and hb.removed == []


def test_waits_until_consumer_settles():
    """判据为假 ⇒ 等；转为真 ⇒ 收尾一次；订阅随之摘掉。"""
    rec = _Recorder()
    hb = _FakeHeartbeat()
    clock = _Clock()
    cleaner = DeferredCleaner(hb, clock=clock)
    settled = {"v": False}

    rec.schedule(cleaner, ready=lambda: settled["v"], watermark=1234.5)
    assert cleaner.pending() == 1
    assert len(hb.tickers) == 1                      # 挂上了心跳
    assert rec.watermarks == [1234.5]                # 水位线原样交给判据

    clock.advance(1.0)
    cleaner.poll()
    assert (rec.commits, rec.give_ups) == (0, 0)     # 仍未收敛：不动

    settled["v"] = True
    cleaner.poll()
    assert (rec.commits, rec.give_ups, cleaner.pending()) == (1, 0, 0)
    assert hb.tickers == [] and len(hb.removed) == 1  # 摘干净


def test_timeout_gives_up_without_committing():
    """超时 ⇒ 走放弃分支，**不做任何撤回**（文件与 task 声明留给消费方下次收敛）。"""
    rec = _Recorder()
    hb = _FakeHeartbeat()
    clock = _Clock()
    cleaner = DeferredCleaner(hb, clock=clock)

    rec.schedule(cleaner, ready=lambda: False, timeout_s=5.0)
    clock.advance(4.0)
    cleaner.poll()
    assert (rec.commits, rec.give_ups) == (0, 0)      # 到点之前不放弃

    clock.advance(1.5)
    cleaner.poll()
    assert (rec.commits, rec.give_ups, cleaner.pending()) == (0, 1, 0)
    assert hb.tickers == [] and len(hb.removed) == 1


def test_ready_exception_counts_as_not_settled():
    """判据自身抛异常 ⇒ 按「还没收敛」处理（等不到再走超时），绝不误判成可收尾。"""
    rec = _Recorder()
    clock = _Clock()
    cleaner = DeferredCleaner(_FakeHeartbeat(), clock=clock)

    def _boom() -> bool:
        raise RuntimeError("判据坏了")

    rec.schedule(cleaner, ready=_boom, timeout_s=5.0)
    clock.advance(1.0)
    cleaner.poll()
    assert (rec.commits, rec.give_ups) == (0, 0)

    clock.advance(5.0)
    cleaner.poll()
    assert (rec.commits, rec.give_ups) == (0, 1)      # 只是放弃，不是收尾


def test_many_items_share_one_ticker():
    """多笔待办共用一个订阅（不是每笔一个定时），全部完成后订阅摘掉。"""
    hb = _FakeHeartbeat()
    cleaner = DeferredCleaner(hb, clock=_Clock())
    first, second = _Recorder(), _Recorder()
    settled = {"a": False, "b": False}

    first.schedule(cleaner, ready=lambda: settled["a"])
    second.schedule(cleaner, ready=lambda: settled["b"])
    assert len(hb.tickers) == 1 and cleaner.pending() == 2

    settled["a"] = settled["b"] = True
    cleaner.poll()
    assert cleaner.pending() == 0 and (first.commits, second.commits) == (1, 1)
    assert hb.tickers == [] and len(hb.removed) == 1


def test_commit_failure_does_not_break_others():
    """一笔收尾炸了，不该连坐另一笔、也不该把异常抛回心跳。"""
    hb = _FakeHeartbeat()
    cleaner = DeferredCleaner(hb, clock=_Clock())
    calls: list = []

    cleaner.schedule(
        label="bad", ready=lambda wm: True, watermark=0.0, timeout_s=5.0,
        on_commit=lambda: (_ for _ in ()).throw(RuntimeError("炸")),
        on_give_up=lambda: calls.append("bad-giveup"),
    )
    cleaner.schedule(
        label="good", ready=lambda wm: True, watermark=0.0, timeout_s=5.0,
        on_commit=lambda: calls.append("good-commit"),
        on_give_up=lambda: calls.append("good-giveup"),
    )
    assert calls == ["good-commit"]
    assert cleaner.pending() == 0
