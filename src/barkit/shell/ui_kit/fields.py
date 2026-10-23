"""取值控件：下拉 / 档案选择 / 单选按钮组 / 数值格。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import (
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QPainter,
    QPaintEvent,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLineEdit,
    QSizePolicy,
    QSpinBox,
    QWidget,
)

from barkit.shell import styles
from barkit.shell.design import metrics

from .buttons import tool_button

if TYPE_CHECKING:
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QPushButton


def apply_combo_style(combo: QComboBox) -> None:
    """统一「下拉选择器」的 QSS（ProfileSelector / ChoiceSelector 共用，防两处漂移）。

    弹层刻意**不加**自定义 QSS（回归 macOS 原生渲染）：此前浅紫高亮叠在原生弹层上
    观感怪异。换主题时调用方负责再调一次。
    """
    t = styles.TOKENS[styles.current_theme()]
    combo.setStyleSheet(f"""
        QComboBox {{
            background: transparent;
            border: 1px solid {t['border']};
            border-radius: {metrics.RADIUS_SM}px;
            padding: 0 {metrics.FIELD_CLEAR_PAD_X[0]}px 0 {metrics.FIELD_CLEAR_PAD_X[1]}px;
            color: {t['text2']};
            font-size: {metrics.FONT_BODY}px;
        }}
        QComboBox:hover, QComboBox:focus {{
            border: 1px solid {t['accent_text']};
            color: {t['text1']};
        }}
        QComboBox:disabled {{
            color: {t['text3']};
            border-color: {t['border']};
        }}
        QComboBox::drop-down {{
            subcontrol-origin: padding;
            subcontrol-position: center right;
            right: 10px;
            width: 14px;
            border: none;
        }}
        /* 箭头**不在 QSS 里画**：QSS 的 box 模型没有 shape，border 三角画出来是
           一个实心块（实测放大截图确认）。箭头由 ``ComboField.paintEvent`` 补画 ——
           上面这条 drop-down 一接管，Qt 就不再画默认箭头，**必须有人补**：
           下拉与输入框现在是同一个模子，没箭头就读成「一个输入框」。
    """)


class ComboField(QComboBox):
    """设置页的下拉格；**滚轮只滚页面，不改选中项**（与 ``NumberField`` 同一条口径）。

    原生 ``QComboBox`` 与 ``QSpinBox`` 是同一类坑，且更难察觉（2026-09-21 顺带实测）：
    焦点停在别处时滑过它的滚轮照样把选中项 1 → 0 改掉，并把这一次滚动 ``accept``
    掉 ⇒ 页面既不动、选中项又变了；而它的默认 ``focusPolicy`` 只是 ``TabFocus``
    （实测值 1），连「被聚焦」的迹象都没有 —— 用户只会看见「主图标」之类的一项
    莫名换了。

    唯一例外：**弹出列表开着时**把滚轮还给它（那时滚轮是在翻列表，不是翻页面）。

    **样式与高度自带**（2026-09-22）：此前 ``apply_combo_style`` 只被
    ``ProfileSelector`` / ``ChoiceSelector`` 两处调用，其余下拉（hosts 管理面、
    设置页表单）拿到的是**裸 ``QComboBox``** —— macOS 原生渲染，24px、另一套描边与
    底色，与同一行的输入框 / 按钮都不是一回事（用户上报「下拉与其它元素不协调」）。
    记住调一个函数是守不住的 ⇒ 由本类自己戴上（高度同 ``metrics.CONTROL_H``）。
    """

    def __init__(self, parent: "QWidget | None" = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(metrics.CONTROL_H)
        apply_combo_style(self)
        # 下拉的 QSS 是**按主题烤色**的（不是令牌引用）⇒ 换肤要重设。绑**实例方法**
        # 而不是 lambda：PySide6 对 bound method 会在控件销毁时自动断开连接，
        # 单例信号不会吊住一个已经被删的控件（lambda 会，且调用时抛 RuntimeError）。
        styles.theme_changed.connect(self._restyle)

    def _restyle(self, _theme: str | None = None) -> None:
        apply_combo_style(self)

    def paintEvent(self, event: "QPaintEvent") -> None:  # type: ignore[override]
        super().paintEvent(event)      # QSS 负责框、底色与当前项文字
        # 右侧补画一枚小 chevron（见 apply_combo_style 里那条注释：QSS 接管
        # drop-down 后 Qt 不画默认箭头，border 三角又画不出）。颜色**现读**令牌，
        # 不在换肤时缓存 —— 自绘件在 paintEvent 里实时取色的既有做法。
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        name = "text3" if not self.isEnabled() else "text2"
        painter.setPen(QPen(
            QColor(styles.token(name, styles.current_theme())),
            1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin,
        ))
        cx = self.width() - 17.0       # drop-down 槽位：右边距 10 + 槽宽 14 的中心
        cy = self.height() / 2.0
        path = QPainterPath()
        path.moveTo(cx - 3.5, cy - 1.5)
        path.lineTo(cx, cy + 2.5)
        path.lineTo(cx + 3.5, cy - 1.5)
        painter.drawPath(path)

    def wheelEvent(self, event: "QWheelEvent") -> None:  # type: ignore[override]
        if self.view().isVisible():      # 弹层开着 ⇒ 交给列表自己滚
            super().wheelEvent(event)
            return
        event.ignore()
    # 弹层落屏：本类**一个字的抢回都不做**（2026-10-09 定案）。挂着 OpenDisplay
    # 虚拟屏时，AppKit 会把「独立顶层弹层窗口」归位到那块屏，且无法阻止（事前布置 /
    # 事后 move / orderFront_ / CanJoinAllSpaces / cascade point / level 降档六条
    # 实测全部无效）—— 抢回一次它就归位一次，对抗本身变成用户看见的弹跳。
    # 出路是**不弹**：选项少时用 :class:`ChoiceBar`（摊在行里）。别再往这里加对抗代码。


class ProfileSelector(QWidget):
    """默认输入项选择器：原生 QComboBox + QSS 样式（替代自绘弹层）。

    原生 QComboBox 自动处理宽度、长文本省略号与弹层定位，比此前自绘方案更稳健，
    也消除了「按钮文字裁切」「弹层过宽」两个 bug。弹层回归 macOS 原生渲染
    （系统选中高亮），不再叠加自定义 QSS，避免浅紫高亮在原生弹层上观感怪异。
    """

    selected = Signal(str)

    def __init__(
        self, names: list[str], current: str | None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._names = list(names)
        self._current = current or (names[0] if names else "")
        self.setFixedHeight(metrics.CONTROL_H)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._combo = ComboField(self)
        self._combo.setFixedHeight(metrics.CONTROL_H)
        self._combo.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._combo.addItems(self._names)
        if self._current in self._names:
            self._combo.setCurrentText(self._current)
        self._combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self._combo.currentTextChanged.connect(self._on_changed)
        layout.addWidget(self._combo)
        if not self._names:
            self._combo.setEnabled(False)
        self.refresh_theme()

    def set_names(self, names: list[str], current: str | None = None) -> None:
        """重设候选与当前项（运行期刷新用，供框架行的动态刷新调用）。

        **程序化变更不发 ``selected``**：刷新不等于用户选择。发信号会让插件把
        「框架重读了一次候选」当成「用户改选了」，进而误跑副作用（如把当前项
        写进配置）。故整段阻断信号，只同步内部状态。

        传入的 ``current`` 不在候选里时落到第一项（与构造期同款回落）：候选集合变过
        之后旧值常常已经不在册，留一个解析不了的值不如落到第一项。
        """
        self._names = list(names)
        want = current if current is not None else self._current
        if want not in self._names:
            want = self._names[0] if self._names else ""
        blocked = self._combo.blockSignals(True)
        self._combo.clear()
        self._combo.addItems(self._names)
        if want:
            self._combo.setCurrentText(want)
        self._combo.setEnabled(bool(self._names))
        self._combo.blockSignals(blocked)
        self._current = want

    def current(self) -> str:
        return self._current

    def refresh_theme(self) -> None:
        """按当前主题重设 QSS，由面板在主题切换时统一调用。"""
        apply_combo_style(self._combo)

    def _on_changed(self, text: str) -> None:
        self._current = text
        self.selected.emit(text)


class ChoiceSelector(QWidget):
    """按**取值**选择的单栏下拉（设置页用，如「菜单栏图标 → 底图来源」）。

    与 ``ProfileSelector`` 的唯一差别在「选中的是哪个」：

    - ``ProfileSelector`` 按**显示文本**选中（面板里用户看到的输入项名）；
    - 本类按**取值**选中（配置里存的东西，如状态 topic），显示名只是标签 ——
      组件改名不该让已存下的取值失效，故用 ``addItem(label, userData)`` 承载取值、
      信号直接发取值。

    取值不在候选里（如来源组件被禁用/卸载）时落到第一项，由调用方按 ``current()``
    回写为「默认」——回落规则与渲染侧同一条：**绝不留下解析不了的取值**。
    """

    selected = Signal(str)

    def __init__(
        self,
        choices: "list[tuple[str, str]]",
        current: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """``choices``：``[(显示名, 取值), ...]``（第一项通常是「BarKit 默认」= 空串）。"""
        super().__init__(parent)
        self.setFixedHeight(metrics.CONTROL_H)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._combo = ComboField(self)
        self._combo.setFixedHeight(metrics.CONTROL_H)
        self._combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        for label, value in choices:
            self._combo.addItem(label, value)
        index = self._combo.findData(current) if current is not None else 0
        self._combo.setCurrentIndex(index if index >= 0 else 0)
        self._combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self._combo.currentIndexChanged.connect(self._on_index_changed)
        layout.addWidget(self._combo)
        # 只有「BarKit 默认」一项时无处可选（当前无组件声明底图义务）→ 置灰
        self._combo.setEnabled(self._combo.count() > 1)
        apply_combo_style(self._combo)

    def current(self) -> str:
        """当前选中项的**取值**（空串 = BarKit 默认）。"""
        return self._combo.currentData() or ""

    def set_current(self, value: str) -> None:
        """程序化选中某个**取值**，**不发 ``selected``**。

        ``__init__`` 只负责首屏那一次，而「选择器」本身不该只能在构建期定一次 ——
        需要重新装载同一组控件的场景是存在的（配置窗体切换编辑对象时把整张表单重灌）。
        取值不在候选里时落到第一项，与 ``__init__`` 同一条回落规则：**绝不留下解析
        不了的取值**。

        不发信号是刻意的：``selected`` 的含义是「**用户**改选了」，装载再发一次，
        调用方就得为「装载」与「用户动作」写两套判据（配置窗体会因此白写一次盘 ——
        用户只是切换了编辑对象）。
        """
        index = self._combo.findData(value)
        if index < 0:
            index = 0
        if index == self._combo.currentIndex():
            return
        blocked = self._combo.blockSignals(True)
        self._combo.setCurrentIndex(index)
        self._combo.blockSignals(blocked)

    def refresh_theme(self) -> None:
        apply_combo_style(self._combo)

    def _on_index_changed(self, _index: int) -> None:
        self.selected.emit(self.current())


class ChoiceBar(QWidget):
    """横向单选按钮组：**选项少**（一行摊得开）时的取值控件，替代 ``ChoiceSelector``。

    为什么要有这一档（2026-10-09 真机）：``QComboBox`` 的弹出列表是一个**独立顶层
    窗口**（原生类 ``QNSPanel``），而 macOS 会把「新顶层窗口」按 ``NSScreen.mainScreen()``
    重新归位 —— 本机那块常态是 iPad 虚拟屏（挂着 OpenDisplay），于是「窗口在内建屏、
    列表弹在 iPad 屏」。六条抢回手段实测全部无效，且**每移动一次系统就归位回来一次**
    ⇒ 抢回本身成了用户看见的弹跳。真正管用的是**不弹**：把选项直接摊在行里，一个独立
    窗口都不建，系统无从归位。

    取值口径与 ``ChoiceSelector`` 逐条对齐 —— ``selected`` 发的是**取值**、取值不在候选
    里回落第一项、``set_current`` **不发**信号 —— 两者可以互换，区别只有「摊开」还是
    「收起」。**一行摊不开（选项多 / 标签长）仍用下拉**：硬摊会把整张表单顶宽。

    视觉走 ``pill-chip`` / ``pill-chip-on``（设置窗「插件」页那排显示位芯片的长相）：
    亮 = 选中。只共用长相 —— 那排是**多选**开关，这里是**单选**。
    """

    selected = Signal(str)

    def __init__(
        self,
        choices: "list[tuple[str, str]]",
        current: str | None = None,
        parent: QWidget | None = None,
        *,
        hints: "dict[str, str] | None" = None,
    ) -> None:
        """``choices``：``[(显示名, 取值), ...]``（第一项通常是缺省）。

        ``hints``：取值 → 悬停释义。释义**属于你选中的那一项**，没选的那几项此刻不需要
        知道 ⇒ 不塞进标签（与 ``forms.choice_hint`` 同一条裁定）。缺一项只是那一枚没有
        tooltip，不报错 —— 它不像 ``choice_hint`` 那样是**必须**有的一份文案。
        """
        super().__init__(parent)
        self._values = [value for _label, value in choices]
        self._current = (
            current if current in self._values
            else (self._values[0] if self._values else "")
        )
        self._hints = dict(hints or {})
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(metrics.GAP_SM)
        self._btns: "list[tuple[QPushButton, str]]" = []
        for label, value in choices:
            btn = tool_button(label, "pill-chip", size="chip", parent=self)
            hint = self._hints.get(value)
            if hint:
                btn.setToolTip(hint)
            # 捕获值各自带默认参数：同一函数体里互为兄弟的 lambda 读不到彼此的形参。
            btn.clicked.connect(lambda _checked=False, v=value: self._pick(v))
            row.addWidget(btn)
            self._btns.append((btn, value))
        # 芯片左对齐、余量甩到右端 —— 摊开之后它们是**窄取值控件**，与下拉同一条排法。
        row.addStretch(1)
        self._repaint()

    def current(self) -> str:
        """当前选中项的**取值**（空串 = 那档的取值本来就是空串）。"""
        return self._current

    def set_current(self, value: str) -> None:
        """程序化选中某个**取值**，**不发 ``selected``**。

        与 ``ChoiceSelector.set_current`` 同一条口径：``selected`` 的含义是「**用户**
        改选了」，装载再发一次，调用方就得为「装载」与「用户动作」写两套判据。
        取值不在候选里时落到第一项 —— **绝不留下解析不了的取值**。
        """
        if value not in self._values:
            value = self._values[0] if self._values else ""
        self._select(value)

    def _pick(self, value: str) -> None:
        """点某一枚芯片：**只有选中项真的变了才发信号**。

        再点一次当前那枚不改任何东西，也不该让插件把「又点了一遍」当成一次用户改选
        （``_on_ask_changed`` 那类槽会跟着跑一遍副作用）。
        """
        if value == self._current:
            return
        self._select(value)
        self.selected.emit(value)

    def _select(self, value: str) -> None:
        if value == self._current:
            return
        self._current = value
        self._repaint()

    def _repaint(self) -> None:
        """每枚芯片按「亮 = 选中」刷档。

        改 ``objectName`` 之后必须 ``unpolish + polish`` —— 否则 QSS 还按旧名字画，
        界面上就是「点了没反应」（状态已改、颜色没变）。与设置窗那排显示位芯片
        （``shell/settings/helpers.py::_apply_chip``）是同一条纪律。

        颜色全部来自**全局 QSS**（按 ``objectName`` 匹配）⇒ 换肤不需要本类做任何事：
        没有烤进控件自己的样式表，全局那一次重设会把它们一起刷掉。这也是它不像
        ``ChoiceSelector`` 那样需要 ``refresh_theme()`` 的理由。
        """
        for btn, value in self._btns:
            btn.setObjectName("pill-chip-on" if value == self._current else "pill-chip")
            style = btn.style()
            style.unpolish(btn)
            style.polish(btn)


class NumberField(QSpinBox):
    """设置页的整数输入格（端口 / 秒数 / 毫秒）；**滚轮永远只滚页面，不改值**。

    插件侧**一律**用这个类，不直接构造 ``QSpinBox`` / ``QComboBox``（门禁
    ``test_plugin_ui_has_no_bare_value_widget`` 守）。为什么必须挡住滚轮
    （2026-09-21 用户上报「滚设置页时端口被改掉」，离屏实测逐步复现）：

    - **滚轮不需要焦点就能改值**：实测焦点正停在同一个窗口里的 ``QLineEdit`` 上，
      滚轮滑过数字格照样 1080 → 1081。用户说的「很容易被聚焦」在真机上还有一层
      —— ``QSpinBox`` 默认 ``focusPolicy`` 是 ``Qt.WheelFocus``（实测值 15），
      即「允许滚轮取得焦点」；离屏合成事件复现不出这一步（焦点不被抢），但这项
      策略本就不是我们想要的，故一并收回；
    - 事件被它 ``accept`` 掉 ⇒ 这一次滚动**被吞了**（滚动条不动、页面停在原处，
      用户只当没滚上，于是再滚一次 ⇒ 又改一格）；
    - 值改完**会落盘**：本仓设置项一律「改完即写、没有保存键」，而
      ``editingFinished`` 在**失焦时补发**（实测：滚轮当次不发，失焦才发）⇒
      一次误滚就是一个被写进配置文件的错端口，界面上**没有任何提示**。

    挡法两条，缺一不可：

    - ``StrongFocus``：收回「滚轮能给焦点」这项策略（点击 / Tab 仍照常聚焦）；
    - ``wheelEvent`` 一律 ``ignore()``：事件交回父级 ⇒ 滚动区照常滚动。

    **聚焦之后也不开滚轮**：点进格子再滚，光标正压在格子上，这次滚动的意图同样是
    翻页，与没聚焦时没有区别，而代价是配置被静默改写。改值的入口留三处足够
    （键入、↑↓、右侧步进箭头）——与 ``QLineEdit`` 一致：文本格子上滚轮也只滚页面，
    数字格子没理由不一样。

    **高度自己钉在 ``CONTROL_H``**（2026-10-08 补，与 :class:`ComboField` 同款）：
    ``QAbstractSpinBox`` 自己的 ``sizeHint`` 比同族的 ``QLineEdit`` 高 5px（实测
    样式表下 33 对 28）—— 它按「上下两枚步进按钮 ＋ 两行字的字高」算，而本仓的
    输入格本来就是**一行**。后果是同一张表单里数字格与文本格**参差**（待办详情页
    13 格里有 3 格高出一截，用户看到的就是「这一列不齐」），而 ``metrics`` 里那句
    「``ChoiceSelector`` / ``ComboField`` / ``NumberField`` 都钉在这一格上」正是
    它本该如此的意思。钉法走 ``setFixedHeight`` 而不是 QSS ``max-height``：QSS 那
    几个高度属性说的是**内容盒**（``min-height: 18`` 是「内容 18 ＋ 内边距 8 ＋
    描边 2 ＝ 28」），拿它当总高压不下去。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedHeight(metrics.CONTROL_H)

    def wheelEvent(self, event: "QWheelEvent") -> None:  # type: ignore[override]
        """滚轮只滚页面：不吃事件、不改值（``ignore()`` 后由父级滚动区接手）。"""
        event.ignore()


class DecimalField(QDoubleSpinBox):
    """设置页的**一位小数**输入格（如「工作小时数」填 ``10.5``）；滚轮口径同 :class:`NumberField`。

    2026-09-27 用户上报「工作小时数要指出 10.5」：推算下班那半边（``scheduler``）本来就按
    ``float(hours) * 3600`` 算，只有输入这一端被 ``QSpinBox`` 的整数格挡住 —— 键进去的
    10.5 会被 Qt 截成 10，用户看到的「只能填整数」就是这么来的。默认保留一位小数、步进
    0.5，够表达「半小时」这一档，也不会长出 10.25 这种没人用的精度。

    **必须同时进 ``styles.build_stylesheet`` 的输入格一族**：样式表里 ``QLineEdit, QSpinBox``
    那两条**不覆盖** ``QDoubleSpinBox`` —— 它是 ``QSpinBox`` 的**兄弟**（同出
    ``QAbstractSpinBox``），不是子类。漏了就是「保持 macOS 原生外观 ⇒ 聚焦时边框不变色」
    —— 与 2026-09-27 定位到的那个原生时间格是同一个坑。（类型选择器本身**是**命中子类的：
    ``QSpinBox`` 覆盖 ``NumberField``，见 ``_apply_tone`` 那条注释与 2026-09-30 实测。）
    门禁 ``test_every_ui_kit_input_subclass_is_styled`` 守这条。

    插件侧一律用这个类，不直接构造 ``QDoubleSpinBox``（门禁
    ``test_plugin_ui_has_no_bare_value_widget`` 的名单里已经有它）。

    高度口径与 :class:`NumberField` **同一处**（它俩同出 ``QAbstractSpinBox``，
    那个多出来的 5px 是同一份 ``sizeHint`` 算出来的）—— 小数格长在整数格旁边，
    两格不一样高比与文本格不一样高更显眼。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedHeight(metrics.CONTROL_H)
        self.setDecimals(1)
        self.setSingleStep(0.5)

    def textFromValue(self, value: float) -> str:  # type: ignore[override]
        """整数不带小数点（``9`` 而不是 ``9.0``），带小数才写出那一位。

        ``value()`` 一律回 float ⇒ 默认写法会把「9 小时」显示成 ``9.0``；用户填的 9 与 10.5
        看起来像两种精度。显示与摘要行（``settings._fmt_hours``）共用 ``:g`` 一条口径。
        """
        return f"{value:g}"

    def wheelEvent(self, event: "QWheelEvent") -> None:  # type: ignore[override]
        """滚轮只滚页面：不吃事件、不改值（``ignore()`` 后由父级滚动区接手）。"""
        event.ignore()


def search_field(
    placeholder: str = "搜索",
    *,
    on_changed: "Callable[[str], None] | None" = None,
    parent: QWidget | None = None,
) -> QLineEdit:
    """列表过滤用的搜索格 —— 「输入即筛选」这一件事的**唯一构造口**。

    **为什么收进组件盒**（2026-10-16）：同一件事在三个页面各手搓了一遍 ——
    ``shell/settings/search.py:48``、``shell/hotkeys_page.py:229``、
    ``plugins/hosts_guard/manager_table.py:59``。三处都写「``QLineEdit`` ＋
    ``setPlaceholderText`` ＋ ``setClearButtonEnabled(True)`` ＋ 接 ``textChanged``」，
    但清空键、占位文案、是否即时触发各差一点 —— 与 ``form_label`` 当初收掉的
    五份 ``_field_label`` 是同一类病。

    高度与同类单行输入格**同源**（全局 QSS 的 ``QLineEdit`` 一族，钉在
    ``metrics.CONTROL_H``）：本件不声明任何样式、也不钉高度 —— 样式归 ``styles.py``、
    尺寸归 ``metrics``，这是组件盒的既定分工。

    ``on_changed`` 拿到的是**原始文本**（不 ``strip``、不转小写）：过滤口径归调用方，
    组件盒不替它决定。不传就只返回控件，接线由调用方自己接。
    """
    edit = QLineEdit(parent)
    edit.setPlaceholderText(placeholder)
    edit.setClearButtonEnabled(True)
    if on_changed is not None:
        edit.textChanged.connect(on_changed)
    return edit


def check_field(
    text: str,
    *,
    checked: bool = False,
    on_toggled: "Callable[[bool], None] | None" = None,
    parent: QWidget | None = None,
) -> QCheckBox:
    """**表单 / 设置页里的一个勾选项** —— 与 :func:`~barkit.shell.ui_kit.forms.form_row`
    配套的那一格。

    与 :class:`~barkit.shell.ui_kit.primitives.Switch` **不是互相替代的关系**，别混用：

    | | 形态 | 契约 | 什么时候用 |
    | --- | --- | --- | --- |
    | `Switch` | iOS 拨动开关（自绘） | **不翻自己** —— ``setChecked`` 只写状态、不发信号，翻由消费者做 | 要「开关」这个意象：一枚控件单独管一件事的开与关 |
    | `check_field` | 系统勾选框（原生） | Qt 自己翻转并 emit ``toggled(bool)`` | 要「勾选」这个意象：一组里选若干项，或一句话前面带个框 |

    选哪个看**意象**、不看**何时落盘** —— 这是 2026-10-16 全仓盘点后校正的口径：
    本表起初按落盘时机分（「改一次落一次盘」归 `Switch`、「跟着保存键」归 `check_field`），
    实测两样都对不上。`route_guard` 的网络配置与 `ipad_tunnel` 的隧道开关都是**改一次
    落一次盘**，用的却一直是勾选框；反过来，拨动开关也能跟着保存键落盘。落盘时机由
    **接线**决定（``on_toggled`` 里直接写盘，还是交给保存键统一提交），与控件形态无关。

    两种契约**故意不同**：`Switch` 那套「不翻自己」是自绘控件的必然（见
    ``primitives.py`` 该类的 docstring），漏接一处就是一枚死开关；而原生 ``QCheckBox``
    由 Qt 负责翻转，消费者再多做一层会翻两次。收这个口统一的是**外观**（QSS 的
    ``QCheckBox`` 段）与**装配**，不是信号语义。

    ``checked`` 只是初值。``on_toggled`` 拿到的是翻转后的**新值** —— 别在回调里用
    ``isChecked()`` 反推历史，信号发出时控件状态已经是新值。
    """
    box = QCheckBox(text, parent)
    box.setChecked(checked)
    if on_toggled is not None:
        box.toggled.connect(on_toggled)
    return box

