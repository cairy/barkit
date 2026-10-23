"""route_guard：自动路由（方案 §4.3，P3 实现；批次 5 v3 迁移）。

显示名已按《网络切换设计》改为「网络切换」（``Plugin.name``，§1.1），包名与 ``id`` 仍是
``route_guard`` —— 它是配置键 / topic / helper 任务名的组成部分，冻结不动。

- daemon / observe：app 侧只读事实（``observed("route")`` ＋ 守护安装状态），写入全在 root helper
- 数据流（2026-09-29 会话通道）：配置 → 算好**完整期望态** → ``declare("route", …)`` 一帧 →
  helper 幂等收敛 → 观测由 ``event.push`` 推回来 → 面板读
- **档 B（批次 4 · S3b）**：路由的**匹配决策**是 policy，归本插件 —— 探活后算出
  「完整期望态」直接声明出去（schema v6），helper 只按意图幂等收敛（见 intent.py）。
  探活走框架托管的长驻服务 ``route_probe``（5s 一轮），不占 tick、不碰线程。
- tick（``suggested_poll`` 秒，现为 30）：读**推来的**观测，返回**四台机**的快照，
  框架逐 key 自动广播。框架节拍的配置覆盖键是 ``poll``；写进保活意图的 **helper 节律**
  另读 ``helper_poll``——同一个配置键只能有一个消费者（决议 D27）。
- config.changed（手编热重载）与规则对话框保存 → 重算期望态并声明（**非法则只报错不下发**）
- 启动：声明期望态（首次/同步）+ helper 版本比对（过期提示重装）
- 「立即检查」：由 ``capabilities/helper_ops`` 直接发一发会话（不再有敲门文件）

契约声明：
- api_version=3，全部贡献由声明点方法声明（决议 D20：row_specs() / component() / state_specs() /
  settings_page()），无旧 panel_widget/header_chips/settings_pages。
- 状态流唯一路径（§5.3）：tick() 返回 ``{key: StateReport}``，框架逐 key 自动 publish 到
  ``plugin.route_guard.status.<key>``；手动动作经 ``_publish_all()`` 逐台机广播
  （基类 ``publish_snapshot(snap, key=...)`` 是 B 契约下插件唯一的即时广播入口）。
- **四台状态机**（设计 §6.3 第 6 步 ＋ 第 8 步；原单台 ``main`` 已拆）：``daemon``（守护装置）/
  ``net``（网卡配置）/ ``routes``（路由结果）/ ``profile``（场景命中）。**一台一行**——面板仍是
  **一行**，副标题由组件算好整句（§3.3），行数与状态机数解绑（契约 §2.4 裁决 1）。
  ``net`` 那台是**第 8 步**补的：它的数据源是 helper 的**网卡配置能力**，而那能力此前不存在
  —— 第 6 步若先声明，只能摊出一格永远「尚未读取」的表（宁缺勿假）；能力落地后它才有话说。
- **四台机的数据源各归各的真源**：``daemon`` 读 ``helper_client.status()``；
  ``net`` / ``routes`` 读 helper **回显**（结果只有 helper 知道：意图可设、结果只读，§4.7）；
  ``profile`` 读「命中」判定（回显优先，无回显时回落 app 侧刚算出的意图
  —— 见 ``_scene_rules``）。同一份内容不解释两遍。
- **网卡那一半的入口只有两个**（§2.5 / §4.7 二）：场景里的 ``net``（命中就生效，走意图）
  与 `[恢复 DHCP]`（用户按下的**命令**，走 ``run_op`` 会话帧 —— 它是一次性的，必须被执行、
  且只执行一次，一帧一命令、回帧即回执；可每轮重复的那一半是**策略** ``handback``）。
- 取值闭集（决议 D13）在模块顶的三张表里各列举一次，快照只报取值名；
- 状态（StateSpec）与托盘徽标（BadgeRuleSpec(trigger=ERR)）均消费报告，
  不再有 badge_role="alert" 字符串魔法值（旧 alert → ERR）。
- **面板徽标 = 场景选择器**（§6.3 第 7b 步，设计 §4.5 / §4.6）：本类覆写
  ``AutomationCapability.mode_badge_spec()``——自动化总闸搬去设置页顶部（低频 + 破坏性），
  徽标这一格改放「现在用的是哪一套」（切场景是主路径，§2.6）。三态：
  ``auto``（生效场景名，neutral）/ ``pinned``（同文案，**accent** 提示「你锁定的」）/
  ``off``（`已停用`，neutral）。每项**必须带摘要**（§4.5 硬要求 1）。

**本文件自 2026-10-01 起只放「契约面」**（《插件结构规范》§2.4：plugin.py 只含框架
契约方法 ＋ 动作回调，业务与 UI 一律在别的模块）。原 2005 行的正文按职责搬进**同包
的兄弟模块**，方法经 ``self`` 取用实例状态（混合类，零行为变更）：``automation.py`` /
``reports.py`` / ``intent_state.py`` / ``feedback.py`` / ``profiles.py`` / ``overlay.py`` /
``netops.py`` / ``subtitle.py`` 各一枚混合类，``constants.py`` 放取值闭集与日志名。
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
    ComponentSpec,
    DETAIL_PRESENTATION_WINDOW,
    DetailEntrySpec,
    FormSaveSpec,
    IconName,
    ModeBadgeSpec,
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
from barkit.plugins.route_guard import (
    intent,
    rules,
    settings,
)
from barkit.plugins.route_guard.rules import RouteConfigError

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget
    from barkit.contribution.specs import ActionResult

from .automation import _AutomationMixin
from .constants import DAEMON_STATES, NET_STATES, PROFILE_STATES, ROUTES_STATES, _SHIPPED_DEFAULT_PROFILES, log
from .feedback import _FeedbackMixin
from .intent_state import _IntentStateMixin
from .netops import _NetOpsMixin
from .overlay import _OverlayMixin
from .profiles import _ProfilesMixin
from .reports import _ReportsMixin, _observed_state
from .subtitle import _SubtitleMixin


class Plugin(
    _AutomationMixin,
    _ReportsMixin,
    _IntentStateMixin,
    _FeedbackMixin,
    _ProfilesMixin,
    _OverlayMixin,
    _NetOpsMixin,
    _SubtitleMixin,
    HelperOpsCapability,
    AutomationCapability,
    InlineFeedbackCapability,
):
    # 混合类按**职责**分开在同包的兄弟模块里（插件包不许子目录，见《插件结构规范》
    # §2.3）：方法经 ``self`` 取用实例状态，故 MRO 顺序不影响变量解析（混合类之间
    # 无继承、无重名）。三个能力基类排在最后 —— `__init__` 里的 `super()` 走的仍是
    # 原来那条链（`HelperOpsCapability` → `AutomationCapability` →
    # `InlineFeedbackCapability` → `core.plugin.Plugin`）。
    id = "route_guard"
    # 显示名取《网络切换设计》§1.1 裁决 6（组件已从「路由规则容器」升级为
    # 「按网络环境切换网卡配置 + 路由」的场景化组件）；**``id`` 冻结**为
    # ``route_guard``——它是配置键、topic、helper 任务名的一部分，改名等于迁移用户数据。
    name = "网络切换"
    run_mode = RUN_PERIODIC
    failure_policy = FAIL_LOG
    suggested_poll = 30          # 框架轮询节拍（契约 D21）：30s = 拆键前配置里 ``poll: 30``
                                 # 的实际生效值，故零行为变更。
                                 # **与**声明给 root helper 的节律是两件事——
                                 # 后者读 ``helper_poll``（决议 D27：一个键只服务一个消费者）
    api_version = 3          # 契约版本：框架按精确匹配校验（声明点方法路径）
    order = 20

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        # 守护报来的观测（推过来的那份的**本组件副本**）。构造期先取一次：面板副标题、
        # 报告机这些主线程读的路径都拿它当输入，首轮 tick 之前拿不到就会先闪一帧
        # 「未回报」（实测 5 条用例会红）。此刻会话多半还没连上，取到的是降级结构 ——
        # 那正是实话（「还没事实」），首轮推送到达即刷掉。
        self._state: dict = _observed_state(self)
        # 守护状态**在构造期就读一次**：面板副标题、报告机这些主线程读的路径都拿它
        # 当输入，首轮 tick 之前拿不到就会先闪一帧「未安装」（实测 5 条用例会红）。
        # 代价是首次 ``daemon.status()`` 要跑一次 ``launchctl print``（timeout 5s），
        # 但只有冷启动这一回——它有 30s TTL，之后全部命中缓存（2026-09-28 复核）。
        self._daemon_status: dict = daemon.status()
        # 配置段当前是否非法（空串 = 合法）。收成一处缓存而不是每次求值：
        # `tick()`（worker）与面板的副标题回调（主线程）都要读它，两处现算会在
        # 配置刚改的那一刻给出两种答案。刷新点：__init__ / tick / 每次写配置之后。
        self._config_problem: str = ""
        # ⚠️ 装置运维（安装 / 卸载 / 立即检查 ＋ 那块警示的刷新接线）自 2026-09-28 起归
        # `capabilities.helper_ops.HelperOpsCapability` —— 本类不再持有 `_daemon_notice`：
        # 那份引用（以及它「页面销毁时摘掉」的纪律）已是那个能力的一部分，三个装同一个
        # root helper 的组件共用一份实现（此前是三份逐字重复的接线）。
        # 配置窗体（`_SceneEditor`）把**自己**交给插件：框架壳那枚「保存」键与关窗那一次
        # 询问走的都是组件的回调，而「怎么保存」只有窗体知道（它手里才是用户正在编辑的
        # 那张表）。同 `_feedback`：销毁时由 `_forget_editor` 摘引用。
        self._editor = None
        # 探活服务（框架托管线程）：网络变化 → 重算意图 → 有变化才落盘 + 敲门。
        # 必须在 __init__ 建好并**每次返回同一实例**（框架按 name 做差量收敛）。
        # 覆盖层：上次写覆盖时作用的场景，用于「换场景即失效」（§4.8）。注意它记的是
        # **观测到的**当前场景，不是「覆盖属于谁」——换场景那一刻旧覆盖就该作废。
        self._last_scene: str | None = None
        # 待执行的网卡命令**没有**内存态、也**不写配置**：它是一帧（``run_op``），
        # 回帧即回执。「这条命令执行过没有」由帧本身回答（一帧一命令），不需要序号、
        # 不需要回执表、也不跨重启留任何状态 —— 没有账可记，也就没有账可错。
        self._probe_service = intent.RouteProbeService(
            provider=self._compute_intent,
            declare=lambda obj: self.declare("route", obj),
        )

        # ⚠️ 这里**刻意没有一次性键迁移**：组件不改写用户 YAML（那份文件是用户
        # 验收过的，自动改写不可逆）。旧写法 / 死键在求值时报错并给出改法，见
        # ``rules.build_routes``。

    # ---------- 生命周期 ----------

    def start(self) -> None:
        """加载期：把一份**合法**的期望态交给守护；非法则只记状态、不下发、不抛出。

        三条都在这一步（顺序即判据）：

        1. 先判配置合法性（``_config_problem``）——它同时是「要不要下发」与四台机里
           ``routes`` 那台的输入，只算一次。
        2. 非法 ⇒ **不下发**：一份错期望态推到 root helper 面前没有好处，helper 只会
           整份拒绝并冻结。也**不抛出去**：``Registry.load_all`` 对 ``start()`` 抛异常
           的处置是把整个组件跳过（「启动失败，详见日志」），用户看到的是**组件从面板
           上消失**——而设计要的是「当场报错并给出手改映射」（§6.3 第 4 步）。留着组件、
           让 ``routes`` 机报 ``failed``，用户才看得见那句改法。
        3. 合法 ⇒ 首轮即声明出去，否则 helper 一直处于「未给出期望态」的冻结态。
           （此刻会话**可能还没连上**，不要紧：``declare`` 记在框架侧，连上即补发。）
        """
        self._config_problem = self._evaluate_config_problem()
        if self._config_problem:
            log.error("配置非法，未下发期望态（组件仍加载，面板会报 failed）：%s",
                      self._config_problem)
        else:
            self.sync_routes()
        # 状态行刷新由框架经事件总线统一处理（见 panel._recompute_plugin），
        # 插件不再自订阅自身 status topic。
        # 手编 YAML 热重载 → 重算期望态并声明（规则/间隔零授权生效）
        try:
            self.ctx.config.changed.connect(self.on_config_changed)
        except Exception:
            log.exception("订阅配置热重载失败")

    def stop(self) -> None:
        try:
            self.ctx.config.changed.disconnect(self.on_config_changed)
        except Exception:
            pass

    def on_disabled(self) -> None:
        """运行期禁用：停掉 helper 实际路由，使 UI 与 helper 一致（禁用即停路由）。

        声明一份「全撤」的期望态（零授权的一帧）+ 让它当场收敛；重新启用时 ``start``
        → ``sync_routes`` 会从配置重算恢复。仅由 PluginManager.disable 触发（非 stop），
        故 app 退出不会误清路由。

        **不能用「撤回意图」（``declare("route", None)``）代替**：撤回的语义是「守护，你
        这轮跑完收尾就歇着」，而那一轮 ``routeops.run_cycle`` 拿到的正文是 ``None``，
        按既有语义是「没有路由意图 ⇒ **保持上轮路由不动**」（保活）。要真撤掉路由，
        必须显式发一份「每条规则 apply=False」的期望态。
        """
        try:
            # 用「永不命中」的假探针，任何规则的 apply 都算成 False。
            try:
                routes = rules.build_routes(self.plugin_config())
            except RouteConfigError:
                # 与 ``rules.build_routes`` 同口径：产物**不带** ``version``，线格式版本号
                # 由下面 ``intent.build_intent`` 一处给出（它自己也设 ``INTENT_VERSION``）。
                routes = {"poll": rules.DEFAULT_HELPER_POLL, "rules": []}
            self.declare("route", intent.build_intent(routes, None, probe=lambda *_a: False))
            log.info("route_guard 已禁用：期望态全撤，helper 将停止生效路由")
        except Exception as e:  # noqa: BLE001
            log.error("禁用 route_guard 时下发全撤意图失败：%s", e)

    # ---------- 与守护的会话（订阅与事实） ----------

    def intent_subject(self) -> str:
        """本组件的期望态挂在 ``route`` 上（``_sync_intent`` 声明的那个领域）。"""
        return "route"

    def subjects(self) -> list:
        """声明本组件要 ``route`` 这个领域的最新事实（取代旧的 ``file_watches()``）。

        框架把这份声明交给会话（订阅即立刻推一份当前的），推送到达时转交
        ``on_observed()``。**不再有「监视哪个文件」这件事** —— 组件也不再自己读
        状态文件、不再判 mtime。
        """
        return ["route"]

    def on_observed(self, subject: str, observed) -> None:
        """守护推来一份路由观测（跑在总线线程 = 主线程）：**只标记脏**。

        事实本身不必在这里存：完整取值走 ``observed("route")``（``tick()`` 里现取），
        参数只是「变了」这件事的通知。改内存 / 读盘 / 子进程一概不留在这里 ——
        那会卡住 UI 线程（§8.2.2 handler 契约）。
        """
        if subject == "route":
            self.mark_dirty("helper_observed")

    # ---------- 长驻服务（框架托管，批次 5 后段定案） ----------

    def resident_services(self) -> list:
        """框架托管的长驻服务：路由探活（周期型）。

        暂停时也要跑：暂停冻结是靠「沿用上轮生效态」实现的，而它的输入不断由本服务
        刷新（意图 ≈ helper 每轮要读的那份期望态），故探活不能停。
        必须廉价、无副作用、每次返回**同一批对象**——服务在 __init__ 里已建好。
        """
        return [self._probe_service]

    # ---------- §9.5 配置默认值单一真源 ----------

    def config_schema(self) -> FormSchema:
        """自动路由的默认值（§9.5 单一真源）。

        2026-09-20 从 ``config.default.yaml`` 迁来（出厂文件从此只留框架保留键
        ``enabled``）；纯搬家，值逐字相同 —— 包括那两条出厂场景。

        - ``helper_poll`` 引用 ``rules.DEFAULT_HELPER_POLL``（它同时是
          ``helper_poll()`` 的兜底），不再另抄一个 ``30``。
        - ``profiles`` 默认给 office / home 两条**出厂示例**：这是发货行为 ——
          用户装上就看到两条能改的场景，而不是面对一张空表。真源从出厂文件换成
          ``_SHIPPED_DEFAULT_PROFILES`` 之后，
          ``test_shipped_default_profiles_compile_like_the_old_default_rules``
          继续逐项守着它编译出的规则。

        下面五条（``paused`` / ``fallback`` / ``pinned`` / ``handback_ifaces`` /
        ``handback_dns`` / ``rule_overrides``）是 2026-10-03 补声明的：它们本来就
        被本组件的代码读（``automation`` / ``netops`` / ``profiles`` / ``overlay``
        各有一处），却一直没进声明层 —— 于是每个读点都得自己写一遍兜底，与这里
        迟早分叉（方案 §2）。这一批只搬**默认值**，读写口径一概不动。
        """
        return FormSchema(
            plugin_id=self.id,
            version=1,
            fields=(
                FieldSpec(key="helper_poll", label="helper 巡逻节律（秒）",
                          type=FieldType.NUMBER, default=rules.DEFAULT_HELPER_POLL,
                          tooltip="随期望态一起发给 root helper 的巡逻间隔；与框架节拍 poll "
                                  "是两个键，各服务一个消费者（决议 D27）。"),
                FieldSpec(key="profiles", label="网络场景",
                          type=FieldType.LIST, default=_SHIPPED_DEFAULT_PROFILES,
                          tooltip="一个条目 = 一个场景（在某网络里、配某 IP、加哪些路由）；"
                                  "列表顺序即优先级，第 1 个命中者胜。"),
                FieldSpec(key="paused", label="自动化总闸（暂停）",
                          type=FieldType.BOOL, default=False,
                          tooltip="True = helper 冻结、已生效的路由原样不动。"),
                FieldSpec(key="fallback", label="所有场景都不匹配时",
                          type=FieldType.CHOICE, default=rules.FALLBACK_KEEP,
                          choices=rules.FALLBACKS,
                          tooltip="keep = 一根手指都不碰网卡；dhcp = 交还系统接管。"),
                FieldSpec(key="pinned", label="手动指定的场景",
                          type=FieldType.TEXT, default="",
                          tooltip="空串 = 按匹配自动选；填名字 = 锁定到那个场景。"),
                FieldSpec(key="handback_ifaces", label="「恢复 DHCP」作用在哪几张网卡",
                          type=FieldType.LIST, default=[],
                          tooltip="空列表 = 还没配置过（徽标下拉里那一项不出现）。"),
                FieldSpec(key="handback_dns", label="「恢复 DHCP」连带恢复 DNS",
                          type=FieldType.BOOL, default=True),
                FieldSpec(key="rule_overrides", label="场景的临时覆盖",
                          type=FieldType.DICT, default={},
                          tooltip="键是场景名，值是 {routes_suppressed: bool}；"
                                  "随换场景自动回到「按配置」。"),
            ),
        )

    # ---------- 可插拔协议（§9.1，批次 6c） ----------

    def resources(self):
        """数据文件 + 系统变更清单。

        - **不再声明 ``root_binaries``**（批次 4 · S4）：hev 只在它真正的使用方
          ``ipad_tunnel/resources/`` 分发，由 ipad_tunnel 独家声明。此前两个插件
          都声明同一个 A 类资产，是「按已声明插件并集取 wanted」口径下的冗余；
          迁出后卸载 route_guard 不再牵连 hev（§11 登记风险收口）。
        - **``data_files`` 空了**（2026-09-29）：本组件从前持有 ``routes.json`` 与
          ``state.json``，两份都是**通道**（期望态往下传、观测往上传），会话通道落地后
          没有对象了。连带消失的是那个「延迟删除」（``deferred``）—— 没有文件要删，
          也就没有「别删早了让 helper 读到空」这件事。
        - 路由属自持型系统变更（app 退出不撤，helper 持续保形）。
        """
        return PluginResources(
            data_files=[],
            # 编排声明（§7.4.4）：我拥有这条 helper task。框架汇总后经 ``intent.set("tasks",…)``
            # 送出；卸载 → 该 task 被置 enabled=false，helper 不再执行。
            helper_tasks=["route_guard.sync"],
            system_mutations=[
                SystemMutation(
                    description="系统路由表中由 BarKit 写入并持续保形的规则",
                    survives_app_exit=True,
                )
            ],
        )

    def teardown(self) -> None:
        """卸载时复原系统状态：下发「全撤」期望态 → helper 撤掉全部已生效路由。

        与 ``on_disabled`` 同一动作（幂等）：卸载七步里 disable 已触发过一次，
        这里再执行是协议完整性 + 防外部路径直接调 teardown 漏撤路由。

        **本方法只负责「把意愿说出去」**，不负责等它被消费 —— 从前要靠框架等 helper
        回报「全部已撤」才敢删文件（删早了 helper 会冻结）；现在没有文件可删，意愿
        与消费走同一条连接的先后次序，送达即收敛。
        """
        self.on_disabled()

    # ---------- 守护状态 ----------

    def refresh_daemon_status(self) -> None:
        self._daemon_status = daemon.status()

    # ---------- 自动化开关（AutomationCapability 能力统一 seam） ----------
    # 路由的「暂停」即自动化的「非运行态」，二者语义同一；用 automation_active
    # 收敛，避免与 notify 的 auto_enabled 形成极性别名。set_paused / is_paused /
    # toggle_pause_action 仍作为路由领域 API 保留（测试与外部调用依赖）。

    @property
    def automation_active(self) -> bool:
        """自动化（自动路由）是否运行中；暂停即非运行态。"""
        return not self.is_paused

    def set_automation_active(self, active: bool) -> "ActionResult | None":
        """统一 seam：开启/暂停自动化 → 委托路由的 `set_paused`，并**透传结局**。

        写盘失败必须传出去（``AutomationCapability`` 契约 §2.6）：宿主拿到才会把
        「没写成」画出来 —— 开关会弹回原位，不透传就是一次静默失败（改前本类声明
        ``-> None`` 且丢掉返回值，正是那种情形）。
        """
        return self.set_paused(not active)

    # ---------- tick（worker 线程，observe 失败只记日志不熔断） ----------

    def tick(self) -> dict[str, StateReport]:
        """每轮：读守护状态 + 推来的观测 → 返回**四台机**的快照（§5.3）。

        返回 ``{key: StateReport}``，框架逐 key 发布到 ``plugin.route_guard.status.<key>``
        ——多台状态机的标准写法（同 ``notify_guard``）；单台机的 ``main`` 保留键不再使用。

        从前这里要判 ``state.json`` 的 mtime 决定要不要重读盘；现在事实是**推来的**
        （``observed`` 读内存里那份，会话收到即更新），仍留一次「与上轮不同吗」的比较 ——
        它不省 I/O（本来就没有 I/O），而是用来判**命中场景换没换**：换了才把上一次的
        「本次不叠加」覆盖层作废（§4.8）。
        """
        self.refresh_daemon_status()
        # 配置合法性：与探活各自求值一次（纯计算、无 I/O），但**结果缓存共用**，
        # 免得 tick 说非法、面板副标题说合法。刷新点集中在这一处（加写配置后的几处）。
        self._config_problem = self._evaluate_config_problem()
        new_state = _observed_state(self)
        if new_state != self._state:
            self._state = new_state
            # 命中的场景变了 ⇒ 上一次的「本次」到此为止（§4.8）；放在这里是因为
            # 换场景这件事只有消费了守护回显才知道。本方法碰配置（有 IO），
            # 故**不能**挪进 ``on_observed``（那里只许更新内存 + mark_dirty）。
            self._reap_suppressions()
        return self._all_reports()

    # ---------- 装置运维（安装 / 卸载 / 立即检查） ----------
    # 三件事的实现与接线归 `capabilities.helper_ops.HelperOpsCapability`（三个装同一个
    # root helper 的组件共用一份），本类只回答「我这几句说什么」＋「装完刷什么」。
    # 设置页那张卡由**框架按声明注入**（`resources().helper_tasks` 非空即画），
    # 本类的 `settings.py` 里不再手写装置运维段。

    def helper_notice(self) -> tuple[str, bool]:
        """装置运维卡那块警示：``(文案, 是否要出「重装守护」)``。

        文案为空串 = 没有警示（整块隐藏）。本组件**没有**「未安装」的警示：那种局面由
        守护状态机自己的取值（``not_installed``）与面板副标题说，设置页再写一遍就是把
        同一句话摆两处。只有「装了但版本过期」值得在这里喊 —— 它有**一条明确的出路**
        （重装），而「未安装」的出路就是旁边那枚「安装守护」。
        """
        if self.daemon_outdated():
            return ("守护版本过期：旧版本会拒绝新的规则，路由不会生效。", True)
        return ("", False)

    def on_helper_change(self) -> None:
        """装 / 卸 / 心跳检查之后：**重新声明期望态** ＋ 重读守护状态 ＋ 广播三台机。

        从前这里是「重读状态文件」—— 装完那一刻 helper 还没跑完一轮，读到的多半还是
        旧的（原样留着只是为了三条路径统一成一句）。会话通道落地后「重读现场」这件事
        没有了对象，该做的变成**把期望态再声明一次**：新守护一起来就该拿到意图，
        而不是干等到下一次配置变化。``sync_routes`` 幂等（内容没变就不重发）。
        """
        try:
            self.sync_routes()
        except RouteConfigError as e:
            log.error("装 / 卸后重新声明期望态失败：%s", e)
        self.refresh_daemon_status()
        self._publish_all()

    def helper_ops_texts(self) -> HelperOpsTexts:
        """装置运维那几句话（动作与接线在能力里，只有文案是它的）。"""
        ver = (self._daemon_status or {}).get("version") or "?"
        return HelperOpsTexts(
            installed=f"路由守护已安装并开始自动维护路由（v{ver}）。",
            uninstalled="路由守护已卸载，规则路由已清理。",
            question="将停止守护、删除已有的规则路由与本机守护文件。确定卸载？",
            note="守护为系统级常驻进程，安装与卸载各需一次系统授权；"
                 "场景与路由规则在面板上点「网络切换」整行编辑。",
        )

    def daemon_outdated(self) -> bool:
        """守护版本落后于 app（helper 与 App 不一致）——设置页据此出红字与一键重装。"""
        return bool(self._daemon_status.get("outdated"))

    def mode_badge_spec(self) -> ModeBadgeSpec:
        """面板徽标 = **场景选择器**（第 7b 步；覆写 ``AutomationCapability`` 的默认实现）。

        为什么推翻默认实现：那个默认给的是「自动化总闸二态徽标」（自动 ⇄ 手动），而设计
        把总闸移到了**设置页顶部**（§4.6：低频 + 破坏性），徽标这一格改放「现在用的是哪一套」
        —— 切场景是**主路径**（§2.6：手动场景无法自动触达，必须两步内可达）。

        三件事全交给声明，框架不猜：``get_items``（每次点击重读，配置改了列表就跟着变）、
        ``get_state``（每次刷新自对齐 label ＋ accent）、``label`` 兼任当前项（决议 D33）。
        ⚠️ **不给 ``on_click``**：与候选列表互斥（``ModeBadgeSpec.__post_init__`` 会当场拒），
        「选中某项做什么」写在各自的 ``MenuItem.on_click`` 上。

        ◷ 停用态下这一格**仍然可点**：徽标可点与否是**构造期**定死的（面板按
        ``spec.mode`` 建一次，之后只做 ``refresh_dynamic``），而配置能否在这条路径上
        改是运行期的事 —— 两者不同源。选到的场景会被写进配置，但在总闸打开之前看不出来，
        故「停用态点徽标」列在真机目测项里（§4），不在这里假装它不可点。
        """
        return ModeBadgeSpec(
            label=self._badge_label(),
            accent=self._badge_accent(),
            get_items=self.scene_items,
            get_state=lambda: (self._badge_label(), self._badge_accent()),
        )

    # ---------- UI 贡献（声明点方法，决议 D20）----------

    def row_specs(self):
        """状态面板行（开关 + 场景选择器徽标 + 整行点击 + 行级副标题）。

        **一行，不是三行**：行与状态机之间没有机制绑定（契约 §2.4 裁决 1），三台机的
        信息由 ``subtitle`` 合成一句（§3.3），框架只负责画。
        """
        return (
            StatusRowSpec(
                key="main",
                controls=(
                    SwitchSpec(
                        checked=self._switch_checked(),
                        on_toggle=self.toggle_route_switch,
                        get_checked=self._switch_checked,
                        # 不再门控在「命中」上（§4.8）：没命中也要能说「下次进来别加」。
                        # 只有压根不知道该作用到哪个场景时才禁用。
                        #
                        # **停用时禁用**（§4.3 的四态对照表）：那时 helper 整体冻结，
                        # 翻这一下只是写个不会生效的覆盖 —— 开关当场翻过去、路由一条没动，
                        # 用户会以为已经撤掉了（而 §3.2 的裁决恰恰是「停用不回滚」）。
                        get_enabled=lambda: (
                            not self.is_paused and self._current_scene() is not None
                        ),
                    ),
                ),
                # 徽标 = **场景选择器**（第 7b 步落地）：文案是生效场景名，点开是场景列表，
                # 选中的那个就是一次 `pinned` 切换（§4.5 / §4.6）。三态与 accent 见
                # `mode_badge_spec`；自动化总闸不在这条路径上 —— 它在设置页顶部。
                mode=self.mode_badge_spec(),
                # 整行点击**不由本行声明**：框架若发现组件声明了 DetailEntrySpec，会合成
                # 「打开详情页」（契约 §2.4 / UI 规范 §5.3）。旧写法把 on_click 接到
                # `install_action`（重装/诊断）上，是设计 §5.1 点名的现存违规——重装是
                # 装置运维，落点归设置页，整行该走正规路径。
                # 行级副标题：三台机合成一句（契约 §2.7 / 决议 D24）
                subtitle=self._row_subtitle,
            ),
        )

    def detail_entry(self):
        """详情页 = **场景配置窗体**（设计 §4.3 L3 草图）。

        形态取 ``window``（决议 D26）：左列表（顺序即优先级）＋ 右表单，实测要 560–640px，
        面板那个 320px 的浮层卡装不下。触发方式是默认的 ``row`` —— 点面板整行进入，
        就是设计 §4.3 L2 注的那句「点整行 → 配置窗体」。

        ``save``（决议 D35）：这张表是**改完攒着、点「保存」才写**的编辑面（2026-09-18
        用户裁定，翻转了原先「改一次落一次盘」的口径）。框架壳据此多画一枚「保存」键，
        并在用户关窗时拦一下「还有没保存的改动」—— 这两件都只有框架能做（它才知道
        窗是什么时候关的），而「写什么、什么算填完」只有窗体知道，故回调挂在插件上、
        转发给当前那个 `_SceneEditor`。
        """
        return DetailEntrySpec(
            key="main",
            build=lambda: settings.build_detail_page(self),
            presentation=DETAIL_PRESENTATION_WINDOW,
            save=FormSaveSpec(commit=self._editor_commit, dirty=self._editor_dirty),
        )

    def component(self):
        """标题栏组件图标（每插件一枚，U2 §5.2）：身份的唯一真源（D14），名字省略取 Plugin.name。

        ``ROUTE`` **不是新画的**：它在图标库里躺了很久、一直零消费方，而真正做路由的
        这个组件从前挂着 ``SHIELD`` —— 与 `hosts_guard` 同一枚（2026-10-05 归位，S10）。
        换它的理由不是美观：四台状态机全用同一枚字形时，菜单栏那一格看不出「这是哪个
        组件在说话」，而 ``SHIELD`` 在本仓是**两个组件共用**的语义资源。
        """
        return ComponentSpec(icon=IconName.ROUTE)

    def state_specs(self):
        """四台状态机（设计 §3.1）：``daemon`` / ``net`` / ``routes`` / ``profile``。

        - states 是取值闭集（决议 D13）；initial 是首屏起点（取值名，不是工厂）。
        - **声明顺序 = 关注度顺序**：严重度同级时先声明的那台当代表态（它决定三处
          显示位的颜色）。``daemon`` 在最前（守护没装时另外三台说什么都无从谈起），
          ``net`` 紧随其后 —— 与设计 §3.1 表格的行序一致。
        - 多台机各自**显式命名**（``title``）：省略会由框架补成 ``Plugin.name``，四台
          就都叫「网络切换」，设置窗的菜单栏来源候选里分不出谁是谁（决议 D14）。
        - 四台都声明主图标 ROUTE ⇒ 四台都可被选为菜单栏主图标来源。**两个 WARN 取值
          另有取值图标** ``ROUTE_WARN``（部分叠加 / 多场景同时命中，2026-10-05 落地）：
          那两格是「叠了但叠得不对」，与主图标同形不足以区分 ⇒ D13「图标随取值变」
          在这台机上第一次真生效。其余取值一律留空（回落主图标，差别在**颜色跟着
          哪台机走**）—— 用户选「网卡」就是让那一格盯网卡配置的健康度。
        - ``stale_after=None``：失联判据留给框架的熔断（``FAIL_LOG`` 策略 + 调度器），
          这里不拿一条自己编的秒数冒充（与旧单台机的口径一致，零行为变更）。
        """
        return (
            StateSpec(
                key="daemon", icon=IconName.ROUTE, title="守护",
                states=DAEMON_STATES, initial="unknown",
                stale_after=None,
            ),
            StateSpec(
                key="net", icon=IconName.ROUTE, title="网卡",
                states=NET_STATES, initial="unknown",
                stale_after=None,
            ),
            StateSpec(
                key="routes", icon=IconName.ROUTE, title="路由",
                states=ROUTES_STATES, initial="unknown",
                stale_after=None,
            ),
            StateSpec(
                key="profile", icon=IconName.ROUTE, title="场景",
                states=PROFILE_STATES, initial="unknown",
                stale_after=None,
            ),
        )

    def settings_page(self):
        """设置页：**装置运维**（动作 ＋ 守护安装卸载 ＋ 过期重装）。

        场景配置不在这里 —— 它是领域对象集合，归详情页（点面板整行，§4.2 落点表）。
        三个 ERR 取值（守护未安装 / 守护过期 / 路由应用失败）与那个 WARN（部分叠加、
        多场景同时命中）自己说了算（``StateOption.alerts``，级别阈值归
        ``tray_min_level``，决议 D17）——面板/托盘不必另写一份判据。
        """
        return SettingsPageSpec(
            build=self._build_settings_page,
            reason="custom: 动作入口页（立即检查 / 守护安装卸载），非字段表单",
        )

    def _build_settings_page(self) -> "QWidget":
        """设置页的稳定门面（框架按 ``SettingsPageSpec.build()`` **零参**调用）。

        UI 本体在 ``settings.py``（§7.2：设置面的全部界面归它），本方法只做转发 ——
        留着它是为了「插件是唯一知道该把什么注入界面的一侧」这条边界，也省得框架侧的
        调用契约跟着模块搬家。
        """
        return settings.build_page(self)
