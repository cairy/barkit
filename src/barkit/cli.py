"""barkit.cli：命令行分派（`barkit` 与 `barkit doctor` 的分岔口）。

**本模块不得 import PySide6**（门禁守）：它是 `barkit doctor` 的入口链首环，
必须在「界面依赖装不上」的环境里也能跑起来。

分派原则：**只认白名单子命令，其余一律回落 GUI**。macOS 从 Finder / Dock 启动时
``sys.argv`` 会带 ``-psn_0_12345`` 这类进程序列号参数，把未知参数当「用法错误」
会直接让正常启动失败 —— 所以这里不报错、也不猜。
"""

from __future__ import annotations

import sys
from typing import Sequence

USAGE = """用法：
  barkit                   启动 GUI（菜单栏常驻）
  barkit doctor            运行环境自检（只读 / 不需要 root / 不需要 GUI）
  barkit new-plugin <id>   生成一个外部插件骨架（`barkit new-plugin --help` 看选项）
  barkit --help            显示本说明
"""


def dispatch(argv: "Sequence[str] | None" = None) -> "int | None":
    """识别子命令。**返回 None ＝ 不是 CLI 之路**，交给 GUI 入口继续。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return None
    head = args[0]
    if head in ("-h", "--help", "help"):
        print(USAGE, end="")
        return 0
    if head == "doctor":
        from barkit.doctor import run        # 延迟 import：GUI 之路不多背一个包
        return run(args[1:])
    if head == "new-plugin":
        from barkit.scaffold import run      # 同上（两条 CLI 之路都 Qt-free）
        return run(args[1:])
    return None
