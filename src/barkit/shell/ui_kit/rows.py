"""记录行骨架（面板行与设置页行的唯一长相）。"""

from __future__ import annotations

from PySide6.QtCore import (
    QEvent,
    QRectF,
    Qt,
)
from PySide6.QtGui import (
    QColor,
    QPainter,
    QPainterPath,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from barkit.contribution.specs import IconName, as_action_result

from .base import apply_text_role, glyph_pixmap
from . import base
from .primitives import ElideLabel
from barkit.shell.design import metrics
from .results import defer_result_to_host

class HoverRow(QWidget):
    """状态行：可点击行或展示行，hover 均填充 accent_soft 紫色底色。

    三行（一键输入 / 通知守卫 / 网络切换）hover 视觉统一为紫色，避免权重不一
    显得不一致；可点击性改用**光标**区分：
    - clickable=True（如“一键输入”）：手型指针 + 响应点击（整行输入）。
    - clickable=False（如“通知守卫 / 网络切换”展示行）：箭头指针、不响应点击，
      真正的操作在行内开关 / 副文本。
    """

    def __init__(
        self,
        on_click,
        combo: QWidget | None = None,
        clickable: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("status_row")
        #: **插件回调本体**，不是「回本行的闭包」—— 控件攥着自己的回调是没必要的环，
        #: 卫生口径见 `relay_action_result`（那一带有尚未定位的离屏崩溃，别当已证结论读）。
        self._on_click = on_click
        self._combo = combo
        self._clickable = clickable
        self._hovered = False
        self._pressed = False
        if clickable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        # 监听自身与行内子控件（开关 / 下拉 / 文本）的进入/离开，
        # 实现“整行任意位置 hover 都亮、且离行即灭、无残留”的可靠效果。
        self.installEventFilter(self)

    def enterEvent(self, event) -> None:  # type: ignore[override]
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        # 离行即灭：无论移出到间隙、其它行还是子控件外，都直接熄灭。
        # （停在子控件上时本行不会收到 leaveEvent，且子控件的 Enter 会经
        #  eventFilter 重新点亮本行，见下方。）
        self._hovered = False
        self._pressed = False
        self.update()
        super().leaveEvent(event)

    def eventFilter(self, obj, event) -> bool:
        # 行内任意子控件（开关 / 下拉 / 文本标签）被进入时，保持整行 hover 亮起；
        # 子控件离开不在此熄灭——真正离开整行时本行 leaveEvent 会熄灭。
        if obj is not self and isinstance(obj, QWidget):
            if event.type() == QEvent.Type.Enter:
                self._hovered = True
                self.update()
        return super().eventFilter(obj, event)

    def childEvent(self, event) -> None:
        # 行内子控件是构造后通过 layout 加入的，故在 ChildAdded 时追加上事件过滤器。
        if event.type() == QEvent.Type.ChildAdded:
            child = event.child()
            if isinstance(child, QWidget):
                self._install_hover_filter(child)
        super().childEvent(event)

    def _install_hover_filter(self, widget) -> None:
        widget.installEventFilter(self)
        for sub in widget.findChildren(QWidget):
            sub.installEventFilter(self)

    def paintEvent(self, event) -> None:
        if self._hovered:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            t = base._theme_tokens()
            rect = self.rect().adjusted(1, 1, -1, -1)
            path = QPainterPath()
            path.addRoundedRect(QRectF(rect), 10, 10)
            # 三行 hover 统一为紫色 accent_soft；可点击性由光标（手型/箭头）区分
            painter.setBrush(QColor(t["accent_soft"]))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawPath(path)
        super().paintEvent(event)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if not self._clickable:
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.update()
        # 同 Switch.mouseReleaseEvent：用 position()（Qt 6 推荐）替代废弃的 pos()。
        #
        # ``combo`` 这条排除分支**至今没有任何生产调用方**（2026-09-18 核实，任务板 §5 #29
        # P0 ③）：面板里的「下拉」是 ``MenuSpec`` → ``MenuButton``（``QPushButton`` 自己吃掉
        # 按下，不冒泡），设置页的 ``ProfileSelector`` / ``ChoiceSelector`` 不在可点行里、
        # 且 autotype 那把真的长在行内的 profile 下拉已由用例证明**不穿透**。留着它是因为：
        # 将来若把某个**不吃按下**的选择器放进行里，这里是唯一该拦的地方。
        combo_geo = self._combo.geometry() if self._combo is not None else None
        if combo_geo is not None and combo_geo.contains(event.position().toPoint()):
            return  # 让下拉框自行处理
        # 存的是**插件回调本体**，不是「回本行的闭包」—— 见 `relay_action_result`。
        #
        # 整行点击**晚一拍**才回访宿主（与行内控件、候选菜单项的「当场回访」不同）：
        # 2026-09-18 实测「在 ``mousePressEvent`` 这个 Python 覆盖里**当场**回访宿主」会让
        # 本机离屏全量 pytest 崩在 access violation（根因未定位，已缩到 0.6 秒两条用例的
        # 复现，见 `docs/log/2026-09-18-动作结果反馈机制.md`）。那里的取证结论是「换等价写法
        # 只是掷骰子」，故本轮改的是**时机**而不是写法：回调本身仍在按下时**同步**跑完
        # （用户不该为「报结果」多等一拍），只把「回访宿主」挪到事件循环下一拍 ——
        # 那时控件树已结算完，且回访前先判活（见 `defer_result_to_host`）。
        if self._on_click is not None:
            defer_result_to_host(self, as_action_result(self._on_click()))
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        self._pressed = False
        self.update()
        super().mouseReleaseEvent(event)


#: 行首**图标盒的三档配色**（底 / 字形各取一枚令牌）：给「这条打哪儿来」这类
#: **出处**上色用（2026-09-24「用图标颜色区分来源」）。
#:
#: 只用「中性 → 品牌浅底 → 品牌实底」这条**浓淡**梯度，**不碰 green / orange / red** ——
#: 那三枚在本仓是**状态**语言（成功 / 警告 / 失败，见 tokens 的四档表），拿来标出处
#: 会被读成「这一条有什么问题」（与来源徽标取中性色同一条理由）。
#: 三档要的是「一列扫下来能分出组」，不是「哪一条更重要」。
ICON_TONES: dict[str, tuple[str, str]] = {
    "neutral": ("surface3", "text2"),
    "accent": ("accent_soft", "accent_text"),
    "solid": ("accent", "on_solid"),
}


#: 没给 ``icon_tone``（或给了个不认识的名字）时用的那一档 —— 与骨架 2026-09-24 之前
#: 的固定配色一致，故面板那两种行不受影响。
ICON_TONE_DEFAULT = "accent"


class RecordRow(HoverRow):
    """列表里**一条记录**的行骨架（全仓唯一一份）。

    四格，从左到右：

    - **图标盒**（36×36，accent 浅底 + 22px 字形，圆角 8）——「是谁」；
    - **主标题行**：标题 ＋ 一枚徽标（徽标由调用方造，骨架只管它落在哪一格）；
    - **副标题**：单行省略（``ElideLabel``），全文进悬停提示；
    - **尾部控件槽**：药丸 / 开关 / 按钮，调用方按 ``add_trailing`` 的顺序追加。

    为什么要有这一份：同一条骨架此前在**面板状态行**（``panel._StatusRowView``）与
    **面板自绘行**（``StatusRow``）里各写了一遍，任一处改动都会让同一个列表里的两种行
    长得不一样（2026-09-20 实修过一回：自绘行 30 盒 / 16px / 左对齐，框架行 36 盒 /
    22px / 居中，界面上表现为那一行图标小一圈且贴左）。设置页的「快捷键」列表是第三个
    用户 —— 三处同源，改骨架即三处一起改。

    **标题与副标题都必须可压**，这不是可选优化：两个都是长句（组件名 ＋ 一句说明），
    而设置页主区横向滚动是**关**的（``settings._scrolled``）⇒ 不可压的标签会把整页顶宽，
    直接后果是行尾那枚「初始化」被裁掉、且没有滚动条可救。故两者一律 ``ElideLabel``
    ＋ ``Ignored`` 横向策略：窄了自己打省略号让位，全文在悬停提示里。

    换肤归 ``refresh_theme``（图标盒烤色 ＋ 重画位图 ＋ 副标题角色 ＋ 徽标），面板在
    主题切换时按名字统一调用；需要额外刷自绘件（状态角标 / 行内下拉）的子类覆盖它、
    末尾补自己的那几行（先调 ``super()``）。
    """

    #: 图标盒边长与盒内字形尺寸（px）。这两个数只留在这里：面板行与自绘行曾经各写
    #: 一份（30/16 与 36/22），同一个列表里就能看出两种体积。
    ICON_BOX = 36
    ICON_GLYPH = 22

    def __init__(
        self,
        *,
        title: str = "",
        subtitle: str = "",
        icon: "IconName | None" = None,
        icon_fallback: "IconName | None" = None,
        badge: QWidget | None = None,
        on_click=None,
        clickable: bool = False,
        icon_tone: str = ICON_TONE_DEFAULT,
        margins: "tuple[int, int, int, int]" = (14, 8, 14, 8),
        extra_refresh=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(on_click=on_click, clickable=clickable, parent=parent)
        #: 图标**名字**（图标那张画布是 ``self._ico``）。空 = 盒里留空，要不要兜一枚
        #: 占位图标由 ``icon_fallback`` 声明（面板给通用齿轮：每个组件都有一行，
        #: 空图标位读起来像坏了）。
        self._icon_name: "IconName | None" = icon
        self._icon_fallback: "IconName | None" = icon_fallback
        #: 图标盒取哪一档配色（见 ``ICON_TONES``）。**只影响颜色，不影响算子** ——
        #: 「是哪一类来的」由调用方用颜色表达，「具体是谁」靠图标本身。
        self._icon_tone = icon_tone if icon_tone in ICON_TONES else ICON_TONE_DEFAULT
        #: 图标盒当前该用的**字形色**（``_init_skin`` 里按档算出来，``_paint_icon`` 用）
        self._icon_fg = base._theme_tokens()[ICON_TONES[self._icon_tone][1]]
        self._extra_refresh = extra_refresh

        row = QHBoxLayout(self)
        row.setContentsMargins(*margins)
        row.setSpacing(10)
        self._row_layout = row

        # 图标盒分两层：外层透明外壳、内层那张 accent 浅底 / 圆角 8 的画布。分成两层
        # 是为了「咬角」—— 面板的状态角标要压在盒的右上角上（角标挂整行、按盒的坐标
        # 定位，见 ``panel._StatusRowView._position_badge``），同尺寸两层谁也不裁谁。
        box = QWidget(self)
        box.setFixedSize(self.ICON_BOX, self.ICON_BOX)
        box.setStyleSheet("background:transparent;")
        self._ico_box = box
        self._ico = QLabel(box)
        self._ico.setFixedSize(self.ICON_BOX, self.ICON_BOX)
        box_layout = QHBoxLayout(box)
        box_layout.setContentsMargins(0, 0, 0, 0)
        box_layout.addWidget(self._ico)
        row.addWidget(box)

        # 主标题行（标题 ＋ 徽标）＋ 副标题，竖向堆叠。容器名 ``status-main`` 是
        # **QSS 的接口**：那一条把容器与它所有子孙刷成透明（见 ``styles``），改名会让
        # hover 底只在缝隙里看得见 —— 故这个名字属于骨架，不属于某一种行。
        main = QWidget(self)
        main.setObjectName("status-main")
        self._main = main
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(2)
        self._title_line = QWidget(main)
        title_layout = QHBoxLayout(self._title_line)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(6)
        self._title_layout = title_layout
        self._name = ElideLabel(title, self._title_line)
        apply_text_role(self._name, "section")
        title_layout.addWidget(self._name)
        title_layout.addStretch(1)          # 徽标插在这一格**之前**（见 set_badge）
        self._title_badge: QWidget | None = None
        self._sub = ElideLabel("", main)
        self._sub.setObjectName("secondary")
        main_layout.addWidget(self._title_line)
        main_layout.addWidget(self._sub)
        # 副标题走 set_subtitle（不是 ElideLabel 的构造参数）：文字与悬停提示在那一处
        # 一次说清，构造期与运行期共用同一条路（空文本的显隐由 ElideLabel 负责）。
        self.set_subtitle(subtitle)
        row.addWidget(main, stretch=1)

        if badge is not None:
            self.set_badge(badge)
        self._init_skin()

    # ---- 标题 / 徽标 / 副标题 ----

    def set_title(self, text: str) -> None:
        """换主标题（两个标题栏位置上的标题都是这一格）。"""
        self._name.setText(text)

    def set_badge(self, badge: QWidget | None) -> None:
        """在主标题行挂一枚徽标：紧随标题、落在弹性空隙**之前**；``None`` = 摘掉。

        徽标是**调用方造好的控件**（面板给模式徽标、快捷键行给来源徽标）—— 骨架不认识
        徽标的内容，只管它在哪一格。传同一个对象是空操作（构造期与运行期都会调它）。
        """
        old = self._title_badge
        if old is badge:
            return
        if old is not None:
            self._title_layout.removeWidget(old)
            old.setParent(None)
            old.deleteLater()
        self._title_badge = badge
        if badge is not None:
            self._title_layout.insertWidget(1, badge)

    def set_subtitle(self, text: str, tooltip: "str | None" = None) -> None:
        """填副标题：文本与悬停提示**共用一份**（不给 ``tooltip`` 时提示就是全文）。

        副标题是单行省略的窄条 —— 长句被截断，截掉的那部分在悬停提示里看得到，
        不必再写第二条文案（「文案只有一份」）。

        **空文本整条隐藏**这条不是本方法做的、也不要在这里重做一遍：``ElideLabel``
        的 ``setText`` 自带（空则 ``setVisible(False)``）。留着的原因见那个类 —— 不隐藏
        的话竖向堆叠仍给它留一行高度，没有副标题的行（面板里很常见）名称会被顶到中间。
        """
        self._sub.setText(text)
        self._sub.setToolTip(text if tooltip is None else tooltip)

    # ---- 图标 ----

    def set_icon(self, icon: "IconName | None") -> None:
        """换图标盒里的字形（未变则不重画 —— 事件回调里会反复调到这里）。"""
        if icon is self._icon_name:
            return
        self._icon_name = icon
        self._paint_icon()

    def set_icon_tooltip(self, text: str) -> None:
        """给**图标盒**挂一句悬停提示（挂盒不挂字形：字形是张位图，鼠标得落在盒上）。

        用途是「图标只分得出类别、名字得补一句」—— 快捷键页拿它显示来源名
        （``框架`` / 某个组件名 / ``我的``）。
        """
        self._ico_box.setToolTip(text)

    def _paint_icon(self) -> None:
        name = self._icon_name or self._icon_fallback
        if name is None:
            return                       # 没名字就留空，不替调用方随便挑一枚
        self._ico.setPixmap(glyph_pixmap(self._ico, name, self.ICON_GLYPH, self._icon_fg))

    # ---- 尾部控件槽 ----

    def add_trailing(self, widget: QWidget, stretch: int = 0) -> None:
        """把一个尾部控件追加到行右侧；**顺序 = 调用顺序**。

        骨架不重排：哪个控件落在哪一格是调用方的语义（面板按声明的控件顺序，
        快捷键行是键位药丸 → 开关 → 初始化）。
        """
        self._row_layout.addWidget(widget, stretch)

    # ---- 主题 ----

    def _init_skin(self) -> None:
        """骨架自己那几处烤色：图标盒 ＋ 主位位图（**按颜色渲染**，不重画就停在旧主题）
        ＋ 副标题角色 ＋ 徽标。

        **构造期只调这一个** —— 子类那几件自绘物（状态角标 / 行内下拉）在这一刻还没造
        出来，合并进 ``refresh_theme`` 会让「子类覆盖 refresh_theme」在构造期就抛异常
        （2026-09-24 实测：骨架构造末尾那一次调用撞上尚未存在的角标）。
        """
        t = base._theme_tokens()
        bg_key, fg_key = ICON_TONES[self._icon_tone]
        self._ico.setStyleSheet(
            f"background:{t[bg_key]}; color:{t[fg_key]}; "
            f"border-radius:{metrics.RADIUS_SM}px; font-size:{metrics.FONT_TITLE}px; qproperty-alignment:AlignCenter;"
        )
        self._icon_fg = t[fg_key]
        self._paint_icon()
        apply_text_role(self._sub, "caption")
        if self._title_badge is not None and hasattr(self._title_badge, "refresh_theme"):
            self._title_badge.refresh_theme()

    def refresh_theme(self, *_args) -> None:  # noqa: D401
        """换肤 = 骨架那几处（``_init_skin``）＋ 子类补的那几处（``_refresh_extra``）。"""
        self._init_skin()
        self._refresh_extra()

    def _refresh_extra(self) -> None:
        """子类自绘件的换肤钩子（默认只转 ``extra_refresh``）。

        构造末尾会**再调一次** ``refresh_theme`` 的只有子类自己 —— 基类构造期那一次
        走的是 ``_init_skin``，故这里可以放心地假定「子类的东西都造好了」。
        """
        if self._extra_refresh is not None:
            self._extra_refresh()


class StatusRow(RecordRow):
    """状态行（设计 §4 StatusRow）：图标容器 + 名称 + 副标题（竖向堆叠）+ 右侧控件 0–2 个。

    与设计稿 ``barkit-ui-refined.html`` 的 ``srow / s-main / s-name / s-sub / s-ctl``
    严格对应：图标盒（accent 底色）+ 名称（13px/600）+ 副标题（11.5px/text-2，单行省略），
    控件（开关 / 下拉）固定在最右。副标题不随名称横向挤占，避免「副标题缺失」观感。

    主题色（图标盒、副标题）统一由 ``refresh_theme`` 重设，供面板在主题切换时统一调用；
    插件可传入 ``extra_refresh`` 以同步自绘控件的 QSS（如 ProfileSelector）。
    """

    def __init__(
        self,
        icon: "IconName",
        name: str,
        controls: "tuple[QWidget, ...]" = (),
        badge: QWidget | None = None,
        on_click=None,
        clickable: bool = False,
        extra_refresh=None,
        parent: QWidget | None = None,
    ) -> None:
        # 骨架在 ``RecordRow``（图标盒 / 标题行 / 副标题 / 尾部控件槽）；本类只把
        # 构造参数摊给骨架。行距比面板紧一档（4 而不是 8）：本行与框架渲染的状态行
        # **并排在同一个列表里**，视觉密度得按那一处的口径。
        super().__init__(title=name, icon=icon, badge=badge, on_click=on_click,
                         clickable=clickable, margins=(14, 4, 14, 4),
                         extra_refresh=extra_refresh, parent=parent)
        for ctrl in controls:
            self.add_trailing(ctrl)

    def set_sub(self, text: str, tooltip: str = "") -> None:
        """设置副标题（副标题为空时隐藏，整行只显示名称）。

        ``tooltip`` 留空 = 悬停提示取**同一份**文本（副标题是单行省略的窄条，
        截掉的部分要靠提示看全）；给了才覆盖。
        """
        self.set_subtitle(text, tooltip or None)

