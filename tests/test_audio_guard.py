"""audio_guard 与通知能力的离线单测。

本期组件的范围是**只检测、只通知**（用户 2026-09-22 定的口径），所以这里要验的核心
只有两件事：

1. **判据对不对** —— 每一种现场（插着没插 / 识别跑到哪一步 / 默认设备漂没漂 / 静音没）
   映射到哪个取值。这一段是本组件全部的业务逻辑，也是真机上唯一要核的东西。
2. **通知与动作回程通不通** —— 出事弹一条、去重、用户点了按钮能派回发起它的插件。

全部离线：真件是 ``Config`` / ``PluginContext``；替身只出现在**系统边界**上
（CoreAudio 探针、AppKit、通知中心）——判据本身走真实路径。

分层：``audio_state`` 的常量与降级、``notify`` 中枢的纯逻辑、
``audio_guard.plugin`` 的取值分支、``shell.macos_notify`` 的按钮顺序契约。
**电源事件源的用例已迁到 ``test_core_power.py``** —— 它早就不只服务音频一家了。
"""

from __future__ import annotations

import struct

import pytest

from barkit.capabilities.notify import (
    MAX_ACTIONS,
    NotificationAction,
    NotificationHub,
    NotificationRequest,
    NotifyCapability,
)
from barkit.contribution.specs import IconName, StateLevel
from barkit.core.config import Config
from barkit.core.plugin import PluginContext
from barkit.plugins.audio_guard import audio_state
from barkit.plugins.audio_guard import plugin as plugin_mod
from barkit.shell.ui_kit import Switch


def _cc(fourcc: str) -> int:
    """四字码 → big-endian 整数（CoreAudio 选择器 / 作用域的写法）。"""
    return struct.unpack(">I", fourcc.encode("ascii"))[0]


# ══════════════════════════════════════════════════════════════════════
# 通知中枢（capabilities.notify，纯逻辑，零平台依赖）
# ══════════════════════════════════════════════════════════════════════

class _FakeBackend:
    """通知后端替身：记录收到的请求，并可手动触发「用户点了按钮」。"""

    def __init__(self, ok: bool = True, available: bool = True,
                 explode_on_probe: bool = False):
        self.ok = ok
        self._available = available
        self.explode_on_probe = explode_on_probe
        self.requests: list[NotificationRequest] = []
        self._on_action = None

    def available(self) -> bool:
        if self.explode_on_probe:
            raise RuntimeError("探活本身炸了")
        return self._available

    def send(self, request, on_action) -> bool:
        self.requests.append(request)
        self._on_action = on_action
        return self.ok

    def click(self, notification_key: str, action_key: str) -> None:
        """模拟用户在通知上点了某枚按钮。"""
        assert self._on_action is not None, "还没发过通知，哪来的回程"
        self._on_action(notification_key, action_key)


class _Receiver:
    """插件替身：只记下收到的动作名。"""

    def __init__(self, pid: str = "demo"):
        self.id = pid
        self.actions: list[str] = []

    def on_notification_action(self, key: str) -> None:
        self.actions.append(key)


def _hub(backend, receivers: dict | None = None):
    table = receivers or {}
    return NotificationHub(
        backends=[backend],
        resolve_plugin=lambda pid: table.get(pid),
    )


def test_hub_delivers_and_returns_true():
    backend = _FakeBackend()
    hub = _hub(backend)
    req = NotificationRequest(key="demo.a", title="出事", body="说明",
                              actions=(NotificationAction("fix", "修一下"),))
    assert hub.send("demo", req) is True
    assert backend.requests == [req]


def test_hub_dedups_same_key_same_content():
    """同 key 同内容只弹一次 —— 通知是「出事说一声」，不是每轮都喊。"""
    backend = _FakeBackend()
    hub = _hub(backend)
    req = NotificationRequest(key="demo.a", title="出事")
    assert hub.send("demo", req) is True
    assert hub.send("demo", req) is False          # 第二次被去重
    assert len(backend.requests) == 1


def test_hub_redelivers_when_content_changes():
    """内容变了（例如文案或按钮改了）⇒ 再弹一次。"""
    backend = _FakeBackend()
    hub = _hub(backend)
    hub.send("demo", NotificationRequest(key="demo.a", title="出事"))
    changed = NotificationRequest(key="demo.a", title="出事了，换了个说法")
    assert hub.send("demo", changed) is True
    assert len(backend.requests) == 2


def test_hub_rejects_missing_key_or_title():
    backend = _FakeBackend()
    hub = _hub(backend)
    assert hub.send("demo", NotificationRequest(key="", title="T")) is False
    assert hub.send("demo", NotificationRequest(key="k", title="")) is False
    assert backend.requests == []                  # 非法声明不落到后端


def test_hub_rejects_more_actions_than_macos_allows():
    """超过两枚按钮在**契约层**就拒 —— 别让第三枚静默消失在界面上。"""
    backend = _FakeBackend()
    hub = _hub(backend)
    over = NotificationRequest(
        key="k", title="T",
        actions=tuple(NotificationAction(f"a{i}", f"第{i}枚")
                      for i in range(MAX_ACTIONS + 1)),
    )
    assert hub.send("demo", over) is False
    assert backend.requests == []
    # 恰好到上限仍放行（控制组：证明拒的是「超一枚」而不是「两枚」）
    at_limit = NotificationRequest(
        key="k", title="T",
        actions=tuple(NotificationAction(f"a{i}", f"第{i}枚")
                      for i in range(MAX_ACTIONS)),
    )
    assert hub.send("demo", at_limit) is True


def test_hub_without_backend_returns_false():
    assert _hub(None).send("demo", NotificationRequest(key="k", title="T")) is False
    assert NotificationHub().send("demo", NotificationRequest(key="k", title="T")) is False


def test_hub_skips_backend_that_is_unavailable():
    down = _FakeBackend(available=False)
    up = _FakeBackend()
    hub = NotificationHub(backends=[down, up])
    assert hub.send("demo", NotificationRequest(key="k", title="T")) is True
    assert down.requests == [] and len(up.requests) == 1     # 顺序即优先级


def test_hub_isolates_backend_availability_probe_failure():
    """探活本身抛异常 ⇒ 跳过它、继续找下一个，不让发送路径翻车。"""
    broken = _FakeBackend(explode_on_probe=True)
    up = _FakeBackend()
    hub = NotificationHub(backends=[broken, up])
    assert hub.send("demo", NotificationRequest(key="k", title="T")) is True
    assert len(up.requests) == 1


def test_hub_returns_false_when_backend_send_fails():
    backend = _FakeBackend(ok=False)
    hub = _hub(backend)
    assert hub.send("demo", NotificationRequest(key="k", title="T")) is False


def test_hub_routes_click_back_to_the_originating_plugin():
    """用户点按钮 ⇒ 动作名派回**发起那条通知的**插件。"""
    backend = _FakeBackend()
    recv = _Receiver("demo")
    hub = _hub(backend, {"demo": recv})
    hub.send("demo", NotificationRequest(
        key="demo.a", title="T",
        actions=(NotificationAction("fix_output", "切回耳机"),)))

    backend.click("demo.a", "fix_output")
    assert recv.actions == ["fix_output"]


def test_hub_ignores_click_for_unknown_notification():
    """找不到归属（通知从没发过 / 已被遗忘）⇒ 静默丢弃，不抛。"""
    backend = _FakeBackend()
    hub = _hub(backend, {"demo": _Receiver("demo")})
    hub.send("demo", NotificationRequest(key="demo.a", title="T"))
    backend.click("someone-else", "fix")           # 不抛异常


def test_hub_ignores_click_when_plugin_offline():
    backend = _FakeBackend()
    hub = _hub(backend)                            # resolve 表为空 ⇒ 插件不在线
    hub.send("demo", NotificationRequest(key="demo.a", title="T",
                                         actions=(NotificationAction("f", "F"),)))
    backend.click("demo.a", "f")                   # 不抛异常


def test_hub_ignores_click_when_plugin_has_no_handler():
    class _Silent:
        id = "demo"

    backend = _FakeBackend()
    hub = _hub(backend, {"demo": _Silent()})
    hub.send("demo", NotificationRequest(key="demo.a", title="T",
                                         actions=(NotificationAction("f", "F"),)))
    backend.click("demo.a", "f")                   # 不抛异常


def test_hub_isolates_plugin_handler_exception():
    class _Boom(_Receiver):
        def on_notification_action(self, key):
            raise RuntimeError("插件处理炸了")

    backend = _FakeBackend()
    hub = _hub(backend, {"demo": _Boom("demo")})
    hub.send("demo", NotificationRequest(key="demo.a", title="T",
                                         actions=(NotificationAction("f", "F"),)))
    backend.click("demo.a", "f")                   # 不抛出（隔离）


def test_hub_forget_drops_route_and_dedup_memory():
    """插件卸载 ⇒ 归属清掉（但**不**牵连别的插件）。"""
    backend = _FakeBackend()
    hub = _hub(backend, {"demo": _Receiver("demo"), "other": _Receiver("other")})
    hub.send("demo", NotificationRequest(key="demo.a", title="T"))
    hub.send("other", NotificationRequest(key="other.a", title="T"))
    hub.forget("demo")

    backend.click("demo.a", "f")                   # 找不到发起者了，不抛
    # 去重记忆也清了：同一内容可以再次投递
    assert hub.send("demo", NotificationRequest(key="demo.a", title="T")) is True
    # 别的插件不受影响：内容没变，仍被自己的去重挡掉
    assert hub.send("other", NotificationRequest(key="other.a", title="T")) is False


# ---------- NotifyCapability mixin ----------

class _Notifier(NotifyCapability):
    id = "notifier"
    name = "Notifier"


def test_capability_notify_needs_a_hub():
    """框架没装中枢（测试桩 / 无界面进程）⇒ 返回 False，不抛。"""
    plugin = _Notifier(PluginContext(config=None, events=None))
    assert plugin.notify(NotificationRequest(key="k", title="T")) is False


def test_capability_notify_uses_its_own_id_for_attribution(tmp_path):
    backend = _FakeBackend()
    hub = _hub(backend)
    ctx = PluginContext(config=Config(user_path=tmp_path / "c.yaml"), events=None)
    ctx.notify = hub
    plugin = _Notifier(ctx)
    assert plugin.notify(NotificationRequest(key="notifier.a", title="T")) is True
    assert hub._routes["notifier.a"] == "notifier"   # 归属用的是插件自己的 id


def test_capability_notify_swallows_hub_failure(tmp_path):
    class _BoomHub:
        def send(self, plugin_id, request):
            raise RuntimeError("中枢炸了")

    ctx = PluginContext(config=Config(user_path=tmp_path / "c.yaml"), events=None)
    ctx.notify = _BoomHub()
    plugin = _Notifier(ctx)
    assert plugin.notify(NotificationRequest(key="k", title="T")) is False


def test_capability_default_action_handler_is_a_noop():
    """只发纯告知通知的组件不必覆写回程 —— 基类默认实现不许抛。"""
    plugin = _Notifier(PluginContext(config=None, events=None))
    assert plugin.on_notification_action("whatever") is None


# ══════════════════════════════════════════════════════════════════════
# audio_state：常量与降级（不碰真机硬件的部分）
# ══════════════════════════════════════════════════════════════════════

def test_coreaudio_constants_are_the_fourcc_they_claim():
    """常量必须与注释里的四字码一致。

    **这不是形式主义**：曾经把输入作用域写成 ``'inpu'``（正确是 ``'inpt'``），
    于是所有设备的「有没有输入流」都读成 False，采集方向整体静默失效 —— 而且不报错。
    这条门禁就是为那次 bug 立的：写错一个字母立刻红。
    """
    assert audio_state._SEL_DEVICES == _cc("dev#")
    assert audio_state._SEL_DEFAULT_IN == _cc("dIn ")
    assert audio_state._SEL_DEFAULT_OUT == _cc("dOut")
    assert audio_state._SEL_NAME == _cc("lnam")
    assert audio_state._SEL_UID == _cc("uid ")
    assert audio_state._SEL_SOURCE == _cc("ssrc")
    assert audio_state._SEL_VOLUME == _cc("volm")
    assert audio_state._SEL_MUTE == _cc("mute")
    assert audio_state._SCOPE_INPUT == _cc("inpt")
    assert audio_state._SCOPE_OUTPUT == _cc("outp")


def test_fourcc_decode_roundtrip_and_bad_input():
    assert audio_state._fourcc(_cc("hdpn")) == "hdpn"
    assert audio_state._fourcc(_cc("ispk")) == "ispk"
    assert audio_state._fourcc(0xFFFFFFFF) == ""      # 非 ASCII ⇒ 空串而不是抛


def test_probe_degrades_when_frameworks_unavailable(monkeypatch):
    """框架拿不到（非 macOS / 异常环境）⇒ 返回 ok=False + 理由，绝不抛。"""
    class _Dead:
        ready = False
        reason = "非 macOS"

    monkeypatch.setattr(audio_state, "_fw", lambda: _Dead())
    snap = audio_state.probe()
    assert snap.ok is False and snap.reason == "非 macOS"
    assert snap.devices == () and snap.codec.available is False


def test_write_ops_refuse_on_unavailable_frameworks(monkeypatch):
    """框架不可用时三个写操作一律拒绝 —— 宁可不改，也不假装改成功了。"""
    class _Dead:
        ready = False
        reason = "非 macOS"

    monkeypatch.setattr(audio_state, "_fw", lambda: _Dead())
    assert audio_state.set_default_input(audio_state.UID_HEADSET_IN) is False
    assert audio_state.set_default_output(audio_state.UID_HEADSET_OUT) is False
    assert audio_state.set_output_muted(False) is False


def test_snapshot_lookup_helpers():
    dev = audio_state.Device(dev_id=7, uid="u", name="n", has_input=False,
                             has_output=True)
    snap = audio_state.Snapshot(ok=True, devices=(dev,))
    assert snap.find("u") is dev
    assert snap.find("nope") is None
    assert snap.uids() == {"u"}


def test_available_matches_platform():
    import sys

    assert audio_state.available() is (sys.platform == "darwin")


# ══════════════════════════════════════════════════════════════════════
# audio_guard.plugin：取值判据（本组件全部的业务逻辑）
# ══════════════════════════════════════════════════════════════════════

def _dev(uid: str, dev_id: int, *, has_in: bool = False, has_out: bool = False):
    return audio_state.Device(dev_id=dev_id, uid=uid, name=uid,
                              has_input=has_in, has_output=has_out)


def _default_devices():
    """一台「耳机插着、录音回放齐活」的机器的设备清单。"""
    return (
        _dev(audio_state.UID_HEADSET_OUT, 10, has_out=True),
        _dev(audio_state.UID_HEADSET_IN, 11, has_in=True),
        _dev(audio_state.UID_SPEAKER, 12, has_out=True),
        _dev(audio_state.UID_INTERNAL_MIC, 13, has_in=True),
    )


def _snap(*, ok: bool = True, reason: str = "", codec_available: bool = True,
          hp: bool | None = True, mic: bool | None = True,
          confidence: str = "", out_uid: str | None = audio_state.UID_HEADSET_OUT,
          in_uid: str | None = audio_state.UID_HEADSET_IN,
          muted: bool | None = False, devices=None) -> audio_state.Snapshot:
    if devices is None:
        devices = _default_devices()
    codec = audio_state.CodecState(
        available=codec_available,
        hp_detect=hp if codec_available else None,
        mic_detect=mic if codec_available else None,
        confidence=confidence if codec_available else "",
    )
    return audio_state.Snapshot(
        ok=ok, reason=reason, devices=tuple(devices),
        default_input_uid=in_uid, default_output_uid=out_uid,
        output_source="hdpn", codec=codec, output_muted=muted, output_volume=0.5,
    )


class _Probe:
    """探针替身控制器：换现场、数调用次数。"""

    def __init__(self, monkeypatch):
        self.snapshot = _snap()
        self.probes = 0
        self.writes: list[tuple[str, object]] = []
        monkeypatch.setattr(audio_state, "probe", self.probe)

    def probe(self):
        self.probes += 1
        return self.snapshot

    def set(self, snapshot: audio_state.Snapshot) -> None:
        self.snapshot = snapshot


@pytest.fixture
def build(tmp_path, monkeypatch):
    """造插件：探针与三个写操作换成替身，其余（配置 / 状态机 / 通知）全走真件。"""
    probe = _Probe(monkeypatch)

    def _record(name):
        def _do(arg):
            probe.writes.append((name, arg))
            return True
        return _do

    monkeypatch.setattr(audio_state, "set_default_input", _record("input"))
    monkeypatch.setattr(audio_state, "set_default_output", _record("output"))
    monkeypatch.setattr(audio_state, "set_output_muted", _record("mute"))

    created: list = []

    def factory(config_extra: str = "", hub=None):
        user = tmp_path / "config.yaml"
        user.write_text(
            "plugins:\n  audio_guard:\n    enabled: true\n" + config_extra,
            encoding="utf-8",
        )
        ctx = PluginContext(config=Config(user_path=user), events=None)
        if hub is not None:
            ctx.notify = hub
        plugin = plugin_mod.Plugin(ctx)
        created.append(plugin)
        return plugin

    factory.probe = probe
    return factory


def test_probe_failure_is_reported_not_swallowed(build):
    """探针读不到现场 ⇒ 取值 probe_failed（**不是**静默当没事）。"""
    build.probe.set(audio_state.Snapshot(ok=False, reason="非 macOS"))
    plugin = build()
    report = plugin.tick()
    assert report.state == "probe_failed"
    assert "非 macOS" in report.summary


def test_no_headset_when_codec_says_socket_is_empty(build):
    build.probe.set(_snap(hp=False, mic=None))
    assert build().tick().state == "no_headset"


def test_no_headset_falls_back_to_device_list_when_codec_is_mute(build):
    """codec 不报这些字段（Intel 机型）⇒ 降级用「耳机设备还在不在」估。"""
    without = tuple(d for d in _default_devices()
                    if d.uid != audio_state.UID_HEADSET_OUT)
    build.probe.set(_snap(codec_available=False, devices=without))
    assert build().tick().state == "no_headset"

    build.probe.set(_snap(codec_available=False))      # 耳机设备在 ⇒ 继续往下判
    assert build().tick().state == "ok"


def test_detecting_while_jack_recognition_is_still_running(build):
    """识别没跑完先别下结论 —— 这正是「只听出耳机」那一刻的形态。

    真机上这一步约 0.4s（先认出耳机）、约 1.7s 才出终态，中间那段就该报「进行中」，
    而不是急着报「麦克风没被识别」把用户赶去拔插。
    """
    build.probe.set(_snap(mic=None, confidence="PartialDetect"))
    assert build().tick().state == "detecting"


def test_complete_final_confidence_is_not_treated_as_in_progress(build):
    """控制组：终态字符串跑了全程 ⇒ 不许停在 detecting。"""
    build.probe.set(_snap(confidence="CompleteFinal"))
    assert build().tick().state == "ok"


def test_mic_missing_is_the_headphone_only_case(build):
    """插孔认了耳机、没认麦克风 ⇒ 只能拔插，报 mic_missing（ERR）。"""
    build.probe.set(_snap(mic=False, in_uid=None))
    report = build().tick()
    assert report.state == "mic_missing"


def test_mic_missing_is_decided_before_output_drift(build):
    """优先级：麦克风没认出来这件事本身最要紧，别被「输出漂了」盖过去。"""
    build.probe.set(_snap(mic=False, out_uid=audio_state.UID_SPEAKER, in_uid=None))
    assert build().tick().state == "mic_missing"


def test_output_drifted_when_sound_goes_to_builtin_speaker(build):
    """耳机插着、默认输出却是内置扬声器 —— 用户感知就是「耳机没声音」。"""
    build.probe.set(_snap(out_uid=audio_state.UID_SPEAKER))
    assert build().tick().state == "output_drifted"


def test_bluetooth_output_is_not_a_fault(build):
    """默认输出是蓝牙耳机等 —— 那是用户的正当选择，不是故障。"""
    build.probe.set(_snap(out_uid="SomeBluetoothHeadset-1234"))
    assert build().tick().state == "ok"


def test_muted_is_reported_after_routing_is_checked(build):
    """路由对了但静音开着 ⇒ muted（优先级在 output_drifted 之后）。"""
    build.probe.set(_snap(muted=True))
    assert build().tick().state == "muted"


def test_input_drifted_when_default_input_points_at_builtin_mic(build):
    """麦克风是好的，只是没被选中 —— 这条软件能修。

    前提：耳机麦克风设备**确实在系统清单里**（否则切不过去）。
    """
    build.probe.set(_snap(in_uid=audio_state.UID_INTERNAL_MIC))
    assert build().tick().state == "input_drifted"


def test_input_absent_routes_to_mic_missing(build):
    """默认输入是内置麦、但耳机麦克风设备压根不在清单里 —— 不是「漂移可修」，
    而是「没认出来」，只能拔插。对应真机踩过的 case：codec 报 MicDetect=True、
    Confidence=CompleteFinal，BuiltInHeadphoneInputDevice 却始终不在设备里。

    旧逻辑会在这种局面下报 input_drifted（「已被识别」）、还给出必然失败的切换按钮。
    """
    devices = (
        _dev(audio_state.UID_HEADSET_OUT, 10, has_out=True),
        _dev(audio_state.UID_SPEAKER, 12, has_out=True),
        _dev(audio_state.UID_INTERNAL_MIC, 13, has_in=True),
    )
    build.probe.set(_snap(in_uid=audio_state.UID_INTERNAL_MIC, devices=devices))
    report = build().tick()
    assert report.state == "mic_missing"
    # 单一真源（决议 D18）：运行时不再硬编码文案，summary 留空、由状态机文本兜底。
    assert report.summary == ""


def test_external_input_is_not_a_fault(build):
    """外接声卡 / 蓝牙麦被用户选为默认输入 ⇒ 不干扰。"""
    build.probe.set(_snap(in_uid="SomeUsbMic-5678"))
    assert build().tick().state == "ok"


def test_all_good_is_ok(build):
    build.probe.set(_snap())
    report = build().tick()
    assert report.state == "ok"
    assert report.summary


def test_start_checks_the_socket_immediately(build):
    """启动即体检一次（同步）—— 首屏不许停在「尚未体检」。"""
    plugin = build()
    assert build.probe.probes == 0
    plugin.start()
    assert build.probe.probes == 1
    assert plugin.diagnostics_text().startswith("# 音频看护")


# ---------- 状态声明与它的自洽 ----------

def test_every_reported_state_is_declared(build):
    """运行期给的取值必须都在声明闭集里（决议 D13）——否则界面上会露成 ERR。"""
    declared = {s.id for s in plugin_mod.STATES}
    for snap, expected in (
        (_snap(ok=False, reason="x"), {"probe_failed"}),
        (_snap(hp=False), {"no_headset"}),
        (_snap(mic=None, confidence="PartialDetect"), {"detecting"}),
        (_snap(mic=False, in_uid=None), {"mic_missing"}),
        (_snap(out_uid=audio_state.UID_SPEAKER), {"output_drifted"}),
        (_snap(muted=True), {"muted"}),
        (_snap(in_uid=audio_state.UID_INTERNAL_MIC), {"input_drifted"}),
        # 默认输入是内置麦、但耳机麦克风设备不在清单里 ⇒ 不是「漂移可修」，归 mic_missing。
        (_snap(in_uid=audio_state.UID_INTERNAL_MIC,
               devices=(_dev(audio_state.UID_HEADSET_OUT, 10, has_out=True),
                        _dev(audio_state.UID_SPEAKER, 12, has_out=True),
                        _dev(audio_state.UID_INTERNAL_MIC, 13, has_in=True))),
         {"mic_missing"}),
        (_snap(), {"ok"}),
    ):
        build.probe.set(snap)
        state = build().tick().state
        assert state in declared and state in expected


def test_state_set_is_named_the_way_the_code_names_it():
    """``_ALERTS`` 的键必须是声明过的取值 —— 否则那条通知永远发不出去。"""
    declared = {s.id for s in plugin_mod.STATES}
    assert set(plugin_mod._ALERTS) <= declared


def test_recovery_states_do_not_alert():
    """「没事」和「没插耳机」不弹通知 —— 那不叫出事，弹了就是打扰。"""
    assert "ok" not in plugin_mod._ALERTS
    assert "no_headset" not in plugin_mod._ALERTS


def test_alert_levels_are_not_ok_level():
    """会弹通知的取值都不该声明成 OK 级别（否则颜色和提醒互相打架）。"""
    levels = {s.id: s.level for s in plugin_mod.STATES}
    for state in plugin_mod._ALERTS:
        assert levels[state] is not StateLevel.OK


def test_state_spec_is_event_driven_not_cycle_judged():
    """唤醒/启动点火，两次体检间隔几小时是常态 ⇒ 不设失联阈值。"""
    plugin = plugin_mod.Plugin(PluginContext(config=None, events=None))
    spec = plugin.state_specs()[0]
    assert spec.stale_after is None
    assert spec.icon is IconName.HEADSET
    assert plugin.component().icon is IconName.HEADSET
    assert len(plugin.row_specs()) == 1


# ---------- 通知与动作回程 ----------

def test_alert_fires_with_a_key_named_after_the_state(build):
    backend = _FakeBackend()
    build.probe.set(_snap(mic=False, in_uid=None))
    plugin = build(hub=_hub(backend))
    plugin.tick()
    assert [r.key for r in backend.requests] == ["audio_guard.mic_missing"]


def test_healthy_state_sends_nothing(build):
    backend = _FakeBackend()
    plugin = build(hub=_hub(backend))
    plugin.tick()
    assert backend.requests == []


def test_notification_is_deduped_across_cycles(build):
    """同一个毛病连着两轮 ⇒ 只弹一条（去重在中枢，组件不必自己记）。"""
    backend = _FakeBackend()
    build.probe.set(_snap(out_uid=audio_state.UID_SPEAKER))
    plugin = build(hub=_hub(backend))
    plugin.tick()
    plugin.tick()
    assert len(backend.requests) == 1


def test_notify_can_be_switched_off_without_stopping_detection(build):
    """关掉通知只是不弹；检测与面板状态照常。"""
    backend = _FakeBackend()
    build.probe.set(_snap(muted=True))
    plugin = build(config_extra="    notify_enabled: false\n", hub=_hub(backend))
    assert plugin.notify_enabled is False
    report = plugin.tick()
    assert report.state == "muted"                 # 检测没停
    assert backend.requests == []                  # 只是没弹


def test_notify_enabled_defaults_to_on(tmp_path):
    plugin = plugin_mod.Plugin(PluginContext(
        config=Config(user_path=tmp_path / "c.yaml"), events=None))
    assert plugin.notify_enabled is True           # 默认值单一真源 = config_schema


def test_setting_publishes_a_fresh_snapshot(build):
    published: list = []
    plugin = build()

    def _capture(snapshot=None, key="main"):
        published.append((snapshot, key))

    plugin.publish_snapshot = _capture                # 只关心「广播了一次」
    plugin.set_notify_enabled(False)
    assert plugin.notify_enabled is False
    assert published and published[0][1] == "main"
    plugin.set_notify_enabled(False)                 # 已是该值 ⇒ 不再广播
    assert len(published) == 1


@pytest.mark.parametrize("action_key,expected_write", [
    ("fix_input", "input"),
    ("fix_output", "output"),
    ("fix_mute", "mute"),
])
def test_fix_buttons_write_through_the_right_call(build, action_key, expected_write):
    """每枚修复按钮对应哪一个写操作 —— 点错了就是改错东西。"""
    plugin = build()
    plugin.on_notification_action(action_key)
    assert [name for name, _ in build.probe.writes] == [expected_write]


def test_fix_targets_the_headset_endpoints(build):
    """切默认设备必须指名耳机那两个 UID，不能靠「第几个设备」猜。"""
    plugin = build()
    plugin.on_notification_action("fix_input")
    plugin.on_notification_action("fix_output")
    assert build.probe.writes == [
        ("input", audio_state.UID_HEADSET_IN),
        ("output", audio_state.UID_HEADSET_OUT),
    ]


def test_fix_mute_unmutes_rather_than_toggles(build):
    """取消静音传的是 False（**不是**取反）—— 否则用户连点两下又静回去了。"""
    build().on_notification_action("fix_mute")
    assert build.probe.writes == [("mute", False)]


def test_every_alert_button_reaches_an_implementation(build):
    """声明里出现的每一枚按钮都必须有实现。

    这是「通知上画了按钮却点了没用」这类静默失效的门禁：判据是「点了之后**现场被
    重读了一遍**」——每一枚动作（含各条 fix_*）都要求重读，所以计数必然增加。
    往 ``_ALERTS`` 里加一枚新按钮而忘了写分支，这条立刻红。
    """
    for state, (_title, _body, actions) in plugin_mod._ALERTS.items():
        for action in actions:
            plugin = build()
            before = build.probe.probes
            plugin.on_notification_action(action.key)
            assert build.probe.probes > before, (
                f"取值 {state} 的按钮 {action.key} 点了没有任何反应（漏了实现分支）")


def test_unknown_action_does_nothing(build):
    """控制组：没声明的动作名不该触发任何重检 —— 证明上一条抓的是真东西。"""
    plugin = build()
    before = build.probe.probes
    plugin.on_notification_action("no_such_action")
    assert build.probe.probes == before
    assert build.probe.writes == []


def test_a_failing_fix_does_not_blow_up_the_action_thread(build, monkeypatch):
    """写失败（权限 / 设备不在了）⇒ 不抛；仍然复检一次，结果如实上报。"""
    def _boom(_arg):
        raise RuntimeError("写不进去")

    monkeypatch.setattr(audio_state, "set_output_muted", _boom)
    plugin = build()
    plugin.on_notification_action("fix_mute")      # 不抛出
    assert build.probe.probes == 1


def test_action_rereads_live_state_before_acting(build):
    """通知可能是几小时前发的 —— 点按钮时必须重读现场。

    构造：发通知时是「输出漂了」，用户点的时候这条已经被别的路径修好了。
    动作本身仍执行（它按 UID 写、幂等），但**结论取的是重读之后的现场**。
    """
    backend = _FakeBackend()
    build.probe.set(_snap(out_uid=audio_state.UID_SPEAKER))
    plugin = build(hub=_hub(backend))
    assert plugin.tick().state == "output_drifted"

    build.probe.set(_snap())                       # 现场已经好了
    plugin.on_notification_action("recheck")
    assert plugin._last.default_output_uid == audio_state.UID_HEADSET_OUT


# ---------- 事件订阅 ----------

def test_plugin_subscribes_to_wake_and_device_events(build):
    """三条订阅：前两条是「不用轮询」的前提（唤醒是高发时刻、设备变化补上运行中插拔），
    第三条（``app.panel_opened``）只为让面板那一行是此刻的事实 —— 本组件没有节拍，
    面板画的是最后一轮 tick 的结论，不订它就会在屏幕一直亮着时停在旧结论上。"""
    from barkit.core.audio import AUDIO_DEVICE_TOPIC
    from barkit.core.events import PANEL_OPENED_TOPIC
    from barkit.core.power import POWER_WAKE_TOPIC

    plugin = build()
    assert plugin.event_topics() == [POWER_WAKE_TOPIC, AUDIO_DEVICE_TOPIC, PANEL_OPENED_TOPIC]


def test_wake_event_only_marks_dirty(build, monkeypatch):
    """事件 handler 只登记意图（§8.2.2）——探针留给 tick，不占 UI 线程。"""
    from barkit.core.power import POWER_WAKE_TOPIC, PowerEvent

    logged: list = []
    plugin = build()
    monkeypatch.setattr(plugin.ctx, "mark_dirty",
                        lambda pid, reason: logged.append((pid, reason)), raising=False)

    plugin.on_system_event(POWER_WAKE_TOPIC, PowerEvent("wake", 1.0))
    assert build.probe.probes == 0                 # 没在 handler 里跑探针
    assert logged == [("audio_guard", "system_wake")]

    plugin.on_system_event("system.screen.lock", object())   # 别人的 topic
    assert len(logged) == 1                        # 不误认


def test_device_event_only_marks_dirty(build, monkeypatch):
    """设备变化走同一条路：只登记意图，探针留给 tick。

    这一条是「按需型也会被点火」的组件侧一半 —— 框架侧那一半在 ``Scheduler``
    （插上耳机要能被发现，靠的就是这一声）。
    """
    from barkit.core.audio import AUDIO_DEVICE_TOPIC, AudioDeviceEvent

    logged: list = []
    plugin = build()
    monkeypatch.setattr(plugin.ctx, "mark_dirty",
                        lambda pid, reason: logged.append((pid, reason)), raising=False)

    plugin.on_system_event(AUDIO_DEVICE_TOPIC, AudioDeviceEvent(("default_output",), 1.0))
    assert build.probe.probes == 0
    assert logged == [("audio_guard", "audio_device")]


def test_event_handler_survives_a_payload_it_does_not_understand(build, monkeypatch):
    """两条事件的载荷形状不同（唤醒是 ``kind``、设备变化是 ``kinds``）；handler 只记日志，
    故任一形状（乃至空载荷）都不该抛 —— 抛了会让这一次点火整个丢掉。"""
    from barkit.core.audio import AUDIO_DEVICE_TOPIC
    from barkit.core.power import POWER_WAKE_TOPIC

    logged: list = []
    plugin = build()
    monkeypatch.setattr(plugin.ctx, "mark_dirty",
                        lambda pid, reason: logged.append((pid, reason)), raising=False)

    plugin.on_system_event(AUDIO_DEVICE_TOPIC, object())     # 没有 kinds 属性
    plugin.on_system_event(AUDIO_DEVICE_TOPIC, None)         # 连载荷都没有
    assert [r for _pid, r in logged] == ["audio_device", "audio_device"]
    plugin.on_system_event(POWER_WAKE_TOPIC, None)
    assert [r for _pid, r in logged][-1] == "system_wake"


def test_event_handler_never_touches_the_probe(build, monkeypatch):
    """mark_dirty 未接线（测试桩 / 无调度器）时也必须静默 —— 不许炸。"""
    from barkit.core.audio import AUDIO_DEVICE_TOPIC
    from barkit.core.power import POWER_WAKE_TOPIC, PowerEvent

    plugin = build()
    plugin.ctx.mark_dirty = None
    plugin.on_system_event(POWER_WAKE_TOPIC, PowerEvent("wake", 1.0))
    plugin.on_system_event(AUDIO_DEVICE_TOPIC, object())
    assert build.probe.probes == 0


def test_no_pace_declared_so_the_settings_page_has_no_interval_card(build):
    """按需型：设置页不画「运行间隔」那一卡（准入判据是两个纯数据，见 ``test_settings``）。

    两个前提缺一不可 —— ``run_mode`` 是按需，且**别声明** ``suggested_poll``：声明了它
    虽不生效（框架只给周期型挂节拍），但会让「这个组件到底有没有节拍」在代码里自相矛盾
    （D21 只给周期型填那一格）。
    """
    from barkit.core.plugin import RUN_ON_DEMAND

    cls = plugin_mod.Plugin
    assert cls.run_mode == RUN_ON_DEMAND
    assert getattr(cls, "suggested_poll", None) is None, "按需型别填建议周期（D21）"
    # 设置窗的准入是「PluginInfo.poll_default 非空」，而它只在 run_mode 是周期型时才算值
    # ⇒ 上一行成立即「不长那张卡」。那条判据本身在 test_plugin_manager 里有正反两组用例。
    assert build().event_topics(), "不轮询的前提是那两条事件订阅真的声明了"


# ---------- 诊断文本 ----------

def test_diagnostics_lists_the_raw_criteria(build):
    """本期只检测，所以「检测对不对」就是全部 —— 明细要列**原始判据**，不只给结论。"""
    build.probe.set(_snap(out_uid=audio_state.UID_SPEAKER))
    plugin = build()
    plugin.start()
    text = plugin.diagnostics_text()
    assert "output_drifted" in text                 # 结论
    assert "HPDetect" in text and "MicDetect" in text
    assert "识别进度" in text and "Confidence" in text
    assert audio_state.UID_SPEAKER in text          # 默认设备逐条列出
    assert audio_state.UID_HEADSET_OUT in text      # 设备清单也在


def test_diagnostics_before_first_check(build):
    text = build().diagnostics_text()
    assert "尚未体检" in text


def test_diagnostics_reports_degraded_probe(build):
    build.probe.set(audio_state.Snapshot(ok=False, reason="非 macOS"))
    plugin = build()
    plugin.start()
    assert "探针不可用：非 macOS" in plugin.diagnostics_text()


def test_diagnostics_are_read_only(build):
    """诊断在 UI 线程被调用（用户点「复制诊断」）—— 不许顺手改系统状态。"""
    plugin = build()
    plugin.start()
    plugin.diagnostics_text()
    assert build.probe.writes == []


def test_plugin_declares_no_external_resources(build):
    """纯只读组件：不落盘、不起 helper —— 声明空清单，框架别替它建路径。"""
    resources = build().resources()
    assert not resources.data_files and not resources.helper_tasks
    assert not resources.system_mutations and not resources.root_binaries


# ══════════════════════════════════════════════════════════════════════
# macOS 通知后端（shell.macos_notify）：按钮顺序与回程翻译
#
# 这一层的逻辑题只有三道：往哪个槽放哪枚按钮、点击怎么翻译回动作名、
# 给进程补 bundle 身份时**只补不覆盖**。AppKit 与通知中心整体换替身。
# ══════════════════════════════════════════════════════════════════════

_MISSING = object()   # 「未指定」与「显式 None」的区分哨兵


class _FakeNSObject:
    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self


class _FakeObjC:
    @staticmethod
    def lookUpClass(_name):
        return _FakeNSObject


def _fake_bundle(info):
    class _Bundle:
        @staticmethod
        def mainBundle():
            return _Bundle

        @staticmethod
        def infoDictionary():
            return info

    return _Bundle


class _FakeNotification:
    """``NSUserNotification`` 替身：把 setter 调用**按序**录下来 —— 顺序就是契约。"""

    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self

    def setTitle_(self, v):
        self.calls.append(("title", v))

    def setInformativeText_(self, v):
        self.calls.append(("body", v))

    def setHasActionButton_(self, v):
        self.calls.append(("has_action", v))

    def setActionButtonTitle_(self, v):
        self.calls.append(("action", v))

    def setOtherButtonTitle_(self, v):
        self.calls.append(("other", v))

    def setUserInfo_(self, v):
        self.calls.append(("userinfo", dict(v)))


class _FakeCenter:
    def __init__(self):
        self.delegate = None
        self.delivered: list = []

    @classmethod
    def defaultUserNotificationCenter(cls):
        return cls()

    def setDelegate_(self, delegate):
        self.delegate = delegate

    def deliverNotification_(self, note):
        self.delivered.append(note)


class _FakeNote:
    """通知中心回调时交给 delegate 的那个 note。"""

    def __init__(self, activation_type, info):
        self._kind = activation_type
        self._info = info

    def activationType(self):
        return self._kind

    def userInfo(self):
        return self._info


@pytest.fixture
def backend(qtbot):
    """造后端并把「系统面」整体换成替身（AppKit / 通知中心），逻辑照真跑。"""
    from barkit.shell import macos_notify as mn

    def factory(info=_MISSING, center=_MISSING, notification=_FakeNotification):
        b = mn.MacNotifyBackend()
        b._api = (_FakeObjC,
                  _fake_bundle({} if info is _MISSING else info),
                  notification, _FakeCenter)
        b._center = _FakeCenter() if center is _MISSING else center
        return b

    return factory


def test_backend_unavailable_without_pyobjc(monkeypatch):
    """非 macOS / 没装 PyObjC ⇒ 自报不可用、发送直接 False，不抛。"""
    from barkit.shell import macos_notify as mn

    monkeypatch.setattr(mn, "_load_appkit", lambda: None)
    b = mn.MacNotifyBackend()
    assert b.available() is False
    assert b.send(NotificationRequest(key="k", title="T"), lambda *_: None) is False


def test_backend_unavailable_without_an_info_dictionary(backend):
    """拿不到 ``infoDictionary`` ⇒ 给不了通知归属身份，投了也是白投（实测）⇒ 不可用。"""
    b = backend(info=None, center=None)
    assert b.available() is False


def test_backend_available_and_caches_the_notification_center(backend):
    b = backend()
    assert b.available() is True
    assert b._center is not None
    first = b._center
    assert b.available() is True
    assert b._center is first                 # 拿过一次就留着，不每轮重新取


def test_available_establishes_identity_before_center(backend):
    """从源码运行（``org.python.python``）时，available() 必须**先**补身份再取通知中心——
    否则中心缓存旧身份，之后改写 ``CFBundleIdentifier`` 已无效，系统静默丢弃通知
    （2026-09-23 修：原先中心在 available() 里先被取走，身份改写晚到一步）。"""
    from barkit.shell.macos_notify import BUNDLE_ID

    b = backend(center=None)
    b._api = (_FakeObjC, _fake_bundle({"CFBundleIdentifier": "org.python.python"}),
              _FakeNotification, _FakeCenter)
    assert b.available() is True
    assert b._api[1].infoDictionary()["CFBundleIdentifier"] == BUNDLE_ID
    assert b._center is not None


def test_send_delivers_synchronously_on_the_main_thread(backend):
    b = backend()
    request = NotificationRequest(key="k", title="T",
                                 actions=(NotificationAction("a", "A"),))
    assert b.send(request, lambda *_: None) is True
    assert len(b._center.delivered) == 1


def test_two_buttons_land_on_action_then_other(backend):
    """**顺序即契约**：第一枚走 ``actionButton``、第二枚走 ``otherButton``。

    ``notify.MAX_ACTIONS`` 说的「两枚」正是这两个槽；放反了用户看到的按钮就换位，
    而回程是按索引翻译的 ⇒ 点「切回耳机」会去取消静音。这条把顺序钉住。
    """
    b = backend()
    request = NotificationRequest(
        key="demo.k", title="标题", body="正文",
        actions=(NotificationAction("fix_output", "切回耳机"),
                 NotificationAction("recheck", "重新检测")),
    )
    assert b._deliver(request) is True

    note = b._center.delivered[0]
    assert ("has_action", True) in note.calls
    assert [k for k, _ in note.calls if k in ("action", "other")] == ["action", "other"]
    assert ("action", "切回耳机") in note.calls
    assert ("other", "重新检测") in note.calls
    assert b._pending["demo.k"] == ("标题", ["fix_output", "recheck"])


def test_notification_key_travels_in_user_info(backend):
    """``NSUserNotification`` 没有可写的 identifier ⇒ 归属 key 必须走 ``userInfo``，
    回程从那里读回来。这条是「点了按钮找不到是谁发的」那类 bug 的守门。"""
    b = backend()
    b._deliver(NotificationRequest(key="demo.k", title="T"))
    note = b._center.delivered[0]
    assert ("userinfo", {"bk_key": "demo.k"}) in note.calls


def test_body_is_omitted_when_empty(backend):
    b = backend()
    b._deliver(NotificationRequest(key="k", title="T", body=""))
    note = b._center.delivered[0]
    assert [k for k, _ in note.calls if k == "body"] == []


def test_no_actions_switches_the_button_off(backend):
    """纯告知通知不该留一枚空按钮（点上去什么也不会发生）。"""
    b = backend()
    b._deliver(NotificationRequest(key="k", title="T"))
    note = b._center.delivered[0]
    assert ("has_action", False) in note.calls
    assert [k for k, _ in note.calls if k in ("action", "other")] == []


def test_deliver_refuses_when_identity_cannot_be_set(backend):
    """身份补不上就别投 —— 投了系统静默吞掉，用户以为组件没报（实测）。"""
    b = backend(info=None, center=_FakeCenter())
    assert b._deliver(NotificationRequest(key="k", title="T")) is False
    assert b._center.delivered == []


def test_delivery_failure_is_reported_not_raised(backend):
    class _AngryCenter(_FakeCenter):
        def deliverNotification_(self, note):
            raise RuntimeError("投递炸了")

    b = backend(center=_AngryCenter())
    assert b._deliver(NotificationRequest(key="k", title="T")) is False


def test_delegate_created_once_and_kept_alive(backend):
    """delegate 必须被持引用 —— 否则被 GC 掉、回调再也不来。"""
    b = backend()
    b._deliver(NotificationRequest(key="a", title="T"))
    first = b._delegate
    assert first is not None
    assert b._center.delegate is first
    b._deliver(NotificationRequest(key="b", title="T"))
    assert b._delegate is first


def test_bundle_identity_spoofed_only_for_python_default(backend):
    """纯 Python 进程的 ``org.python.python`` 身份**投递不出去**（实测）⇒ 补一个。"""
    from barkit.paths import APP_NAME
    from barkit.shell.macos_notify import BUNDLE_ID

    b = backend()
    b._api = (_FakeObjC,
              _fake_bundle({"CFBundleIdentifier": "org.python.python"}),
              _FakeNotification, _FakeCenter)
    assert b._ensure_bundle_identity() is True
    info = b._api[1].infoDictionary()
    assert info["CFBundleIdentifier"] == BUNDLE_ID
    assert info["CFBundleName"] == APP_NAME


def test_bundle_identity_spoofed_when_missing(backend):
    """连身份都没有（空 info）⇒ 也算「没有自己的身份」，同样补上。"""
    from barkit.shell.macos_notify import BUNDLE_ID

    b = backend()
    b._api = (_FakeObjC, _fake_bundle({}), _FakeNotification, _FakeCenter)
    assert b._ensure_bundle_identity() is True
    assert b._api[1].infoDictionary()["CFBundleIdentifier"] == BUNDLE_ID


def test_bundle_identity_is_not_overwritten_when_already_ours(backend):
    """已打包成 .app （自带非 Python 身份）⇒ 一概不动它。"""
    b = backend()
    info = {"CFBundleIdentifier": "com.acme.shipped"}
    b._api = (_FakeObjC, _fake_bundle(info), _FakeNotification, _FakeCenter)
    assert b._ensure_bundle_identity() is True
    assert info == {"CFBundleIdentifier": "com.acme.shipped"}     # 一字未改
    assert b._identity_fixed is True


def test_bundle_identity_fails_without_info_dictionary(backend):
    b = backend()
    b._api = (_FakeObjC, _fake_bundle(None), _FakeNotification, _FakeCenter)
    assert b._ensure_bundle_identity() is False


def test_bundle_identity_is_only_settled_once(backend):
    """补过一次就不再算 —— 也**不再去看**（后面的调用不读 info）。"""
    b = backend()
    assert b._ensure_bundle_identity() is True
    b._api = (_FakeObjC, _fake_bundle(None), _FakeNotification, _FakeCenter)
    assert b._ensure_bundle_identity() is True                   # 命中缓存


def test_activation_translates_buttons_to_action_keys(backend):
    """``activationType`` → 动作名：2 是第一枚、4 是第二枚、1 是点通知本体（不算动作）。"""
    b = backend()
    seen: list = []
    b._on_action = lambda key, action: seen.append((key, action))
    b._pending["demo.k"] = ("T", ["fix_output", "recheck"])

    b._handle_activation(_FakeNote(2, {"bk_key": "demo.k"}))
    assert seen == [("demo.k", "fix_output")]

    b._handle_activation(_FakeNote(4, {"bk_key": "demo.k"}))
    assert seen[-1] == ("demo.k", "recheck")


def test_clicking_the_notification_body_is_not_an_action(backend):
    b = backend()
    seen: list = []
    b._on_action = lambda key, action: seen.append(action)
    b._pending["demo.k"] = ("T", ["fix_output"])
    b._handle_activation(_FakeNote(1, {"bk_key": "demo.k"}))
    assert seen == []


def test_unknown_activation_type_is_ignored(backend):
    b = backend()
    seen: list = []
    b._on_action = lambda key, action: seen.append(action)
    b._pending["demo.k"] = ("T", ["fix_output", "recheck"])
    b._handle_activation(_FakeNote(7, {"bk_key": "demo.k"}))
    assert seen == []


def test_activation_without_a_recognisable_key_is_ignored(backend):
    """userInfo 缺失 / 里没有归属键 ⇒ 认不出是哪一条，丢弃而不是猜。"""
    b = backend()
    seen: list = []
    b._on_action = lambda key, action: seen.append(action)
    b._handle_activation(_FakeNote(2, None))
    b._handle_activation(_FakeNote(2, {}))
    b._handle_activation(_FakeNote(2, {"bk_key": ""}))
    assert seen == []


def test_activation_for_a_superseded_notification_is_ignored(backend):
    """该 key 已被更新的通知覆盖（或从没发过）⇒ 没有对应动作表，丢弃。"""
    b = backend()
    seen: list = []
    b._on_action = lambda key, action: seen.append(action)
    b._handle_activation(_FakeNote(2, {"bk_key": "never-sent"}))
    assert seen == []


def test_second_button_on_a_one_button_notification_is_ignored(backend):
    """只有一枚按钮的通知收到「第二枚」的激活 ⇒ 越界，丢弃（不许 IndexError）。"""
    b = backend()
    seen: list = []
    b._on_action = lambda key, action: seen.append(action)
    b._pending["demo.k"] = ("T", ["only_one"])
    b._handle_activation(_FakeNote(4, {"bk_key": "demo.k"}))
    assert seen == []


def test_activation_is_safe_when_no_action_callback_is_wired(backend):
    b = backend()
    b._pending["demo.k"] = ("T", ["fix_output"])
    b._handle_activation(_FakeNote(2, {"bk_key": "demo.k"}))     # 不抛


def test_activation_of_a_broken_notification_is_ignored(backend):
    class _Broken:
        def activationType(self):
            raise RuntimeError("读不出来")

        def userInfo(self):
            return {}

    b = backend()
    b._handle_activation(_Broken())                              # 不抛


# ══════════════════════════════════════════════════════════════════════
# 设置页（plugins/audio_guard/settings.py）
# ══════════════════════════════════════════════════════════════════════

def test_settings_page_is_declared_with_a_reason(build):
    """组件自己声明了设置页：``build`` 可调用 ＋ ``reason`` 说清为什么必须自绘。

    声明页不是为了「有个页」—— 没有它，框架要注入的状态机段与运行间隔都没挂点
    （2026-09-22 用户上报）。本组件那个唯一的配置项也必须靠它才有地方改。
    """
    spec = build().settings_page()
    assert spec is not None
    assert callable(spec.build)
    assert spec.reason.strip(), "自绘必须写明理由（门禁 test_custom_page_has_reason）"


def test_settings_page_exposes_the_notification_switch(build, qtbot):
    """页面真建得出来，且开关的初值来自组件的真实配置（界面不另读一份）。"""
    plugin = build(config_extra="    notify_enabled: false\n")
    page = plugin.settings_page().build()
    qtbot.add_widget(page)

    sw = page.findChild(Switch)
    assert sw is not None, "页里必须有那个开关"
    assert sw.isChecked() is False, "初值要跟着配置走"


def test_clicking_the_switch_writes_through_the_plugin(build, qtbot):
    """点一下 = 经插件方法落盘（界面不碰 config，门禁 ``test_no_config_io_in_ui``）。

    ``Switch.clicked`` **不翻转自身**，故这里验的是「推目标态 → 落盘 → 回读」整条路：
    两下之后开关与配置必须都在原位，而不是各说各话。
    """
    plugin = build()
    page = plugin.settings_page().build()
    qtbot.add_widget(page)
    sw = page.findChild(Switch)

    sw.clicked.emit()
    assert plugin.notify_enabled is False
    assert sw.isChecked() is False, "开关要跟落盘后的真实值一致"

    sw.clicked.emit()
    assert plugin.notify_enabled is True
    assert sw.isChecked() is True


def test_panel_opened_only_marks_dirty_and_never_touches_the_hardware(build, monkeypatch):
    """开面板 ⇒ 只登记意图；随后那一轮 tick 只重读现状、**不写任何系统设置**。

    与唤醒 / 设备变化同样的纪律（handler 不读现场），区别只在**理由**：它不判故障，
    纯粹是「用户此刻在看，别让他看到上一轮的结论」。本组件已经有两档确定解法的修复
    （默认输入 / 输出漂移、输出静音），但那些**只从通知按钮进**——开面板这一下必须
    是纯只读的，否则「看一眼」就变成了「顺手改用户的默认设备」。
    """
    from barkit.core.events import PANEL_OPENED_TOPIC

    logged: list = []
    plugin = build()
    monkeypatch.setattr(plugin.ctx, "mark_dirty",
                        lambda pid, reason: logged.append((pid, reason)), raising=False)

    plugin.on_system_event(PANEL_OPENED_TOPIC, None)

    assert build.probe.probes == 0, "handler 里跑了探针 —— 主线程上不许做 IO"
    assert logged == [("audio_guard", "panel_opened")], "被当成唤醒那条分支了"

    build.probe.set(_snap(hp=False, mic=None))
    assert plugin.tick().state == "no_headset"     # 面板那一行拿到的是此刻的事实
    assert build.probe.writes == [], "开个面板就把用户的默认设备改了"
