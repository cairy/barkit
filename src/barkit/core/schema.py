"""core/schema：FormSchema 数据契约（方案 v3 §4 / §8.1 / §9.5）。

放 core 而非 contribution 的理由（§3.2 末）：core/config.py 要用它做
默认值注入、保存前校验、版本迁移——**数据契约**不能依赖一个只服务 UI 的包。
渲染相关的 widget 提示常量在 ``contribution/form.py``（Shell 渲染器消费）；
本模块只存 ``FieldSpec.widget`` 字符串，不 import 任何 UI 模块，保持
「core 只依赖标准库」的依赖方向。

配置单一真源 = 插件的 ``config_schema()``（§9.5）：默认值是运行时合成层，
不落盘；用户只把差异项写进 config.yaml。

版本迁移（§9.5 版本表）：单插件配置版本记在 ``plugins.<id>._schema_version``
（下划线前缀 = 框架保留键，渲染器跳过）；记录缺失视为全新安装，直接取默认值。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

SCHEMA_VERSION_KEY = "_schema_version"


class FieldType(str, Enum):
    """字段的数据类型（校验依据；怎么画归 WidgetHint）。"""

    TEXT = "text"
    NUMBER = "number"
    BOOL = "bool"
    CHOICE = "choice"
    SECRET = "secret"      # 导出包默认排除（§9.1），输入打码由渲染器负责
    LIST = "list"          # 复杂结构（list / list-of-dict）字段；仅用于默认值合成与
                          # 宽松校验（isinstance list），不要求简单表单渲染。
                          # 例：notify_guard 的 rules[]。§9.5 单一真源下，此类字段
                          # 也须经 config_schema() 声明默认，不能散落代码 fallback。
    DICT = "dict"          # 嵌套对象**段**：段内是一组子键（如 recognize 的 ocr /
                          # barcode、ipad_tunnel 的 wifi），不是一个标量，故不能拆成
                          # 若干平铺字段——那样 ``defaults()`` 会产出带点的字面键
                          # （``wifi.on_activate``），而消费方读的是 ``cfg["wifi"]
                          # ["on_activate"]``，默认值会静默失效。与 LIST 同族：仅用于
                          # 默认值合成与宽松校验（isinstance dict），全仓无渲染器。
                          # 例：ipad_tunnel 的 wifi（子键固定为 on_activate /
                          # on_deactivate，取值由 root 侧消费，无 type 分发）。


class SchemaError(Exception):
    """schema 声明自身非法（如 CHOICE 无 choices）。"""


class SchemaMigrationError(Exception):
    """配置迁移缺步：无法从记录版本连续迁移到 schema.version。"""


@dataclass(frozen=True)
class FieldSpec:
    """单个配置字段的声明。

    widget 是渲染提示字符串（合法值见 contribution.form.WidgetHint），
    故意为 str 而非枚举引用——core 不 import contribution（见模块 docstring）。
    """

    key: str
    label: str
    type: FieldType = FieldType.TEXT
    default: Any = None
    choices: tuple = ()
    tooltip: str = ""
    required: bool = False
    widget: str = "text"

    def check_value(self, value: Any) -> str | None:
        """类型/取值校验，返回错误文案（None = 合法）。"""
        # schema 自身非法先于取值校验（CHOICE 缺 choices 必须显式炸出来）
        if self.type is FieldType.CHOICE and not self.choices:
            raise SchemaError(f"字段 {self.key} 声明为 CHOICE 但未提供 choices")
        if value is None:
            return None if not self.required else f"{self.key} 不能为空"
        if self.type is FieldType.NUMBER:
            # bool 是 int 子类，必须显式排除（True 会被当成 1 混进来）
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return f"{self.key} 必须是数字：{value!r}"
        elif self.type is FieldType.BOOL:
            if not isinstance(value, bool):
                return f"{self.key} 必须是布尔值：{value!r}"
        elif self.type in (FieldType.TEXT, FieldType.SECRET):
            if not isinstance(value, str):
                return f"{self.key} 必须是字符串：{value!r}"
        elif self.type is FieldType.LIST:
            if not isinstance(value, list):
                return f"{self.key} 必须是列表：{value!r}"
        elif self.type is FieldType.DICT:
            if not isinstance(value, dict):
                return f"{self.key} 必须是对象：{value!r}"
        elif self.type is FieldType.CHOICE:
            if value not in self.choices:
                return f"{self.key} 必须是 {list(self.choices)} 之一：{value!r}"
        return None


@dataclass
class FormSchema:
    """单个插件的配置 schema（版本表最右行，附录 B）。

    migrations：``{from_version: 迁移函数}``。迁移按版本逐级执行，
    缺任一级即 SchemaMigrationError（宁可拒绝也不静默丢数据）。
    """

    plugin_id: str
    version: int = 1
    fields: tuple[FieldSpec, ...] = ()
    migrations: dict[int, Callable[[dict], dict]] = field(default_factory=dict)

    # ---- 默认值合成（运行时层，不落盘，§9.5）----

    def defaults(self) -> dict:
        """按字段声明合成默认值；default 为 None 的字段（多为必填项）不产出。"""
        out: dict = {}
        for f in self.fields:
            if f.default is not None:
                out[f.key] = deepcopy(f.default)
        return out

    # ---- 校验（保存前，§8.1）----

    def validate(self, data: dict) -> tuple[dict, list[str]]:
        """校验并返回 (清理后的副本, 错误列表)。

        - 只校验 schema 声明过的字段；其余键（``enabled`` 等框架键与
          ``_schema_version`` 这个版本键）**都不在 ``fields`` 里** ⇒ 天然落在本
          循环之外，原样透传且不拒绝——schema 管的是表单字段，不是整段配置的
          白名单。**没有「按 `_` 前缀跳过」这码事**：`src/barkit` 里没有任何一处
          按前缀判保留键（按 `_` 开头判、按首字符判都搜过，零命中）。
        - CHOICE 缺 choices 属 schema 自身非法，直接抛 SchemaError。
        """
        errors: list[str] = []
        clean = dict(data)
        for f in self.fields:
            if f.key not in data:
                if f.required:
                    errors.append(f"{f.key} 不能为空")
                continue
            msg = f.check_value(data[f.key])
            if msg:
                errors.append(msg)
        return clean, errors

    # ---- 版本迁移（§9.5 版本表）----

    def migrate(self, data: dict) -> dict:
        """把用户配置从记录版本迁移到 schema.version，返回新 dict。

        - 无 ``_schema_version`` 记录 → 视为全新安装，原样返回（默认值由
          合成层提供，不经迁移，§9.5）；
        - 记录版本 >= schema.version → 无需迁移，仅回写版本号；
        - 缺任一级迁移函数 → SchemaMigrationError。
        """
        recorded = data.get(SCHEMA_VERSION_KEY)
        if recorded is None:
            return dict(data)
        recorded = int(recorded)
        out = dict(data)
        for step in range(recorded, self.version):
            fn = self.migrations.get(step)
            if fn is None:
                raise SchemaMigrationError(
                    f"插件 {self.plugin_id} 配置迁移缺步：v{step} → v{step + 1}"
                )
            out = fn(out)
        out[SCHEMA_VERSION_KEY] = self.version
        return out
