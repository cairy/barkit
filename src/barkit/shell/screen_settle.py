"""``shell/screen_settle``：**「屏参数正在变化」的唯一判据入口**。

## 它防的是哪件事（2026-10-14 定案）

屏配置一变（拉起副屏、拔插显示器、改分辨率），窗口服务器要重算所有窗的落点。那一段里
**新上屏的窗**会被按一份尚未稳定的屏布局算，落点是 ``(0,0)``。糟的是这份错结果**常驻**：
此后这条连接上新建的每个窗都落在 ``(0,0)``，进程内没有任何手段改得回来 —— 真机把十种手段
（同值重推 / 换值再推回 / ``orderOut``→挪→``orderFrontRegardless`` / ``hide·show`` /
``destroy·create`` 换原生体 / ``setFrame_display_`` …）逐条试过，**全无效且永久**；只有换
进程能解（连接一死，服务器侧的这份状态随之消失）。

两条实测边界（决定了本模块的形状）：

- **坏账之前**已经上屏的窗**不受影响** —— 窗口服务器早给它们记好位置了（故障那次同进程
  另两枚浮层窗位置都对，因为它们在屏变化前就上屏了）；
- **坏账之后**新上屏的窗一律中招，**与什么时候上屏无关** —— 屏变化过去 20 秒再开面板，
  照样在 ``(0,0)``。所以「等屏稳定再上屏」这句**不是**规避，只要窗还没上过屏、而这一段
  里它上屏了，就完了。

⇒ 于是唯一能做的事只剩一件：**别让「首次上屏」落在屏变化的那一段里**。本模块就是那一段
的时间窗，判据只有一处（别在别处再写一遍 `screenAdded` 的处理）。

## 口径

窗口期 = **最后一次屏信号之后的 :data:`SETTLE_MS` 毫秒内**。一次屏配置变化会在几秒内产生
**一串**信号（``screenAdded`` → 逐块屏的 ``geometryChanged`` / ``availableGeometryChanged``
…），所以判据要盯「静默」而不是「第一个信号」：串还没走完就开门，等于没防。

没收到过任何屏信号（非 GUI 环境 / 还没 :func:`install`）⇒ **不拦** —— 判据不适用时放行，
比"因为没信号所以永远拦着"安全。
"""

from __future__ import annotations

import logging
import time

from PySide6.QtGui import QGuiApplication, QScreen

log = logging.getLogger(__name__)

#: 最后一次屏信号之后，还要静默这么久才算「屏稳定」（毫秒）。
#: 3 秒的依据：2026-10-14 真机，屏变化之后 **2.8 秒**上屏的面板窗仍被钉在 ``(0,0)``
#: ⇒ 窗口期至少有那么长。取「静默 3 秒」＝整串信号停下来之后再过 3 秒才开门。
SETTLE_MS = 3000

#: 等门时多久探一次（毫秒）。
WAIT_RETRY_MS = 200

#: 最后一次屏信号（:func:`time.monotonic` 刻度）；``0`` = 从来没收到过。
_last_signal_at: float = 0.0

_installed = False
#: 已挂过几何监听的屏（存 ``id()``；屏对象被销毁后 id 可能被复用，最坏结果是漏挂一块
#: 新屏的几何信号 —— 而 app 级的三个信号仍在，判据不会整体失效）。
_attached: set[int] = set()


def note_change(what: str = "") -> None:
    """记一次屏信号（信号槽调用；也供诊断脚本手工喂）。"""
    global _last_signal_at
    _last_signal_at = time.monotonic()
    log.info("屏参数发生变化［%s］—— 在它静默之前不开新窗", what or "-")


def is_settling() -> bool:
    """此刻是否还在窗口期内（最后一次屏信号距今不足 :data:`SETTLE_MS`）。

    这是**全仓唯一**的「屏正在变」判据 —— 消费者（面板的首次上屏）只问这一句。
    """
    if _last_signal_at <= 0.0:
        return False
    return (time.monotonic() - _last_signal_at) * 1000.0 < SETTLE_MS


def install(app: "QGuiApplication | None" = None) -> None:
    """挂上屏变化监听（幂等；拿不到 QApplication 就什么都不做）。"""
    global _installed
    if _installed:
        return
    app = app if app is not None else QGuiApplication.instance()
    if app is None:
        return
    _installed = True
    app.screenAdded.connect(_on_screen_added)
    app.screenRemoved.connect(_on_screen_removed)
    app.primaryScreenChanged.connect(_on_primary_changed)
    for scr in app.screens():
        _attach(scr)


def _on_screen_added(scr: QScreen) -> None:
    _attach(scr)
    note_change("screenAdded")


def _on_screen_removed(_scr: QScreen) -> None:
    note_change("screenRemoved")


def _on_primary_changed(_scr: QScreen) -> None:
    note_change("primaryScreenChanged")


def _attach(scr: QScreen) -> None:
    """盯住这块屏自身的几何变化（同一块屏改分辨率走的是它，不经过 app 级信号）。"""
    if scr is None or id(scr) in _attached:
        return
    _attached.add(id(scr))
    try:
        scr.geometryChanged.connect(lambda *_: note_change("geometryChanged"))
        scr.availableGeometryChanged.connect(
            lambda *_: note_change("availableGeometryChanged")
        )
    except Exception:  # noqa: BLE001 —— 屏对象可能已经随这次变化被销毁
        log.debug("挂屏几何监听失败（屏可能已被销毁）", exc_info=True)


def reset() -> None:
    """把判据清回「从没收到过屏信号」（只给测试用）。"""
    global _last_signal_at, _installed
    _last_signal_at = 0.0
    _installed = False
    _attached.clear()
