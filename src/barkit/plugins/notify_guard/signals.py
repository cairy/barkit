"""notify_guard/signals：信号检测（方案 §4.2，任务 2.2；2026-08 降频重构）。

S1 OpenDisplay 虚拟显示器（仅 extend 模式）：
    system_profiler 指纹匹配 vendor=0x5043 "PC" / product=0x4F53 "OS"。

降频设计（长时间运行、最小资源占用）：
- system_profiler 是重型命令（fork + 枚举全部硬件），原实现每轮 poll（3s）都跑，
  每天约 2.8 万次 fork。现改为"状态变化驱动"：
  1. 每轮只做 CG 在线显示器签名比对（ctypes 微秒级、零 fork）；
  2. 签名变化（显示器插拔）→ 才跑 system_profiler 确认；
  3. 签名里无非内建屏 → 直接 False，不跑 system_profiler；
  4. 签名未变但超过 TTL（默认 60s）→ **只在「还没确认到 OpenDisplay 屏、且重试窗口
     没用完」时**重查（``S1_MAX_RETRY`` 次，签名一变就重置）。
  于是**稳态下 system_profiler 恒零调用** —— 包括挂着外接 / 虚拟屏的时候。此前第 4 条
  写的是「过期兜底重查一次」，实际在挂着虚拟屏时是**每分钟一次**无条件 fork
  （2026-10-09 实测：那行 DEBUG 全史 969 条、间隔 57~63s、单次 220ms），与本节自述的
  「零调用」矛盾 —— 兜底的原意是「防系统延迟识别」，不是一个常驻节拍。

OpenDisplay 应用态（三级线索的 ①②，设计 §2.3）：
    ① 装没装 —— NSWorkspace.URLForApplicationWithBundleIdentifier_（零 fork）
    ② 跑没跑 —— NSWorkspace.runningApplications() 按 bundle id **前缀**匹配
        （必须前缀：Debug 构建是 ...mac.debug，见设计 §2.3）
    ③ 有没有第二屏 —— 即下面的 S1（CG 签名 + system_profiler 指纹）
``detect_display_state()`` 把三级合成一个四态取值（absent/idle/running/extended），
同时喂状态机取值、取值图标与决策依据。①②是进程内调用，同样走 TTL 缓存。

S2 规则命中：统一为进程存在性检测。ps 快照带 TTL 缓存（默认 5s）；
且调用方在 S1=False 时无需查 S2（allow = S1 AND (NOT S2)，S1=False 恒静音），
由 plugin.tick 短路跳过，日常 ps 零调用。
"""

from __future__ import annotations

import ctypes
import json
import logging
import subprocess
import time

from barkit.core.log_throttle import log_throttled

# 装置事实（装没装 / 跑没跑 / bundle id）的唯一落点是 ``opendisplay`` 模块；
# 本模块只做**信号合成**与降频缓存，不重复探测逻辑。
from .opendisplay import (
    APP_ABSENT,
    APP_IDLE,
    APP_RUNNING,
)
from .opendisplay import app_state as probe_app_state

log = logging.getLogger(__name__)

# OpenDisplay 虚拟显示器指纹（来源：OpenDisplay 源码 Mac/VirtualDisplay.swift，
# 与参考脚本 screen_capture_guard.py 一致；system_profiler 输出为 hex 字符串）
OPENDISPLAY_VENDOR_ID = "5043"    # 0x5043 "PC"
OPENDISPLAY_PRODUCT_ID = "4f53"   # 0x4F53 "OS"

# display 状态机的四态取值（取值闭集，契约 §2.2；设计 §3.2 / §4.1）。
# 前三个与「OpenDisplay 应用态」同名同义，故**别名到单点定义**（``opendisplay.APP_*``），
# 不另立一份会与之漂移的副本；``extended`` 是唯一只属于 display 的取值（来自 S1）。
DISPLAY_ABSENT = APP_ABSENT      # 未安装 OpenDisplay
DISPLAY_IDLE = APP_IDLE          # 装了，没跑
DISPLAY_RUNNING = APP_RUNNING    # 跑了，没侦测到第二屏（**含 mirror 模式盲区**）
DISPLAY_EXTENDED = "extended"    # 第二屏在线

# 在线显示器缓存上限
_MAX_DISPLAYS = 32

# 降频参数
DISPLAY_TTL = 60.0     # 秒，签名未变时 system_profiler 兜底刷新间隔
PS_TTL = 5.0           # 秒，ps 快照缓存
APP_TTL = 10.0         # 秒，OpenDisplay 应用态（装没装 / 跑没跑）缓存

#: 签名变化后最多再补查 system_profiler 的次数。
#:
#: 兜底的原意是「防系统延迟识别」：刚插上屏的那一刻 CG 签名已经变了，而 system_profiler
#: 那一份快照可能还没跟上。它不该变成常驻节拍 —— 签名不变就意味着显示器集合没变，
#: 「有没有 OpenDisplay 屏」这个结论不会变。上限 × TTL（3 × 60s = 3 分钟）远长于任何
#: 真实的识别延迟，且签名一变就重置。
S1_MAX_RETRY = 3

# CoreGraphics ctypes 绑定（CG 调用微秒级，供 S1 预检）
_cg = ctypes.cdll.LoadLibrary(
    "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
)
_CGDirectDisplayID = ctypes.c_uint32
_cg.CGGetOnlineDisplayList.restype = ctypes.c_int
_cg.CGGetOnlineDisplayList.argtypes = [
    ctypes.c_uint32,
    ctypes.POINTER(_CGDirectDisplayID),
    ctypes.POINTER(ctypes.c_uint32),
]
_cg.CGDisplayIsBuiltin.restype = ctypes.c_int
_cg.CGDisplayIsBuiltin.argtypes = [_CGDirectDisplayID]

# 模块级缓存（信号低频变化，缓存收益远大于开销）
_cache = {
    "disp_sig": None,     # 上次在线显示器签名（tuple[(id, is_builtin)]）或 None
    "s1": False,          # 缓存的最新 S1
    "s1_at": 0.0,         # 上次确认 S1 的 monotonic 时间
    "s1_retry": 0,        # 自上次签名变化以来补查过几次（上限 S1_MAX_RETRY）
    "ps": [],             # ps 快照缓存（小写路径列表）
    "ps_at": 0.0,
    "app": None,          # OpenDisplay 应用态缓存（absent / idle / running）
    "app_at": 0.0,
}


def reset_cache() -> None:
    """清空缓存（测试用；生产不应调用）。"""
    _cache["disp_sig"] = None
    _cache["s1"] = False
    _cache["s1_at"] = 0.0
    _cache["s1_retry"] = 0
    _cache["ps"] = []
    _cache["ps_at"] = 0.0
    _cache["app"] = None
    _cache["app_at"] = 0.0


# ──────────────────────────────────────────────
# 线索 ①②：OpenDisplay 应用态（装没装 / 跑没跑，零 fork）
#
# 探测实现归 ``opendisplay.app_state()``（装置事实单点）；本小节只负责**降频**——
# pyobjc 调用虽零 fork，也不该每轮都做，故加一层 TTL 缓存。
# ──────────────────────────────────────────────

def detect_app_state(ttl: float = APP_TTL) -> str:
    """线索 ①②（带 TTL 缓存）：``running`` / ``idle`` / ``absent``。

    与 S1 同一套降频纪律：pyobjc 调用虽零 fork，也不该每轮都做——应用启停以秒级
    为尺度，10s 缓存足够，且装 / 卸 App 后最多 10s 纠正。
    """
    now = time.monotonic()
    if _cache["app"] is None or now - _cache["app_at"] >= ttl:
        _cache["app"] = probe_app_state()
        _cache["app_at"] = now
    return _cache["app"]


def detect_display_state() -> str:
    """display 状态机的四态取值（设计 §3.2）：三级线索由便宜到贵叠加。

    - ``absent``：未安装 OpenDisplay（①②，零 fork）
    - ``idle``：装了但没跑（①②，零 fork）
    - ``extended``：第二屏在线（③，S1）
    - ``running``：进程在跑却没侦测到第二屏 —— **含 mirror 模式盲区**（设计 §2.3：
      mirror 不建虚拟屏 ⇒ S1 恒假）。该盲区已裁定**接受且保持二值**（§9.1-3），
      不新增字形，"为什么"由副标题承载。

    顺序有性能含义：App 没跑时**不会**走到 ``detect_virtual_display()``
    （后者在显示器插拔时会 fork ``system_profiler``）。
    """
    app = detect_app_state()
    if app != DISPLAY_RUNNING:
        return app
    return DISPLAY_EXTENDED if detect_virtual_display() else DISPLAY_RUNNING


# ──────────────────────────────────────────────
# S1：虚拟显示器
# ──────────────────────────────────────────────

def _display_signature() -> tuple | None:
    """在线显示器签名：sorted((display_id, is_builtin))。失败返回 None。"""
    try:
        lst = (_CGDirectDisplayID * _MAX_DISPLAYS)()
        cnt = ctypes.c_uint32(0)
        if _cg.CGGetOnlineDisplayList(_MAX_DISPLAYS, lst, ctypes.byref(cnt)) != 0:
            log_throttled(log, logging.WARNING, "CGGetOnlineDisplayList 失败")
            return None
        return tuple(sorted(
            (lst[i], bool(_cg.CGDisplayIsBuiltin(lst[i]))) for i in range(cnt.value)
        ))
    except Exception as e:
        log_throttled(log, logging.WARNING, "显卡签名异常：%s", e)
        return None


def _detect_by_profiler() -> bool:
    """跑 system_profiler 确认 OpenDisplay 指纹（重型调用，仅状态变化时触发）。"""
    try:
        out = subprocess.check_output(
            ["system_profiler", "SPDisplaysDataType", "-json"],
            text=True, timeout=10,
        )
        data = json.loads(out)
        for gpu in data.get("SPDisplaysDataType", []):
            for disp in gpu.get("spdisplays_ndrvs", []):
                vid = disp.get("_spdisplays_display-vendor-id", "")
                pid = disp.get("_spdisplays_display-product-id", "")
                if vid == OPENDISPLAY_VENDOR_ID and pid == OPENDISPLAY_PRODUCT_ID:
                    log.debug("发现 OpenDisplay 虚拟显示器：%s", disp.get("_name", "?"))
                    return True
    except Exception as e:
        log_throttled(log, logging.WARNING, "system_profiler 调用失败：%s", e)
    return False


def detect_virtual_display() -> bool:
    """S1：OpenDisplay 虚拟显示器是否存在（extend 模式指纹）。

    状态变化驱动：CG 签名变化（插拔）→ 必查；签名未变 → TTL 内用缓存，过期后**只在
    还没确认到、且补查窗口没用完时**再问一次（:data:`S1_MAX_RETRY`）。**稳态下
    system_profiler 零调用** —— 包括挂着外接 / 虚拟屏的时候。
    """
    sig = _display_signature()
    now = time.monotonic()

    if sig is not None and sig != _cache["disp_sig"]:
        # 显示器插拔/上线：必须重查，并重置补查窗口
        _cache["disp_sig"] = sig
        _cache["s1_retry"] = 0
        if not any(not builtin for _, builtin in sig):
            _cache["s1"] = False   # 无非内建屏，不可能有 OpenDisplay
        else:
            _cache["s1"] = _detect_by_profiler()
        _cache["s1_at"] = now
        return _cache["s1"]

    # 签名未变（或 CG 失败）：TTL 内用缓存
    if now - _cache["s1_at"] < DISPLAY_TTL:
        return _cache["s1"]
    _cache["s1_at"] = now
    # TTL 过期。签名没变 ⇒ 显示器集合没变 ⇒ 「有没有 OpenDisplay 屏」不会变，所以只有
    # 两种情况值得再 fork 一次：① 还没确认到（覆盖「插上那一刻 profiler 还没跟上」）；
    # ② 补查窗口还没用完。已确认到（s1 为 True）就不必再问 —— 签名没变意味着它还在。
    # 不设这两道闸，挂着虚拟屏时就是每 60s 无条件 fork 一次（P1 性能 + 日志刷屏）。
    if _cache["s1"] or _cache["s1_retry"] >= S1_MAX_RETRY:
        return _cache["s1"]
    if sig is not None and not any(not builtin for _, builtin in sig):
        return _cache["s1"]   # 无非内建屏，不跑 profiler
    _cache["s1_retry"] += 1
    _cache["s1"] = _detect_by_profiler()
    return _cache["s1"]


# ──────────────────────────────────────────────
# S2：规则进程存在性
# ──────────────────────────────────────────────

def get_process_snapshot(ttl: float = PS_TTL) -> list[str]:
    """一次 ps 快照（TTL 缓存），返回小写进程路径列表（失败返回空列表）。"""
    now = time.monotonic()
    if now - _cache["ps_at"] < ttl:
        return _cache["ps"]
    try:
        out = subprocess.check_output(["ps", "-axo", "comm="], text=True, timeout=5)
        snap = [line.strip().lower() for line in out.splitlines() if line.strip()]
    except Exception as e:
        log_throttled(log, logging.WARNING, "ps 快照失败：%s", e)
        snap = []
    _cache["ps"] = snap
    _cache["ps_at"] = now
    return snap


def match_rules(snapshot: list[str], rules: list[dict]) -> list[dict]:
    """S2：统一规则匹配。任一 pattern 命中任一进程路径即算本条命中，返回命中列表。"""
    hits: list[dict] = []
    for rule in rules or []:
        patterns = [str(p).lower() for p in (rule.get("processes") or []) if p]
        if not patterns:
            continue
        if any(pat in path for pat in patterns for path in snapshot):
            hits.append(rule)
    return hits
