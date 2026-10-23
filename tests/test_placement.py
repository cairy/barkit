"""tests/test_placement：窗口落位权威（``shell/placement.py``）的用例。

守三件事，每一件都对应一次真实事故：

1. **锚只有「那一次点击」一个可靠来源** —— 由点击点推出的锚必然落在用户点的那块屏的
   菜单栏那一条里；认不出屏就**不给锚**（不给一个假锚去喂落位）。此前锚是回读原生
   图标窗口的矩形，而一个图标在每块屏各有一枚窗口、系统摆好之前那枚还停在屏幕外的
   初始位 ⇒ 长跑实例读到 ``(0, 969)`` 这类假锚；
2. **认不出屏不许落屏角** —— 没有锚时退到可用区右上角、没有屏时原地不动，绝不收敛到
   ``(0, 0)``。「所有弹窗都在左上角」就是这条兜底在被抄了六份之后算出来的；
3. **落完之后位置必须真的成立** —— ``place_window`` 在原生窗口已存在时强制一次原生
   落位（``QWidget.move()`` 在「Qt 记的坐标已等于目标」时直接返回，光靠它修不回分叉），
   ``verify_placement`` 再回读**原生** frame 核对。
"""

from __future__ import annotations

import logging
import pathlib
import re

import pytest
from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget

from barkit.shell import placement
from barkit.shell.placement import Anchor, Intent

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "barkit"


# ---------- 锚：只认那一次点击 ----------


def test_anchor_from_click_sits_in_the_menu_bar_band_of_the_clicked_screen(qtbot):
    """点击点推出的锚：横向以点击点为心，纵向落在该屏菜单栏那一条里。

    违规必抓：把纵向改成用 ``point.y()``（或直接用回读的图标窗口矩形），锚就不再钉在
    菜单栏那一条上 —— 面板会跟着光标在菜单栏里的落点上下浮动。
    """
    screen = QGuiApplication.primaryScreen()
    geo = screen.geometry()
    point = QPoint(geo.x() + geo.width() // 2, geo.top() + 5)

    anchor = placement.anchor_at_click(point, (40, 24))

    assert anchor is not None
    assert anchor.screen is screen
    # ``QRect.center()`` 是整数语义（``x + (w-1)//2``），故「以点击点为心」允许 1px 偏差
    assert abs(anchor.rect.center().x() - point.x()) <= 1, "锚的横向中心不是点击点"
    assert anchor.rect.top() == geo.top(), "锚没钉在屏幕顶边（菜单栏那一条的起点）"
    assert anchor.rect.height() >= 1
    assert anchor.rect.bottom() <= geo.top() + 64, "锚超出了菜单栏那一条"
    assert geo.contains(anchor.rect.center())


def test_anchor_from_click_is_none_when_the_point_is_on_no_screen(qtbot):
    """点击点不在任何一块屏上 ⇒ **不给锚**（由调用方走退路），不猜一块屏。

    违规必抓：把这一格写成「认不出就退回主屏」，本用例会拿到一个主屏锚而变红 ——
    而那正是「假锚喂给落位 ⇒ 面板夹到屏幕边沿」那条路的第一步。
    """
    assert placement.anchor_at_click(QPoint(-99999, -99999), (40, 24)) is None


def test_anchor_of_rect_is_none_when_no_screen_contains_it(qtbot):
    """矩形落不到任何屏上 ⇒ 不给锚（``None``）。

    违规必抓：去掉这层校验（原来的实现直接把矩形当锚用），本用例会拿到一个锚 ——
    这就是长跑实例读到 ``(0, 969)`` 那类屏幕外假锚时的实际行为。
    """
    assert Anchor.of_rect(QRect(-9000, -9000, 40, 24)) is None
    assert Anchor.of_rect(None) is None
    assert Anchor.of_rect(QRect()) is None


class _FarAwayScreen:
    """伪装成一块「另一处的屏」：只提供 ``geometry()``，用来造「提示屏与矩形对不上」。"""

    @staticmethod
    def geometry() -> QRect:
        return QRect(99999, 99999, 100, 100)


def test_anchor_of_rect_relearns_the_screen_when_the_hint_does_not_contain_it(qtbot):
    """提示的那块屏与矩形对不上 ⇒ 按「能包含这个矩形」的屏重新认一次。

    违规必抓：直接采信提示（``scr = screen or screenAt(...)``），锚就会挂在一块
    根本不包含它的屏上 —— 多屏下一枚图标有多枚原生窗口时，这正是面板落到错屏的来路。
    """
    screen = QGuiApplication.primaryScreen()
    rect = QRect(screen.geometry().x() + 10, screen.geometry().top() + 2, 40, 24)

    anchor = Anchor.of_rect(rect, _FarAwayScreen())

    assert anchor is not None
    assert anchor.screen is screen, "提示屏与矩形对不上时没有重新认屏"
    assert anchor.rect == rect


class _StubStatusItem:
    def __init__(self, rect, screen=None, size=(40, 24)) -> None:
        self.available = True
        self._rect = rect
        self._screen = screen
        self._size = size

    def button_rect_qt(self):
        return self._rect

    def button_screen(self):
        return self._screen

    def button_size(self):
        return self._size


def test_anchor_of_status_item_falls_back_to_the_click_point_when_the_rect_is_unusable(qtbot):
    """图标矩形不可用时（``None`` / 屏幕外），锚退到「光标所在的菜单栏那一格」。

    违规必抓：把 ``button_rect_qt()`` 的返回值无条件用掉（旧行为），本用例拿到的锚会是
    那个屏幕外的矩形，而不是点击点推出来的那一个。
    """
    bad = _StubStatusItem(QRect(-9000, -9000, 40, 24))

    anchor = placement.anchor_of_status_item(bad)

    assert anchor is not None, "两条都拿不到时也该按光标给一个锚"
    assert QGuiApplication.primaryScreen().geometry().contains(anchor.rect.center())


def test_anchor_of_status_item_is_none_without_a_status_item(qtbot):
    """没有状态项（非 macOS 回退等）⇒ 不给锚。"""
    assert placement.anchor_of_status_item(None) is None


def test_status_item_rejects_a_rect_that_no_screen_contains(qtbot):
    """``StatusItem`` 的锚点校验：矩形中心落在所有屏之外 ⇒ 当没读到（``None``）。

    违规必抓：把这一层的判据去掉 / 反过来（无论矩形落在哪里都返回它），本用例都会红
    —— 那意味着长跑实例读到的那类屏幕外假锚又会被当成真锚喂给落位。
    """
    from barkit.shell.status_item import StatusItem

    screen = QGuiApplication.primaryScreen()
    geo = screen.geometry()
    good = QRect(geo.x() + 10, geo.top(), 40, 24)

    assert StatusItem._validated(good) == good
    assert StatusItem._validated(QRect(-9000, -9000, 40, 24)) is None


# ---------- 落位：认不出屏不许落屏角 ----------


def _window(qtbot, w=200, h=100) -> QWidget:
    win = QWidget()
    qtbot.addWidget(win)
    win.setFixedSize(w, h)
    return win


def test_below_icon_without_an_anchor_lands_at_the_available_top_right(qtbot):
    """没有锚时贴图标那一档退到**可用区右上角**，不是屏幕角。

    违规必抓：把这一格写回「认不出屏就退回 ``QRect(0, 0, sw, sh)``」那种兜底
    （旧的 ``_place`` / ``place_toast`` 都是这么写的），算出来正好是 ``(0, gap)``
    —— 也就是用户看到的左上角。本用例会红。
    """
    win = _window(qtbot)
    ag = QGuiApplication.primaryScreen().availableGeometry()

    pt, screen = placement.target_top_left(win, Intent.BELOW_ICON, anchor=None)

    assert screen is not None
    assert pt.y() == ag.top() + placement.ANCHOR_GAP
    assert win.width() + pt.x() == ag.right() + 1, "没贴住可用区右缘"
    assert pt != QPoint(0, 0), "落到了屏幕角上"


def test_below_icon_leaves_a_gap_below_the_icon_and_flips_above_when_full(qtbot):
    """贴图标：下方放得下就落在图标下方（留出 ``ANCHOR_GAP``），放不下翻到上方。"""
    win = _window(qtbot)
    screen = QGuiApplication.primaryScreen()
    ag = screen.availableGeometry()

    top_rect = QRect(ag.x() + 100, screen.geometry().top(), 40, 24)
    top_anchor = Anchor.of_rect(top_rect)
    assert top_anchor is not None
    pt, _ = placement.target_top_left(win, Intent.BELOW_ICON, anchor=top_anchor)
    assert pt.y() == top_rect.bottom() + 1 + placement.ANCHOR_GAP, "图标下方没留缝"
    assert pt.y() >= ag.top(), "卡片顶到可用区之外（盖住菜单栏 / Dock）"

    # 锚贴屏底：下方放不下一张卡 ⇒ 翻到图标上方
    low_rect = QRect(ag.x() + 100, ag.bottom() - 24, 40, 24)
    low_anchor = Anchor.of_rect(low_rect)
    assert low_anchor is not None
    pt2, _ = placement.target_top_left(win, Intent.BELOW_ICON, anchor=low_anchor)
    assert pt2.y() + win.height() <= low_rect.top(), "放不下时没有翻到图标上方"


def test_top_right_and_center_top_keep_the_documented_formulas(qtbot):
    """钉右上角 / 居中偏上两档的算式：可用区右上角一档、垂直 30% 一处。"""
    from barkit.shell.design import metrics

    win = _window(qtbot)
    ag = QGuiApplication.primaryScreen().availableGeometry()

    pt, _ = placement.target_top_left(win, Intent.TOP_RIGHT)
    assert pt.x() == ag.x() + ag.width() - win.width() - metrics.OVERLAY_EDGE_GAP
    assert pt.y() == ag.y() + metrics.OVERLAY_EDGE_GAP

    pt2, _ = placement.target_top_left(win, Intent.TOP_RIGHT, above=30)
    assert pt2.y() == ag.y() + metrics.OVERLAY_EDGE_GAP + 30

    pt3, _ = placement.target_top_left(win, Intent.CENTER_TOP)
    assert pt3.x() == ag.x() + (ag.width() - win.width()) // 2
    assert pt3.y() == ag.y() + int(ag.height() * placement.CENTER_TOP_RATIO)


def test_resolve_screen_prefers_the_explicit_screen_then_the_anchor(qtbot):
    """认屏优先级：显式给的屏 ＞ 锚那块屏 ＞ 光标 / 窗口自己的屏。

    违规必抓：把「显式给的屏」这一支去掉，提示栈重排就会跟着光标换屏 ——
    多屏下光标一动，屏上那一列提示集体搬家。
    """
    win = _window(qtbot)
    screen = QGuiApplication.primaryScreen()
    anchor = Anchor.of_rect(QRect(screen.geometry().x() + 10, screen.geometry().top(), 40, 24))
    assert anchor is not None

    explicit = _FarAwayScreen()
    assert placement.resolve_screen(win, Intent.TOP_RIGHT, anchor, screen=explicit) is explicit
    assert placement.resolve_screen(win, Intent.TOP_RIGHT, anchor) is screen
    assert placement.resolve_screen(win, Intent.TOP_RIGHT, None) is not None


# ---------- 落完之后位置必须真的成立 ----------


def test_place_window_pushes_the_position_to_the_native_window(qtbot, monkeypatch):
    """落位要**同时**走 Qt（账本）与原生（真位置）两条：原生窗口已存在时强制一次。

    违规必抓：删掉 ``place_window`` 里那次 ``set_native_top_left``，本用例收到的原生
    落位调用为空而变红。这一剂治的正是「显式前 move 只写进 Qt 账本、原生窗口建出来时
    并不按它摆，而之后 Qt 又以『账本已等于目标』为由拒绝再 move」。
    """
    win = _window(qtbot)
    forced: list[tuple[int, int]] = []
    monkeypatch.setattr(
        placement, "set_native_top_left",
        lambda widget, x, y: (forced.append((x, y)), True)[1],
    )

    pt, screen = placement.place_window(win, Intent.TOP_RIGHT)

    assert screen is not None
    assert forced == [(pt.x(), pt.y())], "落位没有把坐标推到原生窗口上"
    assert win.pos() == pt


def test_verify_placement_refixes_a_diverged_window(qtbot, monkeypatch):
    """原生窗口与目标分叉 ⇒ 原生强制一次并报 ``"refixed"``。"""
    win = _window(qtbot)
    state = {"pos": QPoint(0, 0)}
    monkeypatch.setattr(placement, "native_top_left", lambda widget: state["pos"])

    def _force(widget, x, y):
        state["pos"] = QPoint(x, y)
        return True

    monkeypatch.setattr(placement, "set_native_top_left", _force)

    assert placement.verify_placement(win, QPoint(100, 200)) == "refixed"
    assert state["pos"] == QPoint(100, 200), "纠正之后原生窗口没落到目标上"


def test_verify_placement_reports_a_mismatch_when_it_cannot_refix(qtbot, monkeypatch):
    """原生纠正也落不上 ⇒ ``"mismatch"``（这一条要用户带日志来报的那种）。"""
    win = _window(qtbot)
    monkeypatch.setattr(placement, "native_top_left", lambda widget: QPoint(7, 7))
    monkeypatch.setattr(placement, "set_native_top_left", lambda widget, x, y: False)

    assert placement.verify_placement(win, QPoint(100, 200)) == "mismatch"


def test_place_window_speaks_up_when_the_native_move_does_not_land(qtbot, monkeypatch, caplog):
    """**已显示**的窗口落位没落到原生窗口上 ⇒ 必须留下一条记录。

    违规必抓：把那条告警删掉（或改成只在窗口不可见时才记），本用例会红。

    静默的后果（2026-09-30 真机）：Qt 账本写着目标点、原生窗口停在它诞生时的默认位
    （屏幕左上角），而此后再 ``move()`` 会因「账本已等于目标」直接短路 —— 分叉从此
    没人修得回来。
    """
    win = _window(qtbot)
    win.show()
    qtbot.wait(1)
    assert win.isVisible(), "前提：窗口已显示（只有这时落位才必须落到原生窗口上）"
    monkeypatch.setattr(placement, "_native_ready", lambda: True)
    monkeypatch.setattr(placement, "set_native_top_left", lambda widget, x, y: False)

    with caplog.at_level(logging.WARNING):
        placement.place_window(win, Intent.TOP_RIGHT)

    assert any(
        "落位没落到原生窗口上" in r.message for r in caplog.records
    ), "落位没落上去却一声不吭"


def test_verify_placement_never_passes_when_it_cannot_read_the_window(
    qtbot, monkeypatch, caplog
):
    """跑在原生窗口下却取不到窗口 ⇒ ``"unknown"`` **并且出声**，不许伪装成 ``"ok"``。

    违规必抓：把 ``return "unknown"`` 改回静默的 ``return "ok"``，本用例会红。

    这正是 2026-09-30 真机那条坏法：面板的原生 frame 在 ``(0,0)``，Qt 账本与日志都
    写着落位 ``(771,42)``，而核对因为「没有原生读数」判了通过、一声不吭 —— 把
    「判据不适用」当成「核对通过」，这条守卫就整个成了摆设。
    """
    win = _window(qtbot)
    monkeypatch.setattr(placement, "_native_ready", lambda: True)
    monkeypatch.setattr(placement, "native_top_left", lambda widget: None)
    with caplog.at_level(logging.WARNING):
        result = placement.verify_placement(win, QPoint(100, 200))
    assert result == "unknown", "取不到读数却报了「通过」"
    assert any(
        "没有被核对" in r.message for r in caplog.records
    ), "判据不适用却没有留下任何痕迹"


def test_verify_placement_is_quiet_when_it_agrees_or_has_no_native_reading(qtbot, monkeypatch):
    """一致 ⇒ ``"ok"``；非原生环境（读不到原生读数）也是 ``"ok"`` —— 那是判据不适用。"""
    win = _window(qtbot)
    monkeypatch.setattr(placement, "native_top_left", lambda widget: QPoint(100, 200))
    assert placement.verify_placement(win, QPoint(100, 200)) == "ok"

    monkeypatch.setattr(placement, "native_top_left", lambda widget: None)
    assert placement.verify_placement(win, QPoint(100, 200)) == "ok"


# ---------- 结构性门禁：落位权威只许有一处 ----------


def test_only_the_placement_module_pins_a_window_to_a_screen():
    """``setScreen(`` 只许出现在 ``placement.py`` —— 钉屏收归一处。

    违规必抓：在面板 / 浮层 / 设置窗里再写一次 ``self.setScreen(...)``（六处兜底当年
    各写一份就是这么长出来的），本用例会红。
    """
    offenders = sorted(
        str(p.relative_to(_SRC))
        for p in _SRC.rglob("*.py")
        if "setScreen(" in p.read_text(encoding="utf-8")
        and p.name != "placement.py"
    )
    assert offenders == [], f"钉屏长出了第二份实现：{offenders}"


def test_no_window_module_reimplements_the_placement():
    """面板 / 设置窗 / 详情页 / 浮层里不许再有**私有落位实现**。

    违规必抓：把任意一份私有算式写回来（``_place`` / ``_center_on_screen`` /
    ``_position_for_height`` / ``place_toast`` / ``place_near_anchor`` 这些名字各自
    对应过一次「只修了六分之一」），本用例会红。
    """
    banned = (
        "def _place(",
        "def _center_on_screen(",
        "def _position_for_height(",
        "def place_toast(",
        "def place_near_anchor(",
        "def _clamp_into_screen(",
    )
    offenders = sorted(
        f"{p.relative_to(_SRC)}:{name}"
        for p in _SRC.rglob("*.py")
        if p.name != "placement.py"
        for name in banned
        if re.search(rf"^\s*{re.escape(name)}", p.read_text(encoding="utf-8"), re.M)
    )
    assert offenders == [], f"落位长出了第二份实现：{offenders}"


def test_the_anchor_argument_never_comes_from_a_raw_status_item_rect():
    """落位 API 的「锚」只许来自 ``placement`` 的取锚口，不许喂裸矩形。

    违规必抓：把 ``show_and_raise(item.button_rect_qt() if ready else None)`` 这类
    写法再写回来。那是本批漏掉的一处调用点（热键「打开设置」）：裸矩形进了
    ``resolve_screen`` 要读 ``.screen`` 会当场炸，而且它绕开的正是「锚是 ``Anchor``、
    不是矩形」这条约定 —— 换句话说，谁能往落位口里塞矩形，谁就还能塞一个屏幕外的
    假矩形进来，那就又回到「弹窗跑到屏角」那条老路上了。

    （``status_item.button_rect_qt`` 本身不算违规 —— 判定「点在不在图标上」仍要用它；
    违规的是**把它当锚递给落位 API**。）
    """
    sinks = (
        "show_and_raise(",
        "place_window(",
        "toggle_at(",
        "on_status_item_click(",
    )
    raw_shapes = ("button_rect_qt", "button_rect(", "button_screen")
    offenders = sorted(
        f"{p.relative_to(_SRC)}:{i}"
        for p in _SRC.rglob("*.py")
        if p.name != "placement.py"
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if any(s in line for s in sinks) and any(r in line for r in raw_shapes)
    )
    assert offenders == [], f"锚参数被喂了裸矩形：{offenders}"


def test_taking_the_native_window_has_only_one_door():
    """取原生窗口只许 ``macos_window.nswindow_of`` **一个**入口。

    违规必抓：再写一份「遍历窗口列表比对指针」的实现（或把 ``_find_nswindow`` 请回来），
    本用例会红。

    这一条是 2026-09-30 的真机根因：取窗此前有两条路各走各的，其中**一条在生产实例上
    取不到窗** ⇒ 落位只写进了 Qt 的账本，核对又因为「没有读数」静默放行 ⇒ 面板停在
    默认位（屏幕左上角）。两份实现里坏的那份**不报错**，只有「收成一个入口 ＋ 取不到
    就出声」能防住它。

    ⚠️ 2026-10-03 拆包后要点名**真身**（``macos_window/macos_level.py``）、不许只按族
    前缀（``shell/macos_window``）放行：族名会一路盖住族里任何一份实现，包括新长出来的
    第二份。判据改成「整个族里的命中**恰好一份**、且落在唯一那枚入口所在的子模块上」，
    族内文件数 > 1 只用来证明「这个族确实被拆开了、这条判据有东西可点名」（控制组，
    拆成单文件时会红）。

    违规必抓：在族里（哪怕另一个子模块里）再写一份「遍历窗口列表比对指针」的实现，
    本用例会红。
    """
    door = "for w in NSApp.windows()"
    hits = sorted(
        f"{p.relative_to(_SRC)}:{i}"
        for p in _SRC.rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if door in line
    )
    assert len(hits) == 1 and hits[0].startswith("shell/macos_window/macos_level.py"), (
        f"取窗长出了第二份实现（只许 macos_window/macos_level.py 里那一处）：{hits}"
    )
    # 前提钉：这条门禁是**对着拆开后的族**写的，所以要能指出「入口确实住在这个族里」。
    # 拆回单文件时上面那条会转红（起点名作用），这里只是把「改回来之前先看一眼」说出口。
    door_owner = _SRC / "shell" / "macos_window" / "macos_level.py"
    assert door_owner.is_file(), (
        f"取窗入口不住在 {door_owner.relative_to(_SRC)} 了 —— 门禁的点名要跟着改，"
        f"别让 `len(hits) == 1` 空转成「没人实现」："
        f"{sorted(str(p.relative_to(_SRC)) for p in door_owner.parent.rglob('*.py'))}"
    )

    ghosts = sorted(
        f"{p.relative_to(_SRC)}:{i}"
        for p in _SRC.rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if "_find_nswindow" in line
    )
    assert ghosts == [], f"旧的那份私有取窗被请回来了：{ghosts}"


def test_the_degenerate_screen_fallback_is_gone():
    """``QRect(0, 0, sw, sh)``（「认不出屏就当整块屏」）不许再出现。

    违规必抓：把它写回来 —— 这个矩形一进 clamp 算式，x 恒为 0、y 恒为边距，
    算出来就是一个屏幕角。这正是「所有弹窗都在左上角」那三个字面来源。
    """
    pattern = re.compile(r"QRect\(\s*0\s*,\s*0\s*,\s*sw\s*,\s*sh\s*\)")
    offenders = sorted(
        str(p.relative_to(_SRC))
        for p in _SRC.rglob("*.py")
        if pattern.search(p.read_text(encoding="utf-8"))
    )
    assert offenders == [], f"认不出屏又拿整块屏兜底了：{offenders}"


@pytest.mark.parametrize("name", ["placement.place_toast", "placement.place_near_anchor"])
def test_the_two_old_placement_entry_points_are_gone(name):
    """两个旧入口（贴图标 / 钉屏角各一个）已被 ``Intent`` 替掉，别留兼容别名。"""
    mod, _, attr = name.partition(".")
    assert not hasattr(placement, attr)


# ---------- 端到端：面板真的落在可用区右上角（不是屏幕角）----------


def test_panel_without_an_anchor_lands_at_the_available_top_right(qtbot):
    """没有锚也能开面板，且落在**可用区右上角**。

    这是用户看到的那一幕的回归：锚坏掉（读到一个屏幕外的假锚）时，面板不该被夹到
    屏幕角上 —— 假锚现在根本构造不出来（``Anchor.of_rect`` 拒绝），锚缺失时也有一条
    与「屏幕角」不同的明确落点。
    """
    from barkit.shell.panel import StatusPanel

    panel = StatusPanel()
    qtbot.addWidget(panel)
    ag = QGuiApplication.primaryScreen().availableGeometry()

    panel.toggle_at(None)
    qtbot.wait(0)

    assert panel.isVisible()
    assert panel.geometry().right() == ag.right(), "没贴住可用区右缘"
    assert panel.y() == ag.top() + placement.ANCHOR_GAP
    assert panel.pos() != QPoint(0, 0), "落到了屏幕角上"
    panel.hide()


# ---------- 独立读数：窗口服务器（用户所见）≠ NSWindow.frame() ----------


class _DeadScreen:
    """伪装成一枚**已随屏变化失效**的 ``QScreen``：碰任何一处 C++ 都抛（shiboken 死对象形态）。

    真身来源见 ``placement._screen_alive``：屏参数一变，此前取到的那枚 ``QScreen`` 底下的
    C++ 对象已被 Qt 销毁，``geometry()`` / ``availableGeometry()`` 随即抛
    ``RuntimeError: libshiboken: Internal C++ object (...) already deleted``。
    """

    _MSG = "libshiboken: Internal C++ object (PySide6.QtGui.QScreen) already deleted."

    def geometry(self):
        raise RuntimeError(self._MSG)

    def availableGeometry(self):
        raise RuntimeError(self._MSG)


def _anchor_on_primary(rect=None, screen=None) -> Anchor:
    """主屏菜单栏那一条上的锚（``screen`` 给假对象时可用来造「缓存的屏已死」）。"""
    scr = QGuiApplication.primaryScreen()
    if rect is None:
        rect = QRect(scr.geometry().x() + 10, scr.geometry().top(), 40, 24)
    return Anchor(rect, scr if screen is None else screen)


def test_resolve_anchor_screen_keeps_a_live_cached_screen(qtbot):
    """锚上那枚 QScreen 还活着 ⇒ 原样用（不白重认，免得把锚挪到别的屏）。"""
    scr = QGuiApplication.primaryScreen()
    assert placement.resolve_anchor_screen(_anchor_on_primary(screen=scr)) is scr


def test_resolve_anchor_screen_relearns_after_the_cached_screen_dies(qtbot):
    """锚上那枚 QScreen 已被屏变化作废 ⇒ 按锚矩形**重新认一次**，不把死对象交出去。

    违规必抓：把 ``resolve_anchor_screen`` 写回 ``return anchor.screen``（旧行为），本用例
    拿到的就是那枚死对象 —— 这正是 2026-10-14 真机 ``RuntimeError: QScreen already deleted``
    的第一跳。
    """
    scr = QGuiApplication.primaryScreen()
    assert placement.resolve_anchor_screen(_anchor_on_primary(screen=_DeadScreen())) is scr, (
        "缓存的死 QScreen 没有被重认掉"
    )


def test_resolve_screen_never_hands_out_a_dead_anchor_screen(qtbot):
    """认屏出口不许把一枚死 QScreen 交出去（死对象在下一跳就会抛）。"""
    win = _window(qtbot)
    dead = Anchor(QRect(-9000, -9000, 40, 24), _DeadScreen())
    scr = placement.resolve_screen(win, Intent.TOP_RIGHT, dead)
    assert scr is not None
    assert placement._screen_alive(scr), "认屏给出了一枚死对象"


def test_panel_height_survives_a_dead_anchor_screen(qtbot):
    """屏变化后锚上那枚 QScreen 会死；开面板那条链此前**当场崩**在这里。

    违规必抓：把 ``_available_panel_height`` 写回 ``scr = anchor.screen``，本用例抛
    ``RuntimeError: ... already deleted``（2026-10-14 复现两次：整条开面板链断掉）。
    """
    from barkit.shell.panel import StatusPanel

    panel = StatusPanel()
    qtbot.addWidget(panel)
    try:
        panel._anchor = Anchor(
            QRect(QGuiApplication.primaryScreen().geometry().x() + 10, 0, 40, 24),
            _DeadScreen(),
        )
        assert panel._available_panel_height() > 0
    finally:
        panel.hide()


def test_verify_on_screen_catches_a_window_the_server_pinned_at_the_corner(qtbot, monkeypatch):
    """窗口服务器把窗摆在 ``(0,0)``、目标是 ``(934,42)`` ⇒ ``"mismatch"``。

    这就是 2026-10-14 定案的形态本身（同一个 ``windowNumber``：NS 说 (934,42)、服务器说
    (0,0)）。违规必抓：把判据恒返回 ``"ok"``（或只比 x），本用例会红。
    """
    win = _window(qtbot)
    monkeypatch.setattr(placement, "window_server_top_left", lambda widget: QPoint(0, 0))
    assert placement.verify_on_screen(win, QPoint(934, 42)) == "mismatch"


def test_verify_on_screen_is_ok_when_the_server_agrees(qtbot, monkeypatch):
    """一致 ⇒ ``"ok"``；容差内（缩放屏上会差零点几像素）也算一致。"""
    win = _window(qtbot)
    monkeypatch.setattr(placement, "window_server_top_left", lambda widget: QPoint(934, 42))
    assert placement.verify_on_screen(win, QPoint(934, 42)) == "ok"

    monkeypatch.setattr(
        placement, "window_server_top_left",
        lambda widget: QPoint(934 + placement.VERIFY_TOLERANCE, 42),
    )
    assert placement.verify_on_screen(win, QPoint(934, 42)) == "ok"


def test_verify_on_screen_separates_not_yet_on_screen_from_a_non_native_run(qtbot, monkeypatch):
    """取不到窗口服务器读数有两种，必须分开：还没上屏（``unknown``）／非原生（判据不适用＝``ok``）。

    违规必抓：把 ``unknown`` 那一支改成静默 ``ok``，本用例第一句就红 —— 而这正是 2026-09-30
    那条「判据不适用伪装成通过」的老毛病。
    """
    win = _window(qtbot)
    monkeypatch.setattr(placement, "window_server_top_left", lambda widget: None)

    monkeypatch.setattr(placement, "_native_ready", lambda: True)
    assert placement.verify_on_screen(win, QPoint(100, 200)) == "unknown"

    monkeypatch.setattr(placement, "_native_ready", lambda: False)
    assert placement.verify_on_screen(win, QPoint(100, 200)) == "ok"


def test_verify_on_screen_speaks_up_on_a_mismatch(qtbot, monkeypatch, caplog):
    """分叉必须留下记录 —— 静默的「核对通过」正是这个故障活了这么久的原因。"""
    win = _window(qtbot)
    monkeypatch.setattr(placement, "window_server_top_left", lambda widget: QPoint(0, 0))
    with caplog.at_level(logging.WARNING):
        placement.verify_on_screen(win, QPoint(934, 42))
    assert any("窗口服务器" in r.message for r in caplog.records), "分叉却一声不吭"


def test_the_two_checkers_disagree_exactly_when_the_window_server_deserts(qtbot, monkeypatch):
    """两眼分工：NS 账本说对了，窗口服务器说没摆到那儿 —— **只有独立读数看得见**。

    违规必抓：把 ``verify_on_screen`` 也改成回读 ``NSWindow.frame()``（＝退回旧行为），本用例
    第二句会拿到 ``"ok"`` 而变红。这条守的是「至少有一口核对不读自己刚写进去的值」。
    """
    win = _window(qtbot)
    monkeypatch.setattr(placement, "_native_ready", lambda: True)
    monkeypatch.setattr(placement, "native_top_left", lambda widget: QPoint(934, 42))
    monkeypatch.setattr(placement, "window_server_top_left", lambda widget: QPoint(0, 0))

    assert placement.verify_placement(win, QPoint(934, 42)) == "ok", "自读自的账本却报不等"
    assert placement.verify_on_screen(win, QPoint(934, 42)) == "mismatch", "独立读数没抓到分叉"


def test_the_window_server_reading_has_exactly_one_door():
    """窗口服务器读窗位置只许 ``macos_window/macos_level.py`` 那一处实现。

    违规必抓：在别处再写一份 ``CGWindowListCopyWindowInfo``（两份实现里坏的那份不报错，
    2026-09-30 取窗那条已经吃过一次），本用例会红。
    """
    hits = sorted(
        f"{p.relative_to(_SRC)}:{i}"
        for p in _SRC.rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if "Quartz.CGWindowListCopyWindowInfo(" in line
    )
    assert len(hits) == 1 and hits[0].startswith("shell/macos_window/macos_level.py"), (
        f"窗口服务器读数长出了第二份实现（只许 macos_level.py 里那一处）：{hits}"
    )


class _FakeWindow:
    """``_apply`` 只用得到这四个口，故不必真造一个 QWidget 去伪造它的 ``screen()``。"""

    def __init__(self, *, visible, screen) -> None:
        self._visible = visible
        self._screen = screen
        self.moves: list = []
        self.pins: list = []

    def isVisible(self) -> bool:  # noqa: N802 —— 对齐 Qt 的命名
        return self._visible

    def screen(self):
        return self._screen

    def move(self, pt) -> None:
        self.moves.append(pt)

    def setScreen(self, scr) -> None:  # noqa: N802
        self.pins.append(scr)


def test_place_window_does_not_pin_an_already_visible_window(monkeypatch):
    """**已可见**的窗口落位只 move，不 ``setScreen``。

    违规必抓：把 ``_apply`` 里那句 ``not window.isVisible()`` 去掉，本用例收到的 ``setScreen``
    调用非空而变红。

    为什么必须在可见时收手：``setScreen`` 会重建原生体，而重建出来的那枚新窗在「屏模型不
    完整」的那几秒里会被窗口服务器落在 ``(0,0)`` —— 落位这一步于是成了「把窗钉死在左上角」
    的成因本身（2026-10-14 真机：只有那扇在屏变化窗口期被反复重落的窗被钉住，同进程另两枚
    只落过一次的浮层窗位置都对）。
    """
    fake = _FakeWindow(visible=True, screen=object())
    monkeypatch.setattr(placement, "set_native_top_left", lambda w, x, y: True)

    placement._apply(fake, QPoint(10, 20), object())

    assert fake.pins == [], "可见窗口落位时又钉了一次屏（那会重建原生体）"
    assert fake.moves == [QPoint(10, 20)], "可见窗口落位时连 move 都省了"


def test_place_window_still_pins_a_window_that_is_not_shown_yet(monkeypatch):
    """还没显示的窗口照旧钉屏 —— 那一档没有重建原生体的代价，而跨屏要它。"""
    fake = _FakeWindow(visible=False, screen=object())
    target = object()
    monkeypatch.setattr(placement, "set_native_top_left", lambda w, x, y: True)

    placement._apply(fake, QPoint(10, 20), target)

    assert fake.pins == [target], "还没显示的窗口没有钉屏（跨屏会落错块屏）"
    assert fake.moves == [QPoint(10, 20)]
