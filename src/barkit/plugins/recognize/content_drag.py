"""``_ContentDragMixin``：内容面自身的拖放与显示事件。

``window_view.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtCore import QTimer


class _ContentDragMixin:
    """内容面自身的拖放与显示事件（**Qt 虚函数重写**，按纪律留本体）。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # 内容面只管「显示后自动识别」（剪贴板预载的图）：激活策略切换归壳 /
        # 框架，这里不碰。独立窗口壳与 DetailForm 都会在 show 时切 Regular，
        # 内容面自身无需知道。
        if self._auto_recognize_pending:
            self._auto_recognize_pending = False
            QTimer.singleShot(120, self._start)  # 等窗口渲染完再识别

    # ---------- 整窗拖放（§4.1 拾取态：拖入图片文件）----------
    # 内容面自身接受拖放；独立窗口壳 RecognizeWindow 也另挂一份同款转发（覆盖其
    # 标题栏区域——QMainWindow 不接受拖放，需窗口层拦截才能覆盖整窗）。

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        event.accept()

    def dropEvent(self, event) -> None:
        urls = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if not urls:
            event.ignore()
            return
        # 走统一文件载入路径（来源语义由内容面管理，避免识别错乱）。
        self._on_file_dropped(urls[0])
        event.acceptProposedAction()
