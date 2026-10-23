"""示例外部插件（F2 模板，演示完整 BarKit v3 插件契约）。

安装：把本目录（连同 manifest.yaml）复制到外部插件根目录
（默认 ~/Library/Application Support/BarKit/plugins/，或用 config.plugins.external_path
覆盖），重启 BarKit 即被加载——**无需改动任何框架文件**。

本插件示范：
- 继承 ``capabilities.automation.AutomationCapability`` 获得「自动化开关」能力（状态行徽标 + 点击切换），
  证明横切能力 mixin 对外部插件同样可用（架构可扩展的直接证据）。
- 声明点方法（决议 D20）：``row_specs()`` / ``component()`` / ``state_specs()`` /
  ``settings_page()`` / ``actions()`` / ``hotkeys()`` 返回 StatusRowSpec / ComponentSpec /
  StateSpec / SettingsPageSpec / ActionSpec / HotkeySpec，全部经 ContributionRegistry 统一消费，
  框架层不持有任何具体插件类型；``plugin_id`` 由框架注入，Spec 里一个字都不写。
- manifest.yaml 声明身份与契约版本，Loader 在导入前即做 api_version 门禁（**精确匹配**现行
  契约版本，不兼容被拒、给可读原因，不崩溃）。

现行为 Spec 贡献契约：v1 的 ``panel_widget`` / ``panel_rows`` / ``header_chips`` /
``settings_pages`` / ``primary_actions`` / ``hotkeys`` 基类方法已随 legacy 适配层删除。
"""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from barkit.capabilities.automation import AutomationCapability
from barkit.contribution.specs import (
    ActionSpec,
    ComponentSpec,
    HotkeySpec,
    IconName,
    SettingsPageSpec,
    StateLevel,
    StateOption,
    StateReport,
    StateSpec,
    StatusRowSpec,
)
from barkit.core.plugin import RUN_ON_DEMAND
from barkit.shell.ui_kit import note

#: 取值闭集（决议 D13）：取值名 / 级别 / 默认文案只写一次，报告只报取值名。
STATES: tuple[StateOption, ...] = (
    StateOption("unknown", StateLevel.IDLE, text="尚未读取"),
    StateOption("ready", StateLevel.OK, text="示例就绪"),
)


class Plugin(AutomationCapability):
    id = "demo_ext"
    name = "示例外部插件"
    run_mode = RUN_ON_DEMAND
    api_version = 3          # 契约版本；须与 manifest.yaml 及框架 SUPPORTED_API_VERSION 精确一致
    order = 95

    # ---------- AutomationCapability 能力：自动化运行态（抽象钩子由子类实现） ----------
    # 仅示例：内存态开关。真实插件应在 set_automation_active 里持久化到
    # config.plugins.<id>.auto（零授权），并在 automation_active 里回读。
    _auto = True

    @property
    def automation_active(self) -> bool:
        """自动化是否运行中。True=自动，False=已暂停/手动接管。"""
        return self._auto

    def set_automation_active(self, active: bool) -> None:
        """写自动化开关（AutomationCapability 统一 seam；真实插件在此施加领域副作用）。

        模式徽标由框架在状态刷新时经 ModeBadge.get_state 自同步，无需插件持有 widget。
        """
        self._auto = bool(active)

    # ---------- 业务逻辑（示例） ----------

    def _do_action(self) -> None:
        """操作区按钮 / 热键的回调示例。"""
        print("demo_ext action triggered")

    # ---------- 状态快照（v3 唯一真相）----------

    def _snapshot(self) -> StateReport:
        """示例状态：始终「示例就绪」。真实插件应报**取值名**（见模块顶的 ``STATES``），
        级别与取值图标归声明（决议 D13），别在报告里重复给。"""
        return StateReport("ready")

    # ---------- 贡献（声明点方法，决议 D20；框架纯消费，无需改 app/panel/settings） ----------

    def row_specs(self):
        """状态面板行。"""
        return (
            StatusRowSpec(
                key="main",
                mode=self.mode_badge_spec(),   # 自动化模式徽标（§5.3 Option 1）
                # 名字与图标都不在行上写（决议 D14/D16）：名字取 Plugin.name，
                # 图标取 component() 里那枚身份图标
            ),
        )

    def component(self):
        """身份的唯一真源（决议 D14）：图标回答「这是谁」，名字省略取 ``Plugin.name``。"""
        return ComponentSpec(icon=IconName.GEAR)

    def state_specs(self):
        """状态声明（静态）：取值闭集 + 首屏起点，标题栏图标颜色 + 行角标由此派生（§4.3）。
        同一组件只有一台状态机 ⇒ 状态名同样不必写（省略取 ``Plugin.name``）。"""
        return (
            StateSpec(
                key="main", icon=IconName.GEAR,
                states=STATES, initial="unknown",
                stale_after=None,
            ),
        )

    def settings_page(self):
        """设置页。"""
        return SettingsPageSpec(
            label="示例外部插件",
            build=self._build_settings_page,
            reason="custom: 动作入口 + 说明页，非字段表单",
        )

    def actions(self):
        """面板操作区按钮。"""
        return (
            ActionSpec(label="示例动作", on_click=self._do_action),
        )

    def hotkeys(self):
        """全局热键。"""
        return (
            HotkeySpec(
                name="demo_ext_hello", label="示例问候",
                default_combo="ctrl+alt+0", callback=self._do_action,
            ),
        )

    def _build_settings_page(self) -> QWidget:
        """构建设置页。

        **为什么本文件的 UI 没有像内部插件那样拆到 `settings.py`**：外部插件由
        `core/loader._import_entry` 用 `spec_from_file_location` 当**顶层独立模块**加载
        （模块名 `barkit_external_<目录名>_<n>`），它没有包上下文，`from . import settings`
        会直接 ImportError。所以「UI 移出 plugin.py」这条（内部插件的规范 §4 禁令 1）
        对外部插件今天做不到——这不是本示例偷懒。改 loader 的加载语义是独立一步。
        """
        page = QWidget()
        page.setObjectName("set-page")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)
        title = QLabel("示例外部插件")
        title.setStyleSheet("font-size:15px; font-weight:700;")
        layout.addWidget(title)
        layout.addWidget(note("这是外部插件贡献的设置页（经 manifest 加载，零改框架）。"))
        layout.addStretch(1)
        return page
