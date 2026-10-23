"""``_MaintenanceMixin`` —— 「维护」页：开机自启 / 权限 / 数据 / 日志。

``settings.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QWidget,
)

from barkit.core.permissions import (
    ACCESSIBILITY_GUIDE,
    accessibility_trusted,
    available,
    open_accessibility_pane,
    request_accessibility,
)
from barkit import paths
from barkit.core import config_bundle, logging_setup
from barkit.shell.autostart_row import AutostartRow
from barkit.shell.data_ops import data_dir_button, reveal_button
from barkit.shell.log_sources import log_sources
from barkit.shell.ui_kit import (
    ChoiceSelector,
    FeedbackBar,
    Switch,
    form_row,
    note,
    settings_section,
    tool_button,
)
from barkit.shell.design import metrics

from .constants import _LOG_LEVEL_CHOICES, _LOG_LEVEL_LABELS, log
from .helpers import _wire_button


class _MaintenanceMixin:
    """「维护」页：恢复系统状态 / 开机自启 / 权限 / 数据 / 日志。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 恢复系统状态 ----------

    def _build_revert_card(self, parent: QWidget) -> "QWidget | None":
        """「恢复系统状态」卡：把各组件改过的系统状态**交还**回去（**空壳不出**）。

        为什么这一卡必须存在：**复原钩子与声明格早就写好了，只是没有入口**。唯一调到
        ``Plugin.teardown`` 的那条路是卸载七步，而它第一关就拒内置组件 —— 恰恰声明了
        系统变更的多是内置的（路由 / hosts / 隧道）。于是那三份 ``teardown`` 从上线起
        一次都没被跑到过。

        它**不是卸载**：不删配置、不删数据文件、不动插件集合（外挂插件的装卸归「插件」
        页）。撤完之后组件会被停用（先停手再复原，否则它下一拍就把撤掉的写回去），
        用户可以自己再启用 —— 那才是「还给你了」的意思。

        粒度是**整条撤销**（一个组件要么全撤、要么不撤）：零契约改动，三个组件现有的
        声明一句都不用改。
        """
        items = self._declared_system_mutations()
        if not items:
            return None
        card, lay = settings_section("恢复系统状态", parent=parent)
        fb = FeedbackBar(card)
        fb.destroyed.connect(lambda *_a, _fb=fb: self._forget_revert_feedback(_fb))
        self._revert_feedback = fb
        lay.addWidget(note(
            "下列组件在系统里留下了东西。想把它改过的还回去，勾上再执行 —— "
            "撤完该组件会一并停用，你可以再启用它。", card
        ))
        # 每次重建都换一套开关：旧的随页面一起销毁，留着会戳已删的 C++ 对象。
        self._revert_switches = {}
        for plugin_id, mutations in items:
            info = self._plugin_info(plugin_id)
            name = (getattr(info, "name", "") or plugin_id)
            sw = Switch(card)
            sw.setChecked(False)          # 默认不勾：不可逆的事不替用户选
            # ``Switch.clicked`` **不翻转自身**（与 ``setChecked`` 解耦，见它的契约）——
            # 谁用它谁负责翻。漏了这一句的开关按下去一点反应都没有，而下面读的是
            # ``isChecked()`` ⇒ 这张卡永远勾不上、永远回「先勾一项」。
            #
            # 走 ``_wire_button`` 而不是自己直连信号：设置页的按钮接线只有
            # 一个出口（异常与返回值都要出声），门禁
            # ``test_settings_page_has_no_raw_button_connection`` 扫的就是这一条。
            # 本行是那次零模态改造留下的唯一漏网点（2026-10-22 全量跑出）。
            _wire_button(sw, lambda _checked=False, _s=sw: _s.setChecked(not _s.isChecked()))
            self._revert_switches[plugin_id] = sw
            lay.addLayout(form_row(name, sw, card))
            lay.addWidget(note("；".join(m.description for m in mutations), card))
        run = tool_button("恢复勾选的这几项", "primary", parent=card)
        _wire_button(run, self._on_revert_requested, self._say_permission)
        row = QHBoxLayout()
        row.addWidget(run)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(fb)
        return card

    def _declared_system_mutations(self) -> list:
        """向编排层现问「谁留下了什么」；没接线 / 读不到即空清单（**卡不出现**）。"""
        if self._manager is None:
            return []
        try:
            return list(self._manager.declared_system_mutations())
        except Exception:  # noqa: BLE001 —— 读不到就当没有，不给一张点了会炸的卡
            log.exception("读取系统变更声明失败，本卡不出现")
            return []

    def _on_revert_requested(self) -> None:
        """执行前先摆出「将撤什么」，**在按钮下面那条槽里**问一句再动手（零模态，不问不跑）。

        确认**就地**（本卡自己的 :class:`~barkit.shell.ui_kit.FeedbackBar`），不走右上角
        那个提示浮层。2026-10-16 真机上报的正是这一点：那个浮层的落点是
        ``Overlay.show_toast``（可用区右上角）、窗口层级 25，而设置窗是 201 且从菜单栏图标
        下展开后被夹在屏幕右缘 —— 正好盖住右上角，**确认卡被设置窗挡死**。用户在按钮下面
        只看到上一轮遗留的那句旧提示，右上角什么也没看见（于是更像「点了没反应」）。

        「弹到屏幕正中」也救不了它：那仍然是**另一扇窗**（层级照样低于设置窗，且认屏跟着
        光标走，多屏时可能弹到另一块屏上）。出路是**别用另一扇窗** —— 不可逆的确认就长在
        刚才点的那枚按钮旁边。

        问句是**多行的组件清单**，行内条会折成长长一条 —— 这是明知的代价：**位置对**比
        「一条好看的话」重要（同页另外几张卡的问句也都在行内条里）。
        """
        chosen = [pid for pid, sw in self._revert_switches.items() if sw.isChecked()]
        if not chosen:
            # 什么都没发生（没出错、也没做成）⇒ 中性告知，不是成功回执（见 `_say_revert`）
            self._say_revert("先勾一项：没勾就等于什么都不撤。", tone="hint")
            return
        fb = self._revert_feedback
        if fb is None:
            # 槽位没了（页面已销毁）⇒ 没有确认口就没人确认。宁可不撤，也不无声地动一件
            # 不可自动恢复的事（与各组件「拿不到槽位就拒绝执行」同款纪律）。
            log.warning("维护页没有反馈槽位，取消复原系统状态：%s", chosen)
            return
        lines = []
        for pid in chosen:
            info = self._plugin_info(pid)
            lines.append("· %s" % (getattr(info, "name", "") or pid))
        text = "将撤这些组件改过的系统状态，撤完不可自动恢复：\n%s" % "\n".join(lines)
        fb.ask(text, "确认恢复", lambda: self._run_revert(chosen), confirm_kind="danger")

    def _run_revert(self, plugin_ids: list) -> None:
        """逐个复原；**成败逐项说清**，不拿一句「已恢复」盖掉失败的那一半。"""
        done, failed = [], []
        for pid in plugin_ids:
            try:
                ok = bool(self._manager.revert_system_state(pid))
            except Exception:  # noqa: BLE001
                log.exception("复原 %s 的系统状态抛异常", pid)
                ok = False
            (done if ok else failed).append(pid)
        if not failed:
            self._say_revert("已恢复 %d 项，相关组件已停用。" % len(done))
            return
        names = [getattr(self._plugin_info(p), "name", "") or p for p in failed]
        self._say_revert("已恢复 %d 项；这些没撤干净（详见日志）：%s"
                         % (len(done), "、".join(names)), tone="error")

    def _say_revert(self, text: str, *, tone: str = "info") -> None:
        """把一句结果写进本卡那条槽；没槽位（窗口已销毁）时只记日志。

        ``tone`` 三档照 :class:`~barkit.shell.ui_kit.FeedbackBar` 的分工：**发生过一件事**
        走 ``info``（绿）、**没出错也还没做成**走 ``hint``（灰）、失败才走 ``error``（红）。
        「先勾一项」属于第二档 —— 拿 ``info`` 说它，等于给一个**没发生的动作**发成功回执，
        用户读到的是「成了」，而屏幕上什么都没变（2026-10-16 用户上报）。
        """
        (log.error if tone == "error" else log.info)("%s", text)
        fb = self._revert_feedback
        if fb is None:
            return
        if tone == "error":
            fb.error(text)
        elif tone == "hint":
            fb.hint(text)
        else:
            fb.info(text)

    def _forget_revert_feedback(self, fb: FeedbackBar) -> None:
        """槽位所属页面被销毁时摘掉引用（同 ``_forget_autostart_feedback``）。"""
        if self._revert_feedback is fb:
            self._revert_feedback = None

    def _build_autostart_card(self, parent: QWidget) -> QWidget:
        """「开机自启」卡：自启开关的唯一落点，也是本卡反馈槽的宿主。

        槽挂在**卡**上而不是自启行内部：行是一条水平布局，塞进一块会长出确认
        按钮的区域会把那行挤变形；而「关闭自启」是破坏性动作，必须有一处能原地
        长出「确认 / 取消」的地方（零模态纪律，见 ``ui_kit.FeedbackBar``）。
        卡标题就是这一行的标题（``AutostartRow`` 因此不再自带一行标题）。
        """
        card, lay = settings_section("开机自启", parent=parent)
        fb = FeedbackBar(card)
        fb.destroyed.connect(lambda *_a, _fb=fb: self._forget_autostart_feedback(_fb))
        self._autostart_feedback = fb
        lay.addWidget(AutostartRow(card, feedback=fb))
        # 槽紧跟在按钮那一行下面：点了按钮，消息就出现在手边
        lay.addWidget(fb)
        lay.addWidget(note("装一次就不动；关掉自启后 BarKit 会随即退出。", card))
        return card

    def _forget_autostart_feedback(self, fb: FeedbackBar) -> None:
        """槽位所属页面被销毁时摘掉引用，避免戳已删的 C++ 对象。

        只在「当前引用的正是刚死掉的那个」时清空 —— 重建时新页的槽位可能**已经先
        赋好值**（与 ``_forget_plugin_feedback`` 同款写法）。
        """
        if self._autostart_feedback is fb:
            self._autostart_feedback = None

    def _build_permissions_card(self, parent: QWidget) -> QWidget:
        """「权限」卡：辅助功能授权的探测与引导，含本卡的行内反馈槽。

        槽挂在**卡**上（与「开机自启」卡同款）：引导语要有能原地出现的地方。改前
        这颗按钮直达 ``core/permissions.ensure_accessibility``，那是
        ``QMessageBox(parent).exec()`` —— 设置窗钉在原生层级 201，从它内部再开一条
        嵌套事件循环（证据见 ``tests/test_architecture.py`` 零模态门禁的 docstring）。
        现按零模态纪律拆开：**探测**的结果写槽，**两个动作**是卡里常驻的普通按钮。
        """
        card, lay = settings_section("权限", parent=parent)
        # 说明放卡首：先讲这一组管什么，再给动作。放在按钮下面会与槽里的引导语挤成
        # 相邻的两段灰字，读起来像说了两遍。
        lay.addWidget(note(
            "一键输入需要「辅助功能」授权才能注入按键；识别等功能不依赖此权限。", card
        ))

        check = tool_button("检查辅助功能权限", "primary", parent=card)
        _wire_button(check, self._check_accessibility, self._say_permission)
        lay.addWidget(check)

        # 两个动作常驻一排：原先它们藏在对话框的按钮里，用户看得到却点不动（框被
        # 置顶窗压住）。摆到卡里，就不再有「先弹一个框、再给按钮」这一层。
        act_row = QHBoxLayout()
        open_btn = tool_button("打开系统设置", "pill-ghost", parent=card)
        _wire_button(open_btn, self._open_accessibility_pane, self._say_permission)
        prompt_btn = tool_button("触发授权弹窗", "pill-ghost", parent=card)
        _wire_button(prompt_btn, self._request_accessibility, self._say_permission)
        for btn in (open_btn, prompt_btn):
            act_row.addWidget(btn)
        act_row.addStretch(1)
        lay.addLayout(act_row)

        # 槽紧跟在两个动作下面：点了按钮，消息就出现在手边
        fb = FeedbackBar(card)
        fb.destroyed.connect(lambda *_a, _fb=fb: self._forget_permissions_feedback(_fb))
        self._permissions_feedback = fb
        lay.addWidget(fb)
        return card

    def _forget_permissions_feedback(self, fb: FeedbackBar) -> None:
        """槽位所属页面被销毁时摘掉引用（同 `_forget_autostart_feedback`）。"""
        if self._permissions_feedback is fb:
            self._permissions_feedback = None

    def _build_data_card(self, parent: QWidget) -> QWidget:
        """「数据」卡：打开数据目录（**全局唯一落点**）＋ 配置的导出 / 导入。

        这枚按钮此前画在**每个组件页的页尾**（与「复制诊断」同卡），而它对每个组件
        打开的是**同一个目录** ⇒ 一份全局动作被复制了 N 份（用户 2026-09-22 上报）。
        现在它只在这里出现一次；诊断那枚回到组件自己的卡里。

        **导出 / 导入为什么也放这张卡**：它们是同一类动作 —— 对「用户域那一堆文件」
        整体做点什么，不属于任何单个组件。组件级的导出仍在组件页（那是另一件事：
        走 ``Plugin.export_data`` 契约，只带该组件声明的数据）。
        """
        card, lay = settings_section("数据", parent=parent)
        fb = FeedbackBar(card)
        fb.destroyed.connect(lambda *_a, _fb=fb: self._forget_data_feedback(_fb))
        self._data_feedback = fb
        row = QHBoxLayout()
        row.setSpacing(metrics.GAP_SM)
        row.addWidget(data_dir_button(
            card,
            say=lambda text, error=False: self._say_data(text, error),
        ))
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(note(
            "配置、状态与各组件的数据文件都住在这个目录里；在访达里打开它。", card
        ))

        # 「导出配置」是给**动代码 / 动配置之前**用的：它把整个用户域打成一个可校验的
        # 包，导出后立刻回读比对（``config_bundle.export_bundle`` 自带这一步）。
        backup_row = QHBoxLayout()
        backup_row.setSpacing(metrics.GAP_SM)
        export_btn = tool_button("导出配置", "primary", parent=card)
        _wire_button(export_btn, self._on_config_export, self._say_data)
        import_btn = tool_button("导入配置", "pill-ghost", parent=card)
        _wire_button(import_btn, self._on_config_import, self._say_data)
        backup_row.addWidget(export_btn)
        backup_row.addWidget(import_btn)
        backup_row.addStretch(1)
        lay.addLayout(backup_row)
        lay.addWidget(self._note(
            "导出的是一个 .zip，含全部设置与各组件数据（hosts 条目、变量答复、日账）。"
            "它含明文口令 —— 一键输入里那段注入文本会原样进包，别放云盘、别发给"
            "别人。导入会覆盖当前配置，动手前自动把当前状态备份到数据目录的 "
            "backups/；导入后需要重启 BarKit 才生效。",
        ))
        lay.addWidget(fb)
        return card

    def _on_config_export(self) -> None:
        """选落点 → 打包 → 回读校验 → 回执。

        默认落点放在 ``backups/``（数据目录里），不用桌面：桌面是个人目录、可能被
        iCloud 同步走，而这个包里有明文口令。
        """
        stamp = time.strftime("%Y%m%d-%H%M%S")
        default = paths.APP_SUPPORT / "backups" / f"barkit-config-{stamp}.zip"
        path, _filter = QFileDialog.getSaveFileName(
            self, "导出配置", str(default), "配置包 (*.zip)"
        )
        if not path:
            return                      # 用户取消：什么都没发生，不写回执
        report = config_bundle.export_bundle(dest_path=Path(path))
        if not report.ok:
            self._say_data(
                "导出没有通过完整性校验：%s。包已写出，但别拿它当备份。"
                % "；".join(report.problems[:3]),
                True,
            )
            return
        size = report.human_size
        self._say_data(
            "已导出 %d 个文件（%s），回读校验通过：%s"
            % (len(report.files), size, report.zip_path)
        )

    def _on_config_import(self) -> None:
        """选包 → 报摘要 → 就地确认 → 覆盖。

        摘要先于确认：用户手里可能有好几个包，而文件名里的时间戳不说明内容。
        确认位给 ``danger`` —— 按下去就是覆盖，与「恢复系统状态」同一档。
        """
        start = str(paths.APP_SUPPORT / "backups")
        path, _filter = QFileDialog.getOpenFileName(
            self, "选择配置包", start, "配置包 (*.zip)"
        )
        if not path:
            return
        summary = config_bundle.describe_bundle(Path(path))
        if summary is None:
            self._say_data("这不是 BarKit 的配置包（读不出清单）。", True)
            return
        fb = self._data_feedback
        if fb is None:
            # 没有确认口就没人确认。宁可不导入，也不无声地覆盖整份配置
            # （与「恢复系统状态」同款纪律）。
            log.warning("维护页没有反馈槽位，取消导入配置：%s", path)
            return
        fb.ask(
            "将用这个包覆盖当前配置：\n%s\n\n导入前会自动备份当前配置。"
            "完成后需要重启 BarKit 才生效。" % summary,
            "确认导入",
            lambda: self._run_config_import(path),
            confirm_kind="danger",
        )

    def _run_config_import(self, path: str) -> None:
        """执行导入并把结果说清 —— 失败时连「已经写到第几个」一起给。"""
        ok, note = config_bundle.import_bundle(Path(path))
        if not ok:
            self._say_data(note, True)
            return
        self._say_data("%s。重启 BarKit 后生效。" % note)

    def _forget_data_feedback(self, fb: FeedbackBar) -> None:
        """槽位所属页面被销毁时摘掉引用（同 `_forget_autostart_feedback`）。"""
        if self._data_feedback is fb:
            self._data_feedback = None

    def _say_data(self, text: str, error: bool = False) -> None:
        """把一句结果写进「数据」卡那条槽；没槽位（窗口已销毁）时只记日志。"""
        if error:
            log.error("%s", text)
            if self._data_feedback is not None:
                self._data_feedback.error(text)
        else:
            log.info("%s", text)
            if self._data_feedback is not None:
                self._data_feedback.info(text)

    def _build_log_card(self, parent: QWidget) -> QWidget:
        """「日志」卡：主程序日志级别 ＋ 四份日志各一枚按钮 ＋ 一枚「打开日志目录」。

        **级别那一格**（2026-09-28 起）：与开机自启 / 权限 / 数据同页，都是「装一次
        基本不动、排查时才来」的框架级开关，不属于任何单个组件——放进组件页会出现
        「同一个框架开关在 N 个组件页各有一份」。落盘经 ``Config.set_log_level``；
        写回首项的空串即删键，让 ``BARKIT_LOG_LEVEL`` 这一道入口继续生效。改档立即经
        ``logging_setup.set_level`` 生效（只调级别、不重建文件 handler；从调试档切回时
        控制台那一份也一并摘掉）。

        **按钮那几枚**（2026-10-13 起）：用户 2026-10-13 报「非命令行启动时控制台输出
        没地方看」——答案是那四份日志文件，但那时界面上**一个入口都没有**，只能靠人
        记住路径。分类与落点归 :mod:`barkit.shell.log_sources`（一处定义），本方法只
        负责把它们画出来。

        **为什么分两排**：贴主题后一枚四字按钮 78px，四枚一排要 348px，而设置窗窄到
        最小（``SettingsWindow.MIN_SIZE`` 的 520）时内容区只剩约 334px ⇒ 会撑出横向
        滚动条。故三枚 + 两枚；排布按**分类**写死（不是按列表下标切），加一份日志时
        必须自己决定它归哪一排。
        """
        card, lay = settings_section("日志", parent=parent)
        fb = FeedbackBar(card)
        fb.destroyed.connect(lambda *_a, _fb=fb: self._forget_log_feedback(_fb))
        self._log_feedback = fb

        current = self._config.log_level() if self._config is not None else ""
        self._log_level_selector = ChoiceSelector(
            _LOG_LEVEL_CHOICES, current, parent=card
        )
        self._log_level_selector.selected.connect(self._on_log_level_selected)
        lay.addLayout(form_row("日志级别", self._log_level_selector, card))
        lay.addWidget(self._note(
            "只管主程序这份日志（~/Library/Logs/barkit/barkit.log），守护进程那份"
            "不看这个档。选「跟随启动环境」时由启动方式决定——终端跑带 "
            "BARKIT_LOG_LEVEL ＝ 临时开一次，常驻实例才真正用这里选的档。「调试」"
            "会写很多，排查完记得调回去。",
        ))

        sources = {src.key: src for src in log_sources()}
        say_source = lambda text, error=False: self._say_log(text, error=error)
        for keys in (("app", "helper", "tunnel"), ("crash",)):
            row = QHBoxLayout()
            row.setSpacing(metrics.GAP_SM)
            for key in keys:
                src = sources[key]
                row.addWidget(reveal_button(src.label, src.target, card, say=say_source))
            if "crash" in keys:
                # 与分类按钮同一排：它们都是「菜单栏上的一个动作」，而四份日志各自
                # 住在三个不同目录 ⇒ 单说「打开目录」得先讲是哪一份的目录。
                row.addWidget(reveal_button(
                    "打开日志目录", lambda: paths.LOG_DIR, card,
                    say=say_source, what="日志目录",
                ))
            row.addStretch(1)
            lay.addLayout(row)

        lay.addWidget(self._note(
            "每一枚在访达里选中对应的那一份；还没有就落在它该在的目录上。"
            "崩溃报告是 macOS 写的，点了选中最新一份 Python-*.ips（那是本程序这一族，"
            "同目录还有别的进程的）。「打开日志目录」开的是应用日志那个目录；"
            "守护与隧道那两份住在各自的目录里。",
        ))
        lay.addWidget(fb)
        return card

    def _on_log_level_selected(self, level: str) -> None:
        """落盘 ＋ 立即生效（同托盘项：程序内写配置不发 ``changed``，故直接调 setter）。

        ``level`` 为空串 ⇒ 「跟随启动环境」⇒ 删键，并把实时级别回落到启动基准
        （``resolve_level(None)`` 即出厂 INFO）。
        """
        if self._config is not None:
            self._config.set_log_level(level)
        logging_setup.set_level(logging_setup.resolve_level(level or None))
        label = _LOG_LEVEL_LABELS.get(level, level or "跟随启动环境")
        suffix = "（已回落跟随启动环境）" if not level else ""
        self._say_log(f"日志级别已切到「{label}」{suffix}")

    def _say_log(self, text: str, *, error: bool = False) -> None:
        """把切档结果写进「日志」卡那条槽；没槽位（窗口已销毁）时只记日志。"""
        if error:
            log.error("%s", text)
            if self._log_feedback is not None:
                self._log_feedback.error(text)
        else:
            log.info("%s", text)
            if self._log_feedback is not None:
                self._log_feedback.info(text)

    def _forget_log_feedback(self, fb: FeedbackBar) -> None:
        """槽位所属页面被销毁时摘掉引用（同 ``_forget_data_feedback``）。"""
        if self._log_feedback is fb:
            self._log_feedback = None

    def _check_accessibility(self) -> None:
        """探测辅助功能授权，结果写本页行内槽（零模态）。

        「问不出来」与「没授权」是**两件事**（``available()`` 管前者），不报同一句：
        本机没有 ApplicationServices 时报「未授予」，等于把用户支去系统设置里白翻
        一遍列表。
        """
        if not available():
            self._say_permission(
                "问不出辅助功能授权状态：本机没有 ApplicationServices。", error=True
            )
            return
        if accessibility_trusted():
            self._say_permission("辅助功能权限已授予。")
            return
        self._say_permission(ACCESSIBILITY_GUIDE)

    def _open_accessibility_pane(self) -> None:
        """打开系统设置 →「隐私与安全性」→「辅助功能」。"""
        open_accessibility_pane()
        self._say_permission(
            "已打开系统设置「辅助功能」面板，请在列表里勾选运行 BarKit 的程序。"
        )

    def _request_accessibility(self) -> None:
        """触发系统授权弹窗（**每进程首次调用才弹**），随后复探一次状态。

        复探是必需的：那次系统弹窗本身就是一次授权机会，用户可能当场就点了
        「打开系统设置」并勾上 —— 再问一次比让用户自己回来点一遍「检查」省事。
        """
        request_accessibility()
        self._check_accessibility()

    def _say_permission(self, text: str, *, error: bool = False) -> None:
        """把一句权限结果写进「维护」页「权限」卡的槽；没有槽位（窗口已销毁）时只记日志。

        与 `_say_info` / `_say_error` 同款，只是落在**本页**那条槽上 —— 一页一条槽，
        消息不该跑到别的页去。``error`` 一路照记 ERROR 日志：没界面时它是唯一线索。
        """
        if error:
            log.error("%s", text)
        else:
            log.info("%s", text)
        fb = self._permissions_feedback
        if fb is None:
            return
        if error:
            fb.error(text)
        else:
            fb.info(text)
