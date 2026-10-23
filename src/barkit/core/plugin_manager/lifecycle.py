"""启用 / 禁用两个开关及其联动。

``plugin_manager.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations


from .consts import log


class _LifecycleMixin:
    """启用 / 禁用两个开关，以及它们各自要联动的那一串（起停、注册、订阅、面板）。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 启用 / 禁用 ----------

    def enable(self, plugin_id: str) -> bool:
        """运行期启用插件（无需重启）。已在内存激活则幂等返回。"""
        if plugin_id in self._registry.plugins:
            self._config.set_plugin_enabled(plugin_id, True)
            return True
        cls = self._registry.discovered_classes.get(plugin_id)
        if cls is None:
            log.error("启用失败：未发现插件 %s", plugin_id)
            return False
        try:
            plugin = cls(self._registry.ctx)
        except Exception:
            log.exception("启用插件 %s 实例化失败", plugin_id)
            return False
        if not plugin.id:
            plugin.id = plugin_id
        try:
            plugin.start()
        except Exception:
            log.exception("启用插件 %s start 失败", plugin_id)
            return False
        self._registry.plugins[plugin.id] = plugin
        # 编排表：登记实例 + 重算（不落盘 —— 结果是经会话声明出去的一帧意图）
        self._registry.register_instance(plugin)
        self._registry.sync_helper_tasks()
        # 长驻服务托管在 start() 之后（与 Registry.load_all 同序）
        self._registry.start_services(plugin)
        # 两种 run_mode 都登记（2026-09-22 改）：按需型不订阅节拍，但要能被事件点火。
        self._scheduler.add_plugin(plugin)
        self._contributions.activate(plugin)
        self._register_hotkeys(plugin)
        self._subscribe_states(plugin)
        self._subscribe_subjects(plugin)
        self._subscribe_topics(plugin)
        self._refresh_panel()
        self._config.set_plugin_enabled(plugin.id, True)
        self._notify_changed()
        log.info("插件已启用：%s", plugin.id)
        return True

    def disable(self, plugin_id: str) -> bool:
        """运行期禁用插件（无需重启）。"""
        plugin = self._registry.plugins.get(plugin_id)
        if plugin is not None:
            # 反订阅状态 / 观测 / 系统事件须在 deactivate / 移除实例前拿到插件对象
            self._unsubscribe_states(plugin)
            self._unsubscribe_subjects(plugin)
            self._unsubscribe_topics(plugin)
            # 顺序铁律：先停长驻服务（线程/端口释放），再 plugin.stop()
            self._registry.stop_services(plugin)
            try:
                plugin.stop()
            except Exception:
                log.exception("禁用插件 %s stop 异常", plugin.id)
            # 无条件摘除（2026-09-22 改）：按需型也在调度器里（为的是能被事件点火），
            # 漏摘会让已禁用的组件继续被 mark_dirty 叫醒干活 —— §8.2.4 的无泄漏正是指这个。
            self._scheduler.remove_plugin(plugin.id)
            self._unregister_hotkeys(plugin)
            self._contributions.deactivate(plugin.id)
            self._registry.plugins.pop(plugin.id, None)
            # 后端副作用清理（如 route_guard 撤回路由意图、停 helper 路由）；
            # 仅由 disable 触发，不影响 app 退出时的 stop()。单插件失败不阻断其余。
            try:
                plugin.on_disabled()
            except Exception:
                log.exception("插件 %s on_disabled 异常", plugin.id)
        else:
            # 早已禁用 / 从未加载：仅确保贡献集合与配置一致
            self._contributions.deactivate(plugin_id)
        self._refresh_panel()
        self._config.set_plugin_enabled(plugin_id, False)
        self._notify_changed()
        log.info("插件已禁用：%s", plugin_id)
        return True
