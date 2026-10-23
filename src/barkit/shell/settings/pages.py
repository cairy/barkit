"""``_PagesMixin`` —— 分页骨架：侧栏条目、页与组件页的装配、页面重建。

``settings.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from barkit.contribution.specs import SettingsPageSpec
from barkit.paths import APP_NAME
from barkit.shell.hotkey_catalog import HotkeyHost
from barkit.shell.hotkeys_page import HotkeysPage
from barkit.shell.ui_kit import (
    apply_text_role,
    insert_page_slot,
    insert_page_tail,
    is_standard_shell,
    settings_page_shell,
)
from barkit.shell.design import metrics
from barkit.shell.variables_page import VariablesPage

from .constants import log
from .helpers import _scrolled


class _PagesMixin:
    """分页骨架：侧栏条目、页与组件页的装配、页面重建。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    def _build_ui(self) -> None:
        """在 __init__ 中一次性构建 UI；showEvent 只负责原生浮层层级与插件列表刷新。

        注意：绝不可在 showEvent 内构建 UI——set_window_level 等原生调用
        在异常时会中断后续布局代码（导致窗口空白），且窗口 hide 后再 show
        会重复构建、与已有布局冲突。
        """
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 侧栏导航
        nav = QListWidget()
        nav.setObjectName("set-nav")
        nav.setFixedWidth(metrics.NAV_WIDTH)
        self._nav = nav  # 存引用：rebuild_pages 需在插件变更后重建导航+栈
        # 框架固定页 + 插件管理页（E3 ②）+ 插件贡献页（registry.settings_pages）+ 框架固定页
        # 所有分页统一包进滚动区（见 ``_scrolled``）：长页面不再被裁。
        pages = self._page_entries()
        for label, _builder, _pid in pages:
            nav.addItem(label)

        # 主区
        self._stack = QStackedWidget()
        # 分页整体重建 ⇒ 旧的状态机段随之作废（不清引用会把已销毁的 widget 一直刷下去）
        self._state_sections = []
        self._zone_feedback = {}
        self._zone_refresh = {}
        for _, builder, _pid in pages:
            content = builder()
            self._stack.addWidget(_scrolled(content))

        nav.currentRowChanged.connect(self._stack.setCurrentIndex)
        nav.setCurrentRow(0)

        # 搜索索引必须在条目加完之后建（下标即行号）
        self._build_search_index(pages)
        root.addWidget(self._build_sidebar())
        root.addWidget(self._stack, stretch=1)

    def _page_entries(self) -> "list[tuple[str, Callable[[], QWidget], str]]":
        """侧栏与内容栈的**唯一**页表（``_build_ui`` 与 ``rebuild_pages`` 共用）。

        两处各写一份的代价 2026-09-22 已经付过一次：组件页准入改判时「画卡」与「判页」
        两处分叉 ⇒ 长出空页。加一页要改两个地方，迟早有一处漏改 —— 页表只留这一份。
        返回顺序即侧栏顺序。

        三元组第三位是**组件 id**（框架页为空串）—— 侧栏搜索靠它去取该组件的
        ``config_schema()``，好让「运行间隔」这类字段标签也能被搜到。
        """
        pages: "list[tuple[str, Callable[[], QWidget], str]]" = [
            ("通用", self._page_general, ""),
        ]
        # 快捷键：**跨组件**的框架级页，紧挨「通用」—— 它是全局偏好，放进组件页那一堆
        # 会被 N 个组件页挤到看不见。
        pages.append(("快捷键", self._page_hotkeys, ""))
        # 变量：同为**跨组件**的框架级页，紧随「快捷键」。变量全局共享（任何组件的任务
        # 都能引用它），故它的配置 UI 不进任何单个组件的设置页（设计文档 §2.6）。
        pages.append(("变量", self._page_variables, ""))
        if self._manager is not None:
            pages.append(("插件", self._page_plugins, ""))
        # 组件页走 ``_plugin_page`` 包装：页首插状态机段、页尾补框架区。
        # **每页是不是存在**由 ``_page_specs`` 判（见那里：声明不再是准入）
        pages += [(p.label, self._plugin_page(p), p.plugin_id)
                  for p in self._page_specs()]
        pages += [
            ("维护", self._page_maintenance, ""),
            ("关于", self._page_about, ""),
        ]
        return pages

    def _page_hotkeys(self) -> QWidget:
        """「快捷键」页：三类来源统一管理（框架内置 / 组件声明 / 用户自建）。

        页面本体在 ``shell/hotkeys_page.py``，这里只**接线**（宿主能力 + 配置递进去）。
        没注入 host 时（未装配 / 单测）页面照出，只是没有内置那几条、改键也挂不上去 ——
        页面会把「没能挂上」说出来，不假装成功。
        """
        host = self._hotkey_host or HotkeyHost()
        return HotkeysPage(
            config=self._config,
            contributions=self._registry,
            actions=host.actions,
            on_rebind=host.rebind,
            conflict_of=host.conflict_of,
            hotkeys_available=host.available,
            feedback=host.feedback,
            failure_of=host.failure_of,
        )

    def rebuild_pages(self) -> None:
        """插件启用 / 禁用 / 卸载后重建全部分页（含插件贡献页与「插件」管理页）。

        由 PluginManager.on_plugins_changed 回调触发，使设置窗在运行期也能即时反映
        插件集合变化（否则启用/禁用后其设置分页数/列表不更新，需关闭重开才刷新）。

        保留当前所在分页（按标签匹配），避免重建后跳回首页。导航对象复用，
        仅清空其条目并重新添加；信号连接仍挂在原 nav 上，无需重连。
        """
        if self._registry is None:
            return
        current_label = (
            self._nav.currentItem().text() if self._nav.currentItem() is not None else None
        )
        self._nav.blockSignals(True)
        try:
            # 清空主区分页（移除并异步销毁旧 widget）
            while self._stack.count():
                w = self._stack.widget(0)
                self._stack.removeWidget(w)
                if w is not None:
                    w.deleteLater()
            self._nav.clear()
            # 重建页列表（重新拉取插件贡献页，反映最新活跃集合）
            # 所有分页统一包进滚动区（见 ``_scrolled``）：长页面不再被裁。
            pages = self._page_entries()
            for label, _builder, _pid in pages:
                self._nav.addItem(label)
            # 分页整体重建 ⇒ 旧的状态机段随之作废（不清引用会把已销毁的 widget 一直刷下去）
            self._state_sections = []
            self._zone_feedback = {}
            self._zone_refresh = {}
            for _, builder, _pid in pages:
                content = builder()
                self._stack.addWidget(_scrolled(content))
            # 恢复当前页（走 select_page 而不是就地再写一遍查找：按标签查页只该有一处
            # 实现，否则「认名字」还是「认位置」会在两条路上各说各话）。信号已 block，
            # 故它内部的 setCurrentRow 不会触发切页，切页由它显式设的栈下标完成。
            # 找不到（当前那页随插件卸载消失）时它什么都不做 ⇒ 停留首页，与旧行为一致。
            if current_label is not None:
                self.select_page(current_label)
            # 组件集合变了 ⇒ 可检索字段也变了；并按当前输入重新过滤一次（不然重建后
            # 侧栏回到「全显示」，与框里还写着字互相矛盾）。
            self._build_search_index(pages)
        finally:
            # 必须无条件解除：任何一个 ``builder()`` 抛异常（插件页自己炸了）时，少了
            # finally 就会把 block 永久留在 nav 上 —— 此后点侧栏再也不切页，而设置窗
            # 看上去一切正常（只是「点了没反应」）。
            self._nav.blockSignals(False)
        # 过滤放在**解除 block 之后**：它那句「当前行被隐藏 ⇒ 落到第一个看得见的」
        # 靠 ``currentRowChanged`` 把内容栈带过去，block 状态下那一下不会切页。
        self._apply_search(self._search.text() if self._search is not None else "")

    # ---------- 插件设置页：框架自动插入「状态机」段（页首）与「框架区」（页尾） ----------

    # ---------- 组件页的准入（2026-09-22 修订：声明不再是准入）----------

    def _page_specs(self) -> list[SettingsPageSpec]:
        """设置窗要出的组件页：**每个有内容的组件一页**，「自己声明了页」不再是准入。

        判据与本仓「组件行」那一格同源 —— 那个显示位已改为「每个组件恒有一行，没声明
        由框架合成朴素行」，声明只决定**长什么样**、不决定**有没有**（用户 2026-09-22
        的原话即判据：「所有的组件都要有组件行（不需要声明）」）。设置页此前仍以「组件
        自己声明了页」当准入，于是同一件事在组件维度上长出两种形态，而框架要往**页里**
        注入的那两段（页首状态机段、页尾框架区）只在第一形态里有挂点 —— 2026-09-22
        用户上报：音频看护声明了一条九取值的状态机，设置窗里却**没有它的页**，于是
        那段画得出来却没处挂。

        准入改成「这一页有没有内容」：

        - 组件自己给了页 ⇒ 用它（组件内容 ＋ 框架两段）；
        - 没给、但框架有东西可画（见 :meth:`_framework_page_content`）⇒ 框架出页 ——
          交回 ``_plugin_page`` 时走它「组件没给页」那条既有分支（框架自己造外壳）；
        - 两边都空 ⇒ **真的不出页**：多一个点开是空的页，比没有那一页更糟。

        候选面是**当前活跃**的组件（``active_ids``）：禁用 / 被隔离的组件不在这里出页，
        它们在「插件」页里由开关与「问题」文案交代（与 ``status_rows`` 不合成隔离插件
        那一行同一条口径）。拿不到活跃集合（轻量桩）⇒ 退回旧行为，只出自声明的页。

        顺序沿用组件顺序（与「插件」页、面板行、标题栏图标条同源），不按收集顺序。
        """
        declared = {
            p.plugin_id: p
            for p in (self._registry.settings_pages() if self._registry is not None else [])
        }
        active = getattr(self._registry, "active_ids", None)
        if active is None:
            return list(declared.values())
        infos: dict = {}
        if self._manager is not None:
            try:
                infos = {i.id: i for i in self._manager.list_plugins()}
            except Exception:  # noqa: BLE001
                log.exception("读取组件清单失败，本次只出组件自己声明的设置页")
        candidates = {
            pid: info
            for pid, info in infos.items()
            if pid in active and not getattr(info, "problem", "")
        }
        out: list[SettingsPageSpec] = []
        for pid in self._plugin_order_ids(candidates):
            own = declared.pop(pid, None)
            if own is not None:
                out.append(own)
                continue
            if not self._framework_page_content(pid):
                continue
            out.append(SettingsPageSpec(
                plugin_id=pid,
                label=getattr(candidates[pid], "name", "") or pid,
                # 不给 ``build``：那份 spec 的语义正是「组件自己不贡献页内容」，而
                # ``_plugin_page`` 早有这条分支（``own is None`` ⇒ 框架自己造外壳）。
                # 这与收集期那条 fail-fast **不冲突**：那条拒的是「**组件声明的**页既无
                # build 又无 schema」（打开设置窗必 TypeError），而这一份是**框架**造的、
                # 且走的正是「没有组件内容」那条被支持的路 —— 两者不是同一件事。
                reason="框架自出一页：组件没有自己的设置内容，但框架有东西可画"
                       "（状态机段 / 运行间隔 / 配置维护）",
            ))
        # 防御：声明了页却不在清单里的（未进活跃集合 / 桩缺 list_plugins）也别丢
        out.extend(declared.values())
        return out

    def _framework_page_content(self, plugin_id: str) -> bool:
        """框架在这位组件页里有没有东西可画 —— 决定「组件没自声明页时还出不出一页」。

        **判据不另写一份**：四项各自复用「画那一段」的那条准入判断 —— 一旦两处分叉，
        就会出现「按这个判据说有内容、按那个画法却画不出来」的空页，或反过来「画得出来
        却拿不到页」。顺序即页里段的顺序：状态机段 → 运行间隔 → 配置维护 → 装置运维。
        """
        if self._state_specs_of(plugin_id):
            return True
        info = self._plugin_info(plugin_id)
        if info is None:
            # 连清单都拿不到 ⇒ 页尾区整块画不出来（``_build_framework_zone`` 的同一道闸）
            # ⇒ 这里必须同判「没有内容」，否则会长出一页点开是空的壳
            return False
        if self._interval_card_admitted(info):
            return True
        if self._maintenance_card_admitted(plugin_id):
            return True
        return self._helper_ops_plugin(plugin_id) is not None

    def _plugin_page(self, page: SettingsPageSpec) -> "Callable[[], QWidget]":
        """插件设置页的**构建器**：框架往组件自己的页里补两段 —— 页首「状态机」、
        页尾「框架区」（运行间隔 / 配置维护 / 装置运维）。

        返回的是**可调用对象**而不是造好的 widget：分页在 ``rebuild_pages`` 时会整体
        重建，段每次都得拿最新的状态机集合与优先级（插件增删后集合会变）。

        插在**标题以下**（外壳给的插槽位）而不是别处：这段说的是「这台机器现在什么情况
        + 我更关心哪一台」，属于该组件的总览；插件自己的详细配置排在其后，符合
        「先看全局再改细节」。也不能插在标题**之上** —— 那会读成「先来一段不知道属于
        谁的卡片」（2026-09-21 用户上报）。页尾区则反过来：它是框架画的**运行参数与
        处置动作**，必须落在组件自己的内容**之后**。
        """
        def build() -> QWidget:
            own = page.build() if page.build is not None else None
            section = self._build_state_section(page.plugin_id)
            zone = self._build_framework_zone(page.plugin_id)
            # 插进**插件自己那一页**，而不是在页外面再包一层盒子：包一层就变成「先来一段
            # 不知道属于谁的卡、然后才是这个组件的名字」，而且外壳边距会被套两遍
            # （2026-09-21 用户上报）。
            if own is not None and is_standard_shell(own):
                if section is not None:
                    insert_page_slot(own, section)
                if zone is not None:
                    insert_page_tail(own, zone)
                return own
            if own is not None and section is None and zone is None:
                # 不是标准外壳、框架这页又没有任何要补的：原样交回 —— 别为「统一」给它
                # 套一层空壳（页标题与边距都是白加的）。
                return own
            if own is None and section is None and zone is None:
                return QWidget()
            # 插件没给页 / 给的页不是标准外壳：框架自己造一页，两头按固定序装上去。
            shell, shell_lay = self._page_shell(page.label or page.plugin_id)
            if section is not None:
                shell_lay.addWidget(section)
            if own is not None:
                shell_lay.addWidget(own)
            if zone is not None:
                shell_lay.addWidget(zone)
            return shell

        return build

    # ---------- 页面构造 ----------

    @staticmethod
    def _page_shell(title: str) -> tuple[QWidget, QVBoxLayout]:
        return settings_page_shell(title)

    def _page_variables(self) -> QWidget:
        """「变量」页：全局变量**声明**的唯一编辑入口（框架级，跨组件；§2.6）。

        页面本体在 ``shell/variables_page.py``，这里只**接线**（配置 ＋ 注入的五个能力）。
        没注入 host 时（未装配 / 单测）页面照出，只是「当前值」那格画一个破折号、
        「测试」那枚按钮不出现 —— 页面**不假装有值**。
        """
        page = VariablesPage(config=self._config, host=self._variables_host)
        # 存引用：设置窗每次 show 都要刷一次这一页（它显示的是**运行值**，而运行值会在
        # 窗口关着的时候变）。重建分页时会被下面那一次赋值覆盖成新页，不会悬空。
        self._variables_page = page
        return page

    def _page_general(self) -> QWidget:
        """「通用」页：框架级设置里**与任何组件无关**的那一类。

        放进组件设置页就会出现「同一个框架开关在 N 个组件页各有一份」—— 那是这条注释
        原本要守的事，今天仍然成立。但「框架级开关的**唯一**落点（不新建页）」那句
        已不再准确（2026-10-10）：跨组件的框架级页另有「快捷键」（改键）与「变量」
        （全局变量的声明，§2.6），判据是「这东西属不属于某一个组件」——不属于就进框架页，
        而不是「只许有一页」。
        """
        page, layout = self._page_shell("通用")
        layout.addWidget(self._note(
            "外观随系统浅色 / 深色自动切换。"
        ))
        layout.addWidget(self._build_tray_card(page))
        layout.addStretch(1)
        return page

    def _page_maintenance(self) -> QWidget:
        """「维护」页：三件**不属于任何单个组件**的事 —— 开机自启 / 权限 / 数据。

        为什么合成一页（2026-09-22）：它们各自都是「装一次、基本不动」的，分成三页时
        侧栏要连点三下才找齐，每页还各占一条页首说明与一条反馈槽。合成一页后按**卡**
        分组（与设置页其余各页同款），每张卡自带一条槽（**一区一条**）。

        为什么「数据」在这里：**数据目录对每个组件是同一个**（配置与状态文件都住在那），
        此前它被框架画在**每个组件页的页尾** ⇒ 同一枚按钮复制了 N 份（用户 2026-09-22
        上报）。「复制诊断」不在这里 —— 它复制的是**某个组件**的诊断，长在该组件自己的
        卡里（与「拨测」同排）。
        """
        page, layout = self._page_shell("维护")
        layout.addWidget(self._note(
            "这几样都不属于某个具体组件：装一次基本不动，排查问题时才来。"
        ))
        # 「恢复系统状态」排第一：它是这页唯一**不可逆**的动作，且只在真有组件改过
        # 系统时才出现（空壳不出，见该卡 docstring）。
        revert = self._build_revert_card(page)
        if revert is not None:
            layout.addWidget(revert)
        layout.addWidget(self._build_autostart_card(page))
        layout.addWidget(self._build_permissions_card(page))
        layout.addWidget(self._build_data_card(page))
        layout.addWidget(self._build_log_card(page))
        layout.addStretch(1)
        return page

    def _page_about(self) -> QWidget:
        page, layout = self._page_shell("关于")
        name = QLabel(APP_NAME)
        apply_text_role(name, "page_title")
        layout.addWidget(name)
        layout.addWidget(self._note(
            "菜单栏常驻助手：把常用操作收进状态栏，按需取用。"
        ))
        layout.addStretch(1)
        return page

    @staticmethod
    def _note(text: str) -> QLabel:
        note = QLabel(text)
        note.setObjectName("secondary")
        note.setWordWrap(True)
        apply_text_role(note, "note")
        return note
