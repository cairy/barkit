"""core/hotkey_command：自建快捷键「跑一条命令」的执行口径。

**为什么 ``shell=False`` + :func:`shlex.split`**：命令来自用户在设置页里敲的一行字，
走 shell 就等于把 ``;`` ``&&`` ``$(...)`` 一并交给一个字符串解释器 —— 那跑的不是
「一条命令」而是**一段脚本**，而这段脚本的唯一来源是一个没有任何审计的输入框。
argv 形态顺带回答了「参数里带空格怎么办」（引号切分），代价是不支持管道与重定向
（真要写脚本，就让它去跑一个脚本文件，而不是把脚本塞进这一格）。

**这里不代跑 root**：系统守护的能力集是编译期封闭的
（``capabilities/helper/actions.py`` 里明令禁止任何 exec/command/shell 形态的原语），
root 侧不接受从配置里读出来的命令。本文件的进程一律以**当前用户**权限跑 ——
需要 root 的动作必须在守护里加具名原语，不能从这里绕过去。

**为什么要有超时**：热键按下去，用户不会等；一条挂住的命令会把整个 app 拖住
（见 :class:`CommandRunner` —— 它把执行挪出主线程，但「挂住」本身仍要有上限）。

**两条「不等它」的路，别混用**：热键那条用 :class:`CommandRunner`（QThread）——
它要把结局送**回界面**，所以信号走队列，**得有 Qt 事件循环**才收得到。
:func:`fire` 是给「只要结局落地、不要界面」的调用方（待办提醒的「到点执行」）用的：
普通线程、``on_result`` 在那条线程里**直接**被调 ⇒ 没有 Qt 的场合也跑得完。
调用方各自的线程一律不许自己起（``tests/test_architecture.py`` 的
``_plugin_timer_hits``：插件不得私起线程），线程归**这一处**。
"""

from __future__ import annotations

import logging
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QThread, Signal

log = logging.getLogger(__name__)

#: 单条命令的时间上限（秒）。取「一条快捷键该有多快」的上界：打开一个 app、
#: 敲一段文本都在 1 秒内；取 10 是为了容留出师未捷的启动（冷启动 / 磁盘唤醒）。
TIMEOUT_SEC = 10

#: 输出保留上限（字符）。命令输出可能是几 MB，而它的去处是浮层 / 通知 / 编辑屏里
#: 一小块结果区 —— 不留上限就是拿整段输出去撑一个只能显示三行的框。
MAX_OUTPUT = 2000

_TRUNCATED_HINT = "\n…（输出过长，只显示前面这段）"

#: :func:`fire` 那条线程的名字。诊断与用例按它认「活确实挪出了主线程」。
FIRE_THREAD_NAME = "hotkey-command"


@dataclass(frozen=True)
class CommandResult:
    """一次执行的结果。**进程起来了但退出码非 0 也走 ``ok=False``** —— 对用户
    而言「跑了，失败了」与「没跑起来」是两件事，前者要看输出、后者要看原因。"""

    ok: bool
    exit_code: "int | None"   # None = 进程没起来（找不到 / 没权限 / 超时）
    duration_ms: int
    output: str = ""
    reason: str = ""          # 没起来时的**人话**原因

    @property
    def started(self) -> bool:
        """进程到底起没起来（超时算起来了 —— 它跑过，只是没跑完）。"""
        return self.exit_code is not None

    @property
    def headline(self) -> str:
        """一句结论，用在通知标题 / 浮层首行。"""
        if not self.started:
            return self.reason or "没能跑起来"
        if self.ok:
            return f"已完成（{self.duration_ms} 毫秒）"
        return f"失败：退出码 {self.exit_code}（{self.duration_ms} 毫秒）"


def split_command(command: str) -> "list[str] | None":
    """一行命令 → argv；**空行或引号不配对给 None**（不是空列表）。

    返回 None 而不是 ``[]``：空列表在 ``subprocess`` 那边是「跑一个空命令」，
    而这里要表达的是「这条没法跑」，两种结局完全不同。
    """
    text = (command or "").strip()
    if not text:
        return None
    try:
        argv = shlex.split(text)
    except ValueError:
        return None          # 引号不配对
    return argv or None


def execute(command: str, timeout: float = TIMEOUT_SEC, cwd: "str | None" = None) -> CommandResult:
    """跑一条命令并**把结果整个带回来**（不抛、不打印、不弹窗）。

    工作目录默认用户家目录：相对路径至少有一个确定的落点，不会因为 app 是从
    Finder 还是从终端启动而变（那种「在我这儿能跑、双击就找不到文件」最难查）。
    """
    argv = split_command(command)
    if argv is None:
        return CommandResult(False, None, 0, reason="命令为空或引号不配对")

    started = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603 —— argv 固定、shell 恒 False，见模块头
            argv,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=cwd or str(Path.home()),
        )
    except FileNotFoundError:
        return CommandResult(False, None, _ms(started), reason=f"找不到这个程序：{argv[0]}")
    except PermissionError:
        return CommandResult(False, None, _ms(started), reason=f"没有执行权限：{argv[0]}")
    except subprocess.TimeoutExpired:
        return CommandResult(False, None, _ms(started),
                             reason=f"超过 {int(timeout)} 秒还没跑完，已中止")
    except OSError as exc:
        return CommandResult(False, None, _ms(started), reason=f"起不来进程：{exc}")

    text = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if len(text) > MAX_OUTPUT:
        text = text[:MAX_OUTPUT] + _TRUNCATED_HINT
    return CommandResult(
        ok=proc.returncode == 0,
        exit_code=proc.returncode,
        duration_ms=_ms(started),
        output=text,
    )


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def fire(command: str, on_result=None, timeout: float = TIMEOUT_SEC) -> bool:
    """跑一条命令，**不等它**；结局（:class:`CommandResult`）交给 ``on_result``。

    ``on_result`` 在**那条线程里直接被调**（不是 Qt 信号）⇒ 不需要事件循环，
    没有 Qt 的场合照样跑得完；正因为如此，它只该做「记一笔」这类就地能做完的事，
    别在里面碰界面（要碰界面的调用方用 :class:`CommandRunner`）。

    **切不开的命令也照跑一次** —— 那条路会得到一条 ``started=False`` 的结果
    （理由写在 :class:`CommandResult.reason`），调用方不必自己再判一遍
    :func:`split_command`。返回 ``False`` 只有两种情形：命令是空串 / 线程起不来，
    两种都**不会**调 ``on_result``。
    """
    if not (command or "").strip():
        return False
    try:
        threading.Thread(
            target=_fire_run,
            args=(command, timeout, on_result),
            name=FIRE_THREAD_NAME,
            daemon=True,
        ).start()
    except Exception:  # noqa: BLE001 —— 起不了线程是「这条没跑」，不是调用点炸掉
        log.exception("起不了线程，这条命令没跑：%s", command)
        return False
    return True


def _fire_run(command: str, timeout: float, on_result) -> None:
    """``fire`` 那条线程的正文。回调异常**就地**隔离：它是旁路动作，
    抛出去只会在那条线程里变成一条无人收的 traceback。"""
    result = execute(command, timeout)
    if on_result is None:
        return
    try:
        on_result(result)
    except Exception:  # noqa: BLE001
        log.exception("命令结局的回调异常：%s", command)


class CommandRunner(QThread):
    """把 :func:`execute` 挪出主线程。

    热键回调与「测试」按钮都跑在主线程上 —— 同步执行一次命令，最坏是**整个界面
    卡住 10 秒**（测试按钮按下去窗子不动，最像「程序死了」）。线程是唯一解：
    不能缩短超时了事，那条命令本身可能就是要跑几秒的。
    """

    result = Signal(object)   # CommandResult

    def __init__(self, command: str, timeout: float = TIMEOUT_SEC, parent=None) -> None:
        super().__init__(parent)
        self._command = command
        self._timeout = timeout

    def run(self) -> None:  # type: ignore[override]
        self.result.emit(execute(self._command, self._timeout))
