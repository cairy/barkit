"""「清单变了」与「刷面板」两个通知出口。

``plugin_manager.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations


from .consts import log


class _NotifyMixin:
    """把「清单变了」与「刷一下面板」这两件事通知出去。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 面板刷新 ----------

    def _refresh_panel(self) -> None:
        if self._panel is not None:
            self._panel.set_contributions(self._contributions)

    def _notify_changed(self) -> None:
        """插件启用/禁用/卸载后，通知外部监听者（如设置窗重建分页）。

        单监听者异常不应中断 manager 的其余逻辑。
        """
        if self.on_plugins_changed is not None:
            try:
                self.on_plugins_changed()
            except Exception:  # noqa: BLE001
                log.exception("插件变更回调 on_plugins_changed 异常")
