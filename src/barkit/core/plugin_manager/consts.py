"""编排全族共用的日志名。

``plugin_manager.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import logging


#: 编排全族共用的日志名（`logging_setup` 的 `%(name)s` 会写进日志文件）。
#: 写死父包名而不是 `__name__`：子模块各自的 `__name__` 会把日志外观改掉。
log = logging.getLogger("barkit.core.plugin_manager")
