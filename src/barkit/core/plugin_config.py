"""core/plugin_config：组件配置的 **typed 门面** —— 默认值只有一处真源。

要解决的问题（2026-10-03 实测，插件侧 29 处读点）：

    FieldSpec(key="auto_reconnect", type=FieldType.BOOL, default=True)   # 声明层
    bool(self.plugin_config().get("auto_reconnect", True))               # 代码层 ── 第二份

第二份的实际危害不是多打几个字，是**漂移不报错**：把 schema 的默认值改成 ``False``，
代码里那句 ``True`` 继续生效 —— 界面显示「已关」而实际行为是「开」，两处都合法，
没有一条判据会红。取值域同理（``todo.set_size`` 手写的三个档位与 schema 的
``choices`` 是同一份清单的两份抄写）。

因此取值口一律经本模块：**类型、默认值、取值域三件全部取自本组件的
``config_schema()``**，调用点只报键名。

写侧同理，并且多一条：**等于声明默认值的写入 ⇒ 删键**。

    用户 YAML 是**差异层**（§9.5：默认值不落盘），只存「与缺省不同」的那些键。
    照着写一遍同值键的代价不是多一行，是**把那个值钉死**：组件日后把 ``debounce``
    的默认从 2 改成 3，那位曾把下拉拨回 2 的用户会一直停在 2，而他并没有要求过
    「永远 2」—— 这件事与 ``Config.set_plugin_poll`` 写回默认值即删键同一条判据
    （``docs/architecture/主框架架构-内核层.md`` 的「用户覆盖的读写」）。

两类失败必须分开（与 ``Config`` 各取数口同一口径）：

- 读了一个 schema **没声明**的键 ⇒ :class:`PluginConfigError`（**作者写错了**，
  早 fail 比回落一个凭空的价值好）；
- 用户手改 YAML 把值写歪 ⇒ 回落**声明默认值**，不抛（**用户手滑**，不该让面板起不来）。

钉住本模块的三条边界（``docs/plans/2026-10-03-配置接口统一.md`` §2）：

1. 只认 ``config_schema()`` 里声明过的键。框架所有的键（``enabled`` / ``poll`` /
   ``ui.<slot>.visible`` / ``states.<key>.priority``）**不进这里** —— 它们各有唯一读侧，
   见那个方案的第 4 节；
2. 不提供按类型命名的取值口（``cfg.bool()`` / ``cfg.str()``）：类型归声明，
   多一个口就多一次「对 BOOL 字段问字符串」的机会；
3. 不落盘、不缓存用户值：每次取用现读 ``plugin_config()``，与热重载同一份数据。
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any, Callable, Mapping

from barkit.core.schema import FieldSpec

if TYPE_CHECKING:                      # 只为标注；运行期不 import，避免 core 内部成环
    from barkit.core.plugin import Plugin


class PluginConfigError(Exception):
    """组件对配置门面的**非法调用**（作者或调用方的错，不是用户数据错）。

    三种：读/写了一个它自己没声明过的键、写了一个不合声明类型或落在 ``choices``
    之外的值、在没有配置源（``ctx.config`` 缺位）的情况下要写配置。
    三种都该早 fail —— 它们都不是「用户手改 YAML 写歪了」那种要宽待的处境。
    """


#: 「这个键不在合并后的配置段里」的哨兵。不能用 ``None``：``None`` 是合法的
#: 用户取值（``device:`` 这种空值写法），两者要分开判。
_ABSENT = object()

#: 「这一笔写下去等于删键」的哨兵（写侧用）。与 ``_ABSENT`` 分开是为了让
#: ``None`` 能作为**合法写入值**流通（写 ``None`` 是「设成空」，不是「删掉」）。
_DROP = object()


def normalize(field: FieldSpec, raw: Any) -> Any:
    """把用户盘上的值按声明归一；**不可用即回落声明默认值**（用户手滑一律宽待）。

    判据直接复用 :meth:`FieldSpec.check_value` —— 校验规则只有一份，本模块不另写一套
    类型判断（两套迟早分叉）。``None`` 与缺位同等看待：``device:`` / ``device: null``
    表达的都是「没设过」，应拿到声明默认值而不是 ``None``。
    """
    if raw is _ABSENT or raw is None:
        return deepcopy(field.default)
    if field.check_value(raw) is not None:
        return deepcopy(field.default)
    return raw


class PluginConfig:
    """一个组件实例的配置门面。由 :attr:`barkit.core.plugin.Plugin.cfg` 惰性给出。"""

    def __init__(self, plugin: "Plugin") -> None:
        self._plugin = plugin
        self._fields: dict[str, FieldSpec] | None = None

    # ---------- 声明面 ----------

    def _field_map(self) -> dict[str, FieldSpec]:
        """本组件声明的字段表（``config_schema()`` 现取一次后缓存）。

        ``config_schema()`` 每次调用都新建一枚 ``FormSchema``（见各插件的实现），
        而按拍取配置的地方在 worker 线程上、每秒都可能跑到，故缓存一份。
        schema 是**类级静态声明**（全仓无一处按运行期状态构造），缓存不会陈旧。
        """
        if self._fields is None:
            schema = self._plugin.config_schema()
            self._fields = {f.key: f for f in schema.fields}
        return self._fields

    def _field(self, key: str) -> FieldSpec:
        field = self._field_map().get(key)
        if field is None:
            raise PluginConfigError(
                f"组件 {self._plugin.id!r} 读了自己没声明的配置键 {key!r}；"
                f"请在它的 config_schema() 里加一条 FieldSpec（默认值也只有那一处）"
            )
        return field

    def declared(self, key: str) -> bool:
        """该键有没有被本组件声明（只读，不抛）。"""
        return key in self._field_map()

    def default_of(self, key: str) -> Any:
        """该键的**声明默认值**；未声明 ⇒ 抛。

        给「界面提示缺省是多少」这类场景用 —— 那个数字同样只许有一份，
        不许调用方自己再抄一遍。
        """
        return deepcopy(self._field(key).default)

    # ---------- 取值 ----------

    def section(self) -> dict:
        """本组件合并后的配置段（含框架键，如 ``enabled``）。

        与 :meth:`barkit.core.plugin.Plugin.plugin_config` 同一份数据；要整段
        （路由规则校验、导出包）时用它，别一个键一个键地取。
        """
        return self._plugin.plugin_config()

    def value(self, key: str) -> Any:
        """按声明取值：类型、默认值、取值域全部来自 ``config_schema()``。

        未声明 ⇒ :class:`PluginConfigError`；用户写歪 ⇒ 声明默认值，不抛。
        """
        field = self._field(key)
        section = self._merged_section()
        if section is None:
            return deepcopy(field.default)
        return normalize(field, section.get(key, _ABSENT))

    # ---------- 写回 ----------

    def set(self, key: str, value: Any) -> None:
        """写回**一个**已声明的键；**等于声明默认值 ⇒ 删键**（模块 docstring 讲了为什么）。

        值不合法（类型不对 / 落在 ``choices`` 之外 / 未声明）⇒
        :class:`PluginConfigError`：那是调用点的错，不该悄悄落一个用户没要的值。

        调用方要「失败就返回 False」那种契约（设置页的既有形状），自己接异常 ——
        本口只报错，不管报到哪里去。
        """
        self.update({key: value})

    def update(self, patch: Mapping[str, Any]) -> None:
        """一次「读—改—写」里写**多个**已声明的键，逐键同 :meth:`set` 的规则。

        **先整批校验，再落盘**：半批写下去等于给用户留了半截状态，
        而剩下那半批他已经点过一次、不会知道没生效。

        给「设置页只写它显示的那几个键」用（本页不显示的键原样留着 ——
        用户手写的 ``enabled`` / 别处加的项不该被界面替他删掉）。
        """
        plan: list[tuple[str, Any]] = []
        for key, value in patch.items():
            field = self._field(key)
            problem = field.check_value(value)
            if problem is not None:
                raise PluginConfigError(
                    f"组件 {self._plugin.id!r} 要给 {key!r} 写的值不合法（{problem}）；"
                    f"类型与取值域归它的 config_schema()，调用点只报键名"
                )
            plan.append((key, _DROP if value == field.default else deepcopy(value)))

        def _apply(section: dict) -> None:
            for key, value in plan:
                if value is _DROP:
                    section.pop(key, None)
                else:
                    section[key] = value

        self.mutate(_apply)

    def mutate(self, fn: Callable[[dict], None]) -> None:
        """**逃生舱**：把「用户差异段」交给 ``fn`` 就地改，然后落盘。

        给三类形状用：

        1. **嵌套子键**（``rule_overrides.<场景>.routes_suppressed``）：子键没有各自的
           ``FieldSpec``，「等于默认即删键」对它无从判定（能判的只有整段）；
        2. **互相牵连的多键**（写 A 的同时清 B）；
        3. **框架键**的搬家（``enabled`` 之类，不在任何组件的 schema 里，
           走 :meth:`set` 会被声明校验挡下）。

        它没有 :meth:`set` / :meth:`update` 的那两条规则 —— 只把
        :meth:`Config.update_plugin_section` 搬到门面后面。**组件侧一个字节都不许
        自己取 ``ctx.config``**：那条禁令由 ``tests/test_plugin_config.py`` 的门禁守着，
        全仓只剩这一处写入口。
        """
        config = self._config_source()
        if config is None:
            raise PluginConfigError(
                f"组件 {self._plugin.id!r} 在没有配置源的情况下要写配置（ctx.config 缺位）"
            )
        config.update_plugin_section(self._plugin.id, fn)

    def _config_source(self) -> Any:
        """本组件的 ``Config``；**没接线**（``ctx`` 或 ``config`` 缺位）时 ``None``。

        两个口（读与写）问的是同一件事，故收成一处 —— 否则「读得到、写不了」
        这种半接线状态会在两个地方各判一次。
        """
        ctx = getattr(self._plugin, "ctx", None)
        cfg = getattr(ctx, "config", None) if ctx is not None else None
        if cfg is None or not hasattr(cfg, "get"):
            return None
        return cfg

    def _merged_section(self) -> dict | None:
        """合并后的配置段；**没有配置源**（未接线 / 测试桩没给 ``config``）时返回 ``None``。

        与 :meth:`Plugin.enabled` 用同一份兜底口径：拿不到配置就按「没写过」办，
        由声明默认值顶上 —— 而不是抛 ``AttributeError``（那是接线问题，不该表现成
        组件读配置崩掉）。
        """
        return None if self._config_source() is None else self._plugin.plugin_config()

    def __repr__(self) -> str:  # pragma: no cover - 只为排查时好读
        return f"<PluginConfig {self._plugin.id!r} fields={sorted(self._field_map())}>"


__all__ = ["PluginConfig", "PluginConfigError", "normalize"]
