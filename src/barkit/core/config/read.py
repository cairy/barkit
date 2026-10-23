"""``_ReadMixin``：三层合并、只读消费方、点路径取值与 ``data`` 快照。

``config.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import yaml

from .consts import log
from .schema import ConfigError, deep_merge, validate
from .store import _load_yaml, atomic_write


class _ReadMixin:
    """读取与热重载：三层合并（出厂 / 插件 schema / 用户差异）、只读消费方不落地、点路径取值与 ``data`` 快照。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 读取 ----------

    def _reload_from_disk(self) -> None:
        """读取并合并配置；失败保留当前快照并广播 error。

        合并顺序（§9.5 配置单一真源）：
            runtime = deep_merge(
                deep_merge(framework_defaults, plugin_defaults),  # 内存层，不落盘
                user_config,                                     # config.yaml 差异层
            )
        - framework_defaults：config.default.yaml 只保留框架级（app.*/hotkeys.*/
          plugins.<id>.enabled）。
        - plugin_defaults：各插件 ``config_schema().defaults()`` 合成（运行时层）。
        - user_config：用户只写差异项。
        """
        try:
            framework_defaults = validate(_load_yaml(self.default_path))
            plugin_defaults = {"plugins": {
                pid: schema.defaults() for pid, schema in self._plugin_schemas.items()
            }}
            user = self._read_user()
            merged = deep_merge(framework_defaults, plugin_defaults)
            self._data = deep_merge(merged, validate(user))
            self._mtime = self.user_path.stat().st_mtime if self.user_path.exists() else None
        except (ConfigError, yaml.YAMLError, OSError) as e:
            if hasattr(self, "_data"):
                log.warning("配置加载失败，沿用上一份有效快照：%s", e)
                self.error.emit(str(e))
            else:
                raise  # 首次加载就失败属于致命错误

    def _read_user(self) -> dict:
        if not self.user_path.exists():
            if not self._create_user_file:
                return {}          # 只读消费方：缺就当空差异层，不落地
            atomic_write(self.user_path, "# BarKit 用户配置：只写与默认值不同的差异项\n# 默认值见仓库 config.default.yaml\n{}\n")
        return _load_yaml(self.user_path)

    @property
    def data(self) -> dict:
        return self._data

    def get(self, dotted: str, default=None):
        """点路径取值，如 get("plugins.recognize.enabled")。"""
        node = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node
