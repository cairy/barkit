"""core/deferred_cleanup：卸载收尾的「延迟删除」（等外部消费方收敛）。

为什么有这个模块
----------------
插件卸载的第 4 步是删数据文件。多数插件的 ``data_files`` 只有 app 自己读，删了就删了；
但有一类文件是**外部消费方（root helper：独立进程、周期轮询）在读**的：插件在
``teardown()`` 里把最后一版期望态写进去、敲门之后，消费方要一个轮次才消费到。

若框架紧接着（毫秒级）把文件删掉，消费方下一轮读到「文件不存在」—— 而按既有语义
「不存在 = 冻结、保持上轮状态」，撤销就**永远不生效**。所以「延迟」不是礼貌，是正确性。

延迟的范围是**整段收尾**（删文件 + 删配置段 + ``forget()``），不只删文件：``forget()``
会把该插件的 helper task 写成 ``enabled: false``，消费方即使被敲醒也不会再跑那条 task。

框架的职责边界
--------------
**不删** → 按框架心跳轮询插件给的判据 → 为真则收尾、到期则放弃。
框架**不解释文件内容**：「消费方收敛了没有」是插件的领域知识，由 ``ready()`` 提供
（见 ``core.resources.DeferredRemoval``）。

为什么时钟用框架心跳
--------------------
卸载动作跑在界面线程上。等一个外部进程的轮次不能阻塞界面（数秒的卡死与「点了没反应」
是本仓反复踩过的坑），也不该把等待塞进提权脚本（那样提示语无法实时更新）。
心跳是框架唯一的时钟源，且插件停摆之后它仍在跑，正好承载「之后几拍内收尾」。

超时为什么选择「什么都不做」
----------------------------
到期即放弃本轮：**文件留着、插件不遗忘、task 表不动**，只记 ERROR 并请界面刷新。
不硬往下删，也不假装成功 —— 往回退是安全的（消费方下次自然轮次仍会收敛），而硬往前走
会把「撤销没做完」变成一个看不见的既成事实。

判据**立即为真**时零等待
------------------------
``schedule()`` 会当场判一次（消费方早已收敛、或压根没有消费方在跑时立即收尾），
所以「本来就不需要等」的情形不会平白多出一个心跳周期。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)

#: 复检节律（秒）：比默认上限小一个数量级，保证「确认即收尾」而不是等到超时才动。
CHECK_INTERVAL = 0.5


@dataclass
class _Pending:
    """一笔在等的收尾。"""

    label: str
    ready: Callable[[float], bool]
    watermark: float
    timeout_s: float
    deadline: float
    on_commit: Callable[[], None]
    on_give_up: Callable[[], None]


class DeferredCleaner:
    """把「等外部消费方收敛之后再收尾」挂到框架心跳上（单实例、可复用）。

    ``heartbeat`` 为 ``None`` 即**未接线**（轻量测试桩 / 无时钟的构造路径）：此时
    ``schedule()`` 返回 False，调用方应当**立即**收尾（退回接线前的行为），
    而不是把卸载悬在半空。
    """

    def __init__(
        self,
        heartbeat=None,
        *,
        clock: Callable[[], float] = time.time,
        check_interval: float = CHECK_INTERVAL,
    ) -> None:
        self._heartbeat = heartbeat
        self._clock = clock
        self._interval = float(check_interval)
        self._items: list[_Pending] = []
        self._ticker = None

    @property
    def hooked(self) -> bool:
        """是否接上了时钟（未接线时调用方须自己立即收尾）。"""
        return self._heartbeat is not None

    def pending(self) -> int:
        """在等的收尾笔数（测试 / 诊断）。"""
        return len(self._items)

    def schedule(
        self,
        *,
        label: str,
        ready: Callable[[float], bool],
        watermark: float,
        timeout_s: float,
        on_commit: Callable[[], None],
        on_give_up: Callable[[], None],
    ) -> bool:
        """登记一笔延迟收尾。返回 False ＝ 未接时钟（调用方立即收尾）。

        登记后**当场判一次**：判据立即为真则同步走 ``on_commit``（零等待），
        因此本方法返回 True 时，收尾**可能已经完成**。
        """
        if self._heartbeat is None:
            return False
        item = _Pending(
            label=label,
            ready=ready,
            watermark=float(watermark),
            timeout_s=max(float(timeout_s), 0.0),
            deadline=self._clock() + max(float(timeout_s), 0.0),
            on_commit=on_commit,
            on_give_up=on_give_up,
        )
        self._items.append(item)
        log.info("延迟收尾已登记：%s（上限 %.1fs，等外部消费方收敛）", label, timeout_s)
        self.poll()
        return True

    def poll(self) -> None:
        """复检全部在等的收尾（心跳每拍调；测试可直接调，无需真实等待）。"""
        now = self._clock()
        for item in tuple(self._items):
            done = self._is_ready(item)
            if done:
                self._settle(item)
            elif now >= item.deadline:
                self._abandon(item)
        self._sync_ticker()

    # ---------- 内部 ----------

    def _is_ready(self, item: _Pending) -> bool:
        """问插件「现在收尾安全吗」。判据自身抛异常按「还不安全」处理。

        判据是插件写的，不能因为它的 bug 把卸载流程卡成半途 —— 抛异常就继续等，
        等不到即走超时分支（同样只是不动、记 ERROR）。
        """
        try:
            return bool(item.ready(item.watermark))
        except Exception:  # noqa: BLE001
            log.exception("延迟收尾判据异常：%s（按未收敛处理）", item.label)
            return False

    def _settle(self, item: _Pending) -> None:
        self._items.remove(item)
        log.info("延迟收尾确认：%s（外部消费方已收敛）", item.label)
        try:
            item.on_commit()
        except Exception:  # noqa: BLE001 —— 收尾失败不该拖垮心跳
            log.exception("延迟收尾执行失败：%s", item.label)

    def _abandon(self, item: _Pending) -> None:
        self._items.remove(item)
        log.error(
            "延迟收尾超时：%s —— 外部消费方未在 %.1fs 内收敛。文件与 task 声明一律保留"
            "（消费方下次轮次仍可收敛），本次不删、不遗忘；可稍后重试卸载。",
            item.label,
            item.timeout_s,
        )
        try:
            item.on_give_up()
        except Exception:  # noqa: BLE001
            log.exception("延迟收尾放弃回调异常：%s", item.label)

    def _sync_ticker(self) -> None:
        """订阅状态跟着待办走：有待办就订，没了就摘（不留孤儿定时）。

        ``schedule()`` 只调 ``poll()``、本方法在其末尾统一收口 —— 若判据当场为真，
        这一笔从头到尾**不会**产生「订了又立刻摘」的多余动作。
        """
        if self._heartbeat is None:
            return
        if self._items and self._ticker is None:
            self._ticker = self._heartbeat.add_ticker(
                self._interval, self.poll, key="deferred_cleanup"
            )
        elif not self._items and self._ticker is not None:
            self._heartbeat.remove_ticker(self._ticker)
            self._ticker = None


__all__ = ["DeferredCleaner", "CHECK_INTERVAL"]
