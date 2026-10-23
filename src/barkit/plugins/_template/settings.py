"""插件脚手架模板 · **设置面**（以 ``_`` 开头，发现器不加载，仅作示例 / 复制源）。

设置面的全部 UI 都归本模块（《插件结构规范》§7.2）：设置窗里的一页（
``SettingsPageSpec.build``）、用户点组件整行进入的详情页（``DetailEntrySpec.build``）、
以及它们打开的子对话框。**不要再新建 ``config_dialog.py`` / ``editor.py`` 这类平行模块名。**

本模块的纪律（照抄时别违反）：

- **不碰 ``ctx`` / ``config`` / 状态文件**：本模块不知道自己属于哪个插件，也就无从读它的
  配置。需要的数据与回调**由插件注入**（与运行期面 §7.3 同一口径）；
- 读配置走 ``plugin.cfg.value("某键")``（类型 / 默认值 / 取值域取自该插件的
  ``config_schema()``，调用点不写兜底）；要**整段**（如把场景表交给校验器）才用
  ``plugin.plugin_config()``。写配置**没有本模块的事**：插件的写入口内部走
  ``plugin.cfg.set`` / ``cfg.update``（等于声明默认值即删键）——**不得**自己读用户
  YAML 再 ``save_user``（UI 直改 config 是跨面禁令第 3 条，门禁
  ``test_no_config_io_in_ui`` 守着）；
- 外壳统一用 ``shell.ui_kit`` 的 ``settings_page_shell`` / ``tool_button`` / ``note``，
  不要自己写 ``QVBoxLayout`` + 手搓样式（视觉一致性靠这一层）。

高频任务流不要做进设置页：设置页是**低频一次性配置**，日常要反复点的入口走
``DetailEntrySpec``（详情页），判据真源见《组件契约规范》§2.8 与《组件契约决议》D30。
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtWidgets import QWidget

from barkit.shell.ui_kit import note, settings_page_shell, tool_button


def build_page(on_action: Callable[[], None] | None = None) -> QWidget:
    """构建设置页。

    框架按 ``SettingsPageSpec.build()`` **零参**调用（``shell/settings.py:93``），
    所以插件侧要传一个闭包把回调注进来（见 ``plugin.py`` 的 ``build=lambda: ...``）。

    ``on_action``：动作按钮的回调，由插件提供。为 ``None`` 时按钮只渲染不接线——
    这样单独预览本模块（不经插件）也不会抛。
    """
    page, layout = settings_page_shell("模板插件")

    btn = tool_button("执行模板动作", "primary")
    if on_action is not None:
        btn.clicked.connect(on_action)
    layout.addWidget(btn)

    layout.addWidget(note(
        "这是插件贡献的设置页示例。设置页只放「配一次就不再动」的低频配置与动作入口；"
        "每天都用的任务流应改用详情页（DetailEntrySpec）。"
    ))
    layout.addStretch(1)
    return page
