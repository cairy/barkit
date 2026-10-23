"""core/hotkey_table 测试（P0 ⑤：热键重复不再静默覆盖）。

**这个文件在任何平台都能跑**，正是拆出 `core/hotkey_table` 的目的：旧实现把去重策略和
CGEventTap 监听混在 `core/hotkeys.py` 里，而那个模块顶层 `import objc` ⇒ 「两条热键配成
同一个键位」这种缺陷在 macOS 之外**一行都测不到**。现在策略在这里被完整证明，需要的
只有「键位对是不透明整数」这一条约定。
"""

import ast
from pathlib import Path

import pytest

from barkit.core.hotkey_table import Conflict, HotkeyTable

SRC = Path(__file__).resolve().parents[1] / "src"
PLUGINS = SRC / "barkit" / "plugins"

CTRL = 0x40000
ALT = 0x80000
O_KEY = 31
M_KEY = 46


class _Owner:
    """假的宿主/插件对象：用它两个**不同的绑定方法**当回调，验「同名换回调」的判据。"""

    def __init__(self, tag: str) -> None:
        self.tag = tag

    def on_a(self) -> None:  # pragma: no cover —— 只当句柄用，从不调用
        pass

    def on_b(self) -> None:  # pragma: no cover
        pass


# ---------- ① 键位冲突：拒绝，不静默顶掉 ----------

def test_second_hotkey_on_the_same_combo_is_refused():
    table = HotkeyTable()
    assert table.conflict("recognize", O_KEY, CTRL | ALT, None) is None
    table.bind("recognize", O_KEY, CTRL | ALT, None)

    found = table.conflict("autotype", O_KEY, CTRL | ALT, None)
    assert found == Conflict("combo", "recognize")
    # 拒绝之后表里还只有先登记者那一条 ——「后登记者顶掉先登记」不再发生
    assert [b.name for b in table.bindings()] == ["recognize"]
    assert table.match(O_KEY, CTRL | ALT).name == "recognize"


def test_different_combos_do_not_conflict():
    table = HotkeyTable()
    table.bind("recognize", O_KEY, CTRL | ALT, None)
    assert table.conflict("notify_toggle", M_KEY, CTRL | ALT, None) is None
    table.bind("notify_toggle", M_KEY, CTRL | ALT, None)
    assert len(table) == 2


def test_same_key_with_different_modifiers_is_not_a_conflict():
    """`ctrl+alt+o` 与 `ctrl+o` 是两个键位 —— 修饰键参与判等。"""
    table = HotkeyTable()
    table.bind("a", O_KEY, CTRL | ALT, None)
    assert table.conflict("b", O_KEY, CTRL, None) is None


def test_released_combo_can_be_taken_again():
    """禁用插件会腾出键位；随后别的热键能登记上来（否则「禁用再启用」换不回原键位）。"""
    table = HotkeyTable()
    table.bind("first", O_KEY, CTRL | ALT, None)
    table.unbind("first")
    assert table.conflict("second", O_KEY, CTRL | ALT, None) is None


# ---------- ② 同名：同回调＝换键位（合法），异回调＝拒绝 ----------

def test_same_name_same_callback_is_a_legal_rebinding():
    table = HotkeyTable()
    owner = _Owner("x")
    table.bind("h", O_KEY, CTRL | ALT, owner.on_a)
    assert table.conflict("h", M_KEY, CTRL, owner.on_a) is None
    table.bind("h", M_KEY, CTRL, owner.on_a)
    assert len(table) == 1
    assert table.match(M_KEY, CTRL).name == "h"
    assert table.match(O_KEY, CTRL | ALT) is None      # 旧键位已腾出


def test_callback_comparison_must_not_use_identity():
    """**回归锚**：绑定方法每次都新建对象，`is` 比会把合法重登记误判成冲突。

    这条同时解释了判据为什么写成 `==`：若哪天有人「优化」成 `is`，
    「插件禁用再启用后热键注册不上」会立刻变成一条极难查的缺陷。
    """
    owner = _Owner("x")
    assert owner.on_a is not owner.on_a     # 新对象
    assert owner.on_a == owner.on_a         # 但相等（同 __self__ ＋ __func__）

    table = HotkeyTable()
    table.bind("h", O_KEY, CTRL | ALT, owner.on_a)
    assert table.conflict("h", O_KEY, CTRL | ALT, owner.on_a) is None


def test_same_name_with_another_callback_is_refused():
    """两个插件声明了同一个 `HotkeySpec.name`（它同时是配置键 `hotkeys.<name>`）。"""
    table = HotkeyTable()
    table.bind("notify_toggle", M_KEY, CTRL | ALT, _Owner("notify_guard").on_a)

    found = table.conflict("notify_toggle", O_KEY, CTRL, _Owner("other").on_a)
    assert found == Conflict("name", "notify_toggle")
    # 先登记者的键位与回调原样保留
    assert table.get("notify_toggle").keycode == M_KEY


# ---------- ③ bind 的守卫：不许绕过询问直接写 ----------

def test_bind_raises_instead_of_silently_overwriting():
    table = HotkeyTable()
    table.bind("recognize", O_KEY, CTRL | ALT, None)
    with pytest.raises(ValueError) as caught:
        table.bind("autotype", O_KEY, CTRL | ALT, None)
    assert "recognize" in str(caught.value)
    assert len(table) == 1


# ---------- ④ 匹配与容器行为 ----------

def test_match_is_exact_and_unique():
    table = HotkeyTable()
    table.bind("demo", O_KEY, CTRL | ALT, None)
    assert table.match(O_KEY, CTRL | ALT).name == "demo"
    assert table.match(O_KEY, CTRL | ALT | 0x20000) is None   # 修饰键不精确 ⇒ 不命中
    assert table.match(O_KEY + 1, CTRL | ALT) is None
    assert table.match(O_KEY, CTRL) is None


def test_empty_table_is_falsy():
    table = HotkeyTable()
    assert not table            # `unregister` 靠这个判「全撤了 ⇒ 停 tap」
    table.bind("a", O_KEY, CTRL | ALT, None)
    assert table
    table.clear()
    assert not table
    assert table.match(O_KEY, CTRL | ALT) is None


def test_get_and_unbind_are_forgiving():
    table = HotkeyTable()
    assert table.get("nope") is None
    table.unbind("nope")        # 撤不存在的名字不抛（disable 可能撤两次）
    assert len(table) == 0


# ---------- ⑤ 措辞：日志要能让用户照着改 ----------

def test_describe_names_both_sides_of_a_combo_conflict():
    text = Conflict("combo", "recognize").describe("autotype", "ctrl+alt+o")
    assert "autotype" in text and "recognize" in text
    assert "hotkeys.recognize" in text and "hotkeys.autotype" in text


def test_describe_names_the_colliding_name_for_a_name_conflict():
    text = Conflict("name", "notify_toggle").describe("notify_toggle", "ctrl+alt+m")
    assert "HotkeySpec.name" in text and "hotkeys.notify_toggle" in text


def test_describe_without_a_combo_does_not_print_empty_parentheses():
    """`bind` 的自查守卫拿不到键位字符串，省掉 `combo` 时不该拼出一个空括号。"""
    text = Conflict("combo", "recognize").describe("autotype")
    assert "（）" not in text and "()" not in text
    assert "autotype" in text


# ---------- ⑥ 门禁：出厂的默认键位不许互撞 ----------

def _declared_default_combos() -> dict[str, list[str]]:
    """扫 `plugins/**/*.py`，取所有 `HotkeySpec(default_combo="…")` 的非空字面量。

    用 AST 取关键字实参，不用字符串匹配 —— `default_combo` 这个名字也出现在文档与注释里
    （例如 `Plugin.hotkeys` 的 docstring），子串匹配会抓回来一堆假条目。
    """
    found: dict[str, list[str]] = {}
    for path in sorted(PLUGINS.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "default_combo" or not isinstance(kw.value, ast.Constant):
                    continue
                combo = kw.value.value
                if isinstance(combo, str) and combo:
                    rel = path.relative_to(SRC).as_posix()
                    found.setdefault(combo, []).append(f"{rel}:{node.lineno}")
    return found


def test_no_two_shipped_plugins_share_a_default_combo():
    """出厂默认键位互不重复 —— 重复了，用户开箱就有一条热键「有时不灵」。

    （空串不算：那是「不声明默认键位」，调用方本来就会跳过。）
    """
    duplicated = {combo: where for combo, where in _declared_default_combos().items()
                  if len(where) > 1}
    assert not duplicated, f"出厂默认键位撞车：{duplicated}"


def test_the_default_combo_scan_actually_sees_the_shipped_plugins():
    """**自证**：扫描面不能是空的 —— glob 写错（扫了个空目录）时上面那条会永远绿。"""
    combos = _declared_default_combos()
    assert len(combos) >= 3, f"只扫到 {combos}，扫描面疑似失效"
