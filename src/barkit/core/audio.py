"""core/audio：框架统一事件源 —— ``system.audio.device_changed``（§8.2.1 命名）。

**它解决什么**：3.5mm 插孔的插入 / 拔出、以及默认输入输出设备的漂移，都是**离散事件**。
此前组件只能靠自己的周期节拍照到它们：``audio_guard`` 原先的兜底节拍是 300 秒，插上耳机
最坏要等五分钟才被看见 —— 而这段窗口正是用户觉得「耳机没声音」的时候。

**为什么不是内核推来的原生通知（2026-09-22 实测，别重试这两条）**：CoreAudio 的两套属性
通知在本机（macOS 15.7.5）都不通：

- ``AudioObjectAddPropertyListener``（经典 C 回调版）：登记返回 0（``noErr``），但**从不
  回调**。判据是排除法做的：同一个属性用读接口能读到它确实变了（真换一次默认输出设备、
  真跑一段无声播放，两处都试过），而回调一次都没来；带 run loop / 不带 run loop 一样。
  注册成功**不等于**监听生效 —— 它连不存在的属性都返回 0。
- ``AudioObjectAddPropertyListenerBlock``（block 版）：要一枚真 block。PyObjC 只有在框架
  元数据齐备时才肯把 Python 可调用对象桥成 block，而 ``loadBundleFunctions`` 只收元组
  签名（``@?`` 不带内联签名 ⇒ "no signature available"）、不收元数据字典，本仓也没装
  ``pyobjc-framework-CoreAudio``；手工用 ctypes 造 block（全局 block）在登记那一刻段错误。

于是改用**读接口比对**：CoreAudio 的读接口（``AudioObjectGetPropertyData``）实测稳定可用，
每次只读四个值 —— 默认输出设备 / 默认输入设备 / 设备清单 / 默认输出设备的数据源。比对挂在
**框架唯一心跳**上（``core.heartbeat``，2 秒一次；而心跳本身每秒就在跳），既不新增时钟，
也不占用组件层：插件侧看到的仍然是一个事件，仍然只声明 topic。

**诚实记一句代价**：这不是内核推过来的事件，是框架替它比出来的。换来的是「延迟从 300 秒
降到几秒」且**不再需要组件自己轮询**。将来原生通知可用时（装上 ``pyobjc-framework-CoreAudio``
或换系统版本），只需换掉本文件的 :class:`_CoreAudioProbe` —— 对订阅方的契约（topic、
载荷、稳定窗口语义）一个字都不用改。

**为什么在事件源这一层等稳定窗口**：``core/power`` 已经用过同一条理由 —— 插孔识别是
**分两步**做的（实测：约 0.4s 先认出耳机，约 1.7s 才确认麦克风）。本事件源因此在最后
一次比对发现变化之后等 ``SETTLE_MS`` 再投递，语义是「**这次变化已经落定、可以读现场了**」。
不这么做的话，订阅方会在识别只跑完一半时被叫起来，读到「系统里没有外置麦克风」⇒ 判成
故障，而一秒后那台设备自己就出现了。

**降级**：非 macOS、或读不到 CoreAudio 时 ``start()`` 返回 ``False`` —— 订阅方照常运行、
只是收不到设备变化事件（降级而不是报错）。
"""

from __future__ import annotations

import ctypes
import logging
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer

if TYPE_CHECKING:
    from barkit.core.events import EventBus
    from barkit.core.heartbeat import Heartbeat

log = logging.getLogger(__name__)

#: §8.2.1 系统事件命名：``system.<domain>.<event>``
AUDIO_DEVICE_TOPIC = "system.audio.device_changed"

#: 比对节律（秒）。比对只是四个属性读（微秒级），而**框架心跳本身每秒就在跳**，
#: 故这里取 2 秒只多出 0.5 Hz —— 与它换来的「插拔几秒内被发现」完全不成比例。
WATCH_INTERVAL_S = 2.0

#: 最后一次比对发现变化之后、投递前的稳定窗口（毫秒）。
#: 取值与 ``core.power.WAKE_SETTLE_MS`` 同源（那条的实测：插孔识别分两步，约 0.4s 认出
#: 耳机、约 1.7s 才确认麦克风）。3 秒是为了让订阅方**一次就读到终态**，而不是被叫起来
#: 两次、中间那次读到半成品而误判。
SETTLE_MS = 3000

#: 心跳订阅的 key（相位由它复现地算出，见 ``heartbeat.phase_for``）。
_TICKER_KEY = "core.audio"

_SYSTEM_OBJECT = 1
_SCOPE_GLOBAL = 0
_SCOPE_OUTPUT = 0x6F757470         # 'outp'
_ELEMENT_MAIN = 0

#: CoreAudio 选择器（'xxxx' 的 big-endian 整数）。与 ``plugins/audio_guard/audio_state``
#: 里那份是同一批常量：那边读得更多（codec / 静音 / 音量），这边**只要够比对**。
#: 两侧不共用一份是有意的 —— 让 core 去 import 某个组件是反向依赖。
_SEL_DEVICES = 0x64657623          # 'dev#'
_SEL_DEFAULT_IN = 0x64496E20       # 'dIn '
_SEL_DEFAULT_OUT = 0x644F7574      # 'dOut'
_SEL_SOURCE = 0x73737263           # 'ssrc'


class _Addr(ctypes.Structure):
    """``AudioObjectPropertyAddress``（sel / scope / elem 三个 uint32）。"""

    _fields_ = [("sel", ctypes.c_uint32), ("scope", ctypes.c_uint32),
                ("elem", ctypes.c_uint32)]


@dataclass(frozen=True)
class _Fingerprint:
    """一次比对的四个读数。

    **只用来回答「变没变」，不解释含义** —— 「变了意味着什么」归组件的判据
    （它才是那个知道自己关心耳机还是扬声器的人）。读不到的值留 ``None``：``None``
    也是一个**稳定**的读数，不会自己制造出一次假变化。
    """

    default_output: "int | None" = None
    default_input: "int | None" = None
    devices: tuple[int, ...] = ()
    output_source: "int | None" = None


def _changed_kinds(old: _Fingerprint, new: _Fingerprint) -> tuple[str, ...]:
    """两个读数之间的变化类别（与载荷里报的名字同一套）。

    ``default_output`` 把「默认输出设备」与「它的数据源」算作同一类：插拔耳机时这两者
    往往一起变，而订阅方要做的动作只有一个（重读现场）—— 分成两类只会诱导它写分叉判据。
    """
    kinds: list[str] = []
    if old.devices != new.devices:
        kinds.append("devices")
    if (old.default_output, old.output_source) != (new.default_output, new.output_source):
        kinds.append("default_output")
    if old.default_input != new.default_input:
        kinds.append("default_input")
    return tuple(kinds)


@dataclass(frozen=True)
class AudioDeviceEvent:
    """``system.audio.device_changed`` 的载荷。

    ``kinds``：这次变化涉及哪几类 —— ``devices`` / ``default_output`` /
    ``default_input`` 的子集（保序去重）。一次插拔往往涉及两三类，这里**合并成一件事**
    报：订阅方要做的都是「重读一次现场」，区分是哪一类只会诱导它写分叉判据。

    ``at`` 是**投递时刻**（已含稳定窗口），不是变化发生的原点时刻。
    """

    kinds: tuple[str, ...]
    at: float


class _CoreAudioProbe:
    """读那次比对要用的四个值（**只读**，普通用户权限）。

    非 macOS、或框架加载失败时 ``read()`` 一律返回 ``None`` ⇒ 事件源降级为不可用。
    ctypes 的签名必须逐条声明：默认按 ``int`` 处理返回值与参数，而这里是 64 位指针，
    不声明会被截断（表现是「偶尔读到垃圾」）。
    """

    def __init__(self) -> None:
        self._ca = None
        self.reason = ""
        if sys.platform != "darwin":
            self.reason = "非 macOS"
            return
        try:
            ca = ctypes.CDLL(
                "/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        except OSError as exc:  # pragma: no cover —— 真实 macOS 上不会走到
            self.reason = f"加载 CoreAudio 失败：{exc}"
            return
        ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
        ca.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(_Addr), ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        self._ca = ca

    def read(self) -> "_Fingerprint | None":
        """读一次现场；不可用 / 读不出来返回 ``None``（不抛异常）。"""
        ca = self._ca
        if ca is None:
            return None
        try:
            devs = self._ids(ca, _SYSTEM_OBJECT, _SEL_DEVICES, 4096)
            if devs is None:
                return None
            out = self._u32(ca, _SYSTEM_OBJECT, _SEL_DEFAULT_OUT)
            # 数据源读的是**当前默认输出设备**上的那份（元素 0、输出作用域）
            source = self._u32(ca, out, _SEL_SOURCE, _SCOPE_OUTPUT) if out else None
            return _Fingerprint(
                default_output=out,
                default_input=self._u32(ca, _SYSTEM_OBJECT, _SEL_DEFAULT_IN),
                devices=devs,
                output_source=source,
            )
        except Exception:  # noqa: BLE001 —— 探针炸了不能掀翻心跳
            log.exception("读音频设备属性失败")
            return None

    @staticmethod
    def _u32(ca, oid: int, sel: int, scope: int = _SCOPE_GLOBAL) -> "int | None":
        addr = _Addr(sel, scope, _ELEMENT_MAIN)
        buf = ctypes.create_string_buffer(4)
        n = ctypes.c_uint32(4)
        rc = ca.AudioObjectGetPropertyData(oid, ctypes.byref(addr), 0, None,
                                           ctypes.byref(n), buf)
        if rc != 0 or n.value != 4:
            return None
        return int.from_bytes(buf.raw[:4], "little")

    @staticmethod
    def _ids(ca, oid: int, sel: int, size: int) -> "tuple[int, ...] | None":
        """变长属性（设备清单）：给足缓冲，长度以 HAL 回填的为准。"""
        addr = _Addr(sel, _SCOPE_GLOBAL, _ELEMENT_MAIN)
        buf = ctypes.create_string_buffer(size)
        n = ctypes.c_uint32(size)
        rc = ca.AudioObjectGetPropertyData(oid, ctypes.byref(addr), 0, None,
                                           ctypes.byref(n), buf)
        if rc != 0 or n.value < 4:
            return None
        raw = buf.raw[: n.value]
        return tuple(int.from_bytes(raw[i:i + 4], "little")
                     for i in range(0, len(raw) - 3, 4))


class AudioDeviceSource(QObject):
    """``system.audio.device_changed`` 的唯一事件源。

    生命周期与宿主一致：``start()`` 在组合根装配时调用、``close()`` 在退出时调用。
    节律挂在**框架唯一心跳**上（不新增时钟，见模块 docstring）。
    """

    def __init__(
        self,
        events: "EventBus",
        heartbeat: "Heartbeat",
        parent: QObject | None = None,
        *,
        probe=None,
        watch_s: float = WATCH_INTERVAL_S,
        settle_ms: int = SETTLE_MS,
    ) -> None:
        super().__init__(parent)
        self._events = events
        self._heartbeat = heartbeat
        self._probe = _CoreAudioProbe() if probe is None else probe
        self._watch_s = float(watch_s)
        self._settle_ms = int(settle_ms)
        self._ticker = None                  # 心跳订阅句柄；None = 未启动（也用作「已停」）
        self._last: _Fingerprint | None = None
        self._kinds: list[str] = []
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.timeout.connect(self._emit)

    # ---------- 查询 ----------

    @property
    def active(self) -> bool:
        """是否已挂上比对节律（= 本平台读得到 CoreAudio 且 ``start()`` 成功过）。"""
        return self._ticker is not None

    # ---------- 起停 ----------

    def start(self) -> bool:
        """取一次基准读数并挂上心跳；读不到现场时返回 ``False``（不抛异常）。"""
        if self._ticker is not None:
            return True
        first = self._probe.read()
        if first is None:
            reason = getattr(self._probe, "reason", "")
            log.info("音频设备事件源不可用：读不到 CoreAudio 现场%s",
                     f"（{reason}）" if reason else "")
            return False
        self._last = first
        self._ticker = self._heartbeat.add_ticker(
            self._watch_s, self._compare, key=_TICKER_KEY)
        log.info("音频设备事件源已就绪（每 %gs 比对一次，变化后 %d ms 投递 %s）",
                 self._watch_s, self._settle_ms, AUDIO_DEVICE_TOPIC)
        return True

    def close(self) -> None:
        """摘掉心跳订阅并静默（幂等）。"""
        if self._ticker is not None:
            self._heartbeat.remove_ticker(self._ticker)
            self._ticker = None
        self._settle.stop()
        self._last = None
        self._kinds.clear()

    # ---------- 比对（心跳线程 = 主线程）----------

    def _compare(self) -> None:
        """心跳到期：读一次现场与上次比。变了就登记类别并重起稳定窗口。

        重起（不是「已在计时就不动」）的语义是「**等到不再变了**再说」，故后续变化会
        顺延到期时刻。这与 ``Scheduler`` 的脏标记窗口刻意相反 —— 那边计时从首个标记
        开始，为的是持续事件风暴不饿死点火；这里恰恰要等它停下来。

        读不出来时**不动基准**：把「读失败」当成一次变化会让订阅方收到一条假事件。
        """
        if self._ticker is None:
            return
        fp = self._probe.read()
        if fp is None or self._last is None:
            return
        kinds = _changed_kinds(self._last, fp)
        self._last = fp
        if not kinds:
            return
        for kind in kinds:
            if kind not in self._kinds:
                self._kinds.append(kind)
        self._settle.start(self._settle_ms)

    def _emit(self) -> None:
        if self._ticker is None:
            # ``close()`` 之后的收尾路径：此刻已经没有节律了，多投一条「设备变了」
            # 是**说谎**（我们已经不看了）。
            self._kinds.clear()
            return
        kinds = tuple(self._kinds)
        self._kinds.clear()
        try:
            self._events.publish(
                AUDIO_DEVICE_TOPIC, AudioDeviceEvent(kinds, time.time()))
        except Exception:  # noqa: BLE001 —— 投递失败不该掀翻心跳
            log.exception("投递音频设备变化事件失败")
