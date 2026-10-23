"""批次 4 · 能力封闭契约层门禁与单测（`capabilities/helper/`，方案 §7.4）。

这里的每一条都对应方案 §7.4.4 / §7.4.5 的一条硬约束。它们是「把约束从文档
变成 CI」的执行者——防止日后有人以「加个例外」的名义重新打开能力口子。
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from barkit import paths
from barkit.capabilities.helper import actions, core, rootbin, routeops, safeio, specs
from barkit.core import installer
from barkit.plugins.route_guard import checks as route_checks
from barkit.plugins.route_guard import intent as intent_mod

SRC = Path(__file__).resolve().parents[1] / "src" / "barkit"
HELPER_PKG = SRC / "capabilities" / "helper"
# hev 的唯一分发位（批次 4 · S4 起从 route_guard/resources/ 迁到使用方 ipad_tunnel）。
HEV_SRC = SRC / "plugins" / "ipad_tunnel" / "resources" / "hev-socks5-tunnel"

STDLIB_OK = {
    "__future__",
    # 契约层（rootbin / specs / actions）
    "dataclasses", "hashlib", "ipaddress", "re", "typing", "pathlib",
    # 运行期框架（__main__ / core / safeio / state / version）
    "os", "json", "sys", "time", "datetime", "atexit", "signal", "subprocess",
    # 隧道运行时：上游 SOCKS 探活（socket）、DNS 协议解析（struct）、
    # DoH 直查（urllib.parse / urllib.request）、hev yaml 合成（random）
    "socket", "struct", "urllib", "random",
    # 会话传输层（2026-09-29）：selectors 做「空闲纯阻塞」的主循环，
    # errno 只为把 EINTR（被信号打断的 select）与真错误分开；
    # stat 只为「会话节点是不是被残留的旧进程占着」这一条判定。
    "selectors", "errno", "stat",
}


def _helper_modules() -> list[Path]:
    """helper 包的**全部** ``.py``（含子包）。

    曾经是 ``glob("*.py")``（只顶层）。helper 在 2026-10-02 拆出 ``routeops/`` 与
    ``tunnelops/`` 两个子包 ⇒ 这条族（标准库白名单 / 3.9 语法 / 禁动态执行 /
    落盘必走 ``safeio`` / 禁用户域 exec）的扫描面当场少了 21 个文件，**一条都没报错**。
    判据是「这一族」而不是「点名某几个文件」，所以扫描面必须跟着包结构走。
    """
    return sorted(p for p in HELPER_PKG.rglob("*.py") if "__pycache__" not in p.parts)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:                      # 包内相对导入，不计入外部依赖
                continue
            if node.module:
                found.append(node.module)
    return found


# ──────────────────────────────────────────────
# 能力集封闭性
# ──────────────────────────────────────────────

def test_action_schemas_are_closed_and_wellformed():
    """能力集非空、全部是编译期常量、每条 schema 都是 Spec 对象。"""
    assert actions.ACTION_SCHEMAS, "能力集不得为空"
    for name, schema in actions.ACTION_SCHEMAS.items():
        assert re.fullmatch(r"[a-z][a-z0-9_.]*", name), f"原语名不规范：{name}"
        assert isinstance(schema, dict), f"{name} schema 需为字典"
        for field, spec in schema.items():
            assert isinstance(field, str) and field
            assert isinstance(spec, specs.Spec), f"{name}.{field} 不是 Spec：{spec!r}"
        # 能力集必须可哈希化（运行期只读）——用 frozenset 固化快照
    assert isinstance(actions.action_names(), frozenset)
    assert actions.action_names() == frozenset(actions.ACTION_SCHEMAS)


def test_action_names_have_no_command_semantics():
    """任何形态的 run_command / exec / script / eval 一律不得存在（§7.4.4）。"""
    for name in actions.ACTION_SCHEMAS:
        low = name.lower()
        for word in actions.FORBIDDEN_ACTION_WORDS:
            assert word not in low, f"原语名含命令类语义 {word!r}：{name}"


def test_helper_no_path_param():
    """请求参数中不得出现任何路径类字段（§7.4.4 红线）。"""
    for name, schema in actions.ACTION_SCHEMAS.items():
        hits = specs.path_like_field_names(schema)
        assert not hits, f"{name} 含路径类参数：{hits}"
        for spec in schema.values():
            assert spec.kind not in {"path", "file", "dir", "directory"}, (
                f"{name} 使用了路径类 spec：{spec!r}"
            )


def test_param_spec_kinds_are_closed_set():
    """参数类型只用封闭白名单里的值域类型（无自由文本容器、无路径）。"""
    allowed = {"text", "ipv4", "cidr", "iface", "service", "ssid", "hostname", "port", "choice", "list"}
    for name, schema in actions.ACTION_SCHEMAS.items():
        for field, spec in schema.items():
            assert spec.kind in allowed, f"{name}.{field} 用了未登记的值域类型 {spec.kind}"


def test_proc_manage_names_derive_from_root_binaries():
    """proc.manage 的可操作进程名必须从 A 类封闭集派生，不另立真源。"""
    schema = actions.ACTION_SCHEMAS["proc.manage"]
    assert tuple(schema["name"].options) == tuple(sorted(rootbin.known_names()))


def test_file_write_block_target_is_constant():
    """file.write_block 只服务一个编译期常量目标 —— marker 不得成为路径替身。"""
    assert actions.FILE_BLOCK_TARGET == "/etc/hosts"
    assert "path" not in actions.ACTION_SCHEMAS["file.write_block"]
    for field in actions.ACTION_SCHEMAS["file.write_block"]:
        assert field in {"marker", "content"}


# ──────────────────────────────────────────────
# 参数校验器
# ──────────────────────────────────────────────

@pytest.mark.parametrize("action", sorted(actions.ACTION_SCHEMAS))
def test_validate_params_rejects_unknown_field(action):
    """未知字段整份拒绝（§7.4.5 P2）。"""
    ok, reason = actions.validate_params(action, {"__nope__": 1})
    assert not ok and "未知参数" in reason


def test_validate_params_rejects_unknown_action():
    ok, reason = actions.validate_params("run_command", {})
    assert not ok and "未知原语" in reason


def test_validate_params_happy_path():
    ok, reason = actions.validate_params(
        "route.ensure", {"dest": "10.0.0.0/8", "gateway": "192.168.1.1", "iface": "utun3"}
    )
    assert ok, reason


@pytest.mark.parametrize(
    "action,params",
    [
        ("route.ensure", {"dest": "/etc/passwd", "gateway": "192.168.1.1", "iface": "utun3"}),
        ("route.ensure", {"dest": "10.0.0.0/8", "gateway": "not-an-ip", "iface": "utun3"}),
        ("route.ensure", {"dest": "10.0.0.0/8", "gateway": "192.168.1.1", "iface": "en0; rm -rf /"}),
        ("dns.set", {"service": "Wi-Fi", "servers": ["8.8.8.8", "not-ip"]}),
        ("wifi.select", {"ssid": ""}),
        ("net.resolve", {"host": "ok.example", "method": "whatever"}),
        ("proc.manage", {"name": "rm", "action": "start"}),
        ("proc.manage", {"name": "hev-socks5-tunnel", "action": "kill"}),
    ],
)
def test_validate_params_rejects_bad_values(action, params):
    ok, _reason = actions.validate_params(action, params)
    assert not ok


def test_dns_restore_takes_no_params():
    assert actions.validate_params("dns.restore", {})[0]
    assert not actions.validate_params("dns.restore", {"x": 1})[0]


# ──────────────────────────────────────────────
# 意图线格式（现行 v4；net / handback / 空 destinations 是 v3 那批）
# ──────────────────────────────────────────────
#
# 下面几条是「app 与 root helper 说的是同一份契约」的判据。线格式是**双向**约束：
# app 多送一个 helper 不认识的键 ⇒ 整份拒绝 + 冻结（用户看到的是「路由突然不动了」）；
# helper 认识一个 app 永不发的形状 ⇒ 那条路没人验证，坏了也看不出来。
# **版本位**把这两条兜在一起（2026-09-19 真锁）：写着的版本 ≠ 本 helper 认的那一版
# —— 含缺位 —— 一律拒绝，不去猜「哪边更新、剩下的是不是还能用」。

def _intent_rule(**kw) -> dict:
    """一条意图规则（app 侧 ``intent.build_intent`` 的产物形状）：必带 ``apply``。"""
    rule = {"name": "office", "priority": 1, "iface": "en5",
            "gateway": "10.199.1.254", "destinations": ["10.199.2.0/24"],
            "enabled": True, "apply": True}
    rule.update(kw)
    return rule


def test_intent_version_is_single_sourced_across_both_sides():
    """版本号只有一处真源，两侧常量必须相等。

    ``HELPER_VERSION`` 是另一件事（改线格式就得跟着**重装**），但它与
    ``INTENT_VERSION`` 在同一个提交里动过 —— 旧 helper 会整份拒绝新意图，
    那是安全态（保持上轮路由不动），依赖它「自动兼容」才是危险的。
    """
    assert routeops.INTENT_VERSION == intent_mod.INTENT_VERSION

    major = int(str(core.HELPER_VERSION).split(".")[0])
    assert major == 2, "helper 版本大版本号变了要单独裁决（升大版本＝换契约窗口）"


def test_intent_version_mismatch_is_refused_not_ignored():
    """契约版本**不符即整份拒绝**（ROADMAP E4 ②「不符即冻结/告警」）。

    落地前这一位只是字段白名单里的一个名字：``validate_routes`` 收下就扔，
    ``validate_intent`` 末尾还反过来「盖自己的号」⇒ 文件里写什么都能进来，
    「两侧不是同一代」在 root 侧完全看不出来。现在三档都拒：
    **旧一版 / 新一版 / 压根没写**（缺位证明不了两侧同代）。报错必须给全**两侧**
    版本号，否则用户不知道该重装哪一头。
    """
    def _err(declared):
        raw: dict = {"poll": 30, "rules": []}
        if declared is not None:
            raw["version"] = declared
        return routeops.validate_routes(raw)[1]

    stale = routeops.INTENT_VERSION - 1
    for err in (_err(stale), _err(routeops.INTENT_VERSION + 1), _err(None)):
        assert err and "契约版本不符" in err
    assert f"v{routeops.INTENT_VERSION}" in _err(stale), "要说清 helper 认哪一版"
    assert str(stale) in _err(stale), "也要说清文件写的是哪一版"

    # 两个入口都得过这把锁（``validate_intent`` 复用 ``validate_routes``）——
    # 只锁一个门等于没锁：真机上跑的是意图那条路。
    _, err = routeops.validate_intent(
        {"version": stale, "poll": 30, "rules": [_intent_rule()]})
    assert err and "契约版本不符" in err

    # 对上号的那一版照旧通过（锁不是「一律拒绝」）
    data, err = routeops.validate_routes(
        {"version": routeops.INTENT_VERSION, "poll": 30, "rules": []})
    assert err is None and data["version"] == routeops.INTENT_VERSION


#: **契约形状指纹**：形状一变，就得连 ``HELPER_VERSION`` 一起动（旧的要重装）。
#:
#: 为什么需要它（真机事故实录，2026-09-18）：顶层新增 ``handback_dns`` 时漏了 bump
#: 版本号 —— `test_intent_wire_format_top_level_keys_are_closed` 照常绿（它的期望集
#: 合同步改了，形状看着"闭合"），但真机上装着的旧 helper 看到不认识的顶层键就
#: **整份拒绝 ＋ 冻结**，而 app 判「该重装」唯一靠版本号 ⇒ 面板报「规则文件含未知
#: 字段」却**没有任何重装提示**，用户无从修。抓的就是「形状变了 · 版本没变」这个组合。
#:
#: 指纹只为形状而设，**不绑定版本号** —— 纯行为 / 安全变更（只 bump 版本号）不致红，
#: 零误报。
#:
#: 变更史（新在前）：
#: - ``c383de47deb7``（2026-10-09）：**``gateway`` 允许空串**（留空 ＝ 由 helper 从本场景
#:   固化那块网卡上取那个网关；只对带 ``pin`` 的 ``net`` 成立），意图契约 7 → 8。
#:   **键没增没减，改的是语义** —— 这个指纹照样动，因为它把 ``intent_version`` 算在内：
#:   版本一变就是「两侧不是同一代」，真机上那份旧 helper 会整份拒绝新意图并冻结。
#:   同批 ``HELPER_VERSION`` → 2.20.0。
#: - ``ac8a65063d5c``（2026-10-06）：每条规则加 ``off_destinations``（逐目标开关：本条里
#:   要撤的那几个目标，``destinations`` 的子集），意图契约 6 → 7。同批
#:   ``HELPER_VERSION`` → 2.19.0。
#: - ``4df15db0da0c``（2026-09-29）：**顶层去掉 ``ops``**（命令搬去会话帧 ``op.run``），
#:   意图契约 5 → 6；命令的参数形状仍在指纹里，只是换了名字（``ops_*`` → ``op_*``，
#:   因为它现在是 ``op.run`` 帧的 ``params``，不再是意图正文的一个字段）。
#:   同批 ``HELPER_VERSION`` → 2.17.0。
#: - ``870ca741b746``（2026-09-29）：顶层加 ``ops``（用户按下的一次显式命令，与
#:   ``handback`` 这个策略分开），意图契约 4 → 5。同批 ``HELPER_VERSION`` → 2.16.0。
_HELPER_CONTRACT_SHAPE_FINGERPRINT = "c383de47deb7"


def _contract_shape() -> dict:
    """契约形状（只有键集合与闭集取值，不含值）：动一个名字，指纹就变。"""
    return {
        "intent_version": routeops.INTENT_VERSION,
        "intent_top_level": sorted(routeops._SCHEMA),
        "rule_fields": sorted(routeops._RULE_FIELDS),
        "net_modes": sorted(routeops._NET_MODES),
        "net_fields": sorted(routeops._NET_FIELDS),
        "pin_fields": sorted(routeops._PIN_FIELDS),
        "op_fields": sorted(routeops._OP_FIELDS),
        "op_kinds": sorted(routeops._OP_KINDS),
        "route_get_keys": sorted(routeops._ROUTE_GET_KEYS),
    }


def test_helper_contract_shape_change_requires_version_bump():
    """契约形状变了 ⇒ 同批必须 bump ``HELPER_VERSION``（改契约必重装）。

    过不了这条时的修法：① 确认这次形状变更**确实**要分发（若是误加，回退它）；
    ② bump ``capabilities/helper/version.py`` 的 ``HELPER_VERSION`` ＋ 写一条变更史；
    ③ 把报错里打印的新指纹写回 ``_HELPER_CONTRACT_SHAPE_FINGERPRINT``。
    """
    digest = hashlib.sha256(
        json.dumps(_contract_shape(), sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    assert digest == _HELPER_CONTRACT_SHAPE_FINGERPRINT, (
        "helper 契约形状变了：\n"
        f"  当前形状指纹 = {digest!r}（锚点写的是 {_HELPER_CONTRACT_SHAPE_FINGERPRINT!r}）\n"
        f"  当前 HELPER_VERSION = {core.HELPER_VERSION!r}（形状变了它必须一起变）\n"
        f"  当前形状 = {json.dumps(_contract_shape(), sort_keys=True, ensure_ascii=False)}\n"
        "新增 / 删除 / 改名线格式字段、动闭集取值、动意图版本 —— 都算形状变更：必须同批"
        "bump HELPER_VERSION 并把新指纹写回锚点。只改形状不 bump，真机上的旧 helper 会"
        "整份拒绝新意图并冻结，而 app 判不出「该重装」，用户看到报错却无从修。"
    )


def test_intent_wire_format_top_level_keys_are_closed():
    """顶层键闭集：``handback``（第 8 步）进契约，别的一律不接受。

    2026-09-29：``ops`` **不在这里了** —— 命令搬去了会话帧 ``op.run``。这不是「删一个
    字段」，是「命令与策略分家」的落点：``handback`` 是**策略**（无场景接管时交还，
    每轮可重复、幂等），命令是**用户按下的一次显式动作**（必须执行且只能执行一次）。
    两者挤在同一个顶层字段时，命令被策略的前提焊死 —— 用户按「恢复 DHCP」什么也没发生
    （真机复现两次）。现在命令走一条独立的路（``op.run``），既不受 ``paused`` 影响，
    也不进这份期望态。
    """
    data, err = routeops.validate_intent({
        "version": routeops.INTENT_VERSION, "poll": 30, "paused": False,
        "handback": ["en5"], "handback_dns": True, "rules": [_intent_rule()],
    })
    assert err is None, err
    assert set(data) == {"version", "poll", "paused", "handback", "handback_dns", "rules"}
    assert data["version"] == routeops.INTENT_VERSION, "版本由真锁校验（不再「盖自己的号」）"
    assert data["handback"] == ["en5"]

    # 还在写 ops 的一方 = 旧契约的产物 ⇒ 整份拒绝（不静默吞掉一条命令）
    _, err = routeops.validate_intent({
        "version": routeops.INTENT_VERSION, "poll": 30, "rules": [_intent_rule()],
        "ops": [{"seq": 1, "iface": "en5", "op": "renew"}],
    })
    assert err and "未知字段" in err, "命令不再住在期望态里：写进来就是旧代产物，拒"

    _, err = routeops.validate_intent({
        "version": routeops.INTENT_VERSION, "poll": 30, "rules": [_intent_rule()],
        "evil": 1,
    })
    assert err and "未知字段" in err

    # ``handback`` 自己也要闭合：字符串数组，空白项不算
    _, err = routeops.validate_intent({
        "version": routeops.INTENT_VERSION, "poll": 30,
        "handback": ["  "], "rules": [_intent_rule()],
    })
    assert err and "handback" in err


def test_op_params_are_closed_and_each_one_is_checked():
    """一次性命令的参数形状：闭集字段 ＋ 逐项校验。

    这一格是**用户按下的命令**、helper 是 root：形状不对就**拒绝这一帧**（宁可不动，
    也不动半件）。与旧 ``ops`` 字段的差别只有一个 —— 没有 ``seq`` 了：旧形态靠它判
    「执行过没有」，而帧本身就是「一次」（一帧一命令、回帧即回执），不需要任何跨重启
    的状态，也就没有水位线活不过重启这件事。
    """
    def _ok(params):
        return routeops.validate_op_params(params)

    # 合法：renew 带 net，release 不带
    clean, err = _ok({"iface": "en5", "op": "renew",
                      "net": {"mode": "dhcp", "pin": {"ip": "", "mask": ""}}})
    assert err is None, err
    assert clean["op"] == "renew" and clean["dns"] is True, "dns 缺省 True（与 handback_dns 同口径）"
    assert _ok({"iface": "en5", "op": "release"})[1] is None

    # 未知字段 → 拒（``seq`` 就在这一类：它是旧形态的字段，写进来要能看出来）
    _, err = _ok({"iface": "en5", "op": "renew", "evil": 1})
    assert err and "未知字段" in err
    _, err = _ok({"iface": "en5", "op": "renew", "seq": 1})
    assert err and "未知字段" in err
    # 未知动作种类 → 拒
    _, err = _ok({"iface": "en5", "op": "reboot"})
    assert err and "op" in err
    # iface 空 → 拒
    _, err = _ok({"iface": "  ", "op": "renew"})
    assert err and "iface" in err
    # 不是对象 → 拒
    _, err = _ok([1])
    assert err and "对象" in err
    # net 走 validate_net 的同一份口径
    _, err = _ok({"iface": "en5", "op": "renew",
                  "net": {"mode": "dhcp", "ip": "10.0.0.5"}})
    assert err and "net" in err
    # dns 非布尔 → 拒
    _, err = _ok({"iface": "en5", "op": "renew", "dns": "yes"})
    assert err and "dns" in err


def test_intent_handback_dns_is_preserved_and_accepted():
    """``handback_dns`` 进契约：``validate_intent`` 收得下、透传（缺省 True）。"""
    data, err = routeops.validate_intent({
        "version": routeops.INTENT_VERSION, "poll": 30, "paused": False,
        "handback": ["en5"], "handback_dns": False, "rules": [_intent_rule()],
    })
    assert err is None, err
    assert data["handback_dns"] is False, "显式 False 要透传"


def test_intent_rule_net_goes_through_the_same_validate_net():
    """规则里的 ``net`` 走 ``validate_net`` 的同一份口径 —— helper 是 root，不信送进来的东西。"""
    ok, err = routeops.validate_intent({
        "version": routeops.INTENT_VERSION, "poll": 30,
        "rules": [_intent_rule(net={"mode": "static", "ip": "10.199.1.10",
                                    "mask": "255.255.255.0"})],
    })
    assert err is None, err
    assert ok["rules"][0]["net"] == {"mode": "static", "ip": "10.199.1.10",
                                     "mask": "255.255.255.0"}

    # 不写 net ⇒ 产物里根本没有这个键（不是写了一空段）
    ok, err = routeops.validate_intent({
        "version": routeops.INTENT_VERSION, "poll": 30, "rules": [_intent_rule()],
    })
    assert err is None and "net" not in ok["rules"][0]

    # 非法形状整份拒绝，且错误里点得出是第几条规则
    _, err = routeops.validate_intent({
        "version": routeops.INTENT_VERSION, "poll": 30,
        "rules": [_intent_rule(net={"mode": "dhcp", "ip": "10.0.0.5"})],
    })
    assert err and err.startswith("rules[0].") and "不得直接写" in err


def test_intent_allows_a_scene_with_no_routes_at_all():
    """``destinations`` 允许空数组（第 8 步）：一个场景可以「只切 IP、不加路由」。

    此时 ``gateway`` 也就不必填 —— 逼用户为一条不存在的路由编一个网关地址，只会让
    他编一个假的。反过来，**有目标就必须有下一跳**（``ensure_route`` 不接受空网关）。
    """
    ok, err = routeops.validate_intent({
        "version": routeops.INTENT_VERSION, "poll": 30,
        "rules": [_intent_rule(destinations=[], gateway="")],
    })
    assert err is None, err
    assert ok["rules"][0]["destinations"] == []

    _, err = routeops.validate_intent({
        "version": routeops.INTENT_VERSION, "poll": 30,
        "rules": [_intent_rule(destinations=["10.199.2.0/24"], gateway="")],
    })
    assert err and "gateway" in err


def test_intent_allows_a_blank_gateway_only_for_a_pinned_lease_scene():
    """v8：有目标 ＋ 空 ``gateway`` ⇒ 只有**固化租约的场景**（带 ``pin`` 的 ``net``）收。

    空串的语义是「这个下一跳由你从本场景固化那块网卡上取」—— 那个值在租约里，用户
    写不出来（这正是 app 侧报的那个坑：界面说「留空可以」，实际保存不了）。
    它**不是**一句通用许可：跟随系统与静态 IP 那两种 ``net`` 都给不出网卡网关，
    收下它们等于让 helper 对着一个空的下一跳去加路由。
    """
    blank = _intent_rule(destinations=["10.199.2.0/24"], gateway="")
    ok, err = routeops.validate_intent({
        "version": routeops.INTENT_VERSION, "poll": 30,
        "rules": [{**blank, "net": {"mode": "dhcp", "pin": {"ip": "", "mask": ""}}}],
    })
    assert err is None, err
    assert ok["rules"][0]["gateway"] == "", "空串要原样透传：它是信息，不是「假值」"
    assert ok["rules"][0]["net"]["pin"] == {"ip": "", "mask": ""}

    # 静态 IP / 纯 DHCP / 压根没有 net：三种都不给出来源 ⇒ 照旧拒
    for net in (None, {"mode": "dhcp"},
                {"mode": "static", "ip": "10.0.0.5", "mask": "255.255.255.0"}):
        rule = dict(blank) if net is None else {**blank, "net": net}
        _, err = routeops.validate_intent(
            {"version": routeops.INTENT_VERSION, "poll": 30, "rules": [rule]})
        assert err and "gateway" in err, f"net={net!r} 不该被放过：{err!r}"


def test_gateway_src_vocabulary_matches_on_both_sides():
    """``gateway_src`` 那三个字面量在两侧必须逐字相同（第二个实现拿不到 Python 常量）。

    写的一侧是 helper（``routeops.GATEWAY_SRC_*``，回显是它造的），读的一侧是插件
    （``checks.GATEWAY_SRC_*``，面板那格状态与那句摘要都按它分派）。两侧不同包、不同
    进程，必然各写一份 —— 这份重复没有任何运行时机制会发现，只能由这条用例钉住：
    改一边不改另一边，它当场转红。
    """
    ok, err = routeops.validate_intent({
        "version": routeops.INTENT_VERSION, "poll": 30,
        "rules": [_intent_rule(destinations=[], gateway="")],
    })
    assert err is None
    assert (routeops.GATEWAY_SRC_RULE, routeops.GATEWAY_SRC_IFACE,
            routeops.GATEWAY_SRC_NONE) == (route_checks.GATEWAY_SRC_RULE,
                                           route_checks.GATEWAY_SRC_IFACE,
                                           route_checks.GATEWAY_SRC_NONE)
    # 三个取值互不相同（复制粘贴最容易漏的就是这一条）
    assert len({routeops.GATEWAY_SRC_RULE, routeops.GATEWAY_SRC_IFACE,
                routeops.GATEWAY_SRC_NONE}) == 3


# ──────────────────────────────────────────────
# task 表五条红线（§7.4.4）
# ──────────────────────────────────────────────

PLUGINS = ("route_guard", "ipad_tunnel", "hosts_guard")


def _task(**kw) -> dict:
    base = {
        "id": "route_guard.sync",
        "action": "route.sync",
        "subject": "route",
        "interval": 30,
        "enabled": True,
    }
    base.update(kw)
    return base


def test_task_happy_path():
    ok, reason = actions.validate_task(_task(), registered_plugin_ids=PLUGINS)
    assert ok, reason


def test_redline5_no_inline_params():
    ok, reason = actions.validate_task(_task(params={"gateway": "1.1.1.1"}), registered_plugin_ids=PLUGINS)
    assert not ok and "params" in reason


def test_redline1_subject_must_be_in_closed_set():
    """红线 1 在 2026-09-29 换了对象：从「``params_from`` 是合法文件名」改成「``subject``
    在闭集里」。

    旧的那条校验防的是「root 被引到数据目录外读东西」；期望态搬进会话之后 root 侧
    **不再读任何文件**，那条校验没有对象可作用了。新对象是 ``subject``（它决定这条
    task 去读哪一份意图）。放宽了吗？没有 —— 旧形态放行「数据目录内的任意文件」，
    新形态连文件名都不接受。
    """
    for bad in ("", "  ", "routes", "route.json", "../route", "tasks", None, 1):
        ok, reason = actions.validate_task(_task(subject=bad), registered_plugin_ids=PLUGINS)
        assert not ok, f"{bad!r} 应被拒绝"
        assert "subject" in reason
    # 闭集里那三个照旧通过
    for good in actions.TASK_SUBJECTS:
        assert actions.validate_task(_task(subject=good), registered_plugin_ids=PLUGINS)[0], good


def test_redline1_forbids_the_old_params_from_key():
    """旧字段 ``params_from`` 不再在白名单里 ⇒ 写进来就是未知字段，整条拒。

    这条是「换位换干净了」的判据：只要白名单里还留着它，就会出现「读的是 subject、
    却也接受一个没人用的 params_from」这种两条路并存的状态。
    """
    ok, reason = actions.validate_task(
        _task(params_from="routes.json"), registered_plugin_ids=PLUGINS)
    assert not ok and "未知字段" in reason


def test_redline2_task_id_binds_plugin():
    ok, reason = actions.validate_task(_task(id="evil.sync"), registered_plugin_ids=PLUGINS)
    assert not ok and "前缀不是已注册插件" in reason
    assert not actions.validate_task(_task(id="noDot"), registered_plugin_ids=PLUGINS)[0]


def test_redline3_quota_per_plugin_and_total():
    counts = {"route_guard": actions.MAX_TASKS_PER_PLUGIN}
    ok, reason = actions.validate_task(_task(), registered_plugin_ids=PLUGINS, plugin_task_counts=counts)
    assert not ok and "上限" in reason

    ok, reason = actions.validate_task(
        _task(), registered_plugin_ids=PLUGINS, total_tasks=actions.MAX_TASKS_TOTAL
    )
    assert not ok and "总量" in reason


def test_redline4_interval_lower_bound():
    assert not actions.validate_task(_task(interval=4.9), registered_plugin_ids=PLUGINS)[0]
    assert actions.validate_task(_task(interval=5), registered_plugin_ids=PLUGINS)[0]
    assert not actions.validate_task(_task(interval=True), registered_plugin_ids=PLUGINS)[0]
    assert not actions.validate_task(_task(interval="30"), registered_plugin_ids=PLUGINS)[0]


def test_task_unknown_field_rejected():
    ok, reason = actions.validate_task(_task(extra=1), registered_plugin_ids=PLUGINS)
    assert not ok and "未知字段" in reason


def test_task_action_must_be_in_closed_set():
    ok, reason = actions.validate_task(_task(action="run_command"), registered_plugin_ids=PLUGINS)
    assert not ok and "封闭集" in reason


def test_task_table_rejects_duplicate_id():
    ok, reason = actions.validate_task_table([_task(), _task()], registered_plugin_ids=PLUGINS)
    assert not ok and "重复" in reason


def test_task_table_enforces_total_quota():
    # 5 个插件 × 7 条 = 35 > 32 总量上限；单插件均未越界
    tasks = []
    for pid in ("p0", "p1", "p2", "p3", "p4"):
        for i in range(7):
            tasks.append(_task(id=f"{pid}.t{i}"))
    ok, reason = actions.validate_task_table(tasks, registered_plugin_ids=("p0", "p1", "p2", "p3", "p4"))
    assert not ok and "总量" in reason


def test_op_actions_are_closed_and_named_by_constant():
    """命令名的闭集在 ``primitives`` 里，是编译期常量 —— 运行期无法引入新的命令。

    与 ``ACTION_SCHEMAS`` 同一条纪律：报文里出现闭集之外的 op 一律拒绝，不存在
    「按名字动态找实现」的分发。
    """
    from barkit.capabilities.helper import primitives

    # 2026-09-29：新增通用 ``resync``（「现在跑一轮」，取代旧的 touch .trigger 敲门）。
    assert set(primitives.op_names()) == {"route.renew", "route.release",
                                          primitives.RESYNC}
    assert primitives.op_names() == frozenset(primitives.OP_ACTIONS)
    # 未登记的命令：拒绝并**如实说清**闭集里有什么
    result, err = primitives.run_op("route.reboot", {})
    assert result is None and err and "未知命令" in err
    assert "route.renew" in err


def test_resync_runs_the_orchestrator_not_a_dict():
    """``resync`` 必须真的调到编排层的 ``runner.resync``，而不是把 dict 当 runner。

    回归：2026-09-30 日志 ``'dict' object has no attribute 'resync'`` —— ``_resync`` 的
    形参顺序写反（写成 ``(runner, params)``），而 ``run_op`` 按约定 ``fn(params, runner)``
    调用，于是 dict 落到 ``runner`` 上、``dict.resync`` 抛 AttributeError。
    """
    from barkit.capabilities.helper import primitives

    captured = {}

    class _Runner:
        def resync(self, subject: str):
            captured["subject"] = subject
            return {"ok": True, "subject": subject}

    result, err = primitives.run_op(primitives.RESYNC,
                                    {"subject": "tunnel"}, _Runner())
    assert err is None, f"resync 不应报错，却得到：{err!r}"
    assert captured == {"subject": "tunnel"}, "必须调到 runner.resync(subject)"
    assert result == {"ok": True, "subject": "tunnel"}


def test_resync_without_orchestrator_says_so():
    """没有编排层（runner=None）时如实报错，而不是把 None 当 runner 用。"""
    from barkit.capabilities.helper import primitives

    result, err = primitives.run_op(primitives.RESYNC, {"subject": "tunnel"}, None)
    assert result is None and err and "编排层" in err



# ──────────────────────────────────────────────
# A 类二进制封闭集与基线
# ──────────────────────────────────────────────

def test_root_binary_closed_set_single_source():
    """installer 的名级副本必须与 rootbin 真源逐字相等（漂移即失败）。"""
    assert installer.KNOWN_ROOT_BINARIES == rootbin.known_names()


def test_root_binary_hash_matches_baseline(tmp_path):
    """发行态 hev 必须与登记基线一致；被篡改的副本必须被拒（禁降级）。"""
    assert HEV_SRC.is_file(), "hev 发行态二进制应随插件分发"
    ok, reason = rootbin.verify_file("hev-socks5-tunnel", HEV_SRC)
    assert ok, reason

    tampered = tmp_path / "hev-socks5-tunnel"
    shutil.copyfile(HEV_SRC, tampered)
    with open(tampered, "ab") as fh:
        fh.write(b"\x00")
    ok, reason = rootbin.verify_file("hev-socks5-tunnel", tampered)
    assert not ok and "基线" in reason


def test_verify_file_rejects_unregistered_name():
    ok, reason = rootbin.verify_file("../../evil", HEV_SRC)
    assert not ok and "未登记" in reason


def test_manifest_roundtrip_and_permissions(tmp_path):
    path = tmp_path / "manifest.json"
    written = rootbin.write_manifest(path)
    assert written == path
    assert (path.stat().st_mode & 0o777) == 0o600

    payload, reason = rootbin.load_manifest(path)
    assert reason is None
    assert payload["hev-socks5-tunnel"] == rootbin.ROOT_BINARIES["hev-socks5-tunnel"].sha256

    ok, reason = rootbin.verify_against_manifest("hev-socks5-tunnel", HEV_SRC, payload)
    assert ok, reason
    ok, reason = rootbin.verify_against_manifest("unknown", HEV_SRC, payload)
    assert not ok and "未登记" in reason


def test_manifest_corrupt_is_reported_not_raised(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text("{ half", encoding="utf-8")
    payload, reason = rootbin.load_manifest(path)
    assert payload == {} and "JSON" in reason

    payload, reason = rootbin.load_manifest(tmp_path / "missing.json")
    assert payload == {} and "不存在" in reason


def test_helper_module_scan_reaches_subpackages():
    """扫描面必须进子包 —— 退回 ``glob("*.py")`` 时这条转红（覆盖不许悄悄缩水）。

    这一族门禁的判据是「helper 包」而不是「点名某几个文件」；helper 拆出子包那天，
    ``glob`` 版本一条错都不报、覆盖却少了大半。没有这条，下次拆包还会重演。
    """
    rels = [p.relative_to(HELPER_PKG).as_posix() for p in _helper_modules()]
    assert any(rel.startswith("routeops/") for rel in rels), rels
    assert any(rel.startswith("tunnelops/") for rel in rels), rels


# ──────────────────────────────────────────────
# 部署形态门禁：仅标准库 + 3.9 可解析 + 无动态执行 + 平铺可导入
# ──────────────────────────────────────────────

def test_helper_package_is_stdlib_only():
    """helper 由系统 /usr/bin/python3 运行：不得 import barkit.*、PySide6、pytest。"""
    for path in _helper_modules():
        for target in _imports(path):
            if target.startswith("."):
                continue                      # 包内相对导入
            root = target.split(".")[0]
            assert root in STDLIB_OK, f"{path} 引入了非标准库依赖：{target}"


def test_helper_package_parses_as_python39():
    """部署目标是系统 python3（3.9+）：语法必须 3.9 可解析。"""
    for path in _helper_modules():
        ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 9))


def test_helper_package_has_no_dynamic_execution():
    """禁 eval / exec / __import__ / importlib / shell=True（§7.4.4）。"""
    banned_calls = {"eval", "exec", "compile", "__import__"}
    for path in _helper_modules():
        text = path.read_text(encoding="utf-8")
        assert "importlib" not in text, f"{path} 出现 importlib"
        assert "shell=True" not in text, f"{path} 出现 shell=True"
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in banned_calls, f"{path} 调用了 {node.func.id}()"


@pytest.mark.parametrize("python", [sys.executable, "/usr/bin/python3"])
def test_helper_package_deploys_flat(tmp_path, python):
    """把包整目录复制到临时位置，以「系统域部署」的方式导入 —— 证明无 barkit 依赖。"""
    if not os.path.exists(python):
        pytest.skip(f"解释器不存在：{python}")
    target = tmp_path / "helper"
    shutil.copytree(HELPER_PKG, target, ignore=shutil.ignore_patterns("__pycache__"))
    code = (
        "import sys;"
        f"sys.path.insert(0, {str(tmp_path)!r});"
        "import helper.actions as a; import helper.rootbin as r; import helper.specs as s;"
        "import helper.core as c; import helper.version as v; import helper.state as st;"
        "assert a.ACTION_SCHEMAS and r.known_names() and s.Ipv4Spec().check('1.2.3.4')[0];"
        "assert c.HELPER_VERSION == v.HELPER_VERSION;"
        "assert st.write_state.__module__ == 'helper.state';"
        "print('ok')"
    )
    res = subprocess.run([python, "-c", code], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert "ok" in res.stdout


# ──────────────────────────────────────────────
# 启动 / 执行前自检（§7.4.5 P1，批次 4 · S4）
# ──────────────────────────────────────────────

def test_hash_tree_is_stable_and_ignores_bytecode(tmp_path):
    """聚合哈希只看 ``*.py``：``__pycache__`` 等运行期产物不得参与。

    否则部署目录（系统域，root 可写）在 helper 首次运行后落入 bytecode，
    第二次启动的自检必然失败 —— 那是自检机制的自身缺陷，不是安全信号。
    """
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "b.py").write_text("y = 2\n", encoding="utf-8")
    first = rootbin.hash_tree(tmp_path)
    assert first and len(first) == 64
    assert rootbin.hash_tree(tmp_path) == first                      # 稳定

    (pkg / "__pycache__").mkdir()
    (pkg / "__pycache__" / "b.cpython-39.pyc").write_bytes(b"\x00")
    assert rootbin.hash_tree(tmp_path) == first                      # bytecode 不影响

    (pkg / "b.py").write_text("y = 3\n", encoding="utf-8")
    assert rootbin.hash_tree(tmp_path) != first                      # 源码变了必然变
    assert rootbin.hash_tree(tmp_path / "不存在") is None


def test_verify_tree_against_manifest_detects_drift(tmp_path):
    (tmp_path / "core.py").write_text("x = 1\n", encoding="utf-8")
    manifest = {rootbin.HELPER_ARTIFACT_NAME: rootbin.hash_tree(tmp_path)}

    ok, reason = rootbin.verify_tree_against_manifest("helper", tmp_path, manifest)
    assert ok, reason
    ok, reason = rootbin.verify_tree_against_manifest("hev-socks5-tunnel", tmp_path, manifest)
    assert not ok and "基线缺失" in reason

    (tmp_path / "core.py").write_text("x = 2\n", encoding="utf-8")
    ok, reason = rootbin.verify_tree_against_manifest("helper", tmp_path, manifest)
    assert not ok and "基线" in reason


def test_check_owner_perms_flags_group_writable(tmp_path):
    target = tmp_path / "hev-socks5-tunnel"
    target.write_text("x", encoding="utf-8")
    # 只看权限位，故把 owner 放到文件自身的真实 uid/gid 上（tmp 建的文件归当前用户，
    # 但组继承自父目录，不能直接拿 os.getgid() 断言）
    st = target.stat()
    me = dict(uid=st.st_uid, gid=st.st_gid)

    os.chmod(target, 0o775)
    ok, reason = rootbin.check_owner_perms(target, **me)
    assert not ok and "写位" in reason

    os.chmod(target, 0o755)
    ok, reason = rootbin.check_owner_perms(target, **me)
    assert ok, reason


def test_check_owner_perms_rejects_foreign_owner(tmp_path):
    target = tmp_path / "hev-socks5-tunnel"
    target.write_text("x", encoding="utf-8")
    ok, reason = rootbin.check_owner_perms(target)      # 测试进程不是 root
    assert not ok and "root:wheel" in reason


def test_verify_registered_requires_closed_set_and_manifest():
    assert rootbin.verify_registered("hev-socks5-tunnel", {"hev-socks5-tunnel": "x"}) == (True, None)

    ok, reason = rootbin.verify_registered("../../evil", {"../../evil": "x"})
    assert not ok and "未登记" in reason

    ok, reason = rootbin.verify_registered("hev-socks5-tunnel", {})
    assert not ok and "基线缺失" in reason

    assert rootbin.is_registered("hev-socks5-tunnel")
    assert not rootbin.is_registered("rm")


def test_helper_dir_points_at_the_deployed_package():
    """自检要算的就是「本包所在目录」的聚合哈希。"""
    assert safeio.helper_dir() == HELPER_PKG


def test_helper_self_check_only_passes_on_matching_baseline(tmp_path, monkeypatch):
    pkg = tmp_path / "helper"
    shutil.copytree(HELPER_PKG, pkg, ignore=shutil.ignore_patterns("__pycache__"))
    manifest = {rootbin.HELPER_ARTIFACT_NAME: rootbin.hash_tree(pkg)}

    monkeypatch.setattr(rootbin, "load_manifest", lambda path=None: (manifest, None))
    monkeypatch.setattr(rootbin, "check_owner_perms", lambda path, **kw: (True, None))
    monkeypatch.setattr(core, "helper_dir", lambda: pkg)

    ok, reason = core.self_check()
    assert ok, reason

    manifest[rootbin.HELPER_ARTIFACT_NAME] = "0" * 64
    ok, reason = core.self_check()
    assert not ok and "基线" in reason


def test_helper_self_check_reports_missing_baseline(monkeypatch):
    monkeypatch.setattr(rootbin, "load_manifest",
                        lambda path=None: ({}, "manifest.json 不存在（尚未安装或安装不完整）"))
    ok, reason = core.self_check()
    assert not ok and "不存在" in reason


def test_helper_main_stops_cleanly_when_self_check_fails(monkeypatch):
    """自检不过 = **停机 + 上报**，且必须 **rc=0**（否则 launchd 崩溃循环）。

    plist 的 KeepAlive={SuccessfulExit:false} 只在非 0 退出时重新拉起；rc=0 让
    launchd 认定正常结束，从而不拉活 —— 语义仍是停机，面板会提示「在设置页重装」。

    判据挂在 ``run_loop``（主循环）上：自检不过就不该**建会话、进调度**。
    """
    monkeypatch.setattr(core, "self_check", lambda: (False, "hev 基线缺失"))
    written: dict = {}
    monkeypatch.setattr(core, "write_state", lambda state: written.update(state))
    ran = {"n": 0}
    monkeypatch.setattr(core, "run_loop",
                        lambda *a, **k: ran.__setitem__("n", ran["n"] + 1))

    assert core.main() == 0
    assert ran["n"] == 0                                  # 绝不进入调度器
    assert written["ok"] is False and "重装" in written["error"]


# ──────────────────────────────────────────────
# §10 门禁补全（批次 4 · S5）：无裸写落盘 / 无用户域执行 / 旧 label 清理
#
# 三条扫描器都抽成「接收路径列表」的纯函数：门禁本体断言「真实源码零命中」，
# 自测再注入违规样例断言「必被检出」——防止门禁自身写成永远通过的空壳。
# ──────────────────────────────────────────────

#: 写模式字符：出现任一即视为「写」。'r' / 'rb' 等只读不受限。
_WRITE_MODE_CHARS = frozenset("wax+")
#: 以「执行外部程序」为目的的调用名（subprocess / os.exec* 家族）。
_EXEC_CALL_NAMES = frozenset({
    "run", "Popen", "call", "check_call", "check_output",
    "execv", "execve", "execvp", "execvpe", "exec",
    "spawnv", "spawnl", "system",
})


def _call_name(node: ast.Call) -> "tuple[str | None, str | None]":
    """返回 (基名, 属性名)：``os.open`` → ('os','open')；``open`` → (None,'open')。"""
    fn = node.func
    if isinstance(fn, ast.Attribute):
        base = fn.value.id if isinstance(fn.value, ast.Name) else None
        return base, fn.attr
    if isinstance(fn, ast.Name):
        return None, fn.id
    return None, None


def _literal_open_mode(node: ast.Call) -> "str | None":
    """取 open() 的模式实参（第 2 个位置参数或 mode= 关键字）；非常量返回 None。"""
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        return node.args[1].value
    for kw in node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            return kw.value.value
    return None


def _scopes_of(tree: ast.AST) -> list:
    """每个函数体 + 模块顶层各算一个作用域（用于判定 os.open 是否在同一作用域内带 O_NOFOLLOW）。"""
    scopes = [tree]
    scopes.extend(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    return scopes


def _scan_bare_write_open(paths) -> list:
    """扫「绕过 O_NOFOLLOW 的落盘写」，返回违规描述列表（空列表 = 合规）。

    允许：``os.open(..., O_NOFOLLOW, ...)``（safeio 的唯一落盘入口，判定按所在
    作用域内是否出现 ``O_NOFOLLOW``，以容忍 ``flags`` 变量拆行）、只读 ``open``。
    禁止：写模式 ``open`` / ``Path.open('w')`` / ``write_text`` / ``write_bytes``
    —— 它们会跟随末级符号链接，把 root 写入导向任意文件（CWE-59）。
    """
    hits: list = []
    for path in paths:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        for scope in _scopes_of(tree):
            scope_src = ast.unparse(scope)
            for node in ast.walk(scope):
                if not isinstance(node, ast.Call):
                    continue
                base, name = _call_name(node)
                src = ast.unparse(node)
                if name == "open" and base == "os":
                    if "O_NOFOLLOW" not in scope_src:
                        hits.append(f"{path}: os.open 所在作用域无 O_NOFOLLOW → {src}")
                elif name == "open":
                    mode = _literal_open_mode(node)
                    if mode is not None and set(mode) & _WRITE_MODE_CHARS:
                        hits.append(f"{path}: 裸 open 以写模式落盘 → {src}")
                elif name in {"write_text", "write_bytes"}:
                    hits.append(f"{path}: {name}() 绕过 O_NOFOLLOW → {src}")
    return hits


def _scan_user_domain_exec(paths) -> list:
    """扫「从用户域取可执行文件」：exec 调用的实参不得含 data_dir() / BARKIT_DATA_DIR。"""
    hits: list = []
    for path in paths:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            _, name = _call_name(node)
            if name in _EXEC_CALL_NAMES:
                src = ast.unparse(node)
                if "data_dir(" in src or "BARKIT_DATA_DIR" in src:
                    hits.append(f"{path}: 从用户域执行程序 → {src}")
    return hits


def _function_body_source(module_path, func_name: str) -> str:
    """取某函数的**函数体**源码（剔除 docstring）—— 供「本体不得引用 X」类断言。

    剔除 docstring 是必需的：``hev_binary`` 的 docstring 正**用文字**解释它删掉的
    用户域回退（教训留档），若连注释一起扫会把它误判成「仍引用用户域」。
    """
    tree = ast.parse(Path(module_path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            body = list(node.body)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]
            return "\n".join(ast.unparse(stmt) for stmt in body)
    raise AssertionError(f"{module_path} 里找不到函数 {func_name}")


def test_helper_no_bare_open():
    """root 侧落盘写入必须经 safeio._open_nofollow（O_NOFOLLOW）—— §7.4.4 红线。"""
    hits = _scan_bare_write_open(_helper_modules())
    assert not hits, "存在绕过 O_NOFOLLOW 的落盘写入：\n" + "\n".join(hits)


def test_helper_no_user_domain_exec():
    """可执行文件只能来自系统域 —— 用户域回退是可直接利用的本地提权面（v2.6.3 已删）。"""
    # (a) 扫描：exec 调用不得内联用户域路径
    hits = _scan_user_domain_exec(_helper_modules())
    assert not hits, "存在从用户域执行程序：\n" + "\n".join(hits)
    # (b) 事实：hev 的唯一来源是系统域，且解析函数**本体**不得引用 data_dir()
    hev = safeio.hev_binary()
    assert hev.parent == rootbin.SYS_DIR, f"hev 不在系统域：{hev}"
    body = _function_body_source(safeio.__file__, "hev_binary")
    assert "data_dir" not in body, "hev 解析函数本体引用了用户域"


def test_snapshot_write_refuses_symlinked_tmp(tmp_path, monkeypatch):
    """CWE-59 回归（S5 门禁 ``test_helper_no_bare_open`` 的首个战果）。

    快照的 tmp 落在**用户域**，本地进程可预置它为符号链接，让 root 的写入跟随链接
    去截断/改写任意文件。落盘一律走 ``_atomic_write_json``（O_NOFOLLOW），
    末级是链接即 ELOOP 失败 —— **宁缺勿险**（无落盘，也不写链接指向的目标）。

    2026-09-29：目标文件名随「三份状态文件 → 一份快照」一起换成了
    ``helper_snapshot.json``（``tunnel_state.json`` 已不存在）。
    """
    from barkit.capabilities.helper import state

    monkeypatch.setenv("BARKIT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(state, "_OBSERVED", {})              # 模块级内存：逐条清干净
    monkeypatch.setattr(state, "_INTENTS", {})
    monkeypatch.setattr(state, "_LAST_SIGNATURE", None)      # 否则「内容没变」会把写跳掉

    victim = tmp_path / "victim.txt"
    victim.write_text("原内容", encoding="utf-8")
    (tmp_path / f"{state.SNAPSHOT_NAME}.tmp").symlink_to(victim)

    state.write_tunnel_state({"active": True})

    assert victim.read_text(encoding="utf-8") == "原内容"      # 符号链接目标未被跟随/截断
    assert not (tmp_path / state.SNAPSHOT_NAME).exists()      # 写入被拒 → 无落盘（宁缺勿险）


def test_helper_log_truncates_when_oversized(tmp_path, monkeypatch):
    """helper.log 超限必须**就地截尾**，且**不得另开第二份文件**（用户 2026-09-29 定案）。

    先前的做法是 ``os.replace`` 到 ``helper.log.1``（主文件 ＋ 1 份备份）。收窄到
    「每个进程恰好一份」之后就做不到那种做法了 —— Python 自带的轮转器只会做
    「主文件 ＋ N 份备份」。做法：读尾部一半、**原子换回**（tmp ＋ ``os.replace``）、
    接着从末尾往下写。代价说清楚：恰好在上限那一刻出事，最旧的一半现场会丢掉。

    换回必须**原子**（2026-10-13 与应用侧同一套）：旧写法就地 ``O_TRUNC`` 重写，会先
    落到 0 字节 —— 那一刻断电丢的是整份日志。判据取「inode 换新 ＋ 不留 ``.tmp``」。
    """
    from barkit.capabilities.helper import safeio

    monkeypatch.setenv("BARKIT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(safeio, "_HELPER_LOG_MAX_BYTES", 128)
    log_path = tmp_path / "helper.log"
    line = "x" * 20 + "\n"
    log_path.write_text(line * 10, encoding="utf-8")          # 预置超限旧日志（210 字节）
    before = log_path.stat().st_ino

    safeio.log("新一行")

    assert not (tmp_path / "helper.log.1").exists(), "收窄到一份：不得再另开备份文件"
    assert not list(tmp_path.glob("*.tmp")), "换尾用的 tmp 必须清干净"
    assert log_path.stat().st_ino != before, "换尾应当原子换上（inode 换新），不是就地重写"
    body = log_path.read_text(encoding="utf-8")
    assert body.endswith("新一行\n"), "新这一行必须还在（截的是最旧的那半）"
    assert body.startswith(line), "留下的应当是旧行的**尾部**，不是整份丢掉"
    assert body.count("\n") == 5, "旧行剩一半（5 行）＋ 新的一行"
    assert len(body) < len(line) * 10, "确实被截短了"


def test_helper_log_keeps_appending_when_small(tmp_path, monkeypatch):
    """未达阈值**一个字都不许动**（否则每次写都截一次，历史全丢）。"""
    from barkit.capabilities.helper import safeio

    monkeypatch.setenv("BARKIT_DATA_DIR", str(tmp_path))
    safeio.log("a")
    safeio.log("b")

    assert not (tmp_path / "helper.log.1").exists()
    body = (tmp_path / "helper.log").read_text(encoding="utf-8")
    assert body.count("\n") == 2 and body.endswith("b\n")


def test_helper_log_refuses_symlinked_target(tmp_path, monkeypatch):
    """轮转不得成为新的 CWE-59 面：helper.log 被预置为符号链接时不得跟随。"""
    from barkit.capabilities.helper import safeio

    victim = tmp_path / "victim.txt"
    victim.write_text("原内容", encoding="utf-8")
    (tmp_path / "helper.log").symlink_to(victim)
    monkeypatch.setenv("BARKIT_DATA_DIR", str(tmp_path))

    safeio.log("攻击")

    assert victim.read_text(encoding="utf-8") == "原内容"     # 目标未被跟随/截断


def test_daemon_cleans_legacy_label(tmp_path, monkeypatch):
    """安装/卸载的硬前置必须清掉旧 label（com.barkit.routed）—— §D5。

    缺了它 = 换了 label 但旧 daemon 带旧代码继续改系统网络状态，热修看似生效实则失效。
    """
    from barkit.capabilities import helper_client as daemon

    assert daemon.LEGACY_PLIST_LABELS, "旧 label 清单不得为空"
    captured: list = []
    monkeypatch.setattr(paths, "APP_SUPPORT", tmp_path)
    monkeypatch.setattr(daemon, "run_admin",
                        lambda p: (captured.append(Path(p).read_text(encoding="utf-8")) or True))

    assert daemon.install({}) is True
    assert daemon.uninstall() is True
    assert len(captured) == 2

    legacy_plist = daemon.LEGACY_PLIST_DIR / (daemon.LEGACY_PLIST_LABELS[0] + ".plist")
    for script in captured:              # 安装 + 卸载双双包含旧 label 清理
        assert f'launchctl bootout "system/{daemon.LEGACY_PLIST_LABELS[0]}"' in script
        assert str(legacy_plist) in script
        for path in daemon.LEGACY_HELPER_FILES:
            assert str(path) in script
    # 卸载还必须 bootout 新 label
    assert f'launchctl bootout "system/{daemon.PLIST_LABEL}"' in captured[1]


def test_helper_src_dir_resolves_to_real_directory():
    """仓库内 helper 源码目录必须真实存在（§8.1 切分点 1）。

    为什么单独立一条：`helper_client.HELPER_SRC_DIR` 若解析错，安装器会在**用户输完
    密码之后**才以「源缺失」拒绝安装——失败点离原因太远。历史教训：该模块原在
    `plugins/route_guard/` 时用三级 `.parent` 上溯，直接搬进 `core/` 会多上溯一层、
    静默解析成不存在的 `src/capabilities/helper`。所以这里把「目录真实存在」钉死，
    不让路径算术自证。
    """
    from barkit.capabilities import helper_client

    assert helper_client.HELPER_SRC_DIR.is_dir(), helper_client.HELPER_SRC_DIR
    assert (helper_client.HELPER_SRC_DIR / "version.py").is_file(), "版本锚点真源必须在源码目录里"
    assert helper_client.HELPER_SRC_VERSION_PATH == helper_client.HELPER_SRC_DIR / "version.py"


def test_install_refuses_when_helper_source_missing(tmp_path, monkeypatch):
    """源码目录不存在时**拒绝安装**，且不得走到弹授权那一步（§8.1 切分点 1）。

    这条守的是「失败点要贴近原因」：宁可本地预检直接报错，也不要让用户白输一次密码。
    """
    from barkit.capabilities import helper_client

    called: list = []
    monkeypatch.setattr(helper_client, "HELPER_SRC_DIR", tmp_path / "不存在的源码目录")
    monkeypatch.setattr(helper_client, "run_admin", lambda p: called.append(p) or True)

    assert helper_client.install({}) is False
    assert not called, "源码缺失时不得弹出系统授权"


# ── 门禁自测：注入违规必须被检出（合规样例不得误报）──

def test_gate_no_bare_open_selfcheck(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("open('/etc/x', 'w').write('y')\n", encoding="utf-8")
    assert _scan_bare_write_open([bad])

    good = tmp_path / "good.py"
    good.write_text(
        "def w():\n"
        "    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW\n"
        "    fd = os.open(str(p), flags, 0o600)\n"
        "    return open(p, 'rb')\n",
        encoding="utf-8",
    )
    assert _scan_bare_write_open([good]) == []


def test_gate_no_user_domain_exec_selfcheck(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("subprocess.run([data_dir() / 'hev'])\n", encoding="utf-8")
    assert _scan_user_domain_exec([bad])

    good = tmp_path / "good.py"
    good.write_text("subprocess.run([SYS_DIR / 'hev'])\n", encoding="utf-8")
    assert _scan_user_domain_exec([good]) == []
