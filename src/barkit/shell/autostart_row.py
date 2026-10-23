"""ui/autostart_row：状态面板「开机自启」设置行（方案 §7.1 + F.2）。

显示当前自启状态（已装/过期/运行中），提供安装/关闭按钮。
安装/卸载都是用户域操作、零授权；venv 重建后 wrapper 过期 → 面板提示重装。

**零模态**（2026-09-17 收口）：本行不弹任何框。反馈出口由所在页注入
（``feedback`` 参数）—— 行不需要知道槽挂在哪，只负责把话说给它。两档语义：
安装 / 关闭的**结果**走 ``info`` / ``error``；**关闭自启是破坏性的**（要摘掉
LaunchAgent），先 ``ask`` 由用户确认，拿不到槽位就**拒绝执行**（只记 warning）——
宁可用「点了没反应」，也不允许无确认地关掉自启。

改前的五处 ``QMessageBox``（安装失败 / 已更新 / 关闭确认 / 关闭失败 / 已关闭）
全部落在这一条槽上；成因与真机证据见 ``shell/ui_kit.FeedbackBar`` 的 docstring。
"""

from __future__ import annotations

import logging

from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from barkit.core import autostart
from barkit.shell.feedback_slot import FeedbackSlot
from barkit.shell.ui_kit import FeedbackBar, apply_text_role, tool_button

log = logging.getLogger(__name__)


class AutostartRow(QWidget):
    """一行：状态文案 + 安装/关闭按钮（**不带标题** —— 标题由所在那张卡给出）。

    2026-09-22 起它住在设置窗「维护」页的「开机自启」卡里，卡标题已经写了「开机自启」，
    行内再写一遍就是两行同名。

    ``feedback`` 是**所在卡**的行内反馈槽（设置窗建好卡时注入）。
    不带槽也能构造（用例 / 未来别处复用）：此时所有提示只记日志，破坏性动作拒绝。
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        feedback: FeedbackBar | None = None,
    ) -> None:
        super().__init__(parent)
        # 反馈出口：None ⇒ 只记日志（见模块 docstring 的两档语义）
        self._slot = FeedbackSlot()
        self._slot.attach(feedback)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)

        status = QLabel("…", self)
        apply_text_role(status, "status")
        # 两枚都走 ``tool_button`` 的**行内档**（22px）：裸 ``QPushButton`` 的自然高是
        # 30px，不在三档里 —— 同页别处 28px 的按钮旁摆两颗 30px 的，就是「风格不统一」
        # 的又一处来源（用户 2026-09-22 上报）。行内档与状态文案这一排的高度协调。
        install_btn = tool_button("安装", "pill-ghost", size="inline", parent=self)
        install_btn.clicked.connect(self._install)
        uninstall_btn = tool_button("关闭", "pill-ghost", size="inline", parent=self)
        uninstall_btn.clicked.connect(self._uninstall)

        layout.addStretch(1)
        layout.addWidget(status)
        layout.addWidget(install_btn)
        layout.addWidget(uninstall_btn)

        self._status = status
        self._install_btn = install_btn
        self._uninstall_btn = uninstall_btn
        self.refresh()

    # ---------- 状态 ----------

    def refresh(self) -> None:
        st = autostart.status()
        if not st["installed"]:
            self._status.setText("未安装")
            self._install_btn.setVisible(True)
            self._uninstall_btn.setVisible(False)
            return
        if st["stale"]:
            text = "运行环境已更新，需重新安装开机自启项"
        elif st["running"]:
            text = "已安装 · 运行中（崩溃自动拉起）"
        else:
            text = "已安装"
        self._status.setText(text)
        self._install_btn.setVisible(bool(st["stale"]))
        self._uninstall_btn.setVisible(True)

    # ---------- 动作 ----------

    def _install(self) -> None:
        ok, hint = autostart.install()
        if not ok:
            self._slot.error(f"安装自启失败：{hint}")
            return
        if hint == "reload":
            self._slot.info(
                "运行环境已更新，正在重载自启配置，BarKit 将随即重启。"
            )
        else:
            log.info("开机自启已安装")
        self.refresh()

    def _uninstall(self) -> None:
        """关闭自启：先要一个确认位，再谈执行（破坏性动作的两段式）。

        拿不到槽位（设置页没开着）时**拒绝执行**：这条路径上没有可见反馈，
        用户看到的是「点了没反应」，代价是关不掉自启 —— 好过无确认地摘掉它。
        """
        fb = self._slot.bar
        if fb is None:
            log.warning(
                "关闭开机自启：当前没有可用的行内确认位（设置页未打开），已忽略"
            )
            return
        fb.ask(
            "关闭后 BarKit 不会在登录时自动启动。确定关闭？",
            "关闭",
            self._do_uninstall,
            # 一般确认：关掉自启没有任何破坏性，随时能再打开（与「删除 / 卸载」那类
            # 不可逆动作分得开）。
            confirm_kind="primary",
        )

    def _do_uninstall(self) -> None:
        """确认之后真正执行的半段（由槽位的确认按钮回调进来）。"""
        ok, hint = autostart.uninstall()
        if not ok:
            self._slot.error(f"关闭自启失败：{hint}")
            self.refresh()
            return
        if hint == "dying":
            # detached bootout 会在 1s 后终止本进程，先给出明确提示
            self._slot.info(
                "开机自启已关闭，BarKit 将随即退出（下次可按运行方式手动启动）。"
            )
        self.refresh()
