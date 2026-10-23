"""``_StateMixin`` —— 状态数据流：行注入、快照与失联改判、熔断故障区与主题刷新。

``shell/panel.py`` 拆包（2026-10-01）：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import time

from PySide6.QtWidgets import QWidget

from barkit.shell import styles
from barkit.shell.ui_kit import apply_text_role
from barkit.contribution.specs import (
    ActionSpec,
    StateReport,
    StatusRowSpec,
)
from barkit.shell.renderers.representative import representative
from barkit.shell.renderers.state_view import resolve

from .constants import _SEV, log
from .row import _StatusRowView, row_subtitle_text


class _StateMixin:
    """状态数据流：行注入、快照与失联改判、熔断故障区与主题刷新。

    与 ``StatusPanel`` 同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 状态区 ----------

    def set_rows(self, widgets: list[QWidget]) -> None:
        """清空并填充插件贡献的状态行（通知 / 路由 / 一键输入）。

        兼容旧消费者（一次性场景 / 测试）：无 identity 复用，直接重建。
        新渲染走 ``sync_status_rows``（按 identity 复用，禁全量重建）。
        """
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._rows = list(widgets)
        for w in widgets:
            self._rows_layout.addWidget(w)
        self._rows_layout.addStretch(1)

    def sync_status_rows(self, specs: list, parent: "QWidget | None" = None) -> None:
        """按 identity 复用状态行 widget（声明式 UI 第一性，方案 §6.3）。

        命中缓存则就地保留（其回调由 ``custom`` 工厂首次构造时接好，稳定不重连），
        未命中才调用 ``spec.custom`` 构造；不再出现的行从布局移除并销毁。
        连续两次同步同一批 Spec → widget 实例不变（``test_renderer_reuses_widget``）。

        **一个组件可以有多行**（决议 D23）：行数与状态机数无关，声明几行就有几行。
        而复用键是 ``spec.identity()``（组件 ＋ 该行 ``key``），故多行天然各占一格。

        行的主位图标是**组件身份图标**（``ComponentSpec.icon``，决议 D16）：组件身份
        与状态行是两个注入点，谁先到都可能，故构造时按当前已知身份给一次、末尾再统一
        同步一次（``_sync_row_identity_icons``）。
        """
        new_cache: dict[str, QWidget] = {}
        row_map: dict[str, "list[_StatusRowView]"] = {}
        self._row_specs = list(specs)           # 原样留底：显示位改判后按原声明重放
        for spec in specs:
            if not self._ui_visible(spec.plugin_id, "row"):
                continue                        # 用户关掉了这一位：不渲染，但声明 / 顺序均在
            ident = spec.identity()
            w = self._row_cache.get(ident)
            if w is None and getattr(spec, "custom", None) is not None:
                w = spec.custom(parent)
            if w is None:
                # 无 custom → 框架按 StatusRowSpec 渲染（§5.3 默认路径）
                w = _StatusRowView(
                    spec, parent, on_click=self._row_on_click(spec),
                    identity_icon=self._component_icons.get(spec.plugin_id),
                )
            new_cache[ident] = w
            if isinstance(w, _StatusRowView):
                # 按 (组件, key) 的 identity 复用 widget，故同一组件的多行各占一格、
                # 互不覆盖（决议 D23）。收集期已保证「多条贡献的 key 必填且唯一」，
                # 所以这里的 ident 一定是互不相同的 —— 撞名在加载期就被拒了。
                row_map.setdefault(spec.plugin_id, []).append(w)
        # 取下全部（含末尾 stretch），不在新集合里的销毁，缓存/新建的重新加入
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w is not None and w not in new_cache.values():
                w.deleteLater()
        for w in new_cache.values():
            self._rows_layout.addWidget(w)
        self._rows_layout.addStretch(1)
        self._row_cache = new_cache
        self._row_widgets = row_map
        self._sync_row_identity_icons()
        self._replay_states()

    def _replay_states(self) -> None:
        """用缓存的报告把当前可见的显示位补画一遍（不广播给托盘）。

        新造 / 重建的显示位（用户改了显示位开关、溢出簇展开、组件增删）出生时没有任何
        状态，要靠**下一次状态事件**才亮 —— 最长要等一个轮询周期，用户看到的是「刚打开
        就空着」。这里直接拿 ``_state_reports`` 重放一次；托盘的队列与底图有自己的重算
        路径（``AppShell._push_badge`` / ``_apply_tray_base``），故 ``emit_tray=False``，
        避免同一次改判被广播两遍。
        """
        for plugin_id in dict.fromkeys(s.plugin_id for s in self._state_specs.values()):
            self._recompute_plugin(plugin_id, emit_tray=False)

    def _sync_row_identity_icons(self) -> None:
        """把组件身份图标回填给状态行（决议 D16：行主位画 ``ComponentSpec.icon``）。

        组件身份（``set_components``）与状态行（``sync_status_rows``）是两个独立注入
        点，谁后到都不该让对方失效 —— 故不把身份写死在行的构造参数里，两个注入点末尾
        各同步一次。未注入组件身份的行图标位留空：框架不替组件编一枚图标。
        """
        for plugin_id, rows in self._row_widgets.items():
            for row in rows:
                row.set_identity_icon(self._component_icons.get(plugin_id))

    @staticmethod
    def _set_callback(widget, signal_name: str, fn) -> None:
        """重连控件信号回调：先 disconnect 再 connect，避免复用后重复触发。

        声明式行（SwitchSpec/ButtonSpec）由渲染器接线时使用；``custom`` 工厂行
        在首次构造时自行接好回调，复用时不经此路径（关闭不重连）。
        """
        signal = getattr(widget, signal_name, None)
        if signal is None:
            return
        try:
            signal.disconnect()
        except Exception:  # noqa: BLE001 —— 无连接时 disconnect 抛错可忽略
            pass
        signal.connect(fn)

    def set_contributions(self, contributions) -> None:
        """一次性重拉并注入三类贡献（状态行 / 标题栏芯片 / 操作区动作）。

        启动期与 E3 ② 运行期启用 / 禁用 / 卸载后统一刷新入口，避免散落三处
        ``set_*`` 调用、并保证三者的顺序/一致性同源（均来自同一 ``contributions``）。
        """
        collector = getattr(contributions, "detail_entries", None)
        self.set_detail_entries(collector() if collector is not None else [])
        self.sync_status_rows(contributions.status_rows(), self)
        self.set_components(contributions.components(), contributions)
        actions = list(contributions.primary_actions())
        actions += [
            ActionSpec(
                plugin_id=e.plugin_id, key=e.key, label=e.label, icon=e.icon,
                on_click=lambda pid=e.plugin_id: self._open_detail(pid),
            )
            for e in self._detail_actions
        ]
        self.set_primary_actions(actions)
        self._relayout()

    def update_state_snapshot(self, spec, report) -> None:
        """事件总线回调（U2 StateSpec）：用 StateReport 刷新该插件代表态角标。

        app 经订阅 StateSpec.topic 收到事件后调用；记录报告、清除失联、重算该插件
        图标角标（不广播给托盘——app 的 on_state_update 已同步 _state_level）。
        """
        if not isinstance(report, StateReport):
            return
        self._state_reports[spec.topic] = report
        self._state_stale.discard(spec.topic)
        # 单调钟刻度：这处只用于「多久没收到事件」的相减，见 ``_check_stale``。
        self._state_last_at[spec.topic] = time.monotonic()
        self._recompute_plugin(spec.plugin_id, emit_tray=False)

    def is_stale(self, topic: str) -> bool:
        """该状态 topic 是否已被判「失联」（只读，P0③ 托盘底图的回落判据）。

        面板是失联判定的**唯一**执行者（``_check_stale`` + ``StateSpec.stale_after``）；
        托盘底图不另起一套时钟，直接问这里，避免「面板显示失联、菜单栏还显示已连接」。
        """
        return topic in self._state_stale

    def _warn_undeclared(self, topic: str, state: str) -> None:
        """组件报了个没在 ``StateSpec.states`` 里声明的取值 —— 记一条日志。

        同一对 (topic, 取值名) 只记一次：界面每 3 秒重算一拍，同一条错别字不该刷屏。
        界面上的露出（err + 取值名）由 ``state_view.resolve`` 负责，这里只留痕。
        """
        key = f"{topic}:{state}"
        if key in self._undeclared_warned:
            return
        self._undeclared_warned.add(key)
        log.warning(
            "%s 报告了未声明的取值 %r —— 界面按 err 露出，请补进 StateSpec.states",
            topic, state,
        )

    def _recompute_plugin(self, plugin_id: str, emit_tray: bool) -> None:
        """按该插件全部状态机重算代表态，刷新图标角标（必要时广播托盘）。

        emit_tray=True 时（仅失联翻转）逐 topic 广播有效级别给托盘徽标，避免与
        app 的正常事件路径重复（§4.3 状态同源）。

        级别 / 文案 / 取值图标一律经 ``state_view.resolve`` 从**声明**解析
        （决议 D13：报告只报取值名，级别归取值）—— 与托盘底图、代表态共用同一个
        解析点，三处不可能算出不同结果。代表态只解析**一次**，同时喂标题栏图标
        （取值图标 + 级别色 + **悬停提示那一句**，决议 D16 / D18）与列表行角标。

        一个组件有多行时，**每一行都刷同一个代表态**（决议 D23）：颜色按组件聚合是
        契约 §2.4 的既定口径（「这个组件整体有多坏」）。行与状态机之间**没有机制绑定**
        ——多行之间的差异来自各行自己的标题、行级副标题回调与行内控件，不是框架替它
        挑一台状态机。
        """
        states = [s for s in self._state_specs.values() if s.plugin_id == plugin_id]
        icon = self._icon_widgets.get(plugin_id)
        rows = self._row_widgets.get(plugin_id, [])
        if not states:
            if icon is not None:
                icon.set_state("idle", None, "")
            for row in rows:
                row.set_health("idle")
                row.set_subtitle("")
                row.set_value_icon(None)
                row.refresh_dynamic()
            return
        worst: str | None = None
        for s in states:
            topic = s.topic
            report = self._state_reports.get(topic)
            if report is None:
                lvl = "idle"
            else:
                rs = resolve(s, report, stale=topic in self._state_stale)
                lvl = rs.level.value
                if not rs.declared:
                    self._warn_undeclared(topic, report.state)
            if emit_tray:
                prev = self._emitted.get(topic)
                if prev != lvl:
                    self._emitted[topic] = lvl
                    self.stateLevelChanged.emit(topic, lvl)
            if worst is None or _SEV[lvl] > _SEV[worst]:
                worst = lvl
        worst = worst or "idle"
        # 代表态只解析一次：标题栏图标（取值图标 + 级别色）与行角标取同一份结果。
        # 失联（stale）也走同一个判据 —— 失联时回落该状态机主图标并报「状态失联」，
        # 继续画上一次的取值图标就是撒谎（与托盘底图的回落规则同源）。
        rep = representative(
            plugin_id,
            self._state_reports,
            self._state_specs,
            priorities=self._state_priorities_of(plugin_id),
        )
        rs = None
        if rep is not None:
            rs = resolve(rep[0], rep[1], stale=rep[0].topic in self._state_stale)
        fallback = rs.text if rs is not None else ""
        if icon is not None:
            # 标题栏那一枚是**组件级**的（每组件一枚）：取**首个声明行**的那句副标题，
            # 与列表行同一个函数、同一份字符串（决议 D18「文案只有一份」）。按**声明**
            # 取而非按 widget 取 —— 用户关掉「列表行」显示位时行 widget 不存在，
            # 悬停提示不该因此哑掉。
            icon.set_state(worst, rs.icon if rs is not None else None,
                           row_subtitle_text(self._first_row_spec(plugin_id), fallback))
        # 代表态同时喂两处：行角标级别 + 托盘徽标（§4.3 状态同源）
        for row in rows:
            row.set_health(worst)
            # 副标题：行级回调优先，未声明回退代表态那一格的**解析文案**（§2.7 / D24；
            # 报告留空 summary 时用取值声明的默认文案，失联时即框架的兜底文案）。
            # 算法只此一份 —— 标题栏图标的悬停提示取的也是它。
            row.set_subtitle(row.subtitle_text(fallback))
            # 取值图标同源于代表态取值（D3）：取值没给图标 → 回落该状态机的主图标
            row.set_value_icon(rs.icon if rs is not None else None)
            row.refresh_dynamic()

    def _first_row_spec(self, plugin_id: str) -> "StatusRowSpec | None":
        """该组件**首个声明行**的行声明；没有行声明或还没注入过任何声明时返回 ``None``。

        取声明而不是取 widget：``_row_specs`` 留的是原样声明（含被用户关掉显示位、
        因而没有 widget 的行），故「列表行」被关掉时本方法照样能返回那一句副标题。
        """
        for spec in self._row_specs:
            if spec.plugin_id == plugin_id:
                return spec
        return None

    def _check_stale(self) -> None:
        """tick 停止（后台坏死）时的兜底告警：超 stale_after 未收到事件 → 代表态判 err。

        失联翻转时广播给托盘（stateLevelChanged），保证面板标红与托盘徽标同源（§4.3）；
        只在状态**翻转**的那一次广播（3s 一轮定时器，避免重复重绘图标）。

        ``now`` 取 :func:`time.monotonic`：这里减的是「距上次事件过了多久」，用墙上时钟时
        时钟前跳会让**所有** ``stale_after`` 非 None 的 topic 在同一拍集体假失联（面板行与
        托盘徽标同时标红），时钟回拨则让这条兜底整个失效。
        """
        now = time.monotonic()
        for spec in self._state_specs.values():
            stale_after = spec.stale_after
            if stale_after is None:
                continue
            last = self._state_last_at.get(spec.topic)
            if last is None:
                continue
            staled = (now - last) > stale_after
            if staled == (spec.topic in self._state_stale):
                continue          # 状态未翻转：不重复刷新、不重复广播
            if staled:
                self._state_stale.add(spec.topic)
            else:
                self._state_stale.discard(spec.topic)
            self._recompute_plugin(spec.plugin_id, emit_tray=True)

    # ---------- 熔断故障区（F.3） ----------

    def set_scheduler(self, scheduler) -> None:
        """注入 scheduler：订阅熔断/恢复信号，显示已熔断插件并提供“重试”。"""
        self._scheduler = scheduler
        self._broken_by: dict[str, str] = {}
        scheduler.broken.connect(self._on_broken)
        scheduler.recovered.connect(self._on_recovered)

    def _on_broken(self, plugin_id: str, error: str, count: int) -> None:
        self._broken_by[plugin_id] = error
        self._broken_label.setText(
            f"{plugin_id} 连续失败 {count} 次已熔断：{error[:60]}"
        )
        self._broken_label.setToolTip(error)
        self._broken_row.show()
        self._relayout()

    def _on_recovered(self, plugin_id: str) -> None:
        self._broken_by.pop(plugin_id, None)
        if not self._broken_by:
            self._broken_row.hide()
        else:
            pid, error = next(iter(self._broken_by.items()))
            self._broken_label.setText(f"{pid} 已熔断：{error[:60]}")
        self._relayout()

    def _retry_broken(self) -> None:
        if self._scheduler is None or not self._broken_by:
            return
        for pid in list(self._broken_by):
            self._scheduler.retry(pid)

    def _on_theme_changed(self) -> None:
        """主题切换：刷新烤色控件并整体重绘（主容器 Card 现读实时主题）。"""
        self._refresh_breach_theme()
        # 底部栏按钮的图标是按当时主题色烤成位图的，换肤要重画一遍
        self._render_primary_actions()
        self.update()

    def _refresh_breach_theme(self) -> None:
        """主题切换时重设熔断卡片的烤色 + 刷新各插件状态行（初始化时写死，需随主题刷新）。"""
        t = styles.TOKENS[styles.current_theme()]
        self._broken_row.set_theme_colors(t["red_soft"], t["red_text"])
        apply_text_role(self._broken_label, "heading")
        self._broken_row.update()
        # 插件状态行在初始化时把主题色烤进 QSS / 位图，统一在此刷新。
        # 两条行路径都要刷：``_rows``（旧的一次性 set_rows）与 ``_row_widgets``
        # （生产路径 sync_status_rows，值是「该组件的行列表」，决议 D23）
        # —— 位图是按颜色渲染的，漏了就会停留在旧主题配色。
        rows = list(self._rows)
        for group in self._row_widgets.values():
            rows.extend(group)
        for row in rows:
            refresh = getattr(row, "refresh_theme", None)
            if callable(refresh):
                refresh()
        # 行内结果槽不在上面那两条行路径里（它是共享的一枚），但它同样把主题色烤进了
        # 内联样式（`FeedbackBar`）—— 漏了就会停在旧主题的配色上。
        self._result_bar.refresh_theme()
        # 标题栏图标同样把级别色烤进了位图，换肤要重画（角标/图标都现读主题）
        for icon in self._icon_widgets.values():
            icon.update()
