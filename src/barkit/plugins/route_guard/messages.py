"""四条「遇到旧模型 / 上一代遗留键」时的**报错正文**。

它们都是整句文案而不是模板：组件不改写用户验收过的那份 YAML ⇒ 不自动迁移、给一句
指路的话（指到「改法」而不是「你写错了」）。四条分别是：旧模型的 ``rules[]``、已下线的
死键 ``state_poll``、上一代的 ``handback_request``、上一代的 ``ops``。

本模块零依赖（无 import）。

``rules.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations


#: 读到旧模型 ``rules[]`` 时的报错正文。不自动迁移的原因见模块 docstring。
_LEGACY_RULES_MESSAGE = (
    "配置段里还写着旧模型的 rules[]（新模型是 profiles[]）——它已不再被读取，"
    "也不会被自动迁移：请把每一条旧规则改写成一个场景（"
    "iface → ifaces: [iface]；gateway → match.gateway 且兼作 items[].via；"
    "destinations → routes.items[]；原 priority 数字丢掉，按想要的顺序排列即可），"
    "或删掉这一段让组件回落到 config.default.yaml 的默认场景"
)

_LEGACY_STATE_POLL_MESSAGE = (
    "配置段里还有已下线的死键 state_poll —— 直接删除这一行即可。"
    "（D27：面板刷新就是框架 tick() 的节拍，不是独立的配置键）"
)

#: 读到上一代的 ``handback_request`` 时的报错正文（2026-09-29 换通道）。
#: 不自动迁移的理由同 ``_LEGACY_RULES_MESSAGE``：**组件不改写用户验收过的那份 YAML**。
_LEGACY_HANDBACK_REQUEST_MESSAGE = (
    "配置段里还有上一代的在途请求 handback_request —— 它已被「命令通道」取代，"
    "请直接删掉这一行：新版在面板徽标下拉里按「恢复 DHCP」，程序自己写入待执行的命令，"
    "执行完自动消失。"
)

#: 读到上一代的 ``ops`` 时的报错正文（2026-09-29 会话通道）。
#: 它是**程序自己写的**键（按下「恢复 DHCP」时写进配置、执行完自己撤掉）；通道换成会话
#: 之后命令走 ``op.run`` 帧，配置里再也不该出现它。留在用户 YAML 里的只可能是上一代
#: 遗物 —— 不认它、也不替用户删，给一句指路的错。
_LEGACY_OPS_MESSAGE = (
    "配置段里还有上一代的在途命令 ops —— 它已被会话命令取代（一帧一命令、回帧即回执，"
    "不再往配置里写任何东西），请直接删掉这一行；要交还网卡请在面板徽标下拉里按"
    "「恢复 DHCP」。"
)
