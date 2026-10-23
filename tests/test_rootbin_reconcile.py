"""批次 4 · S4：A 类 root 二进制「无状态对账」与安装期契约（方案 §9.3 / §9.1）。

两条**指名门禁**（批次 4 执行计划 §3 的验收项）：

- ``test_plugin_root_binary_in_closed_set`` —— 插件声明的 A 类名字必须落在封闭集内；
  且 hev 只由它真正的使用方 ``ipad_tunnel`` 声明（route_guard 的冗余声明已撤销）。
- ``test_root_binary_reconcile_is_stateless`` —— ``reconcile`` 是纯函数：不读盘、
  不改实参、同输入同输出（对账只在**安装与卸载**触发，禁用/启用不触发）。

其余覆盖 ``resolve_sources`` 的「按声明解析来源 / 缺失即拒绝」与 ``daemon.install``
的「发行态基线校验失败不弹授权」、以及安装脚本确实写入完整性与基线清单。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from barkit import paths
from barkit.capabilities.helper import rootbin
from barkit.core import rootbin_reconcile as rr
from barkit.core.installer import KNOWN_ROOT_BINARIES
from barkit.core.resources import PluginResources
from barkit.capabilities import helper_client as daemon

SRC = Path(__file__).resolve().parents[1] / "src" / "barkit"
PLUGINS_DIR = SRC / "plugins"
HEV = "hev-socks5-tunnel"


# ──────────────────────────────────────────────
# 门禁 1：封闭集 + hev 由使用方独家声明
# ──────────────────────────────────────────────

def _declarations() -> dict[str, list[str]]:
    """静态扫描各插件的 ``root_binaries=(...)`` 声明。

    不实例化插件：构造内置插件会拉起 PySide6/Qt 与配置，代价高且与本门禁无关；
    这里只关心源码里的声明文本。
    """
    found: dict[str, list[str]] = {}
    for path in sorted(PLUGINS_DIR.glob("*/plugin.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "root_binaries":
                    continue
                names = [
                    e.value for e in getattr(kw.value, "elts", ())
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                ]
                found.setdefault(path.parent.name, []).extend(names)
    return found


def test_plugin_root_binary_in_closed_set():
    """指名门禁：任何插件声明的 A 类名字都必须在封闭集内（§9.3）。"""
    declared = _declarations()
    assert declared, "至少 ipad_tunnel 应声明 hev（A 类物理随插件分发）"
    for pid, names in sorted(declared.items()):
        unknown = sorted(set(names) - set(KNOWN_ROOT_BINARIES))
        assert not unknown, f"{pid} 声明了未登记的 A 类二进制：{unknown}"


def test_hev_declared_only_by_its_consumer():
    """hev 只由 ``ipad_tunnel`` 声明；route_guard 的冗余声明必须保持撤销（S4）。"""
    declared = _declarations()
    assert HEV in declared.get("ipad_tunnel", []), "hev 必须由它的使用方 ipad_tunnel 声明"
    assert HEV not in declared.get("route_guard", []), "route_guard 不得再声明 hev"


def test_hev_lives_under_ipad_tunnel_resources():
    """物理位置也在使用方（迁移动的是真文件，不是只改声明）。"""
    assert (PLUGINS_DIR / "ipad_tunnel" / "resources" / HEV).is_file()
    assert not (PLUGINS_DIR / "route_guard" / "resources" / HEV).exists()


def test_install_dir_matches_rootbin_single_source():
    """daemon 的落点/清单路径必须与 rootbin 真源逐条一致（不得各写一份）。"""
    assert daemon.SYS_HELPER_DIR == rootbin.SYS_DIR
    assert daemon.MANIFEST_PATH == rootbin.MANIFEST_PATH
    for name in rootbin.known_names():
        assert daemon.SYS_HELPER_DIR / name == rootbin.ROOT_BINARIES[name].install_path


# ──────────────────────────────────────────────
# 门禁 2：reconcile 是纯函数
# ──────────────────────────────────────────────

def test_root_binary_reconcile_is_stateless(monkeypatch):
    """指名门禁：不读盘、不改实参、同输入同输出。"""
    def _boom(*_args, **_kwargs):
        raise AssertionError("reconcile 不得触碰文件系统")

    monkeypatch.setattr(Path, "is_file", _boom)
    monkeypatch.setattr(Path, "exists", _boom)

    want = {"a", "b"}
    have = {"b", "c"}
    first = rr.reconcile(want, have)
    assert first == (frozenset({"a"}), frozenset({"c"}))
    assert rr.reconcile(want, have) == first          # 同输入同输出
    assert want == {"a", "b"} and have == {"b", "c"}  # 实参不被修改


def test_reconcile_empty_diff_means_no_root_needed():
    """差集为空 = 无需授权（「禁用/启用不触发对账」的实现基础）。"""
    assert rr.reconcile({HEV}, {HEV}) == (frozenset(), frozenset())
    assert rr.reconcile({HEV}, {HEV, "other"}) == (frozenset(), frozenset({"other"}))


def test_deployed_filters_non_root_binaries(tmp_path):
    """deployed 只认登记在册的名字（用户域里的同名杂物不参与对账）。"""
    (tmp_path / HEV).write_text("x", encoding="utf-8")
    (tmp_path / "随便一个文件").write_text("x", encoding="utf-8")
    assert rr.deployed(tmp_path) == frozenset({HEV})
    assert rr.deployed(tmp_path / "不存在") == frozenset()


# ──────────────────────────────────────────────
# resolve_sources：按声明解析来源，缺失即报告（安装器据此拒绝安装）
# ──────────────────────────────────────────────

class _Declaring:
    """最小插件桩：只实现 resources() 声明。"""

    id = "ipad_tunnel"

    def __init__(self, names):
        self._names = tuple(names)

    def resources(self):
        return PluginResources(root_binaries=self._names)


def test_resolve_sources_uses_declaring_plugin_package(tmp_path, monkeypatch):
    monkeypatch.setattr(rr, "package_dir", lambda plugin: tmp_path)
    (tmp_path / "resources").mkdir()
    (tmp_path / "resources" / HEV).write_text("bin", encoding="utf-8")
    sources, missing = rr.resolve_sources([_Declaring([HEV])])
    assert missing == {}
    assert sources == {HEV: tmp_path / "resources" / HEV}


def test_resolve_sources_reports_missing_source(tmp_path, monkeypatch):
    """声明了却拿不到源 → 计入 missing（不猜路径，也不静默跳过）。"""
    monkeypatch.setattr(rr, "package_dir", lambda plugin: tmp_path)
    sources, missing = rr.resolve_sources([_Declaring([HEV])])
    assert sources == {}
    assert "ipad_tunnel" in missing[HEV] and HEV in missing[HEV]


def test_resolve_sources_rejects_unregistered_name(tmp_path, monkeypatch):
    monkeypatch.setattr(rr, "package_dir", lambda plugin: tmp_path)
    sources, missing = rr.resolve_sources([_Declaring(["evil-bin"])])
    assert sources == {} and "未登记" in missing["evil-bin"]


def test_resolve_sources_tolerates_broken_declaration(tmp_path, monkeypatch):
    class _Boom:
        id = "boom"

        def resources(self):
            raise RuntimeError("坏声明")

    monkeypatch.setattr(rr, "package_dir", lambda plugin: tmp_path)
    assert rr.resolve_sources([_Boom()]) == ({}, {})
    assert rr.wanted([_Boom(), _Declaring([HEV])]) == frozenset({HEV})


# ──────────────────────────────────────────────
# daemon.install：本地预检先于授权；脚本写部署 + 完整性基线
# ──────────────────────────────────────────────

@pytest.fixture
def fake_admin(tmp_path, monkeypatch):
    """把 APP_SUPPORT 与授权执行都换掉，脚本内容留给断言。"""
    base = tmp_path / "appsupport"
    base.mkdir()
    monkeypatch.setattr(paths, "APP_SUPPORT", base)
    captured: dict[str, str] = {}

    def _run_admin(script_path, timeout=120):
        captured["script"] = Path(script_path).read_text(encoding="utf-8")
        return True

    monkeypatch.setattr(daemon, "run_admin", _run_admin)
    return captured


def test_install_rejects_declared_but_missing_source(fake_admin):
    """声明了却解析不到源 → 拒绝安装，且**不弹授权**（本地预检先行）。"""
    assert daemon.install({}, {HEV: "ipad_tunnel 未随包分发 hev"}) is False
    assert "script" not in fake_admin


def test_install_rejects_hash_mismatch(fake_admin, tmp_path):
    """源与发行态基线不符 → 拒绝安装，且不弹授权（§9.3「缺失/哈希不符即失败」）。"""
    bogus = tmp_path / HEV
    bogus.write_bytes(b"not a real hev")
    assert daemon.install({HEV: bogus}) is False
    assert "script" not in fake_admin


def _ipad_tunnel_probe():
    """真类的只读探针（不跑 __init__：resources() 是纯声明、无副作用）。"""
    from barkit.plugins.ipad_tunnel.plugin import Plugin as IpadTunnel

    return IpadTunnel.__new__(IpadTunnel)


def test_resolved_source_is_the_shipped_binary():
    """端到端解析：真插件声明 → 仓库里那个真实的 hev 文件。"""
    sources, missing = rr.resolve_sources([_ipad_tunnel_probe()])
    assert missing == {}
    assert sources == {HEV: PLUGINS_DIR / "ipad_tunnel" / "resources" / HEV}
    ok, reason = rootbin.verify_file(HEV, sources[HEV])
    assert ok, reason


def test_install_script_deploys_binary_and_writes_manifest(fake_admin, monkeypatch):
    """安装脚本必须：部署二进制（root:wheel + adhoc 签名）→ 写完整性基线 → bootstrap。"""
    sources, missing = rr.resolve_sources([_ipad_tunnel_probe()])
    assert missing == {}
    monkeypatch.setattr(daemon, "deployed_root_binaries", lambda: frozenset())

    assert daemon.install(sources) is True
    script = fake_admin["script"]
    # A 类部署：copy + owner + 解 quarantine + adhoc 签名（**不带任何受限授权**）
    assert f'install -m 0755 "{sources[HEV]}"' in script
    assert "chown root:wheel" in script
    assert "/usr/bin/codesign -s - --force" in script
    assert "vm.networking" not in script
    # 完整性基线：安装期由 root 算 helper 包聚合哈希并写 manifest
    assert "from helper import rootbin" in script
    assert "rootbin.hash_tree" in script
    assert "rootbin.write_manifest" in script
    # 旧形态与旧 label 清理是硬前置
    assert "com.barkit.routed" in script
    # 系统域不留 __pycache__（否则源码聚合哈希自检首次运行后必失败）
    assert "PYTHONDONTWRITEBYTECODE" in daemon.launcher_content()


def test_install_script_reaps_unwanted_root_binaries(fake_admin, monkeypatch):
    """对账回收：deployed − wanted 非空 → 脚本里出现对应 rm（不需要的才删）。"""
    monkeypatch.setattr(daemon, "deployed_root_binaries", lambda: frozenset({HEV}))
    assert daemon.install({}) is True            # wanted 为空 → hev 无人需要
    assert f'rm -f "{daemon.SYS_HELPER_DIR / HEV}"' in fake_admin["script"]


def test_reconcile_root_binaries_is_noop_without_diff(monkeypatch):
    """差集为空 = no-op：不弹授权（禁用/启用路径高频调用也不会有副作用）。"""
    monkeypatch.setattr(daemon, "deployed_root_binaries", lambda: frozenset({HEV}))
    calls = {"n": 0}

    def _run_admin(*_a, **_k):
        calls["n"] += 1
        return True

    monkeypatch.setattr(daemon, "run_admin", _run_admin)
    ok, detail = daemon.reconcile_root_binaries({HEV})
    assert ok and "需要回收" in detail and calls["n"] == 0


def test_reconcile_root_binaries_reaps_when_unwanted(fake_admin, monkeypatch):
    monkeypatch.setattr(daemon, "deployed_root_binaries", lambda: frozenset({HEV}))
    ok, detail = daemon.reconcile_root_binaries(set())
    assert ok and HEV in detail
    assert f'rm -f "{daemon.SYS_HELPER_DIR / HEV}"' in fake_admin["script"]


def test_uninstall_removes_manifest_and_all_root_binaries(fake_admin):
    assert daemon.uninstall() is True
    script = fake_admin["script"]
    # 完整性基线必须一并删除（否则下次安装前会读到过期基线）
    assert str(daemon.MANIFEST_PATH) in script
    assert str(daemon.HELPER_INSTALL_DIR) in script
    for name in rootbin.known_names():
        assert str(daemon.SYS_HELPER_DIR / name) in script


# ──────────────────────────────────────────────
# 卸载第 4b 步：PluginManager 只做转发（root 动作在插件侧 daemon）
# ──────────────────────────────────────────────

def _bare_manager():
    """跳过重量级构造，只验证 4b 这一步的转发行为。"""
    from barkit.core.plugin_manager import PluginManager

    pm = PluginManager.__new__(PluginManager)

    class _Registry:
        def wanted_root_binaries(self):
            return frozenset({HEV})

    pm._registry = _Registry()
    pm.on_reap_root_binaries = None
    return pm


def test_uninstall_step_4b_forwards_wanted_set():
    pm = _bare_manager()
    seen = {}

    def _reap(wanted):
        seen["wanted"] = wanted
        return True

    pm.on_reap_root_binaries = _reap
    pm._reap_root_binaries()
    assert seen["wanted"] == frozenset({HEV})


def test_uninstall_step_4b_without_wiring_is_safe():
    """未接线（如 CLI / 单测桩）只记日志，绝不抛异常、更不猜着去动 root。"""
    pm = _bare_manager()
    pm._reap_root_binaries()          # 不抛即通过
