"""capabilities/helper_link：app 侧那条会话的**客户端**（框架持有，插件不碰）。

它在整条通道里的位置
------------------
旧的通道是「app 写期望态文件 → ``touch`` 敲门 → helper 每 1s ``exists()``」，
而 app 侧读结果靠 ``QFileSystemWatcher`` 监视目录 + 各组件兜底 ``tick()``。
这个模块把四件事一次接住：

===============  ==========================  ============================
四件事            旧的载体                     现在
===============  ==========================  ============================
意图（我要什么）  期望态 json 文件             ``declare(subject, payload)``
命令（做一次）    ``ops`` 字段 + ``seq`` 水位线  ``run_op(op, params, cb)``
事实（现在怎样）  状态 json + 目录监视          ``event.push`` → ``snapshot()``
存活（对面在吗）  没有载体（只能拿端口猜）      **连接本身**
===============  ==========================  ============================

**「被拒也有回执」在这里的落点**：意图帧不带 ``req_id``，守护拒收某个 subject 的
期望态时，app 侧从前只剩一行 warning —— 不知道**是哪一个 subject 没生效**，组件于是
以为自己声明成功了（2026-09-30 挂账）。现在拒绝帧带上被拒的 ``subject``，本模块按
:meth:`HelperLink.intent_rejection` 留一份可查状态，并经 ``on_reject`` 上抛。

**「空闲零唤醒」在这里的落点**：连接在时本模块一个定时器都不跑 —— 数据到达由
Qt 的 ``QSocketNotifier``（内核事件）通知；只有**断开**期间才由一个 1s 的重连
定时器驱动，连上即停。

**写的那一半分帧也在这一层**（2026-09-29）：读的一半由 ``FrameReader`` 拼帧，写的
一半原先假定成「一次 ``sendall`` 发完」—— 而 ``SOCK_STREAM`` 上那两半都不成立。
现在发不出去的部分落在 :class:`FrameWriter` 出站队列里，等装配层报「可写」
（``on_writable`` → 写方向的 ``QSocketNotifier``）再推。于是**「缓冲满」不再等于
「发送失败」**，断开只剩「对端真坏了」一种含义。

分层：本模块在 ``capabilities``（与 ``helper_client`` / ``helper_ops`` 同层），
因为 ``core`` 不得 import ``capabilities``（架构门禁
``test_layer_dependency_direction_core``）。``PluginContext`` 侧只把它当
``object`` 透传，不读它的任何属性 —— 与 ``notify`` / ``interaction`` 同款。

Qt 无关：这里一行 Qt 都不 import。socket 的读写全走 ``fileno()`` + ``pump()``，
由框架用 ``QSocketNotifier`` 驱动；测试里直接调 ``pump()`` 即可离屏自证。
"""

from __future__ import annotations

import functools
import logging
import os
import socket
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from barkit.capabilities.helper import protocol
from barkit.capabilities.helper.protocol import FrameError, FrameReader, FrameWriter

log = logging.getLogger(__name__)


def _locked(fn):
    """把会话的公开方法套进那把可重入锁（理由见 :attr:`HelperLink._lock`）。"""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


#: 断开期间的重连节拍（秒）。**只在没连上时跑**，连上即停。
RETRY_INTERVAL_S = 1.0

#: 单次 ``connect`` 的本地超时。Unix 域套接字是本地操作，正常是微秒级；
#: 给个上限只是防「没人 listen 却卡住主线程」。
CONNECT_TIMEOUT_S = 0.5

#: ``run_op`` 等回执的上限（秒）。到点回调收到「超时」——**这是旧形态给不了的**：
#: 从前命令排进状态文件、回执写回状态文件，用户点了「恢复 DHCP」只能靠肉眼等。
#:
#: 45 秒不是随手给的：``route.renew`` 在守护侧是**一轮内跑完**的（交还 → 等新租约
#: 最多 10s → 按新租约重新固化，见 ``routeops._apply_op``），而它内部每一步
#: （``networksetup``）自身也有十几秒的上限。这个值必须**宽于那条命令的最坏用时**，
#: 否则正常执行会被误判成超时。
OP_TIMEOUT_S = 45.0

#: 等 ``welcome`` 的上限（秒）。**不是**给正常握手留的时间（本地微秒级），而是给
#: 「守护 accept 了却不回帧」这一路留的：假死、被 SIGSTOP、或正卡在一轮长任务里
#: （主循环是单线程，跑任务时不 ``serve()``）。没有它，app 会**永久**停在
#: ``handshake`` —— 既收不到 EOF（``pump()`` 不会醒）也不重连，看上去就是「一直
#: 没连上」而日志里一行都没有。
HANDSHAKE_TIMEOUT_S = 5.0

#: 会话状态（三态，别拿布尔去糊）
STATE_OFF = "off"            # 没连（未安装 / 没跑 / 刚断开）
STATE_HANDSHAKE = "handshake"  # 已连上，等 welcome
STATE_READY = "ready"        # 握手通过，可以收发


class HelperLink:
    """app 侧的会话客户端。**框架单例**，插件经 ``ctx.helper`` 使用它。"""

    def __init__(self, *, client: str = "app") -> None:
        self.client = str(client)
        # **跨线程互斥（2026-09-29 通道换成会话时补上）**：``declare`` / ``run_op`` /
        # ``subscribe`` 会被**框架托管的非主线程**调用 —— route_guard 的探活服务
        # （``intent.RouteProbeService.poll()``）就在那条线程上声明意图，而 ``pump()``
        # 在 Qt 主线程（``QSocketNotifier`` 驱动）。两边都在写同一只套接字：不给锁，
        # 两帧的字节会交错（对端按长度前缀解析 ⇒ 必炸），``_pending`` / ``_intents``
        # 也会被两个线程同时改。可重入是必需的：``_send`` 失败会走 ``_teardown``，
        # 而 ``_teardown`` 会回调待回执（组件可能在回调里再 ``declare`` 一次）。
        self._lock = threading.RLock()
        self._sock: Optional[socket.socket] = None
        self._reader = FrameReader()
        #: 出站队列（**写的那一半分帧**）。``SOCK_STREAM`` 上「一次发完」不成立：
        #: 发不完的字节攒在这里，等「可写」再来（见 ``on_writable``）。没有它，
        #: 非阻塞套接字缓冲一满，``sendall`` 抛的 ``BlockingIOError`` 会被当成
        #: 「对端坏了」——而真相往往只是「守护正在单线程跑一轮任务、没在读」。
        self._out = FrameWriter()
        #: 此刻是否在等「可写」通知（只在**变化**时通知装配层，见 ``_set_write_interest``）
        self._want_write = False
        self._state = STATE_OFF
        # 意图：subject → (rev, payload, takeover)。**重连即重发全部**，靠 rev 幂等
        # 收敛；``takeover`` 必须跟着一起记 —— 重发时丢了它，「我声明过要接管」会
        # 悄悄降级成「我只是再声明一次」，守护于是按「第二个写者」把它拒掉。
        self._intents: Dict[str, Tuple[str, Any, bool]] = {}
        # 被守护拒掉的期望态：subject → (reason, detail)。**只记「这一件事没生效」**
        # 那两条（``protocol.INTENT_REJECT_REASONS``），且不猜：收不到带 subject 的
        # 拒绝帧（旧版守护）就一个字都不记 —— 宁可查不到，不可记错 subject。
        self._rejections: Dict[str, Tuple[str, str]] = {}
        # 已经记过日志的 reason：subject → reason。**只管刷屏，不参与判定** ——
        # 重连会重发全部意图，同一条拒绝因此可能来很多次，而日志里「一直在」与
        # 「来了几次」是两件事（后者没有意义）。
        self._reject_logged: Dict[str, str] = {}
        # 订阅：客户端想要的 subject 集合（重连后重新声明）。
        self._subjects: Set[str] = set()
        # 观测缓存：最近一次 ``event.push`` 的事实。**只在内存**（通道就是事实的来源，
        # 盘上那份只是崩溃恢复快照）。
        self._observed: Dict[str, Any] = {}
        # 待回执：req_id → (截止时刻, 回调)
        self._pending: Dict[str, Tuple[float, Callable[[bool, Any, str], None]]] = {}
        self._req_seq = 0
        self._last_error = ""
        self._welcome_rejected = False
        #: 握手期限（``time.monotonic()`` 刻度）；离开 ``handshake`` 即清空。
        self._handshake_deadline: Optional[float] = None
        #: 守护在握手里**自陈**的版本（连上才有，断开即空）。它回答的是「此刻跑着的
        #: 那份代码是哪一版」，与「盘上装的是哪一版」是两件事 —— 重装了但守护没重启
        #: 时两者会不一致，而只有前者能说明「你现在看到的行为出自哪一版」。
        self._helper_version = ""
        # 事件回调（框架注入）
        self.on_push: Optional[Callable[[str, Any], None]] = None
        self.on_state: Optional[Callable[[str], None]] = None
        # 「把回调挪到持有线程上执行」的注入点（框架装配时给）。**默认就地执行**
        # （离屏测试不装配框架，直接调 ``pump()`` 即可）。为什么需要它：``on_state`` /
        # ``on_pending`` / 回执回调里坐着的是**主线程**的东西（挂摘 QSocketNotifier、
        # 起停 QTimer、碰控件、碰面板），而它们可能被长驻服务线程间接触发 —— 最直接的
        # 一条是服务线程 ``declare`` 时 ``_send`` 失败 ⇒ ``_teardown`` ⇒ 回调。Qt 对
        # 「发者与收者不同线程」的信号自动排队，同线程则直调，故注入它不改变主线程调用
        # 的时序（同步语义保住）。
        self.on_post: Optional[Callable[[Callable[[], None]], None]] = None
        # 「在等回执的命令」的集合变了（框架注入）。收到最早的那个截止时刻，或
        # ``None``（没有在等的了）。框架据此挂一只**按需**的到点检查定时器 ——
        # 没有它，``_expire_pending()`` 只能等下一次 ``pump()`` 才跑，而「守护把
        # 命令吞了、一个字节都不回」恰恰是「不会有下一次 pump」的那种情形。
        self.on_pending: Optional[Callable[[Optional[float]], None]] = None
        # 「握手期限」变了（框架注入）。收到截止时刻，或 ``None``（不用再等了）。
        # 与 ``on_pending`` 同形：本模块一只定时器都不持有，到期检查由装配层排
        # （**按需**，没有在等的握手就一只定时器都不跑）。
        self.on_handshake: Optional[Callable[[Optional[float]], None]] = None
        # 「出站队列里有没有发不完的字节」变了（框架注入）。布尔：True ＝ 需要
        # 「可写」通知（装配层挂上写方向的 QSocketNotifier），False ＝ 摘掉。
        # **只在变化时调** —— notifier 的开关只在主线程合法，重复通知就是每次发送
        # 都往主线程塞活。
        self.on_writable: Optional[Callable[[bool], None]] = None
        # 「我声明的期望态被守护拒了」（框架注入）：``(subject, reason, detail)``。
        # 与 ``on_state`` 同款：回调里坐着的可能是主线程的东西，故经 ``_invoke`` 投递。
        self.on_reject: Optional[Callable[[str, str, str], None]] = None

    # ---------- 查询 ----------

    @property
    def state(self) -> str:
        return self._state

    @property
    def connected(self) -> bool:
        return self._state == STATE_READY

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def helper_version(self) -> str:
        """守护自陈的版本（握手给的）；没连上返回空串。

        版本闸门因此不必再去读盘上那份：连上时**问本人**最准，连不上才退回盘上那份
        （那时也没人在跑，盘上那份就是唯一的线索）。
        """
        return self._helper_version

    def snapshot(self, subject: str, default: Any = None) -> Any:
        """某 subject 的最新观测（**内存里那份**）。没收到过返回 ``default``。"""
        return self._observed.get(subject, default)

    def known_subjects(self) -> Tuple[str, ...]:
        return tuple(sorted(self._observed))

    def intent_rejection(self, subject: str) -> str:
        """这个 subject 的期望态**此刻**被守护拒着吗（返回原因，空串＝没被拒）。

        它回答的是「我声明了，算数吗」—— ``declare()`` 返回 True 只说明**发出去了**，
        不说明守护接受了：意图帧不带 ``req_id``，被拒时无处配对，此前 app 侧只剩
        一行 warning（2026-09-30 挂账）。单客户端今天撞不到，多客户端一上线就是
        组件静默失效：面板写着「已固化」，守护其实在替别人管着这个 subject。

        **空串不等于生效**：读不到拒绝帧（旧版守护不带 subject）也是空串。这是有意
        的降级 —— 「不知道」与「没被拒」在这条查询上故意不分，谁需要区分就去看
        ``diagnostics()['rejections']`` 里的键在不在。
        """
        return self._rejections.get(str(subject or ""), ("", ""))[0]

    def rejection_detail(self, subject: str) -> str:
        """守护给的那句人话（没被拒时是空串）。"""
        return self._rejections.get(str(subject or ""), ("", ""))[1]

    def fileno(self) -> int:
        """当前套接字的 fd；没连上返回 ``-1``（框架据此停掉/摘掉 notifier）。"""
        return self._sock.fileno() if self._sock is not None else -1

    # ---------- 生命周期 ----------

    @_locked
    def connect(self, path) -> bool:
        """连一次。成功即发出握手帧，返回 True（**不等于已就绪**）。"""
        if self._state != STATE_OFF:
            return True
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # 优化项：macOS 的 AF_UNIX 默认 SO_SNDBUF 只有 8 KiB，调大之后大多数帧一次
        # 就发完、根本不进出站队列。**设不上也不影响送达**（分次写兜住），故不校验。
        protocol.widen_send_buffer(sock)
        try:
            sock.settimeout(CONNECT_TIMEOUT_S)
            sock.connect(str(path))
        except OSError as e:
            sock.close()
            self._last_error = "连接失败：%s" % e
            log.debug("会话连接失败（%s）：%s", path, e)
            return False
        try:
            sock.setblocking(False)
        except OSError:  # pragma: no cover
            pass
        self._sock = sock
        self._reader = FrameReader()
        # 新连接＝新队列：上一轮没发完的字节属于上一条连接（对端已经不会再读了）。
        self._out = FrameWriter()
        self._want_write = False
        self._set_state(STATE_HANDSHAKE)
        # 期限先挂上再发握手帧：万一 ``_send`` 当场失败（``_teardown`` → OFF），
        # 期限也会被 ``_set_state`` 顺手清掉并通知装配层撤掉定时器。
        self._handshake_deadline = time.monotonic() + HANDSHAKE_TIMEOUT_S
        self._notify_handshake()
        self._send(protocol.hello(self.client))
        return True

    @_locked
    def expire_pending(self) -> None:
        """到点检查一遍在等的命令（框架按 ``on_pending`` 报来的时刻挂单发定时器）。

        为什么不靠 ``pump()`` 顺带做：``pump()`` 只在「套接字可读」时被调，而
        「守护把命令吞了、一个字节都不回」正是**不会有下一次可读**的那种情形。
        """
        self._expire_pending()

    @_locked
    def close(self) -> None:
        """主动断开（退出时调用，幂等）。"""
        self._teardown("已关闭")
        self._set_state(STATE_OFF)

    @_locked
    def pump(self) -> None:
        """读走套接字上所有可读数据并逐帧处理。由 ``QSocketNotifier`` / 测试调用。"""
        sock = self._sock
        if sock is None:
            return
        # 读到 EAGAIN 为止：一次 recv 可能只拿到半条帧，也可能一口气拿到十几条。
        while self._sock is sock:
            try:
                data = sock.recv(65536)
            except (BlockingIOError, InterruptedError):
                break
            except OSError as e:
                self._teardown("读失败：%s" % e)
                break
            if not data:
                self._teardown("对端关闭（守护没了或重启了）")
                break
            try:
                messages = self._reader.feed(data)
            except FrameError as e:
                self._teardown("帧非法：%s" % e)
                break
            for msg in messages:
                self._handle(msg)
                if self._sock is not sock:
                    break
        self._expire_pending()

    @_locked
    def flush_out(self) -> None:
        """再推一次出站队列（装配层在「可写」事件里调；测试里直接调）。

        与 ``pump()`` 对称 —— 读的一半是 ``pump()``，写的一半是它。
        """
        self._flush()

    # ---------- 下行：意图 / 命令 / 订阅 ----------

    @_locked
    def declare(self, subject: str, payload: Any, *, takeover: bool = False) -> bool:
        """声明某 subject 的期望态（``payload=None`` ＝ 撤回）。

        幂等：同一内容重复调用不会重复上线（``rev`` 相同即不再发）。返回**是否
        已经送达守护**（未连接时返回 False，但意图已记下、连上会自动补发）。

        ``takeover=True`` ＝ 显式接管别人正在写的那个 subject（守护侧默认拒第二个
        写者）。正常路径不要带它 —— 带上就等于「我知道有别人在写，我要抢」。
        """
        rev = protocol.payload_rev(payload)
        prev = self._intents.get(subject)
        if prev is not None and prev[0] == rev and prev[2] == takeover:
            return self._state == STATE_READY
        self._intents[subject] = (rev, payload, takeover)
        # 重新声明＝重新主张，上一次的判决不再描述此刻（等下一帧的答复）。留着它会
        # 让「已经接管回来了」看上去还像失败 —— 而这一位正是「有没有生效」的唯一读数。
        self._forget_rejection(subject)
        if self._state == STATE_READY:
            self._send(protocol.intent_set(subject, payload, rev=rev, takeover=takeover))
            return True
        return False

    @_locked
    def retract(self, subject: str) -> bool:
        """撤回某 subject 的意图（＝ ``declare(subject, None)``）。"""
        return self.declare(subject, None)

    @_locked
    def run_op(self, op: str, params: Any = None,
               callback: Optional[Callable[[bool, Any, str], None]] = None) -> bool:
        """发一条一次性命令；``callback(ok, result, error)`` 在回帧到达时被调用。

        **帧级配对**：``req_id`` 随请求走、回帧原样带回，故没有水位线、也不跨重启
        留状态。未连接时返回 False 并就地回调失败 —— 旧形态下这会变成「命令排进
        队列等下一轮」，用户看不到任何反馈。
        """
        if self._state != STATE_READY:
            if callback is not None:
                callback(False, None, "守护未连接")
            return False
        self._req_seq += 1
        req_id = "%d-%d" % (os.getpid(), self._req_seq)
        if callback is not None:
            self._pending[req_id] = (time.monotonic() + OP_TIMEOUT_S, callback)
            self._notify_pending()
        return self._send(protocol.op_run(op, params, req_id))

    @_locked
    def subscribe(self, subjects: List[str]) -> bool:
        """订阅这些 subject 的最新事实（服务端收到后**立刻推一份当前的**）。"""
        new = [s for s in subjects if s and s not in self._subjects]
        self._subjects.update(s for s in subjects if s)
        if not new or self._state != STATE_READY:
            return self._state == STATE_READY
        return self._send(protocol.subscribe(new))

    @_locked
    def unsubscribe(self, subject: str) -> bool:
        """撤回一个订阅（组件被禁用 / 卸载时框架调）。

        ``subscribe`` 与它必须成对，否则守护会一直朝这条连接推一个已经没人看的领域
        —— 那正是「通道换掉之后还会有多余流量」的一处。
        """
        subject = str(subject or "")
        if not subject or subject not in self._subjects:
            return False
        self._subjects.discard(subject)
        if self._state != STATE_READY:
            return False
        return self._send(protocol.unsubscribe([subject]))

    @_locked
    def unsubscribe_all(self) -> None:
        self._subjects.clear()

    # ---------- 内部：收发 ----------

    def _invoke(self, fn: Callable[[], None]) -> None:
        """执行一只**要交给主线程**的回调（注入 ``on_post`` 时先投递过去）。

        没注入口就**就地执行**（离屏测试直接调 ``pump()``，本来就在「持有线程」上）。
        投递失败就地执行 —— 回调里的异常已经被包住，不该因为它把会话的状态机带停。
        """
        sink = self.on_post
        if callable(sink):
            try:
                sink(fn)
                return
            except Exception:  # noqa: BLE001
                log.exception("投递回调失败（改就地执行）")
        try:
            fn()
        except Exception:  # noqa: BLE001 —— 回调炸掉不能拖垮会话
            log.exception("会话回调异常")

    def _send(self, message: Dict[str, Any]) -> bool:
        """把一帧放进出站队列，并尽量写出去。

        **写不完不算失败**（返回 True，字节留在队列里等「可写」）：非阻塞套接字上
        缓冲满只是「对端此刻没在读」，不是「对端坏了」。只有编帧失败（帧超长）与
        队列积压超限（对端长时间不读）才判断开。
        """
        if self._sock is None:
            return False
        try:
            self._out.queue(message)
        except (OSError, FrameError) as e:
            self._teardown("发送失败：%s" % e)
            return False
        return self._flush()

    def _flush(self) -> bool:
        """把队列里的字节尽量推出去；推不完就登记「等可写」。"""
        sock = self._sock
        if sock is None:
            self._out.clear()
            self._set_write_interest(False)
            return False
        try:
            done = self._out.flush(sock)
        except OSError as e:
            self._teardown("发送失败：%s" % e)
            return False
        self._set_write_interest(not done)
        return True

    def _set_write_interest(self, want: bool) -> None:
        """告诉装配层「要不要可写通知」。**只在变化时上报**。

        与 ``on_state`` 同理：它要动 ``QSocketNotifier``（只在主线程合法），而
        ``_send`` 可能由长驻服务线程触发，故经 ``_invoke`` 投递。
        """
        if want == self._want_write:
            return
        self._want_write = want
        if callable(self.on_writable):
            self._invoke(lambda: self.on_writable(want))

    def _handle(self, msg: Dict[str, Any]) -> None:
        kind = protocol.message_type(msg)
        if kind == protocol.WELCOME:
            self._on_welcome(msg)
        elif kind == protocol.EVENT_PUSH:
            subject = str(msg.get("subject") or "")
            if subject:
                self._observed[subject] = msg.get("observed")
                if callable(self.on_push):
                    # 与其余四只回调同形（``_invoke``）：投递口在主线程上就是直调，
                    # 时序一个字不变；多的是一层异常隔离 —— 直接调时订阅方（总线
                    # 分发）抛异常会冲出 ``pump()``，把同一批里剩下的帧一起丢掉。
                    self._invoke(lambda: self.on_push(subject, msg.get("observed")))
        elif kind == protocol.REPLY:
            self._on_reply(msg)
        elif kind == protocol.ERROR:
            reason = str(msg.get("reason") or "")
            subject = str(msg.get("subject") or "")
            if reason in protocol.INTENT_REJECT_REASONS and subject:
                self._note_rejection(subject, reason, str(msg.get("detail") or ""))
            else:
                log.warning("守护拒收一帧：%s（%s）", reason, msg.get("detail"))
            req_id = msg.get("req_id")
            if req_id is not None:
                self._finish(req_id, False, None,
                             str(msg.get("reason") or "被守护拒收"))

    def _on_welcome(self, msg: Dict[str, Any]) -> None:
        if not msg.get("ok"):
            why = str(msg.get("reason") or "未知原因")
            self._last_error = why
            # 首报用 ERROR（这是用户要看见的一件事：装错了版本 / 守护太旧），
            # 此后降成 DEBUG —— 重连是 1s 一次的循环，不降噪就是每秒一行。
            if not self._welcome_rejected:
                self._welcome_rejected = True
                log.error("会话握手被拒：%s", why)
            else:
                log.debug("会话握手仍被拒：%s", why)
            self._teardown("握手被拒")
            return
        # 复位「已被拒过」：不复位的话，一旦被拒过一次，此后**即便连上成功**，
        # 再被拒也只会记 DEBUG —— 而那往往正是「守护被换成旧版」这种要看见的事。
        self._welcome_rejected = False
        self._helper_version = str(msg.get("helper_version") or "")
        self._set_state(STATE_READY)
        log.info("会话就绪：守护 %s，协议 %s", self._helper_version, msg.get("protocol"))
        # 重连后的**重新断言**：把全部意图与全部订阅重发一遍。幂等靠 rev，
        # 不需要任何水位线 —— 这正是「盘被第三方改写 / 守护重启」两条路都自愈的原因。
        for subject, (rev, payload, takeover) in list(self._intents.items()):
            self._send(protocol.intent_set(subject, payload, rev=rev, takeover=takeover))
        if self._subjects:
            self._send(protocol.subscribe(sorted(self._subjects)))

    def _on_reply(self, msg: Dict[str, Any]) -> None:
        req_id = str(msg.get("req_id") or "")
        if not req_id:
            return
        self._finish(req_id, bool(msg.get("ok")), msg.get("result"),
                     str(msg.get("error") or ""))

    def _finish(self, req_id: str, ok: bool, result: Any, error: str) -> None:
        entry = self._pending.pop(req_id, None)
        if entry is None:
            return
        self._notify_pending()
        _deadline, cb = entry
        # 经 ``_invoke`` 走：``_teardown``（可能发生在长驻服务线程上）也会把待回执
        # 一律判失败，而回调里坐着的通常是主线程的东西。
        self._invoke(lambda: cb(ok, result, error))

    def _notify_handshake(self) -> None:
        """告诉框架「握手期限」变成了什么样（截止时刻 / ``None``＝不用再等）。"""
        sink = self.on_handshake
        if not callable(sink):
            return
        deadline = self._handshake_deadline
        self._invoke(lambda: sink(deadline))

    @_locked
    def expire_handshake(self) -> None:
        """握手到点仍未就绪 ⇒ 断开重连（**不永久僵在 handshake**）。

        ``declare`` 明确会由**长驻服务线程**调用（见模块 docstring），故本方法与
        其他改状态机的出口一样必须持锁：它会读完 ``_state`` 后走 ``_teardown``
        关套接字并清 ``_pending`` —— 不持锁时那一步正好可能撞在服务线程正在跑的
        ``declare`` → ``_send`` → ``sock.send`` 中间。

        「守护 accept 了却不回 welcome」这条路上：既不会有 EOF（``pump()`` 不会被
        唤醒）也不会有回帧，只有这个期限能把连接从「连着但不可用」里拉出来 ——
        断开后走的是正常重连，下一次也许就握手成功了。
        """
        deadline = self._handshake_deadline
        if deadline is None or self._state != STATE_HANDSHAKE:
            return
        if time.monotonic() < deadline:
            return
        self._teardown("握手超时：守护接了连接却不回 welcome")

    def _notify_pending(self) -> None:
        """告诉框架「在等的回执」集合变成了什么样（最早截止时刻 / None）。"""
        sink = self.on_pending
        if not callable(sink):
            return
        deadline = min((d for d, _cb in self._pending.values()), default=None)
        self._invoke(lambda: sink(deadline))

    def _expire_pending(self) -> None:
        """到点没回执的命令：当场判失败。"""
        if not self._pending:
            return
        now = time.monotonic()
        for req_id in [k for k, (dl, _cb) in self._pending.items() if dl <= now]:
            self._finish(req_id, False, None, "超时未收到回执")

    def _note_rejection(self, subject: str, reason: str, detail: str) -> None:
        """记下「这个 subject 的期望态没生效」并上抛（**拒绝的回执在这里落地**）。

        日志只在这一档**与上次不同**时才写：重连会重发全部意图，同一个 subject 于是
        可能被拒很多次，而「一直在」与「又来一次」对用户是同一件事。``detail`` 里带
        着对方是谁（client / uid / pid），那条才是要给用户看的，故不进去重键。
        """
        self._rejections[subject] = (reason, detail)
        if self._reject_logged.get(subject) != reason:
            self._reject_logged[subject] = reason
            # ERROR 而不是 WARNING：这是「用户声明的期望态没生效」，不是「有一帧没
            # 送出去」。静默失效比报错贵得多 —— 面板会一直写着「已固化」。
            log.error("守护拒收我的期望态：%s（%s）%s", subject, reason,
                      f"—— {detail}" if detail else "")
        sink = self.on_reject
        if callable(sink):
            self._invoke(lambda: sink(subject, reason, detail))

    def _forget_rejection(self, subject: str) -> None:
        """撤掉这个 subject 的判决（重新声明 / 断开时调）。"""
        self._rejections.pop(subject, None)
        self._reject_logged.pop(subject, None)

    def _teardown(self, why: str) -> None:
        # 断开即忘掉它自陈的版本：留着就等于在说「此刻跑着的是这一版」，而此刻没人跑。
        self._helper_version = ""
        # 判决也只属于**这条**连接：对端没了，「我被拒了」这个结论没有对象了（留着会
        # 让「重连后被接受」看上去还像失败）。**只清判决、不清 ``_reject_logged``** ——
        # 后者管的是「这一种拒绝报过没有」，是进程级的事；清了它，守护每重启一次就要
        # 重刷一行同样的错。
        self._rejections.clear()
        # 队列属于**这条**连接：对端没了，剩下半截帧再发也没有意义（新连接是新的
        # 帧流）。顺手撤掉「等可写」——套接字马上要关了，写方向不会再有事发生。
        self._out.clear()
        self._set_write_interest(False)
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:  # pragma: no cover
                pass
            log.info("会话断开：%s", why)
        if self._state != STATE_OFF:
            self._set_state(STATE_OFF)
        # 待回执一律判失败：连接没了就不会再有回执，留着只会静默超时。
        for req_id in list(self._pending):
            self._finish(req_id, False, None, "会话已断开")

    def _set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        # 离开 handshake 的所有出口（握手成功 / 被拒 / 发送失败 / 主动关）都在这里
        # 汇合 ⇒ 期限的清理只写一处，不靠每个出口各自记得清。
        if state != STATE_HANDSHAKE and self._handshake_deadline is not None:
            self._handshake_deadline = None
            self._notify_handshake()
        if callable(self.on_state):
            # 经 ``_invoke`` 走：``on_state`` 在装配层里要动 ``QSocketNotifier`` 与
            # ``QTimer``（只在主线程合法），而本方法可能由服务线程的 ``declare`` 触发。
            self._invoke(lambda: self.on_state(state))

    # ---------- 诊断 ----------

    def diagnostics(self) -> Dict[str, Any]:
        """一段可贴进 issue 的现状（不含路径等机器相关值）。"""
        fd = self.fileno()
        return {
            "state": self._state,
            "subjects": sorted(self._subjects),
            "intents": sorted(self._intents),
            "observed": sorted(self._observed),
            "rejections": {s: r for s, (r, _d) in sorted(self._rejections.items())},
            "pending": len(self._pending),
            "outbox": self._out.pending,
            "fd": fd,
            "last_error": self._last_error,
        }


__all__ = ["HelperLink", "STATE_OFF", "STATE_HANDSHAKE", "STATE_READY",
           "RETRY_INTERVAL_S", "OP_TIMEOUT_S", "HANDSHAKE_TIMEOUT_S"]
