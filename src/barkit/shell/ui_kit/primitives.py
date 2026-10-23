"""通用自绘小控件：品牌标记 / 芯片 / 省略标签 / 开关 / 徽标 / 卡片 / 分隔线。"""

from __future__ import annotations

from PySide6.QtCore import (
    QRectF,
    Qt,
    QSize,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QLinearGradient,
    QPainter,
    QPaintEvent,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from barkit.shell.design import metrics
from barkit.shell.design.tokens import BRAND_GRADIENT_END, BRAND_GRADIENT_START

from .base import paint_bar_glyph
from . import base

class LogoWidget(QWidget):
    """面板头部品牌标记：渐变圆角方块 + 白色条形码（默认 26×26）。"""

    def __init__(self, size: int = 26, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)

    def paintEvent(self, _event: QPaintEvent) -> None:
        t = base._theme_tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        grad = QLinearGradient(0, 0, self.width(), self.height())
        grad.setColorAt(0, QColor(BRAND_GRADIENT_START))
        grad.setColorAt(1, QColor(BRAND_GRADIENT_END))
        painter.setBrush(grad)
        painter.setPen(Qt.PenStyle.NoPen)
        r = self.width() * 0.27
        painter.drawRoundedRect(self.rect(), r, r)
        paint_bar_glyph(painter, QRectF(self.rect()), QColor(t["on_solid"]))


class HStatChip(QLabel):
    """标题栏状态芯片：`● 状态文字`，颜色随 level 变化。

    level: ok(绿) | warn(橙) | err(红) | idle(灰)。
    """

    _LEVEL_KEY = {
        "ok": "green_text", "warn": "orange_text", "err": "red_text", "idle": "text2",
    }

    def __init__(
        self, text: str = "", level: str = "idle", parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setObjectName("hstat")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.set_state(text, level)

    def set_state(self, text: str, level: str) -> None:
        self.setText("● " + text)
        key = self._LEVEL_KEY.get(level, "text2")
        color = base._theme_tokens()[key]
        self.setStyleSheet(
            f"color:{color}; font-size:{metrics.FONT_STAT}px; font-weight:600; "
            f"padding:0; background:transparent;"
        )


#: 报宽（``ElideLabel.sizeHint``）在**完整文本宽度之上**多要的那一点余量（px）。
#:
#: 它买的不是「省略号的容身之地」，而是**两把尺子之间的取整差**：``elidedText``
#: 判定「放得下」所需的宽度可能比 ``horizontalAdvance`` 报的多 1px（实测 ASCII 与
#: CJK 混排的 ``"iPad 隧道"``：advance=57、``elidedText`` 要 58）—— 前者把字宽取整成
#: 整数，后者按实际浮点宽比。故**请求宽度**必须比 advance 宽一点，容器才可能给出
#: 够用的那一格。
#:
#: **判定侧（``_elide``）不许再减这个数**：请求宽度 = advance ＋ 余量、判定预算 =
#: 实际宽度 − 余量，两边各减一次就把余量抹平了 ⇒ 标签**拿满自己请求的宽度**仍会被
#: 打上省略号（2026-09-24 主面板实报：「iPad 隧道」显示成「iPad 隧…」，且那一行
#: **连徽标都没有** —— 用户据此怀疑「徽标占位」，实际与徽标无关）。
#: 要守的不变量只有一条：**拿满自己请求的宽度 ⇒ 完整显示**（判据见
#: ``tests/test_panel.py::test_title_given_its_own_width_is_never_elided``）。
_ELIDE_SLACK = 2


class ElideLabel(QLabel):
    """单行标签：文本超出宽度时以省略号截断（不换行）。"""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._full = text
        self.setWordWrap(False)
        # 空文本初始即隐藏：否则 QVBoxLayout 仍为其分配一行高度，
        # 导致无副标题的状态行（如「一键输入」）名称被顶到图标上方、不对齐。
        self.setVisible(bool(text))

    def setText(self, text: str) -> None:  # type: ignore[override]
        self._full = text
        self.setVisible(bool(text))
        super().setText(text)
        self._elide()

    def sizeHint(self) -> QSize:  # type: ignore[override]
        """按**完整文本**报宽 —— 不是「当前显示的那一份」。

        省略号是从**显示**里去掉装不下的部分，不该反过来变成「这一格只要这么宽」。
        报省略后的宽度会让容器**越挤越紧**：被按 0 宽结算过一次之后，sizeHint 也成了 0，
        此后给多少空间它都不要（2026-09-24 实测：主标题行里的标题整条消失 —— 布局先
        按下限 0 试算一次，文本被省略成空串，sizeHint 随即塌成 0，再也没长回来）。
        宽度该由容器给，本标签只按拿到的宽度打省略号（``_elide``）。
        空文本仍报超类的 0 宽，保持「隐藏的标签不占位」这条既有口径。

        **要守的不变量**：拿满自己请求的宽度 ⇒ **文本必须完整显示**。故报宽要带上
        ``_ELIDE_SLACK``（那 1px 取整差），而 ``_elide`` 的预算用实际宽度、**不再减它**
        —— 两边各减一次就把余量抹平，「给满了却仍打省略号」。
        """
        if not self._full:
            return super().sizeHint()
        return QSize(
            self.fontMetrics().horizontalAdvance(self._full) + _ELIDE_SLACK,
            super().sizeHint().height(),
        )

    def minimumSizeHint(self) -> QSize:  # type: ignore[override]
        """一条宽度下限：**能压到 0**。

        ``QLabel`` 默认把 ``sizeHint`` 直接当下限（不换行时）—— 而「会打省略号」的
        标签恰恰是全仓最能压的那一种，报一个按整句算的下限等于逼着容器把它撑开
        （设置页主区横向滚动是关的 ⇒ 撑开＝行尾那几枚控件被裁掉）。高度仍按超类
        （字体行高；空文本 0，隐藏的标签不占位）。
        """
        return QSize(0, super().minimumSizeHint().height())

    def resizeEvent(self, _event) -> None:  # type: ignore[override]
        super().resizeEvent(_event)
        self._elide()

    def _elide(self) -> None:
        if not self._full:
            return
        # 预算是**实际拿到的宽度**，不再减 ``_ELIDE_SLACK``：那个余量属于``sizeHint``
        # （请求侧）。判定侧跟着减一次，等于把余量退回成 0 ⇒ 容器给满也照样打省略号
        # ——「拿满自己请求的宽度 ⇒ 完整显示」这条不变量就守不住了。原因见上面那条常量。
        elided = self.fontMetrics().elidedText(
            self._full, Qt.TextElideMode.ElideRight, max(1, self.width())
        )
        if elided != self.text():
            super().setText(elided)


class Switch(QWidget):
    """iOS 风格开关（自绘）。

    本半透明无边框窗口下 QSS 无法可靠渲染圆形滑块（background-image / 渐变会被丢弃，
    控件自身 background 也不渲染），故用 QPainter 手绘圆角轨道 + 白色圆形滑块——
    与 Card / Divider 同一机制，能正常渲染。对外接口兼容 QCheckBox：
    setChecked / isChecked / clicked（clicked 仅用户点击触发，与 setChecked 解耦，
    避免「刷新 UI → setChecked → 回环」）。
    """

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._on = False
        self._pressed = False
        self._hovered = False
        self.setFixedSize(40, 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def setChecked(self, v: bool) -> None:
        self._on = bool(v)
        self.update()

    def isChecked(self) -> bool:
        return self._on

    def sizeHint(self) -> QSize:
        return QSize(40, 22)

    def enterEvent(self, e) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(e)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.update()
            # **吃掉这次按下**：同 ``ModeBadge.mousePressEvent`` 那条根因 —— 行内控件的
            # 按下若不接受，Qt 会冒泡给父级那个可点状态行，于是「翻开关」会连带把
            # 详情页也打开（层级更高，当场盖住面板）。禁用态收不到事件，不吃也没关系。
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        was = self._pressed
        self._pressed = False
        self.update()
        # ``QMouseEvent.pos()`` 在 Qt 6 已废弃（推荐 position()，返回浮点、无取整损失），
        # 且 ``self.rect().contains()`` 只接受 QPoint ⇒ 转回整型点。
        if e.button() == Qt.MouseButton.LeftButton and was and self.rect().contains(
            e.position().toPoint()
        ):
            self.clicked.emit()
        super().mouseReleaseEvent(e)

    def paintEvent(self, _event) -> None:
        t = base._theme_tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            # 禁用态：整体降透明度，避免「看起来可点实则点了没反应」的错觉
            painter.setOpacity(0.4)
        w, h = self.width(), self.height()
        r = h / 2.0
        # 轨道：开=绿 / 关=中灰；pressed 时轻微加深反馈。
        # 取 `green`（**图形档**）：白滑块与它是 3.62:1，过 1.4.11 的 3:1 —— 这也是
        # 图形档取值的**约束来源**（原先的 `#34C759` 只有 2.22，滑块糊在轨道里）。
        track = QColor(t["green"] if self._on else t["text3"])
        if self._pressed:
            track = track.darker(108)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, w, h), r, r)
        painter.setBrush(track)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPath(path)
        # 前景色圆形滑块：关=左 / 开=右（留出 2px 边距）
        knob = h - 4
        kx = 2.0 if not self._on else (w - knob - 2.0)
        painter.setBrush(QColor(t["on_solid"]))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(int(round(kx)), 2, knob, knob)


class ModeBadge(QLabel):
    """可点击圆角模式徽标（§5.3，取代写死 Badge）：自动 / 手动 / 已接管 等运行模式。

    accent 态 = accent_soft 底 + accent_text 字；neutral 态 = surface2 底 + text2 字。
    背景自绘（QPainterPath 圆角矩形，radius=8，与 pill-ghost 同族）确保 macOS 圆角生效；
    文字由 QLabel 居中绘制。``set_state(label, accent)`` 重设。

    **点击只发信号，不持有回调**（H3）：``clickable=True`` 时手型光标、左键释放 emit
    ``clicked``；「点了做什么」由接线方（面板）在槽里分派。此前徽标自己在
    ``mouseReleaseEvent`` 里调 ``on_click`` ⇒ ``clicked`` 从不 emit ⇒ 面板里接到它的处理
    函数是死代码，且第二次点击后才的内容无人刷新。
    """

    clicked = Signal()

    def __init__(
        self,
        label: str = "",
        accent: bool = True,
        clickable: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._accent = bool(accent)
        self._clickable = bool(clickable)
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if clickable
            else Qt.CursorShape.ArrowCursor
        )
        self.setFixedHeight(metrics.MODE_BADGE_H)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = self.font()
        font.setPointSize(11)
        font.setBold(True)
        self.setFont(font)
        self.set_state(label, accent)

    @property
    def accent(self) -> bool:
        """当前是否 accent 态（候选列表选中后要保留原态时读它，不必碰私有字段）。"""
        return self._accent

    def set_state(self, label: str, accent: bool) -> None:
        """重设模式（label 文案 + accent 态）。"""
        self._accent = bool(accent)
        self.refresh_theme()
        self.setText(label)

    def refresh_theme(self, *_args) -> None:  # noqa: D401
        """主题切换时重设烤色（accent=accent_soft 底；neutral=surface2 底）。"""
        t = base._theme_tokens()
        if self._accent:
            self._bg = QColor(t["accent_soft"])
            self._fg = QColor(t["accent_text"])
        else:
            self._bg = QColor(t["surface2"])
            self._fg = QColor(t["text2"])
        self.setStyleSheet(
            f"background:transparent; color:{self._fg.name()}; padding:0 {metrics.ICON_CHIP_PAD_X}px;"
        )
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(QRectF(rect), 8, 8)
        painter.setBrush(self._bg)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPath(path)
        super().paintEvent(_event)  # QLabel 绘制居中文字（color 来自 QSS）

    def mousePressEvent(self, e) -> None:  # type: ignore[override]
        """**可点时必须吃掉这次按下** —— 否则点徽标会连带触发整行的点击。

        根因（2026-09-18 实验证实，见 tests/test_panel.py 的冒泡用例）：本类是 ``QLabel``，
        而 ``QLabel`` 默认**忽略**鼠标按下；Qt 于是把这次按下**冒泡给父级**，而本徽标通常
        长在一行**可点的状态行**（``HoverRow``）里 —— 那一行的整行点击（打开详情页）就跟着
        跑了。后果不是「多一个动作」而是**把徽标本来要做的事盖掉**：详情页层级更高，
        弹出来的候选菜单被压在下面 / 随之关闭，用户看到的就是「点了徽标，什么都没弹」。

        只在**可点**时吃掉：不可点的徽标仍按原样冒泡（那时点它就是点那一行）。
        """
        if self._clickable and e.button() == Qt.MouseButton.LeftButton:
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e) -> None:  # type: ignore[override]
        # 只 emit：响应什么由接线方在槽里决定（H3）。
        # 鼠标捕获会把「在别处松开」也投递过来，故要求释放点仍在控件内才算一次点击。
        if self._clickable and e.button() == Qt.MouseButton.LeftButton:
            pos = e.position().toPoint()
            if self.rect().contains(pos):
                self.clicked.emit()
        super().mouseReleaseEvent(e)


class Card(QWidget):
    """自绘圆角卡片：背景与边框由 QPainter 绘制，确保圆角在 macOS 上一定生效。

    QSS 的 border-radius / background 对普通 QWidget 在 macOS 上**不裁剪背景**
    （autoFillBackground 填的是矩形），所以用 QSS 永远得到直角矩形。这里用
    QPainterPath.addRoundedRect 自绘，圆角与描边都可靠。

    **外观今天只有三样：实心 surface ＋ 1px border ＋ 圆角**（圆角值由调用方给，设置页
    分区卡取 ``metrics.RADIUS_SM`` 微圆角）。曾经有过一个 ``glass=True`` 模式
    （Phase 1 玻璃 A），2026-09-21 整体删除 —— 删它是量出来的，不是审美取舍：

    - 那层软投影（``QGraphicsDropShadowEffect``）是用户唯一看得见、且判为「非常丑」的
      部分，而它每帧还要把整棵子树重画进一张离屏位图再合成；
    - 半透明面压在设置页那层**不透明**的 ``surface`` 上等于同色混合 —— 真机逐点实测
      卡中心只差 3/255（``#FCFCFC`` vs ``#FFFFFF``）；内描边高光是白压白，看不见；
    - 大圆角 20 与卡片自己的 1px 描边、窗口的 14 圆角三层不同心。

    于是 ``Card`` 在任何平台、任何会话下都画得**一模一样** —— 不再有「离屏是实心、
    真机才是玻璃」这种只能靠真机目测的差别。判据见
    `docs/log/2026-09-21-设置窗观感返工.md`。
    """

    def __init__(
        self,
        radius: int = metrics.RADIUS_LG,
        parent: QWidget | None = None,
        *,
        bg_token: str = "surface",
        border_token: str = "border",
    ) -> None:
        super().__init__(parent)
        self._radius = radius
        # 默认不缓存颜色：paintEvent 实时读主题令牌，主题切换后重绘即生效。
        # 只有调用 set_theme_colors（如熔断卡片的红底）才进入「自定义色」模式。
        self._custom_colors = False
        self._bg: QColor | None = None
        self._border: QColor | None = None
        # 底色 / 描边各取哪个**令牌**（不是色值 —— 色值只能住在 tokens.py）。
        # 默认与设置页同底（surface），只靠 1px 描边区分；高一层的容器（如编辑屏
        # 那块面板）传 ``bg_token="surface2"``：页面是 surface，面板比它深一档才有
        # 「这是一块浮上来的面」的读法（同底 = 只有一条框，读起来还是散件）。
        self._bg_token = bg_token
        self._border_token = border_token

    def set_theme_colors(self, bg: str, border: str) -> None:
        """设置自定义背景/描边色（如熔断卡片红底），此后不再随主题自动变化。"""
        self._custom_colors = True
        self._bg = QColor(bg)
        self._border = QColor(border)
        self.update()

    def _resolve_colors(self) -> tuple[QColor, QColor]:
        if self._custom_colors:
            return self._bg, self._border  # type: ignore[return-value]
        t = base._theme_tokens()
        return QColor(t[self._bg_token]), QColor(t[self._border_token])

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg, border = self._resolve_colors()
        # **必须是 QRectF**：``QRect.adjusted(0.5, …)`` 会把 0.5 截成 0，1px 的笔尖
        # 一半画到控件外被裁掉 ⇒ 直边只剩半像素，而圆角处被反锯齿补回来 ⇒ 同一条
        # 边两种粗细（2026-09-21 用户上报「圆角线条宽度与其他地方不一致」，实测
        # 直边 #f1f1f4、圆角 #e5e5ea）。内缩半像素后整条描边落在控件内，处处同宽。
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, self._radius - 0.5, self._radius - 0.5)
        painter.setBrush(bg)
        painter.setPen(QPen(border, 1))
        painter.drawPath(path)


class Divider(QWidget):
    """自绘 1px 分割线（横向铺满父布局）。

    QSS 的 QLabel background 在半透明无边框浮层上对 1px 控件经常不绘制，
    故改为自绘，确保与标题下的横线对称可见。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(1)

    def paintEvent(self, _event) -> None:
        t = base._theme_tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # 实色 border（**不淡化**）：它今天主要在设置页分区卡的「标题与内容之间」当结构
        # 分隔线用，淡一半就到了「看不出这里有条线」的程度。Phase 1 玻璃期那层 0.5 alpha
        # 是给半透明表面减噪的，玻璃一并删掉后它没有存在理由了。
        painter.fillRect(0, 0, self.width(), 1, QColor(t["border"]))


class EmptyState(QWidget):
    """「这里现在没有内容」的**唯一说法** —— 空列表 / 无搜索结果 / 未识别时占位。

    **为什么收进组件盒**（2026-10-16）：同一件事在四处各写了一遍，且四种写法互不相同 ——
    ``shell/settings/search.py:57``（一条隐藏的 ``note`` 改 ``setVisible``）、
    ``plugins/todo/clock_view.py:608``（把整块行容器 ``hide()``，页面上什么都不留）、
    ``plugins/recognize/content_shell.py:156``（常驻一枚 ``QLabel("未识别")``）、
    ``shell/state_machines_section.py:335``（占位文案 ＋ ``setToolTip``）。

    形态是**居中两行**：一句主文案（``placeholder`` 档，14px 灰）＋ 可选一句副文案
    （``caption`` 档，11.5px 更淡）。副文案用来给「怎么办」，例如「换个说法试试」——
    只给主文案时它自己隐藏，不留一行空高度。

    **不管显示与隐藏**：本件只画那一块，什么时候隐去归调用方（列表有内容时隐藏是调用方
    的判断 —— 组件盒看不到那个列表）。同理不管位置：放进哪一列、要不要 ``stretch``
    都由调用方决定。

    竖直方向**不设伸展策略**：按内容高度。空状态块被布局拉成一大块空白，会把页面顶部的
    内容挤到看不见，而它自己什么都没多说一句。
    """

    def __init__(
        self, text: str = "", hint: str = "", parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        col = QVBoxLayout(self)
        col.setContentsMargins(0, metrics.GAP_LG, 0, metrics.GAP_LG)
        col.setSpacing(metrics.GAP_XS)
        self._title = QLabel(text, self)
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title.setWordWrap(True)
        base.apply_text_role(self._title, "placeholder")
        col.addWidget(self._title)
        self._hint = QLabel(hint, self)
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint.setWordWrap(True)
        base.apply_text_role(self._hint, "caption")
        self._hint.setVisible(bool(hint))
        col.addWidget(self._hint)

    def set_text(self, text: str) -> None:
        """换主文案（同一块占位换个说法复用，如「没有匹配的条目」⇄「还没有条目」）。"""
        self._title.setText(text)

    def set_hint(self, text: str) -> None:
        """换副文案；传空串即收起来（不留空高度）。"""
        self._hint.setText(text)
        self._hint.setVisible(bool(text))

    def text(self) -> str:
        """当前主文案（判据读口，测试与调用方都用它）。"""
        return self._title.text()

    def hint_text(self) -> str:
        """当前副文案（同上）。"""
        return self._hint.text()

