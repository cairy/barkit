"""动作结局送进系统通知的两个出口。

``app_shell.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import time

from barkit.shell.hotkey_catalog import FEEDBACK_NOTIFY
from barkit.shell.hotkey_feedback import outcome_of

from .consts import FRAMEWORK_NOTIFY_ID


class _FeedbackMixin:
    """动作结局的两个出口：面板收起后走系统通知，与热键反馈中枢的重反馈出口。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    def _report_off_panel(self, title: str, result) -> None:
        """面板**已经收起来**时，把一次动作的结局送进系统通知。

        为什么需要这个出口：面板操作区那些按钮按下后**面板先 ``hide()`` 自己再跑回调**
        （它们开的是别的窗）⇒ 结果回来时承载结果的那一块界面已经关了。这正是
        ``ActionSpec.on_click`` 此前一直没扩宽成 ``ActionCallback`` 的理由（“一条送不出去
        的通道 ＝ 撒谎的表”，见那份注释）；2026-09-28 把出口补上之后才扩宽。

        档位固定走通知（这条路上没有逐条设置，那是热键才有的），而「失败了必须被看见」
        是既有的硬规则。成功那一档由 ``panel`` 直接拦下、根本不会走到这里 —— 它没有
        逐条档位可依，而 ActionResult 的契约本来就写着「成功且状态看得见就别再占一行字」。
        """
        ok, detail = outcome_of(result)
        self._feedback.report(
            name=f"action.{title}", title=title, ok=ok, detail=detail, mode=FEEDBACK_NOTIFY,
        )

    def _send_hotkey_notice(self, name: str, title: str, body: str) -> None:
        """发一条系统通知（``HotkeyFeedback`` 的重反馈出口）。

        带上时点：通知中枢按「key + 内容指纹」去重，而热键是**用户显式触发**的 ——
        连按两次就该有两条回执，不能因为两次结果文案一样就被判成重复。
        """
        hub = getattr(getattr(self._registry, "ctx", None), "notify", None)
        if hub is None:
            return
        from barkit.capabilities.notify import NotificationRequest

        hub.send(FRAMEWORK_NOTIFY_ID, NotificationRequest(
            key=f"hotkey.{name}",
            title=title,
            body=f"{body}\n{time.strftime('%H:%M:%S')}" if body else time.strftime('%H:%M:%S'),
        ))
