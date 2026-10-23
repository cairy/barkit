"""``_SearchMixin`` —— 设置项可发现性（ROADMAP F1 ④）：侧栏那枚搜索框与它的索引。

为什么要有这一格
----------------
设置项分布在「通用 / 快捷键 / 插件 / N 个组件页 / 维护 / 关于」里，而**组件页里的字段**
更要紧 —— 用户想改的是「运行间隔」「固化 IP」这类东西，他不知道它在哪一页、甚至不知道
有没有这一项。此前全仓没有「到哪找某个开关」的出口，只能一页一页翻。

检索面
------
``页面名 ＋ 组件 id ＋ 该组件 ``config_schema()`` 里每个字段的标签与键名``。字段那份
**不另写一份索引**：schema 是配置字段的唯一真源（§9.5），抄一份出来就是第二份，改了
字段那边不报错 —— 与「默认值单一真源」同一条纪律。

只做**过滤**，不做「滚动到具体控件并高亮」：后者要给每个控件挂 id、还要在重建分页后
重新定位，成本全在框架侧，而「跳到那一页」已经解决了「不知道在哪」这件主要的事。

``settings.py`` 拆包：新模块，零行为变更（搜索框为空时与从前逐字等价）。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QVBoxLayout,
    QWidget,
)

from barkit.shell.ui_kit import EmptyState, search_field


class _SearchMixin:
    """侧栏搜索：索引、过滤、空态。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 侧栏骨架 ----------

    def _build_sidebar(self) -> QWidget:
        """侧栏（搜索框 ＋ 导航列表 ＋ 空态那句）：``_build_ui`` 一次建好。

        导航列表由调用方建好后经 ``self._nav`` 取用 —— 它的构造与「页表」绑在一起
        （``_page_entries``），搬到这里会让「页从哪来」多一个地方。
        """
        side = QWidget()
        lay = QVBoxLayout(side)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        self._search = search_field(
            "搜索设置项", on_changed=self._apply_search, parent=side,
        )
        # ``objectName`` 归框架侧（``findChild`` 用），组件盒不管它 —— 故建好后自己贴。
        self._search.setObjectName("set-search")
        lay.addWidget(self._search)

        lay.addWidget(self._nav, stretch=1)

        self._no_match = EmptyState("没有匹配的设置项", "换个说法试试", side)
        self._no_match.setVisible(False)
        lay.addWidget(self._no_match)
        return side

    # ---------- 索引 ----------

    def _build_search_index(self, pages) -> None:
        """每页一段可检索文本；与导航行**同序**（下标即行号）。

        ``pages`` 是 ``_page_entries()`` 的三元组（标签 / 构建器 / 组件 id）。
        """
        hay: "list[str]" = []
        fields: "list[list[str]]" = []
        for label, _builder, pid in pages:
            terms = self._plugin_field_terms(pid)
            fields.append(terms)
            hay.append(" ".join([str(label), str(pid or ""), *terms]).lower())
        self._search_hay = hay
        self._search_fields = fields

    def _plugin_field_terms(self, pid: str) -> "list[str]":
        """该组件配置字段的标签与键名（**现取** ``config_schema()``，不抄一份索引）。

        取不到（没装配 / 组件没声明 schema / 声明里抛异常）就当这个组件没有可检索字段：
        搜索是**辅助**，缺了它不该拖垮设置窗，更不该让「通用」「维护」这些框架页消失。
        """
        if not pid or self._manager is None:
            return []
        try:
            inst = self._manager.plugin_instance(pid)
            if inst is None:
                return []
            schema = inst.config_schema()
        except Exception:  # noqa: BLE001 —— 索引取不到只是搜不到，不是错误
            return []
        out: "list[str]" = []
        for f in getattr(schema, "fields", ()) or ():
            for attr in ("label", "key"):
                val = str(getattr(f, attr, "") or "").strip()
                if val:
                    out.append(val)
        return out

    # ---------- 过滤 ----------

    def _apply_search(self, text: str = "") -> None:
        """按输入过滤侧栏。空输入 ⇒ 全部显示（与没有搜索框时逐字等价）。

        **用隐藏行而不是重建列表**：行号必须与内容栈的下标一一对应
        （``nav.currentRowChanged`` → ``stack.setCurrentIndex``），重建一次下标就错位，
        而那正是「点进去的是另一页」这种最难查的错。
        """
        nav = getattr(self, "_nav", None)
        if nav is None or not getattr(self, "_search_hay", None):
            return
        q = str(text or "").strip().lower()
        shown = 0
        for i, hay in enumerate(self._search_hay):
            hit = (not q) or (q in hay)
            nav.setRowHidden(i, not hit)
            nav.item(i).setToolTip(
                self._hit_tooltip(i, q) if (q and hit) else ""
            )
            shown += 1 if hit else 0
        if self._no_match is not None:
            self._no_match.setVisible(bool(q) and shown == 0)
        if not q:
            return
        # 当前那一行被过滤掉了 ⇒ 落到第一个还看得见的：留在隐藏行上会出现「内容区
        # 显示的是别的页」，而侧栏看不出自己指的是谁。
        cur = nav.currentRow()
        if cur < 0 or nav.isRowHidden(cur):
            for i in range(nav.count()):
                if not nav.isRowHidden(i):
                    nav.setCurrentRow(i)
                    break

    def _hit_tooltip(self, row: int, q: str) -> str:
        """这一页里**哪几个字段**命中了（页面名本身命中时不给 —— 那就没什么可补充的）。"""
        hits = [t for t in self._search_fields[row] if q in t.lower()]
        if not hits:
            return ""
        # 去重保序（标签与键名常常是同一项写两遍，如 key=label）
        seen: "list[str]" = []
        for h in hits:
            if h not in seen:
                seen.append(h)
        return "命中字段：" + "、".join(seen[:6])
