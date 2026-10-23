"""``_FeedbackMixin`` —— 行内反馈的**两条出口分流**（设置页那条槽 vs 面板结果槽）。

槽本身（``attach_feedback`` / ``_forget_feedback`` / ``_say_info`` / ``_say_hint`` /
``_say_error``）自 2026-10-05 起收归 ``capabilities.inline_feedback``
（``InlineFeedbackCapability``）—— 四个插件各写一遍同名同形那几份已删，本模块只剩
本组件自己的领域决定。

``plugin.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from barkit.contribution.specs import (
    ActionOutcome,
    ActionResult,
)


class _FeedbackMixin:
    """行内反馈的**两条出口**：设置页那条槽 vs 面板结果槽。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 两条出口的分流 ----------

    def _failed(self, text: str) -> "ActionResult | None":
        """报告一次失败，**两条出口只走一条**。

        - 设置页开着（`self._feedback` 非 None）⇒ 照旧当场落在它那块槽上，返回 ``None``：
          调用方（可能就是设置页自己的按钮）不需要第二份文案。
        - 没有槽位（**面板 / 托盘路径**）⇒ 把结局**返回上去**，由面板那一行结果槽呈现
          （`shell/panel.StatusPanel.present_action_result`）。

        改前的口径是「面板侧看不到文案，日志是唯一线索」—— 于是面板上按一下开关、写盘
        失败时界面毫无变化；`ActionResult`（契约 D32-②）正是为这条缺口立的。

        两个界面同时开着的情况也有归属：设置页的槽是**它的页面**的，先到先得，
        面板那条路的调用根本拿不到返回值 ⇒ 不会同一句话冒两次。
        """
        self._say_error(text)
        if self._feedback is not None:
            return None
        return ActionResult(ActionOutcome.FAILED, text)

    def _note(self, text: str) -> "ActionResult | None":
        """报告一条**中性**结果（不是错，但用户得知道），两条出口二选一，同 `_failed`。

        典型用法是「动作被拒了，但原因不是出错」——如「交还前得先去设置页勾网卡」。
        这类话没有 ``_feedback`` 就浮不上来，而它在面板侧恰恰是**唯一**的行动指引。
        """
        self._say_hint(text)
        if self._feedback is not None:
            return None
        return ActionResult(ActionOutcome.OK, text)
