"""capabilities.automation：组件的「自动化开关」能力 mixin。

**名字为什么是 automation 而不是 guard**（2026-09-14 改名，旧名 ``Guard``）：
``Guard`` 易被读成「常驻守护基类」，但本能力只提供自动化运行态开关 + 模式徽标，
与是否常驻轮询**无关**——是否常驻由 ``run_mode=RUN_PERIODIC`` + 实现 ``tick()`` 决定。
反证即 ``hosts_guard``：它是常驻 daemon 却**不**继承本能力。

边界（单一关注点）：只拥有「自动化运行态 + 状态行里的自动化徽标」，
**不**接管整个状态行（领域开关——如通知的允许/静音、路由的规则覆盖——由插件自己
贡献），也**不**包含任何后端/规则逻辑。

统一语义：
- ``automation_active``  —— True=自动化运行中，False=已暂停/手动接管（子类实现）
- ``set_automation_active(active)`` —— 写配置并施加「开启/暂停自动化」的领域副作用（子类实现）
- ``toggle_automation()`` —— 在 运行/暂停 间切换（统一提供）
- ``mode_badge_spec()`` —— 返回 ``ModeBadgeSpec``（自动/手动模式徽标，§5.3）：
  框架渲染 ``ModeBadge`` 并据 ``get_state`` 在状态刷新时自同步，无需插件持有 widget
  （取代旧的自绘 Badge / ``_make_guard_badge`` / ``_refresh_guard_badge``）

组合：写成协作式 mixin（``__init__`` 调 ``super().__init__``），以便插件用
``class Plugin(AutomationCapability, Scheduled, ...)`` 多重继承而 MRO 干净。

notify_guard / route_guard 曾用 ``auto_enabled`` / ``is_paused`` 两个**极性别名**表达同一概念，
正是「缺统一抽象」的症状；本能力把二者收敛为 ``automation_active`` 一个正交属性。
"""

from __future__ import annotations

from barkit.contribution.specs import ActionResult, ModeBadgeSpec
from barkit.core.plugin import Plugin


class AutomationCapability(Plugin):
    """自动化开关能力基类。子类必须实现 ``automation_active`` / ``set_automation_active``。"""

    # ---------- 抽象钩子（子类必须实现） ----------

    @property
    def automation_active(self) -> bool:
        """自动化是否运行中。True=自动，False=已暂停/手动接管。"""
        raise NotImplementedError(f"{type(self).__name__} 必须实现 automation_active")

    def set_automation_active(self, active: bool) -> "ActionResult | None":
        """写配置并施加「开启/暂停自动化」的领域副作用。

        返回 ``ActionResult`` 只在**有话要说**时（契约 §2.6）：写盘失败这类「用户
        刚点了一下、界面却纹丝不动」的场景。成功且状态本身看得见就返回 ``None``。
        """
        raise NotImplementedError(f"{type(self).__name__} 必须实现 set_automation_active")

    # ---------- 统一提供 ----------

    def __init__(self, ctx) -> None:
        # 协作式：把初始化链交给下一个基类（通常是 Plugin）
        super().__init__(ctx)

    def toggle_automation(self) -> "ActionResult | None":
        """在 自动化运行 / 暂停 之间切换。

        把 ``set_automation_active`` 的结局**透传**出去：开关是 ``ActionCallback``
        （可返回 ``ActionResult``），宿主拿到才会把「没写成」这句话画出来。不透传
        等于让失败全落在日志里。
        """
        return self.set_automation_active(not self.automation_active)

    def mode_badge_spec(self) -> "ModeBadgeSpec":
        """状态行「自动化」模式徽标（§5.3 ModeBadgeSpec）：自动 / 手动，点击切自动化。

        框架渲染 ModeBadge 并据 ``get_state`` 在状态刷新时自同步，无需插件持有
        widget 引用（取代旧的自绘 Badge，§5.3 Option 1）。
        """
        return ModeBadgeSpec(
            label="自动" if self.automation_active else "手动",
            accent=self.automation_active,
            on_click=self.toggle_automation,
            get_state=lambda: (
                "自动" if self.automation_active else "手动",
                self.automation_active,
            ),
        )
