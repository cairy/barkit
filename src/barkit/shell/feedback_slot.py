"""反馈槽的持有者：把「记日志 ＋ 按成败选档 ＋ 槽位销毁后置空」收成一份实现。

**为什么要有它**（2026-10-16）：``shell/`` 与 ``plugins/`` 下曾有八条行内槽，每条都配
三样逐字相同的代码 —— 一个 ``_xxx_feedback`` 属性、一个 ``_say_xxx`` 方法（记日志 ＋
``bar.error(...) if error else bar.info(...)``）、一个 ``_forget_xxx_feedback``
（页面销毁时置空，防戳已删的 C++ 对象）。``maintenance.py`` 一个文件里就有四份。
这些 ``_say_*`` 正是「同一件事多处各写一遍」的样本，也是 ``_wire_button`` 那个
``say(text, *, error=False)`` 参数各页都要写一个 lambda 适配的原因。

**它不画东西**：行内槽本身是 :class:`~barkit.shell.ui_kit.feedback.FeedbackBar`
（组件盒的件）；本类只管**持有它、转发给它、在它死了之后松手**。

**为什么记日志**：槽位随页面销毁失效，那时界面无从说起，日志是唯一线索；``error`` 档
一律进 ``ERROR``。日志与 UI 生命周期这两件事正是本类落在 ``shell/`` 而不在 ``ui_kit/``
的原因 —— 组件盒不记日志、也不管页面生灭。
"""

from __future__ import annotations

import logging

from barkit.shell.ui_kit import FeedbackBar

log = logging.getLogger(__name__)


class FeedbackSlot:
    """一条行内反馈槽的持有者。

    三句话用法::

        self._data = FeedbackSlot()      # 造持有者（页面重建时不必重建它）
        ...
        self._data.attach(feedback)      # 那条槽建出来时接上
        ...
        self._data.info("已清掉 3 项。")                     # 业务路径直接说
        self._data.error("没清干净。")                        # 失败档
        _wire_button(btn, handler, self._data.say)           # 按钮路径：直接传，不必包 lambda

    ``attach`` 接上后 ``destroyed`` 由本类接管 —— 槽位随页面销毁即自动松手，调用方
    不必再写 ``_forget_xxx_feedback``。**重建页面时新的 attach 会替换旧的**：旧槽的
    ``destroyed`` 只在「当前引用的正是它」时清空，否则会把刚接上的新槽一起丢掉
    （与迁移前各处 ``_forget_*`` 逐字同一条判据）。
    """

    def __init__(self) -> None:
        self._bar: FeedbackBar | None = None

    def attach(self, bar: FeedbackBar | None) -> None:
        """接上这一页那条槽；传 ``None`` 即摘掉。"""
        self._bar = bar
        if bar is None:
            return
        bar.destroyed.connect(lambda *_a, _b=bar: self._on_destroyed(_b))

    def _on_destroyed(self, bar: FeedbackBar) -> None:
        """槽位所属页面被销毁 ⇒ 摘掉引用（防戳已删的 C++ 对象）。"""
        if self._bar is bar:
            self._bar = None

    @property
    def bar(self) -> FeedbackBar | None:
        """当下那条槽（没接上时为 ``None``）—— 给要直接用它（如 ``ask``）的场合。"""
        return self._bar

    @property
    def attached(self) -> bool:
        """此刻有没有活着的槽（判「消息说给谁听」时用）。"""
        return self._bar is not None

    def info(self, text: str) -> None:
        """说一句「做成了」。"""
        log.info("%s", text)
        if self._bar is not None:
            self._bar.info(text)

    def hint(self, text: str) -> None:
        """说一句**中性告知**：不是「做成了」，只是要你知道一件事。"""
        log.info("%s", text)
        if self._bar is not None:
            self._bar.hint(text)

    def error(self, text: str) -> None:
        """说一句「没做成」。**照记 ERROR 日志** —— 没界面时它是唯一线索。"""
        log.error("%s", text)
        if self._bar is not None:
            self._bar.error(text)

    def say(self, text: str, *, error: bool = False) -> None:
        """按成败说一句 —— :func:`~barkit.shell.settings.helpers._wire_button` 的 ``say=``
        参数要的就是这个形状，直接传 ``slot.say`` 即可，不必再写 lambda 适配。"""
        if error:
            self.error(text)
        else:
            self.info(text)
