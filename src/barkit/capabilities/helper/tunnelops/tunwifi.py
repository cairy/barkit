"""Wi-Fi 侧的**服务名解析与切换**：``_service_for_device`` / ``_network_service``
（本地化，服务名不能写死 ``"Wi-Fi"``）与 ``_switch_wifi`` 的切换语义，外加
``_resolve_network_targets`` 的端口归集。

``tunnelops.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from .. import routeops
from ..safeio import log

from . import tunconsts
from . import tunshell


# ──────────────────────────────────────────────
# 系统操作（Wi-Fi / DNS / 路由 / hev）
# ──────────────────────────────────────────────

_WIFI_PORT_HINTS = ("wi-fi", "wifi", "wlan", "airport")


def _service_for_device(iface: str) -> str | None:
    """按设备名（en0）反查 networksetup 的「服务名」。

    实现只有一份，在 ``routeops.service_for_iface``（那里是「网卡配置」作用域的
    入口，也是本模块的下层）—— 同一件事在两处各写一遍，改了输出格式就会只改到一边。

    networksetup 的服务名在非英文系统下是本地化的（德语 WLAN 等），而设备名
    永远不受本地化影响，所以一律走「设备名 → 服务名」这条路，绝不写死 "Wi-Fi"。
    """
    return routeops.service_for_iface(iface)


def _primary_iface() -> str | None:
    """承载默认流量的物理接口（通常 en0）。

    优先问默认路由；拿不到就退回按硬件端口找 Wi-Fi 设备。返回的设备名供
    _service_for_device() 反查服务名，全程不依赖本地化的端口名文案。
    """
    info = routeops.route_get("default")
    iface = (info or {}).get("interface")
    if iface and not iface.startswith("utun"):
        return iface
    res = tunshell._run(["/usr/sbin/networksetup", "-listallhardwareports"])
    if res.returncode != 0:
        return None
    cur = None
    for line in res.stdout.splitlines():
        line = line.strip()
        low = line.lower()
        if low.startswith("hardware port:"):
            cur = line.split(":", 1)[1].strip()
        elif low.startswith("device:") and cur:
            if cur.lower() in _WIFI_PORT_HINTS:
                return line.split(":", 1)[1].strip()
    return None


def _network_service() -> str | None:
    """要对其做 DNS / IPv6 操作的 networksetup 服务名；**读不出返回 ``None``**。

    硬编码 "Wi-Fi" 在非英文系统下会静默失败：IPv4 走了隧道、DNS 仍走本机，
    正好是 disable_ipv6 想防的那类泄漏。

    读不出时**不再兜一个猜来的名字**：networksetup 的服务名是本地化的，猜 "Wi-Fi"
    在非英文系统、或默认出口根本不是 Wi-Fi 的机器上会把 DNS / IPv6 写到**另一个**
    网络服务上 —— 那个服务日后启用时就指向一个早已不存在的 mapdns（198.18.0.2），
    而这种误伤现场毫无痕迹。本模块对改网卡的口径一直是「没有依据就不动手」
    （同 ``routeops.service_for_iface`` / ``read_service_config``），这里对齐它：
    调用方拿到 ``None`` 就跳过该项改动，并把 ``state["service_unknown"]`` 如实上屏。
    """
    return tunconsts._TUNNEL_SERVICE


def _resolve_network_targets() -> None:
    """在改动路由/DNS 之前把「目标网络服务」定住并缓存。

    必须在加 /1 路由**之前**调用：隧道一旦生效，route -n get default 就可能
    返回 utun9 而不是物理网卡，此时再解析就会把 DNS 改到隧道接口上去。

    读不出服务名时缓存的是 ``None``，于是**每次重激活**（hev 死亡重建、链路翻转后
    重拉）都会再试一次 —— 单次解析失败不会锁死整个会话，代价只是那一轮不接管。
    """
    if tunconsts._TUNNEL_WIFI_IFACE is None:
        tunconsts._TUNNEL_WIFI_IFACE = _primary_iface()
    if tunconsts._TUNNEL_SERVICE is None:
        tunconsts._TUNNEL_SERVICE = _service_for_device(tunconsts._TUNNEL_WIFI_IFACE) if tunconsts._TUNNEL_WIFI_IFACE else None
        log(f"网络目标已解析：设备={tunconsts._TUNNEL_WIFI_IFACE or '?'} 服务={tunconsts._TUNNEL_SERVICE or '（读不出）'}")


def _switch_wifi(ssid: str) -> bool:
    """切到本机已配置/已知的 Wi-Fi 网络（**不带密码**）。

    目标 SSID 须已在钥匙串/首选列表；未配过则失败（属预期前提）。
    """
    iface = tunconsts._TUNNEL_WIFI_IFACE or _primary_iface()
    if not iface:
        log("未找到 Wi-Fi 接口，无法切换网络")
        return False
    tunconsts._TUNNEL_WIFI_IFACE = iface
    res = tunshell._run(["/usr/sbin/networksetup", "-setairportnetwork", iface, ssid])
    ok = res.returncode == 0
    if not ok:
        log(f"切换 Wi-Fi 到 {ssid!r} 失败（{res.stderr or res.stdout or ''}）；"
            "目标须为本机已配置/已知的网络")
    return ok


def _run_wifi(ssid) -> dict | None:
    """执行固定语义的 Wi-Fi 切换。ssid 为 None（不切）时什么都不做。"""
    if not ssid:
        return None
    ok = _switch_wifi(ssid)
    return {"wifi": ssid, "ok": ok}
