"""插件清单与配置的读：列表项数据类 ＋ 列表 / 实例 / 节拍 / 配置复位 / 诊断。

``plugin_manager.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from barkit.contribution.points import SUPPORTED_API_VERSION
from barkit.core.plugin import (
    RUN_ON_DEMAND,
    RUN_PERIODIC,
    Plugin,
)
from barkit.core.scheduler import poll_default_of

from .consts import log


# ``Plugin`` 需要**运行期**名字：`plugin_diagnostics` 要拿基类的方法对象与子类比对
# （「覆没覆写」这条静态判据），只在 TYPE_CHECKING 里 import 会 NameError。


@dataclass
class PluginInfo:
    """插件列表项（设置页「插件」用）。"""

    id: str
    name: str
    run_mode: str             # 框架要不要周期跑它（契约 D10）；列表页暂未展示
    source: str               # "internal" | "external"
    enabled: bool
    loaded: bool              # 当前是否在内存中激活
    api_version: int = SUPPORTED_API_VERSION   # 兜底即「框架支持的那一个版本」，不留过时字面量
    problem: str = ""         # 非空 = 该插件存在问题（拒绝/装配失败/贡献异常），供 UI 展示
    version: str = ""         # 组件自身版本（决议 D5）；空串 = 未标注（内置组件一律空）
    # 框架节拍的**默认值**（秒）＝ 组件建议（``Plugin.suggested_poll``）／框架兜底
    # （契约 D21）。``RUN_ON_DEMAND`` 为 ``None`` —— 它没有节拍可言（照旧登记进调度器
    # 只为能被事件点火，那是另一件事），设置窗据此决定「要不要给这一行画节拍控件」
    # （准入判据只此一条，不另设布尔）。
    poll_default: "float | None" = None


class _InventoryMixin:
    """插件清单与配置的读：列表项 / 组件实例 / 框架节拍 /配置复位 / 问题与诊断取数。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 列表 ----------

    def list_plugins(self) -> list[PluginInfo]:
        infos: list[PluginInfo] = []
        # 插件问题汇总：发现期拒绝 + 装配期失败（Registry）+ 贡献收集异常（注册中心）。
        # 供设置页「插件问题」展示；防御性 getattr 兼容测试桩。
        problems: dict[str, str] = {}
        get_problems = getattr(self._registry, "problems", None)
        if callable(get_problems):
            try:
                for pid, reason in get_problems() or []:
                    problems.setdefault(pid, str(reason))
            except Exception:  # noqa: BLE001
                log.exception("读取插件问题列表失败")
        get_failed = getattr(self._contributions, "failed", None)
        if callable(get_failed):
            try:
                for pid, exc in get_failed() or []:
                    problems.setdefault(pid, f"贡献收集异常：{exc}")
            except Exception:  # noqa: BLE001
                log.exception("读取贡献失败列表失败")
        for pid, cls in self._registry.discovered_classes.items():
            enabled = bool(self._config.get(f"plugins.{pid}.enabled", True))
            loaded = pid in self._registry.plugins
            run_mode = getattr(cls, "run_mode", RUN_ON_DEMAND)
            infos.append(
                PluginInfo(
                    id=pid,
                    name=getattr(cls, "name", "") or pid,
                    run_mode=run_mode,
                    source=self._registry.source_of(pid),
                    enabled=enabled,
                    loaded=loaded,
                    api_version=getattr(cls, "api_version", SUPPORTED_API_VERSION),
                    problem=problems.get(pid, ""),
                    version=getattr(cls, "version", "") or "",
                    # 读**类属性**而不是实例：未启用 / 未实例化的组件也要能画控件。
                    # 判据与调度器同源（`poll_default_of`），界面显示的默认值不会
                    # 与「实际用的节拍」分叉。
                    poll_default=(
                        poll_default_of(
                            getattr(cls, "suggested_poll", None), run_mode, pid
                        )
                        if run_mode == RUN_PERIODIC
                        else None
                    ),
                )
            )
        return infos

    # ---------- 取组件实例（框架按声明画的那一段要调组件自己的方法） ----------

    def plugin_instance(self, plugin_id: str) -> "Plugin | None":
        """取某组件的**运行时实例**；未加载（禁用 / 装配失败）⇒ ``None``。

        用途是「框架画出来的东西要调组件自己的实现」—— 装置运维卡就是：框架按**声明**
        决定画不画（``resources().helper_tasks`` / ``needs_root``），而安装 / 卸载 /
        那句警示文案只有组件本身知道。

        读的是**装载表**（``Registry.plugins``）而不是发现表（``discovered_classes``）：
        禁用中的组件没有实例，而它那一页也同样打不开，两边自洽（不会长出一张点不动的卡）。
        """
        try:
            return self._registry.plugins.get(plugin_id)
        except Exception:  # noqa: BLE001 —— 轻量测试桩可能没有装载表
            log.exception("读取组件 %s 的实例失败", plugin_id)
            return None

    # ---------- 框架节拍的用户覆盖（契约 D21；D27：`poll` 只服务框架） ----------

    def plugin_poll(self, plugin_id: str) -> "float | None":
        """用户写过的框架节拍覆盖值（秒）；没写过 / 写得不合法 → ``None``。"""
        return self._config.plugin_poll(plugin_id)

    def set_plugin_poll(self, plugin_id: str, seconds: "float | None") -> bool:
        """写框架节拍覆盖值并**让调度器当场重挂**；``seconds=None`` = 恢复默认（删键）。

        两件事必须一起做：① 落盘；② 重挂节拍 —— 少了 ②，用户改完界面纹丝不动、
        要重启才生效，正是「看起来生效了、其实在空转」。② 失败（组件没在跑）
        **不影响 ①**：配置照样落盘，下次启用时自会读到。
        """
        self._config.set_plugin_poll(plugin_id, seconds)
        self._scheduler.set_poll(plugin_id)
        return True

    # ---------- 配置初始化：组件配置恢复默认（框架统一入口） ----------

    def clearable_config_keys(self, plugin_id: str) -> list[str]:
        """「恢复默认配置」将清掉的键（界面先看它，再决定要不要画出那颗按钮）。"""
        return self._config.clearable_plugin_keys(plugin_id)

    def reset_plugin_config(self, plugin_id: str) -> list[str]:
        """把某组件的配置恢复为默认；返回被清掉的键名（空 = 本来就是默认）。

        语义与边界在 ``Config.reset_plugin_section``（清空用户差异段，保留 ``enabled``）。

        这里多做的一件事是**广播**：程序内写配置默认不发 ``config.changed`` —— 那些写
        路径（显示位芯片 / 刷新间隔 / 暂停 / 覆盖层）各自都有界面回调，写了各自叫一声就
        够了。而「恢复默认配置」改的正是**组件自己**的配置，组件唯一的知情渠道就是
        ``config.changed``（route_guard / hosts_guard / ipad_tunnel 都订着它）。不广播
        的后果很隐蔽：界面上清干净了、组件还按老配置跑，直到下一次重启。
        ``Config.save_user`` 顺手记了 mtime，所以文件监视那条路**不会**补上这一声 ——
        必须在这里显式叫。
        """
        removed = self._config.reset_plugin_section(plugin_id)
        if removed:
            self._config.changed.emit(self._config.data)
        return removed

    # ---------- 运维件的取数口（组件自己那张卡里的「复制诊断」用） ----------

    def plugin_diagnostics(self, plugin_id: str) -> "Callable[[], str | None] | None":
        """取某组件的诊断生成口；**没覆写** ``Plugin.diagnostics_text`` ⇒ ``None``。

        返回的是**可调用**而不是文本，两个理由：

        - 界面每次点击要**当场重新生成**（用户点「复制诊断」的场景就是他刚发现不对，
          要的是此刻的事实，不是画设置页那一刻的）；
        - 界面**不该为了决定画不画那枚按钮**先跑一次诊断 —— 那要读 ps 快照、读系统值。
          故准入判据是**静态**的：「这个类覆没覆写那个方法」，零成本、零副作用。

        取不到实例（组件没加载且无法临时实例化）时返回 ``None`` =
        本组件没有诊断可给，界面不画那枚按钮。
        """
        plugin = self._probe(plugin_id)
        if plugin is None:
            return None
        if type(plugin).diagnostics_text is Plugin.diagnostics_text:
            return None
        return plugin.diagnostics_text
