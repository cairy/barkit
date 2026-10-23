"""设置面入口（插件规范 §7.2 的稳定门面）：``build_page`` ＋ ``_RulesEditor`` 本体（类声明与 ``__init__``，全类唯一一处 ``super()``）。

``settings.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QVBoxLayout,
    QWidget,
)

from barkit.shell.ui_kit import (
    EditStack,
    settings_page_shell,
)

if TYPE_CHECKING:
    from barkit.plugins.notify_guard.plugin import Plugin

from .settings_actions import _RulesActionsMixin
from .settings_edit import _RulesEditMixin
from .settings_flow import _RulesFlowMixin
from .settings_list import _RulesListMixin
from .settings_ops import _RulesOpsMixin


class _RulesEditor(
    _RulesListMixin,
    _RulesEditMixin,
    _RulesFlowMixin,
    _RulesActionsMixin,
    _RulesOpsMixin,
    QWidget,
):
    # 混合类按**职责**分开在同目录的兄弟模块，方法经 ``self`` 取用实例状态，
    # 故 MRO 顺序不影响变量解析（混合类之间无继承、无重名）。``QWidget`` 排在
    # **最后**：PySide6 的元类按最靠右的 Qt 基类登记虚函数。

    """设置页主体：单栏「规则列表 → 单条编辑」push 导航（设计 §6.4.3 的形态图）。"""

    def __init__(self, plugin: "Plugin", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._plugin = plugin
        self._rules: list[dict] = plugin.rules()
        self._editing: int | None = None
        self._picked: set[str] = set()
        self._hits: set[str] = set()            # 最近一轮命中的规则名（行渲染要它）
        self._key_rule: dict[str, dict] = {}    # 排序键 → 规则对象（认「这一条」用）

        self._stack = EditStack(self._build_list_page(), self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        # 编辑屏**只造一次**并登记进换屏容器（列表型编辑屏的定式：再进去时字段照上次填的）。
        self._edit = self._build_edit_page()
        self._stack.push(self._edit)
        self._stack.back()               # 开页停在列表
        outer.addWidget(self._stack)
        self._refresh_list()


# 再导出：调用面（``plugin.py`` 与测试）仍从**入口**取这些名字 —— 正文搬去了兄弟
# 模块，入口只负责把它们摆回同一张脸上。平铺模式没有 ``__init__.py`` 替我们做这件
# 事，故这一段是**手工**的（引擎只带 build_page 用得到的那些）。
# 上面那五枚 mixin 是引擎按「owner 要在定义类那一刻拿到全部基类」自动生成的。
# 词条与预设另在 ``settings_words``：入口在定义类那一刻 import 兄弟，而兄弟又在模块
# 作用域读入口的词条 ⇒ 留入口会成环（干跑报告看不出来，只有 import 那刻炸）。
from .settings_words import (
    _APP_WORDS,
    _DISPLAY_WORDS,
    _NOTIFY_WORDS,
    RULE_PRESETS,
)

__all__ = [
    "RULE_PRESETS",
    "build_page",
    "_RulesEditor",
    "_DISPLAY_WORDS",
    "_NOTIFY_WORDS",
    "_APP_WORDS",
]


def build_page(plugin: "Plugin") -> QWidget:
    """构建设置页（框架按 ``SettingsPageSpec.build()`` **零参**调用，故插件侧用闭包注入）。"""
    page, layout = settings_page_shell("录屏通知")
    layout.addWidget(_RulesEditor(plugin, page), stretch=1)
    return page
