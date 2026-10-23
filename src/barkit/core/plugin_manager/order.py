"""用户排序与上下移（§6.4）。

``plugin_manager.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations


class _OrderMixin:
    """用户排序：工具栏顺序（§6.4）与上移下移两条口子。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 排序（§6.4 用户排序，D1 最简上移/下移） ----------

    def effective_order(self) -> list[str]:
        """当前生效的插件间顺序：用户排序在前，未列入的按 Plugin.order 追加。"""
        user = list(self._config.plugin_order())
        known = set(self._registry.discovered_classes)
        user = [pid for pid in user if pid in known]
        tail = [
            pid for pid in self._registry.discovered_classes
            if pid not in user
        ]
        tail.sort(key=lambda pid: getattr(self._registry.discovered_classes[pid], "order", 0)
                  if isinstance(getattr(self._registry.discovered_classes[pid], "order", 0), int) else 0)
        return user + tail

    def reorder_plugin(self, plugin_id: str, to_index: int) -> bool:
        """把插件落到生效顺序的 to_index（绝对下标），持久化到 plugin_order。

        语义与 ``shell.OrderEditor`` 的 ``on_reorder(key, to_index)`` 契约
        一致：``to_index`` 是 pid **移除自身后**剩余序列中的插入下标，故
        ``new_order = others[:to_index] + [pid] + others[to_index:]``。

        落库语义唯一：拖拽、键盘 Alt+↑/↓、以及 ``move_plugin`` 全部收敛到本方法，
        避免「按格交换」与「按下标插入」两套语义漂移。越界下标夹取到
        ``[0, len(others)]``；结果与当前顺序相同则视为无操作（不写盘、不刷 UI）。
        """
        order = self.effective_order()
        if plugin_id not in order:
            return False
        others = [pid for pid in order if pid != plugin_id]
        to_index = max(0, min(to_index, len(others)))
        new_order = others[:to_index] + [plugin_id] + others[to_index:]
        if new_order == order:
            return False
        self._config.set_plugin_order(new_order)
        self._refresh_panel()
        self._notify_changed()
        return True

    def move_plugin(self, plugin_id: str, delta: int) -> bool:
        """把插件在排序中相对移动 ``delta`` 格（负=上移，正=下移），持久化落库。

        ``move_plugin`` 只是 ``reorder_plugin`` 的一格特例（保留给键盘/CLI 等
        无需自己算绝对下标的调用方），落库路径同源，因此 ``delta`` 的语义是
        **位移**：「上移两位」= 该插件插到原位置上两格处，其余项相对序不变。

        历史注记：本方法原为 ``order[i], order[i+delta] = 交换``，对 ``|delta| > 1``
        会产生「不是移动、而是换位」的结果（``[a,b,c]`` 上移两位 → ``[c,b,a]``），
        与 ``reorder_plugin`` 的下标插入模型漂移。UI 只以 ±1 调用，故两者对
        ±1 完全等价；现统一到插入模型后 ``|delta| > 1`` 也有定义。
        """
        order = self.effective_order()
        if plugin_id not in order:
            return False
        return self.reorder_plugin(plugin_id, order.index(plugin_id) + delta)
