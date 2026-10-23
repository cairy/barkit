"""BarKit 应用入口：只做进程级准备，装配交给 ``shell/app_shell``。

进程级准备 = 日志 / 单实例锁 / QApplication / 字体与主题。
其余「core → 插件 → 面板 / 设置 / 菜单栏 / 徽标 → 退出清理」的全链路接线
全部在 :class:`barkit.shell.app_shell.AppShell` 中，新增 UI 位置不必再动入口。

例外：子命令（`barkit doctor`）在**进程级准备之前**分派 —— 自检不需要日志、
不需要单实例锁、更不需要 QApplication，它要能在这些环节坏掉时照跑。
"""

from __future__ import annotations

import fcntl
import logging
import os
import sys

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from barkit import cli, paths
from barkit.core.logging_setup import capture_stdio, setup_logging
from barkit.shell import screen_settle, styles
from barkit.shell.app_shell import AppShell

log = logging.getLogger(__name__)

# 单实例锁文件句柄，须常驻引用防止 GC 后锁被释放
_lock_file = None


def _acquire_single_instance() -> bool:
    """flock 判重：已有实例运行则返回 False。"""
    global _lock_file
    paths.APP_SUPPORT.mkdir(parents=True, exist_ok=True)
    lock_path = paths.APP_SUPPORT / ".lock"
    _lock_file = open(lock_path, "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    _lock_file.write(str(os.getpid()))
    _lock_file.flush()
    return True


def main() -> None:
    # 子命令（`barkit doctor`）先分派；dispatch 返回 None = 不是 CLI 之路，走 GUI。
    subcommand = cli.dispatch(sys.argv[1:])
    if subcommand is not None:
        sys.exit(subcommand)

    # F.1：~/Library/Logs/barkit/barkit.log（单文件 2 MB，写满就地截尾）+ 全局异常钩子
    setup_logging()
    # 「有没有终端」不再是「看不看得到」的前提：此后 print / 第三方 stderr 一并落日志。
    # 必须紧随 setup_logging —— 接管发生在它之后，那之前的输出已经在文件里了。
    capture_stdio()

    if not _acquire_single_instance():
        log.info("BarKit 已在运行，退出。")
        sys.exit(0)

    app = QApplication(sys.argv)
    app.setApplicationName(paths.APP_NAME)
    # 使用系统默认字体（macOS=SF、中文自动回退 PingFang），不在 QSS 里硬编码字体族，
    # 避免 Qt 解析不到字体名（如 SF Pro Text）而反复扫描字体库、刷告警并拖慢启动。
    app.setFont(QFont())
    app.setQuitOnLastWindowClosed(False)  # !关窗不退出，常驻菜单栏
    # 设计系统：浅/深主题一套令牌驱动（详见 barkit.shell.styles）。
    # apply_theme 设置全局 QSS；watch_system_theme 监听系统外观变化自动换肤。
    styles.apply_theme(app)
    styles.watch_system_theme(app)
    # 屏变化监听：它给出的「屏正在变」是**首次上屏要不要让路的唯一判据**。
    # 必须赶在装配之前挂上——装配期插件就可能拉起副屏（拉起副屏＝一次屏变化），
    # 而那段窗口里诞生的窗会被窗口服务器钉在 (0,0) 且进程内救不回来（见 screen_settle）。
    screen_settle.install(app)

    sys.exit(AppShell(app).run())


if __name__ == "__main__":
    main()
