"""**叶子层**校验件：IPv4 与网段两个词法判据、``_is_bool``、``_require_dict``、
``_unknown_keys``（未知键那句）、``_bad_bool``、``_validate_ipv4_list``，外加两个**判据**
—— ``net_writes``（「这份 ``net`` 会不会真写网卡」）与 ``dest_was_added``（「回显里这一条
目标本轮是加还是撤」）。

``net_writes`` 单独点出来是因为它**不是校验而是判据**（helper 侧有一份同样的：
``routeops._target_of`` 返回 ``None`` 即它）。两侧必然各有一份（不同进程、不同包），
所以要一起改；判错的方向是安全的（判成「会写」只是多读一次网卡配置）。

``dest_was_added`` 则是**一处实现两处消费**（面板结果计数 ＋ 停用句里那个数字）：
v7 起一条规则可以有的加、有的撤，两处各写一遍必分叉。

``route_gateway_src``（v8）读的是回显里新增的 ``gateway_src``：「这个下一跳从哪来」。
放在这里的理由与上面两条一样 —— 它是**判据**，面板那格状态与那句文案都要用它；
它同时也是 helper 那份字面量的第二个实现，故与写的一侧一起被门禁钉住（见该函数）。

依赖：只认标准库 ``ipaddress`` 与 ``keys``（``NET_MODE_STATIC`` / ``_UNKNOWN_TAIL``）。

``rules.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import ipaddress

from .keys import NET_MODE_STATIC


def valid_ipv4(text) -> bool:
    try:
        ipaddress.IPv4Address(text)
        return True
    except (ipaddress.AddressValueError, ValueError, TypeError):
        return False


def valid_destination(text) -> bool:
    """合法 IPv4 网段 / 主机（``10.199.2.0/24``、``192.168.200.5/32``）。"""
    try:
        return ipaddress.ip_network(text, strict=False).version == 4
    except (ValueError, TypeError):
        return False


def _is_bool(value) -> bool:
    return isinstance(value, bool)


def net_writes(net) -> bool:
    """这份 ``net`` 会不会**真写网卡**（§2.5）。纯 DHCP ⇒ 一根手指都不碰。

    它是「纯 DHCP」与「配成 DHCP」的分界线：两者在用户看来一样，在代码里差很远 ——
    后者会产生写入，也因此需要 baseline 与交还。

    helper 侧有一份同样的判据（``routeops._target_of`` 返回 ``None`` 就是它）。两侧
    必然各有一份（不同进程、不同包），所以要一起改。判错的方向是安全的：这里判成
    「会写」只是多读一次网卡配置，判成「不会写」最多是不往交还清单里列它。
    """
    if not isinstance(net, dict):
        return False
    if net.get("mode") == NET_MODE_STATIC:
        return True
    # ``mode: dhcp`` 下只有 ``pin`` 会写（纯 DHCP 连 mode 都可以不写）
    return isinstance(net.get("pin"), dict)


def dest_was_added(dest) -> bool:
    """守护回显里这一条目标本轮是被**加**上去的吗。

    v7 起一条规则可以「有的加、有的撤」⇒ 规则的 ``applied`` 一位**不再**等于「这条规则
    里每个目标都加上去了」。按 ``applied`` 数会把撤下来的目标也数成「已叠加」，所以判据
    必须落在**逐目标**的 ``action`` 上（两个消费方：面板结果计数与停用句里那个数字）。

    ``hold``（v8：留空的下一跳取不到网关 ⇒ 这条**一个都没动**）既不是加也不是撤，
    故按「没加上去」算 —— 把它数进「已叠加」就是谎报（那个数字说的是「你关掉自动化
    之后仍然生效着的东西」）。

    缺 ``action``（旧版守护的回显）按「加」算：那时一条规则只有一种动作，多算一条的
    下场只是文案里的数字偏大 —— 比少算安全。
    """
    return str(dest.get("action") or "add") not in ("remove", "hold")


#: helper 回显里 ``gateway_src`` 的三个取值 —— **真源在写的那一侧**
#: （``capabilities/helper/routeops/routeops.py`` 的 ``GATEWAY_SRC_*``：那一段是它
#: 写下的）。两侧必然各有一份字面量（不同进程、不同包），由
#: ``tests/test_helper_contract.py::test_gateway_src_vocabulary_matches_on_both_sides``
#: 把它们钉在一起：改一边不改另一边，那条用例当场转红。
GATEWAY_SRC_RULE = "rule"       # 配置 / 编译期写死的那个（一直如此）
GATEWAY_SRC_IFACE = "iface"     # helper 从本场景固化那块网卡上取的（v8）
GATEWAY_SRC_NONE = "none"       # 取不到 ⇒ 这条**一个都没动**


def route_gateway_src(rule) -> str:
    """这条规则本轮那个下一跳**是从哪来的**（helper 回显的 ``gateway_src``，v8）。

    - ``rule``：用户 / 配置写死的那个（一直如此）；
    - ``iface``：helper 从本场景固化那块网卡上取的 —— 用户压根没写过那个值，面板该把
      它说出来（否则他无从核对自己那条留空的路由到底走了谁）；
    - ``none``：取不到 ⇒ 这条**一个都没动**（加要网关，撤也要网关：``ensure_removed``
      先确认那条路由确实经这个网关生效）。它既不是「已叠加」也不是「失败」，
      面板据此报 ``no_gateway`` 那格警告 —— 但**只对本轮 ``applied``（该它上场）的那几条**：
      没命中的场景也留空、来源也是 ``none``，可它本轮本来就不该动，那不是「该动而没动」
      （判据在 ``reports._routes_report``，理由与代价写在那边的 docstring 里）。

    缺这一栏（旧版守护的回显）按 ``rule`` 算：那时留空的下一跳进不来（校验会拒），
    所以「缺 = 有人写死了」是当时唯一可能的读法。
    """
    src = str(rule.get("gateway_src") or "")
    known = (GATEWAY_SRC_RULE, GATEWAY_SRC_IFACE, GATEWAY_SRC_NONE)
    return src if src in known else GATEWAY_SRC_RULE


def _require_dict(value, where: str) -> str | None:
    return None if isinstance(value, dict) else f"{where} 必须是对象"


#: 「这个键我不认识」那句的尾巴：把用户最该做的两个动作直接写出来。
#:
#: 早先这句只有「含未知字段：['force_apply']」—— 读起来像**用户自己打错了字**（用户
#: 原话：「防止用户理解错误」）。而实测里绝大多数是**旧版本 BarKit 自己写下的键**
#: （如面板开关从前写的 ``force_apply``：用户从没手编过它，是程序写进去的），
#: 新版程序把它当未知键拒了。定性指错方向，用户就会去自己的 YAML 里找错别字，
#: 而正确动作是删掉那几行（界面上看不见它，只能手改）—— 现在还能一键清掉。
_UNKNOWN_TAIL = (
    "——多半是旧版本 BarKit 自己写下的（不是您写错了）；删掉这几行即可，"
    "也可以在设置窗里这个组件的页尾点「恢复默认配置」"
)


def _unknown_keys(obj: dict, allowed: frozenset, where: str) -> str | None:
    bad = sorted(set(obj) - allowed)
    if not bad:
        return None
    return f"{where} 有不认识的键：{bad}{_UNKNOWN_TAIL}"


def _bad_bool(value, where: str) -> str | None:
    return f"{where} 必须是布尔值：{value!r}" if not _is_bool(value) else None


def _validate_ipv4_list(value, where: str) -> str | None:
    if not isinstance(value, list) or not value:
        return f"{where} 必须是非空数组"
    for item in value:
        if not isinstance(item, str) or not valid_ipv4(item):
            return f"{where} 含非法 IPv4：{item!r}"
    return None
