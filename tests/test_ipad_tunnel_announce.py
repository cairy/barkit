"""iOS 端签名到期通告（announce）：解析、拉取、显示与插件接线。

机制与口径见 ``plugins/ipad_tunnel/announce.py`` 与 design.md §17：
- 协议只有**绝对时刻**，「还剩几天」由 Mac 显示时现算；
- 三态（未通告 / absent / ok / unreadable），失败不许拿 0 或「无限期」顶替；
- 拉取挂在探针「链路翻上来」那一次，**不加节拍**；没拿到**不清旧账**；
- 面板只在跨过阈值时挂尾，设置页恒显示 —— 两处消费同一份归一结果。
"""
from __future__ import annotations

import time

import test_ipad_tunnel as base
from barkit.plugins.ipad_tunnel import announce, mux as bridge_mux


# ──────────────────────────────────────────────
# parse：应答字节 → 归一三态
# ──────────────────────────────────────────────

def _ok_payload(expires="2026-10-16T07:59:59Z", announced="2026-10-09T06:00:00Z"):
    import json
    return json.dumps({"version": 1, "state": "ok",
                       "expires_at": expires, "announced_at": announced}).encode()


def test_parse_ok_normalizes_to_epoch():
    data = announce.parse(_ok_payload())
    assert data["state"] == "ok"
    assert isinstance(data["expires_at"], float)
    # 绝对时刻换算没跑偏：2026-10-16T07:59:59Z 的 epoch 必须大于「此刻」的锚
    assert data["expires_at"] > 1_700_000_000
    assert data["announced_at"] is not None


def test_parse_three_states_and_garbage():
    assert announce.parse(b'{"version":1,"state":"absent","announced_at":null}')["state"] == "absent"
    assert announce.parse(b'{"version":1,"state":"unreadable"}')["state"] == "unreadable"
    # 非 JSON / 非对象 / 版本不符 / ok 但缺 expires_at / 无时区的时刻 —— 一律 None（不猜）
    assert announce.parse(b"not json") is None
    assert announce.parse(b"[1,2]") is None
    assert announce.parse(b'{"version":2,"state":"ok","expires_at":"2026-10-16T07:59:59Z"}') is None
    assert announce.parse(b'{"version":1,"state":"ok"}') is None
    assert announce.parse(b'{"version":1,"state":"ok","expires_at":"2026-10-16T07:59:59"}') is None
    assert announce.parse(b'{"version":1,"state":"whatever"}') is None


# ──────────────────────────────────────────────
# days_left / 文案：绝对时刻现算，阈值只在面板
# ──────────────────────────────────────────────

def test_days_left_calendar_semantics():
    now = time.time()
    assert announce.days_left(announce.parse(_ok_payload(
        expires=_iso(now + 3 * 86400 + 3600))), now=now) == 3
    assert announce.days_left(announce.parse(_ok_payload(
        expires=_iso(now + 43200))), now=now) == 0   # 半天 → 0（不是 1）
    # 已过期：-1（不是 0，两种现场要说的话不同）
    assert announce.days_left(announce.parse(_ok_payload(
        expires=_iso(now - 86400))), now=now) == -1
    # 未通告 / absent / unreadable：None
    assert announce.days_left(None) is None
    assert announce.days_left({"state": "absent", "expires_at": None}) is None


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_settings_text_covers_all_states():
    now = time.time()
    assert announce.settings_text(None) == "未通告（旧版 iOS 端，或 iPad 未连接）"
    assert "商店" in announce.settings_text({"state": "absent", "expires_at": None})
    assert "读不出来" in announce.settings_text({"state": "unreadable", "expires_at": None})
    ok = announce.parse(_ok_payload(expires=_iso(now + 5 * 86400 + 60)))
    text = announce.settings_text(ok, now=now)
    assert "还剩 5 天" in text and "到期" in text
    expired = announce.parse(_ok_payload(expires=_iso(now - 86400)))
    assert "已到期" in announce.settings_text(expired, now=now)
    assert "仍在运行" in announce.settings_text(expired, now=now)


def test_panel_suffix_only_speaks_past_threshold():
    now = time.time()
    # 未通告 / 还剩 4 天：一字不加
    assert announce.panel_suffix(None) == ""
    assert announce.panel_suffix(announce.parse(_ok_payload(
        expires=_iso(now + 4 * 86400 + 60))), now=now) == ""
    # 恰好 3 天：加尾
    assert "剩 3 天" in announce.panel_suffix(announce.parse(_ok_payload(
        expires=_iso(now + 3 * 86400 + 60))), now=now)
    # 0 天 / 已过期：各说各话
    assert "今天到期" in announce.panel_suffix(announce.parse(_ok_payload(
        expires=_iso(now + 3600))), now=now)
    assert "已过期" in announce.panel_suffix(announce.parse(_ok_payload(
        expires=_iso(now - 86400))), now=now)


# ──────────────────────────────────────────────
# fetch：usbmuxd 直连回环 9002，读至 EOF
# ──────────────────────────────────────────────

class _FakeDeviceSocket:
    """「连接即倾倒」的对端：吐一次字节就 EOF。"""

    def __init__(self, payload: bytes):
        self._payload = payload
        self.closed = False

    def settimeout(self, t):
        assert t == announce.ANNOUNCE_READ_TIMEOUT_S

    def recv(self, n):
        p, self._payload = self._payload, b""
        return p

    def close(self):
        self.closed = True


def test_fetch_reads_to_eof_and_closes(monkeypatch):
    seen = {}

    def fake_open(device_id, port, **kw):
        seen["device_id"], seen["port"] = device_id, port
        return _FakeDeviceSocket(_ok_payload())

    monkeypatch.setattr(bridge_mux, "pick_device", lambda udid: (7, "SERIAL"))
    monkeypatch.setattr(bridge_mux, "open_device_tunnel", fake_open)
    data = announce.fetch()
    assert seen == {"device_id": 7, "port": announce.ANNOUNCE_PORT}
    assert data["state"] == "ok"
    assert seen["device_id"]


def test_fetch_maps_all_failures_to_none(monkeypatch):
    from barkit.plugins.ipad_tunnel.consts import ConnectRefused, DeviceNotFound

    monkeypatch.setattr(bridge_mux, "pick_device",
                        lambda udid: (_ for _ in ()).throw(DeviceNotFound("no dev")))
    assert announce.fetch() is None

    monkeypatch.setattr(bridge_mux, "pick_device", lambda udid: (7, "S"))
    monkeypatch.setattr(bridge_mux, "open_device_tunnel",
                        lambda *a, **k: (_ for _ in ()).throw(ConnectRefused(" refused")))
    assert announce.fetch() is None          # 旧版 iOS 端：常态，不是故障

    monkeypatch.setattr(bridge_mux, "open_device_tunnel",
                        lambda *a, **k: _FakeDeviceSocket(b"garbage"))
    assert announce.fetch() is None          # 应答不合法：不猜


# ──────────────────────────────────────────────
# 插件接线：链路翻转触发拉取；快照挂尾
# ──────────────────────────────────────────────

def test_plugin_link_flip_fetches_announce(tmp_path, monkeypatch):
    plugin = base._plugin(tmp_path, monkeypatch)
    assert plugin._announce is None

    payload = announce.parse(_ok_payload())
    monkeypatch.setattr(announce, "fetch", lambda *a, **k: payload)
    plugin._on_probe_link_change(True)
    assert plugin._announce is payload

    # 翻下去不拉（更不清旧账）：拉取函数若被调会换成 None，这里不该发生
    plugin._on_probe_link_change(False)
    assert plugin._announce is payload

    # 没拿到（None）**不清旧账** —— 旧缓存仍然为真
    monkeypatch.setattr(announce, "fetch", lambda *a, **k: None)
    plugin._on_probe_link_change(True)
    assert plugin._announce is payload
    # 守的是「没拿到新的也不清旧账」，不是文案档位。断言不写「还剩 N 天」：
    # 那个词只在到期日**还没到**时出现，而桩里的到期日是写死的 2026-10-16 ⇒
    # 写成「还剩」就是一颗时间炸弹（过了那天必红，与本次改动无关）。
    assert "2026-10-16" in plugin.announce_text()
    plugin.stop()


def test_snapshot_appends_expiry_suffix_past_threshold(tmp_path, monkeypatch):
    plugin = base._plugin(tmp_path, monkeypatch)
    base._push(plugin, {"ok": True, "active": True, "utun": "utun9",
                        "dns_set": True, "ipad_reachable": True,
                        "checked_at": time.time()})
    plugin.tick()                          # 观测副本在 tick 里刷新（与生产路径一致）
    now = time.time()

    # 未通告 / 未到阈值：一字不加
    plugin._announce = None
    summary = plugin._snapshot().summary
    assert "证书" not in summary
    plugin._announce = announce.parse(_ok_payload(expires=_iso(now + 6 * 86400)))
    assert "证书" not in plugin._snapshot().summary

    # 跨过阈值：挂尾
    plugin._announce = announce.parse(_ok_payload(expires=_iso(now + 2 * 86400 + 60)))
    assert "剩 2 天" in plugin._snapshot().summary

    # 已过期 + 进程仍活着（announce 通道还在 = 进程活着）：如实说
    plugin._announce = announce.parse(_ok_payload(expires=_iso(now - 3600)))
    assert "已过期" in plugin._snapshot().summary
    plugin.stop()


# ──────────────────────────────────────────────
# 设置页那行是**活行**：设置窗启动期就构建好了，构建期读一次必定格在「未通告」
# ──────────────────────────────────────────────

class _FakeLabel:
    """设置页那行的替身：只用 text / setText / destroyed 三个口，不拉 Qt 起来。"""

    class _Signal:
        def __init__(self):
            self._slots = []

        def connect(self, fn):
            self._slots.append(fn)

        def emit(self, *args):
            for fn in list(self._slots):
                fn(*args)

    def __init__(self, text: str = ""):
        self._text = text
        self.destroyed = _FakeLabel._Signal()

    def text(self) -> str:
        return self._text

    def setText(self, t: str) -> None:
        self._text = t

    def die(self) -> None:
        """模拟页面销毁（Qt 的 destroyed 信号）。"""
        self.destroyed.emit()


def test_settings_row_follows_later_fetch_not_the_build_time_snapshot(tmp_path, monkeypatch):
    """回归（2026-10-09 用户上报「没取到时间」）：设置窗启动期构建，那一刻通告必然
    还没拉到；只读一次的写法会让这行整个会话停在「未通告」。修好后由 tick 续写。"""
    plugin = base._plugin(tmp_path, monkeypatch)
    label = _FakeLabel(plugin.announce_text())
    plugin.attach_announce_label(label)
    assert "未通告" in label.text()          # 构建期：如实说「未通告」

    now = time.time()
    payload = announce.parse(_ok_payload(expires=_iso(now + 5 * 86400 + 60)))
    monkeypatch.setattr(announce, "fetch", lambda *a, **k: payload)
    plugin._on_probe_link_change(True)       # 首翻 ≈启动后 6~12s 才发生

    plugin.tick()                            # 下一个 30s 周期
    assert "还剩 5 天" in label.text()
    plugin.stop()


def test_attach_writes_current_value_immediately(tmp_path, monkeypatch):
    """页面重建（装 / 卸插件触发 rebuild_pages）时缓存可能已有值：挂上就该是现值。"""
    plugin = base._plugin(tmp_path, monkeypatch)
    now = time.time()
    plugin._announce = announce.parse(_ok_payload(expires=_iso(now + 4 * 86400 + 60)))
    label = _FakeLabel("未通告（旧版 iOS 端，或 iPad 未连接）")
    plugin.attach_announce_label(label)
    assert "还剩 4 天" in label.text()
    plugin.stop()


def test_label_ref_is_dropped_when_page_destroyed(tmp_path, monkeypatch):
    """页面销毁后 tick 不得再戳它（C++ 对象已删），引用要摘掉。"""
    plugin = base._plugin(tmp_path, monkeypatch)
    label = _FakeLabel("未通告（旧版 iOS 端，或 iPad 未连接）")
    plugin.attach_announce_label(label)
    label.die()
    assert plugin._announce_label is None
    plugin.tick()                            # 空操作，不抛
    plugin.stop()


# ──────────────────────────────────────────────
# 拿不到要分因（处置不同），且日志按**故障期**出声（长跑进程不许按次刷屏）
# ──────────────────────────────────────────────

def test_fetch_reports_reason_code_for_each_failure(monkeypatch):
    """「没拿到」分四种因：设备不在要去插线、端口没在听要去看 iPad 端 App、契约不符要改
    协议 —— 混成一句「拿不到」会把用户指错方向（同 consts 里结果码 2 / 3 的分法）。"""
    from barkit.plugins.ipad_tunnel.consts import ConnectRefused, DeviceNotFound

    seen: list[str] = []
    monkeypatch.setattr(bridge_mux, "pick_device",
                        lambda udid: (_ for _ in ()).throw(DeviceNotFound("no dev")))
    assert announce.fetch(on_fail=seen.append) is None
    assert seen == [announce.FAIL_NO_DEVICE]

    monkeypatch.setattr(bridge_mux, "pick_device", lambda udid: (7, "S"))
    monkeypatch.setattr(bridge_mux, "open_device_tunnel",
                        lambda *a, **k: (_ for _ in ()).throw(ConnectRefused(" refused")))
    assert announce.fetch(on_fail=seen.append) is None
    assert seen[-1] == announce.FAIL_REFUSED

    monkeypatch.setattr(bridge_mux, "open_device_tunnel",
                        lambda *a, **k: _FakeDeviceSocket(b"garbage"))
    assert announce.fetch(on_fail=seen.append) is None
    assert seen[-1] == announce.FAIL_CONTRACT

    # 控制组：不传 on_fail 时行为一个字不变（老调用方零影响）
    assert announce.fetch() is None


def test_plugin_retries_announce_while_link_up_until_got_one(tmp_path, monkeypatch):
    """回归（2026-10-10 现场）：拉取原先只挂在「链路翻上来」那**一次**，而设备侧应答点
    恰好那一刻不可用 ⇒ **整个会话停在「未通告」**（iPad 端 9002 死了整整一天，Mac 侧只在
    启动那一次试过；设备侧后来好了也没人去问）。

    修好后：链路还在、手里还没通告时低频重试，拿到一份合法应答即停。
    """
    plugin = base._plugin(tmp_path, monkeypatch)
    calls: list[int] = []

    def fake_fetch(*a, **k):
        calls.append(1)
        return None if len(calls) < 3 else announce.parse(_ok_payload())

    monkeypatch.setattr(announce, "fetch", fake_fetch)

    plugin._on_probe_poll(True)                  # 首轮：立刻到期（0.0）⇒ 试一次
    assert len(calls) == 1 and plugin._announce is None

    for _ in range(10):                          # 未到重试间隔：不许每 3s 撞一次设备
        plugin._on_probe_poll(True)
    assert len(calls) == 1

    plugin._announce_next_try = 0.0              # 到点（不必真等 60s）
    plugin._on_probe_poll(True)
    assert len(calls) == 2 and plugin._announce is None

    plugin._announce_next_try = 0.0
    plugin._on_probe_poll(True)                  # 第三次拿到了
    assert len(calls) == 3 and plugin._announce is not None

    for _ in range(10):                          # 拿到了就不必再问（同一份构建不变）
        plugin._announce_next_try = 0.0
        plugin._on_probe_poll(True)
    assert len(calls) == 3

    plugin._on_probe_poll(False)                 # 链路不在：不问
    assert len(calls) == 3
    plugin.stop()


def test_plugin_announce_failure_speaks_once_per_reason(tmp_path, monkeypatch, caplog):
    """日志按**故障期**出声：同一个原因连着拉不到只记一条（本程序要连跑数天、每 60s
    重试一次 ⇒ 按次记就是上千条/天）；换了另一种坏法才再出声（新原因＝新信息）。"""
    import logging

    plugin = base._plugin(tmp_path, monkeypatch)
    box = {"reason": announce.FAIL_NO_DEVICE}

    def fake_fetch(*a, on_fail=None, **k):
        if on_fail is not None:
            on_fail(box["reason"])
        return None

    def trouble_lines() -> list[str]:
        return [r.getMessage() for r in caplog.records
                if "拿不到 iOS 端签名通告" in r.getMessage()]

    monkeypatch.setattr(announce, "fetch", fake_fetch)
    with caplog.at_level(logging.INFO, logger="barkit.plugins.ipad_tunnel.plugin"):
        plugin._refresh_announce()
        plugin._refresh_announce()
        plugin._refresh_announce()               # 三次同一个原因
        assert len(trouble_lines()) == 1

        box["reason"] = announce.FAIL_REFUSED
        plugin._refresh_announce()               # 换了另一种坏法
        assert len(trouble_lines()) == 2
        assert "iPad 不在 USB 上" in trouble_lines()[0]
        assert "应答点没在听" in trouble_lines()[1]

        # 控制组：拿到一次即清账 —— 同样的原因再坏要**重新出声**（闸门不许被永久锁死）
        monkeypatch.setattr(announce, "fetch", lambda *a, **k: announce.parse(_ok_payload()))
        plugin._refresh_announce()
        assert plugin._announce is not None and plugin._announce_trouble is None

        caplog.clear()
        monkeypatch.setattr(announce, "fetch", fake_fetch)
        plugin._refresh_announce()
        assert len(trouble_lines()) == 1
    plugin.stop()
