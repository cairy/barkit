"""路由表那一层：目标解析、``route -n get`` 的查询与增删（幂等）、以及「接口在不
在、它现在走哪个网关」的信号读取。

``routeops.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import ipaddress
import re
import subprocess
from ..consts import (
    IFCONFIG,
    IPCONFIG,
    ROUTE,
)
from ..safeio import log

from .ipvalid import valid_ipv4


# ──────────────────────────────────────────────
# 目标解析（全部经 ipaddress 严格解析）
# ──────────────────────────────────────────────

def parse_destination(text: str):
    """解析目的地 → (network_addr, network_mask, probe_ip, cidr)。非法返回 None。"""
    try:
        net = ipaddress.ip_network(text, strict=False)
    except ValueError:
        return None
    if net.version != 4:
        return None
    probe = str(net.network_address if net.num_addresses > 1 else net.network_address)
    return {
        "network": str(net.network_address),
        "mask": str(net.netmask),
        "probe": probe,          # route get 的探测目标（用网络号即可命中该前缀）
        "cidr": f"{net.network_address}/{net.prefixlen}",
    }


# ──────────────────────────────────────────────
# route 查询：route -n get <ip> → 解析当前最佳匹配
# ──────────────────────────────────────────────

_ROUTE_GET_KEYS = ("destination", "mask", "gateway", "interface")


def route_get(ip: str) -> dict | None:
    """route -n get，返回 {destination, mask, gateway, interface}；失败 None。"""
    try:
        res = subprocess.run(
            [ROUTE, "-n", "get", ip], capture_output=True, text=True,
            timeout=10, check=False,
        )
    except (subprocess.SubprocessError, OSError) as e:
        log(f"route get {ip} 失败：{e}")
        return None
    if res.returncode != 0:
        return None
    out: dict = {}
    for line in res.stdout.splitlines():
        line = line.strip()
        for key in _ROUTE_GET_KEYS:
            if line.startswith(key + ":"):
                out[key] = line[len(key) + 1:].strip()
    return out or None


def route_via(parsed: dict, gateway: str) -> bool:
    """目标是否已精确经该网关生效（destination+mask+gateway 三者一致才算）。"""
    info = route_get(parsed["probe"])
    if not info:
        return False
    return (
        info.get("destination") == parsed["network"]
        and info.get("mask") == parsed["mask"]
        and info.get("gateway") == gateway
    )


# ──────────────────────────────────────────────
# route 增删（幂等）
# ──────────────────────────────────────────────

def _route(args: list[str], dest_cidr: str, gateway: str) -> bool:
    """参数化调 /sbin/route。已存在/不在表中视为成功（幂等）。"""
    try:
        res = subprocess.run(
            [ROUTE, "-n"] + args + [dest_cidr, gateway],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (subprocess.SubprocessError, OSError) as e:
        log(f"route {args[0]} {dest_cidr} 失败：{e}")
        return False
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip().lower()
        if args[0] == "add" and ("file exists" in err or "exists" in err):
            return True                       # 已存在 = 幂等成功
        if args[0] == "delete" and ("not in table" in err or "no such" in err):
            return True                       # 本就不存在 = 幂等成功
        log(f"route {args[0]} {dest_cidr} -> {gateway} 失败（{res.returncode}）：{err[:200]}")
        return False
    return True


def ensure_route(parsed: dict, gateway: str) -> bool:
    """确保该目标经网关添加。返回成功与否。"""
    if route_via(parsed, gateway):
        return True
    return _route(["add", "-net"], parsed["cidr"], gateway)


def ensure_removed(parsed: dict, gateway: str) -> bool:
    """仅当目标当前确实经该网关生效时删除（其他路径绝不误删）。"""
    if not route_via(parsed, gateway):
        return True
    return _route(["delete", "-net"], parsed["cidr"], gateway)


# ──────────────────────────────────────────────
# 信号：接口状态 + 网关匹配
# ──────────────────────────────────────────────

def iface_up(iface: str) -> bool:
    """ifconfig 有 status: active 视为接口在线。"""
    try:
        res = subprocess.run(
            [IFCONFIG, iface], capture_output=True, text=True,
            timeout=10, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    if res.returncode != 0:
        return False          # 接口不存在
    return bool(re.search(r"\bstatus:\s*active", res.stdout.lower()))


def current_gateways(iface: str) -> list[str]:
    """
    接口当前网关列表。优先 ipconfig getpacket（DHCP router 行，可能多条）；
    静态 IP 接口 getpacket 无返回 → 降级 route -n get default 取默认网关
    （方案 §4.3 网关检测兜底）。
    """
    gw: list[str] = []
    try:
        res = subprocess.run(
            [IPCONFIG, "getpacket", iface], capture_output=True, text=True,
            timeout=10, check=False,
        )
        if res.returncode == 0:
            for m in re.finditer(r"router \(ip_mult\):\s*\{([^}]*)\}", res.stdout):
                for addr in m.group(1).split(","):
                    addr = addr.strip()
                    if valid_ipv4(addr):
                        gw.append(addr)
    except (subprocess.SubprocessError, OSError):
        pass
    if not gw:
        info = route_get("default")
        if info and valid_ipv4(info.get("gateway", "")):
            gw.append(info["gateway"])
    return gw


def current_ipv4(iface: str) -> list[str]:
    """接口上的 IPv4 地址列表（``ifconfig <iface>`` 的 ``inet`` 行）。

    只认 IPv4：``ifconfig`` 同一接口下面紧跟着还会列 ``inet6``，用 ``inet\\b`` 之类的
    写法会把它一起吃掉（``6`` 是词字符，``\\b`` 挡不住），故显式要求 ``inet`` 后面
    紧跟空白。地址本身仍过一遍 ``valid_ipv4``，双保险。

    这是**查询**（§2.1「生效网段」判定的数据源），不是判定：落在不在网段里由
    ``intent.network_probe`` 决定 —— 与 ``iface_up`` / ``current_gateways`` 同一层。
    """
    addrs: list[str] = []
    try:
        res = subprocess.run(
            [IFCONFIG, iface], capture_output=True, text=True,
            timeout=10, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return addrs
    if res.returncode != 0:
        return addrs          # 接口不存在
    for m in re.finditer(r"(?m)^\s+inet\s+([0-9.]+)\s", res.stdout + " "):
        addr = m.group(1)
        if valid_ipv4(addr):
            addrs.append(addr)
    return addrs
