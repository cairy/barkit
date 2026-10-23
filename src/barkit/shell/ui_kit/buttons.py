"""按钮档位与统一按钮构造函数。"""

from __future__ import annotations

from PySide6.QtCore import (
    QRectF,
    QSize,
    Qt,
)
from PySide6.QtGui import (
    QColor,
    QIcon,
    QPainter,
    QFontMetrics,
)
from PySide6.QtWidgets import (
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QWidget,
)

from barkit.shell.design import metrics


class _GlyphLabelButton(QPushButton):
    """「图标 ＋ 文字」按钮：两者的垂直排布由**自己画**，不交给 style（2026-10-13）。

    面板 A 区标签态快捷按钮是全仓唯一走 QPushButton 内置 icon+text 的地方，
    用户在 macOS 上看到的是**底部对齐**（图标底边贴文字基线）—— QMacStyle
    忠实模仿原生 NSButton 的排布。``toolButtonStyle`` 那条 remedie 无效：
    QPushButton 的 getter 读成员变量，``setProperty`` 设的是另一个 dynamic
    property，渲染时不读它（2026-10-13 实测为恒等操作）。

    故覆写 ``paintEvent``：**背景 / 边框 / 圆角 / hover / pressed / disabled
    态仍全权交给 QSS 与 style**（把 icon/text 从 option 里摘掉再
    ``drawControl``，style 只画按钮本体）；图标与文字的位置在本方法里算 ——
    同一行、各自垂直居中。内容区取 ``SE_PushButtonContents``：QSS 的 padding
    由 style 反映在那里，这里不第二份复刻。文字色取 ``opt.palette`` 的
    ButtonText（QSS 的 ``color:`` 落在这个 role 上，浅 / 深 / 禁用三态都跟）；
    图标与文字**同源取色**，不会出现「文字跟了 QSS、图标停在旧色」的分叉。

    约束（面板测试钉死的，自绘两条都不碰）：``text()`` 与 ``icon()`` 必须
    原样留着（「放得下就还标签」那条门禁同时断言两者），且 ``sizeHint`` 不许
    变（A 区宽度裁决吃它）——``initStyleOption`` 照旧带全量 icon/text 给
    style 算尺寸，自绘只改**画法**、不改**量法**。

    单元素（纯文字 / 纯图标）时没有「两个元素互相找齐」的问题，原样交回
    ``super().paintEvent`` —— style 本来就居中。
    """

    _GLYPH = 15   # 字形逻辑边长（A 区标签态按钮的既有配比）

    def __init__(
        self,
        text: str,
        icon=None,
        *,
        text_role: str = "ghost",
        glyph_role=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(text, parent)
        # text_role：文字前景色来源。``"ghost"`` = 沿用 pill-ghost 灰字三态（A 区默认，
        # 行为不变）；``"on_solid"`` 等 = 直接取 token 对应色（彩色底按钮白字，见
        # 2026-10-14 daemon_ops 修复）。
        # glyph_role：图标前景色来源。``None`` = 跟随文字色（A 区图标与文字同色）；
        # 否则取指定 token 角色（如守护成功态 ``"green_text"``）。
        self._icon = icon
        self._text_role = text_role
        self._glyph_role = glyph_role
        self._bake_icon()

    def _bake_icon(self) -> None:
        """把字形烤成位图供 ``icon()`` / sizeHint 读数，但**不参与绘制**。

        绘制由 :meth:`paintEvent` 按 ``_glyph_color`` 自绘；位图只烤静止档 text2，
        避免谁读 ``icon()`` 取到旧色（同构造期原逻辑）。
        """
        if self._icon is None:
            self.setIcon(QIcon())
            return
        from barkit.shell import styles
        from barkit.shell.ui_kit import glyph_pixmap

        t = styles.TOKENS[styles.current_theme()]
        self.setIcon(QIcon(glyph_pixmap(self, self._icon, self._GLYPH, t["text2"])))
        self.setIconSize(QSize(self._GLYPH, self._GLYPH))

    def set_glyph(self, icon, role=None) -> None:
        """事后增 / 撤图标（守护按钮装成功才出现对勾，卸载再撤）。

        ``role`` 为图标色角色（如 ``"green_text"``）；``None`` ⇒ 跟随文字色。
        撤图标传 ``icon=None`` 即可。
        """
        self._icon = icon
        self._glyph_role = role
        self._bake_icon()
        self.update()

    def clear_glyph(self) -> None:
        """撤掉图标，回到纯文字态。"""
        self.set_glyph(None)

    def paintEvent(self, event) -> None:
        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        if opt.icon.isNull() or not opt.text:
            super().paintEvent(event)
            return
        body = QStyleOptionButton(opt)
        body.icon = QIcon()
        body.text = ""
        p = QPainter(self)
        self.style().drawControl(
            QStyle.ControlElement.CE_PushButton, body, p, self
        )
        cr = self.style().subElementRect(
            QStyle.SubElement.SE_PushButtonContents, body, self
        )
        # 文字色与图标色各自取来源：A 区二者都走 ghost 灰字（原行为），
        # 守护成功态图标走 green_text、reinstall 文字走 on_solid 白字。
        fg = _glyph_text_color(opt, self._text_role)
        glyph_fg = _glyph_color(opt, self._glyph_role)
        fm = QFontMetrics(self.font())
        icon_w = opt.iconSize.width()
        icon_h = opt.iconSize.height()
        gap = metrics.GAP_SM
        text_w = fm.horizontalAdvance(opt.text)
        total = icon_w + gap + text_w
        x = cr.left() + max(0, (cr.width() - total) // 2)
        # 图标：draw_glyph 是自绘唯一出口（按逻辑 rect 定位、dpr 取自 painter）
        from barkit.shell.ui_kit import draw_glyph

        icon_rect = QRectF(
            x,
            cr.top() + (cr.height() - icon_h) / 2,
            icon_w,
            icon_h,
        )
        draw_glyph(p, self._icon, icon_rect, glyph_fg)
        p.setPen(fg)
        p.setFont(self.font())
        p.drawText(
            QRectF(
                x + icon_w + gap,
                cr.top(),
                text_w + 2,
                cr.height(),
            ),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            opt.text,
        )
        p.end()


def _ghost_fg(opt: QStyleOptionButton) -> QColor:
    """``pill-ghost`` 档的前景色（字形与文字**同一个源**）。

    与 ``styles.py`` 的 ``QPushButton#pill-ghost`` 三条规则逐条对齐：
    静止 ``text2`` ／ hover·pressed ``text1`` ／ disabled ``text3``。

    **为什么不读 ``opt.palette``**：QSS 的 ``color:`` 只对 style 的原生绘制
    生效，``:hover`` 档更是**临时应用、不写回 widget palette** —— 2026-10-13
    实测：静止态读 ``palette.ButtonText`` 确能拿到 text2（QSS 非 hover 规则在
    polish 时写进去了），但 hover 时它**不会跟着升 text1**。自绘要完整跟随
    QSS 的三态，只能**现问** tokens —— 与 ``_GlyphButton`` 位图重烤同一个口径
    （取数口一律「现问」，见项目纪律）。

    此前 A 区自绘读 palette、C 区位图固定烤 text1 ⇒ 静止态两区图标色**不一致**
    （用户 2026-10-13 实机上报）；现在两区都走本函数，同源同档。
    """
    from barkit.shell import styles

    t = styles.TOKENS[styles.current_theme()]
    state = opt.state
    if not (state & QStyle.StateFlag.State_Enabled):
        return QColor(t["text3"])
    if state & (QStyle.StateFlag.State_MouseOver | QStyle.StateFlag.State_Sunken):
        return QColor(t["text1"])
    return QColor(t["text2"])


def _glyph_text_color(opt: QStyleOptionButton, role: str) -> QColor:
    """自绘按钮的**文字**前景色：按按钮自身样式取，不再硬写 pill-ghost 灰字。

    ``"ghost"`` ⇒ 沿用 :func:`_ghost_fg`（pill-ghost 三态 text2/1/3，A 区默认行为）；
    其余角色（如 primary 的 ``"on_solid"``）直接取 token 对应色 —— 彩色底按钮的
    文字是白字，若仍按灰字画会看不清（2026-10-14 daemon_ops 修复）。禁用态一律
    ``text3``，与原生 disabled 灰字一致。
    """
    from barkit.shell import styles

    t = styles.TOKENS[styles.current_theme()]
    if role == "ghost":
        return _ghost_fg(opt)
    if not (opt.state & QStyle.StateFlag.State_Enabled):
        return QColor(t["text3"])
    return QColor(t[role])


def _glyph_color(opt: QStyleOptionButton, role) -> QColor:
    """自绘按钮的**图标**前景色。

    ``None`` ⇒ 跟随文字色（A 区图标与文字同色）；否则取指定 token 角色
    （如守护成功态 ``"green_text"``，即使禁用也保持绿，与旧实现一致）。
    """
    if role is None:
        return _glyph_text_color(opt, "ghost")
    from barkit.shell import styles

    t = styles.TOKENS[styles.current_theme()]
    return QColor(t[role])

def apply_button_tier(btn: QPushButton, size: str = "action") -> int:
    """给按钮定档：写 ``tier`` 动态属性 ＋ 钉高度，返回该档的高度。

    **两个出口共用这一处**：``tool_button``（本模块）与 ``MenuButton``（QPushButton 子类，
    面板的 ``MenuSpec`` 直接构造它、绕不过 ``tool_button``）。此前 ``MenuButton`` 自己写
    ``setFixedHeight(26)`` —— 既不在三档里，又没告诉 QSS 该按哪档给内边距，于是它
    自然高度 33 被裁到 26。档位两条轴（属性 + 高度）必须一起写，所以合成一个函数。

    传一个没登记的档名直接报错，别让笔误静默变成第四个档位。

    注意：本函数必须在控件**首次 polish 之前**调用（`tool_button` / `MenuButton` 的构造期
    就是）。QSS 的属性选择器匹配结果会被缓存：控件显示过后再改 `tier`，不 unpolish /
    polish 就仍按旧档画（与 `_apply_chip` 改 objectName 后必须重刷是同一条纪律）。
    """
    try:
        height = metrics.BUTTON_HEIGHTS[size]
    except KeyError:
        raise ValueError(
            f"未知按钮档位：{size!r}；合法值 {sorted(metrics.BUTTON_HEIGHTS)}"
        ) from None
    btn.setProperty(metrics.TIER_PROPERTY, size)
    btn.setFixedHeight(height)
    return height


def tool_button(
    text: str,
    kind: str = "pill-ghost",
    *,
    size: str = "action",
    expanding: bool = False,
    parent: QWidget | None = None,
) -> QPushButton:
    """构造一个统一的交互按钮——所有窗口复用，避免样式漂移。

    集中处理「交互按钮」的控件级属性，取代各窗口手写
    ``setObjectName`` / ``setFixedHeight`` / ``setCursor`` 的重复块
    （此前识别窗口、设置窗就漏了手型光标，与面板 hover 不一致）：

    - ``objectName``: ``primary`` / ``pill-ghost``，视觉由全局 QSS(styles.py) 定义；
    - 尺寸：按 ``size`` 定档 —— 高度查 :data:`metrics.BUTTON_HEIGHTS`（**调用方不报数字**），
      同时把档名写进 ``tier`` 属性，**字号与内边距由 QSS 按档给**（见 `apply_button_tier`）。
      只钉高度、不告诉 QSS 那一档该用多大的字，就是「按钮显示不全」的成因；
    - 手型光标：所有可点按钮统一 ``PointingHandCursor``（与面板一致）；
    - ``expanding``: 是否占满可用宽度（操作区主按钮用）。

    **为什么高度改成档位（2026-09-21）**：原先这里是个 ``height: int = 30`` 的自由参数，
    于是同一个「次要动作」在仓里长出了 30 / 28 / 26 / 24 / 20 五种高度 —— 默认值与多数页
    实际用的值都不一致。现在三个角色写在 ``metrics``（单一真源），传一个没登记的名字
    直接报错，别让笔误静默变成第四个档位。门禁 ``test_no_button_height_magic_numbers``
    守「调用方不再传 ``height=``」。

    视觉样式（颜色 / hover / 圆角）一律在 ``styles.py`` 的 QSS 里定义，
    这里只管控件级通用属性——二者职责分离、单一来源，改一处即全局生效。
    """
    btn = QPushButton(text, parent)
    _decorate_button(btn, kind, size, expanding)
    return btn


def _decorate_button(
    btn: QPushButton,
    kind: str,
    size: str = "action",
    expanding: bool = False,
) -> None:
    """把 :func:`tool_button` 那套控件级属性刷到一枚**已经存在**的按钮上。

    「造一个」的口是 :func:`tool_button`，「造子类」的口是 :class:`_GlyphButton`
    （面板底部栏那两枚烤色图标按钮）—— 二者共用这一处，否则又会长出第二份
    ``setObjectName`` / ``apply_button_tier`` / 手型光标的代码，也就是
    :func:`tool_button` docstring 里那段「五种高度」的同一类病再犯一次。

    **必须在控件首次 polish 之前调用**（与 :func:`apply_button_tier` 同一个理由：
    QSS 的属性选择器匹配结果会被缓存，显示过之后再改就白改）。

    Args:
        btn: 待装饰的按钮（``QPushButton`` 或其子类）。
        kind: ``objectName``，视觉由全局 QSS 定义（``primary`` / ``pill-ghost`` / …）。
        size: 档位名（``metrics.BUTTON_HEIGHTS`` 查表，未知直接报错）。
        expanding: 是否占满可用宽度（操作区主按钮用）。
    """
    btn.setObjectName(kind)
    apply_button_tier(btn, size)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if expanding:
        btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

