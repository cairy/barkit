"""``_FrameworkMixin`` —— 组件页页尾「框架区」：运行间隔 / 配置维护 / 装置运维与行内反馈。

``settings.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

from barkit.shell.daemon_ops import build_daemon_ops
from barkit.shell.ui_kit import (
    ChoiceSelector,
    FeedbackBar,
    form_row,
    note,
    settings_section,
    tool_button,
)
from barkit.shell.design import metrics

from .constants import log
from .helpers import _poll_choices, _poll_text, _wire_button


class _FrameworkMixin:
    """组件页页尾「框架区」：运行间隔 / 配置维护 / 装置运维与行内反馈。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 组件设置页 · 页尾「框架区」（框架按声明给组件画的东西） ----------
    #
    # 位置口径（第二批立，全仓唯一）：框架画的东西**只许**落在组件页的**页首总览**
    # （状态机段）与**页尾框架区**（本段）两头。页尾区固定卡序 —— 运行间隔 → 配置维护
    # →（第三批）装置运维 —— 准入各自独立 ⇒ **卡可缺、序不变**。
    #
    # 「数据与诊断」**不在这里**（2026-09-22 撤）：它把「全局的数据目录」与「组件自己的
    # 诊断」捆成一卡，于是前者被复制进了每个组件页。前者归「维护」页，后者归组件自己
    # 那张卡（见 ``shell/data_ops``）。
    #
    # 为什么这两样不在「插件」页：那一页管的是**插件集合**（装卸 / 启用 / 排序），却塞着
    # 某个组件自己的运行参数与不可逆动作；用户打开那个组件自己的页时反而看不见它们。
    # 唯一留下的是**显示位芯片** —— 四枚格子只有在这一页才横向比得出来（谁占了哪几格），
    # 而节拍与清配置是单个组件的事，在管理列表里看不出任何别的东西。

    def _build_framework_zone(self, plugin_id: str) -> "QWidget | None":
        """组件设置页**页尾**那一区：框架按声明画的东西。**一张卡都没有 ⇒ 返回 ``None``**。

        为什么空区不出：非周期型 ＋ 没改过配置的组件（多数按需组件就是）在框架区里
        一格都没有 —— 出个空壳只会让它的页被套上一层本不需要的外壳（页标题、页边距
        都跟着来），换不到任何东西。位置标准靠 ``insert_page_tail`` 守，不靠「每页都
        得摆个空盒子」。

        区里自带**一条反馈槽**：「恢复默认配置」要先弹行内确认问句，而这一页原有的槽
        （若有）在页首 —— 长页（网络切换约 915px）里用户在页尾点了按钮**看不到问句**，
        正是零模态要防的「点了没反应」。触发按钮在哪一区，反馈就落在哪一区
        （口径由「一页一条」修订为「**一区一条**」）。本槽供「运行间隔 / 恢复默认配置」
        两卡用；**装置运维卡不借它** —— 那张卡的反馈收在自己卡内（见
        ``_build_helper_ops_card``），触发它的按钮就在卡里。

        返回的区带一个**就地重算**入口（登记进 ``_zone_refresh``）：清掉配置后那张卡
        要当场消失（卡片在不在本身就是「还有没有改动」的显示位），而清卡只动这一区、
        不重建整页。
        """
        info = self._plugin_info(plugin_id)
        if info is None:
            return None
        # 反馈槽**先建**：页尾区里「运行间隔 / 恢复默认配置」两卡的反馈（含二次确认）
        # 经 ``_zone_say`` 落这一条；先于 ``make_cards()`` 建出来，免得那两卡构建时要它
        # 却拿到 ``None``。（装置运维卡的反馈自带卡内槽、不经这条，见 ``_build_helper_ops_card``。）
        fb = FeedbackBar(self)
        self._zone_feedback[plugin_id] = fb
        builders = (
            self._build_interval_card,
            self._build_maintenance_card,
            self._build_helper_ops_card,
        )

        def make_cards() -> list[QWidget]:
            return [c for c in (b(info) for b in builders) if c is not None]

        made = make_cards()
        if not made:
            # 一张卡都没有 ⇒ 区不出（连槽一起撤：不给这一页留个没人用的孤儿槽）
            self._zone_feedback.pop(plugin_id, None)
            fb.deleteLater()
            return None
        box = QWidget(self)
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(metrics.GAP_LG)
        holder = QWidget(box)
        holder_col = QVBoxLayout(holder)
        holder_col.setContentsMargins(0, 0, 0, 0)
        holder_col.setSpacing(metrics.GAP_LG)
        for card in made:
            holder_col.addWidget(card)
        col.addWidget(holder)
        col.addWidget(fb)

        def refresh() -> None:
            """按最新配置重算这一区的卡片（槽位与位置不动，故已有消息不会丢）。"""
            while holder_col.count():
                item = holder_col.takeAt(0)
                old = item.widget() if item is not None else None
                if old is not None:
                    old.setParent(None)
                    old.deleteLater()
            fresh = make_cards()
            for card in fresh:
                holder_col.addWidget(card)
            holder.setVisible(bool(fresh))

        self._zone_refresh[plugin_id] = refresh
        return box

    def _refresh_zone(self, plugin_id: str) -> None:
        """让某组件页的框架区按最新配置重算（页面不在 / 没有区时什么都不做）。"""
        cb = self._zone_refresh.get(plugin_id)
        if cb is not None:
            cb()

    def _interval_card_admitted(self, info) -> bool:
        """「运行间隔」卡的准入：**周期型**组件才有节拍可言（按需型不长这张卡）。

        抽出来由画卡处与 :meth:`_framework_page_content` 共用 —— 两处各判一次就会出现
        「说得出有内容、画出来却是空区」的分叉。
        """
        return (
            getattr(info, "poll_default", None) is not None
            and self._manager is not None
        )

    def _build_interval_card(self, info) -> "QWidget | None":
        """「运行间隔」卡（契约 D21）：**框架按组件声明自动生成**，组件不写。

        准入 = 该组件是周期型（``PluginInfo.poll_default`` 非空）；按需型没有节拍可言，
        不长这张卡。界面上写「框架检查间隔」而不是契约里的「框架节拍」——「节拍」是框架
        内部对 ``tick()`` 周期的叫法，用户看不懂。

        候选与当前项都由框架算：默认值问 ``poll_default``（＝组件建议／框架兜底），
        用户改过的值问 ``manager.plugin_poll``（读用户差异层，只有它答得出「改没改过」）。

        **文案必须与组件自己的节律划清界线**：搬到组件页之后，网络切换的「helper 巡逻
        节律」、hosts 托管的「自动更新间隔」会与这行**同页相邻**，不写清楚用户必然问
        「为什么有两个间隔」。
        """
        if not self._interval_card_admitted(info):
            return None
        default = getattr(info, "poll_default", None)
        card, content = settings_section("运行间隔", self)
        read_poll = getattr(self._manager, "plugin_poll", None)
        override = read_poll(info.id) if callable(read_poll) else None
        sel = ChoiceSelector(
            _poll_choices(override, default),
            "" if override is None else f"{override:g}",
            parent=card,
        )
        # 悬停提示兜住三件推不出来的事：这个数管什么、默认是多少、改完什么时候生效
        parts = [f"框架多久叫一次这个组件（组件建议 {_poll_text(default)}）"]
        if override is not None:
            parts.append(f"当前 {_poll_text(override)}是你改过的，选「默认」即恢复")
        parts.append("改动立即生效，不必重启")
        sel.setToolTip("；".join(parts) + "。")
        content.addLayout(form_row("框架检查间隔", sel, card, control_fill=False))
        content.addWidget(
            note("框架每隔这么久叫一次这个组件干活；组件自己那套节律见上面的卡。")
        )
        # 回调绑本卡自己的那组参数（每卡各建一个闭包，不共享循环变量）
        sel.selected.connect(
            lambda data, pid=info.id, d=float(default), nm=info.name or info.id: (
                self._on_plugin_poll(pid, data, d, nm)
            )
        )
        return card

    def _clearable_keys(self, plugin_id: str) -> list[str]:
        """问配置层「这个组件有哪些可清的键」；桩没这个门面时空列表。

        设置窗的用例大量使用轻量桩，桩缺某个门面时应当**降级**而不是崩（与
        ``_plugin_order_ids`` 同款防御）；空列表的语义就是「没有可清的东西」，
        于是按钮不出现、点了也只会说「本来就是默认」。
        """
        read = getattr(self._config, "clearable_plugin_keys", None)
        if not callable(read):
            return []
        try:
            return list(read(plugin_id))
        except Exception:  # noqa: BLE001
            log.exception("读取插件 %s 可清配置键失败", plugin_id)
            return []

    def _maintenance_card_admitted(self, plugin_id: str) -> bool:
        """「配置维护」卡的准入：**用户差异段里真有可清的键**。

        同 :meth:`_interval_card_admitted`：抽出来与 :meth:`_framework_page_content` 共用。
        """
        return self._config is not None and bool(self._clearable_keys(plugin_id))

    def _build_maintenance_card(self, info) -> "QWidget | None":
        """「配置维护」卡：**恢复默认配置**按钮（框架统一提供，组件一行代码不用加）。

        准入 = **用户差异段里真有可清的键**（``Config.clearable_plugin_keys`` 非空）：

        - 没改过默认值的组件不长这张卡 —— 点了也只会得到「本来就是默认」，白占版面；
        - 配置真被写脏的组件（正是最需要它的那种）**必定**看得见它。

        悬停提示**逐条列出将要清掉的键**：不这么做，那句确认就只能是「确定恢复默认？」，
        而这一步不可撤销（不作备份）。清单由配置层算（判据只此一处），界面不另判一遍。
        """
        if not self._maintenance_card_admitted(info.id):
            return None
        keys = self._clearable_keys(info.id)
        card, content = settings_section("配置维护", self)
        # 档位就是**默认动作档**（与同区其余按钮同高同字号）：它是一张卡里唯一的动作
        # 按钮，不是显示位芯片那类小控件 —— 走 chip 档会让这一页出现两种按钮尺码。
        btn = tool_button("恢复默认配置", "pill-ghost", parent=card)
        name = info.name or info.id
        btn.setToolTip(
            f"把「{name}」在 config.yaml 里的配置清掉，回到默认值（保留「启用」开关）。"
            f"将清掉：{'、'.join(keys)}"
        )
        # 回调绑本卡自己的那组参数（每卡各建一个闭包，不共享循环变量）。两个 lambda
        # 互为**兄弟**（不嵌套）⇒ 一个读不到另一个的形参，故各自带一份 `pid`：
        # 少了第二个那半，回执（尤其「没做成」那句）就是 NameError —— 它正是「异常出声」
        # 这条机制自己的兜底，最不该哑掉的一处（pyflakes 会当场报 undefined name）。
        _wire_button(
            btn,
            lambda *_, pid=info.id, nm=name: self._on_plugin_reset(pid, nm),
            lambda text, error=False, pid=info.id: self._zone_say(
                pid, "error" if error else "info", text
            ),
        )
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(metrics.GAP_SM)
        row.addWidget(btn)
        row.addStretch(1)
        content.addLayout(row)
        return card

    # ---------- 组件页框架区：装置运维（框架按声明注入） ----------

    def _helper_ops_plugin(self, plugin_id: str):
        """取该组件的实例 —— 且它**声明了** root helper 依赖；否则 ``None``。

        声明信号早就有了（``resources().helper_tasks`` / ``needs_root``），框架此前只用它
        算编排表（从前落 ``helper_tasks.json``，现在是 ``intent.set("tasks", …)`` 的一帧），
        **没用它决定「这一页该不该长出一块装置运维」** ⇒
        「谁用 helper」有了两个真源（声明 ＋ 插件手写的接线），第二个必然漂：hosts 托管
        正是漂掉的那个（没接上 ``build_daemon_ops``、警示另立一套 ``daemon_state()``）。
        本方法把那条判据收回声明一处。

        **不新造** ``uses_helper`` 字段：那会与 ``helper_tasks`` 成第二真源；将来若有
        组件「要 root 但不要 task」，``needs_root`` 已经覆盖得住。
        """
        get = getattr(self._manager, "plugin_instance", None)
        if not callable(get):
            # 轻量测试桩没有这个口 ⇒ 一律不画（与桩缺其余门面同款降级，不崩）
            return None
        try:
            plugin = get(plugin_id)
        except Exception:  # noqa: BLE001
            log.exception("读取组件 %s 的实例失败，本次不画装置运维", plugin_id)
            return None
        if plugin is None:
            return None
        try:
            res = plugin.resources()
        except Exception:  # noqa: BLE001
            log.exception("读取组件 %s 的资源声明失败，本次不画装置运维", plugin_id)
            return None
        if getattr(res, "helper_tasks", None) or getattr(res, "needs_root", False):
            return plugin
        return None

    def _build_helper_ops_card(self, info) -> "QWidget | None":
        """「装置运维」卡（页尾区第 ③ 张）：**框架按声明注入**，组件一行不写。

        准入 = 该组件声明了 root helper 依赖（见 :meth:`_helper_ops_plugin`）。

        卡里那三枚按钮、那句警示、以及**卡内**的反馈槽全来自插件侧的能力 mixin
        （``capabilities.helper_ops``）：框架只知道「画不画、画在哪」，动作与文案由组件
        实现 —— 这正是「shell 不认识任何插件」那条边界的用法（同 ``build_daemon_ops``
        的既有口径：只画界面、回调注入）。反馈槽放**卡内**、不借页尾区共用槽：触发它
        的三枚按钮就在这张卡里（用户 2026-09-30 要求）。
        """
        plugin = self._helper_ops_plugin(info.id)
        if plugin is None:
            return None
        if plugin not in self._helper_ops_plugins:
            self._helper_ops_plugins.append(plugin)
        card, content = settings_section("装置运维", self)
        block, sync = build_daemon_ops(
            card,
            install=plugin.install_action,
            uninstall=plugin.uninstall_action,
            immediate_check=plugin.immediate_check,
            status=plugin.helper_notice,
        )
        content.addWidget(block)
        texts = plugin.helper_ops_texts()
        if texts.note:
            content.addWidget(note(texts.note, card))
        # 反馈（安装回执 / 卸载确认）落**本卡内部**：触发它的三枚按钮就在这张卡里，
        # 提示贴同一张卡上才不脱节（用户 2026-09-30 要求）。页尾区那条共用槽留给
        # 「运行间隔 / 配置维护」两卡，不再借给装置运维。
        fb = FeedbackBar(card)
        content.addWidget(fb)
        plugin.attach_helper_ops(card, fb, sync)
        return card

    def refresh_helper_notices(self) -> None:
        """守护连接状态 / 版本变化后，重算所有装置运维卡的警示。

        接 ``HelperSession.stateChanged``（app 侧装配时连线）。重装会重启守护：旧进程
        断开、新进程握手报新版本 —— 那一刻若不主动刷一次，开着设置页的用户会一直看着
        「守护版本过期」，直到重开页面。

        逐插件走它们**公开的** ``refresh_helper_notice()``（它内部先
        ``on_helper_change()`` 重拉状态、再重算警示）—— 只重算不重拉会拿旧缓存算出
        同样的红字。卡已销毁的插件那一份会安全空转。
        """
        for plugin in self._helper_ops_plugins:
            try:
                plugin.refresh_helper_notice()
            except Exception:
                log.exception("重算装置运维警示失败：%s", type(plugin).__name__)

    def _zone_say(self, plugin_id: str, kind: str, text: str) -> None:
        """把一句结果写进**该组件页框架区**的反馈槽（没有那一页时只记日志）。

        重置 / 改节拍的消息**必须落在触发它的那一区** —— 页面那槽在页首，页尾点的按钮
        在长页里够不着它（见 ``_build_framework_zone``）。``kind`` 取 ``info``（做成了）
        / ``hint``（中性告知，如「本来就是默认配置，没有可清除的改动」）/ ``error``。
        """
        if kind == "error":
            log.error("%s", text)
        else:
            log.info("%s", text)
        fb = self._zone_feedback.get(plugin_id)
        if fb is None:
            return
        if kind == "error":
            fb.error(text)
        elif kind == "hint":
            fb.hint(text)
        else:
            fb.info(text)

    # ---------- 组件页框架区：恢复默认配置（框架统一提供，§9.5 初始化入口） ----------

    def _on_plugin_reset(self, plugin_id: str, name: str) -> None:
        """恢复默认配置：**先在行内问一句**，点下「恢复默认」才真清（零模态）。

        与 ``_on_plugin_uninstall`` 同款两段式（``FeedbackBar.ask`` 的确认是异步的）。
        拿不到槽位就**拒绝执行**：这一步不可撤销、也没有备份，「没人确认」等于「不清」。

        问句落在**该组件页框架区**那条槽上（不是「插件」页的槽）：按钮长在页尾，
        问句必须出现在手边，否则长页里用户看不到它、以为点了没反应。
        """
        if self._manager is None:
            return
        keys = self._clearable_keys(plugin_id)
        if not keys:
            self._zone_say(plugin_id, "hint", f"「{name}」本来就是默认配置，没有可清除的改动。")
            return
        fb = self._zone_feedback.get(plugin_id)
        if fb is None:
            log.warning("组件页没有反馈槽位，取消恢复默认配置 %s", plugin_id)
            return
        fb.ask(
            f"把「{name}」的配置恢复为默认？将清掉 config.yaml 里这 {len(keys)} 项："
            f"{'、'.join(keys)}。原内容不作备份，不能撤销。",
            "恢复默认",
            lambda p=plugin_id, n=name: self._do_plugin_reset(p, n),
            confirm_kind="danger",
        )

    def _do_plugin_reset(self, plugin_id: str, name: str) -> None:
        """真正执行（确认之后才走到这里）：清配置 → 重算框架区 → 回报清单。

        **重算框架区**不是装饰：清完这个组件就没有可清的键了，「配置维护」卡该当场消失
        —— 卡在不在本身就是「还有没有改动」的显示位。

        这里**不重建整页**：卡长在组件页上，「插件」页那套 ``_rebuild_plugin_list()``
        动不到它；而整页 ``rebuild_pages()`` 会连带拆掉**别的**组件页（可能正开着未保存
        的草稿），代价与「让一张卡消失」不相称。故框架区自带一个就地重算入口
        （``_refresh_zone``）—— 卡片换新、**槽位与位置不动**，于是下面那句回报写进去
        不会随旧页蒸发。

        写盘失败（权限 / 磁盘）当场报错，**不假装成功** —— 本组件最不能出的错就是
        「界面说清了、盘上没动」。
        """
        if self._manager is None:
            return
        try:
            removed = self._manager.reset_plugin_config(plugin_id)
        except Exception as e:  # noqa: BLE001
            self._zone_say(plugin_id, "error", f"「{name}」的配置恢复默认失败：{e}")
            return
        self._refresh_zone(plugin_id)
        if removed:
            self._zone_say(
                plugin_id, "info", f"「{name}」的配置已恢复默认：清掉了 {'、'.join(removed)}。"
            )
        else:
            self._zone_say(
                plugin_id, "hint", f"「{name}」本来就是默认配置，没有可清除的改动。"
            )

    def _say_info(self, text: str) -> None:
        """把一句结果写进反馈槽；没有槽位（窗口已销毁）时只记日志。"""
        log.info("%s", text)
        if self._plugin_feedback is not None:
            self._plugin_feedback.info(text)

    def _say_error(self, text: str) -> None:
        """同 `_say_info`，红色。**照记 ERROR 日志** —— 没界面时日志是唯一线索。"""
        log.error("%s", text)
        if self._plugin_feedback is not None:
            self._plugin_feedback.error(text)
