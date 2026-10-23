"""routeops：路由原语实现（机制层，§7.4.3「系统状态变更的执行」）。

本模块只做「怎么改路由」，不做「什么条件下该加哪条路由」——后者是 policy，
留在插件侧（档 B）。被 primitives 的 route.sync 原语调用。

- 目标一律经 ipaddress 严格解析后再进命令，subprocess 一律参数化、shell=False
- 幂等：ensure 重复调用无副作用；remove 前先确认路由确实经本网关（宁不清理不误删）

档 B（批次 4 · S3b）：本模块**不再做匹配决策**。``rule_matched``（是否该生效）
与暂停冻结、force_apply 覆盖都是 policy，已迁到 app 侧 ``plugins/route_guard/intent.py``；
helper 只按 app 声明的那份**完整意图**幂等收敛。

意图的载体就是会话上那一帧 ``intent.set("route", …)``
（schema 演进到 v2：每条规则必带 ``apply``），
**不另开旁路文件**——既然只重装一次，为兼容旧 helper 维护双通道是白付的复杂度；
旧 helper 遇到未知的 ``apply`` 字段会整份拒绝 → 冻结保持现状（安全态）。
**版本不符同样整份拒绝**（``validate_routes`` 顶部的契约版本真锁，ROADMAP E4 ②）；
两条拒绝都走「写状态 ＋ 保持上轮路由不动」，用户侧的可见性靠面板与 `barkit doctor` D1。

这里的 ``iface_up`` / ``current_gateways`` / ``current_ipv4`` 是**查询机制**（不是决策），
app 侧直接复用 —— 匹配判定（含 §2.1 的「生效网段」）留在 ``intent.py``。

网卡配置（§2.5，批次 8）也在这里，但**只实现机制**：「这块网卡该配成什么」由 app 侧
按场景算好写在 ``rules[].net`` 里、「该交还了」写在顶层 ``handback`` 里。本模块负责的是
那三件配套 —— ``baseline`` 快照（只留最早一份）、回滚窗口（写坏了自己退回去）、
以及交还时的还原。**这是全仓唯一会改变用户网络状态的地方**，所以它比别处更保守：
读不出来就不动、探不到证据就不回滚、写之前先拦掉「网关不在本网段」这类必然出不去
的配置。

顶层原本还有一条 ``ops``（**命令通道**：用户当场按下的 ``renew`` / ``release``）。
**它 2026-09-29 搬走了**，搬到了会话帧 ``op.run`` 上（见 ``run_net_op``）—— 理由有两条：

- 命令塞在期望态正文里时，「这条执行过没有」只能靠 ``seq`` 水位线，而水位线写在回执表里、
  回执表又要塞回状态文件 ⇒ 「命令的通道」和「事实的通道」搅在一起；帧天然自带
  「一帧一命令、回帧即回执」，这两样一起消失。
- 命令被排在**策略**（``handback``）之后 ⇒ 场景一旦命中（哪怕命中的正是我们自己固化
  出去的地址）命令永远轮不到执行，用户看到的就是「点了没用」。现在命令走一条独立的路，
  与期望态无关、也不受 ``paused`` 影响。


**本文件自 2026-10-02 起只放「一轮周期」**：原 1526 行的正文按职责搬进**同包的兄弟
模块**（`ipvalid` / `netmode` / `validate` / `routes` / `lease` / `netsvc` / `baseline` /
`converge` / `ops`），本文件只留 `_apply_net` 与 `run_cycle` 这两件「把一轮跑完」的事。
`__init__.py` 全量再导出 ⇒ `from .routeops import …` 一行未改。

``routeops.py`` 拆包：正文逐字节搬移，零行为变更。

``routeops.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import time
from ..consts import DEFAULT_POLL
from ..safeio import log
from ..state import (
    read_state,
    write_state,
)

from . import gwledger
from .converge import (
    _converge_iface, _gateway_for, _gateway_from_routes, _idle_net, _iface_gateways,
    _needs_iface_gateway, _pick_handback, _recently_rolled_back,
    _rollback_if_lost,
)
from .routes import ensure_removed, ensure_route, parse_destination
from .validate import load_intent


# ──────────────────────────────────────────────
# 留空的下一跳：这条路由的网关从哪来（v8）
# ──────────────────────────────────────────────

#: 规则回显 ``gateway_src`` 的三个取值：本轮**这条规则用的那个网关**是谁给的。
#: 它同时回答两个问题，故只立一个栏位：① 这条路由能不能动（``none`` ⇒ 一个都没动）；
#: ② 面板要不要把那个网关说出来（``iface`` ⇒ 它是我们自己取的，用户压根没写过，
#: 不说出来他无从核对）。**读的一侧（插件 ``checks.route_gateway_src``）照抄这三个
#: 字面量**，两侧由 ``test_gateway_src_vocabulary_matches_on_both_sides`` 钉住。
GATEWAY_SRC_RULE = "rule"       # 配置 / 编译期写死的那个（一直如此）
GATEWAY_SRC_IFACE = "iface"     # helper 从本场景固化那块网卡上取的（v8 新增）
GATEWAY_SRC_NONE = "none"       # 取不到 ⇒ 本条既不叠也不撤
#
# 那本账的「怎么记、怎么取」三个函数在 ``converge``（``_needs_iface_gateway`` /
# ``_iface_gateways`` / ``_gateway_for``）：一轮周期与**一次性命令**（``ops.run_net_op``）
# 都要维护同一本账，而后者不该反向依赖「把一轮跑完」这个模块。


def _apply_net(data: dict, prev_net: dict, now: int) -> dict:
    """本轮网卡侧的动作（§2.5）。``data`` 是校验过的意图。返回进 ``state["net"]`` 的段。

    四条判据，**顺序就是分量**：

    1. **回滚先于一切** —— 上一轮把自己关在门外时，这一轮该做的是退回去，而不是照着
       同一份（可能正是它把网卡配坏的）意图再写一遍。
    2. **停用 ⇒ 不收不散**（§3.2「停用 = 不碰 = 保持现状」）：规则的 ``apply`` 在暂停
       态是「沿用上轮」，照它收敛会把网卡**重新写一遍** —— 那不是冻结。
       **而交还照办**：显式 `[交还系统]` 是用户当场按下的动作，不是自动化。
    3. **有场景要接管 ⇒ 收敛到它**：**第一个**要接管的场景说了算（``rules`` 已按
       priority 排过，§2.1「第 1 个命中者胜」）。路由可以多条叠加，网卡配置只有一份。
    4. **没场景要 ⇒ 按交还清单还原**；都没有就把上一轮那段**原样带下去**（``_idle_net``）。

    **旧的「命令先于策略」那条 2026-09-29 去掉了**：命令不再走这条函数 —— 它搬去了
    会话帧 ``op.run``（``run_net_op``），与期望态无关、也不受 ``paused`` 影响。
    于是这里不会再出现「一条命令排在一轮策略判断之后、永远轮不到执行」那种局面。

    ⚠️ 一次只收敛**一张**网卡：多网卡同时接管要等路由模型放开（app 侧 ``rules`` 那份
    docstring 记着它还欠什么）。与其假装两张都管了，不如只报一张。
    """
    paused = bool(data.get("paused"))
    cadence = data.get("poll", DEFAULT_POLL)   # route task 的实际间隔（节拍）
    if not paused:
        rolled = _rollback_if_lost(prev_net, now, cadence)
        if rolled is not None:
            return rolled

    chosen: dict | None = None
    if not paused:
        for rule in data["rules"]:          # 已按 priority 排过
            net = rule.get("net")
            if net and rule.get("apply"):
                chosen = rule
                break
    # 有场景要接管时，清单里**同一张**网卡不必再交还 —— 那两条会互相拆台。
    # 这一条由下面的早退直接保证：``chosen`` 非空就一定收敛并返回，走不到 ``hand``。
    hand = list(data.get("handback") or [])
    handback_dns = bool(data.get("handback_dns", True))

    if chosen is not None:
        iface, cfg = chosen["iface"], chosen["net"]
        if _recently_rolled_back(prev_net, iface, cfg, now, cadence):
            # 刚为这张网卡、这份意图退过一次：这一轮不再写回去（见该函数的理由）。
            return dict(prev_net)
        res = _converge_iface(iface, cfg, handback=False, prev_net=prev_net,
                              need_gateway=_needs_iface_gateway(chosen))
        # ``applied_at`` 归 ``_converge_iface`` 记（真写那一轮盖新值、稳态轮照抄上一轮）；
        # 走不到那两条路的（失败 / 交还）在这里补当前时刻兜底。
        res.setdefault("applied_at", now)
        return res
    if hand:
        res = _converge_iface(_pick_handback(hand, prev_net), {},
                              handback=True, skip_dns=not handback_dns,
                              prev_net=prev_net)
        res["applied_at"] = now
        return res
    return _idle_net(prev_net, paused, now)


def run_cycle(text: str | None = None) -> dict | None:
    """按 app 侧算好的**意图**收敛路由（档 B：本函数不含任何匹配决策）。

    三种输入：
      - 意图文件非法 → 整份拒绝，保持上轮路由不动，错误写状态
      - 意图文件不存在（app 未运行/尚未首探）→ **冻结**，保持上轮路由不动
      - 有意图 → **逐目标**决定：``apply`` 为真、且该目标不在本条的
        ``off_destinations`` 里 ⇒ 幂等添加；否则仅当确实经本网关生效时删除
        （删除安全性与迁移前完全一致：``ensure_removed`` 先 ``route_via`` 确认）
      - **停用（``paused``）→ 路由一个都不动**（§3.2「停用 = 不碰」）：不用 ``apply``
        收敛。那一位在停用态只是 app 尽力冻结出来的估计，而冻结靠的正是本函数写出的
        回显 —— 回显缺位时它退化成 ``False``，照它收敛等于把生效中的路由撤光。

    留空的下一跳（v8）：``rules[].gateway`` 为空串表示「这个下一跳由你从这块网卡上取」
    —— 它只对带 ``pin`` 的 ``net`` 成立（``validate._pins_lease``），而取值的来源是
    ``net_gateways`` 那本账（本轮网卡收敛时读到什么就记什么，见 ``_iface_gateways``）。
    **取不到时该条既不叠也不撤**（``gateway_src: "none"``）：加要网关，撤也要网关
    （``ensure_removed`` 先确认那条路由确实经这个网关生效），与其假装动过，不如如实说
    「这一轮没动它」—— 下一轮租约到了、或用户补上网关，它自己就动起来了。

    ``state`` 的形状（v8 起）：顶层 ``net`` / ``net_gateways`` / ``rules`` ／ 每规则
    ``gateway``（**本轮用的那个**，不再是意图里那个）+ ``gateway_src``。读那本账与那两栏
    的只有本进程（``net_gateways``，**回显**：账本体在 ``gwledger`` 那个文件里）与插件
    （``gateway`` / ``gateway_src``）。

    返回写出的 state dict。
    """
    data, err = load_intent(text)
    state: dict = {
        "ok": err is None,
        "checked_at": int(time.time()),
        "poll": data["poll"] if data else DEFAULT_POLL,
        "error": err,
        "paused": bool(data.get("paused")) if data else False,
        "has_intent": data is not None,
        "rules": [],
    }
    # 网卡网关那本账**先带下去，且必须在两条早退之前**：它记的是「这块网卡上一次被
    # 收敛时看到的网关」，与「这一轮有没有意图」无关。早退写出的 state 若没有这一栏，
    # ``record`` 是整体替换 ⇒ 账被抹空；而网卡一旦被固化成 Manual，``ipconfig getpacket``
    # 必然失败、网卡上也可能没有 router（pin 只固化地址时就不写它），账是留空下一跳
    # 唯一还剩的来源（2026-10-19：账被抹空 ⇒ office 命中后三个目标全 hold）。
    # 账的**第一来源是它自己的文件**（``gwledger``）：快照里那一格只作「同进程上一轮」
    # 就近覆盖。反过来的话（只认快照），快照被首帧 flush 抹平 / 版本不符 / 换了进程，
    # 账就一起没了 —— 2026-10-19 到 10-20 连着三次真机「命中却不叠」，每次都是这一条。
    prev = read_state()
    known = gwledger.load()
    known.update(prev.get("net_gateways") or {})   # 同进程上一轮更近，压过文件
    state["net_gateways"] = dict(known)
    if err is not None:
        log(f"意图文件被拒绝，保持上轮路由不动：{err}", key="route.intent_rejected")
        write_state(state)
        return state
    if data is None:
        log("本轮没有路由意图（app 尚未声明，或刚撤回）：保持上轮路由不动",
            key="route.no_intent")
        write_state(state)
        return state

    # 网卡配置先于路由：路由叠在网卡之上，先把网卡放对位置，明细路有下一跳可走。
    # 顺序也在安全侧：网卡写失败时下面的路由仍会照意图收敛（幂等），不会两头都不做。
    # （``prev`` 与 ``net_gateways`` 在上面就取好了 —— 那本账早退时也得带下去。）
    paused = bool(data.get("paused"))
    state["net"] = _apply_net(data, prev.get("net") or {}, int(time.time()))
    # 网卡的网关那本账（见 ``_iface_gateways``）：已经知道的带下去，本轮读到的盖上去，
    # **学到就落盘**（``gwledger.save``）—— 这就是「场景变化那一刻把它存起来」。
    gateways = _iface_gateways(known, state["net"])
    state["net_gateways"] = gateways
    gwledger.save(gateways)
    # 停用时的「上轮生效态」。app 发来的 ``apply`` 是它**尽力**冻结出来的，而它冻得住
    # 全靠这份回显（``compute_apply`` 的 ``prior_applied``）。回显缺位（守护刚重启、
    # 会话刚重连、快照还没读回来）时那一位会退化成 ``False`` —— 照它收敛就是把正在
    # 生效的路由**全撤掉**，恰恰是「停用 = 不碰」最不该发生的事（2026-10-17）。
    prev_applied = {str(r.get("name")): bool(r.get("applied"))
                    for r in (prev.get("rules") or []) if isinstance(r, dict)}

    for rule in data["rules"]:
        results = []
        # 本轮这条规则用的下一跳：写死的那个优先；空串 ⇒ 从这本账里取（v8）。
        gateway = str(rule["gateway"] or "")
        src = GATEWAY_SRC_RULE
        if not gateway:
            gateway = _gateway_for(gateways, rule)
            if not gateway:
                # 账是空的（历史被抹平 / 快照缺位）：最后的证据是系统里那条旧路由 ——
                # 它就是我们上次叠上去的，网关写在它上面。捡回来并记账，下一轮就不用
                # 再捡（见 ``_gateway_from_routes``）。
                gateway = _gateway_from_routes(rule, state.get("net"))
                if gateway:
                    gateways = _iface_gateways(gateways, {
                        "iface": rule["iface"], "iface_gateway": gateway,
                        "net": rule["net"]})
                    state["net_gateways"] = gateways
                    gwledger.save(gateways)      # 捡回来的也算「学到了」，当场落盘
            src = GATEWAY_SRC_IFACE if gateway else GATEWAY_SRC_NONE
        if paused:
            # 停用 = 不碰（§3.2）：路由也**一个都不动**。``apply`` 在停用态只是 app
            # 尽力冻结出来的估计，而冻结靠的正是那份回显；回显还没回来时它是 False，
            # 照它收敛会把生效中的路由撤掉（见上面 ``prev_applied`` 那段）。
            results = [{"destination": dest, "ok": True, "action": "hold", "error": None}
                       for dest in rule["destinations"]]
        elif src == GATEWAY_SRC_NONE:
            # 取不到 ⇒ 一个都不动（加/撤都要网关），只如实报「这一轮没动它」。
            results = [{"destination": dest, "ok": True, "action": "hold", "error": None}
                       for dest in rule["destinations"]]
        else:
            # 本条里**要撤**的那几个目标（v7）：``apply`` 是整条规则的一位，逐目标的那一位
            # 只能由这一栏表达。不在集合里 = 跟随 ``apply``。
            off = set(rule.get("off_destinations") or ())
            for dest in rule["destinations"]:
                parsed = parse_destination(dest)
                if parsed is None:
                    results.append({"destination": dest, "ok": False, "error": "无法解析目标"})
                    continue
                want = rule["apply"] and dest not in off
                ok = ensure_route(parsed, gateway) if want else ensure_removed(parsed, gateway)
                results.append({
                    "destination": dest,
                    "ok": ok,
                    "error": None if ok else ("添加失败" if want else "删除失败"),
                    "action": "add" if want else "remove",
                })
        state["rules"].append({
            "name": rule["name"],
            # 停用态照上轮记账：这一轮压根没动它，报 app 那份估计只会让两件事互相
            # 拖死 —— 它读到 False 就冻结成 False，下一轮更「确信」该撤。
            "applied": (prev_applied.get(rule["name"], bool(rule["apply"]))
                        if paused else rule["apply"]),
            "matched": rule["trigger_matched"] and rule["enabled"],
            "trigger_matched": rule["trigger_matched"],
            "enabled": rule["enabled"],
            "iface": rule["iface"],
            # **本轮这条规则用的**那个下一跳（v8 起不再是「意图里那个」）：留空时它是
            # 从这本账里取到的值，取不到就是空串。面板据此把「自己取到的」说出来。
            "gateway": gateway,
            "gateway_src": src,
            "destinations": results,
        })
        # 每条规则独立节流：稳态下这句每 30s 每规则一行，一天一万多行，
        # 而它报的其实是「和上轮一样」。真变了会立刻落盘（签名不同）。
        log(f"规则 {rule['name']}: apply={rule['apply']} →"
            f" {[r['action'] + ':' + r['destination'] + ('OK' if r['ok'] else 'FAIL') for r in results]}",
            key=f"route.rule.{rule['name']}")
    write_state(state)
    return state
