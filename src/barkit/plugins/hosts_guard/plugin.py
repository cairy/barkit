"""hosts_guard：/etc/hosts 托管块插件（Spec 贡献范式）。

- api_version=3：全部贡献由声明点方法声明（决议 D20：row_specs() / component() / state_specs() /
  settings_page()），无旧 panel_rows/header_chips/settings_pages。
- 状态流唯一路径（§5.3）：tick() 返回 StateReport，框架自动 publish 到
  plugin.hosts_guard.status.main；手动动作经 publish_snapshot() 同 topic 广播。
- 取值闭集（决议 D13）在模块顶的 ``STATES`` 里列举一次，报告只报取值名；
- 状态（StateSpec）与托盘徽标（BadgeRuleSpec(trigger=ERR)）均消费该报告，
  不再有 badge_role="alert" 字符串魔法值（旧 alert → ERR）。
"""

from __future__ import annotations

import logging
import time

from barkit.contribution.specs import (
    ComponentSpec,
    DETAIL_PRESENTATION_WINDOW,
    DetailEntrySpec,
    IconName,
    SettingsPageSpec,
    StateLevel,
    StateOption,
    StateReport,
    StateSpec,
    StatusRowSpec,
)
from barkit.core.plugin import FAIL_BREAK, RUN_PERIODIC
from barkit.core.resources import PluginResources, SystemMutation
from barkit.core.schema import FieldSpec, FieldType, FormSchema
from barkit.plugins.hosts_guard import resolve, settings as settings_ui, store
from barkit.plugins.hosts_guard.hostsfile import read_records
from barkit.plugins.hosts_guard.manager_view import HostsManagerContent
from barkit.plugins.hosts_guard.models import (
    BLOCK_MARKER,
    DEFAULT_RESOLVE_POLL,
    MIN_RESOLVE_POLL,
    normalize_rules,
)
from barkit.capabilities import helper_client as daemon
from barkit.capabilities.helper_ops import HelperOpsCapability, HelperOpsTexts
from barkit.capabilities.inline_feedback import InlineFeedbackCapability

log = logging.getLogger(__name__)

#: 「自动更新（DNS 重新解析）间隔」的默认值与下限（秒）在 ``models`` 里定义
#: （配置键 ``resolve_poll`` 与期望态里的 ``poll`` 字段共用一份）。
#: **不是**框架的轮询节拍——那是 ``suggested_poll``（契约 D21）。一个配置键只服务一个
#: 消费者（决议 D27）：本组件自己的节律读 ``resolve_poll``，框架节拍才读 ``poll``。

#: 取值闭集（决议 D13）：取值名 / 级别 / 默认文案只在**这里**写一次，
#: 报告只报取值名，框架回查本表取级别与图标。
#: 首格 ``unknown`` 是**首屏起点**（``initial``）：决议把 initial 从「读现场的工厂」
#: 改成「从哪一格开始」之后，取值集里必须有这一格，否则首屏只能拿某个具体读数
#: 冒充「还不知道」。取值图标留空即回落状态机主图标 DNS。
STATES: tuple[StateOption, ...] = (
    StateOption("unknown", StateLevel.IDLE, text="尚未读取"),
    StateOption("no_daemon", StateLevel.IDLE, text="hosts 守护未装"),
    StateOption("unconfigured", StateLevel.IDLE, text="hosts 未配置"),
    StateOption("error", StateLevel.ERR, text="规则异常"),
    StateOption("entries_bad", StateLevel.WARN, text="有条目异常"),
    StateOption("synced", StateLevel.OK, text="已同步"),
)


#: 本组件在会话里的**领域名**（订阅事实用它，声明意图也用它）。
HOSTS_SUBJECT = "hosts"


def _observed_state(plugin) -> dict:
    """守护推来的 hosts 观测；**还没收到过**返回空 dict（面板降级，不崩）。

    降级**合并为一种**（不区分异常类型，一律 ``{}``）——这是本组件与
    route_guard / ipad_tunnel 的**有意差异**（规范 §6.5）：hosts 面板对
    「读不到状态」只做空展示，不需要三类文案。

    从前这里是 ``_read_state(plugin)``：读 ``hosts_state.json``。会话通道落地后文件没有了，
    剩下的是「还没事实」这一种 —— 空 dict 正好同时表达「还不知道」与「没什么可说」。
    """
    state = plugin.observed(HOSTS_SUBJECT, None)
    return state if isinstance(state, dict) else {}


class Plugin(HelperOpsCapability, InlineFeedbackCapability):
    # 两个能力都自己继承 `core.Plugin`，故这里不再把那个基类并列写一遍
    # （写成 `class Plugin(HelperOpsCapability, Plugin)` 虽然合法，但读起来像递归）。
    id = "hosts_guard"
    name = "hosts 托管"
    run_mode = RUN_PERIODIC
    failure_policy = FAIL_BREAK
    suggested_poll = 300         # 框架轮询节拍（契约 D21）：300s = 拆键前配置里
                                 # ``poll: 300`` 的实际生效值（框架据此读状态文件有没有变），
                                 # 故零行为变更。
                                 # **与**本组件自己的「自动更新（DNS 解析）间隔」是两件事
                                 # ——后者读 ``resolve_poll``（决议 D27：一个键只服务一个消费者）
    api_version = 3          # 契约版本：框架按精确匹配校验（声明点方法路径）
    order = 35

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        # ⚠️ 这里**刻意没有一次性键迁移**：组件不改写用户 YAML（那份文件是用户
        # 验收过的，自动改写不可逆）。``poll`` 从头到尾就只归框架读；死键
        # ``state_poll`` 直接删掉那一行即可（D27：面板刷新＝框架 tick 的节拍）。
        self._state: dict = _observed_state(self)
        self._daemon_status: dict = daemon.status()
        # 本组件**没有** `self._dialog`：管理面只从面板整行进（框架 `DetailHost` 托管）。
        # 设置页那枚「管理条目…」按钮已删 —— 两个入口会各开一份内容面、互不感知
        # （详见 `detail_entry()` 的说明）；别再把独立窗口长回来。
        self._last_error = None
        # 解析缓存：**只在内存里**（2026-09-29 删掉了 ``hosts_resolved.json``）。它从前
        # 存在的理由是「跨 app 重启保住解析结果」，而解析结果本来就要作为意图正文发出去、
        # 在内存里天然存在；重启后重新解析的代价只是几条 DNS 查询，缓存本身又带 TTL
        # —— 保住一个即将过期的 IP 收益极小（《通信通道换成会话》§11.5 判定）。
        self._cache: dict = {}
        # 最近一次算出的条目（带 status / effective_ip）——供 _snapshot 做状态解释
        #（§7.4.3：状态解释归 app；helper 那边的观测只回报「写了什么 / 是否成功」），
        # 同时也是管理窗那两列的取数源（见 `status_index()`）。
        self._entries: list = []
        # 周期型长驻服务（解析链已归 app 侧，档 B）；服务在 __init__ 建好，
        # resident_services() 每次返回同一批对象（框架按 name 差量收敛）。
        # ``publish`` 是**声明意图**的出口：provider 纯计算，服务负责变化检测 + 声明。
        self._resolve_service = resolve.HostsResolveService(
            provider=self._service_provider, publish=self._publish_intent,
        )
        try:
            self._build_payload(resolve_due=False)   # 预热 _entries（不发网络）
        except Exception:  # noqa: BLE001 —— 构造期失败不拖垮插件加载，状态行降级
            log.exception("hosts 条目预读失败（状态行将降级）")

    # ---------- 状态索引（管理窗那两列的**唯一**取数口）----------

    def status_index(self) -> dict[str, dict]:
        """``{域名: {"ip": 生效 IP, "status": 解析状态, "pending": 是否在等更新}}``
        —— 管理窗那几格的取数口。

        数据源是 **app 自己算的那一份**（``self._entries``，由 ``resolve.apply_resolutions``
        产出），**不是**守护推来的观测。依据是 2026-09-29 那批把解析链与**状态解释**都判给
        app（§7.4.3）：守护从此只回报「写了什么 / 是否成功」（``hosts: [{host, ip}]``），
        它已经不认识 ``status`` 这个字段了。

        ⚠️ 这里此前仍按旧字段名读观测里的 ``entries``（那是**解析链回迁 app 侧之前**的形状）
        ⇒ 恒得空表 ⇒ 管理窗整列落在默认值上、恒显「待解析」（用户 2026-09-30 上报的现场）。
        别再把它读回观测：那个键不会再出现，那批路径常量已连同文件一起删掉（``paths.py``）。

        每次调用**重算一遍**（``resolve_due=False`` —— 不发网络，只读条目 ＋ 内存里的解析
        缓存），与改动前那句「每次调用现取」是同一个承诺：管理窗打开时看到的是此刻的结论。
        代价如实说：app 刚启动的头 ``resolve.PROBE_INTERVAL``（30 秒）内缓存还是空的，
        此刻显示「待解析」是**真的**（还没解析过），长驻服务跑过一轮就变「已同步」。

        按《插件结构规范-缘起》§4 跨面禁令 3，UI 一律不直接读写配置文件 / 状态文件，须经
        插件方法 —— 那份读法只在本模块；门禁
        `tests/test_architecture.py::test_no_config_io_in_ui` 守这条。
        """
        try:
            self._build_payload(resolve_due=False)
        except Exception:  # noqa: BLE001 —— 取数口不该把窗口构建带崩，降级成空表
            log.exception("hosts 状态索引重算失败（管理窗那两列将落到默认值）")
        entries = self._entries          # 读一次到本地：长驻线程每轮会整体换掉这一项
        # 「这一条正等着手动更新完成」也是这两列要说的事实之一（管理窗据此显示
        # 「更新中」并在收到它之后停掉轮询）—— 故与 ip / status 同源同取。
        pending = self._resolve_service.pending_hosts()
        out: dict[str, dict] = {}
        for e in entries:
            if not isinstance(e, dict) or not isinstance(e.get("host"), str) or not e["host"]:
                continue
            out[e["host"]] = {
                "ip": e.get("effective_ip") or e.get("ip") or "",
                "status": e.get("status") or "idle",
                "pending": e["host"] in pending,
            }
        return out

    def refresh_entries(self, hosts) -> "tuple[int, int]":
        """手动「更新」：让这些域名**立刻**重新解析（不等自动更新间隔）。

        返回 ``(受理条数, 被唤醒的服务数)``。第二个为 0 ＝ 解析服务没在托管（组件被禁用
        / 服务已熔断）——**这不是错误**，请求仍记着，等它跑起来自然会解析；界面据此如实
        说「已登记」，而不是谎称「正在更新」。

        本方法自己**再校验一遍资格**（托管 且 自动更新）：管理窗那枚按钮的可用性判据
        在界面侧，而这里是真正会发网络请求的地方 —— 界面判据哪天写坏，外部条目就会被
        拿去解析（拿别人的域名做 DNS 查询，既没必要也说不通）。

        解析本身不在这里做：登记 ＋ 把长驻服务**提前一拍**（``wake_service``），那一轮
        ``poll()`` 走的是与自动轮次**完全相同**的一条路径（UI 线程禁网络）。
        """
        entries = store.load_entries()
        auto_hosts = {e["host"] for e in entries if e.get("auto")}
        wanted = [h for h in (str(x).strip().rstrip(".").lower() for x in hosts) if h in auto_hosts]
        if not wanted:
            return 0, 0
        accepted = self._resolve_service.request_now(wanted)
        woken = self.wake_service(self._resolve_service.name)
        return accepted, woken

    # ---------- §9.5 配置默认值单一真源 ----------

    def config_schema(self) -> FormSchema:
        """hosts 托管的默认值（§9.5 单一真源）。

        2026-09-20 从 ``config.default.yaml`` 迁来，出厂文件从此只留框架保留键
        ``enabled``；纯搬家，值逐字相同。

        默认值**引用 ``models.DEFAULT_RESOLVE_POLL``**，不再写一个字面 ``300``：
        那个常量同时是 ``resolve_poll()`` 的兜底与下限校验的来源，此处另抄一份
        就等于把刚清掉的「两处默认值」换个地方重演。
        """
        return FormSchema(
            plugin_id=self.id,
            version=1,
            fields=(
                FieldSpec(
                    key="resolve_poll", label="自动更新间隔（秒）",
                    type=FieldType.NUMBER, default=DEFAULT_RESOLVE_POLL,
                    tooltip=f"DNS 重新解析的间隔，最小 {MIN_RESOLVE_POLL} 秒（越界回退默认值）。"
                            "不是框架轮询节拍 —— 那是另一个键 poll（决议 D27）。",
                ),
            ),
        )

    # ---------- 配置 ----------

    @property
    def resolve_poll(self) -> int:
        """自动更新（DNS 重新解析）间隔，秒。

        读 ``resolve_poll``，**不读** ``poll``——后者是框架轮询 ``tick()`` 的间隔
        （决议 D27：一个配置键只服务一个消费者）。越界 / 写坏一律回退默认值。

        两档失败分在两个地方：「写坏」（类型不对）已由 ``cfg`` 按声明回落到默认值；
        本方法只管「越界」（写了数字但小于下限）—— 后者是一档**取值域**判断，
        而 ``FieldSpec`` 不表达上下限，故留在这里，回落值取声明默认（不另抄一个数字）。
        """
        v = self.cfg.value("resolve_poll")
        return v if v >= MIN_RESOLVE_POLL else self.cfg.default_of("resolve_poll")

    # ---------- 生命周期 ----------

    def start(self) -> None:
        try:
            self.sync()
        except Exception:  # noqa: BLE001 —— 同步失败不应拖垮整个插件加载
            log.exception("同步 hosts 规则失败（功能可能未生效）")
        # 状态行刷新由框架经事件总线统一处理（见 panel._recompute_plugin），
        # 插件不再自订阅自身 status topic。
        try:
            if not getattr(self, "_cfg_conn", False):
                self.ctx.config.changed.connect(self.on_config_changed)
                self._cfg_conn = True
        except Exception:  # noqa: BLE001
            log.exception("订阅配置热重载失败")

    def stop(self) -> None:
        # 只在 start() 真正连过时断开：未启动 / 启动失败 / 重复 stop 都不去碰信号，
        # 否则 PySide 会对不存在的连接发 "Failed to disconnect" RuntimeWarning。
        if getattr(self, "_cfg_conn", False):
            try:
                self.ctx.config.changed.disconnect(self.on_config_changed)
            except Exception:  # noqa: BLE001
                pass
            self._cfg_conn = False

    # ---------- 长驻服务（框架托管，批次 5 后段定案） ----------

    def resident_services(self) -> list:
        """框架托管的长驻服务：hosts 解析（周期型）。

        暂停/禁用时也要跑吗？——hosts 没有「暂停」语义（只有 enabled 开关），
        关掉时条目为空、无到期域名，服务自然空转。
        """
        return [self._resolve_service]

    def on_config_changed(self, _payload=None) -> None:
        # 只重渲染（用缓存，不发网络）；新 auto 条目的首次解析交给长驻服务，
        # 避免配置热重载把网络请求塞进 UI 线程。
        self.sync()
        # 配置变了但面板状态没跟 ⇒ 用户手改 YAML / 恢复默认后最长 300s 不刷新
        # （suggested_poll=300）。与 save_resolve_poll / _on_entries_changed 对齐。
        self._state = _observed_state(self)
        self.publish_snapshot()

    def on_disabled(self) -> None:
        """禁用时撤掉托管块：声明一份「空块」意图，helper 把标记块从 /etc/hosts 移除。

        条目主数据保留（用户下次启用时配置还在），只是不再生效。

        **不必额外「叫一轮」**：``enabled`` 是意图正文里的字段，一翻内容就变了 ⇒ 声明
        发出去，编排层立刻把它排成到期。
        """
        self._publish_intent({
            "version": resolve.WIRE_VERSION, "enabled": False,
            "marker": BLOCK_MARKER, "content": "",
        })

    # ---------- 可插拔协议（§9.1，批次 6c） ----------

    def resources(self):
        """条目主数据 + /etc/hosts 托管块声明。

        ``data_files`` 从四项收到**一项**（2026-09-29 通道换成会话）：从前还有
        ``hosts_rules.json``（期望态）、``hosts_resolved.json``（解析缓存）、
        ``hosts_state.json``（观测）—— 三份都是**通道或缓存**。会话落地后前两份没有对象
        （意图走 ``intent.set``、解析结果活在内存里），第三份换成 ``event.push``。
        只剩 ``hosts_entries.json``：那是**用户数据**（他亲手填的条目，条目可上千条，
        故不并进 config），丢了会看见与事实不符的东西。
        """
        return PluginResources(
            data_files=["hosts_entries.json"],   # 条目主数据（用户数据）
            helper_tasks=["hosts_guard.sync"],   # 编排声明（§7.4.4）
            system_mutations=[
                SystemMutation(
                    description="/etc/hosts 中的 BarKit 托管块（卸载时经 helper 摘除）",
                    survives_app_exit=True,
                )
            ],
        )

    def teardown(self) -> None:
        """卸载时复原系统状态：摘除 /etc/hosts 托管块（与 on_disabled 同动作，幂等）。"""
        self.on_disabled()

    # ---------- 期望态（app 声明 / helper 消费，零授权） ----------

    def _publish_intent(self, payload: dict) -> "tuple[bool, str | None]":
        """声明 hosts 期望态（唯一出口：``sync()`` 与长驻服务都收敛到这里）。

        返回 ``(是否已送达, 原因)``：**未连接不算错误** —— 意图已记在会话侧，守护一起来
        会自动补发（幂等靠内容哈希），故调用方只把原因记进日志。
        """
        sent = self.declare(HOSTS_SUBJECT, payload)
        if sent:
            return True, None
        return False, "守护未连接（已记下，连上即补发）"

    def sync(self) -> tuple[bool, str | None]:
        """配置 + 条目 + 解析缓存 → 期望态 → **声明给守护**。

        **不发起网络解析**（``resolve=False``）：解析是长驻服务的职责。UI 动作与长驻
        服务因此共用同一条路径（``HostsResolveService.poll``），只是 ``resolve=False``
        保证 UI 线程不被网络请求阻塞。条目非法时整份拒绝：宁可不更新，也不发一份
        helper 会拒收的意图。
        """
        self._resolve_service.poll(force=True, resolve=False)
        return self._last_error is None, self._last_error

    def _build_payload(self, resolve_due: bool = True) -> tuple[dict | None, str | None]:
        """构造完整期望态：条目 + 解析缓存 → 每条 ``effective_ip`` → **块正文**。

        档 B（S3b-2）：解析链与**渲染**都归 app（§7.4.3）——``content`` 是渲染好的
        块正文，对应 §7.4.4 ``file.write_block`` 的 ``content`` 语义。

        ``resolve_due=False`` 时一条都不解析、只用缓存（UI 线程安全）。副作用：
        把算出的条目（带 status）存进 ``self._entries``，供状态解释使用。

        「手动更新」（管理窗那枚按钮）登记在 ``self._resolve_service`` 里的域名**不受
        自动间隔约束**，本轮就解析；但它同样只在 ``resolve_due=True`` 这条路上生效 ——
        UI 取数那条路在主线程，一条网络也不能发。
        """
        raw = {
            "version": 2,
            "enabled": self.enabled,
            "poll": self.resolve_poll,        # 节流用的解析间隔（**app 侧**读它，见
                                              # ``resolve.due_hosts``；helper 不读这个键，
                                              # 它每轮拿到的是已经渲染好的正文）
            "categories": store.load_categories(),
            "entries": store.load_entries(),
        }
        data, err = normalize_rules(raw)
        if err is not None:
            return None, err
        due: set[str] = set()
        if resolve_due:
            due = resolve.due_hosts(data["entries"], self._cache, self.resolve_poll)
            due |= self._resolve_service.pending_hosts()
        entries = resolve.apply_resolutions(data["entries"], self._cache, to_resolve=due)
        self._entries = entries
        # 禁用时空正文 = 没有期望 → helper 摘掉整个块（与 on_disabled 同语义）
        content = resolve.render_block(entries) if self.enabled else ""
        return ({
            "version": resolve.WIRE_VERSION,
            "enabled": self.enabled,
            "marker": BLOCK_MARKER,
            "content": content,
        }, None)

    def _service_provider(self, resolve_due: bool) -> dict | None:
        """长驻服务的 provider：**纯计算、不落盘**（落盘归 ``HostsResolveService``）。

        异常不抛出：交给宿主的 ``report_problem`` 变成设置页可见的「插件问题」。
        """
        try:
            payload, err = self._build_payload(resolve_due)
        except Exception as e:              # noqa: BLE001 —— 长驻线程不能因异常中断
            self._last_error = str(e)
            self._report(f"hosts 期望态计算失败：{e}")
            return None
        if err is not None:
            self._last_error = err
            self._report(f"hosts 条目非法：{err}")
            return None
        self._last_error = None
        return payload

    def _report(self, message: str) -> None:
        """向宿主上报问题（未托管时退化为日志）。"""
        hook = getattr(self._resolve_service, "report_problem", None)
        if callable(hook):
            hook(message)
        else:
            log.error("%s", message)


    # ---------- 状态快照（芯片 / 徽标 / 状态行共用单一真相）----------

    def _snapshot(self) -> StateReport:
        """把当前 hosts 托管状态映射为一份报告（只报**取值名**，决议 D13）。

        取值闭集见模块顶的 ``STATES``：级别归声明，本方法只回答「现在是哪一格」。
        summary 是**唯一文案**（决议 D18）：面板行副标题与两处悬停提示共用这一句；
        「下一步做什么」的引导不写在这里——安装走设置页的按钮、加条目走整行点击。
        失联由状态机的 stale_after 兜底（此处 stale_after=None，靠 mtime 自检测）。
        """
        st = self._state
        installed = self._daemon_status.get("installed")

        if not installed:
            return StateReport("no_daemon", "hosts 守护未装", updated_at=time.time())
        if not st.get("configured", True) and not store.load_entries():
            return StateReport("unconfigured", "hosts 未配置", updated_at=time.time())
        err = st.get("error") or getattr(self, "_last_error", None)
        if err:
            return StateReport("error", f"规则异常 · {err}", updated_at=time.time())

        # 条目异常由 app 自己判定（§7.4.3：状态解释归 app）——helper 只回报
        # 「写了什么 / 是否成功」，不再回报每条的状态。
        entries = self._entries
        managed = len(store.load_entries())
        bad = [e for e in entries if e.get("status") in ("stale", "fake")]
        if bad:
            kinds = "、".join(sorted({e.get("status") for e in bad}))
            names = "、".join(str(e.get("host", "?")) for e in bad[:5])
            return StateReport(
                "entries_bad",
                f"{managed} 条 · {len(bad)} 条异常：{names}（{kinds}）",
                updated_at=time.time(),
            )
        auto_n = sum(1 for e in store.load_entries() if e.get("auto"))
        return StateReport(
            "synced", f"{managed} 条 · 已同步（{auto_n} 条自动更新）", updated_at=time.time(),
        )

    # ---------- tick（worker 线程，scheduler 驱动）----------

    def tick(self) -> StateReport:
        # 事实是推来的：``_observed_state`` 读内存里那份（会话收到即更新）。仍每轮取一次，
        # 因为推送与 tick 之间可能有别的东西改过它，且构造期那份是降级结构。
        self._state = _observed_state(self)
        self._daemon_status = daemon.status()
        # §5.3：返回快照，框架自动 publish 到 plugin.hosts_guard.status.main
        return self._snapshot()

    # ---------- 与守护的会话（订阅与事实） ----------

    def intent_subject(self) -> str:
        """本组件的期望态挂在 ``hosts`` 上（见 ``HOSTS_SUBJECT``）。"""
        return HOSTS_SUBJECT

    def subjects(self) -> list[str]:
        """声明本组件要 ``hosts`` 这个领域的最新事实（取代旧的目录监视）。"""
        return [HOSTS_SUBJECT]

    def on_observed(self, subject: str, observed) -> None:
        """守护推来一份 hosts 观测（跑在总线线程 = 主线程）：**只标记脏**。

        完整取值走 ``observed("hosts")``（``tick()`` 里现取），参数只是「变了」这件事的
        通知。改内存 / 读盘 / 子进程一概不留在这里 —— 那会卡住 UI 线程（§8.2.2）。
        """
        if subject == HOSTS_SUBJECT:
            self.mark_dirty("helper_observed")

    # ---------- 手动动作（主线程）----------

    def immediate_check(self) -> None:
        """「立即检查」：重发当前期望态 + 叫守护**现在**跑一轮渲染落盘。

        真正的 DNS 解析归长驻服务（``sync`` 走 ``resolve=False``，不发网络请求）。
        新到期的条目由服务在 ≤``PROBE_INTERVAL`` 秒内解析并重发，故本动作不阻塞 UI 线程。

        为什么「重发一遍」不够：内容没变的话 ``declare`` 一个字都不发（按内容哈希幂等），
        而用户按下这个键要的正是「现在跑一轮」⇒ 补一条 ``resync`` 命令。基类那份实现
        就是这个动作，这里覆写只是为了先把派生内容算齐（``sync()``）再喊。
        """
        self.sync()
        self._state = _observed_state(self)
        self.publish_snapshot()
        super().immediate_check()

    # ---------- 装置运维（安装 / 卸载 / 立即检查） ----------
    # 实现与接线归 `capabilities.helper_ops.HelperOpsCapability`（三个装同一个 root helper
    # 的组件共用一份）；本组件原先**自己手搓过一套**（`daemon_state()` ＋ 只装不卸），
    # 2026-09-28 统一后那套整体删除 —— 设置页那张卡改由框架按声明注入到**页尾框架区**
    # （原先它长在页首，是三个组件三种长相里最不像的那一种）。
    # 顺带多出来一枚「卸载守护」：三个组件装的是同一个 helper，能装就该能卸。

    def helper_notice(self) -> "tuple[str, bool]":
        """装置运维卡那块警示：``(文案, 是否要出「重装守护」)``。

        ⚠️ 与另两个组件的**形状不同**（统一时定）：本组件原来在设置页**页首**常驻一行
        「状态：守护已安装 v… · 运行中」，统一后那一行归框架那块警示区 —— 而警示区是
        **有问题才喊**（空串即整块隐藏、字色是警示用的红）。故「已安装 · 运行中」这种
        常态信息**不再由这里给**：它归状态机段（页首「当前」那一栏）与面板那一行。
        这里只报「需要用户做点什么」的两种局面。
        """
        st = daemon.status()
        if not st.get("installed"):
            return ("系统守护未安装。hosts 托管要它才能写入 /etc/hosts，"
                    "点「安装守护」完成一次系统授权即可。", False)
        if st.get("outdated"):
            return (f"守护版本过期（当前 {st.get('version')}，期望 {st.get('expected')}）"
                    "—— 需重装后才能使用 hosts 托管。", True)
        return ("", False)

    def on_helper_change(self) -> None:
        """装 / 卸 / 立即检查之后：重读守护状态 ＋ 重发期望态 ＋ 广播一台机。

        装完之后不必额外叫一轮：新守护连上来时框架会把本组件声明过的意图重发一遍，
        内容对它是「从无到有」⇒ 编排层立刻把它排成到期。
        """
        self._daemon_status = daemon.status()
        self.sync()
        self.publish_snapshot()

    def helper_ops_texts(self) -> HelperOpsTexts:
        """装置运维那几句话。

        ⚠️ ``question`` 与另两个组件不同，是**故意**的：``daemon.uninstall()`` 只拆框架
        自己的东西（plist / 启动器 / A 类二进制），**不撤插件写下的系统状态** —— 而
        /etc/hosts 里那块托管内容是**静态文本**，守护没了它照样生效。这里如实说。
        另两个组件的既有问句（「删除已有的规则路由 / iPad 隧道」）本批**照旧搬过来没动**，
        那是另一条待裁的点（挂账见任务板 §5），不在本批。
        """
        return HelperOpsTexts(
            installed="守护安装成功，hosts 托管已可用。",
            uninstalled="守护已卸载（/etc/hosts 里已有的托管块未动）。",
            question="将停止守护并删除本机守护文件。/etc/hosts 里已有的托管块不会被移除、"
                     "仍会生效 —— 要撤掉它请在「插件」页停用本组件。确定卸载？",
            note="「立即检查」会重发一遍当前配置并让守护立刻重跑一轮"
                 "（重新渲染 /etc/hosts 里的托管块）。",
        )

    # ---------- UI 贡献（声明点方法，决议 D20）----------

    def row_specs(self):
        """状态面板行：声明行本身（key="main"）。

        整行点击**不由本行声明** —— 框架发现组件声明了 ``DetailEntrySpec`` 会合成
        「打开详情页」（契约 §2.4 / UI 规范 §5.3），点整行即进入 hosts 管理窗。
        旧写法把 ``on_click`` 接到自建窗口的打开方法上，是「组件自己 new 窗口、自己管
        Dock 显隐」的旁路（与迁移前的 recognize 同款）；现统一收敛到 ``DetailHost``。
        """
        return (
            StatusRowSpec(
                key="main",
            ),
        )

    def detail_entry(self):
        """详情页 = hosts 记录管理窗（整行点击进入，统一走框架 ``DetailHost``）。

        2026-09-20 从 ``StatusRowSpec(on_click=...)`` 旁路迁到
        ``DetailEntrySpec(presentation="window")``：整行点击的弹窗不再各自 new 窗口、
        自己管 Dock 显隐，而与组件行点击收敛成同一条链（面板收起 → 发 detailRequested
        → ``DetailHost.open`` → ``DetailForm`` 托管）。2026-09-22 去掉 ``trigger="row"``
        （字段已删）：**行与底部快捷区域是同一个过程**，两个入口都归用户层的显示位开关管。

        **本详情页是该管理面的唯一入口**：设置页原那枚「管理条目…」按钮**已删** —— 它与本
        入口会各开一份内容面，两份**互不感知**（一份看不到另一份刚做的改动）；而「插件主动
        打开自己的详情页」的框架接口还没有（登记在排期表 §S2 同族）⇒ 留着那枚按钮就只能
        留着两份内容面。与 ``route_guard`` 详情页同款：集合编辑器只从面板整行进，设置页
        不放启动键（设置页管**配置**、任务流归**详情卡**）。

        ``build`` 是零参工厂（框架按 ``DetailEntrySpec.build`` 零参调用），用闭包把插件
        方法 ``status_index``（app 侧那份条目状态，UI 不得直读数据文件）、
        ``_on_entries_changed``（改动后同步 + 触发 helper）与 ``refresh_entries``
        （手动「更新」：立刻重解析选中的自动更新条目）注入内容面；框架打开时才调用，
        故读到的是新鲜状态。
        ``label`` 留空 —— 单条入口时框架补 ``Plugin.name``（「hosts 托管」），写了反而
        触发名字位门禁 ``test_identity_name_is_not_written_twice``。
        """
        return DetailEntrySpec(
            key="main",
            presentation=DETAIL_PRESENTATION_WINDOW,
            # 七列表格 ＋ 一排过滤 / 动作：默认初宽 620 里工具条挤不下，两个下拉会被
            # 压成两种宽度（用户 2026-09-22 上报）。860 是加上「更新」之后**量的**
            # （工具条自然宽 786 ＋ 内容区左右各 16 的边距 ＝ 818，再留一档余量给字体
            # 差异与以后微调 —— 挤不下时 Qt 会把两个下拉压窄，那正是最初那个观感问题）。
            width=860,
            build=lambda: HostsManagerContent(
                status_index=self.status_index,
                on_changed=self._on_entries_changed,
                on_refresh=self.refresh_entries,
            ),
        )

    def component(self):
        """标题栏组件图标（每插件一枚，U2 §5.2）：身份的唯一真源（D14），名字省略取 Plugin.name。"""
        return ComponentSpec(icon=IconName.DNS)

    def state_specs(self):
        """状态声明（静态）：标题栏图标颜色 + 行角标由此派生（§4.3）。

        states 是取值闭集（决议 D13）；initial 是首屏起点（取值名，不是工厂）。
        同一组件只有一台状态机 ⇒ 状态名不必写（省略即取 Plugin.name，D14）。
        """
        return (
            StateSpec(
                key="main", icon=IconName.DNS,
                states=STATES, initial="unknown",
                stale_after=None,
            ),
        )

    def settings_page(self):
        """设置页。规则异常 / 落盘失败（ERR）进托盘队列——取值自己说了算
        （``StateOption.alerts``，ERR 默认即为真，决议 D17）。"""
        return SettingsPageSpec(
            build=self._build_settings_page,
            reason="custom: 守护状态展示 + 说明 + 管理窗口入口，非字段表单",
        )

    def _build_settings_page(self):
        """设置页的稳定门面（框架按 ``SettingsPageSpec.build()`` **零参**调用）。

        UI 本体在 ``settings.py``（§7.2：设置面的全部界面归它），本方法只做转发 ——
        与 ``route_guard`` / ``ipad_tunnel`` / ``recognize`` 同一条边界：插件是唯一
        知道该把什么注入界面的一侧。2026-09-19 由此让本模块运行期零 PySide6。
        """
        return settings_ui.build_page(self)

    # ---------- 设置面的数据面（§7.2：UI 只画，取数留在这里）----------

    def save_resolve_poll(self, seconds: int) -> bool:
        """写回「自动更新间隔」（秒）→ 重算期望态并声明出去（``sync``）。

        值先夹到 ``MIN_RESOLVE_POLL`` 以上：``models.normalize_rules`` 对越界值会
        静默回落默认值，那会造成「界面显示 30、实际跑 300」—— 在 UI 这一层就夹住，
        用户看到的就是会生效的那个数。

        写盘与生效**分别**说话：写盘失败是「没存下」，规则被拒是「存下了但没生效」
        （条目非法时 ``sync`` 整份拒绝，见它的 docstring）。
        """
        value = max(MIN_RESOLVE_POLL, int(seconds))

        try:
            self.cfg.set("resolve_poll", value)
        except Exception as e:  # noqa: BLE001
            log.error("写 hosts 配置失败：%s", e)
            self._say_error("保存失败——写回配置没成功，请重试。")
            return False
        ok, err = self.sync()
        self._state = _observed_state(self)
        self.publish_snapshot()
        if not ok:
            self._say_error(f"间隔已保存，但规则未生效：{err}")
            return False
        self._say_info(f"已保存：自动更新间隔 {value} 秒。")
        return True

    # ---------- 动作（主线程）----------

    def _on_entries_changed(self) -> None:
        """管理窗口改动条目后：重新同步并触发 helper 立即执行。

        条目一变，期望态内容通常也变 ⇒ ``sync()`` 那一下就是「立刻执行」。但「把某条从
        auto 改成 static、算出来的正文恰好一字不差」这类改动会得到同一份正文 —— 那时
        仍要补一条 ``resync``，因为用户改完就在等效果。
        """
        self.sync()
        self.run_op("resync", {"subject": HOSTS_SUBJECT})
        self._state = _observed_state(self)
        self.publish_snapshot()

    # ---------- 供管理窗口查询 ----------

    def external_records(self):
        """/etc/hosts 中未被托管的条目（供管理窗口展示与接管）。"""
        records, _err = read_records()
        managed = {e["host"] for e in store.load_entries()}
        return [r for r in records if r.host not in managed]
