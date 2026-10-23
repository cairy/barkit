"""audio_guard.audio_state：本机音频状态的只读探针（macOS）。

**为什么直接和 CoreAudio / IORegistry 打交道**：要回答的问题（「耳机插着吗」「麦克风被
认出来了吗」「声音现在走哪」「是不是静音了」）没有任何一个系统命令能给全，而拉起
``system_profiler`` 之类子进程又慢又难解析。这里用 ``ctypes`` 直接调两个框架的公开 API：

- **CoreAudio**：枚举设备、读写默认设备、读输出数据源。全部是公开接口、**普通用户权限**；
- **IORegistry**：读音频编解码器（codec）自己报的插孔检测结果 ——
  ``HPDetect``（认出耳机）/ ``MicDetect``（认出麦克风）/ ``DetectConfidence``（这次检测有没有跑完）。

**为什么要读 codec 而不只看设备列表**：插孔识别是**分几步做**的（实测：约 0.4s 时先认出
耳机、约 1.7s 才确认麦克风）。只认出一半时，系统里就**没有**「外置麦克风」这个设备 ——
于是「设备不在」既可能是「没插耳机」，也可能是「插了但这次没认全」。这两个处境该做的事
完全不同（前者什么都不用做，后者要拔插），所以必须读 codec 的结论来区分。设备列表给不出
这个信息。

**降级**：非 macOS、或本机 codec 不报这些属性（Intel 机型的 AppleHDA 就不报同名字段）时，
相关字段为 ``None``，检测照跑、只是判据少几条 —— 不抛异常、不拦启动。

全部是**只读**的，唯一的写操作是 ``set_default_input()``（切换默认输入设备）。
"""

from __future__ import annotations

import ctypes
import logging
import struct
import sys
from dataclasses import dataclass, field

from barkit.core.log_throttle import log_throttled

log = logging.getLogger(__name__)

#: 这几个 UID 是 macOS 为内建音频端点分配的**固定字符串**（不随会话变），
#: 所以判据可以按 UID 认，而不是按名字（名字会随系统语言变）或按设备 ID
#: （实测：同一进程内稳定，但跨会话 / 音频服务重启后会变）。
UID_HEADSET_OUT = "BuiltInHeadphoneOutputDevice"
UID_HEADSET_IN = "BuiltInHeadphoneInputDevice"
UID_SPEAKER = "BuiltInSpeakerDevice"
UID_INTERNAL_MIC = "BuiltInMicrophoneDevice"

#: 输出设备的数据源标识（实测取值）：耳机孔 / 内置扬声器。
SOURCE_HEADPHONES = "hdpn"
SOURCE_INTERNAL_SPEAKER = "ispk"

# CoreAudio 选择器（'xxxx' 的 big-endian 整数）
_SEL_DEVICES = 0x64657623          # 'dev#'
_SEL_DEFAULT_IN = 0x64496E20       # 'dIn '
_SEL_DEFAULT_OUT = 0x644F7574      # 'dOut'
_SEL_NAME = 0x6C6E616D             # 'lnam'
_SEL_UID = 0x75696420              # 'uid '
_SEL_SOURCE = 0x73737263           # 'ssrc'
_SEL_VOLUME = 0x766F6C6D           # 'volm' (master, element 0)
_SEL_MUTE = 0x6D757465             # 'mute'

_SCOPE_GLOBAL = 0                  # 'glob'
_SCOPE_INPUT = 0x696E7074          # 'inpt'（**不是** 'inpu'）
_SCOPE_OUTPUT = 0x6F757470         # 'outp'
_ELEMENT_MAIN = 0

_SYSTEM_OBJECT = 1

#: codec 服务的候选类名。Apple Silicon 上是独立编解码器驱动；Intel 机型是 AppleHDA。
#: 找不到就退化为「没有 codec 判据」，检测仍按 CoreAudio 那几条跑。
_CODEC_CLASSES = (b"AppleCS42L84Audio", b"AppleCS42L83Audio", b"AppleHDA")


def available() -> bool:
    """本平台有没有这套探针（非 macOS 一律没有）。"""
    return sys.platform == "darwin"


class _Addr(ctypes.Structure):
    _fields_ = [("sel", ctypes.c_uint32), ("scope", ctypes.c_uint32),
                ("elem", ctypes.c_uint32)]


class _Framework:
    """两个框架的懒加载包装：构造失败即整体不可用（``ready`` 为假）。"""

    def __init__(self) -> None:
        self.ready = False
        self.reason = ""
        if not available():
            self.reason = "非 macOS"
            return
        try:
            self.ca = ctypes.CDLL(
                "/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
            self.cf = ctypes.CDLL(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            self.iok = ctypes.CDLL(
                "/System/Library/Frameworks/IOKit.framework/IOKit", use_errno=True)
        except OSError as exc:                       # 框架缺失（异常环境）
            self.reason = f"加载框架失败：{exc}"
            return
        self._declare_signatures()
        self.ready = True

    def _declare_signatures(self) -> None:
        """声明用到的签名。

        ctypes 默认把返回值当 ``int``，而这里好几个接口返回的是 **64 位指针**
        —— 不声明就会被截断成 32 位（在真机上表现为「偶尔读到垃圾」或直接崩），
        所以每一个都必须显式写清楚。
        """
        ca, cf, iok = self.ca, self.cf, self.iok
        addr_p = ctypes.POINTER(_Addr)
        ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
        ca.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32, addr_p, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        ca.AudioObjectSetPropertyData.restype = ctypes.c_int32
        ca.AudioObjectSetPropertyData.argtypes = [
            ctypes.c_uint32, addr_p, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_void_p]
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                                 ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                          ctypes.c_size_t, ctypes.c_uint32]
        cf.CFGetTypeID.restype = ctypes.c_ulong
        cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
        cf.CFBooleanGetTypeID.restype = ctypes.c_ulong
        cf.CFBooleanGetValue.restype = ctypes.c_bool
        cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
        cf.CFStringGetTypeID.restype = ctypes.c_ulong
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        iok.IOServiceMatching.restype = ctypes.c_void_p
        iok.IOServiceMatching.argtypes = [ctypes.c_char_p]
        iok.IOServiceGetMatchingService.restype = ctypes.c_uint32
        iok.IOServiceGetMatchingService.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        iok.IORegistryEntryCreateCFProperty.restype = ctypes.c_void_p
        iok.IORegistryEntryCreateCFProperty.argtypes = [
            ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
        iok.IOObjectRelease.argtypes = [ctypes.c_uint32]


_FW: _Framework | None = None


def _fw() -> _Framework:
    global _FW
    if _FW is None:
        _FW = _Framework()
    return _FW


@dataclass(frozen=True)
class Device:
    """一个 CoreAudio 设备（只留判据用得上的字段）。"""

    dev_id: int
    uid: str
    name: str
    has_input: bool
    has_output: bool


@dataclass(frozen=True)
class CodecState:
    """音频编解码器自报的插孔检测结果。"""

    available: bool = False
    hp_detect: bool | None = None        # 认出耳机了吗
    mic_detect: bool | None = None       # 认出麦克风了吗
    confidence: str = ""                 # 这次检测跑到哪一步（CompleteFinal = 跑完）


@dataclass(frozen=True)
class Snapshot:
    """一次体检的完整结果（**只读**，不含任何判断，判断归插件）。"""

    ok: bool = False
    reason: str = ""
    devices: tuple[Device, ...] = ()
    default_input_uid: str | None = None
    default_output_uid: str | None = None
    output_source: str = ""              # 默认输出设备的数据源（hdpn / ispk）
    codec: CodecState = field(default_factory=CodecState)
    output_muted: bool | None = None
    output_volume: float | None = None

    def find(self, uid: str) -> Device | None:
        for d in self.devices:
            if d.uid == uid:
                return d
        return None

    def uids(self) -> set[str]:
        return {d.uid for d in self.devices}


# ---------- CoreAudio 读取 ----------


def _get(fw: _Framework, oid: int, sel: int, size: int = 64, scope: int = 0,
         elem: int = 0) -> tuple[int, bytes]:
    addr = _Addr(sel, scope, elem)
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_uint32(size)
    rc = fw.ca.AudioObjectGetPropertyData(oid, ctypes.byref(addr), 0, None,
                                          ctypes.byref(n), buf)
    return rc, buf.raw[: n.value]


def _cf_string(fw: _Framework, oid: int, sel: int) -> str:
    rc, raw = _get(fw, oid, sel, 8)
    if rc != 0 or len(raw) < 8:
        return ""
    ptr = ctypes.c_void_p.from_buffer_copy(raw).value
    if not ptr:
        return ""
    buf = ctypes.create_string_buffer(512)
    fw.cf.CFStringGetCString(ptr, buf, 512, 0x08000100)
    fw.cf.CFRelease(ptr)
    return buf.value.decode("utf-8", "replace")


def _device_ids(fw: _Framework) -> list[int]:
    rc, raw = _get(fw, _SYSTEM_OBJECT, _SEL_DEVICES, 4096)
    if rc != 0 or len(raw) < 4:
        return []
    return list(struct.unpack(f"<{len(raw) // 4}I", raw))


def _has_channels(fw: _Framework, oid: int, scope: int) -> bool:
    """某方向是否有流（设备既可能是纯输出，也可能是复合设备）。

    用 ``StreamConfiguration`` 的 buffer 数判断，比猜名字可靠（名字随语言变）。
    """
    rc, raw = _get(fw, oid, 0x736C6179, 256, scope=scope)   # 'slay'
    if rc != 0 or len(raw) < 4:
        return False
    nbuf = struct.unpack("<I", raw[:4])[0]
    return nbuf > 0


def _uint32_prop(fw: _Framework, oid: int, sel: int, scope: int = 0,
                 elem: int = 0) -> int | None:
    rc, raw = _get(fw, oid, sel, 4, scope=scope, elem=elem)
    if rc != 0 or len(raw) != 4:
        return None
    return struct.unpack("<I", raw)[0]


def _float32_prop(fw: _Framework, oid: int, sel: int, scope: int = 0,
                  elem: int = 0) -> float | None:
    rc, raw = _get(fw, oid, sel, 4, scope=scope, elem=elem)
    if rc != 0 or len(raw) != 4:
        return None
    return struct.unpack("<f", raw)[0]


def _fourcc(value: int) -> str:
    try:
        return struct.pack(">I", value).decode("ascii")
    except (UnicodeDecodeError, struct.error):
        return ""


# ---------- IORegistry 读取（codec 检测结果）----------


def _codec_service(fw: _Framework) -> int:
    for cls in _CODEC_CLASSES:
        try:
            svc = fw.iok.IOServiceGetMatchingService(0, fw.iok.IOServiceMatching(cls))
        except Exception:  # noqa: BLE001
            continue
        if svc:
            return svc
    return 0


def _read_cf(fw: _Framework, service: int, key: str):
    """按 CFType 分派读取（**不要**用 CFCopyDescription —— 对 CFBoolean 会崩）。"""
    k = fw.cf.CFStringCreateWithCString(None, key.encode(), 0x08000100)
    try:
        ref = fw.iok.IORegistryEntryCreateCFProperty(service, k, None, 0)
    finally:
        fw.cf.CFRelease(k)
    if not ref:
        return None
    try:
        tid = fw.cf.CFGetTypeID(ref)
        if tid == fw.cf.CFBooleanGetTypeID():
            return bool(fw.cf.CFBooleanGetValue(ref))
        if tid == fw.cf.CFStringGetTypeID():
            buf = ctypes.create_string_buffer(512)
            fw.cf.CFStringGetCString(ref, buf, 512, 0x08000100)
            return buf.value.decode("utf-8", "replace")
        return None                      # 数组 / 其它类型：本组件不消费
    finally:
        fw.cf.CFRelease(ref)


def _read_codec(fw: _Framework) -> CodecState:
    svc = _codec_service(fw)
    if not svc:
        return CodecState()
    try:
        hp = _read_cf(fw, svc, "HPDetect")
        mic = _read_cf(fw, svc, "MicDetect")
        conf = _read_cf(fw, svc, "DetectConfidence")
        if hp is None and mic is None:
            return CodecState()          # 本机 codec 不报这些字段（如 Intel 机型）
        return CodecState(available=True, hp_detect=hp, mic_detect=mic,
                          confidence=conf if isinstance(conf, str) else "")
    finally:
        fw.iok.IOObjectRelease(svc)


# ---------- 对外 ----------


def probe() -> Snapshot:
    """做一次完整体检（只读）。任何一步失败都不抛异常，降级为空值。"""
    fw = _fw()
    if not fw.ready:
        return Snapshot(ok=False, reason=fw.reason)
    try:
        devs: list[Device] = []
        for dev_id in _device_ids(fw):
            uid = _cf_string(fw, dev_id, _SEL_UID)
            if not uid:
                continue
            devs.append(Device(
                dev_id=dev_id, uid=uid, name=_cf_string(fw, dev_id, _SEL_NAME),
                has_input=_has_channels(fw, dev_id, _SCOPE_INPUT),
                has_output=_has_channels(fw, dev_id, _SCOPE_OUTPUT),
            ))

        out_id = _uint32_prop(fw, _SYSTEM_OBJECT, _SEL_DEFAULT_OUT)
        in_id = _uint32_prop(fw, _SYSTEM_OBJECT, _SEL_DEFAULT_IN)
        by_id = {d.dev_id: d for d in devs}
        out_dev = by_id.get(out_id) if out_id is not None else None
        in_dev = by_id.get(in_id) if in_id is not None else None

        source = ""
        muted: bool | None = None
        volume: float | None = None
        if out_dev is not None:
            src = _uint32_prop(fw, out_dev.dev_id, _SEL_SOURCE, scope=_SCOPE_OUTPUT)
            if src is not None:
                source = _fourcc(src)
            m = _uint32_prop(fw, out_dev.dev_id, _SEL_MUTE, scope=_SCOPE_OUTPUT)
            muted = None if m is None else bool(m)
            volume = _float32_prop(fw, out_dev.dev_id, _SEL_VOLUME,
                                   scope=_SCOPE_OUTPUT, elem=_ELEMENT_MAIN)

        return Snapshot(
            ok=True, devices=tuple(devs),
            default_input_uid=in_dev.uid if in_dev else None,
            default_output_uid=out_dev.uid if out_dev else None,
            output_source=source,
            codec=_read_codec(fw),
            output_muted=muted, output_volume=volume,
        )
    except Exception as exc:  # noqa: BLE001 —— 探针炸了也不能掀翻组件
        # 体检由 ``tick()`` 每拍驱动，炸一次就一直炸 ⇒ 同因只报一条（首条带堆栈）。
        log_throttled(log, logging.ERROR, "音频状态体检失败", exc_info=True)
        return Snapshot(ok=False, reason=f"体检异常：{exc}")


def _set_default(fw: _Framework, sel: int, uid: str, what: str) -> bool:
    """把默认设备（输入或输出）切成 ``uid`` 指的那个。返回是否写入成功。"""
    target = None
    for dev_id in _device_ids(fw):
        if _cf_string(fw, dev_id, _SEL_UID) == uid:
            target = dev_id
            break
    if target is None:
        log.warning("切默认%s失败：设备清单里没有 uid=%s", what, uid)
        return False
    addr = _Addr(sel, 0, 0)
    payload = ctypes.create_string_buffer(struct.pack("<I", target))
    rc = fw.ca.AudioObjectSetPropertyData(
        _SYSTEM_OBJECT, ctypes.byref(addr), 0, None, 4,
        ctypes.cast(payload, ctypes.c_void_p))
    if rc != 0:
        log.warning("切默认%s失败：rc=%s（uid=%s）", what, rc, uid)
        return False
    log.info("默认%s设备已切到 %s（id=%s）", what, uid, target)
    return True


def set_default_input(uid: str) -> bool:
    """把默认输入设备切成 ``uid`` 指的那个。

    用途是修复「麦克风被认出来了、但默认输入漂到别处」这一类 —— 这是可以纯软件解决的，
    与「芯片压根没认出麦克风」（只能拔插）是两回事。
    """
    fw = _fw()
    return _set_default(fw, _SEL_DEFAULT_IN, uid, "输入") if fw.ready else False


def set_default_output(uid: str) -> bool:
    """把默认输出设备切成 ``uid`` 指的那个。

    修复「耳机插着、声音却走内置扬声器」——用户感知就是「耳机没声音」。同样不需要
    任何特权：这些都是 CoreAudio 的公开写接口。
    """
    fw = _fw()
    return _set_default(fw, _SEL_DEFAULT_OUT, uid, "输出") if fw.ready else False


def set_output_muted(muted: bool) -> bool:
    """把**当前默认输出设备**的静音开关设成 ``muted``。返回是否写入成功。"""
    fw = _fw()
    if not fw.ready:
        return False
    out_id = _uint32_prop(fw, _SYSTEM_OBJECT, _SEL_DEFAULT_OUT)
    if out_id is None:
        return False
    addr = _Addr(_SEL_MUTE, _SCOPE_OUTPUT, _ELEMENT_MAIN)
    payload = ctypes.create_string_buffer(struct.pack("<I", 1 if muted else 0))
    rc = fw.ca.AudioObjectSetPropertyData(
        out_id, ctypes.byref(addr), 0, None, 4, ctypes.cast(payload, ctypes.c_void_p))
    if rc != 0:
        log.warning("写静音开关失败：rc=%s（muted=%s）", rc, muted)
        return False
    log.info("默认输出设备的静音开关已设为 %s", muted)
    return True
