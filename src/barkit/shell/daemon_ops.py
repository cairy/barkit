"""shell/daemon_ops：装置运维段（立即检查 / 安装守护 / 卸载守护 / 过期重装提示）。

**为什么抽到 shell 层**：``route_guard`` / ``ipad_tunnel`` / ``hosts_guard`` 装的是
**同一个 root helper**（``capabilities/helper_client``：一次授权、同一套 plist 与 A 类
二进制、同一份版本对账）。于是「装 / 卸 / 让守护立刻重跑一轮」这三件事在三个组件的
设置页上**字面上就是同一件事**，此前却在各自的 ``settings.py`` 里各写一遍按钮与红字。
差别只有两处：那句警示的**文案**（各自领域的后果不同）与「要不要出重装键」。

本模块只画界面、不认识任何插件：动作与状态都由调用方以回调注入（与 §7.3「运行期 UI
的数据由插件方法注入」同一条边界）。因此它放 ``shell/`` 不违反「shell 不得感知插件」。

**零模态框**（同 ``ui_kit.FeedbackBar`` 的纪律）：本块**不弹任何框**——「安装成功 / 卸载
失败」这类**结果**由各插件自己的行内槽说（``Plugin._say_*``），二次确认（卸载）也归那条槽。
本块只把按钮接到回调上，外加上自己那两枚「装」按钮的**状态**（装成 ⇒ 标「已安装」并失活，
见 ``build_daemon_ops`` 的 ``install`` 那一项）—— 状态与结果各说各的，不是两处说同一句话。

**为什么「过期提示」要能重算**：那块红字是**建页那一刻**判定的，而安装成功后插件只更新
了自己的缓存 —— 不给一次刷新，用户装完仍看着「守护版本过期」与「重装守护」，与旁边刚
写下的「已安装」互相矛盾，只能反复点（2026-09-18 真机实测）。故本函数把重算回调交给
调用方，由它在安装 / 卸载成功后喊一声。
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from barkit.contribution.specs import IconName
from barkit.shell.ui_kit import apply_text_role, tool_button
from barkit.shell.ui_kit.buttons import _GlyphLabelButton, _decorate_button


def build_daemon_ops(
    parent: QWidget,
    *,
    install: Callable[[], bool],
    uninstall: Callable[[], None],
    status: Callable[[], "tuple[str, bool]"],
    immediate_check: "Callable[[], None] | None" = None,
) -> "tuple[QWidget, Callable[[], None]]":
    """构建装置运维段，返回 ``(整块 widget, 重算回调)``。

    - ``status()`` → ``(警示文案, 是否需要「重装守护」键)``。文案为空串 = **没有警示**
      （警示区整块隐藏）——「未安装」「版本过期」各自写自己的话，都由这一个回调给，
      避免出现「文案说过期、按钮却说重装」这类两个真源打架。
    - ``install()`` **返回装没装成**（``bool``）：装成之后那两枚「装」按钮自己变成
      「已安装」并失活 —— 只靠下面那条行内提示说结果时，用户点完仍看见一模一样的
      「安装守护」，会怀疑刚才那下没生效（用户 2026-09-30 要求）。
      **判据是装成了，不是被点过**；且装完仍被闸门喊「重装」时不标已安装（那时跑的
      守护可能还是旧的，按钮得留着能再点）。
    - ``immediate_check`` 省略即不画「立即检查」（不是每个组件都有「重跑一轮」的语义）。
    - 返回的**重算回调**：调用方应在安装 / 卸载成功后调一次（见模块 docstring）。

    形态沿 ``route_guard`` 设置页既有的那两段：按钮**左对齐、不通栏**（通栏那枚看起来
    像个输入框，用户会以为是可填的一格）。

    **三枚排一行**（2026-09-22 改）：此前「立即检查」独占一行、「安装守护 / 卸载守护」
    在下一行，白吃约一行高度（实测三枚各 82px、合计 262px，而卡片内容区宽 412px ——
    一行放得下）。它们是同一件事的三个动作，分两行没有任何语义。
    """
    block = QWidget(parent)
    lay = QVBoxLayout(block)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(10)

    row = QHBoxLayout()
    row.setSpacing(8)
    if immediate_check is not None:
        chk = tool_button("立即检查", "pill-ghost", parent=block)
        chk.clicked.connect(immediate_check)
        row.addWidget(chk)
    inst = _GlyphLabelButton("安装守护", parent=block)
    _decorate_button(inst, "pill-ghost")
    row.addWidget(inst)
    uninst = tool_button("卸载守护", "pill-ghost", parent=block)
    row.addWidget(uninst)
    row.addStretch(1)
    lay.addLayout(row)

    # 警示区（未安装 / 版本过期）：整块包进一个容器，好让状态变好时**整块退场**。
    notice = QWidget(block)
    nlay = QVBoxLayout(notice)
    nlay.setContentsMargins(0, 0, 0, 0)
    nlay.setSpacing(6)
    warn = QLabel("", notice)
    # 走**角色表**（`caption_warn` ⇒ `red_text` 11.5px）。原先这里是
    # `setObjectName("err-note")`，而全局 QSS 里**从来没有这条规则** —— 模块注释写
    # 「警示区」、调用方注释写「红字」，实现里丢了：实机上这行是普通正文色，用户看到的
    # 「守护版本过期」与旁边的说明文字一模一样（2026-09-21 排查确认为死 objectName）。
    # 门禁 `test_label_objectnames_are_styled_or_none` 守这一类（QSS 里没有的 id 不许设）。
    apply_text_role(warn, "caption_warn")
    warn.setWordWrap(True)
    nlay.addWidget(warn)
    reinstall = _GlyphLabelButton("重装守护", text_role="on_solid", parent=notice)
    _decorate_button(reinstall, "primary")
    rrow = QHBoxLayout()
    rrow.addWidget(reinstall)
    rrow.addStretch(1)
    nlay.addLayout(rrow)
    lay.addWidget(notice)
    # 重装键只在「装了但版本过期」时出现：没装时用户该点的是上面那枚「安装守护」，
    # 两个键同时出现会让人以为要先重装才能装。
    reinstall.setVisible(False)

    def _mark_installed() -> None:
        """把两枚「装」按钮标成**已安装**并失活：盘上已是新的，再点它没有意义。

        这是「动作的反馈长在动作自己身上」那一半 —— 行内提示条说结果，按钮说状态；
        只靠前者时，用户点完仍看见一枚一模一样的「安装守护」，会怀疑刚才那下没生效。
        """
        for btn in (inst, reinstall):
            btn.setText("已安装")
            btn.set_glyph(IconName.MARK_OK, "green_text")
            btn.setEnabled(False)

    def _clear_installed_mark() -> None:
        """还原成可点：卸载那条路上调（见下），也用于「再点一次装」。"""
        inst.setText("安装守护")
        reinstall.setText("重装守护")
        for btn in (inst, reinstall):
            btn.clear_glyph()
            btn.setEnabled(True)

    def _install_clicked() -> None:
        if not install():
            return
        # 装完**再看一眼闸门**：如果它不再喊「重装」，说明这一步真到位了 ⇒ 按钮自报结果。
        # 仍在喊（新装的守护还没被拉起、跑着的仍是旧代码）就别把按钮锁死 —— 用户还得
        # 能再点一次（这正是「装了没重启」那一档，闸门说的话是对的）。
        _text, needs_reinstall = status()
        if not needs_reinstall:
            _mark_installed()

    def _uninstall_clicked() -> None:
        # 用户要走「卸载」这条路 ⇒ 先把「装」那两枚还原。确认是可以取消的，取消之后
        # 按钮显示也只是回到「还没装／可以装」，没有任何副作用。
        _clear_installed_mark()
        uninstall()

    def refresh() -> None:
        text, needs_reinstall = status()
        warn.setText(text)
        notice.setVisible(bool(text))
        reinstall.setVisible(bool(text) and bool(needs_reinstall))

    inst.clicked.connect(_install_clicked)
    uninst.clicked.connect(_uninstall_clicked)
    reinstall.clicked.connect(_install_clicked)
    refresh()
    return block, refresh
