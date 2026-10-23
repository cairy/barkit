"""**结构校验**逐层递归：``match`` → ``net`` → ``routes.items[]`` → ``routes`` →
``profile``。每一层返回「错误文案」或 ``None``，不外抛 —— 抛的那一步是调用方（编译层
把文案包进 ``RouteConfigError``）。

本模块是**纯判据、零副作用**：它只读配置、只返回字符串，不写任何东西，也不认识线格式。
它同时被两条路消费（编译全程校验，与设置页的即时校验）⇒ 判据只有这一处，不会出现
「界面收下的形状 / 配置收下的形状」两套。

依赖：``checks`` 那一层 ＋ ``keys`` 的键闭集。

``rules.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations


from .checks import _bad_bool, _require_dict, _unknown_keys, _validate_ipv4_list, valid_destination, valid_ipv4
from .keys import NET_MODES, NET_MODE_DHCP, NET_MODE_STATIC, _MATCH_KEYS, _NET_DHCP_KEYS, _NET_STATIC_KEYS, _PIN_KEYS, _PIN_LEASE_FLAGS, _PROFILE_KEYS, _ROUTES_KEYS, _ROUTE_ITEM_KEYS


# ──────────────────────────────────────────────
# 分项校验（返回错误文案或 None）
# ──────────────────────────────────────────────


def validate_match(match, where: str) -> str | None:
    """``match``：都不写 = 网卡在线即命中；都写 = AND（§6.2 兼容老配置的 gateway 写法）。

    ``manual_only`` 的场景这段整体不生效（§2.6：判定依据会被组件自己写掉 ⇒ 自证）。
    """
    err = _require_dict(match, where)
    if err:
        return err
    err = _unknown_keys(match, _MATCH_KEYS, where)
    if err:
        return err
    subnet = match.get("subnet")
    if subnet is not None and not valid_destination(subnet):
        return f"{where}.subnet 不是合法 IPv4 网段：{subnet!r}"
    gateway = match.get("gateway")
    if gateway is not None and not valid_ipv4(gateway):
        return f"{where}.gateway 不是合法 IPv4：{gateway!r}"
    return None


def validate_net(net, where: str) -> str | None:
    """``net``：网卡配置。缺省视为 ``{mode: dhcp}`` 且不带 ``pin``——**一根手指都不碰网卡**。

    - ``static``：``ip`` / ``mask`` 必填，``gateway`` / ``dns`` 可选；不得出现 ``pin``。
    - ``dhcp``：可选 ``pin``（``ip`` / ``mask`` **两个键**必填、值可留空 = 用 DHCP 拿到的；
      ``gateway`` / ``dns`` 留空 = **不固化**这两位；要连它们一起固化，要么填具体地址，
      要么写 ``gateway_from_lease`` / ``dns_from_lease``：``true``，与填具体地址互斥
      —— 详见 `_PIN_LEASE_FLAGS`）。
    """
    err = _require_dict(net, where)
    if err:
        return err
    mode = net.get("mode", NET_MODE_DHCP)
    if mode not in NET_MODES:
        return f"{where}.mode 只能是 {list(NET_MODES)} 之一：{mode!r}"
    allowed = _NET_STATIC_KEYS if mode == NET_MODE_STATIC else _NET_DHCP_KEYS
    err = _unknown_keys(net, allowed, f"{where}（mode={mode}）")
    if err:
        return err

    if mode == NET_MODE_STATIC:
        for key in ("ip", "mask"):
            if not valid_ipv4(net.get(key)):
                return f"{where}.{key} 缺失或不是合法 IPv4：{net.get(key)!r}"
        if net.get("gateway") is not None and not valid_ipv4(net["gateway"]):
            return f"{where}.gateway 不是合法 IPv4：{net['gateway']!r}"
        if net.get("dns") is not None:
            return _validate_ipv4_list(net["dns"], f"{where}.dns")
        return None

    pin = net.get("pin")
    if pin is None:
        return None
    err = _require_dict(pin, f"{where}.pin")
    if err:
        return err
    err = _unknown_keys(pin, _PIN_KEYS, f"{where}.pin")
    if err:
        return err
    for key in ("ip", "mask"):
        # ⚠ 「必填」指的是**键**必填，值可以留空 ——「留空 = 用 DHCP 拿到的那个」正是
        # §2.5 「获取到 IP 后设置为固定 IP」的字面实现（用户不必预先知道地址）。
        # 原先这里写的是 `not valid_ipv4(pin.get(key))`，把「留空」也当成非法：
        # 于是那条字面路径**写不出来**，而 helper 侧的 `routeops.validate_net` 一直
        # 允许留空 —— 两处口径不一致，且正好差在「用户最想要的那一种写法」上。
        if key not in pin:
            return (f"{where}.pin 必须两个键都在：缺 {key}"
                    f"（值可留空 = 用 DHCP 拿到的那个，§2.5）")
        value = pin.get(key)
        if value not in (None, "") and not valid_ipv4(value):
            return f"{where}.pin.{key} 不是合法 IPv4：{value!r}"
    if pin.get("gateway") is not None and not valid_ipv4(pin["gateway"]):
        return f"{where}.pin.gateway 不是合法 IPv4：{pin['gateway']!r}"
    if pin.get("dns") is not None:
        err = _validate_ipv4_list(pin["dns"], f"{where}.pin.dns")
        if err:
            return err
    # 「照抄租约」（§2.5 的补充）：**留空 = 不固化**，照抄是「把那一位也固化」，两件事
    # 分开才有必要有第二个键。故只认 ``true``（写 ``false`` 与不写等价，不留多余语义），
    # 且与同名的写死值**互斥** —— 同时给说明用户自己也没想清，宁可整份拒绝、不猜哪个
    # 优先。判据与 helper 的 `routeops.validate_net` **同一份口径**（两处必然各有一份：
    # 不同进程、不同包），口径不一致时会出现「app 拒绝 / helper 接受」这类只有一边说得通
    # 的配置，排查时最难查。
    for flag, val_key, label in _PIN_LEASE_FLAGS:
        value = pin.get(flag)
        if value is None:
            continue
        if value is not True:
            return f"{where}.pin.{flag} 只能写 true（不想照抄就整条不写）：{value!r}"
        if pin.get(val_key):
            return (f"{where}.pin 的{label}不能既写死 {val_key} 又照抄租约"
                    f"（{flag}: true）：二者只能选一个")
    return None


def validate_route_item(item, where: str) -> str | None:
    """``routes.items[]``：单条明细路由。``via`` 缺省时回落 ``match.gateway``。"""
    err = _require_dict(item, where)
    if err:
        return err
    err = _unknown_keys(item, _ROUTE_ITEM_KEYS, where)
    if err:
        return err
    dest = item.get("destination")
    if not isinstance(dest, str) or not valid_destination(dest):
        return f"{where}.destination 缺失或非法：{dest!r}"
    via = item.get("via")
    if via is not None and not valid_ipv4(via):
        return f"{where}.via 不是合法 IPv4：{via!r}"
    if item.get("enabled") is not None:
        err = _bad_bool(item["enabled"], f"{where}.enabled")
        if err:
            return err
    return None


def validate_routes_block(routes, where: str) -> str | None:
    """``routes``：配置项级总开关（§4.7 一）+ 单条清单。

    形状是**映射**而不是裸列表——裁决 8 要求「整套关掉但配置还在」，裸列表里塞不下
    这一级（``enabled`` 在上，``items`` 在下）。
    """
    err = _require_dict(routes, where)
    if err:
        return err
    err = _unknown_keys(routes, _ROUTES_KEYS, where)
    if err:
        return err
    if routes.get("enabled") is not None:
        err = _bad_bool(routes["enabled"], f"{where}.enabled")
        if err:
            return err
    items = routes.get("items") or []
    if not isinstance(items, list):
        return f"{where}.items 必须是数组"
    for j, item in enumerate(items):
        err = validate_route_item(item, f"{where}.items[{j}]")
        if err:
            return err
    return None


def validate_profile(profile, idx: int = 0) -> str | None:
    """校验单个场景（profile），非法返回错误文案。结构见设计 §2.1 / §2.6。"""
    where = f"profiles[{idx}]"
    err = _require_dict(profile, where)
    if err:
        return err
    err = _unknown_keys(profile, _PROFILE_KEYS, where)
    if err:
        return err

    name = profile.get("name")
    if not isinstance(name, str) or not name.strip():
        return f"{where}.name 缺失或为空"

    ifaces = profile.get("ifaces")
    if not isinstance(ifaces, list) or not ifaces:
        return f"{where}.ifaces 必须是非空数组（如 [en5]）"
    for k, iface in enumerate(ifaces):
        if not isinstance(iface, str) or not iface.strip():
            return f"{where}.ifaces[{k}] 必须是非空字符串：{iface!r}"

    if profile.get("manual_only") is not None:
        err = _bad_bool(profile["manual_only"], f"{where}.manual_only")
        if err:
            return err

    if profile.get("match") is not None:
        err = validate_match(profile["match"], f"{where}.match")
        if err:
            return err
    if profile.get("net") is not None:
        err = validate_net(profile["net"], f"{where}.net")
        if err:
            return err
    if profile.get("routes") is not None:
        err = validate_routes_block(profile["routes"], f"{where}.routes")
        if err:
            return err
    return None
