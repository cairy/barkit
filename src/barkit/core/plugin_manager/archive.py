"""插件导出与导入 zip。

``plugin_manager.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations


from .consts import log


class _ArchiveMixin:
    """插件导出与导入 zip（§9.1）。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 导出 / 导入（§9.1，批次 6c） ----------

    def export_plugin(self, plugin_id: str, dest_dir, include_secret: bool = False):
        """导出插件用户数据（配置 + 热键 + 数据文件）；返回 zip 路径，失败 None。"""
        from barkit.core import exporter

        plugin = self._probe(plugin_id)
        if plugin is None:
            log.error("导出失败：未发现插件 %s", plugin_id)
            return None
        try:
            return exporter.export_plugin_zip(plugin, self._config, dest_dir, include_secret)
        except Exception:  # noqa: BLE001
            log.exception("导出插件 %s 失败", plugin_id)
            return None

    def import_zip(self, zip_path) -> "tuple[bool, str | None]":
        """把导出包恢复到已安装的插件（按包内 manifest.id 定位）。

        编排（§9.1：导入时框架保证插件处于 stop 状态）：已加载 → disable →
        导入 → 原为启用则重新 enable。返回 (是否成功, 错误)。
        """
        from barkit.core import exporter

        manifest = exporter.read_export_manifest(zip_path)
        if manifest is None:
            return False, "导出包缺失或 manifest 无法解析"
        pid = manifest.get("id")
        cls = self._registry.discovered_classes.get(pid)
        if cls is None:
            return False, f"插件 {pid} 未安装，请先安装后再导入"
        instance = self._registry.plugins.get(pid)
        was_enabled = instance is not None
        if instance is not None:
            self.disable(pid)
        try:
            plugin = self._probe(pid)
            if plugin is None:
                return False, f"插件 {pid} 无法实例化"
            return exporter.import_plugin_zip(plugin, self._config, zip_path)
        finally:
            if was_enabled:
                self.enable(pid)

    def _probe(self, plugin_id: str):
        """取可用实例：优先已加载实例，否则临时实例化（不 start）。"""
        instance = self._registry.plugins.get(plugin_id)
        if instance is not None:
            return instance
        cls = self._registry.discovered_classes.get(plugin_id)
        if cls is None:
            return None
        try:
            return cls(self._registry.ctx)
        except Exception:  # noqa: BLE001
            log.exception("插件 %s 临时实例化失败", plugin_id)
            return None
