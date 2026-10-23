"""表格页：一行工具条（过滤与五个动作同带）、七列表格的搭建、过滤与填格、选中与按钮可用性。

表格只用纯文本单元格（不逐行挂控件）⇒ 上千条广告条目也能流畅筛选。外部条目只读，
视觉上弱化并摘掉可编辑标记。

``manager_view.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from barkit.plugins.hosts_guard import store
from barkit.plugins.hosts_guard.models import (
    CATEGORY_LABELS,
    METHOD_LABELS,
)
from barkit.shell.ui_kit import (
    ComboField,
    FeedbackBar,
    search_field,
    tool_button,
)

from .manager_consts import FILTER_COMBO_W, PENDING_TEXT, SEARCH_W, SRC_EXTERNAL, SRC_MANAGED, STATUS_TEXT, _COLUMNS
from .manager_rows import Row


class _TableMixin:
    """表格页：工具条与表格的搭建、过滤与填格、选中与按钮可用性、状态格文案。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 页 0：表格 ----------

    def _build_table_page(self) -> QWidget:
        page = QWidget(self)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        # 工具条**一行**（2026-09-22）：曾试过拆成「过滤一行 / 动作一行」，用户判「太丑」——
        # 一件过滤与五个动作本来是同一条带子上的东西，拆开后上半条空着一大片。
        # 一行的前提是窗宽够（详情入口声明 860 ⇒ 内容区 828，本行自然宽 ~786），
        # 关键是**别让搜索框吃 stretch**：它一拉伸就把富余全吞掉，自己长成一条、
        # 反而把别人挤窄。故搜索框定宽、富余让给下拉与动作键之间的那段 stretch。
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self._search = search_field(
            "搜索域名或 IP…", on_changed=self._apply_filter, parent=page,
        )
        # 定宽归本页：见上面那段（搜索框一拉伸就把富余全吞掉）。
        self._search.setFixedWidth(SEARCH_W)
        bar.addWidget(self._search)

        # 两个过滤下拉**同宽**（一个常量两处用）：并排的同类控件宽窄不同，看起来像
        # 两个不同性质的东西。宽度取最长那项（「全部来源」）留一档余量。
        self._src_filter = ComboField(page)
        self._src_filter.setMinimumWidth(FILTER_COMBO_W)
        self._src_filter.addItem("全部来源", "")
        self._src_filter.addItem("仅托管", SRC_MANAGED)
        self._src_filter.addItem("仅外部", SRC_EXTERNAL)
        self._src_filter.currentIndexChanged.connect(self._apply_filter)
        bar.addWidget(self._src_filter)

        self._cat_filter = ComboField(page)
        self._cat_filter.setMinimumWidth(FILTER_COMBO_W)
        self._cat_filter.currentIndexChanged.connect(self._apply_filter)
        bar.addWidget(self._cat_filter)

        bar.addStretch(1)

        # 「更新」贴在 stretch 之后（动作组的最左）：它是对**选中项**的动作，与右边那几个
        # 同一族；放到「添加 / 批量导入」那两个**不需要选中**的键旁边就会被读成一族。
        # 没注入 ``on_refresh`` 时（界面被别处复用时）不造这枚键 —— 画一个按了没反应的键
        # 比少一枚键更坏。
        self._btn_refresh = None
        if self._on_refresh is not None:
            self._btn_refresh = tool_button("更新", parent=page)
            self._btn_refresh.setToolTip(
                "重新解析选中的自动更新条目（不等自动更新间隔），随后由守护改写 /etc/hosts")
            self._btn_refresh.clicked.connect(self._begin_refresh)
            bar.addWidget(self._btn_refresh)

        self._btn_edit = tool_button("编辑", parent=page)
        self._btn_edit.clicked.connect(self._begin_edit)
        bar.addWidget(self._btn_edit)

        self._btn_take = tool_button("接管", parent=page)
        self._btn_take.setToolTip("把选中的外部条目纳入 BarKit 托管（原位置的记录会被移除）")
        self._btn_take.clicked.connect(self._begin_take)
        bar.addWidget(self._btn_take)

        self._btn_del = tool_button("删除", parent=page)
        self._btn_del.clicked.connect(self._ask_delete)
        bar.addWidget(self._btn_del)

        btn_add = tool_button("添加", parent=page)
        btn_add.clicked.connect(self._begin_add)
        bar.addWidget(btn_add)

        btn_import = tool_button("批量导入", parent=page)
        btn_import.clicked.connect(lambda: self._show_page(self._edit_import))
        bar.addWidget(btn_import)
        lay.addLayout(bar)

        # 表格
        self._table = QTableWidget(page)
        self._table.setColumnCount(len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.itemDoubleClicked.connect(lambda _i: self._begin_edit())
        self._table.itemSelectionChanged.connect(self._sync_buttons)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        lay.addWidget(self._table, 1)

        self._stat = QLabel("", page)
        lay.addWidget(self._stat)

        # 表页自己的反馈槽：删除确认与「已移除」这类回执都落这里（「一区一条」）。
        # ◷ 2026-09-27 前它是**三页共用**的那一条、挂在窗口底 —— 换屏后各屏有条自己的
        # 槽，共用那条会让二级屏上同时出现两条。
        self._fb = FeedbackBar(page)
        lay.addWidget(self._fb)
        return page

    def _apply_filter(self) -> None:
        kw = self._search.text().strip().lower()
        src = self._src_filter.currentData()
        cat = self._cat_filter.currentData()

        shown = []
        for r in self._rows:
            if src and r.source != src:
                continue
            if cat and r.source == SRC_MANAGED and r.category != cat:
                continue
            if kw and kw not in r.host.lower() and kw not in r.ip.lower():
                continue
            shown.append(r)

        self._table.setUpdatesEnabled(False)
        self._table.setRowCount(len(shown))
        for i, r in enumerate(shown):
            managed = r.source == SRC_MANAGED
            cells = [
                "托管" if managed else ("系统" if r.system else "外部"),
                r.host,
                r.ip,
                CATEGORY_LABELS.get(r.category, r.category) if managed else "—",
                ("自动" if r.auto else "静态") if managed else "—",
                METHOD_LABELS.get(r.method, r.method) if managed and r.auto else "—",
                self._status_text(r),
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col in (2, 4, 5, 6):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if not managed:
                    # 外部条目只读：视觉上弱化，暗示不可直接编辑
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(i, col, item)
        self._table.setUpdatesEnabled(True)
        self._shown = shown
        self._stat.setText(f"共 {len(self._rows)} 条（托管 "
                           f"{sum(1 for r in self._rows if r.source == SRC_MANAGED)} · 外部 "
                           f"{sum(1 for r in self._rows if r.source == SRC_EXTERNAL)}）"
                           f"　当前显示 {len(shown)} 条")
        self._sync_buttons()

    # ---------- 选中与按钮态 ----------

    def _selected_rows(self) -> list[Row]:
        rows = []
        for idx in {i.row() for i in self._table.selectedIndexes()}:
            if 0 <= idx < len(self._shown):
                rows.append(self._shown[idx])
        return rows

    def _status_text(self, row: Row) -> str:
        """这一行「状态」那格该显示什么 —— **表格与动作后的局部重画共用这一份**。

        两处各写一遍就会漂：局部重画那份漏掉「更新中」时，按下「更新」后格子会被回到
        旧结论（看起来像「按了没用」），而整表重载那份又是对的。
        """
        if row.source != SRC_MANAGED or not row.auto:
            return "—"
        if row.pending:
            return PENDING_TEXT
        return STATUS_TEXT.get(row.status, "—")

    def _sync_buttons(self) -> None:
        rows = self._selected_rows()
        managed = [r for r in rows if r.source == SRC_MANAGED]
        external = [r for r in rows if r.source == SRC_EXTERNAL and not r.system]
        self._btn_edit.setEnabled(len(managed) == 1)
        self._btn_del.setEnabled(bool(managed))
        self._btn_take.setEnabled(bool(external))
        if self._btn_refresh is not None:
            # 静态条目没有「重新解析」可言（它的 IP 就是用户写下的那个），故按 auto 卡。
            self._btn_refresh.setEnabled(any(r.auto for r in managed))

    def _ask_delete(self) -> None:
        """删除**先问一句** —— 问在页脚那一格行内槽里（``FeedbackBar.ask``），不是弹框。

        ⚠ **被问的那一批必须跟着问句一起递下去**（``hosts`` 进回调），不能在确认时
        重读选中项：问句挂着的时候表格仍然可点，用户完全可以先把选中挪到别的行、
        再回头按下「删除」—— 那时重读出来的就是**另一批**记录，而屏幕上写着的还是
        原来那几个域名（删错的还是 /etc/hosts 里不可撤销的东西）。
        """
        rows = [r for r in self._selected_rows() if r.source == SRC_MANAGED]
        if not rows:
            return
        hosts = [r.host for r in rows]
        names = "、".join(hosts[:12])
        more = f" …等共 {len(rows)} 条" if len(rows) > 12 else ""
        self._fb.ask(
            f"将从 /etc/hosts 移除 {len(rows)} 条托管记录：{names}{more}。继续？",
            "删除", lambda hosts=hosts: self._do_delete(hosts), cancel_label="取消",
            confirm_kind="danger",
        )

    def _do_delete(self, hosts: list[str]) -> None:
        """删**问句点名的那几条**（不是此刻选中的那几条），见 :meth:`_ask_delete`。"""
        if not hosts:
            return
        for host in hosts:
            store.remove(host)
        self._reload()
        self._notify()
        self._fb.info(f"已移除 {len(hosts)} 条托管记录。")
