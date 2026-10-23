"""隧道那条链路的**常量与跨轮状态**：契约版本 ``TUNNEL_WIRE_VERSION``、
``_TUNNEL_*`` 那一组跨轮记忆（进程 / utun 名 / 服务名 / 劫持标记），以及轮询与 TTL。

``_TUNNEL_*`` 是**本进程内的跨轮账**，不是文件 —— 「隧道此刻在不在」只能靠这一份，
一旦分散到两个模块，两边各记一次就会各说各话。

``tunnelops.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations


# ──────────────────────────────────────────────
# iPad 全流量隧道（复用同一 root helper，schema 锚点 v2.0）
# 设计见 plugins/ipad_tunnel/design.md
# 纯标准库实现，不依赖 barkit；app 侧只声明隧道意图，本函数消费。
# ──────────────────────────────────────────────

TUNNEL_WIFI_KEYS = ("on_activate", "on_deactivate")

#: 隧道**意图**的契约版本（**与 app 侧 ``tunnel_spec.TUNNEL_WIRE_VERSION`` 同一份**，
#: 两边各写一次 —— 不同进程、不同包，没法共享常量；改一处必须改两处）。
#: 1 → 2（2026-09-29）：**去掉 ``link_up`` / ``ignore_link``**，并把它变成真锁
#: （此前只进字段白名单、从不拿它比）。
#:
#: 为什么去掉那两位：它们是**运行期事实**（iPad 那条链路此刻通不通），而这份文件是
#: **期望态**。事实住在期望态里有两个后果，都在 2026-09-29 那次真机上发生了：
#: ① 写方只在自己状态翻转时落笔（边沿触发）⇒ 文件被第三方改写后**永不纠正**，
#: 界面与守护各说各话十几分钟；② 一份过期的事实足以让 helper 去建一条通往黑洞的隧道。
#: 换成的载体是**可观测的事实本身**：app 的中继只在链路就绪时监听 ``local_port``，
#: helper 探那个端口（``_upstream_ready``）就知道上游在不在 —— 谁都不用相信一份快照。
TUNNEL_WIRE_VERSION = 2

_TUNNEL_ACTIVE = False          # 跨轮次：当前隧道是否已拉起
_TUNNEL_PROC = None             # hev-socks5-tunnel 子进程
_TUNNEL_UTUN = None             # 隧道接口名（如 utun9）
_TUNNEL_IPV6_OFF = False        # 是否曾为隧道禁用 Wi-Fi 的 IPv6
_TUNNEL_WIFI_IFACE = None       # 缓存的主网络接口（en0）；激活期不再重解析
_TUNNEL_SERVICE = None          # 缓存的 networksetup 服务名（本地化，不能写死 "Wi-Fi"）
_TUNNEL_ROUTE_HIJACKED = False   # 本轮是否实际加了 /1 路由（受 hijack_route 控制）
_TUNNEL_DNS_HIJACKED = False     # 是否**承担**系统 DNS 的还原责任（拆除/关闭时要 _restore_dns）
_TUNNEL_MAPDNS_ROUTE = False     # 本轮是否加了 mapdns 地址（198.18.0.2）的 /32 主机路由
_TUNNEL_PENDING = None           # hev 已起、utun 未建出的中间态：{"at": ts, "data": data}
_SHUTTING_DOWN = False           # 退出清理进行中（防 atexit/信号重入）

TUNNEL_IPV4 = "198.18.0.1"       # hev yml tunnel.ipv4，用于发现 utun
HEV_DNS = "198.18.0.2"           # 隧道内置 DNS 代理（mapdns.address）默认值
UTUN_WAIT_TIMEOUT = 20.0         # 秒，等 hev 建出 utun 的上限（非阻塞，跨轮次累计）
PENDING_POLL = 1                 # 秒，utun 待建期间的轮询间隔（主循环降频用）
TUNNEL_POLL = 5                  # 秒，隧道单独轮询间隔（见 main：状态文件写频率 = 面板刷新延迟）
SERVICE_TTL = 300.0              # 秒，网络服务名缓存有效期
RESOLVER_ADOPT_TIMEOUT = 3.0     # 秒，等「改完 DNS → resolver 可见」的传播预算（见 _resolver_adopted_dns）
