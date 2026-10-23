"""隧道的**生命周期编排**：激活（起 hev → 建 utun → 加路由 → 劫持 DNS）、
保活复检、失败降级与拆除（``_teardown_tunnel`` 必须把系统状态逐一还回去）。

拆除那一条是**兜底线**：即使本轮是在半途失败的，也必须走到「系统看起来从没被改过」。

``tunnelops.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import time
from ..safeio import (
    _hev_log_path,
    _hev_log_tail,
    hev_binary,
    log,
)

from . import tunconsts
from . import tundns
from . import tunhev
from . import tunroute
from . import tunshell
from . import tunwifi


def _activate_tunnel(data: dict, state: dict) -> str:
    """起隧道（hev → 等 utun → /1 + DNS + 可选禁 v6）。

    返回 "ok" / "pending" / "fail"：
    - pending：hev 已起但 utun 还没建出。**绝不在此阻塞等待**——本函数和
      route_guard 的 run_cycle() 在同一轮循环里串行执行，一次 10s 的 sleep
      会把规则路由的检查一起拖住。等 utun 交给后续轮次（main() 在 pending
      期间把轮询降到 1s）。
    - fail：任何一步失败都回滚已生效的半截状态（见 _fail_tunnel），绝不留下
      「Wi-Fi 已切走、/1 路由却没加上」这种等于全面断网的中间态。

    /1 路由与系统 DNS 是否劫持，分别受隧道意图的 hijack_route / hijack_dns
    控制（**默认都不劫持**，见 §安全默认值）。关闭某一项时该项完全不动系统网络栈
    （不抢全量路由、不改 DNS），用于「Mac 与 iPad 本就在同一网络、无需强行绕路」
    的场景；两项都关即「旁路模式」——只建 utun，系统网络一无所动。
    """

    # 必须在改路由/DNS 之前定住网络目标（隧道一生效，默认路由就指向 utun 了）
    tunwifi._resolve_network_targets()

    # 上游 SOCKS 代理（app 侧 bridge @ 127.0.0.1:local_port）必须先就绪，
    # 否则 hev 会一直阻塞等上游、永远建不出 utun，进而陷入「起→20s 超时杀→重启」的死循环。
    # 先探活：未就绪直接 pending（不起 hev、不杀、不重启），等 bridge/iPad 链路通了再激活。
    _port = int(data.get("local_port", 1080))
    if not tunhev._upstream_ready(_port):
        state["pending"] = True
        log(f"上游 SOCKS 代理未就绪（127.0.0.1:{_port} 无监听），"
            "等待 iPad 已连 + mac_socks_bridge 已起后再拉起隧道",
            key="tunnel.upstream_not_ready")
        return "pending"

    if tunconsts._TUNNEL_PENDING is None:
        ok, why = tunhev._hev_integrity_check(hev_binary())
        if not ok:
            state["error"] = f"hev 二进制未通过执行前完整性自检：{why}"
            log(f"hev 完整性自检未通过，拒绝启动：{why}")
            return _fail_tunnel(state)
        if not tunhev._start_hev(data):
            state["error"] = "hev-socks5-tunnel 启动失败"
            return _fail_tunnel(state)
        # 等待窗口按「过了多久」算 ⇒ 单调钟（同 tundns 的预算循环）。
        tunconsts._TUNNEL_PENDING = {"at": time.monotonic(), "data": data}

    utun = tunshell._find_utun()
    if not utun:
        if time.monotonic() - tunconsts._TUNNEL_PENDING["at"] <= tunconsts.UTUN_WAIT_TIMEOUT:
            state["pending"] = True
            return "pending"
        # 超出等待窗口：只有 hev 自己退了（rc 非 None）才算失败并回滚；
        # hev 还活着说明它仍在等上游连通，此时切勿杀掉它——杀掉只会让下一轮
        # 重新拉起、再次等待，形成每分钟重启的死循环。继续 pending，等上游
        # 真正连通后下一轮自然建出 utun。
        rc = tunconsts._TUNNEL_PROC.poll() if tunconsts._TUNNEL_PROC is not None else None
        if rc is not None:
            state["error"] = f"hev 已退出（rc={rc}），详见 {_hev_log_path()}"
            tail = _hev_log_tail()
            if tail:
                state["error"] += "\n" + tail
            tunconsts._TUNNEL_PENDING = None
            return _fail_tunnel(state)
        # hev 活着但 utun 还没出来：多半上游刚连通、hev 正在建 tun，再等等。
        state["pending"] = True
        if not tunconsts._TUNNEL_PENDING.get("warned"):
            tunconsts._TUNNEL_PENDING["warned"] = True
            log(f"hev 已起但 {tunconsts.UTUN_WAIT_TIMEOUT:.0f}s 仍未建出 utun：上游 127.0.0.1:"
                f"{_port} 可能刚连通，继续等待（不杀 hev）")
        return "pending"

    tunconsts._TUNNEL_PENDING = None
    tunconsts._TUNNEL_UTUN = utun
    state["utun"] = utun

    # mapdns 主机路由：无论是否劫持全量路由都加。旁路模式下它是唯一能在不动
    # 系统网络的前提下验证隧道的入口（dig @198.18.0.2）。失败不致命——全局模式
    # 下 /1 路由本就覆盖该地址，旁路模式下无非验证不了，都不该因此回滚（回滚
    # 反而会把已经建好的 utun 一起拆掉）。
    if tunroute._add_route_host(data.get("dns") or tunconsts.HEV_DNS, utun):
        tunconsts._TUNNEL_MAPDNS_ROUTE = True

    if data.get("hijack_route", False):
        ok = tunroute._add_route_utun("0.0.0.0/1", utun)
        ok = tunroute._add_route_utun("128.0.0.0/1", utun) and ok
        if not ok:
            # 抢不到全量路由就不要切 Wi-Fi/DNS：半吊子状态 = 全面断网
            state["error"] = "添加 /1 路由失败"
            return _fail_tunnel(state)
    tunconsts._TUNNEL_ROUTE_HIJACKED = bool(data.get("hijack_route", False))

    if data.get("hijack_dns", False):
        status, detail = tundns._apply_dns_hijack(data, state)
        if status == "failed":
            state["error"] = (
                f"设置系统 DNS 失败（{detail}）；"
                "已回滚，避免 IPv4 走隧道而 DNS 走本机的泄漏态"
            )
            return _fail_tunnel(state)
        # "unavailable"（服务离线 / resolver 未采纳）不回滚：数据面已经建好，
        # 拆掉只会从「DNS 不可用」变成「彻底断网」。状态已如实上报。

    if data.get("disable_ipv6"):
        tunhev._disable_ipv6(state)
    state["ipad_reachable"] = True
    tundns._sync_hijack_marker()
    return "ok"


def _fail_tunnel(state: dict) -> str:
    """激活失败：回滚已生效的半截状态，只保留 state["error"]。

    _teardown_tunnel() 由 hijack 标记门控且幂等，这里可以无脑调用。
    """
    err = state.get("error")
    _teardown_tunnel(state)
    state["error"] = err
    state["ipad_reachable"] = False
    return "fail"


def _ensure_tunnel(data: dict, state: dict) -> str:
    """持续保活：hev 死亡则重新完整激活；已存活则幂等补一遍路由/DNS/v6。

    /1 路由与系统 DNS 是否劫持分别受 hijack_route / hijack_dns 控制（默认都不劫持）。
    运行期将开关关闭时，本轮即撤掉此前已加的路由/恢复的 DNS（无需等拆除）。

    mapdns 主机路由与 hijack 无关，只要隧道在就保持存在（旁路模式的验证入口）。

    返回 "ok" / "pending" / "fail"，语义同 _activate_tunnel。
    """
    if not tunhev._hev_alive():
        return _activate_tunnel(data, state)
    # 上游复检：hev 活着 ≠ 上游活着。app 退出会带走 127.0.0.1:local_port 的 SOCKS 桥，
    # 而 hev 不会因此退出、utun/路由/DNS 劫持全在——隧道变成流量黑洞（hijack_route
    # 开启时整机假连接）。这里每轮探一次（对 loopback 一次 TCP connect，开销可忽略），
    # 上游死了主动拆，下一轮走激活路径重新探活，恢复成 pending 等上游回来。
    _port = int(data.get("local_port", 1080))
    if not tunhev._upstream_ready(_port):
        log(f"上游 SOCKS 代理已断开（127.0.0.1:{_port} 无监听，app 可能已退出），"
            "隧道已拆除以免流量黑洞；上游恢复后自动重建",
            key="tunnel.upstream_gone")
        _teardown_tunnel(state)
        state["pending"] = True
        return "pending"
    # 缓存优先（2026-10-12 评审）：hev 存活期间接口名与地址都不变，旧写法每轮先
    # 全量扫（`ifconfig -l` ＋ 每个 utun 各一次，本机常态 5-10 fork）是纯稳态开销。
    # 缓存命中只花单次 `ifconfig <名>` 验「接口在且地址是隧道地址」（判据同
    # _find_utun 逐接口那一遍）；验活失败 ⇒ 回退全量扫自愈。名字来自上一世 hev 的
    # 场景走不到缓存：hev 重启时 _hev_alive 为假 ⇒ 直接走激活路径，那里会重设缓存
    # （_activate_tunnel）。
    utun = tunconsts._TUNNEL_UTUN
    if utun is None or not tunshell._utun_exists(utun):
        utun = tunshell._find_utun()
        tunconsts._TUNNEL_UTUN = utun     # 扫不到即清空，别把旧名留在缓存里
    if utun is None:
        # hev 还活着但 utun 暂时没了（重建中）：退回 pending，下一轮再补路由/DNS，
        # 不要带着 stale/None 的 utun 去加路由（会失败）。
        state["pending"] = True
        return "pending"

    # mapdns 主机路由：与 hijack 无关，隧道活着就保持（hev 重启会重建接口，
    # 内核随之丢掉这条路由，故每轮幂等补一次）。
    if tunroute._add_route_host(data.get("dns") or tunconsts.HEV_DNS, utun):
        tunconsts._TUNNEL_MAPDNS_ROUTE = True

    if data.get("hijack_route", False):
        if utun:
            ok = tunroute._add_route_utun("0.0.0.0/1", utun)
            ok = tunroute._add_route_utun("128.0.0.0/1", utun) and ok
            if not ok:
                state["error"] = "/1 路由补建失败（隧道可能已失效）"
        tunconsts._TUNNEL_ROUTE_HIJACKED = True
    elif tunconsts._TUNNEL_ROUTE_HIJACKED:
        # 运行期关闭路由劫持 → 撤掉此前加的 /1 路由
        cur = utun or tunshell._find_utun()
        if cur:
            tunroute._del_route_utun("0.0.0.0/1", cur)
            tunroute._del_route_utun("128.0.0.0/1", cur)
        tunconsts._TUNNEL_ROUTE_HIJACKED = False

    if data.get("hijack_dns", False):
        status, detail = tundns._apply_dns_hijack(data, state)
        if status == "failed":
            state["error"] = f"重设系统 DNS 失败（{detail}）（解析可能走本机）"
    elif tunconsts._TUNNEL_DNS_HIJACKED:
        # 运行期关闭 DNS 劫持 → 还原系统 DNS
        tundns._restore_dns()
        tunconsts._TUNNEL_DNS_HIJACKED = False
        state["dns_set"] = False

    if data.get("disable_ipv6") and not tunconsts._TUNNEL_IPV6_OFF:
        tunhev._disable_ipv6(state)
    tundns._sync_hijack_marker()
    return "ok"


def _teardown_tunnel(state: dict | None = None) -> None:
    """拆除隧道。仅撤掉本轮实际生效的路由/DNS（受 hijack 开关控制）。

    即便意图被拒绝（data=None），全局态 _TUNNEL_ROUTE_HIJACKED /
    _TUNNEL_DNS_HIJACKED 仍能正确反映此前是否已劫持，从而安全回滚、不误伤系统网络。
    幂等，可无脑调用（退出钩子/崩溃恢复都依赖这一点）。
    """
    if state is None:
        state = {}
    tunhev._stop_hev()
    tunconsts._TUNNEL_PENDING = None
    # 只有真的改过路由才去扫接口：空闲轮次（最常见的路径）不该每轮 fork 一串
    # ifconfig（-l 一次 + 每个 utun 一次）。mapdns 主机路由同样算「改过」——
    # 旁路模式下它是唯一动过的系统状态，不扫就会残留一条指向已消失接口的路由。
    if tunconsts._TUNNEL_ROUTE_HIJACKED or tunconsts._TUNNEL_MAPDNS_ROUTE or tunconsts._TUNNEL_UTUN:
        utun = tunconsts._TUNNEL_UTUN or tunshell._find_utun()
        if utun:
            if tunconsts._TUNNEL_ROUTE_HIJACKED:
                tunroute._del_route_utun("0.0.0.0/1", utun)
                tunroute._del_route_utun("128.0.0.0/1", utun)
            if tunconsts._TUNNEL_MAPDNS_ROUTE:
                tunroute._del_route_host(tunconsts.HEV_DNS, utun)
    tunconsts._TUNNEL_ROUTE_HIJACKED = False
    tunconsts._TUNNEL_MAPDNS_ROUTE = False
    if tunconsts._TUNNEL_DNS_HIJACKED:
        tundns._restore_dns()
    tunconsts._TUNNEL_DNS_HIJACKED = False
    if tunconsts._TUNNEL_IPV6_OFF:
        svc = tunwifi._network_service()
        if svc is None:
            log("IPv6 未复原：读不出当前网络服务名，未改动（避免改错服务）",
                key="tunnel.ipv6_restore_no_service")
        else:
            tunshell._run(["/usr/sbin/networksetup", "-setv6automatic", svc])
        tunconsts._TUNNEL_IPV6_OFF = False
    tunconsts._TUNNEL_UTUN = None
    tunconsts._TUNNEL_SERVICE = None          # 下次激活重新解析（可能已换网络）
    state["utun"] = None
    state["dns_set"] = False
    state["dns_unavailable"] = False
    state["service_unknown"] = False
    state["ipad_reachable"] = False
    tundns._sync_hijack_marker()
