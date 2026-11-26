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

try:
    import Quartz

    _CG_OK = True
except ImportError:
    _CG_OK = False

#: 系统设置 → 隐私与安全性 → 辅助功能 的深链（``open`` 可直接打开该面板）。
ACCESSIBILITY_PANE_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
)

#: 系统设置 → 隐私与安全性 → **输入监控** 的深链。
#: 全局热键用的是「只监听」的键盘事件 tap，macOS 把那一项归到输入监控（不是辅助功能）；
#: 引导语若只说辅助功能，用户照着做仍然按不响。
INPUT_MONITORING_PANE_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"
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


def open_input_monitoring_pane() -> None:
    """打开系统设置 → 隐私与安全性 → 输入监控。"""
    subprocess.Popen(["/usr/bin/open", INPUT_MONITORING_PANE_URL])


def request_listen_access() -> bool:
    """申请「输入监控」授权 —— **这是 BarKit 出现在那个列表里的唯一途径**。

    只建 tap 不算申请：``CGEventTapCreate`` 失败后 macOS 不会把 App 列进「输入监控」，
    用户点开面板只会看到一张空名单（2026-11-26 现场：通知引导用户去勾选，可列表里
    根本没有 BarKit，于是「指引还是不行」）。显式调一次 ``CGRequestListenEventAccess``
    才会注册身份并弹出系统授权框。

    已授权 / 已拒绝都不会再弹（系统每 App 只问一次），返回当前是否已授予。
    """
    if not _CG_OK:
        return False
    request = getattr(Quartz, "CGRequestListenEventAccess", None)
    if request is None:
        return False
    try:
        return bool(request())
    except Exception:  # noqa: BLE001 —— 申请失败只意味着「没拿到」
        return False


def listen_event_access() -> "bool | None":
    """能否创建「只监听」的键盘事件 tap（即输入监控是否已授予）。

    **只读预检，不弹系统窗口**（``CGPreflightListenEventAccess``）。返回 ``None``
    表示「这个系统答不上来」（非 macOS / 太旧的系统没有这个 API）—— 调用方应把
    ``None`` 当「不知道」处理，而不是当「没授权」，否则会在正常机器上误报。

    辅助功能权限**包含**输入监控，所以两边任一为真都算能监听（见
    :func:`accessibility_trusted`）。
    """
    if not _CG_OK:
        return False
    preflight = getattr(Quartz, "CGPreflightListenEventAccess", None)
    if preflight is None:
        return None
    try:
        return bool(preflight())
    except Exception:  # noqa: BLE001 —— 预检失败只意味着「答不上来」
        return None
