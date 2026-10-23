"""``_RulesListMixin``：五张分区卡的构造、规则行与列表刷新。

``settings.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from barkit.shell import data_ops
from barkit.shell.feedback_slot import FeedbackSlot
from barkit.shell.order_editor import OrderEditor
from barkit.shell.ui_kit import (
    ChoiceSelector,
    ElideLabel,
    FeedbackBar,
    HoverRow,
    Switch,
    apply_text_role,
    form_row,
    note,
    settings_section,
    tool_button,
)

from .settings_words import _APP_WORDS, _DISPLAY_WORDS, _NOTIFY_WORDS


class _RulesListMixin:
    """列表页那半：五张分区卡（状态 / 自动化 / 启动行为 / 规则 / 装置）的构造、规则行与列表刷新。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 第 1 屏：规则列表 ----------

    def _build_list_page(self) -> QWidget:
        page = QWidget(self)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        # 卡与卡之间的段距。**卡内的段距归 `settings_section` 管**，这里只管卡与卡的间隔
        # （2026-09-21 返工：每一组控件长在卡里，卡外不留裸行）。
        lay.setSpacing(10)

        # ── 状态（只读回显：三台机各一句，设计 §6.4.3）──
        status_card, status_lay = settings_section("状态", parent=page)
        self._status_lbl = QLabel("", status_card)
        self._status_lbl.setWordWrap(True)
        status_lay.addWidget(self._status_lbl)
        lay.addWidget(status_card)

        # ── 自动化总闸（裁决项 6：从面板徽标移到设置页顶部——低频 ＋ 破坏性）──
        # 卡标题已经写了「自动化」，行标签只说这一枚开关管什么。
        auto_card, auto_lay = settings_section("自动化", parent=page)
        gl = QHBoxLayout()
        gl.setSpacing(8)
        gl.addWidget(QLabel("按规则自动静音", auto_card), stretch=1)
        self._auto_sw = Switch(auto_card)
        self._auto_sw.setChecked(self._plugin.automation_active)
        self._auto_sw.clicked.connect(self._on_toggle_automation)
        gl.addWidget(self._auto_sw)
        auto_lay.addLayout(gl)
        lay.addWidget(auto_card)

        # 「恢复自动」卡（2026-09-18）：**只在手动覆盖态出现**，紧贴总闸下面 —— 它俩回答的
        # 是同族问题（谁在管事），但不是一个开关：总闸是「自动化整个开/关」（低频、破坏性），
        # 这里回的是「这一轮临时覆盖归还给自动」。
        #
        # 为什么不在面板行上（设计 §「执行细节」原写的是"面板手动覆盖态提供恢复自动按钮"）：
        # ① 面板行控件硬上限 2 枚（决议 D6），而 §6.3 那行只剩一枚 Switch；② 更硬的一条是
        # 行 widget 按 ``identity()`` 复用（``shell/panel.sync_status_rows``）—— **条件控件
        # 在面板上根本长不出来**，常驻一枚在自动态下又是个死按钮。故落点改判到设置页，
        # 与「总闸归设置页」同一批裁决。偏差与判据见 `内部记录（手动覆盖归还）`。
        #
        # 形态（2026-09-21）：一张**无标题**的分区卡 —— 它只在手动覆盖态出现，本身就是一句
        # 「现在是什么局面 ＋ 怎么归还」的提示，再给个标题是重复。属性名沿用 ``_resume_card``
        # （回归用例按它取控件）。
        self._resume_card, resume_lay = settings_section("", parent=page)
        rl = QHBoxLayout()
        rl.setSpacing(8)
        self._resume_lbl = QLabel("手动覆盖中 · 自动接管已挂起", self._resume_card)
        rl.addWidget(self._resume_lbl, stretch=1)
        self._resume_btn = tool_button("恢复自动", "pill-ghost", parent=self._resume_card)
        self._resume_btn.clicked.connect(self._on_resume_auto)
        rl.addWidget(self._resume_btn)
        resume_lay.addLayout(rl)
        lay.addWidget(self._resume_card)

        # ── 启动行为开关（2026-09-18 新增）：它**不是**静音策略的一部分，故独立成卡
        # 而不是塞进「自动化」那张 ──
        launch_card, launch_lay = settings_section("启动行为", parent=page)
        ll = QHBoxLayout()
        ll.setSpacing(8)
        ll.addWidget(QLabel("启动时自动拉起 OpenDisplay", launch_card), stretch=1)
        self._launch_sw = Switch(launch_card)
        self._launch_sw.setChecked(self._plugin.open_on_startup)
        self._launch_sw.clicked.connect(self._on_toggle_launch)
        ll.addWidget(self._launch_sw)
        launch_lay.addLayout(ll)
        launch_lay.addWidget(note(
            "下次 BarKit 启动时生效。它已经在跑时不动它 —— 想看窗体，点下面那枚「打开」。",
            launch_card,
        ))
        lay.addWidget(launch_card)

        # ── 规则（列表 ＋ 三枚编辑动作）──
        rules_card, rules_lay = settings_section("规则", parent=page)
        head = QHBoxLayout()
        head.setSpacing(6)
        # 预设入口：新装用户的第一个动作不该是手打进程名（§6.4.4）
        preset_btn = tool_button("常用场景", "pill-ghost", size="inline", parent=rules_card)
        preset_btn.clicked.connect(self._on_preset)
        head.addWidget(preset_btn)
        add_btn = tool_button("＋ 新增", "pill-ghost", size="inline", parent=rules_card)
        add_btn.clicked.connect(self._on_add)
        head.addWidget(add_btn)
        # 「初始化」＝ 清空全部规则、回到出厂默认（``config_schema`` 里 ``rules`` 的默认 ``[]``）。
        # 点过「常用场景」之后**没有回头路**是本页此前的缺陷（用户 2026-09-14 提出）——
        # 尤其预设本身写错过（见 RULE_PRESETS 的两条纪律），改完预设后手里那几条仍是旧的，
        # 只能靠「初始化 → 重新点常用场景」刷新，一步到位。
        reset_btn = tool_button("初始化", "pill-ghost", size="inline", parent=rules_card)
        reset_btn.clicked.connect(self._on_reset)
        head.addWidget(reset_btn)
        head.addStretch(1)
        rules_lay.addLayout(head)

        # 本页错误提示与二次确认都落在这里（零模态框）。
        self._list_fb = FeedbackBar(rules_card)
        rules_lay.addWidget(self._list_fb)

        # 规则列表：行首手柄拖拽 / Alt+↑↓（框架 OrderEditor 托管，与「插件」页同件）。
        # 键在 `_refresh_list` 里重新编号，见那里对「为什么不用下标」的说明。
        #
        # **不给它套内层滚动区**（2026-09-22 删）：原实现把列表塞进一个 `QScrollArea`
        # 再让卡片吃掉剩余高度，而内层滚动区的 `sizeHint` 是 0 ⇒ 它被压成一个固定的
        # 小窗口（实测：视口高 520 / 420 / 360、规则 4 条 / 8 条，列表区拿到的高度**恒
        # 为 88px**，而 4 条内容需要 164、8 条需要 332）⇒ 用户永远只看得见约 2 行、
        # 剩下靠**第二根滚动条**，而外层本来就有一根（两根套着滚）。这正是用户报的
        # 「规则区被挤压得看不出」＋「拖排序到框外要先滚再拖」的同一根因。
        # 现在列表按内容自然高度长，滚动交给**页级**那唯一一根。
        # 这一族 2026-10-06 清完（用户报「设置里的路由条目不应该再有第二层滚动条」）：
        # 路由守卫的清单卡、翻页时钟 / 一键输入 / 待办 的列表卡，外加本组件编辑屏的
        # 进程候选列表与截图识别的结果列表 —— 插件面已无自建滚动区，由门禁
        # `test_plugin_ui_does_not_build_its_own_scroll_area` 钉住。
        self._order = OrderEditor(
            build_row=self._build_rule_row, on_reorder=self._on_rule_reorder, parent=rules_card,
        )
        self._order.set_row_spacing(4)
        rules_lay.addWidget(self._order)
        # 空态提示与列表**互斥**（各自占一块地方）：列表为空时不给一个空滚动框
        self._empty_note = note(
            "还没有规则——点「＋ 新增」自己配一条，或点「常用场景」从预设开始。", rules_card
        )
        rules_lay.addWidget(self._empty_note)
        # 卡按内容高度长（原先它吃剩余高度以给内层滚动区留位置，内层删掉后那截是纯留白）
        lay.addWidget(rules_card)

        # ── 装置（设计 §6.2 判定线 2：事实 → 调参 → 运维，都是「这个装置」的事）──
        device_card, device_lay = settings_section("装置", parent=page)
        device = QHBoxLayout()
        device.setSpacing(8)
        self._device_lbl = QLabel("", device_card)
        device.addWidget(self._device_lbl, stretch=1)
        open_btn = tool_button("打开", "pill-ghost", parent=device_card)
        open_btn.clicked.connect(self._plugin.open_opendisplay)
        device.addWidget(open_btn)
        device_lay.addLayout(device)
        self._device_note = note("", device_card)
        device_lay.addWidget(self._device_note)

        # 装置调参：`poll` 归框架画的页尾「运行间隔」卡（契约 D21），本组件只调防抖轮数
        self._debounce_sel = ChoiceSelector(
            self._debounce_choices(), str(self._plugin.debounce), device_card
        )
        self._debounce_sel.setFixedWidth(132)
        self._debounce_sel.selected.connect(self._on_debounce)
        # 定宽下拉拉满会撑成半屏 ⇒ control_fill=False
        device_lay.addLayout(form_row(
            "防抖（连续几轮一致才动系统通知）", self._debounce_sel, device_card,
            control_fill=False,
        ))
        device_lay.addWidget(note(
            "一轮 = 框架节拍，即本页页尾「运行间隔」里那个「框架检查间隔」—— 本组件"
            "读不到它，故这里只说轮数，不替你换算成秒。",
            device_card,
        ))

        # 运维两枚：**拨测**与**复制诊断**是同一批事实的两个出口（一个上屏、一个进剪贴板，
        # 排版共用 ``diagnostics``），故它们并排 —— 用户在哪个组件发现不对，就在哪一页
        # 复制它的诊断，不必先去别处选组件。「打开数据目录」不在这一排：它打开的是
        # **全局**那个数据目录、与组件无关，2026-09-22 起唯一落点是设置窗「维护」页。
        # 本卡那条槽的持有者：**建在这里、attach 在末尾** —— 下面「复制诊断」的
        # `say=` 要传它，而 `FeedbackBar` 到本方法末尾才建出来。
        self._device_slot = FeedbackSlot()
        ops = QHBoxLayout()
        ops.setSpacing(6)
        probe_btn = tool_button("拨测", "pill-ghost", parent=device_card)
        probe_btn.setToolTip(
            "当场读一轮三级线索（第二屏 / 通知 / 自动化 ＋ 规则命中），只上屏，"
            "不改系统状态。"
        )
        probe_btn.clicked.connect(self._on_probe)
        ops.addWidget(probe_btn)
        copy_btn = data_ops.diagnostics_button(
            device_card,
            self._plugin.diagnostics_text,
            say=self._device_slot.say,
        )
        copy_btn.setToolTip(
            "把此刻的拨测结果（含原始取值名与规则明细，比屏幕上那份多两段维护者要的"
            "东西）复制成一段纯文本，便于反馈问题。"
        )
        ops.addWidget(copy_btn)
        ops.addStretch(1)
        device_lay.addLayout(ops)
        # 卡内反馈槽：触发它的两枚按钮都在这张卡里，回执就该长在卡内（「一区一条」）。
        # 放在页尾框架区那条槽上，用户在长页里点完按钮根本看不到问句。
        device_fb = FeedbackBar(device_card)
        device_lay.addWidget(device_fb)
        self._device_slot.attach(device_fb)
        # 拨测结果区：平时不占版面（空字符串 ＋ hidden），点过才长出来
        self._probe_lbl = QLabel("", device_card)
        self._probe_lbl.setWordWrap(True)
        self._probe_lbl.setVisible(False)
        device_lay.addWidget(self._probe_lbl)
        lay.addWidget(device_card)
        return page

    def _build_rule_row(self, key: str, handle: QWidget) -> QWidget:
        """一行规则：手柄 ＋ 名称 ＋ 原因文案 ＋（命中时）●命中 ＋ ›（push 提示）。

        ``key`` / ``handle`` 是 ``OrderEditor.build_row`` 的契约：手柄**必须**放进本行，
        否则这一行看得见却拖不动。手柄独占自身 mousePress，故点行内别处照常进编辑页。

        `›` 是**设置页**的 push 导航语汇（macOS 设置窗标准），与设计 §6.3
        「面板行不加 ›」不冲突——面板行用 hover ＋ 手型表达可点，设置页用箭头。
        """
        rule = self._key_rule[key]
        name = str(rule.get("name") or "?")
        row = HoverRow(on_click=lambda k=key: self._open_edit_key(k), parent=self._order)
        h = QHBoxLayout(row)
        h.setContentsMargins(4, 6, 10, 6)
        h.setSpacing(8)
        h.addWidget(handle)
        title = QLabel(name, row)
        h.addWidget(title)
        reason = str(rule.get("reason") or "")
        if reason:
            rl = ElideLabel(reason, row)
            apply_text_role(rl, "caption")
            h.addWidget(rl, stretch=1)
        else:
            h.addStretch(1)
        if name in self._hits:
            dot = QLabel("● 命中", row)
            apply_text_role(dot, "caption_ok")
            h.addWidget(dot)
        chev = QLabel("›", row)
        apply_text_role(chev, "chevron")
        h.addWidget(chev)
        return row

    def _refresh_list(self) -> None:
        """刷新状态条 / 规则列表 / 装置行（打开本页与每次增删改后各调一次）。"""
        # 局面已变：上一句回执、以及**挂着的确认**都随之过期。撤下确认尤其重要 ——
        # 待确认动作认的是"此刻的编辑目标"（`_editing` / 下标），列表一重建它就可能指向别人。
        self._list_fb.dismiss()
        # 拨测结果是「那一刻」的快照：列表一重建它就过期，先撤下来（同 _list_fb 的判据）
        self._probe_lbl.clear()
        self._probe_lbl.setVisible(False)
        # 「恢复自动」只在手动覆盖态出现。判据取 ``notify_state()``（与面板副标题、状态条
        # **同一个真源**）而不是直接问 ``decider.mode``：自动化关着时 decider 内部也是
        # "manual"，但那时该报的是 ``frozen``（"通知由手动开关控制"），按钮没有意义。
        self._resume_card.setVisible(self._plugin.notify_state() == "manual")
        apply_text_role(self._status_lbl, "status")
        self._status_lbl.setText(
            f"第二屏：{_DISPLAY_WORDS.get(self._plugin.display_state(), '?')}　"
            f"通知：{_NOTIFY_WORDS.get(self._plugin.notify_state(), '?')}　"
            f"自动化：{'开' if self._plugin.automation_active else '关'}"
        )
        # 命中集合先落实例属性：行渲染拿不到它（`build_row` 只给键与手柄）
        self._hits = set(self._plugin.hit_rule_names())
        # 键 → 规则**对象**。键另立一套（`r0`/`r1`…）而不是直接拿下标，是因为排序会把行
        # 搬来搬去 —— 把下标烘进行里，换序之后点「B」那一行会进到「A」的编辑页。
        self._key_rule = {f"r{i}": r for i, r in enumerate(self._rules)}
        self._order.set_items(list(self._key_rule))
        self._order.setVisible(bool(self._rules))
        self._empty_note.setVisible(not self._rules)

        # 装置事实（零 fork：装没装 / 跑没跑 / 是否仍在菜单栏占位）
        app_state, in_menu = self._plugin.opendisplay_facts()
        apply_text_role(self._device_lbl, "status")
        self._device_lbl.setText(f"OpenDisplay：{_APP_WORDS.get(app_state, '?')}")
        self._device_note.setText(
            "它仍在菜单栏占位。若要让位给 BarKit：打开 OpenDisplay，把「Show app in」"
            "改成 Background only（改完需重启该 App，当前推流会断一次；"
            "BarKit 不代写别人的偏好）。"
            if in_menu else "它已不在菜单栏占位。"
        )
