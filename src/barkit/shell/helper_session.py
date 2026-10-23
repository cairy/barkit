"""shell/helper_session：把 app 侧那条会话接到 Qt 事件循环上（装配层持有）。

它补的是 :class:`barkit.capabilities.helper_link.HelperLink` **故意留白的那一段**：
``helper_link`` 一行 Qt 都不 import，读写全走 ``fileno()`` + ``pump()``，为的是能被
离屏自证；「谁来调 ``pump()``、断开之后谁来重连」是**进程装配**的事，落在这一层。

三件事
------
1. **可读即 pump**：``QSocketNotifier(fd, Read)`` 挂在会话套接字上，数据到达即读帧。
   **这是「空闲零唤醒」在 app 侧的落点** —— 连接在时一只定时器都不跑，唤醒由内核事件
   驱动（旧形态是每条被监视路径一个内核句柄 ＋ 一个「待出现」1 秒自检）。
   **写的那一半对称**：``QSocketNotifier(fd, Write)`` **常建、默认禁用**，只在 ``link``
   报「出站队列里有发不完的字节」时开（``link.on_writable``）。套接字几乎永远可写，
   一直开着就等于内核事件持续叫醒主线程 —— 那正是这一层要避免的。
2. **断开即重连**：只在 ``STATE_OFF`` 期间跑一只 1 秒的重连定时器，连上即停。守护由
   launchd 常驻、但可能正在重装 / 崩溃重启，这段窗口必须自己补上。
3. **观测转总线**：守护推来的事实转成 ``system.helper.observed`` 事件，分发规则与
   从前的文件事件逐字相同（插件经 ``subjects()`` 声明，``PluginManager`` 按声明过滤）。

第四件（顺带，但少了它会出错）是**把与 Qt 有关的那几只回调送回主线程**：``declare`` /
``run_op`` 会被框架托管的**长驻服务线程**调用（route_guard 的探活就在那条线程上声明
意图），而 ``on_state`` / ``on_pending`` / 回执回调里要挂摘 ``QSocketNotifier``、起停
``QTimer``、碰面板 —— 那些只在主线程合法。投递用一条 ``Signal``（Qt 对跨线程信号自动
排队，同线程直调，主线程调用的时序因此一个字都不变）。``HelperLink`` 自己不知道 Qt，
它只认 ``on_post`` 这个注入口。

为什么不在 ``capabilities`` 里做
--------------------------------
``capabilities`` 是 L2 且**只许标准库**（门禁 ``test_capabilities_lazy``：整棵目录
零第三方 import，PySide6 也算）。装配是 L3 ``shell`` 的职责，它本来就依赖 Qt 与下层。

「按需」的到点检查
------------------
``op.run`` 的回执到点判失败（``HelperLink._expire_pending``）本来挂在 ``pump()`` 上，
而「守护把命令吞了、一个字节都不回」恰恰是**不会有下一次 pump** 的那种情形。故在这里
按 ``HelperLink.on_pending`` 报来的最早截止时刻挂一只**单发**定时器：没有在等的命令
就一只都不跑，有则精确排在那一刻。
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import QObject, QSocketNotifier, QTimer, Signal

from barkit import paths
from barkit.capabilities import helper_client
from barkit.capabilities.helper_link import (
    STATE_OFF,
    STATE_READY,
    HelperLink,
)
from barkit.core.events import HELPER_OBSERVED_TOPIC, ObservedPush

log = logging.getLogger(__name__)

#: 断开期间的重连节拍（毫秒）。与 ``helper_link.RETRY_INTERVAL_S`` 同源。
RETRY_MS = 1000


class HelperSession(QObject):
    """与 root helper 那条会话在 app 侧的**唯一持有者**。

    生命周期：``__init__`` 建 link（此刻还没连）；装配层把 ``link`` 交给
    ``PluginContext.helper``，此后 ``start()`` 开始连。``stop()`` 关连接 —— 关掉就是
    **对端立刻收到 EOF**，这正是「app 死了、依附型意图（隧道）立刻撤掉」的实现。
    """

    #: 跨线程投递一条要执行的调用（长驻服务线程 → 主线程）。Qt 对「发者与收者不同线程」
    #: 的信号自动排队，同线程则直调，故主线程调用的时序一个字都不变。见 ``link.on_post``。
    _posted = Signal(object)
    #: 会话状态变化（``STATE_OFF`` / ``STATE_READY`` …）。设置页据此重算「装置运维」警示，
    #: 让重装后红字及时退场；版本闸门也据此清/填 ``live_version``。
    stateChanged = Signal(str)

    def __init__(self, events, parent=None, socket_path=None) -> None:
        super().__init__(parent)
        self._events = events
        self._path = str(socket_path if socket_path is not None else paths.HELPER_SOCKET)
        self.link = HelperLink(client="app")
        self.link.on_push = self._on_push
        self.link.on_state = self._on_state
        self.link.on_pending = self._on_pending
        self.link.on_writable = self._on_writable
        self.link.on_handshake = self._on_handshake
        # 会话的收发只许在**主线程**做（套接字与状态机都在这里），而 ``declare`` 会被
        # 长驻服务线程调用（route_guard 的探活就在那条线程上声明意图）⇒ 把与 Qt 有关
        # 的那几只回调投递回主线程。``emit`` 本身就是线程安全的。
        self._posted.connect(self._run_posted)
        self.link.on_post = self._posted.emit
        self._notifier: QSocketNotifier | None = None
        self._w_notifier: QSocketNotifier | None = None
        self._notifier_fd = -1
        #: 断开期间的重连（只在没连上时跑，连上即停）
        self._retry = QTimer(self)
        self._retry.setInterval(RETRY_MS)
        self._retry.timeout.connect(self._attempt)
        #: 命令回执的到点检查（单发，只在有命令在等时排一只）
        self._op_timer = QTimer(self)
        self._op_timer.setSingleShot(True)
        self._op_timer.timeout.connect(self.link.expire_pending)
        #: 握手期限的到点检查（单发，只在等 welcome 的那几秒里排一只）
        self._hs_timer = QTimer(self)
        self._hs_timer.setSingleShot(True)
        self._hs_timer.timeout.connect(self.link.expire_handshake)
        self._started = False

    # ---------- 生命周期 ----------

    def start(self) -> None:
        """开始连（幂等）。连不上不是错误 —— 重连定时器会一直试。"""
        if self._started:
            return
        self._started = True
        self._attempt()

    def stop(self) -> None:
        """关掉会话（退出时调用，幂等）。**对端立刻收到 EOF**，依附型意图随之撤回。"""
        self._started = False
        self._retry.stop()
        self._op_timer.stop()
        self._hs_timer.stop()
        self._detach_notifier()
        self.link.close()

    @property
    def connected(self) -> bool:
        return self.link.state == STATE_READY

    # ---------- 连接 ----------

    def _attempt(self) -> None:
        """试连一次（初始 + 重连定时器共用）。

        ``_started`` 是总闸：**``stop()`` 之后任何路径都不许再把重连拉起来**。漏了它
        就是一条回路：``stop()`` → ``link.close()`` → ``on_state(OFF)`` → 重新
        ``_retry.start()`` —— 「关掉会话」实际只关掉了一瞬间，随后继续每秒试连。
        （今天只在退出路径调用，被「事件循环马上结束」掩盖；任何非退出调用点都会
        显形。）
        """
        if not self._started:
            return
        if self.link.state == STATE_OFF:
            self.link.connect(self._path)
            self._sync_notifier()
        if self.link.state == STATE_OFF:
            if not self._retry.isActive():
                self._retry.start()
        else:
            self._retry.stop()

    def _on_state(self, state: str) -> None:
        # 注意顺序：``link`` 关套接字在改状态**之前**（``_teardown``），所以这里同步
        # notifier 时 ``fileno()`` 已经是 -1 ⇒ 会走「摘掉」那一支。
        self._sync_notifier()
        # 版本闸门改问本人：握手成功那一刻守护自陈了它的版本，交给版本闸门口收着
        # （断开即清空，于是「没连上」自动退回读盘上那份）。
        helper_client.set_live_version(
            self.link.helper_version if state == STATE_READY else "")
        self.stateChanged.emit(state)
        if state == STATE_OFF:
            # 同 ``_attempt``：``stop()`` 已经把 ``_started`` 拨掉了，这里的 OFF 是
            # **自己关的**，不是「对端没了」⇒ 不许再排重连。
            if self._started and not self._retry.isActive():
                self._retry.start()
        else:
            self._retry.stop()

    # ---------- 套接字可读 ----------

    def _sync_notifier(self) -> None:
        """让两只 notifier 的 fd 跟着 ``link`` 走（-1 ＝ 摘掉）。"""
        fd = self.link.fileno()
        if fd < 0:
            self._detach_notifier()
            return
        if self._notifier is not None and self._notifier_fd == fd:
            return
        self._detach_notifier()
        notifier = QSocketNotifier(fd, QSocketNotifier.Type.Read, self)
        notifier.activated.connect(self._readable)
        self._notifier = notifier
        self._notifier_fd = fd
        # 写方向**常建、默认禁用**。套接字绝大多数时候都是可写的，一直开着就等于让
        # 内核事件持续叫醒主线程（与「空闲零唤醒」正好相反）。只有出站队列里真有发不完
        # 的字节时才开（``link.on_writable`` 报来）。
        writable = QSocketNotifier(fd, QSocketNotifier.Type.Write, self)
        writable.activated.connect(self._writable)
        writable.setEnabled(False)
        self._w_notifier = writable

    def _detach_notifier(self) -> None:
        self._notifier = self._drop_notifier(self._notifier)
        self._w_notifier = self._drop_notifier(self._w_notifier)
        self._notifier_fd = -1

    @staticmethod
    def _drop_notifier(notifier):
        if notifier is None:
            return None
        try:
            notifier.setEnabled(False)
            notifier.activated.disconnect()
        except Exception:  # noqa: BLE001 —— PySide6 对「无连接可断」的报错不一
            pass
        notifier.deleteLater()
        return None

    def _readable(self, *_args) -> None:
        self.link.pump()

    def _writable(self, *_args) -> None:
        """套接字可写：把出站队列里积压的字节再推一次（推完它会自己关掉本 notifier）。"""
        self.link.flush_out()

    def _on_writable(self, need: bool) -> None:
        """出站队列里有没有发不完的字节 —— 据此开／关写方向的 notifier。

        由 ``link.on_writable`` 调；重复调用会被 ``HelperLink`` 挡在变化那一层，
        这里不做去重（去重是那一边的职责，两边都做反而说不清谁说了算）。
        """
        writable = self._w_notifier
        if writable is None:
            return
        writable.setEnabled(bool(need))

    # ---------- 回调（由 link 在收帧时调） ----------

    def _on_push(self, subject: str, observed) -> None:
        """守护推来一份事实 → 转成总线事件（分发规则由 ``PluginManager`` 按声明过滤）。"""
        self._events.publish(HELPER_OBSERVED_TOPIC,
                             ObservedPush(subject=str(subject), observed=observed))

    def _on_pending(self, deadline) -> None:
        """在等的命令变了：按最早的那个截止时刻排一只单发定时器。"""
        self._arm(self._op_timer, deadline)

    def _on_handshake(self, deadline) -> None:
        """等 welcome 的期限变了（``None`` ＝ 不用再等了）。

        没有它，「守护 accept 了却不回帧」会让 app 永久停在 handshake：既没有 EOF
        （没数据 ⇒ ``pump()`` 不醒）也不重连，界面上就是「一直没连上」。
        """
        self._arm(self._hs_timer, deadline)

    @staticmethod
    def _arm(timer, deadline) -> None:
        """按截止时刻排一只单发定时器（``None`` ＝ 撤掉）。"""
        if deadline is None:
            timer.stop()
            return
        delay_ms = int(max(0.0, float(deadline) - time.monotonic()) * 1000) + 1
        timer.start(delay_ms)

    def _run_posted(self, fn) -> None:
        """执行一条从别的线程投递过来的调用（回到主线程了）。"""
        try:
            fn()
        except Exception:  # noqa: BLE001 —— 投递过来的活炸了不能拖垮事件循环
            log.exception("会话投递的任务异常")


__all__ = ["HelperSession", "RETRY_MS"]
