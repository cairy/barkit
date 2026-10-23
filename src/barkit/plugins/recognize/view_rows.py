"""结果行的长相：``ResultRow``（悬停提示与复制按钮）与 ``_ResultList``（结果列表容器）。

``window_view.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6 import QtGui
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from barkit.shell.ui_kit import (
    apply_text_role,
    tool_button,
)


class ResultRow(QWidget):
    """结果列表单行：左侧文本/标签 + 右侧「复制」按钮（默认隐藏，悬停整行浮出）。

    设计稿 §5.2：每条结果悬停即出现「复制」，避免原 Tab 方案复制入口过深。
    """

    def __init__(self, text: str, copy_value: str, on_copy, *, tag: str | None = None,
                 mono: bool = False, tip: str = "已复制", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(8)
        if tag:
            tag_label = QLabel(tag)
            apply_text_role(tag_label, "caption")
            tag_label.setFixedWidth(46)
            # 与可换行 _label 并列，单行标签须顶对齐 —— 否则文本换多行时，
            # 标签会按 QHBoxLayout 默认的 AlignVCenter 浮到整行中间（机理 A，
            # 同 clock_view::_RowWidget 对 dot 的处理）。
            layout.addWidget(tag_label, alignment=Qt.AlignmentFlag.AlignTop)
        self._label = QLabel(text)
        self._label.setWordWrap(True)
        self._label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        if mono:
            mf = QtGui.QFont()
            mf.setStyleHint(QtGui.QFont.StyleHint.Monospace)
            mf.setFixedPitch(True)
            self._label.setFont(mf)
        layout.addWidget(self._label, stretch=1)
        self._copy_btn = tool_button("复制", "pill-ghost", size="inline")
        self._copy_btn.setHidden(True)
        self._copy_btn.clicked.connect(lambda: on_copy(copy_value, tip))
        layout.addWidget(self._copy_btn)
        self.setMouseTracking(True)

    def enterEvent(self, event) -> None:
        self._copy_btn.setHidden(False)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._copy_btn.setHidden(True)
        super().leaveEvent(event)


class _ResultList(QWidget):
    """结果列表容器：纵向布局盛放 ResultRow，按内容自然长高。

    不用 QListWidget 是因为其行高取自 item sizeHint，遇到 QLabel 自动换行时
    高度算不准（构造期宽度为 0 → 只算单行），多行文字会被截断。改用自由布局，
    行高由 QLabel 实际换行自然撑开，缩放也正确。
    滚动交给详情卡那一层，本容器不自己套 —— 见 ``__init__`` 里的说明。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._content = QWidget()
        # 不刷背景：透明，直接落在 pane 的 surface 上，避免列表区形成独立「内矩形」
        self._vbox = QVBoxLayout(self._content)
        self._vbox.setContentsMargins(0, 0, 0, 0)
        self._vbox.setSpacing(4)
        # **不自建滚动区**（2026-10-06 删）：本容器住在详情卡里，而详情卡是 ``window``
        # 形态 —— 框架已经把整页包进一层滚动区（`shell/detail.py::_scrolled`）。这里再
        # 包一层就是两层竖滚动条并存，滚轮只驱动鼠标底下那一条。结果行按内容自然长高，
        # 滚动交给外面那一层（删前实测：这一层的视口只分到 149px 高，而 60 条结果要
        # 1409px —— 用户只能看见约十分之一，剩下的全靠内部那根条）。
        layout.addWidget(self._content)

    def clear(self) -> None:
        while self._vbox.count():
            item = self._vbox.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def add_row(self, row: QWidget) -> None:
        self._vbox.addWidget(row)
