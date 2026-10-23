"""barkit.contribution：C 契约层（横切，方案 v3 §3 / §5）。

扩展点常量 + Spec 协议 + PointRegistry。只依赖标准库，
被 L1（core）/ L2（capabilities）/ L3（shell）/ L5（plugins）任意一方依赖，
自身不得反向依赖任何层、不得 import PySide6。
"""

from barkit.contribution.points import (
    POINT_COMPONENT,
    POINT_DETAIL_ENTRY,
    POINT_HOTKEY,
    POINT_MENU_ITEM,
    POINT_PANEL_ACTION,
    POINT_SETTINGS_PAGE,
    POINT_STATE,
    POINT_STATUS_ROW,
    SPEC_VERSION,
    SUPPORTED_API_VERSION,
)
from barkit.contribution.registry import (
    ContributionConflictError,
    PointRegistry,
)
from barkit.contribution.specs import (
    DETAIL_PRESENTATION_POPOVER,
    DETAIL_PRESENTATION_WINDOW,
    DETAIL_PRESENTATIONS,
    ActionCallback,
    ActionOutcome,
    ActionResult,
    ActionSpec,
    ButtonSpec,
    ComponentSpec,
    Contribution,
    ControlSpec,
    DetailEntrySpec,
    FormSaveSpec,
    HotkeySpec,
    IconName,
    MenuItem,
    MenuSpec,
    SettingsPageSpec,
    StateLevel,
    StateOption,
    StateReport,
    StateSpec,
    StatusRowSpec,
    SwitchSpec,
    WidgetFactory,
)

__all__ = [
    "DETAIL_PRESENTATION_POPOVER", "DETAIL_PRESENTATION_WINDOW", "DETAIL_PRESENTATIONS",
    "POINT_COMPONENT", "POINT_DETAIL_ENTRY",
    "POINT_HOTKEY", "POINT_MENU_ITEM", "POINT_PANEL_ACTION", "POINT_SETTINGS_PAGE",
    "POINT_STATE", "POINT_STATUS_ROW", "SPEC_VERSION", "SUPPORTED_API_VERSION",
    "ActionCallback", "ActionOutcome", "ActionResult", "ActionSpec", "ButtonSpec",
    "ComponentSpec", "Contribution", "ContributionConflictError", "ControlSpec", "DetailEntrySpec",
    "FormSaveSpec",
    "HotkeySpec", "IconName", "MenuItem", "MenuSpec", "PointRegistry", "SettingsPageSpec",
    "StateLevel", "StateOption", "StateReport", "StateSpec", "StatusRowSpec", "SwitchSpec",
    "WidgetFactory",
]
