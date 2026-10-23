"""core/heartbeat 测试（批次 8 · S1）：单一心跳 + 相位错开。

**确定性驱动**：注入 fake clock + 直接调 ``_beat()``，不依赖真实等待、
不依赖 Qt 事件循环节拍——到期语义必须能被证明，而不是「跑一会儿看看」。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from barkit.core.heartbeat import DEFAULT_BEAT_MS, MIN_BEAT_MS, Heartbeat, phase_for

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeClock:
    """可控时间轴：测试里显式赋值 ``t``，避免浮点累积误差。"""

    def __init__(self, t: float = 0.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t


def _hb(clock: FakeClock, beat_ms: int = DEFAULT_BEAT_MS) -> Heartbeat:
    return Heartbeat(beat_ms=beat_ms, clock=clock)


# ---------- 基本到期 ----------


def test_fires_only_at_phase_offset(qapp):
    c = FakeClock()
    hb = _hb(c)
    hits: list[float] = []
    hb.add_ticker(3.0, lambda: hits.append(c.t), phase=1.5)

    hb._beat()                      # t=0
    assert hits == []
    c.t = 1.4
    hb._beat()
    assert hits == []               # 相位未到
    c.t = 1.5
    hb._beat()
    assert hits == [1.5]            # 首轮在 phase 之后
    c.t = 3.0
    hb._beat()
    assert hits == [1.5]            # 距上次仅 1.5s < interval
    c.t = 4.5
    hb._beat()
    assert hits == [1.5, 4.5]       # 之后按 interval 稳定推进


def test_cumulative_no_drift_no_double_fire(qapp):
    """累积式到期：恰好落在周期上触发且只触发一次（不准漏、不准双倍）。"""
    c = FakeClock()
    hb = _hb(c)
    hits: list[int] = []
    hb.add_ticker(3.0, lambda: hits.append(1), phase=0.0)

    hb._beat()                      # phase=0 → 首次即到期
    assert len(hits) == 1
    c.t = 2.9
    hb._beat()
    assert len(hits) == 1
    c.t = 3.0
    hb._beat()
    assert len(hits) == 2
    c.t = 5.9
    hb._beat()
    assert len(hits) == 2
    c.t = 6.0
    hb._beat()
    assert len(hits) == 3


def test_sleep_catchup_fires_once_then_resets(qapp):
    """系统休眠回来：只补一次并重置节拍，不补跑整段（防唤醒惊群）。"""
    c = FakeClock()
    hb = _hb(c)
    hits: list[int] = []
    hb.add_ticker(3.0, lambda: hits.append(1), phase=0.0)
    hb._beat()                      # t=0 → 1 次

    c.t = 600.0                     # 休眠 10 分钟
    hb._beat()
    assert len(hits) == 2           # 不是 201 次
    c.t = 601.0
    hb._beat()
    assert len(hits) == 2           # 节拍已重置到 600，下一周期在 603
    c.t = 603.0
    hb._beat()
    assert len(hits) == 3


# ---------- 相位（§8.3.3 层 1）----------


def test_phase_for_is_reproducible_and_bounded(qapp):
    p = phase_for("route_guard", 30.0)
    assert p == phase_for("route_guard", 30.0)      # 同 key 同 interval 恒等
    assert 0.0 <= p < 30.0
    assert phase_for("", 30.0) == 0.0               # 无 key → 不错开
    assert phase_for("route_guard", 0) == 0.0       # 非法 interval → 不错开


def test_phase_for_spreads_distinct_keys(qapp):
    """相位的作用是让同 interval 的订阅不 lockstep（不塌到同一点）。"""
    keys = ["notify_guard", "route_guard", "hosts_guard", "ipad_tunnel"]
    phases = {phase_for(k, 30.0) for k in keys}
    assert len(phases) > 1


def test_phase_for_is_not_quantized_on_whole_second_intervals(qapp):
    """整秒节律（生产里最普遍的 1s 档）不许把相位量化成整秒、更不许塌成 0。

    旧写法 ``crc32(key) % float(interval)`` 是**整数对整数**取模 ⇒ 相位只能落在整秒，
    而 ``interval=1.0`` 时 ``n % 1.0`` 恒等 ``0.0`` ⇒ 1s 档订阅的相位全体为 0，与
    「相位摊在 ``[0, interval)``、同节律不打齐」这条承诺正好相反（2026-10-02 实机排查
    时发现）。这里同时钉两件事：相位**有**细分、且不同 key **互不相同**。
    """
    keys = ["clock", "route_guard", "notify_guard", "hosts_guard", "ipad_tunnel"]
    for interval in (1.0, 3.0):
        phases = [phase_for(k, interval) for k in keys]
        assert all(0.0 <= p < interval for p in phases), phases
        assert len(set(phases)) == len(keys), f"interval={interval} 相位撞车：{phases}"
    # 旧写法的指纹：相位恒为整秒（interval=1.0 时恒为 0）。上面那条断言即由此转红，
    # 这里单独留一条可读的：相位必须带小数部分。
    assert any(phase_for(k, 1.0) % 1.0 != 0.0 for k in keys)


def test_phase_cannot_stagger_tickers_whose_interval_is_the_beat(qapp):
    """**已知边界**：间隔不大于心跳周期的订阅，相位再不同也落在同一拍上。

    到期判定跑在心跳的整数拍（生产 1s）——这是「一个心跳、CoarseTimer 允许系统合并
    唤醒」的必然代价，不是相位公式能修的（见 ``phase_for`` docstring）。钉住它，是为了
    让「相位把 1s 档也错开了」这种说法在仓库里立不住：心跳若调细，相位**确实**能拉开
    （那是改进），届时这条转红，提醒改文档与本条说明。
    """
    c = FakeClock()
    hb = _hb(c)                     # beat_ms=1000，与 ticker 的 interval 同
    a: list[float] = []
    b: list[float] = []
    hb.add_ticker(1.0, lambda: a.append(c.t), key="clock", phase=0.2)
    hb.add_ticker(1.0, lambda: b.append(c.t), key="route_guard", phase=0.8)
    step = hb.beat_ms / 1000.0      # 按**真实心跳步长**推进，不是写死 1s
    for i in range(1, int(3.0 / step) + 1):
        c.t = round(i * step, 6)
        hb._beat()
    assert a == b == [1.0, 2.0, 3.0], (
        f"相位 0.2 / 0.8 落在了不同拍上：{a} / {b}（心跳 {hb.beat_ms}ms）"
    )


def test_phase_is_stable_across_processes_with_randomized_hash():
    """禁用 hash() 的证据：不同 PYTHONHASHSEED 下相位必须一致。"""
    code = (
        "from barkit.core.heartbeat import phase_for;"
        "print(phase_for('route_guard', 30.0))"
    )
    outputs = []
    for seed in ("0", "1"):
        env = {
            **os.environ,
            "PYTHONHASHSEED": seed,
            "PYTHONPATH": str(REPO_ROOT / "src"),
        }
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
        )
        assert proc.returncode == 0, proc.stderr
        outputs.append(proc.stdout.strip())
    assert outputs[0] == outputs[1]


# ---------- 订阅生命周期 ----------


def test_remove_ticker_stops_firing_and_is_idempotent(qapp):
    c = FakeClock()
    hb = _hb(c)
    hits: list[int] = []
    tk = hb.add_ticker(3.0, lambda: hits.append(1), phase=0.0)
    hb._beat()
    assert len(hits) == 1

    hb.remove_ticker(tk)
    hb.remove_ticker(tk)            # 重复注销安全
    hb.remove_ticker(None)          # 空句柄安全
    c.t = 100.0
    hb._beat()
    assert len(hits) == 1           # 无孤儿定时
    assert hb.ticker_count() == 0


def test_callback_exception_is_isolated(qapp):
    """单个订阅抛异常不得中断本拍其余订阅。"""
    c = FakeClock()
    hb = _hb(c)
    hits: list[int] = []

    def boom() -> None:
        raise RuntimeError("boom")

    hb.add_ticker(3.0, boom, key="bad", phase=0.0)
    hb.add_ticker(3.0, lambda: hits.append(1), key="good", phase=0.0)
    hb._beat()
    assert hits == [1]
    assert hb.ticker_count() == 2   # 异常不注销订阅


def test_ticker_can_remove_itself(qapp):
    """回调内注销自身不得破坏本拍遍历。"""
    c = FakeClock()
    hb = _hb(c)
    hits: list[int] = []

    def once() -> None:
        hits.append(1)
        hb.remove_ticker(tk)

    tk = hb.add_ticker(3.0, once, phase=0.0)
    hb._beat()
    c.t = 100.0
    hb._beat()
    assert hits == [1]
    assert hb.ticker_count() == 0


# ---------- 配置边界 ----------


def test_invalid_interval_falls_back_to_every_beat(qapp):
    c = FakeClock()
    hb = _hb(c, beat_ms=100)
    hits: list[int] = []
    hb.add_ticker(0, lambda: hits.append(1), key="bad", phase=0.0)
    hb._beat()
    assert len(hits) == 1
    assert hb.ticker_count() == 1


def test_beat_ms_has_floor_and_reports_running_state(qapp):
    assert Heartbeat(beat_ms=0).beat_ms == MIN_BEAT_MS
    hb = Heartbeat()
    assert hb.beat_ms == DEFAULT_BEAT_MS
    assert hb.running is False
    hb.start()
    assert hb.running is True
    hb.stop()
    assert hb.running is False


def test_timer_lets_the_system_coalesce_wakeups(qapp):
    """心跳不是精确定时器：必须是 CoarseTimer。

    PreciseTimer 的语义是「精度优先，不允许系统合并/延后唤醒」——常驻菜单栏
    App 挂它等于每秒钉一次唤醒，让 macOS 的 timer coalescing 与 App Nap 全部
    失效。而最短的 ticker 节律就是 1s，CoarseTimer 的 ±5%（±50ms）对「错开
    相位、不 lockstep」这个目的完全够。
    """
    from PySide6.QtCore import Qt

    assert Heartbeat()._timer.timerType() == Qt.TimerType.CoarseTimer
