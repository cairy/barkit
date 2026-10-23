"""audio_guard/settings：设置面 —— 设置窗里的「音频看护」页。

本组件只有一个可配置项（``notify_enabled``），但它**必须有个地方能改**：系统通知会弹到
屏幕上、还进通知中心，属打扰性功能。《插件结构规范-UI面》§7.2 规定设置类 UI 一律收进
``settings.py``，故页面在这里、不在 ``plugin.py``（那会挂上 PySide6 门禁）。

「配置项被代码真读、用户却只能手改 YAML」正是「截图识别」被转正时修过的同款缺陷
（见 `docs/log/2026-09-19-recognize转正.md`），本组件不重犯。

**「现在什么情况」不在这页里**：框架在页首注入状态机段（当前取值、优先级排序），
本页只放配置。同一件事留两个显示位就会各自漂移。

本模块的纪律（同 `notify_guard/settings.py` / `recognize/settings.py`）：

- **不碰 ``ctx`` / ``config``**：读走 ``plugin.notify_enabled``、写走
  ``plugin.set_notify_enabled()`` —— 配置 IO 全在 ``plugin.py``（门禁
  ``test_no_config_io_in_ui``）；
- **零模态框**：一个开关没有二次确认的必要，本页不弹任何框。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import QWidget

from barkit.shell.ui_kit import (
    Switch,
    form_row,
    note,
    settings_page_shell,
    settings_section,
)

if TYPE_CHECKING:
    from barkit.plugins.audio_guard.plugin import Plugin


def build_page(plugin: "Plugin") -> QWidget:
    """构建「音频看护」设置页（``SettingsPageSpec.build`` 的落点）。"""
    page, layout = settings_page_shell(plugin.name or plugin.id)

    card, content = settings_section("通知", parent=page)
    sw = Switch(card)
    sw.setChecked(plugin.notify_enabled)

    def _on_click() -> None:
        # ``Switch.clicked`` **不翻转自身**（与 ``setChecked`` 解耦，见 ``ui_kit.Switch``
        # 的 docstring），故这里自己推目标态；写盘后再从插件**回读真实值**同步 —— 写盘
        # 失败时开关弹回原位，而不是留在一个骗人的位置。
        plugin.set_notify_enabled(not sw.isChecked())
        sw.setChecked(plugin.notify_enabled)

    sw.clicked.connect(_on_click)
    # ``control_fill=False``：开关是**窄控件**，拉满会撑成半屏。此时余量给标签
    # （默认那条会把标签钉死在 ``LABEL_W=78`` 上，而本行标签是 9 个字 ⇒ 静默折成
    # 两行「出问题时弹系 / 统通知」）。框架自己画的那几行也是这么写的。
    content.addLayout(form_row("出问题时弹系统通知", sw, card, control_fill=False))
    content.addWidget(note(
        # 强调走 ``<b>``（QLabel 的富文本）：本仓 QLabel **不认 markdown**，
        # 写成 ``**仍然检测**`` 会把星号原样画出来。
        "关掉后<b>仍然检测</b>、面板那一行照常显示状态，只是不再弹通知。"
        "通知里那几枚按钮（把声音切回耳机 / 取消静音）也随之一并停用。",
        card,
    ))
    # **必须显式把卡挂进页面布局**：``settings_section`` 只负责造卡并把 ``card``
    # 认作父，**不会**自己入页（同 ``recognize`` / ``notify_guard`` 的写法）。漏掉这一句，
    # 卡就停在 Qt 的默认几何 ``(0,0,100x30)`` 上——它仍然可见、仍然画，只是**压在页标题
    # 那一片上**，看起来是「标题区糊了一团看不清的东西」（2026-09-22 用户上报）。
    # 门禁 ``test_settings_page_has_no_orphan_widgets`` 守这一条。
    layout.addWidget(card)

    layout.addStretch(1)
    return page
