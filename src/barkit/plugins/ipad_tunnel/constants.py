"""本组件的模块级常量：日志名、取值闭集（决议 D13）与两条阈值（守护回报过期 / 本组件失联）。

``plugin.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import logging

from barkit.contribution.specs import (
    IconName,
    StateLevel,
    StateOption,
)


#: 本组件全族共用的日志名（`logging_setup` 的 `%(name)s` 会写进日志文件）。
#: 写死**入口模块名**而不是 `__name__`：兄弟模块各自的 `__name__` 会把日志外观改掉。
log = logging.getLogger("barkit.plugins.ipad_tunnel.plugin")


#: 本组件在会话里的**领域名**（订阅事实用它，声明意图也用它）。
TUNNEL_SUBJECT = "tunnel"


#: 取值闭集（决议 D13）：取值名 / 级别 / 默认文案只在**这里**写一次，
#: 报告只报取值名，框架回查本表取级别与图标。
#: 首格 ``unknown`` 是**首屏起点**（``initial``）：决议把 initial 从「读现场的工厂」
#: 改成「从哪一格开始」之后，取值集里必须有这一格。取值图标绝大多数留空、回落主图标
#: TABLET；三枚**严重**取值（``error`` / ``data_plane_dead`` / ``ipad_port_refused``）
#: 挂了领域变体 ``TABLET_WARN``（``error`` 2026-09-19；另两枚 2026-09-23 加）。
#:
#: **为什么只有这三枚**：取值图标今天只在一个地方真的画出来 —— 标题栏那格（24px）。
#: 列表行角标与托盘角标走的是 12px 角标字形，`TABLET_WARN` 没有角标版，
#: `badge_name()` 会按级别回落到 `MARK_ERR`（比 12px 画平板＋叹号清楚得多），
#: 所以这三枚的可见效果被限制在「出严重问题时标题栏那格从平板变成带叹号的平板」。
#: 其余取值挂上去只是给同一张平板图换名字，按 D19「不预先堆砌」不挂。
#:
#: **中间那三格是「阶梯结论」**（2026-09-23）：探针改成逐级回退（数据面 → 设备 → 通道
#: → 握手）之后，它给出的不再是「通 / 不通」两态，而是**最浅的失败层**；面板照它点名
#: 「卡在哪一段」，用户才知道该去动哪一头（改之前只能从「等待 iPad」和「隧道生效中」
#: 里二选一，两句话在故障期都是错的）。文案统一成「〈已证实的部分〉 · 〈卡点〉」，
#: 且**每一句都要能在代码里指出凭什么成立**（用户两次退回「空转」「卡在 iPad 端」，
#: 就是凭据不足）。ERR 级按 D17 默认进托盘角标（用户裁定「看角标、不弹系统通知」）。
STATES: tuple[StateOption, ...] = (
    StateOption("unknown", StateLevel.IDLE, text="尚未读取"),
    StateOption("no_daemon", StateLevel.IDLE, text="守护未装"),
    StateOption("error", StateLevel.ERR, icon=IconName.TABLET_WARN, text="隧道异常"),
    # ⑤ 「守护不回报」自成一格（2026-09-29）：状态文件里的 ``checked_at`` 是守护**写下
    # 它那一刻**，它过期就说明这一整份结论不是此刻的事实 —— 那时再照念里面的
    # ``active`` / ``pending`` 都是撒谎（用户看到的正是「面板说隧道生效中、其实早断了」）。
    # 级别取 ERR：此刻用户可能正因为隧道断了上不了网，却盯着一行绿字。秒数由运行期给。
    StateOption(
        "daemon_stale", StateLevel.ERR, icon=IconName.TABLET_WARN,
        text="守护未回报 · 隧道状态已过期",
    ),
    # ① 数据面搬不动字节，而 ②③④ 刚实测全过 ⇒ 卡点在**本机这一侧**（中继）。说得出
    # 「本机」是因为探针刚拿现问的设备号开通过通道、也拿到过 SOCKS5 应答（见 bridge 的
    # 阶梯）；说「数据卡在本机」而不说「没网」，是因为此刻 helper 那边报的还是 active。
    # 不与 ``error`` 合并：那一格是「守护 / 隧道本身报错」，要做的处置完全不同。
    StateOption(
        "data_plane_dead", StateLevel.ERR, icon=IconName.TABLET_WARN,
        text="隧道已连 · 数据卡在本机",
    ),
    # ③ 通道打不开：usbmuxd 明确回了「设备端拒绝」（本机实测的结果码 3）⇒ iPad 那一跳
    # 没在听。这一格是 2026-09-22 那次排查里缺失的那块拼图 —— 当时这种情形被笼统念成
    # 「等待 iPad」，而 iPad 其实插得好好的，用户照着「等待」什么都等不来。
    StateOption(
        "ipad_port_refused", StateLevel.ERR, icon=IconName.TABLET_WARN,
        text="iPad 端口连不上 · SOCKS 服务没在监听",
    ),
    StateOption("dns_degraded", StateLevel.WARN, text="隧道已生效 · DNS 未接管"),
    # 读不出「网络服务名」（networksetup 的服务名是本地化的，反查所用的设备名也可能拿不到）
    # ⇒ helper 一根手指都没碰系统 DNS / IPv6（fail-closed，宁可不接管也不写到猜来的服务上）。
    # 与 dns_degraded 分列：那一格的前提是「碰过、但没被采纳」（要查网络），这一格是
    # 「一次都没碰」（要去查为什么读不出服务名），两件事该做的动作不同。
    StateOption(
        "service_unknown", StateLevel.WARN,
        text="隧道已生效 · 未改动系统网络（读不出网络服务名）",
    ),
    # 「正在建」自成一格（2026-09-29）：helper 有一个明确的中间态 —— 上游已在听、hev
    # 已起、utun 还没建出来（最多等 20 秒）。此前这一格**没被读**，于是它被算进下一格
    # 「等待 iPad · 链路就绪后自动建立隧道」；而那一刻 iPad 插着、链路也好，那句话是错的
    # （用户 2026-09-29 报的「先正常→等 iPad→再正常」里，中间那一段就是它）。
    StateOption("building", StateLevel.IDLE, text="隧道建立中 · 正在等隧道接口建出"),
    StateOption("active", StateLevel.OK, text="隧道生效中"),
    StateOption("manual_waiting", StateLevel.IDLE, text="手动等待上游 · 上游未就绪"),
    # ④ 端口通、SOCKS5 不应答：引擎卡住（2026-09-20 真机就是这一格）。
    StateOption("engine_silent", StateLevel.WARN, text="iPad 未应答 · SOCKS 服务没在干活"),
    # ② 设备不在（usbmuxd 的空设备列表）⇒ 这是**唯一**该说「等待 iPad」的情形。
    StateOption("waiting_ipad", StateLevel.IDLE, text="等待 iPad · 链路就绪后自动建立隧道"),
    StateOption("ipad_ready", StateLevel.IDLE, text="iPad 已连 · 隧道未启用"),
    StateOption("off", StateLevel.IDLE, text="隧道未启用（开关为关）或 iPad 未连接"),
)


#: 「守护这份回报过期了」的判据（秒）：状态文件里的 ``checked_at`` 比此刻旧过这么久，
#: 面板就说「守护未回报」，而不是接着念里面的 ``active`` / ``pending``。取 180 的依据：
#: helper 侧 ``state._write`` 的去重是「内容不变也最多静默 ``REFRESH_AFTER_S``（120 秒）
#: 必刷一次」⇒ 守护活着时这个时间戳最多旧 120 秒，180 = 它 + 一个轮询周期余量。
DAEMON_REPORT_MAX_AGE_S = 180.0


#: 本组件**自己**的失联阈值（秒），交给 ``StateSpec.stale_after``。⚠️ 与上面那条**不是
#: 一件事**：这条判「本组件还在不在上报」（调度器 / worker 死了 ⇒ 整格改判「状态失联」），
#: 上面那条判「守护写下的话是不是此刻的」。组件节拍 30 秒 ⇒ 90 = 三个周期。
SELF_STALE_AFTER_S = 90.0
