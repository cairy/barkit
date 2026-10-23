"""core/resident：长驻资源契约与框架托管宿主（方案 §8.3.1 / 批次 5 后段）。

**原则**：插件只**声明**长驻资源，线程 / 时钟 / 起停 / 销毁归框架。插件目录内
不得出现 ``threading`` / ``QTimer`` / ``QThread`` / ``time.sleep``（门禁
``test_no_private_timer_in_plugins`` 扫描，白名单见 tests/test_architecture.py）。

一个机制两种形态（方案 §3.1）：

- ``interval is None`` → **自持循环型**：``run()`` 自身阻塞（如 select 事件循环），
  直到 ``stop()`` 被调用后尽快返回。宿主线程直接跑 ``run()``。
- ``interval > 0``      → **周期型**：宿主在托管线程里**先等 ``interval`` 秒**
  （等待可被 ``stop()`` 立即打断，也可被 ``wake()`` 提前一拍 ——「现在跑一轮」），
  再 ``poll()`` 一次，循环往复。**首轮在 ``interval`` 之后**——与既有
  ``poll_interval`` 语义一致（插件起完不立刻跑），
  也使「2 票迟滞」的翻转时机与迁移前的 ``threading.Timer`` 完全等价。

节律走**数据声明**（``interval`` 类属性）而非把 ``wait()`` 注入插件：这样插件侧
才能真正做到零时钟/零循环/零 ``threading``，门禁才有意义。

**「框架侧第二套时钟」的挂账：批次 8（2026-09-11）复核后撤销。** 周期型的
``stop_event.wait(interval)`` 曾被记为本模块自持的第二套时钟、拟挂到统一心跳上；
复核结论是**它不是时钟源**——``Event.wait(timeout)`` 没有独立计时器、不累积漂移、
也不重复管理（§8.3.3 列举的四套时钟是 scheduler 的 QTimer 们 / helper 主循环 /
``bridge`` 的 ``threading.Timer`` / 插件私有 ``QFileSystemWatcher``，本模块不在其中；
bridge 那套已在批次 5 后段消灭）。

反过来，改挂心跳会**降低可靠性**：poll 将依赖主线程 Qt 事件循环派发，主线程卡顿
（面板弹出、设置窗渲染、截图识别）会让 relay / probe 停摆；节律还会被量化到心跳
粒度（``MIN_INTERVAL`` 的亚秒语义失效）。故保留自持等待；批次 8 只收归
``Scheduler`` 的每插件 QTimer（见 ``core/heartbeat``）。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from barkit.core.log_throttle import log_throttled

if TYPE_CHECKING:
    from barkit.core.plugin import Plugin

log = logging.getLogger(__name__)

# join 上限：超时记 ERROR 但不停摆（服务线程仍是 daemon，进程退出不受阻）。
JOIN_TIMEOUT = 5.0
# 周期型等待下限：防 interval 误配为 0 造成忙等。
MIN_INTERVAL = 0.01


@runtime_checkable
class ResidentService(Protocol):
    """插件声明的长驻资源。

    硬约定：

    - ``run()`` / ``poll()`` 在**框架托管的非主线程**执行，可做阻塞 IO，但必须对
      ``stop()`` 有界响应（自持型 ≤1s 退出；周期型 ≤ 单次 ``poll()`` 耗时）。
    - 服务内不得 import ``threading`` 或起任何定时器（门禁扫描）。
    - ``stop()`` 幂等、非阻塞，只「请求退出」，不负责 join（join 归宿主）。
    - ``teardown()`` 可选；缺省视为 ``stop()`` 的别名（宿主用 ``getattr`` 探测）。
    - ``report_problem()`` 可选：宿主在托管前会注入一个 ``Callable[[str], None]``，
      服务可在「自己吞掉异常继续跑」的场合把原因上报为**插件问题**（设置页可见），
      从而既不静默失效、也不刷屏（宿主按服务去重）。声明了该属性即会被注入。
    """

    name: str                  # 插件内唯一；日志、幂等、问题定位用
    interval: float | None     # 节律声明（数据，不是时钟）
    def run(self) -> None: ...
    def poll(self) -> None: ...
    def stop(self) -> None: ...


@dataclass
class _Running:
    """一个在托管的服务实例（宿主内部记录）。"""

    plugin_id: str
    name: str
    thread: threading.Thread | None
    stop_event: threading.Event
    periodic: bool
    service: Any = None            # 插件侧声明对象（stop/teardown 回取用）
    failed: str = ""
    problem_reported: bool = False
    #: 「现在跑一轮」的请求（周期型专用，见 :meth:`ResidentHost.wake`）。与
    #: ``stop_event`` **各管一件事**：这个只提前一拍，那个是终止。两者都置位时以
    #: ``stop_event`` 为准（先看终止）。
    wake: threading.Event = field(default_factory=threading.Event)


@dataclass
class _ServiceProblem:
    plugin_id: str
    name: str
    reason: str


class ResidentHost:
    """框架侧托管宿主：拥有线程与时钟，插件只给声明。

    与插件的唯一接口是 ``plugin.resident_services()``（方案 §3.3）：必须**廉价、
    无副作用**，且同一实例上每次调用返回**同一批对象**（宿主按 ``name`` 做差量，
    不重建实例）。
    """

    def __init__(self) -> None:
        self._running: dict[tuple[str, str], _Running] = {}
        self._problems: list[_ServiceProblem] = []
        self._lock = threading.RLock()

    # ---------- 查询 ----------

    def active(self, plugin: "Plugin") -> list[str]:
        """当前在托管的服务名（供状态展示 / 测试）。"""
        pid = getattr(plugin, "id", "")
        with self._lock:
            return sorted(
                name
                for (p, name), rec in self._running.items()
                if p == pid and not rec.failed
            )

    def problems(self) -> list[tuple[str, str]]:
        """服务问题（与 ``Registry.problems()`` 同通道，供「插件问题」展示）。"""
        with self._lock:
            return [(p.plugin_id, f"服务 {p.name} 异常：{p.reason}") for p in self._problems]

    def _record_problem(self, rec: _Running, reason: str) -> None:
        """记服务问题；同一服务只记首次，避免周期型反复失败刷屏。"""
        with self._lock:
            if rec.problem_reported:
                return
            rec.problem_reported = True
            self._problems.append(
                _ServiceProblem(rec.plugin_id, rec.name, reason)
            )

    # ---------- 声明解析 ----------

    def _declare(self, plugin: "Plugin") -> list[ResidentService]:
        fn = getattr(plugin, "resident_services", None)
        if not callable(fn):
            return []
        try:
            items = fn() or []
        except Exception:  # noqa: BLE001 —— 声明抛异常不拖垮插件其余部分
            log.exception("插件 %s resident_services() 抛异常，按空声明处理", getattr(plugin, "id", "?"))
            return []
        out: list[ResidentService] = []
        seen: set[str] = set()
        for svc in items:
            name = getattr(svc, "name", "") or ""
            if not name:
                log.warning("插件 %s 声明的服务缺少 name，忽略", getattr(plugin, "id", "?"))
                continue
            if name in seen:
                log.warning("插件 %s 服务名重复（%s），忽略重复项", getattr(plugin, "id", "?"), name)
                continue
            seen.add(name)
            out.append(svc)
        return out

    # ---------- 托管 ----------

    def start(self, plugin: "Plugin") -> None:
        """起该插件声明的全部服务（幂等：已在托管则跳过）。逐个 isolate。"""
        for svc in self._declare(plugin):
            key = (getattr(plugin, "id", ""), svc.name)
            with self._lock:
                if key in self._running:
                    continue
            self._spawn(plugin, svc)

    def stop(self, plugin: "Plugin") -> None:
        """停该插件的全部服务：``stop()`` → 唤醒等待 → ``join(timeout)``。"""
        pid = getattr(plugin, "id", "")
        with self._lock:
            keys = [k for k in self._running if k[0] == pid]
        for key in keys:
            self._stop_one(key)

    def teardown(self, plugin: "Plugin") -> None:
        """卸载语义：``service.teardown()``（缺省等价 ``stop()``）→ 丢弃记录。"""
        pid = getattr(plugin, "id", "")
        with self._lock:
            records = [(k, r) for k, r in self._running.items() if k[0] == pid]
        for key, rec in records:
            svc = rec.service
            teardown = getattr(svc, "teardown", None)
            if callable(teardown):
                try:
                    teardown()
                except Exception:  # noqa: BLE001
                    log.exception("插件 %s 服务 %s teardown 异常", pid, rec.name)
        self.stop(plugin)
        with self._lock:
            for key, _rec in records:
                self._running.pop(key, None)

    def sync(self, plugin: "Plugin") -> None:
        """声明差量收敛：启动缺失的、停掉多余的（按 ``name``）。

        ``resync_services()``（插件开关翻转后请求重算）的落地实现。声明未变时
        等价空操作，故可廉价反复调用。
        """
        pid = getattr(plugin, "id", "")
        declared = {svc.name: svc for svc in self._declare(plugin)}
        with self._lock:
            running = {name for (p, name) in self._running if p == pid}
        for name in running - set(declared):
            self._stop_one((pid, name))
        for name, svc in declared.items():
            with self._lock:
                if (pid, name) in self._running:
                    continue
            self._spawn(plugin, svc)

    def wake(self, plugin_id: str, name: str = "") -> int:
        """请求某插件的**周期型**服务现在就跑一轮（不必等这一拍走完）。

        语义是「**提前一拍**」，不是「插队并行」：服务线程若正在 ``poll()``，这次请求
        落在它跑完后的下一次等待上（每次唤醒只提前一拍、不累积）。这与幂等的 ``poll()``
        天然相容 —— 宿主**不保证合并**多次唤醒，故调用方不要把它当成「计数器」。

        返回被唤醒的服务数：``0`` ＝ 该插件没有匹配的**在托管的周期型**服务（组件被
        禁用 / 服务已熔断 / 名字写错 / 它是自持型）。**不是错误** —— 调用方据此退化。

        自持型（``interval is None``）不受理：它的 ``run()`` 是自己的循环，「跑一轮」
        没有对应物。
        """
        count = 0
        with self._lock:
            records = list(self._running.items())
        for (pid, svc_name), rec in records:
            if pid != plugin_id or (name and svc_name != name):
                continue
            if not rec.periodic or rec.failed:
                continue
            rec.wake.set()
            count += 1
        return count

    # ---------- 内部：起停一个服务 ----------

    def _spawn(self, plugin: "Plugin", svc: ResidentService) -> None:
        pid = getattr(plugin, "id", "") or "?"
        key = (pid, svc.name)
        periodic = svc.interval is not None
        rec = _Running(
            plugin_id=pid,
            name=svc.name,
            thread=None,
            stop_event=threading.Event(),
            periodic=periodic,
            service=svc,
        )
        # 可选的问题上报注入：服务「吞掉异常继续跑」时用它把原因变成可见的插件问题
        if hasattr(svc, "report_problem"):
            try:
                svc.report_problem = partial(self._record_problem, rec)
            except Exception:  # noqa: BLE001 —— 注入失败不影响托管
                log.exception("resident 服务 %s.%s 的问题上报注入失败", pid, svc.name)
        target = self._loop_periodic if periodic else self._loop_self_sustaining
        th = threading.Thread(
            target=target,
            args=(rec, svc),
            name=f"resident:{pid}.{svc.name}",
            daemon=True,
        )
        rec.thread = th
        with self._lock:
            self._running[key] = rec
        th.start()
        log.info("resident 服务已启动：%s（%s）", f"{pid}.{svc.name}",
                 "周期型" if periodic else "自持型")

    def _stop_one(self, key: tuple[str, str]) -> None:
        with self._lock:
            rec = self._running.get(key)
        if rec is None:
            return
        svc = rec.service
        rec.stop_event.set()
        # 唤醒必须**与终止同时**置位：周期型等的是 ``wake``（另一枚独立事件），只置
        # ``stop_event`` 会让它白等完这一拍 interval —— 超过 ``JOIN_TIMEOUT`` 就是一条
        # 假的「未在 N 秒内退出」。顺序（先 stop 后 wake）不敏感：循环体先看 stop。
        rec.wake.set()
        stop = getattr(svc, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:  # noqa: BLE001
                log.exception("resident 服务 %s.%s stop() 异常", rec.plugin_id, rec.name)
        if rec.thread is not None and rec.thread.is_alive():
            rec.thread.join(timeout=JOIN_TIMEOUT)
            if rec.thread.is_alive():
                log.error("resident 服务 %s.%s 未在 %.0fs 内退出",
                          rec.plugin_id, rec.name, JOIN_TIMEOUT)
                self._record_problem(rec, f"stop 后 {JOIN_TIMEOUT:.0f}s 内未退出")
        with self._lock:
            self._running.pop(key, None)

    # ---------- 内部：两种运行形态 ----------

    def _loop_self_sustaining(self, rec: _Running, svc: ResidentService) -> None:
        """自持循环型：直接跑 ``run()``；异常 → 记问题 + 标记 failed（不重启）。"""
        try:
            svc.run()
        except Exception as exc:  # noqa: BLE001
            log.exception("resident 服务 %s.%s run() 异常退出", rec.plugin_id, rec.name)
            rec.failed = str(exc) or "run() 异常"
            self._record_problem(rec, rec.failed)

    def _loop_periodic(self, rec: _Running, svc: ResidentService) -> None:
        """周期型：可打断地等 ``interval`` → ``poll()`` → 循环。首轮在 interval 之后。

        等待**两路可打断**：到期（``wake.wait`` 返回 False）、有人请求「现在跑一轮」
        （:meth:`ResidentHost.wake`，返回 True）。``stop_event`` 不参与等待、只在每次
        醒来时判定 —— 但 ``_stop_one`` 会连 ``wake`` 一起置位，故终止同样是**立刻**的。
        """
        try:
            interval = max(float(svc.interval or 0.0), MIN_INTERVAL)
        except (TypeError, ValueError):
            log.warning("resident 服务 %s.%s 的 interval 非法，回退 %.2fs",
                        rec.plugin_id, rec.name, MIN_INTERVAL)
            interval = MIN_INTERVAL
        while True:
            rec.wake.wait(interval)
            if rec.stop_event.is_set():
                return
            rec.wake.clear()
            try:
                svc.poll()
            except Exception as exc:  # noqa: BLE001 —— 单次失败不退出循环
                # 同因只报一次：服务坏掉时 poll 按它自己的 interval 一直抛（最短 3s），
                # 逐条带 traceback 会把日志刷满、把别处的现场挤掉。首条仍带完整堆栈。
                log_throttled(log, logging.ERROR,
                              "resident 服务 %s.%s poll() 异常", rec.plugin_id, rec.name,
                              exc_info=True)
                self._record_problem(rec, str(exc) or "poll() 异常")

    # ---------- 调试辅助 ----------

    def thread_names(self) -> list[str]:
        """当前托管线程名（真机排查用；沙箱测试断言命名规范）。"""
        with self._lock:
            return sorted(r.thread.name for r in self._running.values() if r.thread is not None)


__all__ = ["ResidentService", "ResidentHost", "JOIN_TIMEOUT", "MIN_INTERVAL"]
