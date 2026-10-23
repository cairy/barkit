"""设置窗里的「网络切换」页（装置运维）：自动化总闸 / 恢复 DHCP 配置 / 无匹配时 /
helper 巡逻节律 / 装置运维 五张卡。

**场景配置不在这里**（设计 §4.2 落点表）：改场景归配置窗体（点面板整行进）。本页再放一份入口，就是给同一件事开第二个真源。

``settings.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QWidget,
)

from barkit.plugins.route_guard import rules
from barkit.shell.ui_kit import (
    ChoiceSelector,
    FeedbackBar,
    NumberField,
    Switch,
    check_field,
    note,
    settings_page_shell,
    settings_section,
    tool_button,
)

if TYPE_CHECKING:
    from barkit.plugins.route_guard.plugin import Plugin


def build_page(plugin: "Plugin") -> QWidget:
    """设置页（装置运维：自动化总闸 ＋ 恢复 DHCP预设 ＋ 无匹配时策略 ＋ helper 节律 ＋ 守护运维）。

    **场景配置不在这里**（设计 §4.2 落点表）：配置项是领域对象、改它的动作每次都要走
    一遍，归详情页（点面板整行）；设置页只留装置运维。这一页再放一份配置入口，就等于
    给同一件事开第二个真源（§4.4 第 3 条）。

    **自动化总闸在这一页顶部**（设计 §4.3 的 L3 草图第一行，第 7b 步落地）：它是
    **低频 + 破坏性**的决定（一关，IP 与路由都不再碰），所以不在面板上、也不在
    配置窗体里 —— 面板那一行只做状态回显（副标题写「已停用 · …」，徽标写「已停用」）。
    机制（``paused``）早就有了，缺的一直只是这处 UI 落点。

    **卡分组（2026-09-21 返工）**：五张 `settings_section` 卡 —— 自动化总闸 / 恢复 DHCP
    配置 / 无匹配时 / helper 巡逻节律 / 装置运维。此前这五组里只有「装置运维」是一块
    现成的容器，其余四组各有自己的小标题裸贴在页面底色上，靠前一条 `Divider` 划界
    （用户上报「没做到有效分组」）。卡标题承接原来那行小标题的名字，卡外的裸行随之消失。
    """
    page, layout = settings_page_shell("网络切换")

    # 行内反馈槽：本页所有动作的结果 / 二次确认都落在这里，**不开模态框**。
    # 槽位的生死由插件管（页面销毁时自动摘引用，见 `Plugin.attach_feedback`）。
    # **位置提到页首**（2026-09-21）：它原先挂在页面最底下，而本页现在可以很长
    # （设置窗可缩放，内容溢出即滚动）——「保存恢复 DHCP 配置」的回执写在页脚，
    # 用户在页首点了按钮什么也看不见。
    fb = FeedbackBar(page)
    plugin.attach_feedback(fb)
    layout.addWidget(fb)

    # ── 1. 自动化总闸（§4.3 第一行）──
    # 开关只发信号、不持回调（H3），业务仍在插件里。卡标题就是这枚开关的名字
    # （原来那行小标题「自动化」直接升成卡标题），故行内不再重复写一遍标签。
    auto_card, auto_lay = settings_section("自动化总闸", parent=page)
    gate = QHBoxLayout()
    gate.setSpacing(8)
    sw = Switch(auto_card)
    sw.setChecked(plugin.automation_active)
    sw.clicked.connect(plugin.toggle_automation)
    gate.addWidget(sw)
    gate.addStretch(1)
    auto_lay.addLayout(gate)
    # 关掉的代价必须写出来（§3.2「停用不撒谎」）：已生效的路由与网卡**都不会**被自动
    # 撤掉 —— 用户很容易以为关了开关就清干净了。两条出口各自指到真正的那一处：
    # 路由是面板那一行右侧的开关，网卡是下面的「恢复 DHCP 配置」（§4.7 三）。
    auto_lay.addWidget(note(
        "关掉后不碰 IP、也不碰路由，已生效的保持现状（不会自动回滚）。"
        "撤掉已叠加的路由：用面板那一行右侧的开关；网卡还原：用下方「恢复 DHCP」。",
        auto_card,
    ))
    layout.addWidget(auto_card)

    # ── 2. 「恢复 DHCP」（§4.7 二）──
    # 在这里勾选要重新取地址的网卡 + 是否连带 DNS，保存后，徽标下拉第一行的
    # 「恢复 DHCP」按这份配置下发命令。命令做三件事：交还给系统 DHCP（可选连 DNS
    # 一起还原）→ 等新租约 → 按**当前命中的场景**重新固化。**不碰任何场景配置**
    # （用户 2026-09-18 裁定）。没有场景要接管这张网卡（或自动化停着）时，交还完就停在
    # 自动获取 —— 「停用 = 保持现状」不该被一个按钮推翻。
    # 清单源 = 规则涉及的网卡（`rule_ifaces`）：场景还没命中过也能预先勾好。
    # 一格一个 `QCheckBox`（不用 QListWidget）：全局样式给 QListWidget 加了内边距，
    # 两行条目挤在默认高度里出不来，页面上只剩一个灰底空框（2026-09-18 用户实测）。
    hb_card, hb_lay = settings_section("恢复 DHCP 配置", parent=page)
    hb_ifaces = plugin.rule_ifaces()
    hb_boxes: "list[tuple[str, QCheckBox]]" = []
    if hb_ifaces:
        chosen = set(plugin.handback_ifaces())
        for name in hb_ifaces:
            box = check_field(name, checked=name in chosen, parent=hb_card)
            hb_boxes.append((name, box))
            hb_lay.addWidget(box)
    else:
        hb_lay.addWidget(note(
            "尚无规则涉及网卡：先在场景里填上「网卡」，此处才会出现可勾选项。",
            hb_card))
    dns = check_field("恢复 DNS（一并还原）", checked=plugin.handback_dns(), parent=hb_card)
    hb_lay.addWidget(dns)

    def _save_hb() -> None:
        picked = [name for name, box in hb_boxes if box.isChecked()]
        plugin.set_handback_config(picked, dns.isChecked())
    save_hb = tool_button("保存恢复 DHCP 配置", "pill-ghost", parent=hb_card)
    save_hb.clicked.connect(_save_hb)
    # 左对齐、不拉通栏：与「立即检查」「安装守护」同一形态（通栏那枚看起来像个
    # 输入框，用户会以为是可填的一格）。
    srow = QHBoxLayout()
    srow.addWidget(save_hb)
    srow.addStretch(1)
    hb_lay.addLayout(srow)
    hb_lay.addWidget(note(
        "勾选并保存后，面板徽标下拉第一行出现「恢复 DHCP」，按此处配置执行。",
        hb_card))
    layout.addWidget(hb_card)

    # ── 3. 「无匹配时」策略 ──
    fb_card, fb_lay = settings_section("无匹配时", parent=page)
    prow = QHBoxLayout()
    prow.setSpacing(8)
    sel = ChoiceSelector(
        [("保持现状（不碰网卡）", rules.FALLBACK_KEEP),
         ("恢复 DHCP（还原成自动获取）", rules.FALLBACK_DHCP)],
        plugin.fallback_policy(), parent=fb_card,
    )
    sel.selected.connect(plugin.set_fallback)
    prow.addWidget(sel, stretch=1)
    fb_lay.addLayout(prow)
    # 两个取值的代价都要写出来 —— 这一组是整页唯一一处「改了会影响**别的**场景」的设置。
    fb_lay.addWidget(note(
        "所有场景都没命中时怎么办。网卡上留着的，是上一次命中场景时写下的地址。"
        "「保持现状」：不动它（缺省，安全默认）。「恢复 DHCP」：自动改回自动获取 —— "
        "代价是离开网络时会多一次网卡重配，可能短暂断网。",
        fb_card,
    ))
    layout.addWidget(fb_card)

    # ── 4. helper 巡逻节律（`helper_poll`）──
    # 设计 §4.3 那六行里的「检查间隔」有一半落在这里。
    # 2026-09-19 判的「不排」否掉的是**只读展示行**（它要同时展示 `poll` 与 `helper_poll`，
    # 而 `poll` 组件读不得 —— D27 单消费者，见下方那段注释），**不是**这个可调项本身：
    # `helper_poll` 与框架节拍毫无关系，单独给一个可编辑控件不牵动 D27。
    # 取值域与 `rules.build_routes` 的校验共用同一份常量（不再各写一遍数字）。
    hp_card, hp_lay = settings_section("helper 巡逻节律", parent=page)
    hrow = QHBoxLayout()
    hrow.setSpacing(8)
    hspin = NumberField(hp_card)
    hspin.setRange(rules.MIN_HELPER_POLL, rules.MAX_HELPER_POLL)
    hspin.setSuffix(" 秒")
    hspin.setObjectName("helper_poll_spin")   # 测试据此定位；objectName 不用户可见
    hspin.setValue(plugin.helper_poll_seconds())
    # `editingFinished` 而非 `valueChanged`：后者在打字途中每个中间数字都写一次盘
    # （同 `ipad_tunnel` 的端口两格）。
    hspin.editingFinished.connect(lambda: plugin.set_helper_poll(hspin.value()))
    hrow.addWidget(hspin, stretch=1)
    hp_lay.addLayout(hrow)
    hp_lay.addWidget(note(
        "root helper 多久巡逻一轮路由表（它自己巡逻一轮的节律；要它现在跑一轮用「立即检查」）。"
        "面板刷新另有节拍、与此无关 —— 一个配置键只服务一个消费者。",
        hp_card,
    ))
    layout.addWidget(hp_card)

    # 设计 §4.3 设置页草图的六行，到本步**全部有了着落**（其中两行不是「做出来」，是「不排」）：
    #   - 「检查间隔」：**半落地**（2026-09-19）。那行的**只读展示**形态不排 —— 它要同时展示
    #     `poll`（框架节拍）与 `helper_poll`（helper 节律），而 `poll` 只服务框架调度器
    #     （D27 单消费者，组件读它会被 `test_component_does_not_read_framework_poll_key` 抓），
    #     半个展示行做不成；但 `helper_poll` 这个**可调项**本身已在上方给了 NumberField
    #     （它与框架节拍无关，不牵动 D27）。留下的是防抖那个**轮数**下拉（不是秒数），单位指去框架页。
    #   - 「[停止管理并恢复原状]」：**不排**（2026-09-19）。它出自 2026-09-13 的草图，而**草图当天
    #     已过期** —— 2026-09-18 把 `[交还系统]` 移出设置页后，「与它并列」这个前提就没了；
    #     且它要补的坑（§4.7 三「停用 ≠ 清干净」）已由上面那颗总闸 note ＋ 行级副标题兑现。
    #     剩下的只有「两步入 → 一步」的便利，无需求来源；真有需求时落点是总闸那一行旁边，不单列。
    # 两条判据都在 `docs/log/2026-09-18-设计缺口盘点.md`（该表已关条）。

    # ── 5. 装置运维**已不在这里**（2026-09-28）──
    # 「立即检查 / 安装守护 / 卸载守护 ＋ 那句守护警示」现在由**框架按声明注入**：
    # 本组件 `resources().helper_tasks` 非空 ⇒ 框架在本页**页尾框架区**画出第 ③ 张卡
    # （卡序契约：运行间隔 → 配置维护 → 装置运维）。三句文案与「装完刷什么」在
    # `plugin.helper_ops_texts()` / `on_helper_change()`，动作与接线在
    # `capabilities.helper_ops`（与 iPad 隧道 / hosts 托管共用一份实现）。
    # 搬走的原因：本页原先自己画一块，另两个组件也各画一块 ⇒ 三种长相，而 hosts 托管
    # 那一份写着写着漂成了「没有卸载键」—— 同一件事两个真源必然漂。

    layout.addStretch(1)
    return page
