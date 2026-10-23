"""route_guard/rules：YAML **配置项**（``profiles``） → 路由意图正文的转换与校验。

《网络切换设计》§6.3 第 4 步把配置模型从 ``rules[]``（一张网卡 + 一个网关 + 一串目标）
换成 ``profiles[]``（一个场景：匹配条件 + 网卡配置 + 路由清单），见设计 §2.1 / §6.2。

**旧格式不受支持，也没有自动迁移**（用户裁决 2026-09-17）。段里还写着 ``rules[]`` 时
``build_routes`` 会当场报错，而不是读它、更不是悄悄改成新形状：静默迁移会把用户
**验收过的一份 YAML** 换成他没写过的样子，且那次写入不可逆；报错则让他自己决定何时改。
同一条原则落到每一层：**未知键一律报错**，不认识的字段不会被忽略。

为什么每个场景都必须产出一条线格式规则（不能「关掉就不出现」）
----------------------------------------------------------
``helper.run_cycle`` 只对**意图里现存的规则**做增删：某条规则从意图里消失，它此前加的
路由就**没有任何人去撤**（helper 没有旁路账本）。所以本步每个合法场景恒定产出一条线格式
规则，用 helper 早已认识的 ``enabled`` 字段表达「该不该叠」——它就是旧模型里
``rules[].enabled`` 的同一个位子。

新模型落回旧线格式的过渡期口径
------------------------------
- 落盘给 root helper 的线格式仍是同一份 ``{"version", "poll", "rules":[{name, priority,
  iface, gateway, destinations, enabled}]}``。理由写在 §6.3：helper 已装在机器上，线格式
  一动就得重装；且 helper 遇到未知字段会**整份拒绝 → 冻结**，等于静默停摆。新概念要等
  helper 升级（第 8 步）才有处安放。
- 配置里再没有 ``priority`` 数字：**profiles 的列表顺序即优先级**（§2.1，第 1 个命中者胜），
  线格式的 ``priority`` 由编译期按列表下标填入。
- 线格式一条规则只有**一张网卡 + 一个下一跳**，而 profile 的 ``ifaces`` 是列表、
  ``routes.items[].via`` 是逐条的 ⇒ 本步要求每个 profile 只展开出一条线格式规则，
  否则报错，不静默丢房子。
  ⚠️ **原先这里写着「多网卡 / 多下一跳随第 8 步 helper 升级放开」——那个承诺 2026-09-18
  当场撤销**：第 8 步做完了（helper 网卡能力已落地），而这两件并没跟着放开。原因是
  它们压根不是 helper 能力问题：① 线格式一条规则只有一张网卡、一个下一跳，要放开就得
  动**路由模型**（一个 profile 展开成 N 条？线格式的 ``iface`` 改成列表？）；② 更要紧的是
  语义——「多张网卡都命中」时路由叠给哪张？下一跳在多张网卡上未必同一条。
  **这个问号 2026-10-05 已收：定案＝「第一命中制」**，现已落地：多网卡时取**列表顺序上
  第一张命中的**那一张 ⇒ 一个 profile 仍只展开出一条线格式规则（上文那条「只展开一条」
  的硬约束不变），helper 一侧零改动、不需要重装。下一跳不取「多条路由叠加」——
  下一跳在多张网卡上未必同一条，叠加等于替用户编一套他自己没声明的规则。

  **落点分两层**：编译层（``compile._iface_candidates``）只把候选按声明顺序原样收进产物
  的 ``ifaces``（不进线格式），``iface`` 填第一张当兜底；**挑哪一张**由意图层
  （``intent.build_intent``）在探活那一拍做 —— 那里才答得出「哪张此刻命中」。
  「命中」复用 ``network_probe`` 同一条判据（在线 ＋ 声明过的 match 条件全成立），
  不另立一份：两张网卡各自一套判据的话，面板说的「命中了」与路由实际叠给谁就会分叉。
  一张都不命中 ⇒ **仍落在第一张候选上**（不是不产出）：规则从意图里消失，它此前加的
  路由就没人去撤（helper 只对意图里现存的规则增删）。
- 一个场景内的 items **可以各自开关**（v7，2026-10-06）：停用的那几条**仍然**收进
  ``destinations``（helper 只对意图里现存的目标增删，目标一消失它此前加的路由就没人撤），
  再由本条规则的 ``off_destinations`` 点名列出去撤。此前这里写着「必须开关一致」——
  那是线格式只有一位 ``enabled`` 时的过渡口径，随 v7 撤销。
- ``manual_only`` / ``pinned`` / ``fallback`` 本步**只落存储与校验，不参与产出**——
  它们的语义要等第 6 步的四台状态机与场景选择器。


``rules.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations


from .checks import _is_bool
from .compile import _line_fields, _override_suppressed
from .errors import RouteConfigError
from .keys import DEFAULT_HELPER_POLL, FALLBACKS, FALLBACK_KEEP, MAX_HELPER_POLL, MIN_HELPER_POLL, _PLUGIN_SECTION_KEYS, _UNKNOWN_TAIL
from .messages import _LEGACY_HANDBACK_REQUEST_MESSAGE, _LEGACY_OPS_MESSAGE, _LEGACY_RULES_MESSAGE, _LEGACY_STATE_POLL_MESSAGE
from .schema import validate_profile

# 再导出：调用面（``plugin.py`` / ``reports.py`` / ``profiles.py`` / ``netops.py`` /
# ``settings_*`` / ``intent.py`` / 测试）仍从**入口**取这些名字 —— 正文搬去了兄弟模块
# （``keys`` / ``messages`` / ``errors`` / ``checks`` / ``schema`` / ``compile``），
# 入口只负责把它们摆回同一张脸上。平铺模式没有 ``__init__.py`` 替我们做这件事，
# 故这一段是**手工**的（引擎只带 ``build_routes`` 用到的那些）。
from .keys import FALLBACK_DHCP, NET_MODE_DHCP, NET_MODE_STATIC
from .schema import validate_net
from .checks import net_writes
# 上面三条是「外部真在读」的；下面三条补的是**公开 API 名**（无下划线）：全仓 grep 过，
# 这六个目前**零外部读法**，但它们不是私有件 —— 拆包承诺的是「引用面一行未改」，而
# ``from …rules import valid_ipv4`` 这种写法在拆之前是能跑的。收口在这里，让人脸与拆前
# 一张不多、一张不少（判据见 ``tests/test_route_intent.py`` 的
# ``test_route_guard_rules_entry_face`` / ``…_deps_point_one_way`` 两条）。
from .checks import valid_destination, valid_ipv4
from .schema import validate_match, validate_route_item, validate_routes_block
from .keys import NET_MODES

__all__ = [
    "DEFAULT_HELPER_POLL",
    "MIN_HELPER_POLL",
    "MAX_HELPER_POLL",
    "NET_MODES",
    "NET_MODE_STATIC",
    "NET_MODE_DHCP",
    "FALLBACK_KEEP",
    "FALLBACK_DHCP",
    "FALLBACKS",
    "valid_ipv4",
    "valid_destination",
    "net_writes",
    "validate_match",
    "validate_net",
    "validate_route_item",
    "validate_routes_block",
    "validate_profile",
    "build_routes",
    "RouteConfigError",
]


def build_routes(plugin_cfg: dict) -> dict:
    """插件配置段 → 路由意图正文。非法抛 RouteConfigError。

    - 顶层 ``paused``：bool（可选）。True=自动化暂停（helper 冻结、不动路由）。
    - 每个场景 ``routes.enabled``（缺省 True）：False=该场景整套不加路由（§4.7 一）。
    - 每个 item ``enabled``（缺省 True）：False=单条不加。同一场景内必须一致。
    - 覆盖层 ``rule_overrides.<场景名>.routes_suppressed``（§4.8）：True=**本次**该场景不叠加
      路由。它和配置层 ``routes.enabled`` 的区别是生命期：这个随换场景自动回到「按配置」。

    ``manual_only`` / ``pinned`` / ``fallback`` 本步只校验，不参与产出。

    产出里另有几个**只给下游消费、不进线格式**的东西（第 8 步加）：每条规则的 ``net``
    （本场景要把网卡配成什么，``intent`` 会原样递进意图）、以及顶层的 ``fallback`` 与
    ``handback_request``。后两个在 ``intent.build_intent`` 里合成顶层 ``handback`` ——
    helper 的白名单里没有它们，写进意图会让整份被拒。**产出里也没有 ``version``**：
    线格式版本号由 ``intent.build_intent`` 一处给出（它不读入参、直接写 ``INTENT_VERSION``）。

    节律读 ``helper_poll``（**不读** ``poll``——那是框架轮询 ``tick()`` 的间隔，
    决议 D27：一个配置键只服务一个消费者）；写出的线格式字段仍叫 ``poll``。
    """
    poll = plugin_cfg.get("helper_poll", DEFAULT_HELPER_POLL)
    if not isinstance(poll, int) or not MIN_HELPER_POLL <= poll <= MAX_HELPER_POLL:
        raise RouteConfigError(f"helper_poll 非法：{poll!r}")
    paused = plugin_cfg.get("paused")
    if paused is not None and not _is_bool(paused):
        raise RouteConfigError(f"paused 必须是布尔值：{paused!r}")
    fallback = plugin_cfg.get("fallback")
    if fallback is not None and fallback not in FALLBACKS:
        raise RouteConfigError(f"fallback 只能是 {list(FALLBACKS)} 之一：{fallback!r}")
    pinned = plugin_cfg.get("pinned")
    if pinned is not None and not isinstance(pinned, str):
        raise RouteConfigError(f"pinned 必须是场景名字符串：{pinned!r}")
    # ⌈命令通道⌋（顶层 ``ops``）**已随会话一起消失**（2026-09-29）：「恢复 DHCP」这类
    # 一次性动作现在走会话帧 ``op.run``（一帧一命令、回帧即回执，见 ``plugin.renew_action``）。
    # 于是这里不再有「待执行命令」这一节 —— 命令不落配置，也就没有「撤掉它」这件事。
    # 配置里还留着 ``ops`` 只可能是上一代遗物，给一句指路的错（不是静默忽略）。
    if "handback_request" in plugin_cfg:
        raise RouteConfigError(_LEGACY_HANDBACK_REQUEST_MESSAGE)
    if "ops" in plugin_cfg:
        raise RouteConfigError(_LEGACY_OPS_MESSAGE)
    # 「恢复 DHCP」是否连带 DNS（设置页复选框）。缺省 true（还原 DNS）
    # —— 与守护侧 _restore_baseline 的缺省口径一致：交还本就该把 DNS 也交回去。
    handback_dns = plugin_cfg.get("handback_dns", True)
    if not isinstance(handback_dns, bool):
        raise RouteConfigError(f"handback_dns 必须是布尔值：{handback_dns!r}")

    # ⚠️ 旧模型的键直接拒绝，不要「读懂它」也不要「顺手搬走」：它被改造最常见的样子
    # 是用户没改过写法，而任何自动迁移都是把用户验收过的一份 YAML 换成他没写过的样子。
    if "rules" in plugin_cfg:
        raise RouteConfigError(_LEGACY_RULES_MESSAGE)
    # 死键同样 **报错指路**（不「顺手删掉」）：组件不改写用户验收过的那份 YAML。
    if "state_poll" in plugin_cfg:
        raise RouteConfigError(_LEGACY_STATE_POLL_MESSAGE)
    unknown = sorted(k for k in set(plugin_cfg) - _PLUGIN_SECTION_KEYS if isinstance(k, str))
    if unknown:
        raise RouteConfigError(f"plugins.route_guard 有不认识的键：{unknown}{_UNKNOWN_TAIL}")

    profiles = plugin_cfg.get("profiles") or []
    if not isinstance(profiles, list):
        raise RouteConfigError(f"profiles 必须是数组：{profiles!r}")
    overrides = plugin_cfg.get("rule_overrides") or {}
    if not isinstance(overrides, dict):
        raise RouteConfigError(f"rule_overrides 必须是对象：{overrides!r}")
    if pinned:
        known = {
            str(p.get("name", "")).strip()
            for p in profiles if isinstance(p, dict)
        }
        if pinned not in known:
            raise RouteConfigError(f"pinned 指向不存在的场景：{pinned!r}")

    clean: list[dict] = []
    seen: set[str] = set()
    for i, profile in enumerate(profiles):
        err = validate_profile(profile, i)
        if err:
            raise RouteConfigError(err)
        name = str(profile["name"]).strip()
        if name in seen:
            raise RouteConfigError(f"profiles[{i}].name 重复：{name!r}")
        seen.add(name)

        block = profile.get("routes") or {}
        items = block.get("items") or []
        # 空 items **也**要产出一条规则（第 8 步改）。原先这里 `continue`（“没有目标就
        # 展开不出合法线格式”），那时理由成立；现在不行了 —— 一个场景可以「只切 IP、
        # 不加路由」（§4.7 一），而切 IP 要经过 helper，就没法再跳过它。代价是线格式
        # 多了一种「destinations 为空」的规则，helper 侧同步放开（见 `validate_routes`）。
        # 整条要不要叠 = 场景开关 **且** 至少有一条目标启用。混合开关态自 v7 起合法
        # （见下面的 ``off_destinations``），故这里只回答「有没有一条要叠」，
        # **不再**要求它们开关一致 —— 那条硬报错会让整个组件停摆（2026-10-06 撤销）。
        enabled = bool(block.get("enabled", True)) and any(
            bool(item.get("enabled", True)) for item in items
        )
        # 停用的那几条目标（v7）：它们**仍在** ``destinations`` 里（``_line_fields`` 收全量），
        # 再点名列进 ``off_destinations`` 让 helper 去撤。只在整条**要叠**的时候写 ——
        # ``enabled=False``（场景开关关了 / 本次被抑制）时 helper 整条按「撤」收敛，
        # 逐目标那一栏没有意义（v7 的校验要求它是 destinations 的子集，不是要求它非空）。
        #
        # 同一个目标被写了两遍（一遍启用、一遍停用）时**启用者胜**，与上面「任一启用即叠」
        # 同源：配置校验不拦重复目标（界面允许添两行同目标），而两套口径各判一次必分叉。
        on = {str(item["destination"]).strip() for item in items
              if bool(item.get("enabled", True))}
        off: list[str] = []
        for item in items:
            if bool(item.get("enabled", True)):
                continue
            dest = str(item["destination"]).strip()
            if dest and dest not in on and dest not in off:
                off.append(dest)
        # 覆盖层（§4.8）：本次撤掉 ⇒ 落到同一位 enabled=False，helper 照旧按「不叠加」收敛
        if _override_suppressed(name, overrides):
            enabled = False
        ifaces, gateway, destinations = _line_fields(profile, i, items)
        match = profile.get("match") or {}
        clean.append({
            "name": name,
            "priority": i,                  # 列表顺序即优先级（§2.1）
            # ↓ **不进线格式**：候选清单只给意图层挑网卡用，helper 的白名单不认识它
            #   （与下面 ``subnet`` / ``match_gateway`` 同一种处置）。
            "ifaces": ifaces,
            # 编译期的**兜底值**（候选里第一张）；真正生效的那张由 ``build_intent``
            # 按「第一命中制」改写。留着它是为了让这条产物在没有探活的场合（配置诊断、
            # 非法配置报错）形状仍然完整。
            "iface": ifaces[0],
            "gateway": gateway,             # 线格式的下一跳（helper 用；= items 的 via）
            # ↓ 两个**只参与匹配决策、不进线格式**的字段（helper 的白名单不认识它们，
            #   写进去会让整份意图被拒 —— 与 ``pinned`` / ``manual_only`` 同一种处置）。
            "subnet": str(match.get("subnet") or "").strip(),
            # ``match.gateway`` 与上面那个 ``gateway`` **不是一个东西**（§2.4 那条
            # 「两个『网关』不是一个东西」在触发条件上的翻版）：上面的是「这条路由从
            # 哪个下一跳出去」，这个是「我在哪个网络里」。以前探活拿的是上面那个，
            # 于是「match.gateway 写 A、via 写 B」的配置会被拿 B 去比网关 —— 判定依据
            # 悄悄换成了路由参数。分开取之后，触发条件只认用户在 match 里写下的那个。
            "match_gateway": str(match.get("gateway") or "").strip(),
            "destinations": destinations,
            "enabled": enabled,
            # ⌈off_destinations⌋（v7）：本条里要撤的那几条 —— 逐目标开关落在线格式上的
            # 那一栏。整条不叠（enabled=False）时不写：那时 helper 按「整条撤」收敛。
            **({"off_destinations": off} if (enabled and off) else {}),
            # ⌈net⌋：本场景要把网卡配成什么（§2.5）。**进意图、不布线格式的规则字段**
            # —— helper 从第 8 步起认识它（`routeops._RULE_FIELDS` 白名单里已经加上了）。
            # 不写 = 一根手指都不碰网卡，故缺省时连键都不给。
            **({"net": profile["net"]} if profile.get("net") else {}),
        })
    # 产物**不含** ``version``：本函数第 5 步起产的是**中间产物**（喂 ``intent.build_intent``），
    # 线格式版本号的真源只有那一步一处（它自己写 ``INTENT_VERSION``，不读入参的 version）。
    # 换模型前这里写着一个字面的 ``1`` —— 那时产物确实是线格式、直接落盘；今天它既没人读、
    # 又与真版本不一致，留着就像在说「这份可以拿去落盘」，而 helper 拿到缺 ``apply`` 的它
    # 会整份拒绝 → 冻结。删掉比改成常量更诚实：**这份东西根本不该有版本号**。
    out: dict = {"poll": poll, "rules": clean}
    if paused is not None:
        out["paused"] = paused
    # 下面两个是给 `intent.build_intent` 消费的**中间产物**，不是线格式字段：
    #   fallback  = 无匹配时的策略，与「有没有场景 apply」一起决定要不要发策略交还
    #   handback_dns = 交还时要不要连 DNS 一起还原
    # 两者都会被合成到意图顶层（helper 的白名单里没有 fallback 这个名字，
    # 写进意图就是整份拒绝 + 冻结）。
    out["fallback"] = fallback or FALLBACK_KEEP
    out["handback_dns"] = handback_dns
    return out


# 这里原有一个 ``write_routes(routes)``：档 A 时代的**落盘**入口（原子写 routes.json）。
# 档 B 收口后它零调用方，而且留着它是**有方向的害处**：它收任意 dict，谁写
# ``rules.write_routes(rules.build_routes(cfg))`` 就把本模块的**中间产物**当线格式落了盘
# —— 那份东西缺 ``apply`` / ``handback``（由 ``intent.build_intent`` 补），helper 会整份
# 拒绝 → 冻结。落盘入口只该有一个：``intent.write_intent``（它要求完整期望态）。
# 删掉函数比留一句警告有效：这条误用路径从此在 API 层面不存在。
