"""protocol：app 与 root helper 之间那条会话的**线格式**（两侧共用，只用标准库）。

为什么两侧共用一份
----------------
app 侧跑在 PySide6 里、helper 侧跑在系统自带的 Python 3.9 上（plist 里是
``/usr/bin/python3``）。共用一份线格式是「协议只有一处真源」的前提 —— 两侧各写
一份编解码，涨的第一个字段就会对不上。

**因此本模块只许用标准库，且必须兼容 3.9**：不用 ``match``、不用运行期求值的
``X | Y`` 注解（``from __future__ import annotations`` 已开）、不用 3.10+ 的
``typing`` 别名写法。

传输形态（2026-09-29 本机实测定案）
-----------------------------------
- ``AF_UNIX`` + ``SOCK_STREAM``。**Darwin 不支持 ``SOCK_SEQPACKET``**：常量存在，
  但 ``socketpair(AF_UNIX, SOCK_SEQPACKET)`` 直接 ``ENOPROTOOPT``（实测
  ``OSError [Errno 43] Protocol not supported``）。故消息边界得自己画。
- 分帧：**4 字节大端长度前缀 + UTF-8 JSON 正文**。单帧上限 1 MiB（沿用旧的参数
  文件读取上限）。
- **分帧的「两半」都得有**：读的一半是 :class:`FrameReader`（拼帧），写的一半是
  :class:`FrameWriter`（允许部分写）。``SOCK_STREAM`` 上两半都不成立 —— 只补读的那半、
  把写假定成「一次 ``sendall`` 发完」，就会在缓冲一满时把「对端忙」误判成「对端坏」。
- **禁用 pickle / 任何可执行载荷**：root 侧解析的是不受信字节，JSON 是唯一入口
  （与「root 解析不受信 JSON」逐字同源，不新增面）。

消息类型（四件事的载体）
-----------------------
``hello`` / ``welcome``（握手：协议版本 + 身份 + 能力）
``intent.set``  —— 声明「我要什么」（幂等；``rev`` = 内容哈希；``takeover`` = 接管
                   别人正在写的那个 subject，不带即被拒 —— 一个 subject 一个写者）
``op.run``      —— 「现在做一次」，帧级回执（``req_id`` 配对）
``subscribe``   —— 订阅某几个 subject 的最新事实（**订阅即立刻推一份当前的**）
``unsubscribe`` —— 撤回订阅（组件被禁用 / 卸载时框架调；漏这一条会让守护白白推送）
``get``         —— 现问一次快照
``event.push``  —— 服务端 → 客户端：状态变化
``reply``       —— 服务端 → 客户端：``op.run`` / ``get`` 的应答
``error``       —— 服务端 → 客户端：这条帧本身被拒（含原因）
"""

from __future__ import annotations

import hashlib
import json
import socket
import struct
from typing import Any, Dict, List, Optional

#: 协议版本。两侧不符即拒绝握手（比旧「读到未知字段整份拒」精确：能说清谁旧）。
PROTOCOL_VERSION = 1

#: 单帧上限（1 MiB）。超限即判非法帧并断开该连接 —— 帧长前缀来自不受信的一侧，
#: 不设上限等于让对面用 4 个字节要我们分配 4 GiB。
MAX_FRAME = 1 << 20

#: 出站队列的积压上限。``SOCK_STREAM`` 上「一次写完」不成立（对端可能正忙、不读），
#: 发不完的字节要攒着 —— 但攒个没完就成了无界缓冲。超过这个量即判「对端僵死」，
#: 由调用方断开：与「读不出就不动手」同一个取向，宁可断，不无限等。
MAX_OUTBOX = 4 * MAX_FRAME

#: 发送缓冲的目标值（**优化项，不是正确性前提**）。
#: macOS 的 ``AF_UNIX`` 默认 ``SO_SNDBUF`` 只有 8192 字节（Linux 是 212992），而单帧
#: 上限是 1 MiB ⇒ 默认值下稍大的帧必然要分次写。分次写已由 :class:`FrameWriter` 兜住，
#: 把它调大只是让多数帧一次发完、根本不进队列。
#: ⚠️ **不校验、不落盘、不进配置**：能不能设上去取决于内核（macOS 对 ``AF_UNIX``
#: 的历史行为并不一致，且 macOS 上只有发送侧这个数算、``SO_RCVBUF`` 无效），
#: 设不上也不影响送达。
SEND_BUFFER_BYTES = 2 * MAX_FRAME

#: 长度前缀本身：4 字节无符号大端。
_HEADER = struct.Struct(">I")
HEADER_SIZE = _HEADER.size

# ── 消息类型 ──
HELLO = "hello"
WELCOME = "welcome"
INTENT_SET = "intent.set"
OP_RUN = "op.run"
SUBSCRIBE = "subscribe"
UNSUBSCRIBE = "unsubscribe"
GET = "get"
EVENT_PUSH = "event.push"
REPLY = "reply"
ERROR = "error"

#: 客户端 → 服务端（要求 ``req_id`` 的除外）
CLIENT_TYPES = (HELLO, INTENT_SET, OP_RUN, SUBSCRIBE, UNSUBSCRIBE, GET)
#: 服务端 → 客户端
SERVER_TYPES = (WELCOME, EVENT_PUSH, REPLY, ERROR)
#: 需要 ``req_id`` 并在回帧里原样带回的类型
REQUEST_TYPES = (OP_RUN, GET)

#: 未知消息类型 / 非法帧的统一错误码（写进 ``error`` 帧的 ``reason``）。
#: 旧契约里对应的是「读到未知字段整份拒绝」——**那条约定随文件通道一起消失**：
#: 帧级协议下，一个不认识的类型只该拒**那一帧**，不该把整条会话打死。
REASON_BAD_FRAME = "bad_frame"
REASON_BAD_TYPE = "unknown_type"
REASON_BAD_REQUEST = "bad_request"
REASON_PROTOCOL = "protocol_mismatch"
REASON_PEER = "peer_rejected"
#: 一个 subject **只允许一个写者**（§9）：第二个写者不带 ``takeover`` 即被拒。
#: 不拒就会出现「两个客户端互相覆盖」——谁的最后一帧算数全看谁后发。
REASON_CLAIMED = "already_claimed"
#: 回给**原写者**的告知：它名下那个 subject 的写者身份已被另一条连接接管。
#: 它不是「你的帧被拒了」，是「你不再是写者了」—— 不告知的话原写者会一直以为
#: 自己还在管着这件事（它下一帧才会发现自己被拒，而那可能要几小时之后）。
REASON_TAKEOVER = "taken_over"
#: **还没握手就发业务帧**。握手是唯一一次对契约版本的地方，没握手的连接版本
#: 未知 —— 按本版协议去解析它发来的 payload，等于默认「它说的形状我认得」。
REASON_NO_HANDSHAKE = "no_handshake"

#: 「某个 subject 的期望态**没有生效**」这一类拒绝的原因。
#:
#: 只有这两条会带 ``subject`` —— 它们说的是「这一件事没成」；其余原因（帧非法 /
#: 类型未知 / 没握手）说的是**通道本身**，不落在任何一个 subject 上。分开的意义在
#: app 侧：前者的后果是「组件声明了期望态、守护不认」，是**静默失效**；后者是传输
#: 层故障，本来就有别的出口在报。
INTENT_REJECT_REASONS = (REASON_CLAIMED, REASON_TAKEOVER)


class FrameError(Exception):
    """线格式层面的失败：帧超长、长度前缀与正文不符、正文不是 JSON 对象。"""


def canonical(payload: Any) -> str:
    """稳定序列化 —— ``rev`` 的算法基底（两侧必须逐字相同）。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=str)


def payload_rev(payload: Any) -> str:
    """意图正文的内容哈希。

    这是「重连重发天然收敛」的全部依据：客户端重连后无条件重发全部意图，
    服务端拿 ``rev`` 与内存里那份比对 —— 相同即空操作。**不需要读盘比对**，
    也不需要「我上次写了什么」这种边沿触发的参照物。
    """
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()[:32]


def encode(message: Dict[str, Any]) -> bytes:
    """把一条消息编成帧（长度前缀 + 正文）。"""
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise FrameError("帧超长：%d > %d" % (len(body), MAX_FRAME))
    return _HEADER.pack(len(body)) + body


def widen_send_buffer(sock) -> None:
    """尽力把发送缓冲调大到 :data:`SEND_BUFFER_BYTES`。**永不抛**。

    *优化，不是正确性前提*：调不上去（内核不支持 / 被钳）只是意味着更大的帧要走
    :class:`FrameWriter` 的分次写路径，送达性一个字都不差。故这里**不校验结果、
    不落盘、不进配置** —— 也不该有人把它当「必须成功」来检查。
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SEND_BUFFER_BYTES)
    except OSError:
        pass        # 平台差异；分次发送兜住


class FrameWriter:
    """出站帧队列：**允许部分写**。

    为什么必须有它：``sendall`` 在非阻塞套接字上**不等待** —— 内核缓冲一满就抛
    ``BlockingIOError``（实测：把发送缓冲灌满后 ``sendall(b"x")`` 立刻抛）。而
    ``SOCK_STREAM`` 上「一次写完」本来就是不成立的假设：读的那一半本仓已经补了
    （:class:`FrameReader` 老老实实拼帧），写的那一半不能假定成原子。

    攒下的字节由调用方在「可写」时再调 :meth:`flush`。这样**与缓冲多大无关**：
    合法帧（``MAX_FRAME`` 以内）一定送得出去，而 :meth:`flush` 抛 ``OSError`` 只剩
    「对面真坏了」一种含义 —— ``BlockingIOError`` 不再被误判成断开。
    """

    __slots__ = ("_buf", "_sent")

    def __init__(self) -> None:
        self._buf = bytearray()
        self._sent = 0

    @property
    def pending(self) -> int:
        """还没写出去的字节数（0 ＝ 队列空）。"""
        return len(self._buf) - self._sent

    def queue(self, message: Dict[str, Any]) -> None:
        """编一帧并入队。帧超长抛 ``FrameError``；积压超上限抛 ``OSError``。

        两者都由调用方判「断开」—— 区别只在日志说的是哪一件事。
        """
        data = encode(message)
        if self.pending + len(data) > MAX_OUTBOX:
            raise OSError("出站队列积压 %d 字节（上限 %d）：对端长时间不读"
                          % (self.pending + len(data), MAX_OUTBOX))
        self._buf.extend(data)

    def flush(self, sock) -> bool:
        """尽量把队里的字节写出去；返回**是否已发完**。

        ``BlockingIOError``（对端此刻没在读）不是错误：返回 ``False``，队列留着，
        调用方等「可写」再来。真出错才抛 ``OSError``。

        自己记「已发多少字节」而不是用 ``sendall``：后者抛异常时**不告诉你已经
        发出去多少**，没法接着发 —— 这正是这里必须自建偏移的原因。
        """
        if self._sent:
            # 上一轮已写出去的前缀不再需要：先丢掉，让队列内存严格受 ``MAX_OUTBOX``
            # 约束（否则「已发出但没清掉」的那截会叠在积压之上）。偏移随之归零。
            del self._buf[:self._sent]
            self._sent = 0
        view = memoryview(self._buf)
        try:
            while self._sent < len(view):
                try:
                    n = sock.send(view[self._sent:])
                except (BlockingIOError, InterruptedError):
                    return False
                if n <= 0:
                    raise OSError("套接字写返回 %d" % n)
                self._sent += n
        finally:
            view.release()      # 不 release 就改不了 _buf（bytearray 被导出了）
        self._buf.clear()
        self._sent = 0
        return True

    def clear(self) -> None:
        """丢掉队里全部字节（连接断开时调）。"""
        self._buf.clear()
        self._sent = 0


class FrameReader:
    """流式分帧器：喂字节、吐消息（0 到多条）。

    为什么不直接 ``recv``：``SOCK_STREAM`` 一次 ``recv`` 可能拿到半条帧、也可能
    拿到三条半。分帧器把「攒够一帧」这件事收在一处，调用方只面对消息。
    """

    __slots__ = ("_buf",)

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> List[Dict[str, Any]]:
        """喂入刚从套接字读到的一段字节，返回其中**已经完整**的消息。

        ``data`` 为空字节串＝对端关闭（EOF），返回空列表；EOF 的判定归调用方。
        """
        if data:
            self._buf.extend(data)
        out: List[Dict[str, Any]] = []
        while True:
            if len(self._buf) < HEADER_SIZE:
                break
            (length,) = _HEADER.unpack_from(self._buf, 0)
            if length > MAX_FRAME:
                raise FrameError("帧长前缀超限：%d" % length)
            if len(self._buf) < HEADER_SIZE + length:
                break
            body = bytes(self._buf[HEADER_SIZE:HEADER_SIZE + length])
            del self._buf[:HEADER_SIZE + length]
            out.append(_decode_body(body))
        return out

    def pending(self) -> int:
        """缓冲区里还剩多少字节（半条帧）。"""
        return len(self._buf)


def _decode_body(body: bytes) -> Dict[str, Any]:
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise FrameError("正文不是合法 JSON：%s" % e)
    except RecursionError as e:
        # CPython 的 json 解码器对**深嵌套**正文抛的是 RecursionError（RuntimeError 的
        # 子类），既不是 ValueError 也不是 FrameError —— 不接住它，一帧 1 MiB 的
        # ``[[[[…]]]]`` 就能让守护进程崩溃退出，launchd 随即重新拉起 ⇒ 崩溃循环。
        # 帧层面的失败必须一律收敛成 FrameError（只关这一条连接）。
        raise FrameError("正文嵌套过深，拒绝解析：%s" % e)
    if not isinstance(obj, dict):
        raise FrameError("正文顶层不是对象：%r" % type(obj).__name__)
    return obj


def message_type(message: Any) -> Optional[str]:
    """取一条消息的类型名；不是对象 / 没类型 / 类型不是字符串 ⇒ ``None``。"""
    if not isinstance(message, dict):
        return None
    t = message.get("type")
    return t if isinstance(t, str) and t else None


# ──────────────────────────────────────────────
# 构造器（两侧都用它，杜绝手拼字段）
# ──────────────────────────────────────────────

def hello(client: str = "", version: int = PROTOCOL_VERSION) -> Dict[str, Any]:
    """客户端握手：自报门面与我方协议版本。"""
    return {"type": HELLO, "protocol": int(version), "client": str(client)}


def welcome(*, ok: bool, reason: str = "", helper_version: str = "",
            protocol: int = PROTOCOL_VERSION,
            subjects: Optional[List[str]] = None) -> Dict[str, Any]:
    """服务端握手应答。``ok=False`` 时 ``reason`` 必须说清是哪一侧旧。"""
    return {
        "type": WELCOME,
        "ok": bool(ok),
        "reason": str(reason),
        "protocol": int(protocol),
        "helper_version": str(helper_version),
        "subjects": list(subjects or []),
    }


def intent_set(subject: str, payload: Any, *, rev: Optional[str] = None,
               takeover: bool = False) -> Dict[str, Any]:
    """声明某 subject 的期望态。``payload=None`` ＝ 撤回该 subject 的意图。

    ``takeover=True`` ＝ **显式接管**：本 subject 已经有别的连接在写时，只有带这个
    位才允许接手（否则守护回 ``already_claimed``）。这是「一个 subject 只允许一个
    写者」这条规则里「要么被拒、要么显式接管」的后半 —— 前半（拒）是默认行为，
    后半必须写在帧上，不接受「覆盖一下应该没事」这种默契。
    """
    msg: Dict[str, Any] = {
        "type": INTENT_SET,
        "subject": str(subject),
        "rev": rev or payload_rev(payload),
        "payload": payload,
    }
    if takeover:
        msg["takeover"] = True
    return msg


def op_run(op: str, params: Any, req_id: str) -> Dict[str, Any]:
    """一次性命令。``req_id`` 由客户端生成，回帧原样带回。"""
    return {"type": OP_RUN, "op": str(op), "params": params, "req_id": str(req_id)}


def subscribe(subjects: List[str], req_id: Optional[str] = None) -> Dict[str, Any]:
    """订阅：服务端收到后**立刻**把这些 subject 的当前事实推一份，此后有变化再推。"""
    msg: Dict[str, Any] = {"type": SUBSCRIBE, "subjects": [str(s) for s in subjects]}
    if req_id is not None:
        msg["req_id"] = str(req_id)
    return msg


def unsubscribe(subjects: List[str]) -> Dict[str, Any]:
    """撤回订阅（组件禁用 / 卸载时调）。服务端此后不再往这条连接推这几个 subject。

    与 ``subscribe`` 对称、**不含 ``req_id``**：它不是请求-应答，是一条单向声明；
    重复撤回是幂等的，不需要回执来配对。
    """
    return {"type": UNSUBSCRIBE, "subjects": [str(s) for s in subjects]}


def get(subject: str, req_id: str) -> Dict[str, Any]:
    """现问一次快照（面板每次打开、诊断每次抓取都走它）。"""
    return {"type": GET, "subject": str(subject), "req_id": str(req_id)}


def event_push(subject: str, observed: Any) -> Dict[str, Any]:
    """服务端推送：某 subject 的最新观测。"""
    return {"type": EVENT_PUSH, "subject": str(subject), "observed": observed}


def reply(req_id: str, *, ok: bool, result: Any = None,
          error: str = "") -> Dict[str, Any]:
    """``op.run`` / ``get`` 的应答。失败**当场可见**（旧形态下回执写进状态文件、
    配对只能靠 ``seq`` 水位线，而水位线活不过进程重启）。"""
    return {"type": REPLY, "req_id": str(req_id), "ok": bool(ok),
            "result": result, "error": str(error)}


def error(reason: str, *, detail: str = "", req_id: Optional[str] = None,
          subject: Optional[str] = None) -> Dict[str, Any]:
    """拒收**这一帧**（不打死整条会话）。

    ``subject`` 只在「这一帧说的是某个 subject 的期望态」时带上，即 :data:
    `INTENT_REJECT_REASONS` 那两条。没有它，app 侧收到拒绝只知道「有一帧被拒」，
    不知道**哪一个 subject 的期望态没生效** —— 意图帧不带 ``req_id``，无从配对。
    旧版守护不发这一栏，读到空即按「无法归因」处理（降级，不误判）。
    """
    msg: Dict[str, Any] = {"type": ERROR, "reason": str(reason), "detail": str(detail)}
    if req_id is not None:
        msg["req_id"] = str(req_id)
    if subject is not None:
        msg["subject"] = str(subject)
    return msg


__all__ = [
    "PROTOCOL_VERSION", "MAX_FRAME", "HEADER_SIZE", "MAX_OUTBOX", "SEND_BUFFER_BYTES",
    "HELLO", "WELCOME", "INTENT_SET", "OP_RUN", "SUBSCRIBE", "UNSUBSCRIBE", "GET",
    "EVENT_PUSH", "REPLY", "ERROR",
    "CLIENT_TYPES", "SERVER_TYPES", "REQUEST_TYPES",
    "REASON_BAD_FRAME", "REASON_BAD_TYPE", "REASON_BAD_REQUEST",
    "REASON_PROTOCOL", "REASON_PEER", "REASON_CLAIMED", "REASON_TAKEOVER",
    "REASON_NO_HANDSHAKE", "INTENT_REJECT_REASONS",
    "FrameError", "FrameReader", "FrameWriter", "canonical", "payload_rev",
    "encode", "message_type", "widen_send_buffer",
    "hello", "welcome", "intent_set", "op_run", "subscribe", "unsubscribe", "get",
    "event_push", "reply", "error",
]
