"""shell/design：设计系统真源测试（批次 0）。

覆盖：tokens 单一真源（ui/styles re-export 同一对象）、语义色映射完备、
worst_level 严重度序、语义图标渲染（offscreen）。
"""

from __future__ import annotations

import re
import itertools

import pytest

from barkit.contribution.specs import IconName, StateLevel
from barkit.shell.design import levels as levels_mod
from barkit.shell.design import tokens as tokens_mod
from barkit.shell.design.tokens import THEMES, TOKENS, token


# ──────────────────────────────────────────────
# tokens 单一真源
# ──────────────────────────────────────────────

def test_ui_styles_reexports_same_object():
    """ui/styles.TOKENS 必须与 shell/design/tokens.TOKENS 是同一对象（零副本）。"""
    from barkit.shell import styles as legacy_styles

    assert legacy_styles.TOKENS is tokens_mod.TOKENS


def test_tokens_shape():
    assert THEMES == ("light", "dark")
    # 每个键只承担**一个**角色（详见 shell/design/tokens.py 的四档表）。这张表是
    # **故意的确认点**：加/删令牌必须来这里改一行，改的时候你会被迫问一句
    # 「它属于哪一档、对比度契约里的哪条配对」。
    expected_keys = {
        # 实体档（面上压 on_solid 白字）
        "accent", "accent_hover", "red_solid", "red_solid_hover",
        # 图形档（面上压白图形：角标圆盘 / 行首级别图标 / 开关滑块）
        "green", "orange", "red",
        # 前景档（自身就是文字 / 图标 / 描边）
        "accent_text", "accent_text_hover", "green_text", "orange_text", "red_text",
        # 浅底档
        "accent_soft", "green_soft", "orange_soft", "red_soft",
        # 实心块上的前景 + 中性
        "on_solid",
        "text1", "text2", "text3", "surface", "surface2", "surface3", "border",
        "border_strong",
    }
    #: ``shadow_pop`` 2026-09-21 已删（随 Phase 1 玻璃 A 一并退场）：唯一消费方
    #: 是 ``Card`` 的玻璃软投影，而那层投影正是用户判为「非常丑」的东西。
    #: 令牌真源见 ``shell/design/tokens.py`` 的四档表。
    assert "shadow_pop" not in TOKENS["light"], "软投影令牌已删，别让它回来"
    for theme in THEMES:
        assert set(TOKENS[theme]) == expected_keys, f"{theme} 令牌键漂移"


def test_token_lookup_and_color_format():
    for theme in THEMES:
        for name, value in TOKENS[theme].items():
            assert value.startswith("#"), f"{theme}.{name} 非十六进制色：{value}"
    with pytest.raises(KeyError):
        token("not_a_token")


# ──────────────────────────────────────────────
# 语义色四档 · 对比度契约（2026-09-19）
# ──────────────────────────────────────────────
#
# 这条门禁守 `tokens.py` 那张「一符四档」的表：**每个键只有一个角色，且该角色的
# 对比度真的达标**。它存在的原因是可量的 —— 上一版把填充色拿去写字（浅色下
# 绿 2.01 / 橙 1.87 / 红 3.05）、把深色 accent 拿去当实心底（白字 3.44），
# 共 **36 格**不达标，而**没有任何门禁看得见**（`test_no_hardcoded_colors` 只管
# 「有没有硬编码」，不管「配得对不对」）。
#
# 门槛取自 WCAG：文字 4.5:1（1.4.3）、非文本图形 3.0:1（1.4.11）。
# 配对**按档的后缀规则推**，不手抄站点清单 —— 手抄的清单会随消费点漂移，而
# 「新增一个语义键 / 改了消费点的底」这类漏洞正好漏在最需要守的地方。

_SOLID_TIER = ("accent", "accent_hover", "red_solid", "red_solid_hover")
_GRAPHIC_TIER = ("green", "orange", "red")
#: 图形档还会被当成**图标 / 圆点**画在主题底上（行首级别图标落在 `surface3` 的盒里）
_GRAPHIC_BACKDROPS = ("on_solid", "surface", "surface3")
_MIN_TEXT, _MIN_GRAPHIC = 4.5, 3.0


def _relative_luminance(hexcolor: str) -> float:
    """WCAG 相对亮度（sRGB 线性化后的加权和）。"""
    v = hexcolor.lstrip("#")
    chans = [int(v[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in chans]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast_ratio(a: str, b: str) -> float:
    la, lb = _relative_luminance(a), _relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _semantic_pairs(table: dict[str, str]) -> list[tuple[str, str, float]]:
    """一张主题表 → 全部 (前景键, 背景键, 门槛)。按档的规则推，不是站点清单。"""
    pairs: list[tuple[str, str, float]] = [
        (key, "on_solid", _MIN_TEXT) for key in _SOLID_TIER
    ]
    pairs += [
        (key, bg, _MIN_GRAPHIC) for key in _GRAPHIC_TIER for bg in _GRAPHIC_BACKDROPS
    ]
    for key in table:
        if key.endswith("_text_hover"):
            stem, on_surface = key[: -len("_text_hover")], False
        elif key.endswith("_text"):
            stem, on_surface = key[: -len("_text")], True
        else:
            continue
        soft = f"{stem}_soft"
        if soft in table:
            pairs.append((key, soft, _MIN_TEXT))
        if on_surface:
            pairs.append((key, "surface", _MIN_TEXT))
    return pairs


def _contrast_violations(tokens: dict) -> list[str]:
    """不达标清单。**纯函数** —— 自证要拿它喂坏数据（不碰生产文件）。"""
    bad: list[str] = []
    for theme in THEMES:
        table = tokens[theme]
        for fg, bg, need in _semantic_pairs(table):
            got = _contrast_ratio(table[fg], table[bg])
            if got < need:
                bad.append(f"{theme}: {fg} on {bg} = {got:.2f} < {need}")
    return bad


def test_semantic_tiers_meet_contrast_contract():
    assert _contrast_violations(TOKENS) == []


def test_icon_tones_are_readable_in_both_themes():
    """行首图标盒的三档配色：**每一档的字形压在自家底上都要过图形阈值**（≥3.0）。

    ``ICON_TONES`` 是「一条记录打哪儿来」的配色表（框架 / 组件 / 我的），2026-09-24 起
    快捷键页拿它给每组来源上色。它不像语义色那样按档的后缀推规则（键是中性 / 品牌混用
    的），故**只能单独守**：三档里两档与语义档重合（``accent`` / ``solid`` 已被上面的
    契约守住），第三档用的是中性键（``text2`` 画在 ``surface3`` 上），落在契约之外 ——
    没有这条，把中性档调成看不清只需要改一个令牌值，而界面上不报错。
    """
    from barkit.shell.ui_kit import ICON_TONES

    bad: list[str] = []
    for theme in THEMES:
        table = TOKENS[theme]
        for tone, (bg_key, fg_key) in ICON_TONES.items():
            got = _contrast_ratio(table[fg_key], table[bg_key])
            if got < _MIN_GRAPHIC:
                bad.append(f"{theme}: {tone} = {fg_key} on {bg_key} {got:.2f} < {_MIN_GRAPHIC}")
    assert not bad, bad


def test_semantic_pairs_cover_every_tier_key():
    """面**不多不少**：规则推出的前景键集合 == 语义色键去掉浅底档。

    少了 ⇒ 有个语义键没人守（典型：新加了键却没归到某一档）；
    多了 ⇒ 规则指向了不存在的键（典型：改名后档表没跟上）。
    """
    semantic = {
        k for k in TOKENS["light"] if k.split("_")[0] in {"accent", "green", "orange", "red"}
    }
    soft = {k for k in TOKENS["light"] if k.endswith("_soft")}
    covered = {fg for fg, _bg, _need in _semantic_pairs(TOKENS["light"])}
    missing = (semantic - soft) - covered
    extra = covered - (semantic - soft)
    assert not missing, f"这些语义键没有任何对比度配对（新键要归到某一档）：{sorted(missing)}"
    assert not extra, f"配对指向了不存在的档键：{sorted(extra)}"


def test_contrast_contract_catches_violations():
    """自证：判据真的会红，且指得到键。三条对照都是上一版的**真实错法**。"""
    import copy

    assert _contrast_violations(TOKENS) == [], "合规现状被误报"

    # ① 前景档换回同色相的**图形档**（上一版：`pill_ok { color: green }`）
    bad_fg = copy.deepcopy(TOKENS)
    for theme in THEMES:
        bad_fg[theme]["green_text"] = bad_fg[theme]["green"]
    lines = _contrast_violations(bad_fg)
    assert any("green_text" in line for line in lines), lines

    # ② 实体档填成背景色（白字压白底）
    bad_fill = copy.deepcopy(TOKENS)
    for theme in THEMES:
        bad_fill[theme]["accent"] = bad_fill[theme]["on_solid"]
    assert any("accent on on_solid" in line for line in _contrast_violations(bad_fill))

    # ③ 图形档填亮（白滑块糊在轨道里）
    bad_graphic = copy.deepcopy(TOKENS)
    for theme in THEMES:
        bad_graphic[theme]["green"] = "#34C759"
    assert any("green on on_solid" in line for line in _contrast_violations(bad_graphic))


# ──────────────────────────────────────────────
# levels
# ──────────────────────────────────────────────

def test_level_color_covers_all_levels():
    """语义色映射必须覆盖全部 StateLevel（新增级别漏配色 = 渲染崩）。"""
    assert set(levels_mod.LEVEL_COLOR) == set(StateLevel)


def test_level_colors_resolve_via_tokens():
    for level in StateLevel:
        fg_name, bg_name = levels_mod.LEVEL_COLOR[level]
        for theme in THEMES:
            fg, bg = levels_mod.level_colors(level, theme)
            assert fg == TOKENS[theme][fg_name]
            assert bg == TOKENS[theme][bg_name]


def test_level_color_foregrounds_survive_the_graphic_floor():
    """`LEVEL_COLOR` 是**第二个**「键 → 角色」绑定点，角色由消费方定死：三处都拿这个
    前景去**染图标**（`panel/widgets.py` 的 `_TitleBarIcon.paintEvent`，走统一出口
    `draw_glyph`）或**画实心圆盘再压白字形**（同文件 `_ValueBadge.paintEvent` 的
    `drawEllipse` ＋ `draw_badge_glyph(..., QColor(255, 255, 255))`；菜单栏角标同款画法），
    **没有一处拿它写字** ⇒ 每格都要过图形档 3.0。

    档契约（`_semantic_pairs`）只管 `tokens` 内部，**管不到这张表选没选对档** ——
    这正是「一符两用」的另一个入口：把 `OK` 指到 `green_soft`（浅底档）不会让任何
    现有用例变红，圆盘却会在白字形下整个糊掉。`IDLE` / `OFF` 用的 `text3` 也在
    这条约束里（浅色 3.26 / 深色 5.07）—— 它虽归装饰档，被拿来当圆盘色时同样要压得住。
    """
    for level in StateLevel:
        fg_name, _bg_name = levels_mod.LEVEL_COLOR[level]
        for theme in THEMES:
            got = _contrast_ratio(TOKENS[theme][fg_name], TOKENS[theme]["on_solid"])
            assert got >= _MIN_GRAPHIC, (
                f"{theme}: LEVEL_COLOR[{level.value}] 的前景 {fg_name} 压白只有 "
                f"{got:.2f} < {_MIN_GRAPHIC}（它要染图形 / 画圆盘压白字形，不是文字）"
            )
        assert fg_name in TOKENS["light"], f"LEVEL_COLOR[{level.value}] 指向了不存在的键"


def test_worst_level_severity_order():
    assert levels_mod.worst_level([]) is None
    assert levels_mod.worst_level([StateLevel.OK, StateLevel.IDLE]) is StateLevel.IDLE
    assert levels_mod.worst_level([StateLevel.OK, StateLevel.WARN]) is StateLevel.WARN
    assert levels_mod.worst_level([StateLevel.WARN, StateLevel.ERR]) is StateLevel.ERR
    assert levels_mod.worst_level([StateLevel.OFF, StateLevel.OK]) is StateLevel.OFF
    assert levels_mod.worst_level(
        [StateLevel.OK, StateLevel.WARN, StateLevel.ERR, StateLevel.IDLE]
    ) is StateLevel.ERR


# ──────────────────────────────────────────────
# icons
# ──────────────────────────────────────────────

def test_glyphs_cover_all_icon_names():
    from barkit.shell.design import icons as icons_mod

    assert set(icons_mod._GLYPHS) == set(IconName)


# 字形风格契约（P0①）：这条门禁守的是「图标库是**一套**视觉语言」，不是单个字形好看。
# 违反的后果都不是报错，而是**静默画错**（黑色填充 / 主题不跟随 / 视窗不一致），
# 故必须由门禁拦，不能靠人眼在每个调用点复查。

_HEX_OR_FUNC_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(")
_ALLOWED_GLYPH_TAGS = {"path", "rect", "circle", "ellipse", "line", "polyline", "polygon"}


def _glyph_style_violations(fragment: str, markup: str) -> list[str]:
    """字形片段 + 它渲染出的 SVG 文本 → 违反风格约定的原因列表（空 = 合规）。

    刻意做成纯函数（不读模块状态）：这样同一份判据既能扫真实字形，也能被
    「注入违规的假字形」反过来证明它真的会红（见下面那条自证用例）。
    """
    problems: list[str] = []
    if _HEX_OR_FUNC_COLOR.search(fragment):
        problems.append("字形内出现颜色字面量：颜色应由 _svg_markup 统一注入，或写 currentColor")
    if "currentColor" in fragment and 'style="color:' not in markup:
        problems.append('用了 currentColor 但根节点没有 style="color:…"：会渲染成黑色')
    for m in re.finditer(r"<([a-zA-Z]+)", fragment):
        if m.group(1) not in _ALLOWED_GLYPH_TAGS:
            problems.append(f"字形出现非几何元素 <{m.group(1)}>")
    if "stroke-width" in fragment:
        problems.append("字形不得自行覆盖 stroke-width：粗细由 _svg_markup 统一给出")
    return problems


def test_glyph_style_contract():
    """全部字形：24 视窗 + 统一描边 + 无颜色字面量（风格漂移的门禁）。"""
    from barkit.shell.design import icons as icons_mod

    for name in IconName:
        frag = icons_mod._GLYPHS[name]
        markup = icons_mod._svg_markup(name, "#5E5CE6")
        assert not _glyph_style_violations(frag, markup), f"{name}: {frag}"
        assert 'viewBox="0 0 24 24"' in markup, f"{name} 未走 24 网格视窗"
        assert 'stroke-width="1.6"' in markup, f"{name} 丢失统一描边粗细"
        assert re.search(r"<svg[^>]*>", markup), f"{name} 渲染文本不是 SVG"
        assert markup.rstrip().endswith("</svg>") and markup.count("</svg>") == 1, (
            f"{name} SVG 结构不闭合"
        )


def test_glyph_style_contract_catches_violations():
    """自证：合规字形不误报，三类违规各抓一条（门禁不许退化成恒真）。"""
    from barkit.shell.design import icons as icons_mod

    # 合规基线：真实字形的渲染文本必须零问题（用两枚代表：线性 + 实心填充）
    for name in (IconName.GEAR, IconName.DISPLAY_ON):
        assert _glyph_style_violations(
            icons_mod._GLYPHS[name], icons_mod._svg_markup(name, "#5E5CE6")
        ) == []

    good = '<rect x="4" y="4" width="16" height="16" rx="2" fill="currentColor" stroke="none"/>'
    rooted = icons_mod._svg_markup(IconName.GEAR, "#5E5CE6")  # 带 style="color:…" 的根

    cases = [
        # ① 颜色字面量
        ('<rect x="4" y="4" width="16" height="16" fill="#ff0000"/>', rooted),
        # ② currentColor 但根节点没给当前颜色（本轮实测踩到的坑：会画成黑色）
        (good, '<svg viewBox="0 0 24 24" fill="none" stroke="#000000">' + good + "</svg>"),
        # ③ 非几何元素
        ('<text x="4" y="20">A</text>', rooted),
        # ④ 自行覆盖描边粗细
        ('<path d="M4 4h16" stroke-width="3"/>', rooted),
    ]
    for i, (frag, markup) in enumerate(cases, 1):
        assert _glyph_style_violations(frag, markup), f"第 {i} 类违规未被抓到"


def test_display_glyphs_encode_on_off_and_follow_color(qapp):
    """屏幕字形的两件事必须成立：① 填充跟随调用方颜色 ② 实心/空心可区分。

    P0① 的两枚字形是「托盘底图换源」的前提（底图直接用主字形），所以
    「渲染成黑色」与「实心空心画得一样」都是**功能性**失败，不只是视觉问题。
    """
    from barkit.shell.design.icons import pixmap

    on = pixmap(IconName.DISPLAY_ON, 48, "#ff3b30").toImage()
    off = pixmap(IconName.DISPLAY_OFF, 48, "#ff3b30").toImage()
    center = (24, 24)          # 屏内中心（24 网格里的 (12,12)）
    on_c = on.pixelColor(*center)
    assert on_c.alpha() > 200, "实心屏的屏内未填充"
    assert on_c.red() > 200 and on_c.green() < 100, f"填充未跟随调用方颜色：{on_c.name()}"
    assert off.pixelColor(*center).alpha() < 200, "空心屏的屏内不该有填充"


def test_battery_family_shares_one_frame():
    """电池三枚（D19 领域变体）必须**共用同一外框与正极触点**，差异只在框内。

    外框各写一份，三枚就会各自漂移（圆角、框宽、触点位置各差零点几），取值翻转
    时轮廓会跳 —— 而「轮廓不动、只有内部明暗变」正是 `DISPLAY_ON`/`DISPLAY_OFF`
    定下的做法，也是「托盘底图换源」能用的前提。写死三份正是让这条门禁能钉住
    「它们是同一只电池」，而不是三只长得像的电池。
    """
    import re as _re

    from barkit.shell.design import icons as icons_mod

    names = (IconName.BATTERY, IconName.BATTERY_LOW, IconName.BATTERY_CHARGING)
    parts = {n: _re.findall(r"<[^>]+>", icons_mod._GLYPHS[n]) for n in names}

    frames = {n: p[0] for n, p in parts.items()}
    nubs = {n: p[-1] for n, p in parts.items()}
    assert len(set(frames.values())) == 1, f"电池三枚的外框不一致：{frames}"
    assert len(set(nubs.values())) == 1, f"电池三枚的正极触点不一致：{nubs}"

    # 反恒真：框内必须**真的不同**，否则「三枚」是同一张图抄三遍，
    # 上面两条一致性断言全绿却毫无信息。
    bodies = {tuple(p[1:-1]) for p in parts.values()}
    assert len(bodies) == 3, "电池三枚的框内内容必须互不相同"
    assert () in bodies, "应有一枚框内留空（BATTERY 是族里的基础字形）"


def test_icon_pixmap_renders_opaque(qapp):
    from barkit.shell.design.icons import pixmap

    for name in IconName:
        pm = pixmap(name, 32, "#5E5CE6")
        assert not pm.isNull()
        assert pm.size().width() == 32 and pm.size().height() == 32
        # 透明底 + 有笔画 → 至少一个非全透明像素
        img = pm.toImage()
        assert any(
            img.pixelColor(x, y).alpha() != 0
            for x in range(0, 32, 4)
            for y in range(0, 32, 4)
        ), f"{name} 渲染为空白"


# ──────────────────────────────────────────────
# 角标字形族（12 网格简化版 · 决议 D19 / 批次 12 · P0①）
# ──────────────────────────────────────────────
#
# 这一族存在的理由是可测的，不是审美偏好：24 网格主字形在 14px 角标位上的有效笔画
# 约 0.93px，12 网格同数字的笔画约 1.87px（见下面的墨量对比用例）。所以门禁守的不是
# 「字形好看」，而是**两族没有混用** —— 混用的后果是静默画糊，不报错。

_BADGE_GRID = 12
_BADGE_MAX_SEGMENTS = 3

_BADGE_DRAW_TAGS = ("path", "rect", "circle", "ellipse", "line", "polyline", "polygon")


def _badge_segments(fragment: str) -> int:
    """角标字形的**笔画段数**：非 path 元素各算 1 段，每条 path 的每个 ``M`` 各算 1 段。

    按元素个数数会漏掉「一条 path 里塞 4 个子路径」这种写法（它看起来是 1 段，
    画出来是 4 笔）——所以必须数子路径。
    """
    segments = 0
    for m in re.finditer(rf"<({'|'.join(_BADGE_DRAW_TAGS)})\b([^>]*)>", fragment):
        if m.group(1) == "path":
            segments += len(re.findall(r"[Mm]", m.group(2)))
        else:
            segments += 1
    return segments


def _badge_style_violations(fragment: str, markup: str) -> list[str]:
    """角标字形片段 + 渲染文本 → 违反「12 网格简化族」约定的原因（空 = 合规）。

    与主字形判据分开，是因为**唯一会漏的错法恰好是网格不合**：一个 24 网格片段放进
    角标表时，标签、颜色、描边三项全合规（`_glyph_style_violations` 查不出来），
    只有视窗与坐标越界能暴露它。合成一条判据会让这类静默降级漏网。
    """
    problems = _glyph_style_violations(fragment, markup)
    if f'viewBox="0 0 {_BADGE_GRID} {_BADGE_GRID}"' not in markup:
        problems.append("角标字形未走 12 网格视窗")
    segments = _badge_segments(fragment)
    if segments > _BADGE_MAX_SEGMENTS:
        problems.append(
            f"角标字形 {segments} 段，超过 {_BADGE_MAX_SEGMENTS} 段上限（14px 圆里会成一团墨点）"
        )
    # 数字必须认得**省略前导零**的写法（``.88`` / ``.01``）：SVG path 里这是常规写法，
    # 而老正则只认 ``\d+`` 开头 ⇒ ``.88`` 会被读成 ``88``，当场误报「坐标 88 越出
    # 12 网格」。本仓字形本来就写 ``l.9 1.4`` / ``h.01`` —— 它们没炸只是因为 9 /
    # 01 恰好落在界内，属于**蒙对的**，换成 .88 就露馅（2026-09-21 改铃时踩到）。
    for num in re.findall(r"-?(?:\d+(?:\.\d+)?|\.\d+)", fragment):
        if not 0 <= float(num) <= _BADGE_GRID:
            problems.append(f"角标字形坐标 {num} 越出 {_BADGE_GRID} 网格")
            break
    return problems


def test_badge_glyphs_are_main_glyph_subset():
    """角标族不得引入主字形表里没有的名字（同名是**同一枚图标**的小尺寸资源）。

    角标名与取值图标名共用一个 ``IconName``：组件把同一枚图标声明成 ``StateOption.icon``
    时，标题栏 / 面板行走的是 ``pixmap()``（主字形）。若某个名字只在角标表里，
    该组件在标题栏就会 ``KeyError`` —— 而且只在「那台状态机正好取到那个值」时才炸。
    """
    from barkit.shell.design import icons as icons_mod

    assert set(icons_mod._BADGE_GLYPHS) <= set(icons_mod._GLYPHS)


def test_badge_style_contract():
    """角标字形：12 视窗 + 统一描边 + ≤3 段 + 坐标不越界 + 无颜色字面量。"""
    from barkit.shell.design import icons as icons_mod

    assert icons_mod._BADGE_GLYPHS, "角标字形族为空"
    for name, frag in icons_mod._BADGE_GLYPHS.items():
        markup = icons_mod._badge_svg_markup(name, "#FFFFFF")
        assert not _badge_style_violations(frag, markup), (
            f"{name}: {_badge_style_violations(frag, markup)}"
        )
        assert 'stroke-width="1.6"' in markup, f"{name} 丢失统一描边粗细"
        assert markup.rstrip().endswith("</svg>") and markup.count("</svg>") == 1, (
            f"{name} SVG 结构不闭合"
        )


def test_badge_style_contract_catches_violations():
    """自证：合规不误报，四类违规各抓一条（门禁不许退化成恒真）。"""
    from barkit.shell.design import icons as icons_mod

    good_frag = icons_mod._BADGE_GLYPHS[IconName.MARK_OK]
    good_markup = icons_mod._badge_svg_markup(IconName.MARK_OK, "#FFFFFF")
    assert _badge_style_violations(good_frag, good_markup) == []

    # 计数器自身也要自证：一条 path 里塞 2 个子路径 = 2 段（按元素数会数成 1）
    assert _badge_segments('<path d="M1 1h1M2 2h1"/>') == 2
    assert _badge_segments('<circle cx="6" cy="6" r="4"/><path d="M3 3 9 9"/>') == 2

    # 省略前导零的小数**不算越界**（老正则会把 .88 读成 88 ⇒ 误报）
    assert not _badge_style_violations(
        '<path d="M2.98 8.14l.88 1.49"/>', good_markup
    ), "l.88 这类常规写法不该被读成 88"

    main_markup = icons_mod._svg_markup(IconName.MARK_OK, "#FFFFFF")
    cases = [
        # ① 把**主字形片段**放进角标族（标签 / 颜色 / 描边全合规，只有网格不合）
        (good_frag, main_markup, "24 网格片段进了角标族"),
        # ② 段数超限
        ('<path d="M1 1h10M1 4h10M1 7h10M1 10h10"/>', good_markup, "超过 3 段"),
        # ③ 坐标越出 12 网格
        ('<path d="M2 2 22 22"/>', good_markup, "坐标越出 12 网格"),
        # ④ 颜色字面量（沿用主字形判据的那条）
        ('<circle cx="6" cy="6" r="4" fill="#ff0000"/>', good_markup, "颜色字面量"),
    ]
    for frag, markup, why in cases:
        assert _badge_style_violations(frag, markup), f"未抓到：{why}"


def test_level_mark_covers_all_levels():
    """5 枚通用级别标记必须与 StateLevel 一一对应（漏一个 = 该级别角标 KeyError）。

    这条是 D19 的「兜底规则」能不能成立的前提：任何组件都能只声明级别就拿到角标。
    """
    from barkit.shell.design import icons as icons_mod

    assert set(icons_mod._LEVEL_MARK) == set(StateLevel)
    for level, name in icons_mod._LEVEL_MARK.items():
        assert name in icons_mod._BADGE_GLYPHS, f"{level} 的兜底标记 {name} 没有角标字形"


def test_badge_name_resolution_rules():
    """兜底规则（D19）：专用角标字形优先 → 级别标记 → ``MARK_IDLE``。"""
    from barkit.shell.design import icons as icons_mod

    b = icons_mod.badge_name
    assert b(IconName.MARK_WARN, StateLevel.OK) is IconName.MARK_WARN  # 自己就是角标字形
    assert b(IconName.SHIELD, StateLevel.ERR) is IconName.MARK_ERR     # 无角标版 → 级别兜底
    assert b(None, StateLevel.WARN) is IconName.MARK_WARN
    assert b(None, None) is IconName.MARK_IDLE
    assert b(IconName.SHIELD, None) is IconName.MARK_IDLE               # 都给不了 → 不谎称正常
    assert b(IconName.SHIELD, StateLevel.WARN) is b(IconName.SHIELD, StateLevel.WARN)


def test_badge_marks_are_distinguishable_at_badge_size(qapp):
    """5 枚标记在角标尺寸下必须**两两可区分**——包括灰阶（OFF / IDLE 前景同为 text3）。

    画得一样 = 状态角标失去意义，而它是「出事一定看得见」的落点。按像素签名比，
    不靠人眼：这条用例在改坏字形时会直接红，无需真机目测。
    """
    from barkit.shell.design.icons import badge_pixmap

    signatures: dict[StateLevel, frozenset] = {}
    for level in StateLevel:
        img = badge_pixmap(None, level, 28, "#8E8E93").toImage()   # 灰阶前景
        sig = frozenset(
            (x, y)
            for x in range(img.width())
            for y in range(img.height())
            if img.pixelColor(x, y).alpha() > 40
        )
        assert sig, f"{level} 的标记渲染为空白"
        signatures[level] = sig
    for a, b in itertools.combinations(StateLevel, 2):
        assert signatures[a] != signatures[b], f"{a.name} 与 {b.name} 的标记画得一样"


def test_badge_family_stroke_is_readable_at_badge_size(qapp):
    """两族分家的**理由**必须可测：14px 下角标族笔画更粗（墨量明显更高）。

    同一枚标记在两族里的几何尺寸刻意一致（同为 24 : 12 的等比缩放 ⇒ 14px 下半径相同），
    差别只在「1.6 单位」相对谁 —— 所以墨量正是可读性的代理指标。
    """
    from barkit.shell.design import icons as icons_mod

    def ink(pm) -> int:
        img = pm.toImage()
        return sum(
            1
            for x in range(img.width())
            for y in range(img.height())
            if img.pixelColor(x, y).alpha() > 40
        )

    for level in StateLevel:
        name = icons_mod._LEVEL_MARK[level]
        badge_ink = ink(icons_mod.badge_pixmap(None, level, 14, "#FFFFFF"))
        main_ink = ink(icons_mod.pixmap(name, 14, "#FFFFFF"))
        assert badge_ink >= main_ink * 1.3, (
            f"{name} 在 14px 下角标族并不比主字形粗（{badge_ink} vs {main_ink}）"
            "——两族分家的前提不成立"
        )


def test_badge_pixmap_honours_requested_size_and_color(qapp):
    """角标位图：尺寸按调用方给的来，颜色跟随调用方（不是画成黑/固定色）。"""
    from barkit.shell.design.icons import badge_pixmap

    pm = badge_pixmap(IconName.MARK_ERR, StateLevel.OK, 14, "#ff3b30")
    assert pm.size().width() == 14 and pm.size().height() == 14
    img = pm.toImage()
    colored = [
        img.pixelColor(x, y)
        for x in range(14)
        for y in range(14)
        if img.pixelColor(x, y).alpha() > 200
    ]
    assert colored, "角标位图为空白"
    assert any(c.red() > 180 and c.green() < 90 for c in colored), "角标未跟随调用方颜色"


# ──────────────────────────────────────────────
# 墨迹几何门禁（2026-09-21 补）
# ──────────────────────────────────────────────
#
# 「字形画在格子的哪个位置」以前没有门禁：两族字形的坐标都是手摆的，摆歪了不报错，
# 只表现为「图标吊在格子里偏下 / 底边被切掉一块」。用户上报的「铃铛被压扁 + 显示
# 靠下」就是这么溜过全部门禁的（角标铃墨迹偏低 1.75 格、下沿超出 12 视窗被裁）。
#
# 判**墨迹 bbox**而不是路径坐标：描边有 0.8 格半径，路径全在界内 ≠ 墨迹在界内。

_INK_RENDER_PX = 96   # 量墨迹用的渲染尺寸（12 网格 ⇒ 每格 8px，分辨率 0.125 格）
_INK_ALPHA = 24       # 低于此 alpha 视为反锯齿外沿，不计入墨迹

# 2026-09-21 之前发货的那枚角标铃（留着自证门禁抓得住它，不是恒真）
_OLD_BADGE_BELL = (
    '<path d="M3 8.4V7.3a3 3 0 0 1 6 0V8.4l.9 1.4H2.1z"/>'
    '<circle cx="6" cy="10.4" r="0.85"/>'
)


def _ink_box(pm):
    """位图 → 墨迹包围盒（像素）。"""
    img = pm.toImage()
    xs, ys = [], []
    for y in range(img.height()):
        for x in range(img.width()):
            if img.pixelColor(x, y).alpha() > _INK_ALPHA:
                xs.append(x)
                ys.append(y)
    return min(xs), min(ys), max(xs) + 1, max(ys) + 1


def _ink_metrics(pm, grid):
    """墨迹相对网格的**居中偏差**与**四边余量**，单位＝格。"""
    x0, y0, x1, y1 = _ink_box(pm)
    u = pm.width() / grid
    return {
        "dx": (x0 + x1) / 2 / u - grid / 2,
        "dy": (y0 + y1) / 2 / u - grid / 2,
        "top": y0 / u,
        "bottom": grid - y1 / u,
        "left": x0 / u,
        "right": grid - x1 / u,
    }


def _ink_violations(pm, grid, tol, margin):
    """墨迹几何的违规原因（空 = 合规）。

    纯函数（不读模块状态）⇒ 同一份判据既能扫真实字形，也能被「注入旧字形」反过来
    证明它真的会红（见 :func:`test_ink_geometry_gate_catches_the_shipped_bell`）。
    """
    m = _ink_metrics(pm, grid)
    problems = [
        f"墨迹中心 {axis}={m[axis]:+.2f} 格，超出 ±{tol}"
        for axis in ("dx", "dy")
        if abs(m[axis]) > tol
    ]
    problems += [
        f"墨迹压到 {side} 边（余量 {m[side]:.2f} < {margin} 格 ⇒ 笔画被裁）"
        for side in ("top", "bottom", "left", "right")
        if m[side] < margin
    ]
    return problems


def test_badge_glyph_ink_is_centered_and_clears_the_grid(qapp):
    """角标字形：墨迹**居中**（±0.6 格）且**四边不压线**（余量 ≥0.15 格）。

    角标位只有 8–10px（菜单栏 8 / 面板 10），字形画在级别色圆盘正中 —— 墨迹偏
    1.75 格在那里就是下沉 1px 多，肉眼直接读成「图标没居中」；压到视窗边则会被
    **裁掉**半条笔画（铃舌变平顶 ⇒ 「被压扁」），而这既不报错也不发警告。
    """
    from barkit.shell.design import icons as icons_mod

    for name, frag in icons_mod._BADGE_GLYPHS.items():
        pm = icons_mod._render(
            icons_mod._markup(frag, "#FFFFFF", f"0 0 {_BADGE_GRID} {_BADGE_GRID}"),
            _INK_RENDER_PX,
        )
        assert not _ink_violations(pm, _BADGE_GRID, 0.6, 0.15), (
            f"{name}: {_ink_violations(pm, _BADGE_GRID, 0.6, 0.15)}"
        )


def test_main_glyph_ink_is_centered(qapp):
    """主字形：墨迹居中（±1 格）。

    容差比角标宽，是因为主字形里有**天然不对称**的构图（route 的线加箭头，
    dx −0.62 / dy −0.88）；族里其余字形的偏差都在 ±0.8 以内，被修掉的那枚铃
    曾偏离到 +1.75（全族最大）⇒ 1 格足够把「手摆歪了」和「构图本就不对称」分开。
    """
    from barkit.shell.design import icons as icons_mod

    for name in IconName:
        pm = icons_mod.pixmap(name, _INK_RENDER_PX, "#FFFFFF")
        assert not _ink_violations(pm, 24, 1.0, 0.15), (
            f"{name}: {_ink_violations(pm, 24, 1.0, 0.15)}"
        )


def test_ink_geometry_gate_catches_the_shipped_bell(qapp):
    """自证：门禁**抓得住**已发货的那枚歪铃（不许退化成恒真），且不误报合规字形。

    三类各一条：① 合规字形零问题；② 旧角标铃两条违规（偏低 ＋ 下沿被裁）都被点名；
    ③ 现在这枚铃零问题。少了 ② 这条，本门禁就是「扫现状全绿」的空转。
    """
    from barkit.shell.design import icons as icons_mod

    # ① 合规不误报（角标族带 ±0.6 的严档、主字形带 ±1 的宽档各一枚）
    ok = [
        (
            icons_mod.badge_pixmap(IconName.MARK_WARN, None, _INK_RENDER_PX, "#FFFFFF"),
            _BADGE_GRID, 0.6,
        ),
        (icons_mod.pixmap(IconName.SHIELD, _INK_RENDER_PX, "#FFFFFF"), 24, 1.0),
    ]
    for pm, grid, tol in ok:
        assert _ink_violations(pm, grid, tol, 0.15) == [], _ink_violations(pm, grid, tol, 0.15)

    # ② 违规必抓：2026-09-21 前发货的角标铃
    bad = icons_mod._render(
        icons_mod._markup(_OLD_BADGE_BELL, "#FFFFFF", "0 0 12 12"), _INK_RENDER_PX
    )
    problems = _ink_violations(bad, 12, 0.6, 0.15)
    assert any("dy" in p for p in problems), f"未抓到「墨迹偏低」：{problems}"
    assert any("bottom" in p for p in problems), f"未抓到「下沿被裁」：{problems}"

    # ③ 修好之后必须零问题（判据没被放宽到「什么都过」）
    fixed = icons_mod.badge_pixmap(IconName.BELL, None, _INK_RENDER_PX, "#FFFFFF")
    assert _ink_violations(fixed, 12, 0.6, 0.15) == []

