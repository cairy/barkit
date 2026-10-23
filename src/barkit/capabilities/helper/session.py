"""session：守护侧的会话服务端（``selectors`` + 长度分帧 + 对端凭据校验）。

这个模块是**传输层**，不含任何业务判断：它只做「收帧 → 交给 handler → 把回帧发出去」。
「哪种意图算数、哪条命令能跑」全在 ``tasks`` / ``primitives`` 与调用方的 handler 里。

分帧的**两半**都在（2026-09-29）
--------------------------------
读的一半是 ``FrameReader``（拼帧）；写的一半是每条连接一个 ``FrameWriter`` 出站队列。
**``SOCK_STREAM`` 上「一次写完」不成立**：守护是单线程，跑一轮任务时不 ``serve()``，
此间客户端读端停摆、发送缓冲会被填满 —— 此时非阻塞套接字上的 ``sendall`` 直接抛
``BlockingIOError``，而那不是「客户端坏了」。故发不完的字节攒在队列里，用
``selectors`` 的 ``EVENT_WRITE`` 报可写时再推（**只在真有积压时才订阅**，否则套接字
几乎永远可写、主循环空转）。

为什么是套接字而不是文件（选型理由，2026-09-29）
-----------------------------------------------
文件通道只有「此刻盘上写着什么」一个语义，而两侧真正要换的是**四件事**：意图、命令、
事实、存活。后两件（请求/应答、存活）**文件格式永远补不上** —— 于是它们只能寄生在
时间戳 / 端口 / 队列上，每次寄生都留下过一个真实故障（详见
``内部计划（通信通道改会话）`` §2）。一条长期会话天然自带这三样。

安全（**逐字对照旧形态，不降级**）
----------------------------------
旧的期望态文件住在用户私有数据目录里，能改它的只有该用户的进程 —— 文件通道
**从来没有校验过调用方**，它只是把调用方的范围限定成「能写那个目录的用户」。
新的套接字节点同样只放进用户私有目录、属主设成该用户、模式 0600，父目录 0700
⇒ 够得着的进程集合**逐字相同**。

外加一层旧形态给不了的：连接建立时用 ``LOCAL_PEERCRED`` 读出对端 uid，**不等于
允许的用户即拒绝**。（实测：Darwin 上 ``SOL_LOCAL=0`` / ``LOCAL_PEERCRED=1``，
回 76 字节 ``struct xucred``，uid 在偏移 4；``LOCAL_PEERPID=2`` 回 4 字节 pid。
系统自带的 Python 3.9 **没有** ``socket.getpeereid``，故走 ``getsockopt``。）

反面警示（写死在代码里，别改）：**把套接字放进 root 拥有的公共目录并放开读写，
那才是降级** —— 任何本地用户都能连。路径与属主的处置是硬约束，不是实现细节。
"""

from __future__ import annotations

import errno
import os
import selectors
import socket
import struct
import time
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Set, Tuple

from . import protocol
from .protocol import FrameError, FrameReader, FrameWriter
from .safeio import log

#: Darwin 的 ``SOL_LOCAL`` / ``LOCAL_PEERCRED`` / ``LOCAL_PEERPID``。
#: 不是 POSIX 标准量（Linux 上是 ``SO_PEERCRED``），故写死数值并附实测记录：
#: 2026-09-29 本机（macOS，系统 python 3.9.6）实测 uid 与 ``os.getuid()`` 一致。
_SOL_LOCAL = 0
_LOCAL_PEERCRED = 1
_LOCAL_PEERPID = 2
#: ``struct xucred``（Darwin）的字段偏移。**本机字节序、无对齐填充**（``=``）：
#:
#: =======  ============  ==================================================
#: 偏移     字段          说明
#: =======  ============  ==================================================
#: 0        ``cr_version``  u_int
#: 4        ``cr_uid``      uid_t —— **安全校验只认这一个**
#: 8        ``cr_ngroups``  short（**2 字节**，不是 4）
#: 10       （对齐填充）    2 字节，凑 ``gid_t`` 的 4 字节对齐
#: 12       ``cr_groups``   gid_t[16]，**主组就是 ``cr_groups[0]``**
#: =======  ============  ==================================================
#:
#: ⚠️ **这个结构体里没有 ``cr_gid`` 字段**。早先按「version/uid/gid 三个 4 字节连排」
#: 去解，第三段实际落在 ``cr_ngroups`` ＋ 填充上 ⇒ 解出来的「gid」是**组个数**
#: （小端下填充为 0 时两者数值恰好相等，所以错的那个值看上去像真的）。uid 读对了、
#: 安全校验不受影响，但它会让 ``PeerInfo.gid`` 一直是假值。
_XUCRED_SIZE = 76
_XUCRED_OFF_NGROUPS = 8
_XUCRED_OFF_GROUPS = 12

#: 单连接的超时（秒）。**只用于「半条帧卡住」**，不是心跳：长期空闲的连接一个字节
#: 都不发是正常的（订阅型客户端可以几小时不讲话）。故这个值给得很大。
IDLE_READ_TIMEOUT_S = 3600.0

#: 新连接发来**第一条帧**（握手）的等待上限（秒）。连上了却不说话的连接不该永久
#: 占着一个 fd 与一个 ``Connection`` —— 对面可能是卡住的进程，也可能根本不是我们
#: 的客户端（uid 校验之外还有「什么都不发」这种占用法）。
HELLO_TIMEOUT_S = 10.0

#: ``sun_path`` 的字节上限（Darwin 是 104，含结尾 NUL ⇒ 可用 103）。
#: 超了内核直接 ``AF_UNIX path too long`` —— 报那句话时人看不出是哪一段长，
#: 故在 bind 之前先自己量一次，把实际长度写进错误里。
MAX_SOCKET_PATH = 103

#: 建不出节点时，**没有通道**的守护怎么跑（2026-10-06）。
#:
#: 这不是假想故障：节点路径上留一个**普通文件**（不是套接字）就会 ``EADDRINUSE``
#: —— ``open()`` 只删「确实是套接字」的那种，普通文件宁可留着也不误删。路径过长、
#: 父目录被锁成 root:root 0700 同理。旧实现里 ``serve()`` 见 ``_selector is None``
#: 就**立刻返回** ⇒ 主循环 ``while True: serve(); run_due()`` 变成一颗核的 100%
#: 空转，且**永不重试**：节点一直建不起来，直到重启机器。
#:
#: 故这里给两档：空转兜底（睡，不烧 CPU）＋ 隔一段时间重试一次（能自愈）。
REOPEN_INTERVAL_S = 10.0
#: 无监听时单轮 ``serve()`` 的睡眠上限（秒）。取这个值而不是「一直睡到有任务」，
#: 是为了让 ``run_due()`` 仍能按节拍跑（保活 / 收敛不因通道缺席而停摆）。
LISTENLESS_IDLE_S = 1.0


class PeerInfo(NamedTuple):
    uid: int
    gid: int
    pid: int = -1


def parse_xucred(raw: bytes) -> Optional[Tuple[int, int, int]]:
    """把 ``LOCAL_PEERCRED`` 取回的字节解成 ``(uid, gid, ngroups)``。

    抽成**纯函数**是为了让偏移量能被离线验证：这类错误在本机看不出来（uid 读对、
    安全校验照旧通过，只有 ``gid`` 是假值），非得拿一段按文档布局造出来的字节去解
    才暴露 —— 而造字节不需要一条真的 Unix 域套接字。

    解不出来返回 ``None``（**不猜**）：宁可让调用方按「未知身份」处置，也不动用
    「拿不到就等于允许」这类兜底 —— 那等于把校验关掉。``ngroups == 0`` 时
    ``cr_groups[0]`` 没有意义，此时 ``gid`` 记 ``-1``（未知）而非 0。
    """
    if len(raw) < _XUCRED_OFF_GROUPS + 4:
        return None
    uid = struct.unpack_from("=I", raw, 4)[0]
    ngroups = struct.unpack_from("=h", raw, _XUCRED_OFF_NGROUPS)[0]
    gid = struct.unpack_from("=I", raw, _XUCRED_OFF_GROUPS)[0] if ngroups > 0 else -1
    return int(uid), int(gid), int(ngroups)


def peer_credentials(sock: socket.socket) -> Optional[PeerInfo]:
    """读对端 uid/gid/pid。读不到返回 ``None``（**不猜**）。

    读不到就返回 ``None``、由调用方按「未知身份」处置，绝不动用「拿不到就等于允许」
    这类兜底 —— 那等于把校验关掉。
    """
    try:
        raw = sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERCRED, _XUCRED_SIZE)
    except OSError:
        return None
    parsed = parse_xucred(raw)
    if parsed is None:
        return None
    uid, gid, _ngroups = parsed
    pid = -1
    try:
        pid = struct.unpack("=i", sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID, 4))[0]
    except OSError:
        pass
    return PeerInfo(uid=int(uid), gid=int(gid), pid=int(pid))


class Connection:
    """一条已建立的客户端连接。"""

    __slots__ = ("sock", "peer", "reader", "out", "want_write",
                 "subscriptions", "client", "alive", "intents", "deadline")

    def __init__(self, sock: socket.socket, peer: PeerInfo) -> None:
        self.sock = sock
        self.peer = peer
        self.reader = FrameReader()
        #: 出站队列（**写的那一半分帧**）。守护是单线程：它跑一轮任务时不 ``serve()``，
        #: 此间这条连接的读端停摆、发送缓冲会被填满 —— 发不完的字节攒在这里，等
        #: ``selectors`` 报「可写」再推。没有它，非阻塞套接字上 ``sendall`` 抛的
        #: ``BlockingIOError`` 会被当成「客户端坏了」而白掉一条连接。
        self.out = FrameWriter()
        #: 此刻是否已把本连接的读事件扩成「读 ＋ 写」（见 ``_arm_write``）。
        self.want_write = False
        self.subscriptions: Set[str] = set()
        #: 这条连接**持有写者身份**的 subject（一个 subject 一个写者，见
        #: ``core._Dispatcher._claim``）。断开时据此两件事：撤**它名下**的依附型
        #: 意图、把写者身份交还（否则一条死连接会永远挡着下一个写者）。
        self.intents: Set[str] = set()
        self.client = ""
        self.alive = True
        #: 回收期限（``time.monotonic()`` 刻度）；``None`` ＝ 不设期限。见
        #: ``SessionServer._refresh_deadline`` —— 只有「说了话但没说完」的那两种
        #: 情形才有期限，**正常空闲没有**。
        self.deadline: Optional[float] = None

    @property
    def fd(self) -> int:
        return self.sock.fileno()

    def __repr__(self) -> str:  # pragma: no cover - 诊断用
        return "<Connection uid=%d pid=%d subs=%s>" % (
            self.peer.uid, self.peer.pid, sorted(self.subscriptions))


class SessionServer:
    """监听一个用户私有目录下的 Unix 域套接字，把收到的帧交给 ``handler``。

    ``handler(conn, message)`` 在**本线程**（守护主循环）同步执行 —— 与旧形态下
    「读到期望态文件就地处理」逐字对应，没有新增并发面。
    """

    def __init__(self, path, *, handler: Callable[[Connection, Dict[str, Any]], None],
                 allowed_uid: Optional[int] = None, backlog: int = 8,
                 on_close: Optional[Callable[[Connection], None]] = None) -> None:
        self.path = str(path)
        self.handler = handler
        #: 连接断开时的回调（业务侧用它撤「依附型意图」）。None ＝ 不管。
        self.on_close = on_close
        self.allowed_uid = allowed_uid
        self.backlog = int(backlog)
        self._selector: Optional[selectors.BaseSelector] = None
        self._listener: Optional[socket.socket] = None
        self._conns: List[Connection] = []
        self._rejected = 0
        #: 建节点**失败过**吗（``serve()`` 据此决定要不要重试；见 :data:`REOPEN_INTERVAL_S`）。
        #: 主动 ``close()`` 不算失败 —— 那是有意收摊，不该被重新拉起来。
        self._listen_failed = False
        #: 下一次重试建节点的时刻（``time.monotonic()`` 刻度）；``None`` ＝ 还没排上。
        self._reopen_at: Optional[float] = None

    # ---------- 生命周期 ----------

    def open(self) -> None:
        """建节点、设属主与权限、开始监听。**幂等**（已开则空操作）。"""
        if self._listener is not None:
            return
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if len(self.path.encode("utf-8")) > MAX_SOCKET_PATH:
            raise OSError(
                "套接字路径过长（%d 字节 > %d）：%s"
                % (len(self.path.encode("utf-8")), MAX_SOCKET_PATH, self.path))
        # 上一轮留下的节点：Unix 域套接字不会自己消失，不删就 EADDRINUSE。
        # 只删「确实是套接字」的那种；普通文件留在原地（宁可起不来也不误删数据）。
        try:
            st = os.lstat(self.path)
            import stat as _stat
            if _stat.S_ISSOCK(st.st_mode):
                os.unlink(self.path)
        except OSError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(self.path)
            # 属主与权限是**安全约束**（见模块 docstring）：节点必须归那个用户所有、
            # 且只许他读写。父目录 0700 由调用方保证。
            if self.allowed_uid is not None:
                try:
                    os.chown(self.path, self.allowed_uid, -1)
                except OSError as e:  # pragma: no cover - 非 root / 平台差异
                    log(f"会话节点改属主失败（{self.path}）：{e}")
            try:
                os.chmod(self.path, 0o600)
            except OSError:  # pragma: no cover
                pass
            srv.setblocking(False)
            srv.listen(self.backlog)
            sel = selectors.DefaultSelector()
            sel.register(srv, selectors.EVENT_READ, ("listener", None))
        except OSError:
            # 建节点失败：先把这个 fd 收掉（否则每次重试漏一个），再把「失败过」记下
            # —— ``serve()`` 据此重试；不记的话主循环会拿 ``_selector is None``
            # 当无事发生，一圈接一圈地空转（见 :data:`REOPEN_INTERVAL_S`）。
            self._listen_failed = True
            try:
                srv.close()
            except OSError:  # pragma: no cover
                pass
            raise
        self._listen_failed = False
        self._reopen_at = None
        self._listener, self._selector = srv, sel

    def close(self) -> None:
        """关掉全部连接与监听，并删掉节点（幂等）。"""
        for conn in list(self._conns):
            self.drop(conn)
        if self._selector is not None:
            try:
                self._selector.close()
            except Exception:  # noqa: BLE001 pragma: no cover
                pass
            self._selector = None
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:  # pragma: no cover
                pass
            self._listener = None
        try:
            os.unlink(self.path)
        except OSError:
            pass
        # 主动收摊（不是建节点失败）；serve() 不该把它当故障去重试。
        self._listen_failed = False
        self._reopen_at = None

    # ---------- 查询 ----------

    def connections(self) -> List[Connection]:
        return [c for c in self._conns if c.alive]

    def subscriber_count(self, subject: Optional[str] = None) -> int:
        if subject is None:
            return len(self.connections())
        return sum(1 for c in self._conns if c.alive and subject in c.subscriptions)

    def rejected_count(self) -> int:
        """被拒的连接数（身份不符）。诊断用 —— 「有没有人在敲门」要看得见。"""
        return self._rejected

    # ---------- 主循环 ----------

    def _sleep_without_listener(self, timeout: Optional[float]) -> None:
        """没有监听时的一轮：睡过去（不空转），并按期重试建节点。

        重试只对**建节点失败过**的情形做：``close()`` 之后（有意收摊）不再拉起来。
        """
        now = time.monotonic()
        if self._listen_failed:
            if self._reopen_at is None:
                self._reopen_at = now + REOPEN_INTERVAL_S
            elif now >= self._reopen_at:
                self._reopen_at = now + REOPEN_INTERVAL_S
                try:
                    self.open()
                except OSError as e:
                    log(f"会话节点仍建不起来（{REOPEN_INTERVAL_S:.0f}s 后再试）：{e}",
                        key="session.reopen", every_s=REOPEN_INTERVAL_S)
                else:
                    log(f"会话已就绪（重试成功）：{self.path}")
        # 上限压到 LISTENLESS_IDLE_S：调用方常传 None（＝没有待跑任务）或很长的时间，
        # 照原样睡会让 run_due() 的节拍（保活 / 收敛）跟着一起拖长。
        nap = LISTENLESS_IDLE_S if timeout is None else min(timeout, LISTENLESS_IDLE_S)
        time.sleep(max(0.0, nap))

    def serve(self, timeout: Optional[float]) -> None:
        """等 ``timeout`` 秒（``None`` ＝ 纯阻塞到有事发生），处理一轮可读事件。

        这是「空闲零唤醒」的落地点：没有活跃意图时调用方传 ``None``，本方法会把
        线程**一直阻塞在 ``poll`` 上**，一次都不醒（旧形态是每秒 ``stat`` 一次，
        一天 86400 次）。

        ⚠️ **没有监听时不能「立刻返回」**：主循环是 ``while True: serve(); run_due()``，
        立刻返回就等于一圈接一圈地空转 —— 守护（root、launchd KeepAlive）会把一颗核
        打满，而日志里只有启动时那一行「会话节点建立失败」，看不出它在烧 CPU。故这里
        改成睡一段（上限 :data:`LISTENLESS_IDLE_S`），并按 :data:`REOPEN_INTERVAL_S`
        重试建节点 —— 节点路径上的障碍（比如一个留下的普通文件）被清掉后能自愈。
        """
        sel = self._selector
        if sel is None:
            self._sleep_without_listener(timeout)
            return
        # 有待回收的连接时，把阻塞上限压到最近的那个期限 —— 否则「等握手超时」这条
        # 回收路径永远等不到一次 ``select`` 返回（调用方常传 ``None``＝纯阻塞）。
        # 没有待回收的连接则**完全沿用调用方的 timeout** ⇒ 空闲依然零唤醒。
        due = self._earliest_deadline()
        if due is not None:
            wait = max(0.0, due - time.monotonic())
            timeout = wait if timeout is None else min(timeout, wait)
        try:
            events = sel.select(timeout)
        except OSError as e:  # pragma: no cover - 被信号打断等
            if e.errno == errno.EINTR:
                return
            raise
        for key, mask in events:
            kind, conn = key.data
            if kind == "listener":
                self._accept_ready()
            elif conn is not None:
                # 先写后读：本连接上一轮没发完的字节，趁这次「可写」推出去。
                if mask & selectors.EVENT_WRITE:
                    self._flush(conn)
                if conn.alive and (mask & selectors.EVENT_READ):
                    self._read_ready(conn)
        self._reap_expired()

    def _refresh_deadline(self, conn: Connection) -> None:
        """重算一条连接的回收期限。

        **只有两种「说了话但没说完」的情形才有期限**：还没握手（在等 ``hello``）、
        或手里卡着半条帧。正常空闲**不设** —— 订阅型客户端几小时不讲话是对的，
        不能用闲置时长去拆它（``IDLE_READ_TIMEOUT_S`` 守的是「半帧」，不是「闲」）。
        """
        if not conn.client:
            conn.deadline = time.monotonic() + HELLO_TIMEOUT_S
        elif conn.reader.pending() > 0:
            conn.deadline = time.monotonic() + IDLE_READ_TIMEOUT_S
        else:
            conn.deadline = None

    def _earliest_deadline(self) -> Optional[float]:
        dues = [c.deadline for c in self._conns if c.alive and c.deadline is not None]
        return min(dues) if dues else None

    def _reap_expired(self) -> None:
        """回收到了期限的连接（等握手超时 / 半条帧卡住）。"""
        now = time.monotonic()
        for conn in list(self._conns):
            if not conn.alive or conn.deadline is None or conn.deadline > now:
                continue
            why = "等握手超时" if not conn.client else "半条帧卡住"
            log(f"回收一条会话连接（{why}）：uid={conn.peer.uid} pid={conn.peer.pid}")
            self.drop(conn)

    def _accept_ready(self) -> None:
        listener = self._listener
        if listener is None:
            return
        while True:
            try:
                sock, _addr = listener.accept()
            except BlockingIOError:
                return
            except OSError:  # pragma: no cover
                return
            conn = self._admit(sock)
            # 立刻读一次：客户端的握手帧**通常已经在缓冲区里**（它连上就发），
            # 而本轮 ``select`` 的事件集在接纳之前就算完了 —— 不等下一轮。
            if conn is not None:
                self._read_ready(conn)

    def _admit(self, sock: socket.socket) -> Optional[Connection]:
        """接纳一条新连接：先验身份，再登记。被拒/读不到凭据返回 ``None``。

        **身份不符立即关闭**，连握手都不给 —— 握手里才拒绝等于先接受了一个我们
        不该接受的连接。
        """
        peer = peer_credentials(sock)
        if peer is None:
            self._reject(sock, "读不到对端凭据")
            return None
        if self.allowed_uid is not None and peer.uid != self.allowed_uid:
            self._reject(sock, "对端 uid=%d 不等于允许的 %d" % (peer.uid, self.allowed_uid))
            return None
        sock.setblocking(False)
        # 优化项（**不是正确性前提**）：macOS 上只有**发送侧**的 SO_SNDBUF 算数，
        # 所以要在被接纳的这只套接字上调 —— 它正是守护往客户端发的那一侧。
        protocol.widen_send_buffer(sock)
        conn = Connection(sock, peer)
        # 接纳即排上「等握手」的期限：连上了却不发帧的连接不该永久占着。
        conn.deadline = time.monotonic() + HELLO_TIMEOUT_S
        self._conns.append(conn)
        if self._selector is not None:
            self._selector.register(sock, selectors.EVENT_READ, ("conn", conn))
        log(f"会话已接入：uid={peer.uid} pid={peer.pid}")
        return conn

    def _reject(self, sock: socket.socket, why: str) -> None:
        self._rejected += 1
        log(f"拒绝一条会话连接：{why}")
        try:
            sock.close()
        except OSError:  # pragma: no cover
            pass

    def _read_ready(self, conn: Connection) -> None:
        try:
            data = conn.sock.recv(65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self.drop(conn)
            return
        if not data:
            self.drop(conn)          # EOF：对面没了。内核给的，立刻且权威。
            return
        try:
            messages = conn.reader.feed(data)
        except FrameError as e:
            log(f"会话帧非法，断开该连接（uid={conn.peer.uid}）：{e}")
            self.send(conn, protocol.error(protocol.REASON_BAD_FRAME, detail=str(e)))
            self.drop(conn)
            return
        for msg in messages:
            if not conn.alive:
                break
            try:
                self.handler(conn, msg)
            except Exception as e:  # noqa: BLE001 - 一条帧炸掉不能拖垮整条会话
                log(f"会话帧处理异常（type={protocol.message_type(msg)}）：{e}")
        self._refresh_deadline(conn)

    # ---------- 发送 ----------

    def send(self, conn: Connection, message: Dict[str, Any]) -> bool:
        """把一帧放进该连接的出站队列并尽量写出。返回「是否已放进队列」。

        **不再「失败即丢弃该连接」**：``sendall`` 在非阻塞套接字上缓冲一满就抛，而
        那只是「客户端此刻没在读」（本机就是单线程，跑一轮任务时不 ``serve()``），
        不是「客户端坏了」—— 把它判成断开，就是「大帧一来就莫名其妙掉线」。
        真正的坏只剩 ``OSError``（对端没了）与队列积压超限（对端长时间不读）。
        """
        if not conn.alive:
            return False
        try:
            conn.out.queue(message)
        except (OSError, FrameError) as e:
            log(f"会话发送失败，断开该连接（uid={conn.peer.uid}）：{e}")
            self.drop(conn)
            return False
        return self._flush(conn)

    def _flush(self, conn: Connection) -> bool:
        """把该连接队列里的字节尽量推出去；推不完就等 ``selectors`` 报可写。"""
        if not conn.alive:
            return False
        try:
            done = conn.out.flush(conn.sock)
        except OSError as e:
            log(f"会话发送失败，断开该连接（uid={conn.peer.uid}）：{e}")
            self.drop(conn)
            return False
        self._arm_write(conn, not done)
        return True

    def _arm_write(self, conn: Connection, want: bool) -> None:
        """按需把这条连接的事件从「只读」扩成「读 ＋ 写」，或收回只有读。

        与 app 侧的写方向 ``QSocketNotifier`` 对称：**只在真有发不完的字节时**才订阅
        可写。一直订阅的话，套接字几乎永远可写 ⇒ ``select`` 每轮都立刻返回，
        整个主循环空转（「空闲零唤醒」当场作废）。
        """
        sel = self._selector
        if sel is None or want == conn.want_write:
            return
        events = selectors.EVENT_READ | (selectors.EVENT_WRITE if want else 0)
        try:
            sel.modify(conn.sock, events, ("conn", conn))
        except (KeyError, ValueError, OSError):  # pragma: no cover - 连接刚被摘掉
            return
        conn.want_write = want

    def push(self, subject: str, observed: Any) -> int:
        """把一份观测推给所有订阅了该 subject 的连接。返回送达条数。"""
        msg = protocol.event_push(subject, observed)
        sent = 0
        for conn in self.connections():
            if subject in conn.subscriptions and self.send(conn, msg):
                sent += 1
        return sent

    def drop(self, conn: Connection) -> None:
        """摘掉一条连接（幂等）。调用方负责在 ``on_close`` 里撤它的意图。"""
        if not conn.alive:
            return
        conn.alive = False
        # 队列属于这条连接：套接字马上关，剩下半截帧再发没有意义（新连接是新的帧流）。
        conn.out.clear()
        conn.want_write = False
        if self._selector is not None:
            try:
                self._selector.unregister(conn.sock)
            except (KeyError, ValueError, OSError):
                pass
        try:
            conn.sock.close()
        except OSError:  # pragma: no cover
            pass
        try:
            self._conns.remove(conn)
        except ValueError:  # pragma: no cover
            pass
        log(f"会话已断开：uid={conn.peer.uid} pid={conn.peer.pid}")
        if callable(self.on_close):
            try:
                self.on_close(conn)
            except Exception as e:  # noqa: BLE001 —— 收尾失败不该影响其余连接
                log(f"会话断开收尾异常：{e}")


__all__ = ["PeerInfo", "Connection", "SessionServer", "peer_credentials", "parse_xucred",
           "IDLE_READ_TIMEOUT_S", "MAX_SOCKET_PATH"]
