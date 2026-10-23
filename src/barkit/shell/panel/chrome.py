"""``_ChromeMixin`` —— 面板外壳：小工具、显示位开关、详情入口、标题栏图标条。

``shell/panel.py`` 拆包（2026-10-01）：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtCore import (
    QPoint,
    QRectF,
    QSize,
)
from PySide6.QtGui import (
    QIcon,
    QPainter,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QPushButton,
    QStyle,
    QStyleOptionButton,
    QWidget,
)

from barkit.shell import styles
from barkit.shell.ui_kit import (
    Divider,
    draw_glyph,
    glyph_pixmap,
)
from barkit.shell.ui_kit.buttons import _decorate_button, _ghost_fg
from barkit.contribution.specs import (
    IconName,
    StatusRowSpec,
)
from barkit.shell.design import metrics

from .constants import N_VIS, log
from .widgets import _OverflowCluster, _TitleBarIcon


class _GlyphButton(QPushButton):
    """方形图标按钮：字形按**当前主题色**自绘，并**自己认领换肤**。

    自绘（2026-10-13）：此前字形是烤进 ``QIcon`` 位图、交给 style 画，固定一色
    —— 既跟不了 hover（QSS ``#pill-ghost:hover`` 升 ``text1``），又曾与 A 区
    自绘按钮**取色不同源**（位图烤 text1、自绘读 palette），用户实机看到两区
    图标色不一致。现在与 :class:`_GlyphLabelButton` 同一套机制：背景/边框/
    hover 态仍全权交给 QSS 与 style（``drawControl`` 时把 icon 从 option 里摘掉），
    只把字形的位置与颜色抢回来 —— 颜色走 :func:`_ghost_fg`（静止 text2 ／
    hover·pressed text1 ／ disabled text3，与 QSS 三条规则逐条对齐），每次
    paintEvent **现问** tokens，换肤自动跟随，不再依赖位图重烤的时机。

    ``setIcon`` 位图仍烤着（判据读 ``icon()``；style 算 ``sizeHint`` 也要它），
    但**不参与绘制**；烤静止档 text2，与自绘同色。

    ``theme_changed`` 订阅保留：位图重烤保持「谁烤进去的谁负责」的原纪律
    （2026-10-02 为修「切系统明暗后图标停在旧主题色」而加），且自绘每次重画
    都现问，双保险。
    """

    _GLYPH = 16      # 字形逻辑边长（正方形按钮 28px 里的既有配比）

    def __init__(self, icon, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # 与 tool_button 同一处装饰（objectName / 档位 / 手型光标），不复制第二份。
        _decorate_button(self, "pill-ghost")
        self.setFixedWidth(metrics.BTN_ACTION)
        self.setIconSize(QSize(self._GLYPH, self._GLYPH))
        self._icon = icon
        self.refresh_theme()
        styles.theme_changed.connect(self.refresh_theme)

    def refresh_theme(self, *_args) -> None:
        """按当前主题重烤这枚字形（换肤口，见类 docstring）。"""
        t = styles.TOKENS[styles.current_theme()]
        self.setIcon(QIcon(glyph_pixmap(self, self._icon, self._GLYPH, t["text2"])))

    def paintEvent(self, event) -> None:
        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        body = QStyleOptionButton(opt)
        body.icon = QIcon()
        p = QPainter(self)
        self.style().drawControl(
            QStyle.ControlElement.CE_PushButton, body, p, self
        )
        s = self._GLYPH
        rect = QRectF((self.width() - s) / 2, (self.height() - s) / 2, s, s)
        draw_glyph(p, self._icon, rect, _ghost_fg(opt))
        p.end()


class _ChromeMixin:
    """面板外壳：小工具、显示位开关、详情入口、标题栏图标条。

    与 ``StatusPanel`` 同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 小工具 ----------

    @staticmethod
    def _divider() -> Divider:
        return Divider()

    def _build_footer(self, parent: QWidget) -> QWidget:
        """底部栏三段式（UI 规范 §5.6）：A 区快捷按钮集群 ＋ B 区弹性留白 ＋ C 区两枚固定。

        C 区**不参与** A 区那套「自适应标签 / 溢出」裁决：它是常驻入口，按钮数固定、
        永不进溢出（§5.6 明文）。故两区各建各的布局，`_render_primary_actions` 只碰 A 区。
        """
        footer = QWidget(parent)
        footer.setStyleSheet("background:transparent;")
        row = QHBoxLayout(footer)
        row.setContentsMargins(14, 8, 14, 8)
        row.setSpacing(6)

        # A 区（左，弹性）：按钮由 set_primary_actions 注入；末尾补 stretch 让它们靠左
        a_section = QWidget(footer)
        a_section.setStyleSheet("background:transparent;")
        self._action_layout = QHBoxLayout(a_section)
        self._action_layout.setContentsMargins(0, 0, 0, 0)
        self._action_layout.setSpacing(6)
        row.addWidget(a_section, 1)

        # B 区：弹性留白，把 C 区推到右端
        row.addStretch(1)

        # C 区（右，固定）：设置 ＋ 退出
        c_section = QWidget(footer)
        c_section.setStyleSheet("background:transparent;")
        self._footer_fixed = c_section      # 供测试与真机排查读「常驻那两枚是谁」
        fixed = QHBoxLayout(c_section)
        fixed.setContentsMargins(0, 0, 0, 0)
        fixed.setSpacing(6)
        for label, icon, sig in (
            ("设置", IconName.GEAR, self.settingsRequested),
            ("退出", IconName.POWER, self.quitRequested),
        ):
            fixed.addWidget(
                self._icon_button(
                    label, icon, lambda s=sig: self._dispatch(s), parent=c_section
                )
            )
        row.addWidget(c_section)
        return footer

    def _icon_button(self, label: str, icon, on_click, *, parent: QWidget | None = None):
        """方形图标按钮（28px）：C 区固定入口用，A 区转「纯图标」态时也用同一形态。

        字形取自设计系统（**禁 emoji**，§5.6）—— 原来 C 区那三枚写的是 ``⚙`` / ``?`` / ``⏻``，
        属被点名要清掉的那类。图标态没有文字，故 ``tooltip`` 是**强制**的：面板窄时它是
        文案的唯一回退。

        边长取 ``metrics.BTN_ACTION``（＝动作档 28）：它要与**同一排**的「设置」「退出」
        两枚文字按钮齐平 —— 原先钉死 30×30，而文字按钮收口到 28 之后，方的一枚会高出行
        2px（2026-09-21）。

        **字形颜色由 :class:`_GlyphButton` 自己重烤**（换肤时它订阅 ``theme_changed``），
        本方法不再碰图标 —— C 区那两枚是底部栏里只建一次的老控件，以前正是没人想起它们，
        切系统明暗后它们会停在旧主题的图标色上（2026-10-02 实测确认，用户报）。
        """
        # 字形 + 换肤重烤都在 _GlyphButton 里（它把主题色烤进键槽，就得自己重烤）；
        # 这里只补这枚按钮**特有**的两件事：人话 tooltip 与点击回调。
        btn = _GlyphButton(icon, parent=parent)
        btn.setToolTip(label)
        btn.clicked.connect(lambda *_, cb=on_click: cb())
        return btn

    # ---------- 显示位用户开关（契约规范 §2.4，决议 D4） ----------

    def set_ui_visibility(self, provider) -> None:
        """注入用户层显示位开关：``Callable[[plugin_id, slot], bool]``。

        面板**不自己读配置**（配置落点只有一处，见决议 D4 的前置依赖）：装配器把
        ``Config.plugin_ui_visible`` 接进来，面板只问「这一位用户关了吗」。未注入
        （含全部旧单测路径）＝ 全可见，即默认值「声明即出现」。

        注入 / 换源后立即按新判据重放**面板内三处**显示位（标题栏图标条 / 状态行 /
        快捷区域）：用户刚关掉的那一位当场消失、刚打开的那一位当场出现并**带着当前
        状态**（见 ``_replay_states``）。菜单栏角标不归面板画，由装配器重算队列。
        """
        self._ui_visibility = provider
        self._rebuild_header()
        self.sync_status_rows(self._row_specs, self)
        self._render_primary_actions()

    def set_state_priorities(self, provider) -> None:
        """注入状态机优先级：``Callable[[plugin_id], Mapping[str, int]]``。

        与 :meth:`set_ui_visibility` 同款注入（面板不读配置，只问「用户把哪台机排在前面」）。
        未注入（含全部旧单测路径）⇒ ``representative`` 走**声明顺序**，零行为变更。

        注入后立即按新顺序重放状态：用户刚在设置里拖完顺序，代表态当场换那一台 ——
        否则要等下一拍 tick 才变，看着像没生效。
        """
        self._state_priorities = provider
        self._replay_states()

    # ---------- 「刚被打开」的通知口 ----------

    def set_open_notifier(self, notifier) -> None:
        """注入「面板刚被打开」的通知口：``Callable[[], None]``，每次打开调一次。

        面板**只报告「我打开了」**，不自己决定这件事意味着什么、也不自己发事件 ——
        装配器把它接到框架事件总线上（``app.panel_opened``，见
        ``core/events.PANEL_OPENED_TOPIC``）。与 :meth:`set_ui_visibility` 同款：
        未注入（含全部旧单测路径）= 什么都不做。

        **为什么需要它**：按需型组件没有节拍，而面板那一行画的是「最后一次 ``tick()``
        的结论」⇒ 屏幕一直亮着时设备掉了，那一行会一直停在旧结论上。有了这条通知，
        组件声明该 topic 即可「每次开面板重跑一轮」，把界面拉回此刻的事实。

        调用时机＝**隐藏 → 显示那一次**（``_show_at_anchor`` 收尾）。面板已经显示时
        再 toggle 是「关闭」，不会走到那里，故每次打开恰好一次。

        回调异常只记日志：面板显示是用户已经按下去了的动作，不该因为某条订阅方出错
        而开着半截（框架侧的事件分发本就按「单插件异常隔离」设计，见 ``core/events``）。
        """
        self._open_notifier = notifier

    def _state_priorities_of(self, plugin_id: str):
        """该组件的状态机优先级覆盖；读失败按「没配过」处理（回落声明顺序）。

        与 :meth:`_ui_visible` 同一口径：读一个用户态的覆盖值失败，不该让代表态整体
        算不出来 —— 回落声明顺序最多是「顺序没按用户意思来」，抛异常则是「角标没了」。
        """
        provider = self._state_priorities
        if provider is None:
            return None
        try:
            return provider(plugin_id)
        except Exception:  # noqa: BLE001
            log.exception("读取 %s 的状态机优先级失败，按声明顺序处理", plugin_id)
            return None

    def _ui_visible(self, plugin_id: str, slot: str) -> bool:
        """该组件该显示位当前是否可见。

        未注入开关 ⇒ 可见。开关**抛异常也按可见**：一个用户态的读失败不该让整个显示位
        消失 —— 隐藏是破坏性的（东西不见了），宁可多露一处，与 ``Config`` 读侧的
        「非法一律按默认」同一口径。
        """
        provider = self._ui_visibility
        if provider is None:
            return True
        try:
            return bool(provider(plugin_id, slot))
        except Exception:  # noqa: BLE001
            log.exception("读取显示位开关失败（%s.%s），按可见处理", plugin_id, slot)
            return True

    # ---------- 详情页入口（§5.3 / §5.4） ----------

    def set_detail_entries(self, entries: list) -> None:
        """注入各插件的详情页入口（``DetailEntrySpec``）——**两个入口、一个过程**。

        每个入口同时接管两处（2026-09-22，``DetailEntrySpec.trigger`` 已删）：

        - **整行点击**：声明了入口的行可点（发 ``detailRequested``）；没声明入口的行
          保持不可点（无手型、点击无效）—— 行本身恒在，可点性是这一步决定的；
        - **底部栏快捷按钮**：A 区挂一枚，点它发同一个 ``detailRequested``。
        """
        self._detail_entries = {e.plugin_id: e for e in entries}
        self._detail_actions = list(entries)

    def _open_detail(self, plugin_id: str) -> None:
        """打开某插件的详情窗（合成入口的两处共用）。

        为什么要绕一个方法而不是直接 ``lambda pid=...: self.detailRequested.emit(pid)``：
        **``Signal.emit`` 返回 ``True``**（不是 ``None``），而 ``on_click`` 的返回值是
        ``ActionCallback`` 的返回值、宿主要读的。多包一层让这两个 lambda 老实返回
        ``None``（＝「没话说」），而不是漏一个 ``True`` 出去 —— 宿主那侧虽然也规整了
        （``as_action_result``），但把错的东西留在**自己**的代码里靠别人兜底是不对的。
        """
        self.detailRequested.emit(plugin_id)

    def _row_on_click(self, spec: "StatusRowSpec"):
        """整行点击：StatusRowSpec.on_click 优先，其次由详情入口合成（§5.3）。"""
        if spec.on_click is not None:
            return spec.on_click
        if spec.plugin_id in self._detail_entries:
            return lambda pid=spec.plugin_id: self._open_detail(pid)
        return None

    # ---------- 标题栏组件图标条（U2 §5.2） ----------

    def set_components(self, components, contributions) -> None:
        """注入各插件组件图标（ComponentSpec，每插件一枚），按 plugin_order 排序。

        图标条只放图标 + 级别色（代表态），无状态文字行（§5.2）。图标取该插件代表态的
        **取值图标**、颜色取代表态**级别色**（决议 D16，无角标）；组件名（§5.2 悬停提示）
        与状态行主位图标也都出自这一份身份声明（决议 D14）。
        """
        self._all_components = list(components)
        self._components_show_all = False  # 每次全量重注入都回到截断态（用户态切换不跨刷新保留）
        self._component_icons = {c.plugin_id: c.icon for c in components}
        self._state_specs = {s.topic: s for s in contributions.states()}
        self._rebuild_header()
        self._sync_row_identity_icons()     # 状态行可能先于组件身份注入（见该方法）

    def _rebuild_header(self) -> None:
        """按 ``_all_components`` 与 ``_components_show_all`` 重建标题栏图标条。

        截断态：前 ``N_VIS`` 枚 + 末端 ``_OverflowCluster(+K)``；全显态：全部图标、
        无溢出簇。图标顺序恒等于 ``plugin_order``（不按问题优先，§5.2）。

        用户层可关（决议 D4）：被关掉的那几枚**不画**，顺序位仍留在 ``plugin_order``
        里（重新打开时原位出现）；它们也**不计入**溢出簇的 ``+K`` —— 那是「框架放不下
        的 K 枚」，不是「用户不想看见的 K 枚」，混在一起会把用户的意图说反。

        **声明层也可关**（``ComponentSpec.in_header=False``）：这类组件有身份（行主位
        图标、组件名都取自它）但**不占标题栏那一格** —— 与用户层开关是两件事：前者是
        作者的声明（无状态机的组件不该有一格说不出事的图标），后者是用户的偏好。
        它同样不计入溢出簇的 ``+K``。
        """
        self._clear_header_widgets()
        # 前导 stretch：与末尾 stretch 对称 ⇒ 组件图标组在标题栏里**整体居中**
        # （不含左侧 logo；logo 有组件时本就隐藏，故视觉上即整条标题栏居中）。
        # 末尾 stretch 在注入完图标 / 溢出簇后统一加（见下），与这里成对。
        self._head_status.addStretch(1)
        self._icon_widgets = {}
        self._overflow_widget = None

        comps = [
            c for c in self._all_components
            if c.in_header and self._ui_visible(c.plugin_id, "titlebar")
        ]
        show_all = self._components_show_all
        visible = comps if show_all else comps[: N_VIS]
        hidden = [] if show_all else comps[N_VIS:]

        for comp in visible:
            icon = _TitleBarIcon(comp, 36, self)
            # 默认参数绑定 pid：多实例（多枚图标）各自的 id 不能靠晚绑定取，否则
            # 全部回调都会读到循环结束后那个 comp（§5 #27 尾巴的落点之一）。
            icon.clicked.connect(
                lambda *_, pid=comp.plugin_id: self._on_icon_click(pid)
            )
            # 末尾 stretch 尚未加入 ⇒ 追加到当前末尾即「前导 stretch 之后、顺序 =
            # plugin_order」，不会被前导 stretch 推反（用 count()-1 会插到前导 stretch
            # 之前、把顺序颠倒）。
            self._head_status.insertWidget(self._head_status.count(), icon)
            self._icon_widgets[comp.plugin_id] = icon

        if hidden:
            ov = _OverflowCluster(len(hidden), [c.name for c in hidden], self)
            ov.clicked.connect(self._on_overflow_click)
            self._head_status.insertWidget(self._head_status.count(), ov)
            self._overflow_widget = ov

        # 末尾 stretch：与前导 stretch 对称 ⇒ 图标组居中（§用户 2026-09-20 诉求）
        self._head_status.addStretch(1)

        # logo 仅在「无组件」时空状态显示：平时只显示插件图标（用户规划，不显示 app 图标）；
        # 空状态用它当唯一折叠热区，兜底「没有任何状态图标时如何折叠」的边界。
        # set_components 每次重注入都会重跑本方法，故运行时组件增删也跟着切。
        # **用户把标题栏图标全关掉**时同样落到这里：logo 不是组件显示位、关不掉，它是
        # 唯一残留的折叠热区 —— 「允许全隐藏」约束的是框架不把用户关掉的图标塞回来，
        # 不是要求连折叠入口也一起消失。
        self._logo.setVisible(len(comps) == 0)
        self._replay_states()

    def _on_overflow_click(self) -> None:
        """溢出簇点击：在「截断 / 全显」间切换，重排图标条（§5.2 溢出一览）。"""
        self._components_show_all = not self._components_show_all
        self._rebuild_header()

    def _on_icon_click(self, plugin_id: str) -> None:
        """点图标（§5.2 / §10.3）：折叠态展开、展开态收起（再次点击收起）。

        与品牌 logo 的双触发并存：logo 作为稳定热区仍可展开/收起，图标则按规范语义
        承担「点图标展开列表 / 再次点击收起」的主交互（§5.2 / §10.3）。

        **「下探到该组件行」于 2026-09-18 重启**（用户裁定）：折叠态这一半在展开之后
        再滚到该组件的状态行（``_pending_reveal`` 交给动画收尾去执行）。当年删掉这半件
        的唯一理由是「面板没有滚动容器 ⇒ 滚动到那一行无容器可动」，该前提已随 §5 #28
        落地消失：状态行早已外包 ``QScrollArea``，目标行完全可能在可视区外 —— 不滚过去
        就变成「展开了，却没看到你要看的那行」，正是「下探」二字要解决的问题。

        **展开态那一半不滚，仍只收起**：既然是要收起来，就没有「滚到某行」可言。这也
        守住了 §5.2 那条已裁定的点击语义（同一控件只做一件事，不按上下文分裂成两种
        行为 —— 那会多出一个要向用户解释的状态，§2.6 否掉 S3 时用的就是这条判据）。

        **信号 ``componentClicked`` 依旧不恢复**：它不是「下探」的必要零件 —— 接收方
        就是面板自己（滚动自己的滚动条），给自己发信号等于绕一圈为空。留着才会重演
        §A29 当初那个病：一个全仓零接收方的信号 ＝ 一条说得出做不到的承诺。
        """
        self._pending_reveal = plugin_id if self._folded else None
        self.toggle_fold()

    def _reveal_row(self, plugin_id: str) -> None:
        """把状态区滚到该组件的第一行（下探的落点，§5 #27 尾巴）。

        只在展开动画收尾调用：那时 ``_apply_status_scroll_size`` 已把滚动区定高、
        布局也 activate 过，``ensureWidgetVisible`` 才滚得到终态位置。

        三种情况安静返回，都不是错误：
        - 该组件**没有状态行**（无状态机 / 用户关掉了行显示）⇒ 没有可定位的目标；
        - 行不在滚动容器的子树里（旧单测路径用 ``set_rows`` 直塞）⇒ 滚不动；
        - **目标行本来就看得全** ⇒ 不动滚动位置。
        """
        rows = self._row_widgets.get(plugin_id) or []
        row = rows[0] if rows else None
        if row is None:
            return
        # 先把**滚动区里面那层行布局**激活到最新几何，再滚。动画收尾（
        # ``_on_fold_anim_finished``）只 activate 了面板顶层布局，``_rows_layout``
        # 可能还是上一帧的排布：照旧位去滚，等布局随后更新，目标行又被挪出可视区。
        inner = self._status_list.layout()
        if inner is not None:
            inner.activate()
        try:
            viewport = self._status_scroll.viewport()
            top = row.mapTo(viewport, QPoint(0, 0)).y()
            if 0 <= top and top + row.height() <= viewport.height():
                # 已经看得全 —— 不滚。否则「点图标」会顺手把列表挪一下（
                # ``ensureWidgetVisible`` 带 50px 边距，会把目标行推到偏中的位置），
                # 而那个动作在「本来就看得见」时纯属多余。只在真看不全时才下探。
                return
            self._status_scroll.ensureWidgetVisible(row)
        except RuntimeError:
            # 行已被 deleteLater 回收（内容刚变过），下一拍再点即可
            return

    def _clear_header_widgets(self) -> None:
        while self._head_status.count():
            item = self._head_status.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
