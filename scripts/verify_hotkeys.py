"""P0 0.6 手动验证：Carbon 全局热键 → queued signal → 回调。

运行：uv run python scripts/verify_hotkeys.py
按 ⌃⌥O 应打印触发日志；按 ⌃⌥P 验证第二个热键；Ctrl+C 退出。
"""

import logging
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

sys.path.insert(0, "src")  # 便于直接运行

from barkit.core.hotkeys import HotkeyManager  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def main() -> None:
    app = QApplication(sys.argv)
    hm = HotkeyManager()

    ok_o = hm.register("recognize", "ctrl+alt+o", lambda: print(">>> ⌃⌥O 触发（recognize）"))
    ok_p = hm.register("autotype", "ctrl+alt+p", lambda: print(">>> ⌃⌥P 触发（autotype）"))
    print(f"注册结果：O={ok_o} P={ok_p}")
    print("现在切到任意窗口按 ⌃⌥O / ⌃⌥P，30 秒后自动退出")

    QTimer.singleShot(30_000, app.quit)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
