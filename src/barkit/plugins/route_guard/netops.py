"""``_NetOpsMixin`` —— 网卡那一半：无匹配策略、`[恢复 DHCP]` 命令与回帧解读。

``plugin.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from functools import partial

from barkit.contribution.specs import ActionResult
from barkit.plugins.route_guard import rules

from .constants import log


def _rule_net_for(intent_obj: dict | None, iface: str) -> dict | None:
    """意图里**当前接管这张网卡的那个场景**声明要写进网卡的配置；没有就是 ``None``。

    「当前接管」＝ ``apply`` 为真 —— 与 helper 收敛时挑场景用的是同一个位（它取的是
    第一条 ``apply`` 且带 ``net`` 的规则）。命令带着这份 ``net`` 下去，helper 才不必
    自己再判一次「该按哪个场景固化」：决策只有一处真源，helper 只做机制。

    刻意**不**看 ``net_writes``：这里要的是「这份配置会不会真写网卡」这件事的下游
    后果，而 ``_target_of`` 自己就会把「纯 DHCP」判成「不写」。
    """
    for rule in (intent_obj or {}).get("rules") or []:
        if (isinstance(rule, dict) and rule.get("iface") == iface
                and rule.get("apply") and isinstance(rule.get("net"), dict)):
            return dict(rule["net"])
    return None


def _op_result_text(result) -> str:
    """``route.renew`` / ``route.release`` 回帧正文 → 给日志看的一句话。

    从前这叫 ``_op_receipt_text(op, receipts)``：拿 ``seq`` 去状态文件里的回执表里配对。
    现在是**帧级回执** —— 守护那条命令执行完直接回了这段结论，没有回执表、没有序号。
    """
    seg = result if isinstance(result, dict) else {}
    iface = str(seg.get("iface") or "网卡")
    if seg.get("error"):
        return f"{iface}：{seg['error']}"
    if str(seg.get("result") or "") == "pinned":
        return f"{iface} 已恢复 DHCP 并固化：{seg.get('ip') or '（地址未知）'}"
    return f"{iface} 已交还给系统（自动获取）"


class _NetOpsMixin:
    """网卡那一半：无匹配策略、`[恢复 DHCP]` 命令与回帧解读。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 无匹配时的策略（`fallback`，§4.7 二；三个全局项之一） ----------

    def fallback_policy(self) -> str:
        """``fallback``：所有场景都不匹配时怎么办（保持现状 / 交还系统）。

        它是 §2.2 从「全局配置」里留下的三项之一（另两项是总闸与节律）—— 「所有场景都
        不匹配时该怎么办」这句话没有哪一个场景可以归属，故只能全局。

        缺省 ``keep``（保持现状，一根手指都不碰网卡）—— 那正是 ``config_schema()``
        里 ``fallback`` 这一栏声明的默认值，此处不再抄一遍。写坏的取值不在这里兜底：
        ``rules.build_routes`` 读**原值**、会当场报错，那是 ``routes`` 机的 ``failed``；
        这里回落缺省只是为了让设置页画得出来（配置非法时它自己那一页会说原因）。
        """
        return str(self.cfg.value("fallback"))

    def set_fallback(self, value: str) -> None:
        """设置页「无匹配时」那一行：``keep`` / ``dhcp``（§4.7 二）。

        取值域那句手写检查留在这里，只因为它引用的 ``rules.FALLBACKS`` **就是**
        ``config_schema()`` 里 ``choices`` 的那一个对象（同一份清单，不是抄写）——
        它换来的是一句人话（"认不出的取值"）而不是声明层的校验文案；真正落盘前
        仍要过 ``cfg`` 的那一道。

        生效是 ``_write_keys`` 那条尾巴：写 YAML → 重算期望态（``intent._handback``
        据此决定要不要发交还）→ 逐台机即时广播，helper 下一轮照办。

        ``dhcp`` 的**代价**必须由 UI 说清（它会让「离开场景」变成一次真实的网卡改动）——
        文案在设置页那一行的说明里，不在这里重复。

        写盘失败 ⇒ 当场落在设置页的反馈槽（这条入口只有设置页在用）；改前返回值被丢掉，
        界面上那一项看着已经切过去了。
        """
        value = str(value)
        if value not in rules.FALLBACKS:
            self._say_error(f"「无匹配时」认不出的取值：{value!r}，未改动任何东西。")
            return
        if self.fallback_policy() == value:
            return

        ok, problem = self._write_keys({"fallback": value}, fail="设置「无匹配时」失败")
        if not ok:
            self._say_error(problem)

    # ---------- 网卡那一半：`[恢复 DHCP]`（§4.7 二**命令**的配置面） ----------

    def handback_ifaces(self) -> "list[str]":
        """「恢复 DHCP」预设：命令要作用在哪几张网卡上（设置页勾选，持久）。

        空列表 = 用户还没配置过 —— 这时徽标下拉**不出现**那一项（设置页仍可配置）。
        这里是**配置**（长期有效）；按下动作产生的是**命令**（顶层 ``ops``，一次性、
        由守护执行、执行完按回执撤掉）—— 两者是两件事，别合并（第 9 步真机故障的根因）。
        """
        v = self.cfg.value("handback_ifaces")
        return [str(x).strip() for x in v if isinstance(x, str) and str(x).strip()]

    def handback_dns(self) -> bool:
        """「恢复 DHCP」预设：交还那一步是否连带把 DNS 也还原成系统原样（缺省 true）。

        缺省 `true` 的真源是 ``config_schema()`` 里 ``handback_dns`` 那一栏
        ——与守护侧 ``_restore_baseline`` 的缺省口径一致：交还本就该把 DNS 也交回去。
        """
        return bool(self.cfg.value("handback_dns"))

    def set_handback_config(self, ifaces: "list[str]", dns: bool) -> None:
        """设置页「恢复 DHCP 配置」：落盘选中的网卡清单 + 是否恢复 DNS。

        两键一次写（``cfg.update``）：**各自**按声明处置 —— 与缺省同值的那个不留键
        （``ifaces`` 空 / ``dns`` 为 true 都是缺省）。改前这两键在同一段代码里两种口径：
        ``ifaces`` 空了会删键，而 ``dns`` 照样把 ``true`` 写下去。

        写盘失败 ⇒ 当场落在设置页的反馈槽（保存按钮那一刻界面照旧，用户得有话说）。
        """
        ifaces = [str(x).strip() for x in (ifaces or []) if str(x).strip()]
        dns = bool(dns)

        ok, problem = self._write_keys({"handback_ifaces": ifaces, "handback_dns": dns},
                                       fail="保存恢复 DHCP 配置失败")
        if not ok:
            self._say_error(problem)

    def rule_ifaces(self) -> list[str]:
        """规则涉及的网卡清单（设置页「恢复 DHCP 配置」勾选清单的数据源）。

        跨场景收集 ``ifaces``、按声明先后去重 —— **与「接管过没有」无关**（用户
        2026-09-18 裁定）：这个功能就是要在场景还没命中过之前就能预勾选，等真固化了
        再来补配置就晚了。接管与否是运行期结果，归守护的回显说，不归这份清单。
        """
        out: "list[str]" = []
        for profile in self._profiles():
            for iface in (profile.get("ifaces") or []):
                name = str(iface).strip()
                if name and name not in out:
                    out.append(name)
        return out

    def renew_action(self) -> "ActionResult | None":
        """徽标下拉「恢复 DHCP」：给预设里的网卡下发一条**命令**。

        命令做三件事，**一轮内原子完成**（守护侧 ``routeops._run_op``）：

        1. 真的交还给系统（``-setdhcp``，可选连 DNS 一起还原）——这一步就是「恢复 DHCP」；
        2. 等**新租约**落到网卡上（最多十来秒）；
        3. 用新租约按**当前命中场景**重新固化（``net.pin`` 留空的那份就是「DHCP 给什么
           就固化什么」）。

        没有场景要接管这张网卡（或自动化停着）时退化成 ``release``：交还完就停在自动
        获取 —— 「停用 = 保持现状」不该被一个按钮推翻。**决策在这一侧做**（只有插件
        知道当前哪个场景接管了哪张网卡），helper 拿到什么执行什么。

        **为什么改之前点了没用**（2026-09-29 定案）：命令以前与「无匹配时才交还」共用
        顶层 ``handback``，于是被那个**策略前提**挡在门外；而场景判据恰恰是我们自己
        固化出去的地址（自证循环）⇒ 办公场景命中一次就永远为真 ⇒ 命令一直排队，
        用户看到的就是「点了没用」。现在命令走独立通道（顶层 ``ops``），守护那一轮
        **先执行命令**再判场景，且不受停用影响。

        **三个分支都要说话**（`ActionResult`，D32-②；本条只有面板一个入口，面板侧拿不到
        设置页那块槽 ⇒ 三条提示用户一条都看不见）：拒绝按 ``FAILED`` 记 —— 判据是
        「**命令有没有被下发**」，那两条都是「什么都没发生、且原因不在你按下那一刻的
        界面上」，与真出错同一种处境，只有红色才拦得住人。成功那句也返回：执行是
        **守护下一轮**才做的，用户按完看不到当场变化，不确认一句他会以为没点到。
        """
        if not self._daemon_status.get("installed"):
            return self._failed(
                "路由守护未安装：交还不能生效（没有进程会执行它），已忽略。"
            )
        if self._config_problem:
            # 配置非法时意图根本不会落盘（见 sync_routes）⇒ 命令发不出去。
            return self._failed(f"配置非法，命令没下发：{self._config_problem}")
        ifaces = list(self.handback_ifaces())
        if not ifaces:
            return self._note(
                "设置里还没选择要恢复成 DHCP 的网卡；请先到设置页「恢复 DHCP 配置」勾选。"
            )
        dns = self.handback_dns()
        # 停用时只交还、不固化；「当前哪个场景接管了这张网卡」只有一个真源：算一遍意图
        # （与探活服务同一个 provider，不另写一份判据）。
        plan = None if self.is_paused else self._compute_intent()
        sent: list[str] = []
        for iface in ifaces:
            net = _rule_net_for(plan, iface)
            params: dict = {"iface": iface, "op": "renew" if net else "release",
                            "dns": dns}
            if net:
                params["net"] = net
            op = "route.renew" if net else "route.release"
            if self.run_op(op, params, callback=partial(self._on_net_op_done, iface)):
                sent.append(iface)
        if not sent:
            return self._failed("守护未连接，命令没有下发（先确认路由守护已启动）。")
        dns_note = "DNS 一并还原" if dns else "DNS 不动"
        return self._note(
            f"已把 {'、'.join(sent)} 交给守护（{dns_note}），正在执行。"
        )

    def _on_net_op_done(self, iface: str, ok: bool, result, error: str) -> None:
        """``op.run`` 回帧：这条网卡命令到底成没成。

        **为什么不再是 ``_reap_ops``**：旧形态把命令写进期望态顶层的 ``ops`` 数组，
        再靠 ``seq`` 水位线去状态文件的回执表里配对、配上了才把命令撤掉。水位线活不过
        进程重启 —— 重启后认不出自己的命令，那条命令就会**再执行一次**（renew 再执行
        一次 = 又一次 ``setdhcp`` = 又一次断网）。帧级回执把「配对」这件事连带删掉：
        没有 ``ops`` 数组、没有 ``seq``、没有回执表，也就没有账可错。

        回帧时**命令已经执行完**（守护那一轮跑完才回），所以这里只报结论。跑在**主线程**
        （会话的可读通知把它泵到这里），但只写日志：面板那一行由下一轮 tick 的「网卡」
        段呈现 —— 回帧正文已并进守护观测，随后推送过来。
        """
        if not ok:
            log.error("网卡命令 %s 未完成：%s", iface, error or "未知原因")
            return
        log.info("网卡命令已执行：%s", _op_result_text(result))
