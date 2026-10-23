"""iOS 端签名到期通告（announce）的拉取、解析与显示文案。

背景（2026-10-09 设计定案）：iPad 端 App 用个人证书签名（免费开发者描述文件
7 天一签），过期后无法再次启动（前台常驻是系统未文档化的例外，不可依赖）。
SOCKS5 协议（hev）没有放元数据的地方，hev 是第三方引擎不能改 ⇒ iPad 端 App 在
**设备回环** 上开了个「连接即倾倒」应答点（``127.0.0.1:9002``，连接一通就写一份
常量 JSON 然后 close），Mac 经 usbmuxd 直连来读 —— 与 9001 同暴露面（都只绑设备
回环），SOCKS-CONNECT 借道 9001 的方案降为备选。

**节拍纪律**：到期时间是静态事实，不建常驻轮询 —— 拉取只挂在**探针已有的那两个口**上：
① 链路翻上来那一次（``on_link_change(True)``）立刻拉；② 之后每一轮探针 tick
（``on_poll``）在「链路还在、且还没拿到通告」时**低频重试**，拿到一份合法应答即停。
重签 + 重装会重启 App ⇒ 数据面断 ⇒ 链路翻转 ⇒ 下一次翻转自然重拉，故拿到之后不必再定期问。

**为什么失败要重试**（2026-10-10 现场）：设备侧应答点可能恰好在链路翻上来那一刻不可用
（iPad 端 9002 的 accept 源死掉后没有自愈路径），只挂在翻转那一次 ⇒ **整个会话停在
「未通告」，设备侧后来恢复了也没人去问**。「这一次没拿到」不等于「永远拿不到」。

**时间口径**：协议里只有**绝对时刻**（``expires_at``），「还剩几天」一律由 Mac
显示时现算 —— 设备间时钟不同步时相对值必错，绝对值只受 Mac 本地钟影响。

**三态**（判据不许拿 0 或「无限期」顶替失败）：
- ``None`` —— 未通告（旧版 iOS 端 / 设备不在 / 拉取失败），显示层**不显示**；
- ``{"state": "absent"}`` —— App 包里没有 ``embedded.mobileprovision``（商店签名）；
- ``{"state": "ok", "expires_at": <epoch 秒>}`` —— 解析成功。
另有 ``{"state": "unreadable"}``：有档案但读不出来（照实上报，不静默）。

跨线程口径：``fetch()`` 跑在探针线程（框架托管），结果整体替换 ``plugin._announce``
—— 单属性整体替换在 CPython 下原子，读侧（tick / 显示）无需加锁。
"""
from __future__ import annotations

import json
import socket
import time
from datetime import datetime, timezone
from typing import Callable

# ⚠ 走**模块对象**（``mux.X``）而不是绑定副本：测试要 monkeypatch 真身（见 probe.py
# 顶部同一条注释 —— 打成绑定副本补丁落在调用方命名空间上 ⇒ 判据静默失效）。
from . import mux
from .consts import ANNOUNCE_PORT, ANNOUNCE_READ_TIMEOUT_S, log

#: 契约版本（iOS 端 ``payload()["version"]``）。字段收窄见 ``_normalize``：
#: 读侧必须比校验侧宽（严 ⊂ 宽），未知字段** tolerated**、缺关键字段才拒。
ANNOUNCE_VERSION = 1

#: 面板提示阈值（天）：剩余 ≤ 这个数才往状态行上加后缀。设置页**恒显示**，
#: 不受此阈值影响（那是不看阈值的信息位，别共用一把锁）。
EXPIRY_WARN_DAYS = 3

# ── 「这一次没拿到」的短原因（``fetch(on_fail=…)`` 回调给调用方）──────────────────
# 分开报是因为处置完全不同：设备不在要去插线；端口没在听要去看 iPad 端 App；契约不符要改
# 协议。混成一句「拿不到」会把用户指错方向（同 ``consts.RESULT_BADDEV`` 与
# ``RESULT_CONNREFUSED`` 的分法，2026-09-22 现场）。
FAIL_NO_DEVICE = "no_device"    # usbmuxd 里没有目标设备 / 通道不可用
FAIL_REFUSED = "refused"        # 打通了但设备端拒绝 ⇒ 应答点没在听（旧版 App / 已停）
FAIL_READ = "read"              # 打通也接受了，但读不出（超时 / 被断开）
FAIL_CONTRACT = "contract"      # 拿到了却读不懂（version / 字段面对不上）

#: 短原因 → 一句人话。调用方拼它自己那条**按故障期**的日志用（同一个原因不重复出声，
#: 见 ``link._note_announce_fail``）。
FAIL_DESC: dict[str, str] = {
    FAIL_NO_DEVICE: "iPad 不在 USB 上（或 usbmuxd 通道不可用）",
    FAIL_REFUSED: f"iPad 端 {ANNOUNCE_PORT} 应答点没在听（旧版 App，或应答点已停）",
    FAIL_READ: "应答点连通了但读不出来（超时 / 被断开）",
    FAIL_CONTRACT: "应答不合法（Mac 侧与 iPad 侧的契约对不上）",
}


def _log_fail(on_fail: Callable[[str], None] | None, reason: str,
              message: str, *args: object) -> None:
    """一次「没拿到」：细节落 DEBUG（按次，调试用），短原因交回调（调用方按故障期出声）。

    本函数自己**只**落 DEBUG：写 LOG 那条纪律是「长跑进程只记状态翻转」，而这里的调用方
    要连跑数天、每隔 ``ANNOUNCE_RETRY_INTERVAL_S`` 就会再来一次 —— 按次记就是刷屏。
    """
    log.debug("[announce] 拿不到签名通告（%s）：" + message, reason, *args)
    if on_fail is None:
        return
    try:
        on_fail(reason)
    except Exception:  # noqa: BLE001 —— 回调是调用方的事，不许把本函数带走
        log.exception("[announce] on_fail 回调异常")


def fetch(udid: str | None = None, port: int = ANNOUNCE_PORT,
          on_fail: Callable[[str], None] | None = None) -> dict | None:
    """拉一次通告：usbmuxd 直连 iPad 回环 ``port``，读到 EOF，解析成三态 dict。

    返回 ``None`` = 这一次没有拿到通告（旧版 iOS 端 / 设备不在 / 通道故障 /
    应答不合法）——**调用方不得拿 None 清掉已有的好数据**（到期时间不变，
    旧缓存仍然为真；下一次重试自然会再试）。

    ``on_fail``：这一次没拿到时，以**短原因**（``FAIL_*``）回调一次 —— 让调用方把
    「为什么没拿到」写进它自己那条**按故障期**的日志，而不必在这里按次刷屏（本函数
    自己只落 DEBUG，见 ``_log_fail``）。

    只在探针线程调（框架托管），阻塞上限 ≈ 一次 usbmuxd Connect + 读超时。
    """
    try:
        device_id, _serial = mux.pick_device(udid)
    except Exception as e:  # noqa: BLE001 —— 设备不在 / usbmuxd 忙都归「没拿到」
        _log_fail(on_fail, FAIL_NO_DEVICE, "%s", e)
        return None
    try:
        s = mux.open_device_tunnel(device_id, port)
    except Exception as e:  # noqa: BLE001
        # Connect 被拒 = iPad 端没有这个应答点（旧版 App）—— 对旧版是常态；对装过
        # 应答点的设备则意味着那个点停了，两种都要让用户看得见（由调用方按故障期出声）。
        _log_fail(on_fail, FAIL_REFUSED, "打通 iPad:%s 失败：%s", port, e)
        return None
    try:
        raw = _read_to_eof(s)
    except OSError as e:
        _log_fail(on_fail, FAIL_READ, "%s", e)
        return None
    finally:
        try:
            s.close()
        except OSError:
            pass
    parsed = parse(raw)
    if parsed is None:
        # 拿到了却读不懂：**不是**「旧版 iOS 端」那种常态，而是契约不匹配（version /
        # 字段面）—— 原文片段留在 DEBUG，短原因交调用方出声。
        _log_fail(on_fail, FAIL_CONTRACT, "%r", raw[:160])
    return parsed


def _read_to_eof(s: socket.socket, timeout: float = ANNOUNCE_READ_TIMEOUT_S) -> bytes:
    """读到对端 close（EOF）为止，带总超时。应答点是「连接即倾倒」，
    正常应答几百字节 + 立即 close —— EOF 就是应答结束的唯一信号。"""
    s.settimeout(timeout)
    chunks: list[bytes] = []
    while True:
        try:
            buf = s.recv(65536)
        except socket.timeout as e:
            raise OSError(f"读通告超时（{timeout:.0f}s）") from e
        if not buf:
            break
        chunks.append(buf)
    return b"".join(chunks)


def parse(raw: bytes) -> dict | None:
    """把应答字节解析成归一化的三态 dict；不合法一律 ``None``（不猜）。"""
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict) or obj.get("version") != ANNOUNCE_VERSION:
        return None
    return _normalize(obj)


def _normalize(obj: dict) -> dict | None:
    """字段收窄：只认认识的字段，``expires_at`` 统一成 epoch 秒。"""
    state = obj.get("state")
    announced_at = _iso_to_epoch(obj.get("announced_at"))
    if state == "ok":
        expires_at = _iso_to_epoch(obj.get("expires_at"))
        if expires_at is None:
            return None
        return {"state": "ok", "expires_at": expires_at, "announced_at": announced_at}
    if state in ("absent", "unreadable"):
        return {"state": state, "expires_at": None, "announced_at": announced_at}
    return None


def _iso_to_epoch(value) -> float | None:
    """ISO8601（iOS 端 ISO8601DateFormatter，带时区）→ epoch 秒；不合法返回 None。"""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None          # 无时区的时刻说不上「绝对」，不猜
    return dt.timestamp()


# ---------- 显示（消费方读同一份归一结果，不各自再算） ----------

def days_left(announce: dict | None, now: float | None = None) -> int | None:
    """还剩几个整日（按日历天，不是 24h 的整数倍）；没有到期时刻（未通告 /
    absent / unreadable）返回 ``None``。**显示时现算**，不缓存相对值。"""
    if not announce or announce.get("state") != "ok":
        return None
    expires = announce.get("expires_at")
    if not isinstance(expires, (int, float)):
        return None
    now = time.time() if now is None else now
    delta = expires - now
    # 整日向下取整：还剩 0.5 天显示「今天/已到期」而不是「还剩 0 天」。
    if delta <= 0:
        return -1
    return max(0, int(delta // 86400))


def settings_text(announce: dict | None, now: float | None = None) -> str:
    """设置页那一行的**唯一文案出口**（决议 D18 同款纪律：一处算、多处显示）。"""
    if not announce:
        return "未通告（旧版 iOS 端，或 iPad 未连接）"
    state = announce.get("state")
    if state == "absent":
        return "无签名档案（商店/TestFlight 分发，无到期问题）"
    if state == "unreadable":
        return "iOS 端签名档案读不出来（App 侧已通告失败）"
    # state == "ok"
    days = days_left(announce, now)
    expires = announce.get("expires_at")
    date = datetime.fromtimestamp(expires, tz=timezone.utc).astimezone().strftime("%Y-%m-%d")
    if days is not None and days < 0:
        return f"已到期（{date}）· iPad 端进程仍在运行"
    if days == 0:
        return f"今天到期（{date}）· 尽快重签"
    return f"还剩 {days} 天（{date} 到期）"


def panel_suffix(announce: dict | None, now: float | None = None) -> str:
    """面板状态行的后缀：**只在跨过阈值时说话**（不新增状态格，只加一句尾巴）。
    未通告 / 未到阈值 ⇒ 空串（什么都不加）。"""
    days = days_left(announce, now)
    if days is None or days > EXPIRY_WARN_DAYS:
        return ""
    if days < 0:
        return " · iOS 端证书已过期（进程仍在运行）"
    if days == 0:
        return " · iOS 端证书今天到期"
    return f" · iOS 端证书剩 {days} 天"
