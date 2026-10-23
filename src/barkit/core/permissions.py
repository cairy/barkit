"""core/permissions：权限**引导**（P0 0.8，方案 §7）。

本模块只剩「带用户去开权限」这一件事 —— 出引导语、跳系统设置面板。
**只读探测**（``accessibility_trusted`` / ``request_accessibility`` /
``open_accessibility_pane`` / ``available``）已抽到 ``core/ax_trust``（无 Qt
依赖），此处原样 re-export 保持既有消费方零改动。

为什么必须分开：检测可能来自 CLI 自检（`barkit doctor`，不得拖进 QtWidgets），
引导永远是 GUI 的事。分界线见 ``docs/log/2026-09-17-barkit-doctor.md`` §4.1。

引导语的**唯一真源**是 :data:`ACCESSIBILITY_GUIDE`：设置窗「维护」页的「权限」卡把它写进行内
反馈槽，热键路径（``ensure_accessibility``）把它填进对话框 —— 两条路同源，否则
同一件事会在两处出现两种说法。

一切仍以 AXIsProcessTrusted() 实测为准，不假设"授权过一次永久有效"。
"""

from __future__ import annotations

import logging

from PySide6.QtWidgets import QMessageBox

from barkit.core.ax_trust import (  # noqa: F401 —— re-export，既有消费方按原名取用
    ACCESSIBILITY_PANE_URL,
    accessibility_trusted,
    available,
    open_accessibility_pane,
    request_accessibility,
)

# 显式声明对外名字：检测类与引导类是**两个入口**，前者仍从这里可取
# （既有消费方 ``plugins/autotype/plugin.py`` / ``shell/settings.py`` 零改动）。
__all__ = [
    "ACCESSIBILITY_GUIDE",
    "ACCESSIBILITY_PANE_URL",
    "accessibility_trusted",
    "available",
    "ensure_accessibility",
    "open_accessibility_pane",
    "request_accessibility",
]

log = logging.getLogger(__name__)

#: 未授权时的**引导语唯一真源**：讲清「去哪儿开」与「开哪一个」。
#:
#: 必须点名「运行 BarKit 的那个程序」而不是「BarKit」—— TCC 记的是**可执行二进制**
#: 的身份，跑在终端里那就是「终端」；叫用户去列表里找 BarKit 是找不到的。
#: 末句「重启才生效」出自 2026-09-17 实测：关掉终端的辅助功能开关后要**重启**才失效
#: ⇒ 授权状态的变更不会当场对已运行进程生效，反向（刚勾上仍报未授予）同理 ——
#: 与其让用户猜，不如写进引导语。
ACCESSIBILITY_GUIDE = (
    "未授予辅助功能权限：一键输入要注入按键，必须由系统授权。"
    "请在「系统设置 → 隐私与安全性 → 辅助功能」中勾选运行 BarKit 的程序（开发期是「终端」）。"
    "列表里没有它时先点「触发授权弹窗」；勾选后若仍未生效，重启 BarKit 再试。"
)


def ensure_accessibility(parent=None) -> bool:
    """未授权时弹引导对话框。返回当前是否已授权。

    调用时机：需要 CGEvent 注入的功能（autotype）首次触发时 —— 也就是「一键输入」
    的**全局热键路径**：任何时刻都可能没有页面开着，挂不上行内反馈槽，故这里仍走
    对话框。设置窗「维护」页的「权限」卡已改走行内（引导语取 :data:`ACCESSIBILITY_GUIDE`，探测
    与两个动作直接调 :mod:`barkit.core.ax_trust`），不再经过本函数。

    **本路径仍挂账**（零模态收口的最后一批）：难处不在「弹框」而在「没有落点」——
    得先定热键路径把话说到哪里去，故与其余几处一起留待单独一轮，不在此半改。

    venv 重建 / Python 升级后旧授权会失效，此函数会重新引导。
    """
    if accessibility_trusted():
        return True
    log.warning("辅助功能权限未授予")
    box = QMessageBox(parent)
    box.setWindowTitle("需要辅助功能权限")
    box.setText(ACCESSIBILITY_GUIDE)
    open_btn = box.addButton("打开系统设置", QMessageBox.ButtonRole.AcceptRole)
    prompt_btn = box.addButton("触发授权弹窗", QMessageBox.ButtonRole.ActionRole)
    box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
    box.exec()
    clicked = box.clickedButton()
    if clicked == open_btn:
        open_accessibility_pane()
    elif clicked == prompt_btn:
        request_accessibility()
    return accessibility_trusted()
