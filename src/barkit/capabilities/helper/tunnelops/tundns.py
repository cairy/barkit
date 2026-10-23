"""**DNS 劫持与复原**：备份 / 还原 ``/etc/resolver``，判定「是否已被劫持」
（``_resolver_adopted_dns`` / ``_target_service_active``）与标记同步。

被劫持这件事必须**有据**（不是「我记得劫过」）⇒ 判据一律读当时的系统配置，读不出来
就当没劫（安全方向）。

``tunnelops.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import os
import re
import time
from .. import routeops
from ..safeio import (
    _atomic_write_json,
    _open_nofollow,
    dns_backup_path,
    hijack_marker_path,
    log,
)

from . import tunconsts
from . import tunshell
from . import tunwifi


def _backup_dns(svc: str) -> None:
    """仅在首次（备份文件不存在）记录原始 DNS，避免覆盖。

    只保留合法 IPv4：networksetup 在无自定义 DNS 时输出的是一句英文说明
    （"There aren't any DNS Servers set on Wi-Fi."），非英文系统下文案还会变。
    按「能不能解析成 IPv4」判定，天然与语言无关。

    ``svc`` 由调用方传入（它已经确认过服务名读得出）—— 备份与还原必须是**同一个**
    服务，各自去解析一次就可能备份 A、还原到 B。
    """
    if dns_backup_path().exists():
        return
    res = tunshell._run(["/usr/sbin/networksetup", "-getdnsservers", svc])
    servers = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
    servers = [s for s in servers if routeops.valid_ipv4(s)]
    saved = " ".join(servers) if servers else "empty"
    try:
        dns_backup_path().parent.mkdir(parents=True, exist_ok=True)
        with _open_nofollow(dns_backup_path(), mode=0o644) as f:
            f.write(saved)
        log(f"已备份原始 DNS：{saved}")
    except OSError as e:
        log(f"备份 DNS 失败（恢复时将按 empty 处理）：{e}")


def _restore_dns() -> None:
    """还原系统 DNS。

    读回的备份同样按 IPv4 校验后才作为参数下发——参数化执行固然挡得住 shell
    注入，但挡不住「备份文件被写成攻击者 IP 于是 root 把系统 DNS 指过去」。
    """
    svc = tunwifi._network_service()
    if svc is None:
        # 走到这里说明此前确实接管过 DNS（``_TUNNEL_DNS_HIJACKED``），而服务名缓存
        # 到本函数之后的收尾才清 —— 拿不到即异常态。**宁可不还原**（系统 DNS 仍指向
        # mapdns）也不写到一个猜来的服务上：写错服务＝把一个不存在的 DNS 永久留给
        # 那个服务，且现场毫无痕迹。备份文件一并留着，下次还原还有原始值可用。
        log("还原系统 DNS 失败：读不出当前网络服务名，未改动（避免改错服务）",
            key="tunnel.dns_restore_no_service")
        return
    saved = "empty"
    try:
        saved = dns_backup_path().read_text(encoding="utf-8").strip() or "empty"
    except OSError:
        pass
    servers = [] if saved == "empty" else [s for s in saved.split() if routeops.valid_ipv4(s)]
    if servers:
        tunshell._run(["/usr/sbin/networksetup", "-setdnsservers", svc] + servers)
    else:
        tunshell._run(["/usr/sbin/networksetup", "-setdnsservers", svc, "empty"])
    # 不刷缓存的话，应用会继续用隧道 fake IP（100.64.0.0/10）去连——隧道已拆，
    # 这些 IP 再无应答者，表现就是「关掉隧道后网络反而坏了」。
    tunshell._flush_dns_cache()
    try:
        dns_backup_path().unlink()
    except OSError:
        pass


def _target_service_active() -> bool:
    """DNS 劫持的目标服务当前是否在线（有真实 IPv4）。

    off-network（Wi-Fi 断开）时 _network_service() 仍返回 Wi-Fi 服务名，但对应接口
    没有可用 IPv4；此时 networksetup -setdnsservers 会返回 rc=0 却是『假成功』
    （resolver 根本不采纳）。故用接口真实 IPv4 判定，而非盲目信任 returncode。
    """
    iface = tunconsts._TUNNEL_WIFI_IFACE or tunwifi._primary_iface()
    return tunshell._has_ipv4(iface)


def _resolver_adopted_dns(ip: str, timeout: float | None = None) -> bool:
    """resolver 当前是否真的在用 ip（mapdns）做解析。

    只信任 `scutil --dns` 的真值，不信任 networksetup 的 returncode——后者在
    off-network 对未激活服务同样返回 0。

    扫**所有** resolver 块而不是只看 #1：隧道起来后 utun 等接口也会各自产生
    resolver 块，编号顺序并无保证；而只要任一 resolver 采用了 mapdns，查询就会
    走进隧道，判「已采纳」是准确的。

    timeout 是总预算（默认 RESOLVER_ADOPT_TIMEOUT），两重作用：既吸收「改完 →
    resolver 可见」的传播抖动（hev/utun 事件风暴期实测可达 1s+，给 0.5s 会误判），
    也保证保活轮不会被拖死——每次探测的 timeout 按剩余预算下发，不会出现
    「预算 0.5s 却单次卡 8s」。
    """
    budget = tunconsts.RESOLVER_ADOPT_TIMEOUT if timeout is None else timeout
    # 预算是「还要等多久」，不是「等到几点」⇒ 用单调钟。墙上时钟被校回时
    # ``time.time()`` 版的 deadline 会被推远，这个循环能空转到预算之外很久。
    deadline = time.monotonic() + max(0.0, budget)
    while True:
        left = deadline - time.monotonic()
        res = tunshell._run(["/usr/sbin/scutil", "--dns"], timeout=max(1, int(min(left, 8))))
        if res.returncode == 0:
            # 左边界 (?<![\d.]) 不可省：否则 nameserver 是 18.8.8.8 时会被 8.8.8.8 误命中
            pat = r"nameserver\[\d+\]\s*:\s*(?<![\d.])" + re.escape(ip) + r"\b"
            for block in re.split(r"resolver #", res.stdout)[1:]:
                if re.search(pat, block):
                    return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


def _apply_dns_hijack(data: dict, state: dict) -> tuple[str, str]:
    """把系统 DNS 指向 mapdns，并按「resolver 实际是否采纳」如实上报。

    返回 (状态, 失败详情)：
      "ok"          resolver 已在用 mapdns（本轮无需改动，或改动已被采纳）
      "unavailable" 读不出目标服务名 / 目标服务离线 / 写了但 resolver 未采纳
                    → 系统 DNS 未被接管
      "failed"      networksetup 命令本身失败（详情供调用方拼错误文案）

    **每轮都调用，是保活而非一次性配置**：系统 DNS 会被 Wi-Fi 重连、DHCP 续租、
    睡眠唤醒、其他软件改回去，只在首轮写一次等于放弃自愈，结果就是长时间停留在
    「IPv4 走隧道、DNS 走本机」的泄漏态而无人知晓。所以这里先用
    _resolver_adopted_dns 探测，采纳了就不重复写，掉了才补写。

    三个前置顺序不能调换：先要服务名（纯内存，读不出就到此为止），再判服务是否在线
    （一次 ifconfig，很便宜），最后才探测 resolver——反过来会在 off-network 时白跑
    一整轮 3s 的探测。

    调用方决定失败语义：激活期 "failed" 要整条回滚，保活期只报错、不拆隧道。
    """
    dns_ip = data.get("dns") or tunconsts.HEV_DNS
    svc = tunwifi._network_service()
    if svc is None:
        # 读不出目标服务名 ⇒ **一根手指都不碰**（理由见 _network_service）。
        # 数据面不受影响，故按 "unavailable" 处理：保留隧道、如实上报。
        state["service_unknown"] = True
        log("DNS 劫持不可用：读不出当前网络服务名，未改动系统 DNS（避免改错服务）；"
            "隧道数据面不受影响。", key="tunnel.dns_no_service")
        return "unavailable", ""
    if not _target_service_active():
        # 目标服务离线（off-network）：networksetup 会「假成功」，写了也白写。
        # 这里**不回滚整条隧道**是刻意的——没网时本机 DNS 同样不可达（不构成
        # 「泄漏到本机网络」），保留数据面比整条拆掉有用；代价只是域名解析不可用，
        # 已通过 state["dns_unavailable"] 如实上报。
        state["dns_unavailable"] = True
        log("DNS 劫持不可用：目标网络服务离线（无活跃 IPv4），resolver 不会采纳；"
            "请连接 Wi-Fi 后再启用隧道 DNS。隧道数据面不受影响。",
            key="tunnel.dns_offline")
        return "unavailable", ""
    if _resolver_adopted_dns(dns_ip):
        # 已是 mapdns，无需重设。仍要置旗标承担拆除时的还原责任——helper 重启后
        # 从 hijack 标记恢复会话时走的正是这条路径。
        tunconsts._TUNNEL_DNS_HIJACKED = True
        state["dns_set"] = True
        return "ok", ""
    _backup_dns(svc)
    res = tunshell._run(["/usr/sbin/networksetup", "-setdnsservers", svc, dns_ip])
    if res.returncode != 0:
        return "failed", (res.stderr or res.stdout or "未知")
    # 命令成功即承担还原责任：哪怕之后 DNS 又被系统事件冲掉，拆除时也要还原，
    # 否则会残留一条指向已消失 mapdns 的系统 DNS。
    tunconsts._TUNNEL_DNS_HIJACKED = True
    tunshell._flush_dns_cache()          # 否则应用继续用旧缓存里的真实 IP 直连
    if _resolver_adopted_dns(dns_ip):
        state["dns_set"] = True
        return "ok", ""
    state["dns_unavailable"] = True
    log(f"DNS 劫持未生效：setdnsservers 返回成功但 resolver 未采纳 {dns_ip}"
        "（疑似服务离线/假成功），DNS 维持本机直连",
        key="tunnel.dns_not_adopted")
    return "unavailable", ""


def _sync_hijack_marker() -> None:
    """把「本进程当前劫持了哪些系统网络状态」落盘（root 私有目录，O_NOFOLLOW）。"""
    hijacked = tunconsts._TUNNEL_ROUTE_HIJACKED or tunconsts._TUNNEL_DNS_HIJACKED or tunconsts._TUNNEL_IPV6_OFF
    path = hijack_marker_path()
    if not hijacked:
        try:
            path.unlink()
        except OSError:
            pass
        return
    payload = {
        "pid": os.getpid(),
        "hev_pid": int(tunconsts._TUNNEL_PROC.pid) if tunconsts._TUNNEL_PROC is not None else None,
        "utun": tunconsts._TUNNEL_UTUN,
        # 服务名随标记落盘：崩溃回收是在**新进程**里跑的，内存缓存是空的 —— 不带上它
        # 就只能猜（旧写法兜 "Wi-Fi"，非英文系统必猜错，DNS 便永远还原不回去）。
        "service": tunconsts._TUNNEL_SERVICE,
        "route": tunconsts._TUNNEL_ROUTE_HIJACKED,
        "dns": tunconsts._TUNNEL_DNS_HIJACKED,
        "ipv6_off": tunconsts._TUNNEL_IPV6_OFF,
        "ts": int(time.time()),
    }
    try:
        _atomic_write_json(path, payload)
    except OSError as e:
        log(f"写隧道劫持标记失败（崩溃恢复将不可用）：{e}")
