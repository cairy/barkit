"""``_StateMixin`` —— 组件页页首「状态机」段：优先级与运行期当前值。

``settings.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Callable,
)

from PySide6.QtWidgets import QWidget

from barkit.shell.state_machines_section import StateMachinesSection

if TYPE_CHECKING:
    from barkit.contribution.specs import StateReport

from .constants import log


class _StateMixin:
    """组件页页首「状态机」段：优先级与运行期当前值。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    def _build_state_section(self, plugin_id: str) -> "QWidget | None":
        """该组件声明的状态机段；没声明 ⇒ ``None``（调用方不占版面）。"""
        specs = self._state_specs_of(plugin_id)
        if not specs:
            return None
        section = StateMachinesSection(
            specs=specs,
            get_priorities=self._state_priorities_of(plugin_id),
            # **传方法引用，不传 `self._state_provider` 这个字段的当时值**：
            # 分页在 `__init__` 里就建好了，而取数口是装配器随后 `set_state_provider`
            # 注入的 —— 先把值取出来存进段里，等于永远把构造那一刻的 None 钉在了段上，
            # `refresh_state_values()` 再刷也只是刷一个「没有取数口」的段。
            # 后果就是每台机的「当前」一栏永远停在占位文案（2026-09-21 用户上报
            # 的「当前 —」）。`stale` 同病（它在 `set_state_stale` 里注入）。
            current=self._current_report,
            stale=self._is_stale,
            # 没接配置 ⇒ 不挂写回调 ⇒ 状态机段不出拖拽手柄（而不是拖了弹回去）
            on_priority=(
                self._on_state_priority(plugin_id) if self._config is not None else None
            ),
        )
        self._state_sections.append(section)
        return section

    def _state_specs_of(self, plugin_id: str) -> list:
        """该组件声明的状态机（**保声明顺序**）；没有 ⇒ 空列表 ⇒ 不插段。"""
        if self._registry is None:
            return []
        try:
            return [s for s in self._registry.states() if s.plugin_id == plugin_id]
        except Exception:  # noqa: BLE001
            log.exception("读取 %s 的状态机声明失败，本次不插状态机段", plugin_id)
            return []

    def _state_priorities_of(self, plugin_id: str) -> "Callable[[], dict]":
        """现读该组件写过的状态机优先级（**每次重排都重读**，不缓存）。"""
        def read() -> dict:
            if self._config is None:
                return {}
            try:
                return self._config.plugin_state_priorities(plugin_id)
            except Exception:  # noqa: BLE001
                log.exception("读取 %s 的状态机优先级失败，按声明顺序处理", plugin_id)
                return {}

        return read

    def _on_state_priority(
        self, plugin_id: str
    ) -> "Callable[[str, int | None], None]":
        """写某台状态机的优先级；写完叫装配器重算代表态（程序内写配置不发 ``changed``）。"""
        def write(key: str, value: "int | None") -> None:
            if self._config is None:
                return
            try:
                self._config.set_plugin_state_priority(plugin_id, key, value)
            except Exception:  # noqa: BLE001
                log.exception("写入 %s.%s 的状态机优先级失败", plugin_id, key)
                return
            if self._on_state_priority_changed is not None:
                self._on_state_priority_changed()

        return write

    def _current_report(self, topic: str) -> "StateReport | None":
        """现读当前报告 —— 段拿到的是**这个方法**，不是取数口的引用。

        两个都是「取数口没接上就退化成没有报告」，区别在于**问的时机**：把
        ``self._state_provider`` 直接传给段，取的是构造那一刻的值（``None``），
        之后 ``set_state_provider`` 怎么注都进不去段里 —— 这正是「当前 —」那个 bug。
        """
        if self._state_provider is None:
            return None
        return self._state_provider(topic)

    def _is_stale(self, topic: str) -> bool:
        """现问「该 topic 是否失联」；没注入 ⇒ 一律按未失联（旧行为）。"""
        if self._state_stale is None:
            return False
        return bool(self._state_stale(topic))

    def set_state_provider(
        self, provider: "Callable[[str], StateReport | None] | None"
    ) -> None:
        """注入运行期「当前生效值」的取数口：``topic -> StateReport | None``。

        由装配器注入 —— 设置窗不认识 scheduler / 事件总线，也不该认识。
        已建好的段**随即刷一遍**（段持有的是 :meth:`_current_report`，故这一刷真读得到）。
        """
        self._state_provider = provider
        self.refresh_state_values()

    def set_state_stale(self, provider: "Callable[[str], bool] | None") -> None:
        """注入「该 topic 是否已失联」；不注入 ⇒ 一律按未失联（旧行为）。"""
        self._state_stale = provider
        self.refresh_state_values()

    def refresh_state_values(self) -> None:
        """把所有已构建状态机段的「当前生效值」刷一遍（状态事件时由装配器调用）。"""
        for section in self._state_sections:
            try:
                section.refresh_current()
            except Exception:  # noqa: BLE001
                log.exception("刷新状态机当前值失败（已跳过该段）")
