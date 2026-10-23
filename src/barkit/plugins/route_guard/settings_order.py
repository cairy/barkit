"""**拖序**（列表顺序即优先级）与两个内部小工具：草稿下标跟随（它记的是下标、不是对象）与「起一个不重名的场景名」。

``settings.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations


class _SceneOrderMixin:
    """拖序与内部小工具。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 拖序 ----------

    def _on_rows_moved(self, _parent, start: int, _end: int, _dest, row: int) -> None:
        """拖拽调序 = 改配置（§2.1：列表顺序即优先级），攒进内存等「保存」。

        Qt 的语义：``row`` 是**尚未移除源行时**的目标插入位，故源行在目标之前时要减一
        （否则「把第 1 项拖到第 3 项」会落成只挪一位）。
        """
        if self._loading:
            return
        self._reorder(start, row - 1 if row > start else row)

    def _reorder(self, src: int, dst: int) -> None:
        if not (0 <= src < len(self._profiles)) or not (0 <= dst < len(self._profiles)):
            return
        if src == dst:
            return
        profile = self._profiles.pop(src)
        self._profiles.insert(dst, profile)
        self._shift_draft(src, dst)
        self._rebuild_list()
        self._select(dst if 0 <= dst < len(self._profiles) else None)
        if self.dirty():
            self._fb.info(f"「{profile.get('name')}」已排到第 {dst + 1} 位"
                          "——点「保存」才会写进配置。")

    # ---------- 内部 ----------

    def _shift_draft(self, src: int, dst: int) -> None:
        """拖序之后草稿下标跟着走（它记的是**下标**，不是对象）。"""
        d = self._draft
        if d is None:
            return
        if d == src:
            self._draft = dst
        elif src < d <= dst:
            self._draft = d - 1
        elif dst <= d < src:
            self._draft = d + 1

    def _unique_name(self, base: str) -> str:
        names = {str(p.get("name") or "") for p in self._profiles}
        if base not in names:
            return base
        n = 2
        while f"{base}{n}" in names:
            n += 1
        return f"{base}{n}"
