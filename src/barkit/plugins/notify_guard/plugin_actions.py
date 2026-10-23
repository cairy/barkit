"""决策执行与手动控制：``tick``、一次性手动覆盖（``toggle_action``）、
交回自动（``resume_auto_action``）。

``tick`` 跑在 worker 线程（scheduler 驱动），写 backend 失败要 ``raise`` —— 让
scheduler 计失败、execute 类连续失败熔断。三个手动动作都**返回** ``ActionResult``：
用户按热键那一刻界面是纹丝不动的，状态行是兜底、不该是唯一出口。


``plugin.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import logging

from barkit.contribution.specs import (
    ActionOutcome,
    ActionResult,
    StateReport,
)
from barkit.plugins.notify_guard.backend import BackendError


log = logging.getLogger("barkit.plugins.notify_guard.plugin")


class _ActionsMixin:
    """决策执行与手动控制：tick、一次性手动覆盖、交回自动。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 决策执行（worker 线程，scheduler 驱动）----------

    def tick(self) -> dict[str, StateReport]:
        """每轮：读信号 → 决策 → 必要时写 backend → 返回三台机快照。

        §5.3：返回 ``{key: StateReport}``，框架逐 key 发布到
        ``plugin.notify_guard.status.<key>``（多台状态机的标准写法）。
        """
        # 启动首拍先处理「要不要把 OpenDisplay 拉起来」（一次；之后每轮立即返回）——
        # 放在读信号之前：它是本轮最靠前、且与信号无关的一件事。
        self._launch_on_startup_once()
        self.decider.debounce = self.debounce
        combo, hits = self._read_signals()
        d = self.decider.step(*combo, hits)
        if d.action == "write":
            try:
                self.backend.apply(not d.allow)
            except BackendError as e:
                self.decider.on_write_failure()
                log.error("录屏通知写入失败：%s", e)
                raise  # scheduler 计失败，execute 类连续失败熔断
            self.decider.confirm_write(d.allow)
        return self._all_snapshots()

    # ---------- 手动控制（主线程：托盘 / 热键 / 面板）----------

    def toggle_action(self) -> "ActionResult | None":
        """⌃⌥M / 面板开关：一次性手动覆盖，立即生效并保持（信号组合一变即自动交还）。

        失败时**返回** ``ActionResult``（2026-09-28 改，原先只记日志）。原先的理由是
        「快照会报 ``desync``（ERR），那行状态就是反馈」—— 但那要等**下一轮 tick**
        才刷出来，而用户按热键 / 拨开关的那一刻界面是纹丝不动的：开关弹回原位、
        什么也没说。状态行是**兜底**，不该是唯一的出口。
        """
        failed = None
        try:
            combo, _ = self._current_signals()
            new = not self.decider.effective_allow
            # 先写系统、成功后才把 decider 切到 manual：写失败就不改 decider 状态，
            # 否则开关显示已切换、系统其实没变，且 ``step`` 在 manual 且 combo 未变时
            # 恒返回 ``none`` ⇒ 永不重试（P1）。
            self.backend.apply(not new)
        except BackendError as e:
            log.error("手动切换通知失败：%s", e)
            self.decider.on_write_failure()
            failed = ActionResult(
                ActionOutcome.FAILED, "没能切换通知 —— 写系统设置失败，请重试。"
            )
        else:
            self.decider.manual_toggle(*combo)
            self.decider.confirm_write(new)
        self._publish_all()
        return failed

    def resume_auto_action(self) -> "ActionResult | None":
        """手动态「恢复自动」：交回自动逻辑并立即生效（跳过防抖一次写入）。

        **为什么必须有这个入口**：手动覆盖的自动归还条件是**信号组合变化**
        （``decider.step`` 的 ``combo_at_toggle`` 分支）。而「没插 iPad ＋ 没命中任何
        规则」这种稳态下组合永不变化 ⇒ 用户点过那枚 Switch 之后就**再也回不到自动**，
        面板会一直挂着「手动覆盖中 · 自动接管已挂起」。在此之前本方法全仓零调用方
        （设计 §「执行细节」写了"手动覆盖态提供恢复自动按钮"，没人接线）。

        **按钮为什么在设置页而不是面板行**（与设计那句话的落点偏差，判据在此）：面板
        行控件硬上限 2 枚（决议 D6），而 §6.3 那行只剩一枚 Switch（策略：总闸归设置页）；
        更硬的一条是行 widget 按 ``identity()`` 复用（``shell/panel.sync_status_rows``）
        ⇒ **条件控件在面板上根本长不出来**，常驻一枚「恢复自动」在自动态下又是个死按钮。
        偏差与取证 → `docs/log/2026-09-18-手动覆盖归还入口.md`。

        **自动化总闸关着时一个字节都不写**：那时状态归手动开关（``_notify_state`` 报
        ``frozen``），按信号写一次会与「自动化已停用 · 通知由手动开关控制」自相矛盾。

        返回 ``None``（成功）＝ 没话要说：卡片消失 ＋ 状态条从「手动覆盖中」变回
        「正常 / 已静音」，结局本身就看得见（``ActionResult`` 的口径）。
        """
        if not self.automation_active:
            return ActionResult(
                ActionOutcome.FAILED,
                "自动化总闸是关着的 —— 通知此刻完全由手动开关说了算，"
                "先打开它才会按信号自动决策。",
            )
        try:
            combo, hits = self._current_signals()
            d = self.decider.resume_auto(*combo, hits)
            if d.action == "write":
                self.backend.apply(not d.allow)
                self.decider.confirm_write(d.allow)
        except BackendError as e:
            log.error("恢复自动失败：%s", e)
            self._publish_all()
            return ActionResult(
                ActionOutcome.FAILED,
                "没能交回自动 —— 写系统通知设置失败了，请重试。",
            )
        self._publish_all()
        return None
