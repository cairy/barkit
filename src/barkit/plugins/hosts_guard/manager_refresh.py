"""「更新」那一枚键 —— 本表唯一会发起网络请求的动作。

真正的解析在长驻服务线程上（插件目录禁线程）⇒ 按下之后只「登记 ＋ 把服务提前一拍」，
结果异步到：盯梢期间**不重建表**，只把那几条的 IP / 状态两格文本换成最新读数
（重建会掀掉选中与滚动位置）。次数用尽如实说超时，不谎称完成。

``manager_view.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtCore import QTimer

from .manager_consts import SRC_MANAGED, _REFRESH_POLLS, _REFRESH_POLL_MS


class _RefreshMixin:
    """「更新」那一枚键：登记请求、盯梢几眼、只重画那几格、收尾文案。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 表格页的动作：「更新」（唯一会发网络请求的那一枚） ----------

    def _begin_refresh(self) -> None:
        """「更新」：请插件把这些条目**现在**重解析一遍，并盯几眼结果。

        选中项里只取「托管 ＋ 自动更新」的：静态条目的 IP 就是用户写下的那个，没有
        「重新解析」可言（按钮的可用性判据同上，这里是第二道 —— 界面判据哪天写坏，
        后面这一道仍挡住「拿静态条目去发网络请求」）。
        """
        rows = [r for r in self._selected_rows() if r.source == SRC_MANAGED and r.auto]
        if not rows or self._on_refresh is None:
            return
        hosts = [r.host for r in rows]
        accepted, woken = self._on_refresh(hosts)
        if not accepted:
            self._fb.hint("选中的条目里没有可更新的（只有「自动更新」的条目会重新解析）。")
            return
        if not woken:
            # 解析服务没在托管（组件被禁用 / 服务已熔断）：请求记下了，但**它不会正在发生**。
            # 此时说「正在更新」就成了一句假话，用户会一直等一个不会有结果的进度。
            self._fb.warn(
                f"已登记 {accepted} 条，但解析服务当前没在运行；启用本组件后才会处理。")
            return
        self._refresh_seq += 1
        self._refresh_hosts = hosts
        self._refresh_tries = 0
        self._fb.hint(f"正在重新解析 {self._name_list(hosts)}…")
        self._poll_refresh(self._refresh_seq)

    def _poll_refresh(self, seq: int) -> None:
        """看一眼这几条出结果没有：出齐了报结果，没出齐继续盯（有次数上限）。

        每次只重画那几格（:meth:`_paint_cells`），**不重建表** —— 重建会掀掉选中与滚动
        位置，而用户正盯着这几行；上千条时还是一次全量重排。

        ``seq`` 是这一轮盯梢的代号：用户连按两次「更新」时，旧链读到代号不符就自己停，
        不会两条链叠着刷同几格。
        """
        if seq != self._refresh_seq:
            return
        state = self._status_index()
        self._paint_cells(state)
        waiting = [h for h in self._refresh_hosts if (state.get(h) or {}).get("pending")]
        self._refresh_tries += 1
        if not waiting:
            done = list(self._refresh_hosts)
            self._refresh_hosts = []
            self._fb.info(self._done_text(done, state))
            return
        if self._refresh_tries >= _REFRESH_POLLS:
            self._refresh_hosts = []
            self._fb.warn(
                f"更新还在进行（{self._name_list(waiting)}）——这一屏不再等了；"
                "解析完成后重新打开本窗口即可看到结果。")
            return
        QTimer.singleShot(_REFRESH_POLL_MS, self, lambda: self._poll_refresh(seq))

    def _paint_cells(self, state: dict) -> None:
        """把刚取到的结论刷进对应的那几格（只改文本，不碰表格结构，也不动选中）。"""
        for i, r in enumerate(self._shown):
            if r.source != SRC_MANAGED:
                continue
            info = state.get(r.host)
            if info is None:
                continue
            r.ip = info.get("ip") or r.ip
            r.status = info.get("status") or r.status
            r.pending = bool(info.get("pending"))
            self._table.item(i, 2).setText(r.ip)
            self._table.item(i, 6).setText(self._status_text(r))

    @staticmethod
    def _name_list(hosts: list[str]) -> str:
        """域名列表说成人话：最多三个，再多就报条数。"""
        head = "、".join(hosts[:3])
        return head + (f" 等 {len(hosts)} 条" if len(hosts) > 3 else "")

    def _done_text(self, hosts: list[str], state: dict) -> str:
        """收尾那句：**成功与失败分开说**（后者才是用户要动手的）。"""
        bad = [h for h in hosts if (state.get(h) or {}).get("status") in ("stale", "fake")]
        good = [h for h in hosts if h not in bad]
        if bad and not good:
            return f"更新失败：{self._name_list(bad)} 没能解析出 IP（仍沿用上一次的值）。"
        if bad:
            return (f"已更新 {self._name_list(good)}；{self._name_list(bad)} 没能解析成功"
                    "（仍沿用上一次的值）。")
        return f"更新完成：{self._done_list(good, state)}。"

    @staticmethod
    def _done_list(hosts: list[str], state: dict) -> str:
        """最多列三条的「域名 → 生效 IP」（比裸域名更能说明这次更新的结果）。"""
        head = "、".join(
            f"{h} → {(state.get(h) or {}).get('ip') or '—'}" for h in hosts[:3])
        return head + (f" 等 {len(hosts)} 条" if len(hosts) > 3 else "")
