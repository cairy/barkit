"""插件本体入口：类声明、``__init__`` 与插件契约面（``row_specs`` /
``component`` / ``state_specs`` / ``settings_page`` / ``hotkeys``）。

``plugin.py`` 拆包：方法按职责分给同目录的七枚混合类（``_StatesMixin`` /
``_WordsMixin`` / ``_SnapshotsMixin`` / ``_ProbeMixin`` / ``_ConfigMixin`` /
``_DeviceMixin`` / ``_ActionsMixin``），正文逐字节搬移，零行为变更。``super()``
只有 ``__init__`` 那一处且留在 owner，故拆法成立。

``__init__`` 仍然 import ``DndMirroredBackend`` / ``OpenDisplayBackend`` /
``Decider`` —— 测试按字符串点名 ``barkit.plugins.notify_guard.plugin.DndMirroredBackend``
替换类，``self.backend`` 就由那一条进口子造出来；这三个名字**不许**再往兄弟搬。

落盘后手工补的两件（形态 J，平铺没有父包替我们做这件事）：``_rule_reason`` 与
``STALE_S`` 的再导出段 + ``__all__`` —— 消费方仍按 ``tests/test_notify_guard.py``
第 45 行的老写法从本入口取这两个名字。

``plugin.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from barkit.capabilities.automation import AutomationCapability
from barkit.contribution.specs import (
    ComponentSpec,
    HotkeySpec,
    IconName,
    SettingsPageSpec,
    StateSpec,
    StatusRowSpec,
    SwitchSpec,
)
from barkit.core.plugin import (
    FAIL_BREAK,
    RUN_PERIODIC,
)
from barkit.plugins.notify_guard import signals
from barkit.plugins.notify_guard.backend import DndMirroredBackend
from barkit.plugins.notify_guard.decider import Decider
from barkit.plugins.notify_guard.opendisplay import OpenDisplayBackend

from . import settings

# 七枚混合类由同目录兄弟**平铺**提供：平铺没有父包 __init__.py 替我们串，主类要
# 在自己这一层全部拿到（依赖成树 —— 兄弟只向上依赖 plugin_states / plugin_rules，
# 没有任何兄弟 import 本入口，故不构成循环导入）。
from .plugin_states import _StatesMixin
from .plugin_words import _WordsMixin
from .plugin_snapshots import _SnapshotsMixin
from .plugin_probe import _ProbeMixin
from .plugin_config import _ConfigMixin
from .plugin_device import _DeviceMixin
from .plugin_actions import _ActionsMixin

# --- 拆包后的正文分散在同目录的兄弟模块里（平铺拆包，没有 __init__.py 替我们做
# 全量再导出）⇒ 这两样由本入口再导出：消费方仍按 ``tests/test_notify_guard.py``
# 第 45 行的老写法 ``from ...plugin import Plugin, STALE_S, _rule_reason`` 取用。
# 兄弟之间**只**互取自己用得到的名字，谁被谁 import 见各自的 docstring。
from .plugin_rules import _rule_reason
from .plugin_states import AUTOMATION_STATES, DISPLAY_STATES, NOTIFY_STATES, STALE_S

__all__ = ["STALE_S", "_rule_reason"]


class Plugin(_StatesMixin, _WordsMixin, _SnapshotsMixin, _ProbeMixin, _ConfigMixin, _DeviceMixin, _ActionsMixin, AutomationCapability):
    id = "notify_guard"          # **冻结**：它是用户配置段键 ＋ 状态 topic 的一段（§9.1-1）
    name = "录屏通知"             # ★本批唯一改名处：三处显示位（提示 / 面板行 / 设置页）自动跟随
    run_mode = RUN_PERIODIC
    failure_policy = FAIL_BREAK
    suggested_poll = 3           # 建议周期（契约 D21）：守卫要秒级发现「通知被静音」，
                                 # 取值与 ``config_schema()`` 里 ``poll`` 的默认同源（3s）
                                 # —— 本步只把默认值来源换成声明，数值零变更
    api_version = 3              # 契约版本：框架按精确匹配校验（声明点方法路径）
    order = 10

# 包内自引用一律相对导入（《插件结构规范》§2.4）

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.backend = DndMirroredBackend()
        self.opendisplay_backend = OpenDisplayBackend()
        raw = self.backend.read()
        # dndMirrored=True = 静音 → allow=False；读不出按未知处理（首轮防抖后写入）
        applied = None if raw is None else (not raw)
        self.decider = Decider(
            debounce=self.cfg.value("debounce"), applied=applied,
            auto_enabled=self.automation_active,
        )
        # 三级线索的缓存值：首屏快照与副标题都是**纯读内存**（不 fork、不读盘）。
        # 初值取「装没装 / 跑没跑」（pyobjc 零 fork），``extended`` 留待首轮 tick 判定。
        self._display_state: str = signals.detect_app_state()
        self._last_combo: tuple[bool, bool] | None = None
        self._last_hits: list[dict] = []
        # 「启动时自动拉起 OpenDisplay」是否已处理过：tick 每 3s 一轮，没有这道闸它会
        # 把 OpenDisplay 反复拉起来。语义 = **每次加载只做一次**（重新启用组件算一次新的）。
        self._startup_launch_done = False
        # 首拍只「上膛」、第二拍才真拉（见 ``_launch_on_startup_once``）：首拍与宿主装配
        # 同一时刻到达，而拉起副屏会立刻引发一次屏变化 —— 那一段里宿主的窗正在诞生。
        self._startup_launch_armed = False

    # ---------- 生命周期 ----------
    # 状态行刷新由框架经事件总线统一处理（见 panel._recompute_plugin），
    # 插件不再自订阅自身 status topic。

    # ---------- UI 贡献（声明点方法，决议 D20）----------

    def row_specs(self):
        """一行，右侧只剩一枚 Switch（自动化总闸已移设置页，模式徽标随之撤掉）。

        名字与图标都不在行上写（决议 D14 / D16）：名字取 ``Plugin.name``，
        图标取组件身份 ``ComponentSpec.icon``。
        """
        return (
            StatusRowSpec(
                key="main",
                controls=(
                    SwitchSpec(
                        checked=self.decider.status()["allow"],
                        on_toggle=self.toggle_action,
                        get_checked=lambda: self.decider.status()["allow"],
                    ),
                ),
                # 整行 = 打开 OpenDisplay 窗体（D28：on_click 优先，故框架合成的"打开详情页"
                # 永久失效——这是裁决项 2 已认的代价，配置入口归设置页）
                on_click=self.open_opendisplay,
                # 行级副标题：三台机合成一句（契约 §2.7 / D24）
                subtitle=self._row_subtitle,
            ),
        )

    def component(self):
        """标题栏组件图标（每插件一枚）。这是**组件身份的唯一真源**（D14）：图标回答
        "这是什么组件"（不随状态变），名字省略即取 ``Plugin.name`` —— 不必再写一遍。"""
        return ComponentSpec(icon=IconName.BELL)

    def state_specs(self):
        """三台状态机（§4.1）。**声明顺序即关注度顺序**：严重度同级时先声明的那台
        当代表态，故身份主状态 display 写在最前，不参与"现在怎么样"的
        automation 写在最后（它的取值是 OK / OFF / IDLE，级别够不到托盘阈值）。
        """
        return (
            StateSpec(
                key="display", icon=IconName.DISPLAY_OFF, title="第二屏",
                states=DISPLAY_STATES, initial="unknown",
                stale_after=STALE_S,
                # 上面那枚 ``icon`` 就是**主图标**：声明了它，这台状态机才进入
                # 设置窗「通用」页的菜单栏图标候选（准入判据 = 有主图标，没有
                # 单独的 as_tray_base 开关）。用户选中后菜单栏显示它的**取值图标**
                # （实心 / 空心屏，见 DISPLAY_STATES），失联或取值没给图标时回落主图标。
            ),
            StateSpec(
                key="notify", icon=IconName.BELL, title="通知",
                states=NOTIFY_STATES, initial="unknown",
                stale_after=STALE_S,
            ),
            StateSpec(
                key="automation", icon=IconName.GEAR, title="自动化",
                states=AUTOMATION_STATES, initial="unknown",
                # 它是**装置配置态**（用户在设置页开/关），不参与"现在怎么样"的表达——
                # 它的后果已经体现在另两台机上（§4.2）。级别是 OK / OFF / IDLE，
                # 够不到托盘的级别阈值，故不必额外声明 alerts。
                stale_after=STALE_S,
            ),
        )

    def settings_page(self):
        """设置页：规则编辑器 + 自动化总闸 + 装置信息（§6.4；界面在 settings.py）"""
        return SettingsPageSpec(
            build=lambda: settings.build_page(self),
            reason="规则是两层索引的集合型配置（FieldType.LIST 全仓无渲染器），"
                   "自绘是今天唯一可用路径（契约 §2.8）",
        )

    def hotkeys(self):
        """全局热键：手动切换通知。"""
        return (
            HotkeySpec(
                name="notify_toggle", label="切换通知（手动）",
                default_combo="ctrl+alt+m", callback=self.toggle_action,
            ),
        )
