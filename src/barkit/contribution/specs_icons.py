"""``IconName`` —— **语义图标名**枚举。禁止插件使用 emoji，资源由 ``shell/design/icons.py`` 统一提供；命名规范是 ``{DOMAIN}_{VARIANT}``，组件名不进入图标名（图标是按语义增长的公共资源）。

本模块是依赖图的叶子（只依赖标准库 ``enum``，不 import 任何同包兄弟）—— 于是每个要画图标的那一族都能单向取它，不必担心谁反过来依赖图标。

``specs.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from enum import Enum


class IconName(Enum):
    """语义图标名。禁止插件使用 emoji，资源由 shell/design/icons.py 统一提供。

    命名规范（决议 D19）：领域变体统一 ``{DOMAIN}_{VARIANT}``（如 ``DISPLAY_ON`` /
    ``DISPLAY_OFF``）；**组件名不进入图标名**——图标是按**语义**增长的公共资源，
    不是某个组件的私产（``hosts_guard`` 与 ``route_guard`` 共用 ``SHIELD`` 同理）。

    资源分两族（决议 D19）：**主字形**（24 网格线性，供标题栏 / 列表行 / 面板）与
    **角标字形**（12 网格简化，供托盘角标，**可缺省**）。角标字形缺省时框架回落到
    该取值**级别**的通用标记 —— 于是任何组件至少能用 ``MARK_*`` 满足「图标随取值变」，
    领域变体按需增量增加，不阻塞组件重写。
    """

    ROUTE = "route"
    # 路由 + 注意（取值图标，2026-10-05 新增，首个消费方＝`route_guard` 那两个 WARN
    # 取值：``routes.partial``（部分叠加）与 ``profile.conflict``（多场景同时命中））。
    # 做法照 ``TABLET_WARN`` —— **保留折线与起点圆点，只把箭头换成叹号**：取值翻转时
    # 「这是条路由」的识别特征不动，只有末端那一点变。
    # 为什么现在才加：``ROUTE`` 此前是**零消费方**的死资源 —— 真正做路由的那个组件
    # 一直挂着 ``SHIELD``（与 `hosts_guard` 同一枚）⇒ 四台状态机画的是同一个字形，
    # D13「图标随取值变」在那台机上等于没生效（与 2026-09-19 加 ``TABLET_WARN`` 时
    # 在 `ipad_tunnel` 身上撞见的是同一件事）。
    ROUTE_WARN = "route_warn"
    SHIELD = "shield"
    BELL = "bell"
    KEYBOARD = "keyboard"
    SCAN = "scan"
    TABLET = "tablet"
    # 平板 + 注意（取值图标，2026-09-19 新增）。首个消费方＝`ipad_tunnel` 的 `error`
    # 取值：那台状态机此前九个取值一个取值图标都没有，全部回落主图标 `TABLET`
    # ⇒ 「图标随取值变」（D13）在那台机上等于没生效。按 D19「不预先堆砌、随组件
    # 增量加」，它只加真有取值要用的那一枚。
    TABLET_WARN = "tablet_warn"
    DNS = "dns"
    GEAR = "gear"
    # 电源/退出字形（2026-09-17 新增，首个消费方＝面板底部栏 C 区的「退出」图标按钮）。
    # 按 D19「不预先堆砌」：只加真有消费方要用的那一枚。它属**界面语义**而非组件领域语义，
    # 与 GEAR（设置）同类 —— 组件不该拿它当自己的取值图标。
    POWER = "power"
    # 屏幕字形（取值图标：实心 = 第二屏在线 / 空心 = 无第二屏）
    DISPLAY_ON = "display_on"
    DISPLAY_OFF = "display_off"
    # 电池族三枚（领域变体，2026-09-19 新增，是 D19 命名规范 ``{DOMAIN}_{VARIANT}``
    # 里点名的那个例子）。三枚**共用同一外框与正极触点**，靠「框内有没有东西、
    # 有几个」区分取值 —— 与 ``DISPLAY_ON`` / ``DISPLAY_OFF`` 同一套做法：取值翻转
    # 时轮廓不动、只有内部明暗变，那一格换图视觉最稳。
    # 今天的消费方是《组件契约规范-完整例子》的可抄范例（有门禁
    # ``test_doc_examples_reference_real_icons`` 钉住），本仓尚无电池组件 —— 与
    # `SHIELD` 被两个组件共用同理：图标是**语义资源**，不按组件数量增长。
    BATTERY = "battery"
    BATTERY_LOW = "battery_low"
    BATTERY_CHARGING = "battery_charging"
    # 耳机（2026-09-22 新增，首个消费方＝`audio_guard`）。按 D19「不预先堆砌」只加这一枚：
    # 有线耳机这条链路上「插着吗 / 认出麦克风了吗」是同一个物件的两种状态，用同一枚图标
    # 既当身份图标又当取值图标（取值靠级别色区分），不必再造一对实心/空心变体。
    HEADSET = "headset"
    # 鼠标（2026-09-23 新增，首个消费方＝`bt_guard` 的唤醒重连看护）。同 HEADSET 的理由
    # 只加**一枚**：「在线 / 掉线 / 重开中 / 读不到」说的是同一只设备的不同处境（不像
    # DISPLAY_ON/OFF 那样轮廓本身要变），取值靠级别色区分即可，不必再造一对变体。
    MOUSE = "mouse"
    # 通用级别标记 5 枚（D19 的**兜底资源**，对应 StateLevel 五值）：
    # 它们与级别一一对应，是「角标字形缺省时的回落目标」，也是任何组件最省事的取值图标。
    MARK_OK = "mark_ok"
    MARK_IDLE = "mark_idle"
    MARK_OFF = "mark_off"
    MARK_WARN = "mark_warn"
    MARK_ERR = "mark_err"
    # 清单（首个消费方＝`todo` 插件）。按 D19「不预先堆砌」只加这一枚：到点提醒的待办
    # 清单的身份图标。不复用 BELL 的理由：BELL 是**通知**语义（`notify_guard` /
    # `bt_guard` / `audio_guard` 都拿它当自己的身份图标），而这里说的是「一张有待办
    # 做完没有的清单」，两者混起来会让面板上两行看着像同一件事。
    CHECKLIST = "checklist"
