"""**一轮隧道主流程**（``run_tunnel_cycle``）：读意图 → 校验 → 起/保/拆 → 写状态。

四分支与返回字段的语义见函数 docstring（design.md §7 时序）。这是唯一**编排**那一层，
其余各子模块只被它调用、彼此不互相编排。

``tunnelops.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import time
from ..safeio import (
    hev_binary,
    log,
)
from ..state import write_tunnel_state

from . import tunconsts
from . import tunlifecycle
from . import tunvalidate
from . import tunwifi


# ──────────────────────────────────────────────
# 隧道单轮主流程
# ──────────────────────────────────────────────

def run_tunnel_cycle(text: str | None = None) -> dict:
    """跑一轮隧道检查（design.md §7 时序）：

    - 意图非法 → 拒绝，确保隧道已拆除，写错误状态
    - hev 二进制缺失 → 报错，确保拆除
    - 激活（enabled 且上游 SOCKS 就绪）：false→true 先起隧道再切 Wi-Fi；true→true 持续保活
    - 停用：true→false 先切回 Wi-Fi 再拆隧道
    - 中间态 pending：hev 已起、utun 未建出，本轮不阻塞，交给下一轮继续
    返回写出的 tunnel_state dict。

    state 字段语义：
      ok      = 本轮无异常（配置被接受 且 激活/保活/拆除都没报错）
      active  = 隧道已完整生效
      pending = 正在等 hev 建出 utun（面板可显示「建立中」）
      service_unknown = 读不出网络服务名 ⇒ 系统 DNS / IPv6 一项都没动（fail-closed，
                  宁可不接管也不写到一个猜来的服务上，见 _network_service）
    """
    data, err = tunvalidate.load_tunnel(text)
    state: dict = {
        "ok": False,
        "active": False,
        "pending": False,
        "utun": tunconsts._TUNNEL_UTUN,
        "dns_set": False,
        "dns_unavailable": False,
        "service_unknown": False,
        "ipad_reachable": None,
        "last_action": None,
        "error": err,
        "checked_at": int(time.time()),
    }
    if err is not None:
        log(f"隧道意图被拒绝：{err}；确保隧道已拆除", key="tunnel.intent_rejected")
        tunlifecycle._teardown_tunnel(state)
        state["error"] = err
        write_tunnel_state(state)
        return state

    if not hev_binary().exists():
        missing = "hev-socks5-tunnel 二进制未安装（请在 App 内重装守护）"
        log(missing, key="tunnel.hev_missing")
        tunlifecycle._teardown_tunnel(state)
        state["error"] = missing
        write_tunnel_state(state)
        return state

    # 「要不要建隧道」只看开关：**不再看 link_up**（v2 起那一位不在契约里）。
    # 链路通不通由下面几条自己判 —— ``_activate_tunnel`` 起 hev 之前先探上游
    # （不通就 pending，不起进程），``_ensure_tunnel`` 每轮复检上游（断了就拆）。
    # 而「上游在不在」就是「app 的中继在不在」，app 只在链路就绪（或用户显式开了
    # 「手动模式」）时让它监听 ⇒ 两层判据合一，且都是**现问**的，没有快照可言。
    want_active = bool(data["enabled"])

    if want_active and not tunconsts._TUNNEL_ACTIVE:
        result = tunlifecycle._activate_tunnel(data, state)      # 先起隧道
        if result == "ok":
            tunconsts._TUNNEL_ACTIVE = True
            state["last_action"] = tunwifi._run_wifi(data["wifi"].get("on_activate"))  # 再切 Wi-Fi
        elif result == "pending":
            tunconsts._TUNNEL_ACTIVE = False
            state["pending"] = True
        else:
            tunconsts._TUNNEL_ACTIVE = False
    elif not want_active and (tunconsts._TUNNEL_ACTIVE or tunconsts._TUNNEL_PENDING is not None):
        state["last_action"] = tunwifi._run_wifi(data["wifi"].get("on_deactivate"))   # 先切回 Wi-Fi
        tunlifecycle._teardown_tunnel(state)                     # 再拆隧道
        tunconsts._TUNNEL_ACTIVE = False
    elif want_active and tunconsts._TUNNEL_ACTIVE:
        result = tunlifecycle._ensure_tunnel(data, state)        # 持续保活
        if result == "pending":
            # utun 掉了（hev 重建中）：退回未激活，下一轮继续走激活流程
            tunconsts._TUNNEL_ACTIVE = False
            state["pending"] = True
        elif result == "ok":
            state["ipad_reachable"] = True
    else:
        tunlifecycle._teardown_tunnel(state)                     # 未激活也未曾激活：确保干净
        tunconsts._TUNNEL_ACTIVE = False

    state["active"] = tunconsts._TUNNEL_ACTIVE
    state["ok"] = not state.get("error")
    write_tunnel_state(state)
    return state
