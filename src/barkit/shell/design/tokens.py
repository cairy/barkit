"""shell/design/tokens：设计令牌唯一真源（方案 v3 §4 / §6.5）。

`ui/styles.py` 的 TOKENS 自本文件 re-export（单一真源原则，批次 0 定案）：
- 实际数据在这里（含 light / dark 各 **25** 项 + 品牌渐变两端常量）；
- ``ui/styles.py`` 保留 TOKENS 名字做兼容 re-export；
- 禁止在任何业务代码里硬编码颜色——所有取值经 TOKENS（门禁 test_no_hardcoded_colors）。

``shadow_pop`` **已于 2026-09-21 删除**：它唯一的消费方是 Phase 1「玻璃 A」给设置页分区卡挂的
``QGraphicsDropShadowEffect`` 软投影，而这层投影在真机上被判定为「非常丑」（且每帧强制一次
离屏合成）。卡片外观回到「实心 surface + 1px 描边」—— 与面板 / 详情窗的卡同一门语言。
判据与实测（离屏 + 真机逐像素）→ `内部记录（观感返工）`。

## 语义色：一个色相四档（2026-09-19 定案）

判据与实算见 `内部记录（语义色）`。**「一符两用」是量出 33 格
对比度不达标的唯一成因** —— 所以每档一个键，每键只有一个角色，别再把填充色
拿去写字（也别把文字色拿去当底）：

| 档 | 键 | 面上压什么 | 判据 |
| --- | --- | --- | --- |
| **实体档** | `accent` `accent_hover` `red_solid` `red_solid_hover` | `on_solid` 白**文字** | 与白 ≥ **4.5**（WCAG 1.4.3） |
| **图形档** | `green` `orange` `red` | 白**图形**（角标圆盘里的字形、开关滑块、行首级别图标） | 与白 ≥ **3.0**（1.4.11） |
| **前景档** | `accent_text` `accent_text_hover` `green_text` `orange_text` `red_text` | 无（**它自己**就是文字 / 图标 / 描边） | 与 `surface*` / `*_soft` ≥ **4.5** |
| **浅底档** | `accent_soft` `green_soft` `orange_soft` `red_soft` | 只当底 | — |

两处**故意**的同值/分叉，别当抄错：

- 图形档**两套同值**：它压在**菜单栏**上（角标圆盘），菜单栏明暗未知，取值要
  同时压得住白字形、又在浅底与深底上都看得见 ⇒ 解出来的答案是同一个（同
  `on_solid` 那条「物理要求与主题无关」）。
- 浅色下 `red_solid` 与 `red_text` 同值：**「白字压在红上」与「红字压在白上」
  本来就是同一个比值**，同色相下答案自然重合；深色下两者分家
  （`#CE4237` vs `#FF6B60`）。
"""

from __future__ import annotations

# ---- 设计令牌（与 UI设计方案.md §3 一一对应）----
# Qt 不支持 CSS 变量，这里用 dict 承载，build 时拼进 QSS。
# 括号里是**实测比值**（2026-09-19，写死在这里是为了下次改值时知道原来差多少）。
TOKENS: dict[str, dict[str, str]] = {
    "light": {
        # —— 实体档：面上压白字（与白 ≥4.5）——
        "accent": "#5E5CE6",         # 5.06
        "accent_hover": "#4F4FD8",   # 6.10
        "red_solid": "#C7362C",      # 5.26 —— 危险按钮的面（`red` 是图形档，压不住白字）
        "red_solid_hover": "#AC2E25",  # 6.62
        # —— 图形档：面上压白图形（与白 ≥3.0；换成当字写只有 2.0–3.5）——
        "green": "#289A45",          # 3.62（浅底上 3.33、深底上 3.95）
        "orange": "#BE7707",         # 3.60（浅底上 3.31、深底上 3.97）
        "red": "#FF3B30",            # 3.55（原值，本来就过 3.0）
        # —— 前景档：自身就是文字 / 图标 / 描边（压在 surface / *_soft 上 ≥4.5）——
        "accent_text": "#504EC5",        # 白底 6.49 / accent_soft 5.56
        "accent_text_hover": "#4543AE",  # accent_soft 6.74
        "green_text": "#1E7A41",         # 白底 5.36 / green_soft 4.86
        "orange_text": "#8E5900",        # 白底 5.87 / orange_soft 5.35
        "red_text": "#C7362C",           # 白底 5.26 / red_soft 4.52
        # —— 浅底档 ——
        "accent_soft": "#ECECFD",
        "green_soft": "#E7F8EC",
        "orange_soft": "#FFF3E0",
        "red_soft": "#FFE9E7",
        # —— 实心色块上的**前景**（实体档的面上、logo 条形码、开关滑块）——
        # 语义定为「压在实心色块上的前景色」，与前景档（颜色当文字）互为对偶。
        # 两套**刻意同值** —— 本仓的品牌语言就是「实心块 + 白前景」，不随主题分叉
        # （分叉了按钮在深浅下就是两个牌子）。
        "on_solid": "#FFFFFF",
        # —— 中性 ——
        "text1": "#1D1D1F",
        "text2": "#6E6E73",
        # 装饰档：chevron / 浮层 hint / 拖拽手柄这类**非信息**位。信息位（计数、
        # 芯片名、状态小字）用 `text2` —— `text3` 浅色下只有 3.26:1。
        "text3": "#8E8E93",
        "surface": "#FFFFFF",
        "surface2": "#FAFAFC",
        "surface3": "#F5F5F7",
        "border": "#E4E4E9",
        "border_strong": "#D4D4D9",
    },
    "dark": {
        "accent": "#6966D5",         # 4.71（原 #7D7AFF 只有 3.44）
        "accent_hover": "#5B58C6",   # 5.74
        "red_solid": "#CE4237",      # 4.71（原 red #FF453A 只有 3.41）
        "red_solid_hover": "#B83B31",  # 5.66
        # 图形档与浅色**同值**（理由见模块 docstring）
        "green": "#289A45",
        "orange": "#BE7707",
        "red": "#FF453A",            # 3.41（原值，本来就过 3.0）
        "accent_text": "#9C9AFF",        # surface 5.77 / accent_soft 5.31
        "accent_text_hover": "#B0AEFF",  # accent_soft 6.47
        "green_text": "#30D158",         # surface 7.07 / green_soft 6.68（原值）
        "orange_text": "#FFB340",        # surface 8.01 / orange_soft 7.45（原值）
        "red_text": "#FF6B60",           # surface 5.12 / red_soft 5.44
        "accent_soft": "#2E2C52",
        "green_soft": "#1D3326",
        "orange_soft": "#3A2E12",
        "red_soft": "#3A1E1C",
        "on_solid": "#FFFFFF",
        "text1": "#F5F5F7",
        "text2": "#98989D",
        "text3": "#6E6E73",
        "surface": "#2A2A2E",
        "surface2": "#242427",
        "surface3": "#1F1F22",
        "border": "#3A3A3E",
        "border_strong": "#48484C",
    },
}

THEMES = tuple(TOKENS)           # ("light", "dark")

#: 品牌标记渐变（`LogoWidget`）的**两端**。**故意不进 TOKENS**：TOKENS 的语义是
#: 「同一个键、两套值」，而品牌色的定义就是**不随主题变**；写进两张表反而是同一个
#: 值抄两遍（两份真源）。
#: 起点取品牌紫的标称值（＝浅色主题的 ``accent`` 实体档）。**2026-09-19 起它不再
#: 读 ``accent``** —— 深色实体档为了压住白字要变深，品牌标记不该跟着一起闷下去
#: （那是「一符两用」的又一种：品牌图形 ≠ 按钮填充）。
BRAND_GRADIENT_START = "#5E5CE6"
#: 终点。即「色相区间固定、明度固定」——品牌标记在深浅两主题下长得一样。
BRAND_GRADIENT_END = "#8A5CF6"


def token(name: str, theme: str = "light") -> str:
    """取单个令牌值；主题不存在回退 light，令牌不存在抛 KeyError（早 fail）。"""
    return TOKENS.get(theme, TOKENS["light"])[name]
