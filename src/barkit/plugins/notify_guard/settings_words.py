"""设置面用到的纯数据：常用场景预设（``RULE_PRESETS``）与三张状态词条表。**单独成模块**是为了让依赖成树 —— 入口要在定义类那一刻拿到五枚mixin，而这些词条被两个兄弟在模块作用域读 ⇒ 留入口会成环。

``settings.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations


# 常用场景预设（设计 §6.4.4：「新装用户的第一个动作不该是输入 OBS 的可执行文件名」）。
# 进程名一律小写 —— ``signals.match_rules`` 是**子串匹配**，小写 pattern 命中大小写各异
# 的真实路径（``signals.get_process_snapshot()`` 也把路径统一 lower 了）。
#
# **两条硬纪律**（2026-09-14 实测补齐，改前必读）：
#
# 1. **不得放「打开应用就在跑」的主程序名**。规则是 OR 关系、命中即静音，一个常驻进程
#    等于这条规则**恒命中**。飞书正是这么翻车的：预设原写 ``lark`` / ``feishu``，
#    而 ``/Applications/Lark.app/Contents/MacOS/feishu for sany`` 是**常驻主程序** ——
#    只要开着飞书（哪怕没开会）就报「会议进行中」。改用**会议专用 helper**
#    ``lark helper (iron)``：它随会议出现 / 消失（二值、生命周期同步），
#    这是《方案文档》§4.2 飞书诊断的定案，不是新拍的。
# 2. **不得放系统自带的后台进程名**。``airplay`` 原先在「投屏」里，但它命中的是
#    ``/System/Library/CoreServices/AirPlayUIAgent.app`` —— **macOS 常驻、每台机器都在跑**，
#    于是「投屏中」恒真（实测：本轮排查时它就在副标题里挂着）。系统进程不代表用户行为。
RULE_PRESETS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("录屏", "录屏软件运行中", ("obs", "kap", "quicktime player", "screencapture")),
    ("会议", "会议进行中", ("tencentmeeting", "wemeet", "zoom.us", "lark helper (iron)")),
    ("直播", "直播推流中", ("obs", "streamlabs")),
    # 「投屏」删掉 ``airplay`` 后只剩 ``tencentmeeting``（与「会议」重叠）。
    # **这是如实保留、不是漏改**：投屏没有干净的进程信号（AirPlay 那枚是系统常驻），
    # 而 ``tencentmeeting`` 是否也常驻**本轮没有实测证据**（它当时没在跑），故不擅改，
    # 挂账待实测后决定（见《跟踪表》§5）。
    ("投屏", "投屏中", ("tencentmeeting",)),
)


# 顶部状态条的词条（比 plugin 的整句 summary 更短，专供设置页这一行）
_DISPLAY_WORDS = {
    "extended": "iPad 已连接",
    "running": "运行中",
    "idle": "未运行",
    "absent": "未安装",
}
_NOTIFY_WORDS = {
    "allowed": "正常",
    "muted": "已静音",
    "manual": "手动覆盖中",
    "desync": "状态不同步",
    "frozen": "已停用",
}
_APP_WORDS = {"running": "运行中", "idle": "已安装 · 未运行", "absent": "未安装"}
