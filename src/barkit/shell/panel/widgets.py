"""标题栏与行头的自绘小控件（模块级，不依赖 ``StatusPanel``）。

``shell/panel.py`` 拆包（2026-10-01）：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtCore import (
    Qt,
    QRectF,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QPainter,
)
from PySide6.QtWidgets import (
    QLabel,
    QWidget,
)

from barkit.shell.ui_kit import (
    LogoWidget,
    _theme_tokens,
    draw_badge_glyph,
    draw_glyph,
)
from barkit.contribution.specs import (
    ComponentSpec,
    IconName,
)
from barkit.shell.design.levels import as_level

from .constants import _BADGE_LEVELS, _LEVEL_TOKEN


class _ClickableLogo(LogoWidget):
    """标题栏品牌标记（U2 §10.3）：点击展开 / 收起状态区，替代旧折叠箭头。

    仅作为**空状态兜底**存在——平时有插件时标题栏只显示插件图标（用户规划，不显示
    app 图标），logo 隐藏；当 ``set_components`` 注入 0 个组件时，标题栏图标条为空、
    无折叠热区，此时才显示 logo 当唯一折叠入口（见 ``_rebuild_header`` 的可见性切换）。

    点击切换 ``_folded`` 并触发 200ms 高度动画；有插件时折叠改由插件图标承担。
    """

    def __init__(self, size: int, parent: "QWidget | None", on_toggle) -> None:
        super().__init__(size, parent)
        self._on_toggle = on_toggle
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("点击展开 / 收起状态")

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_toggle()
        super().mouseReleaseEvent(event)


class _TitleBarIcon(QWidget):
    """标题栏组件图标（U2 §5.2）：每插件一枚，画**取值图标** + **级别色**，**无角标**。

    三处显示位的分工（决议 D16）——这一处只回答「现在什么状态、多坏」：

    - 图标 = **代表态的取值图标**（解析见 ``state_view.resolve``；取值没给图标 →
      回落该状态机主图标，快照还没到时回落组件身份图标 ``ComponentSpec.icon``）；
    - 颜色 = **级别色染在图标上**（含 ok：级别色只有一个来源
      ``shell/design/levels``，不再在面板里另立色表）；
    - **不再画角标**：角标位归列表行（那里主位画身份图标、角标画取值图标），
      同一枚状态信息不重复画两遍。

    「是谁」由 hover 提示说（**组件名：副标题**，与列表行那一句逐字相同 —— 决议 D18
    「文案只有一份」），不占图标条的位置。**不逐台枚举状态机**：那是第二条文案路径，
    与列表行必然对不上（◷ 2026-09-17 用户裁定）。
    """

    clicked = Signal()

    def __init__(self, spec: "ComponentSpec", size: int = 30, parent: "QWidget | None" = None) -> None:
        super().__init__(parent)
        self._spec = spec
        self._size = size
        self._level = "idle"
        self._value_icon: "IconName | None" = None
        self._subtitle: str = ""
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(spec.name)

    def set_state(self, level: str, value_icon: "IconName | None", subtitle: str = "") -> None:
        """喂代表态：级别（决定颜色）+ 取值图标（决定画什么）+ 副标题（悬停提示）。

        副标题与列表行用的是**同一份字符串**（决议 D18）：由调用方一次算好、两处喂，
        本类不另拼一句。空串 = 这一刻还没有那句文案（如首屏还没报告），只报组件名。
        """
        self._level = level
        self._value_icon = value_icon
        self._subtitle = subtitle
        self._update_tooltip()
        self.update()

    def _update_tooltip(self) -> None:
        """悬停提示 = 「组件名：副标题」；还没有那句文案时只报组件名。"""
        self.setToolTip(
            f"{self._spec.name}：{self._subtitle}" if self._subtitle else self._spec.name
        )

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event) -> None:
        t = _theme_tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        n = self._size
        box = QRectF(2, 2, n - 4, n - 4)
        r = (n - 4) * 0.27
        # 中性圆角方块底（surface3）
        painter.setBrush(QColor(t["surface3"]))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(box, r, r)
        # 取值图标 + 级别色：颜色是这里唯一的「多坏」表达（无角标）。
        # 走统一出口 draw_glyph：按**逻辑矩形**定位（绝不用 pm.width()——dpr>1 时它
        # 返回物理像素，会把图标算偏/裁掉），dpr 由 painter 所在设备提供。
        icon = self._value_icon if self._value_icon is not None else self._spec.icon
        color = t[_LEVEL_TOKEN.get(self._level, "text1")]
        glyph = n - 14
        draw_glyph(
            painter, icon, QRectF((n - glyph) / 2, (n - glyph) / 2, glyph, glyph), color
        )


class _ValueBadge(QLabel):
    """状态行图标盒右上角的**状态角标**（决议 D16）：级别色圆盘 ＋ 白字形。

    取代原来的 10px 纯色圆点 —— 角标要说的不只是「多坏」，还有「现在什么状态」：

    - 主位图标说「**是谁**」：``ComponentSpec.icon``，固定不变；
    - 角标圆盘说「**多坏**」：级别色实心圆盘，**与菜单栏角标同语言**（D17 同款
      「色盘 ＋ 白字形」），严重度统一由圆盘颜色表达，不再压到字形颜色上；
    - 角标字形说「**什么状态**」：代表态的取值图标，走统一出口 ``ui_kit.draw_badge_glyph``
      （12 网格简化字形；取值没有专用角标字形时回落该级别的通用标记），一律白色
      （白字形在任意级别色圆盘上都清晰，深浅主题无需换色）。

    ``ok`` 与「没有状态源」不画（UI 规范 §5.3「ok 隐」：健康组件不抢眼）。
    """

    _SIZE = 16        # 角标画布（图标盒 36×36 的右上角，整个圆盘都落在盒内不被裁切）
    _PAD = 1          # 圆盘留 1px 边，字形再画在里面

    def __init__(self, parent: "QWidget | None" = None) -> None:
        super().__init__(parent)
        self.setFixedSize(self._SIZE, self._SIZE)
        self._icon: "IconName | None" = None
        self._level: "str | None" = None

    def set_badge(self, icon: "IconName | None", level: "str | None") -> None:
        """角标 = 「取值图标 ＋ 级别」两件事，缺一不可，故合成一次调用（不会画出半个角标）。"""
        self._icon = icon
        self._level = level
        self.setVisible(level in _BADGE_LEVELS)
        self.update()

    def paintEvent(self, _event) -> None:
        if self._level not in _BADGE_LEVELS:
            return
        t = _theme_tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        n = self._SIZE
        # 级别色实心圆盘（与菜单栏角标同语言：圆盘 = 有多坏，白字形 = 什么状态）。
        # 不再用卡片底色当分隔环 —— 严重度由圆盘颜色表达，原本压在字形颜色上会与菜单栏分裂。
        disc = n - 2 * self._PAD          # 圆盘直径 14，中心 (8,8)
        p.setBrush(QColor(t[_LEVEL_TOKEN.get(self._level, "text3")]))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(int((n - disc) / 2), int((n - disc) / 2), disc, disc)
        # 走统一出口 draw_badge_glyph：逻辑尺寸取**偶数** 10（圆盘 14 的 71%），
        # 逻辑落点 (16−10)/2 = 3 为整数 —— 奇数字形会让落点带 0.5px、取整方向随屏幕
        # 分辨率漂移（用户实测一块屏读「偏左」），整数落点在 1x/2x 下都精确居中；
        # dpr 由 painter 所在设备提供（Retina 下不再被放大发虚）。
        glyph = 10
        off = (n - glyph) // 2
        draw_badge_glyph(
            p,
            self._icon,
            as_level(self._level),
            QRectF(off, off, glyph, glyph),
            QColor(255, 255, 255),
        )
        super().paintEvent(_event)


class _OverflowCluster(QWidget):
    """标题栏溢出簇（U2 §5.2）：图标数超过 ``N_VIS`` 时，末端显示「+K」。

    ``K`` 为被隐藏的插件数（按 ``plugin_order`` 截断，不按问题优先）；点击在
    「截断 / 全显」之间切换（``StatusPanel._components_show_all``）；hover 出被隐
    组件名清单。中性圆角方块（与 ``_TitleBarIcon`` 同族），文字取 ``text2``。
    """

    clicked = Signal()

    def __init__(
        self,
        hidden_count: int,
        hidden_names: "list[str]",
        parent: "QWidget | None" = None,
    ) -> None:
        super().__init__(parent)
        self._hidden_count = hidden_count
        self.setFixedSize(36, 36)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        tip = "隐藏的插件：" + "、".join(hidden_names) if hidden_names else f"+{hidden_count} 个插件"
        self.setToolTip(tip)

    def set_hidden(self, hidden_count: int, hidden_names: "list[str]") -> None:
        self._hidden_count = hidden_count
        tip = "隐藏的插件：" + "、".join(hidden_names) if hidden_names else f"+{hidden_count} 个插件"
        self.setToolTip(tip)
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event) -> None:
        t = _theme_tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        n = self.width()
        box = QRectF(2, 2, n - 4, n - 4)
        r = (n - 4) * 0.27
        painter.setBrush(QColor(t["surface3"]))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(box, r, r)
        painter.setPen(QColor(t["text2"]))
        painter.setFont(QFont("", 12, QFont.Weight.Bold))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, f"+{self._hidden_count}")
