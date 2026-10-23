"""本组件的模块级常量：日志名、出厂默认场景、四张取值闭集表（决议 D13）。

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
log = logging.getLogger("barkit.plugins.route_guard.plugin")


#: 发货默认场景（§9.5 单一真源，2026-09-20 从 ``config.default.yaml`` 迁来）。
#: **列表顺序即优先级**（第 1 个命中者胜），所以这两条的次序是有意义的发货行为，
#: 不是随手排的示例 —— ``test_shipped_default_profiles_compile_like_the_old_default_rules``
#: 逐项守它编译出的规则与旧 ``rules[]`` 默认一致。
#: 用户一旦自己写 ``profiles``，本列表在 ``deep_merge`` 里被**整体覆盖**（列表语义），
#: 两条示例整体消失 —— 与搬迁前一致。
_SHIPPED_DEFAULT_PROFILES: list = [
    {
        "name": "office",
        "ifaces": ["en5"],
        "match": {"gateway": "10.99.1.254"},
        "net": {"mode": "dhcp"},
        "routes": {
            "enabled": True,
            "items": [
                {"destination": "10.99.2.0/24", "via": "10.99.1.254"},
                {"destination": "10.2.0.0/16", "via": "10.99.1.254"},
                {"destination": "192.168.100.5/32", "via": "10.99.1.254"},
            ],
        },
    },
    {
        "name": "home",
        "ifaces": ["en6"],
        "match": {"gateway": "192.168.2.1"},
        "net": {"mode": "dhcp"},
        "routes": {
            "enabled": True,
            "items": [
                {"destination": "172.16.0.0/16", "via": "192.168.2.1"},
            ],
        },
    },
]

#: 取值闭集（决议 D13）：取值名 / 级别 / 默认文案只在**这里**写一次，
#: 运行期只报取值名，框架回查本表取级别与图标（设计 §3.1 的三台机）。
#: 首格 ``unknown`` 是**首屏起点**（``initial``）：决议把 initial 从「读现场的工厂」
#: 改成「从哪一格开始」之后，取值集里必须有这一格 —— 否则首屏只能拿某个具体读数
#: 冒充「还不知道」，那是一句假话（同 ``notify_guard`` 的 ``frozen`` 取值）。
#: 取值图标一律留空 —— 路由没有「同一台机、按取值可区分」的领域变体，
#: 留空即回落状态机主图标 SHIELD（决议 D13：该字段允许缺省；领域图标见 §5.3-③，
#: 是尚未落地的独立项）。
#:
#: ⚠️ 设计 §3.1 给 ``daemon.unknown`` 定的级别是 WARN。实装取 IDLE，理由是**它没有
#: 触发路径**：``helper_client.status()`` 的四个键由文件检查算出、恒在（装没装都答得出），
#: 于是这一格实际只当**首屏起点**用；按 WARN 画会让启动瞬间闪一下橙（首屏起点由
#: ``AppShell._paint_initial_states`` 立刻上屏，真读数要等首轮 tick）。宁可起点安静，
#: 不要一句没有来源的告警。
DAEMON_STATES: tuple[StateOption, ...] = (
    StateOption("unknown", StateLevel.IDLE, text="尚未读取"),
    StateOption("ready", StateLevel.OK, text="守护就绪"),
    StateOption("missing", StateLevel.ERR, text="守护未安装"),
    StateOption("outdated", StateLevel.ERR, text="守护版本过期"),
    # 停用时的降级格（设计 §3.2「不撒谎」）：压灰，不报错 —— 用户自己关的自动化，
    # 守护装没装都不再重要。
    StateOption("not_needed", StateLevel.IDLE, text="停用中，不检查守护"),
)

#: 路由结果（helper 回显的唯一解释）。``frozen`` = 停用时**保持现状**：
#: 网卡与路由都不碰，已叠加的照着留着 —— 报 ``none``（未叠加）就是粉饰（§3.2）。
ROUTES_STATES: tuple[StateOption, ...] = (
    StateOption("unknown", StateLevel.IDLE, text="尚未读取"),
    StateOption("applied", StateLevel.OK, text="已叠加"),
    # ``partial`` 用 ``ROUTE_WARN``：它是「叠了但没叠全」，与主图标同形的话那一格
    # 看上去和「已叠加」一样 —— 只有颜色不同是不够的（色弱 / 深色下都吃不准）。
    StateOption("partial", StateLevel.WARN, text="部分叠加", icon=IconName.ROUTE_WARN),
    StateOption("pending", StateLevel.IDLE, text="待应用"),
    StateOption("none", StateLevel.IDLE, text="未叠加"),
    StateOption("frozen", StateLevel.IDLE, text="已冻结（保持现状）"),
    StateOption("failed", StateLevel.ERR, text="路由应用失败"),
    # 「留空的下一跳，这块网卡上还没有网关」（v8）：那条路由**一个都没动** —— 没做错
    # 什么（租约还没到 ⇒ 下一轮自愈），也不该混进「未叠加」（那句话说的是「本来就不该
    # 叠」）。用户要的是「它卡住了，缺的是一个网关」，故自成一格 WARN。
    # 与 ``partial`` 同用 ``ROUTE_WARN``：这里要说的是「有东西没叠上」，不是「没这回事」。
    StateOption("no_gateway", StateLevel.WARN, text="未取到网关，路由未动",
                icon=IconName.ROUTE_WARN),
)

#: 场景命中（设计 §3.1）：取值是**闭集三值**，场景名走 ``summary`` ——
#: 这样避开了「取值集合来自用户数据」的坑，不必为它新造契约概念。
#: ``conflict`` 不是凑数：两个场景都监控同一张网卡、网段重叠时，用户才看得见
#: 「为什么生效的是 office 而不是 home」。
PROFILE_STATES: tuple[StateOption, ...] = (
    StateOption("unknown", StateLevel.IDLE, text="尚未读取"),
    StateOption("active", StateLevel.OK, text="命中"),
    StateOption("none", StateLevel.IDLE, text="无匹配"),
    # 同 ``routes.partial``：命中了但不止一个 —— 与 ``active`` 同形的话用户看不出
    # 「为什么生效的是 office 而不是 home」这件事正在发生。
    StateOption("conflict", StateLevel.WARN, text="多场景同时命中",
                icon=IconName.ROUTE_WARN),
)

#: 网卡配置态（设计 §3.1 的第四台机；**第 8 步随 helper 的网卡能力一起补**，
#: 第 6 步时按「宁缺勿假」有意不声明）。数据源只有一处：helper 回显里的 ``net`` 段
#: ——「网卡被改成什么样」只有真动手的那一方知道（意图可设、结果只读，§4.7）。
#:
#: ⚠️ **两处与设计 §3.1 的有意偏离**（口径真源就在这，别去别处找）：
#: ① 多一格 ``unknown``（设计那五格没写它）—— 首屏要有起点，否则只能拿某个具体读数
#:    冒充「还不知道」，那是一句假话（同 ``daemon.unknown`` 的判据）。
#: ② ``dhcp`` 的文案改「跟随系统，未接管」（设计写的是「DHCP 获取中」）—— 没接管时我们
#:    **不读**那块网卡的模式，说它正在 DHCP 是猜的；用户自己在系统设置里配的静态地址
#:    也会被这句话说成 DHCP。
NET_STATES: tuple[StateOption, ...] = (
    StateOption("unknown", StateLevel.IDLE, text="尚未读取"),
    StateOption("static", StateLevel.OK, text="静态配置已生效"),
    StateOption("pinned", StateLevel.OK, text="已按租约固化"),
    StateOption("dhcp", StateLevel.IDLE, text="跟随系统，未接管"),
    StateOption("frozen", StateLevel.IDLE, text="已冻结（网卡保持现状）"),
    StateOption("failed", StateLevel.ERR, text="网卡配置失败"),
)
