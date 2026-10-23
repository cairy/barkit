"""tunnelops 包（2026-10-02 由 tunnelops.py 拆包）：隧道机制的实现，按职责分成
tunconsts / tunshell / tunvalidate / tunwifi / tunroute / tundns / tunhev /
tunlifecycle 八个子模块 ＋ tunnelops.py（一轮主流程）。本文件全量再导出 ⇒ 引用面零改动。"""

from __future__ import annotations

# --- 原 ``tunnelops.py`` 的全部外部导入，原样再导出（引用面零改动）---
import json
import os
import re
import subprocess
import time
from pathlib import Path
from .. import rootbin, routeops
from ..consts import IFCONFIG, ROUTE
from ..safeio import (
    _atomic_write_json,
    _hev_log_path,
    _hev_log_tail,
    _open_nofollow,
    dns_backup_path,
    hev_binary,
    hev_yaml_path,
    hijack_marker_path,
    log,
)
from ..state import write_tunnel_state

# --- 包内各子模块 ---
from .tunconsts import (
    HEV_DNS,
    PENDING_POLL,
    RESOLVER_ADOPT_TIMEOUT,
    SERVICE_TTL,
    TUNNEL_IPV4,
    TUNNEL_POLL,
    TUNNEL_WIFI_KEYS,
    TUNNEL_WIRE_VERSION,
    UTUN_WAIT_TIMEOUT,
    _SHUTTING_DOWN,
    _TUNNEL_ACTIVE,
    _TUNNEL_DNS_HIJACKED,
    _TUNNEL_IPV6_OFF,
    _TUNNEL_MAPDNS_ROUTE,
    _TUNNEL_PENDING,
    _TUNNEL_PROC,
    _TUNNEL_ROUTE_HIJACKED,
    _TUNNEL_SERVICE,
    _TUNNEL_UTUN,
    _TUNNEL_WIFI_IFACE,
)
from .tunshell import (
    _CmdResult,
    _find_utun,
    _flush_dns_cache,
    _has_ipv4,
    _run,
)
from .tunvalidate import (
    _validate_wifi,
    load_tunnel,
    validate_tunnel,
)
from .tunwifi import (
    _WIFI_PORT_HINTS,
    _network_service,
    _primary_iface,
    _resolve_network_targets,
    _run_wifi,
    _service_for_device,
    _switch_wifi,
)
from .tunroute import (
    _add_route_host,
    _add_route_utun,
    _del_route_host,
    _del_route_utun,
)
from .tundns import (
    _apply_dns_hijack,
    _backup_dns,
    _resolver_adopted_dns,
    _restore_dns,
    _sync_hijack_marker,
    _target_service_active,
)
from .tunhev import (
    HEV_TUN_NAME,
    _HEV_YAML_TEMPLATE,
    _disable_ipv6,
    _gen_hev_yaml,
    _hev_alive,
    _hev_integrity_check,
    _start_hev,
    _stop_hev,
    _upstream_ready,
)
from .tunlifecycle import (
    _activate_tunnel,
    _ensure_tunnel,
    _fail_tunnel,
    _teardown_tunnel,
)
from .tunnelops import run_tunnel_cycle

__all__ = [
    "HEV_DNS",
    "HEV_TUN_NAME",
    "IFCONFIG",
    "PENDING_POLL",
    "Path",
    "RESOLVER_ADOPT_TIMEOUT",
    "ROUTE",
    "SERVICE_TTL",
    "TUNNEL_IPV4",
    "TUNNEL_POLL",
    "TUNNEL_WIFI_KEYS",
    "TUNNEL_WIRE_VERSION",
    "UTUN_WAIT_TIMEOUT",
    "_CmdResult",
    "_HEV_YAML_TEMPLATE",
    "_SHUTTING_DOWN",
    "_TUNNEL_ACTIVE",
    "_TUNNEL_DNS_HIJACKED",
    "_TUNNEL_IPV6_OFF",
    "_TUNNEL_MAPDNS_ROUTE",
    "_TUNNEL_PENDING",
    "_TUNNEL_PROC",
    "_TUNNEL_ROUTE_HIJACKED",
    "_TUNNEL_SERVICE",
    "_TUNNEL_UTUN",
    "_TUNNEL_WIFI_IFACE",
    "_WIFI_PORT_HINTS",
    "_activate_tunnel",
    "_add_route_host",
    "_add_route_utun",
    "_apply_dns_hijack",
    "_atomic_write_json",
    "_backup_dns",
    "_del_route_host",
    "_del_route_utun",
    "_disable_ipv6",
    "_ensure_tunnel",
    "_fail_tunnel",
    "_find_utun",
    "_flush_dns_cache",
    "_gen_hev_yaml",
    "_has_ipv4",
    "_hev_alive",
    "_hev_integrity_check",
    "_hev_log_path",
    "_hev_log_tail",
    "_network_service",
    "_open_nofollow",
    "_primary_iface",
    "_resolve_network_targets",
    "_resolver_adopted_dns",
    "_restore_dns",
    "_run",
    "_run_wifi",
    "_service_for_device",
    "_start_hev",
    "_stop_hev",
    "_switch_wifi",
    "_sync_hijack_marker",
    "_target_service_active",
    "_teardown_tunnel",
    "_upstream_ready",
    "_validate_wifi",
    "dns_backup_path",
    "hev_binary",
    "hev_yaml_path",
    "hijack_marker_path",
    "json",
    "load_tunnel",
    "log",
    "os",
    "re",
    "rootbin",
    "routeops",
    "run_tunnel_cycle",
    "subprocess",
    "time",
    "validate_tunnel",
    "write_tunnel_state",
]
