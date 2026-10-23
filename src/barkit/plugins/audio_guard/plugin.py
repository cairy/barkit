"""audio_guard：有线耳机（3.5mm）状态看护。

**它解决什么**：插着 3.5mm 耳机的 Mac，在开机 / 睡眠恢复之后偶发两类毛病 ——

1. **耳机没声音**（声音跑到内置扬声器去了，或者输出被静音）；
2. **只听出耳机、没听出麦克风**（插孔识别没跑完，系统里根本没有「外置麦克风」）。

这两个都是「不常发生、发生时当场就能感觉出来、但事后想复现很难」的那类问题，正好适合
交给常驻组件盯。

**本期范围：只检测、只通知，不动手改**（用户 2026-09-22 定的口径）。原因是判据本身要先
被验证 —— 通知里会写清楚「我看到了什么」，用户据此判断检测对不对；等判据被真机确认过，
再逐条把修复动作接上。**已经有确定解法的几条**（默认输入 / 输出漂移、输出静音）做成了
通知上的按钮，点一下才动手，便于用户逐条验证。

**完全不用轮询**（2026-09-22 改）：检测由四个时刻点火 —— 进程启动、系统唤醒
（``system.power.wake``，见 ``core/power.py``）、**音频设备变化**
（``system.audio.device_changed``，见 ``core/audio.py``：3.5mm 插拔、默认输入输出漂移）、
以及**面板被打开**（``app.panel_opened``，见 ``core/events``，2026-09-24 补）。
前两个正是故障的高发时刻，第三个补上「运行中拔插 / 手动改默认设备」这类变化；
第四个不判故障，只管**界面那一行说的是不是此刻的事实** —— 本组件没有节拍，面板行画的是
最后一轮 tick 的结论，不订它就会在屏幕一直亮着时停在旧结论上（它走普通一拍那条路径，
只重读现状、不动手）。框架的 ``tick()`` 只是**执行体**（探针要跑在 worker 线程），
不是检测节拍。

故本组件是 ``RUN_ON_DEMAND``：**没有节拍**，设置页里也不会出现「运行间隔」那一卡
（框架只给周期型组件画它）。要让这件事成立，框架那侧必须满足两个前提，都已落地：
①按需型组件也能被 ``mark_dirty`` 点火（``Scheduler.add_plugin``）；②有一条**音频设备
变化**的系统事件源（``core/audio``）。少任何一条，这个组件都会静默退化成「插上耳机
没人发现」。

**判据从哪来**（都是只读的，见 ``audio_state``）：音频编解码器自报的 ``HPDetect`` /
``MicDetect`` / ``DetectConfidence``，加上 CoreAudio 的设备清单与默认设备。前者是判断
「插着吗 / 认出麦克风了吗」的唯一权威 —— 只看设备列表会把「没插耳机」和「插了但没认全」
混成同一件事，而这两者的处置完全不同（前者什么都不用做，后者要拔插）。
"""

from __future__ import annotations

import logging
import time

from barkit.capabilities.notify import (
    NotificationAction,
    NotificationRequest,
    NotifyCapability,
)
from barkit.contribution.specs import (
    ComponentSpec,
    IconName,
    SettingsPageSpec,
    StateLevel,
    StateOption,
    StateReport,
    StateSpec,
    StatusRowSpec,
)
from barkit.core.audio import AUDIO_DEVICE_TOPIC
from barkit.core.events import PANEL_OPENED_TOPIC
from barkit.core.plugin import FAIL_LOG, RUN_ON_DEMAND
from barkit.core.power import POWER_WAKE_TOPIC
from barkit.core.resources import PluginResources
from barkit.core.schema import FieldSpec, FieldType, FormSchema
from barkit.plugins.audio_guard import settings as settings_ui

from . import audio_state

log = logging.getLogger(__name__)

#: 取值闭集（决议 D13）：取值名 / 级别 / 默认文案只写一次，报告只报取值名。
#: **顺序即判断顺序，也是代表态的平局序**（D1）—— 所以把「没事」和「有事」按
#: 「先判可用性、再判硬件、最后判设置」排下来。
STATES: tuple[StateOption, ...] = (
    StateOption("unknown", StateLevel.IDLE, text="尚未体检"),
    StateOption("probe_failed", StateLevel.IDLE, text="本机读不到音频状态"),
    StateOption("no_headset", StateLevel.IDLE, text="未接有线耳机"),
    StateOption("detecting", StateLevel.IDLE, text="插孔识别进行中"),
    StateOption("mic_missing", StateLevel.ERR, text="耳机已插入，但未识别到耳机麦克风"),
    StateOption("output_drifted", StateLevel.WARN, text="耳机插着但声音没走耳机"),
    StateOption("muted", StateLevel.WARN, text="输出已静音"),
    StateOption("input_drifted", StateLevel.WARN,
                text="耳机麦克风在，但默认输入仍是内置麦克风"),
    StateOption("ok", StateLevel.OK, text="耳机与麦克风均正常"),
)

#: 取值文案单一真源（决议 D18）：运行时 summary 留空即回落到这里。
_STATE_TEXT = {opt.id: opt.text for opt in STATES}

#: 需要弹通知的取值 → (标题, 正文, 按钮)。**恢复（ok / no_headset）不进这张表**：
#: 通知是「出事说一声」，好转了再弹一条同样是打扰。
_ALERTS: dict[str, tuple[str, str, tuple[NotificationAction, ...]]] = {
    "mic_missing": (
        "耳机已插入，但未识别到耳机麦克风",
        "耳机插着，系统里却没有它的麦克风——插孔识别没跑完。"
        "把耳机拔下来、再插到底一次即可。",
        (NotificationAction("recheck", "我已重新插好"),),
    ),
    "output_drifted": (
        "耳机插着但声音没走耳机",
        "这就是「耳机没声音」最常见的一种。可以点下面的按钮把声音切回耳机。",
        (
            NotificationAction("fix_output", "把声音切回耳机"),
            NotificationAction("recheck", "重新检测"),
        ),
    ),
    "muted": (
        "耳机输出被静音了",
        "声音路由是好的，但输出的静音开关是开着的。",
        (
            NotificationAction("fix_mute", "取消静音"),
            NotificationAction("recheck", "重新检测"),
        ),
    ),
    "input_drifted": (
        "耳机麦克风在，但默认输入仍是内置麦克风",
        "耳机麦克风在系统里，但默认输入设备仍指向内置麦克风。"
        "点下面的按钮把它切回来即可。",
        (
            NotificationAction("fix_input", "切回耳机麦克风"),
            NotificationAction("recheck", "重新检测"),
        ),
    ),
}


class Plugin(NotifyCapability):
    id = "audio_guard"
    name = "音频看护"
    run_mode = RUN_ON_DEMAND      # 无节拍：检测由「启动 / 唤醒 / 音频设备变化」点火
                                  # （见模块 docstring）。tick() 仍是执行体。
    failure_policy = FAIL_LOG     # 纯只读检测：一次失败不该熔断整个组件
    api_version = 3
    order = 80

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._last: audio_state.Snapshot | None = None
        #: 最近一次广播出去的取值（用于日志对比，不参与判断）。
        self._state = "unknown"

    # ---------- 配置（默认值单一真源，§9.5）----------

    def config_schema(self) -> FormSchema:
        """本组件的配置项。

        ``notify_enabled`` 是刻意的：通知会弹到屏幕上、还会进通知中心，属**打扰性**
        功能，用户得有个地方能关掉它 —— 只关通知不影响检测与面板状态。
        """
        return FormSchema(
            plugin_id=self.id,
            version=1,
            fields=(
                FieldSpec(key="notify_enabled", label="出问题时弹系统通知",
                          type=FieldType.BOOL, default=True,
                          tooltip="关掉后仍然检测、面板照常显示状态，只是不弹通知。"),
            ),
        )

    @property
    def notify_enabled(self) -> bool:
        return bool(self.cfg.value("notify_enabled"))

    def set_notify_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self.notify_enabled == enabled:
            return

        self.cfg.set("notify_enabled", enabled)
        self.publish_snapshot()

    # ---------- 生命周期 ----------

    def start(self) -> None:
        """启动即体检一次。

        这里**同步**跑探针而不排给调度器：探针是十来个 CoreAudio / IOKit 属性读，
        微秒~毫秒级、不碰磁盘不起进程，正是「启动时读一次现场」的用法
        （``ipad_tunnel.start()`` 读状态文件是同一类）。排给调度器反而会让首屏
        在若干秒里显示「尚未体检」。
        """
        self._refresh()

    def tick(self) -> StateReport:
        """每轮执行体：重新体检 → 出快照 → 必要时弹通知。

        跑在框架 worker 线程。返回非 None 的 ``StateReport``，框架自动广播到
        ``plugin.audio_guard.status.main``。
        """
        self._refresh()
        report = self._snapshot()
        self._state = report.state
        self._maybe_notify(report.state)
        return report

    # ---------- 事件（只登记意图，活留给 tick）----------

    def event_topics(self) -> list:
        """订阅三个事件（§8.3.1 通用订阅）。

        - ``system.power.wake``：唤醒是故障的高发时刻 —— 音频编解码器此时要重新做
          一遍插孔识别，而系统刚醒、各子系统还在恢复；
        - ``system.audio.device_changed``：运行中的插拔与默认设备漂移。这一条是
          「不用轮询」的前提：没有它，插上耳机要等到下次唤醒才可能被看见；
        - ``app.panel_opened``：**不判故障，只保证面板那一行说的是此刻的事实**。
          本组件没有节拍（面板行画的是最后一轮 tick 的结论），不订它就会在屏幕一直
          亮着时停在旧结论上。

        前两条事件源都**在它那一侧等过稳定窗口**（唤醒 3s / 设备变化 3s），所以这里可以
        直接点火 —— 探针读到的已经是终态，不会把「识别跑完一半」误判成故障。第三条没有
        窗口可言：它就是「用户此刻在看」，探针本来就读的是当下。
        """
        return [POWER_WAKE_TOPIC, AUDIO_DEVICE_TOPIC, PANEL_OPENED_TOPIC]

    def on_system_event(self, topic: str, payload) -> None:
        """系统事件到达（主线程）——**只登记意图**，探针留给 ``tick()``。

        ``payload`` 对两条系统事件都只是「什么时候、因为哪几类属性」的说明，判据要看
        现场，故这里不看它 —— 多一个读法就多一处会漂移的判断（``app.panel_opened``
        连这个都没有：它不带载荷）。

        三个分支做的事**完全一样**（``mark_dirty``），之所以分开写而不是合并成一句
        ``mark_dirty``：``reason`` 是事后查日志时唯一能分辨「这次是谁点起来的」的东西，
        合成一个常量就等于把「用户开了面板」和「耳机刚插上」记成同一件事。
        """
        if topic == POWER_WAKE_TOPIC:
            log.info("系统唤醒，安排一次音频体检")
            self.mark_dirty("system_wake")
        elif topic == AUDIO_DEVICE_TOPIC:
            kinds = ", ".join(getattr(payload, "kinds", ()) or ()) or "未知"
            log.info("音频设备变化（%s），安排一次音频体检", kinds)
            self.mark_dirty("audio_device")
        elif topic == PANEL_OPENED_TOPIC:
            log.debug("面板被打开，安排一次音频体检（只为让面板那一行是此刻的事实）")
            self.mark_dirty("panel_opened")

    # ---------- 体检 ----------

    def _refresh(self) -> None:
        self._last = audio_state.probe()

    def _snapshot(self) -> StateReport:
        """把体检结果映射成取值（**只报取值名**，级别归声明，决议 D13）。"""
        snap = self._last
        now = time.time()
        if snap is None or not snap.ok:
            reason = snap.reason if snap else "尚未体检"
            return StateReport("probe_failed", f"读不到音频状态（{reason}）", updated_at=now)

        codec = snap.codec
        by_uid = snap.uids()

        # 「耳机插着吗」——有 codec 结论就听它，没有就退回「设备还在不在」。
        # 前者权威，后者只是降级估计（所以 Intel 机型上这个判断会弱一些）。
        if codec.available and codec.hp_detect is not None:
            plugged = codec.hp_detect
        else:
            plugged = audio_state.UID_HEADSET_OUT in by_uid
        if not plugged:
            return StateReport("no_headset", "未接有线耳机", updated_at=now)

        # 识别没跑完：先别下结论，等下一轮（这正是「只听出耳机」那一刻的形态）。
        if codec.available and codec.confidence and codec.confidence != "CompleteFinal":
            return StateReport(
                "detecting", f"插孔识别进行中（{codec.confidence}）", updated_at=now,
            )

        # 插孔认了耳机、却没认麦克风 —— 软件修不了，只能拔插（通知里会这么说）。
        if codec.available and codec.mic_detect is False:
            return StateReport("mic_missing", updated_at=now)

        # 声音走内置扬声器 —— 用户感知就是「耳机没声音」。
        # 只认「内置扬声器」这一种：默认输出是蓝牙耳机等，是用户的正当选择，不是故障。
        if snap.default_output_uid == audio_state.UID_SPEAKER:
            return StateReport("output_drifted", updated_at=now)

        if snap.output_muted:
            return StateReport("muted", "耳机输出被静音了", updated_at=now)

        # 默认输入指向内置麦克风 —— 麦克风是好的、只是没被选中，软件能修（切回耳机麦）。
        # 但「耳机麦克风」这个设备必须真的在系统设备清单里，否则切不过去：设备不在 =
        # 芯片 / 系统压根没把它暴露出来（你系统设置里也看不到它），与「没插耳机 /
        # 插了没认全」同类，只能拔插，不能靠切换默认设备修复。旧逻辑不验这一条，于是出过
        # 这种局面：codec 报 MicDetect=True、Confidence=CompleteFinal，但
        # BuiltInHeadphoneInputDevice 始终不在清单里，却仍报「已被识别」、还给了个
        # 必然失败的切换按钮。
        if snap.default_input_uid == audio_state.UID_INTERNAL_MIC:
            if audio_state.UID_HEADSET_IN in by_uid:
                return StateReport("input_drifted", updated_at=now)
            return StateReport("mic_missing", updated_at=now)

        return StateReport("ok", "耳机与麦克风均正常", updated_at=now)

    # ---------- 通知 ----------

    def _maybe_notify(self, state: str) -> None:
        """异常取值 → 弹通知。去重由通知中枢负责（同一条内容只弹一次）。"""
        if not self.notify_enabled:
            return
        alert = _ALERTS.get(state)
        if alert is None:
            return
        title, body, actions = alert
        sent = self.notify(NotificationRequest(
            key=f"audio_guard.{state}", title=title, body=body, actions=actions,
        ))
        if not sent:
            log.debug("通知「%s」未投递（去重命中或本平台无后端）", title)

    def on_notification_action(self, key: str) -> None:
        """用户在通知上点了按钮（主线程）。

        **每条动作都重新读现场再动手**：通知可能是几小时前发的，那时的判断早已过期
        —— 用户点的这一刻耳机可能已经拔了、或者已经被别的路径修好了。
        """
        if key == "recheck":
            log.info("用户点了「重新检测」")
            self._recheck_now()
        elif key == "fix_input":
            self._apply_fix("把默认输入切回耳机麦克风",
                            lambda: audio_state.set_default_input(
                                audio_state.UID_HEADSET_IN))
        elif key == "fix_output":
            self._apply_fix("把默认输出切回耳机",
                            lambda: audio_state.set_default_output(
                                audio_state.UID_HEADSET_OUT))
        elif key == "fix_mute":
            self._apply_fix("取消默认输出的静音", lambda: audio_state.set_output_muted(False))
        else:
            log.info("收到未定义的通知动作：%s", key)

    def _recheck_now(self) -> None:
        """就地重跑一轮（用户点的动作，要求「点了就有反应」）。

        这里直接跑而不 mark_dirty：动作发生在主线程、要立刻反映到面板上；探针本身
        是内存级系统调用（见 ``start()`` 的理由），可以直接跑。
        """
        self._refresh()
        self.publish_snapshot()

    def _apply_fix(self, what: str, action) -> None:
        """执行一条修复动作并立刻复检；结果如实说（成功≠修好）。"""
        ok = False
        try:
            ok = bool(action())
        except Exception:  # noqa: BLE001 —— 修复失败不该掀翻动作线程
            log.exception("执行修复动作失败：%s", what)
        log.info("修复动作「%s」写入%s", what, "成功" if ok else "失败")
        self._recheck_now()

    # ---------- 诊断文本（框架的「复制诊断」消费它）----------

    def diagnostics_text(self) -> str:
        """一份可粘贴的体检明细。

        本组件这一期**只检测**，所以「检测对不对」就是全部要验证的东西 —— 这份文本
        因此把**原始判据**逐条列出来（而不是只给结论），用户对着它就能看出组件是否
        看错了现场。
        """
        lines = [f"# 音频看护 · 体检明细（{time.strftime('%Y-%m-%d %H:%M:%S')}）"]
        snap = self._last
        if snap is None:
            lines.append("（尚未体检）")
            return "\n".join(lines)

        report = self._snapshot()
        lines.append(f"结论取值：{report.state} — {report.summary or _STATE_TEXT.get(report.state, report.state)}")
        lines.append("")

        if not snap.ok:
            lines.append(f"探针不可用：{snap.reason}")
            return "\n".join(lines)

        c = snap.codec
        lines.append("## 插孔识别（编解码器自报）")
        if not c.available:
            lines.append("  本机编解码器不报这些字段（判据降级为「看设备在不在」）")
        else:
            lines.append(f"  认出耳机   HPDetect   = {c.hp_detect}")
            lines.append(f"  认出麦克风 MicDetect  = {c.mic_detect}")
            lines.append(f"  识别进度   Confidence = {c.confidence or '(空)'}")
        lines.append("")

        lines.append("## 默认设备")
        lines.append(f"  输出 = {snap.default_output_uid or '(无)'}"
                     f"  数据源 = {snap.output_source or '(未知)'}")
        lines.append(f"  输入 = {snap.default_input_uid or '(无)'}")
        lines.append(f"  静音 = {snap.output_muted}   音量 = {snap.output_volume}")
        lines.append("")

        lines.append("## 设备清单")
        for dev in snap.devices:
            flags = []
            if dev.has_input:
                flags.append("输入")
            if dev.has_output:
                flags.append("输出")
            lines.append(f"  {dev.uid:36} {'/'.join(flags) or '无流'}   {dev.name}")
        return "\n".join(lines)

    # ---------- 外部资源（无：本组件不落盘、不起守护）----------

    def resources(self) -> PluginResources:
        """本组件没有数据文件、没有 helper 任务、不产生系统变更 —— 声明空清单。

        它是纯只读的（唯一写操作是用户点通知按钮时切默认设备，而那属于 CoreAudio
        的即时设置，不落任何我们自己的文件）。
        """
        return PluginResources()

    # ---------- UI 贡献（声明点方法，决议 D20）----------

    def row_specs(self):
        """状态行：只要一条——整行的副标题就是体检结论。

        不摆开关：本组件的“自动化”就是检测本身，而检测**不该有总闸**（关掉它只是
        让你看不到问题）。真正需要控制的是「要不要弹通知」，那个在设置页。
        """
        return (StatusRowSpec(key="main"),)

    def component(self):
        """组件身份：耳机图标（取值图标另见 ``STATES``）。"""
        return ComponentSpec(icon=IconName.HEADSET)

    def state_specs(self):
        """状态声明（静态）：取值闭集见 ``STATES``。

        ``stale_after=None``：本组件**没有节拍**（按需型，见模块 docstring），两次体检
        之间隔几小时是正常的，按周期组件的口径判「失联」会误报。
        """
        return (
            StateSpec(
                key="main", icon=IconName.HEADSET,
                states=STATES, initial="unknown",
                stale_after=None,
            ),
        )

    def settings_page(self):
        """设置页：「出问题时弹系统通知」这一个开关（界面在 ``settings.py``）。

        本组件**只有一个**可配置项，但它必须有个地方能改 —— 通知会弹到屏幕上、还进
        通知中心（见 ``config_schema`` 的说明），用户得能关掉它。「配置项被代码真读、
        用户却只能手改 YAML」正是 `recognize` 被转正时修过的同款缺陷，不重犯。

        「现在什么情况」不在这页里：框架在页首注入状态机段，同一件事留两个显示位
        就会各自漂移。
        """
        return SettingsPageSpec(
            build=lambda: settings_ui.build_page(self),
            reason="custom: 本组件只有一个布尔配置项，而声明式渲染器至今未接线"
                   "（contribution/form.py 零消费方）⇒ 自绘是今天唯一可用路径",
        )
