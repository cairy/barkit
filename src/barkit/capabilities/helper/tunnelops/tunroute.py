"""隧道路由的**增删**：``_add_route_utun`` / ``_del_route_utun``（utun 那条
``/1`` 全流量）与 ``_add_route_host`` / ``_del_route_host``（中继地址那一条）。

``tunnelops.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from ..consts import ROUTE
from ..safeio import log

from . import tunshell


def _add_route_utun(cidr: str, utun: str) -> bool:
    res = tunshell._run([ROUTE, "-n", "add", "-net", cidr, "-interface", utun])
    if res.returncode == 0:
        return True
    err = (res.stderr or res.stdout or "").strip().lower()
    if "file exists" in err or "exists" in err:
        return True                       # 已存在 = 幂等成功
    log(f"加路由 {cidr} -> {utun} 失败：{err[:200]}")
    return False


def _del_route_utun(cidr: str, utun: str) -> bool:
    res = tunshell._run([ROUTE, "-n", "delete", "-net", cidr, "-interface", utun])
    if res.returncode == 0:
        return True
    err = (res.stderr or res.stdout or "").strip().lower()
    if "not in table" in err or "no such" in err:
        return True                       # 本就不存在 = 幂等成功
    log(f"删路由 {cidr} -> {utun} 失败：{err[:200]}")
    return False


def _add_route_host(ip: str, utun: str) -> bool:
    """给 mapdns 地址加一条 /32 主机路由（旁路模式下也必须加）。

    mapdns 是 LwIP 内部的 udp_pcb，**没有任何宿主机 socket**，只能经 utun 访问。
    没有这条路由，即便隧道本身是好的，`dig @198.18.0.2` 也到不了隧道——
    于是「隧道到底通不通」无法在不劫持全量路由的前提下验证，只能赌着全局开启，
    一赌输就整机断网（2026-09-03 实测：全局开启后 288 条 io timeout，全网瘫痪）。

    安全性：198.18.0.0/15 是 RFC 2544 保留的测试网段，承载不了任何本机正常流量，
    加一条 /32 主机路由对其它连接零影响。幂等（已存在视为成功）。
    """
    res = tunshell._run([ROUTE, "-n", "add", "-host", ip, "-interface", utun])
    if res.returncode == 0:
        return True
    err = (res.stderr or res.stdout or "").strip().lower()
    if "file exists" in err or "exists" in err:
        return True
    log(f"加 mapdns 主机路由 {ip} -> {utun} 失败：{err[:200]}")
    return False


def _del_route_host(ip: str, utun: str) -> bool:
    return _del_route_utun(f"{ip}/32", utun)
