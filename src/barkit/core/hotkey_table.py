"""core/hotkey_table：全局热键的注册表（名字 → 键位 ＋ 回调）。

**为什么单独一层**：热键有两件事 —— ① 「谁占了哪个键位、按下该叫谁」是**纯策略**；
② 「怎么收到按键」是 macOS 的 CGEventTap 实现。两者原先一起长在 `core/hotkeys.py`，而
那个模块**顶层**就 `import objc` / `import Quartz` ⇒ 纯策略在非 macOS 上一行都跑不到。
偏偏「两条热键配成同一个键位」这种缺陷的表现是**「有时不灵」**（后登记的那条被静默顶掉，
而谁生效取决于插件发现顺序），恰是最该被离线守门、也最难靠肉眼发现的一类。把表拆出来，
策略在任何平台都能被完整证明（`tests/test_hotkey_table.py`），`core/hotkeys.py` 只留监听那半。
**这不是跨平台兼容层**：`core/hotkeys.py` 的 macOS 顶层 import 与实现原样不动，拆的只是
「哪一半需要 Quartz」这条线。

**两条「不再静默」的规则**（旧实现都是直接 `dict[name] = …` 覆盖）：

1. **键位被别的热键占了 ⇒ 拒绝登记**。依据 `内部路线图`（U2 ③「全局热键注册表
   在此统一去重」，验收写的是「热键重复被中心化拒绝」）。该改哪条键位由用户决定，注册表
   只负责把冲突说清楚，措辞在 :meth:`Conflict.describe`。
2. **同一个名字被另一个回调占用 ⇒ 拒绝登记**。同名重复登记本身**合法** —— 插件停用再启用
   会走到这里，那时占位的正是它自己，只是换个键位。判据是**回调相等**（``==``：绑定方法按
   ``__self__`` ＋ ``__func__`` 比），**不能用 `is`** —— 贡献重收集会产生新的绑定方法对象，
   `is` 会把合法重登记误判成冲突。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:                       # 只为类型：本模块的立身之本是「不牵重依赖」
    from barkit.contribution.specs import ActionResult

# 热键回调：无参，**可**返回 `ActionResult`（也**可以不返回** —— 不返回 ＝ 这次没话要说）。
# 返回值由宿主消费（`shell/hotkey_feedback.wrap_hotkey_callback`），本表只存不调；
# 2026-09-28 随 `HotkeySpec.callback` 一起扩宽（在那之前是 `Callable[[], None]`）。
HotkeyCallback = Callable[[], "ActionResult | None"]


@dataclass(frozen=True)
class Binding:
    """一条已登记的热键。``keycode`` / ``mask`` 对本报是不透明整数，由调用方解析。"""

    name: str
    keycode: int
    mask: int
    callback: HotkeyCallback | None


@dataclass(frozen=True)
class Conflict:
    """一次被拒绝的登记。``kind`` 决定该改哪儿 —— 改键位还是改声明。"""

    kind: str   # "combo"：键位已被别的热键占了；"name"：同名已被别的回调占了
    owner: str  # kind="combo" ⇒ 占了该键位的那条热键名；kind="name" ⇒ 该名字本身

    def describe(self, name: str, combo: str = "") -> str:
        """写成一行日志：谁跟谁撞了、该改哪个键。

        ``combo`` 可省（注册表内部的自查守卫拿不到键位字符串）—— 省了就只说热键名，
        不拼一个空括号出来。
        """
        head = f"热键 {name}（{combo}）" if combo else f"热键 {name}"
        if self.kind == "name":
            return (
                f"{head}登记被拒：热键名 {self.owner!r} 已被另一个组件的回调占用 —— 两个"
                f"插件声明了同一个 HotkeySpec.name，而它同时是配置键 hotkeys.{self.owner}。"
                f"改掉其中一个的 name（发布后冻结，等于换一个热键）。"
            )
        return (
            f"{head}登记被拒：键位已被热键 {self.owner!r} 占用 —— 两条热键配成了同一个键位，"
            f"今天谁生效取决于插件发现顺序（后登记的被静默顶掉），故直接拒绝。"
            f"改 hotkeys.{self.owner} 或 hotkeys.{name} 的键位后重启。"
        )


class HotkeyTable:
    """名字 → 键位／回调。**两种重复都拒绝，不静默覆盖。**"""

    def __init__(self) -> None:
        self._by_name: dict[str, Binding] = {}

    def __len__(self) -> int:
        return len(self._by_name)

    def __bool__(self) -> bool:
        return bool(self._by_name)

    # ---------- 查询 ----------

    def get(self, name: str) -> Binding | None:
        return self._by_name.get(name)

    def bindings(self) -> tuple[Binding, ...]:
        """按登记顺序返回全部条目（面板诊断 / 用例断言用）。"""
        return tuple(self._by_name.values())

    def match(self, keycode: int, mask: int) -> Binding | None:
        """按 keycode ＋ 修饰键掩码精确匹配。掩码里的无关位（CapsLock 等）由调用方裁掉。

        登记期已禁止两条热键共用一个键位 ⇒ 命中的最多一条，「返回首个」不再是单选。
        """
        for binding in self._by_name.values():
            if binding.keycode == keycode and binding.mask == mask:
                return binding
        return None

    def conflict(self, name: str, keycode: int, mask: int,
                 callback: HotkeyCallback | None) -> Conflict | None:
        """这次登记会不会撞上已在册的某条。

        **先问后写**：调用方据此在碰系统（建 tap）之前就把冲突拒掉，不给「tap 已建好、
        却一条热键都没上去」留中间态。
        """
        for binding in self._by_name.values():
            if binding.name != name and binding.keycode == keycode and binding.mask == mask:
                return Conflict("combo", binding.name)
        existing = self._by_name.get(name)
        if existing is not None and existing.callback != callback:
            return Conflict("name", name)
        return None

    # ---------- 变更 ----------

    def bind(self, name: str, keycode: int, mask: int,
             callback: HotkeyCallback | None) -> None:
        """登记。同名同回调＝换键位，合法。冲突时抛 ``ValueError``。

        正常路径应先 :meth:`conflict` 问一次；这里的抛出是防「绕过询问直接写」——
        那正是旧实现把冲突变成静默覆盖的那一步。
        """
        found = self.conflict(name, keycode, mask, callback)
        if found is not None:
            raise ValueError(found.describe(name))
        self._by_name[name] = Binding(name, keycode, mask, callback)

    def unbind(self, name: str) -> None:
        self._by_name.pop(name, None)

    def clear(self) -> None:
        self._by_name.clear()
