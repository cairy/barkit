"""批次 6b 安装四阶段测试：validate / provision（B·C 资产）/ isolate / 升级原子性。

覆盖（方案 §9.2 / §9.3）：
- validate：seed ⊆ data、相对路径防穿越、root_binaries 封闭集；
- provision：B 类按内容哈希幂等覆盖、C 类种子仅缺失时写、dry-run 不写盘；
- Registry.load_all 四阶段编排：外部插件装配资产、失败标记 problems()；
- install_external 升级原子性：预检失败不动原目录、激活失败恢复旧版。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from barkit import paths
from barkit.core.installer import (
    KNOWN_ROOT_BINARIES,
    extract_package,
    install_external,
    open_package,
    provision,
    read_installed_version,
    read_package_identity,
    validate_plugin,
)
from barkit.core.plugin import Plugin, PluginContext
from barkit.core.registry import Registry
from barkit.core.resources import PluginResources


# ---------- 桩 ----------

class _Cfg:
    def get(self, key, default=None):
        return default


def _ctx():
    return PluginContext(config=_Cfg(), events=None)


def _mk_plugin(pid="demo", resources=None):
    cls = type(
        f"P_{pid}",
        (Plugin,),
        {
            "id": pid,
            "name": pid,
            **({} if resources is None else {}),  # resources 经实例方法注入
        },
    )
    plugin = cls(_ctx())
    if resources is not None:
        plugin.resources = lambda: resources
    return plugin


# ---------- validate ----------

def test_validate_ok_for_empty_resources():
    assert validate_plugin(_mk_plugin()) is None


def test_validate_rejects_seed_outside_data():
    res = PluginResources(data_files=["a.json"], seed_files=["b.json"])
    reason = validate_plugin(_mk_plugin(resources=res))
    assert reason is not None and "子集" in reason


def test_validate_rejects_escaping_path():
    res = PluginResources(data_files=["../escape.json"])
    reason = validate_plugin(_mk_plugin(resources=res))
    assert reason is not None and "非法相对路径" in reason
    res2 = PluginResources(data_files=["/etc/hosts"])
    reason2 = validate_plugin(_mk_plugin(resources=res2))
    assert reason2 is not None and "非法相对路径" in reason2


def test_validate_rejects_unregistered_root_binary():
    assert "hev-socks5-tunnel" in KNOWN_ROOT_BINARIES
    res = PluginResources(root_binaries=("evil-bin",))
    reason = validate_plugin(_mk_plugin(resources=res))
    assert reason is not None and "未登记" in reason


def test_validate_reports_resources_exception():
    p = _mk_plugin()
    p.resources = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    reason = validate_plugin(p)
    assert reason is not None and "resources()" in reason


# ---------- provision ----------

def _make_plugin_dir(tmp_path: Path) -> Path:
    d = tmp_path / "demo"
    (d / "assets" / "icons").mkdir(parents=True)
    (d / "assets" / "icons" / "a.svg").write_text("<svg/>")
    (d / "seeds").mkdir()
    (d / "seeds" / "rules.json").write_text('{"seed": true}')
    return d


def test_provision_deploys_b_and_c(tmp_path, monkeypatch):
    base = tmp_path / "appsupport"
    if not base.exists(): base.mkdir()
    monkeypatch.setattr(paths, "APP_SUPPORT", base)
    pdir = _make_plugin_dir(tmp_path)

    ops = provision("demo", pdir, PluginResources(seed_files=["rules.json"]))
    kinds = {(op.kind, op.action) for op in ops}
    assert ("b", "cover") in kinds and ("c", "seed") in kinds
    assert (base / "plugins" / "demo" / "assets" / "icons" / "a.svg").read_text() == "<svg/>"
    assert (base / "rules.json").read_text() == '{"seed": true}'
    # B 0644 / C 0600（§9.3 权限约定）
    assert ((base / "plugins" / "demo" / "assets" / "icons" / "a.svg")).stat().st_mode & 0o777 == 0o644
    assert (base / "rules.json").stat().st_mode & 0o777 == 0o600


def test_provision_idempotent_and_never_overwrites_seed(tmp_path, monkeypatch):
    base = tmp_path / "appsupport"
    if not base.exists(): base.mkdir()
    monkeypatch.setattr(paths, "APP_SUPPORT", base)
    pdir = _make_plugin_dir(tmp_path)
    res = PluginResources(seed_files=["rules.json"])

    provision("demo", pdir, res)
    # 用户改过种子文件 → 二次装配不得覆盖（C 类存在即用户数据）
    (base / "rules.json").write_text('{"user": true}')
    ops = provision("demo", pdir, res)
    assert (base / "rules.json").read_text() == '{"user": true}'
    assert all(op.action == "skip" for op in ops)   # B/C 全部幂等跳过


def test_provision_recovers_changed_b_asset(tmp_path, monkeypatch):
    base = tmp_path / "appsupport"
    if not base.exists(): base.mkdir()
    monkeypatch.setattr(paths, "APP_SUPPORT", base)
    pdir = _make_plugin_dir(tmp_path)
    res = PluginResources()

    provision("demo", pdir, res)
    target = base / "plugins" / "demo" / "assets" / "icons" / "a.svg"
    target.write_text("tampered")
    ops = provision("demo", pdir, res)
    assert any(op.action == "cover" for op in ops)
    assert target.read_text() == "<svg/>"            # 内容哈希不同 → 幂等覆盖


def test_provision_dry_run_writes_nothing(tmp_path, monkeypatch):
    base = tmp_path / "appsupport"
    if not base.exists(): base.mkdir()
    monkeypatch.setattr(paths, "APP_SUPPORT", base)
    pdir = _make_plugin_dir(tmp_path)
    ops = provision("demo", pdir, PluginResources(seed_files=["rules.json"]), dry=True)
    assert ops and all(op.action != "skip" for op in ops)   # 清单照常计算
    assert not (base / "plugins").exists()                   # 未写盘
    assert not (base / "rules.json").exists()


# ---------- Registry.load_all 四阶段 ----------

def _mk_registry(monkeypatch, tmp_path, discovered, sources=None, enabled=True):
    reg = Registry(_Cfg(), None)
    monkeypatch.setattr(reg, "discover", lambda: discovered)
    monkeypatch.setattr(reg, "_source", sources or {k: "external" for k in discovered})
    monkeypatch.setattr(paths, "EXTERNAL_PLUGINS_DIR", tmp_path / "ext")
    ext_dir = tmp_path / "ext"
    if not ext_dir.exists():
        ext_dir.mkdir()
    reg.config = type("C", (), {"plugin_order": lambda self: [], "get": lambda self, k, d=None: d})()
    reg.ctx.config = reg.config
    # enabled 开关经 plugin.enabled 属性——默认桩配置 get 返回 None → enabled True
    return reg


def test_load_all_provisions_external_assets(tmp_path, monkeypatch):
    base = tmp_path / "appsupport"
    if not base.exists(): base.mkdir()
    monkeypatch.setattr(paths, "APP_SUPPORT", base)
    ext = tmp_path / "ext"
    pdir = ext / "demo"
    (pdir / "assets").mkdir(parents=True)
    (pdir / "assets" / "b.txt").write_text("B")
    (pdir / "seeds").mkdir()
    (pdir / "seeds" / "seed.json").write_text("s")

    class ExtDemo(Plugin):
        id = "demo"
        enabled = True

        def resources(self):
            return PluginResources(data_files=["seed.json"], seed_files=["seed.json"])

        def start(self):
            pass

    reg = _mk_registry(monkeypatch, tmp_path, {"demo": ExtDemo})
    monkeypatch.setattr(paths, "EXTERNAL_PLUGINS_DIR", ext)
    plugins = reg.load_all()
    assert [p.id for p in plugins] == ["demo"]
    assert (base / "plugins" / "demo" / "assets" / "b.txt").read_text() == "B"
    assert (base / "seed.json").read_text() == "s"


def test_load_all_isolates_invalid_declaration(tmp_path, monkeypatch):
    base = tmp_path / "appsupport"
    if not base.exists(): base.mkdir()
    monkeypatch.setattr(paths, "APP_SUPPORT", base)
    ext = tmp_path / "ext"


    class Bad(Plugin):
        id = "bad"
        enabled = True

        def resources(self):
            return PluginResources(data_files=["../evil"])

        def start(self):
            raise AssertionError("validate 失败的插件不应 start")

    class Good(Plugin):
        id = "good"
        enabled = True

        def start(self):
            pass

    reg = _mk_registry(monkeypatch, tmp_path, {"bad": Bad, "good": Good})
    monkeypatch.setattr(paths, "EXTERNAL_PLUGINS_DIR", ext)
    plugins = reg.load_all()
    assert [p.id for p in plugins] == ["good"]
    assert "bad" in reg.failed and "非法相对路径" in reg.failed["bad"]
    assert ("bad", reg.failed["bad"]) in reg.problems()


def test_load_all_records_start_failure_reason(tmp_path, monkeypatch):
    ext = tmp_path / "ext"


    class Boom(Plugin):
        id = "boom"
        enabled = True

        def start(self):
            raise RuntimeError("start boom")

    reg = _mk_registry(monkeypatch, tmp_path, {"boom": Boom})
    monkeypatch.setattr(paths, "EXTERNAL_PLUGINS_DIR", ext)
    assert reg.load_all() == []
    assert "start" in reg.failed["boom"]


# ---------- install_external 升级原子性 ----------

def _make_src(tmp_path: Path, name="mypack", pid="demo", resources_body="return PluginResources()") -> Path:
    src = tmp_path / name
    if not src.exists(): src.mkdir()
    (src / "manifest.yaml").write_text(f"id: {pid}\napi_version: 3\nentry: plugin:Plugin\n")
    (src / "plugin.py").write_text(
        "from barkit.core.plugin import Plugin\n"
        "from barkit.core.resources import PluginResources\n"
        "class Plugin(Plugin):\n"
        "    id = 'demo'\n"
        f"    def resources(self):\n        {resources_body}\n"
    )
    return src


def test_install_external_installs_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "APP_SUPPORT", tmp_path / "appsupport")
    base = tmp_path / "ext"
    if not base.exists(): base.mkdir()
    src = _make_src(tmp_path)
    activated: list = []
    ok, msg = install_external(base, src, activate=lambda pid, d: activated.append(pid))
    assert ok, msg
    assert activated == ["demo"]
    assert (base / "demo" / "manifest.yaml").exists()
    assert not (base / ".staging" / "demo-mypack").exists()   # 暂存已切换走


def test_install_external_precheck_failure_leaves_target_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "APP_SUPPORT", tmp_path / "appsupport")
    base = tmp_path / "ext"
    if not base.exists(): base.mkdir()
    old = _make_src(tmp_path, name="old")
    ok, _ = install_external(base, old)
    assert ok
    old_manifest = (base / "demo" / "manifest.yaml").read_text()

    bad = _make_src(tmp_path, name="new", resources_body="return PluginResources(data_files=['../x'])")
    ok2, msg2 = install_external(base, bad)
    assert not ok2 and "非法相对路径" in msg2
    # 预检失败：原目录内容未动
    assert (base / "demo" / "manifest.yaml").read_text() == old_manifest
    assert (base / ".staging" / "demo-new").exists()          # 暂存保留供排查


def test_install_external_activate_failure_restores_old(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "APP_SUPPORT", tmp_path / "appsupport")
    base = tmp_path / "ext"
    if not base.exists(): base.mkdir()
    ok, _ = install_external(base, _make_src(tmp_path, name="v1"))
    assert ok
    old_manifest = (base / "demo" / "manifest.yaml").read_text()

    def _boom(pid, d):
        raise RuntimeError("activate boom")

    ok2, msg2 = install_external(base, _make_src(tmp_path, name="v2"),
                                 activate=_boom)
    assert not ok2 and "激活失败" in msg2
    assert (base / "demo" / "manifest.yaml").read_text() == old_manifest  # 旧版恢复
    assert (base / ".staging" / "demo-v2").exists()                       # 新版暂存保留


# ---------- 插件包输入侧：认身份 / 解包（决议 D5） ----------

def _write_zip(zip_path: Path, entries: dict) -> Path:
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name, body in entries.items():
            zf.writestr(name, body)
    return zip_path


def _pkg_entries(prefix="", manifest=None) -> dict:
    return {
        f"{prefix}manifest.yaml": manifest
        or 'id: demo\nversion: "0.1.0"\napi_version: 3\nentry: plugin:Plugin\n',
        f"{prefix}plugin.py": "from barkit.core.plugin import Plugin\n"
                              "class Plugin(Plugin):\n    id = 'demo'\n",
    }


def test_read_package_identity_reads_id_and_version(tmp_path):
    src = _make_src(tmp_path)                       # 老清单：不写 version
    ident, reason = read_package_identity(src)
    assert reason == ""
    assert (ident.plugin_id, ident.version) == ("demo", "")

    (src / "manifest.yaml").write_text(
        'id: demo\nversion: "1.2.3"\napi_version: 3\n', encoding="utf-8"
    )
    ident2, _ = read_package_identity(src)
    assert ident2.version == "1.2.3"


def test_read_package_identity_rejects_missing_id(tmp_path):
    src = _make_src(tmp_path)
    (src / "manifest.yaml").write_text("api_version: 3\n", encoding="utf-8")
    ident, reason = read_package_identity(src)
    assert ident is None and "id" in reason


def test_read_installed_version_distinguishes_absent_from_unlabeled(tmp_path):
    """三态必须分清：没装过（None）／装了但清单没写版本（""）／装了且有版本。"""
    base = tmp_path / "ext"
    base.mkdir()
    assert read_installed_version(base, "demo") is None      # 没装过

    (base / "demo").mkdir()
    (base / "demo" / "manifest.yaml").write_text("id: demo\n", encoding="utf-8")
    assert read_installed_version(base, "demo") == ""        # 装了、版本未知

    (base / "demo" / "manifest.yaml").write_text("id: demo\nversion: 0.9\n", encoding="utf-8")
    assert read_installed_version(base, "demo") == "0.9"     # YAML 读成数字也接住


def test_extract_package_finds_root_under_wrapper_dir(tmp_path):
    """整包套一层目录（Finder 右键压缩的常见形态）要能认出来。"""
    zp = _write_zip(tmp_path / "p.zip", _pkg_entries("demo/"))
    dest = tmp_path / "out"
    root, reason = extract_package(zp, dest)
    assert reason == ""
    assert root == dest / "demo" and (root / "manifest.yaml").is_file()


def test_extract_package_skips_finder_noise(tmp_path):
    """``__MACOSX/`` 里那份清单副本不算「第二份」；``.DS_Store`` 不进包。"""
    entries = _pkg_entries("demo/")
    entries["__MACOSX/demo/manifest.yaml"] = "id: ghost\n"
    entries["demo/.DS_Store"] = "junk"
    root, reason = extract_package(_write_zip(tmp_path / "p.zip", entries), tmp_path / "out")
    assert reason == "" and root is not None
    assert not (root / ".DS_Store").exists()
    assert not (tmp_path / "out" / "__MACOSX").exists()


@pytest.mark.parametrize("name", ["../evil.txt", "/abs/evil.txt"])
def test_extract_package_rejects_unsafe_path(tmp_path, name):
    entries = _pkg_entries()
    entries[name] = "x"
    root, reason = extract_package(_write_zip(tmp_path / "p.zip", entries), tmp_path / "out")
    assert root is None and "非法路径" in reason
    assert not (tmp_path / "evil.txt").exists()


def test_extract_package_rejects_symlink(tmp_path):
    """链接能把后续写入引到包外，整包拒收。"""
    zp = tmp_path / "p.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        for name, body in _pkg_entries().items():
            zf.writestr(name, body)
        info = zipfile.ZipInfo("link")
        info.external_attr = 0o120777 << 16             # 高位 Unix mode：S_IFLNK
        zf.writestr(info, "/etc/hosts")
    root, reason = extract_package(zp, tmp_path / "out")
    assert root is None and "符号链接" in reason


def test_extract_package_requires_exactly_one_manifest(tmp_path):
    root, reason = extract_package(
        _write_zip(tmp_path / "no.zip", {"readme.txt": "hi"}), tmp_path / "o1"
    )
    assert root is None and "找不到插件清单" in reason

    two = dict(_pkg_entries("a/"), **_pkg_entries("b/"))
    root2, reason2 = extract_package(_write_zip(tmp_path / "two.zip", two), tmp_path / "o2")
    assert root2 is None and "份插件清单" in reason2


def test_extract_package_rejects_non_zip(tmp_path):
    p = tmp_path / "not.zip"
    p.write_text("hello", encoding="utf-8")
    root, reason = extract_package(p, tmp_path / "out")
    assert root is None and "不是有效的压缩包" in reason


def test_open_package_cleans_temp_dir_after_use(tmp_path):
    zp = _write_zip(tmp_path / "p.zip", _pkg_entries())
    with open_package(zp) as (pkg_dir, reason):
        assert reason == "" and (pkg_dir / "manifest.yaml").is_file()
        temp_dir = pkg_dir
    assert not temp_dir.exists()                    # 用毕即清，不在地上留解好的包


def test_open_package_directory_passes_through(tmp_path):
    """目录是**调用方的**：不复制、也不删（不能拿别人的目录当自己的临时目录）。"""
    src = _make_src(tmp_path)
    with open_package(src) as (pkg_dir, reason):
        assert pkg_dir == src and reason == ""
    assert src.exists()


def test_open_package_missing_path_is_a_reason(tmp_path):
    with open_package(tmp_path / "nope") as (pkg_dir, reason):
        assert pkg_dir is None and "路径不存在" in reason


def test_install_external_fresh_activation_failure_says_not_installed(tmp_path, monkeypatch):
    """全新安装激活失败时不能说「已恢复旧版」——那会让人以为有一个可用版本。"""
    monkeypatch.setattr(paths, "APP_SUPPORT", tmp_path / "appsupport")
    base = tmp_path / "ext"
    base.mkdir()

    def _boom(pid, d):
        raise RuntimeError("activate boom")

    ok, msg = install_external(base, _make_src(tmp_path), activate=_boom)
    assert not ok and "激活失败" in msg and "旧版" not in msg
    assert not (base / "demo").exists()             # 什么都没留下


def test_read_package_identity_rejects_data_export_package(tmp_path):
    """导出包的清单里也有 id，且文件名同样叫 barkit-plugin-* —— 必须明确认出来拒掉，
    否则用户会在「安装」里选到「导出包」，最后收到一句看不出所以然的导入失败。"""
    src = tmp_path / "pkg"
    src.mkdir()
    (src / "manifest.yaml").write_text(
        'format_version: 1\nid: demo\napi_version: 3\nexported_at: "2026-09-16T10:00:00"\n',
        encoding="utf-8",
    )
    ident, reason = read_package_identity(src)
    assert ident is None and "恢复插件数据" in reason
