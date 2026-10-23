"""core/registry：插件发现/加载/启停（方案 §3.2）。

约定：每个插件是 barkit.plugins 下的一个子包，内含 plugin.py，
暴露一个名为 Plugin 的 barkit.core.plugin.Plugin 子类。
配置 plugins.<id>.enabled = false → 不加载。
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from pathlib import Path

from barkit.core.config import Config
from barkit.core.helper_tasks import sync_task_table
from barkit.core.contributions import SUPPORTED_API_VERSION, api_version_reject_reason
from barkit.core.loader import discover_external
from barkit.core.plugin import Plugin, PluginContext
from barkit.core.resident import ResidentHost
from barkit.core.rootbin_reconcile import resolve_sources, wanted
from barkit import paths  # 外部插件根目录走模块属性（测试重定向生效）

log = logging.getLogger(__name__)


class Registry:
    def __init__(self, config: Config, events=None) -> None:
        self.config = config
        self.ctx = PluginContext(config=config, events=events)
        # 长驻服务托管（§8.3.1 / 批次 5 后段）：线程/时钟/起停/销毁全归框架，
        # 插件只经 resident_services() 声明。ctx 是全插件共享单例，回调无参。
        self.resident_host = ResidentHost()
        self.ctx.resync_services = self.sync_services
        # 「现在跑一轮」（同上共享单例的另一口）：**集合不动**，只把某一拍提前。
        # 两者都在宿主身上、都由框架持有托管权 —— 组件只能请求，不能起停服务。
        self.ctx.wake_service = self.resident_host.wake
        # A 类二进制来源解析（§9.3 / 批次 4 · S4）：同样是共享 ctx 上的无参回调，
        # 口径 = 已安装插件并集，含 disabled / failed。
        self.ctx.root_binary_sources = self.root_binary_sources
        self.plugins: dict[str, Plugin] = {}
        # 装配失败隔离（§9.2 isolate）：plugin_id → 人类可读原因（validate /
        # provision / 实例化 / start 任一阶段失败），经 problems() 供 UI 展示
        self._failed: dict[str, str] = {}
        # 全量发现结果（含被禁用未实例化的类），供 E3 ② PluginManager 运行期
        # 启用 / 列表展示；_source 标记 internal（pkgutil）/ external（manifest）。
        self._discovered: dict[str, type[Plugin]] = {}
        self._source: dict[str, str] = {}
        self._disabled_ids: set[str] = set()
        # 已装配实例（含 disabled / failed）——helper task 表与 §9.1 对账的口径都是
        # 「已安装」而非「正在运行」：失败插件写下的系统状态同样需要 task 去收敛。
        self._instantiated: list[Plugin] = []

    # ---------- 发现 ----------

    def discover(self) -> dict[str, type[Plugin]]:
        """扫描 barkit.plugins 子包 + 外部插件根目录，返回 {插件 id: Plugin 类}。

        内部插件经 ``pkgutil`` 发现；外部插件经 ``discover_external``（manifest 驱动）。
        两路在「Plugin 类发现层」汇合为同一份字典，后续 ``load_all`` 实例化、
        ``ContributionRegistry`` 统一消费——单一列表，无双轨。
        """
        found: dict[str, type[Plugin]] = {}
        self._rejected: list[tuple[str, str]] = []
        # 发现器对 L5 的耦合刻意保持为「按名」而非静态 import：导入 core.registry
        # 不会连带加载整个 plugins 包，架构门禁 banned 的是静态依赖（`import
        # barkit.plugins`）。按名加载插件是发现器的固有职责，不是可消除的耦合——
        # 且子模块定位要走父包 __path__，不能退化成纯文件系统枚举。
        plugins_pkg = importlib.import_module("barkit.plugins")
        for info in pkgutil.iter_modules(plugins_pkg.__path__):
            if not info.ispkg:
                continue
            # 跳过以 _ 开头的子包（模板 / 脚手架 / 示例，不加载）
            if info.name.startswith("_"):
                continue
            try:
                mod = importlib.import_module(f"barkit.plugins.{info.name}.plugin")
                cls = getattr(mod, "Plugin", None)
                if cls is None or not issubclass(cls, Plugin):
                    log.warning("插件 %s 未暴露有效的 Plugin 类，跳过", info.name)
                    continue
                # 版本闸门：契约不兼容的插件在「导入期」即拒绝，早于实例化，
                # 避免坏插件 __init__ 触碰事件总线 / 调度器产生副作用。
                reason = api_version_reject_reason(
                    getattr(cls, "api_version", SUPPORTED_API_VERSION), info.name
                )
                if reason is not None:
                    self._rejected.append((info.name, reason))
                    log.warning("跳过插件 %s：%s", info.name, reason)
                    continue
                found[info.name] = cls
                self._source[info.name] = "internal"
            except Exception:
                log.exception("加载插件包 %s 失败，跳过", info.name)

        # 外部插件：manifest 驱动，固定默认路径 + config 覆盖
        ext_dir = self._external_dir()
        ext_found, ext_rejected = discover_external(ext_dir)
        for name, cls in ext_found.items():
            found[name] = cls
            self._source[name] = "external"
        self._rejected.extend(ext_rejected)
        return found

    def refresh_discovery(self) -> None:
        """重扫插件（内外部）并刷新类表与来源 —— 安装 / 覆盖外部包之后调用。

        与 ``load_all`` 的发现阶段**同源**（同一个 ``discover()``），不另造一条发现
        路径：新装的包、被换掉的旧类都经这里进内存。**不动**已在跑的实例 ——
        调用方必须先把要替换的那个停掉（``PluginManager`` 的安装编排正是这个顺序）。

        ``_source`` 里的陈旧项（本次没扫到的）一并摘掉，否则卸载 / 换包之后再问
        「这个 id 是哪来的」会答成上一版的事实。
        """
        self._discovered = self.discover()
        known = set(self._discovered)
        for pid in [p for p in self._source if p not in known]:
            self._source.pop(pid, None)

    def _external_dir(self) -> Path:
        """外部插件扫描根目录：config.external_plugins.path 优先，否则默认路径。

        故意不放于 ``plugins`` 下——配置校验器要求 ``plugins.<id>`` 必为 mapping
        （插件配置段），字符串路径与之冲突；故用顶层 ``external_plugins.path`` 命名空间。
        """
        cfg = self.config.get("external_plugins.path")
        if isinstance(cfg, str) and cfg:
            return Path(cfg).expanduser()
        return paths.EXTERNAL_PLUGINS_DIR

    @property
    def rejected(self) -> list[tuple[str, str]]:
        """被版本闸门拒绝的插件包名及原因（discover 阶段），供启动汇总。"""
        return getattr(self, "_rejected", [])

    @property
    def failed(self) -> dict[str, str]:
        """装配失败隔离结果：plugin_id → 人类可读原因（load_all 阶段）。"""
        return getattr(self, "_failed", {})

    def problems(self) -> list[tuple[str, str]]:
        """全部插件问题（发现期拒绝 + 装配期失败 + 长驻服务异常），供「插件问题」展示。"""
        return [*self.rejected, *self.failed.items(), *self.resident_host.problems()]

    # ---------- 长驻服务托管（§8.3.1 / 批次 5 后段） ----------

    def start_services(self, plugin: Plugin) -> None:
        """起该插件声明的长驻服务（幂等）。失败不阻断插件装载。"""
        try:
            self.resident_host.start(plugin)
        except Exception:  # noqa: BLE001 —— 服务托管失败不得拖垮插件其余部分
            log.exception("插件 %s 长驻服务启动失败", plugin.id)

    def stop_services(self, plugin: Plugin) -> None:
        """停该插件的长驻服务。**必须在 ``plugin.stop()`` 之前调用**（顺序铁律）。"""
        try:
            self.resident_host.stop(plugin)
        except Exception:  # noqa: BLE001
            log.exception("插件 %s 长驻服务停止失败", plugin.id)

    def teardown_services(self, plugin: Plugin) -> None:
        """卸载语义的服务收尾：``service.teardown()`` + 停 + 丢记录。"""
        try:
            self.resident_host.teardown(plugin)
        except Exception:  # noqa: BLE001
            log.exception("插件 %s 长驻服务 teardown 失败", plugin.id)

    def sync_services(self) -> None:
        """按声明重算**所有已加载插件**的服务集合（差量收敛）。

        即 ``PluginContext.resync_services`` 的目标：插件开关翻转后写盘并请求框架
        重算。声明未变的插件是空操作，故可廉价反复调用。单插件失败不阻断其余。
        """
        for plugin in list(self.plugins.values()):
            try:
                self.resident_host.sync(plugin)
            except Exception:  # noqa: BLE001
                log.exception("插件 %s 长驻服务重算失败", plugin.id)

    # ---------- helper task 表（§7.4.4 / §9.1 第 3 步） ----------

    def root_binary_sources(self) -> "tuple[dict, dict]":
        """按声明解析 A 类二进制来源（口径 = **全部已装配实例**，含 disabled / failed）。

        返回 ``({name: 源路径}, {name: 缺失原因})``。纯解析：不写盘、不弹授权；
        由安装器决定「缺失即拒绝安装」（见 ``core.rootbin_reconcile``）。
        """
        return resolve_sources(self._instantiated)

    def wanted_root_binaries(self) -> frozenset:
        """全部已装配实例声明的 A 类名集（§9.3 对账 wanted 侧，含 disabled / failed）。"""
        return wanted(self._instantiated)

    def register_instance(self, plugin: Plugin) -> None:
        """登记一个已装配实例（按 id 去重，后到者覆盖）。"""
        self._instantiated = [p for p in self._instantiated if p.id != plugin.id]
        self._instantiated.append(plugin)

    def sync_helper_tasks(self) -> None:
        """按插件声明重算那张 helper 任务表，**经会话送给守护**（幂等，失败只记日志）。

        口径：**全部已装配实例**（含 disabled / failed，§9.1 同款）。插件声明
        ``Plugin.resources().helper_tasks``，框架汇总成表；未被声明的 task 显式写
        ``enabled: false`` 顶掉内置默认值（见 ``core.helper_tasks`` 的说明）。

        2026-09-29：不再落盘 ``helper_tasks.json``（helper 侧已不读任何用户域文件），
        改走 ``intent.set("tasks", …)``。会话由装配层注入 ``ctx.helper``；没接线时
        如实记一条（不静默当成功 —— 悄悄不发会让「插件声明了却没生效」查不出来）。
        """
        link = getattr(self.ctx, "helper", None)
        try:
            ok, err = sync_task_table(self._instantiated, link)
        except Exception:  # noqa: BLE001 —— 送表失败不得拖垮启动
            log.exception("helper 任务表发送失败")
            return
        if not ok:
            log.info("helper 任务表未送达：%s", err)

    # ---------- 启停 ----------

    def load_all(self) -> list[Plugin]:
        """按配置加载并 start 所有启用的插件（安装四阶段编排，§9.2）。

        流程：discover → validate（声明校验）→ provision（B/C 资产，仅外部）→
        实例化 + start（isolate 边界）。任一步失败 → 标记 failed（含人类可读
        原因，经 ``problems()`` 供 UI「插件问题」展示），不中断其余插件。

        同时缓存全量发现结果（含被禁用未实例化的类）及来源，供 E3 ②
        PluginManager 运行期启用 / 列表展示。

        资产部署先于 enabled 判断：禁用降级的是运行状态，不是资产存在性（§9.3）。
        """
        self._discovered = self.discover()
        self._failed: dict[str, str] = {}
        self._disabled_ids = set()
        self._instantiated = []
        for name, cls in self._discovered.items():
            # 实例化隔离（E3 ①）：__init__ 抛异常的坏插件记入 failed，不中断
            try:
                plugin = cls(self.ctx)
            except Exception:
                log.exception("插件 %s 实例化失败，跳过", name)
                self._failed[name] = "实例化失败（__init__ 抛异常，详见日志）"
                continue
            if not plugin.id:
                plugin.id = name
            # 先登记再校验/启停：失败的插件的声明同样计入（口径见 sync_helper_tasks）
            self.register_instance(plugin)

            # 阶段 2：validate（可插拔声明校验，纯检查不写盘）
            from barkit.core.installer import validate_plugin

            reason = validate_plugin(plugin)
            if reason is not None:
                log.warning("插件 %s 校验失败：%s", plugin.id, reason)
                self._failed[plugin.id] = f"校验失败：{reason}"
                continue

            # 阶段 3：provision（B/C 类资产，幂等；仅外部插件部署）
            if self._source.get(name) == "external":
                from barkit.core.installer import provision

                try:
                    provision(plugin.id, self._external_dir() / name,
                              plugin.resources(), dry=False)
                except Exception as exc:
                    log.exception("插件 %s 资产装配失败", plugin.id)
                    self._failed[plugin.id] = f"资产装配失败：{exc}"
                    continue

            if not plugin.enabled:
                log.info("插件 %s 已在配置中禁用，不加载", plugin.id)
                self._disabled_ids.add(plugin.id)
                continue
            try:
                plugin.start()
            except Exception:
                log.exception("插件 %s start 失败，跳过", plugin.id)
                self._failed[plugin.id] = "启动失败（start 抛异常，详见日志）"
                continue
            self.plugins[plugin.id] = plugin
            # 服务托管在 plugin.start() 之后（插件可能在此完成初始化）
            self.start_services(plugin)
            log.info("插件已加载：%s (%s)", plugin.id, plugin.run_mode)
        # 收尾：把全部声明汇成一份 tasks 意图（含 disabled —— 禁用插件仍需 task
        # 跑一轮，把它写下的「全撤」期望态收敛掉）
        self.sync_helper_tasks()
        return list(self.plugins.values())

    # ---------- E3 ② 运行期查询 ----------

    @property
    def discovered_classes(self) -> dict[str, type[Plugin]]:
        """全量发现到的插件类（含被禁用未实例化的），供运行期启用。"""
        return self._discovered

    def source_of(self, plugin_id: str) -> str:
        """插件来源：'internal'（包内）/ 'external'（manifest 外部插件）。"""
        return self._source.get(plugin_id, "internal")

    def plugin_order(self) -> list[str]:
        """插件间排序唯一调用源（§6.4）：读 config 顶层 ``plugin_order``。

        三处渲染（标题栏图标条、状态行、设置窗插件分页）统一经
        ``ContributionRegistry``（注入本方法为 order_provider）取得同一顺序，
        不允许各自重排。
        """
        return self.config.plugin_order()

    @property
    def disabled_ids(self) -> set[str]:
        """启动时因配置禁用而未实例化的插件 id。"""
        return self._disabled_ids

    def forget(self, plugin_id: str) -> None:
        """从发现结果中移除（卸载外部插件后，避免列表仍显示，直至下次启动重新发现）。

        同时摘掉它的 helper task 声明并重算表 —— §9.1 卸载第 3 步「从
        tasks 意图里摘除该插件的 task」。"""
        self._discovered.pop(plugin_id, None)
        self._source.pop(plugin_id, None)
        self._disabled_ids.discard(plugin_id)
        self._instantiated = [p for p in self._instantiated if p.id != plugin_id]
        self.sync_helper_tasks()

    def stop_all(self) -> None:
        for plugin in self.plugins.values():
            # 顺序铁律：先停长驻服务（线程/端口释放），再 plugin.stop()
            self.stop_services(plugin)
            try:
                plugin.stop()
            except Exception:
                log.exception("插件 %s stop 异常", plugin.id)
        self.plugins.clear()
