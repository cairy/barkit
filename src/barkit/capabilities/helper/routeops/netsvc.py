"""「网络服务名 ↔ BSD 网卡名」的换算与当前配置读取（``networksetup`` 那一侧）。

``routeops.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from ..safeio import log

from .ipvalid import valid_ipv4
from . import netmode
from .netmode import NETWORKSETUP, _NET_MODES


def service_for_iface(iface: str) -> str | None:
    """设备名（en0）→ networksetup 的「服务名」。

    绝不写死 ``"Wi-Fi"``：服务名在非英文系统下是本地化的（德语 WLAN…），而设备名
    永远不受本地化影响，所以一律走「设备名 → 服务名」这条路。返回 ``None`` ＝ 查不到
    ⇒ 调用方**一律不动**这块网卡（fail-closed）。
    """
    if not iface:
        return None
    rc, out = netmode._net_cmd([NETWORKSETUP, "-listallhardwareports"])
    if rc != 0:
        return None
    cur = None
    for line in out.splitlines():
        line = line.strip()
        low = line.lower()
        if low.startswith("hardware port:"):
            cur = line.split(":", 1)[1].strip()
        elif low.startswith("device:") and cur:
            if line.split(":", 1)[1].strip() == iface:
                return cur
    return None


def read_service_config(service: str) -> dict | None:
    """``networksetup -getinfo <service>`` → ``{mode, ip, mask, gateway, dns}``。

    读不出来（命令失败 / **认不出模式**）返回 ``None``，调用方必须把它当成「拒绝改动」
    而不是「按空配置改」—— 这条路径会改网卡，没有依据就不动手。

    ``-getinfo`` 的字段名**大小写随模式变**（``Manual Configuration`` 段写
    ``IP Address:``、``DHCP Configuration`` 段写 ``IP address:``），故一律小写比对。
    ⚠️ 该输出在非英文系统上可能整体本地化 ⇒ 那时本项目会**拒绝改动网卡**（安全侧），
    这是已知限制，列入真机验证。
    """
    rc, out = netmode._net_cmd([NETWORKSETUP, "-getinfo", service])
    if rc != 0:
        return None
    info = {"mode": "", "ip": "", "mask": "", "gateway": "", "dns": []}
    for raw in out.splitlines():
        line = raw.strip()
        low = line.lower()
        if low.startswith("manual configuration"):
            info["mode"] = "static"
        elif low.startswith("dhcp configuration"):
            info["mode"] = "dhcp"
        elif low.startswith("ip address:"):
            info["ip"] = line.split(":", 1)[1].strip()
        elif low.startswith("subnet mask:"):
            info["mask"] = line.split(":", 1)[1].strip()
        elif low.startswith("router:"):
            info["gateway"] = line.split(":", 1)[1].strip()
    if info["mode"] not in _NET_MODES:
        log(f"读不出 {service} 的配置模式（输出可能被本地化），拒绝改动该网卡")
        return None
    rc, dns_out = netmode._net_cmd([NETWORKSETUP, "-getdnsservers", service])
    if rc == 0:
        info["dns"] = [s.strip() for s in dns_out.splitlines()
                       if valid_ipv4(s.strip())]
    return info
