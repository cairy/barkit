"""ui/styles：BarKit 设计令牌 → PySide6 QSS（浅色 / 深色一键换肤）。

设计系统唯一来源在 ``shell/design/tokens.py``（批次 0 起单一真源）：
所有界面、控件、状态色都从 TOKENS 取值，禁止在业务代码里再硬编码颜色。

用法：
    from barkit.shell import styles
    app.setStyleSheet(styles.build_stylesheet("dark"))   # "light" | "dark"
    # 监听 macOS AppleInterfaceStyle 变化后重新 build + setStyleSheet 即可换肤

QSS 不支持 CSS 变量，故在 Python 侧把令牌渲染成字符串。
（仅进度环等少数自绘控件不在 QSS 内，由对应 QWidget 读 TOKENS 绘制；开关 / 按钮等已迁回原生控件 + QSS。）
"""

from __future__ import annotations

import logging
import platform

from PySide6.QtCore import QObject, Signal

# ---- 设计令牌：唯一真源在 shell/design/tokens.py（批次 0 定案）----
# 本模块只保留 TOKENS / token 两个名字做兼容 re-export（消费者：`styles.TOKENS`
# 全仓数十处、`styles.token` 见 `shell/ui_kit/fields.py`）；禁止再向本模块添加令牌
# ——新令牌一律加在 shell/design/tokens.py。
# （`THEMES` 曾是再导出之一，2026-10-05 核：只有真源自己与 `tests/test_shell_design.py`
# 从 `design.tokens` 取它，`styles.THEMES` 零消费方 ⇒ 删。）
from barkit.shell.design import metrics
from barkit.shell.design.tokens import TOKENS, token

# 本仓无 ruff，pyflakes 不认 `# noqa`。「刻意再导出」这件事只能靠 `__all__` 告诉
# 静态检查，否则上面那一行会被按「imported but unused」报出来（2026-10-05 起 `src/`
# 的 pyflakes 基线为零，见 `log/2026-10-05-生产代码pyflakes告警清零.md`）。
__all__ = [
    "TOKENS",
    "token",
    "build_stylesheet",
    "current_system_theme",
    "ThemeManager",
    "current_theme",
    "apply_theme",
    "watch_system_theme",
]


def build_stylesheet(theme: str = "light") -> str:
    """根据主题渲染完整 QSS 字符串。

    Args:
        theme: "light" 或 "dark"（映射 macOS 外观）。
    Returns:
        可直接传给 QApplication.setStyleSheet 的样式表。
    """
    t = TOKENS.get(theme, TOKENS["light"])
    # 按钮档位属性的名字取自 ``metrics``（与 ``tool_button`` 写进控件的那一个同源）——
    # QSS 选择器与控件属性对不上时会**静默**回落基础档，量不出任何报错。
    tier = metrics.TIER_PROPERTY

    return f"""
    /* ---------- 基础 ---------- */
    QWidget {{
        background: {t['surface']};
        color: {t['text1']};
        font-size: {metrics.FONT_BODY}px;
        selection-background-color: {t['accent']};
        selection-color: {t['on_solid']};
    }}
    /* 标签一律**透明底**（2026-09-23 收成通用条，不再一处一处打补丁）。
       上面那条 ``QWidget`` 是**类型选择器**，Qt 让它命中子类 ⇒ ``QLabel`` 也吃它，
       于是每枚标签都自带一块 ``surface`` 实底。压在 surface 上看不出来；压在**自绘底**
       上就是「字底下蒙了一层白」—— 到今已撞三处：分区卡标题带（2026-09-21）、
       状态行 hover 的 ``accent_soft``（2026-09-23 用户上报）、熔断红卡（同批量的，
       ``#breach_text`` 一直没声明底 ⇒ 红卡上顶着白块）。
       带底的药丸（``QLabel#pill_*``）走 id 选择器，优先级**高于**本条 ⇒ 照旧自带底；
       ``_ico`` 那类在自己身上 ``setStyleSheet`` 的更稳（控件自带的样式表压过全局）。
       **本条不许上移**：它与上面那条同优先级（都是一个类型选择器），Qt 按「后声明者胜」
       裁决 —— 排到 ``QWidget`` 之前等于没写（判据见 ``tests/test_styles.py`` 的次序断言
       与 hover 像素断言）。 */
    QLabel {{ background: transparent; }}
    QLabel#secondary {{ color: {t['text2']}; font-size: {metrics.FONT_SMALL}px; }}

    /* ---------- 按钮 ----------
       **大小只有一处**：档位（`metrics.BUTTON_HEIGHTS`：action 28 / inline 22 / chip 20）
       由控件写进 `tier` 动态属性，这里按档给字号 / 内边距 / 圆角 —— 于是一颗按钮的
       「框多高」与「字放不放得下」是同一处算出来的。
       此前这两件事分头演化：QSS 只按**外观族**写死一套 `padding: 6px 14px;
       font-size: 13px`（自然高度 33px），而三个档把框钉在 28 / 22 / 20 ⇒ 所有
       pill-ghost 按钮都被裁 5 / 11 / 13px（「恢复默认配置」= 最小的档 × 最肥的族，最惨）。
       基础块就是 **action 档**，兼作没设 `tier` 的裸 QPushButton 的兜底。
       注意：`tier` 必须与族**合写**：`#id` 选择器优先级高于 `[attr]`，族块里只要还声明着
       padding / font-size / border-radius，`[tier=…]` 就被整体压掉（2026-09-22 实测：
       单写 `[tier="chip"]` 时三档全返回 33）。故**族块一律不写这三样**。 */
    QPushButton {{
        background: {t['surface2']};
        color: {t['text1']};
        border: 1px solid {t['border']};
        border-radius: {metrics.RADIUS_SM}px;
        padding: {metrics.BTN_ACTION_PAD[0]}px {metrics.BTN_ACTION_PAD[1]}px;
        font-size: {metrics.FONT_BODY}px;
    }}
    QPushButton[{tier}="inline"] {{ padding: {metrics.BTN_INLINE_PAD[0]}px {metrics.BTN_INLINE_PAD[1]}px; font-size: {metrics.FONT_SMALL}px; border-radius: {metrics.BTN_INLINE_RADIUS}px; }}
    QPushButton[{tier}="chip"] {{ padding: {metrics.BTN_CHIP_PAD[0]}px {metrics.BTN_CHIP_PAD[1]}px; font-size: {metrics.FONT_CHIP}px; border-radius: {metrics.RADIUS_XS}px; }}
    QPushButton:hover {{ border-color: {t['border_strong']}; }}
    QPushButton:pressed {{ background: {t['border']}; }}
    QPushButton:disabled {{ color: {t['text3']}; background: {t['surface2']}; }}
    QPushButton#primary {{
        background: {t['accent']};
        color: {t['on_solid']};
        /* 与背景同色的 1px 边框：在半透明无边框窗口下，透明边框会导致
           QPushButton 背景不重绘（白字浮在透明上=“按钮消失”）；同色边框
           外观不变，但保证背景一定绘制。 */
        border: 1px solid {t['accent']};
    }}
    QPushButton#primary:hover {{ background: {t['accent_hover']}; }}
    /* 危险按钮的面取 `red_solid`（实体档），不是 `red` —— 后者是图形档（级别圆盘 /
       圆点 / 开关轨道），拿它压白字只有 3.55（浅）/ 3.41（深），低于 4.5。 */
    QPushButton#danger {{ background: {t['red_solid']}; color: {t['on_solid']}; border: none; }}
    /* 悬停档必须是一枚真令牌：原先写 `filter: brightness(0.95)`，而 `filter` **不是
       QSS 属性**（Qt 每次解析都吐 `Unknown property filter`）⇒ 悬停没有反馈。 */
    QPushButton#danger:hover {{ background: {t['red_solid_hover']}; }}
    QPushButton#pill-ghost {{
        /* 只写「观感」这一轴（颜色 / 边框 / 字重）：**字号与内边距归上面的 `[tier=…]`**
           —— 族块一旦声明它们，就会按选择器优先级整体压掉档位（见上面那段注释）。 */
        background: transparent; color: {t['text2']};
        border: 1px solid {t['border']}; font-weight: 600;
    }}
    QPushButton#pill-ghost:hover {{ background: {t['surface2']}; color: {t['text1']}; border-color: {t['accent_text']}; }}
    QPushButton#pill-ghost:pressed {{ background: {t['surface2']}; }}
    QPushButton#pill-ghost:disabled {{ color: {t['text3']}; background: transparent; border-color: {t['border']}; }}
    QPushButton#pill-ghost:disabled:hover {{ background: transparent; border-color: {t['border']}; }}

    /* 显示位芯片（设置窗「插件」页）：与 pill-ghost 同族但更小更轻。
       亮（pill-chip-on）= accent_soft 底 + accent_text 字，与侧栏选中项同一门语言 ——
       「选中 = accent_soft」在本应用里已经是一个固定含义，不再新造一种高亮。 */
    QPushButton#pill-chip, QPushButton#pill-chip-on {{
        /* 尺寸同样归 `[tier="chip"]`（芯片族只有这一档），这里只说颜色与字重。 */
        font-weight: 600;
    }}
    QPushButton#pill-chip {{
        background: transparent; color: {t['text2']}; border: 1px solid {t['border']};
    }}
    QPushButton#pill-chip:hover {{
        background: {t['surface2']}; color: {t['text1']}; border-color: {t['accent_text']};
    }}
    QPushButton#pill-chip-on {{
        background: {t['accent_soft']}; color: {t['accent_text']}; border: 1px solid {t['accent_text']};
    }}
    QPushButton#pill-chip-on:hover {{ border-color: {t['accent_text_hover']}; color: {t['accent_text_hover']}; }}

    /* ---------- 输入框 / 文本域 ----------
       单行格（``QLineEdit`` / ``QSpinBox`` / ``QDoubleSpinBox``）**高度钉在 ``metrics.CONTROL_H``（28）**：
       内容 ``FIELD_CONTENT_H`` + 上下内边距 ``FIELD_PAD_Y``×2 + 上下描边 1×2。
       此前这一段只写 ``padding: 7px 10px`` ⇒ 自然高 34，而同一行的按钮是 28、下拉
       （那时还没有任何 QSS，走 macOS 原生渲染）是 24 —— **同一行三种高度**，
       用户 2026-09-22 上报「下拉与其它元素不协调」。多行格不钉高：它的高度由内容定。
       **这一行列的是原生类**（类型选择器命中该类**及其子类**，2026-09-30 实测）：所以
       ``ui_kit`` 的替身只要原生基类已在这行里就已覆盖；**要补的是还没出现过的原生基类**
       —— ``QDoubleSpinBox`` 是 ``QSpinBox`` 的**兄弟**（同出 ``QAbstractSpinBox``）、
       ``QTimeEdit`` 同出那一支、``QPlainTextEdit`` 是另一族，都不在任何已列类的子树里；
       漏了就退回 macOS 原生渲染（聚焦边框也不变色）。
       门禁 ``test_every_ui_kit_input_subclass_is_styled`` 守这条。

       **避开 ui_kit 自己 new 一个原生输入格同样要补**（2026-09-28 撞到）：询问浮层那枚
       ``QTimeEdit`` 是唯一一处绕开 ``ui_kit`` 的输入格，漏列 ⇒ 它比设置页的输入框矮、
       没有圆角描边、聚焦不变色 —— 用户判为「这的输入框和别处不一样」。扫描线在南边
       那条 ``test_every_native_input_used_in_src_is_styled``，它扫**全仓**，替身 / 直接用
       原生类两种情况都覆盖。 */
    QLineEdit, QSpinBox, QDoubleSpinBox, QTimeEdit {{
        background: {t['surface2']};
        color: {t['text1']};
        border: 1px solid {t['border']};
        border-radius: {metrics.RADIUS_SM}px;
        padding: {metrics.FIELD_PAD_Y}px 10px;
        min-height: {metrics.FIELD_CONTENT_H}px;
        selection-background-color: {t['accent']};
    }}
    QPlainTextEdit, QListWidget, QTextEdit {{
        background: {t['surface2']};
        color: {t['text1']};
        border: 1px solid {t['border']};
        border-radius: {metrics.RADIUS_SM}px;
        padding: {metrics.TEXT_EDIT_PAD[0]}px {metrics.TEXT_EDIT_PAD[1]}px;
        selection-background-color: {t['accent']};
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus,
    QTimeEdit:focus, QTextEdit:focus {{
        border: 1px solid {t['accent_text']};
        /* QSS 无 box-shadow，用 outline 近似聚焦光环 */
        outline: 0;
    }}
    QLineEdit[invalid="true"], QPlainTextEdit[invalid="true"] {{
        border: 1px solid {t['red_text']};
    }}

    /* ---------- 灰掉的输入格 ----------
       2026-10-08 补：此前全仓的 ``:disabled`` 只写在按钮上（``QPushButton`` /
       ``#pill-ghost``），**输入控件一条都没有** ⇒ ``setEnabled(False)`` 的格子长得
       和别的格子**一模一样**（实测：待办详情页里「提醒时间」在问时刻的条目上真的是
       灰的，但界面上看不出任何差别 —— 只有 placeholder 那几个字偶然不同）。
       症状是「这格填了没反应」，而它不报错、不报警，用户只会以为坏了。

       画法是一个**沉下去的实心块**：底色与描边都取 ``surface3``（全仓「一条带子 /
       凹陷区」那一档，两个主题下都比输入格的 ``surface2`` **深一档**），字取 ``text3``
       （三级文字档）。三处一起才认得出来 —— 只填浅一档的底是不够的：实测第一版写
       ``background: surface``，而它在本主题里**比正常态还白**（差 2/255 的差距），
       量出来「灰格子」与「正常格子」在屏幕上没有分别。
       描边也一起取 ``surface3``：留着那圈 ``border`` 的框，读起来仍是「一个可填的
       框」，只是颜色淡了点 —— 而这里要说的是「这一格此刻不参与」。

       值仍留在格子里（切问法再切回来值还在，这是刻意的 —— 见
       ``detail_view._sync_enabled``），故字不能淡到读不出；``text3`` 是这一档的下限。
       伪类必须逐个列：Qt 的 ``:disabled`` 不随类型选择器下发到别的族。 */
    QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QTimeEdit:disabled,
    QPlainTextEdit:disabled, QTextEdit:disabled {{
        color: {t['text3']};
        background: {t['surface3']};
        border: 1px solid {t['surface3']};
    }}

    /* ---------- 勾选框 ----------
       2026-10-16 补：此前 ``QCheckBox`` **一条规则都没有**，于是 10 处裸勾选框的字号
       走 Qt 默认值，与同一屏里的 ``form_label``（``caption`` 档）差一档；禁用态更是
       完全看不出来（与 2026-10-08 补输入格禁用态是同一类缺失）。

       ``::indicator``（那个框本身）**故意不自绘**：macOS 原生指示框在深浅两个主题下
       都跟着系统走，自绘要另处理 hover / pressed / disabled / 深浅四态，成本远大于
       收益。真机目测若发现它与本仓的圆角语言不搭，再单开一批补 —— 别在这里先画上。
       三样之外一律不加（``spacing`` 也不加）：动它就动了布局，而本段的目的只是
       「别用 Qt 默认字号」。 */
    QCheckBox {{ color: {t['text1']}; font-size: {metrics.FONT_BODY}px; }}
    QCheckBox:disabled {{ color: {t['text3']}; }}

    /* ---------- 状态药丸（用 objectName 区分） ---------- */
    /* 药丸里的字是**前景档**：`green` / `orange` / `red` 是图形档（压白图形用的），
       拿它们当字写，浅色下只有 2.01 / 1.87 / 3.05 —— 读不动。 */
    QLabel#pill_ok   {{ color: {t['green_text']}; background: {t['green_soft']}; border-radius: {metrics.RADIUS_PILL}px; padding: {metrics.PILL_PAD[0]}px {metrics.PILL_PAD[1]}px; font-size: {metrics.FONT_CAPTION}px; font-weight: 600; }}
    QLabel#pill_warn {{ color: {t['orange_text']}; background: {t['orange_soft']}; border-radius: {metrics.RADIUS_PILL}px; padding: {metrics.PILL_PAD[0]}px {metrics.PILL_PAD[1]}px; font-size: {metrics.FONT_CAPTION}px; font-weight: 600; }}
    QLabel#pill_err  {{ color: {t['red_text']}; background: {t['red_soft']}; border-radius: {metrics.RADIUS_PILL}px; padding: {metrics.PILL_PAD[0]}px {metrics.PILL_PAD[1]}px; font-size: {metrics.FONT_CAPTION}px; font-weight: 600; }}
    QLabel#pill_idle {{ color: {t['text2']}; background: {t['surface2']}; border-radius: {metrics.RADIUS_PILL}px; padding: {metrics.PILL_PAD[0]}px {metrics.PILL_PAD[1]}px; font-size: {metrics.FONT_CAPTION}px; font-weight: 600; }}

    /* ---------- 熔断故障条 ---------- */
    /* 背景色由 Card 自绘（red_soft + red 描边），此处只定义文字色 */
    QLabel#breach_text {{ color: {t['text1']}; font-size: {metrics.FONT_SMALL}px; }}

    /* ---------- 状态行 hover ---------- */
    /* 状态行 hover 由 HoverRow 自绘（QSS 的 QWidget 背景在 macOS 不生效） */
    /* 状态行本身透明：仅底层 Card 负责统一底色，避免多层背景色差 */
    QWidget#status_row {{ background: transparent; }}
    /* 行**里面那两层嵌套容器**同样要透明：``QWidget`` 那条连**裸 QWidget 实例**也管
       （不是只有 QLabel 吃它 —— 2026-09-23 逐个形态量过像素：行里放一个空容器，
       容器范围里量到的是 ``surface``、不是 hover 的 ``accent_soft``）。面板与
       ``StatusRow`` 的名称/副标题都装在这个容器里，不写这一句，hover 只在缝隙里看得见。
       选择器用 ``#status-main``（不带类型名）：它管住容器本身与其**所有子孙**，
       而药丸那类带 ``QLabel#…`` 的规则优先级仍高于它；控件自己 ``setStyleSheet`` 的
       （图标盒、模式徽标）更是压过全局。 */
    QWidget#status-main, QWidget#status-main QWidget {{ background: transparent; }}

    /* ---------- 设置页分区卡的内层容器 ---------- */
    /* 分区卡的底色**只有 Card 一层**在画（标题栏那条带子是它自己的 paintEvent 画在
       Card 之上的）。全局那条给每个普通容器填实底的规则 —— 标题栏四角会把 Card 的
       圆角描边盖成直角，内容区会盖掉「熔断红底」这类自定义卡色。这两层一律透明。
       （标题带上的**标签**当初也是在这里单独兜的：那条已由上面通用的 ``QLabel``
       透明条接管，见它的注释 —— 本条只留容器。） */
    QWidget#section_head, QWidget#section_body {{ background: transparent; }}

    /* ---------- 编辑屏（`EditorShell`）里那几种「不带面的容器」 ---------- */
    /* ``#editor_nav`` ＝ 导航带（贴在编辑屏那块 ``surface2`` 面的顶边）：它自绘带色，
       控件矩形里**圆角之外**那几像素必须是面底色 —— 实测自定义 QWidget 子类不画
       全局那条 QWidget 铺底规则（本条与 ``#section_head`` 都量过），但这一判定是按
       元类分叉的、不值得赌一个「看着像带子外面套了一圈更亮的框」。
       ``#plain_zone`` ＝ 编辑屏里的**裸 QWidget 实例**（底栏、一行控件的包装盒 ——
       见 ``ui_kit.PLAIN_ZONE``）：这一类**会**画，压在面上就是一块比面更亮的方块
       （2026-09-27 用户上报「『键位』右侧的控件位背景是白色」「action 按钮区域整个
       也有白色底色」）。**只声明容器本身、不带后代**：这些盒子里装着输入框与下拉，
       整棵子树透明会把它们的底一起抹掉。 */
    QWidget#editor_nav, QWidget#plain_zone {{ background: transparent; }}

    /* ``#editor_scroll`` ＝ 编辑屏的**内容区滚动区**（`EditorShell` ②，2026-10-16 加）：
       它自己与它那个 **viewport** 都得透明 —— viewport 是 `QScrollArea` 的内部件、
       没有 objectName，故用**直接子选择器**只罩到它一层（`_content_host` 本身已经是
       ``#plain_zone``，而它里面那些输入框绝不能被带上：整棵子树透明会把它们的底一起
       抹掉，与上面那条同一条理由）。 */
    QWidget#editor_scroll, QWidget#editor_scroll > QWidget {{ background: transparent; }}

    /* ---------- 面板头部 / 状态芯片（refined panel） ---------- */
    /* 卡片(popover)圆角背景由 Card 自绘（paintEvent），不直接用 QSS：
       QSS 的 border-radius/background 对普通 QWidget 在 macOS 不裁剪背景，永远直角。
       头部/主体均为透明容器，继承卡片底色，避免多层背景色差。 */
    QLabel#hstat {{ font-size: {metrics.FONT_STAT}px; font-weight: 600; background: transparent; }}

    /* ---------- 设置窗口（侧栏导航 + 主区） ---------- */
    QWidget#set-page {{ background: {t['surface']}; }}
    QListWidget#set-nav {{
        background: {t['surface3']}; border: none; border-right: 1px solid {t['border']};
        outline: 0; padding: {metrics.NAV_LIST_PAD}px;
    }}
    QListWidget#set-nav::item {{
        padding: {metrics.NAV_ITEM_PAD[0]}px {metrics.NAV_ITEM_PAD[1]}px; border-radius: {metrics.RADIUS_SM}px; color: {t['text2']}; font-size: {metrics.FONT_BODY}px;
    }}
    QListWidget#set-nav::item:selected {{
        background: {t['accent_soft']}; color: {t['accent_text']}; font-weight: 600;
    }}
    QListWidget#set-nav::item:hover:!selected {{ background: {t['surface2']}; }}

    /* ---------- 弹层 / 浮层窗口 ---------- */
    QWidget#popover, QWidget#overlay {{
        background: {t['surface']};
        border: 1px solid {t['border']};
        border-radius: {metrics.RADIUS_LG}px;
    }}

    /* ---------- 识别窗口结果区：原生 QTabWidget + 主题叠加 ---------- */
    /* pane 上移 1px 让 tab 行盖住顶边，消除"圆角边框被 tab 切断"的残边；
       tab 行透明、选中态用 accent 下划线，比自搓分段控件在 macOS 上清晰。 */
    QTabWidget::pane {{
        border: 1px solid {t['border']};
        top: -1px;
        background: {t['surface']};
    }}
    QTabBar {{ background: transparent; }}
    QTabBar::tab {{
        background: transparent; color: {t['text2']};
        padding: {metrics.GAP_MD}px {metrics.TAB_HPAD}px; border: none; border-bottom: 2px solid transparent;
    }}
    QTabBar::tab:selected {{ color: {t['accent_text']}; border-bottom: 2px solid {t['accent_text']}; }}
    QTabBar::tab:hover:!selected {{ color: {t['text1']}; }}

    /* 图片区（QGraphicsView）加与右侧 pane 一致的 1px 直角边框，左右视觉协调。
       直角而非圆角：macOS 普通 widget 的 border-radius 不裁剪背景，圆角会露直角背景，
       反而更丑；直角边框在 macOS 普通 widget 上正常绘制，且与右侧直角 pane 对齐。
       底色仍由 SelectionView.setBackgroundBrush 负责（QSS background 不裁剪）。 */
    QGraphicsView#selview {{ border: 1px solid {t['border']}; }}
    """


def current_system_theme() -> str:
    """读取 macOS 外观偏好（AppleInterfaceStyle）。

    非 macOS 或读不到时回退 light。
    """
    try:
        from Foundation import NSUserDefaults  # type: ignore

        defaults = NSUserDefaults.standardUserDefaults()
        style = defaults.stringForKey_("AppleInterfaceStyle")
        return "dark" if style == "Dark" else "light"
    except Exception:
        return "light"


# ---------------------------------------------------------------------------
# 实时换肤：监听系统外观变化，统一驱动「全局 QSS」+「自绘控件」。
#
# 设计要点：
# - 自绘控件（Card / 倒计时浮层等）在 paintEvent 里实时调用 ``_theme_tokens()``
#   （即 current_system_theme()），所以只要触发一次重绘就能换肤；
# - 少数在初始化时把主题色「烤进」QSS 字符串的控件（熔断卡片、插件状态行图标）
#   需要订阅 theme_changed 重新设置；本模块只负责发信号，刷新逻辑在各自控件里。
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)


class ThemeManager(QObject):
    """单例主题管理器。主题变化时发出 ``theme_changed(theme)``。

    自绘控件 / 烤色控件订阅它来重绘，避免各自去轮询系统外观。
    """

    theme_changed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._theme = current_system_theme()

    @property
    def theme(self) -> str:
        return self._theme

    def apply(self, app, theme: str | None = None) -> None:
        """应用主题：重设全局 QSS 并发出 theme_changed。

        Args:
            app: QApplication 实例（用于 setStyleSheet）。
            theme: 显式主题；为 None 时按系统当前外观检测。
        """
        theme = theme or current_system_theme()
        if theme not in TOKENS:
            theme = "light"
        self._theme = theme
        try:
            app.setStyleSheet(build_stylesheet(theme))
        except Exception:  # noqa: BLE001
            log.warning("setStyleSheet 失败，跳过重设")
        self.theme_changed.emit(theme)


_manager = ThemeManager()

# 暴露为模块级属性，方便各控件直接 ``styles.theme_changed.connect(...)``
theme_changed = _manager.theme_changed


def current_theme() -> str:
    """返回当前生效主题（由 apply 缓存，初始读系统外观）。"""
    return _manager.theme


def apply_theme(app, theme: str | None = None) -> None:
    """应用主题（对外入口）。"""
    _manager.apply(app, theme)


def watch_system_theme(app) -> None:
    """安装系统外观监听，系统切浅/深时自动换肤。

    - 跨平台：Qt 6.5+ 的 ``QStyleHints.colorSchemeChanged``。
    - macOS 双保险：直接监听 ``AppleInterfaceThemeChangedNotification``
      （Accessory 应用下 Qt 的 colorSchemeChanged 偶尔不触发，这个最可靠）。
      同时监听 ``AppleColorPreferencesChangedNotification`` 以防兼容性差异。
    """
    try:
        app.styleHints().colorSchemeChanged.connect(
            lambda *_: apply_theme(app)
        )
    except Exception as e:  # noqa: BLE001
        log.warning("colorSchemeChanged 监听失败：%s", e)

    if platform.system() == "Darwin":
        try:
            from Foundation import NSDistributedNotificationCenter, NSObject

            class _ThemeObs(NSObject):
                def themeChanged_(self, _note):
                    apply_theme(app)

            obs = _ThemeObs.alloc().init()
            obs._app = app  # 保活引用，避免被 GC 后回调悬空
            center = NSDistributedNotificationCenter.defaultCenter()
            center.addObserver_selector_name_object_(
                obs, "themeChanged:",
                "AppleInterfaceThemeChangedNotification", None,
            )
            center.addObserver_selector_name_object_(
                obs, "themeChanged:",
                "AppleColorPreferencesChangedNotification", None,
            )
            # 模块级保活，确保监听在应用生命周期内持续有效
            globals()["_mac_theme_observer"] = obs
        except Exception as e:  # noqa: BLE001
            log.warning("macOS 外观通知监听失败：%s", e)
