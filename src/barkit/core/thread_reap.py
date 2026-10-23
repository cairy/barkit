"""core/thread_reap：``QThread`` 的**安全收尾**。

起因（2026-09-29 真机崩溃，报告 ``~/Library/Logs/DiagnosticReports/
Python-2026-09-29-155751.ips``：``SIGABRT`` / ``abort() called``，触发线程名
``_DiagWorker``）：隧道诊断那条线程在关窗时用 ``quit()`` + ``wait(1000)`` 收尾，
一秒后就把引用丢了 —— 线程还在跑，``~QThread()`` 随即执行，Qt 直接 ``qFatal``
把**整个进程** abort 掉，不是抛异常、不是某次回调失败。

收一条线程要**两件都成**，缺一件就崩：

1. **让它停** —— ``requestInterruption()``，且 ``run()`` 里自己查
   ``isInterruptionRequested()``；
2. **别在它还跑着的时候析构它** —— 等不到就**代持**，等 ``finished`` 到了再放手。

**``quit()`` 在这里一律无效**：它只退出事件循环，而本项目这两条线程的 ``run()``
是**顺序跑命令**、根本没起事件循环。所以这里不用 ``quit()``。

**``QThread`` 不许给短命父对象**：父对象销毁时会连带析构子对象，命中同一条
``qFatal``。所以要交给本模块代持，而不是「给它挂个 parent 就有人管了」。

从工作线程改 ``_LINGERING`` 这个 set 是安全的（``add`` / ``discard`` 在 GIL 下是
单步操作），不必加锁。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QThread

if TYPE_CHECKING:  # pragma: no cover —— 只给类型检查用，运行期不引入
    from collections.abc import Callable

#: 收尾时最多把**主线程**按住多久（毫秒）。刻意取小：够「正在查中断标志的那一步
#: 走完」即可；不够说明它正卡在一条外部命令上 —— 那就交给后台慢慢跑完，绝不为了
#: 收一条线程把界面按在那里。
GRACE_MS = 300

#: 代持中的线程：要么还在跑、要么刚被要求停还没停，两种情况下**都不能**被析构。
_LINGERING: set[QThread] = set()


def own(worker: QThread, on_finish: "Callable[[], None] | None" = None) -> QThread:
    """把线程**交给本模块代持**：从此调用方**不需要**持有引用，也别给它挂短命 parent。

    代持期间它不会被析构；线程结束时本模块断开自己挂的连接并 ``deleteLater()``。
    这样「页面先没了、线程还在跑」这种时序就只是泄漏一条线程一小会儿，而不是崩进程。
    """
    if worker is None:
        return worker
    if worker not in _LINGERING:
        _LINGERING.add(worker)
        worker.finished.connect(lambda *_a: _release(worker, on_finish))
    return worker


def stop(worker: QThread, grace_ms: int = GRACE_MS) -> bool:
    """要求一条线程停下。

    - 返回 ``True``：**已经停了**（或压根没起过），调用方可以 ``deleteLater()``；
    - 返回 ``False``：宽限期里没停住（正卡在一条外部命令上）⇒ 已转代持，
      **调用方此刻绝不能丢引用** —— 那正是这次崩溃的成因。代持方会在 ``finished``
      到达时回收它。

    调用方仍要在调它**之前**自己摘掉指向即将销毁对象的槽（本模块不知道谁连了什么）。
    """
    if worker is None:
        return True
    if not worker.isRunning():
        return True
    worker.requestInterruption()
    if worker.wait(grace_ms):
        return True
    own(worker)
    return False


def lingering() -> frozenset:
    """还在代持的线程集合（诊断 / 测试用）。"""
    return frozenset(_LINGERING)


def _release(worker: QThread, on_finish: "Callable[[], None] | None" = None) -> None:
    """线程自己结束了 ⇒ **此时**析构才安全。"""
    if on_finish is not None:
        try:
            on_finish()
        except Exception:  # noqa: BLE001 —— 回调失败不该拖住回收
            pass
    if worker in _LINGERING:
        _LINGERING.discard(worker)
        worker.deleteLater()
