"""**位置 Spec 五件**：``ModeBadgeSpec``（模式徽标，点击形态二选一有构造期校验）、``StatusRowSpec``（框架渲染的状态行，控件硬上限 2 也在构造期校验）、``SettingsPageSpec`` / ``ActionSpec`` / ``HotkeySpec``。

它们有一个共同点：``point`` 字段是 ``field(default=..., init=False)`` 声明的，收集期靠 ``Contribution.point`` 分桶 ⇒ 这一族必须有 ``Contribution`` 这个基类在作用域里（依赖 ``base``），并且是全包里最大的一块（117 行）。

依赖：``base``（``Contribution`` ＋ ``ActionCallback``）＋ ``controls``（``MenuItem``）＋ ``icons``（``ActionSpec.icon`` / ``HotkeySpec`` 的注解）。

``specs.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from dataclasses import (
    dataclass,
    field,
)
from typing import (
    Callable,
    Sequence,
)

from barkit.contribution.points import (
    POINT_HOTKEY,
    POINT_PANEL_ACTION,
    POINT_SETTINGS_PAGE,
    POINT_STATUS_ROW,
)

from .specs_base import ActionCallback, Contribution, WidgetFactory
from .specs_controls import ControlSpec, MenuItem
from .specs_icons import IconName


# ---- 位置 Spec ----


@dataclass
class ModeBadgeSpec:
    """状态行模式徽标（§5.3，取代写死 Badge）：自动 / 手动 / 已接管 等运行模式。

    框架渲染 ModeBadge：accent 态 = accent_soft 底 + accent_text 字；neutral 态 =
    surface2 底 + text2 字；可点时手型。运行时模式变化经 ``get_state``
    重读 ``(label, accent)``，由框架在状态刷新时调用——插件无需持有 widget 引用即可
    保持徽标与内部态同步（§5.3 Option 1）。

    **两种点击形态，互斥**（契约 §2.5）：

    · **二元切换**：只给 ``on_click`` —— 点一下直接切到另一档（「自动 ⇄ 手动」）。
    · **候选列表**：给 ``items`` / ``get_items`` —— 点一下展开列表、选中即生效；
      「选中某项做什么」写在各自的 ``MenuItem.on_click`` 上，无需二次确认。

    候选列表形态下 ``label`` **兼任当前项**（按 ``MenuItem.label`` 匹配，与
    ``MenuSpec.current`` 是同一套匹配键）⇒ **不设 ``current`` 字段**：徽标上写着谁，
    谁就是当前项；多一个字段就多一个真源（决议 D33）。
    """

    label: str
    accent: bool = True
    on_click: "ActionCallback | None" = None
    get_state: "Callable[[], tuple[str, bool]] | None" = None
    items: "tuple[MenuItem, ...]" = ()                       # 静态候选（一般不配，见下）
    get_items: "Callable[[], Sequence[MenuItem]] | None" = None  # 运行期重读，优先于 items

    def __post_init__(self) -> None:
        """点击形态二选一：同时声明 ``on_click`` 与候选列表 = 声明者自己不知道哪个生效。"""
        if self.on_click is not None and (self.items or self.get_items is not None):
            raise ValueError(
                "ModeBadgeSpec：on_click 与候选列表（items / get_items）互斥 —— "
                "有列表时点击就是展开列表，「选中某项做什么」写在各 MenuItem.on_click 上"
            )


@dataclass
class StatusRowSpec(Contribution):
    """框架渲染的状态行（§5.3）：图标盒（**身份图标**）＋ 名称 ＋ 副标题 ＋ 右侧控件。

    **身份不在行上声明**（决议 D14 ＋ D16）——同一个字符串不写第二遍：

    - 名称：``title`` 省略即取 ``Plugin.name``（收集期由框架补上，见
      ``core/contributions.ContributionRegistry``）。只有同一组件需要给这一行
      **另起一个名字**时才填；与 ``Plugin.name`` 字面相同的值一律不写。
    - 图标：**行上再也声明不了图标**（``icon`` 字段已删）。身份图标只有一个来源
      ``ComponentSpec.icon``，画在图标盒主位、固定不随状态变；状态信息全归角标。
    - 角标（取值图标染级别色）与副标题都是**运行期派生**，不在声明里给 —— 前者由
      ``state_view.resolve`` 从代表态解析，后者未声明行级 ``subtitle`` 时回退代表态
      ``summary``（决议 D18）。
    """

    point: str = field(default=POINT_STATUS_ROW, init=False)
    title: str = ""
    mode: "ModeBadgeSpec | None" = None         # 模式徽标（结构化声明，§5.3 Option 1）
    # 行右侧控件（决议 D6）：0–2 个，**硬上限 2**。顺序即从左到右的排布顺序，
    # 由 Shell 统一渲染（三种类型都在渲染器覆盖面内：SwitchSpec / MenuSpec / ButtonSpec）。
    controls: "tuple[ControlSpec, ...]" = ()
    # 整行点击（**可带结果回馈**：`ActionResult | None`，见 `ActionResult` 的说明）。
    # 优先级在 framework 侧（`shell/panel.StatusPanel._row_on_click`）：本字段优先于合成的
    # 「打开详情页」（契约 §2.4 / 决议 D28）。
    on_click: "ActionCallback | None" = None
    get_clickable: "Callable[[], bool] | None" = None  # 动态整行可点（如路由守护过期）
    # 行级副标题（**纯读取回调**，契约 §2.7 / 决议 D24）：多台状态机时由组件把几台机
    # 合成一句，框架只负责画。未声明（None）时回退代表态快照的 summary——单状态机组件
    # 一字不改，这是纯增补。回调不得有副作用、不得依赖调用次数（框架每次刷新求值）。
    subtitle: "Callable[[], str] | None" = None
    custom: "WidgetFactory | None" = None       # 逃生舱：工厂函数而非实例
    reason: str = ""                            # 使用 custom 时必须说明原因

    def __post_init__(self) -> None:
        """控件个数硬上限 2（决议 D6）——声明期就拒绝，不等渲染时挤成一团。

        上限不是「暂时没做」，是**布局约束**：popover 宽约 320px，一行里还要放
        图标盒 30 + 名称 + 副标题，第三个控件必然挤掉名称。装不下的配置属**低频、
        多字段**，归设置页（``SettingsPageSpec``），不在这里堆。同 ``ModeBadgeSpec``
        的互斥校验：宁可声明期报错，不要静默取前两个。
        """
        if len(self.controls) > 2:
            raise ValueError(
                f"StatusRowSpec：controls 最多 2 个（决议 D6），收到 {len(self.controls)} 个 —— "
                "低频 / 多字段的配置请放设置页"
            )


@dataclass
class SettingsPageSpec(Contribution):
    point: str = field(default=POINT_SETTINGS_PAGE, init=False)
    label: str = ""
    schema: "object | None" = None              # FormSchema（core/schema.py），声明式默认
    build: "WidgetFactory | None" = None        # CustomPage 逃生舱
    reason: str = ""                            # 使用 build 时必须说明原因


@dataclass
class ActionSpec(Contribution):
    point: str = field(default=POINT_PANEL_ACTION, init=False)
    label: str = ""
    # **2026-09-28 扩宽成 ``ActionCallback``**（此前刻意保持 ``Callable[[], None]``）：
    # 当时的理由是「底部栏这枚按钮按下后，面板先 ``hide()`` 自己再跑回调 ⇒ 结果回来时
    # 承载结果的那一块界面**已经关了**，此时再多挂一句『可以返回结果』就是一条送不出去的
    # 通道」。启动条件是「出现第一个需要结果反馈的 ``ActionSpec``，**且届时先把『按下后
    # 要不要收起面板』这半边一并裁定**」——两条都在本轮满足／裁定：
    #
    # - **裁定：保持收起**（不动这一处行为）。这些按钮开的是别的窗，收起是有意的；
    # - 出口补在**面板之外**：宿主注入 ``StatusPanel.set_off_panel_feedback``，由
    #   ``app_shell._report_off_panel`` 把**失败 / 半成功**送进系统通知（成功那档在面板里
    #   就拦下）。于是「结果回头时界面已经关了」不再等于「送不出去」。
    on_click: "ActionCallback | None" = None
    icon: "IconName | None" = None


@dataclass
class HotkeySpec(Contribution):
    point: str = field(default=POINT_HOTKEY, init=False)
    name: str = ""                  # 配置键 hotkeys.<name>
    label: str = ""
    default_combo: str = ""
    # **2026-09-28 扩宽成 ``ActionCallback``**（此前刻意保持 ``Callable[[], None]``）：
    # 当时的理由是「热键触发时面板多半是关着的，没有承载结果的界面 ⇒ 免得挂出一个没人收的
    # ``ActionResult``」。现在**有接收方了**：宿主那条热键反馈通道（系统通知 / 轻浮层，
    # ``shell/hotkey_feedback``）本来就为「面板关着」而生，扩宽后回调的返回值在那里被消费
    # （判定与包装在 ``hotkey_feedback.wrap_hotkey_callback``，宿主只接两个读取口）。
    callback: "ActionCallback | None" = None
