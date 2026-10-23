"""条目表单页（新增 / 编辑 / 接管**共用**）：搭建、按模式装载、校验、落盘。

接管模式整组隐去「域名 / IP / 更新 / 解析方式 / 备注」—— 那几条各自保留自己的 IP，
接管只让用户选分类；摆一格格子出来会让人以为能逐条改。

``manager_view.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtWidgets import QLineEdit

from barkit.plugins.hosts_guard import store
from barkit.plugins.hosts_guard.models import (
    DEFAULT_CATEGORY,
    METHOD_AUTO,
    METHODS,
    valid_host,
    valid_ip,
)
from barkit.shell.ui_kit import (
    ComboField,
    EditScreen,
    form_row_stacked,
    note,
)

from .manager_consts import SRC_EXTERNAL, SRC_MANAGED
from .manager_rows import Row


class _FormMixin:
    """条目表单页（新增 / 编辑 / 接管共用）：搭建、按模式装载、校验、落盘，以及表格页那三枚动作键。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 页 1：条目表单（新增 / 编辑 / 接管共用） ----------

    def _build_form_page(self) -> EditScreen:
        """页 1：条目表单（新增 / 编辑 / 接管共用）—— 版面归框架（`ui_kit.EditScreen`）。

        ◷ 2026-09-27：这一页与批量导入页此前各拼一份「标题行 ＋ 页脚」，页脚顺序还是
        **主按钮在左**（与仓里其余编辑屏相反）—— 改走 `EditScreen`：返回键回导航带、
        主动作贴右缘、反馈槽在主动作之上。**观感变更**见
        `docs/log/2026-09-27-编辑弹窗基准.md`。

        保存那一道**注入**（`EditScreen` 是组合件，覆写调不到）：:meth:`_validate_form`
        过不了关就一个字不落盘。
        """
        form = EditScreen(
            on_validate=self._validate_form,
            on_commit=self._commit_form,
            parent=self,
        )
        form.back.connect(self._back_to_table)
        # 这一页的报错落它自己那条槽（零模态）
        self._form_fb = form.feedback

        group, body = form.add_group()
        # 接管模式：只读列出选中的外部条目 ＋ 提示（IP 取各自当前值，不在这儿逐条改）
        self._f_take_note = note("", group)
        self._f_take_note.setVisible(False)
        body.addWidget(self._f_take_note)

        self._f_host = QLineEdit(group)
        self._f_host.setPlaceholderText("example.ddns.net")
        self._f_host_row = form_row_stacked("域名", self._f_host, group)
        body.addWidget(self._f_host_row)

        self._f_ip = QLineEdit(group)
        self._f_ip.setPlaceholderText("203.0.113.10（自动更新时作为兜底值）")
        self._f_ip_row = form_row_stacked("IP", self._f_ip, group)
        body.addWidget(self._f_ip_row)

        self._f_cat = ComboField(group)
        body.addWidget(form_row_stacked("分类", self._f_cat, group))

        self._f_auto = ComboField(group)
        self._f_auto.addItem("静态（不自动更新）", False)
        self._f_auto.addItem("自动更新（跟随 DDNS）", True)
        self._f_auto.currentIndexChanged.connect(self._sync_method_enabled)
        self._f_auto_row = form_row_stacked("更新", self._f_auto, group)
        body.addWidget(self._f_auto_row)

        self._f_method = ComboField(group)
        for key, label in METHODS:
            self._f_method.addItem(label, key)
        self._f_method_row = form_row_stacked("解析方式", self._f_method, group)
        body.addWidget(self._f_method_row)

        self._f_note = QLineEdit(group)
        self._f_note.setPlaceholderText("可选，仅用于备注")
        self._f_note_row = form_row_stacked("备注", self._f_note, group)
        body.addWidget(self._f_note_row)

        body.addWidget(note(
            "自动更新会定期检查，解析失败会保留上一次的 IP，不会清空。", group))

        # 主动作（文案由 `_open_form` 按模式改成「接管」/「保存」）
        self._btn_apply = form.save_button
        return form

    # ---------- 表单页：装载与应用 ----------

    def _entry_for(self, host: str) -> dict | None:
        return next((e for e in self._entries if e["host"] == host), None)

    def _sync_method_enabled(self) -> None:
        self._f_method.setEnabled(bool(self._f_auto.currentData()))

    def _open_form(self, mode: str, rows: list[Row]) -> None:
        """把表单页按模式装好并切过去（``add`` / ``edit`` / ``take``）。"""
        self._form_mode = mode
        self._form_rows = rows
        take = mode == "take"
        entry = self._entry_for(rows[0].host) if mode == "edit" else None

        self._edit_form.set_title({
            "add": "添加条目",
            "edit": "编辑条目",
            "take": f"接管外部条目（{len(rows)} 条）",
        }[mode])

        self._f_take_note.setVisible(take)
        if take:
            names = "\n".join(f"· {r.host} → {r.ip}" for r in rows[:10])
            more = f"\n…等共 {len(rows)} 条" if len(rows) > 10 else ""
            self._f_take_note.setText(
                "将以下条目纳入 BarKit 托管（IP 取各自当前值，原位置的记录由守护移除）："
                f"\n{names}{more}")

        # 接管模式整组隐去「域名 / IP / 更新 / 解析方式 / 备注」：那几条各自保留自己的
        # IP（接管只让用户选分类），摆一格格子出来会让人以为能逐条改。
        for row in (self._f_host_row, self._f_ip_row,
                    self._f_auto_row, self._f_method_row, self._f_note_row):
            row.setVisible(not take)

        self._f_host.setText(entry["host"] if entry else "")
        # 编辑时域名是**主键**（``store.upsert`` 按它定位），故只读
        self._f_host.setReadOnly(mode == "edit")
        self._f_ip.setText(entry["ip"] if entry else "")
        self._f_auto.setCurrentIndex(1 if (entry or {}).get("auto") else 0)
        m = self._f_method.findData((entry or {}).get("method") or METHOD_AUTO)
        self._f_method.setCurrentIndex(m if m >= 0 else 0)
        self._f_note.setText((entry or {}).get("note", ""))
        idx = self._f_cat.findData((entry or {}).get("category") or DEFAULT_CATEGORY)
        self._f_cat.setCurrentIndex(idx if idx >= 0 else 0)
        self._sync_method_enabled()
        self._btn_apply.setText("接管" if take else "保存")
        self._show_page(self._edit_form)

    def _validate_form(self) -> "str | None":
        """保存前置门（注入给 `EditScreen` 的 `on_validate`）。

        接管模式不校验：它整组隐去了域名 / IP 那几格，只让用户选分类。
        返回文案 = 一个字都不落盘、红字落本屏。
        """
        if self._form_mode == "take":
            return None
        host = self._f_host.text().strip().rstrip(".").lower()
        if not valid_host(host):
            return "域名不合法：只允许字母、数字、点与连字符，且不能包含空格或换行。"
        ip = self._f_ip.text().strip()
        if not valid_ip(ip):
            return "IP 不合法：请填写合法的 IPv4 / IPv6 地址。"
        return None

    def _commit_form(self) -> None:
        """保存（注入给 `EditScreen` 的 `on_commit`）：接管模式走接管，其余落盘。"""
        if self._form_mode == "take":
            self._do_take()
            return

        host = self._f_host.text().strip().rstrip(".").lower()
        ip = self._f_ip.text().strip()
        auto = bool(self._f_auto.currentData())
        ok, err = store.upsert({
            "host": host,
            "ip": ip,
            "category": self._f_cat.currentData() or DEFAULT_CATEGORY,
            "auto": auto,
            "method": self._f_method.currentData() if auto else METHOD_AUTO,
            "note": self._f_note.text().strip(),
        })
        # ``store.upsert`` 的第一项是「是否有变化」：False ＋ err None = 内容没变，
        # 那与「写成功」一样是好结局（不必报错，只是没什么可说的）。
        if err:
            self._form_fb.error(f"保存失败：{err}")
            return
        self._reload()
        self._notify()
        self._back_to_table()
        # 「没变化」不是成功也不是失败：盘上没动过，用成功档（绿）等于把「没做事」
        # 说成「做了事」。分开说（用户 2026-09-30 要求反馈分档后）。
        if ok:
            self._fb.info(f"已保存条目 {host}。")
        else:
            self._fb.hint(f"条目 {host} 没有变化，未写入。")

    def _do_take(self) -> None:
        """接管：以各条当前 IP 纳入托管，原位置记录由 helper 移除。"""
        rows = self._form_rows
        category = self._f_cat.currentData() or DEFAULT_CATEGORY
        for r in rows:
            store.upsert({
                "host": r.host, "ip": r.ip, "category": category,
                "auto": False, "method": METHOD_AUTO, "note": "",
            })
        self._reload()
        self._notify()
        self._back_to_table()
        self._fb.info(
            f"已纳入 {len(rows)} 条。原位置的记录会在守护下一次检查时从 "
            "/etc/hosts 移除。")

    # ---------- 表格页的动作 ----------

    def _begin_add(self) -> None:
        self._open_form("add", [])

    def _begin_edit(self) -> None:
        rows = [r for r in self._selected_rows() if r.source == SRC_MANAGED]
        if len(rows) == 1:
            self._open_form("edit", rows)

    def _begin_take(self) -> None:
        rows = [r for r in self._selected_rows() if r.source == SRC_EXTERNAL and not r.system]
        if rows:
            self._open_form("take", rows)
