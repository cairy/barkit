"""contribution/points：扩展点常量与 Spec 版本（方案 v3 §5.1 / §5.4）。

框架声明「有哪些位置」，插件声明「挂到哪些位置」，两边通过 point 字符串解耦。

**新增一个 UI 位置只需两步**：在本文件加常量 + 在 Shell 注册渲染器。
基类、注册表、插件契约全部不动。

本包是横切契约层（C）：只依赖标准库，**禁止 import PySide6**
（`test_no_pyside6_in_contribution` 守门；WidgetFactory 用 TYPE_CHECKING 守卫）。
"""

from __future__ import annotations

# Spec 数据结构版本：渲染器读的字段。改 Spec 字段者 bump；
# Shell 遇更高版本降级渲染并告警，不崩溃（§5.4 / 附录 B 版本矩阵）。
SPEC_VERSION = 1

# 插件契约版本声明。v1 适配层（legacy/adapter）已删除，框架**只认这一个版本**：
# loader/registry 按 api_version **精确匹配**校验，过旧与过新一律拒绝并给出可读原因
# （见 core/contributions.api_version_reject_reason）。
# 改此值 = 契约不兼容变更：全部插件（含 scaffold 内置模板）须同原子面改齐，否则会被门禁拒绝。
SUPPORTED_API_VERSION = 3

# ---- 状态面板 ----
POINT_STATUS_ROW = "status.row"       # 状态面板的一行（StatusRowSpec）
POINT_SETTINGS_PAGE = "settings.page"  # 设置窗分页（SettingsPageSpec）
POINT_PANEL_ACTION = "panel.action"   # 面板底部动作按钮（ActionSpec）
POINT_MENU_ITEM = "menu.item"         # 菜单栏下拉菜单项（MenuSpec）
POINT_HOTKEY = "hotkey"               # 全局热键（HotkeySpec）

# ---- U1 契约就绪（UI交互分层规范 v2 §8.1）----
POINT_STATE = "state"                 # 状态机声明（StateSpec，静态；动态走 StateReport）
POINT_COMPONENT = "component"         # 组件身份（ComponentSpec，标题栏图标条一枚）
POINT_DETAIL_ENTRY = "detail.entry"   # 详情页入口（DetailEntrySpec）


def declaration_points() -> "dict[str, tuple[str, type, str]]":
    """声明点 ⇄ ``(Plugin 基类方法名, Spec 类, 基数)`` 对应表（决议 D20）。

    「一个声明点一个具名方法」的机器可读真源：收集层（``core/contributions``）
    按本表逐点调用与校验，声明点集合因此**可枚举**——``api_version`` 闸门
    才真正能校验契约变更（新增 / 删除声明点 = 不兼容变更）。

    - **基数** ``"0/1"``：方法返回单个 Spec 或 ``None``；``"n"``：返回 Spec 元组。
    - 表的**键序即收集序**：同一插件各声明点的产出按此顺序拼接进缓存；
      消费者只做 ``isinstance`` 过滤，声明点间的相对顺序不构成契约。

    局部 import 的原因：``specs.py`` 在模块顶层 import 本模块的 POINT 常量，
    本表若在模块级引用 Spec 类会构成循环导入。本函数只在收集期调用，开销可忽略。
    """
    from barkit.contribution.specs import (
        ActionSpec,
        ComponentSpec,
        DetailEntrySpec,
        HotkeySpec,
        SettingsPageSpec,
        StateSpec,
        StatusRowSpec,
    )

    return {
        POINT_COMPONENT: ("component", ComponentSpec, "0/1"),
        POINT_STATE: ("state_specs", StateSpec, "n"),
        POINT_STATUS_ROW: ("row_specs", StatusRowSpec, "n"),
        POINT_DETAIL_ENTRY: ("detail_entry", DetailEntrySpec, "0/1"),
        POINT_SETTINGS_PAGE: ("settings_page", SettingsPageSpec, "0/1"),
        POINT_PANEL_ACTION: ("actions", ActionSpec, "n"),
        POINT_HOTKEY: ("hotkeys", HotkeySpec, "n"),
    }
