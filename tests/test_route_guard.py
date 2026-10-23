"""route_guard 测试（P3 3.1~3.4）。

覆盖：
- helper 3.1：目标解析、schema 校验拒绝、route_get 解析、幂等增删（fake 路由表）、
  规则匹配/不匹配驱动 add/remove、规则文件非法保持路由不动、getpacket 降级兜底
- daemon 3.2：plist 模板关键字段、版本单一来源、安装/卸载脚本内容
- rules 3.3：build/校验抛错、原子写内容
- plugin 3.4：start 声明期望态、tick 读观测广播、立即检查发 resync 命令、面板
"""

from __future__ import annotations

import inspect
import ipaddress
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import yaml

import pytest

from barkit.contribution.specs import (
    StateSpec,
    StatusRowSpec,
    StateReport,
    SwitchSpec,
)
from barkit.core.config import Config
from barkit.core.events import EventBus
from barkit import paths
from barkit.core.contributions import gather_specs
from barkit.core.plugin import PluginContext
from barkit.capabilities.helper import core as helper, routeops

# 2026-10-02 ``routeops`` 由单模块拆成包 ⇒ 被 monkeypatch 的那批名字**真身在兄弟模块**，
# 而包只是把它们再导出了一遍 ⇒ 补丁打在包上（绑定副本）真身纹丝不动，判据静默失效。
# 故这里给真身子模块各留一个别名，补丁点一律落在它们身上。
from barkit.capabilities.helper import safeio as routeops_safeio
from barkit.capabilities.helper.routeops import (baseline as routeops_baseline,
                                                 converge as routeops_converge,
                                                 lease as routeops_lease,
                                                 netmode as routeops_netmode,
                                                 netsvc as routeops_netsvc,
                                                 routeops as routeops_cycle,
                                                 routes as routeops_routes)
from barkit.capabilities.helper import state as helper_state
from barkit.capabilities import helper_client as daemon
from barkit.plugins.route_guard import rules
from barkit.plugins.route_guard.plugin import Plugin as RouteGuardPlugin
from barkit.plugins.route_guard.plugin import _SHIPPED_DEFAULT_PROFILES
from barkit.shell.panel import StatusPanel, _StatusRowView

# ──────────────────────────────────────────────
# 公共


class _StubContrib:
    """最小贡献桩：仅供 StatusPanel.set_components 注入状态规范（白盒测试）。"""

    def __init__(self, states):
        self._states = list(states)

    def states(self):
        return self._states

    def components(self):
        return []
# ──────────────────────────────────────────────


class FakeRes:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def _probe_of(cidr: str) -> str:
    return str(ipaddress.ip_network(cidr, strict=False).network_address)


class FakeRouteTable:
    """模拟路由表：route get 返回当前表；route add/delete 直接改表（幂等语义）。"""

    def __init__(self):
        self.routes: dict[str, dict] = {}   # probe_ip -> route_get dict
        self.ops: list[tuple[str, str, str]] = []   # (verb, cidr, gateway)
        self.iface_status = True
        self.packet_gateways: list[str] = []
        self.default_gateway = "10.1.1.1"

    # ---- 由 monkeypatch 注入 routeops.route_get ----
    def route_get(self, ip: str) -> dict | None:
        if ip in self.routes:
            return dict(self.routes[ip])
        return {
            "destination": "default",
            "mask": "default",
            "gateway": self.default_gateway,
            "interface": "en0",
        }

    # ---- 由 monkeypatch 注入 routeops.subprocess.run（route/ifconfig/ipconfig） ----
    def run(self, args: list[str], **kw):
        if args[0] == "/sbin/route":
            # 实参形如 [route, -n, add, -net, cidr, gateway]
            verb, cidr, gw = args[2], args[4], args[5]
            self.ops.append((verb, cidr, gw))
            probe = _probe_of(cidr)
            if verb == "add":
                net = ipaddress.ip_network(cidr, strict=False)
                self.routes[probe] = {
                    "destination": str(net.network_address),
                    "mask": str(net.netmask),
                    "gateway": gw,
                    "interface": "en5",
                }
                return FakeRes(0)
            if verb == "delete":
                self.routes.pop(probe, None)
                return FakeRes(0)
            return FakeRes(1)
        if args[0] == "/sbin/ifconfig":
            if self.iface_status:
                return FakeRes(0, "en5: flags=8863<UP> mtu 1500\n\tstatus: active")
            return FakeRes(0, "en5: flags=8863<UP> mtu 1500\n\tstatus: inactive")
        if args[0] == "/usr/sbin/ipconfig":
            if self.packet_gateways:
                return FakeRes(0, f"router (ip_mult): {{{', '.join(self.packet_gateways)}}}")
            return FakeRes(1, "no packet")
        return FakeRes(1)


@pytest.fixture
def helper_env(tmp_path, monkeypatch):
    """helper 数据目录指到 tmp；注入 fake 路由表。"""
    monkeypatch.setenv("BARKIT_DATA_DIR", str(tmp_path))
    table = FakeRouteTable()
    monkeypatch.setattr(routeops_routes, "route_get", table.route_get)
    monkeypatch.setattr(routeops.subprocess, "run", table.run)
    return tmp_path, table


def _intent_text(rules_list, poll=30, paused=False, handback=()) -> str:
    """档 B：app 侧算好的**完整期望态**文本（每条规则必带 apply）。

    版本号**取常量不写死**（`routeops.INTENT_VERSION`）：写死的数在两轮契约 bump
    （2→3→4）里都留在了旧值上，而 helper 当时**只在白名单里收下 version、从不比它**，
    于是这些夹具一直在写一个「helper 不支持的版本」而没人发现 —— 真锁一落地（2026-09-19）
    它们集体转红。原 ``_intent_text_v3``（另写一个 ``version: 3``）已并入本函数。
    """
    return json.dumps(
        {"version": routeops.INTENT_VERSION, "poll": poll, "paused": paused,
         "handback": list(handback), "rules": rules_list},
        ensure_ascii=False,
    )


def _apply(rule, apply_flag=True, trigger=True):
    """给规则定义补上意图字段（apply / trigger_matched）。"""
    return {**rule, "apply": apply_flag, "trigger_matched": trigger}


def _observed_route() -> dict:
    """守护侧**最近一次路由观测**（取代旧的读盘上 ``state.json``）。

    ⚠️ 这是**进程内存**里那份（``state.record`` 记下的），不是文件：通道换掉之前观测
    是 helper 每轮原子写下的文件，现在它随每轮推给订阅方、盘上只剩一份崩溃恢复用的
    快照（``helper_snapshot.json``，见 ``capabilities/helper/state.py``）。
    """
    return helper_state.observed(helper_state.SUBJECT_ROUTE)


def _forget_the_books():
    """清空守护那本内存账（进程级单例，不清就带着上一条用例的残留演）。

    ``net_gateways`` 是**只累积**的一格（网卡上取到的网关记下来就不再清），
    而 ``net_env`` 只把盘上目录指到 tmp、不动内存 —— 于是上一条用例记下的网关会
    被这一条读到。既有的两条用例（``…_is_processwide`` 那两处）早就是这么做的。
    """
    helper_state._OBSERVED.clear()


OFFICE = {
    "name": "office",
    "priority": 1,
    "iface": "en5",
    "gateway": "10.99.1.254",
    "destinations": ["10.99.2.0/24"],
}


def _profile(name="office", iface="en5", gateway="10.99.1.254",
             destinations=("10.99.2.0/24",), **overrides):
    """新配置模型的**场景** fixture（§2.1）；``overrides`` 覆盖任意字段。

    ``OFFICE`` 是 helper 线格式的形状（`routeops` 的校验用例直接吃它）；本 fixture 是
    配置侧的 ``profiles[]`` 形状，喂给 ``rules.build_routes``。两者是**编译输入与输出**
    的关系，不是两个版本的同一份东西。
    """
    profile = {
        "name": name,
        "ifaces": [iface],
        "match": {"gateway": gateway},
        "net": {"mode": "dhcp"},
        "routes": {
            "enabled": True,
            "items": [{"destination": d, "via": gateway} for d in destinations],
        },
    }
    profile.update(overrides)
    return profile


# ──────────────────────────────────────────────
# helper：解析与 schema
# ──────────────────────────────────────────────

def test_parse_destination():
    p = routeops.parse_destination("10.99.2.0/24")
    assert p["network"] == "10.99.2.0"
    assert p["mask"] == "255.255.255.0"
    assert p["probe"] == "10.99.2.0"
    assert p["cidr"] == "10.99.2.0/24"
    assert routeops.parse_destination("192.168.100.5/32")["mask"] == "255.255.255.255"
    assert routeops.parse_destination("not-an-ip") is None
    assert routeops.parse_destination("10.0.0.0/8")["mask"] == "255.0.0.0"


def test_validate_routes_rejects_bad_schema():
    ok, err = routeops.validate_routes({"version": routeops.INTENT_VERSION, "poll": 30, "rules": [OFFICE]})
    assert err is None and ok["rules"][0]["name"] == "office"

    _, err = routeops.validate_routes({"version": routeops.INTENT_VERSION, "poll": 30, "rules": [], "evil": 1})
    assert err and "未知字段" in err

    _, err = routeops.validate_routes({"version": routeops.INTENT_VERSION, "poll": "fast", "rules": []})
    assert err and "poll" in err

    bad_rule = {**OFFICE, "gateway": "999.1.1.1"}
    _, err = routeops.validate_routes({"version": routeops.INTENT_VERSION, "poll": 30, "rules": [bad_rule]})
    assert err and "gateway" in err

    bad_rule = {**OFFICE, "destinations": ["10.99.2.0/999"]}
    _, err = routeops.validate_routes({"version": routeops.INTENT_VERSION, "poll": 30, "rules": [bad_rule]})
    assert err and "destinations" in err

    bad_rule = {**OFFICE, "evil": True}
    _, err = routeops.validate_routes({"version": routeops.INTENT_VERSION, "poll": 30, "rules": [bad_rule]})
    assert err and "未知字段" in err


def test_validate_routes_sorts_by_priority():
    home = {**OFFICE, "name": "home", "priority": 9}
    raw = {"version": routeops.INTENT_VERSION, "poll": 30, "rules": [home, OFFICE]}
    data, err = routeops.validate_routes(raw)
    assert err is None
    assert [r["name"] for r in data["rules"]] == ["office", "home"]


def test_validate_routes_accepts_paused_and_enabled():
    data, err = routeops.validate_routes(
        {"version": routeops.INTENT_VERSION, "poll": 30, "paused": True, "rules": [{**OFFICE, "enabled": False}]}
    )
    assert err is None
    assert data["paused"] is True
    assert data["rules"][0]["enabled"] is False


def test_validate_routes_off_destinations_must_be_a_subset():
    """``off_destinations``（v7）：必须是 ``destinations`` 的子集，元素都得是合法目标。

    「撤一个我没声明过的目标」等于让 helper 去动一条它这一轮本不该管的明细路由 ⇒
    宁可整份拒绝 —— 与「未知字段一律整份拒绝」同一口径，不猜用户想撤哪一条。
    """
    rule = _apply({**OFFICE,
                   "destinations": ["10.99.2.0/24", "10.2.0.0/16"],
                   "off_destinations": ["10.2.0.0/16", "10.2.0.0/16"]})
    data, err = routeops.validate_routes(
        {"version": routeops.INTENT_VERSION, "poll": 30, "rules": [rule]})
    assert err is None
    assert data["rules"][0]["off_destinations"] == ["10.2.0.0/16"], "重复的收成一个"

    for bad, why in (
        ("10.2.0.0/16", "不是数组"),
        (["10.9.9.9/32"], "撤一个 destinations 里没有的目标"),
        (["not-a-cidr"], "元素不是合法目标"),
    ):
        got, err2 = routeops.validate_routes(
            {"version": routeops.INTENT_VERSION, "poll": 30,
             "rules": [{**rule, "off_destinations": bad}]})
        assert got == {} and err2 and "off_destinations" in err2, why

    # 不带这一栏照旧合法（纯增量：v6 那份形状语义一个字没改）
    plain = _apply({**OFFICE, "destinations": ["10.99.2.0/24"]})
    got, err3 = routeops.validate_routes(
        {"version": routeops.INTENT_VERSION, "poll": 30, "rules": [plain]})
    assert err3 is None and "off_destinations" not in got["rules"][0]


def test_validate_routes_rejects_bad_paused_and_enabled():
    _, err = routeops.validate_routes({"version": routeops.INTENT_VERSION, "poll": 30, "paused": "yes", "rules": []})
    assert err and "paused" in err
    _, err = routeops.validate_routes({"version": routeops.INTENT_VERSION, "poll": 30, "rules": [{**OFFICE, "enabled": "on"}]})
    assert err and "enabled" in err


# ──────────────────────────────────────────────
# helper：单轮主流程
# ──────────────────────────────────────────────

def test_cycle_match_adds_route(helper_env):
    src, table = helper_env
    table.packet_gateways = ["10.99.1.254"]     # en5 网关匹配规则
    routeops.run_cycle(_intent_text([_apply(OFFICE, True)]))
    assert ("add", "10.99.2.0/24", "10.99.1.254") in table.ops
    state = _observed_route()
    assert state["ok"] is True
    assert state["rules"][0]["matched"] is True
    assert state["rules"][0]["destinations"][0]["ok"] is True


def test_cycle_match_idempotent_skips_add(helper_env):
    src, table = helper_env
    table.packet_gateways = ["10.99.1.254"]
    table.routes["10.99.2.0"] = {           # 路由已存在且经规则网关
        "destination": "10.99.2.0",
        "mask": "255.255.255.0",
        "gateway": "10.99.1.254",
        "interface": "en5",
    }
    routeops.run_cycle(_intent_text([_apply(OFFICE, True)]))
    assert table.ops == []                  # 幂等：已生效不再 add


def test_cycle_no_match_removes_owned_route(helper_env):
    src, table = helper_env
    table.packet_gateways = ["192.168.2.1"]  # 不在办公室（app 侧据此算出 apply=False）
    table.routes["10.99.2.0"] = {            # 此前 helper 加的路由
        "destination": "10.99.2.0",
        "mask": "255.255.255.0",
        "gateway": "10.99.1.254",
        "interface": "en5",
    }
    routeops.run_cycle(_intent_text([_apply(OFFICE, False, trigger=False)]))
    assert ("delete", "10.99.2.0/24", "10.99.1.254") in table.ops
    state = _observed_route()
    assert state["rules"][0]["matched"] is False


def test_cycle_no_match_keeps_foreign_route(helper_env):
    """不匹配且目标当前经其他网关 → 绝不删除（宁不清理不误删）。"""
    src, table = helper_env
    table.packet_gateways = ["192.168.2.1"]
    table.default_gateway = "192.168.2.1"    # 该网段走默认（非规则网关）
    routeops.run_cycle(_intent_text([_apply(OFFICE, False, trigger=False)]))
    assert not any(v[0] == "delete" for v in table.ops)


def test_cycle_adds_one_destination_and_removes_another(helper_env):
    """同一条规则里「A 加、B 撤」（v7）—— 逐目标开关落在线格式上的那一栏。

    改回「一条规则只有一个动作」的旧写法时转红的判据：B 只会被 add（旧写法按整条的
    ``apply`` 一位走），那条「已经叠上去、现在被关掉」的路由就撤不下来了。
    """
    src, table = helper_env
    table.packet_gateways = ["10.99.1.254"]
    table.routes["10.2.0.0"] = {             # B 此前由本规则叠上，现在这条目标被关掉
        "destination": "10.2.0.0",
        "mask": "255.255.0.0",
        "gateway": "10.99.1.254",
        "interface": "en5",
    }
    rule = _apply({**OFFICE,
                   "destinations": ["10.99.2.0/24", "10.2.0.0/16"],
                   "off_destinations": ["10.2.0.0/16"]}, True)
    state = routeops.run_cycle(_intent_text([rule]))

    assert ("add", "10.99.2.0/24", "10.99.1.254") in table.ops
    assert ("delete", "10.2.0.0/16", "10.99.1.254") in table.ops
    assert state["rules"][0]["applied"] is True, "整条在叠，只有其中一条目标在撤"
    by_dest = {d["destination"]: d for d in state["rules"][0]["destinations"]}
    assert by_dest["10.99.2.0/24"]["action"] == "add"
    assert by_dest["10.2.0.0/16"]["action"] == "remove"


def test_cycle_bad_intent_keeps_routes(helper_env):
    """意图文件非法 → 整份拒绝，保持上轮路由不动（与迁移前一致）。"""
    src, table = helper_env
    table.packet_gateways = ["10.99.1.254"]
    routeops.run_cycle(
        f'{{"version": {routeops.INTENT_VERSION}, "poll": 30, "rules": [{{"evil": 1}}]}}')
    assert table.ops == []
    state = _observed_route()
    assert state["ok"] is False and state["error"]


def test_cycle_version_mismatch_freezes_routes(helper_env):
    """契约版本不符 → **整份拒绝 ＋ 冻结**：连一条 route 命令都不许发（E4 ② 真锁）。

    真锁落地前做不到这件事：那时 helper 只是把 ``version`` 收进白名单、从不拿它比，
    于是「helper 与 app 不是同一代」在 root 侧完全看不出来，照单执行 —— 而语义可能
    已经在两代之间变过（例如 ``destinations: []`` 的含义）。
    这里让旧 app 的文件**本该撤掉**一条已生效路由（``apply=False``）：被拒之后它必须
    还在，否则「冻结」就是句空话。
    """
    src, table = helper_env
    table.packet_gateways = ["10.99.1.254"]
    stale_version = routeops.INTENT_VERSION - 1
    table.routes["10.99.2.0"] = {              # 上轮已生效的路由
        "destination": "10.99.2.0",
        "mask": "255.255.255.0",
        "gateway": "10.99.1.254",
        "interface": "en5",
    }
    stale = json.dumps({"version": stale_version, "poll": 30, "paused": False,
                        "rules": [_apply(OFFICE, False)]}, ensure_ascii=False)
    routeops.run_cycle(stale)

    assert table.ops == [], "文件被拒时一条 route 命令都不该发"
    assert table.routes["10.99.2.0"]["gateway"] == "10.99.1.254", "上轮路由不许被动"
    state = _observed_route()
    assert state["ok"] is False and state["has_intent"] is False
    assert "契约版本不符" in state["error"]
    # 报错要给全**两侧**版本号 —— 只说一边，用户不知道该重装哪一头
    assert f"v{routeops.INTENT_VERSION}" in state["error"]
    assert str(stale_version) in state["error"]


def test_cycle_missing_intent_freezes(helper_env):
    """意图文件不存在（app 未运行/尚未首探）→ 冻结，不是错误。"""
    src, table = helper_env
    routeops.run_cycle()
    assert table.ops == []
    state = _observed_route()
    assert state["ok"] is True and state["has_intent"] is False and state["error"] is None


def test_cycle_early_return_keeps_the_gateway_ledger(helper_env):
    """「这一轮没有意图」也要把网关账带下去 —— 它不是这一轮的事实。

    ``net_gateways`` 记的是「这块网卡上一次被收敛时看到的网关」，只在网卡还握在我们
    手里时积累一次，之后全靠往下带。早退写出的 state 若没有这一栏，``record`` 是
    **整体替换** ⇒ 账被抹空；而网卡一旦被固化成 Manual，``ipconfig getpacket`` 必然
    失败、网卡上也未必留着 router（``pin`` 只固化地址时就不写它），这本账是「留空的
    下一跳」唯一还剩的来源（2026-10-19 真机：账空 ⇒ office 命中后三个目标全 hold）。
    """
    _src, _table = helper_env
    helper_state._OBSERVED.clear()
    ledger = {"en5": {"gateway": "10.99.1.1",
                      "net": {"mode": "dhcp", "pin": {"ip": "", "mask": ""}}}}
    helper_state.record(helper_state.SUBJECT_ROUTE,
                        {"ok": True, "rules": [], "net_gateways": dict(ledger)})

    routeops.run_cycle()                       # 意图文件不存在
    assert helper_state.read_state()["net_gateways"] == ledger, "早退不该把账抹掉"

    routeops.run_cycle("not json")             # 意图正文非法（读到半截）
    assert helper_state.read_state()["net_gateways"] == ledger, "被拒的那一轮也不该抹"


def test_cycle_recovers_the_gateway_from_an_existing_route(net_env):
    """账被抹空、租约也读不到 ⇒ 从「已经叠上去的那条路由」把网关捡回来。

    真机 2026-10-19：``net_gateways`` 是 ``{}``、``pyxis`` 停在 Manual 且 Router 为
    ``(null)``，而两条旧路由仍指向 ``10.99.1.254``。账本该记得，但它被历史版本抹过
    —— 系统里那条路由是唯一还剩的证据。捡回来的同时记进账，下一轮不再依赖它。
    """
    _src, table, net = net_env
    service = net.define("en5", mode="static", ip="10.99.1.34",
                         mask="255.255.255.0", gateway="")
    table.packet_gateways = []                     # 租约读不到（Manual 的正常现场）
    table.routes["10.99.2.0"] = {"destination": "10.99.2.0", "mask": "255.255.255.0",
                                 "gateway": "10.99.1.254", "interface": "en5"}
    _forget_the_books()
    rule = {**OFFICE, "gateway": "",
            "destinations": ["10.99.2.0/24", "10.2.0.0/16"],
            "net": {"mode": "dhcp",
                    "pin": {"ip": "10.99.1.34", "mask": "255.255.255.0"}}}

    routeops.run_cycle(_intent_text([_apply(rule, True)]))
    state = _observed_route()
    assert state["rules"][0]["gateway"] == "10.99.1.254"
    assert state["rules"][0]["gateway_src"] == "iface"
    assert ("add", "10.2.0.0/16", "10.99.1.254") in table.ops, "缺的那条要补上"
    assert state["net_gateways"]["en5"]["gateway"] == "10.99.1.254", "捡到就记账"
    assert net.configs[service]["gateway"] == "", "router 仍然不写回网卡"


def test_cycle_does_not_adopt_a_route_from_another_iface(net_env):
    """同目标的路由走在**别的网卡**上 ⇒ 那不是本场景的网关，按「取不到」办。

    这条兜底只认「目标 ＋ 本网卡」两个条件同时成立：否则家里那条默认路由（``en0``）
    会被当成办公室的下一跳，把路由叠到一条已经不在的网关上去。
    """
    _src, table, net = net_env
    net.define("en5", mode="static", ip="10.99.1.34", mask="255.255.255.0")
    table.packet_gateways = []
    table.routes["10.2.0.0"] = {"destination": "10.2.0.0", "mask": "255.255.0.0",
                                "gateway": "192.168.0.1", "interface": "en0"}
    _forget_the_books()
    rule = {**OFFICE, "gateway": "", "destinations": ["10.2.0.0/16"],
            "net": {"mode": "dhcp",
                    "pin": {"ip": "10.99.1.34", "mask": "255.255.255.0"}}}

    routeops.run_cycle(_intent_text([_apply(rule, True)]))
    state = _observed_route()
    assert state["rules"][0]["gateway_src"] == "none"
    assert table.ops == [], "取不到网关就一条都不动"


def test_cycle_reports_paused_and_applies_intent(helper_env):
    """暂停是 app 侧的 policy：helper 只照意图执行，并把 paused 回显给面板。"""
    src, table = helper_env
    table.packet_gateways = ["10.99.1.254"]
    table.routes["10.99.2.0"] = {           # 此前已生效的路由
        "destination": "10.99.2.0",
        "mask": "255.255.255.0",
        "gateway": "10.99.1.254",
        "interface": "en5",
    }
    # 暂停态下 app 算出的意图：沿用上轮（apply=True）→ helper 幂等，无增删
    routeops.run_cycle(_intent_text([_apply(OFFICE, True)], paused=True))
    assert table.ops == []                  # 已生效 + intent 仍要它生效 ⇒ 不动
    state = _observed_route()
    assert state["paused"] is True
    # 已生效路由仍在
    assert ("10.99.2.0" in table.routes) and (table.routes["10.99.2.0"]["gateway"] == "10.99.1.254")


def test_cycle_paused_never_touches_routes_without_prior_echo(helper_env, monkeypatch):
    """停用 ＋ 回显缺位：**一条路由都不许撤**。

    ``apply`` 在停用态是 app 侧「冻结上轮」的结果，而它冻得住全靠本函数写出的那份回显。
    守护重启 / 会话重连 / 快照还没读回来时回显是空的 ⇒ 冻结退化成 ``False`` ⇒ 照它收敛
    等于把生效中的路由全撤掉（2026-10-17，冷启动那条误报的镜像面）。

    §3.2「停用 = 不碰」因此不能只靠 app 记得住上轮 —— 必须在 helper 这一侧也成立：
    停用期间路由一个都不动。判据只能落在「有没有真的发过 route add/delete」上。
    """
    src, table = helper_env
    table.packet_gateways = ["10.99.1.254"]
    table.routes["10.99.2.0"] = {           # 此前已生效的路由
        "destination": "10.99.2.0",
        "mask": "255.255.255.0",
        "gateway": "10.99.1.254",
        "interface": "en5",
    }
    monkeypatch.setattr(routeops, "read_state", lambda: {})   # 守护刚重启：账上什么都还没有

    routeops.run_cycle(_intent_text([_apply(OFFICE, False)], paused=True))

    assert table.ops == [], f"停用态动了路由：{table.ops}"
    assert "10.99.2.0" in table.routes


def test_cycle_paused_echoes_the_previous_applied(helper_env):
    """停用期间回显照上轮记账 —— 报 app 那份（已退化的）估计只会越冻越确信该撤。"""
    src, table = helper_env
    table.packet_gateways = ["10.99.1.254"]
    routeops.run_cycle(_intent_text([_apply(OFFICE, True)]))      # 真叠一轮
    assert ("add", "10.99.2.0/24", "10.99.1.254") in table.ops
    table.ops.clear()

    # 暂停 ＋ app 的冻结失败（apply 退化成 False）
    routeops.run_cycle(_intent_text([_apply(OFFICE, False)], paused=True))

    assert table.ops == []
    assert "10.99.2.0" in table.routes
    assert _observed_route()["rules"][0]["applied"] is True, "回显要照上轮，不是照那份估计"


def test_cycle_apply_false_removes_owned_route(helper_env):
    """app 算出 apply=false（禁用/强制关）→ 撤掉该路由且下一轮不再回加。"""
    src, table = helper_env
    table.packet_gateways = ["10.99.1.254"]   # 网关在线（撤路由与否由意图决定）
    table.routes["10.99.2.0"] = {              # 此前 helper 加的路由
        "destination": "10.99.2.0",
        "mask": "255.255.255.0",
        "gateway": "10.99.1.254",
        "interface": "en5",
    }
    text = _intent_text([_apply({**OFFICE, "enabled": False}, False, trigger=True)])
    routeops.run_cycle(text)
    # 第一轮：撤掉该规则路由
    assert ("delete", "10.99.2.0/24", "10.99.1.254") in table.ops
    state = _observed_route()
    assert state["rules"][0]["matched"] is False      # 触发命中但被禁用
    assert state["rules"][0]["applied"] is False
    # 路由已不在表内
    assert "10.99.2.0" not in table.routes
    # 第二轮：意图未变 → 不再回加（验证“不回加”）
    table.ops.clear()
    routeops.run_cycle(text)
    assert not any(v[0] == "add" for v in table.ops)


# ──────────────────────────────────────────────
# helper：网卡配置（§2.5 的配套三件事：baseline 快照 / 回滚窗口 / 交还系统）
# ──────────────────────────────────────────────

#: 这些用例全部自己传 ``now``（``_apply_net`` / ``_rollback_if_lost`` 都收这个参数），
#: 故取一个固定时刻即可 —— 拿 ``time.time()`` 会让「宽限期 / 窗口」这两条边界用例
#: 在机器慢的时候偶发（窗口是秒级的）。
_NOW = 1_700_000_000


class FakeNetworksetup:
    """模拟 ``networksetup``：维护每个「服务」的配置，记录每一次命令。

    这是**本组件唯一会改掉用户网络状态**的地方（§2.5），所以桩要能让用例问出
    「**到底发了哪几条命令**」—— 断言「没动网卡」时，唯一站得住的证据就是本次调用里
    没有 ``-setmanual`` / ``-setdhcp`` / ``-setdnsservers``。

    服务名的口径与真机一致：``-listallhardwareports`` 里 ``Hardware Port`` 那一行
    就是服务名（``service_for_iface`` 取的正是它）。
    """

    def __init__(self, ports=(("en5", "USB 10/100/1000 LAN"),)):
        self.ports = list(ports)          # (device, hardware port / 服务名)
        self.configs: dict[str, dict] = {}
        self.packets: dict[str, str] = {}  # device -> ``ipconfig getpacket`` 的原文
        self.cmds: list[list[str]] = []
        self.broken: set[str] = set()     # 让某条子命令失败（模拟读不出来）

    def service_of(self, device: str) -> str | None:
        for dev, port in self.ports:
            if dev == device:
                return port
        return None

    def define(self, device: str, *, mode="dhcp", ip="", mask="", gateway="",
               dns=()) -> str:
        """预置「这块网卡现在长这样」。返回它对应的服务名。"""
        service = self.service_of(device)
        assert service is not None, f"{device} 不在 hardware ports 里"
        self.configs[service] = {
            "mode": mode, "ip": ip, "mask": mask, "gateway": gateway,
            "dns": list(dns),
        }
        return service

    def packet(self, device: str, *, ip="", mask="", gateway="", dns=(),
               lease_time="0x001c20", server="") -> None:
        """预置「这块网卡**刚从 DHCP 服务端拿到**的那份租约」（``ipconfig getpacket`` 原文）。

        形状照抄真机：顶层字段用 ``=``、options 段用 ``:``、多条地址写成 ``{a,b}``
        —— 这三条正是解析器的全部依据。**不存在** ``packet()`` 与
        ``configs`` 之间的一致性要求：这份文件里的地址与网卡此刻的配置本来就是两回事
        （网卡被我们固化着时，只有这里还留着真正的租约）。
        """
        lines = ["op = BOOTREPLY", f"yiaddr = {ip}", "siaddr = 0.0.0.0", "options:"]
        lines.append(f"subnet_mask (ip): {mask}")
        if gateway:
            lines.append(f"router (ip_mult): {{{gateway}}}")
        if dns:
            lines.append(f"domain_name_server (ip_mult): {{{', '.join(dns)}}}")
        lines.append(f"lease_time (uint32): {lease_time}")
        if server:
            lines.append(f"server_identifier (ip): {server}")
        self.packets[device] = "\n".join(lines) + "\n"

    # ---- 由 monkeypatch 注入 routeops._net_cmd ----
    def __call__(self, args, timeout: int = 15):
        self.cmds.append([str(a) for a in args])
        op = args[1] if len(args) > 1 else ""
        if op in self.broken:
            return 1, ""
        if op == "-listallhardwareports":
            return 0, "".join(
                f"Hardware Port: {port}\nDevice: {dev}\n\n" for dev, port in self.ports
            )
        if op == "getpacket":
            # 网卡不在 DHCP 模式时真机就是这么答的（见 ``read_lease`` 的 docstring）。
            # 这道闸必须照真机建：固化之后网卡是 Manual，此时**没有**租约可读 ——
            # 早先假桩对 Manual 网卡照旧供包，把「重启后卡在固化失败」整条回归放过了
            # （2026-09-30 真机快照里那段 error 即此）。
            service = self.service_of(args[2])
            cfg = self.configs.get(service) if service else None
            if cfg is not None and cfg.get("mode") != "dhcp":
                return 1, "ipconfig_get_packet(...) failed: not found"
            text = self.packets.get(args[2])
            return (0, text) if text else (1, "ipconfig_get_packet(...) failed: not found")
        if op == "-getinfo":
            cfg = self.configs.get(args[2])
            if cfg is None:
                return 1, ""
            # 真机 ``-getinfo`` 的字段名大小写**随模式变**，照抄两种形态（解析器一律
            # 小写比对，用例若照着一种写死，另一种就永远测不到）。
            head = "Manual Configuration" if cfg["mode"] == "static" else "DHCP Configuration"
            key = "IP Address" if cfg["mode"] == "static" else "IP address"
            return 0, (
                f"{head}\n{key}: {cfg['ip']}\n"
                f"Subnet mask: {cfg['mask']}\nRouter: {cfg['gateway']}\n"
            )
        if op == "-getdnsservers":
            dns = (self.configs.get(args[2]) or {}).get("dns") or []
            if not dns:
                return 0, f"There aren't any DNS Servers set on {args[2]}.\n"
            return 0, "".join(f"{s}\n" for s in dns)
        if op == "-setmanual":
            self.configs.setdefault(args[2], {}).update({
                "mode": "static", "ip": args[3], "mask": args[4],
                "gateway": args[5] if len(args) > 5 else "",
            })
            return 0, ""
        if op == "-setdhcp":
            self.configs.setdefault(args[2], {}).update(
                {"mode": "dhcp", "ip": "", "mask": "", "gateway": "", "dns": []}
            )
            return 0, ""
        if op == "-setdnsservers":
            rest = args[3:]
            self.configs.setdefault(args[2], {})["dns"] = (
                [] if rest == ["empty"] else list(rest)
            )
            return 0, ""
        return 1, ""

    def writes(self) -> list[list[str]]:
        """真正改网卡的那些命令（读命令不算）—— 断言「一根手指都没碰」用这个。"""
        return [c for c in self.cmds
                if c[1] in ("-setmanual", "-setdhcp", "-setdnsservers")]


@pytest.fixture
def net_env(tmp_path, monkeypatch):
    """在 ``helper_env`` 之上再挂一块假网卡 ＋ 一个能写的 baseline 目录。

    ``SYS_RUN_DIR`` 必须指到 tmp：baseline 快照按设计落在 root 私有的系统域
    （``/Library/Application Support/BarKit``），测试里不能真去写那儿。
    """
    monkeypatch.setenv("BARKIT_DATA_DIR", str(tmp_path))
    table = FakeRouteTable()
    monkeypatch.setattr(routeops_routes, "route_get", table.route_get)
    monkeypatch.setattr(routeops.subprocess, "run", table.run)
    net = FakeNetworksetup()
    monkeypatch.setattr(routeops_netmode, "_net_cmd", net)
    run_dir = tmp_path / "sysrun"
    run_dir.mkdir()
    monkeypatch.setattr(routeops_safeio, "SYS_RUN_DIR", run_dir)
    return tmp_path, table, net


def test_validate_net_pure_dhcp_and_pin_are_not_the_same_thing():
    """不写 ``net`` / 写 ``net: {mode: dhcp}`` / 写 ``pin`` 是**三件事**（§2.5）。

    前两者在用户看来一样，在代码里差很远：不写＝不产生任何写入、也不留 baseline；
    写 ``dhcp`` ＝声明「跟随系统」但仍然没有要固化的地址。混起来就会出现「配了个空
    场景却留下了 baseline」这种说不清的残留。
    """
    assert routeops.validate_net(None) == (None, None)

    clean, err = routeops.validate_net({"mode": "dhcp"})
    assert err is None and clean == {"mode": "dhcp"}

    # ``pin`` 的 ip / mask 要的是**键**，值可以留空 = 用 DHCP 拿到的那个
    clean, err = routeops.validate_net({"mode": "dhcp", "pin": {"ip": "", "mask": ""}})
    assert err is None
    assert clean == {"mode": "dhcp", "pin": {"ip": "", "mask": ""}}

    clean, err = routeops.validate_net({
        "mode": "dhcp", "pin": {"ip": "10.0.0.5", "mask": "255.255.255.0",
                                "gateway": "10.0.0.1", "dns": ["8.8.8.8"]},
    })
    assert err is None
    assert clean["pin"]["dns"] == ["8.8.8.8"]

    clean, err = routeops.validate_net(
        {"mode": "static", "ip": "10.0.0.5", "mask": "255.255.255.0"}
    )
    assert err is None and clean["mode"] == "static"


@pytest.mark.parametrize("net, keyword", [
    # 不是对象
    ("dhcp", "必须是对象"),
    # 闭集：多一个键就整份拒绝（这条路径会改网卡，宁可不动也不猜）
    ({"mode": "dhcp", "ip": "10.0.0.5"}, "不得直接写"),
    ({"mode": "dhcp", "dns": ["8.8.8.8"]}, "不得直接写 dns"),
    ({"mode": "dhcp", "whatever": 1}, "未知字段"),
    ({"mode": "  "}, "只能"),
    # static 的三条：ip/mask 必填、不得带 pin
    ({"mode": "static", "mask": "255.255.255.0"}, "ip 必填"),
    ({"mode": "static", "ip": "10.0.0.5"}, "mask 必填"),
    ({"mode": "static", "ip": "10.0.0.5", "mask": "255.255.255.0", "pin": {}}, "不得再写 pin"),
    # pin 自己：两个键都要在、值要合法、不许有野字段
    ({"mode": "dhcp", "pin": {"ip": "10.0.0.5"}}, "两个键都在"),
    ({"mode": "dhcp", "pin": {"ip": "10.0.0.5", "mask": "255.255.255.0", "x": 1}}, "未知字段"),
    ({"mode": "dhcp", "pin": {"ip": "999.1.1.1", "mask": "255.255.255.0"}}, "不是合法 IPv4"),
    ({"mode": "dhcp", "pin": []}, "必须是对象"),
    # 「照抄租约」两个布尔键：只认 true（写 false 与不写等价，不留多余语义）
    ({"mode": "dhcp", "pin": {"ip": "", "mask": "", "gateway_from_lease": False}}, "只能写 true"),
    ({"mode": "dhcp", "pin": {"ip": "", "mask": "", "dns_from_lease": "yes"}}, "只能写 true"),
    # ……且与同名的写死值**互斥**（一个照抄、一个写死 = 用户自己也没想清，不猜哪个优先）
    ({"mode": "dhcp", "pin": {"ip": "", "mask": "", "gateway": "10.0.0.1",
                              "gateway_from_lease": True}}, "二者只能选一个"),
    ({"mode": "dhcp", "pin": {"ip": "", "mask": "", "dns": ["8.8.8.8"],
                              "dns_from_lease": True}}, "二者只能选一个"),
])
def test_validate_net_rejects_illegal_shapes(net, keyword):
    """非法一律整份拒绝：这条路径写的是网卡，猜错一次就是一秒级断网。"""
    clean, err = routeops.validate_net(net)
    assert clean is None and err and keyword in err


def test_validate_net_accepts_the_lease_pin_flags():
    """只写 ``true`` 的照抄开关是合法形状，且原样收下（helper 侧是 root，不信送进来的东西）。

    与上面那条「拒非法」成对：只测拒、不测收，改动方向反了（把合法形状也拒了）不会红。
    """
    clean, err = routeops.validate_net({
        "mode": "dhcp",
        "pin": {"ip": "", "mask": "", "gateway_from_lease": True, "dns_from_lease": True},
    })
    assert err is None
    assert clean == {"mode": "dhcp",
                     "pin": {"ip": "", "mask": "",
                             "gateway_from_lease": True, "dns_from_lease": True}}
    # 键在但值不是 true ⇒ 拒（同名的写死值留空无妨：空值本来就不算「给了答案」）
    clean, err = routeops.validate_net({
        "mode": "dhcp",
        "pin": {"ip": "", "mask": "", "gateway": "", "gateway_from_lease": True},
    })
    assert err is None and clean["pin"]["gateway_from_lease"] is True


def test_target_of_takes_address_from_lease_but_never_gateway():
    """「留空 ＝ 用 DHCP 拿到的值」**只对 pin 的 ip / mask 成立**（§2.5）。

    网关 / DNS 留空是「**不固化**」，不是「照抄租约」。照抄网关会把「继续跟随 DHCP」
    悄悄变成「钉死在某个网关」—— 那正是用户写空值要躲开的事。
    """
    lease = {"mode": "dhcp", "ip": "192.168.1.5", "mask": "255.255.255.0",
             "gateway": "192.168.1.1", "dns": ["192.168.1.1"]}

    # 纯 DHCP（没有 pin）⇒ 不写
    assert routeops._target_of({"mode": "dhcp"}, lease) == (None, None)

    target, err = routeops._target_of({"mode": "dhcp", "pin": {"ip": "", "mask": ""}}, lease)
    assert err is None
    assert target == {"mode": "dhcp+pin", "ip": "192.168.1.5",
                      "mask": "255.255.255.0", "gateway": "", "dns": []}
    assert target["gateway"] == "", "网关留空是不固化，不许照抄租约"
    assert target["mode"] == routeops.MODE_DHCP_PIN, "落盘形态是运行态 dhcp+pin，不是配置里的第三个 mode"

    # 显式填了就用填的
    target, err = routeops._target_of(
        {"mode": "dhcp", "pin": {"ip": "10.0.0.5", "mask": "255.255.255.0",
                                 "gateway": "10.0.0.1", "dns": ["8.8.8.8"]}}, lease
    )
    assert err is None and target["ip"] == "10.0.0.5" and target["dns"] == ["8.8.8.8"]

    # 租约也补不出地址 ⇒ 报错（绝不猜一个地址写下去）
    target, err = routeops._target_of({"mode": "dhcp", "pin": {"ip": "", "mask": ""}}, {})
    assert target is None and err and "租约" in err


def test_target_of_takes_gateway_and_dns_from_the_lease_only_when_asked():
    """显式勾了「照抄」才把租约里的网关 / DNS 固化（2026-09-18 用户裁定补的能力）。

    与上一条成对：上一条钉的是「留空 = 不固化」，这一条钉的是「要固化得有第二个键」。
    两件事必须分得开 —— 用户要的是「这根手指本来就该碰这一格」，而不是给留空换个意思。
    """
    lease = {"mode": "dhcp", "ip": "192.168.1.5", "mask": "255.255.255.0",
             "gateway": "192.168.1.1", "dns": ["192.168.1.1"]}
    both = {"mode": "dhcp", "pin": {"ip": "", "mask": "",
                                    "gateway_from_lease": True, "dns_from_lease": True}}

    # 勾了 ⇒ 租约里那两位一并固化（地址仍走「留空 = 用 DHCP 拿到的」）
    target, err = routeops._target_of(both, lease)
    assert err is None
    assert target == {"mode": routeops.MODE_DHCP_PIN, "ip": "192.168.1.5",
                      "mask": "255.255.255.0",
                      "gateway": "192.168.1.1", "dns": ["192.168.1.1"]}

    # 不勾 ⇒ 老行为一个字不变（已经写着的配置绝不会因为这次改动突然开始钉网关）
    target, err = routeops._target_of({"mode": "dhcp", "pin": {"ip": "", "mask": ""}}, lease)
    assert err is None and target["gateway"] == "" and target["dns"] == []

    # 勾了但租约里没有那一位（或只有 `(null)` 这种本地化残留）⇒ **当成没这项**，不编值
    for gateway in (None, "", "(null)"):
        thin = {"ip": "192.168.1.5", "mask": "255.255.255.0", "gateway": gateway}
        target, err = routeops._target_of(both, thin)
        assert err is None, err
        assert target["gateway"] == "", f"租约里没有可用网关（{gateway!r}）不该编一个出来"
        assert target["dns"] == []

    # 照抄来的网关照样过「不在本网段就不写」那道硬拦（照抄不是免检通道）
    target, err = routeops._target_of(both, {**lease, "gateway": "10.0.0.1"})
    assert target is None and err and "不在" in err


@pytest.mark.parametrize("net", [
    {"mode": "static", "ip": "10.0.0.5", "mask": "255.255.255.0", "gateway": "192.168.1.1"},
    {"mode": "dhcp", "pin": {"ip": "10.0.0.5", "mask": "255.255.255.0",
                             "gateway": "192.168.1.1"}},
])
def test_target_of_refuses_gateway_outside_subnet(net):
    """落笔**之前**的硬拒绝：网关不在本网段 ⇒ 不写。

    这种配置会把网卡变成一张出不去也进不来的网，而**回滚窗口探不到这种坏法** ——
    地址确实在网卡上、默认路由看着也正常。既然探不到，就只能在写之前拦住。
    """
    target, err = routeops._target_of(net, {})
    assert target is None and err and "不在" in err


def test_same_compares_gateway_but_only_declared_dns():
    """``_same`` 比 ip / mask / gateway，DNS 只在 target 声明了才比。

    gateway 恒比是因为「我们没声明」＝「写下去就是没有 router」（``_write_target`` 省略
    该参数即清空）。网卡上多出任何一个合法 router 都算漂移 —— 留着它会被系统当成默认
    路由，影响其他路由与 Apple 连续互通（2026-10-19 用户裁定）。清空后读回 ``(null)``
    这类非 IP 一律当「没有」，否则每轮都判漂移、30 秒重写一次网卡。

    DNS 仍然只在声明时才比：那一位的归属是调用方按各自口径处置的，拿它判漂移只会白写。
    """
    target = {"mode": "static", "ip": "10.0.0.5", "mask": "255.255.255.0",
              "gateway": "", "dns": []}
    current = {"mode": "static", "ip": "10.0.0.5", "mask": "255.255.255.0",
               "gateway": "10.0.0.254", "dns": ["1.1.1.1"]}
    assert routeops._same(current, target) is False, "多出来的 router 算漂移（必须抹）"
    assert routeops._same({**current, "gateway": ""}, target) is True
    assert routeops._same({**current, "gateway": "(null)"}, target) is True, \
        "清空后的读回值不是 IP ⇒ 当「没有」"

    assert routeops._same(current, {**target, "ip": "10.0.0.9"}) is False
    assert routeops._same(current, {**target, "gateway": "10.0.0.1"}) is False
    assert routeops._same(current, {**target, "dns": ["8.8.8.8"]}) is False

    # ``target=None`` ＝「它该是 DHCP」
    assert routeops._same({"mode": "dhcp"}, None) is True
    assert routeops._same(current, None) is False
    # 读不出 / 模式认不出 ⇒ 一律当「不是那份」（保守：只会多写一次）
    assert routeops._same(None, target) is False
    assert routeops._same({"mode": ""}, target) is False

    # static 与 dhcp+pin 落盘后读回来都是 Manual，故按目标比对时一律认 static 读数
    assert routeops._same({**current, "gateway": ""},
                          {**target, "mode": "dhcp+pin"}) is True


def test_converge_iface_pure_dhcp_touches_nothing(net_env):
    """纯 DHCP ⇒ **一根手指都不碰**（§2.5）。

    顺手 ``setdhcp`` 会把用户自己在系统设置里配的静态地址改掉 —— 用户配的
    ``net: {mode: dhcp}`` 意思是「这事我不管」，不是「请你把它改成 DHCP」。
    """
    _src, _table, net = net_env
    net.define("en5", mode="static", ip="192.168.1.5", mask="255.255.255.0",
               gateway="192.168.1.1")

    res = routeops._converge_iface("en5", {"mode": "dhcp"}, handback=False)
    assert res["action"] == "none" and res["managed"] is False
    assert res["mode"] == routeops.MODE_DHCP
    assert res["wrote"] is None
    assert net.writes() == [], "纯 DHCP 不许发任何写命令"
    # 但 baseline 必须已经留下 —— 它是「以后要交还给谁」的唯一依据
    assert net.service_of("en5") in routeops.read_baseline()


def test_converge_iface_writes_once_then_goes_idempotent(net_env):
    """同配置第二轮**不再写**：helper 每轮都把同一份意图再说一遍。

    「说一遍就写一遍」等于每 30s 重配一次网卡 —— 那是一秒级断网，用户只会觉得
    网络时不时抽一下，屏幕上什么都看不出来。
    """
    _src, _table, net = net_env
    service = net.define("en5")           # 初始是 DHCP
    cfg = {"mode": "static", "ip": "10.99.1.10", "mask": "255.255.255.0",
           "gateway": "10.99.1.254"}

    res = routeops._converge_iface("en5", cfg, handback=False)
    assert res["action"] == "applied" and res["managed"] is True
    assert res["mode"] == routeops.MODE_STATIC
    assert res["wrote"]["ip"] == "10.99.1.10" and res["net"] == cfg
    assert [c[1] for c in net.writes()] == ["-setmanual"]
    assert net.configs[service]["mode"] == "static"

    net.cmds.clear()
    res2 = routeops._converge_iface("en5", cfg, handback=False)
    assert res2["action"] == "applied" and res2["managed"] is True
    assert net.writes() == [], "已经在目标那一份上 ⇒ 不写"


def test_converge_iface_refuses_when_service_or_config_or_baseline_missing(
        net_env, monkeypatch):
    """三条 fail-closed 出口：查不到服务名 / 读不出当前配置 / baseline 留不下来。

    三条的共同判据是「**一条写命令都没发**」—— 这台机器要是正在用这块网卡，写错一次
    就是一秒级断网，所以「没有依据」的答案只能是「不动」。
    """
    _src, _table, net = net_env
    cfg = {"mode": "static", "ip": "10.99.1.10", "mask": "255.255.255.0"}

    # ① 设备名 → 服务名查不到（网卡拔了 / 名字写错）
    res = routeops._converge_iface("en9", cfg, handback=False)
    assert res["action"] == "failed" and "服务名" in res["error"]
    assert net.writes() == []

    # ② 服务名查得到，但 ``-getinfo`` 读不出（非英文系统 / 命令失败）
    net.define("en5")
    net.broken.add("-getinfo")
    res = routeops._converge_iface("en5", cfg, handback=False)
    assert res["action"] == "failed" and "读不出" in res["error"]
    assert net.writes() == []

    # ③ baseline 写不下去（系统域不可写）⇒ 宁可不改
    net.broken.clear()

    def _boom(*_a, **_kw):
        raise OSError("read-only file system")

    monkeypatch.setattr(routeops_safeio, "_atomic_write_json", _boom)
    res = routeops._converge_iface("en5", cfg, handback=False)
    assert res["action"] == "failed" and "baseline" in res["error"]
    assert net.writes() == [], "留不下 baseline 就不许动网卡（否则交还时无人可交）"


def test_baseline_keeps_only_the_earliest_snapshot(net_env, monkeypatch):
    """baseline **只留最早那份**：第二次改动再覆盖，交还回去的成了「上一次我们写进去的值」。

    那等于交还失败，而且用户看不出来（网卡上看着确实变了一次）。
    """
    _src, _table, net = net_env
    service = net.define("en5", mode="static", ip="192.168.1.5",
                         mask="255.255.255.0", gateway="192.168.1.1")
    first = dict(routeops.read_service_config(service))

    assert routeops.ensure_baseline(service, "en5", first) is True
    handed = dict(routeops.read_baseline()[service])
    assert handed["mode"] == "static" and handed["ip"] == "192.168.1.5"

    # 改一次（我们写进去的），再改一次 —— baseline 不许被这份新值顶掉
    routeops._converge_iface("en5", {"mode": "static", "ip": "10.99.1.10",
                                     "mask": "255.255.255.0"}, handback=False)
    routeops._converge_iface("en5", {"mode": "static", "ip": "10.99.1.11",
                                     "mask": "255.255.255.0"}, handback=False)
    assert routeops.read_baseline()[service] == handed


def test_handback_restores_baseline_and_does_nothing_when_already_there(net_env):
    """``[交还系统]`` 的落点：还原到 baseline，且**幂等**。

    幂等不是省一条命令 —— 是正确性：「无匹配时交还」是**策略**（每轮都会说一遍），
    不幂等的话 helper 每 30s 就 ``setdhcp`` 一次，用户的网络被周期性打断一下，
    而屏幕上什么都看不出来。
    """
    _src, _table, net = net_env
    service = net.define("en5", mode="dhcp", ip="192.168.1.5",
                         mask="255.255.255.0", gateway="192.168.1.1")
    routeops.ensure_baseline(service, "en5", routeops.read_service_config(service))
    routeops._converge_iface("en5", {"mode": "static", "ip": "10.99.1.10",
                                     "mask": "255.255.255.0"}, handback=False)
    assert net.configs[service]["mode"] == "static"

    net.cmds.clear()
    res = routeops._converge_iface("en5", {}, handback=True)
    assert res["action"] == "handback" and res["error"] is None
    # ``-setdhcp`` 之后还要显式收 DNS：baseline 里没记 DNS（当初本来就没有）时，
    # 不显式清掉我们写上去的那份，交还就只交还了一半。
    assert [c[1] for c in net.writes()] == ["-setdhcp", "-setdnsservers"]
    assert net.configs[service]["mode"] == "dhcp"

    net.cmds.clear()
    res2 = routeops._converge_iface("en5", {}, handback=True)
    assert res2["action"] == "handback"
    assert net.writes() == [], "已经停在 baseline 上 ⇒ 不再写（否则每轮断一次网）"


def test_handback_dns_flag_skips_dns_restore(net_env):
    """「恢复 DNS」复选框取消（skip_dns=True）时，交还只还原地址、不动 DNS。"""
    _src, _table, net = net_env
    service = net.define("en5", mode="dhcp", ip="192.168.1.5",
                         mask="255.255.255.0", gateway="192.168.1.1")
    routeops.ensure_baseline(service, "en5", routeops.read_service_config(service))
    routeops._converge_iface("en5", {"mode": "static", "ip": "10.99.1.10",
                                     "mask": "255.255.255.0"}, handback=False)
    assert net.configs[service]["mode"] == "static"

    # 缺省（skip_dns=False）⇒ 还原地址 + 收 DNS 两条命令
    net.cmds.clear()
    res = routeops._converge_iface("en5", {}, handback=True, skip_dns=False)
    assert res["action"] == "handback"
    assert [c[1] for c in net.writes()] == ["-setdhcp", "-setdnsservers"], \
        "缺省要连 DNS 一起还原"

    # 重新钉回静态，再试 skip_dns=True ⇒ 只 -setdhcp，不碰 DNS
    routeops._converge_iface("en5", {"mode": "static", "ip": "10.99.1.10",
                                     "mask": "255.255.255.0"}, handback=False)
    net.cmds.clear()
    res2 = routeops._converge_iface("en5", {}, handback=True, skip_dns=True)
    assert res2["action"] == "handback"
    assert [c[1] for c in net.writes()] == ["-setdhcp"], "取消恢复 DNS ⇒ 只还原地址"
    assert net.configs[service]["mode"] == "dhcp"


def test_apply_net_handback_dns_flag_skips_dns(net_env):
    """意图里 ``handback_dns=False`` 时，`_apply_net` 交还只还原地址、不动 DNS。"""
    _src, _table, net = net_env
    service = net.define("en5", mode="dhcp", ip="192.168.1.5",
                         mask="255.255.255.0", gateway="192.168.1.1")
    routeops.ensure_baseline(service, "en5", routeops.read_service_config(service))
    routeops._converge_iface("en5", {"mode": "static", "ip": "10.99.1.10",
                                     "mask": "255.255.255.0"}, handback=False)

    net.cmds.clear()
    routeops._apply_net(
        {"paused": False, "handback": ["en5"], "handback_dns": False, "rules": []},
        {}, _NOW)
    assert [c[1] for c in net.writes()] == ["-setdhcp"], "handback_dns=False ⇒ 不动 DNS"


def test_restore_baseline_without_snapshot_writes_nothing(net_env):
    """baseline 缺失一律不猜：宁可不交还，也不替用户编一份配置写下去。"""
    _src, _table, net = net_env
    service = net.define("en5", mode="static", ip="10.99.1.10",
                         mask="255.255.255.0")
    assert routeops.read_baseline() == {}
    assert routeops._restore_baseline(service) is False
    assert net.cmds == [], "没有 baseline ⇒ 连读都不必读，更不许写"


def test_net_lost_ignores_gateway_when_iface_is_not_the_default_route(net_env, monkeypatch):
    """回滚探针只取「拿得到可靠证据」的那一半：默认路由不在本网卡时不比网关。

    ``route -n get default`` 给的是**别人**的网关（它不看接口）。拿它当我们的判据，
    会在完全正常的配置上误判成失联，然后回滚掉一份好配置 —— 那比不报更坏。
    """
    _src, table, _net = net_env
    monkeypatch.setattr(routeops_routes, "current_ipv4", lambda iface: ["10.0.0.5"])
    wrote = {"ip": "10.0.0.5", "gateway": "10.0.0.1"}

    # 默认路由走 en0（``FakeRouteTable.route_get`` 的缺省）⇒ 网关不参与判定
    assert table.route_get("default")["interface"] == "en0"
    assert routeops._net_lost("en5", wrote) is False

    # 地址不在了 ⇒ 失联（这条判据与默认路由无关，``ifconfig`` 永远探得动）
    assert routeops._net_lost("en5", {**wrote, "ip": "10.0.0.9"}) is True

    # 默认路由确实走本网卡，且网关对不上 ⇒ 失联
    table.routes["default"] = {"destination": "default", "mask": "default",
                               "gateway": "10.0.0.254", "interface": "en5"}
    assert routeops._net_lost("en5", wrote) is True
    table.routes["default"]["gateway"] = "10.0.0.1"
    assert routeops._net_lost("en5", wrote) is False

    # 没写网关 ⇒ 只比地址（``_net_lost`` 的早退）
    assert routeops._net_lost("en5", {"ip": "10.0.0.5", "gateway": ""}) is False


def test_rollback_requires_all_four_gates(net_env, monkeypatch):
    """回滚窗口四条判据**缺一不回滚**：宁可不回滚，也不误回滚（§2.5）。

    窗口存在的理由是「别把用户关在门外」；而误回滚会把一份**好的**配置换掉，
    同样是断网。两个方向都要付代价，所以判据收得比「可能出问题」紧得多。
    """
    _src, _table, net = net_env
    service = net.define("en5", mode="dhcp")
    routeops.ensure_baseline(service, "en5", routeops.read_service_config(service))
    wrote = {"mode": "static", "ip": "10.0.0.5", "mask": "255.255.255.0",
             "gateway": "10.0.0.1", "dns": []}
    prev = {"action": "applied", "service": service, "iface": "en5",
            "wrote": wrote, "net": {"mode": "static", "ip": "10.0.0.5",
                                    "mask": "255.255.255.0"}, "applied_at": _NOW}
    monkeypatch.setattr(routeops_routes, "current_ipv4", lambda iface: [])

    # ① 上一轮没真写过 / 已经退过
    assert routeops._rollback_if_lost({**prev, "action": "none"}, _NOW + 10) is None
    assert routeops._rollback_if_lost({**prev, "rolled_back": True}, _NOW + 10) is None
    # ② 宽限期内（刚 setmanual 完地址还没落定，立刻探必然探不到）
    assert routeops._rollback_if_lost(prev, _NOW + 1) is None
    # ③ 出了窗口（窗口是给「刚改完那一刻」留的退路）
    assert routeops._rollback_if_lost(
        prev, _NOW + routeops.ROLLBACK_WINDOW_S + 1) is None
    # ④ 没有可探的东西
    assert routeops._rollback_if_lost(
        {**prev, "wrote": {**wrote, "ip": "", "gateway": ""}}, _NOW + 10) is None

    # 四条齐 ⇒ 回 baseline，且结果标成 rolled_back 与 failed 严格区分
    out = routeops._rollback_if_lost(prev, _NOW + 10)
    assert out["action"] == "rolled_back" and out["rolled_back"] is True
    assert out["managed"] is False and out["error"] is None
    assert out["net"] == prev["net"], "带上那份意图，面板才说得清它是为哪份配置退的"
    assert net.configs[service]["mode"] == "dhcp"


def test_recently_rolled_back_blocks_the_same_intent_only():
    """防「写坏 → 回滚 → 再写坏」的循环；但**用户改了配置就该重试**。

    没有这道闸，一条恒写不成功的配置会每轮转一圈：写 → 探不到 → 回滚 → 再写，
    而每转一圈都在断用户的网。
    """
    cfg = {"mode": "static", "ip": "10.0.0.5", "mask": "255.255.255.0"}
    prev = {"action": "rolled_back", "iface": "en5", "net": cfg, "applied_at": _NOW}

    assert routeops._recently_rolled_back(prev, "en5", cfg, _NOW + 5) is True
    # 意图变了 ⇒ 放行（闸只拦同一份意图）
    assert routeops._recently_rolled_back(
        prev, "en5", {**cfg, "ip": "10.0.0.9"}, _NOW + 5) is False
    # 换了网卡 ⇒ 放行
    assert routeops._recently_rolled_back(prev, "en6", cfg, _NOW + 5) is False
    # 窗口过了 ⇒ 放行
    assert routeops._recently_rolled_back(
        prev, "en5", cfg, _NOW + routeops.ROLLBACK_WINDOW_S + 1) is False
    # 上一轮不是「刚退过」⇒ 不拦
    assert routeops._recently_rolled_back(
        {**prev, "action": "applied"}, "en5", cfg, _NOW + 5) is False


def test_idle_net_carries_the_previous_segment_instead_of_blanking_it(net_env, monkeypatch):
    """本轮不收不散 ≠ 把上一轮那段抹掉。

    回滚窗口的判据挂在「上一轮写下去的地址 / 网关」上，而它最典型的触发场景恰恰是
    本轮什么都不做（配置写坏 ⇒ 网卡失联 ⇒ 连「命中」都不成立）。盖成空段的话，
    那个窗口永远不会触发 —— 这段代码存在的唯一理由会被自己抹掉。
    """
    _src, _table, _net = net_env
    wrote = {"mode": "static", "ip": "10.0.0.5", "mask": "255.255.255.0",
             "gateway": "10.0.0.1", "dns": []}
    prev = {"iface": "en5", "service": "USB 10/100/1000 LAN", "mode": "static",
            "managed": True, "wrote": wrote, "net": {"mode": "static"},
            "action": "applied", "applied_at": _NOW}

    # 从没管过 ⇒ 一张干净的空段
    blank = routeops._idle_net({}, False, _NOW)
    assert blank["action"] == "none" and blank["managed"] is False

    # 上一轮管过、此刻还在我们手上 ⇒ 原样带下去（``wrote`` 必须在，回滚窗口靠它）
    monkeypatch.setattr(routeops_converge, "_still_ours", lambda service, p: True)
    out = routeops._idle_net(prev, False, _NOW + 100)
    assert out["action"] == "applied" and out["managed"] is True
    assert out["wrote"] == wrote

    # 停用 ⇒ frozen，且**不去探活**（§3.2「停用 = 不碰 = 保持现状」）

    def _must_not_probe(*_a):
        raise AssertionError("停用期间不该去读网卡")

    monkeypatch.setattr(routeops_converge, "_still_ours", _must_not_probe)
    assert routeops._idle_net(prev, True, _NOW)["action"] == "frozen"

    # 用户手改过 / 系统换过地址 ⇒ 读数跟上，不再声称在管（但服务名留着，供面板指认）
    monkeypatch.setattr(routeops_converge, "_still_ours", lambda service, p: False)
    out = routeops._idle_net(prev, False, _NOW)
    assert out["action"] == "none" and out["managed"] is False
    assert out["iface"] == "en5" and out["service"] == prev["service"]


def test_pick_handback_prefers_the_iface_we_already_manage():
    """交还清单里先动「上一轮真管过的那张」。

    不这么做会出现一个安静的漏：清单里有两张网卡时每轮都从头取第一张，第二张
    永远轮不到 —— 而真正需要交还的只有我们写过的那张。
    """
    hand = ["en5", "en6"]
    managed = {"managed": True, "iface": "en6"}
    assert routeops._pick_handback(hand, managed) == "en6"
    # 我们管的那张不在清单里 / 没管过 ⇒ 老老实实从头取
    assert routeops._pick_handback(hand, {"managed": True, "iface": "en7"}) == "en5"
    assert routeops._pick_handback(hand, {}) == "en5"


def test_apply_net_converges_then_hands_back_then_goes_idle(net_env, monkeypatch):
    """``_apply_net`` 的四条判据，**顺序就是分量**（§2.5 / §4.7）。

    有场景要接管 ⇒ 收敛到**第一个**（``rules`` 已按 priority 排过，§2.1「第 1 个
    命中者胜」）；没场景要 ⇒ 按交还清单还原；都没有 ⇒ 保持现状。
    """
    _src, _table, net = net_env
    service = net.define("en5", mode="dhcp", ip="192.168.1.5",
                         mask="255.255.255.0", gateway="192.168.1.1")
    cfg = {"mode": "static", "ip": "10.99.1.10", "mask": "255.255.255.0",
           "gateway": "10.99.1.254"}

    data = {"paused": False, "handback": [], "rules": [
        {"iface": "en5", "apply": False, "net": cfg},      # 不该生效的那个
        {"iface": "en5", "apply": True, "net": cfg},       # 第一个 apply 的说了算
    ]}
    first = routeops._apply_net(data, {}, _NOW)
    assert first["action"] == "applied" and first["managed"] is True
    assert net.configs[service]["mode"] == "static", "只收敛 apply 的那一条"

    # 场景要接管的那张，即便同时挂在交还清单里也**不交还** —— 否则两条互相拆台，
    # 同一轮里先把地址写进去、再把它还原掉。（由「收敛分支早退」保证，不是靠过滤。）
    routeops._apply_net(
        {"paused": False, "handback": ["en5"], "rules": [{"iface": "en5", "apply": True,
                                                          "net": cfg}]},
        first, _NOW + 50)
    assert net.configs[service]["mode"] == "static", "正在接管的网卡不许被交还"

    # 场景不接管 + 交还清单点了这张 ⇒ 交还（而不是「上一轮那段」原样带下去）
    second = routeops._apply_net(
        {"paused": False, "handback": ["en5"], "rules": []}, first, _NOW + 100)
    assert second["action"] == "handback", "没场景要接管 ⇒ 按交还清单还原"
    assert net.configs[service]["mode"] == "dhcp"

    # 交还之后就不再声称在管了；再一轮既无场景也无清单 ⇒ 干净的空段
    third = routeops._apply_net({"paused": False, "handback": [], "rules": []},
                                second, _NOW + 200)
    assert third["action"] == "none" and third["managed"] is False


def test_apply_net_paused_keeps_hands_off_but_still_hands_back(net_env, monkeypatch):
    """停用 ⇒ 不收不散，**但交还照办**（§3.2 ＋ §4.7 三）。

    照意图收敛会把网卡重新写一遍 —— 那不是「保持现状」。而 `[交还系统]` 是用户
    当场按下的**动作**，不是自动化，停用态下仍须可达。
    """
    _src, _table, net = net_env
    service = net.define("en5", mode="dhcp", ip="192.168.1.5",
                         mask="255.255.255.0", gateway="192.168.1.1")
    cfg = {"mode": "static", "ip": "10.99.1.10", "mask": "255.255.255.0"}

    # 停用 + 规则里 apply=True（暂停态下 app 算的是「沿用上轮」）⇒ 仍然不写
    paused = routeops._apply_net(
        {"paused": True, "handback": [], "rules": [{"iface": "en5", "apply": True,
                                                    "net": cfg}]},
        {}, _NOW)
    assert paused["action"] == "frozen" and paused["managed"] is False
    assert net.writes() == [], "停用时不许碰网卡"

    # 停用 + 交还请求 ⇒ 照办
    routeops.ensure_baseline(service, "en5", routeops.read_service_config(service))
    routeops._converge_iface("en5", cfg, handback=False)
    net.cmds.clear()
    hand = routeops._apply_net(
        {"paused": True, "handback": ["en5"], "rules": []},
        {"managed": True, "iface": "en5", "service": service}, _NOW)
    assert hand["action"] == "handback" and net.configs[service]["mode"] == "dhcp"

    # 停用 ⇒ 连回滚窗口都不探（代价是停用期间失联不会自动回滚，那正是「保持现状」）
    prev = {"action": "applied", "service": service, "iface": "en5",
            "wrote": {"ip": "10.99.1.10"}, "applied_at": _NOW}
    monkeypatch.setattr(routeops_routes, "current_ipv4", lambda iface: [])
    out = routeops._apply_net({"paused": True, "handback": [], "rules": []}, prev, _NOW + 10)
    assert out["action"] == "frozen", "停用期间不回滚"


def test_apply_net_rollback_outranks_convergence(net_env, monkeypatch):
    """回滚**先于**收敛：上一轮把自己关在门外时，这一轮该退回去。

    照着同一份（可能正是它把网卡配坏的）意图再写一遍，等于把用户再关一次。
    """
    _src, _table, net = net_env
    service = net.define("en5", mode="dhcp")
    routeops.ensure_baseline(service, "en5", routeops.read_service_config(service))
    cfg = {"mode": "static", "ip": "10.99.1.10", "mask": "255.255.255.0"}
    # 上一轮真写过（写下去的东西现在不在网卡上了 —— 被改掉 / 丢了）
    routeops._converge_iface("en5", cfg, handback=False)
    net.configs[service]["ip"] = "10.99.1.99"
    monkeypatch.setattr(routeops_routes, "current_ipv4", lambda iface: [])
    prev = {"action": "applied", "service": service, "iface": "en5",
            "wrote": {**cfg, "gateway": "", "dns": []}, "net": cfg,
            "applied_at": _NOW}
    net.cmds.clear()

    out = routeops._apply_net(
        {"paused": False, "handback": [], "rules": [{"iface": "en5", "apply": True,
                                                     "net": cfg}]},
        prev, _NOW + 10)
    assert out["action"] == "rolled_back"
    ops = [c[1] for c in net.writes()]
    assert "-setdhcp" in ops, "退回去"
    assert "-setmanual" not in ops, "不再写一遍那份（可能正是它把网卡配坏的）意图"


def test_apply_net_skips_rewriting_right_after_a_rollback(net_env, monkeypatch):
    """刚为同一张网卡、同一份意图退过一次 ⇒ 这一轮不再写回去。

    否则一条恒写不成功的配置每轮转一圈（写 → 探不到 → 回滚 → 再写），每圈都在断网。
    """
    _src, _table, net = net_env
    service = net.define("en5", mode="dhcp")
    routeops.ensure_baseline(service, "en5", routeops.read_service_config(service))
    monkeypatch.setattr(routeops_routes, "current_ipv4", lambda iface: [])
    cfg = {"mode": "static", "ip": "10.99.1.10", "mask": "255.255.255.0"}
    rolled = {"action": "rolled_back", "rolled_back": True, "service": service,
              "iface": "en5", "wrote": {**cfg, "gateway": "", "dns": []},
              "net": cfg, "applied_at": _NOW + 4}
    net.cmds.clear()

    out = routeops._apply_net(
        {"paused": False, "handback": [], "rules": [{"iface": "en5", "apply": True,
                                                     "net": cfg}]},
        rolled, _NOW + 10)
    assert out["action"] == "rolled_back" and net.writes() == [], "闸拦住了同一份意图"

    # 用户改了配置（意图变了）⇒ 该重试
    routeops._apply_net(
        {"paused": False, "handback": [],
         "rules": [{"iface": "en5", "apply": True, "net": {**cfg, "ip": "10.99.1.11"}}]},
        rolled, _NOW + 10)
    assert [c[1] for c in net.writes()] == ["-setmanual"], "换了一份配置就该重试"


# ──────────────────────────────────────────────
# helper：命令通道（用户当场按下的显式动作，§4.7 二）
# ──────────────────────────────────────────────

def test_read_lease_parses_getpacket_and_never_invents_an_address(net_env):
    """真租约只能来自 ``ipconfig getpacket``，读不到就是 ``None``（2026-09-29 的病根）。

    「网卡此刻的配置」与「DHCP 服务端发下来的那一份」是两件事：网卡已经被我们固化过
    之后，前者读回来正是**我们自己上次写下去的地址**。拿它当租约，「恢复 DHCP」
    就永远只抄出同一个地址，而表面上一切成功。

    读不到（网卡在 Manual / 命令失败 / 报文里没有 ``yiaddr``）一律 ``None`` —— 绝不编
    一个地址出来。这三种情况在网卡被固化期间是**常态**，不是错误。
    """
    _src, _table, net = net_env
    net.packet("en5", ip="10.99.1.21", mask="255.255.255.0", gateway="10.99.1.254",
               dns=("10.99.1.254", "8.8.8.8"), lease_time="0x001c20",
               server="10.99.1.254")

    lease = routeops.read_lease("en5")
    assert lease == {"ip": "10.99.1.21", "mask": "255.255.255.0",
                     "gateway": "10.99.1.254", "dns": ["10.99.1.254", "8.8.8.8"],
                     "lease_time": 7200, "server": "10.99.1.254"}

    # 我们固化着 ⇒ getpacket 必然失败 ⇒ 没有租约可读
    net.packets.clear()
    assert routeops.read_lease("en5") is None

    # 报文在、也不是失败，但里面没有可用地址 ⇒ 同样不编
    net.packet("en5", ip="0.0.0.0", mask="255.255.255.0")
    assert routeops.read_lease("en5") is None


def test_run_op_renew_pins_the_new_lease_not_the_one_we_wrote(net_env, monkeypatch):
    """``renew`` 一轮内原子走完三步，且固化的是**新租约**（§4.7 二）。

    「点了『恢复 DHCP』之后网卡会不会真的变好」这条链路的判据就在这一条里：
    先真的 ``-setdhcp`` → 等新租约落到网卡上 → 用**那份新租约**重算目标再 ``-setmanual``。

    第三步的数据来源是重点。写死成「读网卡此刻的配置」时读回来的正是 10.99.1.10
    （我们自己上次固化下去的那个）⇒ 网卡转一圈回原地，用户看到「点了没用」。
    """
    _src, _table, net = net_env
    service = net.define("en5", mode="static", ip="10.99.1.10",
                         mask="255.255.255.0", gateway="10.99.1.254")
    net.packet("en5", ip="10.99.1.21", mask="255.255.255.0", gateway="10.99.1.254")
    monkeypatch.setattr(routeops_routes, "current_ipv4", lambda iface: ["10.99.1.21"])
    net.cmds.clear()

    out = routeops._apply_op(
        {"iface": "en5", "op": "renew", "dns": False,
         "net": {"mode": "dhcp", "pin": {"ip": "", "mask": ""}}},
        _NOW)

    assert [c[1] for c in net.writes()] == ["-setdhcp", "-setmanual"], "先交还、再固化"
    assert net.writes()[1][3:5] == ["10.99.1.21", "255.255.255.0"], \
        "固化的是新租约，不是我们自己上次写下去的那个"
    assert net.configs[service]["mode"] == "static"          # 落盘形态：切 Manual 写死
    assert net.configs[service]["ip"] == "10.99.1.21"
    assert out["action"] == "applied" and out["managed"] is True
    assert out["mode"] == routeops.MODE_DHCP_PIN
    assert out["error"] is None
    assert out["result"] == "pinned" and out["ip"] == "10.99.1.21"


def test_run_op_renew_reports_when_no_new_lease_arrives(net_env, monkeypatch):
    """等不到新租约 ⇒ 报 ``failed``、**不固化**，并把「现在停在自动获取」说出来。

    宁可这一次没做成、把话讲清楚，也不要拿一份过期（或自己造的）地址去固化。
    """
    _src, _table, net = net_env
    net.define("en5", mode="static", ip="10.99.1.10", mask="255.255.255.0")
    monkeypatch.setattr(routeops_routes, "current_ipv4", lambda iface: [])
    real_wait = routeops._wait_lease
    # 真等 10 秒会把这个用例拖成慢例；把时限压到 0，走的仍是同一段代码
    monkeypatch.setattr(routeops_lease, "_wait_lease",
                        lambda iface: real_wait(iface, timeout=0.0))
    net.cmds.clear()

    out = routeops._apply_op(
        {"iface": "en5", "op": "renew", "dns": False,
         "net": {"mode": "dhcp", "pin": {"ip": "", "mask": ""}}},
        _NOW)

    assert [c[1] for c in net.writes()] == ["-setdhcp"], "没等到租约就绝不往下走"
    assert out["action"] == "failed"
    assert "没等到新租约" in out["error"] and "自动获取" in out["error"]
    assert out["result"] != "pinned" and out["managed"] is False


def test_run_op_release_stops_at_automatic_address(net_env):
    """``release`` 只交还，不固化 —— 没有场景要接管它时的形态（§4.7 二）。

    连 DNS 一起还原是预设里那个勾决定的（交还的是「进门前的那一份」，不是清空）。
    """
    _src, _table, net = net_env
    service = net.define("en5", mode="dhcp", dns=("1.1.1.1",))
    net.cmds.clear()

    out = routeops._apply_op({"iface": "en5", "op": "release", "dns": True}, _NOW)

    assert [c[1] for c in net.writes()] == ["-setdhcp", "-setdnsservers"]
    assert net.configs[service]["mode"] == "dhcp"
    assert net.configs[service]["dns"] == ["1.1.1.1"], "还原到 baseline 那一份"
    assert out["action"] == "handback" and out["mode"] == routeops.MODE_DHCP
    assert out["result"] == "dhcp"

    # 不勾「还原 DNS」⇒ 只动地址那半（DNS 留给系统）
    net.cmds.clear()
    routeops._apply_op({"iface": "en5", "op": "release", "dns": False}, _NOW)
    assert [c[1] for c in net.writes()] == ["-setdhcp"]


def test_run_op_reports_a_failed_handback_and_stops_there(net_env):
    """``-setdhcp`` 都没成 ⇒ 报 ``failed``，绝不接着去固化一张还在 Manual 上的网卡。

    「交还没成」与「固化没成」是两件事，前者的后果更重：网卡还停在我们写下的地址上，
    再写一遍只是白写 —— 但若照着租约写，那张网卡就真的收不到地址了。
    """
    _src, _table, net = net_env
    net.define("en5", mode="static", ip="10.99.1.10", mask="255.255.255.0")
    net.broken.add("-setdhcp")
    net.cmds.clear()

    out = routeops._apply_op(
        {"iface": "en5", "op": "renew", "dns": False,
         "net": {"mode": "dhcp", "pin": {"ip": "", "mask": ""}}},
        _NOW)

    assert [c[1] for c in net.writes()] == ["-setdhcp"], "交还失败就此打住"
    assert out["action"] == "failed" and "切回 DHCP 失败" in out["error"]
    assert out["managed"] is False


def test_commands_never_ride_the_scene_pass(net_env):
    """命令与场景是**两条路**（§4.7 三 ＋ 2026-09-29 定案），由结构保证、不再靠顺序。

    老两条性质（命令排在所有场景之前、停用挡不住它）从前是靠**顺序与例外**写的：命令
    和策略共用同一条 ``_apply_net``，于是要专门安排「命令先执行」，再专门给 ``paused``
    开一个口子 —— 而那个口子正是当年真机故障的根因（命令与「无匹配才交还」共用字段）。

    命令搬去会话帧 ``op.run``（``run_net_op`` → ``_apply_op``）之后，这两条都是**结构**
    性质：``_apply_net`` 根本不认识命令，``_apply_op`` 也根本不认识场景与暂停。这里钉
    的就是这个结构 —— 「排在前面的那条路」不复存在，也就无从被后面的策略挡住。
    """
    _src, _table, net = net_env
    cfg = {"mode": "static", "ip": "10.99.1.10", "mask": "255.255.255.0",
           "gateway": "10.99.1.254"}
    net.define("en5", mode="dhcp", ip="192.168.1.5", mask="255.255.255.0",
               gateway="192.168.1.1")
    net.cmds.clear()

    # ① 一段带着老式 ``ops`` 键的意图：``_apply_net`` 对它无感（不执行、也不报错）
    out = routeops._apply_net(
        {"paused": False, "handback": [], "rules": [],
         "ops": [{"iface": "en5", "dns": False, "op": "release"}]},
        {}, _NOW)
    assert net.writes() == [], "命令不再经过场景那一段（它走会话帧）"
    assert out["action"] == "none"

    # ② 场景照旧收敛：不必再给谁让位
    net.cmds.clear()
    routeops._apply_net(
        {"paused": False, "handback": [],
         "rules": [{"iface": "en5", "apply": True, "net": cfg}]},
        {}, _NOW)
    assert [c[1] for c in net.writes()] == ["-setmanual"], "场景该写就写"

    # ③ ``_apply_op`` 的入参里没有场景、也没有暂停 ⇒「停用挡不住命令」是结构性质，
    #    不是某处 ``if paused`` 的例外
    assert set(inspect.signature(routeops._apply_op).parameters) == {"op", "now"}


def test_command_channel_has_no_seq_and_no_receipt_book():
    """命令的「只执行一次」现在由**帧**保证，账本整件事被删掉了（不许长回来）。

    旧形态把命令写进期望态顶层的 ``ops``，再靠 ``seq`` 去状态文件的回执表里配对，
    配上了才把命令撤掉。两份账都活不过进程重启 —— 重启后认不出自己的命令，同一条
    ``renew`` 会**再执行一次**（又一次 ``setdhcp`` ＝ 又一次断网）。

    帧级回执（一帧一命令、回帧即回执）把「配对」这件事连带删掉：没有序号、没有回执
    表，也就没有账可错。这条用例守的就是「账不许回来」。
    """
    # ① 命令参数里没有 ``seq``；带上它整帧被拒（那是已经拆掉的通道）
    clean, err = routeops.validate_op_params(
        {"iface": "en5", "op": "release", "dns": False})
    assert err is None and clean == {"iface": "en5", "op": "release", "dns": False}
    _, err = routeops.validate_op_params(
        {"iface": "en5", "op": "release", "dns": False, "seq": 1})
    assert err is not None, "带序号的老命令该被拒"

    # ② 期望态顶层也没有 ``ops``（整份被拒，不是「多一个字段无妨」）
    _, err = routeops.validate_intent(
        {"version": routeops.INTENT_VERSION, "poll": 30, "paused": False,
         "handback": [], "rules": [], "ops": []})
    assert err is not None and "ops" in err

    # ③ 守护侧那本回执账也不在了
    assert not hasattr(routeops, "_carry_op_receipts")


def test_cycle_writes_net_segment_then_hands_it_back(net_env):
    """端到端：网卡动作**先于**路由，且结果进 ``state["net"]``（§2.5）。

    顺序也在安全侧：路由叠在网卡之上，先把网卡放对位置，明细路才有下一跳可走；
    网卡写失败时路由仍照意图收敛（幂等），不会两头都不做。
    """
    src, table, net = net_env
    service = net.define("en5", mode="dhcp", ip="192.168.1.5",
                         mask="255.255.255.0", gateway="192.168.1.1")
    table.packet_gateways = ["10.99.1.254"]
    cfg = {"mode": "static", "ip": "10.99.1.10", "mask": "255.255.255.0",
           "gateway": "10.99.1.254"}

    routeops.run_cycle(_intent_text([_apply({**OFFICE, "net": cfg}, True)]))
    state = _observed_route()
    seg = state["net"]
    assert seg["iface"] == "en5" and seg["service"] == service
    assert seg["action"] == "applied" and seg["managed"] is True
    assert seg["mode"] == routeops.MODE_STATIC
    assert seg["net"] == cfg
    assert state["rules"][0]["applied"] is True
    assert net.configs[service]["mode"] == "static"

    # 下一轮：场景不再要接管（apply=False）+ 交还清单点了它 ⇒ 回 baseline
    routeops.run_cycle(_intent_text(
        [_apply({**OFFICE, "net": cfg}, False, trigger=False)], handback=["en5"]))
    state = _observed_route()
    assert state["net"]["action"] == "handback"
    assert net.configs[service]["mode"] == "dhcp"


def test_net_write_clears_the_router_when_pin_omits_it(net_env):
    """``pin`` 只固化地址 ⇒ 网卡上**不许留 Router**。

    多出来的 router 会被系统当成默认路由，影响其他路由与 Apple 连续互通 ——「不固化」
    就是清空（``-setmanual`` 省略该参数），不是「保留现有那个」。要用的网关由
    ``net_gateways`` 那本账记着（它读的是**写之前**的网卡/租约），故抹掉不丢下一跳。
    2026-10-19 用户裁定；此前那版「保留」是错的。
    """
    _src, _table, net = net_env
    service = net.define("en5", mode="static", ip="10.99.1.99",
                         mask="255.255.255.0", gateway="10.99.1.1")
    rule = {**OFFICE, "gateway": "",
            "net": {"mode": "dhcp",
                    "pin": {"ip": "10.99.1.56", "mask": "255.255.255.0"}}}

    routeops.run_cycle(_intent_text([_apply(rule, True)]))
    writes = [c for c in net.cmds if len(c) > 1 and c[1] == "-setmanual"]
    assert writes, "该写的那一轮要写"
    assert len(writes[-1]) == 5, "pin 没声明网关 ⇒ 命令里就没有 router（＝清空）"
    assert net.configs[service]["gateway"] == "", "网卡上不能留下旧 router"
    # 抹掉归抹掉，账不能丢：下一跳仍从这里取（同轮就要用它叠路由）
    assert _observed_route()["net_gateways"]["en5"]["gateway"] == "10.99.1.1"


def test_net_manual_without_router_is_not_rewritten_every_cycle(net_env):
    """清空后 ``-getinfo`` 读回 ``(null)`` ⇒ 仍算「已经是目标」，不许每轮重写一遍。

    ``_same`` 现在把「我们没声明网关」当成「网卡上就该没有」，所以必须把 ``(null)``
    这类非 IP 读数归成空；否则每一轮都判漂移，30 秒重配一次网卡。
    """
    _src, _table, net = net_env
    service = net.define("en5", mode="static", ip="10.99.1.56",
                         mask="255.255.255.0", gateway="(null)")
    rule = {**OFFICE, "gateway": "",
            "net": {"mode": "dhcp",
                    "pin": {"ip": "10.99.1.56", "mask": "255.255.255.0"}}}

    routeops.run_cycle(_intent_text([_apply(rule, True)]))
    assert [c for c in net.writes() if c[1] == "-setmanual"] == [], "已到位就不写"
    assert net.configs[service]["gateway"] == "(null)", "读数原样，但我们不碰它"


# ──────────────────────────────────────────────
# 固化态跨重启（2026-09-30 真机坏法：重启后「网卡配置失败」永久卡死）
#
# 根因链：dhcp+pin 留空地址 ⇒ 每轮收敛要读租约；网卡被我们固化着 = Manual =
# ``ipconfig getpacket`` 必然失败 ⇒ 稳态早退（上一轮的账）是唯一正路。可上一轮的账
# 一旦缺了或坏了一次（守护首次上线 / 快照缺位 / 启动竞态读失败一轮），失败段里
# ``wrote`` 是空 ⇒ 早退从此永远 miss ⇒ 每轮重走「读租约 → 读不到 → 报错」——
# 一次瞬时失败就把状态机锁死在 failed，重启（快照灌回失败段）也出不来。
# 修复：网卡已是 Manual 且无租约可读时，拿它此刻的配置当租约替身补 pin 的空位。
# ──────────────────────────────────────────────

def _pin_rule():
    """用户现场那份场景：dhcp + 空 pin（「获取到 IP 后固定住」）。"""
    return {**OFFICE, "net": {"mode": "dhcp", "pin": {"ip": "", "mask": ""}}}


def _restart_helper():
    """模拟守护重启：清空内存观测（此后 ``read_state`` 走「现读快照」那条兜底）。"""
    helper_state._OBSERVED.clear()


def test_pin_then_restart_recovers_from_snapshot(net_env):
    """固化 → 守护重启（快照灌回）⇒ 首轮稳态早退，不读租约、不写网卡。"""
    _src, _table, net = net_env
    service = net.define("en5", mode="dhcp")
    net.packet("en5", ip="10.99.1.21", mask="255.255.255.0")

    routeops.run_cycle(_intent_text([_apply(_pin_rule(), True)]))
    assert _observed_route()["net"]["action"] == "applied"
    assert net.configs[service]["mode"] == "static"      # 已固化

    _restart_helper()
    net.cmds.clear()
    routeops.run_cycle(_intent_text([_apply(_pin_rule(), True)]))
    seg = _observed_route()["net"]
    assert seg["action"] == "applied" and seg["managed"] is True
    assert seg["wrote"]["ip"] == "10.99.1.21"
    assert net.writes() == [], "重启后网卡已停在目标那一份上 ⇒ 不许再写"


def test_pin_survives_a_restart_even_when_the_books_are_gone(net_env):
    """上一轮的账**整个没有**（快照缺位 / 守护重装后首轮）也不能报失败。

    网卡是 Manual ⇒ 没有租约可读（假桩已照真机建：非 DHCP 模式 getpacket 必失败）
    ⇒ 修复前这里报「固化要靠租约补地址」并从此每轮如此 —— 正是真机那段 error。
    """
    _src, _table, net = net_env
    service = net.define("en5", mode="dhcp")
    net.packet("en5", ip="10.99.1.21", mask="255.255.255.0")
    routeops.run_cycle(_intent_text([_apply(_pin_rule(), True)]))
    assert _observed_route()["net"]["action"] == "applied"

    # 重启且快照丢了：内存与盘上都没有上一轮的账
    helper_state._OBSERVED.clear()
    (net_env[0] / "helper_snapshot.json").unlink(missing_ok=True)

    net.cmds.clear()
    routeops.run_cycle(_intent_text([_apply(_pin_rule(), True)]))
    seg = _observed_route()["net"]
    assert seg["action"] == "applied" and seg["error"] is None
    assert seg["managed"] is True and seg["wrote"]["ip"] == "10.99.1.21"
    assert net.writes() == [], "网卡已经停在该停的那份上 ⇒ 一条写命令都不该有"
    assert net.configs[service]["ip"] == "10.99.1.21"


def test_a_latched_failure_heals_on_the_next_round(net_env):
    """已锁死在 failed 的现场（真机快照那份）下一轮自愈，面板回到「已固化」。

    失败段的特点是 ``wrote``/``net`` 都是空 ⇒ 稳态早退永远 miss。修复前每轮都会
    重新撞上「没有租约」；修复后替身接住。网卡上那个 router 是我们的配置里没有的，
    自愈这一轮顺手抹掉（一次写），之后不再写。
    """
    _src, _table, net = net_env
    service = net.define("en5", mode="static", ip="10.99.1.21",
                         mask="255.255.255.0", gateway="10.99.1.254")
    helper_state.record(helper_state.SUBJECT_ROUTE, {
        "ok": True, "checked_at": 1, "poll": 30, "error": None, "paused": False,
        "has_intent": True, "rules": [],
        "net": {"iface": "en5", "service": service, "mode": "", "managed": False,
                "wrote": None, "net": None, "action": "failed",
                "error": f"{service}：固化要靠租约补地址，但租约里没拿到可用的"
                         f" ip / mask：'' / ''", "applied_at": 1},
    })

    routeops.run_cycle(_intent_text([_apply(_pin_rule(), True)]))
    seg = _observed_route()["net"]
    assert seg["action"] == "applied" and seg["error"] is None
    assert seg["managed"] is True and seg["wrote"]["ip"] == "10.99.1.21"
    assert [c for c in net.writes() if c[1] == "-setmanual"] == [
        ["/usr/sbin/networksetup", "-setmanual", service,
         "10.99.1.21", "255.255.255.0"]], "只写一次，且把不该留的 router 抹掉"
    assert net.configs[service]["gateway"] == ""

    # 下一轮：账已经在，steady ⇒ 一条写命令都不该有
    routeops.run_cycle(_intent_text([_apply(_pin_rule(), True)]))
    assert len(net.writes()) == 1, "自愈那一轮写过一次就够了，之后是稳态"


def test_transient_read_failure_does_not_latch_the_state_machine(net_env):
    """启动竞态：首轮读不出网卡配置报失败，恢复后下一轮就该回来（不许锁死）。"""
    _src, _table, net = net_env
    net.define("en5", mode="dhcp")
    net.packet("en5", ip="10.99.1.21", mask="255.255.255.0")
    routeops.run_cycle(_intent_text([_apply(_pin_rule(), True)]))
    assert _observed_route()["net"]["action"] == "applied"

    _restart_helper()
    net.cmds.clear()
    real = routeops.read_service_config
    calls = {"n": 0}

    def flaky(svc):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(svc)

    routeops_netsvc.read_service_config = flaky
    try:
        routeops.run_cycle(_intent_text([_apply(_pin_rule(), True)]))
        assert _observed_route()["net"]["action"] == "failed"   # 照实报
        routeops.run_cycle(_intent_text([_apply(_pin_rule(), True)]))
        seg = _observed_route()["net"]
        assert seg["action"] == "applied" and seg["managed"] is True
        assert net.writes() == [], "值就在网卡上，恢复靠认账，不靠重写"
    finally:
        routeops_netsvc.read_service_config = real


def test_missing_lease_on_a_dhcp_nic_is_still_an_honest_failure(net_env):
    """替身只救「已是 Manual」的网卡：DHCP 网卡暂时没租约 = 还没拿到，照实报错。

    这档必须继续报错：它下一轮租约到了自然收敛（自愈），而替身若把它也吞掉，
    「DHCP 服务端坏了」就会被永远说成「已固化」。
    """
    _src, _table, net = net_env
    net.define("en5", mode="dhcp")                       # 有服务、没租约
    routeops.run_cycle(_intent_text([_apply(_pin_rule(), True)]))
    seg = _observed_route()["net"]
    assert seg["action"] == "failed" and "租约" in seg["error"]
    assert net.writes() == []

    # 租约到了 ⇒ 下一轮自愈成固化
    net.packet("en5", ip="10.99.1.21", mask="255.255.255.0")
    routeops.run_cycle(_intent_text([_apply(_pin_rule(), True)]))
    seg = _observed_route()["net"]
    assert seg["action"] == "applied" and seg["wrote"]["ip"] == "10.99.1.21"


class _FakeClock:
    """可拨动的时间源（只替 ``routeops.time``，别的不碰）。"""

    def __init__(self, t: float):
        self.t = t

    def time(self) -> float:
        return self.t


def test_applied_at_is_stamped_by_the_round_that_really_wrote(net_env, monkeypatch):
    """「写下去的时刻」只记在**真写那一轮**；稳态轮照抄上一轮。

    每轮都盖 now 的话，下一轮看到的「距写入多久」永远是约一轮的长度：宽限期算
    「刚过」、窗口上沿算「已错过」（30s 节拍 vs 20s 窗口），回滚保险窗一次都
    不会触发 —— 它就一直是死的。
    """
    _src, _table, net = net_env
    net.define("en5", mode="dhcp")
    net.packet("en5", ip="10.99.1.21", mask="255.255.255.0")
    helper_state._OBSERVED.clear()          # 内存账是进程级的，先清再演
    clock = _FakeClock(1000)
    monkeypatch.setattr(routeops_cycle, "time", clock)
    monkeypatch.setattr(routeops_converge, "time", clock)
    monkeypatch.setattr(routeops_baseline, "time", clock)
    # 假网卡没有 ``inet`` 行 ⇒ 真读会返回空 ⇒ 窗口一通就判「失联」。这里演的是
    # 「写下去的地址还在」的正常稳态，探针必须照实给地址。
    monkeypatch.setattr(routeops_routes, "current_ipv4", lambda iface: ["10.99.1.21"])

    intent = _intent_text([_apply(_pin_rule(), True)])
    routeops.run_cycle(intent)                          # 真写这一轮
    assert _observed_route()["net"]["applied_at"] == 1000
    net.cmds.clear()                                    # 第一轮的合法写入出账

    clock.t = 1030                                      # 下一轮节拍（30s 后）
    routeops.run_cycle(intent)                          # 稳态轮：什么都不写
    assert _observed_route()["net"]["action"] == "applied"
    assert _observed_route()["net"]["applied_at"] == 1000, \
        "稳态轮不许盖时刻：盖了它窗口永远算不出「已过宽限期」"
    assert net.writes() == []


def test_rollback_window_must_cover_the_poll_cadence(net_env, monkeypatch):
    """窗口上沿必须盖住节拍：30s 的节拍、20s 的窗口 ⇒ 下一轮探活永远在窗外。

    ``cadence`` 缺省（旧签名）保持 20s 上沿不变；带上节拍后，下一轮（节拍 ± 抖动）
    恰好落进窗口，再往后（节拍＋余量之外）照旧算出窗。
    """
    _src, _table, net = net_env
    service = net.define("en5", mode="dhcp")
    routeops.ensure_baseline(service, "en5", routeops.read_service_config(service))
    prev = {"action": "applied", "service": service, "iface": "en5",
            "wrote": {"ip": "10.0.0.5", "mask": "255.255.255.0", "gateway": ""},
            "net": {"mode": "static"}, "applied_at": _NOW}
    monkeypatch.setattr(routeops_routes, "current_ipv4", lambda iface: [])

    # 不带节拍（＝旧调用点）：31s 时已在窗外（与修复前一致）
    assert routeops._rollback_if_lost(prev, _NOW + 31) is None
    # 带上 30s 节拍：下一轮（+31s）落在窗口内 ⇒ 回滚判据可达
    out = routeops._rollback_if_lost(prev, _NOW + 31, cadence=30)
    assert out is not None and out["action"] == "rolled_back"
    # 节拍＋余量之外照旧出窗
    assert routeops._rollback_if_lost(prev, _NOW + 30 + 10 + 1, cadence=30) is None


def test_anti_flap_gate_also_covers_the_poll_cadence():
    """防抖闸同宽：30s 节拍下，20s 的闸在下一轮就过期，拦了个寂寞。"""
    cfg = {"mode": "static", "ip": "10.0.0.5", "mask": "255.255.255.0"}
    prev = {"action": "rolled_back", "iface": "en5", "net": cfg, "applied_at": _NOW}
    assert routeops._recently_rolled_back(prev, "en5", cfg, _NOW + 31, cadence=30) is True
    assert routeops._recently_rolled_back(prev, "en5", cfg, _NOW + 31) is False
    assert routeops._recently_rolled_back(prev, "en5", cfg, _NOW + 30 + 10 + 1,
                                          cadence=30) is False


def test_rollback_fires_on_the_next_beat(net_env, monkeypatch):
    """端到端：真写一轮（把网卡写失联）⇒ 下一轮节拍（30s 后）自动退回 baseline。

    修复前这条链一次都走不通：``applied_at`` 被稳态轮盖掉 ＋ 窗口窄过节拍。
    回滚后下一轮还被防抖闸拦住（同一份意图不许立刻写回去）。
    """
    _src, _table, net = net_env
    service = net.define("en5", mode="dhcp")
    net.packet("en5", ip="10.99.1.21", mask="255.255.255.0")
    helper_state._OBSERVED.clear()          # 内存账是进程级的，先清再演
    clock = _FakeClock(1000)
    monkeypatch.setattr(routeops_cycle, "time", clock)
    monkeypatch.setattr(routeops_converge, "time", clock)
    monkeypatch.setattr(routeops_baseline, "time", clock)

    intent = _intent_text([_apply(_pin_rule(), True)])
    routeops.run_cycle(intent)                          # 真写（假设写坏了）
    assert _observed_route()["net"]["action"] == "applied"
    assert net.configs[service]["mode"] == "static"

    # 写下去的地址不在网卡上了（把自己关在门外）
    monkeypatch.setattr(routeops_routes, "current_ipv4", lambda iface: [])
    clock.t = 1030                                      # 下一轮节拍
    routeops.run_cycle(intent)
    seg = _observed_route()["net"]
    assert seg["action"] == "rolled_back" and seg["rolled_back"] is True
    assert net.configs[service]["mode"] == "dhcp", "退回 baseline，用户网络回到进门前"

    # 再下一轮：同一份意图被防抖闸拦住，不许立刻写回去
    net.cmds.clear()
    clock.t = 1060
    routeops.run_cycle(intent)
    assert net.writes() == [], "刚退过一次 ⇒ 同一份意图不许立刻再写"
    assert _observed_route()["net"]["action"] == "rolled_back"


# ──────────────────────────────────────────────
# helper：信号层
# ──────────────────────────────────────────────

def test_iface_up(helper_env):
    _ = helper_env
    assert routeops.iface_up("en5") is True


def test_current_gateways_packet_first_then_default(helper_env):
    src, table = helper_env
    table.packet_gateways = ["10.99.1.254", "10.99.1.253"]
    gws = routeops.current_gateways("en5")
    assert gws == ["10.99.1.254", "10.99.1.253"]
    # 无 DHCP 报文（静态 IP）→ 降级 route -n get default
    table.packet_gateways = []
    table.default_gateway = "10.9.9.9"
    assert routeops.current_gateways("en5") == ["10.9.9.9"]


def test_helper_has_no_matching_policy(helper_env):
    """档 B 门禁：helper 侧不得再持有「是否该生效」的决策函数。"""
    _ = helper_env
    assert not hasattr(routeops, "rule_matched")
    # 查询机制仍在（app 侧复用），但不含决策
    assert callable(routeops.iface_up) and callable(routeops.current_gateways)


def test_validate_intent_requires_apply(helper_env):
    """意图必须由 app 侧算好 apply；缺失/非布尔 → 整份拒绝。"""
    _ = helper_env
    data, err = routeops.validate_intent(
        {"version": routeops.INTENT_VERSION, "poll": 30, "rules": [_apply(OFFICE, True)]}
    )
    # 版本号**取常量不写死**：写死的数在两轮契约 bump 里都留在了旧值上，而当时 helper
    # 只在白名单里收下它、从不比 —— 直到真锁落地（2026-09-19）才集体转红。
    # 产物里的版本由 ``validate_routes`` 给出（原先 ``validate_intent`` 末尾再「盖自己的
    # 号」，真锁落地后那是空动作，已删）。
    assert err is None and data["version"] == routeops.INTENT_VERSION
    assert data["rules"][0]["apply"] is True

    _, err = routeops.validate_intent(
        {"version": routeops.INTENT_VERSION, "poll": 30, "rules": [OFFICE]})
    assert err and "apply" in err

    _, err = routeops.validate_intent(
        {"version": routeops.INTENT_VERSION, "poll": 30, "rules": [{**OFFICE, "apply": "yes"}]}
    )
    assert err and "apply" in err


# ──────────────────────────────────────────────
# daemon：plist/版本/脚本
# ──────────────────────────────────────────────

def test_expected_version_matches_helper():
    assert daemon.expected_version() == helper.HELPER_VERSION


def _write_fake_helper_dir(root, *, version):
    """写一个假 helper 包目录（新形态：整目录 + version.py 锚点）。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "version.py").write_text(f'HELPER_VERSION = "{version}"\n', encoding="utf-8")
    (root / "core.py").write_text("from .version import HELPER_VERSION\n", encoding="utf-8")
    return root


def test_install_format_outdated_missing_anchor(tmp_path, monkeypatch):
    """已装但缺版本锚点（老单文件形态 / 安装不完整）→ 必须判过期逼出重装。

    否则 app 交出的期望态会被旧 helper 整份拒绝 → 面板「路由异常」常驻。
    """
    daemon._invalidate_status_cache()
    monkeypatch.setattr(daemon, "PLIST_PATH", tmp_path / "nope.plist")
    monkeypatch.setattr(daemon, "HELPER_VERSION_PATH", tmp_path / "helper" / "version.py")
    assert daemon.install_format_outdated() is True


def test_install_format_outdated_old_plist(tmp_path, monkeypatch):
    """版本一致但 plist 仍是老形态（未指向 barkit-helper）→ 判过期。"""
    daemon._invalidate_status_cache()
    anchor = _write_fake_helper_dir(tmp_path / "helper", version=helper.HELPER_VERSION) / "version.py"
    monkeypatch.setattr(daemon, "HELPER_VERSION_PATH", anchor)
    old_plist = tmp_path / "com.barkit.routed.plist"
    old_plist.write_text("<string>/usr/bin/python3</string>", encoding="utf-8")
    monkeypatch.setattr(daemon, "PLIST_PATH", old_plist)
    assert daemon.install_format_outdated() is True
    daemon._invalidate_status_cache()
    assert daemon.status()["outdated"] is True


def test_install_format_outdated_current(tmp_path, monkeypatch):
    """版本一致 + 新形态 plist（指向 barkit-helper）→ 不过期。"""
    daemon._invalidate_status_cache()
    anchor = _write_fake_helper_dir(tmp_path / "helper", version=helper.HELPER_VERSION) / "version.py"
    monkeypatch.setattr(daemon, "HELPER_VERSION_PATH", anchor)
    plist = tmp_path / "com.barkit.helper.plist"
    plist.write_text(f"<string>{daemon.LAUNCHER_INSTALL_PATH}</string>", encoding="utf-8")
    monkeypatch.setattr(daemon, "PLIST_PATH", plist)
    assert daemon.install_format_outdated() is False


def test_outdated_also_when_the_running_daemon_reports_another_version(tmp_path, monkeypatch):
    """盘上那份一致、但**跑着的**守护自陈另一版 ⇒ 照样判过期（装了没重启那一档）。

    两处任一处不符即过期，不是「取其一」：只认盘上那份会漏掉「进程还是旧的」，
    只认自陈那份会漏掉「重启后又会回到旧代码」。
    """
    daemon._invalidate_status_cache()
    anchor = _write_fake_helper_dir(tmp_path / "helper", version=helper.HELPER_VERSION) / "version.py"
    monkeypatch.setattr(daemon, "HELPER_VERSION_PATH", anchor)
    plist = tmp_path / "com.barkit.helper.plist"
    plist.write_text(f"<string>{daemon.LAUNCHER_INSTALL_PATH}</string>", encoding="utf-8")
    monkeypatch.setattr(daemon, "PLIST_PATH", plist)
    monkeypatch.setattr(daemon, "live_version", lambda: "0.0.1-residue")
    daemon._invalidate_status_cache()
    st = daemon.status()
    assert st["live_version"] == "0.0.1-residue"
    assert st["outdated"] is True


def test_launchctl_failure_does_not_extend_the_ttl(monkeypatch):
    """launchctl 查询失败不该按 TTL 续命 —— 那是把「没测出来」当成事实。

    续命的现场是守护已经不在了、面板却永远写着「运行中」：每次查询都失败、
    每次都把旧的 True 再续 30 秒。判据写在「下一次调用会不会真的再问一遍」
    上（ run_cmd 的调用次数），而不是内部时间戳。
    """
    daemon._invalidate_status_cache()
    calls: list[int] = []

    def _boom(*_a, **_kw):
        calls.append(1)
        raise TimeoutError("launchctl 挂住了")

    monkeypatch.setattr(daemon, "run_cmd", _boom)
    assert daemon.helper_running() == daemon._launchctl_cache["running"]
    daemon.helper_running()
    assert len(calls) == 2, "失败后必须立刻重试，而不是等 TTL 过期"


def test_launchctl_failure_is_logged_once(monkeypatch, caplog):
    """同一档故障只 WARN 一次（面板每 5s 一问，成百行同一句等于没日志）。"""
    daemon._invalidate_status_cache()
    monkeypatch.setattr(daemon, "run_cmd", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("nope")))
    with caplog.at_level(logging.WARNING, logger=daemon.__name__):
        daemon.helper_running()
        daemon.helper_running()
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1


def test_unknown_expected_version_is_not_a_version(tmp_path, monkeypatch):
    """期望版本读不出来（"?"）时不得参与比较 —— 否则任何真实版本都判过期。

    判据写在用户看得见那一格上：``outdated`` 必须回到 False，而不是去查内部
    到底比没比。
    """
    daemon._invalidate_status_cache()
    anchor = _write_fake_helper_dir(tmp_path / "helper", version="2.19.0") / "version.py"
    monkeypatch.setattr(daemon, "HELPER_VERSION_PATH", anchor)
    monkeypatch.setattr(daemon, "PLIST_PATH", anchor)   # 两个都指向一个现存文件
    monkeypatch.setattr(daemon, "expected_version", lambda: daemon.VERSION_UNKNOWN)
    monkeypatch.setattr(daemon, "installed_version", lambda: "2.19.0")
    monkeypatch.setattr(daemon, "live_version", lambda: "2.19.0")
    monkeypatch.setattr(daemon, "install_format_outdated", lambda: False)
    monkeypatch.setattr(daemon, "helper_running", lambda: True)
    st = daemon.status()
    assert st["installed"] is True
    assert st["outdated"] is False


def test_invalidate_status_cache_also_drops_launchctl_answer(monkeypatch):
    """安装 / 卸载后连 running 一起失效：只清一半 ⇒ 那 30 秒里面板念反话。"""
    monkeypatch.setattr(daemon, "run_cmd", lambda *_a, **_kw: SimpleNamespace(returncode=0))
    assert daemon.helper_running() is True
    daemon._invalidate_status_cache()
    monkeypatch.setattr(daemon, "run_cmd", lambda *_a, **_kw: SimpleNamespace(returncode=1))
    assert daemon.helper_running() is False, "卸载后必须立刻问到新结论"


def test_launchctl_cache_is_empty_right_after_boot(monkeypatch):
    """开机头 30 秒也必须真问一次：拿 ``0.0`` 当「没缓存」的哨兵会把它当成新鲜。

    ``time.monotonic()`` 是**开机以来的秒数**（不是进程启动以来的秒数）⇒ 用它去减
    ``0.0`` 时，开机头 ``LAUNCHCTL_TTL`` 秒里 ``now - 0.0 < TTL`` 恒真 ⇒ 缓存被当成
    新鲜、直接返回默认值 ``False``，一次 launchctl 都不问。用户看到的是「刚开机那
    一段面板写着守护未运行」，而真值从来没被读过。

    判据写在**真问了几次**上，不写在内里的时间戳上：先把 ``running`` 摆成 False，
    于是「沿用默认值」与「真问出来就是 False」两种情形在返回值上同形，只有调用
    次数能把它们分开。
    """
    calls: list[int] = []
    monkeypatch.setattr(
        daemon, "run_cmd",
        lambda *_a, **_kw: (calls.append(1), SimpleNamespace(returncode=0))[1],
    )
    daemon._launchctl_cache["running"] = False
    daemon._invalidate_status_cache()
    monkeypatch.setattr(daemon.time, "monotonic", lambda: 5.0)   # 开机 5 秒
    assert daemon.helper_running() is True, "读到的仍是「从没测过」，没去问"
    assert len(calls) == 1


def test_launchctl_answer_is_cached_within_the_ttl(monkeypatch):
    """反面：真问过一次之后，TTL 内不许再问（面板每 5s 问一次，别每问都起子进程）。

    与上一条配对存在：只守「必须问」会把 TTL 整个删掉也放过。
    """
    calls: list[int] = []
    monkeypatch.setattr(
        daemon, "run_cmd",
        lambda *_a, **_kw: (calls.append(1), SimpleNamespace(returncode=0))[1],
    )
    clock = {"now": 1000.0}
    monkeypatch.setattr(daemon.time, "monotonic", lambda: clock["now"])
    daemon._invalidate_status_cache()

    assert daemon.helper_running() is True
    clock["now"] += daemon.LAUNCHCTL_TTL - 1
    daemon.helper_running()
    assert len(calls) == 1, "TTL 内重复查询 ⇒ 面板每 5s 起一次子进程"
    clock["now"] += 2
    daemon.helper_running()
    assert len(calls) == 2, "过了 TTL 必须重新问"


def test_status_not_installed(tmp_path, monkeypatch):
    daemon._invalidate_status_cache()  # 清 30s TTL 缓存，避免读到真实系统状态
    monkeypatch.setattr(daemon, "PLIST_PATH", tmp_path / "x.plist")
    monkeypatch.setattr(daemon, "HELPER_VERSION_PATH", tmp_path / "helper" / "version.py")
    st = daemon.status()
    assert st["installed"] is False and st["outdated"] is False


def test_plist_template_has_critical_fields():
    # ``uid`` 是必填占位符（2026-09-29 起）：守护开机先起时推不出被服务的是谁。
    # 它该不该在、值对不对，由 ``test_helper_provisioning`` 那条端到端用例守
    # （走真实 ``install()``），这里只补上参数，继续盯原有那几个字段。
    plist = daemon._PLIST_TEMPLATE.format(
        label="com.barkit.helper", launcher="/x/barkit-helper",
        data_dir="/Users/x/Library/Application Support/BarKit",
        uid=501,
        pythonpath="/Library/Application Support/BarKit",
    )
    assert "<true/>" in plist                                    # RunAtLoad
    assert re.search(r"<key>SuccessfulExit</key>\s*<false/>", plist)
    assert "BARKIT_DATA_DIR" in plist and "/Users/x/Library/Application Support/BarKit" in plist
    assert "<key>ThrottleInterval</key>" in plist
    # 整目录包形态：-m helper 需要 PYTHONPATH 指向安装根目录
    assert "<key>PYTHONPATH</key>" in plist
    # 启动器形态：ProgramArguments 指向 barkit-helper（显示名可辨识），不再直跑 python3
    assert "<string>/x/barkit-helper</string>" in plist
    assert "/usr/bin/python3" not in plist


def test_launcher_content_execs_helper_module():
    launcher = daemon.launcher_content()
    assert launcher.startswith("#!/bin/bash")
    assert "exec /usr/bin/python3 -m helper" in launcher
    assert "PYTHONPATH" in launcher


def test_install_script_content(tmp_path, monkeypatch):
    captured = []
    monkeypatch.setattr(paths, "APP_SUPPORT", tmp_path)
    monkeypatch.setattr(daemon, "run_admin", lambda p: (captured.append((tmp_path / p).read_text()) or True))
    assert daemon.install() is True
    content = captured[0]
    assert "launchctl bootstrap system" in content
    assert "com.barkit.helper.plist" in content
    assert "KeepAlive" in content and "SuccessfulExit" in content
    # 旧 label / 旧形态清理是硬前置（否则旧 daemon 带旧代码继续跑）
    assert "com.barkit.routed" in content
    assert "bootout" in content and "rm -f" in content
    # 整目录部署 + 新启动器
    assert f'cat > "{daemon.LAUNCHER_INSTALL_PATH}"' in content
    assert "exec /usr/bin/python3 -m helper" in content
    assert "install -m 0644" in content
    assert "chmod 0755" in content
    # 脚本已清理
    assert not (tmp_path / ".barkit-install-helper.sh").exists()


def test_admin_script_is_not_written_to_a_predictable_path(tmp_path, monkeypatch):
    """交给 root 的那份脚本：名字不可预测、权限出生即受限、不覆盖既有同名文件。

    固定名等于公开宣布「root 一会儿要执行这个路径上的内容」：谁能往那个目录写，
    谁就能让 root 执行他写的字节（``write_text`` 之后才 ``chmod`` 的那版还多一段
    默认 umask 权限的窗口）。
    """
    seen: dict = {}
    monkeypatch.setattr(paths, "APP_SUPPORT", tmp_path)
    planted = tmp_path / ".barkit-install-helper.sh"
    planted.write_text("#!/bin/bash\necho pwned\n", encoding="utf-8")

    def _capture(p, **_kw):
        f = Path(p)
        seen.update(path=p, body=f.read_text(encoding="utf-8"),
                    mode=oct(f.stat().st_mode & 0o777))
        return True

    monkeypatch.setattr(daemon, "run_admin", _capture)
    assert daemon.install() is True

    assert Path(seen["path"]).name != ".barkit-install-helper.sh"
    assert "pwned" not in seen["body"], "既有同名文件被当脚本孙走了（符号链接同理）"
    assert seen["mode"] == oct(0o700)
    assert planted.read_text(encoding="utf-8") == "#!/bin/bash\necho pwned\n"


def test_install_deploys_helper_subpackages(tmp_path, monkeypatch):
    """helper 包的子目录必须整包进安装清单（控制组：临时造一棵带子包的源码树）。

    为什么单独立一条：上面那条只断言脚本里有 ``install -m 0644`` 这串字面量 ——
    那是**代理判据**。安装循环若退回 ``for _f in "$src"/*.py``（只匹配顶层），
    它照样绿。而 helper 自 2026-10-02 起有了子包（``routeops/``、``tunnelops/``），
    盘上那份是拆包前的单文件形态 ⇒ 这条错从落地起一次都没被跑到过；真装一次就是
    「守护启动即 ImportError（``lifecycle`` 里 ``from .tunnelops import …``）
    ＋ launchd KeepAlive 无限重启」，而聚合哈希取自**装完的目录**、缺文件也自洽。
    """
    fake = tmp_path / "src" / "helper"
    (fake / "sub").mkdir(parents=True)
    (fake / "__pycache__").mkdir()
    (fake / "__init__.py").write_text("")
    (fake / "sub" / "__init__.py").write_text("")
    (fake / "sub" / "mod.py").write_text("")
    (fake / "__pycache__" / "junk.py").write_text("")
    (fake / "notes.txt").write_text("")
    monkeypatch.setattr(daemon, "HELPER_SRC_DIR", fake)

    # 清单跟着源码树走：子包在、__pycache__ 与 .py 之外的不在
    assert daemon.helper_deploy_files() == ["__init__.py", "sub/__init__.py", "sub/mod.py"]

    captured = []
    monkeypatch.setattr(paths, "APP_SUPPORT", tmp_path / "appsupport")
    monkeypatch.setattr(daemon, "run_admin",
                        lambda p: (captured.append((tmp_path / p).read_text()) or True))
    assert daemon.install() is True
    content = captured[0]
    dest = str(daemon.HELPER_INSTALL_DIR)
    assert f'mkdir -p "{dest}/sub"' in content
    assert f'"{fake.resolve()}/sub/mod.py" "{dest}/sub/mod.py"' in content


def test_install_deploy_list_covers_real_helper_package():
    """生产树的清单必须真的含子包 —— 只有控制组那条时，清单被写死也能骗过它。"""
    rels = daemon.helper_deploy_files()
    assert any(rel.startswith("routeops/") for rel in rels)
    assert any(rel.startswith("tunnelops/") for rel in rels)
    assert all("__pycache__" not in rel for rel in rels)


def test_uninstall_script_content(tmp_path, monkeypatch):
    captured = []
    monkeypatch.setattr(paths, "APP_SUPPORT", tmp_path)
    monkeypatch.setattr(daemon, "run_admin", lambda p: (captured.append((tmp_path / p).read_text()) or True))
    assert daemon.uninstall() is True
    content = captured[0]
    assert "launchctl bootout" in content and "system/com.barkit.helper" in content
    assert "com.barkit.routed" in content                 # 旧 label 一并清
    # 切分点 2（2026-09-17）：框架**不再**替插件撤系统状态。那段「读 routes.json →
    # /sbin/route delete」曾内嵌在这里，它读的正是插件卸载时已删掉的那份文件 ⇒ 在
    # 最常见的卸载顺序下静默无效。撤净归插件 → helper 那条通道（今天是会话上的一帧）。
    assert "routes.json" not in content
    assert "/sbin/route" not in content
    assert "routed-helper" in content                     # 旧形态残留仍要清
    assert "rm -f" in content and "rm -rf" in content
    # 卸载同步删 plist / 启动器 / helper 目录
    assert str(daemon.LAUNCHER_INSTALL_PATH) in content
    assert str(daemon.HELPER_INSTALL_DIR) in content


def test_uninstall_no_longer_declares_data_files(tmp_path, monkeypatch):
    """卸载**不再需要收尾声明**：一条通道文件都没有了（2026-09-29）。

    从前这里声明「等 helper 收敛到全撤了再删 routes.json」，判据还是模块级函数（收尾
    跑在这个实例被遗忘之后）。会话通道落地后没有文件要删，`deferred` 随对象一起消失
    —— 留一个「等几秒」的收尾只会让人以为还有东西在等。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    res = plugin.resources()

    assert res.data_files == [], "期望态与观测都是通道，会话落地后没有对象"
    assert res.deferred is None, "没有文件要删，也就没有「别删早了」这件事"
    plugin.stop()


# ──────────────────────────────────────────────
# rules：转换与原子写
# ──────────────────────────────────────────────

def test_build_routes_ok():
    cfg = {"helper_poll": 30, "profiles": [_profile()]}
    data = rules.build_routes(cfg)
    assert data["poll"] == 30          # 线格式字段名仍是 poll（helper 读，勿改）
    # 产物**不含** ``version``：它是中间产物（喂 ``intent.build_intent``），线格式版本号
    # 由那一步一处给出。原先这里钉着 ``== 1`` —— 那个 1 是换模型前的残余、零消费者，
    # 钉它等于把「这份能直接落盘」这个错觉固化下来。
    assert "version" not in data
    rule = data["rules"][0]
    # 新模型编译回旧的线格式字段，一字不变（helper 已装在机器上）
    assert (rule["name"], rule["iface"], rule["gateway"]) == ("office", "en5", "10.99.1.254")
    assert rule["destinations"] == ["10.99.2.0/24"]
    # 缺省无 paused 字段；enabled 透传为 True
    assert "paused" not in data
    assert rule["enabled"] is True


def test_build_routes_orders_by_list_position():
    """§2.1：列表顺序即优先级——不再有 priority 数字，线格式的 priority 就是下标。"""
    data = rules.build_routes({"helper_poll": 30, "profiles": [
        _profile(name="second"), _profile(name="first"),
    ]})
    assert [r["name"] for r in data["rules"]] == ["second", "first"]
    assert [r["priority"] for r in data["rules"]] == [0, 1]


def test_build_routes_ignores_framework_poll_key():
    """D27：``poll``（框架节拍）与 ``helper_poll``（helper 节律）互不影响。

    两个键各管一件事：读错键不会报错，只会让「改框架节拍」顺手改掉 helper 的巡逻
    频率（或反之）。这条用例把耦合钉死。
    """
    only_framework = rules.build_routes({"poll": 120, "profiles": [_profile()]})
    assert only_framework["poll"] == rules.DEFAULT_HELPER_POLL
    only_helper = rules.build_routes({"helper_poll": 120, "profiles": [_profile()]})
    assert only_helper["poll"] == 120


def test_build_routes_paused_and_enabled():
    cfg = {"helper_poll": 30, "paused": True,
           "profiles": [_profile(routes={"enabled": False, "items": [{"destination": "10.99.2.0/24", "via": "10.99.1.254"}]})]}
    data = rules.build_routes(cfg)
    assert data["paused"] is True
    assert data["rules"][0]["enabled"] is False
    # paused=False 也应显式写入（区别于缺省）
    data2 = rules.build_routes({"helper_poll": 30, "paused": False, "profiles": [_profile()]})
    assert data2["paused"] is False


def test_build_routes_keeps_disabled_scene_so_helper_can_remove_it():
    """场景必须从意图里**出现**才能撤回：helper 只对现存增删（无旁路账本）。

    ``routes.enabled=false`` 落到线格式的 ``enabled=False`` 一位上——它就是旧模型
    ``rules[].enabled`` 的那个位子，helper 早已认识。
    """
    data = rules.build_routes({"helper_poll": 30, "profiles": [
        _profile(routes={"enabled": False, "items": [{"destination": "10.99.2.0/24"}]}),
    ]})
    assert len(data["rules"]) == 1
    assert data["rules"][0]["enabled"] is False
    assert data["rules"][0]["destinations"] == ["10.99.2.0/24"]   # 目标还在，helper 才有据可撤


def test_build_routes_lets_each_item_switch_itself():
    """同一场景内 items **可以各自开关**（v7）：停用那几条仍在 ``destinations`` 里，
    另由本条的 ``off_destinations`` 点名去撤。

    一位 ``enabled`` 装不下「同一条里 A 加、B 撤」；此前这种混合态是**硬报错**，而代价
    是**整个 route_guard 停摆**（所有场景的路由一起停），不只这一个场景。
    改回旧写法时转红的判据：这里会抛 ``RouteConfigError``。
    """
    data = rules.build_routes({"helper_poll": 30, "profiles": [_profile(routes={
        "enabled": True,
        "items": [
            {"destination": "10.99.2.0/24"},
            {"destination": "10.2.0.0/16", "enabled": False},
            {"destination": "10.3.0.0/16", "enabled": True},
        ],
    })]})
    rule = data["rules"][0]
    assert rule["enabled"] is True, "还有一条要叠 ⇒ 整条要叠"
    assert rule["destinations"] == ["10.99.2.0/24", "10.2.0.0/16", "10.3.0.0/16"], \
        "停用的也留在 destinations 里 —— helper 只对意图里现存的目标增删"
    assert rule["off_destinations"] == ["10.2.0.0/16"]


def test_build_routes_omits_off_destinations_when_nothing_to_remove():
    """没有要撤的目标就不写这一栏；整条不叠时也不写（那时 helper 按「整条撤」收敛）。

    同一条尺寸的另一个坑：目标被写了两遍（一遍启用、一遍停用）时**启用者胜** ——
    配置校验不拦重复目标，两套口径各判一次必分叉。
    """
    same = {"destination": "10.99.2.0/24", "via": "10.99.1.254"}
    all_on = rules.build_routes({"helper_poll": 30, "profiles": [
        _profile(routes={"enabled": True, "items": [same]}),
    ]})["rules"][0]
    assert "off_destinations" not in all_on

    all_off = rules.build_routes({"helper_poll": 30, "profiles": [
        _profile(routes={"enabled": True, "items": [
            {"destination": "10.99.2.0/24", "enabled": False}]}),
    ]})["rules"][0]
    assert all_off["enabled"] is False, "一条都不叠 ⇒ 整条不叠"
    assert "off_destinations" not in all_off, "整条都在撤，逐目标那一栏没意义"

    scene_off = rules.build_routes({"helper_poll": 30, "profiles": [
        _profile(routes={"enabled": False, "items": [
            same, {"destination": "10.2.0.0/16", "enabled": False}]}),
    ]})["rules"][0]
    assert scene_off["enabled"] is False and "off_destinations" not in scene_off

    dup = rules.build_routes({"helper_poll": 30, "profiles": [
        _profile(routes={"enabled": True, "items": [
            same, {"destination": "10.99.2.0/24", "enabled": False}]}),
    ]})["rules"][0]
    assert dup["enabled"] is True and "off_destinations" not in dup, "启用者胜"


def test_component_icon_is_the_route_domain_glyph_not_the_shared_shield():
    """网络切换用自己的领域字形 ``ROUTE``，不与被两个组件共用的 ``SHIELD`` 混着。

    ``ROUTE`` 在图标库里躺了很久、一直零消费方，而真正做路由的组件从前挂着
    ``SHIELD``（与 `hosts_guard` 同一枚）⇒ 四台状态机画的是同一个字形，菜单栏
    那一格看不出「这是哪个组件在说话」。
    """
    from barkit.contribution.specs import IconName

    plugin = RouteGuardPlugin.__new__(RouteGuardPlugin)
    assert plugin.component().icon is IconName.ROUTE
    assert IconName.SHIELD not in {s.icon for s in plugin.state_specs()}


def test_warn_values_carry_a_value_icon_so_d13_actually_bites():
    """两个 WARN 取值带 ``ROUTE_WARN`` —— 「叠了但叠得不对」与主图标同形分不出。

    其余取值一律留空（回落主图标）：只有这两个是「形态本身要变」的那一类。
    """
    from barkit.contribution.specs import IconName
    from barkit.plugins.route_guard.constants import PROFILE_STATES, ROUTES_STATES

    by_id = {s.id: s for s in ROUTES_STATES}
    assert by_id["partial"].icon is IconName.ROUTE_WARN
    assert by_id["applied"].icon is None          # 正常态：回落主图标
    assert by_id["failed"].icon is None           # ERR 靠级别色 + 角标兜底，不另造字形
    assert {s.id: s.icon for s in PROFILE_STATES}["conflict"] is IconName.ROUTE_WARN


def test_build_routes_accepts_multi_iface_as_candidates():
    """多网卡**不再报错**：按第一命中制原样收成候选，``iface`` 填第一张兜底。

    语义（2026-10-05 定案）：列表顺序即候选优先级，运行时取第一张**命中**的那张；
    一个 profile 仍只展开一条线格式规则 ⇒ helper 零改动。
    """
    data = rules.build_routes({"helper_poll": 30, "profiles": [
        _profile(ifaces=["en5", "en6"]),
    ]})
    rule = data["rules"][0]
    assert rule["ifaces"] == ["en5", "en6"]      # 候选（不进线格式，只给意图层挑）
    assert rule["iface"] == "en5"                # 编译期兜底：候选里第一张
    # 声明顺序重复的名字去重叠保留原序（``ifaces: [en6, en5, en6]`` 的优先级是 en6 在前）
    dup = rules.build_routes({"helper_poll": 30, "profiles": [
        _profile(ifaces=["en6", "en5", "en6"]),
    ]})
    assert dup["rules"][0]["ifaces"] == ["en6", "en5"]


def test_ifaces_empty_is_rejected_before_a_rule_can_be_built():
    """一张网卡都没有 ⇒ 拦在展开之前（校验层与编译层各有一道）。

    为什么编译层还要再兜一次：产出的 ``iface`` 取候选第一张，空清单会变 IndexError，
    而那条路的报错文案讲不清「你该去改哪一格」。
    """
    from barkit.plugins.route_guard import compile as compile_mod
    with pytest.raises(rules.RouteConfigError) as e:
        rules.build_routes({"helper_poll": 30, "profiles": [_profile(ifaces=[])]})
    assert "ifaces 必须是非空数组" in str(e.value)
    with pytest.raises(rules.RouteConfigError) as e:
        compile_mod._iface_candidates({"name": "office", "ifaces": ["  "]}, 0)
    assert "没有声明任何网卡" in str(e.value)


def test_build_intent_picks_first_hit_iface():
    """第一命中制：候选里第一张**命中**的胜；全不命中仍落在第一张（规则不能消失）。

    全不命中时 ``apply`` 为假 ⇒ helper 按「不叠加」收敛 —— 正是「这场景现在不成立」。
    """
    from barkit.plugins.route_guard import intent as intent_mod
    cfg = {"helper_poll": 30, "profiles": [_profile(ifaces=["en5", "en6"])]}
    compiled = rules.build_routes(cfg)

    up = {"en6"}
    def probe(iface, subnet, gateway):
        return iface in up

    # en6 命中（en5 不在线）⇒ 落在 en6 上
    intent = intent_mod.build_intent(compiled, probe=probe)
    assert intent["rules"][0]["iface"] == "en6"
    assert intent["rules"][0]["apply"] is True

    # 两张都命中 ⇒ 取候选顺序上第一张（en5）
    intent = intent_mod.build_intent(compiled, probe=lambda i, s, g: True)
    assert intent["rules"][0]["iface"] == "en5"

    # 一张都不命中 ⇒ 仍落在第一张，但不叠加
    intent = intent_mod.build_intent(compiled, probe=lambda i, s, g: False)
    assert intent["rules"][0]["iface"] == "en5"
    assert intent["rules"][0]["apply"] is False
    # 候选清单**不进线格式**（helper 的白名单不认识它，写进去整份被拒）
    assert "ifaces" not in intent["rules"][0]


def test_build_intent_single_iface_probes_exactly_once():
    """单网卡场景与多网卡落地前**逐字等价**：仍只探一次（多候选没给主路径加成本）。"""
    from barkit.plugins.route_guard import intent as intent_mod
    compiled = rules.build_routes({"helper_poll": 30, "profiles": [_profile()]})
    seen = []
    intent_mod.build_intent(compiled, probe=lambda i, s, g: seen.append(i) or True)
    assert seen == ["en5"]


def test_build_routes_rejects_multi_via_until_helper_upgrade():
    """多下一跳今天仍表达不了 → 报错，而不是静默丢房子。"""
    with pytest.raises(rules.RouteConfigError) as e:
        rules.build_routes({"helper_poll": 30, "profiles": [
            _profile(destinations=("10.99.2.0/24", "10.2.0.0/16"),
                     routes={"enabled": True, "items": [
                         {"destination": "10.99.2.0/24", "via": "10.99.1.254"},
                         {"destination": "10.2.0.0/16", "via": "10.9.0.1"},
                     ]}),
        ]})
    assert "多下一跳" in str(e.value)


def test_build_routes_rejects_legacy_rules_key_out_right():
    """旧格式不是「待迁移」，是「拒绝并指明改法」（用户裁决：不做向后兼容）。

    关键是**不要静默**:旧段读不出来又不出声，症状是「路由莫名没了」，比报错难查得多。
    """
    with pytest.raises(rules.RouteConfigError) as e:
        rules.build_routes({"helper_poll": 30, "rules": [OFFICE]})
    msg = str(e.value)
    assert "profiles" in msg and "routes.items" in msg      # 报错里给出手改映射


def test_build_routes_rejects_unknown_top_level_keys():
    """拼错的顶层键当场报错——静默忽略会让「写了不生效」没有任何线索。"""
    with pytest.raises(rules.RouteConfigError) as e:
        rules.build_routes({"helper_poll": 30, "profiles": [_profile()], "rulez": []})
    assert "rulez" in str(e.value)
    # ``poll`` 是框架节拍覆盖键（D27），本模块不读它，但它是合法的段内键
    assert rules.build_routes({"poll": 120, "helper_poll": 30,
                               "profiles": [_profile()]})["poll"] == 30


def test_build_routes_rejects_legacy_manual_override():
    """旧覆盖层的 ``force_apply`` / ``manual_override`` 都已下线：报错，文案给出等价写法。"""
    with pytest.raises(rules.RouteConfigError) as e:
        rules.build_routes({"helper_poll": 30, "profiles": [_profile()],
                            "rule_overrides": {"office": {"force_apply": True}}})
    assert "routes_suppressed" in str(e.value)
    with pytest.raises(rules.RouteConfigError):
        rules.build_routes({"helper_poll": 30, "profiles": [_profile()],
                            "rule_overrides": {"office": {"manual_override": True}}})
    with pytest.raises(rules.RouteConfigError):
        rules.build_routes({"helper_poll": 30, "profiles": [_profile()],
                            "rule_overrides": {"office": {"routes_suppressed": 1}}})


def test_unknown_key_error_says_old_version_not_a_typo():
    """报错**先定性**：这是旧版本 BarKit 自己写下的键，不是用户写错了。

    2026-09-18 用户要求（原话「防止用户理解错误」）：实测触发场景是面板开关从前把
    ``force_apply`` 写进用户 YAML，新版把它当未知键拒了。原文案「含未知字段：['force_apply']」
    读起来像**用户自己手滑打错** —— 于是用户会去翻自己的 YAML 找错别字，而正确动作其实是
    删掉那几行（界面上根本看不见它）。故三项缺一不可：**定性、指路、映射**。
    """
    with pytest.raises(rules.RouteConfigError) as e:
        rules.build_routes({"helper_poll": 30, "profiles": [_profile()],
                            "rule_overrides": {"office": {"force_apply": True}}})
    msg = str(e.value)
    assert "旧版本" in msg and "不是您写错了" in msg
    assert "恢复默认配置" in msg, "同一句里要给出一键清掉的入口（界面上看不见那个键）"
    assert "routes_suppressed" in msg, "旧写法 → 新写法的映射不能丢"

    with pytest.raises(rules.RouteConfigError) as e:
        rules.build_routes({"helper_poll": 30, "profiles": [_profile()], "rulez": []})
    top = str(e.value)
    assert "rulez" in top and "旧版本" in top


def test_config_reset_heals_a_component_stuck_on_a_legacy_key(tmp_path, monkeypatch):
    """端到端：段里留着旧版本的键 ⇒ 组件报「配置非法」；**一键恢复默认之后就自愈**。

    这条串起 2026-09-18 用户诉求的全链路：框架统一入口清掉用户差异段 → 广播
    ``config.changed`` → 组件重读配置、重判合法性、重建期望态。缺任何一环，用户看到的
    都是「界面上说清干净了，面板还红着」。
    """
    body = (
        "plugins:\n"
        "  route_guard:\n"
        "    enabled: true\n"
        "    helper_poll: 30\n"
        "    rule_overrides:\n"
        "      office:\n"
        "        force_apply: true\n"
    )
    plugin = _plugin(tmp_path, monkeypatch, body=body)
    assert "force_apply" in plugin._config_problem, "旧键在 ⇒ 组件判为配置非法"
    assert plugin.ctx.helper.declared() is None, "非法配置 ⇒ 一帧都不声明"

    cfg = plugin.ctx.config
    assert cfg.clearable_plugin_keys("route_guard") == ["helper_poll", "rule_overrides"]
    assert cfg.reset_plugin_section("route_guard") == ["helper_poll", "rule_overrides"]
    cfg.changed.emit(cfg.data)       # 框架侧由 manager.reset_plugin_config 广播这一声

    assert plugin._config_problem == "", "组件必须自愈（不重读＝界面清了、面板还红着）"
    names = {r["name"] for r in plugin.ctx.helper.declared()["rules"]}
    assert names == {"office", "home"}, "合法的期望态当场重建"
    assert _user_section(tmp_path) == {"enabled": True}, "只剩启用状态"
    plugin.stop()


def test_build_routes_invalid_raises():
    from barkit.plugins.route_guard.rules import RouteConfigError

    with pytest.raises(RouteConfigError):
        rules.build_routes({"helper_poll": 30, "profiles": [_profile(gateway="nope")]})
    with pytest.raises(RouteConfigError):
        rules.build_routes({"helper_poll": "fast", "profiles": []})
    with pytest.raises(RouteConfigError):
        rules.build_routes({"helper_poll": 30, "profiles": [
            _profile(routes={"enabled": True, "items": [{"destination": "10.99.2.0/999"}]})]})
    with pytest.raises(RouteConfigError):
        rules.build_routes({"helper_poll": 30, "paused": "yes", "profiles": []})
    with pytest.raises(RouteConfigError):
        rules.build_routes({"helper_poll": 30, "profiles": [_profile(), _profile()]})  # 重名
    with pytest.raises(RouteConfigError):
        rules.build_routes({"helper_poll": 30, "fallback": "maybe", "profiles": []})
    with pytest.raises(RouteConfigError):
        rules.build_routes({"helper_poll": 30, "pinned": "nope", "profiles": [_profile()]})


def test_validate_profile_covers_new_shape():
    """新模型的新字段：合法取值收下，非法取值/拼错的键当场报错。"""
    assert rules.validate_profile(_profile()) is None
    # manual_only（§2.6）：今天只落存储
    assert rules.validate_profile(_profile(manual_only=True)) is None
    # net.static 带 IP，或 dhcp 带 pin 固化（§2.5）
    assert rules.validate_profile(_profile(
        net={"mode": "static", "ip": "192.168.11.5", "mask": "255.255.255.0",
             "gateway": "192.168.11.1", "dns": ["8.8.8.8"]})) is None
    assert rules.validate_profile(_profile(
        net={"mode": "dhcp", "pin": {"ip": "192.168.2.88", "mask": "255.255.255.0"}})) is None
    # 「照抄租约」（§2.5 的补充）：只写 true 的开关合法
    assert rules.validate_profile(_profile(
        net={"mode": "dhcp", "pin": {"ip": "", "mask": "",
                                     "gateway_from_lease": True, "dns_from_lease": True}}
    )) is None
    # 非法：开关只能写 true（写 false 与不写等价，不留多余语义）
    assert rules.validate_profile(_profile(
        net={"mode": "dhcp", "pin": {"ip": "", "mask": "", "gateway_from_lease": False}}
    )) is not None
    # 非法：照抄与写死值互斥（app 侧与 helper 侧同一份口径，只认一边会让配置「只有一半说得通」）
    assert rules.validate_profile(_profile(
        net={"mode": "dhcp", "pin": {"ip": "", "mask": "", "gateway": "10.99.1.254",
                                     "gateway_from_lease": True}}
    )) is not None
    assert rules.validate_profile(_profile(
        net={"mode": "dhcp", "pin": {"ip": "", "mask": "", "dns": ["8.8.8.8"],
                                     "dns_from_lease": True}}
    )) is not None
    # 非法：mode 不存在第三种值；static 缺 ip；dhcp 下不许写 static 的字段；pin 缺 mask
    assert rules.validate_profile(_profile(net={"mode": "dhcp_pin"})) is not None
    assert rules.validate_profile(_profile(net={"mode": "static", "mask": "255.255.255.0"})) is not None
    assert rules.validate_profile(_profile(net={"mode": "dhcp", "ip": "1.2.3.4"})) is not None
    assert rules.validate_profile(_profile(net={"mode": "dhcp", "pin": {"ip": "1.2.3.4"}})) is not None
    assert rules.validate_profile(_profile(ifaces=[])) is not None
    assert rules.validate_profile(_profile(match={"gateway": "nope"})) is not None
    assert rules.validate_profile(_profile(match={"ssid": "x"})) is not None
    assert rules.validate_profile(_profile(routes={"enabled": True, "items": [{"dst": "x"}]})) is not None


def test_shipped_default_profiles_compile_like_the_old_default_rules():
    """配置模型的验收判据：**发货默认场景编译出的规则字段仍是原来那一份**。

    换模型前发货的默认规则是 office / home 两条；换模型后同样的内容先写进
    ``config.default.yaml`` 的 ``profiles[]``，2026-09-20 再往前一步 —— 真源搬进
    ``plugin.py::_SHIPPED_DEFAULT_PROFILES``（§9.5 默认值单一真源，出厂文件不再含它）。
    **本用例吃那个常量本身**，不是另手写一份等价的：真源一换，这条就是唯一的守门人。
    编译结果必须逐项相同（``priority`` 除外——它现在是列表下标而非用户手写的数字，
    相对次序一致即可）。
    **不比 ``version``**：产物是中间产物、本就不带它（线格式版本的真源在
    ``intent.build_intent``），故 ``old_doc`` 里也不再摆一个摆着好看的版本号。

    ⚠️ 反向验证时**别往 ``match.gateway`` 里植入**：规则里的 ``gateway`` 取自场景
    ``routes.items[].via``（那是**动作** —— 往哪一跳），``match.gateway`` 是**匹配条件**
    （在哪个网络里命中）。改前者，产物会跟着变、这条转红；改后者，产物一个字不动，
    于是这条「看着是假守、其实只是植入点选错了」。实测记录见
    ``内部记录（配置迁移）``。
    """
    old_doc = {
        "poll": 30,
        "rules": [
            {"name": "office", "priority": 0, "iface": "en5", "gateway": "10.99.1.254",
             "destinations": ["10.99.2.0/24", "10.2.0.0/16", "192.168.100.5/32"],
             "enabled": True},
            {"name": "home", "priority": 1, "iface": "en6", "gateway": "192.168.2.1",
             "destinations": ["172.16.0.0/16"], "enabled": True},
        ],
    }
    new_doc = rules.build_routes({
        "helper_poll": rules.DEFAULT_HELPER_POLL,
        "profiles": _SHIPPED_DEFAULT_PROFILES,
    })
    assert new_doc["poll"] == old_doc["poll"]
    assert len(new_doc["rules"]) == len(old_doc["rules"])
    for new_rule, old_rule in zip(new_doc["rules"], old_doc["rules"]):
        for key in ("name", "iface", "gateway", "destinations", "enabled"):
            assert new_rule[key] == old_rule[key], f"{key} 变了 ⇒ 不是零行为变更"
        assert "force_apply" not in new_rule      # 字节级兼容未升级的旧 helper


def test_legacy_rules_section_is_left_untouched_by_plugin(tmp_path, monkeypatch):
    """不自动迁移：用户段里的旧 rules[] **一个字节都不改**，只在求值时报错。

    这条钉的是「宁可报错也不改写用户的文件」——自动迁移会把用户验收过的一份 YAML
    换成他没写过的样子，且那次写入不可逆。
    """
    body = (
        "plugins:\n  route_guard:\n    enabled: true\n"
        "    rules:\n"
        "      - name: office\n        priority: 3\n        iface: en5\n"
        "        gateway: 10.99.1.254\n        destinations: [10.99.2.0/24]\n"
    )
    ctx = _ctx(tmp_path, body=body)
    monkeypatch.setattr(daemon, "status", lambda: {"installed": True, "running": True})
    RouteGuardPlugin(ctx)
    section = _user_section(tmp_path)
    assert "rules" in section              # 旧段原样留着
    assert "profiles" not in section       # 没有被改写 / 新增
    with pytest.raises(rules.RouteConfigError):
        rules.build_routes(dict(section, helper_poll=30))


# 这里原有一条 ``test_write_routes_atomic``：测 ``rules.write_routes`` 的原子写 + tmp 不残留。
# 那个函数已删（落盘入口只留 ``intent.write_intent``），原子性由
# ``test_write_intent_atomic_and_readable`` 继续守着 —— 通道收窄，覆盖不丢。


# ──────────────────────────────────────────────
# plugin：接线
# ──────────────────────────────────────────────

class _Link:
    """最小会话桩：记下声明出去的 ``(subject, payload)``、发出的命令与订阅。

    ``snapshot()`` 是组件侧 ``observed("route")`` 的读口 —— 守护推来的事实住在**内存**
    里（从前是每轮写下的 ``state.json``）。``push()`` 模拟一次推送。
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
        """模拟守护推来一份事实（见模块级 ``_push_route``）。"""
        self._observed[subject] = observed

    def declared(self, subject: str = "route"):
        """最近一次为 ``subject`` 交出去的期望态正文（没有则 ``None``）。"""
        for sub, payload in reversed(self.sent):
            if sub == subject:
                return payload
        return None


def _push_route(plugin, state: dict) -> None:
    """模拟守护推来一份路由观测（组件侧 ``observed("route")`` 的读口）。

    取代旧的「往 ``state.json`` 写一份」：观测不是文件，是会话上推来的一帧。
    """
    plugin.ctx.helper.push("route", state)


def _ctx(tmp_path, events=None, body: str | None = None,
         helper=None) -> PluginContext:
    """插件上下文：默认按**新键**配（``helper_poll``，D27 拆键后的写法）。

    ``body`` 可换成任意 YAML 片段——「旧写法该怎么处置」的用例走这条路（组件
    不再有任何迁移，只看它读出来的结果对不对、用户的文件有没有被动过）。

    ``helper`` 是会话桩（默认给一个）：组件侧的 ``declare`` / ``run_op`` /
    ``observed`` 三个包装都落到它上面。
    """
    user = tmp_path / "config.yaml"
    user.write_text(
        body or "plugins:\n  route_guard:\n    enabled: true\n    helper_poll: 30\n",
        encoding="utf-8",
    )
    ctx = PluginContext(config=Config(user_path=user), events=events)
    ctx.helper = helper if helper is not None else _Link()
    return ctx


def _user_section(tmp_path) -> dict:
    """用户配置里的 ``plugins.route_guard``（读原始 YAML，看真实落盘）。"""
    import yaml

    raw = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    return (raw.get("plugins") or {}).get("route_guard") or {}


def test_no_key_migration_rewrites_user_yaml(tmp_path, monkeypatch):
    """旧配置键**不迁移**：组件一个字节都不改写用户 YAML。

    原先那条「``poll`` → ``helper_poll``」的一次性迁移（D27）已于 2026-09-17 删除。
    它当初的动机是「别让用户调过的 helper 节律被改回默认」，但代价是把用户验收过的
    一份 YAML 换成他没写过的样子、且那次写入不可逆 —— 而本机没有任何人手写过这两个
    键。现在改成：``poll`` 原地留着（它本来就是合法的框架节拍覆盖），死键
    ``state_poll`` 留着并在**求值路径**报错指路（见下一条）。
    """
    body = (
        "plugins:\n  route_guard:\n    enabled: true\n    poll: 120\n    state_poll: 5\n"
    )
    ctx = _ctx(tmp_path, body=body)
    monkeypatch.setattr(daemon, "status", lambda: {"installed": True, "running": True})
    RouteGuardPlugin(ctx)
    section = _user_section(tmp_path)
    assert section.get("poll") == 120       # 原地留着：它是合法的框架节拍覆盖
    assert section.get("state_poll") == 5   # 死键也留着：报错在求值路径，不在启动路径
    assert "helper_poll" not in section     # 不凭空给用户补键


def test_plugin_construction_never_rewrites_user_config(tmp_path):
    """幂等 + 不白写：没有迁移这回事，构造插件一个字都不落盘。"""
    ctx = _ctx(tmp_path)
    before = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    RouteGuardPlugin(ctx)
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == before


def test_framework_poll_is_not_read_as_helper_rhythm(tmp_path):
    """D27 判据（删掉迁移后反而更干净）：``poll`` 只归框架，helper 节律回落默认。"""
    p = RouteGuardPlugin(_ctx(
        tmp_path, body="plugins:\n  route_guard:\n    enabled: true\n    poll: 60\n",
    ))
    assert p.plugin_config().get("poll") == 60        # 框架节拍覆盖仍在生效
    assert rules.build_routes(p.plugin_config())["poll"] == rules.DEFAULT_HELPER_POLL


def test_dead_state_poll_key_is_rejected_with_advice():
    """死键不「顺手删掉」，而是报错并给出改法（删掉这一行即可）。"""
    with pytest.raises(rules.RouteConfigError) as e:
        rules.build_routes({
            "helper_poll": 30, "state_poll": 5, "profiles": [_profile()],
        })
    assert "state_poll" in str(e.value)


def _plugin(tmp_path, monkeypatch, events=None, body: str | None = None,
            link=None) -> RouteGuardPlugin:
    # 守护状态**不得依赖真机已装版本**：`Plugin.__init__` 会调 `daemon.status()` 并缓存。
    # 不桩的话每次 bump `HELPER_VERSION` 都会让这批单测假红（真机 2.11.1 vs 源码 2.11.2
    # → 误判「守护过期」）。真机状态相关用例自己置 `_daemon_status`（见 outdated 用例）。
    monkeypatch.setattr(daemon, "status", lambda: {
        "installed": True, "running": True, "outdated": False,
        "version": helper.HELPER_VERSION, "expected": helper.HELPER_VERSION,
    })
    plugin = RouteGuardPlugin(_ctx(tmp_path, events, body, link))
    plugin.start()
    return plugin


def test_start_declares_routes_intent(tmp_path, monkeypatch):
    """启动即把期望态**声明出去**（不再往数据目录写一份 ``routes.json``）。"""
    plugin = _plugin(tmp_path, monkeypatch)
    data = plugin.ctx.helper.declared()
    assert data["poll"] == 30
    plugin.stop()


def test_tick_returns_four_snapshots(tmp_path, monkeypatch):
    """v3 契约：tick() 返回 ``{key: StateReport}``（四台机），框架逐 key 自动 publish。

    原先是单台 ``main`` 的一份快照（设计 §6.3 第 6 步把它拆成 daemon / routes /
    profile，一台一行不行 —— 面板仍是一行，副标题由组件合成）。第 4 台 ``net``
    随第 8 步的 helper 网卡能力上线（数据源只在 helper 回显里，见 ``NET_STATES``）。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    _push_route(plugin, {"ok": True, "checked_at": 1700000000, "rules": [
        {"name": "office", "matched": True, "applied": True, "enabled": True,
         "trigger_matched": True,
         "destinations": [{"ok": True, "destination": "10.99.2.0/24"}]}
    ]})
    snaps = plugin.tick()
    assert set(snaps) == {"daemon", "net", "routes", "profile"}, "四台机的键固定"
    assert all(isinstance(s, StateReport) for s in snaps.values())
    assert snaps["daemon"].state == "ready"
    assert snaps["routes"].state == "applied"        # 取值名，级别归声明（决议 D13）
    assert snaps["routes"].summary == "已叠加 1 条"  # 条数进文案（声明里答不出来）
    assert snaps["profile"].state == "active"
    assert snaps["profile"].summary == "office"      # 场景名走 summary（设计 §3.1）
    # 这份回显没有 ``net`` 段（第 8 步之前的 helper 就写不出它）⇒ 首屏起点「尚未读取」，
    # **不是**「跟随系统」—— 没读到就不许替网卡编一个模式（``_net_state`` 的判据）。
    assert snaps["net"].state == "unknown"
    plugin.stop()


def test_reported_values_are_all_declared(tmp_path, monkeypatch):
    """四台机报了取值就必须在声明里 —— 闭集（决议 D13）不是形容词。

    这一条同时钉住「取值名两端同源」：模块顶四张表的 ``id`` 与 ``StateSpec.states``
    是同一份对象；报告若拼错一个字母，框架只会把那一格当「未声明的取值」画成 ERR。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    declared = {s.key: {opt.id for opt in s.states} for s in plugin.state_specs()}
    assert set(declared) == {"daemon", "net", "routes", "profile"}

    # 各种回显形态（含「读不到」）都扫一遍：取值不得越界
    for state in (
        {"ok": True, "rules": []},
        {"ok": True, "error": "意图文件被拒绝", "rules": []},
        {"ok": True, "rules": [
            {"name": "office", "applied": True, "enabled": True, "trigger_matched": True,
             "destinations": [{"ok": False}]},
        ]},
        {"ok": True, "rules": [
            {"name": "office", "applied": False, "enabled": True, "trigger_matched": True,
             "destinations": [{"ok": True}]},
            {"name": "home", "applied": False, "enabled": True, "trigger_matched": True,
             "destinations": [{"ok": True}]},
        ]},
        {"ok": False, "error": "守护尚未回报（未连接或未运行）"},
    ):
        plugin._state = state
        for key, snap in plugin._all_reports().items():
            assert snap.state in declared[key], f"{key} 报了没声明的取值 {snap.state!r}"
    plugin.stop()


def test_manual_action_publishes_all_machines(tmp_path, monkeypatch, qtbot):
    """手动动作经 ``_publish_all()`` **逐台机**广播 StateReport（三台各一条 topic）。

    旧写法是一份快照发到保留键 ``main``；多台机之后每条 topic 各发自己那份，
    否则订阅方只看到其中一台（``status_topic`` 的注释把这条钉成保留键存在的原因）。
    """
    events = EventBus()
    plugin = _plugin(tmp_path, monkeypatch, events=events)
    received: dict[str, list] = {k: [] for k in ("daemon", "routes", "profile")}
    for key, bucket in received.items():
        events.subscribe(plugin.status_topic(key), bucket.append)
    plugin.immediate_check()          # 手动动作：触碰 trigger 后即时广播
    qtbot.waitUntil(lambda: all(bucket for bucket in received.values()), timeout=2000)
    assert all(isinstance(bucket[-1], StateReport) for bucket in received.values())
    plugin.stop()


def test_immediate_check_sends_the_resync_command(tmp_path, monkeypatch):
    """「立即检查」＝ ``resync`` 一帧（不是 touch 文件）：「现在跑一轮」，不重发配置。"""
    plugin = _plugin(tmp_path, monkeypatch)
    plugin.immediate_check()
    assert plugin.ctx.helper.ops[-1] == ("resync", {"subject": "route"})
    plugin.stop()


# ──────────────────────────────────────────────
# 三台状态机 + 行级副标题（设计 §3.1 / §3.2 / §3.3 —— §6.3 第 6 步）
# ──────────────────────────────────────────────

def _machine_specs(plugin) -> dict:
    """该组件声明的三台状态机的声明（键 → StateSpec）。"""
    return {
        s.key: s
        for s in gather_specs(plugin, plugin.id) if isinstance(s, StateSpec)
    }


def _push_all(panel, plugin, specs) -> None:
    """把三台机的真实快照推给面板（等价于框架逐 key 发布后再重算代表态）。"""
    for key, report in plugin._all_reports().items():
        panel.update_state_snapshot(specs[key], report)


def _echo(rules: list[dict], *, ok: bool = True, error=None) -> dict:
    """一份 helper 回显（``routeops.run_cycle`` 的形状）。

    ⚠️ 目标上的 ``ok`` 是「**这一轮那次动作成功了**」，不是「此刻在路由表里」：
    ``ensure_removed`` 在「本来就没有」时也返回成功。故「不叠加」的那条规则，
    目标照样是 ``ok: True``（旧用例写成 False 是错的，会把「没叠」误报成失败）。
    """
    return {"ok": ok, "checked_at": 1700000000, "error": error, "rules": rules}


def _rule(name="office", *, applied=True, matched=True, enabled=True, dests=("10.99.2.0/24",),
          dest_ok=True) -> dict:
    return {
        "name": name, "applied": applied, "enabled": enabled,
        "trigger_matched": matched, "iface": "en5", "gateway": "10.99.1.254",
        "destinations": [{"destination": d, "ok": dest_ok} for d in dests],
    }


def test_machines_declare_design_value_sets(tmp_path, monkeypatch):
    """四台机的取值闭集与设计 §3.1 一致（少一格/多一格都算偏离）。"""
    plugin = _plugin(tmp_path, monkeypatch)
    specs = plugin.state_specs()
    assert [s.key for s in specs] == ["daemon", "net", "routes", "profile"], "声明顺序 = 关注度顺序"
    assert [s.title for s in specs] == ["守护", "网卡", "路由", "场景"], "多台机各自显式命名（D14）"

    def ids(key):
        return {opt.id for opt in next(s for s in specs if s.key == key).states}

    assert ids("daemon") == {"unknown", "ready", "missing", "outdated", "not_needed"}
    assert ids("routes") == {
        "unknown", "applied", "partial", "pending", "none", "frozen", "failed",
        # v8：留空的下一跳取不到网关 ⇒ 那一份路由一个都没动（既不是「失败」也不是
        # 「未叠加」，见 ``ROUTES_STATES`` 那条注释）
        "no_gateway",
    }
    assert ids("profile") == {"unknown", "active", "none", "conflict"}
    # 第 8 步随 helper 的网卡能力上线（第 6 步按「宁缺勿假」有意不声明）：比设计 §3.1
    # 多一格 ``unknown``（首屏起点）—— 口径真源就在 ``NET_STATES`` 的注释里。
    assert ids("net") == {"unknown", "static", "pinned", "dhcp", "frozen", "failed"}
    plugin.stop()


def test_daemon_machine_covers_ready_missing_outdated(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path, monkeypatch)
    plugin._daemon_status = {"installed": True, "running": True, "outdated": False}
    assert plugin._daemon_report().state == "ready"

    plugin._daemon_status = {"installed": False, "running": False, "outdated": False}
    assert plugin._daemon_report().state == "missing"

    plugin._daemon_status = {
        "installed": True, "running": True, "outdated": True,
        "version": "1.0", "expected": "2.0",
    }
    rep = plugin._daemon_report()
    assert rep.state == "outdated" and "v1.0 → v2.0" in rep.summary
    plugin.stop()


def test_paused_downgrades_every_machine_to_idle(tmp_path, monkeypatch):
    """停用（§3.2）：三台机全部降级 IDLE —— 且**不撒谎**。

    用户自己关掉的自动化，面板该变灰，不该继续满屏红；但「关掉自动化」和「功能正常」
    必须看得出区别（前者 IDLE 灰，后者 daemon 是 OK 绿）。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    # 停用前：一处也不健康（守护过期 + 叠加失败）
    plugin._daemon_status = {
        "installed": True, "running": True, "outdated": True,
        "version": "1.0", "expected": "2.0",
    }
    plugin._state = _echo([_rule(applied=False, matched=True, dest_ok=False)])
    assert plugin._daemon_report().state == "outdated"
    assert plugin._routes_report().state == "failed"

    plugin.set_paused(True)
    reports = plugin._all_reports()
    assert (reports["daemon"].state, reports["profile"].state) == ("not_needed", "none")
    assert reports["routes"].state == "frozen", "停用后路由仍在，报「未叠加」就是粉饰"

    level_of = lambda key, value: next(          # noqa: E731 —— 用例内的查表小工具
        o.level for o in next(s for s in plugin.state_specs() if s.key == key).states
        if o.id == value
    )
    from barkit.contribution.specs import StateLevel

    assert all(
        level_of(key, rep.state) is StateLevel.IDLE
        for key, rep in reports.items()
    ), "停用时三台机必须全 IDLE（§3.2）"
    plugin.stop()


def test_routes_machine_reads_helper_echo(tmp_path, monkeypatch):
    """``routes`` 只认回显：应用 / 部分叠加 / 待应用 / 未叠加 / 读不到 五格各有来源。"""
    plugin = _plugin(tmp_path, monkeypatch)

    # 还没收到过推送 ⇒ 不知道，不冒充「未叠加」
    plugin._state = {"ok": False, "error": "守护尚未回报（未连接或未运行）"}
    assert plugin._routes_report().state == "unknown"

    # helper 拒绝了期望态：错误原文进 summary，级别 ERR（这就是「配置写坏了看得见」）
    plugin._state = _echo([], error="意图文件被拒绝：rules[0] 含未知字段")
    rep = plugin._routes_report()
    assert rep.state == "failed" and "未知字段" in rep.summary

    # 全部叠上
    plugin._state = _echo([_rule(dests=("10.99.2.0/24", "10.2.0.0/16"))])
    rep = plugin._routes_report()
    assert (rep.state, rep.summary) == ("applied", "已叠加 2 条")

    # 一半成功一半失败 ⇒ 部分叠加（WARN），不是 ERR
    plugin._state = {
        "ok": True, "rules": [{
            **_rule(dests=("10.99.2.0/24", "10.2.0.0/16")),
            "destinations": [{"destination": "10.99.2.0/24", "ok": True},
                             {"destination": "10.2.0.0/16", "ok": False}],
        }],
    }
    rep = plugin._routes_report()
    assert (rep.state, rep.summary) == ("partial", "部分叠加（1/2 条）")

    # 一个也没成 ⇒ 失败
    plugin._state = _echo([_rule(dest_ok=False)])
    assert plugin._routes_report().state == "failed"

    # 期望该叠、回显里一条都没叠 ⇒ 待应用（helper 还没消费到这一份）。
    # **必须带目标**：第 8 步起「一个路由目标都没有」是合法形态（场景只切 IP 不加路由，
    # §4.7 一），那条路走下面的 ``none``；拿空目标来问「待应用」问不出来。
    plugin._state = _echo([_rule(applied=False, matched=True)])
    assert plugin._routes_report().state == "pending"

    # 一条路由目标都没有 ⇒ 未叠加（路由这半没话说，网卡那半由 net 机自己说）——
    # 即便 ``trigger_matched`` 为真也不报「待应用」：没有目标就没什么可等的。
    plugin._state = _echo([_rule(applied=False, matched=True, dests=())])
    assert plugin._routes_report().state == "none"

    # 本来就不该叠 ⇒ 未叠加（目标 ok=True 只是「确认它不在」，不算已叠加）
    plugin._state = _echo([_rule(applied=False, matched=False)])
    assert plugin._routes_report().state == "none"
    plugin.stop()


def test_profile_machine_reports_conflict_when_two_scenes_match(tmp_path, monkeypatch):
    """两个场景同时命中 ⇒ ``conflict``（闭集第三值不是凑数，见设计 §3.1）。"""
    plugin = _plugin(tmp_path, monkeypatch)
    plugin._state = _echo([
        _rule("office", matched=True),
        _rule("home", matched=True),
    ])
    rep = plugin._profile_report()
    assert rep.state == "conflict" and rep.summary == "office、home"

    plugin._state = _echo([_rule("office", matched=True), _rule("home", matched=False)])
    rep = plugin._profile_report()
    assert (rep.state, rep.summary) == ("active", "office")

    plugin._state = _echo([_rule("office", matched=False), _rule("home", matched=False)])
    assert plugin._profile_report().state == "none"
    plugin.stop()


def test_row_subtitle_speaks_for_all_three_machines(tmp_path, monkeypatch):
    """副标题 = 几台机合成的一句，次序即设计 §3.3 的口径。

    标准句是「网关命中 · 已叠加 3 条」——**不含场景名**：它在徽标上，设计 §3.3 要求
    配置项名只出现一次（第 6 步留的口子，第 7b 步随徽标换成场景选择器一起兑现），
    **也不含「守护就绪」**：装置健康是默认态，占一格等于什么都没说（2026-09-18 用户
    裁定，见下条用例）。这正是不拿代表态 ``summary`` 顶副标题的原因（代表态只会给
    一台机的一句话）。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    plugin._daemon_status = {"installed": True, "running": True, "outdated": False}

    plugin._state = _echo([
        _rule(dests=("10.99.2.0/24", "10.2.0.0/16", "192.168.100.5/32")),
    ])
    assert plugin._row_subtitle() == "网关命中 · 已叠加 3 条"

    # 装置坏了 ⇒ 异常领句（用户此刻要处理的就是这件事）
    plugin._daemon_status = {
        "installed": True, "running": True, "outdated": True,
        "version": "1.0", "expected": "2.0",
    }
    assert plugin._row_subtitle() == "守护过期 v1.0 → v2.0 · 在设置页重装"

    plugin._daemon_status = {"installed": False, "running": False, "outdated": False}
    assert plugin._row_subtitle() == "守护未安装 · 路由不会自动维护"
    plugin.stop()


def test_row_subtitle_drops_the_daemon_segment_while_healthy(tmp_path, monkeypatch):
    """装置健康时**不占那一格**：默认态写进文案＝同义反复（2026-09-18 用户裁定）。

    同源判据：§4.6 里徽标原先写「自动 / 手动」被换掉，理由就是「那是默认态，放进一个
    格子等于开关的同义反复」。装置那台机同理 —— 四种故障本来就各自**领句并 return**
    （见上一条用例），正常时再写一句「守护就绪」只是白占 320px 里的一截。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    plugin._daemon_status = {"installed": True, "running": True, "outdated": False}
    plugin._state = _echo([_rule()])
    text = plugin._row_subtitle()
    assert "守护就绪" not in text, text
    assert text == "网关命中 · 已叠加 1 条", text
    plugin.stop()


def test_row_subtitle_says_why_the_scene_matched(tmp_path, monkeypatch):
    """「凭什么就是它」：自动命中时说出**依据**（2026-09-18 用户裁定）。

    原先这里写「自动命中」—— 它只回答「自动而不是手动」，用户看完仍要问「命中了什么」；
    而场景名就写在旁边的徽标上，复述它是设计 §3.3 明令禁止的（配置项名只出现一次）。
    依据（网段 / 网关）**别处都没有**、只在配置窗体里查得到，正是「为什么生效的是它」的
    答案。四档与配置一一对应：都写 = AND（§2.1）⇒ 说两样；只写一样就说那一样；两样都没
    写 ⇒ 该场景「网卡在线即命中」（同 `_scene_hint` 的兜底口径）。
    """
    body = (
        "plugins:\n  route_guard:\n    enabled: true\n    helper_poll: 30\n"
        "    profiles:\n"
        "      - name: office\n"
        "        ifaces: [en5]\n"
        "        match: {cond}\n"
        "        routes:\n"
        "          enabled: true\n"
        "          items:\n"
        "            - {{destination: 10.99.2.0/24, via: 10.99.1.254}}\n"
    )
    cases = [
        ("{gateway: 10.99.1.254}", "网关命中"),
        ("{subnet: 10.8.0.0/16}", "网段命中"),
        ("{gateway: 10.99.1.254, subnet: 10.8.0.0/16}", "网段与网关命中"),
        ("{}", "网卡在线命中"),
    ]
    for cond, want in cases:
        plugin = _plugin(tmp_path, monkeypatch, body=body.format(cond=cond))
        plugin._daemon_status = {"installed": True, "running": True, "outdated": False}
        plugin._state = _echo([_rule()])
        text = plugin._row_subtitle()
        assert text.startswith(want + " · "), f"{cond!r} → {text!r}"
        plugin.stop()

    # 手动指定那一路不受影响（有依据也不说依据：锁着的时候与网段 / 网关无关）
    plugin = _plugin(tmp_path, monkeypatch)
    plugin._daemon_status = {"installed": True, "running": True, "outdated": False}
    plugin._state = _echo([_rule()])
    plugin.set_pinned("office")
    assert plugin._row_subtitle().startswith("手动指定 · "), plugin._row_subtitle()
    plugin.stop()



def test_row_subtitle_separates_this_time_from_config(tmp_path, monkeypatch):
    """副标题把「本次已撤回」与「按配置不叠加」分开说；场景名**一次都不出现**。

    §4.8 要的是两句分开（开关上两者长得一样）；§3.3 要的是配置项名只出现一次 ——
    那一次在**徽标**上（第 7b 步起徽标就是场景选择器），副标题一个字都不占。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    plugin._state = _echo([_rule(applied=False, matched=True)])
    plugin.set_routes_suppressed("office", True)
    text = plugin._row_subtitle()
    # 条数取**配置**（office 默认 3 条：撤掉的就是那一套），回显只兜底
    assert text.startswith("网关命中 · 本次已撤回路由（3 条）"), text
    assert "office" not in text, "场景名归徽标（设计 §3.3：配置项名只出现一次）"
    plugin.stop()


def test_row_subtitle_says_what_is_frozen_when_paused(tmp_path, monkeypatch):
    """停用句必须写明代价：路由**仍生效**（§3.2 要求停用不撒谎 + 显式写出后果）。"""
    plugin = _plugin(tmp_path, monkeypatch)
    plugin._state = _echo([_rule(dests=("10.99.2.0/24", "10.2.0.0/16"))])
    plugin.set_paused(True)
    assert plugin._row_subtitle() == "已停用 · 保持现状，路由仍生效（2 条）"

    # 停用且没有东西在生效：不许编一个 0 条出来
    plugin._state = _echo([_rule(applied=False, matched=False)])
    assert plugin._row_subtitle() == "已停用 · 保持现状"
    plugin.stop()


def test_row_subtitle_waits_for_first_helper_report(tmp_path, monkeypatch):
    """读不到回显时说实话（「等待守护上报状态」），不拿「无匹配 · 未叠加」充数。"""
    plugin = _plugin(tmp_path, monkeypatch)
    plugin._daemon_status = {"installed": True, "running": True, "outdated": False}
    plugin._state = {"ok": False, "error": "守护尚未回报（未连接或未运行）"}
    plugin._probe_service.last = None
    assert plugin._row_subtitle() == "等待守护上报状态"
    plugin.stop()


def test_illegal_config_reports_failed_and_keeps_component_loaded(tmp_path, monkeypatch):
    """配置非法（旧 ``rules[]`` 不迁移）⇒ ``routes`` 报 failed 并给出那句改法。

    **组件不许因此消失**：``Registry.load_all`` 对 ``start()`` 抛异常的处置是把整个
    组件跳过，用户看到的是面板上少了这一行 —— 而设计要的是「当场报错并给出手改映射」
    （§6.3 第 4 步）。故 ``start`` 只记状态、不抛，也不落盘一份错期望态。
    """
    body = (
        "plugins:\n  route_guard:\n    enabled: true\n"
        "    rules:\n"
        "      - name: office\n        priority: 3\n        iface: en5\n"
        "        gateway: 10.99.1.254\n        destinations: [10.99.2.0/24]\n"
    )
    ctx = _ctx(tmp_path, body=body)
    monkeypatch.setattr(daemon, "status", lambda: {
        "installed": True, "running": True, "outdated": False,
        "version": helper.HELPER_VERSION, "expected": helper.HELPER_VERSION,
    })
    plugin = RouteGuardPlugin(ctx)
    plugin.start()                                   # 不抛：抛了组件就被框架跳过
    assert ctx.helper.declared() is None, "非法配置不许把错期望态推给 helper"
    rep = plugin._routes_report()
    assert rep.state == "failed" and "rules[]" in rep.summary, "改法要在文案里"
    assert "配置非法" in plugin._row_subtitle()
    plugin.stop()


def test_config_problem_clears_after_user_fixes_it(tmp_path, monkeypatch):
    """改好配置后那条 failed 要自己消失（缓存不能在 tick 之外过期不了）。"""
    body = "plugins:\n  route_guard:\n    enabled: true\n    state_poll: 5\n"
    ctx = _ctx(tmp_path, body=body)
    monkeypatch.setattr(daemon, "status", lambda: {
        "installed": True, "running": True, "outdated": False,
        "version": helper.HELPER_VERSION, "expected": helper.HELPER_VERSION,
    })
    plugin = RouteGuardPlugin(ctx)
    plugin.start()
    assert plugin._routes_report().state == "failed"

    def _mutate(section: dict) -> None:
        section.pop("state_poll", None)

    plugin.ctx.config.update_plugin_section("route_guard", _mutate)
    plugin.on_config_changed()                       # 手编热重载那条路径
    assert plugin._config_problem == ""
    assert plugin._routes_report().state != "failed"
    plugin.stop()


def test_panel_row_builds(tmp_path, monkeypatch, qtbot):
    plugin = _plugin(tmp_path, monkeypatch)
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    # v3：状态行由框架经 StatusRowSpec 声明式渲染（§5.3，取代 custom 工厂）
    panel = StatusPanel()
    qtbot.addWidget(panel)
    row_spec = next(s for s in gather_specs(plugin, plugin.id)
                    if isinstance(s, StatusRowSpec))
    panel.sync_status_rows([row_spec])
    row = panel._row_widgets["route_guard"][0]
    assert isinstance(row, _StatusRowView)
    assert len(row._switches) == 1, "声明式 SwitchSpec → 框架渲染一枚 Switch"
    # 徽标那一格现在装的是**场景选择器**（第 7b 步）——`_mode` 非空即已渲染，
    # 三态文案与候选列表由专门用例覆盖（本用例只验这一行能建起来）
    assert row._mode is not None
    panel.deleteLater()
    plugin.stop()


def test_no_hotkey_contract(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path, monkeypatch)
    # route_guard 不注册全局热键，v3 贡献里无 HotkeySpec
    assert not plugin.hotkeys()
    plugin.stop()


def test_set_paused_writes_yaml_and_declares(tmp_path, monkeypatch):
    """set_paused(True)：写用户 YAML paused=true，并重新声明期望态（含 paused）。"""
    plugin = _plugin(tmp_path, monkeypatch)
    assert plugin.is_paused is False
    # 反馈走设置页的行内反馈槽；这里没有设置页 ⇒ 只记日志，不再需要给弹框打桩
    plugin.set_paused(True)
    assert plugin.is_paused is True
    # 用户 YAML 已写入 paused
    import yaml

    user = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert user["plugins"]["route_guard"]["paused"] is True
    # 期望态重新声明，含 paused=true
    data = plugin.ctx.helper.declared()
    assert data["paused"] is True
    # 切回恢复
    plugin.set_paused(False)
    assert plugin.is_paused is False
    data = plugin.ctx.helper.declared()
    assert data["paused"] is False
    plugin.stop()


def test_set_routes_suppressed_writes_yaml_and_declares(tmp_path, monkeypatch):
    """面板开关（覆盖层 §4.8）：写 `rule_overrides.<场景>.routes_suppressed`，
    编译后落到该场景线格式规则的 `enabled=False`，且不能把默认其它场景（home）抹掉。"""
    plugin = _plugin(tmp_path, monkeypatch)
    data = plugin.ctx.helper.declared()
    names0 = [r["name"] for r in data["rules"]]
    assert "office" in names0 and "home" in names0
    frames_before = len(plugin.ctx.helper.sent)

    plugin.set_routes_suppressed("office", True)
    user = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    ov = user["plugins"]["route_guard"]["rule_overrides"]
    assert ov["office"]["routes_suppressed"] is True
    # 覆盖层只剩一位（force_apply / manual_override 都已下线）
    assert list(ov["office"]) == ["routes_suppressed"]
    # 落到线格式：helper 眼里仍是老样子的一位 enabled
    data = plugin.ctx.helper.declared()
    by_name = {r["name"]: r for r in data["rules"]}
    assert "home" in by_name, "覆盖 office 不应丢失默认 home 规则"
    assert by_name["office"]["enabled"] is False
    assert by_name["home"]["enabled"] is True
    assert "force_apply" not in by_name["office"], "force_apply 通道已下线"
    # 期望态一变就**声明出去**（会话上的一帧）—— 不必等下一个节拍
    assert len(plugin.ctx.helper.sent) > frames_before

    # 恢复按配置叠加
    plugin.set_routes_suppressed("office", False)
    user = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert user["plugins"]["route_guard"]["rule_overrides"]["office"][
        "routes_suppressed"] is False, "恢复应显式写 False（非省略）"
    data = plugin.ctx.helper.declared()
    by_name = {r["name"]: r for r in data["rules"]}
    assert by_name["office"]["enabled"] is True
    assert "home" in by_name
    plugin.stop()


def test_switch_reflects_intent_not_result(tmp_path, monkeypatch):
    """开关显示的是**意图**（要不要叠），不是 helper 回显的**结果**（applied）。

    旧写法读 applied：开关会随下一次探活自己跳，用户改的是意图、看到的却是结果。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    plugin._state = {"ok": True, "rules": [
        {"name": "office", "trigger_matched": True, "applied": True,
         "destinations": [{"destination": "10.99.2.0/24", "ok": True}]},
    ]}
    assert plugin._switch_checked() is True          # 按配置 ⇒ 开
    plugin.set_routes_suppressed("office", True)
    assert plugin._switch_checked() is False         # 本次不叠加 ⇒ 关
    # 结果还没跟上（helper 尚未收敛）也不能把开关显示成开
    assert plugin._state["rules"][0]["applied"] is True
    plugin.stop()


def test_suppression_note_distinguishes_this_time_from_config(tmp_path, monkeypatch):
    """§4.8：必须一眼区分「本次已撤回」和「配置已关闭」——开关上两者长得一样。"""
    plugin = _plugin(tmp_path, monkeypatch)
    plugin._state = {"ok": True, "rules": [
        {"name": "office", "trigger_matched": True, "applied": False,
         "destinations": [{"destination": "10.99.2.0/24", "ok": False}]},
    ]}
    plugin.set_routes_suppressed("office", True)
    assert "本次已撤回" in plugin._suppression_note()
    assert "配置" not in plugin._suppression_note()

    # 配置层关闭（routes.enabled=false）是另一件事，文案必须说成「按配置」
    plugin.set_routes_suppressed("office", False)
    ctx_cfg = plugin.plugin_config()
    profiles = [dict(p) for p in ctx_cfg["profiles"]]
    for p in profiles:
        if p["name"] == "office":
            p["routes"] = {**p["routes"], "enabled": False}

    def _mutate(section: dict) -> None:
        section["profiles"] = profiles

    plugin.ctx.config.update_plugin_section("route_guard", _mutate)
    note = plugin._suppression_note()
    assert "按配置不叠加" in note and "本次" not in note
    plugin.stop()


def test_suppressions_reaped_when_scene_changes(tmp_path, monkeypatch):
    """覆盖是「本次」的：换了场景就清掉，不然它就和配置层的 routes.enabled 没区别。"""
    plugin = _plugin(tmp_path, monkeypatch)
    plugin._state = {"ok": True, "rules": [
        {"name": "office", "trigger_matched": True, "applied": False, "destinations": []},
    ]}
    plugin.set_routes_suppressed("office", True)
    assert plugin._scene_suppressed("office") is True

    # helper 回显换成了 home ⇒ tick 应当抹掉上一次的覆盖
    _push_route(plugin, {"ok": True, "rules": [
        {"name": "home", "trigger_matched": True, "applied": True, "destinations": []},
    ]})
    plugin.tick()
    user = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    section = user["plugins"]["route_guard"]
    assert "rule_overrides" not in section or not section["rule_overrides"]
    assert plugin._scene_suppressed("office") is False
    plugin.stop()


def test_panel_widget_shows_switch(tmp_path, monkeypatch, qtbot):
    plugin = _plugin(tmp_path, monkeypatch)
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    # 守护状态非过期（避免开发机真实 daemon 状态干扰面板开关判定）
    monkeypatch.setattr(
        "barkit.capabilities.helper_client.status",
        lambda: {"installed": True, "running": True, "outdated": False},
    )
    # 模拟当前主路由已生效（applied=True 既表示自动命中也表示强制生效）
    plugin._state = {
        "ok": True, "checked_at": 1700000000, "error": None, "paused": False,
        "rules": [{
            "name": "office", "matched": True, "trigger_matched": True,
            "applied": True, "enabled": True, "iface": "en5",
            "gateway": "10.99.1.254",
            "destinations": [{"destination": "10.99.2.0/24", "ok": True}],
        }],
    }
    panel = StatusPanel()
    qtbot.addWidget(panel)
    row_spec = next(s for s in gather_specs(plugin, plugin.id)
                    if isinstance(s, StatusRowSpec))
    panel.sync_status_rows([row_spec])
    row = panel._row_widgets["route_guard"][0]
    sw = row._switches[0][0]
    # 有启用规则 → 开关可用，且路由实际生效（打开）
    assert sw.isEnabled() is True
    assert sw.isChecked() is True
    # 徽标写的是**生效场景名**（§4.6 微调：徽标不再写「自动 / 手动」——那是默认态，
    # 放进带 accent 的槽位就成了开关的同义反复）
    assert row._mode.text() == "office"
    panel.deleteLater()
    plugin.stop()


def test_panel_switch_stays_usable_when_off_network(tmp_path, monkeypatch, qtbot):
    """§4.8 把这条门控反过来：不在某场景的网络里时，开关**照常可用**。

    旧写法是「没命中 ⇒ 开关禁用」——用户站在别处没法提前说一句「下次进这个网别加」，
    而且那时候开关看着能点、点了没反应（正是当初那个 bug 的真因）。现在的判据改成：
    **只要还知道该作用到哪个场景**（helper 回显里有、或配置里有），就允许操作。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "barkit.capabilities.helper_client.status",
        lambda: {"installed": True, "running": True, "outdated": False},
    )
    # 规则存在但触发未命中（不在该 Wi-Fi/网关）：场景仍判定得出 ⇒ 开关保持可用
    plugin._state = _echo([_rule(matched=False, applied=False)])
    panel = StatusPanel()
    qtbot.addWidget(panel)
    specs = _machine_specs(plugin)
    panel.set_components([], _StubContrib(list(specs.values())))
    panel.sync_status_rows([next(
        s for s in gather_specs(plugin, plugin.id) if isinstance(s, StatusRowSpec))])
    row = panel._row_widgets["route_guard"][0]
    assert plugin._current_scene() == "office"
    assert row._switches[0][0].isEnabled() is True
    # 显示态读意图（要不要叠）而非结果：没命中也显示「开」，因为没有覆盖
    assert row._switches[0][0].isChecked() is True
    # 副标题如实说没生效（§5.3：这笔账归结果，不归开关）——「无匹配 · 未叠加」是
    # 两台机（profile 与 routes）各自的结论，拼在同一句里（设计 §3.3）；装置健康不再
    # 占一格（2026-09-18 用户裁定）。
    # 比对 tooltip 而不是 label 文本：离屏布局宽度不定，ElideLabel 会截断，
    # 而完整那一句就挂在 tooltip 上（``_StatusRowView.set_subtitle``）。
    _push_all(panel, plugin, specs)
    assert row._sub.toolTip() == "无匹配 · 未叠加", row._sub.toolTip()
    panel.deleteLater()
    plugin.stop()


def test_detail_status_points_to_the_settings_page_when_daemon_is_stale(
        tmp_path, monkeypatch):
    """配置窗顶上那条状态：守护过期 / 未装时**先说问题并指路**（2026-09-18 用户实测）。

    用户在面板点整行进来的是**场景配置窗**，而装守护的按钮在设置窗 —— 状态条不指出
    这一点，他会在这一页里翻（原话：「过期了只能通过网络切换的设置页面安装，点击组件
    弹出的是场景配置」）。正常态照旧报「自动化 / 当前生效」，故障不占位、正常不啰嗦。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    plugin._daemon_status = {
        "installed": True, "running": True, "outdated": True,
        "version": "2.12.0", "expected": "2.13.0",
    }
    text = plugin.detail_status_text()
    assert "过期" in text and "2.12.0" in text and "2.13.0" in text
    assert "重装守护" in text and "设置窗" in text, text

    plugin._daemon_status = {"installed": False, "running": False, "outdated": False}
    text = plugin.detail_status_text()
    assert "尚未安装" in text and "安装守护" in text and "设置窗" in text, text

    plugin._daemon_status = {"installed": True, "running": True, "outdated": False}
    assert plugin.detail_status_text().startswith("自动化：")
    plugin.stop()


def test_panel_widget_outdated_makes_row_clickable(tmp_path, monkeypatch, qtbot):
    """守护过期时：live 副标题提示重装 + 整行可点（根治「路由异常」却无从下手）。

    §5.3：整行可点由**详情页入口**声明驱动（声明了 ``DetailEntrySpec`` ⇒ 框架合成
    「打开详情页」，组件不再自己接一个 `on_click` 去做重装）；live 副标题走**行级回调**
    （决议 D24）——守护过期由 daemon 机领句（设计 §3.3「异常项优先」），不再是代表态
    那台的 ``summary``。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    # 守护过期：直接置 _daemon_status（三台机读此缓存，而非每次调 daemon.status，
    # 避免真实守护状态干扰判定）。字段形状对齐真实 status()：version 落后 expected。
    plugin._daemon_status = {
        "installed": True, "running": True,
        "version": "1.0",
        "expected": daemon.expected_version(),
        "outdated": True,
    }
    panel = StatusPanel()
    qtbot.addWidget(panel)
    specs = _machine_specs(plugin)
    panel.set_components([], _StubContrib(list(specs.values())))
    # 详情页入口由框架注入（`AppShell` 取 `ContributionRegistry.detail_entries()` 后下发）
    panel.set_detail_entries(_detail_entries(plugin))
    panel.sync_status_rows([next(
        s for s in gather_specs(plugin, plugin.id) if isinstance(s, StatusRowSpec))])
    row = panel._row_widgets["route_guard"][0]
    # 声明了详情页 ⇒ 整行可点（点开的是场景配置窗体，不再直接触发重装）
    assert row._clickable is True
    # 守护过期 → 副标题提示重装**并指出在哪装**（完整那句在 tooltip，见上一条用例），
    # 行角标染 ERR。「在设置页」这四个字是 2026-09-18 用户实测补的：只说「需重装」时
    # 他找不到入口（面板整行点开的是场景配置窗，不是装守护的地方）。
    _push_all(panel, plugin, specs)
    assert row._sub.toolTip() == plugin._row_subtitle()
    assert "守护过期" in row._sub.toolTip() and "在设置页重装" in row._sub.toolTip()
    assert row._badge_level == "err", "守护过期是 ERR 取值，颜色归代表态（决议 D13）"
    panel.deleteLater()
    plugin.stop()


def test_install_uninstall_actions_publish(tmp_path, monkeypatch, qtbot):
    """安装直接执行；卸载**先进行内二次确认**，点下确认才动守护（零模态 —— 见 ui_kit.FeedbackBar）。"""
    events = EventBus()
    plugin = _plugin(tmp_path, monkeypatch, events=events)
    received = []
    # 三台机各有一条 topic（原先是保留键 main 一条）
    for key in ("daemon", "routes", "profile"):
        events.subscribe(plugin.status_topic(key), received.append)
    # install 现签名为 (sources, missing)：桩要能吃下两个参数（批次 4 · S4）
    monkeypatch.setattr(daemon, "install", lambda *a, **k: True)
    uninstalled = []
    monkeypatch.setattr(daemon, "uninstall", lambda: uninstalled.append(1) or True)

    plugin.install_action()
    qtbot.waitUntil(lambda: len(received) >= 3, timeout=2000)
    assert isinstance(received[-1], StateReport)

    # 装置运维卡在**页尾框架区**（2026-09-28 起框架按声明注入），故确认落在那一区的槽
    from barkit.shell.ui_kit import FeedbackBar

    page = plugin._build_settings_page()
    qtbot.addWidget(page)                      # 注意：qtbot 只存 weakref，靠本函数的 page 保住
    zone_fb = FeedbackBar(page)                # 模拟框架区那条槽（真实接线归框架区的用例）
    plugin.attach_helper_ops(page, zone_fb, lambda: None)

    plugin.uninstall_action()                  # 只进确认态
    assert zone_fb.is_pending(), "卸载必须先经过行内二次确认"
    assert uninstalled == [], "未确认前不得动守护"
    assert "确定卸载" in zone_fb.text()

    zone_fb.accept()                           # 等价于用户点了「卸载」
    assert uninstalled == [1], "确认后才真正卸载"
    qtbot.waitUntil(lambda: len(received) >= 6, timeout=2000)   # 又一组三台机
    plugin.stop()


def test_actions_report_into_the_ops_zone_feedback_bar(tmp_path, monkeypatch, qtbot):
    """动作结果不再弹框，而是落进**页尾框架区**那条槽（装置运维卡所在的那一区）。

    槽随卡一起从页内挪到了框架区（2026-09-28）：触发按钮在哪一区，回执就落哪一区。
    """
    from barkit.shell.ui_kit import FeedbackBar

    plugin = _plugin(tmp_path, monkeypatch)
    page = plugin._build_settings_page()
    qtbot.addWidget(page)
    zone_fb = FeedbackBar(page)
    plugin.attach_helper_ops(page, zone_fb, lambda: None)

    monkeypatch.setattr(daemon, "install", lambda *a, **k: True)
    assert plugin.install_action() is True, "装成了必须能被调用方读到（按钮据此自报已安装）"
    assert "已安装" in zone_fb.text()

    monkeypatch.setattr(daemon, "install", lambda *a, **k: False)
    assert plugin.install_action() is False, "没装成不许报成功"
    assert "安装失败" in zone_fb.text()
    plugin.stop()


def test_note_lands_in_the_neutral_hint_tone(tmp_path, monkeypatch, qtbot):
    """中性结果（``_note``）走**告知**档，不走成功档。

    「交还前得先去设置页勾网卡」这类话若被画成绿色的「成了」，用户就不会去动手 ——
    而它在面板侧恰恰是**唯一**的行动指引。判据落在**底色令牌**上：成功档是
    ``green_soft``，告知档是 ``surface3``。
    """
    from barkit.shell import styles
    from barkit.shell.design.tokens import TOKENS

    plugin = _plugin(tmp_path, monkeypatch)
    page = plugin._build_settings_page()
    qtbot.addWidget(page)
    fb = plugin._feedback
    assert plugin._note("交还前得先去设置页勾网卡") is None, "设置页开着 ⇒ 两条出口只走一条"
    assert "勾网卡" in fb.text()
    tk = TOKENS[styles.current_theme()]
    css = fb._box.styleSheet().lower()
    assert tk["surface3"].lower() in css, f"中性告知该用中性浅底：{css}"
    assert tk["green_soft"].lower() not in css, f"告知档不该冒充成功档：{css}"
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
    new = plugin._build_settings_page()        # 重建（旧页此时尚未销毁）
    qtbot.addWidget(new)
    new_fb = plugin._feedback
    assert new_fb is not old_fb

    plugin._forget_feedback(old_fb)            # 旧页销毁回调
    assert plugin._feedback is new_fb, "旧槽位的销毁回调不应摘掉新槽位"
    plugin._forget_feedback(new_fb)
    assert plugin._feedback is None
    plugin.stop()


# ──────────────────────────────────────────────
# 配置窗体（设计 §4.3 / §5.2-④ —— §6.3 第 7 步）
#
# 落点：UI 在 `plugins/route_guard/settings.py`（§7.2），配置 IO 仍在 plugin 里
# （`editable_profiles` / `save_profiles`）。这批用例只经那两个门面。
# ──────────────────────────────────────────────

def _user_profiles(tmp_path) -> list[dict]:
    """用户 YAML 里现在写着的场景（没写过该键 → 空表）。"""
    raw = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8")) or {}
    section = ((raw.get("plugins") or {}).get("route_guard")) or {}
    return list(section.get("profiles") or [])


def _editor(plugin, qtbot):
    """开一次配置窗体（等价于用户点面板整行 → 框架按 DetailEntrySpec 建页）。"""
    page = plugin.detail_entry().build()
    qtbot.addWidget(page)
    return page


def _detail_entries(plugin) -> list:
    """该插件的详情页入口，**经注册中心收一遍**（`plugin_id` 由收集期填上）。

    直接拿 `plugin.detail_entry()` 喂面板是不对的：那一份还没过收集期，`plugin_id` 是空的，
    面板按 id 找入口自然找不到（本用例先踩过这个）。
    """
    from barkit.core.contributions import ContributionRegistry

    registry = ContributionRegistry([plugin])
    assert registry.rejected == [], registry.rejected
    return registry.detail_entries()


def _settings_buttons(page) -> list[str]:
    from PySide6.QtWidgets import QPushButton

    return [b.text() for b in page.findChildren(QPushButton)]


def test_detail_entry_is_a_window_config_form(tmp_path, monkeypatch):
    """整行点击的落点 = 详情页「场景配置窗体」，形态必须是 window（§5.2-④）。

    这条同时钉住设计 §5.1 末尾点名的那处现存违规已修：整行不再被接到「重装 / 诊断」上。

    「整行点击」这一半 2026-09-22 起不必再声明 ``trigger``（字段已删）—— 声明了入口，
    行点击与快捷区域按钮**同时**接管（同一个 ``detailRequested``），入口可见性归用户层。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    entry = plugin.detail_entry()
    assert entry is not None
    assert entry.presentation == "window", "左列表右表单装不进面板那个 380px 的浮层卡"
    assert entry.build is not None

    row = plugin.row_specs()[0]
    assert row.on_click is None, "整行点击归框架合成的「打开详情页」，组件不得自己接一个动作"

    # 框架侧的判据：声明了可构建的详情页 ⇒ 这一行才可点（`DetailHost.available`）
    from barkit.core.contributions import ContributionRegistry
    from barkit.shell.detail import DetailHost

    registry = ContributionRegistry([plugin])
    assert registry.rejected == [], registry.rejected
    assert DetailHost(registry).available(plugin.id), "声明了详情页却不可点 = 用户点了没反应"
    plugin.stop()


def test_config_form_has_no_second_scroll_area(tmp_path, monkeypatch, qtbot):
    """整页**只许有一条竖滚动条**：路由清单不再自建内层滚动区（2026-10-06 用户上报）。

    原来的写法是把逐条行塞进一个 `QScrollArea`、再让右栏吃掉剩余高度。本窗体是详情页
    内容，而框架已经把整页包了一层滚动区（`shell/detail.py::_scrolled`）⇒ 窗一变矮就是
    **两条竖滚动条套着**，滚轮只驱动鼠标底下那一条，用户看不出上面还有内容。

    判据三段，缺一不可：
    ① 内容里一个 `QScrollArea` 都不许有（框架那一条是它的**祖先**，不是子孙）；
    ② 矮窗时全窗**只有**框架那一条滚动区的竖条是可见的（这是用户看到的现象本身）；
    ③ 行仍保持自然高度、那一条的 `maximum() > 0` —— 只删内层不管外层就是「滚不动」，
       而把外层也撤掉是另一条老症状（内容被压扁）。

    实测数字（窗高 `WINDOW_MIN_HEIGHT` ＋ 13 条目标）：旧写法**两条**竖条同时可见，
    内层那条 `maximum=380` 却只有 112px 高（清单被压成一个小窗口，与
    `notify_guard/settings_list.py` 记下的 88px 同一根因）；现在只剩框架那一条
    `maximum=735`，内容按自然高度 989px 长。
    """
    from PySide6.QtWidgets import QScrollArea

    from barkit.shell.detail import DETAIL_PRESENTATION_WINDOW, DetailForm

    plugin = _plugin(tmp_path, monkeypatch)
    page = plugin.detail_entry().build()
    form = DetailForm("网络切换", page, presentation=DETAIL_PRESENTATION_WINDOW)
    qtbot.addWidget(form)
    form.show()
    qtbot.waitExposed(form)

    assert page.findChildren(QScrollArea) == [], "路由清单不许再套一层滚动区"

    # 加够行数让内容高过窗口下限：否则「要不要滚」这一半测不到
    for i in range(10):
        page._on_add_item()
        page._item_rows[-1][1].setText(f"10.9.{i}.0/24")
    qtbot.wait(1)
    # 只量**逐条行里那两格**（常显）：网卡那一段会随模式整块藏起来，藏起来的控件量不得
    wants = {w: w.sizeHint().height() for pair in page._item_rows for w in pair[1:3]}

    form.resize(form.width(), DetailForm.WINDOW_MIN_HEIGHT)
    qtbot.wait(1)

    for w, want in wants.items():
        assert w.height() == want, f"控件被压扁：{w.height()} < {want}"
    assert page.height() >= page.sizeHint().height()
    # 只数 QScrollArea：场景列表那个 QListWidget 自带的竖条不算「第二层」（它就是一张列表）
    shown = [a for a in form.findChildren(QScrollArea)
             if a.verticalScrollBar().isVisible()]
    extra = [a for a in shown if a is not form._content_scroll]
    assert extra == [], f"整窗只许有框架那一条竖滚动条，多出来 {len(extra)} 条"
    assert form._content_scroll.verticalScrollBar().maximum() > 0, "内容顶出窗口 ⇒ 外层那一条要能滚"
    plugin.stop()


def test_config_form_starts_from_effective_scenes(tmp_path, monkeypatch, qtbot):
    """窗体列的是**此刻在跑的那份**（默认 ＋ 用户差异已合并），不是只有用户差异段。

    旧窗体只读用户段：新装用户打开看到一张**空表**，而面板上明明写着命中 `office`；
    再加一个场景，整份默认列表就被顶掉了（`profiles` 是整表覆盖语义）。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    assert _user_profiles(tmp_path) == [], "前提：用户还没写过 profiles"

    page = _editor(plugin, qtbot)
    assert [page._list.item(i).text() for i in range(page._list.count())] == ["office", "home"]
    assert page._editing == 0, "默认选中第一条"
    assert page._name.text() == "office"
    assert page._iface.text() == "en5"
    assert page._gw.text() == "10.99.1.254"
    assert page._routes_on.isChecked() is True
    assert "当前生效" in page._status.text(), "顶部只读状态条要说「现在谁在管」"
    plugin.stop()


def test_config_form_edit_waits_for_the_save_key(tmp_path, monkeypatch, qtbot):
    """改字段**只攒在窗体里**，盘上要等一次「保存」（2026-09-18 用户裁定，改成手动保存）。

    这条是本批的要害：以前「改一格写一次盘」，于是「界面改了、盘上没动」在界面上**看不
    出来**；现在这两件事被分开 Assert —— 攒的时候盘上必须一个字不动，点保存之后才写。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    page = _editor(plugin, qtbot)

    page._name.setText("office2")
    page._name.editingFinished.emit()
    assert page.dirty() is True, "改过就该是「有没保存的改动」"
    assert _user_profiles(tmp_path) == [], "还没点保存 ⇒ 盘上一个字都不动"

    ok, text = page.save()
    assert ok and text == "已保存。"
    assert page.dirty() is False, "存下去之后就不再是「有没保存的改动」"

    saved = _user_profiles(tmp_path)
    assert [p["name"] for p in saved] == ["office2", "home"]
    assert saved[0]["ifaces"] == ["en5"] and saved[0]["match"] == {"gateway": "10.99.1.254"}

    # 期望态跟着重建：列表顺序即优先级（priority = 下标）
    data = plugin.ctx.helper.declared()
    assert [r["name"] for r in data["rules"]] == ["office2", "home"]
    plugin.stop()


def test_config_form_is_clean_again_when_the_value_comes_back(tmp_path, monkeypatch, qtbot):
    """改回原样 ⇒ 自动算干净。

    判据是「与上次保存那份快照整体比」，不是在每条编辑路径上记得「标记脏」——
    那种写法漏一处就永久脏，而用户看到的是「明明什么都没改，关窗却一直问我要不要保存」。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    page = _editor(plugin, qtbot)

    page._name.setText("office2")
    page._name.editingFinished.emit()
    assert page.dirty() is True

    page._name.setText("office")
    page._name.editingFinished.emit()
    assert page.dirty() is False, "值改回去了，就没有「没保存的改动」"
    plugin.stop()


def test_config_form_panes_are_draggable(tmp_path, monkeypatch, qtbot):
    """左列表 / 右表单之间是**可拖动分隔条**（2026-09-18 用户要求）。

    原先写死 2:3 —— 那个比例取决于「场景名与路由地址有多长」，而那是**用户当场才知道**
    的事：他这会儿在改哪一格，哪一格就该宽。锁死的代价是改一条长地址得横向滚着改。
    拖到 0 宽不行：两栏各自都没有别的入口（0 宽 = 一半功能看不见了）。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QListWidget, QLineEdit, QSplitter

    plugin = _plugin(tmp_path, monkeypatch)
    page = _editor(plugin, qtbot)
    split = page.findChild(QSplitter)
    assert split is not None, "左右两栏必须挂在分隔条上（否则拖不动）"
    assert split.orientation() == Qt.Orientation.Horizontal
    assert split.count() == 2, "只有左右两栏"
    assert not split.isCollapsible(0) and not split.isCollapsible(1), \
        "拖到 0 宽等于把一半功能藏起来（两栏都没有别的入口）"
    assert all(s > 0 for s in split.sizes()), split.sizes()
    # 拖动只换容器，控件的归属不能变：列表在左、表单在右
    assert page._list in split.widget(0).findChildren(QListWidget)
    assert page._name in split.widget(1).findChildren(QLineEdit)
    plugin.stop()


def test_config_form_keeps_keys_it_does_not_manage(tmp_path, monkeypatch, qtbot):
    """编辑器只改它显示的东西：手写的 `manual_only` / `match.subnet` / `net.pin` 原样保留。

    它们今天没有语义（场景选择器与 helper 网卡能力还没做），所以**不进表单**；但也不能
    「保存一次就掉一半」——那等于组件偷偷改了用户验收过的那份 YAML。
    """
    plugin = _plugin(
        tmp_path, monkeypatch,
        body=(
            "plugins:\n  route_guard:\n    enabled: true\n    helper_poll: 30\n"
            "    profiles:\n"
            "      - name: office\n"
            "        ifaces: [en5]\n"
            "        manual_only: true\n"
            "        match: {subnet: 10.99.1.0/24}\n"
            "        net: {mode: dhcp, pin: {ip: 10.99.1.88, mask: 255.255.255.0}}\n"
            "        routes:\n"
            "          enabled: true\n"
            "          items: [{destination: 10.99.2.0/24, via: 10.99.1.254}]\n"
        ),
    )
    page = _editor(plugin, qtbot)
    page._name.setText("office-renamed")
    page._name.editingFinished.emit()
    assert page.save()[0] is True

    saved = _user_profiles(tmp_path)          # 直接读盘，不是读内存
    assert saved[0]["name"] == "office-renamed"
    assert saved[0]["manual_only"] is True
    assert saved[0]["match"] == {"subnet": "10.99.1.0/24"}, "网关清空后 match 里别的键也要在"
    assert saved[0]["net"] == {
        "mode": "dhcp", "pin": {"ip": "10.99.1.88", "mask": "255.255.255.0"},
    }
    plugin.stop()


def test_config_form_refuses_illegal_save_and_keeps_the_draft(tmp_path, monkeypatch, qtbot):
    """非法时**一条都不写**，但**不回滚**：攒着的就是用户刚打的字，收走它等于毁掉草稿。"""
    plugin = _plugin(tmp_path, monkeypatch)
    page = _editor(plugin, qtbot)

    page._iface.clear()                       # 网卡是硬要求（validate_profile）
    page._iface.editingFinished.emit()
    assert page.dirty() is True
    ok, msg = page.save()
    assert ok is False and msg, "失败必须给出一句人话"
    assert "ifaces" in msg, "原因来自 rules 的校验（一次都不重写）"
    assert _user_profiles(tmp_path) == [], "非法配置一个字都不该落盘"
    assert page._iface.text() == "", "改动留在窗体里（旧口径「失败即回滚」会连草稿一起丢掉）"

    # 名字重复同理（校验发生在写盘之前）
    page._iface.setText("en5")
    page._iface.editingFinished.emit()
    page._name.setText("home")
    page._name.editingFinished.emit()
    ok, msg = page.save()
    assert ok is False and "重复" in msg
    assert _user_profiles(tmp_path) == []

    # 改回合法 ⇒ 同一颗「保存」键就能存下去（不需要别的动作把状态救回来）
    page._name.setText("office2")
    page._name.editingFinished.emit()
    assert page.save()[0] is True
    assert [p["name"] for p in _user_profiles(tmp_path)] == ["office2", "home"]
    plugin.stop()


def test_config_form_new_scene_blocks_the_save_until_nic_filled(tmp_path, monkeypatch, qtbot):
    """新增的场景先是**半成品**：不能保存（半份场景会把整段配置带崩），先指路到那一格。"""
    plugin = _plugin(tmp_path, monkeypatch)
    page = _editor(plugin, qtbot)

    page._on_add()
    assert page._list.count() == 3
    assert page._list.item(2).text() == "新场景（还没填完）"
    assert _user_profiles(tmp_path) == [], "半成品不进配置"

    ok, msg = page.save()
    assert ok is False and "监控网卡" in msg, "拒绝时要指到具体哪一格"
    assert _user_profiles(tmp_path) == [], "整份都不写（一半写一半不写会让人以为全存了）"
    assert page._list.item(2).text() == "新场景（还没填完）"
    assert page._editing == 2 and page._name.text() == "新场景"

    page._iface.setText("en6")                # 填上网卡 = 它合法了
    page._iface.editingFinished.emit()
    assert page.save()[0] is True
    saved = _user_profiles(tmp_path)
    assert [p["name"] for p in saved] == ["office", "home", "新场景"]
    assert saved[2]["ifaces"] == ["en6"]
    assert page._list.item(2).text() == "新场景", "（还没填完）标记随之消失"
    plugin.stop()


def test_config_form_delete_asks_inline_then_waits_for_save(tmp_path, monkeypatch, qtbot):
    """删除走**行内**二次确认（零模态框），文案要说清「路由不会自动撤掉」这条代价。"""
    plugin = _plugin(
        tmp_path, monkeypatch,
        body=(
            "plugins:\n  route_guard:\n    enabled: true\n"
            "    profiles:\n"
            "      - name: a\n        ifaces: [en5]\n"
            "        routes: {enabled: true, items: [{destination: 10.99.2.0/24, via: 1.1.1.1}]}\n"
            "      - name: b\n        ifaces: [en6]\n"
            "        routes: {enabled: true, items: [{destination: 10.2.0.0/16, via: 2.2.2.2}]}\n"
        ),
    )
    page = _editor(plugin, qtbot)

    page._on_delete()
    assert page._fb.is_pending(), "删除必须先确认"
    assert "不会" in page._fb.text(), "helper 只对意图里现存的规则做增删 —— 这条代价要写出来"
    assert [p["name"] for p in _user_profiles(tmp_path)] == ["a", "b"], "未确认前不得写盘"

    page._fb.accept()
    assert page._list.count() == 1 and page._editing == 0, "确认后先从表里摘掉"
    assert [p["name"] for p in _user_profiles(tmp_path)] == ["a", "b"], "摘掉 ≠ 写盘：还要点保存"
    assert page.save()[0] is True
    assert [p["name"] for p in _user_profiles(tmp_path)] == ["b"]
    plugin.stop()


def test_config_form_reorder_rewrites_priority(tmp_path, monkeypatch, qtbot):
    """拖序 = 改配置（§2.1：列表顺序即优先级），写下去仍然等「保存」。"""
    plugin = _plugin(tmp_path, monkeypatch)
    page = _editor(plugin, qtbot)
    assert [p["name"] for p in plugin.editable_profiles()] == ["office", "home"]

    page._reorder(0, 1)
    assert _user_profiles(tmp_path) == [], "还没保存"
    assert page.save()[0] is True
    assert [p["name"] for p in _user_profiles(tmp_path)] == ["home", "office"]
    data = plugin.ctx.helper.declared()
    assert [r["name"] for r in data["rules"]] == ["home", "office"]
    # priority 随列表下标走；第 1 条的 0 不会出现在线格式里（`intent` 只写非 0 值）
    assert [r.get("priority", 0) for r in data["rules"]] == [0, 1]
    assert page._editing == 1, "调序后仍停在同一条上"

    page._reorder(1, 0)                       # 拖回原位 = 回到出厂顺序
    assert page.save()[0] is True
    assert _user_profiles(tmp_path) == [], \
        "与出厂顺序逐字相同时不留 profiles 键（写侧口径：等于声明默认值即删键）"
    assert [r["name"] for r in plugin.ctx.helper.declared()["rules"]] == ["office", "home"], \
        "删键不等于没写：生效顺序仍是出厂那一份"
    plugin.stop()


def test_config_form_edits_route_items(tmp_path, monkeypatch, qtbot):
    """逐条路由：改一行 / 加一行 / 删一行都先进内存，保存时一起写。"""
    plugin = _plugin(tmp_path, monkeypatch)
    page = _editor(plugin, qtbot)
    assert len(page._item_rows) == 3, "office 默认带 3 条目标"

    # 改第 1 条：下一跳留空 = 不写 via，线格式回落 match.gateway
    _row, dest, via, _sw = page._item_rows[0]
    dest.setText("10.9.9.0/24")
    via.clear()
    dest.editingFinished.emit()
    assert page.dirty() is True

    # 加一条
    page._on_add_item()
    page._item_rows[-1][1].setText("192.168.77.0/24")
    page._item_rows[-1][1].editingFinished.emit()

    # 删一条
    page._remove_item_row(page._item_rows[-1][0])

    assert _user_profiles(tmp_path) == [], "一路都是攒着"
    assert page.save()[0] is True
    items = _user_profiles(tmp_path)[0]["routes"]["items"]
    assert items[0] == {"destination": "10.9.9.0/24"}, "改的那条写下去了"
    assert len(items) == 3, "加一条又删一条 ⇒ 仍是 3 条"
    plugin.stop()


def test_config_form_item_switch_round_trips(tmp_path, monkeypatch, qtbot):
    """逐条开关（v7）：关掉的行写 ``enabled: False``，勾着的不写键；重开窗体仍记得。

    三个判据一条都不能少：① 攒着不写盘；② 写侧口径「等于默认值即删键」（勾着的不留
    ``enabled: true``）；③ 装载侧认得回来 —— 否则「保存一次就把关掉的又打开」，
    而这一条恰好是最难在真机上看出来的那种（界面照旧，行为反了）。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    page = _editor(plugin, qtbot)
    assert [sw.isChecked() for _r, _d, _v, sw in page._item_rows] == [True] * 3

    page._on_item_toggled(page._item_rows[1][3])      # 关掉第 2 条
    assert page._item_rows[1][3].isChecked() is False
    assert page.dirty() is True
    assert _user_profiles(tmp_path) == [], "还没保存"

    assert page.save()[0] is True
    items = _user_profiles(tmp_path)[0]["routes"]["items"]
    assert [it.get("enabled", True) for it in items] == [True, False, True]
    assert "enabled" not in items[0] and "enabled" not in items[2], \
        "勾着的不留键（等于声明默认值即删键）"

    # 线格式：停用的那条**仍在** destinations 里（helper 只对意图里现存的目标增删）。
    # 这里不判 off_destinations —— 本拍没命中 ⇒ ``apply=False`` ⇒ helper 逐目标全撤，
    # 那一栏不递（``test_build_intent_passes_off_destinations_only_when_applying`` 管它）。
    rule = plugin.ctx.helper.declared()["rules"][0]
    assert rule["enabled"] is True
    assert rule["destinations"] == [it["destination"] for it in items]

    # 重开一次窗体：关掉的仍是关着的
    page2 = _editor(plugin, qtbot)
    assert [sw.isChecked() for _r, _d, _v, sw in page2._item_rows] == [True, False, True]
    plugin.stop()


def test_config_form_scene_switch_only_collects(tmp_path, monkeypatch, qtbot):
    """换场景不再是「一次保存点」：只把上一条收进内存，盘上不动。

    旧口径下换场景会顺手写盘、写不成还把选中行退回去；手动保存之后这两件事都不需要了
    —— 写盘只发生在点「保存」那一刻，换场景就只是换一格看。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    page = _editor(plugin, qtbot)

    page._name.setText("office2")             # 还没触发 editingFinished
    page._list.setCurrentRow(1)               # 用户直接点第二条
    assert page._editing == 1 and page._name.text() == "home"
    assert [p["name"] for p in page._profiles][0] == "office2", "上一条的值已收进内存"
    assert _user_profiles(tmp_path) == [], "但盘上还没动"

    page._iface.clear()                       # 把第 2 条改坏
    page._list.setCurrentRow(0)
    assert page._editing == 0 and page._name.text() == "office2", "换场景不再被失败拦住"
    assert page.save()[0] is False, "存的时候才拦（坏的那条在表里）"
    plugin.stop()


def test_config_form_route_switch_is_scene_level(tmp_path, monkeypatch, qtbot):
    """配置项级开关（§4.7 一）：整套关掉，但场景与目标都留着。"""
    plugin = _plugin(tmp_path, monkeypatch)
    page = _editor(plugin, qtbot)

    page._routes_on.setChecked(False)          # toggled → 攒进内存
    assert page._list.item(0).text() == "office（不加路由）"
    assert page.dirty() is True
    assert page.save()[0] is True
    saved = _user_profiles(tmp_path)[0]
    assert saved["routes"]["enabled"] is False
    assert len(saved["routes"]["items"]) == 3, "关掉 ≠ 删掉"

    data = plugin.ctx.helper.declared()
    office = next(r for r in data["rules"] if r["name"] == "office")
    assert office["enabled"] is False, "关掉的场景仍要保留规则（否则它已加的路由没人撤）"
    plugin.stop()


# ──────────────────────────────────────────────
# 「照抄租约」（设计 §2.5 的补充 —— 2026-09-18 用户裁定）
#
# 用户问的那件事：「固化租约里网关 / DNS 留空代表不固化，要固化 DHCP 的结果怎么配？」
# 答案是**两个显式布尔键**（`gateway_from_lease` / `dns_from_lease`），而不是给「留空」
# 换个意思 —— 换意思会静默改掉已经写着空值的那些场景。
# ──────────────────────────────────────────────

def _set_net_mode(page, value: str) -> None:
    """按**用户的路径**切网卡模式：改下拉 ⇒ 发 ``selected`` ⇒ ``_on_net_mode``。

    直接调 `ChoiceSelector.set_current` 是**不发信号**的（那是装载那条路），拿它测
    「用户切模式」会什么也没测到还全绿。
    """
    from PySide6.QtWidgets import QComboBox

    combo = page._net_mode.findChild(QComboBox)
    index = combo.findData(value)
    assert index >= 0, f"下拉里没有 {value!r} 这一项"
    combo.setCurrentIndex(index)


def test_net_form_round_trips_the_lease_flags():
    """窗体 ↔ 配置：勾选存的是 ``*_from_lease: true``，**不是**把租约值抄进配置。

    抄进去两头都错：地址跟着当时的网络变（换个 DHCP 服务器就陈旧），而且再也分不出
    「用户手填的」与「照抄来的」—— 后者本该随租约一起变。
    """
    from barkit.plugins.route_guard import settings as rg

    form = rg._net_to_form({"mode": "dhcp", "pin": {
        "ip": "", "mask": "", "gateway_from_lease": True}})
    assert form["gateway_from_lease"] is True
    assert form["dns_from_lease"] is False
    assert form["gateway"] == "", "照抄不是把值填进框里"

    net, hold = rg._net_from_form({**form, "mode": rg._NET_FORM_PIN})
    assert hold == ""
    assert net == {"mode": "dhcp",
                   "pin": {"ip": "", "mask": "", "gateway_from_lease": True}}
    assert "gateway" not in net["pin"], "互斥：写了照抄就不能再写值（保存会被自己的校验拒掉）"

    # 取消勾选 ⇒ 配置里那个键也得消失（改一下勾选＝改一下配置，不是只改界面）
    net, _hold = rg._net_from_form({**form, "mode": rg._NET_FORM_PIN,
                                    "gateway_from_lease": False})
    assert net["pin"] == {"ip": "", "mask": ""}


def test_config_form_lease_checkbox_clears_and_locks_its_field(tmp_path, monkeypatch, qtbot):
    """勾上「照抄 DHCP」＝ 那一格**当场清空并置灰**（两件事都要有，少一件用户就得猜）。

    配置里两者互斥，留着旧值等于同时给了两个答案 —— 那会拖到**保存**那一刻才报错，
    而用户刚看到的是自己勾了个选项，不是填了个值。
    """
    from barkit.plugins.route_guard import settings as rg

    plugin = _plugin(tmp_path, monkeypatch)
    page = _editor(plugin, qtbot)
    _set_net_mode(page, rg._NET_FORM_PIN)

    page._net_gw.setText("10.99.1.254")
    page._net_gw.editingFinished.emit()
    page._net_gw_lease.setChecked(True)
    assert page._net_gw.text() == "", "勾上照抄，同一格的旧值要当场清掉"
    assert page._net_gw.isEnabled() is False, "这一格此刻不由用户填"
    assert page._net_dns.isEnabled() is True, "另一格不受牵连"

    # 取消勾选 ⇒ 那一格回到可填（勾选是来回的，不是单程）
    page._net_gw_lease.setChecked(False)
    assert page._net_gw.isEnabled() is True
    page._net_gw_lease.setChecked(True)

    assert page.save()[0] is True
    pin = _user_profiles(tmp_path)[0]["net"]["pin"]
    assert pin["gateway_from_lease"] is True
    assert "gateway" not in pin, "照抄与写死值不许同时落盘"
    plugin.stop()


def test_config_form_lease_checkbox_releases_the_field_when_leaving_pin_mode(
        tmp_path, monkeypatch, qtbot):
    """切走「固化租约」那一态 ⇒ 勾选一起取消、输入框恢复可填。

    不取消的话会留下「框灰着、勾却看不见」：那一态下勾选框根本不出现，用户既填不进去、
    也找不到是谁锁的（2026-09-18 在本用例钉住）。
    """
    from barkit.plugins.route_guard import settings as rg

    plugin = _plugin(tmp_path, monkeypatch)
    page = _editor(plugin, qtbot)
    _set_net_mode(page, rg._NET_FORM_PIN)
    page._net_gw_lease.setChecked(True)

    _set_net_mode(page, rg._NET_FORM_STATIC)
    assert page._net_gw.isEnabled() is True, "静态 IP 那一态网关由用户填，不继承固化租约的锁"
    assert page._net_gw_lease.isChecked() is False, "勾选跟着态走（它在那一态没有任何含义）"

    _set_net_mode(page, rg._NET_FORM_PIN)
    assert page._net_gw.isEnabled() is True

    # 切走再切回来，照抄**不该**自己回来（那是用户取消过的选择）
    assert page.save()[0] is True
    assert _user_profiles(tmp_path)[0]["net"]["pin"] == {"ip": "", "mask": ""}
    plugin.stop()


def test_settings_page_drops_the_config_entry_and_the_ops_block(tmp_path, monkeypatch, qtbot):
    """设置页两样都不留：不开「打开规则配置」（入口是点面板整行，§4.2 落点表），也不再画装置运维。

    装置运维（立即检查 / 安装守护 / 卸载守护）自 2026-09-28 起由框架按**声明**注入到
    **页尾框架区**，本页只剩自动化总闸与场景相关的配置。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    page = plugin._build_settings_page()
    qtbot.addWidget(page)

    labels = _settings_buttons(page)
    assert "打开规则配置" not in labels, "配置窗体归详情页，设置页不再开第二个入口"
    assert not {"立即检查", "安装守护", "卸载守护"} & set(labels), labels
    assert plugin._feedback is not None, "建页即挂上反馈槽（本页其余动作仍用它）"
    plugin.stop()


# ──────────────────────────────────────────────
# 场景选择器与手动指定（设计 §2.3 / §4.5 / §4.6 —— §6.3 第 7b 步）
#
# 落点：徽标声明在 `plugin.mode_badge_spec`（覆写 AutomationCapability 的默认实现，
# 那一份给的是自动化总闸二态徽标 —— 总闸已搬去设置页顶部），写入在
# `plugin.set_pinned` / `clear_pinned`；设置页总闸在 `settings.build_page`。
# ──────────────────────────────────────────────

def _patched_network(monkeypatch, gateways) -> None:
    """把探活看到的「网络事实」钉成给定的网关集合（``subnet`` 这条腿不管，见下）。

    必须打在 ``intent.network_probe``（模块级名字）上，不能打 ``routeops`` 里那两枚：
    ``network_probe`` 在**调用时**才查这个全局名，而 ``routeops`` 那几个是定义处 ——
    打错地方会让用例看着通过、其实读的是真机网卡。

    签名是 §1.2 那三个（``iface`` / ``subnet`` / ``gateway``）；本组用例的配置夹具都只
    写 ``match.gateway``（``subnet`` 为空串），故这里只认网关那一半。
    """
    from barkit.plugins.route_guard import intent as intent_mod

    monkeypatch.setattr(
        intent_mod, "network_probe",
        lambda iface, subnet, gateway: gateway in set(gateways),
    )


def _intent(plugin) -> dict:
    """组件最近一次**声明出去**的期望态（取代旧的读盘上 ``routes.json``）。

    期望态不再是文件，是会话上的一帧；读口就是会话桩记下的那一帧 —— 面板与 helper
    看到的是同一份东西，中间没有「写下去再读回来」这个转手。
    """
    data = plugin.ctx.helper.declared()
    assert isinstance(data, dict), "组件还没声明过期望态"
    return data


def test_subnet_only_profile_matches_end_to_end(monkeypatch):
    """纯网段场景真的会命中（回归：``match.subnet`` 写了却永不生效）。

    走的是**真链路**（``build_routes`` → ``build_intent`` → 真 ``network_probe``，只把
    helper 的三枚查询桩掉），因为断链的地方正是两段之间：``build_routes`` 得把
    ``match.subnet`` 带进编译产物（**只给决策看、不进线格式**），``build_intent`` 才拿得到
    它去探。少这一环，「生效网段」就是配置窗体里一个写了不起作用的框。
    """
    from barkit.plugins.route_guard import intent as intent_mod

    monkeypatch.setattr(intent_mod, "iface_up", lambda iface: True)
    monkeypatch.setattr(intent_mod, "current_ipv4", lambda iface: ["192.168.11.5"])
    monkeypatch.setattr(intent_mod, "current_gateways", lambda iface: [])

    cfg = {"helper_poll": 30, "profiles": [_profile(
        match={"subnet": "192.168.11.0/24"},
        routes={"enabled": True, "items": [
            {"destination": "10.99.2.0/24", "via": "10.99.1.254"}]},
    )]}
    compiled = rules.build_routes(cfg)
    assert compiled["rules"][0]["subnet"] == "192.168.11.0/24", (
        "编译产物没带上 match.subnet ⇒ 探活拿不到判定依据"
    )

    intent_obj = intent_mod.build_intent(compiled)      # probe 缺省 ⇒ 真 network_probe
    assert intent_obj["rules"][0]["apply"] is True, "网段对上了却没生效"

    monkeypatch.setattr(intent_mod, "current_ipv4", lambda iface: ["192.168.2.5"])
    assert intent_mod.build_intent(compiled)["rules"][0]["apply"] is False

    # 而且 **subnet 不许漏进线格式**：helper 的 ``_RULE_FIELDS`` 白名单不认识它，
    # 漏进去会让**整份意图**被拒（表现为路由停更），不是「多一个字段无妨」。
    assert "subnet" not in intent_obj["rules"][0]
    assert not routeops.load_intent(json.dumps(intent_obj))[1], (
        "线格式被 helper 拒绝：" + str(routeops.load_intent(json.dumps(intent_obj))[1])
    )


def _apply_flags(plugin) -> dict:
    return {r["name"]: r["apply"] for r in _intent(plugin)["rules"]}


def test_badge_declares_a_scene_selector_not_an_automation_toggle(tmp_path, monkeypatch):
    """徽标 = **场景选择器**（§4.5），不再是「自动 ⇄ 手动」的自动化总闸。

    三件事一起钉：

    ① 走候选列表形态 ⇒ **不给 ``on_click``**（两者互斥，``ModeBadgeSpec`` 构造期就拒）；
    ② 每项**必须带摘要**（§4.5「硬要求 1」，不放松：只给名字的菜单等于没给信息）；
    ③ 「回到自动」那一行**只在真锁着的时候**出现。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    mode = plugin.row_specs()[0].mode
    assert mode.on_click is None, "列表形态不许再给 on_click（选中即生效写在各 MenuItem 上）"
    assert mode.get_items is not None and mode.get_state is not None

    items = plugin.scene_items()
    assert [i.label for i in items] == ["office", "home"], "预设为空 ⇒ 不出现「恢复 DHCP」项"
    assert all(i.hint for i in items), "每项都要有摘要"
    assert items[0].hint == "网关 10.99.1.254 · 3 条路由"
    assert items[1].hint == "网关 192.168.2.1 · 1 条路由"
    assert items[0].on_click is not None, "「选中某项做什么」写在各自的 MenuItem 上"
    plugin.stop()


def test_badge_three_states_follow_pin_and_master_switch(tmp_path, monkeypatch):
    """§2.3 三态：``auto`` 命中（生效场景名，**accent**）/ ``pinned``（同文案，**accent**）/
    ``off``（已停用，neutral）/ 无匹配（neutral）。

    accent 表示「这一行当前有东西在管」：你锁定的、或规则命中的都强调 —— 命中却显示成
    灰色看着像「禁用」，与「规则已生效」的事实相反（2026-09-18 真机）。停用 / 无匹配才
    neutral。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    plugin._state = _echo([_rule(matched=True)])

    assert (plugin._badge_label(), plugin._badge_accent()) == ("office", True)

    plugin.set_pinned("home")
    assert (plugin._badge_label(), plugin._badge_accent()) == ("home", True)

    plugin.set_paused(True)
    assert (plugin._badge_label(), plugin._badge_accent()) == ("已停用", False)
    plugin.set_paused(False)

    plugin.clear_pinned()
    plugin._state = _echo([_rule(matched=False, applied=False)])
    assert (plugin._badge_label(), plugin._badge_accent()) == ("无匹配", False)
    plugin.stop()


def test_badge_state_keeps_the_check_on_the_effective_scene(tmp_path, monkeypatch):
    """决议 D33：徽标文案**兼任当前项** ⇒ 它必须能在候选里找到同名的那一项。

    而「自动（按网络匹配）」那一行是**动作**、不是状态：它若被当成当前项，等于说
    「我现在就是自动」—— 可锁着的时候恰恰不是。故它只在锁着时出现，且永远不打勾。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    plugin.set_pinned("home")

    label, accent = plugin.row_specs()[0].mode.get_state()
    assert (label, accent) == ("home", True)
    labels = [i.label for i in plugin.scene_items()]
    assert labels == ["office", "home", "自动（按网络匹配）"], "预设为空 ⇒ 不出现「恢复 DHCP」项"
    assert label in labels, "当前项必须落在候选里（打勾落点）"

    plugin.clear_pinned()
    assert [i.label for i in plugin.scene_items()] == ["office", "home"]
    plugin.stop()


def test_badge_menu_offers_renew_when_preset_configured(tmp_path, monkeypatch):
    """「恢复 DHCP」入口的显隐看预设：设置页勾选了网卡才出现，且固定**第一行**。

    点下去下发一条**命令**（会话上的一帧 ``op.run``），而不是写一个「待办」标记 ——
    命令与「无匹配时才交还」那条策略是两件事（第 9 步真机故障的根因就是两者共用
    一个字段）。写进用户 YAML 的那种「待办」形态已随命令通道一起拆掉。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    plugin._daemon_status = {"installed": True, "running": True, "outdated": False}
    plugin.set_handback_config(["en5"], True)

    labels = [i.label for i in plugin.scene_items()]
    assert labels[0] == "恢复 DHCP", "预设非空 ⇒ 出现且在第一行"

    # 点它 ⇒ 下发命令（当前没有场景接管 en5 ⇒ 只交还的 release）
    plugin.scene_items()[0].on_click()
    kinds = [op for op, _params in plugin.ctx.helper.ops]
    assert kinds == ["route.release"], "没有场景接管 ⇒ 只交还"
    params = plugin.ctx.helper.ops[0][1]
    assert params == {"iface": "en5", "op": "release", "dns": True}, \
        "预设里「恢复 DNS」为真就带上；没有要固化的地址 ⇒ 不带 net"
    assert "seq" not in params, "帧级回执取代了序号配对"
    plugin.stop()


def test_renew_action_decides_renew_vs_release_here(tmp_path, monkeypatch):
    """命令的两种形态：有场景接管这张网卡 ⇒ ``renew``（交还后按场景重新固化）；
    没有 ⇒ ``release``（交还后停在自动获取）。

    「哪个场景此刻接管了哪张网卡」只有插件知道（它才算得出意图），helper 只做机制 ——
    故这份判断必须在下发**之前**做完，命令里带着 ``net`` 下去。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    plugin._daemon_status = {"installed": True, "running": True, "outdated": False}
    plugin.set_handback_config(["en5"], True)
    plugin._compute_intent = lambda: {
        "rules": [{"name": "office", "iface": "en5", "apply": True,
                   "net": {"mode": "dhcp", "pin": {"ip": "", "mask": ""}}}],
    }

    assert plugin.renew_action() is not None
    kind, params = plugin.ctx.helper.ops[-1]
    assert kind == "route.renew" and params["op"] == "renew", \
        "有场景接管 ⇒ 交还后要按它重新固化"
    assert params["net"] == {"mode": "dhcp", "pin": {"ip": "", "mask": ""}}

    # 同一个入口，场景没接管时退化成 release：不带 net
    plugin._compute_intent = lambda: {"rules": []}
    plugin.renew_action()
    kind2, params2 = plugin.ctx.helper.ops[-1]
    assert kind2 == "route.release"
    assert params2["op"] == "release" and "net" not in params2, \
        "没场景接管 ⇒ 只交还、不固化"
    plugin.stop()


def test_renew_action_never_touches_scene_config(tmp_path, monkeypatch):
    """下命令**不碰任何场景配置**（用户 2026-09-18 三次裁定）。

    固化场景正命中时也不许动它的 ``net``：交还是命令的一部分，场景配置是长期意图，
    改它等于「按一次按钮顺手改了配置」。这里两份 profile 的 ``net`` 都必须原样保留。
    """
    body = (
        "plugins:\n"
        "  route_guard:\n"
        "    enabled: true\n"
        "    helper_poll: 30\n"
        "    profiles:\n"
        "      - name: office\n"
        "        ifaces: [en5]\n"
        "        match: {gateway: 10.99.1.254}\n"
        "        net: {mode: dhcp, pin: {ip: '', mask: ''}}\n"
        "        routes: {enabled: true, items: [{destination: 10.99.2.0/24, via: 10.99.1.254}]}\n"
        "      - name: home\n"
        "        ifaces: [en6]\n"
        "        match: {gateway: 192.168.2.1}\n"
        "        net: {mode: dhcp, pin: {ip: '', mask: ''}}\n"
        "        routes: {enabled: true, items: [{destination: 192.168.3.0/24, via: 192.168.2.1}]}\n"
    )
    plugin = _plugin(tmp_path, monkeypatch, body=body)
    plugin._daemon_status = {"installed": True, "running": True, "outdated": False}
    plugin.set_handback_config(["en5"], True)
    plugin._compute_intent = lambda: {
        "rules": [{"name": "office", "iface": "en5", "apply": True,
                   "net": {"mode": "dhcp", "pin": {"ip": "", "mask": ""}}},
                  {"name": "home", "iface": "en6", "apply": False,
                   "net": {"mode": "dhcp", "pin": {"ip": "", "mask": ""}}}],
    }
    plugin.renew_action()
    assert plugin.ctx.helper.ops[-1][1]["op"] == "renew"
    section = _user_section(tmp_path)
    profiles = {p.get("name"): p for p in section.get("profiles") or []}
    assert "net" in profiles["office"], "正接管的场景固化配置也一律不动"
    assert "net" in profiles["home"], "别的场景固化配置不动"
    plugin.stop()


# 这里原有 ``test_reap_ops_removes_only_executed_commands`` 与 ``test_reap_ops_survives_a_restart``：
# 两条测的都是「按 ``seq`` 把执行过的命令从期望态里撤掉」那本账。命令搬去会话帧之后
# 账本整件事没了（一帧一命令、回帧即回执），要守的不变量从「配对配得对」变成「序数与
# 回执不许长回来」，由 ``test_command_channel_has_no_seq_and_no_receipt_book`` 逐条钉住
# —— 覆盖不丢，换了个地方守。


def test_renew_action_refusals_are_reported_to_the_panel(tmp_path, monkeypatch):
    """两条拒绝路径都要说话 —— 它只有徽标下拉这一个入口，静默等于「点了没用」。

    判据是「**命令有没有被下发**」：这两条下的结果都是「什么都没发生，而原因不在
    你按下那一刻的界面上」，与真出错同一种处境（只有红色拦得住人）。
    """
    from barkit.contribution.specs import ActionOutcome

    plugin = _plugin(tmp_path, monkeypatch)

    plugin._daemon_status = {"installed": False, "running": False, "outdated": False}
    result = plugin.renew_action()
    assert result is not None and result.outcome is ActionOutcome.FAILED
    assert "守护" in result.text

    plugin._daemon_status = {"installed": True, "running": True, "outdated": False}
    plugin._config_problem = "profiles[0].net 非法"
    result = plugin.renew_action()
    assert result is not None and result.outcome is ActionOutcome.FAILED
    assert "profiles[0].net 非法" in result.text

    # 预设为空：不是失败，是「你还没配置」（去设置页勾一下即可）—— 但仍要说
    plugin._config_problem = ""
    result = plugin.renew_action()
    assert result is not None and result.text, "只说一句，但不该是红的"
    assert plugin.ctx.helper.ops == [], "预设为空时一条命令都不许发"
    plugin.stop()


def test_renew_action_confirm_because_the_effect_is_deferred(tmp_path, monkeypatch):
    """成功也要说话：执行是**守护下一轮**做的事，用户按完看不到当场变化。

    回执只说「交还系统」——之后守护按哪个场景做什么是**机制**，不是用户按下的那个动作
    （2026-09-29 用户裁定：面板上那一行就叫「恢复 DHCP」，不必替它解释后续）。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    plugin._daemon_status = {"installed": True, "running": True, "outdated": False}
    plugin.set_handback_config(["en5"], True)

    plugin._compute_intent = lambda: {
        "rules": [{"name": "office", "iface": "en5", "apply": True,
                   "net": {"mode": "dhcp", "pin": {"ip": "", "mask": ""}}}],
    }
    result = plugin.renew_action()
    assert result is not None and "交给守护" in result.text and "固化" not in result.text
    plugin.stop()


def test_settings_page_lists_ifaces_as_checkboxes(tmp_path, monkeypatch, qtbot):
    """「恢复 DHCP」的网卡清单必须是**页面上看得见的控件**：一台网卡一个复选框。

    原先勾选项装在 ``QListWidget`` 里，被全局样式给它的内边距挤成一个灰底空框 ——
    用户 2026-09-18 实测「设置里没有选择网卡的列表，只有一个灰色的空的矩形」。改成
    复选框行后与页面其他控件同一形态，也不依赖列表控件的尺寸计算；勾选后按「保存」
    落盘这条链路照旧。
    """
    from PySide6.QtWidgets import QCheckBox, QPushButton

    body = (
        "plugins:\n"
        "  route_guard:\n"
        "    enabled: true\n"
        "    helper_poll: 30\n"
        "    handback_ifaces: [en5]\n"
        "    profiles:\n"
        "      - name: office\n"
        "        ifaces: [en5]\n"
        "        match: {subnet: 10.99.1.0/24}\n"
        "        routes: {enabled: true, items: []}\n"
        "      - name: home\n"
        "        ifaces: [en6]\n"
        "        match: {gateway: 192.168.2.1}\n"
        "        routes: {enabled: true, items: []}\n"
    )
    plugin = _plugin(tmp_path, monkeypatch, body=body)
    page = plugin._build_settings_page()
    qtbot.addWidget(page)

    boxes = {b.text(): b for b in page.findChildren(QCheckBox)}
    assert "en5" in boxes and "en6" in boxes, "规则涉及的网卡每台一个复选框"
    assert boxes["en5"].isChecked() is True, "已保存的预设要回显成勾上"
    assert boxes["en6"].isChecked() is False

    boxes["en6"].setChecked(True)
    save = next(b for b in page.findChildren(QPushButton)
                if b.text() == "保存恢复 DHCP 配置")
    save.click()
    assert _user_section(tmp_path).get("handback_ifaces") == ["en5", "en6"]
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
        "version": "2.12.0", "expected": "9.9.9",
    }
    page = plugin._build_settings_page()
    qtbot.addWidget(page)
    refreshed = []
    zone_fb = FeedbackBar(page)
    plugin.attach_helper_ops(page, zone_fb, lambda: refreshed.append(1))

    monkeypatch.setattr(daemon, "install", lambda *a, **k: True)
    monkeypatch.setattr(daemon, "status", lambda: {
        "installed": True, "running": True, "outdated": False,
        "version": "9.9.9", "expected": "9.9.9",
    })
    plugin.install_action()

    assert refreshed, "装完必须喊一声 —— 不喊用户仍看着「守护版本过期」与「重装守护」"
    assert "已安装" in zone_fb.text(), "成功反馈落在装置运维那一区的槽里"
    assert "9.9.9" in zone_fb.text(), "反馈带上装好的版本号，用户能确认真的换了版本"
    plugin.stop()


def test_refresh_helper_notice_reloads_status_before_recomputing(tmp_path, monkeypatch, qtbot):
    """守护连接 / 版本变化时：**先重拉状态、再重算**警示 —— 顺序不能反。

    来由（2026-09-30 复评）：上一版只重算 UI（走插件缓存的 ``_daemon_status``），
    守护重连后若插件还没 tick 过，红字会按旧缓存算出来、退不掉。故新入口
    ``refresh_helper_notice()`` 必须先 ``on_helper_change()`` 把状态刷成本刻的。
    """
    from barkit.shell.ui_kit import FeedbackBar

    plugin = _plugin(tmp_path, monkeypatch)
    plugin._daemon_status = {           # 缓存里仍是「已装但过期」
        "installed": True, "running": True, "outdated": True,
        "version": "2.12.0", "expected": "9.9.9",
    }
    page = plugin._build_settings_page()
    qtbot.addWidget(page)
    # 重算那一刻看到的「过期」判据：顺序反了（先重算后重拉）这里会是 True（旧缓存）
    seen = []
    plugin.attach_helper_ops(
        page, FeedbackBar(page), lambda: seen.append(plugin.daemon_outdated()))

    monkeypatch.setattr(daemon, "status", lambda: {   # 守护已换成新版
        "installed": True, "running": True, "outdated": False,
        "version": "9.9.9", "expected": "9.9.9",
    })
    plugin.refresh_helper_notice()

    assert seen == [False], "重算之时状态必须已是新拉的（顺序反了这里会是 True）"
    plugin.stop()


def test_badge_menu_hides_handback_when_preset_empty(tmp_path, monkeypatch):
    """「恢复 DHCP」只在设置页勾选了网卡时出现（2026-09-18 二次裁定）：
    预设为空说明用户还没配置这个功能，常驻一项点了只会被提示「去设置页」是噪声。"""
    plugin = _plugin(tmp_path, monkeypatch)
    plugin._daemon_status = {"installed": True, "running": True, "outdated": False}
    plugin._state = {
        "net": {"iface": "en5", "service": "BarKit", "mode": "dhcp+pin",
                "managed": True, "wrote": {"ip": "10.99.2.9"}, "action": "", "error": ""},
    }
    labels = [i.label for i in plugin.scene_items()]
    assert "恢复 DHCP" not in labels, "预设为空 ⇒ 即便网卡正被接管也不出现"
    assert plugin.handback_ifaces() == []
    plugin.stop()


def test_set_pinned_writes_yaml_and_beats_matching(tmp_path, monkeypatch):
    """「手动指定」= **忽略匹配**：锁着谁就只有谁生效，别的这一轮一个都不要动（§2.3）。

    这个用例同时是「``pinned`` 不进意图文件」那条硬约束的判据：线格式是 root helper
    已在消费的契约，多一个它不认识的键就是**整份拒绝 + 冻结**，所以 ``pinned`` 只折进
    ``apply`` 一位里，产物里不许冒出这个键。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    _patched_network(monkeypatch, gateways={"10.99.1.254"})      # 只有 office 命中
    plugin.sync_routes()
    assert _apply_flags(plugin) == {"office": True, "home": False}

    plugin.set_pinned("home")
    assert _user_section(tmp_path)["pinned"] == "home"

    data = _intent(plugin)
    by_name = {r["name"]: r for r in data["rules"]}
    assert by_name["home"]["apply"] is True, "锁着的那个即便网关对不上也要生效"
    assert by_name["office"]["apply"] is False, "忽略匹配 = 别的场景一条都不动"
    # 「命中」是**事实**，照实回显（不假装网络变了）：面板靠它算「多场景同时命中」
    assert by_name["office"]["trigger_matched"] is True
    # 顶层键 = 意图契约本身（第 8 步的 ``handback`` 与 ``net`` 一起随 HELPER_VERSION 升级
    # 进的线格式，两者都是「helper 那边已经认识」的键）。``pinned`` 仍然不许出现。
    assert set(data) == {"version", "poll", "paused", "handback", "handback_dns",
                         "rules"}, "意图里不许有 pinned，也不再有 ops（命令走会话帧）"
    assert data["handback"] == [], "没按 [交还系统]、策略也是 keep ⇒ 一个网卡都不交还"
    assert all("pinned" not in r and "manual_only" not in r for r in data["rules"])
    # 网卡那一半：默认场景写的是 ``net: {mode: dhcp}``（不接管），原样递给 helper ——
    # 它认这个形状且**什么都不做**（没有 ``pin`` 就没有要固化的地址，一根手指都不碰网卡）。
    # 这里钉的是「app 侧不做 net 决策」：形状原样过，判据全在 helper（§2.5）。
    assert all(r["net"] == {"mode": "dhcp"} for r in data["rules"])
    plugin.stop()


def test_clear_pinned_returns_to_auto(tmp_path, monkeypatch):
    """取消手动指定 = 回到按网段 / 网关自动挑（§2.3 的 ``auto``）。"""
    plugin = _plugin(tmp_path, monkeypatch)
    _patched_network(monkeypatch, gateways={"10.99.1.254"})
    plugin.set_pinned("home")
    plugin.clear_pinned()

    assert "pinned" not in _user_section(tmp_path), "清键而不是写空串（段里少一个键更干净）"
    assert _apply_flags(plugin) == {"office": True, "home": False}
    plugin.stop()


def test_manual_only_scenes_never_auto_match(tmp_path, monkeypatch):
    """``manual_only``（§2.6）退出自动匹配：判定依据会被组件自己写掉 ⇒ 自证循环。

    它只能被 ``pinned`` 点亮，且 ``trigger_matched`` 恒 False —— 报告与行为同源，
    否则面板会在「明明没在用它、却被报成命中」上撒谎（「多场景同时命中」那条也跟着虚报）。
    """
    body = (
        "plugins:\n  route_guard:\n    enabled: true\n    helper_poll: 30\n"
        "    profiles:\n"
        "      - name: office\n"
        "        ifaces: [en5]\n"
        "        match: {gateway: 10.99.1.254}\n"
        "        routes:\n"
        "          items: [{destination: 10.99.2.0/24, via: 10.99.1.254}]\n"
        "      - name: vpn\n"
        "        ifaces: [en5]\n"
        "        manual_only: true\n"
        "        match: {gateway: 10.99.1.254}\n"
        "        routes:\n"
        "          items: [{destination: 10.9.0.0/16, via: 10.99.1.254}]\n"
    )
    plugin = _plugin(tmp_path, monkeypatch, body=body)
    _patched_network(monkeypatch, gateways={"10.99.1.254"})      # 两张网卡都「命中」
    plugin.sync_routes()

    by_name = {r["name"]: r for r in _intent(plugin)["rules"]}
    assert by_name["office"]["apply"] is True
    assert by_name["vpn"]["apply"] is False, "自证循环的场景不许自动叠加"
    assert by_name["vpn"]["trigger_matched"] is False, "它根本不参与匹配，别报成命中"

    # 摘要如实标出「仅手动」（用户凭它知道这一类只能从面板切过去）
    assert plugin.scene_items()[1].hint == "网关 10.99.1.254 · 仅手动 · 1 条路由"

    plugin.set_pinned("vpn")
    by_name = {r["name"]: r for r in _intent(plugin)["rules"]}
    assert by_name["vpn"]["apply"] is True, "手动指定的场景只能这样被点亮"
    assert by_name["office"]["apply"] is False
    plugin.stop()


def test_subtitle_says_manual_and_keeps_the_name_on_the_badge(tmp_path, monkeypatch):
    """锁着的时候副标题说「手动指定」，且**不再重复场景名**。

    这是第 6 步给第 7b 步留的两个口子之一：徽标换成场景选择器之后，设计 §3.3 那句
    「配置项名只出现一次」才真正成立（那一次在徽标上）。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    plugin._state = _echo([_rule(matched=True)])
    plugin.set_pinned("office")

    text = plugin._row_subtitle()
    assert text.startswith("手动指定 · "), text
    assert "office" not in text, "场景名归徽标（设计 §3.3：配置项名只出现一次）"
    plugin.stop()


def test_panel_switch_is_disabled_while_paused(tmp_path, monkeypatch):
    """停用态那一行的 Switch 是**禁用**的（§4.3 的四态对照表）。

    停用 = helper 整体冻结，翻这一下只是写个不会生效的覆盖 —— 开关当场翻过去、路由
    一条没动，用户会以为已经撤掉了（而 §3.2 的裁决恰恰是「停用**不**回滚」）。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    plugin._state = _echo([_rule(matched=True)])

    spec = plugin.row_specs()[0]
    sw = next(c for c in spec.controls if isinstance(c, SwitchSpec))
    assert sw.get_enabled() is True, "未停用：知道作用到哪个场景就允许操作（§4.8）"

    plugin.set_paused(True)
    spec = plugin.row_specs()[0]
    sw = next(c for c in spec.controls if isinstance(c, SwitchSpec))
    assert sw.get_enabled() is False
    plugin.stop()


def test_settings_page_has_the_automation_master_switch(tmp_path, monkeypatch, qtbot):
    """自动化总闸在**设置页顶部**（§4.3 的 L3 设置页草图第一行）。

    「低频 + 破坏性」的决定不在面板上：面板那一行只做状态回显（副标题写「已停用 · …」、
    徽标写「已停用」）。机制（``paused``）早就有了，缺的一直只是这处 UI 落点。
    """
    from barkit.shell.ui_kit import Switch

    plugin = _plugin(tmp_path, monkeypatch)
    page = plugin._build_settings_page()
    qtbot.addWidget(page)

    sw = page.findChildren(Switch)[0]
    assert sw.isChecked() is True, "默认自动化是开着的（未暂停）"
    sw.clicked.emit()                          # Switch.clicked 是自定义无参信号
    assert plugin.is_paused is True
    assert _user_section(tmp_path)["paused"] is True, "总闸的落点就是 paused，不另开键"
    assert sw.isChecked() is True, "控件自己不回读：显示态由下次建页给出（不制造第二真源）"
    plugin.stop()


def test_settings_page_helper_poll_spinbox_wired(tmp_path, monkeypatch, qtbot):
    """helper 巡逻节律（helper_poll）在设置页有 QSpinBox，且接到 plugin 的读写方法。

    取值域与 ``rules.build_routes`` 的校验共用同一份常量（1–86400 秒）；控件只调
    ``plugin.helper_poll_seconds()`` / ``set_helper_poll()``，自己不碰 config
    （过 ``test_no_config_io_in_ui``）。
    """
    from PySide6.QtWidgets import QSpinBox
    from barkit.plugins.route_guard import rules

    plugin = _plugin(tmp_path, monkeypatch)
    page = plugin._build_settings_page()
    qtbot.addWidget(page)

    spin = page.findChild(QSpinBox, "helper_poll_spin")
    assert spin is not None, "设置页应有 helper_poll 的 QSpinBox"
    assert (spin.minimum(), spin.maximum()) == (
        rules.MIN_HELPER_POLL, rules.MAX_HELPER_POLL,
    ), "取值域与校验共用常量"
    assert spin.value() == plugin.helper_poll_seconds(), "初始值来自合并后有效配置"

    spin.setValue(120)
    spin.editingFinished.emit()
    assert plugin.helper_poll_seconds() == 120
    assert _user_section(tmp_path)["helper_poll"] == 120, "editingFinished 落盘"
    plugin.stop()


def test_set_helper_poll_roundtrip(tmp_path, monkeypatch):
    """直接走 ``plugin.set_helper_poll``：写盘 ＋ 重读一致，且同值幂等早退不写盘。"""
    from barkit.plugins.route_guard import rules

    plugin = _plugin(tmp_path, monkeypatch)

    assert plugin.helper_poll_seconds() == rules.DEFAULT_HELPER_POLL
    plugin.set_helper_poll(120)
    assert plugin.helper_poll_seconds() == 120
    assert _user_section(tmp_path)["helper_poll"] == 120

    before = _user_section(tmp_path)
    plugin.set_helper_poll(120)            # 同值 ⇒ 早退，不改盘
    assert _user_section(tmp_path) == before

    plugin.stop()


def test_config_form_pins_the_scene_it_shows(tmp_path, monkeypatch, qtbot):
    """配置窗体页脚的「固定使用」与面板徽标**同一个落点**（§2.3）：两个入口、一份写入。

    这个入口不是冗余：``manual_only`` 的场景**无法自动触达**（§2.6），而配置窗体恰恰是
    唯一看得见它的地方 —— 用户得能在这里把控制权交给它。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    page = _editor(plugin, qtbot)
    assert page._pin.text() == "固定使用"

    page._pin.click()
    assert _user_section(tmp_path)["pinned"] == "office"
    assert page._pin.text() == "已固定 · 取消"
    assert "手动指定" in page._status.text(), "顶部状态条跟着改口"

    page._pin.click()
    assert "pinned" not in _user_section(tmp_path)
    assert page._pin.text() == "固定使用"
    plugin.stop()


def test_deleting_the_pinned_scene_clears_the_pin(tmp_path, monkeypatch):
    """删掉被固定的那个场景 ⇒ 顺手取消固定。

    悬空引用会让**整段配置非法**（``build_routes`` 报「pinned 指向不存在的场景」），
    而那是用户刚做的那件事的反面 —— 他只是删了一个场景，不该顺手把组件停掉。

    **边界**：只清「本次界面动作」造成的悬空（走 ``save_profiles``）；手改 YAML 造成的
    悬空照样由 ``build_routes`` 报错指路 —— 与「旧 ``rules[]`` 不自动迁移」同一条判据：
    组件不替用户猜他想要什么。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    plugin.set_pinned("home")
    keep = [p for p in plugin.editable_profiles() if p["name"] != "home"]

    ok, msg, bad = plugin.save_profiles(keep)
    assert ok and bad is None and "固定使用一并取消" in msg, msg
    assert "pinned" not in _user_section(tmp_path)
    assert plugin._config_problem == "", "清干净之后配置不该停在非法态"
    assert list(_apply_flags(plugin)) == ["office"], "场景确实只剩一个"
    plugin.stop()


# ---------- 面板侧动作的结果反馈（`ActionResult`，契约 D32-②） ----------
#
# 本组件从前那条口径写在 `_say_error` 的 docstring 里：「面板侧看不到文案，日志是唯一
# 线索」—— 用户在面板上按一下开关、写盘失败时，开关会自己弹回原位，一句解释都没有。
# 下面几条钉住「面板侧的四个入口都把结局交回去」，以及**设置页开着时不重复说两遍**。


def _break_config_write(plugin, monkeypatch):
    """让下一次 `update_plugin_section` 失败（模拟只读文件系统）。"""
    def _boom(*_a, **_kw):
        raise OSError("Read-only file system")

    monkeypatch.setattr(plugin.ctx.config, "update_plugin_section", _boom)


def test_panel_switch_reports_write_failure_as_action_result(tmp_path, monkeypatch):
    """面板开关写盘失败 ⇒ 返回 `FAILED`（面板那一行结果槽会说出来）。"""
    from barkit.contribution import ActionOutcome

    plugin = _plugin(tmp_path, monkeypatch)
    _break_config_write(plugin, monkeypatch)

    result = plugin.toggle_route_switch()

    assert result is not None, "写盘失败却什么都不返回 —— 面板上又是「点了没反应」"
    assert result.outcome is ActionOutcome.FAILED
    assert "无法写入配置" in result.text and "操作失败" in result.text
    plugin.stop()


def test_panel_switch_stays_silent_on_success(tmp_path, monkeypatch):
    """成功**不返回**任何结果（`ActionResult` 的口径：只在有话要说时返回）。"""
    plugin = _plugin(tmp_path, monkeypatch)
    assert plugin.toggle_route_switch() is None
    plugin.stop()


def test_pinned_unknown_scene_is_reported_as_failure(tmp_path, monkeypatch):
    """徽标里选了一个刚被手改掉的场景名 ⇒ `FAILED`，而不是静默不动。"""
    from barkit.contribution import ActionOutcome

    plugin = _plugin(tmp_path, monkeypatch)
    result = plugin.set_pinned("不存在的场景")
    assert result is not None and result.outcome is ActionOutcome.FAILED
    assert "没有" in result.text
    plugin.stop()


def test_settings_slot_takes_precedence_over_panel_result(tmp_path, monkeypatch, qtbot):
    """设置页开着时，同一句错话**只落它那块槽**，不再回给面板（同一个界面说两遍＝像出了两次）。"""
    from barkit.shell.ui_kit import FeedbackBar

    plugin = _plugin(tmp_path, monkeypatch)
    fb = FeedbackBar()
    plugin._feedback = fb

    assert plugin._failed("写盘失败：只读文件系统") is None, "设置页开着时不该再回给面板"
    assert "只读文件系统" in fb.text(), "设置页那块槽必须拿到文案"

    plugin._feedback = None
    late = plugin._failed("写盘失败：只读文件系统")
    assert late is not None, "设置页关掉之后，同一句错话要能交回面板"
    plugin.stop()


def test_profile_topic_matches_the_constant_subscribers_use():
    """订阅方（如翻页时钟）用的常量必须与**本插件**真发的 topic 同源。

    订阅方不许 import 本插件（插件之间只经总线），于是常量落在 ``core/events``；
    代价是这个字面量有了第二份。故这里对拍：改了一边另一边立刻转红。
    """
    from types import SimpleNamespace

    from barkit.core.events import ROUTE_PROFILE_TOPIC

    assert RouteGuardPlugin.status_topic(SimpleNamespace(id="route_guard"), "profile") == (
        ROUTE_PROFILE_TOPIC
    )


def test_config_form_delete_drops_the_one_the_question_named(tmp_path, monkeypatch, qtbot):
    """确认时**不许重读选中项**：问句写着 office，就必须摘掉 office。

    问句挂着的时候左侧列表照样可点 —— 用户完全可以先选中 home、再回头按「删除」。
    那时重读 ``_editing`` 摘掉的就是 home，而问句上写着的还是 office（删除是不可撤销的
    那一类动作，且配置是**整表覆盖**语义，摘错了连回旋余地都没有）。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    page = _editor(plugin, qtbot)
    assert page._editing == 0                      # 默认选中 office

    page._on_delete()
    assert "office" in page._fb.text()
    page._select(1)                                # 问句还挂着，选中挪到 home
    page._fb.accept()
    assert [p["name"] for p in page._profiles] == ["home"], (
        "摘掉了此刻选中的那一条，而不是问句点名的那一条")
    plugin.stop()


# ---------- 设置页那几条写配置的入口：写盘失败必须有话说 ----------
#
# 改前这些入口把 `_write_keys` 交出的 `(成功?, 文案)` 直接丢掉 —— 保存按钮 / 下拉 /
# 秒数格按下之后界面照旧显示新值，盘上一个字节没动，日志里也没有一行。下面几条钉住
# 「失败就说出来」，并钉住托盘那条切换**不报假成功**。


def _break_second_config_write(plugin, monkeypatch):
    """只让**第二次**写入失败（第一次写 `profiles` 得成功，才走得到「顺手清悬空」那半）。"""
    real = plugin.ctx.config.update_plugin_section
    calls = {"n": 0}

    def _hook(plugin_id, mutate):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("Read-only file system")
        real(plugin_id, mutate)

    monkeypatch.setattr(plugin.ctx.config, "update_plugin_section", _hook)


def test_settings_writes_report_failure_to_the_slot(tmp_path, monkeypatch, qtbot):
    """设置页三条写配置的入口：写盘失败 ⇒ 文案落在它那块反馈槽。"""
    from barkit.shell.ui_kit import FeedbackBar

    plugin = _plugin(tmp_path, monkeypatch)
    fb = FeedbackBar()
    plugin._feedback = fb
    _break_config_write(plugin, monkeypatch)

    plugin.set_helper_poll(999)
    assert "保存巡逻间隔失败" in fb.text(), "helper 巡逻节律写失败没说话"

    plugin.set_fallback("dhcp")
    assert "设置「无匹配时」失败" in fb.text(), "「无匹配时」写失败没说话"

    plugin.set_handback_config(["en5"], True)
    assert "保存恢复 DHCP 配置失败" in fb.text(), "「恢复 DHCP 配置」写失败没说话"
    plugin.stop()


def test_automation_switch_reports_write_failure_to_the_host(tmp_path, monkeypatch):
    """自动化开关写盘失败 ⇒ 把结局交回宿主（`AutomationCapability` 契约 §2.6）。

    本类改前声明 ``-> None`` 且丢掉返回值，正是「开关弹回原位、宿主拿不到话」那种情形。
    """
    from barkit.contribution import ActionOutcome

    plugin = _plugin(tmp_path, monkeypatch)
    _break_config_write(plugin, monkeypatch)

    result = plugin.set_automation_active(False)

    assert result is not None, "开关写盘失败却什么都不返回 —— 宿主拿不到「没写成」"
    assert result.outcome is ActionOutcome.FAILED
    assert "无法写入配置" in result.text and "暂停失败" in result.text
    plugin.stop()


def test_set_paused_lands_on_the_settings_slot(tmp_path, monkeypatch, qtbot):
    """设置页开着时，`set_paused` 只落它那块槽、不再回 `ActionResult`（同一个界面不说两遍）。"""
    from barkit.shell.ui_kit import FeedbackBar

    plugin = _plugin(tmp_path, monkeypatch)
    fb = FeedbackBar()
    plugin._feedback = fb
    _break_config_write(plugin, monkeypatch)

    assert plugin.set_paused(True) is None, "设置页那块槽已经说过了，不该再回给面板"
    assert "暂停失败" in fb.text()
    plugin.stop()


def test_toggle_pause_action_does_not_claim_success_after_a_failed_write(tmp_path, monkeypatch, qtbot):
    """托盘那条切换：写盘失败 ⇒ 槽里留下的是错话，不许被「已暂停 / 已记录」盖掉。

    守护状态打成「未安装」是**故意**的：那一路的收尾话是「暂停状态已记录，安装后生效」，
    写盘失败还说这句正是最典型的假成功（改前它会盖掉错话，用户看到的是一句「已记录」）。
    """
    from barkit.shell.ui_kit import FeedbackBar

    plugin = _plugin(tmp_path, monkeypatch)
    fb = FeedbackBar()
    plugin._feedback = fb
    monkeypatch.setattr(plugin, "_daemon_status", {"installed": False})
    _break_config_write(plugin, monkeypatch)

    plugin.toggle_pause_action()

    assert "无法写入配置" in fb.text(), "写失败得说出来"
    assert "已暂停自动路由" not in fb.text(), "写盘失败还说「已暂停」是假成功"
    assert "暂停状态已记录" not in fb.text(), "写盘失败还说「已记录」是假成功"
    plugin.stop()


def test_save_profiles_says_which_half_failed_when_unpinning_fails(tmp_path, monkeypatch):
    """删掉被固定的场景时，「顺手取消固定」那一步写失败 ⇒ 文案不许说成「一并取消」。"""
    plugin = _plugin(tmp_path, monkeypatch)
    plugin.set_pinned("home")
    keep = [p for p in plugin.editable_profiles() if p["name"] != "home"]
    _break_second_config_write(plugin, monkeypatch)

    ok, msg, bad = plugin.save_profiles(keep)

    assert ok and bad is None, msg
    assert "已删除" in msg, "成了的那一半要说清"
    assert "固定使用一并取消" not in msg, "那一步没成，不许报成成了"
    assert "清理失效的固定使用失败" in msg, "得说出没成的是哪一步"
    plugin.stop()


# ──────────────────────────────────────────────
# 留空的下一跳（v8）：那个网关由 helper 从本场景固化那块网卡上取
#
# 用户报的坑：界面写着「下一跳留空 = 用上面的匹配网关」，而匹配网关那格也空着时
# 留空就无处可落 —— 保存被拦。正题那一支（dhcp → 固定 IP）**写不出**那个值：它在
# 租约里。于是 v8 让 helper 在本轮把它取出来：网卡收敛时读到的那个网关记进
# ``net_gateways``（跨守护重启靠快照带回），有规则留空就拿它补；补不出来时那条
# 路由**一个都不动**（加要网关，撤也要网关），面板报一格 WARN。
# ──────────────────────────────────────────────

def _blank_hop_profile(net=None, **overrides) -> dict:
    """配置侧的「下一跳留空」场景：``match`` 空、路由只写目标。

    ``net`` 缺省是**固化租约**那份（`pin` 只固化地址、网关不固化）—— 用户现场就是它。
    传 ``net=None`` 得到「跟随系统」（连 ``net`` 键都不写）。
    """
    profile = _profile(destinations=())
    profile["match"] = {}
    profile["routes"] = {"enabled": True,
                         "items": [{"destination": "10.99.2.0/24"}]}
    if net is None:
        profile.pop("net", None)
    else:
        profile["net"] = net
    profile.update(overrides)
    return profile


def _pin_lease_net() -> dict:
    return {"mode": "dhcp", "pin": {"ip": "", "mask": ""}}


def _pin_blank_rule() -> dict:
    """线格式侧的同一条规则（app 侧编译的产物）：``gateway`` 空 ＋ 带 ``pin`` 的 ``net``。"""
    return {**OFFICE, "gateway": "", "net": _pin_lease_net()}


def _levied(iface: str = "en5") -> dict:
    """那本账里某个网卡那一格。

    账的形状是 ``{网卡: {"gateway": …, "net": …}}``（网卡 + 它当时那份网络配置一起记，
    否则不同场景的规则会互相认领）—— 形状只在这里读，改形状时改这一处。
    """
    entry = (_observed_route().get("net_gateways") or {}).get(iface)
    return entry if isinstance(entry, dict) else {}


def _levied_gateway(iface: str = "en5") -> str:
    """那本账里某个网卡记下的网关；没记过 ⇒ 空串。"""
    return str(_levied(iface).get("gateway") or "")


def test_blank_next_hop_compiles_only_for_a_pinned_lease_scene():
    """留空能存下来的，只有「固化租约」那一态 —— 别的场景照旧当场报错。

    这条守的正是用户那句话：注释说留空可以，实际保存不了。修法不是「都能留空」，
    而是**把能留空的那一支真的做通**，其余照旧拦下（它们确实没有一个网关可落）。
    """
    out = rules.build_routes({"profiles": [_blank_hop_profile(_pin_lease_net())]})
    rule = out["rules"][0]
    assert rule["gateway"] == "", "空串是**信息**：它说的是「由 helper 从网卡上取」"
    assert rule["destinations"] == ["10.99.2.0/24"]
    assert rule["net"] == _pin_lease_net(), "取值的依据（pin）要跟着进意图"

    # 跟随系统 / 静态 IP：这一态给不出网关 ⇒ 报错，且文案点场景、点两条出路
    for net in (None, {"mode": "static", "ip": "10.99.1.10",
                       "mask": "255.255.255.0"}):
        with pytest.raises(rules.RouteConfigError) as exc:
            rules.build_routes({"profiles": [_blank_hop_profile(net)]})
        msg = str(exc.value)
        assert "没有下一跳" in msg and "office" in msg, msg
        assert "固化租约" in msg and "匹配网关" in msg, f"报错要给两条出路：{msg}"

    # 有的写、有的留空：一个只取得到、一个写死了 ⇒ 无法确定唯一下一跳，不替用户挑
    mixed = _blank_hop_profile(_pin_lease_net())
    mixed["routes"]["items"].append({"destination": "10.2.0.0/16",
                                     "via": "10.99.1.254"})
    with pytest.raises(rules.RouteConfigError) as exc:
        rules.build_routes({"profiles": [mixed]})
    assert "统一" in str(exc.value), str(exc.value)


def test_blank_next_hop_error_reaches_the_user_in_plain_words(tmp_path, monkeypatch):
    """配置窗体那一路（``save_profiles``）拿到的是那句人话，不是 ``profiles[0]`` 坐标。

    保存被拦时用户只有这条反馈可看（窗体不弹二次确认），故它必须自己说清「哪一条、
    怎么改」—— 坐标缩到括号里，留给去手改 YAML 的人。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    ok, msg, bad = plugin.save_profiles([_blank_hop_profile(None)])
    assert not ok and bad is None
    assert "office" in msg and "固化租约" in msg and "匹配网关" in msg, msg
    plugin.stop()


def test_blank_next_hop_takes_the_gateway_from_the_nic(net_env):
    """正题：租约给了网关 ⇒ 那条路由经**它**叠上去，并把来源报出来（``iface``）。"""
    _src, table, net = net_env
    _forget_the_books()
    net.define("en5", mode="dhcp")
    net.packet("en5", ip="10.99.1.21", mask="255.255.255.0", gateway="10.99.1.254")

    routeops.run_cycle(_intent_text([_apply(_pin_blank_rule(), True)]))
    state = _observed_route()
    assert _levied_gateway() == "10.99.1.254", "网卡的网关要记进那本账"
    assert _levied()["net"] == _pin_lease_net(), "记的那一格要连它依据的那份配置一起记"
    rule = state["rules"][0]
    assert rule["gateway_src"] == routeops.GATEWAY_SRC_IFACE
    assert rule["gateway"] == "10.99.1.254"
    assert [d["action"] for d in rule["destinations"]] == ["add"]
    assert table.ops == [("add", "10.99.2.0/24", "10.99.1.254")]


def test_blank_next_hop_works_when_the_address_is_pinned_too(net_env):
    """pin 把地址写死时，这一轮**仍要去读一次租约** —— 留空的下一跳要的网关只在租约里。

    ``_lease_needed`` 的常规判据是「pin 的 ip / mask 有没有留空」：都写死就不必碰租约。
    可留空的下一跳打破了这个推断 —— 我们没往网卡上写网关，那个值**只在租约里**
    （见 ``_lease_needed`` 的 ``need_gateway``）。不为此读这一次，用户在「固化地址 ＋
    下一跳留空」这个组合下就永远取不到网关，而面板上只会说「未取到网关」。
    """
    _src, table, net = net_env
    _forget_the_books()
    net.define("en5", mode="dhcp")
    net.packet("en5", ip="10.99.1.21", mask="255.255.255.0", gateway="10.99.1.254")
    rule = _pin_blank_rule()
    rule["net"] = {"mode": "dhcp", "pin": {"ip": "10.99.1.21", "mask": "255.255.255.0"}}

    routeops.run_cycle(_intent_text([_apply(rule, True)]))

    got = _observed_route()["rules"][0]
    assert got["gateway_src"] == routeops.GATEWAY_SRC_IFACE, "这一次租约必须真去读"
    assert got["gateway"] == "10.99.1.254"
    assert table.ops == [("add", "10.99.2.0/24", "10.99.1.254")]


def test_blank_next_hop_holds_when_there_is_no_gateway_yet(net_env):
    """租约里还没有网关（这一轮没拿到）⇒ 那条路由**一个都没动**，如实报 ``none``。

    「不动」不是偷懒：加要网关，撤也要网关（``ensure_removed`` 先确认那条路由确实经
    这个网关生效），空网关连确认都做不了。而整份拒绝又会把组件冻住 —— 守护刚启动时
    「还没拿到租约」是常态。
    """
    _src, table, net = net_env
    _forget_the_books()
    net.define("en5", mode="dhcp")
    net.packet("en5", ip="10.99.1.21", mask="255.255.255.0")   # 没有 router 这一项

    routeops.run_cycle(_intent_text([_apply(_pin_blank_rule(), True)]))
    state = _observed_route()
    assert _levied_gateway() == "", "读到空值不许记进去"
    rule = state["rules"][0]
    assert rule["gateway"] == "" and rule["gateway_src"] == routeops.GATEWAY_SRC_NONE
    assert [d["action"] for d in rule["destinations"]] == ["hold"]
    assert table.ops == [], "取不到网关 ⇒ 那条路由一条命令都不许发"


def test_the_nics_gateway_survives_a_helper_restart(net_env):
    """那本账跨守护重启不丢（快照带回）：重启后租约这一路已经读不到了。

    这正是「值读一次就没了」的现场 —— 场景一命中，网卡就被切成 Manual，
    ``ipconfig getpacket`` 从此必然失败。不记下来，下一轮那些留空的路由就全成了
    「取不到网关」。
    """
    _src, table, net = net_env
    _forget_the_books()
    net.define("en5", mode="dhcp")
    net.packet("en5", ip="10.99.1.21", mask="255.255.255.0", gateway="10.99.1.254")
    routeops.run_cycle(_intent_text([_apply(_pin_blank_rule(), True)]))
    assert _levied_gateway() == "10.99.1.254"

    _restart_helper()                 # 内存清空，从快照灌回
    net.packets.clear()               # 网卡已是 Manual：租约这一路读不到了
    table.ops.clear()

    routeops.run_cycle(_intent_text([_apply(_pin_blank_rule(), True)]))
    state = _observed_route()
    rule = state["rules"][0]
    assert _levied_gateway() == "10.99.1.254", "重启后账还在"
    assert rule["gateway_src"] == routeops.GATEWAY_SRC_IFACE
    assert rule["gateway"] == "10.99.1.254"
    assert [d["action"] for d in rule["destinations"]] == ["add"]


def test_the_baseline_is_the_last_source_when_the_ledger_and_routes_are_gone(net_env):
    """账空、旧路由也没了（刚开机）⇒ 从 baseline 捡回网关，仍能叠路由。

    2026-10-20 真机：2.20.8 下开机首帧 flush 把账抹平；重启又把上一轮叠的路由清掉了。
    ``_gateway_from_routes`` 的两条证据（账 / 旧路由）同时缺席，只剩 baseline ——
    它是**第一次改这块网卡之前**的原始 DHCP 配置，网关就写在上面。
    """
    tmp_path, table, net = net_env
    _forget_the_books()
    service = net.define("en5", mode="static", ip="10.99.1.34",
                         mask="255.255.255.0", gateway="")
    (tmp_path / "sysrun" / "iface_baseline.json").write_text(json.dumps({
        service: {"iface": "en5", "at": 1, "mode": "dhcp", "ip": "10.99.1.55",
                  "mask": "255.255.255.0", "gateway": "10.99.1.254"}}),
        encoding="utf-8")
    net.packets.clear()                    # Manual：租约这一路读不到
    # 真重启：内存与盘上的账都清掉（快照也删），系统路由表本来就没有这条目标
    helper_state._OBSERVED.clear()
    helper_state._FLUSHED_OBSERVED.clear()
    helper_state._INTENTS.clear()
    helper_state._LAST_SIGNATURE = None
    helper_state._SEEDED = True            # 本用例不测 seed，别让它把旧快照读回来
    (tmp_path / "helper_snapshot.json").unlink(missing_ok=True)

    routeops.run_cycle(_intent_text([_apply(_pin_blank_rule(), True)]))

    rule = _observed_route()["rules"][0]
    assert rule["gateway_src"] == routeops.GATEWAY_SRC_IFACE
    assert rule["gateway"] == "10.99.1.254", "baseline 是账与旧路由都缺时的最后一份证据"
    assert [d["action"] for d in rule["destinations"]] == ["add"]
    assert table.ops[:1] == [("add", "10.99.2.0/24", "10.99.1.254")]
    # 捡回来的也算「学到了」：当场落盘，下一轮不必再捡（否则每轮都要读一次路由表/基线）
    assert (json.loads((tmp_path / "iface_gateways.json").read_text(encoding="utf-8"))
            ["ifaces"]["en5"]["gateway"]) == "10.99.1.254"


def test_the_ledger_is_its_own_file_so_a_restart_cannot_lose_it(net_env):
    """账归账：学到那一刻就落盘，重启（内存与快照全清）之后仍拿得到网关。

    2026-10-19 到 10-20 连着三次真机「场景命中却不叠路由」，机制每次都是同一个：账
    寄生在 route 观测那一块里，而那一块**每轮整体替换** —— 早退漏带、别的 subject 顺手
    flush、新进程首帧 flush，任何一条都能把它抹平。补了三次「让某条路径记得带下去」，
    路径却一直长出来（10-20 那条就是首帧 flush）。

    这条用例只留**账文件**这一份证据：快照删掉、系统里没有那条旧路由、租约读不到
    （网卡已被固化）、baseline 也没有网关可捡。命中后必须仍能 add。
    """
    tmp_path, table, net = net_env
    _forget_the_books()
    net.define("en5", mode="dhcp")
    net.packet("en5", ip="10.99.1.21", mask="255.255.255.0", gateway="10.99.1.254")
    routeops.run_cycle(_intent_text([_apply(_pin_blank_rule(), True)]))

    ledger_file = tmp_path / "iface_gateways.json"
    assert ledger_file.exists(), "学到网关的那一轮就必须落盘（不靠快照、不等下一轮）"
    assert (json.loads(ledger_file.read_text(encoding="utf-8"))
            ["ifaces"]["en5"]["gateway"]) == "10.99.1.254"

    # 真重启：内存全空、快照没了、上一轮叠的路由也没了、网卡已被固化（租约读不到）
    helper_state._OBSERVED.clear()
    helper_state._FLUSHED_OBSERVED.clear()
    helper_state._INTENTS.clear()
    helper_state._LAST_SIGNATURE = None
    helper_state._SEEDED = True            # 本用例不测 seed：快照本来就要删掉
    (tmp_path / "helper_snapshot.json").unlink(missing_ok=True)
    table.ops.clear()
    table.routes.clear()
    net.packets.clear()

    routeops.run_cycle(_intent_text([_apply(_pin_blank_rule(), True)]))

    rule = _observed_route()["rules"][0]
    assert rule["gateway_src"] == routeops.GATEWAY_SRC_IFACE
    assert rule["gateway"] == "10.99.1.254", "账文件是唯一还留着的那份证据"
    assert [d["action"] for d in rule["destinations"]] == ["add"]
    assert table.ops[:1] == [("add", "10.99.2.0/24", "10.99.1.254")]


def test_the_nics_gateway_is_only_used_by_the_scene_that_recorded_it(net_env):
    """账里那个网关**只认它当时依据的那份配置**：改了那份 `net` 就不算数，按取不到办。

    账只按**网卡**索引，而同一块网卡会被不同场景（或同一场景被用户改过之后）配成不同的
    样子。不做这份核对的话，「留空 = 用**本场景**固化那块网卡的网关」里的「本场景」就是
    空话 —— 换过场景之后旧值仍会被新场景的规则认领，而界面上没有任何一栏显示它取的是谁。
    """
    _src, table, net = net_env
    _forget_the_books()
    net.define("en5", mode="dhcp")
    net.packet("en5", ip="10.99.1.21", mask="255.255.255.0", gateway="10.99.1.254")
    first = _pin_blank_rule()
    routeops.run_cycle(_intent_text([_apply(first, True)]))
    assert _levied_gateway() == "10.99.1.254"
    table.ops.clear()

    # 用户把这份场景的「固化地址」改到**另一个网段** ⇒ 规则的 net 变了，而这一轮网卡
    # 已是 Manual、租约这一路读不到（假桩照真机建闸）⇒ 账里那个值不再算数。
    # （改到不同网段是必须的：同网段时系统里那条旧路由仍指向 .254，兜底会把它捡回来 ——
    # 那是**当前事实**，不属于「旧配置的账」，见 ``_gateway_from_routes``。）
    other = _pin_blank_rule()
    other["net"] = {"mode": "dhcp", "pin": {"ip": "192.168.5.99", "mask": "255.255.255.0"}}
    routeops.run_cycle(_intent_text([_apply(other, True)]))

    assert _levied_gateway() == "10.99.1.254", "账本身仍记着旧那份配置的值（只是不认领）"
    rule = _observed_route()["rules"][0]
    assert rule["gateway"] == "" and rule["gateway_src"] == routeops.GATEWAY_SRC_NONE, \
        "旧配置记下的网关不许被新配置认领；跨网段的旧路由也不许捡"
    assert [d["action"] for d in rule["destinations"]] == ["hold"]
    assert table.ops == [], "认不出来就一个命令都不发"


def test_routes_machine_says_the_gateway_it_took(tmp_path, monkeypatch):
    """面板那台机：``gateway_src`` 三个取值各报什么（``rule`` 那一格一个字不变）。"""
    plugin = _plugin(tmp_path, monkeypatch)

    # 写死的那个（缺 ``gateway_src`` 的旧回显也走这里）：摘要一个字不变
    plugin._state = _echo([_rule(dests=("10.99.2.0/24",))])
    assert plugin._routes_report().summary == "已叠加 1 条"

    # 自己从网卡上取到的：那个值用户没写过 ⇒ 摘要里点出来
    plugin._state = _echo([{**_rule(), "gateway": "192.168.11.1",
                            "gateway_src": routeops.GATEWAY_SRC_IFACE}])
    rep = plugin._routes_report()
    assert (rep.state, rep.summary) == ("applied", "已叠加 1 条（下一跳 192.168.11.1）")

    # 取不到 ⇒ 那一份路由一个都没动：WARN，且**不算「已叠加」**（停用句里那个数字
    # 说的是「关掉自动化之后仍然生效着的东西」）
    plugin._state = _echo([{**_rule(), "gateway": "",
                            "gateway_src": routeops.GATEWAY_SRC_NONE,
                            "destinations": [{"destination": "10.99.2.0/24", "ok": True,
                                              "action": "hold"}]}])
    rep = plugin._routes_report()
    assert (rep.state, rep.summary) == ("no_gateway", "未取到网关，1 条路由未动")
    assert plugin._applied_route_count() == 0

    # 真失败（ERR）比它严重：两样同时出现时报失败，不拿警告盖掉真问题
    plugin._state = _echo([
        {**_rule(), "gateway": "", "gateway_src": routeops.GATEWAY_SRC_NONE,
         "destinations": [{"destination": "10.99.2.0/24", "ok": True, "action": "hold"}]},
        _rule(name="home", dest_ok=False),
    ])
    assert plugin._routes_report().state == "failed"
    plugin.stop()


def test_a_scene_that_is_not_matched_raises_no_warning(tmp_path, monkeypatch):
    """**没命中的场景留空不是警告**（2026-10-09 用户裁定）。

    配置里那个固化租约的场景配了留空、你此刻人在另一个场景上 —— 它本轮既不该叠、
    也没有东西可撤。对它报「未取到网关，N 条路由未动」是一句**不成立**的话：本是
    「该动而没动」的意思，可它本来就不该动。代价是面板挂假话、托盘角标被占住。

    与上一条的差别只有 ``applied`` / ``matched`` 两位 —— 那正是新判据唯一多认的东西，
    两条并排才说明「收掉的是没命中的，不是所有留空」。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    plugin._state = _echo([
        {**_rule(applied=False, matched=False),
         "gateway": "", "gateway_src": routeops.GATEWAY_SRC_NONE,
         "destinations": [{"destination": "10.99.2.0/24", "ok": True, "action": "hold"}]},
    ])
    rep = plugin._routes_report()
    assert (rep.state, rep.summary) == ("none", ""), "没命中 ⇒ 只是「未叠加」"
    plugin.stop()


def test_an_unmatched_scene_does_not_hide_a_taken_gateway(tmp_path, monkeypatch):
    """放宽判据的另一半理由（也是用户看得见的那一半）：别把取到的网关挤掉。

    一条真叠上了（下一跳是从网卡取的），另一条配了留空可此刻没命中 —— 面板该报的是
    「已叠加 1 条（下一跳 192.168.11.1）」。按旧判据这里会变成「未取到网关，1 条路由未动」，
    于是**灯在报警、而用户想核对的那个网关反而看不见了**。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    plugin._state = _echo([
        {**_rule(dests=("10.99.2.0/24",)), "gateway": "192.168.11.1",
         "gateway_src": routeops.GATEWAY_SRC_IFACE},
        {**_rule(name="office", applied=False, matched=False, dests=("10.2.0.0/16",)),
         "gateway": "", "gateway_src": routeops.GATEWAY_SRC_NONE,
         "destinations": [{"destination": "10.2.0.0/16", "ok": True, "action": "hold"}]},
    ])
    rep = plugin._routes_report()
    assert (rep.state, rep.summary) == ("applied", "已叠加 1 条（下一跳 192.168.11.1）")
    plugin.stop()


def test_the_warning_counts_only_the_scenes_that_should_have_acted(tmp_path, monkeypatch):
    """那句「N 条路由未动」的 **N 只数该上场的那条**（判据与计数共用同一份名单）。

    两个场景都留空、都取不到网关：一条本轮该叠（2 个目标），一条没命中（3 个目标）。
    该报的是 2 —— 没命中的那 3 条本来就不该动，算进「未动」是把话说满。
    判据与 ``n`` 各写一遍自己的筛选就会变成 5（``reports`` 那条注释说的正是这件事）。
    """
    plugin = _plugin(tmp_path, monkeypatch)
    act = ("10.99.2.0/24", "10.2.0.0/16")
    idle = ("10.3.0.0/16", "10.4.0.0/16", "10.5.0.0/16")
    plugin._state = _echo([
        {**_rule(), "gateway": "", "gateway_src": routeops.GATEWAY_SRC_NONE,
         "destinations": [{"destination": d, "ok": True, "action": "hold"} for d in act]},
        {**_rule(name="office", applied=False, matched=False),
         "gateway": "", "gateway_src": routeops.GATEWAY_SRC_NONE,
         "destinations": [{"destination": d, "ok": True, "action": "hold"} for d in idle]},
    ])
    rep = plugin._routes_report()
    assert (rep.state, rep.summary) == ("no_gateway", "未取到网关，2 条路由未动")
    plugin.stop()


# ──────────────────────────────────────────────
# 「恢复 DHCP」那条路（一次性命令）也要记账
#
# 真机现场（2026-10-09）：回执说「已恢复 DHCP 并固化：10.99.1.54」，面板却一直报
# 「未取到网关」。根因是**命令与周期是两条路** —— 周期那条（``_converge_iface``）记
# ``iface_gateway``，命令那条（``ops._apply_op``）不记。偏偏那个值**只有这条路上读得到**：
# 命令把网卡切成 Manual 之后，``ipconfig getpacket`` 再也读不到租约。
# ──────────────────────────────────────────────


def test_renew_reads_the_gateway_out_of_the_new_lease(net_env, monkeypatch):
    """「恢复 DHCP」把新租约里的网关一并交出来（``iface_gateway``）。

    写下去的那一份里没有它 —— 固化只写地址（``wrote.gateway`` 是空串）。这个值只在
    租约里，而这条命令是它**唯一**的来处（网卡一切成 Manual 就读不到了）。
    """
    _src, _table, net = net_env
    net.define("en5", mode="static", ip="10.99.1.10", mask="255.255.255.0")
    net.packet("en5", ip="10.99.1.21", mask="255.255.255.0", gateway="10.99.1.254")
    monkeypatch.setattr(routeops_routes, "current_ipv4", lambda iface: ["10.99.1.21"])

    out = routeops._apply_op(
        {"iface": "en5", "op": "renew", "dns": False, "net": _pin_lease_net()},
        _NOW)

    assert out["action"] == "applied" and out["result"] == "pinned"
    assert out["wrote"]["gateway"] == "", "只固化地址：写下去的那份里确实没有网关"
    assert out["iface_gateway"] == "10.99.1.254", \
        "租约里的网关要带出去（它是留空下一跳唯一可能的来处）"


def test_renew_hands_the_gateway_to_the_ledger(net_env, monkeypatch):
    """这条命令读到的网关要进那本账 —— 而且留空的那几行**当轮**就能用它叠上。

    时序照真机走：组件早就在跑（网卡被固化着 ⇒ 没有租约可读 ⇒ 账一直是空的），
    用户按下「恢复 DHCP」，网关就藏在那一份**新租约**里。
    """
    _src, table, net = net_env
    _forget_the_books()
    net.define("en5", mode="dhcp")
    net.packet("en5", ip="10.99.1.21", mask="255.255.255.0")      # 这一轮还没有网关
    routeops.run_cycle(_intent_text([_apply(_pin_blank_rule(), True)]))
    assert _levied_gateway() == "", "起点：账还是空的（周期那条路读不到网关）"

    # 用户按下「恢复 DHCP」：网卡短暂回到 DHCP，等来一份**带网关**的新租约
    net.packet("en5", ip="10.99.1.54", mask="255.255.255.0", gateway="10.99.1.254")
    # ``_wait_lease`` 有两条判据，缺一不可：报文里有 yiaddr **且**那个地址确实在网卡的
    # IPv4 列表里（新鲜度 —— 刚 setdhcp 完，报文可能还是上一份缓存的）。假桩里后者由这
    # 条 patch 提供；真机上它是 ``ifconfig`` 说的。
    monkeypatch.setattr(routeops_routes, "current_ipv4", lambda iface: ["10.99.1.54"])
    routeops.run_net_op("renew", {"iface": "en5", "op": "renew", "dns": False,
                                  "net": _pin_lease_net()})

    assert _levied_gateway() == "10.99.1.254", "这条路上读到的网关也要记进那本账"
    assert _levied()["net"] == _pin_lease_net(), "连它依据的那份配置一起记"

    # 下一轮：账里有了 ⇒ 留空的那几行当轮就叠上
    table.ops.clear()
    routeops.run_cycle(_intent_text([_apply(_pin_blank_rule(), True)]))
    got = _observed_route()["rules"][0]
    assert got["gateway_src"] == routeops.GATEWAY_SRC_IFACE
    assert got["gateway"] == "10.99.1.254"
    assert table.ops == [("add", "10.99.2.0/24", "10.99.1.254")], \
        "账里有了就不该再是 hold"


def test_a_failed_renew_keeps_the_gateway_we_already_knew(net_env, monkeypatch):
    """renew 没成（等不到新租约）时，账里那个网关**不许被抹掉**。

    「只拿合法值去更新」这条纪律在**入口侧**的这一半：失败段里没有 ``iface_gateway``，
    账照旧带着上一轮那份走 —— 否则一次瞬时失败就会让一条健康的配置突然「取不到网关」。
    """
    _src, _table, net = net_env
    _forget_the_books()
    net.define("en5", mode="dhcp")
    net.packet("en5", ip="10.99.1.21", mask="255.255.255.0", gateway="10.99.1.254")
    routeops.run_cycle(_intent_text([_apply(_pin_blank_rule(), True)]))
    assert _levied_gateway() == "10.99.1.254"

    # 这一次等不到新租约：网卡交还了 DHCP，但没发下来
    net.packets.clear()
    real_wait = routeops._wait_lease
    monkeypatch.setattr(routeops_lease, "_wait_lease",
                        lambda iface: real_wait(iface, timeout=0.0))
    out = routeops.run_net_op("renew", {"iface": "en5", "op": "renew", "dns": False,
                                        "net": _pin_lease_net()})

    assert out["action"] == "failed", "这一步本来就没成"
    assert _levied_gateway() == "10.99.1.254", "失败不许把已经知道的抹掉"
