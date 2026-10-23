"""shell/data_ops：两枚动作按钮 —— 打开数据目录 / 复制诊断。

**形状归框架、内容归组件**，但两枚的**落点完全不同**（2026-09-22 用户上报后重划）：

- **打开数据目录是全局的**：每个组件打开的都是**同一个**目录（``paths.APP_SUPPORT``），
  零授权、零成本。它因此只该在**一处**出现 —— 设置窗「维护」页的「数据」卡。
  它曾与「复制诊断」捆成一张「数据与诊断」卡、由框架画在**每个组件页的页尾**，
  于是这枚全局按钮被复制了 N 份（用户 2026-09-22 上报）。
- **复制诊断是组件自己的**：只有它知道该收集哪些事实（``Plugin.diagnostics_text()``）。
  它因此长在**组件自己那张卡里**、与「拨测」同一行 —— 用户在哪发现不对，就在哪复制，
  不必先去别处选组件。

与 ``daemon_ops`` 同一条边界（本模块只画界面、不认识任何插件）：

- 数据目录的**位置**归框架 —— UI 与插件都不该知道文件住在哪；
- 诊断文本的**内容**归组件（``Plugin.diagnostics_text()``），框架只负责把它放进剪贴板
  并说一句回执；
- 回执落**调用方给的槽**（零模态纪律，与 ``daemon_ops`` 同款：本模块不自己弹东西）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from PySide6.QtWidgets import QApplication, QPushButton

from barkit import paths
from barkit.core.proc import run_cmd
from barkit.shell.ui_kit import tool_button

log = logging.getLogger(__name__)

#: 回执回调：``(文案, 是否错误)``。省略时只记日志（无界面场景）。
Say = Callable[..., None]

#: 路径取数口：**每次点击现问一次**（理由见 :func:`reveal_button`）。
PathGetter = Callable[[], Path]


def _tell(say: "Say | None") -> "Callable[..., None]":
    """造一个「记日志 ＋（有槽就）说一句」的回执出口。"""
    def tell(text: str, error: bool = False) -> None:
        if error:
            log.error("%s", text)
        else:
            log.info("%s", text)
        if say is not None:
            say(text, error)

    return tell


def data_dir_button(
    parent=None, *, say: "Say | None" = None
) -> QPushButton:
    """「打开数据目录」按钮 —— **全局唯一落点**是设置窗「维护」页的「数据」卡。

    ``say``：动作回执的落点（``say(文案, error=True/False)``）。省略 = 只记日志。

    **成功不说话**：访达窗口跳出来就是回执。
    """
    return reveal_button("打开数据目录", lambda: paths.APP_SUPPORT,
                         parent=parent, say=say, what="数据目录")


def reveal_button(
    label: str, target: "PathGetter", parent=None, *,
    say: "Say | None" = None, what: str | None = None,
) -> QPushButton:
    """「在访达里揭示某个路径」按钮。

    这是「打开数据目录」与「日志」卡那五枚按钮**共用的唯一实现**（2026-10-13）——
    它们做的事逐字相同（跑 ``open``、失败写槽），此前只有一处消费方时不必抽，
    日志卡一上来就变成第五处。

    ``target`` 是**取数口**而不是路径值：路径常量必须调用期读（``tests/conftest.py``
    的 ``PATH_REDIRECTS`` 逐用例改 ``barkit.paths`` 的模块属性，导入期冻成值会让重定向
    静默失效 —— 测试会把真实用户目录打开。门禁 ``test_no_by_value_path_imports`` 守着）；
    崩溃报告那种「最新一份」还会变。
    ``what`` 是回执里的名词（省略即用 ``label``）—— 按钮上写「打开数据目录」，
    回执里该说「没能打开数据目录」而不是「没能打开『打开数据目录』」。
    """
    name = what or label
    btn = tool_button(label, "pill-ghost", parent=parent)
    btn.clicked.connect(lambda *_: reveal(target, _tell(say), name))
    return btn


def reveal(target: "PathGetter", tell: "Callable[..., None]", what: str) -> None:
    """在访达里揭示 ``target()``：是目录就打开它，是文件就打开它所在目录并选中它。

    命令走 ``core/proc.run_cmd``（禁 shell、超时清进程组），与其余系统动作同路。
    """
    try:
        path = target()
    except Exception as e:  # noqa: BLE001 —— 取路径失败不该冒泡到 UI 线程
        tell(f"没能打开{what} —— 请重试。", True)
        log.warning("取「%s」的路径失败：%s", what, e)
        return
    # 目录用 open（进去）、文件用 open -R（开父目录并选中它）；不存在时按文件处理 ⇒
    # 落在父目录上，用户仍能看见「本来该在那儿的东西」不在。
    args = ["open", str(path)] if path.is_dir() else ["open", "-R", str(path)]
    try:
        res = run_cmd(args, timeout=10)
    except Exception as e:  # noqa: BLE001 —— 超时 / 可执行缺失都不该冒泡到 UI 线程
        tell(f"没能打开{what} —— 请重试。", True)
        log.warning("打开%s失败：%s", what, e)
        return
    if res.returncode != 0:
        tell(f"没能打开{what} —— 请重试。", True)
        log.warning(
            "打开%s失败（rc=%s）：%s", what, res.returncode, (res.stderr or "").strip()
        )


def diagnostics_button(
    parent, diagnostics: "Callable[[], str | None]", *, say: "Say | None" = None
) -> QPushButton:
    """「复制诊断」按钮 —— 长在**组件自己那张卡里**（与「拨测」同排）。

    ``diagnostics``：**每次点击现问一次**的诊断文本生成口（通常就是
    ``plugin.diagnostics_text``）。传的是**可调用**而不是文本：用户点「复制诊断」
    的场景是他刚发现不对，要的是**此刻**的事实，不是画这一页那一刻的。

    判据（画不画这枚按钮）仍在组件侧：它有没有诊断可给（是否覆写了
    ``Plugin.diagnostics_text``）—— 不画一个点了只能得到「没东西」的按钮。
    """
    btn = tool_button("复制诊断", "pill-ghost", parent=parent)
    btn.clicked.connect(lambda *_: _copy_diagnostics(diagnostics, _tell(say)))
    return btn


def _copy_diagnostics(
    diagnostics: "Callable[[], str | None]", tell: "Callable[..., None]"
) -> None:
    """把组件给的诊断文本放进剪贴板（**当场重新生成**，见 ``diagnostics_button``）。"""
    try:
        text = diagnostics()
    except Exception:  # noqa: BLE001 —— 诊断生成失败不该拖垮设置窗
        log.exception("生成诊断文本失败")
        tell("生成诊断信息失败 —— 请重试。", True)
        return
    if not text:
        tell("这个组件没有可复制的诊断信息。", True)
        return
    QApplication.clipboard().setText(text)
    tell(f"诊断信息已复制到剪贴板（{len(text)} 字符）。")
