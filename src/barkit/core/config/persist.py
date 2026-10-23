"""``_PersistMixin``：原子写盘与 mtime 热重载看门狗。

``config.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import yaml

from PySide6.QtCore import QTimer

from .schema import validate
from .store import atomic_write


class _PersistMixin:
    """写盘（原子写 ＋ chmod 600 ＋ 立即重载）与 mtime 热重载的看门狗。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 写入 ----------

    def save_user(self, user_data: dict) -> None:
        """覆盖写用户配置（原子写 + chmod 600），并立即重载。"""
        validate(user_data)
        atomic_write(self.user_path, yaml.safe_dump(user_data, allow_unicode=True, sort_keys=False))
        self._reload_from_disk()

    # ---------- 热重载 ----------

    def start_watching(self, interval_ms: int = 1000) -> None:
        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(interval_ms)
        self._watch_timer.timeout.connect(self._check_mtime)
        self._watch_timer.start()

    def stop_watching(self) -> None:
        if self._watch_timer:
            self._watch_timer.stop()

    def _check_mtime(self) -> None:
        try:
            mtime = self.user_path.stat().st_mtime
        except FileNotFoundError:
            return
        if self._mtime is not None and mtime == self._mtime:
            return
        old = self._data
        self._reload_from_disk()
        if self._data is not old:
            self.changed.emit(self._data)
