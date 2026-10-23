"""core/heartbeat：框架唯一时钟源（方案 v3 §8.3.3 层 1 + 层 2）。

动机（批次 8）：改造前每个 daemon 插件各持一个 ``QTimer``、各自漂移，插件之间
「彼此不知道对方存在」。本模块把节拍收归为**单一心跳**——一个 ``QTimer`` 每拍
（生产默认 1s）遍历全部 ticker，按**累积式**判定到期并通知订阅方。

职责边界（刻意不做，§8.3.2 保留不动）：

- 只负责**计时与到期通知**。执行、并发闸门、watchdog、僵尸回收、熔断自愈
  仍归 ``Scheduler``；本模块不感知插件、不碰线程池。
- 不引入 APScheduler / RxPY 等第三方调度库（§8.3.7 克制清单）。

批次 8 裁决（2026-09-11 审查后定稿）：

- **层 1 相位错开**：``phase_for()`` 用 ``zlib.crc32`` 复现地算相位偏移，
  **禁用 ``hash()``**（Python 3 字符串哈希默认随机化，跨进程不可复现、
  测试无法断言）。相位按散列的**整条 32 位**摊到区间上，**不是**对
  ``interval`` 取模（2026-10-02 修：整秒 interval 会把相位量化成整秒，
  ``interval=1.0`` 时直接塌成 0 ⇒ 1s 档全体相位为 0、与「错开」的承诺相反）。
  另注：相位细于**心跳周期**的部分会被到期判定量化掉，见 ``phase_for``。
- **层 2 节拍对齐**：单一心跳取代 Scheduler 的全部每插件 ``QTimer``。
  到期判定用累积式（``last_fire += interval``），绝不用取模——取模一旦因
  主线程卡顿 / 系统休眠错过窗口，要等整整一个 interval。
- **层 3 全局并发闸门（全局最多 2 个 tick）= 不做**。理由：① 与 §8.3.2
  「每插件独立线程池、卡死不连坐」互斥——卡死 tick 占住闸门名额会把其余
  插件拖到 ``TICK_TIMEOUT`` 之后；② 与「启动即跑一轮、首屏零陈旧」冲突——
  ``add_plugin`` 的首轮同步点火发生在 Qt 事件循环启动之前，闸门会挡掉后
  几个插件的首屏数据；③ 其防惊群的目标已由层 1 相位错开零成本达成。
- **心跳周期与 ticker 周期解耦**：心跳周期是**采样频率**（生产 1s），ticker
  的 ``interval`` 是任意浮点。测试注入更小的心跳周期即可保留亚秒级用例。
"""

from __future__ import annotations

import logging
import time
import zlib
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, QTimer

log = logging.getLogger(__name__)

# 生产心跳周期：1s（§8.3.3 层 2）。作为采样频率，决定到期判定的量化粒度。
DEFAULT_BEAT_MS = 1000
# 心跳周期下限：防误配成 0 造成忙等。
MIN_BEAT_MS = 10

# ``zlib.crc32`` 的取值空间（32 位无符号整数）。相位取它的**整条位宽**按比例摊到
# 区间上；除以 2 的幂在二进制浮点里是精确运算 ⇒ 逐位可复现、不引入舍入漂移。
_HASH_SPACE = float(1 << 32)


def phase_for(key: str, interval: float) -> float:
    """按 ``key`` 复现地算相位偏移 ∈ ``[0, interval)``（§8.3.3 层 1）。

    **禁用 ``hash()``**：Python 3 对 str 的哈希默认启用随机化
    （``PYTHONHASHSEED``），行为跨进程不可复现、测试无法断言。``zlib.crc32``
    是确定性散列，同 key 同 interval 恒等，且与进程启动顺序无关。

    **不许写成 ``crc32(key) % interval``**（2026-10-02 修）：``crc32`` 是整数，
    ``int % float`` 的余数在整秒 ``interval`` 上**只能落在整秒**，而
    ``interval=1.0`` 时 ``n % 1.0`` 恒等 ``0.0`` ⇒ 1s 档订阅的相位**全体为 0**
    —— 与「相位摊在 ``[0, interval)``、同节律不打齐」这条承诺正好相反，且不报错、
    不留痕。改为按整条 32 位散列取比例（``crc32 / 2**32 * interval``）。

    **有效分辨率＝心跳周期**（生产 1s）：到期判定跑在心跳的整数拍上，细于一拍的
    相位会被量化掉。因此 ``interval`` 不大于心跳周期的订阅（生产里就是 1s 档）
    **无论相位取何值都落在同一拍上** —— 相位能拉开的是 3s / 30s / 300s 这类跨拍
    节律（3s ⇒ 三个拍位）。这一条由心跳粒度决定，不是本函数能修的。
    """
    span = float(interval)
    if not key or span <= 0:
        return 0.0
    return (zlib.crc32(key.encode("utf-8")) / _HASH_SPACE) * span


class Ticker:
    """一个订阅者（宿主内部记录）。``add_ticker`` 的返回值即句柄。"""

    __slots__ = ("key", "interval", "phase", "on_due", "last_fire")

    def __init__(
        self,
        key: str,
        interval: float,
        phase: float,
        on_due: Callable[[], None],
        last_fire: float,
    ) -> None:
        self.key = key
        self.interval = interval
        self.phase = phase
        self.on_due = on_due
        self.last_fire = last_fire


class Heartbeat(QObject):
    """框架唯一时钟源：一个 ``QTimer``，每拍通知到期 ticker。

    ``clock`` 可注入（默认 ``time.monotonic``），使测试能用可控时间轴断言
    到期语义，而不必依赖真实等待。
    """

    def __init__(
        self,
        parent: Optional[QObject] = None,
        *,
        beat_ms: int = DEFAULT_BEAT_MS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(parent)
        self._clock = clock
        self._beat_ms = max(int(beat_ms), MIN_BEAT_MS)
        self._tickers: list[Ticker] = []
        self._timer = QTimer(self)
        # CoarseTimer（2026-09-28 由 PreciseTimer 改）：语义是「允许系统把唤醒
        # 合并到 ±5% 内」。常驻菜单栏 App 用 PreciseTimer 等于每秒钉一次唤醒、
        # 让 macOS 的 timer coalescing 与 App Nap 全部失效 —— 而所有 ticker 里
        # 最短的节律就是 1s，±50ms 的合并窗口对「错开相位、不 lockstep」这个
        # 目的完全够（相位偏移本身就是秒级粒度）。
        self._timer.setTimerType(Qt.TimerType.CoarseTimer)
        self._timer.setInterval(self._beat_ms)
        self._timer.timeout.connect(self._beat)

    # ---------- 启停 ----------

    def start(self) -> None:
        """启动心跳（幂等；重复调用无副作用）。"""
        self._timer.start()

    def stop(self) -> None:
        """停止心跳（幂等）。已注册的 ticker 保留，下次 start 后继续计时。"""
        self._timer.stop()

    @property
    def running(self) -> bool:
        return self._timer.isActive()

    @property
    def beat_ms(self) -> int:
        return self._beat_ms

    # ---------- 订阅 ----------

    def add_ticker(
        self,
        interval: float,
        on_due: Callable[[], None],
        *,
        key: str = "",
        phase: Optional[float] = None,
    ) -> Ticker:
        """注册一个到期订阅，返回句柄（供 ``remove_ticker``）。

        - ``interval``：节律（秒）。≤0 视为误配，回退「每拍」（不忙等）。
        - ``key``：订阅方标识；同时作为 ``phase`` 的默认散列输入。
        - ``phase``：相位偏移（秒），缺省由 ``phase_for(key, interval)`` 算。
          语义见 §8.3.3 层 1：首轮之后的节拍相对心跳网格错开 ``phase``，
          使同 ``interval`` 的多个订阅**不 lockstep**。

        首个到期时刻 = ``now + phase``（订阅方若已自行跑过首轮，即「首轮立即
        点火 + 后续相位错开」——两者不互斥）。
        """
        interval = float(interval)
        if interval <= 0:
            log.warning("heartbeat ticker %r 的 interval=%r 非法，回退为每拍",
                        key or "?", interval)
            interval = self._beat_ms / 1000.0
        if phase is None:
            phase = phase_for(key, interval)
        # 累积式到期的起点：把「首轮」锚在 now + phase（见 add_ticker docstring）
        now = self._clock()
        tk = Ticker(key, interval, float(phase), on_due, now - interval + float(phase))
        self._tickers.append(tk)
        return tk

    def remove_ticker(self, ticker: Optional[Ticker]) -> None:
        """注销订阅（幂等；``None`` / 已移除均安全返回）。"""
        if ticker is None:
            return
        try:
            self._tickers.remove(ticker)
        except ValueError:
            pass

    def ticker_count(self) -> int:
        """当前订阅数（调试 / 测试；亦用于断言「无孤儿定时」）。"""
        return len(self._tickers)

    def ticker_keys(self) -> list[str]:
        return [t.key for t in self._tickers]

    # ---------- 每拍 ----------

    def _beat(self) -> None:
        """一拍：遍历快照，累积式判到期，逐个通知。

        单拍内每个 ticker **最多通知一次**；若落后多个周期（休眠回来），
        走 catch-up 分支重置节拍而不补跑——避免唤醒瞬间惊群。
        """
        now = self._clock()
        for tk in tuple(self._tickers):
            if tk not in self._tickers:
                continue  # 本次遍历中已被回调移除
            if now - tk.last_fire < tk.interval:
                continue
            tk.last_fire += tk.interval                # 累加而非赋 now，避免漂移累积
            if tk.last_fire < now - tk.interval:       # 落后超一周期（休眠回来）
                tk.last_fire = now
            try:
                tk.on_due()
            except Exception:  # noqa: BLE001 —— 单个订阅异常不拖垮心跳
                log.exception("heartbeat ticker %s 到期回调异常", tk.key or "?")


__all__ = ["Heartbeat", "Ticker", "phase_for", "DEFAULT_BEAT_MS", "MIN_BEAT_MS"]
