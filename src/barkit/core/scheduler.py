"""core/scheduler：统一轮询调度（方案 §3.4，2026-08 加固；批次 8 收归时钟）。

可靠性设计（根因：tick 卡死子进程 → 共享线程池被占 + _running 永久占用
→ 后续 tick 静默跳过 → 面板显示陈旧状态且无告警）：

- 线程池隔离：每个 daemon 插件独立 QThreadPool(maxThreadCount=1)，
  一个插件 tick 卡死不占其他插件 worker（卡死不连坐）。
- 时钟来源（批次 8，§8.3.3 层 1+2）：不再每插件一个 QTimer，改由
  ``Heartbeat`` 单一心跳驱动；相位经 ``phase_for(plugin.id, poll)`` 错开，
  同 poll 的插件不 lockstep。**「启动即跑一轮」保留**（首屏零陈旧）——
  相位只作用于首轮之后的节拍（见 ``Heartbeat.add_ticker``）。
- 心跳：单一 ``Heartbeat`` 驱动全部 tick（相位经 ``phase_for`` 错开），
  tick 成功与否只落到 ``status()`` 的 ``fail_count`` / ``broken`` / ``dirty``。
  **本类不记录「上次成功时刻」** —— 「守卫失联」按**状态 topic 多久没收到
  事件**判（阈值由插件 ``stale_after`` 给），与「tick 有没有跑通」是两个量。
- 按需点火（批次 10，§8.2.2）：事件 handler 只调 ``mark_dirty()`` **标记脏**，
  实际工作交本类在 worker 线程跑一轮 ``tick()``——合并窗口内多次标记归并为一轮
  （事件风暴天然合并）；目标**在跑**时保留脏标记、跑完补点火（不并跑、不丢事件），
  **已熔断**时丢弃（解熔后的 ``retry`` 本就无条件点火）。
  事件路径与轮询路径由此汇入同一执行入口。（``resident`` 服务型资源不走这里，
  它们不经 ``tick``。）
- **按需型也登记**（2026-09-22）：``RUN_ON_DEMAND`` 组件不订阅节拍，但必须进
  ``_plugins`` 才能被 ``mark_dirty`` / ``retry`` 点火 —— 此前它在 ``add_plugin``
  就被挡回去，于是事件 handler 里那句 ``mark_dirty`` 静默落空（``mark_dirty`` 对
  未知 id 是**有意**静默的），「只靠系统事件干活」的组件因此只能谎报成周期型。
- 僵尸 tick 回收：_on_beat 时检查 _running 中是否存在超龄 tick
  （> TICK_TIMEOUT 未完成），存在则强制清 _running 并记 ERROR——
  watchdog（singleShot）失效时的第二重保险。
- 熔断自愈：execute 类连续失败达阈值熔断后自动重试，无需手动"重试"；手动
  retry 仍然有效。间隔按**熔断轮次**指数退避（:func:`retry_delay_for`，上沿
  ``RETRY_DELAY_MAX``），只有一次**成功** tick 会把退避归零 —— 长故障下不再
  按固定 30s 无谓地撞，故障结束后也不必干等上一轮攒下的长间隔。
- 崩溃隔离内置：tick 包 try/except 记日志；execute 类连续失败达阈值熔断，
  observe 类只记日志不熔断。
- tick 级 watchdog：超时按失败处理并递增代号，迟到的结果丢弃（防卡住耗尽线程池）。

**本类不含全局并发闸门**（批次 8 裁决，对 §8.3.3 层 3 的偏离已记入执行计划
并附理由）：全局「最多 2 个 tick 在跑」与上面的「卡死不连坐」互斥——一个卡死
tick 占住名额会把其余插件拖到 ``TICK_TIMEOUT`` 之后；且会挡掉 Qt 事件循环
启动前的首轮同步点火（届时是首屏数据的唯一来源）。防惊群由相位错开承担。
"""

from __future__ import annotations

import logging
import time
from functools import partial

from PySide6.QtCore import QObject, QRunnable, QTimer, QThreadPool, Signal

from barkit.contribution.specs import StateReport
from barkit.core.events import EventBus
from barkit.core.heartbeat import Heartbeat, Ticker
from barkit.core.plugin import FAIL_BREAK, RUN_PERIODIC, Plugin

log = logging.getLogger(__name__)

DEFAULT_POLL = 30       # 秒。**兜底值**：仅用于「``RUN_PERIODIC`` 却没声明
                        # ``suggested_poll``」的组件——本仓 4 个周期组件均已声明（契约
                        # D21），故它只在第三方插件漏填时生效，且那一刻会记 warning。
FAIL_THRESHOLD = 5      # FAIL_BREAK 类熔断阈值（连续失败次数）
TICK_TIMEOUT = 60       # 秒，tick watchdog（覆盖内部 subprocess 10s 上限后的余量）
RETRY_DELAY = 30        # 秒，**首次**熔断后的自动重试间隔
#: 指数退避底数：第 N 次熔断的间隔 = ``RETRY_DELAY * RETRY_BACKOFF ** (N-1)``。
#: 理由：熔断的成因大多是外部条件（helper 没装 / 权限没给 / 服务没起），
#: 固定间隔会在故障期间按同一节奏反复撞同一堵墙 —— 而那一撞要跑一整轮 tick
#: （含子进程），是真开销。退避把「试一下」的成本按故障时长摊薄。
RETRY_BACKOFF = 2
#: 退避上沿（秒）：再长的故障也不必等过 5 分钟才试一次 —— 超过这个量级，
#: 「早点发现它回来了」比「少跑几轮」更值钱；且上沿保证退避收敛，不会溢出成
#: 一个永远等不到的定时器。
RETRY_DELAY_MAX = 300
#: 「标记脏」合并窗口（§8.2.2）：窗内多次 mark_dirty 只点火一轮。取值兼顾两点——
#: 大到足够把**同一瞬间到达**的多个标记合成一次点火（一条推送含多个 subject /
#: 多状态插件逐 key 变更），小到让「面板秒级刷新」的观感不受影响。
#: （2026-09-30 改注：原理由「吃掉 helper 写临时文件 + rename 的 2 次目录变化」
#: 随文件通道下线而失效；窗口值不变。）
DIRTY_WINDOW_MS = 50


def poll_default_of(declared, run_mode: str, plugin_id: str) -> float:
    """框架节拍的**默认值**（秒）：组件建议合法则用它，否则回落 ``DEFAULT_POLL``。

    取值顺序的口径（别改成读配置——配置的读点在 :meth:`Scheduler._interval_for`）：

    - 组件声明了合法值（正整数）→ 用它；
    - 没声明 / 声明非法 → 记 warning 后回落 ``DEFAULT_POLL``（**不静默空转**：
      漏填是作者写错了，日志里必须留痕，否则「为什么我的插件 30s 才跑一次」
      查不到任何线索）。

    **用户覆盖仍优先**：本函数只给默认值，调用方必须以
    ``plugin_config().get("poll", <本函数>)`` 的形态读取——用户配置永远压过
    组件建议（D21：组件给的是建议，不是命令）。

    为什么认**三个纯数据**而不是收一个 ``Plugin``：设置窗手上只有**类属性**
    （``discovered_classes`` 里那些没实例化的组件也要能画控件），拿不出实例。
    判据只此一份，两侧问同一个函数——否则「界面显示的默认」与「调度器用的默认」
    就会各算一遍、迟早分叉。
    """
    if isinstance(declared, int) and not isinstance(declared, bool) and declared > 0:
        return float(declared)
    if run_mode == RUN_PERIODIC:
        log.warning(
            "插件 %s 是 RUN_PERIODIC 却未声明 suggested_poll（=%r），本轮回落 %ss；"
            "请按契约 D21 补上建议周期",
            plugin_id, declared, DEFAULT_POLL,
        )
    return float(DEFAULT_POLL)


def suggested_poll_of(plugin: Plugin) -> float:
    """本组件实例的节拍默认值（秒）＝ :func:`poll_default_of` 的实例版包装。"""
    return poll_default_of(
        getattr(plugin, "suggested_poll", None), plugin.run_mode, plugin.id
    )


def retry_delay_for(breaker_round: int) -> int:
    """第 ``breaker_round`` 次熔断的自动重试间隔（秒），``breaker_round`` 从 1 起。

    三个常量（``RETRY_DELAY`` / ``RETRY_BACKOFF`` / ``RETRY_DELAY_MAX``）都在
    **调用时**读模块全局 —— 测试可整体缩放，不必改被测代码。

    ``breaker_round <= 1`` 直接给首轮值：一是免掉 ``RETRY_BACKOFF ** 0`` 这种
    「算了个等于没算」的表达，二是让非法轮次（0 / 负数）落回最保守那一档。
    """
    if breaker_round <= 1:
        return int(RETRY_DELAY)
    delay = RETRY_DELAY * (RETRY_BACKOFF ** (breaker_round - 1))
    return int(min(delay, RETRY_DELAY_MAX))


class _TickTask(QRunnable):
    def __init__(self, scheduler: "Scheduler", plugin: Plugin, generation: int) -> None:
        super().__init__()
        self._scheduler = scheduler
        self._plugin = plugin
        self._generation = generation

    def run(self) -> None:  # worker 线程
        try:
            result = self._plugin.tick()
            # §5.3 闭环：v3 插件 tick() 返回 StateReport（或 {key: snapshot}），
            # 框架自动 publish 到 plugin.<id>.status.<key>，插件禁自 publish。
            # v1 插件 tick() 返回 None（自行 publish），此处不重复发。
            self._scheduler._publish_status(self._plugin, result)
            self._scheduler._report.emit(self._plugin, True, "", self._generation)
        except Exception as e:
            log.exception("插件 %s tick 异常", self._plugin.id)
            self._scheduler._report.emit(self._plugin, False, str(e), self._generation)


class Scheduler(QObject):
    _report = Signal(object, bool, str, int)  # (plugin, ok, error, generation)
    broken = Signal(str, str, int)           # (plugin_id, error, fail_count) 熔断，面板出“重试”
    recovered = Signal(str)                  # plugin_id，重试清零计数后恢复

    def __init__(
        self,
        events: EventBus,
        heartbeat: Heartbeat | None = None,
        parent: QObject | None = None,
        *,
        dirty_window_ms: int = DIRTY_WINDOW_MS,
    ) -> None:
        super().__init__(parent)
        self.events = events
        # 时钟源（§8.3.3 层 2）：组合根注入共享实例；缺省自建，使既有
        # ``Scheduler(events)`` 调用点不静默失效——自建的是**同一套机制**，
        # 不是第三套时钟。start 幂等，故本类统一负责启动。
        self._heartbeat = heartbeat or Heartbeat()
        self._heartbeat.start()
        self._tickers: dict[str, Ticker] = {}         # plugin_id → 心跳订阅句柄
        self._plugins: dict[str, Plugin] = {}         # plugin_id → 实例（retry 立即点火用）
        self._pools: dict[str, QThreadPool] = {}      # 每插件独立线程池
        self._running: set[str] = set()
        self._run_since: dict[str, float] = {}        # 每插件当前 tick 的开始时间（僵尸检测）
        self._fail_counts: dict[str, int] = {}
        self._broken: set[str] = set()
        # 熔断轮次（退避档位）：自上次**成功**以来的第几次熔断。手动 retry 不
        # 归零（用户催一次不该把攒下的退避抹掉），只有成功 tick 归零。
        self._breaker_rounds: dict[str, int] = {}
        # 执行代号：每次点火一枚，**只增不减**（组件被移除再登记也不归零）。
        # 它是「这一次执行」的编号，专门用来作废迟到回调 —— watchdog 到点认领、
        # 报告回来认领，两处都是「对不上就丢」。归零会让新实例的第一拍与旧实例
        # 撞号，见 :meth:`_bump_generation`。
        self._gens: dict[str, int] = {}
        self._retry_timers: dict[str, QTimer] = {}    # 熔断自愈定时器（一次性，非节拍源）
        # 按需点火（§8.2.2，批次 10）：plugin_id → 最近一次 dirty 原因（诊断用；
        # 「有脏标记」本身才是语义，原因是给人看的）。窗口定时器一次性、非节拍源。
        self._dirty: dict[str, str] = {}
        self._dirty_window_ms = max(int(dirty_window_ms), 0)
        self._dirty_timer = QTimer(self)
        self._dirty_timer.setSingleShot(True)
        self._dirty_timer.timeout.connect(self._flush_dirty)
        self._report.connect(self._on_report)

    # ---------- 注册/启停 ----------

    def _publish_status(self, plugin: Plugin, result) -> None:
        """§5.3 闭环：把 tick() 返回的快照自动 publish 到 plugin.<id>.status.<key>。

        支持两种返回：单个 ``StateReport`` → 保留键 ``"main"``；``dict[key,
        StateReport]`` → 逐 key 发布（多状态插件）。v1 插件返回 None，此处无
        操作、不重复发。事件总线跨线程安全（queued 回主线程分发）。

        topic 拼法**不在此处手写**：统一走 ``Plugin.status_topic()``，避免同一个
        字符串在框架与组件两侧各拼一遍（两处拼法迟早分叉）。
        """
        if result is None:
            return
        if isinstance(result, StateReport):
            self.events.publish(plugin.status_topic(), result)
            return
        if isinstance(result, dict):
            for key, snap in result.items():
                if isinstance(snap, StateReport):
                    self.events.publish(plugin.status_topic(key), snap)

    def _interval_for(self, plugin: Plugin) -> float:
        """本组件**此刻**的节拍（秒）：用户配置 > 组件建议 > 兜底值。

        ``add_plugin`` 与 :meth:`set_poll` 共用这一份判据 —— 两处各写一遍，
        「改配置立即重挂」与「注册时算一遍」迟早算出两个数。
        配置里写了非数字 / ≤0 一律回退**建议值**（不是 ``DEFAULT_POLL``）：
        用户填坏了不该顺手把组件自己的合理节拍也丢掉。
        """
        suggested = suggested_poll_of(plugin)
        raw = plugin.plugin_config().get("poll", suggested)
        try:
            interval_s = float(raw)
        except (TypeError, ValueError):
            interval_s = 0.0
        if interval_s <= 0:
            log.warning("插件 %s 的 poll=%r 非法，回退 %ss", plugin.id, raw, suggested)
            interval_s = suggested
        return interval_s

    def add_plugin(self, plugin: Plugin) -> None:
        """登记一个组件，并立刻点火一轮（首屏零陈旧）。

        **两种 ``run_mode`` 都登记**（2026-09-22 改）：按需型不订阅心跳（没有节拍），
        但要能被 ``mark_dirty`` 与 ``retry`` 点火 —— 它此前根本不进 ``_plugins``，
        于是系统事件 handler 里那句 ``mark_dirty`` **静默落空**、连日志都没有，
        「只用钩子的组件」被迫声明成周期型才能干活（见 ``core/audio`` 的来历）。

        登记 = 实例 + 独立线程池 + 代号；**节拍是另一件事**，只有周期型才订阅。
        「启动即跑一轮」对两者都做：按需型没有节拍，这一轮就是它唯一的**自动**首屏来源。
        """
        if plugin.id in self._plugins:
            return
        self._plugins[plugin.id] = plugin
        # 独立线程池：单 worker。插件 tick 卡死只占自己的 worker，不饿死其他插件。
        pool = QThreadPool(self)
        pool.setMaxThreadCount(1)
        self._pools[plugin.id] = pool
        # **刻意不重置代号**：见 :meth:`_bump_generation`。同名的旧实例可能还有一枚
        # 已排上队的 watchdog 与一帧在途报告，重置会让它们认领新实例的执行。
        if plugin.run_mode == RUN_PERIODIC:
            # 节拍默认值来自**组件声明**（契约 D21，``suggested_poll``），用户配置仍优先——
            # 组件给的是建议，不是命令（``plugin_config`` 是合并后的段，用户写过就是用户的）。
            # §8.3.3 层 1+2：订阅单一心跳。相位由 key(=plugin.id) 复现地算出，
            # 同 poll 的插件错开到期时刻（禁 hash()，见 core.heartbeat）。
            self._tickers[plugin.id] = self._heartbeat.add_ticker(
                self._interval_for(plugin), partial(self._on_beat, plugin),
                key=plugin.id,
            )
        self._on_beat(plugin)  # 启动即跑一轮

    def set_poll(self, plugin_id: str) -> bool:
        """按**当前配置**重挂某插件的节拍（用户改了 ``plugins.<id>.poll`` 之后调用）。

        为什么必须重挂而不是改 ``Ticker.interval``：相位也是按 interval 算的
        （``heartbeat.phase_for``），只改数字会让「同节拍的插件错开到期」失真。

        返回 ``False`` = 该组件不在调度器里，或它是**按需型**（没有节拍可挂）：配置
        照样落盘、下次启用时生效，**不是失败**。设置窗只给周期型组件画那个控件（判据
        是 ``poll_default`` 非空），正常路径下不会走到这里；留着这条分支是为了
        「关掉再打开」与「卸载中」两个时序缝隙 —— 以及防「给按需型挂上节拍」这件事
        被别的调用点悄悄做出来（那会让「按需」名不副实）。
        """
        plugin = self._plugins.get(plugin_id)
        if plugin is None or plugin.run_mode != RUN_PERIODIC:
            return False
        self._heartbeat.remove_ticker(self._tickers.pop(plugin_id, None))
        self._tickers[plugin_id] = self._heartbeat.add_ticker(
            self._interval_for(plugin), partial(self._on_beat, plugin), key=plugin_id
        )
        return True

    def remove_plugin(self, plugin_id: str) -> None:
        # 先作废该组件上一切在飞回调，再动状态：这一帧如果已经跑在 worker 里，
        # 它的报告会排在 ``waitForDone`` 之后回到主线程，那时下面这些清空已经做完
        # ⇒ 不推进代号的话，``_on_report`` 会把刚清掉的失败计数 / 熔断状态**又写一遍**
        # （症状与「熔断状态随实例失效」那条修的是同一个：禁用再启用后照旧不跑）。
        self._bump_generation(plugin_id)
        self._heartbeat.remove_ticker(self._tickers.pop(plugin_id, None))
        self._plugins.pop(plugin_id, None)
        pool = self._pools.pop(plugin_id, None)
        if pool:
            pool.waitForDone(5000)
        self._cancel_retry(plugin_id)
        self._running.discard(plugin_id)
        self._run_since.pop(plugin_id, None)
        # 熔断状态随实例一起失效：这三份是「这个组件实例的历史」，不是 RPC 那种可以
        # 跨实例继承的事实。缺了 ``_broken`` / ``_fail_counts`` 时，曾经熔断过的组件
        # 被禁用再重挂回来后会**一次 tick 都不跑** —— 面板写着已启用、状态永远停在
        # 旧结论，而自愈定时器已在上一行被取消，没人再把它拉起来。
        self._broken.discard(plugin_id)
        self._fail_counts.pop(plugin_id, None)
        # 退避档位随实例一起失效：同名插件重装后不该继承上一次的故障史
        self._breaker_rounds.pop(plugin_id, None)
        # 脏标记随实例一起失效（§8.2.4 无泄漏）：移除后不再有点火目标
        self._dirty.pop(plugin_id, None)
        if not self._dirty:
            self._dirty_timer.stop()

    def stop_all(self) -> None:
        # 与 :meth:`remove_plugin` 同一条口径：停机也要作废一切在飞回调，否则停机后
        # 回来的一帧报告会把它下面刚清空的失败计数 / 熔断状态重新写进去。
        for pid in list(self._gens):
            self._bump_generation(pid)
        for handle in self._tickers.values():
            self._heartbeat.remove_ticker(handle)
        self._tickers.clear()
        self._plugins.clear()
        for pid, pool in self._pools.items():
            pool.waitForDone(5000)
        self._pools.clear()
        for t in self._retry_timers.values():
            t.stop()
        self._retry_timers.clear()
        self._breaker_rounds.clear()
        self._dirty.clear()
        self._dirty_timer.stop()
        # 与 :meth:`remove_plugin` 同一条口径：熔断与失败计数是**实例**的故障史，
        # 随实例一起清空。退出路径上它看着多余，留着却等于给「重启前停摆过、
        # 重启后照旧不跑」这种一年一次的怪事留一个容身处。
        self._broken.clear()
        self._fail_counts.clear()
        self._running.clear()
        self._run_since.clear()

    # ---------- 调度 ----------

    def mark_dirty(self, plugin_id: str, reason: str = "") -> None:
        """按需点火入口（§8.2.2，批次 10）：事件 handler 只标记脏，不干活。

        handler 跑在总线线程（主线程），**不得**在其中做 IO / 子进程 / 网络；
        调本方法登记「有活要干」，由调度器在 **worker 线程**跑一轮 ``tick()``
        完成实际工作。收益：① 事件风暴经合并窗口归并为一次 tick，插件不必自写
        debounce；② UI 不被事件拖卡；③ 事件路径与轮询路径汇入同一执行入口。

        合并语义（trailing throttle）：窗口 ``_dirty_window_ms`` 内多次标记只
        点火一轮——计时从**首个**标记开始，不因后续标记顺延，故持续风暴不会
        饿死点火。目标**正在跑**时保留脏标记，待其结束（``_on_report``）补点火，
        不丢事件、不并跑；目标**已熔断**时丢弃脏标记（熔断自愈的 ``retry`` 会
        无条件点火，且熔断期间的状态本就要等解熔后重新取）。

        未知 ``plugin_id`` 静默忽略（已卸载插件 / 面板对未知 id 调用均安全）。
        """
        if plugin_id not in self._plugins:
            return
        self._dirty[plugin_id] = reason or ""
        self._arm_dirty_flush()

    def _arm_dirty_flush(self) -> None:
        """武装合并窗口（幂等：已在计时则不重启 —— trailing throttle 语义）。"""
        if self._dirty and not self._dirty_timer.isActive():
            self._dirty_timer.start(self._dirty_window_ms)

    def _flush_dirty(self) -> None:
        """窗口到期：为每个脏插件点火一轮；在跑者保留标记（待 ``_on_report``）。"""
        for pid, reason in list(self._dirty.items()):
            plugin = self._plugins.get(pid)
            if plugin is None:
                self._dirty.pop(pid, None)
                continue
            if pid in self._broken:
                self._dirty.pop(pid, None)   # 熔断中：解熔后的 retry 会无条件点火
                continue
            if pid in self._running:
                continue                     # 保留标记：跑完由 _on_report 补点火
            self._dirty.pop(pid, None)
            log.debug("按需刷新：%s（%s）", pid, reason or "-")
            self._on_beat(plugin)

    def _bump_generation(self, plugin_id: str) -> int:
        """推进该组件的执行代号，返回新值。**只增不减，跨实例亦然。**

        代号有两个消费方，都是「对不上就丢弃」：① 点火时排的那枚 ``TICK_TIMEOUT``
        后的 watchdog，到点要认领**这一次**执行；② worker 跑完发回的报告，要认领
        **这一次**执行。两处都靠 ``self._gens[id] == gen``。

        因此它必须是「这一次执行」的编号，而不是「这个组件第几次执行」：

        - ``add_plugin`` 里归零 ⇒ 新实例的第一拍又是 1，与旧实例撞号 ⇒ 旧实例那枚
          已排上队的 watchdog 到点时会认领新实例的在飞 tick，记一次**假超时**并
          把它从 ``_running`` 摘掉（用户看不到日志，只看到面板那一格一次异常）；
        - 移除时推进一次 ⇒ 该组件上一切在飞回调（未回来的报告、已排上的 watchdog）
          一次性作废，移除那一刻清掉的熔断 / 失败计数不会又被写回来。
        """
        gen = self._gens.get(plugin_id, 0) + 1
        self._gens[plugin_id] = gen
        return gen

    def _on_beat(self, plugin: Plugin) -> None:
        """心跳到期回调：只做「该不该点火」的判定，执行仍丢给插件自己的线程池。"""
        pid = plugin.id
        # 僵尸回收（第二重保险）：_running 里存在超龄未完成 tick → 强制清理，
        # 避免 watchdog 因任何原因失效时插件被永久停摆。
        now = time.monotonic()
        if pid in self._running and now - self._run_since.get(pid, now) > TICK_TIMEOUT:
            log.error("插件 %s 存在超龄 tick（>%ds），强制回收 _running", pid, TICK_TIMEOUT)
            self._running.discard(pid)
            self._run_since.pop(pid, None)
        if pid in self._running or pid in self._broken:
            return        # 上一轮未完成 / 已熔断 → 跳过本拍（不排队，见模块 docstring）
        self._running.add(pid)
        self._run_since[pid] = now
        gen = self._bump_generation(pid)
        pool = self._pools.get(pid)
        if pool is None:
            self._running.discard(pid)
            self._run_since.pop(pid, None)
            return
        pool.start(_TickTask(self, plugin, gen))
        # watchdog：超时按失败计，迟到结果作废
        QTimer.singleShot(TICK_TIMEOUT * 1000, partial(self._watchdog, plugin, gen))

    def _watchdog(self, plugin: Plugin, gen: int) -> None:
        if self._gens.get(plugin.id) != gen or plugin.id not in self._running:
            return  # 已正常完成
        log.error("插件 %s tick 超时（%ds），按失败处理", plugin.id, TICK_TIMEOUT)
        self._bump_generation(plugin.id)  # 作废迟到的报告
        self._running.discard(plugin.id)
        self._run_since.pop(plugin.id, None)
        self._on_failure(plugin, f"tick 超时 {TICK_TIMEOUT}s")

    def _on_report(self, plugin: Plugin, ok: bool, error: str, gen: int) -> None:
        if self._gens.get(plugin.id) != gen:
            return  # watchdog 已作废
        self._running.discard(plugin.id)
        self._run_since.pop(plugin.id, None)
        if ok:
            self._fail_counts[plugin.id] = 0
            # 退避归零的唯一出口：真的跑通过一次 ⇒ 故障结束了，下一次熔断
            # 重新从首轮间隔起步（否则一次历史故障会把之后的恢复拖慢好几分钟）。
            self._breaker_rounds[plugin.id] = 0
        else:
            self._on_failure(plugin, error)
        # 本插件在跑期间收到的脏标记：此刻补点火（§8.2.2 不丢事件）。
        # 熔断路径下 _flush_dirty 会丢弃标记，故不会在此反复武装。
        if plugin.id in self._dirty:
            self._arm_dirty_flush()

    def _on_failure(self, plugin: Plugin, error: str) -> None:
        if plugin.failure_policy != FAIL_BREAK:
            return  # FAIL_LOG 类只记日志（tick 内已记），不熔断
        count = self._fail_counts.get(plugin.id, 0) + 1
        self._fail_counts[plugin.id] = count
        if count >= FAIL_THRESHOLD:
            self._broken.add(plugin.id)
            # 无需停表：心跳仍在每拍调用 _on_beat，_broken 前置判定直接返回；
            # 解除熔断（retry / 自动重试）后立即点火。
            rounds = self._breaker_rounds.get(plugin.id, 0) + 1
            self._breaker_rounds[plugin.id] = rounds
            delay = retry_delay_for(rounds)
            log.error("插件 %s 连续失败 %d 次，已熔断停用（第 %d 次熔断），%ds 后自动重试",
                      plugin.id, count, rounds, delay)
            self.broken.emit(plugin.id, error, count)
            self.events.publish("plugin_broken", {"id": plugin.id, "error": error})
            self._schedule_auto_retry(plugin.id, delay)

    # ---------- 熔断自愈 ----------

    def _schedule_auto_retry(self, plugin_id: str, delay: int) -> None:
        """武装一次性自愈定时器（秒）。间隔由调用方按退避档位算好。"""
        self._cancel_retry(plugin_id)
        t = QTimer(self)
        t.setSingleShot(True)
        t.setInterval(delay * 1000)
        t.timeout.connect(lambda: self.retry(plugin_id))
        t.start()
        self._retry_timers[plugin_id] = t

    def _cancel_retry(self, plugin_id: str) -> None:
        t = self._retry_timers.pop(plugin_id, None)
        if t:
            t.stop()
            t.deleteLater()

    # ---------- 状态/恢复 ----------

    def status(self, plugin_id: str) -> dict:
        return {
            "broken": plugin_id in self._broken,
            "fail_count": self._fail_counts.get(plugin_id, 0),
            "running": plugin_id in self._running,
            "dirty": plugin_id in self._dirty,         # 有已标记、待点火的脏状态（§8.2.2）
        }

    def retry(self, plugin_id: str) -> None:
        """面板“重试”/熔断自愈：清零计数、解除熔断、立即点火一轮。

        「立即点火」而非「等下一拍」：面板点重试的语义是立刻恢复，且与
        ``add_plugin`` 的「启动即跑一轮」同构（相位只作用于后续节拍）。
        未注册的 plugin_id（如面板对未知 id 重试）安全空转。
        """
        self._cancel_retry(plugin_id)
        self._fail_counts[plugin_id] = 0
        self._broken.discard(plugin_id)
        plugin = self._plugins.get(plugin_id)
        if plugin is not None:
            self._on_beat(plugin)
        self.recovered.emit(plugin_id)
