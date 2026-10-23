"""``_RulesFlowMixin``：push 导航、进程选中、挑预设与增删。

``settings.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations


from .settings_words import RULE_PRESETS


class _RulesFlowMixin:
    """导航与增删改：打开 / 返回、选中进程、挑预设、新增与删除。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    def _on_pick(self, name: str, on: bool) -> None:
        if on:
            self._picked.add(name)
        else:
            self._picked.discard(name)

    def _add_manual(self, name: str) -> None:
        self._picked.add(name)
        self._filter_edit.clear()      # clear 会触发 textChanged → 自动重填
        self._refill_processes()

    # ---------- 交互 ----------

    def _open_edit(self, idx: int) -> None:
        rule = self._rules[idx]
        self._editing = idx
        self._name_edit.setText(str(rule.get("name") or ""))
        self._reason_edit.setText(str(rule.get("reason") or ""))
        self._picked = {str(p) for p in (rule.get("processes") or []) if p}
        self._filter_edit.clear()
        self._edit.set_title(str(rule.get("name") or "未命名"))
        self._edit_fb.dismiss()          # 换了一条规则，上一条的提示/确认都不再适用
        self._refill_processes()
        self._stack.push(self._edit)

    def _on_back(self) -> None:
        """返回列表（丢弃本次未保存的字段改动——保存是显式的「保存」按钮）。"""
        self._editing = None
        self._edit_fb.dismiss()
        self._stack.back()
        self._refresh_list()

    def _on_add(self) -> None:
        self._rules.append({"name": self._unique_name("新规则"), "reason": "", "processes": []})
        if not self._save():
            self._rules.pop()
            return
        self._refresh_list()
        self._open_edit(len(self._rules) - 1)

    def _on_preset(self) -> None:
        """从预设加一条规则（同名已存在则跳过，不覆盖用户的改动）。

        第二次点击（或盘上已齐四条时）走「都已存在」分支 —— 这里**以前是卡死的源头**：
        原实现弹 `QMessageBox.information`，在置顶设置窗里开嵌套事件循环（见模块 docstring）。
        """
        existing = {str(r.get("name") or "") for r in self._rules}
        added: list[str] = []
        for name, reason, procs in RULE_PRESETS:
            if name in existing:
                continue
            self._rules.append({"name": name, "reason": reason, "processes": list(procs)})
            added.append(name)
        if not added:
            self._list_fb.hint("四条常用场景都已存在，未改动。")
            return
        if self._save():
            self._refresh_list()
            self._list_fb.info("已补齐 " + "、".join(added) + "。")

    def _on_delete(self) -> None:
        if self._editing is None:
            return
        rule = self._rules[self._editing]
        self._edit_fb.ask(
            f"确定删除规则「{rule.get('name')}」？此操作不可恢复。",
            "删除",
            self._do_delete,
            confirm_kind="danger",
        )

    def _do_delete(self) -> None:
        if self._editing is None:
            return
        idx = self._editing
        removed = self._rules.pop(idx)
        if not self._save():
            self._rules.insert(idx, removed)     # 落盘失败 → 回滚内存
            return
        self._editing = None
        self._stack.back()
        self._refresh_list()
        self._list_fb.info(f"已删除规则「{removed.get('name')}」。")
