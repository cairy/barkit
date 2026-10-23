"""lifecycle：退出钩子、信号、崩溃残留回收。

§7.4.3 归「留在 helper」的系统状态复原：隧道一激活，/1 路由、系统 DNS、
Wi-Fi 的 IPv6 都被改掉了；helper 被 launchctl stop / kill / 崩溃打断而没走
teardown，这些改动就永久残留（整机断网）。三道防线：

  1) SIGTERM/SIGINT/SIGHUP → 抛 SystemExit → 触发 atexit
  2) atexit → teardown（幂等）
  3) SIGKILL/断电兜不住的 → 启动时读 hijack 标记回收（含杀孤儿 hev）

所有隧道态归属 ``tunnelops`` 那组模块（拆包后是包，状态在 ``tunconsts`` 里），
本模块只做编排 —— 经**真身模块**访问，不经过包属性（包属性是导入时那份快照）。
"""
from __future__ import annotations

import atexit
import json
import os
import re
import signal

# ``tunnelops`` 拆包成包后，包属性只是**导入时那份快照**（没人往包上写）⇒ 必须拿
# 真身模块，否则读到的一律是初始值。指向真身之后与拆包前逐字等价。
from .tunnelops import tunconsts, tunlifecycle, tundns, tunshell, tunwifi
from .safeio import hijack_marker_path, log


# ──────────────────────────────────────────────
# 崩溃残留回收 + 退出钩子
#
# 为什么必须有：隧道一激活，/1 路由、系统 DNS(198.18.0.2)、Wi-Fi 的 IPv6 全都
# 被改掉了。helper 一旦被 launchctl stop / kill / 崩溃打断而没走 _teardown_tunnel，
# 这些改动就永久残留 —— 整台 Mac 断网，而面板显示「未激活」，用户无从恢复。
# 三道防线：
#   1) SIGTERM/SIGINT/SIGHUP → 抛 SystemExit → 触发 atexit
#   2) atexit → _teardown_tunnel（幂等）
#   3) SIGKILL/断电兜不住的 → 启动时读 hijack 标记回收（含杀掉孤儿 hev）
# ──────────────────────────────────────────────

def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _proc_name(pid: int) -> str:
    """进程名（用于确认 pid 没被复用成别的进程）。"""
    if not _pid_alive(pid):
        return ""
    res = tunshell._run(["/bin/ps", "-p", str(pid), "-o", "comm="], timeout=5)
    lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
    return lines[0] if lines else ""


def _boot_time() -> float:
    """系统启动时间戳；拿不到返回 0（调用方按「无法判断」处理）。"""
    res = tunshell._run(["/usr/sbin/sysctl", "-n", "kern.boottime"], timeout=5)
    out = res.stdout or ""
    m = re.search(r"sec\s*=\s*(\d+)", out)
    if m:
        return float(m.group(1))
    m = re.match(r"\s*(\d+)", out)
    return float(m.group(1)) if m else 0.0


def _recover_stale_tunnel() -> None:
    """启动时回收上一次异常退出留下的隧道残留（路由/DNS/IPv6/孤儿 hev）。

    只看「上一个 helper 进程已经不存在」的标记：若上实例还活着，说明是并发/
    重装导致的双实例，它自己会收尾，这里绝不能抢着改网络。
    """
    path = hijack_marker_path()
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(marker, dict):
        try:
            path.unlink()
        except OSError:
            pass
        return

    boot = _boot_time()
    ts = marker.get("ts")
    if boot and isinstance(ts, (int, float)) and ts < boot:
        # 标记来自上一次开机：macOS 重启本身已清空路由/DNS。只清标记，绝不动网络
        # —— 否则会把用户当前正常的网络配置当成残留给「复原」掉。
        log("发现上次开机前的隧道标记（重启已清空网络配置），仅清理标记")
        try:
            path.unlink()
        except OSError:
            pass
        return

    if _pid_alive(marker.get("pid")):
        log("检测到上一个 helper 实例仍在运行，跳过残留回收（交给它自己收尾）")
        return

    log(f"发现上一次异常退出的隧道残留（pid={marker.get('pid')}），开始回收")
    # 孤儿 hev 会一直占着 198.18.0.1，新实例永远建不出 utun，必须先清掉
    hev_pid = marker.get("hev_pid")
    if isinstance(hev_pid, int) and _pid_alive(hev_pid) and "hev" in _proc_name(hev_pid):
        try:
            os.kill(hev_pid, signal.SIGTERM)
            log(f"已终止孤儿 hev（pid={hev_pid}）")
        except OSError as e:
            log(f"终止孤儿 hev 失败：{e}")

    tunconsts._TUNNEL_UTUN = marker.get("utun") or None
    # 服务名由标记带回（`_sync_hijack_marker` 写的）：回收跑在**新进程**里，内存缓存是空的
    # —— 不带上它就只能猜（旧写法兜 "Wi-Fi"，非英文系统必猜错，DNS 便永远还原不回去）。
    # 老标记没这个字段 ⇒ 现场重解析一次（与激活同一入口）；仍读不出就 fail-closed：
    # 宁可这一轮不动，也不写到一个猜来的服务名上。
    tunconsts._TUNNEL_SERVICE = marker.get("service") or None
    if tunconsts._TUNNEL_SERVICE is None:
        tunwifi._resolve_network_targets()
    tunconsts._TUNNEL_ROUTE_HIJACKED = bool(marker.get("route"))
    tunconsts._TUNNEL_DNS_HIJACKED = bool(marker.get("dns"))
    tunconsts._TUNNEL_IPV6_OFF = bool(marker.get("ipv6_off"))
    tunlifecycle._teardown_tunnel({})
    try:
        path.unlink()
    except OSError:
        pass
    log("隧道残留已回收（/1 路由、DNS、IPv6 均已复原）")


def _on_exit() -> None:
    """退出前务必把系统网络复原（atexit + 信号共用）。"""
    if tunconsts._SHUTTING_DOWN:
        return
    tunconsts._SHUTTING_DOWN = True
    try:
        tunlifecycle._teardown_tunnel({})
        tundns._sync_hijack_marker()
    except Exception as e:  # noqa: BLE001 - 退出路径不能再抛
        try:
            log(f"退出清理异常：{e}")
        except Exception:
            pass
    _close_server()


#: 会话服务端（core 起好后注册进来）。
#:
#: 退出时**主动关掉**：Unix 域套接字节点不会随进程消失，留着它下一次启动只是被
#: 覆盖 —— 但「数据目录里该有哪些文件」这件事会因此说不清（一个没人认领的 socket
#: 文件看起来像某个还在跑的东西）。`SessionServer.close()` 会把节点删掉。
_SERVER = None


def _register_server(server) -> None:
    """把会话服务端交给退出路径（core 在 ``open()`` 之前调）。"""
    global _SERVER
    _SERVER = server


def _close_server() -> None:
    """关掉会话服务端并删掉节点（幂等；退出路径调用）。"""
    global _SERVER
    server, _SERVER = _SERVER, None
    if server is None:
        return
    try:
        server.close()
    except Exception as e:  # noqa: BLE001 - 退出路径不能再抛
        try:
            log(f"关闭会话失败（可忽略）：{e}")
        except Exception:
            pass


def _on_signal(signum, _frame) -> None:
    try:
        name = signal.Signals(signum).name
    except Exception:
        name = str(signum)
    log(f"收到 {name}，准备退出并复原网络")
    # 抛 SystemExit 让解释器走正常退出路径，从而触发 atexit 钩子
    raise SystemExit(0)


def _install_exit_hooks() -> None:
    atexit.register(_on_exit)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError, AttributeError):
            pass
