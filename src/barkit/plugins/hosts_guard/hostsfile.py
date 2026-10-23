"""hosts_guard/hostsfile：/etc/hosts 文本解析（只读，零特权）。

/etc/hosts 是 0644，app 侧可直接读，无需 helper —— 面板与管理窗口据此实时
展示；写入才需要 root helper。

解析保留行结构（空行 / 注释 / 条目），并识别 BarKit 标记块，据此把条目分成
「托管」「外部」两类。一行可挂多个域名（``127.0.0.1 a.com b.com``），故对外
一律按 ``(ip, host)`` 记录展开。

不提供渲染（写回）——渲染只发生在 root helper 内，见 route_guard/helper.py。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from barkit.plugins.hosts_guard.models import BLOCK_BEGIN, BLOCK_END, valid_host, valid_ip

HOSTS_PATH = Path("/etc/hosts")

# 系统保留域名：接管会破坏回环解析，一律不可接管
SYSTEM_HOSTS = frozenset({"localhost", "broadcasthost"})

KIND_BLANK = "blank"
KIND_COMMENT = "comment"
KIND_ENTRY = "entry"
KIND_BLOCK_BEGIN = "block_begin"
KIND_BLOCK_END = "block_end"

_SPLIT_RE = re.compile(r"\s+")


@dataclass
class Line:
    """hosts 的一行，保留原始文本以便无损写回。"""

    kind: str
    raw: str
    no: int = 0
    ip: str = ""
    hosts: list[str] = field(default_factory=list)


@dataclass
class Record:
    """一条 (ip, host) 记录及其来源。"""

    ip: str
    host: str
    line_no: int
    managed: bool = False       # 在 BarKit 标记块内
    system: bool = False        # 系统保留域名，不可接管


def parse(text: str) -> list[Line]:
    """把 hosts 全文解析成行列表（不丢任何字符，raw 原样保留）。"""
    lines: list[Line] = []
    for i, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped:
            lines.append(Line(kind=KIND_BLANK, raw=raw, no=i))
            continue
        if stripped.startswith("#"):
            if stripped == BLOCK_BEGIN:
                kind = KIND_BLOCK_BEGIN
            elif stripped == BLOCK_END:
                kind = KIND_BLOCK_END
            else:
                kind = KIND_COMMENT
            lines.append(Line(kind=kind, raw=raw, no=i))
            continue

        # 行内注释：'127.0.0.1 a.com # 备注' → 取 '#' 之前
        body = raw.split("#", 1)[0].strip()
        parts = [p for p in _SPLIT_RE.split(body) if p]
        if len(parts) < 2 or not valid_ip(parts[0]):
            # 形态异常（无 IP 或无域名）：原样保留，不参与管理
            lines.append(Line(kind=KIND_COMMENT, raw=raw, no=i))
            continue
        lines.append(Line(kind=KIND_ENTRY, raw=raw, no=i, ip=parts[0], hosts=parts[1:]))
    return lines


def block_range(lines: list[Line]) -> tuple[int, int] | None:
    """标记块的 [起, 止) 行下标；未出现或不成对返回 None。"""
    begin = end = None
    for idx, ln in enumerate(lines):
        if ln.kind == KIND_BLOCK_BEGIN and begin is None:
            begin = idx
        elif ln.kind == KIND_BLOCK_END and begin is not None:
            end = idx
            break
    if begin is None or end is None:
        return None
    return begin, end + 1


def records(lines: list[Line]) -> list[Record]:
    """展开全部 (ip, host) 记录，标注是否托管 / 是否系统保留。"""
    rng = block_range(lines)
    out: list[Record] = []
    for idx, ln in enumerate(lines):
        if ln.kind != KIND_ENTRY:
            continue
        managed = rng is not None and rng[0] <= idx < rng[1]
        for host in ln.hosts:
            out.append(Record(
                ip=ln.ip,
                host=host.lower(),
                line_no=ln.no,
                managed=managed,
                system=host.lower() in SYSTEM_HOSTS,
            ))
    return out


def read_records(path: Path = HOSTS_PATH) -> tuple[list[Record], str | None]:
    """读取并解析 hosts 文件。返回 (记录列表, 错误)；错误时记录为空。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return [], f"读取 {path} 失败：{e}"
    return records(parse(text)), None


def parse_import_text(text: str) -> tuple[list[tuple[str, str]], int]:
    """解析待导入的 hosts 文本（批量粘贴用）。

    返回 ([(ip, host), ...], 跳过行数)。跳过：空行、注释行、形态异常行、
    系统保留域名、非法域名/IP。调用方负责去重与冲突提示。
    """
    out: list[tuple[str, str]] = []
    skipped = 0
    for raw in text.splitlines():
        body = raw.split("#", 1)[0].strip()
        if not body:
            if raw.strip() and not raw.strip().startswith("#"):
                skipped += 1
            continue
        parts = [p for p in _SPLIT_RE.split(body) if p]
        if len(parts) < 2 or not valid_ip(parts[0]):
            skipped += 1
            continue
        ip = parts[0]
        for host in parts[1:]:
            host = host.strip().rstrip(".").lower()
            if not valid_host(host) or host in SYSTEM_HOSTS:
                skipped += 1
                continue
            out.append((ip, host))
    return out, skipped
