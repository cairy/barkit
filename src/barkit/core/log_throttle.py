"""同因日志节流：同一句话在窗口内只落一条，窗口过后补一句「已合并 N 次」。

**为什么需要**（2026-10-13）：本仓有三类高频出口，它们的日志过去都是「每拍一条」：

- 周期型长驻服务的 ``poll()`` 抛异常（``core/resident.py``）—— 服务按 3s 的节拍跑，
  坏掉就按 3s 的节拍刷 traceback；
- Qt 探测函数（``notify_guard`` 的显示器 / 进程快照）—— 取不到就每拍一条 warning，
  而「这台机器上取不到某条信息」是常态退化，不是故障；
- 挂着的持续态（``hosts_guard`` 的「意图暂未送达」）—— 没送达之前每 30s 一条。

用户 2026-10-13 的原话是「去掉一些不需要的输出」。口径**不是把话删掉**：同一条信息
第一次出现永远要落盘（排查全靠它），要去掉的是同一个原因的第 2 条到第 N 条。

**与另两处节流的分工**（三处都在，别合并 —— 判据不同）：

- ``logging_setup`` 的 Qt 消息合并：按**同一条文本**合并，窗口 60s，挡的是「同一条
  布局警告按帧复现」；
- helper 侧 ``safeio.log(key=...)``：跑在 root 守护里，按**显式 key** 节流，窗口 300s，
  只写它自己那份 ``helper.log``；
- 本模块：app 侧，按**渲染后的整句话**节流（默认 key ＝ 那句话本身），窗口同 helper。

**为什么 key 取整句话**：失败原因变了就是新信息（「超时」与「权限不足」的处置完全
不同），此时必须再出声。按整句话比对能自动区分这两种，调用点不必自己维护状态。
要按更粗的粒度合并（例如同一件事但文案里带了个每次都变的计数），显式传 ``key``。
"""

from __future__ import annotations

import logging
import time

#: 同一句话的默认节流窗口（秒）。取 helper 侧同一个数（``safeio.THROTTLE_DEFAULT_S``）——
#: 两个落点用两个口径，用户对「多久说一次」的预期就得学两遍。
DEFAULT_WINDOW_S = 300.0

#: 文本各异的失败堆太多时的整表丢弃线（与 ``logging_setup._QT_MUTE_MAX_KEYS`` 同款兜底）。
_MAX_KEYS = 512

#: key -> [窗口起点, 窗口内被丢掉的条数]
_seen: dict[str, list] = {}


def log_throttled(
    logger: logging.Logger,
    level: int,
    msg: str,
    *args,
    key: str | None = None,
    exc_info: bool = False,
    window: float | None = None,
) -> None:
    """写一行「同因只报一次」的日志。

    ``msg % args`` 的结果既是正文、也是默认的合并键。窗口内重复只累加不落盘；
    窗口过后那一条把累计次数补在正文后面 —— 「这个问题一直在」与「持续了多久」
    两条信息都还在，刷屏没了。

    ``exc_info=True`` 与 ``logging`` 同义（**必须在 except 块里用**，否则 Python
    会往日志里写一行 ``NoneType: None``）。首条带完整堆栈，被合并掉的那些不带。
    """
    try:
        text = msg % args if args else msg
    except Exception:  # noqa: BLE001 —— 格式化失败不该让「记日志」这件事本身炸掉
        text = str(msg)
    memo_key = key if key is not None else text
    span = DEFAULT_WINDOW_S if window is None else window
    if span > 0:
        now = time.monotonic()
        entry = _seen.get(memo_key)
        if entry is not None:
            started, dropped = entry
            if now - started <= span:
                entry[1] = dropped + 1
                return
            if dropped:
                text = f"{text}（{span:.0f}s 内已合并 {dropped} 次）"
            entry[0], entry[1] = now, 0
        else:
            if len(_seen) >= _MAX_KEYS:
                _seen.clear()
            _seen[memo_key] = [now, 0]
    logger.log(level, "%s", text, exc_info=exc_info)


def reset_throttle() -> None:
    """清空合并账（测试用；生产不调 —— 跨用例残留会让下一条本该落盘的静默）。"""
    _seen.clear()


__all__ = ["DEFAULT_WINDOW_S", "log_throttled", "reset_throttle"]
