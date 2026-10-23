"""回归测试：状态栏图标 Cocoa → Qt 的 Y 翻转常数（多屏排布）。

背景（BUG A）：旧实现把翻转常数写成 ``qt_top + qt_height - cocoa_origin_y``，
比正确值少 ``2 × cocoa_origin_y``。它只在 ``cocoa_origin_y == 0``（主屏、或与主屏
等高且 y 向对齐的左右副屏）时碰巧正确；副屏上下摆放时面板偏移
``2 × |cocoa_origin_y|`` —— 1080p 屏就是 ±2160px，面板直接飞出屏幕。

本测试是**纯数学推演**，不依赖 Qt 窗口服务器 / AppKit 真实屏幕，结论 100% 可验证。
坐标约定与推导见 :func:`barkit.shell.status_item.flip_constant`。
"""

import pytest
from PySide6.QtCore import QRect

from barkit.shell.status_item import (
    flip_constant,
    is_global_cocoa_y,
    match_qt_screen_by_geometry,
)


class _FakeScreen:
    """极简 Qt 屏替身：只暴露 geometry()（含 x/width/height）。"""

    def __init__(self, x: int, y: int, w: int, h: int, name: str) -> None:
        self._g = QRect(x, y, w, h)
        self._name = name

    def geometry(self):
        return self._g

    def name(self):
        return self._name

PRIMARY_H = 1080.0   # 主屏高（Cocoa 与 Qt 两系的变换常数）
BTN_H = 22.0         # 菜单栏图标按钮高度
BTN_TOP_GAP = 8.0    # 按钮顶边距「所属屏顶边」的距离

# (用例名, cocoa_origin_x, cocoa_origin_y, width, height)
LAYOUTS = [
    ("单屏（仅主屏）",     0.0,     0.0, 1920.0, 1080.0),
    ("副屏在右·等高对齐",  1920.0,   0.0, 1440.0,  900.0),
    ("副屏在上·不等高",     0.0,  1080.0, 1440.0,  900.0),
    ("副屏在下·不等高",     0.0,  -900.0, 1440.0,  900.0),
    ("副屏在右上·小屏",   1920.0,  1080.0, 1280.0,  720.0),
]


def _qt_top(cocoa_origin_y: float, height: float) -> float:
    """Cocoa frame → Qt geometry 的 top（两系 y 反向，描述同一条物理顶边）。"""
    return PRIMARY_H - cocoa_origin_y - height


@pytest.mark.parametrize("name,ox,oy,w,h", LAYOUTS)
def test_flip_constant_equals_primary_height(name, ox, oy, w, h):
    """翻转常数在每屏都应恒等于主屏高 —— 这是公式正确性的自校验。"""
    qt_top = _qt_top(oy, h)
    assert flip_constant(qt_top, h, oy) == pytest.approx(PRIMARY_H), f"{name}：常数≠主屏高"


@pytest.mark.parametrize("name,ox,oy,w,h", LAYOUTS)
def test_button_y_maps_to_screen_top(name, ox, oy, w, h):
    """端到端：Cocoa 全局坐标的按钮 → Qt 顶边 Y，必须落在所属屏顶部下方 8px。

    真值来自几何定义（与被测公式无关），不靠公式反推：
      按钮顶边距所属屏顶边 8px  ⇒  Qt 顶边 Y == qt_top + 8
    """
    qt_top = _qt_top(oy, h)
    # Cocoa 全局：按钮底边 y（原点=主屏左下，y 向上）
    cy = oy + h - BTN_TOP_GAP - BTN_H
    const = flip_constant(qt_top, h, oy)
    qt_y_top = const - (cy + BTN_H)
    assert qt_y_top == pytest.approx(qt_top + BTN_TOP_GAP), f"{name}：按钮 Y 算错"


@pytest.mark.parametrize("name,ox,oy,w,h", LAYOUTS)
def test_local_coords_branch_maps_correctly(name, ox, oy, w, h):
    """兜底分支：cy 为「本屏局部」坐标（原点=本屏左下）时也要算对。"""
    qt_top = _qt_top(oy, h)
    cy_local = h - BTN_TOP_GAP - BTN_H          # 距本屏底边的高度
    const = flip_constant(qt_top, h, oy, global_coords=False)
    qt_y_top = const - (cy_local + BTN_H)
    assert qt_y_top == pytest.approx(qt_top + BTN_TOP_GAP), f"{name}：局部坐标分支算错"


@pytest.mark.parametrize(
    "name,ox,oy,w,h",
    [c for c in LAYOUTS if c[2] != 0.0],   # 只测 cocoa_origin_y != 0 的副屏
)
def test_old_formula_was_wrong_on_stacked_screens(name, ox, oy, w, h):
    """锁死回归：旧公式的偏差必须等于 -2×cocoa_origin_y（证明它确实错了）。

    若哪天这个断言不成立，说明坐标约定变了，上面几个正向测试的前提也要重审。
    """
    qt_top = _qt_top(oy, h)
    old = qt_top + h - oy
    new = flip_constant(qt_top, h, oy)
    assert old - new == pytest.approx(-2 * oy)


def test_global_vs_local_detection():
    """「cy 是否落在本屏 Cocoa 纵向区间内」能区分全局 / 局部坐标。"""
    # 副屏在主屏上方：Cocoa 区间 [1080, 1980]，局部区间 [0, 900] —— 不相交
    assert is_global_cocoa_y(1080.0 + 900.0 - 30.0, 1080.0, 900.0) is True    # 全局
    assert is_global_cocoa_y(900.0 - 30.0, 1080.0, 900.0) is False           # 局部
    # 主屏（origin_y == 0）两区间重合 → 判为全局；此时两式取值相同，不影响正确性
    assert is_global_cocoa_y(1080.0 - 30.0, 0.0, 1080.0) is True
    assert flip_constant(0.0, 1080.0, 0.0, global_coords=True) == flip_constant(
        0.0, 1080.0, 0.0, global_coords=False
    )


def test_match_qt_screen_is_order_independent():
    """回归锁死：`NSScreen` 与 `QScreen` 列表顺序错位时，仍能按几何命中正确那块屏。

    这是「面板偶发弹到屏幕左上角」的根因——此前按下标 `qt_screens[i]` 取屏，两列表
    顺序不一致就把面板钉到错误的屏。本测试让 Qt 列表顺序与 Cocoa 完全相反，验证匹配
    只认几何（x 原点 ＋ 宽高），与顺序无关。
    """
    # Cocoa 几何：主屏 (0,0,1920,1080)，副屏在右 (1920,0,1440,900)
    cocoa_primary = (0.0, 0.0, 1920.0, 1080.0)
    cocoa_secondary = (1920.0, 0.0, 1440.0, 900.0)
    # Qt 列表顺序反过来：index0=副屏，index1=主屏
    qt_primary = _FakeScreen(0, 0, 1920, 1080, "primary")
    qt_secondary = _FakeScreen(1920, 0, 1440, 900, "secondary")
    qt_screens = [qt_secondary, qt_primary]

    # 给定副屏的 Cocoa 几何，必须命中 qt_secondary（列表 index0），而非按下标误取主屏
    assert match_qt_screen_by_geometry(*cocoa_secondary, qt_screens) is qt_secondary
    # 主屏同理，命中 qt_primary
    assert match_qt_screen_by_geometry(*cocoa_primary, qt_screens) is qt_primary
    # 完全对不上的几何返回 None（调用方应退回主屏）
    assert match_qt_screen_by_geometry(9999.0, 0.0, 100.0, 100.0, qt_screens) is None
