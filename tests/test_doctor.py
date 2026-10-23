"""`barkit doctor` 自检脚手架的用例（D1 批次：骨架 ＋ A/B/C 组；D2：D/E/F 组）。

本文件全部可离屏跑 —— doctor 的设计目标之一就是「不依赖 GUI」，
因此它的用例也不该依赖窗口服务器（Windows 侧也能验，见
`.workbuddy/skills/barkit-windows-verify/`）。
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from barkit import cli, paths
from barkit.doctor import (
    GROUPS,
    GroupSpec,
    checks_data,
    checks_helper,
    checks_permissions,
    checks_plugins,
    collect,
    probes,
    render,
    survey,
)
from barkit.doctor import run as doctor_run
from barkit.doctor.model import FAIL, PASS, SKIP, WARN, Check, Report, Result, run_checks

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"


# ──────────────────────────────────────────────
# model：级别 / 退出码 / 逐条隔离
# ──────────────────────────────────────────────

def test_exit_code_is_zero_only_without_failures():
    report = Report()
    report.add(Result("X1", "g", "t", PASS), Result("X2", "g", "t", WARN),
               Result("X3", "g", "t", SKIP))
    assert report.exit_code == 0
    assert report.worst == WARN
    report.add(Result("X4", "g", "t", FAIL))
    assert report.exit_code == 1
    assert report.worst == FAIL
    assert [r.id for r in report.failures] == ["X4"]


def test_empty_report_is_skip_not_pass():
    """空报告不许报成 PASS —— 「一项都没跑」和「全过」不是一回事。"""
    assert Report().exit_code == 0
    assert Report().worst == SKIP


def test_run_checks_isolates_a_throwing_check():
    """一条检查内部炸了，只毁那一条，其余照跑完。"""
    def boom() -> list[Result]:
        raise RuntimeError("环境怪")

    results = run_checks("g", (
        Check("X1", "好的一条", lambda: [Result("X1", "g", "好的一条", PASS)]),
        Check("X2", "坏的一条", boom),
        Check("X3", "之后的一条", lambda: [Result("X3", "g", "之后的一条", PASS)]),
    ))
    by_id = {r.id: r for r in results}
    assert by_id["X1"].level == PASS
    assert by_id["X3"].level == PASS, "抛异常的那条不得中断后面的检查"
    assert by_id["X2"].level == FAIL
    assert "RuntimeError" in by_id["X2"].detail
    assert by_id["X2"].hint, "FAIL 必须带「下一步做什么」"


# ──────────────────────────────────────────────
# probes（A 组）：只读是硬约束
# ──────────────────────────────────────────────

def test_env_group_ids_and_levels():
    results = probes.check_env()
    ids = {r.id for r in results}
    assert {"A1", "A2", "A4", "A5", "A6"} <= ids
    assert any(r.id.startswith("A3") for r in results), "依赖检查必须有结果"
    assert all(r.level in {PASS, WARN, FAIL, SKIP} for r in results)
    assert all(r.group == "env" for r in results)


def test_missing_app_support_dir_is_not_created(monkeypatch, tmp_path):
    """**关键约束**：目录不存在时 doctor 只探祖先，绝不替用户创建。"""
    target = tmp_path / "not-yet"
    monkeypatch.setattr(paths, "APP_SUPPORT", target, raising=False)
    (result,) = probes.check_app_support()
    assert result.level == WARN
    assert not target.exists(), "doctor 不得创建用户目录"


def test_writable_dir_passes_and_leaves_no_residue(monkeypatch, tmp_path):
    target = tmp_path / "data"
    target.mkdir()
    monkeypatch.setattr(paths, "APP_SUPPORT", target, raising=False)
    (result,) = probes.check_app_support()
    assert result.level == PASS
    # 探针是 doctor 唯一的写盘动作，必须自清
    assert list(target.iterdir()) == []


def test_dir_path_that_is_a_file_fails(monkeypatch, tmp_path):
    target = tmp_path / "file-not-dir"
    target.write_text("x", encoding="utf-8")
    monkeypatch.setattr(paths, "APP_SUPPORT", target, raising=False)
    (result,) = probes.check_app_support()
    assert result.level == FAIL
    assert result.hint


def test_platform_check_tracks_sys_platform(monkeypatch):
    monkeypatch.setattr(probes.sys, "platform", "darwin")
    assert probes.check_platform()[0].level == PASS
    monkeypatch.setattr(probes.sys, "platform", "win32")
    off = probes.check_platform()[0]
    assert off.level == FAIL
    assert off.hint, "非 macOS 的 FAIL 要说明「只能写码不能跑」"


def test_dependency_failure_is_reported_per_module(monkeypatch):
    monkeypatch.setattr(
        probes, "DEPENDENCIES", (("definitely_not_a_real_module_xyz", "假用途", "装它"),)
    )
    (result,) = probes.check_dependencies()
    assert result.id == "A3.definitely_not_a_real_module_xyz"
    assert result.level == FAIL
    assert result.hint == "装它"


# ──────────────────────────────────────────────
# checks_plugins：纯函数部分（真发现过程是 macOS 专属，见 darwin_only）
# ──────────────────────────────────────────────

def test_orphan_sections_lists_unknown_plugin_ids():
    data = {"plugins": {"route_guard": {}, "ghost": {}, "old_thing": {}}}
    assert checks_plugins.orphan_sections(data, {"route_guard"}) == ["ghost", "old_thing"]


def test_orphan_sections_tolerates_missing_and_broken_shapes():
    assert checks_plugins.orphan_sections({}, set()) == []
    assert checks_plugins.orphan_sections({"plugins": "不是 mapping"}, set()) == []


def test_rejection_results_carry_reason():
    (result,) = checks_plugins.rejection_results([("demo", "契约 v9 过旧")])
    assert result.level == FAIL
    assert result.id == "C3.demo"
    assert "契约 v9 过旧" in result.detail


# ──────────────────────────────────────────────
# checks_data（D 组）：崩溃恢复快照 ＋ 用户数据 ＋ 日志
# ──────────────────────────────────────────────

#: D 组会碰到的全部受管文件。重定向手法与 conftest 一致：**改模块属性**，
#: 按值导入会让补丁静默失效。
_DATA_PATH_ATTRS = (
    "HOSTS_ENTRIES_FILE", "HELPER_SOCKET", "BT_HELPER_SOCKET", "HELPER_LOG",
)


def _redirect_data_files(monkeypatch, tmp_path):
    """把用户域文件整体挪进 tmp_path（保留原文件名）。"""
    monkeypatch.setattr(paths, "APP_SUPPORT", tmp_path, raising=False)
    monkeypatch.setattr(paths, "LOG_DIR", tmp_path, raising=False)
    for attr in _DATA_PATH_ATTRS:
        monkeypatch.setattr(paths, attr, tmp_path / getattr(paths, attr).name, raising=False)


def _expected_snapshot_version():
    """快照格式当前期望的版本号；取不到就让用例失败（不猜）。"""
    value, why = checks_data._snapshot_version()
    assert value is not None, why
    return value


def test_snapshot_absent_is_skip_not_pass(monkeypatch, tmp_path):
    """一份快照都没有 ⇒ SKIP。**不许报 PASS** —— 「没跑」和「全过」不是一回事。"""
    _redirect_data_files(monkeypatch, tmp_path)
    (result,) = checks_data.check_snapshot()
    assert result.id == "D1"
    assert result.level == SKIP


def test_snapshot_ok_passes(monkeypatch, tmp_path):
    _redirect_data_files(monkeypatch, tmp_path)
    (tmp_path / checks_data.SNAPSHOT_NAME).write_text(json.dumps({
        "version": _expected_snapshot_version(),
        "written_at": int(time.time()),
        "subjects": {"route": {"intent": {}, "observed": {}}},
    }), encoding="utf-8")
    (result,) = checks_data.check_snapshot()
    assert result.id == "D1"
    assert result.level == PASS
    assert "1 个领域" in result.detail


def test_snapshot_broken_json_is_warn_not_fail(monkeypatch, tmp_path):
    """快照坏了只报 WARN：它不参与任何在线判据，守护按「没有」处理。"""
    _redirect_data_files(monkeypatch, tmp_path)
    (tmp_path / checks_data.SNAPSHOT_NAME).write_text("{ 这不是 JSON", encoding="utf-8")
    (result,) = checks_data.check_snapshot()
    assert result.level == WARN
    assert result.level != FAIL
    assert result.hint, "WARN 也要带「下一步做什么」"


def test_snapshot_missing_subjects_is_warn(monkeypatch, tmp_path):
    _redirect_data_files(monkeypatch, tmp_path)
    (tmp_path / checks_data.SNAPSHOT_NAME).write_text(
        json.dumps({"version": _expected_snapshot_version()}), encoding="utf-8")
    (result,) = checks_data.check_snapshot()
    assert result.level == WARN
    assert "subjects" in result.detail


def test_snapshot_version_mismatch_is_warn(monkeypatch, tmp_path):
    """版本不符只报 WARN：快照单写方（守护），同代问题归 E1 判死，不在 D 组重复。"""
    _redirect_data_files(monkeypatch, tmp_path)
    (tmp_path / checks_data.SNAPSHOT_NAME).write_text(json.dumps({
        "version": _expected_snapshot_version() + 1, "subjects": {},
    }), encoding="utf-8")
    (result,) = checks_data.check_snapshot()
    assert result.level == WARN
    assert "version" in result.detail


def test_snapshot_version_reports_unreachable_module(monkeypatch, tmp_path):
    """取不到期望版本时**如实说问不到**，不许编一个数字出来。"""
    _redirect_data_files(monkeypatch, tmp_path)
    (tmp_path / checks_data.SNAPSHOT_NAME).write_text(json.dumps({
        "version": 1, "subjects": {},
    }), encoding="utf-8")
    monkeypatch.setattr(checks_data, "_SNAPSHOT_MODULE", "definitely.not.a.module_xyz")
    (result,) = checks_data.check_snapshot()
    assert result.level == WARN
    assert "取不到期望版本" in result.detail


def test_user_json_broken_is_fail(monkeypatch, tmp_path):
    _redirect_data_files(monkeypatch, tmp_path)
    paths.HOSTS_ENTRIES_FILE.write_text("[1, 2,", encoding="utf-8")
    (result,) = checks_data.check_user_json()
    assert result.level == FAIL
    assert result.id == "D2.hosts_entries.json"


def test_user_json_ok_passes(monkeypatch, tmp_path):
    _redirect_data_files(monkeypatch, tmp_path)
    paths.HOSTS_ENTRIES_FILE.write_text('{"version": 1}', encoding="utf-8")
    (tmp_path / "interaction_state.json").write_text('{"days": {}}', encoding="utf-8")
    (result,) = checks_data.check_user_json()
    assert result.level == PASS
    assert "2 份" in result.detail


def test_user_json_names_match_their_source_constants():
    """清单里的文件名与它自称的真源**必须逐字相同**（两处字面量，没有别的东西绑着）。

    改名或换真源时只动一处 ⇒ 这份清单**静默**指着不存在的文件：新的那份数据从此
    不再被 doctor 检查、老的那条悄悄 ``skip``，**两件事都不报错**（合并方案批 4 把按天
    记账并成 ``agenda_log.json`` 时差点就这么过去）。真源写完整 import 路径正是为了
    能当场把它解出来 —— 它们是给用户看的字符串，不是装饰。

    比的是 **basename**：真源有的是一条整路径（``paths.HOSTS_ENTRIES_FILE``）、有的是
    裸文件名（``day_log.AGENDA_FILENAME``），而清单里那一栏两种都只存文件名。
    """
    for filename, source in checks_data.USER_JSON_FILES:
        module_name, _, attr = source.rpartition(".")
        assert module_name, f"{source} 不是「模块.常量」的形式"
        module = importlib.import_module(module_name)
        actual = getattr(module, attr, None)
        assert actual is not None, f"{source} 取不到（模块里没有这个常量？）"
        assert Path(str(actual)).name == filename, (
            f"清单写的是 {filename!r}，而 {source} 实际是 {actual!r}"
        )


def test_log_tail_surfaces_first_error(monkeypatch, tmp_path):
    _redirect_data_files(monkeypatch, tmp_path)
    (tmp_path / "barkit.log").write_text(
        "2026-09-17 08:00:00 INFO 启动\n"
        "2026-09-17 08:00:01 ERROR 路由同步失败\n"
        "2026-09-17 08:00:02 INFO 继续\n",
        encoding="utf-8",
    )
    (result,) = checks_data.check_log_tail()
    assert result.level == WARN
    assert "路由同步失败" in result.detail
    assert result.hint


def test_log_tail_clean_passes(monkeypatch, tmp_path):
    _redirect_data_files(monkeypatch, tmp_path)
    (tmp_path / "barkit.log").write_text("2026-09-17 INFO 一切正常\n", encoding="utf-8")
    (result,) = checks_data.check_log_tail()
    assert result.level == PASS


def test_data_group_is_read_only(monkeypatch, tmp_path):
    """**只读断言**：D 组跑完，临时目录里不许冒出任何新文件。"""
    _redirect_data_files(monkeypatch, tmp_path)
    paths.HOSTS_ENTRIES_FILE.write_text(json.dumps({"version": 1}), encoding="utf-8")
    (tmp_path / checks_data.SNAPSHOT_NAME).write_text(json.dumps({
        "version": _expected_snapshot_version(), "subjects": {},
    }), encoding="utf-8")
    before = {p.name: p.stat().st_mtime for p in tmp_path.iterdir()}
    checks_data.check_data()
    after = {p.name: p.stat().st_mtime for p in tmp_path.iterdir()}
    assert after == before


# ──────────────────────────────────────────────
# checks_helper（E 组）：A 类资产对账走**类声明**
# ──────────────────────────────────────────────

def test_root_binaries_readable_from_classes_without_instances():
    """**核心约束**：读 A 类声明不需要实例化插件（不跑 ``__init__``）。

    doctor 按只读铁律不跑 ``load_all()``，只能靠类声明算 wanted 侧；而
    ``Registry.wanted_root_binaries()`` 读的是「已装配实例」，用它做对账会把
    **全部已部署资产判成多余并建议回收**。
    """
    from barkit.core.resources import PluginResources

    class _Declared:
        id = "demo"

        def __init__(self, *a, **k):     # 真被实例化就该炸
            raise AssertionError("doctor 不得实例化插件")

        def resources(self):
            return PluginResources(root_binaries=["hev-socks5-tunnel"])

    class _Broken:
        id = "broken"

        def resources(self):
            raise RuntimeError("声明坏了")

    want, owners, errors = survey.declared_root_binaries(
        {"demo": _Declared, "broken": _Broken}
    )
    assert want == frozenset({"hev-socks5-tunnel"})
    assert owners["hev-socks5-tunnel"] == "demo"
    assert [pid for pid, _ in errors] == ["broken"], "读不出的声明必须报出来，不许静默"


def test_helper_not_installed_is_warn(monkeypatch, tmp_path):
    from barkit.capabilities import helper_client

    monkeypatch.setattr(helper_client, "status", lambda: {
        "installed": False, "running": False, "version": None,
        "expected": "2.11.2", "outdated": False,
    })
    (result,) = checks_helper.check_helper_status()
    assert result.level == WARN, "没装 helper 是常见状态，不许判死"
    assert result.hint


def test_helper_outdated_is_fail(monkeypatch):
    from barkit.capabilities import helper_client

    monkeypatch.setattr(helper_client, "status", lambda: {
        "installed": True, "running": True, "version": "2.10.0",
        "expected": "2.11.2", "outdated": True,
    })
    (result,) = checks_helper.check_helper_status()
    assert result.level == FAIL
    assert "重装" in result.hint


def test_install_format_outdated_is_warn(monkeypatch, tmp_path):
    from barkit.capabilities import helper_client

    monkeypatch.setattr(helper_client, "HELPER_VERSION_PATH", tmp_path / "version.py",
                        raising=False)
    (result,) = checks_helper.check_install_format()
    assert result.level == SKIP, "没有版本锚点 = 没装，先让 E1 说话"

    anchor = tmp_path / "version.py"
    anchor.write_text('HELPER_VERSION = "2.11.2"\n', encoding="utf-8")
    monkeypatch.setattr(helper_client, "install_format_outdated", lambda: True)
    (result,) = checks_helper.check_install_format()
    assert result.level == WARN


def test_root_binaries_missing_asset_is_warn(monkeypatch, tmp_path):
    """声明了但系统域没有 ⇒ WARN，并指出是哪个插件的声明。"""
    from barkit.capabilities import helper_client
    from barkit.core.resources import PluginResources

    class _Declared:
        id = "ipad_tunnel"

        def resources(self):
            return PluginResources(root_binaries=["hev-socks5-tunnel"])

    monkeypatch.setattr(survey, "discover", lambda: (None, {"ipad_tunnel": _Declared}, []))
    monkeypatch.setattr(helper_client, "helper_installed", lambda: True)
    monkeypatch.setattr(helper_client, "deployed_root_binaries", lambda: frozenset())
    results = checks_helper.check_root_binaries()
    by_id = {r.id: r for r in results}
    assert by_id["E3.缺"].level == WARN
    assert "hev-socks5-tunnel" in by_id["E3.缺"].detail
    assert "ipad_tunnel" in by_id["E3.缺"].detail


def test_root_binaries_extra_asset_is_warn(monkeypatch):
    """系统域有、声明里没有 ⇒ 提示可回收（卸载残留）。"""
    from barkit.capabilities import helper_client

    monkeypatch.setattr(survey, "discover", lambda: (None, {}, []))
    monkeypatch.setattr(helper_client, "helper_installed", lambda: True)
    monkeypatch.setattr(helper_client, "deployed_root_binaries",
                        lambda: frozenset({"hev-socks5-tunnel"}))
    results = checks_helper.check_root_binaries()
    by_id = {r.id: r for r in results}
    assert by_id["E3.多"].level == WARN


def test_root_binaries_not_installed_is_skip(monkeypatch):
    from barkit.capabilities import helper_client

    monkeypatch.setattr(survey, "discover", lambda: (None, {}, []))
    monkeypatch.setattr(helper_client, "helper_installed", lambda: False)
    (result,) = checks_helper.check_root_binaries()
    assert result.level == SKIP


def test_helper_log_over_cap_is_warn(monkeypatch, tmp_path):
    """日志**超过上限** ⇒ 提示大概是不会截尾的旧装（单文件就地截尾才是本版形态）。

    判据 2026-09-29 换过：从前看「有没有 ``helper.log.1``」，而备份形态已下线 ——
    新 helper 永远不会再生出 ``.1``，那条判据会把正常的单文件态误报成旧装。
    """
    _redirect_data_files(monkeypatch, tmp_path)
    log = tmp_path / "helper.log"
    log.write_text("x" * (checks_helper.LOG_CAP_BYTES + 1), encoding="utf-8")
    (result,) = checks_helper.check_helper_log()
    assert result.level == WARN
    assert "截尾" in result.hint

    # 早先轮转留下的 .1 只是残渣：大小在上限内 ⇒ PASS，但在 detail 里提一句
    log.write_text("x" * 1024, encoding="utf-8")
    log.with_name("helper.log.1").write_text("旧", encoding="utf-8")
    (result,) = checks_helper.check_helper_log()
    assert result.level == PASS
    assert "helper.log.1" in result.detail


def test_autostart_not_installed_is_skip(monkeypatch):
    from barkit.core import autostart

    monkeypatch.setattr(autostart, "status", lambda: {
        "installed": False, "running": False, "stale": False,
    })
    (result,) = checks_helper.check_autostart()
    assert result.level == SKIP

    monkeypatch.setattr(autostart, "status", lambda: {
        "installed": True, "running": True, "stale": True,
    })
    (result,) = checks_helper.check_autostart()
    assert result.level == WARN, "venv 重建后启动器指向旧解释器，要提示重装"


# ──────────────────────────────────────────────
# checks_permissions（F 组）：数据驱动，未知键必须报出来
# ──────────────────────────────────────────────

def test_permission_unknown_key_is_warn_not_silent(monkeypatch):
    """声明面扩容而探针表没跟上 ⇒ 必须报 WARN，**不许静默跳过**。"""
    monkeypatch.setattr(survey, "discover", lambda: (None, {"demo": object}, []))
    monkeypatch.setattr(survey, "declared_permissions",
                        lambda found: {"demo": ("screen_recording",)})
    (result,) = checks_permissions.check_permissions()
    assert result.id == "F2.screen_recording"
    assert result.level == WARN
    assert "demo" in result.detail


def test_accessibility_unauthorized_is_fail(monkeypatch):
    from barkit.core import ax_trust

    monkeypatch.setattr(survey, "discover", lambda: (None, {"autotype": object}, []))
    monkeypatch.setattr(survey, "declared_permissions",
                        lambda found: {"autotype": ("accessibility",)})
    monkeypatch.setattr(ax_trust, "available", lambda: True)
    monkeypatch.setattr(ax_trust, "accessibility_trusted", lambda: False)
    (result,) = checks_permissions.check_permissions()
    assert result.level == FAIL
    assert "autotype" in result.title
    assert ax_trust.ACCESSIBILITY_PANE_URL in result.hint, "hint 要给得能直接照做"


def test_accessibility_granted_passes(monkeypatch):
    from barkit.core import ax_trust

    monkeypatch.setattr(survey, "discover", lambda: (None, {"autotype": object}, []))
    monkeypatch.setattr(survey, "declared_permissions",
                        lambda found: {"autotype": ("accessibility",)})
    monkeypatch.setattr(ax_trust, "available", lambda: True)
    monkeypatch.setattr(ax_trust, "accessibility_trusted", lambda: True)
    (result,) = checks_permissions.check_permissions()
    assert result.level == PASS


def test_ax_unavailable_is_skip_not_fail(monkeypatch):
    """「问不出来」与「没授权」是两件事，前者必须 SKIP。"""
    from barkit.core import ax_trust

    monkeypatch.setattr(survey, "discover", lambda: (None, {"autotype": object}, []))
    monkeypatch.setattr(survey, "declared_permissions",
                        lambda found: {"autotype": ("accessibility",)})
    monkeypatch.setattr(ax_trust, "available", lambda: False)
    (result,) = checks_permissions.check_permissions()
    assert result.level == SKIP


def test_no_declared_permissions_passes(monkeypatch):
    monkeypatch.setattr(survey, "discover", lambda: (None, {"recognize": object}, []))
    monkeypatch.setattr(survey, "declared_permissions", lambda found: {"recognize": ()})
    (result,) = checks_permissions.check_permissions()
    assert result.level == PASS


# ──────────────────────────────────────────────
# render
# ──────────────────────────────────────────────

def _sample_report() -> Report:
    report = Report()
    report.declare_group("env", "运行环境")
    report.add(
        Result("A1", "env", "Python 版本", PASS, "当前 3.12.8"),
        Result("A2", "env", "运行平台", FAIL, "当前 win32", "请在 macOS 上跑"),
    )
    return report


def test_render_text_prints_hint_for_failures():
    text = render.render_text(_sample_report())
    assert "运行环境" in text
    assert "A2" in text and "当前 win32" in text
    assert "→ 请在 macOS 上跑" in text
    assert "结论：FAIL" in text


def test_render_text_verbose_shows_pass_details():
    report = _sample_report()
    assert "当前 3.12.8" not in render.render_text(report)
    assert "当前 3.12.8" in render.render_text(report, verbose=True)


def test_render_json_is_machine_readable():
    payload = json.loads(render.render_json(_sample_report()))
    assert payload["schema"] == render.SCHEMA
    assert payload["summary"] == {PASS: 1, WARN: 0, FAIL: 1, SKIP: 0}
    assert payload["exit_code"] == 1
    assert payload["results"][1]["hint"] == "请在 macOS 上跑"
    assert payload["results"][1]["group_title"] == "运行环境"


# ──────────────────────────────────────────────
# collect / run：编排
# ──────────────────────────────────────────────

def test_collect_skips_darwin_only_groups_off_darwin(monkeypatch):
    monkeypatch.setattr(probes.sys, "platform", "linux")
    report = collect()
    plugins_rows = [r for r in report.results if r.group == "plugins"]
    assert plugins_rows and all(r.level == SKIP for r in plugins_rows)
    assert any(r.group == "env" for r in report.results), "平台无关的组照跑"


def test_group_table_covers_d2_batches():
    """D2 三组必须真的挂进表里，且 ``darwin_only`` 标得准。

    标错的代价是双向的：把 ``data`` 标成 darwin_only ⇒ 非 macOS 上白白跳过一组
    能跑的检查；漏标 ``helper`` ⇒ Windows 上跑 doctor 会满屏红（plan §8 R3）。
    """
    by_key = {spec.key: spec for spec in GROUPS}
    assert {"env", "config", "plugins", "data", "helper", "permissions"} == set(by_key)
    assert [s.key for s in GROUPS].index("env") == 0, "地基组排最前"
    darwin_only = {k for k, s in by_key.items() if s.darwin_only}
    assert darwin_only == {"plugins", "helper", "permissions"}, (
        "data / config 是纯逻辑检查，不该跟着平台跳过"
    )
    assert all(s.target.count(":") == 1 for s in GROUPS), "target 须是 «模块:函数名»"


def test_every_group_declares_a_title():
    for spec in GROUPS:
        assert spec.title and not spec.title.startswith("barkit."), spec


def test_collect_isolates_a_broken_group():
    """组级 import 失败 ⇒ 该组 FAIL（带原因），其余组照跑。"""
    specs = (
        GroupSpec("broken", "坏组", "barkit.doctor._no_such_module:check", False),
        GroupSpec("env", "运行环境", "barkit.doctor.probes:check_env", False),
    )
    report = collect(specs)
    by_group = {}
    for r in report.results:
        by_group.setdefault(r.group, []).append(r)
    assert by_group["broken"][0].level == FAIL
    assert "_no_such_module" in by_group["broken"][0].detail
    assert by_group["env"], "前面的组坏了，后面的组仍要跑"


def test_collect_declares_groups_in_order():
    report = collect()
    assert list(report.groups) == [spec.key for spec in GROUPS]


def test_run_rejects_unknown_flag():
    """用法错 ⇒ 退出码 2（argparse 自己已打印用法）。"""
    assert doctor_run(["--no-such-flag"]) == 2


def test_run_json_output_is_parseable(capsys):
    code = doctor_run(["--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code in (0, 1)
    assert payload["exit_code"] == code
    assert payload["results"], "至少要跑出 A 组的结果"


# ──────────────────────────────────────────────
# cli：分派
# ──────────────────────────────────────────────

def test_dispatch_falls_back_to_gui(monkeypatch):
    """未知参数（含 macOS Finder 塞的 -psn_…）不许报错，一律回落 GUI。"""
    assert cli.dispatch([]) is None
    assert cli.dispatch(["-psn_0_12345"]) is None
    assert cli.dispatch(["nonsense"]) is None


def test_dispatch_help_prints_usage(capsys):
    assert cli.dispatch(["--help"]) == 0
    assert "barkit doctor" in capsys.readouterr().out


def test_dispatch_doctor_routes_to_engine(monkeypatch):
    seen = {}

    def fake_run(argv):
        seen["argv"] = list(argv)
        return 7

    import barkit.doctor as doctor
    monkeypatch.setattr(doctor, "run", fake_run)
    assert cli.dispatch(["doctor", "--json"]) == 7
    assert seen["argv"] == ["--json"]


# ──────────────────────────────────────────────
# 「PySide6 装不上」时 doctor 仍要能报出这件事（**行为门禁**）
#
# 比 AST 门禁更贴目标：它证明的是端到端能力，而不是「文件里没有那行 import」。
# 用子进程做 —— 本进程里 PySide6 早已进了 sys.modules，拦不住。
# ──────────────────────────────────────────────

_MISSING_QT_PROBE = """
import builtins
_real = builtins.__import__


def _blocked(name, *args, **kwargs):
    if name.split('.')[0] in ('PySide6', 'shiboken6'):
        raise ImportError('blocked by test')
    return _real(name, *args, **kwargs)


builtins.__import__ = _blocked

from barkit import cli            # 入口链必须在无 Qt 时也能导入
print(cli.dispatch(['doctor']))   # 打印 doctor 的退出码
"""


def test_doctor_survives_missing_pyside6():
    proc = subprocess.run(
        [sys.executable, "-c", _MISSING_QT_PROBE],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
        env={**os.environ, "PYTHONPATH": str(SRC), "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode == 0, f"doctor 自己在无 Qt 环境里崩了：\n{proc.stderr}"
    out = proc.stdout
    assert "PySide6" in out, f"没把「PySide6 缺失」报出来：\n{out}"
    assert out.strip().splitlines()[-1] == "1", f"缺依赖时退出码应为 1：\n{out}"
