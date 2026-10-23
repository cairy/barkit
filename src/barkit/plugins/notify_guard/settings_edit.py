"""``_RulesEditMixin``：编辑页表单、进程候选重填、校验与提交。

``settings.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from barkit.shell.ui_kit import (
    EditScreen,
    check_field,
    form_row_stacked,
    note,
    tool_button,
)


class _RulesEditMixin:
    """编辑页那半：单条表单的构建、进程候选重填，以及提交前的校验与落盘。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 第 2 屏：单条规则编辑 ----------

    def _build_edit_page(self) -> EditScreen:
        """第 2 屏：单条规则编辑 —— 版面归框架（`ui_kit.EditScreen`）。

        ◷ 2026-09-27：顶上那行 ``[‹ 返回] 标题``、页脚 ``[删除] … [保存]``、末尾那条
        ``FeedbackBar`` 此前是本文件自己拼的（另两处同模板编辑器逐字相同）—— 现在由
        `EditScreen` 给：四区固定、位置归框架。**观感变更有两处**（进真机清单）：
        反馈槽由按钮**下方**移到**上方**、内容不再自带一块卡面（整屏已是 ``surface2``
        的一块面，面里再铺底就是两层）。

        保存那一道**注入**（`EditScreen` 是组合件，在本文件覆写是调不到的）：
        :meth:`_validate_form` 过不了关就一个字不落盘，过了才走 :meth:`_commit_form`。
        """
        edit = EditScreen(
            on_validate=self._validate_form,
            on_commit=self._commit_form,
            parent=self,
        )
        edit.back.connect(self._on_back)
        # 这一屏的进程列表自带滚动区，要吃掉多余高度（否则它按 sizeHint 缩成一小条）
        edit.stretch_content()
        # 本页的错误提示与删除确认都落这一条槽（零模态框）
        self._edit_fb = edit.feedback

        # 这条规则的两个字段
        basic, basic_body = edit.add_group("这条规则")
        self._name_edit = QLineEdit(basic)
        self._name_edit.setPlaceholderText("如 会议")
        basic_body.addWidget(form_row_stacked("名称", self._name_edit, basic))

        self._reason_edit = QLineEdit(basic)
        self._reason_edit.setPlaceholderText("如 腾讯会议进行中")
        basic_body.addWidget(form_row_stacked(
            "原因文案（面板副标题里显示这句）", self._reason_edit, basic,
        ))

        # 进程匹配：筛选框 ＋ 勾选列表（筛选框的说明留在组内当小标题行，别丢语义）。
        # 这一组 ``stretch=1``：分组吃掉这一屏剩下的高度；列表本身按内容自然长高。
        proc, proc_body = edit.add_group("进程匹配", stretch=1)
        self._filter_edit = QLineEdit(proc)
        self._filter_edit.setPlaceholderText("筛选下面的进程，或直接输入未列出的名字")
        self._filter_edit.textChanged.connect(self._refill_processes)
        proc_body.addWidget(form_row_stacked(
            "命中任一即算这条规则；子串匹配", self._filter_edit, proc,
        ))

        self._proc_box = QWidget(proc)
        self._proc_lay = QVBoxLayout(self._proc_box)
        self._proc_lay.setContentsMargins(0, 0, 0, 0)
        self._proc_lay.setSpacing(2)
        # **不给勾选列表另套滚动区**（2026-10-06 删）：本屏住在设置页里，而设置页每个
        # 分页都由框架统一包进一层滚动区（`shell/settings/helpers.py::_scrolled`）。
        # 这里再套一层就是两层竖滚动条并存，滚轮只驱动鼠标底下那一条；内层那条还会被
        # 压扁（删前实测：宿主 490×360 时这一层只分到 30px 高）。候选项多了用上面那个
        # 筛选框收敛，滚动交给设置页那一层。
        proc_body.addWidget(self._proc_box, stretch=1)

        edit.add_destructive("删除这条规则", self._on_delete)
        return edit

    def _refill_processes(self) -> None:
        """重建进程勾选列表：候选 = 运行中进程 ∪ 已选（已配的即使没跑也要列出来）。

        「从正在运行的 App 里勾」才是可视化配置的本分（§6.4.4）——用户不该知道
        ``processes`` 里填的是**路径小写子串**。候选外的名字仍可手输（过滤框下方
        会出现一条「＋ 添加」，覆盖"要配的进程此刻没在跑"这一情形）。
        """
        candidates = set(self._plugin.process_candidates()) | self._picked
        keyword = self._filter_edit.text().strip().lower()
        shown = sorted(c for c in candidates if keyword in c) if keyword else sorted(candidates)
        while self._proc_lay.count():
            item = self._proc_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for name in shown:
            chk = check_field(
                name,
                checked=name in self._picked,
                on_toggled=lambda on, n=name: self._on_pick(n, on),
                parent=self._proc_box,
            )
            self._proc_lay.addWidget(chk)
        if keyword and keyword not in candidates:
            add = tool_button(f"＋ 添加「{keyword}」", "pill-ghost", size="inline", parent=self._proc_box)
            add.clicked.connect(lambda: self._add_manual(keyword))
            self._proc_lay.addWidget(add)
        if not shown and not keyword:
            self._proc_lay.addWidget(note("没有可列的进程（ps 快照为空）——直接在上面输入进程名。",
                                          self._proc_box))
        self._proc_lay.addStretch(1)

    def _validate_form(self) -> "str | None":
        """保存前置门（注入给 `EditScreen` 的 `on_validate`）：名称非空且不重名。

        返回文案 = **一个字都不落盘**、红字落本屏（那一道由 `EditScreen` 执行）。
        """
        if self._editing is None:
            return None
        name = self._name_edit.text().strip()
        if not name:
            return "请先给这条规则起个名字。"
        dup = any(
            str(r.get("name")) == name
            for i, r in enumerate(self._rules) if i != self._editing
        )
        if dup:
            return f"已存在名为「{name}」的规则，名称不能重复。"
        return None

    def _commit_form(self) -> None:
        """保存（注入给 `EditScreen` 的 `on_commit`）：写过盘才回列表并报「已保存」。"""
        if self._editing is None:
            self._stack.back()
            return
        name = self._name_edit.text().strip()
        self._rules[self._editing] = {
            "name": name,
            "reason": self._reason_edit.text().strip(),
            "processes": sorted(self._picked),
        }
        if not self._save():
            return
        self._editing = None
        self._stack.back()
        self._refresh_list()
        self._list_fb.info(f"已保存「{name}」。")
