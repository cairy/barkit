"""三台状态机的取值闭集（决议 D13）：取值名 / 级别 / 取值图标 /
默认文案只在**这里**定义一次（设计 §4.1 / §4.2），报告只报取值名，框架回查本表取
级别与图标。首格 ``unknown`` 是**首屏起点**，取值集里必须有这一格。

``_NOTIFY_TEXT`` 由 ``NOTIFY_STATES`` 派生（不是第二份真源）—— ``_notify_snapshot``
要在默认词条后面接一句该取值自己的补充说明，得先拿到词条本身。

``plugin.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from barkit.contribution.specs import (
    IconName,
    StateLevel,
    StateOption,
)
from barkit.plugins.notify_guard import signals


STALE_S = 15  # 守卫失联阈值（poll=3s × 5 余量）


# 三台机的取值闭集（决议 D13）：取值名 / 级别 / 取值图标 / 默认文案只在**这里**
# 定义一次（设计 §4.1 / §4.2），报告只报取值名，框架回查本表取级别与图标。
# 首格 ``unknown`` 是**首屏起点**（``initial``）：决议把 initial 从「读现场的工厂」
# 改成「从哪一格开始」之后，取值集里必须有这一格，否则首屏只能拿某个具体读数
# 冒充「还不知道」（同 ``frozen`` 的「不撒谎」判据）。


DISPLAY_STATES: tuple[StateOption, ...] = (
    StateOption("unknown", StateLevel.IDLE, IconName.DISPLAY_OFF, "尚未读取"),
    # 第二屏在线 —— 面板行左上的**实心屏**（本组件在菜单栏那一格的主看点）
    StateOption(signals.DISPLAY_EXTENDED, StateLevel.OK, IconName.DISPLAY_ON, "第二屏已连接"),
    # 进程在跑但没侦测到第二屏（**含 mirror 模式盲区**，设计 §2.3 / §9.1-3 已裁定接受）
    StateOption(signals.DISPLAY_RUNNING, StateLevel.IDLE, IconName.DISPLAY_OFF, "OpenDisplay 运行中"),
    StateOption(signals.DISPLAY_IDLE, StateLevel.IDLE, IconName.DISPLAY_OFF, "OpenDisplay 未运行"),
    StateOption(signals.DISPLAY_ABSENT, StateLevel.IDLE, IconName.DISPLAY_OFF, "未安装 OpenDisplay"),
)


NOTIFY_STATES: tuple[StateOption, ...] = (
    StateOption("unknown", StateLevel.IDLE, text="尚未读取"),
    StateOption("allowed", StateLevel.OK, text="通知正常"),
    # 静音（WARN，alerts=True）值得让用户看见，故显式写出来而非靠级别默认
    #（D17 之后静音就是普通取值，不再走托盘那条「静」字符特殊通道）。
    # 取值图标：本机所有取值都不带取值图标 ⇒ 经 ``state_view.resolve`` 回落主图标 BELL；
    # BELL 已有角标字形（D19 已补），muted 直接画 BELL 角标（橙）。不需要「铃划掉」：
    # allowed 是 OK 级、低于托盘阈值本就不出角标，muted（橙铃）vs allowed（无角标）已天然分开。
    StateOption("muted", StateLevel.WARN, text="通知静音", alerts=True),
    StateOption("manual", StateLevel.WARN, text="手动静音"),
    # ``desync`` 是「状态自证」的唯一出口（系统实际值与守卫期望不符），必须留（§4.1）
    StateOption("desync", StateLevel.ERR, text="状态不同步"),
    # ``frozen``：自动化停用时的降级取值（§4.2「不撒谎」——压灰，不报红）
    StateOption("frozen", StateLevel.IDLE, text="自动化已停用"),
)


AUTOMATION_STATES: tuple[StateOption, ...] = (
    StateOption("unknown", StateLevel.IDLE, text="尚未读取"),
    StateOption("running", StateLevel.OK, text="自动化运行中"),
    StateOption("stopped", StateLevel.OFF, text="自动化已停用"),
)


# 取值名 → 默认文案。**派生自上面的声明**，不是第二份真源：`_notify_snapshot` 需要在
# 默认词条后面接一句该取值自己的补充说明，得先拿到词条本身。
_NOTIFY_TEXT: dict[str, str] = {opt.id: opt.text for opt in NOTIFY_STATES}


class _StatesMixin:
    """三台状态机的取值闭集：第二屏 / 通知 / 自动化的取值、级别、图标与默认文案（决议 D13）。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """
