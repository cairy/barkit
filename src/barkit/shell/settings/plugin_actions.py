"""``_PluginActionsMixin`` —— 插件动作：安装 / 导出 / 导入 / 启停 / 卸载 / 排序。

``settings.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtWidgets import QFileDialog

from barkit.core.plugin_manager import version_label
from barkit.shell.ui_kit import Switch

from .constants import log
from .helpers import _apply_chip, _poll_text


class _PluginActionsMixin:
    """插件动作：安装 / 导出 / 导入 / 启停 / 卸载 / 排序。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    def _on_plugin_poll(self, plugin_id: str, data: str, default: float, name: str) -> None:
        """改运行间隔：落盘 ＋ 让调度器**当场重挂**节拍。

        选「默认」那一项送 ``None`` = **删键**（不是写回默认值）——理由见
        ``core/config.py::set_plugin_poll``。

        两件事都在 ``manager.set_plugin_poll`` 里，不由本窗分两步调：配置层与调度器
        只做一半，就会出现「界面上改了、实际还按老节拍跑」这种看不见的空转。
        """
        if self._manager is None:
            return
        seconds = float(data) if data else None
        self._manager.set_plugin_poll(plugin_id, seconds)
        what = (
            f"恢复为组件建议的 {_poll_text(default)}"
            if seconds is None
            else f"改为 {_poll_text(seconds)}"
        )
        self._zone_say(plugin_id, "info", f"「{name}」运行间隔已{what}，立即生效。")

    def _on_slot_chip(self, plugin_id: str, slot: str, btn) -> None:
        """点显示位芯片：翻转 → 落盘 → 立即重画三处。

        只写配置，**不碰插件实例** —— 隐藏 ≠ 停用（决议 D4 约束 ①）。配置写完由装配器
        的回调重画（程序内写配置不发 ``changed``，故必须显式叫一声）。
        """
        if self._config is None:
            return
        want = not self._config.plugin_ui_visible(plugin_id, slot)
        self._config.set_plugin_ui_visible(plugin_id, slot, want)
        _apply_chip(btn, slot, want)
        if self._on_display_changed is not None:
            self._on_display_changed()

    def _on_plugin_reorder(self, plugin_id: str, to_index: int) -> bool:
        """落库回调（``OrderEditor.on_reorder``）：返回 False 时编辑器自行回滚。

        ``to_index`` 是 pid 移除自身后剩余序列中的插入下标，与
        ``PluginManager.reorder_plugin`` 语义一致；落库成功后 manager 会广播
        ``on_plugins_changed``（三处渲染同源刷新），无需本窗再手动重建行。
        """
        if self._manager is None:
            return False
        return bool(self._manager.reorder_plugin(plugin_id, to_index))

    # ---------- 安装 / 覆盖外部插件（决议 D5） ----------

    def _on_plugin_install_dir(self) -> None:
        """从**目录**安装插件。

        ``QFileDialog`` 的父传 ``self``：原生选择器挂在设置窗上，不另开一层事件循环。
        它不属于「插件 UI 零模态」那条红线 —— 那条禁的是 ``exec()`` 造出的**嵌套**
        事件循环（理由见 ``ui_kit.FeedbackBar``）：设置窗是原生置顶窗，从它内部再弹
        一个模态框才会卡住。
        """
        path = QFileDialog.getExistingDirectory(self, "选择插件目录")
        if path:
            self._install_plugin(path)

    def _on_plugin_install_zip(self) -> None:
        """从**压缩包**安装插件（包内套一层目录也认，见 installer.extract_package）。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择插件包", "", "插件包 (*.zip)"
        )
        if path:
            self._install_plugin(path)

    def _display_name(self, plugin_id: str) -> str:
        """plugin_id → 组件显示名；取不到回落 id（fail-safe）。"""
        manager = self._manager
        if manager is None:
            return plugin_id
        try:
            infos = manager.list_plugins()
        except Exception:  # noqa: BLE001
            return plugin_id
        for info in infos:
            if info.id == plugin_id:
                return info.name or plugin_id
        return plugin_id

    def _install_plugin(self, path: str) -> None:
        """安装 / 覆盖外部插件：先预检，要覆盖就在行内问一句，答完才真正落盘。

        **两段式**的理由：行内确认是**异步**的（点了按钮才走），而编排层的确认回调
        只能是同步的 —— 合成一段就只剩两条路：弹模态框，或者不问就覆盖。故这里先取
        事实（装谁、机器上是哪个版本、包里是哪个版本），要覆盖就挂一条待确认。
        """
        if self._manager is None:
            return
        preview, reason = self._manager.preview_install(path)
        if preview is None:
            self._say_error(f"安装失败：{reason}")
            return
        if preview.installed_version is None:
            self._run_install(path)        # 首次安装：没有既有状态可破坏，不问
            return
        fb = self._plugin_feedback
        if fb is None:
            # 破坏性动作拿不到槽位就**拒绝执行**（与各组件同款纪律）：没有确认口就
            # 等于没人确认。宁可不装，也不无声覆盖用户的插件。
            log.warning("插件页没有反馈槽位，取消覆盖安装 %s", preview.plugin_id)
            return
        fb.ask(
            f"已安装「{self._display_name(preview.plugin_id)}」（{version_label(preview.installed_version)}），"
            f"用新包（{version_label(preview.version)}）覆盖它？",
            "覆盖",
            lambda p=path: self._run_install(p),
            confirm_kind="danger",
        )

    def _run_install(self, path: str) -> None:
        """真正执行安装，结果写进反馈槽。"""
        if self._manager is None:
            return
        ok, msg = self._manager.install(path)
        if ok:
            self._say_info(msg)
            self._rebuild_plugin_list()
        else:
            self._say_error(f"安装失败：{msg}")

    def notify_uninstall_settled(self, plugin_id: str, problem: str) -> None:
        """卸载收尾的**异步**结论（``PluginManager.on_uninstall_settled``）。

        有外部消费方的插件，卸载的收尾（删数据文件 / 摘 helper task / 遗忘）要等那个
        进程确认收敛才执行，所以点完「删除」那一刻还出不来结论 —— 本方法就是它的落点。
        ``problem`` 非空＝收尾**没做成**（文件与插件一律保留：不假装成功，用户可重试）。

        调用时机由 manager 保证：**先重建分页、再调这里**，故消息落在新页的新槽位上。
        """
        if problem:
            self._say_error(f"「{self._display_name(plugin_id)}」卸载未完成：{problem}")
        else:
            self._say_info(f"「{self._display_name(plugin_id)}」已卸载。")

    def _on_plugin_export(self, plugin_id: str) -> None:
        """导出组件数据：结果写进行内反馈槽（零模态 —— 见 ``ui_kit.FeedbackBar``）。

        ``QFileDialog`` 保留不动：它是原生选择器、不是模态框，不另开嵌套事件循环
        （同 ``_on_plugin_install_dir`` 的判据）。
        """
        if self._manager is None:
            return
        dest = QFileDialog.getExistingDirectory(self, "选择导出目录")
        if not dest:
            return
        path = self._manager.export_plugin(plugin_id, dest)
        if path is not None:
            self._say_info(f"已导出到：{path}")
        else:
            self._say_error("导出失败，请重试。")

    def _on_plugin_import(self) -> None:
        """恢复插件数据：结果写进行内反馈槽（零模态）。"""
        if self._manager is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "选择插件导出包", "", "BarKit 插件导出包 (*.zip)"
        )
        if not path:
            return
        ok, err = self._manager.import_zip(path)
        if ok:
            self._say_info("插件数据已恢复。")
            self._rebuild_plugin_list()
        else:
            self._say_error(err or "导入失败，请重试。")

    def _on_plugin_toggle(self, plugin_id: str, sw: Switch) -> None:
        """翻启停开关：成功才翻，失败**必须出声**。

        ``Switch`` 自己不改状态（只在用户点击时发 ``clicked``），所以失败时开关
        停在原状 —— 那正是最坏的反馈：界面看着像「点了没反应」，而日志是唯一线索。
        本页别的动作（安装 / 卸载 / 导出 / 导入）都往行内槽写结果，启停不该是唯一
        一个沉默的。``enable`` 在「实例化抛异常 / ``start()`` 抛异常」时返回 False；
        ``disable`` 当前恒真（它的失败都吞在日志里），故本分支只可能为启用服务。
        """
        if self._manager is None:
            return
        new_enabled = not sw.isChecked()
        ok = (
            self._manager.enable(plugin_id)
            if new_enabled
            else self._manager.disable(plugin_id)
        )
        if ok:
            sw.setChecked(new_enabled)
            return
        self._say_error(
            f"「{self._display_name(plugin_id)}」{'启用' if new_enabled else '停用'}失败，请重试。"
        )

    def _on_plugin_uninstall(self, plugin_id: str) -> None:
        """卸载外部插件：**先在行内问一句**，点下「卸载」才真删（零模态）。

        与 ``_install_plugin`` 的覆盖确认同款**两段式** —— ``FeedbackBar.ask`` 的确认
        是异步的（点了按钮才走），而这里是「先问后删」，故拆成两段。

        拿不到槽位就**拒绝执行**（与覆盖安装同一条纪律）：没有确认口 ＝ 没人确认，
        而删目录不可恢复，宁可不删。改前这里是 ``QMessageBox.question``：设置窗钉在
        原生置顶层级，从它内部弹模态框会另开一条嵌套事件循环（实测崩溃记录见
        ``ui_kit.FeedbackBar`` 的 docstring），故一并收口。
        """
        if self._manager is None:
            return
        fb = self._plugin_feedback
        if fb is None:
            log.warning("插件页没有反馈槽位，取消卸载 %s", plugin_id)
            return
        fb.ask(
            f"确定卸载外部插件「{self._display_name(plugin_id)}」？"
            f"将从磁盘删除其目录并移除配置（不可恢复）。",
            "卸载",
            lambda p=plugin_id: self._do_plugin_uninstall(p),
            confirm_kind="danger",
        )

    def _do_plugin_uninstall(self, plugin_id: str) -> None:
        """真正执行卸载（确认之后才走到这里），随即重建列表。"""
        if self._manager is None:
            return
        self._manager.uninstall(plugin_id)
        self._rebuild_plugin_list()

    def _say_action(self, text: str, *, error: bool = False) -> None:
        """把一句动作结局落进本页槽 —— ``_wire_button`` 那个 ``say`` 的形状。

        info / error 各走各的口（``_say_info`` / ``_say_error``），本方法只负责把签名
        对齐成收口那一层只认的一种：``say(text, error=...)``。页面槽位入口不同
        （``_zone_say`` / ``_say_permission`` 各不相同），各页照自己的情形换一个
        ``say`` 交给 ``_wire_button`` 即可。
        """
        if error:
            self._say_error(text)
        else:
            self._say_info(text)
