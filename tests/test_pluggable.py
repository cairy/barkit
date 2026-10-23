"""批次 6a 可插拔收口测试：排序三级规则 + 可插拔契约 + 卸载七步。

覆盖（方案 §6.4 / §9.1）：
- config.plugin_order 持久化与校验；
- ContributionRegistry 三处渲染同源排序（用户排序优先，未列入按 Plugin.order 追加）；
- Plugin 基类 resources()/teardown()/export_data()/import_data() 默认契约；
- PluginManager.uninstall 七步编排（teardown、孤儿排序项、hotkeys 清理、
  data_files − seed_files、system_mutations 确认）。
"""

from __future__ import annotations

import importlib
import tempfile
from pathlib import Path

import pytest

from barkit.core.config import Config, ConfigError
from barkit.core.plugin import (
    FAIL_BREAK,
    FAIL_LOG,
    RUN_ON_DEMAND,
    RUN_PERIODIC,
    Plugin,
    PluginContext,
)
from barkit.core.plugin_manager import PluginManager
from barkit.core.registry import Registry
from barkit.core.resources import PluginResources, SystemMutation
from barkit.contribution.specs import HotkeySpec, StatusRowSpec
from barkit import paths


# ---------- 契约数据类 ----------

def test_plugin_resources_defaults():
    res = PluginResources()
    assert res.data_files == [] and res.seed_files == []
    assert res.system_mutations == [] and res.root_binaries == ()
    assert res.needs_root is False


def test_system_mutation_fields():
    m = SystemMutation(description="/etc/hosts 中的 BarKit 标记块")
    assert m.survives_app_exit is True       # 自持型默认 True（§2.5）


def test_plugin_base_pluggable_protocol_defaults():
    class _Cfg:
        def get(self, key, default=None):
            return default

    class _P(Plugin):
        id = "p"

    p = _P(PluginContext(config=_Cfg()))
    assert isinstance(p.resources(), PluginResources)
    p.teardown()                              # 不抛
    assert p.export_data() == {}              # 默认 = 配置段（空）
    p.import_data({"anything": 1})            # 不抛（config 无 setter 时静默跳过）


# ---------- 执行方式 / 失败策略的声明面（契约 D10 / D11）----------

#: 六个内建插件的包名（``_template`` 以 ``_`` 开头、发现器不加载，不在扫描面内）。
_BUILTIN_PLUGIN_PACKAGES = (
    "autotype", "hosts_guard", "ipad_tunnel", "notify_guard", "recognize", "route_guard",
)


def test_plugin_declares_run_mode_and_failure_policy_not_kind_and_role():
    """插件类上只有 `run_mode` / `failure_policy` 两个声明位，旧名 `kind` / `role` 不得回归。

    D10 / D11 的理由是**语义即消费点**：框架读这两个属性只问两件事——「要不要周期跑」
    与「失败要不要熔断」，于是名字就直接写成那两句话。旧名 `kind` / `role` 描述的是实现
    形态（`action` / `daemon`）与插件在做什么（写系统 / 只读），那是**原因**，读者要自己
    反推到框架行为。旧名一旦以别名字段回来，「框架读哪一个」就变成隐式约定，故按字段集守着。
    """
    attrs = set(vars(Plugin))
    missing = {"run_mode", "failure_policy"} - attrs
    assert not missing, f"Plugin 缺少声明位：{missing}"
    assert not ({"kind", "role"} & attrs), "旧名 kind / role 不得作为别名回归（契约 D10 / D11）"
    assert Plugin.run_mode == RUN_ON_DEMAND, "默认不轮询：不声明就不进调度器"
    assert Plugin.failure_policy == FAIL_LOG, "默认只记日志：熔断是要显式认领的"
    # 四个取值两两不同——防「复制粘贴改错一个」让两组语义撞车（如 FAIL_LOG = "periodic"）
    assert len({RUN_PERIODIC, RUN_ON_DEMAND, FAIL_BREAK, FAIL_LOG}) == 4


def _declares_own_tick(pkg: str, cls: type) -> bool:
    """这一族里有没有人写 ``tick`` —— **不能只看本类** ``vars()``：拆包后方法住
    在同目录的混合类里（notify_guard 的 ``tick`` 自 2026-10-03 起在
    ``plugin_probe._ProbeMixin``），只看本类那条判据就恒真 ⇒ 一致性门禁跟着假红。
    这里收的是「插件目录内那几个模块定义的类」这一族，框架基类照旧不看。"""
    prefix = f"barkit.plugins.{pkg}."
    return any("tick" in vars(k) for k in cls.__mro__ if k.__module__.startswith(prefix))


def _assert_mode_tick_consistent(pkg: str, cls: type) -> None:
    """两个声明取自闭集 ＋ ``run_mode`` 与「这一族写没写 tick()」一致。"""
    legal_run = {RUN_ON_DEMAND, RUN_PERIODIC}
    legal_fail = {FAIL_LOG, FAIL_BREAK}
    assert cls.run_mode in legal_run, f"{pkg}.run_mode={cls.run_mode!r} 不在取值闭集内"
    assert cls.failure_policy in legal_fail, (
        f"{pkg}.failure_policy={cls.failure_policy!r} 不在取值闭集内"
    )
    declares_tick = _declares_own_tick(pkg, cls)
    assert declares_tick == (cls.run_mode == RUN_PERIODIC), (
        f"{pkg}: run_mode={cls.run_mode!r} 与「自己写了 tick()」不一致"
        f"（declares_tick={declares_tick}）"
    )


def test_every_builtin_plugin_mode_values_are_legal_and_match_its_tick():
    """六个内建插件的两个声明必须取自闭集，且 run_mode 与「有没有写 tick()」一致。

    「声明 RUN_PERIODIC 却不写 tick」= 调度器空转；「写了 tick 却声明 RUN_ON_DEMAND」=
    那段轮询体永远不会被执行。两种都是**静默失效**，用一致性断言钉住。
    """
    for pkg in _BUILTIN_PLUGIN_PACKAGES:
        cls = importlib.import_module(f"barkit.plugins.{pkg}.plugin").Plugin
        _assert_mode_tick_consistent(pkg, cls)


def test_tick_moved_into_a_mixin_still_counts_as_declared(monkeypatch):
    """非恒真控制组：把 notify_guard 的 ``run_mode`` 拨成 on-demand（那一族里仍有人写
    ``tick``）⇒ 同一条一致性判据必须转红。否则「只看本类 vars」那个变异（拆类后恒真）
    这条门禁根本抓不住。"""
    cls = importlib.import_module("barkit.plugins.notify_guard.plugin").Plugin
    monkeypatch.setattr(cls, "run_mode", RUN_ON_DEMAND)
    with pytest.raises(AssertionError):
        _assert_mode_tick_consistent("notify_guard", cls)


# ---------- Config：plugin_order 持久化 ----------

def test_config_plugin_order_round_trip():
    with tempfile.TemporaryDirectory() as d:
        user = Path(d) / "config.yaml"
        cfg = Config(user_path=user)
        assert cfg.plugin_order() == []
        cfg.set_plugin_order(["notify_guard", "route_guard", "hosts_guard"])
        cfg2 = Config(user_path=user)
        assert cfg2.plugin_order() == ["notify_guard", "route_guard", "hosts_guard"]
        cfg2.remove_from_plugin_order("route_guard")
        cfg3 = Config(user_path=user)
        assert cfg3.plugin_order() == ["notify_guard", "hosts_guard"]
        # 移除不存在的 id 幂等
        cfg3.remove_from_plugin_order("nope")
        assert cfg3.plugin_order() == ["notify_guard", "hosts_guard"]


def test_config_plugin_order_validation_rejects_non_list():
    with tempfile.TemporaryDirectory() as d:
        user = Path(d) / "config.yaml"
        user.write_text("plugin_order: not-a-list\n")
        with pytest.raises(ConfigError):
            Config(user_path=user)
        user.write_text("plugin_order: [1, 2]\n")
        with pytest.raises(ConfigError):
            Config(user_path=user)


def test_registry_plugin_order_reads_config():
    with tempfile.TemporaryDirectory() as d:
        user = Path(d) / "config.yaml"
        cfg = Config(user_path=user)
        reg = Registry(cfg)
        assert reg.plugin_order() == []
        cfg.set_plugin_order(["a", "b"])
        assert reg.plugin_order() == ["a", "b"]


# ---------- ContributionRegistry：三级排序 ----------

class _Ctx:
    pass


def _ctx():
    return _Ctx()


class _P(Plugin):
    def __init__(self, ctx=None):
        super().__init__(ctx)

    def row_specs(self):
        return (StatusRowSpec(key="main", title=self.id),)


def _mk(pid: str, order: int) -> Plugin:
    cls = type(f"P_{pid}", (_P,), {"id": pid, "order": order})
    return cls(_ctx())


def test_sorted_plugins_user_order_first_then_default_insert_position():
    # 声明序故意打乱：b(order 1) / a(order 0) / c(order 2)
    a, b, c = _mk("a", 0), _mk("b", 1), _mk("c", 2)
    from barkit.core.contributions import ContributionRegistry

    reg = ContributionRegistry([c, b, a], order_provider=lambda: ["c", "a"])
    ids = [p.id for p in reg._sorted_plugins()]
    # 用户排序中的按其顺序在前；未列入的 b 按 default order 追加尾部
    assert ids == ["c", "a", "b"]


def test_sorted_plugins_unlisted_appended_by_declared_order():
    from barkit.core.contributions import ContributionRegistry

    a, b, c = _mk("a", 30), _mk("b", 10), _mk("c", 20)
    reg = ContributionRegistry([a, b, c], order_provider=lambda: [])
    assert [p.id for p in reg._sorted_plugins()] == ["b", "c", "a"]


def test_sorted_plugins_no_provider_falls_back_to_declared_order():
    from barkit.core.contributions import ContributionRegistry

    a, b = _mk("a", 5), _mk("b", 1)
    reg = ContributionRegistry([a, b])
    assert [p.id for p in reg._sorted_plugins()] == ["b", "a"]


def test_sorted_plugins_survives_broken_provider():
    from barkit.core.contributions import ContributionRegistry

    def _boom():
        raise RuntimeError("config 损坏")

    a, b = _mk("a", 5), _mk("b", 1)
    reg = ContributionRegistry([a, b], order_provider=_boom)
    assert [p.id for p in reg._sorted_plugins()] == ["b", "a"]


# ---------- 卸载七步 ----------

class _FakeRegistry:
    def __init__(self, discovered, plugins, source):
        self._discovered = dict(discovered)
        self.plugins = dict(plugins)
        self._source = dict(source)
        self.ctx = PluginContext(config=None)
        self.forgotten: list = []

    @property
    def discovered_classes(self):
        return self._discovered

    def source_of(self, pid):
        return self._source.get(pid, "internal")

    def forget(self, pid):
        self._discovered.pop(pid, None)
        self._source.pop(pid, None)
        self.forgotten.append(pid)

    # 长驻服务托管（批次 5 后段 S2）：本桩不托管真实服务，仅满足编排调用。
    def start_services(self, plugin):
        pass

    def stop_services(self, plugin):
        pass

    def teardown_services(self, plugin):
        pass


class _FakeConfig:
    def __init__(self, plugin_order=None):
        self._plugin_order = list(plugin_order or [])
        self._enabled: dict = {}
        self.removed_sections: list = []
        self.removed_hotkeys: list = []

    def get(self, dotted, default=None):
        if dotted == "plugin_order":
            return self._plugin_order
        if dotted.startswith("plugins.") and dotted.endswith(".enabled"):
            pid = dotted.split(".")[1]
            return self._enabled.get(pid, default)
        return default

    def set_plugin_enabled(self, pid, enabled):
        self._enabled[pid] = enabled

    def plugin_order(self):
        return list(self._plugin_order)

    def set_plugin_order(self, order):
        self._plugin_order = list(order)

    def remove_from_plugin_order(self, pid):
        self._plugin_order = [x for x in self._plugin_order if x != pid]

    def remove_plugin_section(self, pid):
        self.removed_sections.append(pid)

    def remove_hotkeys(self, names):
        self.removed_hotkeys.extend(names)


class _FakeDeps:
    """PluginManager 其余依赖的空桩。"""

    def __init__(self):
        class _C:
            def activate(self, p):
                pass

            def deactivate(self, pid):
                pass

            def states(self):
                return []

            def hotkeys(self):
                return []

        self.contributions = _C()

        class _S:
            def add_plugin(self, p):
                pass

            def remove_plugin(self, pid):
                pass

        self.scheduler = _S()

        class _H:
            def register(self, *a):
                return True

            def unregister(self, name):
                pass

        self.hotkeys = _H()

        class _E:
            def subscribe(self, *a):
                pass

            def unsubscribe(self, *a):
                pass

        self.events = _E()

        class _P:
            def set_contributions(self, c):
                pass

        self.panel = _P()


class _UninstallablePlugin(Plugin):
    id = "ext1"
    name = "Ext1"
    run_mode = RUN_ON_DEMAND

    def __init__(self, ctx=None):
        super().__init__(ctx)
        self.started = False
        self.stopped = False
        self.torn_down = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def teardown(self):
        self.torn_down = True

    def row_specs(self):
        return (StatusRowSpec(key="main", title="x"),)

    def hotkeys(self):
        return (HotkeySpec(name="ext1_hk", label="d",
                           default_combo="ctrl+alt+e", callback=lambda: None),)


def _make_uninstall_env(tmp_path, plugin_order=None, cls=_UninstallablePlugin):
    ext_dir = tmp_path / "plugins"
    ext_dir.mkdir(exist_ok=True)
    (ext_dir / "ext1").mkdir(exist_ok=True)
    (ext_dir / "ext1" / "manifest.yaml").write_text("id: ext1\n")
    reg = _FakeRegistry({"ext1": cls}, {}, {"ext1": "external"})
    config = _FakeConfig(plugin_order)
    deps = _FakeDeps()
    mgr = PluginManager(
        reg, deps.contributions, deps.scheduler, deps.hotkeys, deps.events,
        deps.panel, config, on_state_update=lambda c, p: None,
        external_base=ext_dir,
    )
    return mgr, reg, config, ext_dir


def test_uninstall_runs_seven_steps(tmp_path):
    plugin_order = ["ext1", "other"]
    mgr, reg, config, ext_dir = _make_uninstall_env(tmp_path, plugin_order)
    instance = _UninstallablePlugin()
    instance.started = True
    reg.plugins["ext1"] = instance

    assert mgr.uninstall("ext1") is True
    # 2. disable：stop + 内存移除
    assert instance.stopped is True
    assert "ext1" not in reg.plugins
    # 3. teardown：已加载实例被复原
    assert instance.torn_down is True
    # 3.5 孤儿排序项移除
    assert config.plugin_order() == ["other"]
    # 4. 目录删除
    assert not (ext_dir / "ext1").exists()
    # 5. 配置段 + 热键键名清理
    assert config.removed_sections == ["ext1"]
    assert config.removed_hotkeys == ["ext1_hk"]
    # 6. forget
    assert "ext1" in reg.forgotten


def test_uninstall_teardown_skipped_for_never_loaded(tmp_path):
    mgr, reg, config, _ = _make_uninstall_env(tmp_path)
    assert mgr.uninstall("ext1") is True
    # 从未加载：没有实例被 teardown（无运行期状态可复原），但目录照删
    assert not (tmp_path / "plugins" / "ext1").exists()
    assert "ext1" in reg.forgotten


def test_uninstall_removes_data_files_minus_seeds(tmp_path, monkeypatch):
    base = tmp_path / "appsupport"
    base.mkdir()
    monkeypatch.setattr(paths, "APP_SUPPORT", base)
    (base / "ext_data.json").write_text("{}")
    (base / "ext_seed.json").write_text("{}")

    class _Res(_UninstallablePlugin):
        def resources(self):
            return PluginResources(
                data_files=["ext_data.json", "ext_seed.json"],
                seed_files=["ext_seed.json"],
            )

    mgr, reg, config, _ = _make_uninstall_env(tmp_path, cls=_Res)
    assert mgr.uninstall("ext1") is True
    assert not (base / "ext_data.json").exists()      # 非 seed：删除
    assert (base / "ext_seed.json").exists()          # seed：保留（已是用户数据）


def test_uninstall_rejects_escaping_data_file(tmp_path, monkeypatch):
    base = tmp_path / "appsupport"
    base.mkdir()
    monkeypatch.setattr(paths, "APP_SUPPORT", base)
    outside = tmp_path / "outside.json"
    outside.write_text("{}")

    class _Res(_UninstallablePlugin):
        def resources(self):
            return PluginResources(data_files=["../outside.json"])

    mgr, reg, config, _ = _make_uninstall_env(tmp_path, cls=_Res)
    assert mgr.uninstall("ext1") is True
    assert outside.exists()                            # 越界路径拒绝删除


def test_uninstall_confirm_callback_can_abort(tmp_path):
    seen: list = []

    def _deny(pid, mutations):
        seen.append((pid, mutations))
        return False

    mgr, reg, config, ext_dir = _make_uninstall_env(tmp_path)

    class _Mut(_UninstallablePlugin):
        def resources(self):
            return PluginResources(
                system_mutations=[SystemMutation(description="撤掉某系统状态")],
            )

    reg._discovered["ext1"] = _Mut
    mgr.on_uninstall_confirm = _deny
    assert mgr.uninstall("ext1") is False
    assert seen and seen[0][0] == "ext1"
    assert seen[0][1][0].description == "撤掉某系统状态"
    # 中止后：目录、配置、发现结果均未动
    assert (ext_dir / "ext1").exists()
    assert config.removed_sections == []
    assert "ext1" in reg._discovered
    assert config.plugin_order() == []


def test_uninstall_confirm_receives_mutations_and_proceeds(tmp_path):
    mgr, reg, config, _ = _make_uninstall_env(tmp_path)

    class _Mut(_UninstallablePlugin):
        def resources(self):
            return PluginResources(
                system_mutations=[
                    SystemMutation(description="a", survives_app_exit=False),
                    SystemMutation(description="b"),
                ],
            )

    reg._discovered["ext1"] = _Mut
    confirmed: list = []
    mgr.on_uninstall_confirm = lambda pid, muts: confirmed.append(muts) or True
    assert mgr.uninstall("ext1") is True
    assert len(confirmed) == 1
    assert [m.description for m in confirmed[0]] == ["a", "b"]
    assert confirmed[0][0].survives_app_exit is False


def test_uninstall_internal_rejected(tmp_path):
    ext_dir = tmp_path / "plugins"
    ext_dir.mkdir()
    reg = _FakeRegistry({"demo": _UninstallablePlugin}, {}, {"demo": "internal"})
    config = _FakeConfig()
    deps = _FakeDeps()
    mgr = PluginManager(
        reg, deps.contributions, deps.scheduler, deps.hotkeys, deps.events,
        deps.panel, config, on_state_update=lambda c, p: None,
        external_base=ext_dir,
    )
    assert mgr.uninstall("demo") is False
