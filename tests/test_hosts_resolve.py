"""hosts_guard 解析链（policy）测试：解析与渲染在 app 侧，helper 只落盘 /etc/hosts。

覆盖五件事：
1. ``due_hosts`` / ``apply_resolutions`` 的判定矩阵（static / 缓存命中 / 解析成功 /
   fake 段 / 失败保留上次 IP）；
2. ``render_block`` 的块正文格式（``<ip>\\t<host>\\n``，与 helper 落盘格式一致）；
3. ``HostsResolveService`` 的变化检测 / ``force`` / ``resolve=False`` / 异常上报 ——
   「声明意图」的唯一出口，未送达不算插件问题；
4. 插件侧的意图形状（``_build_payload``）与声明出口（``_publish_intent``），
   以及 marker 两侧常量同名同值（漂移即 helper 整份拒绝）；
5. 管理窗取数口 ``status_index`` **只认 app 侧那份**（状态解释归 app）—— 2026-09-30
   「状态列恒显待解析」的回归护栏，见文件末尾两条。

2026-09-29 通道换成会话之后，本文件不再有任何「落盘」断言 —— ``write_rules`` 与
解析缓存（``hosts_resolved.json``）都已删除，取而代之的是「声明出去的那一帧」。
"""

from __future__ import annotations

from barkit import paths
from barkit.capabilities.helper import hostsops
from barkit.core.config import Config
from barkit.core.plugin import PluginContext
from barkit.plugins.hosts_guard import models, resolve, store


def _entry(host, ip="1.1.1.1", *, auto=False, method="auto"):
    return models.make_entry(host, ip, auto=auto, method=method)


# ──────────────────────────────────────────────
# due_hosts：谁需要这一轮真正发网络解析
# ──────────────────────────────────────────────

NOW = 1_000_000.0


def test_due_hosts_picks_auto_only():
    entries = [_entry("a.com", auto=True), _entry("b.com", auto=False)]
    assert resolve.due_hosts(entries, {}, 300, now=NOW) == {"a.com"}


def test_due_hosts_respects_cache_age():
    entries = [_entry("a.com", auto=True)]
    fresh = {"a.com": {"ip": "9.9.9.9", "at": NOW - 10}}
    stale = {"a.com": {"ip": "9.9.9.9", "at": NOW - 301}}
    assert resolve.due_hosts(entries, fresh, 300, now=NOW) == set()
    assert resolve.due_hosts(entries, stale, 300, now=NOW) == {"a.com"}


# ──────────────────────────────────────────────
# apply_resolutions：条目 + 缓存 → 每条 effective_ip / status
# ──────────────────────────────────────────────

def test_static_entry_uses_its_ip_and_no_cache():
    cache: dict = {}
    out = resolve.apply_resolutions([_entry("a.com", "2.2.2.2")], cache, to_resolve=set())
    assert out[0]["effective_ip"] == "2.2.2.2" and out[0]["status"] == "static"
    assert cache == {}                     # 非 auto 永不进缓存


def test_cache_hit_does_not_touch_network():
    cache = {"a.com": {"ip": "3.3.3.3", "method": "doh-pub", "at": 5}}

    def boom(*_a):                          # 断言式 resolver：被调用即失败
        raise AssertionError("to_resolve=set() 时不应发起解析")

    out = resolve.apply_resolutions([_entry("a.com", auto=True)], cache,
                                    to_resolve=set(), resolver=boom)
    assert out[0]["effective_ip"] == "3.3.3.3" and out[0]["status"] == "ok"


def test_resolve_success_updates_cache():
    cache: dict = {}
    out = resolve.apply_resolutions(
        [_entry("a.com", auto=True)], cache, to_resolve={"a.com"},
        resolver=lambda h, m: ("4.4.4.4", "udp-ali", None), now=123)
    assert out[0]["status"] == "ok" and out[0]["effective_ip"] == "4.4.4.4"
    assert cache["a.com"] == {"ip": "4.4.4.4", "method": "udp-ali", "at": 123}


def test_fake_ip_is_rejected_and_falls_back_to_cache():
    """100.64.0.0/10 是 hev mapdns 的段：写进 hosts 会成死循环，必须拒。"""
    cache = {"a.com": {"ip": "5.5.5.5", "method": "doh-pub", "at": 1}}
    out = resolve.apply_resolutions(
        [_entry("a.com", auto=True)], cache, to_resolve={"a.com"},
        resolver=lambda h, m: ("100.64.0.7", "system", None))
    assert out[0]["status"] == "fake" and out[0]["effective_ip"] == "5.5.5.5"
    assert cache["a.com"]["ip"] == "5.5.5.5"      # 假 IP 绝不进缓存


def test_resolve_failure_keeps_last_ip_never_clears():
    cache = {"a.com": {"ip": "5.5.5.5", "method": "doh-pub", "at": 1}}
    out = resolve.apply_resolutions(
        [_entry("a.com", auto=True)], cache, to_resolve={"a.com"},
        resolver=lambda h, m: (None, None, "全部方式失败"))
    assert out[0]["status"] == "stale" and out[0]["effective_ip"] == "5.5.5.5"


def test_resolve_failure_without_cache_falls_back_to_static_ip():
    out = resolve.apply_resolutions(
        [_entry("a.com", "6.6.6.6", auto=True)], {}, to_resolve={"a.com"},
        resolver=lambda h, m: (None, None, "全部方式失败"))
    assert out[0]["status"] == "idle" and out[0]["effective_ip"] == "6.6.6.6"


# ──────────────────────────────────────────────
# render_block
# ──────────────────────────────────────────────

def test_render_block_matches_hosts_line_format():
    entries = [{"host": "a.com", "effective_ip": "1.2.3.4"},
               {"host": "b.com", "effective_ip": "5.6.7.8"}]
    assert resolve.render_block(entries) == "1.2.3.4\ta.com\n5.6.7.8\tb.com\n"
    assert resolve.render_block([]) == ""      # 空 = 没有期望 → helper 摘掉整块


# ──────────────────────────────────────────────
# 跨进程契约：marker 必须两侧同名同值
# ──────────────────────────────────────────────

def test_hosts_block_marker_single_source():
    """helper 只接受封闭取值；两侧常量漂移会让整份意图被拒（hosts 静默不更新）。"""
    assert models.BLOCK_MARKER == hostsops.HOSTS_BLOCK_MARKER
    assert hostsops.HOSTS_WIRE_VERSION == resolve.WIRE_VERSION


# ──────────────────────────────────────────────
# 长驻服务：变化检测 + 声明意图（与 RouteProbeService 同构）
# ──────────────────────────────────────────────

def _payload(content=""):
    return {"version": resolve.WIRE_VERSION, "enabled": True,
            "marker": models.BLOCK_MARKER, "content": content}


class _Publisher:
    """最小声明出口桩：记下每次声明出去的那份意图，可设定是否送达。"""

    def __init__(self, ok=True):
        self.sent: list = []
        self._ok = ok

    def __call__(self, payload):
        self.sent.append(payload)
        return (self._ok, None if self._ok else "守护未连接")


def test_service_declares_only_on_change():
    """期望态没变就不再声明一次（帧也不发）。"""
    pub = _Publisher()
    svc = resolve.HostsResolveService(provider=lambda _r: _payload("x"), publish=pub)
    svc.poll()
    svc.poll()
    assert [p["content"] for p in pub.sent] == ["x"]


def test_service_force_declares_even_when_unchanged():
    """``force=True`` 是 ``sync()`` 走的路径：即使内容没变也要再声明一次。"""
    pub = _Publisher()
    svc = resolve.HostsResolveService(provider=lambda _r: _payload(), publish=pub)
    svc.poll()
    svc.poll(force=True)
    assert len(pub.sent) == 2


def test_service_forwards_resolve_flag_to_provider():
    seen: list = []

    def provider(flag):
        seen.append(flag)
        return _payload()

    svc = resolve.HostsResolveService(provider=provider, publish=_Publisher())
    svc.poll()
    svc.poll(force=True, resolve=False)      # UI 路径：只用缓存，不发网络
    assert seen == [True, False]


def test_service_reports_problem_instead_of_raising():
    reported: list = []
    svc = resolve.HostsResolveService(provider=lambda _r: 1 / 0, publish=_Publisher())
    svc.report_problem = reported.append
    svc.poll()                               # 不得抛出（长驻线程不能死）
    assert reported, "计算异常必须经 report_problem 上报，而不是抛出"
    assert "division by zero" in reported[0]


def test_service_without_publisher_reports_instead_of_crashing():
    """没有会话出口（未接线）时不得抛：记成插件问题，别静默当成功。"""
    reported: list = []
    svc = resolve.HostsResolveService(provider=lambda _r: _payload())
    svc.report_problem = reported.append
    svc.poll()
    assert reported and "出口" in reported[0]


def test_service_undelivered_is_not_a_plugin_problem():
    """「没送到」不是「内容错」：仍记下这份期望态（免得下一轮重复发同样的内容），
    但**不**记成插件问题 —— 守护一起来会按内容哈希幂等补发。"""
    reported: list = []
    pub = _Publisher(ok=False)
    svc = resolve.HostsResolveService(provider=lambda _r: _payload("x"), publish=pub)
    svc.report_problem = reported.append
    svc.poll()
    assert svc.last == _payload("x")
    assert reported == []


# ──────────────────────────────────────────────
# 插件侧接线：意图 v2 的形状与声明出口
# ──────────────────────────────────────────────

class _Link:
    """最小会话桩：记下声明出去的 ``(subject, payload)``。"""

    def __init__(self, ok=True):
        self.sent: list = []
        self._ok = ok

    def declare(self, subject, payload):
        self.sent.append((subject, payload))
        return self._ok


def _plugin(tmp_path, monkeypatch, entries=(), enabled=True, link=None):
    base = tmp_path / "appsupport"
    base.mkdir(exist_ok=True)
    monkeypatch.setattr(paths, "APP_SUPPORT", base)
    monkeypatch.setattr(paths, "HOSTS_ENTRIES_FILE", base / "hosts_entries.json")
    cfg = Config(user_path=tmp_path / "config.yaml")
    if not enabled:
        cfg.set_plugin_enabled("hosts_guard", False)
    store.save_entries(list(entries))
    from barkit.plugins.hosts_guard.plugin import Plugin as HG

    ctx = PluginContext(config=cfg)
    ctx.helper = link if link is not None else _Link()
    return HG(ctx), base


def test_build_payload_renders_block_content(tmp_path, monkeypatch):
    p, _base = _plugin(tmp_path, monkeypatch,
                       entries=[_entry("a.com", "1.2.3.4"), _entry("b.com", "5.6.7.8")])
    payload, err = p._build_payload(resolve_due=False)
    assert err is None
    assert payload["version"] == 2 and payload["marker"] == models.BLOCK_MARKER
    assert payload["content"] == "1.2.3.4\ta.com\n5.6.7.8\tb.com\n"
    # 状态解释归 app：条目带 status，供 _snapshot 判徽标
    assert [e["status"] for e in p._entries] == ["static", "static"]


def test_build_payload_disabled_yields_empty_block(tmp_path, monkeypatch):
    p, _base = _plugin(tmp_path, monkeypatch, entries=[_entry("a.com", "1.2.3.4")])
    monkeypatch.setattr(type(p), "enabled", property(lambda _self: False))
    payload, err = p._build_payload(resolve_due=False)
    assert err is None and payload["enabled"] is False and payload["content"] == ""


def test_sync_declares_intent_without_touching_any_file(tmp_path, monkeypatch):
    """``sync()`` 走服务那条 ``publish`` 出口，把渲染好的意图声明出去。

    2026-09-29 通道换成会话：从前这里断言「写入 hosts_rules.json + touch 敲门」，
    现在两件事合成一件 —— 发一帧意图，且**盘上不留任何中间文件**。
    """
    link = _Link()
    p, base = _plugin(tmp_path, monkeypatch,
                      entries=[_entry("a.com", "1.2.3.4")], link=link)
    ok, err = p.sync()
    assert ok and err is None
    assert link.sent, "sync() 必须把意图声明出去"
    subject, payload = link.sent[-1]
    assert subject == "hosts"
    assert payload["content"] == "1.2.3.4\ta.com\n"
    assert not (base / "hosts_rules.json").exists()
    assert not (base / "trigger").exists()


def test_sync_rejects_duplicate_hosts_without_declaring(tmp_path, monkeypatch):
    """条目主数据出现域名重复（大小写不同也算）→ 整份拒绝：宁可不更新，也不发一半。

    注：单条非法条目在 ``store.load_raw`` 阶段就被滤掉了，走不到这里；能穿透到
    ``normalize_rules`` 的只有「各自合法但组合后冲突」的情形（重复域名）。
    """
    link = _Link()
    p, _base = _plugin(tmp_path, monkeypatch,
                       entries=[_entry("a.com", "1.2.3.4"), _entry("A.com", "5.6.7.8")],
                       link=link)
    ok, err = p.sync()
    assert not ok and err and "重复" in err
    assert link.sent == [], "整份拒绝时连意图都不该声明出去"


def test_on_disabled_declares_empty_block(tmp_path, monkeypatch):
    """禁用钩子触发时 self.enabled 仍为 True（manager 先调钩子后写 config），
    故必须显式声明「全撤」意图，否则 helper 会继续维持托管块。"""
    link = _Link()
    p, _base = _plugin(tmp_path, monkeypatch,
                       entries=[_entry("a.com", "1.2.3.4")], link=link)
    p.on_disabled()
    subject, payload = link.sent[-1]
    assert subject == "hosts"
    assert payload["enabled"] is False and payload["content"] == ""


def test_resident_services_declares_single_stable_service(tmp_path, monkeypatch):
    p, _base = _plugin(tmp_path, monkeypatch)
    svcs = p.resident_services()
    assert [s.name for s in svcs] == ["hosts_resolve"]
    assert p.resident_services()[0] is svcs[0]   # 同实例同批对象（框架按 name 收敛）
    assert svcs[0].interval == resolve.PROBE_INTERVAL


def test_intent_subject_and_subjects_declared(tmp_path, monkeypatch):
    """「写哪个领域」（intent_subject）与「看哪些领域」（subjects）是两个声明点。"""
    p, _base = _plugin(tmp_path, monkeypatch)
    assert p.intent_subject() == "hosts"
    assert p.subjects() == ["hosts"]


# ──────────────────────────────────────────────
# 管理窗取数口：状态 / 生效 IP **只能**来自 app 侧
# ──────────────────────────────────────────────

def test_status_index_reports_app_side_resolution(tmp_path, monkeypatch):
    """回归护栏（2026-09-30 用户上报「状态列恒显待解析」）。

    管理窗那两列的取数口（``Plugin.status_index``）读的必须是 **app 自己算的那份**
    （``self._entries``，状态解释归 app，§7.4.3）。此前它按旧字段名读守护的观测
    （``entries``）—— 那个字段随解析链回迁 app 侧一起作废（守护现在只报
    ``hosts: [{host, ip}]``）⇒ 恒得空表 ⇒ 整列落在默认值上。

    判据落在**取值本身**上：既要有条目，也要是 App 侧算出的那个结论
    （把取数口改回读观测，下面两条断言当场转红）。
    """
    monkeypatch.setattr(resolve, "resolve_host",
                        lambda host, method: ("9.9.9.9", "udp-ali", None))
    p, _base = _plugin(tmp_path, monkeypatch, entries=[
        _entry("auto.example.com", "1.1.1.1", auto=True),
        _entry("static.example.com", "2.2.2.2"),
    ])
    p._resolve_service.poll(force=True, resolve=True)

    idx = p.status_index()
    # 自动更新的那条：状态与 IP 都是解析出来的（不是条目里存的 1.1.1.1）
    assert idx["auto.example.com"]["ip"] == "9.9.9.9"
    assert idx["auto.example.com"]["status"] == "ok"
    assert idx["auto.example.com"]["pending"] is False     # 没在等手动更新
    # 静态那条：不参与解析，IP 就是它自己的
    assert idx["static.example.com"]["ip"] == "2.2.2.2"
    assert idx["static.example.com"]["status"] == "static"


def test_status_index_says_pending_before_the_first_poll(tmp_path, monkeypatch):
    """「待解析」是**真**语义，不是恒定位 —— 这一条挡住另一种谎。

    app 刚启动、长驻服务还没跑过那一轮时，解析缓存是空的 ⇒ 自动更新的条目报 ``idle``
    （文案「待解析」＝还没解析过）；服务跑过一轮才转 ``ok``。若有人图省事把「取不到」
    直接当成「已同步」，本用例转红。
    """
    monkeypatch.setattr(resolve, "resolve_host",
                        lambda host, method: ("9.9.9.9", "udp-ali", None))
    p, _base = _plugin(tmp_path, monkeypatch,
                       entries=[_entry("auto.example.com", "1.1.1.1", auto=True)])
    assert p.status_index()["auto.example.com"]["status"] == "idle"
    p._resolve_service.poll(force=True, resolve=True)
    assert p.status_index()["auto.example.com"]["status"] == "ok"


# ──────────────────────────────────────────────
# 手动「更新」：登记 → 本轮就解析 → 待办划掉
# （2026-09-30 加：管理窗那枚「更新」键的去处）
# ──────────────────────────────────────────────

def test_service_hands_manual_requests_over_and_clears_them():
    """手动请求的生命周期：登记后看得见 → 跑完一轮就划掉（界面靠它收尾）。"""
    svc = resolve.HostsResolveService(provider=lambda _r: _payload(), publish=_Publisher())
    assert svc.request_now(["a.com"]) == 1
    assert svc.pending_hosts() == {"a.com"}
    svc.poll()
    assert svc.pending_hosts() == set()


def test_pending_is_still_visible_while_the_round_is_running():
    """划掉必须发生在 ``provider`` **之后**：早一刻清，界面就会在结论还没落地时说「已完成」。"""
    seen: list = []

    def provider(_resolve_flag):
        seen.append(svc.pending_hosts())     # 干活期间必须还看得见这一条
        return _payload()

    svc = resolve.HostsResolveService(provider=provider, publish=_Publisher())
    svc.request_now(["a.com"])
    svc.poll()
    assert seen == [{"a.com"}]
    assert svc.pending_hosts() == set()


def test_pending_is_cleared_even_when_the_round_fails():
    """失败也要划掉 —— 否则界面永远停在「更新中」，而那一轮早就结束了。"""
    svc = resolve.HostsResolveService(provider=lambda _r: 1 / 0, publish=_Publisher())
    svc.report_problem = lambda _m: None
    svc.request_now(["a.com"])
    svc.poll()
    assert svc.pending_hosts() == set()


def test_requests_arriving_mid_round_wait_for_the_next_one():
    """划掉用**差集**而不是清空：一轮跑的过程中新按的那次不能被一起抹掉。"""

    def provider(_resolve_flag):
        svc.request_now(["b.com"])           # 用户在这一轮跑着的时候又按了一次
        return _payload()

    svc = resolve.HostsResolveService(provider=provider, publish=_Publisher())
    svc.request_now(["a.com"])
    svc.poll()
    assert svc.pending_hosts() == {"b.com"}


def test_refresh_entries_accepts_only_managed_auto_hosts(tmp_path, monkeypatch):
    """插件的资格判定：静态条目与不在册的域名一律不受理（界面判据写坏时的第二道）。"""
    p, _base = _plugin(tmp_path, monkeypatch, entries=[
        _entry("auto.example.com", "1.1.1.1", auto=True),
        _entry("static.example.com", "2.2.2.2"),
    ])
    accepted, _woken = p.refresh_entries(
        ["auto.example.com", "static.example.com", "nope.example.com"])
    assert accepted == 1
    assert p._resolve_service.pending_hosts() == {"auto.example.com"}


def test_status_index_reports_pending_until_the_round_runs(tmp_path, monkeypatch):
    """取数口多出来的那一格：**登记了但还没跑完** —— 管理窗据此显示「更新中」并知道何时停。"""
    monkeypatch.setattr(resolve, "resolve_host",
                        lambda host, method: ("9.9.9.9", "udp-ali", None))
    p, _base = _plugin(tmp_path, monkeypatch,
                       entries=[_entry("auto.example.com", "1.1.1.1", auto=True)])
    p.refresh_entries(["auto.example.com"])
    assert p.status_index()["auto.example.com"]["pending"] is True

    p._resolve_service.poll(force=True, resolve=True)
    idx = p.status_index()
    assert idx["auto.example.com"]["pending"] is False
    assert idx["auto.example.com"]["ip"] == "9.9.9.9"
    assert idx["auto.example.com"]["status"] == "ok"
