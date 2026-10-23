"""批次 6c 导出/导入 + 逐插件 resources()/teardown() 测试。

覆盖（方案 §9.1）：
- export_plugin_zip / import_plugin_zip 往返：配置段 + 热键 + 数据文件 100% 还原；
- SECRET 默认排除（autotype text 置空）与勾选包含；
- 导入校验：身份不匹配 / api_version 不匹配 / 未声明数据文件 → 拒绝；
- PluginManager.move_plugin（§6.4 用户排序持久化）与 import_zip 编排
  （disable → 导入 → 重新 enable）；
- route_guard / hosts_guard / ipad_tunnel 的 resources() 声明与 teardown() 幂等。
"""

from __future__ import annotations

import zipfile

from barkit import paths
from barkit.core.config import Config
from barkit.core.exporter import (
    export_plugin_zip,
    import_plugin_zip,
    read_export_manifest,
)
from barkit.core.plugin import RUN_ON_DEMAND, Plugin, PluginContext
from barkit.core.plugin_manager import PluginManager
from barkit.core.resources import PluginResources
from barkit.contribution.specs import HotkeySpec, StatusRowSpec


# ---------- 桩 ----------

class _FakeRegistry:
    def __init__(self, discovered, plugins, source, ctx):
        self._discovered = dict(discovered)
        self.plugins = dict(plugins)
        self._source = dict(source)
        self.ctx = ctx
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

    # helper task 表（批次 4 S3b-2c）：本桩不落盘，仅满足 enable 编排调用。
    def register_instance(self, plugin):
        pass

    def sync_helper_tasks(self):
        pass


class _FakeDeps:
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


def _make_manager(reg, config):
    deps = _FakeDeps()
    mgr = PluginManager(
        reg, deps.contributions, deps.scheduler, deps.hotkeys, deps.events,
        deps.panel, config, on_state_update=lambda c, p: None,
    )
    return mgr


def _mk_plugin_cls(pid="demo", data_files=None, with_hotkey=False, config=None):
    class _Demo(Plugin):
        id = pid
        name = "Demo"
        run_mode = RUN_ON_DEMAND

        def __init__(self, ctx=None):
            super().__init__(ctx)

        def resources(self):
            return PluginResources(data_files=list(data_files or []))

        def row_specs(self):
            return (StatusRowSpec(key="main", title="x"),)

        def hotkeys(self):
            if not with_hotkey:
                return ()
            return (HotkeySpec(
                name="demo_hk", label="d",
                default_combo="ctrl+alt+d", callback=lambda: None,
            ),)

        def start(self):
            pass

    return _Demo


# ---------- 导出 / 导入往返 ----------

def test_export_import_round_trip(tmp_path, monkeypatch):
    base = tmp_path / "appsupport"
    base.mkdir()
    monkeypatch.setattr(paths, "APP_SUPPORT", base)
    cfg = Config(user_path=tmp_path / "config.yaml")
    cfg.set_plugin_section("demo", {"interval": 5, "enabled": True})
    cfg.set_hotkey("demo_hk", "ctrl+shift+d")
    (base / "demo_data.json").write_text('{"v": 1}')

    cls = _mk_plugin_cls(data_files=["demo_data.json"], with_hotkey=True)
    plugin = cls(PluginContext(config=cfg))
    zip_path = export_plugin_zip(plugin, cfg, tmp_path / "out")
    assert zip_path.exists()

    # 模拟丢失：删配置段 + 删数据文件 + 热键差异项
    cfg.remove_plugin_section("demo")
    cfg.remove_hotkeys(["demo_hk"])
    (base / "demo_data.json").unlink()
    fresh = cls(PluginContext(config=cfg))
    ok, err = import_plugin_zip(fresh, cfg, zip_path)
    assert ok, err
    # 100% 还原
    assert cfg.get("plugins.demo.interval") == 5
    assert (base / "demo_data.json").read_text() == '{"v": 1}'
    assert cfg.get("hotkeys.demo_hk") == "ctrl+shift+d"


def test_export_zip_manifest_and_layout(tmp_path, monkeypatch):
    base = tmp_path / "appsupport"
    base.mkdir()
    monkeypatch.setattr(paths, "APP_SUPPORT", base)
    cfg = Config(user_path=tmp_path / "config.yaml")
    (base / "d.json").write_text("x")
    cls = _mk_plugin_cls(data_files=["d.json"])
    plugin = cls(PluginContext(config=cfg))
    zp = export_plugin_zip(plugin, cfg, tmp_path)
    with zipfile.ZipFile(zp) as zf:
        names = zf.namelist()
        assert "manifest.yaml" in names and "config.json" in names
        assert "hotkeys.json" in names and "data/d.json" in names
    manifest = read_export_manifest(zp)
    assert manifest["id"] == "demo" and manifest["api_version"] == 3


def test_export_excludes_missing_data_files(tmp_path, monkeypatch):
    base = tmp_path / "appsupport"
    base.mkdir()
    monkeypatch.setattr(paths, "APP_SUPPORT", base)
    cfg = Config(user_path=tmp_path / "config.yaml")
    cls = _mk_plugin_cls(data_files=["not_there.json"])
    plugin = cls(PluginContext(config=cfg))
    zp = export_plugin_zip(plugin, cfg, tmp_path)
    with zipfile.ZipFile(zp) as zf:
        assert [n for n in zf.namelist() if n.startswith("data/")] == []


# ---------- SECRET 处理 ----------

def test_autotype_style_secret_exclusion(tmp_path):
    cfg = Config(user_path=tmp_path / "config.yaml")
    cfg.set_plugin_section("autotype", {
        "profiles": [{"name": "a", "text": "s3cret"}, {"name": "b", "text": "x"}],
    })

    class _At(Plugin):
        id = "autotype"

        def export_data(self, include_secret: bool = False):
            data = super().export_data()
            if not include_secret:
                data["profiles"] = [
                    {**p, "text": ""} if isinstance(p, dict) else p for p in data.get("profiles", [])
                ]
            return data

    at = _At(PluginContext(config=cfg))
    clean = at.export_data()
    assert clean["profiles"][0]["text"] == ""
    full = at.export_data(include_secret=True)
    assert full["profiles"][0]["text"] == "s3cret"
    # 导入端经基类 import_data 写回
    at.import_data(clean)
    imported = cfg.get("plugins.autotype.profiles")
    assert imported[0]["text"] == ""


# ---------- 导入校验 ----------

def test_import_rejects_identity_mismatch(tmp_path, monkeypatch):
    base = tmp_path / "appsupport"
    base.mkdir()
    monkeypatch.setattr(paths, "APP_SUPPORT", base)
    cfg = Config(user_path=tmp_path / "config.yaml")
    src = tmp_path / "bad.zip"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("manifest.yaml", "id: other\napi_version: 3\n")
    cls = _mk_plugin_cls()
    ok, err = import_plugin_zip(cls(PluginContext(config=cfg)), cfg, src)
    assert not ok and "身份不匹配" in err


def test_import_rejects_bad_api_version(tmp_path, monkeypatch):
    base = tmp_path / "appsupport"
    base.mkdir()
    monkeypatch.setattr(paths, "APP_SUPPORT", base)
    cfg = Config(user_path=tmp_path / "config.yaml")
    src = tmp_path / "bad.zip"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("manifest.yaml", "id: demo\napi_version: 1\n")
    cls = _mk_plugin_cls()
    ok, err = import_plugin_zip(cls(PluginContext(config=cfg)), cfg, src)
    assert not ok and "api_version" in err


def test_import_rejects_undeclared_data_file(tmp_path, monkeypatch):
    base = tmp_path / "appsupport"
    base.mkdir()
    monkeypatch.setattr(paths, "APP_SUPPORT", base)
    cfg = Config(user_path=tmp_path / "config.yaml")
    src = tmp_path / "bad.zip"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("manifest.yaml", "id: demo\napi_version: 3\n")
        zf.writestr("config.json", "{}")
        zf.writestr("data/evil.json", "{}")
    cls = _mk_plugin_cls(data_files=["d.json"])
    ok, err = import_plugin_zip(cls(PluginContext(config=cfg)), cfg, src)
    assert not ok and "未声明" in err
    assert not (base / "evil.json").exists()       # 拒绝导入：不落盘


# ---------- PluginManager：排序 / 导入编排 ----------

def test_move_plugin_persists_user_order(tmp_path):
    cfg = Config(user_path=tmp_path / "config.yaml")

    class A(Plugin):
        id = "a"
        order = 0

    class B(Plugin):
        id = "b"
        order = 1

    class C(Plugin):
        id = "c"
        order = 2

    reg = _FakeRegistry(
        {"a": A, "b": B, "c": C}, {}, {"a": "internal", "b": "internal", "c": "internal"},
        PluginContext(config=cfg),
    )
    mgr = _make_manager(reg, cfg)
    # 生效顺序 = 声明序 a b c；把 c 上移两位 → 插到原位置上两格处 = c a b（整表持久化）。
    # 注：delta 语义为「位移」（与 OrderEditor 拖拽的绝对下标插入同源）；
    # 旧实现为 order[i]↔order[i+delta] 换位，对 |delta|>1 会得到 [c,b,a]（非移动语义），
    # UI 只以 ±1 调用故两者等价，此处已收敛到插入模型。
    assert mgr.move_plugin("c", -2) is True
    assert cfg.plugin_order() == ["c", "a", "b"]
    assert mgr.effective_order() == ["c", "a", "b"]
    # 边界：越界移动返回 False 且不写
    assert mgr.move_plugin("c", -5) is False
    assert mgr.move_plugin("nope", 1) is False


def test_manager_import_zip_disables_then_reenables(tmp_path, monkeypatch):
    base = tmp_path / "appsupport"
    base.mkdir()
    monkeypatch.setattr(paths, "APP_SUPPORT", base)
    cfg = Config(user_path=tmp_path / "config.yaml")
    cfg.set_plugin_section("demo", {"interval": 3})
    (base / "d.json").write_text("v1")

    cls = _mk_plugin_cls(data_files=["d.json"])
    reg = _FakeRegistry({"demo": cls}, {}, {"demo": "external"}, PluginContext(config=cfg))
    mgr = _make_manager(reg, cfg)

    zp = mgr.export_plugin("demo", tmp_path / "out")
    assert zp is not None
    cfg.remove_plugin_section("demo")
    (base / "d.json").unlink()

    # 已加载插件：导入须 disable → 导入 → 重新 enable
    instance = cls(PluginContext(config=cfg))
    reg.plugins["demo"] = instance
    ok, err = mgr.import_zip(zp)
    assert ok, err
    assert cfg.get("plugins.demo.interval") == 3
    assert (base / "d.json").read_text() == "v1"
    assert "demo" in reg.plugins                   # 已重新启用


# ---------- 逐插件 resources()/teardown() ----------

class _Link:
    """最小会话桩：记下声明出去的 ``(subject, payload)``。"""

    def __init__(self, ok=True):
        self.sent: list = []
        self._ok = ok

    def declare(self, subject, payload):
        self.sent.append((subject, payload))
        return self._ok


def test_route_guard_resources_and_teardown(tmp_path, monkeypatch):
    from barkit.plugins.route_guard.plugin import Plugin as RG

    base = tmp_path / "appsupport"
    base.mkdir()
    monkeypatch.setattr(paths, "APP_SUPPORT", base)
    cfg = Config(user_path=tmp_path / "config.yaml")
    link = _Link()
    ctx = PluginContext(config=cfg)
    ctx.helper = link
    p = RG(ctx)

    res = p.resources()
    # 2026-09-29 通道换成会话：路由期望态不再落盘（``routes.json`` 已删），
    # 故本组件不再声明任何数据文件。
    assert res.data_files == []
    # 批次 4 · S4：hev 只在它的真实使用方 ipad_tunnel 声明，route_guard 撤销冗余声明
    assert res.root_binaries == ()
    assert res.system_mutations and res.system_mutations[0].survives_app_exit is True

    p.teardown()
    # teardown 声明「全撤期望态」：**规则仍在列表里，但 apply 全为 False**。
    # 档 B 语义陷阱：helper 只按期望态收敛，写成空 rules 等于「没有期望」→ helper
    # 什么都不做，已生效的路由反而不会被撤。必须逐条 apply=False 才会 ensure_removed。
    assert link.sent, "teardown 必须把全撤意图声明出去"
    subject, intent = link.sent[-1]
    assert subject == "route"
    assert intent.get("rules"), "全撤必须列出规则：空列表 = 没有期望 = helper 不动作"
    assert all(r["apply"] is False for r in intent["rules"])
    assert not (base / "routes.json").exists()


def test_hosts_guard_resources_and_teardown(tmp_path, monkeypatch):
    from barkit.plugins.hosts_guard.plugin import Plugin as HG

    base = tmp_path / "appsupport"
    base.mkdir()
    monkeypatch.setattr(paths, "APP_SUPPORT", base)
    monkeypatch.setattr(paths, "HOSTS_ENTRIES_FILE", base / "hosts_entries.json")
    cfg = Config(user_path=tmp_path / "config.yaml")
    link = _Link()
    ctx = PluginContext(config=cfg)
    ctx.helper = link
    p = HG(ctx)

    res = p.resources()
    assert res.data_files == ["hosts_entries.json"]   # 只剩用户数据一项
    assert any("/etc/hosts" in m.description for m in res.system_mutations)
    p.teardown()
    subject, intent = link.sent[-1]
    # 意图 v2：禁用 = 「没有期望」→ 空块正文 + enabled=False，
    # helper 据此摘掉整个标记块。marker 是封闭取值（§7.4.4）。
    assert subject == "hosts"
    assert intent.get("enabled") is False and intent.get("content") == ""
    assert intent.get("marker") == "BarKit hosts-guard"


def test_ipad_tunnel_resources_declaration(tmp_path):
    from barkit.plugins.ipad_tunnel.plugin import Plugin as IT

    cfg = Config(user_path=tmp_path / "config.yaml")
    p = IT(PluginContext(config=cfg))
    res = p.resources()
    # 2026-09-29 通道换成会话：``tunnel.json`` / ``tunnel_state.json`` 都是通道，
    # 两边（往下传的意图、往上传的观测）都没有对象了 ⇒ 数据文件清空。
    assert res.data_files == []
    assert res.root_binaries == ("hev-socks5-tunnel",)
    assert res.system_mutations[0].survives_app_exit is False   # 依附型（§2.5）
