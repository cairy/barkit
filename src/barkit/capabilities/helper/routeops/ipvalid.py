"""IP / 网段的**严格解析与校验**（全族共用的最低一层，不依赖包内任何东西）。

「IPv4 合不合法」的判据只有这一处 —— 它是安全边界：目标与地址最终会交给
``route`` / ``networksetup`` 去执行，任何一处自己再写一遍「看着像 IP 就放行」
都会把 shell 参数那道门开一条缝。

``routeops.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import ipaddress


def valid_ipv4(text) -> bool:
    try:
        ipaddress.IPv4Address(text)
        return True
    except ipaddress.AddressValueError:
        return False


def valid_destination(text) -> bool:
    """IPv4 网络（前缀或掩码）或 /32 主机。"""
    try:
        ip = ipaddress.ip_network(text, strict=False)
        return ip.version == 4
    except ValueError:
        return False
