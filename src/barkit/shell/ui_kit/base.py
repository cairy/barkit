"""基础层：常量、文字排版角色、图标与托盘渲染（不依赖包内其它模块）。"""

from __future__ import annotations

import logging
import re
import sys

from PySide6.QtCore import (
    QPropertyAnimation,
    QRectF,
    QEasingCurve,
    Qt,
)
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import (
    QLabel,
    QSizePolicy,
    QWidget,
)
import shiboken6

from barkit.contribution.specs import IconName
from barkit.shell import styles
from barkit.shell.design import metrics

#: 全族共用一个日志名（`logging_setup` 的 `%(name)s` 会写进日志文件）。
#: 写死父包名而不是 `__name__`：子模块各自的 `__name__` 会把日志外观改掉。
log = logging.getLogger("barkit.shell.ui_kit")

#: 「面板内的**裸容器**」共用的 ``objectName`` —— 走 QSS 那条透明条（见 ``styles.py``）。
#:
#: 为什么需要它（2026-09-27 用户上报「『键位』右侧的控件位背景是白色」「action 按钮区域
#: 整个也有白色底色」）：全局有一条 ``QWidget { background: surface }``，而 Qt 对它的判定
#: **按元类分叉** —— ``QLabel`` / ``QLineEdit`` 这类内置类与**裸 ``QWidget`` 实例**都会画，
#: 而自定义子类（``FormGroup`` / ``_EditorNav`` / ``FeedbackBar`` 这些）**不画**。
#: 于是「不画」的那几个安然无事，**裸实例**（一行控件的包装盒、底栏）却自带一块
#: ``surface``，压在 ``surface2`` 的编辑屏面板上就是一块更亮的方块。
#:
#: 与 ``status-main`` 那条的区别：那条的选择器带**后代**（``#status-main QWidget``），
#: 整棵子树都透明 —— 只对「行里只有标签 / 自绘开关」的容器安全。这里的容器里装的是
#: 输入框与下拉，整棵子树透明会把它们的底一起抹掉 ⇒ **只声明容器本身**，故用一个
#: 独立名字、不能复用 ``status-main``。
PLAIN_ZONE = "plain_zone"

def qt_alive(obj: object) -> bool:
    """这个 Qt 对象的 **C++ 本体还在不在**（``None`` ＝ 没有）。

    判活的**唯一真源**（``overlay.overlay_alive`` 也走这一条）：退场走 ``deleteLater``
    时，事件循环跑完删除后 C++ 对象没了、**Python 壳还活着** —— 对死壳调任何方法
    （``isVisible`` / ``update``…）都抛 ``RuntimeError``，读属性也可能读到垃圾。
    ``shiboken6.isValid`` 是 PySide6 没有 ``QPointer`` 时的标准替代，别在各消费方散写。
    """
    return obj is not None and shiboken6.isValid(obj)

# BarKit 条形码字形在 24×24 视图里的 5 根条（设计源见 docs/design/barkit-glyph.svg）
_BARS = [
    (3.5, 7.0, 2.6, 10),
    (7.7, 4.0, 2.6, 16),
    (11.9, 8.5, 2.6, 7),
    (16.1, 5.0, 2.6, 14),
    (20.3, 7.5, 2.6, 9),
]

# ---------------------------------------------------------------------------
# 排版角色：宿主与插件共用的**唯一**文字样式入口
# ---------------------------------------------------------------------------

#: 排版角色 → QSS 片段（``{token}`` 取当前**生效**主题的令牌值，见 ``apply_text_role``）。
#:
#: **为什么要收这张表**：同一个「卡内小节标题」曾在这 6 个设置页里各拼一遍
#: （`font-size:13px; font-weight:600;` 5 处）、「小字说明」被拼了 9 处 —— 改一档字号要
#: 满仓 grep，而**漏改的那一处没人看得出来**。收成角色表之后，插件侧一律走
#: ``apply_text_role``；门禁 ``test_no_inline_stylesheet`` 守「插件侧零自拼 QSS」。
#:
#: **与 ``styles.build_stylesheet`` 的分工**：那张表按 ``objectName`` 定义**控件族**的完整
#: 外观（按钮 / 输入框 / 导航项 / 状态药丸…），随主题切换**整体重放**；这里只管**纯文字**的
#: 那几档，由调用方在构建时刻消费一次。二者不重叠 —— 带边框 / 背景 / hover 的角色属于前者，
#: 别往这张表里加。
#:
#: **取色用 ``current_theme()``**（认 ``apply_theme`` 的覆盖），不是自绘控件那条路上的
#: ``_theme_tokens()``（只读系统外观）—— 与 ``FeedbackBar._tokens()`` 同一条理由，也与这些
#: 角色原先散在各插件里 ``_tokens()`` 的口径逐字一致，故换过来**颜色一个值都没变**。
#:
#: **哪些进这张表、哪些不进**（判据，别靠感觉）：
#:
#: - **进**：调用方**构页时拼一次**、颜色只随主题令牌变的**纯文字**档 —— 从插件的设置页
#:   到宿主的 `panel.py` / `settings.py` / `detail.py` / `autostart_row.py` / `note()`，
#:   一律走这张表。
#: - **不进**：① **自绘控件的内部实现**（``HStatChip`` 按 level 换色的药丸、``_ValueBadge``
#:   的品牌渐变、状态行图标盒的烤色）—— 那是一个控件的**完整外观**，颜色由控件自身状态
#:   驱动，不是「同一档文字被拼了多遍」；② **容器档**（``background:transparent`` /
#:   ``border-radius`` / ``QComboBox`` ``QMenu`` 的 QSS）—— 那是**布局约束**，不是排版。
#:
#: 宿主 `shell/` 也走这张表，但**门禁不扫它**：`test_no_inline_stylesheet` 只看
#: `plugins/**`（`shell/` 是样式表的**实现方**，扫它就得给 `ui_kit` / `styles` 开豁免表
#: ＝违反零白名单）。宿主侧靠评审守，判据就是上面这三条。
_TEXT_ROLES: dict[str, str] = {
    # 小字说明：字段名 / 行副标题 / 拨测输出
    "caption": "color:{text2}; font-size:{FONT_CAPTION}px;",
    # 同上，但落在「正常 / 命中」色上（如录屏通知规则行的「● 命中」）
    "caption_ok": "color:{green_text}; font-size:{FONT_CAPTION}px;",
    # 同上，但内容是**问题描述**（插件装配失败 / 被拒绝加载的原因）。走 `red_text` 而不是
    # `red`：后者是填充色的红，压在浅底上写小字只有 3.55:1，读不动（见 tokens.py 该行注释）。
    "caption_warn": "color:{red_text}; font-size:{FONT_CAPTION}px;",
    # 设置页顶部的只读状态行：比 caption 大一档
    "status": "color:{text2}; font-size:{FONT_SMALL}px;",
    # 成段的说明文字（`note()` 与各页手写的同类）：行高放宽到 1.6
    "note": "color:{text2}; font-size:{FONT_SMALL}px; line-height:1.6;",
    # 卡内小节标题
    "section": "font-size:{FONT_BODY}px; font-weight:600;",
    # 卡片 / 页面标题（设置页外壳、详情窗卡头）
    "detail_title": "font-size:{FONT_TITLE}px; font-weight:700;",
    # 品牌名一档（「关于」页的 APP_NAME），比 detail_title 再大一档
    "page_title": "font-size:{FONT_PAGE}px; font-weight:700;",
    # 比正文重、比标题轻的一行强调文字（熔断卡片那条故障文案）
    "heading": "color:{text1}; font-size:{FONT_SMALL}px; font-weight:600;",
    # 一行内「条目的名字」（状态机名 / 一台机的标题）：比卡标题小、比它下面的
    # 取值行**大一号且最重** —— 条目是这一段的主语，取值是挂在它下面的（2026-09-21
    # 用户上报「状态机名与枚举差别不够明显」：字号只差 1.5px、字重相同 ⇒ 一屏平的）。
    "row_title": "color:{text1}; font-size:{FONT_TITLE_SM}px; font-weight:700;",
    # 行尾「›」指示符（纯装饰，跟着走 text3）
    "chevron": "color:{text3}; font-size:{FONT_TITLE_SM}px;",
    # 分区卡标题栏那枚**折叠箭头**：它是**可点的控件**不是装饰 ⇒ ① 字号比 chevron
    # 大一档（14 在带子里只有「一个墨点」，用户上报看不清）；② 走 text2 —— text3
    # 压在 surface3 带子上只有 2.8:1，一枚要人去点的箭头不该这么淡。
    "fold_mark": "color:{text2}; font-size:{FONT_FOLD}px;",
    # 浮层（无边框半透明窗）里的四档：**必须显式透明底** —— 全局 QSS 给 ``QWidget``
    # 铺了 ``surface`` 底色，不写这一句标签会带一块不透明方块浮在卡片上。
    # （2026-09-23 起全局已有一条 ``QLabel { background: transparent }`` 兜底，这里
    # 照旧显式写着 —— 浮层是最经不起白块的地方，第二道保险比省两个词值。）
    "overlay_title": "color:{accent_text}; font-weight:700; font-size:{FONT_TITLE_SM}px; background:transparent;",
    "overlay_count": "color:{text1}; font-size:{FONT_COUNT}px; background:transparent;",
    "overlay_hint": "color:{text3}; font-size:{FONT_HINT}px; background:transparent;",
    # 浮层**正文**那一行：轻提示的一句话（热键「轻浮层」反馈用的就是它）。比
    # overlay_title 小一号、不加粗 —— 内容常是「标题：明细」这种一行，主次由前两档去分。
    "overlay_body": "color:{text1}; font-size:{FONT_BODY}px; background:transparent;",
    # 图片区占位提示（原先在 recognize/selview 里写死 ``#9aa0a6``，那个值不在任何一套令牌里）
    "placeholder": "color:{text2}; font-size:{FONT_TITLE_SM}px; background: transparent;",
}

def apply_text_role(widget: QWidget, role: str, *, color: "str | None" = None) -> None:
    """给控件套一个**排版角色**（可选值见 ``_TEXT_ROLES``）；角色名写错当场报错。

    只设 ``styleSheet`` —— 尺寸 / 光标 / ``objectName`` / 换行都由调用方自己管（那些是控件
    属性，不是排版）。样式在**调用时刻**烤进控件，与原先各插件自己 ``setStyleSheet`` 的时序
    完全一致 ⇒ 换过来零行为变更；跟着主题自动重放的那一类（``styles.build_stylesheet``）不在
    这条路上，别把两者混起来读。

    角色名拼错时**抛错而不是静默跳过**：静默跳过会得到一个「颜色对不上但没人报错」的界面
    —— 与热键注册表拒绝冲突是同一条口径（用户看得见的错，别让它在日志里消失）。

    ``color`` 传的是**令牌名**（如 ``red_text``）：把该角色的默认色换成这个令牌，字号等
    其余部分不动。给「按级别染色的文字」走这一个口，而不是让调用方自己拼一份 QSS ——
    自己拼就得把字号也抄一遍，字号一改又是「漏改的那一处没人看得出来」。

    ⚠️ **别改用调色板（``QPalette``）换色**：QSS 里的 ``color`` 优先级**高于**调色板，
    控件上一旦有样式表，``setPalette`` 写进去的颜色一个像素都不生效（2026-09-21 实测：
    设了红的 ``WindowText``，画出来仍是样式表里的灰）—— 状态机段原先就是这么哑掉的。
    """

    try:
        template = _TEXT_ROLES[role]
    except KeyError:
        raise KeyError(
            f"未知排版角色 {role!r}；可选：{', '.join(sorted(_TEXT_ROLES))}"
        ) from None
    tokens = styles.TOKENS[styles.current_theme()]
    # 字号占位符（``{FONT_BODY}`` 等）与颜色令牌同一次格式化：模板里的裸 px 全部改成
    # ``{FONT_*}``，真源在 ``metrics``（门禁 ``test_no_font_size_magic_numbers`` 守）。
    qss = template.format(**tokens, **metrics.FONT_SCALE)
    if color is not None:
        # 令牌名拼错当场 KeyError（与「角色名拼错抛错」同一口径），不静默回落。
        qss = _COLOR_IN_QSS.sub(f"color:{tokens[color]}", qss, count=1)
    widget.setStyleSheet(qss)

#: 角色 QSS 里那一句 ``color:`` —— 换色时按它替换（角色表里颜色都是 ``#{hex}``，
#: 格式化之后才替换，故认的是色值不是令牌名）。
_COLOR_IN_QSS = re.compile(r"color:\s*#[0-9A-Fa-f]{3,8}")

def note(text: str, parent: QWidget | None = None) -> QLabel:
    """统一的小字说明标签（设置页 / 对话框复用，替代各窗口手写 _note）。

    **竖直方向钉住**：说明文字的高度只由内容决定，不该随窗口变高。``QLabel`` 默认
    竖直策略带 ``GrowFlag``，同一列里一旦有别的项让出余高（最典型：一张可折叠的卡
    收起来），布局会把那段余高**全喂给它** —— 它会从一行撑成一大块空白，并把后面的
    内容整体顶到页面底部（2026-09-24 用户报「点『键位』收起卡，整个界面错乱」的
    第二半：卡缩对了，却沉到了页底）。

    取 ``Maximum`` 而不是 ``Fixed``：前者不伸展但仍可被压小，且**保留
    ``heightForWidth``**（``QLabel`` 开了 ``setWordWrap`` 才挂上这条）—— 换行标签的
    高度仍按实际宽度算，不会被钉成一行高而裁掉后半句。改竖直策略走
    ``sizePolicy()`` 拷贝，正是为了不把 ``heightForWidth`` 一起抹掉
    （``QSizePolicy(QPolicy, QPolicy)`` 构造出来的是 ``heightForWidth=False``）。
    """
    lbl = QLabel(text, parent)
    lbl.setObjectName("secondary")
    lbl.setWordWrap(True)
    pol = lbl.sizePolicy()
    pol.setVerticalPolicy(QSizePolicy.Policy.Maximum)
    lbl.setSizePolicy(pol)
    apply_text_role(lbl, "note")  # 角色表在本模块下方定义；函数体到调用时才解析名字
    return lbl

def _theme_tokens() -> dict:
    return styles.TOKENS[styles.current_system_theme()]

def _is_native_cocoa() -> bool:
    """入场动画只在原生 macOS cocoa 会话生效；其余（离屏 / Windows）一律 no-op。

    离屏下即使平台是 darwin，``QT_QPA_PLATFORM=offscreen`` 也会让 platformName 变成
    "offscreen"，同样判为非原生 ⇒ 动画不触发（``test_entrance_is_noop_off_cocoa`` 守这条）。

    历史上玻璃也读这个判据，2026-09-21 玻璃模式已整体删除（分区卡改实心 + 1px 描边），
    故本函数今天只剩入场动画一个消费方。
    """
    if sys.platform != "darwin":
        return False
    app = QGuiApplication.instance()
    return app is not None and app.platformName() == "cocoa"

def play_entrance(win: QWidget) -> None:
    """顶层窗入场淡入。仅原生 macOS cocoa 生效，其余环境 no-op。

    设置窗弹出时调一次即可（非 cocoa 下直接返回，离屏测试不触发）。

    **走窗口不透明度，不走 ``QGraphicsEffect``**（2026-09-21 重写；原写法是「设置窗打开
    一片空」的元凶，判据见《设置窗入场动画空窗修复》）：

    - ``QWidget.windowOpacity`` 落到 macOS 的 ``NSWindow.alphaValue``，由窗口服务器合成
      —— 实测 180ms 入场里 Qt **一次都不重画**。``QGraphicsOpacityEffect`` 则每帧都要把
      整窗子树重画进离屏位图再合成（真机实测 11 帧 / 每帧 +0.56 ms），而且不透明度存在
      Qt 侧：一旦没人把它拉回来，整窗（连同全部内容）就被画成透明。
    - 同批删掉几何缩放（0.94 → 1.0）：它同样是「先落一个不可见态、再靠动画拉回来」的写法，
      而每次改顶层窗 ``geometry`` 都要整窗重排 ＋ 让窗口服务器重分配 backing store ——
      比淡入贵得多。（2026-09-21 起设置窗尺寸**可由用户拖动**，所以「本就缩不动」不再是
      理由，纯粹是「不值」：入场那一瞬的缩放换不来任何信息。）

    只服务顶层窗（当前唯一调用方：设置窗 ``show_and_raise``）。子控件淡入没有调用方，
    不留那条岔路。
    """
    if not _is_native_cocoa() or win is None or not win.isWindow():
        return

    # 先落 0：动画的首帧要等下一次事件循环，不先落会闪一帧全不透明。
    win.setWindowOpacity(0.0)
    anim = QPropertyAnimation(win, b"windowOpacity", win)  # 第三参 = 父对象，防被回收
    anim.setDuration(180)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

def paint_bar_glyph(
    painter: QPainter, rect: QRectF, color: QColor, bg: QColor | None = None
) -> None:
    """在 rect 内绘制条形码字形；bg=None 时透明背景。bars/圆角随 rect 等比缩放。"""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    if bg is not None:
        painter.setBrush(bg)
        painter.setPen(Qt.PenStyle.NoPen)
        r = rect.height() * 0.27
        painter.drawRoundedRect(rect, r, r)
    painter.setBrush(color)
    painter.setPen(Qt.PenStyle.NoPen)
    scale = rect.width() / 24.0
    for x, y, w, h in _BARS:
        painter.drawRoundedRect(
            rect.x() + x * scale,
            rect.y() + y * scale,
            w * scale,
            h * scale,
            1.3 * scale,
            1.3 * scale,
        )
    painter.restore()

def make_tray_pixmap(
    size: int = 22, color: QColor | None = None, dpr: float = 1.0
) -> QPixmap:
    """生成菜单栏托盘图标（透明底 + 单色条形）。

    ``size`` 为**逻辑尺寸**；``dpr>1`` 时按物理像素渲染并打 ``devicePixelRatio`` 标记，
    菜单栏（Retina）不再把它放大发虚（与面板图标同一口径）。本函数**只画**——NSImage
    那一侧的点数归一见 ``status_item._pixmap_to_nsimage``。
    """
    if color is None:
        color = QColor(40, 40, 45)
    px = max(1, int(round(size * dpr)))
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    # 物理坐标 = 逻辑 × dpr（保留 1 逻辑像素留边）；devicePixelRatio 在画完之后再打，
    # 否则 painter 的 viewport 会被当成逻辑尺寸、只画出左上角（见 2026-09-21 踩坑）。
    paint_bar_glyph(painter, QRectF(dpr, dpr, px - 2 * dpr, px - 2 * dpr), color)
    painter.end()
    pm.setDevicePixelRatio(dpr)
    return pm

def tray_render_dpr() -> float:
    """菜单栏图标渲染用的设备像素比（无屏退 1.0，测试 / 离线安全）。"""
    screen = QGuiApplication.primaryScreen()
    return screen.devicePixelRatio() if screen is not None else 1.0

def tray_base_pixmap(
    icon: "IconName | None", size: int, color: QColor, dpr: float = 1.0
) -> QPixmap:
    """托盘**底图**渲染的唯一解析点（P0③ 底图换源）。

    - ``icon is None`` → BarKit 条形码底图（历史行为，出厂默认）；
    - ``icon`` 非空 → 该语义图标（取值图标）按同一网格渲染——菜单栏那一格于是
      可以显示「某个状态当前取值」的图标（如屏幕字形的实心/空心）。

    **本函数不做任何回落判断**：调什么图标由装配器的回落规则决定（快照缺失 /
    没给取值图标 / 状态失联 → 传 None 进来），这里只负责画。
    颜色由调用方给：无角标路径走 template 图（系统单色渲染，颜色只影响透明度），
    有角标路径须按菜单栏实际明暗取色。
    """
    if icon is None:
        return make_tray_pixmap(size, color, dpr)
    from barkit.shell.design import icons

    return icons.pixmap(icon, size, color, dpr)

# ---------------------------------------------------------------------------
# 图标统一出口（单点）：渲染与布局各收敛到一个点
#
# 发虚的根因是「1x 位图被 Qt 在 Retina 上放大」。让位图带上 devicePixelRatio 即可锐利，
# 但 **`QPixmap.width()/height()` 在 dpr>1 时返回物理像素**，于是所有「用 pm.width()
# 手算居中/布局」的代码会集体错位。因此正确做法是**渲染与布局一起收敛**：
#
#   - 自绘 paintEvent → ``draw_glyph`` / ``draw_badge_glyph``（dpr 取自 painter，
#     按**逻辑矩形**定位，调用方永不碰 pm.width()）；
#   - 控件资源（setIcon / setPixmap）→ ``glyph_pixmap`` / ``badge_glyph_pixmap``
#     （dpr 取自控件；QIcon/QLabel 对 DPR-aware 位图原生正确）。
#
# 除本模块与 ``design/icons`` 内部外，全仓**禁止**直接调 ``icons.pixmap()`` /
# ``icons.badge_pixmap()``（AST 门禁 test_ui_no_direct_icon_render 守）。
# ---------------------------------------------------------------------------

def glyph_pixmap(target: QWidget, icon: "IconName", size: int, color) -> QPixmap:
    """为**控件资源**接口（``setIcon`` / ``setPixmap``）渲染一枚主字形。

    ``size`` 是**逻辑尺寸**；返回位图逻辑尺寸 == ``size``、物理 = ``size×target.dpr``。
    dpr 取自 ``target``（真正显示这枚图标的控件），多屏下各自正确。
    QIcon/QLabel 会按 ``devicePixelRatio`` 显示，调用方**不要**再拿 ``pm.width()``
    当逻辑尺寸做布局（dpr>1 时它是物理像素）。
    """
    from barkit.shell.design import icons

    return icons.pixmap(icon, size, color, target.devicePixelRatioF())

def badge_glyph_pixmap(
    target: QWidget, icon: "IconName | None", level, size: int, color
) -> QPixmap:
    """``glyph_pixmap`` 的角标字形版（12 网格简化字形，回落规则见 ``icons.badge_name``）。"""
    from barkit.shell.design import icons

    return icons.badge_pixmap(icon, level, size, color, target.devicePixelRatioF())

def draw_glyph(painter: QPainter, icon: "IconName", rect: QRectF, color) -> None:
    """把一枚主字形画进**逻辑矩形** ``rect``（自绘 ``paintEvent`` 的唯一出口）。

    dpr 取自 ``painter`` 所在设备（多屏各自正确）；字形按 ``rect``（逻辑）定位，
    调用方**无需**再做居中算术、更**不得**用 ``pm.width()``（dpr>1 时它是物理像素，
    会让图标错位/被裁 —— 2026-09-21 实测过）。
    """
    from barkit.shell.design import icons

    dpr = painter.device().devicePixelRatioF()
    size = max(1, int(round(rect.width())))
    painter.drawPixmap(rect.topLeft(), icons.pixmap(icon, size, color, dpr))

def draw_badge_glyph(
    painter: QPainter, icon: "IconName | None", level, rect: QRectF, color
) -> None:
    """``draw_glyph`` 的角标字形版（自绘 ``paintEvent`` 用）。"""
    from barkit.shell.design import icons

    dpr = painter.device().devicePixelRatioF()
    size = max(1, int(round(rect.width())))
    painter.drawPixmap(rect.topLeft(), icons.badge_pixmap(icon, level, size, color, dpr))

