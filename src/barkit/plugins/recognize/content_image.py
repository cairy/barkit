"""``_ContentImageMixin``：选区拾取、拖放落地与图片入库。

``window_view.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QImage,
    QPixmap,
)


class _ContentImageMixin:
    """图片载入那一路：选区拾取、拖放落地、QImage/路径两条入库路径。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    def _pick_image(self) -> None:
        """「换图」按钮：以父窗口为父的模态文件对话框。

        以 self 为父 + exec()：Qt 会自动把对话框显示在父窗口所在屏幕、模态
        阻塞识别窗口，且窗口管理器会把它吸附在识别窗口之上（不会跑到另一块屏）。
        不手动 setWindowFlag(Window)——那会让对话框脱离父窗口关联、落到默认屏。
        """
        from PySide6.QtWidgets import QFileDialog

        dlg = QFileDialog(self, "选择图片", "", "图片 (*.png *.jpg *.jpeg *.gif *.bmp *.webp *.tiff *.heic)")
        dlg.setFileMode(QFileDialog.FileMode.ExistingFile)
        dlg.setWindowModality(Qt.WindowModality.WindowModal)  # 仅阻塞父窗口（识别窗口）
        if dlg.exec():
            paths = dlg.selectedFiles()
            if paths:
                self._set_image_from_path(paths[0])

    def _on_file_dropped(self, path: str) -> None:
        """SelectionView 发来的文件请求：空串=开'选择文件'对话框，非空=按路径载入。"""
        if path:
            self._set_image_from_path(path)
        else:
            self._pick_image()

    def _set_image_from_qimage(self, image: QImage) -> None:
        self._image_qimage = image
        self._image_path = None
        self._view.set_image(QPixmap.fromImage(image))
        self._show_status(f"图片 {image.width()}×{image.height()}")

    def _set_image_from_path(self, path: str) -> bool:
        pixmap = QPixmap(path)
        if pixmap.isNull():
            self._show_status(f"无法读取图片：{path}")
            return False
        self._image_path = path
        self._image_qimage = None
        self._view.set_image(pixmap)
        self._show_status(f"已载入: {path.rsplit('/', 1)[-1]}（{pixmap.width()}×{pixmap.height()}）")
        return True

    def _image_or_path(self):
        """返回传给识别库的输入：文件来源传路径（零复制），剪贴板来源传 QImage（worker 内转 PIL）。"""
        if self._image_path:
            return self._image_path
        if self._image_qimage is not None:
            return self._image_qimage
        # 剪贴板大图识别后已释放：从视图 pixmap 恢复（缩放显示不影响像素数据）
        pixmap = self._view.pixmap()
        if pixmap is None or pixmap.isNull():
            return None
        return pixmap.toImage()
