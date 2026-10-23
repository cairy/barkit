"""ipad_tunnel 测试（M1~M3）。

覆盖：
- tunnel_spec：意图正文的构建/校验（无 password 字段）
- helper run_tunnel_cycle：激活（hev+/1/DNS/v6/切 Wi-Fi）、停用（切回/拆隧道/还原）、
  二进制缺失 / 意图非法 的拒绝与清理
- bridge：链路探测逻辑（pick_device + open_device_tunnel 探活 → 回调）
- plugin：start 声明意图、set_automation_active 写 user YAML＋重声明、tick、
  立即检查（resync 命令）、安装委托、订阅与观测

2026-09-29 起本组件**不落任何通道文件**：期望态 / 命令 / 事实 / 存活四条通道都在
与守护的那条会话上。所以这里看不到 ``tunnel.json`` / ``tunnel_state.json`` /
``.trigger`` —— 意图是 ``declare("tunnel", …)`` 的一帧，事实是推来的一份观测。
"""

from __future__ import annotations

import json
import time

import pytest

from barkit.contribution.specs import StateReport
from barkit.core.config import Config
from barkit.core.events import EventBus
from barkit.core.plugin import PluginContext
from barkit.capabilities.helper import safeio, tunnelops
# 拆包后运行时走**模块对象**读（``tunconsts._TUNNEL_ACTIVE`` / ``tunshell._run`` …）。
# 在**包**上打桩打到的是 __init__ 的再导出副本，判据纹丝不动、测试还假绿 ⇒
# 打桩点一律落在真身模块上；``_entry`` 是**入口本体**（run_tunnel_cycle 住的那一份）。
from barkit.capabilities.helper.tunnelops import (
    tunconsts,
    tunhev,
    tunlifecycle,
    tunshell,
    tunwifi,
)
from barkit.capabilities.helper.tunnelops import tunnelops as _entry
from barkit.plugins.ipad_tunnel import tunnel_spec
from barkit.plugins.ipad_tunnel.tunnel_spec import TunnelConfigError
from barkit.plugins.ipad_tunnel.bridge import (
    HOP_CHANNEL,
    PROBE_INTERVAL,
    LinkProbeService,
    RelayService,
)
from barkit.plugins.ipad_tunnel.diagnostics import (
    is_fake_ip_line,
    parse_primary_nameserver,
    parse_route_interface,
    run_diagnostics,
)
from barkit.plugins.ipad_tunnel.plugin import TUNNEL_SUBJECT, Plugin
from barkit.capabilities import helper_client as daemon

# 2026-10-02 `bridge.py` 平铺拆包后，被 monkeypatch 的那批名字**真身在兄弟模块**，
# 而入口 `bridge` 只是把它们再导出了一遍 ⇒ 补丁打在入口上（绑定副本）真身纹丝不动，
# 判据静默失效。故这里给真身子模块各留一个别名，补丁点一律落在它们身上。
from barkit.plugins.ipad_tunnel import consts as bridge_consts
from barkit.plugins.ipad_tunnel import mux as bridge_mux


# ──────────────────────────────────────────────
# 公共
# ──────────────────────────────────────────────

class _Cmd:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


class FakeRun:
    """模拟系统命令：route/ifconfig/networksetup 全部按最乐观结果返回。

    用整条命令字符串做子串匹配（避免 "-listallhardwareports" 与
    "listallhardwareports" 的列表成员检查因前导短横而失配）。

    两个可变状态让它能建模真实因果，而不是恒返回「成功」：
      online     目标网络服务是否持有真实 IPv4（False = off-network，只有链路本地地址）
      accept_dns setdnsservers 是否真被 resolver 采纳（False = networksetup 返回 0
                 却不生效的「假成功」，正是 2.6.1 要防的场景）
      dns        当前生效的系统 DNS（None = 未设置），`scutil --dns` 据此输出
    """
    def __init__(self):
        self.calls = []
        self.online = True
        self.accept_dns = True
        self.dns = None

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        cmd = " ".join(args)
        if "/sbin/route" in cmd:
            return _Cmd(0)
        if "/sbin/ifconfig" in cmd:
            if args[-1] == "-l":
                return _Cmd(0, "lo0 en0 utun9")
            # 建模 ifconfig <iface>：utun* 承载隧道内网地址 TUNNEL_IPV4，供
            # _utun_exists / _find_utun 的「接口在 ＋ 地址对」判据命中；其余接口在线
            # 时持有真实单播 IPv4，off-network 时系统只会自分配一个链路本地地址
            # （169.254/16），_has_ipv4 据此判离线。
            if args[-1].startswith("utun"):
                return _Cmd(0, f"{args[-1]}: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST>\n"
                               f"\tinet {tunconsts.TUNNEL_IPV4} --> {tunconsts.TUNNEL_IPV4} "
                               f"netmask 0xffffff00\n")
            ip = "192.168.1.5" if self.online else "169.254.33.7"
            return _Cmd(0, f"{args[-1]}: flags=8863<UP,BROADCAST,RUNNING,MULTICAST>\n"
                           f"\tinet {ip} netmask 0xffffff00\n")
        if "scutil" in cmd and "--dns" in cmd:
            # 反映当前系统 DNS：多 resolver 块（隧道起来后 utun 也会有），
            # mapdns 落在当前生效的那个上面。
            ns = f"  nameserver[0] : {self.dns}\n" if self.dns else ""
            return _Cmd(0, "resolver #1\n" + ns + "\nresolver #2\n  nameserver[0] : 8.8.8.8\n")
        if "setdnsservers" in cmd:
            if self.accept_dns:
                self.dns = None if args[-1] == "empty" else args[-1]
            return _Cmd(0)
        if "getdnsservers" in cmd:
            return _Cmd(0, "There aren't any DNS Servers set on Wi-Fi.")
        if "listallhardwareports" in cmd:
            return _Cmd(0, "Hardware Port: Wi-Fi\nDevice: en0\n")
        return _Cmd(0)   # setairportnetwork / setv6off / setv6automatic / dscacheutil


class FakePopen:
    """hev 子进程的替身。pid 必须存在：helper 会把它写进劫持标记（崩溃恢复用）。"""

    def __init__(self, *a, **k):
        self.returncode = None
        self.pid = 4242

    def poll(self):
        return None

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


#: 本模块最后造出来的那份意图正文（见 ``_intent`` / ``_cycle``）。
_LAST_INTENT: list = [None]


def _intent(enabled=True, disable_ipv6=True,
            activate="iPad-Share", deactivate="HomeNet",
            hijack_route=True, hijack_dns=True) -> str:
    """造一份合法的隧道意图正文（v2 契约）并记下，返回它。

    2026-09-29 起守护侧**不读文件**：意图由调度器从内存里那份取来，递进
    ``run_tunnel_cycle(text)``。故这里返回正文，不再往数据目录落一份 ``tunnel.json``
    ——「期望态住在哪个路径」这件事两边都不再知道。

    这里**没有** link_up / ignore_link 两格（v2 起不在契约里，见
    tunnelops.TUNNEL_WIRE_VERSION）：「iPad 那条链路此刻通不通」是运行期事实，
    它的载体是 app 侧中继占用的端口，不在意图正文里。要造「上游不在」的现场，
    改的是 helper 侧 ``_upstream_ready`` 的返回值，不是这份正文。
    """
    text = json.dumps({
        "version": 2,
        "enabled": enabled,
        "local_port": 1080,
        "device_port": 9001,
        "bind_interface": "",
        "dns": "198.18.0.2",
        "disable_ipv6": disable_ipv6,
        "hijack_route": hijack_route,
        "hijack_dns": hijack_dns,
        "wifi": {
            "on_activate": activate,
            "on_deactivate": deactivate,
        },
    }, ensure_ascii=False)
    _LAST_INTENT[0] = text
    return text


def _cycle() -> dict:
    """跑一轮守护侧隧道循环：意图取本模块最后造的那一份（见 ``_intent``）。"""
    return tunnelops.run_tunnel_cycle(_LAST_INTENT[0])


@pytest.fixture(autouse=True)
def _reset_helper_state():
    tunconsts._TUNNEL_ACTIVE = False
    tunconsts._TUNNEL_PROC = None
    tunconsts._TUNNEL_UTUN = None
    tunconsts._TUNNEL_IPV6_OFF = False
    tunconsts._TUNNEL_WIFI_IFACE = None
    # 服务名缓存必须一起清：它是 fail-closed 的判据本体，留着上一条用例的值会让
    # 「读不出服务名」那条用例的断言看着绿（其实读到的是别人的 "Wi-Fi"）。
    tunconsts._TUNNEL_SERVICE = None
    tunconsts._TUNNEL_ROUTE_HIJACKED = False
    tunconsts._TUNNEL_DNS_HIJACKED = False
    # 「hev 已起、utun 未建出」的中间态与 mapdns 路由标记也要清：它们本身就是判据
    # （「上游不在听就不起 hev」那条用例正是靠前者证明进程压根没被拉起来）。留着上一条
    # 用例的值，断言会**看着绿** —— 典型是「换了条路径到达同一个结局」的假绿。
    tunconsts._TUNNEL_PENDING = None
    tunconsts._TUNNEL_MAPDNS_ROUTE = False
    yield
    tunconsts._TUNNEL_ACTIVE = False
    tunconsts._TUNNEL_PROC = None
    tunconsts._TUNNEL_UTUN = None
    tunconsts._TUNNEL_IPV6_OFF = False
    tunconsts._TUNNEL_WIFI_IFACE = None
    tunconsts._TUNNEL_SERVICE = None
    tunconsts._TUNNEL_ROUTE_HIJACKED = False
    tunconsts._TUNNEL_DNS_HIJACKED = False
    tunconsts._TUNNEL_PENDING = None
    tunconsts._TUNNEL_MAPDNS_ROUTE = False


@pytest.fixture
def helper_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BARKIT_DATA_DIR", str(tmp_path))
    # root 私有运行目录（日志/hev 配置/DNS 备份/劫持标记）在非 root 环境下
    # 指向 /Library/... 会 PermissionError，测试里重定向到 tmp_path。
    # 生产环境这两个都指向 /Library/Application Support/BarKit（root:wheel 0755）。
    monkeypatch.setattr(safeio, "SYS_RUN_DIR", tmp_path)
    # 传播预算在测试里压到最短：真实环境要等 resolver 抖动，测试只关心判定逻辑
    monkeypatch.setattr(tunconsts, "RESOLVER_ADOPT_TIMEOUT", 0.05)
    fake = FakeRun()
    monkeypatch.setattr(tunnelops.subprocess, "run", fake)
    monkeypatch.setattr(tunnelops.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(tunshell, "_find_utun", lambda: None)
    # 上游 SOCKS 代理（bridge @ 127.0.0.1:local_port）在测试里没有真在监听，
    # 这里统一视为「已就绪」，让激活流程走到 hev/utun 逻辑（与新增的上游探活解耦）。
    monkeypatch.setattr(tunhev, "_upstream_ready", lambda port, timeout=1.0: True)
    hev = tmp_path / "hev-socks5-tunnel"
    hev.write_text("", encoding="utf-8")
    _set_hev_binary(monkeypatch, lambda: hev)
    # hev **执行前**完整性自检（owner/权限 + codesign --verify）在非 root 环境必然不过
    # （tmp 文件不属 root:wheel，也未经 ad-hoc 签名）。本夹具放行它，让既有激活/停用
    # 用例保持聚焦；自检判定本身在 test_hev_integrity_check_* 单测里覆盖。
    monkeypatch.setattr(tunhev, "_hev_integrity_check", lambda path: (True, None))
    return tmp_path, fake


def _set_hev_binary(monkeypatch, fn) -> None:
    """``hev_binary`` 拆包后在三个模块里各有一份绑定，谁也替不了谁。"""
    for _m in (tunhev, tunlifecycle, _entry):
        monkeypatch.setattr(_m, "hev_binary", fn)


# ──────────────────────────────────────────────
# tunnel_spec：构建与校验
# ──────────────────────────────────────────────

def test_build_tunnel_ok_no_password():
    t = tunnel_spec.build_tunnel({
        "enabled": True,
        "on_activate": [{"type": "switch_wifi", "ssid": "A"}],
        "on_deactivate": [{"type": "switch_wifi", "ssid": "B"}],
    })
    assert t["enabled"] is True and t["version"] == 2
    assert t["wifi"]["on_activate"] == "A"
    assert "password" not in json.dumps(t)


def test_build_tunnel_carries_intent_only():
    """产出里**没有**运行期事实（v2 契约）：链路状态不进这份期望态文件。

    ``link_up`` / ``ignore_link`` 都曾是这里的一格。前者是「iPad 那条链路此刻通不通」，
    后者是「要不要等它」这个**意图** —— 但意图已经由中继要不要占端口表达（可观测），
    再往文件里抄一份只会多一处会过期的副本（真机故障的根因）。
    """
    t = tunnel_spec.build_tunnel({"enabled": True, "ignore_link": True, "link_up": True})
    assert "link_up" not in t and "ignore_link" not in t


def test_build_tunnel_hijack_defaults_and_toggle():
    # 默认旁路（都不劫持）：隧道不通时全局劫持 = 整机断网，风险不对称
    t = tunnel_spec.build_tunnel({
        "on_activate": [{"type": "switch_wifi", "ssid": "A"}],
        "on_deactivate": [{"type": "switch_wifi", "ssid": "B"}],
    })
    assert t["hijack_route"] is False and t["hijack_dns"] is False
    # 可显式开启（用户确认隧道可用后）
    t2 = tunnel_spec.build_tunnel({
        "on_activate": [{"type": "switch_wifi", "ssid": "A"}],
        "on_deactivate": [{"type": "switch_wifi", "ssid": "B"}],
        "hijack_route": True, "hijack_dns": True,
    })
    assert t2["hijack_route"] is True and t2["hijack_dns"] is True
    # 非布尔被拒
    with pytest.raises(tunnel_spec.TunnelConfigError):
        tunnel_spec.build_tunnel({"hijack_route": "yes"})
    with pytest.raises(tunnel_spec.TunnelConfigError):
        tunnel_spec.build_tunnel({"hijack_dns": 1})


def test_build_tunnel_rejects_password_field():
    with pytest.raises(tunnel_spec.TunnelConfigError):
        tunnel_spec.build_tunnel({
            "on_activate": [{"type": "switch_wifi", "ssid": "A", "password": "x"}],
        })


def test_build_tunnel_rejects_unknown_action_and_missing_ssid():
    with pytest.raises(tunnel_spec.TunnelConfigError):
        tunnel_spec.build_tunnel({"on_activate": [{"type": "magic", "ssid": "A"}]})
    with pytest.raises(tunnel_spec.TunnelConfigError):
        tunnel_spec.build_tunnel({"on_activate": [{"type": "switch_wifi"}]})


def test_build_tunnel_new_wifi_field():
    """新配置：固定 wifi 字段（无 type 分发）。空串归一为 None（不切）。"""
    t = tunnel_spec.build_tunnel({"wifi": {"on_activate": "A", "on_deactivate": None}})
    assert t["wifi"] == {"on_activate": "A", "on_deactivate": None}
    # 空串 = 不切（与 None 等价）
    t2 = tunnel_spec.build_tunnel({"wifi": {"on_activate": "  ", "on_deactivate": ""}})
    assert t2["wifi"] == {"on_activate": None, "on_deactivate": None}
    # 未知键 / 非 dict / 非字符串 → 拒绝
    with pytest.raises(tunnel_spec.TunnelConfigError):
        tunnel_spec.build_tunnel({"wifi": {"on_activate": "A", "run": "x"}})
    with pytest.raises(tunnel_spec.TunnelConfigError):
        tunnel_spec.build_tunnel({"wifi": "A"})
    with pytest.raises(tunnel_spec.TunnelConfigError):
        tunnel_spec.build_tunnel({"wifi": {"on_activate": 123}})


def test_build_tunnel_legacy_conversion_output_shape():
    """旧配置兼容转换：动作列表 → wifi 字段（app 侧转换，root 侧无 type 解析）。"""
    t = tunnel_spec.build_tunnel({
        "on_activate": [{"type": "switch_wifi", "ssid": " A "}],
        "on_deactivate": [],
    })
    assert t["wifi"] == {"on_activate": "A", "on_deactivate": None}
    # 旧配置带 password 仍拒绝（不静默吞掉）
    with pytest.raises(tunnel_spec.TunnelConfigError):
        tunnel_spec.build_tunnel({"on_activate": [{"type": "switch_wifi", "ssid": "A", "pwd": "x"}]})


def test_build_tunnel_rejects_bad_ports_and_dns():
    with pytest.raises(tunnel_spec.TunnelConfigError):
        tunnel_spec.build_tunnel({"local_port": 70000})
    with pytest.raises(tunnel_spec.TunnelConfigError):
        tunnel_spec.build_tunnel({"dns": "not-an-ip"})


# ──────────────────────────────────────────────
# helper：validate_tunnel 边界
# ──────────────────────────────────────────────

def test_validate_tunnel_rejects_unknown_field():
    _, err = tunnelops.validate_tunnel({"version": 2, "evil": 1})
    assert err and "未知字段" in err


def test_validate_tunnel_version_is_a_hard_lock():
    """契约版本真锁：不是本 helper 那一代就**整份拒绝**，且报的是「该重装」。

    此前版本只进字段白名单、从不拿它比 ⇒ 两侧不同代在 helper 侧完全看不出来，
    只有恰好撞上形状变化才会拒绝，而那时报的是「含未知字段」—— 用户拿不到
    「去重装守护」这条线索（2.13.0 那次真机事故正是这么发生的）。
    缺位同样算不符：没有版本号的文件一律不认。
    """
    _, err = tunnelops.validate_tunnel({"version": 1, "enabled": True})
    assert err and "契约版本" in err and "v2" in err
    _, err = tunnelops.validate_tunnel({"enabled": True})
    assert err and "契约版本" in err
    # 版本对、字段全 → 放行（对照组，证明这条锁没误伤正常文件）
    _, err = tunnelops.validate_tunnel({
        "version": 2, "enabled": True,
        "local_port": 1080, "device_port": 9001, "dns": "198.18.0.2",
    })
    assert err is None


def test_validate_tunnel_rejects_runtime_facts():
    """v2 起 ``link_up`` / ``ignore_link`` 不在契约里：写进来就是**未知字段**。

    它们曾经是合法的（v1）。这一条守的是「事实不再住在期望态文件里」——
    真按 v1 的写法留着，helper 就会拿着一句可能早已过期的事实去建隧道。
    """
    base = {"version": 2, "enabled": True,
            "local_port": 1080, "device_port": 9001, "dns": "198.18.0.2"}
    for field, value in (("link_up", True), ("ignore_link", True)):
        _, err = tunnelops.validate_tunnel({**base, field: value})
        assert err and "未知字段" in err and field in err, (field, err)


def test_validate_tunnel_rejects_deprecated_actions():
    """v2.6.3 安全热修：旧 `actions` 通用动作列表在 root 侧整份拒绝（未知字段）。"""
    _, err = tunnelops.validate_tunnel({
        "version": 2, "enabled": True,
        "local_port": 1080, "device_port": 9001, "dns": "198.18.0.2",
        "actions": {"on_activate": [{"type": "switch_wifi", "ssid": "A", "password": "x"}]},
    })
    assert err and "未知字段" in err


def test_validate_tunnel_wifi_field_rules():
    """wifi 字段只允许 null / 非空字符串；未知键、非字符串、空串均整份拒绝。"""
    base = {
        "version": 2, "enabled": True,
        "local_port": 1080, "device_port": 9001, "dns": "198.18.0.2",
    }
    # null = 该阶段不切 → 放行并归一为 None
    data, err = tunnelops.validate_tunnel({**base, "wifi": {"on_activate": None}})
    assert err is None and data["wifi"]["on_activate"] is None
    # 未知键 → 拒绝
    _, err = tunnelops.validate_tunnel({**base, "wifi": {"on_activate": "A", "evil": 1}})
    assert err and "未知字段" in err
    # 非字符串 → 拒绝
    _, err = tunnelops.validate_tunnel({**base, "wifi": {"on_activate": ["A"]}})
    assert err and "on_activate" in err
    # 空串 → 拒绝（留空请用 null）
    _, err = tunnelops.validate_tunnel({**base, "wifi": {"on_activate": "  "}})
    assert err and "on_activate" in err
    # 非 dict → 拒绝
    _, err = tunnelops.validate_tunnel({**base, "wifi": "A"})
    assert err and "wifi" in err


def test_validate_tunnel_ok():
    data, err = tunnelops.validate_tunnel({
        "version": 2, "enabled": True,
        "local_port": 1080, "device_port": 9001, "dns": "198.18.0.2",
        "wifi": {"on_activate": "A"},
    })
    assert err is None and data["wifi"]["on_activate"] == "A"


def test_validate_tunnel_rejects_dns_outside_tunnel():
    # 安全约束：mapdns 只监听固定地址 198.18.0.2（_HEV_YAML_TEMPLATE），系统 DNS
    # 必须与之严格相等，否则 DNS 请求进隧道后 mapdns 不应答 → 整机断网。故任何
    # 非该地址的 dns 都在加载期拒绝（宁可隧道不激活，也不搞瘫 Mac 网络）。
    base = {
        "version": 2, "enabled": True,
        "local_port": 1080, "device_port": 9001,
        "wifi": {"on_activate": "A"},
    }
    # 公网 DNS：合法 IP 但不是隧道地址 → 拒绝
    _, err = tunnelops.validate_tunnel({**base, "dns": "8.8.8.8"})
    assert err and "dns" in err
    # 同网段但不是 mapdns 监听地址 → 拒绝
    _, err = tunnelops.validate_tunnel({**base, "dns": "198.18.0.3"})
    assert err and "dns" in err
    # 空 dns（沿用默认 HEV_DNS）→ 放行
    _, err = tunnelops.validate_tunnel({**base, "dns": ""})
    assert err is None
    # 恰好等于 HEV_DNS → 放行
    _, err = tunnelops.validate_tunnel({**base, "dns": "198.18.0.2"})
    assert err is None


def test_validate_tunnel_hijack_fields():
    data, err = tunnelops.validate_tunnel({
        "version": 2, "enabled": True,
        "local_port": 1080, "device_port": 9001, "dns": "198.18.0.2",
        "hijack_route": False, "hijack_dns": False,
        "wifi": {"on_activate": "A"},
    })
    assert err is None
    assert data["hijack_route"] is False and data["hijack_dns"] is False
    # 默认旁路：都不劫持（安全默认值）
    data2, _ = tunnelops.validate_tunnel({
        "version": 2, "enabled": True,
        "local_port": 1080, "device_port": 9001, "dns": "198.18.0.2",
        "wifi": {"on_activate": "A"},
    })
    assert data2["hijack_route"] is False and data2["hijack_dns"] is False
    # 显式开启仍受支持
    data3, _ = tunnelops.validate_tunnel({
        "version": 2, "enabled": True,
        "local_port": 1080, "device_port": 9001, "dns": "198.18.0.2",
        "hijack_route": True, "hijack_dns": True,
        "wifi": {"on_activate": "A"},
    })
    assert data3["hijack_route"] is True and data3["hijack_dns"] is True
    # 非布尔被拒（但未知字段会被「未知字段」先拦）
    _, err2 = tunnelops.validate_tunnel({
        "version": 2, "enabled": True,
        "local_port": 1080, "device_port": 9001, "dns": "198.18.0.2",
        "hijack_route": 1, "hijack_dns": "yes",
        "wifi": {"on_activate": "A"},
    })
    assert err2 and "hijack_route" in err2


# ──────────────────────────────────────────────
# helper：run_tunnel_cycle 激活 / 停用
# ──────────────────────────────────────────────

def test_cycle_activate_starts_tunnel(helper_env, monkeypatch):
    src, fake = helper_env
    monkeypatch.setattr(tunshell, "_find_utun", lambda: "utun9")
    _intent(enabled=True)
    state = _cycle()
    assert state["active"] is True
    assert state["utun"] == "utun9"
    assert state["dns_set"] is True
    assert state["ipad_reachable"] is True
    assert state["last_action"] == {"wifi": "iPad-Share", "ok": True}
    # 两条 /1 路由 + DNS + 禁 v6 + 切 Wi-Fi 均已执行
    joined = [" ".join(c) for c in fake.calls]
    assert any("route -n add -net 0.0.0.0/1 -interface utun9" in j for j in joined)
    assert any("route -n add -net 128.0.0.0/1 -interface utun9" in j for j in joined)
    assert any("setdnsservers Wi-Fi 198.18.0.2" in j for j in joined)
    assert any("setv6off Wi-Fi" in j for j in joined)
    assert any("setairportnetwork en0 iPad-Share" in j for j in joined)


def test_cycle_deactivate_tears_down(helper_env, monkeypatch):
    src, fake = helper_env
    monkeypatch.setattr(tunshell, "_find_utun", lambda: "utun9")
    _intent(enabled=True)
    _cycle()            # 先激活
    # 用户关掉开关 → 停用（拆除路径与「上游消失」共用同一条：见
    # test_ensure_tunnel_upstream_lost_tears_down）
    _intent(enabled=False)
    state = _cycle()
    assert state["active"] is False
    assert state["utun"] is None
    assert state["dns_set"] is False
    joined = [" ".join(c) for c in fake.calls]
    # 切回恢复 SSID
    assert any("setairportnetwork en0 HomeNet" in j for j in joined)
    # 删两条 /1
    assert any("route -n delete -net 0.0.0.0/1 -interface utun9" in j for j in joined)
    assert any("route -n delete -net 128.0.0.0/1 -interface utun9" in j for j in joined)
    # 还原 DNS（备份为 empty）+ 复原 v6
    assert any("setdnsservers Wi-Fi empty" in j for j in joined)
    assert any("setv6automatic Wi-Fi" in j for j in joined)


def test_cycle_missing_binary_reports_error(helper_env, monkeypatch):
    src, fake = helper_env
    _intent(enabled=True)
    _set_hev_binary(monkeypatch, lambda: src / "does-not-exist")
    state = _cycle()
    assert state["error"] and "hev" in state["error"]
    assert state["active"] is False


def test_cycle_bad_tunnel_json_keeps_clean(helper_env):
    src, fake = helper_env
    state = tunnelops.run_tunnel_cycle('{"version":2,"evil":1}')
    assert state["ok"] is False and state["error"]
    # 非法文件：不应拉起任何 /1 路由
    joined = [" ".join(c) for c in fake.calls]
    assert not any("route -n add" in j for j in joined)


def test_cycle_link_down_no_action(helper_env):
    src, fake = helper_env
    _intent(enabled=False)
    state = _cycle()
    assert state["active"] is False
    # 未启用：不应拉起任何 /1 路由
    joined = [" ".join(c) for c in fake.calls]
    assert not any("route -n add" in j for j in joined)


def test_cycle_activates_when_upstream_listens(helper_env, monkeypatch):
    """上游在听（＝ app 的中继占着 local_port＝链路就绪）⇒ 起隧道。

    这是 v2 之后**唯一**的激活判据：文件里不再有 link_up 这一格，
    「链路通不通」整个由「那个端口有没有人在听」表达。
    """
    src, fake = helper_env
    monkeypatch.setattr(tunshell, "_find_utun", lambda: "utun9")
    _intent(enabled=True)
    state = _cycle()
    assert state["active"] is True and state["utun"] == "utun9"
    joined = [" ".join(c) for c in fake.calls]
    assert any("route -n add -net 0.0.0.0/1 -interface utun9" in j for j in joined)


def test_cycle_upstream_absent_stays_pending(helper_env, monkeypatch):
    """上游没在听 ⇒ **不拉起 hev**，如实报 pending（等上游回来）。

    ``pending`` 与 ``error`` 必须分得开：前者是「还没到时候」，后者是「出了事」。

    ⚠️ 判据必须落在「hev 有没有被拉起来」上，不能只落在那句 ``pending`` 上：这条路上
    「起了 hev、utun 还没建出」**也是** pending（`_TUNNEL_PENDING` 那个中间态），
    只判结局的话，把上游闸门整个拆掉、让进程白起一次，用例照样绿。
    """
    src, fake = helper_env
    _intent(enabled=True)
    monkeypatch.setattr(tunhev, "_upstream_ready", lambda port, timeout=1.0: False)
    state = _cycle()
    assert state["active"] is False
    assert state["pending"] is True and not state.get("error")
    assert tunconsts._TUNNEL_PENDING is None and tunconsts._TUNNEL_PROC is None, \
        "上游不在听时不许起 hev（起了它也只是干等，还会每分钟重启一次）"
    joined = [" ".join(c) for c in fake.calls]
    assert not any("route -n add" in j for j in joined)


def test_cycle_route_hijack_off_keeps_dns(helper_env, monkeypatch):
    src, fake = helper_env
    monkeypatch.setattr(tunshell, "_find_utun", lambda: "utun9")
    _intent(enabled=True, hijack_route=False)
    state = _cycle()
    assert state["active"] is True and state["utun"] == "utun9"
    joined = [" ".join(c) for c in fake.calls]
    # 关闭路由劫持：完全不加 /1 路由
    assert not any("route -n add -net 0.0.0.0/1" in j for j in joined)
    assert not any("route -n add -net 128.0.0.0/1" in j for j in joined)
    # DNS 劫持仍默认开启
    assert any("setdnsservers Wi-Fi 198.18.0.2" in j for j in joined)
    assert state["dns_set"] is True


def test_cycle_dns_hijack_off_keeps_route(helper_env, monkeypatch):
    src, fake = helper_env
    monkeypatch.setattr(tunshell, "_find_utun", lambda: "utun9")
    _intent(enabled=True, hijack_dns=False)
    state = _cycle()
    assert state["active"] is True
    joined = [" ".join(c) for c in fake.calls]
    # 路由劫持仍默认开启
    assert any("route -n add -net 0.0.0.0/1 -interface utun9" in j for j in joined)
    # 关闭 DNS 劫持：完全不改系统 DNS
    assert not any("setdnsservers Wi-Fi 198.18.0.2" in j for j in joined)
    assert state["dns_set"] is False


def test_cycle_both_hijack_off(helper_env, monkeypatch):
    """旁路模式：不加 /1 路由、不改 DNS，但仍要加 mapdns 主机路由。

    这条 /32 主机路由是旁路模式下唯一验证隧道的入口（dig @198.18.0.2），
    且 198.18.0.0/15 是保留测试段，不影响任何本机流量。
    """
    src, fake = helper_env
    monkeypatch.setattr(tunshell, "_find_utun", lambda: "utun9")
    _intent(enabled=True, hijack_route=False, hijack_dns=False)
    state = _cycle()
    assert state["active"] is True and state["dns_set"] is False
    joined = [" ".join(c) for c in fake.calls]
    # 不加全量路由
    assert not any("route -n add -net 0.0.0.0/1" in j for j in joined)
    assert not any("route -n add -net 128.0.0.0/1" in j for j in joined)
    # 不动 DNS
    assert not any("setdnsservers Wi-Fi 198.18.0.2" in j for j in joined)
    # 但 mapdns 主机路由必须加（旁路模式的验证入口）
    assert any("route -n add -host 198.18.0.2 -interface utun9" in j for j in joined)


def test_cycle_offnetwork_skips_dns_hijack(helper_env, monkeypatch):
    """目标服务离线（只有链路本地地址）→ 不尝试 DNS 劫持，dns_unavailable=True。

    隧道数据面（/1 路由）照常建立；系统 DNS 完全不被触碰（写了也不会被采纳）。
    """
    src, fake = helper_env
    monkeypatch.setattr(tunshell, "_find_utun", lambda: "utun9")
    fake.online = False          # off-network：en0 只有 169.254.x
    _intent(enabled=True)
    state = _cycle()
    assert state["active"] is True and state["utun"] == "utun9"
    assert state["dns_set"] is False
    assert state["dns_unavailable"] is True
    joined = [" ".join(c) for c in fake.calls]
    # 数据面照常
    assert any("route -n add -net 0.0.0.0/1 -interface utun9" in j for j in joined)
    # DNS 完全没碰
    assert not any("setdnsservers Wi-Fi 198.18.0.2" in j for j in joined)


def test_cycle_dns_set_but_resolver_unadopted(helper_env, monkeypatch):
    """服务在线但 resolver 不采纳 mapdns（networksetup 假成功）→ 如实上报不可用。

    setdnsservers 确实执行且返回 0，但 resolver 没变；dns_set 保持 False、
    dns_unavailable=True，且不回滚整条隧道（数据面保留）。
    """
    src, fake = helper_env
    monkeypatch.setattr(tunshell, "_find_utun", lambda: "utun9")
    fake.accept_dns = False      # rc=0 但系统 DNS 纹丝不动
    _intent(enabled=True)
    state = _cycle()
    assert state["active"] is True
    assert state["dns_set"] is False
    assert state["dns_unavailable"] is True
    joined = [" ".join(c) for c in fake.calls]
    assert any("setdnsservers Wi-Fi 198.18.0.2" in j for j in joined)


def test_cycle_service_unknown_leaves_system_network_untouched(helper_env, monkeypatch):
    """读不出网络服务名 ⇒ 系统 DNS / IPv6 **一项都不动**，并如实上报（fail-closed）。

    networksetup 的服务名是本地化的（德语 WLAN…），反查用的设备名也可能拿不到。旧写法
    在这里兜一个猜来的字面量 "Wi-Fi"：猜错就把 DNS / IPv6 写到**另一个**网络服务上 ——
    那个服务日后启用时指向一个早已不存在的 mapdns（198.18.0.2），而现场毫无痕迹。
    故读不出即不接管，三条一起守：

    - 数据面照常（两条 /1 ＋ mapdns 主机路由都建）—— 隧道本身是好的，不该被连累；
    - 系统 DNS / IPv6 一根手指都没碰（断言看**命令行**，不看 helper 的 ok 回显）；
    - 拆除那一轮也不留半截状态（没碰过就没有要还原的）。
    """
    src, fake = helper_env
    monkeypatch.setattr(tunshell, "_find_utun", lambda: "utun9")
    # 设备名照常解析出来（en0），只有「设备名 → 服务名」这一步读不出
    monkeypatch.setattr(tunwifi, "_service_for_device", lambda iface: None)
    _intent(enabled=True)

    state = _cycle()
    assert state["active"] is True and state["utun"] == "utun9"
    assert state["service_unknown"] is True
    assert state["dns_set"] is False and state["dns_unavailable"] is False

    joined = [" ".join(c) for c in fake.calls]
    assert any("route -n add -net 0.0.0.0/1 -interface utun9" in j for j in joined)
    assert any("route -n add -host 198.18.0.2 -interface utun9" in j for j in joined)
    # 系统 DNS / IPv6 完全没碰：连读原值（getdnsservers）那一步都不该发生
    assert not any("setdnsservers" in j for j in joined)
    assert not any("getdnsservers" in j for j in joined)
    assert not any("setv6off" in j for j in joined)

    # 拆除那一轮：没碰过 ⇒ 没有任何要还原的（也不许拿猜来的名字去写）
    _intent(enabled=False)
    _cycle()
    joined2 = [" ".join(c) for c in fake.calls]
    assert not any("setdnsservers" in j for j in joined2)
    assert not any("setv6automatic" in j for j in joined2)


def test_hijack_marker_carries_the_network_service(helper_env):
    """服务名随劫持标记落盘 —— 崩溃回收跑在**新进程**里，内存缓存是空的。

    不带它，回收时只能猜（旧写法兜 "Wi-Fi"）：非英文系统上写错服务，真正被劫持的那个
    永远还原不回去，而日志与面板都看不出异常。
    """
    src, fake = helper_env
    tunconsts._TUNNEL_SERVICE = "WLAN"          # 非英文系统上的真实服务名
    tunconsts._TUNNEL_ROUTE_HIJACKED = True      # 有劫持才写标记
    tunnelops._sync_hijack_marker()
    payload = json.loads(safeio.hijack_marker_path().read_text(encoding="utf-8"))
    assert payload["service"] == "WLAN"


def test_recover_stale_tunnel_restores_to_the_marker_service(helper_env, monkeypatch):
    """崩溃回收：还原写回**原来那个**服务，而不是猜一个。

    这正是旧写法里字面量 "Wi-Fi" 唯一真正承重的地方 —— 回收在新进程里跑，
    ``_TUNNEL_SERVICE`` 缓存是空的，标记不带服务名就只能猜。
    """
    from barkit.capabilities.helper import lifecycle

    src, fake = helper_env
    monkeypatch.setattr(lifecycle, "_boot_time", lambda: 0.0)
    monkeypatch.setattr(lifecycle, "_pid_alive", lambda pid: False)
    safeio.hijack_marker_path().write_text(json.dumps({
        "pid": 999999, "hev_pid": None, "utun": "utun9", "service": "WLAN",
        "route": False, "dns": True, "ipv6_off": False, "ts": 1700000000,
    }), encoding="utf-8")
    tunconsts._TUNNEL_UTUN = None

    lifecycle._recover_stale_tunnel()
    joined = [" ".join(c) for c in fake.calls]
    assert any("setdnsservers WLAN empty" in j for j in joined)   # 原无自定义 DNS ⇒ empty
    assert not any("Wi-Fi" in j for j in joined)                  # 一次都没猜


def _dns_writes(fake):
    return [c for c in fake.calls if "setdnsservers" in " ".join(c)
            and "198.18.0.2" in " ".join(c)]


def test_ensure_tunnel_reasserts_dns_after_it_is_lost(helper_env, monkeypatch):
    """保活回归：系统 DNS 被外部改回去后，下一轮必须补写，而不是放弃自愈。

    DNS 会被 Wi-Fi 重连 / DHCP 续租 / 睡眠唤醒冲掉（路由侧每轮幂等补，DNS 同理）。
    若只在首轮写一次，就会长期停在「IPv4 走隧道、DNS 走本机」的泄漏态。
    """
    src, fake = helper_env
    monkeypatch.setattr(tunshell, "_find_utun", lambda: "utun9")
    _intent(enabled=True)

    first = _cycle()
    assert first["dns_set"] is True and first["dns_unavailable"] is False
    assert len(_dns_writes(fake)) == 1

    # 模拟系统事件把 DNS 冲回原值
    fake.dns = None
    second = _cycle()
    # 关键：补写了第二次
    assert len(_dns_writes(fake)) == 2
    assert second["active"] is True
    # 补写生效 → 状态如实回到已劫持（而不是恒 False）
    assert second["dns_set"] is True and second["dns_unavailable"] is False


def test_ensure_tunnel_does_not_rewrite_when_dns_holds(helper_env, monkeypatch):
    """保活幂等：resolver 仍用着 mapdns 时不重复写，但状态仍如实报 dns_set=True。

    这是 P0 的另一半——之前的实现会让 dns_set 从第 2 轮起恒为 False，
    与 hijack 标记里的 dns=True 自相矛盾。
    """
    src, fake = helper_env
    monkeypatch.setattr(tunshell, "_find_utun", lambda: "utun9")
    _intent(enabled=True)

    first = _cycle()
    assert first["dns_set"] is True
    second = _cycle()
    assert second["dns_set"] is True
    assert second["dns_unavailable"] is False
    # 仍只有首轮那一次写入（幂等，不每轮无脑重设）
    assert len(_dns_writes(fake)) == 1


def test_ensure_tunnel_reports_unavailable_when_reassert_ignored(helper_env, monkeypatch):
    """保活轮补写后 resolver 仍不采纳 → 报 dns_unavailable，但隧道保持生效。"""
    src, fake = helper_env
    monkeypatch.setattr(tunshell, "_find_utun", lambda: "utun9")
    _intent(enabled=True)
    assert _cycle()["dns_set"] is True

    fake.accept_dns = False      # 之后系统不再接受任何 DNS 修改
    fake.dns = None              # 且当前已被冲掉
    state = _cycle()
    assert state["active"] is True
    assert state["dns_set"] is False
    assert state["dns_unavailable"] is True


# ── 保活轮的 utun 缓存（2026-10-12 评审：稳态不再每轮全量扫） ─────────────────

def test_ensure_tunnel_cache_hit_skips_full_scan(helper_env, monkeypatch):
    """缓存命中 ⇒ 单次验活代替全量扫，`_find_utun` 整轮零调用，路由照常补。

    旧写法 `_find_utun() or 缓存` 把全量扫放在第一位，hev 存活期间接口名不变，
    每轮白扫（`ifconfig -l` ＋ 每个 utun 各一次）。"""
    src, fake = helper_env
    monkeypatch.setattr(tunshell, "_find_utun", lambda: "utun9")
    _intent(enabled=True)
    assert _cycle()["active"] is True
    assert tunconsts._TUNNEL_UTUN == "utun9"   # 激活路径已写缓存

    scans = []
    monkeypatch.setattr(tunshell, "_find_utun", lambda: scans.append("scan"))
    second = _cycle()
    assert scans == []                          # 缓存命中 ⇒ 不扫
    assert second["active"] is True


def test_ensure_tunnel_stale_cache_falls_back_to_full_scan(helper_env, monkeypatch):
    """缓存里的接口被外部销毁 ⇒ 验活失败 ⇒ 回退全量扫并回填新名字。"""
    src, fake = helper_env
    monkeypatch.setattr(tunshell, "_find_utun", lambda: "utun9")
    _intent(enabled=True)
    assert _cycle()["active"] is True
    assert tunconsts._TUNNEL_UTUN == "utun9"

    monkeypatch.setattr(tunshell, "_utun_exists", lambda name: False)  # utun9 没了
    monkeypatch.setattr(tunshell, "_find_utun", lambda: "utun11")      # hev 换了号
    state = _cycle()
    assert state["active"] is True
    assert tunconsts._TUNNEL_UTUN == "utun11"   # 自愈且缓存回填


def test_ensure_tunnel_empty_cache_scans_then_backfills(helper_env, monkeypatch):
    """缓存为空（hev 刚从 pending 走出来等场景）⇒ 照旧全量扫，命中即回填。"""
    src, fake = helper_env
    monkeypatch.setattr(tunshell, "_find_utun", lambda: "utun9")
    monkeypatch.setattr(tunshell, "_utun_exists", lambda name: True)
    tunconsts._TUNNEL_UTUN = None
    _intent(enabled=True)

    scans = []

    def _counting():
        scans.append("scan")
        return "utun9"

    monkeypatch.setattr(tunshell, "_find_utun", _counting)
    first = _cycle()
    assert first["active"] is True
    assert scans == ["scan"]                    # 空缓存 ⇒ 扫了一次
    assert tunconsts._TUNNEL_UTUN == "utun9"    # 回填

    monkeypatch.setattr(tunshell, "_find_utun", lambda: scans.append("again"))
    second = _cycle()
    assert second["active"] is True
    assert scans == ["scan"]                    # 下一轮起走缓存


def test_utun_exists_requires_tunnel_address(monkeypatch):
    """缓存验活必须比「接口在」更严：同名接口地址不对时判假（回退全量扫自愈）。

    只判 rc==0 的后果：接口被销毁后同名被别的软件按别的地址重建，缓存会留住一个
    地址不对的 utun 名，后续加路由 / 改 DNS 打在错的接口上。判据与 `_find_utun`
    逐接口那一遍逐字相同（rc==0 ＋ 输出含 TUNNEL_IPV4），且不多花一次 fork。
    """

    class _Cmd:
        def __init__(self, rc, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    # ① 接口在、地址不是隧道地址（被别的软件重建）⇒ 判假
    monkeypatch.setattr(tunshell, "_run", lambda *a, **k: _Cmd(
        0, "utun9: flags=8051<UP,POINTOPOINT,RUNNING>\n\tinet 10.0.0.7 netmask 0xffffff00\n"))
    assert tunshell._utun_exists("utun9") is False

    # ② 接口在、地址是隧道地址 ⇒ 判真
    monkeypatch.setattr(tunshell, "_run", lambda *a, **k: _Cmd(
        0, f"utun9: flags=8051<UP,POINTOPOINT,RUNNING>\n\tinet {tunconsts.TUNNEL_IPV4} netmask 0xffffff00\n"))
    assert tunshell._utun_exists("utun9") is True

    # ③ 接口不在（ifconfig 直接失败）⇒ 判假
    monkeypatch.setattr(tunshell, "_run", lambda *a, **k: _Cmd(1, ""))
    assert tunshell._utun_exists("utun9") is False


# ──────────────────────────────────────────────
# DNS 劫持判定：纯函数单测（直接喂命令输出，不 mock 被测函数本身）
# ──────────────────────────────────────────────

@pytest.mark.parametrize("stdout,expected", [
    ("en0: flags=8863\n\tinet 192.168.1.5 netmask 0xffffff00\n", True),
    ("en0: flags=8863\n\tinet 169.254.33.7 netmask 0xffff0000\n", False),   # 只有链路本地
    ("en0: flags=8863\n\tinet 127.0.0.1 netmask 0xff000000\n", False),      # 回环不算
    ("en0: flags=8863\n\tinet6 fe80::1 prefixlen 64\n", False),             # 纯 IPv6
    ("en0: flags=8863\n", False),                                           # 没拿到地址
    ("en0: flags=8863\n\tinet 169.254.1.2\n\tinet 192.168.1.5\n", True),    # 别名里有真的就算
])
def test_has_ipv4(monkeypatch, stdout, expected):
    monkeypatch.setattr(tunshell, "_run", lambda *a, **k: _Cmd(0, stdout))
    assert tunnelops._has_ipv4("en0") is expected


def test_has_ipv4_no_iface_or_ifconfig_fails(monkeypatch):
    assert tunnelops._has_ipv4("") is False
    monkeypatch.setattr(tunshell, "_run", lambda *a, **k: _Cmd(1, "", "interface does not exist"))
    assert tunnelops._has_ipv4("en9") is False


@pytest.mark.parametrize("stdout,expected", [
    ("resolver #1\n  nameserver[0] : 198.18.0.2\n", True),
    ("resolver #1\n  nameserver[0] : 192.168.1.1\n", False),
    # 不在 #1 也要认：隧道起来后 utun 等也会占 resolver 块，编号顺序无保证
    ("resolver #1\n  nameserver[0] : 192.168.1.1\n"
     "resolver #2\n  nameserver[0] : 198.18.0.2\n", True),
    # 左边界：不能被更长 IP 的子串误命中
    ("resolver #1\n  nameserver[0] : 18.198.18.0.2\n", False),
    ("resolver #1\n  nameserver[0] : 198.18.0.20\n", False),
    ("", False),
])
def test_resolver_adopted_dns(monkeypatch, stdout, expected):
    monkeypatch.setattr(tunshell, "_run", lambda *a, **k: _Cmd(0, stdout))
    assert tunnelops._resolver_adopted_dns("198.18.0.2", timeout=0) is expected


def test_resolver_adopted_dns_scutil_fails(monkeypatch):
    monkeypatch.setattr(tunshell, "_run", lambda *a, **k: _Cmd(1, "", "permission denied"))
    assert tunnelops._resolver_adopted_dns("198.18.0.2", timeout=0) is False


def test_resolver_adopted_dns_respects_budget(monkeypatch):
    """预算用尽即返回 False，且单次 _run 的 timeout 不会超出剩余预算。"""
    seen = []

    def fake_run(args, timeout=None):
        seen.append(timeout)
        return _Cmd(0, "resolver #1\n  nameserver[0] : 192.168.1.1\n")

    monkeypatch.setattr(tunshell, "_run", fake_run)
    monkeypatch.setattr(tunnelops.time, "sleep", lambda s: None)
    assert tunnelops._resolver_adopted_dns("198.18.0.2", timeout=0) is False
    assert seen == [1]      # 只探测一次，且 timeout 被夹到 >=1 而不是默认的 8


def test_ensure_tunnel_upstream_lost_tears_down(helper_env, monkeypatch):
    """上游复检：隧道生效中 app 退出（1080 无监听）→ 保活轮主动拆除，转 pending。

    防「hev 活着但上游死了」的流量黑洞：hijack 的 /1 路由与 DNS 必须撤掉，
    上游恢复后由激活路径自动重建。
    """
    src, fake = helper_env
    monkeypatch.setattr(tunshell, "_find_utun", lambda: "utun9")
    _intent(enabled=True)
    state = _cycle()
    assert state["active"] is True
    # app 退出：上游 SOCKS 桥（bridge @ 127.0.0.1:local_port）消失
    monkeypatch.setattr(tunhev, "_upstream_ready", lambda port, timeout=1.0: False)
    state = _cycle()
    assert state["active"] is False
    assert state["pending"] is True
    joined = [" ".join(c) for c in fake.calls]
    # 劫持的 /1 路由与 DNS 已撤（黑洞解除）
    assert any("route -n delete -net 0.0.0.0/1 -interface utun9" in j for j in joined)
    assert any("setdnsservers Wi-Fi empty" in j for j in joined)


def test_ensure_tunnel_upstream_alive_no_flap(helper_env, monkeypatch):
    """上游正常时保活轮不复位：复检通过就继续幂等补，不拆不重建。"""
    src, fake = helper_env
    monkeypatch.setattr(tunshell, "_find_utun", lambda: "utun9")
    _intent(enabled=True)
    _cycle()
    fake.calls.clear()
    state = _cycle()
    assert state["active"] is True
    joined = [" ".join(c) for c in fake.calls]
    assert not any("route -n delete" in j for j in joined)
    assert not any("hev-socks5-tunnel" in j for j in joined)  # 没有重启 hev


def test_cycle_teardown_only_reverts_hijacked(helper_env, monkeypatch):
    src, fake = helper_env
    monkeypatch.setattr(tunshell, "_find_utun", lambda: "utun9")
    # DNS 劫持关闭 → 不应备份/改 DNS；路由劫持开启 → 应加 /1
    _intent(enabled=True, hijack_dns=False)
    _cycle()
    joined1 = [" ".join(c) for c in fake.calls]
    assert any("route -n add -net 0.0.0.0/1 -interface utun9" in j for j in joined1)
    assert not any("setdnsservers Wi-Fi 198.18.0.2" in j for j in joined1)

    # 停用：只撤路由，绝不把 DNS 重设成 empty（因为压根没劫持过 DNS）
    _intent(enabled=False, hijack_dns=False)
    state = _cycle()
    assert state["active"] is False
    joined2 = [" ".join(c) for c in fake.calls]
    assert any("route -n delete -net 0.0.0.0/1 -interface utun9" in j for j in joined2)
    assert not any("setdnsservers Wi-Fi empty" in j for j in joined2)


# ──────────────────────────────────────────────
# bridge：链路探测（resident 服务；插件侧零线程 / 零定时器）
# ──────────────────────────────────────────────

def _probe(monkeypatch, on_link_change=None, port=9001, **kw):
    """构造探针服务，并把 usbmuxd 交互整体打桩（不碰真实设备）。

    打桩三层：设备解析、开通道、SOCKS5 握手应答 —— 探针的就绪判据到「引擎会不会
    应答」这一层（端口能连 ≠ 引擎会干活）。
    """

    monkeypatch.setattr(bridge_mux, "pick_device", lambda u: (1, "S"))
    monkeypatch.setattr(bridge_mux, "open_device_tunnel", lambda d, p, timeout=None: _fake_sock())
    monkeypatch.setattr(bridge_mux, "socks5_handshake", lambda s, timeout=None: True)
    return LinkProbeService(
        device_port_provider=lambda: port,
        on_link_change=on_link_change,
        **kw,
    )


def test_probe_link_reports_up_and_down(monkeypatch):

    ctrl = _probe(monkeypatch)
    assert ctrl.probe_link() is True

    monkeypatch.setattr(bridge_mux, "pick_device",
                        lambda u: (_ for _ in ()).throw(RuntimeError("no dev")))
    assert ctrl.probe_link() is False


def test_probe_is_declared_not_a_clock(monkeypatch):
    """节律是**数据声明**（框架按时钟调用 poll），不是插件自带的定时器。"""
    ctrl = _probe(monkeypatch)
    assert ctrl.name == "probe" and ctrl.interval == PROBE_INTERVAL
    assert ctrl.device_port == 9001
    assert ctrl.concluded is False          # 未 poll 过 → 未给结论
    import barkit.plugins.ipad_tunnel.bridge as bridge
    assert not hasattr(bridge, "threading")  # 迁移收口：模块内不再 import threading


def test_probe_poll_dispatches_on_change_with_hysteresis(monkeypatch):
    events = []
    ctrl = _probe(monkeypatch, on_link_change=lambda up: events.append(up))
    ctrl.poll()                 # 第 1 次与当前态（down）相反 → 只计票
    assert events == [] and ctrl.concluded is True
    ctrl.poll()                 # 第 2 次 → 翻转
    assert events == [True]
    ctrl.poll()                 # 仍 up，无变化
    assert events == [True]


def test_probe_poll_notifies_every_tick_even_when_the_walk_raises(monkeypatch):
    """每轮 poll 都给一次「当前链路结论」（电平）；``on_link_change`` 只在翻转那一次响（边沿）。

    按需型读取（iOS 端签名通告）拿它当低频重试的点火源 —— 所以它必须**每轮都到**，
    包括判据自己出异常那一轮（那一次不到，重试链就断了）。
    """
    import pytest

    import barkit.plugins.ipad_tunnel.bridge as bridge

    flips, ticks = [], []
    ctrl = _probe(monkeypatch, on_link_change=flips.append, on_poll=ticks.append)
    _stub_measure(monkeypatch, ctrl, bridge.LINK_OK)
    ctrl.poll(); ctrl.poll(); ctrl.poll()        # 迟滞 2 票后才翻 up
    assert flips == [True]                       # 边沿：只报一次
    # 电平：每轮都报，报的是**该轮判定之后**的结论 —— 所以翻转那一轮就已经是 True
    # （同一轮里 on_link_change 先响、on_poll 后响；通告的「翻转即拉」靠前者、
    #   「还没拿到则低频重试」靠后者，后者被前者刚记下的下次时刻挡住，不会一问两遍）。
    assert ticks == [False, True, True]

    ticks.clear()
    monkeypatch.setattr(ctrl, "_poll_walk",
                        lambda: (_ for _ in ()).throw(RuntimeError("判据炸了")))
    with pytest.raises(RuntimeError):
        ctrl.poll()                              # 异常照旧往上抛（框架会记走）
    assert ticks == [True]                       # ★ 但那一次通知仍然发得出去


def test_probe_hysteresis_resists_flapping(monkeypatch):
    """抖动序列永不翻转；稳定序列两次才翻。"""
    import barkit.plugins.ipad_tunnel.bridge as bridge

    events = []
    ctrl = _probe(monkeypatch, on_link_change=events.append)
    results = iter([True, False, True, False, True, False])   # 交替抖动
    monkeypatch.setattr(
        ctrl, "_measure",
        lambda force_full=False: ((None, bridge.LINK_OK) if next(results)
                                  else ("设备不在", bridge.LINK_NO_DEVICE)))
    for _ in range(6):
        ctrl.poll()
    assert events == []             # 抖动下 on_link_change 从不触发

    ctrl2 = _probe(monkeypatch, on_link_change=lambda up: events.append(("c2", up)))
    results2 = iter([True, True, False, False])
    monkeypatch.setattr(
        ctrl2, "_measure",
        lambda force_full=False: ((None, bridge.LINK_OK) if next(results2)
                                  else ("设备不在", bridge.LINK_NO_DEVICE)))
    for _ in range(4):
        ctrl2.poll()
    assert events == [("c2", True), ("c2", False)]   # 稳定两次各翻一次


# ──────────────────────────────────────────────
# bridge：判据分层（2026-09-20 自激回路修复）
#   准入期 = 完整探测说了算；运行期 = 只有「设备真消失」才算真断；
#   数据面健康由中继自报；发布 false 后进冷却。
# ──────────────────────────────────────────────

def _stub_measure(monkeypatch, ctrl, kind, reason="boom"):
    import barkit.plugins.ipad_tunnel.bridge as bridge

    if kind == bridge.LINK_OK:
        reason = None
    monkeypatch.setattr(ctrl, "_measure", lambda force_full=False: (reason, kind))


def test_probe_runtime_channel_timeout_never_tears_down(monkeypatch):
    """回归：隧道接管流量后，探针「量不通」**不得**拆机（自激回路的根）。

    现场签名：57 条会话全卡在 SOCKS 握手、探针走同一条 USB 通道必然 5s 超时；
    旧判据据此写 link_up=false ⇒ helper 拆整机网络 ⇒ 负载消失、探针立刻恢复 ⇒ 重开。
    """
    import barkit.plugins.ipad_tunnel.bridge as bridge

    carrying = [False]
    events = []
    ctrl = _probe(monkeypatch, on_link_change=events.append,
                  mode_provider=lambda: carrying[0])
    _stub_measure(monkeypatch, ctrl, bridge.LINK_OK)
    ctrl.poll(); ctrl.poll()                     # 准入：两次翻 up
    assert events == [True]

    carrying[0] = True                           # 隧道开始接管流量
    _stub_measure(monkeypatch, ctrl, bridge.LINK_MUX_BUSY, "经 usbmuxd 打通超时")
    for _ in range(6):
        ctrl.poll()
    assert events == [True]                      # 一条 false 都不许发
    assert ctrl.link_up is True


def test_probe_runtime_device_gone_tears_down(monkeypatch):
    """运行期唯一允许拆机的真断：USB 设备消失。"""
    import barkit.plugins.ipad_tunnel.bridge as bridge

    events = []
    ctrl = _probe(monkeypatch, on_link_change=events.append, mode_provider=lambda: True)
    _stub_measure(monkeypatch, ctrl, bridge.LINK_OK, reason=None)
    ctrl._publish(True, 0)                       # 现实态：接管流量中（link_up 已为真）
    events.clear()

    _stub_measure(monkeypatch, ctrl, bridge.LINK_NO_DEVICE, "未找到 iPad USB 设备")
    ctrl.poll()
    assert events == []                          # 第一票
    ctrl.poll()
    assert events == [False]                     # 两票后发布断开


def test_probe_cooldown_blocks_auto_restart(monkeypatch):
    """发布 false 之后冷却期内不重开（USB 抖动不反复拆建路由/DNS）。"""
    import barkit.plugins.ipad_tunnel.bridge as bridge

    carrying = [True]
    events = []
    ctrl = _probe(monkeypatch, on_link_change=events.append,
                  mode_provider=lambda: carrying[0])
    _stub_measure(monkeypatch, ctrl, bridge.LINK_OK)
    ctrl._publish(True, 0)

    _stub_measure(monkeypatch, ctrl, bridge.LINK_NO_DEVICE, "未找到 iPad USB 设备")
    ctrl.poll(); ctrl.poll()                     # 设备消失 → 发布 false
    assert events[-1] is False
    events.clear()

    carrying[0] = False                          # 设备回来（插回 USB）
    _stub_measure(monkeypatch, ctrl, bridge.LINK_OK)
    for _ in range(3):                           # 三票足够翻转，但冷却未过
        ctrl.poll()
    assert events == []                          # 冷却期内不重开
    for _ in range(bridge.PROBE_COOLDOWN_POLLS + 2):
        ctrl.poll()
    assert events == [True]                      # 冷却过后才翻回 up


def test_probe_data_plane_hop_tears_down_once(monkeypatch):
    """阶梯第 ① 级判死（②③④ 又刚实测全过）⇒ 立即拆，不等票；拆过之后不追着拆。"""
    import barkit.plugins.ipad_tunnel.bridge as bridge

    plane = [None]
    events = []
    ctrl = _probe(monkeypatch, on_link_change=events.append,
                  mode_provider=lambda: True,
                  data_plane_state_provider=lambda: plane[0],
                  data_plane_reset=lambda: plane.__setitem__(0, None))
    _stub_measure(monkeypatch, ctrl, bridge.LINK_OK)
    ctrl._publish(True, 0)
    events.clear()

    plane[0] = False
    ctrl.poll()
    assert ctrl.last_hop == bridge.HOP_DATA      # 结论：②③④ 全过，卡在数据面这一级
    assert events == [False]                     # 立即拆（中继已按窗口过滤过）
    assert plane[0] is None                      # 顺手给了新起跑线
    for _ in range(4):
        ctrl.poll()
    assert events == [False]                     # 拆过之后不追着拆


def test_probe_flip_resets_data_plane_readout(monkeypatch):
    """链路翻转必须给阶梯第 ① 级一条新起跑线。

    2026-09-22 真机的形状：旧链路的结论原样算到新链路头上，冷却过后一重开，判据在
    **第一个探针周期**内就成立 ⇒ 刚建起来的隧道立刻又被拆（18:46:53 开、18:46:56 拆，
    只隔一个周期），真机上就是「两分钟一轮的整机断网」。

    两半都验：复位必须真发生；复位之后新链路若还继续搬不动字节，照样要拆 ——
    给的是新起点，不是免死金牌。
    """
    import barkit.plugins.ipad_tunnel.bridge as bridge

    plane = [None]
    resets = []
    events = []

    def _reset():
        resets.append(1)
        plane[0] = None

    ctrl = _probe(monkeypatch, on_link_change=events.append,
                  mode_provider=lambda: True,
                  data_plane_state_provider=lambda: plane[0],
                  data_plane_reset=_reset)
    _stub_measure(monkeypatch, ctrl, bridge.LINK_OK)
    ctrl._publish(True, 0)                       # 首轮就绪（复位回调这时也在跑，无害）
    events.clear()
    resets_after_start = len(resets)

    plane[0] = False                             # 第 ① 级判死
    ctrl.poll()
    assert events == [False]                     # 判据照旧拆机
    assert plane[0] is None                      # ★ 复位真发生了（新起跑线）
    assert len(resets) == resets_after_start + 1

    ctrl._cooldown_polls = 0                     # 冷却过后重开（迟滞要两票）
    ctrl.poll(); ctrl.poll()
    assert events == [False, True]
    ctrl.poll(); ctrl.poll()                     # 再跑两个周期
    assert events == [False, True]               # ★ 重开后没有立刻又被拆

    plane[0] = False                             # 控制组：新链路真的又搬不动字节
    ctrl.poll()
    assert events == [False, True, False]        # 照拆 —— 判据没被关掉


def test_probe_admission_requires_socks_handshake(monkeypatch):
    """端口能连 ≠ 引擎会干活：准入判据必须验到 SOCKS5 握手应答。"""
    import barkit.plugins.ipad_tunnel.bridge as bridge

    ctrl = _probe(monkeypatch)
    assert ctrl.probe_link() is True
    monkeypatch.setattr(bridge_mux, "socks5_handshake", lambda s, timeout=None: False)
    assert ctrl.probe_link() is False
    assert ctrl._measure(force_full=True)[1] == bridge.LINK_ENGINE_SILENT


def test_probe_classifies_mux_busy_vs_no_device(monkeypatch):
    """两种失败必须分开：通道忙（量不出来）vs 设备不在（真断）。"""
    import barkit.plugins.ipad_tunnel.bridge as bridge

    ctrl = _probe(monkeypatch)
    monkeypatch.setattr(bridge_mux, "pick_device",
                        lambda u: (_ for _ in ()).throw(bridge.DeviceNotFound("empty")))
    assert ctrl._measure()[1] == bridge.LINK_NO_DEVICE
    monkeypatch.setattr(bridge_mux, "pick_device",
                        lambda u: (_ for _ in ()).throw(bridge.MuxError("busy")))
    assert ctrl._measure()[1] == bridge.LINK_MUX_BUSY
    monkeypatch.setattr(bridge_mux, "pick_device",
                        lambda u: (_ for _ in ()).throw(OSError("timeout")))
    assert ctrl._measure()[1] == bridge.LINK_MUX_BUSY
    # 量不出来**既不算通过也不算失败**：探针不改判定（旧版运行期把它默认成「健康」）
    assert bridge._HOP_BY_KIND[bridge.LINK_MUX_BUSY] == bridge.HOP_UNKNOWN


# ──────────────────────────────────────────────
# bridge：阶梯判据（逐级回退；成本从低到高，通过就停）
# ──────────────────────────────────────────────

def test_probe_stops_at_data_plane_when_bytes_flow(monkeypatch):
    """★ 阶梯的核心不变量：**第 ① 级通过就停**，绝不去碰更贵的那几级。

    「不与数据面抢 USB 通道」现在靠这条成立（2026-09-20 那版是靠「运行期整条砍掉」实现
    的，代价是运行期**没有任何东西在验端到端**）。所以这里数的是真开了几条设备通道 ——
    数「有没有调 pick_device」也算，因为连设备列表都不该问。
    """
    import barkit.plugins.ipad_tunnel.bridge as bridge

    opened = []

    def _pick(_udid):
        opened.append("list")
        return (1, "S")

    def _open(_d, _p, timeout=None):
        opened.append("open")
        return _fake_sock()

    monkeypatch.setattr(bridge_mux, "pick_device", _pick)
    monkeypatch.setattr(bridge_mux, "open_device_tunnel", _open)
    monkeypatch.setattr(bridge_mux, "socks5_handshake", lambda s, timeout=None: True)

    plane = [True]
    ctrl = LinkProbeService(device_port_provider=lambda: 9001,
                            mode_provider=lambda: True,
                            data_plane_state_provider=lambda: plane[0])
    for _ in range(5):
        ctrl.poll()
    assert ctrl.last_hop is None
    assert opened == []                     # ★ 一次都没下探

    # 控制组：数据面一说不通，立刻逐级下探（不是「永远不下探」）
    plane[0] = False
    ctrl.poll()
    assert opened == ["list", "open"] and ctrl.last_hop == bridge.HOP_DATA


def test_probe_reports_the_shallowest_failing_hop(monkeypatch):
    """逐级回退：哪一级先失败就报哪一级，**且不再往下探**（省下一次没意义的等待）。"""
    import barkit.plugins.ipad_tunnel.bridge as bridge

    calls = []
    ctrl = _probe(monkeypatch)

    # ② 设备不在 ⇒ 报 device；设备都没找着，没道理去开通道
    monkeypatch.setattr(bridge_mux, "pick_device",
                        lambda u: (_ for _ in ()).throw(bridge.DeviceNotFound("empty")))
    monkeypatch.setattr(bridge_mux, "open_device_tunnel",
                        lambda d, p, timeout=None: calls.append("open") or _fake_sock())
    assert ctrl._walk()[0] == bridge.HOP_DEVICE
    assert calls == []

    # ③ 通道打不开（usbmuxd 明确回「设备端拒绝」）⇒ 报 channel；通道都没开，没道理去握手
    monkeypatch.setattr(bridge_mux, "pick_device", lambda u: (1, "S"))
    monkeypatch.setattr(bridge_mux, "open_device_tunnel",
                        lambda d, p, timeout=None: (_ for _ in ()).throw(
                            bridge.ConnectRefused("usbmuxd Connect 失败：Number 3")))
    monkeypatch.setattr(bridge_mux, "socks5_handshake",
                        lambda s, timeout=None: calls.append("shake") or True)
    assert ctrl._walk()[0] == bridge.HOP_CHANNEL
    assert calls == []

    # ④ 端口通、握手无应答 ⇒ 报 socks
    monkeypatch.setattr(bridge_mux, "open_device_tunnel",
                        lambda d, p, timeout=None: calls.append("open") or _fake_sock())
    monkeypatch.setattr(bridge_mux, "socks5_handshake", lambda s, timeout=None: False)
    assert ctrl._walk()[0] == bridge.HOP_SOCKS
    assert calls[-1] == "open"

    # ① 四级全过 ⇒ 没有失败层
    monkeypatch.setattr(bridge_mux, "socks5_handshake", lambda s, timeout=None: True)
    assert ctrl._walk()[0] is None


def test_probe_pauses_relay_before_touching_the_channel(monkeypatch):
    """下探 ③④（要碰 USB 通道）之前必须让中继停手：不停就会量到我们自己的噪音。

    2026-09-20 那场自激回路的根正是「探针把『自己太忙』读成『链路断了』」。准入期不必
    叫停 —— 那时隧道没接管流量，没有数据面要保护，也没有噪音。
    """
    pauses = []
    ctrl = _probe(monkeypatch, mode_provider=lambda: True,
                  descend_pause=lambda: pauses.append(1))
    ctrl.poll()
    assert pauses == [1]
    assert ctrl.last_hop is None

    # 反证：准入期不叫停
    pauses.clear()
    ctrl2 = _probe(monkeypatch, descend_pause=lambda: pauses.append(1))
    ctrl2.poll()
    assert pauses == []

    # 反证：第 ① 级通过时，连「让路」都不该发生（压根不下探）
    pauses.clear()
    ctrl3 = _probe(monkeypatch, mode_provider=lambda: True,
                   data_plane_state_provider=lambda: True,
                   descend_pause=lambda: pauses.append(1))
    ctrl3.poll()
    assert pauses == []


def test_connect_failure_codes_are_not_conflated(monkeypatch):
    """usbmuxd 的「Connect 失败」有两种，段位完全不同，**必须分开报**。

    实测依据（2026-09-23 本机，不是照抄文档）：伪 DeviceID（9999 / 0 / 42）一律回结果码
    ``2``；往 iPad 上没在听的端口连回结果码 ``3``。旧版把两者都当「端口被拒」⇒ 2026-09-22
    的现场（清一色码 2，其实是本机缓存的设备号过期）被念成「iPad 端口没在听」，把人指错。
    """
    import barkit.plugins.ipad_tunnel.bridge as bridge

    ctrl = _probe(monkeypatch)
    cases = (
        (bridge.RESULT_BADDEV, bridge.LINK_DEVICE_CHANGED, bridge.HOP_DEVICE),
        (bridge.RESULT_CONNREFUSED, bridge.LINK_PORT_REFUSED, bridge.HOP_CHANNEL),
        # 其他码：说不出是哪一段就**别硬指** ⇒ 归「量不出来」
        (1, bridge.LINK_MUX_BUSY, bridge.HOP_UNKNOWN),
    )
    for code, kind, hop in cases:
        monkeypatch.setattr(bridge_mux, "pick_device", lambda u: (1, "S"))
        monkeypatch.setattr(
            bridge_mux, "open_device_tunnel",
            lambda d, p, timeout=None, _c=code: (_ for _ in ()).throw(
                bridge._connect_error(_c, {"MessageType": "Result", "Number": _c})))
        ctrl._last_deep_at = 0.0
        got_hop, _reason, got_kind = ctrl._walk()
        assert (got_kind, got_hop) == (kind, hop), f"结果码 {code} 分错了段位"


def test_open_device_tunnel_maps_result_codes(monkeypatch):
    """``open_device_tunnel`` 要按结果码抛**指名道姓**的异常（且都是 RuntimeError 子类）。"""
    import barkit.plugins.ipad_tunnel.bridge as bridge

    class _Sock:
        def close(self):
            pass

    monkeypatch.setattr(bridge_mux, "_mux_connect", lambda d, p, t: _Sock())
    for code, exc in ((bridge.RESULT_BADDEV, bridge.DeviceUnknown),
                      (bridge.RESULT_CONNREFUSED, bridge.ConnectRefused)):
        monkeypatch.setattr(bridge_mux, "_recv_plist",
                            lambda s, _c=code: {"MessageType": "Result", "Number": _c})
        with pytest.raises(exc):
            bridge.open_device_tunnel(1, 9001)
        assert issubclass(exc, RuntimeError)   # 既有的 except RuntimeError 一个都不用改
    monkeypatch.setattr(bridge_mux, "_recv_plist",
                        lambda s: {"MessageType": "Result", "Number": 0})
    assert bridge.open_device_tunnel(1, 9001) is not None


# ──────────────────────────────────────────────
# bridge：中继（resident 服务；端口热改在循环内收敛）
# ──────────────────────────────────────────────

def _free_port() -> int:
    """取一个当前空闲的本地端口（先绑 0 再释放；测试用，冲突概率可忽略）。"""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _connect_ok(port: int) -> bool:
    import socket
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


class _RelayHost:
    """最小宿主桩：ResidentHost 只要求 id + resident_services()。"""

    id = "relay_test"

    def __init__(self, *services):
        self._services = list(services)

    def resident_services(self):
        return list(self._services)


def _no_device(monkeypatch):
    """让任何被接受的连接都快速失败，避免测试碰真实 usbmuxd。"""
    monkeypatch.setattr(bridge_mux, "pick_device",
                        lambda u: (_ for _ in ()).throw(RuntimeError("no dev")))


def _wait(pred, timeout=5.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_relay_listens_when_declared_and_releases_port_on_stop(monkeypatch):
    from barkit.core.resident import ResidentHost

    _no_device(monkeypatch)
    port = _free_port()
    relay = RelayService(ports_provider=lambda: (port, 9001))
    host = ResidentHost()
    plugin = _RelayHost(relay)

    host.start(plugin)
    assert _wait(lambda: relay.listening)
    assert relay.local_port == port and relay.device_port == 9001
    assert _connect_ok(port) is True                     # 1080 类端口真的在听

    host.stop(plugin)
    assert relay.listening is False
    assert _connect_ok(port) is False                    # 停止即释放端口


def test_relay_converges_port_change_inside_loop(monkeypatch):
    """端口热改：同一循环内「关旧 → 开新」，不重建实例、不重启线程。"""
    from barkit.core.resident import ResidentHost

    _no_device(monkeypatch)
    p1, p2 = _free_port(), _free_port()
    state = {"ports": (p1, 9001)}
    relay = RelayService(ports_provider=lambda: state["ports"])
    host = ResidentHost()
    plugin = _RelayHost(relay)
    host.start(plugin)
    assert _wait(lambda: relay.listening and relay.local_port == p1)
    assert _connect_ok(p1) is True

    state["ports"] = (p2, 9001)                # 配置热改
    assert _wait(lambda: relay.local_port == p2)
    assert _connect_ok(p2) is True
    assert _connect_ok(p1) is False            # 旧端口已释放
    assert host.thread_names() == ["resident:relay_test.relay"]   # 线程没重建
    host.stop(plugin)


def test_relay_gate_closes_and_reopens_the_port(monkeypatch):
    """监听门（v2 契约的承重墙）：判据说「不该听」就放掉端口，说「可以听」再占回来。

    为什么它必须在**中继循环里**判、而不是靠起停这个服务：helper 那侧只认
    「127.0.0.1:local_port 有没有人在听」，链路会来回翻，门就得能反复开合，
    而反复拆建线程既贵又容易漏（见下一条回归）。通知只在**状态真变**时发一次。
    """
    from barkit.core.resident import ResidentHost

    _no_device(monkeypatch)
    port = _free_port()
    ready = {"v": False}
    changes: list = []
    relay = RelayService(ports_provider=lambda: (port, 9001),
                         link_ready_provider=lambda: ready["v"],
                         on_listen_change=changes.append)
    host = ResidentHost()
    plugin = _RelayHost(relay)
    host.start(plugin)

    # 门关着：不占端口，也不发通知（没变过状态，敲门是空的）
    assert _wait(lambda: relay._bridge is None)
    assert _connect_ok(port) is False
    assert changes == []
    assert host.thread_names() == ["resident:relay_test.relay"]   # 线程照常在跑

    ready["v"] = True
    assert _wait(lambda: _connect_ok(port))
    assert changes == [True]

    ready["v"] = False
    assert _wait(lambda: not _connect_ok(port))
    assert changes == [True, False]

    ready["v"] = True
    assert _wait(lambda: _connect_ok(port))
    assert changes == [True, False, True]      # 开合可反复
    host.stop(plugin)


def test_relay_gate_defaults_to_open_without_a_judge(monkeypatch):
    """对照组：没注入判据一律恒真（CLI 的 ``main()`` / 直连场景不设门）。

    这条与上一条成对：证明那道门是**判据驱动的**，不是「把中继关掉」。
    """
    from barkit.core.resident import ResidentHost

    _no_device(monkeypatch)
    port = _free_port()
    relay = RelayService(ports_provider=lambda: (port, 9001))     # 没有 link_ready_provider
    host = ResidentHost()
    host.start(_RelayHost(relay))
    assert _wait(lambda: _connect_ok(port))
    host.stop(_RelayHost(relay))


def test_relay_restarts_after_stop(monkeypatch):
    """回归：同一实例被停掉、再被框架起回来，必须真的重新监听。

    ``stop()`` 只置 ``_stopped``、没人清它 —— ``run()`` 开头少了复位那一句，
    第二次被 spawn 起来时循环条件一上来就是假、线程**静默正常退出**。
    真机形状：隧道开关关一次再打开，中继就再也不监听（直到重启 app），
    而界面上开关明明是「开」的。框架反复起停同一批对象是常态（声明差量收敛）。
    """
    from barkit.core.resident import ResidentHost

    _no_device(monkeypatch)
    port = _free_port()
    relay = RelayService(ports_provider=lambda: (port, 9001))
    host = ResidentHost()
    plugin = _RelayHost(relay)

    host.start(plugin)
    assert _wait(lambda: _connect_ok(port))
    host.stop(plugin)
    assert _connect_ok(port) is False

    host.start(plugin)                     # 第二次托管同一实例
    assert _wait(lambda: _connect_ok(port))
    host.stop(plugin)


def test_relay_reports_problem_but_keeps_running(monkeypatch):
    """循环体异常：上报为插件问题并继续跑，不静默退出。"""
    from barkit.core.resident import ResidentHost

    port = _free_port()
    relay = RelayService(ports_provider=lambda: (port, 9001))
    host = ResidentHost()
    plugin = _RelayHost(relay)
    host.start(plugin)
    assert _wait(lambda: relay.listening)

    calls = []
    relay.report_problem = calls.append          # 模拟框架注入
    relay._pause = lambda seconds: None          # 不等待，加速测试

    original = relay._bridge.serve_once

    def boom(*a, **k):
        raise RuntimeError("loop boom")

    relay._bridge.serve_once = boom
    assert _wait(lambda: calls)
    assert "中继事件循环异常" in calls[0]
    assert _wait(lambda: relay.listening)        # 自动重建监听，仍在跑
    relay._bridge.serve_once = original
    host.stop(plugin)


def _fake_sock():
    import socket
    return socket.socket(socket.AF_INET, socket.SOCK_STREAM)


# ──────────────────────────────────────────────
# bridge（数据面）：非阻塞接入 / 背压 / 会话统计
#   设备通道用 socketpair 顶替，全部不碰真实设备。
# ──────────────────────────────────────────────

def _bridge_env(monkeypatch):
    """建一个只听本地 TCP 的 Bridge，返回 (bridge, listener, peers)。

    ``begin_device_tunnel`` 改成返回 socketpair 的一端：应答由用例自己握着另一端写，
    于是「等应答」这段完全可控（可以永远不答，也可以喂一条合法的 Result）。
    """
    import socket

    import barkit.plugins.ipad_tunnel.bridge as bridge

    monkeypatch.setattr(bridge_mux, "pick_device", lambda u: (1, "S"))
    peers: list = []

    def _begin(device_id, port):
        a, b = socket.socketpair()
        peers.append(b)
        return a

    monkeypatch.setattr(bridge_mux, "begin_device_tunnel", _begin)
    br = bridge.Bridge(bridge.DeviceResolver(None), 9001)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    listener.setblocking(False)
    br.register_listener(listener)
    return br, listener, peers


def _teardown_bridge(br, listener, peers, *clients):
    for sock in clients:
        try:
            sock.close()
        except OSError:
            pass
    for p in peers:
        try:
            p.close()
        except OSError:
            pass
    try:
        listener.close()
    except OSError:
        pass
    br.close()


def test_bridge_accept_does_not_block_on_device_reply(monkeypatch):
    """回归：开设备通道不再卡住事件循环（旧实现同步等 5s × 2 次重试）。"""
    import socket
    import time as _time

    br, listener, peers = _bridge_env(monkeypatch)
    clients = []
    try:
        for _ in range(3):
            clients.append(socket.create_connection(listener.getsockname()))
        t0 = _time.monotonic()
        for _ in range(4):
            br.serve_once(timeout=0.05)
        dt = _time.monotonic() - t0
        assert dt < 1.0                      # 三路都只是「等应答」，没人被卡住
        assert len(br.pending) == 3 and br.connections == 0
    finally:
        _teardown_bridge(br, listener, peers, *clients)


def test_bridge_pending_promotes_when_reply_arrives(monkeypatch):
    """等到 usbmuxd 的合法 Result 才升级成正式隧道（两段式的第二段）。"""
    import plistlib
    import socket

    import barkit.plugins.ipad_tunnel.bridge as bridge

    br, listener, peers = _bridge_env(monkeypatch)
    client = socket.create_connection(listener.getsockname())
    try:
        br.serve_once(timeout=0.05)
        assert len(br.pending) == 1 and br.connections == 0

        peers[0].sendall(bridge._frame(plistlib.dumps(
            {"MessageType": "Result", "Number": 0})))
        br.serve_once(timeout=0.05)
        assert br.connections == 1 and not br.pending and len(br.tunnels) == 1

        peers[0].sendall(b"hello")           # 数据面搬运一个字节 ⇒ 本窗口算成功
        br.serve_once(timeout=0.05)
        assert br._win_ok == 1
    finally:
        _teardown_bridge(br, listener, peers, client)


def test_bridge_read_treats_eagain_as_no_data_not_eof():
    """回归（2026-10-12 评审）：``_read`` 在非阻塞 socket 上收到 EAGAIN 只能当
    「这一轮没读到」，不能当 EOF——select 报可读后 recv 仍可能抛 EAGAIN（伪唤醒），
    旧实现把它当对端关闭 ⇒ 静默拆掉正在搬运的连接。同文件 ``_write`` /
    ``_poll_pending`` 早已放行 EAGAIN/EINTR，此处对齐。"""
    import errno as _errno
    import socket

    import barkit.plugins.ipad_tunnel.bridge_core as bridge_core

    class _Sock:
        """recv 行为可注入的桩（``_read`` 只碰 recv，无需真 fd）。"""

        def __init__(self, behavior):
            self._behavior = behavior

        def recv(self, _n):
            return self._behavior()

    a, b = socket.socketpair()
    try:
        br = bridge_core.Bridge(bridge_core.DeviceResolver(None), 9001)
        t = bridge_core.Tunnel(a, b)

        # ① EAGAIN：既不置 EOF，也不当数据/成功
        eagain = _Sock(lambda: (_ for _ in ()).throw(
            BlockingIOError(_errno.EAGAIN, "Resource temporarily unavailable")))
        br._read(t, eagain, is_local=True)
        assert not t.eof_local and not t.eof_dev
        assert t.buf_ld == b"" and not t.moved

        # ② 其他 OSError（如 ENOTCONN）仍按 EOF 处理：半关闭传播不能失效
        dead = _Sock(lambda: (_ for _ in ()).throw(
            OSError(_errno.ENOTCONN, "Socket not connected")))
        br._read(t, dead, is_local=False)
        assert t.eof_dev

        # ③ 真 EOF（对端关闭，recv 返回 b""）照旧置 EOF
        b.close()
        br._read(t, a, is_local=True)
        assert t.eof_local
    finally:
        for s in (a, b):
            try:
                s.close()
            except OSError:
                pass


def test_bridge_pending_expires_and_frees_slot(monkeypatch):
    """等应答超过截止时间：放弃该连接（不再像旧实现那样无限重试）。"""
    import socket

    br, listener, peers = _bridge_env(monkeypatch)
    client = socket.create_connection(listener.getsockname())
    try:
        br.serve_once(timeout=0.05)
        assert len(br.pending) == 1
        for pend in br.pending.values():     # 直接把截止时间拨到过去
            pend.deadline = 0.0
        br.serve_once(timeout=0.05)          # 本轮清掉
        assert not br.pending and br.connect_failed == 1
    finally:
        _teardown_bridge(br, listener, peers, client)


def test_bridge_backpressure_stops_accepting_at_pending_limit(monkeypatch):
    """在途上限满了就**停 accept**（背压交给内核 backlog），不是收下来再关。"""
    import socket


    monkeypatch.setattr(bridge_consts, "PENDING_LIMIT", 1)
    br, listener, peers = _bridge_env(monkeypatch)
    clients = []
    try:
        clients.append(socket.create_connection(listener.getsockname()))
        br.serve_once(timeout=0.05)
        assert len(br.pending) == 1 and br._accepting is False

        for pend in br.pending.values():
            pend.deadline = 0.0
        br.serve_once(timeout=0.05)          # 名额腾出 → 恢复 accept
        assert not br.pending and br._accepting is True
    finally:
        _teardown_bridge(br, listener, peers, *clients)


def test_bridge_pause_accept_lets_the_probe_measure_clean(monkeypatch):
    """探针下探期间中继要停收新连接，且**必须自己恢复**（探针中途死掉也不留永久黑洞）。"""
    import socket

    br, listener, peers = _bridge_env(monkeypatch)
    clients = []
    try:
        assert br._accepting is True
        br.pause_accept(5.0)
        # 事件循环每轮都会重算一次 accept（_sync_backpressure）—— 让路期间不许被它翻回来
        br.serve_once(timeout=0.02)
        assert br._accepting is False

        clients.append(socket.create_connection(listener.getsockname()))
        br.serve_once(timeout=0.02)
        assert not br.pending                  # ★ 连接压在 backlog 里，没被收上来

        br.pause_accept(0.0)                   # 期限到 ⇒ 自己恢复（不需要谁记得去开）
        br.serve_once(timeout=0.02)            # 本轮**末尾**才重算 accept ⇒ 重新登记监听
        assert br._accepting is True
        br.serve_once(timeout=0.02)            # 下一轮才把压在 backlog 里的连接收上来
        assert br.pending                      # ★ 连接终于被收上来了
    finally:
        _teardown_bridge(br, listener, peers, *clients)


def test_bridge_window_marks_data_plane_bad_only_after_zero_byte_windows(monkeypatch):
    """会话统计口径：窗口内尝试够多且**零字节**才计「不通」；尝试太少视为无结论。"""
    import barkit.plugins.ipad_tunnel.bridge as bridge

    br, listener, peers = _bridge_env(monkeypatch)
    enough = bridge.DATA_WINDOW_MIN_SESSIONS
    try:
        br._win_attempts = enough
        br._win_ok = 0
        br._win_deadline = 0                 # 让本轮立刻结算
        br.serve_once(timeout=0.01)
        # 一个坏窗口还不够下结论（防抖）⇒ 阶梯第 ① 级仍是「没结论」
        assert br.data_plane_bad_streak == 1 and br.data_plane_state is None

        br._win_attempts = enough
        br._win_ok = 0
        br._win_deadline = 0
        br.serve_once(timeout=0.01)
        assert br.data_plane_bad_streak == 2
        assert br.data_plane_state is False   # ★ 够 DATA_BAD_WINDOWS 了 ⇒ 第 ① 级判死

        br._win_attempts = enough
        br._win_ok = 1                       # 有成功 ⇒ 立刻清零 / 判通
        br._win_deadline = 0
        br.serve_once(timeout=0.01)
        assert br.data_plane_bad_streak == 0
        assert br.data_plane_state is True

        br._win_attempts = 1                 # 太少 ⇒ 无结论，**一个字段都不改**
        br._win_ok = 0
        br._win_deadline = 0
        br.serve_once(timeout=0.01)
        assert br.data_plane_bad_streak == 0 and br.data_plane_state is True
    finally:
        _teardown_bridge(br, listener, peers)


def test_bridge_reply_failure_refreshes_device_id(monkeypatch):
    """回归（2026-09-22 真机）：usbmuxd 回一条失败应答 ⇒ 必须清掉缓存的设备号。

    五条失败出口里只有这一条能暴露「设备号过期」—— usbmuxd 对不认识的 DeviceID 回的正
    是「应答内容本身是失败」，收包报错 / 超时那些出口一个都不会走到。它当时漏了清缓存，
    于是隧道每轮建起来、第一条连接被拒、随即被拆：真机上是两分钟一轮的整机断网。

    两半都验：失败必须清；成功**绝不能**清（否则每次建连都白解析一次设备列表）。
    """
    import plistlib
    import socket

    import barkit.plugins.ipad_tunnel.bridge as bridge

    br, listener, peers = _bridge_env(monkeypatch)
    invalidated = []
    real_invalidate = br.resolver.invalidate
    monkeypatch.setattr(br.resolver, "invalidate",
                        lambda: (invalidated.append(1), real_invalidate()))
    clients = []
    try:
        clients.append(socket.create_connection(listener.getsockname()))
        br.serve_once(timeout=0.05)
        peers[0].sendall(bridge._frame(plistlib.dumps(
            {"MessageType": "Result", "Number": 2})))     # 设备号不认识
        br.serve_once(timeout=0.05)
        assert br.connect_failed == 1 and not br.pending
        assert invalidated == [1] and br.reconnects == 1   # ★ 清了缓存

        clients.append(socket.create_connection(listener.getsockname()))
        br.serve_once(timeout=0.05)
        peers[1].sendall(bridge._frame(plistlib.dumps(
            {"MessageType": "Result", "Number": 0})))     # 合法应答
        br.serve_once(timeout=0.05)
        assert br.connections == 1
        assert invalidated == [1]                          # 反证：成功不清缓存
    finally:
        _teardown_bridge(br, listener, peers, *clients)


def test_bridge_data_plane_fault_survives_link_reset(monkeypatch):
    """空转标记只回答「搬过字节没有」：链路翻转复位计数，**不能**把故障一起抹掉。

    面板那格严重状态（``data_plane_dead``）就靠这枚标记 —— 它要是在重开那一瞬间被清掉，
    用户永远看不到「刚才是空转」。反过来，真搬过字节的窗口必须立刻解除，否则会一直亮。
    """
    import barkit.plugins.ipad_tunnel.bridge as bridge

    br, listener, peers = _bridge_env(monkeypatch)
    enough = bridge.DATA_WINDOW_MIN_SESSIONS
    try:
        for _ in range(bridge.DATA_BAD_WINDOWS):
            br._win_attempts, br._win_ok, br._win_deadline = enough, 0, 0
            br.serve_once(timeout=0.01)
        assert br.data_plane_bad_streak == bridge.DATA_BAD_WINDOWS
        assert br.data_plane_fault is True

        assert br.data_plane_state is False          # 阶梯第 ① 级也判死了

        br.reset_data_plane()                        # 链路翻转
        assert br.data_plane_bad_streak == 0         # 计数给新起点
        # ★ 判据的读数也要复位：留着旧链路的「搬不动」，新链路刚建起来就被第 ① 级判死
        #   ——那正是 18:46:53 开 / 18:46:56 拆（只隔一个探针周期）的形状。
        assert br.data_plane_state is None
        assert br.data_plane_fault is True           # ★ 而「事实」留下（面板那格靠它）

        br._win_attempts, br._win_ok, br._win_deadline = enough, 1, 0
        br.serve_once(timeout=0.01)                  # 真搬过字节 ⇒ 解除
        assert br.data_plane_fault is False
        assert br.data_plane_state is True
    finally:
        _teardown_bridge(br, listener, peers)


# ──────────────────────────────────────────────
# plugin：接线
# ──────────────────────────────────────────────

class _Link:
    """最小会话桩：记下声明出去的 ``(subject, payload)``、发出的命令与订阅。

    ``snapshot()`` 就是组件侧 ``observed("tunnel")`` 的读口 —— 守护推来的那份事实
    在**内存**里，不是某个文件。
    """

    def __init__(self, ok=True, observed=None):
        self.sent: list = []
        self.ops: list = []
        self.subscribed: list = []
        self._ok = ok
        self._observed: dict = dict(observed or {})

    def declare(self, subject, payload):
        self.sent.append((subject, payload))
        return self._ok

    def run_op(self, op, params, callback=None):
        self.ops.append((op, params))
        if callable(callback):
            callback(True, {}, "")
        return self._ok

    def subscribe(self, subjects):
        self.subscribed.append(list(subjects))
        return self._ok

    def snapshot(self, subject, default=None):
        return self._observed.get(subject, default)

    def push(self, subject, observed) -> None:
        """模拟守护推来一份事实（见模块级 ``_push``）。"""
        self._observed[subject] = observed


def _ctx(tmp_path, events=None, helper=None) -> PluginContext:
    user = tmp_path / "config.yaml"
    user.write_text(
        "plugins:\n  ipad_tunnel:\n    enabled: false\n"
        "    local_port: 1080\n    device_port: 9001\n",
        encoding="utf-8",
    )
    ctx = PluginContext(config=Config(user_path=user), events=events)
    # 会话桩：``declare`` / ``run_op`` / ``subscribe`` / ``observed`` 四个包装都落到它上。
    ctx.helper = helper if helper is not None else _Link()
    return ctx


def _push(plugin, state: dict) -> None:
    """模拟守护推来一份隧道观测 —— 组件侧 ``observed("tunnel")`` 的读口。

    取代旧的「往 ``tunnel_state.json`` 写一份」：观测不是文件，是会话上推来的一帧，
    组件只认内存里那份（取值一律「现问」，见 ``_observed_state``）。
    """
    plugin.ctx.helper.push(TUNNEL_SUBJECT, state)


def _patch_env(tmp_path, monkeypatch, daemon_status=None) -> None:
    """把守护交互整体打桩（不碰真实用户目录 / launchctl）。

    2026-09-29 起**没有数据文件要重定向**：期望态 / 命令 / 事实 / 存活四条通道全在
    会话上（``ctx.helper``），组件侧一个路径都不认 —— 从前那三份重定向
    （``TUNNEL_FILE`` / ``TUNNEL_STATE_FILE`` / ``TRIGGER_FILE``）随之没有对象。
    """
    monkeypatch.setattr(daemon, "status", lambda: dict(
        daemon_status or {"installed": True, "running": True,
                          "version": "2.0", "expected": "2.0", "outdated": False}))
    # install 现签名为 (sources, missing)：桩要能吃下两个参数（批次 4 · S4）
    monkeypatch.setattr(daemon, "install", lambda *a, **k: True)
    monkeypatch.setattr(daemon, "uninstall", lambda: True)
    # 任何被中继接受的连接都快速失败，避免测试碰真实 usbmuxd
    _no_device(monkeypatch)


def _plugin(tmp_path, monkeypatch, events=None, ctx=None) -> Plugin:
    """构造并 start 一个 ipad_tunnel 插件。

    不再需要打桩桥接：长驻服务由**框架**按 resident_services() 启动，本组测试只
    直接调 plugin.start()（无 Registry ⇒ 无服务被托管 ⇒ 不会占用端口）。
    """
    _patch_env(tmp_path, monkeypatch)
    plugin = Plugin(ctx if ctx is not None else _ctx(tmp_path, events))
    plugin.start()
    return plugin


def test_start_declares_tunnel_intent(tmp_path, monkeypatch):
    """启动即把隧道意图**声明出去**（不再是往数据目录写一份 tunnel.json）。

    这一帧同时就是「叫守护立刻跑一轮」：本进程刚起、守护手里没有这条意图 ⇒ 内容变了
    ⇒ 编排层排成「立刻到期」。路径彻底离开两侧的约定：组件声明的是领域 ``tunnel``。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    subject, payload = plugin.ctx.helper.sent[-1]
    assert subject == TUNNEL_SUBJECT
    assert payload["enabled"] is False
    assert payload["local_port"] == 1080 and payload["device_port"] == 9001
    assert payload["wifi"]["on_activate"]
    # 运行期事实不进这份意图（v2）：链路、手动档都不在这里
    assert "link_up" not in payload and "ignore_link" not in payload
    assert payload["version"] == 2
    plugin.stop()


def test_switch_failures_are_handed_back_not_swallowed(tmp_path, monkeypatch):
    """两个开关写失败时必须把结局交出去——开关会弹回原位，界面不能一言不发。

    面板那枚 Switch 的回调是 ``ActionCallback``（可以返回 ``ActionResult``），宿主
    拿到才会画出来；返回 ``None`` 等于「这次没话说」，于是失败全落进日志。
    """
    from barkit.contribution.specs import ActionOutcome

    plugin = _plugin(tmp_path, monkeypatch)

    def _boom(_section_fn):
        raise OSError("磁盘满了")

    # ① 隧道总开关（也顺带验「基类的 toggle_automation 会透传」）
    monkeypatch.setattr(plugin.ctx.config, "update_plugin_section",
                        lambda pid, mutate, **kw: _boom(mutate))
    result = plugin.toggle_automation()
    assert result is not None and result.outcome is ActionOutcome.FAILED
    assert result.text.strip()

    # ② 建隧道模式（自动 / 手动）
    result = plugin.toggle_ignore_link()
    assert result is not None and result.outcome is ActionOutcome.FAILED

    # 写通的情况下不该啰嗦：状态本身就看得见（开关翻了）
    monkeypatch.undo()
    assert plugin.toggle_automation() is None
    plugin.stop()


def test_set_automation_active_writes_yaml_and_declares(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path, monkeypatch)
    assert plugin.automation_active is False
    plugin.set_automation_active(True)
    assert plugin.automation_active is True
    # 用户 YAML 写入的是隧道功能开关 tunnel_enabled（而非 framework 的 enabled）
    import yaml
    user = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert user["plugins"]["ipad_tunnel"]["tunnel_enabled"] is True
    # 关键回归：绝不能写 framework 的 enabled（否则会误伤插件加载门控）
    assert "enabled" not in user["plugins"]["ipad_tunnel"]
    # 意图重新声明为 enabled=True（由 tunnel_enabled 派生）
    subject, payload = plugin.ctx.helper.sent[-1]
    assert subject == TUNNEL_SUBJECT and payload["enabled"] is True
    # 开关打开 ⇒ 声明中继 + 探针（框架据此起线程并监听；关着时不占 1080）
    assert [svc.name for svc in plugin.resident_services()] == ["relay", "probe"]
    plugin.stop()


def test_start_declares_from_config_not_from_a_stale_copy(tmp_path, monkeypatch):
    """回归（2026-09-29 真机）：声明的是**配置此刻的样子**，不是任何人留下的旧副本。

    旧写法是**边沿触发**：只在自己状态翻转时才落笔 ⇒ 盘上那份被第三方（或被旧进程的
    残留逻辑）改掉之后**永不纠正**，界面与守护各说各话十几分钟，而 helper 拿着那份
    过期的期望态去建隧道。现在这一层连盘都没有：启动时无脑按配置重声明一遍就够了 ——
    幂等（同一内容不重复上线）由 ``declare`` 按内容哈希保证，这就是电平触发该有的样子。
    """
    user = tmp_path / "config.yaml"
    user.write_text(
        "plugins:\n  ipad_tunnel:\n    enabled: false\n"
        "    local_port: 1080\n    device_port: 9001\n"
        "    tunnel_enabled: true\n"
        "    hijack_route: false\n    hijack_dns: false\n",
        encoding="utf-8",
    )
    _patch_env(tmp_path, monkeypatch)
    link = _Link()
    ctx = PluginContext(config=Config(user_path=user))
    ctx.helper = link
    plugin = Plugin(ctx)
    plugin.start()
    _subject, payload = link.sent[-1]
    assert payload["enabled"] is True          # 按配置，而不是上一次别人写下的值
    assert payload["hijack_route"] is False and payload["hijack_dns"] is False
    plugin.stop()


def test_stop_leaves_the_knock_to_the_connection(tmp_path, monkeypatch):
    """退出**不敲任何门**：连接本身关掉就是那一声。

    上游（本进程的中继端口）由框架在 plugin.stop() 之前先停掉 ⇒ 端口连不上了；
    守护拿到连接的 **EOF** ⇒ 依附型意图（``tunnel``）当场被撤回并跑一轮收尾 ⇒
    它复检上游发现没了，秒级拆隧道，不必等最长 5s 的下一轮（那期间 hijack_route
    开着就是整机假连接）。

    ⚠️ 从前这一步是往数据目录 touch 一个 ``.trigger`` 文件；现在什么都没有 ——
    进程被杀（连 stop() 都跑不到）也照样生效，这正是旧形态给不了的。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    link = plugin.ctx.helper
    n_ops, n_sent = len(link.ops), len(link.sent)
    plugin.stop()
    assert link.ops[n_ops:] == [], "退出不靠命令：连接关掉就是那一声"
    assert link.sent[n_sent:] == [], "退出不重发意图（内容没变）"


def test_legacy_enabled_off_still_loads_after_migration(tmp_path, monkeypatch):
    """回归：旧版用户配置把隧道开关写进 framework 的 enabled（=false）；升级后关开关不应再
    让整个插件不加载。__init__ 的迁移会把 enabled 迁到 tunnel_enabled 并移除 enabled，
    使 framework 加载门控回退默认 true。"""
    import yaml
    # 模拟升级前「关掉开关」残留的旧用户配置
    user = tmp_path / "config.yaml"
    user.write_text(
        "plugins:\n  ipad_tunnel:\n    enabled: false\n"
        "    local_port: 1080\n    device_port: 9001\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(daemon, "status",
                        lambda: {"installed": True, "running": True,
                                 "version": "2.0", "expected": "2.0", "outdated": False})
    # install 现签名为 (sources, missing)：桩要能吃下两个参数（批次 4 · S4）
    monkeypatch.setattr(daemon, "install", lambda *a, **k: True)
    monkeypatch.setattr(daemon, "uninstall", lambda: True)
    _no_device(monkeypatch)
    ctx = PluginContext(config=Config(user_path=user))
    ctx.helper = _Link()
    plugin = Plugin(ctx)
    # 关键：即使旧配置 enabled=false，迁移后 framework 加载门控应为 true（插件仍加载）
    assert plugin.enabled is True
    # 隧道功能开关状态被原样保留（关）
    assert plugin.automation_active is False
    # 用户 YAML 已迁移：enabled 移除；隧道开关是「关」＝``tunnel_enabled`` 的**声明
    # 默认值**，按写侧口径（等于默认即删键）不留键 —— 有效值仍是关。
    out = yaml.safe_load(user.read_text(encoding="utf-8"))
    sec = out["plugins"]["ipad_tunnel"]
    assert "enabled" not in sec, "框架的加载门控键必须被移走"
    assert "tunnel_enabled" not in sec, "关＝缺省 ⇒ 不留键（留了会把缺省钉死）"
    assert plugin.automation_active is False
    plugin.stop()


def test_tick_returns_snapshot(tmp_path, monkeypatch):
    """v3 契约：tick() 返回 StateReport，框架自动 publish（不再自 publish）。"""
    plugin = _plugin(tmp_path, monkeypatch)
    _push(plugin, {
        "ok": True, "checked_at": int(time.time()), "active": True, "utun": "utun9",
        "dns_set": True, "ipad_reachable": True, "last_action": None, "error": None,
    })
    snap = plugin.tick()
    assert isinstance(snap, StateReport)
    assert snap.state == "active"          # 取值名；级别归声明（决议 D13）
    plugin.stop()


def test_snapshot_reports_engine_silent_when_ipad_never_answers(tmp_path, monkeypatch):
    """端口能连上、SOCKS5 一个字节都不回 ⇒ 报 engine_silent，不再错落成「等待 iPad」。

    这是 2026-09-20 真机故障在面板上的样子：iPad 那边内核还在 backlog 里收连接，
    所以「端口通不通」永远为真 —— **只有握手无应答才是真信号**。旧版把这一格和
    「还没连上」都报成 waiting_ipad，而两件事该做的动作完全不同（前者什么都不用做，
    后者得去 iPad 上看那个 App），用户因此一直以为隧道是好的。

    两半都验（本仓门禁的规矩：既证「该抓的抓住」，也证「不该报的不报」）。
    """
    import barkit.plugins.ipad_tunnel.bridge as bridge

    plugin = _plugin(tmp_path, monkeypatch)          # 先建插件：_patch_env 会把 pick_device 打桩成抛错
    plugin.set_automation_active(True)
    _push(plugin, {
        "ok": True, "checked_at": int(time.time()), "active": False, "utun": None,
        "dns_set": False, "ipad_reachable": False, "last_action": None, "error": None,
    })

    # ① 设备在、通道打得通，但握手没人应答（真机就是这一档）
    monkeypatch.setattr(bridge_mux, "pick_device", lambda u: (1, "S"))
    monkeypatch.setattr(bridge_mux, "open_device_tunnel",
                        lambda d, p, timeout=None: _fake_sock())
    monkeypatch.setattr(bridge_mux, "socks5_handshake", lambda s, timeout=None: False)
    plugin._probe.poll()
    assert plugin._probe.last_kind == bridge.LINK_ENGINE_SILENT   # 前提：探针真判出了这一档
    snap = plugin.tick()                         # tick 会重读状态文件（插件 __init__ 时它还不存在）
    assert snap.state == "engine_silent"
    assert "9001" in snap.summary                # 文案带端口，好和真机读数对上

    # ② 反证：握手有应答时绝不能报这一格（否则每次重连都会被误报成「iPad 挂了」）
    monkeypatch.setattr(bridge_mux, "socks5_handshake", lambda s, timeout=None: True)
    plugin._probe.poll()
    assert plugin._probe.last_kind == bridge.LINK_OK
    assert plugin.tick().state == "waiting_ipad"
    plugin.stop()


def test_snapshot_reports_data_plane_dead_above_active(tmp_path, monkeypatch):
    """隧道建起来了却搬不动字节 ⇒ 报 data_plane_dead（严重级），且**盖过 helper 报的 active**。

    helper 只验「上游那个端口在听」，所以故障期它报的恰恰是 active=true —— 本格排在 active
    之前才是这场故障能被看见的关键（否则面板永远「隧道生效中」，而用户上不了网）。
    级别与提醒意图都从声明来：ERR 级 ⇒ 按 D17 默认进托盘角标（用户裁定「看角标、不弹通知」）。
    """
    import barkit.plugins.ipad_tunnel.bridge as bridge
    from barkit.contribution.specs import StateLevel
    from barkit.shell.renderers.state_view import resolve

    plugin = _plugin(tmp_path, monkeypatch)
    plugin.set_automation_active(True)
    _push(plugin, {
        "ok": True, "checked_at": int(time.time()), "active": True, "utun": "utun9",
        "dns_set": True, "ipad_reachable": True, "last_action": None, "error": None,
    })
    monkeypatch.setattr(plugin, "_data_plane_fault", lambda: True)
    spec = next(s for s in plugin.state_specs() if s.key == "main")

    # ① 阶梯这一轮还没给出段位（可能刚拆过隧道）：只说搬不动，**不指认段位**
    plugin._probe.last_hop = None
    snap = plugin.tick()
    assert snap.state == "data_plane_dead"          # ★ 盖过 active（helper 说一切正常）
    rs = resolve(spec, snap, stale=False)
    assert rs.level is StateLevel.ERR               # 要的「严重级别」
    assert rs.alerts is True                        # 角标会亮（ERR 一律露出）
    assert "数据卡在本机" in rs.text
    assert "实测正常" not in rs.text               # 说不出段位时**不许**声称 iPad 侧正常

    # ② 阶梯给出「②③④ 刚实测全过、① 搬不动」⇒ 这时才敢说卡在本机，并把凭据摆出来
    plugin._probe.last_hop = bridge.HOP_DATA
    rs2 = resolve(spec, plugin.tick(), stale=False)
    assert "数据卡在本机" in rs2.text
    assert "实测正常" in rs2.text and "SOCKS" in rs2.text   # 凭据逐段点名

    # 反证①：拔线（阶梯 ② 设备不在）时不许报这一格 —— 那时该说「等待 iPad」
    plugin._probe.last_hop = bridge.HOP_DEVICE
    assert plugin.tick().state != "data_plane_dead"

    # 反证②：开关关着时也不报（没开隧道，谈不上隧道故障）
    plugin._probe.last_hop = None
    plugin.set_automation_active(False)
    assert plugin.tick().state != "data_plane_dead"
    plugin.stop()


def test_snapshot_reports_building_while_the_helper_is_still_creating(tmp_path, monkeypatch):
    """helper 报 pending、而本机链路判据已就绪 ⇒ 报 building，不再错落成「等待 iPad」。

    2026-09-29 用户报的「启动先正常 → 过一会儿等 iPad → 再恢复正常」，中间那一段就是这
    一格：helper 的中间态（上游在听、hev 已起、utun 未建出，最多 20 秒）此前**没被读**，
    于是被算成「等待 iPad · 链路就绪后自动建立隧道」—— 那一刻 iPad 插着、链路也好，这句
    话是错的。

    两半都验：pending ＋ 链路就绪 ⇒ building；pending 而链路**没**就绪 ⇒ 仍是「等待 iPad」
    （那才是真在等的时候，别把它也说成「正在建」）。
    """

    plugin = _plugin(tmp_path, monkeypatch)
    plugin.set_automation_active(True)
    pending_state = {
        "ok": True, "checked_at": int(time.time()), "active": False, "pending": True,
        "utun": None, "dns_set": False, "ipad_reachable": None, "last_action": None,
        "error": None,
    }
    _push(plugin, pending_state)

    # ① 链路已就绪（探针真给出正向结论）⇒ 正在建
    monkeypatch.setattr(bridge_mux, "pick_device", lambda u: (1, "S"))
    monkeypatch.setattr(bridge_mux, "open_device_tunnel",
                        lambda d, p, timeout=None: _fake_sock())
    monkeypatch.setattr(bridge_mux, "socks5_handshake", lambda s, timeout=None: True)
    # 探针有迟滞（``PROBE_FLIP_VOTES`` 票才翻）⇒ 连探几轮才落定「链路就绪」
    for _ in range(3):
        plugin._probe.poll()
    assert plugin._probe.concluded and plugin._probe.link_up   # 前提：链路判据真成立
    snap = plugin.tick()
    assert snap.state == "building"
    assert "建立中" in snap.summary
    plugin.stop()

    # ② 反证：链路还没结论（新起的实例、探针没跑过）⇒ 那时确实在等，不许报「正在建」
    plugin2 = _plugin(tmp_path, monkeypatch)
    plugin2.set_automation_active(True)
    _push(plugin2, pending_state)
    assert not plugin2._probe.concluded
    assert plugin2.tick().state == "waiting_ipad"
    plugin2.stop()


def test_snapshot_reports_daemon_stale_instead_of_reciting_an_old_report(tmp_path, monkeypatch):
    """守护写下的 ``checked_at`` 过期 ⇒ 报 daemon_stale，绝不照念文件里的 active。

    这是「面板说隧道生效中、其实早断了」的防呆：状态文件里的 active 只有在**守护真的在跑**
    时才算数。判据取 ``checked_at``（守护自己写下的时刻），**不是**文件 mtime —— 谁碰一下
    文件都会把 mtime 改新，那并不代表守护跑过一轮。
    """
    import re

    # 阈值住在插件的 `constants.py`（2026-10-01 拆包前它在 `plugin.py`）
    from barkit.plugins.ipad_tunnel import constants as tunnel_constants

    plugin = _plugin(tmp_path, monkeypatch)
    plugin.set_automation_active(True)
    age = tunnel_constants.DAEMON_REPORT_MAX_AGE_S + 30
    _push(plugin, {
        "ok": True, "checked_at": int(time.time() - age), "active": True, "utun": "utun9",
        "dns_set": True, "ipad_reachable": True, "last_action": None, "error": None,
    })
    snap = plugin.tick()
    assert snap.state == "daemon_stale"            # ★ 盖过 active（文件里那行绿字不作数）
    assert "已过期" in snap.summary
    m = re.search(r"守护 (\d+) 秒未回报", snap.summary)
    assert m and abs(int(m.group(1)) - age) <= 2   # 把「多久没回报」的数字摆出来（±2s 取整）

    # 反证①：同样的内容、只是回报是新鲜的 ⇒ 照常报 active（别把正常情形误报成过期）
    _push(plugin, {
        "ok": True, "checked_at": int(time.time()), "active": True, "utun": "utun9",
        "dns_set": True, "ipad_reachable": True, "last_action": None, "error": None,
    })
    assert plugin.tick().state == "active"

    # 反证②：读不到 checked_at（老版本守护 / 结构不完整）不判过期——没依据就别下结论
    _push(plugin, {
        "ok": True, "active": True, "utun": "utun9", "dns_set": True,
        "ipad_reachable": True, "last_action": None, "error": None,
    })
    assert plugin.tick().state == "active"
    plugin.stop()


def test_state_spec_turns_on_the_framework_liveness_check(tmp_path, monkeypatch):
    """失联判定打开（2026-09-29）：周期型组件三轮收不到自己的上报 ⇒ 框架改判「状态失联」。

    ⚠️ 这条判的是**本组件**还在不在上报；「守护写下的结论是不是此刻的」是另一条判据
    （``DAEMON_REPORT_MAX_AGE_S``，见上一个用例），两件事别混。此前这里是关着的
    （``stale_after=None``），于是调度器 / worker 真死了面板也只会照念最后一份结论。
    """
    import barkit.plugins.ipad_tunnel.plugin as plugin_mod

    plugin = _plugin(tmp_path, monkeypatch)
    spec = next(s for s in plugin.state_specs() if s.key == "main")
    assert spec.stale_after == plugin_mod.SELF_STALE_AFTER_S
    assert spec.stale_after > plugin.suggested_poll     # 至少容得下自己一个节拍
    plugin.stop()


def test_start_declares_immediately(tmp_path, monkeypatch):
    """启动即声明（2026-09-29）：不叫的话面板要等探针 ＋ 中继各一轮，那之前念的是旧结论。

    ``_plugin`` 夹具会调 ``plugin.start()``；这里只盯「意图已经交出去了」这一件事
    —— 一帧声明就是旧日那一下敲门（守护收到即跑一轮）。
    """
    link = _Link()
    plugin = _plugin(tmp_path, monkeypatch, ctx=_ctx(tmp_path, helper=link))
    assert link.sent and link.sent[-1][0] == TUNNEL_SUBJECT, \
        "启动时必须叫守护跑一轮，否则面板显示的是上一次进程的结论"
    plugin.stop()


def test_snapshot_points_at_the_failing_hop(tmp_path, monkeypatch):
    """面板照阶梯结论**点名卡在哪一段** —— 这是这轮改动的目的本身。

    改之前只有两句话可选，而故障期两句都不对：helper 报 active ⇒「隧道生效中」；helper
    报未激活 ⇒「等待 iPad」（可 iPad 明明插着）。现在每段有自己的话，且每句都说得出凭据。
    """
    import barkit.plugins.ipad_tunnel.bridge as bridge
    from barkit.contribution.specs import StateLevel
    from barkit.shell.renderers.state_view import resolve

    plugin = _plugin(tmp_path, monkeypatch)
    plugin.set_automation_active(True)
    _push(plugin, {
        "ok": True, "checked_at": int(time.time()), "active": True, "utun": "utun9",
        "dns_set": True, "ipad_reachable": True, "last_action": None, "error": None,
    })
    spec = next(s for s in plugin.state_specs() if s.key == "main")

    # ③ 通道打不开：**盖过 active**（helper 只验它自己那侧的端口在听）
    plugin._probe.last_hop = bridge.HOP_CHANNEL
    snap = plugin.tick()
    assert snap.state == "ipad_port_refused"
    rs = resolve(spec, snap, stale=False)
    assert rs.level is StateLevel.ERR and rs.alerts is True     # 角标会亮
    assert "9001" in rs.text and "没人在听" in rs.text         # 带端口，好和真机读数对上

    # ④ 引擎不应答：同样盖过 active
    plugin._probe.last_hop = bridge.HOP_SOCKS
    assert plugin.tick().state == "engine_silent"

    # 反证：阶梯全过 ⇒ 照旧报 active（不许把正常状态也染红）
    plugin._probe.last_hop = None
    assert plugin.tick().state == "active"

    # ② 设备不在：这一格要点名 USB —— 只说「等待 iPad」会让人干等
    _push(plugin, {
        "ok": True, "active": False, "utun": None, "dns_set": False,
        "ipad_reachable": False, "error": None,
    })
    plugin._probe.last_hop = bridge.HOP_DEVICE
    snap2 = plugin.tick()
    assert snap2.state == "waiting_ipad" and "USB" in snap2.summary
    plugin.stop()


def test_snapshot_reports_service_unknown_above_dns_degraded(tmp_path, monkeypatch):
    """读不出服务名（helper **一次都没碰**系统网络）与「碰了但没被采纳」分两格说。

    两件事要用户做的动作不同：dns_degraded 去查网络，service_unknown 去查「为什么读不出
    服务名」—— 合成一格等于把两件事都说成一件。故本格**排在它之前**（两个原因同时在场时，
    该说给用户听的是「没碰过」）。
    """
    from barkit.contribution.specs import StateLevel
    from barkit.shell.renderers.state_view import resolve

    plugin = _plugin(tmp_path, monkeypatch)
    plugin.set_automation_active(True)
    plugin._probe.last_hop = None
    spec = next(s for s in plugin.state_specs() if s.key == "main")

    # 读不出服务名时 helper 什么也没改（它同时也会置 dns_unavailable —— 「未接管」这个
    # 事实确实成立）⇒ 面板必须说「没碰过」那一句
    _push(plugin, {
        "ok": True, "active": True, "utun": "utun9", "dns_set": False,
        "dns_unavailable": True, "service_unknown": True,
        "ipad_reachable": True, "error": None,
    })
    snap = plugin.tick()
    assert snap.state == "service_unknown"
    rs = resolve(spec, snap, stale=False)
    assert rs.level is StateLevel.WARN
    assert "读不出" in rs.text

    # 反证：只有「碰了没被采纳」时照旧报 dns_degraded —— 新格不许把旧格整个吞掉
    _push(plugin, {
        "ok": True, "active": True, "utun": "utun9", "dns_set": False,
        "dns_unavailable": True, "service_unknown": False,
        "ipad_reachable": True, "error": None,
    })
    assert plugin.tick().state == "dns_degraded"
    plugin.stop()


class _FeedbackRecorder:
    """``FeedbackBar`` 的最小替身：只留**最后一句**（真槽也只有一条，后写的赢）。"""

    def __init__(self):
        self.kind = None
        self.text = ""

    def info(self, text):
        self.kind, self.text = "info", text

    def error(self, text):
        self.kind, self.text = "error", text


def test_immediate_check_sends_the_resync_command(tmp_path, monkeypatch):
    """「立即检查」＝ ``resync`` 一帧（不是 touch 文件）：「现在跑一轮」，不重发配置。"""
    plugin = _plugin(tmp_path, monkeypatch)
    plugin.immediate_check()
    assert plugin.ctx.helper.ops[-1] == ("resync", {"subject": TUNNEL_SUBJECT})
    plugin.stop()


def test_immediate_check_does_not_claim_success_when_daemon_is_unreachable(
    tmp_path, monkeypatch
):
    """守护没连时点「立即检查」：只说失败，不许再补一句「已让守护立即重跑一轮」。

    ``helper_link.run_op`` 未连接时**就地**回调失败并返回 False —— 反馈槽只有一条，
    改前那句 info 写在 error 后面，正好把失败盖掉。本组件是唯一填了
    ``helper_ops_texts().check`` 的（另两个组件那句是空串，故不受影响）。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    fb = _FeedbackRecorder()
    plugin._helper_feedback = fb

    def refuse(op, params, callback=None):
        if callable(callback):
            callback(False, None, "守护未连接")
        return False

    plugin.ctx.helper.run_op = refuse
    plugin.immediate_check()

    assert fb.kind == "error"
    assert "立即重跑一轮" not in fb.text
    plugin.stop()


def test_immediate_check_still_says_it_asked_on_success(tmp_path, monkeypatch):
    """对照组：真发出去了才说那句（防把正常路改坏）。"""
    plugin = _plugin(tmp_path, monkeypatch)
    fb = _FeedbackRecorder()
    plugin._helper_feedback = fb

    plugin.immediate_check()

    assert fb.kind == "info"
    assert "立即重跑一轮" in fb.text
    plugin.stop()


# ---------- 观测订阅（§8.2.3 批次 9 收归框架；§8.2.2 批次 10 只标记脏） ----------

class _RecorderBus:
    """事件总线桩：记录 publish 的 (topic, payload)。"""

    def __init__(self):
        self.published: list = []

    def publish(self, topic, payload=None):
        self.published.append((topic, payload))


def test_subjects_declares_the_tunnel_domain(tmp_path, monkeypatch):
    """声明的是**领域**（``tunnel``），不是某个文件路径（2026-09-29）。

    从前这里声明 ``tunnel_state.json`` 那个路径，框架于是去盯它所在目录 —— 同一目录里
    别的组件写文件（route_guard 的状态每 30 秒必写）也会把本组件叫醒，一天 1476 条
    日志、真变化被淹没。现在订阅挂在连接上，推的是本领域的事实，与别人写不写文件无关。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    assert plugin.subjects() == [TUNNEL_SUBJECT]
    assert plugin.intent_subject() == TUNNEL_SUBJECT
    plugin.stop()


def test_on_observed_only_marks_dirty(tmp_path, monkeypatch):
    """§8.2.2：handler 只标记脏——**不取值、不广播**；实际工作归 tick（worker 线程）。"""
    bus = _RecorderBus()
    calls: list = []
    ctx = _ctx(tmp_path, bus)
    ctx.mark_dirty = lambda pid, reason: calls.append((pid, reason))
    plugin = _plugin(tmp_path, monkeypatch, ctx=ctx)
    _push(plugin, {
        "ok": True, "checked_at": int(time.time()), "active": True, "utun": "utun9",
        "dns_set": True, "ipad_reachable": True, "last_action": None, "error": None,
    })

    plugin.on_observed(TUNNEL_SUBJECT, {"active": True})

    assert calls == [("ipad_tunnel", "helper_observed")], "handler 必须只登记意图（带自身 id）"
    assert bus.published == [], "总线线程不得取值 / 广播"

    # 真正的工作在 tick 里完成（事件路径与轮询路径的同一执行入口）
    snap = plugin.tick()
    assert isinstance(snap, StateReport) and snap.state == "active"
    plugin.stop()


def test_on_observed_ignores_foreign_subject(tmp_path, monkeypatch):
    """别的领域的事实（防御性判断）不标记脏。"""
    calls: list = []
    ctx = _ctx(tmp_path)
    ctx.mark_dirty = lambda pid, reason: calls.append((pid, reason))
    plugin = _plugin(tmp_path, monkeypatch, ctx=ctx)
    plugin.on_observed("route", {"whatever": 1})
    assert calls == []
    plugin.stop()


# 「内容没变就别重复上线」的去重链已随通道一起搬到 ``HelperLink``（按内容哈希比对
# ``rev``，见 tests/test_session.py）—— 组件这一层不再有「和什么比」这件事可错，
# 故原先守 mtime 去重的那条用例随之删除。


def test_install_action_delegates(tmp_path, monkeypatch, qtbot):
    """安装直接执行（不再弹框 —— 反馈走详情页的行内反馈槽）。"""
    events = EventBus()
    plugin = _plugin(tmp_path, monkeypatch, events=events)
    received = []
    events.subscribe(plugin.status_topic(), received.append)
    plugin.install_action()
    qtbot.waitUntil(lambda: len(received) >= 1, timeout=2000)
    assert isinstance(received[-1], StateReport)
    assert plugin._daemon_status["installed"] is True
    plugin.stop()


def test_actions_report_into_the_ops_zone_feedback_bar(tmp_path, monkeypatch, qtbot):
    """动作结果落进**页尾框架区**那条槽（装置运维卡所在的那一区）。

    槽随卡一起从页内挪到了框架区（2026-09-28）：触发按钮在哪一区，回执就落哪一区。
    """
    from barkit.shell.ui_kit import FeedbackBar

    plugin = _plugin(tmp_path, monkeypatch)
    page = plugin._build_settings_page()
    qtbot.addWidget(page)                      # qtbot 只存 weakref，靠本函数的 page 保住
    zone_fb = FeedbackBar(page)                # 模拟框架区那条槽（真实接线归框架区的用例）
    plugin.attach_helper_ops(page, zone_fb, lambda: None)

    monkeypatch.setattr(daemon, "install", lambda *a, **k: True)
    plugin.install_action()
    assert "已安装" in zone_fb.text()

    monkeypatch.setattr(daemon, "install", lambda *a, **k: False)
    plugin.install_action()
    assert "安装失败" in zone_fb.text()
    plugin.stop()


def test_uninstall_action_asks_inline_then_runs(tmp_path, monkeypatch, qtbot):
    """卸载是破坏性的：先进行内二次确认，点下确认才动守护。

    确认位是**页尾框架区**那条槽（2026-09-28 起随装置运维卡由框架注入）——
    触发按钮在哪一区，问句就落哪一区（口径「一区一条」）。
    """
    from barkit.shell.ui_kit import FeedbackBar

    plugin = _plugin(tmp_path, monkeypatch)
    page = plugin._build_settings_page()
    qtbot.addWidget(page)
    zone_fb = FeedbackBar(page)
    plugin.attach_helper_ops(page, zone_fb, lambda: None)
    uninstalled = []
    monkeypatch.setattr(daemon, "uninstall", lambda: uninstalled.append(1) or True)

    plugin.uninstall_action()
    assert zone_fb.is_pending(), "卸载必须先经过行内二次确认"
    assert uninstalled == [], "未确认前不得动守护"
    assert "确定卸载" in zone_fb.text()

    zone_fb.accept()
    assert uninstalled == [1], "确认后才真正卸载"
    assert "已卸载" in zone_fb.text()
    plugin.stop()


def test_uninstall_without_settings_page_is_refused(tmp_path, monkeypatch):
    """拿不到行内确认位时**拒绝执行** —— 破坏性动作不允许在无确认的情况下静默跑掉。

    确认位是**框架区那条槽**（2026-09-28 起随装置运维卡一起由框架注入）：没开设置窗
    就没有它。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    uninstalled = []
    monkeypatch.setattr(daemon, "uninstall", lambda: uninstalled.append(1) or True)
    assert plugin._helper_feedback is None, "没有设置页就不该有确认位"
    plugin.uninstall_action()
    assert uninstalled == []
    plugin.stop()


def test_forget_feedback_only_clears_its_own_slot(tmp_path, monkeypatch, qtbot):
    """页面重建：旧槽位的 destroyed 不得把**新**槽位一起摘掉。"""
    plugin = _plugin(tmp_path, monkeypatch)
    old = plugin._build_settings_page()
    qtbot.addWidget(old)
    old_fb = plugin._feedback
    new = plugin._build_settings_page()
    qtbot.addWidget(new)
    new_fb = plugin._feedback
    assert new_fb is not old_fb

    plugin._forget_feedback(old_fb)
    assert plugin._feedback is new_fb, "旧槽位的销毁回调不应摘掉新槽位"
    plugin._forget_feedback(new_fb)
    assert plugin._feedback is None
    plugin.stop()


def test_build_settings_page_survives_all_daemon_states(tmp_path, monkeypatch):
    """回归：设置页必须能构造（曾因缺 QHBoxLayout 导入在运行时崩）。

    覆盖三种守护状态分支：已装 / 未装 / 过期，各分支都建得出这一页。
    ⚠️ 装置运维段自 2026-09-28 起归框架区，本页不再画它 —— 本用例只守「这一页能构造」。
    """
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])  # 保证 app 存在；本用例不读这个引用

    statuses = (
        {"installed": True, "running": True, "version": "2.0",
         "expected": "2.0", "outdated": False},
        {"installed": False, "running": False, "version": None,
         "expected": "2.0", "outdated": False},
        {"installed": True, "running": True, "version": "1.2",
         "expected": "2.0", "outdated": True},
    )
    for st in statuses:
        plugin = _plugin(tmp_path, monkeypatch)
        monkeypatch.setattr(daemon, "status", lambda s=st: s)
        plugin.refresh_daemon_status()
        page = plugin._build_settings_page()
        assert page is not None
        plugin.stop()


def test_hev_binary_system_domain_only(tmp_path, monkeypatch):
    """hev_binary 只认系统域安装位，**用户域回退已删除**（批次 -1 安全热修）。

    旧实现「用户域 data_dir 有就执行用户域文件」= root 执行普通用户可写目录里的
    Mach-O，是可直接利用的本地提权面。现在无论用户域有没有文件，路径都是系统域。
    """
    sys_dir = tmp_path / "sys"
    user_dir = tmp_path / "user"
    sys_dir.mkdir(); user_dir.mkdir()
    monkeypatch.setattr(safeio, "SYS_HEV_DIR", sys_dir)
    monkeypatch.setenv("BARKIT_DATA_DIR", str(user_dir))

    # 用户域放了文件也不看它：路径恒为系统域安装位
    (user_dir / "hev-socks5-tunnel").write_text("", encoding="utf-8")
    assert safeio.hev_binary() == sys_dir / "hev-socks5-tunnel"
    # 系统域也不存在 → 仍是系统域路径（run_tunnel_cycle 报「未安装」，不降级）
    assert not (sys_dir / "hev-socks5-tunnel").exists()
    assert safeio.hev_binary() == sys_dir / "hev-socks5-tunnel"


def test_hev_yaml_mapdns_has_address_and_port():
    """生成的 hev-tunnel.yml 的 mapdns 段必须带 address/port（198.18.0.2:53）。

    回归：2026-09-03 曾因「strings 误判这构建不认 address」而删掉这俩键，
    导致 mapdns_address 默认 0 → udp_recv_handler 的 `faddr == 包地址` 严格
    等值匹配永远为假 → mapdns 被静默禁用 → 系统 DNS 设 198.18.0.2 后永远收
    不到应答（症状「DNS 无法连通」）。故必须显式写死，任何人再删都会在此红。
    """
    text = tunnelops._HEV_YAML_TEMPLATE.format(local_port=1080, tun_name="utun90")
    assert "mapdns:" in text
    # 段内必须出现这两个键（顺序无关，逐键断言避免只看段头）
    assert "address: '198.18.0.2'" in text, "mapdns.address 缺失 → DNS 拦截被禁用"
    assert "port: 53" in text, "mapdns.port 缺失 → DNS 拦截被禁用"


def test_hev_yaml_socks5_udp_in_tcp_mode():
    """模板必须**显式**写 ``socks5.udp: 'tcp'``（钉死 FWD UDP / UDP-in-TCP 模式）。

    这条守卫钉的是「显式」两个字，不是「UDP 可用」：hev 只在取值恰为 ``'udp'`` 时
    置 ``udp_in_udp=1``（``hev-config.c:251``），其余 —— 含键缺失，靠 ``memset``
    归零（同文件 467 行）—— 都落到 FWD UDP ⇒ **删掉本键行为逐字节不变**。这一点
    2026-10-12 核对（初判「缺键 ⇒ 非 DNS 的 UDP 全灭」是把上游 README 里未注释的
    ``udp: 'udp'`` 样例误当成了默认值）。
    留这条守卫的目的：把模式钉在明面上，上游改默认值的版本升级就不会静默改变隧道
    行为；真有人顺手删键，这里也会红，逼一次有意识的决定。断言走解析后的 YAML 键：
    模板注释里出现 "udp" 字样不会假红。
    """
    import yaml

    text = tunnelops._HEV_YAML_TEMPLATE.format(local_port=1080, tun_name="utun90")
    socks5 = yaml.safe_load(text)["socks5"]
    assert socks5.get("udp") == "tcp", (
        "模板不再显式写 socks5.udp='tcp'：模式退回隐式依赖上游默认值，"
        "上游改默认值的版本升级会静默改变隧道 UDP 行为"
    )


def test_hev_yaml_rw_timeout_not_30s_harvester():
    """misc 必须用新键 tcp/udp-read-write-timeout 且取上游默认量级（秒级值禁止回归）。

    回归：旧配置 read-write-timeout: 30000 的语义是「单次读/写等待超过 30s
    仍无字节即杀会话」（core hev-socks5-misc.c 的 "io timeout"），推送/WebSocket
    等长连接一静默即被收割、客户端立刻重连再被收割——2026-10-09 实测 57 分钟
    3107 条会话、66% io timeout。改回秒级 30000（或删新键回退到秒级）都在此红。
    注意断言带换行边界：「tcp-read-write-timeout: 300000」含旧值前缀子串，
    不带边界会假红。
    末一条断言守的是**配置整洁度**而非行为：新旧键并存时 hev 优先用新键
    （hev-config.c 的解析顺序），故它拦的是「下一任维护者改了一个不起作用的
    旧键」这类误导，不是行为回归；行为回归由前两条断言负责。
    """
    text = tunnelops._HEV_YAML_TEMPLATE.format(local_port=1080, tun_name="utun90")
    assert "tcp-read-write-timeout: 300000" in text, "TCP 读写超时须取上游默认 5 分钟"
    assert "udp-read-write-timeout: 60000" in text, "UDP 读写超时须取上游默认 60s"
    assert "\n  read-write-timeout: 30000\n" not in text, \
        "秒级 read-write-timeout 回归：30s 空闲收割长连接"


def test_hev_yaml_nofile_covers_session_cap():
    """misc 的 limit-nofile 必须够得着 max-session-count（fd 是**先撞上**的那道闸）。

    回归：limit-nofile 曾配 512。本端每条会话只占 1 个 fd —— 本地侧走 tun 不占
    fd，对端就是 ``socks5`` 那条 TCP（hev-socks5-session.c 每会话只 connect 上游
    一次）⇒ 约 505 条会话就撞 fd 天花板，而 max-session-count 写着 1024，于是
    会话上限永远够不着、配置自相矛盾；且撞线后 hev 开不出新 socket，失败不显眼
    （只有一条 accept 级日志）。两值同段相邻，这里**用解析而非字面匹配**，改注释 /
    调换行不该炸。

    只钉「上限够得着」这一条不变量，**不断言余量**：余量取决于 hev 自身的固定 fd
    开销（tun / 日志 / 事件循环），本仓拿不到那个数，写死一个数字只会变成下一个人
    随手改的魔法常数。
    """
    import yaml

    text = tunnelops._HEV_YAML_TEMPLATE.format(local_port=1080, tun_name="utun90")
    misc = yaml.safe_load(text)["misc"]
    nofile = int(misc["limit-nofile"])
    cap = int(misc["max-session-count"])
    assert nofile >= cap, (
        f"limit-nofile={nofile} < max-session-count={cap}："
        "fd 先耗尽，会话上限永远够不着（本端每会话 1 fd）"
    )


def test_hev_yaml_omits_inert_task_stack_size():
    """misc 不写 task-stack-size：本模板下它恒被 hev 推导的下限抬高，写了等于没写。

    回归：模板曾照抄上游 README 的 ``task-stack-size: 24576`` —— 那个数是配
    ``tcp-buffer-size: 4096`` 的示例。本模板的 tcp-buffer-size 是 65536，hev 取
    ``max(写入值, 20480 + max(min(tcp-buffer-size, lwip 的 TCP_SND_BUF 65528),
    1500×udp-copy-buffer-nums 默认 10))`` = max(24576, 86008) = 86008：写入值从不
    生效，唯一副作用是让人以为在调栈尺寸。删掉即取上游默认 86016（与推导下限相差
    8 字节，行为等价）。

    本断言守的是**配置整洁度**（别再抄一次上游 README），不是行为回归 —— 8 字节的
    栈差在行为上看不出来。判定走**解析后的 YAML 键**，故模板注释里出现这几个字
    （上面那段「不写 task-stack-size」的说明）不会假红。
    """
    text = tunnelops._HEV_YAML_TEMPLATE.format(local_port=1080, tun_name="utun90")
    import yaml

    doc = yaml.safe_load(text)
    where = [sec for sec, keys in doc.items()
             if isinstance(keys, dict) and "task-stack-size" in keys]
    assert not where, (
        f"task-stack-size 又写进 {where}：本模板下它恒被抬高、不生效（见 docstring）"
    )


def test_start_hev_uses_positional_config(helper_env, monkeypatch):
    """hev-socks5-tunnel 以「位置参数」接收配置文件，不认 -c / -f 等 flag。

    回归：误用 `-c` 会让 hev 尝试打开名为 "-c" 的文件而立即退出
    （Open -c failed!），表现成「hev 已启动但未发现 utun 接口」。
    """
    captured = {}

    class _Cap:
        def __init__(self, *a, **k):
            captured["args"] = list(a[0])
            self.returncode = None
            self.pid = 4242

        def poll(self):
            return None

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(tunnelops.subprocess, "Popen", _Cap)
    ok = tunnelops._start_hev({"local_port": 1080})
    assert ok is True
    args = captured["args"]
    assert args[0].endswith("hev-socks5-tunnel")
    assert "-c" not in args, "hev 不认 -c，配置必须作为位置参数传入"
    assert args[-1].endswith("hev-tunnel.yml")


def test_install_script_codesigns_hev_entitlement():
    """install 脚本必须对 hev 做 adhoc 签名，但**绝不可**注入受限授权。

    com.apple.vm.networking 是受限授权，单独 codesign 注入（无 provisioning
    profile 背书）会被 AMFI 在 hev 启动时 SIGKILL。daemon 以 root 跑 hev，root
    建 utun 本不需要该授权，故只做 adhoc 签名（无 entitlements）即可。
    """
    import pathlib
    src = pathlib.Path(__file__).parent.parent / "src/barkit/capabilities/helper_client.py"
    text = src.read_text(encoding="utf-8")
    assert "codesign" in text, "install 脚本仍须对 hev 二进制 codesign（adhoc，无 entitlements）"
    assert "--entitlements" not in text, "绝不可给 hev 注入受限授权（会导致 AMFI SIGKILL）"


def test_settings_page_carries_the_config_blocks_and_drops_the_ops_block(
    tmp_path, monkeypatch, qtbot
):
    """本页只剩配置三块（总闸 / 建隧道模式 / 隧道配置）；装置运维自 2026-09-28 起归框架区。

    装置运维（立即检查 / 安装守护 / 卸载守护）由框架按**声明**注入到**页尾框架区**，
    故这一页里找不到那几枚按钮 —— 比「有没有某张卡」更直：搬走的实体就是它们。
    """
    from PySide6.QtWidgets import QCheckBox, QPushButton

    plugin = _plugin(tmp_path, monkeypatch)
    page = plugin._build_settings_page()
    qtbot.addWidget(page)

    labels = [b.text() for b in page.findChildren(QPushButton)]
    assert not {"立即检查", "安装守护", "卸载守护"} & set(labels), labels
    # 隧道配置的三枚开关（激活 / 恢复 SSID 两格已撤）
    boxes = [b.text() for b in page.findChildren(QCheckBox)]
    assert sum("IPv6" in t for t in boxes) == 1, boxes
    assert sum("劫持全量 IPv4 路由" in t for t in boxes) == 1, boxes
    assert sum("劫持系统 DNS" in t for t in boxes) == 1, boxes
    assert not any("SSID" in t for t in boxes), "激活/恢复 SSID 已从表单撤掉"
    assert plugin._feedback is not None, "建页即挂上反馈槽"
    plugin.stop()


def test_settings_page_saves_tunnel_config_and_keeps_wifi_keys(tmp_path, monkeypatch, qtbot):
    """改一格写一次盘；**不整段覆盖** —— 用户手改的 wifi 键必须原样留着。

    SSID 两格从表单撤掉之后，这条尤其要紧：界面不再显示它，就更没有理由替用户删它
    （``wifi.on_activate`` 仍被 helper 消费，撤掉等于悄悄改掉一个还在生效的行为）。
    """
    import yaml

    # 用户手改过 Wi-Fi 切换（界面已不再显示它）⇒ 建插件之前就写进用户 YAML
    user = tmp_path / "config.yaml"
    user.write_text(
        "plugins:\n  ipad_tunnel:\n    enabled: false\n"
        "    local_port: 1080\n    device_port: 9001\n"
        "    wifi:\n      on_activate: iPad-Share\n",
        encoding="utf-8",
    )
    _patch_env(tmp_path, monkeypatch)
    ctx = PluginContext(config=Config(user_path=user))
    ctx.helper = _Link()
    plugin = Plugin(ctx)
    plugin.start()

    page = plugin._build_settings_page()
    qtbot.addWidget(page)
    assert plugin.save_tunnel_settings({"hijack_route": True, "hijack_dns": True}) is True

    sec = yaml.safe_load(user.read_text(encoding="utf-8"))["plugins"]["ipad_tunnel"]
    assert sec["hijack_route"] is True and sec["hijack_dns"] is True
    assert sec["wifi"]["on_activate"] == "iPad-Share", "界面不该替用户删掉它不显示的键"
    # 改完立刻生效：意图重新声明出去（不是等 helper 下一轮 30s）
    _subject, payload = plugin.ctx.helper.sent[-1]
    assert payload["hijack_route"] is True and payload["hijack_dns"] is True
    plugin.stop()


def test_settings_page_reports_illegal_config_inline(tmp_path, monkeypatch, qtbot):
    """校验失败（tunnel.json 生成不了）→ 行内红字，不弹框、也不假装保存成功。"""
    plugin = _plugin(tmp_path, monkeypatch)
    page = plugin._build_settings_page()
    qtbot.addWidget(page)                # qtbot 只存 weakref，靠本函数的 page 保住
    monkeypatch.setattr(
        tunnel_spec, "build_tunnel",
        lambda cfg: (_ for _ in ()).throw(TunnelConfigError("端口非法")),
    )
    assert plugin.save_tunnel_settings({"local_port": 1}) is False
    assert "配置未生效" in plugin._feedback.text()
    plugin.stop()


def test_daemon_notice_refresh_fires_after_install(tmp_path, monkeypatch, qtbot):
    """装完守护，注入的重算回调必须被**喊一声** —— 那块警示才会当场退场。

    来由不变（2026-09-18 真机实测）：警示按**建页那一刻**的状态画死的话，安装成功后
    组件只更新自己的缓存 ⇒ 用户装完仍看着红字与「重装守护」，只能反复点。

    ⚠️ 2026-09-28 起那一块由框架画在**页尾框架区**（``plugin._build_settings_page()``
    里已经没有它），故这里守的是**契约**这一半：装 / 卸成功后插件调一次刷新回调；
    「警示真实退场」的渲染归框架区的用例与真机目视。
    """
    from barkit.shell.ui_kit import FeedbackBar

    plugin = _plugin(tmp_path, monkeypatch)
    plugin._daemon_status = {           # 建卡时：已装但过期
        "installed": True, "running": True, "outdated": True,
        "version": "1.2", "expected": "2.0",
    }
    page = plugin._build_settings_page()
    qtbot.addWidget(page)
    refreshed = []
    zone_fb = FeedbackBar(page)
    plugin.attach_helper_ops(page, zone_fb, lambda: refreshed.append(1))

    monkeypatch.setattr(daemon, "install", lambda *a, **k: True)
    monkeypatch.setattr(daemon, "status", lambda: {
        "installed": True, "running": True, "outdated": False,
        "version": "2.0", "expected": "2.0",
    })
    plugin.install_action()

    assert refreshed, "装完必须喊一声 —— 不喊用户仍看着「守护版本过期」与「重装守护」"
    assert "已安装" in zone_fb.text(), "成功反馈落在装置运维那一区的槽里"
    plugin.stop()


def test_helper_notice_variants(tmp_path, monkeypatch):
    """守护警示 ＋ 「要不要出重装键」由插件算（界面只画，不自己问 helper_client）。

    与另两个用 root helper 的组件共用 ``capabilities.helper_ops``：只剩这一口回答
    「需要用户做点什么」的两种局面，正常态空串 = 整块隐藏。

    状态源是插件**缓存的** ``_daemon_status``（不是每次现问 helper_client），故每档都先
    ``refresh_daemon_status()`` 把缓存刷成要测的那一档。
    """
    from barkit.capabilities import helper_client as daemon

    plugin = _plugin(tmp_path, monkeypatch)

    monkeypatch.setattr(daemon, "status", lambda: {"installed": False, "outdated": False})
    plugin.refresh_daemon_status()
    text, needs_reinstall = plugin.helper_notice()
    assert "未安装" in text
    assert needs_reinstall is False, "未装时该点的是「安装守护」，不给「重装守护」"

    monkeypatch.setattr(daemon, "status", lambda: {
        "installed": True, "outdated": True, "version": "1.2", "expected": "2.0"})
    plugin.refresh_daemon_status()
    text, needs_reinstall = plugin.helper_notice()
    assert "过期" in text
    assert needs_reinstall is True

    monkeypatch.setattr(daemon, "status", lambda: {
        "installed": True, "outdated": False, "version": "2.0", "running": True})
    plugin.refresh_daemon_status()
    assert plugin.helper_notice() == ("", False), "一切正常 ⇒ 不给警示（整块隐藏）"
    plugin.stop()


def test_row_spec_has_no_mode_badge(tmp_path, monkeypatch):
    """面板行只剩一枚 Switch —— 「自动 / 手动」徽标归设置页（它本身就是自动化开关）。"""
    plugin = _plugin(tmp_path, monkeypatch)
    row = plugin.row_specs()[0]
    assert row.mode is None, "模式徽标已随自动化开关一起收进设置页"
    assert len(row.controls) == 1
    plugin.stop()


def test_detail_entry_is_the_diagnostics_form(tmp_path, monkeypatch, qtbot):
    """点面板整行 = 运行诊断窗（window 形态，壳归框架 DetailForm）。"""
    from barkit.contribution import DETAIL_PRESENTATION_WINDOW

    plugin = _plugin(tmp_path, monkeypatch)
    entry = plugin.detail_entry()
    assert entry.presentation == DETAIL_PRESENTATION_WINDOW, "诊断输出是宽文本，浮层卡装不下"
    assert "诊断" in (entry.label or ""), "窗标题该直接说这是诊断"

    from PySide6.QtWidgets import QLineEdit

    pane = entry.build()
    qtbot.addWidget(pane)
    assert len(pane.findChildren(QLineEdit)) == 2, "测试 IP ＋ 测试域名两格"
    plugin.stop()


def test_diag_pane_rejects_bad_input_inline(tmp_path, monkeypatch, qtbot):
    """输入不合法 → **页内**红字（旧实现是 QMessageBox，在置顶窗里会开嵌套事件循环）。"""
    from barkit.shell.ui_kit import FeedbackBar

    plugin = _plugin(tmp_path, monkeypatch)
    pane = plugin.detail_entry().build()
    qtbot.addWidget(pane)
    fb = next(w for w in pane.findChildren(FeedbackBar))

    pane._ip.setText("999.1.1.1")
    pane._on_run()
    assert "合法 IPv4" in fb.text() and pane._worker is None, "非法输入不该起线程"

    pane._ip.setText("8.8.8.8")
    pane._domain.setText("")
    pane._on_run()
    assert "测试域名不能为空" in fb.text() and pane._worker is None
    plugin.stop()


# ──────────────────────────────────────────────
# 诊断引擎（diagnostics.py）
# ──────────────────────────────────────────────

class FakeDiagRunner:
    """注入式命令 runner：按命令子串返回脚本化输出，覆盖真实 subprocess。"""

    def __init__(self, *, route_iface="utun90", nameserver="198.18.0.2",
                 dig_system="100.64.0.5", dig_tunnel="100.64.0.0",
                 curl_egress="203.0.113.7", socks_rc=0, socks_out="http=200 t=0.31",
                 ifconfig_out="utun90: flags=8051<UP,POINTOPOINT,RUNNING>\n"):
        self.route_iface = route_iface
        self.nameserver = nameserver
        self.dig_system = dig_system
        self.dig_tunnel = dig_tunnel
        self.curl_egress = curl_egress
        self.socks_rc = socks_rc
        self.socks_out = socks_out
        self.ifconfig_out = ifconfig_out

    def __call__(self, args, timeout=12.0):
        cmd = " ".join(args)
        if "route" in args and "-n" in args and "get" in args:
            return 0, (f"route to: x\n"
                       f"destination: x\nmask: x\ngateway: (direct)\n"
                       f"interface: {self.route_iface}\n")
        if "scutil" in args and "--dns" in args:
            return 0, (f"resolver #1\n  search domain[0] : lan\n"
                       f"  nameserver[0] : {self.nameserver}\n"
                       f"resolver #2\n  nameserver[0] : 8.8.8.8\n")
        if "dig" in args:
            if "@198.18.0.2" in cmd:
                return 0, (self.dig_tunnel + "\n") if self.dig_tunnel else ""
            return 0, (self.dig_system + "\n") if self.dig_system else ""
        if "curl" in args and "socks5h" in cmd:
            return self.socks_rc, self.socks_out
        if "curl" in args:
            return 0, self.curl_egress
        if "ifconfig" in args or ("sh" in args and "-c" in args):
            return 0, self.ifconfig_out
        return 0, ""


def test_diagnostics_parsers():
    assert parse_route_interface("route to: x\ninterface: utun90\n") == "utun90"
    assert parse_route_interface("no interface line") is None
    dns = ("resolver #1\n  nameserver[0] : 198.18.0.2\n"
           "resolver #2\n  nameserver[0] : 8.8.8.8\n")
    assert parse_primary_nameserver(dns) == "198.18.0.2"
    assert is_fake_ip_line("100.64.0.5\n") is True
    assert is_fake_ip_line("93.184.216.34\n") is False


def test_diagnostics_points_at_service_unknown():
    """DNS 该劫持却没劫持时，观测说了「读不出服务名」就点名这个原因。

    否则第 5 格只会说「helper 未应用」，用户去查网络 —— 而该查的是服务名为什么读不出来。
    反证：没有这个标记时仍说「helper 未应用」，不许把原因一律改写成「读不出」。
    """
    tunnel = {"hijack_route": True, "hijack_dns": True}

    def _step5(state_data):
        results = list(run_diagnostics("8.8.8.8", "example.com",
                                       runner=FakeDiagRunner(nameserver="8.8.8.8"),
                                       state=state_data, tunnel=tunnel))
        return {r.step: r for r in results}[5]

    r = _step5({"active": True, "utun": "utun90", "dns_set": False,
                "service_unknown": True, "ipad_reachable": True})
    assert r.status == "warn"
    assert "读不出当前网络服务名" in r.detail

    r2 = _step5({"active": True, "utun": "utun90", "dns_set": False,
                 "service_unknown": False, "ipad_reachable": True})
    assert r2.status == "warn"
    assert "helper 未应用" in r2.detail and "读不出" not in r2.detail


def test_diagnostics_hijacked():
    """全劫持场景：6 步都应 PASS，且命令/输出字段非空。"""
    state = {"active": True, "utun": "utun90",
             "dns_set": True, "ipad_reachable": True}
    tunnel = {"hijack_route": True, "hijack_dns": True}
    results = list(run_diagnostics("8.8.8.8", "example.com",
                                   runner=FakeDiagRunner(),
                                   state=state, tunnel=tunnel))
    assert len(results) == 6
    by = {r.step: r for r in results}
    assert by[1].status == "pass"      # iPad 就绪
    assert by[2].status == "pass" and "utun90" in by[2].detail
    assert by[3].status == "pass" and "劫持" in by[3].detail
    assert by[4].status == "pass" and "iPad" in by[4].detail
    assert by[5].status == "pass" and "198.18.0.2" in by[5].detail
    assert by[6].status == "pass" and "隧道" in by[6].detail
    for r in results:
        assert r.command
        assert r.output is not None


def test_diagnostics_fail_when_status_claims_ready_but_curl_dead():
    """诊断第 1 步以**实测**为准：观测说 iPad 就绪、实测取不到数据 ⇒ FAIL。

    2026-09-22 那场故障里，旧判据把「观测里写着 ipad_reachable=true」也算 PASS，于是
    面板与诊断页一起瞒报 —— 用户拿到的是「一切正常，就是没网」。这里守住改判后的方向，
    并反证「实测通了照旧 PASS」（不能把正常的就绪也误报掉）。
    """
    state = {"active": True, "utun": "utun90",
             "dns_set": True, "ipad_reachable": True}
    tunnel = {"hijack_route": True, "hijack_dns": True}

    dead = FakeDiagRunner(socks_rc=7, socks_out="curl: (7) failed to connect")
    first = next(run_diagnostics("8.8.8.8", "example.com", runner=dead,
                                 state=state, tunnel=tunnel))
    assert first.status == "fail"
    # 没有阶梯结论时：只说搬不动，**不指认段位**（说不出就别硬指）
    assert "没给出结论" in first.detail

    alive = FakeDiagRunner()                    # 反证：实测通了仍是 PASS
    first_ok = next(run_diagnostics("8.8.8.8", "example.com", runner=alive,
                                    state=state, tunnel=tunnel))
    assert first_ok.status == "pass"


def test_diagnostics_first_step_names_the_hop():
    """第 1 步要把探针的阶梯结论说成人话：**卡在哪一段 + 该去动哪一头**。

    诊断页与面板用的是**同一份结论**（``hop`` 由插件现问后传进来），不许两处各说一套。
    """
    import barkit.plugins.ipad_tunnel.bridge as bridge

    state = {"active": True, "utun": "utun90",
             "dns_set": True, "ipad_reachable": True}
    tunnel = {"hijack_route": True, "hijack_dns": True}
    dead = FakeDiagRunner(socks_rc=7, socks_out="curl: (7) failed to connect")

    def _first(hop):
        return next(run_diagnostics("8.8.8.8", "example.com", runner=dead,
                                    state=state, tunnel=tunnel, hop=hop))

    assert "iPad" in _first(bridge.HOP_CHANNEL).detail
    assert "SOCKS" in _first(bridge.HOP_SOCKS).detail
    assert "USB" in _first(bridge.HOP_DEVICE).detail
    # ②③④ 全过却还搬不动 ⇒ 敢说卡在本机（那是探针逐级实测出来的）
    assert "本机" in _first(bridge.HOP_DATA).detail


def test_diagnostics_bypass():
    """全旁路场景：隧道未激活、系统 DNS 走本机直连，6 步应为 INFO/FAIL（无异常绿）。"""
    state = {"active": False, "utun": None,
             "dns_set": False, "ipad_reachable": False}
    tunnel = {"hijack_route": False, "hijack_dns": False}
    runner = FakeDiagRunner(route_iface="en0", nameserver="10.205.55.254",
                            dig_system="93.184.216.34", dig_tunnel="",
                            curl_egress="198.51.100.7", socks_rc=7,
                            socks_out="curl: (7) failed to connect",
                            ifconfig_out="")
    results = list(run_diagnostics("8.8.8.8", "example.com", runner=runner,
                                   state=state, tunnel=tunnel))
    by = {r.step: r for r in results}
    assert by[1].status == "fail"      # iPad 未就绪
    assert by[2].status == "fail"      # 无 utun
    assert by[3].status == "info"      # 未劫持（旁路）
    assert by[4].status == "info"      # 直连
    assert by[5].status == "info"      # DNS 未劫持
    assert by[6].status == "info"      # DNS 直连


def test_diagnostics_config_mismatch():
    """配置要劫持但 helper 尚未应用：路由/DNS 应报 WARN（运行/配置不一致）。"""
    state = {"active": False, "utun": None,
             "dns_set": False, "ipad_reachable": True}
    tunnel = {"hijack_route": True, "hijack_dns": True}
    runner = FakeDiagRunner(route_iface="en0", nameserver="10.205.55.254",
                            dig_system="93.184.216.34", dig_tunnel="",
                            curl_egress="198.51.100.7", socks_rc=0,
                            socks_out="http=200 t=0.1", ifconfig_out="")
    results = list(run_diagnostics("8.8.8.8", "example.com", runner=runner,
                                   state=state, tunnel=tunnel))
    by = {r.step: r for r in results}
    assert by[1].status == "pass"      # iPad 可达（上游活）
    assert by[3].status == "warn"      # 配置要劫持但路由仍 en0
    assert by[5].status == "warn"      # 配置要劫持但系统 DNS 仍本机


# ──────────────────────────────────────────────
# plugin：长驻服务声明（批次 5 后段 S3）
# ──────────────────────────────────────────────

def test_resident_services_follow_switch_and_are_stable_objects(tmp_path, monkeypatch):
    """关着 ⇒ 空声明（不占 1080、不起线程）；开着 ⇒ 中继 + 探针，且始终同一批对象。"""
    plugin = _plugin(tmp_path, monkeypatch)          # 开关默认关
    assert plugin.resident_services() == []

    plugin.set_automation_active(True)
    svcs = plugin.resident_services()
    assert [s.name for s in svcs] == ["relay", "probe"]
    assert svcs[0] is plugin._relay and svcs[1] is plugin._probe   # 差量收敛的前提
    assert plugin.resident_services()[0] is svcs[0]                # 反复调用不重建

    plugin.set_automation_active(False)
    assert plugin.resident_services() == []
    plugin.stop()


def test_relay_listen_gate_truth_table(tmp_path, monkeypatch):
    """中继监听门：「链路事实」怎么变成「要不要占端口」的完整真值表。

    这是 v2 契约里唯一一处把**链路事实**翻译成**可观测信号**的地方，四格都要说清：
    没结论 ≠ 未就绪（前者是「还不知道」，后者是「知道不通」）；手动档不看链路。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    probe = plugin._probe

    # ① 还没结论 ⇒ 不许占端口（哪怕上一次开机它是通的 —— 那正是要废弃的快照）
    assert probe.concluded is False
    assert plugin._relay_should_listen() is False

    # ② 有结论、链路不通 ⇒ 不许占
    probe.concluded, probe._link_up = True, False
    assert plugin._relay_should_listen() is False

    # ③ 有结论、链路通 ⇒ 占
    probe.concluded, probe._link_up = True, True
    assert plugin._relay_should_listen() is True

    # ④ 手动档：不等链路判据，一律占（这正是这个开关的语义）
    probe.concluded, probe._link_up = False, False
    assert plugin.set_ignore_link(True) is None
    assert plugin._relay_should_listen() is True
    plugin.stop()


def test_listen_change_asks_for_a_resync(tmp_path, monkeypatch):
    """端口起了 / 放了 ⇒ 叫守护**现在**跑一轮（而不是等它最长 5s 的轮询）。

    叫的只此一处：探针翻转时**不叫**（那时端口还没放开，守护会被叫去看上一次的
    现场，而它拆隧道的判据就是那个端口 —— 白跑一轮还拆不掉）。

    用的是 ``resync`` 一帧：**意图一个字都没变**，光重发同一份意图发不出去
    （``declare`` 按内容哈希幂等），等下一轮节拍又要多等最长 5 秒。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    link = plugin.ctx.helper

    plugin._on_relay_listen_change(True)
    assert link.ops[-1] == ("resync", {"subject": TUNNEL_SUBJECT})

    n = len(link.ops)
    plugin._on_relay_listen_change(False)
    assert len(link.ops) == n + 1
    plugin.stop()


def test_carrying_follows_the_relay_not_a_snapshot(tmp_path, monkeypatch):
    """运行期档位的判据 = 「中继此刻占着端口」，不是「我们最后写了什么」。

    旧判据读的是「我们上次写进文件的 link_up」这个**快照**：手动档（链路判否、
    但中继如实占着端口）下它会说「没在接管」⇒ 探针每 3s 做一次完整下探、每次把
    中继停手 8s，把一条好端端的隧道勒死。现在只问端口在不在。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    plugin.set_automation_active(True)
    # 自动档、还没结论 ⇒ 门关着（中继不该占端口）
    assert plugin._relay._link_ready() is False

    # 手动档 + 未结论：门开了，但中继还没真占上端口 ⇒ 仍算「未接管」
    assert plugin.set_ignore_link(True) is None
    assert plugin._relay._link_ready() is True
    assert plugin._relay.listening is False
    assert plugin._carrying() is False

    # 让中继这一轮真的占上端口（端口真源是**构造期**绑定的方法引用，改配置读不到）
    monkeypatch.setattr(plugin._relay, "_ports_provider", lambda: (_free_port(), 9001))
    plugin._relay._converge()                      # 中继这一轮真占上端口
    try:
        assert plugin._relay.listening is True
        assert plugin._carrying() is True
    finally:
        plugin._relay._discard_listener()
    plugin.set_automation_active(False)
    plugin.stop()


def test_switch_resets_the_probe_conclusion(tmp_path, monkeypatch):
    """开关翻转 ⇒ 链路判定清零，下一次开机必须**重新量**。

    不清的后果很具体：上次留下的「链路就绪」会在开关重新打开的瞬间被当成当下的
    事实 ⇒ 中继立刻占上端口 ⇒ helper 把整机流量劫持进一条可能通往黑洞的隧道，
    等探针 6 秒后翻下来才收场。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    probe = plugin._probe
    probe.concluded, probe._link_up, probe._flip_votes = True, True, 2

    plugin.set_automation_active(True)
    assert probe.concluded is False
    assert probe.link_up is False
    assert probe._flip_votes == 0
    plugin.set_automation_active(False)
    plugin.stop()


def test_probe_reset_clears_cooldown_and_last_hop(tmp_path, monkeypatch):
    """``reset()`` 是**一次新的判定**：迟滞票、冷却、上次的失败层一起归零。

    只清一半会留一个更隐蔽的坑：票数还在 ⇒ 新的一轮里第一次 poll 就凑够票数翻转；
    冷却还在 ⇒ 明明链路刚插上却几十秒不重开。
    """
    ctrl = _probe(monkeypatch)
    ctrl.concluded, ctrl._link_up, ctrl._flip_votes = True, True, 2
    ctrl._cooldown_polls = 40
    ctrl.last_hop, ctrl.last_kind = HOP_CHANNEL, "port_refused"
    ctrl._last_reason = "旧原因"

    ctrl.reset()
    assert ctrl.concluded is False and ctrl.link_up is False
    assert ctrl._flip_votes == 0 and ctrl._cooldown_polls == 0
    assert ctrl.last_hop is None and ctrl.last_kind is None
    assert ctrl._last_reason is None


def test_switch_starts_and_stops_services_through_framework(tmp_path, monkeypatch):
    """端到端：开关 → ctx.resync_services() → 框架差量收敛起/停中继（端口随开随放）。

    这是「关着隧道不占 1080」「开开关立刻监听」两条真机验收口径的自动化替身。

    配 ``ignore_link``（手动档）：现场没有 iPad ⇒ 自动档下探针会如实判「链路未就绪」，
    中继按监听门不占端口，这里就测不到「开关一开就监听」了。手动档的语义正是
    「不等链路也占端口」，用它把链路这一维按住，剩下要验的就是框架的差量收敛。
    """
    from barkit.core.registry import Registry

    port = _free_port()
    user = tmp_path / "config.yaml"
    user.write_text(
        "plugins:\n  ipad_tunnel:\n    ignore_link: true\n"
        f"    local_port: {port}\n    device_port: 9001\n",
        encoding="utf-8",
    )
    _patch_env(tmp_path, monkeypatch)
    reg = Registry(Config(user_path=user))
    plugin = Plugin(reg.ctx)
    reg.plugins[plugin.id] = plugin
    plugin.start()
    reg.start_services(plugin)                 # 等价 Registry.load_all 的「start 后接线」

    assert reg.resident_host.active(plugin) == []      # 未启用 ⇒ 无服务
    assert _connect_ok(port) is False

    plugin.set_automation_active(True)                 # → ctx.resync_services()
    assert _wait(lambda: _connect_ok(port))            # 立刻在听
    assert reg.resident_host.active(plugin) == ["probe", "relay"]

    plugin.set_automation_active(False)
    assert _wait(lambda: not _connect_ok(port))        # 关闭即释放端口
    assert reg.resident_host.active(plugin) == []
    reg.stop_all()


def test_port_change_takes_effect_without_restarting_plugin(tmp_path, monkeypatch):
    """端口热改：改配置 → 中继在同一循环内换端口；插件不重启、线程不重建。

    手动档同上：现场没有 iPad，自动档下中继根本不会占端口，这条就无从测起。
    """
    from barkit.core.registry import Registry

    p1, p2 = _free_port(), _free_port()
    user = tmp_path / "config.yaml"
    user.write_text(
        "plugins:\n  ipad_tunnel:\n    tunnel_enabled: true\n    ignore_link: true\n"
        f"    local_port: {p1}\n    device_port: 9001\n",
        encoding="utf-8",
    )
    _patch_env(tmp_path, monkeypatch)
    reg = Registry(Config(user_path=user))
    plugin = Plugin(reg.ctx)
    reg.plugins[plugin.id] = plugin
    plugin.start()
    reg.start_services(plugin)
    assert _wait(lambda: _connect_ok(p1))

    plugin.ctx.config.set_plugin_section("ipad_tunnel", {"local_port": p2})
    assert _wait(lambda: _connect_ok(p2))              # 热改生效（无需重启插件）
    assert _connect_ok(p1) is False                    # 旧端口已释放
    assert reg.resident_host.thread_names() == [
        "resident:ipad_tunnel.probe", "resident:ipad_tunnel.relay",
    ]                                                  # 线程未重建
    reg.stop_all()


# ──────────────────────────────────────────────
# hev 执行前完整性自检（§7.4.5 P1，批次 4 · S4）
# ──────────────────────────────────────────────

def test_hev_integrity_check_missing_binary(tmp_path):
    ok, reason = tunnelops._hev_integrity_check(tmp_path / "hev-socks5-tunnel")
    assert not ok and "未安装" in reason


def test_hev_integrity_check_rejects_foreign_owner(tmp_path, monkeypatch):
    """非 root:wheel 的 Mach-O 由 root 执行 = 本地提权面，必须拒绝。"""
    hev = tmp_path / "hev-socks5-tunnel"
    hev.write_text("x", encoding="utf-8")
    monkeypatch.setattr(tunnelops.rootbin, "load_manifest",
                        lambda path=None: ({"hev-socks5-tunnel": "x"}, None))
    ok, reason = tunnelops._hev_integrity_check(hev)      # 测试进程不是 root
    assert not ok and "root:wheel" in reason


def test_hev_integrity_check_requires_manifest_entry(tmp_path, monkeypatch):
    """manifest 未登记 → 拒绝执行（基线缺失即视为不可信）。"""
    hev = tmp_path / "hev-socks5-tunnel"
    hev.write_text("x", encoding="utf-8")
    monkeypatch.setattr(tunnelops.rootbin, "check_owner_perms",
                        lambda path, **kw: (True, None))
    monkeypatch.setattr(tunnelops.rootbin, "load_manifest", lambda path=None: ({}, None))
    ok, reason = tunnelops._hev_integrity_check(hev)
    assert not ok and "基线缺失" in reason


def test_hev_integrity_check_rejects_bad_signature(tmp_path, monkeypatch):
    hev = tmp_path / "hev-socks5-tunnel"
    hev.write_text("x", encoding="utf-8")
    monkeypatch.setattr(tunnelops.rootbin, "check_owner_perms",
                        lambda path, **kw: (True, None))
    monkeypatch.setattr(tunnelops.rootbin, "load_manifest",
                        lambda path=None: ({"hev-socks5-tunnel": "x"}, None))
    monkeypatch.setattr(tunnelops.subprocess, "run",
                        lambda *a, **k: _Cmd(rc=1, err="code object is not signed at all"))
    ok, reason = tunnelops._hev_integrity_check(hev)
    assert not ok and "codesign" in reason


def test_hev_integrity_check_passes_on_codesign_ok(tmp_path, monkeypatch):
    """全绿路径，并断言用的正是 ``codesign --verify``（**不做哈希**：签名不可复现）。"""
    hev = tmp_path / "hev-socks5-tunnel"
    hev.write_text("x", encoding="utf-8")
    monkeypatch.setattr(tunnelops.rootbin, "check_owner_perms",
                        lambda path, **kw: (True, None))
    monkeypatch.setattr(tunnelops.rootbin, "load_manifest",
                        lambda path=None: ({"hev-socks5-tunnel": "x"}, None))
    seen = {}

    def _run(cmd, **kw):
        seen["cmd"] = list(cmd)
        return _Cmd(rc=0)

    monkeypatch.setattr(tunnelops.subprocess, "run", _run)
    ok, reason = tunnelops._hev_integrity_check(hev)
    assert ok, reason
    assert seen["cmd"][:2] == ["/usr/bin/codesign", "--verify"]


def test_cycle_hev_integrity_failure_takes_tunnel_down(helper_env, monkeypatch):
    """执行前自检不过 → 拒绝启动 hev 并拆隧道（不降级、不硬起）。"""
    src, fake = helper_env
    _intent(enabled=True)
    monkeypatch.setattr(tunshell, "_find_utun", lambda: "utun9")
    monkeypatch.setattr(tunhev, "_hev_integrity_check",
                        lambda path: (False, "签名结构不完整"))
    state = _cycle()
    assert state["active"] is False
    assert state["error"] and "自检" in state["error"]
    joined = [" ".join(c) for c in fake.calls]
    assert not any("route -n add" in j for j in joined)


# ──────────────────────────────────────────────
# 测试专用端口隔离（2026-09-20 重做）：自证
# ──────────────────────────────────────────────

def test_test_session_moves_relay_off_the_app_port(tmp_path):
    """自证：测试会话把 ipad_tunnel 中继默认端口从 1080 挪到了空闲端口。

    发货语义锁死：``DEFAULT_LOCAL_PORT`` 必须是 1080（主程序隧道端口）；
    生产路径（``Plugin.config_schema``）不改这个值。
    测试路径：conftest 的 autouse 固件（``_inject_builtin_plugin_schemas``）在注入
    内置 schema 时把 ``ipad_tunnel.local_port`` 默认值改写成会话空闲端口，两条注入
    路径（``Config.__init__`` / ``Config.set_plugin_schemas``，后者是
    ``app_shell.assemble`` 走的、唯一真会把中继跑起来的路径）都覆盖。

    反向验证：把 conftest 的改写逻辑去掉，本会话构造的 Config 读到的
    ``plugins.ipad_tunnel.local_port`` 会回落成 1080、与「生产路径默认值」相等
    ⇒ 本断言转红。谁把隔离又改回「假守」，这条会立刻报。
    """
    # 发货值锁死：改了它 == 改了主程序隧道端口语义
    assert tunnel_spec.DEFAULT_LOCAL_PORT == 1080

    # 测试路径：autouse 固件已改写内置 schema 的默认端口（生产 schema 的默认值
    # 仍是上行的常量，conftest 不碰生产字段定义）。
    user = tmp_path / "config.yaml"
    user.write_text("plugins:\n  ipad_tunnel:\n    enabled: false\n", encoding="utf-8")
    cfg = Config(user_path=user)
    test_lp = cfg.get("plugins.ipad_tunnel.local_port")
    # 隔离真的生效：测试会话里默认端口 ≠ 发货的 1080
    assert test_lp != tunnel_spec.DEFAULT_LOCAL_PORT
    # 且是个合法端口（1..65535）
    assert 1 <= int(test_lp) <= 65535
