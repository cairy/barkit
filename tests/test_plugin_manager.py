"""E3 ② PluginManager 单测（纯逻辑，可离屏；UI 重绘留给真机目测）。

用轻量桩替代 registry / contributions / scheduler / hotkeys / events / panel / config，
验证运行期启用 / 禁用 / 卸载对各方影响的编排是否正确，不依赖真实 Qt 窗口。
"""

from __future__ import annotations

import ast
import tempfile
import zipfile
from pathlib import Path

import barkit.core.plugin_manager
from barkit import paths
from barkit.core.config import Config
from barkit.contribution.specs import (
    HotkeySpec,
    IconName,
    StateLevel,
    StateOption,
    StateReport,
    StateSpec,
)
from barkit.core.events import HELPER_OBSERVED_TOPIC, ObservedPush
from barkit.core.power import POWER_WAKE_TOPIC, PowerEvent
from barkit.core.plugin import FAIL_LOG, RUN_ON_DEMAND, RUN_PERIODIC, Plugin, PluginContext
from barkit.core.plugin_manager import PluginManager
from barkit.core.resources import DeferredRemoval, PluginResources, SystemMutation


# ---------- 桩 ----------

class _FakeRegistry:
    def __init__(self, discovered, plugins, source):
        self._discovered = dict(discovered)
        self.plugins = dict(plugins)
        self._source = dict(source)
        self.ctx = object()
        self.forgotten = []
        # 长驻服务托管调用记录（批次 5 后段 S2）：("start"|"stop"|"teardown", pid)
        self.service_calls = []
        # helper task 表（批次 4 S3b-2c）：登记实例 + 重算落盘调用记录
        self.registered = []
        self.sync_tasks_calls = 0
        # 安装路径的重扫接口（决议 D5）。真重扫由 Registry.refresh_discovery 覆盖，
        # 这里让桩能按序应答「每次重扫看到的世界」—— 换包失败再回滚这种事，两次
        # 重扫看到的东西本就不同（第一次是坏掉的新版、第二次是退回的旧版）。
        self.refresh_calls = 0
        self.rescan_queue: list[tuple[dict, dict]] = []

    def refresh_discovery(self):
        self.refresh_calls += 1
        if self.rescan_queue:
            discovered, source = self.rescan_queue.pop(0)
            self._discovered = dict(discovered)
            self._source = dict(source)

    def register_instance(self, plugin):
        self.registered = [p for p in self.registered if p.id != plugin.id]
        self.registered.append(plugin)

    def sync_helper_tasks(self):
        self.sync_tasks_calls += 1

    @property
    def discovered_classes(self):
        return self._discovered

    def source_of(self, pid):
        return self._source.get(pid, "internal")

    def forget(self, pid):
        self._discovered.pop(pid, None)
        self._source.pop(pid, None)
        self.forgotten.append(pid)

    def start_services(self, plugin):
        self.service_calls.append(("start", plugin.id))

    def stop_services(self, plugin):
        self.service_calls.append(("stop", plugin.id))

    def teardown_services(self, plugin):
        self.service_calls.append(("teardown", plugin.id))


class _FakeContributions:
    def __init__(self, plugins=None):
        # 真实 ContributionRegistry 在构造期即持有全部插件（无论是否激活），
        # 故 hotkeys() 始终能枚举到被禁用插件的契约。这里同样在构造期注入全部已加载插件。
        self._plugins: list = list(plugins or [])
        self.activated: list = []
        self.deactivated: list = []

    def activate(self, plugin):
        self.activated.append(plugin.id)
        self._plugins.append(plugin)

    def deactivate(self, pid):
        self.deactivated.append(pid)
        self._plugins = [p for p in self._plugins if getattr(p, "id", None) != pid]

    @property
    def active_ids(self):
        return {getattr(p, "id", None) for p in self._plugins}

    def hotkeys(self):
        """镜像真实 ContributionRegistry.hotkeys()：取活跃插件的热键，译成
        v3 HotkeySpec（含 plugin_id），供 PluginManager._register/_unregister_hotkeys
        按 plugin_id 过滤。"""
        from barkit.contribution.specs import HotkeySpec as V2HotkeySpec

        out: list = []
        for p in self._plugins:
            for hk in (getattr(p, "hotkeys", lambda: [])() or []):
                out.append(
                    V2HotkeySpec(
                        plugin_id=getattr(p, "id", ""),
                        key=getattr(hk, "name", "") or "hk",
                        name=getattr(hk, "name", ""),
                        label=getattr(hk, "label", ""),
                        default_combo=getattr(hk, "default_combo", ""),
                        callback=getattr(hk, "callback", None),
                    )
                )
        return out

    def states(self):
        """镜像真实 ContributionRegistry.states()：取活跃插件声明点方法（决议 D20，
        ``state_specs()``）里的 StateSpec，供 PluginManager._subscribe_states 按 plugin_id 过滤。"""
        out: list = []
        for p in self._plugins:
            for s in (getattr(p, "state_specs", lambda: ())() or ()):
                if isinstance(s, StateSpec):
                    out.append(s)
        return out

    def badges(self):
        """镜像真实 ContributionRegistry.badges()：v3 徽标规则（本桩无插件贡献）。"""
        return []


class _FakeScheduler:
    def __init__(self):
        self.added: list = []
        self.removed: list = []
        self.poll_refreshes: list = []      # 节拍重挂调用（D21 的用户入口）

    def add_plugin(self, plugin):
        self.added.append(plugin.id)

    def remove_plugin(self, pid):
        self.removed.append(pid)

    def set_poll(self, pid):
        """重挂节拍：真实实现返回「这个组件在不在调度器里」，这里镜像同一口径。"""
        self.poll_refreshes.append(pid)
        return pid in self.added


class _FakeHotkeys:
    def __init__(self):
        self.registered: list = []
        self.unregistered: list = []

    def register(self, name, combo, callback):
        self.registered.append(name)
        return True

    def unregister(self, name):
        self.unregistered.append(name)


class _FakeEvents:
    def __init__(self):
        self._subs: dict = {}
        self.unsubs: list = []

    def subscribe(self, topic, handler):
        self._subs.setdefault(topic, []).append(handler)

    def unsubscribe(self, topic, handler):
        self.unsubs.append((topic, handler))
        lst = self._subs.get(topic, [])
        if handler in lst:
            lst.remove(handler)


class _FakePanel:
    def __init__(self):
        self.refreshed = 0

    def set_contributions(self, contributions):
        self.refreshed += 1


class _FakePlugin(Plugin):
    id = "demo"
    name = "Demo"
    run_mode = RUN_ON_DEMAND

    def __init__(self, ctx=None):
        super().__init__(ctx)
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def hotkeys(self):
        return [HotkeySpec(plugin_id=self.id, name="demo_hk", label="d",
                           default_combo="ctrl+alt+d", callback=lambda: None)]


class _FakeDaemonPlugin(_FakePlugin):
    id = "daemon1"
    name = "Daemon1"
    run_mode = RUN_PERIODIC


class _FakePluginWithDisable(_FakePlugin):
    """覆盖 on_disabled 钩子，验证 disable 时后端清理被调用。"""

    id = "withdisable"
    name = "WithDisable"

    def __init__(self, ctx=None):
        super().__init__(ctx)
        self.disabled_called = False

    def on_disabled(self):
        self.disabled_called = True


class _FakeHeartbeat:
    """心跳替身：只记订阅，不真的计时（用例显式调 cleaner.poll() 推进）。"""

    def __init__(self):
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


class _DeferredPlugin(_FakePlugin):
    """声明了「延迟收尾」的插件桩：等不等、等多久由用例给。"""

    id = "deferred"
    name = "Deferred"

    def __init__(self, ctx=None, *, settled=None, timeout_s=5.0):
        super().__init__(ctx)
        self.settled = settled if settled is not None else {"v": False}
        self.timeout_s = timeout_s

    def resources(self):
        res = super().resources()
        res.data_files = ["routes.json", "state.json"]     # 有外部消费方在读的那两个
        res.deferred = DeferredRemoval(
            ready=lambda _wm: self.settled["v"], timeout_s=self.timeout_s,
        )
        return res


def _make_manager(discovered, plugins, source, config, heartbeat=None):
    reg = _FakeRegistry(discovered, plugins, source)
    contrib = _FakeContributions(list(plugins.values()))
    sched = _FakeScheduler()
    hk = _FakeHotkeys()
    ev = _FakeEvents()
    panel = _FakePanel()
    state_updates = []
    mgr = PluginManager(
        reg, contrib, sched, hk, ev, panel, config,
        on_state_update=lambda spec, payload: state_updates.append((spec, payload)),
        heartbeat=heartbeat,
    )
    return mgr, reg, contrib, sched, hk, ev, panel


# ---------- 列表 ----------

def test_list_plugins_distinguishes_source_and_loaded():
    config = _FakeConfig({"demo": False}, {"demo": True})
    mgr, reg, *_ = _make_manager(
        {"demo": _FakePlugin, "ext1": _FakePlugin},
        {"demo": _FakePlugin()},          # demo 已加载
        {"demo": "internal", "ext1": "external"},
        config,
    )
    infos = {i.id: i for i in mgr.list_plugins()}
    assert infos["demo"].source == "internal" and infos["demo"].loaded is True
    assert infos["ext1"].source == "external" and infos["ext1"].loaded is False
    # 被禁用（config 中 enabled=False）应反映 enabled=False
    config.set_plugin_enabled("demo", False)
    infos = {i.id: i for i in mgr.list_plugins()}
    assert infos["demo"].enabled is False


# ---------- 启用 ----------

def test_enable_instantiates_and_wires_everything():
    config = _FakeConfig({}, {"demo": True})
    mgr, reg, contrib, sched, hk, ev, panel = _make_manager(
        {"demo": _FakePlugin}, {}, {"demo": "internal"}, config,
    )
    assert mgr.enable("demo") is True
    assert "demo" in reg.plugins                 # 已实例化并加入 registry
    assert reg.plugins["demo"].started is True  # start 被调
    assert contrib.activated == ["demo"]         # 贡献加回
    assert hk.registered == ["demo_hk"]          # 热键注册
    assert panel.refreshed == 1                  # 面板刷新
    assert config.get_enabled("demo") is True    # 持久化
    # 重复启用幂等（已在内存中）
    assert mgr.enable("demo") is True


def test_enable_daemon_adds_to_scheduler():
    config = _FakeConfig({}, {"daemon1": True})
    mgr, reg, contrib, sched, _, _, _ = _make_manager(
        {"daemon1": _FakeDaemonPlugin}, {}, {"daemon1": "internal"}, config,
    )
    mgr.enable("daemon1")
    assert sched.added == ["daemon1"]


def test_enable_subscribes_states_with_initial_paint():
    state = StateSpec(
        plugin_id="demo", key="notify", icon=IconName.GEAR, title="demo",
        states=(StateOption("running", StateLevel.OK, text="运行中"),),
        initial="running", stale_after=30,
    )
    config = _FakeConfig({}, {"demo": True})

    class _StatePlugin(_FakePlugin):
        def state_specs(self):
            return (state,)

    state_updates: list = []
    reg = _FakeRegistry({"demo": _StatePlugin}, {}, {"demo": "internal"})
    contrib = _FakeContributions()
    sched = _FakeScheduler()
    hk = _FakeHotkeys()
    ev = _FakeEvents()
    panel = _FakePanel()
    mgr = PluginManager(
        reg, contrib, sched, hk, ev, panel, config,
        on_state_update=lambda s, p: state_updates.append((s, p)),
    )
    mgr.enable("demo")
    assert state.topic in ev._subs                     # 状态已订阅
    # 首屏画的是声明的起点那一格（决议 D13：initial 是取值名，不是工厂函数）
    assert state_updates == [(state, StateReport("running"))]


def test_enable_missing_plugin_returns_false():
    config = _FakeConfig({}, {})
    mgr, *_ = _make_manager({}, {}, {}, config)
    assert mgr.enable("nope") is False


# ---------- 禁用 ----------

def test_disable_tears_down_everything():
    config = _FakeConfig({}, {"demo": True})
    plugin = _FakePlugin()
    mgr, reg, contrib, sched, hk, ev, panel = _make_manager(
        {"demo": _FakePlugin}, {"demo": plugin}, {"demo": "internal"}, config,
    )
    assert mgr.disable("demo") is True
    assert "demo" not in reg.plugins               # 实例移除
    assert plugin.stopped is True                   # stop 被调
    assert contrib.deactivated == ["demo"]          # 贡献移除
    assert hk.unregistered == ["demo_hk"]           # 热键移除
    assert panel.refreshed == 1                     # 面板刷新
    assert config.get_enabled("demo") is False      # 持久化


def test_disable_already_disabled_is_safe():
    config = _FakeConfig({}, {"demo": False})
    mgr, reg, contrib, *_ = _make_manager(
        {"demo": _FakePlugin}, {}, {"demo": "internal"}, config,
    )
    # 插件不在 registry.plugins（早已禁用）；应安全返回且不抛
    assert mgr.disable("demo") is True
    assert contrib.deactivated == ["demo"]


def test_disable_calls_plugin_on_disabled_hook():
    config = _FakeConfig({}, {"withdisable": True})
    plugin = _FakePluginWithDisable()
    mgr, reg, contrib, sched, hk, ev, panel = _make_manager(
        {"withdisable": _FakePluginWithDisable}, {"withdisable": plugin},
        {"withdisable": "internal"}, config,
    )
    assert mgr.disable("withdisable") is True
    assert plugin.disabled_called is True       # 后端清理钩子被调用
    assert "withdisable" not in reg.plugins       # 实例仍被移除


# ---------- 变更回调 ----------

def test_on_plugins_changed_fires_on_enable_disable_uninstall():
    config = _FakeConfig({}, {"demo": True})
    calls: list = []
    mgr, reg, contrib, sched, hk, ev, panel = _make_manager(
        {"demo": _FakePlugin, "ext1": _FakePlugin},
        {"demo": _FakePlugin()},
        {"demo": "internal", "ext1": "external"},
        config,
    )
    mgr.on_plugins_changed = lambda: calls.append(1)
    assert mgr.disable("demo") is True          # 1
    assert mgr.enable("demo") is True           # 2
    assert mgr.enable("ext1") is True           # 3（外部插件先启用以便卸载路径覆盖）
    assert len(calls) == 3
    # uninstall 内部先 disable(ext1) 再自身通知，共触发 2 次
    assert mgr.uninstall("ext1") is True
    assert len(calls) == 5


# ---------- 卸载（仅外部） ----------

def test_uninstall_external_removes_dir_config_and_memory(tmp_path):
    ext_dir = tmp_path / "plugins"
    ext_dir.mkdir()
    (ext_dir / "ext1").mkdir()
    (ext_dir / "ext1" / "manifest.yaml").write_text("id: ext1\n")
    config = _FakeConfig({}, {"ext1": True})
    # 让 source_of 返回 external 且 discovered 含 ext1（已加载实例）
    plugin = _FakePlugin(); plugin.id = "ext1"
    mgr, reg, contrib, _, _, _, _ = _make_manager(
        {"ext1": _FakePlugin}, {"ext1": plugin}, {"ext1": "external"}, config,
    )
    # 注入可配置外部根目录（测试用，app 不传则走默认常量）
    mgr._external_base = ext_dir
    assert mgr.uninstall("ext1") is True
    assert not (ext_dir / "ext1").exists()         # 目录已删
    assert "ext1" not in reg.plugins               # 内存实例移除
    assert "ext1" in reg.forgotten                 # 从发现结果遗忘
    assert config.get_removed() == ["ext1"]        # 配置段删除


def test_uninstall_internal_rejected():
    config = _FakeConfig({}, {"demo": True})
    plugin = _FakePlugin()
    mgr, reg, *_ = _make_manager(
        {"demo": _FakePlugin}, {"demo": plugin}, {"demo": "internal"}, config,
    )
    assert mgr.uninstall("demo") is False           # 内置不可删


# ---------- 辅助：FakeConfig（不碰真实用户配置） ----------

class _FakeConfig:
    """内存版配置桩，模拟 Config 的插件启停持久化语义，避免触碰真实文件。"""

    def __init__(self, initial_enabled, initial_loaded):
        # initial_enabled: {id: 默认 enabled}；initial_loaded 仅占位
        self._enabled: dict = dict(initial_enabled)
        self._removed: list = []
        self._plugin_order: list = []
        self._removed_hotkeys: list = []
        self._polls: dict = {}          # 框架节拍的用户覆盖键（D21）

    def get(self, dotted, default=None):
        if dotted == "plugin_order":
            return self._plugin_order
        if dotted.startswith("plugins.") and dotted.endswith(".enabled"):
            pid = dotted.split(".")[1]
            return self._enabled.get(pid, default)
        if dotted.startswith("hotkeys."):
            return default
        return default

    def plugin_poll(self, pid):
        """框架节拍的用户覆盖值（D21）：没写过 → None（默认由组件建议给）。"""
        return self._polls.get(pid)

    def set_plugin_poll(self, pid, seconds):
        """``None`` = 恢复默认（删键），与真实 Config 同语义。"""
        if seconds is None:
            self._polls.pop(pid, None)
        else:
            self._polls[pid] = float(seconds)

    def set_plugin_enabled(self, pid, enabled):
        self._enabled[pid] = enabled

    def get_enabled(self, pid):
        return self._enabled.get(pid)

    def plugin_order(self):
        return list(self._plugin_order)

    def set_plugin_order(self, order):
        self._plugin_order = [str(x) for x in order]

    def remove_from_plugin_order(self, pid):
        self._plugin_order = [x for x in self._plugin_order if x != pid]

    def remove_hotkeys(self, names):
        self._removed_hotkeys.extend(names)

    def remove_plugin_section(self, pid):
        self._enabled.pop(pid, None)
        self._removed.append(pid)

    def get_removed(self):
        return list(self._removed)


# ---------- Config 真实文件持久化（用临时用户配置） ----------

def test_config_set_plugin_enabled_persists_and_merges():
    with tempfile.TemporaryDirectory() as d:
        user = Path(d) / "config.yaml"
        cfg = Config(user_path=user)
        cfg.set_plugin_enabled("recognize", False)
        # 重新读取应反映 enabled=False
        cfg2 = Config(user_path=user)
        assert cfg2.get("plugins.recognize.enabled") is False
        cfg2.set_plugin_enabled("recognize", True)
        cfg3 = Config(user_path=user)
        assert cfg3.get("plugins.recognize.enabled") is True


def test_config_remove_plugin_section():
    with tempfile.TemporaryDirectory() as d:
        user = Path(d) / "config.yaml"
        cfg = Config(user_path=user)
        cfg.set_plugin_enabled("demo_ext", True)
        cfg.remove_plugin_section("demo_ext")
        cfg2 = Config(user_path=user)
        assert cfg2.get("plugins.demo_ext") is None


# ---------- 长驻服务托管接线（批次 5 后段 S2） ----------

class _OrderedPlugin(_FakePlugin):
    """把 plugin.stop() 记进共享事件表，供「服务 stop 早于 plugin.stop」断言。"""

    id = "ord"
    name = "Ordered"

    def __init__(self, ctx=None):
        super().__init__(ctx)
        self.events: list = []

    def stop(self):
        self.events.append("plugin.stop")
        super().stop()


def test_enable_starts_declared_services():
    config = _FakeConfig({}, {"demo": True})
    mgr, reg, *_ = _make_manager(
        {"demo": _FakePlugin}, {}, {"demo": "internal"}, config,
    )
    assert mgr.enable("demo") is True
    assert reg.service_calls == [("start", "demo")]


def test_disable_stops_services_before_plugin_stop():
    config = _FakeConfig({}, {"ord": True})
    plugin = _OrderedPlugin()
    mgr, reg, *_ = _make_manager(
        {"ord": _OrderedPlugin}, {"ord": plugin}, {"ord": "internal"}, config,
    )
    reg.stop_services = lambda p: plugin.events.append("service.stop")

    assert mgr.disable("ord") is True
    assert plugin.events == ["service.stop", "plugin.stop"]   # 顺序铁律


def test_uninstall_tears_down_services_before_disable(tmp_path):
    ext_dir = tmp_path / "plugins"
    (ext_dir / "ext").mkdir(parents=True)
    (ext_dir / "ext" / "manifest.yaml").write_text("id: ext\n")
    config = _FakeConfig({}, {"ext": True})
    plugin = _OrderedPlugin()
    plugin.id = "ext"
    mgr, reg, *_ = _make_manager(
        {"ext": _OrderedPlugin}, {"ext": plugin}, {"ext": "external"}, config,
    )
    mgr._external_base = ext_dir
    reg.teardown_services = lambda p: plugin.events.append("service.teardown")
    reg.stop_services = lambda p: plugin.events.append("service.stop")

    assert mgr.uninstall("ext") is True
    # 服务 teardown 必须先于 disable（disable 内的 stop 只「停」不「拆」）
    assert plugin.events == ["service.teardown", "service.stop", "plugin.stop"]


# ---------- 守护观测订阅（2026-09-29 起取代文件事件订阅） ----------

class _FakeLink:
    """会话桩：记下订阅 / 反订阅声明；``ok=False`` 模拟未接线。"""

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.subscribed: list = []
        self.unsubscribed: list = []

    def subscribe(self, subjects):
        self.subscribed.append(sorted(subjects))
        return self.ok

    def unsubscribe(self, subject):
        self.unsubscribed.append(subject)
        return self.ok


class _FakeSubjectPlugin(_FakePlugin):
    """声明领域订阅并记录推送的插件桩。"""

    id = "watcher"
    name = "Watcher"

    def __init__(self, ctx=None):
        super().__init__(ctx)
        self.observed_pushes: list = []

    def subjects(self):
        return ["route"]

    def on_observed(self, subject, observed):
        self.observed_pushes.append((subject, observed))


_UNSET = object()   # 「未显式指定」与「显式不接线（None）」的区分哨兵


def _make_subject_manager(config=None, link=_UNSET, plugin_cls=_FakeSubjectPlugin,
                          preloaded=True):
    """构造带会话桩的 manager（不动既有 _make_manager 的返回元数）。

    ``preloaded=True`` 模拟「启动期已加载」的插件（供 subscribe_all_active 用例）；
    False 则 registry.plugins 为空，供 enable/disable 用例走完整的实例化路径。
    ``link=None`` 表示**不接线**会话（``ctx.helper`` 为 None 的退化分支）。
    """
    fk = _FakeLink() if link is _UNSET else link
    ctx = PluginContext(config=config or _FakeConfig({}, {"watcher": True}))
    ctx.helper = fk
    plugin = plugin_cls(ctx)
    plugins = {plugin.id: plugin} if preloaded else {}
    reg = _FakeRegistry({plugin.id: plugin_cls}, plugins, {plugin.id: "internal"})
    reg.ctx = ctx                      # enable() 会用 registry.ctx 造新实例
    contrib = _FakeContributions(list(plugins.values()))
    sched = _FakeScheduler()
    hk = _FakeHotkeys()
    ev = _FakeEvents()
    panel = _FakePanel()
    mgr = PluginManager(
        reg, contrib, sched, hk, ev, panel, config or _FakeConfig({}, {plugin.id: True}),
        on_state_update=lambda spec, payload: None,
    )
    return mgr, reg, contrib, ev, plugin, fk


def test_subscribe_all_active_registers_declared_subjects():
    mgr, reg, _, ev, _, fk = _make_subject_manager()
    assert fk.subscribed == []                    # 未调用前一条不声明
    mgr.subscribe_all_active()
    assert fk.subscribed == [["route"]]           # 按声明订阅
    assert ev._subs.get(HELPER_OBSERVED_TOPIC)    # 总线上挂了分发口


def test_observed_push_is_filtered_by_plugin_declaration():
    mgr, _, _, ev, plugin, _ = _make_subject_manager()
    mgr.subscribe_all_active()
    handler = ev._subs[HELPER_OBSERVED_TOPIC][-1]
    handler(ObservedPush(subject="route", observed={"ok": True}))
    assert len(plugin.observed_pushes) == 1
    # 非本插件声明的领域 ⇒ 框架侧丢弃，插件收不到
    handler(ObservedPush(subject="tunnel", observed={"ok": True}))
    assert len(plugin.observed_pushes) == 1


def test_observed_handler_exception_is_isolated():
    class _Boom(_FakeSubjectPlugin):
        def on_observed(self, subject, observed):
            raise RuntimeError("boom")

    mgr, _, _, ev, _, _ = _make_subject_manager(plugin_cls=_Boom)
    mgr.subscribe_all_active()
    handler = ev._subs[HELPER_OBSERVED_TOPIC][-1]
    handler(ObservedPush(subject="route", observed={}))        # 不抛出


def test_enable_then_disable_toggles_subject_subscription():
    mgr, reg, _, ev, plugin, fk = _make_subject_manager(preloaded=False)
    assert mgr.enable("watcher") is True
    assert fk.subscribed == [["route"]]
    assert mgr.disable("watcher") is True
    assert fk.unsubscribed == ["route"]                                # 撤掉会话里的订阅
    assert HELPER_OBSERVED_TOPIC in {topic for topic, _ in ev.unsubs}   # 已反订阅
    assert fk.subscribed == [["route"]]                                # 不重复订阅


def test_missing_link_or_declaration_does_not_raise():
    """未接线会话 / 插件未声明领域：都不抛异常，且都不产生「订阅已送达」这件事。"""
    # ① 未接线：订阅发不出去（``subscribe`` 如实返回 False），但总线分发口照挂 ——
    #    插件的观测订阅是「声明了就在总线上等」，通道没通只是暂时收不到。
    mgr, _, _, ev, _, no_link = _make_subject_manager(link=None)
    assert no_link is None                       # 「不接线」＝ ctx.helper 为 None
    mgr.subscribe_all_active()
    assert ev._subs.get(HELPER_OBSERVED_TOPIC)

    # ② 未声明领域：连总线分发口都不挂（框架按声明过滤，没声明就没什么可过滤的）。
    mgr2, _, _, ev2, _, fk2 = _make_subject_manager(plugin_cls=_FakePlugin)
    mgr2.subscribe_all_active()
    assert fk2.subscribed == [] and HELPER_OBSERVED_TOPIC not in ev2._subs


def test_undelivered_subscription_still_registers_bus_handler():
    """会话没连上（``subscribe`` 返回 False）时总线照常挂着 —— 插件如实反映
    「没通道」，不假装收得到；连上之后守护会补推一份当前的。"""
    mgr, _, _, ev, _, fk = _make_subject_manager(link=_FakeLink(ok=False))
    mgr.subscribe_all_active()
    assert fk.subscribed == [["route"]]
    assert ev._subs.get(HELPER_OBSERVED_TOPIC)


def test_subjects_declaration_exception_is_isolated():
    class _Broken(_FakeSubjectPlugin):
        def subjects(self):
            raise RuntimeError("boom")

    mgr, _, _, ev, _, fk = _make_subject_manager(plugin_cls=_Broken)
    mgr.subscribe_all_active()                                   # 不抛出
    assert fk.subscribed == [] and HELPER_OBSERVED_TOPIC not in ev._subs


# ---------- §8.2.2 事件 handler 契约：只标记脏（批次 10） ----------

def _make_live_session_plugin(qtbot, tmp_path):
    """真件组合：真实 ``EventBus`` + 真实 ``Scheduler`` + 真实 ``PluginContext``。

    只有「与守护的那条会话」是桩（不真连套接字）。返回 ``(events, scheduler, plugin,
    link)``，供下面两条端到端用例共用。
    """
    from barkit.core.events import EventBus
    from barkit.core.heartbeat import Heartbeat
    from barkit.core.scheduler import Scheduler

    events = EventBus()
    scheduler = Scheduler(events, Heartbeat(beat_ms=20), dirty_window_ms=0)
    link = _FakeLink()

    class _Filer(Plugin):
        id = "filer"
        name = "Filer"
        run_mode = RUN_PERIODIC
        failure_policy = FAIL_LOG

        def __init__(self, ctx):
            super().__init__(ctx)
            self.ticks = 0
            self.pushes: list = []

        def subjects(self):
            return ["route"]

        def on_observed(self, subject, observed):
            self.pushes.append((subject, observed))
            self.mark_dirty("helper_observed")      # §8.2.2：只登记意图，不干活

        def tick(self):
            self.ticks += 1

    ctx = PluginContext(config=Config(user_path=tmp_path / "c.yaml"))
    ctx.mark_dirty = scheduler.mark_dirty           # 等价于 AppShell._wire_context
    ctx.helper = link
    plugin = _Filer(ctx)
    scheduler.add_plugin(plugin)
    qtbot.waitUntil(lambda: plugin.ticks >= 1, timeout=2000)     # 启动即跑一轮
    return events, scheduler, plugin, link


def test_observed_push_marks_dirty_then_scheduler_ignites(qtbot, tmp_path):
    """端到端：观测推送 → 框架投递 → 插件只标记脏 → 调度器点火 tick。

    这条把 §8.2.2 的三段接线一次串起来：``ctx.mark_dirty`` 的注入（组合根职责）、
    ``Plugin.mark_dirty`` 的绑定、``Scheduler.mark_dirty`` 的点火。
    """
    events, scheduler, plugin, _link = _make_live_session_plugin(qtbot, tmp_path)

    reg = _FakeRegistry({"filer": type(plugin)}, {"filer": plugin}, {"filer": "internal"})
    reg.ctx = plugin.ctx
    manager = PluginManager(
        reg, _FakeContributions([plugin]), scheduler, _FakeHotkeys(), events, _FakePanel(),
        _FakeConfig({"filer": True}, {}),
        on_state_update=lambda spec, payload: None,
    )
    manager.subscribe_all_active()               # 订阅 system.helper.observed

    n = plugin.ticks
    events.publish(HELPER_OBSERVED_TOPIC,
                   ObservedPush(subject="route", observed={"ok": True}))
    qtbot.waitUntil(lambda: plugin.ticks > n, timeout=3000)      # 标记脏 → 点火
    assert len(plugin.pushes) == 1                               # 推送确已到达插件
    scheduler.stop_all()


def test_non_declared_subject_never_reaches_plugin(qtbot, tmp_path):
    """框架侧过滤仍然生效（文件订阅时代的语义未因换代而退化）。"""
    events, scheduler, plugin, _link = _make_live_session_plugin(qtbot, tmp_path)

    reg = _FakeRegistry({"filer": type(plugin)}, {"filer": plugin}, {"filer": "internal"})
    reg.ctx = plugin.ctx
    manager = PluginManager(
        reg, _FakeContributions([plugin]), scheduler, _FakeHotkeys(), events, _FakePanel(),
        _FakeConfig({"filer": True}, {}),
        on_state_update=lambda spec, payload: None,
    )
    manager.subscribe_all_active()

    n = plugin.ticks
    events.publish(HELPER_OBSERVED_TOPIC,
                   ObservedPush(subject="tunnel", observed={"ok": True}))
    qtbot.wait(200)
    assert plugin.pushes == [] and plugin.ticks == n             # 未投递、未点火
    scheduler.stop_all()


# ---------- 系统事件订阅（§8.3.1 通用订阅） ----------
#
# 与文件订阅同款：插件**只声明 topic**，框架按声明订阅、按声明过滤、单插件异常隔离。


class _FakeTopicPlugin(_FakePlugin):
    """声明系统事件订阅并记录回调的插件桩。"""

    id = "watcher"
    name = "Watcher"

    def __init__(self, ctx=None):
        super().__init__(ctx)
        self.system_events: list = []

    def event_topics(self):
        return [POWER_WAKE_TOPIC]

    def on_system_event(self, topic, payload):
        self.system_events.append((topic, payload))


def _make_topic_manager(config=None, plugin=None, preloaded=True):
    """构造带系统事件订阅的 manager（对照 ``_make_watcher_manager``）。"""
    plugin = plugin if plugin is not None else _FakeTopicPlugin()
    plugins = {plugin.id: plugin} if preloaded else {}
    reg = _FakeRegistry({plugin.id: type(plugin)}, plugins, {plugin.id: "internal"})
    contrib = _FakeContributions(list(plugins.values()))
    ev = _FakeEvents()
    mgr = PluginManager(
        reg, contrib, _FakeScheduler(), _FakeHotkeys(), ev, _FakePanel(),
        config or _FakeConfig({}, {plugin.id: True}),
        on_state_update=lambda spec, payload: None,
    )
    return mgr, ev, plugin


def test_subscribe_all_active_registers_declared_topics():
    mgr, ev, _ = _make_topic_manager()
    assert POWER_WAKE_TOPIC not in ev._subs          # 未调用前一条不订阅
    mgr.subscribe_all_active()
    assert len(ev._subs[POWER_WAKE_TOPIC]) == 1


def test_subscribing_twice_is_idempotent():
    """启动期订阅 + 运行期启用都走同一条路 ⇒ 不许长出第二份订阅（会双投递）。"""
    mgr, ev, _ = _make_topic_manager()
    mgr.subscribe_all_active()
    mgr.subscribe_all_active()
    assert len(ev._subs[POWER_WAKE_TOPIC]) == 1


def test_system_event_reaches_the_plugin():
    mgr, ev, plugin = _make_topic_manager()
    mgr.subscribe_all_active()
    ev._subs[POWER_WAKE_TOPIC][-1](PowerEvent("wake", 1.0))
    assert plugin.system_events == [(POWER_WAKE_TOPIC, PowerEvent("wake", 1.0))]


def test_system_event_handler_exception_is_isolated():
    """一个插件在处理事件时炸了，不得拖垮事件分发（别人的订阅还要到货）。"""

    class _Boom(_FakeTopicPlugin):
        def on_system_event(self, topic, payload):
            raise RuntimeError("boom")

    mgr, ev, _ = _make_topic_manager(plugin=_Boom())
    mgr.subscribe_all_active()
    ev._subs[POWER_WAKE_TOPIC][-1](PowerEvent("wake", 1.0))       # 不抛出


def test_enable_then_disable_toggles_topic_subscription():
    mgr, ev, _ = _make_topic_manager(preloaded=False)
    assert mgr.enable("watcher") is True
    assert len(ev._subs[POWER_WAKE_TOPIC]) == 1
    assert mgr.disable("watcher") is True
    assert ev._subs.get(POWER_WAKE_TOPIC) == []                   # 已反订阅
    assert POWER_WAKE_TOPIC in {topic for topic, _ in ev.unsubs}


def test_no_declaration_means_no_subscription():
    """没声明就不订阅 —— 框架不替插件猜它关心什么。"""
    mgr, ev, _ = _make_topic_manager(plugin=_FakePlugin())
    mgr.subscribe_all_active()
    assert ev._subs == {}


def test_blank_topic_names_are_ignored():
    class _Blank(_FakeTopicPlugin):
        def event_topics(self):
            return ["", None, POWER_WAKE_TOPIC]

    mgr, ev, _ = _make_topic_manager(plugin=_Blank())
    mgr.subscribe_all_active()
    assert set(ev._subs) == {POWER_WAKE_TOPIC}


def test_topic_declaration_exception_is_isolated():
    class _Broken(_FakeTopicPlugin):
        def event_topics(self):
            raise RuntimeError("boom")

    mgr, ev, _ = _make_topic_manager(plugin=_Broken())
    mgr.subscribe_all_active()                                   # 不抛出
    assert ev._subs == {}


def test_wake_event_marks_dirty_then_scheduler_ignites(qtbot, tmp_path):
    """端到端（全真件）：唤醒事件 → 框架投递 → 插件只标记脏 → 调度器点火 tick。

    真件 = 真实 ``EventBus`` + 真实 ``Scheduler`` + 真实 ``PluginContext``；只有
    「事件源」是替身（直接往总线上发一条，不注册 ``NSWorkspace`` 观察者）。
    与文件事件那条端到端用例同款，验的是 §8.3.1 的三段接线。
    """
    from barkit.core.events import EventBus
    from barkit.core.heartbeat import Heartbeat
    from barkit.core.scheduler import Scheduler

    events = EventBus()
    scheduler = Scheduler(events, Heartbeat(beat_ms=20), dirty_window_ms=0)

    class _Waker(Plugin):
        id = "waker"
        name = "Waker"
        run_mode = RUN_PERIODIC
        failure_policy = FAIL_LOG
        suggested_poll = 300

        def __init__(self, ctx):
            super().__init__(ctx)
            self.ticks = 0
            self.wakes = 0

        def event_topics(self):
            return [POWER_WAKE_TOPIC]

        def on_system_event(self, topic, payload):
            self.wakes += 1
            self.mark_dirty("system_wake")        # §8.2.2：只登记意图，不干活

        def tick(self):
            self.ticks += 1

    ctx = PluginContext(config=Config(user_path=tmp_path / "c.yaml"))
    ctx.mark_dirty = scheduler.mark_dirty         # 等价于 AppShell._wire_context
    plugin = _Waker(ctx)
    scheduler.add_plugin(plugin)
    qtbot.waitUntil(lambda: plugin.ticks >= 1, timeout=2000)     # 启动即跑一轮

    reg = _FakeRegistry({"waker": _Waker}, {"waker": plugin}, {"waker": "internal"})
    manager = PluginManager(
        reg, _FakeContributions([plugin]), scheduler, _FakeHotkeys(), events,
        _FakePanel(), _FakeConfig({"waker": True}, {}),
        on_state_update=lambda spec, payload: None,
    )
    manager.subscribe_all_active()

    n = plugin.ticks
    events.publish(POWER_WAKE_TOPIC, PowerEvent("wake", 1.0))
    qtbot.waitUntil(lambda: plugin.ticks > n, timeout=3000)      # 标记脏 → 点火
    assert plugin.wakes == 1
    scheduler.stop_all()


# ---------- 安装 / 覆盖外部插件（决议 D5） ----------
#
# 编排面在这里验（停旧实例 → 换包 → 重扫 → 按原启用态起新实例 → 回滚）；
# 「真去扫目录会发现什么」归 test_loader / test_registry，解包安全线归 test_installer。


class _ExtPlugin(Plugin):
    """最小外部插件类（安装编排用）。``ctx`` 可省：编排层只按接口用它。"""

    id = "demo"
    name = "Demo Ext"

    def __init__(self, ctx=None):
        super().__init__(ctx)
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def _write_ext_package(path: Path, pid="demo", version="0.2.0") -> Path:
    """写一份最小可安装的外部插件包（目录形态）；``version=None`` = 老清单不写版本。"""
    path.mkdir(parents=True, exist_ok=True)
    ver = f'version: "{version}"\n' if version is not None else ""
    (path / "manifest.yaml").write_text(
        f"id: {pid}\n{ver}api_version: 3\nentry: plugin:Plugin\n", encoding="utf-8"
    )
    (path / "plugin.py").write_text(
        "from barkit.core.plugin import Plugin\n"
        "class Plugin(Plugin):\n"
        f"    id = {pid!r}\n"
        "    def start(self):\n"
        "        self.started = True\n"
        "    def stop(self):\n"
        "        self.stopped = True\n",
        encoding="utf-8",
    )
    return path


def _install_mgr(tmp_path, discovered=None, plugins=None, sources=None):
    """装好外部根目录的管理器（外部根可写、跟真实用户目录无关）。"""
    base = tmp_path / "ext"
    base.mkdir(parents=True, exist_ok=True)
    cfg = _FakeConfig({}, {})
    mgr, reg, contrib, sched, hk, ev, panel = _make_manager(
        discovered or {}, plugins or {}, sources or {}, cfg
    )
    mgr._external_base = base
    return mgr, reg, contrib, cfg, base


def test_preview_install_first_time_reports_no_installed_version(tmp_path):
    mgr, _, _, _, base = _install_mgr(tmp_path)
    src = _write_ext_package(tmp_path / "pkg", version="0.2.0")

    prev, reason = mgr.preview_install(src)

    assert reason == "" and prev is not None
    assert (prev.plugin_id, prev.version) == ("demo", "0.2.0")
    assert prev.installed_version is None          # 首次安装
    assert not (base / "demo").exists()            # 预检**不写盘**


def test_preview_install_reports_upgrade_and_unknown_installed_version(tmp_path):
    """已装但清单没写版本 → ``""``，**不能**跟「首次安装」折成同一个值。"""
    mgr, _, _, _, base = _install_mgr(tmp_path)
    _write_ext_package(base / "demo", version=None)
    src = _write_ext_package(tmp_path / "pkg", version="0.3.0")

    prev, _ = mgr.preview_install(src)

    assert prev.installed_version == ""
    assert prev.version == "0.3.0"


def test_install_first_time_installs_and_starts(tmp_path):
    mgr, reg, contrib, cfg, base = _install_mgr(tmp_path)
    reg.rescan_queue = [({"demo": _ExtPlugin}, {"demo": "external"})]
    src = _write_ext_package(tmp_path / "pkg", version="0.2.0")

    ok, msg = mgr.install(src)

    assert ok and "0.2.0" in msg
    assert (base / "demo" / "manifest.yaml").is_file()   # 包已落盘
    assert "demo" in reg.plugins                         # 已起实例
    assert contrib.activated == ["demo"]                 # 已进贡献注册中心
    assert cfg.get_enabled("demo") is True               # 启用态落配置
    assert reg.refresh_calls == 1                        # 只在激活那步重扫一次


def test_install_upgrade_stops_old_instance_then_starts_new(tmp_path):
    old = _ExtPlugin()
    mgr, reg, contrib, cfg, base = _install_mgr(
        tmp_path, {"demo": _ExtPlugin}, {"demo": old}, {"demo": "external"}
    )
    _write_ext_package(base / "demo", version="0.1.0")
    reg.rescan_queue = [({"demo": _ExtPlugin}, {"demo": "external"})]
    src = _write_ext_package(tmp_path / "pkg", version="0.2.0")

    ok, msg = mgr.install(src)

    assert ok, msg
    assert old.stopped is True                        # 旧实例先停（模块文件正被替换）
    assert contrib.deactivated == ["demo"]
    assert reg.plugins["demo"] is not old             # 跑的是新实例
    assert "0.2.0" in (base / "demo" / "manifest.yaml").read_text(encoding="utf-8")


def test_install_refuses_to_shadow_internal_plugin(tmp_path):
    """外部包不得顶替内置插件：发现层是「外部覆盖内部」，入口必须拦下来。"""
    mgr, _, _, _, base = _install_mgr(
        tmp_path, {"demo": _ExtPlugin}, {}, {"demo": "internal"}
    )
    src = _write_ext_package(tmp_path / "pkg")

    ok, msg = mgr.install(src)

    assert not ok and "内置插件" in msg
    assert not (base / "demo").exists()               # 一个字都没落盘


def test_install_rejects_package_without_manifest(tmp_path):
    mgr, _, _, _, _ = _install_mgr(tmp_path)
    bad = tmp_path / "pkg"
    bad.mkdir()
    (bad / "readme.txt").write_text("hello", encoding="utf-8")

    ok, msg = mgr.install(bad)

    assert not ok and "manifest" in msg


def test_install_accepts_zip_package(tmp_path):
    """压缩包与目录走同一条路（决议 D5：两种都收）。"""
    mgr, reg, _, _, base = _install_mgr(tmp_path)
    reg.rescan_queue = [({"demo": _ExtPlugin}, {"demo": "external"})]
    pkg = _write_ext_package(tmp_path / "pkg", version="0.4.0")
    zp = tmp_path / "demo.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        for f in sorted(pkg.rglob("*")):
            if f.is_file():
                zf.write(f, f"demo/{f.relative_to(pkg)}")   # 套一层目录（Finder 形态）

    ok, msg = mgr.install(zp)

    assert ok and "0.4.0" in msg
    assert (base / "demo" / "manifest.yaml").is_file()


def test_install_activation_failure_rolls_back_and_restarts_old(tmp_path):
    """激活失败：底层退回旧版目录，编排层再把旧版重新起起来（＝旧版仍可用）。"""
    old = _ExtPlugin()
    mgr, reg, _, _, base = _install_mgr(
        tmp_path, {"demo": _ExtPlugin}, {"demo": old}, {"demo": "external"}
    )
    _write_ext_package(base / "demo", version="0.1.0")
    # 两次重扫看到的东西不同：第一次是「新类没被发现」（包写坏 / 被闸门拒），
    # 第二次是回滚后的旧版。
    reg.rescan_queue = [({}, {}), ({"demo": _ExtPlugin}, {"demo": "external"})]
    src = _write_ext_package(tmp_path / "pkg", version="0.2.0")

    ok, msg = mgr.install(src)

    assert not ok and "激活失败" in msg
    assert "0.1.0" in (base / "demo" / "manifest.yaml").read_text(encoding="utf-8")
    assert "demo" in reg.plugins                      # 旧版已重新起来


# ---------- 卸载：延迟收尾（等外部消费方收敛，§9.1 第 4 步） ----------

def _deferred_mgr(plugin, heartbeat=None):
    """建一个「外部 + 声明了 deferred」的卸载现场，并把它的数据文件摆好。

    数据文件写在 ``paths.APP_SUPPORT`` 里（autouse 夹具已把它重定向到临时目录），
    这样断言的是真实删除逻辑，而不是替身。
    """
    config = _FakeConfig({}, {plugin.id: True})
    mgr, reg, *_ = _make_manager(
        {plugin.id: _DeferredPlugin}, {plugin.id: plugin}, {plugin.id: "external"},
        config, heartbeat=heartbeat,
    )
    data = paths.APP_SUPPORT / "routes.json"
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_text("{}", encoding="utf-8")
    (paths.APP_SUPPORT / "state.json").write_text("{}", encoding="utf-8")
    return mgr, reg, data


def test_uninstall_defers_tail_until_consumer_settles():
    """有 deferred ⇒ 收尾整段推迟到判据为真；同步那段（disable / teardown）照常跑。"""
    plugin = _DeferredPlugin()
    mgr, reg, data = _deferred_mgr(plugin, heartbeat=_FakeHeartbeat())

    assert mgr.uninstall("deferred") is True
    assert plugin.stopped is True                      # 停用是同步的
    assert data.is_file()                              # 但收尾还没做
    assert "deferred" not in reg.forgotten             # 插件仍在册（helper task 还没摘）
    assert mgr._deferred_cleanup.pending() == 1

    plugin.settled["v"] = True                         # 消费方收敛
    mgr._deferred_cleanup.poll()

    assert not data.exists()
    assert "deferred" in reg.forgotten
    assert mgr._deferred_cleanup.pending() == 0


def test_uninstall_deferred_commits_at_once_when_already_settled():
    """判据一开始就为真 ⇒ 当场收尾，不白等一个心跳周期。"""
    plugin = _DeferredPlugin(settled={"v": True})
    mgr, reg, data = _deferred_mgr(plugin, heartbeat=_FakeHeartbeat())

    assert mgr.uninstall("deferred") is True
    assert not data.exists() and "deferred" in reg.forgotten
    assert mgr._deferred_cleanup.pending() == 0


def test_uninstall_timeout_keeps_files_and_plugin():
    """超时 ⇒ **什么都不撤**（文件留、插件留），并把原因交给界面 —— 不假装成功。"""
    plugin = _DeferredPlugin(timeout_s=0.0)
    settled: list = []
    mgr, reg, data = _deferred_mgr(plugin, heartbeat=_FakeHeartbeat())
    mgr.on_uninstall_settled = lambda pid, problem: settled.append((pid, problem))

    assert mgr.uninstall("deferred") is True
    assert data.is_file()                              # 文件留着：消费方下次还能收敛
    assert "deferred" not in reg.forgotten             # 也没被遗忘
    assert mgr._deferred_cleanup.pending() == 0        # 只是放弃了本轮等待
    assert [(pid, bool(problem)) for pid, problem in settled] == [("deferred", True)]
    assert "deferred" not in reg.plugins               # 但已停用（同步那段做完了）


def test_uninstall_settled_callback_reports_success():
    """收尾成功也回执一句（界面据此说「已卸载」，而不是静默）。"""
    plugin = _DeferredPlugin(settled={"v": True})
    settled: list = []
    mgr, reg, data = _deferred_mgr(plugin, heartbeat=_FakeHeartbeat())
    mgr.on_uninstall_settled = lambda pid, problem: settled.append((pid, problem))

    mgr.uninstall("deferred")

    assert settled == [("deferred", "")]


def test_uninstall_without_heartbeat_falls_back_to_immediate():
    """未接线（无心跳的轻量构造路径）⇒ 退回当场收尾，不把卸载悬在半空。"""
    plugin = _DeferredPlugin(settled={"v": False})
    mgr, reg, data = _deferred_mgr(plugin, heartbeat=None)

    assert mgr._deferred_cleanup.hooked is False
    assert mgr.uninstall("deferred") is True
    assert not data.exists() and "deferred" in reg.forgotten   # 判据为假也当场收尾


# ---------- 框架节拍的用户入口（决议 D21：默认归组件，覆盖归用户） ----------


class _FakePollingPlugin(_FakePlugin):
    id = "poller"
    name = "Poller"
    run_mode = RUN_PERIODIC
    suggested_poll = 7


def test_list_plugins_fills_poll_default_only_for_periodic():
    """``poll_default`` 是设置窗画不画「刷新间隔」的**唯一准入判据**（D21）。

    周期型给数（组件建议，缺声明则由框架兜底），按需型给 ``None`` —— 它不进调度器，
    根本没有节拍可言。判据走 ``scheduler.poll_default_of``，与调度器同源：界面显示的
    默认值不会与「实际用的节拍」分叉。
    """
    config = _FakeConfig({"poller": True, "demo": True}, {})
    mgr, *_ = _make_manager(
        {"poller": _FakePollingPlugin, "demo": _FakePlugin},
        {},
        {"poller": "internal", "demo": "internal"},
        config,
    )
    infos = {i.id: i for i in mgr.list_plugins()}
    assert infos["poller"].poll_default == 7.0, "组件声明的建议值"
    assert infos["demo"].poll_default is None, "按需型没有节拍"


def test_set_plugin_poll_writes_config_and_refreshes_the_scheduler():
    """改节拍必须**两件事一起做**：落盘 ＋ 让调度器重挂。

    只落盘不重挂，用户改完界面上纹丝不动、要重启才生效 —— 一次静默空转；
    只重挂不落盘，重启又回到旧值。故两条断言缺一不可。
    """
    config = _FakeConfig({"poller": True}, {})
    mgr, _reg, _contrib, sched, *_ = _make_manager(
        {"poller": _FakePollingPlugin}, {"poller": _FakePollingPlugin()},
        {"poller": "internal"}, config,
    )
    assert mgr.plugin_poll("poller") is None          # 没改过

    assert mgr.set_plugin_poll("poller", 60) is True
    assert config.plugin_poll("poller") == 60.0
    assert sched.poll_refreshes == ["poller"]

    mgr.set_plugin_poll("poller", None)               # 恢复默认 = 删键
    assert mgr.plugin_poll("poller") is None
    assert sched.poll_refreshes == ["poller", "poller"]


# ---------- 配置初始化：组件配置恢复默认（2026-09-18） ----------


class _RecordingSignal:
    """只记载荷的信号替身（真实 ``QObject`` 信号要 QApplication，本文件不起 Qt）。"""

    def __init__(self):
        self.payloads: list = []

    def emit(self, *payload):
        self.payloads.append(payload)


class _ResettableConfig(_FakeConfig):
    """带 ``changed`` 与「可清键」的配置桩：语义与真实 ``Config`` 对齐（保留 enabled）。"""

    def __init__(self, sections=None):
        super().__init__({}, {})
        self.changed = _RecordingSignal()
        self.data: dict = {}
        self._sections = dict(sections or {})

    def clearable_plugin_keys(self, pid):
        return [k for k in (self._sections.get(pid) or {}) if k != "enabled"]

    def reset_plugin_section(self, pid):
        section = self._sections.get(pid) or {}
        removed = [k for k in section if k != "enabled"]
        if removed:
            self._sections[pid] = {k: v for k, v in section.items() if k == "enabled"}
        return removed


def test_reset_plugin_config_broadcasts_changed_so_components_reload():
    """清完**必须广播一次** —— 否则就是「界面清干净了、组件还按老配置跑」。

    程序内写配置默认不发 ``changed``（那些写路径各有自己的界面回调），而组件唯一的
    知情渠道就是它；``save_user`` 顺手记了 mtime，文件监视那条路**不会**补上这一声。
    """
    config = _ResettableConfig({"demo": {"enabled": False, "profiles": [], "poll": 60}})
    mgr, *_ = _make_manager({"demo": _FakePlugin}, {}, {"demo": "internal"}, config)

    removed = mgr.reset_plugin_config("demo")

    assert removed == ["profiles", "poll"]
    assert len(config.changed.payloads) == 1
    assert mgr.clearable_config_keys("demo") == [], "清完就没有可清的了（行内按钮随之消失）"


def test_reset_plugin_config_stays_silent_when_nothing_was_cleared():
    """没清掉任何东西就不广播：白叫一声会让所有组件重读一遍配置（route_guard 还会顺手
    重写一次期望态），而盘上一个字节没变。"""
    config = _ResettableConfig({"demo": {"enabled": True}})
    mgr, *_ = _make_manager({"demo": _FakePlugin}, {}, {"demo": "internal"}, config)

    assert mgr.reset_plugin_config("demo") == []
    assert config.changed.payloads == []


def test_plugin_manager_package_layout():
    """``core/plugin_manager.py``（1028 行）2026-10-02 按职责拆成**包** + 混合类。

    四条判据守**四件不同的事**（2026-10-02 门禁自证逐条敲过：拿真出错的输入试过，违规必抓）：

    · 「这一族点名」—— 按**子模块集合**点名，不按文件名。写「``plugin_manager.py`` 不许
      存在」这种是恒真断言（正文搬走后入口本来就没有它，连「这个文件名」这条线索都不留），
      删掉某个**子模块**（比如把 notify 并回 owner）就一声不响；
    · 「混合类还挂在基类位上」—— 把某个族**合回 owner** 不会红：方法还在、成员面
      照样全对，只有这一条能拦住「拆了个寂寞」。判据**从各子模块现算**而不是写死个数
      （2026-10-02 加 ``revert`` 时改的：写死 9 的话，每加一族就要改一次数字，而改数字
      这一手同时能把「某族被摘掉」也放过去）⇒ 「新建了一族却没挂上」与「挂了却没有那一族」
      两头都红；
    · 「不许自己建 logger」—— ``logging_setup`` 的 ``%(name)s`` 会写进日志文件，某个子
      模块图省手 ``logging.getLogger(__name__)`` 一下，日志外观就变了（**行为变更**，
      而正文本身看不出来、成员面也看不出来）；只许 ``from .consts import log``。
      ⚠️ 唯一例外是 ``consts.py`` 自己 —— 它就是**唯一**建 logger 的那一份，而且得写死
      父包名（写 ``__name__`` 会把日志外观改掉），所以这条判据扫的是其余那些子模块；
    · 「四枚外部名由包再导出」—— ``shell/settings/*`` 与若干测试是真从
      ``barkit.core.plugin_manager`` 这**一个导入路径**取 ``PluginManager`` /
      ``PluginInfo`` / ``version_label`` / ``InstallPreview`` 的，少再导出一枚就是引用面
      改动（比包内乱掉更贵：别的模块当场 ImportError）。
    """
    pkg_dir = Path(barkit.core.plugin_manager.__file__).parent
    assert pkg_dir.name == "plugin_manager" and (pkg_dir / "__init__.py").is_file()
    assert not pkg_dir.with_suffix(".py").is_file(), "旧的单文件 plugin_manager.py 又回来了"

    family = {"consts", "inventory", "lifecycle", "subscribe", "install", "uninstall",
              "order", "archive", "hotkeys", "notify", "revert", "plugin_manager"}
    have = {p.stem for p in pkg_dir.glob("*.py")}
    assert family <= have, f"plugin_manager 子模块缺了：{sorted(family - have)}"

    owner = (pkg_dir / "plugin_manager.py").read_text(encoding="utf-8")
    tree = ast.parse(owner)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PluginManager")
    bases = [b.id for b in cls.bases]

    # consts 是唯一**允许**建 logger 的那一份（它就是 logger 的产地），扫其余的。
    # 同一趟顺手把「这一族里有哪些混合类」算出来 —— 判定对象由盘上真有什么决定，
    # 不靠写死的个数（个数的毛病：改它既放行新增、也放行被摘掉）。
    declared: set[str] = set()
    for name in sorted(family - {"plugin_manager", "consts"}):
        text = (pkg_dir / f"{name}.py").read_text(encoding="utf-8")
        assert "logging.getLogger" not in text, (
            f"{name}.py 自己建了 logger：日志名会变成子模块名（`%(name)s` 会写进日志文件），"
            "只许 `from .consts import log`"
        )
        for node in ast.parse(text).body:
            if isinstance(node, ast.ClassDef) and node.name.startswith("_"):
                declared.add(node.name)
    assert set(bases) == declared and len(bases) == len(declared), (
        f"PluginManager 的基类位与各子模块里声明的混合类对不上"
        f"（基类 {sorted(bases)}／子模块 {sorted(declared)}）："
        "编排链路（enable / disable / uninstall / revert / install）得真分在各族里"
    )
    sib_imported = sorted(
        n.names[0].name for n in tree.body
        if isinstance(n, ast.ImportFrom) and n.level and n.module in family
    )
    assert sib_imported == sorted(bases), (
        f"owner 没把混合类全 import 进来（import 了 {sib_imported}／基类 {sorted(bases)}）："
        "少一条就是定义类那一刻拿不到基类 ⇒ 循环导入"
    )

    exported: set[str] = set()
    for node in ast.parse((pkg_dir / "__init__.py").read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "__all__" for t in node.targets
        ):
            exported = {e.value for e in node.value.elts}
    missing = {"PluginManager", "PluginInfo", "version_label", "InstallPreview"} - exported
    assert not missing, f"__init__.py 没再导出 {sorted(missing)}：那是外部真 import 走的名字"


# ---------- 复原系统状态（§5 A2，2026-10-02） ----------

class _RevertPlugin(Plugin):
    """声明了系统变更、并把「谁先谁后」记下来的组件。

    ``stop`` 是 ``disable`` 那一侧唯一的落点（``lifecycle.disable`` 调它），
    ``teardown`` 是复原那一侧 —— 两者在 ``order`` 里的先后就是本批要守的东西。
    """

    id = "reverter"
    name = "复原用例"

    def __init__(self, ctx=None, mutations=(), boom=False):
        super().__init__(ctx)
        self._mutations = list(mutations)
        self._boom = boom
        self.order: list = []

    def resources(self):
        return PluginResources(system_mutations=self._mutations)

    def stop(self):
        self.order.append("disable")

    def teardown(self):
        self.order.append("teardown")
        if self._boom:
            raise RuntimeError("boom")


def _make_revert_manager(mutations=(), boom=False, preloaded=True):
    plugin = _RevertPlugin(mutations=mutations, boom=boom)
    plugins = {plugin.id: plugin} if preloaded else {}
    reg = _FakeRegistry({plugin.id: _RevertPlugin}, plugins, {plugin.id: "internal"})
    reg.ctx = PluginContext(config=_FakeConfig({}, {plugin.id: True}))
    mgr = PluginManager(
        reg, _FakeContributions(list(plugins.values())), _FakeScheduler(),
        _FakeHotkeys(), _FakeEvents(), _FakePanel(),
        _FakeConfig({}, {plugin.id: True}),
        on_state_update=lambda spec, payload: None,
    )
    return mgr, reg, plugin


def _mutation(text: str = "/etc/hosts 中的 BarKit 托管块"):
    return SystemMutation(text)


def test_declared_system_mutations_lists_only_those_that_declared():
    """只列**声明过**且**已加载**的组件 —— 空壳不出那张卡。

    没在跑的组件此刻也没有可供复原的运行期状态，列出来就是给用户一个必然失败的选择。
    """
    mgr, _reg, _p = _make_revert_manager()
    assert mgr.declared_system_mutations() == []          # 声明为空 ⇒ 不列

    mgr, _reg, _p = _make_revert_manager(mutations=[_mutation()])
    found = mgr.declared_system_mutations()
    assert [pid for pid, _m in found] == ["reverter"]
    assert found[0][1][0].description == "/etc/hosts 中的 BarKit 托管块"

    mgr, _reg, _p = _make_revert_manager(mutations=[_mutation()], preloaded=False)
    assert mgr.declared_system_mutations() == []          # 没加载 ⇒ 不列


def test_revert_stops_the_plugin_before_tearing_it_down():
    """顺序是承重的那条：**先停手，再复原**。

    反过来的话组件下一拍就会把它刚撤掉的再写回去 —— 用户看见的是「撤了、又回来了」，
    而日志里两个动作都显示成功。这与卸载七步里 2b→3 的次序同源。
    """
    mgr, _reg, plugin = _make_revert_manager(mutations=[_mutation()])
    assert mgr.revert_system_state("reverter") is True
    assert plugin.order == ["disable", "teardown"]


def test_revert_refuses_when_the_plugin_is_not_loaded():
    """没在跑 ⇒ 无从复原，返回 False，**不**假装撤过了。"""
    mgr, _reg, _p = _make_revert_manager(mutations=[_mutation()], preloaded=False)
    assert mgr.revert_system_state("reverter") is False


def test_revert_reports_failure_when_teardown_raises():
    """``teardown`` 抛异常 = 撤到一半 ⇒ 说失败，不说「已恢复」。"""
    mgr, _reg, _p = _make_revert_manager(mutations=[_mutation()], boom=True)
    assert mgr.revert_system_state("reverter") is False
