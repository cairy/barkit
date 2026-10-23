"""core/proc 测试（2026-08 加固：进程组收尸）。"""

import subprocess
import time

import pytest

from barkit.core.proc import run_cmd


def test_run_cmd_timeout_kills_process_group():
    """超时后 killpg 整组，不残留子进程（防孤儿）。"""
    t0 = time.time()
    with pytest.raises(subprocess.TimeoutExpired):
        run_cmd(["sleep", "30"], timeout=1)
    assert time.time() - t0 < 5
    # 子进程组已被清除：不应再有 sleep 30 进程
    r = subprocess.run(["pgrep", "-f", "^sleep 30$"], capture_output=True, text=True)
    assert r.returncode != 0, "超时后残留子进程"


def test_run_cmd_normal():
    r = run_cmd(["echo", "hi"], timeout=5)
    assert r.returncode == 0 and r.stdout.strip() == "hi"


def test_run_cmd_rejects_shell():
    # 必须是运行期 TypeError（不是 assert）：``python -O`` 下 assert 被剥离，
    # 若用 assert 护栏会静默消失，字符串命令一路走到 subprocess。
    with pytest.raises(TypeError):
        run_cmd("echo hi; rm -rf /")  # type: ignore[arg-type]


def test_run_cmd_rejects_non_string_args():
    """参数元素也必须是字符串，杜绝把一段命令当 argv 逐字符喂进去。"""
    with pytest.raises(TypeError):
        run_cmd(["echo", 123])  # type: ignore[list-item]
