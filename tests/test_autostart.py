"""F.2 测试：开机自启 LaunchAgent（wrapper/plist 内容 + 安装/卸载逻辑）。

不真调 launchctl / 不写真实 home：路径常量与 run_cmd/Popen 均可 monkeypatch。
launchctl 真实链路（安装→崩溃拉起→退出不拉活）依赖登录会话，留手动验证
（README 给出验证方法）。
"""

from __future__ import annotations

import sys

from barkit.core import autostart


def _patch_paths(monkeypatch, tmp_path):
    """把安装目标重定向到临时目录。"""
    wrapper = tmp_path / "bin" / "barkit"
    plist = tmp_path / "com.barkit.app.plist"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(autostart, "WRAPPER_PATH", wrapper)
    monkeypatch.setattr(autostart, "PLIST_PATH", plist)
    monkeypatch.setattr(autostart, "LAUNCH_AGENTS_DIR", tmp_path)
    return wrapper, plist


def test_wrapper_content_pins_python_and_root():
    content = autostart.wrapper_content(
        python="/tmp/fake-venv/bin/python", project_root=autostart.Path("/tmp/proj")
    )
    assert "cd /tmp/proj" in content
    assert "exec /tmp/fake-venv/bin/python -m barkit.app" in content
    assert 'export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin' in content


def test_plist_content_keepalive_semantics():
    plist = autostart.plist_content(wrapper=autostart.Path("/Users/u/.local/bin/barkit"))
    assert "<string>com.barkit.app</string>" in plist
    assert "<string>/Users/u/.local/bin/barkit</string>" in plist
    assert "<true/>" in plist           # RunAtLoad
    assert "SuccessfulExit" in plist and "<false/>" in plist   # 崩溃拉起、正常退出不拉活


def test_plist_routes_launchd_stdio_to_the_startup_log():
    """launchd 的 fd 1/2 必须落到 ``paths.STARTUP_LOG``（调用期读，随重定向走）。

    缺这两条键时，logging 起来之前那段解释器级输出被 launchd 直接丢弃 —— 现象是
    「启动就没了」，而 barkit.log 里一个字都查不到（改前正是如此）。落点必须经
    ``paths`` 取：字面量写死的话，下面这条会拿测试重定向后的路径去比、当场对不上。
    """
    from barkit import paths

    plist = autostart.plist_content(wrapper=autostart.Path("/Users/u/.local/bin/barkit"))
    assert "<key>StandardOutPath</key>" in plist
    assert "<key>StandardErrorPath</key>" in plist
    # 两条键指向同一份启动期日志（stdout 与 stderr 合流到一处，免得再来第二份）
    assert plist.count(f"<string>{paths.STARTUP_LOG}</string>") == 2


def test_wrapped_python_parse(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    autostart.WRAPPER_PATH.parent.mkdir(parents=True, exist_ok=True)
    autostart.WRAPPER_PATH.write_text(autostart.wrapper_content(), encoding="utf-8")
    assert autostart._wrapped_python() == sys.executable


def test_install_fresh_writes_files_and_bootstraps(monkeypatch, tmp_path):
    _, plist = _patch_paths(monkeypatch, tmp_path)
    calls: list[list[str]] = []

    def fake_run(args, **kw):
        calls.append(args)
        return type("Res", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(autostart, "running", lambda: False)
    monkeypatch.setattr(autostart, "run_cmd", fake_run)

    ok, hint = autostart.install()
    assert ok and hint == ""
    assert autostart.WRAPPER_PATH.exists() and autostart.PLIST_PATH.exists()
    assert [a for a in calls if a[1] == "bootstrap"] and calls[-1][0] == "/bin/launchctl"


def test_install_already_loaded_same_python_is_noop(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    autostart.WRAPPER_PATH.parent.mkdir(parents=True, exist_ok=True)
    autostart.WRAPPER_PATH.write_text(autostart.wrapper_content(), encoding="utf-8")
    autostart.PLIST_PATH.write_text(autostart.plist_content(), encoding="utf-8")
    monkeypatch.setattr(autostart, "running", lambda: True)
    monkeypatch.setattr(autostart, "run_cmd", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调用 launchctl")))

    ok, hint = autostart.install()
    assert ok and hint == ""          # 已一致：只刷新文件，不重载 job


def test_install_stale_python_schedules_reload(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    # 旧 wrapper 指向已不存在的 python → 判定 stale
    autostart.WRAPPER_PATH.write_text(
        autostart.wrapper_content(python="/old/venv/bin/python"), encoding="utf-8"
    )
    autostart.PLIST_PATH.write_text(autostart.plist_content(), encoding="utf-8")
    monkeypatch.setattr(autostart, "running", lambda: True)
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        autostart.subprocess,
        "Popen",
        lambda args, **kw: spawned.append(args) or type("P", (), {})(),
    )

    ok, hint = autostart.install()
    assert ok and hint == "reload"
    assert spawned and len(spawned) == 1
    joined = spawned[0][2]   # /bin/bash -c SCRIPT
    assert "bootout" in joined and "bootstrap" in joined
    # wrapper 已更新为新 python
    assert autostart._wrapped_python() == sys.executable


def test_uninstall_removes_plist(monkeypatch, tmp_path):
    _, plist = _patch_paths(monkeypatch, tmp_path)
    plist.write_text(autostart.plist_content(), encoding="utf-8")
    monkeypatch.setattr(autostart, "running", lambda: False)

    ok, hint = autostart.uninstall()
    assert ok and hint == ""
    assert not plist.exists()
    assert not autostart.status()["installed"]


def test_uninstall_running_job_schedules_bootout(monkeypatch, tmp_path):
    _, plist = _patch_paths(monkeypatch, tmp_path)
    plist.write_text(autostart.plist_content(), encoding="utf-8")
    monkeypatch.setattr(autostart, "running", lambda: True)
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        autostart.subprocess,
        "Popen",
        lambda args, **kw: spawned.append(args) or type("P", (), {})(),
    )

    ok, hint = autostart.uninstall()
    assert ok and hint == "dying"
    assert "bootout" in spawned[0][2]
    assert not plist.exists()


def test_status_detects_stale(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    autostart.WRAPPER_PATH.write_text(
        autostart.wrapper_content(python="/old/venv/bin/python"), encoding="utf-8"
    )
    autostart.PLIST_PATH.write_text(autostart.plist_content(), encoding="utf-8")
    monkeypatch.setattr(autostart, "running", lambda: True, )
    st = autostart.status()
    assert st["installed"] and st["running"] and st["stale"]