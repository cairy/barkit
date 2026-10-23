"""core/resident 测试（批次 5 后段 S1）：契约 + 托管宿主。

不依赖 Qt；假插件只实现 ``id`` 与 ``resident_services()``。
"""

from __future__ import annotations

import threading
import time

from barkit.core.plugin import Plugin, PluginContext
from barkit.core.resident import JOIN_TIMEOUT, ResidentHost


class FakePlugin:
    """最小假插件：只需 id + 可切换的声明清单。"""

    def __init__(self, pid="fake", services=None):
        self.id = pid
        self._services = list(services or [])

    def set_services(self, services):
        self._services = list(services)

    def resident_services(self):
        return list(self._services)


class SelfSustaining:
    """自持循环型：run() 阻塞，直到 stop() 置位。"""

    interval = None

    def __init__(self, name="relay"):
        self.name = name
        self.started = threading.Event()
        self.exited = threading.Event()
        self.stop_calls = 0
        self.teardown_calls = 0
        self._ev = threading.Event()

    def run(self):
        self.started.set()
        self._ev.wait(10)
        self.exited.set()

    def stop(self):
        self.stop_calls += 1
        self._ev.set()

    def teardown(self):
        self.teardown_calls += 1
        self._ev.set()


class Periodic:
    """周期型：poll() 计数。"""

    def __init__(self, name="probe", interval=0.05, fail=False):
        self.name = name
        self.interval = interval
        self.fail = fail
        self.polls = 0
        self.stop_calls = 0
        self._lock = threading.Lock()

    def poll(self):
        with self._lock:
            self.polls += 1
        if self.fail:
            raise RuntimeError("poll boom")

    def stop(self):
        self.stop_calls += 1

    @property
    def count(self):
        with self._lock:
            return self.polls


class RaisingRun:
    interval = None
    name = "boom"

    def run(self):
        raise RuntimeError("run boom")

    def stop(self):
        pass


def _wait(pred, timeout=3.0, step=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


# ---------- 默认声明 / 契约 ----------


def test_plugin_base_declares_no_services():
    assert Plugin(PluginContext(config=None)).resident_services() == []


def test_context_resync_services_defaults_to_none():
    assert PluginContext(config=None).resync_services is None


def test_plugin_without_declaration_spawns_nothing():
    host = ResidentHost()
    plugin = FakePlugin()
    host.start(plugin)
    assert host.active(plugin) == []
    assert host.thread_names() == []


def test_declaration_exception_is_isolated():
    class Bad(FakePlugin):
        def resident_services(self):
            raise RuntimeError("declaration boom")

    host = ResidentHost()
    plugin = Bad()
    host.start(plugin)                    # 不抛
    assert host.active(plugin) == []


def test_duplicate_and_nameless_declarations_ignored():
    svc = SelfSustaining("dup")
    host = ResidentHost()
    plugin = FakePlugin(services=[svc, svc, SelfSustaining("")])
    host.start(plugin)
    assert host.active(plugin) == ["dup"]
    host.stop(plugin)


# ---------- 自持循环型 ----------


def test_self_sustaining_runs_on_own_thread_and_stops():
    main_thread = threading.get_ident()
    svc = SelfSustaining("relay")
    host = ResidentHost()
    plugin = FakePlugin(services=[svc])
    host.start(plugin)

    assert svc.started.wait(2)
    assert host.active(plugin) == ["relay"]
    assert host.thread_names() == ["resident:fake.relay"]
    runner = next(t for t in threading.enumerate() if t.name == "resident:fake.relay")
    assert runner.ident != main_thread          # 未跑在主线程

    host.stop(plugin)
    assert svc.exited.is_set()
    assert svc.stop_calls >= 1
    assert host.active(plugin) == []
    assert not any(
        t.name == "resident:fake.relay" for t in threading.enumerate() if t.ident != main_thread
    )


def test_run_exception_records_problem_and_marks_failed():
    host = ResidentHost()
    plugin = FakePlugin(services=[RaisingRun()])
    host.start(plugin)
    assert _wait(lambda: host.problems())
    pid, reason = host.problems()[0]
    assert pid == "fake" and "boom" in reason
    assert host.active(plugin) == []          # failed 不再计入 active
    host.stop(plugin)                          # 幂等：无记录也不报错


# ---------- 周期型 ----------


def test_periodic_polls_repeatedly_and_stops():
    svc = Periodic("probe", interval=0.05)
    host = ResidentHost()
    plugin = FakePlugin(services=[svc])
    host.start(plugin)

    assert _wait(lambda: svc.count >= 3)
    assert host.active(plugin) == ["probe"]
    host.stop(plugin)
    stopped_at = svc.count
    time.sleep(0.15)
    assert svc.count == stopped_at          # 停止后不再轮询
    assert svc.stop_calls >= 1


def test_periodic_poll_exception_does_not_kill_loop():
    svc = Periodic("probe", interval=0.05, fail=True)
    host = ResidentHost()
    plugin = FakePlugin(services=[svc])
    host.start(plugin)
    assert _wait(lambda: svc.count >= 2)
    assert host.active(plugin) == ["probe"]  # 周期型单次失败不摘除
    assert len(host.problems()) == 1         # 问题只记首次（防刷屏）
    host.stop(plugin)


def test_periodic_invalid_interval_falls_back():
    svc = Periodic("probe", interval=0)
    host = ResidentHost()
    plugin = FakePlugin(services=[svc])
    host.start(plugin)
    assert _wait(lambda: svc.count >= 2)
    host.stop(plugin)


# ---------- 幂等 / 差量收敛 / teardown ----------


def test_start_is_idempotent():
    svc = Periodic("probe", interval=0.05)
    host = ResidentHost()
    plugin = FakePlugin(services=[svc])
    host.start(plugin)
    host.start(plugin)
    host.start(plugin)
    assert host.thread_names() == ["resident:fake.probe"]
    host.stop(plugin)


def test_stop_is_idempotent():
    host = ResidentHost()
    plugin = FakePlugin(services=[Periodic("probe")])
    host.start(plugin)
    host.stop(plugin)
    host.stop(plugin)
    assert host.active(plugin) == []


def test_sync_converges_by_declaration():
    probe = Periodic("probe", interval=0.05)
    relay = SelfSustaining("relay")
    host = ResidentHost()
    plugin = FakePlugin(services=[probe])
    host.start(plugin)
    assert host.active(plugin) == ["probe"]

    # 声明变化：加 relay、去 probe → sync 差量收敛
    plugin.set_services([relay])
    host.sync(plugin)
    assert host.active(plugin) == ["relay"]
    assert probe.stop_calls >= 1
    assert relay.started.wait(2)

    # 再 sync 一次（声明未变）→ 不重建已托管实例
    before = relay.stop_calls
    host.sync(plugin)
    assert relay.stop_calls == before
    assert host.active(plugin) == ["relay"]
    host.stop(plugin)


def test_teardown_calls_service_teardown_then_drops():
    svc = SelfSustaining("relay")
    host = ResidentHost()
    plugin = FakePlugin(services=[svc])
    host.start(plugin)
    host.teardown(plugin)
    assert svc.teardown_calls == 1
    assert host.active(plugin) == []
    assert host.thread_names() == []


def test_teardown_falls_back_to_stop_without_teardown_method():
    svc = Periodic("probe", interval=0.05)
    host = ResidentHost()
    plugin = FakePlugin(services=[svc])
    host.start(plugin)
    host.teardown(plugin)
    assert svc.stop_calls >= 1
    assert host.active(plugin) == []


def test_join_timeout_bounds_and_reports(monkeypatch):
    """失联服务：stop() 不唤醒 → join 超时记问题，但宿主调用不挂起。"""
    released = threading.Event()

    class Stuck(SelfSustaining):
        def run(self):
            self.started.set()
            released.wait(30)          # stop() 故意不唤醒 → 只能靠 join 超时兜底

        def stop(self):
            self.stop_calls += 1

    svc = Stuck("stuck")
    host = ResidentHost()
    plugin = FakePlugin(services=[svc])
    host.start(plugin)
    assert svc.started.wait(2)

    monkeypatch.setattr("barkit.core.resident.JOIN_TIMEOUT", 0.2)
    start = time.monotonic()
    try:
        host.stop(plugin)
        elapsed = time.monotonic() - start
        assert elapsed < 3.0                                  # 未无限等待
        assert any("未退出" in reason and "stuck" in reason for _pid, reason in host.problems())
        assert JOIN_TIMEOUT > 0                               # 常量本身有效
    finally:
        released.set()                                        # 放掉残留线程
    assert _wait(lambda: not any(
        t.name == "resident:fake.stuck" for t in threading.enumerate()
    ))


# ---------- Registry 接线（S2）：真实宿主 + 真实 Registry ----------

from barkit.core.config import Config          # noqa: E402
from barkit.core.registry import Registry      # noqa: E402


class _SvcService:
    """自持型服务，stop() 记进共享事件表。"""

    interval = None
    name = "relay"

    def __init__(self, log):
        self.log = log
        self._ev = threading.Event()

    def run(self):
        self._ev.wait(5)

    def stop(self):
        self.log.append("service.stop")
        self._ev.set()


def _registry(tmp_path):
    return Registry(Config(user_path=tmp_path / "config.yaml"))


def test_load_all_starts_services_then_stop_all_stops_them_first(monkeypatch, tmp_path):
    log: list = []

    class P(Plugin):
        id = "rs"
        name = "RS"

        def __init__(self, ctx):
            super().__init__(ctx)
            self._svc = _SvcService(log)

        def resident_services(self):
            return [self._svc]

        def stop(self):
            log.append("plugin.stop")

    reg = _registry(tmp_path)
    monkeypatch.setattr(reg, "discover", lambda: {"rs": P})
    loaded = reg.load_all()
    assert [p.id for p in loaded] == ["rs"]
    assert reg.resident_host.active(loaded[0]) == ["relay"]

    reg.stop_all()
    assert log == ["service.stop", "plugin.stop"]      # 顺序铁律
    assert reg.resident_host.active(loaded[0]) == []
    assert reg.plugins == {}


def test_plugin_without_services_is_unaffected(monkeypatch, tmp_path):
    class Plain(Plugin):
        id = "plain"
        name = "Plain"

    reg = _registry(tmp_path)
    monkeypatch.setattr(reg, "discover", lambda: {"plain": Plain})
    loaded = reg.load_all()
    assert reg.resident_host.active(loaded[0]) == []
    assert reg.resident_host.thread_names() == []
    reg.stop_all()


def test_ctx_resync_services_converges_declaration(tmp_path):
    class P(Plugin):
        id = "p"
        name = "P"

        def __init__(self, ctx):
            super().__init__(ctx)
            self.svc = _SvcService([])
            self.declared = False

        def resident_services(self):
            return [self.svc] if self.declared else []

    reg = _registry(tmp_path)
    plugin = P(reg.ctx)
    reg.plugins["p"] = plugin

    plugin.declared = True
    reg.ctx.resync_services()                     # 框架注入的请求入口（无参）
    assert reg.resident_host.active(plugin) == ["relay"]

    plugin.declared = False
    reg.ctx.resync_services()
    assert reg.resident_host.active(plugin) == []


def test_problems_merges_service_failures(tmp_path):
    class P(Plugin):
        id = "bad"
        name = "Bad"

        def resident_services(self):
            return [RaisingRun()]

    reg = _registry(tmp_path)
    plugin = P(reg.ctx)
    reg.plugins["bad"] = plugin
    reg.start_services(plugin)
    assert _wait(lambda: reg.resident_host.problems())

    problems = dict(reg.problems())
    assert "bad" in problems and "boom" in problems["bad"]


def test_teardown_services_uses_service_teardown(tmp_path):
    class Svc(_SvcService):
        def __init__(self, log):
            super().__init__(log)
            self.torn = 0

        def teardown(self):
            self.torn += 1
            self._ev.set()

    class P(Plugin):
        id = "t"
        name = "T"

        def __init__(self, ctx):
            super().__init__(ctx)
            self.svc = Svc([])

        def resident_services(self):
            return [self.svc]

    reg = _registry(tmp_path)
    plugin = P(reg.ctx)
    reg.plugins["t"] = plugin
    reg.start_services(plugin)
    reg.teardown_services(plugin)
    assert plugin.svc.torn == 1
    assert reg.resident_host.active(plugin) == []


# ──────────────────────────────────────────────
# 隧道退出拆除门禁（方案 §10：test_tunnel_teardown_on_app_exit）
#
# 隧道是依附型服务：app 退出必须连根拔掉（代理线程退、端口释、helper 收信号），
# 否则变黑洞（流量仍被灌入死隧道）。app 退出路径 = ``AppShell._shutdown`` →
# ``registry.stop_all`` → ``stop_services`` → ``ResidentHost.stop``（join 在飞
# worker）。ipad_tunnel 的 RelayService / LinkProbeService 经同一机制托管，故本
# 门禁用真实 Registry + 真实 ResidentHost + 仿隧道自持服务守卫该退出路径。
# ──────────────────────────────────────────────

def test_app_exit_teardown_removes_attached_tunnel_services(monkeypatch, tmp_path):
    """App 退出（stop_all）必须拆掉依附型隧道服务，不留黑洞。"""
    log: list = []

    class TunnelRelay:
        interval = None
        name = "relay"

        def __init__(self, log):
            self.log = log
            self._ev = threading.Event()
            self.started = threading.Event()

        def run(self):
            self.started.set()
            self._ev.wait(10)

        def stop(self):
            self.log.append("relay.stop")
            self._ev.set()

    class TunnelPlugin(Plugin):
        id = "tunnel"
        name = "Tunnel"

        def __init__(self, ctx):
            super().__init__(ctx)
            self._relay = TunnelRelay(log)

        def resident_services(self):
            return [self._relay]

    reg = _registry(tmp_path)
    monkeypatch.setattr(reg, "discover", lambda: {"tunnel": TunnelPlugin})
    loaded = reg.load_all()
    assert reg.resident_host.active(loaded[0]) == ["relay"]
    assert loaded[0]._relay.started.wait(2)            # 服务真在跑

    reg.stop_all()                                      # 模拟 App 退出路径
    assert log == ["relay.stop"]                        # 顺序铁律：先停服务
    assert reg.resident_host.active(loaded[0]) == []
    assert reg.plugins == {}
    assert _wait(lambda: not any(
        t.name.startswith("resident:tunnel.") for t in threading.enumerate()
    ), timeout=3.0)


# ──────────────────────────────────────────────
# 「现在跑一轮」（2026-09-30 新增）
#
# 由来：hosts 托管的管理窗要一枚「更新」按钮 —— 用户选中条目按下之后，**解析不能等
# 下一拍**（服务节拍 30s，而那是「多久检查一次谁到期」，不是「用户按了键要等多久」）。
# 宿主此前只提供「等 interval」这一种等待，插件侧要立刻跑一轮就只能自起时钟 ——
# 那正是本模块存在的理由所禁止的。故把等待做成**两路可打断**，终止与唤醒各占一路。
# ──────────────────────────────────────────────


def test_context_wake_service_defaults_to_none():
    assert PluginContext(config=None).wake_service is None


def test_plugin_without_wake_wiring_declines_quietly():
    """未接线时叫不动服务 → 返回 0，不抛（与 mark_dirty 同款「框架未接线就退化为空转」）。"""
    assert Plugin(PluginContext(config=None)).wake_service("probe") == 0


def test_wake_runs_a_periodic_poll_before_the_interval_elapses():
    svc = Periodic("probe", interval=30.0)
    host = ResidentHost()
    plugin = FakePlugin(services=[svc])
    host.start(plugin)

    assert svc.count == 0                       # 首轮仍在 interval 之后（没叫就不跑）
    assert host.wake("fake", "probe") == 1
    assert _wait(lambda: svc.count >= 1, timeout=2.0)   # 远早于那一拍
    host.stop(plugin)


def test_wake_matches_only_this_plugins_periodic_services():
    svc = Periodic("probe", interval=30.0)
    host = ResidentHost()
    plugin = FakePlugin(services=[svc, SelfSustaining("relay")])
    host.start(plugin)

    assert host.wake("nobody") == 0                  # 不是本插件
    assert host.wake("fake", "relay") == 0           # 自持型没有「跑一轮」
    assert host.wake("fake", "nope") == 0            # 名字对不上
    assert host.wake("fake") == 1                    # 留空 = 本插件全部周期型
    assert _wait(lambda: svc.count >= 1, timeout=2.0)
    host.stop(plugin)


def test_stop_does_not_wait_out_a_long_interval():
    """终止与唤醒是同一个等待的两条出路 —— 只置 stop 会让退出白等一个 interval。

    ⚠️ 这条是给「唤醒」改动的**反面对照**：``_stop_one`` 若只置 ``stop_event``，
    周期型（这里 30s）会睡满那一拍才看终止，join 直接撞 ``JOIN_TIMEOUT`` ——
    用户看到的是「退出要等 5 秒」＋ 一条假的「未在 5s 内退出」问题。
    """
    svc = Periodic("probe", interval=30.0)
    host = ResidentHost()
    plugin = FakePlugin(services=[svc])
    host.start(plugin)

    start = time.monotonic()
    host.stop(plugin)
    assert time.monotonic() - start < 2.0
    assert host.active(plugin) == []
    assert host.problems() == []
