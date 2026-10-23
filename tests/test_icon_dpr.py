"""图标 DPR 渲染回归测试（对应 2026-09-21 的两次回归）。

尺寸类断言（``pm.width() == N``）**抓不到**这两类真问题，故这里改用像素/几何判据：

1. **缺角**：设 dpr 的时机错了 ⇒ SVG 只画进左上角那块物理像素。判据 = dpr=2 的墨迹
   包围盒必须是 dpr=1 的 **2 倍**（整张按物理像素画），而不是挤在左上角。
2. **尺寸错乱**：``QPixmap.width()`` 在 dpr>1 时返回**物理**像素 ⇒ 调用方若拿它当传入
   的逻辑尺寸布局就会错位；托盘 NSImage 把物理像素当点数会放大两倍。判据 = 逻辑尺寸
   （``deviceIndependentSize``）等于传入值、NSImage 点数在 dpr=1/2 下**一致**。
"""

from __future__ import annotations

import platform

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter

from barkit.shell import ui_kit
from barkit.shell.design import icons
from barkit.shell.design.icons import IconName

_RED = QColor(255, 0, 0)


def _ink_bbox(pm) -> tuple[int, int, int, int] | None:
    """位图内 alpha>16 像素的包围盒（物理坐标）。"""
    img = pm.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    w, h = img.width(), img.height()
    xs: list[int] = []
    ys: list[int] = []
    for y in range(h):
        for x in range(w):
            if img.pixelColor(x, y).alpha() > 16:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def test_pixmap_2x_is_exactly_2x_of_1x(qapp):
    """dpr=2 的渲染 = dpr=1 的 2 倍（整张按物理像素画），**不是**被挤到左上角。

    正对「缺角」回归：设 dpr 的时机错了时，dpr=2 的墨迹会退回 dpr=1 的尺寸
    （挤在左上角），右边界只有物理宽度的一半左右。
    """
    one = icons.pixmap(IconName.KEYBOARD, 15, _RED, 1.0)
    two = icons.pixmap(IconName.KEYBOARD, 15, _RED, 2.0)
    b1 = _ink_bbox(one)
    b2 = _ink_bbox(two)
    assert b1 is not None and b2 is not None, "两种 dpr 都必须画出内容"

    for i, axis in enumerate(("宽", "高")):
        left = 2 * b1[i] - 4
        right = 2 * b1[i + 2] + 4
        assert left <= b2[i] and b2[i + 2] <= right, (
            f"dpr=2 的墨迹{axis}未按 2 倍展开：dpr=1={b1}，dpr=2={b2}"
        )
    # 缺角时右边界会 ≈ 物理一半以下；正常应接近右缘。
    assert b2[2] >= two.width() - 4, f"墨迹右边界 {b2[2]} 太小（疑似只画了左上角）"


def test_pixmap_logical_size_equals_requested_at_any_dpr(qapp):
    """``deviceIndependentSize`` 恒等于传入的逻辑尺寸；物理尺寸 = 逻辑 × dpr。"""
    for dpr in (1.0, 2.0):
        pm = icons.pixmap(IconName.KEYBOARD, 22, _RED, dpr)
        assert int(round(pm.deviceIndependentSize().width())) == 22, (
            "逻辑尺寸必须 == 传入值（布局要用它，不能用返回物理像素的 width()）"
        )
        assert pm.devicePixelRatio() == dpr


def test_glyph_pixmap_uses_target_dpr_and_logical_size(qapp):
    """``glyph_pixmap`` 从 target 取 dpr；逻辑尺寸 == 传入值。"""

    class _Target:
        def devicePixelRatioF(self):
            return 2.0

    pm = ui_kit.glyph_pixmap(_Target(), IconName.KEYBOARD, 18, _RED)
    assert int(round(pm.deviceIndependentSize().width())) == 18
    assert pm.devicePixelRatio() == 2.0


def test_draw_glyph_centers_within_logical_rect(qapp):
    """``draw_glyph`` 把字形画进给定的**逻辑矩形**：墨迹落在矩形内且左右余量对称。

    正对「用 pm.width()（物理像素）算居中」的错位回归：那会让墨迹偏移到矩形外。
    """
    n = 40
    rect = QRectF(9, 9, 22, 22)
    img = QImage(n, n, QImage.Format.Format_ARGB32)
    img.fill(0)
    p = QPainter(img)
    ui_kit.draw_glyph(p, IconName.KEYBOARD, rect, _RED)
    p.end()

    img = img.convertToFormat(QImage.Format.Format_ARGB32)
    xs = [x for y in range(n) for x in range(n) if img.pixelColor(x, y).alpha() > 16]
    assert xs, "必须画出内容"
    assert min(xs) >= rect.left() and max(xs) <= rect.right(), (
        f"墨迹 [{min(xs)}..{max(xs)}] 越出逻辑矩形 {rect}（说明定位用了物理像素）"
    )
    # 键盘字形自身左右对称 ⇒ 余量应对称（容差 2px）
    left_gap = min(xs) - rect.left()
    right_gap = rect.right() - max(xs)
    assert abs(left_gap - right_gap) <= 2, f"左右余量不对称：{left_gap} vs {right_gap}"


@pytest.mark.skipif(
    platform.system() != "Darwin", reason="NSImage/AppKit 仅在 macOS 可用"
)
def test_nsimage_point_size_is_normalized(qapp):
    """托盘 NSImage 点数在 dpr=1/2 下**一致**（既锐利又不改变菜单栏那一格的大小）。

    正对「菜单栏图标放大两倍」回归：不做归一时，dpr=2 的底图会被 NSImage 当点数用。
    """
    from barkit.shell.status_item import _pixmap_to_nsimage

    sizes = []
    for dpr in (1.0, 2.0):
        pm = ui_kit.make_tray_pixmap(24, QColor(0, 0, 0), dpr)
        sizes.append(_pixmap_to_nsimage(pm).size().width)
    assert sizes[0] == pytest.approx(sizes[1], abs=0.01), (
        f"dpr 改变时 NSImage 点数不得变：{sizes}"
    )
    assert sizes[0] == pytest.approx(18.0, abs=0.5), "沿用历史显示尺寸（24px 源 → 18pt）"
