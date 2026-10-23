"""本组件唯一的异常类型 ``RouteConfigError``（场景配置非法）。

校验层（``schema``）与编译层（``compile`` / ``rules``）都抛它，捕获方在插件与浮层：
``plugin.py`` / ``reports.py`` / ``profiles.py`` / ``overlay.py`` 都以它当「配置非法」
那一格，不自己再定义一份（两份同名异常会互相 catches 不到）。

本模块零依赖（不 import 任何东西）。

``rules.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations


class RouteConfigError(Exception):
    """场景（profile）配置非法。"""
