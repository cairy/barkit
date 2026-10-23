"""contribution/form：渲染相关的 widget 提示（方案 v3 §4 目录注释）。

数据契约（FormSchema / FieldSpec / FieldType）在 ``core/schema.py``——
因为 core/config.py 要用它做默认值注入、保存前校验、版本迁移，不能让 core
依赖一个本该只服务 UI 的包。本文件只保留「怎么画」的提示常量：
Shell 的表单渲染器按 widget hint 选择控件，插件无渲染权。
"""

from __future__ import annotations

from enum import Enum


class WidgetHint(str, Enum):
    """FieldSpec.widget 的合法取值：表单渲染器据此选控件（仅提示，不承载行为）。"""

    TEXT = "text"          # 单行文本（默认）
    MULTILINE = "multiline"  # 多行文本
    SECRET = "secret"      # 密码/密钥：输入打码 + 导出时默认排除
    CHECK = "check"        # 布尔勾选
    NUMBER = "number"      # 数字步进器
    CHOICE = "choice"      # 下拉单选（配合 FieldSpec.choices）
