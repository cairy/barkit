"""原生能力底座：平台/离屏探活（``_native_enabled``）、AppKit 符号与降级绑定、六枚窗口层级与激活策略常量、全族共用 ``log``，以及``app_is_active`` / ``set_activation_policy`` 两枚原生入口。

``macos_window.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import logging
import sys

from PySide6.QtGui import QGuiApplication


log = logging.getLogger("barkit.shell.macos_window")

_NATIVE = sys.platform == "darwin"

if _NATIVE:
    try:
        import objc
        from AppKit import (
            NSLeftMouseDownMask,
            NSLeftMouseDraggedMask,
            NSMouseEnteredMask,
            NSMouseExitedMask,
            NSMouseMovedMask,
            NSRightMouseDownMask,
            NSRightMouseDraggedMask,
            NSEvent,
        )
        from Cocoa import (
            NSApplication,
            NSApplicationDidResignActiveNotification,
            NSPopUpMenuWindowLevel,
            NSStatusWindowLevel,
        )
        from Foundation import NSNotificationCenter, NSObject

        _HAS_OBJC = True
    except Exception:  # PyObjC 缺失时降级，应用仍可跑（仅缺原生浮层/关闭行为）
        _HAS_OBJC = False
        NSObject = object
        objc = NSApplication = NSEvent = NSNotificationCenter = None
        NSApplicationDidResignActiveNotification = None
        NSLeftMouseDownMask = NSRightMouseDownMask = None
        NSLeftMouseDraggedMask = NSRightMouseDraggedMask = None
        NSMouseMovedMask = NSMouseEnteredMask = NSMouseExitedMask = None
        # 包 ``__init__`` 无条件再导出这两个裸名字（``__all__`` 也列了）⇒ 兜底分支
        # 必须绑上。2026-10-03 拆包时两处都漏了：非 darwin / 缺 PyObjC 的机器上
        # import 本包即 ImportError，该模块「降级仍可跑」的承诺随之作废。取值与
        # 下面 ``PANEL_LEVEL`` / ``OVERLAY_LEVEL`` 的数字兜底同值。
        NSPopUpMenuWindowLevel = 101
        NSStatusWindowLevel = 25
else:
    _HAS_OBJC = False
    NSObject = object
    objc = NSApplication = NSEvent = NSNotificationCenter = None
    NSApplicationDidResignActiveNotification = None
    NSLeftMouseDownMask = NSRightMouseDownMask = None
    NSLeftMouseDraggedMask = NSRightMouseDraggedMask = None
    NSMouseMovedMask = NSMouseEnteredMask = NSMouseExitedMask = None
    # 包 ``__init__`` 无条件再导出这两个裸名字（``__all__`` 也列了）⇒ 兜底分支
    # 必须绑上。2026-10-03 拆包时两处都漏了：非 darwin / 缺 PyObjC 的机器上
    # import 本包即 ImportError，该模块「降级仍可跑」的承诺随之作废。取值与
    # 下面 ``PANEL_LEVEL`` / ``OVERLAY_LEVEL`` 的数字兜底同值。
    NSPopUpMenuWindowLevel = 101
    NSStatusWindowLevel = 25


if _HAS_OBJC:
    PANEL_LEVEL = NSPopUpMenuWindowLevel
    # 二级窗口（设置）层级高于面板，确保不被面板遮挡
    SETTINGS_LEVEL = NSPopUpMenuWindowLevel + 100
else:
    PANEL_LEVEL = 101
    SETTINGS_LEVEL = 201

#: 浮层（``shell/overlay.py``）用的窗口层级。**不许用 ``NSFloatingWindowLevel``(3)**
#: —— 2026-09-22 真机 + 探针实测：那个值在这套环境里站不住，会被反复改写，结局有两种，
#: 都让浮层失去置顶：
#:
#: - 被顶回 Qt 自己的档（``NSModalPanelWindowLevel`` = 8）：设完 3 之后紧接着的 Qt 侧
#:   窗口动作（``show`` / ``raise_`` 会把 ``WindowStaysOnTopHint`` 重新落一遍）会覆盖它；
#: - 被清成 ``NSNormalWindowLevel``(0)：**只发生在 NSPanel ＋ nonactivating mask 的窗上**，
#:   触发时刻是 app 的 active→inactive 跃变。浮层一旦落到 0 就是普通层窗口，而一键输入的
#:   两个必经动作都会让 BarKit 失活（面板关闭 / 点进目标 app 聚焦输入框）⇒ 浮层立刻被目标
#:   窗口压住。真机监视器抓到的原始读数：``kCGWindowLayer 3 → 0``，同一秒里面板（layer=101）
#:   也从屏幕上消失，而前台 app 全程未变（＝失活来自面板关闭，不是切换 app）。
#:
#: 20 / 25 / 101 / 201 四档逐档验过：**没人动**，且各自扛过一次失活跃变。取
#: ``NSStatusWindowLevel``(25) 是其中最低的一档 —— 低于面板(101)与设置窗(201)，保持
#: 「浮层 < 面板 < 二级窗口」的既有分层不变。判据与探针原始输出见
#: ``内部记录（浮层置顶）``。
OVERLAY_LEVEL = NSStatusWindowLevel if _HAS_OBJC else 25

# 详情页（L3 模态任务流）与设置窗同为二级窗口，共用同一层级
DETAIL_LEVEL = SETTINGS_LEVEL

# NSApplicationActivationPolicy（Dock 图标显隐）：0=Regular 1=Accessory
POLICY_REGULAR = 0
POLICY_ACCESSORY = 1

#: ``NSWindowStyleMaskNonactivatingPanel`` 的值（1<<7）。AppKit 里二十多年没变过，
#: 老版本 PyObjC 可能没导出这个名字，故取值一律走 ``getattr(..., 该常量)`` 兜底。
#: **只对 NSPanel 有效** —— 用在普通 NSWindow 上是静默无效的（这正是「点一下就把用户
#: 正在打字的 app 顶掉」的根因，别指望靠它救普通窗）。
NONACTIVATING_PANEL_MASK = 1 << 7


def _native_enabled() -> bool:
    """仅当真正在 macOS 原生 Cocoa 下运行时才启用原生调用。

    offscreen（pytest 的 QT_QPA_PLATFORM=offscreen）平台仍是 darwin，但无真实
    NSWindow，调用 PyObjC 会崩溃或静默无效，故一并禁止——此时仅回归逻辑，窗口
    层级/点击关闭由测试外的真机保证。
    """
    return _HAS_OBJC and QGuiApplication.platformName() != "offscreen"


def app_is_active() -> bool:
    """本进程此刻是不是「活跃 app」（``NSApp.isActive``）；非原生 / 离屏环境返回 False。

    **为什么这个判据值得单独一个口**：macOS 上 Qt 的 cocoa 插件对鼠标事件有一道
    *应用级* 闸门。``qnsview_mouse.mm``（2026-09-24 对着 Qt 6.8 源码逐行核过）里
    ``NSApp.active`` 出现四处，**都不是**「方法第一句」那种整齐的样子，得按事实记：

    - ``cursorUpdate:`` 的第一句就是 ``if (!NSApp.active) return;`` ⇒ 光标形状不更新；
    - ``mouseMovedImpl:`` / ``mouseEnteredImpl:`` / ``mouseExitedImpl:`` 各自在「子窗口归属」
      「是否对输入透明」的检查之后出现 ``if (!NSApp.active) return;`` ⇒ 移动 / 进入 / 离开
      **一个都不交给 Qt**（Enter/Leave 由 ``QWindowSystemInterface::handleEnterEvent`` /
      ``handleLeaveEvent`` 投递，正是被这一句挡在门外）；
    - 点按那几条（``mouseDown:`` / ``mouseUp:`` / ``rightMouseDown:``）**没有**这道检查。
      （跟踪区 owner 是 ``QNSViewMouseMoveHelper``，它只做无条件转发，过滤全在上面几处。）

    tracking area 用的是 ``NSTrackingActiveAlways``，源码注释给了原因：理想的
    ``NSTrackingActiveInActiveApp`` 在「cmd+tab 切走、之后靠点击切回来」时会失效，所以
    宁可用 Always 再自己丢。原注释的措辞是「simply ignore any related callbacks while the
    application is inactive」—— 于是回调**照样送过来、只是被丢掉**，从外面完全看不出
    「收到但没处理」和「压根没收到」的区别。结论两半，都要记住：

    1. 「窗口看得见」不等于「窗口收得到鼠标」；
    2. 本进程不是活跃 app 时，悬停样式（QSS ``:hover`` 与自绘 ``HoverRow``）、tooltip、
       光标形状**三者一起**失效，而点按照常 ⇒ 现象正是「点得动，但鼠标经过、停留都没有
       任何反馈」。tooltip 与鼠标移动同源也是源码里写明的：``NSTrackingMouseMoved`` 的注释是
       「Qt will send 'tooltip' events on mouse moves, so we need to turn it on in ALL cases」。

    于是「面板没有任何鼠标反馈」这类现象，第一个要问的是这里，而不是控件焦点或布局；
    反过来说，**只有其中一个坏**（比如悬停正常、tooltip 不出）才轮得到别的嫌疑。

    注意它**不是**「窗口是不是 key」：key 状态由 AppKit 在同进程的窗口之间轮转，Qt 不看它。

    非原生环境返回 False —— 那是「判据不适用」，不是「确认没激活」，调用方别把两者混用。
    """
    if not _native_enabled():
        return False
    try:
        return bool(NSApplication.sharedApplication().isActive())
    except Exception:  # noqa: BLE001 —— 读不到按「不是活跃 app」处理，由调用方决定怎么报
        return False


def set_activation_policy(policy: int) -> bool:
    """切换 App 激活策略——Dock 图标显隐的**唯一入口**（UI 规范 §5.4）。

    收归前这套逻辑有两份私有实现：装配器启动时的 ``app_shell._set_accessory_policy``
    （设 Accessory）与识别窗口的 ``_set_macos_activation_policy``（开窗口切 Regular、
    关窗口切回）。两处并存意味着「Dock 图标什么时候出现」的答案分散在插件里，
    详情页（§5.4 要求弹出时显示 Dock 图标）若再造第三份就彻底失控。

    语义：``POLICY_REGULAR`` → Dock 图标出现（模态任务流可从 Dock / Cmd+Tab 找回）；
    ``POLICY_ACCESSORY`` → 恢复菜单栏 App 常态（无 Dock 图标）。

    返回是否真正切了（非 macOS / offscreen / 调用失败返回 False），调用方据此
    决定是否需要在关闭时复原。
    """
    if not _native_enabled():
        return False
    try:
        NSApplication.sharedApplication().setActivationPolicy_(policy)
        return True
    except Exception:
        return False
