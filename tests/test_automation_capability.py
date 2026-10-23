"""AutomationCapability 能力基类测试：统一自动化 seam + 状态行徽标 + 协作式 mixin 组合。

验证：
- automation_active / toggle_automation 统一翻转（notify 的 auto_enabled 与 route 的
  is_paused 曾是极性别名，现收敛为一个正交属性）
- _make_guard_badge 返回的徽标初值正确、点击即 toggle_automation
- _refresh_guard_badge 按 automation_active 刷新徽标文本
- 协作式 mixin：插件可用 class Plugin(AutomationCapability, Scheduled, ...) 多重组合且 MRO 干净
- 抽象钩子未实现时访问即报错（早 fail）
"""
from __future__ import annotations

import types

from PySide6.QtWidgets import QApplication

from barkit.capabilities.automation import AutomationCapability
from barkit.core.plugin import Plugin
from barkit.shell.ui_kit import ModeBadge


def _ctx():
    app = QApplication.instance() or QApplication([])
    return app, types.SimpleNamespace(config=None, events=None)


class FakeAutomationCapability(AutomationCapability):
    id = "fake_guard"
    name = "fake"
    order = 0

    def __init__(self, ctx, active=True):
        super().__init__(ctx)
        self._active = active
        self.set_calls = []

    @property
    def automation_active(self):
        return self._active

    def set_automation_active(self, active):
        self.set_calls.append(active)
        self._active = active


class Scheduled(Plugin):
    """模拟「未来能力基类」，仅用于验证协作式 mixin 多重组合。"""

    def __init__(self, ctx):
        super().__init__(ctx)
        self.ticked = 0

    def tick(self):
        self.ticked += 1


class ComboAutomationCapability(FakeAutomationCapability, Scheduled):
    id = "combo"
    name = "combo"


def test_guard_unifies_automation_state_and_toggle():
    app, ctx = _ctx()
    g = FakeAutomationCapability(ctx, active=True)
    assert g.automation_active is True
    g.toggle_automation()
    assert g.set_calls == [False]
    assert g.automation_active is False
    g.toggle_automation()
    assert g.set_calls == [False, True]
    assert g.automation_active is True


def test_guard_badge_wired_to_toggle():
    app, ctx = _ctx()
    g = FakeAutomationCapability(ctx, active=True)
    spec = g.mode_badge_spec()
    assert spec.label == "自动"
    before = list(g.set_calls)
    spec.on_click()  # 模式徽标点击 → toggle_automation
    assert g.set_calls == before + [False]
    assert g.automation_active is False
    # 框架经 ModeBadge.get_state 自同步；此处校验切回后 spec 派生正确
    assert g.mode_badge_spec().label == "手动"


def test_guard_refresh_badge_mode():
    app, ctx = _ctx()
    g = FakeAutomationCapability(ctx, active=False)
    assert g.mode_badge_spec().label == "手动"
    g._active = True  # 状态翻转后刷新
    assert g.mode_badge_spec().label == "自动"


def test_guard_badge_recolors_on_mode_switch():
    """回归：set_state 后颜色必须随模式变化，不能只改文字（否则「自动」显示灰底灰字）。"""
    app, ctx = _ctx()
    badge = ModeBadge(label="手动", accent=False)  # neutral → surface2 底
    grey_bg, grey_fg = badge._bg, badge._fg
    assert badge.text() == "手动"
    badge.set_state("自动", True)  # 切到 accent
    assert badge.text() == "自动"
    assert badge._bg != grey_bg, "切到自动后背景色未从 neutral 变为 accent 浅底"
    assert badge._fg != grey_fg, "切到自动后文字色未从 neutral 变为 accent"
    # 再切回手动，颜色也应跟随回到 neutral
    badge.set_state("手动", False)
    assert badge.text() == "手动"
    assert badge._bg == grey_bg
    assert badge._fg == grey_fg



def test_mode_badge_emits_clicked_and_stays_clickable(qtbot):
    """H3：徽标只 emit ``clicked``，不持有回调 —— 「点了做什么」归接线方。

    声明了动作 ⇒ clickable ⇒ 手型光标 + 左键释放 emit 一次 ``clicked``。
    （此前它自己调 ``on_click``、信号从不 emit，面板里的处理函数因此是死代码。）
    """
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    badge = ModeBadge(label="自动", accent=True, clickable=True)
    qtbot.addWidget(badge)
    badge.show()
    seen: list[int] = []
    badge.clicked.connect(lambda: seen.append(1))
    assert badge.cursor().shape() == Qt.CursorShape.PointingHandCursor

    QTest.mouseRelease(badge, Qt.MouseButton.LeftButton)
    assert seen == [1], f"可点徽标的左键释放没 emit clicked：{seen}"


def test_mode_badge_not_clickable_emits_nothing(qtbot):
    """没声明任何动作 ⇒ 箭头光标、点了不 emit（免「看着能点、实则没反应」）。"""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    badge = ModeBadge(label="只读", accent=True)
    qtbot.addWidget(badge)
    badge.show()
    seen: list[int] = []
    badge.clicked.connect(lambda: seen.append(1))
    assert badge.cursor().shape() == Qt.CursorShape.ArrowCursor

    QTest.mouseRelease(badge, Qt.MouseButton.LeftButton)
    assert seen == [], f"不可点徽标也 emit 了 clicked：{seen}"


def test_guard_mixin_composition_mro():
    app, ctx = _ctx()
    c = ComboAutomationCapability(ctx, active=True)
    # 协作式 init：两个 mixin 的 __init__ 都执行到
    assert c.ctx is ctx
    assert c.ticked == 0
    c.tick()
    assert c.ticked == 1
    # 自动化 seam 在组合后仍可用
    assert c.automation_active is True
    c.toggle_automation()
    assert c.automation_active is False


def test_guard_abstract_hook_not_implemented():
    app, ctx = _ctx()

    class Bare(AutomationCapability):
        pass

    b = Bare(ctx)
    try:
        _ = b.automation_active
        raised = False
    except NotImplementedError:
        raised = True
    assert raised, "未实现 automation_active 应早 fail"
