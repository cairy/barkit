"""core/contributions：贡献注册中心（P0 架构主轴）。

注册中心承担「统一贡献收集 + 消费者门面」两职：
- 每个插件在通过契约版本闸门（``api_version == SUPPORTED_API_VERSION``）后，
  按声明点逐个调用其具名方法（``component()`` / ``state_specs()`` /
  ``row_specs()`` / ``detail_entry()`` / ``settings_page()`` / ``actions()`` /
  ``hotkeys()``，对应表见 ``contribution/points.declaration_points()``，决议 D20），
  收集期做返回类型 / 元素类型校验并注入 ``plugin_id``。
- 消费者（``ui/panel`` / ``ui/settings`` / ``plugin_manager`` / ``app``）纯消费下列门面：
  ``status_rows`` / ``settings_pages`` / ``primary_actions`` / ``hotkeys`` / ``states`` / ``components``
  / ``detail_entries``。

设计原则：所有贡献**可选**，框架不强制插件实现全部；只做「收集 + 排序」，不持有任何
具体插件类型，新增插件零改框架文件。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from barkit.contribution.points import SUPPORTED_API_VERSION, declaration_points
from barkit.contribution.specs import (
    ActionSpec,
    ComponentSpec,
    DetailEntrySpec,
    HotkeySpec,
    SettingsPageSpec as V2SettingsPageSpec,
    StateSpec as V2StateSpec,
    StatusRowSpec,
)

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

    from barkit.core.plugin import Plugin


def api_version_reject_reason(version: int, plugin_id: str) -> "str | None":
    """返回契约版本不兼容的拒绝原因（None 表示兼容）。

    供 ``Registry.discover``（导入期）与 ``ContributionRegistry``（注册期）共用。
    框架只认 ``SUPPORTED_API_VERSION`` 这一个版本：比它旧（且无适配层）与比它新
    （需更新 Barkit）都拒绝——区间放行会让上一个版本的插件静默通过。
    """
    if version == SUPPORTED_API_VERSION:
        return None
    pid = plugin_id or "<unknown>"
    if version > SUPPORTED_API_VERSION:
        return (
            f"需要 Barkit ≥ v{version}（当前框架支持 v{SUPPORTED_API_VERSION}），"
            f"请升级 Barkit 后重试（插件 {pid}）"
        )
    return (
        f"契约 v{version} 过旧且框架暂无适配层（已仅支持 v{SUPPORTED_API_VERSION}），"
        f"请升级该插件（插件 {pid}）"
    )


def gather_specs(plugin: "Plugin", pid: str) -> list:
    """按声明点对应表逐个调用具名方法，校验并注入 ``plugin_id``（决议 D20）。

    - ``0/1`` 声明点：返回 ``None`` = 未声明（跳过）；返回别的类型 → ``ValueError``；
    - ``n`` 声明点：必须返回 Spec 元组 / 列表；任一元素不是该声明点的 Spec
      （**拼错类名**从「静默丢弃」变成这里的一句可读报错）→ ``ValueError``；
    - ``plugin_id`` 一律由框架覆写为 ``Plugin.id``：组件写不写、写什么都以
      框架注入为准——归属漂移在机制上不可能。

    注册中心的 ``_collect`` 在崩溃隔离内调用它；测试 / 诊断需要「一个插件的
    全部 Spec」时也应走这里，别在测试里自己重拼一遍。
    """
    specs: list = []
    for point, (method_name, spec_cls, arity) in declaration_points().items():
        method = getattr(plugin, method_name, None)
        if method is None:
            # 真插件继承 Plugin 基类必有全方法；鸭子型测试桩缺方法 = 未声明该点
            continue
        result = method()
        if arity == "0/1":
            if result is None:
                continue
            if not isinstance(result, spec_cls):
                raise ValueError(
                    f"插件 {pid} 的 {method_name}() 应返回 {spec_cls.__name__} 或 None，"
                    f"实得 {type(result).__name__}（声明点 {point}）"
                )
            result.plugin_id = pid
            specs.append(result)
            continue
        # n 声明点：接受元组 / 列表；其余（含 None）都是声明写错
        if result is None or isinstance(result, (str, bytes)) or not hasattr(result, "__iter__"):
            raise ValueError(
                f"插件 {pid} 的 {method_name}() 应返回 tuple[{spec_cls.__name__}, ...]，"
                f"实得 {type(result).__name__}（声明点 {point}）"
            )
        for s in result:
            if not isinstance(s, spec_cls):
                raise ValueError(
                    f"插件 {pid} 的 {method_name}() 返回了不属于声明点 {point} 的元素："
                    f"{type(s).__name__}（应为 {spec_cls.__name__}）—— "
                    "装错 Spec 不再静默丢弃（决议 D20）"
                )
            s.plugin_id = pid
        specs.extend(result)
    return specs


class ContributionRegistry:
    """统一收集插件贡献，按 point 聚合排序，供消费者纯消费。

    - 通过版本闸门（``api_version == SUPPORTED_API_VERSION``）的插件：按声明点
      逐个调用其具名方法（决议 D20，见 :meth:`_gather`）；
    - 崩溃隔离（E3 ①）：任一插件贡献收集中抛异常 → 隔离该插件（记 failed），其余不受影响。
    """

    def __init__(
        self,
        plugins: list["Plugin"],
        order_provider: "Callable[[], list[str]] | None" = None,
    ) -> None:
        """``order_provider``：插件间排序唯一数据源（§6.4，通常传
        ``Registry.plugin_order``——读 config 顶层 ``plugin_order``）。
        None 时退化为仅按 ``Plugin.order`` 排序（测试与桩场景）。"""
        accepted: list["Plugin"] = []
        rejected: list[tuple["Plugin", str]] = []
        for p in plugins:
            reason = self._validate_plugin(p)
            if reason is None:
                accepted.append(p)
            else:
                rejected.append((p, reason))
                log.warning("跳过插件 %s：%s", self._pid(p), reason)
        self._plugins = accepted
        self._rejected = rejected
        self._order_provider = order_provider
        self._failed: set[str] = set()
        self._failures: list[tuple[str, "Exception"]] = []
        # plugin_id -> 该插件的统一贡献列表（Spec）；由 _collect 崩溃隔离填充
        self._by_plugin: dict[str, list] = {}
        for p in accepted:
            self._by_plugin[self._pid(p)] = self._collect(p)

    # ---------- 属性 ----------

    @property
    def rejected(self) -> list[tuple["Plugin", str]]:
        """被版本闸门拒绝的插件及原因，供启动时汇总提示。"""
        return self._rejected

    @property
    def failed(self) -> list[tuple[str, "Exception"]]:
        """在一次贡献收集中抛异常的插件及其首个异常，供启动汇总提示。"""
        return self._failures

    # ---------- 运行期增删 ----------

    @property
    def active_ids(self) -> set[str]:
        """当前处于激活（贡献可见）状态的插件 id 集合。"""
        return {self._pid(p) for p in self._plugins}

    def deactivate(self, plugin_id: str) -> None:
        """从活跃插件集合中移除（运行期禁用），其贡献随即从各类列表中消失。

        **失败标记也一并清**：``_collect`` 把「这个 id 失败过」当作终点
        （``pid in self._failed`` 在 ``try`` 之外就提前返回），不清的话插件一旦
        在收集期被判失败，此后哪怕故障已经消失、用户也关掉再开过，它**再也产不出
        任何贡献**——而第二次没有任何日志（早返回那条路连 ``except`` 都到不了）。
        下一次 ``activate`` 因而必须有一次重新收集的机会；判失败照旧即时生效。
        """
        self._plugins = [p for p in self._plugins if self._pid(p) != plugin_id]
        self._by_plugin.pop(plugin_id, None)
        self._failed.discard(plugin_id)

    def activate(self, plugin: "Plugin") -> None:
        """把已实例化的插件（运行期启用）加回活跃集合；重复激活幂等。"""
        pid = self._pid(plugin)
        if pid in self.active_ids:
            return
        self._plugins.append(plugin)
        self._by_plugin[pid] = self._collect(plugin)

    # ---------- 版本闸门 ----------

    @staticmethod
    def _validate_plugin(plugin: "Plugin") -> "str | None":
        return api_version_reject_reason(
            getattr(plugin, "api_version", SUPPORTED_API_VERSION),
            getattr(plugin, "id", None) or type(plugin).__name__,
        )

    # ---------- 统一收集（按声明点，决议 D20） ----------

    @staticmethod
    def _pid(plugin: "Plugin") -> str:
        return getattr(plugin, "id", None) or type(plugin).__name__

    def _collect(self, plugin: "Plugin") -> list:
        """取该插件全部贡献（按声明点逐个调用，崩溃隔离 + 收集期 fail-fast）。

        框架按 ``declaration_points()`` 的对应表逐个调用插件的具名声明方法
        （决议 D20），并做三件事：返回类型 / 元素类型校验（装错 Spec **不再
        静默丢弃**，而是报错并隔离该插件）、``plugin_id`` 注入（身份唯一真源
        从约定升级为机制，决议 D14 / D20）、基数由方法签名天然保证（0/1 声明点
        只可能产出零或一枚）。

        任一声明点抛异常 / 校验不过的插件在此被隔离，不影响其余插件；
        ``_validate_contributions`` 抛出的 ``ValueError`` 走同一条隔离路径——把
        「打开界面才崩」前移成「加载期明确拒绝」（插件结构规范 §7.1 决议 4）。
        """
        pid = self._pid(plugin)
        if pid in self._failed:
            return []
        try:
            specs = gather_specs(plugin, pid)
            self._validate_contributions(pid, specs)
            self._fill_identity_defaults(plugin, specs)
            return specs
        except Exception as exc:  # noqa: BLE001 —— 边界隔离必须吞掉任意插件异常
            self._failed.add(pid)
            self._failures.append((pid, exc))
            log.exception("插件 %s 贡献收集中抛异常，已隔离该插件", pid)
            return []

    @staticmethod
    def _fill_identity_defaults(plugin: "Plugin", specs: list) -> None:
        """名字位唯一真源（决议 D14 ＋ 2026-09-20 扩到入口类）：省略即取 ``Plugin.name``。

        名字位分两类：

        - **身份类** —— ``StatusRowSpec.title`` / ``StateSpec.title`` /
          ``ComponentSpec.name``：省略即补，无条件。
        - **入口类** —— ``SettingsPageSpec.label`` / ``ActionSpec.label`` /
          ``HotkeySpec.label`` / ``DetailEntrySpec.label``：**该声明点只有一条贡献**
          时才补。多页 / 多按钮 /
          多热键时它们本就各有其事（同 D23「多条必须各有其名」），补同一个名字反而
          让两个入口分不清；此时省略视为声明缺失，不替它编。

        「组件声明一个即可，框架统一使用一个」的另一半：**入口文案不等于组件名是
        合法的**（例：recognize 组件名「识别（OCR + 条码）」太长不适合做按钮，按钮
        就叫「截图识别」）—— 那是**显式写出来的另一个名字**，不是第二份真源；真源
        始终是 ``Plugin.name``，写了别的只是覆盖。反过来说，**写了与组件名完全相同
        的字符串**就是重复写，由门禁 ``test_identity_name_is_not_written_twice`` 抓。

        补的是名字，**不是图标**：图标来源只有一个 —— ``ComponentSpec.icon``
        （``StatusRowSpec.icon`` 字段已随决议 D16 删除，行上再也声明不了图标）。
        ``Plugin.name`` 为空时不补（框架不替组件编名字）。
        """
        name = getattr(plugin, "name", "") or ""
        if not name:
            return
        per_point: dict[type, int] = {}
        for s in specs:
            per_point[type(s)] = per_point.get(type(s), 0) + 1
        for s in specs:
            if isinstance(s, (StatusRowSpec, V2StateSpec)):
                if not s.title:
                    s.title = name
            elif isinstance(s, ComponentSpec):
                if not s.name:
                    s.name = name
            elif isinstance(s, (V2SettingsPageSpec, ActionSpec, HotkeySpec, DetailEntrySpec)):
                if not s.label and per_point[type(s)] == 1:
                    s.label = name

    @staticmethod
    def _validate_contributions(plugin_id: str, specs: list) -> None:
        """收集期校验（fail-fast）：能静态判定的「必然崩溃」在加载期就拒绝。

        两条判据，都在这里（第二条见 :meth:`_validate_multi_contributions`）：

        1. **``SettingsPageSpec`` 必须至少有一个可构建来源**（``build`` 或 ``schema``）。
          两者都空时，``shell/settings.py`` 打开设置窗会执行 ``builder()`` →
          ``TypeError: 'NoneType' object is not callable``（P0 崩溃路径，且崩在
          **框架侧**、看不出是哪个插件的错）。这里改抛 ``ValueError``，由
          :meth:`_collect` 的崩溃隔离接住：该插件记 ``failed`` + 可读原因，
          其余插件不受影响。

        「只填 ``schema`` 不填 ``build``」**不在此拒绝**：``schema`` 是合法声明
        （将来渲染器接线后可用），今天确实会因为渲染器缺位而崩，但那是「缺渲染器」
        而非「声明不完整」，口径按 §7.1 决议 3 归挂账，不在这里拦。

        对应的门禁是 ``test_settings_page_is_buildable``（存量静态扫描 + 本函数的
        行为验证），保证这条判据不退化成恒真。

        第二条见 :meth:`_validate_multi_contributions`：同一声明点下的多条贡献
        必须各有其名（决议 D23）。
        """
        for spec in specs:
            if not isinstance(spec, V2SettingsPageSpec):
                continue
            if spec.build is None and spec.schema is None:
                where = getattr(spec, "label", "") or getattr(spec, "key", "") or "<无标签>"
                raise ValueError(
                    f"插件 {plugin_id} 的 SettingsPageSpec({where!r}) 既没有 build 也没有 schema；"
                    "设置页必须至少提供一个可构建来源"
                    "（否则打开设置窗时 TypeError，见插件结构规范 §7.1 决议 4）"
                )
        ContributionRegistry._validate_multi_contributions(plugin_id, specs)

    @staticmethod
    def _validate_multi_contributions(plugin_id: str, specs: list) -> None:
        """同一声明点下的**多条**贡献必须各有其名 —— 否则后写的静默顶掉前面的。

        两条判据都由决议 D23（行数与状态机数解绑）逼出来，判据本身是通用的：

        1. **``key`` 必填且唯一**（同一插件 ＋ 同一声明点内）。全局身份由框架拼成
           ``{plugin_id}.{key or 'default'}``，两条都不写 key 就都落到 ``…default``：
           面板的行复用缓存按 identity 索引（``panel.sync_status_rows``），状态机的
           topic 表也是按 identity 建的 —— 撞名意味着**界面上只剩最后一条，且不报错**。
           本判据与 ``Contribution`` 的 docstring 早已写明的那句一致，此前只是没人守。
        2. **声明多行必须逐行给 ``reason``**。契约 §2.4 的门槛：面板不是控制台，
           每加一行都要能回答「用户在这一行上的决定，别的行替不了」；不说清楚就加行
           = 面板行数随组件内部复杂度增长。

        两者都在这里 fail-fast：由 :meth:`_collect` 的崩溃隔离接住 —— 该插件记
        ``failed`` ＋ 可读原因，其余插件不受影响（同一插件**不部分生效**）。

        上限只对「多条」生效：单条贡献照旧可以省略 ``key``（框架取 ``default``），
        单行照旧不必写 ``reason`` —— 存量 6 个组件正是这一类，本判据对它们零影响。
        """
        points = declaration_points()
        by_point: dict[str, list] = {}
        for s in specs:
            by_point.setdefault(getattr(s, "point", ""), []).append(s)
        for point, group in by_point.items():
            if len(group) < 2:
                continue
            where = points.get(point, (point,))[0]
            keys = [s.key for s in group]
            if any(not k for k in keys):
                raise ValueError(
                    f"插件 {plugin_id} 的 {where}() 声明了 {len(group)} 条贡献，"
                    f"但其中 {sum(1 for k in keys if not k)} 条没写 key —— "
                    "多条贡献必须各有其名（key 必填），否则会撞进同一个全局身份 "
                    f"「{plugin_id}.default」，界面上只剩最后一条（决议 D23）"
                )
            dupes = sorted({k for k in keys if keys.count(k) > 1})
            if dupes:
                raise ValueError(
                    f"插件 {plugin_id} 的 {where}() 里 key 重复 {dupes} —— "
                    "一个名字只对应一条贡献，重名会让它们撞进同一个全局身份、"
                    "后写的静默顶掉前面的（决议 D23）"
                )

        rows = [s for s in specs if isinstance(s, StatusRowSpec)]
        if len(rows) > 1:
            for i, spec in enumerate(rows, start=1):
                if not (spec.reason or "").strip():
                    raise ValueError(
                        f"插件 {plugin_id} 声明了 {len(rows)} 行，第 {i} 行"
                        f"（key={spec.key!r}）没写 reason —— 契约 §2.4：声明多行时须在 "
                        "reason 里说明为什么面板必须同时看到它们"
                        "（面板不是控制台，行数不该随组件内部复杂度增长；决议 D23）"
                    )

    # ---------- 排序（统一入口，§6.4 三级规则） ----------

    def _sorted_plugins(self) -> list["Plugin"]:
        """插件间排序（§6.4 唯一入口，三处渲染同源）：

        - ``plugin_order``（用户排序，经 order_provider 读 config）中出现的插件
          按其顺序优先；
        - 未列入者按 ``Plugin.order``（新插件默认插入位）追加尾部，同值稳定；
        - 框架不做动态重排（问题优先浮前之类），行为可预期。
        """
        user_order: list[str] = []
        if self._order_provider is not None:
            try:
                po = self._order_provider() or []
                user_order = [x for x in po if isinstance(x, str)]
            except Exception:  # noqa: BLE001 —— 排序数据源损坏不应拖垮渲染
                log.exception("读取 plugin_order 失败，退化为默认插入位排序")
        rank = {pid: i for i, pid in enumerate(user_order)}

        def _key(p: "Plugin") -> tuple[int, int]:
            pid = self._pid(p)
            if pid in rank:
                return (0, rank[pid])
            order = getattr(p, "order", 0)
            return (1, order if isinstance(order, int) else 0)

        return sorted(self._plugins, key=_key)

    # ---------- 内部 ----------

    def _plugin_specs(self, plugin: "Plugin") -> list:
        return self._by_plugin.get(self._pid(plugin), [])

    # ---------- 状态面板行 ----------

    def status_rows(self) -> list[StatusRowSpec]:
        """所有状态行 Spec（按 §6.4 插件间排序），供面板按 identity 复用渲染。

        **每个组件都有一行 —— 不必声明**（2026-09-22 契约改动）。没写 ``row_specs()``
        的组件由框架合成一个**朴素行**：图标取身份声明 ``ComponentSpec.icon``、
        名字取 ``Plugin.name``；没有状态机 ⇒ 无角标、无副标题，也不带右侧控件
        （``_plain_row``）。被隔离的插件（贡献收集期抛异常）**不合成** —— 它本来就
        不该出现在面板上。

        为什么要合成：「有没有这一行」此前由 ``StatusRowSpec`` 当准入，于是同一件事
        在组件维度上长出两种形态 —— 声明了的有行、没声明的没有。行是组件在面板上的
        **落脚点**（也是「快捷区域」那枚按钮的同一过程，见 ``DetailEntrySpec``），
        不是一份可选贡献；组件要不要**额外**东西（角标 / 副标题 / 行内控件）才是
        声明的事。用户 2026-09-22 的原话即判据：「所有的组件都要有组件行（不需要声明）」。
        """
        out: list[StatusRowSpec] = []
        for p in self._sorted_plugins():
            pid = self._pid(p)
            if pid in self._failed:
                continue
            specs = self._plugin_specs(p)
            declared = [s for s in specs if isinstance(s, StatusRowSpec)]
            out.extend(declared if declared else [self._plain_row(p)])
        return out

    @classmethod
    def _plain_row(cls, plugin: "Plugin") -> StatusRowSpec:
        """没声明行的组件 → 框架合成的那一行（画材只有名字，图标归渲染层兜底）。

        名字走 ``title`` 字段而不是让渲染层去认 ``Plugin.name``：``StatusRowSpec``
        的名字位真源是 ``_fill_identity_defaults`` 补的那一份，合成行在消费期才建，
        补不着它，故在这里补同一件事。「组件名本身为空」时不编（同那条的口径）。
        """
        return StatusRowSpec(
            plugin_id=cls._pid(plugin),
            title=getattr(plugin, "name", "") or "",
        )

    def panel_rows(self, parent: "QWidget | None" = None) -> list["QWidget"]:
        """兼容旧消费者：直接按 Spec 工厂产出 QWidget 列表（无复用缓存）。

        新面板渲染走 ``status_rows()`` + 按 identity 复用；本方法保留给测试与
        一次性场景。
        """
        rows: list["QWidget"] = []
        for s in self.status_rows():
            if s.custom is not None:
                widget = s.custom(parent)
                if widget is not None:
                    rows.append(widget)
        return rows

    # ---------- 设置分页 ----------

    def settings_pages(self) -> list[V2SettingsPageSpec]:
        """插件贡献的设置分页（SettingsPageSpec，label + build）。"""
        pages: list[V2SettingsPageSpec] = []
        for p in self._sorted_plugins():
            for s in self._plugin_specs(p):
                if isinstance(s, V2SettingsPageSpec):
                    pages.append(s)
        return pages

    # ---------- 操作区动作 ----------

    def primary_actions(self) -> list[ActionSpec]:
        """插件贡献的操作区动作（ActionSpec，label + on_click）。
        
        按声明点收集（决议 D20），排序仍走 §6.4 插件间规则。"""
        acts: list[ActionSpec] = []
        for p in self._sorted_plugins():
            for s in self._plugin_specs(p):
                if isinstance(s, ActionSpec):
                    acts.append(s)
        return acts

    # ---------- 热键（v3 HotkeySpec） ----------

    def hotkeys(self) -> list[HotkeySpec]:
        """插件声明的全局热键（HotkeySpec，属性与旧热键契约兼容）。"""
        out: list[HotkeySpec] = []
        for p in self._sorted_plugins():
            for s in self._plugin_specs(p):
                if isinstance(s, HotkeySpec):
                    out.append(s)
        return out

    # ---------- U1 契约接线（UI 规范 §8.1 / §8.2） ----------

    def states(self) -> list[V2StateSpec]:
        """全部 ``StateSpec`` 状态声明（UI 规范 §4.2，静态；运行期数值在快照里）。

        消费方：``shell/renderers``（托盘队列派生、每插件代表态派生）。插件未
        声明时返回空列表——渲染侧决定兜底。

        **返回顺序 = 声明顺序**（同一插件内先声明的在前）：代表态平局判据直接依赖
        它（先声明者胜），故本函数**不排序**；``_gather`` 按对应表顺序拼接、
        ``state_specs()`` 的产出原样 extend，保序收集。
        """
        out: list[V2StateSpec] = []
        for p in self._sorted_plugins():
            for s in self._plugin_specs(p):
                if isinstance(s, V2StateSpec):
                    out.append(s)
        return out

    def tray_base_candidates(self) -> list[V2StateSpec]:
        """可作**菜单栏主图标**来源的状态机（准入判据的**唯一**落点）。

        判据只有一条：该状态机在声明里**定义了主图标**（``StateSpec.icon is not None``）。
        「能不能被选」是状态机自己的事实（我有没有一枚图标），不是一道许可 ——
        故不需要 ``as_tray_base`` 那种义务声明布尔；也未声明则**永不出现**在
        设置窗「通用」页的候选里。

        消费方：``shell/settings.py``（枚举候选）。``shell/app_shell.py`` 的同一条
        判据走 ``state_by_id``（按身份取回 spec，再读它自己的 ``icon``）。
        """
        return [s for s in self.states() if s.icon is not None]

    def state_by_id(self, state_id: str) -> V2StateSpec | None:
        """按**状态机身份**（``StateSpec.state_id``）取回声明；不在册返回 ``None``。

        「不在册」= 该组件已禁用/卸载（``deactivate`` 会把它的贡献从各列表摘掉），
        此时配置项与菜单栏必须同时回落 BarKit 默认底图 —— 两处判据同源才不会
        「设置窗显示默认、菜单栏还画着旧图」。
        """
        if not state_id:
            return None
        for s in self.states():
            if s.state_id == state_id:
                return s
        return None

    def components(self) -> list[ComponentSpec]:
        """全部 ``ComponentSpec`` 组件身份（标题栏图标条一枚一插件，U2 渲染消费）。"""
        out: list[ComponentSpec] = []
        for p in self._sorted_plugins():
            for s in self._plugin_specs(p):
                if isinstance(s, ComponentSpec):
                    out.append(s)
        return out

    def detail_entries(self) -> list[DetailEntrySpec]:
        """全部 ``DetailEntrySpec`` 详情页入口。

        语义（UI 规范 §5.3）：插件未声明 → 其 L2 整行不响应点击（U2 渲染消费）。
        """
        out: list[DetailEntrySpec] = []
        for p in self._sorted_plugins():
            for s in self._plugin_specs(p):
                if isinstance(s, DetailEntrySpec):
                    out.append(s)
        return out
