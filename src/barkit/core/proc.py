"""core/proc：统一 subprocess 封装——强制 timeout、进程组收尸、禁止 shell 拼接。

2026-08 加固：子进程放入独立进程组（start_new_session），超时后 killpg 整组，
防止 system_profiler / launchctl 等命令派生孙进程后，直接子进程被杀而孙进程
残留孤儿（这是 notify_guard 卡死事故的诱因之一）。
"""

from __future__ import annotations

import os
import signal
import subprocess

DEFAULT_TIMEOUT = 10  # 秒，所有 subprocess 调用的强制上限


def _kill_process_group(exc: subprocess.TimeoutExpired) -> None:
    """超时后杀掉子进程所在进程组（start_new_session 保证其独立成组）。"""
    pid = getattr(exc, "pid", None)
    if not pid:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def run_cmd(
    args: list[str],
    timeout: int = DEFAULT_TIMEOUT,
    kill_pg: bool = True,
    **kwargs,
) -> subprocess.CompletedProcess:
    """参数化执行命令；args 必须是列表，禁止 shell=True。

    kill_pg=True（默认）：子进程独立进程组，超时后 killpg 清组（防孤儿）。
    调用方可传 kill_pg=False 关闭（极少需要）。
    """
    # 不能写 ``assert``：``python -O`` / ``PYTHONOPTIMIZE=1`` 下 assert 不生成字节码，
    # 护栏静默消失后字符串命令会一路走到 subprocess。这里必须是运行期抛异常。
    if not isinstance(args, (list, tuple)):
        raise TypeError("run_cmd 只接受参数列表，禁止 shell 拼接")
    if not all(isinstance(a, str) for a in args):
        raise TypeError("run_cmd 的参数必须是字符串，禁止 shell 拼接")
    if kill_pg:
        kwargs.setdefault("start_new_session", True)
    try:
        return subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, **kwargs
        )
    except subprocess.TimeoutExpired as e:
        if kill_pg:
            _kill_process_group(e)
        raise
