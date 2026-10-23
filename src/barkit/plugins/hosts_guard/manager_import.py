"""批量导入页：粘贴 hosts 文本 ＋ 归入分类 ＋ 行内「新建分类」（不弹输入对话框）。

多行文本框吃掉多余高度（`EditScreen.stretch_content`）：不调它这个框会被压成一小条。

``manager_view.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
)

from barkit.plugins.hosts_guard import store
from barkit.plugins.hosts_guard.hostsfile import parse_import_text
from barkit.plugins.hosts_guard.models import DEFAULT_CATEGORY
from barkit.shell.ui_kit import (
    ComboField,
    EditScreen,
    note,
    tool_button,
)


class _ImportMixin:
    """批量导入页：搭建、实时统计、行内新建分类、校验与落盘。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 页 2：批量导入 ----------

    def _build_import_page(self) -> EditScreen:
        """页 2：批量导入 —— 版面归框架（`ui_kit.EditScreen`）。

        主动作文案是「导入」（`save_label`）；多行文本框是这一屏的主体，故
        :meth:`EditScreen.stretch_content` 让它吃掉多余高度（那一份默认归内容区下方的
        留白，不调它这个框会被压成一小条）。
        """
        form = EditScreen(
            save_label="导入",
            on_validate=self._validate_import,
            on_commit=self._commit_import,
            parent=self,
        )
        form.back.connect(self._back_to_table)
        form.stretch_content()
        self._import_fb = form.feedback

        group, body = form.add_group(stretch=1)
        body.addWidget(note(
            "粘贴 hosts 格式的文本（每行一条，如 0.0.0.0 ads.example.com）。"
            "空行、注释行、系统保留域名会被自动跳过。", group))

        self._i_text = QPlainTextEdit(group)
        self._i_text.setPlaceholderText(
            "0.0.0.0 ads.example.com\n0.0.0.0 tracker.example.com\n# 这是注释，会被跳过")
        self._i_text.textChanged.connect(self._refresh_import_stat)
        body.addWidget(self._i_text, 1)

        cat_bar = QHBoxLayout()
        cat_bar.setSpacing(8)
        cat_bar.addWidget(QLabel("归入分类", group))
        self._i_cat = ComboField(group)
        cat_bar.addWidget(self._i_cat, 1)
        body.addLayout(cat_bar)

        # 「新建分类」原来是一枚 ``QInputDialog.getText``（模态）。改成行内一格输入
        # ＋ 一枚按钮：**同样的能力、不再开窗**（排期表 §S1-a 的第三条映射）。
        new_bar = QHBoxLayout()
        new_bar.setSpacing(8)
        new_bar.addWidget(QLabel("新建分类", group))
        self._i_new_cat = QLineEdit(group)
        self._i_new_cat.setPlaceholderText("如「工作」「家庭」")
        self._i_new_cat.returnPressed.connect(self._new_category)
        new_bar.addWidget(self._i_new_cat, 1)
        btn_new_cat = tool_button("新建", parent=group)
        btn_new_cat.clicked.connect(self._new_category)
        new_bar.addWidget(btn_new_cat)
        body.addLayout(new_bar)

        self._i_stat = QLabel("", group)
        body.addWidget(self._i_stat)

        self._refresh_import_stat()
        return form

    # ---------- 导入页 ----------

    def _refresh_import_stat(self) -> None:
        pairs, skipped = parse_import_text(self._i_text.toPlainText())
        self._i_stat.setText(f"识别到 {len(pairs)} 条，跳过 {skipped} 行")

    def _new_category(self) -> None:
        """行内新建分类（取代原来的 ``QInputDialog.getText``）。"""
        name = self._i_new_cat.text().strip()
        if not name:
            self._import_fb.error("分类名不能为空。")
            return
        ok, err = store.add_category(name)
        if not ok:
            self._import_fb.error(f"无法新建分类：{err or '名称无效'}")
            return
        self._i_new_cat.clear()
        self._categories = store.load_categories()
        self._refresh_category_combos()
        idx = self._i_cat.findData(name)
        if idx >= 0:
            self._i_cat.setCurrentIndex(idx)
        self._import_fb.info(f"已新建分类「{name}」。")

    def _validate_import(self) -> "str | None":
        """导入前置门（注入给 `EditScreen` 的 `on_validate`）：得真有可导入的行。"""
        pairs, _skipped = parse_import_text(self._i_text.toPlainText())
        if not pairs:
            return ("没有可导入的条目 —— 请粘贴 hosts 格式的文本"
                    "（如 0.0.0.0 ads.example.com）。")
        return None

    def _commit_import(self) -> None:
        """导入（注入给 `EditScreen` 的 `on_commit`）：落盘后回表页并报计数。"""
        pairs, skipped = parse_import_text(self._i_text.toPlainText())
        category = self._i_cat.currentData() or DEFAULT_CATEGORY
        added, updated, _skipped = store.import_entries(pairs, category=category)
        self._i_text.clear()
        self._reload()
        self._notify()
        self._back_to_table()
        self._fb.info(f"导入完成：新增 {added} 条，覆盖 {updated} 条，跳过 {skipped} 行。")
