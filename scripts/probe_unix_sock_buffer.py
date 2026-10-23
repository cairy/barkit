#!/usr/bin/env python3
"""probe_unix_sock_buffer —— 量「本机 AF_UNIX 一个帧最多能吃下多少」这把尺子。

⚠️ **它不承担正确性**（2026-09-29 起）。会话两侧的发送不再假定「一次 ``sendall``
发完」：发不完的字节落在 ``protocol.FrameWriter`` 出站队列里，等「可写」再推。所以
「帧比缓冲大」不再会误判成断连 —— 与缓冲多大无关。

那它还量什么
------------
量的是**优化有没有生效**：两侧建连时都 ``setsockopt(SO_SNDBUF, 2 * MAX_FRAME)``，
为的是让多数帧一次就发完、根本不进出站队列。能不能设上去取决于内核，故：

* ``实际容量 >= MAX_FRAME`` ⇒ 优化生效，任何合法帧都走单次写；
* ``实际容量 <  MAX_FRAME`` ⇒ 优化没生效（或内核把它钳回去了）。**送达性不受影响**，
  只是大帧要分次写。这时**不要**去加配置键、把量出来的值存起来 —— 那是派生量，
  本仓口径是不落盘（见 ``docs/plans/2026-09-29-通信通道换成会话.md`` §11.1）。

参考量（为什么会走到这一步）：macOS 的 AF_UNIX **名义**默认发送缓冲只有 **8 KiB**
（Linux 是 ~208 KiB），而 hosts 意图正文是**渲染好的整块文本**
（``resolve.render_block``：每行 ``ip\\thost\\n``，上千条就是几十到几百 KiB）。

**但名义值不是答案**：Windows loopback 名义 64 KiB，实测却能灌进 2.75 MiB。所以本脚本
量的是**实际在途容量**（灌到 ``EWOULDBLOCK`` 为止），不是 ``getsockopt`` 的读数。

只读、只在本进程内建 ``socketpair``，不碰任何项目文件（唯一例外：步骤 5 只读地数一眼
用户真实条目数，用来给「最大帧」一个上界）。

用法：
    uv run python scripts/probe_unix_sock_buffer.py
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

#: 与 ``barkit.capabilities.helper.protocol.MAX_FRAME`` 同值。scripts/ 不 import src
#: （否则又要处理 sys.path），故在此重述一次；改协议上限时这里要跟着改。
MAX_FRAME = 1 << 20

#: 另一个参考帧长：一份上千条的 hosts 块大致落在这个量级。
TYPICAL_FRAME = 32 * 1024

#: 试着把在途容量抬到「一个上限帧还装得下」，留一倍余量。
TARGET_SNDBUF = 2 * MAX_FRAME

CHUNK = 64 * 1024


def _pair() -> "tuple[socket.socket, socket.socket]":
    """一对非阻塞 ``socketpair``；对端**故意不读**（模拟「守护正忙一轮任务」）。"""
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    for s in (a, b):
        s.setblocking(False)
    return a, b


def _nominal(sndbuf: "int | None") -> "tuple[int, int]":
    """``getsockopt`` 读数（**名义值**，不代表实际容量）。"""
    a, b = _pair()
    if sndbuf is not None:
        for s in (a, b):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, sndbuf)
    return (a.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF),
            b.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF))


def _capacity(sndbuf: "int | None") -> int:
    """**实际在途容量**：对端不读，灌 ``send()`` 直到 ``EWOULDBLOCK``，返回累计字节。"""
    a, b = _pair()
    if sndbuf is not None:
        for s in (a, b):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, sndbuf)
    total = 0
    while True:
        try:
            n = a.send(b"x" * CHUNK)
        except BlockingIOError:
            break
        except OSError:                          # noqa: BLE001 —— 报出来即可
            break
        if n == 0:
            break
        total += n
    return total


def _sendall_peer_stuck(sndbuf: "int | None", total: int) -> str:
    """对端不读时，``sendall`` 一整帧：抛出还是发完？

    **不再是正确性判据**（分次写已经兜住 —— 抛出也照送不误），它只回答
    「这一帧会不会落进出站队列」。
    """
    a, b = _pair()
    if sndbuf is not None:
        for s in (a, b):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, sndbuf)
    try:
        a.sendall(b"y" * total)
    except BlockingIOError as exc:
        return f"抛 BlockingIOError（装不下）：{exc}"
    except OSError as exc:                       # noqa: BLE001
        return f"抛 OSError：{exc!r}"
    return "整帧一次性发出（装得下）"


def _real_entry_bound() -> "tuple[int, int] | None":
    """只读地数一眼用户真实条目，给「最大帧」一个上界。返回 ``(条数, 字节上界)``。

    上界口径故意取宽：每行按 50 字节算（IPv6 地址最长 45 字符 + 制表符 + 域名 +
    换行 + JSON 转义）。真实值只会更小。
    """
    path = Path.home() / "Library/Application Support/BarKit/hosts_entries.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return None
    return len(entries), len(entries) * 50


def main() -> int:
    if not hasattr(socket, "AF_UNIX"):
        print("本机 Python 没有 socket.AF_UNIX（Windows 上常见）。")
        print("这条尺子只能在 macOS 上量 —— 请在真机执行同一个脚本。")
        return 0

    print(f"MAX_FRAME = {MAX_FRAME}；参考帧 {TYPICAL_FRAME}；目标 SO_SNDBUF = {TARGET_SNDBUF}")
    print()

    n_snd, n_rcv = _nominal(None)
    cap_default = _capacity(None)
    print(f"1) 默认：名义 SO_SNDBUF/SO_RCVBUF = {n_snd}/{n_rcv}")
    print(f"   实际在途容量 = {cap_default} 字节（以这个为准）")
    print()

    n2_snd, _ = _nominal(TARGET_SNDBUF)
    cap_raised = _capacity(TARGET_SNDBUF)
    print(f"2) 设 SO_SNDBUF={TARGET_SNDBUF} 后：名义回读 = {n2_snd}")
    print(f"   实际在途容量 = {cap_raised} 字节")
    print()

    print("3) 参考：对端不读时 sendall 一整帧（现在它只决定「一次写完 / 进队列」）")
    for label, size in (("参考帧 32KiB", TYPICAL_FRAME), ("上限帧 1MiB", MAX_FRAME)):
        print(f"   {label}  默认缓冲：{_sendall_peer_stuck(None, size)}")
        print(f"   {label}  调过缓冲：{_sendall_peer_stuck(TARGET_SNDBUF, size)}")
    print()

    print("4) 判读")
    if cap_default >= MAX_FRAME:
        print("   默认容量已能装下一个上限帧 ⇒ 优化本来就是多余的，任何帧都一次写完。")
    else:
        print(f"   默认容量 {cap_default} < 上限帧 {MAX_FRAME}："
              f"默认值下{'连参考帧 32KiB 都一次写不完' if cap_default < TYPICAL_FRAME else '大帧要分次写'}。")
        if cap_raised >= MAX_FRAME:
            print("   调到 2MiB 后能装下上限帧 ⇒ 优化生效：多数帧一次发完、不进队列。")
        else:
            print("   调缓冲仍装不下 ⇒ 优化无效，帧一律走分次写（**送达性不受影响**，"
                  "别为此加配置键或把值存起来）。")
    print()

    bound = _real_entry_bound()
    if bound is None:
        print("5) 没读到用户 hosts_entries.json（没装过 / 没有条目），跳过真实帧上界。")
    else:
        count, upper = bound
        print(f"5) 真实条目 {count} 条 -> 渲染块上界约 {upper} 字节"
              f"（默认容量的 {upper / max(1, cap_default):.1f} 倍）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
