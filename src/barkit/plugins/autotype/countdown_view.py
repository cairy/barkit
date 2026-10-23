"""autotype/countdown_view：倒计时 HUD ＋ 逐字符注入浮层（**运行期面**，§7.3）。

生命周期随窗口、由插件在业务时刻打开 ⇒ 归运行期面，模块名 `*_view.py`
（原名 `countdown.py`，2026-09-19 随阶段 3 改名）。类名仍叫 `AutotypeOverlay`：
本仓运行期面的类名说的是**控件的 Qt 种类**，不跟模块名逐字对齐 —— 同
`hosts_guard/manager_view.py` 的 `HostsManagerDialog`。

**2026-09-22：窗口那一半已收归框架** `shell/overlay.py`（无边框置顶 / 显示时不激活 app /
自由拖动 / 跟随光标所在屏 / 显示时长 / 退场口径），本文件只剩业务：

- 倒计时内切到目标窗口聚焦；倒计时结束逐字注入（QTimer 驱动，不阻塞主线程）
- 倒计时为 profile 级：spec 由 engine.resolve_profile 产出（delay 已合并默认值）
- 按钮在倒计时阶段是「取消」、注入阶段是「停止」，可中途中断
- 退场时两种口径都发：框架口径 `dismissed(reason)` ＋ 本组件自己的 `finished(ok)`
  （`plugin.py` 接的是后者 —— 完成了才记「上次使用」）
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel

from barkit.plugins.autotype import engine
from barkit.shell.overlay import DISMISS_CANCELLED, DISMISS_COMPLETED, Overlay
from barkit.shell.ui_kit import apply_text_role, tool_button

log = logging.getLogger(__name__)

STEP_MS = 100  # 计时粒度

# 卡片尺寸（内容区 ＋ 内容边距 = 整个窗）。尺寸由**这一屏内容**说了算，属业务，不进框架
# 标尺；沿用收归前的值，观感一字未动。
_CONTENT_W = 360
_CONTENT_H = 130


class AutotypeOverlay(Overlay):
    """一键输入的倒计时 + 注入浮层。finished(True) = 完成输入，finished(False) = 取消/中断。"""

    finished = Signal(bool)

    def __init__(self, spec: dict, parent=None) -> None:
        super().__init__(parent, size=(_CONTENT_W, _CONTENT_H))
        self._spec = spec
        self._text = spec["text"]
        self._interval = spec["interval"]
        self._press_enter = spec["press_enter"]
        self._enter_delay = spec["enter_delay"]

        self._phase = "countdown"   # countdown → typing
        self._left_ms = int(spec["delay"] * 1000)
        self._sent = 0
        self._cancelled = False
        self._next_char_at = 0.0
        self._enter_pending = False
        self._enter_at = 0.0

        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._build_ui()

        # 不管因为什么退场（到点 / 外部关闭 / 业务走完），先把注入停掉 ——
        # 否则 deleteLater 排队那段时间里 QTimer 还会继续往外发字。
        self.dismissed.connect(self._stop_injection)

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(STEP_MS)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start()

    # ---------- UI ----------

    def _build_ui(self) -> None:
        layout = self.content_layout

        title = QLabel(f"一键输入：{self._spec['name']}")
        apply_text_role(title, "overlay_title")
        layout.addWidget(title)

        self._status_label = QLabel()
        apply_text_role(self._status_label, "overlay_count")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._status_label)

        hint = QLabel("请切到目标窗口并聚焦输入框")
        apply_text_role(hint, "overlay_hint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._cancel_btn = tool_button("取消", "pill-ghost")
        self._cancel_btn.clicked.connect(self._on_cancel)
        buttons.addWidget(self._cancel_btn)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self._update_label()

    def _update_label(self) -> None:
        if self._phase == "countdown":
            seconds = max(0, -(-self._left_ms // 1000))  # 向上取整
            self._status_label.setText(f"{seconds} 秒后开始输入…")
            self._cancel_btn.setText("取消")
        else:
            if self._sent >= len(self._text) and self._press_enter:
                self._status_label.setText("处理回车…")
            else:
                self._status_label.setText(f"正在输入 {self._sent}/{len(self._text)}")
            self._cancel_btn.setText("停止")

    # ---------- 阶段推进 ----------

    def _tick(self) -> None:
        if self._cancelled:
            return
        if self._phase == "countdown":
            self._left_ms -= STEP_MS
            if self._left_ms <= 0:
                self._start_typing()
            else:
                self._update_label()
        elif self._phase == "typing":
            self._tick_typing()

    def _start_typing(self) -> None:
        log.info("autotype 倒计时结束，开始注入（%d 字符）", len(self._text))
        self._phase = "typing"
        self._next_char_at = time.monotonic() + self._interval
        self._update_label()

    def _tick_typing(self) -> None:
        now = time.monotonic()
        if self._enter_pending:
            if now >= self._enter_at:
                self._send_key(engine.RETURN)
                self._finish(True)
            return
        if now < self._next_char_at:
            return
        if self._sent < len(self._text):
            self._send_char(self._text[self._sent])
            self._sent += 1
            self._next_char_at = now + self._interval
            self._update_label()
            return
        # 全部字符发出
        if self._press_enter:
            self._enter_pending = True
            self._enter_at = now + self._enter_delay
            self._update_label()
        else:
            self._finish(True)

    # ---------- 发送（包一层便于差装/兜底） ----------

    def _send_char(self, ch: str) -> None:
        engine.post_char(ch)

    def _send_key(self, code: int) -> None:
        engine.post_keycode(code)

    # ---------- 退场 ----------

    def _stop_injection(self, _reason: str) -> None:
        """退场钩子：注入时钟必须比窗口先停（连接见 __init__）。"""
        self._tick_timer.stop()

    def _on_cancel(self) -> None:
        self._cancelled = True
        log.info("autotype 浮层被取消（phase=%s，已发送 %d 字符）", self._phase, self._sent)
        self._finish(False)

    def _finish(self, ok: bool) -> None:
        log.info("autotype %s：%s", "完成" if ok else "已取消",
                 f"共注入 {self._sent} 字符" if ok else f"已发送 {self._sent} 字符")
        self.dismiss(DISMISS_COMPLETED if ok else DISMISS_CANCELLED)
        self.finished.emit(ok)
