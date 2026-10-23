"""E3 ① 崩溃隔离测试：单插件在贡献/实例化路径抛异常时，其余插件不受影响。

覆盖 ContributionRegistry 的各类贡献隔离、坏 order 兜底、失败只记一次、
以及 Registry.load_all 实例化隔离。

批次 7 后全部走 v3：插件以 ``contributions()`` 声明贡献，任一插件在其中抛异常
→ 该插件被整体隔离（记 failed），其余插件照常出现。
"""

from PySide6.QtWidgets import QLabel, QWidget

from barkit.contribution.specs import (
    ActionSpec,
    SettingsPageSpec,
    StateLevel,
    StateOption,
    StateSpec,
    StatusRowSpec,
)

from barkit.core.contributions import ContributionRegistry
from barkit.core.plugin import Plugin, PluginContext
from barkit.core.registry import Registry


class _Cfg:
    """极简配置桩：任意键返回默认值，供测试构造 PluginContext。"""

    def get(self, key, default=None):
        return default


def _ctx() -> PluginContext:
    return PluginContext(config=_Cfg(), events=None)


# ---------- 假插件 ----------

class Good(Plugin):
    id = "good"
    order = 0
    api_version = 3

    def row_specs(self):
        return (StatusRowSpec(key="main",
                              custom=lambda p: QLabel("ok", p), reason="隔离桩"),)

    def state_specs(self):
        return (
            StateSpec(key="main",
                      states=(StateOption("ok", StateLevel.OK, text="G"),), initial="ok",
                      stale_after=None),
        )

    def settings_page(self):
        return SettingsPageSpec(label="G", build=lambda: QWidget())

    def actions(self):
        return (ActionSpec(label="GA", on_click=lambda: None),)


class BrokenRows(Plugin):
    id = "broken_rows"
    order = 1

    def row_specs(self):
        raise RuntimeError("boom rows")


class BrokenStates(Plugin):
    id = "broken_states"

    def state_specs(self):
        raise RuntimeError("boom states")


class BrokenSettings(Plugin):
    id = "broken_settings"

    def settings_page(self):
        raise RuntimeError("boom settings")


class BrokenActions(Plugin):
    id = "broken_actions"

    def actions(self):
        raise RuntimeError("boom actions")


class BadOrder(Plugin):
    id = "bad_order"
    order = "not-an-int"  # 契约误用：非 int

    def row_specs(self):
        return (StatusRowSpec(key="main",
                              custom=lambda p: QLabel("bad", p), reason="隔离桩"),)


# ---------- ContributionRegistry 隔离 ----------

def test_panel_rows_isolates_broken_plugin(qtbot):
    reg = ContributionRegistry([Good(_ctx()), BrokenRows(_ctx())])
    rows = reg.panel_rows()
    assert len(rows) == 1 and isinstance(rows[0], QLabel)
    assert ("broken_rows",) in [(pid,) for pid, _ in reg.failed]


def test_states_isolates_broken_plugin(qtbot):
    reg = ContributionRegistry([Good(_ctx()), BrokenStates(_ctx())])
    states = reg.states()
    assert len(states) == 1 and states[0].plugin_id == "good"
    assert ("broken_states",) in [(pid,) for pid, _ in reg.failed]


def test_settings_pages_isolates_broken_plugin(qtbot):
    reg = ContributionRegistry([Good(_ctx()), BrokenSettings(_ctx())])
    pages = reg.settings_pages()
    assert len(pages) == 1 and pages[0].label == "G"
    assert ("broken_settings",) in [(pid,) for pid, _ in reg.failed]


def test_primary_actions_isolates_broken_plugin(qtbot):
    reg = ContributionRegistry([Good(_ctx()), BrokenActions(_ctx())])
    acts = reg.primary_actions()
    assert len(acts) == 1 and acts[0].label == "GA"
    assert ("broken_actions",) in [(pid,) for pid, _ in reg.failed]


def test_failed_plugin_logged_once_and_skipped_afterward(qtbot):
    reg = ContributionRegistry([Good(_ctx()), BrokenRows(_ctx())])
    r1 = reg.panel_rows()
    r2 = reg.panel_rows()
    # 两次均只含 Good（坏插件被隔离），且失败只记录一次（不刷日志风暴）
    assert len(r1) == 1 and len(r2) == 1
    assert len(reg.failed) == 1
    # 贡献收集失败后，BrokenRows 的状态也应被整体跳过
    states = reg.states()
    assert all(c.plugin_id != "broken_rows" for c in states)


def test_sorted_plugins_robust_to_bad_order(qtbot):
    reg = ContributionRegistry([BadOrder(_ctx()), Good(_ctx())])
    rows = reg.panel_rows()
    # 坏 order 兜底为 0，不抛 TypeError，两个插件都正常出现
    assert len(rows) == 2


# ---------- Registry.load_all 实例化隔离 ----------

def test_load_all_skips_plugin_that_raises_on_init(monkeypatch):
    reg = Registry(_Cfg(), None)

    class Boom(Plugin):
        def __init__(self, ctx):
            raise RuntimeError("init boom")

    monkeypatch.setattr(reg, "discover", lambda: {"boom": Boom})
    plugins = reg.load_all()
    assert plugins == []
    assert "boom" in reg.failed


def test_load_all_keeps_good_plugins_when_one_init_fails(monkeypatch):
    reg = Registry(_Cfg(), None)

    class Boom(Plugin):
        def __init__(self, ctx):
            raise RuntimeError("init boom")

    monkeypatch.setattr(reg, "discover", lambda: {"boom": Boom, "good": Good})
    plugins = reg.load_all()
    assert [p.id for p in plugins] == ["good"]
    assert "boom" in reg.failed
