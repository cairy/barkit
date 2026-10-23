"""primitives：原语分发（能力集 → 实现，§7.4.4）。

**唯一**的「action / op → 实现」映射表。增删能力必须同时改
``actions.ACTION_SCHEMAS``、本模块与 ``version.HELPER_VERSION``（重装 daemon）
——能力封闭是硬约束。

两种调用形态，对应会话里的两类消息：

- :func:`run_sync` —— 周期型。输入是某 subject **当前那份意图**的 JSON 文本
  （由 ``tasks.Runner`` 从内存里取，不再读盘）。旧签名与旧语义一字未变。
- :func:`run_op` —— 一次性。输入是 ``op.run`` 帧自带的参数。**它取代了旧的
  「顶层 ``ops`` 字段 + ``seq`` 水位线 + 回执写状态文件」那一整套**：命令的
  「执行且仅执行一次」现在由**帧本身**保证（一帧一命令，回帧即回执），不再需要
  任何跨重启的状态。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from . import hostsops, routeops, tunnelops
from .safeio import log

#: task 表可驱动的同步原语。签名统一为 ``(raw_text: str | None) -> dict``。
SYNC_ACTIONS: dict[str, Callable[[Optional[str]], dict]] = {
    "route.sync": routeops.run_cycle,
    "tunnel.sync": tunnelops.run_tunnel_cycle,
    "hosts.sync": hostsops.run_hosts_cycle,
}


def _next_interval(action: str, state: Optional[dict]) -> Optional[float]:
    """从状态派生下次运行间隔（复刻迁移前的节流语义）。

    - route：跟随意图里的 ``poll``（用户可调，默认 30s）
    - tunnel：待建 utun 期间降到 ``PENDING_POLL``（1s），否则 ``TUNNEL_POLL``（5s）
    - hosts：由 hosts 内部节流决定，不调整
    """
    if not isinstance(state, dict):
        return None
    if action == "route.sync":
        poll = state.get("poll")
        return float(poll) if isinstance(poll, int) and not isinstance(poll, bool) else None
    if action == "tunnel.sync":
        return tunnelops.PENDING_POLL if state.get("pending") else tunnelops.TUNNEL_POLL
    return None


def run_sync(action: str, raw_text: Optional[str]) -> tuple[Optional[dict], Optional[float]]:
    """执行一条 sync 原语。返回 ``(state, next_interval)``。"""
    fn = SYNC_ACTIONS.get(action)
    if fn is None:
        log(f"原语 {action} 尚未接入 task 调度（已跳过）")
        return None, None
    state = fn(raw_text)
    return state, _next_interval(action, state)


# ──────────────────────────────────────────────
# 一次性命令（会话帧 ``op.run`` 的落点）
# ──────────────────────────────────────────────

#: 「现在按**已声明的那份意图**给某 subject 跑一轮」—— 唯一一条**通用**命令。
#:
#: 它取代的是旧的 ``touch .trigger``（敲门）：敲门说的只有「现在跑一轮」，跑什么早就在
#: 意图里；会话通道落地后这一句仍然需要 —— 意图**没变**但现场变了的时候（iPad 链路刚
#: 就绪 / 刚断开、用户按下「立即检查」），光靠重发同一份意图是发不出去的（``declare``
#: 按内容哈希幂等，内容没变就不发帧），而等下一轮节拍又要多等 5~30 秒。
#:
#: 命令里**不带**意图，要收敛到什么在意图里（一个 subject 一个写者这条红线）。
RESYNC = "resync"


def _resync(params, runner) -> dict:
    """``resync`` 的实现：把 subject 交给编排层，让它按已声明的意图跑一轮。

    参数顺序必须跟 ``OP_ACTIONS`` 的统一约定 ``(params, runner)`` 一致 —— ``run_op``
    以 ``fn(params, runner)`` 调用，顺序写反会让 dict 落到 ``runner`` 上，触发
    ``'dict' object has no attribute 'resync'``（见 2026-09-30 那条日志）。
    """
    if runner is None:
        raise ValueError("resync 需要编排层（本侧没有 task 调度）")
    subject = params.get("subject") if isinstance(params, dict) else None
    state = runner.resync(str(subject or ""))
    if not isinstance(state, dict):
        return {"ok": False, "error": "该 subject 这一轮没有产出状态"}
    return state


#: 命令名闭集。**命令名是编译期常量**（与 ``ACTION_SCHEMAS`` 同一条纪律）：
#: 报文里出现闭集之外的 op 一律拒绝，不存在「按名字动态找实现」的分发。
#:
#: 签名统一为 ``(params, runner) -> dict``：``runner`` 是编排层（``tasks.Runner``），
#: 只给 ``resync`` 用 —— 它要的是「现问那份已经收到的意图」，而那只有编排层有。
OP_ACTIONS: dict[str, Callable[..., dict]] = {
    "route.renew": lambda params, runner=None: routeops.run_net_op("renew", params),
    "route.release": lambda params, runner=None: routeops.run_net_op("release", params),
    RESYNC: _resync,
}


def op_names() -> frozenset:
    return frozenset(OP_ACTIONS)


def run_op(op: str, params: Any, runner: Any = None
           ) -> tuple[Optional[dict], Optional[str]]:
    """执行一条一次性命令。返回 ``(result, error)`` —— error 非空即回帧判失败。

    失败**不回滚、不重试**：这类命令（renew / release / resync）改的是系统网络状态，
    重试一次就是又断一次网（见 ``routeops._run_op`` 的理由）。帧级回执让「这次
    到底成没成」当场可见，不再靠对状态文件里的 ``seq`` 水位线。
    """
    fn = OP_ACTIONS.get(op)
    if fn is None:
        log(f"收到未知命令（已拒绝）：{op}")
        return None, f"未知命令：{op!r}（闭集 {sorted(OP_ACTIONS)}）"
    try:
        return fn(params, runner), None
    except Exception as e:  # noqa: BLE001 —— 命令炸掉要如实回帧，不能拖垮主循环
        log(f"命令 {op} 执行异常：{e}")
        return None, str(e) or type(e).__name__
