"""ui/styles：主题令牌与实时换肤（ThemeManager）的单元测试。

注意：换肤的「原生窗口重建 / 系统外观监听」只能在真实 macOS 验证；
这里只守住可测的纯逻辑：令牌完整性、apply_theme 缓存+发信号。
托盘角标队列决策已迁 shell/renderers/badge.tray_queue（决议 D17），
其测试见 tests/test_u1_contract.py。
"""

import re

import pytest
from PySide6.QtCore import QEvent, QPoint
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from barkit.shell import styles, ui_kit
from barkit.shell.design import metrics


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_tokens_have_required_keys(theme):
    t = styles.TOKENS[theme]
    for key in (
        "accent", "accent_hover", "accent_soft", "text1", "text2", "text3",
        "surface", "surface2", "surface3", "border", "border_strong",
        "red", "red_soft", "green", "green_soft", "orange", "orange_soft",
    ):
        assert key in t, f"{theme} 缺令牌 {key}"


def test_build_stylesheet_contains_tokens():
    css = styles.build_stylesheet("dark")
    assert "background" in css and "border-radius" in css


def test_apply_theme_updates_cache_and_emits(qtbot):
    app = QApplication.instance() or QApplication([])
    received = []
    styles.theme_changed.connect(lambda th: received.append(th))

    styles.apply_theme(app, "dark")
    assert styles.current_theme() == "dark"
    assert received and received[-1] == "dark"

    styles.apply_theme(app, "light")
    assert styles.current_theme() == "light"
    assert received[-1] == "light"


def test_apply_theme_invalid_falls_back_to_light(qtbot):
    app = QApplication.instance() or QApplication([])
    styles.apply_theme(app, "neon")
    assert styles.current_theme() == "light"


# ---- 按钮几何：档位（大小）与外观族（颜色）是两根轴，族不许把档压掉（2026-09-22）----
#
# 来历：QSS 只按**外观族**写死了一套 `padding: 6px 14px; font-size: 13px`（自然高度 33px），
# 而 `metrics.BUTTON_HEIGHTS` 把框钉在 28 / 22 / 20 ⇒ 每个 pill-ghost 按钮都被裁
# 5 / 11 / 13px —— 用户上报的「按钮显示不全」，最典型的是「恢复默认配置」（最小的档 ×
# 最肥的族）。修法＝把尺寸那根轴补上：控件把档写进 `tier` 属性，QSS 按 `[tier=…]` 给字号
# 与内边距，族块只管颜色。
#
# 下面三条守同一件事的三个面，缺一面就会重新长出这个 bug：
#   ① 档位真的落到控件上（量自然高度，几何口径）；
#   ② QSS 里的档名与 `metrics` 不分叉；
#   ③ 族块里不许再声明尺寸 —— 压制是**整块**的（`#id` 优先级高于 `[attr]`），
#      写一行 `padding` 就等于把档位功能关掉，且不报任何错。

#: 基础 `QPushButton` 块就是这一档的形态（兼作没设 `tier` 的裸按钮的兜底）；
#: 其余档必须有显式的 `[tier=…]` 选择器。
_BASE_TIER = "action"


def _qss_blocks(css: str) -> list[tuple[str, list[str]]]:
    """把生成的 QSS 抠成 ``(选择器, [声明, ...])``（只认顶层 ``… { … }`` 块）。"""
    blocks: list[tuple[str, list[str]]] = []
    for raw in css.split("}"):
        if "{" not in raw:
            continue
        selector, body = raw.rsplit("{", 1)
        selector = " ".join(selector.split())
        decls = [d.strip() for d in body.replace("\n", ";").split(";") if d.strip()]
        blocks.append((selector, decls))
    return blocks


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_button_natural_height_fits_its_tier(theme):
    """每个「族 × 档」的自然高度都不得超过该档钉死的高度（超了就是字被裁）。

    量 `sizeHint()` 而不是读 QSS 文本：属性名一旦写错、或档位被族块压掉，控件会**静默**
    回落到基础档的内边距 —— 大档（28）下只差 1px 看不出来，chip 档（20）下差 7px，
    正好是用户看得见的那种「显示不全」。
    """
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(styles.build_stylesheet(theme))
    over: list[str] = []
    for family in ("pill-ghost", "pill-chip", "primary", "danger"):
        for tier, height in metrics.BUTTON_HEIGHTS.items():
            natural = ui_kit.tool_button("恢复默认配置", family, size=tier).sizeHint().height()
            if natural > height:
                over.append(f"{family}[{tier}]：自然 {natural}px > 档高 {height}px")
    # 面板的 `MenuSpec` 走 `MenuButton`（QPushButton 子类，绕不过 tool_button），
    # 它曾经自己写 setFixedHeight(26)：既不在三档里，也没告诉 QSS 按哪档给内边距。
    menu = ui_kit.MenuButton("场景")
    if menu.height() != metrics.BTN_ACTION:
        over.append(f"MenuButton：高度 {menu.height()}px ≠ action 档 {metrics.BTN_ACTION}px")
    if menu.sizeHint().height() > menu.height():
        over.append(f"MenuButton：自然 {menu.sizeHint().height()}px > 档高 {menu.height()}px")
    assert not over, "按钮内容被裁（QSS 没按 tier 分档）：\n  " + "\n  ".join(over)


def test_button_natural_height_check_catches_the_old_stylesheet():
    """自证（控制组 = 旧行为）：2026-09-22 之前那套 QSS 下，上一条用例的判据必须红。

    旧 QSS 的形态就是「尺寸只跟着外观族走」：族块里写死 ``padding: 6px 14px;
    font-size: 13px``（自然高度 33px），而框仍被钉在 chip 档的 20px。把这份样式表原样
    套上去量 —— 要么**每一档**都被裁（正是那个 bug 的面貌），要么上一条用例就是哑的。
    """
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(
        "QPushButton { background: #EFEFF0; color: #1D1D1F;"
        " border: 1px solid #D5D5D7; border-radius: 8px;"
        " padding: 6px 14px; font-size: 13px; }"
        "QPushButton#pill-ghost { background: transparent; color: #6E6E73;"
        " border: 1px solid #D5D5D7; border-radius: 8px;"
        " padding: 6px 14px; font-size: 13px; font-weight: 600; }"
    )
    cut: list[str] = []
    for tier, height in metrics.BUTTON_HEIGHTS.items():
        natural = ui_kit.tool_button("恢复默认配置", "pill-ghost", size=tier).sizeHint().height()
        if natural > height:
            cut.append(f"{tier}：自然 {natural}px > 档高 {height}px")
    assert len(cut) == len(metrics.BUTTON_HEIGHTS), (
        f"旧样式表下每一档都该被裁；实际只裁了 {len(cut)} 档：{cut}"
    )
    app.setStyleSheet(styles.build_stylesheet("light"))   # 还原，别污染后面的用例


def test_qss_tiers_match_metrics_tiers():
    """QSS 里的档位选择器必须与 ``metrics.BUTTON_HEIGHTS`` 的档名**一一对上**。"""
    css = styles.build_stylesheet("dark")
    found = set(re.findall(r'\[(?:%s)="([^"]+)"\]' % metrics.TIER_PROPERTY, css))
    expected = set(metrics.BUTTON_HEIGHTS) - {_BASE_TIER}
    assert found == expected, (
        f"QSS 的档位选择器 {sorted(found)} ≠ 应有 {sorted(expected)}"
        f"（`{_BASE_TIER}` 档由基础 QPushButton 块承担，不另写选择器）"
    )
    # 反例：档名被改错时判据必须能看出来（否则这条只是「两个空集合相等」）
    assert found != set(metrics.BUTTON_HEIGHTS), "判据写反了：基础档不该出现在选择器里"


def test_button_size_axes_live_in_tier_selectors_not_family_blocks():
    """外观族块（``#pill-*``）不得声明字号 / 内边距 / 圆角。

    ``#id`` 选择器的优先级高于 ``[tier=…]``，而**压制是整块的**：族块里只要有一行
    ``padding``，整条 ``[tier="chip"]`` 就失效（2026-09-22 实测：单写档位选择器时
    三档全部返回 33px）。这不是「多写一处冗余」，是「档位被静默关掉」。
    """
    offenders: list[str] = []
    for selector, decls in _qss_blocks(styles.build_stylesheet("dark")):
        if "#pill-" not in selector:
            continue
        for decl in decls:
            if decl.split(":")[0].strip().lower() in {"padding", "font-size", "border-radius"}:
                offenders.append(f"{selector} ⇒ {decl}")
    assert not offenders, (
        "外观族块里写了尺寸（按优先级会把档位整体压掉，按钮显示不全）：\n  "
        + "\n  ".join(offenders)
    )
    # 自证：同一套抠块逻辑喂一个违规块，必须认出来（否则上面那条可能是「一块都没抠到」）
    probe = "QPushButton#pill-ghost { background: transparent; padding: 6px 14px; }"
    caught = [
        d for s, ds in _qss_blocks(probe) if "#pill-" in s for d in ds
        if d.split(":")[0].strip().lower() in {"padding", "font-size", "border-radius"}
    ]
    assert caught == ["padding: 6px 14px"], caught


# ---- 行内控件的高度：下拉 / 输入框与按钮同一格（2026-09-22）----
#
# 来历：用户上报 hosts 管理窗里「『全部来源』『全部分类』与其它元素不协调」。量出来那一行的
# 三种控件是**三种高度**：输入框 34（QSS 只写 `padding: 7px 10px`，自然高没人管）、
# 下拉 24（**一条 QSS 都没有** ⇒ macOS 原生渲染，另一种描边与底色）、按钮 28。
# 下拉尤其特殊：统一样式此前只由 `apply_combo_style` 提供，而它只有
# `ProfileSelector` / `ChoiceSelector` 两处记得调 —— 其余下拉（hosts 管理面、
# 设置页表单）拿到的是裸 `QComboBox`。故样式与定高改由 `ComboField` 自己戴上。

def test_a_combo_field_carries_its_own_style_and_height():
    """下拉**自己**戴上统一样式与定高：不靠每个调用方记得调一次 ``apply_combo_style``。

    控制组是裸 ``QComboBox``：它一条 QSS 都没有、高度也不是 ``CONTROL_H`` —— 若这条
    断言在它身上同样成立，说明量到的不是「ComboField 戴上的东西」，而是原生默认值。
    """
    combo = ui_kit.ComboField()
    combo.addItem("全部来源", "")
    assert "QComboBox" in combo.styleSheet(), "没有样式 ⇒ 走 macOS 原生渲染（24px，另一套观感）"
    assert combo.minimumHeight() == metrics.CONTROL_H
    assert combo.maximumHeight() == metrics.CONTROL_H

    bare = QComboBox()
    bare.addItem("全部来源", "")
    assert bare.styleSheet() == "", "控制组：裸 QComboBox 不该有样式（否则这条判据是哑的）"
    assert (bare.minimumHeight(), bare.maximumHeight()) != (
        metrics.CONTROL_H, metrics.CONTROL_H), "控制组：裸 QComboBox 不该是定高"


def test_a_combo_field_paints_its_arrow():
    """下拉的箭头必须**真的画出来**（像素级判据）。

    QSS 接管 ``drop-down`` 后 Qt 不再画默认箭头；而下拉与输入框现在是同一个模子，
    没箭头就读成「一个输入框」。判据抓箭头色（``text2``）在右侧槽位里的像素覆盖 ——
    尺寸类断言抓不到「没画」这种事。主题必须走 ``apply_theme``（缓存 + 全局 QSS 一起换）：
    paintEvent 里的箭头色**现读缓存**，只 setStyleSheet 的话两边主题脱节。
    """
    app = QApplication.instance() or QApplication([])
    styles.apply_theme(app, "dark")
    combo = ui_kit.ComboField()
    combo.addItem("全部来源", "")
    combo.resize(combo.sizeHint().width(), metrics.CONTROL_H)
    combo.show()
    app.processEvents()
    img = combo.grab().toImage()
    found = _pixel_near(img, _hex(styles.TOKENS["dark"]["text2"]),
                        x_range=(img.width() - 26, img.width() - 6))
    combo.hide()
    assert found, "右侧槽位里找不到箭头 —— chevron 没画（读起来像一个输入框）"

    # 控制组：跳过 paintEvent 的箭头那一段，同区域必须抓不到 —— 否则判据是哑的
    class _NoArrow(ui_kit.ComboField):
        def paintEvent(self, event):  # noqa: N802  （Qt 命名）
            super(ui_kit.ComboField, self).paintEvent(event)

    blank = _NoArrow()
    blank.addItem("全部来源", "")
    blank.resize(blank.sizeHint().width(), metrics.CONTROL_H)
    blank.show()
    app.processEvents()
    img2 = blank.grab().toImage()
    blank.hide()
    assert not _pixel_near(img2, _hex(styles.TOKENS["dark"]["text2"]),
                           x_range=(img2.width() - 26, img2.width() - 6)), (
        "控制组失败：不画箭头也能命中 —— 判据抓的是别的东西")


def _hex(css_color: str) -> tuple[int, int, int]:
    """``#rrggbb`` → (r, g, b)。"""
    value = css_color.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _pixel_near(img, want: tuple[int, int, int], *, x_range, tol: int = 60) -> bool:
    """图里 ``x_range`` × 全高范围内有没有接近 ``want`` 的像素（曼哈顿距离）。"""
    for yy in range(img.height()):
        for xx in range(max(x_range[0], 0), min(x_range[1], img.width())):
            c = img.pixelColor(xx, yy)
            if (abs(c.red() - want[0]) + abs(c.green() - want[1])
                    + abs(c.blue() - want[2])) <= tol:
                return True
    return False


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_a_line_edit_fits_the_control_height(theme):
    """单行输入框的高度钉在 ``CONTROL_H``（28）：与同一行的按钮 / 下拉同一格。

    此前只写 ``padding: 7px 10px`` ⇒ 自然高 34，比按钮高 6px（量 `sizeHint()` 而不是
    读 QSS 文本：内边距一旦被改回去，控件会静默长高，不报任何错）。
    """
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(styles.build_stylesheet(theme))
    try:
        field = QLineEdit()
        field.setPlaceholderText("搜索域名或 IP…")
        assert field.sizeHint().height() == metrics.CONTROL_H
    finally:
        app.setStyleSheet("")


def test_the_field_height_check_catches_the_old_padding():
    """自证（控制组 = 旧行为）：旧的那句 ``padding: 7px 10px`` 下，上一条判据必须红。

    否则上一条只是「QLineEdit 恰好这么高」的复述，改回旧写法时它照样绿。
    """
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(styles.build_stylesheet("dark")
                      + "QLineEdit { padding: 7px 10px; min-height: 0px; }")
    try:
        field = QLineEdit()
        field.setPlaceholderText("搜索域名或 IP…")
        assert field.sizeHint().height() > metrics.CONTROL_H, "旧内边距下输入框该比 CONTROL_H 高"
    finally:
        app.setStyleSheet("")


# ---- 标签 / 行内容器不许自带实底（2026-09-23）----
#
# 来历：全局那条 ``QWidget`` 是**类型选择器**，Qt 让它命中子类 ⇒ 每枚 ``QLabel``、
# 每个**裸 QWidget 容器**都自带一块 ``surface`` 实底。压在 surface 上看不出来；压在
# **自绘底**上就是「字底下蒙了一层白」—— 用户 2026-09-23 在设置窗「快捷键」页上报
# （hover 行是 ``accent_soft`` 淡紫底、字底下却是白的），同批量出熔断红卡
# （``#breach_text`` 从没声明过底）上也是白块，面板状态行的嵌套容器同理。
#
# 两条判据分工，缺一条都会重新长出这个 bug：
#   ① **次序**（静态）：通用透明条与 ``QWidget`` 同优先级 ⇒ Qt 按「后声明者胜」裁决，
#      排到它前面等于没写。这条守的就是那个「等于没写」；
#   ② **像素**（结果）：真的画出来是什么颜色。次序对了但选择器名/属性写错，只有它抓得住。


def test_the_label_transparency_rule_comes_after_the_base_widget_block():
    """① 通用 ``QLabel`` 透明条必须**排在**基础 ``QWidget`` 块之后（同优先级，后者胜）。"""
    # 先抠掉注释：`_qss_blocks` 不认注释，块前面的说明会被并进选择器里。
    css = re.sub(r"/\*.*?\*/", "", styles.build_stylesheet("dark"), flags=re.S)
    blocks = _qss_blocks(css)
    selectors = [" ".join(sel.split()) for sel, _ in blocks]
    assert "QLabel" in selectors, (
        "没有通用的 QLabel 透明条 —— 行里每一枚标签都会顶着一块 surface 实底"
    )
    base = selectors.index("QWidget")
    label = selectors.index("QLabel")
    assert label > base, (
        "QLabel 透明条排在了基础 QWidget 块之前 ⇒ 同优先级下被它盖掉（等于没写）"
    )
    decls = [d.replace(" ", "") for d in blocks[label][1]]
    assert "background:transparent" in decls, (
        f"QLabel 那条不是透明底：{blocks[label][1]}"
    )
    # 行内容器（面板与 StatusRow 的名称/副标题就装在里面）同样要透明。
    assert "QWidget#status-main" in " ".join(selectors), (
        "行内容器没有透明条 —— 裸 QWidget 也吃那条类型选择器，容器范围里量到的是 surface"
    )


def _hover_row_with_labels():
    """按真实结构拼一行：容器里的标签（面板形态）＋ 直接贴行的标签（设置页形态）。"""
    row = ui_kit.HoverRow(lambda: None)
    lay = QHBoxLayout(row)
    lay.setContentsMargins(10, 6, 10, 6)
    lay.setSpacing(8)

    box = QWidget(row)
    box.setObjectName("status-main")
    vertical = QVBoxLayout(box)
    vertical.setContentsMargins(0, 0, 0, 0)
    for text, role in (("录屏通知", "section"), ("规则 3 条 · 命中 0 次", "caption")):
        lbl = QLabel(text, box)
        ui_kit.apply_text_role(lbl, role)
        vertical.addWidget(lbl)
    lay.addWidget(box, stretch=1)

    tail = QLabel("框架 · 打开主面板", row)
    ui_kit.apply_text_role(tail, "caption")
    lay.addWidget(tail, stretch=1)

    row.resize(420, 64)
    return row, (box, tail)


def _measure_hover_row(app, row, widgets, theme: str) -> tuple[str, list[tuple]]:
    """显示 → 走 Enter（与鼠标进入同一条路径）→ 抓图 → 在**每个控件的右下角内侧**取样。

    右下角是**没有字**的地方（标签都顶左对齐），量到的就是底色本身。
    返回 ``(这一行的填充色, [(取样点, 量到的色), …])``。
    """
    row.show()
    app.processEvents()
    QApplication.sendEvent(row, QEvent(QEvent.Type.Enter))
    app.processEvents()
    img = row.grab().toImage()
    row.hide()

    fill = styles.TOKENS[theme]["accent_soft"].lower()
    assert img.pixelColor(2, img.height() // 2).name().lower() == fill, (
        "控制点不成：行里压根没上填充色 ⇒ 下面的取样没意义（`_hovered` 没置上？）"
    )
    spots = []
    for w in widgets:
        origin = w.mapTo(row, QPoint(0, 0))
        pt = (origin.x() + w.width() - 6, origin.y() + w.height() - 6)
        spots.append((pt, img.pixelColor(*pt).name().lower()))
    return fill, spots


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_labels_do_not_paint_over_the_hover_fill(qtbot, theme, monkeypatch):
    """② hover 行里**每一处**都该是这一行的填充色（像素级判据）。"""
    app = QApplication.instance() or QApplication([])
    before = styles.current_theme()
    styles.apply_theme(app, theme)     # 角色 QSS 取 current_theme()（认覆盖）
    # 行的自绘底走 ``ui_kit._theme_tokens()``，那条只读**系统外观** ⇒ 与上面对齐到同一主题，
    # 否则深色那一轮量到的是浅色的填充色（本机就是浅色），断言会指向假失败。
    monkeypatch.setattr(ui_kit.base, "_theme_tokens", lambda: styles.TOKENS[theme])
    row, widgets = _hover_row_with_labels()
    try:
        fill, spots = _measure_hover_row(app, row, widgets, theme)
        bad = [f"{pt} = {got}（该是 {fill}）" for pt, got in spots if got != fill]
        assert not bad, (
            "hover 底被行内的标签 / 容器盖住了（字底下蒙了一层实底）：\n  " + "\n  ".join(bad)
        )
    finally:
        row.setParent(None)
        styles.apply_theme(app, before)


def test_the_hover_fill_check_catches_the_old_stylesheet(qtbot, monkeypatch):
    """自证（控制组 = 旧行为）：把实底加回去，上一条必须红。

    加法的形态就是 2026-09-23 之前那张表：既没有通用 ``QLabel`` 透明条，也没有行内容器
    那条 ⇒ 在样式表**末尾**补两条同效规则（一条同优先级靠后声明、一条优先级更高），
    复现的就是当时那份表（当时量到的就是白块）。
    """
    app = QApplication.instance() or QApplication([])
    before = styles.current_theme()
    styles.apply_theme(app, "light")
    monkeypatch.setattr(ui_kit.base, "_theme_tokens", lambda: styles.TOKENS["light"])
    t = styles.TOKENS["light"]
    app.setStyleSheet(styles.build_stylesheet("light")
                      + f"QLabel {{ background: {t['surface']}; }}"
                      + f"QWidget#status-main, QWidget#status-main QWidget "
                        f"{{ background: {t['surface']}; }}")
    row, widgets = _hover_row_with_labels()
    try:
        fill, spots = _measure_hover_row(app, row, widgets, "light")
        white = [pt for pt, got in spots if got == t["surface"].lower()]
        assert white, (
            f"控制组失败：把实底加回去却量不到白块（取样点 {spots}）—— 上一条判据抓的是别的东西"
        )
    finally:
        row.setParent(None)
        styles.apply_theme(app, before)
