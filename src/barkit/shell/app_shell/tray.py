"""托盘底图与角标：状态事件到底图、角标的全部重算入口。

``app_shell.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from barkit.contribution.specs import (
    IconName,
    StateReport,
)
from barkit.paths import APP_NAME
from barkit.shell.renderers.badge import (
    BROKEN_TEXT,
    Candidate,
    tray_queue,
)
from barkit.shell.renderers.representative import representative
from barkit.shell.renderers.state_view import resolve

from .consts import log


class _TrayMixin:
    """状态 → 菜单栏的重画：底图来源四层回落、角标队列与各处重算入口（状态事件 / 失联翻转 / 配置热重载）。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 托盘底图换源（P0③） ----------

    def _resolve_tray_base_spec(self, state_id: str):
        """按**状态机身份**取回来源声明；未配置 / 不在册 → ``None``。

        与设置窗枚举候选**同一个判据来源**（``ContributionRegistry.state_by_id``），
        两处不各写一份 —— 否则会出现「设置窗已回落默认、菜单栏还画着旧图」。
        """
        if not state_id or self._contributions is None:
            return None
        return self._contributions.state_by_id(state_id)

    def _apply_tray_base(self) -> None:
        """按 ``tray.base_source`` 决定菜单栏**主图标**（状态机图标 vs BarKit 默认底图）。

        配置存的是状态机**身份**（``StateSpec.state_id``），不是事件总线 topic；
        先换回声明，再决定画哪枚。回落规则是**硬性**的（缺一条就是「菜单栏那一格
        撒谎 / 消失」）：

        | 情形 | 结果 |
        |---|---|
        | 未配置来源 | BarKit 默认底图 |
        | 来源状态机不在册（组件被禁用 / 卸载） | BarKit 默认底图 |
        | 该状态还没有报告 | 该状态机声明的**主图标** |
        | 该取值没给取值图标，或该状态已失联 | 同上一行（由 ``resolve`` 回落，见下） |

        后两行是首版没有的一层：**有声明的主图标就不必掉到 BarKit 默认底图**。
        尤其失联 —— 继续画上一次的取值图标（如「实心屏」）才是撒谎；回落到状态机
        自己声明的主图标（如「空心屏」）既如实、又仍是**该组件**的信息，
        比换成 BarKit 自己的牌子更少误导。

        取值图标怎么算**不在这里写第二遍**：走 ``state_view.resolve``（决议 D13 的
        单一解析点，与面板行 / 代表态同源），它已经把「取值没给图标」与「已失联」
        两级回落做完。

        「失联」不另起一套时钟：直接问面板（``panel.is_stale``，它是
        ``StateSpec.stale_after`` 的唯一执行者），否则会出现「面板标失联、
        菜单栏还显示已连接」。

        本方法是**唯一**的底图决策点：状态事件、失联翻转、配置热重载、
        设置窗改选都汇到这里；实际重绘由 ``StatusItem.set_base_icon`` 去重。
        """
        icon: IconName | None = None
        state_id = self._config.tray_base_source() if self._config is not None else ""
        spec = self._resolve_tray_base_spec(state_id)
        if spec is not None:
            report = self._state_reports.get(spec.topic)
            if report is None:
                icon = spec.icon                      # 还没有报告：只画状态机的主图标
            else:
                stale = self.panel is not None and self.panel.is_stale(spec.topic)
                icon = resolve(spec, report, stale=stale).icon
        if self.status_item is not None:
            self.status_item.set_base_icon(icon)
        if self._tray is not None:
            self._tray.setIcon(self._make_tray_icon(icon))

    # ---------- 托盘徽标 ----------

    def _build_badge(self, contributions, scheduler, panel) -> None:
        """算出托盘角标：挑出「最该上屏的那个组件」（决议 D17）。

        规则三步（候选 → 触发 → 取胜）都在 ``renderers.badge.tray_queue`` 里，本方法
        只负责**凑候选**与**把结果交给状态项**：

        - 候选 = 每个组件的代表态（``representative`` ＋ ``state_view.resolve``），
          与面板行角标、标题栏图标同一个解析点 —— 三处不会算出不同结论；
        - 熔断（``scheduler.broken``）走即时通道：组件崩了立刻按 ERR 进队，不等
          ``stale_after`` 超时（用户裁定 2026-09-15：「出事一定看得见」不降级）。
        """
        self._contributions = contributions
        panel.stateLevelChanged.connect(self._on_state_level_changed)
        # 存成实例属性，便于 _shutdown 精确断开（匿名 lambda 无法再 disconnect）
        self._sched_broken_slot = lambda pid, err, c: (
            self._broken_ids.add(pid), self._push_badge()
        )
        self._sched_recovered_slot = lambda pid: (
            self._broken_ids.discard(pid), self._push_badge()
        )
        scheduler.broken.connect(self._sched_broken_slot)
        scheduler.recovered.connect(self._sched_recovered_slot)
        self._push_badge()  # 首屏（还没有报告，先算一次「无触发者」兜底）

    def on_state_update(self, spec, payload) -> None:
        """状态事件统一入口：更新面板图标角标 + 重绘托盘底图与角标。

        E3 ② 起状态的订阅 / 反订阅 / 首屏绘制全部收归 PluginManager 编排，
        此处只负责「收到事件后怎么画 + 徽标联动」，不感知插件生命周期。

        级别不在报告里（决议 D13：报告只报取值名），经 ``state_view.resolve``
        从声明解析 —— 与面板、托盘底图同一个解析点。
        """
        self.panel.update_state_snapshot(spec, payload)
        if isinstance(payload, StateReport):
            # 级别与取值图标都靠解析（D13）：这里只缓存报告本体，消费点各自解析
            # （托盘底图走 _apply_tray_base、托盘队列走 _tray_candidates）。
            self._state_reports[spec.topic] = payload
        self._apply_tray_base()
        self._push_badge()
        # 设置窗开着 ⇒ 把新值推给它的「状态机」段：当前生效值是运行期数据，只在开页时
        # 拍一次快照会得到一屏静止的谎（看着「正常」，其实已经红了）。窗口没开就不推。
        if self._settings is not None and self._settings.isVisible():
            self._settings.refresh_state_values()

    def _on_state_level_changed(self, topic: str, level: str) -> None:
        """面板侧改判（目前只有「状态失联」）→ 重算托盘底图与角标。

        面板自己改图标角标时（``_check_stale`` 的失联兜底）不会走事件总线，若不在这里
        补一次，就会出现「面板已标红、托盘还显示正常」的不一致。

        ``level`` 只用于日志：托盘的判据一律现读 ``panel.is_stale``（与
        ``_apply_tray_base`` 同源），不在这里另存一份级别。
        """
        log.debug("面板改判 %s → %s，重算托盘", topic, level)
        # 失联翻转会改变底图的回落结果（§5.2.2-③ 的硬性回落规则之一）
        self._apply_tray_base()
        self._push_badge()

    def _push_badge(self) -> None:
        """重算托盘队列并把队首交给状态项（决议 D17）。

        队列 = 「最坏那一级的触发者」，同级内按 ``plugin_order`` 排队。轮播是
        本批的下一个原子步（3 秒计时器在队内轮换），今天恒取队首。
        """
        self._tray_queue = tray_queue(
            self._tray_candidates(),
            self._registry.plugin_order() if self._registry is not None else [],
            min_level=self._config.tray_min_level() if self._config is not None else "warn",
        )
        self._apply_tray_badge()

    def _on_config_changed(self, _data=None) -> None:
        """配置热重载（手改 YAML）后的统一重画入口。

        ``Config.changed`` 只由**文件监视**发出：程序内写配置走 ``save_user``，写完
        顺手把 mtime 记下了，不会误触发这一路。故设置窗改完仍靠自己的回调（见
        ``_build_settings``），两条路径互不重复。
        """
        self._apply_tray_base()
        self._refresh_display_slots()
        # 变量池按新声明重来一遍（订阅面与 ask 询问规则都跟着变）。与设置页写回那条路
        # 走**同一个方法** —— 两处各写一遍的话，迟早只有一处被改到。
        self._on_variables_config_changed()

    def _ui_visible(self, plugin_id: str, slot: str) -> bool:
        """用户层显示位开关；未装配配置（部分单测路径）时一律可见。

        判据不在本处另写一份：直接问 ``Config.plugin_ui_visible``，与面板注入的是
        同一个方法（两处不会一个说关、一个说开）。
        """
        if self._config is None:
            return True
        return self._config.plugin_ui_visible(plugin_id, slot)

    def _state_priorities_of(self, plugin_id: str):
        """该组件写过的状态机优先级覆盖（``{状态机 key: int}``）。

        没接配置 / 读失败 ⇒ ``None`` ⇒ ``representative`` 回落**声明顺序**：读一个用户态
        的覆盖值失败，代价只是「顺序没按用户意思来」；抛出去则是「角标没了」——
        与 :meth:`_ui_visible`「读失败按可见」同一口径。
        """
        if self._config is None:
            return None
        try:
            return self._config.plugin_state_priorities(plugin_id)
        except Exception:  # noqa: BLE001
            log.exception("读取 %s 的状态机优先级失败，按声明顺序处理", plugin_id)
            return None

    def _refresh_display_slots(self) -> None:
        """用户改了显示位开关后重画四处（标题栏图标条 / 面板行 / 快捷区域 / 托盘角标）。

        只重注入贡献与重算队列：**不碰** scheduler、订阅与插件实例 —— 隐藏 ≠ 停用
        （决议 D4 约束 ①），关掉一个显示位不该让组件的 tick / 副作用跟着停。走
        ``set_contributions`` 是因为标题栏、状态行与操作区按钮是同一批声明的三个面
        （与 E3 ② 插件增删后走的是同一条刷新路径）。
        """
        if self.panel is not None and self._contributions is not None:
            self.panel.set_contributions(self._contributions)
        self._push_badge()

    def _apply_tray_badge(self) -> None:
        """把队首画到菜单栏角标上；队列为空则撤掉角标并复位悬停提示。"""
        if self.status_item is None:
            return
        if not self._tray_queue:
            self.status_item.set_badge(None)
            self.status_item.set_tooltip(APP_NAME)
            return
        head = self._tray_queue[0]
        self.status_item.set_badge(head.level, head.icon)
        # 悬停提示跟随当前上屏的那个组件（D17 硬要求 7）
        self.status_item.set_tooltip(head.tooltip)

    def _tray_candidates(self) -> list[Candidate]:
        """凑出每个**组件**的候选：代表态（级别 / 取值图标 / 提醒意图）＋ 是否熔断。

        级别与图标都经 ``state_view.resolve`` 回查声明（决议 D13 ＋ D17），失联由框架
        判（``panel.is_stale``）—— 与 ``_apply_tray_base`` 同一口径，两处不会打架。
        """
        if self._contributions is None:
            return []
        specs = {s.topic: s for s in self._contributions.states()}
        names = {c.plugin_id: c.name for c in self._contributions.components()}
        out: list[Candidate] = []
        for plugin_id in dict.fromkeys(s.plugin_id for s in specs.values()):
            # 用户关掉了这个组件的托盘显示位：不参与挑选（决议 D4 约束 ① 只关渲染，
            # 故这里只跳过候选，状态机 / 订阅 / 调度一概不动）
            if not self._ui_visible(plugin_id, "tray"):
                continue
            rep = representative(
                plugin_id,
                self._state_reports,
                specs,
                priorities=self._state_priorities_of(plugin_id),
            )
            if rep is None:
                continue
            spec, report = rep
            stale = self.panel is not None and self.panel.is_stale(spec.topic)
            rs = resolve(spec, report, stale=stale)
            broken = plugin_id in self._broken_ids
            name = names.get(plugin_id, plugin_id)
            out.append(Candidate(
                plugin_id=plugin_id,
                level=rs.level.value,
                alerts=rs.alerts,
                # 熔断的组件画不出「现在的取值」——按级别通用标记画，不拿上一次的取值图标充数
                icon=None if broken else rs.icon,
                tooltip=f"{name}：{BROKEN_TEXT if broken else rs.text}",
                broken=broken,
            ))
        return out

    def _paint_initial_states(self, contributions) -> None:
        """首屏状态绘制：给每台状态机画**起点那一格**（托盘随之刷新）。

        ``StateSpec.initial`` 是取值名（决议 D13：首屏只要「从哪一格开始」，不需要
        整份报告工厂），留空取声明的第一个取值。真实读数由首轮 tick 补上 ——
        调度器在装配时就点第一轮，不等用户打开面板。
        """
        for spec in contributions.states():
            start = spec.initial_value
            if start:
                self.on_state_update(spec, StateReport(start))
        self._push_badge()
