"""菜单栏主图标来源候选：**收集链路**（批次 13 · P0③ / 跟踪表 §C2 的 P2-a）。

为什么单独一个文件：`tests/test_settings.py` 里「通用」页那几条用例走的是轻量桩
（`_FakeContrib`），只证明「给了候选，渲染器画得出来」。**候选从哪来**那半 ——

    StateSpec.icon  →  ContributionRegistry.tray_base_candidates()
                    →  SettingsWindow._tray_base_choices()

此前**零覆盖**：链条任一处断掉（字段没被带出、组件名取错、label 拼法变了、
取值退回事件总线 topic、未声明主图标的状态机混进来），都只有真机目测才会发现。
本文件把这条链跑在**真实插件集**上（内建 6 个组件），据此把 P2-a 的「下拉不再
只有默认项」从「人眼确认」升级成可回归的断言。

留给人眼的那半只剩**视觉**：菜单栏那一格是否随第二屏连接态在实心 / 空心屏间切换
（P2-b / P2-c）。
"""

from __future__ import annotations

from barkit.core.config import Config
from barkit.core.contributions import ContributionRegistry
from barkit.core.events import EventBus
from barkit.core.registry import Registry
from barkit.shell.settings import SettingsWindow

_NEEDLE = ("录屏通知 · 第二屏", "notify_guard.display")


def _real_settings(qapp) -> SettingsWindow:
    """用**真实**插件集装配一个设置窗（数据路径由 conftest 重定向到 tmp）。"""
    config = Config()
    registry = Registry(config, EventBus())
    plugins = registry.load_all()
    assert not registry.rejected, f"发现期被拒：{registry.rejected}"

    contributions = ContributionRegistry(plugins, order_provider=registry.plugin_order)
    assert not contributions.rejected, f"注册期被拒：{contributions.rejected}"
    assert not contributions.failed, f"贡献收集中被隔离：{contributions.failed}"

    # 页面在 __init__ 里一次性构建；**不可**再次调用 _page_general()——那一份新建的
    # 卡片无人持引用，其 combo 立刻被 GC（踩坑 #9 同源）。
    return SettingsWindow(config=config, registry=contributions)


def _items(win: SettingsWindow) -> list[tuple[str, str]]:
    combo = win._tray_base_selector._combo  # noqa: SLF001 —— 直接读下拉，端到端
    return [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())]


def test_tray_base_choices_match_icon_declaring_states_exactly(qapp):
    """候选 ⇄ 「声明了主图标的状态机」**双向一致**：一个不多、一个不少。

    这是本条最硬的一句：正向单测只能证明「我知道的那台在」，说不了「不该在的没混进来」。
    双向比对把两侧都钉住 —— 多一个（漏了准入判据）与少一个（字段没带出 / 被过滤掉）
    都会当场变红。
    """
    win = _real_settings(qapp)
    contrib = win._registry  # noqa: SLF001 —— 与窗口同一个注册中心实例
    items = _items(win)

    assert items[0] == ("BarKit 默认", ""), f"首项必须是默认底图（空串取值）：{items[0]!r}"
    assert _NEEDLE in items, f"「录屏通知 · 第二屏」未进候选 —— 收集链路断了：{items!r}"

    expected = {s.state_id for s in contrib.states() if s.icon is not None}
    got = [value for _, value in items[1:]]
    assert sorted(got) == sorted(expected), (
        f"候选与「有主图标的状态机」不一致：\n  多出：{sorted(set(got) - expected)}\n"
        f"  少了：{sorted(expected - set(got))}"
    )
    # 非空也要防：两侧都空时上面那条会假绿
    assert expected, "真实插件集里一台声明了主图标的状态机都没有？收集链路断了"


def test_tray_base_choice_value_is_state_identity_not_bus_topic(qapp):
    """显示名 = ``组件名 · 状态名``；取值 = 状态机**身份**（不是事件总线 topic）。

    取值形态是本项「不耦合」的落点：配置指向「哪个组件的哪台机」，而不是总线上
    的地址 —— 于是组件改名 / 改 topic 拼法都不会让用户已选的来源失效，
    也不要求配置层认识 pub/sub 的命名规则。
    """
    items = _items(_real_settings(qapp))

    label, value = _NEEDLE
    assert (label, value) in items
    # 显示名两段分别来自 ComponentSpec.name 与 StateSpec.title —— 任一取错都会失败
    name, _, state_title = label.partition(" · ")
    assert name == "录屏通知" and state_title == "第二屏"

    # 取值是身份：`{plugin_id}.{key}`，**不含** `plugin.` 前缀、**不含** `.status.` 段
    assert "." in value and value.count(".") == 1, f"取值不是 `{{plugin_id}}.{{key}}` 形态：{value!r}"
    pid, _, key = value.partition(".")
    assert (pid, key) == ("notify_guard", "display")
    for _, v in items[1:]:
        assert not v.startswith("plugin."), f"取值退回了事件总线 topic：{v!r}"
        assert ".status." not in v, f"取值退回了事件总线 topic：{v!r}"


def test_tray_base_choices_refresh_after_plugin_deactivate(qapp):
    """停用组件后，「通用」页下拉即时**收缩**该组件的那几项（其余仍在）。

    关掉的是《重构-开工手册》§4.2「待实机确认」里挂着风险的那条：「通用」页今天不随插件变，
    **可能列表过期**。实际不会 —— ``rebuild_pages()``（插件启停经
    ``PluginManager.on_plugins_changed`` 触发）**把「通用」页一并重建**。
    故这条从「真机看会不会过期」降级为「已由本用例守住的代码事实」。

    为什么它值得守：下拉里留着一个已停用的来源，用户选中后会得到一个永远不生效的
    配置（落盘的身份在册子里查无此机）——而这正是 P2-c 回落规则要排掉的场景。

    断言刻意分成「该组件的都没了」＋「别的组件还在」两句：只断言后者会漏掉
    「整份清空」这种过度反应，只断言前者会漏掉「全表被重置成只剩默认项」。
    """
    win = _real_settings(qapp)
    contribution = win._registry  # noqa: SLF001 —— 与窗口同一个注册中心实例
    before = [value for _, value in _items(win)]
    assert _NEEDLE[1] in before, "前置不成立：候选里本就没有该状态"
    assert any(v.startswith("hosts_guard.") for v in before), (
        f"前置不成立：另一个组件没进候选，收缩就测不出方向：{before!r}"
    )

    contribution.deactivate("notify_guard")
    win.rebuild_pages()

    after = [value for _, value in _items(win)]
    assert _NEEDLE[1] not in after, f"停用后仍列出已失效的来源：{after!r}"
    assert all(not v.startswith("notify_guard.") for v in after), f"该组件仍有残留：{after!r}"
    assert any(v.startswith("hosts_guard.") for v in after), (
        f"别的组件的候选被一起清掉了（过度反应）：{after!r}"
    )
