"""**详情页这一族**：形态常量（``DETAIL_PRESENTATION_POPOVER`` / ``_WINDOW`` / ``DETAIL_PRESENTATIONS``）、``FormSaveSpec``（大窗体的「保存」：``commit`` ＋ ``dirty`` 两枚回调，决议 D35）、``DetailEntrySpec``（详情页入口，整行点击与快捷区域那枚按钮是同一个过程）。

常量与两个 Spec 同在一处：``DetailEntrySpec`` 的 ``__post_init__`` 直接拿那两个常量当默认值、拿 ``DETAIL_PRESENTATIONS`` 当校验域，拆开就得跨模块传一组三个常量。
依赖：``base``（``Contribution``）＋ ``icons``（``DetailEntrySpec.icon`` 的注解）。

``specs.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from dataclasses import (
    dataclass,
    field,
)
from typing import Callable

from .specs_base import Contribution, WidgetFactory
from .specs_icons import IconName


# 详情页呈现形态（决议 D26）：框架给多大的壳。取值只有两个，声明期即校验。
DETAIL_PRESENTATION_POPOVER = "popover"
DETAIL_PRESENTATION_WINDOW = "window"
DETAIL_PRESENTATIONS = (DETAIL_PRESENTATION_POPOVER, DETAIL_PRESENTATION_WINDOW)


@dataclass(frozen=True)
class FormSaveSpec:
    """大窗体的「保存」：**两枚回调**，框架只画键、拦关窗、转达结果（决议 D35）。

    存在的理由：``presentation="window"`` 的窗体是「改完再一起写」的编辑面（左列表 ＋
    右表单那类集合型配置）。框架壳原先只有一枚「完成」，而「什么时候写下去」被隐式定成
    「改一格写一次」—— 于是**界面改了半天、盘上没动**这件事在界面上完全看不出来，
    用户点「完成」也不知道自己刚才那一下到底生效没有。把「什么时候写」交回组件
    （只有它知道什么算填完），框架只负责三件机械的事：

    - **画键**：声明了就长一颗「保存」（没声明的窗体一字不变）；
    - **拦关窗**：``dirty()`` 为真时关窗**先问一句**（问在窗内，不是弹模态框）；
    - **转达**：``commit()`` 的成败各出一句行内回执。

    - ``commit``：写盘。返回 ``(ok, text)``——``ok=False`` 时 ``text`` 是失败原因（红字，
      必填）；``ok=True`` 时 ``text`` 留空则框架显示「已保存」。
    - ``dirty``：现在还有没有没写进去的改动。框架**只在关窗那一刻**问它，不是轮询 ——
      作者可以按「与上次保存时的快照比一比」的代价去算。

    **为什么 ``dirty`` 是必答项**：没有它，关窗就只能靠作者自己在别处兜底
    （否则「改了一半点完成」＝静默丢改动，而那条正是无保存键时代被记下来的老账）。
    """

    commit: "Callable[[], 'tuple[bool, str]']"
    dirty: "Callable[[], bool]"


@dataclass
class DetailEntrySpec(Contribution):
    """详情页入口：**整行点击**与**快捷区域那枚按钮**是**同一个过程**（2026-09-22）。

    声明了它 ⇒ 该组件的状态行可点、底部栏「快捷区域」也有它一枚按钮；两处都发同一个
    ``detailRequested`` → ``DetailHost.open``（这就是 D28 的「入口一条链」）。

    ``trigger`` 字段已删（2026-09-22）：它此前让作者**二选一**（入口长在行上还是长在
    底部栏），而两处触发的是同一个过程 —— 「选哪个」实际只是在选**哪个入口可见**。
    那本该是**用户层**的事：显示位的「列表行」与「快捷区域」本就是两枚独立开关（决议
    D4），用户各自可关。作者写死它，就出现「声明了 row 的组件底部栏永远没有它」这种
    用户改不了的局面（recognize 反向受限：只有快捷区域、行上永远缺席）。

    删掉之后：**行恒在**（框架为每个组件合成，见
    ``core/contributions.ContributionRegistry.status_rows``）、快捷区域按钮对每个声明了
    入口的组件都在 —— 两个入口都归用户层开关管。

    注意：**行为变更** —— 此前 ``trigger="row"`` 的三个组件（hosts 托管 / 路由守护 /
    iPad 隧道）**底部栏会多出一枚按钮**。面板 A 区有溢出兜底（可见上限之外的折进末端
    「⋯」，§5.6），用户不需要它时关掉该组件的「快捷区域」显示位即可。

    ``presentation`` 选**框架托管的窗口形态**（决议 D26），**按内容宽度一开始就选**：

    · ``"popover"``（默认）——**贴着菜单栏图标弹出的浮层卡**，固定宽度、点外部即关。
      一屏能平铺完的内容用它。
    · ``"window"``——**原生标题栏的独立窗**（居中弹出，关闭键与缩放都归系统），
      初宽 620 且用户可缩放。「左列表 + 右表单」这类集合型编辑器实测要 560–640px，
      浮层卡装不下；先按 popover 设计、放不下再改，代价是表单要重排，故一开始就按
      内容宽度选。**它不做「点外部即关」** —— 那是浮层卡的行为，一个居中的独立窗
      被点一下就没了比不给关更危险。

    形态只影响框架给多大的壳，**不影响 ``build`` 返回什么**——两种形态都接收同一个
    内容 widget。取值非法在构造期报错，与 ``ModeBadgeSpec`` / ``StatusRowSpec`` 的
    校验同一先例：宁可声明期报错，不静默回落。

    ``save`` 也**只对 window 形态有意义**（决议 D35）：浮层卡是「点开就改、点外面就
    没了」的形态，攒一堆改动再问一句「要不要存」在那种形态里没有立足之地；声明组合
    非法同样在构造期报错，不静默忽略（填了不生效是最坏的一种「成功」）。
    """

    point: str = field(default="detail.entry", init=False)
    label: str = ""
    # 快捷区域那枚按钮的图标（与 ``ActionSpec`` 同款的图标位）；框架合成按钮时带过去，
    # 否则统一入口的按钮会丢掉图标。整行点击没有图标按钮，用不到它。
    icon: "IconName | None" = None
    build: "WidgetFactory | None" = None
    presentation: str = DETAIL_PRESENTATION_POPOVER   # "popover"（浮层卡）| "window"（原生独立窗）
    save: "FormSaveSpec | None" = None                # “怎么保存”（只 window 形态可用，见 D35）
    # window 形态的**初宽**（px）；None = 用框架默认。内容宽的页面（多列表格、工具条
    # 挤不下）声明一个更宽的初宽，别让控件在默认宽度里互相压。声明了不生效是最坏的
    # 一种「成功」，故填在 popover 上直接报错（与 ``save`` 同一条纪律）。
    width: int | None = None

    def __post_init__(self) -> None:
        if self.presentation not in DETAIL_PRESENTATIONS:
            raise ValueError(
                f"DetailEntrySpec：presentation 只能是 {DETAIL_PRESENTATIONS} 之一，"
                f"收到 {self.presentation!r}"
            )
        if self.save is not None and self.presentation != DETAIL_PRESENTATION_WINDOW:
            raise ValueError(
                "DetailEntrySpec：save 只对 presentation='window' 有意义"
                f"（收到 presentation={self.presentation!r}）—— 浮层卡没有「保存」键的"
                "位置与生命期，声明了也不会渲染。要么改 window，要么别声明"
            )
        if self.width is not None:
            if self.presentation != DETAIL_PRESENTATION_WINDOW:
                raise ValueError(
                    "DetailEntrySpec：width 只对 presentation='window' 有意义"
                    f"（收到 presentation={self.presentation!r}）—— 浮层卡是固定宽的，"
                    "声明了也不会生效。要么改 window，要么别声明"
                )
            if self.width <= 0:
                raise ValueError(
                    f"DetailEntrySpec：width 必须是正整数（收到 {self.width!r}）"
                )
