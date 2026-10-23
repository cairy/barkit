"""F.2/F.3 UI 测试：面板自启行 + 熔断故障区（重试按钮）。"""

import sys
import time

import pytest
from unittest.mock import patch

from PySide6.QtCore import QPropertyAnimation, Qt
from PySide6.QtWidgets import QWidget

from barkit.shell.panel import QWIDGETSIZE_MAX

from barkit.contribution.specs import (
    ActionSpec,
    ComponentSpec,
    IconName,
    StateLevel,
    StateOption,
    StateReport,
    StateSpec,
    StatusRowSpec,
)
from barkit.core import autostart
from barkit.core.events import EventBus
from barkit.core.scheduler import Scheduler
from barkit.shell.autostart_row import AutostartRow
from barkit.shell.design import metrics
from barkit.shell.panel import StatusPanel, _TitleBarIcon
from barkit.shell.ui_kit import FeedbackBar


class _StubContrib:
    """最小贡献桩：仅提供 set_components 需要的 states()（标题栏图标条不消费快照）。"""

    def __init__(self, states=None):
        self._states = states or []

    def states(self):
        return self._states


def _wait_fold_animation(qtbot, panel, timeout=3000):
    """等「折叠 / 展开」那条 200ms 高度动画**跑完**，而不是等一个「够久」的时长。

    回归（折叠动画由 waitUntil 等条件替代固定时长，见 ``_wait_fold_animation``）：此前各用例统一 ``qtbot.wait(320)``，机器一忙动画就
    来不及走完 ⇒ 同一份代码连跑 5 次 2 红 3 绿。等**条件**（动画不再 Running）而
    不是等**时长**：既去掉抖动，还更快 —— 动画一停就继续，不必每次白等那 120ms 余量。
    """
    qtbot.waitUntil(
        lambda: panel._anim is None
        or panel._anim.state() != QPropertyAnimation.Running,
        timeout=timeout,
    )


def test_autostart_row_not_installed(qtbot):
    with patch.object(autostart, "status", return_value={
        "installed": False, "running": False, "stale": False,
    }):
        row = AutostartRow()
        qtbot.addWidget(row)
    assert "未安装" in row._status.text()
    assert row._install_btn.isVisibleTo(row) and not row._uninstall_btn.isVisibleTo(row)


def test_autostart_row_stale_prompts_reinstall(qtbot):
    with patch.object(autostart, "status", return_value={
        "installed": True, "running": True, "stale": True,
    }):
        row = AutostartRow()
        qtbot.addWidget(row)
    assert "重新安装" in row._status.text()
    assert row._install_btn.isVisibleTo(row)


# ---------- 自启行：结果落行内槽，关闭先确认（2026-09-17 零模态收口） ----------
#
# 改前这五处全是 ``QMessageBox``（父窗口 ``None``）；设置窗钉在原生层级 201，从它
# 内部弹模态框会另开一条嵌套事件循环（证据见 tests/test_architecture.py 的零模态门禁）。


def test_autostart_row_install_failure_lands_in_feedback_bar(qtbot):
    """安装失败说到行内槽里（原来是一条 ``QMessageBox.warning``）。"""
    fb = FeedbackBar()
    qtbot.addWidget(fb)
    with patch.object(autostart, "status", return_value={
        "installed": False, "running": False, "stale": False,
    }):
        row = AutostartRow(feedback=fb)
        qtbot.addWidget(row)
    with patch.object(autostart, "install", return_value=(False, "launchctl 报错")):
        row._install()
    assert "安装自启失败" in fb.text()
    assert "launchctl 报错" in fb.text()


def test_autostart_row_reload_hint_lands_in_feedback_bar(qtbot):
    """venv 重建后的「正在重载、即将重启」也要说到（原来是 ``information`` 框）。"""
    fb = FeedbackBar()
    qtbot.addWidget(fb)
    with patch.object(autostart, "status", return_value={
        "installed": True, "running": True, "stale": True,
    }):
        row = AutostartRow(feedback=fb)
        qtbot.addWidget(row)
    with patch.object(autostart, "install", return_value=(True, "reload")):
        row._install()
    assert "随即重启" in fb.text()


def test_autostart_row_uninstall_asks_then_runs(qtbot):
    """关闭自启是破坏性动作：先挂起确认位，**取消不动、确认才动**。"""
    fb = FeedbackBar()
    qtbot.addWidget(fb)
    status = {"installed": True, "running": False, "stale": False}
    with patch.object(autostart, "status", return_value=status):
        row = AutostartRow(feedback=fb)
        qtbot.addWidget(row)
        calls: list[int] = []

        def _fake_uninstall():
            calls.append(1)
            return (True, "")

        with patch.object(autostart, "uninstall", _fake_uninstall):
            row._uninstall()
            assert fb.is_pending() and calls == []     # 未确认前不得动
            fb.dismiss()                               # 取消
            assert calls == []
            row._uninstall()                           # 再来一次，这回确认
            fb.accept()
            assert calls == [1]                        # 确认后才真干


def test_autostart_row_without_slot_refuses_uninstall(qtbot):
    """拿不到确认位 ⇒ **拒绝执行**。

    宁可用「点了没反应 ＋ 一条 WARNING」（面板/无页路径），也不允许无确认地摘掉
    LaunchAgent —— 副作用是那条路径上用户看不出发生了什么，属有意取舍。
    """
    with patch.object(autostart, "status", return_value={
        "installed": True, "running": False, "stale": False,
    }):
        row = AutostartRow()                       # 刻意不带槽
        qtbot.addWidget(row)
    calls: list[int] = []
    with patch.object(
        autostart, "uninstall",
        side_effect=lambda: (calls.append(1), (True, ""))[1],
    ):
        row._uninstall()
    assert calls == []


def test_panel_broken_row_shows_and_retry(qtbot):
    events = EventBus()
    sched = Scheduler(events)
    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.set_scheduler(sched)

    sched.broken.emit("notify_guard", "backend 写失败", 5)
    assert panel._broken_row.isVisibleTo(panel)
    assert "notify_guard" in panel._broken_label.text()
    assert "backend 写失败" in panel._broken_label.text()

    # 点“重试”→ 清零计数并恢复
    panel._retry_btn.click()
    assert panel._scheduler.status("notify_guard")["fail_count"] == 0
    assert not panel._broken_row.isVisibleTo(panel)


def test_panel_broken_row_multiple_plugins(qtbot):
    events = EventBus()
    sched = Scheduler(events)
    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.set_scheduler(sched)

    sched.broken.emit("plug_a", "a 挂了", 5)
    sched.broken.emit("plug_b", "b 挂了", 6)
    assert "plug_b" in panel._broken_label.text()   # 显示最近一个
    # 重试全部（含已恢复的）：逐条 recovered 后全清
    panel._retry_btn.click()
    assert not panel._broken_row.isVisibleTo(panel)


def test_panel_fold_toggles_container_height(qtbot):
    """折叠↔展开：内容容器高度应随之增减，并能回到原高度。

    ⚠️ 前提校正：面板**默认折叠**（``_folded = True``），``toggle_at`` 首次显示即为
    折叠态，所以第一次 ``toggle_fold`` 是「展开」（变高）、第二次才是「收起」。
    旧版把首显误写成「展开态」，断言方向整个反了（拿折叠高当展开高、断言收起后
    更小），被 xfail 掩盖多年未暴露。这里按真实方向重写。
    """
    from barkit.shell.ui_kit import HoverRow

    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.set_rows([HoverRow(lambda: None), HoverRow(lambda: None), HoverRow(lambda: None)])
    panel.toggle_at(None)        # 首显 = 折叠态
    qtbot.wait(0)
    folded = panel._container.height()
    assert panel._status_section.maximumHeight() == 0

    panel.toggle_fold()          # 展开：200ms 高度动画
    _wait_fold_animation(qtbot, panel)
    expanded = panel._container.height()
    assert expanded > folded
    assert panel._status_section.maximumHeight() == QWIDGETSIZE_MAX   # 上界已解除

    panel.toggle_fold()          # 收起
    _wait_fold_animation(qtbot, panel)
    assert panel._container.height() == folded
    assert panel._status_section.maximumHeight() == 0


def test_logo_click_expands_panel(qtbot):
    """U2 §10.3：点击品牌 logo 展开状态区（替代旧折叠箭头）。

    首显为折叠态（状态区最大高度 0）；点击 logo 后 ``toggle_fold`` 触发 200ms
    高度动画，展开后状态区最大高度解除、``_folded`` 置 False。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from barkit.shell.panel import _ClickableLogo
    from barkit.shell.ui_kit import HoverRow

    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.set_components([], _StubContrib())   # 空状态：logo 作为唯一折叠热区显示
    panel.set_rows([HoverRow(lambda: None), HoverRow(lambda: None), HoverRow(lambda: None)])
    panel.toggle_at(None)            # 首显 = 折叠态
    qtbot.wait(0)
    assert panel._folded
    assert panel._status_section.maximumHeight() == 0

    logo = panel.findChild(_ClickableLogo)
    assert logo is not None
    assert logo.isVisible()          # 空状态 logo 可见
    QTest.mouseClick(logo, Qt.MouseButton.LeftButton)
    _wait_fold_animation(qtbot, panel)

    assert not panel._folded
    assert panel._status_section.maximumHeight() == QWIDGETSIZE_MAX


def test_logo_click_twice_collapses(qtbot):
    """U2 §10.3：再次点击 logo 收起状态区，回到初始折叠高度。"""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from barkit.shell.panel import _ClickableLogo
    from barkit.shell.ui_kit import HoverRow

    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.set_components([], _StubContrib())   # 空状态：logo 作为唯一折叠热区显示
    panel.set_rows([HoverRow(lambda: None), HoverRow(lambda: None)])
    panel.toggle_at(None)
    qtbot.wait(0)
    folded = panel._container.height()

    logo = panel.findChild(_ClickableLogo)
    assert logo.isVisible()          # 空状态 logo 可见
    QTest.mouseClick(logo, Qt.MouseButton.LeftButton)   # 展开
    _wait_fold_animation(qtbot, panel)
    assert panel._container.height() > folded

    QTest.mouseClick(logo, Qt.MouseButton.LeftButton)   # 收起
    _wait_fold_animation(qtbot, panel)
    assert panel._container.height() == folded
    assert panel._status_section.maximumHeight() == 0


def test_logo_hidden_when_components_present(qtbot):
    """边界：有插件时标题栏只显示插件图标，品牌 logo 隐藏（用户规划：不显示 app 图标）。

    折叠改由插件图标承担（见 ``test_icon_click_expands_panel``）；logo 仅在「无组件」
    的空状态才显示当折叠热区。这里钉死「有组件即隐藏」，防止回归把 app 图标又放回来。
    """
    from PySide6.QtTest import QTest

    from barkit.shell.panel import _ClickableLogo, _TitleBarIcon

    panel = StatusPanel()
    qtbot.addWidget(panel)
    comp = ComponentSpec(plugin_id="plug_a", key="main", name="插件 A", icon=IconName.GEAR)
    panel.set_components([comp], _StubContrib())
    panel.toggle_at(None)
    qtbot.wait(0)

    logo = panel.findChild(_ClickableLogo)
    assert logo is not None
    assert not logo.isVisible()              # 有组件 → logo 隐藏

    # 插件图标条应显示且可见，折叠由它承担
    icons = panel.findChildren(_TitleBarIcon)
    assert icons
    assert all(ic.isVisible() for ic in icons)
    QTest.mouseClick(icons[0], Qt.MouseButton.LeftButton)
    _wait_fold_animation(qtbot, panel)
    assert not panel._folded                  # 点插件图标展开


def test_status_item_click_while_visible_collapses(qtbot):
    """U2 §10.3：面板可见时再次点状态栏图标（或点面板外）→ 收起隐藏。

    ``on_status_item_click`` 在可见时走 ``toggle_at`` 的隐藏分支，等价于
    「点图标外 / 再次点图标」的收起路径（与 PanelDismissController 点击外关闭一致）。
    """
    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.toggle_at(None)            # 显示
    qtbot.wait(0)
    assert panel.isVisible()

    panel.on_status_item_click(None)   # 再次点击 → 收起
    qtbot.wait(0)
    assert not panel.isVisible()
    assert panel._folded


def test_panel_status_cap_released_after_expand(qtbot):
    """展开动画结束后必须解除状态区高度上界，否则后续内容增长被裁。

    回归（BUG B）：折叠动画把 ``maximumHeight`` 停在终点值后**没人复位**；展开之后
    再增长的内容（熔断红条出现 / 运行期启用插件多一行 / 某行副标题由空变有）被钳
    在动画终点高度，多出来的部分直接被切掉。

    断言取「状态区自然高度 ≤ 上界」这一等价形式：内容放得下 = 不会被裁。
    """
    from barkit.shell.ui_kit import HoverRow

    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.set_rows([HoverRow(lambda: None), HoverRow(lambda: None)])
    panel.toggle_at(None)          # 面板默认折叠显示
    qtbot.wait(0)
    assert panel._folded
    panel.toggle_fold()            # 展开（200ms 动画）—— 原来就是这一步把上界钉死
    _wait_fold_animation(qtbot, panel)

    assert not panel._folded
    assert panel._status_section.maximumHeight() == QWIDGETSIZE_MAX


def test_panel_content_growth_not_clipped_when_expanded(qtbot):
    """展开态下内容变高（熔断条出现）必须放得下 —— 上界不能小于自然高度。"""
    from barkit.shell.ui_kit import HoverRow

    events = EventBus()
    sched = Scheduler(events)
    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.set_scheduler(sched)
    panel.set_rows([HoverRow(lambda: None)])
    panel.toggle_at(None)
    qtbot.wait(0)
    panel.toggle_fold()            # 展开（面板默认折叠）
    _wait_fold_animation(qtbot, panel)

    before = panel._status_section.sizeHint().height()
    sched.broken.emit("notify_guard", "backend 写失败", 5)   # 熔断条出现 → 内容变高
    after = panel._status_section.sizeHint().height()
    assert after > before, "熔断条未让状态区变高，用例前提不成立"
    assert panel._status_section.maximumHeight() >= after, (
        f"展开态高度上界 {panel._status_section.maximumHeight()} < 内容自然高度 "
        f"{after}：内容会被裁掉"
    )


def test_panel_scrolls_when_status_rows_overflow_the_screen(qtbot):
    """状态行总高超过屏幕时，多出来的行必须**滚得到**（§5 #28）。

    改前面板按内容 ``adjustSize`` 长高、``_position_for_height`` 只把窗口 clamp 进
    屏幕 ⇒ 内容真超过屏幕时，底部那些行既没有滚动条、也没有别的路径够得着（今天
    6 个组件够得放，故不显形；6→8 个组件就显形了）。本用例把行数加过一块屏，核三件：
    ① 窗口不再顶穿屏幕；② 确实出了滚动条（内容放不下）；③ 滚到底时末行完整可见。
    """
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QGuiApplication

    from barkit.shell.ui_kit import HoverRow

    panel = StatusPanel()
    qtbot.addWidget(panel)
    rows = [HoverRow(lambda: None) for _ in range(40)]
    # HoverRow 是自绘行、没有 sizeHint（实测 (-1,-1)），高度由布局摊；给它一个确定
    # 高度，让「内容总高 vs 可视区」的算术可预测（本用例要断言的就是这条算术）。
    for w in rows:
        w.setFixedHeight(40)
    panel.set_rows(rows)
    panel.toggle_at(None)          # 面板默认折叠显示
    qtbot.wait(0)
    panel.toggle_fold()            # 展开（200ms 动画）
    _wait_fold_animation(qtbot, panel)

    viewport = panel._status_scroll.viewport()
    assert viewport.height() > 0, "状态区还没展开，用例前提不成立"
    assert panel._status_list.height() > viewport.height(), "内容没超出可视区，前提不成立"

    screen = QGuiApplication.primaryScreen().availableGeometry()
    assert panel.height() <= screen.height(), (
        f"面板高 {panel.height()} 顶穿了屏幕 {screen.height()}：底部行会跑到屏幕外"
    )

    bar = panel._status_scroll.verticalScrollBar()
    assert bar.maximum() > 0
    bar.setValue(bar.maximum())
    qtbot.wait(10)
    last = rows[-1]
    bottom = last.mapTo(viewport, QPoint(0, 0)).y() + last.height()
    assert bottom <= viewport.height(), "滚到底后末行仍被裁掉"


def test_panel_keeps_no_scrollbar_while_content_fits(qtbot):
    """内容放得下时**不许**出滚动条（§5 #28 的另一半：别为滚动改坏常态布局）。

    上界只在「屏幕真装不下」时才咬合。行数少（今天 6 个组件就是这档）时滚动容器
    必须正好等于内容自然高度：多出 1px 都是白边，少 1px 就是裁行。
    """
    from barkit.shell.ui_kit import HoverRow

    panel = StatusPanel()
    qtbot.addWidget(panel)
    rows = [HoverRow(lambda: None) for _ in range(3)]
    for w in rows:
        w.setFixedHeight(40)
    panel.set_rows(rows)
    panel.toggle_at(None)
    qtbot.wait(0)
    panel.toggle_fold()
    _wait_fold_animation(qtbot, panel)

    assert panel._status_scroll.verticalScrollBar().maximum() == 0
    assert panel._status_scroll.height() == panel._status_list.sizeHint().height()


def test_icon_click_reveals_that_component_row(qtbot, monkeypatch):
    """点图标 = 展开**并下探到该组件那一行**（§5 #27 尾巴，2026-09-18 重启）。

    当年删掉这半件的唯一理由是「面板没有滚动容器 ⇒ 滚到那一行无容器可动」。§5 #28
    把状态行包进 ``QScrollArea`` 之后，目标行完全可能落在可视区外 —— 于是「展开了，
    却没看到你要看的那行」成了真问题，正是「下探」要解决的那个。

    做法：把可用高度压到一屏装不下（``_available_panel_height`` 是预算的唯一来源，
    直接换掉它，比依赖测试机真实屏幕尺寸稳），点**最后**一个组件的图标，核该组件的
    **首行**滚进了可视区 —— 下探的落点是「该组件那一行」，一组件多行时取首行。
    """
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    from barkit.contribution.specs import StatusRowSpec

    # 6 个组件 × 2 行；把屏幕预算压到 260px ⇒ 内容必定超屏、滚动条必出
    comps = [
        ComponentSpec(plugin_id=f"p{i}", icon=IconName.GEAR, name=f"组件{i}")
        for i in range(6)
    ]
    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.set_components(comps, _StubContrib())
    panel.sync_status_rows(
        [
            StatusRowSpec(plugin_id=f"p{i}", key=f"k{j}", title=f"行{i}-{j}")
            for i in range(6)
            for j in range(2)
        ]
    )
    monkeypatch.setattr(panel, "_available_panel_height", lambda: 260)

    panel.toggle_at(None)            # 首显 = 折叠态
    qtbot.wait(0)
    assert panel._folded, "用例前提：首显必须是折叠态（否则测不到「展开时下探」）"

    # 点最后一个组件 p5 的图标：它的两行在列表最末尾
    icon = panel._icon_widgets["p5"]
    QTest.mouseClick(icon, Qt.MouseButton.LeftButton)
    _wait_fold_animation(qtbot, panel)

    assert not panel._folded, "点图标没展开"
    assert panel._pending_reveal is None, "待定位的组件没被消费掉（会赖到下一次折叠上）"

    viewport = panel._status_scroll.viewport()
    assert panel._status_list.height() > viewport.height(), "内容没超出可视区，前提不成立"

    target = panel._row_widgets["p5"][0]

    def _target_fully_visible() -> bool:
        y = target.mapTo(viewport, QPoint(0, 0)).y()
        return y >= 0 and y + target.height() <= viewport.height()

    # 定位动作由一拍单次定时器发出（见 ``_on_fold_anim_finished``），等条件而不是等时长
    qtbot.waitUntil(_target_fully_visible, timeout=2000)
    assert _target_fully_visible()


def test_icon_click_leaves_scroll_alone_when_row_already_visible(qtbot, monkeypatch):
    """目标行本来就看得到时，下探**不许**动滚动位置（§5 #27 尾巴的边界）。

    ``ensureWidgetVisible`` 带 50px 边距，会把目标行推到偏中的位置 —— 那在「本来就
    看得见」时纯属多余：点图标的主语义是折叠 / 展开，顺手把列表挪一下会让人以为
    点错了。故只在目标行**看不全**时才滚。

    做法：先展开、把列表滚到底（p5 的两行因此可见），收起，再点 p5 的图标展开 ——
    此时滚动位置必须原封不动。
    """
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    from barkit.contribution.specs import StatusRowSpec

    comps = [
        ComponentSpec(plugin_id=f"p{i}", icon=IconName.GEAR, name=f"组件{i}")
        for i in range(6)
    ]
    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.set_components(comps, _StubContrib())
    panel.sync_status_rows(
        [
            StatusRowSpec(plugin_id=f"p{i}", key=f"k{j}", title=f"行{i}-{j}")
            for i in range(6)
            for j in range(2)
        ]
    )
    monkeypatch.setattr(panel, "_available_panel_height", lambda: 260)

    panel.toggle_at(None)
    qtbot.wait(0)
    panel.toggle_fold()                       # 展开
    _wait_fold_animation(qtbot, panel)

    bar = panel._status_scroll.verticalScrollBar()
    assert bar.maximum() > 0, "内容没超出可视区，前提不成立"
    bar.setValue(bar.maximum())               # 滚到底 ⇒ p5 的行已经在可视区
    qtbot.wait(10)
    viewport = panel._status_scroll.viewport()
    t = panel._row_widgets["p5"][0]
    y = t.mapTo(viewport, QPoint(0, 0)).y()
    assert y >= 0 and y + t.height() <= viewport.height(), "前提不成立：p5 首行还没看全"
    before = bar.value()

    panel.toggle_fold()                       # 收起（此时不改滚动位置）
    _wait_fold_animation(qtbot, panel)
    QTest.mouseClick(panel._icon_widgets["p5"], Qt.MouseButton.LeftButton)   # 展开＋下探
    _wait_fold_animation(qtbot, panel)
    qtbot.wait(30)

    assert not panel._folded
    assert bar.value() == before, "目标行本来就看得全，不该挪滚动位置"


def test_icon_click_while_expanded_does_not_scroll(qtbot, monkeypatch):
    """展开态点图标仍**只收起**，不「滚到某行」（§5 #27 尾巴的另一半）。

    守住 §5.2 那条已裁定的点击语义：同一控件只做一件事。若按上下文分裂成「折叠时
    定位、展开时定位而非收起」，就多出一个要向用户解释的状态——§2.6 否掉 S3 时用的
    正是这条判据。本用例把滚动条先推到底，再点图标，核它走的是收起、且滚动位置没被
    动过（收起时「滚到某行」无意义，不许顺手改滚动位置）。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from barkit.contribution.specs import StatusRowSpec

    comps = [
        ComponentSpec(plugin_id=f"p{i}", icon=IconName.GEAR, name=f"组件{i}")
        for i in range(6)
    ]
    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.set_components(comps, _StubContrib())
    panel.sync_status_rows(
        [
            StatusRowSpec(plugin_id=f"p{i}", key=f"k{j}", title=f"行{i}-{j}")
            for i in range(6)
            for j in range(2)
        ]
    )
    monkeypatch.setattr(panel, "_available_panel_height", lambda: 260)

    panel.toggle_at(None)
    qtbot.wait(0)
    panel.toggle_fold()                       # 先展开
    _wait_fold_animation(qtbot, panel)

    bar = panel._status_scroll.verticalScrollBar()
    assert bar.maximum() > 0, "内容没超出可视区，前提不成立"
    bar.setValue(bar.maximum())
    qtbot.wait(10)
    before = bar.value()

    QTest.mouseClick(panel._icon_widgets["p0"], Qt.MouseButton.LeftButton)
    _wait_fold_animation(qtbot, panel)
    qtbot.wait(30)      # 给「万一误留了待定位」的那一拍定时器机会，让它暴露出来

    assert panel._folded, "展开态点图标应当是收起"
    assert panel._pending_reveal is None, "收起这一半不该留下待定位的组件"
    assert bar.value() == before, "收起那条路径动了滚动位置（它不该碰滚动）"


def test_panel_sync_status_cap_rules(qtbot):
    """_sync_status_cap 的三条规则：折叠不动 / 动画中不抢 / 展开静止时解除。"""
    panel = StatusPanel()
    qtbot.addWidget(panel)

    # ① 折叠态：不解除（上界必须保持 0，否则状态区会露出）
    panel._folded = True
    panel._status_section.setMaximumHeight(0)
    panel._sync_status_cap()
    assert panel._status_section.maximumHeight() == 0

    # ② 展开但动画在跑：不抢（避免与动画争高度）
    panel._folded = False
    panel._anim = QPropertyAnimation(panel, b"statusMaxH")
    panel._anim.setStartValue(0)
    panel._anim.setEndValue(100)
    panel._anim.start()
    panel._sync_status_cap()
    assert panel._status_section.maximumHeight() == 0   # 未被改动
    panel._anim.stop()

    # ③ 展开且无动画：解除上界
    panel._anim = None
    panel._sync_status_cap()
    assert panel._status_section.maximumHeight() == QWIDGETSIZE_MAX


def test_panel_first_open_height_matches_later_opens(qtbot):
    """启动后首次打开的高度必须与重开一致（首开偏高的回归测试）。

    回归：启动期 ``set_contributions → _relayout`` 会在面板**仍隐藏**时算一次尺寸，
    而折叠态的「状态区上界 = 0」原先只在 ``_show_at_anchor`` 才落地 —— 于是隐藏期
    算的是**展开态**高度（含状态区 25px），首开就偏高（实测 173 vs 148），关掉重开
    才对。修法：构造末尾即落地折叠态（``_apply_fold_state``），并在 ``show()`` 之后
    再重排一次兜底（隐藏期的 ``adjustSize`` 拿到的是过期 sizeHint）。
    """
    from barkit.shell.ui_kit import HoverRow

    def _comp(plugin_id, key, label):
        comp = ComponentSpec(plugin_id=plugin_id, icon=IconName.GEAR, name=label)
        state = StateSpec(
            plugin_id=plugin_id, key=key,
            icon=IconName.GEAR, title=label,
            states=(StateOption("ok", StateLevel.OK, text=label),),
            initial="ok",
            stale_after=None,
        )
        return comp, state

    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.set_rows([HoverRow(lambda: None) for _ in range(3)])
    comps, states = zip(*[
        _comp("notify_guard", "notify", "通知守卫"),
        _comp("route_guard", "route", "路由守卫"),
    ])
    panel.set_components(list(comps), _StubContrib(list(states)))
    panel.set_primary_actions([
        ActionSpec(plugin_id="recognize", label="截图识别", on_click=lambda: None),
        ActionSpec(plugin_id="autotype", label="一键输入", on_click=lambda: None),
    ])
    panel._relayout()          # 启动期最后一步（面板仍隐藏）

    heights = []
    for _ in range(3):
        panel.toggle_at(None)  # 打开
        qtbot.wait(0)
        heights.append(panel.height())
        panel.toggle_at(None)  # 关闭
        qtbot.wait(0)
    assert len(set(heights)) == 1, f"三次打开高度不一致：{heights}"
    # 折叠态不该给状态区留空间
    assert panel._status_section.maximumHeight() == 0


def test_check_stale_emits_state_level_changed(qtbot):
    """失联改判必须发 stateLevelChanged（否则托盘徽标与面板不一致）。

    回归：``_check_stale`` 原先只改面板芯片文字，既不更新 ``_state_level`` 也不通知
    app → 面板已标红、托盘仍显示正常。且只在状态**翻转**时发一次（3s 一轮的定时器，
    不能每轮都重绘图标）。
    """
    def _state(stale_after):
        return StateSpec(
            plugin_id="notify_guard", key="notify",
            icon=IconName.GEAR, title="通知守卫",
            states=(StateOption("running", StateLevel.OK, text="运行中"),),
            initial="running",
            stale_after=stale_after,
        )

    panel = StatusPanel()
    qtbot.addWidget(panel)
    state = _state(stale_after=30)
    comp = ComponentSpec(plugin_id="notify_guard", icon=IconName.GEAR, name="通知守卫")
    panel.set_components([comp], _StubContrib([state]))
    topic = state.topic
    snap = StateReport("running")
    panel.update_state_snapshot(state, snap)     # 记录 last_at（未超时）

    got = []
    panel.stateLevelChanged.connect(lambda topic, level: got.append((topic, level)))

    panel._state_last_at[topic] -= 999     # 伪造超时
    panel._check_stale()
    assert got == [(topic, "err")], f"失联未上报：{got}"
    assert panel._icon_widgets["notify_guard"]._level == "err"
    assert "状态失联" in panel._icon_widgets["notify_guard"].toolTip()

    # 第 2 轮仍是失联 → 不应重复发（避免每 3s 重绘一次托盘图标）
    panel._check_stale()
    assert got == [(topic, "err")]

    # 事件恢复 → 解除失联标记，且未超时不再改判
    panel.update_state_snapshot(state, snap)
    panel._check_stale()
    assert len(got) == 1


def test_check_stale_measures_with_monotonic_clock(qtbot, monkeypatch):
    """失联判据减的是单调钟：墙钟前跳不会让**所有** topic 在同一拍集体假失联。

    回归：``_state_last_at`` 原存 ``time.time()``、``_check_stale`` 也拿它取 ``now``。
    时钟被往前校（NTP 步进 / 唤醒后校正）时，``now - last`` 一跳就跨过全部
    ``stale_after`` ⇒ 面板行与托盘徽标同时标红，而下一次状态事件到达后自愈，现场只
    看得见「闪一下红」。反向（回拨）更糟：这条兜底**整个失效**且一声不响。
    """
    def _state(stale_after):
        return StateSpec(
            plugin_id="notify_guard", key="notify",
            icon=IconName.GEAR, title="通知守卫",
            states=(StateOption("running", StateLevel.OK, text="运行中"),),
            initial="running",
            stale_after=stale_after,
        )

    panel = StatusPanel()
    qtbot.addWidget(panel)
    state = _state(stale_after=30)
    comp = ComponentSpec(plugin_id="notify_guard", icon=IconName.GEAR, name="通知守卫")
    panel.set_components([comp], _StubContrib([state]))
    panel.update_state_snapshot(state, StateReport("running"))

    got = []
    panel.stateLevelChanged.connect(lambda topic, level: got.append((topic, level)))
    # 墙钟被往前校了十一天：减墙上时钟的话，此刻每个 topic 都会「超时」
    monkeypatch.setattr(time, "time", lambda: 2_000_000_000.0)
    panel._check_stale()
    assert got == [], f"墙钟一跳就误判失联：{got}"


def test_status_item_click_dedup_uses_monotonic_clock(qtbot, monkeypatch):
    """「刚关过就不重开」那 0.3 秒用单调钟：墙钟回拨不会让点图标打不开面板。

    回归：这段去重原是 ``time.time() - self._last_hide < 0.3``。时钟被**往回**校时
    差值长期为负 ⇒ 恒真 ⇒ 每次点菜单栏图标都被当成「刚被原生 dismiss 关掉的那一次」
    ⇒ **面板点不开**，而日志里只有一句「面板关闭」，看不出是被这句挡掉的。
    """
    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.toggle_at(None)            # 先显示
    qtbot.wait(0)
    panel.dismiss()                  # 走统一关闭入口（记 _last_hide）
    qtbot.wait(0)
    assert not panel.isVisible()

    panel._last_hide = time.monotonic() - 10.0    # 关了十秒：远超去重窗口
    monkeypatch.setattr(time, "time", lambda: 1.0)   # 墙钟被校回 1970
    panel.on_status_item_click(None)
    qtbot.wait(0)
    assert panel.isVisible(), "墙钟回拨后点图标打不开面板（去重恒真）"


def test_status_row_main_icon_is_identity_and_corner_badge_is_value(qtbot):
    """三处显示位的分工（决议 D16）：行主位 = **身份图标**；右上角标 = **取值图标**。

    行上一次说完三件事：主位说「是谁」（``ComponentSpec.icon``，固定）、角标说
    「什么状态」（代表态的取值图标）、角标颜色说「多坏」（级别色）。三条断言各证一半：

    ① 身份图标**晚于行注入也要生效**（组件身份与状态行是两个注入点）；
    ② 取值变了 → **角标**跟着换，主位**不动**（两件事混回一处就又分不清了）；
    ③ ``ok`` 不画角标（健康不抢眼，UI 规范 §5.3），非 ok 才画。

    取值图标本身仍是 ``StateOption.icon``（静态声明，随取值变），解析在
    ``shell/renderers/state_view``；回落不生效会让「在线/离线」两态画成同一个图标，
    也就是面板在撒谎。
    """
    from barkit.contribution.specs import (
        ComponentSpec,
        StateSpec,
        StatusRowSpec,
    )

    row_spec = StatusRowSpec(plugin_id="notify_guard", key="main")   # 名字/图标都不写（D14/D16）
    state = StateSpec(
        plugin_id="notify_guard", key="display", icon=IconName.BELL, title="第二屏",
        states=(
            StateOption("extended", StateLevel.OK, IconName.DISPLAY_ON, "第二屏已连接"),
            StateOption("absent", StateLevel.IDLE, text="无第二屏"),
        ),
    )
    comp = ComponentSpec(plugin_id="notify_guard", icon=IconName.BELL)
    panel = StatusPanel()
    qtbot.addWidget(panel)

    # ① 行先注入：此刻还不知道组件身份 → 主位那一格留空（框架不替组件编一枚图标；
    # 盒里**画**的是通用齿轮，那是骨架的占位，不是被记下来的身份 —— 见 RecordRow.ICON）
    panel.sync_status_rows([row_spec])
    row = panel._row_widgets["notify_guard"][0]
    assert row._icon_name is None
    # 组件身份后到 → 回填到行上，不因注入顺序失效
    panel.set_components([comp], _StubContrib([state]))
    assert row._icon_name is IconName.BELL

    panel.update_state_snapshot(state, StateReport("extended"))
    assert row._icon_name is IconName.BELL               # 主位：身份图标
    assert row._badge_icon is IconName.DISPLAY_ON        # 角标：取值图标
    assert row._badge._level == "ok"
    # 面板本身没显示，isVisible() 恒 False ⇒ 用 isVisibleTo(行) 问「相对这一行是否显示」
    assert not row._badge.isVisibleTo(row), "ok 不该画角标（健康不抢眼）"
    # 位图是否被重画用 cacheKey 判（``toImage().bits()`` 在 macOS 上两次调用不保证逐字节相同）
    main_key_before = row._ico.pixmap().cacheKey()

    # ② 取值翻转：角标换字形，主位不动
    panel.update_state_snapshot(state, StateReport("absent"))
    assert row._icon_name is IconName.BELL, "主位图标跟着取值变了 —— 那是角标的活"
    assert row._ico.pixmap().cacheKey() == main_key_before, "主位图标位图被重画了（应固定不变）"
    assert row._badge_icon is IconName.BELL              # 取值没给图标 → 回落状态机主图标
    # ③ 非 ok → 角标画出来（字形再回落该级别的通用标记，见 icons.badge_name）
    assert row._badge._icon is IconName.BELL
    assert row._badge._level == "idle"
    assert row._badge.isVisibleTo(row)


def test_titlebar_icon_paints_value_icon_in_level_color_without_badge(qtbot):
    """标题栏图标（决议 D16）：图标 = **取值图标**、颜色 = **级别色染在图标上**、**不画角标**。

    三条断言各证一半：

    ① 图标跟代表态的**取值**走（不再是固定的身份图标）；
    ② 颜色跟代表态的**级别**走（位图随级别换色 —— 位图是按颜色渲染的，颜色不换
       就等于级别没表达出来）；
    ③ **角标位空着**：原 9px 圆点画在 ``(n-7, 7)``，那里现在必须是方块底色 ——
       角标位归列表行，同一枚状态信息不重复画两遍。
    """
    from PySide6.QtGui import QColor

    from barkit.shell.ui_kit import _theme_tokens

    state = StateSpec(
        plugin_id="notify_guard", key="display", icon=IconName.BELL, title="第二屏",
        states=(
            StateOption("extended", StateLevel.OK, IconName.DISPLAY_ON, "第二屏已连接"),
            StateOption("lost", StateLevel.WARN, IconName.DISPLAY_OFF, "第二屏已断开"),
        ),
    )
    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.set_components(
        [ComponentSpec(plugin_id="notify_guard", icon=IconName.BELL)], _StubContrib([state])
    )
    icon = panel._icon_widgets["notify_guard"]

    panel.update_state_snapshot(state, StateReport("extended"))
    assert icon._value_icon is IconName.DISPLAY_ON        # ① 取值图标
    assert icon._level == "ok"
    ok_pm = bytes(icon.grab().toImage().bits())

    panel.update_state_snapshot(state, StateReport("lost"))
    assert icon._value_icon is IconName.DISPLAY_OFF
    assert icon._level == "warn"
    assert bytes(icon.grab().toImage().bits()) != ok_pm, "级别变了颜色却没变（位图没按级别染色）"

    # ③ 角标位是空的：原圆点中心（n-7, 7）必须是方块底色 surface3
    img = icon.grab().toImage()
    ratio = img.devicePixelRatio()
    px = img.pixelColor(int((icon.width() - 7) * ratio), int(7 * ratio))
    assert px.name() == QColor(_theme_tokens()["surface3"]).name(), (
        f"标题栏角标位不是空的（{px.name()}）—— 决议 D16 里标题栏不画角标"
    )


def test_status_row_two_controls_do_not_crosstalk(qtbot):
    """多槽控件（决议 D6）：0–2 个按声明顺序画出来，且**各调各的回调**。

    单槽时代一行只有一个控件，闭包晚绑定不会暴露问题；改多槽后若 ``connect`` 的闭包
    引用循环变量，两个开关会一起调到**最后一个**的回调 —— 一个静默指错方向的 bug
    （点了「A」实际切换了「B」，界面还会正确变化，很难看出来）。

    同时锁 ``refresh_dynamic`` 不串台：每个开关各读自己那份 ``get_checked``。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from barkit.contribution.specs import StatusRowSpec, SwitchSpec

    fired: list[str] = []
    flags = {"a": True, "b": False}

    spec = StatusRowSpec(
        plugin_id="demo", key="main", title="双开关",
        controls=(
            SwitchSpec(
                checked=True, on_toggle=lambda: fired.append("a"),
                get_checked=lambda: flags["a"],
            ),
            SwitchSpec(
                checked=False, on_toggle=lambda: fired.append("b"),
                get_checked=lambda: flags["b"],
            ),
        ),
    )
    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.sync_status_rows([spec])

    row = panel._row_widgets["demo"][0]
    assert len(row._switches) == 2, "声明的两个控件都要画出来"

    # 点第一个开关：只有第一个的回调该响（晚绑定会让它响成 "b"）
    QTest.mouseClick(row._switches[0][0], Qt.MouseButton.LeftButton)
    assert fired == ["a"], f"第一个开关点到了别人的回调：{fired}"

    # 运行期重读不串台：各自读各自那份 get_checked（两个方向都验）
    flags["a"] = False                      # 只翻第一个
    row.refresh_dynamic()
    assert not row._switches[0][0].isChecked()
    assert not row._switches[1][0].isChecked(), "第二个开关被第一个的取值带偏"

    flags["b"] = True                       # 只翻第二个
    row.refresh_dynamic()
    assert row._switches[1][0].isChecked()
    assert not row._switches[0][0].isChecked(), "第一个开关被第二个的取值带偏"


def test_row_dynamic_refresh_survives_a_broken_callback(qtbot):
    """一个回调抛异常，本行其余控件仍要刷新完。

    ``refresh_dynamic`` 跑在 ``_recompute_plugin`` 的循环里：不隔离的话，一处
    ``get_checked`` 炸掉会把本行剩下的控件、以及后面那些行一起掐掉 —— 用户看到
    的是面板半新半旧，而界面上没有任何提示。
    """
    from barkit.contribution.specs import StatusRowSpec, SwitchSpec

    spec = StatusRowSpec(
        plugin_id="demo", key="main", title="混合控件",
        controls=(
            SwitchSpec(
                checked=True, on_toggle=lambda: None,
                get_checked=lambda: (_ for _ in ()).throw(RuntimeError("插件还没就绪")),
            ),
            SwitchSpec(checked=False, on_toggle=lambda: None, get_checked=lambda: True),
        ),
    )
    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.sync_status_rows([spec])
    row = panel._row_widgets["demo"][0]

    row.refresh_dynamic()                       # 不冒泡
    assert row._switches[1][0].isChecked() is True, "兄弟开关被前一个的异常拖下去了"


def test_status_row_mode_badge_click_is_dispatched_by_panel(qtbot):
    """H3 回归锁：模式徽标点击**由面板分派**，徽标自己只 emit ``clicked``。

    此前 ``ModeBadge`` 在 ``mouseReleaseEvent`` 里直接调 ``on_click`` ⇒ ``clicked``
    从不 emit ⇒ 面板的 ``_on_mode_click`` 是死代码，「点完按 ``get_state`` 自刷新」
    从没生效过（表现为点了徽标模式变了、字却不变）。两条断言各证一半：

    ① 点击真的跑到了声明的 ``on_click``（证明信号 emit 了、面板接住了）；
    ② 跑完之后徽标文案已按 ``get_state`` 自同步（证明分派不是空跑）。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from barkit.contribution.specs import ModeBadgeSpec, StatusRowSpec

    active = {"on": True}
    fired: list[int] = []

    def _get_state():
        return ("自动", True) if active["on"] else ("手动", False)

    def _on_click():
        fired.append(1)
        active["on"] = not active["on"]

    badge = ModeBadgeSpec(
        label="自动", accent=True, on_click=_on_click, get_state=_get_state,
    )
    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.sync_status_rows(
        [StatusRowSpec(plugin_id="demo", key="main", title="徽标行", mode=badge)]
    )
    row = panel._row_widgets["demo"][0]
    assert row._mode is not None
    assert row._mode.text() == "自动"

    QTest.mouseClick(row._mode, Qt.MouseButton.LeftButton)
    assert fired == [1], "徽标点击没走到面板分派（clicked 又没 emit？）"
    assert row._mode.text() == "手动", "跑完 on_click 后没按 get_state 自同步"

    # 再点一次回到自动：证明每次点击都走同一条分派链
    QTest.mouseClick(row._mode, Qt.MouseButton.LeftButton)
    assert len(fired) == 2
    assert row._mode.text() == "自动"


def _fake_pick(menu_self, _pos, index=0, anchor_rect=None):
    """替身弹出：不真弹窗，直接在弹层里选第 ``index`` 项。

    ``CandidateMenu`` 刻意把「弹出」与「选中」拆开（``popup_at`` / ``pick``），测试才能
    绕开窗口服务器只验证「选中了谁、跑了什么」。

    **刻意不返回选中项** —— 真实 ``popup_at`` 现在也没有返回值（非阻塞 ``popup()`` 回读
    不到）：选中之后的收尾只走 ``on_picked``。谁若把调用方改回「读返回值」的写法，这些
    用例会当场红 —— 那种形态只有当弹层阻塞（``exec()``）时才需要，而 ``exec()`` 已进禁区。
    """
    menu_self.pick(index)


def test_status_row_mode_badge_candidate_list_pick(qtbot):
    """§5.3：模式徽标的**候选列表**形态 —— 点开展开、选中即生效、徽标上的字跟着变。

    契约 §2.5：候选列表下「选中某项做什么」写在各自的 ``MenuItem.on_click`` 上，
    无需二次确认；``label`` **兼任当前项**（决议 D33）⇒ 选完徽标就该写着选中项。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from barkit.contribution.specs import MenuItem, ModeBadgeSpec, StatusRowSpec
    from barkit.shell import panel as panel_mod

    picked: list[str] = []
    items = (
        MenuItem("office", lambda: picked.append("office"), "办公网段 · 3 条路由"),
        MenuItem("home", lambda: picked.append("home"), "家庭网段 · 1 条路由"),
    )
    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.sync_status_rows([StatusRowSpec(
        plugin_id="demo", key="main", title="徽标行",
        mode=ModeBadgeSpec(label="office", accent=True, items=items),
    )])
    row = panel._row_widgets["demo"][0]
    assert row._mode.text() == "office"

    with patch.object(panel_mod.CandidateMenu, "popup_at",
                      lambda self, pos, anchor_rect=None: _fake_pick(self, pos, index=1)):
        QTest.mouseClick(row._mode, Qt.MouseButton.LeftButton)

    assert picked == ["home"], "候选列表没跑选中项的 on_click（选中即生效）"
    assert row._mode.text() == "home", "选完徽标没显示当前项（label 兼任当前项）"


def test_mode_badge_candidates_reread_per_click(qtbot):
    """候选列表**每次点击重读** ``get_items`` —— 静态声明会给到过期列表。

    契约 §2.6：候选集合会随配置变化的组件（如「网络切换」的场景列表）必须给回调；
    若渲染器只在构造时读一次，用户看到的就是上一次的列表。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from barkit.contribution.specs import MenuItem, ModeBadgeSpec, StatusRowSpec
    from barkit.shell import panel as panel_mod

    seen: list[tuple[str, ...]] = []
    gen = {"n": 1}

    def _get_items():
        return tuple(
            MenuItem(f"场景{i}", lambda i=i: None, f"第 {gen['n']} 版") for i in range(gen["n"])
        )

    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.sync_status_rows([StatusRowSpec(
        plugin_id="demo", key="main", title="徽标行",
        mode=ModeBadgeSpec(label="场景0", accent=True, get_items=_get_items),
    )])
    row = panel._row_widgets["demo"][0]

    def _record(self, pos, anchor_rect=None):
        seen.append(tuple(i.label for i in self.items))
        return None                     # 取消：本例只关心「弹出来的是什么」

    with patch.object(panel_mod.CandidateMenu, "popup_at", _record):
        QTest.mouseClick(row._mode, Qt.MouseButton.LeftButton)
        gen["n"] = 3
        QTest.mouseClick(row._mode, Qt.MouseButton.LeftButton)

    assert seen == [("场景0",), ("场景0", "场景1", "场景2")], f"候选列表未逐次重读：{seen}"


def test_mode_badge_candidate_cancel_keeps_label(qtbot):
    """取消（点外部关闭 / Esc）⇒ 什么都不做：不跑 on_click、徽标也不变。"""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from barkit.contribution.specs import MenuItem, ModeBadgeSpec, StatusRowSpec
    from barkit.shell import panel as panel_mod

    fired: list[str] = []
    items = (MenuItem("office", lambda: fired.append("office")),
             MenuItem("home", lambda: fired.append("home")))
    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.sync_status_rows([StatusRowSpec(
        plugin_id="demo", key="main", title="徽标行",
        mode=ModeBadgeSpec(label="office", accent=True, items=items),
    )])
    row = panel._row_widgets["demo"][0]

    with patch.object(panel_mod.CandidateMenu, "popup_at", lambda self, pos, anchor_rect=None: None):
        QTest.mouseClick(row._mode, Qt.MouseButton.LeftButton)

    assert fired == []
    assert row._mode.text() == "office"


def test_interactive_child_swallows_its_press(qtbot):
    """行内**可交互**子控件的鼠标按下必须自己吃掉，不得冒泡给整行的点击。

    根因（2026-09-18 实验证实）：``ModeBadge`` 是 ``QLabel``、``Switch`` 是裸 ``QWidget``，
    两者默认都**忽略**鼠标按下；Qt 于是把它**冒泡给父级** —— 而它们通常长在一行**可点的
    状态行**里，那一行的整行点击（打开详情页）就跟着跑了。真机上表现为两条：
    「点徽标弹不出场景菜单」（详情页层级更高，把候选菜单压住 / 顶掉）、「翻开关顺带开详情页」。

    三个方向一起钉：徽标不吃 ⇒ 红；开关不吃 ⇒ 红；**点行内非交互标签仍走整行点击** ⇒
    防止"修过头"把整行可点性一并废掉（那是本行唯一的详情入口）。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from barkit.contribution.specs import (
        MenuItem,
        ModeBadgeSpec,
        StatusRowSpec,
        SwitchSpec,
    )
    from barkit.shell import panel as panel_mod

    row_clicks: list[str] = []
    toggles: list[str] = []
    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.sync_status_rows([StatusRowSpec(
        plugin_id="demo", key="main", title="一行",
        on_click=lambda: row_clicks.append("row"),
        controls=(SwitchSpec(checked=False, on_toggle=lambda: toggles.append("sw")),),
        mode=ModeBadgeSpec(label="office", accent=True, items=(
            MenuItem("office", lambda: None, "办公网段 · 3 条路由"),
        )),
    )])
    row = panel._row_widgets["demo"][0]

    with patch.object(panel_mod.CandidateMenu, "popup_at", lambda self, pos, anchor_rect=None: None):
        QTest.mouseClick(row._mode, Qt.MouseButton.LeftButton)
    assert row_clicks == [], "点徽标连带跑了整行点击 —— 详情页会把候选菜单盖掉"

    QTest.mouseClick(row._switches[0][0], Qt.MouseButton.LeftButton)
    assert toggles == ["sw"], "开关自己没响应？"
    assert row_clicks == [], "翻开关连带跑了整行点击"

    # 反面：非交互标签仍旧走整行点击（整行可点性是声明了详情入口的组件唯一的入口）
    QTest.mouseClick(row._name, Qt.MouseButton.LeftButton)
    assert row_clicks == ["row"], "整行点击被修没了"


def test_row_menu_button_swallows_its_press(qtbot):
    """行内**下拉按钮**（`MenuSpec`）的按下同样不得冒泡给整行点击。

    为什么单独钉这一条（任务板 §5 #29 **P0 ③** 的收尾）：上一批只钉了徽标与开关，
    而当时列的担心是「下拉 / 选择器这一类走的是 **另一个**排除路径（`HoverRow` 的
    `combo` 参数），而**无人传参** ⇒ 若真有行内下拉，点它必连带触发整行 `on_click`」。

    核实结果（本用例就是证据，不是推断）：
    - 面板里的「下拉」只有 `MenuSpec` → `ui_kit.MenuButton`（**`QPushButton` 子类**，
      自己处理并 accept 按下）⇒ 行 `HoverRow` 收不到 mousePress，**不穿透**；
    - `HoverRow` 那条 `combo` 排除分支的消费方是设置页的 `ProfileSelector` /
      `ChoiceSelector`（`QComboBox`），它们**不在**可点行里 —— 面板路径上无人传 `combo`；
    - 全仓 `src/` 今天**零个** `MenuSpec` / `ButtonSpec` 声明方（只有用例），
      所以这条担心在**存在性**上也不成立。

    结论：判「不排」，但把行为钉住 —— 将来真要给某行加下拉时，这条会替他试一次。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from barkit.contribution.specs import MenuItem, MenuSpec, StatusRowSpec
    from barkit.shell import panel as panel_mod

    row_clicks: list[str] = []
    picked: list[str] = []
    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.sync_status_rows([StatusRowSpec(
        plugin_id="demo", key="main", title="一行",
        on_click=lambda: row_clicks.append("row"),
        controls=(MenuSpec(
            items=(MenuItem("office", lambda: picked.append("office"), "办公网段"),),
            current="office",
        ),),
    )])
    row = panel._row_widgets["demo"][0]
    assert len(row._menus) == 1, "MenuSpec 应当渲染成一枚 MenuButton"

    with patch.object(panel_mod.CandidateMenu, "popup_at", lambda self, pos, anchor_rect=None: None):
        QTest.mouseClick(row._menus[0][0], Qt.MouseButton.LeftButton)

    assert row_clicks == [], "点行内下拉连带跑了整行点击 —— 那一行的详情入口会被误触发"


def test_candidate_menu_popup_does_not_block(qtbot):
    """§5 第 17 条：候选菜单的**真实**弹出路径不得阻塞。

    这条用例刻意**不 patch** ``popup_at`` —— 正因为它不 patch 才证明得了「弹层里没有嵌套
    事件循环」：``exec()`` 在离屏（offscreen）下会永久阻塞，用例根本走不到断言（整轮直接
    挂死）。顺带钉住「弹出后弹层确实在屏幕上」—— 换 ``popup()`` 后仍要有可见的候选列表，
    不能悄悄退化成「什么都不弹」。
    """
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QWidget

    from barkit.contribution.specs import MenuItem
    from barkit.shell.ui_kit import CandidateMenu

    host = QWidget()
    qtbot.addWidget(host)
    fired: list[str] = []
    menu = CandidateMenu(
        (MenuItem("office", lambda: fired.append("office"), "办公网段"),
         MenuItem("home", lambda: fired.append("home"), "家庭网段")),
        current="office",
        parent=host,
    )
    qtbot.addWidget(menu)

    menu.popup_at(host.mapToGlobal(QPoint(0, host.height())))

    assert menu.isVisible(), "非阻塞弹出后弹层应当已经在屏幕上"
    assert fired == [], "只是弹出来、还没选，谁的 on_click 都不该跑"
    menu.close()


def test_candidate_menu_on_picked_fires_only_on_real_pick(qtbot):
    """``on_picked`` 只在**真选中**时回调一次；取消（点外部 / Esc）一次都不调。

    它是弹出改非阻塞之后调用方**唯一**的收尾通道（``popup_at`` 没有返回值了），所以要
    把「谁被选中」「调了几次」都钉死：这两件事任何一件错，收尾就静默失效 —— 比阻塞隐蔽。
    """
    from barkit.contribution.specs import MenuItem
    from barkit.shell.ui_kit import CandidateMenu

    got: list[str] = []
    menu = CandidateMenu(
        (MenuItem("office", lambda: None), MenuItem("home", lambda: None)),
        current="office",
        on_picked=lambda item: got.append(item.label),
    )
    qtbot.addWidget(menu)

    menu.close()                                   # 取消路径：什么都没选
    assert got == [], "取消（点外部 / Esc）不该触发 on_picked"
    assert menu.picked is None, "没选过就不该有「最近选中项」"

    menu.pick(1)
    assert got == ["home"], f"选中项没交付给 on_picked：{got}"
    assert menu.picked is not None and menu.picked.label == "home"


def test_candidate_menu_on_picked_exception_is_isolated(qtbot):
    """``on_picked`` 抛异常不得穿进 Qt 事件循环（与 ``on_click`` 同口径）。

    收尾逻辑是**业务边界**：调用方重读插件状态时崩了，不该把弹层和事件循环一起带走 ——
    弹层照常收起、异常记日志。
    """
    from barkit.contribution.specs import MenuItem
    from barkit.shell.ui_kit import CandidateMenu

    def _boom(_item):
        raise RuntimeError("收尾炸了")

    menu = CandidateMenu((MenuItem("office", lambda: None),), on_picked=_boom)
    qtbot.addWidget(menu)

    menu.pick(0)                                   # 不抛即通过：真穿出去这里就是 ERROR
    assert not menu.isVisible(), "收尾抛异常后弹层照样要收起"
    assert menu.picked is not None


def test_mode_badge_real_popup_returns_and_cancel_keeps_label(qtbot):
    """端到端（**不 patch**）：徽标点击走真实弹出，不阻塞；没选中则徽标保持原样。

    与 ``test_candidate_menu_popup_does_not_block`` 同源，但从**面板**入口进，覆盖整条链：
    构造弹层（带 ``on_picked``）→ 非阻塞弹出 → 面板持住引用 → 取消后什么都不做。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from barkit.contribution.specs import MenuItem, ModeBadgeSpec, StatusRowSpec

    fired: list[str] = []
    items = (MenuItem("office", lambda: fired.append("office"), "办公网段"),
             MenuItem("home", lambda: fired.append("home"), "家庭网段"))
    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.sync_status_rows([StatusRowSpec(
        plugin_id="demo", key="main", title="徽标行",
        mode=ModeBadgeSpec(label="office", accent=True, items=items),
    )])
    row = panel._row_widgets["demo"][0]

    QTest.mouseClick(row._mode, Qt.MouseButton.LeftButton)

    assert row._candidate_menu is not None and row._candidate_menu.isVisible(), \
        "真实点击后弹层没弹出来（或行没持住它）"
    assert fired == [], "只是弹出来、还没选，谁的 on_click 都不该跑"
    assert row._mode.text() == "office", "取消路径不该改动徽标"

    row._candidate_menu.close()


def test_status_row_menu_control_renders_and_picks(qtbot):
    """``MenuSpec`` 行控件（§5.3）：渲染成「当前项 ▾」按钮，点开展开、选中即生效。"""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from barkit.contribution.specs import MenuItem, MenuSpec, StatusRowSpec
    from barkit.shell import panel as panel_mod

    cur = {"v": "office"}
    picked: list[str] = []
    ctrl = MenuSpec(
        items=(
            MenuItem("office", lambda: (picked.append("office"), cur.__setitem__("v", "office")), "办公网段"),
            MenuItem("home", lambda: (picked.append("home"), cur.__setitem__("v", "home")), "家庭网段"),
        ),
        current="office",
        get_current=lambda: cur["v"],
    )
    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.sync_status_rows([StatusRowSpec(
        plugin_id="demo", key="main", title="菜单行", controls=(ctrl,),
    )])
    row = panel._row_widgets["demo"][0]
    btn = row._menus[0][0]
    assert "office" in btn.text(), f"菜单按钮没显示当前项：{btn.text()!r}"

    with patch.object(panel_mod.CandidateMenu, "popup_at",
                      lambda self, pos, anchor_rect=None: _fake_pick(self, pos, index=1)):
        QTest.mouseClick(btn, Qt.MouseButton.LeftButton)

    assert picked == ["home"]
    assert "home" in btn.text(), "选中后按钮没按 get_current 重读当前项"


def test_status_row_button_control_click(qtbot):
    """``ButtonSpec`` 行控件（§5.3）：按 variant 取 QSS 族，点击直接跑 ``on_click``。"""
    from PySide6.QtWidgets import QPushButton

    from barkit.contribution.specs import ButtonSpec, StatusRowSpec

    fired: list[str] = []
    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.sync_status_rows([StatusRowSpec(
        plugin_id="demo", key="main", title="按钮行",
        controls=(ButtonSpec("导出", on_click=lambda: fired.append("export"), variant="primary"),),
    )])
    row = panel._row_widgets["demo"][0]
    btns = [b for b in row.findChildren(QPushButton) if b.text() == "导出"]
    assert len(btns) == 1, f"ButtonSpec 没渲染出按钮：{[b.text() for b in row.findChildren(QPushButton)]}"
    assert btns[0].objectName() == "primary"
    btns[0].click()
    assert fired == ["export"]


def test_status_row_button_unknown_variant_falls_back(qtbot):
    """未知 ``variant`` 不静默变味：回落到 pill-ghost 并留 warning 日志。"""
    from PySide6.QtWidgets import QPushButton

    from barkit.contribution.specs import ButtonSpec, StatusRowSpec

    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.sync_status_rows([StatusRowSpec(
        plugin_id="demo", key="main", title="按钮行",
        controls=(ButtonSpec("试试", on_click=lambda: None, variant="neon"),),
    )])
    row = panel._row_widgets["demo"][0]
    btns = [b for b in row.findChildren(QPushButton) if b.text() == "试试"]
    assert btns and btns[0].objectName() == "pill-ghost"


def test_candidate_menu_item_sizehint_covers_hint(qtbot):
    """H2 定稿口径：候选项用组合控件渲染，**宽度必须覆写 ``sizeHint``**。

    不覆写的话 ``QMenu`` 会按最窄的一行把整片压窄 —— ``hint``（「这一项是什么」）
    被截断，菜单就退化成「只给名字」，正是决议 D25 要避免的。
    """
    from barkit.contribution.specs import MenuItem
    from barkit.shell.ui_kit import _MenuItemWidget

    bare = _MenuItemWidget(MenuItem("office", lambda: None))
    with_hint = _MenuItemWidget(
        MenuItem("office", lambda: None, "办公网段 · 带 3 条路由，覆盖 10.0.0.0/8")
    )
    qtbot.addWidget(bare)
    qtbot.addWidget(with_hint)
    assert with_hint.sizeHint().width() > bare.sizeHint().width(), "宽度没算上 hint"
    assert with_hint.sizeHint().height() > bare.sizeHint().height(), "高度没算上第二行"


def test_components_preserve_contribution_order(qtbot):
    """标题栏组件图标顺序必须等于贡献注册中心的 components() 顺序（按 order）。

    回归：此前 set_components 用 insertWidget(count()-1) 且 stretch 在循环后才
    添加，导致每插入一个新芯片都被插到前一个之前 → 顺序整体反转（route 跑到
    notify 前面）。这里用两个顺序明确的组件锁住正确顺序。
    """
    comps = [
        ComponentSpec(plugin_id="notify_guard", icon=IconName.GEAR, name="通知守卫"),
        ComponentSpec(plugin_id="route_guard", icon=IconName.GEAR, name="路由守卫"),
    ]
    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.set_components(comps, _StubContrib())

    # 图标按注入顺序出现（stretch 在末尾，不计入）
    ids = [w._spec.plugin_id for w in panel._icon_widgets.values()]
    assert ids == ["notify_guard", "route_guard"], f"标题栏组件顺序异常：{ids}"


def test_icon_click_expands_panel(qtbot):
    """U2 §10.3（点图标版）：点击组件图标展开状态区（与 logo 触发等价）。

    首显为折叠态（状态区最大高度 0）；点击图标后 ``toggle_fold`` 触发 200ms 高度
    动画，展开后状态区最大高度解除、``_folded`` 置 False。
    （原先这里还断言并发一个「下探到该插件行」的信号；该信号 2026-09-17 已删 ——
    面板没有滚动容器，那件事无从实现，详见 ``StatusPanel._on_icon_click`` 的说明。）
    """
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from barkit.shell.ui_kit import HoverRow

    panel = StatusPanel()
    qtbot.addWidget(panel)
    comp = ComponentSpec(plugin_id="notify_guard", icon=IconName.GEAR, name="通知守卫")
    panel.set_components([comp], _StubContrib())
    panel.set_rows([HoverRow(lambda: None)])
    panel.toggle_at(None)            # 首显 = 折叠态
    qtbot.wait(0)
    assert panel._folded

    icon = panel.findChild(_TitleBarIcon)
    assert icon is not None
    QTest.mouseClick(icon, Qt.MouseButton.LeftButton)
    _wait_fold_animation(qtbot, panel)

    assert not panel._folded
    assert panel._status_section.maximumHeight() == QWIDGETSIZE_MAX


def test_icon_click_again_collapses(qtbot):
    """U2 §10.3（点图标版）：展开态再次点击组件图标收起状态区，回到初始折叠高度。"""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from barkit.shell.ui_kit import HoverRow

    panel = StatusPanel()
    qtbot.addWidget(panel)
    comp = ComponentSpec(plugin_id="notify_guard", icon=IconName.GEAR, name="通知守卫")
    panel.set_components([comp], _StubContrib())
    panel.set_rows([HoverRow(lambda: None)])
    panel.toggle_at(None)
    qtbot.wait(0)
    folded = panel._container.height()

    icon = panel.findChild(_TitleBarIcon)
    QTest.mouseClick(icon, Qt.MouseButton.LeftButton)   # 展开
    _wait_fold_animation(qtbot, panel)
    assert panel._container.height() > folded

    QTest.mouseClick(icon, Qt.MouseButton.LeftButton)   # 再次点击 → 收起
    _wait_fold_animation(qtbot, panel)
    assert panel._container.height() == folded
    assert panel._status_section.maximumHeight() == 0


def _n_components(prefix: str, n: int):
    """生成 n 个顺序明确的组件 + 对应 stub 贡献（用于溢出簇测试）。"""
    comps = [
        ComponentSpec(plugin_id=f"{prefix}{i}", icon=IconName.GEAR, name=f"插件{i}")
        for i in range(n)
    ]
    states = [
        StateSpec(
            plugin_id=f"{prefix}{i}", key=f"k{i}",
            icon=IconName.GEAR, title=f"插件{i}",
            states=(StateOption("ok", StateLevel.OK, text="ok"),), initial="ok",
            stale_after=None,
        )
        for i in range(n)
    ]
    return comps, states


def test_titlebar_overflow_follows_plugin_order(qtbot):
    """U2 §5.2 溢出一览：组件数 > N_VIS 时只放前 N_VIS 枚 + 末端溢出簇(+K)。

    K = 隐藏数（按 plugin_order 截断，不按问题优先）；点击溢出簇切到全显后所有
    组件图标出现、溢出簇消失；截断态顺序必须等于 plugin_order 前 N_VIS。
    """
    from barkit.shell.panel import N_VIS

    panel = StatusPanel()
    qtbot.addWidget(panel)
    comps, states = _n_components("p", 9)
    panel.set_components(comps, _StubContrib(states))
    qtbot.wait(0)

    # 截断态：前 N_VIS 枚图标 + 末端溢出簇
    assert len(panel._icon_widgets) == N_VIS
    assert panel._overflow_widget is not None
    assert panel._overflow_widget._hidden_count == 9 - N_VIS
    shown = [w._spec.plugin_id for w in panel._icon_widgets.values()]
    assert shown == [f"p{i}" for i in range(N_VIS)], f"截断态顺序异常：{shown}"

    # 点击溢出簇 → 全显：9 枚图标全在、溢出簇消失
    qtbot.mouseClick(panel._overflow_widget, Qt.MouseButton.LeftButton)  # noqa: F405
    qtbot.wait(0)
    assert panel._components_show_all is True
    assert len(panel._icon_widgets) == 9
    assert panel._overflow_widget is None
    assert [w._spec.plugin_id for w in panel._icon_widgets.values()] == [f"p{i}" for i in range(9)]


def test_titlebar_no_overflow_when_within_limit(qtbot):
    """U2 §5.2：组件数 ≤ N_VIS 时不出现溢出簇，全部图标按序出现。"""
    from barkit.shell.panel import N_VIS

    panel = StatusPanel()
    qtbot.addWidget(panel)
    comps, states = _n_components("q", 5)
    panel.set_components(comps, _StubContrib(states))
    qtbot.wait(0)

    assert len(panel._icon_widgets) == 5
    assert panel._overflow_widget is None
    assert [w._spec.plugin_id for w in panel._icon_widgets.values()] == [f"q{i}" for i in range(5)]
    assert N_VIS >= 5  # 防护：若 N_VIS 被改小到 ≤5，本用例前提失效

def test_row_subtitle_callback_then_fallback_on_error(qtbot, caplog):
    """行级 ``subtitle``（契约 §2.7 / 决议 D24）：声明了就用，抛异常回退代表态 `summary`。

    两条断言各证一半：① 正常路径取**回调值**（不是代表态 `summary`）；
    ② 回调炸了不能把整行文案炸没 —— 回退代表态 `summary` 并记日志。
    这是「组件的一行文案不许拖垮整个面板」的兜底，也是 P1 首个使用方（`notify_guard`）
    在框架侧拿到的保证。
    """
    from barkit.contribution.specs import StatusRowSpec

    panel = StatusPanel()
    qtbot.addWidget(panel)
    state = StateSpec(
        plugin_id="plug", key="main", icon=IconName.BELL, title="主",
        stale_after=None,
        states=(StateOption("ok", StateLevel.OK, text="代表态一句话"),),
        initial="ok",
    )
    panel.set_components([], _StubContrib([state]))
    spec = StatusRowSpec(
        plugin_id="plug", key="main", title="组件",
        subtitle=lambda: "组件合成的一句话",
    )
    panel.sync_status_rows([spec])
    row = panel._row_widgets["plug"][0]

    # ① 走 _recompute_plugin 的正常路径：副标题 = 回调返回值
    panel.update_state_snapshot(state, StateReport("ok"))
    assert row.subtitle_text("代表态一句话") == "组件合成的一句话"

    # ② 回调抛异常 → 回退代表态那一格的解析文案（并留日志），不向上抛
    def boom() -> str:
        raise RuntimeError("组件的副标题算炸了")

    spec.subtitle = boom
    with caplog.at_level("ERROR"):
        assert row.subtitle_text("代表态一句话") == "代表态一句话"
    assert "回退代表态 summary" in caplog.text


def test_titlebar_tooltip_is_the_same_sentence_as_the_row(qtbot):
    """标题栏图标的悬停提示 = 「组件名：副标题」，与列表行**逐字相同**（决议 D18）。

    ◷ 2026-09-17 用户裁定：原实现是「组件名 + 每台状态机一行」，那是**第二条文案路径**
    ——组件把几台机合成一句时，悬停还在逐台枚举，两处必然对不上。现在两处取同一份字符串
    （算一次、两处喂），故这里同时钉住两件事：内容相同、且**不再逐台枚举**。
    """
    from barkit.contribution.specs import StatusRowSpec

    panel = StatusPanel()
    qtbot.addWidget(panel)
    states = [
        StateSpec(plugin_id="plug", key="display", icon=IconName.BELL, title="第二屏",
                  states=(StateOption("ok", StateLevel.OK, text="第二屏已连接"),), initial="ok"),
        StateSpec(plugin_id="plug", key="notify", icon=IconName.GEAR, title="通知",
                  states=(StateOption("ok", StateLevel.OK, text="通知正常"),), initial="ok"),
    ]
    comp = ComponentSpec(plugin_id="plug", icon=IconName.GEAR, name="录屏通知")
    panel.set_components([comp], _StubContrib(states))
    panel.sync_status_rows([
        StatusRowSpec(plugin_id="plug", key="main",
                      subtitle=lambda: "iPad 已连接 · 通知正常"),
    ])
    for s in states:
        panel.update_state_snapshot(s, StateReport("ok"))

    row = panel._row_widgets["plug"][0]
    icon = panel._icon_widgets["plug"]
    assert row.subtitle_text("") == "iPad 已连接 · 通知正常"
    assert icon.toolTip() == "录屏通知：iPad 已连接 · 通知正常"
    # 逐台枚举已下线：单句（无换行），且不再出现各状态机自己的那句话
    assert "\n" not in icon.toolTip()
    assert "第二屏已连接" not in icon.toolTip()


def test_titlebar_tooltip_falls_back_to_representative_without_a_row(qtbot):
    """没有列表行的组件（如「识别」只在标题栏）：悬停提示回退代表态那一句。

    回退路径与列表行走**同一个算法**，故失联时也照样是框架的「状态失联」
    ——标题栏那一格的悬停提示不会因此变成空白。
    """
    panel = StatusPanel()
    qtbot.addWidget(panel)
    state = StateSpec(
        plugin_id="plug", key="main", icon=IconName.SCAN, title="识别",
        states=(StateOption("ok", StateLevel.OK, text="就绪"),), initial="ok",
    )
    comp = ComponentSpec(plugin_id="plug", icon=IconName.SCAN, name="识别")
    panel.set_components([comp], _StubContrib([state]))

    panel.update_state_snapshot(state, StateReport("ok"))
    assert panel._icon_widgets["plug"].toolTip() == "识别：就绪"

    # 首屏还没报告时：只报组件名，不出现一个空的「：」
    panel2 = StatusPanel()
    qtbot.addWidget(panel2)
    panel2.set_components([comp], _StubContrib([state]))
    assert panel2._icon_widgets["plug"].toolTip() == "识别"


# ---------- 显示位用户开关（契约规范 §2.4，决议 D4） ----------


def _visibility(*hidden):
    """开关桩：只把 ``hidden`` 里列出的 ``(plugin_id, slot)`` 报为「用户关掉了」。"""
    closed = set(hidden)
    return lambda plugin_id, slot: (plugin_id, slot) not in closed


def test_hidden_titlebar_icon_disappears_but_keeps_its_order_slot(qtbot):
    """关掉标题栏显示位 → 那枚图标不画；声明与顺序位都还在（重新打开原位出现）。"""
    panel = StatusPanel()
    qtbot.addWidget(panel)
    comps, states = _n_components("p", 3)

    panel.set_ui_visibility(_visibility(("p1", "titlebar")))
    panel.set_components(comps, _StubContrib(states))
    qtbot.wait(0)
    assert list(panel._icon_widgets) == ["p0", "p2"]

    # 关的只是**渲染**：声明没被改动（否则重开时位次会乱）
    assert [c.plugin_id for c in panel._all_components] == ["p0", "p1", "p2"]

    panel.set_ui_visibility(None)          # 用户改回默认 = 全可见
    assert list(panel._icon_widgets) == ["p0", "p1", "p2"]


def test_hidden_titlebar_icons_do_not_count_as_overflow(qtbot):
    """溢出簇的 ``+K`` 只算「框架放不下的」，不算「用户自己关掉的」。

    两者混在一起会把用户的意图说反：他主动关掉两枚，界面却告诉他「这里还有 2 个」。
    """
    from barkit.shell.panel import N_VIS

    panel = StatusPanel()
    qtbot.addWidget(panel)
    comps, states = _n_components("p", N_VIS + 2)

    panel.set_ui_visibility(_visibility(("p0", "titlebar"), ("p1", "titlebar")))
    panel.set_components(comps, _StubContrib(states))
    qtbot.wait(0)

    assert len(panel._icon_widgets) == N_VIS
    assert panel._overflow_widget is None      # 关掉两枚后正好装得下


def test_row_slot_can_be_hidden_and_all_hidden_is_allowed(qtbot):
    """行显示位可关；全关也不回退（决议 D4 约束 ③）。"""
    from barkit.contribution.specs import StatusRowSpec

    panel = StatusPanel()
    qtbot.addWidget(panel)
    specs = [
        StatusRowSpec(plugin_id="a", key="main"),
        StatusRowSpec(plugin_id="b", key="main"),
    ]

    panel.sync_status_rows(specs)
    assert set(panel._row_widgets) == {"a", "b"}

    panel.set_ui_visibility(_visibility(("a", "row")))
    assert set(panel._row_widgets) == {"b"}

    panel.set_ui_visibility(_visibility(("a", "row"), ("b", "row")))
    assert panel._row_widgets == {}
    assert [s.plugin_id for s in panel._row_specs] == ["a", "b"]   # 声明原样留底

    panel.set_ui_visibility(None)
    assert set(panel._row_widgets) == {"a", "b"}


def test_hidden_display_slot_does_not_stop_state_aggregation(qtbot):
    """隐藏 ≠ 停用（决议 D4 约束 ①）：三处显示位全关，状态聚合与广播照常。

    这是本批最容易做错的一条 —— 顺手把「隐藏」实现成「不订阅 / 不聚合」，
    界面看起来一样（都不显示），但托盘角标与状态新鲜度会跟着静默失效。
    """
    panel = StatusPanel()
    qtbot.addWidget(panel)
    comps, states = _n_components("p", 1)
    state = states[0]

    panel.set_ui_visibility(
        _visibility(("p0", "titlebar"), ("p0", "row"), ("p0", "tray"))
    )
    panel.set_components(comps, _StubContrib(states))
    from barkit.contribution.specs import StatusRowSpec

    panel.sync_status_rows([StatusRowSpec(plugin_id="p0", key="main")])
    assert panel._icon_widgets == {} and panel._row_widgets == {}

    emitted: list[tuple[str, str]] = []
    panel.stateLevelChanged.connect(lambda topic, level: emitted.append((topic, level)))
    panel.update_state_snapshot(state, StateReport("ok"))
    assert panel._state_reports[state.topic] == StateReport("ok")   # 报告照收
    panel._recompute_plugin("p0", emit_tray=True)
    assert emitted == [(state.topic, "ok")]                          # 照常广播给托盘


def test_reshown_slot_carries_current_state_immediately(qtbot):
    """重新打开的显示位**当场**带着当前状态，不等下一次事件。

    新造的 widget 出生时没有任何状态，靠下一次状态事件才亮 —— 最长要等一个轮询
    周期（分钟级）。用户看到的是「刚打开又是空的」，会以为开关坏了。
    """
    from barkit.contribution.specs import StatusRowSpec

    panel = StatusPanel()
    qtbot.addWidget(panel)
    comps, states = _n_components("p", 1)
    panel.set_components(comps, _StubContrib(states))
    panel.sync_status_rows([StatusRowSpec(plugin_id="p0", key="main")])

    # 先喂一条报告（此时是 warn），再把两位关掉
    warning = StateSpec(
        plugin_id="p0", key="k0", icon=IconName.GEAR, title="插件0",
        states=(
            StateOption("ok", StateLevel.OK, text="一切正常"),
            StateOption("bad", StateLevel.WARN, text="出问题了"),
        ),
        initial="ok",
        stale_after=None,
    )
    panel.set_components(comps, _StubContrib([warning]))
    panel.update_state_snapshot(warning, StateReport("bad"))

    panel.set_ui_visibility(_visibility(("p0", "titlebar"), ("p0", "row")))
    assert panel._icon_widgets == {} and panel._row_widgets == {}

    panel.set_ui_visibility(None)          # 重开：应当立刻是 warn，而不是 idle
    assert panel._icon_widgets["p0"]._level == "warn"
    assert panel._row_widgets["p0"][0]._badge_level == "warn"


def test_visibility_provider_failure_keeps_slots_visible(qtbot, caplog):
    """开关读失败按**可见**处理：一个用户态的读错误不该让整个显示位消失。"""
    panel = StatusPanel()
    qtbot.addWidget(panel)
    comps, states = _n_components("p", 3)
    panel.set_components(comps, _StubContrib(states))
    qtbot.wait(0)
    assert list(panel._icon_widgets) == ["p0", "p1", "p2"]

    def boom(plugin_id, slot):
        raise RuntimeError("读配置炸了")

    with caplog.at_level("ERROR"):
        panel.set_ui_visibility(boom)
    assert "读取显示位开关失败" in caplog.text
    assert list(panel._icon_widgets) == ["p0", "p1", "p2"]   # 三位都还在


def _footer_btns(panel, *, fixed: bool = False):
    """底部栏按钮：``fixed=False`` 取 A 区快捷按钮，``True`` 取 C 区常驻两枚。

    按 **widget** 取而不是按布局项 —— A 区末尾补了一个 stretch 把按钮压左（§5.6），
    ``count()`` 会把它也算进去。
    """
    lay = panel._footer_fixed.layout() if fixed else panel._action_layout
    return [w for w in (lay.itemAt(i).widget() for i in range(lay.count())) if w]


def test_hidden_action_slot_drops_the_quick_button_and_comes_back(qtbot):
    """关掉「快捷区域」→ A 区不画那枚按钮；重开**当场**回来（契约 §2.4 第四位）。

    按钮是**入口**不是状态：关掉它不动任何状态机（决议 D4 约束 ①）。但用户点开开关
    必须当场看得见 —— 要等下一次 ``set_contributions`` 才回来，就会被当成开关坏了。
    """
    panel = StatusPanel()
    qtbot.addWidget(panel)

    panel.set_primary_actions([
        ActionSpec(plugin_id="recognize", label="截图识别", on_click=lambda: None),
        ActionSpec(plugin_id="autotype", label="一键输入", on_click=lambda: None),
    ])
    assert [b.text() for b in _footer_btns(panel)] == ["截图识别", "一键输入"]

    panel.set_ui_visibility(_visibility(("recognize", "action")))
    assert [b.text() for b in _footer_btns(panel)] == ["一键输入"], "被关掉的那一枚仍在 A 区里"

    panel.set_ui_visibility(_visibility())          # 重开
    assert [b.text() for b in _footer_btns(panel)] == ["截图识别", "一键输入"]


def test_footer_c_zone_is_two_fixed_icon_buttons(qtbot):
    """底部栏 C 区＝设置 ＋ 退出两枚**图标**按钮，常驻、不参与 A 区那套溢出裁决。

    两件事一起守：① 字形取自设计系统（禁 emoji —— 原来是 ``⚙`` / ``?`` / ``⏻``）；
    ② 「帮助」已按用户裁定删除（它开的是与「设置」同一个窗，白占一格）。
    """
    panel = StatusPanel()
    qtbot.addWidget(panel)

    fixed = _footer_btns(panel, fixed=True)
    assert [b.toolTip() for b in fixed] == ["设置", "退出"]
    assert all(not b.text() for b in fixed), "C 区是图标态，不该有文字"
    assert all(not b.icon().isNull() for b in fixed), "图标态没字形就成了一个空方块"
    assert not hasattr(panel, "helpRequested"), "「帮助」那枚应已随重排删除"


def _glyph_tint(btn) -> tuple:
    """一枚按钮上字形的不透明像素**平均色**（换肤判据读真实位图，不读「icon 非空」）。

    「烤进键槽的主题色」这种坏法：``icon().isNull()`` 依旧是 False，只有把位图像素
    摊开看才知道颜色停在旧主题上 —— 恰好是肉眼之外没人能报的那类静默失效。
    """
    img = btn.icon().pixmap(btn.iconSize()).toImage()
    assert not img.isNull(), "取不到字形位图：图标态没画出来就没得判"
    rs = gs = bs = n = 0
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.alpha() > 0:
                rs += c.red()
                gs += c.green()
                bs += c.blue()
                n += 1
    assert n, "字形位图全透明"
    return (rs // n, gs // n, bs // n)


def test_c_zone_icon_button_rethemes_on_theme_change(qtbot):
    """C 区那两枚图标换肤必须跟随（2026-10-02 用户报：切系统明暗后它们不变色）。

    字形色是**烤**进位图的，换肤后必须有人重烤；这件事归烤色的控件自己认领
    （``_GlyphButton`` 订阅 ``theme_changed``），不该归「换肤回调记得遍历它」——
    后者漏一枚就是一片停在旧配色的死图标，还不报错。

    用 light → dark 各烤一次取真实像素：重烤接线通了才换得动，否则两次取样同色。
    """
    from PySide6.QtWidgets import QApplication

    from barkit.shell import styles

    app = QApplication.instance()
    original = styles.current_theme()
    panel = StatusPanel()
    qtbot.addWidget(panel)
    # C 区那两枚 = 底部栏里只建一次的常驻控件（A 区每次换肤都整区重建，顺带就重烤了）
    fixed = _footer_btns(panel, fixed=True)
    try:
        styles.apply_theme(app, "light")
        light = [_glyph_tint(b) for b in fixed]
        styles.apply_theme(app, "dark")
        dark = [_glyph_tint(b) for b in fixed]
    finally:
        styles.apply_theme(app, original)      # 别污染后面的用例（与 styled_panel 同法）
    assert all(a != b for a, b in zip(light, dark)), (
        f"C 区图标换肤没跟随（停在旧主题配色上）：light={light} dark={dark}"
    )


# ── 底部栏 A 区：宽度裁决（§5.6，2026-09-22 修）─────────────────────────────
#
# 这组用例必须**装上生产样式表**再量：裁决读的就是 `sizeHint()`（QSS 算出来的宽），
# 没样式表量到的是 Qt 默认控件 ——「⋯」会从 39px 涨到 80px，判据与真机分叉。
#
# 起因（用户 2026-09-22 报）：启动后五枚快捷按钮里**两枚渲染成图标、三枚渲染成文字**
# 且整排超出面板范围。根因有两条，各自对应下面一条门禁：
#   ① 旧摆法「没给图标的动作降级成『图标 ＋ 标签』」⇒ 一行里混着两种形态；
#   ② 摆法与枚数写死（≤3 标签 / 上限 5），而 320px 面板的 A 区**实得 224px** ——
#      三枚带图标的短标签合计 249px，布局把标签挤成 48px 的截断字样。

# 现实五个组件的快捷按钮素材（标签 ＋ 图标 ＋ 身份图标）：三枚 DetailEntrySpec
# **没给图标**，正是当年那三枚「渲染成文字」的（图标位已由框架按身份回落补齐）。
_REAL_QUICK = (
    ("autotype", "一键输入", IconName.KEYBOARD, IconName.KEYBOARD),
    ("recognize", "截图识别", IconName.SCAN, IconName.SCAN),
    ("route_guard", "网络切换", None, IconName.ROUTE),
    ("hosts_guard", "hosts 托管", None, IconName.DNS),
    ("ipad_tunnel", "iPad 隧道 · 运行诊断", None, IconName.TABLET),
)


@pytest.fixture
def styled_panel(qtbot):
    """带**生产样式表**的面板（用后还原，别污染后面的用例 —— 与 test_styles 同法）。"""
    from PySide6.QtWidgets import QApplication

    from barkit.shell import styles

    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(styles.build_stylesheet("dark"))
    panel = StatusPanel()
    qtbot.addWidget(panel)
    yield panel
    app.setStyleSheet("")


def _quick_acts(n, *, icon=IconName.SCAN):
    """n 枚形状统一的快捷动作（标签两字 ＋ 定字形），供宽度裁决那组用例喂料。"""
    return [
        ActionSpec(plugin_id=f"p{i}", key="main", label=f"动作{i}", icon=icon,
                   on_click=lambda: None)
        for i in range(n)
    ]


def _real_quick_acts():
    """现实五枚：两枚自带字形、三枚没有（靠身份图标回落）。"""
    return [
        ActionSpec(plugin_id=pid, key="main", label=label, icon=icon,
                   on_click=lambda: None)
        for pid, label, icon, _ in _REAL_QUICK
    ]


def _real_identity():
    return [
        ComponentSpec(plugin_id=pid, icon=identity)
        for pid, _, _, identity in _REAL_QUICK
    ]


def _quick_natural_width(panel) -> int:
    """A 区按钮集群的自然宽（含区间距）——「装不装得下」量的就是这个。"""
    return panel._action_layout.sizeHint().width()


def test_quick_buttons_keep_labels_when_they_fit(styled_panel):
    """放得下就还给用户短标签（§5.6 的「图标 ＋ 短标签」态仍然存在）。

    两枚「图标 ＋ 两字标签」合计 162px ≤ 224px —— 修宽度裁决不是把它矫枉过正成
    「永远纯图标」：少而短的场景里文字比 tooltip 好认得多。
    """
    panel = styled_panel
    panel.set_primary_actions(_quick_acts(2))

    btns = _footer_btns(panel)
    assert [b.text() for b in btns] == ["动作0", "动作1"]
    assert all(not b.icon().isNull() for b in btns), "声明了字形就该画上"


def test_quick_label_buttons_paint_their_own_icon_text_layout(styled_panel):
    """标签态快捷按钮必须走**自绘排布**的子类（2026-10-13 实机上报）。

    macOS 的 QMacStyle 忠实模仿原生 NSButton：QPushButton 内置的 icon+text 是
    「图标底边贴文字基线」（视觉即**底部对齐**），而 ``toolButtonStyle`` 对
    QPushButton 无效（getter 读成员变量，``setProperty`` 设的 dynamic property
    渲染时不读它 —— 2026-10-13 实测为恒等操作）。故 A 区标签态按钮换成自己算
    icon/text 几何的 ``_GlyphLabelButton``；这条守「别退回到裸 QPushButton」。

    自证（控制组）：工厂口 ``tool_button`` 造出来的**不是**这个子类 ⇒ 判据量的
    确是类结构，不是恒真。
    """
    from barkit.shell.ui_kit import tool_button
    from barkit.shell.ui_kit.buttons import _GlyphLabelButton

    panel = styled_panel
    panel.set_primary_actions(_quick_acts(2))
    btns = _footer_btns(panel)
    assert all(isinstance(b, _GlyphLabelButton) for b in btns), (
        "标签态快捷按钮必须是自绘 icon/text 排布的 _GlyphLabelButton"
    )
    # 三件套判据不因自绘而丢（与「还标签」那条门禁同一口径）
    assert all(b.text() for b in btns)
    assert all(not b.icon().isNull() for b in btns)
    # 控制组：工厂口（裸 QPushButton）造的不是这个子类
    assert not isinstance(tool_button("x", "pill-ghost"), _GlyphLabelButton)


def test_ghost_fg_maps_the_three_qss_states_to_tokens(monkeypatch):
    """``pill-ghost`` 前景色与 QSS 三条规则逐条对齐（两区自绘的同一个取色口）。

    2026-10-13 用户实机上报「快捷按钮的图标颜色与设置/退出按钮不一致」——
    根因是**取色不同源**：C 区（``_GlyphButton``）把 ``text1`` 烤进位图交给
    style 画（固定一色、跟不了 hover），A 区（``_GlyphLabelButton``）自绘读
    ``opt.palette.ButtonText``（静止态确为 text2 —— QSS 非 hover 规则在 polish
    时写进去了；但 ``:hover`` 档只临时应用、**不写回 palette**，hover 时不会
    跟着升 text1）。两区静止态一个 text1 一个 text2 ⇒ 用户一眼看出不一致。

    现在两区都走 :func:`_ghost_fg`：静止 ``text2`` ／ hover·pressed ``text1`` ／
    disabled ``text3``，与 ``styles.py`` 的 ``QPushButton#pill-ghost`` 三条
    规则一一对应，且每次 paintEvent **现问** tokens（不依赖 palette 同步时机，
    也不依赖位图重烤的时机）。
    """
    from PySide6.QtWidgets import QStyle, QStyleOptionButton

    from barkit.shell import styles
    from barkit.shell.ui_kit.buttons import _ghost_fg

    enabled = QStyle.StateFlag.State_Enabled
    over = QStyle.StateFlag.State_MouseOver
    sunken = QStyle.StateFlag.State_Sunken
    cases = (
        ("静止", enabled, "text2"),
        ("hover", enabled | over, "text1"),
        ("pressed", enabled | sunken, "text1"),
        ("disabled", over, "text3"),
    )
    for theme in ("light", "dark"):
        monkeypatch.setattr(styles, "current_theme", lambda th=theme: th)
        for name, flags, token in cases:
            opt = QStyleOptionButton()
            opt.state = flags
            got = _ghost_fg(opt).name().lower()
            expect = styles.TOKENS[theme][token].lower()
            assert got == expect, f"{theme} {name}: 取到 {got}，应为 {token}={expect}"


def test_footer_glyph_buttons_take_color_from_the_same_source(styled_panel):
    """C 区（设置/退出）与 A 区（快捷按钮）字形取色同源同档（2026-10-13）。

    判据分两层，缺一层都能被绕过：

    - 接线（结构）：两区都自绘，且 A 区的取色经过 ``_glyph_color`` —— 中间那层
      是图标角色分派（``None`` ＝ 跟随文字色），不是第二套色表。
    - 同源（行为）：A 区在 ghost 角色下取到的色，四种态逐一等于 ``_ghost_fg``。
      「同源同档」说的就是这件事。只搜源码字面量不够：2026-10-22 把取色下沉一层
      之后同源仍然成立，字面量却搜不到了 —— 那条门禁当时红，红的是判据不是代码。

    另守 ``_ghost_fg`` 全仓只有一处定义（不可能再长出第三套口径）。控制组：
    工厂口 ``tool_button`` 造的普通按钮不覆写 ``paintEvent`` ⇒ 判据量的确实是
    这两处自绘子类，不是恒真。
    """
    import inspect
    from pathlib import Path

    from PySide6.QtWidgets import QPushButton, QStyle, QStyleOptionButton

    from barkit.shell.panel.chrome import _GlyphButton
    from barkit.shell.ui_kit import buttons as button_mod
    from barkit.shell.ui_kit import tool_button
    from barkit.shell.ui_kit.buttons import _GlyphLabelButton

    panel = styled_panel
    panel.set_primary_actions(_quick_acts(2))

    c_btns = panel._footer_fixed.findChildren(_GlyphButton)
    assert len(c_btns) == 2, "C 区常驻两枚（设置/退出）"
    a_btns = [
        b for b in _footer_btns(panel) if isinstance(b, _GlyphLabelButton)
    ]
    assert a_btns, "A 区标签态按钮（另一条门禁守其结构，这里作同源对照）"

    # 判据一（接线）：两区都必须自绘；A 区经 _glyph_color，C 区直接问 _ghost_fg
    assert _GlyphButton.paintEvent is not QPushButton.paintEvent, (
        "_GlyphButton 必须自绘（否则又回到 style 的固定排布/固定色）"
    )
    assert "_ghost_fg" in inspect.getsource(_GlyphButton.paintEvent), (
        "_GlyphButton.paintEvent 必须走 _ghost_fg 取色"
    )
    assert _GlyphLabelButton.paintEvent is not QPushButton.paintEvent, (
        "_GlyphLabelButton 必须自绘（否则又回到 style 的固定排布/固定色）"
    )
    assert "_glyph_color" in inspect.getsource(_GlyphLabelButton.paintEvent), (
        "_GlyphLabelButton.paintEvent 必须经 _glyph_color 取色"
        "（图标角色分派在那里，绕过去就是第二套口径）"
    )

    # 判据一之二（行为）：ghost 角色下 A 区取到的色，四态逐一等于 C 区那个源。
    # 这一层才是「同源同档」的本体 —— 只比源码字面量的话，把 _glyph_color 的实现
    # 改成写死一个色也照样绿。
    for flags, label in (
        (QStyle.StateFlag.State_Enabled, "静止"),
        (QStyle.StateFlag.State_Enabled | QStyle.StateFlag.State_MouseOver, "hover"),
        (QStyle.StateFlag.State_Enabled | QStyle.StateFlag.State_Sunken, "pressed"),
        (QStyle.StateFlag.State_None, "disabled"),
    ):
        opt = QStyleOptionButton()
        opt.state = flags
        assert button_mod._glyph_color(opt, None) == button_mod._ghost_fg(opt), (
            f"ghost 角色在「{label}」态与 _ghost_fg 取色不一致 ⇒ 两区会看出色差"
        )

    # 判据二：_ghost_fg 全仓只有一处定义（同一条长相只许一处实现）
    src_root = Path(__file__).resolve().parents[1] / "src" / "barkit"
    hits = [
        p for p in src_root.rglob("*.py")
        if "def _ghost_fg" in p.read_text(encoding="utf-8")
    ]
    assert len(hits) == 1, f"_ghost_fg 应有且只有一处定义，现有：{hits}"

    # 控制组：普通 ghost 按钮不覆写 paintEvent（不经过 _ghost_fg）
    # 注：PySide6 的 built-in 方法经实例访问与经类访问不是同一个对象，
    # 故比**类属性**（type(btn).paintEvent is QPushButton.paintEvent）。
    plain = tool_button("x", "pill-ghost")
    assert type(plain).paintEvent is QPushButton.paintEvent


def test_quick_buttons_fall_back_to_icons_when_labels_do_not_fit(styled_panel):
    """标签态放不下 ⇒ **整区**转纯图标，不是逐枚挑（§5.6，2026-09-22 修）。

    三枚「图标 ＋ 两字标签」合计 249px > 224px 预算。旧判据只看枚数（≤3 就摆标签），
    于是布局把三个标签挤成 48px 的截断字样 —— 那时用户看到的就是「有的图标有的文字
    且超出范围」。宽度代替枚数做裁决，且模式**整体**切换（混排是最难认的一种）。

    纯图标态**强制** tooltip：图标态没有文字，悬停是文案的唯一回退。
    """
    panel = styled_panel
    panel.set_primary_actions(_quick_acts(3))

    btns = _footer_btns(panel)
    assert [b.text() for b in btns] == ["", "", ""], "装不下就该整区转图标"
    assert [b.toolTip() for b in btns] == ["动作0", "动作1", "动作2"]
    # 边长读**动作档**而不是写 30（2026-09-21 收口）：图标态按钮与同一排的「设置 / 退出」
    # 两枚文字按钮必须齐平 —— 方的那一枚一旦自己钉一个数，行里就会高出来 2px。真源是
    # `metrics.BTN_ACTION`（＝ 文字按钮的高度，`tool_button` 按档位取）。
    assert all(b.width() == metrics.BTN_ACTION for b in btns)
    assert all(b.height() == metrics.BTN_ACTION for b in btns), "图标态按钮必须是方的"


def test_real_five_components_render_as_one_uniform_icon_row(styled_panel):
    """用户报的那个形态：五枚快捷按钮**一律纯图标**，三枚没自带字形的按身份回落。

    这条守两件事：
    ① 形态**整齐**（旧行为：两枚图标 ＋ 三枚文字）；
    ② 没声明字形的动作**画得出图形** —— 回落链是「动作图标 → 组件身份图标 → 通用
       齿轮」（与状态行主位同一份判据）。三枚 DetailEntrySpec 没给图标，若框架不
       回落，纯图标态里它们就是三个看不见的空方块。
    """
    panel = styled_panel
    panel.set_components(_real_identity(), _StubContrib())
    panel.set_primary_actions(_real_quick_acts())

    btns = _footer_btns(panel)
    assert len(btns) == 5
    assert [b.text() for b in btns] == [""] * 5, "整区纯图标，一枚文字都不该有"
    assert all(not b.icon().isNull() for b in btns), "没自带图标的必须按身份回落，不留空"
    assert [b.toolTip() for b in btns] == [row[1] for row in _REAL_QUICK]
    # 回落链本身（字形真源是组件身份，不是框架随手挑的那枚占位齿轮）
    assert [panel._quick_glyph(a) for a in _real_quick_acts()] == [
        row[3] for row in _REAL_QUICK
    ]


def test_quick_glyph_falls_back_to_the_framework_placeholder(styled_panel):
    """两级回落都缺位（动作没图标、组件没身份）⇒ 通用齿轮，而不是空方块。"""
    from barkit.shell.panel import StatusPanel as _P

    panel = styled_panel
    ghost = ActionSpec(plugin_id="nobody", key="main", label="幽灵", on_click=lambda: None)
    panel.set_primary_actions([ghost])          # 标签态：不塞字形（有文字就不必）

    assert panel._quick_glyph(ghost) == IconName.GEAR
    assert _P._quick_glyph(panel, ghost) == IconName.GEAR, "同一个出口，别处调也一样"
    assert ghost.plugin_id not in panel._component_icons


def test_quick_area_never_exceeds_its_budget(styled_panel):
    """**总门禁**：A 区按钮集群的自然宽恒不超过它实得的宽度（§5.6）。

    实得宽度由 `_quick_area_budget` 反推（面板宽 − 底部栏边距 − C 区 − 区间距）。
    门禁不写死枚数：枚数是派生量，超了预算布局就把按钮挤扁 —— 用户看到的是截断字样。
    喂料覆盖 1–9 枚（跨度跨过「标签态装得下 / 装不下 / 要折⋯」三档）＋现实五枚。
    """
    panel = styled_panel
    budget = panel._quick_area_budget()
    over: list[str] = []
    cases = [(f"{n} 枚", _quick_acts(n)) for n in range(1, 10)]
    cases.append(("现实五枚", _real_quick_acts()))
    for name, acts in cases:
        panel.set_primary_actions(acts)
        natural = _quick_natural_width(panel)
        if natural > budget:
            over.append(f"{name}：自然 {natural}px > 预算 {budget}px")
    assert not over, "A 区超出实得宽度（按钮会被挤成截断字样）：\n  " + "\n  ".join(over)


def test_quick_area_gate_catches_the_old_count_rule(styled_panel):
    """自证（控制组 = 旧行为）：按**旧摆法**搭出那五枚，上一条门禁必须红。

    旧摆法一字不改地重放：枚数 ≤3 走「图标 ＋ 标签」，否则纯图标；而**没给图标的动作
    降级成文字按钮**（就是当年那句「纯图标态若画不出图形，那一格会变成空白」）。现实
    五枚里三枚没有图标 ⇒ 自然宽 401px ≫ 224px 预算。这条若也过，说明上面的门禁量的
    东西与这个 bug 无关（是条哑门禁）。
    """
    from barkit.shell.ui_kit import tool_button

    panel = styled_panel
    old: list = []
    for _, label, icon, _identity in _REAL_QUICK:
        if icon is not None:                     # 有图标 → 旧路径做方形图标按钮
            btn = tool_button("", "pill-ghost")
            btn.setFixedWidth(metrics.BTN_ACTION)
        else:                                    # 没图标 → 旧路径降级成文字按钮
            btn = tool_button(label, "pill-ghost")
        old.append(btn)
    total = sum(b.sizeHint().width() for b in old) + panel._action_layout.spacing() * 4
    assert total > panel._quick_area_budget(), (
        f"旧摆法的自然宽 {total}px 应当超预算 {panel._quick_area_budget()}px —— "
        "否则 `test_quick_area_never_exceeds_its_budget` 是哑门禁"
    )


def test_quick_buttons_overflow_into_a_more_button(styled_panel):
    """装不下的快捷按钮折成末端「⋯」，点开是非阻塞浮层（§5.6 溢出）。

    浮层里的项与 A 区按钮同源：选中即生效（跑该动作的 ``on_click``），不弹二次确认。

    这条刻意**不 patch** ``popup_at``（与 ``test_mode_badge_real_popup_returns_and_cancel_keeps_label``
    同款）：正因为不 patch 才顺带证明得了「弹出没开嵌套事件循环」。

    拿生产样式表跑：折几枚是**量出来的**（7 枚图标态 ＝ 5 枚 ＋ 「⋯」正好 209px ≤ 224px
    预算），没样式表量到的是 Qt 默认控件，「⋯」会胖到 80px、只折得下 4 枚 —— 那就不再
    是这条用例要说的那件事了。
    """
    from PySide6.QtTest import QTest

    panel = styled_panel
    fired: list[str] = []
    panel.set_primary_actions([
        ActionSpec(plugin_id=f"p{i}", key="main", label=f"动作{i}",
                   on_click=lambda i=i: fired.append(f"动作{i}"), icon=IconName.SCAN)
        for i in range(panel.N_QA_VIS + 2)
    ])

    btns = _footer_btns(panel)
    assert len(btns) == panel.N_QA_VIS + 1, "可见上限 ＋ 一枚「⋯」"
    more = btns[-1]
    assert more.text() == "⋯"
    assert "还有 2 个快捷按钮" in more.toolTip(), "「⋯」强制 tooltip，且要说清藏了什么"
    assert panel._quick_menu is None, "还没点开，不该有浮层"

    QTest.mouseClick(more, Qt.MouseButton.LeftButton)   # 真实点击：非阻塞 popup，不开嵌套循环

    menu = panel._quick_menu
    assert menu is not None and menu.isVisible(), "点了「⋯」却没弹出（或面板没持住它）"
    assert len(menu.items) == 2, "浮层里就该是折起来的那两枚"
    assert fired == [], "只是弹出来、还没选，谁的 on_click 都不该跑"

    menu.pick(1)                       # 选中浮层第 2 项（离屏走纯逻辑）
    assert fired == ["动作6"], "浮层里选中即生效"
    assert not menu.isVisible(), "选中后弹层要收起"


def test_component_can_declare_more_than_one_row(qtbot):
    """行数与状态机数解绑（决议 D23）：声明两行，面板上就该是两行。

    改这条之前行是按 ``plugin_id`` 索引的，第二行会覆盖第一行 —— 表现是「第二行
    根本不出现」，而且**不报错**。所以断言里最关键的是「两条都在」。
    """
    from barkit.contribution.specs import StatusRowSpec

    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.sync_status_rows([
        StatusRowSpec(plugin_id="plug", key="main", title="主行"),
        StatusRowSpec(plugin_id="plug", key="extra", title="副行"),
    ])

    rows = panel._row_widgets["plug"]
    assert len(rows) == 2, "声明两行就要画两行，不是后写的顶掉前面的"
    assert rows[0] is not rows[1], "两行必须是两个 widget，不能复用成同一个"
    assert [r._name.text() for r in rows] == ["主行", "副行"]   # 顺序 = 声明顺序


def test_every_row_of_a_component_shares_one_representative_state(qtbot):
    """一个组件的每行都刷**同一个**代表态 —— 颜色按组件聚合（契约 §2.4）。

    行的差异不来自「框架替它挑一台状态机」（行与状态机之间没有机制绑定，同名 key
    只是给人读的命名约定），而来自各行自己的标题、行级副标题回调与行内控件。
    """
    from barkit.contribution.specs import StatusRowSpec

    panel = StatusPanel()
    qtbot.addWidget(panel)
    spec = StateSpec(
        plugin_id="plug", key="main", icon=IconName.GEAR, title="插件",
        states=(StateOption("bad", StateLevel.WARN, text="出问题了"),),
        initial="bad", stale_after=None,
    )
    panel.set_components([], _StubContrib([spec]))
    panel.sync_status_rows([
        StatusRowSpec(plugin_id="plug", key="main", title="主行",
                      subtitle=lambda: "主行自己算的一句话"),
        StatusRowSpec(plugin_id="plug", key="extra", title="副行",
                      subtitle=lambda: "副行自己算的一句话"),
    ])
    panel.update_state_snapshot(spec, StateReport("bad"))

    rows = panel._row_widgets["plug"]
    assert [r._badge_level for r in rows] == ["warn", "warn"], "颜色按组件聚合，两行同色"
    assert [r.subtitle_text("") for r in rows] == [
        "主行自己算的一句话", "副行自己算的一句话",
    ], "各自的行级副标题回调各答一句"


def test_multi_row_widgets_are_reused_per_row(qtbot):
    """多行各自按 identity 复用：重同步后实例不变，也不会串到别人身上。"""
    from barkit.contribution.specs import StatusRowSpec

    panel = StatusPanel()
    qtbot.addWidget(panel)
    specs = [
        StatusRowSpec(plugin_id="plug", key="main", title="主行"),
        StatusRowSpec(plugin_id="plug", key="extra", title="副行"),
    ]
    panel.sync_status_rows(specs)
    first = list(panel._row_widgets["plug"])

    panel.sync_status_rows(specs)
    again = list(panel._row_widgets["plug"])
    assert again == first, "同一批 Spec 重同步不该重建（声明式复用的第一性问题）"
    assert [r._name.text() for r in again] == ["主行", "副行"]


def test_hiding_row_slot_hides_every_row_of_that_component(qtbot):
    """用户层的行开关是**按组件**的（配置键 ``plugins.<id>.ui.row.visible``）。

    所以一个组件声明多行时，关掉「列表行」这一位 = 整组行一起隐藏（决议 D4 ＋ D23）。
    这条把口径钉死：用户层没有「只关第几行」这一档。
    """
    from barkit.contribution.specs import StatusRowSpec

    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.sync_status_rows([
        StatusRowSpec(plugin_id="plug", key="main"),
        StatusRowSpec(plugin_id="plug", key="extra"),
        StatusRowSpec(plugin_id="other", key="main"),
    ])
    assert len(panel._row_widgets["plug"]) == 2

    panel.set_ui_visibility(_visibility(("plug", "row")))
    assert "plug" not in panel._row_widgets, "整组行一起隐藏"
    assert "other" in panel._row_widgets, "别的组件不受影响"



# ---------- 动作结果槽（`ActionResult`，契约 D32-② /《网络切换》§5.3-②） ----------
#
# 这一组钉的是那条缺口的**宿主半边**：插件做事不许碰界面，结局由面板这一行共享的结果槽
# 说出来。四个入口（整行点击 / 行内控件 / 模式徽标 / 候选菜单项）都要接上，缺哪一个，
# 那条路径上的失败就又是「点了一下，界面上什么也没发生」。


def _rows_of(panel, plugin_id="demo"):
    return panel._row_widgets[plugin_id][0]


def test_defer_result_to_host_reports_when_the_row_is_still_alive(qtbot):
    """行还活着 ⇒ 那一拍到了就把结局交出去（延迟是**时机**，不是丢弃）。"""
    from barkit.contribution import ActionOutcome, ActionResult
    from barkit.shell import ui_kit

    calls: list = []
    # 先把前面用例可能**迟到的那一拍**排空，再换掉宿主入口：本函数用打桩替掉了模块级的
    # `present_to_host`，别处一条还没跑完的回执若在这段窗口里落地，就会被记进下面的 `calls`
    # —— 判据会随**执行顺序**摇摆（本仓在「提示栈串用例」上已经吃过一次这种假红）。
    qtbot.wait(1)
    orig = ui_kit.results.present_to_host
    ui_kit.results.present_to_host = lambda w, r: calls.append(r)   # type: ignore[assignment]
    try:
        row = ui_kit.HoverRow(None)
        qtbot.addWidget(row)
        result = ActionResult(ActionOutcome.FAILED, "x")
        ui_kit.defer_result_to_host(row, result)
        assert calls == [], "必须是**下一拍**才交，不是当场（当场就是那条崩溃的写法）"
        qtbot.wait(30)
    finally:
        ui_kit.results.present_to_host = orig                      # type: ignore[assignment]
    assert calls == [result]


def test_defer_result_to_host_drops_a_dead_row(qtbot):
    """行在那一拍里已经拆掉了 ⇒ **丢弃**这次回执，不拿死壳往上找宿主。

    这是那一拍存在的理由：回调里的**业务逻辑**可能把这一行 ``deleteLater`` 掉
    （面板重画状态行就是这么干的，见 ``StatusPanel.sync_status_rows``）。回访前不判活的话，
    顺着死壳读 ``parent()`` 就是 ``RuntimeError: Internal C++ object already deleted``
    —— 从「晚一拍修掉的那个崩溃」换成一个同样难看的新报错。

    **判据落在「有没有调用 ``present_to_host``」上**，不是「有没有抛异常」：丢进 Qt 计时器
    里的回调抛异常会被 PySide6 打印后吞掉，用例照样绿 —— 那种断言在「去掉判活」时不会红，
    等于没守（去判活的实测：这条当场红，`calls` 多出一条）。
    """
    import shiboken6
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    from barkit.contribution import ActionOutcome, ActionResult
    from barkit.shell import ui_kit

    calls: list = []
    # 先把前面用例可能**迟到的那一拍**排空，再换掉宿主入口：本函数用打桩替掉了模块级的
    # `present_to_host`，别处一条还没跑完的回执若在这段窗口里落地，就会被记进下面的 `calls`
    # —— 判据会随**执行顺序**摇摆（本仓在「提示栈串用例」上已经吃过一次这种假红）。
    qtbot.wait(1)
    orig = ui_kit.results.present_to_host
    ui_kit.results.present_to_host = lambda w, r: calls.append(r)   # type: ignore[assignment]
    try:
        row = ui_kit.HoverRow(None)
        qtbot.addWidget(row)
        ui_kit.defer_result_to_host(row, ActionResult(ActionOutcome.FAILED, "x"))
        row.deleteLater()
        # 把 deleteLater **真正执行掉**（`qtbot.wait` 不够：那只是转事件循环，离屏下
        # DeferredDelete 不一定结算 —— 不 flush 的话壳还活着，这条用例就测不到病灶。
        # 同法见 `test_todo_overlay.py` 的 `test_the_controller_survives_a_dead_overlay_shell`）。
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not shiboken6.isValid(row), "测试前提：这一行此刻应当已经是死壳"
        qtbot.wait(30)          # 让那一拍跑完
    finally:
        ui_kit.results.present_to_host = orig                      # type: ignore[assignment]
    assert calls == [], "行都没了，那条回执不该再去敲宿主"


def test_synthesized_detail_entry_callbacks_say_nothing(qtbot):
    """框架自己合成的详情入口回调要返回 ``None``，不能把 ``Signal.emit`` 的 ``True`` 漏出去。

    实测 ``Signal.emit(...)`` **返回 ``True``**（不是 ``None``）。这两处回调（整行点击合成
    的那条 ＋ 底部栏快捷按钮那枚）以前就写成 ``lambda pid=...: self.detailRequested.emit(pid)``，
    在 ``on_click`` 还是 ``Callable[[], None]`` 的年代没人读返回值，所以一直没暴露；
    ``ActionCallback`` 扩宽之后宿主会读，漏出去的 ``True`` 就是一次 ``AttributeError``。

    ``as_action_result`` 是兜底（第三方插件的笔误不能把 app 弄崩），但**自己代码里留一个
    已知违约的写法、靠兜底接着，是两回事** —— 这条用例钉的就是「自己那两处已经干净」。
    去掉 ``_open_detail`` 那一层包装，两处断言各当场红一次。
    """
    from barkit.contribution.specs import DetailEntrySpec, StatusRowSpec

    class _Contrib:
        """最小贡献桩：只提供 ``set_contributions`` 要的那几个扩展点。"""

        def detail_entries(self):
            return [DetailEntrySpec(plugin_id="demo", key="main", label="诊断",
                                    build=lambda: None)]

        def status_rows(self):
            return [StatusRowSpec(plugin_id="demo", key="main")]

        def components(self):
            return []

        def states(self):
            return []

        def primary_actions(self):
            return []

    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.set_contributions(_Contrib())

    # 入口一：整行点击（框架合成的那条）
    row_cb = panel._row_on_click(StatusRowSpec(plugin_id="demo", key="main"))
    assert row_cb is not None, "声明了详情入口的行应当可点"

    # 入口二：底部栏快捷按钮（同一过程的那一枚）
    quick = [a for a in panel._primary_actions if a.plugin_id == "demo"]
    assert quick, "详情入口应当也被合成成一枚底部栏动作"

    assert row_cb() is None, "整行点击那条合成回调漏了 Signal.emit 的 True"
    assert quick[0].on_click() is None, "快捷按钮那条合成回调漏了 Signal.emit 的 True"


def test_action_result_contract_shape():
    """`ActionResult` 是**冻结**值对象，只带「结局 + 一句给用户看的话」。"""
    import dataclasses

    from barkit.contribution import ActionOutcome, ActionResult

    r = ActionResult(ActionOutcome.FAILED, "没能唤起 OpenDisplay")
    assert r.outcome is ActionOutcome.FAILED and r.text == "没能唤起 OpenDisplay"
    assert ActionResult(ActionOutcome.OK).text == ""      # 文案可省（空 = 没话说）
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.text = "改不了"                                  # 冻结值对象：不给就地改写
    # 只有三种结局：`PENDING` 是**有意推迟**的（理由写在 `ActionOutcome` 的 docstring）
    assert {o.value for o in ActionOutcome} == {"ok", "failed", "partial"}


def test_button_result_lands_in_panel_result_bar(qtbot):
    """行内按钮返回 `FAILED` ⇒ 面板结果槽当场说出那句文案，且**不自动退场**。

    行内控件走的是**当场**中继（与整行点击的「晚一拍」不同，那一处有崩溃取证要绕，见
    `test_row_click_result_is_relayed_one_tick_later`）：控件自己处理完按下就完事，
    不存在「宿主里跑业务把本控件拆掉」的那条路。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from barkit.contribution import ActionOutcome, ActionResult
    from barkit.contribution.specs import ButtonSpec, StatusRowSpec

    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.sync_status_rows([StatusRowSpec(
        plugin_id="demo", key="main", title="行",
        controls=(ButtonSpec(
            label="打开",
            on_click=lambda: ActionResult(
                ActionOutcome.FAILED, "没能唤起 OpenDisplay —— 它可能没安装。"),
        ),),
    )])

    assert panel._result_bar.text() == "", "没人做事时结果槽不该占位"

    QTest.mouseClick(_rows_of(panel)._buttons[0][0], Qt.MouseButton.LeftButton)

    assert "没能唤起 OpenDisplay" in panel._result_bar.text()
    assert panel._result_bar.isVisibleTo(panel), "结果槽没露出来（等于没说）"
    assert not panel._result_timer.isActive(), "失败必须等人看见，不许自己退场"


def test_row_click_result_is_relayed_one_tick_later(qtbot):
    """整行点击的结局**要进结果槽** —— 但晚一拍（2026-09-28 接通）。

    为什么不是当场：2026-09-18 实测「在 ``HoverRow.mousePressEvent`` 这个 Python 覆盖里
    **当场**回访宿主」会让本机离屏全量 pytest 崩在 access violation（根因未定位，已缩到
    0.6 秒两条用例的复现，见 `docs/log/2026-09-18-动作结果反馈机制.md`）。那一批的取证结论
    是「换等价写法只是掷骰子」—— 故这一轮改的是**时机**而不是写法：回访宿主从「控件自己的
    事件处理栈内」挪到「事件循环下一拍」，回访前先判活（``ui_kit.defer_result_to_host``）。
    于是业务逻辑（可能把这一行 ``deleteLater`` 掉，面板重画状态行就是那么干的）先跑完。

    **判据的承重点在中间那一句**（这条是新版最容易写错的地方）：`QTest.mouseClick` 之后
    结果槽**必须还是空的** —— 那不是「没接通」，是那一拍还没到。少了这一句，把实现改回
    「当场回访宿主」，`waitUntil` 会立刻满足、其余断言照样绿 ⇒ 一个**反向验证不会红**的
    假门禁（本轮实测踩到：去掉延迟那条用例仍然 rc=0）。有了它，改回当场就当场红。

    另一件同时守住的事：**回调本体仍是当场跑的**（用户不该为了「报结果」多等一拍），
    所以 `fired` 在 `mouseClick` 之后立刻就要是 ``[1]``。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from barkit.contribution import ActionOutcome, ActionResult
    from barkit.contribution.specs import StatusRowSpec

    fired: list[int] = []

    def _row_action():
        fired.append(1)
        return ActionResult(ActionOutcome.FAILED, "整行点击报的错")

    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.sync_status_rows([StatusRowSpec(
        plugin_id="demo", key="main", title="行", on_click=_row_action,
    )])

    QTest.mouseClick(_rows_of(panel)._name, Qt.MouseButton.LeftButton)

    assert fired == [1], "回调本体照旧当场跑（延迟的只是「结局送到哪里」）"
    assert panel._result_bar.text() == "", "这一拍还没到 —— 当场落地就是那条崩溃的写法"
    qtbot.waitUntil(lambda: panel._result_bar.text() != "", timeout=1000)
    assert "整行点击报的错" in panel._result_bar.text()
    assert not panel._result_timer.isActive(), "失败必须等人看见，不许自己退场"


def test_success_result_shows_then_expires(qtbot):
    """`OK` 短暂一现：信息本来就在状态里，留久了只剩噪音。"""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from barkit.contribution import ActionOutcome, ActionResult
    from barkit.contribution.specs import ButtonSpec, StatusRowSpec
    from barkit.shell import panel as panel_mod

    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.sync_status_rows([StatusRowSpec(
        plugin_id="demo", key="main", title="行",
        controls=(ButtonSpec(
            label="交还",
            on_click=lambda: ActionResult(
                ActionOutcome.OK, "已请求交还系统，守护下一轮执行。"),
        ),),
    )])
    QTest.mouseClick(_rows_of(panel)._buttons[0][0], Qt.MouseButton.LeftButton)

    assert "已请求交还系统" in panel._result_bar.text()
    assert panel._result_timer.isActive(), "成功那句话应当挂着退场计时"
    assert panel._result_timer.interval() == panel_mod._RESULT_AUTO_HIDE_MS

    panel._result_timer.timeout.emit()          # 等价于计时到点（不必真等 5 秒）
    assert panel._result_bar.text() == "" and not panel._result_bar.isVisibleTo(panel)


def test_action_without_result_leaves_previous_message_alone(qtbot):
    """返回 `None` ＝ 这次没话要说 ⇒ **连上一条也不撤**。

    否则「上一个动作报的失败」会被随手一个成功动作擦掉，而那正是用户唯一看得见的线索。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from barkit.contribution import ActionOutcome, ActionResult
    from barkit.contribution.specs import ButtonSpec, StatusRowSpec

    answer = {"r": ActionResult(ActionOutcome.FAILED, "写盘失败：只读文件系统")}
    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.sync_status_rows([StatusRowSpec(
        plugin_id="demo", key="main", title="行",
        controls=(ButtonSpec(label="执行", on_click=lambda: answer["r"]),),
    )])
    btn = _rows_of(panel)._buttons[0][0]

    QTest.mouseClick(btn, Qt.MouseButton.LeftButton)
    assert "写盘失败" in panel._result_bar.text()

    answer["r"] = None                          # 第二个动作：没什么要说的
    QTest.mouseClick(btn, Qt.MouseButton.LeftButton)
    assert "写盘失败" in panel._result_bar.text(), "没话说的动作把上一条提示擦掉了"


def test_switch_and_button_results_are_relayed(qtbot):
    """行内两枚控件的结局都要回宿主 —— 它们是最容易「按了没反应」的两处。"""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from barkit.contribution import ActionOutcome, ActionResult
    from barkit.contribution.specs import ButtonSpec, StatusRowSpec, SwitchSpec

    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.sync_status_rows([StatusRowSpec(
        plugin_id="demo", key="main", title="行",
        controls=(
            SwitchSpec(checked=False,
                       on_toggle=lambda: ActionResult(
                           ActionOutcome.FAILED, "操作失败：无法写入配置（只读文件系统）")),
            ButtonSpec(label="保存",
                       on_click=lambda: ActionResult(
                           ActionOutcome.PARTIAL, "已保存 2 条，第 3 条被守护拒绝")),
        ),
    )])
    row = _rows_of(panel)

    QTest.mouseClick(row._switches[0][0], Qt.MouseButton.LeftButton)
    assert "无法写入配置" in panel._result_bar.text(), "开关的结局没接上来"

    # 同一行里的第二枚控件也得各自接上（多槽下闭包晚绑定会全调到最后一个，D6 的老坑）
    QTest.mouseClick(row._buttons[0][0], Qt.MouseButton.LeftButton)
    assert "第 3 条被守护拒绝" in panel._result_bar.text(), "按钮的结局没接上来"


def test_mode_badge_result_is_relayed(qtbot):
    """模式徽标的二元切换（`on_click`）结局同样回结果槽。"""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from barkit.contribution import ActionOutcome, ActionResult
    from barkit.contribution.specs import ModeBadgeSpec, StatusRowSpec

    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.sync_status_rows([StatusRowSpec(
        plugin_id="demo", key="main", title="行",
        mode=ModeBadgeSpec(
            label="自动", accent=True,
            on_click=lambda: ActionResult(ActionOutcome.FAILED, "切模式失败：守护未安装"),
        ),
    )])
    QTest.mouseClick(_rows_of(panel)._mode, Qt.MouseButton.LeftButton)
    assert "切模式失败" in panel._result_bar.text(), "徽标切换的结局没接上来"


def test_candidate_menu_item_result_is_relayed(qtbot):
    """候选菜单项（徽标那一路）选中后的结局也要回结果槽。

    「选中即生效」意味着用户不会再有机会去发现「其实没生效」—— 这一条尤其不能哑。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from barkit.contribution import ActionOutcome, ActionResult
    from barkit.contribution.specs import MenuItem, ModeBadgeSpec, StatusRowSpec
    from barkit.shell import panel as panel_mod

    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.sync_status_rows([StatusRowSpec(
        plugin_id="demo", key="main", title="行",
        mode=ModeBadgeSpec(label="office", accent=True, items=(
            MenuItem("office", lambda: None, "办公 · 3 条路由"),
            MenuItem("home", lambda: ActionResult(
                ActionOutcome.FAILED, "配置里没有「home」这个场景，未改动任何东西。"),
                "家庭 · 1 条路由"),
        )),
    )])

    with patch.object(panel_mod.CandidateMenu, "popup_at",
                      lambda self, pos, anchor_rect=None: _fake_pick(self, pos, index=1)):
        QTest.mouseClick(_rows_of(panel)._mode, Qt.MouseButton.LeftButton)

    assert "配置里没有「home」" in panel._result_bar.text(), "候选菜单项的结局没接上来"


def test_result_bar_is_cleared_when_panel_reopens(qtbot):
    """面板重开时清干净 —— 上次那句不属于这一次（尤其失败那句）。"""
    from barkit.contribution import ActionOutcome, ActionResult
    from barkit.contribution.specs import StatusRowSpec

    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.sync_status_rows([StatusRowSpec(plugin_id="demo", key="main")])
    panel.present_action_result(ActionResult(ActionOutcome.FAILED, "上次的失败"))
    assert panel._result_bar.text() == "上次的失败"

    panel._show_at_anchor()
    assert panel._result_bar.text() == "", "面板重开还挂着上一次的结果"


# ── 面板**已收起**时的结果出口（底部栏那几枚快捷按钮）──────────────────────────
#
# 底部栏的动作按下后**面板先 hide() 自己**（它们开的是别的窗），于是结果回头时结果槽
# 已经不在屏上 —— 只说给那一行听等于没说。出口由装配器注入（`set_off_panel_feedback`），
# 面板自己不认识通知中枢。**成功那一档在面板里就拦下**（没有逐条档位可依，而且
# `ActionResult` 的契约写着「成功且状态本身看得见就别再占一行字」）。


def _off_panel_panel(styled_panel):
    """带记录出口的面板 ＋ 返回记录表。"""
    seen: list[tuple[str, str]] = []
    styled_panel.set_off_panel_feedback(
        lambda title, result: seen.append((title, result.text))
    )
    return styled_panel, seen


def test_failed_quick_action_reaches_the_off_panel_sink(styled_panel):
    """底部栏动作返回 ``FAILED`` ⇒ 面板收起 ＋ 结局走面板之外的出口。"""
    from PySide6.QtTest import QTest

    from barkit.contribution import ActionOutcome, ActionResult

    panel, seen = _off_panel_panel(styled_panel)
    fired: list[str] = []

    def _act():
        fired.append("go")
        return ActionResult(ActionOutcome.FAILED, "没能唤起 OpenDisplay")

    panel.set_primary_actions([ActionSpec(
        plugin_id="demo", key="main", label="打开窗口", icon=IconName.SCAN, on_click=_act,
    )])
    panel.show()

    QTest.mouseClick(_footer_btns(panel)[0], Qt.MouseButton.LeftButton)

    assert fired == ["go"], "回调本体照旧要跑"
    assert not panel.isVisible(), "这些动作开的是别的窗，按下就该把面板收起来"
    assert seen == [("打开窗口", "没能唤起 OpenDisplay")]
    assert panel._result_bar.text() == "", "面板都收了，结果槽写进去也没人看得见"


def test_off_panel_sink_only_gets_what_must_be_seen(styled_panel):
    """出口只收「必须被看见」的：成功（状态本身看得见）与「没话说」都拦在面板里。

    ``OK`` 拦在面板里是刻意的 —— 这条路上没有逐条档位可依（逐条档位是热键才有的），
    而一条「已执行」的系统通知比它的信息量长寿。没有这一档，五枚按钮各弹一条
    「已打开」就是新的噪音源。

    第三枚是**返回值不合契约**那一档（``lambda: True`` —— 实测 ``Signal.emit`` 就是
    返回 ``True``）：宿主要照直读 ``result.text`` 就是一次 ``AttributeError``，
    长相是「点了没反应 ＋ 日志里一句莫名其妙的错」。规整成「没话说」是唯一安全的落法。
    """
    from PySide6.QtTest import QTest

    from barkit.contribution import ActionOutcome, ActionResult

    panel, seen = _off_panel_panel(styled_panel)
    fired: list[str] = []

    def _ok():
        fired.append("ok")
        return ActionResult(ActionOutcome.OK, "已打开")

    def _none():
        fired.append("none")

    def _bool():
        fired.append("bool")
        return True

    panel.set_primary_actions([
        ActionSpec(plugin_id="ok", key="main", label="成功", icon=IconName.SCAN,
                   on_click=_ok),
        ActionSpec(plugin_id="none", key="main", label="没话说", icon=IconName.SCAN,
                   on_click=_none),
        ActionSpec(plugin_id="bool", key="main", label="漏了 return", icon=IconName.SCAN,
                   on_click=_bool),
    ])

    for btn in _footer_btns(panel)[:3]:
        QTest.mouseClick(btn, Qt.MouseButton.LeftButton)

    assert fired == ["ok", "none", "bool"], "三枚的回调都该跑（拦的是「说不说」，不是「做不做」）"
    assert seen == [], "成功 / 没话说 / 返回值不合契约，三条都不该占一条系统通知"


def test_more_menu_action_result_also_reaches_the_off_panel_sink(styled_panel):
    """「⋯」里收起来的那些走**同一个**触发口（2026-09-28 收口）。

    此前这一处的 ``on_click`` 是 ``(self.hide(), a.on_click())`` 这种「逗号表达式」——
    它返回**元组**，于是结局既到不了结果槽（契约上不成立）、也没有别的出口，等于
    面板外那几枚按钮里，「装不下被折起来」的那批是**说不了话**的。两处收口到
    ``_fire_off_panel`` 之后口径一致。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from barkit.contribution import ActionOutcome, ActionResult

    panel, seen = _off_panel_panel(styled_panel)
    panel.set_primary_actions([
        ActionSpec(
            plugin_id=f"p{i}", key="main", label=f"动作{i}", icon=IconName.SCAN,
            on_click=(lambda: ActionResult(ActionOutcome.FAILED, "这个动作没跑成")),
        )
        for i in range(panel.N_QA_VIS + 2)
    ])

    more = _footer_btns(panel)[-1]
    assert more.text() == "⋯"
    QTest.mouseClick(more, Qt.MouseButton.LeftButton)

    menu = panel._quick_menu
    assert menu is not None and len(menu.items) == 2
    menu.pick(1)

    assert seen == [("动作6", "这个动作没跑成")], "折起来的那批也要能把失败说出来"


def test_autostart_row_buttons_use_the_inline_tier(qtbot):
    """自启行那两枚按钮走**行内档**（22px），不是裸 ``QPushButton`` 的自然高（30px）。

    裸按钮不在三档里 ⇒ 同页别处 28px 的按钮旁边摆两颗 30px 的，就是用户 2026-09-22
    上报的「风格不统一」的又一处来源。行内档与状态文案这一排的高度也协调。
    """
    from barkit.shell.design import metrics

    with patch.object(autostart, "status", return_value={
        "installed": True, "running": False, "stale": False,
    }):
        row = AutostartRow()
        qtbot.addWidget(row)
    for btn in (row._install_btn, row._uninstall_btn):
        assert btn.property(metrics.TIER_PROPERTY) == "inline"
        assert btn.height() == metrics.BUTTON_HEIGHTS["inline"]


# ── 「面板可见却没有任何鼠标反馈」= 本进程不是活跃 app（2026-09-24）──────────────
#
# 判据的出处不在本仓：macOS 上 Qt 的 cocoa 插件对鼠标事件有一道**应用级**闸门 ——
# `qnsview_mouse.mm` 里 mouseMoved / mouseEntered / mouseExited / cursorUpdate 四处的
# 第一句都是 `if (!NSApp.active) return;`（tracking area 用的是 NSTrackingActiveAlways，
# 回调照样送过来、只是被这一句丢掉）。于是本进程不是活跃 app 时，窗口一个鼠标移动 /
# 进入 / 离开事件都收不到，悬停样式与 tooltip（Qt 正是靠鼠标移动投递 tooltip）**一起**
# 失效。所以「面板看得见却没反应」的第一个判据是它 —— 不是控件焦点，也不是状态刷新。


def test_panel_mouse_dead_verdict_covers_every_case():
    """六格逐格钉住：只有「可见 + 不是活跃 app」才处置，复核过就升级成留痕。

    两个阴性格是防「顺手把正常态也算故障」的：面板已收起时无论 app 活跃与否都不该动，
    以及 app 活跃时不做任何事。
    """
    from barkit.shell import macos_window as mw

    assert mw.panel_mouse_dead_verdict(True, True, False) == mw.VERDICT_OK
    assert mw.panel_mouse_dead_verdict(True, True, True) == mw.VERDICT_OK
    assert mw.panel_mouse_dead_verdict(False, False, False) == mw.VERDICT_OK
    assert mw.panel_mouse_dead_verdict(False, False, True) == mw.VERDICT_OK
    assert mw.panel_mouse_dead_verdict(True, False, False) == mw.VERDICT_REACTIVATE
    assert mw.panel_mouse_dead_verdict(True, False, True) == mw.VERDICT_STUCK


def test_app_is_active_is_false_off_native(monkeypatch):
    """非原生（离屏 / 无 PyObjC）下这条判据不适用 ⇒ 返回 False。

    它表示「读不到」，不是「确认没激活」，故调用方只在原生路径上用它（面板的闸门复核
    本身就整条在原生分支里）。
    """
    from barkit.shell import macos_window as mw

    # ⚠️ 打桩点落成 `macos_base`：`app_is_active` 的 __globals__ 在 macos_base，
    # 打在包（mw）上等于打进空气（离屏下本来就是 False，会变成一条「假绿」）。
    monkeypatch.setattr("barkit.shell.macos_window.macos_base._native_enabled", lambda: False)
    assert mw.app_is_active() is False


@pytest.mark.skipif(sys.platform != "darwin", reason="非 macOS：没有 PyObjC 原生分支")
def test_dismiss_controller_reactivates_once_when_visible_but_inactive(
    qtbot, monkeypatch, caplog
):
    """面板可见但不活跃 ⇒ 重新激活一次并安排复核；复核仍不活跃 ⇒ 只留一条能对上现象的
    日志，不反复激活。

    这条钉的是本轮的修法本身：闸门期内的失活通知被有意吞掉，而失活通知只在**跃变**时来
    ⇒ 「压根没激活成功」那种没有任何通知会到，面板会停在「可见但收不到鼠标事件」上不动。
    """
    from barkit.shell import macos_window as mw

    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.show()
    qtbot.wait(0)
    assert panel.isVisible()
    ctrl = mw.create_dismiss_controller(panel)
    if ctrl is None:
        pytest.skip("本机没有 PyObjC：原生分支不参与")
    # 打桩点落成 `macos_dismiss`：`_check_activation` 读的就是 macos_dismiss 里那份
    # 绑定副本（打在包上 ⇒ 打进空气，而这条在真机上会红、在离屏下会假绿）。
    monkeypatch.setattr("barkit.shell.macos_window.macos_dismiss.app_is_active", lambda: False)
    activates = []
    monkeypatch.setattr(
        "barkit.shell.macos_window.macos_dismiss.PanelDismissController._activate",
        lambda self: activates.append(1),
    )
    try:
        ctrl._check_activation(retried=False)
        assert activates == [1], "面板可见但不活跃 ⇒ 应重新激活一次"
        assert ctrl._recheck.isActive(), "重新激活后要安排一次复核（当场读不到结果）"
        with caplog.at_level("WARNING"):
            ctrl._check_activation(retried=True)      # 复核：仍不是活跃 app
        assert activates == [1], "复核仍不行就不该反复激活"
        assert "不是活跃 app" in caplog.text
        assert "悬停" in caplog.text and "tooltip" in caplog.text, (
            "日志要直接对上现象（悬停 / tooltip 一起没了），否则下次还得靠猜"
        )
    finally:
        ctrl._recheck.stop()


@pytest.mark.skipif(sys.platform != "darwin", reason="非 macOS：没有 PyObjC 原生分支")
def test_dismiss_controller_silent_when_app_active(qtbot, monkeypatch, caplog):
    """阴性对照：app 活跃（正常态）时既不重新激活也不出声 —— 别把正常路径吵起来。"""
    from barkit.shell import macos_window as mw

    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.show()
    qtbot.wait(0)
    ctrl = mw.create_dismiss_controller(panel)
    if ctrl is None:
        pytest.skip("本机没有 PyObjC：原生分支不参与")
    monkeypatch.setattr("barkit.shell.macos_window.macos_dismiss.app_is_active", lambda: True)
    activates = []
    monkeypatch.setattr(
        "barkit.shell.macos_window.macos_dismiss.PanelDismissController._activate",
        lambda self: activates.append(1),
    )
    try:
        with caplog.at_level("WARNING"):
            ctrl._check_activation(retried=False)
            ctrl._check_activation(retried=True)
        assert activates == [], "正常态不该重新激活"
        assert not ctrl._recheck.isActive(), "正常态不该安排复核"
        assert "不是活跃 app" not in caplog.text
    finally:
        ctrl._recheck.stop()


# ── 不活跃时自己补投鼠标位置（2026-10-04）────────────────────────────────────
#
# 上一轮走的是「重新激活 app」，真机实测那条路整条走不通（同一场景连试六次请求全部落空，
# 诊断见 `docs/log/2026-09-28-面板鼠标反馈诊断-现场与机制.md`）。这一路不指望激活成功：
# 只要面板可见、本进程不是活跃 app，就自己把鼠标位置补投给 Qt。


class _HoverSpy(QWidget):
    """只记「指针进 / 出过没有」的最小控件。"""

    def __init__(self):
        super().__init__()
        self.enters = 0
        self.leaves = 0

    def enterEvent(self, ev):
        self.enters += 1
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self.leaves += 1
        super().leaveEvent(ev)


def _hover_target(qtbot):
    """造「一个窗口 ＋ 里面一个会被指针压到的子控件」，并把它显示出来。

    必须 `show()`：没显示过的窗口没有原生句柄，补投无从谈起（量与控件的用例都栽在这上面）。

    还必须等到窗口**已暴露**（`isExposed()`）：Qt 只在窗口暴露之后才把合成鼠标移动派发下去，
    没暴露时整条链安静无声 —— 补投成功、`sendEvent` 返回 True、控件却一个事件都收不到。
    离屏平台下 `show()` 之后要等一拍才转真（真机上立即为真），所以这里轮询而不是固定等一次。
    """
    win = QWidget()
    qtbot.addWidget(win)
    win.resize(320, 200)
    child = _HoverSpy()
    child.setParent(win)
    child.setGeometry(40, 40, 120, 60)
    win.show()
    handle = win.windowHandle()
    for _ in range(50):
        qtbot.wait(10)
        if handle is not None and handle.isExposed():
            break
    return win, child


def test_should_reinject_motion_only_takes_over_while_inactive_but_visible():
    """四格逐格钉住：只有「可见 ＋ 不活跃」才接管。

    三个阴性格都在防「顺手把正常态也接管」—— app 活跃时 Qt 本来就收得到真实事件，
    再补一条就是同一件事两个来源：悬停会在两个来源之间闪，还多出一条假的鼠标移动。
    """
    from barkit.shell import macos_window as mw

    assert mw.should_reinject_motion(True, False) is True
    assert mw.should_reinject_motion(True, True) is False
    assert mw.should_reinject_motion(False, False) is False
    assert mw.should_reinject_motion(False, True) is False


def test_reinjected_motion_actually_gives_the_widget_hover(qtbot, monkeypatch):
    """承重的一条：补投出去的那条鼠标移动，Qt 真的当成悬停派发下来。

    只钉「该不该投」是不够的 —— 这条要证的是「投出去有用」：子控件要收到 Enter、
    `underMouse()` 要为真。少了它，判据可以全绿而真机上什么都没发生。
    """
    from barkit.shell import macos_window as mw

    win, child = _hover_target(qtbot)
    assert win.isVisible() and child.underMouse() is False

    relay = mw.create_hover_relay(win)
    assert relay is not None
    # 打桩点落 `macos_hover`：`_reinject` 的 __globals__ 在那里（打在包上＝打进空气）。
    # 两枚都要打：`cursor_inside_widget` 内部读的是 `macos_cursor` 那份 `cursor_pos`，
    # 只打 `macos_hover` 这一份的话，它会拿**真实**指针位置判「在不在面板内」。
    monkeypatch.setattr("barkit.shell.macos_window.macos_hover.app_is_active", lambda: False)
    monkeypatch.setattr("barkit.shell.macos_window.macos_hover.cursor_inside_widget", lambda w: True)
    monkeypatch.setattr(
        "barkit.shell.macos_window.macos_hover.cursor_pos",
        lambda: child.mapToGlobal(child.rect().center()),
    )
    relay._reinject()
    qtbot.wait(0)

    assert child.enters >= 1, "补投只设了标志位、没有真派发 Enter ⇒ 悬停样式不会重绘"
    assert child.underMouse() is True, "补投的鼠标移动没让 Qt 把指针算到这个控件上"


def test_reinjection_sends_one_last_move_when_the_cursor_leaves(qtbot, monkeypatch):
    """光标离开面板的那一下要投**最后一次**（撤悬停），此后面板外的移动一律不投。

    两个方向都要钉：离开那一下不投 ⇒ 最后悬停的那一行一直亮着；离开之后还接着投 ⇒
    光标在屏幕别处移动时，我们每一动都在给面板送事件。
    """
    from barkit.shell import macos_window as mw
    from PySide6.QtCore import QPoint

    win, child = _hover_target(qtbot)
    relay = mw.create_hover_relay(win)
    inside = [True]
    # 「在不在面板内」与「鼠标位置」要一起改：位置本身决定 Qt 会不会把 Leave 派下来，
    # 只改 `inside` 是自欺 —— 补投出去的那一条仍然落在控件上。
    pos = [child.mapToGlobal(child.rect().center())]
    monkeypatch.setattr("barkit.shell.macos_window.macos_hover.app_is_active", lambda: False)
    monkeypatch.setattr("barkit.shell.macos_window.macos_hover.cursor_pos", lambda: pos[0])
    monkeypatch.setattr(
        "barkit.shell.macos_window.macos_hover.cursor_inside_widget", lambda w: inside[0]
    )

    relay._reinject()                      # 指针进面板
    qtbot.wait(0)
    assert child.enters == 1

    inside[0] = False
    pos[0] = QPoint(1, 1)                  # 指针挪出面板
    relay._reinject()                      # 离开那一动
    qtbot.wait(0)
    assert child.leaves == 1, "离开那一下没投 ⇒ 最后悬停的那一行会一直亮着"

    relay._reinject()
    relay._reinject()                      # 已在面板外，之后每一动都不该再投
    qtbot.wait(0)
    assert child.leaves == 1, "光标已在面板外还在投 ⇒ 每一动都给面板送事件"


def test_local_monitor_hands_the_event_back(qtbot):
    """本地监视只旁听：事件必须**原样交回**。

    返回 None 会被 AppKit 当成「吃掉这条事件」—— 那会让面板连点按都收不到，比改动前更糟。
    """
    from barkit.shell import macos_window as mw

    win = QWidget()
    qtbot.addWidget(win)
    relay = mw.create_hover_relay(win)     # 故意不 show：补投那一步会因「不可见」直接返回
    assert relay is not None
    sentinel = object()

    assert relay._on_local_event(sentinel) is sentinel


class _FakeMonitorAPI:
    """替掉 `NSEvent` 的两条监视接口，只记账 —— 用例里不真挂系统监视。"""

    def __init__(self):
        self.added = []
        self.removed = []

    def addLocalMonitorForEventsMatchingMask_handler_(self, mask, handler):
        token = ("local", mask, handler)
        self.added.append(token)
        return token

    def addGlobalMonitorForEventsMatchingMask_handler_(self, mask, handler):
        token = ("global", mask, handler)
        self.added.append(token)
        return token

    def removeMonitor_(self, token):
        self.removed.append(token)


def test_hover_relay_registers_both_monitors_and_takes_them_down(qtbot, monkeypatch):
    """两条监视都要挂，且 `stop` 要撤干净；重复 `start` 不许越挂越多。

    只挂一条就是押注 ——「事件送到本 app 还是前台 app」在真机上没有定论（Qt 的 tracking area
    用 `NSTrackingActiveAlways`，回调看着像送到本 app；但那次诊断里全局监视确实也收到过别的
    事件）。押错的那一天，用户在面板上什么都看不到，而日志里一个字都没有。
    """
    from barkit.shell import macos_window as mw

    win = QWidget()
    qtbot.addWidget(win)
    relay = mw.create_hover_relay(win)
    fake = _FakeMonitorAPI()
    monkeypatch.setattr("barkit.shell.macos_window.macos_hover._native_enabled", lambda: True)
    monkeypatch.setattr("barkit.shell.macos_window.macos_hover.NSEvent", fake)

    assert mw.macos_hover._MOTION_MASK & mw.macos_hover.NSMouseMovedMask, (
        "掩码里没有「鼠标移动」这一位 ⇒ 监视挂上了也永不触发，而且不报错"
    )

    relay.start()
    assert sorted(kind for kind, _, _ in fake.added) == ["global", "local"]
    relay.start()                          # 每次显示都会调到 start
    assert len(fake.added) == 2, "重复 start 又挂了一条 ⇒ 监视会越积越多"

    relay.stop()
    assert len(fake.removed) == 2, "stop 没把监视撤干净"
    assert relay._local is None and relay._global is None


def test_dismiss_controller_owns_the_hover_relay(qtbot):
    """补投中继的起停归「失活即关闭」控制器持。

    面板可见期间要盯的四件事（激活、失活关闭、点外部关闭、不活跃时补投鼠标）起停时机完全
    相同；分成两个持有点就会出现「一个停了另一个还在跑」。
    """
    from barkit.shell import macos_window as mw

    panel = StatusPanel()
    qtbot.addWidget(panel)
    ctrl = mw.create_dismiss_controller(panel)
    if ctrl is None:
        pytest.skip("本机没有 PyObjC：原生分支不参与")
    assert ctrl._hover is not None


# ══════════════════════════════════════════════════════════════════════
# 「面板刚被打开」的通知口（框架据此发 app.panel_opened）
# ══════════════════════════════════════════════════════════════════════

def test_open_notifier_fires_once_per_open_and_not_on_close(qtbot):
    """每次「隐藏 → 显示」报一次；已显示时再 toggle 是**关闭**，不算一次打开。

    次数必须准：多报一次就是让所有按需型组件白跑一轮；少报一次就是「用户开面板看不到
    此刻的事实」，而这正是这条线存在的唯一理由。
    """
    panel = StatusPanel()
    qtbot.addWidget(panel)
    opened: list = []
    panel.set_open_notifier(lambda: opened.append(1))

    panel.toggle_at(None)            # 打开
    assert opened == [1]
    panel.toggle_at(None)            # 已显示 ⇒ 这一次是关闭
    assert opened == [1]
    panel.toggle_at(None)            # 再打开
    assert opened == [1, 1]


def test_a_failing_open_notifier_does_not_break_the_panel(qtbot):
    """订阅方出错不得让面板停在半开状态 —— 显示是用户已经按下去了的动作。

    事件分发本身按「单订阅方隔离」设计（``core/events`` 逐个 try），但这里是面板**直接
    调**回调，没有那层兜底，所以隔离必须写在调用点上。
    """
    panel = StatusPanel()
    qtbot.addWidget(panel)

    def boom() -> None:
        raise RuntimeError("订阅方炸了")

    panel.set_open_notifier(boom)
    panel.toggle_at(None)
    assert panel.isVisible(), "回调抛异常把面板的显示链打断了"


# ---------- 主标题：省略号的边界（2026-09-24 主面板实报） ----------
#
# 用户报「主标题显示不全，即便主标题没有 badge 也是显示的省略号」。查到的**不是**徽标
# 占位，而是省略判定卡在悬崖边上：``ElideLabel`` 的请求宽度是
# ``horizontalAdvance(全文) + _ELIDE_SLACK``，而 ``_elide`` 的判定预算当时写成
# ``width - _ELIDE_SLACK`` —— 两边各减一次，余量被抹平 ⇒ 标签**拿满自己请求的宽度**时，
# 判定预算恰好等于 ``horizontalAdvance``，而 ``elidedText`` 判「放得下」所需的宽度
# 可能比它多 1px（它按实际浮点宽比，``horizontalAdvance`` 把字宽取整）。
#
# 差在哪一类文案上：**ASCII 与 CJK 混排**会差（实测 12.5px/600 下「iPad 隧道」：
# advance=57、elidedText 要 58），纯中文那一档不差 —— 所以本文件里那几条用例量了一整天
# 都还是绿的，而 `ipad_tunnel` 的真实行名恰好落在差 1px 的那一类。
#
# 判据只守**一条不变量**：拿满自己请求的宽度 ⇒ 文本完整显示。它不是「结局」型断言
# （这里的结局就是用户看到的那一行），而是这条不变量本身；两条用例分别守它的两半。


def test_title_given_its_own_width_is_never_elided(qtbot):
    """标签拿满自己请求的宽度 ⇒ 文本**完整**显示（含 ASCII ＋ CJK 混排的组件名）。

    文案取 `ipad_tunnel` 的真实行名：用户看到「iPad 隧…」的那一行就是它。
    宽度不由布局给、而由本用例**直接指定**成 ``sizeHint()``：真实面板里标题正是这么被
    结算的（标题行里标题是唯一的定宽项，剩下的全归弹性空隙），直接指定可让判据与
    「面板当前多宽」解耦 —— 换屏幕 / 换字号都不会让它变得时红时绿。
    """
    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.set_components(
        [ComponentSpec(plugin_id="ipad", icon=IconName.GEAR, name="iPad 隧道")],
        _StubContrib(),
    )
    panel.sync_status_rows(
        [StatusRowSpec(plugin_id="ipad", key="main", title="iPad 隧道")]
    )
    # 必须**先 show 出来**再量：隐藏控件上即使布局改了它的几何，也不会投递重排事件，
    # ``_elide`` 压根不重算（实测宽度已经变了、文本却原样停着，用例变成假绿）。
    # 离屏 show 同样算数；``qtbot.wait(1)`` 是让布局落定（一次 processEvents 不够）。
    panel.show()
    panel.resize(panel.WIDTH, 400)
    qtbot.wait(1)

    label = panel._row_widgets["ipad"][0]._name
    assert label._full == "iPad 隧道", "用例前提：这一行的主标题就是那个混排名"

    fm = label.fontMetrics()
    assert fm.elidedText(
        label._full, Qt.TextElideMode.ElideRight, fm.horizontalAdvance(label._full)
    ) != label._full, (
        "用例前提失效：这段文案的两把尺子不再有取整差 ⇒ 本用例对坏实现也照样绿。"
        "换文案前先量一遍「elidedText 在 horizontalAdvance 处是否已截断」。"
    )
    assert label.width() == label.sizeHint().width(), (
        "用例前提：这一格是被**按请求宽度**结算的（真被压窄了就不再是这条不变量）"
    )
    assert label.text() == label._full, (
        f"拿满自己请求的宽度仍被打了省略号：{label.text()!r}"
    )


def test_a_squeezed_title_still_elides(qtbot):
    """另一条腿：**真的放不下时仍要省略**。

    没有这一条，把 ``_elide`` 改成「永远显示全文」也能让上面那条绿 —— 那会把
    「不可压的标签顶宽整页、行尾控件被裁」这个老问题放回来（这一页的横向滚动是关的）。
    """
    long_name = "一条长到这一格绝放不下的状态行名称" * 2
    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.set_components(
        [ComponentSpec(plugin_id="ipad", icon=IconName.GEAR, name=long_name)],
        _StubContrib(),
    )
    panel.sync_status_rows(
        [StatusRowSpec(plugin_id="ipad", key="main", title=long_name)]
    )
    panel.show()
    panel.resize(panel.WIDTH, 400)
    qtbot.wait(1)
    label = panel._row_widgets["ipad"][0]._name
    assert label.width() < label.sizeHint().width(), "用例前提：这一格真的被压窄了"
    assert label.text() != label._full, "挤窄了还是全文，省略号等于没做"


def test_should_close_on_deactivate_keeps_the_panel_while_the_cursor_is_on_it():
    """失活时该不该关面板 —— 四格判据（纯函数，不碰原生层）。

    用户报的现象（2026-09-27「程序启动后第一次点面板标题栏里的图标，整个面板消失」）
    走的就是这条：真机会话里 ``app_is_active()`` 反复报「面板可见但不是活跃 app」，
    那种会话里每一次前台跃变都不是用户点的，可它照样满足「可见 + 过了屏蔽窗」。
    第三问（鼠标在哪）是与激活状态无关的事实，也正是挡住这一条的那一问。
    """
    from barkit.shell import macos_window as mw

    # ① 鼠标在面板内 ⇒ 留下（用户的手还在面板上，这次失活不是他点的）
    assert mw.should_close_on_deactivate(True, True, True) is False
    # ② 鼠标在面板外 ⇒ 关（「点别处关闭」的原语义，这条不能被改掉）
    assert mw.should_close_on_deactivate(True, True, False) is True
    # ③ 显示后的屏蔽窗还没走完 ⇒ 不关（开面板那一下自身会引起激活切换）
    assert mw.should_close_on_deactivate(True, False, False) is False
    # ④ 面板本来就没显示 ⇒ 无所谓
    assert mw.should_close_on_deactivate(False, True, False) is False
    assert mw.should_close_on_deactivate(False, True, True) is False


@pytest.mark.skipif(sys.platform != "darwin", reason="非 macOS：没有 PyObjC 原生分支")
def test_dismiss_controller_keeps_the_panel_when_the_cursor_is_inside(
    qtbot, monkeypatch, caplog
):
    """失活回调的两面：鼠标在面板内 ⇒ 留下（并留一句能对上的日志）；在面板外 ⇒ 照关。

    这条钉的是「哪一问挡住的」：把 ``cursor_inside_widget`` 钉成固定值来分别驱动两格，
    所以它不依赖真实鼠标位置（离屏跑得动），也就能在 CI 上守住。
    """
    from barkit.shell import macos_window as mw

    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.show()
    qtbot.wait(0)
    ctrl = mw.create_dismiss_controller(panel)
    if ctrl is None:
        pytest.skip("本机没有 PyObjC：原生分支不参与")
    panel._can_close_on_deactivate = True      # 前提：过了显示后的屏蔽窗

    # ① 鼠标在面板内 ⇒ 留下，且要说清为什么（否则下次仍只能靠猜）
    monkeypatch.setattr("barkit.shell.macos_window.macos_dismiss.cursor_inside_widget", lambda w: True)
    with caplog.at_level("INFO"):
        ctrl.appDidResignActive_(None)
    assert panel.isVisible(), "鼠标还在面板内 ⇒ 不该关（用户正在操作它）"
    assert "鼠标还在面板内" in caplog.text

    # ② 鼠标在面板外 ⇒ 照关（「点别处 / 点菜单栏图标关闭」都靠这一支）
    caplog.clear()
    monkeypatch.setattr("barkit.shell.macos_window.macos_dismiss.cursor_inside_widget", lambda w: False)
    with caplog.at_level("INFO"):
        ctrl.appDidResignActive_(None)
    assert not panel.isVisible(), "鼠标在面板外 ⇒ 该关"
    assert "鼠标在面板外" in caplog.text


def test_cursor_on_panel_or_icon_is_about_the_cursor_not_the_event_point():
    """「这一下算不算点在面板外」的判据 —— 只认鼠标位置这一个事实（纯函数，不比原生）。

    2026-09-27：这条判据原先读全局事件自带的 ``locationInWindow()``，而那个读数的坐标系
    是一条**没验证过的注释假设**；拿没验过的空间里的一个点去比确定空间里的矩形，错了的
    表现是「点哪儿都判在外面」—— 面板一被点就消失，而两块矩形看上去都没错。现在改读
    ``cursor_pos()``（实测与系统读数一致）与 Qt 侧两块矩形，四格都钉在这里。
    """
    from PySide6.QtCore import QPoint, QRect

    from barkit.shell import macos_window as mw

    panel = QRect(800, 40, 320, 100)
    icon = QRect(955, 15, 16, 22)

    assert mw.cursor_on_panel_or_icon(QPoint(900, 80), panel, icon) is True, "面板内 ⇒ 算在里面"
    assert mw.cursor_on_panel_or_icon(QPoint(960, 20), panel, icon) is True, "图标上 ⇒ 也算"
    assert mw.cursor_on_panel_or_icon(QPoint(300, 500), panel, icon) is False, "都在外 ⇒ 不算"
    # 图标矩形取不到时**只判面板那一块**，不是「一律判在外面」
    assert mw.cursor_on_panel_or_icon(QPoint(900, 80), panel, None) is True
    assert mw.cursor_on_panel_or_icon(QPoint(300, 500), panel, None) is False


@pytest.mark.skipif(sys.platform != "darwin", reason="非 macOS：没有 PyObjC 原生分支")
def test_global_click_keeps_the_panel_while_the_cursor_is_on_it(qtbot, monkeypatch, caplog):
    """「点到面板外」那条路：鼠标在面板内 / 图标上 / 读不到 ⇒ 都不关；真在外面 ⇒ 关并留痕。

    驱动用的是被钉住的 ``mw.cursor_pos``，事件自带的那点故意给成与真实落点无关的 ``(0,0)``
    —— 结论必须只随鼠标位置变，这正是这条判据在 2026-09-27 的修法。
    """
    from PySide6.QtCore import QPoint, QRect

    from barkit.shell import macos_window as mw

    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.show()
    # 挪离原点：不然「读不到鼠标 ⇒ 当在 (0,0)」与「读到了、鼠标在左上角」这两种情形
    # 在离屏平台上分不开（窗口默认就贴着原点），那一格等于没验。
    panel.move(600, 400)
    qtbot.wait(1)
    ctrl = mw.create_dismiss_controller(panel)
    if ctrl is None:
        pytest.skip("本机没有 PyObjC：原生分支不参与")

    class _Ev:
        def locationInWindow(self):
            return (0.0, 0.0)  # 与真实落点无关，判据不该读它

    box = [panel.frameGeometry().center()]
    # ⚠️ 落成 `macos_dismiss`：`_on_global_click` 自己就直调 `cursor_pos()`（第 9 行 import
    # 进来的那份绑定副本），不走 macos_cursor 那条链 ⇒ 补在 macos_cursor 上照样打进空气。
    monkeypatch.setattr("barkit.shell.macos_window.macos_dismiss.cursor_pos", lambda: box[0])

    # ① 鼠标在面板内 ⇒ 留下（用户正在操作它）
    ctrl._on_global_click(_Ev())
    assert panel.isVisible(), "鼠标在面板内 ⇒ 不许关"

    # ② 鼠标在菜单栏图标上 ⇒ 留下（关不关交给图标自己的 toggle）
    rect = panel.frameGeometry()
    icon = QRect(rect.right() + 120, rect.top(), 16, 22)
    monkeypatch.setattr(ctrl, "_icon_rect_qt", lambda: icon)
    box[0] = icon.center()
    ctrl._on_global_click(_Ev())
    assert panel.isVisible(), "鼠标在菜单栏图标上 ⇒ 不许关"

    # ③ 鼠标位置读不到 ⇒ 也不关（判据缺一半时宁可留着，用户点一下就能关）
    box[0] = None
    ctrl._on_global_click(_Ev())
    assert panel.isVisible(), "读不到鼠标位置 ⇒ 不许拿「读不出来」当「在外面」"

    # ④ 真在外面 ⇒ 照关，且要留一行能对上的记录
    box[0] = QPoint(rect.left() - 400, rect.top() - 400)
    with caplog.at_level("INFO"):
        ctrl._on_global_click(_Ev())
    assert not panel.isVisible(), "鼠标在面板与图标之外 ⇒ 该关"
    assert "点到面板外" in caplog.text, "关掉面板必须留痕（否则下次仍只能靠猜）"
    assert "鼠标=" in caplog.text and "面板=" in caplog.text


@pytest.mark.skipif(sys.platform != "darwin", reason="非 macOS：没有 PyObjC 原生分支")
def test_hiding_the_panel_leaves_a_trace_with_the_call_site(qtbot, caplog):
    """面板每隐藏一次都留一条带调用栈的记录 —— 下次「谁关的」不必再猜。

    2026-09-27 的教训：关闭出口原先全静默，用户报「面板消失」时日志里一个字都没有，
    排查只能靠推断（前两轮就是这么推错的）。判据写在 hideEvent 这个**唯一收口**上：
    任何一条关闭路径都必须经过它，所以它留痕 ⇒ 每条路径都留痕。
    """
    panel = StatusPanel()
    qtbot.addWidget(panel)
    panel.show()
    qtbot.wait(0)

    with caplog.at_level("INFO"):
        panel.hide()
    assert "面板隐藏（收口）" in caplog.text, "隐藏没留痕 ⇒ 下次现场仍然只有一个「面板没了」"
    assert "test_panel.py" in caplog.text, (
        "记录里要带上调用点（否则知道「被关了」也说不出是谁关的）"
    )
