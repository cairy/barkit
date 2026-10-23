"""ui/macos_window：macOS 原生窗口能力桥接（仅 darwin 生效，非 darwin / offscreen 为 no-op）。

状态面板需要三个能力同时成立：
  1) 折叠时顶层窗口能实时 resize（点小三角平滑收缩，对应设计 §7.2 过渡 200ms）；
  2) 浮在全屏 App 之上（全屏 Space 不遮挡）；
  3) 点面板外区域自动关闭。

- Qt.Popup：原生给 2)+3)，但 1) 在 macOS 上「可见时」resize 被窗口服务器忽略，
  故折叠会「缩不了」——这是最初的 bug。
- Qt.Window：原生给 1)，但 2)/3) 用 Qt 的 WindowStaysOnTopHint（macOS 上仅 level 3，
  压不过全屏 Space）/ WindowDeactivate（菜单栏代理 app 从不「激活」，不可靠）都不行。

因此选 Qt.Window 拿 1)，再用 PyObjC 把 2)/3) 的原生能力补回：
  - 2) 设 NSWindow.level = NSPopUpMenuWindowLevel（101，即 Qt.Popup 所用的层级，浮于全屏 Space 之上）；
  - 3) 显示时激活 app，靠 NSApplicationDidResignActiveNotification：用户点任何别处
     （其他 App / 桌面 / 菜单栏）时 app 失活，通知触发即关闭面板。无需辅助功能授权
     （全局鼠标监视器才需要，且常静默失效），比 NSEvent 监视器稳得多。
状态栏图标那次点击由 app.py 的 toggle 单独处理，不与失活关闭竞争。

**关键是 3) 不只是「点外面能关」**：macOS 上 Qt 的 cocoa 插件对鼠标事件有一道
*应用级* 闸门 —— 本进程不是活跃 app 时，鼠标移动 / 进入 / 离开与光标更新都在 Qt 侧被丢掉，
于是悬停样式、tooltip、光标形状**三者一起**静默失效，而点按照常（判据与出处逐行见
:func:`macos_base.app_is_active`）。所以「面板可见」与「app 是活跃 app」这两件事在本项目里是**绑定的**：
前者成立而后者不成立，用户看到的就是「面板点得动、但没反应」。3) 只管在失活时关掉，
**没有**管「压根没激活成功」那一种（那时不会有失活通知，因为从未活跃过）⇒ 补了
:func:`macos_verdict.panel_mouse_dead_verdict` 那条复核链，见 ``PanelDismissController``。

2026-10-04 又补了**第二条**路：``macos_hover.PanelHoverRelay`` —— 真机实测「重试激活」整条
路走不通（同一场景连试六次请求全部落空，诊断见 ``docs/log/2026-09-28-面板鼠标反馈诊断-现场与机制.md``），
故改成不指望激活成功：只要面板可见、本进程不是活跃 app，就自己把鼠标位置补投给 Qt。

本包按职责切分（2026-10-03），这里**全量再导出**：调用方继续
``from barkit.shell.macos_window import X``，原先能从本模块拿到的名字一张不少
（包括 ``PANEL_LEVEL`` / ``OVERLAY_LEVEL`` / ``create_dismiss_controller`` / ``_native_enabled`` 这些私有件）。

⚠️ 方法体里的全局名解析的是**它自己那个子模块** ⇒ 往本包上 ``monkeypatch`` 任何一个
被函数体引用的名字（``_native_enabled`` / ``cursor_pos`` / ``objc`` 这些）都是打进空气、
判据纹丝不动；打桩点一律落真身子模块（``barkit.shell.macos_window.macos_base`` /
``.macos_cursor`` / ``.macos_verdict`` / ``.macos_level`` / ``.macos_dismiss``）。
"""

from __future__ import annotations

# --- 原 ``macos_window.py`` 的全部外部导入，原样再导出（引用面零改动）---
import logging
import sys
from PySide6.QtCore import QPoint
from PySide6.QtGui import QGuiApplication

# --- 包内各子模块 ---
from .macos_base import (
    DETAIL_LEVEL,
    NONACTIVATING_PANEL_MASK,
    NSApplication,
    NSApplicationDidResignActiveNotification,
    NSEvent,
    NSLeftMouseDownMask,
    NSNotificationCenter,
    NSObject,
    NSPopUpMenuWindowLevel,
    NSRightMouseDownMask,
    NSStatusWindowLevel,
    OVERLAY_LEVEL,
    PANEL_LEVEL,
    POLICY_ACCESSORY,
    POLICY_REGULAR,
    SETTINGS_LEVEL,
    _HAS_OBJC,
    _NATIVE,
    _native_enabled,
    app_is_active,
    log,
    objc,
    set_activation_policy,
)
from .macos_verdict import (
    VERDICT_OK,
    VERDICT_REACTIVATE,
    VERDICT_STUCK,
    panel_mouse_dead_verdict,
    should_close_on_deactivate,
    should_reinject_motion,
)
from .macos_hover import (
    PanelHoverRelay,
    create_hover_relay,
)
from .macos_cursor import (
    cursor_inside_widget,
    cursor_on_panel_or_icon,
    cursor_pos,
)
from .macos_level import (
    _apply_overlay_level,
    apply_nonactivating,
    make_floating_topmost,
    native_top_left,
    native_top_left_on_screen,
    nswindow_of,
    set_native_top_left,
    set_window_level,
)
from .macos_dismiss import (
    PanelDismissController,
    _GATE_MS,
    _RECHECK_MS,
    create_dismiss_controller,
)

__all__ = [
    "DETAIL_LEVEL",
    "NONACTIVATING_PANEL_MASK",
    "NSApplication",
    "NSApplicationDidResignActiveNotification",
    "NSEvent",
    "NSLeftMouseDownMask",
    "NSNotificationCenter",
    "NSObject",
    "NSPopUpMenuWindowLevel",
    "NSRightMouseDownMask",
    "NSStatusWindowLevel",
    "OVERLAY_LEVEL",
    "PANEL_LEVEL",
    "POLICY_ACCESSORY",
    "POLICY_REGULAR",
    "PanelDismissController",
    "PanelHoverRelay",
    "QGuiApplication",
    "QPoint",
    "SETTINGS_LEVEL",
    "VERDICT_OK",
    "VERDICT_REACTIVATE",
    "VERDICT_STUCK",
    "_GATE_MS",
    "_HAS_OBJC",
    "_NATIVE",
    "_RECHECK_MS",
    "_apply_overlay_level",
    "_native_enabled",
    "app_is_active",
    "apply_nonactivating",
    "create_dismiss_controller",
    "create_hover_relay",
    "cursor_inside_widget",
    "cursor_on_panel_or_icon",
    "cursor_pos",
    "log",
    "logging",
    "make_floating_topmost",
    "native_top_left",
    "native_top_left_on_screen",
    "nswindow_of",
    "objc",
    "panel_mouse_dead_verdict",
    "set_activation_policy",
    "set_native_top_left",
    "set_window_level",
    "should_close_on_deactivate",
    "should_reinject_motion",
    "sys",
]
