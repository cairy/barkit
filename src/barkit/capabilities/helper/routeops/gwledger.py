"""gwledger：网卡 → 网关那本账的**独立落盘**（跨进程、跨快照版本）。

它替掉什么
----------
这本账以前寄生在 ``state`` 的 route 观测里（``net_gateways`` 那一格）。那是**每轮
整体替换**的一份块：任何一条路径产出的 route 观测只要漏带这一格，``record`` 就把
它抹平；``forget_observed()`` 之后再被别的 subject 带一次 flush 同样抹平；守护一
重启、新进程内存全空时首帧 flush 又抹平一次。2026-10-19 到 10-20 连着三次真机
「场景命中却不叠路由」，根都是这一件事 —— 每次都在补某一条路径「记得把它带下去」，
而路径会一直长出来。

现在改成账归账：**学到的那一刻就写这个文件**，读的时候先读它，与快照里那一格
（只作兼容与就近覆盖）合并。快照被抹、版本不符、进程重启，都不再影响这本账。

形状
----
``{iface: {"gateway": <ipv4>, "net": <那份网卡配置>}}`` —— 与 ``converge._iface_gateways``
完全一致。**合并的口径只有那一处**（要求「当时那份 net 逐字相同」才认），本模块只负责
读与写这一个 dict，不另立判据。

放在用户域（``data_dir()``）而不是 ``SYS_RUN_DIR``：它与 ``helper_snapshot.json`` 是
同一类数据、同一级可信度（快照里本来就带着这一格），读写都已经是 root 写用户域。
读回来的值一律还要过 ``valid_ipv4`` 与「落在本网卡当前网段内」两道判据才会被用。
"""
from __future__ import annotations

import json

from ..safeio import _atomic_write_json, data_dir, log

#: 文件名（与快照分开：它有自己的生命周期，不该跟着快照的写入节奏走）。
LEDGER_NAME = "iface_gateways.json"

#: 格式版本。读到别的版本按「没有」处理 —— 这本账丢了最多是少一条恢复依据，
#: 而 ``run_cycle`` 后面还有「系统旧路由」「baseline」两级证据可退。
LEDGER_VERSION = 1


def ledger_path():
    return data_dir() / LEDGER_NAME


def load() -> dict:
    """读整本账；缺失 / 半截 / 版本不符一律当「没有」。"""
    try:
        data = json.loads(ledger_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != LEDGER_VERSION:
        return {}
    ifaces = data.get("ifaces")
    if not isinstance(ifaces, dict):
        return {}
    return {str(k): v for k, v in ifaces.items() if isinstance(v, dict)}


def save(known: dict) -> None:
    """把整本账落盘。**内容没变就不写**（它每轮都会被合并一次，写下去的是同一个 dict）。

    写失败只记日志：这本账丢了有后两级证据兜着，不该让一轮周期因此停摆。
    """
    if not isinstance(known, dict) or not known:
        # 空账不写：写一个空文件与「没有这个文件」等价，还多一次目录变更。
        return
    if known == load():
        return
    try:
        _atomic_write_json(ledger_path(),
                           {"version": LEDGER_VERSION, "ifaces": known})
    except OSError as e:
        log(f"写网卡网关账失败（不影响本轮）：{e}", key="route.gwledger.write")


__all__ = ["LEDGER_NAME", "LEDGER_VERSION", "ledger_path", "load", "save"]
