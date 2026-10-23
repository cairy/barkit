"""网卡配置的**纯换算**：三态常量 ＋ ``配置 ↔ 窗体取值`` 那三个函数。

本模块**运行期零 PySide6** —— 它只认得 dict，不认得控件。放在这里是刻意的：「哪些形状算合法」的判据只有 ``rules.validate_net`` 一处，而换算必须与它同源，把这段混进 UI 只会让判据多一个藏身的地方。

``_NET_DRAFT_KEY`` 那个键名故意用下划线开头：它是**窗体里**的半成品暂存位，不是配置键（配置里出现它就是脏数据）。

``settings.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import re

from barkit.plugins.route_guard import rules


#: 窗体里**不是配置**的那一块：用户填了一半的网卡配置先存在这里（键名故意用下划线开头，
#: 与真配置键区分开）。
#:
#: 为什么需要它：静态 IP 的 ``ip`` / ``mask`` 是必填，而「先选模式、再填地址」是唯一
#: 可行的输入顺序 —— 选中的那一刻必然没有地址。要是照写，用户一点模式就会吃到一条
#: 校验失败、控件还被弹回去，等于**永远走不到那一态**。于是与「刚新增、还没填完的场景」
#: 同一种处置（列表上标「还没填完」）：**先只活在窗体里**，而**保存时它是拦路虎**
#: （见 `_SceneEditor.save`）——「保存」这一下的含义是「整张表写进去」，一半写一半不写
#: 会让用户以为全存了。
_NET_DRAFT_KEY = "_net_draft"

#: 网卡配置三态（**窗体取值**，不是配置里的取值 —— 配置里是 ``net.mode`` ＋ 可选的
#: ``net.pin``，见 `_net_ui_mode` 的对应表）。
_NET_FORM_NONE = "none"
_NET_FORM_PIN = "pin"
_NET_FORM_STATIC = "static"

#: 「照抄租约」那两枚勾选的文案（网关 / DNS 各一枚，位置就在各自那一行的行尾，
#: 所以只说「照抄 DHCP」——共用一份，免得两处字面量各写一遍）。
_NET_LEASE_LABEL = "照抄 DHCP"


# ──────────────────────────────────────────────
# 「监控网卡」那一格 ↔ 网卡候选清单
# ──────────────────────────────────────────────
#
# 多网卡（第一命中制）落地后这一格收的是**一张清单**而不是一张网卡：扩展坞与直插转换器
# 在系统里拿到的名字不同，用户要的是「哪个接上都算」。分隔口径只写一份 —— 装载
# （``settings_list``）与收集（``settings_draft``）各写一遍必然在「用户填了空格」上分叉。

#: 分隔符：逗号 / 空白 / 顿号都认（名字是从 ``ifconfig`` 里抄来的，手边是什么就用什么）。
_IFACE_SPLIT = re.compile(r"[,\s、]+")


def parse_iface_field(text) -> "list[str]":
    """「监控网卡」那一格 → 网卡候选清单（**原序去重** —— 顺序即候选优先级）。

    空输入 ⇒ 空清单（不是 ``[""]``）：那一格留空是「还没填完」，由保存那一步拦，
    这里不替它编一个网卡名出来。
    """
    out: "list[str]" = []
    for raw in _IFACE_SPLIT.split(str(text or "").strip()):
        name = raw.strip()
        if name and name not in out:
            out.append(name)
    return out


def format_iface_field(ifaces) -> str:
    """网卡候选清单 → 「监控网卡」那一格的文本（逗号 + 空格，回读同一份分隔符口径）。"""
    return ", ".join(str(x).strip() for x in (ifaces or []) if str(x).strip())


def _net_ui_mode(net) -> str | None:
    """配置里的 ``net`` → 三态之一；**认不出的形状返回 ``None``**（窗体不碰它，只提示手改）。

    对应表（恰好这三种，判据直接复用 ``rules.validate_net`` —— 求值路径的同一只校验器，
    自己再写一份「哪些键算合法」就是第二真源）：

    | 配置 | 三态 |
    |---|---|
    | 不写 / ``{"mode": "dhcp"}`` | ``_NET_FORM_NONE``（一根手指都不碰网卡）|
    | ``{"mode": "dhcp", "pin": {...}}`` | ``_NET_FORM_PIN`` |
    | ``{"mode": "static", ...}`` | ``_NET_FORM_STATIC`` |

    为什么要那个 ``None``：用户手写的 ``net`` 只错一处（如 ``pin`` 少了 ``mask`` 键、
    模式敲错一个字母）时，三态里没有一个装得下它。硬塞进「跟随系统」再顺手把原值写掉，
    就是**替用户静默改配置** —— 本组件的口径是报错指路、让用户自己改（同「旧 ``rules[]``
    不自动迁移」）。所以这里的 ``None`` 不是「不知道」，是「**这个形状归你手改**」。
    """
    if net is None:
        return _NET_FORM_NONE
    if not isinstance(net, dict) or rules.validate_net(net, "net"):
        return None
    if net.get("mode") == rules.NET_MODE_STATIC:
        return _NET_FORM_STATIC
    return _NET_FORM_PIN if isinstance(net.get("pin"), dict) else _NET_FORM_NONE


def _net_to_form(net) -> dict:
    """``net`` → 四个输入框的初值 ＋ 模式（`_net_ui_mode` 的逆运算，供装载用）。

    ``dns`` 收成**一个逗号分隔的字符串**：配置里它是列表，而窗体给它一格输入框 ——
    地址一般就一两个，摆一排动态行是杀鸡用牛刀（``FeedbackBar`` 那样的行内增删是给
    路由目标用的，那里条数真的会多）。
    """
    blank = {"mode": _NET_FORM_NONE, "ip": "", "mask": "", "gateway": "", "dns": "",
             "gateway_from_lease": False, "dns_from_lease": False}
    if not isinstance(net, dict):
        return blank
    if net.get("mode") == rules.NET_MODE_STATIC:
        src, mode = net, _NET_FORM_STATIC
    else:
        pin = net.get("pin")
        if not isinstance(pin, dict):
            return blank
        src, mode = pin, _NET_FORM_PIN
    return {
        "mode": mode,
        "ip": str(src.get("ip") or ""),
        "mask": str(src.get("mask") or ""),
        "gateway": str(src.get("gateway") or ""),
        "dns": "、".join(str(x) for x in (src.get("dns") or [])),
        # 「照抄租约」两位只可能是布尔；静态 IP 那一态没有这个概念（它本来就没租约可抄），
        # 取不到即 False —— 与那两格勾选框在静态态下不出现是同一件事。
        "gateway_from_lease": src.get("gateway_from_lease") is True,
        "dns_from_lease": src.get("dns_from_lease") is True,
    }


def _net_from_form(form: dict) -> tuple[dict | None, str]:
    """四个输入框 ＋ 模式 → 配置里的 ``net``。返回 ``(net, 还没填完的原因)``。

    - ``none``   → ``(None, "")``：**连 ``net`` 键都不写**（＝跟随系统，一根手指都不碰）；
    - ``pin``    → ``{"mode": "dhcp", "pin": {ip, mask, ...}}``：``ip`` / ``mask``
      **两个键都在、值可留空** —— 「留空 = 用 DHCP 拿到的那个」正是 §2.5「获取到 IP 后
      设置为固定 IP」的字面实现（用户不必预先知道地址）；
    - ``static`` → ``{"mode": "static", ip, mask, ...}``：``ip`` / ``mask`` **必填**。

    第二项返回值只判**必填项空没空**，**不做合法性校验**（IPv4 格式、网关在不在网段内）：
    那是 ``rules.validate_net`` 与 helper 的事，一次都不重写（判据只有一处）。填到格式
    不对时由**保存**那一刻报错（改动留在窗体里，不回滚）—— 与名称 / 网卡那些字段同一条路。

    ⚠️ **``pin`` 那一态永远「填得完」**（两个键都在、值可空），所以选它当场就是合法的。
    这一点很要紧：要是它也要求非空，用户点一下模式就得先知道地址，选择就成了一道关卡。
    """
    mode = form["mode"]
    if mode == _NET_FORM_NONE:
        return None, ""
    # 三种分隔符都认：装载时是用顿号拼出来的（`_net_to_form`），而用户手打时最常见的是
    # 逗号。漏认一种的后果不是「拆不开」而是**拆错了**——整串会被当成一个地址，然后在
    # 校验那一步报「不是合法 IPv4」，用户看着一个明明填对的框不知道错在哪。
    dns = form["dns"].replace(",", " ").replace("，", " ").replace("、", " ").split()
    if mode == _NET_FORM_STATIC:
        if not form["ip"] or not form["mask"]:
            return None, "网卡配置还没填完：静态 IP 的「IP」与「掩码」都是必填，填完才能保存。"
        out: dict = {"mode": rules.NET_MODE_STATIC, "ip": form["ip"], "mask": form["mask"]}
        if form["gateway"]:
            out["gateway"] = form["gateway"]
        if dns:
            out["dns"] = dns
        return out, ""
    pin: dict = {"ip": form["ip"], "mask": form["mask"]}
    # 网关 / DNS 留空 = **不固化**（不是「照抄租约」）：照抄会把「这根手指不碰它」
    # 变成「钉死在某个值」，正是用户写空值要躲开的事（helper `_target_of` 同口径）。
    # 要照抄得**显式勾**，那时写的是 `*_from_lease: true` 而不是值 —— 二者互斥
    # （`rules.validate_net` 会拒同时给），这里按勾选状态只写一边。
    if form.get("gateway_from_lease"):
        pin["gateway_from_lease"] = True
    elif form["gateway"]:
        pin["gateway"] = form["gateway"]
    if form.get("dns_from_lease"):
        pin["dns_from_lease"] = True
    elif dns:
        pin["dns"] = dns
    return {"mode": rules.NET_MODE_DHCP, "pin": pin}, ""
