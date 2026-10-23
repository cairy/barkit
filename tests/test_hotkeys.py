"""core/hotkeys 测试（P0 0.6，CGEventTap 实现；真实按键由手动验证）。

**本文件只能在 macOS 跑**：模块顶层 `import objc` / `import Quartz`。所以「谁占了哪个
键位、按下该叫谁」那半（含两条冲突拒绝规则）**不在这里** —— 它已拆到平台无关的
`core/hotkey_table`，用例在 `tests/test_hotkey_table.py`（任何平台可跑）。留在这里的是
两件真需要 macOS 的事：① `parse_combo`（键位表与修饰键掩码来自 Carbon/Quartz）；
② `HotkeyManager._match` 里「从 CGEvent 的 flags 裁掉无关位」这一步。
"""

import pytest

from barkit.core.hotkey_table import HotkeyTable
from barkit.core.hotkeys import HotkeyManager, parse_combo

CTRL = 0x40000
SHIFT = 0x20000
ALT = 0x80000
CMD = 0x100000


def test_parse_ctrl_alt_o():
    keycode, mask = parse_combo("ctrl+alt+o")
    assert keycode == 31  # ANSI kVK_ANSI_O
    assert mask == CTRL | ALT


def test_parse_order_insensitive():
    assert parse_combo("alt+ctrl+p") == parse_combo("ctrl+alt+p")


def test_parse_cmd_shift():
    _, mask = parse_combo("cmd+shift+1")
    assert mask == CMD | SHIFT


def test_parse_rejects_bad_key():
    with pytest.raises(ValueError):
        parse_combo("ctrl+ä")


def test_parse_rejects_bad_modifier():
    with pytest.raises(ValueError):
        parse_combo("super+o")


def test_parse_rejects_empty():
    with pytest.raises(ValueError):
        parse_combo("  ")


def test_match_exact_modifiers():
    hm = HotkeyManager.__new__(HotkeyManager)  # 不触发 QObject/tap 初始化
    hm._table = HotkeyTable()
    hm._table.bind("demo", 29, CTRL | ALT, None)
    assert hm._match(29, CTRL | ALT) == "demo"
    # 修饰键不精确匹配（多按了 shift）→ 不触发
    assert hm._match(29, CTRL | ALT | SHIFT) is None
    # 忽略无关标志位（CapsLock 等）—— 这一步就住在 _match 里，故留在此文件
    assert hm._match(29, CTRL | ALT | 0x10000) == "demo"
    assert hm._match(30, CTRL | ALT) is None


# ── 注册失败要留得下原因（2026-10-03）─────────────────────────────────────────
#
# 启动那批热键是**在没有界面的时刻**注册的：注册失败时用户那边只有「按了没反应」，
# 而这一行日志他不会去看。原因存在注册表里，等他事后翻到「快捷键」页时那一行的
# 副标题会写着为什么没挂上。这两条守的是「原因存得下、也退得掉」。


def _manager(monkeypatch, quartz_ok: bool, qtbot):
    """构建一个**不碰 tap** 的注册表：失败路径在建 tap 之前，够这两条判据用。"""
    from barkit.core import hotkeys as mod

    monkeypatch.setattr(mod, "_QUARTZ_OK", quartz_ok)
    mgr = mod.HotkeyManager()      # qtbot 只为保住 QApplication（Signal 要它）
    return mgr


def test_register_failure_is_recorded_and_readable(qtbot, monkeypatch):
    """注册失败 ⇒ `failure_of` 给得出原因（不是只剩一行日志）。"""
    mgr = _manager(monkeypatch, quartz_ok=False, qtbot=qtbot)
    assert mgr.register("framework.panel", "ctrl+alt+o", lambda: None) is False
    reason = mgr.failure_of("framework.panel")
    assert reason, "失败了却查不到原因 ⇒ 「快捷键」页那一行还是说不出话"
    assert "Quartz" in reason, f"原因要能指到根子上，别只写「失败」：{reason}"
    assert mgr.failures() == {"framework.panel": reason}


def test_failure_drops_when_the_hotkey_goes_away(qtbot, monkeypatch):
    """撤绑 / 全清 ⇒ 原因跟着除名（否则修好之后那一页永远红着一行）。"""
    mgr = _manager(monkeypatch, quartz_ok=False, qtbot=qtbot)
    mgr.register("framework.panel", "ctrl+alt+o", lambda: None)
    assert mgr.failure_of("framework.panel")

    mgr.unregister("framework.panel")
    assert mgr.failure_of("framework.panel") is None, "撤绑了原因还留着 ⇒ 修好也一直报错"

    mgr.register("framework.panel", "ctrl+alt+o", lambda: None)
    mgr.unregister_all()
    assert mgr.failures() == {}, "unregister_all 没把失败账一起清掉"


def test_failure_is_dropped_once_the_hotkey_binds(qtbot, monkeypatch):
    """挂上了就得除名（**成功**那一头）：修好权限之后那一页不能继续红着一行。

    tap 是唯一要真 Quartz 的那一步 ⇒ 把它替掉就行（建表与冲突判定都是 `HotkeyTable`
    那半的纯逻辑），于是「成功」这条路径在离屏下也测得到 —— 否则守它的用例只能靠
    真机有辅助功能权限。
    """
    from barkit.core import hotkeys as mod

    monkeypatch.setattr(mod, "_QUARTZ_OK", True)
    monkeypatch.setattr(mod.HotkeyManager, "_ensure_tap", lambda self: True)
    mgr = mod.HotkeyManager()
    mgr._failures["framework.panel"] = "上一轮没挂上"
    assert mgr.register("framework.panel", "ctrl+alt+o", lambda: None) is True
    assert mgr.failure_of("framework.panel") is None, "挂上了却还记着失败 ⇒ 修好也一直报错"
