"""core/scheduler + events 测试（P0 0.5；2026-08 补充可靠性用例）。

批次 8 起调度节拍来自 ``core.heartbeat``（单一心跳）。本文件**注入快速心跳**
（20ms 采样）——时钟语义由 ``test_heartbeat`` 逐条覆盖（累积式到期 / 相位 /
休眠 / 注销），这里只验证「调度行为」，不必每个 tick 真实等 1s。
"""

import threading
import time

from PySide6.QtCore import QObject

from barkit.core import scheduler as sched_mod
from barkit.core.config import Config
from barkit.core.events import EventBus
from barkit.core.heartbeat import Heartbeat, phase_for
from barkit.core.plugin import (
    FAIL_BREAK,
    FAIL_LOG,
    RUN_ON_DEMAND,
    RUN_PERIODIC,
    Plugin,
    PluginContext,
)
from barkit.core.scheduler import (
    DEFAULT_POLL,
    FAIL_THRESHOLD,
    TICK_TIMEOUT,
    Scheduler,
    poll_default_of,
    retry_delay_for,
    suggested_poll_of,
)


class FakeDaemon(Plugin):
    id = "fake"
    run_mode = RUN_PERIODIC
    failure_policy = FAIL_LOG

    def __init__(self, ctx, fail=False):
        super().__init__(ctx)
        self.fail = fail
        self.ticks = []

    def tick(self):
        self.ticks.append(threading.get_ident())
        if self.fail:
            raise RuntimeError("boom")


def _ctx(tmp_path, poll=0.05) -> PluginContext:
    user = tmp_path / "config.yaml"
    user.write_text(f"plugins:\n  fake:\n    enabled: true\n    poll: {poll}\n", encoding="utf-8")
    return PluginContext(config=Config(user_path=user))


def _sched(events, beat_ms: int = 20) -> Scheduler:
    """注入快速心跳构造调度器（采样 20ms，见模块 docstring）。

    ``heartbeat`` 对象由 Scheduler 持有（``_heartbeat``），不会提前被回收。
    """
    return Scheduler(events, Heartbeat(beat_ms=beat_ms))


def test_tick_runs_on_worker_thread(qtbot, tmp_path):
    main_thread = threading.get_ident()
    events = EventBus()
    sched = _sched(events)
    plugin = FakeDaemon(_ctx(tmp_path))
    sched.add_plugin(plugin)
    qtbot.waitUntil(lambda: len(plugin.ticks) >= 3, timeout=3000)
    assert all(t != main_thread for t in plugin.ticks)
    sched.stop_all()


def test_fail_break_plugin_breaker(qtbot, tmp_path):
    events = EventBus()
    sched = _sched(events)
    broken = []
    events.subscribe("plugin_broken", broken.append)
    plugin = FakeDaemon(_ctx(tmp_path), fail=True)
    plugin.failure_policy = FAIL_BREAK
    sched.add_plugin(plugin)
    qtbot.waitUntil(lambda: sched.status("fake")["broken"], timeout=10000)
    assert len(broken) == 1 and broken[0]["id"] == "fake"
    # 熔断后不再调度
    count = len(plugin.ticks)
    qtbot.wait(300)
    assert len(plugin.ticks) == count
    sched.stop_all()


def test_broken_recovered_signals(qtbot, tmp_path):
    """F.3：熔断发 broken（带错误与计数），面板重试后发 recovered。"""
    events = EventBus()
    sched = _sched(events)
    broken, recovered = [], []
    sched.broken.connect(lambda pid, err, n: broken.append((pid, err, n)))
    sched.recovered.connect(recovered.append)
    plugin = FakeDaemon(_ctx(tmp_path), fail=True)
    plugin.failure_policy = FAIL_BREAK
    sched.add_plugin(plugin)
    qtbot.waitUntil(lambda: bool(broken), timeout=10000)
    pid, err, n = broken[0]
    assert pid == "fake" and "boom" in err and n == FAIL_THRESHOLD
    # 修好后重试：清零计数并广播恢复
    plugin.fail = False
    sched.retry("fake")
    assert recovered == ["fake"]
    assert sched.status("fake")["fail_count"] == 0
    sched.stop_all()


def test_retry_resets_breaker(qtbot, tmp_path):
    events = EventBus()
    sched = _sched(events)
    plugin = FakeDaemon(_ctx(tmp_path), fail=True)
    plugin.failure_policy = FAIL_BREAK
    sched.add_plugin(plugin)
    qtbot.waitUntil(lambda: sched.status("fake")["broken"], timeout=10000)
    # 修好后重试：计数清零、解除熔断、立即点火（不再等下一拍）
    plugin.fail = False
    sched.retry("fake")
    st = sched.status("fake")
    assert st["broken"] is False and st["fail_count"] == 0
    # 刻意不断言 running：retry 立即点火，此刻 tick 可能已跑完（时序不定），
    # 故只断言「确实又跑了一轮」。
    n = len(plugin.ticks)
    qtbot.waitUntil(lambda: len(plugin.ticks) > n, timeout=3000)
    sched.stop_all()


def test_fail_log_never_breaks(qtbot, tmp_path):
    events = EventBus()
    sched = _sched(events)
    plugin = FakeDaemon(_ctx(tmp_path), fail=True)  # failure_policy=FAIL_LOG
    sched.add_plugin(plugin)
    qtbot.waitUntil(lambda: len(plugin.ticks) >= FAIL_THRESHOLD + 2, timeout=10000)
    assert not sched.status("fake")["broken"]
    sched.stop_all()


def test_event_bus_cross_thread(qtbot):
    bus = EventBus()
    received = []
    bus.subscribe("state_changed", received.append)

    class Emitter(QObject):
        pass

    def worker():
        bus.publish("state_changed", {"n": 1})

    t = threading.Thread(target=worker)
    t.start()
    qtbot.waitUntil(lambda: bool(received), timeout=3000)
    t.join()
    assert received[0] == {"n": 1}
    # 分发发生在总线线程（主线程）
    assert threading.get_ident() == threading.main_thread().ident


# ──────────────────────────────────────────────
# 2026-08 可靠性加固用例
# ──────────────────────────────────────────────

def test_stale_tick_reclaimed_on_fire(qtbot, tmp_path):
    """僵尸回收：_running 被超龄 tick 永久占住时，下一轮 _fire 强制清理并恢复调度。

    **断言必须确定性**：回收后 `_on_beat` 会立刻把 `_running` 重新占给新 tick，
    故不能在「观察到新 tick」后直接断言 `_running` 为空——那是与被回收者抢占的
    竞态（全量跑时偶发）。这里先停表（此后不会有新 tick 抢 `_running`），再等
    在飞的那一轮收尾，断言不残留。
    """
    events = EventBus()
    sched = _sched(events)
    plugin = FakeDaemon(_ctx(tmp_path))
    sched.add_plugin(plugin)
    qtbot.waitUntil(lambda: len(plugin.ticks) > 0, timeout=3000)
    # 模拟坏死：_running 被一个远超 watchdog 阈值的 tick 占住
    sched._running.add("fake")
    sched._run_since["fake"] = time.monotonic() - TICK_TIMEOUT - 100
    n = len(plugin.ticks)
    qtbot.waitUntil(lambda: len(plugin.ticks) > n, timeout=3000)  # fire 兜底回收并继续

    sched._heartbeat.stop()                                        # 停表：不再有新 tick
    qtbot.waitUntil(lambda: "fake" not in sched._running, timeout=3000)
    sched.stop_all()


def test_broken_auto_retry(qtbot, tmp_path, monkeypatch):
    """熔断自愈：FAIL_BREAK 插件熔断后，无需手动重试，延迟后自动恢复轮询。"""
    monkeypatch.setattr("barkit.core.scheduler.RETRY_DELAY", 0.2)
    events = EventBus()
    sched = _sched(events)
    plugin = FakeDaemon(_ctx(tmp_path), fail=True)
    plugin.failure_policy = FAIL_BREAK
    sched.add_plugin(plugin)
    qtbot.waitUntil(lambda: sched.status("fake")["broken"], timeout=10000)
    # 修复插件，等待自动重试（不调用 sched.retry）
    plugin.fail = False
    n = len(plugin.ticks)
    qtbot.waitUntil(
        lambda: len(plugin.ticks) > n and not sched.status("fake")["broken"],
        timeout=5000,
    )
    sched.stop_all()


def test_retry_delay_for_grows_then_caps(monkeypatch):
    """退避档位：首轮 = ``RETRY_DELAY``，此后按底数翻倍，撞到上沿即收敛。"""
    monkeypatch.setattr(sched_mod, "RETRY_DELAY", 30)
    monkeypatch.setattr(sched_mod, "RETRY_BACKOFF", 2)
    monkeypatch.setattr(sched_mod, "RETRY_DELAY_MAX", 300)
    assert retry_delay_for(1) == 30          # 首轮：不乘底数
    assert retry_delay_for(2) == 60
    assert retry_delay_for(3) == 120
    assert retry_delay_for(4) == 240
    assert retry_delay_for(5) == 300         # 上沿
    assert retry_delay_for(500) == 300       # 收敛：不会溢出成等不到的定时器
    assert retry_delay_for(0) == 30          # 非法轮次落回最保守那一档


def test_breaker_backoff_grows_until_a_success(qtbot, tmp_path, monkeypatch):
    """熔断轮次驱动退避：第 2 次熔断等更久，一次成功 tick 把档位归零。

    三个常量按**秒**缩放但**不缩到亚秒**——自愈定时器若在用例里真的到期，
    它会自动 retry 并推进轮次，断言看到的就不是刚熔断那一档了。
    """
    monkeypatch.setattr(sched_mod, "RETRY_DELAY", 5)
    monkeypatch.setattr(sched_mod, "RETRY_BACKOFF", 2)
    monkeypatch.setattr(sched_mod, "RETRY_DELAY_MAX", 300)
    events = EventBus()
    sched = _sched(events)
    plugin = FakeDaemon(_ctx(tmp_path), fail=True)
    plugin.failure_policy = FAIL_BREAK
    sched.add_plugin(plugin)

    # 第一次熔断 → 首轮间隔
    qtbot.waitUntil(lambda: sched.status("fake")["broken"], timeout=10000)
    assert sched._retry_timers["fake"].interval() == 5_000

    # 手动重试：清零失败计数、解熔、立即点火 ⇒ 再失败 5 次即第二次熔断。
    # 手动 retry **不**清退避档位（用户催一次不该把攒下的退避抹掉）。
    sched.retry("fake")
    qtbot.waitUntil(lambda: sched.status("fake")["broken"], timeout=10000)
    assert sched._breaker_rounds["fake"] == 2
    assert sched._retry_timers["fake"].interval() == 10_000

    # 修好后重试一次即成功 ⇒ 退避归零（下一轮熔断重新从首轮起步）
    plugin.fail = False
    sched.retry("fake")
    qtbot.waitUntil(lambda: sched._breaker_rounds.get("fake") == 0, timeout=10000)
    assert sched.status("fake")["broken"] is False

    plugin.fail = True
    qtbot.waitUntil(lambda: sched.status("fake")["broken"], timeout=10000)
    assert sched._retry_timers["fake"].interval() == 5_000
    sched.stop_all()


def test_breaker_rounds_do_not_survive_plugin_removal(qtbot, tmp_path, monkeypatch):
    """退避档位随实例一起失效：同名插件重装不该继承上一次的故障史。"""
    monkeypatch.setattr(sched_mod, "RETRY_DELAY", 5)
    events = EventBus()
    sched = _sched(events)
    plugin = FakeDaemon(_ctx(tmp_path), fail=True)
    plugin.failure_policy = FAIL_BREAK
    sched.add_plugin(plugin)
    qtbot.waitUntil(lambda: sched.status("fake")["broken"], timeout=10000)
    assert sched._breaker_rounds["fake"] == 1
    sched.remove_plugin("fake")
    assert "fake" not in sched._breaker_rounds
    sched.stop_all()


# ──────────────────────────────────────────────
# 批次 8：时钟收归心跳（§8.3.3 层 1 + 层 2）
# ──────────────────────────────────────────────


def test_first_tick_fires_immediately_without_waiting_for_poll(qtbot, tmp_path):
    """启动即跑一轮保留：poll=300s 也必须立刻有数据（首屏零陈旧）。

    首轮由 ``add_plugin`` 同步点火，不经过心跳——此刻 Qt 事件循环尚未启动，
    这是首屏数据的唯一来源。
    """
    events = EventBus()
    hb = Heartbeat(beat_ms=1000)
    sched = Scheduler(events, hb)
    plugin = FakeDaemon(_ctx(tmp_path, poll=300))
    sched.add_plugin(plugin)
    qtbot.waitUntil(lambda: len(plugin.ticks) >= 1, timeout=2000)
    assert hb.ticker_count() == 1

    sched.stop_all()
    assert hb.ticker_count() == 0                 # 停表后无孤儿订阅


def test_phase_and_interval_wired_from_plugin_id(qtbot, tmp_path):
    """层 1+2 接线：节拍挂心跳，相位由 plugin.id 复现地错开（禁 hash()）。"""
    events = EventBus()
    hb = Heartbeat(beat_ms=20)
    sched = Scheduler(events, hb)
    sched.add_plugin(FakeDaemon(_ctx(tmp_path, poll=3)))

    tk = sched._tickers["fake"]
    assert tk.interval == 3.0
    assert tk.phase == phase_for("fake", 3.0)     # 同 id 恒等，可断言
    assert 0.0 <= tk.phase < 3.0
    assert sched._heartbeat is hb                # 注入被采纳（非自建「第三套」）
    assert hb.ticker_keys() == ["fake"]          # 订阅登记在共享心跳上
    assert sched._plugins["fake"] is not None     # retry 立即点火所需的实例引用

    sched.stop_all()


def test_remove_plugin_unsubscribes_ticker(qtbot, tmp_path):
    """禁用/卸载插件后心跳无孤儿订阅（§9 真机清单项的自动版）。"""
    events = EventBus()
    hb = Heartbeat(beat_ms=20)
    sched = Scheduler(events, hb)
    sched.add_plugin(FakeDaemon(_ctx(tmp_path, poll=3)))
    assert hb.ticker_keys() == ["fake"]

    sched.remove_plugin("fake")
    assert hb.ticker_count() == 0
    sched.remove_plugin("fake")                   # 幂等：重复移除不抛
    assert hb.ticker_count() == 0


# ──────────────────────────────────────────────
# 批次 10：按需点火（§8.2.2 handler 只标记脏，不干活）
# ──────────────────────────────────────────────

def _sched_dirty(events, window_ms: int = 0, beat_ms: int = 20) -> Scheduler:
    """调度器 + 指定「脏标记」合并窗口。

    心跳 20ms、被测插件 ``poll=300`` ⇒ 心跳永远不会点火，断言到的点火
    **只能来自 mark_dirty**，与节拍彻底解耦。
    """
    return Scheduler(events, Heartbeat(beat_ms=beat_ms), dirty_window_ms=window_ms)


class _BlockingDaemon(FakeDaemon):
    """首轮 tick 阻塞在 ``gate`` 上，用于验证「在跑期间收到标记 → 完成后补点火」。"""

    def __init__(self, ctx):
        super().__init__(ctx)
        self.gate = threading.Event()
        self.entered = threading.Event()
        self._first = True

    def tick(self):
        if self._first:
            self._first = False
            self.entered.set()
            self.gate.wait(5.0)
        super().tick()


def test_mark_dirty_ignites_a_tick_on_worker_thread(qtbot, tmp_path):
    """按需点火：标记脏 → 调度器在 worker 线程跑一轮 tick。"""
    main_thread = threading.get_ident()
    events = EventBus()
    sched = _sched_dirty(events, window_ms=0)
    plugin = FakeDaemon(_ctx(tmp_path, poll=300))
    sched.add_plugin(plugin)                       # 启动即跑一轮
    qtbot.waitUntil(lambda: len(plugin.ticks) >= 1, timeout=2000)

    sched.mark_dirty("fake", "file")
    qtbot.waitUntil(lambda: len(plugin.ticks) >= 2, timeout=2000)
    assert all(t != main_thread for t in plugin.ticks)      # 绝不占 UI 线程
    assert sched.status("fake")["dirty"] is False           # 已消化
    sched.stop_all()


def test_mark_dirty_coalesces_burst_into_one_tick(qtbot, tmp_path):
    """事件风暴合并：一个窗口内连打 5 次标记，只点火一轮。"""
    events = EventBus()
    sched = _sched_dirty(events, window_ms=60)
    plugin = FakeDaemon(_ctx(tmp_path, poll=300))
    sched.add_plugin(plugin)
    qtbot.waitUntil(lambda: len(plugin.ticks) >= 1, timeout=2000)
    n = len(plugin.ticks)

    for i in range(5):
        sched.mark_dirty("fake", f"burst-{i}")
    qtbot.waitUntil(lambda: len(plugin.ticks) > n, timeout=3000)
    qtbot.wait(300)                                # 窗口 + 余波：不得再点火
    assert len(plugin.ticks) == n + 1
    sched.stop_all()


def test_dirty_while_running_reignites_after_completion(qtbot, tmp_path):
    """在跑期间收到标记：保留脏状态（不并跑、不丢事件），跑完补点火。"""
    events = EventBus()
    sched = _sched_dirty(events, window_ms=0)
    plugin = _BlockingDaemon(_ctx(tmp_path, poll=300))
    sched.add_plugin(plugin)                       # 首轮阻塞在 gate
    qtbot.waitUntil(lambda: plugin.entered.is_set(), timeout=2000)

    sched.mark_dirty("fake", "during-run")
    qtbot.wait(150)                                # 窗口已到期，但目标仍在跑
    assert len(plugin.ticks) == 0                  # 未并跑
    assert sched.status("fake")["dirty"] is True   # 脏标记被保留

    plugin.gate.set()                              # 放行首轮 → 完成后补点火
    qtbot.waitUntil(lambda: len(plugin.ticks) >= 2, timeout=3000)
    assert sched.status("fake")["dirty"] is False
    sched.stop_all()


def test_mark_dirty_unknown_plugin_is_noop(qtbot, tmp_path):
    """未知 plugin_id（已卸载 / 面板误调）安全空转。"""
    sched = _sched_dirty(EventBus(), window_ms=0)
    sched.mark_dirty("nope", "x")                  # 不抛
    assert sched.status("nope")["dirty"] is False
    sched.stop_all()


def test_mark_dirty_discards_for_broken_plugin(qtbot, tmp_path):
    """熔断中不点火、脏标记即丢弃（解熔后的 retry 会无条件点火）。"""
    events = EventBus()
    sched = _sched_dirty(events, window_ms=0)
    plugin = FakeDaemon(_ctx(tmp_path, poll=0.05), fail=True)
    plugin.failure_policy = FAIL_BREAK
    sched.add_plugin(plugin)
    qtbot.waitUntil(lambda: sched.status("fake")["broken"], timeout=10000)

    n = len(plugin.ticks)
    sched.mark_dirty("fake", "x")
    qtbot.wait(200)
    assert len(plugin.ticks) == n
    assert sched.status("fake")["dirty"] is False
    sched.stop_all()


def test_remove_plugin_clears_dirty_and_stops_window(qtbot, tmp_path):
    """移除插件后脏标记随实例失效、窗口定时器停摆（§8.2.4 无泄漏）。"""
    sched = _sched_dirty(EventBus(), window_ms=1000)
    sched.add_plugin(FakeDaemon(_ctx(tmp_path, poll=300)))
    sched.mark_dirty("fake", "x")
    assert sched._dirty_timer.isActive() is True
    assert sched.status("fake")["dirty"] is True

    sched.remove_plugin("fake")
    assert sched.status("fake")["dirty"] is False
    assert sched._dirty_timer.isActive() is False
    sched.stop_all()


def test_remove_plugin_clears_breaker_state(qtbot, tmp_path):
    """熔断状态随实例失效：一个曾熔断的组件，禁用再重挂回来必须照旧跑 tick。

    判据落在「重新登记后又跑了几轮」上，而不是查内部字段：留着旧的 ``_broken``
    时唯一的用户可见症状就是「面板写着已启用、状态永远停在旧结论」，而自愈
    定时器已在移除那一刻被取消，再没有人把它拉起来。
    """
    events = EventBus()
    sched = _sched(events)
    ctx = _ctx(tmp_path)
    broken_plugin = FakeDaemon(ctx, fail=True)
    broken_plugin.failure_policy = FAIL_BREAK
    sched.add_plugin(broken_plugin)
    qtbot.waitUntil(lambda: sched.status("fake")["broken"], timeout=10000)

    sched.remove_plugin("fake")                      # 用户「禁用」
    healed = FakeDaemon(ctx)                         # 修好了再「启用」
    healed.failure_policy = FAIL_BREAK
    sched.add_plugin(healed)
    qtbot.waitUntil(lambda: len(healed.ticks) >= 2, timeout=5000)
    assert sched.status("fake")["fail_count"] == 0
    sched.stop_all()


def test_late_report_from_a_removed_instance_is_not_adopted(qtbot, tmp_path):
    """移除 / 停机之后，旧实例迟到的那一帧报告不得被采纳。

    「熔断状态随实例失效」清的是**移除那一刻**的账；旧实例还有一帧报告在路上时，
    它回来会把刚清掉的账又写一遍 —— 症状与那条一模一样：组件已经不在调度器里，
    却背着别人的故障史，重挂回来时带着失败计数。

    走 ``_report`` 信号而不是直接调槽：这条约束守的是「迟到回调认不认得出自己」，
    接线本身也是它的一半。
    """
    events = EventBus()
    sched = _sched(events)
    plugin = FakeDaemon(_ctx(tmp_path, poll=300))
    plugin.failure_policy = FAIL_BREAK
    sched.add_plugin(plugin)
    qtbot.waitUntil(lambda: "fake" not in sched._running, timeout=3000)  # 首轮跑完、报告已回
    gen = sched._gens["fake"]

    sched.remove_plugin("fake")
    assert sched.status("fake")["fail_count"] == 0
    sched._report.emit(plugin, False, "旧实例迟到的失败", gen)
    assert sched.status("fake")["fail_count"] == 0, "组件已不在调度器里，没人该给它记账"

    sched.add_plugin(plugin)
    qtbot.waitUntil(lambda: "fake" not in sched._running, timeout=3000)
    gen = sched._gens["fake"]
    sched.stop_all()
    sched._report.emit(plugin, False, "停机后迟到的失败", gen)
    assert sched.status("fake")["fail_count"] == 0, "停机后仍被记账"


def test_old_instances_watchdog_does_not_kill_the_new_instance(qtbot, tmp_path):
    """禁用再启用之后，旧实例那枚已排上队的 watchdog 不得认领新实例的在飞 tick。

    代号是「**这一次执行**」的编号，两个消费方（watchdog 认领 / 报告认领）都靠
    「对不上就丢」。登记时归零 ⇒ 新实例第一拍又是 1、与旧实例撞号 ⇒ 旧实例那枚
    ``TICK_TIMEOUT`` 后才到点的 watchdog 会把新实例的 tick 判成超时：记一次假失败、
    并把在飞标记摘掉。判据落在「新实例有没有被记这次假失败」上。
    """
    events = EventBus()
    sched = _sched(events, beat_ms=1000)          # 慢心跳：本用例只走「登记即点火」那一拍
    old = FakeDaemon(_ctx(tmp_path, poll=300))
    old.failure_policy = FAIL_BREAK
    sched.add_plugin(old)
    qtbot.waitUntil(lambda: "fake" not in sched._running, timeout=3000)
    old_gen = sched._gens["fake"]
    sched.remove_plugin("fake")

    # 复用本文件既有的 ``_BlockingDaemon``（首轮 tick 卡在它自己的闸门上）：本用例
    # 要的正是「新实例的首轮**在飞**」，否则 worker 跑得太快、判据会飘。
    fresh = _BlockingDaemon(_ctx(tmp_path, poll=300))
    fresh.failure_policy = FAIL_BREAK
    sched.add_plugin(fresh)
    assert fresh.entered.wait(3), "新实例的首轮没跑起来"
    assert "fake" in sched._running
    assert sched._gens["fake"] != old_gen, "代号跨实例必须只增不减（归零就会与旧实例撞号）"

    sched._watchdog(old, old_gen)                 # 旧实例那枚 watchdog 此刻到点
    assert sched.status("fake")["fail_count"] == 0, "旧实例的 watchdog 认领了新实例的 tick"
    assert "fake" in sched._running, "新实例的在飞标记被旧 watchdog 摘掉了"

    fresh.gate.set()
    sched.stop_all()


def test_plugin_mark_dirty_degrades_without_framework(qtbot, tmp_path):
    """ctx 未接线（测试桩 / 无调度器）时静默空转，不抛异常。"""
    plugin = FakeDaemon(_ctx(tmp_path))
    assert plugin.ctx.mark_dirty is None
    plugin.mark_dirty("x")                         # 不抛


def test_plugin_mark_dirty_binds_own_id(qtbot, tmp_path):
    """ctx 是共享单例、拿不到调用者身份 ⇒ Plugin.mark_dirty 必须填 self.id。"""
    calls: list = []
    ctx = _ctx(tmp_path)
    ctx.mark_dirty = lambda pid, reason: calls.append((pid, reason))
    plugin = FakeDaemon(ctx)
    plugin.mark_dirty("state_file")
    assert calls == [("fake", "state_file")]


# ---------- D21：轮询周期进声明（suggested_poll）作为「建议值」 ----------
#
# 变的只是**数值来源**（字面量 DEFAULT_POLL → 组件声明），调度权 / 线程 / 失败
# 处置一概不变；用户配置永远压过组件建议（组件给的是建议，不是命令）。


class FakeDeclared(Plugin):
    """声明了建议周期的周期组件（与 FakeDaemon 的唯一差别就是那一个声明位）。"""

    id = "declared"
    run_mode = RUN_PERIODIC
    failure_policy = FAIL_LOG
    suggested_poll = 7

    def tick(self):
        pass


class FakeBadDeclare(Plugin):
    """声明位写坏了（0 = 非正数）：回落兜底值，且必须留痕。"""

    id = "bad_declare"
    run_mode = RUN_PERIODIC
    failure_policy = FAIL_LOG
    suggested_poll = 0

    def tick(self):
        pass


def _ctx_raw(tmp_path, section: str = "") -> PluginContext:
    """不预置任何插件段的 ctx（D21 用例要断言「用户没配时」的默认值）。"""
    user = tmp_path / "config.yaml"
    user.write_text(section, encoding="utf-8")
    return PluginContext(config=Config(user_path=user))


def test_poll_default_comes_from_declaration(qtbot, tmp_path):
    """用户没配 poll 时，框架用组件声明的 suggested_poll（D21）。"""
    sched = _sched(EventBus())
    sched.add_plugin(FakeDeclared(_ctx_raw(tmp_path)))
    assert sched._tickers["declared"].interval == 7.0
    sched.stop_all()


def test_configured_poll_beats_declaration(qtbot, tmp_path):
    """用户配置压过组件建议（D21：节流是用户对电池 / CPU 的正当控制面）。"""
    sched = _sched(EventBus())
    ctx = _ctx_raw(
        tmp_path, "plugins:\n  declared:\n    enabled: true\n    poll: 0.2\n"
    )
    sched.add_plugin(FakeDeclared(ctx))
    assert sched._tickers["declared"].interval == 0.2
    sched.stop_all()


def test_missing_declaration_falls_back_and_warns(qtbot, tmp_path, monkeypatch):
    """漏声明（RUN_PERIODIC 却没填 suggested_poll）→ 回落兜底值并记 warning。

    **不静默空转**：漏填是作者写错了，日志里必须留痕，否则「我的插件为什么 30s
    才跑一次」查不到任何线索。日志用打桩断言（不依赖 logging 配置）。
    """
    lines: list[str] = []
    monkeypatch.setattr(
        sched_mod.log, "warning", lambda fmt, *a, **k: lines.append(fmt % a)
    )
    sched = _sched(EventBus())
    sched.add_plugin(FakeDaemon(_ctx_raw(tmp_path)))       # FakeDaemon 没填声明位
    assert sched._tickers["fake"].interval == float(DEFAULT_POLL)
    assert any("未声明 suggested_poll" in ln for ln in lines), lines
    sched.stop_all()


def test_nonpositive_declaration_falls_back(tmp_path):
    """声明非正数 = 写坏了：回落兜底值，不把 0 交给心跳（heartbeat 会退化成每拍）。"""
    assert suggested_poll_of(FakeBadDeclare(_ctx_raw(tmp_path))) == float(DEFAULT_POLL)


class OnDemand(Plugin):
    """按需型替身：没有节拍，只在「启动 / 事件 / 重试」时跑。"""

    id = "on_demand"
    run_mode = RUN_ON_DEMAND
    failure_policy = FAIL_LOG

    def __init__(self, ctx):
        super().__init__(ctx)
        self.ticks: list[int] = []

    def tick(self):
        self.ticks.append(threading.get_ident())


def test_on_demand_plugin_is_registered_but_has_no_ticker(qtbot, tmp_path):
    """按需型：**进 ``_plugins``（为的是能被点火），但不订阅心跳**（没有节拍）。

    登记与节拍是两件事 —— 混成一件就出过事：按需型在 ``add_plugin`` 里被整个挡回去，
    于是事件 handler 里那句 ``mark_dirty`` **静默落空**（``mark_dirty`` 对未知 id 是
    有意静默的），「只靠系统事件干活」的组件只能谎报成周期型（见 ``core/audio`` 的来历）。
    """
    sched = _sched(EventBus())
    plugin = OnDemand(_ctx_raw(tmp_path))
    sched.add_plugin(plugin)
    assert sched._tickers == {}, "按需型不订阅节拍"
    assert sched._heartbeat.ticker_count() == 0
    assert sched._plugins["on_demand"] is plugin, "但必须在册，否则点不着火"
    sched.stop_all()


def test_on_demand_plugin_runs_once_on_start(qtbot, tmp_path):
    """启动即跑一轮对按需型同样成立 —— 那是它唯一的**自动**首屏来源。"""
    sched = _sched(EventBus())
    plugin = OnDemand(_ctx_raw(tmp_path))
    sched.add_plugin(plugin)
    qtbot.waitUntil(lambda: len(plugin.ticks) >= 1, timeout=2000)
    sched.stop_all()


def test_mark_dirty_ignites_an_on_demand_plugin(qtbot, tmp_path):
    """**这一条就是那次改动的全部理由**：事件来了，按需型真的会跑。

    心跳被注入成 20ms，而按需型没有 ticker ⇒ 断言到的点火**只可能来自 mark_dirty**。
    """
    main_thread = threading.get_ident()
    events = EventBus()
    sched = Scheduler(events, Heartbeat(beat_ms=20), dirty_window_ms=0)
    plugin = OnDemand(_ctx_raw(tmp_path))
    sched.add_plugin(plugin)
    qtbot.waitUntil(lambda: len(plugin.ticks) >= 1, timeout=2000)
    n = len(plugin.ticks)

    sched.mark_dirty("on_demand", "system_event")
    qtbot.waitUntil(lambda: len(plugin.ticks) > n, timeout=2000)
    assert all(t != main_thread for t in plugin.ticks), "绝不占 UI 线程"
    sched.stop_all()


def test_disabling_an_on_demand_plugin_stops_the_ignition(qtbot, tmp_path):
    """禁用后不许再被叫醒（§8.2.4 无泄漏）：``remove_plugin`` 对两种模式都无条件摘除。

    漏摘的表现很隐蔽：界面上组件已经关掉了，系统事件一来它还在后台跑探针。
    """
    events = EventBus()
    sched = Scheduler(events, Heartbeat(beat_ms=20), dirty_window_ms=0)
    plugin = OnDemand(_ctx_raw(tmp_path))
    sched.add_plugin(plugin)
    qtbot.waitUntil(lambda: len(plugin.ticks) >= 1, timeout=2000)

    sched.remove_plugin("on_demand")
    n = len(plugin.ticks)
    sched.mark_dirty("on_demand", "after-disable")
    qtbot.wait(200)
    assert len(plugin.ticks) == n
    assert sched.status("on_demand")["dirty"] is False
    sched.stop_all()


def test_on_demand_declaration_never_becomes_a_pace(qtbot, tmp_path):
    """声明位填了也不生效（D21：节拍只给周期型挂），取值照样算得出。"""

    class Declared(OnDemand):
        suggested_poll = 3

    sched = _sched(EventBus())
    sched.add_plugin(Declared(_ctx_raw(tmp_path)))
    assert sched._tickers == {}
    assert suggested_poll_of(Declared(_ctx_raw(tmp_path))) == 3.0
    sched.stop_all()


# ---------- D21 的用户入口：设置窗改完节拍要**当场**生效 ----------


def test_set_poll_picks_up_the_new_config(qtbot, tmp_path):
    """用户改了 ``plugins.<id>.poll`` → ``set_poll`` 当场换节拍（不必重启）。

    这是设置窗那个「刷新间隔」下拉的兑现处：只落盘不重挂，界面上改完纹丝不动、
    要重启才生效 —— 一次静默空转。重挂而不是改 ``Ticker.interval``，因为相位也是
    按 interval 算的（只改数字会让「同节拍的插件错开到期」失真）。
    """
    ctx = _ctx_raw(tmp_path, "plugins:\n  declared:\n    enabled: true\n    poll: 0.2\n")
    sched = _sched(EventBus())
    sched.add_plugin(FakeDeclared(ctx))
    assert sched._tickers["declared"].interval == 0.2

    ctx.config.set_plugin_poll("declared", 5)
    assert sched.set_poll("declared") is True
    assert sched._tickers["declared"].interval == 5.0
    assert sched._heartbeat.ticker_count() == 1, "旧 ticker 必须摘掉，不许留孤儿定时"

    ctx.config.set_plugin_poll("declared", None)       # 恢复默认（删键）
    sched.set_poll("declared")
    assert sched._tickers["declared"].interval == 7.0, "回到组件建议值"
    assert sched._heartbeat.ticker_count() == 1
    sched.stop_all()


def test_set_poll_outside_the_scheduler_is_not_a_failure(qtbot, tmp_path):
    """**没有节拍可挂**的组件 → 返回 ``False``，配置照样落盘（不是失败）。

    两条腿：① 未启用（没进调度器）；② 按需型（在调度器里，但本来就没有节拍）。
    设置窗只给周期型组件画这个控件，正常路径走不到这里；留着是为「关掉再打开」与
    「卸载收尾」两个时序缝隙 —— 以及防「给按需型挂上节拍」这件事被别的调用点悄悄
    做出来（那会让「按需」名不副实）。
    """
    sched = _sched(EventBus())
    assert sched.set_poll("never_enabled") is False

    sched.add_plugin(OnDemand(_ctx_raw(tmp_path)))
    assert sched.set_poll("on_demand") is False
    assert sched._tickers == {}
    assert sched._heartbeat.ticker_count() == 0
    sched.stop_all()


def test_garbage_poll_in_config_falls_back_to_declaration(qtbot, tmp_path, monkeypatch):
    """配置里写了非数字 → 回退**组件建议**，不是兜底 30s，更不能崩。

    用户填坏了不该顺手把组件自己的合理节拍也丢掉；而 ``float("abc")`` 以前会直接
    抛 ValueError 把 ``add_plugin`` 打穿（用户手改 YAML 就能触发）。
    """
    lines: list[str] = []
    monkeypatch.setattr(
        sched_mod.log, "warning", lambda fmt, *a, **k: lines.append(fmt % a)
    )
    ctx = _ctx_raw(tmp_path, "plugins:\n  declared:\n    enabled: true\n    poll: 一会儿\n")
    sched = _sched(EventBus())
    sched.add_plugin(FakeDeclared(ctx))
    assert sched._tickers["declared"].interval == 7.0
    assert any("非法" in ln for ln in lines), lines
    sched.stop_all()


def test_poll_default_of_ignores_unusable_declarations(tmp_path):
    """默认值判据只认正整数：``0`` / 布尔 / 字符串 / 浮点 / 缺省一律回落兜底值。

    布尔是 ``int`` 子类（``True`` 会被当成 1 混进来），故必须显式排除 —— 这条在
    ``poll_default_of`` 上单测，因为设置窗也直接问它（手上只有类属性、没有实例）。
    """
    for declared in (0, -1, True, "7", None, 7.5):
        assert poll_default_of(declared, RUN_PERIODIC, "p") == float(DEFAULT_POLL), declared
    assert poll_default_of(7, RUN_PERIODIC, "p") == 7.0
    assert poll_default_of(300, RUN_ON_DEMAND, "p") == 300.0, "按需型不问，但取值照样算得出"

