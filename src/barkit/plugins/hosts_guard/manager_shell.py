"""换屏那一层：撤下各屏挂着的确认问句、切到某一屏、回表页。

三屏（表格 / 条目表单 / 批量导入）由 `ui_kit.EditStack` 换，**没有**「页 0 / 页 1 / 页 2」
的魔数下标（写错一位的症状是「点了返回、屏没变」，而代码里看不出来）。

``manager_view.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtWidgets import QWidget


#: 两个二级页（条目表单 / 批量导入）与表页用 :class:`ui_kit.EditStack` 换屏，
#: **不再有「页 0 / 页 1 / 页 2」的魔数下标**（写错一位的症状是「点了返回、屏没变」，
#: 而代码里看不出来）—— 2026-09-27 改。


class _ShellMixin:
    """换屏：撤下各屏挂着的确认问句、切到某一屏、回表页。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 切页 ----------

    def _dismiss_feedback(self) -> None:
        """撤下各屏挂着的确认问句（换屏了就别再等那一下确认）。"""
        for fb in (self._fb, self._edit_form.feedback, self._edit_import.feedback):
            fb.dismiss()

    def _show_page(self, screen: QWidget) -> None:
        """切到某个二级屏（换屏容器自己记「开过哪几屏」）。"""
        self._dismiss_feedback()
        self._stack.push(screen)

    def _back_to_table(self) -> None:
        """回表页 —— 两个二级屏的返回键、以及「做完了」都走这里。"""
        self._dismiss_feedback()
        self._stack.back()
