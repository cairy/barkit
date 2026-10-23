"""bt_guard 的离线单测。

这个组件解决的是「合盖 / 关屏再唤醒之后，蓝牙鼠标偶发连不上」，而它的判据**不是单点
读数**，是**跨睡眠边界的两个点的比对**：

========================  ================  ==========================
睡前                      醒后              结论
========================  ================  ==========================
在                        在                正常
在                        不在              **掉线** ⇒ 动手重开蓝牙
不在                      不在              用户自己关了鼠标 ⇒ 不动手
不在                      在                它自己回来了 ⇒ 正常
（没记到）                不在              见「降级路径」
========================  ================  ==========================

所以本文件的重心也在这里：**「睡前不在 ＋ 醒后不在」与「睡前在 ＋ 醒后不在」必须走出
相反的动作**。单点读「醒后不在」永远分不出这两者，而它们的正确处置刚好相反 —— 一个是
动手，一个是别去动用户自己关掉的设备。

**「醒后不在」还要先过一道耐心**（2026-10-01 加）：刚醒那几秒设备常还没枚举回来，所以取
「醒后」那份读数之前，会先在一段耐心窗里重探到**它出现**为止（上限 ``WAKE_HID_PATIENCE_S``）。
不等就动手会白断一次正连着的设备 —— 09-30 现场就是这么白断了 AirPods（14:05:00 唤醒后
3 毫秒就重开、14:05:06 才见到设备）。这条不改上面表格里任何一格，只是把「醒后」那一格的
读数取在「设备已经有机会回来」之后。

**边界有两族**（系统与显示器），四个方向全订：本机日常那次「关屏 / 亮回来」只发显示器
那一族，只订系统那两条的组件在这台机器上除启动那轮外**永远不会跑**（这是本组件第一次
上线时的实际故障）。两族在真合盖时会两两同时来，所以下面还有两条专门盯住「一次唤醒只
动一次手」与「同族里先到的那条读数才算基线」。

分层（与 `test_audio_guard.py` 同口径：替身只出现在**系统边界**上，判据走真实路径）：

1. ``bt_state`` 的**两层现场解析** —— 喂真实的命令输出格式（夹具照抄本机实测值），
   走真解析；被替掉的是「跑哪个命令」这一步；
2. ``bt_state`` 的**助手会话** —— 握手对契约版本、``req_id`` 配对、节点用完即删、
   会话死在两条命令之间要重建、可执行位自愈、断电重开的最终态保证（这几条都真跑一个
   对端进程，不是打桩：协议的错只有真对端才照得出来）；
3. ``plugin`` 的**判定与动作闸** —— 上面那张表逐格验，外加冷却 / 手动 / 降级；
4. 事件纪律（handler 只登记意图）与**设置页**（下拉填设备、开关写回）。
"""

from __future__ import annotations

import itertools
import json
import plistlib
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from barkit import paths
from barkit.capabilities.helper import protocol
from barkit.capabilities.helper.protocol import FrameReader
from barkit.capabilities.notify import NotificationRequest
from barkit.contribution.specs import IconName, StateLevel
from barkit.core.config import Config
from barkit.core.events import PANEL_OPENED_TOPIC
from barkit.core.plugin import FAIL_LOG, RUN_ON_DEMAND, PluginContext
from barkit.core.power import (
    POWER_DISPLAY_SLEEP_TOPIC,
    POWER_DISPLAY_WAKE_TOPIC,
    POWER_SLEEP_TOPIC,
    POWER_WAKE_TOPIC,
)
from barkit.plugins.bt_guard import bt_session, bt_state
from barkit.plugins.bt_guard import plugin as plugin_mod
from barkit.shell.ui_kit import ComboField, Switch

# 两个地址（**形状**照抄真实产物，地址本身是假值，用 mnemonic 段）：
#   ``system_profiler`` 给 ``AA:BB:CC:DD:EE:01``（带冒号、大写）
#   ``ioreg`` 给           ``aa-bb-cc-dd-ee-01``（带短横、小写）
# 两者是**同一只设备** —— 这正是必须走 ``norm()`` 归一化的原因，也是本文件要钉住的一条。
MOUSE = "AA:BB:CC:DD:EE:01"
MOUSE_N = "AABBCCDDEE01"
PAD = "AA:BB:CC:DD:EE:00"
PAD_N = "AABBCCDDEE00"


# ══════════════════════════════════════════════════════════════════════
# 一、两层现场的解析（真解析、假命令）
# ══════════════════════════════════════════════════════════════════════

def _sysprof(connected=(), not_connected=(), controller="attrib_on") -> bytes:
    """``system_profiler SPBluetoothDataType -json`` 的产物（照真实结构）。"""
    def group(pairs):
        return [{name: {"device_address": addr}} for name, addr in pairs]

    return json.dumps({"SPBluetoothDataType": [{
        "controller_properties": {"controller_state": controller},
        "device_connected": group(connected),
        "device_not_connected": group(not_connected),
    }]}).encode("utf-8")


def _ioreg(entries) -> bytes:
    """``ioreg -a -c IOHIDDevice`` 的产物：**plist 不是 JSON**（见 bt_state 的注释）。"""
    return plistlib.dumps([
        {"Transport": transport, **props} for transport, props in entries
    ])


class _Layers:
    """两层现场的命令替身：真解析、假读数。

    只替掉「跑哪条命令」这一步 —— 解析、归一化、判据全是生产代码。
    """

    def __init__(self, monkeypatch):
        self.sysprof = _sysprof()
        self.ioreg = _ioreg([])
        self.commands: list[str] = []
        monkeypatch.setattr(bt_state, "_run", self._run)

    def _run(self, argv, timeout_s=None):
        self.commands.append(argv[0])
        if argv[0] == bt_state.SYSPROF:
            return self.sysprof
        if argv[0] == bt_state.IOREG:
            return self.ioreg
        raise AssertionError(f"没预料到的命令：{argv}")


def test_bluetooth_layer_normalizes_addresses(monkeypatch):
    """蓝牙层：地址归一化 ＋ 已连 / 未连分组（分组决定 ``connected``）。"""
    layers = _Layers(monkeypatch)
    layers.sysprof = _sysprof(connected=[("MX Master 3S", MOUSE)],
                              not_connected=[("我的 iPad", PAD)])

    devices, controller = bt_state.bluetooth_layer()
    assert set(devices) == {MOUSE_N, PAD_N}
    assert devices[MOUSE_N] == {"name": "MX Master 3S", "connected": True}
    assert devices[PAD_N] == {"name": "我的 iPad", "connected": False}
    assert controller["controller_state"] == "attrib_on"


def test_hid_layer_normalizes_and_drops_non_bluetooth(monkeypatch):
    """HID 层：只收蓝牙传输的，且地址要与蓝牙层**对得上**（归一化后同键）。"""
    layers = _Layers(monkeypatch)
    layers.ioreg = _ioreg([
        ("Bluetooth", {"DeviceAddress": "aa-bb-cc-dd-ee-01", "Product": "ATK A9 SE"}),
        ("USB", {"DeviceAddress": "00-00-00-00-00-01", "Product": "有线键盘"}),
        ("Bluetooth", {"Product": "没地址的那台"}),      # 收不了，也不该崩
    ])

    assert bt_state.hid_layer() == {MOUSE_N: "ATK A9 SE"}
    # 两层给的是同一种格式 ⇒ 跨接口比对不会「把同一只设备当成两台」
    assert MOUSE_N in bt_state.hid_layer()
    assert bt_state.norm(MOUSE) == MOUSE_N


def test_probe_is_not_ok_only_when_nothing_was_read(monkeypatch):
    """``ok=False`` 说的是「根本没读到」，**不是**「读到了、设备不在」—— 两者取值不同。"""
    layers = _Layers(monkeypatch)
    layers.sysprof = None
    layers.ioreg = None
    snap = bt_state.probe()
    assert snap.ok is False
    assert snap.reason, "读不到必须留下原因（面板会把这句话上屏）"

    # 命令通了、只是一台设备都没有 ⇒ 仍是 ok=True，只是清单空
    layers.sysprof = _sysprof()
    assert bt_state.probe().ok is True


def test_probe_reads_the_controller_state_and_refuses_to_guess(monkeypatch):
    """控制器开关读到什么报什么；认不出的值报「读不出状态」，不猜。"""
    layers = _Layers(monkeypatch)
    layers.sysprof = _sysprof(controller="attrib_off")
    assert bt_state.probe().controller_on is False

    layers.sysprof = _sysprof(controller="something_else")
    assert bt_state.probe().controller_on is None


def test_paired_devices_lists_what_the_picker_needs(monkeypatch):
    """已配对清单：设置页那个下拉的数据源（地址归一化 ＋ 名字）。"""
    layers = _Layers(monkeypatch)
    layers.sysprof = _sysprof(connected=[("MX Master 3S", MOUSE)],
                              not_connected=[("我的 iPad", PAD)])
    assert bt_state.paired_devices() == [(PAD_N, "我的 iPad"), (MOUSE_N, "MX Master 3S")]


def test_pretty_renders_colons_and_passes_unparsable_through():
    assert bt_state.pretty(MOUSE_N) == MOUSE
    assert bt_state.pretty("") == "（未设置）"
    assert bt_state.pretty("nonsense") == "nonsense"     # 认不出就原样，不编造


# ══════════════════════════════════════════════════════════════════════
# 二、判据与动作闸（本组件全部的业务逻辑）
# ══════════════════════════════════════════════════════════════════════

def _snap(*, ok=True, reason="", controller_on=True, paired=True, here=False,
          others=(), hid_ok=True) -> bt_state.Snapshot:
    """一份现场：``paired`` ＝ 在已配对列表里，``here`` ＝ HID 层枚举到了。

    默认那只鼠标**已配对但不在线**（＝平时关机、合盖后的常见样子）。``hid_ok`` 是
    HID 主判据有没有真读出来（``False`` ＝ ioreg 解析失败，≠「设备不在」）。
    """
    devices: dict = {}
    hid: dict = {}
    if paired:
        devices[MOUSE_N] = {"name": "MX Master 3S", "connected": here}
    if here:
        hid[MOUSE_N] = "MX Master 3S"
    for addr in others:
        devices[bt_state.norm(addr)] = {"name": addr, "connected": True}
        hid[bt_state.norm(addr)] = addr
    return bt_state.Snapshot(ok, reason, controller_on, devices, hid, hid_ok=hid_ok)


class _Rig:
    """探针与动作的替身：换现场、数动作次数。"""

    def __init__(self, monkeypatch):
        self.snapshot = _snap()
        self.probes = 0
        self.acts: list[str] = []
        self.restart_result: tuple[bool, str] = (True, "已重开蓝牙，设备回来了")
        monkeypatch.setattr(bt_state, "probe", self.probe)
        monkeypatch.setattr(bt_state, "restart_controller", self.restart)

    def probe(self):
        self.probes += 1
        return self.snapshot

    def restart(self, addr):
        self.acts.append(addr)
        return self.restart_result

    def set(self, snapshot: bt_state.Snapshot) -> None:
        self.snapshot = snapshot


def _cfg(**fields) -> str:
    """插件配置段（YAML）。``device`` 缺省填本机实测的那只鼠标。"""
    fields.setdefault("device", MOUSE)
    return "".join(f"    {k}: {v}\n" for k, v in fields.items())


@pytest.fixture
def build(tmp_path, monkeypatch):
    """造插件：探针与动作换成替身，其余（配置 / 状态机 / 通知）全走真件。

    **耐心窗默认置 0**：那段等待只决定「醒后这一读取在什么时候」，不改变任何一条判定的
    结论（替身在窗口里每次给的读数都一样）。不置 0 的话，每一条「醒后设备不在」的用例都要
    真等满 7 秒 —— 实测整个文件从 8 秒涨到 150 秒以上。专测耐心窗的那几条自己覆盖它
    （见 ``test_wake_hid_reprobe_recovers_within_patience`` 等）。
    """
    monkeypatch.setattr(plugin_mod, "WAKE_HID_PATIENCE_S", 0.0)
    rig = _Rig(monkeypatch)

    def factory(config_extra: str | None = None, hub=None, dirty=None):
        user = tmp_path / "config.yaml"
        user.write_text(
            "plugins:\n  bt_guard:\n    enabled: true\n"
            + (_cfg() if config_extra is None else config_extra),
            encoding="utf-8",
        )
        ctx = PluginContext(config=Config(user_path=user), events=None)
        if hub is not None:
            ctx.notify = hub
        if dirty is not None:
            ctx.mark_dirty = dirty
        return plugin_mod.Plugin(ctx)

    factory.rig = rig
    return factory


def _sleep(plugin, topic: str = POWER_SLEEP_TOPIC) -> None:
    """走一遍睡眠类事件（默认系统那一族；传显示器话题就换那一族）。"""
    plugin.on_system_event(topic, None)


def _wake(plugin, topic: str = POWER_WAKE_TOPIC) -> None:
    """走一遍唤醒类事件（默认系统那一族；传显示器话题就换那一族）。"""
    plugin.on_system_event(topic, None)


# ---------- 前提：配置与探针 ----------

def test_no_device_configured_says_so_and_never_touches_anything(build):
    plugin = build(config_extra="")
    assert plugin.tick().state == "not_configured"
    assert build.rig.acts == []


def test_probe_failure_is_reported_not_swallowed(build):
    build.rig.set(_snap(ok=False, reason="system_profiler 没给出蓝牙数据"))
    report = build().tick()
    assert report.state == "probe_failed"
    assert "system_profiler" in report.summary
    assert build.rig.acts == []


def test_hid_unreadable_on_wake_does_not_power_cycle(build, monkeypatch):
    """HID 主判据读不出来 ⇒ 无权判定掉线，绝不趁这次断电重开。

    这正是 2026-09-30 那一批要守的：读不出 ≠ 设备不在，把前者当后者去重开蓝牙会无端
    打断 AirPods。修法：``hid_ok=False`` 时按「没有证据」处理，报 ``probe_failed``、
    不动手（见 ``plugin._evaluate``）。

    本用例只钉「守卫」这一条，故把耐心窗置 0（不测重探，见下面那几条专测耐心窗的用例）。
    """
    monkeypatch.setattr(plugin_mod, "WAKE_HID_PATIENCE_S", 0.0)
    plugin = build()
    # 睡前：HID 读得到、设备在场 ⇒ 基线记 True（睡前它还在）。
    build.rig.set(_snap(here=True))
    _sleep(plugin)
    plugin.tick()
    # 醒后：HID 主判据读不出来（ioreg 解析失败），但蓝牙层照常。
    build.rig.set(_snap(here=False, hid_ok=False))
    _wake(plugin)
    report = plugin.tick()
    assert report.state == "probe_failed"
    assert "HID" in report.summary
    assert build.rig.acts == []          # 关键：没有断电重开


def test_wake_hid_reprobe_recovers_within_patience(build, monkeypatch):
    """醒后 HID 读不出 ⇒ 在耐心窗内重探；抖过去之后照常判定，不白白退化成「读不到」。

    现场实测亮屏后约 6 秒才重新读到 HID 现场（2026-09-30：14:05:00 起连续读不出 →
    14:05:06 读到），一次短重试盖不住 —— 这条钉的正是「重探到读到为止，再拿这份现场去
    判定」。
    """
    monkeypatch.setattr(plugin_mod, "WAKE_HID_PATIENCE_S", 5.0)
    monkeypatch.setattr(plugin_mod, "WAKE_HID_RETRY_GAP_S", 0.0)
    plugin = build()
    # 睡前：HID 读得到、设备在场。
    build.rig.set(_snap(here=True))
    _sleep(plugin)
    plugin.tick()
    # 醒后：头一次读不出，重探第二次读得出且设备在场（模拟抖动过去）。
    seq = iter([_snap(here=False, hid_ok=False), _snap(here=True)])
    monkeypatch.setattr(bt_state, "probe", lambda: next(seq, _snap(here=True)))
    _wake(plugin)
    report = plugin.tick()
    assert report.state == "ok"          # 重探读到「在场」⇒ 不误判、不动手
    assert build.rig.acts == []


def test_wake_hid_patience_exhausted_still_does_not_act(build, monkeypatch):
    """耐心窗内始终读不出 ⇒ 如实报 ``probe_failed``，**仍然不动手**（安全兜底）。

    给一段短的耐心窗（读不出会一直重探到窗满），验证窗满之后是「放弃并如实说」，而不是
    掉进「当掉线去重开」那条路。
    """
    monkeypatch.setattr(plugin_mod, "WAKE_HID_PATIENCE_S", 0.2)
    monkeypatch.setattr(plugin_mod, "WAKE_HID_RETRY_GAP_S", 0.05)
    plugin = build()
    build.rig.set(_snap(here=True))
    _sleep(plugin)
    plugin.tick()
    build.rig.set(_snap(here=False, hid_ok=False))   # 耐心窗内每次读都失败
    _wake(plugin)
    report = plugin.tick()
    assert report.state == "probe_failed"
    assert build.rig.acts == []


def test_wake_reprobes_until_the_device_shows_up(build, monkeypatch):
    """醒后设备还没枚举回来 ⇒ 在耐心窗里重探，它自己出现就**不动手**。

    这是 2026-09-30 现场真实栽的那一档：亮屏后头一读**读得到**、只是里面没有那只鼠标，
    旧代码当掉线、当场重开了一次蓝牙、把正连着的 AirPods 打断了 —— 而设备其实 6 秒就自己
    回来了（日志 14:05:00,264 重开 → 14:05:06 才见到），那一刀是白挨的。

    与下面那条成对：耐心**不是**「醒后一律不动手」，它只是把「醒后」那份读数取在
    「设备已经有机会回来」之后。
    """
    monkeypatch.setattr(plugin_mod, "WAKE_HID_PATIENCE_S", 5.0)
    monkeypatch.setattr(plugin_mod, "WAKE_HID_RETRY_GAP_S", 0.0)
    plugin = build()
    build.rig.set(_snap(here=True))              # 睡前：它在
    _sleep(plugin)
    plugin.tick()
    # 醒后：头两读都「读得到、但它还没回来」，第三读它出现了。
    seq = iter([_snap(here=False), _snap(here=False), _snap(here=True)])
    monkeypatch.setattr(bt_state, "probe", lambda: next(seq, _snap(here=True)))
    _wake(plugin)
    report = plugin.tick()
    assert report.state == "ok"
    assert build.rig.acts == []                  # 关键：没白断一次已连的设备


def test_wake_patience_exhausted_then_acts(build, monkeypatch):
    """耐心窗里设备始终没出现 ⇒ 判定真掉线，照旧重开（耐心不是「不动手」）。

    上面那条的边界：耐心窗只负责「给设备一个自己回来的机会」，给完了仍不在，就该走原来
    那条路 —— 否则「多加一段等待」会把真掉线也一起放过。
    """
    monkeypatch.setattr(plugin_mod, "WAKE_HID_PATIENCE_S", 0.2)
    monkeypatch.setattr(plugin_mod, "WAKE_HID_RETRY_GAP_S", 0.05)
    plugin = build()
    build.rig.set(_snap(here=True))
    _sleep(plugin)
    plugin.tick()
    build.rig.set(_snap(here=False))             # 读得到、设备一直不在
    _wake(plugin)
    started = time.monotonic()
    report = plugin.tick()
    waited = time.monotonic() - started
    # 钉中间量：这一拍**真的在窗口里等过**。少了它，把重探整个删掉这条用例照样绿 ——
    # 那它就没守住「窗满才动手」这件事（替身每次给的读数一样，删掉重探后结论没变）。
    assert waited >= 0.15, f"应等满那段窗口（0.2s），实际只等了 {waited:.3f}s"
    assert build.rig.acts == [MOUSE], "窗满仍不在 ⇒ 该动手"
    assert report.state == "ok"                  # 替身的默认答复是「重开后设备回来了」


def test_wake_patience_window_uses_monotonic_clock(build, monkeypatch):
    """耐心窗的 deadline 走单调钟：墙钟被校回时不会把这段循环拖成空转。

    回归：循环判据原是 ``time.time() < deadline``，而 ``deadline`` 也由 ``time.time()``
    算出来。时钟被往回校（NTP 步进 / 唤醒后校正）时这个条件长时间为真 ⇒ 循环按
    :data:`WAKE_HID_RETRY_GAP_S` 一跳一直跑下去，worker 被占住 ⇒ 调度器判 tick 超时
    ⇒ 连续五次即熔断停用，而日志只写「tick 超时」，看不出真正原因。
    """
    monkeypatch.setattr(plugin_mod, "WAKE_HID_PATIENCE_S", 0.2)
    monkeypatch.setattr(plugin_mod, "WAKE_HID_RETRY_GAP_S", 0.05)
    plugin = build()
    build.rig.set(_snap(here=True))
    _sleep(plugin)
    plugin.tick()
    build.rig.set(_snap(here=False, hid_ok=False))    # 耐心窗内每次都读不出

    # 墙钟一路往回走：旧写法下 deadline 永远追不上，循环停不下来。
    wall = itertools.count(1_700_000_000.0, -3600.0)
    monkeypatch.setattr(time, "time", lambda: next(wall))
    real_sleep, spins = time.sleep, itertools.count(1)

    def _guarded_sleep(seconds):
        if next(spins) > 40:      # 0.2s 的窗、0.05s 一跳 ⇒ 正常只要 4 次
            raise AssertionError("耐心窗没在到点后退出（墙钟一被校回就一直空转）")
        real_sleep(seconds)

    monkeypatch.setattr(time, "sleep", _guarded_sleep)

    _wake(plugin)
    report = plugin.tick()
    assert report.state == "probe_failed"


def test_hid_layer_retries_transient_parse_failure(monkeypatch):
    """ioreg 偶发半截 plist（exit 0 但非法）⇒ 重试后成功，不当成缺席。

    亮屏后 IORegistry 处于变动期，这种瞬态是 2026-09-30 现场的直接成因；重试几下兜住它，
    就根本不会走到「读不出 ⇒ 当掉线 ⇒ 重开蓝牙」那条链。
    """
    calls = {"n": 0}
    bad = b"<plist><dict><key>Incomplete"
    good = _ioreg([("Bluetooth", {"DeviceAddress": "aa-bb-cc-dd-ee-01",
                                   "Product": "ATK A9 SE"})])

    def fake_run(argv, timeout_s=None):
        if argv[0] == bt_state.SYSPROF:
            return _sysprof()
        calls["n"] += 1
        return bad if calls["n"] == 1 else good

    monkeypatch.setattr(bt_state, "_run", fake_run)
    assert bt_state.hid_layer() == {MOUSE_N: "ATK A9 SE"}


def test_hid_layer_all_fail_marks_unreadable(monkeypatch):
    """三次都解析不出 ⇒ ``probe().hid_ok`` 为 ``False``（调用方据此不动手）。"""
    bad = b"not a plist at all"

    def fake_run(argv, timeout_s=None):
        if argv[0] == bt_state.SYSPROF:
            return _sysprof()
        return bad

    monkeypatch.setattr(bt_state, "_run", fake_run)
    snap = bt_state.probe()
    assert snap.hid_ok is False
    assert snap.ok is True              # 蓝牙层另说，HID 只是读不出、不是「没设备」


def test_hid_probe_command_does_not_filter_by_key():
    """探针命令不许再按值过滤（``-k``）—— 这不是形状洁癖，是 2026-10-01 现场踩出来的。

    ``ioreg`` 的 ``-c`` 与 ``-k`` 是与关系：带上 ``-k DeviceAddress`` 之后，根集合就变成
    「class 是 ``IOHIDDevice`` 且带设备地址的对象」，而「设备不在场」正好让这个集合为空；
    那时 ``ioreg`` **一个字节都不输出**（退出码仍是 0），``plistlib`` 报
    ``InvalidFileException``，与「命令半路没写完」完全同形。本机实测：带 ``-k`` 跑 40 次
    → 40 次解析失败、输出恒 0 字节；去掉它 → 40 次全成、2.53MB、约 0.28 秒。

    省量的那一项是 ``-r -d 1``（``-a -c IOHIDDevice`` 6.0MB → 2.53MB），不是 ``-k``；
    别再把它俩的功劳混在一起记账。行为面的守卫见下面那条按真机契约写的替身。
    """
    assert "-k" not in bt_state.IOREG_HID_ARGV
    assert "-c" in bt_state.IOREG_HID_ARGV          # 只按 class 筛，筛法不能整个丢掉
    assert "-r" in bt_state.IOREG_HID_ARGV and "-d" in bt_state.IOREG_HID_ARGV


def test_probe_reads_a_site_without_bluetooth_as_a_valid_reading(monkeypatch):
    """「读到了一份没有蓝牙设备的现场」是**合法读数**，不是「读不出来」。

    替身照真机的契约给读数：按 ``-k`` 筛完之后**命中为空就一个字节都不输出**。本机
    （无蓝牙设备在场）HID 对象全是内建键盘 / 音频 / 传感器那一批、**一个都不带
    ``DeviceAddress``** ⇒ 旧命令必然零字节 ⇒ 被读成「读不出来」⇒ 绝不动手，而这恰恰是
    本组件唯一该动手的现场。命令一退回带 ``-k`` 的写法，本用例即转红。

    另一半（命令真的没写完）见 ``test_hid_layer_all_fail_marks_unreadable``。
    """
    box = {"objs": [{"Transport": "FIFO", "Product": "Apple Internal Keyboard / Trackpad"}]}

    def fake_run(argv, timeout_s=None):
        if argv[0] == bt_state.SYSPROF:
            return _sysprof(not_connected=[("ATK A9 SE", MOUSE)])
        if argv[0] != bt_state.IOREG:
            raise AssertionError(f"没预料到的命令：{argv}")
        objs = list(box["objs"])
        if "-k" in argv:                        # 真机语义：按值与 class 取交集
            key = argv[argv.index("-k") + 1]
            objs = [o for o in objs if key in o]
        return plistlib.dumps(objs) if objs else b""    # 命中为空 ⇒ 零字节（rc 仍 0）

    monkeypatch.setattr(bt_state, "_run", fake_run)

    snap = bt_state.probe()
    assert snap.hid_ok is True, "读到了一份里面没有蓝牙设备的现场 = 合法读数"
    assert snap.present(MOUSE) is False      # 判据敢据此说「它不在场」
    assert snap.bluetooth_connected(MOUSE) is False

    # 设备回来了（带地址的蓝牙 HID 对象出现）⇒ 仍然读得到、且判得对。
    box["objs"].append({"Transport": "Bluetooth", "DeviceAddress": "aa-bb-cc-dd-ee-01",
                        "Product": "ATK A9 SE"})
    assert bt_state.probe().present(MOUSE) is True


def test_bluetooth_off_is_a_user_choice_not_a_fault(build):
    """蓝牙关着 ⇒ 报「蓝牙关着」，**不动手**：重开一次等于替用户改主意。"""
    build.rig.set(_snap(controller_on=False))
    plugin = build()
    _sleep(plugin)
    plugin.tick()
    _wake(plugin)
    assert plugin.tick().state == "bt_off"
    assert build.rig.acts == []


def test_a_device_outside_the_paired_list_is_reported_as_such(build):
    """不在已配对列表里 ⇒ 如实说「这个设备不在已配对列表里」，不是「掉线」。"""
    build.rig.set(_snap(paired=False, here=False))
    assert build().tick().state == "device_unknown"


# ---------- 正题：跨睡眠边界的四点 ----------

def test_online_at_start_and_still_online_is_ok(build):
    build.rig.set(_snap(here=True))
    assert build().tick().state == "ok"


def test_absent_without_any_event_is_just_offline_and_does_not_act(build):
    """没有跨边界事件时，「不在线」只是现状，不做掉线判定、更不动手。"""
    build.rig.set(_snap(here=False))
    assert build().tick().state == "offline"
    assert build.rig.acts == []


def test_lost_across_sleep_is_brought_back(build):
    """睡前在、醒后不在 ⇒ **掉线**，动手重开蓝牙（本组件的正题）。"""
    plugin = build()
    rig = build.rig

    rig.set(_snap(here=True))
    assert plugin.tick().state == "ok"                 # 启动那一次：无事件，只报现状

    _sleep(plugin)
    assert plugin.tick().state == "ok"                 # 睡前那一拍：记下基线
    assert rig.acts == []

    _wake(plugin)
    rig.set(_snap(here=False))
    assert plugin.tick().state == "ok"                 # 重开成功、设备回来
    assert rig.acts == [MOUSE]


def test_absent_on_both_sides_does_not_act(build):
    """睡前不在、醒后也不在 ⇒ **用户自己关了鼠标**，不许动手。

    这一格与上一条的**动作刚好相反**，而「醒后不在」这个单点读数长得一模一样 ——
    整条判据存在的理由就是把它俩分开。
    """
    plugin = build()
    rig = build.rig

    rig.set(_snap(here=False))
    plugin.tick()

    _sleep(plugin)
    assert plugin.tick().state == "offline"
    _wake(plugin)
    assert plugin.tick().state == "offline"
    assert rig.acts == []


def test_still_online_after_sleep_is_ok(build):
    plugin = build()
    build.rig.set(_snap(here=True))
    _sleep(plugin)
    plugin.tick()
    _wake(plugin)
    assert plugin.tick().state == "ok"
    assert build.rig.acts == []


def test_device_returning_by_itself_is_ok(build):
    """睡前不在、醒后在 ⇒ 它自己回来了，正常，不动手。"""
    plugin = build()
    rig = build.rig
    rig.set(_snap(here=False))
    _sleep(plugin)
    plugin.tick()
    _wake(plugin)
    rig.set(_snap(here=True))
    assert plugin.tick().state == "ok"
    assert rig.acts == []


# ---------- 显示器那一族：本机日常那次「关屏 / 亮回来」就是它 ----------

def test_display_family_alone_is_enough_to_catch_a_lost_device(build):
    """只发显示器那一族也要能判定并动手。

    这条对应本组件第一次上线时的实际故障：那台机器上 ``Sleep/Wakes since boot`` 一直是
    ``0``，关屏只留下 ``Display is turned off/on`` ⇒ 只订系统那两条时，它除启动那轮外
    一次都没跑过。判据本身没错，是**没人叫醒它**。
    """
    plugin = build()
    rig = build.rig

    rig.set(_snap(here=True))
    assert plugin.tick().state == "ok"

    _sleep(plugin, POWER_DISPLAY_SLEEP_TOPIC)
    assert plugin.tick().state == "ok"
    assert rig.acts == []
    # ★ 这一条断言才是本用例的要害：只看「醒后动手了」是分不出走的是判据还是**降级
    #   路径**（「本次运行见过它」那条也会动手）。少了它，「睡眠类事件没被认出来」这种
    #   植入照样全绿 —— 而真机上那正是「判据整体失效、却还在碰运气动手」。
    assert plugin._baseline is True, "显示器睡眠那一族没被认成睡前边界"

    _wake(plugin, POWER_DISPLAY_WAKE_TOPIC)
    rig.set(_snap(here=False))
    assert plugin.tick().state == "ok"
    assert rig.acts == [MOUSE], "只发显示器那一族时判据根本没跑"


def test_display_family_also_spares_a_mouse_the_user_switched_off(build):
    """显示器那一族里，「睡前不在 ＋ 醒后不在」照样不许动手。"""
    plugin = build()
    build.rig.set(_snap(here=False))
    _sleep(plugin, POWER_DISPLAY_SLEEP_TOPIC)
    plugin.tick()
    _wake(plugin, POWER_DISPLAY_WAKE_TOPIC)
    assert plugin.tick().state == "offline"
    assert build.rig.acts == []


def test_the_first_sleep_reading_wins_the_baseline(build):
    """两族睡眠通知里**先到的那条**才算基线。

    合盖时屏幕先灭、系统随后才睡；等到系统那条来时，蓝牙设备可能已经掉了 —— 拿它覆盖
    基线，就把「这次掉的」读成「本来就没连」，醒来不动手、也不报错（判据整体静默失效）。
    这里第二条睡眠读到的是「不在」，正解仍然要动手。
    """
    plugin = build()
    rig = build.rig

    rig.set(_snap(here=True))
    _sleep(plugin, POWER_DISPLAY_SLEEP_TOPIC)          # 屏幕先灭：它还连着
    assert plugin.tick().state == "ok"
    assert plugin._baseline is True

    _sleep(plugin, POWER_SLEEP_TOPIC)                  # 系统随后才睡：此刻已经读不到了
    rig.set(_snap(here=False))
    plugin.tick()
    assert plugin._baseline is True, "后到的那条睡眠把基线覆盖掉了"

    _wake(plugin, POWER_WAKE_TOPIC)
    assert plugin.tick().state == "ok"
    assert rig.acts == [MOUSE], "基线被覆盖 ⇒ 掉线被读成「用户自己关的」"


def test_both_families_waking_together_act_only_once(build):
    """真合盖：两族两两同时来（屏幕先亮、系统十几秒后才醒）⇒ 一次唤醒只动一次手。

    ★这条**不能靠动作冷却兜**：冷却拦下的那一轮会被算成「已处理」，而面板停在动作中途
    那句「正在重开蓝牙」上。判据必须是「同一轮边界只算一次」。
    """
    plugin = build()
    rig = build.rig

    rig.set(_snap(here=True))
    _sleep(plugin, POWER_DISPLAY_SLEEP_TOPIC)
    plugin.tick()
    _sleep(plugin, POWER_SLEEP_TOPIC)                  # 合盖：系统那一族也来
    plugin.tick()

    rig.set(_snap(here=False))
    _wake(plugin, POWER_DISPLAY_WAKE_TOPIC)            # 屏幕先亮
    assert plugin.tick().state == "ok"
    assert rig.acts == [MOUSE]

    _wake(plugin, POWER_WAKE_TOPIC)                    # 十几秒后系统才唤醒
    assert plugin.tick().state == "offline", "第二条唤醒又比对了一次 —— 这一轮早该算完了"
    assert len(rig.acts) == 1


# ---------- 降级路径（基线缺失）----------

def test_missing_baseline_falls_back_to_ever_seen(build):
    """没记到睡前基线、但本次运行见过它 ⇒ 仍按掉线处理并动手。

    这是**有意选得偏动手**：``WillSleep`` 在某些关屏姿势下不来，而重开一次蓝牙是 8 秒、
    无损、可恢复的。代价写在下一格里。
    """
    plugin = build()
    rig = build.rig

    rig.set(_snap(here=True))
    plugin.tick()                                      # 启动时见过它 ⇒ _ever_seen

    _wake(plugin)                                      # 没有睡眠事件（WillSleep 没来）
    rig.set(_snap(here=False))
    assert plugin.tick().state == "ok"
    assert rig.acts == [MOUSE]


def test_missing_baseline_and_never_seen_reports_and_does_not_act(build):
    """既没基线、本次运行也从没见过它 ⇒ 如实说、不动手：**没有证据就别动人东西**。"""
    plugin = build()
    rig = build.rig
    rig.set(_snap(here=False))
    _wake(plugin)
    assert plugin.tick().state == "no_baseline"
    assert rig.acts == []


def test_sleep_and_wake_in_one_tick_does_not_mistake_now_for_the_baseline(build):
    """两个事件挤进同一轮 tick（睡前那次没跑完就被系统冻结）⇒ 不许把此刻当睡前现场。

    守的是「双标志而不是单槽」。单槽实现下这一轮只看到「有过事件 ＋ 设备不在」，就会
    把**醒后**的现场记成基线 —— 而它随即拿去跟自己比，结论永远自洽、也永远错（设备不在
    就会去动手，可真实原因是用户关掉了鼠标）。判据：走「没记到基线」那条路。
    """
    plugin = build()
    _sleep(plugin)
    _wake(plugin)
    assert plugin.tick().state == "no_baseline"
    assert plugin._baseline is None, "把醒后的现场当成了睡前基线"
    assert build.rig.acts == []


# ---------- 动作闸：开关 / 冷却 / 手动 ----------

def test_auto_reconnect_off_still_judges_but_does_not_act(build):
    """关掉自动重连 ⇒ **仍然判定与显示**，只是不动手。"""
    plugin = build(_cfg(auto_reconnect=False))
    rig = build.rig
    rig.set(_snap(here=True))
    _sleep(plugin)
    plugin.tick()
    _wake(plugin)
    rig.set(_snap(here=False))
    report = plugin.tick()
    assert report.state == "lost_untouched"
    assert "自动重连关着" in report.summary
    assert rig.acts == []


def test_a_second_action_within_the_cooldown_is_skipped(build):
    """一轮唤醒只动一次手：挨太近的第二次重开会造成「连上又断」的叠加。"""
    plugin = build()
    rig = build.rig

    rig.set(_snap(here=True))
    _sleep(plugin)
    plugin.tick()
    _wake(plugin)
    rig.set(_snap(here=False))
    assert plugin.tick().state == "ok"
    assert len(rig.acts) == 1

    rig.set(_snap(here=True))
    _sleep(plugin)
    plugin.tick()
    _wake(plugin)
    rig.set(_snap(here=False))
    report = plugin.tick()
    assert report.state == "lost_untouched"
    assert "刚重开过" in report.summary
    assert len(rig.acts) == 1, "冷却期内的第二次动手没被拦住"


def test_manual_retry_bypasses_the_cooldown(build):
    """用户点「再试一次」是他明确的意图 ⇒ 不受冷却限制。"""
    plugin = build()
    rig = build.rig

    rig.set(_snap(here=True))
    _sleep(plugin)
    plugin.tick()
    _wake(plugin)
    rig.set(_snap(here=False))
    plugin.tick()
    assert len(rig.acts) == 1

    plugin.on_notification_action("retry")             # 紧接着手动再来一次
    assert plugin.tick().state == "ok"
    assert len(rig.acts) == 2


def test_manual_retry_on_a_device_that_is_online_does_not_act(build):
    plugin = build()
    build.rig.set(_snap(here=True))
    plugin.tick()
    plugin.on_notification_action("retry")
    assert plugin.tick().state == "ok"
    assert build.rig.acts == []


def test_failed_action_reports_recover_failed_with_the_reason(build):
    plugin = build()
    rig = build.rig
    rig.restart_result = (False, "蓝牙重开过了，但 60 秒内没等到设备回来")

    rig.set(_snap(here=True))
    _sleep(plugin)
    plugin.tick()
    _wake(plugin)
    rig.set(_snap(here=False))
    report = plugin.tick()
    assert report.state == "recover_failed"
    assert "60 秒" in report.summary


def test_action_announces_working_before_it_starts(build):
    """动作要 8 秒以上 ⇒ 先播一条「正在处理」，别让面板空等一轮。"""
    plugin = build()
    rig = build.rig
    seen: list[str] = []
    plugin.publish_snapshot = lambda snap=None, key="main": seen.append(snap.state)

    rig.set(_snap(here=True))
    _sleep(plugin)
    plugin.tick()
    _wake(plugin)
    rig.set(_snap(here=False))
    plugin.tick()
    assert seen == ["lost"], seen


# ---------- 事件纪律 ----------

def test_event_topics_cover_both_edges_of_both_families(build):
    """五条都要订 —— **少一族就等于没订**；少了面板那条，界面就会说谎。

    四条电源事件：缺睡眠那条，判据退回单点读数的老路；缺**显示器**那一族则更彻底：本机
    日常那次「关屏 / 亮回来」只发它，组件除启动那轮外永远不会跑（真机上表现为「装好之后
    一点动静都没有」，而离屏用例全绿 —— 本组件第一次上线就是这么哑掉的）。

    第五条 ``app.panel_opened`` 与判定无关，管的是**面板那一行是不是此刻的事实**：本组件
    没有节拍，不订它就会在屏幕一直亮着时停在旧结论上（用户 2026-09-24 那条「鼠标掉了而
    界面还写设备在线」）。它与那四条并列钉在这里，防的是「顺手删掉一条没人拦」。
    """
    plugin = build()
    assert plugin.event_topics() == [
        POWER_SLEEP_TOPIC, POWER_DISPLAY_SLEEP_TOPIC,
        POWER_WAKE_TOPIC, POWER_DISPLAY_WAKE_TOPIC,
        PANEL_OPENED_TOPIC,
    ]


def test_system_event_handler_only_registers_intent(build):
    """handler 在主线程：只置标志 ＋ 登记脏，**不读现场、不动手**（IO 归 worker）。"""
    dirty: list = []
    plugin = build(dirty=lambda pid, reason: dirty.append((pid, reason)))
    rig = build.rig

    _sleep(plugin, POWER_DISPLAY_SLEEP_TOPIC)
    _wake(plugin, POWER_DISPLAY_WAKE_TOPIC)

    assert dirty == [("bt_guard", "sleep"), ("bt_guard", "wake")]
    assert rig.probes == 0, "handler 里读了现场 —— 主线程上不许做 IO"
    assert rig.acts == []


def test_the_second_wake_of_a_round_is_not_even_scheduled(build):
    """同一轮边界里的第二条唤醒**连脏都不登记**，只留一条日志。

    真合盖那一次：屏幕先亮（显示器唤醒），16 秒后系统才唤醒。两条都属「唤醒类」，而
    间隔远超过调度器的合并窗口 —— 拦不住就会跑两轮 tick、动两次手。

    为什么判据放在这一层（不登记脏）而不是「登记了、在 tick 里跳过」：第二条那一轮没有
    任何新信息，跑了只会白读一次现场；更糟的是它会把标志消费掉，让「这一轮已经处理过」
    这件事看起来像「已经是上一轮了」。
    """
    dirty: list = []
    plugin = build(dirty=lambda pid, reason: dirty.append((pid, reason)))

    _sleep(plugin, POWER_DISPLAY_SLEEP_TOPIC)
    _wake(plugin, POWER_DISPLAY_WAKE_TOPIC)
    _wake(plugin, POWER_WAKE_TOPIC)                    # 16 秒后的第二条

    assert dirty == [("bt_guard", "sleep"), ("bt_guard", "wake")]
    assert build.rig.probes == 0


def test_a_new_round_re_arms_the_wake_check(build):
    """下一轮边界重新武装 —— 去重不许把「第二次关屏」也一起吃掉。"""
    dirty: list = []
    plugin = build(dirty=lambda pid, reason: dirty.append((pid, reason)))

    _sleep(plugin, POWER_DISPLAY_SLEEP_TOPIC)
    _wake(plugin, POWER_DISPLAY_WAKE_TOPIC)
    _sleep(plugin, POWER_DISPLAY_SLEEP_TOPIC)
    _wake(plugin, POWER_DISPLAY_WAKE_TOPIC)

    assert dirty == [("bt_guard", "sleep"), ("bt_guard", "wake")] * 2


def test_notification_action_only_registers_intent(build):
    dirty: list = []
    plugin = build(dirty=lambda pid, reason: dirty.append((pid, reason)))
    plugin.on_notification_action("retry")
    assert dirty == [("bt_guard", "manual_retry")]
    assert build.rig.probes == 0
    assert build.rig.acts == []


def test_unknown_notification_action_is_ignored(build):
    plugin = build()
    plugin.on_notification_action("whatever")
    assert plugin._manual_seen is False
    assert build.rig.acts == []


def test_each_event_is_consumed_by_exactly_one_tick(build):
    """标志消费后清零：一次事件只处理一轮，下一轮退回「只报现状」。"""
    plugin = build()
    rig = build.rig
    rig.set(_snap(here=False))

    _wake(plugin)
    assert plugin.tick().state == "no_baseline"        # 这一轮消费掉了唤醒
    assert plugin.tick().state == "offline"            # 下一轮没有事件了
    assert rig.acts == []


# ══════════════════════════════════════════════════════════════════════
# 三、助手：唯一的写路径
# ══════════════════════════════════════════════════════════════════════

class FakeHelper(threading.Thread):
    """假助手：**真连会话节点、真握手、真收发帧**。

    app 侧那一半（监听、accept、握手、``req_id`` 配对、断链重建）只能靠一个**真的对端**
    来验 —— 换成「假装收到命令就返回结果」的桩，验到的是桩自己。真件里唯一被换掉的是
    「``open`` 拉起 ``.app``」这一步（测试里既不弹窗口也不碰真蓝牙）；节点、分帧、握手、
    配对全部走真路径。线上的 Objective-C 助手与它镜像同一份 ``protocol``。

    剧本（都对应一条真实会发生的形态）：

    - ``replies``：op → ``reply.result``；没配的 op 答 ``{"code": 0, "lines": []}``
    - ``drop_after``：答完 N 条就断开 —— 验「会话死在两条命令之间必须重建」
    - ``silent_for``：这些 op **不回执** —— 验 app 侧超时
    - ``protocol_version``：故意报别的版本 —— 验握手对契约版本
    """

    daemon = True

    def __init__(self, path, replies=None, *, drop_after=None, silent_for=(),
                 protocol_version=None):
        super().__init__()
        self.path = str(path)
        self.replies = dict(replies or {})
        self.drop_after = drop_after
        self.silent_for = set(silent_for)
        self.protocol_version = protocol_version
        #: 收到的 op，按顺序 —— 验「谁被跑过、跑了几次、顺序对不对」。
        self.seen: list[str] = []
        #: 收到的 ``welcome``（握手里 app 的回帧）。
        self.welcome: dict | None = None
        #: 线程里抛出来的异常（不能让它在后台静默）。
        self.failed: Exception | None = None

    def run(self) -> None:
        try:
            self._serve()
        except Exception as e:      # noqa: BLE001
            self.failed = e

    def _serve(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        deadline = time.monotonic() + 5.0
        while True:
            try:
                sock.connect(self.path)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.01)          # 模拟助手起来那点延迟
        reader = FrameReader()
        pending: list = []

        def send(msg) -> None:
            sock.sendall(protocol.encode(msg))

        def recv():
            while not pending:
                data = sock.recv(65536)
                if not data:
                    return None
                pending.extend(reader.feed(data))
            return pending.pop(0)

        send(protocol.hello("bt-helper",
                            version=self.protocol_version or protocol.PROTOCOL_VERSION))
        self.welcome = recv()
        if not self.welcome or not self.welcome.get("ok"):
            sock.close()
            return

        answered = 0
        while True:
            msg = recv()
            if msg is None:
                break                     # app 关了连接
            if protocol.message_type(msg) != protocol.OP_RUN:
                continue
            op = str(msg.get("op") or "")
            self.seen.append(op)
            if op in self.silent_for:
                continue                  # 不回执：让 app 侧走到超时
            result = self.replies.get(op, {"code": 0, "lines": []})
            send(protocol.reply(str(msg.get("req_id") or ""),
                                ok=result.get("code") == 0, result=result))
            answered += 1
            if self.drop_after is not None and answered >= self.drop_after:
                break                     # 断链：验 app 侧重不重建
        sock.close()


class HelperRig:
    """把「拉起助手」换成「起一个 :class:`FakeHelper` 线程」，并记下起了几个。

    为什么必须在 ``spawn()`` 那一刻才起线程：app 是「**先 listen 再拉起**」的，节点此刻
    才存在（``FakeHelper`` 自己会重试连接）。返回 ``True`` 表示「发起了拉起」。
    """

    def __init__(self) -> None:
        self.instances: list[FakeHelper] = []
        self.spawn_count = 0
        self.replies: dict = {}
        self.options: dict = {}

    def will_answer(self, replies=None, **options) -> "HelperRig":
        """配置剧本（在调 ``run_commands`` 之前）。"""
        self.replies = dict(replies or {})
        self.options = dict(options)
        return self

    def spawn(self) -> bool:
        self.spawn_count += 1
        node = FakeHelper(paths.BT_HELPER_SOCKET, self.replies, **self.options)
        self.instances.append(node)
        node.start()
        return True

    def wait(self, timeout: float = 3.0) -> None:
        """等假助手把话说完，并把线程里的异常翻出来（否则用例会假绿）。"""
        for node in self.instances:
            node.join(timeout)
            if node.failed is not None:
                raise node.failed


@pytest.fixture
def helper(monkeypatch):
    """助手会话的替身工厂：换掉「拉起」那一步，其余走真件。

    **节点必须换到一个短路径**：Unix 域套接字的 ``sun_path`` 上限是 103 字节，而 pytest
    的临时目录本身（``/private/var/folders/…/pytest-of-…/test_…/barkit-data``）就有一百
    多字节 —— 不换的话测到的是「路径太长」，不是被测的那件事（那三条用例会**假绿**：
    结果同样是 ``None``）。生产上没这个问题：节点在 ``~/Library/Application Support/
    BarKit/`` 下，约 57 字节；真超了会报一条 ERROR（不静默）。
    """
    node_dir = Path(tempfile.mkdtemp(prefix="bt-helper-", dir="/tmp"))
    monkeypatch.setattr(paths, "BT_HELPER_SOCKET", node_dir / "bt_helper.sock")
    rig = HelperRig()
    monkeypatch.setattr(bt_state, "spawn_helper", rig.spawn)
    yield rig
    shutil.rmtree(node_dir, ignore_errors=True)


def test_app_path_ships_inside_the_plugin_package():
    """助手随包分发：它就在插件包里，不是运行时才去下载的东西。"""
    app = bt_state.app_path()
    assert app.name == "BTHelper.app"
    assert app.parent.name == "resources"
    assert app.parent.parent.name == "bt_guard"
    assert (app / "Contents" / "Info.plist").is_file()
    assert (app / "Contents" / "MacOS" / "bt-helper").is_file()


def _objc_protocol_mismatches(src: str) -> list[str]:
    """``bt-helper.m`` 与 ``protocol.py`` 对不上的地方（空列表 ＝ 一致）。

    抽成**纯函数**是为了能喂一份「被改坏」的源码进去自证 —— 否则没人知道这条门禁
    到底抓不抓得到东西（这是本仓门禁的标准姿势）。
    """
    bad: list[str] = []
    if f'"protocol": @{protocol.PROTOCOL_VERSION}' not in src:
        bad.append("协议版本")
    for name in (protocol.HELLO, protocol.WELCOME, protocol.OP_RUN, protocol.REPLY):
        if f'@"{name}"' not in src:
            bad.append("消息名 %s" % name)
    for field in ("type", "req_id", "ok", "result", "code", "lines"):
        if f'@"{field}"' not in src:
            bad.append("字段 %s" % field)
    if "1u << 20" not in src or protocol.MAX_FRAME != 1 << 20:
        bad.append("单帧上限")
    rel = str(Path("Library/Application Support/BarKit") / paths.BT_HELPER_SOCKET.name)
    if f'@"{rel}"' not in src:
        bad.append("会话节点路径")
    return bad


def test_the_objc_helper_mirrors_the_python_protocol():
    """助手是同一份协议的**第二份实现**（Objective-C），而它**拿不到 Python 常量**
    —— 只能靠这条门禁把两侧的字面量钉在一起。

    没有它，改了 ``protocol.py`` 的消息名或协议版本、忘了改 ``bt-helper.m``，两侧会在
    真机上握手失败；而那要到用户合盖唤醒、组件真动手时才暴露，离屏一条用例都不会红。
    路径同理：节点名错一个字，症状是「app 在听、助手连不上」，看起来像助手没起来。
    """
    src = (bt_state.app_path().parent / "helper" / "bt-helper.m").read_text(encoding="utf-8")
    assert src, "助手源码是空的 —— 这条门禁在测别的东西"
    assert _objc_protocol_mismatches(src) == []


def test_the_objc_protocol_gate_catches_a_mismatch():
    """双自证：改了消息名必被抓、原样的不误报。

    控制组用**真实会出错的那份输入**（改了 ``protocol.py`` 却忘了改助手），改动方向是
    从真源码里替换掉那个字面量，不是凭空造一份。
    """
    src = (bt_state.app_path().parent / "helper" / "bt-helper.m").read_text(encoding="utf-8")
    assert _objc_protocol_mismatches(src) == [], "原样的助手被误报了"

    broken = src.replace('@"op.run"', '@"op.exec"')
    assert broken != src, "替换没生效 —— 控制组测不到东西"
    assert "消息名 op.run" in _objc_protocol_mismatches(broken)


def test_ensure_executable_restores_a_stripped_bit(monkeypatch, tmp_path):
    """wheel 打包会静默丢掉可执行位 ⇒ 首次使用前就地补回来（用户可写区、免 root）。"""
    app = tmp_path / "BTHelper.app"
    binary = app / "Contents" / "MacOS" / "bt-helper"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o644)
    monkeypatch.setattr(bt_state, "app_path", lambda: app)

    assert bt_state.ensure_executable() is True
    assert binary.stat().st_mode & 0o111


def test_ensure_executable_does_not_touch_a_binary_that_already_has_the_bit(
        monkeypatch, tmp_path):
    app = tmp_path / "BTHelper.app"
    binary = app / "Contents" / "MacOS" / "bt-helper"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr(bt_state, "app_path", lambda: app)
    assert bt_state.ensure_executable() is True
    assert binary.stat().st_mode & 0o111


def test_ensure_executable_reports_false_when_the_helper_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(bt_state, "app_path", lambda: tmp_path / "nope.app")
    assert bt_state.ensure_executable() is False


def test_helper_session_runs_every_command_over_one_session(helper):
    """两条命令走**同一条会话**：一个助手进程，不是两次 ``open``。

    这正是会话相对「一次调用一个进程」的全部意义 —— 旧形态下 ``restart_controller``
    要 ``open`` 两次，每次都有一次冷启动。
    """
    helper.will_answer({"power-off": {"code": 0, "lines": ["before=1", "after=0"]},
                        "power-on": {"code": 0, "lines": ["after=1"]}})

    got = bt_session.run_commands(["power-off", "power-on"], spawn=helper.spawn, gap_s=0.0)
    helper.wait()

    assert [r["code"] for r in got] == [0, 0]
    assert helper.spawn_count == 1, "两条命令起了两个助手 —— 会话没被复用"
    assert helper.instances[0].seen == ["power-off", "power-on"]


def test_helper_session_removes_its_node_when_done(helper):
    """节点不是常驻通道：跑完就 unlink，稳态下磁盘上不该有它。"""
    helper.will_answer()

    bt_session.run_commands(["power"], spawn=helper.spawn)
    helper.wait()

    assert not paths.BT_HELPER_SOCKET.exists()


def test_helper_session_gives_up_when_the_helper_cannot_be_spawned(helper):
    """拉起失败 ⇒ 当场放弃（**不等满 connect 超时**），结果全空。

    ``helper`` 在这里只用它那个副作用：把会话节点换成短路径（理由见该 fixture）。
    """
    started = time.monotonic()
    got = bt_session.run_commands(["power-off", "power-on"], spawn=lambda: False)
    assert got == [None, None]
    assert time.monotonic() - started < 1.0, "助手都没拉起来，却还在等它连上来"


def test_helper_session_gives_up_when_nobody_connects(helper):
    """拉起了助手、它却没连上来 ⇒ 到点收手，不无限等。"""
    got = bt_session.run_commands(["power"], spawn=lambda: True, connect_wait_s=0.2)
    assert got == [None]


def test_helper_session_rebuilds_when_it_dies_between_commands(helper):
    """``power-off`` 答完就断 ⇒ **重建一条会话**把 ``power-on`` 跑完。

    守的是那条不变量：把用户的蓝牙留在关着的状态，是这条路上唯一真正危险的失败形态，
    而它恰好由「off 成了、on 没跑」造成。旧形态下那是两次独立调用、天然能继续；
    换成一条会话之后必须显式保住。
    """
    helper.will_answer({"power-off": {"code": 0, "lines": []},
                        "power-on": {"code": 0, "lines": []}}, drop_after=1)

    got = bt_session.run_commands(["power-off", "power-on"], spawn=helper.spawn, gap_s=0.0)
    helper.wait()

    assert [r["code"] for r in got] == [0, 0]
    assert helper.spawn_count == 2, "会话断了却没重建 —— power-on 没跑成"
    assert [n.seen for n in helper.instances] == [["power-off"], ["power-on"]]


def test_helper_session_times_out_on_a_silent_command(helper):
    """助手收了命令却不回执 ⇒ 到点判失败，不把 worker 线程一直占着。"""
    helper.will_answer(silent_for={"power-off"})

    got = bt_session.run_commands(["power-off"], spawn=helper.spawn, reply_wait_s=0.3)
    assert got == [None]


def test_helper_session_refuses_a_helper_on_the_wrong_protocol(helper):
    """握手是唯一对契约版本的地方：版本不符即拒，且**说得出是哪一侧旧**。"""
    helper.will_answer(protocol_version=protocol.PROTOCOL_VERSION + 1)

    got = bt_session.run_commands(["power"], spawn=helper.spawn)
    helper.wait()

    assert got == [None]
    assert helper.instances[0].welcome is not None
    assert helper.instances[0].welcome["ok"] is False
    assert "旧" in helper.instances[0].welcome["reason"]


def _fake_run(monkeypatch, results: dict) -> list[str]:
    """把会话那一层换掉，只验 ``restart_controller`` 怎么处置这两条回执。

    返回「收到过的命令」清单 —— 条数与顺序就是这条路的语义（见下三条用例）。
    会话怎么建、怎么握手、断了怎么重建，由 ``FakeHelper`` 那组用例单独守。
    """
    calls: list[str] = []

    def fake_run(cmds, *, spawn=None, gap_s=0.0, **kwargs):
        calls.extend(cmds)
        return [results.get(c, {"code": 0, "lines": []}) for c in cmds]

    monkeypatch.setattr(bt_session, "run_commands", fake_run)
    return calls


def test_restart_controller_turns_bluetooth_back_on_even_if_power_off_failed(monkeypatch):
    """``power-off`` 成没成都必须继续 ``power-on``。

    把用户的蓝牙留在**关着**的状态，是这条路上唯一真正危险的失败形态 —— 所以判据不是
    「断电成功了吗」，而是「最后是不是开着」。
    """
    calls = _fake_run(monkeypatch, {"power-off": {"code": 1, "lines": ["设备端拒绝"]},
                                    "power-on": {"code": 0, "lines": []}})
    monkeypatch.setattr(bt_state, "wait_present", lambda addr, timeout_s=None: True)

    ok, said = bt_state.restart_controller(MOUSE)
    assert calls == ["power-off", "power-on"], "断电失败就没继续开 —— 会把蓝牙留在关着"
    assert ok is True and said


def test_restart_controller_reports_failure_when_the_device_never_returns(monkeypatch):
    _fake_run(monkeypatch, {})
    monkeypatch.setattr(bt_state, "wait_present", lambda addr, timeout_s=None: False)

    ok, said = bt_state.restart_controller(MOUSE)
    assert ok is False
    assert "没等到" in said


def test_restart_controller_reports_when_the_helper_cannot_turn_it_back_on(monkeypatch):
    """开不回来 ⇒ 明说「请手动检查」，而不是含糊地说「没等到设备」。"""
    _fake_run(monkeypatch, {"power-on": {"code": 3, "lines": ["拒绝"]}})
    monkeypatch.setattr(bt_state, "wait_present", lambda addr, timeout_s=None: True)

    ok, said = bt_state.restart_controller(MOUSE)
    assert ok is False
    assert "手动" in said


# ══════════════════════════════════════════════════════════════════════
# 四、声明面与配置
# ══════════════════════════════════════════════════════════════════════

def _bare():
    return plugin_mod.Plugin(PluginContext(config=None, events=None))


def test_start_defers_the_probe_to_the_worker(build, monkeypatch):
    """``start()`` 不在主线程读现场——探针交给 worker，主线程只登记一次点火。

    探针是 ``system_profiler`` + ``ioreg``（2.6MB）+ 两次 plist 解析，约 0.45s。
    放在启动主线程上，排在它后面的每个组件的 ``start()`` 与面板 / 设置窗的构造
    都得等着。判据是「探针一次都没被调」且「点火已登记」——后者保证这一轮不会
    被漏掉，只是挪到了 worker 线程。
    """
    fired: list[str] = []
    plugin = build(dirty=lambda pid, reason: fired.append(reason))
    calls: list[int] = []
    monkeypatch.setattr(bt_state, "probe", lambda: calls.append(1))

    plugin.start()
    assert calls == []            # 启动期一次现场都没读
    assert fired == ["start"]     # 但登记了点火，worker 随即补上那一轮


def test_declaration_surface():
    plugin = _bare()
    assert (plugin.id, plugin.name) == ("bt_guard", "蓝牙看护")
    assert plugin.run_mode == RUN_ON_DEMAND, "本组件没有节拍 —— 只看睡眠 / 唤醒两个时刻"
    assert plugin.failure_policy == FAIL_LOG, "只读探针失败不该熔断整个组件"
    assert plugin.api_version == 3
    assert plugin.order == 85, "顺序号必须唯一（80 = 音频看护，90 = 新组件默认位）"

    assert plugin.component().icon is IconName.MOUSE
    spec = plugin.state_specs()[0]
    assert spec.icon is IconName.MOUSE
    assert spec.stale_after is None, "按需型：两次体检隔几小时是正常的，别按周期判失联"

    assert len(plugin.row_specs()) == 1
    assert not plugin.resources().data_files, "本组件不写数据文件、不起守护"
    assert not plugin.resources().root_binaries, "助手免特权，不进 A 类 root 封闭集"


def test_states_are_a_closed_set_with_meaningful_levels():
    """取值闭集完整、起点在集合内，且级别不是清一色 —— 界面全靠级别色区分处境。"""
    ids = [opt.id for opt in plugin_mod.STATES]
    assert len(ids) == len(set(ids)), "取值名重复"

    spec = _bare().state_specs()[0]
    assert {opt.id for opt in spec.states} == set(ids)
    assert spec.initial_value in ids

    levels = {opt.id: opt.level for opt in plugin_mod.STATES}
    assert levels["ok"] is StateLevel.OK
    assert levels["recover_failed"] is StateLevel.ERR      # ERR 不受用户阈值限制，必上屏
    assert levels["lost"] is StateLevel.WARN
    assert levels["bt_off"] is StateLevel.IDLE, "蓝牙关着是用户的选择，不是故障"
    assert levels["no_baseline"] is StateLevel.IDLE

    # 取值图标一律回落主图标：这些说的是同一只设备的不同处境，靠级别色区分就够
    assert all(opt.icon is None for opt in plugin_mod.STATES)


def test_config_defaults_are_declared_once(build):
    """默认值真源在 ``config_schema``（§9.5）：属性与它同源，不另写一份 fallback。"""
    plugin = build()
    schema = plugin.config_schema()
    assert {f.key for f in schema.fields} == {"device", "auto_reconnect", "notify_enabled"}
    assert schema.defaults() == {"device": "", "auto_reconnect": True, "notify_enabled": True}

    assert plugin.device == MOUSE
    assert plugin.auto_reconnect is True
    assert plugin.notify_enabled is True


def test_defaults_hold_without_any_plugin_config(build):
    """配置段里什么都没有时的默认值，就是 ``config_schema`` 声明的那份。"""
    plugin = build(config_extra="")
    assert plugin.device == ""
    assert plugin.auto_reconnect is True
    assert plugin.notify_enabled is True


def test_writing_a_setting_asks_for_a_rerun_instead_of_replaying_the_old_verdict(build):
    """改完配置必须请框架重跑一轮 —— 否则面板会停在「上一次 tick 算出来的结论」。

    2026-09-23 的真实现场：配置里躺着选好的设备、在这台机器上判出来其实是「设备在线」，
    而面板那一行一直写着「还没选要看的设备」—— 那是**启动那一轮**（那时还没选设备）
    算出来的；本组件没有节拍，之后没人再判过。缺的正是「写完请框架点火」这一步。

    反向那半也要钉住：**不许**改成「就地重判一遍再 publish_snapshot()」——这会顺手把
    两个子进程的 IO 带进主线程（这三个方法是从设置页的信号里直接进来的）；而且本组件
    没有 ``_snapshot()``，那条调用**什么都不广播**，只会给人「已经刷新过了」的错觉。
    """
    dirty: list = []
    published: list[str] = []
    plugin = build(dirty=lambda pid, reason: dirty.append((pid, reason)))
    plugin.publish_snapshot = lambda snap=None, key="main": published.append(key)

    plugin.set_device(PAD)
    plugin.set_auto_reconnect(False)
    plugin.set_notify_enabled(False)

    assert plugin.device == PAD
    assert dirty == [("bt_guard", "device_changed"),
                     ("bt_guard", "auto_reconnect_changed"),
                     ("bt_guard", "notify_enabled_changed")]
    assert published == [], "改完配置不该就地重播旧结论 —— 要的是重新判定，不是重播"

    plugin.set_device(PAD)                               # 已是该值 ⇒ 不重复点火
    assert len(dirty) == 3


# ══════════════════════════════════════════════════════════════════════
# 五、通知：只打扰「修不好」那一种
# ══════════════════════════════════════════════════════════════════════

class _Hub:
    """通知中枢替身：记下 (插件 id, 请求)。"""

    def __init__(self, ok=True):
        self.ok = ok
        self.sent: list[tuple[str, NotificationRequest]] = []

    def send(self, plugin_id, request):
        self.sent.append((plugin_id, request))
        return self.ok


def _lose_and_wake(plugin, rig, *, recovered: bool):
    """走一轮「睡前在、醒后不在」的完整流程。"""
    rig.restart_result = ((True, "已重开蓝牙，设备回来了") if recovered
                          else (False, "蓝牙重开过了，但 60 秒内没等到设备回来"))
    rig.set(_snap(here=True))
    _sleep(plugin)
    plugin.tick()
    _wake(plugin)
    rig.set(_snap(here=False))
    return plugin.tick()


def test_a_successful_auto_fix_stays_silent(build):
    """自动修好了就该安静 —— 用户要的是鼠标能用，不是一条「我修好了」的通知。"""
    hub = _Hub()
    plugin = build(hub=hub)
    report = _lose_and_wake(plugin, build.rig, recovered=True)
    assert report.state == "ok"
    assert hub.sent == []


def test_only_the_unfixable_case_alerts(build):
    hub = _Hub()
    plugin = build(hub=hub)
    _lose_and_wake(plugin, build.rig, recovered=False)

    assert [(pid, r.key) for pid, r in hub.sent] == [("bt_guard", "bt_guard.recover_failed")]
    assert [a.key for a in hub.sent[0][1].actions] == ["retry"], "「修不好」时得给用户一个手动的出口"


def test_notifications_can_be_switched_off(build):
    hub = _Hub()
    plugin = build(_cfg(notify_enabled=False), hub=hub)
    _lose_and_wake(plugin, build.rig, recovered=False)
    assert hub.sent == []


# ══════════════════════════════════════════════════════════════════════
# 六、诊断文本
# ══════════════════════════════════════════════════════════════════════

def test_diagnostics_text_shows_both_points_of_the_comparison(build):
    """判据是**两个时刻的比对** ⇒ 诊断必须把两个点都留得住。

    「留得住」这三个字是自检出来的真缺陷：睡前基线在醒来比对的那一刻就被消费掉
    （那是它的设计：它答的是「下一拍要不要拆机」），诊断文本若直接读它，就会在**刚刚
    做过一次掉线判定之后**显示「（本次没记到）」—— 恰好把用户最想核实的那一次说成没数据。
    所以「上一次比了什么」是**另一个字段**。
    """
    plugin = build()
    rig = build.rig
    rig.set(_snap(here=True))
    plugin.tick()
    _sleep(plugin)
    plugin.tick()
    _wake(plugin)
    rig.set(_snap(here=False))
    plugin.tick()

    text = plugin.diagnostics_text()
    assert "上一次比对 = 睡前 在 ⇒ 醒后 不在" in text
    assert "本次运行见过它 = True" in text
    assert MOUSE in text                 # 地址按 ``XX:XX:…`` 给人看
    assert "HID 层" in text              # 主判据那一层
    assert "可执行位" in text            # 助手的现状


def test_diagnostics_text_says_so_when_no_boundary_was_crossed(build):
    plugin = build()
    build.rig.set(_snap(here=True))
    plugin.tick()
    assert "还没跨过睡眠边界" in plugin.diagnostics_text()


def test_diagnostics_text_survives_a_probe_that_read_nothing(build):
    build.rig.set(_snap(ok=False, reason="system_profiler 没给出蓝牙数据"))
    plugin = build()
    plugin.tick()
    text = plugin.diagnostics_text()
    assert "system_profiler 没给出蓝牙数据" in text
    assert "Traceback" not in text


# ══════════════════════════════════════════════════════════════════════
# 七、设置页（下拉填设备 ＋ 两个开关写回）
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture
def page(build, qtbot, monkeypatch):
    """构建设置页；下拉的数据源换成固定清单（不读真实系统现场）。"""
    monkeypatch.setattr(bt_state, "paired_devices",
                        lambda: [(PAD_N, "我的 iPad"), (MOUSE_N, "MX Master 3S")])
    plugin = build()
    widget = plugin.settings_page().build()
    qtbot.addWidget(widget)
    return plugin, widget


def test_settings_page_offers_the_paired_devices_and_selects_the_configured_one(page):
    """让用户手抄一串十六进制地址是没人会用的 ⇒ 必须能在下拉里挑。"""
    _plugin, widget = page
    combo = widget.findChildren(ComboField)[0]
    labels = [combo.itemText(i) for i in range(combo.count())]
    assert labels[0] == "（不监控）"
    assert any("MX Master 3S" in t for t in labels)
    assert any("我的 iPad" in t for t in labels)
    assert combo.currentData() == MOUSE


def test_settings_page_picking_another_device_writes_it_through(page):
    plugin, widget = page
    combo = widget.findChildren(ComboField)[0]
    combo.setCurrentIndex(combo.findData(PAD_N))
    assert plugin.device == PAD_N


def test_settings_page_selection_refreshes_the_row_right_away(build, qtbot, monkeypatch):
    """在这一页挑完设备 ⇒ 面板那一行**立刻**重判，而不是等到下次睡眠 / 唤醒。

    接上一条（写穿只是写进了配置文件）：本组件没有节拍，没人点火就没人重判 —— 用户
    看到的就是「选了，但界面还说我没选」。所以要爬到真实入口上验一遍。
    """
    monkeypatch.setattr(bt_state, "paired_devices",
                        lambda: [(PAD_N, "我的 iPad"), (MOUSE_N, "MX Master 3S")])
    dirty: list = []
    plugin = build(dirty=lambda pid, reason: dirty.append((pid, reason)))
    widget = plugin.settings_page().build()
    qtbot.addWidget(widget)

    combo = widget.findChildren(ComboField)[0]
    combo.setCurrentIndex(combo.findData(PAD_N))

    assert plugin.device == PAD_N
    assert dirty == [("bt_guard", "device_changed")]


def test_settings_page_keeps_a_configured_device_that_left_the_paired_list(build, qtbot,
                                                                          monkeypatch):
    """配置里那只已经不在配对列表里 ⇒ 仍列为可选项并选中。

    否则用户一打开这一页，配置就被下面某次无关的改动悄悄换成别的设备。
    """
    monkeypatch.setattr(bt_state, "paired_devices", lambda: [(PAD_N, "我的 iPad")])
    plugin = build()
    widget = plugin.settings_page().build()
    qtbot.addWidget(widget)

    combo = widget.findChildren(ComboField)[0]
    assert combo.currentData() == MOUSE
    assert "已不在配对列表" in combo.currentText()


def test_settings_page_switches_write_through_and_read_back(page):
    """两枚开关各写各的（顺带钉住行的次序：上面那枚是自动重连、下面是通知）。"""
    plugin, widget = page
    switches = widget.findChildren(Switch)
    assert len(switches) == 2

    switches[0].clicked.emit()
    assert plugin.auto_reconnect is False and plugin.notify_enabled is True
    assert switches[0].isChecked() is False            # 写盘后从插件回读，不留在骗人的位置

    switches[1].clicked.emit()
    assert plugin.notify_enabled is False and plugin.auto_reconnect is False


# ══════════════════════════════════════════════════════════════════════
# 八、能被框架发现（内置发现走 pkgutil 的 ``barkit.plugins.<name>.plugin``）
# ══════════════════════════════════════════════════════════════════════

def test_the_component_is_discovered_as_a_builtin():
    from barkit.core.registry import Registry

    found = Registry(Config(create_user_file=False)).discover()
    assert "bt_guard" in found, "没被发现 —— 目录名 / Plugin 类名/ 模块名三者对不上"
    assert found["bt_guard"].id == "bt_guard"


# ══════════════════════════════════════════════════════════════════════
# 九、面板被打开 ⇒ 重读一次现状（**不动手**）
# ══════════════════════════════════════════════════════════════════════

def test_panel_opened_refreshes_the_reading_without_touching_bluetooth(build):
    """开面板 ⇒ 把那一行拉回此刻的事实，但**绝不动手**。

    修的是「屏幕一直亮着时鼠标掉了，面板还写着设备在线」（2026-09-24 现场：掉了近两小时，
    那一行停在启动那轮）。本组件没有节拍，唯一能让它重跑的就是事件，而这件事不属于任何
    边界 —— 用户只是想看一眼。所以它必须走 ``_evaluate`` 里 ``not woke`` 那条普通路径：
    读现场、报现状、不碰蓝牙开关（「亮屏期间掉线先不管」是用户当天裁的口径）。
    """
    dirty: list = []
    plugin = build(dirty=lambda pid, reason: dirty.append((pid, reason)))
    rig = build.rig

    rig.set(_snap(here=True))
    plugin.tick()                        # 启动那轮：在线
    rig.set(_snap(here=False))           # 之后它掉了（屏幕一直亮着，没有任何事件）

    probes_before = rig.probes
    plugin.on_system_event(PANEL_OPENED_TOPIC, None)

    assert dirty == [("bt_guard", "panel_opened")]
    assert rig.probes == probes_before, "handler 里读了现场 —— 主线程上不许做 IO"

    assert plugin.tick().state == "offline", "面板那一行没被拉回此刻"
    assert rig.acts == [], "开个面板就把蓝牙断了一次"


def test_panel_opened_is_not_a_boundary_and_keeps_the_pending_baseline(build):
    """面板打开**不算一轮边界**：不置标志、不消费基线、也不解除唤醒去重。

    顺序恰好是真实里最险的那一种 —— 睡前那一拍还没跑完，用户先开了面板。若面板那条也去
    置 ``woke``（或顺手把标志清掉），「睡前」那一拍就没有基线了：醒来判不出「是这次掉
    的」，于是不动手、还不报错（整条判据静默失效）。这里逐格验它什么都没动。
    """
    dirty: list = []
    plugin = build(dirty=lambda pid, reason: dirty.append((pid, reason)))
    rig = build.rig

    rig.set(_snap(here=True))
    _sleep(plugin)
    plugin.on_system_event(PANEL_OPENED_TOPIC, None)     # 用户此刻开了面板
    assert plugin.tick().state == "ok"
    assert plugin._baseline is True, "面板那条把睡前标志吃掉了"

    _wake(plugin)                                        # 唤醒仍然要能点火
    rig.set(_snap(here=False))
    assert plugin.tick().state == "ok"                   # 重开成功、设备回来
    assert rig.acts == [MOUSE]
    assert dirty == [("bt_guard", "sleep"), ("bt_guard", "panel_opened"), ("bt_guard", "wake")]
