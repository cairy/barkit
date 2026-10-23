"""``_ActionsMixin`` —— 动作结果槽与底部栏快捷动作区。

``shell/panel.py`` 拆包（2026-10-01）：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtCore import (
    QPoint,
    QRect,
)
from barkit.contribution.specs import (
    ActionOutcome,
    ActionResult,
    IconName,
    MenuItem,
    as_action_result,
)
from barkit.shell.ui_kit import (
    CandidateMenu,
    tool_button,
)
from barkit.shell.ui_kit.buttons import (
    _decorate_button,
    _GlyphLabelButton,
)
from barkit.shell.design import metrics


class _ActionsMixin:
    """动作结果槽与底部栏快捷动作区。

    与 ``StatusPanel`` 同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 动作结果呈现（`ActionResult`，D32-② /《网络切换》§5.3-②） ----------

    def set_off_panel_feedback(self, sink) -> None:
        """注入「面板已经收起来」时的结果出口（装配器给，本类不认识通知中枢）。

        为什么需要两条出口：底部栏那些快捷按钮按下后**面板先 ``hide()`` 自己再跑回调**
        （它们开的是别的窗，收起是有意的），所以结果回来时结果槽已经不在屏上 —— 只写进
        那一行等于没说。这条出口把**失败 / 半成功**送到面板之外（今天接的是系统通知）。

        成功那一档在这里**就拦下**：它没有逐条档位可依（逐条档位是热键才有的），而
        ``ActionResult`` 的契约写着「成功且状态本身看得见就别再占一行字」—— 一条
        「已执行」的通知比它的信息量长寿。
        """
        self._off_panel_feedback = sink

    def _report_off_panel(self, title: str, result: "ActionResult") -> None:
        """面板已收起：只把**必须被看见**的结局交给出口（其余丢弃）。

        ``result`` 是**回调的原始返回值**，先过 ``as_action_result`` 规整：插件少写一个
        ``return`` 漏出来的东西（``True`` / 元组…）在这里变成「没话说」，而不是
        ``result.text`` 上的一次 ``AttributeError``。
        """
        result = as_action_result(result)
        if result is None or not result.text:
            return
        if result.outcome is ActionOutcome.OK:
            return
        if self._off_panel_feedback is not None:
            self._off_panel_feedback(title, result)

    def present_action_result(self, result: "ActionResult | None") -> None:
        """把一次动作的结局说给用户听 —— **插件给回访结果，宿主决定怎么画**。

        为什么要有这一层：在此之前插件想给反馈只能自弹 ``QMessageBox``，那与契约
        §1.2「组件不碰界面」冲突，且在 macOS 上的下场是嵌套事件循环把面板卡死（崩溃
        链路的实测来由见 ``ui_kit.FeedbackBar`` 的 docstring）。
        ``route_guard`` 此前那句注释是这条缺口的原话：面板侧路径「拿不到槽位 ⇒ 只记日志」。

        三条口径：

        - ``None``（以及空文案）＝ 本次没话要说 ⇒ **什么都不做**，连旧结果也不撤
          —— 否则每个「什么也没写」的动作都会顺手擦掉上一条有用的失败提示；
        - ``OK`` 短暂一现（``_RESULT_AUTO_HIDE_MS``）：成功的信息本来就在状态里
          （开关翻了、副标题变了），留久了是噪音；
        - ``FAILED`` / ``PARTIAL`` **留到下一个结果或面板重开**（``showEvent``）：
          它是「我点了一下、界面纹丝不动」时唯一的线索，自动消失等于白说。

        这一行装在主体里、**不在滚动容器内**，属于 ``_fixed_chrome_height`` 说的
        「滚不走的那一截」⇒ 它一出现就要重算滚动上界，否则内容会顶出屏幕。
        """
        if result is None or not result.text:
            return
        self._result_timer.stop()
        if result.outcome is ActionOutcome.FAILED:
            self._result_bar.error(result.text)
        else:
            # OK / PARTIAL 都不必红色 —— 前者是确认，后者是「做了一半」，
            # 该有的强度在文案里。
            self._result_bar.info(result.text)
        if result.outcome is ActionOutcome.OK:
            self._result_timer.start()
        # 结果槽是「不参与滚动的那一截」的一部分：它一出现，滚动区的上界就得重算，
        # 否则内容会顶出屏幕（see `_apply_status_scroll_size`）。
        self._apply_status_scroll_size()

    def _clear_action_result(self) -> None:
        """撤下结果槽（自动退场计时器 / 面板重开时走这一条）。"""
        self._result_timer.stop()
        self._result_bar.clear()
        self._apply_status_scroll_size()

    # ---------- 操作区首要动作（由贡献注册中心注入） ----------

    def set_primary_actions(self, actions) -> None:
        """注入面板操作区首要动作按钮（v3 ActionSpec 列表）。

        原样留底再渲染：用户层显示位开关（``ui.action.visible``，契约 §2.4）改判后
        要能原地重放，见 :meth:`_render_primary_actions`。
        """
        self._primary_actions = list(actions)
        self._render_primary_actions()

    def _render_primary_actions(self) -> None:
        """画 A 区（底部栏快捷按钮集群）；关掉「快捷区域」显示位的组件**不出现**。

        隐藏 ≠ 停用（决议 D4 约束 ①）：按钮不画，组件的状态机、轮询与它的热键一律
        照常 —— 关掉这一位只是把面板上那枚入口收起来。

        摆法由 :meth:`_plan_quick_area` 按**实测宽度**裁决（此前的写死枚数会把标签
        挤成截断字样，见 :attr:`N_QA_VIS` 上那段）。这里只负责按裁决结果建控件。

        用户层默认值：**快捷区域默认关**（决议 D37）—— 装配器会把
        ``Config.plugin_ui_visible`` 注入进来，故出厂状态下这一区是空的；用户想摆哪
        几个由他在「插件」页勾。
        """
        layout = getattr(self, "_action_layout", None)
        if layout is None:
            return                          # 还没建底部栏（构造中途注入路径）
        # 取空整个布局：**连带**把上一轮补在末尾的 stretch 一起摘掉，否则每刷一次就多一个
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        acts = [
            a for a in self._primary_actions
            if self._ui_visible(getattr(a, "plugin_id", ""), "action")
        ]
        visible, hidden, icon_only = self._plan_quick_area(acts)
        for act in visible:
            layout.addWidget(self._quick_button(act, icon_only=icon_only))
        if hidden:
            layout.addWidget(self._more_button(hidden))
        layout.addStretch(1)

    def _quick_area_budget(self) -> int:
        """A 区实得宽度：面板宽 − 底部栏左右边距 − C 区两枚 − 区间距。

        不另立一个「A 区多宽」的常数：它由 ``_build_footer`` 的三段式几何决定，这里
        按同一份几何反推（底部栏内容边距 ＋ C 区自然宽 ＋ 布局间距）。反推对不对由
        门禁钉在实测上（``test_quick_area_never_exceeds_its_budget``：A 区自然宽超预算
        时会被布局压在 224px，故「反推值」与「实测值」必须相等）。
        """
        footer = getattr(self, "_footer_bar", None)
        fixed = getattr(self, "_footer_fixed", None)
        if footer is None or fixed is None:
            return self.WIDTH              # 底部栏还没建（构造中途）⇒ 不设限
        row = footer.layout()
        margins = row.contentsMargins()
        return max(
            0,
            self.WIDTH - margins.left() - margins.right()
            - fixed.sizeHint().width() - row.spacing(),
        )

    @staticmethod
    def _natural_width(widget) -> int:
        """量一枚**临时**控件的自然宽然后丢掉。

        宽度必须由**真样式表**算出来（``sizeHint`` 会把 QSS 的 padding/border 算进去），
        框架自己估一份就会与样式表分叉 —— 那样「装不装得下」的判据会随一次改档位而静默
        失真。代价是每次重排多建几枚不挂上布局的临时控件：这条路径只在用户改显示位、
        重拉贡献时走一次，不在 tick 热路径上（建的控件不 show、不进布局，随即回收）。
        """
        width = widget.sizeHint().width()
        widget.deleteLater()
        return width

    def _plan_quick_area(self, acts) -> tuple[list, list, bool]:
        """A 区怎么摆：``(可见动作, 折起进「⋯」的动作, 是否纯图标)``。

        两条裁决（§5.6）：

        1. **模式整体切换，不逐枚挑**：标签态（图标 ＋ 短标签）要放就全放，放不下就
           **整区**转纯图标。一行里混着「有的图标、有的文字」是最难认的一种 —— 真机
           上报的就是这个形态（两枚图标 ＋ 三枚被挤成 48px 的文字）。
        2. **宽度是裁决者，枚数是派生量**：枚数上限（:attr:`N_QA_VIS`）只限制「别堆成
           一面墙」，装不装得下由实测宽度说话；超出部分折进末端「⋯」。

        「⋯」自身也占宽，故图标态先试「全都放得下」，放不下才留出它的位置再数枚数 ——
        不留位置就会出现「加了 ⋯ 反而溢出」这种自相矛盾的结果。
        """
        if not acts:
            return [], [], False
        budget = self._quick_area_budget()
        row = getattr(self, "_action_layout", None)
        spacing = row.spacing() if row is not None else 6
        # ① 标签态：枚数与实测宽度**两条都要过**（任一不过就整区转图标）
        if len(acts) <= self.N_QA_LABELED:
            total = sum(
                self._natural_width(self._quick_button(a, icon_only=False)) for a in acts
            )
            total += spacing * (len(acts) - 1)
            if total <= budget:
                return list(acts), [], False
        # ② 纯图标态：先试全放得下，再试留一枚「⋯」的位置
        unit = metrics.BTN_ACTION
        more_w = self._natural_width(self._more_button([]))
        for count in range(min(len(acts), self.N_QA_VIS), 0, -1):
            folds = count < len(acts)
            total = count * unit + (count - 1 + (1 if folds else 0)) * spacing
            if folds:
                total += more_w
            if total <= budget:
                return list(acts[:count]), list(acts[count:]), True
        # 窄到一枚都摆不下（极端）：全部收进「⋯」，A 区只剩它一枚
        return [], list(acts), True

    def _quick_glyph(self, act):
        """快捷按钮的字形：动作自带 → 组件身份图标 → 通用齿轮（框架占位）。

        回落口径与状态行主位（``_StatusRowView.set_identity_icon`` 声明的 ``icon_fallback``）**同一份**：
        「这是谁」的图只有一个来源（``ComponentSpec.icon``，决议 D14/D16）。纯图标态下
        没有文字兜底，那一格画不出图形就是一个看不见的按钮 —— 比回落一枚通用齿轮难认
        得多。三个声明了 ``DetailEntrySpec`` 但没给图标的组件（hosts 托管 / 网络切换 /
        iPad 隧道）由此各得自己的身份图标，不必逐个补声明。
        """
        icon = getattr(act, "icon", None)
        if icon is not None:
            return icon
        return self._component_icons.get(getattr(act, "plugin_id", "")) or IconName.GEAR

    def _quick_button(self, act, *, icon_only: bool):
        """A 区一枚快捷按钮。

        ``icon_only`` 态**一律**做成方形图标按钮（宽 ``metrics.BTN_ACTION``）：它是
        「装不下标签」时的整区形态，混进一枚带文字的就会把那枚挤成截断字样。字形走
        :meth:`_quick_glyph` 的回落链，故「动作没自带图标」不等于「画不出来」。

        标签态只给**声明了图标的**动作加字形：那一态有文字，图标是锦上添花，缺了不
        影响认；也避免与行主位那枚身份图标在同一屏里重复两次同样的信息。

        回调的返回值**要接**（``ActionSpec.on_click`` 已扩宽成 ``ActionCallback``）：
        主位动作里「按了却什么都没发生」是最难自查的一种坏法，而面板紧接着就收起来了，
        结果只能走面板之外的出口（见 :meth:`set_off_panel_feedback`）。
        """
        def fire(*_, a=act):
            self._fire_off_panel(a)
        if icon_only:
            return self._icon_button(act.label, self._quick_glyph(act), fire)
        # 标签态走**自绘排布**的按钮（2026-10-13 实机上报「图标与文本上下对不齐」）：
        # macOS 的 QMacStyle 忠实模仿原生 NSButton —— 图标底边贴文字基线（视觉即底部
        # 对齐），而 ``toolButtonStyle`` 对 QPushButton 无效（getter 读成员变量，
        # setProperty 设的是另一个 dynamic property，渲染不读它 —— 实测恒等）。
        # `_GlyphLabelButton` 把背景/边框/圆角/hover 仍全权交给 QSS，只把图标与
        # 文字的位置抢过来自己算（同处一行、各自垂直居中）；text() / icon() /
        # sizeHint 三项判据原样保留（A 区宽度裁决与「还标签」门禁都吃它们）。
        btn = _GlyphLabelButton(act.label, getattr(act, "icon", None))
        _decorate_button(btn, "pill-ghost")
        btn.clicked.connect(fire)
        return btn

    def _fire_off_panel(self, act) -> None:
        """底部栏一枚动作的**唯一**触发口：收起面板 → 跑回调 → 结局走面板之外的出口。

        两处消费方共用（主位那几枚 ＋ 「⋯」里收起来的那些）：两处**都得收起面板**
        （这些动作开的是别的窗），于是结果回头时结果槽已经不在屏上 —— 两边各写一遍
        必然漂移成「一边报、一边丢」。返回值由 :meth:`_report_off_panel` 规整，
        这里不管回调返回了什么。

        回调本体仍是**同步**跑完（与行内控件一致）；只有「结局送到哪里」是面板外的。
        """
        self.hide()
        self._report_off_panel(act.label, act.on_click())

    def _more_button(self, hidden):
        """末端「⋯」：把装不下的快捷按钮收进浮层（§5.6 溢出的入口）。"""
        btn = tool_button("⋯", "pill-ghost")
        btn.setToolTip(
            f"还有 {len(hidden)} 个快捷按钮：" + " / ".join(a.label for a in hidden)
        )
        btn.clicked.connect(lambda *_, hs=hidden, b=btn: self._open_quick_more(hs, b))
        return btn

    def _open_quick_more(self, hidden, anchor) -> None:
        """展开被折起的快捷按钮。

        用 ``CandidateMenu`` 的**非阻塞** ``popup()``（禁嵌套事件循环，见其 docstring），
        与行内徽标的候选列表同一条路：选中即生效，故无需回读选中项。持一份引用替弹层
        保命 —— 非阻塞弹层出作用域就可能被回收（与 ``_candidate_menu`` 同一个理由）。
        """
        items = [
            MenuItem(
                label=a.label,
                hint="",
                # 走与主位那几枚**同一个**触发口（见 `_fire_off_panel`）：收起面板、
                # 跑回调、结局送面板之外。此前这里是 `(self.hide(), a.on_click())`
                # 这种「逗号表达式」—— 返回元组，既不是 None 也不是 ActionResult。
                on_click=(lambda a=a: self._fire_off_panel(a)),
            )
            for a in hidden
        ]
        menu = CandidateMenu(items, parent=anchor)
        self._quick_menu = menu
        anchor_rect = QRect(anchor.mapToGlobal(QPoint(0, 0)), anchor.size())
        menu.popup_at(anchor.mapToGlobal(QPoint(0, anchor.height())), anchor_rect)
