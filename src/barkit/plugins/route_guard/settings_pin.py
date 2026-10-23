"""页脚那枚 ``[固定使用]``（``pinned``，§2.3）：按钮文案跟着「当前这条是不是被锁着的那条」走，按下那一下与面板徽标**同一个落点**（``plugin.set_pinned`` / ``clear_pinned``）。

``settings.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations


class _ScenePinMixin:
    """固定使用（pinned，§2.3）。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 固定使用（`pinned`，§2.3） ----------

    def _refresh_pin(self) -> None:
        """[固定使用] 的文案跟着「这一条是不是被锁着的那条」走。

        一个按钮两个意思（固定 / 取消）是刻意的：它本来就是同一个开关的两面，拆成两个
        按钮只会让用户在「我现在到底锁没锁」上多问一次。草稿（还没填完、没落盘）不能固定
        ——``plugin.set_pinned`` 认的是**配置里存在**的场景名，草稿还不存在。
        """
        idx = self._editing
        name = ""
        if idx is not None and 0 <= idx < len(self._profiles):
            name = str(self._profiles[idx].get("name") or "").strip()
        pinned = bool(name) and self._plugin.pinned_scene() == name
        self._pin.setText("已固定 · 取消" if pinned else "固定使用")
        self._pin.setEnabled(bool(name) and self._draft != idx)

    def _on_pin(self) -> None:
        """[固定使用] / [取消固定]：把 ``pinned`` 指向（或不再指向）当前场景（§2.3）。

        与面板徽标那条路径**同一个落点**（``plugin.set_pinned`` / ``clear_pinned``）：
        两个入口、一份写入逻辑。这个入口不是冗余 —— ``manual_only`` 的场景**无法自动
        触达**（§2.6），而配置窗体恰恰是唯一看得见它的地方。
        """
        idx = self._editing
        if idx is None or not (0 <= idx < len(self._profiles)):
            return
        if self._draft == idx:
            self._fb.hint("这个场景还没填完（网卡空着），填完这一条就会自动存下来。")
            return
        name = str(self._profiles[idx].get("name") or "").strip()
        if not name:
            self._fb.hint("先给这个场景起个名字。")
            return
        if self._plugin.pinned_scene() == name:
            self._plugin.clear_pinned()
            self._fb.info(f"已取消固定「{name}」——交回按网段 / 网关自动挑。")
        else:
            self._plugin.set_pinned(name)
            self._fb.info(f"已固定使用「{name}」——忽略匹配，一直用它。")
        self._refresh_pin()
        self._status.setText(self._plugin.detail_status_text())
