"""组件热键的注册与撤销。

``plugin_manager.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from barkit.core.plugin import Plugin

from .consts import log


class _HotkeyMixin:
    """本组件热键的注册与撤销。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 热键 ----------

    def _register_hotkeys(self, plugin: "Plugin") -> None:
        for spec in self._contributions.hotkeys():
            if spec.plugin_id != plugin.id:
                continue
            combo = self._config.get(f"hotkeys.{spec.name}", spec.default_combo)
            if combo and not self._hotkeys.register(spec.name, combo, spec.callback):
                log.warning("热键 %s 注册失败", spec.name)

    def _unregister_hotkeys(self, plugin: "Plugin") -> None:
        for spec in self._contributions.hotkeys():
            if spec.plugin_id != plugin.id:
                continue
            self._hotkeys.unregister(spec.name)
