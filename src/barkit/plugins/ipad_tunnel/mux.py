"""usbmuxd 协议层：帧编解码、设备解析、建设备隧道、SOCKS5 握手
（纯标准库 iproxy 等价物，零依赖）。

``bridge.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import plistlib
import socket
import struct

from .consts import CONNECT_TIMEOUT, ConnectRefused, DeviceNotFound, DeviceUnknown, HANDSHAKE_TIMEOUT, MUX_CONNECT_TIMEOUT, MuxError, RESULT_BADDEV, RESULT_CONNREFUSED, SOCK_BUFSIZE, USBMUXD_SOCKET, _HEADER, _MSG_PLIST, _PENDING, _VERSION


def _frame(payload: bytes, message_type: int = _MSG_PLIST, tag: int = 1) -> bytes:
    return _HEADER.pack(16 + len(payload), _VERSION, message_type, tag) + payload


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("usbmuxd 连接意外关闭")
        buf += chunk
    return buf


def _recv_plist(sock: socket.socket) -> dict:
    header = _recv_exact(sock, _HEADER.size)
    total, _, _, _ = _HEADER.unpack(header)
    payload = _recv_exact(sock, total - _HEADER.size)
    return plistlib.loads(payload)


def list_devices() -> list:
    """向 usbmuxd 要设备列表。**传输层失败一律抛 ``MuxError``**（不抛「没设备」）。

    两种失败必须分开：``MuxError`` = 这条通道本身不通（请求超时 / 协议错），
    空列表 = 通道好、设备真不在。运行期的链路判据只认后者（见模块 docstring）。
    """
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(CONNECT_TIMEOUT)
    try:
        s.connect(USBMUXD_SOCKET)
        req = plistlib.dumps({
            "ClientVersionString": "barkit_bridge",
            "MessageType": "ListDevices",
            "ProgName": "barkit_bridge",
        })
        s.sendall(_frame(req))
        return _recv_plist(s).get("DeviceList", []) or []
    except (OSError, ConnectionError, struct.error, plistlib.InvalidFileException,
            ValueError) as e:
        raise MuxError(f"usbmuxd 请求失败：{e!r}") from e
    finally:
        s.close()


def pick_device(udid: str | None):
    """返回 (device_id, serial)。udid 为空时优先选 USB 设备。

    列表为空 / 找不到指定 UDID ⇒ ``DeviceNotFound``（真断，可据此拆机）；
    通道本身不通 ⇒ ``MuxError``（由 ``list_devices`` 抛出，调用方不得当链路故障）。
    """
    devices = list_devices()
    def conn_type(d):
        return d.get("Properties", {}).get("ConnectionType")
    usb = [d for d in devices if conn_type(d) == "USB"]
    candidates = usb or devices
    if not candidates:
        raise DeviceNotFound(
            "未发现任何已连接的 iOS 设备。\n"
            "  · 请确认 USB 数据线已插紧（部分充电线不支持数据传输）\n"
            "  · 在 iPad 上解锁屏幕，并点选弹出的「信任此电脑」\n"
            "  · 若已信任，尝试重新插拔 USB 线"
        )
    if udid:
        for d in candidates:
            props = d.get("Properties", {})
            if props.get("SerialNumber") == udid or props.get("UDID") == udid:
                return d["DeviceID"], udid
        serials = [d.get("Properties", {}).get("SerialNumber") for d in candidates]
        raise DeviceNotFound(f"未找到 UDID={udid} 的设备；当前可见：{serials}")
    d = candidates[0]
    serial = d.get("Properties", {}).get("SerialNumber")
    return d["DeviceID"], serial


def _connect_request(device_id: int, device_port: int) -> bytes:
    """usbmuxd 的 Connect 请求正文（plist）。"""
    port_net = ((device_port << 8) & 0xFF00) | (device_port >> 8)   # htons
    return plistlib.dumps({
        "ClientVersionString": "barkit_bridge",
        "MessageType": "Connect",
        "DeviceID": device_id,
        "PortNumber": port_net,
        "ProgName": "barkit_bridge",
    })


def _mux_connect(device_id: int, device_port: int, timeout: float) -> socket.socket:
    """连上 usbmuxd 并发出 Connect 请求；**不等应答**（应答交给调用方）。

    阻塞面只到「本地 unix socket 的 connect + sendall」——对活着的 usbmuxd 是即时的，
    真正的等待（设备端答应答）在调用方按截止时间轮询，绝不占着事件循环。
    """
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(USBMUXD_SOCKET)
        s.sendall(_frame(_connect_request(device_id, device_port)))
    except OSError as e:
        s.close()
        raise MuxError(f"usbmuxd 请求失败：{e!r}") from e
    return s


def _result_code(resp: dict) -> int | None:
    """取 Connect 应答的结果码；不是成功结构（缺 MessageType / Number 不可解析）返回 None。"""
    if resp.get("MessageType") != "Result":
        return None
    try:
        return int(resp.get("Number", -1))
    except (TypeError, ValueError):
        return None


def _connect_error(code: int, resp: dict) -> RuntimeError:
    """把结果码翻成**指名道姓**的异常 —— 都是 Connect 失败，但含义与处置不同。

    非零码里只有 ``RESULT_CONNREFUSED`` 与 ``RESULT_BADDEV`` 能指认段位（本机实测过），
    其余一律给普通 ``RuntimeError``：**说不出是哪一段就别硬指**（宁可让上层报「量不出来」）。
    """
    text = f"usbmuxd Connect 失败：{resp}"
    if code == RESULT_CONNREFUSED:
        return ConnectRefused(f"{text}（设备端拒绝：那个端口没在听）")
    if code == RESULT_BADDEV:
        return DeviceUnknown(f"{text}（设备号已失效：设备重新枚举过）")
    return RuntimeError(text)


def open_device_tunnel(device_id: int, device_port: int,
                       timeout: float = CONNECT_TIMEOUT) -> socket.socket:
    """（阻塞版）对 usbmuxd 发起 Connect 并等应答；成功返回设备端口的透明管道。

    调用方是**链路探针 / CLI**（本来就在自己的线程里等待）。中继的事件循环走
    ``begin_device_tunnel`` + ``parse_connect_reply`` 的非阻塞两段式（那条路只关心
    「成不成」，不需要种类，故仍返回一句字符串）。

    失败时按结果码抛 ``ConnectRefused`` / ``DeviceUnknown``（都是 ``RuntimeError`` 子类，
    既有的 ``except RuntimeError`` 一个都不用改）。
    """
    s = _mux_connect(device_id, device_port, timeout)
    try:
        resp = _recv_plist(s)
    except (OSError, ConnectionError, struct.error, plistlib.InvalidFileException,
            ValueError) as e:
        s.close()
        raise MuxError(f"usbmuxd 应答失败：{e!r}") from e
    code = _result_code(resp)
    if code is None or code != 0:
        s.close()
        raise _connect_error(-1 if code is None else code, resp)
    return s


def begin_device_tunnel(device_id: int, device_port: int) -> socket.socket:
    """非阻塞版第一步：发出 Connect 请求，返回**待应答**的 socket（已切非阻塞）。"""
    s = _mux_connect(device_id, device_port, MUX_CONNECT_TIMEOUT)
    s.setblocking(False)
    return s


def parse_connect_reply(buf: bytes):
    """解析 usbmuxd 的 Connect 应答（流式，可反复调用喂增量）。

    返回 ``(state, rest)``：``_PENDING`` = 还没收全；``None`` = 成功；
    ``str`` = 失败原因（设备端拒绝 / 应答非法）。
    """
    if len(buf) < _HEADER.size:
        return _PENDING, buf
    total, _, _, _ = _HEADER.unpack(buf[:_HEADER.size])
    if total < _HEADER.size or len(buf) < total:
        return _PENDING, buf
    payload, rest = buf[_HEADER.size:total], buf[total:]
    try:
        resp = plistlib.loads(payload)
    except (plistlib.InvalidFileException, ValueError) as e:
        return f"usbmuxd 应答无法解析：{e!r}", rest
    if resp.get("MessageType") != "Result" or resp.get("Number", -1) != 0:
        return f"usbmuxd Connect 失败：{resp}", rest
    return None, rest


def socks5_handshake(sock: socket.socket, timeout: float = HANDSHAKE_TIMEOUT) -> bool:
    """对设备端口做一次 SOCKS5「无认证」问候，读回应答；返回引擎是否正常应答。

    **端口能连上 ≠ 引擎会干活**：2026-09-20 的现场里 57 条会话全部卡在等这条应答
    （``socks5 client read auth``），所以准入判据必须验到这一层，而不是只验 Connect。
    """
    old = sock.gettimeout()
    try:
        sock.settimeout(timeout)
        sock.sendall(b"\x05\x01\x00")
        return _recv_exact(sock, 2) == b"\x05\x00"
    except (OSError, ConnectionError):
        return False
    finally:
        try:
            sock.settimeout(old)
        except OSError:
            pass


def tune_tcp(sock: socket.socket) -> None:
    """关 Nagle、放大内核缓冲区（Unix 域套接字不支持这些选项，忽略即可）。"""
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    for opt in (socket.SO_SNDBUF, socket.SO_RCVBUF):
        try:
            sock.setsockopt(socket.SOL_SOCKET, opt, SOCK_BUFSIZE)
        except OSError:
            pass


class DeviceResolver:
    """缓存 DeviceID，并在设备消失/更换后自动重新解析。

    无锁：所有调用点都在**同一条中继线程**内（``Bridge._accept`` 走 get/invalidate；
    链路探针直接调模块函数 ``pick_device``，不碰解析器）。旧实现的 ``threading.Lock``
    是防御性的，随「插件零时钟/零线程」门禁一并去除；若未来出现第二个调用线程，
    必须在 resident 宿主侧串行化，而不是把锁加回来。
    """

    def __init__(self, udid: str | None):
        self._udid = udid
        self._device_id: int | None = None
        self._serial: str | None = None

    def get(self) -> int:
        if self._device_id is None:
            self._device_id, self._serial = pick_device(self._udid)
        return self._device_id

    def invalidate(self) -> None:
        self._device_id = None

    @property
    def serial(self):
        return getattr(self, "_serial", None)


class Tunnel:
    """一条桥接连接：本机 TCP socket <-> usbmuxd 设备管道（双向独立缓冲，互不阻塞）。"""

    __slots__ = ("local", "dev", "buf_ld", "buf_dl",
                 "eof_local", "eof_dev", "flushed_ld", "flushed_dl", "moved")

    def __init__(self, local: socket.socket, dev: socket.socket):
        self.local = local
        self.dev = dev
        self.buf_ld = b""
        self.buf_dl = b""
        self.eof_local = False
        self.eof_dev = False
        self.flushed_ld = False
        self.flushed_dl = False
        #: 是否搬运过至少一个字节（会话统计口径：**零字节 = 这条链路没干活**）
        self.moved = 0

    @property
    def finished(self) -> bool:
        return (self.eof_local and not self.buf_ld
                and self.eof_dev and not self.buf_dl)


class _Pending:
    """一条「已受理、正等 usbmuxd 答应答」的连接（非阻塞两段式的第一段）。"""

    __slots__ = ("local", "dev", "deadline", "buf")

    def __init__(self, local: socket.socket, dev: socket.socket, deadline: float):
        self.local = local
        self.dev = dev
        self.deadline = deadline
        self.buf = b""
