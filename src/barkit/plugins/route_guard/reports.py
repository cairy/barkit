"""``_ReportsMixin`` —— 四台状态机的快照与手动广播（含观测的降级结构）。

``plugin.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import time

from barkit.contribution.specs import StateReport
from barkit.plugins.route_guard import rules
from barkit.plugins.route_guard.checks import (
    GATEWAY_SRC_IFACE,
    GATEWAY_SRC_NONE,
    dest_was_added,
    route_gateway_src,
)
from barkit.plugins.route_guard.rules import RouteConfigError


def _observed_state(plugin) -> dict:
    """守护报来的路由观测；**还没收到过**返回降级结构（面板显示，不崩溃）。

    从前这里是 ``_read_state_file()``：读 ``state.json``，按异常类型分化三类文案
    （``FileNotFoundError`` / ``JSONDecodeError`` / 其余 ``OSError``）。会话通道落地后
    只剩**一种**降级 —— 「还没事实」：文件在不在、写了一半、mtime 新不新这三问都没有
    对象了。少了两句文案不是信息损失：那两句描述的是**读文件失败**，而今天没有文件。
    """
    state = plugin.observed("route", None)
    if isinstance(state, dict) and state:
        return state
    return {"ok": False, "error": "守护尚未回报（未连接或未运行）"}


class _ReportsMixin:
    """四台状态机的快照与手动广播（含观测的降级结构）。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 三台状态机（设计 §3.1 / §3.2） ----------

    def _evaluate_config_problem(self) -> str:
        """配置段是否非法；非法时返回那句报错（旧格式 / 未知键 / 值不合法都算）。

        为什么组件要自己判一次：``rules.build_routes`` 是**求值路径**的报错点，而第 4 步
        起「旧 ``rules[]`` 不做向后兼容」——写错的那份配置今天**不会改写用户 YAML**，
        只会在求值时报错。这个错总得有个看得见的地方：它就是 ``routes`` 机的 ``failed``
        （ERR → 托盘队列 + 面板行变红），不再只是日志里的一行。
        """
        try:
            rules.build_routes(self.plugin_config())
        except RouteConfigError as e:
            return str(e)
        except Exception as e:  # noqa: BLE001 —— 配置结构离谱到校验器抛别的错，也算非法
            return f"配置校验异常：{e}"
        return ""

    def _reported_rules(self) -> list[dict] | None:
        """helper 推送的规则清单；**还没收到过**返回 ``None``。

        ``None`` 与空表是两件事：前者「还不知道」，后者「收到了，里面没有规则」。
        helper 每轮必定报出 ``rules`` 键（``routeops.run_cycle``），故该键缺席即
        「守护尚未回报」（``_observed_state`` 的降级结构）。
        """
        st = self._state or {}
        raw = st.get("rules")
        if not isinstance(raw, list):
            return None
        return [r for r in raw if isinstance(r, dict)]

    def _scene_rules(self) -> list[dict] | None:
        """**命中判定**用的规则清单：回显优先，无回显时回落 app 侧刚算出的意图。

        为什么「场景」可以吃两份证据而「路由结果」不能：命中是 app 侧算出来的
        （探活服务每 5s 重算），回显只是它的回声；而「有没有真叠上」只有 helper 知道
        （§4.7「意图可设、结果只读」）。回显优先是为了与 ``_current_scene`` 同源 ——
        面板开关作用的场景与副标题说的场景不能是两个。
        """
        reported = self._reported_rules()
        if reported is not None:
            return reported
        last = getattr(self._probe_service, "last", None)
        if isinstance(last, dict) and isinstance(last.get("rules"), list):
            return [r for r in last["rules"] if isinstance(r, dict)]
        return None

    def _matched_scenes(self) -> list[str]:
        """此刻命中的场景名（**可能多于一个** —— 网段重叠就是 ``profile.conflict``）。

        判据取 ``trigger_matched``（触发命中）而不是 ``matched``（命中 **且** 启用）：
        用户「本次撤掉路由」撤的是路由，不是「这个场景命中了」这件事。
        """
        rules = self._scene_rules() or []
        return [str(r["name"]) for r in rules if r.get("trigger_matched") and r.get("name")]

    def _daemon_report(self) -> StateReport:
        """守护机关的动作：停用降级 → 过期 → 未装 → 就绪（设计 §3.1 / §3.2）。"""
        now = time.time()
        if self.is_paused:
            return StateReport("not_needed", updated_at=now)
        st = self._daemon_status or {}
        if st.get("outdated"):
            installed = st.get("version") or "?"
            expected = st.get("expected") or "?"
            return StateReport(
                "outdated", f"守护过期 v{installed} → v{expected}", updated_at=now,
            )
        if not st.get("installed"):
            return StateReport("missing", updated_at=now)
        return StateReport("ready", updated_at=now)

    def _routes_report(self) -> StateReport:
        """路由结果：只认 helper **回显**（意图可设、结果只读，§4.7）。

        怎么读回显（``routeops.run_cycle`` 写出的形状）：每条规则带 ``applied``
        （这一轮该加还是该撤）与逐目标的 ``ok``。**``ok`` 的含义是「这一轮那次动作成功了」**，
        不是「此刻在路由表里」—— ``ensure_removed`` 在「本来就没有」时也返回成功
        （它的注释写着「其他路径绝不误删」）。所以：

        - 有目标 ``ok=False`` ⇒ 真失败：加不上或撤不掉；
        - ``ok=True`` 且该规则 ``applied`` ⇒ 真的在路由表里，算「已叠加」；
        - ``ok=True`` 且该规则不 applied ⇒ 「成功确认它不在」，中性，不计数。

        v8 多一条 ``gateway_src``：「这个下一跳从哪来」。它是 ``none`` 时那条路由
        **一个都没动**（留空要取的那块网卡的网关还没有）—— 那一格既不是「已叠加」
        也不是「失败」，单独报 ``no_gateway``（WARN）。**它排在真失败之后、别的计数之前**：
        真失败（ERR）比它严重，而它比「叠了几条」要紧（用户看到的该是「有一条没动」，
        不是「一切正常」）。取到值的（``iface``）则在摘要里点名 —— 那个值用户没写过，
        不说出来他无从核对。

        **但「配置里写了留空」本身不是报警的理由**（2026-10-09 修）：那个配了留空的场景
        此刻可能**根本没命中**（人在家、配的是办公室）。用户的话是「都没有命中场景，
        这句话根本就不对」—— 这句文案说的是「该动而没动」，而没命中的场景本轮本来
        就不该动。故判据是「本轮 ``applied``（该它上场）**且**来源为 ``none``」；没命中
        的那些留空规则连计数（``n``）都不进。放宽的代价是实打实的三样：面板挂一句不
        成立的话、这块警告占住托盘角标、以及把另一条**真叠上了**的摘要（含它取到的
        那个网关）挤掉 —— 用户想核对的东西反而看不见。

        三条判据按「先看有没有坏消息，再看有没有好消息」排。
        """
        now = time.time()
        if self.is_paused:
            # 停用 = 不碰 = 保持现状（§3.2）：报 ``none``（未叠加）就是粉饰，
            # 已叠加的路由此刻还在系统路由表里。
            return StateReport("frozen", updated_at=now)
        if self._config_problem:
            return StateReport("failed", self._config_problem, updated_at=now)
        rules = self._reported_rules()
        if rules is None:
            return StateReport("unknown", updated_at=now)
        # helper 拒绝了期望态（旧的/手工塞进去的非法意图）：它保持上轮路由不动并写了错。
        error = str((self._state or {}).get("error") or "").strip()
        if error:
            return StateReport("failed", error, updated_at=now)

        bad = added = 0
        for rule in rules:
            applied = bool(rule.get("applied"))
            for dest in rule.get("destinations") or []:
                if not isinstance(dest, dict):
                    continue
                if not dest.get("ok"):
                    bad += 1
                elif applied and dest_was_added(dest):
                    # **两个条件缺一不可**：``applied`` 回答「这条规则本轮要不要叠」，
                    # 逐目标的 ``action`` 回答「这一条目标本轮是加还是撤」—— v7 起一条
                    # 规则可以有的加、有的撤，只看 ``applied`` 会把撤下来的也数成已叠加。
                    added += 1
        if bad:
            if added:
                return StateReport("partial", f"部分叠加（{added}/{added + bad} 条）", updated_at=now)
            return StateReport("failed", updated_at=now)
        # 「本轮该它上场、却留空取不到网关」的那几条（判据的三段各有理由，见 docstring）：
        # ``applied`` 收掉没命中的场景（它们本轮本就不该动，报警告是一句假话），
        # ``gateway_src`` 收掉写死了网关、或从网卡上取到了的，``destinations`` 收掉
        # 「一个目标都没有」的场景（没目标就没有「几条没动」可数）。
        stuck = [r for r in rules
                 if r.get("applied")
                 and route_gateway_src(r) == GATEWAY_SRC_NONE
                 and (r.get("destinations") or [])]
        if stuck:
            # 留空的下一跳取不到网关 ⇒ 那一份路由一个都没动（一格的 WARN，不是 ERR：
            # 谁都没做错，租约到了下一轮自己就走起来）。条数是这一句话能给出的全部信息
            # —— 场景名在徽标上、不在这儿复述（设计 §3.3）。**判据与 ``n`` 同一份名单**：
            # 这两处一旦各写一遍自己的筛选，条数就会把没命中的那些也算进来（说满）。
            n = sum(len(r.get("destinations") or []) for r in stuck)
            return StateReport("no_gateway", f"未取到网关，{n} 条路由未动", updated_at=now)
        if added:
            # 那个网关是**我们自己从网卡上取的**（用户没写过）⇒ 摆在摘要里给他核对。
            taken = next((str(r.get("gateway") or "") for r in rules
                          if r.get("applied")
                          and route_gateway_src(r) == GATEWAY_SRC_IFACE), "")
            return StateReport("applied", f"已叠加 {added} 条（下一跳 {taken}）" if taken
                               else f"已叠加 {added} 条", updated_at=now)
        if any(r.get("applied") and (r.get("destinations") or []) for r in rules):
            # 意图是「叠」、也列了目标，却一个都没叠上：这种回显只可能来自旧版守护，
            # 不替它编一个条数出来。
            return StateReport("applied", updated_at=now)
        if not any(r.get("destinations") for r in rules):
            # **一条路由目标都没有**：第 8 步起这是合法的 —— 一个场景可以「只切 IP、
            # 不加路由」（§4.7 一）。路由这一半没什么可说的，报「未叠加」才是实话
            # （网卡那一半由 ``net`` 机自己说）。
            return StateReport("none", updated_at=now)
        if any(r.get("enabled", True) and r.get("trigger_matched") for r in rules):
            # 期望里该叠，回显里一个都没叠上 ⇒ helper 还没消费到这一份（≤ 一轮）
            return StateReport("pending", updated_at=now)
        return StateReport("none", updated_at=now)

    def _profile_report(self) -> StateReport:
        """场景命中（设计 §3.1）：闭集三值，场景名走 ``summary``。

        停用时降到 ``none``（§3.2 的表）：压灰的一行里「命中了哪个场景」不再是
        用户此刻能拿它怎么办的信息（那张表要求四台机全部 IDLE）。

        手动指定时直接报 ``active``（名字 = 被指定的场景）：这一台机回答的是
        「**现在用的是哪一套**」，而锁着的时候答案与网段、网关都无关。此时不去看不看
        ``_matched_scenes`` 是有意的 —— 拿「匹配到了谁」冒充「在用哪一套」，
        在锁定的那一刻就错了（那正是 §2.6 要面板给选场景入口的原因）。
        """
        now = time.time()
        if self.is_paused:
            return StateReport("none", updated_at=now)
        pinned = self.pinned_scene()
        if pinned:
            return StateReport("active", pinned, updated_at=now)
        rules = self._scene_rules()
        if rules is None:
            # 两份证据都没有：没有场景配置就是「无匹配」，有场景却读不到才是「不知道」。
            return StateReport("none" if not self._profiles() else "unknown", updated_at=now)
        matched = self._matched_scenes()
        if len(matched) > 1:
            return StateReport("conflict", "、".join(matched), updated_at=now)
        if matched:
            return StateReport("active", matched[0], updated_at=now)
        return StateReport("none", updated_at=now)

    def _net_state(self) -> dict:
        """helper 回显里的 ``net`` 段；读不到（缺键 / 结构不对）返回空 dict。

        空 dict ＝ 「**尚不知道**」，不是「没接管」—— 老版本 helper、被整份拒绝的那一轮、
        守护没装，都会走到这里。两者混起来，面板会在启动瞬间说一句「跟随系统，未接管」，
        而那时我们其实什么都没读到。
        """
        seg = (self._state or {}).get("net")
        return seg if isinstance(seg, dict) else {}

    def _net_report(self) -> StateReport:
        """网卡配置态（设计 §3.1 的第四台机）：判据**全部**来自 helper 回显。

        回显的形状见 ``routeops._converge_iface``：``{iface, service, mode, managed,
        wrote, action, error}``（另有 v8 的 ``iface_gateway``，只在真读到网关那一轮有，
        这四格的判据一个都不读它）。映射规则：

        - 停用 ⇒ ``frozen``（§3.2：不碰 = 保持现状。网卡**可能**仍停在我们固化过的
          地址上，报 ``dhcp`` 就是粉饰）；
        - 没有 ``net`` 段 ⇒ ``unknown``；
        - ``error`` 非空或 ``action == "failed"`` ⇒ ``failed``（带 error 原文）；
        - ``managed`` 且 ``mode == "static"`` ⇒ ``static``（摘要带地址）；
        - ``managed`` 且 ``mode == "dhcp+pin"`` ⇒ ``pinned``（同上）；
        - ``action == "rolled_back"`` ⇒ ``failed``：自动回滚是**一次失败的尝试**，
          网卡现在回到进门前那份（结果本身是健康的），但用户得知道「刚才那一笔没成」。
          它是 transient 的 —— 下一轮要么写成功、要么还在闸里，读数跟着走。
        - 其余 ⇒ ``dhcp``（我们不接管 = 网卡归系统）。
        """
        now = time.time()
        if self.is_paused:
            return StateReport("frozen", updated_at=now)
        seg = self._net_state()
        if not seg:
            return StateReport("unknown", updated_at=now)
        error = str(seg.get("error") or "").strip()
        action = str(seg.get("action") or "")
        if error or action == "failed":
            return StateReport("failed", error or "网卡配置失败", updated_at=now)
        if action == "rolled_back":
            return StateReport("failed", "写入后失联，已自动退回", updated_at=now)
        if seg.get("managed"):
            ip = str((seg.get("wrote") or {}).get("ip") or "")
            if seg.get("mode") == "static":
                return StateReport("static", f"静态 {ip}" if ip else None, updated_at=now)
            if seg.get("mode") == "dhcp+pin":
                return StateReport("pinned", f"已固化 {ip}" if ip else None, updated_at=now)
            return StateReport("static", updated_at=now)
        return StateReport("dhcp", updated_at=now)

    def _all_reports(self) -> dict[str, StateReport]:
        """四台机的当前快照（tick 返回值与手动即时广播共用同一份拼装）。"""
        return {
            "daemon": self._daemon_report(),
            "net": self._net_report(),
            "routes": self._routes_report(),
            "profile": self._profile_report(),
        }

    def _publish_all(self) -> None:
        """手动操作后即时广播**三台机**（不等下一拍轮询）。

        走基类 ``publish_snapshot(snap, key=...)``——B 契约下插件唯一的即时广播入口，
        只可能发到本插件自己的 ``status.<key>`` topic。
        """
        for key, snap in self._all_reports().items():
            self.publish_snapshot(snap, key=key)
