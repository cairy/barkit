"""卸载七步与延迟收尾（排尾 / 收尾 / 超时 / 对账 / 清数据文件）。

``plugin_manager.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import shutil
import time
from functools import partial

from barkit.core.resources import (
    DeferredRemoval,
    PluginResources,
)
from barkit.contribution.specs import HotkeySpec
from barkit.core.plugin import Plugin
from barkit import paths

from .consts import log


class _UninstallMixin:
    """卸载七步与延迟收尾：排尾 / 收尾 / 超时 / 根二进制对账 /数据文件清理。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 卸载（仅外部，§9.1 卸载七步） ----------

    def uninstall(self, plugin_id: str) -> bool:
        """卸载外部插件（方案 §9.1 七步，框架统一编排）。

        1. system_mutations 确认   展示插件声明的系统变更，用户拒绝即中止（未做变更）
        2. disable()              stop → 反订阅 → 移除贡献 → 刷新 UI
        2b. teardown_services()   长驻服务卸载语义收尾（必须在 disable 之前）
        3. teardown()             插件复原本插件造成的系统状态（仅已加载实例）
        3.5 A 类对账（4b）        重算差集，回收无插件再需要的 root 二进制（§9.3）
        3.6 移除排序项            从 config.plugin_order 移除该 id（§6.4 防孤儿）
        4. 删数据文件             data_files − seed_files（seed 保留 = 已是用户数据）
        5. 删配置段               plugins.<id> 与 hotkeys.<name>*
        6. forget()               从发现结果移除（内部即摘 helper task 并重算表）
        7. 收尾                   无插件 needs_root → 提示可卸载守护（挂账）

        第 3 步的 **helper task 摘除**（方案原第 3 步）已随批次 4 · S3b-2 落在
        ``Registry.forget()`` → ``sync_helper_tasks()``；A 类对账（第 4b 步）在
        批次 4 · S4 接线（root 动作经 ``on_reap_root_binaries`` 回调转交，core
        不依赖 plugins）。

        **第 4~7 步可能延迟执行**（2026-09-17）：插件的 ``data_files`` 若被外部消费方
        （root helper）读取，则不能在这里当场删 —— 文件写完到消费方读到之间隔着它的一个
        轮次，当场删会让消费方读到「文件不存在」而按既有语义**冻结**，撤销就永远不生效。
        ``resources().deferred`` 声明了这件事时，第 4~7 步整段挂到框架心跳上，等插件的
        判据为真再跑（见 ``core/deferred_cleanup``）。故本方法的返回值语义是
        **「卸载流程已受理」**，不是「磁盘已清干净」：延迟那段的结果经
        ``on_uninstall_settled`` 事后通知。
        """
        if self._registry.source_of(plugin_id) != "external":
            log.warning("卸载被拒：内置插件 %s 不可删除", plugin_id)
            return False

        # 延迟收尾的**水位线**：必须在插件写期望态之前取（下面第 2b/3 步各写一次），
        # 否则消费方上一轮写回的「早已撤干净」会被误当作本轮的确认。
        watermark = time.time()

        # ---- 0. 前置收集：目录删除前必须拿到清单（之后 import 不可用） ----
        cls = self._registry.discovered_classes.get(plugin_id)
        instance = self._registry.plugins.get(plugin_id)
        resources = self._probe_resources(plugin_id, cls, instance)
        hotkey_names = self._probe_hotkey_names(plugin_id, cls, instance)

        # ---- 1. system_mutations 确认 ----
        mutations = resources.system_mutations if resources else []
        if mutations:
            if self.on_uninstall_confirm is not None:
                if not self.on_uninstall_confirm(plugin_id, list(mutations)):
                    log.info("卸载 %s 已被用户取消", plugin_id)
                    return False
            else:
                log.warning(
                    "卸载 %s 涉及系统变更（无确认 UI，继续执行）：%s",
                    plugin_id,
                    "；".join(m.description for m in mutations),
                )

        # ---- 2a. 长驻服务 teardown（必须在 disable 之前：disable 内的 stop_services
        #          只「停」，记录一丢就再也拿不到 service.teardown()） ----
        if instance is not None:
            self._registry.teardown_services(instance)

        # ---- 2b. disable（服务 stop + 反订阅 + 移除贡献 + on_disabled） ----
        self.disable(plugin_id)

        # ---- 3. teardown（复原系统状态；仅已加载实例——从未启动则无状态可复原） ----
        if instance is not None:
            try:
                instance.teardown()
            except Exception:  # noqa: BLE001 —— 单插件 teardown 失败不阻断清理
                log.exception("插件 %s teardown 异常", plugin_id)

        # ---- 3.5 A 类资产无状态对账（§9.3 第 4b 步）----
        # 与 3.6 都留在**当场**：它们与「消费方收敛」无关，且对账要弹 root 授权 ——
        # 让授权框在点完「删除」之后立刻出现，而不是几拍之后莫名弹出。
        self._reap_root_binaries()

        # ---- 3.6 移除 plugin_order 排序项 ----
        self._config.remove_from_plugin_order(plugin_id)

        # ---- 4~7. 收尾：需要等外部消费方时整段挂到心跳上 ----
        self._schedule_tail(plugin_id, resources, hotkey_names, watermark)
        return True

    def _schedule_tail(
        self,
        plugin_id: str,
        resources: "PluginResources | None",
        hotkey_names: list[str],
        watermark: float,
    ) -> None:
        """第 4~7 步：有 ``deferred`` 声明就等消费方收敛，否则当场跑。

        「未接线」（无心跳的轻量构造路径）时退回**当场跑** —— 与其把卸载悬在半空，
        不如保持接线前的行为，并把这件事记进日志。
        """
        deferred: DeferredRemoval | None = getattr(resources, "deferred", None)
        if deferred is None:
            self._finish_uninstall(plugin_id, resources, hotkey_names)
            return
        scheduled = self._deferred_cleanup.schedule(
            label=plugin_id,
            ready=deferred.ready,
            watermark=watermark,
            timeout_s=deferred.timeout_s,
            on_commit=partial(self._finish_uninstall, plugin_id, resources, hotkey_names),
            on_give_up=partial(self._on_tail_timeout, plugin_id),
        )
        if not scheduled:
            log.warning(
                "延迟收尾未接时钟（无框架心跳）：%s 当场收尾 —— 有外部消费方时可能撤不干净",
                plugin_id,
            )
            self._finish_uninstall(plugin_id, resources, hotkey_names)

    def _finish_uninstall(
        self,
        plugin_id: str,
        resources: "PluginResources | None",
        hotkey_names: list[str],
    ) -> None:
        """卸载收尾（第 4~7 步的实体；可能由心跳在几拍之后调用）。"""
        # ---- 4. 删外部目录 + 数据文件（data_files − seed_files） ----
        target = self._external_base / plugin_id
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        if resources is not None:
            self._remove_data_files(resources)

        # ---- 5. 删配置段（plugins.<id> + hotkeys.<name>） ----
        self._config.remove_plugin_section(plugin_id)
        try:
            self._config.remove_hotkeys(hotkey_names)
        except Exception:  # noqa: BLE001
            log.exception("插件 %s 热键配置清理失败", plugin_id)

        # ---- 6. forget ----
        self._registry.forget(plugin_id)

        # ---- 7. 收尾（批次 4 后接「无 needs_root 插件 → 提示可卸载 helper」） ----
        log.info("外部插件已卸载：%s", plugin_id)
        self._notify_changed()
        self._notify_settled(plugin_id, "")

    def _on_tail_timeout(self, plugin_id: str) -> None:
        """收尾超时：**什么都不撤**（文件 / 配置 / task 声明 / 发现结果全留）。

        往回退是安全的：期望态还在，消费方下次自然轮次仍会收敛；硬往前走才会把
        「撤销没做完」变成一个看不见的既成事实。所以这里只通知 + 记日志（ERROR
        已在 cleaner 里记过一次，含原因与上限）。
        """
        reason = "外部消费方未确认收敛（可能未运行）；路由/系统状态未撤，插件保留"
        self._notify_changed()
        self._notify_settled(plugin_id, reason)

    def _notify_settled(self, plugin_id: str, problem: str) -> None:
        """把延迟收尾的结论交给界面（**在 rebuild 之后**调用，否则消息落在旧控件上）。

        单独一个回调而不是复用 ``on_plugins_changed``：后者只表示「集合变了」，
        而这里要传的是「成了没有、没成是为什么」。
        """
        if self.on_uninstall_settled is None:
            return
        try:
            self.on_uninstall_settled(plugin_id, problem)
        except Exception:  # noqa: BLE001
            log.exception("卸载收尾回调 on_uninstall_settled 异常")

    # ---------- 卸载辅助 ----------

    def _reap_root_binaries(self) -> None:
        """第 4b 步：重算差集，回收「已部署但无插件再需要」的 A 类资产（§9.3）。

        wanted 口径 = **卸载后**仍已安装的插件声明并集（含 disabled / failed）；
        实现侧 `helper_client.reconcile_root_binaries` 只在差集非空时才弹授权，
        故本方法可无条件调用（禁用/启用不经过这里 —— 对账只在安装与卸载触发）。
        """
        try:
            still_wanted = self._registry.wanted_root_binaries()
        except Exception:  # noqa: BLE001
            log.exception("A 类对账：重算 wanted 失败，跳过回收")
            return
        if self.on_reap_root_binaries is None:
            log.info("A 类对账未接线，仅记录 wanted=%s（回收动作需 root 授权）",
                     sorted(still_wanted))
            return
        try:
            if not self.on_reap_root_binaries(still_wanted):
                log.warning("A 类资产回收未完成（用户取消授权或失败）")
        except Exception:  # noqa: BLE001
            log.exception("A 类资产回收回调抛异常")

    def _probe_resources(
        self, plugin_id: str, cls: "type[Plugin] | None", instance: "Plugin | None"
    ) -> PluginResources | None:
        """卸载前取资源清单：优先已加载实例，否则用临时实例读声明（不 start）。"""
        probe = instance
        if probe is None and cls is not None:
            try:
                probe = cls(self._registry.ctx)
            except Exception:  # noqa: BLE001
                log.warning("插件 %s 无法实例化以读取资源清单，按空清单处理", plugin_id)
                return None
        if probe is None:
            return None
        try:
            res = probe.resources()
        except Exception:  # noqa: BLE001
            log.exception("插件 %s resources() 抛异常，按空清单处理", plugin_id)
            return None
        return res if isinstance(res, PluginResources) else None

    def _probe_hotkey_names(
        self, plugin_id: str, cls: "type[Plugin] | None", instance: "Plugin | None"
    ) -> list[str]:
        """卸载前收集热键配置键名（hotkeys.<name>），供第 5 步清理。"""
        probe = instance
        if probe is None and cls is not None:
            try:
                probe = cls(self._registry.ctx)
            except Exception:  # noqa: BLE001
                return []
        if probe is None:
            return []
        try:
            specs = list(probe.hotkeys() or [])
        except Exception:  # noqa: BLE001
            return []
        return [
            s.name
            for s in specs
            if isinstance(s, HotkeySpec) and s.name
        ]

    @staticmethod
    def _remove_data_files(resources: PluginResources) -> None:
        """删除 data_files − seed_files（§9.1 第 4 步）。

        - seed_files 无论是否被改写一律保留（分类依据是清单声明，非运行期状态）；
        - 路径必须落在 APP_SUPPORT 之内（防绝对路径/穿越逃逸），越界即拒绝并记日志。
        """
        base = paths.APP_SUPPORT.resolve()
        seeds = {r for r in resources.seed_files if isinstance(r, str)}
        for rel in resources.data_files:
            if not isinstance(rel, str) or not rel:
                continue
            if rel in seeds:
                continue  # C 类种子：已是用户数据，卸载保留
            target = (base / rel).resolve()
            if target != base and base not in target.parents:
                log.warning("拒绝删除越界数据文件（%s 不在 APP_SUPPORT 内）", rel)
                continue
            try:
                if target.is_file():
                    target.unlink()
                elif target.is_dir():
                    shutil.rmtree(target)
            except OSError:
                log.exception("删除插件数据文件失败：%s", target)
