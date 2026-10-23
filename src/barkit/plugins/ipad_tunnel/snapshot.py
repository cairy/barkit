"""``_SnapshotMixin`` —— 状态快照：观测的降级结构（模块级）＋ 取值闭集 → 一格结论。

``plugin.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import dataclasses
import time

from barkit.contribution.specs import StateReport
from barkit.plugins.ipad_tunnel.bridge import (
    HOP_CHANNEL,
    HOP_DATA,
    HOP_DEVICE,
    HOP_SOCKS,
)

from . import announce
from .constants import DAEMON_REPORT_MAX_AGE_S, TUNNEL_SUBJECT


def _observed_state(plugin) -> dict:
    """守护推来的隧道观测；**还没收到过**返回降级结构（面板显示，不崩溃）。

    从前这里是 ``_read_state_file()``：读 ``tunnel_state.json``，按异常类型分化三类文案
    （``FileNotFoundError`` / ``JSONDecodeError`` / 其余 ``OSError``）。会话通道落地后
    只剩**一种**降级 —— 「还没事实」：文件在不在、写了一半、读不动这三问都没有对象了。
    少掉两句文案不是信息损失：那两句描述的是**读文件失败**，而今天没有文件。
    """
    state = plugin.observed(TUNNEL_SUBJECT, None)
    if isinstance(state, dict) and state:
        return state
    return {"ok": False, "error": "守护尚未回报（未连接或未运行）"}


class _SnapshotMixin:
    """状态快照：观测的降级结构（模块级）＋ 取值闭集 → 一格结论。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 状态快照（芯片 / 徽标 / 状态行共用单一真相）----------

    def _daemon_report_age(self) -> float | None:
        """守护推来的那份结论里 ``checked_at`` 距今多少秒；没有就给 None（不猜）。

        ``checked_at`` 是守护**得出这份结论那一刻**（它每轮无条件更新这个字段）。所以它
        过期就等于「守护没在回报」。**不能改用别的更浅的东西顶替**（比如「连接还在」）：
        连接在只说明对端活着，不代表它还在跑那一轮 —— 判据必须落在守护自己写下的那个数上。

        未来值 / 时钟回拨一律归 0（不判过期）：那两种情形下我们没有依据说它旧。
        """
        checked = self._state.get("checked_at")
        if not isinstance(checked, (int, float)) or isinstance(checked, bool):
            return None
        return max(0.0, time.time() - float(checked))

    def _snapshot(self) -> StateReport:
        """把当前隧道状态映射为一份报告（对外入口）。

        取值闭集见模块顶的 ``STATES``：级别归声明，``_snapshot_base`` 只回答「现在是
        哪一格」。summary 是**唯一文案**（决议 D18）：面板行副标题与两处悬停提示共用
        这一句。签名到期提示在此统一挂尾（``announce.panel_suffix``）—— 只在跨过
        阈值时说话，未通告 / 未到阈值一字不加；**不新增状态格**，避免为一句话把取值
        闭集翻一倍。判定只在 announce 那一处，这里只拼接。
        """
        report = self._snapshot_base()
        suffix = announce.panel_suffix(getattr(self, "_announce", None))
        if suffix:
            # StateReport 是**冻结** dataclass：不能改字段，只能整份重建。
            report = dataclasses.replace(report, summary=(report.summary or "") + suffix)
        return report

    def _snapshot_base(self) -> StateReport:
        """取值闭集 → 一格结论（正文见各返回点；``_snapshot`` 在此之上统一挂尾）。"""
        st = self._state
        installed = self._daemon_status.get("installed")
        if not installed:
            return StateReport("no_daemon", "守护未装", updated_at=time.time())
        if st.get("error"):
            return StateReport("error", st.get("error") or "隧道异常", updated_at=time.time())
        hop = self._probe.last_hop if self.automation_active else None
        port = self._probe.device_port
        # ── 阶梯结论：探针逐级回退（数据面 → 设备 → 通道 → 握手）定出的**最浅失败层** ──
        # 这一整块**必须排在 ``active`` 之前**：故障期 helper 的回报恰恰就是 active（它
        # 只验上游那个端口在听 —— 而那是中继自己），排到它后面就永远显示「隧道生效中」，
        # 用户此刻却上不了网。旧版正是因为无从定位，只能在这三句里二选一，两句都不对。
        if hop == HOP_CHANNEL:
            return StateReport(
                "ipad_port_refused",
                f"iPad 端口连不上 · 端口 {port} 上没人在听"
                f"（iPad 那头的 SOCKS 引擎没跑起来）",
                updated_at=time.time(),
            )
        if hop == HOP_SOCKS:
            return StateReport(
                "engine_silent",
                f"iPad 未应答 · 端口 {port} 能连上但 SOCKS5 不回应",
                updated_at=time.time(),
            )
        # ⑤ 这份结论是不是**此刻**的：守护写下的 ``checked_at`` 过期 ⇒ 它以下的一切都不许
        # 照念（``active`` 会画成绿字，而隧道可能早断了）。排在探针那两格**之后**：那两格
        # 是探针当场实测的活数据，跟守护在不在没关系，仍该说给用户听。
        age = self._daemon_report_age()
        if age is not None and age > DAEMON_REPORT_MAX_AGE_S:
            return StateReport(
                "daemon_stale",
                f"守护 {int(age)} 秒未回报 · 隧道状态已过期（面板显示的不是此刻的事实）",
                updated_at=time.time(),
            )
        # 「正在建」：helper 报 pending（上游在听、hev 已起、utun 未建出），而本机这一侧的
        # 链路判据也确认了就绪 ⇒ 正在建，**不是**「在等 iPad」。这一格排在 data_plane_fault
        # 之前：pending 期间 hev 还没起或刚起，谈不上「隧道已连 · 数据卡在本机」。
        if self.automation_active and st.get("pending") and self._relay_should_listen():
            return StateReport(
                "building",
                "隧道建立中 · 链路已就绪，正在建出隧道接口（通常几秒）",
                updated_at=time.time(),
            )
        # ① 数据面搬不动字节（判据来自中继的窗口统计，见 bridge.Bridge.data_plane_fault）。
        # 「设备不在」（②）时让位：那时该说的是「等待 iPad」，本格文案里的「本机」也不成
        # 立 —— 拔线之后这个事实标记仍为真（它要真的搬过字节才解除）。
        # 开关关着时也一律让位（``automation_active``）：那是「没开隧道」，谈不上隧道故障，
        # 而 helper 的状态文件可能还是关掉前的**旧**快照（active=true 挂在里面）。
        if self.automation_active and self._data_plane_fault() and hop != HOP_DEVICE:
            # 只在「②③④ 刚实测全过」时才敢说卡在本机 —— 那是探针拿现问的设备号开通过
            # 通道、也拿到过 SOCKS5 应答；否则只说「搬不动」，不指认段位。
            evidence = "（iPad 侧实测正常：设备在、端口通、SOCKS 有应答）" if hop == HOP_DATA else ""
            return StateReport(
                "data_plane_dead",
                f"隧道已连 · 数据卡在本机{evidence}",
                updated_at=time.time(),
            )
        if st.get("active"):
            if st.get("service_unknown"):
                # 读不出服务名 ⇒ helper 没动系统 DNS / IPv6（fail-closed）。这一格**排在
                # dns_degraded 之前**：此刻两个原因都在场时，该说给用户听的是「一次都没碰」
                # （去查服务名为什么读不出），而不是「碰了没被采纳」（去查网络）。
                return StateReport(
                    "service_unknown",
                    "隧道已生效 · 未改动系统 DNS/IPv6（读不出当前网络服务名）",
                    updated_at=time.time(),
                )
            if st.get("dns_unavailable"):
                # 数据面正常、DNS 没接管：不能报 err（面板会显示成整条隧道异常，
                # 而实际上只是域名解析走不了隧道），也不能静默（用户会以为是网坏了）
                return StateReport(
                    "dns_degraded",
                    "隧道已生效 · DNS 未接管（网络服务离线或未采纳 mapdns）",
                    updated_at=time.time(),
                )
            utun = st.get("utun")
            ipad = " · iPad 已连" if st.get("ipad_reachable") else ""
            return StateReport(
                "active",
                f"隧道生效中{ipad}{(' · ' + utun) if utun else ''}",
                updated_at=time.time(),
            )
        if self.automation_active and self.ignore_link:
            return StateReport(
                "manual_waiting",
                "手动等待上游 · 上游未就绪（iPad 未连接或其 SOCKS 端口未监听）",
                updated_at=time.time(),
            )
        # 「设备不在」（阶梯 ②）——**唯一**该说「等待 iPad」的情形（旧版把「连上了却不
        # 干活」也塞进这一句，两件事该做的动作完全不同：一个等或者查线，一个得上 iPad）。
        # 探针现在每轮都现问一次 usbmuxd，所以 ``hop == HOP_DEVICE`` 是实测出来的。
        if self.automation_active and not st.get("ipad_reachable"):
            return StateReport(
                "waiting_ipad",
                ("等待 iPad · USB 上没找到设备（检查连线、解锁与「信任此电脑」）"
                 if hop == HOP_DEVICE else "等待 iPad · 链路就绪后自动建立隧道"),
                updated_at=time.time(),
            )
        if st.get("ipad_reachable"):
            return StateReport(
                "ipad_ready", "iPad 已连 · 隧道未启用", updated_at=time.time(),
            )
        return StateReport(
            "off", "隧道未启用（开关为关）或 iPad 未连接", updated_at=time.time(),
        )
