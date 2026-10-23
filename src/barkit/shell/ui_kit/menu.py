"""候选菜单与行内菜单按钮。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Sequence

from PySide6.QtCore import (
    QPoint,
    QRect,
    QRectF,
    Qt,
    QSize,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QPainter,
    QPainterPath,
)
from PySide6.QtWidgets import (
    QLabel,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from barkit.shell import placement

from .base import log
from . import base
from .buttons import apply_button_tier
from .results import relay_action_result
from barkit.shell.design import metrics

if TYPE_CHECKING:
    from barkit.contribution.specs import MenuItem

class _MenuItemWidget(QWidget):
    """候选菜单里的**一项**：``label`` 主行 ＋ ``hint`` 副行（契约 §2.6 / 决议 D25）。

    为什么不用 QMenu 自带的 QAction：原生 action 只画一行文本，而 ``hint`` 承载的是
    「这一项到底是什么」——「office」只是个名字，看不出它管哪个网段、带几条路由，
    **只给名字的菜单等于没有信息**（决议 D25）。两行必须自己组合。

    H2 定稿口径是「弹层用**原生组合控件**渲染」：外壳仍是原生 ``QMenu``（圆角、阴影、
    点外部关闭、键盘导航都由窗口服务器给），项由 ``QWidgetAction`` 承载。代价是宽度
    得自己管 —— 不覆写 ``sizeHint`` 的话 QMenu 会按最窄的一行把整片压窄、hint 被截断，
    故这里按两行内容里**较宽的那一行**报宽（左侧另留 14px 给当前项标记）。
    """

    clicked = Signal()

    _PAD_X = 12
    _PAD_Y = 6
    _MARK = 14          # 左侧当前项标记的预留宽度
    _RADIUS = metrics.RADIUS_XS  # 当前项 / hover 底色圆角，与 pill-ghost 同族（metrics 单一真源）

    def __init__(
        self,
        item: "MenuItem",
        current: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._item = item
        self._current = bool(current)
        self._hover = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(self._PAD_X, self._PAD_Y, self._PAD_X, self._PAD_Y)
        lay.setSpacing(1)
        t = base._theme_tokens()
        self._fg = QColor(t["accent_text"] if self._current else t["text1"])
        self._label = QLabel(item.label, self)
        font = self._label.font()
        font.setPointSize(13)
        font.setBold(True)
        self._label.setFont(font)
        self._label.setStyleSheet(f"background:transparent; color:{self._fg.name()};")
        lay.addWidget(self._label)
        self._hint: "QLabel | None" = None
        if item.hint:
            self._hint = QLabel(item.hint, self)
            hf = self._hint.font()
            hf.setPointSize(11)
            self._hint.setFont(hf)
            self._hint.setStyleSheet(f"background:transparent; color:{t['text2']};")
            lay.addWidget(self._hint)

    def sizeHint(self) -> QSize:  # noqa: D102 —— 说明见类 docstring（H2 定稿口径）
        w = self._label.sizeHint().width()
        h = self._label.sizeHint().height()
        if self._hint is not None:
            w = max(w, self._hint.sizeHint().width())
            h += self._hint.sizeHint().height() + 1
        return QSize(w + 2 * self._PAD_X + self._MARK, h + 2 * self._PAD_Y)

    def paintEvent(self, _event) -> None:
        """自绘 hover / 当前项底色（圆角矩形，与 pill-ghost 同族）。"""
        if not (self._hover or self._current):
            return
        t = base._theme_tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(QRectF(rect), self._RADIUS, self._RADIUS)
        painter.setPen(Qt.PenStyle.NoPen)
        if self._hover:
            painter.setBrush(QColor(t["surface2"]))
            painter.drawPath(path)
        if self._current:
            # 当前项：左端一枚小竖条（不占 label 的位置，故不改匹配键）
            painter.setBrush(QColor(t["accent_text"]))
            painter.drawRoundedRect(
                rect.x() + 3, rect.y() + rect.height() // 4, 3, rect.height() // 2, 1.5, 1.5
            )
        super().paintEvent(_event)

    def enterEvent(self, event) -> None:  # type: ignore[override]
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, e) -> None:  # type: ignore[override]
        # 与 ModeBadge 同口径：鼠标捕获会把「在别处松开」也投递过来，
        # 故要求释放点仍在控件内才算一次点击。
        if e.button() == Qt.MouseButton.LeftButton:
            if self.rect().contains(e.position().toPoint()):
                self.clicked.emit()
        super().mouseReleaseEvent(e)


class CandidateMenu(QMenu):
    """候选列表弹层（H2 定稿）：原生 QMenu 外壳 ＋ ``_MenuItemWidget`` 项。

    **选中即生效** —— 跑 ``MenuItem.on_click``，无需二次确认（契约 §2.5）：
    「选中某项做什么」本来就写在各自的 ``MenuItem`` 上，再问一次是替用户做决定。

    与「弹出」解耦是刻意的：``pick(i)`` 是纯粹的「选中第 i 项」（跑回调 ＋ 记录），
    ``popup_at`` 只负责弹出。于是离屏测试能绕开窗口服务器，只验证「选中了谁、跑了什么」。

    **弹出是非阻塞的**（2026-09-17 收口）：``popup_at`` 走 ``popup()``，不开嵌套事件
    循环 —— ``exec()`` 与 ``FeedbackBar`` docstring 里那条崩溃链路同源（置顶窗上再开
    一条模态循环）。代价是它**回读不到**选中项，故「选中之后做什么」改由 ``on_picked``
    交付：``pick()`` 跑完 ``on_click`` 与 ``close()`` 后回调它，取消（点外部 / Esc）
    则一次都不调 —— 调用方不必再区分「选了个啥」和「没选」。
    """

    def __init__(
        self,
        items: "Sequence[MenuItem]",
        current: str = "",
        parent: QWidget | None = None,
        on_picked: "Callable[[MenuItem], None] | None" = None,
    ) -> None:
        super().__init__(parent)
        # 面板是 WA_TranslucentBackground 的浮层窗；QMenu 作为它的子顶层弹层会继承
        # 透明属性 ⇒ 菜单背景透明、字看不清（2026-09-18 真机）。显式关掉，并给不透明底。
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, False)
        t = base._theme_tokens()
        self.setStyleSheet(
            f"QMenu {{ background:{t['surface']}; border:1px solid {t['border']};"
            f" border-radius:{metrics.RADIUS_SM}px; padding:{metrics.GAP_XS}px; }}"
        )
        self._items = tuple(items)
        self._picked: "MenuItem | None" = None
        self._on_picked = on_picked
        for i, item in enumerate(self._items):
            row = _MenuItemWidget(item, current=item.label == current, parent=self)
            act = QWidgetAction(self)
            act.setDefaultWidget(row)
            row.clicked.connect(lambda idx=i: self.pick(idx))
            self.addAction(act)

    @property
    def items(self) -> "tuple[MenuItem, ...]":
        return self._items

    @property
    def picked(self) -> "MenuItem | None":
        """最近一次选中的项（**只读口**，与 ``FeedbackBar.text()`` 同款：测试与调用方走它）。

        ``popup_at`` 不再回读选中项，它是「选过谁」的唯一留痕；取消 / 点外部关闭不写它。
        """
        return self._picked

    def pick(self, index: int) -> "MenuItem | None":
        """选中第 ``index`` 项：跑它的 ``on_click``（选中即生效），收起弹层，再通知 ``on_picked``。

        两个回调都是**业务边界**：插件的 on_click 抛异常不该把弹层留在屏幕上，调用方的
        收尾逻辑抛异常也不该让事件循环崩掉，故各自吞掉并记日志 —— 与面板对 subtitle
        回调的处理同口径。
        """
        if not 0 <= index < len(self._items):
            return None
        item = self._items[index]
        self._picked = item
        try:
            # 以**弹层自己**为起点中继（它的父链上就是那一行的行控件 → 面板）：
            # 于是 ``MenuItem.on_click`` 保持插件原回调，值对象里不必塞捕行的闭包。
            relay_action_result(self, item.on_click)
        except Exception:  # noqa: BLE001 —— 视图边界隔离，坏回调不得卡住弹层
            log.exception("候选项 %r 的 on_click 抛异常（已记录，弹层照常收起）", item.label)
        self.close()
        # close() 在前、on_picked 在后：与原 exec() 版口径一致（调用方总是在弹层收起后才收尾），
        # 于是「收尾时再弹一个层 / 再读一次屏幕几何」这类写法不会被开着的自己挡到。
        if self._on_picked is not None:
            try:
                self._on_picked(item)
            except Exception:  # noqa: BLE001 —— 同上：调用方的收尾不得穿进 Qt 事件循环
                log.exception("候选菜单 on_picked 抛异常（已记录，弹层已收起）")
        return item

    def popup_at(self, pos, anchor_rect: "QRect | None" = None) -> None:
        """在 ``pos``（全局坐标）**非阻塞**弹出；``anchor_rect`` 给定触发控件的全局矩形时，
        把菜单**右对齐**到它（贴着触发控件、不超出面板右侧），再夹进所在屏。

        没有返回值 —— 选中项经 ``on_picked`` 交付（见类 docstring）。取消 / 点外部关闭
        时 ``on_picked`` 不被调用，调用方无需自行判空。
        """
        self.adjustSize()
        target = pos
        if anchor_rect is not None:
            w, h = self.sizeHint().width(), self.sizeHint().height()
            r = QRect(0, 0, w, h)
            # 右对齐：菜单右缘贴触发控件右缘，向下展开（与 macOS 原生下拉一致）
            r.moveTopLeft(QPoint(anchor_rect.right() - w, anchor_rect.bottom()))
            # 认屏走 ``placement`` 那一处（全仓唯一的认屏规矩）；本处只做「夹进可用区」，
            # 不另写一份「认不出屏就退回主屏」的兜底。
            screen = placement.resolve_screen(
                self,
                placement.Intent.BELOW_ICON,
                anchor=placement.Anchor.of_rect(anchor_rect),
            )
            if screen is not None:
                sg = screen.availableGeometry()
                if r.right() > sg.right():
                    r.moveRight(sg.right())
                if r.left() < sg.left():
                    r.moveLeft(sg.left())
                if r.bottom() > sg.bottom():
                    r.moveTop(sg.bottom() - h)
                if r.top() < sg.top():
                    r.moveTop(sg.top())
            target = r.topLeft()
        self.popup(target)


class MenuButton(QPushButton):
    """``MenuSpec`` 的渲染器：显示**当前项**，点击只 emit ``clicked``。

    与 ``ModeBadge`` 同款口径（H3）：控件不持有业务回调，也不自己构造菜单 ——
    「点了弹什么、选中后刷新谁」由接线方（面板）在槽里分派，控件只管显示与发信号。
    """

    def __init__(self, label: str = "", parent: QWidget | None = None) -> None:
        super().__init__(f"{label}  ▾" if label else "选择…  ▾", parent)
        self.setObjectName("pill-ghost")
        # 尺寸走与 `tool_button` 同一个出口（原先自己写 setFixedHeight(26)：既不在三档里，
        # 也没告诉 QSS 按哪档给内边距 ⇒ 自然高度 33 被裁到 26，字显示不全）。
        apply_button_tier(self, "action")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_label(self, text: str) -> None:
        """重设当前项文案（``▾`` 是「会展开」的提示，不算文案的一部分）。"""
        self.setText(f"{text}  ▾" if text else "选择…  ▾")

