"""bt_guard：蓝牙设备唤醒重连看护。

**它解决什么**：这台 Mac 合盖 / 关屏再唤醒之后，蓝牙鼠标偶发连不上 —— 蓝牙菜单里那条
记录压根不给「连接」按钮，系统看不见它。用户手动关开一次蓝牙开关就好了。手动那个动作
有**免特权**的等价实现（切蓝牙控制器电源），所以这件事完全可以自动化。

**什么时候跑 —— 两个时刻，永不轮询**：本组件是 ``RUN_ON_DEMAND``，没有节拍。
点火来源有两类：

- **边界的两头**（``core/power`` 的**四条**事件 —— 两族 × 两个方向）：

  - 睡眠类（``system.power.sleep`` ＋ ``system.power.display_sleep``）：记下**睡前那一刻**
    的设备在场情况，存成本轮的基线；
  - 唤醒类（``system.power.wake`` ＋ ``system.power.display_wake``）：等事件源那个稳定窗口
    过去后再读一次，与基线比。**刚醒那几秒设备常还没枚举回来**，所以这一读若还没见到它，
    先在一段耐心窗里重探到它出现为止（上限见 :data:`WAKE_HID_PATIENCE_S`）—— 不等就动手
    会白断一次正连着的设备（09-30 现场就是这么白断了 AirPods 的）。

- **面板被打开**（``app.panel_opened``，见 ``core/events``）：**只重读一次现状，绝不动手**。
  加它的理由是本组件没有节拍，而面板那一行画的是「最后一轮 tick 的结论」⇒ 屏幕一直亮着
  时掉了线，界面会一直写着「设备在线」（2026-09-24 现场：鼠标掉了近两小时，那一行还停在
  启动那轮）。它走的是 ``_evaluate`` 里 ``not woke`` 那条普通路径，所以只报现状、不碰
  蓝牙开关 —— 「亮屏期间掉线不动手」这条行为（用户 2026-09-24 裁定）不受影响。

**两族都必须订，而且要自己去重**：本机日常那次「关屏 / 亮回来」走的是**显示器那一族**，
``WillSleep`` / ``DidWake`` 根本不发（现场依据见 ``core/power`` 的 docstring）；而真合盖
那一次两族会两两同时来（实测屏幕先亮、16 秒后系统才唤醒）⇒ 不去重就会在**一次**唤醒里
动两次手。去重口径＝「本轮已处理过唤醒就不再处理，直到收到下一条睡眠类事件才重新武装」
（:meth:`Plugin.on_system_event`）。**别拿动作冷却兜这件事**：冷却的语义是「防两次操作
叠加」，不是「同轮去重」—— 用它兜的结果是「上半轮已经动过手、下半轮被静默跳过」，而面板
停在动作中途那条「正在重开蓝牙」上。

判据因此是**跨睡眠边界的两个点**，不是单点：

======================  ================  ==========================
睡前                      醒后              结论
======================  ================  ==========================
在                        在                正常
在                        不在              **掉线** ⇒ 动手
不在                      不在              本来就没连（用户自己关了鼠标）⇒ 不动手
不在                      在                它自己回来了 ⇒ 正常
（没记到）                 不在              见下「降级路径」
======================  ================  ==========================

单点永远读不出「是刚才掉的，还是一直没连」—— 这两种情况的正确动作相反。

**降级路径（基线缺失时）**：睡眠类事件在某些关屏姿势下可能两条都不来。那时若「本进程
运行期间亲眼见过它」，仍按掉线处理并动手 —— 代价是「先关鼠标再关屏」这种情形会被误判
一次（重开一次蓝牙，8 秒、无损、可恢复）。若从没见过，则如实报「没记到睡前现场」、
不动手。这个取舍写在 :meth:`Plugin._on_wake` 里，诊断文本也会带上。

**同族里先到的那条读数才算数**：两族睡眠通知会先后到达，而**先到的那条**离「睡之前」
最近 —— 后到的那条可能已经是设备掉线之后了。拿它覆盖基线，就会把「这次掉的」读成
「本来就没连」，醒来不动手且不报错（判据整体静默失效）。所以基线**已记过就不覆盖**，
见 :meth:`Plugin._evaluate`。

**动作只有一档**：蓝牙控制器断电重开。单设备重连（connect / disconnect）在本机故障态
下实测无效（全返回 ``kIOReturnError``、HID 层 20 秒零变化），所以不存在「逐级回退」——
这一档要么成、要么不成。它有副作用：**会打断所有蓝牙设备**，所以一轮唤醒只做一次。

**为什么要有那个 ``.app``**：切蓝牙控制器电源调的是私有 API，而 macOS 要求调用者具备
「带蓝牙用途说明的代码签名身份」。barkit 主进程（Homebrew python）怎么补都不行 ——
本机实测直接 SIGABRT。所以这个能力落在一个随包分发的 80KB 助手 ``.app`` 上，由 ``open``
按需拉起。全部依据见 ``resources/README.md``。
"""

from __future__ import annotations

import logging
import time

from barkit import paths
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
from barkit.core.events import PANEL_OPENED_TOPIC
from barkit.core.plugin import FAIL_LOG, RUN_ON_DEMAND
from barkit.core.power import (
    POWER_DISPLAY_SLEEP_TOPIC,
    POWER_DISPLAY_WAKE_TOPIC,
    POWER_SLEEP_TOPIC,
    POWER_WAKE_TOPIC,
)
from barkit.core.resources import PluginResources
from barkit.core.schema import FieldSpec, FieldType, FormSchema
from barkit.plugins.bt_guard import settings as settings_ui

from . import bt_state

log = logging.getLogger(__name__)

#: 取值闭集（决议 D13）：取值名 / 级别 / 默认文案只写一次，运行期只报取值名。
#: **声明顺序＝判断次序** —— ``_evaluate`` 里那串 early-return 与它同序（先问「读得到吗 /
#: 配了没」，再报事实：在不在线，最后才是动作相关的取值）。唯一的例外是排在最前的
#: ``unknown``：它是首屏起点，不是某条判断的结论。该顺序同时是**代表态的平局序**
#: （D1：平局取先声明者），故别随手调整。
#: 取值图标一律留空、回落主图标 ``MOUSE``：这些取值说的是**同一件东西**的不同处境
#: （在线 / 掉线 / 重开中 / 读不到），靠级别色区分就够，不必再造一组变体
#: —— 与 ``IconName.HEADSET`` 同一条理由（D19「不预先堆砌」）。
STATES: tuple[StateOption, ...] = (
    StateOption("unknown", StateLevel.IDLE, text="尚未体检"),
    StateOption("not_configured", StateLevel.IDLE, text="还没选要看的设备"),
    StateOption("probe_failed", StateLevel.IDLE, text="读不到蓝牙状态"),
    # 蓝牙关着是**用户的选择**，不是故障 —— 不动手，也不进角标。
    StateOption("bt_off", StateLevel.IDLE, text="蓝牙关着"),
    StateOption("device_unknown", StateLevel.WARN, text="这个设备不在已配对列表里"),
    StateOption("ok", StateLevel.OK, text="设备在线"),
    StateOption("offline", StateLevel.IDLE, text="设备不在线"),
    # 醒后不在、而睡前那次没记到现场 ⇒ 判不出是掉的还是本来就没连，如实说、不动手。
    StateOption("no_baseline", StateLevel.IDLE, text="睡醒后不在线，但这次没记到睡前现场"),
    StateOption("lost", StateLevel.WARN, text="睡醒后掉线，正在重开蓝牙"),
    StateOption("lost_untouched", StateLevel.WARN, text="睡醒后掉线，这次没有动手"),
    StateOption("recover_failed", StateLevel.ERR, text="重开蓝牙后设备仍未回来"),
)

#: 取值文案单一真源（决议 D18）：运行时 ``summary`` 留空即回落到这里。
_STATE_TEXT = {opt.id: opt.text for opt in STATES}

#: 四条点火事件按「离开边界 / 回来边界」分两类。**两族都要订**：本机日常那次
#: 「关屏 / 亮回来」只发显示器那一族（依据见 ``core/power`` docstring），而真合盖时
#: 两族会两两同时来 —— 两条睡眠各自都要能被认出来（日志里要看得见是哪族），两条唤醒
#: 则要合起来只算一次（见 :meth:`Plugin.on_system_event`）。
_SLEEP_TOPICS = (POWER_SLEEP_TOPIC, POWER_DISPLAY_SLEEP_TOPIC)
_WAKE_TOPICS = (POWER_WAKE_TOPIC, POWER_DISPLAY_WAKE_TOPIC)

#: 话题 → 说人话的说法（只进日志）。「系统」还是「显示器」必须留在日志里 —— 那是事后
#: 分辨「那次到底是关屏还是真睡眠」的唯一线索，合并成一句「收到睡眠事件」就没了。
_TOPIC_ZH = {
    POWER_SLEEP_TOPIC: "系统将要睡眠",
    POWER_DISPLAY_SLEEP_TOPIC: "显示器将要关闭",
    POWER_WAKE_TOPIC: "系统已唤醒",
    POWER_DISPLAY_WAKE_TOPIC: "显示器已唤醒",
}


def _tri(value: bool | None) -> str:
    """三态渲染：``True`` / ``False`` / ``None``（没记到）分别说人话。

    ``None`` 与 ``False`` 必须说得出区别 —— 前者是「没读到」，后者是「读到了、不在」，
    合成一句就会把「组件没记上」误读成「设备本来就不在」。
    """
    return "在" if value is True else "不在" if value is False else "（没记到）"

#: 自动动作的冷却（秒）。防的是**两次操作叠加** —— 用户实测过「先连上、又断开、又连上」
#: 那种形态，根因就是两次重开挨得太近。用户手动点通知不受它限制（那是他明确的意图）。
ACTION_COOLDOWN_S = 120

#: 醒后那一次读若还没见到目标设备，重探到这么久为止再下结论。
#: **两档共用**（闸门是 :meth:`Plugin._settled`）：「这层读不出来」与「读得出来、但设备
#: 还没回到 HID 层」。后者是 2026-09-30 现场真实发生的那一档 —— 那天亮屏后第一读就读得到
#: （只是里面没有那只鼠标），旧代码当掉线当场重开了一次蓝牙、把正连着的 AirPods 打断了，
#: 而设备其实约 6 秒就自己枚举回来了（14:05:00 读空 → 14:05:06 见到）⇒ 那一刀是白挨的。
#: 两个数差一个数量级：``bt_state._ioreg_items`` 那次短重试一共才 0.6 秒（3 × 0.3），
#: 无论如何盖不住 6 秒 ⇒ 耐心必须落在这里（「多试几次」，但试的是**设备在不在**）。
#: **必须有上限**：真掉线时设备永远不会自己出现，「一直等」会让醒来这一拍永不收口。
#: **只给「醒后决策」这一读**：``wait_present`` 的高频轮询不吃这段耐心，否则每拍被拖长、
#: 反而拖慢恢复探测。
WAKE_HID_PATIENCE_S = 7.0

#: 耐心窗内两次重探之间的间隔。每次重探本身约 0.35 秒（``ioreg`` 那份 2.5MB 的现场是
#: 大头），所以实际每轮约 0.85 秒 —— 7 秒的窗里落得下八次左右。
WAKE_HID_RETRY_GAP_S = 0.5

#: 需要弹通知的取值 → (标题, 正文, 按钮)。**只有「修不好」才打扰用户**：
#: 自动修好了就该安静（用户要的是鼠标能用，不是一条「我修好了」的通知）。
_ALERTS: dict[str, tuple[str, str, tuple[NotificationAction, ...]]] = {
    "recover_failed": (
        "蓝牙设备没有自动连回来",
        "已经重开过一次蓝牙，但设备仍然没有出现在系统里。"
        "它可能是关机了或不在范围内，也可能需要在系统设置里手动点一下「连接」。",
        (NotificationAction("retry", "再试一次"),),
    ),
}


class Plugin(NotifyCapability):
    id = "bt_guard"
    name = "蓝牙看护"
    # 无节拍：只在「启动」「边界两头（睡眠类 / 唤醒类四条事件）」「面板被打开」时跑
    # （见模块 docstring）。
    # tick() 仍是执行体，动作跑在框架给本组件单开的 worker 线程里。
    run_mode = RUN_ON_DEMAND
    failure_policy = FAIL_LOG      # 只读探针失败不该熔断整个组件
    api_version = 3
    order = 85

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._last: bt_state.Snapshot | None = None
        #: 最近一次广播出去的取值（用于日志对比，不参与判断）。
        self._state = "unknown"
        #: 两个事件标志。**不能用单槽**：睡眠事件与唤醒事件可能挤进同一轮 tick
        #: （睡前那次没跑完就被系统冻结），分不清谁是谁就会把「醒后的现场」错当成
        #: 「睡前的现场」存成基线。
        self._sleep_seen = False
        self._wake_seen = False
        #: 本轮边界的唤醒**是否已经处理过**（两族去重的唯一依据）。收到任意一条睡眠类
        #: 事件时重新武装 —— 也就是「一轮边界 = 一条睡眠 ＋ 首个唤醒」。不这么做的话，
        #: 真合盖那一次会先来显示器唤醒、16 秒后再来系统唤醒，两次都去比对基线：第一次
        #: 把基线消费掉、第二次落到「降级路径」又动一次手。
        self._wake_handled = False
        #: 本轮睡眠的睡前基线：True = 睡前在场 / False = 睡前不在 / None = 没记到。
        #: **这是一次性的**：醒来比对时消费掉（见 ``_on_wake``），所以它答的是
        #: 「下一拍要不要拆机」，不是「上一拍发生过什么」。
        self._baseline: bool | None = None
        #: 最近一次真正做过的跨边界比对 ``(睡前, 醒后)``，**只喂诊断文本**。
        #: 与 ``_baseline`` 分开是必须的：基线一消费就归零，拿它当「上一次比的是什么」
        #: 会永远显示「没记到」—— 那正是最该看出来的那一次（2026-09-23 自检发现）。
        self._last_pair: tuple[bool | None, bool] | None = None
        #: 本进程运行期间有没有亲眼见过它 —— 基线缺失时靠它走降级路径。
        self._ever_seen = False
        #: 上一次自动动手结束的**墙上**时刻（诊断文本里按 HH:MM:SS 显示给人看）。
        self._last_act_at = 0.0
        #: 同一次动手的**单调**时刻（冷却依据）。两个都要：冷却问的是「过了多久」，
        #: 显示问的是「几点」；只留墙上时钟时，系统时钟一被校回，``now - _last_act_at``
        #: 变负 ⇒ 冷却期永远过不去 ⇒ 该重连的那一次被当成「刚重开过」静默跳过。
        self._last_act_mono = 0.0
        #: 用户从通知上点了「再试一次」。
        self._manual_seen = False

    # ---------- 配置（默认值单一真源，§9.5）----------

    def config_schema(self) -> FormSchema:
        return FormSchema(
            plugin_id=self.id,
            version=1,
            fields=(
                FieldSpec(key="device", label="要看的设备", type=FieldType.TEXT,
                          default="",
                          tooltip="已配对蓝牙设备的地址。在设置页的下拉里选。"),
                FieldSpec(key="auto_reconnect", label="睡醒后掉线时自动重开蓝牙",
                          type=FieldType.BOOL, default=True,
                          tooltip="关掉后仍然判定与显示，只是不动手。"
                                  "重开会把当前连着的蓝牙设备（键盘、耳机等）一并断一次，"
                                  "它们通常几秒内自己回来 —— 这是它唯一的副作用。"),
                FieldSpec(key="notify_enabled", label="修不好时弹系统通知",
                          type=FieldType.BOOL, default=True,
                          tooltip="只在「重开过了、设备还是没回来」时弹。自动修好不打扰。"),
            ),
        )

    # 三处默认值（``""`` / ``True`` / ``True``）的真源都在上面那份 ``config_schema()`` 里，
    # 这里不再各抄一遍 —— 抄一遍的代价是把 schema 的默认值改掉时行为不动、也没有判据会红。

    @property
    def device(self) -> str:
        return str(self.cfg.value("device")).strip()

    @property
    def auto_reconnect(self) -> bool:
        return bool(self.cfg.value("auto_reconnect"))

    @property
    def notify_enabled(self) -> bool:
        return bool(self.cfg.value("notify_enabled"))

    # ---------- 配置写入 ----------
    #
    # **写完一律请求框架重跑一轮**（``mark_dirty``），而不是就地重算再广播。两条理由
    # （2026-09-23 从「选完设备界面还写着『还没选要看的设备』」这一个症状趟出来）：
    #
    # 1. **本组件没有节拍**（``RUN_ON_DEMAND``），而面板那一行显示的是**最后一次 tick
    #    算出来的结论**。启动那轮如果还没选设备，它说的就是「还没选要看的设备」；用户在
    #    设置页选了设备、配置也确实写进去了，可如果没有人再去跑一次判定，那句话就会一直
    #    挂着 —— 界面说的就不是现在的做法了。（挂到什么时候为止：2026-09-24 之前只挂到
    #    下次睡眠 / 唤醒，真能挂一整天；那天补了 ``app.panel_opened`` 之后，用户开一次
    #    面板也会重跑一轮。）
    # 2. **不能改成就地重算再广播**：那要跑两个子进程（属 IO），而这三个方法是设置页那条
    #    信号直接进来的（主线程）。何况本组件没有 ``_snapshot()``，无参 ``publish_snapshot()``
    #    在基类里只会打一条日志、什么都不广播 —— 上一版正是这么写的，所以它既没刷新
    #    也没报错，只是悄悄没有任何作用。

    def set_device(self, addr: str) -> None:
        addr = str(addr or "").strip()
        if self.device == addr:
            return

        self.cfg.set("device", addr)
        self.mark_dirty("device_changed")

    def set_auto_reconnect(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self.auto_reconnect == enabled:
            return

        self.cfg.set("auto_reconnect", enabled)
        self.mark_dirty("auto_reconnect_changed")

    def set_notify_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self.notify_enabled == enabled:
            return

        self.cfg.set("notify_enabled", enabled)
        self.mark_dirty("notify_enabled_changed")

    # ---------- 生命周期 ----------

    def start(self) -> None:
        """启动只登记意图，读现场留给 worker 线程。

        （2026-09-28 改）原先在这里同步跑 ``bt_state.probe()``：``system_profiler``
        + ``ioreg``（2.6MB）+ 两次 ``plistlib`` 解析，合计约 0.45s，而且**卡在启动
        主线程上** —— 排在它后面的每个插件的 ``start()``、面板与设置窗的构造都得等。
        改走 ``mark_dirty``：合并窗口 50ms，worker 随即跑首轮 ``tick()``，主线程只
        付出一次登记；首屏拿到数据只晚「probe 本身那点时间」，不是多等一个周期。
        """
        self.mark_dirty("start")

    def tick(self) -> StateReport:
        """每轮执行体。跑在框架给本组件单开的 worker 线程里。

        **事件标志在这里消费**（而不是在 handler 里处理）：handler 只许登记意图，
        读现场与动手都归这里。消费后立刻清零 —— 一次事件只处理一轮。
        """
        slept, woke, manual = self._sleep_seen, self._wake_seen, self._manual_seen
        self._sleep_seen = self._wake_seen = self._manual_seen = False

        report = self._evaluate(slept=slept, woke=woke, manual=manual)
        self._state = report.state
        self._maybe_notify(report.state)
        return report

    # ---------- 事件（只登记意图，活留给 tick）----------

    def event_topics(self) -> list:
        """订阅**四条电源事件 ＋ 面板打开**（§8.3.1 通用订阅）。

        电源那四条：**系统与显示器两族都要**。时机都在 ``core/power`` 那一侧定好了
        （唤醒类等过稳定窗口、睡眠类立即投），这里直接信它、不再各自 sleep。少订显示器
        那一族就等于没订：本机日常的「关屏 / 亮回来」只发那一族，只订系统那两条的组件
        除启动那轮外永远不会跑（见 ``core/power``）。

        第五条 ``app.panel_opened`` 与判定无关，只管**界面说的是不是此刻的事实**：本组件
        没有节拍，面板那一行画的是最后一轮 tick 的结论，不订这条就会在「屏幕一直亮着」
        期间停在旧结论上（:meth:`on_system_event` 里它只发 ``mark_dirty``，不置任何判别
        标志 ⇒ 醒来/睡前那条判据完全不受影响）。
        """
        return [*_SLEEP_TOPICS, *_WAKE_TOPICS, PANEL_OPENED_TOPIC]

    def on_system_event(self, topic: str, payload) -> None:
        """系统事件到达（主线程）—— **只置标志 + 登记意图**，读现场与动作留给 ``tick()``。

        ``payload`` 对各条事件都只是「什么时候」，判据要看现场，故这里不看它。

        **唤醒侧在这一层去重**：一次真实的「合盖 → 唤醒」会送来两条唤醒（实测屏幕先亮、
        16 秒后系统才唤醒），而两次比对之间有 16 秒——远超过调度器的合并窗口，所以这里
        不拦就会跑两轮 ``tick()``、动两次手。口径是「本轮首个唤醒算数」：收到任意一条
        睡眠类事件才重新武装。**不置标志就等于不点火**，因此被拦下的那条连 ``mark_dirty``
        都不发（否则会白跑一轮，还会把标志消费在没有任何新信息的一拍上）。

        **面板打开不属于边界**：它不置任何标志，于是 ``tick()`` 走的是「普通一拍」那条
        路径（``_evaluate`` 里 ``not woke``）—— 只重读现状、绝不碰蓝牙开关。这条区分是
        要的：用户开面板只是想看看现在怎么样，不是让组件趁他看的时候去断一次蓝牙。
        """
        if topic in _SLEEP_TOPICS:
            self._wake_handled = False        # 一轮边界从这里开始
            self._sleep_seen = True
            log.info("%s，会在睡前记一次设备现场（唤醒后据此判断是不是这次掉的）",
                     _TOPIC_ZH[topic])
            self.mark_dirty("sleep")
        elif topic in _WAKE_TOPICS:
            if self._wake_handled:
                log.info("同一轮边界内又收到一次唤醒（%s），不重复检查", _TOPIC_ZH[topic])
                return
            self._wake_handled = True
            self._wake_seen = True
            log.info("%s，安排一次蓝牙检查", _TOPIC_ZH[topic])
            self.mark_dirty("wake")
        elif topic == PANEL_OPENED_TOPIC:
            log.debug("面板被打开，安排一次只读体检（不动手）")
            self.mark_dirty("panel_opened")

    def on_notification_action(self, key: str) -> None:
        """用户在通知上点了按钮（主线程）。

        这里**只登记**，不在主线程上跑动作 —— 重开蓝牙要 8 秒以上（还得等设备回来），
        在主线程跑会冻住整个界面。交给 worker。
        """
        if key == "retry":
            log.info("用户点了「再试一次」")
            self._manual_seen = True
            self.mark_dirty("manual_retry")
        else:
            log.info("收到未定义的通知动作：%s", key)

    # ---------- 判定 ----------

    def _refresh(self) -> None:
        """只读一次现场（不动手）。"""
        self._last = bt_state.probe()

    def _settled(self, snap: bt_state.Snapshot, target: str) -> bool:
        """这份现场够下结论了吗 —— 读得到，**且**已知目标设备此刻在不在。

        两件事缺一不可：``hid_ok=False``（读不出）时不知道它在不在；读到了但里面没有它，
        也只说明「此刻还没枚举出来」。醒后那段耐心窗等的就是这一条。
        """
        return snap.hid_ok and snap.present(target)

    def _reprobe_hid_until(self, deadline: float, target: str) -> bt_state.Snapshot:
        """醒后这一读还没见分晓时，重探到 ``deadline`` 为止，再下结论。

        ``deadline`` 取 :func:`time.monotonic` 刻度（**不是**墙上时钟）：这是「还要等多久」
        而不是「这是何时的事实」—— 用 ``time.time()`` 时系统时钟被校回（NTP 步进 /
        唤醒后校正），``time.time() < deadline`` 会长时间为真，这段循环就按
        :data:`WAKE_HID_RETRY_GAP_S` 一跳一直空转，worker 被占住 ⇒ 调度器判 tick 超时
        ⇒ 连续五次即熔断停用，而日志只写「tick 超时」，看不出真正原因。

        「见分晓」的判据是 :meth:`_settled`，所以**两档共用**这段耐心：「这层读不出来」与
        「读得出来、但设备还没回到 HID 层」。依据见 :data:`WAKE_HID_PATIENCE_S` —— 现场实测
        亮屏后约 6 秒设备才重新枚举出来，而 ``bt_state`` 里那次短重试一共才 0.6 秒，盖不住。
        别把这段耐心搬进 ``bt_state`` —— ``wait_present`` 的高频轮询走的是那里，它要的是快。

        **到点仍没见分晓 ≠ 判定掉线**：读不出来仍走「读不出就别动手」那条守卫；读得出、
        设备确实不在，才由 :meth:`_on_wake` 按睡前基线处置。
        """
        snap = self._last or bt_state.Snapshot(False, "尚未体检")
        while not self._settled(snap, target) and time.monotonic() < deadline:
            time.sleep(WAKE_HID_RETRY_GAP_S)
            self._refresh()
            snap = self._last or snap
        return snap

    def _evaluate(self, *, slept: bool, woke: bool, manual: bool) -> StateReport:
        """本轮的核心判定：读现场 → 按「睡前 / 醒后」两点得结论 → 必要时动手。"""
        self._refresh()
        snap = self._last or bt_state.Snapshot(False, "尚未体检")
        now = time.time()

        target = self.device
        if not target:
            return StateReport("not_configured", updated_at=now)
        if not snap.ok:
            return StateReport("probe_failed", f"读不到蓝牙状态（{snap.reason}）", updated_at=now)

        # 醒后这一读若还没见分晓（读不出，或读得出而设备尚未枚举回来）：重探一段耐心窗
        # 再下结论 —— 刚醒那几秒设备「不在」是常态，不等就动手会白断一次已连的设备。
        # 重探后蓝牙层若也塌了，就当探针失败如实报。
        if woke and not self._settled(snap, target):
            snap = self._reprobe_hid_until(time.monotonic() + WAKE_HID_PATIENCE_S, target)
            now = time.time()
            if not snap.ok:
                return StateReport("probe_failed", f"读不到蓝牙状态（{snap.reason}）",
                                   updated_at=now)

        if not snap.hid_ok:
            # HID 主判据读不出来：无权判定「设备掉了」，按「没有证据」处理 —— 绝不趁这次
            # 去断电重开（那会无端打断 AirPods 之类当前连着的设备）。等下一轮边界或用户
            # 开面板再读。这正是不读 HID 就别动手的同一条原则（见 ``_on_wake`` 的降级路径）。
            return StateReport("probe_failed",
                               "HID 层读不出来（ioreg 解析失败），无法判断设备是否掉线",
                               updated_at=now)
        if snap.controller_on is False:
            return StateReport("bt_off", updated_at=now)
        if snap.paired_name(target) is None and not snap.present(target):
            return StateReport("device_unknown", updated_at=now)

        here = snap.present(target)
        if here:
            self._ever_seen = True

        # 睡前那一拍：把这个现场存成本轮的基线（醒来后据此判断）。
        if slept and not woke:
            if self._baseline is None:
                self._baseline = here
                log.info("睡前记到基线：设备%s", "在场" if here else "不在场")
            else:
                # **先到先得**：两族睡眠通知会先后到达，而先到的那条离「睡之前」最近；
                # 后到的那条可能已经是设备掉线之后了。覆盖它就把「这次掉的」读成
                # 「本来就没连」—— 醒来不动手、还不报错，整条判据静默失效。
                log.info("这一轮已经记过睡前基线（设备%s），这一次不覆盖", _tri(self._baseline))
            return self._plain(now, here)

        if manual:
            if here:
                return StateReport("ok", "手动检查：设备就在线", updated_at=now)
            return self._act(now, snap, why="手动请求", manual=True)

        if not woke:
            return self._plain(now, here)

        baseline = self._baseline
        self._baseline = None                 # 一次睡眠消费一次
        if slept:
            # 睡眠与唤醒挤进了同一轮 tick：睡前那一刻的现场已经读不到了（进程当时被
            # 系统冻结）。把此刻当成「睡前」是错的 —— 如实走「没有基线」那条路径。
            log.info("睡眠与唤醒挤在同一轮，睡前现场没读到")
            baseline = None

        return self._on_wake(now, snap, here, baseline)

    def _on_wake(self, now: float, snap: bt_state.Snapshot, here: bool,
                 baseline: bool | None) -> StateReport:
        """醒来这一拍：与睡前基线比。

        四种组合见模块 docstring。**「没记到基线」那条是降级路径**：那时若本进程运行期间
        见过它，仍按掉线处理并动手 —— 代价是「先关鼠标再关屏」会被误判一次（重开一次
        蓝牙，8 秒、无损、可恢复）。反过来若从没见过，就如实说「没记到睡前现场」、
        不动手：没有证据就不要动人东西。
        """
        if here:
            self._last_pair = (baseline, True)
            return StateReport("ok", updated_at=now)
        if baseline is True:
            self._last_pair = (baseline, False)
            return self._act(now, snap, why="睡前它还在", manual=False)
        if baseline is False:
            self._last_pair = (baseline, False)
            return StateReport("offline", updated_at=now)
        if self._ever_seen:
            self._last_pair = (baseline, False)
            return self._act(now, snap, why="本次运行见过它（这次没记到睡前现场）",
                             manual=False)
        self._last_pair = (baseline, False)
        return StateReport("no_baseline", updated_at=now)

    def _plain(self, now: float, here: bool) -> StateReport:
        """不带比对的现状报告（启动时、睡前那一拍）。"""
        if here:
            return StateReport("ok", updated_at=now)
        return StateReport("offline", updated_at=now)

    def _act(self, now: float, snap: bt_state.Snapshot, *, why: str,
             manual: bool) -> StateReport:
        """动手：重开蓝牙控制器，然后等设备回来。

        **跑在 worker 线程**（本方法只从 ``tick()`` 调），总时长可达 8 + 2 + 60 秒。
        调度器给每个插件单开一个线程池（``maxThreadCount=1``），所以这几十秒只占本组件
        自己的 worker，不会饿着别的插件。
        """
        if not self.auto_reconnect:
            return StateReport("lost_untouched", f"睡醒后掉线（{why}），"
                                                 "自动重连关着，没有动手", updated_at=now)
        if not manual and time.monotonic() - self._last_act_mono < ACTION_COOLDOWN_S:
            return StateReport("lost_untouched", f"睡醒后掉线（{why}），"
                                                 "刚重开过，这次不重复", updated_at=now)

        # 先播一条「正在处理」：动作要 8 秒以上，面板当场有反应比事后补一条更让人安心。
        self.publish_snapshot(StateReport(
            "lost", f"睡醒后掉线（{why}），正在重开蓝牙", updated_at=now))

        log.info("重开蓝牙控制器（%s）；当前已连的蓝牙设备都会断一次：%s",
                 why, "、".join(snap.connected_names()) or "（没有别的）")

        ok, said = bt_state.restart_controller(self.device)
        self._last_act_at = time.time()
        self._last_act_mono = time.monotonic()
        self._refresh()

        if ok:
            self._ever_seen = True
            log.info("重开蓝牙后设备回来了：%s", said)
            return StateReport("ok", said, updated_at=time.time())
        log.warning("重开蓝牙后设备仍未回来：%s", said)
        return StateReport("recover_failed", said, updated_at=time.time())

    # ---------- 通知 ----------

    def _maybe_notify(self, state: str) -> None:
        """**只有「修不好」才弹**。去重由通知中枢负责（同一条内容只弹一次）。"""
        if not self.notify_enabled:
            return
        alert = _ALERTS.get(state)
        if alert is None:
            return
        title, body, actions = alert
        if not self.notify(NotificationRequest(
            key=f"bt_guard.{state}", title=title, body=body, actions=actions,
        )):
            log.debug("通知「%s」未投递（去重命中或本平台无后端）", title)

    # ---------- 诊断文本（框架的「复制诊断」消费它）----------

    def diagnostics_text(self) -> str:
        """一份可粘贴的现场明细。

        这个组件的判据是**两个时刻的比对**，所以这里把「基线 / 现状 / 这个进程见过它没有」
        三样都列出来 —— 只给一句结论的话，用户没法判断组件是不是看错了。
        """
        lines = [f"# 蓝牙看护 · 现场明细（{time.strftime('%Y-%m-%d %H:%M:%S')}）"]
        target = self.device
        lines.append(f"要看的设备：{bt_state.pretty(target) if target else '（未设置）'}")
        lines.append(f"自动重连：{'开' if self.auto_reconnect else '关'}"
                     f"    修不好时通知：{'开' if self.notify_enabled else '关'}")
        lines.append("")

        lines.append("## 判定依据（跨睡眠边界的两个点）")
        lines.append("  睡前基线（本轮还没用掉）= " + _tri(self._baseline))
        lines.append("  本轮边界的唤醒 = "
                     + ("已处理过（同一轮再来唤醒不会重复动手）" if self._wake_handled
                        else "还没处理过（收到睡眠事件会重新武装）"))
        pair = self._last_pair
        if pair is None:
            lines.append("  上一次比对 = （本次运行还没跨过睡眠边界）")
        else:
            lines.append("  上一次比对 = 睡前 " + _tri(pair[0])
                         + " ⇒ 醒后 " + ("在" if pair[1] else "不在"))
        lines.append(f"  本次运行见过它 = {self._ever_seen}")
        lines.append("  最近一次动手 = "
                     + (time.strftime('%H:%M:%S', time.localtime(self._last_act_at))
                        if self._last_act_at else "（没动手过）"))
        lines.append(f"  当前取值 = {self._state} — "
                     f"{_STATE_TEXT.get(self._state, self._state)}")
        lines.append("")

        snap = self._last
        if snap is None:
            lines.append("（尚未体检）")
            return "\n".join(lines)
        if not snap.ok:
            lines.append(f"现场读不出来：{snap.reason}")
            return "\n".join(lines)

        lines.append("## 两层现场")
        lines.append("  蓝牙控制器 = "
                     + ("开" if snap.controller_on else "关" if snap.controller_on is False
                        else "（读不出状态）"))
        lines.append(f"  HID 层可读性 = {'读出来了' if snap.hid_ok else '读不出（ioreg 解析失败）'}")
        if target:
            addr = bt_state.norm(target)
            lines.append(f"  蓝牙层   = {'已连' if snap.bluetooth_connected(target) else '未连'}"
                         f"    在已配对列表 = {snap.paired_name(target) is not None}")
            lines.append(f"  HID 层   = {'已枚举' if (snap.hid_ok and addr in snap.hid) else '没有它'}"
                         f"    （主判据：用户能拿它输入吗；读不出时这一格不可信）")
        lines.append("")

        lines.append("## 设备清单")
        for addr, info in sorted(snap.devices.items()):
            marks = []
            if info.get("connected"):
                marks.append("已连")
            if addr in snap.hid:
                marks.append("HID")
            lines.append(f"  {bt_state.pretty(addr)}  {'/'.join(marks) or '未连':6}  "
                         f"{info.get('name', '?')}")
        if not snap.devices:
            lines.append("  （系统没报出任何蓝牙设备）")
        lines.append("")

        lines.append("## 助手")
        app = bt_state.app_path()
        binary = app / "Contents" / "MacOS" / "bt-helper"
        lines.append(f"  路径 = {app}")
        lines.append(f"  存在 = {app.is_dir()}    主二进制 = {binary.is_file()}")
        lines.append("  可执行位 = "
                     + (str(bool(binary.stat().st_mode & 0o111))
                        if binary.is_file() else "（没有二进制）"))
        lines.append(f"  会话节点 = {paths.BT_HELPER_SOCKET.name}"
                     "（动手那一拍才建，用完即删）")
        return "\n".join(lines)

    # ---------- 外部资源 ----------

    def resources(self) -> PluginResources:
        """本组件不写数据文件、不起守护、不请求 root。

        那个助手 ``.app`` 虽然是个可执行文件，但它**免特权**（以当前用户身份跑），
        所以**不进 A 类 root 二进制封闭集** —— 那套的定位是「框架授权、root 执行」，
        把它塞进去等于白送一份 root。
        """
        return PluginResources()

    # ---------- UI 贡献（声明点方法，决议 D20）----------

    def row_specs(self):
        """状态行只要一条 —— 整行的副标题就是判定结论。

        不摆开关：本组件的「自动化」开关在设置页（``auto_reconnect``）。行内只放开关会
        让人以为关掉它就不检测了，而实际上关掉只是不动手。
        """
        return (StatusRowSpec(key="main"),)

    def component(self):
        return ComponentSpec(icon=IconName.MOUSE)

    def state_specs(self):
        """``stale_after=None``：本组件没有节拍（按需型），两次体检之间隔几小时是正常的，
        按周期组件的口径判「失联」会误报。"""
        return (
            StateSpec(
                key="main", icon=IconName.MOUSE,
                states=STATES, initial="unknown",
                stale_after=None,
            ),
        )

    def settings_page(self):
        """设置页：选设备 ＋ 两个开关（界面在 ``settings.py``）。

        「要看的设备」必须在这里能选 —— 让用户手抄一串十六进制地址是没人会用的，
        而「配置项被代码真读、用户却只能手改 YAML」正是「截图识别」转正时修过的同款缺陷。
        """
        return SettingsPageSpec(
            build=lambda: settings_ui.build_page(self),
            reason="custom: 设备是「从已配对列表里挑一个」，需要读系统现场填下拉；"
                   "声明式渲染器至今未接线（contribution/form.py 零消费方）",
        )
