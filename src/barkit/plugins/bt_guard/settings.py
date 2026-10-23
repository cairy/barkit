"""bt_guard/settings：设置面 —— 设置窗里的「蓝牙看护」页。

三件事：选一个要看的设备、开不开自动重连、修不好时弹不弹通知。

**为什么这一页必须自绘**：设备是「从系统已配对列表里挑一个」，页面得先读一次现场才能
把下拉填出来 —— 声明式渲染器至今未接线（``contribution/form.py`` 零消费方），
所以走 ``SettingsPageSpec.build``（同 ``audio_guard`` 那一页的理由）。

本模块的纪律（同 ``audio_guard/settings.py``）：

- **不碰 ``ctx`` / ``config``**：读走 ``plugin.device`` 之类的属性，写走
  ``plugin.set_xxx()`` —— 配置 IO 全在 ``plugin.py``（门禁 ``test_no_config_io_in_ui``）；
- **零模态框**：本页没有需要二次确认的动作（切开关、换设备都即时生效、随时可改回）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import QWidget

from barkit.plugins.bt_guard import bt_state
from barkit.shell.ui_kit import (
    ComboField,
    Switch,
    form_row,
    note,
    settings_page_shell,
    settings_section,
)

if TYPE_CHECKING:
    from barkit.plugins.bt_guard.plugin import Plugin

#: 设备下拉的最小宽度。取值依据：最长的项是「设备名 ＋ 全角空格 ＋ 地址」
#: （如 ``MX Master 3S　AA:BB:CC:DD:EE:FF``），13px 下约 220px，留一档余量。
#: 用 ``setMinimumWidth`` 而不是 ``setFixedWidth``：设备名有长有短，钉死宽度会把长名字
#: 截掉；给最小值则短名单至少不至于缩成一个看不出内容的小方块。
DEVICE_COMBO_MIN_W = 240


def _fill_devices(combo: ComboField, plugin: "Plugin") -> None:
    """把已配对设备填进下拉，并选中当前配置的那一个。

    **列表是打开这一页的那一刻读的**（一次 ``system_profiler``，亚秒级）。刚配对的新设备
    要关掉再打开这一页才会出现 —— 这比每帧去读一次系统现场划算得多。
    """
    combo.addItem("（不监控）", "")
    for addr, name in bt_state.paired_devices():
        combo.addItem(f"{name}　{bt_state.pretty(addr)}", addr)

    idx = combo.findData(plugin.device)
    if plugin.device and idx < 0:
        # 配置里那个设备已经不在配对列表里了（被删了 / 关掉了配对）。仍然把它列为可选项
        # 并选中 —— 否则用户一打开这页，配置就被下方的一次改动悄悄换成别的设备。
        combo.addItem(f"（已不在配对列表）{bt_state.pretty(plugin.device)}", plugin.device)
        idx = combo.count() - 1
    combo.setCurrentIndex(idx if idx >= 0 else 0)


def build_page(plugin: "Plugin") -> QWidget:
    """构建「蓝牙看护」设置页（``SettingsPageSpec.build`` 的落点）。"""
    page, layout = settings_page_shell(plugin.name or plugin.id)

    # ---------- 卡 1：要看的设备 ----------
    card, content = settings_section("设备", parent=page)
    combo = ComboField(card)
    combo.setMinimumWidth(DEVICE_COMBO_MIN_W)
    _fill_devices(combo, plugin)
    combo.currentIndexChanged.connect(
        lambda _i: plugin.set_device(combo.currentData() or ""))
    # ``control_fill=False``：下拉是**窄取值类**，拉满会撑成半屏（同「主图标」那一行）。
    content.addLayout(form_row("要看的设备", combo, card, control_fill=False))
    content.addWidget(note(
        "这只设备在<b>从睡眠中唤醒后掉线</b>时，本组件会重开一次蓝牙把它带回来。"
        "列表是打开这一页时读的，刚配对的设备要重开这一页才会出现。",
        card,
    ))
    layout.addWidget(card)      # 漏了这句卡会压在页标题上，见 audio_guard 的同款注释

    # ---------- 卡 2：自动化 ----------
    card2, content2 = settings_section("自动化", parent=page)

    sw_auto = Switch(card2)
    sw_auto.setChecked(plugin.auto_reconnect)

    def _on_auto() -> None:
        plugin.set_auto_reconnect(not sw_auto.isChecked())
        sw_auto.setChecked(plugin.auto_reconnect)

    sw_auto.clicked.connect(_on_auto)
    content2.addLayout(form_row("睡醒后掉线时自动重开蓝牙", sw_auto, card2, control_fill=False))
    content2.addWidget(note(
        "关掉后<b>仍然判定与显示</b>，只是不动手。"
        "重开会把当前已连的蓝牙设备一并断一次（它们通常几秒内自己回来），这是它唯一的副作用。",
        card2,
    ))

    sw_notify = Switch(card2)
    sw_notify.setChecked(plugin.notify_enabled)

    def _on_notify() -> None:
        plugin.set_notify_enabled(not sw_notify.isChecked())
        sw_notify.setChecked(plugin.notify_enabled)

    sw_notify.clicked.connect(_on_notify)
    content2.addLayout(form_row("修不好时弹系统通知", sw_notify, card2, control_fill=False))
    content2.addWidget(note(
        "只在「重开过了、设备还是没回来」时弹一条。"
        "自动修好不打扰 —— 你要的是鼠标能用，不是一条「我修好了」的通知。",
        card2,
    ))
    layout.addWidget(card2)

    layout.addStretch(1)
    return page
