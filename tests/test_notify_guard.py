"""notify_guard 测试（P2b 2.1~2.4）。

覆盖：
- backend 2.1：tmp plist 读写、defaults write 优先、kickstart 失败降级 killall、
  直接写文件回退、读失败返回 None
- signals 2.2：规则进程内存匹配、虚拟显示器指纹（预检短路 + profiler 匹配）
- decider 2.3：防抖写入、手动覆盖保持/信号变化接管、恢复自动跳过防抖、
  写失败清计数
- plugin 2.4：tick 编排（防抖后写 backend）、手动切换立即生效、事件总线广播
"""

from __future__ import annotations

import plistlib

import pytest

from barkit.core.config import Config
from barkit.core.contributions import gather_specs
from barkit.core.events import EventBus
from barkit.core.plugin import PluginContext
from barkit.plugins.notify_guard import backend as backend_mod
from barkit.plugins.notify_guard import opendisplay
from barkit.plugins.notify_guard import signals
from barkit.plugins.notify_guard.backend import DndMirroredBackend
from barkit.contribution.specs import (
    ActionOutcome,
    ActionResult,
    ComponentSpec,
    HotkeySpec,
    IconName,
    SettingsPageSpec,
    StateLevel,
    StateReport,
    StateSpec,
    StatusRowSpec,
)
from barkit.plugins.notify_guard.decider import Decider
from barkit.plugins.notify_guard.diagnostics import (
    Probe,
    RuleLine,
    diagnostic_text,
    format_probe,
)
from barkit.plugins.notify_guard.plugin import Plugin, STALE_S, _rule_reason
from barkit.shell.panel import StatusPanel
from barkit.shell.renderers.state_view import resolve


def _state_spec(plugin, key: str) -> StateSpec:
    """取回本插件某台状态机的声明（报告只报取值名，级别 / 图标 / 默认文案都在这儿）。"""
    return next(s for s in plugin.state_specs() if s.key == key)


class _StubContrib:
    """最小贡献桩：仅供 StatusPanel.set_components 注入状态规范（白盒测试）。"""

    def __init__(self, states):
        self._states = list(states)

    def states(self):
        return self._states

    def components(self):
        return []


# ──────────────────────────────────────────────
# 公共
# ──────────────────────────────────────────────

def _make_prefs(path, dnd_mirrored: bool):
    inner = plistlib.dumps({"dndMirrored": dnd_mirrored}, fmt=plistlib.FMT_BINARY)
    path.write_bytes(plistlib.dumps({"dnd_prefs": inner}, fmt=plistlib.FMT_BINARY))


class FakeProc:
    def __init__(self, rc=0, stderr=""):
        self.returncode = rc
        self.stderr = stderr
        self.stdout = ""

    def __getattr__(self, _):
        return ""

    def __bool__(self):
        return self.returncode == 0


class FakeBackend:
    """apply 只记录写入，不回真系统写。applied_allow 为当前（隐含的）allow 状态。"""

    def __init__(self, applied_allow: bool, fail: bool = False):
        self.applied_allow = applied_allow
        self.writes: list[bool] = []  # 记录的 suppress 值
        self.fail = fail

    def read(self) -> bool | None:
        return not self.applied_allow  # dndMirrored

    def apply(self, suppress: bool) -> None:
        if self.fail:
            raise backend_mod.BackendError("假的写失败")
        self.writes.append(suppress)
        self.applied_allow = not suppress


def _plugin_ctx(tmp_path, events=None, extra: str = "") -> PluginContext:
    # 自包含：显式给出 notify_guard 配置，不依赖 config.default.yaml 的插件段
    # （§9.5 后 notify 默认已迁至插件 config_schema，合并机制由 test_config 覆盖）。
    # ``extra`` 是**追加的 YAML 行**（含缩进与换行），供单个用例带上自己那一项配置。
    user = tmp_path / "config.yaml"
    user.write_text(
        "plugins:\n"
        "  notify_guard:\n"
        "    enabled: true\n"
        "    debounce: 2\n"
        "    rules:\n"
        "      - name: lark\n"
        "        reason: 飞书会议中\n"
        '        processes: ["Lark Helper (Iron)"]\n'
        "      - name: screencast\n"
        "        reason: 录屏软件运行中\n"
        "        processes: [OBS, Kap, zoom.us]\n" + extra,
        encoding="utf-8",
    )
    return PluginContext(config=Config(user_path=user), events=events)


def _plugin(tmp_path, monkeypatch, applied_allow=True, events=None,
            app_state="idle", virtual=False,
            extra: str = "") -> tuple[Plugin, FakeBackend]:
    """构造插件并**离线化三级线索**（绝不在测试里真调 AppKit / system_profiler）。

    ``app_state`` / ``virtual`` 是给 ``detect_display_state()`` 的两个输入：
    前者是「装没装 / 跑没跑」，后者是「第二屏在不在」——四态合成走真实路径，
    只是输入被替身接管。
    """
    fake = FakeBackend(applied_allow)
    monkeypatch.setattr(
        "barkit.plugins.notify_guard.plugin.DndMirroredBackend", lambda: fake
    )
    monkeypatch.setattr(
        "barkit.plugins.notify_guard.plugin.signals.detect_app_state", lambda: app_state
    )
    monkeypatch.setattr(
        "barkit.plugins.notify_guard.plugin.signals.detect_virtual_display",
        lambda: virtual,
    )
    monkeypatch.setattr(
        "barkit.plugins.notify_guard.plugin.signals.get_process_snapshot", lambda: []
    )
    plugin = Plugin(_plugin_ctx(tmp_path, events, extra))
    plugin.start()
    return plugin, fake


# ──────────────────────────────────────────────
# 2.1 backend
# ──────────────────────────────────────────────

def test_backend_read(tmp_path):
    prefs = tmp_path / "ncprefs.plist"
    _make_prefs(prefs, dnd_mirrored=True)
    b = DndMirroredBackend()
    orig = backend_mod.NCPREFS_PATH
    backend_mod.NCPREFS_PATH = prefs
    try:
        assert b.read() is True
    finally:
        backend_mod.NCPREFS_PATH = orig

def test_backend_read_missing_returns_none(tmp_path):
    b = DndMirroredBackend()
    orig = backend_mod.NCPREFS_PATH
    backend_mod.NCPREFS_PATH = tmp_path / "nope.plist"
    try:
        assert b.read() is None
    finally:
        backend_mod.NCPREFS_PATH = orig


def test_backend_read_is_cached_for_a_short_ttl(tmp_path, monkeypatch):
    """读有 TTL：面板每重算一次都解析一遍系统 plist 是主线程上的浪费。

    判据是「TTL 内盘上改了也读不到，TTL 过了才看得到」—— 缓存必须**会过期**，
    否则 desync 判定（守卫期望 vs 系统实际）就成了永远看旧值的假守。
    """
    prefs = tmp_path / "ncprefs.plist"
    _make_prefs(prefs, dnd_mirrored=True)
    monkeypatch.setattr(backend_mod, "NCPREFS_PATH", prefs)
    b = DndMirroredBackend()
    assert b.read() is True                    # 首次：真读

    _make_prefs(prefs, dnd_mirrored=False)     # 盘上改了，但 TTL 没过
    assert b.read() is True

    monkeypatch.setattr(backend_mod, "READ_TTL_S", 0.0)
    assert b.read() is False                   # TTL 过：重新读


def test_backend_apply_refreshes_the_cache(tmp_path, monkeypatch):
    """写完之后立刻读必须拿到新值——否则「刚切完」那一轮会报假 desync。"""
    prefs = tmp_path / "ncprefs.plist"
    _make_prefs(prefs, dnd_mirrored=False)
    monkeypatch.setattr(backend_mod, "NCPREFS_PATH", prefs)
    monkeypatch.setattr(backend_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(backend_mod, "run_cmd", lambda *a, **k: FakeProc(0))
    monkeypatch.setattr(DndMirroredBackend, "_restart_usernoted", lambda self: None)

    b = DndMirroredBackend()
    assert b.read() is False                   # 先把缓存填成 False
    b.apply(suppress=True)
    assert b.read() is True                    # 写后读：不再吃 TTL 里的旧值


def test_backend_apply_defaults_then_killall_fallback(tmp_path, monkeypatch):
    """defaults 成功；非 macOS 场景 kickstart 被拒 → 降级 killall。

    （macOS 上 kickstart 被 SIP 拦截会直接走 killall，此用例用非 Darwin 平台
    保留「kickstart 失败→降级」路径的覆盖。）
    """
    prefs = tmp_path / "ncprefs.plist"
    _make_prefs(prefs, dnd_mirrored=False)
    monkeypatch.setattr(backend_mod, "NCPREFS_PATH", prefs)
    monkeypatch.setattr(backend_mod.platform, "system", lambda: "Linux")
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=10, **kw):
        calls.append(list(cmd))
        if cmd[0] == "launchctl":
            return FakeProc(150, "Operation not permitted while System Integrity Protection is engaged")
        return FakeProc(0)

    monkeypatch.setattr(backend_mod, "run_cmd", fake_run)
    DndMirroredBackend().apply(suppress=True)

    cmd0 = [c for c in calls if c[0] == "defaults"]
    assert cmd0 and "dnd_prefs" in cmd0[0] and "-data" in cmd0[0]
    assert any(c[0] == "launchctl" and c[1] == "kickstart" for c in calls)
    assert ["killall", "usernoted"] in calls  # kickstart 失败后降级
    # defaults 的 -data hex 反解后应含 dndMirrored=True（真实路径由 cfprefsd 落盘）
    hex_data = cmd0[0][cmd0[0].index("-data") + 1]
    inner = plistlib.loads(bytes.fromhex(hex_data))
    assert inner["dndMirrored"] is True


def test_backend_apply_defaults_failure_direct_write(tmp_path, monkeypatch):
    """defaults 失败 → 直接写文件 + killall cfprefsd。"""
    prefs = tmp_path / "ncprefs.plist"
    _make_prefs(prefs, dnd_mirrored=False)
    monkeypatch.setattr(backend_mod, "NCPREFS_PATH", prefs)
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=10, **kw):
        calls.append(list(cmd))
        if cmd[0] == "defaults":
            return FakeProc(1, "Could not write domain")
        if cmd[0] == "launchctl":
            return FakeProc(150, "SIP")
        return FakeProc(0)

    monkeypatch.setattr(backend_mod, "run_cmd", fake_run)
    DndMirroredBackend().apply(suppress=True)
    assert ["killall", "cfprefsd"] in calls
    assert _read_mirrored(prefs) is True


def test_backend_apply_restart_total_failure_raises(tmp_path, monkeypatch):
    """killall 返回真实错误（非 exit 1）→ BackendError。"""
    prefs = tmp_path / "ncprefs.plist"
    _make_prefs(prefs, dnd_mirrored=False)
    monkeypatch.setattr(backend_mod, "NCPREFS_PATH", prefs)

    def fake_run(cmd, timeout=10, **kw):
        if cmd[0] == "launchctl":
            return FakeProc(150, "SIP")
        if cmd[0] == "killall":
            # exit 2 = 真实错误（如权限被拒）；区别于 exit 1（无进程，良性）
            return FakeProc(2, "Operation not permitted")
        return FakeProc(0)

    monkeypatch.setattr(backend_mod, "run_cmd", fake_run)
    from barkit.plugins.notify_guard.backend import BackendError

    with pytest.raises(BackendError):
        DndMirroredBackend().apply(True)


def test_backend_apply_restart_no_process_is_ok(tmp_path, monkeypatch):
    """killall 返回 exit 1（未命中进程，usernoted 当前未运行）属于良性：
    prefs 已由 defaults write 经 cfprefsd IPC 生效，无需重启，不抛异常。
    这正是连续快速切换时 kill 掉 usernoted、launchd 尚未拉活那一瞬的窗口。"""
    prefs = tmp_path / "ncprefs.plist"
    _make_prefs(prefs, dnd_mirrored=False)
    monkeypatch.setattr(backend_mod, "NCPREFS_PATH", prefs)

    calls = []

    def fake_run(cmd, timeout=10, **kw):
        calls.append(cmd)
        if cmd[0] == "defaults":
            return FakeProc(0)
        if cmd[0] == "killall":
            return FakeProc(1, "No matching processes belonging to you were found")
        return FakeProc(0)

    monkeypatch.setattr(backend_mod, "run_cmd", fake_run)
    # 不应抛异常（此前会把 exit 1 误判为致命）
    DndMirroredBackend().apply(True)
    assert [c[0] for c in calls][:1] == ["defaults"]


def _read_mirrored(path):
    outer = plistlib.loads(path.read_bytes())
    return plistlib.loads(outer["dnd_prefs"])["dndMirrored"]


# ──────────────────────────────────────────────
# 2.2 signals
# ──────────────────────────────────────────────

def test_match_rules_substring():
    snap = [
        "/applications/lark.app/contents/frameworks/lark framework.framework/versions/109.0.5414.128/helpers/lark helper (iron).app/contents/macos/lark helper (iron)",
        "/usr/libexec/loginwindow",
    ]
    rules = [
        {"name": "lark", "reason": "飞书会议中", "processes": ["Lark Helper (Iron)"]},
        {"name": "screencast", "reason": "录屏软件运行中", "processes": ["OBS", "Kap", "zoom.us"]},
    ]
    hits = signals.match_rules(snap, rules)
    assert [h["name"] for h in hits] == ["lark"]


def test_match_rules_empty_no_hit():
    assert signals.match_rules([], [{"name": "lark", "processes": ["Lark Helper"]}]) == []
    assert signals.match_rules(["anything"], [{"name": "x", "processes": []}]) == []


def test_detect_virtual_display_profile(monkeypatch):
    signals.reset_cache()
    monkeypatch.setattr(signals, "_display_signature", lambda: ((1, False), (2, True)))
    payload = json_with_display("5043", "4f53")
    monkeypatch.setattr(signals.subprocess, "check_output", lambda *a, **k: payload)
    assert signals.detect_virtual_display() is True


def test_detect_virtual_display_no_fingerprint(monkeypatch):
    signals.reset_cache()
    monkeypatch.setattr(signals, "_display_signature", lambda: ((1, False), (2, True)))
    monkeypatch.setattr(
        signals.subprocess, "check_output",
        lambda *a, **k: json_with_display("610", "a053"),
    )
    assert signals.detect_virtual_display() is False


def test_detect_virtual_display_preflight_shortcut(monkeypatch):
    """签名无非内建屏 → 短路，不调 system_profiler。"""
    signals.reset_cache()
    called = []
    monkeypatch.setattr(signals, "_display_signature", lambda: ((2, True),))
    monkeypatch.setattr(signals.subprocess, "check_output", lambda *a, **k: called.append(1) or "")
    assert signals.detect_virtual_display() is False
    assert called == []


def test_detect_virtual_display_ttl_cache(monkeypatch):
    """签名未变 → TTL 内不重复调 system_profiler（资源降频核心）。"""
    signals.reset_cache()
    calls = []
    monkeypatch.setattr(signals, "_display_signature", lambda: ((1, False),))
    monkeypatch.setattr(
        signals.subprocess, "check_output",
        lambda *a, **k: calls.append(1) or json_with_display("5043", "4f53"),
    )
    assert signals.detect_virtual_display() is True   # 签名变化 → 调一次
    assert len(calls) == 1
    assert signals.detect_virtual_display() is True   # TTL 内 → 缓存
    assert signals.detect_virtual_display() is True
    assert len(calls) == 1


def json_with_display(vid: str, pid: str) -> str:
    import json

    return json.dumps({
        "SPDisplaysDataType": [{
            "spdisplays_ndrvs": [{
                "_name": "T",
                "_spdisplays_display-vendor-id": vid,
                "_spdisplays_display-product-id": pid,
            }],
        }],
    })


# ──────────────────────────────────────────────
# 2.3 decider
# ──────────────────────────────────────────────

def test_debounce_write_after_n_consistent():
    d = Decider(debounce=2, applied=True)  # 当前 allow=True
    r1 = d.step(False, False, [])          # 信号 → 静音
    assert r1.action == "wait" and r1.allow is False
    r2 = d.step(False, False, [])          # 第二周期一致 → 写
    assert r2.action == "write" and r2.allow is False
    d.confirm_write(False)
    assert d.step(False, False, []).action == "none"  # 稳定不再写


def test_debounce_requires_n_consistent_cycles():
    d = Decider(debounce=3, applied=True)
    assert d.step(False, False, []).action == "wait"   # 周期 1
    assert d.step(False, False, []).action == "wait"   # 周期 2
    r = d.step(False, False, [])
    assert r.action == "write" and r.allow is False     # 周期 3 连续一致 → 写
    d.confirm_write(False)
    assert d.step(False, False, []).action == "none"    # 稳定不再写
    # 信号翻转成新值（与已写入值不同）→ 重新防抖累计
    assert d.step(True, False, []).action == "wait"


def test_manual_toggle_holds_until_combo_change():
    d = Decider(debounce=2, applied=True)
    assert d.manual_toggle(False, False) is False  # 立即静音
    d.confirm_write(False)
    assert d.mode == "manual"
    # 信号不变 → 手动保持
    r = d.step(False, False, [])
    assert r.mode == "manual" and r.action == "none" and r.allow is False
    # 信号组合变化 → 自动接管（防抖重新累计）
    r = d.step(True, False, [])
    assert r.mode == "auto" and r.action == "wait" and r.allow is True


def test_manual_toggle_again_flips_within_manual():
    d = Decider(debounce=2, applied=True)
    d.manual_toggle(False, False)   # → False 静音
    d.confirm_write(False)
    assert d.manual_toggle(False, False) is True  # 再按 → 允许，仍手动
    d.confirm_write(True)
    assert d.mode == "manual"


def test_resume_auto_immediate_write():
    d = Decider(debounce=2, applied=True)
    d.manual_toggle(False, False)   # 手动静音
    d.confirm_write(False)
    # 信号仍要静音 → 恢复自动后无写入
    r = d.resume_auto(False, False, [])
    assert r.mode == "auto" and r.action == "none"
    # 信号要允许，且与手动值不同 → 跳过防抖立即写
    r = d.resume_auto(True, False, [])
    assert r.action == "write" and r.allow is True


def test_write_failure_resets_pending():
    d = Decider(debounce=2, applied=True)
    d.step(False, False, [])  # pending count=1
    d.on_write_failure()      # 写失败
    assert d.pending is None and d.pending_count == 0
    assert d.step(False, False, []).action == "wait"  # 从阈值重新累计


def test_status_shape():
    d = Decider(debounce=2, applied=True)
    st = d.status()
    assert set(st) >= {"mode", "allow", "applied", "pending", "pending_count", "debounce", "reason"}
    assert st["mode"] == "auto"


# ──────────────────────────────────────────────
# 2.4 plugin
# ──────────────────────────────────────────────

def test_tick_writes_after_debounce(tmp_path, monkeypatch):
    plugin, fake = _plugin(tmp_path, monkeypatch)
    plugin.tick()                      # 首轮防抖
    assert fake.writes == []
    plugin.tick()                      # 第二轮一致 → 写静音
    assert fake.writes == [True]
    assert fake.applied_allow is False
    plugin.tick()                      # 稳定不再写
    assert fake.writes == [True]
    plugin.stop()


def test_tick_manual_override_and_takeover(tmp_path, monkeypatch):
    plugin, fake = _plugin(tmp_path, monkeypatch)
    plugin.tick()
    plugin.tick()                      # 自动静音（s1=F s2=F → suppress=True）
    assert fake.writes == [True]
    # 手动切换 → 立即允许通知（一次性覆盖）
    plugin.toggle_action()
    assert fake.writes[-1] is False    # suppress=False
    assert plugin.decider.mode == "manual"
    # 信号组合变化：录屏进程出现（s2=True）→ 自动接管，防抖后写回静音
    monkeypatch.setattr(
        "barkit.plugins.notify_guard.plugin.signals.get_process_snapshot",
        lambda: ["/applications/obs.app/contents/macos/obs"],
    )
    plugin.tick()                       # 首轮：自动接管 wait（desired False ≠ applied True）
    assert plugin.decider.mode == "auto"
    assert plugin.decider.pending is False
    plugin.tick()                       # 第二轮一致 → 写静音
    assert fake.writes[-1] is True


def test_tick_write_failure_melting(tmp_path, monkeypatch):
    """写失败抛 BackendError → scheduler 计失败（execute 熔断路径由 scheduler 测试覆盖）。"""
    plugin, fake = _plugin(tmp_path, monkeypatch)
    from barkit.plugins.notify_guard.backend import BackendError

    def boom(suppress):
        raise BackendError("强制失败")

    fake.apply = boom
    plugin.tick()   # 防抖第一轮
    with pytest.raises(BackendError):
        plugin.tick()  # 第二轮写失败
    assert plugin.decider.pending is None  # 计数已清，不会连锤


def test_state_published_on_manual_change(tmp_path, monkeypatch, qtbot):
    """§5.3 闭环：手动切换经 publish_snapshot 广播**三台机**各自 topic 的快照。

    v3 下 tick() 自身不再自 publish——调度权归框架，由 scheduler 对返回值
    自动 publish；此处测手动状态变化路径（toggle_action → _publish_all）。
    三台机各有一条 topic（``plugin.notify_guard.status.<key>``），订阅方按 key 取。
    """
    events = EventBus()
    plugin, fake = _plugin(tmp_path, monkeypatch, events=events)
    received: dict[str, list] = {"display": [], "notify": [], "automation": []}
    for key, box in received.items():
        events.subscribe(plugin.status_topic(key), box.append)
    plugin.toggle_action()  # 手动静音 → 三台机快照经事件总线广播
    qtbot.waitUntil(lambda: all(box for box in received.values()), timeout=2000)
    notify = received["notify"][-1]
    assert isinstance(notify, StateReport)
    assert notify.state == "manual"
    # manual 是独立取值（设计 §4.1）：词条是「手动静音」，与自动静音的「通知静音」不共用。
    # 报告留空 summary ⇒ 框架回退声明的默认词条（决议 D13 的覆盖语义）
    assert notify.summary == ""
    assert resolve(_state_spec(plugin, "notify"), notify).text == "手动静音"
    display = resolve(_state_spec(plugin, "display"), received["display"][-1])
    assert display.icon in (IconName.DISPLAY_ON, IconName.DISPLAY_OFF)
    assert received["automation"][-1].state == "running"
    plugin.stop()


def test_tick_returns_three_machines(tmp_path, monkeypatch):
    """§5.3 闭环：tick() 返回 ``{key: StateReport}``（三台机各一条 topic，只报取值名）。"""
    plugin, fake = _plugin(tmp_path, monkeypatch)
    plugin.tick()
    plugin.tick()          # 两轮一致 → 写静音
    snaps = plugin.tick()
    assert set(snaps) == {"display", "notify", "automation"}
    assert all(isinstance(s, StateReport) for s in snaps.values())
    notify = snaps["notify"]
    assert resolve(_state_spec(plugin, "notify"), notify).level is StateLevel.WARN   # 静音
    # 文案只有一份（决议 D18）：取值词条 + 该取值自己的补充说明，合成同一句 summary
    # （decider 给的信号侧原因「无虚拟显示器/录屏活动」= s1/s2 都为假）
    assert notify.summary == "通知静音 · 无虚拟显示器/录屏活动"
    display = resolve(_state_spec(plugin, "display"), snaps["display"])
    assert display.text == "OpenDisplay 未运行"        # app_state=idle
    assert display.icon is IconName.DISPLAY_OFF
    assert snaps["automation"].state == "running"
    plugin.stop()


def test_panel_row_via_contributions(tmp_path, monkeypatch, qtbot):
    """框架按 StatusRowSpec 渲染：Switch 回显生效态；副标题走**行级** subtitle（§2.7/D24）。"""
    plugin, fake = _plugin(
        tmp_path, monkeypatch, app_state=signals.DISPLAY_RUNNING, virtual=True
    )
    collected = gather_specs(plugin, plugin.id)   # 经框架收集，plugin_id 一并注入
    row_spec = next(s for s in collected if isinstance(s, StatusRowSpec))
    states = [s for s in collected if isinstance(s, StateSpec)]

    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.set_components([], _StubContrib(states))
    panel.sync_status_rows([row_spec])
    row = panel._row_widgets["notify_guard"][0]
    assert len(row._switches) == 1, "声明式 SwitchSpec → 框架渲染一枚 Switch"
    sw = row._switches[0][0]

    def push_all():
        plugin.tick()
        for s in states:
            panel.update_state_snapshot(s, plugin._all_snapshots()[s.key])

    # 第二屏在线 ＋ 无录屏 → 允许通知（场景 1），取值图标 = 实心屏
    push_all()
    assert sw.isChecked() is True
    # 断言取 ``subtitle_text()``（未省略的全文）：``ElideLabel.text()`` 是按当前宽度省略过的
    # 展示串，离屏面板行很窄，拿它比对等于在测控件宽度。
    assert row.subtitle_text("") == "iPad 已连接 · 通知正常"
    # 行**角标**画取值图标（决议 D16）：主位固定是组件身份图标（BELL）
    assert row._badge_icon is IconName.DISPLAY_ON
    # 手动切换 → 立即静音：Switch 关、副标题换成"手动覆盖中"（场景 4）
    plugin.toggle_action()
    push_all()
    assert sw.isChecked() is False
    assert row.subtitle_text("") == "手动覆盖中 · 自动接管已挂起"
    panel.deleteLater()


def test_toggle_action_reports_the_failure_instead_of_going_quiet(tmp_path, monkeypatch):
    """热键 / 面板开关写失败 ⇒ 必须把结局**交出去**（成功则闭嘴）。

    原先只记日志，指望下一轮 tick 报的 ``desync`` 来告诉用户。但那一轮要等一个
    轮询周期（3s），而用户按 ``⌃⌥M`` 或拨开关的那一刻：开关弹回原位、界面一句话
    也没有。状态行是兜底，不该是唯一出口。
    """
    from barkit.contribution.specs import ActionOutcome

    plugin, fake = _plugin(tmp_path, monkeypatch)
    assert plugin.toggle_action() is None          # 成功：状态本身就看得见，不多话

    fake.fail = True
    result = plugin.toggle_action()
    assert result is not None
    assert result.outcome is ActionOutcome.FAILED
    assert result.text.strip()                     # 说了人话，不是空串


def test_hotkeys_contract(tmp_path, monkeypatch):
    plugin, fake = _plugin(tmp_path, monkeypatch)
    specs = list(plugin.hotkeys())
    assert any(s.name == "notify_toggle" and s.callback == plugin.toggle_action for s in specs)
    plugin.stop()


def test_contributions_v2_shape(tmp_path, monkeypatch):
    """v3 贡献清单齐全：状态行 + **三台状态机** + 徽标规则 + 设置页 + 热键。"""
    plugin, fake = _plugin(tmp_path, monkeypatch)
    row = plugin.row_specs()[0]
    assert row.key == "main"
    assert row.on_click == plugin.open_opendisplay      # 整行 = 打开 OpenDisplay（§6.5-A）
    assert row.subtitle == plugin._row_subtitle         # 行级副标题（§2.7 / D24）
    assert row.mode is None                             # 自动化徽标已撤出面板（裁决项 6）
    specs = gather_specs(plugin, plugin.id)             # 全声明点产出（决议 D20）
    states = {s.key: s for s in specs if isinstance(s, StateSpec)}
    assert set(states) == {"display", "notify", "automation"}
    # 声明顺序 = 关注度顺序（决议 D1）：同级平局取先声明者，故身份主状态 display
    # 必须写在最前、装置配置态 automation 写在最后
    assert [s.key for s in specs if isinstance(s, StateSpec)] == [
        "display", "notify", "automation"
    ]
    assert all(s.stale_after == STALE_S for s in states.values())
    assert states["display"].icon is IconName.DISPLAY_OFF  # ★主图标：声明它才进菜单栏候选
    # 装置配置态不进托盘竞争（§4.2）：取值级别是 OK / OFF / IDLE，够不到阈值，
    # 也不必额外标 alerts（D17 之后「进不进托盘」由取值自己说）
    assert all(opt.alerts_on is False for opt in states["automation"].states)
    comp = next(s for s in specs if isinstance(s, ComponentSpec))
    # 名字**不在声明里写第二遍**（决议 D14）：省略即取 Plugin.name，由注册中心补齐
    assert comp.name == "" and comp.icon is IconName.BELL
    # 静音（WARN）**显式**标了 alerts（D17）：它原来是托盘那条「静」字符的特殊通道，
    # 现在是普通取值 —— 值得让用户看见这件事得自己写出来
    notify_opts = {o.id: o for o in states["notify"].states}
    assert notify_opts["muted"].alerts is True and notify_opts["muted"].alerts_on is True
    assert any(isinstance(s, SettingsPageSpec) for s in specs)
    assert any(isinstance(s, HotkeySpec) and s.name == "notify_toggle" for s in specs)
    plugin.stop()


def test_plugin_name_changed_id_frozen():
    """改名只改 ``name``：``id`` / 目录名 / 配置段键全部冻结（设计 §1.1 / 裁决项 1）。"""
    assert Plugin.name == "录屏通知"
    assert Plugin.id == "notify_guard"

# ──────────────────────────────────────────────
# 2.5 三级线索与 display 四态（设计 §2.3 / §3.2）
# ──────────────────────────────────────────────

def test_detect_display_state_short_circuits_when_app_not_running(monkeypatch):
    """App 没跑（idle / absent）→ **不调** S1 探测（后者在插拔时会 fork）。"""
    signals.reset_cache()
    probed = []
    monkeypatch.setattr(signals, "detect_app_state", lambda: signals.DISPLAY_IDLE)
    monkeypatch.setattr(signals, "detect_virtual_display", lambda: probed.append(1) or True)
    assert signals.detect_display_state() == signals.DISPLAY_IDLE
    assert probed == [], "App 没跑时不该探第二屏"


def test_detect_display_state_running_without_second_screen(monkeypatch):
    """进程在跑但没侦测到第二屏 → running（**含 mirror 盲区**，§2.3 已裁定接受）。"""
    signals.reset_cache()
    monkeypatch.setattr(signals, "detect_app_state", lambda: signals.DISPLAY_RUNNING)
    monkeypatch.setattr(signals, "detect_virtual_display", lambda: False)
    assert signals.detect_display_state() == signals.DISPLAY_RUNNING


def test_detect_display_state_extended(monkeypatch):
    signals.reset_cache()
    monkeypatch.setattr(signals, "detect_app_state", lambda: signals.DISPLAY_RUNNING)
    monkeypatch.setattr(signals, "detect_virtual_display", lambda: True)
    assert signals.detect_display_state() == signals.DISPLAY_EXTENDED


def test_detect_app_state_uses_ttl_cache(monkeypatch):
    """TTL 内不重复探测（pyobjc 调用降频，与 S1 同一套纪律）。"""
    signals.reset_cache()
    calls = []
    monkeypatch.setattr(
        signals, "probe_app_state", lambda: calls.append(1) or signals.DISPLAY_IDLE
    )
    assert signals.detect_app_state() == signals.DISPLAY_IDLE
    assert signals.detect_app_state() == signals.DISPLAY_IDLE
    assert len(calls) == 1
    assert signals.detect_app_state(ttl=0) == signals.DISPLAY_IDLE
    assert len(calls) == 2


def test_reset_cache_clears_app_state(monkeypatch):
    signals.reset_cache()
    monkeypatch.setattr(signals, "probe_app_state", lambda: signals.DISPLAY_ABSENT)
    assert signals.detect_app_state() == signals.DISPLAY_ABSENT
    signals.reset_cache()
    monkeypatch.setattr(signals, "probe_app_state", lambda: signals.DISPLAY_RUNNING)
    assert signals.detect_app_state() == signals.DISPLAY_RUNNING


# ──────────────────────────────────────────────
# 2.6 opendisplay 后端（装置事实 + 动作）
# ──────────────────────────────────────────────

def _fake_appkit(monkeypatch, running=(), installed=()):
    """把 ``AppKit`` 换成最小替身（不依赖真实 NSWorkspace 状态）。"""
    import sys
    import types

    class _App:
        def __init__(self, bid):
            self._bid = bid

        def bundleIdentifier(self):
            return self._bid

    class _WS:
        def runningApplications(self):
            return [_App(b) for b in running]

        def URLForApplicationWithBundleIdentifier_(self, bid):
            return "/Applications/OpenDisplay.app" if bid in installed else None

    ws = _WS()
    monkeypatch.setitem(
        sys.modules, "AppKit",
        types.SimpleNamespace(NSWorkspace=types.SimpleNamespace(sharedWorkspace=lambda: ws)),
    )


def test_app_state_absent_when_appkit_unavailable(monkeypatch):
    """无 AppKit（CI / 非 macOS）→ absent：零 fork 前提下的保守答案。"""
    import builtins

    real = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "AppKit":
            raise ImportError("no AppKit")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert opendisplay.app_state() == opendisplay.APP_ABSENT


def test_app_state_matches_debug_build_by_prefix(monkeypatch):
    """Debug 构建 ``...mac.debug`` 也算"在跑"（bundle id 必须**前缀**匹配，§2.3）。"""
    _fake_appkit(monkeypatch, running=["com.peetzweg.opensidecar.mac.debug"])
    assert opendisplay.app_state() == opendisplay.APP_RUNNING


def test_app_state_idle_when_installed_but_not_running(monkeypatch):
    _fake_appkit(monkeypatch, installed=["com.peetzweg.opensidecar.mac"])
    assert opendisplay.app_state() == opendisplay.APP_IDLE


def test_app_state_absent_when_not_installed(monkeypatch):
    _fake_appkit(monkeypatch)
    assert opendisplay.app_state() == opendisplay.APP_ABSENT


def test_open_app_goes_through_run_cmd(monkeypatch):
    """唤起走 ``core/proc.run_cmd``（禁 shell），插件不自建 subprocess。"""
    calls = []

    class _Res:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(
        opendisplay, "run_cmd", lambda cmd, timeout=10: calls.append(list(cmd)) or _Res()
    )
    assert opendisplay.OpenDisplayBackend().open_app() is True
    assert calls == [["open", "-a", "OpenDisplay"]]


def test_open_app_failure_is_false_not_raise(monkeypatch):
    """未安装时 ``open`` 返回非 0 → False（不抛）：absent 是 display 的正常取值。"""

    class _Res:
        returncode = 1
        stderr = "Unable to find application named 'OpenDisplay'"

    monkeypatch.setattr(opendisplay, "run_cmd", lambda cmd, timeout=10: _Res())
    assert opendisplay.OpenDisplayBackend().open_app() is False


def test_presentation_defaults_to_menu_bar(monkeypatch):
    """读不到该键（本机实测从未改过）⇒ ``menuBar``，与上游默认值一致（§2.5）。"""
    monkeypatch.setattr(opendisplay.OpenDisplayBackend, "_pref", staticmethod(lambda a, k: None))
    b = opendisplay.OpenDisplayBackend()
    assert b.presentation() == opendisplay.PRESENTATION_MENU_BAR
    assert b.is_in_menu_bar() is True


def test_presentation_background_frees_the_slot(monkeypatch):
    monkeypatch.setattr(
        opendisplay.OpenDisplayBackend, "_pref", staticmethod(lambda a, k: "background")
    )
    b = opendisplay.OpenDisplayBackend()
    assert b.is_in_menu_bar() is False


def test_presentation_illegal_value_falls_back_to_menu_bar(monkeypatch):
    """值非法（上游 enum 只有三个 rawValue）⇒ 保守报"仍占位"，不谎报已让位。"""
    monkeypatch.setattr(opendisplay.OpenDisplayBackend, "_pref", staticmethod(lambda a, k: 42))
    assert opendisplay.OpenDisplayBackend().presentation() == opendisplay.PRESENTATION_MENU_BAR


# ──────────────────────────────────────────────
# 2.7 三台状态机 / 副标题场景表 / 设置面接口
# ──────────────────────────────────────────────

def _ticked(tmp_path, monkeypatch, rounds=2, **kw):
    """构造插件并跑若干轮 tick（穿过防抖），返回 (plugin, fake)。"""
    plugin, fake = _plugin(tmp_path, monkeypatch, **kw)
    for _ in range(rounds):
        plugin.tick()
    return plugin, fake


def test_automation_off_keeps_display_reading_and_its_level(tmp_path, monkeypatch):
    """§4.2 × 决议 D13：display 的**取值与级别都不变**（世界的状态不该被组件开关改写）。

    旧实现把级别压到 IDLE —— 那是**运行期改级别**，正是 D13 要消灭的第二条路径。
    「组件归灰」并不因此丢掉：automation 那台机的 ``stopped``（OFF）在严重度序里比
    IDLE / OK 都靠后，代表态取最坏仍是灰的（本用例另一半锁着它的取值）。
    """
    plugin, fake = _ticked(tmp_path, monkeypatch, app_state=signals.DISPLAY_RUNNING, virtual=True)
    display_spec = _state_spec(plugin, "display")
    assert resolve(display_spec, plugin._display_snapshot()).level is StateLevel.OK  # extended → OK
    plugin.set_automation_active(False)
    snap = plugin._display_snapshot()
    assert snap.state == signals.DISPLAY_EXTENDED                  # 取值不变
    rs = resolve(display_spec, snap)
    assert rs.text == "第二屏已连接"                                # 措辞也不说"已冻结"
    assert rs.level is StateLevel.OK                               # 如实报绿，不再手动压灰
    assert rs.icon is IconName.DISPLAY_ON
    assert plugin._notify_state()[0] == "frozen"                   # notify 降级取值（不撒谎）
    assert plugin._automation_snapshot().state == "stopped"        # 归灰由这台机承担
    plugin.stop()


def test_automation_off_stops_probing(tmp_path, monkeypatch):
    """自动化关掉后**不探测**（§4.2「不探测」）：display 保持最后已知取值。"""
    plugin, fake = _ticked(tmp_path, monkeypatch, app_state=signals.DISPLAY_RUNNING, virtual=True)
    assert plugin.display_state() == signals.DISPLAY_EXTENDED
    plugin.set_automation_active(False)
    probed = []
    monkeypatch.setattr(
        "barkit.plugins.notify_guard.plugin.signals.detect_display_state",
        lambda: probed.append(1) or signals.DISPLAY_IDLE,
    )
    plugin.tick()
    assert probed == []
    assert plugin.display_state() == signals.DISPLAY_EXTENDED
    plugin.stop()


def test_desync_is_reported_when_system_disagrees(tmp_path, monkeypatch):
    """``desync`` 是"状态自证"的唯一出口（§4.1）：系统值与守卫期望不符 → ERR。"""
    plugin, fake = _ticked(tmp_path, monkeypatch)      # idle ⇒ 期望静音
    assert fake.applied_allow is False
    fake.applied_allow = True                          # 别的东西把静音改掉了
    state, reason = plugin._notify_state()
    assert state == "desync"
    assert "守卫期望静音" in reason
    snap = plugin._notify_snapshot()
    assert snap.state == "desync"
    assert resolve(_state_spec(plugin, "notify"), snap).level is StateLevel.ERR
    plugin.stop()


def test_row_subtitle_scenario_second_screen_ok(tmp_path, monkeypatch):
    """场景 1：第二屏在用、无录屏 → ``iPad 已连接 · 通知正常``（§6.3）。"""
    plugin, fake = _ticked(tmp_path, monkeypatch, app_state=signals.DISPLAY_RUNNING, virtual=True)
    assert plugin._row_subtitle() == "iPad 已连接 · 通知正常"
    plugin.stop()


def test_row_subtitle_scenario_recording_hit(tmp_path, monkeypatch):
    """场景 2：第二屏在用且规则命中 → 报**命中原因**并静音（§6.3）。"""
    plugin, fake = _plugin(tmp_path, monkeypatch, app_state=signals.DISPLAY_RUNNING, virtual=True)
    monkeypatch.setattr(
        "barkit.plugins.notify_guard.plugin.signals.get_process_snapshot",
        lambda: ["/applications/obs.app/contents/macos/obs"],
    )
    plugin.tick()
    plugin.tick()                                      # 防抖后写入静音
    assert plugin._row_subtitle() == "iPad 已连接 · 录屏软件运行中，已静音"
    plugin.stop()


def test_row_subtitle_scenario_no_second_screen(tmp_path, monkeypatch):
    """场景 3：没插 iPad → ``无第二屏 · 通知已静音``（规则字面的直接体现，§1.4）。"""
    plugin, fake = _ticked(tmp_path, monkeypatch, app_state=signals.DISPLAY_IDLE)
    assert plugin._row_subtitle() == "无第二屏 · 通知已静音"
    plugin.stop()


def test_row_subtitle_scenario_manual_override(tmp_path, monkeypatch):
    """场景 4：手动覆盖中 → 只报"谁在管事"（§6.3）。"""
    plugin, fake = _ticked(tmp_path, monkeypatch, app_state=signals.DISPLAY_IDLE)
    plugin.toggle_action()
    assert plugin._row_subtitle() == "手动覆盖中 · 自动接管已挂起"
    plugin.stop()


def test_row_subtitle_scenario_automation_off(tmp_path, monkeypatch):
    """场景 5：自动化关（设置页关掉）→ ``自动化已停用 · 通知由手动开关控制``。"""
    plugin, fake = _ticked(tmp_path, monkeypatch, app_state=signals.DISPLAY_IDLE)
    plugin.set_automation_active(False)
    assert plugin._row_subtitle() == "自动化已停用 · 通知由手动开关控制"
    plugin.stop()


def test_row_subtitle_scenario_not_installed(tmp_path, monkeypatch):
    """场景 6：未安装 OpenDisplay → ``未安装 OpenDisplay · 通知已静音``。

    这条同时是设计 §9.2「未安装 ⟹ 永久静音」的可见性证明：后果被**显示出来**了。
    """
    plugin, fake = _ticked(tmp_path, monkeypatch, app_state=signals.DISPLAY_ABSENT)
    assert plugin._row_subtitle() == "未安装 OpenDisplay · 通知已静音"
    plugin.stop()


def test_save_rules_round_trip(tmp_path, monkeypatch):
    """设置页写规则的唯一入口：经插件方法 → ``update_plugin_section``（UI 不碰 config）。"""
    plugin, fake = _plugin(tmp_path, monkeypatch)
    rules = [{"name": "会议", "reason": "腾讯会议进行中", "processes": ["tencentmeeting"]}]
    assert plugin.save_rules(rules) is True
    assert plugin.rules() == rules
    assert plugin._last_hits == [] and plugin._last_combo is None   # 命中缓存已失效
    plugin.stop()


def test_save_rules_failure_returns_false(tmp_path, monkeypatch):
    plugin, fake = _plugin(tmp_path, monkeypatch)

    def boom(*_a, **_k):
        raise OSError("磁盘只读")

    monkeypatch.setattr(plugin.ctx.config, "update_plugin_section", boom)
    assert plugin.save_rules([]) is False
    plugin.stop()


def test_process_candidates_filters_system_paths(tmp_path, monkeypatch):
    """进程候选取 basename（match_rules 是子串匹配），并滤掉系统内部路径。"""
    plugin, fake = _plugin(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "barkit.plugins.notify_guard.plugin.signals.get_process_snapshot",
        lambda: [
            "/system/library/coreServices/launchd",
            "/usr/libexec/logd",
            "/applications/obs.app/contents/macos/obs",
            "/applications/lark.app/contents/macos/lark",
            "/applications/obs.app/contents/macos/obs",
        ],
    )
    got = plugin.process_candidates()
    assert got == ["lark", "obs"]
    plugin.stop()


def test_opendisplay_facts_returns_pair(tmp_path, monkeypatch):
    plugin, fake = _plugin(tmp_path, monkeypatch)
    monkeypatch.setattr("barkit.plugins.notify_guard.plugin_device.app_state",
                        lambda: opendisplay.APP_IDLE)
    monkeypatch.setattr(plugin.opendisplay_backend, "is_in_menu_bar", lambda: True)
    assert plugin.opendisplay_facts() == (opendisplay.APP_IDLE, True)
    plugin.stop()


def test_open_opendisplay_delegates_to_backend(tmp_path, monkeypatch):
    """整行点击转给后端；**失败要把结局交回去**（`ActionResult`，契约 D32-②）。

    改前这里只记一行日志，用户在面板上看到的是「点了一下，什么也没发生」；成功那句
    什么都不返回则是**有意**的：``open -a`` 成不成看得见（窗体自己会出来），而「未安装」
    本就是 display 状态机的一个正常取值（``absent``），面板上早有那句话。
    """
    from barkit.contribution import ActionOutcome

    plugin, fake = _plugin(tmp_path, monkeypatch)
    called = []
    monkeypatch.setattr(plugin.opendisplay_backend, "open_app", lambda: called.append(1) or True)
    assert plugin.open_opendisplay() is None, "成功不必再补一句（窗体自己会出来）"
    assert called == [1]

    def _fail():
        called.append(2)
        return False

    monkeypatch.setattr(plugin.opendisplay_backend, "open_app", _fail)
    failed = plugin.open_opendisplay()
    assert called == [1, 2]
    assert failed is not None and failed.outcome is ActionOutcome.FAILED
    assert "OpenDisplay" in failed.text
    plugin.stop()


def test_toggle_failure_does_not_raise_and_marks_desync(tmp_path, monkeypatch):
    """写失败不弹窗（零 PySide6）：只记日志，状态由 ``desync`` 承担可见性。"""
    from barkit.plugins.notify_guard.backend import BackendError

    plugin, fake = _plugin(tmp_path, monkeypatch)

    def boom(suppress):
        raise BackendError("强制失败")

    fake.apply = boom
    plugin.toggle_action()                       # 不抛
    plugin.stop()


# ──────────────────────────────────────────────
# 2.8 设置页（规则编辑器，设计 §6.4）
# ──────────────────────────────────────────────

def _editor(tmp_path, monkeypatch, qtbot):
    from barkit.plugins.notify_guard import settings as ng_settings

    plugin, fake = _plugin(tmp_path, monkeypatch)
    page = ng_settings.build_page(plugin)
    qtbot.addWidget(page)
    editor = page.findChild(ng_settings._RulesEditor)
    assert editor is not None
    # pytest-qt 4.5.0 的 ``addWidget`` 只存 **weakref**：page 若不另加强引用，一离开本函数
    # 作用域就被 GC，连带销毁整棵控件树（后续访问子控件必 "Internal C++ object already
    # deleted"）。挂在调用方持有的 editor 上钉住它（QWidget 允许挂 Python 属性）。
    editor._page_keepalive = page
    return plugin, fake, editor


def _click_save(editor) -> None:
    """点**真实那枚**「保存」键（不直接调内部方法）。

    `ui_kit.EditScreen` 的保存两端是**注入**的回调（它是组合件，在消费方那边覆写是
    调不到的 —— 2026-09-27 踩过「键亮着、点了没反应」）。只有走按钮才验证得到那条
    接线，「直接调 `_commit_form()`」会绕过它、把这一类缺陷放过去。
    """
    editor._edit.save_button.click()


def _on_edit_screen(editor) -> bool:
    """此刻台上那一屏是不是编辑屏（换屏容器的读口，替代旧的下标魔数）。"""
    return editor._stack.current() is editor._edit


def test_settings_page_never_opens_a_modal_box(tmp_path, monkeypatch, qtbot):
    """门禁（本文件版）：模块连**导入**都不许再有模态弹框的入口。

    2026-09-14 实测（`~/Library/Logs/DiagnosticReports/Python-2026-09-14-170030.ips`
    的 `lastExceptionBacktrace` 80 帧逐帧可读）：点「常用场景」→ Python 槽 →
    `QMessageBox.information` → `QDialog::exec()` 嵌套事件循环 → 主线程再收 SIGINT 时
    KeyboardInterrupt 在 PyObjC 回调里抛出 → `objc_exception_throw` → `abort()`。
    用户侧的体感是「窗口不动、关不掉、只能强制结束程序」。

    判据用「这一族的每个文件各自的模块命名空间」而不是扫源码子串：子串会被**模块
    docstring 里对这件事的说明**误判（文档就是要提 `QMessageBox` 这个名字）。
    正式门禁在
    `tests/test_architecture.py::test_plugin_settings_pages_have_no_modal_dialogs`（AST）。

    ⚠️ 2026-10-03 拆包后判据从「单个模块」改成「**这一族**」：原先只查入口模块的
    `hasattr`，而正文（也就是真会弹框的那片）全搬进了兄弟模块 ⇒ 入口上本来就没有
    `QMessageBox`，两条断言变成**恒真**、照样绿（拆包带给路径型门禁的那类失效：
    收口对象变了，而入口那个名字还在）。改法同
    `test_architecture.py::test_plugin_py_has_no_pyside6` 那条：收一族、逐个查。
    """
    import importlib

    from pathlib import Path as _Path

    pkg, mod_name = "barkit.plugins.notify_guard", "settings"
    entry = importlib.import_module(f"{pkg}.{mod_name}")
    family = sorted(
        f"{pkg}.{p.stem}" for p in _Path(entry.__file__).parent.glob("settings*.py")
    )
    assert len(family) > 1, (
        f"这一族只收到 {len(family)} 个文件 ⇒ 判据退回单模块，"
        "而真会弹框的那片早就搬进兄弟模块了"
    )
    for dotted in family:
        mod = importlib.import_module(dotted)
        assert not hasattr(mod, "QMessageBox"), f"{dotted} 引入了 QMessageBox"
        assert not hasattr(mod, "QDialog"), f"{dotted} 引入了 QDialog"


def test_settings_page_builds_and_shows_state(tmp_path, monkeypatch, qtbot):
    """设置页可构建，且顶部状态条回显三台机的当前取值。"""
    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    text = editor._status_lbl.text()
    assert "第二屏：" in text and "通知：" in text and "自动化：" in text
    assert editor._auto_sw.isChecked() is True
    plugin.stop()


def test_editor_lists_existing_rules(tmp_path, monkeypatch, qtbot):
    """规则列表来自配置（``_plugin_ctx`` 预置了 lark / screencast 两条）。"""
    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    assert [r["name"] for r in editor._rules] == ["lark", "screencast"]
    plugin.stop()


def test_editor_add_rule_persists_and_opens_editor(tmp_path, monkeypatch, qtbot):
    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    editor._on_add()
    names = [r["name"] for r in plugin.rules()]
    assert names == ["lark", "screencast", "新规则"]
    assert _on_edit_screen(editor)                  # 新增后直接进编辑页
    plugin.stop()


def test_editor_save_writes_name_reason_processes(tmp_path, monkeypatch, qtbot):
    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    editor._open_edit(0)
    editor._name_edit.setText("飞书会议")
    editor._reason_edit.setText("飞书会议进行中")
    editor._picked = {"lark", "feishu"}
    _click_save(editor)
    first = plugin.rules()[0]
    assert first == {
        "name": "飞书会议", "reason": "飞书会议进行中", "processes": ["feishu", "lark"],
    }
    assert not _on_edit_screen(editor)              # 保存后回列表
    plugin.stop()


def test_editor_rejects_duplicate_name(tmp_path, monkeypatch, qtbot):
    """重名 → 提示落在**行内**反馈槽，不落盘、不弹框。"""
    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    editor._open_edit(0)
    editor._name_edit.setText("screencast")          # 与另一条重名
    _click_save(editor)
    assert "不能重复" in editor._edit_fb.text()
    assert _on_edit_screen(editor), "校验没过时不该离开编辑屏"
    assert [r["name"] for r in plugin.rules()] == ["lark", "screencast"]
    plugin.stop()


def test_editor_rejects_empty_name(tmp_path, monkeypatch, qtbot):
    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    editor._open_edit(0)
    editor._name_edit.setText("   ")
    _click_save(editor)
    assert "起个名字" in editor._edit_fb.text()
    assert [r["name"] for r in plugin.rules()] == ["lark", "screencast"]
    plugin.stop()


def test_editor_delete_asks_then_persists(tmp_path, monkeypatch, qtbot):
    """删除是**两步**：先挂起确认（不落盘），确认后才删。

    「挂起」= `_edit_fb.is_pending()`，而不是"弹了个框"——本页已零模态（见同目录门禁用例）。
    """
    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    editor._open_edit(1)
    editor._on_delete()
    assert editor._edit_fb.is_pending()                             # 先问
    assert editor._edit_fb.text().count("screencast")
    assert [r["name"] for r in plugin.rules()] == ["lark", "screencast"]   # 还没删
    editor._edit_fb.accept()                                        # 点确认
    assert [r["name"] for r in plugin.rules()] == ["lark"]
    assert not _on_edit_screen(editor)                              # 回列表
    plugin.stop()


def test_editor_delete_cancel_keeps_rule(tmp_path, monkeypatch, qtbot):
    """取消：撤回确认，一个字都不改。"""
    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    editor._open_edit(1)
    editor._on_delete()
    editor._edit_fb.dismiss()                                       # 点取消
    assert not editor._edit_fb.is_pending()
    assert [r["name"] for r in plugin.rules()] == ["lark", "screencast"]
    assert _on_edit_screen(editor)                                  # 仍停在编辑屏
    plugin.stop()


def test_editor_preset_adds_scenarios_once(tmp_path, monkeypatch, qtbot):
    """预设一次补齐四条常用场景；已存在的同名不覆盖（§6.4.4）。"""
    from barkit.plugins.notify_guard.settings import RULE_PRESETS

    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    editor._on_preset()
    names = [r["name"] for r in plugin.rules()]
    assert names[:2] == ["lark", "screencast"]              # 原有两条不动
    for name, _reason, _procs in RULE_PRESETS:
        assert name in names
    assert "已补齐" in editor._list_fb.text()
    before = list(names)
    editor._on_preset()                                     # 再点一次：不重复添加
    assert [r["name"] for r in plugin.rules()] == before
    plugin.stop()


def test_editor_preset_second_click_is_inline_and_never_pends(tmp_path, monkeypatch, qtbot):
    """**回归用例**：第二次点「常用场景」原本走 `QMessageBox.information` —— 真机卡死。

    当时的证据链：`QMessageBoxFunc_information` → `QDialog::exec()` 嵌套事件循环 →
    主线程收 SIGINT 时在 PyObjC 回调里抛 KeyboardInterrupt → `abort()`（见本文件门禁用例的
    docstring）。现在这条路只回一句行内提示，且**不挂任何待确认动作**（它不是确认流）。
    """
    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    editor._on_preset()
    editor._list_fb.info("上一轮的残留提示")                  # 先放一句，验证会被覆盖
    editor._on_preset()                                     # ← 崩溃现场那一次点击
    assert "都已存在" in editor._list_fb.text()
    assert not editor._list_fb.is_pending()
    plugin.stop()


def test_rule_presets_exclude_resident_processes():
    """门禁：预设不得放「打开应用就在跑」的主程序名或系统常驻进程。

    规则是 OR 关系、命中即静音，所以一个常驻进程 = 这条规则**恒命中**。实测两次翻车：
    - `lark` / `feishu` 命中 `/Applications/Lark.app/Contents/MacOS/feishu for sany`
      （**常驻主程序**，开着飞书就报「会议进行中」）；
    - `airplay` 命中 `/System/Library/CoreServices/AirPlayUIAgent.app`（**系统常驻**，
      每台 Mac 都在跑，实测「投屏中」恒真）。
    会议场景改用**会议专用 helper**（随会议出现/消失），这是《方案文档》§4.2 的定案。
    """
    from barkit.plugins.notify_guard.settings import RULE_PRESETS

    procs = {p for _n, _r, ps in RULE_PRESETS for p in ps}
    assert "lark" not in procs and "feishu" not in procs
    assert "airplay" not in procs
    meeting = dict((n, ps) for n, _r, ps in RULE_PRESETS)["会议"]
    assert "lark helper (iron)" in meeting


def test_editor_reset_asks_then_clears_all_rules(tmp_path, monkeypatch, qtbot):
    """「初始化」= 回到出厂默认（`rules` 的 `default=[]`）——**两步**，先确认再清。"""
    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    editor._on_reset()
    assert editor._list_fb.is_pending()                            # 先问
    assert [r["name"] for r in plugin.rules()] == ["lark", "screencast"]   # 还没清
    editor._list_fb.accept()
    assert plugin.rules() == []                                    # 落盘也是空的
    assert editor._rules == []
    assert not editor._list_fb.is_pending()
    plugin.stop()


def test_editor_reset_cancel_keeps_rules(tmp_path, monkeypatch, qtbot):
    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    editor._on_reset()
    editor._list_fb.dismiss()
    assert [r["name"] for r in plugin.rules()] == ["lark", "screencast"]
    plugin.stop()


def test_editor_reset_when_already_empty_says_so(tmp_path, monkeypatch, qtbot):
    """已经是出厂态就不必再问一次（避免"点了没反应"，也避免多一步确认）。"""
    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    editor._do_reset()
    assert plugin.rules() == []
    editor._on_reset()
    assert not editor._list_fb.is_pending()
    assert "初始状态" in editor._list_fb.text()
    plugin.stop()


def test_editor_reset_is_undoable_by_presets(tmp_path, monkeypatch, qtbot):
    """清空 → 再点「常用场景」⇒ 拿回**修正过**的四条预设（这就是「初始化」的用途）。"""
    from barkit.plugins.notify_guard.settings import RULE_PRESETS

    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    editor._do_reset()
    editor._on_preset()
    got = {r["name"]: r["processes"] for r in plugin.rules()}
    assert set(got) == {n for n, _r, _p in RULE_PRESETS}
    assert "lark helper (iron)" in got["会议"] and "airplay" not in got["投屏"]
    plugin.stop()


def test_editor_toggle_automation_writes_config(tmp_path, monkeypatch, qtbot):
    """自动化总闸在设置页（裁决项 6）：开 / 关都写回用户配置段。"""
    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    editor._on_toggle_automation()                          # True → False
    assert plugin.automation_active is False
    assert editor._auto_sw.isChecked() is False
    editor._on_toggle_automation()                          # 再点回 True
    assert plugin.automation_active is True
    plugin.stop()


def test_editor_process_list_marks_picked_and_offers_manual_add(tmp_path, monkeypatch, qtbot):
    """进程区：已选的先勾上；筛不到的关键词给一条「＋ 添加」（覆盖"此刻没在跑"）。"""
    from PySide6.QtWidgets import QCheckBox

    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    monkeypatch.setattr(plugin, "process_candidates", lambda: ["obs", "lark"])
    editor._open_edit(0)                                    # lark 规则（processes=[Lark Helper (Iron)]）
    boxes = {b.text(): b for b in editor._proc_box.findChildren(QCheckBox)}
    assert "Lark Helper (Iron)" in boxes                    # 已配的（此刻没跑）也要列出来
    assert boxes["Lark Helper (Iron)"].isChecked() is True
    editor._filter_edit.setText("kap")                      # 候选里没有 → 出现"添加"
    editor._refill_processes()
    labels = [b.text() for b in editor._proc_box.findChildren(QCheckBox)]
    assert "kap" not in labels                              # 未确认前不进候选列表
    plugin.stop()


# ──────────────────────────────────────────────
# 2.9 启动时自动拉起 OpenDisplay（2026-09-18）
#
# 判据三条：开关关着 ⇒ 连装置态都不探；开关开着且「装了但没跑」⇒ 拉起一次；
# 已经在跑 / 没装 ⇒ 不动它。全部打桩 —— 用例里绝不真调 AppKit、绝不真 open。
# ──────────────────────────────────────────────

# 追加进用户 YAML 的那一行（缩进对齐 plugins.notify_guard 的键）
_STARTUP_ON = "    open_on_startup: true\n"


def _startup_plugin(tmp_path, monkeypatch, *, app_state: str):
    """开关**已设**的插件 ＋ 打桩的装置态，返回 (plugin, 拉起次数)。"""
    plugin, fake = _plugin(tmp_path, monkeypatch, extra=_STARTUP_ON)
    monkeypatch.setattr("barkit.plugins.notify_guard.plugin_device.app_state", lambda: app_state)
    calls: list[int] = []
    monkeypatch.setattr(
        plugin.opendisplay_backend, "open_app", lambda: calls.append(1) or True
    )
    return plugin, calls


def test_open_on_startup_defaults_to_off(tmp_path, monkeypatch):
    """缺省＝「用户没设过」⇒ 不自动拉起。判据本体就是这一条。"""
    plugin, fake = _plugin(tmp_path, monkeypatch)
    assert plugin.open_on_startup is False
    plugin.stop()


def test_startup_launch_skipped_when_switch_is_off(tmp_path, monkeypatch):
    """开关关着时**连 ``app_state()`` 都不探**（默认路径不碰 AppKit）。"""
    plugin, fake = _plugin(tmp_path, monkeypatch)
    probed: list[int] = []
    monkeypatch.setattr(
        "barkit.plugins.notify_guard.plugin_device.app_state",
        lambda: probed.append(1) or opendisplay.APP_IDLE,
    )
    calls: list[int] = []
    monkeypatch.setattr(
        plugin.opendisplay_backend, "open_app", lambda: calls.append(1) or True
    )
    plugin.tick()   # 上膛
    plugin.tick()   # 真判据：开关关着，连 app_state 都不探
    assert probed == [] and calls == []
    plugin.stop()


def test_startup_launch_when_enabled_and_not_running(tmp_path, monkeypatch):
    """「装了但没跑」才拉起，且**只拉一次**（tick 每 3s 一轮，别拉成循环）。

    三拍正好盖住三段：第 1 拍只上膛（那一下与宿主装配同一时刻到达）、第 2 拍真拉、
    第 3 拍起不再拉。
    """
    plugin, calls = _startup_plugin(
        tmp_path, monkeypatch, app_state=opendisplay.APP_IDLE
    )
    plugin.tick()
    plugin.tick()
    plugin.tick()
    assert calls == [1]
    plugin.stop()


def test_startup_launch_skipped_when_already_running(tmp_path, monkeypatch):
    """已经在跑 ⇒ 不动它（``open -a`` 对运行中的 App 是「再打开一次」＝弹窗体）。

    两拍：首拍只「上膛」（那一下与宿主装配同一时刻到达，拉副屏会撞上正在建窗的宿主），
    第二拍才是真判据所在的那一拍。
    """
    plugin, calls = _startup_plugin(
        tmp_path, monkeypatch, app_state=opendisplay.APP_RUNNING
    )
    plugin.tick()   # 上膛
    plugin.tick()   # 真判据
    assert calls == []
    plugin.stop()


def test_startup_launch_skipped_when_not_installed(tmp_path, monkeypatch):
    """没装 ⇒ 不试（否则只换来一次注定失败的 open 与一行 warning）。"""
    plugin, calls = _startup_plugin(
        tmp_path, monkeypatch, app_state=opendisplay.APP_ABSENT
    )
    plugin.tick()   # 上膛
    plugin.tick()   # 真判据
    assert calls == []
    plugin.stop()


def test_set_open_on_startup_round_trip(tmp_path, monkeypatch):
    """设置页写这一项：落进用户配置段，重读得到同一个值。"""
    plugin, fake = _plugin(tmp_path, monkeypatch)
    assert plugin.set_open_on_startup(True) is True
    assert plugin.open_on_startup is True
    assert plugin.ctx.config.get("plugins.notify_guard", {}).get("open_on_startup") is True
    plugin.stop()


def test_set_open_on_startup_failure_returns_false(tmp_path, monkeypatch):
    """写盘失败 ⇒ 返回 False 且**内存读数不变**（界面不该显示一个没落盘的值）。"""
    plugin, fake = _plugin(tmp_path, monkeypatch)

    def boom(plugin_id, mutate):
        raise OSError("盘写不了")

    monkeypatch.setattr(plugin.ctx.config, "update_plugin_section", boom)
    assert plugin.set_open_on_startup(True) is False
    assert plugin.open_on_startup is False
    plugin.stop()


def test_editor_toggle_startup_writes_config(tmp_path, monkeypatch, qtbot):
    """设置页那枚开关：开 / 关都写回用户配置段（零模态，走行内反馈）。"""
    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    assert editor._launch_sw.isChecked() is False
    editor._on_toggle_launch()
    assert plugin.open_on_startup is True
    assert editor._launch_sw.isChecked() is True
    editor._on_toggle_launch()
    assert plugin.open_on_startup is False
    assert editor._launch_sw.isChecked() is False
    plugin.stop()


def test_settings_page_shows_startup_switch_from_config(tmp_path, monkeypatch, qtbot):
    """开关初值来自配置（预置 YAML，不经 setter —— 免得用例自证）。"""
    from barkit.plugins.notify_guard import settings as ng_settings

    plugin, fake = _plugin(tmp_path, monkeypatch, extra=_STARTUP_ON)
    page = ng_settings.build_page(plugin)
    qtbot.addWidget(page)
    editor = page.findChild(ng_settings._RulesEditor)
    editor._page_keepalive = page
    assert editor._launch_sw.isChecked() is True
    plugin.stop()


def test_editor_toggle_startup_failure_rolls_back_switch(tmp_path, monkeypatch, qtbot):
    """写盘失败 ⇒ 开关回原位并给行内红字（界面不许撒谎说已打开）。"""
    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    monkeypatch.setattr(plugin, "set_open_on_startup", lambda on: False)
    editor._on_toggle_launch()
    assert editor._launch_sw.isChecked() is False
    plugin.stop()


# ──────────────────────────────────────────────
# 2.10 手动覆盖的归还入口（2026-09-18，任务板 §5 #29 P0 ②）
#
# 缺陷本体：手动覆盖的自动归还条件是**信号组合变化**（decider.combo_at_toggle）；
# 「没插 iPad ＋ 没命中规则」这种稳态下组合永不变化 ⇒ 用户点过那枚 Switch 就再也回不到
# 自动，面板一直挂着「手动覆盖中 · 自动接管已挂起」。resume_auto_action 此前零调用方。
# 落点为什么在设置页（而不是设计原写的面板行）见 docs/log/2026-09-18-手动覆盖归还入口.md。
# ──────────────────────────────────────────────

def _manual(plugin):
    """把插件推到「手动覆盖中」（面板 Switch 的语义），并确认真的在那一态。"""
    plugin.toggle_action()
    assert plugin.notify_state() == "manual"
    return plugin


def test_resume_auto_action_returns_to_auto_and_writes(tmp_path, monkeypatch):
    """手动覆盖 → 恢复自动：mode 回 auto、按当前信号立即写一次、成功时不说话。"""
    plugin, fake = _ticked(tmp_path, monkeypatch, app_state=signals.DISPLAY_IDLE)
    _manual(plugin)
    before = len(fake.writes)
    assert plugin.resume_auto_action() is None            # 成功 = 没话要说（ActionResult 口径）
    assert plugin.decider.mode == "auto"
    assert plugin.decider.manual_allow is None
    assert plugin.notify_state() != "manual"
    assert len(fake.writes) == before + 1                 # 跳过防抖，当场写一次
    assert plugin.resume_auto_action() is None            # 已在自动态：幂等，不抛
    plugin.stop()


def test_resume_auto_action_is_the_only_way_back_in_a_steady_state(tmp_path, monkeypatch):
    """这条钉住缺陷本身：稳态下**只靠 tick** 永远退不出手动（这就是入口必须存在的理由）。"""
    plugin, fake = _ticked(tmp_path, monkeypatch, app_state=signals.DISPLAY_IDLE)
    _manual(plugin)
    for _ in range(3):
        plugin.tick()                                     # 信号组合不变
    assert plugin.notify_state() == "manual"              # 还是回不去
    plugin.resume_auto_action()
    assert plugin.notify_state() != "manual"              # 有入口就回得去
    plugin.stop()


def test_resume_auto_action_refuses_when_automation_is_off(tmp_path, monkeypatch):
    """总闸关着 ⇒ 一个字节都不写，只回一句"先打开总闸"（那时状态归手动开关）。"""
    plugin, fake = _ticked(tmp_path, monkeypatch, app_state=signals.DISPLAY_IDLE)
    plugin.set_automation_active(False)
    before = len(fake.writes)
    r = plugin.resume_auto_action()
    assert r is not None and r.outcome is ActionOutcome.FAILED
    assert "自动化总闸" in r.text
    assert len(fake.writes) == before                     # 没碰系统
    plugin.stop()


def test_resume_auto_action_reports_write_failure(tmp_path, monkeypatch):
    """写失败 ⇒ FAILED 带文案（用户看不出别的，因为面板上什么都不会变）。"""
    from barkit.plugins.notify_guard.backend import BackendError

    plugin, fake = _ticked(tmp_path, monkeypatch, app_state=signals.DISPLAY_IDLE)
    _manual(plugin)

    def boom(suppress):
        raise BackendError("强制失败")

    fake.apply = boom
    r = plugin.resume_auto_action()                       # 不抛
    assert r is not None and r.outcome is ActionOutcome.FAILED
    assert "自动" in r.text
    plugin.stop()


def test_editor_resume_card_only_visible_in_manual(tmp_path, monkeypatch, qtbot):
    """卡片只在手动覆盖态出现；点它 = 交回自动 ＋ 卡片消失。

    判据用 ``isVisibleTo``：卡片是**常驻构建、靠 setVisible 切换**的（同 `daemon_ops`
    那枚「重装守护」），"在不在控件树里"恒真、看不出可见性。
    """
    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    assert editor._resume_card.isVisibleTo(editor) is False

    _manual(plugin)
    editor._refresh_list()
    assert editor._resume_card.isVisibleTo(editor) is True

    editor._on_resume_auto()
    assert plugin.notify_state() != "manual"
    assert editor._resume_card.isVisibleTo(editor) is False
    plugin.stop()


def test_editor_resume_failure_lands_in_the_page_feedback(tmp_path, monkeypatch, qtbot):
    """失败文案落在本页反馈槽（设置页直接消费返回值，不经那套声明式中继）。"""
    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    monkeypatch.setattr(
        plugin, "resume_auto_action",
        lambda: ActionResult(ActionOutcome.FAILED, "没能交回自动 —— 写系统通知设置失败了，请看日志。"),
    )
    editor._on_resume_auto()
    assert "没能交回自动" in editor._list_fb._lbl.text()
    plugin.stop()


# ──────────────────────────────────────────────
# 2.11 装置段三项：规则拖排序 / 防抖 / 运维（2026-09-19，任务板 §5 #29 P1）
#
# 三件事共用设置页的**装置段**（设计 §6.2 判定线 2：「这个装置」的事），但各有各的真源：
#
# - 规则顺序、防抖轮数 → 写用户 YAML（**组件**的事）；
# - 框架节拍 `poll` → 组件**碰都不碰**（D27：那个键只服务 ``core/scheduler.py``）；
# - 拨测 / 复制的措辞 → 归 `diagnostics.py`（排版层），探测归 `plugin.probe()`。
#
# 最后一条是这套用例里最要紧的不变量：**探测与排版分家**之后，「拨测」上屏那份与
# 「复制诊断」进剪贴板那份共用同一个排版函数，两个出口不可能各说各话。
# ──────────────────────────────────────────────


def _probe(**kw) -> Probe:
    """排一个可控的 ``Probe``：排版层零 IO，故用例只需给字段、不必起插件。"""
    base = dict(
        app="idle", display="idle", notify="allowed", reason="", automation=True,
        rules=(), hits=(), system=False, debounce=2, open_on_startup=False,
    )
    base.update(kw)
    return Probe(**base)


# ---------- 2.11.1 排版层（纯逻辑，零 Qt / 零 IO）----------

def test_format_probe_is_five_lines_in_troubleshooting_order():
    """五行的顺序即排查顺序：装置 → 第二屏 → 规则 → 决策 → 系统。"""
    p = _probe(
        rules=(RuleLine("lark", "飞书会议中", ("lark helper (iron)",)),),
        hits=("飞书会议中",),
    )
    lines = format_probe(p)
    assert len(lines) == 5
    assert lines[0] == "装置：已安装 · 未运行"
    assert lines[1].startswith("第二屏：")
    assert lines[2] == "规则：1 条 · 命中 1 条（飞书会议中）"
    assert lines[3] == "决策：允许通知"
    assert lines[4] == "系统：未静音 · 与决策一致"


def test_format_probe_zero_rules_is_a_conclusion_not_a_gap():
    """没有规则不是缺数据，而是「第二屏在线即允许通知」这条**结论**本身。

    ``allow = S1 AND NOT S2``，S2 恒假 ⇒ 第二屏在线就放行。写出来，省得用户以为
    规则没生效（这正是「拨测要回答为什么」与「列个数字」的差别）。
    """
    assert format_probe(_probe())[2] == "规则：0 条（没有规则 → 第二屏在线即允许通知）"


def test_format_probe_decision_line_uses_the_state_machine_own_reason():
    """静音行取 ``Probe.reason``（状态机自带），**不**在排版层重算 ``allow``。

    重算一遍就是第二条真源：与 decider 迟早漂移，还会在防抖窗口里报出假的不一致。
    """
    p = _probe(notify="muted", reason="录屏软件运行中", system=True)
    assert format_probe(p)[3] == "决策：静音 —— 录屏软件运行中"
    assert format_probe(_probe(notify="muted", system=True))[3] == "决策：静音"


def test_format_probe_decision_line_precedence():
    """desync > 自动化停用 > 手动覆盖 > 允许 / 静音。顺序错了会把「谁在管事」说丢。"""
    assert format_probe(
        _probe(notify="desync", automation=False)
    )[3].startswith("决策：状态不同步")
    assert format_probe(
        _probe(notify="muted", automation=False, reason="x")
    )[3].startswith("自动化：已停用")
    assert format_probe(_probe(notify="manual"))[3].startswith("决策：手动覆盖中")


def test_format_probe_system_line_skips_comparison_when_it_would_lie():
    """三种情况**不做**一致性比对（比了就是假的不一致）：

    自动化停用（值归手动开关）、手动覆盖中（值由上次手动操作决定）、``desync``
    （比对本身已无意义）。判据是「不出现'一致'二字」，而不是逐句钉住措辞。
    """
    for p in (
        _probe(automation=False, notify="muted", system=False),
        _probe(notify="manual", system=False),
        _probe(notify="desync", system=False),
    ):
        assert format_probe(p)[4] == "系统：未静音"


def test_format_probe_system_line_reports_the_mismatch_with_expectation():
    """可比时不一致要**说出期望值**：只说"不符"用户不知道谁对。"""
    assert format_probe(_probe(notify="muted", system=False))[4] == (
        "系统：未静音 · 与决策不符（期望静音）"
    )
    assert format_probe(_probe(notify="allowed", system=True))[4] == (
        "系统：已静音 · 与决策不符（期望允许通知）"
    )


def test_format_probe_unreadable_system_is_not_treated_as_off():
    """读不出 ≠ 未静音：``None`` 不许当 ``False`` 拿去比对（那会凭空报一个不符）。"""
    assert format_probe(_probe(system=None))[4] == "系统：读不出（无法读取系统勿扰状态）"


def test_diagnostic_text_carries_raw_values_and_rule_patterns():
    """剪贴板那份比屏幕上多两段**维护者才用得上**的：原始取值名 ＋ 规则 pattern。"""
    p = _probe(
        app="running", display="extended", notify="muted", reason="录屏软件运行中",
        system=True, debounce=3, open_on_startup=True,
        rules=(
            RuleLine("screencast", "录屏软件运行中", ("OBS", "Kap")),
            RuleLine("empty", "", ()),
        ),
        hits=("录屏软件运行中",),
    )
    text = diagnostic_text(p, version="9.9.9", stamp="2026-09-19 09:00:00")
    assert text.startswith("BarKit · 录屏通知 诊断\n")
    assert "生成时间：2026-09-19 09:00:00　版本：9.9.9" in text
    assert "app=running display=extended notify=muted" in text
    # 防抖只报轮数：秒数要读框架的 `poll`，D27 不许组件碰那个键（报了就是第二真源）
    assert "防抖：3 轮（一轮 = 框架节拍，见设置窗里本组件页尾的「运行间隔」）" in text
    assert "- screencast（录屏软件运行中），进程：OBS、Kap" in text
    # 这两句是排版层替维护者下的判据，不是让他自己对着一行空再看半天
    assert "（没有进程名 → 这条永不命中）" in text
    assert "（无原因文案）" in text


def test_diagnostic_text_without_rules_says_so():
    text = diagnostic_text(_probe(), version="1.0", stamp="t")
    assert "[规则] 0 条" in text
    assert "- （无）" in text


# ---------- 2.11.2 插件侧：debounce / probe / open_data_dir ----------

def test_rule_reason_prefers_reason_then_name():
    """面板副标题、设置页行、拨测报告**共用这一处取词**：各写一遍必然漂移。

    退到名称的条件是 ``reason`` **空串 / 缺失**（``or`` 的口径）；只由空白组成的串
    算"有" —— 取完 ``strip`` 得空串，调用方按"没原因"渲染。这层语义与抽取前
    ``_hit_reason`` 那行内联表达式逐字同义，故取值口径零变更（本函数只是提取）。
    """
    assert _rule_reason({"name": "a", "reason": "  飞书会议中  "}) == "飞书会议中"
    assert _rule_reason({"name": "a", "reason": ""}) == "a"
    assert _rule_reason({"name": "a"}) == "a"
    assert _rule_reason({"name": "a", "reason": "   "}) == ""
    assert _rule_reason({}) == ""


def test_debounce_defaults_to_schema_and_clamps_to_one(tmp_path, monkeypatch):
    """下限 1：0 轮等于不看信号（那是「关掉守卫」，不是「调参」）。"""
    plugin, fake = _plugin(tmp_path, monkeypatch)
    assert plugin.debounce == 2
    assert plugin.set_debounce(0) is True
    assert plugin.debounce == 1
    assert plugin.ctx.config.get("plugins.notify_guard", {})["debounce"] == 1
    plugin.stop()


def test_set_debounce_round_trip_syncs_the_decider_immediately(tmp_path, monkeypatch):
    """写完立即同步 decider：等下一拍才生效的话，用户点完的那段时间界面在撒谎。"""
    plugin, fake = _plugin(tmp_path, monkeypatch)
    assert plugin.decider.debounce == 2
    assert plugin.set_debounce(4) is True
    assert plugin.ctx.config.get("plugins.notify_guard", {})["debounce"] == 4
    assert plugin.decider.debounce == 4
    assert plugin.set_debounce(4) is True          # 同值：幂等，不写盘也不报错
    plugin.stop()


def test_set_debounce_failure_returns_false_and_leaves_the_decider_alone(tmp_path, monkeypatch):
    """写盘失败 ⇒ False ＋ 内存读数不变（界面不该显示一个没落盘的值）。"""
    plugin, fake = _plugin(tmp_path, monkeypatch)

    def boom(plugin_id, mutate):
        raise OSError("盘写不了")

    monkeypatch.setattr(plugin.ctx.config, "update_plugin_section", boom)
    assert plugin.set_debounce(5) is False
    assert plugin.debounce == 2
    assert plugin.decider.debounce == 2
    plugin.stop()


def test_component_has_no_way_to_read_the_framework_poll(tmp_path, monkeypatch):
    """组件**读不到**框架节拍 —— D27 规定 ``plugins.<id>.poll`` 只服务调度器一个读者。

    这条不是"顺手加个断言"：它是设置页那句「只说轮数、不报秒数」的**依据**。
    读得到的通道一旦出现（多一个 ``poll_seconds()`` 之类），秒数就会作为第二真源
    重新长出来，而用户在页尾改了运行间隔之后它还报着旧值。
    AST 门禁 ``test_component_does_not_read_framework_poll_key`` 是它的硬版本。
    """
    plugin, fake = _plugin(tmp_path, monkeypatch)
    assert not hasattr(plugin, "poll_seconds")
    assert not hasattr(plugin, "poll")
    plugin.stop()


def test_probe_is_read_only(tmp_path, monkeypatch):
    """拨测回答「现在是什么情况」，一个字节都不写。

    判据是两条**记账**都没动：系统通知开关没被写，``tick`` 的 ``_last_combo`` /
    ``_last_hits`` 也没被改成诊断的账 —— 「点一下诊断，通知被静音了」是事故不是功能。
    """
    plugin, fake = _plugin(tmp_path, monkeypatch)
    plugin._last_combo = ("sentinel", True)
    hit = {"name": "sentinel", "reason": "哨兵", "processes": ["x"]}
    plugin._last_hits = [hit]
    before = list(fake.writes)

    p = plugin.probe()

    assert isinstance(p, Probe)
    assert fake.writes == before
    assert plugin._last_combo == ("sentinel", True)
    assert plugin._last_hits == [hit]
    plugin.stop()


def test_probe_reads_the_current_signals(tmp_path, monkeypatch):
    """拨测当场重读三级线索与配置 —— 「刚发现不对」时用户要的是**此刻**的事实。"""
    plugin, fake = _ticked(
        tmp_path, monkeypatch, rounds=2, app_state=signals.DISPLAY_RUNNING, virtual=True
    )
    monkeypatch.setattr(
        "barkit.plugins.notify_guard.plugin.signals.get_process_snapshot",
        lambda: ["/applications/obs.app/contents/macos/obs"],
    )
    p = plugin.probe()
    assert p.app == signals.DISPLAY_RUNNING
    assert p.display == signals.DISPLAY_EXTENDED          # 走真实四态合成（输入被替身接管）
    assert [r.name for r in p.rules] == ["lark", "screencast"]
    assert p.rules[1].processes == ("OBS", "Kap", "zoom.us")
    assert p.hits == ("录屏软件运行中",)                   # 命中那条的**原因文案**，不是规则名
    assert p.automation is True
    assert p.system is False                              # FakeBackend 的 dndMirrored
    assert p.debounce == 2
    assert p.open_on_startup is False
    plugin.stop()


def test_probe_reports_fresh_hits_next_to_a_decision_it_did_not_rerun(tmp_path, monkeypatch):
    """命中行与决策行**允许**不一致：那是「还没轮到写系统」的样子，不是矛盾。

    本用例先 tick 到稳态（无命中 ⇒ allowed），再把快照换成命中 OBS 后立刻拨测 ——
    决策仍是上一次 tick 的结论，命中却是当场重读的。这正是 ``diagnostics`` 段头
    写明的两条口径，钉住它以防后来者顺手"修"成一致的（那会引入防抖窗口内的假不一致）。
    """
    plugin, fake = _ticked(
        tmp_path, monkeypatch, rounds=2, app_state=signals.DISPLAY_RUNNING, virtual=True
    )
    assert plugin.notify_state() == "allowed"
    monkeypatch.setattr(
        "barkit.plugins.notify_guard.plugin.signals.get_process_snapshot",
        lambda: ["/applications/obs.app/contents/macos/obs"],
    )
    p = plugin.probe()
    assert p.notify == "allowed" and p.hits == ("录屏软件运行中",)
    plugin.stop()


# ---------- 2.11.3 设置页：防抖下拉 ----------

def test_editor_debounce_choices_are_a_plain_round_ladder(tmp_path, monkeypatch, qtbot):
    """1–5 轮，**只说轮数不报秒数**；**不设「默认」项**（那是 ``poll`` 的语汇）。

    秒数要读框架节拍 ``poll``（D27：那个键只服务调度器），组件读不到 ⇒ 报出来就是
    第二真源。单位交给旁边那句 note 指去框架页。
    """
    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    choices = editor._debounce_choices()
    assert [v for _label, v in choices] == ["1", "2", "3", "4", "5"]
    assert [label for label, _v in choices] == ["1 轮", "2 轮", "3 轮", "4 轮", "5 轮"]
    assert editor._debounce_sel.current() == "2"
    plugin.stop()


def test_editor_debounce_choices_include_the_off_ladder_value(tmp_path, monkeypatch, qtbot):
    """用户手改过 YAML（7 轮）⇒ 如实列出，**绝不显示成别的数**（否则一改就静默改写）。"""
    from barkit.plugins.notify_guard import settings as ng_settings

    plugin, fake = _plugin(tmp_path, monkeypatch, extra="    debounce: 7\n")
    page = ng_settings.build_page(plugin)
    qtbot.addWidget(page)
    editor = page.findChild(ng_settings._RulesEditor)
    editor._page_keepalive = page
    assert [v for _label, v in editor._debounce_choices()] == ["1", "2", "3", "4", "5", "7"]
    assert editor._debounce_sel.current() == "7"
    plugin.stop()


def test_editor_debounce_change_writes_and_confirms(tmp_path, monkeypatch, qtbot):
    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    editor._on_debounce("4")
    assert plugin.debounce == 4
    assert editor._list_fb.text() == "防抖已设为 4 轮。"
    plugin.stop()


def test_editor_debounce_failure_rolls_back_the_selector(tmp_path, monkeypatch, qtbot):
    """写盘失败 ⇒ 下拉回原位 ＋ 行内红字（界面不许显示一个没落盘的值）。"""
    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    monkeypatch.setattr(plugin, "set_debounce", lambda cycles: False)
    editor._debounce_sel.set_current("4")          # 用户已经选到了 4
    editor._on_debounce("4")
    assert editor._debounce_sel.current() == "2"
    assert "保存失败" in editor._list_fb.text()
    plugin.stop()


# ---------- 2.11.4 设置页：规则拖排序 ----------

def test_editor_rule_reorder_persists_by_key_not_by_index(tmp_path, monkeypatch, qtbot):
    """键 → 规则对象 → 新数组：全程不碰下标残留。

    下标一旦被烘进行里，换序之后点「B」那一行就会进「A」的编辑页。
    """
    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    assert editor._on_rule_reorder("r1", 0) is True
    assert [r["name"] for r in plugin.rules()] == ["screencast", "lark"]
    assert [r["name"] for r in editor._rules] == ["screencast", "lark"]
    plugin.stop()


def test_editor_rule_reorder_failure_rolls_back_memory_too(tmp_path, monkeypatch, qtbot):
    """写盘失败 ⇒ 内存序也退回（内存换了序而盘上没换，下一次开机就变脸）。"""
    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    monkeypatch.setattr(plugin, "save_rules", lambda rules: False)
    assert editor._on_rule_reorder("r1", 0) is False
    assert [r["name"] for r in editor._rules] == ["lark", "screencast"]
    assert [r["name"] for r in plugin.rules()] == ["lark", "screencast"]
    assert "保存失败" in editor._list_fb.text()
    plugin.stop()


def test_editor_rule_reorder_unknown_key_and_noop_are_false(tmp_path, monkeypatch, qtbot):
    """两种「不该落库」：键不认识（列表已被重建）与位置没变（别白写一次盘）。"""
    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    assert editor._on_rule_reorder("ghost", 0) is False
    assert editor._on_rule_reorder("r0", 0) is False
    plugin.stop()


def test_editor_rule_handle_is_inside_each_row(tmp_path, monkeypatch, qtbot):
    """手柄必须真的进了行 —— 否则那一行看得见却拖不动（``OrderEditor`` 的硬约束）。"""
    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    assert set(editor._order._handles) == {"r0", "r1"}
    for key, handle in editor._order._handles.items():
        assert handle.parent() is editor._order._rows[key]
    plugin.stop()


def test_editor_row_click_lands_on_the_same_rule_after_a_swap(tmp_path, monkeypatch, qtbot):
    """行点击：键 → 规则对象 → **当下**的下标（所以换序后仍指同一条规则）。"""
    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    assert editor._on_rule_reorder("r1", 0) is True     # 键 r1 所指那条被搬到首位
    editor._open_edit_key("r1")                         # 行尚未重建，键仍指着同一个对象
    assert _on_edit_screen(editor)
    assert editor._editing == 0                         # 对象换了位置，下标跟着变
    assert editor._name_edit.text() == "screencast"

    editor._stack.back()
    editor._open_edit_key("ghost")                      # 不认的键不动导航
    assert not _on_edit_screen(editor)
    plugin.stop()


def test_editor_empty_state_and_list_are_mutually_exclusive(tmp_path, monkeypatch, qtbot):
    """空态提示与列表**互斥**：列表为空时不给一个空列表。"""
    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    assert editor._order.isVisibleTo(editor) is True
    assert editor._empty_note.isVisibleTo(editor) is False

    editor._rules = []
    editor._refresh_list()
    assert editor._order.isVisibleTo(editor) is False
    assert editor._empty_note.isVisibleTo(editor) is True
    plugin.stop()


# ---------- 2.11.5 设置页：运维三枚 ----------

def test_editor_probe_result_appears_only_after_clicking(tmp_path, monkeypatch, qtbot):
    """拨测结果平时不占版面（空串 ＋ hidden），点过才长出来，且是五行。"""
    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    assert editor._probe_lbl.isVisibleTo(editor) is False
    editor._on_probe()
    assert editor._probe_lbl.isVisibleTo(editor) is True
    assert editor._probe_lbl.text().splitlines()[0].startswith("装置：")
    assert len(editor._probe_lbl.text().splitlines()) == 5
    plugin.stop()


def test_editor_probe_result_expires_when_the_list_is_rebuilt(tmp_path, monkeypatch, qtbot):
    """拨测是「那一刻」的快照：列表一重建它就过期（同 ``_list_fb`` 的判据）。"""
    plugin, fake, editor = _editor(tmp_path, monkeypatch, qtbot)
    editor._on_probe()
    editor._refresh_list()
    assert editor._probe_lbl.text() == ""
    assert editor._probe_lbl.isVisibleTo(editor) is False
    plugin.stop()


def test_plugin_diagnostics_text_is_a_full_report(tmp_path, monkeypatch):
    """诊断文本（框架「复制诊断」的取数口）：比屏幕上那份多出**维护者才用得上**的两段。

    形状（放进剪贴板 / 说一句回执）2026-09-22 已提为框架件（``shell/data_ops``），
    组件侧只负责**内容** —— 这条用例守的就是「内容仍然完整」。
    """
    plugin, fake = _plugin(tmp_path, monkeypatch)
    text = plugin.diagnostics_text()
    assert text.startswith("BarKit · 录屏通知 诊断")
    assert "[拨测]" in text and "[原始取值]" in text and "[规则] 2 条" in text
    plugin.stop()


def test_plugin_diagnostics_text_reprobes_each_call(tmp_path, monkeypatch):
    """每次调用**当场重新拨测**（用户点它的场景就是他刚发现不对，要的是此刻的事实）。"""
    plugin, fake = _plugin(tmp_path, monkeypatch)
    calls: list[int] = []
    real = plugin.probe

    def counted():
        calls.append(1)
        return real()

    monkeypatch.setattr(plugin, "probe", counted)
    plugin.diagnostics_text()
    plugin.diagnostics_text()
    assert len(calls) == 2
    plugin.stop()


def test_toggle_action_reverts_decider_on_write_failure(tmp_path, monkeypatch):
    """写入系统失败时，decider 不能停在 manual：否则面板显示已切换、系统其实没变，
    且 ``step`` 在 manual 且 combo 未变时恒返回 ``none`` ⇒ 永不重试（P1）。"""
    fail = FakeBackend(applied_allow=True, fail=True)
    monkeypatch.setattr(
        "barkit.plugins.notify_guard.plugin.DndMirroredBackend", lambda: fail
    )
    monkeypatch.setattr(
        "barkit.plugins.notify_guard.plugin.signals.detect_app_state", lambda: "idle"
    )
    monkeypatch.setattr(
        "barkit.plugins.notify_guard.plugin.signals.detect_virtual_display", lambda: False
    )
    monkeypatch.setattr(
        "barkit.plugins.notify_guard.plugin.signals.get_process_snapshot", lambda: []
    )
    plugin = Plugin(_plugin_ctx(tmp_path))
    plugin.start()
    assert plugin.decider.mode == "auto"
    result = plugin.toggle_action()
    assert result is not None
    assert result.outcome == ActionOutcome.FAILED
    assert plugin.decider.mode == "auto"   # 没切到 manual
    assert fail.writes == []               # 写失败那一次没落盘


def test_detect_virtual_display_skips_profiler_when_no_external(monkeypatch):
    """无非内建屏的稳态下，TTL 过期兜底重查不应 fork system_profiler（P1 性能）。"""
    import time as _time

    calls = []
    monkeypatch.setattr(signals, "_detect_by_profiler",
                        lambda: (calls.append(1) or False))
    sig = [(0, True)]  # 一块内建屏，没有 OpenDisplay
    monkeypatch.setattr(signals, "_display_signature", lambda: sig)

    saved = dict(signals._cache)
    try:
        signals._cache["disp_sig"] = sig
        signals._cache["s1_at"] = _time.monotonic() - 1000  # TTL 已过期
        signals._cache["s1"] = False
        assert signals.detect_virtual_display() is False
        assert calls == [], "无非内建屏时不该 fork system_profiler"
    finally:
        signals._cache.clear()
        signals._cache.update(saved)


def test_detect_virtual_display_gives_up_after_the_retry_window(monkeypatch):
    """挂着外接屏但指纹不符时，TTL 兜底也不许变成**常驻节拍**。

    违规必抓：把 ``_cache["s1_retry"] >= S1_MAX_RETRY`` 那道闸去掉（回到「过期就重查
    一次」），本用例在 10 轮过期里会收到 10 次 fork 而变红。

    这是 2026-10-09 的实测：那行 DEBUG 全史 969 条、间隔 57~63s、单次 220ms ——
    **只在挂着虚拟屏时**发生（签名里始终有非内建屏 ⇒ 旧的兜底从不短路），与模块自述的
    「日常零调用」矛盾（P1 性能 + 日志刷屏）。
    """
    calls = []
    monkeypatch.setattr(
        signals, "_detect_by_profiler", lambda: (calls.append(1) or False)
    )
    sig = [(0, True), (1, False)]      # 内建屏 ＋ 一块外接屏（指纹不符）
    monkeypatch.setattr(signals, "_display_signature", lambda: sig)

    saved = dict(signals._cache)
    try:
        signals._cache["disp_sig"] = sig
        signals._cache["s1"] = False
        signals._cache["s1_retry"] = 0
        for _ in range(10):            # 模拟 TTL 反复过期
            signals._cache["s1_at"] = -1e9
            assert signals.detect_virtual_display() is False
        assert len(calls) == signals.S1_MAX_RETRY, (
            f"补查了 {len(calls)} 次，超过上限 {signals.S1_MAX_RETRY}"
        )
    finally:
        signals._cache.clear()
        signals._cache.update(saved)


def test_detect_virtual_display_does_not_recheck_after_it_is_confirmed(monkeypatch):
    """已确认到 OpenDisplay 屏 ⇒ 签名没变就不必再问（结论不会变）。

    违规必抓：把那道 ``if _cache["s1"]`` 短路去掉，本用例会收到 5 次多余的 fork。
    """
    calls = []
    monkeypatch.setattr(
        signals, "_detect_by_profiler", lambda: (calls.append(1) or True)
    )
    sig = [(0, True), (1, False)]
    monkeypatch.setattr(signals, "_display_signature", lambda: sig)

    saved = dict(signals._cache)
    try:
        signals._cache["disp_sig"] = sig
        signals._cache["s1"] = True
        signals._cache["s1_retry"] = 0
        for _ in range(5):
            signals._cache["s1_at"] = -1e9
            assert signals.detect_virtual_display() is True
        assert calls == [], "确认到之后还在按 TTL 重查"
    finally:
        signals._cache.clear()
        signals._cache.update(saved)


def test_detect_virtual_display_resets_the_retry_window_on_a_signature_change(monkeypatch):
    """补查窗口是**每次签名变化**重置的 —— 插拔一次就该重新给满额度。

    违规必抓：签名变化分支里去掉 ``_cache["s1_retry"] = 0``，本用例在插上新屏之后
    拿到的仍是「窗口已用尽」而变红。
    """
    calls = []
    monkeypatch.setattr(
        signals, "_detect_by_profiler", lambda: (calls.append(1) or False)
    )
    state = {"sig": [(0, True), (1, False)]}
    monkeypatch.setattr(signals, "_display_signature", lambda: state["sig"])

    saved = dict(signals._cache)
    try:
        signals._cache["disp_sig"] = state["sig"]
        signals._cache["s1"] = False
        signals._cache["s1_retry"] = 0
        for _ in range(signals.S1_MAX_RETRY + 2):    # 先把补查窗口用尽
            signals._cache["s1_at"] = -1e9
            signals.detect_virtual_display()
        used = len(calls)
        signals._cache["s1_at"] = -1e9
        signals.detect_virtual_display()
        assert len(calls) == used, "窗口用尽之后还在重查"

        state["sig"] = [(0, True), (1, False), (2, False)]   # 又插了一块屏
        signals._cache["s1_at"] = -1e9
        signals.detect_virtual_display()
        assert len(calls) == used + 1, "签名变了却没有重新确认"
        assert signals._cache["s1_retry"] == 0, "签名变了却没重置补查窗口"
    finally:
        signals._cache.clear()
        signals._cache.update(saved)
