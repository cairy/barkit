"""配置解析与校验：``ConfigError`` / ``deep_merge`` / ``validate``。

``config.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from barkit.core.variables import spec as variables_spec

from .consts import SUPPORTED_CONFIG_SCHEMA_VERSION


class ConfigError(Exception):
    """配置校验失败。"""


def deep_merge(base: dict, override: dict) -> dict:
    """深度合并：dict 递归，list/标量整体覆盖。不修改入参。"""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def validate(data) -> dict:
    """轻量 schema 校验：顶层结构与关键字段类型。失败抛 ConfigError。"""
    if not isinstance(data, dict):
        raise ConfigError("配置顶层必须是 mapping")
    # 配置 schema 版本闸门：与 SUPPORTED_CONFIG_SCHEMA_VERSION 不一致即拒绝
    # （缺省视为当前版本，向后兼容旧/无该键的用户配置）。
    sv = data.get("schema_version", SUPPORTED_CONFIG_SCHEMA_VERSION)
    if sv != SUPPORTED_CONFIG_SCHEMA_VERSION:
        raise ConfigError(
            f"配置 schema 版本不兼容：需要 v{SUPPORTED_CONFIG_SCHEMA_VERSION}，"
            f"当前 v{sv}；请升级 BarKit 或删除用户 config.yaml 后重启"
        )
    plugins = data.get("plugins", {})
    if not isinstance(plugins, dict):
        raise ConfigError("plugins 必须是 mapping")
    for name, section in plugins.items():
        if not isinstance(section, dict):
            raise ConfigError(f"plugins.{name} 必须是 mapping")
        if "enabled" in section and not isinstance(section["enabled"], bool):
            raise ConfigError(f"plugins.{name}.enabled 必须是布尔值")
    # 插件间排序（§6.4）：顶层 plugin_order 为 plugin_id 列表；可缺省（= 未自定义排序）
    if "plugin_order" in data:
        po = data["plugin_order"]
        if not isinstance(po, list) or not all(isinstance(x, str) for x in po):
            raise ConfigError("plugin_order 必须是字符串列表")
    hotkeys = data.get("hotkeys", {})
    if not isinstance(hotkeys, dict) or not all(
        isinstance(v, str) for v in hotkeys.values()
    ):
        raise ConfigError("hotkeys 必须是 字符串→字符串 的 mapping")
    # 菜单栏图标（P0③）：tray.base_source 为状态 topic 字符串，"" = BarKit 默认底图
    tray = data.get("tray", {})
    if not isinstance(tray, dict) or not all(
        isinstance(v, str) for v in tray.values()
    ):
        raise ConfigError("tray 必须是 字符串→字符串 的 mapping")
    # 全局变量声明（2026-10-10，设计文档 §2.2）：与 plugins 平级的顶层段。
    # **只判形状**（哪个声明说的是哪个变量）；单条写坏的语义问题（来源不认识 / 表达式
    # 写错 / 引用自己）留给 core.variables.spec.parse_all 逐条跳过并记问题 —— 一条坏声明
    # 不该让整份配置回滚（那会让用户看到的是上一版配置，而报错只留在日志里）。
    variables = data.get("variables")
    if variables is not None:
        try:
            variables_spec.check_section(variables)
        except variables_spec.VariableSpecError as e:
            raise ConfigError(f"variables 段不合法：{e}") from e
    return data
