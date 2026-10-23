"""bridge 这一族的**常量与异常**：usbmuxd 协议位、中继与探针的节律、链路取值闭集。

⚠ 与插件自己的 ``constants.py`` 是两回事 —— 那边是**组件**的状态取值与两条阈值
（面板那一行用的），这边是**桥接**的参数。两边都叫「常量」，别合并。

``bridge.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import logging
import struct


#: bridge 这一族共用一个日志名（`logging_setup` 的 `%(name)s` 会写进日志文件）。
#: 写死**入口模块名**而不是 `__name__`：兄弟模块各自的 `__name__` 会把日志外观改掉。
log = logging.getLogger("barkit.plugins.ipad_tunnel.bridge")


USBMUXD_SOCKET = "/var/run/usbmuxd"   # macOS 系统 usbmuxd 的 Unix 域套接字

# usbmuxd plist 协议帧头：length(含头总长), version, 帧类型, tag —— 小端 4×int32
# 帧类型永远是 8（"这是一条 plist 消息"），真正语义写在 plist 正文的 MessageType。
_HEADER = struct.Struct("<iiii")
_VERSION = 1
_MSG_PLIST = 8

RECV_SIZE = 64 * 1024        # 单次 recv 的块大小
BUF_LIMIT = 256 * 1024       # 单方向积压上限；达到后停止从对端读取（背压）
SOCK_BUFSIZE = 256 * 1024    # 内核收发缓冲区建议值
CONNECT_TIMEOUT = 5.0        # 建立 usbmuxd 隧道的超时（秒）

DEFAULT_HOST = "127.0.0.1"
DEFAULT_LOCAL_PORT = 1080
DEFAULT_DEVICE_PORT = 9001
RELAY_SELECT_TIMEOUT = 1.0   # 中继事件循环单轮 select 超时（也是 stop() 的退出上界）
RELAY_RETRY_DELAY = 1.0      # 监听失败 / 事件循环异常后的重试间隔
PROBE_INTERVAL = 3.0         # 链路探针节律（秒）；迟滞 2 票 ⇒ 首翻约 6s（与迁移前一致）
PROBE_FLIP_VOTES = 2

# ── 中继接入（开设备通道）的背压与截止（2026-09-20） ──────────────────────────
PENDING_LIMIT = 24           # 在途 Connect 上限；满了就停 accept（背压交给内核 backlog）
MUX_CONNECT_TIMEOUT = 1.0    # 向 usbmuxd 发出 Connect 请求本身的超时（本地 unix，应即时）
PENDING_TIMEOUT = 2.0        # 单个连接等 usbmuxd 应答的截止（旧实现是 5s × 2 次重试）
RESOLVE_RETRY_INTERVAL = 5.0  # 重建设备解析（ListDevices）的最小间隔：防失败风暴里放大请求
HANDSHAKE_TIMEOUT = 1.5      # 探针 SOCKS5 握手应答的等待上限（秒）

# ── 数据面自报健康（会话统计窗口） ────────────────────────────────────────────
DATA_WINDOW = 20.0           # 统计窗口长度（秒）
DATA_WINDOW_MIN_SESSIONS = 6  # 窗口内至少这么多条尝试才算「有结论」，否则不计数（防误判）
DATA_BAD_WINDOWS = 2         # 连续这么多个「零成功」窗口 ⇒ 判数据面不通

# ── 探针判据的冷却（帧数 × PROBE_INTERVAL） ──────────────────────────────────
PROBE_COOLDOWN_POLLS = 4         # 「设备消失」拆除后 ≈12s 内不自动重开（USB 抖动不反复拆建）
PROBE_DOWN_COOLDOWN_POLLS = 40   # 「数据面不通」拆除后 ≈120s 内不自动重开（掐死自激回路）

# ── iOS 端签名到期通告（2026-10-09；机制见同目录 announce.py 与 design.md §17） ──
ANNOUNCE_PORT = 9002             # iPad 端「连接即倾倒」应答点（只绑设备回环，与 9001 同暴露面）
ANNOUNCE_READ_TIMEOUT_S = 5.0    # 读到 EOF 的总超时（应答几百字节 + 立即 close，正常毫秒级）
#: 拉不到通告时的重试间隔（秒）。**拉不到 ≠ 永远拉不到**（2026-10-10 现场：iPad 端应答点
#: 死掉后 Mac 侧只在启动那一次试过，整个会话停在「未通告」）⇒ 链路还在就每隔这么久再问一次，
#: 拿到即停。取值与 ``PROBE_DEEP_INTERVAL`` 对齐：运行期「多久才许碰一次 USB 通道」同理。
ANNOUNCE_RETRY_INTERVAL_S = 60.0

# ── 阶梯下探（2026-09-23；成本从低到高，通过就停） ────────────────────────────
PROBE_DEEP_INTERVAL = 60.0   # ③④ 两级的最小间隔（秒）；② 设备那级仍按 PROBE_INTERVAL
PROBE_DESCEND_PAUSE = 8.0    # 下探 ③④ 前让中继停 accept 的时长（秒）—— 覆盖最坏一次下探
                             # （开通道 5s ＋ 握手 1.5s）；正常时下探是毫秒级，早退早恢复

#: 阶梯的**层号**。探针从最便宜的一级开始，**通过就停**；不通过才下探一级，最后把
#: 「最浅的失败层」报给面板 —— 用户据此知道该去动哪一段，而不是只知道「不通」。
HOP_DATA = "data"        # ① 数据面：中继的字节统计（零成本，天然覆盖端到端）
HOP_DEVICE = "device"    # ② USB 设备在不在（usbmuxd ListDevices，纯本地）
HOP_CHANNEL = "channel"  # ③ iPad:device_port 的通道能不能打开（usbmuxd Connect）
HOP_SOCKS = "socks"      # ④ iPad 端 SOCKS5 引擎是否应答
HOP_UNKNOWN = "unknown"  # 这一轮量不出来（通道忙）—— 既不算通过、也不算失败

# ── usbmuxd Connect 应答的结果码（2026-09-23 本机实测，不是照抄文档） ──────────
#: 同一个「Connect 失败」里藏着两种完全不同的故障，混着报会把用户指错方向。
RESULT_BADDEV = 2        # 不认识这个设备号（实测：伪 DeviceID 9999 / 0 / 42 都回它）
                         # ⇒ 设备重新枚举过，缓存该刷新
RESULT_CONNREFUSED = 3   # 设备端拒绝（实测：往 iPad 上没在听的 9999 端口连）⇒ 那一跳没在听

#: 链路测量结果的**种类**。种类决定这次落在阶梯的哪一层（``_HOP_BY_KIND``）。
LINK_OK = "ok"                        # 全过（内部值，不对外）
LINK_NO_DEVICE = "no_device"          # usbmuxd 的**空设备列表** ⇒ 真的拔了 / 没解锁
LINK_MUX_BUSY = "mux_busy"            # usbmuxd 请求本身超时/失败 ⇒ 通道忙，**量不出来**
LINK_PORT_REFUSED = "port_refused"    # 结果码 3 ⇒ iPad 那一跳确实没在听
LINK_DEVICE_CHANGED = "device_changed"  # 结果码 2 ⇒ 设备号已失效（设备重新枚举过）
LINK_ENGINE_SILENT = "engine_silent"  # 端口可连但 SOCKS5 握手无应答 ⇒ 引擎卡住

#: 种类 → 阶梯层号。``LINK_MUX_BUSY`` 落在 ``HOP_UNKNOWN``：那是「这一轮量不出来」，
#: **既不能当通过、也不能当失败** —— 旧版把运行期一切非「设备消失」都当通过（等于默认
#: 健康），于是「量不出来」被静默读成了「没问题」。
_HOP_BY_KIND: dict[str, str | None] = {
    LINK_OK: None,
    LINK_NO_DEVICE: HOP_DEVICE,
    LINK_DEVICE_CHANGED: HOP_DEVICE,
    LINK_PORT_REFUSED: HOP_CHANNEL,
    LINK_ENGINE_SILENT: HOP_SOCKS,
    LINK_MUX_BUSY: HOP_UNKNOWN,
}


class MuxError(RuntimeError):
    """与 usbmuxd 的交互本身失败（请求超时 / 协议错 / 连接断）。

    与「设备列表为空」严格区分：前者是**通道**的问题，不能当成链路故障拆隧道。
    """


class DeviceNotFound(RuntimeError):
    """usbmuxd 应答正常，但设备列表里没有目标设备（真拔线 / 没解锁 / 没信任）。"""


class ConnectRefused(RuntimeError):
    """usbmuxd 明确回「设备端拒绝连接」（结果码 ``RESULT_CONNREFUSED``）⇒ 那一跳没在听。

    与 ``DeviceUnknown`` 分开是必须的：两者都表现为「Connect 失败」，但该报的段位、该做的
    处置完全不同 —— 一个要用户去 iPad 上看服务有没有跑起来，一个是本机缓存的设备号过期
    （该刷新缓存）。2026-09-22 的现场就是后者被笼统地念成「端口被拒」，把人指错了方向。
    """


class DeviceUnknown(RuntimeError):
    """usbmuxd 明确回「不认识这个设备号」（结果码 ``RESULT_BADDEV``）⇒ 设备重新枚举过。"""


#: ``parse_connect_reply`` 的三态之一：应答还没收全，继续等。
_PENDING = object()
