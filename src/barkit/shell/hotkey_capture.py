"""shell/hotkey_capture：键位药丸（显示 + 录制）。

**为什么不直接用 ``QKeySequenceEdit``**：它给的是 Qt 的按键序列（``QKeySequence``），
要落到配置还得再翻一次译，而**它能录到的组合与本仓真会去注册的组合不是同一个集合**
—— ``core/hotkeys`` 只认 ``_KEYCODES`` 里那批键与四个修饰键，Qt 侧却能录出
「⌘⌥⇧F20」「Media Play」这类**注册不了**的东西，表现是「录进去了、按下去没反应」。
自己录的好处是**录的时候就知道能不能注册**：不在表里的键当场忽略，不给人假希望。

**修饰键的取名按 macOS**（与 ``core/hotkeys`` 一致，别按 Qt 的名字读）：

- ``cmd``  = ⌘ = ``Qt.ControlModifier``（Qt 在 macOS 上把 Command 报成 Control）
- ``ctrl`` = ⌃ = ``Qt.MetaModifier``
- ``alt``  = ⌥ = ``Qt.AltModifier``
- ``shift``= ⇧ = ``Qt.ShiftModifier``

**录制态的退出**有三条：录到一条组合、按下 Esc（**不带修饰**的 Esc 才算取消，
⌘Esc 是合法组合）、焦点离开（切走 / 点了别处）。不给超时 —— 一个等在那儿的
键位药丸不会自己变坏，而超时会让「慢慢找那个键」的用户被莫名打断。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QPushButton

from barkit.shell.design import metrics
from barkit.shell.hotkey_catalog import display_combo
from barkit.shell.ui_kit import apply_button_tier

#: 只列出**照抄按键字符会译错**的这些：字母数字走 ``QKeyEvent.text()`` 的兜底
#: （按 ⌘O 与按 O 的 text 都是 "o"，正好对上键名），标点与控制键没有这个便利 ——
#: 键名刻意取词（``comma``）而不是符号（``,``），取值与 ``core/hotkeys._KEYCODES`` 对齐。
KEY_NAMES: dict[int, str] = {
    Qt.Key.Key_Comma: "comma",
    Qt.Key.Key_Period: "period",
    Qt.Key.Key_Slash: "slash",
    Qt.Key.Key_Semicolon: "semicolon",
    Qt.Key.Key_Apostrophe: "quote",
    Qt.Key.Key_BracketLeft: "lbracket",
    Qt.Key.Key_BracketRight: "rbracket",
    Qt.Key.Key_Backslash: "backslash",
    Qt.Key.Key_Minus: "minus",
    Qt.Key.Key_Equal: "equal",
    Qt.Key.Key_QuoteLeft: "grave",
    Qt.Key.Key_Space: "space",
    Qt.Key.Key_Return: "return",
    Qt.Key.Key_Enter: "return",
    Qt.Key.Key_Tab: "tab",
    Qt.Key.Key_Backspace: "delete",
    Qt.Key.Key_Escape: "esc",
    Qt.Key.Key_Left: "left",
    Qt.Key.Key_Right: "right",
    Qt.Key.Key_Up: "up",
    Qt.Key.Key_Down: "down",
    **{getattr(Qt.Key, f"Key_F{i}"): f"f{i}" for i in range(1, 20)},
    # 字母数字也显式登记，不靠 ``text()`` 兜底：输入法接管 / 组合输入时 text 可能是
    # 空串或别的字符，而**键是哪个**才是这里唯一要回答的事（按 ⌘O 该录到 "o"，
    # 与当时输入法给出什么无关）。
    **{getattr(Qt.Key, f"Key_{c}"): c.lower() for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
    **{getattr(Qt.Key, f"Key_{d}"): d for d in "0123456789"},
}

#: 纯修饰键按下（还没落到某个键上）：此时**不算录完**，继续等。
_MODIFIER_KEYS = frozenset({
    Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Meta, Qt.Key.Key_Alt,
    Qt.Key.Key_AltGr, Qt.Key.Key_CapsLock,
})

#: 修饰键 → combo 里的名字，顺序即拼接顺序（⌃⌥⇧⌘，系统约定的排法）
_MODIFIER_ORDER: tuple[tuple[Qt.KeyboardModifier, str], ...] = (
    (Qt.KeyboardModifier.MetaModifier, "ctrl"),
    (Qt.KeyboardModifier.AltModifier, "alt"),
    (Qt.KeyboardModifier.ShiftModifier, "shift"),
    (Qt.KeyboardModifier.ControlModifier, "cmd"),
)

#: 录制态文案（六字）；定宽只在非录制态生效，故它能整句显示。
_RECORDING_TEXT = "按下组合键…"


def event_combo(event: QKeyEvent) -> "str | None":
    """把一个按键事件翻成 combo 串（**不能注册的键返回 None**）。

    返回 ``""``（空串）是**取消**的意思：不带修饰的 Esc。
    """
    key = event.key()
    if key in _MODIFIER_KEYS:
        return None
    name = KEY_NAMES.get(key)
    if name is None:
        text = event.text()
        # 兜底只收单字符可打印键（字母 / 数字）：多字符的 text 是组合输入，不是键名
        if len(text) == 1 and text.isascii() and text.isprintable():
            name = text.lower()
    if name is None:
        return None
    mods = event.modifiers()
    parts = [n for mod, n in _MODIFIER_ORDER if mods & mod]
    if not parts and name == "esc":
        return ""
    parts.append(name)
    return "+".join(parts)


class ComboButton(QPushButton):
    """一枚键位药丸：平时显示 ``⌃⌥O``，点一下进录制态，录到就发 :attr:`captured`。

    它**不碰配置**：录到什么由外头决定要不要落盘（页面要先问冲突、再写、再重挂），
    药丸只负责「把一次按键翻成一个字符串」。
    """

    captured = Signal(str)

    def __init__(self, combo: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("pill-ghost")
        apply_button_tier(self, "inline")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._combo = combo
        self._recording = False
        self._render()
        self.clicked.connect(self._on_clicked)

    # ---------- 对外 ----------

    def combo(self) -> str:
        return self._combo

    def set_combo(self, combo: str) -> None:
        self._combo = combo
        self._render()

    @property
    def recording(self) -> bool:
        return self._recording

    def stop_recording(self) -> None:
        """外部强制退出录制态（换页 / 重建列表前必须调，否则键盘还被它抓着）。"""
        self._finish()

    # ---------- 内部 ----------

    def _on_clicked(self) -> None:
        if self._recording:
            self._finish()
            return
        self._recording = True
        self.grabKeyboard()
        self._render()

    def _finish(self) -> None:
        if not self._recording:
            return
        self._recording = False
        self.releaseKeyboard()
        self._render()

    def _render(self) -> None:
        if self._recording:
            # 放开定宽：六个字塞不进 96，宁可这一行右移也不截成「按下组…」
            self.setMinimumWidth(0)
            self.setMaximumWidth(0xFFFFFF)
            self.setText(_RECORDING_TEXT)
            return
        self.setMinimumWidth(metrics.COMBO_PILL_W)
        self.setMaximumWidth(metrics.COMBO_PILL_W)
        self.setText(display_combo(self._combo))

    def keyPressEvent(self, event: QKeyEvent) -> None:  # type: ignore[override]
        if not self._recording:
            super().keyPressEvent(event)
            return
        combo = event_combo(event)
        if combo is None:
            return
        # **先退录制态、后发信号**：槽里大概率会重建列表（本药丸随即被销毁），
        # 若在录制态中发信号，紧随其后的 ``_finish()`` 就落在了一个正在被拆的对象上。
        if combo:
            self._combo = combo
        self._finish()
        if combo:
            self.captured.emit(combo)

    def focusOutEvent(self, event) -> None:  # type: ignore[override]
        self._finish()
        super().focusOutEvent(event)
