"""网卡**快照**与回滚：改之前留一份 baseline、判「还是不是我们写的」、以及还原那一下。

这是本组件唯一会改变用户网络状态的地方，故快照是**保命**的那一份 —— 没有它，
写坏了就出不来。

``routeops.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import json
import time
from .. import safeio
from ..safeio import (
    log,
)

from . import netmode, netsvc
from .ipvalid import valid_ipv4
from .netmode import MODE_DHCP, MODE_STATIC, NETWORKSETUP, _NET_MODES, _set_dns


def _baseline_file():
    """baseline 快照位置：root 私有系统域（同 hosts 备份 / DNS 备份的落点纪律）。"""
    return safeio.SYS_RUN_DIR / "iface_baseline.json"


def read_baseline() -> dict:
    try:
        data = json.loads(_baseline_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def ensure_baseline(service: str, iface: str, info: dict) -> bool:
    """首次改动该服务前留一份它**原来的**配置；返回「现在有没有 baseline」。

    **只留最早那份**（与 ``hostsops._hosts_backup_once`` 同法，存在即 return）：
    baseline 的语义是「交还系统时交还给谁」。第二次改动后再覆盖，交还回去的就成了
    「上一次由我们写进去的值」—— 那等于交还失败，而且用户看不出来。
    """
    data = read_baseline()
    if service in data:
        return True
    data[service] = {"iface": iface, "at": int(time.time()), **info}
    try:
        safeio._atomic_write_json(_baseline_file(), data)
    except OSError as e:
        log(f"留 baseline 失败（本次不改这块网卡）：{e}")
        return False
    log(f"已留网卡 baseline：{service} mode={info.get('mode')} ip={info.get('ip')}")
    return True


def _write_target(service: str, target: dict) -> bool:
    """按规范化目标写网卡的**地址部分**（DNS 归调用方按各自口径处置）。

    ``static`` 与 ``dhcp+pin`` 落盘是同一个动作：**切 Manual ＋ 写死地址**（§2.5 那张
    流程图末行）。两条路的差别只在 ``target`` 是从配置抄的还是从租约抄的 —— 命令一模一样，
    故这里不再分叉。
    """
    args = [NETWORKSETUP, "-setmanual", service, target["ip"], target["mask"]]
    if target.get("gateway"):
        args.append(target["gateway"])
    return netmode._net_cmd(args)[0] == 0


def _same(current: dict, target: dict | None) -> bool:
    """网卡**现在**是不是已经等于 ``target`` 那一份（``target=None`` ＝ 它是 DHCP）。

    只比「我们会写下去的字段」：``ip`` / ``mask`` / ``gateway`` 恒比。``gateway`` 没
    声明在我们这份里 ＝ 我们写下去的就是「没有 router」，所以网卡上多出任何一个合法
    router 都算漂移 —— 留着它会被系统当成默认路由，影响其他路由与 Apple 连续互通
    （2026-10-19 用户裁定）。清空后 ``-getinfo`` 读回 ``(null)`` 之类，非 IP 一律当
    「没有」。``dns`` 只在 target 声明了才比：DNS 归调用方按各自口径处置。

    两个用途（省一次多余的写入、判断网卡还在不在我们手上）。判错的**方向是安全的**：
    只会多写一次（幂等），不会少写。
    """
    if not isinstance(current, dict) or current.get("mode") not in _NET_MODES:
        return False
    if target is None:
        return current["mode"] == MODE_DHCP
    # ``static`` 与 ``dhcp+pin`` 落盘后都是 Manual，读回来一律是 "static"
    if current["mode"] != MODE_STATIC:
        return False
    if current.get("ip") != target["ip"] or current.get("mask") != target["mask"]:
        return False
    want = str(target.get("gateway") or "")
    have = str(current.get("gateway") or "")
    if want != (have if valid_ipv4(have) else ""):
        return False
    if target.get("dns") and list(target["dns"]) != list(current.get("dns") or []):
        return False
    return True


def _baseline_target(entry: dict) -> tuple[dict | None, str | None]:
    """baseline 条目 → 规范化目标（``None`` ＝ 它就是 DHCP）。认不出就报错，不猜。"""
    mode = entry.get("mode")
    if mode == MODE_DHCP:
        return None, None
    if mode != MODE_STATIC:
        return None, f"baseline 的模式认不出（{mode!r}）"
    ip = str(entry.get("ip") or "")
    mask = str(entry.get("mask") or "")
    if not (valid_ipv4(ip) and valid_ipv4(mask)):
        return None, f"baseline 里的静态地址不可用：{ip!r} / {mask!r}"
    return {"mode": MODE_STATIC, "ip": ip, "mask": mask,
            "gateway": str(entry.get("gateway") or ""),
            "dns": [s for s in (entry.get("dns") or []) if valid_ipv4(s)]}, None


def _restore_baseline(service: str, current: dict | None = None, *,
                      skip_dns: bool = False) -> bool:
    """把服务还原成 baseline（＝ `[交还系统]` 的落点）。返回是否已达成。

    **幂等**：网卡已经停在 baseline 上时**不写**。这一条不是省一次命令 —— 是正确性：
    「无匹配时交还」是**策略**（每轮都会说一遍），不幂等的话 helper 每 30s 就
    `setdhcp` 一次，用户的网络被周期性地打断一下，而屏幕上什么都看不出来。

    DNS 单独收口：``baseline`` 里没记 DNS（= 当初本来就没有）时，要显式清掉我们写上去
    的那份（``-setdnsservers <svc> empty``），否则交还只交还了一半。``skip_dns`` 为真时
    （用户设置里「不恢复 DNS」）整段跳过 DNS 收口，只还原地址部分。

    baseline 缺失一律**不猜**：宁可不交还，也不替用户编一份配置写下去。
    """
    entry = read_baseline().get(service)
    if not isinstance(entry, dict):
        log(f"{service} 没有 baseline，无法交还（宁不动，也不猜一个配置写下去）")
        return False
    target, err = _baseline_target(entry)
    if err:
        log(f"{service} 的 {err}，拒绝交还")
        return False
    if current is None:
        current = netsvc.read_service_config(service)
    want_dns = [s for s in (entry.get("dns") or []) if valid_ipv4(s)]
    if (_same(current, target)
            and (skip_dns
                 or list((current or {}).get("dns") or []) == list(want_dns))):
        return True                      # 已经在 baseline 上了（含 DNS）
    if target is None:
        if netmode._net_cmd([NETWORKSETUP, "-setdhcp", service])[0] != 0:
            return False
    elif not _write_target(service, target):
        return False
    if skip_dns:
        return True
    return _set_dns(service, want_dns)


def _baseline_dns(service: str) -> list:
    """baseline 里记着的 DNS（没有 baseline / 当初就没设 ⇒ 空表 ⇒ 调用方**清空**）。"""
    entry = read_baseline().get(service)
    if not isinstance(entry, dict):
        return []
    return [s for s in (entry.get("dns") or []) if valid_ipv4(s)]
