"""PyInstaller 打包入口（仅用于构建 .app，不参与日常开发）。

``barkit`` 用的是绝对导入（``from barkit import ...``），所以打包时必须把
``src`` 放进 ``pathex``；这里只把真正的入口 ``barkit.app:main`` 转接一下。
"""
from barkit.app import main

if __name__ == "__main__":
    main()
