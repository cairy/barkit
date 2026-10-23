"""设置页骨架与表单行（分区卡 / 表单组 / 标签 / 行）。"""

from __future__ import annotations

from PySide6.QtCore import (
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QPainter,
    QPainterPath,
)
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSpacerItem,
    QVBoxLayout,
    QWidget,
)

from barkit.shell.design import metrics

from .base import apply_text_role, note
from . import base
from .primitives import Card, Divider

def settings_page_shell(title: str, parent: QWidget | None = None) -> tuple[QWidget, QVBoxLayout]:
    """设置页统一外壳：一个「set-page」卡片 + 标题 + 标准间距的纵向布局。

    设置窗口与各插件贡献的设置页共用，确保外壳视觉一致、且插件无需各自
    import ``QWidget`` / ``QVBoxLayout`` / ``QLabel``（此前 autotype / notify
    插件漏导入 ``QVBoxLayout`` 导致构建崩溃）。返回 (page, layout)，调用方
    继续往 layout 里加控件即可。
    """
    page = QWidget(parent)
    page.setObjectName("set-page")
    layout = QVBoxLayout(page)
    layout.setContentsMargins(*metrics.PAD_PAGE)
    layout.setSpacing(12)
    h = QLabel(title)
    apply_text_role(h, "detail_title")
    layout.addWidget(h)
    return page, layout


#: 设置页外壳里「标题以下」那个**插槽位**的下标（第 0 项是标题本身）。
PAGE_SLOT = 1


def is_standard_shell(page: QWidget) -> bool:
    """这一页是不是**标准外壳**（有可插的纵向布局）—— 两个插槽口共用的同一判据。

    判据只有一条：有个能 ``insertWidget`` 的 layout。不认 widget 的类名、不认
    ``objectName`` —— 那是外壳的**实现**，以后换个容器类名就不该让插槽失效。
    """
    lay = page.layout()
    return lay is not None and hasattr(lay, "insertWidget")


def insert_page_slot(page: QWidget, widget: QWidget) -> bool:
    """把 ``widget`` 插进设置页「标题**以下**」那个插槽（框架自动段的落点）。

    为什么要有这么一个位：框架往插件设置页里补的段（今天只有「状态机」）不能**抢在
    标题前面**——那样整页读起来是「先来一段不知道属于谁的卡片，然后才是这个组件的
    名字」（2026-09-21 用户上报）。它也不该掉到页尾：那是「先看全局再改细节」的反面。
    于是外壳固定给一个位：标题之下、插件自己内容之上。

    返回 False = 这一页不是标准外壳（没有可插的纵向布局），调用方自行兜底。
    """
    if not is_standard_shell(page):
        return False
    lay = page.layout()
    lay.insertWidget(min(PAGE_SLOT, lay.count()), widget)
    return True


def insert_page_tail(page: QWidget, widget: QWidget) -> bool:
    """把 ``widget`` 追加到设置页「组件内容**之后**」那个插槽（框架区的落点）。

    与 :func:`insert_page_slot` 是一对：那个管页首（框架自动段，如状态机），这个管页尾
    （框架区：运行间隔 / 配置维护 / 装置运维）。框架画的东西**只许**落在这两头 ——
    夹在组件自己的配置卡中间，用户分不出哪几张是组件的、哪几张是框架的。

    **要跳过末尾连续的 stretch**：四个组件页以 ``addStretch(1)`` 收尾、``notify_guard``
    没有；不跳过的话框架区会被顶到 stretch **上面**，于是「有 stretch 的页」里它长在
    页面中部、「没 stretch 的页」里它长在最底下 —— 同一个东西每页位置不同，正是这一批
    要消掉的东西。跳过之后两种页都落在同一条基准线上（紧贴页尾）。

    返回 False = 这一页不是标准外壳（没有可插的纵向布局），调用方自行兜底。
    """
    if not is_standard_shell(page):
        return False
    lay = page.layout()
    idx = lay.count()
    while idx > 0:
        item = lay.itemAt(idx - 1)
        if item is None or item.spacerItem() is None:
            break
        idx -= 1
    lay.insertWidget(idx, widget)
    return True


class _SectionHeader(QWidget):
    """分区卡的**标题栏**：一条带底色的带子，可点折叠（``collapsible`` 时）。

    为什么给**底色**而不是继续用一条横线（2026-09-21 返工）：整页都是同款卡片时，
    那条 1px 的 border 色横线**扫不出分组** —— 它和卡内的分隔线是同一种东西，用户
    要「一眼定位到某一组」靠的是**块的边界**。带上底色之后，标题与内容之间就没有
    要分隔的东西了（底色本身就是边界）⇒ 那条 ``Divider`` 一并去掉。

    只圆**上面两个角**：这条带子贴在卡片顶边，下面两角要是也圆，卡内会露出两个凹口。
    绘制区向内缩 1px —— ``Card`` 那圈 1px 描边画在更外层、而子控件画在它之上，
    不缩就会把描边盖掉一段（看着像「卡的边缺了一截」）。
    """

    #: True = 展开；折叠由本栏自己管，外部只订阅就够了（不反向查控件树）
    toggled = Signal(bool)

    def __init__(
        self,
        title: str,
        radius: int = metrics.RADIUS_SM,
        collapsible: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # 全局 QSS 有一条 ``QWidget { background: surface }``：不给这里显式透明，
        # 四个角会被填成直角的实底，把 Card 的圆角描边盖掉（规则见 styles.py）。
        self.setObjectName("section_head")
        # 高度**钉死在自己内容那一格**，不许被卡的高度拉走。
        #
        # 折叠时内容区隐藏，卡（通常是 ``stretch=1`` 铺满一屏）里就只剩这条带子一个
        # 子件；而 ``QWidget`` 默认竖直策略是 ``Preferred`` ——它带着 ``GrowFlag``，
        # 布局在没有别的可伸展项时会把多出来的卡高**全给这一项**（实测卡高 426 时
        # 标题栏也涨到 426），于是整张卡被这层底色糊成一条实心带子
        # （2026-09-24 用户报「点『键位』收起卡，整个界面错乱」）。
        # 展开时看不出来，是因为内容区自己会吃掉余高，标题栏停在 34。
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._radius = radius
        self._collapsible = collapsible
        self._expanded = True
        lay = QHBoxLayout(self)
        lay.setContentsMargins(*metrics.PAD_SECTION_HEAD)
        lay.setSpacing(metrics.GAP_SM)
        t = QLabel(title, self)
        apply_text_role(t, "section")
        lay.addWidget(t)
        self._title_lbl = t
        lay.addStretch(1)
        self._mark: QLabel | None = None
        if collapsible:
            self._mark = QLabel("▾", self)
            apply_text_role(self._mark, "fold_mark")
            lay.addWidget(self._mark)
            self.setCursor(Qt.CursorShape.PointingHandCursor)

    @property
    def title_text(self) -> str:
        """本栏的标题文案（按标题找卡 / 断言用；标题为空时本栏压根不存在）。"""
        return self._title_lbl.text()

    @property
    def expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, on: bool) -> None:
        """展开 / 折叠；带子上的箭头跟着翻（箭头是**状态**，不是装饰）。"""
        if not self._collapsible or self._expanded == bool(on):
            return
        self._expanded = bool(on)
        if self._mark is not None:
            self._mark.setText("▾" if self._expanded else "▸")
        self.toggled.emit(self._expanded)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        # 非 collapsible 时没有手型光标，也不响应（没有可折叠的东西）
        if self._collapsible and event.button() == Qt.MouseButton.LeftButton:
            self.set_expanded(not self._expanded)
        super().mousePressEvent(event)

    def paintEvent(self, _event) -> None:
        t = base._theme_tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # 与 Card 那条描边**共用一套几何**：描边整条落在卡片内（左 [0,1)、右 (w-1,w]），
        # 带子就填它内侧那一块 —— 左起 1、右到 w-1（浮点矩形，没有 off-by-one）。
        # 早先写 ``QRect.adjusted(1, 1, -1, 0)``：``QRect.right()`` 已经是 ``w-1``，
        # 再 -1 得到 ``w-2`` ⇒ 带子右侧比左侧**少一列**，与描边之间露出一道白缝
        # （2026-09-21 用户上报「右侧没有覆盖到位」，实测 x=w-2 是纯白）。
        rect = QRectF(self.rect()).adjusted(1, 1, -1, 0)
        r = float(max(self._radius - 1, 0))
        # 先按「卡片描边内沿」裁一刀：带子的圆角与描边内沿（同一圆心、半径 r）本该
        # 重合，但两处各自反锯齿取整 ⇒ 角上会有一两个像素的带色探到描边外面。
        # 折叠时带子下沿就是卡片下沿，方角同样会探出去（卡只剩标题栏那一截）。
        clip = QPainterPath()
        card = self.parentWidget()
        inner_h = (card.height() if card is not None else self.height()) - 2.0
        clip.addRoundedRect(
            QRectF(1.0, 1.0, rect.width(), max(inner_h, 1.0)), r, r
        )
        painter.setClipPath(clip)
        path = QPainterPath()
        path.moveTo(rect.left(), rect.bottom())
        path.lineTo(rect.left(), rect.top() + r)
        path.arcTo(rect.left(), rect.top(), 2 * r, 2 * r, 180.0, -90.0)
        path.lineTo(rect.right() - r, rect.top())
        path.arcTo(rect.right() - 2 * r, rect.top(), 2 * r, 2 * r, 90.0, -90.0)
        path.lineTo(rect.right(), rect.bottom())
        path.closeSubpath()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(t["surface3"]))
        painter.drawPath(path)


def settings_section(
    title: str,
    parent: QWidget | None = None,
    *,
    collapsible: bool = True,
    expanded: bool = True,
) -> tuple[QWidget, QVBoxLayout]:
    """设置页统一的「分区卡」原语：``Card``（微圆角）＋ 带底色的标题栏 ＋ 内容区。

    框架「通用」页与所有插件设置页共用 —— **设置页里每一组控件都该长在这张卡里**：
    卡外的裸行没有归属，用户看不出哪几行是一件事（2026-09-21 的返工就是这么来的）。
    返回 ``(card, content)``，调用方往 ``content`` 里加行即可；行与行之间**不必**再自己
    插 ``Divider`` —— 卡本身就是分组，卡内用间距分段即可。

    结构固定为「**标题栏（带底色）→ 内容**」，标题为空则**不建标题栏**（没有标题就
    没有要标出来的那一组）⇒ 也不可折叠 —— 折叠的把手就是标题栏本身。

    **默认可折叠**：一页里塞五张卡时，折叠是「只留我关心的那一组开着」的唯一手段，
    而它的代价只有标题栏右侧那一枚箭头。``expanded=False`` 可让某张卡初开就是收起的。

    **收起时的几何**（2026-09-24 用户报「点『键位』收起卡，整个界面错乱」之后定的）：
    收起只藏内容区，但**卡与相邻内容的位置一个都不许动**。这条要成立，得同时做到三件
    事，少一件都会当场看出错乱：

    1. **标题栏高度钉在自己内容上**（``_SectionHeader`` 的竖直策略 ``Fixed``）。
       收起后卡里只剩它一个子件，而 ``QWidget`` 默认策略带 ``GrowFlag`` ⇒ 布局会把整张
       卡的高都喂给它，标题栏涨满、底色糊成一块实心带子。
    2. **卡把伸展份额让出去**（收起时 ``setStretch(idx, 0)``）。宿主（如「快捷键」页）
       常用 ``stretch=1`` 把卡铺满一屏；不让出去的话，收起来剩下的是「带子 ＋ 一大块
       空卡面」。**让的方式必须是改 stretch**，改卡的尺寸策略没有用（它只管自己愿意
       长多高，空间是布局按 stretch 发的）—— 那一版已被植入自证验死并删除。
    3. **余高有人接**（往宿主布局里塞一根 ``Expanding`` 的空档）。只做前两件更糟：
       余高会被同一列里别的行吃走（说明行撑成一大块空白），后面的内容整体沉到页底
       —— 实测卡从 ``y=29`` 掉到 ``y=296``。展开时把伸展份额还回去；那根空档常驻
       不动（它接住余高靠的是 ``Expanding``，与 stretch 无关），于是同一页反复收放
       不会漂。

    几何全部走 ``metrics``（圆角 ``RADIUS_SM`` 微圆角、内容区内边距 ``PAD_CARD``、
    标题栏内边距 ``PAD_SECTION_HEAD``、段距 ``GAP_SM``）。**卡片是实心 surface ＋ 1px
    描边，没有软投影**：投影 2026-09-21 已判丑并删除（判据见
    `docs/log/2026-09-21-设置窗观感返工.md`），别再挂 ``QGraphicsEffect``。
    """
    card = Card(radius=metrics.RADIUS_SM, parent=parent)
    outer = QVBoxLayout(card)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)
    body = QWidget(card)
    # 透明：底色只许 Card 画一层（规则与理由见 styles.py 的 section_body 那条）
    body.setObjectName("section_body")
    body_lay = QVBoxLayout(body)
    body_lay.setContentsMargins(*metrics.PAD_CARD)
    body_lay.setSpacing(metrics.GAP_SM)
    if title:
        head = _SectionHeader(title, metrics.RADIUS_SM, collapsible, card)
        #: 那根「接住余高」的空档（第一次收起时才造；造出来就常驻，不再动它）
        fill: list[QSpacerItem] = []
        #: 宿主原本给卡的伸展份额（收起时让出去，展开时还回来）
        restore: list[int] = []
        #: 「构造期就收起」时的一次性补救标记（卡那时还没进宿主布局）
        retried: list[bool] = []

        def _sync_expanded(on: bool) -> None:
            """收起 / 展开各自要做的事，缺一件这个交互就是坏的。

            三件事：隐藏内容区、把卡的**伸展份额让出去**、并保证余高**有人接**。
            为什么三件都要，见 :func:`settings_section` 的「收起时的几何」。

            注意**不是**靠改卡的尺寸策略让出伸展 —— 试过，没用：``sizePolicy`` 只影响
            它自己愿意长多高，而空间是布局按 stretch 发的。真正管用的是下面这两个
            动作（改 stretch 份额 ＋ 塞一根 ``Expanding`` 空档），植入自证也证实了：
            把策略那一版改回旧样子，用例照样全绿 ⇒ 它是死代码，已删。
            """
            body.setVisible(on)
            host = card.parentWidget()
            lay = host.layout() if host is not None else None
            idx = lay.indexOf(card) if lay is not None else -1
            if idx < 0:
                # 卡还没进宿主布局 —— 本栏在构造期就收起（``expanded=False``）时正是
                # 这样：那一刻宿主的布局里还没有这张卡。**等布局转起来补一次**，
                # 不重试（卡一直没进布局就不该一直排队等着）。
                if not retried:
                    retried.append(True)
                    QTimer.singleShot(0, lambda: _sync_expanded(on))
                return
            if not fill:
                # 余高必须有人接：同列里别的行（说明行那种 ``QLabel``）会把它吃走，
                # 撑成一大块空白并把卡挤到页底（实测卡从 y=29 掉到 y=296）。
                # 这根空档靠 **``Expanding``** 接住余高，不需要额外的 stretch 开关。
                spacer = QSpacerItem(0, 0, QSizePolicy.Policy.Minimum,
                                     QSizePolicy.Policy.Expanding)
                lay.addItem(spacer)
                fill.append(spacer)
            if on:
                lay.setStretch(idx, restore[0] if restore else lay.stretch(idx))
            else:
                restore[:] = [lay.stretch(idx)]
                lay.setStretch(idx, 0)
            card.updateGeometry()

        head.toggled.connect(_sync_expanded)
        outer.addWidget(head)
        if not expanded:
            head.set_expanded(False)
    outer.addWidget(body)
    return card, body_lay


class FormGroup(QWidget):
    """:func:`form_group` 造出来的那个容器 —— **存在的唯一理由是判据**。

    「内容区里不许有裸行」这条判据（卡外的裸行没有归属）此前靠 ``isinstance(w, Card)``
    认：内容区的一级子件必须是卡。分组改成不带面之后它就是一个普通 ``QWidget``，
    跟一行裸控件在类型上没有区别 ⇒ 判据**认不出**这一条了。给它一个具名类型，判据
    就能接着守同一件事（不走 ``objectName``：那是给 QSS 用的，为判据挂 id 是
    「设了等于没设」，与 :meth:`EditorShell.panel` 同一条理由）。
    """


def form_group(
    title: str,
    parent: QWidget | None = None,
    *,
    divider_above: bool = False,
) -> tuple[QWidget, QVBoxLayout]:
    """一块**已有面的容器内部**的分组：标题 ＋ 内容行，**没有底色、没有描边**。

    与 ``settings_section`` 的分工：后者是「分区卡」，自带一张 ``Card`` 的面 —— 那是
    **直接铺在设置页上**的分组语言。已经自成一块面的容器（今天只有 :class:`EditorShell`）
    里面**不能再铺一层底**：那是「面里有面」，两层描边两层圆角，而底下的动作行没有
    面 ⇒ 内容与动作行分属两层（2026-09-24 用户报「不像弹窗、按钮区域和其它区域不在
    一个层」）。这里的分组只靠**标题 ＋ 间距**分段，面由外面那层容器统一给 —— 同一
    块面上的东西才读作「同一个弹窗的内部」。

    ``divider_above=True`` 在标题之上补一条 :class:`Divider`：**只给第二组及以后**用
    —— 第一组头顶那一刀是多余的（容器自己的顶边已经是边界）。

    返回 ``(group, layout)``，调用方往 ``layout`` 里加行，与 ``settings_section``
    的 ``(card, body)`` 同形（换原语不用改调用方的写法）。
    """
    box = FormGroup(parent)
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(metrics.GAP_SM)
    if divider_above:
        lay.addWidget(Divider(box))
        lay.addSpacing(metrics.GAP_MD)
    head = QLabel(title, box)
    apply_text_role(head, "section")
    lay.addWidget(head)
    return box, lay


def form_label(text: str, parent: QWidget | None = None) -> QLabel:
    """表单行标签（``caption`` 档）—— **全仓「标签 + 控件」行唯一的标签构造口**。

    此前这条路上有 **五份** ``_field_label`` 私有拷贝（`route_guard` / `ipad_tunnel` /
    `hosts_guard` / `autotype` / `notify_guard` 各一份）＋ 一处手搓 ``QLabel`` ：
    同页里标签左缘时对齐时不对齐，且五份各自演化 —— 其中只有 `route_guard` 那份带
    ``setWordWrap``，另四份没有。后果是**长文案会把窗撑宽、分隔条拖不动**（2026-09-18
    真机踩过），而修好的只有一份。

    ``setWordWrap`` 是**默认开**的：不换行时 QLabel 的最小宽度被整行文案撑住，而标签
    是这一行里最该让步的那个。要定宽（``form_row``）走那个口、别在这里传宽度。
    """
    lbl = QLabel(text, parent)
    lbl.setWordWrap(True)
    apply_text_role(lbl, "caption")
    return lbl


def form_row(
    label: str,
    control: QWidget,
    parent: QWidget | None = None,
    label_width: int = metrics.LABEL_W,
    *,
    control_fill: bool = True,
) -> QHBoxLayout:
    """设置页统一的「标签 + 控件」行：默认标签定宽（``caption`` 档）＋ 控件占满剩余宽度。

    泛化框架页的 ``_tray_row``（原本只服务「通用」页，插件页各自重写了一份）；
    所有「一行一个设置项」都走这里，**一页里标签左缘对齐**。

    ``control_fill`` 判的是**控件该不该拉满**（不是排版偏好，是控件的性质）：

    - ``True``（默认）：控件拉满剩余宽度 —— 全宽输入类（``QLineEdit`` / ``QTextEdit``）
      天生要占满一行，留白反而看不出输入区到哪结束。
    - ``False``：控件保持自己的宽度、贴到行尾 —— **窄取值类**（端口格 / 毫秒格 /
      下拉选择器）拉满会变成「三个数字撑满半屏」，比对齐更难看。此时**余量给标签**
      （``label_width`` 随之不生效）：定宽标签右边会空出一段，控件再靠右就成了两段。

    标签超过 ``label_width`` 时**换行**而不是把这一行撑宽（见 :func:`form_label`）；
    标签本身就长（如「监控网卡 / 匹配网关（都写 = 同时满足才命中）」）时改用
    :func:`form_row_stacked` —— 那是**另一种排法**，不是这一种的特例。
    """
    row = QHBoxLayout()
    row.setSpacing(metrics.GAP_MD)
    lbl = form_label(label, parent)
    if control_fill:
        lbl.setFixedWidth(label_width)
        row.addWidget(lbl)
        row.addWidget(control, stretch=1)
    else:
        row.addWidget(lbl, stretch=1)
        row.addWidget(control)
    return row


def form_row_stacked(
    label: str,
    control: QWidget,
    parent: QWidget | None = None,
) -> QWidget:
    """「标签在上、控件在下」的表单行 —— 长标签的规格化排法。

    与 :func:`form_row` 是**同一件事的两种排法**，判据是标签长度：定宽 78 装得下就用
    并排（横向省高度），装不下（含括号说明的长句、窄栏里的表单）就用堆叠 —— 否则那个
    定宽标签要么被截断、要么把左边一列撑到没边。

    返回的是**一个 widget**（不是 layout），只有两个后果都对得上才配这个形状：
    ① 整组能一刀隐藏 —— 表单里「接管模式才需要填的那几格」要连着标签一起隐去，
    只隐控件会留下一枚孤零零的标签；② ``addWidget`` 的 stretch 参数照用。
    间距取 ``GAP_XS``：标签要与**它自己管的那格**贴得比与上一行更近，否则标签会读成
    上一行的尾巴（原先各页写 2 / 6 不等）。

    加这个口之前，堆叠排法在各页是「``addWidget(_field_label(...))`` 之后再
    ``addWidget(控件)``」两句话，靠调用方肉眼保证顺序与间距；现在一句话说完。
    """
    box = QWidget(parent)
    col = QVBoxLayout(box)
    col.setContentsMargins(0, 0, 0, 0)
    col.setSpacing(metrics.GAP_XS)
    col.addWidget(form_label(label, box))
    col.addWidget(control)
    return box


def choice_hint(
    combo: QComboBox,
    hints: dict,
    parent: QWidget | None = None,
) -> QLabel:
    """跟着下拉**当前选中项**变的一句话说明（不是「把所有选项的释义摊一遍」）。

    为什么要有这个口（2026-09-23 观感返工）：快捷键编辑屏原先在反馈下拉下面常驻
    **84 个字**、把三个选项的释义一次说完 —— 一屏里控件 4 个、灰字两大块，扫一眼先
    看到的是字不是控件。释义属于**你选中的那一项**，没选的那两项此刻不需要知道，
    它们该待在下拉每项的 tooltip 里（调用方自己挂）。

    ``hints`` 以选项的**取值**（``itemData``）为键：显示名是给人读的标签，取值才是
    存下来的东西 —— 标签改了不该让释义断链。缺一个取值**当场报错**而不是留空：
    空释义比没有更糟（用户会以为这一档就是什么都不做）。
    """
    lbl = note("", parent)
    lbl.setWordWrap(True)

    def _sync(_index: int = 0) -> None:
        value = combo.currentData()
        if value not in hints:
            raise KeyError(
                f"选项 {value!r} 没有释义；已给的取值 {sorted(hints)}"
            )
        lbl.setText(hints[value])

    combo.currentIndexChanged.connect(_sync)
    _sync()
    return lbl

