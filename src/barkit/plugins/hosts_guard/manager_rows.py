"""**一行展示数据**（`Row`）：托管条目与外部条目在这一张表里是同一种东西。

`pending` 与 `status` 并存：前者是「正在重算」，后者是「上次算出来是什么」—— 两者可以同时
成立，而用户按下「更新」之后要知道的是「我这次按有没有在动」。

``manager_view.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations


class Row:
    """一行展示数据（托管条目或外部条目）。"""

    __slots__ = ("source", "host", "ip", "category", "auto", "method", "status", "system",
                 "pending")

    def __init__(self, source, host, ip, category="", auto=False, method="", status="",
                 system=False, pending=False):
        self.source = source
        self.host = host
        self.ip = ip
        self.category = category
        self.auto = auto
        self.method = method
        self.status = status
        self.system = system
        #: 这一条正等着「手动更新」出结果（``status_index`` 的 ``pending``）。
        #: 与 ``status`` 并存：前者是「正在重算」，后者是「上次算出来是什么」。
        self.pending = pending
