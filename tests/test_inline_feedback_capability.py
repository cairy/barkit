"""InlineFeedbackCapability 能力基类测试：页首反馈槽接线 + 三档出口 + 协作式 mixin 组合。

验证：
- ``_say_info`` / ``_say_hint`` / ``_say_error`` 各写进槽的对应档；没有槽位时只记日志、不崩
- ``attach_feedback`` 接线：槽 destroyed 后摘引用（不戳已删对象），迟到的销毁信号不误清新槽
- 协作式 mixin：与另一个能力多重组合后 MRO 干净、两个能力的初始化都执行到
- 接口：能力自己继承 ``core.Plugin`` ⇒ 子类基类括号里不再并列写那个基类
"""
from __future__ import annotations

import types

from barkit.capabilities.helper_ops import HelperOpsCapability
from barkit.capabilities.inline_feedback import InlineFeedbackCapability
from barkit.core.plugin import Plugin


class _Signal:
    """最小信号桩：只支持 connect / emit。"""

    def __init__(self) -> None:
        self._slots: list = []

    def connect(self, fn) -> None:
        self._slots.append(fn)

    def emit(self, *args) -> None:
        for fn in list(self._slots):
            fn(*args)


class FakeFeedbackBar:
    """最小反馈槽桩：只记「哪一档、什么话」，``destroyed`` 可手动 emit。"""

    def __init__(self) -> None:
        self.destroyed = _Signal()
        self.calls: list = []

    def info(self, text: str) -> None:
        self.calls.append(("info", text))

    def hint(self, text: str) -> None:
        self.calls.append(("hint", text))

    def error(self, text: str) -> None:
        self.calls.append(("error", text))


def _ctx():
    return types.SimpleNamespace(config=None, events=None)


class FakeFeedbackPlugin(InlineFeedbackCapability):
    id = "fake_feedback"
    name = "fake"


class MixedPlugin(InlineFeedbackCapability, HelperOpsCapability):
    """多重组合：两个能力 + ``core.Plugin`` 链。"""

    id = "mixed"
    name = "mixed"

    def helper_notice(self):
        return ("", False)

    def on_helper_change(self) -> None:
        pass


def test_capability_is_a_plugin():
    """能力自己继承 ``core.Plugin`` ⇒ 子类基类括号里不再并列写那个基类。"""
    assert issubclass(InlineFeedbackCapability, Plugin)


def test_three_outlets_write_to_matching_tone():
    p = FakeFeedbackPlugin(_ctx())
    bar = FakeFeedbackBar()
    p.attach_feedback(bar)

    p._say_info("成了")
    p._say_hint("先去做点别的")
    p._say_error("没成")

    assert bar.calls == [("info", "成了"), ("hint", "先去做点别的"), ("error", "没成")]


def test_outlets_without_slot_only_log(caplog):
    """没有槽位（面板 / 托盘路径）不许崩，日志是唯一线索。"""
    p = FakeFeedbackPlugin(_ctx())
    with caplog.at_level("INFO"):
        p._say_info("只有日志这一条路")
        p._say_error("错了也只在日志里")
    text = "\n".join(r.message for r in caplog.records)
    assert "只有日志这一条路" in text
    assert "错了也只在日志里" in text


def test_slot_destroyed_drops_reference_but_keeps_newer_one():
    p = FakeFeedbackPlugin(_ctx())
    old = FakeFeedbackBar()
    p.attach_feedback(old)
    old.destroyed.emit()
    assert p._feedback is None

    new = FakeFeedbackBar()
    p.attach_feedback(new)
    old.destroyed.emit()          # 老槽迟到的销毁信号：不许把新槽一起清掉
    assert p._feedback is new

    p._say_error("还在")
    assert new.calls == [("error", "还在")]


def test_mixin_composition_mro():
    p = MixedPlugin(_ctx())
    # 协作式 init：链上每个能力的 __init__ 都执行到
    assert p._feedback is None
    assert p._helper_feedback is None
    assert isinstance(p, InlineFeedbackCapability)
    assert isinstance(p, HelperOpsCapability)
    assert isinstance(p, Plugin)

    bar = FakeFeedbackBar()
    p.attach_feedback(bar)
    p._say_info("组合后仍可用")
    assert bar.calls == [("info", "组合后仍可用")]
