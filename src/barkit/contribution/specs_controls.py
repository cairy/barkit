"""**行内控件四件**与它们的联合类型：``SwitchSpec`` / ``ButtonSpec`` / ``MenuItem`` / ``MenuSpec`` / ``ChoiceSpec`` ＋ ``ControlSpec = Union[...]``。

渲染权在 Shell，插件只给数据 —— 这一族是「给数据的那一层」，不含任何 Qt 类型（控件壳由 ``shell/panel/widgets.py`` 渲染）。分家的理由与 ``ModeBadgeSpec`` 同类：它们只被状态行的 ``controls`` 读，塞进 rows 那条文件会把「位置 Spec」撑大。
依赖：``base`` 的 ``ActionCallback``（各 Spec 的回调字段是注解）。

``specs.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import (
    Callable,
    Sequence,
    Union,
)

from .specs_base import ActionCallback


# ---- 控件 Spec（渲染权在 Shell，插件只给数据）----


@dataclass
class SwitchSpec:
    checked: bool
    on_toggle: "ActionCallback"
    enabled: bool = True
    tooltip: str = ""
    get_checked: "Callable[[], bool] | None" = None   # 运行时重读开关态（外部变更刷新，§5.3）
    get_enabled: "Callable[[], bool] | None" = None   # 运行时重读可用态（如守护未装，§5.3）


@dataclass
class ButtonSpec:
    label: str
    on_click: "ActionCallback"
    variant: str = "ghost"          # ghost | primary | danger


@dataclass(frozen=True)
class MenuItem:
    """菜单候选项（契约 §2.6 / 决议 D25）。

    用户是在「**选哪个**」的时刻需要上下文：「office」只是个名字，看不出它管哪个
    网段、带几条路由 —— **只给名字的菜单等于没有信息**。故 ``hint`` 是**必答项**
    而非装饰：它承载「这一项是什么 / 会带来什么」的副文案，不参与选择。

    ``label`` 同时是**选中匹配键**（``MenuSpec.current`` 按它匹配），故同一个菜单里
    的 ``label`` 必须唯一。
    """

    label: str
    on_click: "ActionCallback"
    hint: str = ""


@dataclass
class MenuSpec:
    """点击弹出菜单（注意：是**弹菜单**，不是下拉框；契约 §2.6）。

    候选项与当前项都可在**运行期重读**：``get_items`` / ``get_current`` 与
    ``SwitchSpec.get_checked`` / ``get_enabled`` 同款 —— 纯读取、每次刷新求值
    （不得有副作用、不得依赖调用次数），不声明时用静态的 ``items`` / ``current``。

    什么时候必须给回调：**候选集合会随配置变化**（如「网络切换」的场景列表会随
    配置窗体改动而变、随运行时命中的场景而变）。只读一次的静态声明在这类组件上
    会让用户看到过期列表。
    """

    items: "tuple[MenuItem, ...]"
    current: str = ""                                        # 按 MenuItem.label 匹配
    get_items: "Callable[[], Sequence[MenuItem]] | None" = None
    get_current: "Callable[[], str] | None" = None


@dataclass
class ChoiceSpec:
    """行内**下拉选择器**（第四种行内控件，契约 §2.6）。

    与 ``MenuSpec`` 的分界：**弹菜单是动作，下拉是状态**。

    - ``MenuSpec``：点开列表，**选一项就执行那一项的动作**（每项自带 ``on_click``）；
    - 本控件：持久显示当前项，**切换只改选中，不执行任何业务动作** —— 真正的动作
      挂在整行 ``on_click`` 上（如「一键输入」：下拉选输入项，点整行才输入）。

    这条分界是它存在的理由：在它之前，要「行内带一个下拉框」的组件只能走
    ``StatusRowSpec.custom`` 逃生舱自己拼一行，于是行的尺寸/对齐常量出现第二份，
    与框架渲染的那份各自漂移（2026-09-20 实修：逃生舱行 30 盒 / 16px / 左对齐，
    框架行 36 盒 / 22px / 居中，界面上表现为那一行图标小一圈且贴左）。

    候选与当前项都支持**运行期重读**（``get_items`` / ``get_current``），与
    ``SwitchSpec.get_checked`` 同款：纯读取、每次刷新求值，不得有副作用、不得依赖
    调用次数。候选集合会随配置变化的必须给回调。

    ``on_change`` 是**通知**不是动作：插件拿它记住「现在选中的是哪个」即可，
    不要在里面执行业务（那会让「切换一下」和「执行一次」混为一谈）。
    """

    items: "tuple[str, ...]" = ()
    current: str = ""                                        # 按 items 里的显示名匹配
    get_items: "Callable[[], Sequence[str]] | None" = None
    get_current: "Callable[[], str] | None" = None
    on_change: "Callable[[str], None] | None" = None         # 选中变化通知（非动作）
    enabled: bool = True
    get_enabled: "Callable[[], bool] | None" = None
    tooltip: str = ""
    width: int = 0                                           # 0 = 由内容自然宽度定


ControlSpec = Union[SwitchSpec, ButtonSpec, MenuSpec, ChoiceSpec]
