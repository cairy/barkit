"""core/plugin：插件基类与契约（方案 §3.1）。

契约：插件按**声明点**覆写具名方法（``component()`` / ``state_specs()`` /
``row_specs()`` / ``detail_entry()`` / ``settings_page()`` / ``actions()`` /
``hotkeys()``，决议 D20），返回对应的 Spec（``contribution/specs``）；
Spec 的 ``plugin_id`` 由框架在收集期注入，组件**一个字都不写**。
框架纯消费，零改框架文件。单一 ``contributions() -> list`` 入口已随 D20 删除
（签名即结构：拼错声明点 / 装错 Spec 在收集期报错并隔离，不再静默丢弃）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Protocol

from barkit.contribution.specs import (
    ActionSpec,
    ComponentSpec,
    DetailEntrySpec,
    HotkeySpec,
    SettingsPageSpec,
    StateSpec,
    StatusRowSpec,
)
from barkit.core.plugin_config import PluginConfig
from barkit.core.resources import PluginResources

if TYPE_CHECKING:
    from barkit.core.config import Config
    from barkit.core.events import EventBus
    from barkit.core.resident import ResidentService

log = logging.getLogger(__name__)

# A 类二进制来源的解析结果：({name: 源路径}, {name: 缺失原因})。
RootBinarySources = "tuple[dict[str, Path], dict[str, str]]"

# run_mode：框架要不要周期性叫我（契约 D10）
RUN_PERIODIC = "periodic"    # 常驻轮询：框架按 poll 间隔驱动 tick()
RUN_ON_DEMAND = "on_demand"  # 按需触发：没有节拍（从不轮询），只在「启动、事件、重试」时跑

# failure_policy：我失败时框架怎么办（契约 D11，见 §3.4）
FAIL_BREAK = "break"  # 连续失败达阈值熔断（标红 + 给出重试）
FAIL_LOG = "log"      # 只记日志、继续跑（只读刷新不该被阻断）


# ---------- 注入对象的形状（组件的调用面） ----------
#
# 下面三个 Protocol 描述的是**注入对象必须满足的最小调用面**，不是实现类的完整接口：
# 排队 / 去重 / 弹窗 / 会话状态机 / 变量求值全在各自那一侧，组件碰不到也不该碰。
#
# 为什么用 Protocol 而不是标注实现类：``core`` **不得** import ``shell`` /
# ``capabilities``（分层方向单向，门禁 ``test_layer_dependency_direction_core`` 守）。
# 依赖倒置在这里正合适 —— 由调用方（core）声明「我需要什么形状」，上层去满足它。
# 这几个 Protocol **不参与运行期 isinstance**：那样只查方法在不在、不查签名，会给读者
# 「签名也验过了」的错觉。运行期那一半由 :func:`verify_context` 按下表逐项检查，
# 静态那一半归类型检查器。


class InteractionPort(Protocol):
    """询问中枢的组件调用面（实为 ``shell.interaction.InteractionCenter``）。"""

    def register(self, spec, on_result=None, on_shown=None) -> None: ...
    def ask_now(self, spec, on_result=None) -> bool: ...


class HelperPort(Protocol):
    """与守护那条会话的组件调用面（实为 ``capabilities.helper_link.HelperLink``）。

    组件**永远不碰 socket、不拼协议帧、不认路径** —— 它只经这四个方法说话，而这四个
    又只被 :class:`Plugin` 的 ``declare`` / ``run_op`` / ``subscribe`` / ``observed``
    四个包装调用。
    """

    def declare(self, subject: str, payload, *, takeover: bool = False) -> bool: ...
    def run_op(self, op: str, params=None, callback=None) -> bool: ...
    def subscribe(self, subjects) -> bool: ...
    def snapshot(self, subject: str, default=None): ...


class VariablesPort(Protocol):
    """变量池的组件调用面（实为 ``core.variables.pool.VariablePool``）。

    只列**读口**：声明从哪来、怎么重载、何时重算是框架与设置页的事。
    """

    def value(self, var_id: str): ...
    def render(self, var_id: str) -> str: ...
    def interpolate(self, text: str) -> str: ...


#: 注入口 ⇄ 该口的契约形状。**只列静态类型为 ``object`` 的那三个**：其余字段
#: （``config`` / ``events`` 与那六个 ``Callable[...]``）本就带着可读的类型，
#: 再包一层 Protocol 只是把同一件事写两遍。
_PORT_FIELDS: "tuple[tuple[str, type], ...]" = (
    ("interaction", InteractionPort),
    ("helper", HelperPort),
    ("variables", VariablesPort),
)


def _port_members(port: type) -> "tuple[str, ...]":
    """Protocol 类体里声明的方法名（``__module__`` 那类自带属性按前缀滤掉）。

    自己数而不是用 ``typing.get_protocol_members``：那个口 3.13 才有，本仓不保证
    运行时就到那一档。判据也简单 —— 类体里不带下划线的函数就是成员。
    """
    return tuple(
        name for name, value in vars(port).items()
        if not name.startswith("_") and callable(value)
    )


def verify_context(ctx: "PluginContext") -> None:
    """装配期校验：**已接线**的注入口必须满足各自的 Protocol。

    ``None`` 一律跳过 —— 那是契约允许的降级态（测试桩 / 该路未起），不是错误。
    装配层在插件实例化之前调一次；接错对象当场抛，好过等到某组件的某条路径上
    静默降级（``getattr`` 探不到就当没接线）—— 那种「无声」正是本函数要消掉的。

    只查**方法在不在**，不查签名（``*args`` 那类运行期查不动，签名归类型检查器）。
    """
    for field, port in _PORT_FIELDS:
        obj = getattr(ctx, field, None)
        if obj is None:
            continue
        missing = tuple(
            name for name in _port_members(port)
            if not callable(getattr(obj, name, None))
        )
        if missing:
            raise RuntimeError(
                f"PluginContext.{field} 注入了不合格对象："
                f"{type(obj).__module__}.{type(obj).__name__} 缺少 {missing}，"
                f"该字段要求满足 {port.__name__}"
            )


@dataclass
class PluginContext:
    """注入给插件的运行时上下文（config + 事件总线 + 服务重算请求入口）。"""

    config: "Config"
    events: "EventBus | None" = None   # §3.1：daemon 状态经事件总线广播，UI 订阅渲染
    # §8.3.1 / 批次 5 后段：插件开关翻转后请求框架「重算所有插件的长驻服务集合」。
    # ctx 是全插件共享的单例，故回调**无参**、语义是全局重算（差量收敛对声明未变
    # 的插件是空操作）。托管权仍在框架：插件不能起停服务，只能请求重新评估。
    # 默认 None ⇒ 现有测试桩零改动。
    resync_services: "Callable[[], None] | None" = None
    # 「让某个长驻服务**现在**跑一轮」的入口（框架注入到
    # ``ResidentHost.wake``）。签名 ``(plugin_id, service_name) -> int``：ctx 全插件
    # 共享、拿不到调用者身份，故须显式带 id（与 ``mark_dirty`` 同款）；返回被唤醒的
    # 服务数，``0`` ＝ 没有匹配的在托管周期型服务（**不是错误**）。
    # 与 ``resync_services`` 分工：那个是「重算服务集合」（声明变了才用），这个是
    # 「集合不动，只提前一拍」——用户在界面上按「现在更新一下」走的是这条。
    # 默认 None ⇒ 现有测试桩零改动（Plugin.wake_service 退化为「叫不动」）。
    wake_service: "Callable[[str, str], int] | None" = None
    # 批次 4 · S4：A 类 root 二进制「**按声明解析来源**」的入口（框架注入）。
    # ctx 是全插件共享的单例，故回调无参、语义是全局重算（口径见
    # core.rootbin_reconcile：已安装插件并集，含 disabled / failed）。
    # 返回 ({name: 源路径}, {name: 缺失原因})；缺失项非空时安装器拒绝安装。
    # 默认 None ⇒ 现有测试桩零改动（Plugin.root_binary_sources 退化为空）。
    root_binary_sources: "Callable[[], RootBinarySources] | None" = None
    # §8.2.2 / 批次 10：事件 handler 的「按需点火」入口（框架注入到
    # ``Scheduler.mark_dirty``）。签名 ``(plugin_id, reason)``——ctx 是全插件共享
    # 单例，**拿不到调用者身份**，故须显式带 id；插件侧统一走
    # ``Plugin.mark_dirty(reason)`` 包装，不必自己填 id。
    # 默认 None ⇒ 现有测试桩零改动（Plugin.mark_dirty 退化为空操作）。
    mark_dirty: "Callable[[str, str], None] | None" = None
    # 通知中枢（实为 ``capabilities.notify.NotificationHub``，由装配层注入）。
    # 签名 ``(plugin_id, NotificationRequest) -> bool``：返回「是否真的发出去了」。
    # 这里标注成 ``object`` 而不是导入那个类：``core`` **不得**依赖 ``capabilities``
    # （依赖方向单向，见 capabilities/__init__ 的说明），而本字段只是转手透传，
    # 不读它的任何属性 —— 组件的调用面在 ``NotifyCapability.notify()`` 那一侧。
    # 默认 None ⇒ 现有测试桩零改动（组件发通知退化为「没中枢，记日志」）。
    notify: "Callable[[str, object], bool] | None" = None
    # 条件触发询问的中枢（实为 ``shell.interaction.InteractionCenter``，由装配层注入）。
    # 组件在 ``start()`` 里交一条 ``TriggerSpec``（何时问 + 问什么）＋ 一个答案回调，
    # 求值 / 去重 / 排队 / 弹窗**全在中枢**，组件不碰一个窗口。
    #
    # 类型是 :class:`InteractionPort`（调用面由 core 自己声明，故不必 import
    # ``shell``——分层方向单向）；实现类在 ``shell.interaction``。登记时交的 Spec 是
    # **纯数据**（``core.interaction`` 那一侧），组件读不到中枢的内部状态。
    # 默认 None ⇒ 现有测试桩零改动（未接线时规则登记退化为空操作）。
    interaction: "InteractionPort | None" = None
    # 与 root helper 的那条**会话**（实为 ``capabilities.helper_link.HelperLink``，
    # 由装配层注入）。2026-09-29 起它是「期望态下发 / 命令 / 事实订阅 / 存活」四条
    # 通道的唯一载体，取代了「写文件 ＋ touch 敲门 ＋ 目录监视」那一整套。
    #
    # 类型是 :class:`HelperPort`（同上，调用面由 core 声明，不 import
    # ``capabilities``）。组件的调用面在 ``Plugin.declare()`` / ``Plugin.run_op()`` /
    # ``Plugin.subscribe()`` / ``Plugin.observed()`` 四个包装上
    # —— 组件**永远不碰 socket、不拼协议帧、不认路径**。
    #
    # 默认 None ⇒ 现有测试桩零改动（未接线时三个包装退化为「记下但发不出去」）。
    helper: "HelperPort | None" = None
    # 「插件主动请求打开**自己的**详情窗」的出口（S18，由装配层注入到
    # ``shell.detail.DetailHost.open``）。签名 ``(plugin_id) -> bool``：ctx 是全插件
    # 共享单例、拿不到调用者身份，故须显式带 id（与 ``mark_dirty`` 同款）；返回
    # 「窗是不是开着」。
    #
    # 它解决的是**两个入口各开一份内容面**这件事：详情页从前只能由面板整行 / 快捷
    # 按钮打开，插件自己想开（热键、通知按钮、长驻服务结论）就只能另 new 一扇窗 ⇒
    # 两份内容面互不感知（一份看不到另一份刚做的改动）。有了这条出口，两条路收敛到
    # ``DetailHost`` 的同一份内容面上（S13）。
    #
    # 默认 None ⇒ 现有测试桩零改动（未接线时返回 False，调用方自己降级）。
    open_detail: "Callable[[str], bool] | None" = None
    # 变量池（实为 ``core.variables.pool.VariablePool``，由装配层注入）。
    #
    # 为什么是**框架注入**而不是组件自己 new 一个：值是全局共享的（设计文档 §2）——
    # 同一个「下班时间」喂给多条任务、也能喂给别的组件，而它由谁算、算一次还是每拍算
    # 一次，是框架的事。组件只负责「在这一个时刻，这个变量是多少」这一个问题。
    #
    # 类型是 :class:`VariablesPort`（同上）。组件的调用面只有**读口**
    # （``value`` / ``render`` / ``interpolate``）：声明从哪来、怎么重载是框架与
    # 设置页的事，组件只回答「在这一个时刻，这个变量是多少」。
    #
    # 默认 None ⇒ 现有测试桩零改动（未接线时引用变量的任务按「条件不满足」处理 ——
    # 与「变量还没值」同一条口径，不另造一种降级行为）。
    variables: "VariablesPort | None" = None


class Plugin:
    """插件契约基类，子类用类属性覆盖声明。"""

    id: str = ""
    name: str = ""
    run_mode: str = RUN_ON_DEMAND      # 框架要不要周期跑 tick()（契约 D10）
    failure_policy: str = FAIL_LOG     # tick 失败时熔断还是只记日志（契约 D11；两种模式都
                                       # 有意义 —— 按需型的 tick 同样会失败）
    # 建议轮询周期（秒，契约 D21）：框架据此定 ``plugins.<id>.poll`` 的默认值，
    # 用户仍可在配置里覆盖 —— **组件给的是建议，不是命令**（节流是用户对电池 /
    # CPU 的正当控制面）。``RUN_PERIODIC`` 组件必填（门禁
    # ``test_periodic_plugin_declares_suggested_poll`` 守）；``RUN_ON_DEMAND``
    # 组件**没有节拍**（2026-09-22 起它照样登记进调度器，为的是能被事件点火），
    # 填了也不生效、不报错，但别填 —— 一个不生效的节拍数字只会误导读者。
    # **声明的只是节拍数值**——谁调用、跑在哪、失败怎么办一概不变（调度权仍在框架）。
    suggested_poll: int | None = None
    required_permissions: list[str] = []
    order: int = 0                     # 新插件默认插入位（§6.4）：仅当 id 尚未进入
                                       # config.plugin_order 时生效，进入后即失效
    api_version: int = 3               # 插件契约版本；须与 contribution/points.SUPPORTED_API_VERSION
                                       # **精确相等**，否则被注册中心拒绝（区间放行已取消）
    # 组件**自身**版本（契约决议 D5）：自由格式字符串，如 ``"0.1.0"``。**只用于展示
    # 与「覆盖哪个版本」的提示**，不参与任何门禁 —— 契约兼容性归 ``api_version``。
    # 真源是插件包清单里的 ``version`` 字段（loader 读到后强制对齐本属性，与 id /
    # order 同款：清单是包元数据的权威）；类属性留着给内置组件与「清单没写」兜底。
    # 空串 = 未标注（不编造 ``"0.0.0"`` —— 那是把「不知道」写成一个看起来确定的版本号）。
    version: str = ""

    def __init__(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._cfg: "PluginConfig | None" = None

    # ---------- 配置 ----------

    @property
    def cfg(self) -> "PluginConfig":
        """本组件配置的 typed 门面：类型 / 默认值 / 取值域全部取自 ``config_schema()``。

        **读自己的配置一律走它**（``self.cfg.value("某键")``），别在调用点写
        ``self.plugin_config().get("某键", 兜底)`` —— 那个兜底是第二份默认值，
        与 schema 漂移时不报错（方案 §2）。框架所有的键（``enabled`` / ``poll`` /
        ``ui.*`` / ``states.*``）**不在**这里，各有唯一读侧，见那个方案的第 4 节。

        惰性给出：``config_schema()`` 是类级静态声明，但构造期取它会让
        「子类在 ``__init__`` 里改 schema」这类写法静默失效，故等到第一次取用。
        """
        if self._cfg is None:
            self._cfg = PluginConfig(self)
        return self._cfg


    def config_schema(self):
        """本插件的配置 schema（FormSchema，§9.5 默认值单一真源）。

        基类默认返回空 schema（无插件级字段）。子类覆盖声明自身的
        FieldSpec 列表；``defaults()`` 合成默认值，经 Config 注入后取代
        ``config.default.yaml`` 的对应插件段（卸载即随 schema 消失，无残留）。

        默认值归属：框架级（app.*/hotkeys.*/plugins.<id>.enabled）仍由 yaml 提供，
        其余字段一律在此声明，不得散落代码 fallback 或 yaml 插件段。
        """
        from barkit.core.schema import FormSchema

        return FormSchema(plugin_id=self.id)

    def plugin_config(self) -> dict:
        """本插件的配置段（已合并默认值）。"""
        return self.ctx.config.get(f"plugins.{self.id}", {}) or {}

    @property
    def enabled(self) -> bool:
        return bool(self.plugin_config().get("enabled", True))

    # ---------- 生命周期 ----------

    def start(self) -> None:
        """加载并启用时调用（daemon 不自起线程，tick 由 scheduler 驱动）。"""

    def stop(self) -> None:
        """停用/退出时调用。"""

    def on_disabled(self) -> None:
        """运行期被禁用（PluginManager.disable）时调用，用于停掉插件后端的真实副作用。

        默认空实现；如 route_guard 在此撤回路由意图，使 root helper 停止生效路由，
        让「UI 已禁用」与其实际行为一致。注意：本钩子仅由 manager.disable 触发，
        app 退出时的 ``registry.stop_all`` 调的是 ``stop``，不会误触后端清理。
        """

    def tick(self) -> None:
        """组件的执行体，跑在框架 worker 线程。

        两种模式都靠它干活：周期型由节拍驱动，按需型由「启动 / 系统事件 / 重试」驱动
        （2026-09-22 起按需型也登记进调度器，见 ``Scheduler.add_plugin``）。插件**不得**
        自己起线程 / 定时器 —— 执行时机一律归框架。
        """

    def mark_dirty(self, reason: str = "") -> None:
        """事件 handler 里「只标记脏」：请求框架跑一轮 tick（§8.2.2，批次 10）。

        事件 handler 在总线线程（主线程）执行，**不得**在其中做 IO / 子进程 /
        网络；把「有活要干」登记给调度器，由它在 worker 线程跑 ``tick()`` 完成
        实际工作。合并窗口内多次标记只点火一轮（事件风暴天然合并），
        ``reason`` 仅作诊断。框架未接线（测试桩 / 无调度器）时静默空转。

        用法（守护推来一份事实为例）：``on_observed`` 里
        ``self.mark_dirty("helper_observed")``，读现场与广播留在 ``tick()``。
        """
        cb = getattr(self.ctx, "mark_dirty", None)
        if callable(cb):
            cb(self.id, reason)

    def open_detail(self) -> bool:
        """请求框架打开**本组件**的详情窗（S18）；返回「窗是不是开着」。

        与 :meth:`mark_dirty` 同款「框架注入、组件只管用」：ctx 是共享单例、拿不到
        调用者身份，故这里填自己的 ``id``。窗的壳 / 标题 / 居中 / Dock 显隐 / 页脚
        「完成」**全归框架**（``shell.detail.DetailHost``），组件只交内容面
        （``detail_entry().build``）。

        **同一组件重复请求不会开第二扇窗**：``DetailHost`` 按 plugin_id 单例，
        已开着就把它提到前台（S13）。这正是「两个入口互不感知」的解法 —— 热键与
        面板整行打开的是**同一份**内容面，谁改的对方都看得见。

        ``False`` 有三层意思，调用方按需降级（**不是错误**）：框架没接线（测试桩）／
        本组件没声明 ``detail_entry()``／注册中心里没这一条（组件被禁用或卸载）。
        """
        cb = getattr(self.ctx, "open_detail", None)
        if not callable(cb):
            return False
        try:
            return bool(cb(self.id))
        except Exception:  # noqa: BLE001 —— 开不了窗不该拖垮按下热键那一下
            log.exception("插件 %s 请求打开详情窗失败", self.id)
            return False

    def wake_service(self, name: str = "") -> int:
        """请求框架让本组件声明的长驻服务**现在**跑一轮（不等这一拍走完）。

        与 :meth:`mark_dirty` 同款「框架注入、组件只管用」：ctx 是共享单例、拿不到
        调用者身份，故这里填自己的 ``id``；线程与时钟仍在框架侧（``core/resident``），
        本方法只把「有活要干」提前一拍。

        返回被唤醒的服务数。``0`` **不是错误**：组件可能被禁用（服务不在托管）、
        服务可能已熔断、也可能真的没有这个名字的周期型服务。调用方**不得**把 0 当成
        「已经跑过了」——它要退化到「等下一拍」（例如界面文案就别再说「立刻」）。

        ``name`` 留空 ＝ 唤醒本组件的全部周期型服务。
        """
        cb = getattr(self.ctx, "wake_service", None)
        if not callable(cb):
            return 0
        try:
            return int(cb(self.id, name) or 0)
        except Exception:  # noqa: BLE001 —— 叫不动服务不该拖垮调用它的那条路
            log.exception("插件 %s 唤醒长驻服务 %r 失败", self.id, name)
            return 0

    # ---------- 状态广播 / 敲门（阶段 0 上提，规范 §6.2） ----------
    # 这三件在四个 daemon 插件里逐字重复（publish ×4 / status_topic ×4 /
    # touch_trigger ×3），收归基类。它们都是「框架保证语义」的东西（发布边界、
    # 敲门语义），故归**基类**、不做成自由函数（判据见规范 §6.1）。

    def status_topic(self, key: str = "main") -> str:
        """本插件状态 topic：``plugin.<id>.status.<key>``（``key`` 缺省 ``"main"``）。

        与 ``StateSpec.topic`` **同源**（同一套拼法），故不必在各处手写字符串。
        ``key`` 是该插件内状态机的名字；只有一台状态机时省略即可——此时框架约定的
        保留键是 **``"main"``**。多台状态机的组件（如 ``display`` / ``notify`` /
        ``automation``）按 ``key`` 分别取 topic。

        为什么缺省是 ``"main"`` 而不是 ``StateSpec`` 的 ``key or 'default'``：
        框架自动发布路径（``scheduler._publish_status``）对「tick 返回单份快照」
        的情形本就发到 ``"main"``，且全仓 5 条真实 ``StateSpec`` 全部显式写了
        ``key="main"``——``'default'`` 是从未被触发的失活分支。若此处取 ``'default'``，
        手动即时广播与框架自动发布会落到**两条不同 topic** 上，订阅方只看到其中一条。
        因此保留键统一为 ``"main"``，``StateSpec.topic`` 的兜底值同步对齐。

        刻意做成**方法**而非只读属性：契约 §2.2 的 topic 形态本就是
        ``plugin.<id>.status.<key>``，一台机时取保留键、多台机时带 key，
        一个方法覆盖两种形态；写成属性则必然要为多状态机再加一个平行入口，
        等于同一件事两个写法。
        """
        return f"plugin.{self.id}.status.{key}"

    def publish_snapshot(self, snapshot=None, key: str = "main") -> None:
        """把**自身**状态广播到**自身** status topic（B 契约）。

        适用场景：用户手动操作后需要立刻把新状态反映到界面（轮询要等下一拍）。
        周期兜底仍归框架（``tick()`` 返回非 None 时框架自动 publish），本方法
        只是「即时」那条路径。

        ``key`` 缺省 ``"main"``，与 ``status_topic()`` 及框架自动发布路径取同一个
        保留键——两处默认值**必须同源**，理由见 :meth:`status_topic`。

        **边界**（门禁 ``test_plugin_publishes_only_own_status_snapshot`` 守）：
        只能发自身 topic、只能发 ``StateReport``，不得发裸 dict、不得发他人 topic。

        ``snapshot=None`` 时调用子类的私有快照工厂 ``_snapshot()``——这是四个
        daemon 插件共有的写法，保留它使调用点保持无参（``self.publish_snapshot()``），
        迁移零语义变化。子类两样都没给时**记日志后返回**：这是组件写错了（漏实现
        快照工厂），不该静默空转让人以为「已经广播过了」。
        """
        if snapshot is None:
            factory = getattr(self, "_snapshot", None)
            if not callable(factory):
                log.warning(
                    "publish_snapshot()：%s 既没传 snapshot 也没有 _snapshot()，本轮未广播",
                    self.id,
                )
                return
            snapshot = factory()
        events = getattr(self.ctx, "events", None)
        if events is not None:
            events.publish(self.status_topic(key), snapshot)

    # ---------- 与守护的会话（2026-09-29 起，替代「写文件 ＋ 敲门」） ----------
    #
    # 从前这里还有一个 ``touch_trigger()``（touch 一个 ``.trigger`` 文件、让守护立刻
    # 跑一轮）：会话通道落地后守护只认帧、不认任何敲门文件，它**整份删掉**了 —— 不留
    # 空实现，因为一个什么都不做的同名方法会让「我敲过门了」继续出现在调用点与注释里。
    # 要「立刻收敛」用 :meth:`declare`（声明期望态，守护收到即跑），要「现在做一次」用
    # :meth:`run_op`。
    #
    # 三个包装，组件**永远不碰 socket、不拼协议帧、不认路径**：路径（会话节点）
    # 在框架侧解析，协议在 ``capabilities/helper/protocol`` 侧定义。
    # 未接线（测试桩 / 装配层还没起会话）时：``declare`` 记不下（没地方记）、
    # ``run_op`` 就地回调失败、``subscribe`` 无声 —— 都**如实**，不假装成功。

    def declare(self, subject: str, payload) -> bool:
        """声明某 ``subject`` 的期望态（``payload=None`` ＝ 撤回）。返回**是否已送达**。

        ``subject`` 是**领域名**（``route`` / ``tunnel`` / ``hosts``），不是文件路径
        —— 组件声明领域，路径永远不出框架。幂等：同一内容重复调用不会重复上线。
        未连接时返回 ``False``，但期望态已在框架侧记下，守护一起来会自动补发。
        """
        link = getattr(self.ctx, "helper", None)
        declare = getattr(link, "declare", None)
        if not callable(declare):
            return False
        try:
            return bool(declare(subject, payload))
        except Exception:  # noqa: BLE001 —— 会话异常不得拖垮组件
            log.exception("插件 %s 声明 %s 失败", self.id, subject)
            return False

    def run_op(self, op: str, params=None, callback=None) -> bool:
        """发一条**一次性命令**（``route.renew`` / ``route.release``），回帧时回调。

        这就是旧的「命令塞进期望态顶层 ``ops`` ＋ 靠 ``seq`` 水位线配对回执」的替代：
        一帧一命令、回帧即回执，**没有水位线、不跨重启留状态**。失败当场可见
        （``callback(False, None, 原因)``），不再靠「去状态文件里找有没有回执」。
        """
        link = getattr(self.ctx, "helper", None)
        send = getattr(link, "run_op", None)
        if not callable(send):
            if callable(callback):
                callback(False, None, "会话未接线")
            return False
        try:
            return bool(send(op, params, callback))
        except Exception:  # noqa: BLE001
            log.exception("插件 %s 发送命令 %s 失败", self.id, op)
            if callable(callback):
                callback(False, None, "发送失败")
            return False

    def subscribe(self, subjects) -> bool:
        """订阅这些 ``subject`` 的最新事实（服务端收到后**立刻推一份当前的**）。

        取代旧的「声明文件路径 ＋ 框架监视目录」：不再有「监视谁」这件事，
        也不再需要组件各自的兜底 ``tick()`` 去读状态文件。
        """
        link = getattr(self.ctx, "helper", None)
        sub = getattr(link, "subscribe", None)
        if not callable(sub):
            return False
        try:
            return bool(sub(list(subjects)))
        except Exception:  # noqa: BLE001
            log.exception("插件 %s 订阅失败", self.id)
            return False

    def observed(self, subject: str, default=None):
        """某 ``subject`` 的**最新事实**（内存里那份，由推送喂）。

        取代旧的 ``_read_state_file()``：不再有「文件存不存在 / mtime 新不新 /
        里面那个时刻戳是不是此刻的」这三问 —— 事实是推过来的，随变的。
        没收到过返回 ``default``（＝「还没事实」，**不等于**「一切正常」）。
        """
        link = getattr(self.ctx, "helper", None)
        get = getattr(link, "snapshot", None)
        if not callable(get):
            return default
        try:
            return get(subject, default)
        except Exception:  # noqa: BLE001
            return default

    # ---------- 贡献（声明点方法，决议 D20；子类只覆写自己有的声明点） ----------
    #
    # 「一个声明点一个具名方法」取代旧的单入口 ``contributions() -> list``：
    # 签名即结构（返回类型 / 基数写进契约），拼错声明点或装错 Spec 在收集期报错
    # 并隔离该组件，不再静默丢弃。Spec 的 ``plugin_id`` 由框架在收集期注入。
    # 对应表（声明点 ⇄ Spec 类）的真源在 ``contribution/points.declaration_points()``。

    def component(self) -> "ComponentSpec | None":
        """身份图标（0/1）：标题栏图标条一枚，也是状态行图标盒的主位来源。

        与状态无关；组件名省略时框架取 ``Plugin.name``（身份唯一真源，决议 D14）。
        """
        return None

    def state_specs(self) -> "tuple[StateSpec, ...]":
        """状态机声明（n）：每台机一个 ``StateSpec``，取值闭集在 ``states`` 里。

        **声明顺序即作者写的关注度顺序**（代表态平局取先声明者，决议 D1），
        返回元组时别打乱自己写下的次序。
        """
        return ()

    def row_specs(self) -> "tuple[StatusRowSpec, ...]":
        """状态面板行（n）：每行一个 ``StatusRowSpec``，按 identity 复用渲染。"""
        return ()

    def detail_entry(self) -> "DetailEntrySpec | None":
        """详情页入口（0/1）：独立模态卡的高频任务流；未声明则整行不响应点击。"""
        return None

    def settings_page(self) -> "SettingsPageSpec | None":
        """设置窗分页（0/1）：低频配置；``build`` 与 ``schema`` 至少给一个。"""
        return None

    def actions(self) -> "tuple[ActionSpec, ...]":
        """面板底部操作区按钮（n）。"""
        return ()

    def hotkeys(self) -> "tuple[HotkeySpec, ...]":
        """全局热键（n）：配置键 ``hotkeys.<name>``，用户可覆盖键位。"""
        return ()

    def diagnostics_text(self) -> "str | None":
        """诊断文本（0/1）：一段可粘贴给维护者的纯文本，框架的「复制诊断」用它。

        **声明分界与别处同款 —— 有没有内容决定画不画那枚按钮**：返回 ``None`` / 空串 =
        本组件没有诊断可给，框架就不画「复制诊断」（不可能画一个点了只能得到「没东西」的
        按钮；判据由框架按「覆没覆写本方法」静判定，不会为了画界面先跑一次诊断）。
        「打开数据目录」不在此列：它与组件无关，框架恒给（``shell/data_ops.py``）。

        **只读**（同 ``tick()`` 与拨测的纪律）：框架在 UI 线程调用它，不得改系统状态、
        不得起子进程干活 —— 用户点「复制诊断」的场景是他刚发现不对，要的是**此刻**的
        事实，不是画设置页那一刻的。生成失败由框架兜住（记日志 + 一句回执），不冒泡。

        时间戳与版本号由**组件**自己排进文本（它的排版层最清楚该报哪些事实）。
        框架只负责两件与组件无关的事：放进剪贴板、说一句回执。
        """
        return None

    # ---------- 可插拔协议（方案 §9.1，批次 6a） ----------

    def resources(self) -> PluginResources:
        """声明本插件持有的全部外部资源（数据文件 / 种子 / 系统变更 / root 二进制）。

        框架据此做彻底卸载与 A 类对账（批次 4）。默认空清单 = 无外部资源。
        """
        return PluginResources()

    def resident_services(self) -> list[ResidentService]:
        """声明本插件的长驻服务（方案 §8.3.1 / 批次 5 后段）。

        线程 / 时钟 / 起停 / 销毁全归框架，插件只给声明。默认空清单 = 无常驻资源。

        **硬约束**（框架按 ``name`` 做差量收敛，会反复调用本方法）：
        - 必须廉价、无副作用（不得在此起线程 / 开端口 / 读盘）；
        - 同一实例上每次调用必须返回**同一批对象**（在 ``__init__`` 里建好并持有
          引用，不要每次新建），否则框架无法按 ``name`` 判定「同一服务」。
        """
        return []

    def root_binary_sources(self) -> "RootBinarySources":
        """本机全部已安装插件声明的 A 类二进制来源（框架注入，批次 4 · S4）。

        返回 ``({name: 源路径}, {name: 缺失原因})``。安装器只部署前者；后者非空
        即**拒绝安装**（不做静默降级）。源路径由框架按声明解析
        （``<声明插件的包目录>/resources/<name>``），插件不得自行拼路径。

        ctx 未接线（轻量测试桩）时返回空 —— 安装器据此走「无 A 类声明」路径，
        **绝不猜测源路径**（猜错等于让 root 执行一个非预期文件）。
        """
        provider = getattr(self.ctx, "root_binary_sources", None)
        if not callable(provider):
            return {}, {}
        try:
            return provider()
        except Exception:  # noqa: BLE001 —— 解析失败不得拖垮安装流程的其余部分
            log.exception("插件 %s 解析 A 类二进制来源失败", self.id)
            return {}, {}

    def subjects(self) -> list:
        """声明本插件要哪几个**领域**的最新事实（2026-09-29 通道换成会话）。

        它取代的是 ``file_watches()``：从前插件声明「监视哪个文件路径」、框架用
        ``QFileSystemWatcher`` 盯那个目录；现在插件声明的是**领域名**（``route`` /
        ``tunnel`` / ``hosts``），框架把这份声明交给会话（``subscribe``）—— 守护收到
        即推一份当前的，此后有变化再推。路径彻底不再出现在组件契约里。

        「事实从哪来」也一并变了：从前组件在 ``tick()`` 里自己读状态文件、还要判
        「文件在不在 / mtime 新不新 / 里面那个时刻戳是不是此刻的」；现在事实是
        **推过来的**（:meth:`observed` 取），那三问没有对象了，``tick()`` 只留给
        本来就没有推送的来源（系统 API 轮询）。

        **硬约束**：必须廉价、无副作用（框架会反复调用）；只声明**真实存在**的领域名
        —— 声明了却没人推不会报错，只是永远收不到事实。默认空清单 = 不订阅。
        """
        return []

    def on_observed(self, subject: str, observed) -> None:
        """守护推来一份事实（**§8.2.2 handler 契约：只许「更新内存 + mark_dirty()」**）。

        ``subject`` 是 ``subjects()`` 里声明过的领域名（框架按声明过滤，插件不必再判
        归属）；``observed`` 是守护侧那份观测的副本（可能是空 dict ＝「还没有事实」，
        **不等于**「一切正常」）。历史与完整取值走 :meth:`observed`，不靠这个参数传递
        —— 参数只是「变了」这件事的通知。

        跑在总线所在线程（主线程），**不得**在这里读盘 / 起子进程 / 发网络请求 ——
        那会卡住 UI 线程；实际工作交给 ``tick()``。
        """
        return None

    def intent_subject(self) -> str:
        """本组件的**期望态**挂在哪个 ``subject`` 上（空串 = 本组件不声明期望态）。

        与 :meth:`subjects` 不是一回事：那个说的是「我要看哪些事实」（订阅、只读），
        这个是「我往哪个领域写意图」。三者今天恰好同名（``route`` / ``tunnel`` /
        ``hosts``），但它们是两件事，别合并 —— 将来完全可能有一个只读的观察者。

        要这个声明的只有一处：「立即检查」要按它让守护**现在**跑一轮（``resync``
        命令）。判断「守护有没有在收敛」的输入就是那份意图，而这份意图挂在哪，只有
        组件自己知道。
        """
        return ""

    def event_topics(self) -> list:
        """声明本插件关心的**系统事件 topic**（§8.3.1 通用订阅）。

        与 ``subjects()`` 同款「只声明不接线」：框架把声明的 topic 订阅到统一
        入口，事件到达时转交 ``on_system_event(topic, payload)``；插件禁用 / 卸载时
        框架反订阅（§8.2.4）。

        为什么要有它：系统事件（唤醒 / 外设 / 网络 / 屏幕）此前只有「文件变化」一条
        收归了框架，「插件不各自接系统 API」这条规矩在其余几类上无处落地 —— 组件想
        知道「机器刚醒过来」就只能自己起线程或轮询。有了这个声明点，新事件源（如
        ``core/power`` 的 ``system.power.wake``）一落地，任何组件都能声明消费。

        **硬约束**：必须廉价、无副作用（框架会反复调用）；只声明**真实存在**的 topic
        —— 声明了却没人投递不会报错，只是永远收不到事件。默认空清单 = 不订阅。
        """
        return []

    def on_system_event(self, topic: str, payload) -> None:
        """框架投递的系统事件（§8.2.2 同款契约：**只许「更新内存 + mark_dirty()」**）。

        ``topic`` 是 ``event_topics()`` 里声明过的名字（框架按声明过滤，插件不必再判
        归属）；``payload`` 是该事件源的载荷对象（如 ``core.power.PowerEvent``）。

        跑在总线所在线程（主线程），**不得**在这里读盘 / 起子进程 / 发网络请求 ——
        那会卡住 UI 线程；实际工作交给 ``tick()``。
        """
        return None

    def teardown(self) -> None:
        """卸载时调用（仅 PluginManager.uninstall 触发）：复原本插件造成的系统状态。

        与 ``on_disabled``（禁用时停后端副作用，可逆）不同，``teardown`` 面向
        **不可逆的卸载**：撤路由 / 删 hosts 托管块 / 拆隧道 / 停子进程。
        默认空实现；只有改过系统状态的插件需要覆盖。
        """

    def export_data(self, include_secret: bool = False) -> dict:
        """序列化本插件全部用户数据（导出包 config.json 的内容来源）。

        默认实现 = 本插件配置段（合并层，剔除 ``enabled``）。不得含绝对路径与
        机器相关值。含敏感字段（如 autotype 的明文文本）的插件**必须覆盖**本方法，
        按 ``include_secret`` 决定是否包含（默认排除——方案待决 7：从源头降低
        暴露面）；框架按此签名约定传参。
        """
        data = dict(self.plugin_config() or {})
        data.pop("enabled", None)
        return data

    def import_data(self, payload: dict) -> None:
        """从导出包恢复（框架保证在插件 stop 状态调用）。

        默认实现 = 把 payload 写回本插件配置段（经框架写盘路径，原子写）。
        与 ``export_data`` 对称，多数插件无需覆盖。
        """
        if not isinstance(payload, dict):
            return
        section = {k: v for k, v in payload.items() if k != "enabled"}
        setter = getattr(self.ctx.config, "set_plugin_section", None)
        if callable(setter):
            setter(self.id, section)
