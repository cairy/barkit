"""``StatusPanel`` 本体：常量、信号、构造与 Qt 虚函数重写；混合类在包的其余子模块。

``shell/panel.py`` 拆包（2026-10-01）：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import logging
import time
import traceback

from PySide6.QtCore import (
    Qt,
    QTimer,
    Signal,
    Property,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from barkit.shell import styles
from barkit.shell.macos_window import create_dismiss_controller
from barkit.shell.ui_kit import (
    Card,
    CandidateMenu,
    FeedbackBar,
    apply_text_role,
    tool_button,
)

from .actions import _ActionsMixin
from .chrome import _ChromeMixin
from .constants import _RESULT_AUTO_HIDE_MS, log
from .fold import _FoldMixin
from .place import _PlaceMixin
from .state import _StateMixin
from .widgets import _ClickableLogo


class StatusPanel(
    _ChromeMixin,
    _StateMixin,
    _ActionsMixin,
    _FoldMixin,
    _PlaceMixin,
    QWidget,
):
    # 混合类按**职责**分开在包内各子模块，方法经 ``self`` 取用实例状态，
    # 故 MRO 顺序不影响变量解析（混合类之间无继承、无重名）。``QWidget``
    # 必须排在最后：PySide6 的元类按最靠右的 Qt 基类登记虚函数。
    WIDTH = 320          # 卡片内容宽度（不含阴影边距）
    SHADOW_MARGIN = 0    # 不再绘制投影，卡片直接贴窗边
    #: 状态区滚动容器**至少**留出的高度（§5 #28）。屏幕再矮也不把它压没 ——
    #: 压没了等于「没有滚动条」，回到「底部行够不着」那个原点。
    MIN_STATUS_H = 120
    # 底部栏 A 区的两条**声明**上限（UI 规范 §5.6）。真正咬住的是**实测宽度**：
    # 面板只有 320px、A 区实得 224px，而三枚带图标的短标签合计就 249px —— 光看枚数
    # 会把标签挤成 48px 的截断字样（2026-09-22 用户报的「两枚图标 ＋ 三枚文字且超出
    # 范围」）。故这两个数是**意图**（别把 A 区堆成一面墙），宽度是**硬约束**。
    N_QA_VIS = 5         # 可见枚数上限，超出的折成末端「⋯」
    N_QA_LABELED = 3     # 标签态尝试上限：超过它直接转纯图标（不再逐枚量宽）

    # 底部栏 C 区的两个固定信号（面板内部，由 app 接线到具体回调）。
    # 「帮助」已随底部栏重排删除（2026-09-17，用户裁定）：它做的是「打开设置窗的『关于』页」，
    # 与「设置」开的是**同一个窗**，只差一次点击 —— 留着就是在窄面板上白占一格。
    settingsRequested = Signal()
    quitRequested = Signal()
    # 状态行被点击请求打开详情页（§5.3：仅声明了 DetailEntrySpec 的行才发）
    detailRequested = Signal(str)
    # 状态级别变化（含「守卫失联」改判）→ 由 app 同步托盘徽标，
    # 保证面板与托盘看到的是同一份级别（topic, level）。
    stateLevelChanged = Signal(str, str)

    def __init__(self) -> None:
        # 用无边框 Window：macOS 上 Window 顶层高度可实时 resize（Popup 会忽略
        # 「可见时」resize 导致折叠「缩不了」），故折叠用 200ms 高度动画。
        # 代价是 Popup 的两个原生能力（浮于全屏 Space 之上、点外部关闭）需用 PyObjC
        # 补回：① 显示时把 NSWindow.level 提到 NSPopUpMenuWindowLevel；② 显示时激活
        # app，靠 NSApplicationDidResignActiveNotification 在点击任何别处（其他 App /
        # 桌面 / 菜单栏）时关闭面板（无需辅助功能授权）。
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # 阻止 Qt 绘制根窗口系统背景，否则圆角外的矩形区域会显示为底色，
        # 看起来像直角；同时避免用 QSS background:transparent 影响子控件。
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self._folded = True  # 启动时面板默认收起（仅标题栏 + 操作区 + 次级区），点展开再显示状态区
        self._anim = None
        # 最近一次被隐藏的时刻（用于「点图标」与「失活关闭」去重）；单调钟刻度，
        # 初值取负 ⇒ 任何时刻都算「关了很久」（见 place.on_status_item_click）
        self._last_hide = -10.0
        self._can_close_on_deactivate = False
        self._scheduler = None
        self._dismiss = create_dismiss_controller(self)
        self._rows: list[QWidget] = []
        # 状态行 widget 按 identity 缓存：命中则就地复用（不重建），未命中才构造
        self._row_cache: dict[str, QWidget] = {}
        # 标题栏组件图标条（由 app 经 set_components 注入，框架层不持有具体插件）
        self._all_components: list = []         # set_components 注入的全量组件（含被溢出簇隐藏的）
        self._components_show_all: bool = False  # 溢出簇点击后在「截断 / 全显」间切换
        self._overflow_widget = None             # 末端溢出簇（组件数 > N_VIS 时存在）
        self._state_specs: dict = {}            # topic -> StateSpec
        self._state_reports: dict = {}          # topic -> StateReport（动态，只报取值名）
        self._state_last_at: dict = {}
        self._state_stale: set = set()          # 已改判「失联」的 topic（翻转时广播）
        # 组件报了没声明的取值 → 记一条日志（同一 topic+取值名只记一次，别按 3s 一拍刷屏）
        self._undeclared_warned: set = set()
        self._emitted: dict = {}                # topic -> 上次广播给托盘的级别（去重）
        self._icon_widgets: dict = {}           # plugin_id -> _TitleBarIcon
        # plugin_id -> [_StatusRowView, ...]（由 sync_status_rows 注入）。**是一个列表**
        # ——行数与状态机数解绑（决议 D23）：一个组件声明几行就有几个，不是「一行」。
        # 值类型是列表而不是单个 widget，正是为了让「第二行起」不被静默吞掉。
        self._row_widgets: dict = {}
        # 「点标题栏图标下探到该组件行」（§5 #27 尾巴，2026-09-18 重启）：记下待定位的
        # 组件，等展开动画收尾、高度定完再滚 —— 动画进行中滚动区高度还在变，那时滚会
        # 滚到一半就被随后的重排推回去。仅「这一次点击导致了展开」才有值（收起无意义）。
        self._pending_reveal: str | None = None
        # 用户层显示位开关（决议 D4）：由装配器注入 ``Config.plugin_ui_visible`` 的签名
        # ``(plugin_id, slot) -> bool``。未注入（含全部旧单测路径）= 全可见，即默认值
        # 「声明即出现」——面板**不自己读配置**，只问「这一位用户关了吗」。
        self._ui_visibility = None
        # 状态机优先级覆盖（用户在设置页拖过顺序才有）：由装配器注入
        # ``Config.plugin_state_priorities`` 的签名 ``(plugin_id) -> {状态机 key: int}``。
        # 未注入 ⇒ 代表态回落**声明顺序**（旧行为）。
        self._state_priorities = None
        # 「面板刚被打开」的通知口（由装配器注入 ``app_shell._build_panel``）：面板只负责
        # **报告自己打开了**，不自己发事件 —— 谁受这件事影响、发到哪条 topic 都是框架的事
        # （与上面两个 provider 同款：面板不读配置、不认 topic）。未注入（含全部旧单测
        # 路径）= 什么都不做，与「框架没接这条线」等价。
        self._open_notifier = None
        self._row_specs: list = []              # 最近一次注入的行声明（显示位改判后原地重放）
        # plugin_id -> 身份图标（来自 ComponentSpec.icon，决议 D14/D16）：状态行图标盒主位
        # 用它，与标题栏图标条同源；由 set_components 填、两个注入点各同步一次到行上。
        self._component_icons: dict = {}
        # 详情页入口（由 set_detail_entries 注入）：决定 L2 整行可点性 + 底部栏快捷入口
        self._detail_entries: dict = {}         # plugin_id -> DetailEntrySpec（驱动整行可点）
        self._detail_actions: list = []         # 同一批 DetailEntrySpec（驱动底部栏快捷按钮）
        # 最近一次注入的操作区动作（原样留底）：显示位改判后按原声明重放，见
        # ``_render_primary_actions``。与 ``_row_specs`` 同一个用意。
        self._primary_actions: list = []
        # 底部栏「⋯」溢出浮层（非阻塞 popup ⇒ 必须持引用替它保命，与行内 CandidateMenu 同理）
        self._quick_menu: "CandidateMenu | None" = None
        # 失联检查：tick 停止后事件不再到来，由本定时器兜底标红（而非永远显示陈旧状态）
        self._stale_timer = QTimer(self)
        self._stale_timer.setInterval(3000)
        self._stale_timer.timeout.connect(self._check_stale)
        self._stale_timer.start()

        # 透明顶层 + 圆角容器（自绘，确保 macOS 上圆角一定生效）；SHADOW_MARGIN=0 即卡片贴窗边、无投影
        root = QVBoxLayout(self)
        root.setContentsMargins(
            self.SHADOW_MARGIN, self.SHADOW_MARGIN, self.SHADOW_MARGIN, self.SHADOW_MARGIN
        )
        container = Card(radius=14, parent=self)
        container.setFixedWidth(self.WIDTH)
        self._container = container
        root.addWidget(container)

        clayout = QVBoxLayout(container)
        clayout.setContentsMargins(0, 0, 0, 0)
        clayout.setSpacing(0)

        # ---------- 标题栏 ----------
        head = QWidget(container)
        # 留引用：算「不参与滚动的那一截」高度时要用它的 sizeHint（见 _fixed_chrome_height）
        self._head = head
        head.setObjectName("pop-head")
        head.setStyleSheet("background:transparent;")  # 透明，仅 Card 负责底色与圆角
        hlayout = QHBoxLayout(head)
        # QSS 对 QWidget 的 padding 不会撑开内部布局，用 layout margin 控制真实内边距
        hlayout.setContentsMargins(14, 12, 12, 10)
        hlayout.setSpacing(9)

        logo = _ClickableLogo(26, head, self.toggle_fold)
        self._logo = logo
        head_status = QHBoxLayout()
        head_status.setSpacing(14)
        self._head_status = head_status  # 组件图标条由 set_components 动态注入（含末尾 stretch）

        hlayout.addWidget(logo)
        # head_status 吃满标题栏剩余宽度（logo 固定 26，有组件时还隐藏），内部
        # 前导 + 末尾 stretch 对称 ⇒ 组件图标组整体居中（见 _rebuild_header）
        hlayout.addLayout(head_status, 1)
        clayout.addWidget(head)

        # 头部 / 主体分割线（head 不再用 border-bottom，避免 QWidget 边框不生效）
        clayout.addWidget(self._divider())

        # ---------- 主体 ----------
        body = QWidget(container)
        body.setObjectName("pop-body")
        body.setStyleSheet("background:transparent;")  # 透明，仅 Card 负责底色与圆角
        blayout = QVBoxLayout(body)
        # 主体水平无内边距：让分割线全宽（与标题下横线对称）；各区块内部自带左右内边距。
        blayout.setContentsMargins(0, 0, 0, 0)
        blayout.setSpacing(0)
        clayout.addWidget(body)

        def add_divider() -> None:
            blayout.addWidget(self._divider())

        # ---- 动作结果槽（2026-09-18，`ActionResult` 的宿主落点）----
        # 插件做事不许碰界面（崩溃由来见 `ui_kit.FeedbackBar` 的 docstring），结果统一由
        # 这一行共享的结果槽接住：整行点击 / 行内控件 / 模式徽标 / 候选菜单项都落这里。
        # `FeedbackBar` 构造即 hide ⇒ 没人说话时对布局零贡献（与 `_broken_row` 同款）。
        self._result_bar = FeedbackBar(body)
        self._result_bar.setObjectName("action-result")
        # 留白走**它自己的口**：2026-09-30 起反馈条外面套了一层浅底盒，直接戳
        # ``layout()`` 会静默打到外面那一层（留白变外缩、字还挤在原处）。
        self._result_bar.set_body_margins(14, 8, 14, 8)
        # 成功是一句会过期的话（状态本身已经变了），失败要等人看见 ⇒ 只有 OK 自动退场。
        self._result_timer = QTimer(self)
        self._result_timer.setSingleShot(True)
        self._result_timer.setInterval(_RESULT_AUTO_HIDE_MS)
        self._result_timer.timeout.connect(self._clear_action_result)
        blayout.addWidget(self._result_bar)
        # 面板**已经收起来**时的结果出口（装配器注入，见 `set_off_panel_feedback`）。
        # 没有它就降级成「什么都不说」——与本类此前对「面板不可见」的处理一致。
        self._off_panel_feedback = None

        # ---- 状态区（可折叠）：熔断条 + 插件状态行 ----
        self._status_section = QWidget(body)
        self._status_section.setStyleSheet("background:transparent;")
        ss_layout = QVBoxLayout(self._status_section)
        # 水平无内边距：让状态行的 hover 铺满整行宽度（与面板等宽）；
        # 各行的左右留白由各插件行内部 layout margin 负责（见各 plugin 的 panel_widget）。
        ss_layout.setContentsMargins(0, 8, 0, 8)
        ss_layout.setSpacing(4)
        blayout.addWidget(self._status_section)

        # 熔断故障区（F.3）：保留接口与测试属性
        t0 = styles.TOKENS[styles.current_system_theme()]
        self._broken_row = Card(radius=9, parent=self._status_section)
        self._broken_row.setObjectName("breach")
        self._broken_row.set_theme_colors(t0["red_soft"], t0["red_text"])
        broken_layout = QHBoxLayout(self._broken_row)
        broken_layout.setContentsMargins(10, 10, 10, 10)
        self._broken_label = QLabel(self._broken_row)
        self._broken_label.setObjectName("breach_text")
        apply_text_role(self._broken_label, "heading")
        broken_layout.addWidget(self._broken_label, stretch=1)
        self._retry_btn = tool_button("重试", "pill-ghost")
        self._retry_btn.clicked.connect(self._retry_broken)
        broken_layout.addWidget(self._retry_btn)
        self._broken_row.hide()
        ss_layout.addWidget(self._broken_row)
        # 主题切换时重绘：刷新熔断卡片/插件行的烤色，并整体 update()
        # （主容器 Card 与自绘控件现读实时主题，update 即触发换肤）。
        styles.theme_changed.connect(self._on_theme_changed)

        # 状态行容器（可折叠）＋ 外面那层滚动容器（§5 #28）
        self._status_list = QWidget()
        self._status_list.setObjectName("status-list")
        self._status_list.setStyleSheet("background:transparent;")
        self._rows_layout = QVBoxLayout(self._status_list)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(4)

        # 面板高度是**按内容** adjustSize 长高的，而 `place_window` 只把窗口
        # clamp 进屏幕 —— 内容真超过屏幕时，底部那些行既没有滚动路径、窗口也顶穿屏幕，
        # 就是「够不着」。故把状态行装进独立滚动区（与设置窗「插件」页同法：
        # NoFrame + 横向滚动关 + 不自刷背景，底色仍归 Card）。
        # 高度由 `_apply_status_scroll_size` 按屏幕可用高度算；屏够高时它取到内容
        # 自然高度，于是布局与今天**逐像素相同**（滚动条不出现）。
        self._status_scroll = QScrollArea(self._status_section)
        self._status_scroll.setObjectName("status-scroll")
        self._status_scroll.setWidgetResizable(True)
        self._status_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._status_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._status_scroll.setStyleSheet("background:transparent;border:none;")
        self._status_scroll.viewport().setStyleSheet("background:transparent;")
        self._status_scroll.setWidget(self._status_list)
        ss_layout.addWidget(self._status_scroll)

        # 全宽分割线：状态区 ↔ 底部栏（与标题下横线对称）。
        # 折叠时状态区隐藏，此线与标题下横线会贴在一起形成“双线”，故折叠时一并隐藏。
        self._div_status_action = self._divider()
        blayout.addWidget(self._div_status_action)

        # 底部栏（UI 规范 §5.6）：一条 hairline 之下**三段式** ——
        #   A 区（左，弹性）快捷按钮集群，今天是插件 `ActionSpec` 贡献（app 级按钮暂无来源）；
        #   B 区 弹性留白，把 C 区推到右端；
        #   C 区（右，固定）设置 ＋ 退出两枚图标按钮，**永久可见、永不进溢出**。
        # 重排前这里是「操作区 ＋ 次级」两段（中间另有一条分割线），合并成一条工具栏。
        self._footer_bar = self._build_footer(body)   # 留引用：同上，算「固定部分」用
        blayout.addWidget(self._footer_bar)

        # 折叠态必须在**任何** sizing 之前落地：启动期 set_contributions → _relayout
        # 会在面板仍隐藏时算一次尺寸，若此时状态区还没被收紧，算出的就是「展开态」
        # 高度（含状态区），而首显其实是折叠态 → 首次打开面板偏高，关掉重开才对。
        self._apply_fold_state()

    #: 折叠动画（``QPropertyAnimation(self, b"statusMaxH")``）的目标属性。
    #: 存取器在 ``_FoldMixin``：``Property`` 的取/存要在**类体**里给出，
    #: 故用类对象点名，别把它们搬回本类打断折叠逻辑的聚合。
    statusMaxH = Property(int, _FoldMixin._get_status_max, _FoldMixin._set_status_max)

    def hideEvent(self, event) -> None:
        """面板被隐藏：停控制器、记时刻，并**留一条带调用栈的记录**。

        2026-09-27 加这条的理由：用户报「点标题栏图标 ⇒ 整个面板消失」，当时**关闭出口
        全是静默的** —— 现场只看得到「面板没了」，日志里一个字都没有，只能靠猜（那两轮
        就是这么猜错的）。这里是唯一躲不掉的收口（任何路径最终都走到它），调用栈直接
        点出是谁关的。隐藏是低频动作，不会刷屏。
        """
        if self._dismiss is not None:
            self._dismiss.stop()
        self._last_hide = time.monotonic()
        if log.isEnabledFor(logging.INFO):
            frames = traceback.extract_stack()[:-1][-4:]
            where = " <- ".join(
                f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno}:{f.name}" for f in frames
            )
            log.info("面板隐藏（收口）：%s", where)
        super().hideEvent(event)
