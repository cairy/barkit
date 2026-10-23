"""设置页只读回显、进程候选、拨测文本，以及三级线索的读取。

分界是**副作用**：``tick`` 会按防抖往系统写一次通知开关，这里的任何一条都不写 ——
用户在设置页点一下诊断，不该把通知静音改掉。``probe`` 与 ``tick`` 的分界同理，
且它不动 ``_last_combo`` / ``_last_hits``（那是 tick 的记账，不是诊断的账）。
措辞不在这里（``diagnostics.format_probe``）：探测归插件、排版归排版层。

``plugin.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import time

from barkit import __version__
from barkit.plugins.notify_guard import (
    diagnostics,
    signals,
)
from barkit.plugins.notify_guard.diagnostics import (
    Probe,
    RuleLine,
)

from .plugin_rules import _rule_reason


class _ProbeMixin:
    """设置页只读回显、进程候选、拨测文本，以及三级线索的读取（全部只读、零副作用）。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 设置页用的只读回显 ----------

    def display_state(self) -> str:
        """最近一次探测的 display 四态取值（设置页顶部状态条）。"""
        return self._display_state

    def notify_state(self) -> str:
        """当前 notify 取值（allowed / muted / manual / desync / frozen）。"""
        return self._notify_state()[0]

    def hit_rule_names(self) -> list[str]:
        """最近一轮命中的规则名（设置页列表行右侧「●命中」回显用）。"""
        return [str(r.get("name") or "?") for r in self._last_hits]

    def process_candidates(self) -> list[str]:
        """进程选择器候选：正在运行的 App（basename，去重排序）。

        复用 ``signals.get_process_snapshot()``（ps 快照，TTL 缓存）。ps 给的是**小写
        全路径**，取 basename 当用户可见的进程名——而 ``match_rules`` 本就是子串匹配，
        basename 作为 pattern 依然命中（``signals.py`` 的 `any(pat in path ...)`）。
        系统内部路径（``/System`` / ``/usr/libexec`` 等）被滤掉，把列表压到用户
        认得出、也敢勾的那一批。**懒加载**：只在用户点「选择进程」时调。
        """
        out: set[str] = set()
        for path in signals.get_process_snapshot():
            base = path.rsplit("/", 1)[-1].strip()
            if not base or base.startswith("."):
                continue
            if path.startswith(
                ("/system/", "/usr/libexec/", "/usr/sbin/", "/sbin/", "/usr/bin/")
            ):
                continue
            out.add(base)
        return sorted(out)

    # ---------- 运维（设置页：拨测 / 打开数据目录）----------

    def probe(self) -> Probe:
        """拨测：当场读一轮三级线索，回报「现在是什么情况」（**只读**）。

        与 ``tick()`` 的分界是**副作用**：tick 会按防抖往系统写一次通知开关，拨测一个
        字节都不写 —— 用户在设置页点一下诊断，不该把通知静音改掉。同理它**不动**
        ``_last_combo`` / ``_last_hits``（那是 tick 的记账，不是诊断的账）。

        措辞不在这里（``diagnostics.format_probe``）：探测归插件、排版归排版层，
        于是「拨测」与「复制诊断」两个出口不可能各说各话。
        """
        display = signals.detect_display_state()
        rules = self.rules()
        hits = signals.match_rules(signals.get_process_snapshot(), rules)
        reasons = [_rule_reason(r) for r in hits]
        notify, notify_reason = self._notify_state()
        return Probe(
            app=signals.detect_app_state(),
            display=display,
            notify=notify,
            reason=notify_reason,
            automation=self.automation_active,
            rules=tuple(
                RuleLine(
                    name=str(r.get("name") or "?"),
                    reason=_rule_reason(r),
                    processes=tuple(str(x) for x in (r.get("processes") or []) if x),
                )
                for r in rules
            ),
            hits=tuple(x for x in reasons if x),
            system=self.backend.read(),
            debounce=self.debounce,
            open_on_startup=self.open_on_startup,
        )

    def diagnostics_text(self) -> "str | None":
        """诊断文本：此刻的拨测结果排成一段可粘贴给维护者的纯文本（框架「复制诊断」用）。

        归**组件**的理由只有一条：只有它知道该收集哪些事实（三级线索 / 规则明细 /
        原始取值名）。形状（放进剪贴板、说一句回执）归框架 —— 见 ``shell/data_ops``。

        与「拨测」按钮共用同一批事实（``probe()``）与同一个排版层（``diagnostics``），
        两个出口因此不可能各说各话。**当场重新拨测**而不是复用屏幕上那份：用户点
        「复制诊断」的场景就是他刚发现不对，要的是此刻的事实。

        （此前这段逻辑长在 ``settings.py::_on_copy_diagnostics`` 里，而「打开数据目录」
        也长在插件侧 —— 于是「为什么只有录屏通知有这两枚按钮」成了必然，用户 2026-09-22
        上报。形状提为框架件后，插件侧只留内容。）
        """
        return diagnostics.diagnostic_text(
            self.probe(), version=__version__, stamp=time.strftime("%Y-%m-%d %H:%M:%S")
        )

    # ---------- 信号 ----------

    def _read_signals(self) -> tuple[tuple[bool, bool], list[dict]]:
        """完整读一轮信号：(s1, s2), 命中规则。任意线程可调。

        沿用既有降频纪律（signals.py 模块 docstring）：
        - 自动化关闭 → **不探测**（§4.2「不探测」：display 保持最后已知取值），
          决策恒为维持现状；
        - ``S1`` 由 ``display`` 四态派生——只有 ``extended`` 才算「第二屏在线」，
          决策口径与重写前**逐字一致**（§1.4 已结案，decider 一行未改）；
        - ``allow = S1 AND (NOT S2)``：S1=False 时决策恒为静音、S2 不影响结果
          → 短路跳过 ps 快照（日常无第二屏时 ps 零调用）。
        """
        # 自动化关闭：无需读信号（状态由手动开关控制）
        if not self.decider.auto_enabled:
            return (False, False), []
        self._display_state = signals.detect_display_state()
        s1 = self._s1()
        if not s1 and self.decider.mode != "manual":
            self._last_combo = (False, False)
            self._last_hits = []
            return self._last_combo, []
        hits = signals.match_rules(signals.get_process_snapshot(), self.rules())
        self._last_combo = (s1, bool(hits))
        self._last_hits = hits
        return self._last_combo, hits

    def _s1(self) -> bool:
        """S1 = 第二屏在线（extend 模式指纹；mirror 模式恒假，§2.3 已知盲区）。"""
        return self._display_state == signals.DISPLAY_EXTENDED

    def _current_signals(self) -> tuple[tuple[bool, bool], list[dict]]:
        """手动操作优先用最近的 tick 结果（3s 内新鲜），避免主线程跑 profiler 卡顿。"""
        if self._last_combo is not None:
            return self._last_combo, self._last_hits
        return self._read_signals()
