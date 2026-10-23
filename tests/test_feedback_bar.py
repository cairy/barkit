"""``FeedbackBar`` 确认位的外观契约：三档、默认落最轻那档、不给第四档。

判据对应 2026-10-16 用户上报「按钮没有颜色区分」—— 询问档那两枚按钮此前都是幽灵档
（透明底 ＋ 1px 灰边框 ＋ 灰字），于是「卸载」和它右边的「取消」长得一模一样。

这里钉的是「**外观真的换到那一枚上**」；「哪一句问话该用哪一档」由
``tests/test_architecture.py::test_destructive_confirm_is_styled_as_danger`` 那条 AST
门禁守（扫全仓 ``.ask(`` 的确认位文案）。
"""
from __future__ import annotations

import pytest

from barkit.shell.ui_kit import FeedbackBar


def _ask(bar: FeedbackBar, **kw) -> FeedbackBar:
    bar.ask("删掉它？", "删除", lambda: None, **kw)
    return bar


def _up(bar: FeedbackBar) -> list[str]:
    """此刻**在屏上**的确认位档名（``isVisibleTo`` 而非 ``isVisible``：夹具不 show 窗体）。"""
    return [k for k, b in bar._yes_btns.items() if b.isVisibleTo(bar)]


def test_confirm_kind_defaults_to_the_lightest_one(qtbot):
    """不传 ``confirm_kind`` ⇒ 与加这个参数之前**逐字同行为**（各调用点的测试因此零改动）。"""
    bar = FeedbackBar()
    qtbot.addWidget(bar)
    _ask(bar)
    assert bar._yes.objectName() == "pill-ghost"
    assert bar._no.objectName() == "pill-ghost"


def test_destructive_confirm_takes_the_solid_red(qtbot):
    """不可逆动作的确认位 = 实心红；**取消位不受影响** —— 它是「我不做这件事」。"""
    bar = FeedbackBar()
    qtbot.addWidget(bar)
    _ask(bar, confirm_kind="danger")
    assert bar._yes.objectName() == "danger"
    assert bar._no.objectName() == "pill-ghost"
    assert bar.confirm_text() == "删除"
    assert bar.cancel_text() == "取消"


def test_plain_confirm_takes_the_solid_accent(qtbot):
    """一般确认（两个都是真动作、都没有破坏性）⇒ 实心品牌紫，取消位照旧幽灵档。"""
    bar = FeedbackBar()
    qtbot.addWidget(bar)
    bar.ask("有还没保存的改动", "保存并关闭", lambda: None,
            cancel_label="放弃改动", on_cancel=lambda: None, confirm_kind="primary")
    assert bar._yes.objectName() == "primary"
    assert bar._no.objectName() == "pill-ghost"
    assert bar.confirm_text() == "保存并关闭"
    assert bar.cancel_text() == "放弃改动"


def test_only_the_current_confirm_button_is_up(qtbot):
    """三枚确认位常驻布局、一次只显一枚 —— 切档换的是「显哪一枚」。

    **不按需改 ``objectName``** 是有理由的：QSS 的匹配结果在控件上屏后被缓存，改完
    不重刷就还是旧外观（``buttons.apply_button_tier`` 那段注释讲的是同一件事）。常驻
    三枚 ＋ 藏起来不占位，把这条从「依赖 Qt 缓存行为」变成「显哪一枚」，无风险。
    """
    bar = FeedbackBar()
    qtbot.addWidget(bar)
    _ask(bar, confirm_kind="danger")
    assert _up(bar) == ["danger"]
    _ask(bar)
    assert _up(bar) == ["ghost"]
    assert bar._yes.objectName() == "pill-ghost"


def test_confirm_kind_survives_a_tone_change(qtbot):
    """问句撤下、切到结果档之后，三枚确认位一枚都不许还留在屏上。

    承重的是「``_say`` 只 hide 了当前那一枚」这个实现细节：换档切走时，另外两枚若
    没在 ``ask`` 里被收起，就会留下两枚孤零零的红/紫按钮挂在结果文案旁边。
    """
    bar = FeedbackBar()
    qtbot.addWidget(bar)
    _ask(bar, confirm_kind="danger")
    bar.info("成了")
    assert _up(bar) == []


def test_unknown_confirm_kind_fails_loud(qtbot):
    """传了没登记的档名**当场报错**，别静默长成第四种外观（与 ``apply_button_tier`` 同口径）。"""
    bar = FeedbackBar()
    qtbot.addWidget(bar)
    with pytest.raises(ValueError, match="未知的确认位外观"):
        bar.ask("删掉它？", "删除", lambda: None, confirm_kind="destructive")
