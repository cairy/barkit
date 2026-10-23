"""capabilities.inline_feedback：组件的「设置页行内反馈」能力 mixin。

四个组件（``route_guard`` / ``ipad_tunnel`` / ``recognize`` / ``hosts_guard``）在各自的
设置页上都要把一句话说给用户，而一个设置页只有**一条**页首反馈槽
（``ui_kit.FeedbackBar``，零模态弹框纪律的落点）。此前四处各写一遍，方法名逐字相同
（``attach_feedback`` / ``_forget_feedback`` / ``_say_info`` / ``_say_error``，
``route_guard`` 另多一条 ``_say_hint``），差别只在日志归属 —— 正是
「同一件事多处各写一遍」的样本（与 ``HelperOpsCapability`` 的诞生同型）。

本能力把那几件收成**一份实现**，组件继承即得：

- :meth:`attach_feedback` —— 设置页把页首那条槽交给组件（唯一入口，注销归本能力管）
- ``_say_info`` / ``_say_hint`` / ``_say_error`` —— 三档出口（成功 / 中性告知 / 失败）；
  方法名带前导下划线，与迁移前逐字相同 ⇒ 各组件 ``self._say_*(...)`` 调用点零改动

**边界（单一关注点）**：只拥有「页首那条槽的接线与三档出口」。
**不**决定该说什么（那是组件的领域文案）；**不**接管页尾那条槽 ——
装置运维的反馈归 ``HelperOpsCapability``（「一区一条」：触发按钮在哪一区，
问句与回执就落哪一区），两者互补，同一个组件可以都继承。
**不**含任何 UI 类型：槽对象由设置页注入，本能力只调它的 ``info`` / ``hint`` / ``error``。

**两档出口的分流不在本能力里**：``route_guard`` 有两条出口（有槽位就地落、没有槽位
返回 ``ActionResult`` 上面板那条结果槽），那是该组件自己的领域决定（面板路径的调用
本来就拿不到设置页的槽），留在 ``plugins/route_guard``。

组合：写成协作式 mixin（``__init__`` 调 ``super().__init__``），以便插件用
``class Plugin(..., InlineFeedbackCapability, HelperOpsCapability)`` 多重继承而 MRO 干净。
"""

from __future__ import annotations

import logging

from barkit.core.plugin import Plugin

log = logging.getLogger(__name__)


class InlineFeedbackCapability(Plugin):
    """设置页行内反馈能力基类。

    没有必须实现的抽象钩子 —— 继承即得那条槽与三档出口；要说什么由子类自己决定。
    """

    # ---------- 协作式初始化（与另三个能力同款） ----------

    def __init__(self, ctx) -> None:
        # 协作式：把初始化链交给下一个基类（通常是 Plugin）
        super().__init__(ctx)
        # 设置页注入的那条槽。页面销毁时由 _forget_feedback 置回 None ——
        # 不清就会戳已删的 C++ 对象。
        self._feedback = None

    # ---------- 接线（设置页调） ----------

    def attach_feedback(self, feedback) -> None:
        """设置页把页首那条行内反馈槽交给组件（**唯一**反馈出口；组件不开模态框）。

        槽位的生死归这里管：设置面被销毁（关窗 / 页面重建）时摘掉引用，免得
        ``_say_*`` 戳到已删的 C++ 对象。
        """
        feedback.destroyed.connect(
            lambda *_args, _fb=feedback: self._forget_feedback(_fb)
        )
        self._feedback = feedback

    def _forget_feedback(self, feedback) -> None:
        """槽位所属页面被销毁时摘掉引用，避免戳已删的 C++ 对象。

        只在「当前引用的正是刚死掉的那个」时清空：重建时新页的槽位可能**已经先赋好值**，
        无条件清空会把新槽位一起丢掉。
        """
        if self._feedback is feedback:
            self._feedback = None

    # ---------- 出口（组件调） ----------

    def _say_info(self, text: str) -> None:
        """把一句**结果**写进那条槽；没有槽位（面板 / 托盘路径）时只记日志。"""
        log.info("%s", text)
        if self._feedback is not None:
            self._feedback.info(text)

    def _say_hint(self, text: str) -> None:
        """把一句**中性告知**写进同一槽（不是「做成了」，只是要你知道一件事）。

        「守护尚未安装，暂停状态已记录」这类话归它：用户要照着做点什么，
        但此刻并**没有**一件事刚做成 —— 用成功档（绿）会把「还没做成」说成「成了」。
        """
        log.info("%s", text)
        if self._feedback is not None:
            self._feedback.hint(text)

    def _say_error(self, text: str) -> None:
        """同 :meth:`_say_info`，红色。**照记 ERROR 日志** —— 没界面时日志是唯一线索。"""
        log.error("%s", text)
        if self._feedback is not None:
            self._feedback.error(text)
