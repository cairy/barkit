"""tests/test_hotkey_command：自建快捷键「跑一条命令」的执行口径（2026-09-23 新增）。

覆盖的是**边界**，不是 subprocess 本身：切分（空 / 引号不配对 / 管道不被解释）、
三种起不来的原因、非零退出码算失败、超长输出要截断、那条最要紧的红线 ——
**不走 shell**，以及 ``fire``（跑一条命令但不等它）。
"""

from __future__ import annotations

import inspect
import sys
import threading

from pathlib import Path

import pytest

from barkit.core import hotkey_command as mod
from barkit.core.hotkey_command import (
    FIRE_THREAD_NAME,
    MAX_OUTPUT,
    CommandResult,
    execute,
    split_command,
)

#: 用正斜杠形式：``shlex`` 按 POSIX 语义切分，Windows 的 ``C:\Users\…`` 会被它
#: 当成转义序列吃掉反斜杠（本仓面向 macOS，切分语义就按 macOS 那一套，不改）。
_PY = Path(sys.executable).as_posix()


def test_split_returns_none_for_empty_or_unbalanced_quotes():
    """空行与引号不配对都不能跑 —— 两种都表达「这条没法跑」，不是「跑一个空命令」。"""
    assert split_command("") is None
    assert split_command("   ") is None
    assert split_command('open "未闭合') is None


def test_split_keeps_quoted_arguments_together():
    argv = split_command('/usr/bin/open -a "日历 App"')
    assert argv == ["/usr/bin/open", "-a", "日历 App"]


def test_pipes_are_not_interpreted():
    """不支持管道 / 重定向是**刻意**的：走 shell 就是把整段脚本交给一个输入框。

    ``|`` 在这里只是一个普通参数，切完仍在 argv 里 —— 真正去跑时它会落到
    「找不到这个程序：|」那条路上，而不是变成一条管道。
    """
    assert split_command("cat a | grep b") == ["cat", "a", "|", "grep", "b"]


def test_execute_reports_success_with_output():
    result = execute(f'{_PY} -c "print(1+1)"')
    assert result.ok and result.exit_code == 0
    assert "2" in result.output
    assert result.duration_ms >= 0


def test_nonzero_exit_is_a_failure_but_the_process_did_start():
    """「跑了，失败了」与「没跑起来」是两件事：前者要看输出，后者要看原因。"""
    result = execute(f'{_PY} -c "import sys; sys.exit(3)"')
    assert result.started and not result.ok
    assert result.exit_code == 3
    assert "退出码 3" in result.headline


def test_missing_program_is_reported_in_plain_words():
    result = execute("/definitely/not/a/program")
    assert not result.started and not result.ok
    assert "找不到" in result.reason


def test_timeout_aborts_and_says_so():
    result = execute(f'{_PY} -c "import time; time.sleep(5)"', timeout=0.5)
    assert not result.ok
    assert "中止" in result.reason


def test_long_output_is_truncated_with_a_hint():
    result = execute(f'{_PY} -c "print(\'x\' * {MAX_OUTPUT + 500})"')
    assert "输出过长" in result.output
    assert len(result.output) < MAX_OUTPUT + 100


def test_the_module_never_asks_for_a_shell():
    """红线自证：**执行方式参数化**要求 argv 形态，``shell=True`` 在本模块里
    不该出现（它来自用户在设置页里敲的一行字，见模块头）。用源码扫而不是靠人记。"""
    assert "shell=True" not in inspect.getsource(mod)
    assert "shell=False" in inspect.getsource(mod)


# ── fire：跑一条命令但不等它 ──────────────────────────────────────────────


def test_fire_returns_before_the_command_finishes(monkeypatch):
    """``fire`` 只**收下**就返回 —— 真跑发生在另一条线程上。

    判据不是「有没有起线程」这种实现细节，而是**调用点会不会被那条命令堵住**：
    真跑那一半被换成「堵住直到本用例放行」，而 ``fire`` 必须在它还堵着的时候就返回了。
    同步跑的写法在这一条上会超时 —— 那正是「提醒一弹出来，界面卡十秒」的成因。
    """
    started = threading.Event()
    released = threading.Event()
    seen: "list[str]" = []

    def _slow(command, timeout=None):
        seen.append(f"{threading.current_thread().name} {command}")
        started.set()
        released.wait(5.0)          # 真跑一条命令要多久就多久，这里堵住当哨兵
        return CommandResult(True, 0, 1)

    monkeypatch.setattr(mod, "execute", _slow)
    try:
        assert mod.fire("open https://example.com") is True, "收下就该返回 True"
        assert started.wait(2.0), "工作线程该真的跑起来"
        assert seen == [f"{FIRE_THREAD_NAME} open https://example.com"], seen
    finally:
        released.set()


def test_fire_hands_the_result_back_on_that_thread(monkeypatch):
    """结局交回 ``on_result``，且**就在那条线程里**被调（不是 Qt 信号）。

    这一条是待办那条路能不用 Qt 的原因 —— 信号走队列就要有事件循环，而这条命令的
    产物只是日志，不该把「有没有事件循环」变成能不能跑的前提。
    """
    done = threading.Event()
    got: "list[tuple[str, CommandResult]]" = []
    canned = CommandResult(False, 3, 7, output="boom")

    monkeypatch.setattr(mod, "execute", lambda command, timeout=None: canned)

    def _collect(result):
        got.append((threading.current_thread().name, result))
        done.set()

    assert mod.fire("/usr/bin/whatever", _collect) is True
    assert done.wait(5.0), "回调该被调到"
    assert got == [(FIRE_THREAD_NAME, canned)], got


def test_fire_refuses_an_empty_command_without_doing_anything(monkeypatch):
    """空串不起线程、不调回调 —— 「没配命令」不是失败，但也不该白跑一条空线程。"""
    monkeypatch.setattr(mod, "execute", lambda *_a, **_k: pytest.fail("空命令不该被执行"))
    assert mod.fire("") is False
    assert mod.fire("   ") is False
    assert mod.fire(None) is False


def test_a_broken_result_callback_does_not_escape(monkeypatch, caplog):
    """回调抛异常：就地咽掉并记一条 —— 让它抛出去只会在那条线程里变成无人收的
    traceback（真跑那个进程的结局反而没人知道）。

    直接调线程正文（``_fire_run``）而不真起线程：**判据是「抛不抛」与「记没记」，
    而这两件事都在异常被咽下的那几行里**。真起线程的话，「回调已返回」这个信号发生
    在 ``log.exception`` **之前** —— 用例会跑在日志落笔前，红得毫无规律。
    """
    def _boom(_result):
        raise RuntimeError("回调坏了")

    monkeypatch.setattr(mod, "execute", lambda command, timeout=None: CommandResult(True, 0, 1))
    with caplog.at_level("ERROR", logger="barkit.core.hotkey_command"):
        mod._fire_run("/bin/echo hi", mod.TIMEOUT_SEC, _boom)   # 不抛就是通过
    assert any("回调异常" in r.message for r in caplog.records), caplog.records


def test_fire_cannot_be_fooled_by_an_unsplittable_command():
    """切不开的命令也照跑一次：拿到的是「没跑起来 ＋ 说得出理由」，不是静默。

    「这一层不判切得开吗」是**故意的** —— 判据只该有一处（``split_command``，在
    ``execute`` 里），调用方不必自己再判一遍；真判的话两边一旦分家，这边就会把
    一条明明能跑的命令挡掉，而界面上看不出原因。
    """
    done = threading.Event()
    got: "list[CommandResult]" = []
    assert mod.fire('open "https://x', lambda r: (got.append(r), done.set())) is True
    assert done.wait(5.0)
    assert got and not got[0].started and "引号" in got[0].reason, got

