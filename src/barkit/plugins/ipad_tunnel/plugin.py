"""ipad_tunnel：iPad 全流量隧道（方案见本目录 design.md；批次 5 v3 迁移）。

M1 数据层：app 侧**声明隧道意图**（零授权）+ 消费守护推来的观测刷面板。
M2 特权操作：复用 route_guard 的 root helper（tunnel.sync 原语，HELPER_VERSION 见 helper 侧）。
M3 接线（批次 5 后段起）：bridge.py 的两个 resident 服务（RelayService 中继 /
  LinkProbeService 探针）由框架 core/resident 托管线程与时钟，插件只经
  resident_services() 声明。链路翻转 → 中继按「监听门」占用 / 释放 local_port，
  并**发一条 ``resync`` 命令**叫 helper 现在按已声明的意图跑一轮；helper 探那个端口
  就知道上游在不在。
  ⚠️ 链路这条**运行期事实不进意图**（v2 契约，2026-09-29）：意图里只有「要不要隧道」，
  事实的载体是**端口本身**（见 tunnel_spec.TUNNEL_WIRE_VERSION）。

数据流（2026-09-29 通道换成会话）：YAML → ``intent.set("tunnel", …)`` → helper 消费
  → ``event.push("tunnel")`` → 面板。**中间没有文件**：从前那句「tunnel.json →
  tunnel_state.json」描述的两个文件连同监视与敲门一起删了。

契约声明：
- api_version=3，全部贡献由声明点方法声明（决议 D20：row_specs() / component() / state_specs() /
  detail_entry() / settings_page()），无旧 panel_widget/header_chips/settings_pages。
- **设置面收归（2026-09-18，参考《录屏通知》重构）**：自动化开关 / 建隧道模式 / 隧道配置 /
  守护安装卸载与立即检查 → 设置页；**点面板整行 = 运行诊断**（``detail_entry``，
  ``presentation="window"``，壳归框架 ``DetailForm``）。界面全在 ``settings.py``，
  本模块运行期**零 PySide6**（跨面禁令 1），旧 ``config_dialog.py``（含 4 处
  ``QMessageBox``）一并删除。面板行的「自动 / 手动」模式徽标随之撤销 —— 它本身就是一枚
  自动化开关（``ignore_link``），归设置页。
- 状态流唯一路径（§5.3）：tick() 返回 StateReport，框架自动 publish 到
  plugin.ipad_tunnel.status.main；手动动作经 publish_snapshot() 同 topic 广播。
- 事实订阅（2026-09-29）：``subjects()`` 声明 ``tunnel`` 这一个领域，守护一推就到
  （框架转 ``system.helper.observed``）；``on_observed`` **只 mark_dirty**，取值与广播
  全归 ``tick()``（worker 线程）——总线线程不再被任何 IO 占用。
- 状态（StateSpec）与托盘徽标（BadgeRuleSpec(trigger=ERR)）均消费报告，
  不再有 badge_role="alert" 字符串魔法值（旧 alert → ERR）。
- 取值闭集（决议 D13）在模块顶的 ``STATES`` 里列举一次，报告只报取值名。

**本文件自 2026-10-01 起只放「契约面」**（《插件结构规范》§2.4：plugin.py 只含框架
契约方法 ＋ 动作回调，业务与 UI 一律在别的模块）。原 1123 行的正文按职责搬进**同包的
兄弟模块**，方法经 ``self`` 取用实例状态（混合类，零行为变更）：``intent.py`` / ``link.py`` /
``snapshot.py`` / ``settings_data.py`` 各一枚混合类，``constants.py`` 放取值闭集与日志名。
**为什么不拆成子包**：§2.3「目录白名单（硬）」只允许包根 ``*.py`` / ``*.md`` /
``resources/`` / ``__pycache__/`` —— 插件包里不许有子目录。

``plugin.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from barkit.capabilities.automation import AutomationCapability
from barkit.capabilities.helper_ops import (
    HelperOpsCapability,
    HelperOpsTexts,
)
from barkit.capabilities.inline_feedback import InlineFeedbackCapability
from barkit.contribution.specs import (
    DETAIL_PRESENTATION_WINDOW,
    ActionOutcome,
    ActionResult,
    ComponentSpec,
    DetailEntrySpec,
    IconName,
    SettingsPageSpec,
    StateReport,
    StateSpec,
    StatusRowSpec,
    SwitchSpec,
)
from barkit.core.plugin import (
    FAIL_LOG,
    RUN_PERIODIC,
)
from barkit.core.resources import (
    PluginResources,
    SystemMutation,
)
from barkit.core.schema import (
    FieldSpec,
    FieldType,
    FormSchema,
)
from barkit.capabilities import helper_client as daemon
from barkit.plugins.ipad_tunnel import tunnel_spec
from barkit.plugins.ipad_tunnel.bridge import (
    LinkProbeService,
    RelayService,
)
from barkit.plugins.ipad_tunnel.tunnel_spec import TunnelConfigError

# 包内自引用一律相对导入（《插件结构规范》§2.4）。界面全在 settings.py —— 本模块
# 运行期零 PySide6（跨面禁令 1），2026-09-18 随「设置面收归」达成（原 config_dialog.py
# 的 TunnelConfigDialog / TunnelDiagnosticDialog 一并删除）。
from . import settings
from .constants import (
    SELF_STALE_AFTER_S,
    STATES,
    TUNNEL_SUBJECT,
    log,
)
from .intent import _IntentMixin
from .link import _LinkMixin
from .settings_data import _SettingsDataMixin
from .snapshot import _SnapshotMixin, _observed_state

if TYPE_CHECKING:                       # 运行期用局部导入（本模块运行期零 PySide6/UI 依赖）
    from PySide6.QtWidgets import QWidget


class Plugin(
    _IntentMixin,
    _LinkMixin,
    _SnapshotMixin,
    _SettingsDataMixin,
    HelperOpsCapability,
    AutomationCapability,
    InlineFeedbackCapability,
):
    # 混合类按**职责**分开在同包的兄弟模块里（插件包不许子目录，见《插件结构规范》
    # §2.3）：方法经 ``self`` 取用实例状态，故 MRO 顺序不影响变量解析（混合类之间
    # 无继承、无重名）。三个能力基类排在最后 —— `__init__` 里的 `super()` 走的仍是
    # 原来那条链（`HelperOpsCapability` → `AutomationCapability` →
    # `InlineFeedbackCapability` → `core.plugin.Plugin`）。
    id = "ipad_tunnel"
    name = "iPad 隧道"
    run_mode = RUN_PERIODIC
    failure_policy = FAIL_LOG
    suggested_poll = 30          # 建议周期（契约 D21）：隧道通断要尽快反映到面板，
                                 # 30s = 迁移前 DEFAULT_POLL 的实际取值，故零行为变更
                                 # （本组件此前没有 ``poll`` 字段，默认值全由框架给）
    api_version = 3          # 契约版本：框架按精确匹配校验（声明点方法路径）
    order = 25

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        # 守护推来的观测（推过来的那份的**本组件副本**）。构造期先取一次：面板副标题、
        # 报告机这些主线程读的路径都拿它当输入，首轮 tick 之前拿不到就会先闪一帧
        # 「未回报」。此刻会话多半还没连上，取到的是降级结构 —— 那正是实话（「还没
        # 事实」），首轮推送到达即刷掉。
        self._state: dict = _observed_state(self)
        self._daemon_status: dict = daemon.status()
        # iOS 端签名到期通告（announce.fetch 的归一结果；None = 还没通告过）。
        # 写方是探针线程（链路翻转拉一次 ＋ 之后低频重试成功时整体替换），读方是
        # tick / 显示 —— 单属性整体替换在 CPython 下原子，不引入锁。
        self._announce: dict | None = None
        # 下一次允许拉通告的时刻（``time.monotonic()``；0 = 立刻到期）。写读都在探针线程
        # （链路翻转 ＋ 每轮 poll 的低频重试），见 link._on_probe_poll。
        self._announce_next_try = 0.0
        # 最近一次**已出声**的「拿不到」原因（None = 没事）。日志据此压成「按故障期一条」，
        # 见 link._note_announce_fail。
        self._announce_trouble: str | None = None
        # 设置页那行「iOS 端签名」的活引用（页面构建期挂进来，销毁时摘掉）。它必须存在：
        # 设置窗是**启动期构建一次**的，而通告要等探针首翻（≈6~12s 后）才拉得到 ——
        # 这行只能由 `tick()` 续写，构建期读一次必停在「未通告」（见 link.attach_announce_label）。
        self._announce_label = None
        # ⚠️ 装置运维（安装 / 卸载 / 立即检查 ＋ 那块警示的刷新接线）自 2026-09-28 起归
        # `capabilities.helper_ops.HelperOpsCapability` —— 本类不再持有 `_daemon_notice`：
        # 那份引用（以及「页面销毁时摘掉」的纪律）已是那个能力的一部分。
        # 长驻服务（§8.3.1 / 批次 5 后段）：在 __init__ 里建好并持有引用，
        # resident_services() 每次返回**同一批对象**（框架按 name 做差量收敛）。
        # 线程/时钟/起停全归框架；插件只声明。
        #
        # 中继的「监听门」（v2 契约承重）：它只在链路就绪（或用户开了手动模式）时占用
        # local_port —— helper 探那个端口就是在问「iPad 那条链路通不通」。所以这里必须
        # 把判据**以方法引用**交进去（取数口一律现问：注入发生在构造之后，链路马上就会变）。
        self._relay = RelayService(
            ports_provider=self._read_ports,
            link_ready_provider=self._relay_should_listen,
            on_listen_change=self._on_relay_listen_change,
        )
        self._probe = LinkProbeService(
            device_port_provider=self._read_device_port,
            # 探针翻转**不再直接敲门**（v2 契约）：链路这条事实要经中继的监听门落地
            # （占用 / 释放 local_port）之后才叫 helper 跑一轮 —— 上游就是中继本身，
            # 门敲早了 helper 看到的还是上一次的现场。敲门的活归 `_on_relay_listen_change`。
            # 链路翻上来时顺手拉一次 iOS 端签名到期通告（见 link._on_probe_link_change）；
            # 之后每轮 poll 再给一次「链路还在吗」——通告还没有时按低频重试（见
            # link._on_probe_poll，节拍借探针这一个，插件不新起时钟）。
            on_link_change=self._on_probe_link_change,
            on_poll=self._on_probe_poll,
            # 运行期档位：决定 ③④ 两级的节流，以及下探前要不要让中继让路
            mode_provider=self._carrying,
            # 阶梯第 ① 级：中继自报「最近一次有结论的窗口搬过字节吗」。它说搬过就到此为止
            # —— 这是「不与数据面抢通道」的根本保证（详见 bridge 模块 docstring）
            data_plane_state_provider=self._data_plane_state,
            # 链路一翻转就复位那个读数：不复位则重开后第一个周期被旧结论再拆一次
            # （详见 bridge.LinkProbeService._publish 的说明）
            data_plane_reset=self._reset_data_plane,
            # 下探 ③④ 前让中继停收新连接：不停就会量到我们自己的噪音（2026-09-20 自激回路）
            descend_pause=self._pause_relay_accept,
        )
        # 升级迁移：旧版把隧道开关写进了 framework 的 enabled 键，会误伤插件加载门控
        # （关开关 → 下次启动组件不加载）。迁移到独立键 tunnel_enabled（见 _migrate_legacy_config）。
        self._migrate_legacy_config()

    def start(self) -> None:
        # 隧道意图**只表达意图**（要或不要隧道），启动时按配置段声明一遍即可 ——
        # 内容是纯配置、声明幂等，不存在「冲掉运行期事实」这回事（v2 契约之前这里
        # 必须先读盘上的 link_up 再原样写回，正是「事实住在期望态里」的病象）。
        # 链路就绪与否由中继的监听门在运行期表达，helper 现问那个端口。
        #
        # **这一下同时就是「叫守护立刻跑一轮」**：本进程刚起来、守护手里没有这条意图
        # ⇒ 内容变了 ⇒ 调度器把它排成「立刻到期」。不叫的话，面板要等探针探完一轮
        # iPad、中继占上端口之后才被动收到第一份新状态，那之前它念的一直是上一次进程
        # 留下的结论——用户看到的「启动先显示隧道正常、过一会儿才变」就是这么来的。
        # ⚠️ 此刻中继还没起（框架按 resident_services() 在 start() **之后**启动长驻
        # 服务），helper 这一轮探上游会看到「127.0.0.1:local_port 没人在听」——
        # 那**正是事实**，上游此刻确实没有。残留的旧隧道本就该拆掉（一条没有上游的
        # 隧道就是黑洞），而中继占上端口时会由 `_on_relay_listen_change` 立刻再叫一轮
        # 把它建回来。
        try:
            self.sync_tunnel()
        except TunnelConfigError as e:
            # 手改配置非法时，不应让插件整体启动失败（其余功能仍可工作）。
            # 隧道功能此次停用，等配置恢复合法后再由 on_config_changed 重新拉起。
            log.error("同步隧道配置失败（配置可能被手改，隧道功能停用）：%s", e)
        # 状态行刷新由框架经事件总线统一处理（见 panel._recompute_plugin），
        # 插件不再自订阅自身 status topic。
        # 事实订阅（订阅哪个领域）归框架按 `subjects()` 声明登记；禁用时反登记。
        try:
            if not getattr(self, "_cfg_conn", False):
                self.ctx.config.changed.connect(self.on_config_changed)
                self._cfg_conn = True
        except Exception:
            log.exception("订阅配置热重载失败")
        # 长驻服务（中继 + 探针）由框架在 start() 之后按 resident_services() 声明启动：
        # 开关关闭 → 声明为空 → 不监听 1080、不起线程（见 Registry.start_services）。

    def stop(self) -> None:
        # 中继/探针由框架先行停止（Registry.stop_all / PluginManager.disable 的
        # 顺序铁律：先停服务，再 plugin.stop()），此处不再手工停桥接。
        # 拆除靠**两件事分开做**，别再往意图里塞事实（v2 契约）：
        # ① 框架停掉中继 ⇒ 127.0.0.1:local_port 的监听消失（上游这条事实自己变了）；
        # ② 连接关掉 ⇒ 守护立刻收到 **EOF** ⇒ 依附型意图（``tunnel``，见
        #    ``EPHEMERAL_SUBJECTS``）当场被撤回并跑一轮收尾 ⇒ 它复检上游发现没了，
        #    秒级拆隧道。少了 ② 就要等它最长 5s 的下一轮，那期间 hijack_route 开着
        #    就是整机假连接。
        # ⚠️ 从前 ② 是写一个 ``.trigger`` 文件「敲门」；现在**连接本身就是那一声**
        # —— 进程被杀（连 stop() 都跑不到）也照样生效，这正是旧形态给不了的。
        if getattr(self, "_cfg_conn", False):
            try:
                self.ctx.config.changed.disconnect(self.on_config_changed)
            except Exception:  # noqa: BLE001
                pass
            self._cfg_conn = False

    def on_disabled(self) -> None:
        """运行期禁用：**撤回**意图（``declare(tunnel, None)``），守护下一轮即拆隧道。

        中继/探针已由框架在 disable 流程里先行停止（顺序铁律），此处只负责
        「后端副作用」这一层（与 route_guard 禁用时下发全撤意图同理）。

        **撤回不是「什么都不发」**：编排层把它记成「一次待跑的收尾轮」，跑完才休眠
        （见 ``tasks.Runner.set_intent``）—— 隧道是依附型，撤了意图就必须真拆掉它。
        从前这一步是「写 enabled=false 的 tunnel.json + 敲门」，两件事现在合成一件。
        """
        try:
            self.declare(TUNNEL_SUBJECT, None)
            log.info("ipad_tunnel 已禁用：已撤回隧道意图，helper 将停止隧道")
        except Exception as e:  # noqa: BLE001
            log.error("禁用 ipad_tunnel 时撤回意图失败：%s", e)

    # ---------- §9.5 配置默认值单一真源 ----------

    def config_schema(self) -> FormSchema:
        """iPad 隧道的默认值（§9.5 单一真源）。

        2026-09-20 从 ``config.default.yaml`` 迁来（出厂文件从此只留框架保留键
        ``enabled``）；纯搬家，值逐字相同。

        三个要点：

        - ``tunnel_enabled`` 与框架键 ``enabled`` **是两件事**，别混：前者是面板上
          那枚隧道开关（默认关 —— 未配置就接管流量等于让人断网），后者是插件加载
          门控（恒 true）。``_migrate_legacy_config`` 当年修的正是把二者混用的旧版。
        - ``local_port`` 就是主程序端口 1080 这个**静态常量**，发货语义里**没有**
          「看环境变量」的通道 —— 2026-09-20 起连 ``tunnel_spec.default_local_port()``
          这个访问器都删了（它读 ``BARKIT_IPAD_TUNNEL_TEST_PORT``，而配置里恒有本键
          ⇒ 那条通道从来没生效过）。测试要挪开端口，改的是**本 schema 的默认值层**
          （``tests/conftest._retarget_relay_port``），不碰生产路径。
        - ``wifi`` 是**嵌套段**（子键 on_activate / on_deactivate），整段用
          ``FieldType.DICT`` 声明，别拆成平铺字段（理由见 ``FieldType.DICT`` 注释）。
        """
        return FormSchema(
            plugin_id=self.id,
            version=1,
            fields=(
                FieldSpec(key="tunnel_enabled", label="隧道功能开关",
                          type=FieldType.BOOL, default=False,
                          tooltip="面板上那枚开关的状态。默认关：没配好就接管流量会让人断网。"),
                FieldSpec(key="ignore_link", label="手动模式（不等链路探测）",
                          type=FieldType.BOOL, default=False,
                          tooltip="勾选后不等 iPad 链路探测就直接尝试建隧道；它只跳过链路判定，"
                                  "不跳过健康检查。"),
                FieldSpec(key="local_port", label="本机监听端口",
                          type=FieldType.NUMBER, default=tunnel_spec.DEFAULT_LOCAL_PORT),
                FieldSpec(key="device_port", label="iPad 端端口",
                          type=FieldType.NUMBER, default=tunnel_spec.DEFAULT_DEVICE_PORT,
                          tooltip="iPad 端 hev 的监听端口（USB 经 usbmuxd）。"),
                FieldSpec(key="disable_ipv6_on_activate", label="激活时禁用 IPv6",
                          type=FieldType.BOOL, default=True,
                          tooltip="fail-closed 防泄漏；关掉它，v6 会经本机 Wi-Fi 泄漏。"),
                # 下面三条是 2026-10-03 补声明的：它们本来就被读（``settings_data``
                # 读前两条给设置页、``tunnel_spec.build_tunnel`` 读 ``dns`` 建意图），
                # 但默认值一直只活在代码里 —— 于是设置页那两处各自又抄了一份
                # ``False``。这一批只把默认值搬进声明层，读写口径一概不动。
                # 两条劫持开关的**安全默认**是「都关」＝旁路模式，理由见
                # ``tunnel_spec.build_tunnel`` 的 docstring（全局劫持的风险不对称）。
                FieldSpec(key="hijack_route", label="劫持全局 /1 路由",
                          type=FieldType.BOOL, default=False,
                          tooltip="默认关（旁路）：只建隧道、系统网络一无所动。"),
                FieldSpec(key="hijack_dns", label="劫持系统 DNS",
                          type=FieldType.BOOL, default=False,
                          tooltip="默认关；与 hijack_route 必须同开或同关。"),
                FieldSpec(key="dns", label="隧道 DNS 地址",
                          type=FieldType.TEXT,
                          default=tunnel_spec.DEFAULT_DNS,
                          tooltip="系统 DNS 要指向的 hev mapdns 地址，须与 helper 侧同源。"),
                FieldSpec(key="wifi", label="Wi-Fi 切换", type=FieldType.DICT,
                          default={"on_activate": "iPad-Share", "on_deactivate": "HomeNet"},
                          tooltip="激活 / 恢复时切到的 Wi-Fi（须是本机已配过的网络，不托管密码）；"
                                  "留空 = 不切。"),
            ),
        )

    # ---------- 可插拔协议（§9.1，批次 6c） ----------

    def resources(self):
        """隧道配置/状态文件 + A 类 hev 声明（**物理在本插件 ``resources/``**）。

        批次 4 · S4：hev 从 `route_guard/resources/` 迁到本插件目录——这里才是它
        真正的使用方，且从此**只有本插件声明**（route_guard 已撤销冗余声明）。
        框架按「所有已安装插件（含 disabled/failed）的声明并集」算 wanted 集，
        安装器据此部署/回收；卸载本插件且无他人声明时 hev 才被回收。

        隧道是依附型系统变更（§2.5）：app 进程消失 → SOCKS 上游桥必消失 →
        helper 保活轮秒级拆除，不存在跨退出残留。
        """
        return PluginResources(
            # **data_files 空了**（2026-09-29）：本组件从前持有 ``tunnel.json``（期望态）
            # 与 ``tunnel_state.json``（观测），两份都是**通道**（一个往下传、一个往上传），
            # 会话通道落地后两者都没有对象了 —— 意图走 ``intent.set``、观测走
            # ``event.push``，中间不留文件。
            data_files=[],
            helper_tasks=["ipad_tunnel.sync"],   # 编排声明（§7.4.4）
            root_binaries=("hev-socks5-tunnel",),
            system_mutations=[
                SystemMutation(
                    description="utun 隧道接口与默认路由劫持（依附型，helper 秒级拆除）",
                    survives_app_exit=False,
                )
            ],
        )

    def teardown(self) -> None:
        """卸载时复原系统状态：撤回隧道意图 → helper 拆隧道。

        与 ``on_disabled`` 同一动作（幂等）：依附型变更本身不跨退出存续，
        这里再执行是协议完整性 + 确保卸载后不会留着一条没人要的隧道。
        """
        self.on_disabled()

    # ---------- 长驻服务声明（§8.3.1 / 批次 5 后段） ----------

    def resident_services(self) -> list:
        """框架托管的长驻服务集合：开关打开 ⇒ 中继 + 探针；关闭 ⇒ 空。

        声明为空 ⇒ 框架直接停掉两者（线程退出、1080 端口释放），与迁移前
        「开关关闭时不起桥接、不监听」逐项一致。

        必须廉价、无副作用，且每次返回**同一批对象**（框架按 name 做差量收敛）——
        故两个服务在 __init__ 里建好，这里只按开关取舍。
        """
        if not self.automation_active:
            return []
        return [self._relay, self._probe]

    def _observed_now(self) -> dict:
        """此刻的隧道观测（**现取**，不缓存）。

        诊断页要拿它与实时命令输出交叉印证「运行/配置是否一致」，所以必须取**跑诊断
        那一刻**那份 —— 缓存一份构造期的观测会让结论慢一个组件节拍。观测由守护推来，
        住在内存（``observed("tunnel")``），不读盘。
        """
        return _observed_state(self)

    # ---------- 自动化开关（AutomationCapability 能力统一 seam） ----------

    @property
    def automation_active(self) -> bool:
        """隧道自动化（即用户启用开关）是否开启。

        读的是独立的 ``tunnel_enabled`` 键（隧道功能开关），**不是**框架的
        ``plugins.ipad_tunnel.enabled``——后者是插件加载门控，恒为 true 以保证组件常驻、
        开关随时可拨。旧版把二者混用，导致关开关会把整个插件从加载名单里划掉（已通过
        _migrate_legacy_config 迁移修复）。
        """
        return bool(self.cfg.value("tunnel_enabled"))

    def set_automation_active(self, active: bool) -> "ActionResult | None":
        """写 user config 的 tunnel_enabled（隧道功能开关）+ 重声明意图 + 起/停桥接。

        仅写 ``tunnel_enabled``，**绝不**写 framework 的 ``enabled``——否则会误伤插件加载门控，
        复现「关开关 → 下次启动组件整个不加载」的本 bug。framework enabled 由插件框架/默认
        配置管理，恒为 true，与隧道开关正交。

        不需要额外「叫一轮」：开关一翻，意图内容随之变（``tunnel_enabled`` 就是意图里的
        ``enabled``）⇒ 声明发出去，编排层立刻把它排成到期。
        """
        active = bool(active)
        if self.automation_active == active:
            return
        try:
            self.cfg.set("tunnel_enabled", active)
        except Exception as e:  # noqa: BLE001
            log.error("写隧道开关失败：%s", e)
            # 开关会弹回原位而界面一言不发 —— 这正是 ActionResult 存在的理由
            # （契约 §2.6：失败且用户看不出来时，把结局交给宿主去画）。
            return ActionResult(ActionOutcome.FAILED, f"隧道开关没写成：{e}")
        # 新一轮的起跑线：开关是唯一能把链路判定重开的入口。不复位的话，上次留下的
        # 「链路就绪」会让中继在开关一打开时就立刻占端口，helper 于是可能拿一条通往
        # 黑洞的隧道劫持整机流量，等探针 6 秒后翻下来才收场。
        self._probe.reset()
        try:
            self.sync_tunnel()
        except TunnelConfigError as e:
            log.error("同步隧道意图失败：%s", e)
            return ActionResult(ActionOutcome.FAILED, f"开关已存下，但同步给守护失败：{e}")
        # 开关翻转 ⇒ 声明变化 ⇒ 请求框架起动/停止中继 + 探针（差量收敛）
        self._request_service_resync()
        self.publish_snapshot()

    # ---------- 守护状态 ----------

    def refresh_daemon_status(self) -> None:
        self._daemon_status = daemon.status()

    # ---------- 与守护的会话（订阅与事实） ----------

    def intent_subject(self) -> str:
        """本组件的期望态挂在 ``tunnel`` 上（见 ``TUNNEL_SUBJECT``）。"""
        return TUNNEL_SUBJECT

    def subjects(self) -> list[str]:
        """声明本组件要 ``tunnel`` 这个领域的最新事实（取代旧的 ``file_watches()``）。

        框架把这份声明交给会话（订阅即立刻推一份当前的），推送到达时转交
        ``on_observed()``。**不再有「监视哪个文件」这件事** —— 从前盯 ``.parent``
        目录的代价写得下一段：数据目录里任何文件被写都会叫醒本组件，而同一目录里
        住着 route_guard 的状态文件（一个每轮必变的落地时刻 ⇒ 每 30 秒必写一次）
        ⇒ 本组件每 30 秒被一件无关的事叫起来，真变化被淹没（实测一天 1476 条日志）。
        现在订阅挂在连接上，推的是**本领域**的事实，与别人写不写文件无关。
        """
        return [TUNNEL_SUBJECT]

    def on_observed(self, subject: str, observed) -> None:
        """守护推来一份隧道观测（跑在总线线程 = 主线程）：**只标记脏**。

        事实本身不必在这里存：完整取值走 ``observed("tunnel")``（``tick()`` 里现取），
        参数只是「变了」这件事的通知。改内存 / 读盘 / 子进程一概不留在这里 ——
        那会卡住 UI 线程（§8.2.2 handler 契约）。
        """
        if subject == TUNNEL_SUBJECT:
            self.mark_dirty("helper_observed")

    def tick(self) -> StateReport:
        # 事实是推来的（``_observed_state`` 读内存里那份，会话收到即更新）；仍每轮取一次，
        # 因为推送与 tick 之间可能有别的东西改过它，且构造期那份是降级结构。
        self.refresh_daemon_status()
        self._state = _observed_state(self)
        # 设置页那行签名状态续写一遍（**只在这里写**：设置窗启动期就建好了，构建期那份
        # 快照必是「未通告」；见 link.attach_announce_label 与 `_refresh_announce_label`）。
        self._refresh_announce_label()
        # §5.3：返回快照，框架自动 publish 到 plugin.ipad_tunnel.status.main
        return self._snapshot()

    # ---------- 装置运维（安装 / 卸载 / 立即检查） ----------
    # 三件事的实现与接线归 `capabilities.helper_ops.HelperOpsCapability`（三个装同一个
    # root helper 的组件共用一份实现），本类只回答「我这几句说什么」＋「装完刷什么」。
    # 设置页那张卡由**框架按声明注入**（`resources().helper_tasks` 非空即画），
    # 本组件的 `settings.py` 里不再手写装置运维段。

    def helper_notice(self) -> tuple[str, bool]:
        """装置运维卡那块警示：``(文案, 是否要出「重装守护」)``。

        与网络切换共用同一套动作，差别只在这两句文案 —— 本组件**有**「未安装」的警示：
        隧道依赖守护管路由 / DNS / Wi-Fi，没装时整页的动作全都跑不动，而状态行那格
        （``no_daemon``）只说「守护未装」、指不到出路。未装时不给「重装」键 —— 那时
        用户该点的是旁边的「安装守护」。
        """
        if not self._daemon_status.get("installed"):
            return ("系统守护尚未安装。iPad 全流量隧道依赖它（root 权限管理路由/DNS/Wi-Fi），"
                    "点「安装守护」完成一次授权即可。", False)
        if self._daemon_status.get("outdated"):
            return ("守护版本过期（helper 与 App 不一致），会导致隧道配置被拒绝。"
                    "点「重装守护」以应用最新版本。", True)
        return ("", False)

    def on_helper_change(self) -> None:
        """装 / 卸 / 立即检查之后：重取事实 ＋ 重读守护状态 ＋ 广播一台机。

        「重取事实」从前是**重读状态文件**（三条路径 —— 敲门 / 装 / 卸 —— 统一走这一句，
        比按路径分叉好维护）；现在事实是推来的，取的是内存里那份，所以这一句只是把本组件
        的副本拉齐。装完之后新守护会重发意图、跑一轮、把观测推回来 —— 不必在这里额外叫。
        """
        self._state = _observed_state(self)
        self.refresh_daemon_status()
        self.publish_snapshot()

    def helper_ops_texts(self) -> HelperOpsTexts:
        """装置运维那几句话（动作与接线在能力里，只有文案是它的）。"""
        return HelperOpsTexts(
            installed="路由/隧道守护已安装并开始维护（含 iPad 全流量隧道）。",
            uninstalled="守护已卸载，隧道与规则路由已清理。",
            question="将停止守护、删除已有的规则路由与 iPad 隧道、并移除本机守护文件。"
                     "确定卸载？",
            note="「立即检查」让守护立刻重跑一轮并回报状态；"
                 "逐项验证 iPad 链路 / utun / 路由 / DNS 是否按配置生效：点面板里的"
                 "「iPad 隧道」那一行，直接进运行诊断。",
            check="已让守护立即重跑一轮（1~2 秒内回报，面板状态随之刷新）。",
        )

    # ---------- UI 贡献（声明点方法，决议 D20）----------

    def row_specs(self):
        """状态面板行：只剩一枚 Switch（隧道总闸）。

        **「自动 / 手动」模式徽标已撤**（2026-09-18）：它本身就是一枚自动化开关
        （``ignore_link`` = 要不要等链路），与总闸同属「低频 ＋ 有代价」的一类，故随总闸
        一起归设置页（与《网络切换》第 7b 步同款判据）。行上留着它，等于把「配一次就不
        再动」的东西摆在每次都要扫一眼的那一行。

        整行点击 = 运行诊断（``detail_entry``，框架托管）—— 那才是本组件的高频任务流。
        """
        return (
            StatusRowSpec(
                key="main",
                controls=(
                    SwitchSpec(
                        checked=bool(self.automation_active),
                        on_toggle=self.toggle_automation,
                        get_checked=lambda: bool(self.automation_active),
                        get_enabled=lambda: bool(self._daemon_status.get("installed")),
                    ),
                ),
            ),
        )

    def component(self):
        """标题栏组件图标（每插件一枚，U2 §5.2）：身份的唯一真源（D14），名字省略取 Plugin.name。"""
        return ComponentSpec(icon=IconName.TABLET)

    def state_specs(self):
        """状态声明（静态）：标题栏图标颜色 + 行角标由此派生（§4.3）。

        同一组件只有一台状态机 ⇒ 状态名不必写（省略即取 Plugin.name，D14）。
        """
        return (
            StateSpec(
                key="main", icon=IconName.TABLET,
                states=STATES, initial="unknown",
                # 打开失联判定（2026-09-29）：本组件是周期型（30 秒一轮），三轮收不到
                # 它自己的上报 ⇒ 框架把整格改判「状态失联」。⚠️ 这条判的是**本组件**
                # 还在不在上报；「守护写下的结论是不是此刻的」由 ``DAEMON_REPORT_MAX_AGE_S``
                # 那条判据管（见 ``_daemon_report_age``）—— 两件事别混。
                stale_after=SELF_STALE_AFTER_S,
            ),
        )

    def detail_entry(self):
        """详情页 = **运行诊断**（§5.4 高频任务流），点面板那一行直接进。

        原先这一页是「安装 / 卸载 ＋ 立即检查 ＋ 运行诊断」四合一；那三件装置运维动作
        已归设置页（低频），剩下的一件就是这个页面本身 —— 于是「点组件」与「运行诊断」
        合为同一个动作，中间不再隔一层「先选要做什么」。

        形态取 ``window``（决议 D26）：诊断输出是**宽文本**（命令与原始输出逐行排），
        浮层卡那 380px 固定宽装不下；原生标题栏 ＋ 可缩放 ＋ 居中 ＋ 页脚「完成」＋
        Dock 图标全部由框架 ``DetailForm`` 托管，本组件不再自持 ``QDialog``
        （旧 ``TunnelDiagnosticDialog`` 自带 2 处 ``QMessageBox``）。
        """
        return DetailEntrySpec(
            key="main", label="iPad 隧道 · 运行诊断",
            # 三个取数口都传**方法引用**而不是当前值：诊断页可能在探针翻结论、观测刷新
            # 或配置改动之后才被打开/重跑（「取数口一律现问」）。
            build=lambda: settings.build_diag_page(
                hop_provider=self._probe_hop,
                state_provider=self._observed_now,
                tunnel_provider=self._current_tunnel_intent,
            ),
            presentation=DETAIL_PRESENTATION_WINDOW,
        )

    def settings_page(self):
        """设置页：自动化开关 ＋ 建隧道模式 ＋ 隧道配置 ＋ 装置运维（界面在 settings.py）。

        守护未装 / 状态异常（ERR）进托盘队列 —— 取值自己说了算（``StateOption.alerts``，
        ERR 默认即为真，决议 D17）。
        """
        return SettingsPageSpec(
            build=lambda: settings.build_page(self),
            reason="custom: 隧道配置含两个「不对称风险」开关（劫持路由/DNS 在隧道不通时"
                   "会让整机断网），代价说明必须贴在开关旁；装置运维段与 route_guard "
                   "共用 shell/daemon_ops",
        )

    def _build_settings_page(self) -> "QWidget":
        """设置页的稳定门面（框架按 ``SettingsPageSpec.build()`` **零参**调用）。

        UI 本体在 ``settings.py``（§7.2：设置面的全部界面归它），本方法只做转发 ——
        与 ``route_guard`` 同一条边界：插件是唯一知道该把什么注入界面的一侧。
        """
        return settings.build_page(self)
