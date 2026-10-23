"""core/power：框架统一事件源 —— 系统与显示器的睡眠／唤醒四条事件（§8.2.1 命名）。

**为什么需要它**：睡眠恢复后，音频编解码器、外设、网络栈都要重新做一遍它们的检测与
枚举。组件若想知道「机器刚醒过来」，此前只能靠轮询或者自己接系统 API —— 前者费电且
有延迟，后者违反「系统事件一律收归框架、插件不各自接系统 API」这条规矩。

**两族、四个方向 —— 少订一族等于没订**（2026-09-24 现场）：

======================  =============================  ==========
事件                    话题                           投递时机
======================  =============================  ==========
系统将要睡眠            ``system.power.sleep``         **立即**
系统已唤醒              ``system.power.wake``          稳定窗口后
显示器将要关闭          ``system.power.display_sleep`` **立即**
显示器已唤醒            ``system.power.display_wake``  稳定窗口后
======================  =============================  ==========

**「关屏」不是「睡眠」**：本机连续多日 ``pmset -g log`` 里 ``Sleep/Wakes since boot`` 都是
``0``，关屏只留下 ``Display is turned off/on`` —— 日常那次「关屏 / 亮回来」走的是**显示器
那一族**通知，``WillSleep`` / ``DidWake`` 压根不来。只订后者的组件在这台机器上除启动那
一轮外**永远不会被点火**（第一个受害者是蓝牙看护：装好之后零事件、鼠标掉线两小时无人
过问、面板停在启动那轮的结论）。反过来，真合盖睡眠时两族**会同时来**，所以订阅方必须
自己保证「同一轮边界只算一次」，不能靠动作冷却兜。

**投递时机是本模块唯一的语义，两族刚好相反**（见上表 —— 别按「对称」去改）：

- **唤醒类**（``…wake`` ／ ``…display_wake``）：**等稳定窗口再投**。唤醒通知在系统「刚
  睁眼」时就到了，此刻不少子系统还在恢复中（音频那条链尤其明显：插孔识别分几步做，
  实测要一秒多才出结论）。立刻投，订阅方读到的是**半成品现场**，据此做的判断会自相
  矛盾。语义＝「**系统已经醒来、可以读现场了**」—— 口径在这一层确立一次，所有订阅方
  共享，不必各自 sleep 一遍。
- **睡眠类**（``…sleep`` ／ ``…display_sleep``）：**立即投，一秒都不等**。它们的全部价值
  就是「趁进程还活着读一次现场」，而系统随后就要冻结进程 —— 这里再排一个延时回调，多半
  等不到。所以宁可让订阅方在「接近冻结」的时刻抢着读；读不到由它自己如实降级（订阅方
  少一个读数的代价，比一个永远不来的事件小得多）。显示器那一族的「立即」还多一层理由：
  屏幕一灭，紧接着就可能是合盖真睡眠，那一拍是最后一次能安稳读现场的机会。

**线程**：``NSNotificationCenter`` 在注册它的线程投递通知，而本模块在总线所在线程
（主线程）注册，故 ``EventBus.publish`` 也在主线程 —— 与其余框架事件源一致。

非 macOS 上本模块可用但 ``start()`` 返回 False（无事件源），订阅方照常运行、只是收不到
这些事件 —— 降级而不是报错。
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from barkit.core.events import EventBus

log = logging.getLogger(__name__)

#: §8.2.1 系统事件命名：``system.<domain>.<event>``
POWER_WAKE_TOPIC = "system.power.wake"
POWER_SLEEP_TOPIC = "system.power.sleep"
#: 显示器那一族 —— **另一族通知**，与系统睡眠 / 唤醒不是一回事（见模块 docstring）。
#: 用 ``display`` 而不是 ``screen``：``system.screen.*`` 那个域留给锁屏，别混。
POWER_DISPLAY_WAKE_TOPIC = "system.power.display_wake"
POWER_DISPLAY_SLEEP_TOPIC = "system.power.display_sleep"

#: 唤醒类事件投递前的稳定窗口（毫秒）。取值依据：实测音频插孔识别分两步，
#: 第一步约 0.4s、出终态约 1.7s，所以「醒来」到「现场可读」要留够这个量级。
WAKE_SETTLE_MS = 3000
# 睡眠类事件**没有**对应的窗口常量 —— 它们刻意不延时，理由见模块 docstring。
# 别为「看起来对称」给它们补一个 0：那会让下一个读者以为这里本该有个可调窗口。

#: 四条通知的**单一真源**：``(观察者选择器, 事件名, 话题, 是否等稳定窗口)``。
#: 加一条通知只改这里一处 —— 选择器名 / 话题名 / 时机绑在同一条上，分开写就会「注册了
#: 却从不投递」且不报错（PyObjC 不会因为选择器名写错而拒绝注册，它只是永远不回调）。
_NOTIFICATIONS: tuple[tuple[str, str, str, bool], ...] = (
    ("onWake:", "wake", POWER_WAKE_TOPIC, True),
    ("onSleep:", "sleep", POWER_SLEEP_TOPIC, False),
    ("onDisplayWake:", "display_wake", POWER_DISPLAY_WAKE_TOPIC, True),
    ("onDisplaySleep:", "display_sleep", POWER_DISPLAY_SLEEP_TOPIC, False),
)

#: 事件名 → 说人话的说法（只进日志）。
_KIND_ZH = {
    "wake": "系统已唤醒",
    "sleep": "系统将要睡眠",
    "display_wake": "显示器已唤醒",
    "display_sleep": "显示器将要关闭",
}

#: 事件名 → (话题, 是否等稳定窗口)。观察者回调只报「是哪个方向」，其余全在这张表里查
#: —— 四个回调各写一份，等于把投递策略复制四遍。
_KINDS: dict[str, tuple[str, bool]] = {
    kind: (topic, settle) for _sel, kind, topic, settle in _NOTIFICATIONS
}


@dataclass(frozen=True)
class PowerEvent:
    """四条电源事件的载荷。

    ``at`` 是**投递时刻**，不是系统事件的原点时刻。两族的差别正在这里：唤醒类含稳定
    窗口，``at`` 比真实醒来晚几秒；睡眠类不含窗口，``at`` 就是「即将冻结」的时刻本身。
    想算「醒来多久了」的订阅方用它做基准即可，不必自己去找原点。

    ``kind`` 是四个方向之一（``wake`` / ``sleep`` / ``display_wake`` /
    ``display_sleep``）—— 订阅方若只订了其中两条，可以直接不看它；两族都订的组件
    靠它区分「同一轮边界里现在来的是哪一半」。
    """

    kind: str
    at: float


@dataclass(frozen=True)
class _AppKit:
    """``_load_appkit()`` 的产物：PyObjC 里要用的那几样，**按名字取，不用位置元组**。

    只有两件东西时位置元组还够用，四件就开始靠数数了 —— 测试替身也要跟着数，加一条
    通知就得改所有构造点，而且位置写错不会报错、只会安静地订到别的通知上去。
    """

    NSObject: type
    NSWorkspace: type
    #: 事件名 → 系统通知名（两条字符串常量，不做解释）。
    names: dict = field(default_factory=dict)


def _load_appkit():
    """延迟导入 PyObjC；非 macOS 或缺包时返回 None（降级为无事件源）。"""
    try:
        # 下面这几条 import 本身就是「本平台有没有这族通知」的探针：拿不到就是没有。
        from Foundation import NSObject
        from AppKit import (
            NSWorkspace,
            NSWorkspaceDidWakeNotification,
            NSWorkspaceScreensDidSleepNotification,
            NSWorkspaceScreensDidWakeNotification,
            NSWorkspaceWillSleepNotification,
        )
    except Exception:  # noqa: BLE001 —— 导入失败即「本平台没有这个事件源」
        log.debug("电源事件源不可用（非 macOS 或缺 PyObjC）", exc_info=True)
        return None
    return _AppKit(
        NSObject=NSObject,
        NSWorkspace=NSWorkspace,
        names={
            "wake": NSWorkspaceDidWakeNotification,
            "sleep": NSWorkspaceWillSleepNotification,
            # AppKit 管显示器那一族叫 ``Screens…``，话题名里用 ``display`` ——
            # 两处名字不一样是**故意的**：前者是系统 API 的拼法，后者是本仓的域命名。
            "display_wake": NSWorkspaceScreensDidWakeNotification,
            "display_sleep": NSWorkspaceScreensDidSleepNotification,
        },
    )


class PowerSource:
    """四条电源事件（系统 / 显示器 × 睡眠 / 唤醒）的**唯一**事件源。

    四条共用同一个观察者与同一个通知中心（见 ``start()``）—— 它们本就是同族通知，
    拆成多个源只会让「谁在听睡眠」这个问题有多处答案。

    生命周期与宿主一致：``start()`` 在组合根装配时调用，``close()`` 在退出时调用。
    **不做懒加载**：电源事件是全局的，没有「谁声明才注册」的粒度可言，而底层只是一个
    通知观察者，常驻成本可以忽略。
    """

    def __init__(self, events: "EventBus") -> None:
        self._events = events
        self._api = _load_appkit()
        self._observer = None
        self._center = None

    @property
    def active(self) -> bool:
        """观察者是否已注册（= 本平台可用且 ``start()`` 成功过）。"""
        return self._observer is not None

    def start(self) -> bool:
        """注册四条通知；返回是否成功（失败不抛异常 —— 没有事件源不是致命错误）。"""
        if self._observer is not None:
            return True
        if self._api is None:
            log.info("电源事件源不可用：本平台没有这套通知（非 macOS）")
            return False
        observer = center = None
        try:
            observer = _make_observer(self._api.NSObject, self)
            center = self._api.NSWorkspace.sharedWorkspace().notificationCenter()
            for selector, kind, _topic, _settle in _NOTIFICATIONS:
                center.addObserver_selector_name_object_(
                    observer, selector.encode("ascii"), self._api.names[kind], None)
        except Exception:  # noqa: BLE001 —— 注册失败只记日志，不拦启动
            log.exception("注册电源观察者失败，睡眠 / 唤醒后不再触发")
            # **回滚半注册**：注册到一半失败时，已经挂上的那几条照样会回调、照样投事件
            # —— 那就成了「start() 报失败，事件却还在流动」，比一条都没订更难查。四条
            # 通知里第 2~4 条失败的概率，比只有两条时高得多，所以回滚值得写。
            _silent_remove(center, observer)
            return False
        # 成功才落成实例状态：`active` 与 close() 都只看这两个字段。
        self._observer, self._center = observer, center
        log.info("电源事件源已就绪（%d 条通知共用一个观察者；唤醒类等 %d ms 稳定窗口再投，"
                 "睡眠类立即投）", len(_NOTIFICATIONS), WAKE_SETTLE_MS)
        return True

    def close(self) -> None:
        """摘掉观察者（幂等）。"""
        if self._observer is None or self._center is None:
            return
        try:
            self._center.removeObserver_(self._observer)
        except Exception:  # noqa: BLE001
            log.exception("摘除电源观察者失败")
        finally:
            self._observer = None
            self._center = None

    # ---------- 观察者回调（主线程）----------

    def _on_event(self, kind: str) -> None:
        """一条通知到达（主线程，已由 Cocoa 投递到这里）。

        这里只做「按表投递」：唤醒类排一个延时、睡眠类立刻投。**不读现场** —— 那是
        订阅方 ``tick()`` 的事，本层只管把「发生了什么」送出去。
        """
        topic, settle = _KINDS[kind]
        if not settle:
            log.info("%s，立即投递 %s", _KIND_ZH[kind], topic)
            self._emit(topic, kind)
            return

        from PySide6.QtCore import QTimer

        log.info("%s，%d ms 后投递 %s", _KIND_ZH[kind], WAKE_SETTLE_MS, topic)
        QTimer.singleShot(WAKE_SETTLE_MS, partial(self._emit, topic, kind))

    def _emit(self, topic: str, kind: str) -> None:
        """投递一条事件。

        四条共用一个投递口：它们除了 topic 与 kind 之外没有任何差别，各写一份等于把
        「投递失败怎么办」这条策略复制四遍。
        """
        import time

        try:
            self._events.publish(topic, PowerEvent(kind, time.time()))
        except Exception:  # noqa: BLE001 —— 事件投递失败不该掀翻回调线程
            log.exception("投递 %s 事件失败", topic)


def _silent_remove(center, observer) -> None:
    """尽力摘掉观察者（``start()`` 回滚半注册时用）；失败只记日志。"""
    if center is None or observer is None:
        return
    try:
        center.removeObserver_(observer)
    except Exception:  # noqa: BLE001
        log.exception("回滚半注册的电源观察者失败")


#: 动态造的观察者类的名字计数。**必须是唯一的** —— 见 ``_make_observer``。
_OBSERVER_SEQ = itertools.count(1)


def _make_observer(NSObject, source: PowerSource):
    """造通知观察者。

    两件必须做对的事：

    1. **必须由调用方持引用**（``source._observer``），否则被 GC 掉、醒来时再也没有回调。
    2. **类名必须每次都不同**。PyObjC 是按**类名**向 Objective-C 运行时注册的：同一个
       进程里第二次定义同名类会抛 ``_Observer is overriding existing Objective-C
       class``，于是第二个 ``PowerSource`` 的 ``start()`` 直接失败 —— 观察者一枚都没挂
       上，只有一行 ERROR 日志。原先每个实例都定义同名 ``_Observer``，在任何「同一进程
       里重新装配一次」的场景（离屏用例、宿主重启装配）下都会踩到，真机上看不见是因为
       生产只装配一次。名字带序号即免疫，代价只是一个几乎不占地方的类。

    四个方法只报「是哪个方向」，话题与时机都去 ``_KINDS`` 里查。选择器名必须与
    ``_NOTIFICATIONS`` 里的字面量**逐字一致** —— 差一个字母 PyObjC 照样注册成功、
    只是永远不回调（「登记成功」≠「监听生效」，外面看不出区别）。
    """
    namespace = {
        "onWake_": lambda self, _notification: source._on_event("wake"),
        "onSleep_": lambda self, _notification: source._on_event("sleep"),
        "onDisplayWake_": lambda self, _notification: source._on_event("display_wake"),
        "onDisplaySleep_": lambda self, _notification: source._on_event("display_sleep"),
    }
    observer_cls = type(f"_BarKitPowerObserver{next(_OBSERVER_SEQ)}", (NSObject,), namespace)
    return observer_cls.alloc().init()
