"""状态 / 守护观测 / 系统事件三类订阅的登记与撤销。

``plugin_manager.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from functools import partial
from typing import Callable

from barkit.contribution.specs import StateReport
from barkit.core.events import HELPER_OBSERVED_TOPIC
from barkit.core.plugin import Plugin

from .consts import log


class _SubscriptionMixin:
    """三类订阅（状态 / 守护观测 / 系统事件）的登记、撤销与「本插件声明了哪些」的取数口。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 状态订阅（U2 StateSpec） ----------

    def _subscribe_states(self, plugin: "Plugin") -> None:
        # v3：经注册中心收集的 StateSpec（按 plugin_id 归属）
        for spec in self._contributions.states():
            if spec.plugin_id == plugin.id:
                self._sub_state(plugin.id, spec)

    def _sub_state(self, pid: str, spec) -> None:
        handler = partial(self._on_state_update, spec)
        self._events.subscribe(spec.topic, handler)
        self._state_handlers[(pid, spec.topic)] = handler
        # 首屏画**起点那一格**（决议 D13：``initial`` 是取值名，不是读现场的工厂），
        # 留空取声明的第一个取值；真实读数由首轮 tick 补上，不等用户打开面板。
        start = spec.initial_value
        if start:
            try:
                self._on_state_update(spec, StateReport(start))
            except Exception:  # noqa: BLE001 —— 单插件首屏绘制失败不应中断其余
                log.exception("插件 %s 状态初始绘制失败", pid)

    def _unsubscribe_states(self, plugin: "Plugin") -> None:
        specs = [s for s in self._contributions.states() if s.plugin_id == plugin.id]
        for spec in specs:
            handler = self._state_handlers.pop((plugin.id, spec.topic), None)
            if handler is not None:
                self._events.unsubscribe(spec.topic, handler)

    # ---------- 守护观测订阅（2026-09-29 起，取代文件事件订阅） ----------

    def _subscribe_subjects(self, plugin: "Plugin") -> None:
        """按 ``Plugin.subjects()`` 声明订阅守护观测，并把推送接到插件回调。

        两条动作，缺一不可：

        1. ``plugin.subscribe(声明)`` —— 把「要这几个领域」经会话发给守护（未连接时
           记在框架侧，连上即补发）。**订阅即立刻推一份当前的**，所以面板一打开就有
           事实，不必等下一次变化；
        2. 订阅总线上的 ``system.helper.observed``，框架侧按声明过滤，只把本插件声明过
           的领域转交 ``plugin.on_observed``。

        未接线（轻量测试桩：``ctx.helper`` 为 None）时 ``subscribe`` 返回 False、
        总线照常挂着 —— 插件收不到事实，如实反映「没通道」，不假装。
        """
        if plugin.id in self._subject_handlers:
            return
        declared = self._declared_subjects(plugin)
        if not declared:
            return
        plugin.subscribe(declared)
        registered = frozenset(declared)
        handler = partial(self._on_subject_push, plugin, registered)
        self._events.subscribe(HELPER_OBSERVED_TOPIC, handler)
        self._subject_handlers[plugin.id] = (handler, registered)
        log.debug("插件 %s 已订阅守护观测：%s", plugin.id, ", ".join(sorted(registered)))

    def _on_subject_push(self, plugin: "Plugin", declared: frozenset, event) -> None:
        subject = getattr(event, "subject", None)
        if subject not in declared:
            return
        try:
            plugin.on_observed(str(subject), getattr(event, "observed", None))
        except Exception:  # noqa: BLE001 —— 单插件异常不得拖垮事件分发
            log.exception("插件 %s 处理守护观测异常（已隔离）", plugin.id)

    def _unsubscribe_subjects(self, plugin: "Plugin") -> None:
        """撤销该插件的领域订阅：反订阅总线 + 撤掉会话里的订阅声明（§8.2.4 无泄漏）。"""
        entry = self._subject_handlers.pop(plugin.id, None)
        if entry is None:
            return
        handler, _registered = entry
        self._events.unsubscribe(HELPER_OBSERVED_TOPIC, handler)
        unsub = getattr(getattr(self._registry, "ctx", None), "helper", None)
        drop = getattr(unsub, "unsubscribe", None)
        if callable(drop):
            try:
                for subject in sorted(_registered):
                    drop(subject)
            except Exception:  # noqa: BLE001
                log.exception("插件 %s 撤销领域订阅失败", plugin.id)

    @staticmethod
    def _declared_subjects(plugin: "Plugin") -> list:
        """读插件声明（防御性）：抛异常按空清单处理，返回原样（去空值）。"""
        try:
            declared = plugin.subjects() or []
        except Exception:  # noqa: BLE001
            log.exception("插件 %s 声明守护观测失败，按空清单处理", plugin.id)
            return []
        return [s for s in declared if s]

    # ---------- 系统事件订阅（§8.3.1 通用订阅）----------

    def _subscribe_topics(self, plugin: "Plugin") -> None:
        """按 ``event_topics()`` 声明订阅系统事件，事件到达转交 ``on_system_event``。

        与文件订阅同款：框架**按声明过滤**（插件只收自己声明过的 topic），
        单插件回调异常被隔离，不影响其余订阅者。
        """
        if plugin.id in self._topic_handlers:
            return
        declared = self._declared_topics(plugin)
        if not declared:
            return
        entries: list[tuple[str, Callable]] = []
        for topic in declared:
            handler = partial(self._on_topic_event, plugin, topic)
            self._events.subscribe(topic, handler)
            entries.append((topic, handler))
        self._topic_handlers[plugin.id] = entries
        log.debug("插件 %s 已订阅系统事件：%s", plugin.id, ", ".join(declared))

    def _on_topic_event(self, plugin: "Plugin", topic: str, payload) -> None:
        try:
            plugin.on_system_event(topic, payload)
        except Exception:  # noqa: BLE001 —— 单插件异常不得拖垮事件分发
            log.exception("插件 %s 处理系统事件 %s 异常（已隔离）", plugin.id, topic)

    def _unsubscribe_topics(self, plugin: "Plugin") -> None:
        """撤销该插件的系统事件订阅（§8.2.4 无泄漏）。"""
        for topic, handler in self._topic_handlers.pop(plugin.id, []):
            self._events.unsubscribe(topic, handler)

    @staticmethod
    def _declared_topics(plugin: "Plugin") -> list[str]:
        """读 ``event_topics()`` 声明（防御性）：抛异常按空清单处理。"""
        try:
            declared = plugin.event_topics() or []
        except Exception:  # noqa: BLE001
            log.exception("插件 %s 声明系统事件订阅失败，按空清单处理", plugin.id)
            return []
        return [t for t in declared if t]

    def subscribe_all_active(self) -> None:
        """启动期：为所有已激活插件订阅 v3 状态并刷新面板。

        由 app 在构建好 contributions / panel 后调用一次，取代原先散落在
        ``main`` 里的芯片订阅 + 三处 ``set_*`` 注入。首屏绘制（需 ``_push_badge``
        已定义）由 app 在 ``status_item`` 与 ``_push_badge`` 就绪后额外触发，
        以保持与原启动顺序一致、避免提前引用未绑定的徽标回调。
        """
        for spec in self._contributions.states():
            handler = partial(self._on_state_update, spec)
            self._events.subscribe(spec.topic, handler)
            self._state_handlers[(spec.plugin_id, spec.topic)] = handler
        # 守护观测订阅（2026-09-29 起取代文件事件订阅）与系统事件订阅（§8.3.1）：
        # 只对**已加载**插件登记（禁用插件不订阅、也不收事件）
        for plugin in list(getattr(self._registry, "plugins", {}).values()):
            self._subscribe_subjects(plugin)
            self._subscribe_topics(plugin)
        self._refresh_panel()
