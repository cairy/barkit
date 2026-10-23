"""core/ax_trust：辅助功能（Accessibility）信任状态的**只读探测**。

为什么与本文件分开
----------------
「检测」（问系统一个 bool）与「引导」（弹对话框带用户去开权限）是两件事，原先都
挤在 ``core/permissions.py`` 里，而后者顶层 ``from PySide6.QtWidgets import
QMessageBox`` —— 于是任何只想读一个 bool 的消费方都被迫拖进整个 QtWidgets。

最直接的消费方是 ``barkit doctor``：它的引擎顶层不得 import PySide6（「PySide6
能不能导入」本身就是它的一条检查项，见 ``内部 doctor 设计记录`` §4.1）。
故把无 Qt 依赖的检测抽到本模块，``core/permissions.py`` 原样 re-export 本模块的
名字 —— 两个既有消费方（``plugins/autotype/plugin.py``、``shell/settings.py``）
零改动。

铁律：辅助功能权限绑定**可执行二进制的身份**（开发期授权给终端），一切以
``AXIsProcessTrusted()`` 实测为准，**不假设「授权过一次永久有效」**——venv 重建 /
Python 升级都会让旧授权失效。
"""

from __future__ import annotations

import subprocess

try:
    from ApplicationServices import (
        AXIsProcessTrusted,
        AXIsProcessTrustedWithOptions,
        kAXTrustedCheckOptionPrompt,
    )

    _AX_OK = True
except ImportError:
    _AX_OK = False

#: 系统设置 → 隐私与安全性 → 辅助功能 的深链（``open`` 可直接打开该面板）。
ACCESSIBILITY_PANE_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
)


def available() -> bool:
    """本机是否有可用的 AX 查询接口（pyobjc 的 ApplicationServices 在不在）。

    返回 False 时 ``accessibility_trusted()`` 恒为 False —— 那是「问不出来」，
    不是「没授权」。**调用方必须能分辨这两者**（doctor 的 F 组据此报 SKIP 而非 FAIL）。
    """
    return _AX_OK


def accessibility_trusted() -> bool:
    if not _AX_OK:
        return False
    return bool(AXIsProcessTrusted())


def request_accessibility() -> bool:
    """触发系统授权弹窗（**每进程首次调用才弹**），返回当前信任状态。"""
    if not _AX_OK:
        return False
    options = {kAXTrustedCheckOptionPrompt: True}
    return bool(AXIsProcessTrustedWithOptions(options))


def open_accessibility_pane() -> None:
    """打开系统设置 → 隐私与安全性 → 辅助功能。"""
    subprocess.Popen(["/usr/bin/open", ACCESSIBILITY_PANE_URL])
