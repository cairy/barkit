"""ContributionRegistry + 贡献契约测试（声明点方法，决议 D20）。

验证：注册中心能按声明点收集 / 排序各类贡献；版本闸门拒绝过旧与过新契约；
且一个只贡献状态源 + 组件图标 + 设置页的「模板插件」可在不改动 app/panel/settings 的前提下
被框架消费（即 P0 的验收判据）。
"""

from __future__ import annotations

import importlib

from barkit.contribution.specs import (
    ActionSpec,
    IconName,
    SettingsPageSpec,
    StateLevel,
    StateOption,
    StateSpec,
    StatusRowSpec,
)
from barkit.core.contributions import (
    ContributionRegistry,
    SUPPORTED_API_VERSION,
)
from barkit.core.plugin import PluginContext


def all_specs(plugin) -> list:
    """把一个插件实例的全部声明点产出拼成一份 Spec 列表（测试侧便利封装）。

    直接委托框架的 ``gather_specs``（同一份对应表与校验），测试里需要
    「全部 Spec」的场景用它，不再依赖已删除的单入口 ``contributions()``。
    """
    from barkit.core.contributions import gather_specs

    return gather_specs(plugin, getattr(plugin, "id", "") or type(plugin).__name__)


class _FakePlugin:
    """最小贡献桩：验证注册中心收集与排序（无需真实 Plugin 子类）。"""

    id = "fake"
    order = 0
    api_version = 3

    def row_specs(self):
        return (StatusRowSpec(key="main", custom=lambda p: "row", reason="收集桩"),)

    def state_specs(self):
        return (
            StateSpec(
                key="main",
                states=(StateOption("ok", StateLevel.OK, text="F"),), initial="ok",
                stale_after=None,
            ),
        )

    def settings_page(self):
        return SettingsPageSpec(label="Fake", build=lambda: None)

    def actions(self):
        return (ActionSpec(label="Do", on_click=lambda: None),)


def test_registry_collects_contributions():
    reg = ContributionRegistry([_FakePlugin()])
    assert len(reg.states()) == 1
    assert len(reg.settings_pages()) == 1
    assert len(reg.primary_actions()) == 1


def test_registry_panel_rows_sorted_by_order():
    """状态行按 plugin 的 order 升序（不随声明顺序）。"""

    class A:
        id = "a"
        order = 30
        api_version = 3

        def row_specs(self):
            return (StatusRowSpec(key="main",
                                  custom=lambda p: "rowA", reason="排序桩"),)

    class B:
        id = "b"
        order = 10
        api_version = 3

        def row_specs(self):
            return (StatusRowSpec(key="main",
                                  custom=lambda p: "rowB", reason="排序桩"),)

    reg = ContributionRegistry([A(), B()])
    assert reg.panel_rows() == ["rowB", "rowA"]


def test_registry_skips_underscore_packages():
    # registry.discover 应跳过以 _ 开头的子包（模板 / 脚手架）
    from barkit.core.registry import Registry
    from barkit.core.config import Config

    reg = Registry(Config())
    found = reg.discover()
    assert "template" not in found  # _template 被跳过


def test_discover_rejects_incompatible_api_version(tmp_path, monkeypatch):
    # 在临时目录造两个插件包：good(api_version=SUPPORTED) 与 bad(过新)，
    # 把 barkit.plugins.__path__ 指向它，真实驱动 discover() 的版本闸门。
    # 注意：框架只认 SUPPORTED_API_VERSION 一个版本，故「不兼容」须用 >SUPPORTED 的版本。
    import barkit.plugins as plugins_pkg

    for name, ver in (("good", SUPPORTED_API_VERSION), ("bad", SUPPORTED_API_VERSION + 2)):
        pkg = tmp_path / name
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "plugin.py").write_text(
            "from barkit.core.plugin import Plugin\n"
            "class Plugin(Plugin):\n"
            f"    id = {name!r}\n"
            f"    api_version = {ver}\n"
        )
    monkeypatch.setattr(plugins_pkg, "__path__", [str(tmp_path)])

    from barkit.core.registry import Registry
    from barkit.core.config import Config

    reg = Registry(Config())
    found = reg.discover()
    assert "good" in found
    assert "bad" not in found  # 导入期即拒，不进入 found（不会被实例化）
    assert any(name == "bad" for name, _ in reg.rejected)
    _, reason = next((n, r) for n, r in reg.rejected if n == "bad")
    assert "升级 Barkit" in reason


def test_template_plugin_contributes_without_framework_change():
    # 模板插件（_template）声明式贡献，框架零改即可被消费
    from barkit.plugins._template.plugin import Plugin as Tpl

    t = Tpl(PluginContext(config=None, events=None))
    assert t.api_version == 3, "模板插件应为现行契约版本示例"
    specs = all_specs(t)
    types = {type(s).__name__ for s in specs}
    assert "StatusRowSpec" in types, "模板插件应贡献状态行"
    assert "StateSpec" in types, "模板插件应贡献状态源"
    assert "SettingsPageSpec" in types, "模板插件应贡献设置页"
    assert "ActionSpec" in types, "模板插件应贡献操作按钮"
    assert "HotkeySpec" in types, "模板插件应贡献热键"


# ---------- api_version 版本闸门 ----------


class _ApiPlugin:
    """最小桩：仅声明 api_version 与一行贡献，用于验证版本闸门。"""

    id = "apip"
    order = 0

    def __init__(self, version: int) -> None:
        self.api_version = version

    def row_specs(self):
        return (StatusRowSpec(key="main",
                              custom=lambda p: f"row-{self.api_version}", reason="闸门桩"),)


def test_api_version_accepts_compatible():
    reg = ContributionRegistry([_ApiPlugin(SUPPORTED_API_VERSION)])
    assert reg.rejected == []
    assert len(reg._plugins) == 1


def test_api_version_rejects_newer():
    # 仅 v3 受支持；>SUPPORTED_API_VERSION 才视为需要更新的 Barkit。
    reg = ContributionRegistry([_ApiPlugin(SUPPORTED_API_VERSION + 2)])
    assert len(reg._plugins) == 0
    assert len(reg.rejected) == 1
    plug, reason = reg.rejected[0]
    assert plug.id == "apip"
    assert "升级 Barkit" in reason


def test_api_version_rejects_older_no_shim():
    # v1 及更旧：适配层已删，无 shim 可回落
    reg = ContributionRegistry([_ApiPlugin(SUPPORTED_API_VERSION - 1)])
    assert len(reg._plugins) == 0
    assert len(reg.rejected) == 1
    _, reason = reg.rejected[0]
    assert "暂无适配层" in reason


def test_rejected_plugin_not_consumed():
    # 被拒绝的插件不应出现在任何贡献集合里（早 fail，不带入后续逻辑）

    class Bad:
        id = "bad"
        order = 0
        api_version = 999

        def row_specs(self):
            return (StatusRowSpec(key="r",
                                  custom=lambda p: "should-not-appear", reason="闸门桩"),)

    class Good:
        id = "good"
        order = 0
        api_version = SUPPORTED_API_VERSION

        def row_specs(self):
            return (StatusRowSpec(key="r",
                                  custom=lambda p: "good-row", reason="闸门桩"),)

        def state_specs(self):
            return (
                StateSpec(key="main",
                          states=(StateOption("ok", StateLevel.OK, text="G"),), initial="ok",
                          stale_after=None),
            )

    reg = ContributionRegistry([Bad(), Good()])
    assert reg.panel_rows() == ["good-row"]
    assert all(c.plugin_id != "bad" for c in reg.states())


# ---------- 真实插件 identity 全局唯一（v3 路径） ----------

REAL_PLUGINS = {
    "autotype": "barkit.plugins.autotype.plugin",
    "hosts_guard": "barkit.plugins.hosts_guard.plugin",
    "ipad_tunnel": "barkit.plugins.ipad_tunnel.plugin",
    "notify_guard": "barkit.plugins.notify_guard.plugin",
    "recognize": "barkit.plugins.recognize.plugin",
    "route_guard": "barkit.plugins.route_guard.plugin",
}


def test_real_plugins_identity_globally_unique(tmp_path, monkeypatch):
    """全部真实插件的 v3 贡献合到一张注册表：identity 不得跨插件冲突。

    identity = f"{plugin_id}.{key or 'default'}"，由 plugin_id 前缀保证全局唯一；
    PointRegistry 在重复时抛 ContributionConflictError —— 能通过即证明无冲突。
    """
    from barkit import paths
    from barkit.contribution import PointRegistry
    from barkit.core.config import Config

    monkeypatch.setattr(paths, "TUNNEL_FILE", tmp_path / "tunnel.json", raising=False)
    monkeypatch.setattr(paths, "TRIGGER_FILE", tmp_path / ".trigger", raising=False)

    reg = PointRegistry()
    contributed = 0
    for pid, modname in sorted(REAL_PLUGINS.items()):
        mod = importlib.import_module(modname)
        cfg = Config(user_path=tmp_path / f"{pid}.yaml")
        plugin = mod.Plugin(PluginContext(config=cfg))
        assert plugin.api_version == 3, f"{pid} 应声明现行契约版本"
        for spec in all_specs(plugin):
            reg.contribute(spec)  # identity 重复即 ContributionConflictError
            contributed += 1
    assert contributed > 0, "真实插件的声明贡献为空"
    assert reg.snapshot()


# ---------- 多条贡献各有其名（决议 D23：行数与状态机数解绑） ----------


def _rows_plugin(rows):
    """造一个只声明状态行的鸭子插件；rows 由调用方给，便于构造违规样本。"""

    class P:
        id = "plug"
        order = 0
        api_version = SUPPORTED_API_VERSION

        def row_specs(self):
            return tuple(rows)

    return P()


def test_multiple_contributions_must_each_have_a_name():
    """同一声明点下声明多条却不写 key → 加载期拒绝。

    都不写 key 就都落到 ``plug.default``：面板的行复用缓存与状态机的 topic 表都按
    identity 索引 —— 撞名意味着界面上只剩最后一条，而且**不报错**。所以这条判据是
    「声明几行就是几行」能不能成立的前提，不能只靠文档约定。
    """
    reg = ContributionRegistry([_rows_plugin([StatusRowSpec(), StatusRowSpec()])])

    assert reg.status_rows() == [], "违规插件被隔离，不产出贡献"
    assert len(reg.failed) == 1
    assert "key" in str(reg.failed[0][1])


def test_multiple_contributions_must_have_unique_keys():
    """两条贡献撞同一个 key → 加载期拒绝（一个 identity 只对应一条贡献）。"""
    reg = ContributionRegistry(
        [_rows_plugin([StatusRowSpec(key="main"), StatusRowSpec(key="main")])]
    )

    assert reg.status_rows() == []
    assert "重复" in str(reg.failed[0][1])


def test_multiple_state_machines_must_have_unique_keys():
    """判据是**通用**的，不是只守状态行：多台状态机撞 key 同样拒绝。

    状态机撞 key 会让 topic / ``state_id`` 撞车 —— 「托盘听哪台、配置指向哪台」
    都没有唯一答案。
    """

    class P:
        id = "plug"
        order = 0
        api_version = SUPPORTED_API_VERSION

        def state_specs(self):
            return (StateSpec(key="main"), StateSpec(key="main"))

    reg = ContributionRegistry([P()])
    assert reg.states() == []
    assert "重复" in str(reg.failed[0][1])


def test_multiple_rows_must_explain_themselves():
    """声明多行必须逐行给 ``reason``（契约 §2.4 的门槛：面板不是控制台）。"""
    reg = ContributionRegistry(
        [_rows_plugin([StatusRowSpec(key="a"), StatusRowSpec(key="b")])]
    )

    assert reg.status_rows() == []
    assert "reason" in str(reg.failed[0][1])


def test_deactivate_clears_the_collection_failure_mark():
    """收集期失败过一次的插件，停用再启用后必须有一次重新收集的机会。

    ``_collect`` 的早返回（``pid in self._failed``）在 ``try`` 之外，故第二次
    连异常带日志都到不了 —— 症状是「用户启用成功了，但插件的行 / 页 / 按钮一个
    都没回来，且无声」。
    """

    class P:
        id = "plug"
        order = 0
        api_version = SUPPORTED_API_VERSION
        broken = True

        def row_specs(self):
            if self.broken:
                raise RuntimeError("依赖还没就绪")
            return (StatusRowSpec(key="main", reason="恢复后再给一次机会"),)

    p = P()
    reg = ContributionRegistry([p])
    assert reg.status_rows() == []          # 收集期失败 ⇒ 隔离
    assert len(reg.failed) == 1

    p.broken = False                        # 故障消失
    reg.deactivate("plug")
    reg.activate(p)
    assert len(reg.status_rows()) == 1


def test_single_contribution_may_omit_key_and_reason():
    """阴性对照：单条贡献照旧可以省略 key 与 reason —— 新门槛只对「多条」生效。

    存量 6 个内建组件全是「一个声明点一条贡献」，这条保证新判据不会误伤它们。
    """
    reg = ContributionRegistry([_rows_plugin([StatusRowSpec()])])

    assert reg.failed == []
    assert len(reg.status_rows()) == 1


def test_multiple_rows_with_keys_and_reasons_are_accepted():
    """阳性对照：两条都写 key、都写 reason → 正常收下两行（判据没变成恒真）。"""
    reg = ContributionRegistry([_rows_plugin([
        StatusRowSpec(key="a", reason="两台机各自有开关，面板要同时看到"),
        StatusRowSpec(key="b", reason="两台机各自有开关，面板要同时看到"),
    ])])

    assert reg.failed == []
    assert [s.key for s in reg.status_rows()] == ["a", "b"]


def test_real_plugins_pass_the_multi_contribution_rules(tmp_path, monkeypatch):
    """真实插件必须在**生产路径**上过得了新门槛（不是 ``gather_specs`` 那条旁路）。

    门槛挂在 ``ContributionRegistry._collect`` 上，而多数插件测试直接调 ``gather_specs``
    —— 那条路**不经校验**。故这里用真实插件组一张注册表，先断言零隔离（不误伤存量），
    再往一个真实插件身上植入一条违规（多出来的一行不写 key），断言它被抓住。
    两半都做，才说明门槛在「真实数据 ＋ 生产路径」上成立，而不只是对测试桩有效。
    """
    from barkit import paths
    from barkit.core.config import Config

    monkeypatch.setattr(paths, "TUNNEL_FILE", tmp_path / "tunnel.json", raising=False)
    monkeypatch.setattr(paths, "TRIGGER_FILE", tmp_path / ".trigger", raising=False)

    plugins = []
    for pid, modname in sorted(REAL_PLUGINS.items()):
        mod = importlib.import_module(modname)
        plugins.append(mod.Plugin(PluginContext(config=Config(user_path=tmp_path / f"{pid}.yaml"))))

    reg = ContributionRegistry(plugins)
    assert reg.failed == [], f"真实插件被新门槛误伤：{reg.failed}"
    assert reg.status_rows(), "真实插件里应当有组件声明了状态行"

    victim = next(p for p in plugins if any(isinstance(s, StatusRowSpec) for s in all_specs(p)))
    original = victim.row_specs
    victim.row_specs = lambda: original() + (StatusRowSpec(),)   # 多出来的一行不写 key
    try:
        broken = ContributionRegistry([victim])
        assert broken.status_rows() == [], "违规插件应当被隔离"
        assert broken.failed and broken.failed[0][0] == victim.id
        assert "key" in str(broken.failed[0][1])
    finally:
        victim.row_specs = original


# ---------- 菜单栏主图标来源：注册中心的两个门面（P0③） ----------


def _two_state_plugin():
    """两个插件共三台状态机：两台声明了主图标（⇒ 有资格），一台没声明（⇒ 没资格）。"""

    class P:
        id = "p"
        order = 0
        api_version = SUPPORTED_API_VERSION

        def state_specs(self):
            return (
                StateSpec(key="display", icon=IconName.DISPLAY_OFF),
                StateSpec(key="notify"),           # 未声明主图标
            )

    class Q:
        id = "q"
        order = 1
        api_version = SUPPORTED_API_VERSION

        def state_specs(self):
            return (StateSpec(key="main", icon=IconName.DNS),)

    return ContributionRegistry([P(), Q()])


def test_tray_base_candidates_requires_main_icon():
    """准入判据只有一条：**声明了主图标**。没声明的永不进候选。

    这条把「准入」钉在注册中心这一层（唯一落点）：设置窗与装配器都向它要答案，
    两处不各写一份 —— 否则会出现「下拉里选得到、菜单栏却认不出」的错位。
    """
    reg = _two_state_plugin()

    ids = [s.state_id for s in reg.tray_base_candidates()]
    assert ids == ["p.display", "q.main"], f"候选集不对：{ids!r}"
    assert all(s.icon is not None for s in reg.tray_base_candidates()), "候选里混进了无主图标的机"


def test_state_by_id_returns_none_for_unknown_or_empty():
    """按身份取回声明：**不在册 / 空串一律 ``None``**（调用方据此回落默认底图）。

    「不在册」= 该组件已禁用 / 卸载 —— 这是回落规则里唯一允许掉到 BarKit 默认底图
    的那条，故必须精确：既不能把别的机认成它，也不能对空串报错（出厂默认就是空串）。
    """
    reg = _two_state_plugin()

    assert reg.state_by_id("p.display").key == "display"
    assert reg.state_by_id("q.main").icon is IconName.DNS
    # 无主图标的那台**也**要能取回：准入与取回是两件事（决策点自己再判 icon）
    assert reg.state_by_id("p.notify") is not None
    assert reg.state_by_id("p.gone") is None          # 组件已卸下
    assert reg.state_by_id("") is None                # 出厂默认（未配置来源）
    assert reg.state_by_id("notify_guard.display") is None


def test_state_by_id_round_trips_every_candidate():
    """候选里的每一项都必须能被 ``state_by_id`` 原样取回。

    两个门面服务同一条链路（设置窗写身份 ↔ 装配器读身份），取值形态一旦漂移
    （比如一边存身份、一边存 topic），用户选完就等于没选。这条把两端焊在一起。
    """
    reg = _two_state_plugin()

    for spec in reg.tray_base_candidates():
        assert reg.state_by_id(spec.state_id) is spec
