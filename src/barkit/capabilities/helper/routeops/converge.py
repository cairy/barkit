"""**收敛与回滚保险**：一轮里把每张网卡推向目标态，并在「刚写下去的探不到」时
按窗口自动回 baseline。

窗口三件套（``ROLLBACK_WINDOW_S`` / ``ROLLBACK_GRACE_S`` /
``ROLLBACK_CADENCE_MARGIN_S``）在这里 —— 窗口窄过节拍的话保险一次都不会触发。

``routeops.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import time
from ..safeio import log

from .baseline import _restore_baseline, _same, _write_target, ensure_baseline, read_baseline
# ⚠ 这几个走模块对象（``<mod>.f``）而不是绑定副本（``from .<mod> import f``）：``tests/test_route_guard.py`` 要 monkeypatch 它们，打成副本的话补丁落在调用方命名空间上、真身纹丝不动 ⇒ 判据静默失效。见《拆包》第 2 步第 3 条。
from .ipvalid import valid_ipv4
from .lease import read_lease
from .netmode import MODE_DHCP, MODE_STATIC, _in_subnet, _set_dns, _target_of
from . import netsvc
from .netsvc import service_for_iface
from . import routes


#: 回滚窗口（秒）：改完网卡后这段时间内**探不到刚写下去的东西**，就当作把自己关在
#: 门外了，自动回 baseline。这是本组件唯一会改变用户网络状态的地方 —— 改的如果正是
#: 当前在用的那块网卡，配置一落就是一秒级断网，没有自动退出路径的话，用户只能
#: 手动去系统设置里改回来（或者干脆够不着这台机器）。
ROLLBACK_WINDOW_S = 20

#: 回滚探测的宽限期（秒）：刚 setmanual 完，地址与路由表还没收敛，立刻探必然「探不到」
#: 而误回滚。留几秒等它落定。
ROLLBACK_GRACE_S = 3

#: 探活只能发生在**下一轮节拍**上，而节拍（意图里的 ``poll``，默认 30s）比窗口上沿
#: （20s）长 —— 不把节拍算进上沿，下一轮跑到时早出了窗口，这条保险窗一次都不会触发
#: （2026-09-30 发现：它一直是死的）。余量盖住「节拍到点后真正跑起来」的抖动。
ROLLBACK_CADENCE_MARGIN_S = 10


def _rollback_window_s(cadence: float) -> int:
    """回滚窗口的实际宽度：下沿恒为 ``ROLLBACK_GRACE_S``，上沿必须**盖住节拍**。

    ``cadence`` 取意图里的 ``poll`` —— 它就是 route task 的实际间隔
    （``primitives._next_interval`` 照它调 ``tasks`` 的节拍），不是另一个真源。
    """
    return max(ROLLBACK_WINDOW_S, int(cadence) + ROLLBACK_CADENCE_MARGIN_S)


def _lease_needed(cfg: dict, need_gateway: bool = False) -> bool:
    """这份 ``net`` 落地**需不需要租约**（不需要就不去 fork ``ipconfig``）。

    ``static`` 与「pin 把 ip / mask 都写死、且不照抄网关/DNS」的配置一个字都不来自租约。

    ``need_gateway``（v8）：本轮有条规则的下一跳留空了，要拿**这块网卡的网关**去补 ——
    那一格里唯一可能的来源就是租约里那个（我们没把它写进网卡时，网卡上什么都没有）。
    故这种情况也得读一次：网卡被切成 Manual 之后 ``ipconfig getpacket`` 必然失败，
    「读过一次」是这份值唯一的来处（读过之后由 ``run_cycle`` 记进持久账）。
    """
    if not isinstance(cfg, dict) or cfg.get("mode") != MODE_DHCP:
        return False
    pin = cfg.get("pin")
    if not isinstance(pin, dict):
        return False
    if not pin.get("ip") or not pin.get("mask"):
        return True                     # 留空 = 用 DHCP 拿到的那个
    return bool(need_gateway or pin.get("gateway_from_lease") or pin.get("dns_from_lease"))


def _iface_gateway(target: dict, lease) -> str:
    """本场景给这块网卡**定下来的那个网关**（v8）：写给网卡的那个，否则租约里那个。

    写给网卡的那个（``static`` / ``pin.gateway`` / ``gateway_from_lease``）当然算；
    「只固化地址」那一支网卡上没有网关，可 DHCP 给过一个 —— 那正是留空的下一跳唯一
    可能的来源，故一并记下来（谁需要它见 ``run_cycle``）。两边都拿不到 ⇒ 空串
    （「还没有」，不是「没有网关」：下一轮租约到了就有了）。
    """
    written = str((target or {}).get("gateway") or "")
    if valid_ipv4(written):
        return written
    leased = str((lease or {}).get("gateway") or "")
    return leased if valid_ipv4(leased) else ""


# ──────────────────────────────────────────────
# 留空的下一跳：这条路由的网关从哪来（v8）
# ──────────────────────────────────────────────


def _needs_iface_gateway(rule: dict) -> bool:
    """这条规则是不是「下一跳留空、要靠 helper 从网卡上取」。

    app 侧把那种行编译成空串（``compile._line_fields``），校验也只在固化租约的场景上
    放它进来（``validate._pins_lease``）。这里只判「有没有目标」——没有目标时那个空串
    什么也不影响。
    """
    return bool(rule.get("destinations")) and not str(rule.get("gateway") or "")


def _iface_gateways(prev, seg: dict | None) -> dict:
    """网卡 → **最后一次把这块网卡收敛到哪一份配置时**看到的网关（**持久账**：跨守护
    重启靠它自己的文件 ``gwledger`` 带回，不靠快照 —— 这一格以前寄生在 route 观测块里，
    被整块替换抹掉过三次）。

    本函数是这本账**唯一的合并口径**（「学到的东西怎么并进去」只在这里写一遍）；
    ``gwledger`` 只负责那一个 dict 的读与写。

    为什么要有这本账、而不直接读这一刻网卡上的配置：**值读一次就没了** —— 场景一命中，
    网卡就被切成 Manual，``ipconfig getpacket`` 从此必然失败（``read_lease`` 的
    docstring）。而留空的下一跳每一轮都得知道它是谁（撤那条路由时也要用它确认
    ``route_via``）。故本轮真读到了就记下来，之后每轮原样带下去。

    连 ``net`` 一起记（而不是只记一个网关）：这本账只按网卡索引，而**同一块网卡会被
    不同场景配成不同的样子** —— 换过场景、或用户改了那份配置，旧值就未必还算数。
    读的时候要求那份配置**逐字相同**才认（``_gateway_for``），于是「留空 ＝ 用**本场景**
    固化那块网卡的网关」里的「本场景」是真的被核对过的，而不是「这块网卡上次不知被谁
    配上时看到的那个」。查不到就按「取不到」办（那条路由这一轮不动）。

    **只拿合法值去更新**：读不到（本轮网卡没被收敛、或租约还没到）时不许把已经知道的
    那个抹成空 —— 那会让一条健康的配置在下一轮突然「取不到网关」。

    **两个写方，故放在这里**（而不是 ``routeops``）：一轮周期（``run_cycle``）与用户
    当场按下的一次性命令（``ops.run_net_op``）**都要维护这本账**，而后者不该反向依赖
    「把一轮跑完」那个模块。本模块是双方共同的下层。
    """
    known = dict(prev) if isinstance(prev, dict) else {}
    iface = str((seg or {}).get("iface") or "")
    gateway = str((seg or {}).get("iface_gateway") or "")
    if iface and valid_ipv4(gateway):
        known[iface] = {"gateway": gateway, "net": (seg or {}).get("net")}
    return known


def _gateway_for(known, rule: dict) -> str:
    """这条规则该用的那个「网卡网关」；没记过、或记的那份配置跟它不同 ⇒ 空串。"""
    entry = known.get(rule.get("iface")) if isinstance(known, dict) else None
    if not isinstance(entry, dict) or entry.get("net") != rule.get("net"):
        return ""
    gateway = str(entry.get("gateway") or "")
    return gateway if valid_ipv4(gateway) else ""


def _gateway_from_routes(rule: dict, seg: dict | None) -> str:
    """账被抹空之后的**最后一份证据**：这条目标现在经本网卡的那条路由。

    账（``net_gateways``）本该记得上次用的网关，可它只在本进程里积累 —— 历史版本
    早退抹过、快照也可能缺位。而**已经叠上去的路由还留在系统里**，网关就写在它上面：
    读它，等于把「上次到底经谁」从系统自己的账本里捡回来（2026-10-19 真机：账是
    ``{}``、租约读不到，而两条旧路由仍指向 ``10.99.1.254``）。

    只认三条同时成立：目标确实是这条规则要的；那条路由走的是**这条规则的网卡**；
    网关落在该网卡**当前地址的网段**内（否则是上个网络的残迹，拿它加路由只会更坏）。
    任何一条不成立都按「取不到」办，不猜。

    旧路由也一条都没有时（典型：刚开机，上一轮叠的路由随重启一起消失），再退一级：
    读 **baseline** —— 那是我们**第一次改动这块网卡之前**的原始 DHCP 配置，网关就写在
    上面（2026-10-20 真机：账被首帧 flush 抹平、旧路由也随重启没了，而 baseline 里
    ``pyxis`` 仍记着 ``10.99.1.254``）。同样只认「网关落在当前网段内」。
    """
    iface = str(rule.get("iface") or "")
    seg = seg if isinstance(seg, dict) and seg.get("iface") == iface else {}
    wrote = (seg or {}).get("wrote") or {}
    ip = str(wrote.get("ip") or "")
    mask = str(wrote.get("mask") or "")
    for dest in rule.get("destinations") or ():
        parsed = routes.parse_destination(dest)
        if parsed is None:
            continue
        info = routes.route_get(parsed["probe"])
        if not isinstance(info, dict) or info.get("interface") != iface:
            continue
        gateway = str(info.get("gateway") or "")
        if not valid_ipv4(gateway):
            continue
        if valid_ipv4(ip) and valid_ipv4(mask) and not _in_subnet(ip, mask, gateway):
            continue
        return gateway
    service = str((seg or {}).get("service") or "")
    entry = read_baseline().get(service) if service else None
    if isinstance(entry, dict) and entry.get("iface") == iface:
        gateway = str(entry.get("gateway") or "")
        stale = (valid_ipv4(ip) and valid_ipv4(mask)
                 and not _in_subnet(ip, mask, gateway))
        if valid_ipv4(gateway) and not stale:
            return gateway
    return ""


def _converge_iface(iface: str, cfg: dict, *, handback: bool,
                    skip_dns: bool = False, prev_net: dict | None = None,
                    need_gateway: bool = False) -> dict:
    """把一张网卡收敛到 ``cfg``，或交还系统。返回一段可进 ``state["net"]`` 的结果。

    返回段里那三个字段是给**上一层**的判据看的，不是给面板看的：

    - ``managed`` ＝ 网卡此刻停在**我们写下的**那份配置上（`[交还系统]` 显隐、
      `net` 机读数都靠它）；
    - ``wrote`` ＝ 写下去的那一份（解析过租约之后的实际值），回滚窗口与
      「还在不在我们手上」都拿它比；
    - ``mode`` ＝ 运行形态（``static`` / ``dhcp+pin`` / ``dhcp``）。

    另有 ``iface_gateway``（v8）：本场景给这块网卡定下来的网关，给留空的下一跳用
    （见 ``_iface_gateway`` 与 ``run_cycle``）。稳态早退那两条路**不写它** ——
    它们没读租约，而上一层那张账只在拿到合法值时才更新，故旧值照旧带着走。

    ``need_gateway``（v8）：本轮有条规则的下一跳留空 ⇒ 即使这份 ``net`` 本来不必读
    租约（地址写死、不照抄），也要读一次（理由见 ``_lease_needed``）。

    ``prev_net`` 是上一轮的这段（可能为空）。它换来一条**稳态早退**：网卡还停在上轮
    为**这一份**意图写下的那一份上时，本轮不读租约、不写网卡。没有它，``net.pin``
    留空的场景每轮都要重新解析一次租约 —— 而网卡已经在我们写下的 Manual 配置上时
    压根没有租约可读（``ipconfig getpacket`` 必然失败），那会把一个健康的稳态报成
    「租约里没拿到可用的 ip / mask」。早退接不住的场合（上一轮的账缺了或坏掉了，
    典型如守护刚启动、快照缺位），下面还有一道**租约替身**兜底：网卡已是 Manual 时
    拿它此刻的配置补 ``pin`` 留空的位 —— 只补空位，pin 写死的值照旧赢。
    """
    res = {"iface": iface, "service": None, "mode": "", "managed": False,
           "wrote": None, "net": None, "action": "failed", "error": None}
    service = service_for_iface(iface)
    if not service:
        res["error"] = f"查不到 {iface} 对应的网络服务名，未动网卡"
        return res
    res["service"] = service
    current = netsvc.read_service_config(service)
    if current is None:
        res["error"] = f"读不出 {service} 的当前配置，未动网卡"
        return res
    # baseline 必须在**第一次改动之前**留，故「交还」那条路也要先确认它存在
    if not ensure_baseline(service, iface, current):
        res["error"] = f"{service} 的 baseline 留不下来，未动网卡"
        return res
    if handback:
        ok = _restore_baseline(service, current, skip_dns=skip_dns)
        res["action"] = "handback" if ok else "failed"
        if not ok:
            res["error"] = f"交还 {service} 失败（未动网卡）"
        return res
    # 稳态早退（见 docstring）：网卡还在我们手上、意图也没变 ⇒ 这一轮什么都不做
    prev_wrote = (prev_net or {}).get("wrote")
    if (isinstance(prev_wrote, dict) and (prev_net or {}).get("iface") == iface
            and (prev_net or {}).get("net") == cfg and _same(current, prev_wrote)):
        res.update(mode=str(prev_wrote.get("mode") or ""), managed=True,
                   wrote=prev_wrote, net=cfg, action="applied")
        # 「写下去的时刻」必须是**真写那一轮**的（回滚窗口从它起算）：稳态轮不写，
        # 时刻照抄上一轮。每轮都盖 now 的话，下一轮看到的「距写入多久」永远是约一轮
        # 的长度 —— 宽限期算「刚过」、窗口上沿算「已错过」，保险窗一次都不会触发。
        res["applied_at"] = int((prev_net or {}).get("applied_at") or 0)
        return res
    needs_lease = _lease_needed(cfg, need_gateway)
    lease = read_lease(iface) if needs_lease else None
    if lease is None and needs_lease and current.get("mode") == MODE_STATIC \
            and valid_ipv4(current.get("ip") or "") \
            and valid_ipv4(current.get("mask") or ""):
        # 网卡已是 Manual 且没有租约可读 —— 这**不是错误**，正是「固化着」的现场
        # （``read_lease`` 的 docstring：getpacket 在非 DHCP 模式必然失败）。常态由
        # 上面的稳态早退接住；走到这里说明「上一轮的账」不可用（首次上线、快照缺位、
        # 或上一轮失败过一次）。此时 pin 留空要补的那几位就在网卡上 —— 当初写下去的
        # 正是租约给的那份。拿网卡此刻的配置当租约替身：pin 里写死的位照旧赢，
        # 替身只补留空的位。**DHCP 模式**下读不到租约不在此列：那是「还没拿到」，
        # 下一轮租约到了自然收敛，照实报错是对的（也能自愈）。
        # ⚠️ 没有这道替身，失败段（``wrote`` 为空）会让稳态早退永远 miss，之后每一轮
        # 都重新走「读租约 → 读不到 → 报错」—— 一次瞬时失败就把状态机永久锁死在
        # 「网卡配置失败」（2026-09-30 真机快照里那段 ``error`` 即此）。
        lease = {"ip": current["ip"], "mask": current["mask"],
                 "gateway": current.get("gateway") or "",
                 "dns": list(current.get("dns") or [])}
        log(f"{service}：网卡已是手动配置且没有租约（固化中的正常现场），"
            f"按网卡当前配置继续", key=f"route.net.no_lease.{service}")
    target, terr = _target_of(cfg, lease or {})
    if terr:
        res["error"] = f"{service}：{terr}"
        return res
    if target is None:
        # 纯 DHCP：**一根手指都不碰**（§2.5）。已固化过的网卡要回 DHCP 走 handback，
        # 不在这个分支顺手 setdhcp —— 那会让「切场景」把用户自己配的静态 IP 改掉。
        res["mode"] = MODE_DHCP
        res["action"] = "none"
        return res
    res["mode"] = target["mode"]
    res["net"] = cfg
    # ``pin`` 没声明网关 ⇒ **网卡上就不留 router**（``_write_target`` 省略该参数即清空）。
    # 这是刻意的：多出来的 router 会被系统当成默认路由，影响其他路由与 Apple 连续互通
    # （2026-10-19 用户裁定，别再把「不固化」做成「保留」）。要用的网关由下面那本账记着
    # —— 它写在这一行之前，读的是**写之前**的网卡/租约，所以抹掉不会丢。
    # 本场景给这块网卡定下来的网关（v8）：留空的下一跳靠它。只在**这条**路上记 ——
    # 上面那两条早退/失败的路没读租约，报了空值就会把上一轮已经知道的那个抹掉。
    res["iface_gateway"] = _iface_gateway(target, lease)
    if _same(current, target):
        # 已经在目标那一份上 ⇒ 不写。helper 每轮都会把同一份意图再说一遍，
        # 「说一遍就写一遍」等于每 30s 重配一次网卡。
        res["managed"], res["wrote"], res["action"] = True, target, "applied"
        # 「写下去的时刻」必须是**真写那一轮**的（回滚窗口从它起算）：稳态轮不写，
        # 时刻照抄上一轮。每轮都盖 now 的话，下一轮看到的「距写入多久」永远是约一轮
        # 的长度 —— 宽限期算「刚过」、窗口上沿算「已错过」，保险窗一次都不会触发。
        res["applied_at"] = int((prev_net or {}).get("applied_at") or 0)
        return res
    if not _write_target(service, target):
        res["error"] = f"写 {service} 的网卡配置失败"
        return res
    if target.get("dns"):
        _set_dns(service, list(target["dns"]))
    res["managed"], res["wrote"], res["action"] = True, target, "applied"
    res["applied_at"] = int(time.time())        # 真写这一轮：回滚窗口从这里起算
    return res


def _net_lost(iface: str, wrote: dict) -> bool:
    """我们写下去的东西**还在不在**这张网卡上？（回滚窗口的探针）

    「失联」在这里**只取拿得到可靠证据的那一半**，宁可不报也不误报：

    - **地址**永远探得动（``ifconfig``）：写下去时填了地址，就要求它还在；
    - **网关**只在「默认路由正走这张网卡」时才有意义。别的网卡是主路时，
      ``route -n get default`` 给的是**别人**的网关（它不看接口），拿它当我们的判据
      会在完全正常的配置上误判成失联，然后回滚掉一份好配置 —— 那比不报更坏。
      所以：默认路由走本网卡 ⇒ 要求网关正是我们写的那个；否则退回只比地址。
    """
    if wrote.get("ip") and wrote["ip"] not in routes.current_ipv4(iface):
        return True
    if not wrote.get("gateway"):
        return False
    info = routes.route_get("default") or {}
    if info.get("interface") != iface:
        return False
    return info.get("gateway") != wrote["gateway"]


def _rollback_if_lost(prev_net: dict, now: int, cadence: float = 0) -> dict | None:
    """回滚窗口（§2.5 配套第 2 条）：写坏网卡后 N 秒内探不到写下去的东西 ⇒ 回 baseline。

    这是本组件唯一会改变用户网络状态的地方，而它最坏的结果是把用户**关在门外**
    （改的正是当前在用的那块网卡 ⇒ 一秒级断网 ⇒ 用户够不着这台机器）。窗口就是为
    这一刻留的退路：探不到 = 十有八九是我把自己配坏了，退回进门前那份。

    四条判据缺一不回滚（**宁可不回滚，也不误回滚**）：

    1. 上一轮**真写过**（``action == "applied"``）且没退过（``rolled_back``）；
    2. 在窗口内、且**过了宽限期** —— 刚 setmanual 完地址还没落定，立刻探必然探不到；
       上沿盖住节拍（``_rollback_window_s``）：探活只能发生在下一轮，节拍比
       ``ROLLBACK_WINDOW_S`` 长时窗口必须随之放宽，否则一次都轮不到；
    3. 有可探的东西（写下去时的地址或网关，至少一个）；
    4. ``_net_lost`` 说它不在网卡上了。

    ⚠️ 停用（``paused``）期间由调用方跳过：§3.2「停用 = 一根手指都不碰」。代价是
    停用期间失联不会自动回滚 —— 但那正是用户要的「保持现状」，出路是显式 `[交还系统]`。
    """
    if not isinstance(prev_net, dict):
        return None
    if prev_net.get("action") != "applied" or prev_net.get("rolled_back"):
        return None
    service, iface = prev_net.get("service"), prev_net.get("iface")
    wrote = prev_net.get("wrote")
    if not (service and iface and isinstance(wrote, dict)):
        return None
    if not wrote.get("ip") and not wrote.get("gateway"):
        return None                     # 没写地址也没写网关，无从探起
    elapsed = now - int(prev_net.get("applied_at") or 0)
    if not ROLLBACK_GRACE_S <= elapsed <= _rollback_window_s(cadence):
        return None
    if not _net_lost(iface, wrote):
        return None
    log(f"回滚窗口内探不到 {iface} 上写下去的配置 ⇒ 回 baseline")
    ok = _restore_baseline(service)
    return {
        "iface": iface, "service": service, "mode": "", "managed": not ok,
        "wrote": wrote, "net": prev_net.get("net"), "applied_at": now,
        "action": "rolled_back" if ok else "failed", "rolled_back": True,
        "error": None if ok else f"自动回滚 {service} 失败",
    }


def _recently_rolled_back(prev_net: dict, iface: str, cfg: dict, now: int,
                          cadence: float = 0) -> bool:
    """这张网卡刚**因为同一份意图**退过一次吗？（防「写坏 → 回滚 → 再写坏」的循环）

    没有这道闸，一条恒写不成功的配置会每轮转一圈：写 → 探不到 → 回滚 → 再写，
    而每转一圈都在断用户的网。判据里带 ``net == cfg``：用户改了配置（意图变了）
    就该重试，闸只拦「同一份意图」。闸宽同样盖住节拍（``_rollback_window_s``）：
    节拍 30s、闸 20s 的话，下一轮跑到时闸已过期，拦了个寂寞。
    """
    if not isinstance(prev_net, dict) or prev_net.get("action") != "rolled_back":
        return False
    if prev_net.get("iface") != iface or prev_net.get("net") != cfg:
        return False
    return now - int(prev_net.get("applied_at") or 0) <= _rollback_window_s(cadence)


def _still_ours(service: str, prev_net: dict) -> bool:
    """网卡此刻是否**还停在我们写下去的那份配置上**。

    只在一处用：「上一轮我们写过、这一轮既不用收敛也不用交还」时，把「还在我们手上」
    这个事实核对一遍。用户在系统设置里手改过、或系统自己换了地址，读数就该跟上 ——
    否则面板会一直说「静态配置已生效」，而它早就不在了。

    读不出来一律按「**不在我们手上了**」处理：这条路径只影响读数与 `[交还系统]` 的
    显隐，保守一点不会写坏任何东西。
    """
    wrote = prev_net.get("wrote")
    if not service or not isinstance(wrote, dict):
        return False
    return _same(netsvc.read_service_config(service), wrote)


def _idle_net(prev_net: dict, paused: bool, now: int) -> dict:
    """本轮不收敛也不交还：网卡保持现状。返回进 state 的那一段。

    ⚠️ 这里**不能**用一个空段盖掉上一轮那份。回滚窗口的判据挂在「上一轮写下去的
    地址 / 网关」上，而它最典型的触发场景恰恰是**本轮什么都不做**：配置写坏 ⇒ 网卡
    失联 ⇒ 连「命中」都不成立 ⇒ 上面两条路径都是空的。盖下去的话那个窗口永远不会
    触发 —— 也就是说，这段代码存在的唯一理由会被自己抹掉。

    窗口过去之后仍然记着「是我们写的」（``managed``，但不再探活）：网卡上确实还停着
    那份配置，`net` 那台机照实说。真交还了才清掉它。
    """
    blank = {"iface": "", "service": None, "mode": "", "managed": False,
             "wrote": None, "net": None, "action": "frozen" if paused else "none",
             "error": None, "applied_at": now}
    if not isinstance(prev_net, dict) or not prev_net.get("managed"):
        return blank
    if paused or _still_ours(prev_net.get("service") or "", prev_net):
        out = dict(prev_net)
        out["action"] = "frozen" if paused else "applied"
        out["error"] = None
        return out
    # 不在我们手上了（用户手改 / 系统换了地址）：读数跟上，不再声称在管
    return dict(blank, iface=prev_net.get("iface") or "",
                service=prev_net.get("service"))


def _pick_handback(hand: list, prev_net: dict) -> str:
    """交还清单里先动哪一张：**上一轮真管过的那张优先**。

    不这么做会出现一个安静的漏：清单里有两张网卡时，每轮都从头取第一张，第二张
    永远轮不到。而真正需要交还的只有「我们写过的」那张 —— ``prev_net`` 知道是谁。
    """
    if isinstance(prev_net, dict) and prev_net.get("managed"):
        ours = prev_net.get("iface")
        if ours and ours in hand:
            return ours
    return hand[0]
