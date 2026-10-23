"""设置窗的模块级常量表与全族共用的日志名。

``settings.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import logging


#: 设置窗全族共用的日志名（`logging_setup` 的 `%(name)s` 会写进日志文件）。
#: 写死父包名而不是 `__name__`：子模块各自的 `__name__` 会把日志外观改掉。
log = logging.getLogger("barkit.shell.settings")


# 显示位的中文名（芯片文案与悬停提示共用一份）。键必须与 ``PLUGIN_UI_SLOTS`` 一一对应
# ——契约里新增一位而这里忘了补名，芯片会以英文键名出现；用例
# ``test_slot_label_table_matches_contract`` 守着这条。
_SLOT_LABELS = {
    "titlebar": "标题栏",
    "row": "列表行",
    "action": "快捷区域",
    "tray": "托盘角标",
}


#: 「刷新间隔」下拉的候选**阶梯**（秒）。它只是常用值的梯子，**不是合法域** ——
#: 用户手写在 `config.yaml` 里的离梯值会被如实列进候选（见 `_poll_choices`）。
#: 覆盖本仓实际用到的三档（3 / 30 / 300），并给出秒级到五分钟的常用档。
_POLL_LADDER: tuple[int, ...] = (1, 2, 3, 5, 10, 15, 30, 60, 120, 300)


#: 「日志级别」下拉候选 ``[(显示名, 取值), ...]``。**取值就是级别名**（写配置 /
#: 喂 ``resolve_level`` 直接用）；首项空串 = 「跟随启动环境」（回落删键，让
#: 环境变量档继续生效）。顺序按**从安静到吵**排：选下面那项就是「记更多」，
#: 与「角标最低级别」那档的「往下=更宽松」保持同一直觉。
_LOG_LEVEL_CHOICES: list[tuple[str, str]] = [
    ("跟随启动环境（默认）", ""),
    ("仅错误", "error"),
    ("警告", "warning"),
    ("信息", "info"),
    ("调试", "debug"),
]


_LOG_LEVEL_LABELS: dict[str, str] = dict(_LOG_LEVEL_CHOICES)


#: 托盘阈值两档的**用户文案**。取值域归契约（``contribution.specs.TRAY_MIN_LEVELS``），
#: 这里只管怎么说用户听得懂 —— 用户想知道的是「哪几级会在菜单栏出角标」，
#: 不是 ``warn`` / ``err`` 这两个字符串。
_BADGE_LEVEL_LABELS = {"warn": "警告与错误（默认）", "err": "仅错误"}
