"""插件脚手架模板 · **动作型**骨架（以 ``_`` 开头，发现器不加载，仅作示例 / 复制源）。

接入步骤：复制本目录 → 去掉目录名下划线前缀 → 改 ``id`` / ``name`` / ``order``
与各回调逻辑，即成为真实插件；**零改框架文件**（app.py / panel.py / settings.py 都不动）。
守护型（常驻轮询 + 自动化开关）请改用同目录 ``plugin_daemon.py``（复制时改名为
``plugin.py``）——两种形态选一个作起点，见《插件结构规范》§10.1。

本文件演示的东西（动作型清单，§10.1）：

- ``RUN_ON_DEMAND``：按需触发，无常驻轮询；
- 声明点方法（决议 D20）：``component()`` / ``row_specs()`` / ``state_specs()`` /
  ``settings_page()`` / ``actions()`` / ``hotkeys()``——一个声明点一个方法，
  ``plugin_id`` 由框架注入（Spec 里一个字都不写）；
- ``_do_action()``：操作区按钮 / 热键的回调（**委托**领域模块，不在此写业务）；
- ``_snapshot()``：状态快照工厂（只报**取值名**；``StateSpec.states`` 的静态声明、
  ``StateSpec.initial`` 的首屏起点、手动广播 ``publish_snapshot()`` 共用同一份取值闭集）。

两条会踩的硬约束，照抄时别违反：

1. **本文件不得出现任何 ``PySide6`` import（含函数内惰性 import）**。UI 构建一律在
   ``settings.py`` / ``<name>_view.py``；门禁 ``test_plugin_py_has_no_pyside6`` 按 AST 判定。
2. **状态流唯一真相是 ``StateReport``**：``tick()`` 返回它、手动操作用
   ``self.publish_snapshot()`` 即时广播（topic 由 ``self.status_topic()`` 派生）。
   插件**不得**自己 ``ctx.events.publish``，更不得广播裸 dict（B 契约，门禁守着）。
"""

from __future__ import annotations

import logging

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
    SwitchSpec,
)
from barkit.core.plugin import RUN_ON_DEMAND, Plugin

# 包内自引用一律相对导入（§2.4）：复制改名后不必再改这一行。
from . import settings

log = logging.getLogger(__name__)

#: 取值闭集（决议 D13）：取值名 / 级别 / 默认文案只写一次，报告只报取值名。
#: 复制本模板时按自己的业务把取值列全 —— 数值在这里，界面上才有颜色可画。
STATES: tuple[StateOption, ...] = (
    StateOption("unknown", StateLevel.IDLE, text="尚未读取"),
    StateOption("ready", StateLevel.OK, text="模板就绪"),
)


class Plugin(Plugin):
    id = "template"
    name = "模板插件"
    run_mode = RUN_ON_DEMAND
    api_version = 3          # 契约版本示例；须与 contribution/points.SUPPORTED_API_VERSION **精确相等**
    order = 90               # 新插件默认插入位：仅当 id 未进入 config.plugin_order 时生效

    # ---------- 业务逻辑（示例：真实插件在此委托领域模块）----------

    def _do_action(self) -> None:
        """操作区按钮 / 热键的回调。

        真实插件这里只做「编排 + 委托」：决策在 ``<domain>.py``，界面在 ``settings.py``。
        """
        log.info("模板动作已触发")

    def toggle_action(self) -> None:
        """状态行开关的回调：改完状态后**立即**广播，不等下一拍轮询。

        广播走基类的 ``publish_snapshot()``（B 契约下插件唯一的即时广播入口），
        它只可能发到本插件自己的 ``status.main`` topic。
        """
        self.publish_snapshot()

    # ---------- 状态快照（静态声明与手动广播共用的单一真源）----------

    def _snapshot(self) -> StateReport:
        """状态快照工厂：只回答「现在是哪一格」（取值名，决议 D13）。

        取值闭集见模块顶的 ``STATES``：**级别与取值图标归声明**，这里不要重复给
        （一套取值三个真源，迟早在界面上分叉）。

        ``summary`` 是**唯一文案**（决议 D18）：面板行副标题与两处悬停提示共用这一句，
        所以要写成人看得懂的一句话（如「3 条生效 · 最后检查 12:04」），不要只写两个字；
        需要「下一步做什么」的引导语请落详情页或行内按钮，别塞进这里。
        **留空则用该取值声明的默认文案** —— 默认文案够用时就不必在这里再写一遍。

        失联判定看的是**框架收到报告的时刻**（配 ``StateSpec.stale_after``），
        不是本报告里的 ``updated_at``（该字段今天全仓零消费，见遗留项）。
        """
        return StateReport("ready")

    # ---------- 贡献（声明点方法，决议 D20；框架纯消费，无需改 app / panel / settings）----------

    def component(self):
        """标题栏组件图标（每插件一枚，U2 §5.2）：身份的唯一真源（D14）——
        名字**不要**在这里再写一遍，省略即取 ``Plugin.name``。

        **前提：你有状态机**（◷ 2026-09-17）。这一格画的是**代表态的取值图标 ＋
        级别色**，没有 ``state_specs()`` 就没有代表态 ⇒ 声明出去只会永远画同一枚
        中性色的身份图标、悬停只说组件名。按需型组件（如 ``recognize``）因此**不声明**
        本方法（基类默认返回 ``None``）；门禁
        ``test_display_slot_declarations_need_a_state_machine`` 守这条。
        """
        return ComponentSpec(icon=IconName.GEAR)

    def row_specs(self):
        """状态面板行。"""
        return (
            StatusRowSpec(
                key="main",
                controls=(
                    SwitchSpec(checked=False, on_toggle=self.toggle_action),
                ),
            ),
        )

    def state_specs(self):
        """状态声明（**静态**：取值闭集 + 节律元数据，不放 level / summary——
        动态数值归报告）；initial 是首屏起点（取值名，不是工厂函数）。
        同一组件只有一台状态机 ⇒ 状态名同样不必写（省略取 ``Plugin.name``）。"""
        return (
            StateSpec(
                key="main", icon=IconName.GEAR,
                states=STATES, initial="unknown",
                stale_after=None,
            ),
        )

    def settings_page(self):
        """设置页：``build`` 是今天唯一可用的声明路径（``schema`` 尚未接线，
        只填它会崩，见规范 §7.1）；用了 ``build`` 就必须写 ``reason``（§7.1 决议 2）。
        界面本体在 ``settings.py``——本文件不许 import PySide6。"""
        return SettingsPageSpec(
            build=lambda: settings.build_page(self._do_action),
            reason="动作入口 + 使用说明页，无字段表单可声明（渲染器未接线，自绘是唯一路径）",
        )

    def actions(self):
        """面板操作区按钮。"""
        return (
            ActionSpec(label="模板动作", on_click=self._do_action),
        )

    def hotkeys(self):
        """全局热键。"""
        return (
            HotkeySpec(
                name="template_action", label="模板动作",
                default_combo="", callback=self._do_action,
            ),
        )
