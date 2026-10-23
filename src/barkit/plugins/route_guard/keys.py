"""路由配置里**会写进正文的那一批字面量**：helper 节律的三条阈值、``net.mode`` 与
``fallback`` 两个闭集、各层允许出现的键集合（``frozenset``）、以及「不认识的键」那句
报错的尾巴 ``_UNKNOWN_TAIL``。

为什么要单独一层：闭集与阈值**只写一份**（两处各写一遍数字，迟早在「界面能设成什么」
与「配置收什么」之间裂开）；键集合是**严格校验**的依据（拼错的键宁可当场报错，也不要
被静默忽略 —— 静默忽略的症状是「写了不生效」而没有任何提示）。

本模块**零依赖**（不 import 任何同包兄弟），是依赖图的根。

``rules.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations


#: 路由意图里那份 **helper 节律**（秒）：root helper 据此决定「多久巡逻一轮」。
#: 它不是框架的轮询节拍（那是 ``suggested_poll``，配置键 ``poll``）——同一个配置键只能
#: 有一个消费者（决议 D27），故本组件的节律读 ``helper_poll``。
#: 线格式字段名仍是 ``poll``（root helper 已装在机器上，改它等于要重装）。
DEFAULT_HELPER_POLL = 30
#: 取值域（秒）——**校验与设置页的控件共用这一份**：下限 1（比 1 秒还快的巡逻没有意义），
#: 上限一天。两处各写一遍数字，迟早在「界面能设成什么」与「配置收什么」之间裂开
#: （`fallback` / `net.mode` 那两个闭集的处置同理：闭集只写一次）。
MIN_HELPER_POLL = 1
MAX_HELPER_POLL = 86400

#: ``net.mode`` 取值闭集（§2.5）：固化 ``pin`` 是 ``dhcp`` 的修饰，**不是**它的兄弟。
NET_MODE_STATIC = "static"
NET_MODE_DHCP = "dhcp"
NET_MODES = (NET_MODE_STATIC, NET_MODE_DHCP)

#: ``fallback`` 取值闭集（§2.2 冲突 A / §4.7 二）：所有场景都不匹配时怎么办。
#: 本步只立形态与校验，行为随第 6 步。
FALLBACK_KEEP = "keep"
FALLBACK_DHCP = "dhcp"
FALLBACKS = (FALLBACK_KEEP, FALLBACK_DHCP)

#: ``plugins.route_guard`` 段顶层允许出现的键。
#: 严格：拼错的键宁可当场报错，也不要被静默忽略——静默忽略的手写配置，症状是
#: 「写了不生效」而没有任何提示，比报错难查得多。
#: 注意 ``rules`` **不在**其中：旧模型的键单独给一条指着改法的报错（见 ``build_routes``）。
#: ``poll`` 归框架的节拍覆盖（决议 D27），本模块不读它，但它是合法的段内键。
_PLUGIN_SECTION_KEYS = frozenset({
    "enabled", "poll", "helper_poll", "paused", "fallback", "pinned",
    "profiles", "rule_overrides",
    "handback_ifaces", "handback_dns",
})

#: ``rule_overrides.<场景名>`` 条目允许出现的键（§4.8：覆盖层的位置只有一处）。
#: 旧模型的 ``force_apply``（真手动：强制开 / 强制关）随第 5 步下线——
#: 「强制开」是配置层的意思（`routes.enabled`），覆盖层只回答「**本次**要不要叠加」。
_OVERRIDE_KEYS = frozenset({"routes_suppressed"})


#: 各层允许出现的键（严格：拼错的键宁可当场报错，也不要被静默忽略）。
_PROFILE_KEYS = frozenset({"name", "ifaces", "manual_only", "match", "net", "routes"})
_MATCH_KEYS = frozenset({"subnet", "gateway"})
_NET_STATIC_KEYS = frozenset({"mode", "ip", "mask", "gateway", "dns"})
_NET_DHCP_KEYS = frozenset({"mode", "pin"})
_PIN_KEYS = frozenset({"ip", "mask", "gateway", "dns",
                       # 「照抄租约」（§2.5 的补充）：只写 true，与同名的写死值互斥。
                       "gateway_from_lease", "dns_from_lease"})
#: 「照抄租约」的两个开关（键名 → 它跟哪个写死值互斥 → 中文名，用于报错）。
_PIN_LEASE_FLAGS = (("gateway_from_lease", "gateway", "网关"),
                    ("dns_from_lease", "dns", "DNS"))
_ROUTES_KEYS = frozenset({"enabled", "items"})
_ROUTE_ITEM_KEYS = frozenset({"destination", "via", "enabled"})


#: 「这个键我不认识」那句的尾巴：把用户最该做的两个动作直接写出来。
#:
#: 早先这句只有「含未知字段：['force_apply']」—— 读起来像**用户自己打错了字**（用户
#: 原话：「防止用户理解错误」）。而实测里绝大多数是**旧版本 BarKit 自己写下的键**
#: （如面板开关从前写的 ``force_apply``：用户从没手编过它，是程序写进去的），
#: 新版程序把它当未知键拒了。定性指错方向，用户就会去自己的 YAML 里找错别字，
#: 而正确动作是删掉那几行（界面上看不见它，只能手改）—— 现在还能一键清掉。
_UNKNOWN_TAIL = (
    "——多半是旧版本 BarKit 自己写下的（不是您写错了）；删掉这几行即可，"
    "也可以在设置窗里这个组件的页尾点「恢复默认配置」"
)
