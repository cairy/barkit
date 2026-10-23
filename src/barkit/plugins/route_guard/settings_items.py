"""路由清单（逐条行）：行的增删与回收，以及「把行收成清单」。

每行三格：**开关 ＋ 目标 ＋ 下一跳**，末尾一枚 ✕。目标那一格留空的行直接丢 ——
用户点「＋ 添加」后还没填，空目标不算一条。

逐条开关（v7，2026-10-06）：关掉的那条**不写进线格式的增量**，只列进 ``off_destinations``
让 helper 去撤 —— 一位 ``enabled`` 装不下「同一条里 A 加、B 撤」，此前只能整条一起开关。

``settings.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QWidget,
)

from barkit.shell.ui_kit import (
    Switch,
    note,
    tool_button,
)


class _SceneItemsMixin:
    """路由清单（逐条行）。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 路由清单（逐条） ----------

    def _clear_items(self) -> None:
        while self._items_lay.count():
            item = self._items_lay.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._item_rows = []
        self._placeholder = None

    def _refill_items(self, profile: dict) -> None:
        """按场景里的清单重建逐条行（开关 ＋ 目标 ＋ 下一跳 ＋ ✕）。"""
        self._clear_items()
        items = [it for it in ((profile.get("routes") or {}).get("items") or [])
                 if isinstance(it, dict)]
        for item in items:
            self._add_item_row(
                str(item.get("destination") or ""),
                str(item.get("via") or ""),
                # 只有**明写** false 才算关（与 `settings_list` / `subtitle` 的读法同一口径）
                item.get("enabled", True) is not False,
            )
        if not items:
            self._placeholder = note("还没有路由目标——点上面的「＋ 添加」。", self._items_box)
            self._items_lay.addWidget(self._placeholder)
        self._items_lay.addStretch(1)

    def _add_item_row(self, destination: str, via: str, enabled: bool = True) -> QWidget:
        if self._placeholder is not None:
            self._placeholder.setParent(None)
            self._placeholder.deleteLater()
            self._placeholder = None
        row = QWidget(self._items_box)
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        dest = QLineEdit(destination, row)
        dest.setPlaceholderText("如 10.99.2.0/24")
        via_edit = QLineEdit(via, row)
        via_edit.setPlaceholderText("下一跳（留空 = 匹配网关 / 固化租约的）")
        dest.editingFinished.connect(self._collect_fields)
        via_edit.editingFinished.connect(self._collect_fields)
        # 逐条开关：关掉 = 这一条不加，其余照旧。放在 ✕ **之前**、元组**末尾** ——
        # 两处既有读法（删行取 `[0]`、加行后聚焦目标格取 `[1]`）一个字都不用改。
        sw = Switch(row)
        sw.setChecked(enabled)
        sw.setToolTip("这一条要叠：关掉 = 不加它（其余照旧）")
        sw.clicked.connect(lambda _b=None, b=sw: self._on_item_toggled(b))
        remove = tool_button("✕", "pill-ghost", size="inline", parent=row)
        remove.clicked.connect(lambda _checked=False, r=row: self._remove_item_row(r))
        h.addWidget(dest, stretch=3)
        h.addWidget(via_edit, stretch=2)
        h.addWidget(sw)
        h.addWidget(remove)
        # 插在末尾那个 stretch 之前（`_refill_items` 保证它一直在）
        self._items_lay.insertWidget(max(0, self._items_lay.count() - 1), row)
        self._item_rows.append((row, dest, via_edit, sw))
        return row

    def _remove_item_row(self, row: QWidget) -> None:
        keep = [pair for pair in self._item_rows if pair[0] is not row]
        if len(keep) == len(self._item_rows):
            return
        self._item_rows = keep
        row.setParent(None)
        row.deleteLater()
        self._collect_fields()

    def _collect_items(self) -> list[dict]:
        """把逐条行收成清单：目标空的行直接丢（用户点「＋ 添加」后还没填）。

        未勾选的行写 ``enabled: False``；勾选**不写键** —— 与全仓写侧口径一致
        （等于声明默认值即删键），免得盘上多出一堆 ``enabled: true``。
        """
        out: list[dict] = []
        for _row, dest, via_edit, sw in self._item_rows:
            destination = dest.text().strip()
            if not destination:
                continue
            item = {"destination": destination}
            via = via_edit.text().strip()
            if via:
                item["via"] = via          # 留空即不写：回落 match.gateway / 固化租约的网关
            if not sw.isChecked():
                item["enabled"] = False
            out.append(item)
        return out
