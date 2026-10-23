"""数据那一层：重载（条目主数据 ＋ /etc/hosts ＋ 插件给的解析状态）、三个分类下拉的同步、
合并成展示行，以及变更回调。

生效 IP 与解析状态由**插件**注入（``status_index``），本层不碰数据文件也不自己判状态。
三个下拉共用 ``store.load_categories()`` 一份真源 —— 新建分类后一处刷新、三处同步。

``manager_view.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from barkit.plugins.hosts_guard import store
from barkit.plugins.hosts_guard.hostsfile import read_records
from barkit.plugins.hosts_guard.models import (
    CAT_ADBLOCK,
    CATEGORY_LABELS,
    DEFAULT_CATEGORY,
)

from .manager_consts import SRC_EXTERNAL, SRC_MANAGED, log
from .manager_rows import Row


class _DataMixin:
    """数据：条目主数据与 /etc/hosts 合并成展示行、三个分类下拉的同步、变更回调。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 数据 ----------

    def _reload(self) -> None:
        """重新读取条目主数据 + /etc/hosts + 插件给的解析状态，重建行并刷新三个分类下拉。"""
        self._entries = store.load_entries()
        self._categories = store.load_categories()
        self._refresh_category_combos()
        self._collect()
        self._apply_filter()

    def _refresh_category_combos(self) -> None:
        """把分类列表灌进三个下拉（过滤 / 表单 / 导入），各自**保留当前选中项**。

        三处共用一份真源（``store.load_categories()``）：新建分类后一处刷新、三处同步 ——
        这正是「模块级单一真源」要防的那种漂移（旧版只有一个下拉，多出来的两处是新加的）。
        """
        prev = self._cat_filter.currentData()
        self._cat_filter.blockSignals(True)
        self._cat_filter.clear()
        self._cat_filter.addItem("全部分类", "")
        for key in self._categories:
            self._cat_filter.addItem(CATEGORY_LABELS.get(key, key), key)
        idx = self._cat_filter.findData(prev)
        self._cat_filter.setCurrentIndex(idx if idx >= 0 else 0)
        self._cat_filter.blockSignals(False)

        for combo, fallback in ((self._f_cat, DEFAULT_CATEGORY), (self._i_cat, CAT_ADBLOCK)):
            prev = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            for key in self._categories:
                combo.addItem(CATEGORY_LABELS.get(key, key), key)
            want = prev if prev in self._categories else fallback
            idx = combo.findData(want)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)

    def _collect(self) -> None:
        """合并「托管条目」与「外部条目」成展示行。

        生效 IP 与解析状态由**插件**给（``self._status_index()``，即
        ``plugin.status_index``）：UI 既不碰数据文件，也不自己判状态。
        理由与门禁见类 docstring。
        """
        state = self._status_index()

        rows = []
        for e in self._entries:
            st = state.get(e["host"], {})
            # IP 列优先取**生效值**（解析出来的），索引给不出才回落条目里存的那个静态 IP；
            # 状态列同理，给不出就落 "idle"（它的文案是「待解析」＝还没解析过）。
            rows.append(Row(
                SRC_MANAGED, e["host"], st.get("ip") or e["ip"], e["category"],
                bool(e["auto"]), e["method"], st.get("status", "idle"),
                pending=bool(st.get("pending")),
            ))
        managed_hosts = {e["host"] for e in self._entries}

        records, err = read_records()
        if err:
            log.warning("读取 /etc/hosts 失败：%s", err)
        for r in records:
            if r.host in managed_hosts:
                continue            # 已托管：只显示托管行，避免重复
            rows.append(Row(SRC_EXTERNAL, r.host, r.ip, system=r.system))

        rows.sort(key=lambda r: (r.source != SRC_MANAGED, r.host))
        self._rows = rows

    def _notify(self) -> None:
        if self._on_changed is not None:
            self._on_changed()
