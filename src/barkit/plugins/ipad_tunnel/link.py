"""``_LinkMixin`` —— 链路与数据面：给中继 / 探针的取数口，以及「链路这条事实」的监听门与叫守护重跑的出口。

``plugin.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import logging
import time

from barkit.core.log_throttle import log_throttled
from barkit.plugins.ipad_tunnel.bridge import PROBE_DESCEND_PAUSE

from . import announce
from .constants import TUNNEL_SUBJECT, log
from .consts import ANNOUNCE_RETRY_INTERVAL_S


class _LinkMixin:
    """链路与数据面：给中继 / 探针的取数口，以及「链路这条事实」的监听门与叫守护重跑的出口。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    def _read_ports(self) -> tuple[int, int]:
        """中继的端口真源（中继循环每轮读一次，端口热改据此生效）。

        两个默认值（1080 / 9001）的真源是 ``config_schema()`` 里那两条
        ``FieldSpec``（它们又引用 ``tunnel_spec`` 的常量），此处不再各抄一份。
        """
        return (
            int(self.cfg.value("local_port")),
            int(self.cfg.value("device_port")),
        )

    def _read_device_port(self) -> int:
        """探针的设备端口真源（与中继同源，热改后下一轮探针即生效）。"""
        return int(self.cfg.value("device_port"))

    def _carrying(self) -> bool:
        """隧道此刻是否**正在接管流量**（探针据此选档位；见 bridge 模块 docstring）。

        判据取「中继此刻占着 local_port」—— 那是「上游存在」唯一的事实来源，而 helper
        只在探到该端口时才建隧道，所以「端口在听」就等于「这条隧道（正要）在搬运」。
        旧写法读「我们最后写进去的 link_up」（一个快照），错在两处：手动模式下链路判否
        会让探针每分钟去停一次中继（把好端端的隧道勒死），而快照本身也可能与现场不符。
        跨线程只读一个属性（``RelayService.listening``），故不引入锁。
        """
        try:
            return bool(self.automation_active and self._relay.listening)
        except Exception:  # noqa: BLE001
            log.exception("读取中继监听状态失败，按未接管处理")
            return False

    def _data_plane_state(self) -> bool | None:
        """中继自报的阶梯第 ① 级读数：最近一次有结论的窗口搬过字节吗（None = 还没结论）。

        中继未运行时也是 None：那时无从证伪，探针会往下探，而不是默认「健康」。
        """
        try:
            return self._relay.data_plane_state
        except Exception:  # noqa: BLE001
            log.exception("读取中继数据面状态失败，按无结论处理")
            return None

    def _pause_relay_accept(self) -> None:
        """探针要下探「通道 / 握手」两级了：让中继先停收新连接，免得量到我们自己的噪音。

        只挡**新**连接，在途与已建成的隧道照常搬运；带截止时间，探针中途死掉也不会把
        中继停成永久黑洞。中继未运行时是无操作。
        """
        try:
            self._relay.pause_accept(PROBE_DESCEND_PAUSE)
        except Exception:  # noqa: BLE001
            log.exception("让中继停手失败（这次下探的结果可能含我们自己的噪音）")

    def _reset_data_plane(self) -> None:
        """给中继的窗口统计一条新起跑线（探针线程在链路翻转时调）。

        与读侧同理不抛：复位失败只是让第 ① 级仍拿旧结论说话，不该把探针线程带走。
        """
        try:
            self._relay.reset_data_plane()
        except Exception:  # noqa: BLE001
            log.exception("复位中继数据面统计失败（判据起跑线可能仍旧值）")

    def _data_plane_fault(self) -> bool:
        """中继自报：最近一次有结论的窗口「一条连接都没搬动字节」。

        与 ``_data_plane_state`` 的分工：那个是「第 ① 级过没过」的**判据**（链路一翻转
        就复位），这个是「这条隧道搬过字节吗」的**事实**（只有真搬到字节才解除）。面板
        的严重格用它 —— 拆了又重开的循环里它持续为真，用户才看得到刚才确实搬不动。
        """
        try:
            return bool(self._relay.data_plane_fault)
        except Exception:  # noqa: BLE001
            log.exception("读取中继数据面故障标记失败，按正常处理")
            return False

    def _probe_hop(self) -> str | None:
        """探针最近一次的**最浅失败层**（``HOP_*``；None = 全过）。给面板与诊断页用。"""
        return self._probe.last_hop

    def _request_service_resync(self) -> None:
        """请求框架重算长驻服务集合。

        托管权在框架：插件不能起停服务，只能请求「重新评估」。ctx 是全插件共享单例，
        回调无参、语义为「重算所有插件」；声明未变的插件是空操作，可廉价反复调用。
        测试桩未注入该回调时静默跳过（字段默认 None）。
        """
        cb = getattr(self.ctx, "resync_services", None)
        if not callable(cb):
            return
        try:
            cb()
        except Exception:  # noqa: BLE001
            log.exception("请求框架重算长驻服务失败")

    # ---------- iOS 端签名到期通告（2026-10-09，机制见 announce.py） ----------

    def _on_probe_link_change(self, up: bool) -> None:
        """探针把链路翻上来（``on_link_change``，跑在探针线程）：立刻拉一次签名通告。

        只在翻**上来**时拉 —— 那是「iPad 端 App 活着」刚被证实的一刻（准入 ④ 级
        握手刚通过 / 数据面恢复）。翻下去不拉：设备都不在了，拉了也是空手。

        阻塞上限一次 usbmuxd Connect + 读超时（≈5s），且探针线程不是数据面，慢这一下无碍。

        刻意**不 mark_dirty**：那个入口会碰调度器的主线程 QTimer（跨线程不安全），
        而本组件是周期型（30s 一轮 tick），缓存变化最迟下个周期就被读走 ——
        对一个按天走的倒计时，30 秒的显示延迟什么都不是。
        """
        if not up:
            return
        self._refresh_announce()

    def _on_probe_poll(self, link_up: bool) -> None:
        """探针每轮 tick 给一次当前链路结论：**链路在、且还没拿到通告**时低频重试拉一次。

        为什么要它（2026-10-10 现场）：拉取原先只挂在「链路翻上来」那**一次**上，而设备侧
        应答点可能恰好那一刻不可用 —— iPad 端 9002 的 accept 源死掉后没有自愈路径，Mac 侧
        只在启动那一次试过 ⇒ **整个会话停在「未通告」**，设备侧后来好了也没人去问。到期时间
        对同一份构建确实不变，但「这一次没拿到」不等于「永远拿不到」。

        节拍**借探针这一个**（``LinkProbeService.on_poll``，不新起时钟）：稳态下本回调立刻
        返回（一次 bool 读 ＋ 一次属性读）。拿到一份合法通告（ok / absent / unreadable 都算）
        即停，直到下一次链路翻转或进程重启；拉不到则每 ``ANNOUNCE_RETRY_INTERVAL_S`` 再问
        一次 —— 与「运行期每 ``PROBE_DEEP_INTERVAL`` 才许碰一次 USB 通道」同一条节律。

        只在探针线程跑，与 ``_refresh_announce`` 同线程 ⇒ ``_announce_next_try`` 无并发。
        """
        if not link_up:
            return
        if self._announce is not None:
            return                      # 已有合法通告：同一份构建不变，不必再问
        if time.monotonic() < self._announce_next_try:
            return
        self._refresh_announce()

    def _refresh_announce(self) -> None:
        """拉一次通告并**整体替换**缓存；没拿到（旧版 iOS 端 / 设备不在）**不清旧账**。

        到期时间对同一份构建不变：拉不到时旧缓存仍然为真，下一次重试自然会再试
        （``_on_probe_poll``）。读侧（tick / 显示）与本方法跨线程，但只做单属性整体替换
        —— CPython 下原子，不引入锁。

        **出声按故障期、不按次**（同 ``bridge`` 探针的 ``_last_reason``）：本组件要连跑
        数天，每次重试都记一条就是刷屏。拿到即记一条成功 INFO —— 那是「App 到底有没有
        通告」**唯一落盘**的线索（fetch 内部各条失败路径都只是 debug，见 `_log_fail`）。
        """
        # 无论成否都推到下一次：这是「一次尝试」的记账，不是「拿到没有」的记账
        # （后者由 ``_announce`` 是否为 None 表达）。
        self._announce_next_try = time.monotonic() + ANNOUNCE_RETRY_INTERVAL_S
        try:
            fresh = announce.fetch(on_fail=self._note_announce_fail)
        except Exception:  # noqa: BLE001 —— fetch 内部已兜，这里再兜一道：探针线程不许被带走
            log.exception("拉取 iOS 端签名通告失败（本轮放弃，等下一次重试）")
            return
        if fresh is not None:
            self._announce = fresh
            self._announce_trouble = None
            log.info("iOS 端签名通告：%s", announce.settings_text(fresh))

    def _note_announce_fail(self, reason: str) -> None:
        """``fetch`` 报「这一次为什么没拿到」：**同一个原因只出声一次**（跑在探针线程）。

        原因**变了**再出声 —— 新原因就是新信息（设备不在 vs 应答点没在听，处置完全不同，
        见 ``announce.FAIL_DESC``）；同一个原因连着拉不到则静默，否则每 60s 一条、
        一天就是上千条。
        """
        if reason == self._announce_trouble:
            return
        self._announce_trouble = reason
        log.info("拿不到 iOS 端签名通告（%s）—— 链路在时每 %.0f 秒重试一次",
                 announce.FAIL_DESC.get(reason, reason), ANNOUNCE_RETRY_INTERVAL_S)

    def announce_text(self) -> str:
        """设置页那一行的文案（归一结果 → 显示，判定只在 announce.settings_text 一处）。"""
        return announce.settings_text(getattr(self, "_announce", None))

    def attach_announce_label(self, label) -> None:
        """设置页把那行「iOS 端签名」交给组件，此后由 tick 续写。

        **为什么这行不能「构建期读一次」**（2026-10-09 现场）：设置窗是**启动期
        构建一次**的（``shell/app_shell/app_shell.py`` 装配时建 ``SettingsWindow``，
        ``shell/settings/pages.py`` 的 ``_build_ui`` 当场把每一页 ``builder()`` 跑完），
        而探针首翻在启动后 ≈6~12s 才发生（``probe`` 首轮延迟 ＋ 2 票迟滞）⇒ 构建那一刻
        ``_announce`` **必然是 None**。读过一次就定格的写法会让这一行整个会话停在
        「未通告（旧版 iOS 端，或 iPad 未连接）」，而 Mac 端其实早就拉到了时间 ——
        用户看到的正是这句假话（用户上报「没取到时间」）。

        槽位的生死归这里管：页面重建 / 关窗时摘引用，免得 tick 戳到已删的 C++ 对象。
        与 ``capabilities.inline_feedback`` 的 ``attach_feedback`` 同款纪律。
        """
        label.destroyed.connect(lambda *_args, _lb=label: self._forget_announce_label(_lb))
        self._announce_label = label
        self._refresh_announce_label()

    def _forget_announce_label(self, label) -> None:
        """那行所属页面被销毁时摘掉引用（只在引用的正是刚死掉的那个时清空：
        页面重建时新行可能**已经先赋好值**，无条件清空会把新引用一起丢掉）。"""
        if getattr(self, "_announce_label", None) is label:
            self._announce_label = None

    def _refresh_announce_label(self) -> None:
        """把当前归一结果写进那一行；值没变就不动（省一次重绘）。

        由 ``Plugin.tick()``（30s，主线程）驱动，故不引入任何新时钟；设置窗没建 /
        已销毁时是空操作。倒计时按天走，30 秒的延迟无意义。
        """
        label = getattr(self, "_announce_label", None)
        if label is None:
            return
        text = self.announce_text()
        try:
            if label.text() != text:
                label.setText(text)
        except RuntimeError:
            # 页面正在销毁（C++ 对象已删）——这一轮跳过，destroyed 会摘掉引用。
            pass

    # ---------- 链路这条事实：由中继的「监听门」承载 ----------

    def _relay_should_listen(self) -> bool:
        """中继此刻该不该占用 ``local_port``（链路这条事实的**唯一**判据）。

        手动模式（``ignore_link``）恒真 —— 那正是这个开关的语义：不等链路探测就去建。
        自动模式看探针的**新鲜结论**（``concluded``）：没有结论就是「还不知道」，
        此刻不许占端口 —— 宁可让 helper 看到「上游不在」（最坏是隧道晚 6 秒生效），
        也不能让它在 iPad 不在场时把整机流量劫持进一条通往黑洞的隧道。

        取数口一律现问（注入发生在构造之后，链路马上就会变）。
        """
        if self.ignore_link:
            return True
        return bool(self._probe.concluded and self._probe.link_up)

    def _on_relay_listen_change(self, listening: bool) -> None:
        """中继刚占上 / 放开 ``local_port`` ⇒ 叫守护**现在**跑一轮（顺序天然正确）。

        这是链路翻转之后**唯一**去叫 helper 的地方，别在探针翻转时也叫一遍：那时端口
        还没放开（中继下一轮才收敛），叫了只会让 helper 看到上一次的现场、白跑一轮，
        而且它拆不掉 —— 拆隧道的判据（上游端口）此刻还立着。

        由中继线程调，异常只记日志。
        """
        log.info("iPad 链路事实已变更：中继%s 127.0.0.1；叫 helper 跑一轮",
                 "开始监听" if listening else "放开")
        self._resync_now()

    def _resync_now(self) -> None:
        """叫守护**现在**按已声明的隧道意图跑一轮（``resync`` 命令）。

        这就是旧的 ``touch_trigger()``：链路那侧的现场刚变了，而**意图一个字都没变**
        ⇒ 光重发同一份意图是发不出去的（``declare`` 按内容哈希幂等，内容没变就不发帧），
        等下一轮节拍又要多等最长 5 秒。命令说的只有「现在」，要收敛到什么在意图里。

        **可以在任意线程调**：``run_op`` 收进会话锁，回调由装配层送回主线程；
        这里的回调只写日志，更简单。
        """
        self.run_op("resync", {"subject": TUNNEL_SUBJECT},
                    callback=self._on_resync_done)

    @staticmethod
    def _on_resync_done(ok: bool, _result, error: str) -> None:
        """``resync`` 的回执：成功不吭声（它只是「又跑了一轮」），失败留一条日志。"""
        if not ok:
            # 投递失败是**故障**（守护没装 / 会话断了），不是信息 ⇒ warning；
            # 而链路反复翻转会反复投递 ⇒ 同因只报一次。
            log_throttled(log, logging.WARNING,
                          "叫守护重跑一轮没送到：%s", error or "未知原因")
