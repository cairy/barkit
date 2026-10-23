"""网卡配置的**规格层**：三态常量（``MODE_*``）与它们的字段表、``validate_net``，
以及「目标值怎么算出来」（``_target_of``）与「怎么下发」（``_net_cmd`` / ``_set_dns``）。

判据与 ``rules.validate_net`` 同源 —— helper 侧不再写第二份「哪些键算合法」。

``routeops.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import ipaddress
import subprocess
from ..safeio import log

from .ipvalid import valid_ipv4


# ──────────────────────────────────────────────
# 网卡配置（§2.5 的配套三件事：baseline 快照 / 回滚窗口 / 交还系统）
# ──────────────────────────────────────────────

#: networksetup 绝对路径（同 consts 口径：LaunchDaemon 环境 PATH 干净）。
NETWORKSETUP = "/usr/sbin/networksetup"


#: ``net`` 落盘后的**运行形态**（比配置里的 ``mode`` 多一格）：
#: ``static`` / ``dhcp+pin`` 落到系统上都是「切 Manual + 写死地址」，差别只在它从哪来。
#: ``dhcp+pin`` 是**运行态**而不是配置里的第三个 ``mode``（§2.5 那条修正记录）。
MODE_STATIC = "static"
MODE_DHCP_PIN = "dhcp+pin"
MODE_DHCP = "dhcp"

_NET_MODES = (MODE_STATIC, MODE_DHCP)
_NET_FIELDS = ("mode", "ip", "mask", "gateway", "dns", "pin")
_PIN_FIELDS = ("ip", "mask", "gateway", "dns",
               # 「照抄租约」（§2.5 的补充）：只写 true，与同名的写死值互斥。
               "gateway_from_lease", "dns_from_lease")
#: 「照抄租约」的两个开关（键名 → 它们与哪个写死值互斥）。
_PIN_LEASE_FLAGS = (("gateway_from_lease", "gateway", "网关"),
                    ("dns_from_lease", "dns", "DNS"))


def _net_cmd(args, timeout: int = 15) -> tuple[int, str]:
    """参数化执行；异常归一为 ``(-1, "")``。禁止 shell 拼接（同本模块其余命令）。"""
    try:
        res = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False,
        )
        return res.returncode, res.stdout or ""
    except (subprocess.SubprocessError, OSError) as e:
        log(f"命令执行异常 {' '.join(map(str, args))}：{e}")
        return -1, ""


def _valid_dns_list(v) -> bool:
    return isinstance(v, list) and all(
        isinstance(x, str) and valid_ipv4(x) for x in v
    )


def validate_net(net) -> tuple[dict | None, str | None]:
    """``net``：本场景命中时把网卡配成什么（§2.1 / §2.5）。返回 ``(clean, error)``。

    缺省（不写 / ``None``）= **一根手指都不碰网卡**（纯 DHCP 跟随）—— 不是「配成 DHCP」，
    这两件事在用户看来一样、在代码里差很远：前者不产生任何写入，也不会留 baseline。

    - ``static``：``ip`` / ``mask`` 必填，``gateway`` / ``dns`` 可选；**不得带 ``pin``**
      （固化是 DHCP 的修饰，不是 static 的兄弟，§2.5 那条修正记录）；
    - ``dhcp``：不得直接写 ``ip`` / ``mask`` / ``gateway``（那是固化，走 ``pin``）；
      可选 ``pin`` —— 两个**键**必须都在（值可以留空 =也用 DHCP 拿到的那个值），
      这正是「获取到 IP 后设置为我指定的固定 IP」的字面实现：用户不必预先知道地址。

    非法一律**整份拒绝**（与本模块其余 schema 同一纪律）：这条路径会改网卡，
    宁可不动，也不能猜。
    """
    if net is None:
        return None, None
    if not isinstance(net, dict):
        return None, f"net 必须是对象：{net!r}"
    unknown = set(net) - set(_NET_FIELDS)
    if unknown:
        return None, f"net 含未知字段：{sorted(unknown)}"
    mode = net.get("mode", MODE_DHCP)
    if mode not in _NET_MODES:
        return None, f"net.mode 只能是 {list(_NET_MODES)}：{mode!r}"
    if mode == MODE_DHCP:
        bad = [k for k in ("ip", "mask", "gateway") if k in net]
        if bad:
            return None, f"net.mode=dhcp 不得直接写 {bad}（固化请用 net.pin）"
        if "dns" in net:
            # 收紧到与 app 侧 ``rules.validate_net`` **同一份口径**（那边 dhcp 只允许
            # ``{mode, pin}``）：裸 DNS 在 DHCP 下没有归宿 —— 固化那份是 ``pin.dns``，
            # 「跟随 DHCP」则压根不该由我们写。两处口径不一致时，会出现「app 拒绝、
            # helper 接受」这种只有一边说得通的配置，排查时最难查。
            return None, "net.mode=dhcp 不得直接写 dns（固化请用 net.pin.dns）"
    dns = net.get("dns")
    if dns and not _valid_dns_list(dns):
        return None, f"net.dns 含非法 IPv4：{dns!r}"

    out: dict = {"mode": mode}
    if mode == "static":
        for req in ("ip", "mask"):
            val = net.get(req)
            if not isinstance(val, str) or not valid_ipv4(val):
                return None, f"net.mode=static 的 {req} 必填且要是合法 IPv4：{val!r}"
            out[req] = val
        gw = net.get("gateway")
        if gw:
            if not valid_ipv4(gw):
                return None, f"net.gateway 不是合法 IPv4：{gw!r}"
            out["gateway"] = gw
        if "pin" in net:
            return None, "net.mode=static 不得再写 pin（固化是 DHCP 的修饰，§2.5）"
        if dns:
            out["dns"] = list(dns)
        return out, None

    pin = net.get("pin")
    if pin is not None:
        if not isinstance(pin, dict):
            return None, f"net.pin 必须是对象：{pin!r}"
        unknown_pin = set(pin) - set(_PIN_FIELDS)
        if unknown_pin:
            return None, f"net.pin 含未知字段：{sorted(unknown_pin)}"
        # ``ip`` / ``mask`` 要求的是**键**必填，值可以留空（= 用 DHCP 拿到的那个）。
        # 这正是 §2.5 「获取到 IP 后设置为固定 IP」的字面实现：用户不必预先知道地址。
        # 与 app 侧 ``rules.validate_net`` 同一口径（那里原先错写成「值必填」，见该函数）。
        clean_pin: dict = {}
        for req in ("ip", "mask"):
            if req not in pin:
                return None, f"net.pin 必须两个键都在：缺 {req}（值可留空 = 用 DHCP 的）"
            val = pin.get(req)
            if val and not isinstance(val, str):
                return None, f"net.pin.{req} 非法：{val!r}"
            if val and not valid_ipv4(val):
                return None, f"net.pin.{req} 不是合法 IPv4：{val!r}"
            clean_pin[req] = val or ""
        gw = pin.get("gateway")
        if gw:
            if not valid_ipv4(gw):
                return None, f"net.pin.gateway 不是合法 IPv4：{gw!r}"
            clean_pin["gateway"] = gw
        pin_dns = pin.get("dns")
        if pin_dns and not _valid_dns_list(pin_dns):
            return None, f"net.pin.dns 含非法 IPv4：{pin_dns!r}"
        if pin_dns:
            clean_pin["dns"] = list(pin_dns)
        # 「照抄租约」（§2.5 的补充）：留空是**不固化**，照抄是**把那一位也固化**。
        # 两件事分得开才有必要存在第二个键 —— 所以只认 ``true``：
        # ① 写 ``false`` 与不写完全等价，允许多一条读起来像有别的语义的路；
        # ② 与同名的写死值**互斥**（一个照抄、一个写死，同时给说明用户自己也没想清），
        #    宁可整份拒绝，也不猜哪个优先 —— 这条路径会改网卡。
        for flag, val_key, label in _PIN_LEASE_FLAGS:
            val = pin.get(flag)
            if val is None:
                continue
            if val is not True:
                return None, f"net.pin.{flag} 只能写 true（不想照抄就整条不写）：{val!r}"
            if pin.get(val_key):
                return None, (f"net.pin 的{label}不能既写死 {val_key} 又照抄租约"
                              f"（{flag}: true）：二者只能选一个")
            clean_pin[flag] = True
        out["pin"] = clean_pin
    if dns:
        out["dns"] = list(dns)
    return out, None


def _set_dns(service: str, servers: list) -> bool:
    if servers:
        rc, _ = _net_cmd([NETWORKSETUP, "-setdnsservers", service] + list(servers))
    else:
        rc, _ = _net_cmd([NETWORKSETUP, "-setdnsservers", service, "empty"])
    return rc == 0


def _in_subnet(ip: str, mask: str, other: str) -> bool:
    """``other`` 是否落在 ``ip/mask`` 这个网段里（解析不了就答否）。"""
    try:
        net = ipaddress.ip_network(f"{ip}/{mask}", strict=False)
        return ipaddress.IPv4Address(other) in net
    except ValueError:
        return False


def _target_of(net: dict, lease: dict) -> tuple[dict | None, str | None]:
    """``net`` ＋ 当前（租约）配置 → **要写到网卡上的那一份**。返回 ``(target, err)``。

    写与比对共用同一份规范化，免得「写下去的是 A、比的是 B」。

    - ``(None, None)``：纯 DHCP —— 一根手指都不碰（§2.5）；
    - ``(target, None)``：``target["mode"]`` 取**运行形态**（``static`` / ``dhcp+pin``）；
    - ``(None, err)``：这份 net 落地就会写坏网卡 ⇒ 调用方**不许写**，报错进状态。

    「留空 ＝ 用 DHCP 拿到的值」**只对 ``pin`` 的 ``ip`` / ``mask`` 成立**。
    ``pin.gateway`` / ``pin.dns`` 留空是**不固化**；要连那两位一起固化，得显式写
    ``gateway_from_lease`` / ``dns_from_lease``（2026-09-18 用户裁定补的能力）。

    为什么留空不能顺手当成「照抄」：那会**静默改掉现有配置的含义** —— 已经写着
    ``pin: {ip: '', mask: ''}`` 的场景会突然开始钉网关，而用户一个字都没动过。
    布尔键不写就完全是老行为，对已有场景零影响。反过来，勾了照抄而租约里压根没有那
    一位（或读到 ``(null)`` 这种本地化残留）⇒ **当成没这项**，不编值。

    另加一条**落笔前**的硬拒绝：网关若不在本网段内，一律不写。那种配置会把这块网卡
    变成一张出不去也进不来的网，而回滚窗口**探不到这种坏法**（地址确实在网卡上、
    默认路由看着也正常）—— 既然探不到，就只能在写之前拦住。
    """
    if not isinstance(net, dict):
        return None, f"net 必须是对象：{net!r}"
    mode = net.get("mode")
    if mode == MODE_STATIC:
        ip = str(net.get("ip") or "")
        mask = str(net.get("mask") or "")
        if not (valid_ipv4(ip) and valid_ipv4(mask)):
            return None, f"static 的 ip / mask 不可用：{ip!r} / {mask!r}"
        gateway = str(net.get("gateway") or "")
        if gateway and not _in_subnet(ip, mask, gateway):
            return None, f"网关 {gateway} 不在 {ip}/{mask} 网段内（写下去这块网卡就出不去）"
        return {"mode": MODE_STATIC, "ip": ip, "mask": mask, "gateway": gateway,
                "dns": list(net.get("dns") or [])}, None
    if mode != MODE_DHCP:
        return None, f"net.mode 认不出：{mode!r}"
    pin = net.get("pin")
    if not isinstance(pin, dict):
        return None, None                       # 纯 DHCP：不写
    lease = lease if isinstance(lease, dict) else {}
    ip = str(pin.get("ip") or lease.get("ip") or "")
    mask = str(pin.get("mask") or lease.get("mask") or "")
    if not (valid_ipv4(ip) and valid_ipv4(mask)):
        # pin 把地址留空 = 「用 DHCP 拿到的」，那就得真拿得到；拿不到绝不猜
        return None, f"固化要靠租约补地址，但租约里没拿到可用的 ip / mask：{ip!r} / {mask!r}"
    gateway = str(pin.get("gateway") or "")
    if not gateway and pin.get("gateway_from_lease"):
        leased = str(lease.get("gateway") or "")
        gateway = leased if valid_ipv4(leased) else ""
    if gateway and not _in_subnet(ip, mask, gateway):
        return None, f"固化网关 {gateway} 不在 {ip}/{mask} 网段内（写下去这块网卡就出不去）"
    dns = list(pin.get("dns") or [])
    if not dns and pin.get("dns_from_lease"):
        dns = [s for s in (lease.get("dns") or []) if valid_ipv4(s)]
    return {"mode": MODE_DHCP_PIN, "ip": ip, "mask": mask, "gateway": gateway,
            "dns": dns}, None
