"""capabilities.helper_ops：组件的「装置运维」能力 mixin。

装**同一个** root helper 的组件（``route_guard`` / ``ipad_tunnel`` / ``hosts_guard``）
在设置页上要的三件事字面就是同一件：让守护立刻重跑一轮、安装 / 重装、卸载。
此前三处各写一遍（网络切换与 iPad 隧道各 8 个同名方法、hosts 托管另立一套），
差别只在四句领域文案上 —— 而 hosts 托管那一份写着写着就漂了（没有卸载键、
警示另立 ``daemon_state()``），正是「同一件事两个真源必然漂」的样本。

本能力把那三件事收成**一份实现**，组件只回答「我这一句说什么」：

- :meth:`helper_notice`        —— 装置运维卡那块警示：``(文案, 是否出「重装守护」)``
- :meth:`on_helper_change`     —— 装 / 卸 / 敲门之后的**组件后置动作**（刷状态 ＋ 广播）
- :meth:`helper_ops_texts`     —— 那几句话（默认值见 :class:`HelperOpsTexts`）

**为什么后置动作归组件、不归框架代劳**：安装之后要跑 ``refresh_daemon_status()``
/ ``publish_snapshot()`` 这类**组件自己的**动作，三家刷新面各不相同（网络切换刷三台
状态机、iPad 隧道刷一台、hosts 托管还要重算条目），框架不知道要刷什么。

**反馈落「页尾区」那条槽**：装置运维卡由框架画在组件设置页的**页尾框架区**
（卡序契约的第 ③ 张），所以它的结果也落在那一区的反馈槽 —— 由
:meth:`attach_helper_ops` 注入。**不**走各组件自己那条槽（那条在页首，页尾点的按钮
在长页里够不着它，正是「一区一条」要防的「点了没反应」）。

**本能力不认识任何插件**：它只被插件继承，动作与文案都由子类给（与
``AutomationCapability`` 同一条边界）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from barkit.capabilities import helper_client as daemon
from barkit.core.plugin import Plugin

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HelperOpsTexts:
    """装置运维卡里的几句话。**只有这几句是各组件自己的**，动作与接线都在能力里。

    字段默认值是「最一般的那句」；子类覆写 :meth:`HelperOpsCapability.helper_ops_texts`
    时**给出全部字段**（读一眼就知道哪句没写），而不是逐个覆写方法。
    """

    #: 装完之后在设置页说的一句
    installed: str = "系统守护已安装。"
    #: 卸载之后
    uninstalled: str = "系统守护已卸载。"
    #: 卸载的二次确认问句（各组件领域后果不同）
    question: str = "将停止系统守护并删除本机守护文件与已有规则。确定卸载？"
    #: 卡里那条小字说明（空串 = 不画）
    note: str = ""
    #: 「立即检查」之后说的一句（空串 = 不说，只记日志）
    check: str = ""


class HelperOpsCapability(Plugin):
    """装置运维能力基类。

    子类**必须**实现 :meth:`helper_notice` 与 :meth:`on_helper_change`；其余按需覆写。
    """

    # ---------- 协作式初始化（与 AutomationCapability 同款） ----------

    def __init__(self, ctx) -> None:
        # 协作式：把初始化链交给下一个基类（通常是 Plugin）
        super().__init__(ctx)
        # 页尾区注入的两件：那条反馈槽与警示重算回调。
        # 页面（卡）销毁时由 _forget_* 置回 None —— 不清就会戳已删的 C++ 对象。
        self._helper_feedback = None
        self._helper_notice_refresh = None

    # ---------- 抽象钩子（子类必须实现） ----------

    def helper_notice(self) -> "tuple[str, bool]":
        """装置运维卡那块警示的内容：``(文案, 是否要出「重装守护」键)``。

        文案为空串 = **没有警示**（整块隐藏）。「未安装」「版本过期」各自写自己的话，
        都由这一个口给 —— 避免出现「文案说过期、按钮却说重装」这类两个真源打架。

        「未安装」那种局面一般**不给**重装键：那时用户该点的是旁边恒有的「安装守护」。
        """
        raise NotImplementedError(f"{type(self).__name__} 必须实现 helper_notice()")

    def on_helper_change(self) -> None:
        """装 / 卸 / 敲门之后的**组件后置动作**：刷守护状态并广播。

        为什么归子类：要刷的东西是组件自己的（状态缓存 / 状态机报告 / 派生文件），
        框架既不知道也不该知道。
        """
        raise NotImplementedError(f"{type(self).__name__} 必须实现 on_helper_change()")

    # ---------- 文案（子类按需覆写） ----------

    def helper_ops_texts(self) -> HelperOpsTexts:
        """本组件装置运维的那几句话。默认即 :class:`HelperOpsTexts` 的一般说法。"""
        return HelperOpsTexts()

    # ---------- 动作（统一实现；组件语义不同可覆写） ----------

    def immediate_check(self) -> None:
        """「立即检查」：让守护**现在**按本组件已声明的意图跑一轮，然后由组件刷状态。

        走的是 ``resync`` 命令（会话里的「现在跑一轮」），不再是旧的 ``touch .trigger``
        敲门。语义一步没变：**不重发配置**，只是让对面把已有那份期望态再收敛一次 ——
        所以组件不必先写一遍自己的派生文件（那是各组件 ``sync`` 的事，UI 路径各自覆写）。

        **「已让守护跑一轮」只在真发出去时才说**（2026-10-04 修）：``run_op`` 未连接
        时会**就地**回调失败（``_on_check_done`` 已写过一句 error），紧接着再写一句
        info 就等于把失败盖掉 —— 反馈槽只有一条，后写的那句赢。守护没装 / 没连时点
        这个键，改前界面念的正是那句成功。
        """
        subject = self.intent_subject()
        if not subject:
            log.warning("%s 没有声明意图 subject，「立即检查」无事可做", type(self).__name__)
            self._helper_say("error", "「立即检查」无事可做：本组件没有声明意图 subject")
            return
        sent = self.run_op("resync", {"subject": subject}, callback=self._on_check_done)
        self.on_helper_change()
        if not sent:
            return
        text = self.helper_ops_texts().check
        if text:
            self._helper_say("info", text)

    def _on_check_done(self, ok: bool, _result, error: str) -> None:
        """``resync`` 的回执：只把失败说出来（成功早就说了「已让守护跑一轮」）。"""
        if not ok:
            self._helper_say("error", f"立即检查失败：{error or '守护未响应'}")

    def install_action(self) -> bool:
        """安装 / 重装守护：一次系统授权，装完刷状态并让警示退场。**返回是否装成**。

        失败语义不降级：A 类二进制解析不到源、或与发行态基线不符时
        ``daemon.install`` 直接拒装（它自己已预检并记了日志），这里只说给用户听。

        返回值只有一个消费方：装置运维卡上那枚「安装守护 / 重装守护」按钮 —— 装成之后
        它把自己标成「已安装」并失活（用户 2026-09-30 要求）。**判据是安装真的成了**，
        不是「按钮被点过」（那就成了「点了没成也报已安装」）。
        """
        sources, missing = self.root_binary_sources()
        if not daemon.install(sources, missing):
            self._helper_say("error", "安装失败：系统授权流程未完成，守护未安装。")
            return False
        self.on_helper_change()
        # 设置页那块警示是**建页那一刻**判定的：不给这一次刷新，用户装完仍看着红字与
        # 「重装守护」，与旁边刚写下的「已安装」互相矛盾，只能反复点（2026-09-18 真机实测）。
        self._refresh_helper_notice()
        self._helper_say("info", self.helper_ops_texts().installed)
        return True

    def uninstall_action(self) -> None:
        """卸载守护：**行内**二次确认之后才真拆。

        确认走**页尾区**那条反馈槽（口径「一区一条」：触发按钮在哪一区，问句就落哪一区）。
        **拿不到槽位就拒绝执行** —— 破坏性动作绝不允许在「没有确认位」的情况下静默跑掉。
        """
        fb = self._helper_feedback
        if fb is None:
            log.warning("卸载守护：当前没有可用的行内确认位（设置页未打开），已忽略")
            return
        fb.ask(self.helper_ops_texts().question, "卸载", self._do_uninstall,
               confirm_kind="danger")

    def _do_uninstall(self) -> None:
        """确认之后真拆：拆完刷状态 ＋ 退警示 ＋ 说一句。

        ⚠️ ``daemon.uninstall()`` **只拆框架自己的东西**（plist / 启动器 / A 类二进制），
        不替任何插件撤系统状态 —— 各组件那些状态（静态路由 / 隧道 / /etc/hosts 托管块）
        由各自的 ``teardown()`` 在**卸载组件**时撤。本动作是「只拆守护、组件留着」，
        故**不动**那些状态（既有的三份文案也照旧，见任务板 §5 的挂账）。
        """
        if not daemon.uninstall():
            self._helper_say("error", "卸载失败：系统授权流程未完成，守护未卸载。")
            return
        self.on_helper_change()
        self._refresh_helper_notice()
        self._helper_say("info", self.helper_ops_texts().uninstalled)

    # ---------- 设置页接线（页尾框架区调） ----------

    def attach_helper_ops(self, widget, feedback, refresh) -> None:
        """设置页把装置运维卡、**页尾区那条反馈槽**与警示重算回调交给插件。

        ``widget`` 只用来挂 ``destroyed``（卡销毁后摘掉刷新回调，免得戳已删的 C++ 对象）；
        ``feedback`` 同款 —— 两者都只在「当前引用的正是刚死掉的那个」时清空。
        """
        widget.destroyed.connect(
            lambda *_args, _fn=refresh: self._forget_helper_notice(_fn)
        )
        feedback.destroyed.connect(
            lambda *_args, _fb=feedback: self._forget_helper_feedback(_fb)
        )
        self._helper_feedback = feedback
        self._helper_notice_refresh = refresh

    def _forget_helper_notice(self, fn) -> None:
        """只在「当前引用的正是刚死掉的那个」时清空（重建时新回调可能已先赋好值）。"""
        if self._helper_notice_refresh is fn:
            self._helper_notice_refresh = None

    def _forget_helper_feedback(self, fb) -> None:
        """同上：页面重建时新槽位可能已先赋好值，别无脑清。"""
        if self._helper_feedback is fb:
            self._helper_feedback = None

    def refresh_helper_notice(self) -> None:
        """守护连接 / 版本变化时，**先重拉状态、再重算**卡上警示（公开入口）。

        供装配层在守护状态变化时对整个插件共振一次（见
        ``SettingsWindow.refresh_helper_notices``）。**必须先 ``on_helper_change()``
        把状态刷成本刻的** —— 组件各自的刷新面不同（网络切换重声明期望态、iPad 隧道
        重取事实、hosts 托管重发配置），故这一步归子类；顺序反了就是拿旧缓存重算、
        红字退不掉（2026-09-30 复评：这正是上一版只调 ``_refresh_helper_notice()``
        的错处）。
        """
        self.on_helper_change()
        self._refresh_helper_notice()

    def _refresh_helper_notice(self) -> None:
        """让设置页那块警示按最新守护状态重算（安装 / 卸载 / 守护状态变化后调）。"""
        fn = self._helper_notice_refresh
        if fn is None:
            return
        try:
            fn()
        except RuntimeError:      # 底层 C++ 对象已销毁（页面刚关掉）
            self._helper_notice_refresh = None

    def _helper_say(self, kind: str, text: str) -> None:
        """把一句结果写进**装置运维卡所在那一区**的反馈槽（没有那一页时只记日志）。

        ``kind`` 取 ``info`` / ``error``。**照记日志** —— 没界面时（面板路径进来、
        或设置窗没开）日志是唯一线索。
        """
        if kind == "error":
            log.error("%s", text)
        else:
            log.info("%s", text)
        fb = self._helper_feedback
        if fb is None:
            return
        if kind == "error":
            fb.error(text)
        else:
            fb.info(text)
