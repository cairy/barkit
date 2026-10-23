"""recognize：图片预览 + 多矩形框选视图（方案 §4.1，任务 1.2/1.3）。

- 图片区二合一：有图即预览（QGraphicsPixmapItem 原始像素 1:1 入 scene），
  无图即拾取态（占位提示：点击选文件 / 拖入图片文件）
- 框选作用于图片而非屏幕（评审定案）：rubber-band 画多个矩形，坐标换算走
  scene（图片像素）→ 归一化，Retina 天然正确；Esc 清空；右键点击框 = 删单框
- 视图只负责显示缩放（fitInView），选区始终以图片像素坐标记录，缩放不影响精度
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QLabel,
    QWidget,
)

from barkit.shell import styles
from barkit.shell.ui_kit import apply_text_role

log = logging.getLogger(__name__)

_MIN_BOX_PX = 5  # 小于该尺寸的拖拽视为点击，不成框

# 选区外观：**前景档** accent（靛蓝）描边 + 半透明同色填充，与系统选区观感一致
# （设计稿 §5.2：框选用主题色描边，去掉原先的彩虹多色循环）。
# 取 `accent_text` 而不是 `accent`：后者是**实体档**（面上压白字的实心块），
# 为了压住白字在深色下变深，画到图片区上就太闷了（判据见 tokens.py 的四档表）。
_BOX_FILL_ALPHA = 36


def _accent_color() -> QColor:
    """取当前主题的 accent **前景档**色（靛蓝），随系统浅/深自动切换。"""
    theme = styles.current_theme()
    return QColor(styles.TOKENS[theme]["accent_text"])


def _bg_color() -> QColor:
    """取当前主题 surface 色作图片区底色，随系统浅/深自动切换。"""
    theme = styles.current_theme()
    return QColor(styles.TOKENS[theme]["surface"])


class SelectionView(QGraphicsView):
    """图片预览视图：载入/换图、多矩形框选、归一化选区导出。"""

    # 主线程内信号（无需跨线程）
    image_loaded = Signal()
    boxes_changed = Signal()
    message = Signal(str)  # 状态提示（如"已载入 xxx"）
    file_dropped = Signal(str)  # 用户拖入/点击空白选中的图片路径，交由外层统一处理来源语义

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("selview")  # 供全局 QSS 加边框（与右侧 pane 一致的直角）
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setAcceptDrops(True)
        self.setBackgroundBrush(_bg_color())
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        # 必须能收键盘事件（Esc 清空选区依赖）
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._pixmap_item: QGraphicsPixmapItem | None = None
        self._boxes: list[QGraphicsRectItem] = []
        self._drag_box: QGraphicsRectItem | None = None  # rubber-band 进行中
        self._drag_box_index: int | None = None  # 非 None = 正在移动已有框
        self._drag_start: QRectF | None = None   # scene 坐标起点

        # 拾取态占位提示（无图时覆盖在视图上方）。
        # 文案是用户在这个状态里**唯一**能看到的说明书，必须与真路径一致：截图到剪贴板后，
        # 载入靠「重按热键」或工具栏「剪贴板」按钮 —— 窗口内那 1 s 轮询只更新按钮可用态、
        # 不做自动拾取（见 window_view.py::_poll_clipboard）。回归锚：test_recognize.py 里那枚
        # 文案用例；真的恢复了自动拾取再去改它。
        self._overlay = QLabel(
            "点击选择图片 ／ 拖入图片文件\n\n⌃⌘⇧4 截图到剪贴板，重按热键载入并识别\n（或点工具栏「剪贴板」按钮）",
            self,
        )
        # 占位提示的样式走 `placeholder` 角色（原先这里写死 `#9aa0a6` —— 那个值**不在任何
        # 一套令牌里**，是早期手调的灰：浅色下偏浅、深色下偏亮，且跟着系统切换也不动；
        # 门禁 `test_no_hardcoded_colors` 守这一条）。
        self._overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        apply_text_role(self._overlay, "placeholder")
        self._overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._update_overlay()

        # 系统切浅/深时重绘选区（accent 色随主题变）+ 图片区底色（surface）；
        # 接收者 self 销毁后 Qt 自动断连。
        styles.theme_changed.connect(self._restyle)

    # ---------- 图片 ----------

    @property
    def has_image(self) -> bool:
        return self._pixmap_item is not None

    def image_size(self) -> tuple[int, int] | None:
        """图片原始像素尺寸 (w, h)。无图返回 None。"""
        if self._pixmap_item is None:
            return None
        pixmap = self._pixmap_item.pixmap()
        return pixmap.width(), pixmap.height()

    def pixmap(self) -> QPixmap | None:
        """当前预览图；无图返回 None。"""
        if self._pixmap_item is None:
            return None
        return self._pixmap_item.pixmap()

    def set_image(self, pixmap: QPixmap) -> None:
        # Retina 截图 QPixmap 可能带 dpr=2：QGraphicsPixmapItem 的 boundingRect
        # 会按逻辑尺寸缩水，破坏“scene 像素 = 图片像素”的框选/归一化假设（§4.1）。
        # 强制 dpr=1，以原始像素尺寸进入 scene，坐标链路在 Retina 下才天然正确。
        pixmap.setDevicePixelRatio(1.0)
        self._scene.clear()
        self._pixmap_item = self._scene.addPixmap(pixmap)
        self._pixmap_item.setZValue(0)
        self._boxes.clear()
        self._drag_box = None
        self._fit_image()
        self._update_overlay()
        self.image_loaded.emit()
        self.boxes_changed.emit()

    def clear_image(self) -> None:
        if self._pixmap_item is None:
            return
        self._scene.clear()
        self._pixmap_item = None
        self._boxes.clear()
        self._drag_box = None
        self._update_overlay()
        self.image_loaded.emit()
        self.boxes_changed.emit()

    def _fit_image(self) -> None:
        if self._pixmap_item is not None:
            self.fitInView(self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_image()  # 窗口缩放时保持图片完整显示
        self._overlay.setGeometry(self.rect())

    # ---------- 选区 ----------

    def normalized_boxes(self) -> list[tuple[float, float, float, float]]:
        """导出归一化选区 (x, y, w, h) top-left；含 0 框 = 整图识别。"""
        from barkit.plugins.recognize.recognizer import normalized_regions

        size = self.image_size()
        if size is None:
            return []
        rects = [(b.rect().x(), b.rect().y(), b.rect().width(), b.rect().height()) for b in self._boxes]
        return normalized_regions(rects, size[0], size[1])

    def has_boxes(self) -> bool:
        return bool(self._boxes)

    def clear_boxes(self) -> None:
        for b in self._boxes:
            self._scene.removeItem(b)
        self._boxes.clear()
        self.boxes_changed.emit()
        log.info("已清空 %s 个选区", "")

    def _box_at(self, pos) -> QGraphicsRectItem | None:
        """scene 坐标下命中某个框（不含临时框）。"""
        for b in self._boxes:
            if b.rect().contains(pos):
                return b
        return None

    def _remove_box(self, box: QGraphicsRectItem) -> None:
        if box in self._boxes:
            self._boxes.remove(box)
        self._scene.removeItem(box)
        self.boxes_changed.emit()

    def _restyle(self) -> None:
        """系统浅/深切换时重绘：选区 accent 色 + 图片区 surface 底色 + 占位提示文字色。"""
        self.setBackgroundBrush(_bg_color())
        apply_text_role(self._overlay, "placeholder")
        accent = _accent_color()
        fill = QColor(accent)
        fill.setAlpha(_BOX_FILL_ALPHA)
        for b in self._boxes:
            b.setPen(QPen(accent, 2.0))
            b.setBrush(fill)

    def _new_box(self, rect: QRectF) -> None:
        if rect.width() < _MIN_BOX_PX or rect.height() < _MIN_BOX_PX:
            return
        item = QGraphicsRectItem(rect)
        item.setZValue(1)
        self._scene.addItem(item)
        self._boxes.append(item)
        self._restyle()
        self.boxes_changed.emit()

    # ---------- 交互：框选 / 移动 / 删框 ----------

    def mousePressEvent(self, event) -> None:
        if not self.has_image:
            # 拾取态点击空白：请外层弹文件对话框（来源语义由外层统一管，避免
            # 子控件直接加载导致 _image_qimage 残留而识别错乱）。
            self.file_dropped.emit("")  # 空串 = 请求"选择文件"对话框，非具体路径
            return
        if event.button() == Qt.MouseButton.RightButton:
            hit = self._box_at(self._scene_pos(event))
            if hit:
                self._remove_box(hit)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = self._scene_pos(event)
        self._drag_start = pos
        hit = self._box_at(pos)
        if hit:
            # 拖住已有框 = 移动（保持中心跟随）
            self._drag_box_index = self._boxes.index(hit)
        else:
            # 空白处拖拽 = 新建框
            self._drag_box_index = None
            self._drag_box = QGraphicsRectItem(QRectF(pos, pos))
            self._drag_box.setZValue(2)
            self._drag_box.setPen(QPen(_accent_color(), 2, Qt.PenStyle.DashLine))
            self._scene.addItem(self._drag_box)

    def mouseMoveEvent(self, event) -> None:
        if not event.buttons() & Qt.MouseButton.LeftButton or self._drag_start is None:
            return
        pos = self._scene_pos(event)
        if self._drag_box_index is not None:
            # 移动已有框：保持尺寸，中心跟随
            target = self._boxes[self._drag_box_index]
            size = target.rect().size()
            target.setRect(
                QRectF(pos.x() - size.width() / 2, pos.y() - size.height() / 2, size.width(), size.height())
            )
        elif self._drag_box is not None:
            self._drag_box.setRect(QRectF(self._drag_start, pos).normalized())

    def _scene_pos(self, event):
        return self.mapToScene(event.position().toPoint())

    # ⚠️ 本类**曾经有两份** `mouseReleaseEvent`（拆包时逐字节搬移把两份都带了过来）：
    # 前一份从未被执行，是死代码；两份在**可达路径**上等价（`mousePressEvent` 保证
    # `_drag_box_index` 与 `_drag_box` 恰有一个非 None），差别只在 `_drag_start = None`
    # 落在分支内还是分支外 —— 现留的是原来**生效的那一份**（行为零变更），
    # 2026-10-05 删死代码，见 `log/2026-10-05-生产代码pyflakes告警清零.md`。
    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._drag_box_index is not None:
            self._drag_box_index = None
            self._drag_start = None
            self.boxes_changed.emit()
            return
        if self._drag_box is not None:
            rect = self._drag_box.rect().normalized()
            self._scene.removeItem(self._drag_box)
            self._drag_box = None
            self._drag_start = None
            self._new_box(rect)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape and self._boxes:
            self.clear_boxes()
            self._show_message("已清空全部选区")
            return
        super().keyPressEvent(event)

    # ---------- 拾取：文件选择 / 拖入 ----------
    # 子控件不再直接加载图片，统一发射 file_dropped 交由外层处理来源语义
    # （存路径 / 存 QImage / 清旧状态），避免拖入时残留旧 _image_qimage 导致识别错乱。

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        # macOS 下必须持续接受 move 事件，否则拖到控件上会显示"禁止"图标。
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        event.accept()

    def dropEvent(self, event) -> None:
        urls = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if urls:
            self.file_dropped.emit(urls[0])  # 具体路径 → 外层统一载入

    # ---------- 提示 ----------

    def _show_message(self, text: str) -> None:
        log.info(text)
        self.message.emit(text)

    def _update_overlay(self) -> None:
        self._overlay.setVisible(not self.has_image)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._overlay.setGeometry(self.rect())