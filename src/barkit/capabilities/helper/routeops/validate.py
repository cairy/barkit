"""**意图档的 schema 校验**：顶层键 / 规则字段 / 操作参数三张表，以及版本号那把真锁。

口径是「**非法整份拒绝**」：一处不合法就不写任何一个字 —— 半份期望态比没有更危险
（它会让收敛跑向一个谁也没打算要的中间态）。

``routeops.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import json
from ..consts import DEFAULT_POLL

from .ipvalid import valid_destination, valid_ipv4
from .netmode import validate_net


# ──────────────────────────────────────────────
# 规则文件加载与 schema 校验（非法整份拒绝）
# ──────────────────────────────────────────────

_SCHEMA = {"version", "poll", "rules", "paused", "handback", "handback_dns"}
_RULE_FIELDS = {"name", "priority", "iface", "gateway", "destinations", "enabled",
                "off_destinations",                 # 本条规则里要撤的那几个目标（v7，可选）
                "net",                              # 网卡配置（§2.5，可选）
                "apply", "trigger_matched"}         # 意图字段（档 B）：由 app 侧写入，helper 只消费

#: 一条**一次性命令**的参数形状（``op.run`` 帧的 ``params``）。
#:
#: **命令与策略是两样东西**（2026-09-29 定案）：``handback`` 是**策略**（「没有任何
#: 场景要接管时就交还」——每轮都能说一遍，幂等即可）；命令是用户当场按下的一次性动作，
#: 必须执行、且**只能执行一次**。
#:
#: 这里**没有 ``seq``** 了：旧形态靠它判「执行过没有」，而帧本身就是「一次」——
#: 一帧一命令，回帧即回执，不需要任何跨重启的状态。
_OP_FIELDS = {"iface", "op", "net", "dns"}
_OP_KINDS = ("renew", "release")

#: 意图规模的两条硬上限。
#:
#: **为什么必须设上限**：收敛一轮对每条规则的每个目标都要问一次内核路由表
#: （``route_get`` 起一次 ``/sbin/route -n get`` 子进程）。``MAX_FRAME`` 是 1 MiB，
#: 一帧塞得下上万条规则 ⇒ 一轮收敛上万次 fork，守护主循环被钉死几分钟，且每隔
#: ``poll`` 秒复发；期间隧道拆除、心跳、交还全部停摆。这是**只有 root 守护才有的**
#: 放大面：普通进程送一帧小数据的代价，与守护的实际开销不成比例。
#:
#: 取值：一个场景编译出一条规则，一条规则的目标数来自用户手填的目的网段。
#: 64 / 64 对真实配置是几十倍余量，挡的只是「把帧长上限吃满」这种攻击形输入。
MAX_RULES = 64
MAX_DESTINATIONS = 64


def _pins_lease(net) -> bool:
    """这份（已过 ``validate_net`` 的）``net`` 会不会**把租约固化下来**。

    它决定「``gateway`` 留空」有没有来源（v8）：只有固化租约的场景会真的去读一次这块
    网卡的租约，那个网关才存在（纯 DHCP 与静态 IP 都没有「租约给的网关」这件事）。
    取自**校验后的** ``net``，故不必再判 ``mode``：``pin`` 只在 ``dhcp`` 下才留得下来。
    """
    return isinstance(net, dict) and isinstance(net.get("pin"), dict)


def validate_routes(raw) -> tuple[dict, str | None]:
    """校验路由意图。返回 (data, error)；error 非 None 时 data 不可用。"""
    if not isinstance(raw, dict):
        return {}, "规则文件顶层必须是 JSON 对象"
    # 契约版本**真锁**（ROADMAP E4 ②「不符即冻结/告警」）：只认本 helper 的这一版。
    # 排在「未知字段」**之前**是有意的 —— 两侧不同代时最该先说的就是这一句。
    # 反例（真机事故，2.13.0）：旧 helper 看到新顶层键只报「规则文件含未知字段」，
    # 用户拿不到「该重装 helper」这条线索；版本位就是为这句话存在的。
    # 缺 ``version`` 也算不符：没有版本位就**证明不了两侧同代**，而 helper 是 root，
    # 宁可冻结（保持上轮路由不动）也不要按「大概是这个意思」去改用户的网络。
    # 与 ``hostsops.load_hosts_rules`` 的 ``HOSTS_WIRE_VERSION`` 检查同一口径。
    declared = raw.get("version")
    if declared != INTENT_VERSION:
        return {}, (f"契约版本不符：文件写的是 {declared!r}，本 helper 只认 "
                    f"v{INTENT_VERSION}（整份拒绝，保持上轮路由不动；"
                    f"在设置页装一次 helper，或让 app 重写该文件）")
    unknown = set(raw) - _SCHEMA
    if unknown:
        return {}, f"规则文件含未知字段：{sorted(unknown)}"
    paused = raw.get("paused", False)
    if not isinstance(paused, bool):
        return {}, f"paused 非法：{paused!r}"
    poll = raw.get("poll", DEFAULT_POLL)
    # ``bool`` 是 ``int`` 的子类（``1 <= True <= 86400`` 成立），必须显式排除 ——
    # 否则 ``poll: true`` 会被当成 1 秒一轮，把收敛压力放大两个数量级。
    if isinstance(poll, bool) or not isinstance(poll, int) or not 1 <= poll <= 86400:
        return {}, f"poll 非法：{poll!r}"
    rules = raw.get("rules")
    if not isinstance(rules, list):
        return {}, "rules 必须是数组"
    if len(rules) > MAX_RULES:
        return {}, f"rules 超过上限 {MAX_RULES} 条（实际 {len(rules)}）"
    clean = []
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            return {}, f"rules[{i}] 必须是对象"
        unknown_r = set(rule) - _RULE_FIELDS
        if unknown_r:
            return {}, f"rules[{i}] 含未知字段：{sorted(unknown_r)}"
        name = rule.get("name", "")
        if not isinstance(name, str) or not name.strip():
            return {}, f"rules[{i}].name 缺失或为空"
        iface = rule.get("iface", "")
        if not isinstance(iface, str) or not iface.strip():
            return {}, f"rules[{i}].iface 缺失或为空"
        dests = rule.get("destinations", [])
        if not isinstance(dests, list):
            return {}, f"rules[{i}].destinations 必须是数组"
        if len(dests) > MAX_DESTINATIONS:
            return {}, (f"rules[{i}].destinations 超过上限 {MAX_DESTINATIONS} 条"
                        f"（实际 {len(dests)}）")
        bad = [d for d in dests if not isinstance(d, str) or not valid_destination(d)]
        if bad:
            return {}, f"rules[{i}].destinations 含非法目标：{bad}"
        # ``off_destinations``（v7）：本条规则里**要撤**的那几个目标，必须是
        # ``destinations`` 的子集。「A 加、B 撤」在线格式里本来表达不了 —— 一条规则只有
        # 一位 ``enabled``，而收敛是「逐个目标按那一位决定加还是撤」，装不下逐目标开关。
        #
        # 为什么必须是子集而不是随便给一串：撤一个**没在本条声明过**的目标，等于让
        # helper 去动一条它这一轮本不该管的明细路由。宁可整份拒绝 —— 与「未知字段一律
        # 整份拒绝」同一口径，不猜用户到底想撤哪一条。
        off = rule.get("off_destinations")
        if off is not None:
            if not isinstance(off, list):
                return {}, f"rules[{i}].off_destinations 必须是数组：{off!r}"
            bad_off = [d for d in off
                       if not isinstance(d, str) or not valid_destination(d)]
            if bad_off:
                return {}, f"rules[{i}].off_destinations 含非法目标：{bad_off}"
            outside = [d for d in off if d not in dests]
            if outside:
                return {}, (f"rules[{i}].off_destinations 含 destinations 里没有的目标："
                            f"{outside}")
        # ``net``：本场景命中时要给网卡配成什么（§2.5）。缺省 = 一根手指都不碰网卡。
        # 形状校验在 ``validate_net``（与 routeops 同一份口径，app 侧编译时已经过一遍，
        # 这里再兜一次——helper 是 root，绝不相信送进来的东西）。
        # ⚠️ 排在下面那条 ``gateway`` 判据**之前**：那条要问它「本场景固化租约吗」（v8）。
        net, net_err = validate_net(rule.get("net"))
        if net_err:
            return {}, f"rules[{i}].{net_err}"
        # ``gateway`` 只在**有目标**时才要求（第 8 步放开）：一个场景可以「只切 IP、
        # 不加路由」（§4.7 一那个真实场景），那时它一条目标都没有，也就没有一个下一跳
        # 可言。要求它必须写个 IPv4，等于逼用户为不存在的路由编一个网关。
        # 反过来，有目标就必须有下一跳 —— ``ensure_route`` 不接受空网关。
        #
        # v8 的唯一例外：**空网关 ＋ 带 ``pin`` 的 ``net``**。那表示「这个下一跳由你
        # 在本轮从这块网卡上取」（app 侧把「留空」编译成空串）。取不到时 helper 整条
        # 不动并回显 ``gateway_src: "none"``，**不是**拒绝整份 —— 一次读不到租约就把
        # 整个组件冻住太狠，而那正是守护刚启动时的常态。
        gateway = rule.get("gateway", "")
        if dests and not valid_ipv4(gateway) and not (
                isinstance(gateway, str) and not gateway and _pins_lease(net)):
            return {}, (f"rules[{i}].gateway 不是合法 IPv4：{gateway!r}"
                        f"（留空只在 net 带 pin 时成立——那表示从本场景固化那块网卡上取）")
        if gateway and not valid_ipv4(gateway):
            return {}, f"rules[{i}].gateway 不是合法 IPv4：{gateway!r}"
        priority = rule.get("priority")
        if priority is not None and (not isinstance(priority, int) or priority < 0):
            return {}, f"rules[{i}].priority 非法：{priority!r}"
        enabled = rule.get("enabled", True)
        if not isinstance(enabled, bool):
            return {}, f"rules[{i}].enabled 非法：{enabled!r}"
        # ``force_apply`` / ``manual_override`` 已随第 8 步删除：前者是「面板真手动开关」
        # 的遗留（面板那个控件位早已给「本次叠加」，见 §4.5），后者是一次键改名的遗留通道。
        # app 侧自第 5 步起就不再产出这两个字段（``rules.build_routes`` 不写），
        # 故删掉白名单不会影响任何在用的配置；留着反而是两条**能写进来但我们从不读**的路。
        # 意图字段（档 B）：apply = app 侧算好的「该规则本轮是否应生效」；
        # trigger_matched = app 侧探测到的触发命中（回显给面板用）。
        # 二者由 app 写入、helper 只消费。
        apply_flag = rule.get("apply", None)
        if apply_flag is not None and not isinstance(apply_flag, bool):
            return {}, f"rules[{i}].apply 非法：{apply_flag!r}"
        trigger_matched = rule.get("trigger_matched", False)
        if not isinstance(trigger_matched, bool):
            return {}, f"rules[{i}].trigger_matched 非法：{trigger_matched!r}"
        clean.append({
            "name": name.strip(),
            "priority": int(priority) if priority is not None else 0,
            "iface": iface.strip(),
            "gateway": gateway,
            "destinations": list(dests),
            "enabled": enabled,
            **({"off_destinations": list(dict.fromkeys(off))} if off is not None else {}),
            **({"net": net} if net is not None else {}),
            **({"apply": apply_flag} if apply_flag is not None else {}),
            **({"trigger_matched": trigger_matched} if "trigger_matched" in rule else {}),
        })
    clean.sort(key=lambda r: r["priority"])   # destinations 重叠时小 priority 先排序
    # ``handback``：要求「把这几张网卡交还系统」（§4.7 二）。app 侧的触发条件有两个
    # —— 用户在配置窗体点了 `[交还系统]`，或策略 ``fallback: dhcp`` 且没有任何场景
    # 命中。落到这里就是一个 iface 列表：helper 认得「交还」＝ 还原 baseline。
    #
    # ⚠️ **本字段无条件执行，helper 不看 ``paused``**（与上面的网卡收敛相反）：
    # 「停用期间要不要发那份**策略**交还」是 policy，app 算好才写进来（只有它知道
    # 自己是不是停用）；显式按下的 `[交还系统]` 在停用态下仍然有效（§4.7 三要求它
    # 停用态下仍可达）。helper 再判一次就是第二个决策点。
    handback = raw.get("handback", [])
    if not isinstance(handback, list) or any(
        not isinstance(x, str) or not x.strip() for x in handback
    ):
        return {}, f"handback 必须是非空字符串数组：{handback!r}"
    # ``ops`` 已不在意图里（2026-09-29 搬到会话帧 ``op.run``）：期望态正文只剩
    # 「该保持成什么样」，命令走另一条路。故这里返回的字典里也没有它。
    return {"version": INTENT_VERSION, "poll": poll, "paused": paused,
            "handback": [x.strip() for x in handback],
            "handback_dns": bool(raw.get("handback_dns", True)),
            "rules": clean}, None


def validate_op_params(params) -> tuple[dict, str | None]:
    """校验一条一次性命令的参数（``op.run`` 帧的 ``params``）。缺省＝没有参数。

    形状：``{iface, op, net?, dns}``

    - ``op``：``renew``（交还 DHCP → 等**新租约** → 按 ``net`` 重新固化）/ ``release``
      （只交还给系统，停在 DHCP）。
    - ``net``：只有 ``renew`` 用得上，形状与 ``rules[].net`` 同一套（``validate_net``）。
      缺省 = 交还完就停（等价于 ``release``）。
    - ``dns``：交还时要不要连 DNS 一起还原（缺省 ``True``，与 ``handback_dns`` 同口径）。

    非法一律**拒绝这一帧**（这条路径会改网卡，宁可不动也不猜）。
    """
    if not isinstance(params, dict):
        return {}, f"命令参数必须是对象：{params!r}"
    unknown = set(params) - _OP_FIELDS
    if unknown:
        return {}, f"命令参数含未知字段：{sorted(unknown)}"
    iface = params.get("iface", "")
    if not isinstance(iface, str) or not iface.strip():
        return {}, f"命令参数 iface 缺失或为空：{iface!r}"
    kind = params.get("op", "")
    if kind not in _OP_KINDS:
        return {}, f"命令参数 op 只能是 {list(_OP_KINDS)}：{kind!r}"
    net, net_err = validate_net(params.get("net"))
    if net_err:
        return {}, net_err
    dns = params.get("dns", True)
    if not isinstance(dns, bool):
        return {}, f"命令参数 dns 非法：{dns!r}"
    return {
        "iface": iface.strip(), "op": kind, "dns": dns,
        **({"net": net} if net is not None else {}),
    }, None


# 这里原有 ``load_routes(text=None)``：档 A 时代读「规则定义」的入口，且**自己读文件**
# —— 那是第 4 步的形态（task 调度器当时还不传 params，读什么由函数自己决定）。档 B 收口后
# 它成了零调用方的死代码：今天的调度器一律把 ``params_from`` 指的文本读好再递进来
# （``tasks._read_params``），而且递进来的是**完整期望态**、由 ``validate_intent`` 收。
# 删掉它，本模块的读取通道只剩 ``load_intent`` 一条 —— 与 app 侧只剩 ``write_intent``
# 一条落盘通道对称（同一条链路的两端一起收口，别半做）。


# ──────────────────────────────────────────────
# 意图文件（档 B：policy 由 app 侧算好，helper 只消费）
# ──────────────────────────────────────────────

#: 意图文件版本。与 HELPER_VERSION 解耦：这里只标识**意图契约**的版本。
#: 2 → 3（第 8 步）：顶层多一个可选 ``handback``、每条规则多一个可选 ``net``，
#: 且 ``destinations`` 允许为空数组（「只切 IP、不加路由」的场景）。
#: 3 → 4：「照抄租约」—— ``net.pin`` 多两个可选布尔键
#: ``gateway_from_lease`` / ``dns_from_lease``（形状变了就是契约版本变了；
#: 与 ``HELPER_VERSION`` 同理）。
#: 4 → 5：**命令通道**（顶层 ``ops``）—— 显式动作与策略分家（见 ``_OP_FIELDS``）。
#: 同版行为变更：``net.pin`` 留空的「用 DHCP 拿到的那个」，从**照抄网卡当前配置**
#: 改成**真读 DHCP 租约**（``read_lease``）—— 旧行为在网卡已被我们固化过之后读到的
#: 正是自己上次写下去的值，于是「重新固化」永远拿不到新地址。
#: 5 → 6（2026-09-29）：**去掉 ``ops``** —— 命令搬去会话帧 ``op.run``，期望态正文
#: 只剩「该保持成什么样」。这是「通道换掉之后线格式跟着瘦一圈」的那一处。
#: 6 → 7（2026-10-06）：每条规则多一个可选 ``off_destinations`` —— 本条里**要撤**的
#: 那几个目标（``destinations`` 的子集）。一位 ``enabled`` 装不下「同一条里 A 加、B 撤」，
#: app 侧的手写混合态此前只能整份报错、整个组件停摆；这一版把逐目标开关放进线格式。
#: **纯增量**：不带该栏的 v6 形状语义一个字没变，只是版本位是严格相等判定、必须同批升级。
#: 7 → 8（2026-10-09）：``gateway`` 允许空串 —— **有目标**时它表示「这个下一跳由 helper
#: 在本轮从这块网卡上取」，只对带了 ``pin`` 的 ``net`` 成立（那是唯一会去读租约、
#: 从而真的知道这块网卡网关的场景）。同版行为：取不到时**该条既不叠也不撤**并在回显里
#: 报 ``gateway_src: "none"``（旧语义是「有目标必须给网关」，因此这条只把今天非法的
#: 配置救活，合法配置的含义一个字节没变）。helper 的 ``net`` 段多一栏 ``iface_gateway``、
#: 顶层多一栏 ``net_gateways``（网卡 → 它的网关，跨守护重启靠快照带回）。
#:
#: **这一位是真锁，不是标记**（2026-09-19 改，原注释自称「纯标记、不参与拒绝判定」）：
#: ``validate_routes`` 拿它跟文件里写的比，不等（含缺位）⇒ 整份拒绝 + 冻结。
#: 判据是「两侧说不说同一个版本」，不是「这份文件形状认不认得」—— 形状对得上也可能
#: 语义变了（例如 ``destinations: []`` 的语义在 v3 变过），猜不了的就不猜。
INTENT_VERSION = 8


def validate_intent(raw) -> tuple[dict, str | None]:
    """校验意图文件。返回 (data, error)；error 非 None 时 data 不可用。

    意图 = **完整期望态**：每条规则都必须带 app 侧算好的 ``apply``。helper 不再
    自己做「接口是否在线 / 网关是否命中 / 是否暂停 / 是否被手动覆盖」的判断，
    只做「按意图幂等收敛」（§7.4.3 档 B）。
    与 ``validate_routes`` 共用拓扑校验（同一套字段合法性 ＋ 契约版本真锁），
    只多一条硬要求。
    """
    data, err = validate_routes(raw)
    if err is not None:
        return {}, err
    for i, rule in enumerate(data["rules"]):
        if not isinstance(rule.get("apply"), bool):
            return {}, (
                f"rules[{i}].apply 缺失或不是布尔值（意图必须由 app 侧算好，"
                f"helper 不做匹配决策）：{rule.get('apply')!r}"
            )
        rule.setdefault("trigger_matched", False)
    # 原先这里再盖一次 ``INTENT_VERSION``（「认了就盖自己的号」）—— 真锁落地后是空动作，
    # 且会把「文件里写的什么」抹掉。版本号由 ``validate_routes`` 给出，真源只有一处。
    return data, None


def load_intent(text: str | None = None) -> tuple[dict | None, str | None]:
    """校验意图文件。返回 (data, error)。

    ``text`` 为 None（文件不存在）**不是错误** —— 表示 app 尚未给出意图
    （未运行 / 尚未首探），helper 保持上轮路由不动。
    """
    if text is None:
        return None, None
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"意图正文不是合法 JSON（可能是传了一半）：{e}"
    data, err = validate_intent(raw)
    if err is not None:
        return None, err
    return data, None
