"""autotype 引擎：CGEvent Unicode 直注 + TIS 输入法处理（方案 §4.4，任务 2.5）。

收编自 autotype_password.py，改造点：
- CLI print 全部改为 logging，由宿主（GUI）统一落日志
- 发送函数（_post_char/_post_keycode）为模块级可替换点，测试 monkeypatch 不真发键
- inject() 支持 per_char（进度回调）与 should_stop（取消/中断），供 UI 编排

已知限制（方案 §4.4）：开启 Secure Event Input 的应用（1Password、部分银行/密码框）
收不到注入事件，静默失败；README 已注明，不影响 v1。
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# macOS virtual keycode（仅用于 Backspace / Return 等功能键）
BACKSPACE = 51
RETURN = 36

try:
    import Quartz

    _QUARTZ_OK = True
except ImportError:
    _QUARTZ_OK = False


# ----------------------------------------------------------------------------
# 发送原语（测试可 monkeypatch 模块级函数替换，避免真发键）
# ----------------------------------------------------------------------------
def _post_char(ch: str, interval: float) -> None:
    """发送单个字符：CGEventKeyboardSetUnicodeString 直注，不受输入法影响。"""
    if not _QUARTZ_OK:
        raise RuntimeError("Quartz 不可用，无法注入键盘事件")
    ev_down = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
    ev_up = Quartz.CGEventCreateKeyboardEvent(None, 0, False)
    Quartz.CGEventKeyboardSetUnicodeString(ev_down, len(ch), ch)
    Quartz.CGEventKeyboardSetUnicodeString(ev_up, len(ch), ch)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev_down)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev_up)
    time_sleep(interval)


def _post_keycode(code: int, interval: float) -> None:
    """发送功能键（Backspace / Return 等）。"""
    if not _QUARTZ_OK:
        raise RuntimeError("Quartz 不可用，无法注入键盘事件")
    down = Quartz.CGEventCreateKeyboardEvent(None, code, True)
    up = Quartz.CGEventCreateKeyboardEvent(None, code, False)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
    time_sleep(interval)


def time_sleep(seconds: float) -> None:
    """统一 sleep 出口，测试可提速/替换。"""
    import time

    time.sleep(seconds)


def post_char(ch: str) -> None:
    """发送单个字符，不等睡（节奏由调用方控制，如 UI 定时器）。"""
    _post_char(ch, 0.0)


def post_keycode(code: int) -> None:
    """发送功能键，不等睡（节奏由调用方控制）。"""
    _post_keycode(code, 0.0)


# ----------------------------------------------------------------------------
# macOS 输入法检测 / 切换（ctypes 直连 Carbon/HIToolbox 的 TIS API）
# ----------------------------------------------------------------------------
class _TIS:
    """懒加载 TIS 绑定。加载失败则所有方法返回 None / False（功能降级）。"""

    def __init__(self) -> None:
        self._ok = False
        self._err = ""
        self._lib = self._cf = None
        self._K_ID = self._K_CAT = None
        try:
            import ctypes.util

            lib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("Carbon"))
            cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
            _ = lib.TISCopyCurrentKeyboardInputSource
            cf.CFStringGetCStringPtr.restype = ctypes.c_char_p
            cf.CFStringGetCStringPtr.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            cf.CFStringGetLength.restype = ctypes.c_long
            cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
            cf.CFStringGetCString.restype = ctypes.c_bool
            cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
            cf.CFArrayGetCount.restype = ctypes.c_long
            cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
            cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
            cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
            cf.CFRelease.restype = None
            cf.CFRelease.argtypes = [ctypes.c_void_p]
            lib.TISCopyCurrentKeyboardInputSource.restype = ctypes.c_void_p
            lib.TISGetInputSourceProperty.restype = ctypes.c_void_p
            lib.TISGetInputSourceProperty.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            lib.TISCreateInputSourceList.restype = ctypes.c_void_p
            lib.TISCreateInputSourceList.argtypes = [ctypes.c_void_p, ctypes.c_bool]
            lib.TISSelectInputSource.restype = ctypes.c_int32
            lib.TISSelectInputSource.argtypes = [ctypes.c_void_p]
            self._ctypes = ctypes
            self._lib = lib
            self._cf = cf
            self._K_ID = ctypes.c_void_p.in_dll(lib, "kTISPropertyInputSourceID").value
            self._K_CAT = ctypes.c_void_p.in_dll(lib, "kTISPropertyInputSourceCategory").value
            self._ok = True
        except Exception as e:  # noqa: BLE001
            self._err = str(e)

    @property
    def available(self) -> bool:
        return self._ok

    def _cf2py(self, p) -> str | None:
        if not p:
            return None
        b = self._cf.CFStringGetCStringPtr(p, 0x08000100)
        if b:
            return b.decode("utf-8")
        n = self._cf.CFStringGetLength(p)
        buf = self._ctypes.create_string_buffer(n * 4 + 2)
        self._cf.CFStringGetCString(p, buf, len(buf), 0x08000100)
        return buf.value.decode("utf-8")

    def current(self) -> tuple[str | None, bool]:
        """返回 (输入源 id, 是否为纯键盘布局/英文无候选框)。"""
        if not self._ok:
            return None, False
        cur = self._lib.TISCopyCurrentKeyboardInputSource()
        if not cur:
            return None, False
        sid = self._cf2py(self._lib.TISGetInputSourceProperty(cur, self._K_ID))
        self._cf.CFRelease(cur)
        return sid, bool(sid and sid.startswith("com.apple.keylayout."))

    def select(self, target_id: str) -> bool:
        if not self._ok:
            return False
        arr = self._lib.TISCreateInputSourceList(None, False)
        if not arr:
            return False
        ok = False
        try:
            n = self._cf.CFArrayGetCount(arr)
            for i in range(n):
                s = self._cf.CFArrayGetValueAtIndex(arr, i)
                if self._cf2py(self._lib.TISGetInputSourceProperty(s, self._K_ID)) == target_id:
                    ok = self._lib.TISSelectInputSource(s) == 0
                    break
        finally:
            self._cf.CFRelease(arr)
        return ok


TIS = _TIS()
ABC_ID = "com.apple.keylayout.ABC"


def ensure_english(auto_switch: bool, require: bool) -> tuple[bool, str]:
    """检测并确保英文布局。返回 (是否可安全输入, 说明文案)。

    - TIS 不可用：降级（require=False 时继续）；require=True 时中止
    - 非英文布局且 auto_switch → 尝试切 ABC
    - 切不回且 require → 中止，提示手动切
    """
    sid, is_layout = TIS.current()
    if not TIS.available:
        msg = "无法读取当前输入法（TIS 不可用），请自行确认已切到英文/ABC。"
        log.warning(msg)
        return (not require), msg
    if is_layout:
        return True, f"当前输入法为英文布局：{sid}"
    if auto_switch:
        log.info("当前输入法非英文布局（%s），尝试切换到 ABC …", sid)
        if TIS.select(ABC_ID):
            time_sleep(0.3)
            sid2, ok2 = TIS.current()
            if ok2:
                return True, f"已切换到英文布局：{sid2}"
            msg = f"切换指令已发送，但复核仍为：{sid2}"
            log.warning("%s", msg)
        else:
            msg = "自动切换输入法失败。"
            log.warning("%s", msg)
    else:
        msg = f"当前输入法非英文布局：{sid}，且未开启自动切换。"
        log.warning("%s", msg)
    if require:
        return False, "请把输入法切到【英文/ABC】后重试。"
    return True, "已降级：非英文布局下继续输入。"


# ----------------------------------------------------------------------------
# 执行编排
# ----------------------------------------------------------------------------
def inject(
    text: str,
    interval: float = 0.03,
    press_enter: bool = False,
    enter_delay: float = 0.3,
    per_char=None,
    should_stop=None,
) -> int:
    """逐字符注入 text；press_enter 时收尾按回车。返回实际发送的字符数。

    - per_char(sent: int, ch: str)：每发送一个字符回调（UI 进度/浮层）
    - should_stop() -> bool：每字符发送前检查，True 则中断返回
    """
    sent = 0
    for ch in text:
        if should_stop is not None and should_stop():
            log.info("注入被中断（已发送 %d 字符）", sent)
            return sent
        _post_char(ch, interval)
        sent += 1
        if per_char is not None:
            per_char(sent, ch)
    if press_enter:
        if should_stop is not None and should_stop():
            log.info("注入中断：已发送 %d 字符，跳过回车", sent)
            return sent
        time_sleep(enter_delay)
        _post_keycode(RETURN, 0.05)
    return sent


def resolve_profile(plugin_cfg: dict, name: str) -> dict | None:
    """profile 名 → 合并默认值后的执行参数；找不到/非法返回 None。（纯函数可测）

    合并顺序：profile 缺省字段（delay/interval/press_enter）回退插件段全局默认。
    """
    profiles = plugin_cfg.get("profiles") or []
    for p in profiles:
        if isinstance(p, dict) and p.get("name") == name:
            text = p.get("text")
            if not isinstance(text, str) or not text:
                log.warning("profile %s 的 text 为空，拒绝执行", name)
                return None
            return {
                "name": name,
                "text": text,
                # delay / interval 显式 0 都合法（立即输入 / 逐字符不等待），不能因 falsy
                # 回退全局默认 —— 用 `is not None` 判（设置页暴露 `interval` 后更容易踩到）
                "delay": p.get("delay") if p.get("delay") is not None else plugin_cfg.get("delay", 4),
                "interval": float(
                    p.get("interval") if p.get("interval") is not None
                    else plugin_cfg.get("interval", 0.03)
                ),
                "press_enter": bool(p.get("press_enter") or plugin_cfg.get("press_enter", False)),
                "enter_delay": float(plugin_cfg.get("enter_delay", 0.3)),
            }
    return None


def validate_profile(name: str, text: str, delay: int | None) -> str | None:
    """profile 字段校验，返回错误文案（None = 通过）。（纯函数，配置窗口复用）"""
    if not name.strip():
        return "名称不能为空"
    if not text:
        return "输入文本不能为空"
    if delay is not None and (not isinstance(delay, int) or delay < 0):
        return "延迟必须是大于等于 0 的整数"
    return None