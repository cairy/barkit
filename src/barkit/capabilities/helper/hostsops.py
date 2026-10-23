"""hostsops：/etc/hosts 托管块机制（校验 + 渲染落盘）。

§7.4.3 职责边界：

- **留在 helper（root 侧）**：写 /etc/hosts 是 root 专属的系统状态变更；且
  「块外已被接管的同名行必须一并剔除」只能由能读改该文件的进程执行。
- **归 app 侧（插件）**：解析链（DoH / UDP 直查 / 系统 DNS + 节流 + fake IP 判定）
  与**块内容渲染**（``plugins/hosts_guard/resolve.py``）。两者都是 policy，无需 root。

故入参是一份**完整意图**（hosts 那份意图正文，契约 v2）::

    {"version": 2, "enabled": true, "marker": "BarKit hosts-guard",
     "content": "1.2.3.4\\texample.com\\n"}

``content`` 是 app 渲染好的块**正文**（每行 ``<ip>\\t<host>``）；起止标记行由本模块
用自身常量补上，避免两侧各写一份装饰文本。helper 只做三件事：

1. **校验**（契约版本 / marker 属封闭取值 / 每行 IP 合法且不落 fake 段 / 域名合法
   且不重复）——这是 root 侧最后防线，不是 policy，**不回迁**；
2. 按 content **渲染标记块** + 剔除块外被接管的同名行；
3. ``O_NOFOLLOW`` 落盘 + 写状态。

故本模块不认识任何 DoH 端点或 UDP 服务器，也不持有解析缓存与条目模型。
"""
from __future__ import annotations

import ipaddress
import json
import re
import time

from .safeio import (
    HOSTS_BACKUP,
    HOSTS_PATH,
    _atomic_write_text,
    _open_nofollow,
    log,
)
from .state import write_hosts_state

# ──────────────────────────────────────────────
# hosts_guard：/etc/hosts 托管块
#
# 安全要点（本进程以 root 改写系统文件，务必遵守）：
#   1) 只重写标记块与被接管的行，块外内容（第三方条目、注释）原样保留；
#   2) 期望态里的 IP 若落在 mapdns 的 fake IP 段（100.64.0.0/10）一律拒绝写入——
#      写进去就成了「查到假 IP → 从此只查本地记录」的死循环，且不可自愈；
#   3) 期望态任一处非法（版本 / marker / 任一行）即**整份拒绝**，保持 hosts 不动；
#   4) 落盘走 safeio._open_nofollow（O_NOFOLLOW），遵守 safeio 顶部的安全模型；
#   5) 首次改动前留一份快照到 SYS_RUN_DIR（root 私有，非 root 可写目录）。
# ──────────────────────────────────────────────

HOSTS_BLOCK_BEGIN = "# >>> BarKit hosts-guard"
HOSTS_BLOCK_END = "# <<< BarKit hosts-guard"

#: 期望态 ``marker`` 的**封闭取值**（§7.4.4：marker 是名字，不得当路径/目标选择器
#: 用）。与本模块的块标记一一对应，别的取值整份拒绝。app 侧的同一常量见
#: ``plugins/hosts_guard/models.BLOCK_MARKER``，漂移由
#: ``test_hosts_block_marker_single_source`` 守护。
HOSTS_BLOCK_MARKER = "BarKit hosts-guard"

HOSTS_FAKE_NETS = ("100.64.0.0/10",)   # hev mapdns 的 CGNAT 段
HOSTS_CONTENT_MAX = 1 << 20            # 块正文长度上限（1 MiB）
HOSTS_WIRE_VERSION = 2                 # 期望态契约版本（v1「条目集」已废弃）


def _hosts_valid_host(text) -> bool:
    """域名严格校验：仅字母数字与点、连字符，首末为字母数字。

    挡住空白与换行，杜绝 ``example.com\\n1.2.3.4 apple.com`` 这类注入。
    """
    if not isinstance(text, str) or not text or len(text) > 253:
        return False
    return bool(re.match(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$", text))


def _hosts_valid_ip(text) -> bool:
    if not isinstance(text, str) or not text:
        return False
    try:
        ipaddress.ip_address(text.strip())
    except ValueError:
        return False
    return True


def _hosts_is_fake(ip: str) -> bool:
    """是否落在 mapdns 的 fake IP 段——命中即拒绝写入。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in ipaddress.ip_network(n) for n in HOSTS_FAKE_NETS)


def _parse_block_content(content: str):
    """把块正文解析成 ``[(ip, host), …]``；任一非法行即返回 ``(None, 原因)``。

    严格格式：每行 ``<ip> <host>[ <host>…]``（空白分隔，允许制表符），空行与
    ``#`` 注释行跳过。任一行 IP 不合法 / 落 fake 段、域名不合法 / 重复 → 整份拒绝。

    解析在 root 侧再做一次，是「app 说写什么就写什么」与「root 只写安全的东西」
    之间的最后一道闸门：即便 app 侧校验被绕过，注入与 fake IP 仍进不了 /etc/hosts。
    """
    pairs: list = []
    seen: set = set()
    for lineno, raw in enumerate(content.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            return None, f"content 第 {lineno} 行不是「IP 主机名」：{raw!r}"
        ip = parts[0]
        if not _hosts_valid_ip(ip):
            return None, f"content 第 {lineno} 行 IP 不合法：{ip!r}"
        if _hosts_is_fake(ip):
            return None, f"content 第 {lineno} 行 IP 落在 fake 段（死循环源）：{ip}"
        for host in parts[1:]:
            if not _hosts_valid_host(host):
                return None, f"content 第 {lineno} 行域名不合法：{host!r}"
            host = host.strip().rstrip(".").lower()
            if host in seen:
                return None, f"content 第 {lineno} 行域名重复：{host}"
            seen.add(host)
            pairs.append((ip, host))
    return pairs, None


def load_hosts_rules(text: str | None = None):
    """校验 hosts 的期望态（v2），返回 ``(intent, error)``。

    ``intent = {"enabled": bool, "pairs": [(ip, host), …]}``。任一处非法即整份拒绝，
    保持 /etc/hosts 不动。

    ``text`` 是 ``tasks.Runner`` 从**内存里的意图**取来递进来的（2026-09-29 起）。
    从前它读用户域 ``hosts_rules.json``；那个分支连同文件一起消失 —— 调度器只在
    「这个 subject 有意图」时才叫这里，故 ``text`` 不会是「文件不存在」那种 None。
    """
    if text is None:
        return None, None
    try:
        raw = json.loads(text)
    except ValueError as e:
        return None, f"hosts 期望态解析失败：{e}"
    if not isinstance(raw, dict):
        return None, "hosts 期望态不是对象"
    # 契约版本：v1 是「条目集」（helper 自己解析+渲染），语义完全不同，不能猜
    if raw.get("version") != HOSTS_WIRE_VERSION:
        return None, f"期望态版本不受支持（需 {HOSTS_WIRE_VERSION}）：{raw.get('version')!r}"
    if raw.get("marker") != HOSTS_BLOCK_MARKER:
        return None, f"marker 不在封闭取值内：{raw.get('marker')!r}"
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        return None, f"enabled 需为布尔：{enabled!r}"
    content = raw.get("content", "")
    if not isinstance(content, str):
        return None, "content 需为字符串"
    if len(content) > HOSTS_CONTENT_MAX:
        return None, f"content 超过上限 {HOSTS_CONTENT_MAX} 字节"
    if not enabled:
        # 禁用 = 没有期望 → 空块。忽略 content：杜绝「禁用却残留内容」的旧文件
        return {"enabled": False, "pairs": []}, None
    pairs, err = _parse_block_content(content)
    if err is not None:
        return None, err
    return {"enabled": True, "pairs": pairs}, None


def _hosts_backup_once() -> None:
    """首次改动前留一份 /etc/hosts 快照（root 私有目录，只保留最早那份）。"""
    if HOSTS_BACKUP.exists():
        return
    try:
        text = HOSTS_PATH.read_text(encoding="utf-8", errors="replace")
        with _open_nofollow(HOSTS_BACKUP, mode=0o600) as f:
            f.write(text)
        log(f"已备份 /etc/hosts → {HOSTS_BACKUP}")
    except OSError as e:
        log(f"备份 /etc/hosts 失败（不影响本次改动）：{e}")


def _apply_hosts(block_lines: list, managed: set):
    """渲染标记块并写回 /etc/hosts。返回 ``(ok, error, changed)``。

    只改动两处：① 标记块整体替换（块内完全由 BarKit 控制，外部手改会被纠正）；
    ② 块外已被接管的同名域名从原行剔除（整行剔完则删整行）。
    其余内容（第三方条目、注释、空行）原样保留。文本无变化则不落盘。
    """
    try:
        old_text = HOSTS_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return False, f"读取 /etc/hosts 失败：{e}", False

    kept: list = []
    for ln in old_text.splitlines():
        stripped = ln.strip()
        if not stripped or stripped.startswith("#"):
            kept.append(ln)
            continue
        # 行内注释整段保留：剥离 ``#`` 之前的部分做判定，命中后把注释拼回去，
        # 否则「``1.2.3.4 a.com  # 家里 NAS``」一旦 ``a.com`` 被接管，改写后注释丢掉
        # （2026-10 审查：原实现重建整行时把注释段一并丢掉了）。
        hash_i = ln.find("#")
        if hash_i != -1:
            comment = " " + ln[hash_i:]
            before = ln[:hash_i].strip()
        else:
            comment = ""
            before = ln.strip()
        parts = before.split()
        if len(parts) < 2 or not _hosts_valid_ip(parts[0]):
            kept.append(ln)
            continue
        ip = parts[0]
        hosts = [h for h in parts[1:] if h.strip().rstrip(".").lower() not in managed]
        if not hosts:
            continue                                    # 该行域名全被接管 → 删整行
        if len(hosts) == len(parts) - 1:
            kept.append(ln)                             # 一个都没动，原样（含注释）
        else:
            kept.append(ip + "\t" + "\t".join(hosts) + comment)

    # 移除旧标记块，插入位取原处（无块则文件末尾）
    begin = end = None
    for i, ln in enumerate(kept):
        s = ln.strip()
        if begin is None and s == HOSTS_BLOCK_BEGIN:
            begin = i
        elif begin is not None and s == HOSTS_BLOCK_END:
            end = i
            break
    if begin is not None and end is not None:
        del kept[begin:end + 1]
        insert_at = begin
    else:
        insert_at = len(kept)

    if block_lines:
        block = [HOSTS_BLOCK_BEGIN, "# 以下条目由 BarKit 自动维护，手动修改会被覆盖"]
        block.extend(block_lines)
        block.append(HOSTS_BLOCK_END)
        kept[insert_at:insert_at] = block

    new_text = "\n".join(kept)
    if new_text and not new_text.endswith("\n"):
        new_text += "\n"
    if new_text == old_text:
        return True, None, False

    _hosts_backup_once()
    try:
        # 原子写：断电/被杀也不会留下半截 /etc/hosts（域名解析瘫痪）。
        _atomic_write_text(HOSTS_PATH, new_text, mode=0o644)
    except OSError as e:
        return False, f"写入 /etc/hosts 失败：{e}", False
    return True, None, True


def run_hosts_cycle(text: str | None = None) -> dict:
    """一轮 hosts 托管：按 app 算好的**期望态**渲染标记块 → 落盘 → 写状态。

    档 B（S3b-2）：本函数**不解析任何域名、也不决定写什么**——DoH / UDP 直查 /
    系统 DNS、节流与块内容渲染都已迁回 app 侧（``plugins/hosts_guard/resolve.py``）。
    它只做安全校验 + 渲染落盘。

    未配置（没有 hosts 意图）时不动 hosts，只写一份空状态，避免
    app 尚未启动就把用户 hosts 里的块清掉。
    """
    intent, err = load_hosts_rules(text)
    state = {
        "ok": err is None,
        "configured": intent is not None,
        "checked_at": int(time.time()),
        "error": err,
        "count": 0,
        "hosts": [],
        "changed": False,
    }
    if intent is None:
        if err:
            log(f"hosts 期望态被拒绝，保持 /etc/hosts 不动：{err}")
        write_hosts_state(state)
        return state

    pairs = intent["pairs"]
    block_lines = [f"{ip}\t{host}" for ip, host in pairs]
    state["count"] = len(pairs)
    state["hosts"] = [{"host": host, "ip": ip} for ip, host in pairs]

    ok, werr, changed = _apply_hosts(block_lines, {h for _ip, h in pairs})
    state["changed"] = changed
    if werr:
        state["ok"] = False
        state["error"] = werr
        log(f"hosts 落盘失败：{werr}")
    elif changed:
        log(f"hosts 标记块已更新（{len(pairs)} 条）")

    write_hosts_state(state)
    return state
