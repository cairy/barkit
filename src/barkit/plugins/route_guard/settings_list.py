"""左列表的**装载与选中**：行文案（含「不加路由 / 还没填完」两个后缀）、重建、只改一行，以及「选中某一条 ⇒ 把它装进右表单」。

列表顺序即优先级（§2.1），故拖序是**改配置**而不是改外观 —— 那一段在 ``settings_order``。

``settings.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtWidgets import QListWidgetItem

from .settings_netform import _NET_DRAFT_KEY, format_iface_field


class _SceneListMixin:
    """左列表的装载与选中，以及「把某一条装进右表单」。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 列表装载 / 选中 ----------

    def _row_label(self, idx: int) -> str:
        """列表项文案：名字 ＋ 一眼可见的状态后缀（不加路由、还没填完）。

        这两个后缀说**同一件事**：这一条现在**保存不进去**。它们与「整表有没有保存过」
        无关 —— 那是窗体级的状态（由框架壳那枚「保存」键和关窗那一次询问承担），挂在
        每一行上只会变成一行行重复的噪声。判据不同故仍然分开说：``_draft`` 是整条场景
        还没填完（缺监控网卡），`_NET_DRAFT_KEY` 只是网卡那一格还没填完。
        """
        profile = self._profiles[idx]
        name = str(profile.get("name") or "?")
        if (profile.get("routes") or {}).get("enabled", True) is False:
            name += "（不加路由）"
        if self._draft == idx:
            name += "（还没填完）"
        elif profile.get(_NET_DRAFT_KEY):
            name += "（网卡未填完）"
        return name

    def _rebuild_list(self) -> None:
        """按内存里的场景表重建左列表。**不动选中行**（选中由 `_select` 负责）。"""
        self._loading = True
        self._list.clear()
        for i in range(len(self._profiles)):
            QListWidgetItem(self._row_label(i), self._list)
        self._loading = False

    def _update_row_label(self, idx: int) -> None:
        """只改一行的文案（改名 / 开关翻转后）。
        不重建整个列表：重建会动选中行，而这儿常常正处在「选中行刚变」的回调里。"""
        item = self._list.item(idx)
        if item is not None:
            item.setText(self._row_label(idx))

    def _select(self, idx: int | None) -> None:
        """选中某一项并把它装进表单（**程序化**选中：`_loading` 期不发装载信号）。"""
        self._loading = True
        if idx is None or not (0 <= idx < self._list.count()):
            self._list.clearSelection()
            self._list.setCurrentRow(-1)
            self._loading = False
            self._load_scene(None)
            return
        self._list.setCurrentRow(idx)
        self._loading = False
        self._load_scene(idx)

    def _on_pick(self, row: int) -> None:
        """用户改选中行：**先把上一条的字段收进内存**，再装载新的一条。

        不再需要「收不进去就不许换」那道守卫了 —— 收进内存不会失败（会失败的是保存
        那一刻）。换场景以前是一次保存点，现在它只是换一格看。
        """
        if self._loading:
            return
        prev = self._editing
        if prev is not None and prev != row:
            self._collect_fields()
        if 0 <= row < len(self._profiles):
            self._load_scene(row)
        else:
            self._editing = None

    def _load_scene(self, idx: int | None) -> None:
        """把场景 ``idx`` 装进表单；``None`` = 没有场景（清空并禁用表单）。"""
        self._editing = idx
        editable = idx is not None and 0 <= idx < len(self._profiles)
        self._editable = editable
        self._loading = True
        if editable:
            profile = self._profiles[idx]
            self._name.setText(str(profile.get("name") or ""))
            self._iface.setText(format_iface_field(profile.get("ifaces")))
            match = profile.get("match") or {}
            self._gw.setText(str(match.get("gateway") or ""))
            self._subnet.setText(str(match.get("subnet") or ""))
            self._manual.setChecked(profile.get("manual_only") is True)
            self._routes_on.setChecked(
                (profile.get("routes") or {}).get("enabled", True) is not False
            )
            self._load_net(profile)
        else:
            self._name.clear()
            self._iface.clear()
            self._gw.clear()
            self._subnet.clear()
            self._manual.setChecked(False)
            self._routes_on.setChecked(True)
            self._load_net({})
        self._loading = False
        for widget in (self._name, self._iface, self._routes_on, self._del):
            widget.setEnabled(editable)
        self._refill_items(self._profiles[idx] if editable else {})
        # 匹配那两格与网卡那一块的可用性各有各的判据，各自收口（别在这里再写一遍）
        self._refresh_match_ui()
        self._refresh_net_ui()
        self._refresh_pin()
