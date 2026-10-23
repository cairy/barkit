"""**特权子进程那一层**：``_CmdResult`` / ``_run`` 参数化执行（禁 shell 拼串），
以及 utun 查找（``_find_utun``）、IPv4 探测与 DNS 缓存冲刷。

这一层不带任何隧道语义 —— 它只回答「这条命令出来了什么」。

``tunnelops.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import re
import subprocess
from ..consts import IFCONFIG
from ..safeio import log

from . import tunconsts


class _CmdResult:
    def __init__(self, rc: int, out: str = "", err: str = ""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def _run(args: list[str], timeout: int = 15) -> _CmdResult:
    """参数化执行系统命令；异常归一为 returncode=-1。禁止 shell 拼接。"""
    try:
        res = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
        return _CmdResult(res.returncode, res.stdout, res.stderr)
    except (subprocess.SubprocessError, OSError) as e:
        log(f"命令执行异常 {' '.join(map(str, args))}：{e}")
        return _CmdResult(-1, "", str(e))


def _find_utun() -> str | None:
    """扫描接口，返回承载隧道内网地址 TUNNEL_IPV4 的 utunN。"""
    res = _run([IFCONFIG, "-l"])
    if res.returncode != 0:
        return None
    for name in res.stdout.split():
        if not re.match(r"^utun\d+$", name):
            continue
        r2 = _run([IFCONFIG, name])
        if r2.returncode == 0 and tunconsts.TUNNEL_IPV4 in r2.stdout:
            return name
    return None


def _utun_exists(name: str) -> bool:
    """缓存验活：单次 ``ifconfig <name>`` 判「接口在 ＋ 地址仍是 TUNNEL_IPV4」。

    判据与 ``_find_utun`` 逐接口那一遍逐字相同（同一条命令 ＋ 同一个地址子串），
    所以缓存命中得出的结论与全量扫一致，1 fork 代替 5-10 fork。
    只判「接口存在」的后果：接口被销毁后同名被别的软件按别的地址重建时，缓存会留住
    一个地址不对的 utun 名，后续加路由 / 改 DNS 打在错的接口上。地址比对不多花一次
    fork —— ``ifconfig <name>`` 的输出本来就在手上。
    """
    r = _run([IFCONFIG, name])
    return r.returncode == 0 and tunconsts.TUNNEL_IPV4 in r.stdout


def _flush_dns_cache() -> None:
    """切换系统 DNS 后刷新 mDNSResponder 缓存。

    不改缓存的后果（2026-09-03 实测）：系统 DNS 已指向 mapdns，但应用仍持有劫持前
    解析出的**真实 IP** 并直接 connect。hev 收到真实 IP 时无法还原域名，只能以
    纯 IP 发 SOCKS5 CONNECT —— 而 iPad 上游只接受域名（实测纯 IP 一律 rep=4），
    于是「DNS 明明指向了隧道、却依然解析不了也上不了网」。
    必须把旧缓存冲掉，让查询重新走 mapdns 拿 fake IP。
    """
    _run(["/usr/bin/dscacheutil", "-flushcache"], timeout=10)
    _run(["/usr/bin/killall", "-HUP", "mDNSResponder"], timeout=10)


def _has_ipv4(iface: str) -> bool:
    """目标接口是否真的拿到非链路本地 IPv4（= 该网络服务确实在线）。

    链路本地 169.254.0.0/16 不算：Wi-Fi 连上却没拿到地址时系统会自分配一个，
    但那不算「有网」。仅当存在正常单播 IPv4 才认为该服务活跃、可对其进行 DNS 劫持。

    只看 IPv4 是刻意的：DNS 劫持下发的就是 IPv4（mapdns），本机上行不会出现
    纯 IPv6 场景。若将来出现 IPv6-only 上行，这里要同步判 inet6。
    """
    if not iface:
        return False
    res = _run([IFCONFIG, iface])
    if res.returncode != 0:
        return False
    for line in res.stdout.splitlines():
        m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", line)
        if not m:
            continue
        ip = m.group(1)
        if ip.startswith("127.") or ip.startswith("169.254."):
            continue
        return True
    return False
