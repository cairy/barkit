"""「谁是被服务的那个用户」这条链的**两端**：安装期给信息 / 启动期用信息。

这是同一条因果链，两端不许半做（本仓口径：同一链路两端不许半做）：

- **安装期**（app 侧，``helper_client``）：此刻本进程正归那个用户所有，所以 uid 是
  **已知**的 —— 写进 plist 的 ``BARKIT_UID``，顺手把数据目录归位（重装即自愈）。
- **启动期**（守护侧，``core``）：守护开机先起、app 可能几小时后才开。它**推不出**
  这个 uid，只能靠安装期留下的那一项。目录还不存在时，「数据目录的属主」这条兜底
  必然取不到（那是它自己刚建出来的，属主就是 root）⇒ **不许据此 chown**。

猜错的代价不对称、而且**当场看不见**：chown 成 root 之后 app 连不上会话、也写不进
配置，要等 app 起来才发作，而它非 root 改不回来 —— 只能重装守护自愈。

（``os.getuid`` 是 POSIX 专有，而安装器本来就是 macOS 专有：osascript / launchctl /
``/Library``。测试里把它打成定值，让这条链在 Windows 上也跑得起来。）
"""

from __future__ import annotations

import os
import re

import pytest

from barkit import paths
from barkit.capabilities import helper_client as HC
from barkit.capabilities.helper import core


@pytest.fixture
def captured_install(monkeypatch):
    """拦住 ``install()`` 的三处副作用，只把拼出来的脚本留下。"""
    box: dict = {}
    monkeypatch.setattr(HC, "_write_script",
                        lambda name, body: (box.setdefault("script", body), "/tmp/fake.sh")[1])
    monkeypatch.setattr(HC, "_cleanup_script", lambda *a, **k: None)
    monkeypatch.setattr(HC, "run_admin", lambda *a, **k: True)
    monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
    return box


# ──────────────────────────────────────────────
# 1. 安装期：把「被服务的是谁」留给守护
# ──────────────────────────────────────────────

def test_installed_plist_carries_the_served_uid(captured_install):
    """plist 里必须有 ``BARKIT_UID``，值是**安装那一刻**的用户 uid。

    它是守护开机先起时**唯一**能给得出 uid 的来源。从前这一项根本没人写（模板里
    没有、docstring 却写着「优先读它」）⇒ 那条「优先」分支永不命中。
    """
    assert HC.install() is True
    script = captured_install["script"]
    m = re.search(r"<key>BARKIT_UID</key>\s*<string>(\d+)</string>", script)
    assert m is not None, "plist 缺 BARKIT_UID：守护开机先起时推不出被服务的是谁"
    assert m.group(1) == "501"


def test_install_script_gives_the_data_dir_back_to_the_user(captured_install):
    """安装脚本把数据目录归位给那个用户（**重装即自愈**）。

    守护开机先起、目录还不存在时，会建出一个属主是 root 的目录；app 非 root 改不
    回来。安装这一刻是 root、且知道用户是谁 —— 只有这里修得好它。
    """
    assert HC.install() is True
    script = captured_install["script"]
    data_dir = str(paths.APP_SUPPORT)
    assert f'mkdir -p "{data_dir}"' in script
    assert f'chown 501 "{data_dir}"' in script


# ──────────────────────────────────────────────
# 2. 启动期：推不出就不许猜
# ──────────────────────────────────────────────

def _stub_chown(monkeypatch):
    calls = []
    monkeypatch.setattr(core.os, "chown", lambda *a: calls.append(a), raising=False)
    return calls


def test_allowed_uid_prefers_the_plist_value(monkeypatch, tmp_path):
    monkeypatch.setenv("BARKIT_UID", "501")
    monkeypatch.setattr(core, "data_dir", lambda: tmp_path / "还没建")
    assert core.allowed_uid() == 501


def test_allowed_uid_falls_back_to_the_existing_dir_owner(monkeypatch, tmp_path):
    """目录**已经存在**时，属主确实是那个用户（这条兜底本身是对的，保留）。

    ``os.stat`` 打成定值：Windows 的 ``st_uid`` 恒为 0，构造不出「非 root 属主」。
    """
    monkeypatch.delenv("BARKIT_UID", raising=False)
    d = tmp_path / "BarKit"
    d.mkdir()
    monkeypatch.setattr(core, "data_dir", lambda: d)
    monkeypatch.setattr(core.os, "stat", lambda _p: type("S", (), {"st_uid": 501})())
    assert core.allowed_uid() == 501


def test_a_root_owned_dir_is_not_an_answer(monkeypatch, tmp_path):
    """属主是 root ⇒ **不是答案**（多半就是本进程刚建出来的那个目录）。

    报「允许 uid=0」是自证：它读到的正是自己。真出问题时排查的人会照着这条日志
    往完全错的方向走 —— 宁可如实说「不知道」。
    """
    monkeypatch.delenv("BARKIT_UID", raising=False)
    d = tmp_path / "BarKit"
    d.mkdir()
    monkeypatch.setattr(core, "data_dir", lambda: d)
    monkeypatch.setattr(core.os, "stat", lambda _p: type("S", (), {"st_uid": 0})())
    assert core.allowed_uid() is None


def test_new_data_dir_is_not_chowned_to_the_daemon_itself(monkeypatch, tmp_path):
    """目录由守护自己建出来时**不许**把属主推断成自己。

    ``allowed_uid()`` 的兜底是「数据目录的属主」，而刚被本进程建出来的目录属主就是
    root（＝它自己）。拿它当被服务者 ⇒ 整棵数据目录连同套接字被锁成 root:root 0700
    ⇒ app 连不上会话、写不进配置，且**无法自愈**（非 root 无权 chown）。

    反向证伪：把 ``allowed_uid()`` 的调用挪到 ``mkdir`` **之后**，本用例会转红（那时
    读到的属主存在，chown 会被调用）。
    """
    calls = _stub_chown(monkeypatch)
    monkeypatch.delenv("BARKIT_UID", raising=False)
    target = tmp_path / "BarKit"
    monkeypatch.setattr(core, "data_dir", lambda: target)

    core._ensure_data_dir()

    assert target.is_dir(), "目录还是要建出来的（不建则守护连节点都开不出来）"
    assert calls == [], "属主只能说明「建它的是谁」；推不出被服务者时不得 chown"


def test_new_data_dir_is_chowned_when_the_uid_is_known(monkeypatch, tmp_path):
    """拿得到 uid 时照旧归位（上一条不许被理解成「干脆别 chown 了」）。"""
    calls = _stub_chown(monkeypatch)
    monkeypatch.setenv("BARKIT_UID", "501")
    target = tmp_path / "BarKit"
    monkeypatch.setattr(core, "data_dir", lambda: target)

    core._ensure_data_dir()

    assert calls and calls[0][0] == str(target) and calls[0][1] == 501
