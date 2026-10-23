"""iPad 隧道诊断引擎（纯逻辑，无 Qt 依赖，便于单测）。

对应设置页「运行诊断」：输入测试 IP / 测试域名，依次跑 6 步，
每步产出「执行的命令 / 命令输出 / 评估结果」，供 UI 流式展示。

设计原则：
- 所有命令都是**只读**的网络查询，**不需要 root**（路由表 / DNS / curl / dig /
  ifconfig 均用户态可跑），因此诊断可在 app 进程内直接驱动，不必走 helper。
- 辅助判据来自**会话**：守护推来的那份观测（内存里现取）与当前声明的意图正文，
  与实时命令输出交叉印证（如「系统 DNS 是否指向 198.18.0.2」既要看命令输出，
  也要看配置开关，避免「运行/配置不一致」被忽略）。值由调用方传进来 ——
  本模块**不读盘**，观测与意图都是别人手里那份。
- `runner` 可注入，便于测试用假命令覆盖真实 subprocess。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from barkit.plugins.ipad_tunnel.bridge import (
    HOP_CHANNEL,
    HOP_DATA,
    HOP_DEVICE,
    HOP_SOCKS,
)

HEV_DNS = "198.18.0.2"          # 隧道内置 mapdns 地址（hev yml mapdns.address）

#: 阶梯层号 → 「该去查什么」。探针逐级回退定出的最浅失败层，面板与这里用的是**同一份
#: 结论**；诊断页多给一句「下一步动哪一头」，因为它本来就是给人照着做的。
_HOP_ADVICE = {
    HOP_DATA: "本机到 iPad 那几段逐级实测都正常（设备在、端口通、SOCKS 有应答）"
              "→ 卡点在本机这一侧（中继），表现为连接无数据传输",
    HOP_DEVICE: "逐级下探发现 USB 上没有设备 → 查连线、解锁与「信任此电脑」",
    HOP_CHANNEL: "逐级下探发现 iPad 那一跳的端口没在听 → 去 iPad 上看那个 SOCKS 服务"
                 "有没有跑起来",
    HOP_SOCKS: "逐级下探发现端口能连上、SOCKS5 却不应答 → iPad 上的引擎卡住了",
}
FAKE_RE = re.compile(r"^100\.64\.")  # fake IP 段（hev mapdns 返回）

# 状态着色用 key（UI 侧映射为具体颜色）
PASS, FAIL, WARN, INFO = "pass", "fail", "warn", "info"


@dataclass
class DiagResult:
    """单步诊断结果。"""
    step: int
    title: str
    command: str          # 执行的命令（展示用）
    output: str           # 命令原始输出（展示用）
    status: str           # PASS / FAIL / WARN / INFO
    detail: str           # 评估结论（人话）


# ──────────────────────────────────────────────
# 命令执行（可注入）
# ──────────────────────────────────────────────

def _run_default(cmd: list[str], timeout: float = 12.0) -> tuple[int, str]:
    """默认 runner：真实跑命令，返回 (returncode, 合并输出)。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"(命令超时 {timeout}s)"
    except FileNotFoundError as e:
        return 127, f"(命令不存在: {e})"


# ──────────────────────────────────────────────
# 输出解析（纯函数，单测友好）
# ──────────────────────────────────────────────

def parse_route_interface(text: str) -> str | None:
    """从 `route -n get <ip>` 输出里取 interface 行。"""
    m = re.search(r"interface:\s*(\S+)", text)
    return m.group(1) if m else None


def parse_primary_nameserver(text: str) -> str | None:
    """从 `scutil --dns` 输出里取 resolver #1 的 nameserver[0]。

    `scutil --dns` 有多个 resolver 段，应用真正用的是 resolver #1；
    若结构异常（无 resolver #1），退化取第一个出现的 nameserver。
    """
    blocks = re.split(r"resolver #", text)
    if len(blocks) >= 2:
        m = re.search(r"nameserver\[\d+\]\s*:\s*(\S+)", blocks[1])
        if m:
            return m.group(1)
    m = re.search(r"nameserver\[\d+\]\s*:\s*(\S+)", text)
    return m.group(1) if m else None


def is_fake_ip_line(text: str) -> bool:
    """输出里是否出现 100.64.0.0/10 的 fake IP（= DNS 走了隧道 mapdns）。"""
    return any(FAKE_RE.match(line.strip()) for line in text.splitlines() if line.strip())


# ──────────────────────────────────────────────
# 诊断主流程（生成器，逐步 yield，便于 UI 流式展示）
# ──────────────────────────────────────────────

def run_diagnostics(
    test_ip: str = "8.8.8.8",
    test_domain: str = "example.com",
    runner=_run_default,
    state: dict | None = None,
    tunnel: dict | None = None,
    hop: str | None = None,
):
    """依次跑 6 步诊断，逐步 yield DiagResult。

    步骤：1) iPad 端就绪  2) utun 已创建(名称)  3) 路由是否劫持
          4) 路由测试    5) DNS 是否劫持       6) DNS 测试

    ``state`` 是守护推来的那份观测（调用方从内存现取），``tunnel`` 是当前声明的
    意图正文 —— 两者都是**传进来的值**，本函数不读盘。

    ``hop`` 是探针逐级回退定出的**最浅失败层**（``HOP_*``，由插件现问后传进来）。
    第 1 步据此说清「卡在哪一段、该去动哪一头」—— 诊断页与面板用同一份结论，不许两处
    各说一套。它是运行时现取的，故不要在这里缓存。
    """
    state = state if isinstance(state, dict) else {}
    tunnel = tunnel if isinstance(tunnel, dict) else {}
    hijack_route_flag = bool(tunnel.get("hijack_route", False))
    hijack_dns_flag = bool(tunnel.get("hijack_dns", False))

    # ── 1. iPad 端是否已就绪 ──
    cmd1 = ("curl -x socks5h://127.0.0.1:1080 -o /dev/null -s "
            "-w 'http=%{http_code} t=%{time_total}' --max-time 10 https://example.com")
    rc1, out1 = runner([
        "curl", "-x", "socks5h://127.0.0.1:1080", "-o", "/dev/null", "-s",
        "-w", "http=%{http_code} t=%{time_total}", "--max-time", "10",
        "https://example.com",
    ], timeout=12)
    ipad_reachable = state.get("ipad_reachable")
    # 判据顺序：**实测优先**。旧版把「状态文件说 ipad_reachable=true」也当 PASS，于是
    # 2026-09-22 那场「隧道建起来了、却一条数据都过不去」的故障里，面板与诊断页一起瞒报
    # （状态文件说没事、实测 curl 全灭）⇒ 用户只能拿到「一切正常，但没网」。
    if rc1 == 0 and "http=200" in out1:
        s1, d1 = PASS, "iPad 端就绪（经隧道取到数据）"
    elif ipad_reachable is True:
        # 阶梯结论（若探针给得出）优先：它把「过不去」细到具体哪一段。
        advice = _HOP_ADVICE.get(hop or "", "逐级下探这一轮没给出结论（可能刚拆过隧道）")
        s1, d1 = FAIL, (f"状态说 iPad 端就绪，但经隧道取不到数据（curl rc={rc1}，"
                        f"out={out1.strip()!r}）：{advice}")
    elif ipad_reachable is False:
        s1, d1 = FAIL, "iPad 端未就绪（USB 未连，或 iPad 端隧道引擎未启动）"
    else:
        s1, d1 = WARN, f"无法确认（curl rc={rc1}，输出={out1.strip()!r}）"
    yield DiagResult(1, "iPad 端是否已就绪", cmd1, out1.strip(), s1, d1)

    # ── 2. utun 是否已创建（名称）──
    utun = state.get("utun")
    if utun:
        cmd2 = f"ifconfig {utun}"
        rc2, out2 = runner(["ifconfig", utun], timeout=8)
        exists = rc2 == 0 and utun in out2
        s2 = PASS if exists else WARN
        d2 = (f"utun 已创建，名称 = {utun}" +
              ("" if exists else "（状态记录存在但 ifconfig 未查到，可能正在拆除）"))
        yield DiagResult(2, "utun 是否已创建（名称）", cmd2, out2.strip() or "(无输出)", s2, d2)
    else:
        cmd2 = "ifconfig -l | tr ' ' '\\n' | grep utun"
        rc2, out2 = runner(
            ["sh", "-c", "ifconfig -l | tr ' ' '\\n' | grep utun"], timeout=8)
        found = out2.strip()
        s2 = FAIL if not found else INFO
        d2 = ("未创建 utun（隧道未激活）" if not found
              else f"观测里没记到 utun，但系统存在：{found}")
        yield DiagResult(2, "utun 是否已创建（名称）", cmd2, found or "(无 utun)", s2, d2)

    # ── 3. 当前路由是否已劫持 ──
    cmd3 = f"route -n get {test_ip}"
    rc3, out3 = runner(["route", "-n", "get", test_ip], timeout=8)
    iface3 = parse_route_interface(out3)
    hijacked3 = bool(iface3 and iface3.startswith("utun"))
    if hijacked3 and hijack_route_flag:
        s3, d3 = PASS, f"路由已劫持：{test_ip} → {iface3}（与「路由劫持」设置一致：已开启）"
    elif hijacked3 and not hijack_route_flag:
        s3, d3 = WARN, (f"路由经 {iface3}，但路由劫持设置未开启"
                        "（运行/配置不一致，helper 可能尚未重跑）")
    elif not hijacked3 and hijack_route_flag:
        s3, d3 = WARN, (f"路由未劫持（{test_ip} → {iface3 or '?'}），但路由劫持设置已开启"
                        "（helper 未应用 / 隧道未激活）")
    else:
        s3, d3 = INFO, f"路由未劫持（{test_ip} → {iface3 or '?'}），与设置一致（旁路模式）"
    yield DiagResult(3, "当前路由是否已劫持", cmd3, out3.strip(), s3, d3)

    # ── 4. 路由测试情况（功能验证：真实流量走哪条出口）──
    cmd4a = f"route -n get {test_ip}"
    _, out4a = runner(["route", "-n", "get", test_ip], timeout=8)
    iface4 = parse_route_interface(out4a)
    cmd4b = "curl -4 --max-time 10 https://ifconfig.me"
    rc4b, out4b = runner(["curl", "-4", "--max-time", "10", "https://ifconfig.me"], timeout=12)
    egress = out4b.strip()
    if rc4b == 0 and egress:
        if iface4 and iface4.startswith("utun"):
            s4, d4 = PASS, f"路由测试通过：{test_ip} → {iface4}，出口公网 IP={egress}（流量经 iPad）"
        else:
            s4, d4 = INFO, (f"路由测试：{test_ip} → {iface4 or '?'}（未劫持），"
                            f"出口公网 IP={egress}（流量走本机直连）")
    else:
        s4, d4 = FAIL, f"路由测试失败：出口探测 curl rc={rc4b}，out={out4b.strip()!r}"
    yield DiagResult(4, "路由测试情况", cmd4a + "\n" + cmd4b,
                     (out4a.strip() + "\n---\n" + (egress or "(无输出)")), s4, d4)

    # ── 5. 当前 DNS 是否已劫持 ──
    cmd5 = "scutil --dns"
    rc5, out5 = runner(["scutil", "--dns"], timeout=8)
    ns5 = parse_primary_nameserver(out5)
    hijacked5 = (ns5 == HEV_DNS)
    if hijacked5 and hijack_dns_flag:
        s5, d5 = PASS, f"DNS 已劫持：resolver #1 = {ns5}（与「DNS 劫持」设置一致：已开启）"
    elif hijacked5 and not hijack_dns_flag:
        s5, d5 = WARN, f"系统 DNS = {ns5}，但 DNS 劫持设置未开启（运行/配置不一致）"
    elif not hijacked5 and hijack_dns_flag:
        # 观测里说「读不出服务名」就把原因点出来：这一格否则只会说「helper 未应用」，
        # 用户于是去查网络，而该查的是「为什么读不出当前网络服务名」。
        why = ("（观测：读不出当前网络服务名，helper 未改动系统 DNS）"
               if state.get("service_unknown") else "（helper 未应用）")
        s5, d5 = WARN, (f"系统 DNS = {ns5 or '?'}（非 {HEV_DNS}），但 DNS 劫持设置已开启{why}")
    else:
        s5, d5 = INFO, f"DNS 未劫持：resolver #1 = {ns5 or '?'}（旁路模式，走本机直连 DNS）"
    yield DiagResult(5, "当前 DNS 是否已劫持", cmd5,
                     (out5.strip()[:1500] or "(无输出)"), s5, d5)

    # ── 6. 当前 DNS 测试情况 ──
    cmd6a = f"dig +short {test_domain}"
    rc6, out6 = runner(["dig", "+short", test_domain], timeout=12)
    through_tunnel = is_fake_ip_line(out6)
    cmd6b = f"dig @{HEV_DNS} {test_domain}"
    rc6b, out6b = runner(["dig", f"@{HEV_DNS}", test_domain], timeout=12)
    tunnel_dns_ok = is_fake_ip_line(out6b)
    if through_tunnel:
        first = out6.strip().splitlines()[0] if out6.strip() else "?"
        s6, d6 = PASS, f"DNS 测试通过：{test_domain} 经隧道解析 → {first}（隧道内网地址，走 iPad）"
    elif tunnel_dns_ok:
        sys_ip = out6.strip().splitlines()[0] if out6.strip() else "?"
        s6, d6 = WARN, (f"系统 DNS 未走隧道（{test_domain} → 真实 IP {sys_ip}），"
                        f"但隧道侧 DNS 可达（dig @{HEV_DNS} 返回隧道内网地址）—— 系统 resolver 未指向 {HEV_DNS}")
    elif out6.strip():
        first = out6.strip().splitlines()[0]
        s6, d6 = INFO, f"DNS 走本机直连：{test_domain} → {first}（未劫持，符合预期）"
    else:
        s6, d6 = FAIL, f"DNS 解析失败（系统 resolver 无应答）；隧道侧探测 rc={rc6b}，out={out6b.strip()!r}"
    yield DiagResult(6, "当前 DNS 测试情况", cmd6a + "\n" + cmd6b,
                     ((out6.strip() or "(无输出)") + "\n---\n" + (out6b.strip() or "(无输出)")),
                     s6, d6)
