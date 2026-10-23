"""插件脚手架模板包（以 ``_`` 开头，registry 不加载，仅作示例 / 复制源）。

两个骨架，二选一作起点（规范 §10.1）：

- ``plugin.py``        —— 动作型（``RUN_ON_DEMAND``，按需触发）；
- ``plugin_daemon.py`` —— 守护型（``RUN_PERIODIC`` + ``AutomationCapability``，常驻轮询），
  **复制时改名为 ``plugin.py``**。

``settings.py`` 是设置面（设置页 + 详情页 + 其子对话框）的归处，两种骨架共用。
"""
