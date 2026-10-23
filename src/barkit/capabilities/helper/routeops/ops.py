"""**一次性显式动作**（会话帧 ``op.run`` 的落点）：renew / release 与「交还系统」。

它们是**命令**不是策略 —— 按下那一次就执行一次，不受停用影响，且要给回执。

``routeops.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import time
from ..state import (
    read_state,
    write_state,
)

from .baseline import _baseline_dns, _same, _write_target, ensure_baseline
from .converge import _iface_gateway, _iface_gateways
from . import gwledger
from . import lease as lease_pkg
from .lease import RENEW_LEASE_TIMEOUT_S
from . import netmode
from .netmode import MODE_DHCP, NETWORKSETUP, _set_dns, _target_of
from . import netsvc
from .netsvc import service_for_iface
from .validate import validate_op_params


# ──────────────────────────────────────────────
# 一次性显式动作（renew / release）：会话帧 ``op.run`` 的落点
# ──────────────────────────────────────────────


def _set_dhcp(service: str) -> bool:
    """切回自动获取（交还的地址那半）。"""
    return netmode._net_cmd([NETWORKSETUP, "-setdhcp", service])[0] == 0


def _apply_op(op: dict, now: int) -> dict:
    """执行一条命令，返回进 ``state["net"]`` 的段，同时**它就是回帧的正文**。

    ``renew`` 的全部意义在**一轮内原子完成**（2026-09-29 用户裁定）：

    1. 先真的交还给系统（``-setdhcp``，可选连 DNS 一起还原）—— 这一步就是「恢复 DHCP」；
    2. 等**新租约**落到网卡上（``_wait_lease``，最多 ``RENEW_LEASE_TIMEOUT_S`` 秒）；
    3. 用**这份新租约**重算目标并固化（``_target_of`` ＋ ``_write_target``），并把本场景
       给这块网卡定下来的网关记进 ``iface_gateway``（留给留空的下一跳，见 ``run_net_op``）。

    为什么必须挤在一轮里：交还之后网卡上短暂没有地址，而「命中哪个场景」在 app 侧是
    按网卡地址判的 —— 拆成两轮就有一条**没人负责**的中间窗口（app 那一秒可能算出
    「没场景命中」而把 ``net`` 撤掉）。一轮内跑完，期望的目标在同一份意图里，不会漂。

    ⚠️ 代价是这一轮最坏占住 ``RENEW_LEASE_TIMEOUT_S`` 秒。用在这个动作上是划算的：
    它是用户当场按的，且只按一次。

    **一次就是一次**：这条命令的「只执行一次」现在由**帧**保证（一帧一命令、回帧即
    回执），不需要 ``seq`` 水位线，也不需要「重做一次就是又一次断网」那种靠记账救的
    保护 —— 没有账可记，也就没有账可错。

    **失败不回滚**：交还失败就停在原地、如实回帧；调用方（app）拿到的是一句明确的
    错误，而不是「命令排进队列、等下一轮看状态文件里有没有回执」。
    """
    iface, kind = op["iface"], op["op"]
    res = {"iface": iface, "service": None, "mode": "", "managed": False,
           "wrote": None, "net": op.get("net"), "action": "none", "error": None,
           "op": kind, "applied_at": now}

    def _fail(msg: str) -> dict:
        res["error"] = msg
        res["action"] = "failed"
        return res

    service = service_for_iface(iface)
    if not service:
        return _fail(f"查不到 {iface} 对应的网络服务名，未动网卡")
    current = netsvc.read_service_config(service)
    if current is None:
        return _fail(f"读不出 {service} 的当前配置，未动网卡")
    if not ensure_baseline(service, iface, current):
        return _fail(f"{service} 的 baseline 留不下来，未动网卡")
    res["service"] = service

    # ① 真的交还（与「恢复 DHCP」等价，也是拿到新租约的前提）
    if not _set_dhcp(service):
        return _fail(f"把 {service} 切回 DHCP 失败")
    if op.get("dns"):
        _set_dns(service, _baseline_dns(service))
    res["mode"] = MODE_DHCP
    res["action"] = "handback"
    res["result"] = "dhcp"
    if kind == "release" or not isinstance(op.get("net"), dict):
        return res                       # 只交还：停在 DHCP 就完了

    # ② 等网络里那个 DHCP 服务端把**新**租约发下来
    lease = lease_pkg._wait_lease(iface)
    if lease is None:
        return _fail(f"{iface} 已交还给 DHCP，但 {RENEW_LEASE_TIMEOUT_S:.0f} 秒内"
                     f"没等到新租约（网卡现在仍是自动获取）")
    res["ip"] = str(lease["ip"])

    # ③ 用这份新租约重算目标并固化（「获取到 IP 后设置为固定 IP」的字面实现）
    target, terr = _target_of(op["net"], lease)
    if terr:
        return _fail(f"{service}：{terr}")
    if target is None:
        return res                       # 意图本来就是留在 DHCP
    if not _same(netsvc.read_service_config(service), target):
        if not _write_target(service, target):
            return _fail(f"写 {service} 的网卡配置失败")
        if target.get("dns"):
            _set_dns(service, list(target["dns"]))
    # 本场景给这块网卡定下来的网关（v8）：周期那条路在 ``_converge_iface`` 里记同一栏，
    # 这条**一次性的**路必须一并记。漏了它的症状 2026-10-09 在真机上出现过 ——
    # 「恢复 DHCP」刚把网关读到手就固化（只写地址），值被丢掉，而网卡已切成 Manual、
    # ``ipconfig getpacket`` 从此读不到 ⇒ 面板恒报「未取到网关」，留空的下一跳一直不动。
    res["iface_gateway"] = _iface_gateway(target, lease)
    res.update(mode=target["mode"], managed=True, wrote=target,
               action="applied", error=None, result="pinned")
    return res


def run_net_op(kind: str, params) -> dict:
    """会话帧 ``op.run`` 的入口：校验参数 → 执行 → 把结论并进最近一次观测。

    ``kind`` 来自帧的 ``op``（``route.renew`` / ``route.release``）；``params`` 是帧
    自带的参数（形状见 :func:`validate_op_params`）。

    两条边界：

    - 参数非法直接抛 ``ValueError``（调用方 ``primitives.run_op`` 把它翻成回帧里的
      ``error``）—— 这条路径会改网卡，宁可拒绝也不猜。
    - 执行完**把这一段写进观测**：面板要看得见「刚才那次动作的结果」。这一步顺带触发
      一次推送，订阅方不必等下一轮。
    - 顺带**更新网卡网关那本账**（``converge._iface_gateways``）：这条路上读到的网关与
      周期那条路一样要留下来 —— 网卡一被固化就读不到租约了，而留空的下一跳每轮都要它。
    """
    clean, err = validate_op_params(params)
    if err is not None:
        raise ValueError(err)
    if clean["op"] != kind:
        raise ValueError(f"命令名与参数不一致：{kind} vs {clean['op']}")
    seg = _apply_op(clean, int(time.time()))
    current = read_state()
    if current:
        current["net"] = seg
        # 网卡的网关那本账跟着更新（与 ``run_cycle`` 同一个口径、同一个函数）：这条路上
        # 读到的网关同样要留下来 —— 网卡一被固化就再也读不到了（``converge._iface_gateways``）。
        # 学到就落盘（``gwledger``）：账不寄生在观测块里，才不会被某一轮的整块替换抹平。
        known = gwledger.load()
        known.update(current.get("net_gateways") or {})
        current["net_gateways"] = _iface_gateways(known, seg)
        gwledger.save(current["net_gateways"])
        write_state(current)
    return seg
