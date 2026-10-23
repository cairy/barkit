"""consts：跨模块共享的编译期常量（绝对路径 / 节拍）。

绝对路径：LaunchDaemon 环境 PATH 干净，禁止依赖 PATH 查找。
"""

from __future__ import annotations

# 系统命令（绝对路径，禁依赖 PATH）
ROUTE = "/sbin/route"
IFCONFIG = "/sbin/ifconfig"
IPCONFIG = "/usr/sbin/ipconfig"

# 主循环节拍
TRIGGER_CHECK_INTERVAL = 1.0   # 秒，trigger 轻查粒度
DEFAULT_POLL = 30              # 秒，路由/ hosts 默认轮询
