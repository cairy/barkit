"""hosts_guard/settings：设置面 —— 设置窗里的「hosts 托管」页。

**为什么单独一个文件**（§7.2：设置面的全部界面归 `settings.py`）：本页原先内联在
`plugin.py::_build_settings_page`，于是那个模块被逼着**运行期** import PySide6
（`from barkit.shell.ui_kit import FeedbackBar, note, settings_page_shell, tool_button`）
—— 那正是 `PLUGIN_PYSIDE6_WHITELIST` 里本组件的存量条目。2026-09-19 迁到这里，
界面与配置 IO 分家：`plugin.py` 回到「零 PySide6（除 TYPE_CHECKING 标注）」，
白名单条目随之撤销。

**顺带补 `resolve_poll` 的控件**：旧版这块只有一句 note ——「可在配置
plugins.hosts_guard.resolve_poll 调整，最小 60」，等于**明示用户去手改 YAML**。
同为「数字型装置参数」的 `ipad_tunnel` 端口有输入格（`ui_kit.NumberField`），
同类参数一个给控件、一个让手编文件就是风格不统一 ⇒ 这里补上。

纪律（同 `ipad_tunnel/settings.py` / `route_guard/settings.py`）：

- **不碰 `ctx` / `config`**：读走 `plugin.resolve_poll`，写走 `plugin.save_resolve_poll()`
  （门禁 `test_no_config_io_in_ui`）。装置运维那几枚按钮（立即检查 / 安装守护 /
  卸载守护）自 2026-09-28 起**不由本文件画** —— 框架按声明注入到页尾框架区，
  动作与文案在 `capabilities.helper_ops`；
- **不放「管理条目」启动键**（2026-09-20 删）：管理面**只有一个入口** —— 面板点本组件
  整行，由框架 `DetailHost` 托管。设置页管**配置**，任务流归**详情卡**
  （`route_guard` 同款）。
  ◷ 2026-10-05：当初的另一半理由（「插件主动打开自己详情页」的接口还没有）**已不成立**
  —— `Plugin.open_detail` 已落地，且 `DetailHost` 按 plugin_id 复用同一份内容面，
  再放一枚按钮也不会开出第二份。今天不放它是**边界**问题，不是技术做不到；
- **改一次落一次盘、没有「保存」键**：数字格用 `NumberField`（滚轮不改值），落盘时机
  接 `editingFinished` —— 打字途中的中间数字不落盘（端口是 1..65535，中间值可能非法）；
- **零模态框**：一切反馈走行内 `FeedbackBar`；
- 外壳统一用 `shell.ui_kit` 的 `settings_page_shell` / `settings_section` / `FeedbackBar` /
  `note`；
- 文字样式一律经 `ui_kit` 的构造口（`form_label` / `note`…）落到 `apply_text_role`
  的角色表上，**本文件不自拼 QSS**（门禁 `test_no_inline_stylesheet` 守）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import QWidget

from barkit.plugins.hosts_guard.models import DEFAULT_RESOLVE_POLL, MIN_RESOLVE_POLL
from barkit.shell.ui_kit import (
    FeedbackBar,
    NumberField,
    form_row,
    note,
    settings_page_shell,
    settings_section,
)

if TYPE_CHECKING:
    from barkit.plugins.hosts_guard.plugin import Plugin

#: 「自动更新间隔」的上限：一天。纯经验值 —— 再长就不如关掉托管块了。
MAX_RESOLVE_POLL = 86400


def build_page(plugin: "Plugin") -> QWidget:
    """设置页（框架按 ``SettingsPageSpec.build()`` **零参**调用，故插件侧用闭包注入）。

    两张分区卡：「守护状态」（只读状态 ＋ 动作按钮）与「自动更新间隔」（一个数字格）。
    分组是 2026-09-21 的返工口径 —— 设置页里**每一组控件都长在一张卡里**，卡外不留裸行
    （原先的状态行与按钮直接贴在页面底色上，看不出哪几件是一件事）。
    """
    page, layout = settings_page_shell("hosts 托管")

    # 行内反馈槽：本页动作（安装守护 / 立即检查 / 保存节律）的结果都落在这里，
    # **不开模态框**。槽位的生死由插件管（页面销毁时自动摘引用，见 attach_feedback）。
    fb = FeedbackBar(page)
    plugin.attach_feedback(fb)
    layout.addWidget(fb)

    layout.addWidget(note(
        "维护 /etc/hosts 里的 BarKit 托管块：让指定域名绕开隧道的虚拟地址、解析到真实地址，"
        "并可跟随 DDNS 自动更新。写入 /etc/hosts 需要系统守护（root）。",
        page,
    ))

    # ── 1. 装置运维**已不在这里**（2026-09-28）──
    # 「立即检查 / 安装守护 / 卸载守护 ＋ 那句守护警示」原先长在**本页页首**：本组件是
    # 三个用 root helper 的组件里唯一没接上共用块的那个（按钮组成自搓、只有「安装 / 重装」
    # 而没有卸载）。现在由**框架按声明注入**到本页**页尾框架区**的第 ③ 张卡；文案在
    # `plugin.helper_notice()` / `helper_ops_texts()`，动作与接线在 `capabilities.helper_ops`。
    # ⚠️ 对用户可见的变更两处：① 「卸载守护」这枚键是**新出现**的；② 常态那行
    # 「状态：守护已安装 v… · 运行中」不再显示 —— 那份信息归页首状态机段的「当前」栏
    # 与面板那一行（框架的警示区是**有问题才喊**，不承担常态展示）。

    # ── 2. 自动更新（DNS 重新解析）间隔 ──
    poll_card, poll_lay = settings_section("自动更新间隔", parent=page)
    spin = NumberField(poll_card)
    spin.setRange(MIN_RESOLVE_POLL, MAX_RESOLVE_POLL)
    spin.setSuffix(" 秒")
    spin.setValue(int(plugin.resolve_poll))
    # `editingFinished` 而非 `valueChanged`：后者在打字途中每个中间数字都写一次盘
    # （同 `ipad_tunnel` 的端口两格）。
    spin.editingFinished.connect(lambda: plugin.save_resolve_poll(spin.value()))
    poll_lay.addLayout(form_row("重查间隔", spin, poll_card))

    poll_lay.addWidget(note(
        f"多久重查一次 DDNS 域名的真实地址（下限 {MIN_RESOLVE_POLL} 秒，默认 "
        f"{DEFAULT_RESOLVE_POLL}）。解析顺序：DoH(doh.pub) → DoH(阿里) → "
        "UDP 直查(阿里/腾讯)；落在隧道虚拟地址段 100.64.0.0/10 的结果一律拒绝写入。",
        poll_card,
    ))
    layout.addWidget(poll_card)

    layout.addStretch(1)
    return page
