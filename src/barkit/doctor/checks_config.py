"""doctor/checks_config：B 组「配置」检查。

单一真源：解析与校验一律走 ``core/config`` 的 ``validate`` / ``deep_merge``
（``Config`` 构造期就会跑这两个），doctor **不另写一份校验规则**。

**只读**：``Config(create_user_file=False)``。默认行为下 Config 发现用户
``config.yaml`` 不存在时会写一份空模板（``core/config.py::_read_user``）——
自检不该改用户家目录，故显式关掉该副作用。
"""

from __future__ import annotations

from barkit import paths
from barkit.core import config as config_mod
from barkit.doctor.model import FAIL, PASS, Check, Result, run_checks

GROUP = "config"


def check_default_config() -> list[Result]:
    path = config_mod.DEFAULT_CONFIG_PATH
    if not path.exists():
        return [
            Result("B1", GROUP, "默认配置", FAIL, f"找不到 {path}",
                   "安装不完整（config.default.yaml 随仓库分发）：重装或 `git checkout` 找回")
        ]
    return [Result("B1", GROUP, "默认配置", PASS, f"{path} 存在")]


def _hint_for(exc: Exception, user_path) -> str:
    text = str(exc)
    if "schema 版本" in text:
        return "升级 BarKit，或删掉用户 config.yaml 让默认值重建后重启"
    if isinstance(exc, config_mod.ConfigError):
        return f"按提示修正 {user_path}；保存即热重载，不必重启 BarKit"
    return f"检查 {user_path} 的可读性与 YAML 语法（缩进 / 制表符 / 中文冒号）"


def check_config_loads() -> list[Result]:
    """走一次真实的 ``Config`` 构造：schema 闸门 ＋ 深度合并 ＋ ``validate`` 全覆盖。"""
    user_path = paths.APP_SUPPORT / "config.yaml"
    try:
        # 构造即完成「读默认 ＋ 读用户差异 ＋ schema 闸门 ＋ 深度合并 ＋ validate」
        config_mod.Config(create_user_file=False)
    except Exception as exc:  # noqa: BLE001 —— ConfigError / YAMLError / OSError 都算这条不过
        return [
            Result("B2", GROUP, "配置加载", FAIL,
                   f"{exc.__class__.__name__}: {exc}",
                   _hint_for(exc, user_path))
        ]
    where = f"{user_path}（存在）" if user_path.exists() else f"{user_path}（不存在，用默认值）"
    return [
        Result("B2", GROUP, "配置加载", PASS,
               f"schema v{config_mod.SUPPORTED_CONFIG_SCHEMA_VERSION} · 合并校验通过 · 用户配置 {where}")
    ]


CHECKS = (
    Check("B1", "默认配置", check_default_config),
    Check("B2", "配置加载", check_config_loads),
)


def check_config() -> list[Result]:
    return run_checks(GROUP, CHECKS)
