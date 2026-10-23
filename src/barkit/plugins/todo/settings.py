"""todo/settings：**设置页 = 只剩配置**（合并方案 §4.3）。

三张配置卡：浮层大小 / 关闭方式 / 最多显示几条，外加一张**入口卡**（任务管理）。
**条目的增删改一律不在这里** —— 那是详情页的事，本页再放一份编辑器就是给同一件事开
第二个真源（两份互不感知：一份看不到另一份刚做的改动）。

入口卡里那枚按钮**只开窗、不画内容**：走 ``Plugin.open_detail()``，进的是框架
``DetailHost`` 按 plugin_id 托管的**同一份**内容面 —— 与点面板整行进去的完全是那份
（已开着就提到前台，不会开出第二扇）。这正是它与 ``hosts_guard`` 那枚被删掉的
「管理条目…」按钮的区别：那一枚自己 new 了一个私有对话框，与整行入口各开一份。
2026-10-10 之前本页只留一句指路文字（「点面板上那一行进去」），那要求用户先知道
「面板上的哪一行是自己的待办」；按钮把这条路上的那一步省掉。

**「常驻预览」开关已删**（方案 §4.3）：它原本是合并前那个翻页钟组件「面板行点击」的
行为，而合并后面板整行归详情页。预览改为**在条目编辑页对那一条看**（``detail_view.py``
的「预览这一条」）—— 一条一条地看，比一枚全局开关有用。

``size`` / ``close_gesture`` 两格的取值域是**本组件自己那份**（合并前两个组件各有一份
逐字相同的抄写，批 5 旧组件退场之后只剩这里一份）。

本模块不许碰 ``Config``（门禁 ``test_no_config_io_in_ui``）：三格各自走插件的
``set_size`` / ``set_close_gesture`` / ``set_max_rows``；入口那一枚走 ``open_detail()``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

from barkit.shell.ui_kit import (
    ChoiceSelector,
    FeedbackBar,
    NumberField,
    form_row,
    note,
    settings_page_shell,
    settings_section,
    tool_button,
)

if TYPE_CHECKING:
    from barkit.plugins.todo.plugin import Plugin

#: 浮层大小三档。**显示名与取值分家**：取值是盘上存的（改名不该让已存的取值失效）。
SIZE_CHOICES = [("小", "small"), ("中", "medium"), ("大", "large")]

#: 关闭方式三选（左键拖动与它们互不打架）。
CLOSE_CHOICES = [("双击", "double_click"), ("右键", "right_click"), ("双击或右键", "either")]

#: 「最多显示几条」的上限。0 是特例（不限），故下限必须是 0。
MAX_ROWS_LIMIT = 100


class _TodoSettings(QWidget):
    """设置页主体：三张配置卡 ＋ 一条行内反馈槽。"""

    def __init__(self, plugin: "Plugin", parent: "QWidget | None" = None) -> None:
        super().__init__(parent)
        self._plugin = plugin
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        # 反馈槽提到页首：本页虽短，但「设置窗可缩放」是框架的既定行为，反馈落在页脚时
        # 用户在页首点完按钮什么也看不见（route_guard 那页同款理由）。
        self._fb = FeedbackBar(self)
        lay.addWidget(self._fb)

        # ── 1. 浮层大小 ──
        size_card, size_lay = settings_section("浮层大小", parent=self)
        self._size_sel = ChoiceSelector(SIZE_CHOICES, plugin.get_size(), size_card)
        self._size_sel.selected.connect(self._on_size)
        size_lay.addLayout(form_row("尺寸", self._size_sel, size_card))
        size_lay.addWidget(note("浮层在屏幕上的大小档（小 / 中 / 大）。", size_card))
        lay.addWidget(size_card)

        # ── 2. 关闭方式 ──
        close_card, close_lay = settings_section("关闭方式", parent=self)
        self._close_sel = ChoiceSelector(
            CLOSE_CHOICES, plugin.get_close_gesture(), close_card
        )
        self._close_sel.selected.connect(self._on_close)
        close_lay.addLayout(form_row("在浮层上", self._close_sel, close_card))
        close_lay.addWidget(note(
            "双击 / 右键关闭浮层（左键拖动，互不打架）。", close_card
        ))
        lay.addWidget(close_card)

        # ── 3. 最多显示几条 ──
        rows_card, rows_lay = settings_section("最多显示几条", parent=self)
        self._rows_spin = NumberField(rows_card)
        self._rows_spin.setRange(0, MAX_ROWS_LIMIT)
        self._rows_spin.setSuffix(" 条")
        # 0 那一档说人话：「不限」比一个赤裸的 0 清楚（方案 §3.1 的上限是能关掉的）。
        self._rows_spin.setSpecialValueText("不限")
        self._rows_spin.setValue(plugin.get_max_rows())
        # `editingFinished` 而非 `valueChanged`：后者在打字途中每个中间数字都写一次盘
        # （同 ipad_tunnel 那处格子的口径）。
        self._rows_spin.editingFinished.connect(
            lambda: self._on_max_rows(self._rows_spin.value())
        )
        rows_lay.addLayout(form_row("浮层里", self._rows_spin, rows_card))
        rows_lay.addWidget(note(
            "同时命中好几条时，浮层只显示这么多行（超出的折成一行「还有 k 条」）；"
            "填 0 ＝ 不限。",
            rows_card,
        ))
        lay.addWidget(rows_card)

        # ── 4. 任务管理入口 ──
        # 这一格**不是配置**，是「那些配置在哪改」的那一步 —— 放进卡里是因为设置页里
        # 每一组控件都得有归属（卡外的裸行看不出它属于哪件事）。
        task_card, task_lay = settings_section("任务管理", parent=self)
        head = QHBoxLayout()
        head.setSpacing(6)
        self._manage_btn = tool_button("打开任务管理", "primary", parent=task_card)
        self._manage_btn.clicked.connect(self._on_manage)
        head.addWidget(self._manage_btn)
        head.addStretch(1)
        task_lay.addLayout(head)
        task_lay.addWidget(note(
            "任务的<b>增删改</b>在主窗体里（这枚按钮、点面板上那一行，进的都是<b>同一份</b>）"
            "—— 那里还能对单独一条预览效果、补记今天的完成。任务引用的变量在"
            "<b>设置 → 变量</b>（框架级那一页）：变量不属于某一条任务独享。",
            task_card,
        ))
        lay.addWidget(task_card)
        lay.addStretch(1)

    # ---------- 入口 ----------

    def _on_manage(self) -> None:
        """「打开任务管理」= 请求框架开**本组件**的详情窗（``Plugin.open_detail``）。

        这枚按钮**不持有任何界面**：内容面归框架 ``DetailHost``（按 plugin_id 单例）。
        故它开出来的与点面板整行进来的**是同一份** —— 一处改完另一处看得见，正是当初
        删掉 ``hosts_guard`` 那枚「管理条目…」按钮要防的那种漂移。

        返回 ``False`` 不是「出错」而是三种实情之一（框架没接线／组件没声明入口／
        注册中心里没这一条），界面上统一说一句实话即可 —— 这里没有可降级的东西可做。
        """
        if not self._plugin.open_detail():
            self._fb.warn("这个环境里暂时打不开任务管理窗（详见日志）。")

    # ---------- 三个写口 ----------

    def _on_size(self, value: str) -> None:
        if not self._plugin.set_size(value):
            self._fb.error("写回配置没成功——改动没存下，请重试。")
            return
        self._fb.info("浮层大小已保存。")

    def _on_close(self, value: str) -> None:
        if not self._plugin.set_close_gesture(value):
            self._fb.error("写回配置没成功——改动没存下，请重试。")
            return
        self._fb.info("关闭方式已保存。")

    def _on_max_rows(self, value: int) -> None:
        if not self._plugin.set_max_rows(int(value)):
            self._fb.error("写回配置没成功——改动没存下，请重试。")
            return
        self._fb.info(f"浮层最多显示 {int(value)} 条。" if value > 0 else "浮层不再截断行数。")


def build_page(plugin: "Plugin") -> QWidget:
    """构建设置页（框架按 ``SettingsPageSpec.build()`` 零参调用，故用闭包注入插件）。"""
    # 标题取 ``Plugin.name``（不手抄一份）：抄一份的下场是改名那天有一处不跟着变
    # —— 与 `bt_guard` / `audio_guard` 那两页同一个写法。
    page, layout = settings_page_shell(plugin.name or plugin.id)
    layout.addWidget(_TodoSettings(plugin, page), stretch=1)
    return page
