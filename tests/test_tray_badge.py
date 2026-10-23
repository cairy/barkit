"""托盘角标渲染回归测试。

两层契约：

1. 语义决策（候选 → 触发 → 取胜 ＋ 级内按 `plugin_order` 排序）在
   `shell/renderers/badge.tray_queue`（决议 D17 纯函数），用例见
   `tests/test_u1_contract.py`。
2. 角标渲染（`StatusItem._render_badge`）必须产出**彩色图层**（非 template）：
   角标带级别色，不能再交给系统单色渲染，底图颜色因此要按菜单栏实际外观另取。
   离屏只保证「画得出来、是彩色的、尺寸与图标一致」——真机可见性以用户目测为准。

注意：真正的「状态事件 → 重绘角标」接线在 `app_shell._push_badge` /
`_apply_tray_badge`，离屏下 `StatusItem._item` 为 None、`set_badge` 直接 return，
无法在离屏断言。该接线是否生效须由真机验证（参见提交说明）。
"""

import platform

import pytest

from barkit.shell.status_item import StatusItem

pytestmark = pytest.mark.skipif(
    platform.system() != "Darwin",
    reason="NSImage/AppKit 仅在 macOS 可用",
)


@pytest.mark.parametrize("level", ["err", "warn", "idle"])
def test_badge_renders_color_layer_per_level(qapp, level):
    """每个级别都画得出彩色角标图层，尺寸与图标一致。

    字形走 `icons.badge_pixmap`（12 网格角标字形）⇒ 需要 QApplication，故显式带
    `qapp` fixture（离屏 QPixmap 的固定要求，见踩坑记录）。
    """
    layer = StatusItem._render_badge(level, None, size=24, dark=False)
    assert layer is not None
    assert not layer.isTemplate()  # 保留彩色角标，交给手动绘制
    size = layer.size()
    assert int(size.width) == 24 and int(size.height) == 24


def test_badge_renders_value_icon_and_dark_palette(qapp):
    """给了取值图标照样画得出来；深色菜单栏走另一套级别色（换肤不崩）。"""
    from barkit.contribution.specs import IconName

    layer = StatusItem._render_badge("warn", IconName.BELL, size=24, dark=True)
    assert layer is not None
    assert not layer.isTemplate()
    assert int(layer.size().width) == 24
