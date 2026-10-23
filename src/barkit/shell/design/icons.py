"""shell/design/icons：语义图标集（方案 v3 §4 / §6.5；两族资源见决议 D19）。

取代散落各处的 emoji（``"🧭"`` → IconName.DNS、``"✨"`` → IconName.GEAR）。
每个 IconName 对应一段线性 SVG（stroke 跟随 currentColor），渲染为 template 图标，
颜色由调用方传（通常取 TOKENS 的 text1 / accent），从而自动跟随明暗主题——
图标自身不感知主题。

**两族资源 + 一组兜底**（D19）：

| 族 | 网格 | 视窗 | 用途 | 入口 |
|---|---|---|---|---|
| 主字形 | 24 | `0 0 24 24` | 标题栏 / 列表行 / 面板 / 托盘底图 | ``pixmap()`` |
| 角标字形 | 12 | `0 0 12 12` | 托盘角标（12px 实心圆内），**可缺省** | ``badge_pixmap()`` |

24 网格主字形缩到 14px 后有效笔画约 0.7px，**必糊** —— 所以角标不是「同一个字形换个尺寸」，
而是两套资源。角标字形缺省时 ``badge_name()`` 回落到该取值**级别**的通用标记
（``MARK_OK`` / ``MARK_IDLE`` / ``MARK_OFF`` / ``MARK_WARN`` / ``MARK_ERR``），
于是任何组件至少能用 ``MARK_*`` 满足「图标随取值变」，领域变体按需增量增加。

字形内**不得写颜色字面量**（`#rrggbb` / `rgb()`）：描边颜色由本模块统一注入，
需要**实心填充**时写 `fill="currentColor" stroke="none"` —— 根节点已给出
`style="color:<color>"`，故 `currentColor` 与描边同色、同样跟随调用方传入的颜色
（模板图／主题色都不受影响）。门禁 `test_glyph_style_contract` 守这条约定。
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from barkit.contribution.specs import IconName, StateLevel

# 主字形：IconName -> SVG 内部标记（24×24 视窗，stroke 继承外层 svg 的 stroke 属性）
_GLYPHS: dict[IconName, str] = {
    IconName.ROUTE: (
        '<path d="M5 18h4a4 4 0 0 0 4-4V9a4 4 0 0 1 4-4h2"/>'
        '<path d="M16 2.5 19.5 5 16 7.5"/>'
        '<circle cx="5" cy="18" r="1.8"/>'
    ),
    # 路由 + 注意（领域变体，2026-10-05 新增）：做法与 ``TABLET_WARN`` 同款 ——
    # **保留折线与起点圆点，只把末端箭头换成叹号**。叹号尺寸与 `TABLET_WARN`
    # 同档（竖笔 4.6、点与竖笔之间留 3.2 格），保证灰阶下两枚分得开。
    # 叹号摆在原箭头那个角上（x≈18）而不是图形正中：占住「箭头本来在的位置」，
    # 换图时墨迹的重心几乎不动，菜单栏那一格闪一下就读得出来。
    IconName.ROUTE_WARN: (
        '<path d="M5 18h4a4 4 0 0 0 4-4V9a4 4 0 0 1 4-4h2"/>'
        '<circle cx="5" cy="18" r="1.8"/>'
        '<path d="M18 3.2v4.6"/>'
        '<path d="M18 11h.01"/>'
    ),
    IconName.SHIELD: (
        '<path d="M12 3.2 19 6v5.2c0 4.8-3.2 7.9-7 9.6-3.8-1.7-7-4.8-7-9.6V6z"/>'
    ),
    IconName.BELL: (
        # 铃：**钟体（圆顶 ＋ 外撇底沿）＋ 铃舌（下坠小弧）**，与主字形同构图。
        # y 坐标是按「墨迹居中」摆的（不是按「看着差不多」）：钟体下沿最宽、铃舌
        # 还坠在下面，几何随手摆一定偏低 —— 本族基线是「墨迹 bbox 中心落在 12」
        # （见 tests 里的居中门禁），早先那版墨迹中心在 13.75（偏低 1.75 格，
        # 全族最大），真机上就是「组件图标整体吊在格子里偏下」。
        '<path d="M6.5 13.75v-5a5.5 5.5 0 0 1 11 0v5l1.6 2.7H4.9z"/>'
        '<path d="M10 18.75a2 2 0 0 0 4 0"/>'
    ),
    # 键盘（一键输入）：外框 ＋ 上排三键 ＋ 一条空格键，**全是连续线条**。
    # 两条硬约束（改几何前先算，别凭眼睛）：
    # ① **居中**：外框 3–21 / 7–18 ⇒ 中心 (12, 12.5)。每行键位的**跨度**中心必须
    #   落在 12，两行合起来的中心必须落在 12.5 —— 只看「段的起点」排会整体偏
    #   （上一版四段 5.8→16.3 中心 11.05，偏左近 1 格，就是这么错的）。
    #   上排 5.5–18.5 中心 12；空格 8–16 中心 12；两行 10.5 / 14.5 中心 12.5。
    # ② **段间距 > 描边宽**：16px 下 1 格 ≈ 0.67px、描边 1.6 格 ≈ 1.07px ⇒ 间隙
    #   要留 ≥ 2.5 格才不会被描边吃掉。这就是上排只能放**三键**（2.6 长 ＋ 2.6
    #   间隔）而不是四键的原因：四键塞进 18 格宽，间隙只剩 2 格，真机上会连成
    #   一条实线。段长 2.6 与 TABLET 内部那条细节线（3 格）同档 —— 再短就会
    #   显得「内部细节比别的图标小」。
    # （更早那版用零长线段 h.01 画圆点表键位，真机下是亚像素灰糊噪点，已废。）
    IconName.KEYBOARD: (
        '<rect x="3" y="7" width="18" height="11" rx="2"/>'
        '<path d="M5.5 10.5h2.6M10.7 10.5h2.6M15.9 10.5h2.6"/>'
        '<path d="M8 14.5h8"/>'
    ),
    IconName.SCAN: (
        '<path d="M4 8V5.5A1.5 1.5 0 0 1 5.5 4H8"/>'
        '<path d="M16 4h2.5A1.5 1.5 0 0 1 20 5.5V8"/>'
        '<path d="M20 16v2.5a1.5 1.5 0 0 1-1.5 1.5H16"/>'
        '<path d="M8 20H5.5A1.5 1.5 0 0 1 4 18.5V16"/>'
        '<path d="M4 12h16"/>'
    ),
    IconName.TABLET: (
        '<rect x="5.5" y="3.5" width="13" height="17" rx="2.5"/>'
        '<path d="M10.5 17.8h3"/>'
    ),
    # 平板 + 注意：**保留平板轮廓与内圈比例**，把底部那条 home 线换成框内的叹号
    # —— 取值翻转时「这是个平板」的识别特征不动，只有框里的东西变。叹号尺寸与
    # `MARK_WARN` 同档（竖笔 4.6、点上移），保证两枚在灰阶下都分得开。
    IconName.TABLET_WARN: (
        '<rect x="5.5" y="3.5" width="13" height="17" rx="2.5"/>'
        '<path d="M12 8.4v4.6"/>'
        '<path d="M12 16.2h.01"/>'
    ),
    IconName.DNS: (
        '<rect x="4" y="4.5" width="16" height="6.5" rx="1.6"/>'
        '<rect x="4" y="13" width="16" height="6.5" rx="1.6"/>'
        '<path d="M7.2 7.75h.01M7.2 16.25h.01"/>'
    ),
    # 滑块（设置）：**两条轨道 ＋ 两个圆钮**，钮位左右错开（上轨偏左、下轨偏右）。
    # 不用齿轮是刻意的 —— 24 网格 ＋ 1.6 描边下，齿轮要么糊成一团、要么密到发毛
    # （试过 6 齿 / 8 齿轮廓两种，放大看都更像花）。滑块只有「横直线 ＋ 圆」两种元素，
    # 与 DNS / TABLET 那类「一个主体 ＋ 一两笔细节」的构图同档，14–16px 下仍读得出
    # 是两个可拨的钮。语义上它同样是「调整参数」的通用代表，与「设置」等价。
    IconName.GEAR: (
        '<path d="M4 8.5h16"/>'
        '<circle cx="8.8" cy="8.5" r="2.4"/>'
        '<path d="M4 15.5h16"/>'
        '<circle cx="15.2" cy="15.5" r="2.4"/>'
    ),
    # 电源（退出）：标准写法 = 竖杠 ＋ 顶部断口的圆环。两条独立路径，靠大弧标志
    # 把圆环画成「开口朝上」—— 与「实心 / 空心」那类取值图标不同，它不带状态语义，
    # 只是「退出」这个动作的代表字形。
    IconName.POWER: (
        '<path d="M12 3.6v8.2"/>'
        '<path d="M8.2 7.4a6.4 6.4 0 1 0 7.6 0"/>'
    ),
    # 屏幕字形（取值图标，录屏通知组件首发需求方）：**纯屏无底座**，
    # 靠「屏内是否实心」区分取值 —— 实心 = 第二屏在线（屏内有内容），
    # 仅外框 + 底缀线 = 无第二屏（空屏，去掉了原有的内圈白线）。两枚共用同一
    # 外框与底缀线路径：状态翻转时轮廓不动、只有屏内明暗变，菜单栏那一格换图
    # 视觉最稳（也是「底图换源」能用的前提，见托盘底图来源设置）。
    IconName.DISPLAY_ON: (
        '<rect x="3" y="5" width="18" height="12.5" rx="2.5"/>'
        '<rect x="5.5" y="7.5" width="13" height="7.5" rx="1.2" fill="currentColor" stroke="none"/>'
        '<path d="M9 20.5h6"/>'
    ),
    IconName.DISPLAY_OFF: (
        '<rect x="3" y="5" width="18" height="12.5" rx="2.5"/>'
        '<path d="M9 20.5h6"/>'
    ),
    # 电池族三枚（领域变体，2026-09-19 新增）：**共用同一外框与正极触点**，
    # 靠框内内容区分 —— 框内不画东西的是 `BATTERY`（族里的**基础字形**，多作
    # 身份图标 / 主图标，**不表示「没电」**；留空是为了让三枚外框逐像素一致），
    # 左起一根窄实心条 = 电量低，框内闪电 = 充电中。外框（`x=2.5 y=6.5
    # w=16.5 h=11`）与触点（`x=21`）是三枚共用的常量 —— 改一处必须改三处，
    # 故写死不抽（抽了也只在本文用，反而看不出「三枚同框」这个约束）。
    IconName.BATTERY: (
        '<rect x="2.5" y="6.5" width="16.5" height="11" rx="2.6"/>'
        '<path d="M21 10.2v3.6"/>'
    ),
    IconName.BATTERY_LOW: (
        '<rect x="2.5" y="6.5" width="16.5" height="11" rx="2.6"/>'
        '<rect x="4.9" y="8.9" width="3" height="6.2" rx="1.2"'
        ' fill="currentColor" stroke="none"/>'
        '<path d="M21 10.2v3.6"/>'
    ),
    IconName.BATTERY_CHARGING: (
        '<rect x="2.5" y="6.5" width="16.5" height="11" rx="2.6"/>'
        # 闪电是描边多边形（与 `POWER` 同档）。**重心要落在外框中心**：
        # 顶点 x 8.9–13.1（中心 11.0，外框中心 10.75）、y 8.7–15.5，加 0.8
        # 描边半径后 7.9–16.3，离框内沿（7.3–16.7）上下各留 0.6 —— 不咬框线。
        '<path d="M11.9 8.7 8.9 12.55h2.4l-1.2 2.95 3-3.85h-2.4z"/>'
        '<path d="M21 10.2v3.6"/>'
    ),
    # 耳机（3.5mm 有线耳机）：头梁 ＋ 左右两个耳罩。**墨迹中心必须落在 (12, 12)**
    # （全族基线，有居中门禁）：几何 bbox 是 x 3.5–20.5 / y 4.5–19.5，中心恰好 (12, 12)。
    # 头梁用半圆弧（半径 6 ＝ 弦长 12 的一半）⇒ 弧顶 y = 10.5 − 6 = 4.5；竖直段收在
    # y=13，落在耳罩（12.5–19.5）里面 —— 这样两者是**接上**的，不是两根漂着的线。
    IconName.HEADSET: (
        '<path d="M6 13v-2.5a6 6 0 0 1 12 0V13"/>'
        '<rect x="3.5" y="12.5" width="5" height="7" rx="2.2"/>'
        '<rect x="15.5" y="12.5" width="5" height="7" rx="2.2"/>'
    ),
    # 鼠标（蓝牙看护）：**竖直胶囊 ＋ 上部一条竖线**（分隔左 / 右键）。这两笔就是「鼠标」
    # 在 24 网格上最省的识别特征 —— 少一笔认不出是什么，多一笔在 16px 下就糊成一团。
    # 几何照本仓两条硬约束核过：① 墨迹 bbox x 5–19 / y 2–22 ⇒ 中心 (12, 12)；
    # ② 竖线段长 4 格 > 描边 1.6 格，不会被描边吃掉（KEYBOARD 那枚踩过这条）。
    IconName.MOUSE: (
        '<rect x="5" y="2" width="14" height="20" rx="7"/>'
        '<path d="M12 6v4"/>'
    ),
    # 通用级别标记 5 枚（D19 的兜底资源，与 StateLevel 五值一一对应）。
    # 语义与色板同源：OK = 勾（一切正常）/ IDLE = 横线（开着但无事）/ OFF = 斜杠（关着）/
    # WARN = 三角叹号（需要注意）/ ERR = 圆叉（出错）。
    # **必须与颜色解耦**：这五枚在灰阶（OFF / IDLE 的 text3 前景）下也得分得开，
    # 所以形状各不同，不是「同一个圆换颜色」。
    IconName.MARK_OK: (
        '<circle cx="12" cy="12" r="8.6"/>'
        '<path d="M7.8 12.4l2.9 2.9 5.5-6.1"/>'
    ),
    IconName.MARK_IDLE: (
        '<circle cx="12" cy="12" r="8.6"/>'
        '<path d="M8 12h8"/>'
    ),
    IconName.MARK_OFF: (
        '<circle cx="12" cy="12" r="8.6"/>'
        '<path d="M6.1 6.1 17.9 17.9"/>'
    ),
    IconName.MARK_WARN: (
        '<path d="M12 3.4 21.3 19.6H2.7z"/>'
        '<path d="M12 9.6v4.3"/>'
        '<path d="M12 17.1h.01"/>'
    ),
    IconName.MARK_ERR: (
        '<circle cx="12" cy="12" r="8.6"/>'
        '<path d="M9.1 9.1l5.8 5.8M14.9 9.1l-5.8 5.8"/>'
    ),
    # 清单（首个消费方＝`todo` 插件，身份图标）：一张卡片 ＋ 两条待办行。**墨迹中心
    # 必须落在 (12, 12)**（全族基线，有居中门禁）：框体 stroke 中心线
    # x 4–20 / y 4.5–19.5，加描边半径 0.8 后外沿 x 3.2–20.8 / y 3.7–20.3 ⇒ 中心仍
    # (12, 12)；四边余量 ≥ 3.2 格，远高于 0.15 的不压线下限。
    # 两条横线只画**内部**行：三行以上在 16px 下会糊成一块灰（KEYBOARD 那枚踩过
    # 同一条 —— 段长与段间距都有下限）。
    IconName.CHECKLIST: (
        '<rect x="4" y="4.5" width="16" height="15" rx="2.5"/>'
        '<path d="M7.5 9.5h9M7.5 14.5h5.5"/>'
    ),
}

# 角标字形族（12 网格简化版）：只放**有专用简化字形**的图标，缺省即下表的级别兜底。
# 约束（D19「硬要求 2」）：12 网格、笔画 ≥1.6、**不超过 3 段** —— 段数一多就成一团墨点。
# 五枚 ``MARK_*`` 是通用级别标记的角标版（任何取值都能靠它们上角标）；领域图标按
# D19「按需增量」随组件补，无角标版时同一回落规则生效（见 ``badge_name``）。
_BADGE_GLYPHS: dict[IconName, str] = {
    IconName.MARK_OK: (
        '<circle cx="6" cy="6" r="4.3"/>'
        '<path d="M3.85 6.2 5.6 7.95 8.35 4.75"/>'
    ),
    IconName.MARK_IDLE: (
        '<circle cx="6" cy="6" r="4.3"/>'
        '<path d="M3.7 6h4.6"/>'
    ),
    IconName.MARK_OFF: (
        '<circle cx="6" cy="6" r="4.3"/>'
        '<path d="M3.05 3.05 8.95 8.95"/>'
    ),
    IconName.MARK_WARN: (
        # 描边（空心）三角**轻微光学上移 0.35 格**（apex 1.25 / base 10.05）：
        # 轮廓线的墨迹沿路径分布，质量重心在下半部（底边 + 两腰下段），几何居中
        # 会让观感「偏下」。两次实机目测锚定区间：apex 0.9（上移 0.7）用户报「偏上」，
        # apex 1.6（几何居中，当时量 bbox 中心 dy=-0.03 —— 那量的是外框，构造上
        # 必为 0，不是视觉重心）用户报「偏下」⇒ 最优点取中间偏 1.6 侧 = 1.25。
        # 顶点 1.25 − 描边半径 0.8 = 0.45，笔画不触 viewBox 边。
        '<path d="M6 1.25 10.4 10.05H1.6z"/>'
        '<path d="M6 4.35v2.2"/>'
        '<path d="M6 8.25h.01"/>'
    ),
    IconName.MARK_ERR: (
        '<circle cx="6" cy="6" r="4.3"/>'
        '<path d="M4.4 4.4 7.6 7.6M7.6 4.4 4.4 7.6"/>'
    ),
    # ---- 领域角标（D19 按需增量）----
    # 录屏通知（notify_guard）三台状态机的专属角标：托盘角标从「级别通用标记」
    # 升级为「领域字形」，同级别下也能区分「是屏幕 / 铃 / 设置哪台机在报错」。
    # display 机：实心屏 = 第二屏在线、空心屏 = 无第二屏，与主字形同构（轮廓不动、
    # 只有屏内明暗变，菜单栏换图最稳）；notify 机主图标 = 铃；automation 机主图标
    # = 设置滑块（两轨 ＋ 一枚旋钮，≤3 段）。
    IconName.DISPLAY_OFF: (
        '<rect x="2.2" y="2.6" width="7.6" height="5.4" rx="1.2"/>'
        '<path d="M4.4 9.2h3.2"/>'
    ),
    IconName.DISPLAY_ON: (
        '<rect x="2.2" y="2.6" width="7.6" height="5.4" rx="1.2"/>'
        '<rect x="3.4" y="3.8" width="5.2" height="3.0" rx="0.6" fill="currentColor" stroke="none"/>'
        '<path d="M4.4 9.2h3.2"/>'
    ),
    IconName.BELL: (
        # 角标铃 = 主字形**钟体**的等比缩小（k≈0.55），**不带铃舌**。
        # 两处都是量出来的，别凭眼睛调：
        # ① **居中**：墨迹 bbox 落在 x 1.3–10.7 / y 1.58–10.43 ⇒ 中心 (6.0, 6.0)，
        #    与主字形那枚铃（以及 MARK_* 那五枚）同一条基线。早先那版中心在
        #    (6.0, 7.75) —— 偏低 1.75 格，8–10px 角标位上就是下沉约 1px。
        # ② **不压边**：下沿 10.43，离视窗还有 1.57 格。早先那版铃舌画到 12.05，
        #    超出 12 视窗被**裁掉下半笔** ⇒ 圆顶还在、底部变平 ⇒ 用户报的「被压扁」。
        # ③ **比例**：宽高比 0.94，等于主字形钟体的 0.94（早先那版是 0.89，
        #    比主字形扁一档 —— 同是铃，两族两种长相）。
        # 为什么**去掉铃舌**：铃舌要「看得见的缝」才能与钟体分开，缝 = 2×描边半径
        # ＋间隙 ≈ 2 格；而整枚塞进 12 视窗后留给铃舌的只有 0.5 格 ⇒ 画出来必然
        # 与底沿糊成一团（比没有更糟）。角标族本来就是「去细节保轮廓」
        #（GEAR 主字形两枚旋钮、角标只留一枚），这里同口径。
        '<path d="M2.98 8.14V5.4a3.02 3.02 0 0 1 6.04 0V8.14l.88 1.49H2.1z"/>'
    ),
    IconName.GEAR: (
        '<path d="M3 5.3h6"/>'
        '<path d="M3 8.7h6"/>'
        '<circle cx="8.4" cy="5.3" r="1.05"/>'
    ),
}

# 级别 -> 兜底标记（角标字形缺省时用；D19「兜底规则」）。
# 做成显式映射而非按名字拼装（``MARK_`` + level.value.upper()）：拼装会让
# 「级别枚举改名」静默改变图标解析结果，而这里必须让漏配**当场 KeyError**。
_LEVEL_MARK: dict[StateLevel, IconName] = {
    StateLevel.OK: IconName.MARK_OK,
    StateLevel.IDLE: IconName.MARK_IDLE,
    StateLevel.OFF: IconName.MARK_OFF,
    StateLevel.WARN: IconName.MARK_WARN,
    StateLevel.ERR: IconName.MARK_ERR,
}

_MAIN_VIEWBOX = "0 0 24 24"
_BADGE_VIEWBOX = "0 0 12 12"
_STROKE_WIDTH = "1.6"


def _markup(glyph: str, color: str, view_box: str) -> str:
    """字形片段 → 完整 SVG 文本（颜色 / 视窗 / 描边粗细三件事只在此处注入）。"""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{view_box}" '
        f'fill="none" stroke="{color}" stroke-width="{_STROKE_WIDTH}" '
        # style 是给字形里 fill="currentColor" 的实心填充用的（当前颜色 = 调用方
        # 传入的颜色）；少了它，currentColor 会落到初始值黑色，彩色渲染路径就错了。
        f'style="color:{color}" '
        'stroke-linecap="round" stroke-linejoin="round">'
        f"{glyph}</svg>"
    )


def _svg_markup(name: IconName, color: str) -> str:
    """主字形（24 网格）的渲染文本。"""
    return _markup(_GLYPHS[name], color, _MAIN_VIEWBOX)


def _badge_svg_markup(name: IconName, color: str) -> str:
    """角标字形（12 网格）的渲染文本。``name`` 必须已在 ``_BADGE_GLYPHS`` 里。"""
    return _markup(_BADGE_GLYPHS[name], color, _BADGE_VIEWBOX)


def _render(markup: str, size: int, dpr: float = 1.0) -> QPixmap:
    """字形片段 → 位图。``size`` 为**逻辑**像素；``dpr`` 非 1 时按物理像素渲染并给
    位图打上设备像素比，Retina 下不再被 Qt 放大发虚（与标题栏角标同一做法）。

    旧实现固定 1x，是底部栏快捷按钮图标偏软的根因：15 逻辑像素的位图在 dpr=2 屏上
    被 Qt 拉伸到 30 物理像素显示。
    """
    px = max(1, int(round(size * dpr)))
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    renderer = QSvgRenderer(QByteArray(markup.encode("utf-8")))
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    # 注意：devicePixelRatio 必须**在渲染完成之后**再打。若在 QPainter 创建前就
    # setDevicePixelRatio，painter 的 viewport 会被当成逻辑尺寸，SVG 只会画进左上角
    # 那一块物理像素，最终被 QIcon 当逻辑尺寸显示成「只露出一个小角」。
    renderer.render(painter)
    painter.end()
    pm.setDevicePixelRatio(dpr)
    return pm


def _color_str(color: str | QColor) -> str:
    return color.name() if isinstance(color, QColor) else color


def badge_name(icon: IconName | None, level: StateLevel | None) -> IconName:
    """角标字形解析（D19「兜底规则」）：有**专用角标字形**就用它，否则回落级别标记。

    ``icon`` 是取值的取值图标（``StateOption.icon`` 或报告里的 ``StateReport.icon``），
    ``level`` 是该取值的级别（代表态级别）。两者都能缺：
    只给 level 时按级别取标记；只给一个「没有角标版」的 icon 时同样回落级别；
    两者都没有时回落 ``MARK_IDLE``（「开着但无事」是最不误导的默认——不谎称正常、
    也不谎称出错）。

    **纯函数，不读全局状态**：解析结果只由入参决定，与渲染时机无关。
    """
    if icon is not None and icon in _BADGE_GLYPHS:
        return icon
    if level is not None:
        return _LEVEL_MARK[level]
    return IconName.MARK_IDLE


def pixmap(
    name: IconName, size: int, color: str | QColor, dpr: float = 1.0
) -> QPixmap:
    """渲染**主字形**为位图。``size`` 是**逻辑尺寸**；``dpr`` 默认 1.0。

    返回位图**逻辑尺寸 == ``size``**、物理 = ``size×dpr``（``dpr>1`` 时已
    ``setDevicePixelRatio``，Retina 上不再被放大发虚）。

    **注意** ``QPixmap.width()/height()`` 在 ``dpr>1`` 时返回**物理像素**，不等于
    ``size``。故布局一律用逻辑尺寸，且**不要直接调本函数**——自绘走
    ``ui_kit.draw_glyph``、控件资源走 ``ui_kit.glyph_pixmap``（它们替你取 dpr 并用
    逻辑尺寸定位）。2026-09-21 两次回归都是「直接调本函数 + 用 pm.width() 布局」造成的。
    """
    return _render(_svg_markup(name, _color_str(color)), size, dpr)


def badge_pixmap(
    icon: IconName | None,
    level: StateLevel | None,
    size: int,
    color: str | QColor,
    dpr: float = 1.0,
) -> QPixmap:
    """渲染**角标字形**（12 网格简化版；缺省回落该级别的通用标记，见 ``badge_name``）。

    与 ``pixmap()`` 是两个入口而非一个「带网格参数的入口」，因为**调用方拿到的资源
    不同**：角标位（12px 实心圆内）与主位（18–30px）在笔画预算上不可互换，
    合成一个函数会让「用错网格」变成一次静默的视觉降级。

    尺寸语义与 ``pixmap()`` 一致：``size`` 为**逻辑尺寸**，``dpr`` 决定物理分辨率；
    调用方走 ``ui_kit.badge_glyph_pixmap`` / ``ui_kit.draw_badge_glyph``。
    """
    return _render(
        _badge_svg_markup(badge_name(icon, level), _color_str(color)), size, dpr
    )


def icon(name: IconName, size: int, color: str | QColor) -> QIcon:
    """语义图标 → QIcon（``pixmap()`` 的 QIcon 版，签名与它逐参对齐）。

    ``color`` **故意没有默认值**：给一个「缺省灰」（本仓曾写死 ``#888888``）等于让
    调用方**静默**拿到一个不随主题变的值 —— 不报错、不崩溃，只是深色下看着发闷，
    正是最该由签名挡住的那种缺陷。要默认色就去 ``shell/design/tokens.py`` 加令牌。
    """
    return QIcon(pixmap(name, size, color))
