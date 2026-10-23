"""装配器全族共用的两个常量与日志名。

``app_shell.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import logging


#: 装配器全族共用的日志名（`logging_setup` 的 `%(name)s` 会写进日志文件）。
#: 写死父包名而不是 `__name__`：子模块各自的 `__name__` 会把日志外观改掉。
log = logging.getLogger("barkit.shell.app_shell")


#: 框架自己发通知时用的 ``plugin_id``：通知中枢按它回查「谁发的」，而框架不是一个
#: 插件 —— 用一个不可能与插件 id 撞的名字（插件 id 是小写字母 + 点，不带斜杠）。
FRAMEWORK_NOTIFY_ID = "framework/shell"

#: 退出时给在跑的命令线程多长宽限（毫秒）。比 ``thread_reap.GRACE_MS`` 长：退出路上
#: 没有界面可按，多等一两秒换来「线程都干净结束」是划算的；再长就不值了 —— 收不住的
#: 由 thread_reap 代持，一样不崩。
_EXIT_GRACE_MS = 2000
