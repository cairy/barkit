"""route_guard 意图（policy）测试：档 B —— 决策在 app 侧，helper 只执行。

覆盖三件事：
1. ``compute_apply`` 的决策矩阵（pinned / manual_only / enabled / paused 冻结）与迁移前
   helper 内 ``_applied`` 逐条等价；
2. ``build_intent`` 的产物能被 helper 的 ``validate_intent`` 接受（跨进程契约），
   且 ``pinned`` / ``manual_only`` **不进产物**；
3. ``RouteProbeService`` 每轮把意图交给 ``declare`` 送出、异常只上报不抛。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from barkit import paths
from barkit.capabilities.helper import routeops
from barkit.capabilities.helper.routeops import routeops as routeops_cycle
from barkit.plugins.route_guard import intent as intent_mod
from barkit.plugins.route_guard import rules


OFFICE_PROFILE = {
    "name": "office",
    "ifaces": ["en5"],
    "match": {"gateway": "10.99.1.254"},
    "net": {"mode": "dhcp"},
    "routes": {
        "enabled": True,
        "items": [{"destination": "10.99.2.0/24", "via": "10.99.1.254"}],
    },
}

OFFICE = {
    "name": "office",
    "priority": 1,
    "iface": "en5",
    "gateway": "10.99.1.254",
    "destinations": ["10.99.2.0/24"],
}


def _routes(rules_list, *, paused=False, poll=30):
    return {"version": 1, "poll": poll, "paused": paused, "rules": list(rules_list)}


# 决策矩阵（与迁移前 helper._applied 等价）。
# ``force`` 那几行随 §4.8 一起删了：面板开关不再走「强制开 / 强制关」，覆盖层在配置
# 编译期就落到 ``enabled`` 一位上 —— 本函数因此只剩「自动」与「暂停冻结」两档。
@pytest.mark.parametrize("trigger,enabled,paused,prior,expected", [
    (True,  True,  False, None,  True),    # 自动：命中且启用
    (False, True,  False, None,  False),   # 自动：未命中
    (True,  False, False, None,  False),   # 自动：命中但被禁用（覆盖层也是走这里）
    (True,  True,  True,  True,  True),    # 暂停冻结：沿用上轮「已生效」
    (True,  True,  True,  False, False),   # 暂停冻结：沿用上轮「未生效」
    (True,  True,  True,  None,  False),   # 暂停冻结：无上轮态 → 不动
])
def test_compute_apply_matrix(trigger, enabled, paused, prior, expected):
    rule = {**OFFICE, "enabled": enabled}
    assert intent_mod.compute_apply(rule, trigger, paused, prior) is expected


# 「谁来挑场景」（§2.3 三态 / §2.6）同样只改 ``apply`` 一位 —— 两个档位各三行。
@pytest.mark.parametrize("pinned,manual,expected", [
    ("", False, True),        # 自动：不动
    ("home", False, False),   # 锁着别的场景 ⇒ 这一条不参与（「忽略匹配」不是「多加一条」）
    ("office", False, True),  # 锁着它
    ("", True, False),        # manual_only：退出自动匹配（判定依据会被组件自己写掉）
    ("office", True, True),   # …但手动指定仍然点得亮它
    ("home", True, False),
])
def test_compute_apply_pinned_and_manual(pinned, manual, expected):
    rule = {**OFFICE, "enabled": True}
    assert intent_mod.compute_apply(
        rule, True, False, None, pinned=pinned, manual=manual
    ) is expected


def test_build_intent_keeps_pin_and_manual_out_of_the_wire_format():
    """``pinned`` / ``manual_only`` **不进产物**：线格式是 helper 已在消费的契约。

    多一个它不认识的键 = 整份拒绝 + 冻结（静默停摆），所以这两件事只折进 ``apply``
    一位里。插件侧那条完整链路见 `test_route_guard.py` 的
    ``test_set_pinned_writes_yaml_and_beats_matching``。
    """
    routes = _routes([OFFICE, {**OFFICE, "name": "vpn"}])
    out = intent_mod.build_intent(
        routes, None, probe=lambda *_a: True, pinned="vpn", manual_only=["office"]
    )
    by_name = {r["name"]: r for r in out["rules"]}
    assert by_name["vpn"]["apply"] is True and by_name["office"]["apply"] is False
    assert by_name["office"]["trigger_matched"] is False, "manual_only 不参与匹配"
    assert by_name["vpn"]["trigger_matched"] is True, "命中是事实，照实回显"
    assert set(out) == {"version", "poll", "paused", "handback", "handback_dns",
                        "rules"}
    assert all("pinned" not in r and "manual_only" not in r for r in out["rules"])

    data, err = routeops.validate_intent(out)      # helper 侧收得下这一份
    assert err is None, err


# ──────────────────────────────────────────────
# build_intent
# ──────────────────────────────────────────────

def test_build_intent_marks_apply_and_trigger():
    out = intent_mod.build_intent(_routes([OFFICE]), None, probe=lambda *_a: True)
    rule = out["rules"][0]
    assert rule["apply"] is True and rule["trigger_matched"] is True
    assert rule["gateway"] == "10.99.1.254" and rule["destinations"] == ["10.99.2.0/24"]

    out2 = intent_mod.build_intent(_routes([OFFICE]), None, probe=lambda *_a: False)
    assert out2["rules"][0]["apply"] is False
    assert out2["rules"][0]["trigger_matched"] is False


def test_build_intent_freezes_on_pause_using_prior_state():
    """暂停 → 无 force 的规则沿用上轮实际生效态（来自守护推来的那份观测）。"""
    prior = {"office": True}
    out = intent_mod.build_intent(
        _routes([OFFICE], paused=True), prior, probe=lambda *_a: False
    )
    assert out["paused"] is True
    assert out["rules"][0]["apply"] is True      # 冻结：保持已生效
    assert out["rules"][0]["trigger_matched"] is False   # 但命中态如实回显


def test_build_intent_carries_no_force_field():
    """§4.8：``force_apply`` 通道下线，期望态里不再有它。

    「本次不叠加」在配置编译期已落到 ``enabled`` 一位上，helper 眼里还是同一个开关。
    """
    out = intent_mod.build_intent(
        _routes([{**OFFICE, "enabled": False}]), None, probe=lambda *_a: True
    )
    assert "force_apply" not in out["rules"][0]
    assert out["rules"][0]["apply"] is False
    assert out["rules"][0]["enabled"] is False


def test_build_intent_accepted_by_helper_validator():
    """跨进程契约：app 侧产物必须能被 helper 的 validate_intent 接受。"""
    out = intent_mod.build_intent(_routes([OFFICE, {**OFFICE, "name": "home"}]),
                                  None, probe=lambda *_a: True)
    data, err = routeops.validate_intent(out)
    assert err is None, err
    assert data["version"] == routeops.INTENT_VERSION == intent_mod.INTENT_VERSION
    assert [r["name"] for r in data["rules"]] == ["office", "home"]


def test_build_intent_passes_off_destinations_only_when_applying():
    """逐目标开关（v7）：``off_destinations`` 随规则进线格式，**只在整条要叠时递**。

    整条在撤（``apply=False``）时 helper 逐目标全撤，这一栏它一个字节都不看 —— 不递
    也省得同一件事被两处各判一遍（判分叉了不报错）。停用的那条**仍要**留在
    ``destinations`` 里：目标一从意图里消失，它此前加上的路由就没人去撤了。
    """
    compiled = rules.build_routes({"helper_poll": 30, "profiles": [{
        **OFFICE_PROFILE,
        "routes": {"enabled": True, "items": [
            {"destination": "10.99.2.0/24", "via": "10.99.1.254"},
            {"destination": "10.2.0.0/16", "via": "10.99.1.254", "enabled": False},
        ]},
    }]})
    assert compiled["rules"][0]["off_destinations"] == ["10.2.0.0/16"], \
        "编译层就带上（它不过探活，那一步在意图层）"

    hit = intent_mod.build_intent(compiled, None, probe=lambda *_a: True)
    rule = hit["rules"][0]
    assert rule["apply"] is True
    assert rule["destinations"] == ["10.99.2.0/24", "10.2.0.0/16"]
    assert rule["off_destinations"] == ["10.2.0.0/16"]
    assert routeops.validate_intent(hit)[1] is None, "helper 侧收得下这一份"

    miss = intent_mod.build_intent(compiled, None, probe=lambda *_a: False)["rules"][0]
    assert miss["apply"] is False and "off_destinations" not in miss


def test_build_intent_from_real_config_roundtrip():
    """真实链路：YAML 配置段 → build_routes → build_intent → helper 校验。"""
    cfg = {"helper_poll": 15, "profiles": [OFFICE_PROFILE]}   # 配置键（D27 拆键后）
    routes_doc = rules.build_routes(cfg)
    out = intent_mod.build_intent(routes_doc, None, probe=lambda *_a: True)
    assert out["poll"] == 15                              # 线格式字段名仍是 poll
    data, err = routeops.validate_intent(out)
    assert err is None, err
    assert data["rules"][0]["apply"] is True


# ──────────────────────────────────────────────
# 期望态不再落盘（2026-09-29 通道换成会话）
# ──────────────────────────────────────────────

def test_intent_module_has_no_file_writer():
    """回归护栏：本模块不再有 ``write_intent``，``paths`` 上也没有路由期望态那条路径。

    从前这两样是「期望态的唯一落盘入口」。会话通道落地后它没有对象了 —— 期望态是一帧
    （``declare("route", …)``），不是一份文件；留一个写口等于留着第二个写方。
    """
    assert not hasattr(intent_mod, "write_intent")
    assert not hasattr(paths, "ROUTES_FILE")
    assert not hasattr(intent_mod, "routes_settled")   # 「收敛了没有」由连接本身回答
    assert not hasattr(intent_mod, "DEFER_TIMEOUT_S")  # 没有东西在等，就不该有等待常量


# ──────────────────────────────────────────────
# 探活服务（框架托管的周期型）
# ──────────────────────────────────────────────

class _Declarer:
    """最小 ``declare`` 出口桩：记下每次交出去的那份意图。"""

    def __init__(self, ok=True):
        self.sent: list = []
        self._ok = ok

    def __call__(self, doc):
        self.sent.append(doc)
        return self._ok


def test_probe_service_declares_every_poll():
    """每轮都重声明一次：幂等（同一份内容不重复上线）是 ``declare`` 的事，
    服务这一层不再有「和什么比」可错。

    ⚠️ 这条取代的是从前的「变化检测 + 落盘」。旧写法在内存里记「我上次写了什么」，
    盘被第三方改写之后永远发现不了（2026-09-28 真机事故）。现在连盘都没有了。
    """
    matched = {"v": True}
    decl = _Declarer()

    def provider():
        return intent_mod.build_intent(
            _routes([OFFICE]), None, probe=lambda *_a: matched["v"]
        )

    svc = intent_mod.RouteProbeService(provider=provider, declare=decl)
    svc.poll()
    svc.poll()
    assert len(decl.sent) == 2, "每轮都要交出去（幂等归 declare）"
    assert decl.sent[0]["rules"][0]["apply"] is True

    matched["v"] = False                        # 网络变化 → 重算 → 交出新的一份
    svc.poll()
    assert len(decl.sent) == 3
    assert decl.sent[-1]["rules"][0]["apply"] is False


def test_probe_service_reports_problem_not_raise():
    """长驻服务不能因异常退出：交给宿主的 report_problem 变成设置页可见的问题。"""
    problems: list[str] = []
    decl = _Declarer()

    def boom():
        raise RuntimeError("配置读不出来")

    svc = intent_mod.RouteProbeService(provider=boom, declare=decl)
    svc.report_problem = problems.append
    svc.poll()                                  # 不抛
    assert problems and "路由意图计算失败" in problems[0]
    assert decl.sent == []                      # 算不出来就什么都不发


def test_probe_service_none_means_nothing_to_declare():
    """``provider`` 返回 ``None``（配置非法）⇒ 什么都不发。

    一份错期望态推到守护面前没有好处，它只会整份拒绝并冻结。
    """
    decl = _Declarer()
    svc = intent_mod.RouteProbeService(provider=lambda: None, declare=decl)
    svc.poll()
    assert decl.sent == []


def test_probe_service_interval_declared():
    """节律是**数据声明**，不是插件自带的时钟（批次 5 后段定案）。"""
    svc = intent_mod.RouteProbeService(provider=lambda: None, declare=lambda _d: True)
    assert svc.name == "route_probe"
    assert svc.interval == intent_mod.PROBE_INTERVAL > 0
    assert svc._provider is not None            # 取数口是**方法引用**，不是一份快照


# ──────────────────────────────────────────────
# 真实探活（复用 helper 的查询机制，非决策）
# ──────────────────────────────────────────────

def test_network_probe_uses_helper_queries(monkeypatch):
    """``network_probe`` 的四条命中规则（复用 helper 的**查询**，不含决策）。

    §1.2 的触发条件＝「监控网卡 ＋ 生效网段」，判决 4 又保留了 ``gateway`` 这条兼容路，
    于是规则是「网卡在线 ＋ **声明过的**条件全部成立」—— 正是 ``validate_match`` 文档里
    那两句。此前只有网关那半真的接上了，``subnet`` 是「写进配置、校验通过、永不生效」。

    ⚠️ 桩要打在 ``intent`` 命名空间的那些名字上，不能打 ``routeops`` 里同名的函数：
    ``intent`` 是 ``from ... import iface_up`` 的**按值导入**（模块 docstring 里写明
    「复用 helper 侧实现，避免两侧语义漂移」），改 ``routeops.iface_up`` 根本影响不到它。
    原先那条用例打错了地方，于是它只在「开发机真有一块 en5、网关恰好是 10.99.1.254」
    时通过 —— 换句话说它测的是开发机的网络，不是这段代码（2026-09-17 随第 7b 步订正）。
    """
    monkeypatch.setattr(intent_mod, "iface_up", lambda iface: iface == "en5")
    monkeypatch.setattr(intent_mod, "current_gateways", lambda iface: ["10.99.1.254"])
    monkeypatch.setattr(intent_mod, "current_ipv4",
                        lambda iface: ["192.168.11.5", "169.254.1.2"])

    # 只写 gateway（兼容旧配置那条路）
    assert intent_mod.network_probe("en5", "", "10.99.1.254") is True
    assert intent_mod.network_probe("en5", "", "192.168.2.1") is False    # 网关不在
    assert intent_mod.network_probe("en9", "", "10.99.1.254") is False   # 接口不在线

    # 只写 subnet（本设计的**主路径**）
    assert intent_mod.network_probe("en5", "192.168.11.0/24", "") is True
    assert intent_mod.network_probe("en5", "192.168.2.0/24", "") is False  # 卡上没这网段的地址
    assert intent_mod.network_probe("en9", "192.168.11.0/24", "") is False

    # 两个都写 = **AND**（不是「任一」）
    assert intent_mod.network_probe("en5", "192.168.11.0/24", "10.99.1.254") is True
    assert intent_mod.network_probe("en5", "192.168.11.0/24", "192.168.2.1") is False
    assert intent_mod.network_probe("en5", "192.168.2.0/24", "10.99.1.254") is False

    # 都不写 = 网卡在线即命中（§2.1 允许空 match）。改动前这里恒 False —— 那是
    # 「没人接上 subnet 这条腿」留下的口径，与 validate_match 的文档正好相反。
    assert intent_mod.network_probe("en5", "", "") is True
    assert intent_mod.network_probe("en9", "", "") is False


def test_subnet_hit_is_fail_closed_on_bad_input(monkeypatch):
    """网段解析不了 ⇒ 不命中（fail-closed），不抛异常。

    ``_subnet_hit`` 在**判定路径**上跑，手写坏的网段不该把整轮意图计算打崩：
    ``build_intent`` 的调用方是按「拿不到意图就冻结」处理的，抛异常等于把一次配置
    笔误升级成「路由停更」。
    """
    monkeypatch.setattr(intent_mod, "iface_up", lambda iface: True)
    monkeypatch.setattr(intent_mod, "current_ipv4", lambda iface: ["192.168.11.5"])
    assert intent_mod.network_probe("en5", "不是网段", "") is False
    assert intent_mod.network_probe("en5", "192.168.11.0/33", "") is False
    # 网卡上的地址解析不了也不该炸（ifconfig 抓到脏数据时的兜底）
    monkeypatch.setattr(intent_mod, "current_ipv4", lambda iface: ["坏地址"])
    assert intent_mod.network_probe("en5", "192.168.11.0/24", "") is False


def test_helper_no_longer_decides(monkeypatch):
    """回归护栏：helper 侧不得再出现匹配决策（否则 policy 又回到 root 侧）。"""
    assert not hasattr(routeops, "rule_matched")
    # 2026-10-02 ``routeops`` 拆成包：那句说明正文随 ``_apply_net`` / ``run_cycle`` 一起
    # 搬进了 ``routeops/routeops.py``，包自己的 docstring 是拆包说明 ⇒ 判据必须去真身取，
    # 否则读到的是新写的那段、断言照样绿（收口证明恒真）。
    src = routeops_cycle.__doc__ or ""
    assert "不再做匹配决策" in src


def test_no_route_intent_side_channel():
    """回归护栏：期望态不得再留 route_intent 旁路（它从前是 routes.json 那份文件）。

    旁路是「为兼容旧 helper 而双写」的产物。既然只重装一次，它属于白付的复杂度，
    且会留下一个无人消费的僵尸文件。这里把它钉死。
    """
    assert not hasattr(paths, "ROUTE_INTENT_FILE")
    plugin_src = (Path(__file__).resolve().parent.parent / "src/barkit/plugins/route_guard/plugin.py")
    assert "route_intent" not in plugin_src.read_text(encoding="utf-8")




# ────────────────────────────────────────────────────────────
# 拆包门禁：rules.py 618 行 → 入口 ＋ 6 个同目录兄弟（平铺，插件不许子目录）
# ────────────────────────────────────────────────────────────
_RULES_DIR = Path(__file__).resolve().parent.parent / "src/barkit/plugins/route_guard"
# 依赖图是**单向**的：keys ← checks ← schema；compile 独立吃 keys/checks/errors；
# 入口吃全部。名字不带 `rules_`/`route_guard` 前缀是刻意的 —— 目录里已经有
# constants.py / reports.py / netops.py / profiles.py，撞名会静默覆盖。
_RULES_FAMILY = ("rules.py", "keys.py", "messages.py", "errors.py",
                 "checks.py", "schema.py", "compile.py")


def test_route_guard_rules_family_points():
    """这一族点名（按**文件集合**，不按「旧单文件不许存在」那种恒真断言）。

    §2.3「目录白名单（硬）」只许 `*.py` / `*.md` / `resources/` / `__pycache__/` ⇒
    插件里拆不动包，只能平铺。于是真正要守的不是「rules.py 在不在」（删了子模块它也在），
    而是「六个兄弟**都有**，且没有第八个混进来」。
    """
    on_disk = sorted(p.name for p in _RULES_DIR.glob("*.py"))
    missing = [n for n in _RULES_FAMILY if n not in on_disk]
    assert not missing, f"拆出去的兄弟模块丢了：{missing}（当前：{on_disk}）"
    assert len(_RULES_FAMILY) == 7, "这一族的点名集合要跟着拆法走，别偷改"
    # ⚠ 别再往这条加「反向数一遍磁盘」的对称判据：missing 一空 ⇒ 点名的每个名字
    # 都在盘上 ⇒ 计数恒等于 len，加多少条都是**恒真**（试过一次，当场撤）。
    # 点名集合被偷改由上面那条 len 挡（实测：划掉一条 ⇒ 这条红）。


def test_route_guard_rules_entry_face():
    """入口的「脸」一张不变：``__all__`` 里的名字必须**真绑定**，且外部读法逐个可取。

    平铺没有 `__init__.py` 替我们做全量再导出，入口这张脸是**手工**补的 —— 漏一个就是
    外部 ``AttributeError``（``rules.validate_net`` 这种属性读，静态判据一条都看不见，
    只有跑到那条路径才炸）。反过来也列一遍：`__all__` 里出现没绑定的名字同样是坑，
    所以两边都查。
    """
    import barkit.plugins.route_guard.rules as rules

    for name in rules.__all__:
        assert hasattr(rules, name), f"__all__ 里列了 {name}，入口上却没有这个绑定"

    # 外部（plugin / reports / profiles / intent / netops / 三个 settings / overlay）真读到的
    external = {
        "DEFAULT_HELPER_POLL": ("plugin", "FALLBACK_KEEP"),
        "MIN_HELPER_POLL": (), "MAX_HELPER_POLL": (),
        "NET_MODE_STATIC": (), "NET_MODE_DHCP": (),
        "FALLBACK_KEEP": (), "FALLBACK_DHCP": (), "FALLBACKS": (),
        "net_writes": (), "validate_net": (), "validate_profile": (),
        "build_routes": (), "RouteConfigError": (),
    }
    missing = [n for n in external if not hasattr(rules, n)]
    assert not missing, f"外部读法用得到的名字入口没给：{missing}"


def test_route_guard_rules_deps_point_one_way():
    """依赖图单向、且入口只认这 6 个兄弟 —— 反向 import 是拆包后最常见的手滑（加个功能顺手
    就 import 了），而「多引一个兄弟」会让入口那张脸长歪（``rules.X`` 看着还能用，其实
    是某个兄弟漏出来给人误当入口 API 拿）。

    AST 扫**顶层** `ImportFrom` 的模块名。入口的集合是**双向**判（恰好这 6 个，少一个
    ⇒ 脸缺、多一个 ⇒ 脸长歪）；其余三条是**单向**判（不许回头看）。判据写在「你要守的那条
    路独有的中间量」上：一条依赖边都没有的那侧（``keys`` 是根）最容易被顺手加上一条而
    无人报警。
    """
    def sibs(name):
        tree = ast.parse((_RULES_DIR / name).read_text(encoding="utf-8"))
        out = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.level and node.module:
                out.add(node.module)
        return out

    entry = sibs("rules.py")
    assert entry == {"keys", "messages", "errors", "checks", "schema", "compile"}, (
        f"入口顶层相对 import 应当恰好是那 6 个兄弟，实际：{sorted(entry)}"
    )
    assert sibs("keys.py") == set(), "keys.py 是依赖图的根：谁都不许 import 兄弟（它自己零依赖）"
    assert not {"schema", "compile", "rules", "messages"} & sibs("checks.py"), (
        f"checks.py 只该依赖 keys：{sibs('checks.py')}"
    )
    assert not {"compile", "rules"} & sibs("schema.py"), (
        f"schema 不许回头看 compile / rules：{sibs('schema.py')}"
    )
    assert not {"schema", "rules"} & sibs("compile.py"), (
        f"compile 不许回头看 schema / rules：{sibs('compile.py')}"
    )


def test_route_guard_rules_ipaddress_only_in_checks():
    """``ipaddress`` 只在 ``checks.py`` 出现 —— 「判据只有一处」的物理形态。

    ``valid_ipv4`` / ``valid_destination`` 是 IPv4 词法判据的**唯一**实现，校验层与编译层
    都经它们取用。哪天谁为了省一条 import 在 ``schema`` / ``compile`` 里直接
    ``import ipaddress`` 自己判一遍，两份口径就会在「留空算不算非法」这类边界上悄悄岔开；
    而这一条岔开静态自证全部照绿（两处都“能跑”）。
    """
    users = []
    for name in _RULES_FAMILY:
        tree = ast.parse((_RULES_DIR / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(alias.name == "ipaddress" for alias in node.names):
                    users.append(name)
            elif isinstance(node, ast.ImportFrom) and node.module == "ipaddress":
                users.append(name)
    assert users == ["checks.py"], f"ipaddress 只许出现在 checks.py，实际：{users}"


def test_route_guard_rules_entry_holds_one_function():
    """入口只留 ``build_routes`` —— 正文搬走后不许有人顺手搬回来（搬回来就成两份）。

    数**模块级** `FunctionDef`；``keys`` / ``messages`` / ``errors`` 那三层不受这条管
    （它们本来就是纯数据），只看入口。
    """
    tree = ast.parse((_RULES_DIR / "rules.py").read_text(encoding="utf-8"))
    defs = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
    assert defs == ["build_routes"], f"入口只该有 build_routes，实际：{defs}"


# ──────────────────────────────────────────────
# 意图规模硬上限（2026-10 审查：挡「一帧吃满 MAX_FRAME」的放大攻击）
# ──────────────────────────────────────────────

def _valid_intent() -> dict:
    out = intent_mod.build_intent(_routes([OFFICE]), None, probe=lambda *_a: True)
    data, err = routeops.validate_intent(out)
    assert err is None, err
    return out


def test_validate_rejects_too_many_rules():
    base = _valid_intent()
    one = base["rules"][0]
    bad = dict(base, rules=[dict(one) for _ in range(routeops.MAX_RULES + 1)])
    _rules, err = routeops.validate_routes(bad)
    assert err is not None and "rules" in err


def test_validate_rejects_too_many_destinations():
    base = _valid_intent()
    one = dict(base["rules"][0])
    one["destinations"] = [f"10.0.0.{i}" for i in range(routeops.MAX_DESTINATIONS + 1)]
    bad = dict(base, rules=[one])
    _rules, err = routeops.validate_routes(bad)
    assert err is not None and "destinations" in err


def test_validate_rejects_bool_poll():
    base = _valid_intent()
    bad = dict(base, poll=True)
    _rules, err = routeops.validate_routes(bad)
    assert err is not None and "poll" in err


# ──────────────────────────────────────────────
# 续上命中（2026-10-20）
#
# 场景一旦命中并开始固化，判据里那几位（网关，有时连地址）就是我们自己写上去的。
# 不续的话「命中一次」必然变成「下一次判不中」，路由跟着被撤 —— 这条补的就是这一环。
# 只有 **dhcp ＋ pin**（上次是 DHCP 固化租约、现在网卡是 static）才续，``mode: static``
# 一律不续；续期只核对**配置声明过的位**（2026-10-20 用户裁定），没声明的一概不管。
# ──────────────────────────────────────────────

PINNED_OFFICE = {
    "name": "office",
    "iface": "en5",
    "gateway": "",
    "destinations": ["10.99.2.0/24"],
    "net": {"mode": "dhcp", "pin": {"ip": "10.99.1.21", "mask": "255.255.255.0"}},
}

#: 我们上次写下去的那一份（守护回显 ``net.wrote``）
WROTE = {"mode": "dhcp+pin", "ip": "10.99.1.21", "mask": "255.255.255.0",
         "gateway": "", "dns": []}


def _applied_once(rule_name="office", iface="en5", wrote=None):
    """上一轮的回显：这条规则生效过、落在 ``iface`` 上，网卡段记着我们写的 ``wrote``。"""
    rules_map = {rule_name: {"name": rule_name, "applied": True, "iface": iface}}
    net_seg = {"iface": iface, "action": "applied", "wrote": dict(wrote or WROTE)}
    return rules_map, net_seg


def _continued(rule=None, prior_net=None, current=None, **kw):
    """探不中（``probe`` 恒 False）时这条规则最终报成什么。"""
    prior_rules, net_seg = _applied_once()
    return intent_mod.build_intent(
        _routes([rule or PINNED_OFFICE]), None, probe=lambda *_a: False,
        prior_rules=kw.get("prior_rules", prior_rules),
        prior_net=kw.get("prior_net", net_seg if prior_net is None else prior_net),
        config_probe=kw.get("config_probe") or (lambda _iface: current),
    )["rules"][0]


def test_a_pinned_scene_keeps_matching_while_its_own_config_is_still_on_the_nic():
    """网卡还停在我们按这份 net 写下去的样子 ⇒ 续上命中（探不中也算命中）。"""
    rule = _continued(current={"mode": "static", "ip": "10.99.1.21",
                               "mask": "255.255.255.0", "gateway": "", "dns": []})
    assert rule["trigger_matched"] is True, "固化过的场景不该因为我们自己的写入而失效"
    assert rule["apply"] is True
    assert rule["iface"] == "en5", "续上的是上次那张网卡，不是候选里的第一张"


def test_a_continuation_ends_when_the_user_puts_the_nic_back_on_dhcp():
    """用户把网卡改回 DHCP ⇒ 立刻不续（「还是固定 IP 模式」这条不成立）。"""
    rule = _continued(current={"mode": "dhcp", "ip": "10.99.1.21",
                               "mask": "255.255.255.0", "gateway": "", "dns": []})
    assert rule["trigger_matched"] is False
    assert rule["apply"] is False, "不是我们固化的样子了，就不能再声称在这个网络里"


def test_a_continuation_ends_when_a_declared_field_drifts():
    """配置声明了 ip / mask ⇒ 它俩任何一位变了就断。"""
    rule = _continued(current={"mode": "static", "ip": "10.99.1.99",
                               "mask": "255.255.255.0", "gateway": "", "dns": []})
    assert rule["trigger_matched"] is False


def test_an_undeclared_field_never_ends_a_continuation():
    """配置没声明 gateway ⇒ 用户自己加的 router 不该让场景「离开」网络。

    这是 2026-10-20 用户裁定的那一条：**DHCP 固定什么就只检测什么**。pin 只固化
    地址，就只核「还是固定模式 ＋ 地址没变」；用户之后加了个网关、换了个 DNS，
    与「我在不在办公室」无关。
    """
    rule = _continued(current={"mode": "static", "ip": "10.99.1.21",
                               "mask": "255.255.255.0", "gateway": "10.99.1.254",
                               "dns": ["8.8.8.8"]})
    assert rule["trigger_matched"] is True


def test_a_blank_pin_compares_against_what_we_last_wrote():
    """``pin`` 留空 = 「用 DHCP 拿到的那个」⇒ 跟**我们上次写下的**比（没有第二个真源）。"""
    rule = {"name": "office", "iface": "en5", "gateway": "",
            "destinations": ["10.99.2.0/24"],
            "net": {"mode": "dhcp", "pin": {"ip": "", "mask": ""}}}
    same = _continued(rule=rule, current={"mode": "static", "ip": "10.99.1.21",
                                          "mask": "255.255.255.0"})
    assert same["trigger_matched"] is True

    other = _continued(rule=rule, current={"mode": "static", "ip": "10.9.0.7",
                                           "mask": "255.255.255.0"})
    assert other["trigger_matched"] is False, "地址变了就不再是我们固化的那一份"


def test_a_scene_that_writes_no_net_never_continues():
    """不写网卡的场景没有「我们留下的样子」可核 ⇒ 不许续（否则永不退出，就是自证循环）。"""
    rule = _continued(rule={**PINNED_OFFICE, "net": {"mode": "dhcp"}},
                      current={"mode": "static", "ip": "10.99.1.21",
                               "mask": "255.255.255.0"})
    assert rule["trigger_matched"] is False


def test_continuation_is_off_while_paused_pinned_or_manual_only():
    """停用 / 手动指定 / ``manual_only`` 三条路都不许被续期偷偷拉回来。"""
    current = {"mode": "static", "ip": "10.99.1.21", "mask": "255.255.255.0"}
    prior_rules, prior_net = _applied_once()

    paused = intent_mod.build_intent(
        _routes([PINNED_OFFICE], paused=True), None, probe=lambda *_a: False,
        prior_rules=prior_rules, prior_net=prior_net,
        config_probe=lambda _i: current)["rules"][0]
    assert paused["trigger_matched"] is False

    pinned = intent_mod.build_intent(
        _routes([PINNED_OFFICE, {**PINNED_OFFICE, "name": "home"}]), None,
        probe=lambda *_a: False, pinned="home",
        prior_rules=prior_rules, prior_net=prior_net,
        config_probe=lambda _i: current)["rules"][0]
    assert pinned["trigger_matched"] is False, "手动指定时只有被点亮那条生效"

    manual = intent_mod.build_intent(
        _routes([PINNED_OFFICE]), None, probe=lambda *_a: False,
        manual_only=["office"], prior_rules=prior_rules, prior_net=prior_net,
        config_probe=lambda _i: current)["rules"][0]
    assert manual["trigger_matched"] is False, "§2.6：manual_only 只能被 pinned 点亮"


def test_a_static_config_never_continues():
    """``mode: static`` 不是「DHCP 固化租约」⇒ 永不续（2026-10-20 用户裁定）。

    static 写死的地址本来就该由匹配判据自己判出来；拿「上次命中」给它续命，等于
    让一份本该靠现场判断的配置永远自证成立。
    """
    rule = {"name": "office", "iface": "en5", "gateway": "",
            "destinations": ["10.99.2.0/24"],
            "net": {"mode": "static", "ip": "10.99.1.21", "mask": "255.255.255.0"}}
    got = _continued(rule=rule, current={"mode": "static", "ip": "10.99.1.21",
                                         "mask": "255.255.255.0"})
    assert got["trigger_matched"] is False


def test_a_continuation_needs_the_last_round_to_have_been_dhcp_pinned():
    """上一轮写的不是 ``dhcp+pin``（或没有这份回显）⇒ 不算「上次是 DHCP 固化」。"""
    prior_rules, _ = _applied_once()
    current = {"mode": "static", "ip": "10.99.1.21", "mask": "255.255.255.0"}
    for wrote in ({"mode": "static", "ip": "10.99.1.21", "mask": "255.255.255.0"}, {}):
        got = intent_mod.build_intent(
            _routes([PINNED_OFFICE]), None, probe=lambda *_a: False,
            prior_rules=prior_rules,
            prior_net={"iface": "en5", "action": "applied", "wrote": wrote},
            config_probe=lambda _i, _c=current: _c)["rules"][0]
        assert got["trigger_matched"] is False, f"wrote={wrote} 不该被当成 dhcp 固化"
