"""core/audio：音频设备事件源（``system.audio.device_changed``）测试。

**为什么测的是「读接口比对」而不是原生通知**：CoreAudio 的两套属性监听接口在本机
（macOS 15.7.5）实测登记成功却从不回调（排除法见模块 docstring）。本文件守的就是
那条替身路线 —— 读四个值、比对、等稳定窗口、投一条事件。

**为什么用替身**：真去插拔一枚 3.5mm 耳机在自动化里既慢又不可靠，而会**静默**出错的
恰恰是另外几件事 —— 「读不到现场时有没有降级」「变了没变怎么算」「一次变化投一条还是
投三条」「摘掉之后还会不会说话」。错了不抛异常，只是耳机插上去没人发现，故这里逐条钉死。
唯一碰真机的是最后那一条冒烟（只读、只验 ctypes 签名真被认下）。
"""

from __future__ import annotations

import struct
import sys

import pytest

from barkit.core import audio as audio_mod
from barkit.core.audio import (
    AUDIO_DEVICE_TOPIC,
    AudioDeviceEvent,
    AudioDeviceSource,
    _changed_kinds,
    _Fingerprint,
)
from barkit.core.events import EventBus
from barkit.core.heartbeat import Heartbeat


def _cc(fourcc: str) -> int:
    """四字码 → big-endian 整数（CoreAudio 选择器 / 作用域的写法）。"""
    return struct.unpack(">I", fourcc.encode("ascii"))[0]


#: 两枚现场读数：``_A`` 是耳机在、声音也走耳机；``_B`` 是耳机还插着、声音跑去了扬声器。
_A = _Fingerprint(default_output=83, default_input=85, devices=(83, 85, 92),
                  output_source=_cc("hdpn"))
_B = _Fingerprint(default_output=92, default_input=85, devices=(83, 85, 92),
                  output_source=_cc("ispk"))


# ──────────────────────────────────────────────
# 探针：只读、可降级
# ──────────────────────────────────────────────

def test_probe_is_degraded_off_macos(monkeypatch):
    """非 macOS：自报理由、``read()`` 一律 ``None`` —— 事件源据此降级，不抛异常。"""
    monkeypatch.setattr(audio_mod.sys, "platform", "linux")
    probe = audio_mod._CoreAudioProbe()
    assert probe.read() is None
    assert probe.reason == "非 macOS"


@pytest.mark.skipif(sys.platform != "darwin", reason="非 macOS：本机没有 CoreAudio")
def test_probe_really_reads_the_live_devices():
    """真机上真读一次，且读出来的设备 id 是**像样的整数**。

    **这不是形式主义**：``ctypes`` 默认按 ``int`` 处理返回值与参数，而 CoreAudio 的
    对象 id 是 64 位；不逐条声明签名会被静默截断 —— 表现是「偶尔读到垃圾」，最难查的
    那一类。签名没声明对时这条会拿到空清单或离谱的数字。
    """
    probe = audio_mod._CoreAudioProbe()
    fp = probe.read()
    assert fp is not None, "macOS 上 CoreAudio 必须读得动（读不动说明签名声明错了）"
    assert len(fp.devices) >= 1, "一台 Mac 至少有一枚音频设备"
    assert all(isinstance(d, int) and d > 0 for d in fp.devices), fp.devices


# ──────────────────────────────────────────────
# 选择器常量：写成四字码，写错一个字母就红
# ──────────────────────────────────────────────

def test_selectors_are_the_fourcc_they_claim():
    """常量必须与注释里的四字码一致 —— 同一批常量在 ``audio_state`` 那边也有同款门禁。

    这条为「``'inpu'`` vs ``'inpt'``」那次事故立的（详见 ``test_audio_guard``）：
    错的四字码照样有返回值，只是读的不是你以为的那个属性。
    """
    assert audio_mod._SEL_DEVICES == _cc("dev#")
    assert audio_mod._SEL_DEFAULT_IN == _cc("dIn ")
    assert audio_mod._SEL_DEFAULT_OUT == _cc("dOut")
    assert audio_mod._SEL_SOURCE == _cc("ssrc")
    assert audio_mod._SCOPE_OUTPUT == _cc("outp")


# ──────────────────────────────────────────────
# 比对：变没变、变了哪几类（纯逻辑）
# ──────────────────────────────────────────────

def test_identical_readings_report_no_change():
    assert _changed_kinds(_A, _A) == ()


def test_each_family_is_reported_separately():
    """三类变化各自可辨；且顺序固定（``devices`` → ``default_output`` → ``default_input``）。"""
    more_devices = _Fingerprint(default_output=83, default_input=85,
                                devices=(83, 85, 92, 99), output_source=_cc("hdpn"))
    other_input = _Fingerprint(default_output=83, default_input=92,
                               devices=(83, 85, 92), output_source=_cc("hdpn"))
    assert _changed_kinds(_A, more_devices) == ("devices",)
    assert _changed_kinds(_A, _B) == ("default_output",)
    assert _changed_kinds(_A, other_input) == ("default_input",)


def test_output_source_folds_into_default_output():
    """默认输出设备与它的数据源算**同一类**：插拔耳机时两者常一起变，而订阅方要做的
    动作只有一个（重读现场）。分成两类只会诱导它写分叉判据。"""
    only_source = _Fingerprint(default_output=83, default_input=85, devices=(83, 85, 92),
                               output_source=_cc("ispk"))
    assert _changed_kinds(_A, only_source) == ("default_output",)


def test_nothing_readable_is_a_stable_reading():
    """``None`` 也是一个**稳定**的读数：读不到不会自己制造出一次假变化。"""
    empty = _Fingerprint()
    assert _changed_kinds(empty, _Fingerprint()) == ()


def test_all_three_families_at_once():
    assert _changed_kinds(
        _A, _Fingerprint(default_output=92, default_input=92, devices=(92,),
                         output_source=_cc("ispk"))
    ) == ("devices", "default_output", "default_input")


# ──────────────────────────────────────────────
# 替身与装配
# ──────────────────────────────────────────────

class _FakeProbe:
    """探针替身：按预设序列作答，序列用完后**重复最后一个读数**。

    「重复最后一个」是有意的：这样「某一拍读失败」可以写成往序列里塞一个 ``None``
    （而 ``_FakeProbe`` 缺省就一直是 ``None``），不必另造一个会抛的探针。
    """

    def __init__(self, *readings):
        self._queue = list(readings)
        self._last = readings[-1] if readings else None
        self.reads = 0
        self.reason = ""

    def read(self):
        self.reads += 1
        if self._queue:
            self._last = self._queue.pop(0)
        return self._last

    def push(self, *readings) -> None:
        self._queue.extend(readings)


def _source(probe, *, watch_s: float = 0.05, settle_ms: int = 60):
    """造一个事件源 + 真事件总线 + 未启动的心跳，返回 ``(源, 心跳, 收到的载荷)``。

    心跳**不启动**（``Heartbeat`` 构造时只建定时器、不 start）：多数用例直接调
    ``_compare()`` 驱动，节律本身由「挂上了没有」那条单独验。这样比对逻辑与时钟解耦，
    断言不会随相位漂移。
    """
    events = EventBus()
    received: list = []
    events.subscribe(AUDIO_DEVICE_TOPIC, received.append)
    heartbeat = Heartbeat(beat_ms=10)
    src = AudioDeviceSource(events, heartbeat, probe=probe,
                            watch_s=watch_s, settle_ms=settle_ms)
    return src, heartbeat, received


def test_start_degrades_when_the_probe_reads_nothing():
    """读不到现场：``start()`` 返回 False、``active`` 为假、``close()`` 幂等不抛。

    降级而不是报错 —— 订阅方照常运行，只是收不到设备变化事件。
    """
    src, heartbeat, _received = _source(_FakeProbe())
    assert src.start() is False
    assert src.active is False
    assert heartbeat.ticker_count() == 0, "读不到现场就不该占心跳订阅"
    src.close()
    src.close()          # 幂等


def test_start_takes_a_baseline_and_hangs_on_the_framework_heartbeat():
    """成功路径：取一次基准读数 + 挂上**框架那一个**心跳（不新增时钟）。"""
    probe = _FakeProbe(_A)
    src, heartbeat, _received = _source(probe, watch_s=0.5)
    assert src.start() is True
    assert src.active is True
    assert probe.reads == 1, "基准读数只取一次"
    assert heartbeat.ticker_keys() == [audio_mod._TICKER_KEY]
    assert heartbeat.ticker_count() == 1


def test_start_is_idempotent():
    """重复 ``start()`` 不重复登记、也不再取一次基准（那会把「变化」吃掉）。"""
    probe = _FakeProbe(_A)
    src, heartbeat, _received = _source(probe)
    assert src.start() is True
    assert src.start() is True
    assert probe.reads == 1
    assert heartbeat.ticker_count() == 1


def test_close_removes_the_ticker_and_is_idempotent():
    src, heartbeat, _received = _source(_FakeProbe(_A))
    src.start()
    src.close()
    assert src.active is False
    assert heartbeat.ticker_count() == 0
    src.close()                       # 再摘一次不抛
    assert heartbeat.ticker_count() == 0


# ──────────────────────────────────────────────
# 投递：一次变化 = 一条事件，且等现场落定
# ──────────────────────────────────────────────

def test_change_publishes_one_event_with_its_kinds(qtbot):
    """变了 ⇒ 等稳定窗口 ⇒ 投一条事件，载荷报出变化类别。"""
    probe = _FakeProbe(_A)
    src, _heartbeat, received = _source(probe, settle_ms=40)
    src.start()

    probe.push(_B)
    src._compare()
    assert received == [], "稳定窗口内不许提前投递（此刻现场可能还没落定）"

    qtbot.waitUntil(lambda: bool(received), timeout=2000)
    assert len(received) == 1
    event = received[0]
    assert isinstance(event, AudioDeviceEvent)
    assert event.kinds == ("default_output",)
    assert event.at > 0


def test_no_change_publishes_nothing(qtbot):
    """控制组：同一份读数反复比对**一条都不许投** —— 证明上一条抓的是真东西。"""
    probe = _FakeProbe(_A)
    src, _heartbeat, received = _source(probe, settle_ms=40)
    src.start()
    for _ in range(3):
        src._compare()
    qtbot.wait(120)
    assert received == []


def test_several_changes_in_one_window_merge_into_one_event(qtbot):
    """一次插拔往往连着一串变化 ⇒ **合成一件事**报（订阅方要做的动作只有一种）。

    类别保序去重：``devices`` → ``default_output`` → ``default_input``，同类只出现一次。
    """
    probe = _FakeProbe(_A)
    src, _heartbeat, received = _source(probe, settle_ms=80)
    src.start()

    probe.push(_Fingerprint(default_output=83, default_input=85, devices=(83, 85, 92, 99),
                            output_source=1))
    src._compare()                                  # devices 变了
    probe.push(_B)
    src._compare()                                  # default_output 又变了
    probe.push(_B)
    src._compare()                                  # 没再变
    assert received == []

    qtbot.waitUntil(lambda: bool(received), timeout=2000)
    assert len(received) == 1, "一条插拔 = 一条事件"
    assert received[0].kinds == ("devices", "default_output")


def test_settle_window_restarts_on_each_change(qtbot):
    """稳定窗口的语义是「**等到不再变了**」：窗口内的后续变化把投递顺延（trailing）。

    与 ``Scheduler`` 的脏标记窗口刻意相反 —— 那边从首个标记起算（持续风暴不饿死点火），
    这边恰恰要等它停下来，否则订阅方读到的是识别跑了一半的现场。
    """
    probe = _FakeProbe(_A)
    src, _heartbeat, received = _source(probe, settle_ms=300)
    src.start()

    probe.push(_B)
    src._compare()
    qtbot.wait(180)                                 # 还没到 300ms
    assert received == []

    probe.push(_Fingerprint(default_output=83, default_input=92, devices=(83, 85, 92),
                            output_source=1))
    src._compare()                                  # 第二次变化：到期时刻顺延
    qtbot.wait(180)                                 # 距第一次 360ms、距第二次 180ms
    assert received == [], "第二次变化必须把到期时刻顺延，此刻仍未到"

    qtbot.waitUntil(lambda: bool(received), timeout=2000)
    assert received[0].kinds == ("default_output", "default_input"), "两轮变化归成一条"


def test_a_failed_read_does_not_move_the_baseline_or_fake_an_event(qtbot):
    """某一拍读失败 ⇒ **基准不动**、也不投事件；下一拍真变了照样发现。

    把「读失败」当成一次变化，订阅方会收到一条假事件；而把基准清成 ``None``，这条
    节律会从此**静默失效**（后续比对永远拿不出结论）—— 两种都不可接受。
    """
    probe = _FakeProbe(_A, None)
    src, _heartbeat, received = _source(probe, settle_ms=40)
    src.start()

    src._compare()                                  # 读到 None
    qtbot.wait(120)
    assert received == [], "读失败不是「设备变了」"

    probe.push(_B)
    src._compare()                                  # 与最初的基准比，仍算得出变化
    qtbot.waitUntil(lambda: bool(received), timeout=2000)
    assert received[0].kinds == ("default_output",), "基准必须还在，节律没死"


# ──────────────────────────────────────────────
# 摘除：close() 之后一条都不许再说话
# ──────────────────────────────────────────────

def test_close_drops_the_pending_event(qtbot):
    """窗口已开、还没到期就 ``close()`` ⇒ 那条事件**不许**发出去。

    不守这一条的表现是：用户刚禁用组件，一条「设备变了」还在飞 —— 面板上冒出一句
    没人认得的话。
    """
    probe = _FakeProbe(_A)
    src, _heartbeat, received = _source(probe, settle_ms=200)
    src.start()
    probe.push(_B)
    src._compare()

    src.close()
    src._emit()                                     # 在途那一条（close 停表后仍可能被叫到）
    qtbot.wait(400)
    assert received == []


def test_publish_failure_does_not_break_the_rhythm(qtbot):
    """投递失败（订阅方是别人的代码）不得掀翻心跳。"""
    class _AngryEvents:
        def publish(self, topic, payload):
            raise RuntimeError("订阅方炸了")

    heartbeat = Heartbeat(beat_ms=10)
    src = AudioDeviceSource(_AngryEvents(), heartbeat, probe=_FakeProbe(_A), settle_ms=10)
    src.start()
    src._emit()                                     # 不抛


# ──────────────────────────────────────────────
# 端到端：挂在真心跳上，插拔几秒内被发现
# ──────────────────────────────────────────────

def test_change_is_noticed_by_the_real_heartbeat(qtbot):
    """接上真心跳跑一遍：节律自己到期 ⇒ 读 ⇒ 比对 ⇒ 等窗口 ⇒ 投递。

    上面几条都是手动驱动 ``_compare()``（判据与时钟解耦），这一条专门守「挂上去真的会
    跳」——少它的话，「注册了但永不触发」这类静默失效在测试里看不出来。
    """
    probe = _FakeProbe(_A)
    src, heartbeat, received = _source(probe, watch_s=0.01, settle_ms=30)
    assert src.start() is True
    heartbeat.start()
    try:
        probe.push(_B)
        qtbot.waitUntil(lambda: bool(received), timeout=3000)
        assert received[0].kinds == ("default_output",)
    finally:
        heartbeat.stop()
        src.close()
