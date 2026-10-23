"""DHCP **真租约**的读取与等待（服务端给的那一份，不是网卡当前配置）。

「固化」要照抄的是这一份：读网卡当前配置会读到**自己上次写下去的值**，于是
「重新固化」永远拿不到新地址。

``routeops.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import ipaddress
import time
from ..consts import IPCONFIG

from .ipvalid import valid_ipv4
from . import netmode, routes


# ──────────────────────────────────────────────
# DHCP 租约（**真租约**：服务端给的那一份）
# ──────────────────────────────────────────────

def _packet_value(line: str) -> str:
    """``ipconfig getpacket`` 一行 → 值。

    顶层字段用 ``=``（``yiaddr = 10.199.1.21``），options 段用 ``:``
    （``subnet_mask (ip): 255.255.255.0``），故取**先出现的那个分隔符**。
    """
    for i, ch in enumerate(line):
        if ch in "=:":
            return line[i + 1:].strip()
    return ""


def _mult_addr(text: str) -> list[str]:
    """``{192.168.0.1,198.18.0.2}`` → 地址表（``ip_mult`` 型 option 的写法）。"""
    body = text.strip()
    if body.startswith("{") and body.endswith("}"):
        body = body[1:-1]
    return [a.strip() for a in body.split(",") if valid_ipv4(a.strip())]


def read_lease(iface: str) -> dict | None:
    """这块网卡**从 DHCP 服务端拿到的**租约；读不到返回 ``None``。

    返回 ``{ip, mask, gateway, dns, lease_time, server}``（缺的那几位给空串 / 0）。

    **为什么必须是它，不能拿 ``read_service_config`` 顶**（2026-09-29 定位到的病根）：
    那个读的是「网卡**此刻**的配置」。网卡已经是我们固化过的 Manual 状态时，读回来的
    就是我们上次写下去的那个地址 —— 拿它当「租约」，等于**自我复制**：
    「获取到 IP 后设置为固定 IP」这句话在第一次之后就永远只会抄出同一个地址，
    而表面上一切「成功」。真正的租约只有 ``ipconfig getpacket`` 里有，且网卡不在
    DHCP 模式时该命令**必然失败**（``ipconfig_get_packet(...) failed: not found``）
    —— 这也正是「我们固化着 = 没有租约可读」的判据，不要把它当成错误。
    """
    rc, out = netmode._net_cmd([IPCONFIG, "getpacket", iface], timeout=10)
    if rc != 0:
        return None
    lease: dict = {"ip": "", "mask": "", "gateway": "", "dns": [],
                   "lease_time": 0, "server": ""}
    for raw_line in out.splitlines():
        line = raw_line.strip()
        low = line.lower()
        if low.startswith("yiaddr"):
            lease["ip"] = _packet_value(line)
        elif low.startswith("subnet_mask"):
            lease["mask"] = _packet_value(line)
        elif low.startswith("router"):
            lease["gateway"] = next(iter(_mult_addr(_packet_value(line))), "")
        elif low.startswith("domain_name_server"):
            lease["dns"] = _mult_addr(_packet_value(line))
        elif low.startswith("server_identifier"):
            lease["server"] = _packet_value(line)
        elif low.startswith("lease_time"):
            val = _packet_value(line)
            try:
                lease["lease_time"] = int(val, 16) if val.lower().startswith("0x") \
                    else int(val)
            except ValueError:
                lease["lease_time"] = 0
    if not valid_ipv4(lease["ip"]) or ipaddress.IPv4Address(lease["ip"]).is_unspecified:
        # ``yiaddr = 0.0.0.0`` 是「这一轮还没分到地址」，不是「分到了 0.0.0.0」。
        # 放它过去后果很实在：``_target_of`` 认得出它是合法 IPv4 ⇒ 会把它写进
        # ``-setmanual``，那张网卡就真的收不到地址了（自锁，且回滚窗口探不到这种坏法）。
        return None
    return lease


#: 「恢复 DHCP」等新租约的上限（秒）。一轮内**原子**完成（见 ``_run_op``），
#: 故这段时间会占住本轮；10s 是 DHCP 常规收敛时间的两三倍，且远短于 helper 的轮询。
RENEW_LEASE_TIMEOUT_S = 10.0

#: 轮询新租约的步长（秒）。
RENEW_LEASE_STEP_S = 0.4


def _wait_lease(iface: str, timeout: float = RENEW_LEASE_TIMEOUT_S) -> dict | None:
    """等一个**落到网卡上**的新租约（最多 ``timeout`` 秒），拿不到返回 ``None``。

    两条一起成立才算数，缺一不可：

    - ``ipconfig getpacket`` 里有 ``yiaddr``；
    - 那个地址**确实在网卡的 IPv4 列表里**（``ifconfig``）。

    第二条是**新鲜度**的判据，不是多余的核对：刚 ``setdhcp`` 完，``getpacket`` 有可能
    先吐出**上一份**缓存的报文（甚至就是我们在固化之前那一份），而地址是否已落到网卡上
    只有 ``ifconfig`` 说了算。两者一致 ⇒ 这次租约已经生效 —— 拿它去固化才不会固化出一个
    网卡上压根不存在的地址。
    """
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        lease = read_lease(iface)
        if lease and lease["ip"] in routes.current_ipv4(iface):
            return lease
        if time.monotonic() >= deadline:
            return None
        time.sleep(RENEW_LEASE_STEP_S)
