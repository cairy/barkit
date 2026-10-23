"""**公共底座三件**：``Contribution``（所有 Spec 的基类，``identity()`` 是声明式 UI 的复用键）、``WidgetFactory``（逃生舱工厂函数的类型别名，只在 ``TYPE_CHECKING`` 里才认识真 QWidget）、``ActionCallback``（动作回调类型别名）。

三个都是「被多方引用、自己不依赖别人」的那类：``Contribution`` 被 rows / state / component / detail 继承，``WidgetFactory`` 被 rows / detail 的注解读，``ActionCallback`` 被 controls / rows 的注解读。放一起是为了让依赖图**成树** —— 按原文件里它们散在 import 段与动作结果之间的位置分家，会把 ``rows → base → outcome`` 拐成一条回边（见 plan 文件头）。

依赖：只向下取 ``outcome`` 的 ``ActionResult``（``ActionCallback`` 的注解里出现）。

``specs.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Callable,
    Optional,
)
# ⚠️ 手工补（引擎按 ast 名字粒度重建 import，**看不见字符串里的名字**）：
# ``ActionCallback = Callable[[], Optional["ActionResult"]]`` 的注解是字符串 ⇒ ActionResult
# 落在这里，而没有一条 ast.Name 拉它。outcome 是叶子、base 只向下取它 ⇒ 不成环。
from .specs_outcome import ActionResult


if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

    WidgetFactory = Callable[[Optional["QWidget"]], "QWidget"]
else:
    WidgetFactory = Callable[[object], object]


@dataclass
class Contribution:
    """所有 Spec 的基类。

    `key` 是声明式 UI 的 identity —— 渲染层靠它在重绘时复用 widget，
    而不是靠位置或对象身份。这是声明式 UI 的第一性问题（React / SwiftUI 同理）。

    key 语义（一次定死，勿再讨论）：
      · key 是**插件内唯一**的语义名，如 "main" / "tunnel" / "dns"
      · 不要写成 "route_guard.main" —— plugin_id 不该重复写一遍
      · 全局唯一标识由框架拼装：identity() == f"{plugin_id}.{key}"
      · 同一声明点下只有一个贡献时 key 可留空（框架取 "default"）；
        同一声明点下有多个贡献时 key 必填且唯一，重复即收集期报错

    ``plugin_id`` **不由组件写**（决议 D20）：默认空串，框架在收集期按
    ``Plugin.id`` 注入——归属漂移（写错别人的 id）从机制上不可能再发生。
    """

    point: str
    plugin_id: str = ""
    key: str = ""
    order: int = 0          # 同插件内多贡献的相对顺序（不决定插件间顺序，§6.4）

    def identity(self) -> str:
        return f"{self.plugin_id}.{self.key or 'default'}"


#: 带结果回馈的动作回调。**返回 ``None`` 是合法的**（等于写了句什么都没说的结果），
#: 故现存的 ``def f() -> None`` 一个字都不必改 —— 这是纯扩宽，零破坏。
ActionCallback = Callable[[], Optional["ActionResult"]]
