"""``_PluginsMixin`` —— 「插件」页：列表行、显示位芯片、清单查询。

``settings.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from barkit.core.config import (
    PLUGIN_UI_SLOTS,
    slot_default_visible,
)
from barkit.core.plugin import RUN_ON_DEMAND
from barkit.core.plugin_manager import (
    PluginInfo,
    version_label,
)
from barkit.shell.order_editor import OrderEditor
from barkit.shell.ui_kit import (
    Card,
    FeedbackBar,
    Switch,
    apply_text_role,
    tool_button,
)
from barkit.shell.design import metrics

from .constants import _SLOT_LABELS, log
from .helpers import _apply_chip, _wire_button


class _PluginsMixin:
    """「插件」页：列表行、显示位芯片、清单查询。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    def _page_plugins(self) -> QWidget:
        """插件管理（E3 ②）：列出所有插件，运行时启用 / 禁用 / 删除外部插件。

        不再需要手动复制 / 移动文件夹或改配置后重启——开关即时生效，
        外部插件可直接在此卸载（删目录 + 删配置段）。

        排序由框架提供的 ``OrderEditor`` 托管（规范 §5.5）：本窗只负责
        渲染每一行的内容（``_build_plugin_row``），拖拽 / 键盘微调 / 落库 / 回滚
        全在编辑器内，顺序与「标题栏图标条 / 状态行」同源（``plugin_order``）。
        """
        page, layout = self._page_shell("插件")
        # 不给排序加说明文字：窗口高度紧张（见下方滚动区注释），手柄自带
        # tooltip「拖拽调整顺序（聚焦后 Alt+↑/↓ 微调）」，提示不占版面。
        self._order_editor = OrderEditor(
            build_row=self._build_plugin_row,
            on_reorder=self._on_plugin_reorder,
        )
        self._order_editor.set_row_spacing(8)
        self._plugin_infos = {}
        # 设置窗主区已统一包进滚动区（见 ``_build_ui``），整页可滚；列表直接占满
        # 内容高度，不再另设独立滚动区（原 380px 窗口裁掉排序手柄的问题由页级滚动解决）。
        layout.addWidget(self._order_editor, stretch=1)

        # 行内反馈槽（零模态纪律的落点）：安装结果与覆盖确认都落在这一条里。
        # 挂在按钮上方 —— 点了按钮，消息就出现在手边，不必回头找。
        fb = FeedbackBar(page)
        fb.destroyed.connect(lambda *_a, _fb=fb: self._forget_plugin_feedback(_fb))
        self._plugin_feedback = fb
        layout.addWidget(fb)

        # 安装入口（决议 D5）：外部插件此前只能手动拷进插件目录。目录与压缩包各一个
        # 按钮 —— 原生选择器不能同时收「文件」与「目录」，合成一个按钮就得再问一次
        # 「你给的是哪种」，多一层无从判断的猜测。三个按钮排一行：窗口只有 560 宽，
        # 一行放得下就不占列表高度（列表本来就装不下全部插件，见上方滚动区注释）。
        act_row = QHBoxLayout()
        dir_btn = tool_button("从目录安装…", "pill-ghost")
        _wire_button(dir_btn, self._on_plugin_install_dir, self._say_action)
        zip_btn = tool_button("从压缩包安装…", "pill-ghost")
        _wire_button(zip_btn, self._on_plugin_install_zip, self._say_action)
        # 数据恢复入口（§9.1：卸载 → 重装 → 恢复数据 往返的「恢复」端）。
        # 注意语义：import_zip 把导出包恢复到**已安装**的插件（按 manifest.id 定位），
        # 插件未安装时直接报错 —— 它与上面两个按钮是**两件事**（一个装代码、一个灌
        # 数据），故文案必须写「数据」而不是「插件包」。
        import_btn = tool_button("恢复插件数据…", "pill-ghost")
        _wire_button(import_btn, self._on_plugin_import, self._say_action)
        for btn in (dir_btn, zip_btn, import_btn):
            act_row.addWidget(btn)
        act_row.addStretch(1)
        layout.addLayout(act_row)
        self._rebuild_plugin_list()
        return page

    def _forget_plugin_feedback(self, fb: FeedbackBar) -> None:
        """槽位所属页面被销毁时摘掉引用，避免戳已删的 C++ 对象。

        只在「当前引用的正是刚死掉的那个」时清空 —— 重建时新页的槽位可能**已经先
        赋好值**（与各组件同款写法，见 hosts_guard 的同名方法）。
        """
        if self._plugin_feedback is fb:
            self._plugin_feedback = None

    def _rebuild_plugin_list(self) -> None:
        """按生效顺序重建插件行（每次打开设置时调用，反映最新运行期状态）。

        顺序取自 ``PluginManager.effective_order()``（= config ``plugin_order``
        用户排序 + 未列入者按 ``Plugin.order`` 追加），与标题栏图标条 / 状态行
        同源；行内容来自 ``list_plugins()``，两者按 id 对齐。
        """
        editor = getattr(self, "_order_editor", None)
        if editor is None or self._manager is None:
            return
        infos = {info.id: info for info in self._manager.list_plugins()}
        self._plugin_infos = infos
        self._slots_by_plugin = self._collect_slots()
        order = self._plugin_order_ids(infos)
        editor.set_items(order)

    def _collect_slots(self) -> dict[str, list[str]]:
        """各组件**真正声明了**的显示位：``{plugin_id: [slot, ...]}``。

        只列它真有的位 —— 一个没声明 ``StatusRowSpec`` 的组件（如「识别」只在标题栏
        出现）不该长出「列表行」开关：关掉一个本来就不存在的东西，用户只会以为开关坏了。
        判据直接读注册中心**已收集**的声明，不在本处另立一份「谁知道哪些显示位」的表 ——
        它一旦与收集层分叉，就会出现「设置窗有开关、面板上却没有这一位」。

        轻量桩缺某个门面时按「没有该显示位」处理（与 ``_plugin_order_ids`` 同款防御）。
        """
        if self._registry is None:
            return {}
        found: dict[str, set[str]] = {}
        # (slot, 注册中心门面, 该门面里的可选筛选)。同一显示位可以由**两个**声明点
        # 供给：「快捷区域」既可能是 `ActionSpec`，也可能是 `DetailEntrySpec`
        # —— 契约 §2.4 两行都写着，用户看到的是同一个位置，故两处都要收集。
        # `DetailEntrySpec` 2026-09-22 起**不再分 trigger**（行与快捷区域是同一个
        # 过程、两个入口都归用户层开关管），故它整体计入「快捷区域」这一位；
        # 「列表行」那一位的准入也变了 —— 行由框架为**每个组件**合成，所以每个组件
        # 都有这枚芯片（`status_rows()` 里合成的朴素行同样算）。
        # 「标题栏」这一位多一道**作者层**门槛：`ComponentSpec.in_header=False` 的组件
        # 声明自己不上标题栏（panel.py 渲染那格时直接按 `in_header` 排除），故设置窗
        # 也不该给它出「标题栏」芯片 —— 否则用户会看到一颗能点的开关、点了却毫无反应
        # （实际那一格本就不存在）。这与「用户层可隐藏」是**两件事**：前者是作者事后
        # 声明「这个组件根本没有标题栏位」，后者是组件有该位、用户选择不显示。
        sources = (
            ("titlebar", "components", lambda s: s.in_header),
            ("row", "status_rows", None),
            ("action", "primary_actions", None),
            ("action", "detail_entries", None),
            ("tray", "states", None),
        )
        for slot, getter_name, keep in sources:
            getter = getattr(self._registry, getter_name, None)
            if not callable(getter):
                continue
            try:
                specs = getter()
            except Exception:  # noqa: BLE001
                log.exception("读取 %s 贡献失败，该显示位不出开关", getter_name)
                continue
            for spec in specs:
                if keep is not None and not keep(spec):
                    continue
                found.setdefault(spec.plugin_id, set()).add(slot)
        # 顺序取契约里的固定顺序（PLUGIN_UI_SLOTS），不按收集顺序
        return {
            pid: [s for s in PLUGIN_UI_SLOTS if s in slots]
            for pid, slots in found.items()
        }

    def _plugin_order_ids(self, infos: dict) -> list[str]:
        """生效顺序里「有对应列表项」的 id；缺 ``effective_order`` 时退回列表序。

        防御性：设置窗单测用轻量 manager 桩，可能只实现 ``list_plugins()``。
        """
        getter = getattr(self._manager, "effective_order", None)
        order: list[str] = []
        if callable(getter):
            try:
                order = [pid for pid in getter() if pid in infos]
            except Exception:  # noqa: BLE001
                log.exception("读取插件生效顺序失败，退回列表顺序")
                order = []
        if not order:
            order = list(infos)
        # 兜底：生效顺序漏掉的 id 追加在后（不丢行）
        order += [pid for pid in infos if pid not in order]
        return order

    def _build_plugin_row(self, plugin_id: str, handle: QWidget) -> QWidget:
        """渲染一行插件（``OrderEditor.build_row`` 契约）。

        ``handle`` 是框架给的拖拽手柄，必须放进本行，否则该行不可拖拽；手柄
        独占自身 mousePress，故行内开关 / 按钮照常响应，不会被拖拽抢事件。
        """
        info = getattr(self, "_plugin_infos", {}).get(plugin_id)
        if info is None:
            # 防御：顺序与列表项不同步时仍渲染一行（不可缺行），且手柄必须进行内，
            # 否则会被 Qt 当成无父窗口的游离控件。
            info = PluginInfo(
                id=plugin_id, name=plugin_id, run_mode=RUN_ON_DEMAND,
                source="internal", enabled=True, loaded=False,
            )
        row = Card(radius=metrics.RADIUS_MD, parent=self)
        h = QHBoxLayout(row)
        h.setContentsMargins(*metrics.PAD_CARD)
        h.setSpacing(10)
        h.addWidget(handle)  # 行首拖拽手柄（框架提供）

        # 左：名称 + 副行（id · 来源 · 契约版本）
        left = QVBoxLayout()
        left.setSpacing(2)
        title = QLabel(info.name or info.id)
        apply_text_role(title, "section")
        origin = "外部" if info.source == "external" else "内置"
        sub = QLabel(f"{info.id} · {origin} · 契约 v{info.api_version}")
        sub.setObjectName("secondary")
        apply_text_role(sub, "caption")
        # 组件版本（决议 D5）走**悬停提示**而不是塞进副行：这一行里已经挤了手柄 / 名称 /
        # 副行 / 开关 / 导出 / 删除六样，而「用哪个版本覆盖哪个版本」真正被用到的时刻是
        # 覆盖提示（那里会把新旧版本都写出来）。要不要常显在行上，属真机目测项。
        if info.source == "external":
            sub.setToolTip(f"组件版本：{version_label(info.version)}")
        left.addWidget(title)
        left.addWidget(sub)
        if info.problem:
            # 「插件问题」展示（§9.2 isolate）：拒绝/装配失败/贡献异常的人类可读原因
            warn = QLabel(f"问题：{info.problem}")
            warn.setWordWrap(True)
            apply_text_role(warn, "caption_warn")
            left.addWidget(warn)
        chips = self._build_slot_chips(info.id)
        if chips is not None:
            left.addWidget(chips)
        h.addLayout(left, stretch=1)

        # 右：启用开关（所有插件）+ 删除/导出按钮（仅外部）。
        # 排序不再是行内按钮（§6.4）：改由行首手柄拖拽 / Alt+↑↓（框架编辑器托管）。
        # 注意 Switch 只有 clicked 信号且不自动翻转自身状态，故在处理器里
        # 用 not sw.isChecked() 推导新目标态，操作成功后再 setChecked 同步。
        sw = Switch()
        sw.setChecked(info.enabled)
        _wire_button(
            sw,
            lambda *_, pid=info.id, s=sw: self._on_plugin_toggle(pid, s),
            self._say_action,
        )
        h.addWidget(sw)
        if info.source == "external":
            exp_btn = tool_button("导出", "pill-ghost", size="inline")
            _wire_button(
                exp_btn,
                lambda *_, pid=info.id: self._on_plugin_export(pid),
                self._say_action,
            )
            h.addWidget(exp_btn)
            del_btn = tool_button("删除", "pill-ghost", size="inline")
            _wire_button(
                del_btn,
                lambda *_, pid=info.id: self._on_plugin_uninstall(pid),
                self._say_action,
            )
            h.addWidget(del_btn)
        return row

    def _build_slot_chips(self, plugin_id: str) -> "QWidget | None":
        """一行显示位芯片（契约规范 §2.4 的用户层，决议 D4）。

        只画该组件**真有的**显示位；一个也没有（轻量桩 / 未注入贡献）时返回 ``None``，
        调用方不占版面。芯片是二元开关：亮 ＝ 显示中，点一下隐藏，再点恢复。

        为什么是就地小芯片而不是下拉或折叠：这是**低频一次性偏好**，但用户想改的时候
        必须能一眼看到「这位现在是什么状态」—— 折叠起来就要多点一次才看得见，而这里
        总共最多四枚，摊开比折叠省事。
        """
        slots = self._slots_by_plugin.get(plugin_id, [])
        if not slots:
            return None
        box = QWidget(self)
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        for slot in slots:
            # 没有配置源（轻量桩 / 未注入）时按**该位的默认**画 —— 默认逐位不同
            # （快捷区域默认关，决议 D37），这里若一律填 ``True`` 就会画出一枚
            # 与实际行为相反亮着的芯片。
            visible = (
                self._config.plugin_ui_visible(plugin_id, slot)
                if self._config else slot_default_visible(slot)
            )
            btn = tool_button(_SLOT_LABELS[slot], "pill-chip", size="chip", parent=box)
            _apply_chip(btn, slot, visible)
            _wire_button(
                btn,
                lambda *_, pid=plugin_id, s=slot, b=btn: self._on_slot_chip(pid, s, b),
                self._say_action,
            )
            row.addWidget(btn)
        row.addStretch(1)
        return box

    def _plugin_info(self, plugin_id: str) -> "PluginInfo | None":
        """现问 manager 要某个组件的最新 ``PluginInfo``（**不吃页序**）。

        框架区是在**组件页构建时刻**画的。早先这类信息只读 ``self._plugin_infos``
        （只在「插件」页构建时才填），能跑通纯属页序侥幸 —— 页序一改（或某个组件页
        先建）就静默拿不到清单、框架区整块消失。故这里现问一次。
        """
        if self._manager is None:
            return None
        try:
            for info in self._manager.list_plugins():
                if info.id == plugin_id:
                    return info
        except Exception:  # noqa: BLE001
            log.exception("读取组件 %s 的清单失败，本次不画框架区", plugin_id)
        return None
