"""批次 1 验收快照：autotype / recognize 迁 v3 贡献契约。

白盒验证（无 UI 依赖部分不构造 widget，避免触碰 QApplication）：
- api_version == 3，且声明点方法收集出的 Spec 结构与 identity 稳定；
- 同一插件内 key 不重复（注册中心拼接 identity 的唯一性前提）；
- ContributionRegistry 能无崩溃收集两者（v3 直调路径，绕过 legacy 适配层）；
- 状态行 custom 工厂能产出 QWidget（端到端，由 qtbot 提供 QApplication）。
"""

from __future__ import annotations

from barkit.contribution.specs import (
    ActionSpec,
    ChoiceSpec,
    ComponentSpec,
    DETAIL_PRESENTATION_WINDOW,
    DetailEntrySpec,
    HotkeySpec,
    IconName,
    SettingsPageSpec,
    StatusRowSpec,
)
from barkit.core.contributions import ContributionRegistry, gather_specs
from barkit.core.plugin import PluginContext


def _ctx() -> PluginContext:
    config = type("C", (), {"get": lambda self, k, d=None: d})()
    return PluginContext(config=config, events=None)


def _autotype() -> "object":
    from barkit.plugins.autotype.plugin import Plugin

    return Plugin(_ctx())


def _recognize() -> "object":
    from barkit.plugins.recognize.plugin import Plugin

    return Plugin(_ctx())


def test_autotype_contract_structure():
    p = _autotype()
    assert p.api_version == 3
    specs = gather_specs(p, p.id)
    kinds = [type(s) for s in specs]
    assert kinds.count(StatusRowSpec) == 1
    assert kinds.count(SettingsPageSpec) == 1
    assert kinds.count(ActionSpec) == 1
    assert kinds.count(HotkeySpec) == 1

    row = next(s for s in specs if isinstance(s, StatusRowSpec))
    assert row.plugin_id == "autotype" and row.key == "main"
    # 声明式（2026-09-20 收归：不再用 ``custom`` 逃生舱自建 —— 行内下拉由第四种行内
    # 控件 ``ChoiceSpec`` 表达）。行的尺寸 / 对齐因此由框架单点决定。
    assert row.custom is None
    assert len(row.controls) == 1 and isinstance(row.controls[0], ChoiceSpec)
    assert callable(row.on_click)

    # 身份：有（供行主位图标），但**不上标题栏**（无状态机 ⇒ 那一格只会恒灰）
    comp = next(s for s in specs if isinstance(s, ComponentSpec))
    assert comp.icon is IconName.KEYBOARD
    assert comp.in_header is False

    page = next(s for s in specs if isinstance(s, SettingsPageSpec))
    assert page.plugin_id == "autotype" and page.key == "text-input"
    assert callable(page.build)

    act = next(s for s in specs if isinstance(s, ActionSpec))
    assert act.key == "type-last" and act.on_click is not None

    hk = next(s for s in specs if isinstance(s, HotkeySpec))
    assert hk.name == "autotype_last" and hk.default_combo == "ctrl+alt+p"


def test_recognize_contract_structure():
    p = _recognize()
    assert p.api_version == 3
    specs = gather_specs(p, p.id)
    kinds = [type(s) for s in specs]
    assert kinds.count(StatusRowSpec) == 0   # 识别不贡献状态行
    # 底部「快捷区域」入口统一走 DetailHost（与行点击同一条触发链），不再单独
    # 声明 ActionSpec（2026-09-20 迁移）
    assert kinds.count(DetailEntrySpec) == 1
    assert kinds.count(HotkeySpec) == 1

    entry = next(s for s in specs if isinstance(s, DetailEntrySpec))
    # `trigger` 字段已删（2026-09-22）：行与快捷区域是**同一个过程**的两个入口，
    # 谁可见由用户层的显示位开关管，不再由作者二选一。
    assert not hasattr(entry, "trigger")
    assert entry.presentation == DETAIL_PRESENTATION_WINDOW
    assert entry.icon is IconName.SCAN
    assert callable(entry.build)
    hk = next(s for s in specs if isinstance(s, HotkeySpec))
    assert hk.name == "recognize" and hk.default_combo == "ctrl+alt+o"


def test_identity_stable_and_unique_per_point():
    for p in (_autotype(), _recognize()):
        specs = gather_specs(p, p.id)
        for s in specs:
            ident = s.identity()
            # identity 形式 plugin_id.key（key 空时取 default）
            assert ident == f"{s.plugin_id}.{s.key or 'default'}"
        # 同 point 内 key 不重复（注册中心按 identity 复用 widget 的前提）
        for point in {s.point for s in specs}:
            point_keys = [s.key or "default" for s in specs if s.point == point]
            assert len(point_keys) == len(set(point_keys))


def test_registry_collects_v2_plugins():
    reg = ContributionRegistry([_autotype(), _recognize()])
    assert reg.rejected == []
    assert reg.failed == [], [str(e) for _id, e in reg.failed]
    # 翻译门面（不构造 widget，纯结构校验）
    assert len(reg.hotkeys()) == 2
    # autotype 与 recognize 各贡献一页设置面（recognize 的设置页 2026-09-19 已补）
    assert len(reg.settings_pages()) == 2
    # 仅 autotype 声明 ActionSpec；recognize 的快捷按钮入口改走 DetailEntrySpec
    # （声明入口即同时接管两处），由面板合成、不经 primary_actions 收集
    assert len(reg.primary_actions()) == 1
    assert {s.plugin_id for s in reg.hotkeys()} == {"autotype", "recognize"}


def test_autotype_row_renders_from_the_framework(qtbot):
    """autotype 那一行由**框架**渲染出下拉（不再是 ``custom`` 工厂自建的 widget）。

    原先这里验的是「``row.custom(None)`` 能产出 QWidget」—— 那正是把行的尺寸常量写成
    第二份的入口（30 盒 / 16px / 不设对齐，而框架行是 36 盒 / 22px / 居中）。行收归
    声明式后，验的是同一件事的另一面：框架能从 ``ChoiceSpec`` 渲出一枚行内下拉。
    """
    from PySide6.QtWidgets import QApplication

    from barkit.shell.panel import StatusPanel, _StatusRowView

    QApplication.instance() or QApplication([])
    p = _autotype()
    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel._component_icons[p.id] = IconName.KEYBOARD
    # 走**收集后**的 spec（``gather_specs`` 补上 plugin_id / 行名），与生产同路径；
    # 直接喂 ``p.row_specs()`` 的话 plugin_id 还是空的，行的复用键会塌成 ""。
    panel.sync_status_rows([s for s in gather_specs(p, p.id)
                            if isinstance(s, StatusRowSpec)])
    row = panel._row_widgets[p.id][0]
    assert isinstance(row, _StatusRowView)
    assert len(row._choices) == 1, "声明式 ChoiceSpec → 框架渲染一枚行内下拉"
    panel.deleteLater()
