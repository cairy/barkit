"""状态行视图 ``_StatusRowView`` 与它的副标题取材单点。

``shell/panel.py`` 拆包（2026-10-01）：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtCore import (
    Qt,
    QPoint,
    QRect,
    QTimer,
)
from PySide6.QtWidgets import QWidget

from barkit.shell.ui_kit import (
    CandidateMenu,
    MenuButton,
    ModeBadge,
    ProfileSelector,
    RecordRow,
    Switch,
    relay_action_result,
    tool_button,
)
from barkit.contribution.specs import (
    ButtonSpec,
    ChoiceSpec,
    IconName,
    MenuItem,
    MenuSpec,
    ModeBadgeSpec,
    StatusRowSpec,
    SwitchSpec,
)

from .constants import _BUTTON_KIND, log
from .widgets import _ValueBadge


def row_subtitle_text(spec: "StatusRowSpec | None", fallback: str) -> str:
    """**行副标题文案的唯一算法**（决议 D18 文案只有一份 ＋ 契约 §2.7）。

    声明了行级 ``subtitle`` 回调就用它；未声明（含该组件没有行声明）回退代表态那句
    ``fallback``。面板行与标题栏图标的悬停提示都从这里取 —— 两处显示的是同一句话，
    不各算一份（◷ 2026-09-17 用户裁定：标题栏 tips 不再逐台枚举）。

    回调是**纯读取**（不得有副作用）；此处仍兜一层异常——副标题是「一眼扫过」的位置，
    它算不出来不该让整行连名字都画不出（回退代表态文案，并记日志）。
    """
    fn = getattr(spec, "subtitle", None) if spec is not None else None
    if fn is None:
        return fallback
    try:
        return fn() or ""
    except Exception:  # noqa: BLE001 —— 视图边界隔离，坏回调不得拖垮整行渲染
        log.exception("插件 %s 的行级 subtitle 回调抛异常，回退代表态 summary",
                      getattr(spec, "plugin_id", "?"))
        return fallback


def _safe_call(what: str, fn, default):
    """跑一个行内读取回调，抛异常只记不冒（视图边界隔离的同款口径）。

    为什么要单独一个 helper：``refresh_dynamic`` 里四路回调（开关 / 下拉 / 模式 /
    可点）是**并列的兄弟**，任何一路坏了都不该掐掉其余三路 —— 写成一个 try 包全体
    会把「这一个坏了」放大成「这一行的动态全部不刷新」。
    """
    try:
        return fn()
    except Exception:  # noqa: BLE001 —— 坏回调不得拖垮整行刷新
        log.exception("行内刷新回调 %s 抛异常", what)
        return default


class _StatusRowView(RecordRow):
    """框架渲染的状态行（§5.3）：图标盒（身份图标）+ 状态角标 + 名称+模式徽标 + 副标题 + 右侧控件。

    行骨架（图标盒 / 主标题行 / 副标题 / 尾部控件槽）来自 ``ui_kit.RecordRow`` ——
    与设置页的列表行**同一份实现**。本类只加两样**面板特有**的东西：压在图标盒右上角的
    状态角标，以及按 ``StatusRowSpec.controls`` 分派的右侧控件。

    三处显示位的分工（决议 D16）——这一处一次说完三件事：

    - 主位图标 = **身份图标**（``ComponentSpec.icon``，由面板注入；固定不随状态变）
      ——「是谁」；
    - 右上角标 = **取值图标**（代表态解析，``state_view.resolve``）——「什么状态」；
    - 角标颜色 = **级别色**——「多坏」。

    名称与图标都不在行声明里给（决议 D14）：名称取 ``StatusRowSpec.title``（省略时
    框架已用 ``Plugin.name`` 补上），图标取组件身份。插件不再自拼 StatusRow/Badge/Switch；
    整行可点由 ``on_click`` + ``get_clickable`` 决定。
    """

    def __init__(
        self,
        spec: "StatusRowSpec",
        parent: "QWidget | None" = None,
        on_click=None,
        identity_icon: "IconName | None" = None,
    ) -> None:
        # ``on_click`` 是框架注入的整行点击（未声明 on_click 的插件若声明了
        # DetailEntrySpec，由框架合成「打开详情页」）；None 时回退 spec.on_click。
        # 两种来源都**原样**交给行：行存的是插件回调本体（不存「回本行的闭包」）。
        # 整行点击的结局**要回访**（2026-09-28 接通），但走的是**晚一拍**那条路
        # （`HoverRow.mousePressEvent` → `ui_kit.defer_result_to_host`，原因与指向在那儿）；
        # 行内控件那三处走**当场**的 `relay_action_result`。
        effective = on_click if on_click is not None else spec.on_click
        # 骨架（``ui_kit.RecordRow``）：图标盒 ＋ 主标题行（标题 ＋ 徽标）＋ 副标题 ＋
        # 右侧控件槽。与设置页的列表行**同一份实现** —— 同一块面板里两种行、以及面板与
        # 设置页之间，都不该各写一遍再各自漂移。
        #
        # 身份图标（静态）**可能晚于本行注入**（组件身份与状态行是两个注入点，见
        # ``StatusPanel._sync_row_identity_icons``）：故这一格允许为空，空时由骨架的
        # 兜底位画**通用齿轮**（``icon_fallback``）—— 每个组件都有一行，空图标位读起来
        # 像坏了。``set_identity_icon`` 供回填。
        super().__init__(title=spec.title, icon=identity_icon, icon_fallback=IconName.GEAR,
                         on_click=effective, clickable=effective is not None, parent=parent)
        self._spec = spec
        self._badge_icon: "IconName | None" = None     # 代表态取值图标（动态）
        self._badge_level: "str | None" = None         # 代表态级别（动态）
        # 本行当前打开的候选弹层（徽标 / 行内菜单两处共用这一格）。**必须持引用**：弹层走
        # 的是非阻塞 ``popup()``（§5 第 17 条收口），没有 ``exec()`` 的嵌套事件循环替它
        # 保命，而调用处只有个局部变量 —— 持一格最省心地钉住它，直到下次点击换掉。
        self._candidate_menu: "CandidateMenu | None" = None
        # 状态角标：**整行的**子控件（不是 36×36 图标盒的子），定位在 ``_ico_box`` 右上角
        # 跨出，像 macOS 通知红点那样「咬」在角上 —— 挂整行才不被图标盒几何裁掉。
        # 构造时布局尚未结算盒坐标，故延迟到首帧 + resize 时重定位。
        self._badge = _ValueBadge(self)
        QTimer.singleShot(0, self._position_badge)

        # 模式徽标落在骨架的标题行里（紧随标题）；可点与否由声明决定：二元切换
        # （on_click）或候选列表（items / get_items）都算有动作；两者都没有 ⇒ 纯装饰，
        # 给箭头光标、点了也没反应。
        self._mode: "ModeBadge | None" = None
        if spec.mode is not None:
            clickable = (
                spec.mode.on_click is not None
                or bool(spec.mode.items)
                or spec.mode.get_items is not None
            )
            self._mode = ModeBadge(
                label=spec.mode.label, accent=spec.mode.accent,
                clickable=clickable, parent=self,
            )
            self._mode.clicked.connect(self._on_mode_click)
            self.set_badge(self._mode)
        # 副标题初值：声明了行级 subtitle 的组件用它（契约 §2.7 / D24）。静态备选文案字段
        # 已随决议 D18 删除——没有运行时快照就留空，由首个状态刷新填上。
        self.set_subtitle(self.subtitle_text(""))

        # 右侧控件（决议 D6：0–2 个，声明顺序即从左到右）
        # 按类型分派渲染器（SwitchSpec / MenuSpec / ButtonSpec）。控件只负责「显示 +
        # 发信号」，业务回调与弹层由本视图分派 —— 与 ModeBadge 同口径（H3）。
        self._switches: "list[tuple[Switch, SwitchSpec]]" = []
        self._menus: "list[tuple[MenuButton, MenuSpec]]" = []
        # 按钮也留一份引用（与上面两列表对称）：三者在渲染上是同级的槽位，测试要逐槽
        # 点击核对回调绑得对不对（多槽晚绑定的老坑，决议 D6）。不写类型注解是为了不为此
        # 多引一个 QPushButton 名字（`tool_button` 的返回类型已经说明了它是什么）。
        self._buttons = []
        # 行内下拉（ChoiceSpec）：与上面三者同级的第四种槽位，同样留引用供测试逐槽核对。
        self._choices: "list[tuple[ProfileSelector, ChoiceSpec]]" = []
        for ctrl in spec.controls:
            if isinstance(ctrl, SwitchSpec):
                sw = Switch(self)
                sw.setChecked(bool(ctrl.checked))
                sw.setEnabled(bool(ctrl.enabled))
                if ctrl.tooltip:
                    sw.setToolTip(ctrl.tooltip)
                # 连的是**本行自己的绑定方法**，不是捕 ``self`` 的闭包：多槽下「点了哪一个」
                # 由 ``sender()`` 在槽里认领（见 ``_on_switch_clicked``），于是既没有晚绑定
                # 问题，也没有「控件攥着行」的环（理由见 `relay_action_result` 的 docstring）。
                # （``Switch.clicked`` 是**无参**信号，不要按 QPushButton 那样收 bool。）
                sw.clicked.connect(self._on_switch_clicked)
                self._switches.append((sw, ctrl))
                self.add_trailing(sw)
            elif isinstance(ctrl, MenuSpec):
                btn = MenuButton(self._menu_current(ctrl), self)
                # 同上：认槽位靠控件身份，不靠闭包固化 ctrl。
                btn.clicked.connect(self._on_menu_button_clicked)
                self._menus.append((btn, ctrl))
                self.add_trailing(btn)
            elif isinstance(ctrl, ButtonSpec):
                kind = _BUTTON_KIND.get(ctrl.variant)
                if kind is None:
                    # 未知 variant 不该静默变成另一种按钮：兜 ghost 但留痕
                    log.warning(
                        "ButtonSpec.variant=%r 未登记，回落 pill-ghost（插件 %s）",
                        ctrl.variant, spec.plugin_id,
                    )
                    kind = "pill-ghost"
                btn = tool_button(ctrl.label, kind=kind, parent=self)
                # 同上：首位吞掉 QPushButton.clicked 的 bool 参数（见 ``_on_button_clicked``）
                btn.clicked.connect(self._on_button_clicked)
                self.add_trailing(btn)
                self._buttons.append((btn, ctrl))
            elif isinstance(ctrl, ChoiceSpec):
                # 第四种行内控件（下拉选择器）：**切换只改选中**，业务动作归整行
                # ``on_click`` —— 与 MenuSpec 的分界见 ChoiceSpec 的 docstring。
                # 点下拉不会连带触发整行点击：QComboBox 自己吃掉按下、不冒泡（与
                # autotype 原先那把自绘行内的下拉同款，已由用例证明不穿透）。
                sel = ProfileSelector(
                    self._choice_items(ctrl), self._choice_current(ctrl), self
                )
                if ctrl.tooltip:
                    sel.setToolTip(ctrl.tooltip)
                if ctrl.width > 0:
                    sel.setFixedWidth(ctrl.width)
                sel.setEnabled(bool(ctrl.enabled))
                sel.selected.connect(self._on_choice_changed)
                self._choices.append((sel, ctrl))
                self.add_trailing(sel)
            else:
                log.debug(
                    "StatusRowSpec.controls 无对应渲染器，跳过 %s（插件 %s）",
                    type(ctrl).__name__, spec.plugin_id,
                )

        # 构建即按运行时态就绪：get_checked/get_enabled 是动态真相，
        # 不应等到首个状态快照才刷新（避免 off-network 下开关初始误显可用）。
        self.refresh_dynamic()
        self.refresh_theme()

    # ---- 行内控件 → 结果槽的三条中继（认槽位靠 ``sender()``，不靠捕 ``self`` 的闭包）----

    # 三条必须同款。连接里存了捕 ``self`` 的闭包，本行与控件就互相攥着 —— 没必要，
    # 且这一带有一条尚未定位的本机离屏崩溃（见 `ui_kit.relay_action_result` 的 docstring）。
    # 副作用是「闭包
    # 默认参数固化 ctrl」那套防晚绑定的写法一并退场 —— 槽位改由控件身份认领，晚绑定从
    # 结构上不可能发生（原先的坑见决议 D6）。

    def _on_switch_clicked(self) -> None:
        """行内开关被点：认领是哪一枚，把 ``SwitchSpec.on_toggle`` 的结局交给宿主。"""
        w = self.sender()
        for sw, ctrl in self._switches:
            if sw is w:
                relay_action_result(self, ctrl.on_toggle)
                return
        log.debug("行内开关的 sender 不在本行登记表内（插件 %s）", self._spec.plugin_id)

    def _on_button_clicked(self, _checked: bool = False) -> None:
        """行内按钮被点：同上。首位吞掉 ``QPushButton.clicked`` 带的 bool（与旧写法同）。"""
        w = self.sender()
        for btn, ctrl in self._buttons:
            if btn is w:
                relay_action_result(self, ctrl.on_click)
                return
        log.debug("行内按钮的 sender 不在本行登记表内（插件 %s）", self._spec.plugin_id)

    def _on_menu_button_clicked(self, _checked: bool = False) -> None:
        """行内下拉被点：认领是哪一枚，交给 ``_on_menu_click`` 弹候选列表（§2.5 选中即生效）。"""
        w = self.sender()
        for btn, ctrl in self._menus:
            if btn is w:
                self._on_menu_click(btn, ctrl)
                return
        log.debug("行内下拉的 sender 不在本行登记表内（插件 %s）", self._spec.plugin_id)

    def _position_badge(self) -> None:
        """把状态角标定位到图标盒右上角、略压入盒内（macOS 通知红点的「咬角」观感）。

        角标是整行的子控件（不是 36×36 图标盒的子），故不受图标盒几何裁剪；
        顶部跨出行顶的部分由行留白兜底，不会越出整行被外层裁掉。
        """
        ico = getattr(self, "_ico_box", None)
        if ico is None:
            return
        # badge 为 16px、圆盘（直径 14 / 半径 7）圆心在 badge 内 (8,8)。圆心内缩到
        # 图标盒右上角的对角点 (32,4)：8px 圆角弧的两个端点 (28,0) / (36,8) 到该点
        # 均为 5.66 < 7，整段圆角弧都被圆盘罩住。圆心压在角点 (36,0) 时两端点距 8，
        # 弧的两头会从圆盘后面露出来，圆角看着近似直角（2026-09-16 用户实机反馈）。
        self._badge.move(ico.x() + 24, ico.y() - 4)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._position_badge()

    def _on_mode_click(self) -> None:
        """模式徽标点击的**唯一分派点**：先跑声明的点击动作，再按 get_state 重读自刷新。

        徽标自己只 emit ``clicked`` 不持回调（H3），所以「点了做什么」全在这里：

        - 二元切换（``on_click``）：跑它，再按 ``get_state`` 重读 ``(label, accent)``
          —— 此前这条自同步从没生效过（徽标自己调回调、信号从不 emit）；
        - 候选列表（``items`` / ``get_items``）：弹原生菜单，**选中即生效**（契约 §2.5），
          选中后徽标上的字就是当前项（``label`` 兼任当前项，决议 D33）。

        弹层是**非阻塞**的（2026-09-17 收口）：不用 ``QMenu.exec()`` 的嵌套事件循环去
        回读选中项 —— 面板与设置窗同为置顶窗，那正是崩溃链路的成因（见
        ``ui_kit.FeedbackBar`` docstring）。「选中之后同步徽标」改挂在 ``on_picked`` 上；
        取消 / 点外部关闭时回调根本不来，天然什么都不做（原先那两行判空随之消失）。

        两类都不成立时构造期就不会接线上来（``clickable=False``），这里到不了。
        """
        mode = self._spec.mode
        if mode is None or self._mode is None:
            return
        if mode.on_click is not None:
            relay_action_result(self, mode.on_click)
            self._sync_mode_label(mode)
            return
        items = self._menu_items(mode)
        if not items:
            log.debug(
                "ModeBadgeSpec 候选列表为空，点击未展开（插件 %s，label=%r）",
                self._spec.plugin_id, mode.label,
            )
            return
        menu = CandidateMenu(
            items, current=mode.label, parent=self._mode,
            on_picked=lambda item: self._sync_mode_label(mode, chosen=item.label),
        )
        self._candidate_menu = menu          # 非阻塞 ⇒ 需本行替弹层保命，见 __init__ 注
        # 弹在徽标正下方：与 macOS 原生下拉一致，贴着触发控件
        anchor = QRect(self._mode.mapToGlobal(QPoint(0, 0)), self._mode.size())
        menu.popup_at(self._mode.mapToGlobal(QPoint(0, self._mode.height())), anchor)

    def _sync_mode_label(self, mode: "ModeBadgeSpec", chosen: str | None = None) -> None:
        """把徽标刷新到「当前项」：``get_state`` 优先，无它时退回选中项（决议 D33）。

        ``chosen`` 只有候选列表路径给得出 —— 二元切换没有「选中项」这个概念，只能靠
        ``get_state``；两者都没有时徽标保持原样（与原实现一致）。
        """
        if self._mode is None:
            return
        st = mode.get_state
        if st is not None:
            label, accent = st()
            self._mode.set_state(label, accent)
        elif chosen is not None:
            self._mode.set_state(chosen, self._mode.accent)

    def _on_menu_click(self, btn: "MenuButton", ctrl: "MenuSpec") -> None:
        """行内菜单控件的点击分派：弹候选列表，选中即生效（``MenuItem.on_click``）。

        控件只发信号（与 ModeBadge 同口径），菜单在这里构造 —— 于是「弹什么」永远
        按**点击那一刻**的 ``get_items`` 重读，不会给到过期列表。

        同 ``_on_mode_click``：非阻塞弹出，选中项经 ``on_picked`` 回来 —— ``on_click``
        已在弹层里跑过，这里只要重读一遍当前项（它才是动态真相）。
        """
        items = self._menu_items(ctrl)
        if not items:
            log.debug("MenuSpec 候选项为空，点击未展开（插件 %s）", self._spec.plugin_id)
            return
        menu = CandidateMenu(
            items, current=self._menu_current(ctrl), parent=btn,
            on_picked=lambda _item: self.refresh_dynamic(),
        )
        self._candidate_menu = menu          # 非阻塞 ⇒ 需本行替弹层保命，见 __init__ 注
        anchor = QRect(btn.mapToGlobal(QPoint(0, 0)), btn.size())
        menu.popup_at(btn.mapToGlobal(QPoint(0, btn.height())), anchor)

    def _on_choice_changed(self, text: str) -> None:
        """行内下拉改选：认领是哪一枚，把 ``ChoiceSpec.on_change`` 交给宿主。

        **只是通知，不是动作**：``on_change`` 让插件记住「现在选中的是哪个」，业务
        动作归整行 ``on_click``。这里也**不走结果中继**——它没有 ``ActionResult``
        可送（回调返回 None），强行中继等于给一次「改选」挂上成败提示。

        认槽位靠 ``sender()``（与开关 / 菜单同款）：多槽下不会张冠李戴。
        """
        sender = self.sender()
        for sel, ctrl in self._choices:
            if sel is sender:
                if ctrl.on_change is not None:
                    ctrl.on_change(text)
                return
        log.debug("行内下拉的 selected 信号找不到对应槽位（插件 %s）", self._spec.plugin_id)

    # ---- 候选列表的两个读取口（get_* 运行期重读优先，纯读取、失败回退静态值）----

    @staticmethod
    def _menu_items(spec: "MenuSpec | ModeBadgeSpec") -> "tuple[MenuItem, ...]":
        """候选项：``get_items`` 优先（每次点击重读），否则用静态 ``items``。"""
        gi = spec.get_items
        if gi is None:
            return tuple(spec.items)
        try:
            return tuple(gi())
        except Exception:  # noqa: BLE001 —— 声明者回调抛异常不该让整行失去交互
            log.exception("插件候选项回调 get_items 抛异常，回退静态 items")
            return tuple(spec.items)

    @staticmethod
    def _menu_current(spec: "MenuSpec") -> str:
        """当前项：``get_current`` 优先，否则静态 ``current``。"""
        gc = spec.get_current
        if gc is None:
            return spec.current
        try:
            return gc() or ""
        except Exception:  # noqa: BLE001 —— 同上
            log.exception("插件当前项回调 get_current 抛异常，回退静态 current")
            return spec.current

    @staticmethod
    def _choice_items(spec: "ChoiceSpec") -> "list[str]":
        """下拉候选：``get_items`` 优先（每次刷新重读），否则静态 ``items``。"""
        gi = spec.get_items
        if gi is None:
            return list(spec.items)
        try:
            return list(gi())
        except Exception:  # noqa: BLE001 —— 同上：回调抛异常不该让整行失去交互
            log.exception("插件下拉候选回调 get_items 抛异常，回退静态 items")
            return list(spec.items)

    @staticmethod
    def _choice_current(spec: "ChoiceSpec") -> str:
        """下拉当前项：``get_current`` 优先，否则静态 ``current``。"""
        gc = spec.get_current
        if gc is None:
            return spec.current
        try:
            return gc() or ""
        except Exception:  # noqa: BLE001 —— 同上
            log.exception("插件下拉当前项回调 get_current 抛异常，回退静态 current")
            return spec.current

    def set_health(self, level: "str | None") -> None:
        """喂代表态**级别**：角标颜色与显隐（``ok`` / 无状态源不画，见 ``_BADGE_LEVELS``）。"""
        self._badge_level = level
        self._badge.set_badge(self._badge_icon, level)

    def set_value_icon(self, icon: "IconName | None") -> None:
        """喂代表态的**取值图标**：角标字形（留空 → 该级别的通用标记，见 ``badge_name``）。

        取值图标随取值变（「第二屏在线 → 实心屏 / 离线 → 空心屏」），画在**角标**上
        —— 主位固定是身份图标（决议 D16）。插件不需要持有 widget 引用。
        """
        if icon is self._badge_icon:
            return                                  # 未变则不重绘（事件每次都到）
        self._badge_icon = icon
        self._badge.set_badge(icon, self._badge_level)

    def set_identity_icon(self, icon: "IconName | None") -> None:
        """注入**身份图标**（``ComponentSpec.icon``）——它固定回答「这是谁」。

        组件身份与状态行是两个注入点，谁后到都不该让对方失效，故由面板在两个注入
        点各自同步一次（``StatusPanel._sync_row_identity_icons``）。

        这一格是**空值也照存**的：缺位时盒里画通用齿轮（构造期声明的 ``icon_fallback``），
        因为 2026-09-22 起**每个组件都有一行**（框架为没声明行的组件合成朴素行，见
        ``ContributionRegistry.status_rows``），空图标位从「极端情况」变成「新组件的
        默认长相」。齿轮是框架的**占位**、不代表组件身份：组件声明 ``ComponentSpec``
        即换成自己的图标。
        """
        self.set_icon(icon)

    def set_subtitle(self, text: str) -> None:
        """填副标题：文本与悬停提示**用同一份**（决议 D18「文案只有一份」）。

        副标题是单行省略的窄条，长句会被 ``ElideLabel`` 截断；悬停提示给全文，
        于是「截断的看得到全貌」而不需要第二条文案路径。**不开放 tooltip 参数**——
        本行的提示只能是这一句，给了第二个口子就有第二个真源。
        """
        super().set_subtitle(text)

    def subtitle_text(self, fallback: str) -> str:
        """本行的副标题文案；算法不在本类里 —— 转调 ``row_subtitle_text``。

        与标题栏图标的悬停提示共用同一个函数（决议 D18）：两处不可能算出两句不同的话。
        ``fallback`` 是代表态那句（``state_view.resolve`` 的产物，失联时即框架的兜底文案）。
        """
        return row_subtitle_text(self._spec, fallback)


    def refresh_dynamic(self) -> None:
        """运行期刷新：开关态 / 菜单当前项 / 模式徽标 / 整行可点（§5.3）。

        每一路读取都**各自隔离**（``_safe_call``）：本文件其余读取口（``_menu_items``
        等）本就是这个口径，这里漏掉时，一个 ``get_checked`` 抛异常会把本行剩下的
        控件、以及 ``_recompute_plugin`` 循环里**排在它后面那些行**一起掐掉 ——
        面板停在半新半旧的样子，而界面上没有任何提示。
        """
        for sw, ctrl in self._switches:
            gc = ctrl.get_checked
            ge = ctrl.get_enabled
            if gc is not None:
                sw.setChecked(bool(_safe_call("get_checked", gc, False)))
            if ge is not None:
                sw.setEnabled(bool(_safe_call("get_enabled", ge, True)))
        for btn, ctrl in self._menus:
            btn.set_label(self._menu_current(ctrl))
        for sel, ctrl in self._choices:
            # 传 None 表示「不指定」，set_names 会保留当前项；当前项已不在新候选里时
            # 由它落到第一项。**程序化刷新不发 selected**（见 set_names 的 docstring）。
            sel.set_names(self._choice_items(ctrl), self._choice_current(ctrl) or None)
            ge = ctrl.get_enabled
            if ge is not None:
                sel.setEnabled(bool(_safe_call("get_enabled(choice)", ge, True)))
        if self._mode is not None and self._spec.mode is not None:
            gs = self._spec.mode.get_state
            if gs is not None:
                label, accent = _safe_call("mode.get_state", gs, (None, None))
                if label is not None:
                    self._mode.set_state(label, accent)
        gclick = self._spec.get_clickable
        if gclick is not None:
            self._clickable = bool(_safe_call("get_clickable", gclick, False))
            self.setCursor(
                Qt.CursorShape.PointingHandCursor if self._clickable
                else Qt.CursorShape.ArrowCursor
            )

    def _refresh_extra(self) -> None:
        """本行多出的两件自绘物的换肤：状态角标 ＋ 行内下拉。

        骨架那几处（图标盒 / 主位位图 / 副标题 / 模式徽标）走 ``super()``；面板换肤时
        按名字统一调用 ``refresh_theme``（``StatusPanel._refresh_breach_theme``），两条
        行路径一起刷新。
        """
        super()._refresh_extra()
        self._badge.update()          # 圆盘与字形都现读主题色，重绘即换肤
        for sel, _ctrl in self._choices:
            sel.refresh_theme()       # 下拉的 QSS 是按主题生成的，换肤必须重设
