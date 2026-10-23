"""识别窗口族：日志名、Dock 激活策略两个函数，以及**独立窗口壳**``RecognizeWindow``（组合包住内容面 ＋ ``__getattr__`` 转发 ＋ 五个Qt 虚函数重写，按纪律整个留入口）。

``window_view.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
)

from barkit.shell.macos_window import (
    POLICY_ACCESSORY,
    POLICY_REGULAR,
    set_activation_policy,
)

from .content_shell import RecognizeContent


# ---------- macOS Dock 激活策略切换 ----------
# 应用整体是 Accessory 模式（无 Dock 图标，常驻菜单栏）。但识别窗口
# 打开时用户常需要切到 Finder/其他 App 找图片再拖回窗口，没有 Dock
# 图标会导致窗口"消失"后难找回。因此窗口显示时临时切到 Regular（有
# Dock + 菜单栏 App 名），关闭时切回 Accessory，Dock 图标随窗口生命周期
# 出现/消失。非 macOS 或缺少 pyobjc 时静默降级，不影响其余逻辑。
#
# 切换动作统一走 shell/macos_window.set_activation_policy（UI 规范 §5.4 收归）：
# 与详情页的 Dock 显隐共用同一入口，避免「Dock 图标什么时候出现」散落多份实现。
# **注意**：这条只属于「独立窗口壳」`RecognizeWindow`——经 `DetailHost` 承载时由框架的
# `DetailForm` 统一切（同一种入口），内容面 `RecognizeContent` 不碰激活策略。

def _activate_macos_window() -> None:
    """把窗口带到前台（普通 raise，不跨 App 强抢焦点）。"""
    try:
        from AppKit import NSApplication
        NSApplication.sharedApplication().activateIgnoringOtherApps_(False)
    except ImportError:
        pass


def _system_is_fullscreen() -> bool:
    """检测系统当前是否处于「全屏 App」模式（有 App 占据全屏 Space）。

    用本进程即可查询的 NSApplication.presentationOptions：当某 App 全屏时，
    系统 presentationOptions 的 NSApplicationPresentationFullScreen 位（bit 0）
    会被置位。无需辅助功能/屏幕录制权限，比查前台 App 窗口 styleMask 可靠
    （后者在沙盒/无权限时拿不到窗口列表）。

    全屏模式下若强抢焦点/切 Regular 并把窗口 activate，本窗口会被拉到全屏
    Space 上覆盖全屏 App——这正是用户反馈的「窗口浮在全屏程序上」。
    """
    try:
        from AppKit import NSApplication

        opts = int(NSApplication.sharedApplication().presentationOptions())
        # NSApplicationPresentationFullScreen = 1 << 0
        return bool(opts & (1 << 0))
    except ImportError:
        return False

# ---------- 独立窗口壳（旧路径：热键 open_window 直接用） ----------


class RecognizeWindow(QMainWindow):
    """识别窗口（独立窗口壳）：包住 ``RecognizeContent``，负责窗口级生命周期。

    仅热键复用 ``open_window`` 那条旧路径仍会直接 new 它；面板入口已统一走
    ``DetailHost``（``RecognizeContent`` 经 ``DetailEntrySpec.build`` 交给框架）。
    所有业务属性与方法经 ``__getattr__`` 转发到内部 ``_content``，故旧测试与调用方
    直接访问 ``win._view`` / ``win.try_load_clipboard()`` 等仍可原样工作。
    """

    def __init__(self, plugin_cfg: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._content = RecognizeContent(plugin_cfg, parent=self)
        self.setCentralWidget(self._content)
        self.setWindowTitle("识别（OCR + 条码）")
        self.resize(1040, 640)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        # 整窗拖放转发：QMainWindow 默认不接受拖放，覆盖标题栏区域需窗口层拦截
        self.setAcceptDrops(True)

    def __getattr__(self, name: str):
        """业务成员转发到内部内容面 ``_content``。

        ``__getattr__`` 仅在常规查找失败时触发；``_content`` 自身写在 ``__dict__``
        里，不走这里。构造中途（``_content`` 尚未赋值）也不应触发——Qt 事件只在外层
        ``show()`` 之后才来，届时 ``_content`` 早已就位，这里多一道 None 兜底防崩。
        """
        content = self.__dict__.get("_content")
        if content is None:
            raise AttributeError(name)
        try:
            return content.__getattribute__(name)
        except AttributeError:
            raise AttributeError(
                f"'{type(self).__name__}' 对象没有属性 '{name}'"
            ) from None

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # 显示时临时切到 Regular 激活策略：出现 Dock 图标 + 菜单栏 App 名，
        # 方便用户切到其他 App 找图后从 Dock 找回本窗口（关闭时再切回 Accessory）。
        # 但若系统正处于全屏 App 模式，则只切 Regular（给 Dock 图标）而【不】
        # 强抢焦点/activate——否则本窗口会被 macOS 拉到全屏 Space 上覆盖全屏
        # App（用户反馈的实机问题）。此时仅靠 Qt 的 show()+raise_ 在当前 Space
        # 处理窗口，不侵入全屏 Space。
        set_activation_policy(POLICY_REGULAR)  # Dock 图标随窗口出现
        if not _system_is_fullscreen():
            _activate_macos_window()

    def closeEvent(self, event) -> None:
        self._content._pool.waitForDone(300)  # 关闭时给 worker 短暂收尾，随后窗口销毁释放原图
        # 窗口关闭即切回 Accessory，销毁 Dock 图标（临时出现，生命周期跟随窗口）。
        set_activation_policy(POLICY_ACCESSORY)
        super().closeEvent(event)

    # ---------- 整窗拖放（§4.1 拾取态：拖入图片文件）----------
    # 内容面自身已接拖放，这里再在窗口层拦截一次，覆盖 QMainWindow 不接受拖放的
    # 标题栏区域，转发给内容面的统一载入路径。

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        event.accept()

    def dropEvent(self, event) -> None:
        urls = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if not urls:
            event.ignore()
            return
        # 走统一文件载入路径（来源语义由内容面管理，避免识别错乱）。
        self._content._on_file_dropped(urls[0])
        event.acceptProposedAction()


# 再导出：调用面（``plugin.py`` 与测试）仍从**入口**取这些名字 —— 正文搬去了兄弟
# 模块，入口只负责把它们摆回同一张脸上。平铺模式没有 ``__init__.py`` 替我们做这件
# 事，故这一段是**手工**的（引擎只带本模块自己用得到的那些）。
# 上面那几枚 mixin 与 ``RecognizeContent`` 是引擎按「主类要在定义类那一刻拿到全部
# 基类」自动生成的；``RecognizeContent`` 的类声明在 content_shell.py（那里是本体，
# 用 plan 的 CLASS_DECL_BY_MOD 换掉基类列表），不在这份入口里。
from .view_rows import ResultRow, _ResultList
from .view_task import _qimage_to_pil, _RecognizeTask, _TaskSignals, log

__all__ = [
    "RecognizeContent",
    "RecognizeWindow",
    "ResultRow",
    "_qimage_to_pil",
    "_ResultList",
    "_RecognizeTask",
    "_TaskSignals",
    "log",
]
