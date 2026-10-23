"""core/hotkeys：全局热键（方案 §5 修订）。

**实现变更（P0 实测结论）**：Carbon RegisterEventHotKey 在 Qt 6 macOS 进程中
收不到事件——Qt 6 用 CFRunLoop 直驱，不泵 Carbon 事件队列（纯 CLI 进程能收到，
GUI 进程收不到，已用对照组实验证实）。改用 **CGEventTap 会话级监听**：
- Quartz.CGEventTapCreate 监听 keyDown，自行匹配 keycode + 修饰键
- tap 源挂到主 CFRunLoop（Qt 事件循环会泵它），无需额外线程/轮询
- **需要辅助功能权限**（autotype 本就需要，非新增负担）；未授权时热键不可用并引导

对外 API 不变：register(name, combo, callback) / unregister_all()，
配置仍是 YAML 字符串（如 "ctrl+alt+o"）。

**注册表已拆到 `core/hotkey_table`**（2026-09-18，P0 ⑤）：本模块只留「怎么收到按键」，
「谁占了哪个键位、按下该叫谁」归那张表。旧实现 `register()` 直接 `dict[name] = …` 覆盖、
`_match()` 命中首个即返回 ⇒ 两条热键配成同一个键位时**静默顶掉**一条，谁生效取决于插件
发现顺序（表现是「有时不灵」）。现在键位冲突与「同一个热键名被另一个回调占用」都**拒绝登记**
并给出指名道姓的日志；拆表同时让纯策略能在非 macOS 上被完整证明（`tests/test_hotkey_table.py`）。
"""

from __future__ import annotations

import ctypes
import logging

import objc
from PySide6.QtCore import QObject, Qt, Signal

from barkit.core.hotkey_table import Binding, Conflict, HotkeyTable

log = logging.getLogger(__name__)

try:
    import Quartz

    _QUARTZ_OK = True
except ImportError:
    _QUARTZ_OK = False

# pyobjc 未绑定 CFRunLoop 系列函数，直调 CoreFoundation
_cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
_cf.CFRunLoopAddSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
try:
    _kCFRunLoopCommonModes = ctypes.c_void_p.in_dll(_cf, "kCFRunLoopCommonModes")
except ValueError:
    _kCFRunLoopCommonModes = None

# ANSI keycode（kVK_ANSI_*），字母/数字键必须用它而非字符码
_KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
    "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46,
    "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26, "8": 28, "9": 25, "0": 29,
    "return": 36, "tab": 48, "space": 49, "delete": 51, "esc": 53,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    "f13": 105, "f14": 107, "f15": 113, "f16": 106, "f17": 64, "f18": 79, "f19": 80,
    # 标点 / 方向与控制键（kVK_ANSI_*）：键名取词不取符号 —— 组合串按 "+" 分段，
    # 键名里出现 "+" 会被当成修饰键；符号本身也难在 YAML / 日志里读。
    # 补这批的直接来由是设置页「快捷键」那一页的键位录制：此前只有字母数字与 F1–F19，
    # 用户按下 ⌘, 会得到「不支持的按键」，而系统偏好里 ⌘, 恰恰是最常见的组合之一。
    "comma": 43, "period": 47, "slash": 44, "semicolon": 41, "quote": 39,
    "lbracket": 33, "rbracket": 30, "backslash": 42,
    "minus": 27, "equal": 24, "grave": 50,
}

# 组合键字符串 → CGEvent 修饰键掩码
_MODIFIER_MASKS = {
    "cmd": Quartz.kCGEventFlagMaskCommand if _QUARTZ_OK else 0,
    "shift": Quartz.kCGEventFlagMaskShift if _QUARTZ_OK else 0,
    "alt": Quartz.kCGEventFlagMaskAlternate if _QUARTZ_OK else 0,
    "option": Quartz.kCGEventFlagMaskAlternate if _QUARTZ_OK else 0,
    "ctrl": Quartz.kCGEventFlagMaskControl if _QUARTZ_OK else 0,
}
_ALL_MODIFIERS = (
    (Quartz.kCGEventFlagMaskCommand | Quartz.kCGEventFlagMaskShift
     | Quartz.kCGEventFlagMaskAlternate | Quartz.kCGEventFlagMaskControl)
    if _QUARTZ_OK else 0
)


def parse_combo(combo: str) -> tuple[int, int]:
    """"ctrl+alt+o" → (keycode, modifier_mask)。不合法抛 ValueError。"""
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"空热键配置：{combo!r}")
    key = parts[-1]
    if key not in _KEYCODES:
        raise ValueError(f"不支持的按键：{key!r}（来自 {combo!r}）")
    mask = 0
    for mod in parts[:-1]:
        if mod not in _MODIFIER_MASKS:
            raise ValueError(f"不支持的修饰键：{mod!r}（来自 {combo!r}）")
        mask |= _MODIFIER_MASKS[mod]
    return _KEYCODES[key], mask


class HotkeyManager(QObject):
    """CGEventTap 全局热键。triggered 以 queued 方式回 Qt 主循环。"""

    triggered = Signal(str)  # 热键名

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._table = HotkeyTable()  # 名字 → 键位／回调（策略＋去重都在里面）
        #: 名字 → 最近一次注册失败的原因（挂上 / 撤绑即除名，见 :meth:`register`）
        self._failures: dict[str, str] = {}
        self._tap = None
        self._tap_source = None
        self.triggered.connect(self._dispatch, Qt.ConnectionType.QueuedConnection)

    # ---------- 注册 ----------

    @property
    def available(self) -> bool:
        return _QUARTZ_OK

    def conflict(self, name: str, combo: str, callback) -> "Conflict | None":
        """这次改动会不会撞上已在册的某条（**写盘之前**问，故与 register 同形状）。

        ``callback`` 必须给真身：注册表判「同名被另一个回调占用」靠它，传 ``None``
        会把「同一条热键换个键位」误判成名字冲突（``existing.callback != None``）。
        """
        try:
            keycode, mask = parse_combo(combo)
        except ValueError:
            return None
        return self._table.conflict(name, keycode, mask, callback)

    def register(self, name: str, combo: str, callback) -> bool:
        """注册热键。失败返回 False：Quartz 不可用 / 配置非法 / **键位或名字冲突** / tap 建不起来。

        冲突**不再静默覆盖** —— 两条拒绝规则与措辞在 `core/hotkey_table`。冲突检查放在建 tap
        之前：建好了 tap 却一条热键都没上去，是个没有意义的中间态。

        **失败原因一律存进 :data:`_failures`**（成功与撤绑时清掉）：启动那批热键是**无界面**
        注册的，注册失败时用户那边只有「按了没反应」，而这一行日志他不会去看。
        :meth:`failure_of` 让「快捷键」页把原因摆到出问题的那一行上。
        """
        reason = ""
        if not _QUARTZ_OK:
            reason = "Quartz 不可用（本机热键不可用）"
        else:
            try:
                keycode, mask = parse_combo(combo)
            except ValueError as e:
                reason = str(e)
            else:
                conflict = self._table.conflict(name, keycode, mask, callback)
                if conflict is not None:
                    reason = conflict.describe(name, combo)
                elif not self._ensure_tap():
                    reason = "CGEventTap 不可用（检查辅助功能权限）"
                else:
                    self._table.bind(name, keycode, mask, callback)
                    self._failures.pop(name, None)
                    log.info("热键已注册：%s → %s", name, combo)
                    return True
        self._failures[name] = reason
        log.error("热键 %s 注册失败：%s", name, reason)
        return False

    def failures(self) -> "dict[str, str]":
        """名字 → 失败原因（**只收最近一次注册失败**的，挂上就除名）。

        给「快捷键」页与诊断用：启动那批热键注册失败时没有任何界面在场，
        只能等用户自己翻到那一页 —— 那时原因要还在。
        """
        return dict(self._failures)

    def failure_of(self, name: str) -> "str | None":
        """这条热键**此刻**是不是没挂上；是就给原因，挂上了给 None。"""
        return self._failures.get(name)

    def get(self, name: str) -> "Binding | None":
        """名字 → 当前登记的热键（含回调）。冲突判定回查「自身真身」用。"""
        return self._table.get(name)

    def unregister_all(self) -> None:
        if self._tap is not None:
            Quartz.CGEventTapEnable(self._tap, False)
        self._table.clear()
        self._failures.clear()

    def unregister(self, name: str) -> None:
        """移除单个热键（运行期禁用某插件时，只撤掉它声明的热键）。

        全部移除后顺手停用 tap（无监听目标时无需继续泵事件）。**只挪走自己那一条** ——
        腾出的键位随后可被别的热键登记（这是「禁用再启用」能换回原键位的前提）。
        """
        self._table.unbind(name)
        self._failures.pop(name, None)
        if not self._table and self._tap is not None:
            Quartz.CGEventTapEnable(self._tap, False)

    # ---------- tap ----------

    def _ensure_tap(self) -> bool:
        """创建 tap 并挂到主 CFRunLoop（Qt 事件循环负责泵它）。幂等。"""
        if self._tap is not None:
            return True
        # kCGEventTapDisabledByTimeout/UserInput 是 tap 状态通知，
        # 不在 mask 中表达（且会自动送达回调），mask 只需真实事件类型
        mask = Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
        self._tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly,
            mask,
            self._on_event,
            None,
        )
        if self._tap is None:
            log.error("CGEventTapCreate 失败：未授予辅助功能权限")
            return False
        self._tap_source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        # objc 包装对象取原始指针，交给 ctypes 调 CFRunLoopAddSource
        _cf.CFRunLoopAddSource(
            _cf.CFRunLoopGetCurrent(),
            ctypes.c_void_p(objc.pyobjc_id(self._tap_source)),
            _kCFRunLoopCommonModes,
        )
        Quartz.CGEventTapEnable(self._tap, True)
        return True

    def _match(self, keycode: int, flags: int) -> str | None:
        """按 keycode + 修饰键精确匹配（忽略 CapsLock/功能键位等无关标志）。

        裁掉无关位之后交给注册表 —— 匹配规则住在 `core/hotkey_table`，本模块只负责
        「从 CGEvent 里取出 keycode 与 flags」这段。按热键名取回调走 :meth:`_dispatch`
        一次 `get`，不再由这里返回名字再查一遍。
        """
        binding = self._table.match(keycode, flags & _ALL_MODIFIERS)
        return binding.name if binding is not None else None

    def _on_event(self, proxy, event_type, event, refcon):
        if event_type in (
            Quartz.kCGEventTapDisabledByTimeout,
            Quartz.kCGEventTapDisabledByUserInput,
        ):
            # 系统可能因超时禁用 tap，必须手动重新启用
            log.warning("CGEventTap 被系统禁用（%s），重新启用", event_type)
            Quartz.CGEventTapEnable(self._tap, True)
            return None
        if event_type == Quartz.kCGEventKeyDown:
            keycode = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
            name = self._match(keycode, Quartz.CGEventGetFlags(event))
            if name:
                self.triggered.emit(name)
        return None

    def _dispatch(self, name: str) -> None:
        binding = self._table.get(name)
        if binding is None or binding.callback is None:
            return
        try:
            binding.callback()
        except Exception:
            log.exception("热键 %s 回调异常", name)
