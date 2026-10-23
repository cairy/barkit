"""U2 §5.4 详情页：框架托管 + DetailEntrySpec 驱动 L2 整行可点性。

三条主线：
1. **分流**（2026-09-18 已改判）：ipad_tunnel 的守护安装/卸载/立即检查 + 隧道配置
   归**设置页**（低频 / 装置运维），**诊断**才是高频任务流 → 点面板整行直接进运行
   诊断窗（§5.4 / §5.5；与《网络切换》「设置页只剩装置运维」同一条判据）。
2. **可点性**：L2 整行「声明了 DetailEntrySpec 才可点」（UI 规范 §5.3）——
   未声明的行不给手型、点击不发信号。
3. **托管**：Dock 图标随详情页弹出出现、关闭后复原（§5.4，此前是 recognize
   窗口的私有实现，本轮收归到 shell/macos_window）。
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from barkit.contribution.specs import (
    DETAIL_PRESENTATION_POPOVER,
    DETAIL_PRESENTATION_WINDOW,
    DETAIL_PRESENTATIONS,
    DetailEntrySpec,
    FormSaveSpec,
    StatusRowSpec,
)
from barkit.core.config import Config
from barkit.core.plugin import PluginContext
from barkit.plugins.hosts_guard.plugin import Plugin as HostsPlugin
from barkit.shell import macos_window
from barkit.shell.detail import DetailForm, DetailHost
from barkit.shell.panel import StatusPanel
from barkit.shell.ui_kit import EditScreen, EditStack


def _footer_buttons(panel) -> list:
    """面板底部栏 A 区**按钮**（按 widget 取，跳过末尾那个 stretch）。"""
    lay = panel._action_layout
    out = []
    for i in range(lay.count()):
        w = lay.itemAt(i).widget()
        if w is not None:
            out.append(w)
    return out


# ──────────────────────────────────────────────
# L2 整行可点性（§5.3）
# ──────────────────────────────────────────────

def _row_spec(plugin_id: str = "ipad_tunnel", **kw) -> StatusRowSpec:
    return StatusRowSpec(plugin_id=plugin_id, key="main", title="iPad 隧道", **kw)


class _FakeContrib:
    """最小贡献桩：只提供 set_contributions 需要的五个扩展点。"""

    def __init__(self, entries, rows):
        self._entries, self._rows = list(entries), list(rows)

    def detail_entries(self):
        return self._entries

    def status_rows(self):
        return self._rows

    def components(self):
        return []

    def states(self):
        return []

    def primary_actions(self):
        return []


def test_row_not_clickable_without_detail_entry(qtbot):
    """未声明 DetailEntrySpec：整行不可点（无手型、点击不发出请求）。"""
    panel = StatusPanel()
    qtbot.addWidget(panel)
    requested = []
    panel.detailRequested.connect(requested.append)

    panel.set_detail_entries([])
    panel.sync_status_rows([_row_spec()], panel)

    row = panel._row_widgets["ipad_tunnel"][0]
    assert row._clickable is False
    QTest.mouseClick(row, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     QPoint(5, 5))
    assert requested == []


def test_row_click_emits_detail_request_when_declared(qtbot):
    """声明了详情入口：整行可点，点击发 detailRequested。"""
    panel = StatusPanel()
    qtbot.addWidget(panel)
    requested = []
    panel.detailRequested.connect(requested.append)

    entry = DetailEntrySpec(plugin_id="ipad_tunnel", key="main", label="iPad 隧道",
                            build=lambda: QLabel("content"))
    panel.set_detail_entries([entry])
    panel.sync_status_rows([_row_spec()], panel)

    row = panel._row_widgets["ipad_tunnel"][0]
    assert row._clickable is True
    QTest.mouseClick(row, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     QPoint(5, 5))
    assert requested == ["ipad_tunnel"]


def test_spec_on_click_wins_over_detail_entry(qtbot):
    """StatusRowSpec 自带 on_click 时优先（详情入口不抢插件自己的整行语义）。"""
    panel = StatusPanel()
    qtbot.addWidget(panel)
    fired = []
    panel.detailRequested.connect(fired.append)

    panel.set_detail_entries([
        DetailEntrySpec(plugin_id="ipad_tunnel", build=lambda: QLabel("c")),
    ])
    panel.sync_status_rows([_row_spec(on_click=lambda: fired.append("spec"))], panel)

    row = panel._row_widgets["ipad_tunnel"][0]
    QTest.mouseClick(row, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     QPoint(5, 5))
    assert fired == ["spec"]


def test_detail_entry_unlocks_both_entry_points(qtbot):
    """声明 DetailEntrySpec ＝ 两个入口同时开：整行可点，且底部栏挂一枚快捷按钮。

    两个入口触发的是**同一个过程**（发 detailRequested），差别只在用户
    怎么找到它；各自受显示位开关（row / action）单独管辖。
    """
    panel = StatusPanel()
    qtbot.addWidget(panel)
    requested = []
    panel.detailRequested.connect(requested.append)

    panel.set_contributions(_FakeContrib(
        [DetailEntrySpec(plugin_id="ipad_tunnel", key="main", label="诊断",
                         build=lambda: QLabel("c"))],
        [_row_spec()],
    ))

    # 入口一：整行可点
    row = panel._row_widgets["ipad_tunnel"][0]
    assert row._clickable is True
    QTest.mouseClick(row, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     QPoint(5, 5))
    assert requested == ["ipad_tunnel"]

    # 入口二：底部栏 A 区多了一个按钮，点击发同一个请求。
    # 取法按「按钮」而不是按「布局项」：A 区末尾补了一个 stretch 把按钮压左（§5.6），
    # `count()` 会把那个 spacer 也算进去。
    btns = _footer_buttons(panel)
    assert len(btns) == 1
    btn = btns[0]
    assert isinstance(btn, QPushButton) and btn.text() == "诊断"
    btn.click()
    assert requested == ["ipad_tunnel", "ipad_tunnel"]


def test_set_contributions_consumes_detail_entries(qtbot):
    """装配路径：set_contributions 从注册中心取详情入口并驱动整行可点性。"""
    panel = StatusPanel()
    qtbot.addWidget(panel)
    entries = [DetailEntrySpec(plugin_id="ipad_tunnel", build=lambda: QLabel("c"))]
    panel.set_contributions(_FakeContrib(entries, [_row_spec()]))
    assert panel._row_widgets["ipad_tunnel"][0]._clickable is True


# ──────────────────────────────────────────────
# 框架托管：Dock 图标显隐成对（§5.4）
# ──────────────────────────────────────────────

def test_detail_form_dock_policy_pairs(qtbot, monkeypatch):
    """弹出时切 Regular（0，Dock 图标出现）；关闭后复原 Accessory（1）。"""
    calls = []
    monkeypatch.setattr(
        macos_window, "set_activation_policy", lambda p: (calls.append(p), True)[1]
    )
    form = DetailForm("iPad 隧道", QLabel("诊断内容"))
    qtbot.addWidget(form)

    form.show()
    assert calls == [macos_window.POLICY_REGULAR]
    form.close()
    assert calls == [macos_window.POLICY_REGULAR, macos_window.POLICY_ACCESSORY]

    # 复原只做一次：再次 hide 不应重复切
    form.hide()
    assert len(calls) == 2


def test_detail_form_does_not_restore_when_switch_failed(qtbot, monkeypatch):
    """非 macOS / offscreen 下切换失败（返回 False）→ 关闭时不复原（不无谓改策略）。"""
    calls = []
    monkeypatch.setattr(
        macos_window, "set_activation_policy", lambda p: (calls.append(p), False)[1]
    )
    form = DetailForm("iPad 隧道", QLabel("内容"))
    qtbot.addWidget(form)
    form.show()
    form.close()
    assert calls == [macos_window.POLICY_REGULAR]


def test_detail_form_hosts_plugin_content(qtbot):
    """外壳只提供标题 + 完成按钮，内容原样放插件 build() 的 widget。"""
    content = QLabel("诊断内容")
    form = DetailForm("iPad 隧道", content)
    qtbot.addWidget(form)
    assert form.windowTitle() == "iPad 隧道"
    assert content.parent() is not None


# ──────────────────────────────────────────────
# 呈现形态：popover / window（决议 D26）
# ──────────────────────────────────────────────

def test_presentation_defaults_to_popover_and_keeps_fixed_width(qtbot):
    """默认形态零行为变更：字段默认 popover，壳宽与加该字段之前一模一样（固定宽）。"""
    entry = DetailEntrySpec(plugin_id="a", build=lambda: QLabel("c"))
    assert entry.presentation == DETAIL_PRESENTATION_POPOVER

    form = DetailForm("iPad 隧道", QLabel("内容"))
    qtbot.addWidget(form)
    assert form.minimumWidth() == form.maximumWidth() == DetailForm.WIDTH
    assert form.isSizeGripEnabled() is False


def test_window_presentation_is_resizable(qtbot):
    """window 形态：初宽按内容宽度给，且**开放缩放**（无上界 ＋ 右下角拖拽手柄）。"""
    form = DetailForm("网络切换", QLabel("场景列表"),
                      presentation=DETAIL_PRESENTATION_WINDOW)
    qtbot.addWidget(form)
    assert form.minimumWidth() == DetailForm.WINDOW_MIN_WIDTH
    assert form.maximumWidth() > DetailForm.WINDOW_MIN_WIDTH   # 不设上界 = 拖得动
    assert form.width() == DetailForm.WINDOW_WIDTH
    assert form.isSizeGripEnabled() is True


def test_presentation_values_are_a_closed_set():
    """取值闭集：两个合法值都收，非法值在**声明期**报错（不静默回落成浮层卡）。"""
    for ok in DETAIL_PRESENTATIONS:
        assert DetailEntrySpec(plugin_id="a", presentation=ok).presentation == ok
    with pytest.raises(ValueError, match="presentation"):
        DetailEntrySpec(plugin_id="a", presentation="modal")


# ──────────────────────────────────────────────
# 窗变矮 = 出滚动条，不许把控件压扁（2026-09-18）
# ──────────────────────────────────────────────

def _rows(n: int) -> QWidget:
    """造一份「自然高度由行数定、且压不动」的内容（输入框有自己的最小高度）。

    用真控件而不是 QLabel：挤压正是从「控件的 `minimumSizeHint` 被无视」开始的，
    用没有最小高度的东西量不出来。
    """
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    for i in range(n):
        lay.addWidget(QLineEdit(f"row {i}", box))
    return box


def test_window_opens_at_the_content_natural_height(qtbot):
    """开窗**不许**自带滚动条：初高按内容自然高度算。

    滚动区只报内容的「最小」尺寸，拿窗的 `sizeHint` 当初高就会一开窗就带条 ——
    屏幕明明装得下，却先让人滚一屏，等于把新缺口换成另一个坏。
    """
    content = _rows(20)                     # 自然高要**明显高过窗口下限**，否则两种算法同值、量不出差别
    form = DetailForm("网络切换", content, presentation=DETAIL_PRESENTATION_WINDOW)
    qtbot.addWidget(form)
    form.show()
    qtbot.waitExposed(form)
    # 刚 show 完滚动区还没重算显隐（此刻 `maximum()` 已经是 0，`isVisible()` 仍是 True）
    # —— 等一拍再读才是真值。真机上那是 macOS 的 overlay 条一闪，不占宽度。
    qtbot.wait(1)

    assert content.height() >= content.sizeHint().height(), "内容被压扁了"
    assert form._content_scroll.verticalScrollBar().isVisible() is False, "放得下就不该有滚动条"


def test_window_shrinking_scrolls_instead_of_squeezing(qtbot):
    """窗缩到比内容矮 → 走滚动条，内容保持自然高度（控件一个都不许被压扁）。

    真机现象：窗可拖到 320px 高，而插件内容有它自己的自然高度 ⇒ 之前 Qt 把内容
    一路压缩（实测输入框从 21px 掉到 9px，字被挤掉一半），**不报错、只是界面变形**。
    """
    content = _rows(20)
    form = DetailForm("网络切换", content, presentation=DETAIL_PRESENTATION_WINDOW)
    qtbot.addWidget(form)
    form.show()
    qtbot.waitExposed(form)
    wants = {w: w.sizeHint().height() for w in content.findChildren(QLineEdit)}

    form.resize(form.width(), DetailForm.WINDOW_MIN_HEIGHT)
    qtbot.wait(1)

    # 先断言**没被压扁**（这才是用户看到的现象），再断言有滚动条：两处都要，
    # 顺序也要紧 —— 少了滚动区时，先失败的就是这一条。
    for w, want in wants.items():
        assert w.height() == want, f"控件被压扁：{w.height()} < {want}"
    assert content.height() >= content.sizeHint().height()
    assert form._content_scroll.verticalScrollBar().isVisible() is True, "矮窗要给滚动路径"


def test_popover_content_is_not_wrapped_in_a_scroll_area(qtbot):
    """浮层卡一字不变：它按内容 `adjustSize()`，本来就不会挤出滚动 —— 不许跟着包一层。"""
    content = _rows(3)
    form = DetailForm("iPad 隧道", content)
    qtbot.addWidget(form)
    assert form._content_scroll is None
    assert content.findChildren(QScrollArea) == []
    assert form.minimumWidth() == form.maximumWidth() == DetailForm.WIDTH


# ──────────────────────────────────────────────
# 窗体的「保存」与未保存关窗（决议 D35）
# ──────────────────────────────────────────────

def _saver(dirty=True, ok=True, text=""):
    """造一份能读回调用痕迹的 `FormSaveSpec`（`state` 里能看到调了几次）。"""
    state = {"commit": 0, "dirty": 0, "dirty_now": dirty}

    def _commit():
        state["commit"] += 1
        return ok, text

    def _dirty():
        state["dirty"] += 1
        return state["dirty_now"]

    return FormSaveSpec(commit=_commit, dirty=_dirty), state


def _save_button(form):
    from PySide6.QtWidgets import QPushButton

    for b in form.findChildren(QPushButton):
        if b.text() == "保存":
            return b
    return None


def test_save_only_exists_when_declared(qtbot):
    """没声明 `save` 的窗体一字不变：页脚只有「完成」，没有保存键、没有多余反馈槽。"""
    bare = DetailForm("iPad 隧道", QLabel("内容"), presentation=DETAIL_PRESENTATION_WINDOW)
    qtbot.addWidget(bare)
    assert _save_button(bare) is None
    assert bare._save_btn is None
    assert bare._fb is None
    # 声明了才有
    spec, _ = _saver()
    form = DetailForm("网络切换", QLabel("场景"), presentation=DETAIL_PRESENTATION_WINDOW,
                      save=spec)
    qtbot.addWidget(form)
    assert _save_button(form) is not None
    assert form._fb is not None


def test_window_width_defaults_and_can_be_declared(qtbot):
    """window 形态的初宽：默认 ``WINDOW_WIDTH``，声明了 ``width`` 就用声明的那个。

    宿主（多列表格、工具条挤不下）自己知道该多宽 —— 让它在声明里说，而不是靠用户
    每次开窗自己拖。
    """
    plain = DetailForm("A", QLabel("内容"), presentation=DETAIL_PRESENTATION_WINDOW)
    qtbot.addWidget(plain)
    assert plain.width() == DetailForm.WINDOW_WIDTH

    wide = DetailForm("B", QLabel("内容"), presentation=DETAIL_PRESENTATION_WINDOW,
                      width=780)
    qtbot.addWidget(wide)
    assert wide._window_width == 780
    assert wide.width() > DetailForm.WINDOW_WIDTH   # 未 show ⇒ 还没被夹进屏


def test_width_requires_the_window_presentation():
    """``width`` 只对 window 形态有意义（浮层卡固定宽）—— 填了不生效最坏，故声明期报错。

    与 ``save`` 同一条纪律：声明组合非法要当场说出来，不静默忽略。
    """
    with pytest.raises(ValueError, match="width"):
        DetailEntrySpec(plugin_id="a", build=lambda: QLabel("c"), width=780)
    with pytest.raises(ValueError, match="width"):
        DetailEntrySpec(plugin_id="a", build=lambda: QLabel("c"),
                        presentation=DETAIL_PRESENTATION_WINDOW, width=0)
    entry = DetailEntrySpec(plugin_id="a", build=lambda: QLabel("c"),
                            presentation=DETAIL_PRESENTATION_WINDOW, width=780)
    assert entry.width == 780


def test_host_passes_declared_width_through(qtbot):
    """托管器把声明的初宽交给壳：声明 780 开出来就是 780（不是框架的默认值）。"""
    entry = DetailEntrySpec(plugin_id="a", build=lambda: QLabel("c"),
                            presentation=DETAIL_PRESENTATION_WINDOW, width=780)
    host = DetailHost(_HostRegistry([entry]))
    assert host.open("a") is True
    assert host._form._window_width == 780
    # 实际宽度可能被 ``show_at`` 夹进所在屏（离屏那块屏只有 800 宽 ⇒ 780 → 752）。
    # 判据取「明显宽于默认初宽」，而不是某个定值 —— 夹宽是给小屏兜底的另一条规则。
    assert host._form.width() > DetailForm.WINDOW_WIDTH
    host.close()


def test_save_requires_the_window_presentation():
    """`save` 只对 window 形态有意义 —— 填了不生效是最坏的一种「成功」，故声明期报错。"""
    spec, _ = _saver()
    with pytest.raises(ValueError, match="save"):
        DetailEntrySpec(plugin_id="a", build=lambda: QLabel("c"), save=spec)
    # 显式给 window 就收
    entry = DetailEntrySpec(plugin_id="a", build=lambda: QLabel("c"),
                            presentation=DETAIL_PRESENTATION_WINDOW, save=spec)
    assert entry.save is spec


def test_window_presentation_is_a_native_titled_window(qtbot):
    """window 形态走原生标题栏：不要无边框、不要透明底（关闭键归系统）。

    改前它也是 frameless 浮层卡 —— 那种长相**没有关闭键**，而外面又挂着「点外部即关」
    的浮层语义，两个都不像用户见过的编辑器窗口（识别窗口就是原生标题栏）。
    """
    spec, _ = _saver()
    form = DetailForm("网络切换", QLabel("场景"), presentation=DETAIL_PRESENTATION_WINDOW,
                      save=spec)
    qtbot.addWidget(form)
    assert not (form.windowFlags() & Qt.WindowType.FramelessWindowHint), "要原生标题栏"
    assert form.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground) is False
    assert form._dismiss is None, "独立窗不做「点外部即关」（那是浮层卡的行为）"

    pop = DetailForm("iPad 隧道", QLabel("内容"))
    qtbot.addWidget(pop)
    assert pop.windowFlags() & Qt.WindowType.FramelessWindowHint, "浮层卡一字不变"
    assert pop._dismiss is not None


def test_window_presentation_is_centered_on_the_anchor_screen(qtbot):
    """居中在**触发它的那块屏**上（多屏下开到主屏 = 要用户自己把窗拖过来）。"""
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QGuiApplication

    from barkit.shell.placement import Anchor

    form = DetailForm("网络切换", QLabel("场景"), presentation=DETAIL_PRESENTATION_WINDOW)
    qtbot.addWidget(form)
    screen = QGuiApplication.primaryScreen()
    assert screen is not None
    # 锚是「矩形 ＋ 它那块屏」一对（见 placement.Anchor）：只给矩形、认不出屏的锚没有
    # 意义 —— 那正是此前把窗口夹到屏幕边沿的那半份输入。
    anchor = Anchor.of_rect(QRect(0, 0, 24, 24))   # 菜单栏左上角那枚图标
    assert anchor is not None
    form.show_at(anchor)
    center = form.frameGeometry().center()
    want = screen.availableGeometry().center()
    assert abs(center.x() - want.x()) <= 2 and abs(center.y() - want.y()) <= 2
    form.force_close()


def test_save_button_writes_and_reports(qtbot):
    """点「保存」= 跑一次 commit，结果说到页脚那条行内槽里（成功 / 失败各一句）。"""
    spec, state = _saver(ok=True)
    form = DetailForm("网络切换", QLabel("场景"), presentation=DETAIL_PRESENTATION_WINDOW,
                      save=spec)
    qtbot.addWidget(form)
    _save_button(form).click()
    assert state["commit"] == 1
    assert form._fb.text() == "已保存。", "成功也有回执（否则用户不知道点没点上）"

    fail, _ = _saver(ok=False, text="profiles[0].ifaces 必须是非空数组")
    form2 = DetailForm("网络切换", QLabel("场景"), presentation=DETAIL_PRESENTATION_WINDOW,
                       save=fail)
    qtbot.addWidget(form2)
    assert form2.save_now() is False
    assert "ifaces" in form2._fb.text()


def test_commit_exception_is_reported_not_swallowed(qtbot):
    """组件的 commit 抛异常不许变成「点了没反应」：笼住并说明。"""
    def _boom():
        raise RuntimeError("磁盘满")

    form = DetailForm("网络切换", QLabel("场景"), presentation=DETAIL_PRESENTATION_WINDOW,
                      save=FormSaveSpec(commit=_boom, dirty=lambda: False))
    qtbot.addWidget(form)
    assert form.save_now() is False
    assert "磁盘满" in form._fb.text()


def test_closing_with_unsaved_changes_asks_first(qtbot):
    """有没保存的改动就关窗 → **不关**，问在窗内；两个出路都不是空操作。"""
    spec, state = _saver(ok=True)
    form = DetailForm("网络切换", QLabel("场景"), presentation=DETAIL_PRESENTATION_WINDOW,
                      save=spec)
    qtbot.addWidget(form)
    form.show()

    form.close()                              # 相当于点「完成」或按关闭键
    assert form.isVisible(), "有改动 ⇒ 不许静默关掉（那正是「以为改过了」的老陷阱）"
    assert form._fb.is_pending()
    assert form._fb.text() == "有还没保存的改动"
    assert form._fb.cancel_text() == "放弃改动", "第二个出路要说清它会丢掉什么"

    form._fb.accept()                         # 「保存并关闭」
    assert state["commit"] == 1
    assert form.isVisible() is False


def test_closing_after_a_failed_save_stays_open(qtbot):
    """存不下去就留在窗内（红字说明卡在哪），不许「关了但没存」。"""
    spec, state = _saver(ok=False, text="场景名重复：「office」")
    form = DetailForm("网络切换", QLabel("场景"), presentation=DETAIL_PRESENTATION_WINDOW,
                      save=spec)
    qtbot.addWidget(form)
    form.show()

    form.close()
    form._fb.accept()
    assert form.isVisible(), "保存失败 ⇒ 不许关（否则改动就这么没了）"
    assert "重复" in form._fb.text()


def test_discarding_closes_without_writing(qtbot):
    """「放弃改动」= 明确丢掉并关闭（要有路可走，不然用户只能强杀）。"""
    spec, state = _saver()
    form = DetailForm("网络切换", QLabel("场景"), presentation=DETAIL_PRESENTATION_WINDOW,
                      save=spec)
    qtbot.addWidget(form)
    form.show()

    form.close()
    form._fb.cancel()
    assert form.isVisible() is False
    assert state["commit"] == 0, "「放弃」一个字节都不写"


def test_clean_form_closes_without_asking(qtbot):
    """没有改动就直接关（干净时多问一句就是噪声）。"""
    spec, _ = _saver(dirty=False)
    form = DetailForm("网络切换", QLabel("场景"), presentation=DETAIL_PRESENTATION_WINDOW,
                      save=spec)
    qtbot.addWidget(form)
    form.show()
    form.close()
    assert form.isVisible() is False
    assert form._fb.is_pending() is False


def test_framework_close_never_blocks(qtbot):
    """框架自己关（换一个详情页 / 退出）**不拦** —— 拦了会让「点另一行没反应」。"""
    spec, _ = _saver()
    entry = DetailEntrySpec(plugin_id="a", build=lambda: QLabel("c"),
                            presentation=DETAIL_PRESENTATION_WINDOW, save=spec)
    host = DetailHost(_HostRegistry([entry]))
    assert host.open("a") is True
    form = host._form
    assert form.isVisible() is True
    host.close()                              # 用户看不见这个动作，问他也无从答起
    assert form.isVisible() is False
    assert host._form is None


# ──────────────────────────────────────────────
# DetailHost：按 plugin_id 开页
# ──────────────────────────────────────────────

class _HostRegistry:
    def __init__(self, entries):
        self._entries = list(entries)

    def detail_entries(self):
        return self._entries


def test_host_opens_declared_entry(qtbot):
    built = []
    entry = DetailEntrySpec(
        plugin_id="ipad_tunnel", label="iPad 隧道",
        build=lambda: built.append(1) or QLabel("c"),
    )
    host = DetailHost(_HostRegistry([entry]))
    assert host.available("ipad_tunnel") is True
    assert host.open("ipad_tunnel") is True
    assert built == [1]
    host.close()


def test_host_ignores_undeclared_or_unbuildable(qtbot):
    host = DetailHost(_HostRegistry([
        DetailEntrySpec(plugin_id="a", build=lambda: QLabel("c")),
    ]))
    assert host.available("b") is False
    assert host.open("b") is False
    # 声明了但 build 为空（如只占位）→ 同样拒绝，不留一个空壳窗口
    host2 = DetailHost(_HostRegistry([DetailEntrySpec(plugin_id="a", build=None)]))
    assert host2.available("a") is False
    assert host2.open("a") is False


def test_host_passes_presentation_through(qtbot):
    """托管器按声明的形态开壳：声明 window 开出来的就是可缩放的窗。"""
    entry = DetailEntrySpec(plugin_id="a", build=lambda: QLabel("c"),
                            presentation=DETAIL_PRESENTATION_WINDOW)
    host = DetailHost(_HostRegistry([entry]))
    assert host.open("a") is True
    assert host._form.isSizeGripEnabled() is True
    assert host._form.maximumWidth() > host._form.minimumWidth()
    host.close()


def test_plugin_open_detail_asks_the_host_with_its_own_id():
    """``Plugin.open_detail`` 是 S18 那条出口的组件侧包装：填自己的 id、只回报成败。

    ctx 是全插件共享单例、拿不到调用者身份（与 ``mark_dirty`` / ``wake_service``
    同款），故回调签名带 id。未接线时返回 False —— 调用方据此降级（例如
    ``recognize`` 退回独立窗口壳），**不是错误**。
    """
    from barkit.core.plugin import Plugin, PluginContext

    class _P(Plugin):
        id = "recognize"
        name = "截图识别"

    p = _P(PluginContext(config=None))
    assert p.open_detail() is False          # 框架没接线

    seen: list[str] = []
    p.ctx.open_detail = lambda pid: seen.append(pid) or True
    assert p.open_detail() is True
    assert seen == ["recognize"]


def test_host_reuses_the_same_content_for_the_same_plugin(qtbot):
    """同一插件再请求一次 ⇒ **复用**，不重建内容面（S13）。

    这是「两个入口各开一份内容面」的解法：面板整行、快捷按钮、插件自己
    （``Plugin.open_detail``）现在指向同一份内容面。改之前这里无条件 close 再重建
    ⇒ 第二个入口一按，第一个入口里**还没保存的改动**当场蒸发。
    """
    built = []
    entry = DetailEntrySpec(
        plugin_id="a", build=lambda: built.append(1) or QLabel("A"),
    )
    host = DetailHost(_HostRegistry([entry]))
    qtbot.addWidget(QLabel())           # 保证有 QApplication
    assert host.open("a") is True
    qtbot.wait(1)
    first = host._form
    assert host.open("a") is True
    assert host._form is first          # 同一扇窗
    assert built == [1]                 # 内容面**没有**再建一次
    host.close()


def test_host_reuses_single_form(qtbot):
    """详情页是单例语义：再开一个插件前先关掉上一个。"""
    entry_a = DetailEntrySpec(plugin_id="a", build=lambda: QLabel("A"))
    entry_b = DetailEntrySpec(plugin_id="b", build=lambda: QLabel("B"))
    host = DetailHost(_HostRegistry([entry_a, entry_b]))
    assert host.open("a") is True
    first = host._form
    assert host.open("b") is True
    assert host._form is not first
    assert first.isVisible() is False
    host.close()


# ──────────────────────────────────────────────
# ipad_tunnel 分流：详情页 vs 设置页
# ──────────────────────────────────────────────

def _ipad_plugin(tmp_path, monkeypatch):
    """构造 ipad_tunnel 插件（不真正起桥接线程、不碰真实守护）。"""
    from barkit.core.config import Config
    from barkit.core.plugin import PluginContext
    from barkit.plugins.ipad_tunnel.plugin import Plugin
    from barkit.capabilities import helper_client as daemon

    # 长驻服务（中继/探针）由框架按声明托管；此处不 start 插件，故无需打桩桥接。
    monkeypatch.setattr(daemon, "status", lambda: {
        "installed": True, "running": True,
        "version": "2.0", "expected": "2.0", "outdated": False,
    })
    user = tmp_path / "config.yaml"
    user.write_text("plugins:\n  ipad_tunnel:\n    enabled: true\n", encoding="utf-8")
    return Plugin(PluginContext(config=Config(user_path=user), events=None))


def _button_texts(widget: QWidget) -> list[str]:
    return [b.text() for b in widget.findChildren(QPushButton)]


def test_ipad_ops_live_in_settings_and_detail_is_diag_only(tmp_path, monkeypatch, qtbot):
    """分流（2026-09-18 重构，2026-09-28 归位）：装置运维归设置页**页尾框架区**；详情页只剩诊断。

    与《网络切换》同一条判据：安装 / 卸载 / 立即检查是「配一次就不再动」的装置动作，
    诊断才是每次都要跑的高频任务流（§5.4），于是「点面板整行」与「运行诊断」合成一个
    动作 —— 中间不再隔一层「先选要做什么」。

    ⚠️ 那三枚自 2026-09-28 起由框架按**声明**注入到设置页的**页尾框架区**，
    ``plugin._build_settings_page()``（本页自己那部分）里已经没有它们；「装进设置页」
    这一半由框架区的用例守（``tests/test_settings.py``）。
    """
    plugin = _ipad_plugin(tmp_path, monkeypatch)

    settings = plugin._build_settings_page()
    qtbot.addWidget(settings)
    settings_btns = _button_texts(settings)
    assert not {"安装守护", "卸载守护", "立即检查"} & set(settings_btns), (
        settings_btns, "装置运维已搬去页尾框架区，本页不再自己画"
    )
    assert "打开隧道配置" not in settings_btns, "隧道配置已内联进本页，不再开第二个窗"

    detail = plugin.detail_entry().build()
    qtbot.addWidget(detail)
    detail_btns = _button_texts(detail)
    assert "运行诊断" in detail_btns, detail_btns
    # 装置运维一个都不许留在这一页（页内那两枚是 FeedbackBar 自带的确认/取消，常态隐藏）
    assert not {"安装守护", "卸载守护", "立即检查"} & set(detail_btns), detail_btns


def test_ipad_detail_entry_is_declared(tmp_path, monkeypatch):
    """面板整行可点的前提：detail_entry() 里确实声明了 DetailEntrySpec。"""
    plugin = _ipad_plugin(tmp_path, monkeypatch)
    entry = plugin.detail_entry()
    assert entry is not None
    assert entry.build is not None


# 守护状态分支（未装 / 过期 ⇒ 「重装守护」出现与否）已随装置运维卡搬去**页尾框架区**，
# 判据现落在 `tests/test_settings.py`（`build_daemon_ops` 的渲染 ＋ 框架按声明注入那两条）。


# ──────────────────────────────────────────────
# 动作行单一化：编辑屏被详情窗托管（2026-10-16）
# ──────────────────────────────────────────────
#
# 用户上报「编辑按钮与窗体组件按钮两行叠层」：窗体页脚有一行（「完成」），编辑屏自己又
# 画一行（「删除 …… 确定」），两行各夹各的反馈条。修法是**谁提供窗体壳、谁提供动作行**
# —— 屏把整条动作行交给窗体，由窗体页脚拼成一行。
#
# 判据不量像素而量**归属**：编辑屏那几枚动作的祖先必须是窗体页脚那一行，而编辑屏自己
# 那条底栏必须不在版面上。位置好不好看是目视的事，「屏上出现了第二行」不是。


def _editor_content() -> "tuple[QWidget, EditStack, list[EditScreen]]":
    """造一份「列表屏 ＋ 两块编辑屏」的内容面（与 hosts 管理窗同款结构）。

    两块屏是**必须**的：``QStackedWidget`` 默认显示第一页 ⇒ 「台上是列表屏时页脚也挂着
    第一页那行按钮」这个错，只有两块屏（或一块屏 ＋ 换屏）才暴露得出来。
    """
    content = QWidget()
    lay = QVBoxLayout(content)
    stack = EditStack(QWidget(), content)
    edits: list[EditScreen] = []
    for name in ("表单页", "导入页"):
        edit = EditScreen(save_label="确定")
        edit.add_destructive(f"删除{name}", lambda: None)
        edit.add_action("⋯ 更多", lambda: None, tier="pill-ghost")
        edit.set_hint(f"{name}的提示")
        stack.push(edit)
        edits.append(edit)
    stack.back()
    lay.addWidget(stack)
    return content, stack, edits


def _detail_window(qtbot, content: QWidget) -> DetailForm:
    form = DetailForm("测试详情", content, presentation=DETAIL_PRESENTATION_WINDOW)
    qtbot.addWidget(form)
    form.show()
    QApplication.processEvents()
    return form


def test_the_editor_action_bar_is_adopted_by_the_form_footer(qtbot):
    """编辑屏在台上时：它的动作落在**窗体页脚那一行**，编辑屏自己那条底栏不在版面上。

    另外钉住 ``save_button`` 交出之后**仍可取且可见** —— 快捷键那屏靠它按脏标记灰亮，
    托管后取不到就是那屏静默失去灰亮能力（一条判据守两件事：它们是同一次改动的两端）。
    """
    content, stack, edits = _editor_content()
    form = _detail_window(qtbot, content)
    stack.push(edits[0])
    QApplication.processEvents()

    row = form._row_stack.currentWidget()
    assert row.isAncestorOf(edits[0].save_button), "保存键没落进窗体页脚那一行"
    assert row.isAncestorOf(edits[0].feedback), "反馈槽没跟着动作行一起交出去"
    assert not edits[0].shell().footer_widget().isVisible(), (
        "编辑屏自己那条底栏还在版面上 ⇒ 动作行搬了但没搬干净，还是两行"
    )
    assert edits[0].save_button.isVisible(), "托管后保存键不见了（它必须仍可选可见）"
    assert edits[0].feedback is form._fb, "本窗那条反馈槽不是台上这屏交出的那条"


def test_the_footer_shows_no_editor_actions_while_the_list_screen_is_on_stage(qtbot):
    """台上是列表屏时，页脚**不显示编辑屏的动作**。

    ``QStackedWidget`` 默认显示第一页 ⇒ 不在上屏时若不显式收起来，打开详情页第一眼看到
    的列表屏底下就挂着一排编辑屏的按钮（这是探针实测到的那个错）。
    """
    content, _stack, _edits = _editor_content()
    form = _detail_window(qtbot, content)

    assert not form._row_stack.isVisible(), (
        "停在列表屏时页脚还挂着编辑屏那一行（QStackedWidget 默认显示了第一页）"
    )
    visible = [b.text() for b in form.findChildren(QPushButton) if b.isVisible()]
    assert visible == ["完成"], f"列表屏下页脚只该有「完成」，实际 {visible}"


def test_the_footer_row_follows_the_screen_on_stage(qtbot):
    """两块编辑屏换着上屏时页脚那一行跟着换（一个详情窗只有一处动作位）。"""
    content, stack, edits = _editor_content()
    form = _detail_window(qtbot, content)

    stack.push(edits[0])
    QApplication.processEvents()
    row = form._row_stack.currentWidget()
    assert row.isAncestorOf(edits[0].save_button)
    assert not row.isAncestorOf(edits[1].save_button)

    stack.push(edits[1])
    QApplication.processEvents()
    row = form._row_stack.currentWidget()
    assert row.isAncestorOf(edits[1].save_button), "换到导入页后页脚还是表单页那一行"
    assert form._fb is edits[1].feedback, "反馈槽没跟着换屏（说的话会落到看不见的那条）"

    stack.back()
    QApplication.processEvents()
    assert not form._row_stack.isVisible(), "回列表后页脚那行没收起来"


# ──────────────────────────────────────────────
# 头尾固定：矮窗下返回键与确定键仍在可视区（2026-10-16）
# ──────────────────────────────────────────────


def _hosts_detail_form(qtbot, tmp_path) -> DetailForm:
    """hosts 管理窗的**真实**详情页（框架按 ``DetailEntrySpec`` 装出来的那一份）。

    用真实插件、不用自造内容面：这一页「编辑屏之外那几样」（顶部说明 ＋ 页边距）正是
    「内容面会不会超出视口」的决定因素 —— 自造的替身量不出真实行为（2026-10-16 试过，
    少了它们时外层那层滚动区根本不会滚，判据成了空转的）。
    """
    user = tmp_path / "hosts.yaml"
    user.write_text("plugins:\n  hosts_guard:\n    enabled: true\n", encoding="utf-8")
    plugin = HostsPlugin(PluginContext(config=Config(user_path=user), events=None))
    entry = plugin.detail_entry()
    form = DetailForm(entry.label or "hosts", entry.build(),
                      presentation=entry.presentation, width=entry.width,
                      plugin_id=entry.key)
    qtbot.addWidget(form)
    return form


def _in_window(widget: QWidget, form: QWidget) -> bool:
    """控件**映射到窗口**的矩形整不整在窗口里。

    不用 ``isVisible()``：控件只是没被 hide，滚出视口它照样 ``isVisible()``。
    """
    top = widget.mapTo(form, QPoint(0, 0)).y()
    return 0 <= top and top + widget.height() <= form.height()


def _back_button(screen: EditScreen) -> QPushButton:
    for b in screen.findChildren(QPushButton):
        if b.text() == "‹ 返回":
            return b
    raise AssertionError("编辑屏里没找到返回键")


def test_a_short_window_keeps_the_editor_headers_reachable(qtbot, tmp_path):
    """hosts 管理窗拖到最矮（``WINDOW_MIN_HEIGHT``）：内容滚得起来，而返回键仍在窗内。

    用户 2026-10-16 报「编辑不能头尾固定」—— 实测（探针）：改之前窗拖到 320 时外层那层
    滚动区有 214px 量程，滚到底返回键跑到 y=-159（窗外）。

    两半都要判：只判「返回键在窗内」的话，内容没超高时它自动成立（判据空转），故先确认
    这一屏的**内层**滚动条有量程。

    另一半（保存键）自「动作行单一化」之后长在窗体页脚上、本来就不滚，这里一并对照着判。
    """
    form = _hosts_detail_form(qtbot, tmp_path)
    form.resize(form.WINDOW_MIN_WIDTH, form.WINDOW_MIN_HEIGHT)
    form.show()
    QApplication.processEvents()

    screen = next(iter(form._editor_rows))
    form._editor_stack.push(screen)
    QApplication.processEvents()

    inner = screen.shell().content_scroll().verticalScrollBar()
    assert inner.maximum() > 0, "这一屏的内容并没有超高 ⇒ 下面的判据是空转的"
    assert form.height() == form.WINDOW_MIN_HEIGHT, "窗口没被拖到最矮"
    # 「头尾固定」的直接原因：内容面**没有**超出视口 ⇒ 外层那层滚动区无从滚起。
    # 只判「返回键在窗内」是不够的 —— 不钳高度时它初始也在窗内，用户一滚才出窗。
    outer = form._content_scroll.verticalScrollBar()
    assert outer.maximum() == 0, (
        f"内容面比视口高 {outer.maximum()}px ⇒ 外层滚动区会把整块编辑屏（含返回键）"
        "滚出去（编辑屏在台上时内容面该被钳在视口里，见 "
        "``DetailForm._apply_editor_height_limit``）"
    )
    back = _back_button(screen)
    assert _in_window(back, form), (
        f"窗拖到最矮之后返回键跑到窗外（y={back.mapTo(form, QPoint(0, 0)).y()}，"
        f"窗高 {form.height()}）⇒ 头尾没固定住"
    )
    assert _in_window(screen.save_button, form), (
        "保存键不在窗内（它长在窗体页脚上，本来就不该滚）"
    )
