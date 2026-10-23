"""``Config`` 本体：类声明、两条 ``Signal``、``__init__``（唯一一处 ``super()``）；方法实现分散在包的其余子模块。

``config.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import (
    QObject,
    QTimer,
    Signal,
)

from barkit import paths

from .consts import DEFAULT_CONFIG_PATH
from .hotkeys import _HotkeyMixin
from .persist import _PersistMixin
from .plugins import _PluginMixin
from .read import _ReadMixin
from .shellcfg import _ShellCfgMixin
from .tuning import _TuningMixin
from .variables import _VariablesMixin


class Config(
    _ReadMixin,
    _PluginMixin,
    _ShellCfgMixin,
    _HotkeyMixin,
    _TuningMixin,
    _VariablesMixin,
    _PersistMixin,
    QObject,
):
    # 混合类按**职责**分开在包内各子模块，方法经 ``self`` 取用实例状态，
    # 故 MRO 顺序不影响变量解析（混合类之间无继承、无重名）。``QObject`` 排在
    # **最后**：PySide6 的元类按最靠右的 Qt 基类登记虚函数。
    # 本体只留**不许搬走**的三样：两条 ``Signal`` 声明（类体求值时按名解析）、
    # ``__init__``（含全类唯一一处 ``super()``）、以及类声明本身。

    """配置中心：合并后的完整配置 + mtime 热重载。"""

    changed = Signal(dict)        # 热重载成功，携带合并后的完整配置
    error = Signal(str)           # 校验/读取失败，携带错误信息

    def __init__(
        self,
        user_path: Path | None = None,
        default_path: Path | None = None,
        plugin_schemas: dict | None = None,
        create_user_file: bool = True,
    ) -> None:
        super().__init__()
        # 路径在**调用期**解析：默认参数若写成 ``paths.X`` 会在函数定义时求值，
        # 使 conftest 的模块属性补丁失效（曾因此让全链路测试直写真实用户目录）。
        self.user_path = user_path if user_path is not None else paths.APP_SUPPORT / "config.yaml"
        # 首次运行补一份空用户配置（GUI 之路的默认）。**只读消费方必须关掉它**：
        # 自检 / 工具类调用「读一下配置」不该改用户家目录（`barkit doctor` 即此事，
        # 见 内部 doctor 设计记录 §8 R1 的只读铁律）。
        self._create_user_file = bool(create_user_file)
        self.default_path = default_path if default_path is not None else DEFAULT_CONFIG_PATH
        # §9.5 插件默认值单一真源：各插件 ``config_schema().defaults()`` 合成层。
        # 装配根（app_shell.assemble）在 ``registry.load_all()`` 后注入。
        # **未注入 ⇒ 插件业务键的默认值整体不可见**（2026-09-20 起出厂文件已不含
        # 它们，只剩 ``plugins.<id>.enabled``）。这是既定行为、不是缺陷：凡读插件
        # 配置的代码都必须先注入。契约用例 →
        # ``tests/test_config.py::test_plugin_defaults_need_schema_injection``。
        self._plugin_schemas: dict = dict(plugin_schemas) if plugin_schemas else {}
        self._mtime: float | None = None
        self._watch_timer: QTimer | None = None
        self._reload_from_disk()
