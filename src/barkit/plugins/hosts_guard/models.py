"""hosts_guard 数据模型：分类、解析方式、条目 schema 与校验。

安全要点（本插件以 root 改写 /etc/hosts，校验是唯一的防线）：
  - 域名/IP 一律严格校验后才落盘，**绝不允许空白与换行**进入条目。
    否则形如 ``example.com\\n1.2.3.4 apple.com`` 的输入可劫持任意域名。
  - 解析结果落在 mapdns 的 fake IP 段（100.64.0.0/10）时拒绝写入，
    否则「查到假 IP → 写进 hosts → 从此只查到本地记录」形成不可自愈的死循环。
"""

from __future__ import annotations

import ipaddress
import re

# ── 与 helper `hostsops` 同步的常量（漂移由 test_hosts_block_marker_single_source 守护）──
BLOCK_BEGIN = "# >>> BarKit hosts-guard"
BLOCK_END = "# <<< BarKit hosts-guard"

#: 块标记的**名字**（不含 ``# >>>`` 装饰）。§7.4.4：marker 是封闭取值的名字，
#: 不得当路径替身——helper 只接受这一个值，期望态里带别的即整份拒绝。
#: 块正文由 app 渲染（§7.4.3），起始/结束行由 helper 用自身常量补上。
BLOCK_MARKER = "BarKit hosts-guard"

# fake IP 段：hev 的 mapdns 默认 network（CGNAT）。命中即判定为「疑似 fake IP」。
FAKE_IP_NETWORKS = ("100.64.0.0/10",)

# ── 节律 ────────────────────────────────────────────────────────────
#: 「自动更新（DNS 重新解析）间隔」的默认值与下限（秒）。**唯一真源在这**：
#: 配置侧（``plugins.hosts_guard.resolve_poll``，见 ``plugin.Plugin.resolve_poll``）与
#: 线格式侧（期望态里的 ``poll`` 字段，本模块 :func:`normalize_rules`）读同一份，
#: 免得一个 300 一个 60 各说各的。它**不是**框架的轮询节拍——那是 ``suggested_poll``。
DEFAULT_RESOLVE_POLL = 300
MIN_RESOLVE_POLL = 60

# ── 分类 ────────────────────────────────────────────────────────────
CAT_DIRECT = "direct"      # 直连：DDNS 端点等，通常开自动更新
CAT_ADBLOCK = "adblock"    # 广告/追踪屏蔽：批量导入，IP 多为 0.0.0.0
CAT_CUSTOM = "custom"      # 自定义

PRESET_CATEGORIES: list[tuple[str, str]] = [
    (CAT_DIRECT, "直连"),
    (CAT_ADBLOCK, "广告屏蔽"),
    (CAT_CUSTOM, "自定义"),
]

# ── 解析方式 ─────────────────────────────────────────────────────────
METHOD_AUTO = "auto"        # 按优先级依次降级
METHOD_DOH_PUB = "doh-pub"  # DoH · doh.pub（TCP 443）
METHOD_DOH_ALI = "doh-ali"  # DoH · dns.alidns.com（TCP 443）
METHOD_UDP_ALI = "udp-ali"  # UDP 直查 · 223.5.5.5
METHOD_UDP_TX = "udp-tx"    # UDP 直查 · 119.29.29.29
METHOD_SYSTEM = "system"    # 系统 DNS（隧道开启时会拿到 fake IP，靠校验兜底）

METHODS: list[tuple[str, str]] = [
    (METHOD_AUTO, "自动（按优先级降级）"),
    (METHOD_DOH_PUB, "DoH · doh.pub"),
    (METHOD_DOH_ALI, "DoH · 阿里"),
    (METHOD_UDP_ALI, "UDP 直查 · 阿里 223.5.5.5"),
    (METHOD_UDP_TX, "UDP 直查 · 腾讯 119.29.29.29"),
    (METHOD_SYSTEM, "系统 DNS（隧道开启时可能失效）"),
]

METHOD_LABELS = dict(METHODS)
CATEGORY_LABELS = dict(PRESET_CATEGORIES)

DEFAULT_METHOD = METHOD_AUTO
DEFAULT_CATEGORY = CAT_DIRECT

# ── 校验 ────────────────────────────────────────────────────────────

# 域名：仅字母数字与点、连字符；首末必须是字母数字（挡住 "_"、"/"、空白与换行）
_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
MAX_HOST_LEN = 253


def valid_host(text: str) -> bool:
    """域名是否合法（严格字符集，杜绝注入）。"""
    if not isinstance(text, str) or not text or len(text) > MAX_HOST_LEN:
        return False
    return bool(_HOST_RE.match(text))


def valid_ip(text: str) -> bool:
    """IPv4/IPv6 地址是否合法。"""
    if not isinstance(text, str) or not text:
        return False
    try:
        ipaddress.ip_address(text.strip())
    except ValueError:
        return False
    return True


def is_fake_ip(text: str) -> bool:
    """是否落在 mapdns 的 fake IP 段（写进 hosts 会形成死循环）。"""
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return False
    return any(addr in ipaddress.ip_network(net) for net in FAKE_IP_NETWORKS)


def normalize_host(text: str) -> str:
    """清洗域名：去空白、去尾部点、转小写。调用方仍须先用 valid_host 判定。"""
    return text.strip().rstrip(".").lower()


# ── 条目 schema ──────────────────────────────────────────────────────

_ENTRY_FIELDS = ("host", "ip", "category", "auto", "method", "note")


def make_entry(
    host: str,
    ip: str,
    category: str = DEFAULT_CATEGORY,
    auto: bool = False,
    method: str = DEFAULT_METHOD,
    note: str = "",
) -> dict:
    """构造一条条目（已清洗，未校验；落盘前须经 validate_entry）。"""
    return {
        "host": normalize_host(host),
        "ip": str(ip).strip(),
        "category": category if category else DEFAULT_CATEGORY,
        "auto": bool(auto),
        "method": method if method in METHOD_LABELS else DEFAULT_METHOD,
        "note": (note or "").strip(),
    }


def validate_entry(entry) -> str | None:
    """校验单条条目，返回错误描述；合法返回 None。"""
    if not isinstance(entry, dict):
        return "条目不是对象"
    missing = [k for k in ("host", "ip") if not isinstance(entry.get(k), str) or not entry[k].strip()]
    if missing:
        return f"缺少字段：{'、'.join(missing)}"
    if not valid_host(entry["host"].strip()):
        return f"域名不合法：{entry['host']!r}"
    if not valid_ip(entry["ip"]):
        return f"IP 不合法：{entry['ip']!r}"
    if entry.get("method", DEFAULT_METHOD) not in METHOD_LABELS:
        return f"解析方式未知：{entry.get('method')!r}"
    if not isinstance(entry.get("note", ""), str):
        return "备注必须是字符串"
    if not isinstance(entry.get("auto", False), bool):
        return "auto 必须是布尔值"
    return None


def normalize_rules(raw) -> tuple[dict | None, str | None]:
    """校验并规整 hosts 期望态（app 侧那份内部结构）。返回 (data, error)。

    与 helper 侧同名函数语义一致：任何一条非法即整份拒绝，宁可不动 hosts
    也不写一半。域名去重（后者覆盖前者），分类保留未知值（用户可自定义）。
    """
    if not isinstance(raw, dict):
        return None, "hosts 规则不是对象"
    entries_raw = raw.get("entries", [])
    if not isinstance(entries_raw, list):
        return None, "entries 不是数组"

    entries: list[dict] = []
    seen: set[str] = set()
    for i, item in enumerate(entries_raw):
        err = validate_entry(item)
        if err is not None:
            return None, f"第 {i + 1} 条：{err}"
        e = make_entry(
            host=item["host"],
            ip=item["ip"],
            category=item.get("category") or DEFAULT_CATEGORY,
            auto=bool(item.get("auto", False)),
            method=item.get("method") or DEFAULT_METHOD,
            note=item.get("note") or "",
        )
        if e["host"] in seen:
            return None, f"第 {i + 1} 条：域名重复 {e['host']}"
        seen.add(e["host"])
        entries.append(e)

    categories_raw = raw.get("categories", [])
    categories: list[str] = []
    if isinstance(categories_raw, list):
        for c in categories_raw:
            if isinstance(c, str) and c and c not in categories:
                categories.append(c)
    for key, _label in PRESET_CATEGORIES:
        if key not in categories:
            categories.append(key)

    poll = raw.get("poll", DEFAULT_RESOLVE_POLL)
    if not isinstance(poll, int) or isinstance(poll, bool) or poll < MIN_RESOLVE_POLL:
        poll = DEFAULT_RESOLVE_POLL

    return {
        "version": 1,
        "enabled": bool(raw.get("enabled", True)),
        "poll": poll,
        "categories": categories,
        "entries": entries,
    }, None
