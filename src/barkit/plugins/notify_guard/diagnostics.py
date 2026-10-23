"""notify_guard/diagnostics：运维诊断的**排版层**（拨测文案 ＋ 诊断报告）——纯逻辑。

分工与《iPad 隧道》的同名模块一致（``ipad_tunnel/diagnostics.py`` 段头）：**探测**由
调用方（``plugin.probe()``，它才知道怎么读三级线索与系统实际值）做，本模块只把
**已经读到的值**排成给用户看的字。于是它零 Qt、零 IO、零副作用，可直接单测。

**为什么探测与排版要分开**：拨测与「复制诊断」是同一批事实的两个出口 —— 一个上屏、
一个进剪贴板。排版只写一遍，两个出口就不可能各说各话。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Probe", "RuleLine", "format_probe", "diagnostic_text"]


@dataclass(frozen=True)
class RuleLine:
    """一条静音规则的用户可见形状（``processes`` 是 **pattern**，不是展开后的进程）。"""

    name: str
    reason: str
    processes: tuple[str, ...]


@dataclass(frozen=True)
class Probe:
    """一次**只读**拨测的全部事实（每个字段的取值域见注释）。

    只读是这份结构的语义本体：拨测回答「现在是什么情况」，不写系统、不动决策状态 ——
    「点一下诊断，通知被改了」是事故，不是功能。
    """

    app: str                        # OpenDisplay 应用态：running / idle / absent
    display: str                    # display 状态机取值：extended / running / idle / absent
    notify: str                     # notify 状态机取值：allowed / muted / manual / desync / frozen
    reason: str                     # 该取值自带的原因（``_notify_state`` 的第二项；无则空串）
    automation: bool                # 自动化总闸
    rules: tuple[RuleLine, ...]     # 全部规则（含未命中的）
    hits: tuple[str, ...]           # 命中规则的**原因文案**（空串已被过滤）
    system: bool | None             # 系统实际 dndMirrored；None = 读不出
    debounce: int                   # 防抖轮数
    open_on_startup: bool           # 启动时自动拉起 OpenDisplay

    # **没有 ``poll``**（框架节拍）：它住在 ``plugins.<id>.poll``，D27 规定那个键只服务
    # ``core/scheduler.py`` 一个读者。拨测报告因此只报「N 轮」，不把它换算成秒 ——
    # 报一个自己读不到的值就是第二真源（用户在本组件页尾改了间隔，报告还写着旧数）。


# 装置 / 第二屏 / 系统三处的词条。它们与设置页顶部状态条那份**不是同一套语气**：
# 状态条要一行说完（简称），拨测要回答「为什么」（把原因写进括号）。两份都是**排版**，
# 不是两份事实 —— 事实只来自 `Probe` 的字段。
_APP_WORDS = {"running": "运行中", "idle": "已安装 · 未运行", "absent": "未安装"}
_DISPLAY_WORDS = {
    "extended": "已连接",
    "running": "未连接（OpenDisplay 在跑，未侦测到虚拟屏）",
    "idle": "未连接（OpenDisplay 未运行）",
    "absent": "未连接（未安装 OpenDisplay）",
    "unknown": "尚未读取",
}


def _rule_line(p: Probe) -> str:
    total = len(p.rules)
    if not total:
        # 「没有规则」不是异常，而是一条**结论**：allow = S1 AND NOT S2，S2 恒假
        # ⇒ 只要第二屏在线就允许通知。把它写出来，省得用户以为规则没生效。
        return "规则：0 条（没有规则 → 第二屏在线即允许通知）"
    if not p.hits:
        return f"规则：{total} 条 · 命中 0 条"
    return f"规则：{total} 条 · 命中 {len(p.hits)} 条（{'、'.join(p.hits)}）"


def _decision_line(p: Probe) -> str:
    """决策行：状态机**当前的结论**，静音时附上它自己记的原因。

    两条口径，别把两者混起来读（它们是不同时刻的两个事实）：

    - 本行的原因取 ``Probe.reason``（**状态机**记的，与 ``notify`` 取值同源、同时刻）；
    - 「规则」那行报的是**当场重读**的命中 —— 刚命中还没过防抖时两行本就可以不一致，
      那正是「还没轮到写系统」的样子，不是矛盾。

    也不在这儿重算 ``allow = S1 AND NOT S2``：再算一遍就是第二条真源，与 decider
    迟早漂移，还会在防抖窗口里报出假的不一致。
    """
    if p.notify == "desync":
        return "决策：状态不同步 —— 系统值与守卫期望不符"
    if not p.automation:
        return "自动化：已停用 —— 通知由手动开关控制，本轮不做一致性比对"
    if p.notify == "manual":
        return "决策：手动覆盖中 —— 自动接管已挂起（系统值由上次手动操作决定）"
    if p.notify == "allowed":
        return "决策：允许通知"
    if p.reason:
        return f"决策：静音 —— {p.reason}"
    return "决策：静音"


def _system_line(p: Probe) -> str:
    """系统行：实际值 ＋（只有确实可比时才有的）一致性判据。

    三种情况**不做比对**，因为那会报出假的不一致：自动化停用（值归手动开关）、
    手动覆盖中（值由上次手动操作决定）、``desync``（比对本身已无意义）。
    """
    if p.system is None:
        return "系统：读不出（无法读取系统勿扰状态）"
    actual = "已静音" if p.system else "未静音"
    if not p.automation or p.notify in ("manual", "desync"):
        return f"系统：{actual}"
    if (p.notify == "muted") == bool(p.system):
        return f"系统：{actual} · 与决策一致"
    expected = "静音" if p.notify == "muted" else "允许通知"
    return f"系统：{actual} · 与决策不符（期望{expected}）"


def format_probe(p: Probe) -> list[str]:
    """拨测逐行文案（设置页拨测结果区一行一句，顺序即排查顺序）。"""
    return [
        f"装置：{_APP_WORDS.get(p.app, p.app)}",
        f"第二屏：{_DISPLAY_WORDS.get(p.display, p.display)}",
        _rule_line(p),
        _decision_line(p),
        _system_line(p),
    ]


def diagnostic_text(p: Probe, *, version: str, stamp: str) -> str:
    """「复制诊断」进剪贴板的整段纯文本。

    比屏幕上那份多两段**维护者才用得上**的东西：原始取值名（``display=extended``
    这种，用户看不懂、维护者一眼定位）与规则明细（含进程 pattern —— 匹配不上时
    第一件事就是看 pattern 长什么样）。
    """
    lines: list[str] = [
        "BarKit · 录屏通知 诊断",
        f"生成时间：{stamp}　版本：{version}",
        "",
        "[拨测]",
        *format_probe(p),
        "",
        "[原始取值]",
        f"app={p.app} display={p.display} notify={p.notify}",
        "",
        "[配置]",
        f"自动化：{'开' if p.automation else '关'}",
        # 只报轮数，不报秒数：一轮 = 框架节拍，而那个值归框架（见 ``Probe`` 上的说明）。
        # 把它写成「见本组件页尾的运行间隔」而不是留空 —— 维护者要顺着这句话去找。
        f"防抖：{p.debounce} 轮（一轮 = 框架节拍，见设置窗里本组件页尾的「运行间隔」）",
        f"启动时自动拉起 OpenDisplay：{'开' if p.open_on_startup else '关'}",
        "",
        f"[规则] {len(p.rules)} 条",
    ]
    for rule in p.rules:
        procs = "、".join(rule.processes) or "（没有进程名 → 这条永不命中）"
        reason = rule.reason or "（无原因文案）"
        lines.append(f"- {rule.name}（{reason}），进程：{procs}")
    if not p.rules:
        lines.append("- （无）")
    return "\n".join(lines) + "\n"
