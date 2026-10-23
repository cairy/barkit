"""notify_guard/decider：决策状态机（方案 §4.2，任务 2.3）。

决策规则定案：allow = S1 AND (NOT S2)，全 app 唯一决策者。

- 防抖：连续 debounce 个轮询周期结果一致才写 backend（避免反复重启 usernoted）
- 手动覆盖：manual_toggle 立即生效并持续保持；信号组合 (S1,S2) 下一次变化时
  自动逻辑重新接管（信号不变则手动保持）
- 恢复自动：resume_auto 立即交回自动逻辑，跳过防抖一次写入（防用户忘记自己切过）

纯逻辑、零 Qt 依赖，可直接单测。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class Decision:
    """单轮决策结果。action: write（需写 backend）| none | wait（防抖中）。"""

    allow: bool          # 应生效的 allow（True=允许通知,dndMirrored=False）
    reason: str          # 原因文案（信号侧）
    mode: str            # auto | manual
    action: str          # write | none | wait
    s1: bool
    s2: bool


def reason_for(s1: bool, s2: bool, hit_rules: list[dict]) -> str:
    """信号组合 → 面板原因文案。"""
    names = "、".join(
        str(r.get("reason") or r.get("name") or "?") for r in (hit_rules or [])
    )
    if s1 and s2:
        return f"OpenDisplay 延伸屏 + {names}"
    if s1:
        return "OpenDisplay 延伸屏使用中"
    if s2:
        return names
    return "无虚拟显示器/录屏活动"


class Decider:
    def __init__(self, debounce: int = 2, applied: bool | None = None,
                 auto_enabled: bool = True) -> None:
        """
        debounce: 连续 N 个周期结果一致才写入（配置 plugins.notify_guard.debounce）
        applied:   启动时从 backend 读入的当前 allow 状态（None=未知，当作需写入）
        auto_enabled: 自动化开关。False=自动化关闭，状态完全由手动开关控制
          （decider 不再按信号自动静音/恢复，step 直接返回当前有效值）。
        """
        self.debounce = max(1, int(debounce))
        self.applied = applied            # 记忆的最近一次成功写入值
        self.mode = "auto"                # auto | manual
        self.auto_enabled = bool(auto_enabled)
        self.manual_allow: bool | None = None
        self.combo_at_toggle: tuple[bool, bool] | None = None
        self.pending: bool | None = None  # 防抖中的候选 allow
        self.pending_count = 0
        self.last_reason = "启动中…"

    # ---------- 主决策 ----------

    def step(self, s1: bool, s2: bool, hit_rules: list[dict]) -> Decision:
        """每轮 tick 调用一次。"""
        reason = reason_for(s1, s2, hit_rules)
        self.last_reason = reason

        # 自动化关闭：decider 不再按信号决策，仅维持当前有效状态（手动开关接管）。
        if not self.auto_enabled:
            return Decision(self.effective_allow, "", "manual", "none", s1, s2)

        if self.mode == "manual":
            if self.combo_at_toggle is not None and (s1, s2) != self.combo_at_toggle:
                log.info("信号组合变化 %s → %s，通知守卫自动接管", self.combo_at_toggle, (s1, s2))
                self._enter_auto()
            else:
                return Decision(
                    self.manual_allow, reason, "manual", "none", s1, s2
                )

        return self._compute_auto(s1, s2, hit_rules, immediate=False)

    # ---------- 手动控制 ----------

    def manual_toggle(self, s1: bool, s2: bool) -> bool:
        """手动切换（一次性覆盖）：翻转有效值并保持，记录信号组合，返回新 allow。"""
        new = not self.effective_allow
        self.mode = "manual"
        self.manual_allow = new
        self.combo_at_toggle = (s1, s2)
        self.pending = None
        self.pending_count = 0
        log.info("手动切换通知 → allow=%s（combo=%s，信号变化前保持手工状态）", new, (s1, s2))
        return new

    def resume_auto(self, s1: bool, s2: bool, hit_rules: list[dict]) -> Decision:
        """恢复自动：退出手动、按当前信号立即决策（跳过防抖，一次写入）。"""
        self._enter_auto()
        reason = reason_for(s1, s2, hit_rules)
        self.last_reason = reason
        return self._compute_auto(s1, s2, hit_rules, immediate=True)

    def _enter_auto(self) -> None:
        self.mode = "auto"
        self.manual_allow = None
        self.combo_at_toggle = None
        self.pending = None
        self.pending_count = 0

    def _compute_auto(self, s1, s2, hit_rules, immediate: bool) -> Decision:
        desired = s1 and not s2
        reason = reason_for(s1, s2, hit_rules)
        if immediate:
            action = "write" if desired != self.applied else "none"
        elif desired != self.applied:
            if self.pending != desired:
                self.pending = desired
                self.pending_count = 1
            else:
                self.pending_count += 1
            action = "write" if self.pending_count >= self.debounce else "wait"
        else:
            self.pending = None
            self.pending_count = 0
            action = "none"
        return Decision(desired, reason, "auto", action, s1, s2)

    # ---------- 写结果确认 ----------

    def confirm_write(self, allow: bool) -> None:
        """backend 写成功后调用：更新记忆、清防抖。"""
        self.applied = allow
        self.pending = None
        self.pending_count = 0

    def on_write_failure(self) -> None:
        """写失败：清防抖计数，下轮从新阈值重新累积（避免连锤 usernoted）。"""
        self.pending = None
        self.pending_count = 0

    # ---------- 状态 ----------

    @property
    def effective_allow(self) -> bool:
        if self.mode == "manual" and self.manual_allow is not None:
            return self.manual_allow
        return self.applied if self.applied is not None else True

    def status(self) -> dict:
        return {
            "mode": self.mode,
            "allow": self.effective_allow,
            "applied": self.applied,
            "pending": self.pending,
            "pending_count": self.pending_count,
            "debounce": self.debounce,
            "auto_enabled": self.auto_enabled,
            "reason": self.last_reason,
        }