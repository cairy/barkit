"""**编译层**：一个 profile 展开成**唯一一条**线格式规则（``_iface_candidates`` 收网卡
候选、``_line_fields`` 收 ``(ifaces, gateway, destinations)``），以及覆盖层
``rule_overrides.<场景名>.routes_suppressed`` 的「本次抑制」判定。

多网卡的「第一命中制」（2026-10-05 定案）
---------------------------------------
一个 profile 可以声明多张网卡（``ifaces: [en5, en6]``，列表顺序＝候选优先级），编译层
**原样产出全部候选**（``ifaces``，按声明顺序去重），由意图层在探活那一拍挑出**第一张
命中**的那一张（见 ``intent.build_intent``）。编译期产出的 ``iface`` 是**兜底值**
＝候选里的第一张 —— 它保证纯编译路径（配置诊断、非法配置报错）与线格式形状不变，
而真正生效的那张只有探活那一层知道。

为什么挑网卡这件事不在编译层做：编译层是**纯函数**（无运行时状态），而「哪张网卡此刻
在线、落在声明的网段里」只有探活答得出来。放在编译层就得让它去跑子进程，既污染纯函数
又让「配置能不能编译」取决于当时的网络。

为什么这两个和 ``build_routes`` 不放在同一个模块：前者是**单场景**的纯函数（无状态、
无循环），后者是**整段配置**的编排（要跨场景查重、要读顶层键、要拼产物）。编排那一步
留在入口，编译那一步落这里。

依赖：``checks``（``valid_ipv4`` / ``_is_bool``）＋ ``errors``（``RouteConfigError``）
＋ ``keys``（``_OVERRIDE_KEYS`` / ``_UNKNOWN_TAIL``）。**不依赖入口** ⇒ 无环。

``rules.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations


from .checks import _is_bool, valid_ipv4
from .errors import RouteConfigError
from .keys import NET_MODE_DHCP, _OVERRIDE_KEYS, _UNKNOWN_TAIL


# ──────────────────────────────────────────────
# 编译：profiles → 线格式
# ──────────────────────────────────────────────


def _iface_candidates(profile, idx: int) -> list[str]:
    """本 profile 的网卡**候选清单**（列表顺序＝候选优先级，去重后原序保留）。

    多张是合法的：一张是「只有这一个口」，多张是「这几个口里哪个接上都算」（典型场合是
    扩展坞与直插转换器在系统里拿到的名字不同）。**取哪一张不由本层决定** —— 编译层是
    纯函数，答不出「哪张此刻在线」；判定在意图层那一拍（``intent.build_intent``），
    规则是「候选顺序上第一张命中者胜」。

    空清单仍然报错：一个没有网卡的场景展开不出线格式规则，而「静默跳过」会让该场景此前
    加的路由从此没人撤（helper 只对意图里现存的规则增删）。
    """
    ifaces: list[str] = []
    for raw in profile["ifaces"]:
        name = str(raw).strip()
        if name and name not in ifaces:
            ifaces.append(name)
    if not ifaces:
        raise RouteConfigError(
            f"profiles[{idx}]（{profile['name']}）没有声明任何网卡：{profile['ifaces']!r}"
            f"——ifaces 必须是非空数组（如 [en5]，或 [en5, en6] 表示哪个接上都算）"
        )
    return ifaces


def _pins_lease(profile) -> bool:
    """本场景会不会**把租约固化下来**（``net.mode = dhcp`` ＋ 带 ``pin``）。

    它回答的是「下一跳留空有没有来源」（v8）：只有固化租约的场景会真的去读一次这块
    网卡的租约，那个网关才存在；跟随系统与静态 IP 都没有「租约给的网关」这件事。
    判据与 helper 侧 ``routeops.validate._pins_lease`` 同一份口径（不同进程、不同包，
    那里用的是**校验后**的 ``net``，故不必再判 ``mode``）。
    """
    net = profile.get("net")
    return (isinstance(net, dict) and net.get("mode", NET_MODE_DHCP) == NET_MODE_DHCP
            and isinstance(net.get("pin"), dict))


def _line_fields(profile, idx: int, items: list) -> tuple[list[str], str, list[str]]:
    """``(ifaces, gateway, destinations)`` —— 本场景展开后的唯一一条线格式规则。

    - 返回的 ``ifaces`` 是**候选清单**（顺序即优先级）；真正生效的那张由意图层挑。
    - ``destinations`` 一律是**全部** items 的目标（含被停用的），好让 helper 有据可撤；
      「该不该叠」由线格式的 ``enabled`` 一位表达（见模块 docstring）。
    - ``via`` 缺省回落 ``match.gateway``；两处都没有时**分两种**（v8）：
      「固化租约」的场景（``_pins_lease``）里这是许可的 —— 线格式写空串，表示「这个
      下一跳由 helper 从本场景固化那块网卡上取」（**只有这么填才能表达**：那个值在
      租约里，用户写不出来）；其余场景仍**报错**，因为没有任何地方能给出一个网关，
      静默跳过会让用户以为配上了。
    - 两种写法**不许混用**：留空那几条取的是 helper 给的网关，与写死的值未必是同一个，
      而线格式一条规则只有一个下一跳 —— 混着写只能报错（不替用户挑一个）。
    - **没有 items 也是一个合法场景**（第 8 步放开）：§4.7 一那个真实场景就是「只切 IP、
      不加路由」。那时网关可以为空 —— 一条目标都没有，就没有一个下一跳可言。
    """
    name = profile["name"]
    default_via = str((profile.get("match") or {}).get("gateway") or "").strip()
    ifaces = _iface_candidates(profile, idx)
    if not items:
        return ifaces, default_via, []
    pins_lease = _pins_lease(profile)
    blanks = 0
    via_groups: list[str] = []
    destinations: list[str] = []
    for j, item in enumerate(items):
        via = str(item.get("via") or "").strip() or default_via
        if via and not valid_ipv4(via):
            # 走不到这儿（``validate_route_item`` 会先拦），留一条兜底免得求值路径抛别的错
            raise RouteConfigError(
                f"「{name}」的第 {j + 1} 条路由的下一跳不是合法 IPv4：{via!r}"
                f"（profiles[{idx}].routes.items[{j}]）"
            )
        if not via:
            if not pins_lease:
                raise RouteConfigError(
                    f"「{name}」的第 {j + 1} 条路由没有下一跳：这一行留空了，本场景也没填「匹配网关」。"
                    f"填一个，或把本场景的「网络配置」选成「固化租约」——那种场景会自己从网卡上取。"
                    f"（profiles[{idx}].routes.items[{j}]）"
                )
            blanks += 1
            destinations.append(str(item["destination"]).strip())
            continue
        if via not in via_groups:
            via_groups.append(via)
        destinations.append(str(item["destination"]).strip())
    if blanks and via_groups:
        raise RouteConfigError(
            f"「{name}」的路由有的写了下一跳、有的留空：留空的那几条要取「固化租约」"
            f"从网卡上拿到的网关，与写死的 {via_groups} 未必是同一个值，"
            f"而线格式一条规则只有一个下一跳 —— 请统一。"
            f"（profiles[{idx}].routes）"
        )
    if blanks:
        return ifaces, "", destinations
    if len(via_groups) != 1:
        raise RouteConfigError(
            f"profiles[{idx}]（{name}）的路由有 {len(via_groups)} 个下一跳：{via_groups}"
            f"——线格式一条规则只有一个网关，多下一跳要等路由模型放开（模块 docstring 记着那个承诺为什么撤销）"
        )
    return ifaces, via_groups[0], destinations


def _override_suppressed(name: str, overrides: dict) -> bool:
    """覆盖层：该场景「**本次**」是否被撤掉全部路由（§4.8）。

    它的生命期是**这一次**——换个场景就自动回到「按配置」（清除时机见插件
    ``_reap_suppressions``），与配置层 ``routes.enabled=false`` 的「以后都不要」是两件事。
    落到线格式后仍是一位的 ``enabled``：helper 不需要认识新概念。
    """
    entry = overrides.get(name)
    if entry is None:
        return False
    if not isinstance(entry, dict):
        raise RouteConfigError(f"rule_overrides[{name!r}] 必须是对象：{entry!r}")
    bad = sorted(set(entry) - _OVERRIDE_KEYS)
    if bad:
        raise RouteConfigError(
            f"rule_overrides[{name!r}] 有不认识的键：{bad}"
            f"（旧模型的 force_apply / manual_override 已下线：本次撤路由写 "
            f"routes_suppressed=true；「以后都不要」去写 profiles[].routes.enabled）"
            f"{_UNKNOWN_TAIL}"
        )
    suppressed = entry.get("routes_suppressed", False)
    if not _is_bool(suppressed):
        raise RouteConfigError(
            f"rule_overrides[{name!r}].routes_suppressed 必须是布尔值：{suppressed!r}"
        )
    return suppressed
