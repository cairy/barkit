"""编辑屏外壳（导航条 + 四区版面）。"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import (
    QRectF,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QPainter,
    QPainterPath,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpacerItem,
    QVBoxLayout,
    QWidget,
)

from barkit.shell.design import metrics

from .base import PLAIN_ZONE, apply_text_role, note
from . import base
from .buttons import tool_button
from .feedback import FeedbackBar
from .primitives import Card, Divider

# ---------------------------------------------------------------------------
# 编辑屏外壳：导航带 / 内容区 / 动作行 / 反馈槽
# ---------------------------------------------------------------------------
#
# 来历（2026-09-23）：用户在用完当天上午的「快捷键」页后报「编辑界面排版太杂乱」，
# 要求**由框架给出一个版面、所有场景共用**。当时仓里已经有三处「列表屏 → 编辑屏」的
# 同款页面（快捷键条目、hosts 管理窗的条目表单 / 批量导入、录屏通知的规则页），
# 三处的**页脚各排各的**：快捷键是 `[删除] <stretch> [保存] 文案`，hosts 是
# `<stretch> [保存] [取消]`，导入页又是 `[导入] [返回] <stretch>` —— 同一个动作在
# 三页里落在三个不同的角落。四区就是那条「同一件事只许有一种排法」的落点。


@dataclass(frozen=True)
class EditorActionBar:
    """一屏**动作行的全部成员**（保持原有次序），供窗体壳接管后重排。

    它是「动作行单一化」的搬运容器：编辑屏长在详情窗里时，窗体页脚已经有它自己的
    一行动作（「完成」），屏再画一行就是**两行叠层**（2026-10-16 用户上报「编辑按钮与
    窗体组件按钮两行叠层」）。故屏把整条动作行交出去，由窗体壳拼成一行 ——
    **谁提供窗体壳，谁提供动作行**。

    ``hint`` 与 ``feedback`` **一同交出去，不是顺带**：``hint`` 是「紧贴在主动作左边」
    那一格的状态位（``todo`` 的「「确定」存好这条就回列表」就落在那儿），留在屏里就成了
    一句没有动作行的话；``feedback`` 是这一屏那些零模态消息的唯一出口，搬走的只是它的
    **位置**。三者是一行里的三格，拆开任何一格都读不出原本的版面。
    """

    #: 左组：破坏性动作（删除这类），原序。
    destructive: "list[QPushButton]"
    #: 右组：主 / 次动作，**最后一个恒是保存键**（`EditorShell.add_action` 的次序）。
    primaries: "list[QPushButton]"
    #: 状态位那一格（从没设过 ``hint`` 时为 ``None``）。
    hint: "QLabel | None"
    #: 这一屏那条反馈槽。
    feedback: "FeedbackBar"


def _take_widgets(layout: QHBoxLayout) -> "list[QWidget]":
    """把布局里的控件**逐个摘出来**（保持次序），布局本身留空。

    ``takeAt`` 只从布局里摘记录、**不 reparent** —— 不取父的话控件仍被旧父持有，新布局
    再 ``addWidget`` 它就成了「一个控件挂在两处」：几何按旧父算、显示按新布局走。故这里
    连 ``setParent(None)`` 一起做（中间态无事件循环，不会闪出一个顶层窗）。
    """
    out: "list[QWidget]" = []
    while layout.count():
        item = layout.takeAt(0)
        widget = None if item is None else item.widget()
        if widget is None:
            continue
        widget.setParent(None)
        out.append(widget)
    return out


class _NavRule(QWidget):
    """导航带里那根 1px 竖线（返回键与标题之间的分界）。

    自绘而不是 QSS ``border-left``：1px 的竖线走 QSS 要么被 ``autoFillBackground``
    填成一个方块、要么在无边框窗口上不绘制（与 :class:`Divider` 同一条实测理由）。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(1, metrics.NAV_RULE_H)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(base._theme_tokens()["border"]))


class _EditorNav(QWidget):
    """编辑屏的**导航带**：``[返回] │ 标题``，画在一条 ``surface3`` 的圆角带子上。

    带色取 ``surface3`` —— 与分区卡标题栏**同一个值**：全仓「带子」只有这一档，不再
    开第二个带色（开了的代价就是「同一种东西两种深浅」，正是这一批要消掉的东西）。

    **带子端到端贴在面的顶边**（2026-09-27 改）：此前带子四周留着面内边距，用户报
    「标题栏有灰色背景，但没有覆盖整个标题栏区域 —— 与外部边框之间有空隙」。它现在
    与分区卡的标题栏是同一种东西的两种位置（都在「一块面」的顶边），画法也就该同款：
    **只圆上面两个角**（下面两角圆的话，带子下沿会露出两个凹口），且四边向内缩 1px
    （面那圈 1px 描边画在更外层、子控件画在它之上，不缩就会把描边盖掉一段）。

    标题走 ``row_title``（14px/700）而不是 ``detail_title``：它是**你刚点进来的那一行
    的名字**，字号与列表行上那一枚逐格相同 —— 点进来时标题不跳号，读起来是「同一件
    东西换了一屏」，不是「又开了一个页面」。设置窗的页标题（``detail_title``）在它
    上面一格，两者靠这条带子的边界分开。
    """

    def __init__(self, title: str, back_label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # objectName 有两个用处：测试按名字找这条带子量像素 / 量次序，且 ``styles.py``
        # 里那条 ``QWidget#editor_nav`` 的透明条靠它命中。
        self.setObjectName("editor_nav")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(*metrics.PAD_NAV)
        lay.setSpacing(metrics.GAP_MD)
        self.back_btn = tool_button(back_label, size="inline", parent=self)
        lay.addWidget(self.back_btn)
        lay.addWidget(_NavRule(self))
        self.title_label = QLabel(title, self)
        apply_text_role(self.title_label, "row_title")
        lay.addWidget(self.title_label)
        lay.addStretch(1)

    def set_title(self, title: str) -> None:
        """换标题。

        标题在编辑屏上**不是一个恒定值**：它写的是「正在编辑的那一条叫什么」，换一条
        （或从「新增」切到某一条）就得跟着换。三处列表型编辑屏此前各写各的标题
        （一行 ``QLabel`` ＋ ``apply_text_role(…, "section")``，字号与列表行不同），
        现在都走这条带子上的同一个标签。
        """
        self.title_label.setText(title)

    def paintEvent(self, _event) -> None:
        """上两角圆、下两角方、四边内缩 1px —— 与面那圈描边共用一套几何。

        画法与 :class:`_SectionHeader`（分区卡标题栏）**逐句同源**，只有圆角半径跟面
        走（那条跟卡走）—— 同一门语言的两处落点，几何上是同一件事。曾经写成
        ``QRect.adjusted(1, 1, -1, 0)``：``QRect.right()`` 已经是 ``w-1``，再 -1 得到
        ``w-2`` ⇒ 带子右侧比左侧少一列、与描边之间露出一道白缝（那一坑 2026-09-21 在
        分区卡标题栏上踩过，这里照抄它的修法）。
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, 0)
        r = float(max(metrics.RADIUS_LG - 1, 0))
        path = QPainterPath()
        path.moveTo(rect.left(), rect.bottom())
        path.lineTo(rect.left(), rect.top() + r)
        path.arcTo(rect.left(), rect.top(), 2 * r, 2 * r, 180.0, -90.0)
        path.lineTo(rect.right() - r, rect.top())
        path.arcTo(rect.right() - 2 * r, rect.top(), 2 * r, 2 * r, 90.0, -90.0)
        path.lineTo(rect.right(), rect.bottom())
        path.closeSubpath()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(base._theme_tokens()["surface3"]))
        painter.drawPath(path)


class EditorShell(QWidget):
    """编辑屏**四区**外壳 —— 自上而下固定为：

    ① **导航带**：``[返回] │ 标题``（:class:`_EditorNav`），高度固定；
    ② **内容区**：这一屏的输入项，**只放分组**（:func:`form_group`，不带底色 —— 面
       已经由壳给了，里面不再铺一层）；
    ③ **动作行**：左端放**破坏性**动作（删除这类），右端放**主动作**（保存），提示文案
       紧贴在主动作左边 —— 破坏性动作离主动作越远越好，是这一行的全部理由；
    ④ **反馈槽**：一屏一条 ``FeedbackBar``，零模态的唯一出口。

    **这一屏自己就是一「面」**（2026-09-24 定案，第三版）：整块是一张 :class:`Card`
    —— ``RADIUS_LG`` 圆角、底色 ``surface2``（比设置页的 ``surface`` 深一档）、1px
    描边，导航带 / 内容 / 动作行 / 反馈槽**全在这一个面里**。

    前三批返工都栽在同一件事上：只改「底栏长什么样」，没碰「这一屏有没有自己的面」。
    设置页（``settings_page_shell``）只给它**位置**、不给它**面**：内容那两张卡自带面
    （``settings_section`` 是一张 ``Card``），底栏是裸控件直接坐在页面底上 ⇒ 字面意义
    上的两层（用户报「不像弹窗」「按钮区域和其它区域不在一个层」）。而把空白沉到底栏
    下面那版更丑，根因也是同一个 —— 那段空白没有被任何面兜住，就成了页面上的空洞。

    **面由壳给，里面的分组不再自带面**：故内容区用 :func:`form_group`（只有标题 ＋
    间距）而不是 :func:`settings_section`（那是一张卡）—— 面里再铺一层底是「面里有
    面」，两层描边两层圆角。

    **③④ 同属一条「底部区」，贴在面板的底边**（弹窗的按钮本就在底部）：弹性空间
    ``addStretch`` 放在**内容区与底栏之间**，多出来的高度落在内容区下方 ⇒ 它是「面板
    内部的留白」，不是「页面上的空洞」。底栏**不画发丝线**：整屏已经是同一块面，面内
    再划一刀就等于又把内容与动作切成两个部件（2026-09-23 加过一次线，判「割裂」）。

    **反馈槽排在动作行之上**：底栏贴底、从上往下长 ⇒ 插在按钮下方的东西会把按钮往上顶
    （点完「保存」按钮自己挪了位置）。放在按钮之上，长出来的是反馈条、不动的是按钮。

    它**只管版面**，四件事明确不归它：

    - 不管窗口与滚动区：消费方自己给外边距（本壳**自己**的四边内边距是 0，那块面
      ``panel`` 撑满它）。设置窗里的编辑屏本来就长在设置窗的滚动区里，套第二层会出
      双滚动条；带外框的消费方（详情窗）自己加 ``metrics.PAD_PAGE``。
    - 不管换屏：谁把本壳塞进 ``QStackedWidget`` 是消费方的事。
    - 不管保存语义：``add_action`` 只负责「放哪儿」，点了干什么由调用方决定。
    - 不管文案归属：标题与返回键的文案是**声明**，由调用方传；壳自己不说一个字
      （只有 ``hint`` 那一句是调用方递进来的）。

    用法（内容区里那几个分组是调用方加的，不是壳加的 —— 壳不该猜你有几组）::

        shell = EditorShell("新增快捷键", parent=self, hint="改动在点「保存」之后才生效")
        shell.back.connect(self.back.emit)
        group, body = form_group("这条快捷键", shell)
        shell.content.addWidget(group)
        body.addLayout(form_row("名称", self._title_edit, group))
        self._save_btn = shell.add_action("保存", self._on_save, kind="primary")
        shell.feedback.error("…")
    """

    #: 导航带里那枚「返回」被点了（消费方接它去关掉这一屏）。
    back = Signal()

    def __init__(
        self,
        title: str,
        parent: QWidget | None = None,
        *,
        hint: str = "",
        back_label: str = "返回",
    ) -> None:
        super().__init__(parent)
        # 这一屏的**面**：一张撑满本部件的 Card。**用组合而不是继承** —— `Card` 定义在
        # 本文件更下面，继承得把整段类搬上来；且「壳 = 一块面 ＋ 面上的四区」本来就是
        # 组合关系（换一种面的画法不该牵动四区的装法）。
        #
        # 底色取 `surface2`（比设置页的 `surface` 深一档）：**同底就只有一条框**，读起来
        # 还是「几块部件摆在页面上」，高一档才有「这是一块浮上来的面」。
        self._panel = Card(radius=metrics.RADIUS_LG, parent=self,
                           bg_token="surface2", border_token="border")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._panel)

        self._root = QVBoxLayout(self._panel)
        # **面内边距不再由根部统一给**（2026-09-27 用户上报「标题栏有灰色背景，但没有
        # 覆盖整个标题栏区域 —— 与外部边框之间有空隙」）：导航带要**端到端**地贴在面上
        # （与分区卡标题栏同一门语言），而根部一旦留 12 / 10 的边距，带子四周就露一圈
        # 面底色，读起来是「一条浮在面中间的带子」。
        #
        # 改由各区自己带：内容区给左右上、底栏给左右下、导航带自带自己的左右内边距
        # （它现在从面的左边缘起算）。于是这一屏的标签左缘、带子里的「返回」、底栏按钮
        # 三处**仍是同一条竖线**（都是面的边 ＋ 12）。
        self._root.setContentsMargins(0, 0, 0, 0)
        self._root.setSpacing(0)

        self._nav = _EditorNav(title, back_label, self._panel)
        self._nav.back_btn.clicked.connect(lambda _checked=False: self.back.emit())
        self._root.addWidget(self._nav)

        # ② 内容区：**自带滚动区**（2026-10-16，用户上报「编辑不能头尾固定」）。
        #
        # 导航带与底栏落在这个滚动区**外面** ⇒ 矮窗下滚的是内容，返回键与动作行留在
        # 原处。此前滚动只由外层（详情窗的 `_scrolled` / 设置窗的滚动区）提供，滚的是
        # **整个编辑屏**，头尾跟着走 —— 在那套结构里「滚动」与「头尾固定」是互斥的，
        # 不是参数没调对。
        #
        # 卡与卡之间比卡内的行间距宽一档（GAP_LG 12 对卡内的 GAP_SM 6）—— 「换了一组」
        # 与「换了一行」在间距上就得是两个数。
        self._content_host = QWidget(self._panel)
        # 裸容器**必须显式透明**：全局那条 `QWidget { background: surface }` 管的是裸实例
        # （自定义子类不画），不声明的话滚动区里会垫着一块比面更亮的方块。
        self._content_host.setObjectName(PLAIN_ZONE)
        self.content = QVBoxLayout(self._content_host)
        self.content.setContentsMargins(
            metrics.PAD_CARD[0], metrics.GAP_LG, metrics.PAD_CARD[2], 0
        )
        self.content.setSpacing(metrics.GAP_LG)
        # **末尾常驻一段伸展**：内容不足一屏时余高落在**内容下方**，而不是把几组拉开
        # —— 所以 `add_group` 是**插在它之前**的（见那个方法）。
        self.content.addStretch(1)
        self._content_scroll = QScrollArea(self._panel)
        self._content_scroll.setObjectName("editor_scroll")
        self._content_scroll.setWidgetResizable(True)
        self._content_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._content_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._content_scroll.setWidget(self._content_host)
        self._root.addWidget(self._content_scroll)
        # 底栏与内容之间钉一段**最小间距**：内容撑满时它保证底栏不贴到内容上（那会读成
        # 「又一组」）。
        #
        # **这一段留白不再抢余高**（2026-10-16 起余高归内容滚动区）：滚动区是
        # ``Expanding``，它把余高吃掉之后，「内容不足一屏、留白落在内容下方」由内容布局
        # 末尾那段 ``addStretch(1)`` 兑现（见上面 ② 那段）。`_tail_stretch` 这个具名
        # spacer 留着是因为 :meth:`stretch_content` 仍要引用它。
        self._root.addSpacing(metrics.GAP_LG)
        self._tail_stretch = QSpacerItem(
            0, 0, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Minimum
        )
        self._root.addSpacerItem(self._tail_stretch)

        # ③ ④ **底部区**：反馈槽与动作行同属一条底栏，**贴在面板底边**。
        # 动作区**顶边一条发丝线**（`Divider`，`border` 令牌，与面板 1px 描边同色）：
        # 2026-09-24 晚补 —— 当前整屏是一块 `surface2` 面板（第三版定案），线与内容
        # 同处一面，它只 demarcate 底部动作块、不会复现 2026-09-23 那次「割裂」（那次
        # 根因是底栏裸控件坐页面底 ＋ 215px 缝的**两层结构**，不是线本身）。线走
        # `border` 令牌 ⇒ 与背景同源、天然协调。它作为 `_root` 独立子项落在 stretch 与
        # 底栏之间（不进 `footer` 内部）⇒ 不改底栏自身高度，不误伤「内容底边↔底栏距离」
        # 与「底栏贴底」两条判据。
        #
        # ◷ 2026-09-27：它现在**到得了头**（从面的左边缘到右边缘）—— 根部边距归零之后
        # 它作为根的直接子项自然铺满。此前它跟内容区一样缩在 12px 里，用户问「横线分隔
        # 为什么不到头」。
        # 存引用（不是匿名加进去）：:meth:`release_action_bar` 托管走动作行之后要把它
        # 一起藏掉 —— 内容底下挂着一条没有归属的发丝线比不画更糟。
        self._divider = Divider(self._panel)
        self._root.addWidget(self._divider)
        self._footer = QWidget(self._panel)
        # 裸容器**必须显式透明**：全局那条 `QWidget { background: surface }` 管的是
        # **裸实例**（自定义子类不画），不声明的话这块底栏就自带一块比面更亮的方块
        # —— 用户 2026-09-27 报的原话是「action 按钮区域整个也有白色底色，违和」。
        self._footer.setObjectName(PLAIN_ZONE)
        footer_lay = QVBoxLayout(self._footer)
        # 底栏自带内边距（面的边距归零之后由它补）：左右与内容区同宽（12 ⇒ 破坏性动作
        # 与主动作各自贴住**内容区**的左右缘，不是贴住面的边），上留一档让按钮不顶着
        # 那条发丝线，下留 `PAD_CARD` 让按钮不顶着面的底边。
        footer_lay.setContentsMargins(
            metrics.PAD_CARD[0], metrics.GAP_MD, metrics.PAD_CARD[2], metrics.PAD_CARD[3]
        )
        footer_lay.setSpacing(metrics.GAP_MD)

        # ④ 动作行：左组＝破坏性动作，右组＝提示＋主动作。**两组分开装**（不是一条
        # 带索引的平铺行）：新动作只会追加到本组末尾，「谁在左谁在右」是结构决定的，
        # 不靠调用方按顺序调用的自觉。
        self._actions = QHBoxLayout()
        self._actions.setSpacing(metrics.GAP_MD)
        self._left = QHBoxLayout()
        self._left.setSpacing(metrics.GAP_MD)
        self._right = QHBoxLayout()
        self._right.setSpacing(metrics.GAP_MD)
        #: 右端已有的主动作（按**从右往左**的次序记）：新加的插在它们之前 ⇒ 保存恒在最右。
        self._primaries: "list[QPushButton]" = []
        self._actions.addLayout(self._left)
        self._actions.addStretch(1)
        # 提示**常建**（``hint`` 为空也建）：它是这一屏的**状态位**，由调用方按当前
        # 状态改写（见 :meth:`set_hint`），不是一句钉死的说明 —— 建了再藏比用时再造
        # 少一条「造的时候该不该算进版面」的分支。
        #
        # **不换行**：页脚那一行的高度由按钮（28）定，边注一旦折成两行就把页脚撑到
        # 34、还在按钮边上挂一小段孤字（2026-09-23 实测：上一版 13 个字的提示在
        # 132px 里折成两行）。故提示是**一句短文案**，长了由调用方自己改短 ——
        # 这是文案的事，不是版面的事。
        self._hint = note(hint, self._panel)
        self._hint.setWordWrap(False)
        self._hint.setVisible(bool(hint))
        self._right.addWidget(self._hint)
        self._actions.addLayout(self._right)
        # `FeedbackBar` 自建时是隐藏的，由调用方 `info/warn/error` 唤出。**排在动作行
        # 之上**：底栏贴在面板底边、从上往下长 ⇒ 插在按钮下方的东西会把按钮往上顶
        # （点完「保存」按钮自己挪了位置）。放在按钮之上，长出来的是反馈条、不动的是按钮。
        self.feedback = FeedbackBar(self._footer)
        footer_lay.addWidget(self.feedback)
        footer_lay.addLayout(self._actions)
        self._root.addWidget(self._footer)

    # ---------- 对外（四个口，够这一批用） ----------

    def panel(self) -> "Card":
        """这一屏**那一块面**的判据读口（量整屏是不是自成一个容器）。

        判据要抓手就给读口，不挂 ``objectName``：``Card`` 是**自绘**的，QSS 里没有它的
        规则，挂一个 id 上去就是「设了等于没设」（与 :meth:`hint_label` 同一条理由）。
        """
        return self._panel

    def footer_widget(self) -> QWidget:
        """底部区那条**底栏**的判据读口（量它是不是贴在面板底边）。

        与 :meth:`panel` 同一条理由：给读口而不是给 id。
        """
        return self._footer

    def content_scroll(self) -> QScrollArea:
        """内容区那个**滚动区**的判据读口（量余高归谁、量滚动发生在哪一层）。

        与 :meth:`panel` / :meth:`footer_widget` 同一条理由：给读口而不是给 id。
        """
        return self._content_scroll

    def hint_label(self) -> QLabel:
        """页脚那句提示的**判据读口**（量它落在按钮那条带子里 / 读它当前说了什么）。

        **不给它设 ``objectName``**：QLabel 上的 objectName 必须在 `styles` 里真有
        一条规则（``test_label_objectnames_are_styled_or_none`` 守这一条），而这句提示
        的样式走 :func:`note` + ``apply_text_role``，再来一个 id 规则就是「设了等于没
        设」。判据要抓手就给一个读口 —— 与 :meth:`content_widgets` 同一条理由。
        """
        return self._hint

    def set_hint(self, text: str, *, role: str = "note") -> None:
        """改写页脚那句提示（**空串 = 不显示**），顺带换它的文字档。

        它是这一屏的**状态位**，不是一句钉死的开场说明：调用方按当前状态改写它
        （「有改动没保存」/「名称与命令都得填」/ 干净时干脆一句都不说）。
        ``role`` 默认 ``note``（与 :func:`note` 同档）；要它更抢眼时传 ``caption_warn``。

        **空串藏起来而不是留一句空话**：一条常驻的「改动在点保存之后才生效」在保存
        之后还挂在那里，说的就是已经过期的状态 —— 状态位的价值全在它跟着状态变。
        """
        apply_text_role(self._hint, role)
        self._hint.setText(text)
        self._hint.setVisible(bool(text))

    def set_title(self, text: str) -> None:
        """改写导航带里的标题（见 :meth:`_EditorNav.set_title`：编辑屏的标题是会变的）。"""
        self._nav.set_title(text)

    def add_action(
        self, text: str, on_click, *, kind: str = "primary", tier: str = "primary"
    ) -> QPushButton:
        """往动作行加一枚按钮，返回它（调用方要拿它去禁用 / 改文案）。

        ``kind="primary"`` 落在**右端**（主 / 次动作，按添加顺序从右往左排）；
        ``kind="destructive"`` 落在**左端**（删除这类不可逆动作）。传别的名字当场报错
        —— 与 ``apply_button_tier`` 同一条口径：笔误别静默变成第四种排法。

        **右端那枚恒是最后一步（保存）**：后加的主动作（如翻页时钟那枚「测试」）插在
        已有主动作**之前**，不是追加到最右 —— 追加的话保存键就不再贴住右缘，而「主动作
        贴右缘」是这一行唯一的排版理由（守它的判据量的是保存键的右边距）。

        ``tier`` 是**外观档**（``"primary"`` 实心 / ``"pill-ghost"`` 幽灵），与 ``kind``
        （位置）是两件事：第二枚实心按钮会跟保存抢注意力，次动作该用幽灵档。

        **破坏性动作今天只影响位置**，外观仍是 ``pill-ghost``（仓里唯一的「危险」外观
        ``#danger`` 是**实心红**）—— 一条两段式确认（点一次变「确认删除」）配实心红，
        按钮会比它要确认的那件事更抢眼，故本批不开这一族；等真有第二个消费方要
        红色危险按钮时再开，别在这里就地拼一份 QSS。
        """
        if kind == "destructive":
            btn = tool_button(text, "pill-ghost", size="action", parent=self)
            self._left.addWidget(btn)
        elif kind == "primary":
            btn = tool_button(text, tier, size="action", parent=self)
            # 插在**已有主动作之前**（提示之后）：保存恒在最右，见 docstring。
            self._right.insertWidget(self._right.count() - len(self._primaries), btn)
            self._primaries.append(btn)
        else:
            raise ValueError(
                f"未知动作位置：{kind!r}；合法值 ['destructive', 'primary']"
            )
        btn.clicked.connect(lambda _checked=False, cb=on_click: cb())
        return btn

    def content_widgets(self) -> list[QWidget]:
        """内容区当前的一级子件（按落位顺序）。

        有这个口是因为「内容区**只放卡**」是一条**判据**（门禁与测试走它），不是一句
        口头约定：裸行落在卡外就没有归属，用户看不出哪几行是一件事 —— 2026-09-21 那次
        「设置页分区卡」返工与本次「编辑界面杂乱」是同一件事的两次发作。
        """
        widgets = []
        for i in range(self.content.count()):
            item = self.content.itemAt(i)
            w = None if item is None else item.widget()
            if w is not None:
                widgets.append(w)
        return widgets

    def stretch_content(self) -> None:
        """让**内容区吃掉多余的垂直高度**。

        ◷ **2026-10-16 起它已是兼容口**（留着是因为两处消费方仍在调）：内容区自带滚动区
        之后，余高**本来就归它**（滚动区 ``Expanding``、``_tail_stretch`` 退成
        ``Minimum``）⇒ 这个方法今天做什么都不影响结果。「内容不足一屏时留白落在内容
        下方」也不再靠这里，而是靠内容布局末尾那段 ``addStretch(1)``。

        旧版（滚动区之前）它做两件事：① 把尾部那段留白收回去；② 把该份高度转给内容区。
        **承重的是 ②** —— 内容**不**自带伸展体时只有 ② 够（2026-09-27 实测，壳高 640：
        都不做 13 / 只做① 197 / 只做② 537 / 都做 537）。

        给「内容里自带滚动区 / 多行文本框」的屏（录屏通知的进程匹配、hosts 的批量导入
        多行框）用 —— 它们按 ``sizeHint`` 只拿一小条高，里面那个滚动区会被压扁。
        """
        self._tail_stretch.changeSize(
            0, 0, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Minimum
        )
        self._root.setStretchFactor(self._content_scroll, 1)

    def release_action_bar(self) -> EditorActionBar:
        """把**整条动作行**（破坏性动作 / 状态位 / 主次动作 / 反馈槽）交出去托管。

        给「编辑屏长在详情窗里」那种用法：窗体页脚已经有一行动作（它自己的「完成」），
        屏再画一行就是两行叠层。收走之后本壳只剩导航带与内容区 —— 底栏与它顶上那条发丝
        线一起藏掉，不然内容底下会挂着一条没有归属的线。

        ⚠️ **只该在屏真被托管时调**：调过之后本壳不再管这些控件的落位、也不再有动作行
        （再 ``add_action`` 也接不回那条带子，它已经藏了）。屏自己用（快捷键页、录屏
        通知规则页、一键输入那几处）时不要碰它。

        次序随返回值原样带出：左组原序、右组原序（保存恒在末尾）。拼成什么顺序由调用方
        决定 —— 本壳不猜窗体页脚长什么样。
        """
        destructive = _take_widgets(self._left)
        right = _take_widgets(self._right)
        hint = next((w for w in right if w is self._hint), None)
        primaries = [w for w in right if w is not self._hint]
        self._primaries = []
        self._divider.setVisible(False)
        self._footer.setVisible(False)
        feedback = self.feedback
        feedback.setParent(None)
        return EditorActionBar(
            destructive=destructive,
            primaries=primaries,
            hint=hint,
            feedback=feedback,
        )

