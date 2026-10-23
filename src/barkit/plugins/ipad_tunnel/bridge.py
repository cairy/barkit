"""ipad_tunnel/bridge：本机 127.0.0.1:<local> ↔ iPad:<device> 的 USB/usbmuxd TCP 桥。

移植自上游项目 tools/mac_socks_bridge.py（纯标准库 iproxy 等价物，零依赖）。

**批次 5 后段（resident service）**：本模块不再自起线程、不再自带定时器，改为向框架
声明两个长驻服务（``RelayService`` / ``LinkProbeService``），线程 / 时钟 / 起停 / join
全归 ``core/resident.ResidentHost``：

- ``RelayService``（自持循环型）：监听 socket 的开关与重建收进同一循环，端口热改在
  循环内「关旧 → 开新」顺序收敛——没有跨实例 bind 竞争，也没有退避重试。
- ``LinkProbeService``（周期型，interval=3.0）：探测「usbmuxd 有 USB 设备 且 能打通到
  iPad:device_port 的隧道」，就绪翻转带 2 票迟滞后回调 on_link_change —— 结论决定
  **中继要不要占用 ``local_port``**（见 ``RelayService`` 的监听门），而 helper 探那个
  端口就知道上游在不在，进而拉起/拆除全流量隧道（design.md §6）。链路这条事实
  **不再写进 tunnel.json**（v2 契约，2026-09-29）：它是运行期事实，期望态文件里没有它
  的位置 —— 载体是可观测的端口本身。

**2026-09-20 · 判据与探针分离（自激回路修复）**：探针原来在**运行期**也拿「usbmuxd
Connect 超时」当「链路断开」，可它与数据面**抢同一条 USB 通道**——隧道一忙，探针必然
超时 ⇒ 写 link_up=false ⇒ helper 拆机 ⇒ 负载消失、探针立刻恢复 ⇒ 又自动拉起，两分钟一轮、
每轮约 40s 全机断网。现在：

- **运行期**（隧道正在接管流量）只问一件事「USB 设备还在吗」（``LINK_NO_DEVICE`` 才拆），
  不再做 usbmuxd Connect + 握手，零通道占用；
- **准入**（隧道未接管时）才做完整探测（设备 ＋ Connect ＋ SOCKS5 握手应答）；
- 数据面是否健康由**中继自己数**（``Bridge`` 的会话窗口统计，见 ``data_plane_bad_streak``），
  连续窗口零成功才允许在运行期触发拆除，且带长冷却。

**2026-09-23 · 判据改成「逐级回退」的阶梯（成本从低到高，通过就停）**：上一条把完整探测
整条砍掉换来的是「运行期没有任何东西在验端到端」，2026-09-22 那场「隧道建起来了、一个字节
都搬不动」因此完全静默（日志刷屏、面板一动不动）。改成阶梯后两个目标同时成立：

- ① **数据面**（中继的会话窗口统计，``data_plane_state``）—— 零成本，且它数的就是真实
  流量、天然覆盖端到端。**它说搬过字节就到此为止**，绝不因此去碰任何更贵的探测；这就是
  「不与数据面抢通道」的根本保证（不再靠「干脆不测」来实现）。
- ① 没通过（有结论地零字节，或还没结论）才往下：② 设备在不在 → ③ 通道能否打开 →
  ④ SOCKS5 有没有应答。**每一级通过就停**，不白花下一次等待。
- ③④ 要碰 USB 通道，所以：**下探前先让中继停手**（``Bridge.pause_accept``，带截止时间 ——
  探针中途死掉也不会留下一个永久黑洞），且运行期限速 ``PROBE_DEEP_INTERVAL``。
- 结论不再是「通 / 不通」两态，而是**最浅的失败层**（``HOP_*``），面板据此点名卡在哪一段。

顺带把「usbmuxd 说了句 Connect 失败」拆成两种（``RESULT_BADDEV`` / ``RESULT_CONNREFUSED``，
本机实测过）：前者是设备号过期（该刷新缓存），后者才是 iPad 那一跳没在听 —— 混着报会把用户
指错方向，2026-09-22 的现场就是前者被当成「端口被拒」。

数据面（``Bridge`` 的字节流逻辑与 ``main()`` CLI）保持原样：只把外层 ``while True``
拆成 ``serve_once``，好让中继循环每轮都能重读配置。**接入路径不再阻塞事件循环**：开设备
通道改成「发出请求 + 轮询应答 + 截止时间」（``begin_device_tunnel`` / ``parse_connect_reply``），
在途上限 ``PENDING_LIMIT`` 满了就停 accept（背压交给内核 backlog）——旧实现在循环里同步等
5s × 2 次重试，忙时一分钟只服务 6 条连接，正是那一轮「57 条会话全部卡在握手」的放大器。

禁止事项（门禁 ``test_no_private_timer_in_plugins`` 扫描本文件）：
不得 import ``threading`` / 不得 ``time.sleep`` / 不得起定时器。等待一律用
``selectors`` 的空 select（见 ``RelayService._pause``）。

- 桥接本身无 root；纯粹的 usbmuxd TCP 管道，与 route_guard 的特权操作解耦。
- 探测只在 app 侧进行：helper 不直连 usbmuxd，它**只探本机那个中继端口**是否在听
  （``_upstream_ready``）—— 那是唯一需要跨进程传递的事实，而端口本身就是它。

**2026-10-02 拆模块（零行为变更）**：本文件的正文按职责搬进**同包的兄弟模块**
（``consts.py`` / ``mux.py`` / ``bridge_core.py`` / ``relay.py`` / ``probe.py``），
入口只留 ``main()`` 与再导出。**插件包不许子目录**（§2.3 白名单，硬）⇒ 是同目录
平铺，不是包。上面「扫描本文件」那句讲的是**这一族**。
"""
from __future__ import annotations

import argparse
import socket

# --- 再导出：调用面（含本文件的 ``main()``）仍从**入口**取这些名字 ---
# --- （平铺拆包后它们的真身在兄弟模块，这里只是把入口这个门面补齐）---
# `__all__` 是必须的：pyflakes 不认 `# noqa`，但对出现在 `__all__` 里的名字不算
# unused import —— 没有它，这 68 个再导出会变成 68 条静态噪声。
from .bridge_core import Bridge
from .consts import (
    BUF_LIMIT,
    CONNECT_TIMEOUT,
    ConnectRefused,
    DATA_BAD_WINDOWS,
    DATA_WINDOW,
    DATA_WINDOW_MIN_SESSIONS,
    DEFAULT_DEVICE_PORT,
    DEFAULT_HOST,
    DEFAULT_LOCAL_PORT,
    DeviceNotFound,
    DeviceUnknown,
    HANDSHAKE_TIMEOUT,
    HOP_CHANNEL,
    HOP_DATA,
    HOP_DEVICE,
    HOP_SOCKS,
    HOP_UNKNOWN,
    LINK_DEVICE_CHANGED,
    LINK_ENGINE_SILENT,
    LINK_MUX_BUSY,
    LINK_NO_DEVICE,
    LINK_OK,
    LINK_PORT_REFUSED,
    MUX_CONNECT_TIMEOUT,
    MuxError,
    PENDING_LIMIT,
    PENDING_TIMEOUT,
    PROBE_COOLDOWN_POLLS,
    PROBE_DEEP_INTERVAL,
    PROBE_DESCEND_PAUSE,
    PROBE_DOWN_COOLDOWN_POLLS,
    PROBE_FLIP_VOTES,
    PROBE_INTERVAL,
    RECV_SIZE,
    RELAY_RETRY_DELAY,
    RELAY_SELECT_TIMEOUT,
    RESOLVE_RETRY_INTERVAL,
    RESULT_BADDEV,
    RESULT_CONNREFUSED,
    SOCK_BUFSIZE,
    USBMUXD_SOCKET,
    _HEADER,
    _HOP_BY_KIND,
    _MSG_PLIST,
    _PENDING,
    _VERSION,
    log,
)
from .mux import (
    DeviceResolver,
    Tunnel,
    _Pending,
    _connect_error,
    _connect_request,
    _frame,
    _mux_connect,
    _recv_exact,
    _recv_plist,
    _result_code,
    begin_device_tunnel,
    list_devices,
    open_device_tunnel,
    parse_connect_reply,
    pick_device,
    socks5_handshake,
    tune_tcp,
)
from .probe import LinkProbeService
from .relay import RelayService

__all__ = [
    "BUF_LIMIT",
    "Bridge",
    "CONNECT_TIMEOUT",
    "ConnectRefused",
    "DATA_BAD_WINDOWS",
    "DATA_WINDOW",
    "DATA_WINDOW_MIN_SESSIONS",
    "DEFAULT_DEVICE_PORT",
    "DEFAULT_HOST",
    "DEFAULT_LOCAL_PORT",
    "DeviceNotFound",
    "DeviceResolver",
    "DeviceUnknown",
    "HANDSHAKE_TIMEOUT",
    "HOP_CHANNEL",
    "HOP_DATA",
    "HOP_DEVICE",
    "HOP_SOCKS",
    "HOP_UNKNOWN",
    "LINK_DEVICE_CHANGED",
    "LINK_ENGINE_SILENT",
    "LINK_MUX_BUSY",
    "LINK_NO_DEVICE",
    "LINK_OK",
    "LINK_PORT_REFUSED",
    "LinkProbeService",
    "MUX_CONNECT_TIMEOUT",
    "MuxError",
    "PENDING_LIMIT",
    "PENDING_TIMEOUT",
    "PROBE_COOLDOWN_POLLS",
    "PROBE_DEEP_INTERVAL",
    "PROBE_DESCEND_PAUSE",
    "PROBE_DOWN_COOLDOWN_POLLS",
    "PROBE_FLIP_VOTES",
    "PROBE_INTERVAL",
    "RECV_SIZE",
    "RELAY_RETRY_DELAY",
    "RELAY_SELECT_TIMEOUT",
    "RESOLVE_RETRY_INTERVAL",
    "RESULT_BADDEV",
    "RESULT_CONNREFUSED",
    "RelayService",
    "SOCK_BUFSIZE",
    "Tunnel",
    "USBMUXD_SOCKET",
    "_HEADER",
    "_HOP_BY_KIND",
    "_MSG_PLIST",
    "_PENDING",
    "_Pending",
    "_VERSION",
    "_connect_error",
    "_connect_request",
    "_frame",
    "_mux_connect",
    "_recv_exact",
    "_recv_plist",
    "_result_code",
    "begin_device_tunnel",
    "list_devices",
    "log",
    "main",
    "open_device_tunnel",
    "parse_connect_reply",
    "pick_device",
    "socks5_handshake",
    "tune_tcp",
]


def main():
    ap = argparse.ArgumentParser(
        description="经 USB/usbmuxd 把本机端口桥接到 iOS 设备端口（纯标准库 iproxy 等价物）")
    ap.add_argument("--local", type=int, default=1080, help="本机监听端口（默认 1080）")
    ap.add_argument("--device", type=int, default=9001, help="设备侧端口（默认 9001，即 hev）")
    ap.add_argument("--host", default="127.0.0.1", help="本机绑定地址（默认仅回环 127.0.0.1）")
    ap.add_argument("--udid", default=None, help="指定设备 UDID（多设备同时连接时必填）")
    ap.add_argument("--verbose", action="store_true", help="打印每条连接与统计信息")
    args = ap.parse_args()
    device_id, serial = pick_device(args.udid)
    print(f"[bridge] 选定设备 DeviceID={device_id} Serial={serial} -> 设备端口 :{args.device}")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    listener.bind((args.host, args.local))
    listener.listen(128)
    listener.setblocking(False)
    print(f"[bridge] 监听 {args.host}:{args.local}，等待连接（Ctrl-C 退出）...")
    bridge = Bridge(DeviceResolver(args.udid), args.device, verbose=args.verbose)
    try:
        bridge.serve_forever(listener)
    except KeyboardInterrupt:
        print("\n[bridge] 收到中断，退出")
    finally:
        listener.close()


if __name__ == "__main__":
    main()
