"""route_guard/intent：路由**期望态**（policy）的计算（批次 4 · S3b，档 B）。

为什么有这个模块
----------------
方案 §7.4.2 / §7.4.3 要求「helper 不提供业务逻辑」：root 侧只保留**机制**
（怎么加/删路由、怎么查询网卡），「什么条件下该加哪条路由」是 policy，归插件。
于是 app 侧在这里算好**完整期望态**交给守护；helper 的 ``route.sync`` 只做「按意图
幂等收敛」，不再自己做任何匹配决策。

载体：2026-09-29 起是**一帧**，不是文件
--------------------------------------
从前这个模块负责把期望态原子写进 ``routes.json``（唯一落盘入口 ``write_intent``）；
现在它是 ``intent.set("route", …)`` 的一帧 —— 组件调 ``declare("route", obj)``，
路径彻底离开两侧的约定。于是本模块只剩**决策**：算出完整期望态，交给调用方送出去。

policy 语义（与迁移前 helper 内逻辑逐条等价）
---------------------------------------------
- ``trigger_matched`` = 接口在线 且 当前网关列表含规则网关（``manual_only`` 的场景
  一律 False —— 它退出自动匹配，见下）
- 自动：触发命中 且 规则启用
- ``pinned``（手动指定生效场景，§2.3）→ **忽略匹配**，只有被指定的那一条生效：
  「忽略」的意思就是别的场景这一轮一个都不要动
- ``manual_only``（§2.6）→ 退出自动匹配：它的判定依据（网卡 IP / 网关）正是组件
  自己会写的东西，拿它判定就是自证循环（判成真的那一刻起永远为真）。只能被
  ``pinned`` 点亮
- ``paused``（自动化暂停）→ 规则**冻结**：沿用上轮实际生效态
  （上轮态来自守护回显，与本步之前的 helper 行为一致）

``pinned`` / ``manual_only`` **不进意图正文**
--------------------------------------------
线格式（``rules[].{name,priority,iface,gateway,destinations,enabled,off_destinations,apply,
trigger_matched,net}`` ＋顶层 ``{version,poll,paused,handback,handback_dns}``）是 root
helper 在消费的契约：多一个它不认识的
键，就是**整份拒绝 + 冻结**（见上文）。故 ``pinned`` / ``manual_only`` 由 app 侧就地折进
``apply`` 一位里，helper 眼里一个字节都没变。

**那三个例外：``net`` / ``handback``（第 8 步）与 ``off_destinations``（v7）**。它们能进
契约，正是因为各自那一步同时 bump 了 ``HELPER_VERSION`` ——「要动线格式，就得跟着重装」
这条铁律的正面用法。

第 4 个例外是 **v8 的「空的 ``gateway``」**：它不是新键，是新**语义**（「这个下一跳由你
从这块网卡上取」，只对带 ``pin`` 的 ``net`` 成立）。同样 bump 了版本 ⇒ 须重装。

曾经还有第三个例外 ``ops``（命令通道，2026-09-29 当天加、当天删）：显式动作现在走会话帧
``op.run``（一帧一命令、回帧即回执，**不跨重启留状态**），期望态正文只剩「该保持成什么样」。

``force_apply``（强制开 / 强制关）已随《网络切换》§6.3 第 5 步下线：面板开关不再是
「真手动」，而是覆盖层「本次不叠加」（§4.8）——它在配置编译期就落到了 ``enabled`` 一位上，
helper 眼里仍然是同一个开关。「强制开」这条路径删掉的理由：配置层的 ``enabled``
已能表达更持久的意思，而「未命中却强加」本来就是往黑洞里指。

探活为什么放在长驻服务里
------------------------
触发命中要跑子进程（``ifconfig`` / ``ipconfig getpacket`` / ``route -n get``），
放进 tick 会占住工作线程。故用框架托管的**周期型 resident service**
（``interval`` 声明节律，线程/时钟/起停全归框架），插件不碰线程。
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Callable, Iterable, Optional

# 查询**机制**复用 helper 侧实现（纯查询、无决策），避免两侧语义漂移。
# 注意：不是复用 policy —— helper 侧的 rule_matched 已在本次改动中删除。
from barkit.capabilities.helper.routeops import (
    MODE_DHCP_PIN, current_gateways, current_ipv4, iface_up, read_service_config,
    service_for_iface,
)
from barkit.plugins.route_guard.rules import (
    FALLBACK_DHCP, NET_MODE_DHCP, NET_MODE_STATIC, net_writes,
)

log = logging.getLogger(__name__)

#: 意图契约版本（与 helper ``routeops.INTENT_VERSION`` 一致）。
#: 2 → 3（第 8 步）：顶层多一个 ``handback``、每条规则多一个可选 ``net``。
#: 3 → 4：「照抄租约」—— ``net.pin`` 多两个可选布尔键（形状变了就是契约版本变了）。
#: 4 → 5（2026-09-29）：顶层多一个 ``ops``（**命令通道**）。它把「用户按下的一次性动作」
#: 从顶层 ``handback``（**策略**：无场景接管时才交还）里拆出来 —— 两者挤在一条通道时，
#: 命令被排在策略的判断之后，而场景判据正是我们自己固化出去的地址（自证循环）⇒ 命令
#: 永远执行不到。同版行为变更：``net.pin`` 留空的「用 DHCP 拿到的那个」改成**真读 DHCP
#: 租约**（原先读的是网卡当前配置 = 我们自己上次写下去的值）。
#: 5 → 6（2026-09-29）：**去掉 ``ops``** —— 命令搬去了会话帧 ``op.run``（一帧一命令、
#: 回帧即回执），期望态正文只剩「该保持成什么样」。同批期望态本身也不再是文件：
#: app 发的是 ``intent.set("route", …)`` 的一帧，路径彻底离开两侧的约定。
#: 6 → 7（2026-10-06）：每条规则多一个可选 ``off_destinations``（``destinations`` 的子集）
#: —— 逐目标开关落在线格式上的那一栏，一位 ``enabled`` 装不下「同一条里 A 加、B 撤」。
#: 7 → 8（2026-10-09）：``gateway`` 允许空串 —— 只对带 ``pin`` 的 ``net`` 成立，
#: 表示「这个下一跳由 helper 从本场景固化那块网卡上取」（那个值在租约里，用户写不出来）。
#: 同版 helper 侧：取不到时该条既不叠也不撤、回显 ``gateway_src: "none"``；``net`` 段
#: 多一栏 ``iface_gateway``、顶层多一栏 ``net_gateways``（网卡 → 网关，跨重启靠快照带回）。
#: 故意图升级**不做兼容** —— 旧 helper 遇到不认识的键会整份拒绝 → 冻结（保持上轮路由
#: 不动），那是安全态；重装即走新契约。
INTENT_VERSION = 8

#: 探活间隔（秒）。网络变化 → 意图更新的最大延迟（远快于 helper 的 poll 默认 30s）。
PROBE_INTERVAL = 5.0

#: 触发命中探测签名：``(iface, subnet, gateway) -> bool``（后两个可能为空串）
ProbeFn = Callable[[str, str, str], bool]


# ──────────────────────────────────────────────
# 查询（机制）
# ──────────────────────────────────────────────

def _subnet_hit(iface: str, subnet: str) -> bool:
    """网卡上是否存在落在该网段的 IPv4（§2.1 的「生效网段」）。

    网段与地址两侧都严格解析：网段来自用户配置（``validate_match`` 已经拦过一道，
    这里再兜一次 —— 判定路径不该因为一个手写坏的网段就抛异常）。
    """
    try:
        net = ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        return False
    for addr in current_ipv4(iface):
        try:
            if ipaddress.ip_address(addr) in net:
                return True
        except ValueError:
            continue
    return False


def network_probe(iface: str, subnet: str = "", gateway: str = "") -> bool:
    """真实探测（§1.2 的触发条件）：网卡在线 ＋ **声明过的**条件全部成立。

    命中规则就是 ``validate_match`` 文档里那两句，此前只有后半个条件真的接上了：

    - **都不写** ⇒ 网卡在线即命中（§2.1 允许空 ``match``）；
    - **只写 ``subnet``** ⇒ 网卡上得有落在该网段的 IPv4 —— 这是本设计的**主路径**
      （判决 4 说 ``gateway`` 是兼容旧配置用的那半）；
    - **只写 ``gateway``** ⇒ 当前网关列表含它（改动前的唯一行为）；
    - **都写** ⇒ **AND**。``match`` 说的是「我在这个网络里」，两个条件各自都是这个
      网络的特征；取或会让「网段对上但网关不对」的半个场景也算命中。
    """
    if not iface_up(iface):
        return False
    if subnet and not _subnet_hit(iface, subnet):
        return False
    if gateway and gateway not in current_gateways(iface):
        return False
    return True


def _pick_iface(rule: dict, probe: ProbeFn) -> tuple[str, bool]:
    """「**第一命中制**」：多网卡候选时，本轮这张规则落在哪一张上（2026-10-05 定案）。

    返回 ``(iface, trigger_matched)``。候选顺序 = ``ifaces`` 的声明顺序（编译层原样
    带过来），取**第一张命中**的那张；一张都不命中 ⇒ **仍落在第一张候选上**，不是不产出
    —— 规则从意图里消失，它此前加的路由就没有任何人去撤（helper 只对意图里现存的规则
    做增删，它没有旁路账本）。此时 ``trigger_matched`` 为假 ⇒ ``apply`` 为假 ⇒ helper
    按「不叠加」收敛，正是「这个场景现在不成立」该有的样子。

    「命中」用的是 ``network_probe`` 这一条判据（在线 ＋ 声明过的 match 条件全成立），
    与单网卡时逐字相同：**两张网卡各自一套判据的话，面板说的「命中了」与路由实际叠给谁
    就会分叉**。``match`` 为空时它退化成「网卡在线」，正是「扩展坞与直插转换器哪个接上
    都算」那个场合要的语义。

    代价：一张都不命中时要探完全部候选（每张一次子进程）。命中即停，且单网卡场景与改动
    前同样是**一次**探测 —— 多候选没有给主路径加成本。
    """
    candidates = [str(x).strip() for x in (rule.get("ifaces") or []) if str(x).strip()]
    if not candidates:
        candidates = [str(rule.get("iface") or "")]
    subnet = rule.get("subnet") or ""
    gateway = rule.get("match_gateway") or ""
    for cand in candidates:
        if probe(cand, subnet, gateway):
            return cand, True
    return candidates[0], False


# ──────────────────────────────────────────────
# 续上命中：固化过的网卡上，场景不该因为我们自己的写入而失效
# ──────────────────────────────────────────────

#: 读一张网卡**此刻**的配置（``{mode, ip, mask, gateway, dns}``）；读不出来 ⇒ None。
#: 可注入，与 ``probe`` 同一个理由：判定路径不该在测试里真去 shell 出子进程。
ConfigFn = Callable[[str], Optional[dict]]


def read_iface_config(iface: str) -> Optional[dict]:
    """网卡此刻的配置（机制，复用 helper 侧同一份读法）。"""
    service = service_for_iface(iface)
    return read_service_config(service) if service else None


def _still_pinned(net: dict, wrote: Optional[dict], current: Optional[dict]) -> bool:
    """网卡此刻是否**仍然停在这份 ``net`` 要求的那份配置上** —— 只核配置声明过的位。

    为什么需要它：场景一旦命中并开始固化，网卡的网关（有时连 IP 也是我们写上去的）
    就变成「我们自己的产物」。这时候再拿它去判「我在不在这个网络里」，等于用自己写下
    的答案回答自己的问题 —— 换场景/重启之后必然判不出来。

    只服务**一种**配置：``mode: dhcp`` ＋ ``pin``（2026-10-20 用户裁定 —— 只有「上次是
    DHCP 固化租约、现在网卡是静态且对得上」才算续上，``mode: static`` 不在此列）。
    **「哪种配置才够格」由调用方（``_continue_hit``）一处判定**，这里只管比字段。
    判据**只跟配置承诺管的那几位走**（别加码）：

    - ``pin`` 写死的位照它比；**留空的位**（= 「用 DHCP 拿到的那个」）与我们**上次写下去
      的那份**（``wrote``）比 —— 那个值没有第二个真源。
    - ``gateway`` / ``dns`` 同理，只有显式要固化（``*_from_lease``）时才比。
    - 没声明的那几位**一概不管**：用户自己加了个 router、换了个 DNS，都不该让一个已经
      固化着的场景突然变成「不在这个网络里」。

    网卡此刻必须是 ``static``（`dhcp+pin` 落盘就是 Manual）；用户把它切回 DHCP 或手改
    了地址，这里必然对不上 ⇒ 不续。读不到配置一律按「不在」办。
    """
    if not isinstance(net, dict) or not isinstance(current, dict):
        return False
    if current.get("mode") != NET_MODE_STATIC:
        return False
    wrote = wrote if isinstance(wrote, dict) else {}
    pin = net.get("pin") if isinstance(net.get("pin"), dict) else {}
    want = {
        "ip": pin.get("ip") or wrote.get("ip"),
        "mask": pin.get("mask") or wrote.get("mask"),
        "gateway": pin.get("gateway") or (
            wrote.get("gateway") if pin.get("gateway_from_lease") else ""),
        "dns": pin.get("dns") or (
            wrote.get("dns") if pin.get("dns_from_lease") else []),
    }
    if not (want["ip"] and want["mask"]):
        return False
    if current.get("ip") != want["ip"] or current.get("mask") != want["mask"]:
        return False
    if want["gateway"] and current.get("gateway") != want["gateway"]:
        return False
    if want["dns"] and list(current.get("dns") or []) != list(want["dns"]):
        return False
    return True


def _continue_hit(rule: dict, prior_rule: dict, prior_net: dict,
                  config_probe: ConfigFn) -> str:
    """探不中时，看能不能**续上**：这台网卡上还停着我们上次按这份配置写下去的样子。

    返回续上的网卡（空串＝续不上）。五条同时成立才算（少一条都不续）：

    1. 这条规则是 **``mode: dhcp`` ＋ ``pin``**（DHCP 固化租约）—— 2026-10-20 用户裁定：
       只有这一种算续上，``mode: static`` 与「没写过」都不算；
    2. 上一轮**是本条施加的**（回显 ``applied``），且那次写的确实是 ``dhcp+pin``
       （``prior_net.wrote.mode``）—— 没留下这个证据就当「上次不是」；
    3. 网段确实是它（``prior_net.iface``）；
    4. 网卡**还在线**（拔了 dock 就该退出，不能续）；
    5. 网卡此刻是 **static 且仍是这份配置要求的那几位**（``_still_pinned``，只核声明过的）
       —— 用户切回 DHCP 或手改了 IP 就必然对不上。

    刻意不看「上次命中的场景名」：名字会改、会删，而这几条都是当场可核对的事实。
    """
    net = rule.get("net")
    if not net_writes(net):
        return ""
    if str(net.get("mode") or "") != NET_MODE_DHCP:
        return ""
    if not prior_rule.get("applied"):
        return ""
    iface = str(prior_rule.get("iface") or "")
    if not iface or str(prior_net.get("iface") or "") != iface:
        return ""
    wrote = prior_net.get("wrote")
    if not isinstance(wrote, dict) or wrote.get("mode") != MODE_DHCP_PIN:
        return ""
    if not iface_up(iface):
        return ""
    if not _still_pinned(net, wrote, config_probe(iface)):
        return ""
    return iface


# ──────────────────────────────────────────────
# 决策（policy）—— app 侧唯一入口
# ──────────────────────────────────────────────

def compute_apply(rule: dict, trigger_matched: bool, paused: bool,
                  prior_applied: Optional[bool], *,
                  pinned: str = "", manual: bool = False) -> bool:
    """单条规则本轮是否应生效。

    ``prior_applied`` 为上轮实际生效态（来自守护上一次报来的观测），**仅**暂停冻结时使用。

    ``pinned`` / ``manual`` 两个关键字把「谁来挑场景」（§2.3 三态 / §2.6）折进这一个
    决定里：

    - ``pinned`` 非空 = 用户手动指定了场景 ⇒ **忽略匹配**：只有它生效，别的这一轮
      一个都不要动（「忽略」不是「额外加一条」）；
    - ``manual`` = 这个场景 ``manual_only`` ⇒ 退出自动匹配（判定依据会被组件自己写掉
      ⇒ 自证循环），只能靠 ``pinned`` 点亮。

    *（旧签名里那个``force``的前两个分支已随 §4.8 下线，见模块 docstring。）*
    """
    if paused:
        return bool(prior_applied)      # 冻结：沿用上轮，不动路由
    if pinned:
        return rule.get("name") == pinned and bool(rule.get("enabled", True))
    if manual:
        return False                    # §2.6：不参与自动匹配，只等 pinned
    return trigger_matched and bool(rule.get("enabled", True))


def build_intent(routes: dict, prior_applied: dict | None = None,
                 probe: ProbeFn | None = None, *,
                 pinned: str = "", manual_only: "Iterable[str]" = (),
                 prior_rules: dict | None = None, prior_net: dict | None = None,
                 config_probe: ConfigFn | None = None) -> dict:
    """规则定义（``rules.build_routes`` 产物）+ 上轮生效态 → 完整期望态。

    ``probe`` 可注入（测试用假网络）；缺省走真实探测。
    ``pinned`` / ``manual_only`` 来自配置段，**只在这里参与决策、不写进产物**
    （理由见模块 docstring）：前者是手动指定的场景名，后者是 ``manual_only`` 的场景名集合。

    ``prior_rules`` / ``prior_net`` / ``config_probe`` 只服务「续上命中」
    （``_continue_hit``）：上一轮完整回显里的规则表与网卡段、以及读网卡此刻配置的口。
    缺省（None）＝ 不续，行为与从前逐字相同。
    """
    probe = probe or network_probe
    prior_applied = prior_applied or {}
    prior_rules = prior_rules or {}
    prior_net = prior_net or {}
    manual = {str(n).strip() for n in manual_only}
    pinned = str(pinned or "").strip()
    paused = bool(routes.get("paused"))
    clean = []
    for rule in routes.get("rules") or []:
        name = rule["name"]
        # 触发条件取 ``subnet`` / ``match_gateway``（**匹配用的那两个**），不取
        # ``gateway``（那是这条路由从哪个下一跳出去，§2.4）。两者在 `build_routes` 的
        # 产物里是分开的字段，这里只是各取各的。
        # 网卡由 ``_pick_iface`` 按「第一命中制」挑（多候选时）；单候选与从前逐字等价。
        iface, trigger = _pick_iface(rule, probe)
        # 探不中，但这块网卡上还停着我们上次按这份 ``net`` 写下去的样子 ⇒ 续上命中。
        # 固化过的场景，判据里那几位（网关，有时连地址）本身就是我们自己写上去的，
        # 不续的话「命中一次」就会变成「下一次必然判不中」，路由跟着被撤掉。
        # 三个例外不续：停用（保持现状）、手动指定（只认指定那条）、``manual_only``
        # （§2.6：它本来就退出自动匹配，续期会把它偷偷拉回来）。
        if not trigger and not paused and not pinned and name not in manual:
            continued = _continue_hit(rule, prior_rules.get(name) or {}, prior_net,
                                      config_probe or read_iface_config)
            if continued:
                iface, trigger = continued, True
        entry = {
            "name": name,
            "iface": iface,
            # 空串（v8）= 「这个下一跳由 helper 从本场景固化那块网卡上取」。合法与否的
            # 判据在编译层（``compile._pins_lease``）：只有固化租约的场景能这么写。
            "gateway": rule["gateway"],
            "destinations": list(rule["destinations"]),
            "enabled": bool(rule.get("enabled", True)),
            "apply": compute_apply(rule, trigger, paused, prior_applied.get(name),
                                   pinned=pinned, manual=name in manual),
            # 触发命中 = 「这一条**参不参与**自动匹配、且网卡在线 + 网关对得上」。
            # 它回答的是**自动匹配的资格**，不是本轮的决定（决定在 ``apply``）——
            # 于是 ① 手动指定时被点亮的那条即便网关对不上也照报 False（不假装网络变了，
            # 面板的「多场景同时命中」也不会因此虚报）；② ``manual_only`` 的场景恒 False
            # （它本来就不再参与匹配，报告与行为同源）。
            "trigger_matched": trigger and name not in manual,
        }
        if rule.get("priority"):
            entry["priority"] = rule["priority"]
        # 网卡配置（§2.5）：
        # 原样递给 helper（它从第 8 步起认识这个字段），**不做决策** —— 场景命中与否已经在
        # 上面折进 `apply` 了，剩余部分（租约里抨地址、回滚窗口、交还）全是机制。
        if rule.get("net"):
            entry["net"] = rule["net"]
        # 逐目标开关（v7）：**只在整条要叠的时候递**。``apply`` 为假时 helper 整条按「撤」
        # 收敛，这一栏它一个字节都不看；不递也省得同一件事被两处各判一遍（判分叉了不报错）。
        if entry["apply"] and rule.get("off_destinations"):
            entry["off_destinations"] = list(rule["off_destinations"])
        clean.append(entry)
    return {
        "version": INTENT_VERSION,
        "poll": routes.get("poll", 30),
        "paused": paused,
        "handback": _handback(routes, clean, paused),
        "handback_dns": bool(routes.get("handback_dns", True)),
        "rules": clean,
    }


def _handback(routes: dict, clean: list, paused: bool) -> list[str]:
    """顶层 ``handback``：**策略** —— 要求 helper「把这几个网卡交还系统」。

    「没有任何场景要接管」时，把声明过网卡配置的网卡交还系统。这就是 §2.5
    「退出配置项时要还原」的常态化实现 —— 固化只在命中期间成立，离开就回去。
    ``fallback: keep``（缺省）时什么都不发：**不动才是安全的默认**。

    停用时**什么都不发**：§3.2「停用 = 保持现状」冻住的是**自动化**（含这条策略）。
    而「用户按下的那一次动作」走的是另一条通道（顶层 ``ops``，见
    ``_run_op`` 与 ``build_routes`` 的 ``ops``）—— 它是一次性命令，不受停用影响
    （§4.7 三：显式动作在停用态下仍可达）。**两件事以前共用这里，是 2026-09-29
    那次「点了没用」的直接原因**：命令被排在「无场景接管」这个策略前提之后，
    而场景判据正是我们自己固化出去的地址 ⇒ 命中一次就永远为真 ⇒ 命令一直排队。

    只列 ``net_writes`` 为真的场景的网卡：只有那几张是我们真的会写的，交还它们才有意义。
    列多了不会写坏任何东西（helper 那边交还是幂等的），但会多读一次网卡配置。
    """
    if (paused or any(r["apply"] for r in clean)
            or routes.get("fallback") != FALLBACK_DHCP):
        return []
    hand: list[str] = []
    for rule in clean:
        iface = str(rule["iface"])
        if net_writes(rule.get("net")) and iface not in hand:
            hand.append(iface)
    return hand


# ──────────────────────────────────────────────
# 卸载收尾：**没有判据了，因为不再有文件**
# ──────────────────────────────────────────────
#
# 从前这里有 ``write_intent``（原子写 routes.json，期望态的唯一落盘入口）与
# ``routes_settled(watermark)``（卸载时问「helper 收敛到全撤了吗」——为的是**别删早了**：
# 删早了 helper 下一轮读到「文件不存在」，按既有语义会冻结、撤销永远不生效）。
# 会话通道落地后两个都没了对象：期望态是一帧、没有文件可删，「收敛了没有」由
# **连接本身**回答（守护还在跑就是收到了）。于是 ``DEFER_TIMEOUT_S`` 也一起删——
# 留一个「等几秒」的常量会让人以为还有东西在等。

# ──────────────────────────────────────────────
# 周期型长驻服务（框架托管线程）
# ──────────────────────────────────────────────

class RouteProbeService:
    """周期型长驻服务：探网络 → 重算意图 → **交给 declare 送出去**。

    节律靠实例属性 ``interval`` **声明**（数据，不是时钟）：框架在托管
    线程里每 ``interval`` 秒调用一次 ``poll()``，首轮在 ``interval`` 秒后
    （与 ipad_tunnel 的 ``LinkProbeService`` 同一范式）。

    有意**不**在插件里起线程或用定时器：线程/时钟/起停/join 全归框架
    （批次 5 后段定案，见 ``core/resident.py``）。

    ``poll()`` 也是 UI 动作的复用入口（配置变更 / 手动开关后立刻收敛一次），
    故它必须廉价且可重入。

    ⚠️ 变化检测**整条删掉了**（2026-09-29，第二次改）：先是「参照物从内存换成盘上
    现在的字节」（盘被第三方改写也能发现），现在连盘都没有了 —— 幂等由 ``declare``
    按**内容哈希**保证（同一份内容不会重复上线，``HelperLink`` 里比对 ``rev``）。
    于是这里只剩「算出新的一份，交出去」，没有「和什么比」这件事可错。
    """

    name = "route_probe"

    def __init__(self, provider: Callable[[], Optional[dict]],
                 declare: Callable[[Optional[dict]], bool],
                 interval: float = PROBE_INTERVAL) -> None:
        self.interval = float(interval)
        self._provider = provider
        self._declare = declare
        self.report_problem = None          # 宿主托管前注入（可选）

    def poll(self) -> None:
        """一次探测 + 交出期望态（框架每 interval 秒调一次）。

        ``provider`` 返回 ``None`` ＝ 这一轮没有可交的东西（配置非法），**什么都不发**：
        一份错期望态推到守护面前没有好处，它只会整份拒绝并冻结。
        """
        try:
            new = self._provider()
        except Exception as e:              # noqa: BLE001 - 长驻服务不能因异常退出
            if callable(self.report_problem):
                self.report_problem(f"路由意图计算失败：{e}")
            else:
                log.warning("路由意图计算失败：%s", e)
            return
        if new is None:
            return
        try:
            self._declare(new)
        except Exception as e:              # noqa: BLE001
            log.warning("路由意图下发失败：%s", e)
