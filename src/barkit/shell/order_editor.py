"""ui/shell/order_editor：有序列表编辑器（手柄拖拽 ＋ Alt+↑/↓ 键盘微调）。

**通用件**：它只管「一串**键**按什么顺序排、拖完落到哪」，完全不知道那串键是什么。
两个消费方 —— 设置窗「插件」页（键 = `plugin_id`）与《录屏通知》设置页的规则列表
（键 = 规则下标）。**键由调用方决定**，只需在**一次装载内**唯一且稳定。

调用方只需给两个回调::

    editor = OrderEditor(
        build_row=lambda key, handle: my_row(key, handle),
        on_reorder=lambda key, to_index: persist(key, to_index),
    )
    editor.set_items(["a", "b", "c"])

- ``build_row(key, handle) -> QWidget``：调用方渲染行内容，并把框架给的
  手柄放进自己想要的位置（一般是行首）。编辑器只持有返回的 widget，不关心内容，
  因此行内仍可自由放开关 / 按钮，不会被拖拽逻辑抢事件。
- ``on_reorder(key, to_index) -> bool``：落库。``to_index`` 是 key 在
  「移除自身后的剩余序列」中的插入下标，与落库后的数组下标一致（插件页那条消费路径
  见 ``PluginManager.reorder_plugin``）。返回 False = 落库失败，编辑器回滚。
  ``on_reorder=None`` 时只做视觉排序（便于独立复用与单测）。

三条硬约束：

1. **拖拽只由手柄发起**：手柄独占 mousePress，行内开关 / 按钮照常响应，
   不会出现「想点开关却把行拖走」。
2. **手柄可聚焦 + Alt+↑/↓**：鼠标拖拽不是唯一入口（键盘等价操作）。
3. **拖拽中重入安全**：拖到一半外部调 ``set_items``（如数据变了触发重建）
   先中止拖拽并复位，不留悬浮残影。

> **名字的来历**（2026-09-19）：本模块原叫 `plugin_order.py`、类名 `PluginOrderEditor`，
> 是照「插件列表」这一个消费方命名的。规则列表要排序时它成了第二个消费方，
> 于是按职责改名 —— 不改名就等于让第二个消费方去用一把写着「插件」的尺子。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from PySide6.QtCore import QPoint, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QVBoxLayout, QWidget

from barkit.shell import styles

__all__ = ["OrderEditor"]

# 超过该像素位移才认定为拖拽（否则视为点击手柄，不产生任何顺序变化）
_DRAG_THRESHOLD = 4

#: 手柄悬停的那句**操作话术**：怎么拖、键盘怎么用。只说「怎么操作」，
#: **不说「顺序意味着什么」** —— 那是消费方的事（``OrderEditor(handle_hint=...)``），
#: 通用件认识「状态机 / 规则 / 插件」任何一词都是越界。
_HANDLE_TIP = "拖拽调整顺序（聚焦后 Alt+↑ / Alt+↓ 微调）"


class _DragHandle(QWidget):
    """行首拖拽手柄：6 点字形，鼠标拖拽 + Alt+↑/↓ 键盘微调。"""

    dragPressed = Signal(int, QPoint)   # (行下标, 全局按下点)
    dragMoved = Signal(QPoint)          # 全局当前点
    dragReleased = Signal()
    nudgeRequested = Signal(int)        # -1 上移 / +1 下移

    def __init__(self, row_index: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._row_index = row_index
        self._pressed = False
        self.setFixedSize(14, 26)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip(_HANDLE_TIP)

    # ---------- 状态 ----------

    def set_row_index(self, index: int) -> None:
        self._row_index = index

    @property
    def pressed(self) -> bool:
        return self._pressed

    # ---------- 鼠标 ----------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self._pressed = True
        self.setFocus()
        # 抓取鼠标：快速拖拽时指针会离开手柄，不抓取就收不到 move 事件
        self.grabMouse()
        self.dragPressed.emit(self._row_index, event.globalPosition().toPoint())

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._pressed:
            self.dragMoved.emit(event.globalPosition().toPoint())

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if not self._pressed:
            super().mouseReleaseEvent(event)
            return
        self._pressed = False
        self.releaseMouse()
        self.dragReleased.emit()

    # ---------- 键盘 ----------

    def keyPressEvent(self, event) -> None:  # noqa: N802
        alt = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
        if alt and event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.nudgeRequested.emit(-1 if event.key() == Qt.Key.Key_Up else 1)
            event.accept()
            return
        super().keyPressEvent(event)

    # ---------- 自绘 ----------

    def paintEvent(self, _event) -> None:  # noqa: N802
        t = styles.TOKENS[styles.current_system_theme()]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(t["text3"]))
        cx, cy = self.width() / 2, self.height() / 2
        for dx in (-3.0, 3.0):
            for dy in (-5.0, 0.0, 5.0):
                painter.drawEllipse(QPointF(cx + dx, cy + dy), 1.4, 1.4)


@dataclass
class _Drag:
    """一次拖拽的运行期状态；未越过阈值前 ``started=False``。"""

    key: str
    from_index: int
    row: QWidget
    grab_y: int                       # 按下点在行内的 y 偏移（保持抓取手感）
    origin_pos: QPoint                # 行在布局中的原始位置（中止/复位用）
    target: int
    started: bool = False
    placeholder: QWidget | None = field(default=None)


class OrderEditor(QWidget):
    """可拖拽排序的列表容器：渲染交给调用方，顺序与落库由管理器托管。"""

    # 落库成功后发出新顺序，调用方据此同步其余渲染
    orderChanged = Signal(list)

    def __init__(
        self,
        build_row: "Callable[[str, QWidget], QWidget]",
        on_reorder: "Callable[[str, int], bool] | None" = None,
        parent: QWidget | None = None,
        *,
        handle_hint: str = "",
    ) -> None:
        """``handle_hint``：接在手柄悬停**操作话术后面**的一句「这个顺序意味着什么」。

        只告诉用户「能拖」是不够的 —— 他还得知道**该把想要的那个拖到前面还是后面**
        （2026-09-21 用户上报：状态机段就不知道「哪个优先显示」）。这句话只有消费方
        说得出来，通用件不认识它排的是状态机还是规则，所以由调用方给、原样接在后面。
        留空 ⇒ 只有操作话术（既有消费方零行为变更）。
        """
        super().__init__(parent)
        self._build_row = build_row
        self._on_reorder = on_reorder
        self._handle_hint = handle_hint
        self._keys: list[str] = []
        self._rows: dict[str, QWidget] = {}
        self._handles: dict[str, _DragHandle] = {}
        self._drag: _Drag | None = None
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)

    # ---------- 对外 API ----------

    def set_row_spacing(self, spacing: int) -> None:
        self._layout.setSpacing(spacing)

    @property
    def order(self) -> list[str]:
        return list(self._keys)

    def set_items(self, keys) -> None:
        """按给定顺序重建全部行（数据变了 / 拖拽落库后调用）。"""
        if self._drag is not None:
            self._abort_drag()
        self._teardown_rows()
        self._keys = list(keys)
        for i, key in enumerate(self._keys):
            handle = _DragHandle(i)
            if self._handle_hint:
                handle.setToolTip(f"{_HANDLE_TIP}\n{self._handle_hint}")
            row = self._build_row(key, handle)
            # 持有引用：手柄若未被 build_row 装进布局，没有 Python 引用会被 GC，
            # 信号连接随之失效（表现为手柄可见但拖不动）
            self._handles[key] = handle
            self._rows[key] = row
            handle.dragPressed.connect(self._begin_drag)
            handle.dragMoved.connect(self._update_drag)
            handle.dragReleased.connect(self._finish_drag)
            handle.nudgeRequested.connect(
                lambda delta, k=key: self._nudge(k, delta)
            )
            self._layout.addWidget(row)

    def reorder(self, key: str, to_index: int) -> bool:
        """把 key 移到 to_index（绝对下标），落库并同步视图。

        拖拽释放与键盘微调共用这一条路径，避免两套落库语义漂移。
        """
        if key not in self._keys:
            return False
        others = [k for k in self._keys if k != key]
        to_index = max(0, min(to_index, len(others)))
        new_order = others[:to_index] + [key] + others[to_index:]
        if new_order == self._keys:
            return False  # 位置没变：不落库、不刷 UI（避免无谓重建）
        row = self._rows[key]
        self._layout.removeWidget(row)
        self._layout.insertWidget(to_index, row)
        ok = True if self._on_reorder is None else bool(
            self._on_reorder(key, to_index)
        )
        if ok:
            self._keys = new_order
            self._reindex_handles()
            self.orderChanged.emit(list(new_order))
        else:
            # 落库失败：把行放回原位，顺序保持拖拽前
            self._layout.removeWidget(row)
            self._layout.insertWidget(self._keys.index(key), row)
        return ok

    # ---------- 拖拽流程 ----------

    def _begin_drag(self, index: int, global_pos: QPoint) -> None:
        if self._drag is not None or not (0 <= index < len(self._keys)):
            return
        key = self._keys[index]
        row = self._rows[key]
        self._drag = _Drag(
            key=key,
            from_index=index,
            row=row,
            grab_y=row.mapFromGlobal(global_pos).y(),
            origin_pos=row.pos(),
            target=index,
        )

    def _update_drag(self, global_pos: QPoint) -> None:
        d = self._drag
        if d is None:
            return
        y = self.mapFromGlobal(global_pos).y()
        if not d.started:
            # 未越过阈值：不重排、不留占位，纯点击手柄零副作用
            if abs((y - d.grab_y) - d.origin_pos.y()) < _DRAG_THRESHOLD:
                return
            self._start_drag()
        d.row.move(d.origin_pos.x(), y - d.grab_y)
        target = self._target_for(y)
        if target != d.target:
            self._layout.removeWidget(d.placeholder)
            self._layout.insertWidget(target, d.placeholder)
            d.target = target

    def _finish_drag(self) -> None:
        d = self._drag
        self._drag = None
        if d is None:
            return
        if not d.started:
            return  # 只是点了一下手柄
        self._layout.removeWidget(d.placeholder)
        d.placeholder.deleteLater()
        self._layout.insertWidget(d.target, d.row)
        self.reorder(d.key, d.target)

    def _abort_drag(self) -> None:
        """拖拽未完成时外部重建：复位视图且不落库。"""
        d = self._drag
        self._drag = None
        if d is None:
            return
        handle = self._handles.get(d.key)
        if handle is not None and handle.pressed:
            handle.releaseMouse()
            handle._pressed = False  # noqa: SLF001（同模块内复位，避免句柄卡在按下态）
        if d.started:
            self._layout.removeWidget(d.placeholder)
            d.placeholder.deleteLater()
            self._layout.insertWidget(d.from_index, d.row)
            d.row.move(d.origin_pos)

    # ---------- 内部 ----------

    def _start_drag(self) -> None:
        d = self._drag
        placeholder = QWidget(self)
        placeholder.setFixedHeight(max(d.row.height(), 1))
        self._layout.removeWidget(d.row)
        self._layout.insertWidget(d.from_index, placeholder)
        placeholder.show()
        d.row.raise_()  # 浮在其余行之上
        d.placeholder = placeholder
        d.started = True

    def _target_for(self, y: int) -> int:
        """指针 y 对应的插入下标 = 中心位于指针上方的「其余行」数量。

        被拖行已从布局中取出、由占位块顶替，故其余行的 geometry 已经含了
        空位——直接按它们的中心计数即可，无需再按原下标做偏移修正。
        """
        d = self._drag
        return sum(
            1
            for key in self._keys
            if key != d.key and y > self._rows[key].geometry().center().y()
        )

    def _nudge(self, key: str, delta: int) -> None:
        if key not in self._keys:
            return
        cur = self._keys.index(key)
        self.reorder(key, cur + delta)

    def _reindex_handles(self) -> None:
        for i, key in enumerate(self._keys):
            handle = self._handles.get(key)
            if handle is not None:
                handle.set_row_index(i)

    def _teardown_rows(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._rows.clear()
        self._handles.clear()
