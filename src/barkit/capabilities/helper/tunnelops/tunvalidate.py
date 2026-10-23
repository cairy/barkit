"""隧道**意图**的加载与 schema 校验（``validate_tunnel`` / ``load_tunnel``）。

口径与 route 那条一致：**非法整份拒绝** —— 半份意图比没有更危险，它会让隧道跑向一个
谁也没打算要的中间态。

``tunnelops.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import json

from . import tunconsts


# ──────────────────────────────────────────────
# 隧道意图的加载与 schema 校验（非法整份拒绝）
# ──────────────────────────────────────────────

def validate_tunnel(raw) -> tuple[dict, str | None]:
    if not isinstance(raw, dict):
        return {}, "隧道意图顶层必须是 JSON 对象"
    # 契约版本**真锁**（与 ``routeops.validate_routes`` 同一口径，2026-09-29 加）：
    # 只认本 helper 的这一版，缺位也算不符。排在「未知字段」之前 —— 两侧不同代时最该
    # 先说的就是「该重装守护」这一句，而报「含未知字段」拿不到这条线索（2.13.0 那次
    # 真机事故正是这么发生的）。
    declared = raw.get("version")
    if declared != tunconsts.TUNNEL_WIRE_VERSION:
        return {}, (f"契约版本不符：对端发的是 {declared!r}，本 helper 只认 "
                    f"v{tunconsts.TUNNEL_WIRE_VERSION}（整份拒绝，隧道保持现状；"
                    f"在设置页装一次 helper，或升级 App）")
    # ``link_up`` / ``ignore_link`` 自 v2 起**不在契约里**（见 ``TUNNEL_WIRE_VERSION``）：
    # 「iPad 这条链路通不通」是**运行期事实**，不该住在「期望态文件」里。它的新载体是
    # 可观测的事实本身 —— app 的中继只在链路就绪时监听 127.0.0.1:local_port，helper 这边
    # 探那个端口（``_upstream_ready``）就知道上游在不在。于是这个文件退化成纯意图：
    # 幂等、谁都能纠正、也不会拿着一句过期的事实把人误导。
    known = {"version", "enabled", "local_port", "device_port",
             "bind_interface", "dns", "disable_ipv6", "wifi",
             "hijack_route", "hijack_dns"}
    unk = set(raw) - known
    if unk:
        return {}, f"隧道意图含未知字段：{sorted(unk)}"
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        return {}, f"enabled 非法：{enabled!r}"
    local = raw.get("local_port")
    if not isinstance(local, int) or not 1 <= local <= 65535:
        return {}, f"local_port 非法：{local!r}"
    device = raw.get("device_port")
    if not isinstance(device, int) or not 1 <= device <= 65535:
        return {}, f"device_port 非法：{device!r}"
    # 仅做类型校验：hev 2.17.1 的 Apple 构建**没有** bind-interface 配置项
    # （已对该二进制 strings 核对），故这里接受但下游不使用。保留字段是为了
    # 不让旧版意图因「未知字段」被整份拒绝。
    bind = raw.get("bind_interface", "")
    if not isinstance(bind, str):
        return {}, f"bind_interface 非法：{bind!r}"
    dns = raw.get("dns", "")
    # 安全约束：mapdns 只监听固定地址 HEV_DNS（见 _HEV_YAML_TEMPLATE 的
    # mapdns.address），系统 DNS 必须与之严格相等，否则 DNS 请求进隧道后
    # mapdns 不应答 → 整机断网。故 dns 只能为空（沿用默认 HEV_DNS）或显式
    # 等于该地址；其它任何值（哪怕合法 IP 如 8.8.8.8）都在此处拒绝，
    # 宁可让隧道不激活，也不能把 Mac 网络搞瘫。
    if dns and dns != tunconsts.HEV_DNS:
        return {}, f"dns 必须等于隧道 DNS 代理地址 {tunconsts.HEV_DNS}（mapdns 仅监听该地址）：{dns!r}"
    disable = raw.get("disable_ipv6", True)
    if not isinstance(disable, bool):
        return {}, f"disable_ipv6 非法：{disable!r}"
    # ★安全默认值：两项都默认 False（旁路模式）。
    # 全局劫持的风险是不对称的——隧道一旦不通（上游 iPad SOCKS 拒纯 IP CONNECT、
    # 不响应 UDP 等），抢了 /1 路由 + 改了 DNS 就等于整机断网，用户还得手动找
    # 备份恢复。2026-09-03 实测正是如此：全局开启后 288 条 io timeout、全网瘫痪。
    # 默认旁路则最坏情况只是「隧道没派上用场」，Mac 网络一切照旧；想让 Mac 真正
    # 经 iPad 上网，由用户在配置里显式勾选这两项。
    hijack_route = raw.get("hijack_route", False)
    if not isinstance(hijack_route, bool):
        return {}, f"hijack_route 非法：{hijack_route!r}"
    hijack_dns = raw.get("hijack_dns", False)
    if not isinstance(hijack_dns, bool):
        return {}, f"hijack_dns 非法：{hijack_dns!r}"
    wifi = raw.get("wifi") or {}
    if not isinstance(wifi, dict):
        return {}, "wifi 非法（需对象）"
    unk_wifi = set(wifi) - set(tunconsts.TUNNEL_WIFI_KEYS)
    if unk_wifi:
        return {}, f"wifi 含未知字段：{sorted(unk_wifi)}"
    clean_wifi: dict = {}
    for key in tunconsts.TUNNEL_WIFI_KEYS:
        ok, e = _validate_wifi(wifi.get(key))
        if not ok:
            return {}, f"wifi.{key}：{e}"
        v = wifi.get(key)
        clean_wifi[key] = v.strip() if isinstance(v, str) else None
    return {
        "version": tunconsts.TUNNEL_WIRE_VERSION, "enabled": enabled,
        "local_port": local, "device_port": device,
        "bind_interface": bind, "dns": dns, "disable_ipv6": disable,
        "hijack_route": hijack_route, "hijack_dns": hijack_dns,
        "wifi": clean_wifi,
    }, None


def _validate_wifi(v) -> tuple[bool, str | None]:
    """wifi.<key> 只允许 null（不切）或非空字符串（目标 SSID）。

    曾是通用动作列表 `actions: {on_activate: [{type, ssid}]}`——「type 分发」
    等于在 root 侧预留了可配置执行语义的口子：将来任何新 type 都只需要往
    KNOWN_TUNNEL_ACTION_TYPES 加一个字符串。收为固定 `wifi` 字段后，root 侧
    没有 type 解析、没有循环分发，唯一能做的就是 networksetup 切 Wi-Fi。
    """
    if v is None:
        return True, None
    if not isinstance(v, str):
        return False, f"非法（需 null 或非空字符串）：{v!r}"
    if not v.strip():
        return False, "SSID 为空（留空请用 null）"
    return True, None


def load_tunnel(text: str | None = None) -> tuple[dict | None, str | None]:
    """校验隧道的期望态（v2）。

    ``text`` 是 ``tasks.Runner`` 从**内存里的意图**取来递进来的（2026-09-29 起）。
    从前它读用户域 ``tunnel.json``；那个分支连同文件一起消失 —— 调度器只在
    「tunnel 有意图」时才叫这里，而「没有意图」的语义是**拆掉隧道**（依附型：
    隧道表达的是「现在要」，app 一断连就该撤），由 runner 那边少跑一轮来实现，
    不是靠这里读到「文件不存在」再猜。
    """
    if text is None:
        # 没有意图＝**拆掉**。隧道是依附型：它表达的是「现在要」，app 一断连、或
        # 用户在面板上关掉，就该撤下来 —— 而不是「文件读不到，先冻结着看」。
        return None, "没有隧道意图（app 未声明或已断开）：拆除隧道"
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"tunnel 期望态不是合法 JSON：{e}"
    data, err = validate_tunnel(raw)
    if err is not None:
        return None, err
    return data, None
