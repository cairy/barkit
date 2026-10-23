"""hosts_guard/resolve：解析链（policy）与解析缓存。

为什么在 app 侧
--------------
§7.4.3 把「配置生成与校验」划给插件：用哪种方式解析（DoH / UDP 直查 / 系统 DNS）、
什么节律解析、解析失败怎么办，都是 policy，**不需要 root**。helper 只按 app 算好的
**完整期望态**（每条带 ``effective_ip``）渲染 /etc/hosts 标记块——它不再认识
DoH 端点，也不再做节流。

档 B 的取舍（与路由同一条）：app 退出后 helper 按**上次期望态**保活，不再自主
重解析。hosts 条目多为静态 / 广告屏蔽列表，IP 变化慢，可接受。

fake IP 为什么两侧都判
---------------------
``100.64.0.0/10`` 是 hev mapdns 的 CGNAT 段，写进 hosts 会形成「查到假 IP →
只查本地记录」的不可自愈死循环。本模块**拒绝写入**是策略层拦截；helper 侧仍
保留同一条校验作为 root 侧最后防线（安全红线不是 policy，不回迁）。

块渲染为什么也在 app 侧
---------------------
§7.4.3 把「渲染 hosts 块内容」归 app（与「解析链」同属配置生成），§7.4.4 把
``file.write_block`` 的入参定为 ``{marker, content}``——即 **content 由调用方渲染**。
故本模块的 :func:`render_block` 产出块正文，helper 只做安全校验 + 落盘
（起始/结束标记行由 helper 用自身常量补，避免两侧各写一份装饰文本）。

探活为什么放在长驻服务里
-----------------------
解析要发网络请求（可能几百毫秒一条），放进 tick 会占住工作线程。故用框架托管的
**周期型 resident service**（``interval`` 声明节律，线程/时钟/起停全归框架）。
服务内部再按每条的 ``resolved_at`` 与配置的 ``resolve_poll`` 判断是否真的到期，
这样用户改它无需重建服务。

落盘为什么只有一条路径
--------------------
（2026-09-29 通道换成会话后这一节改写成「发声为什么只有一条路径」。）

意图的声明者**只有** ``HostsResolveService.poll`` 走的那一条：UI 动作（``sync()``）
与长驻服务都经它进出，变化检测也只在它里面。与 route_guard 的
``RouteProbeService`` 同构——provider 纯计算，服务负责变化检测 + **声明意图**。

从前这里写的是「落盘只有一条路径（``write_rules``）」：那份 ``hosts_rules.json``
是通道，会话落地后连同它一起删了，``publish`` 这个回调接的就是它的位。
解析缓存（``hosts_resolved.json``）**也删了**（§11.5 判定）：解析结果本来就要作为
意图正文的一部分发出去，它在内存里天然存在；app 重启后重新解析的代价只是几条 DNS
查询，而缓存本身带 TTL。
"""

from __future__ import annotations

import json
import logging
import random
import socket
import struct
import time
import urllib.parse
import urllib.request
from typing import Callable, Optional

from barkit.core.log_throttle import log_throttled
from barkit.plugins.hosts_guard.models import (
    METHOD_AUTO,
    METHOD_SYSTEM,
    is_fake_ip,
    normalize_host,
    valid_ip,
)

log = logging.getLogger(__name__)

#: 解析方式 → DoH 端点 / UDP 服务器（与 helper 历史实现一致）
DOH_ENDPOINTS = {
    "doh-pub": "https://doh.pub/dns-query?name={host}&type=A",
    "doh-ali": "https://dns.alidns.com/resolve?name={host}&type=A",
}
UDP_SERVERS = {
    "udp-ali": "223.5.5.5",
    "udp-tx": "119.29.29.29",
}
#: ``method=auto`` 时的降级顺序（DoH 走 TCP 443，不受隧道里 UDP 53 丢包影响）
METHOD_CHAIN = ("doh-pub", "doh-ali", "udp-ali", "udp-tx")

#: 探活服务的固定节律（秒）。真正的解析间隔由配置 ``resolve_poll`` 在 provider 内判断，
#: 这里只决定「多久检查一次谁到期」——故改它无需重建服务。
PROBE_INTERVAL = 30.0

#: 解析函数签名：``(host, method) -> (ip, used_method, error)``
ResolveFn = Callable[[str, str], tuple[Optional[str], Optional[str], Optional[str]]]


# ──────────────────────────────────────────────
# 解析（机制：纯网络查询，无决策）
# ──────────────────────────────────────────────

def doh_resolve(host: str, method: str, timeout: float = 6.0) -> Optional[str]:
    """DoH（JSON API）查 A 记录。走 TCP 443，不受隧道里 UDP 53 丢包影响。"""
    url_tpl = DOH_ENDPOINTS[method]
    url = url_tpl.format(host=urllib.parse.quote(host))
    req = urllib.request.Request(url, headers={"accept": "application/dns-json"})
    # 显式禁用代理：避免系统代理把查询绕进隧道
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    for ans in payload.get("Answer") or []:
        if ans.get("type") == 1 and isinstance(ans.get("data"), str):
            ip = ans["data"].strip()
            if valid_ip(ip):
                return ip
    return None


def udp_resolve(host: str, method: str, timeout: float = 4.0) -> Optional[str]:
    """UDP 53 直查 A 记录（纯标准库手搓，不依赖 dig，便于精确控超时）。"""
    server = UDP_SERVERS[method]
    qid = random.randint(0, 0xFFFF)
    query = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)   # 标准查询 + RD
    for label in host.split("."):
        raw = label.encode("ascii", "ignore")
        if not raw:
            return None
        query += bytes([len(raw)]) + raw
    query += b"\x00" + struct.pack(">HH", 1, 1)                # 根 + QTYPE=A, QCLASS=IN

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(query, (server, 53))
        data, _addr = sock.recvfrom(2048)
    finally:
        sock.close()

    if len(data) < 12:
        return None
    r_id, _flags, qdcount, ancount = struct.unpack(">HHHH", data[:8])
    if r_id != qid:
        return None
    off = 12
    for _ in range(qdcount):                                   # 跳过 question 段
        while off < len(data) and data[off] != 0:
            off += 1 + data[off]
        off += 5
    for _ in range(ancount):
        if off >= len(data):
            return None
        if data[off] & 0xC0 == 0xC0:                           # 压缩指针（2 字节）
            off += 2
        else:
            while off < len(data) and data[off] != 0:
                off += 1 + data[off]
            off += 1
        if off + 10 > len(data):
            return None
        rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", data[off:off + 10])
        off += 10
        if rtype == 1 and rdlen == 4 and off + 4 <= len(data):
            return ".".join(str(b) for b in data[off:off + 4])
        off += rdlen
    return None


def system_resolve(host: str) -> Optional[str]:
    """系统 DNS（getaddrinfo → mDNSResponder）。

    隧道开启时 resolver #1 是 mapdns，会返回 fake IP——由 ``is_fake_ip`` 兜底拒绝。
    """
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET)
    except OSError:
        return None
    for info in infos:
        ip = info[4][0]
        if valid_ip(ip):
            return ip
    return None


def resolve_one(host: str, method: str) -> Optional[str]:
    if method in DOH_ENDPOINTS:
        return doh_resolve(host, method)
    if method in UDP_SERVERS:
        return udp_resolve(host, method)
    if method == METHOD_SYSTEM:
        return system_resolve(host)
    return None


def resolve_host(host: str, method: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """按方式解析 A 记录，返回 ``(ip, 实际方式, 错误)``。

    ``method=auto`` 时按 :data:`METHOD_CHAIN` 依次降级，取第一个拿到合法 IP 的
    结果。全部失败时 ip 为 None，调用方保留上次 IP 不清除。
    """
    chain = METHOD_CHAIN if method == METHOD_AUTO else (method,)
    errs = []
    for m in chain:
        try:
            ip = resolve_one(host, m)
        except Exception as e:  # noqa: BLE001 —— 任一方式抛错都只作降级依据
            errs.append(f"{m}:{type(e).__name__}")
            continue
        if ip and valid_ip(ip):
            return ip, m, None
        errs.append(f"{m}:无A记录")
    return None, None, "；".join(errs) or "无可用解析方式"


# ──────────────────────────────────────────────
# 期望态：条目 + 缓存 → 每条的最终 IP
# ──────────────────────────────────────────────

def due_hosts(entries: list[dict], cache: dict, resolve_poll: int,
              now: float | None = None) -> set[str]:
    """本轮真正需要发起网络解析的域名。

    判据：``auto`` 且（无缓存 或 缓存年龄 ≥ ``resolve_poll``）。非 auto 永不解析。
    """
    now = time.time() if now is None else now
    out = set()
    for e in entries:
        if not e.get("auto"):
            continue
        host = normalize_host(e["host"])
        at = (cache.get(host) or {}).get("at", 0)
        if now - float(at) >= float(resolve_poll):
            out.add(host)
    return out


def apply_resolutions(entries: list[dict], cache: dict,
                      to_resolve: set[str] | None = None,
                      resolver: ResolveFn | None = None,
                      now: int | None = None) -> list[dict]:
    """给每条条目补上 ``effective_ip`` / ``status``（完整期望态的核心）。

    ``to_resolve`` 控制**这一轮真正发起网络解析**的域名集合：
      - ``None``  → 全部 auto 条目都解析
      - ``set()`` → 一条都不解析，只用缓存（app 启动 / 纯回显，不发网络请求）

    状态语义：非 auto → ``static``；解析成功 → ``ok``；落在 fake 段 → ``fake``
    （**不写入**，沿用缓存或静态 IP）；解析失败 → ``stale``（有缓存）/ ``idle``
    （无缓存）—— 绝不因一次失败把生效中的条目清空。
    """
    resolver = resolver or resolve_host
    now = int(time.time()) if now is None else now
    out = []
    for e in entries:
        host = normalize_host(e["host"])
        item = dict(e)
        if not e.get("auto"):
            item.update(effective_ip=e["ip"], status="static",
                        resolved_method=None, resolved_at=0, error=None)
            out.append(item)
            continue

        cached = cache.get(host) or {}
        if to_resolve is not None and host not in to_resolve:
            item.update(effective_ip=cached.get("ip", e["ip"]),
                        status="ok" if cached else "idle",
                        resolved_method=cached.get("method"),
                        resolved_at=int(cached.get("at", 0)), error=None)
            out.append(item)
            continue

        ip, used, err = resolver(host, e.get("method") or METHOD_AUTO)
        if ip and not is_fake_ip(ip):
            cache[host] = {"ip": ip, "method": used, "at": now}
            item.update(effective_ip=ip, status="ok", resolved_method=used,
                        resolved_at=now, error=None)
        elif ip:
            item.update(effective_ip=cached.get("ip", e["ip"]), status="fake",
                        resolved_method=used, resolved_at=now,
                        error=f"解析结果落在 fake IP 段：{ip}")
        else:
            item.update(effective_ip=cached.get("ip", e["ip"]),
                        status="stale" if cached else "idle",
                        resolved_method=cached.get("method"),
                        resolved_at=int(cached.get("at", 0)),
                        error=err or "解析失败")
        out.append(item)
    return out


# ──────────────────────────────────────────────
# 块渲染（§7.4.3：渲染归 app，helper 只落盘）
# ──────────────────────────────────────────────

def render_block(entries: list[dict]) -> str:
    """把期望态条目渲染成 /etc/hosts 托管块的**正文**（不含起止标记行）。

    每行 ``<ip>\t<host>``；用制表符对齐，与迁移前的 helper 渲染逐字节一致
    （真机对比项之一）。条目为空 → 空串（helper 据此摘掉整个块）。
    """
    return "".join(f"{e['effective_ip']}\t{e['host']}\n" for e in entries)


#: 意图的契约版本。v1 = 条目集（helper 自己解析+渲染）；
#: **v2 = 完整期望态**（``content`` 已由 app 渲染好，helper 只校验 + 落盘）。
#: helper 侧 ``load_hosts_rules`` 只接受本版本，别的一律整份拒绝 → 冻结（安全态）。
WIRE_VERSION = 2

# 这里原有 ``WIRE_KEYS`` 与 ``write_rules()``：一份「落盘白名单 + 原子写
# hosts_rules.json」的唯一入口。会话通道落地后那份文件没有了，声明意图走
# ``Plugin.declare``，白名单也失去了对象（要发的就是 ``_build_payload`` 那份产物本身
# —— helper 侧只认那四个字段，多带的 ``entries`` 它不读）。


# ──────────────────────────────────────────────
# 周期型长驻服务（框架托管线程）
# ──────────────────────────────────────────────

class HostsResolveService:
    """周期型长驻服务：解析到期条目 → 重建期望态 → **有变化才声明** → 完成。

    节律用固定 :data:`PROBE_INTERVAL` 声明（数据，不是时钟）；真正的解析间隔由
    配置 ``resolve_poll`` 在 ``provider`` 内判断，故改配置无需重建服务。

    与 route_guard 的 ``RouteProbeService`` **同构**：``provider`` 纯计算（不落盘），
    本服务负责变化检测 → :meth:`publish`（＝ ``plugin.declare("hosts", …)``）。UI 动作
    复用同一个 ``poll()``，保持单一代码路径（批次 5 后段定案）。

    有意**不**在插件里起线程或用定时器：线程/时钟/起停/join 全归框架。
    """

    name = "hosts_resolve"

    def __init__(self, provider: Callable[[bool], Optional[dict]],
                 publish: Callable[[dict], "tuple[bool, Optional[str]]"] | None = None,
                 interval: float = PROBE_INTERVAL) -> None:
        self.interval = float(interval)
        self._provider = provider
        #: 声明意图的出口（注入 ``plugin.declare``）。返回 ``(是否已送达, 原因)``
        #: ——「未连接」不是错误，只是还没送到，故调用方只记日志。
        self._publish = publish
        self.report_problem = None          # 宿主托管前注入（可选）
        self.last: dict | None = None       # 上次声明出去的期望态（变化检测）
        #: 用户手动「更新」登记进来的域名（见 :meth:`request_now`）：**跨线程**读写
        #: —— 置位在 UI 线程、取走在服务线程。只用 ``set`` 的原子方法维护
        #: （``update`` / ``difference_update`` 都是一步到位的 C 级操作），因为插件
        #: 目录内不得 ``import threading``（门禁 ``test_no_private_timer_in_plugins``）。
        self._manual: set[str] = set()

    # ---------- 手动「更新」（用户按键 → 不等那一拍） ----------

    def request_now(self, hosts) -> int:
        """登记「这些域名现在就要重新解析」，返回其中**新**的条数。

        与 :data:`resolve_poll` 那条自动间隔**互不干涉**：这里的域名由 ``provider``
        并入本轮 ``due``（见 ``Plugin._build_payload``），不再看缓存年龄。

        只登记，不解析、不声明 —— 真正的活留给服务线程那一轮 ``poll()``（UI 线程禁网络）。
        调用方紧接着应 ``wake_service()`` 把服务提前一拍，否则这一批要等到下一拍。
        """
        wanted = {str(h).strip().lower() for h in hosts if str(h).strip()}
        fresh = wanted - self._manual
        self._manual.update(wanted)
        return len(fresh)

    def pending_hosts(self) -> set[str]:
        """还没被某一轮 ``poll()`` 取走的手动请求（管理窗据此显示「更新中」）。

        「取走」发生在 ``poll()`` 把候选交给 ``provider`` **之后**（成功失败都算）——
        早一刻清就会让界面在解析还没落地时报「已完成」。
        """
        return set(self._manual)

    def _forget(self, taken: set[str]) -> None:
        """划掉这一轮已经处理过的那批（用差集，不 ``clear()``：``poll`` 期间新登记的
        请求必须留下 —— 它们属于下一次唤醒）。"""
        self._manual.difference_update(taken)

    def poll(self, force: bool = False, resolve: bool = True) -> None:
        """一次收敛：``provider`` → 变化检测 → 声明。

        发起前先取走一批手动请求（``_manual`` 的快照），交给 ``provider`` 的那一轮吃进
        ``due``；**不管成败**，跑完就划掉这一批 —— 划早了界面会说「已完成」，划晚了同一批
        会被解析两遍（幂等，但白花一次网络）。``poll`` 期间新到的请求留在集合里，
        由下一次唤醒再跑。
        """
        taken = self.pending_hosts()
        try:
            new = self._provider(resolve)
        except Exception as e:              # noqa: BLE001 - 长驻服务不能因异常退出
            self._fail(f"hosts 期望态计算失败：{e}")
            return
        finally:
            self._forget(taken)
        if new is None:
            return                          # provider 已上报原因
        if not force and new == self.last:
            return
        if self._publish is None:
            self._fail("hosts 意图没有出口（未接入会话）")
            return
        try:
            ok, err = self._publish(new)
        except Exception as e:              # noqa: BLE001 - 同上
            self._fail(f"声明 hosts 意图失败：{e}")
            return
        if not ok:
            # 「没送到」不是「内容错」：内容合法只是还没发出去（守护没连上时会自动补发）。
            # 故这里**不**记成插件问题，只留一条日志 —— 与「规则被拒」分开。
            # 但这轮询是 30s 一次、没连上就会一直没连上 ⇒ 同因只留一条（级别仍是 info）。
            log_throttled(log, logging.INFO, "hosts 意图暂未送达：%s", err or "守护未连接")
        self.last = new

    def _fail(self, message: str) -> None:
        """「吞掉异常继续跑」的场合把原因变成设置页可见的插件问题（宿主按服务去重）。"""
        if callable(self.report_problem):
            self.report_problem(message)
        else:
            log.warning("%s", message)
