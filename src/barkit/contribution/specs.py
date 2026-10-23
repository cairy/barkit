"""
**静态归 Spec，动态归 StateReport**（状态流唯一路径，§5.3）：

- Spec（声明点方法返回，决议 D20）只在插件加载、配置变更、启停时重建；
- 运行期数值（取值名 + 文案）走 `StateReport`，由 tick() 产出、
  框架自动 publish `plugin.<id>.status.<key>`，插件禁自 publish。
- **取值抬到声明层**（决议 D13）：`StateSpec.states` 列举这台状态机的全部取值，
  每个取值自带 `level` 与取值图标；运行期只报取值名，**级别与图标由框架回查声明**
  —— 颜色 / 角标 / 托盘聚合因此只剩一个来源（状态同源，§1-4）。
- **文案只有一份**（决议 D18）：`summary` 同时是面板行副标题、副标题的悬停提示、
  标题栏图标的悬停提示；它**覆盖**取值声明里的默认 `StateOption.text`，
  留空即用默认。`StateReport.detail` 已删除——运行期它零消费（副标题的
  真源一直是 `summary`），留着只会让作者以为存在第二条文案路径。
  需要「下一步做什么」的引导语请落详情页或行内按钮，不要塞进 `summary`。

key 语义（一次定死，勿再讨论，见 Contribution docstring）：
  · key 是**插件内唯一**的语义名（如 "main" / "tunnel"），勿重复写 plugin_id；
  · 全局唯一标识由框架拼装：identity() == f"{plugin_id}.{key or 'default'}"；
  · 同 point 下只有一个贡献时 key 可留空；多个贡献时 key 必填且唯一。

本包只依赖标准库，禁 import PySide6（门禁 test_no_pyside6_in_contribution）。

``specs.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

# --- 拆包后的正文分散在下面这些**同目录兄弟模块**里（平铺拆包，没有 __init__.py
# 替我们做全量再导出）⇒ 这里按依赖树顺序再导出全部契约名字，
# 于是 160 处 `from barkit.contribution.specs import X` 一行未改。
# 兄弟模块之间**只**互相取自己用得到的东西（谁被谁 import 见各自的 docstring）。
from .specs_base import Contribution, WidgetFactory, ActionCallback
from .specs_outcome import ActionOutcome, ActionResult, as_action_result
from .specs_icons import IconName
from .specs_state import (
    StateLevel,
    TRAY_MIN_LEVELS,
    TRAY_MIN_LEVEL_DEFAULT,
    StateOption,
    StateSpec,
)
from .specs_report import StateReport
from .specs_controls import (
    ButtonSpec,
    ChoiceSpec,
    ControlSpec,
    MenuItem,
    MenuSpec,
    SwitchSpec,
)
from .specs_rows import ActionSpec, HotkeySpec, ModeBadgeSpec, SettingsPageSpec, StatusRowSpec
from .specs_component import ComponentSpec
from .specs_detail import (
    DETAIL_PRESENTATIONS,
    DETAIL_PRESENTATION_POPOVER,
    DETAIL_PRESENTATION_WINDOW,
    DetailEntrySpec,
    FormSaveSpec,
)

__all__ = [
    "ActionCallback",
    "ActionOutcome",
    "ActionResult",
    "ActionSpec",
    "ButtonSpec",
    "ChoiceSpec",
    "ComponentSpec",
    "Contribution",
    "ControlSpec",
    "DETAIL_PRESENTATIONS",
    "DETAIL_PRESENTATION_POPOVER",
    "DETAIL_PRESENTATION_WINDOW",
    "DetailEntrySpec",
    "FormSaveSpec",
    "HotkeySpec",
    "IconName",
    "MenuItem",
    "MenuSpec",
    "ModeBadgeSpec",
    "SettingsPageSpec",
    "StateLevel",
    "StateOption",
    "StateReport",
    "StateSpec",
    "StatusRowSpec",
    "SwitchSpec",
    "TRAY_MIN_LEVELS",
    "TRAY_MIN_LEVEL_DEFAULT",
    "WidgetFactory",
    "as_action_result",
]



