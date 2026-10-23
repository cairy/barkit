"""``ComponentSpec`` —— 插件的**组件身份**：标题栏图标条的一枚，与状态无关、一个插件一枚。

它是 ``icon``（身份图标）与 ``name``（组件名）的唯一真源（决议 D14），也是唯一一枚在**字段默认值**里真正运行期用到 ``IconName`` 的 Spec（``IconName.GEAR``）—— 这处运行期取值决定了本模块必须 import ``icons``，而不只是注解。

依赖：``base``（``Contribution``）＋ ``icons``（运行期 ``IconName.GEAR``）。

``specs.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from dataclasses import (
    dataclass,
    field,
)

from .specs_base import Contribution
from .specs_icons import IconName


# ---- U1 契约就绪（UI 分层规范 v2 §8.2）----


@dataclass
class ComponentSpec(Contribution):
    """插件的组件身份：标题栏图标条的一枚。与状态无关，一个插件一枚。

    它是**组件身份的唯一真源**（决议 D14），两件事都从这里取：

    - ``icon``：**身份图标** —— 标题栏图标条、状态行图标盒主位画的就是它
      （``StatusRowSpec`` 已不再声明图标）。它回答「这是谁」，不随状态变；
      「什么状态」归取值图标（``StateOption.icon``）与级别色。
    - ``name``：**组件名** —— 省略即取 ``Plugin.name``（收集期框架补上），
      标题栏悬停提示、溢出簇清单、设置窗的菜单栏来源候选都读这一份。

    名字与图标都不必各写一遍：与 ``Plugin.name`` 相同的名字写了也是重复，取值图标
    与身份图标相同时填了也只是重复。组件没声明它就没有身份可言 —— 图标位留空，
    框架不替它编一枚（门禁 ``test_identity_name_is_not_written_twice`` 守名字那一半）。
    """

    point: str = field(default="component", init=False)
    icon: IconName = IconName.GEAR
    name: str = ""
    # 要不要在**标题栏图标条**占一格（默认要）。关掉之后身份仍然在 —— 行主位图标、
    # 组件名照旧取这一份，只是标题栏不出现它。
    #
    # 为什么需要它：身份图标是「组件有身份」这件事的唯一真源，而「标题栏那一格」是
    # 一种**呈现**，两者此前被绑成一体 —— 于是「按需型、无状态机」的组件陷入死锁：
    # 声明身份 ⇒ 标题栏多一格恒灰图标（无状态机 ⇒ 那格说不出任何事，2026-09-17
    # 用户裁定不许）；不声明 ⇒ 状态行图标位空框（D16：行主位画 ComponentSpec.icon）。
    # 结果就是这类组件只能用 ``custom`` 自绘行绕开，进而把行的尺寸常量写成第二份。
    # 拆开这两件事：``in_header=False`` 的组件有身份、有行，但不上标题栏。
    in_header: bool = True
