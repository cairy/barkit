"""ipad_tunnel/tunnel_spec：插件配置段 → 隧道**意图正文**的转换与校验。

- 纯函数：把插件配置段校验后转成一份 dict，交给 ``declare("tunnel", …)`` 声明；
  **本模块不落盘**（旧形态里它写 ``tunnel.json``，那个文件已随通道一起消失）。
- schema 与 helper 侧同一套（app 先校验，避免把坏意图发出去；helper 端校验是最后防线）。
- 校验失败抛 TunnelConfigError，由 UI 提示。

结构镜像 route_guard/rules.py（build_routes / RouteConfigError）。
"""

from __future__ import annotations

import ipaddress
import logging

log = logging.getLogger(__name__)

DEFAULT_LOCAL_PORT = 1080
DEFAULT_DEVICE_PORT = 9001

# 隧道意图的线格式版本。两侧各写一个具名常量（本处与 helper 的 ``tunnelops.TUNNEL_WIRE_VERSION``），
# 握手时对账 —— 版本不符就是「两侧不同代」，由会话在握手阶段指出，比从前「读到未知字段整份拒」
# 强的地方是能直接说清是哪一侧旧。
# 1 → 2（2026-09-29）：**去掉 ``link_up`` / ``ignore_link``**（helper 侧从 v2 起把它们
# 从白名单删掉）。理由：那两位是**运行期事实**（iPad 那条链路此刻通不通），而这份意图是
# **期望态**；事实混进期望态的代价是「写方只在自己翻转时落笔 ⇒ 被第三方改写后永不纠正」，
# 而 helper 拿着过期的实事去建隧道就是一条通往黑洞的路。新载体是可观测的事实本身：app 的
# 中继只在链路就绪时监听，helper 探那个端口就知道上游在不在。
TUNNEL_WIRE_VERSION = 2

# hev 的 mapdns 是 LwIP 内部的 udp_pcb，没有任何宿主机 socket；系统 DNS 必须
# 指向这个地址，DNS 才会被隧道接管。helper 侧既把它写进 yml 的 mapdns.address，
# 也把它设为系统 DNS —— 两处同源，改这里即同步生效（不再是死配置）。
DEFAULT_DNS = "198.18.0.2"
# 旧版插件配置的通用动作列表（唯一合法类型）。新配置统一为固定 `wifi` 字段，
# 旧列表仅由 _legacy_wifi_ssid() 读取并转换——root 侧（helper）已无 type 解析。
LEGACY_ACTION_TYPE = "switch_wifi"
WIFI_KEYS = ("on_activate", "on_deactivate")


class TunnelConfigError(Exception):
    """隧道配置非法。"""


def valid_port(value, what: str) -> int:
    if not isinstance(value, int) or not 1 <= value <= 65535:
        raise TunnelConfigError(f"{what} 非法（需 1..65535）：{value!r}")
    return value


def valid_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
        return True
    except (ipaddress.AddressValueError, ValueError):
        return False


def validate_wifi_ssid(value, key: str) -> str | None:
    """校验 wifi.<key>：None/"" = 不切；非空字符串 = 目标 SSID（不托管密码）。"""
    if value is None:
        return None
    if isinstance(value, str):
        ssid = value.strip()
        return ssid or None
    raise TunnelConfigError(f"wifi.{key} 必须是 null 或非空字符串：{value!r}")


def _legacy_wifi_ssid(actions, key: str) -> str | None:
    """旧版 `on_activate: [{type: switch_wifi, ssid: X}]` → SSID 字符串（兼容转换）。

    只认首个 `switch_wifi` 条目；未知 type、缺 ssid、带 password 一律拒绝——
    旧结构里的非法项不静默吞掉，避免「配置写歪了但行为没解释」。
    """
    if actions is None:
        return None
    if not isinstance(actions, list):
        raise TunnelConfigError(f"{key} 必须是数组（旧格式）或改用 wifi.{key}")
    for i, a in enumerate(actions):
        if not isinstance(a, dict):
            raise TunnelConfigError(f"{key}[{i}] 必须是对象")
        atype = a.get("type")
        if atype != LEGACY_ACTION_TYPE:
            raise TunnelConfigError(f"{key}[{i}].type 未知：{atype!r}（旧格式仅支持 {LEGACY_ACTION_TYPE}）")
        if "password" in a or "pwd" in a:
            raise TunnelConfigError(f"{key}[{i}] 不允许携带 password/pwd 字段")
        ssid = a.get("ssid")
        if not isinstance(ssid, str) or not ssid.strip():
            raise TunnelConfigError(f"{key}[{i}].ssid 缺失或为空")
        return ssid.strip()
    return None


def build_tunnel(plugin_cfg: dict) -> dict:
    """插件配置段 → 隧道意图正文。非法抛 TunnelConfigError。

    - enabled：用户开关（是否希望隧道启用）；默认 False（与 config.default.yaml 一致）
    - disable_ipv6：fail-closed 开关（默认 True）→ 激活期禁用 Mac Wi-Fi 的 IPv6
    - hijack_route：是否抢全量 /1 路由（**默认 False**）
    - hijack_dns：是否改系统 DNS 为 hev mapdns（**默认 False**）
    - wifi：固定语义的 Wi-Fi 切换字段 {"on_activate": ssid|null, "on_deactivate": ssid|null}；
      旧配置的动作列表在 build_tunnel() 兼容转换为该字段，root 侧无 type 解析

    ⚠️ **``link_up`` / ``ignore_link`` 不在这里**（v2 起）：链路通不通是**运行期事实**，
    不进这份**意图**（理由见 ``TUNNEL_WIRE_VERSION``）。``ignore_link`` 仍是本插件的
    一个**配置键**（「手动模式」：链路没就绪也让中继监听），但它只影响 app 侧要不要
    起中继 —— 那是可观测的事实，helper 探端口就能看到。

    ★安全默认值：两项默认都关闭 = 旁路模式（只建 utun，系统网络一无所动）。
    全局劫持的风险是**不对称**的：隧道通了只是省事，隧道不通（上游 iPad SOCKS
    拒纯 IP CONNECT、不响应 UDP 等）则等于整机断网，还要用户手动找备份恢复。
    实测（2026-09-03）全局开启后 288 条 io timeout、全网瘫痪。
    默认旁路的最坏结果只是「隧道没派上用场」，Mac 照常上网；用户确认隧道确实
    可用后，再在配置里显式勾选，把 Mac 流量真正交给 iPad。
    """
    enabled = plugin_cfg.get("enabled", False)
    if not isinstance(enabled, bool):
        raise TunnelConfigError(f"enabled 必须是布尔值：{enabled!r}")

    local_port = plugin_cfg.get("local_port", DEFAULT_LOCAL_PORT)
    device_port = plugin_cfg.get("device_port", DEFAULT_DEVICE_PORT)
    local_port = valid_port(local_port, "local_port")
    device_port = valid_port(device_port, "device_port")

    # bind_interface 已移除：hev-socks5-tunnel 的 Apple 构建**没有** bind-interface
    # 配置项（已对随包二进制 strings 核对），填了也只会被静默忽略。这里不再写出，
    # 免得用户以为自己指定了出口接口。helper 侧仍接受该字段（兼容旧版意图）。
    disable_ipv6 = plugin_cfg.get("disable_ipv6_on_activate", True)
    if not isinstance(disable_ipv6, bool):
        raise TunnelConfigError(f"disable_ipv6_on_activate 必须是布尔值：{disable_ipv6!r}")

    dns = plugin_cfg.get("dns", DEFAULT_DNS)
    if not isinstance(dns, str) or not valid_ip(dns):
        raise TunnelConfigError(f"dns 必须是合法 IP：{dns!r}")

    # 默认 False：旁路优先，避免隧道一不通就整机断网（理由见 build_tunnel docstring）
    hijack_route = plugin_cfg.get("hijack_route", False)
    if not isinstance(hijack_route, bool):
        raise TunnelConfigError(f"hijack_route 必须是布尔值：{hijack_route!r}")

    hijack_dns = plugin_cfg.get("hijack_dns", False)
    if not isinstance(hijack_dns, bool):
        raise TunnelConfigError(f"hijack_dns 必须是布尔值：{hijack_dns!r}")

    # ignore_link：面板「手动」档——不等链路探测就尝试建隧道。
    # ⚠️ 它**只留在插件配置里**（控制 app 侧中继要不要在链路没就绪时也监听），
    # v2 起不进 tunnel.json：链路是一条**运行期事实**，而这份文件是期望态；helper
    # 那一侧的判据改成「探上游端口在不在听」，与这里说的是同一件事，且是现问的。
    # 仍然校验：配置里写错类型要当场报错，而不是留着以后让人莫名其妙。
    ignore_link = plugin_cfg.get("ignore_link", False)
    if not isinstance(ignore_link, bool):
        raise TunnelConfigError(f"ignore_link 必须是布尔值：{ignore_link!r}")

    # Wi-Fi 切换：新配置读固定 `wifi` 字段（root 侧无 type 解析）；
    # 旧配置的 on_activate/on_deactivate 动作列表在此转换为 wifi 字段（兼容层）。
    wifi_raw = plugin_cfg.get("wifi")
    if wifi_raw is None:
        on_activate = _legacy_wifi_ssid(plugin_cfg.get("on_activate"), "on_activate")
        on_deactivate = _legacy_wifi_ssid(plugin_cfg.get("on_deactivate"), "on_deactivate")
    else:
        if not isinstance(wifi_raw, dict):
            raise TunnelConfigError(f"wifi 必须是对象：{wifi_raw!r}")
        unknown = set(wifi_raw) - set(WIFI_KEYS)
        if unknown:
            raise TunnelConfigError(f"wifi 含未知字段：{sorted(unknown)}")
        on_activate = validate_wifi_ssid(wifi_raw.get("on_activate"), "on_activate")
        on_deactivate = validate_wifi_ssid(wifi_raw.get("on_deactivate"), "on_deactivate")

    return {
        "version": TUNNEL_WIRE_VERSION,
        "enabled": enabled,
        "local_port": local_port,
        "device_port": device_port,
        "dns": dns,
        "disable_ipv6": disable_ipv6,
        "hijack_route": hijack_route,
        "hijack_dns": hijack_dns,
        "wifi": {
            "on_activate": on_activate,
            "on_deactivate": on_deactivate,
        },
    }
