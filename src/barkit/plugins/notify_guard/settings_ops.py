"""``_RulesOpsMixin``：拖排序、装置拨测、三个回执出口与保存。

``settings.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from barkit.contribution.specs import (
    ActionResult,
    ActionOutcome,
)
from barkit.plugins.notify_guard import diagnostics
from barkit.shell.ui_kit import (
    FeedbackBar,
    apply_text_role,
)


class _RulesOpsMixin:
    """排序、装置运维（拨测）与三个回执出口，以及保存与可见反馈条。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    def _on_rule_reorder(self, key: str, to_index: int) -> bool:
        """规则排序落库（``OrderEditor.on_reorder``）：返回 False 时编辑器自行回滚。

        键 → 规则对象 → 新数组：**全程不碰下标残留**。返回 False 的两种情形都真的回滚：
        写盘失败时内存态也退回原序（与 `_do_reset` / `_do_delete` 同一口径 ——
        内存里换了序而盘上没换，下一次开机就变脸）。
        """
        rule = self._key_rule.get(key)
        if rule is None:
            return False
        others = [r for r in self._rules if r is not rule]
        target = max(0, min(to_index, len(others)))
        new_rules = others[:target] + [rule] + others[target:]
        if new_rules == self._rules:
            return False
        backup = list(self._rules)
        self._rules = new_rules
        if not self._plugin.save_rules(self._rules):
            self._rules = backup
            self._list_fb.error("保存失败——写回配置没成功，请重试。")
            return False
        return True

    def _on_probe(self) -> None:
        """拨测：把插件读到的**只读事实**排成五行（排版归 ``diagnostics``）。

        只上屏、不进剪贴板 —— 进剪贴板那一路是本卡同一排的「复制诊断」（框架件
        ``data_ops`` 画的按钮，内容取 ``plugin.diagnostics_text()``，与这里是同一批
        事实、同一个排版层）。
        """
        lines = diagnostics.format_probe(self._plugin.probe())
        apply_text_role(self._probe_lbl, "caption")
        self._probe_lbl.setText("\n".join(lines))
        self._probe_lbl.setVisible(True)

    def _say_result(self, result: "ActionResult | None") -> None:
        """把插件动作的结局说在**行内**（``None`` / 空文案 = 没话要说）。

        本页「恢复自动」用：各写一遍「OK 就 info、否则 error」迟早分叉，而这是一条会随
        `ActionOutcome` 取值增加而变形的规则。
        """
        if result is None or not result.text:
            return
        if result.outcome is ActionOutcome.OK:
            self._list_fb.info(result.text)
        else:
            self._list_fb.error(result.text)

    def _open_edit_key(self, key: str) -> None:
        """行点击：键 → 规则对象 → **当下**的下标（列表重建过也仍指同一条规则）。"""
        rule = self._key_rule.get(key)
        if rule is None:
            return
        for i, r in enumerate(self._rules):
            if r is rule:
                self._open_edit(i)
                return

    # ---------- 内部 ----------

    def _save(self) -> bool:
        if self._plugin.save_rules(self._rules):
            return True
        # 错误要落在**用户正看着的那一页**：删除失败时还在编辑页，保存失败时已回列表。
        self._visible_fb().error("保存失败——写回配置没成功，请重试。")
        return False

    def _visible_fb(self) -> FeedbackBar:
        """当前可见页的反馈槽（编辑屏 = 换屏容器里那一刻在台上的那一屏）。"""
        return self._edit_fb if self._stack.current() is self._edit else self._list_fb

    def _unique_name(self, base: str) -> str:
        names = {str(r.get("name") or "") for r in self._rules}
        if base not in names:
            return base
        i = 2
        while f"{base}{i}" in names:
            i += 1
        return f"{base}{i}"
