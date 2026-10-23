"""__main__：``python -m helper`` 的入口（方案 §7.4.6）。

安装脚本把整个 ``helper/`` 包部署到系统域
``/Library/Application Support/BarKit/helper``；plist 注入
``PYTHONPATH=/Library/Application Support/BarKit``，启动器 ``barkit-helper``
执行 ``exec /usr/bin/python3 -m helper``。

保持极薄：只转交 ``core.main()``，**不在此处添加任何能力**——root 侧能做什么
由 ``actions.ACTION_SCHEMAS``（编译期常量）决定，入口不得成为旁路。
"""

from __future__ import annotations

import sys

from .core import main

if __name__ == "__main__":
    sys.exit(main())
