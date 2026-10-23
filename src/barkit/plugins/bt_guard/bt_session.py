"""bt_session：与随包助手 ``BTHelper.app`` 的那一条会话 —— **app 侧是服务端**。

它取代什么
----------
2026-09-29 的通道迁移把 app↔root helper 换成了「一条会话」（``capabilities/helper_link``
＋ ``capabilities/helper/session``），但蓝牙助手那条旁路整步推迟 —— 它此前仍是
「写 ``bt_request.json`` → ``open -n BTHelper.app`` → 轮询 ``bt_result.json``」
（方案 §8 第 4 步）。本模块就是那一步的落地：**同一份线格式，第二个端点。**

产物侧对应 ``plugins/bt_guard/resources/helper/bt-helper.m``（Objective-C 镜像）。
线格式的真源是 ``capabilities/helper/protocol.py``，两侧共用一份；跨语言的那一侧
（ObjC）只能在注释里点名它，改动必须同批。**这两个文件是同一份协议的两个实现，
改一个就要改另一个。**

方向与 root helper 那条会话相反（**这条里 app 是服务端**）
-------------------------------------------------------
root helper 是 LaunchDaemon 常驻、开机即 listen，app 去 connect。助手不是：它由
``open`` 按需拉起、干完就退 —— 拉起之前没有进程在 listen，所以只能反过来：
**app 先 listen，助手起来后 connect。**「谁 listen」不影响协议本身：助手发 ``hello``、
app 回 ``welcome``（握手用来对契约版本与身份），此后 app 发 ``op.run``、助手回 ``reply``。
这几个消息类型都是 ``protocol`` 里既有的，一个新类型都不用加。

一次动手 = 一条会话 = 一个助手进程
---------------------------------
``bt_state.restart_controller`` 要跑两条命令（``power-off`` → 停 2 秒 → ``power-on``），
旧形态下那是两次 ``open``、两次冷启动（实测冷启动 2 秒以上）。会话天然支持
「一条连接上多条请求/应答」，所以这里一次拉起、两条命令。

**断了要能重建**：``power-off`` 成功之后若会话断掉（助手崩了），直接把 ``power-on``
判失败会把用户的蓝牙留在关着的状态 —— 那是这条路上唯一真正危险的失败形态。故
:func:`run_commands` 在会话于两条命令之间断掉时**重建会话**，把剩下的命令跑完。

节点路径与权限（**安全约束，不是实现细节**）
--------------------------------------
节点放 ``paths.BT_HELPER_SOCKET``（用户私有数据目录，父目录 0700），bind 后 ``chmod 600``，
**用完即 unlink** —— 它不是常驻通道，稳态下磁盘上不该有它。accept 时另外校验对端 uid
（复用 ``capabilities/helper/session.peer_credentials``），与 root helper 那条同款：
够得着这个节点的进程集合 ＝ 能读该用户私有数据目录的进程集合，一条都不多。
"""

from __future__ import annotations

import itertools
import logging
import os
import socket
import stat
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from barkit import paths
from barkit.capabilities.helper import protocol
from barkit.capabilities.helper.protocol import FrameError, FrameReader
from barkit.capabilities.helper.session import MAX_SOCKET_PATH, peer_credentials

log = logging.getLogger(__name__)

#: 等助手连上来的上限（秒）。含 ``open`` 之后的 LaunchServices 注册 ＋ dyld 首次加载
#: ——实测冷启动可到 2 秒以上，预热后单程约 0.13 秒。宁慢勿误判。
CONNECT_WAIT_S = 20.0

#: 连上后等握手帧（``hello``）的上限（秒）。本地连上就该立刻发；给它只是防
#: 「连上了却不说话」把 worker 线程一直占着。
HELLO_WAIT_S = 5.0

#: 每条命令等回执的上限（秒）。助手侧 ``power-off`` / ``power-on`` 内部各自要等到
#: 状态达成（``waitPower``，上限 8 秒）才回话，故必须**宽于**那个数。
REPLY_WAIT_S = 20.0

#: 单帧发送的上限（秒）。帧很小（几十字节），给个期限只是防「对端不读」时无限阻塞。
SEND_WAIT_S = 5.0

#: app 在 ``welcome`` 里自陈的身份。协议里这一栏叫 ``helper_version``（root helper
#: 那条会话用它报守护版本）；这条会话的对端不是 root helper，故填一个能认出来的标识。
HELPER_LABEL = "bt-helper"

#: 同一条命令最多试几次（每次失败都**重建一条会话**）。
#:
#: 两条都要说清：
#: - **为什么要上限**：「没拿到回执就重试」在没有上限时是死循环（助手一直不回执 ⇒
#:   会话建了又建，而 ``done`` 一步都不前进），症状是 worker 线程永久卡住。
#: - **为什么敢重试**：助手那三条命令都是**幂等**的（``power-off`` / ``power-on``
#:   在状态已经对的时候直接回 ``already``），所以重跑一次不会造成第二次副作用。
#:   这一点是重试成立的前提，改助手时要一起看。
MAX_ATTEMPTS_PER_CMD = 2


class _Session:
    """一条已握手完毕的助手会话。"""

    __slots__ = ("sock", "reader", "pending")

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.reader = FrameReader()
        #: 已拼出、还没被消费的帧。``recv`` 一次可能拿到半条也可能拿到三条，
        #: 多出来的不能丢 —— 丢了就只能等下一次 recv，而对面可能再也不发。
        self.pending: List[Dict[str, Any]] = []


def _unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _listen(path: Path) -> Optional[socket.socket]:
    """建节点并开始监听。失败返回 ``None``（**不抛**：调用方按「会话起不来」处理）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 上一轮留下的节点不会自己消失，不删就 ``EADDRINUSE``。只删「确实是套接字」
        # 的那种；普通文件留在原地（宁可起不来也不误删数据）—— 与 root helper 那条同款。
        st = os.lstat(path)
        if stat.S_ISSOCK(st.st_mode):
            os.unlink(path)
    except OSError:
        pass
    if len(str(path).encode("utf-8")) > MAX_SOCKET_PATH:
        log.error("蓝牙助手会话节点路径过长（%d 字节 > %d）：%s",
                  len(str(path).encode("utf-8")), MAX_SOCKET_PATH, path)
        return None
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(path))
        # 属主与权限是安全约束（见模块 docstring）：节点必须只许本用户读写。
        os.chmod(str(path), 0o600)
        srv.listen(1)
    except OSError as e:
        log.warning("蓝牙助手会话节点建不出来（%s）：%s", path, e)
        srv.close()
        return None
    return srv


def _send(sock: socket.socket, message: Dict[str, Any]) -> bool:
    """发一帧。失败返回 ``False``（调用方按「会话废了」处理）。"""
    try:
        sock.settimeout(SEND_WAIT_S)
        sock.sendall(protocol.encode(message))
        return True
    except (OSError, FrameError) as e:
        log.warning("蓝牙助手会话发送失败：%s", e)
        return False


def _next_message(session: _Session, deadline: float) -> Optional[Dict[str, Any]]:
    """取下一帧；到 ``deadline`` 还没来返回 ``None``。

    先看已经拼出来的，再阻塞读 —— 一次 ``recv`` 可能一次给出好几帧。
    """
    while True:
        if session.pending:
            return session.pending.pop(0)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        session.sock.settimeout(remaining)
        try:
            data = session.sock.recv(65536)
        except (socket.timeout, TimeoutError):
            return None
        except OSError as e:
            log.debug("蓝牙助手会话读失败：%s", e)
            return None
        if not data:
            return None                     # EOF：助手没了
        try:
            session.pending.extend(session.reader.feed(data))
        except FrameError as e:
            log.warning("蓝牙助手会话收到非法帧：%s", e)
            return None


def _handshake(session: _Session, wait_s: float) -> bool:
    """助手一连上就发 ``hello``；这一层对契约版本，不符即拒。"""
    msg = _next_message(session, time.monotonic() + wait_s)
    if protocol.message_type(msg) != protocol.HELLO:
        log.warning("蓝牙助手握手失败：第一条帧不是 hello（%r）", msg)
        return False
    theirs = msg.get("protocol")
    if theirs != protocol.PROTOCOL_VERSION:
        # 说清是哪一侧旧 —— 这正是握手相对「读到未知字段整份拒」的好处。
        reason = "助手协议 %r 与 app 的 %r 不符（助手太旧或太新，重装可解决）" % (
            theirs, protocol.PROTOCOL_VERSION)
        _send(session.sock, protocol.welcome(
            ok=False, reason=reason, helper_version=HELPER_LABEL))
        log.error("蓝牙助手握手被拒：%s", reason)
        return False
    return _send(session.sock, protocol.welcome(
        ok=True, helper_version=HELPER_LABEL))


def _open_session(spawn: Callable[[], bool], path: Path, *,
                  connect_wait_s: float, hello_wait_s: float) -> Optional[_Session]:
    """建节点 → 拉起助手 → 收连接 → 握手。任何一步不成返回 ``None``。"""
    srv = _listen(path)
    if srv is None:
        _unlink(path)
        return None
    try:
        # **先 listen 再拉起**：反过来助手会连不上（它一起来就 connect，只重试几秒）。
        if not spawn():
            return None
        srv.settimeout(connect_wait_s)
        try:
            sock, _addr = srv.accept()
        except (socket.timeout, TimeoutError):
            log.warning("蓝牙助手没有在 %s 秒内连上来", connect_wait_s)
            return None
        except OSError as e:
            log.warning("蓝牙助手会话 accept 失败：%s", e)
            return None
    finally:
        try:
            srv.close()
        except OSError:  # pragma: no cover
            pass

    peer = peer_credentials(sock)
    if peer is None or peer.uid != os.getuid():
        # 读不到凭据就按「未知身份」拒 —— 绝不用「拿不到就等于允许」兜底。
        log.warning("拒绝一条不是本用户的助手连接：%r", peer)
        sock.close()
        return None

    session = _Session(sock)
    if not _handshake(session, hello_wait_s):
        sock.close()
        return None
    return session


def _close_session(session: Optional[_Session]) -> None:
    if session is None:
        return
    try:
        session.sock.close()
    except OSError:  # pragma: no cover
        pass


def _ask(session: _Session, op: str, req_id: str, wait_s: float) -> Optional[Dict[str, Any]]:
    """发一条命令、等它自己的回执。返回 ``reply.result``；断了/超时返回 ``None``。

    ``req_id`` 配对是**协议给的**（不是靠结果里自带的命令名去认领）—— 旧形态下那条
    「归属校验」的手工标记正是文件通道的补丁，此处不再需要。
    """
    if not _send(session.sock, protocol.op_run(op, None, req_id)):
        return None
    deadline = time.monotonic() + wait_s
    while True:
        msg = _next_message(session, deadline)
        if msg is None:
            log.warning("蓝牙助手没有在 %s 秒内给出 %r 的回执", wait_s, op)
            return None
        kind = protocol.message_type(msg)
        if kind == protocol.REPLY and str(msg.get("req_id") or "") == req_id:
            result = msg.get("result")
            return result if isinstance(result, dict) else {}
        if kind == protocol.ERROR:
            log.warning("蓝牙助手拒了一条命令（%s）：%s", msg.get("reason"),
                        msg.get("detail"))
            return None
        # 别的帧不该出现（这条会话只有请求/应答）；记一笔继续等，期限照旧不重置。
        log.debug("蓝牙助手发来一条与本次请求无关的帧：%r", msg)


def run_commands(
    cmds: Sequence[str],
    *,
    spawn: Callable[[], bool],
    gap_s: float = 0.0,
    socket_path: Optional[Path] = None,
    connect_wait_s: float = CONNECT_WAIT_S,
    hello_wait_s: float = HELLO_WAIT_S,
    reply_wait_s: float = REPLY_WAIT_S,
) -> List[Optional[Dict[str, Any]]]:
    """按顺序跑完 ``cmds``，返回与它**等长**的结果列表（起不来的那些是 ``None``）。

    ``spawn()`` 负责把助手拉起来（成功返回 ``True``）—— 由调用方注入，本模块不认识
    ``open``，也不认识 ``.app`` 在哪。

    ``gap_s``：两条命令之间的静默期（``power-off`` 与 ``power-on`` 之间要让蓝牙栈
    真的把控制器拆完）。**只在第 2 条起生效**，重试不算。

    **拿不到回执就会重开会话再试**（每条至多 :data:`MAX_ATTEMPTS_PER_CMD` 次）。这条
    不是为了好看 —— ``power-off`` 成功后会话断掉而 ``power-on`` 没跑，等于把用户的
    蓝牙留在关着的状态，而那正是这条路上唯一真正危险的失败形态。上限定成 2 是因为
    助手那三条命令幂等，重试无副作用；再往上加只会拖长最坏耗时。

    一旦某条命令用光了尝试次数，**跳过它继续跑下一条**（``restart_controller`` 需要的
    正是这个：``power-off`` 没成也要接着 ``power-on``）。
    """
    cmds = [str(c) for c in cmds]
    path = Path(socket_path) if socket_path is not None else paths.BT_HELPER_SOCKET
    out: List[Optional[Dict[str, Any]]] = [None] * len(cmds)
    ids = itertools.count(1)
    done = 0
    attempts = 0
    while done < len(cmds):
        session = _open_session(spawn, path, connect_wait_s=connect_wait_s,
                                hello_wait_s=hello_wait_s)
        if session is None:
            break                     # 会话建不起来：剩下的都发不出去，留 None
        try:
            while done < len(cmds):
                if done and attempts == 0 and gap_s:
                    time.sleep(gap_s)
                result = _ask(session, cmds[done], "%s-%d" % (os.getpid(), next(ids)),
                              reply_wait_s)
                attempts += 1
                if result is not None:
                    out[done] = result
                    done += 1
                    attempts = 0
                    continue
                if attempts >= MAX_ATTEMPTS_PER_CMD:
                    log.warning("蓝牙助手这一条没成（%s），试了 %d 次，跳过它继续",
                                cmds[done], attempts)
                    done += 1
                    attempts = 0
                    continue
                break                 # 换一条会话把这一条再跑一次
        finally:
            _close_session(session)
    _unlink(path)
    return out


def socket_path() -> Path:
    """会话节点的绝对路径（诊断文本与手工核对用）。"""
    return paths.BT_HELPER_SOCKET


__all__ = ["run_commands", "socket_path", "CONNECT_WAIT_S", "HELLO_WAIT_S",
           "REPLY_WAIT_S", "HELPER_LABEL", "MAX_ATTEMPTS_PER_CMD"]
