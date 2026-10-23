"""插件脚手架模板 · **守护型**骨架（以 ``_`` 开头，发现器不加载，仅作示例 / 复制源）。

**复制时请把本文件改名为 ``plugin.py``**——发现器只认「直属子包 + 入口模块名
``plugin.py`` + 名为 ``Plugin`` 的类」三件事，缺一即插件不存在（规范 §2.1）。
若要做的是「按需触发、无常驻轮询」的插件，用同目录 ``plugin.py``（动作型）作起点。

本骨架刻意**不含 UI**：守护型插件的常态是「无界面后台收敛 + 状态面板一行」。
需要设置页 / 详情页时另建 ``settings.py``；PySide6 只许出现在 UI 模块
（``plugin.py`` 内出现 PySide6 import 会被 ``test_plugin_py_has_no_pyside6`` 拦下）。

演示的能力（守护型清单，§10.1）：

- ``RUN_PERIODIC`` + ``FAIL_BREAK`` + ``tick()``：轮询体跑在框架 worker 线程；
- ``resident_services()``：声明长驻服务，**线程 / 时钟 / 起停 / join 全归框架**
  （插件侧禁 ``threading`` / ``QTimer`` / ``QThread`` / ``time.sleep``，门禁守着）；
- ``subjects()`` + ``on_observed()``：**守护推来事实**（2026-09-29 通道换成会话之后）。
  组件只声明「我要哪几个领域」，框架把订阅交给会话；推送到达时转交 handler，handler
  只许「更新内存 + ``mark_dirty()``」，读现场留给 ``tick()``（§8.2.2）。**路径不在
  组件契约里** —— 从前那套 ``file_watches()`` 声明文件、自己读状态文件、还要判
  「文件在不在 / mtime 新不新」的写法整份没有了。
- ``declare()`` / ``run_op()``：往下达期望态、发一次性命令（也走会话，不写文件）。
- ``resources()``：数据文件 / 种子 / helper task 声明（框架据此彻底卸载 + A 类对账）；
- ``AutomationCapability``：``automation_active`` / ``set_automation_active`` /
  ``mode_badge_spec``（自动化运行态 + 模式徽标，与「是否常驻」正交）；
- ``observed()`` 取守护报来的事实，以及手动操作后的即时广播 ``publish_snapshot()``。
"""

from __future__ import annotations

import logging
import time

from barkit.capabilities.automation import AutomationCapability
from barkit.contribution.specs import (
    ComponentSpec,
    HotkeySpec,
    IconName,
    StateLevel,
    StateOption,
    StateReport,
    StateSpec,
    StatusRowSpec,
    SwitchSpec,
)
from barkit.core.plugin import FAIL_BREAK, RUN_PERIODIC
from barkit.core.resources import PluginResources
from barkit.core.schema import FieldSpec, FieldType, FormSchema

log = logging.getLogger(__name__)

STALE_S = 15  # 失联阈值：轮询间隔的若干倍余量（守护进程挂了要能看出来）

#: 本插件订阅的**领域名**（占位）。真实可订阅的领域名是**闭集**
#: ``route`` / ``tunnel`` / ``hosts`` —— 那是 root helper 侧真正会推的东西（一个领域
#: 一份事实，名字与文件路径无关）。第三方插件若订阅闭集外的名字，守护照样接受、
#: 只是永远推空 —— 所以这里写占位名时必须知道自己在示范形状，不是在申请一个新领域。
SUBJECT = "demo"

#: 取值闭集（决议 D13）：取值名 / 级别 / 默认文案只写一次，报告只报取值名。
#: 复制本模板时按自己的业务把取值列全 —— 数值在这里，界面上才有颜色可画。
STATES: tuple[StateOption, ...] = (
    StateOption("unknown", StateLevel.IDLE, text="尚未读取"),
    StateOption("waiting", StateLevel.IDLE, text="等待守护"),
    StateOption("converged", StateLevel.OK, text="已收敛"),
)


class _DemoPoller:
    """长驻服务示例（**周期型**）。

    宿主每隔 ``interval`` 秒在托管线程里调一次 ``poll()``；``interval is None`` 则是
    「自持循环型」——宿主直接跑 ``run()``，由 ``run()`` 自己阻塞到 ``stop()`` 被调用。

    三条硬约定：
    - ``name`` 插件内唯一，宿主按它做差量收敛；
    - 服务内**不得** import ``threading`` / 起任何定时器——节律只声明在 ``interval`` 里；
    - ``stop()`` 幂等、非阻塞，只「请求退出」，``join`` 归宿主（周期型 ≤ 单次 poll 耗时）。
    """

    name = "demo_poller"
    interval: float | None = 5.0

    def __init__(self, on_poll) -> None:
        """``on_poll``：每次心跳要做的事，由插件注入（服务不认识插件）。"""
        self._on_poll = on_poll
        self._stopped = False

    def run(self) -> None:
        """自持循环型入口。周期型不用实现（宿主跑的是 :meth:`poll`）。"""

    def poll(self) -> None:
        if not self._stopped:
            self._on_poll()

    def stop(self) -> None:
        self._stopped = True


class Plugin(AutomationCapability):
    id = "template_daemon"
    name = "模板插件（守护型）"
    run_mode = RUN_PERIODIC
    failure_policy = FAIL_BREAK   # 有写入/执行动作 → 连续失败达阈值熔断；只读刷新用 FAIL_LOG
    suggested_poll = 5            # 建议周期（契约 D21）：框架据此定 ``plugins.<id>.poll``
                                  # 的默认值，用户仍可覆盖 —— 组件给的是建议，不是命令。
                                  # RUN_PERIODIC 组件**必填**；取值与下面 config_schema()
                                  # 里 ``poll`` 的默认保持一致（说明书示范）
    api_version = 3           # 须与 contribution/points.SUPPORTED_API_VERSION **精确相等**
    order = 91

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        # 长驻服务实例必须在 __init__ 里建好并持有：resident_services() 会被框架反复
        # 调用，每次必须返回**同一批对象**，否则宿主无法按 name 判定「同一服务」（§8.3.1）。
        self._poller = _DemoPoller(self._on_poll)
        self._cycles = 0

    # ---------- 与守护的会话（不写文件、不读文件）----------

    def _observed(self) -> dict:
        """取守护报来的那一份事实。

        ``observed()`` 读的是**会话推到内存里的**最新观测，没收到过返回兜底值。
        这里给兜底值而不是「假装一切正常」：面板要说的是「还没事实」，不是「已收敛」。
        从前那一套（``read_json`` ＋ ``mtime`` 判缓存）整份没有了 —— 事实是推过来的，
        「文件在不在 / mtime 新不新 / 里面那个时刻戳是不是此刻的」三问都没有对象了。
        """
        state = self.observed(SUBJECT, None)
        if isinstance(state, dict):
            return state
        return {"error": "守护尚未回报（未连接或未运行）"}

    # ---------- 状态快照 ----------

    def _snapshot(self) -> StateReport:
        """把自身状态映射为 ``StateReport``（面板行 / 角标 / 托盘徽标共用的单一真相）。

        取值闭集见模块顶的 ``STATES``：**级别归声明**（决议 D13），这里只回答
        「现在是哪一格」，不重复给 level。

        ``summary`` 是**唯一文案**（决议 D18）：面板行副标题与两处悬停提示共用这一句，
        没有第二个文案字段可放补充说明——要说的都并进这一句。
        """
        state = self._observed()
        if "error" in state:
            return StateReport(
                "waiting", f"等待守护 · {state['error']}", updated_at=time.time(),
            )
        note = str(state.get("note", ""))
        return StateReport(
            "converged",
            f"已收敛 {self._cycles}" + (f" · {note}" if note else ""),
            updated_at=time.time(),
        )

    # ---------- 自动化开关（AutomationCapability）----------

    @property
    def automation_active(self) -> bool:
        """自动化是否运行中。False = 已暂停 / 手动接管，状态完全由手动开关决定。

        取值走 ``self.cfg``：下面 ``config_schema()`` 里 ``auto_enabled`` 那条
        ``FieldSpec`` 是**默认值的唯一真源**，调用点只报键名。
        """
        return bool(self.cfg.value("auto_enabled"))

    def set_automation_active(self, enabled: bool) -> None:
        """切换自动化：写用户配置段（**零授权**）+ 施加领域副作用。

        写配置走 ``self.cfg.set``（读-改-写单键，§6.3）—— 不要自己读用户 YAML 再
        ``save_user``：那正是阶段 0 收口掉的 18 处散点，且会把「单个嵌套键」的改写
        退化成「整段覆盖」。**等于声明默认值即删键**（用户 YAML 是差异层），
        所以这里不写 ``section["auto_enabled"] = ...`` 那种把默认值也物化下去的写法。
        """
        enabled = bool(enabled)
        if self.automation_active == enabled:
            return

        self.cfg.set("auto_enabled", enabled)
        self.publish_snapshot()   # 即时广播；不等下一拍 tick

    # ---------- 生命周期 ----------

    def tick(self) -> StateReport:
        """每轮轮询体（worker 线程，由 scheduler 驱动；插件不起线程也不 sleep）。

        返回非 ``None`` 的 ``StateReport``，框架自动 publish 到
        ``plugin.<id>.status.main``——这是**周期兜底**那条路；手动操作的即时广播
        走 ``self.publish_snapshot()``，两条路同一个 topic（基类保证同源）。

        ``FAIL_BREAK`` 下异常要**抛出去**：scheduler 计失败，连续失败熔断；
        ``FAIL_LOG`` 的只读刷新则自己吞掉并记日志即可。
        """
        self._cycles += 1
        return self._snapshot()

    def _on_poll(self) -> None:
        """长驻服务每次心跳做的事（托管线程里执行，不要碰 UI）。"""
        log.debug("%s 心跳 #%d", self.id, self._cycles)

    # ---------- 声明：长驻服务 / 订阅的领域 / 外部资源 ----------

    def resident_services(self) -> list:
        """声明长驻服务。必须廉价、无副作用，且每次返回同一批对象（见 ``__init__``）。"""
        return [self._poller]

    def subjects(self) -> list:
        """声明要哪几个**领域**的最新事实（不是文件路径）。

        框架把这份声明交给会话（订阅即立刻推一份当前的），推送到达时转交
        ``on_observed()``。**没有「监视哪个文件」这件事了** —— 也不要再自己读状态文件：
        那正是 2026-09-29 拆掉的那条通道。
        """
        return [SUBJECT]

    def on_observed(self, subject: str, observed) -> None:
        """守护推来一份事实（跑在总线线程 = 主线程）。

        **只许「更新内存 + ``mark_dirty()``」**：读盘 / 子进程 / 网络一律禁止，
        否则会卡住 UI 线程。实际工作留给 ``tick()``——事件路径与轮询路径汇入同一入口。
        事实本身不必在这里存：完整取值走 ``observed()``，参数只是「变了」的通知。
        """
        if subject == SUBJECT:
            self.mark_dirty("helper_observed")

    def resources(self) -> PluginResources:
        """声明本插件持有的外部资源（框架据此做彻底卸载 + A 类对账）。

        - ``data_files`` 相对 ``paths.APP_SUPPORT``（用户域），**不得**含绝对路径。
          **默认零文件**：新组件要落盘必须在这里显式声明，并答得出「丢了以后用户会看见
          与事实不符的东西吗、或被重复打扰吗」；答不上来的（通道 / 派生 / 缓存）一律
          留内存。所以本骨架的 ``data_files`` 是空的 —— 这是**默认**，不是偷懒。
        - ``seed_files`` ⊆ ``data_files``：随包提供的初始内容，卸载**保留**（已是用户数据）——
          分类依据是**声明**，不是运行期检测文件有没有被改过；
        - ``helper_tasks`` 给 root helper 的任务名（任务表红线：``subject`` 取自闭集
          ``route`` / ``tunnel`` / ``hosts``、≤8 条、禁内联 ``params``；未声明的 task
          会被显式 ``enabled: false`` 顶掉内置默认）。
        """
        return PluginResources(
            data_files=[],
            seed_files=[],
            config_section=self.id,
            hotkeys=["template_daemon_toggle"],
            helper_tasks=[],
        )

    # ---------- 贡献（声明点方法，决议 D20） ----------

    def row_specs(self):
        """守护型通常只贡献一行状态；徽标由 AutomationCapability 统一提供。"""
        return (
            StatusRowSpec(
                key="main",
                controls=(
                    SwitchSpec(
                        checked=self.automation_active,
                        on_toggle=self.toggle_automation,
                        get_checked=lambda: self.automation_active,
                    ),
                ),
                # 模式徽标（自动 / 手动）：由 AutomationCapability 统一提供，插件不持 widget
                mode=self.mode_badge_spec(),
            ),
        )

    def component(self):
        """身份的唯一真源（D14）：名字省略取 ``Plugin.name``，不要再写一遍。

        **前提：你有状态机**（◷ 2026-09-17）—— 这一格画的是代表态的取值图标 ＋
        级别色；没有状态机就不声明它（细节见 ``_template/plugin.py`` 同方法）。
        """
        return ComponentSpec(icon=IconName.SHIELD)

    def state_specs(self):
        """状态声明（静态）：取值闭集见 ``STATES``。"""
        return (
            StateSpec(
                key="main", icon=IconName.SHIELD,
                states=STATES, initial="unknown",
                stale_after=STALE_S,
            ),
        )

    def hotkeys(self):
        """全局热键。托盘角标不需要在这里声明：每个取值自己说「值不值得提醒」
        （``StateOption.alerts``，WARN / ERR 默认即为真，决议 D17）。"""
        return (
            HotkeySpec(
                name="template_daemon_toggle", label="暂停/恢复自动化",
                default_combo="", callback=self.toggle_automation,
            ),
        )

    def config_schema(self) -> FormSchema:
        """本插件的配置 schema（**默认值单一真源**，§9.5）。

        这里声明的字段由 ``FormSchema.defaults()`` 合成默认值并经 Config 注入，
        取代 ``config.default.yaml`` 的对应插件段（卸载即随 schema 消失，无残留）。
        插件内 ``.get(key, default)`` 的 fallback 只是防御性写法，真源在此。

        **别在这里声明 ``poll``**：框架节拍（＝设置页页尾「运行间隔」卡那个下拉）由
        **框架**按 ``run_mode`` ＋ ``suggested_poll`` 自动生成控件并写
        ``plugins.<id>.poll``（契约 D21），组件再声明一个同名字段就等于开了第二条覆盖
        通道 —— 两处都写、界面上只看得到一处。门禁 ``test_no_plugin_declares_framework_poll_field`` 守。
        """
        return FormSchema(
            plugin_id=self.id,
            version=1,
            fields=(
                FieldSpec(
                    key="auto_enabled", label="自动化（自动收敛）",
                    type=FieldType.BOOL, default=True,
                ),
            ),
        )
