"""``core/power``：四条电源事件（系统 / 显示器 × 睡眠 / 唤醒）的事件源。

本文件是从 ``test_audio_guard.py`` 搬过来的 —— 这个事件源随音频看护立起来，但它早已不只
服务音频一家（蓝牙看护，以及后面每个「醒来要重读现场」的组件），再挂在音频的测试文件里
会越写越找不到。

**两条贯穿全篇的口径**：
① 四条通知必须都在（少一族＝订阅方除启动那轮外永不运行，见 ``core/power`` docstring）；
② 时机按族分（唤醒类等稳定窗口、睡眠类立即投）—— 这是本模块唯一的语义。
"""

from __future__ import annotations

import sys

import pytest

from barkit.core.events import EventBus
from barkit.core.plugin import Plugin


class _FakeTimer:
    """``QTimer`` 替身：只记下「排了什么」，让用例不必真等 3 秒。"""

    calls: list = []

    @staticmethod
    def singleShot(ms, fn):
        _FakeTimer.calls.append((ms, fn))


@pytest.fixture
def fake_qtimer(monkeypatch):
    _FakeTimer.calls = []
    monkeypatch.setattr("PySide6.QtCore.QTimer", _FakeTimer)
    yield _FakeTimer
    _FakeTimer.calls = []


class _Center:
    """通知中心替身：记下注册 / 摘除了什么。"""

    def __init__(self):
        self.added: list = []
        self.removed: list = []

    def addObserver_selector_name_object_(self, obs, sel, name, obj):
        self.added.append((obs, sel, name, obj))

    def removeObserver_(self, obs):
        self.removed.append(obs)


class _NSObject:
    """``NSObject`` 替身：只需 ``alloc().init()`` 这一条链。"""

    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self


class _Workspace:
    def __init__(self, center):
        self._center = center

    def sharedWorkspace(self):
        return self

    def notificationCenter(self):
        return self._center


#: 四条通知的替身名字 —— 真名是 ``NSWorkspace…`` 常量，离屏下拿不到（也不必拿到：
#: 用例要验的是「哪条选择器配哪个名字」，用假名反而看得更清楚）。
FAKE_NAMES = {
    "wake": "wake-name",
    "sleep": "sleep-name",
    "display_wake": "display-wake-name",
    "display_sleep": "display-sleep-name",
}


def _install(monkeypatch, *, center=None, NSObject=_NSObject, names=None):
    """把 ``_load_appkit`` 换成替身；返回 ``(source, center)``（**还没 start**）。"""
    from barkit.core import power as power_mod

    center = _Center() if center is None else center
    api = power_mod._AppKit(
        NSObject=NSObject,
        NSWorkspace=_Workspace(center),
        names=FAKE_NAMES if names is None else names,
    )
    monkeypatch.setattr(power_mod, "_load_appkit", lambda: api)
    return power_mod.PowerSource(EventBus()), center


# ══════════════════════════════════════════════════════════════════════
# 一、投递时机：按族分，两族刚好相反
# ══════════════════════════════════════════════════════════════════════

def test_wake_event_is_deferred_by_the_settle_window(fake_qtimer):
    """唤醒 = 「排一个延时投递」，窗口内**一条都不发**。

    这就是本模块存在的理由：系统刚睁眼时音频那条链还在恢复，立刻投递只会让订阅方
    读到半成品现场。所以「排期」与「投递」必须分开断言。
    """
    from barkit.core.power import POWER_WAKE_TOPIC, WAKE_SETTLE_MS, PowerSource

    events = EventBus()
    seen: list = []
    events.subscribe(POWER_WAKE_TOPIC, seen.append)

    source = PowerSource(events)
    source._on_event("wake")

    assert [ms for ms, _ in fake_qtimer.calls] == [WAKE_SETTLE_MS]
    assert seen == []                              # 窗口内：先不投递

    fake_qtimer.calls[0][1]()                      # 窗口到点
    assert len(seen) == 1
    assert seen[0].kind == "wake"
    assert isinstance(seen[0].at, float) and seen[0].at > 0


def test_display_family_follows_the_same_rule(fake_qtimer):
    """显示器那一族与系统那一族**时机相同**：睡眠立即、唤醒等窗口。

    这条守的是「关屏 ≠ 睡眠」那件事补齐之后最容易漏的一半 —— 只把通知注册上、却把
    时机写反（或照抄唤醒那条给显示器睡眠也套窗口）。套了窗口的后果是真机上「关屏时
    记不到基线」，而离屏测试里完全看不出来（都只是「排了个延时」）。
    """
    from barkit.core.power import (
        POWER_DISPLAY_SLEEP_TOPIC,
        POWER_DISPLAY_WAKE_TOPIC,
        WAKE_SETTLE_MS,
        PowerSource,
    )

    events = EventBus()
    seen: list = []
    events.subscribe(POWER_DISPLAY_SLEEP_TOPIC, seen.append)
    events.subscribe(POWER_DISPLAY_WAKE_TOPIC, seen.append)

    source = PowerSource(events)
    source._on_event("display_sleep")
    assert [e.kind for e in seen] == ["display_sleep"], "显示器睡眠被排了延时"
    assert fake_qtimer.calls == []                 # 睡眠那条不经定时器

    source._on_event("display_wake")
    assert [e.kind for e in seen] == ["display_sleep"], "显示器唤醒没等稳定窗口"
    assert [ms for ms, _ in fake_qtimer.calls] == [WAKE_SETTLE_MS]

    fake_qtimer.calls[0][1]()
    assert [e.kind for e in seen] == ["display_sleep", "display_wake"]


def test_settle_window_covers_the_codec_detection_time():
    """窗口必须盖过插孔识别出终态的时间（实测约 1.7s）。

    窗口比它短 ⇒ 订阅方每次醒来读到的都是「识别中」，白报一轮；这条把那个数字钉住。
    """
    from barkit.core.power import WAKE_SETTLE_MS

    assert WAKE_SETTLE_MS >= 2000


def test_emitting_survives_a_throwing_subscriber():
    """投递失败不得掀翻回调线程（订阅方是别人的代码）。"""
    from barkit.core.power import POWER_WAKE_TOPIC, PowerSource

    class _Events:
        def publish(self, topic, payload):
            raise RuntimeError("订阅方炸了")

    source = PowerSource(_Events())
    source._emit(POWER_WAKE_TOPIC, "wake")         # 不抛出


# ══════════════════════════════════════════════════════════════════════
# 二、注册：四条都要，且必须真能回调
# ══════════════════════════════════════════════════════════════════════

def test_power_source_registers_all_four_and_removes_one_observer(monkeypatch):
    """一次 ``start()`` 注册**四条**通知，共用同一个观察者；``close()`` 摘掉它。"""
    source, center = _install(monkeypatch)

    assert source.start() is True
    assert source.active is True
    assert len(center.added) == 4
    assert [sel for _o, sel, _n, _t in center.added] == [
        b"onWake:", b"onSleep:", b"onDisplayWake:", b"onDisplaySleep:"]
    assert [n for _o, _s, n, _t in center.added] == [
        FAKE_NAMES["wake"], FAKE_NAMES["sleep"],
        FAKE_NAMES["display_wake"], FAKE_NAMES["display_sleep"]]
    assert {o for o, _s, _n, _t in center.added} == {source._observer}
    assert all(t is None for _o, _s, _n, t in center.added)
    # 必须被持引用，否则被 GC 掉 —— 四条通知会一起哑掉，且没有任何报错
    assert source._observer is not None

    assert source.start() is True                       # 幂等：不重复注册
    assert len(center.added) == 4

    observer = source._observer                  # close() 之后这个字段就归零了
    source.close()
    assert center.removed == [observer]
    assert source.active is False
    source.close()                                      # 再摘一次不抛


def test_every_declared_selector_exists_on_the_observer(monkeypatch):
    """表里的每个选择器，观察者类上都要有同名方法。

    守的是「登记成功 ≠ 监听生效」：PyObjC 不会因为选择器名写错而拒绝注册，它只是永远
    不回调 —— 外面看不出「没订上」与「订上了但系统没发」的区别。表与类是两个地方，这
    条把它们对起来（改名字时漏一处就红）。
    """
    from barkit.core import power as power_mod

    source, _center = _install(monkeypatch)
    observer_cls = type(power_mod._make_observer(_NSObject, source))

    missing = [sel for sel, _kind, _topic, _settle in power_mod._NOTIFICATIONS
               if not callable(getattr(observer_cls, sel.rstrip(":") + "_", None))]
    assert missing == [], f"这些选择器在观察者上没有对应方法：{missing}"


def test_observers_get_unique_class_names(monkeypatch):
    """同一进程里造第二个观察者，类名必须与第一个不同。

    PyObjC 按**类名**向 Objective-C 运行时注册：第二次定义同名类会抛
    ``_Observer is overriding existing Objective-C class`` ⇒ 第二个事件源的 ``start()``
    直接失败、观察者一枚都没挂上（日志只有一行 ERROR）。替身 ``NSObject`` 不会报这个
    错，所以这里直接盯**类名** —— 那正是 PyObjC 用来判重的依据。
    """
    from barkit.core import power as power_mod

    source, _center = _install(monkeypatch)
    names = [power_mod._make_observer(_NSObject, source).__class__.__name__
             for _ in range(2)]
    assert len(set(names)) == 2, names


@pytest.mark.skipif(sys.platform != "darwin", reason="非 macOS：没有 PyObjC 原生分支")
def test_two_sources_can_start_in_one_process():
    """真件（真 AppKit、真通知中心）：两个事件源都要挂得上。

    上面那条盯的是类名这个「判重依据」，这条把真实后果钉住 —— 顺带证明四条通知名在真
    AppKit 里都存在（名字写错时 ``addObserver`` 同样会抛）。
    """
    from barkit.core.power import PowerSource

    first, second = PowerSource(EventBus()), PowerSource(EventBus())
    try:
        assert first.start() is True
        assert second.start() is True, "第二个事件源没挂上"
        assert first._observer is not second._observer
    finally:
        first.close()
        second.close()


def test_every_kind_in_the_table_has_a_delivery_rule():
    """表与投递规则必须同源：表里每个事件名都要查得到话题与时机。

    查不到就是 ``KeyError``（回调线程上抛，四条一起哑）或静默什么都不投 —— 两种都
    比直接红掉难查，所以在这里钉住。
    """
    from barkit.core import power as power_mod

    kinds = [kind for _sel, kind, _topic, _settle in power_mod._NOTIFICATIONS]
    assert sorted(power_mod._KINDS) == sorted(kinds)
    for kind in kinds:
        topic, settle = power_mod._KINDS[kind]
        assert topic.startswith("system.power.")
        assert isinstance(settle, bool)
    # 话题全局唯一：两条通知共用一个话题会让订阅方分不出是哪个方向来的
    topics = [topic for _sel, _kind, topic, _settle in power_mod._NOTIFICATIONS]
    assert len(set(topics)) == len(topics)


def test_a_notification_name_missing_from_the_table_is_rolled_back(monkeypatch):
    """名字表少一条 ⇒ ``start()`` 报 False，并且**不留下半注册**。

    半注册的下场是「``start()`` 说失败、事件却还在流动」：已经挂上的那几条照样回调。
    四条通知里第 2~4 条失败的概率比只有两条时高得多，所以回滚值得写、也值得守。
    """
    source, center = _install(monkeypatch, names={"wake": "wake-name"})   # 缺三条

    assert source.start() is False
    assert source.active is False
    assert center.added, "这条用例要盯的正是「挂上了一条才发现名字缺失」"
    assert center.removed == [o for o, _s, _n, _t in center.added], "半注册的观察者没回滚"


def test_power_source_degrades_when_appkit_is_missing(monkeypatch):
    """非 macOS / 没装 PyObjC ⇒ ``start()`` 返回 False 且不抛，订阅方照常运行。"""
    from barkit.core import power as power_mod

    monkeypatch.setattr(power_mod, "_load_appkit", lambda: None)
    source = power_mod.PowerSource(EventBus())
    assert source.start() is False
    assert source.active is False
    source.close()                                 # 没注册过也必须幂等


def test_power_source_start_failure_is_logged_not_raised(monkeypatch):
    """造观察者本身失败（系统 API 抽风）⇒ 返回 False，不拦启动。"""
    class _BoomNSObject:
        @classmethod
        def alloc(cls):
            raise RuntimeError("注册炸了")

    source, center = _install(monkeypatch, NSObject=_BoomNSObject)
    assert source.start() is False
    assert source.active is False
    assert center.added == []                      # 一枚都没挂上


# ══════════════════════════════════════════════════════════════════════
# 三、基类的空声明默认（「不声明就不订阅」）
# ══════════════════════════════════════════════════════════════════════

def test_base_plugin_declares_no_system_events_and_noop_handler():
    """没有声明就不订阅；基类回程是空实现（纯告知型组件的默认）。"""
    plugin = Plugin(None)
    assert plugin.event_topics() == []
    assert plugin.on_system_event("system.power.wake", object()) is None
