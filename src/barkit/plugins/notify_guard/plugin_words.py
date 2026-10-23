"""面板行副标题：一句说完三台机（设计 §4.3 词条库 + §6.3 场景表）。

次序「异常项优先 → 第二屏 → 静音原因 → 通知态 → 自动化」由此保证：``desync`` 先说、
自动化停用只报这一件事、手动覆盖报「覆盖中」、常态报「第二屏 · 命中原因，已静音」。
超宽交给 ``ElideLabel`` 省略号兜底 —— 不引入裁剪阈值（不可验证，而本组件几种场景
句长本就在 320px 内）。

``plugin.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from barkit.plugins.notify_guard import signals

from .plugin_rules import _rule_reason


class _WordsMixin:
    """面板行副标题：一句说完三台机（设计 §4.3 词条库 + §6.3 场景表）。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 面板行副标题（行级回调，契约 §2.7 / 决议 D24）----------

    def _display_word(self) -> str:
        """第二屏词条（设计 §4.3 词条库：三选一，**必出**）。

        措辞沿用设计 §6.3 场景表原文（``iPad 已连接``）——上游的"第二屏"在本项目里
        就是那台 iPad，用户看到的该是人话，不是 `extended`。
        """
        if self._display_state == signals.DISPLAY_ABSENT:
            return "未安装 OpenDisplay"
        if self._display_state == signals.DISPLAY_EXTENDED:
            return "iPad 已连接"
        return "无第二屏"

    def _hit_reason(self) -> str:
        """最近一轮命中的规则**文案**（如「OBS 推流中」）；无命中返回空串。

        取规则自己的 ``reason``（用户配的话），而不是 decider 合成的整句
        ``reason_for(...)``——副标题只需要"为什么静音"这一件事（§4.3 词条库）。
        """
        names = [_rule_reason(r) for r in self._last_hits]
        return "、".join(n for n in names if n)

    def _row_subtitle(self) -> str:
        """面板行副标题：一行说完三台机（设计 §4.3 词条库 ＋ §6.3 场景表）。

        唯一真源是设计的 §6.3 场景表；本方法每个分支对应它的一行，次序
        「异常项优先 → 第二屏 → 静音原因 → 通知态 → 自动化」由此保证：

        - 异常（``desync``）优先说出（"状态自证"要看得见）；
        - 自动化停用 ⇒ 只报这一件事（其余读数已不可信）；
        - 手动覆盖 ⇒ 报"覆盖中 / 已挂起"（不报第二屏——用户此刻关心的是谁在管事）；
        - 常态 ⇒ ``第二屏 · [命中原因，]通知态``。

        超宽交给 ``ElideLabel`` 省略号兜底（**不做词条级裁剪**：裁剪阈值不可验证，
        而本组件这几种场景句长本就在 320px 内——不引入魔法数字是更诚实的取舍）。
        """
        state, _reason = self._notify_state()
        if state == "desync":
            return f"{self._display_word()} · 状态不同步"
        if not self.automation_active:
            return "自动化已停用 · 通知由手动开关控制"
        if state == "manual":
            return "手动覆盖中 · 自动接管已挂起"
        disp = self._display_word()
        if state == "allowed":
            return f"{disp} · 通知正常"
        hit = self._hit_reason()
        if hit:
            return f"{disp} · {hit}，已静音"
        return f"{disp} · 通知已静音"
