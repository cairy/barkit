"""``_SubtitleMixin`` —— 状态行副标题：一行说完四台机的分段拼装。

``plugin.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from barkit.plugins.route_guard.checks import dest_was_added


class _SubtitleMixin:
    """状态行副标题：一行说完四台机的分段拼装。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 面板行副标题（行级回调，契约 §2.7 / 决议 D24） ----------

    def _row_subtitle(self) -> str:
        """副标题：**一行说完四台机**（设计 §3.3）。

        次序就是设计 §3.3 的口径——**异常项优先 → 生效场景（凭什么命中的）→ 结果 → 网卡**，
        标准句例如：「网关命中 · 已叠加 3 条」（**不含场景名**：它在徽标上，设计 §3.3 要求
        配置项名只出现一次；第 7b 步把徽标换成场景选择器后才成立）。

        **「守护就绪」已删（2026-09-18 用户裁定）**：装置健康是**默认态**，占一格等于什么
        都没说 —— 与 §4.6 那条判据同源（徽标原先写「自动 / 手动」也是同一个毛病）。故障那
        四种（过期 / 未装 / 配置非法 / 停用）本来就各自**领句并 return**，轮不到常规句式去
        替它们站岗。故这一段现在只在**出事时**出现，正常时那句更短。

        为什么必须由组件算整句、不能拿代表态那台的 ``summary`` 顶（这是设计 §3.3 的
        核心判断，也是本步存在的理由）：代表态回答的是「**有多坏**」（取最坏那台，
        三处显示位同色），文案回答的是「**现在是什么**」（该写哪几台、按什么次序）。
        靠「同一个取值同时决定颜色与文案」在单状态机组件上侥幸成立，多状态机一上就
        散架——真正的限制不是「选不对那台机」，而是「只准用一台机的文案」。
        颜色照旧归代表态（框架算），两件事不互相冒充。

        四个异常各自**领句**（它们正是用户此刻要处理的那件事）：停用 / 配置非法 /
        守护过期 / 守护未装。异常句之后不再拼常规三段——「已停用 · 保持现状，
        路由仍生效（3 条）」本身就是完整一句（§4.3 的四态对照表）。

        超宽交给 ``ElideLabel`` 省略号兜底（**不做词条级裁剪**：阈值不可验证），
        省略掉的那半在悬停时仍是完整一句（``_StatusRowView`` 给副标题挂了 tooltip）。
        """
        if self.is_paused:
            n = self._applied_route_count()
            tail = f"保持现状，路由仍生效（{n} 条）" if n else "保持现状"
            return f"已停用 · {tail}"
        if self._config_problem:
            return f"配置非法，路由未更新：{self._config_problem}"
        health = self._daemon_report()
        if health.state == "outdated":
            # 文案不在这儿重写第二遍：版本号那句由 daemon 机给（同 D14「不重复写」），
            # 这里只补**该做什么**。而「在设置页」这四个字必须写出来 —— 副标题只说
            # 「需重装」时，用户找不到入口：面板整行点开的是场景配置窗，装守护的按钮
            # 在设置窗那一页（2026-09-18 实测，他就是这么问的）。
            return f"{health.summary} · 在设置页重装"
        if health.state == "missing":
            return "守护未安装 · 路由不会自动维护"
        scene_seg = self._scene_segment()
        result_seg = self._result_segment()
        net_seg = self._net_segment()
        if scene_seg is None and result_seg is None and net_seg is None:
            # 守护在跑、但一轮都还没回报过：说清楚在等什么，别拿「无匹配 · 未叠加」充数
            return "等待守护上报状态"
        return " · ".join(s for s in (scene_seg, result_seg, net_seg) if s)


    def _net_segment(self) -> str | None:
        """副标题的「网卡」段；**没什么可说的时候返回 ``None``**（整段省略）。

        只有三种情况值得占那几十个像素：网卡上停着我们写下去的配置（静态 / 固化）、
        写入失败了（含自动回滚）、以及停用时的「保持现状」。``dhcp``（未接管）与
        ``unknown``（还没读到）都是「这一格没有信息」—— 塞进那一句就是噪声。

        位置放在路由结果**之后**（§3.3 的次序：场景 → 结果 → 网卡）有两个理由：
        路由是这套配置的主要目的；而最常见的那句（网卡归系统管着）这一整段不出现
        ⇒ 该在的那几个词一个不多、一个不少。
        """
        report = self._net_report()
        if report.state in ("static", "pinned"):
            return f"网卡{report.summary}" if report.summary else "网卡已按配置生效"
        if report.state == "failed":
            return f"网卡{report.summary}" if report.summary else "网卡配置失败"
        if report.state == "frozen" and self._net_state().get("managed"):
            return "网卡保持现状"
        return None

    def _scene_segment(self) -> str | None:
        """副标题的「**凭什么就是它**」段；**没有证据**时返回 ``None``（整段省略）。

        **场景名不在这里**（第 6 步留的口子，第 7b 步兑现）：徽标已经换成场景选择器、
        上面写的就是生效场景名，副标题再写一遍就是同一个名字出现两次 —— 设计 §3.3
        那句「配置项名只在徽标上出现一次」要到这时才真正成立。
        例：``网关命中 · 已叠加 3 条``。

        ◷ **2026-09-18 用户裁定：自动命中时要说依据**。「自动命中」四个字回答的是
        「怎么来的」（自动而非手动），没回答用户看完仍要追问的那句「命中了什么」；
        而场景名就在旁边徽标上、不能复述。依据（网段 / 网关）**别处都没有**，
        只在配置窗体里查得到 —— 它正是「为什么生效的是它」的答案，故拿它替换原词。

        ``conflict`` 例外，仍把名字列出来：那是**多**个名字的清单（不是「当前那一个」），
        也是「为什么生效的不是我以为的那个」的唯一线索，省掉就没法查了。
        """
        report = self._profile_report()
        if report.state == "unknown":
            return None
        if report.state == "conflict":
            return f"多场景同时命中（{report.summary}）"
        if report.state == "active":
            if self.pinned_scene():
                return "手动指定"
            return self._match_basis(report.summary)
        return "无匹配"

    def _match_basis(self, scene: str) -> str:
        """自动命中时那句「凭什么」：按该场景**配置里真写着的**命中条件说（§2.1）。

        三档与配置一一对应，不编也可能说得满：
        ``match.subnet`` / ``match.gateway`` **都写 = AND**（两个都要满足），只写一个就
        只说那一个；两个都没写 ⇒ 该场景「网卡在线即命中」（同 `_scene_hint` 的兜底口径）。

        **查不到这条场景时退回「自动命中」**：回显里可能留着一条配置已经删掉的旧规则
        （正是回收站期的那一轮），那时任何依据都是编的 —— 与其猜一个网段出来，不如老实
        说「它是自动挑的」。同 `_scene_hint`「编出来的那半句话用户核对不了」。
        """
        profile = self._profile_by_name(scene)
        if not profile:
            return "自动命中"
        match = profile.get("match") or {}
        has_subnet = bool(str(match.get("subnet") or "").strip())
        has_gateway = bool(str(match.get("gateway") or "").strip())
        if has_subnet and has_gateway:
            return "网段与网关命中"
        if has_subnet:
            return "网段命中"
        if has_gateway:
            return "网关命中"
        return "网卡在线命中"


    def _result_segment(self) -> str | None:
        """副标题的「结果」段：已叠加 N 条 / 部分叠加 K/N 条 / 本次已撤回路由（N 条）…

        「本次撤回」与「按配置不叠加」都归 ``_suppression_note``——它们说的是同一件事的
        两个来源，必须在同一处分辨（§4.8：开关上两者长得一样，只有文案能把它们分开）。
        """
        note = self._suppression_note()
        if note:
            return note
        report = self._routes_report()
        if report.state == "unknown":
            return None
        return {
            "applied": report.summary,                  # 「已叠加 N 条」由报告带文案
            "partial": report.summary,                  # 「部分叠加（K/N 条）」
            "failed": "叠加失败",
            "pending": "待应用",
            "none": "未叠加",
            "frozen": "已冻结",
            # v8：留空的下一跳取不到网关 ⇒ 那一份路由一个都没动。这一格的文案由声明给
            # （``ROUTES_STATES``），不在这里重写第二遍 —— 同 daemon 那两台机的口径。
            "no_gateway": report.summary,
        }.get(report.state)

    def _route_count(self, scene: str) -> int:
        """该场景**会叠**的路由条数（给文案用）：配置优先，helper 回显兜底。

        只数启用中的那些（v7 起逐条可关）：「本次已撤回路由（N 条）」说的是撤回几条，
        把停用条目算进去就是把话说满。回显那一支同理 —— 只数真加上去的（``dest_was_added``
        ：v7 起一条规则可以有的加、有的撤，拿 ``applied`` 数会多算）。
        """
        items = (self._profile_by_name(scene).get("routes") or {}).get("items") or []
        if items:
            return sum(1 for it in items
                       if isinstance(it, dict) and it.get("enabled", True) is not False)
        entry = next(
            (r for r in ((self._state or {}).get("rules") or [])
             if isinstance(r, dict) and r.get("name") == scene),
            {},
        )
        return sum(1 for d in (entry.get("destinations") or [])
                   if isinstance(d, dict) and dest_was_added(d))

    def _applied_route_count(self) -> int:
        """回显里**确实在路由表里**的条数（停用句里那个数字，§3.2）。

        三个条件缺一不可：规则本轮 ``applied``（要不要叠）、逐目标 ``action`` 是加不是撤
        （v7 起一条规则可以有的加、有的撤）、且 ``ok`` —— 那句话说的是「你关掉自动化之后
        仍然生效着的东西」，把加失败的、被撤下来的都算进去就是把话说满。
        """
        return sum(
            1
            for r in (self._reported_rules() or [])
            if r.get("applied")
            for d in (r.get("destinations") or [])
            if isinstance(d, dict) and d.get("ok") and dest_was_added(d)
        )

    def _suppression_note(self) -> str:
        """为什么不叠加的说明：覆盖层（本次）与配置层（以后）分开说。

        两者在开关上长得一样（都显示关），**只有文案能把它们分开**：一个是「换了场景
        就回来」，另一个要回配置窗体改。

        句子里**不带场景名**：它在副标题的场景段已经写过一次（设计 §3.3「配置项名只在
        徽标上出现一次」的同一精神）——两处都写就成了「office · 本次已撤回 office 的路由」。
        """
        scene = self._current_scene()
        if not scene:
            return ""
        overrides = self.cfg.value("rule_overrides")
        entry = overrides.get(scene)
        if isinstance(entry, dict) and entry.get("routes_suppressed"):
            n = self._route_count(scene)
            return f"本次已撤回路由（{n} 条）" if n else "本次已撤回路由"
        routes_block = self._profile_by_name(scene).get("routes") or {}
        if routes_block.get("enabled", True) is False:
            return "按配置不叠加路由"
        return ""
