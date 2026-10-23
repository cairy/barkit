"""hosts_guard：数据模型 / hosts 解析 / 条目存储 的单元测试。

重点覆盖两类安全防线：
  1) 域名/IP 注入防线（换行劫持 /etc/hosts 的输入必须被拒）；
  2) fake IP 死循环防线（100.64.0.0/10 判定）。
"""

from __future__ import annotations

import pytest

from barkit.contribution.specs import (
    DetailEntrySpec,
    StateSpec,
    StateLevel,
    StatusRowSpec,
    StateReport,
)
from barkit.core.config import Config
from barkit.core.contributions import gather_specs
from barkit.core.plugin import PluginContext
from barkit.shell.panel import StatusPanel, _StatusRowView
from barkit.plugins.hosts_guard import store
from barkit.plugins.hosts_guard.hostsfile import (
    KIND_BLOCK_BEGIN,
    KIND_BLOCK_END,
    KIND_ENTRY,
    block_range,
    parse,
    parse_import_text,
    records,
)
from barkit.plugins.hosts_guard.models import (
    BLOCK_BEGIN,
    BLOCK_END,
    CAT_ADBLOCK,
    CAT_DIRECT,
    make_entry,
    normalize_host,
    normalize_rules,
    valid_host,
    valid_ip,
    is_fake_ip,
    validate_entry,
)
from barkit.plugins.hosts_guard.plugin import Plugin


# ── models：域名校验（注入防线）────────────────────────────────────────

@pytest.mark.parametrize("text, ok", [
    ("example.ddns.net", True),
    ("a.com", True),
    ("localhost", True),
    ("xn--fiqs8s", True),
    ("a" * 253, True),
    # 注入类：换行 / 空白 / 特殊字符一律拒绝
    ("example.com\n1.2.3.4 evil.com", False),
    ("example.com 1.2.3.4", False),
    ("a b.com", False),
    ("bad_host.com", False),
    ("http://x.com", False),
    ("-lead.com", False),
    ("trail-.com", True),   # 字符集合法即可；连字符位置不属安全边界
    ("", False),
    (None, False),
    ("a" * 254, False),
])
def test_valid_host(text, ok):
    assert valid_host(text) is ok


@pytest.mark.parametrize("text, ok", [
    ("203.0.113.10", True),
    ("0.0.0.0", True),
    ("::1", True),
    ("100.64.0.106", True),
    ("1.2.3", False),
    ("999.1.1.1", False),
    ("example.com", False),
    ("", False),
    (None, False),
])
def test_valid_ip(text, ok):
    assert valid_ip(text) is ok


def test_normalize_host():
    assert normalize_host("  Example.COM. ") == "example.com"


# ── models：fake IP 死循环防线 ────────────────────────────────────────

@pytest.mark.parametrize("ip, fake", [
    ("100.64.0.106", True),     # mapdns fake IP
    ("100.127.255.255", True),  # /10 段末
    ("100.63.255.255", False),  # 段前
    ("100.128.0.1", False),     # 段后
    ("203.0.113.10", False),
    ("192.168.1.10", False),
    ("not-an-ip", False),
])
def test_is_fake_ip(ip, fake):
    assert is_fake_ip(ip) is fake


# ── models：条目与规则文件 ────────────────────────────────────────────

def test_make_entry_defaults():
    e = make_entry("A.COM.", "1.2.3.4")
    assert e["host"] == "a.com"
    assert e["category"] == CAT_DIRECT
    assert e["method"] == "auto"
    assert e["auto"] is False


def test_validate_entry():
    assert validate_entry(make_entry("a.com", "1.2.3.4")) is None
    assert validate_entry({"host": "bad host", "ip": "1.2.3.4"}) is not None
    assert validate_entry({"host": "a.com", "ip": "1.2.3.999"}) is not None
    assert validate_entry({"host": "a.com", "ip": "1.2.3.4", "method": "nope"}) is not None
    assert validate_entry("not-a-dict") is not None


def test_normalize_rules_rejects_bad_and_dedup():
    data, err = normalize_rules({"entries": [make_entry("a.com", "1.2.3.4")]})
    assert err is None and data["entries"][0]["host"] == "a.com"
    assert "direct" in data["categories"]  # 预置分类自动补齐

    _, err = normalize_rules({"entries": [{"host": "bad host", "ip": "1.2.3.4"}]})
    assert err and "域名不合法" in err

    dup = [make_entry("a.com", "1.2.3.4"), make_entry("a.com", "5.6.7.8")]
    _, err = normalize_rules({"entries": dup})
    assert err and "重复" in err

    _, err = normalize_rules("not-a-dict")
    assert err

    # poll（线格式字段，配置键 resolve_poll 的落点）越界回退默认：下限 60 与配置侧同一份
    for bad in (1, 45, True, "fast"):
        data, _ = normalize_rules({"entries": [], "poll": bad})
        assert data["poll"] == 300, bad
    data, _ = normalize_rules({"entries": [], "poll": 120})
    assert data["poll"] == 120


# ── hostsfile：解析与标记块 ───────────────────────────────────────────

HOSTS_SAMPLE = "\n".join([
    "127.0.0.1 localhost broadcasthost",   # 系统保留
    "192.168.110.63 homeassistant.local",   # 外部条目
    "",                                    # 空行
    "# 普通注释",                           # 注释
    "10.0.0.5 a.local # 行内注释",          # 行内注释条目
    "this line is broken",                 # 形态异常 → 按注释保留
    BLOCK_BEGIN,
    "203.0.113.10 example.ddns.net",        # 托管条目
    BLOCK_END,
    "1.2.3.4 after.local",                 # 块外
])


def test_parse_line_kinds():
    lines = parse(HOSTS_SAMPLE)
    kinds = [ln.kind for ln in lines]
    assert kinds[0] == KIND_ENTRY
    assert kinds[2] == "blank"
    assert kinds[3] == "comment"
    assert kinds[6] == KIND_BLOCK_BEGIN
    assert kinds[8] == KIND_BLOCK_END
    assert lines[4].kind == KIND_ENTRY and lines[4].ip == "10.0.0.5"
    assert lines[7].kind == KIND_ENTRY and lines[7].ip == "203.0.113.10"
    # 形态异常行原样保留、不参与管理
    assert lines[5].kind == "comment" and lines[5].raw == "this line is broken"


def test_block_range_and_records():
    lines = parse(HOSTS_SAMPLE)
    rng = block_range(lines)
    assert rng is not None
    recs = records(lines)
    by_host = {r.host: r for r in recs}
    assert by_host["example.ddns.net"].managed is True
    assert by_host["example.ddns.net"].ip == "203.0.113.10"
    assert by_host["after.local"].managed is False
    assert by_host["homeassistant.local"].managed is False
    # 一行多域名展开
    assert by_host["localhost"].system is True
    assert by_host["broadcasthost"].system is True
    assert by_host["homeassistant.local"].system is False
    # 行内注释不污染域名
    assert by_host["a.local"].ip == "10.0.0.5"


def test_block_range_unpaired():
    assert block_range(parse("1.2.3.4 a.com")) is None
    only_begin = parse(f"1.2.3.4 a.com\n{BLOCK_BEGIN}\n")
    assert block_range(only_begin) is None


def test_parse_import_text():
    text = "\n".join([
        "# 注释行",
        "",
        "0.0.0.0 ad.example.com",
        "0.0.0.0 tracker.example.net # 注释",
        "bad line here",
        "0.0.0.0 localhost",           # 系统保留 → 跳过
        "0.0.0.0 bad_host.example",    # 非法域名 → 跳过
        "1.2.3.4 ok.example",
    ])
    pairs, skipped = parse_import_text(text)
    hosts = [h for _ip, h in pairs]
    assert hosts == ["ad.example.com", "tracker.example.net", "ok.example"]
    assert skipped == 3


# ── store：条目主数据（conftest 已把路径重定向到 tmp）─────────────────

def test_store_roundtrip_and_upsert():
    assert store.load_entries() == []

    ok, err = store.upsert(make_entry("a.com", "1.2.3.4", auto=True))
    assert ok and err is None
    # 同内容重复 upsert → 无变化不写盘
    ok, err = store.upsert(make_entry("a.com", "1.2.3.4", auto=True))
    assert ok is False and err is None
    # 覆盖同域名
    ok, _ = store.upsert(make_entry("a.com", "5.6.7.8"))
    assert ok
    entries = store.load_entries()
    assert len(entries) == 1 and entries[0]["ip"] == "5.6.7.8"
    # 域名归一化：大写与尾点视为同一条
    ok, _ = store.upsert(make_entry("A.COM.", "9.9.9.9"))
    assert ok and len(store.load_entries()) == 1
    # 非法条目拒绝
    ok, err = store.upsert({"host": "bad host", "ip": "1.2.3.4"})
    assert not ok and err


def test_store_remove_and_categories():
    store.upsert(make_entry("a.com", "1.2.3.4"))
    ok, _ = store.remove("a.com")
    assert ok and store.load_entries() == []
    ok, _ = store.remove("a.com")            # 不存在 → False 不报错
    assert ok is False

    ok, _ = store.add_category("内部服务")
    assert ok and "内部服务" in store.load_categories()
    ok, err = store.add_category("内部服务")  # 重复
    assert not ok and "已存在" in err
    ok, err = store.add_category("  ")
    assert not ok and err


def test_store_import_entries():
    pairs = [("1.2.3.4", "a.com"), ("5.6.7.8", "b.com"), ("bad", "c.com")]
    added, updated, skipped = store.import_entries(pairs, category=CAT_ADBLOCK)
    assert (added, updated, skipped) == (2, 0, 1)
    # 重复导入 → 覆盖
    added, updated, skipped = store.import_entries(pairs[:2], category=CAT_ADBLOCK)
    assert (added, updated, skipped) == (0, 2, 0)
    cats = store.load_categories()
    assert CAT_ADBLOCK in cats


# ── 插件 v3 契约（批次 3 迁移，对齐 notify_guard 写法）─────────────────

def _plugin(tmp_path, events=None, body: str | None = None) -> Plugin:
    """插件实例：默认按**新键**配（``resolve_poll``，D27 拆键后的写法）。

    ``body`` 换成任意 YAML 片段，供「旧写法该怎么处置」的用例写旧键。
    """
    user = tmp_path / "hosts_guard.yaml"
    user.write_text(
        body or "plugins:\n  hosts_guard:\n    enabled: true\n    resolve_poll: 120\n",
        encoding="utf-8",
    )
    return Plugin(PluginContext(config=Config(user_path=user), events=events))


def _user_section(tmp_path) -> dict:
    """用户配置里的 ``plugins.hosts_guard``（读原始 YAML，看真实落盘）。"""
    import yaml

    raw = yaml.safe_load((tmp_path / "hosts_guard.yaml").read_text(encoding="utf-8"))
    return (raw.get("plugins") or {}).get("hosts_guard") or {}


def test_resolve_poll_reads_new_key_and_falls_back(tmp_path):
    """D27：业务节律只认 ``resolve_poll``，越界 / 写坏 / 缺失一律回退默认 300。"""
    from barkit.plugins.hosts_guard.plugin import DEFAULT_RESOLVE_POLL

    assert _plugin(tmp_path).resolve_poll == 120
    for value in ("45", '"fast"', "0", "true"):     # 下限 60；非整数 / 布尔 / 0 都不收
        p = _plugin(tmp_path, body=(
            f"plugins:\n  hosts_guard:\n    enabled: true\n    resolve_poll: {value}\n"
        ))
        assert p.resolve_poll == DEFAULT_RESOLVE_POLL, value


def test_resolve_interval_follows_new_key_not_framework_poll(tmp_path):
    """D27 核心判据：两个键各管一件事——``poll`` 不再是解析间隔的来源。

    段里同时有两个键时取 ``resolve_poll``；``poll`` 原样留着（它是**框架节拍覆盖**，
    组件既读它做间隔、也不许改写它）。
    """
    p = _plugin(tmp_path, body=(
        "plugins:\n  hosts_guard:\n    enabled: true\n"
        "    poll: 600\n    resolve_poll: 120\n"
    ))
    assert p.resolve_poll == 120
    assert _user_section(tmp_path).get("poll") == 600


def test_solo_poll_is_a_framework_tick_not_a_resolve_interval(tmp_path):
    """只有 ``poll`` 一个键时，它**不是**「升级前的遗留写法」，就是框架节拍覆盖。

    那条「``poll`` → ``resolve_poll``」的一次性迁移已于 2026-09-17 删除（与 route_guard
    同一批裁决：组件不改写用户 YAML）。故这里断言的是：``poll`` 原地留着给框架读，
    解析间隔照 ``resolve_poll`` 的默认值走。
    """
    p = _plugin(tmp_path, body="plugins:\n  hosts_guard:\n    enabled: true\n    poll: 600\n")
    from barkit.plugins.hosts_guard.plugin import DEFAULT_RESOLVE_POLL

    assert p.resolve_poll == DEFAULT_RESOLVE_POLL
    assert _user_section(tmp_path).get("poll") == 600


def test_no_key_migration_rewrites_user_yaml(tmp_path):
    """旧键不迁移：``poll`` / ``state_poll`` 一个字节都不动，也不凭空补新键。"""
    _plugin(tmp_path, body=(
        "plugins:\n  hosts_guard:\n    enabled: true\n    poll: 900\n    state_poll: 5\n"
    ))
    section = _user_section(tmp_path)
    assert section.get("poll") == 900        # 框架节拍覆盖，原地留着
    assert section.get("state_poll") == 5    # 死键也留着（hosts 侧不校验未知键，直接忽略）
    assert "resolve_poll" not in section     # 没给用户补键


def test_plugin_construction_never_rewrites_user_config(tmp_path):
    """幂等 + 不白写：没有迁移这回事，构造插件一个字都不落盘。"""
    user = tmp_path / "hosts_guard.yaml"
    user.write_text(
        "plugins:\n  hosts_guard:\n    enabled: true\n    resolve_poll: 120\n",
        encoding="utf-8",
    )
    before = user.read_text(encoding="utf-8")
    Plugin(PluginContext(config=Config(user_path=user)))
    assert user.read_text(encoding="utf-8") == before


def test_contributions_v2_shape(tmp_path):
    """v3 贡献清单齐全：状态行 + 状态 + 设置页（托盘提示归取值声明，D17）。"""
    plugin = _plugin(tmp_path)
    rows = plugin.row_specs()
    states = list(plugin.state_specs())
    assert any(isinstance(s, StatusRowSpec) and s.key == "main" for s in rows)
    assert any(isinstance(s, StateSpec) and s.stale_after is None for s in states)
    # 规则异常（ERR）的取值进托盘队列（D17）：不再声明徽标规则，取值自己说 alerts ——
    # ERR 的默认即为真，故这一格不必显式写
    declared = next(iter(states)).states
    assert all(o.alerts_on for o in declared if o.level is StateLevel.ERR)
    assert plugin.settings_page() is not None


def test_tick_returns_snapshot(tmp_path):
    """§5.3 闭环：tick() 返回 StateReport（只报取值名，决议 D13；框架自动 publish）。"""
    from barkit.plugins.hosts_guard.plugin import STATES

    plugin = _plugin(tmp_path)
    snap = plugin.tick()
    assert isinstance(snap, StateReport)
    # 报告引用的取值必须**在声明里列举过**（D13 的存在理由之一：拼错的取值可被发现）
    assert snap.state in {opt.id for opt in STATES}, f"报告了没声明的取值 {snap.state!r}"


def test_on_config_changed_refreshes_panel_immediately(tmp_path, monkeypatch):
    """Fix 11 回归：配置热重载后**立即**刷新状态行，不等最长 300s 的下一轮 tick。

    改前 ``on_config_changed`` 只调 ``sync()``，没重取观测、也没 publish_snapshot ⇒
    面板那一行停在旧结论，用户手改 YAML / 恢复默认后最长 300s 不更新。本用例模拟
    「守护此刻推来了一份新观测」，断言配置热重载能立刻把它反映到 ``_state`` 并广播。
    """
    from barkit.plugins.hosts_guard.plugin import _observed_state

    p = _plugin(tmp_path)
    # 初始观测：尚未收到任何事实（空 dict）
    p._state = _observed_state(p)
    assert p._state == {}

    # 模拟守护此刻推来了一份观测（hosts 已配置好）
    monkeypatch.setattr(p, "observed", lambda subject, default=None: (
        {"configured": True} if subject == "hosts" else default))
    # sync 会碰长驻服务 / 网络，本用例不关心它的产物
    monkeypatch.setattr(p, "sync", lambda: (True, None))

    published = []
    monkeypatch.setattr(p, "publish_snapshot", lambda: published.append(True))

    p.on_config_changed()
    assert p._state == {"configured": True}, "配置变了必须立即重取观测"
    assert published, "配置变了必须立即广播快照"


def test_panel_row_via_contributions(tmp_path, qtbot):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])  # 保证 app 存在；本用例不读这个引用
    plugin = _plugin(tmp_path)
    specs = gather_specs(plugin, plugin.id)
    row_spec = next(s for s in specs if isinstance(s, StatusRowSpec))
    entries = [s for s in specs if isinstance(s, DetailEntrySpec)]
    # §5.3：状态行由框架经 StatusRowSpec 声明式渲染（取代 custom 工厂）
    panel = StatusPanel()
    qtbot.addWidget(panel)
    # 整行可点**不由行声明**：框架发现组件声明了 ``DetailEntrySpec``
    # 才合成「打开详情页」（契约 §2.4 / 决议 D28）。注入顺序按 ``set_contributions``
    # （先详情入口、再状态行），否则 ``_row_on_click`` 拿不到那条合成点击。
    panel.set_detail_entries(entries)
    panel.sync_status_rows([row_spec])
    row = panel._row_widgets["hosts_guard"][0]
    assert isinstance(row, _StatusRowView)
    assert row._clickable is True
    panel.deleteLater()


def test_install_action_reports_into_the_ops_zone_feedback_bar(tmp_path, monkeypatch, qtbot):
    """安装结果落进**页尾框架区**那条槽（装置运维卡所在的那一区）。

    零模态不变（见 ``ui_kit.FeedbackBar``），变的是**落哪一条槽**：2026-09-28 起装置
    运维卡由框架按**声明**注入到页尾框架区，触发按钮在哪一区，回执 / 确认问句就落哪一区
    （口径「一区一条」）。组件自己那条槽（``_feedback``）仍管本页其余动作。
    """
    from barkit.capabilities import helper_client as daemon
    from barkit.shell.ui_kit import FeedbackBar

    plugin = _plugin(tmp_path)
    page = plugin._build_settings_page()
    qtbot.addWidget(page)                      # qtbot 只存 weakref，靠本函数的 page 保住
    zone_fb = FeedbackBar(page)                # 模拟框架区那条槽（真实接线归框架区的用例）
    plugin.attach_helper_ops(page, zone_fb, lambda: None)

    monkeypatch.setattr(daemon, "install", lambda *a, **k: True)
    # 返回值只有一个消费方：装置运维卡那枚「安装守护」按钮（它据此自报「已安装」）。
    assert plugin.install_action() is True, "装成了必须能被调用方读到"
    assert "安装成功" in zone_fb.text()

    monkeypatch.setattr(daemon, "install", lambda *a, **k: False)
    assert plugin.install_action() is False, "没装成不许报成功"
    assert "安装失败" in zone_fb.text()
    plugin.stop()


# ──────────────────────────────────────────────
# 设置面（2026-09-19：设置页迁 settings.py，并补 resolve_poll 控件）
# ──────────────────────────────────────────────

def test_settings_page_has_resolve_poll_spinbox(tmp_path, qtbot):
    """原先只有一句「可在配置 plugins.hosts_guard.resolve_poll 调整」的 note
    —— 现在是可改的 QSpinBox（初值取自插件，不是页面自己读配置）。"""
    from PySide6.QtWidgets import QSpinBox

    from barkit.plugins.hosts_guard.models import MIN_RESOLVE_POLL

    p = _plugin(tmp_path)                      # resolve_poll: 120
    page = p._build_settings_page()
    qtbot.addWidget(page)
    spins = page.findChildren(QSpinBox)
    assert len(spins) == 1, "本页只该有一个数字型装置参数"
    assert spins[0].value() == 120
    assert spins[0].minimum() == MIN_RESOLVE_POLL


def test_helper_notice_variants(tmp_path, monkeypatch):
    """守护警示 ＋ 「要不要出重装键」由插件算（界面只画，不自己问 helper_client）。

    ⚠️ 2026-09-28 **形状变了**：本组件原来自立一套 ``daemon_state()``（页首常驻一行
    「状态：…」＋ 只装不卸、不出重装键）；统一到 ``capabilities.helper_ops`` 之后只剩
    这一口 —— **常态不再有一句**（「已安装 · 运行中」归状态机段与面板那一行），只剩
    「需要用户做点什么」的两种局面；空串 = 整块隐藏。
    """
    from barkit.capabilities import helper_client as daemon

    p = _plugin(tmp_path)

    monkeypatch.setattr(daemon, "status", lambda: {"installed": False})
    text, needs_reinstall = p.helper_notice()
    assert "未安装" in text
    assert needs_reinstall is False, "未装时该点的是「安装守护」，不给「重装守护」"

    monkeypatch.setattr(daemon, "status", lambda: {
        "installed": True, "outdated": True, "version": "2.14.0", "expected": "2.15.0"})
    text, needs_reinstall = p.helper_notice()
    assert "过期" in text and "2.15.0" in text
    assert needs_reinstall is True

    monkeypatch.setattr(daemon, "status", lambda: {
        "installed": True, "outdated": False, "version": "2.15.0", "running": True})
    assert p.helper_notice() == ("", False), "一切正常 ⇒ 不给警示（整块隐藏）"


def test_settings_page_no_longer_draws_the_ops_block(tmp_path, qtbot):
    """本页**自己不再画**装置运维（2026-09-28 起归框架区的第 ③ 张卡）。

    判据取「这一页里找不到那几枚按钮」—— 比「有没有某张卡」更直：搬走的实体就是它们。
    它原先长在**页首**（三个用 root helper 的组件里唯一没接上共用块的那个：按钮组成
    自搓、没有卸载键），现在与另两个组件一样落在**页尾框架区**，位置由框架定。
    """
    from PySide6.QtWidgets import QPushButton

    p = _plugin(tmp_path)
    page = p._build_settings_page()
    qtbot.addWidget(page)
    labels = {b.text() for b in page.findChildren(QPushButton)}
    assert "立即检查" not in labels
    assert not any("守护" in t for t in labels), labels


def test_save_resolve_poll_clamps_and_writes_only_that_key(tmp_path, monkeypatch):
    """夹到下限 ＋ 只写这一个键（``poll`` 是框架节拍覆盖，组件不许改写它）。

    跨平台版：不落盘，只看插件交给 Config 的那个 ``mutate`` 干了什么。
    """
    p = _plugin(tmp_path)
    section = {"resolve_poll": 120, "poll": 600}
    seen = {}

    def _fake(_self, plugin_id, mutate):
        seen["id"] = plugin_id
        mutate(section)

    monkeypatch.setattr(type(p.ctx.config), "update_plugin_section", _fake)
    monkeypatch.setattr(p, "sync", lambda: (True, None))          # 落盘产物不在本用例范围
    monkeypatch.setattr(p, "publish_snapshot", lambda: None)
    assert p.save_resolve_poll(5) is True
    assert seen["id"] == "hosts_guard"
    assert section["resolve_poll"] == 60          # 夹到下限（helper 越界会静默回默认值）
    assert section["poll"] == 600                 # 框架节拍原样不动


def test_save_resolve_poll_reports_rule_rejection(tmp_path, monkeypatch):
    """落盘与生效分别说话：存下了但规则被拒时要如实说、且返回 False。"""
    p = _plugin(tmp_path)
    page = p._build_settings_page()
    assert page is not None

    monkeypatch.setattr(type(p.ctx.config), "update_plugin_section",
                        lambda _self, _pid, mutate: mutate({}))
    monkeypatch.setattr(p, "sync", lambda: (False, "第 3 条 host 不合法"))
    monkeypatch.setattr(p, "publish_snapshot", lambda: None)
    assert p.save_resolve_poll(300) is False
    assert "规则未生效" in p._feedback.text()


# ──────────────────────────────────────────────────────────────────────
# 管理内容面：**零模态**行内交互（排期表 §S1-a，2026-09-20）
#
# 改前这里有三枚模态子对话框（新增 / 编辑 / 接管的 `EntryDialog`、`TakeOverDialog`、
# `ImportDialog`，各带 ``exec()``）＋ 四处 ``QMessageBox`` ＋ 一处 ``QInputDialog``。
# 本组用例守的是「**不再有窗**」这件事的**行为面**（AST 面由
# `test_architecture.py::test_plugin_settings_pages_have_no_modal_dialogs` 守）：
# 每个动作都换成 / 换回一屏，回执与确认走页脚那一格 `FeedbackBar`。
# ──────────────────────────────────────────────────────────────────────


def _manager(tmp_path, qtbot, monkeypatch, *, entries=(), records=()):
    """构造管理内容面，并把 ``/etc/hosts`` 的读法固定成 ``records``（不碰真实文件）。

    ``status_index`` 给空 dict —— 解析状态那一路由插件侧用例覆盖，这里只关心编辑交互。
    ``on_refresh`` 给一个**假回执**（受理全部、服务已叫起）：那枚「更新」键的**接线**
    由本文件末尾的端到端用例覆盖，这里要的是内容面与生产**同形**（有这枚键），
    否则「工具条一行摆下全部动作」那条会漏掉它。
    """
    # 打桩点落在**真身模块**：`read_records` 归「数据」那一枚混合类
    # （`manager_view` 拆包后入口只剩组合根，补丁打在入口上纹丝不动）。
    from barkit.plugins.hosts_guard import manager_data, manager_view

    if entries:
        store.save_entries(list(entries))
    monkeypatch.setattr(manager_data, "read_records", lambda: (list(records), None))
    content = manager_view.HostsManagerContent(
        status_index=lambda: {},
        on_refresh=lambda hosts: (len(hosts), 1),
    )
    qtbot.addWidget(content)          # 只存 weakref：调用方本函数的返回值必须被引用住
    return content


def test_manager_delete_asks_inline_before_removing(tmp_path, qtbot, monkeypatch):
    """删除**先问一句**，问在行内反馈槽里；确认之前一条都不动（零模态）。"""
    content = _manager(tmp_path, qtbot, monkeypatch,
                       entries=[make_entry("a.example.com", "1.2.3.4")])
    content._table.selectRow(0)
    content._ask_delete()
    assert content._fb.is_pending(), "应当长出一次行内确认（而不是弹框）"
    assert [e["host"] for e in store.load_entries()] == ["a.example.com"]   # 还没删
    content._fb.accept()
    assert store.load_entries() == []
    assert "已移除" in content._fb.text()


def test_manager_delete_removes_what_the_question_named(tmp_path, qtbot, monkeypatch):
    """确认时**不许重读选中项**：问句写着 a，就必须删 a。

    问句挂着的时候表格照样可点 —— 用户完全可以先把选中挪到 b、再回头按下「删除」。
    那时重读选中项删掉的就是 b（屏幕上写着的还是 a），而这是写 /etc/hosts 的不可逆
    动作。故被问的那一批跟着问句一起递下去。
    """
    content = _manager(tmp_path, qtbot, monkeypatch,
                       entries=[make_entry("a.example.com", "1.2.3.4"),
                                make_entry("b.example.com", "5.6.7.8")])
    content._table.selectRow(0)                      # 选中 a
    content._ask_delete()
    assert "a.example.com" in content._fb.text()
    content._table.selectRow(1)                      # 问句还挂着，选中挪到 b
    content._fb.accept()
    assert [e["host"] for e in store.load_entries()] == ["b.example.com"], (
        "删掉了此刻选中的那一条，而不是问句点名的那一条")


def test_manager_add_is_an_inline_page(tmp_path, qtbot, monkeypatch):
    """「添加」换一屏行内表单（不开窗）；保存后落盘、回表格页、回执落槽。"""
    content = _manager(tmp_path, qtbot, monkeypatch)
    content._begin_add()
    assert content._stack.current() is content._edit_form
    content._f_host.setText("new.example.com")
    content._f_ip.setText("5.6.7.8")
    # 点**真实那枚**主动作（`EditScreen` 的保存两端是注入的回调，只有走按钮才验证得到接线）
    content._edit_form.save_button.click()
    assert [e["host"] for e in store.load_entries()] == ["new.example.com"]
    assert content._stack.current() is content._table_page
    assert "已保存" in content._fb.text()


def test_manager_form_rejects_bad_input_without_writing(tmp_path, qtbot, monkeypatch):
    """非法输入**留在表单页**、只落一行红字，不写盘（原来是两处 QMessageBox.warning）。"""
    content = _manager(tmp_path, qtbot, monkeypatch)
    content._begin_add()
    content._f_host.setText("bad host")            # 含空格 ⇒ 注入防线必须挡住
    content._f_ip.setText("1.2.3.4")
    content._edit_form.save_button.click()
    assert store.load_entries() == []
    assert content._stack.current() is content._edit_form
    assert "域名不合法" in content._form_fb.text()

    content._f_host.setText("ok.example.com")
    content._f_ip.setText("not-an-ip")
    content._edit_form.save_button.click()
    assert store.load_entries() == []
    assert content._stack.current() is content._edit_form
    assert "IP 不合法" in content._form_fb.text()


def test_manager_edit_locks_host_as_primary_key(tmp_path, qtbot, monkeypatch):
    """编辑时域名只读（``store.upsert`` 按它定位），且字段被**回填**成现值。"""
    content = _manager(tmp_path, qtbot, monkeypatch,
                       entries=[make_entry("a.example.com", "1.2.3.4")])
    content._table.selectRow(0)
    content._begin_edit()
    assert content._f_host.text() == "a.example.com"
    assert content._f_ip.text() == "1.2.3.4"
    assert content._f_host.isReadOnly()
    content._f_ip.setText("9.9.9.9")
    content._edit_form.save_button.click()
    assert [e["ip"] for e in store.load_entries()] == ["9.9.9.9"]


def test_manager_take_over_uses_each_row_current_ip(tmp_path, qtbot, monkeypatch):
    """「接管」把选中的外部条目按**各自当前 IP** 纳入托管，且只让用户选分类。"""
    from barkit.plugins.hosts_guard.hostsfile import Record

    content = _manager(tmp_path, qtbot, monkeypatch,
                       records=[Record(ip="9.9.9.9", host="ext.example.com", line_no=3)])
    content._table.selectRow(0)
    content._begin_take()
    assert content._stack.current() is content._edit_form
    # 接管模式整组隐去「域名 / IP / …」（各自保留自己的 IP，摆出来等于暗示能逐条改）。
    # 判据用 isHidden()：父窗未 show 时 isVisible() 恒为假，那个断言等于没写。
    assert content._f_host_row.isHidden() and content._f_ip_row.isHidden()
    assert content._btn_apply.text() == "接管"
    content._edit_form.save_button.click()
    assert [(e["host"], e["ip"]) for e in store.load_entries()] == [("ext.example.com", "9.9.9.9")]


def test_manager_import_is_an_inline_page(tmp_path, qtbot, monkeypatch):
    """批量导入在行内页完成（原来是 ``ImportDialog``）：粘贴 → 导入 → 回执落槽。"""
    content = _manager(tmp_path, qtbot, monkeypatch)
    content._show_page(content._edit_import)
    content._i_text.setPlainText("# 只有注释，没有条目\n")
    content._edit_import.save_button.click()
    assert store.load_entries() == []
    assert "没有可导入的条目" in content._import_fb.text()

    content._i_text.setPlainText(
        "0.0.0.0 ads.example.com\n0.0.0.0 tracker.example.com\n# 注释\n")
    assert "识别到 2 条" in content._i_stat.text()
    content._edit_import.save_button.click()
    assert {e["host"] for e in store.load_entries()} == {
        "ads.example.com", "tracker.example.com"}
    assert content._stack.current() is content._table_page
    assert "导入完成" in content._fb.text()


def test_manager_new_category_uses_inline_field(tmp_path, qtbot, monkeypatch):
    """「新建分类」是行内一格输入（原来是 ``QInputDialog.getText``），三处下拉同步刷新。"""
    content = _manager(tmp_path, qtbot, monkeypatch)
    content._new_category()
    assert "不能为空" in content._import_fb.text()

    content._i_new_cat.setText("工作")
    content._new_category()
    assert "工作" in store.load_categories()
    assert content._i_cat.currentData() == "工作"
    assert content._f_cat.findData("工作") >= 0
    assert content._cat_filter.findData("工作") >= 0
    assert "已新建分类" in content._import_fb.text()


# ──────────────────────────────────────────────
# 入口唯一性（2026-09-20：设置页那枚「管理条目…」按钮已删）
# ──────────────────────────────────────────────

def test_manager_face_has_exactly_one_entry(tmp_path, qtbot):
    """管理面**只有一个入口**：面板整行 → 框架 ``DetailHost``（不存在第二份内容面）。

    回归守两件事 —— 都是这次删掉的形态，且都是**静默**的那种（界面照常显示，
    只是多出一扇互不感知的窗）：
      ① 设置页不得再长出「管理条目…」启动键：它会另开一份内容面，与面板整行那份
         **互不感知**（一份看不到另一份刚做的改动）；
      ② 插件不得再自持窗口对象（``_dialog`` / 自建打开方法 / 独立薄壳窗口类）——
         唯一入口归框架托管，窗口级事务（居中 / Dock 显隐 / 关闭）不该回到插件手里。

    设置页**不留**「管理在别处」这类备注：删掉就是删掉，界面不为已删除的入口留痕
    （2026-09-20 用户裁定）。
    """
    from PySide6.QtWidgets import QPushButton

    from barkit.plugins.hosts_guard import manager_view

    p = _plugin(tmp_path)
    page = p._build_settings_page()
    qtbot.addWidget(page)

    labels = [b.text() for b in page.findChildren(QPushButton)]
    assert not [t for t in labels if "管理条目" in t], labels
    assert not hasattr(p, "_dialog")
    assert not hasattr(p, "open_manager")
    assert not hasattr(manager_view, "HostsManagerDialog")
    # 唯一入口仍在：详情页由插件声明、框架据此合成整行点击
    assert p.detail_entry() is not None


# ────────────────────────────────────────────────────────────────────────
# 表格页工具条（2026-09-22）：过滤与动作**同一行**，两个过滤下拉**同宽**，
# 一行上的控件**同一高度**
# ────────────────────────────────────────────────────────────────────────

@pytest.fixture
def styled(qtbot):
    """带**生产样式表**（用后还原，别污染后面的用例）—— 与 test_panel 的同款固件同法。

    控件高度是 QSS 给的：没样式表时量到的「输入框多高」是另一件事（QLineEdit 的裸高），
    那条判据会静默分叉。
    """
    from PySide6.QtWidgets import QApplication

    from barkit.shell import styles

    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(styles.build_stylesheet("dark"))
    yield
    app.setStyleSheet("")

def test_toolbar_keeps_filters_and_actions_on_one_row(tmp_path, qtbot, monkeypatch):
    """过滤与动作**同一行**：它们是同一条带子上的东西，拆两行会空出半条（用户判「太丑」）。

    一行的前提是窗宽够 —— 详情入口声明 860（内容区 828），而本行自然宽 ~786。空间是
    **搜出来的**，不是把控件赶去第二行：见下面「搜索框不吃 stretch」那条。
    """
    content = _manager(tmp_path, qtbot, monkeypatch)
    page = content._table_page

    rows = [page.layout().itemAt(i).layout() for i in range(page.layout().count())]
    rows = [r for r in rows if r is not None]
    assert len(rows) >= 1
    bar = rows[0]

    def holds(widget) -> bool:
        return any(bar.itemAt(i).widget() is widget for i in range(bar.count()))

    for w in (content._search, content._src_filter, content._cat_filter,
              content._btn_refresh, content._btn_edit, content._btn_del):
        assert holds(w), "过滤与动作该在同一行"


def test_the_search_field_does_not_stretch(tmp_path, qtbot, monkeypatch):
    """搜索框**定宽**：让它吃 stretch，富余的空间就全归它 —— 它长成一条、并排的按钮被挤窄
    （用户 2026-09-22：「输入框还太长了」）。富余该让给下拉与动作键之间的那段 stretch。
    """
    content = _manager(tmp_path, qtbot, monkeypatch)
    assert content._search.minimumWidth() > 0, "不能靠自然宽（那由占位文案长度决定）"
    assert content._search.minimumWidth() == content._search.maximumWidth(), (
        "定宽才不参与拉伸：min == max 才是 fixed")


def test_the_two_filter_combos_share_one_width(tmp_path, qtbot, monkeypatch):
    """并排的两个过滤下拉**同宽**：宽窄不同会读成两种不同性质的控件。

    此前「全部来源」走自然宽（85）、「全部分类」被单独钉了 120 —— 同一个工具条里的
    同类控件两个尺码，正是「不协调」的直观看感来源。
    """
    content = _manager(tmp_path, qtbot, monkeypatch)
    assert content._src_filter.minimumWidth() == content._cat_filter.minimumWidth()
    assert content._src_filter.minimumWidth() > 0, "不能靠自然宽（两个下拉内容长度不同）"


def test_the_toolbar_row_shares_one_control_height(tmp_path, qtbot, monkeypatch, styled):
    """工具条一行上的输入框 / 下拉 / 按钮**同一高度**（``metrics.CONTROL_H``）。

    这是「下拉与其它元素不协调」的端到端判据：改前三者分别是 34 / 24 / 28。
    必须带**生产样式表**（``styled`` 固件）：输入框的高度是 QSS 给的，没样式表时量到
    的是 QLineEdit 的裸高度 —— 那是另一件事（见 ``test_styles`` 里那条控制组）。
    """
    from barkit.shell.design import metrics

    content = _manager(tmp_path, qtbot, monkeypatch)
    content.show()
    qtbot.wait(1)
    for name, w in (("输入框", content._search), ("来源下拉", content._src_filter),
                    ("分类下拉", content._cat_filter), ("按钮", content._btn_edit)):
        assert w.height() == metrics.CONTROL_H, f"{name} 高 {w.height()} ≠ {metrics.CONTROL_H}"


def test_manager_status_column_shows_real_resolution(tmp_path, qtbot, monkeypatch):
    """端到端：插件给的状态 / 生效 IP **真的画进了表里**（不是恒「待解析」）。

    2026-09-30 用户上报「状态列一直是待解析」的回归护栏。修的是**两处之间的接缝**——
    生产方（``plugin.status_index`` 与它读的 ``self._entries``）与消费方（表格那两格）
    各自都「正常」，中间那个字段名作废了 ⇒ 整列静默落在默认值上。

    故本用例**不测任何一处实现细节**，只把两边接起来跑一遍：构造真插件 → 让长驻服务
    真解析一轮 → 用 ``plugin.status_index`` 给内容面取数 → 读表格里那两格。
    """
    from barkit.plugins.hosts_guard import manager_data, manager_view, resolve
    from barkit.plugins.hosts_guard.plugin import Plugin as HG

    user = tmp_path / "hosts_guard.yaml"
    user.write_text("plugins:\n  hosts_guard:\n    enabled: true\n", encoding="utf-8")
    store.save_entries([make_entry("auto.example.com", "1.1.1.1", auto=True)])
    monkeypatch.setattr(resolve, "resolve_host",
                        lambda host, method: ("9.9.9.9", "udp-ali", None))
    monkeypatch.setattr(manager_data, "read_records", lambda: ([], None))

    plugin = HG(PluginContext(config=Config(user_path=user)))
    plugin._resolve_service.poll(force=True, resolve=True)
    content = manager_view.HostsManagerContent(status_index=plugin.status_index)
    qtbot.addWidget(content)          # 只存 weakref：下面的引用必须被引用住

    assert content._table.item(0, 1).text() == "auto.example.com"
    assert content._table.item(0, 2).text() == "9.9.9.9", "IP 列要显示解析后的生效值"
    assert content._table.item(0, 6).text() == "已同步"    # 改前恒为「待解析」


def test_manager_refresh_button_updates_the_row(tmp_path, qtbot, monkeypatch):
    """端到端：选中一条自动更新条目 → 按「更新」→ 那两格自己变成新 IP /「已同步」。

    这条接的是**三处之间的缝**：管理窗按下 → 插件登记 ＋ **把解析服务提前一拍** →
    服务真跑一轮 → 取数口给出新结论 → 那两格刷新。任何一处漏掉，用户看到的就是
    「点了没反应」—— 而那正是这枚键存在的唯一理由（不然等自动间隔就好了）。

    故这里用**真长驻宿主**（``ResidentHost``）托管真服务：「唤醒是不是真把那一拍提前」
    只有让宿主跑起来才验得到（假桩返回 1 就只能证明插件调过那个口）。
    """
    from barkit.core.resident import ResidentHost
    from barkit.plugins.hosts_guard import manager_data, manager_view, resolve
    from barkit.plugins.hosts_guard.plugin import Plugin as HG

    user = tmp_path / "hosts_guard.yaml"
    user.write_text("plugins:\n  hosts_guard:\n    enabled: true\n", encoding="utf-8")
    store.save_entries([make_entry("auto.example.com", "1.1.1.1", auto=True)])
    monkeypatch.setattr(resolve, "resolve_host",
                        lambda host, method: ("9.9.9.9", "udp-ali", None))
    monkeypatch.setattr(manager_data, "read_records", lambda: ([], None))

    plugin = HG(PluginContext(config=Config(user_path=user)))
    host = ResidentHost()
    plugin.ctx.wake_service = host.wake       # 框架在装配层注入的那一口
    host.start(plugin)
    try:
        content = manager_view.HostsManagerContent(
            status_index=plugin.status_index,
            on_refresh=plugin.refresh_entries,
        )
        qtbot.addWidget(content)              # 只存 weakref：下面的引用必须被引用住
        assert content._table.item(0, 1).text() == "auto.example.com"

        content._table.selectRow(0)
        assert content._btn_refresh.isEnabled(), "选中自动更新条目时这枚键该可用"
        content._btn_refresh.click()

        qtbot.waitUntil(lambda: content._table.item(0, 2).text() == "9.9.9.9", timeout=5000)
        assert content._table.item(0, 6).text() == "已同步"
        assert "更新完成" in content._fb.text()
    finally:
        host.stop(plugin)


def test_manager_refresh_button_needs_an_auto_entry(tmp_path, qtbot, monkeypatch):
    """静态条目**不给更新**：它的 IP 就是用户写下的那个，没有「重新解析」可言。"""
    content = _manager(tmp_path, qtbot, monkeypatch,
                       entries=[make_entry("static.example.com", "1.2.3.4")])
    assert not content._btn_refresh.isEnabled()
    content._table.selectRow(0)
    assert not content._btn_refresh.isEnabled()


def test_manager_refresh_reports_when_the_service_is_not_running(tmp_path, qtbot, monkeypatch):
    """服务没在跑时**不许说「正在更新」**：请求记下了，但它不会正在发生 ——
    说成正在进行就是让用户等一个不会有结果的进度（这里如实说「已登记」）。"""
    from barkit.plugins.hosts_guard import manager_data, manager_view

    store.save_entries([make_entry("auto.example.com", "1.1.1.1", auto=True)])
    monkeypatch.setattr(manager_data, "read_records", lambda: ([], None))
    calls: list = []
    content = manager_view.HostsManagerContent(
        status_index=lambda: {},
        on_refresh=lambda hosts: (calls.append(list(hosts)), (len(hosts), 0))[1],
    )
    qtbot.addWidget(content)
    content._table.selectRow(0)
    content._btn_refresh.click()

    assert calls == [["auto.example.com"]]
    assert "已登记" in content._fb.text()
    assert content._refresh_hosts == [], "没叫起服务就别进盯梢（否则会一直等到超时）"


def test_manager_shows_in_progress_while_the_update_is_in_flight(tmp_path, qtbot, monkeypatch):
    """「更新中…」要真的显示出来，而且**整表重载与局部重画走同一份判据**。

    用户按下键之后看到的第一个变化就是这一格（解析在后台线程上，结论几秒后才到）——
    少了它，界面在那几秒里对「按了到底有没有动静」一个字都不说。后半段守的是另一件：
    局部重画（``_paint_cells``）若自己另写一份状态判据，按下更新后会被**回退成旧结论**
    （看起来正好像「按了没用」），而整表重载那份又是对的。
    """
    from barkit.plugins.hosts_guard import manager_data, manager_view

    store.save_entries([make_entry("auto.example.com", "1.1.1.1", auto=True)])
    monkeypatch.setattr(manager_data, "read_records", lambda: ([], None))
    content = manager_view.HostsManagerContent(
        status_index=lambda: {"auto.example.com": {
            "ip": "1.1.1.1", "status": "ok", "pending": True}},
        on_refresh=lambda hosts: (len(hosts), 1),
    )
    qtbot.addWidget(content)
    assert content._table.item(0, 6).text() == "更新中…"     # 整表重载这一路

    content._paint_cells({"auto.example.com": {
        "ip": "9.9.9.9", "status": "ok", "pending": True}})   # 局部重画这一路
    assert content._table.item(0, 2).text() == "9.9.9.9"
    assert content._table.item(0, 6).text() == "更新中…"
