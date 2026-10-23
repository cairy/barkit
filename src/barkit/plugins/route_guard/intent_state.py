"""``_IntentStateMixin`` —— 期望态：配置段 → 完整期望态 → 声明出去；含用户 YAML 的写回。

``plugin.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from barkit.plugins.route_guard import (
    intent,
    rules,
)
from barkit.plugins.route_guard.rules import RouteConfigError

from .constants import log


class _IntentStateMixin:
    """期望态：配置段 → 完整期望态 → 声明出去；含用户 YAML 的写回。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 意图（档 B：policy 在 app 侧） ----------

    def _compute_intent(self) -> dict | None:
        """当前配置 → 完整期望态；配置非法返回 None（不落盘、不敲门）。

        ``pinned`` / ``manual_only`` 只喂给决策、不进产物（线格式是 helper 已在消费的
        契约，多一个它不认识的键就是整份拒绝 + 冻结，见 ``intent`` 模块 docstring）。
        """
        try:
            routes = rules.build_routes(self.plugin_config())
        except RouteConfigError as e:
            if callable(getattr(self._probe_service, "report_problem", None)):
                self._probe_service.report_problem(f"规则配置非法：{e}")
            log.error("规则配置非法，未更新意图：%s", e)
            return None
        state = self._state or {}
        prior_rules = {r.get("name"): r for r in (state.get("rules") or [])}
        prior = {name: r.get("applied") for name, r in prior_rules.items()}
        return intent.build_intent(
            routes, prior,
            pinned=self.pinned_scene(),
            manual_only=self._manual_scenes(),
            # 续上命中要的两样：上一轮**完整**的规则回显（谁生效过、落在哪张网卡）与
            # 网卡段（我们上次写下去的那份 ``wrote``）。少了它，固化过的场景在网关被
            # 自己写没之后必然判不中，命中一次就永远退出。
            prior_rules=prior_rules,
            prior_net=state.get("net") or {},
        )

    def _sync_intent(self) -> None:
        """立刻收敛一次意图（配置变更 / 手动开关后）：复用探活服务的 poll()。

        与周期轮次走同一条代码路径：算 → 声明出去。幂等由 ``declare`` 按内容哈希保证，
        故「就算刚发过一模一样的一份」也不必在这里判 —— 没有「和什么比」这件事。
        """
        self._probe_service.poll()

    # ---------- 期望态同步（零授权） ----------

    def sync_routes(self) -> None:
        """配置段 → 完整期望态 → 声明出去。校验失败抛 RouteConfigError（UI 提示）。

        档 B 收口后这份产物不是「规则定义」，而是 app 侧算好的**完整期望态**；档 C
        （2026-09-29）之后它连文件都不是，是一帧。这里只负责配置校验 + 立刻刷新。
        """
        routes = rules.build_routes(self.plugin_config())   # 非法即抛，由 UI 层捕获提示
        self._sync_intent()
        log.info("期望态已同步（%d 条规则）", len(routes["rules"]))

    def on_config_changed(self, _payload=None) -> None:
        try:
            self.sync_routes()      # 含重算期望态 + 声明出去
        except RouteConfigError as e:
            log.error("热重载后期望态未更新（规则非法）：%s", e)
        # 手编 YAML 的报错也要在面板上看得见：`routes` 机会读这份缓存报 failed。
        self._config_problem = self._evaluate_config_problem()

    def _sync_after_change(self) -> None:
        """写配置之后的统一收尾：重判合法性 → 合法才落盘期望态 → 逐台机即时广播。

        本组件有三条写配置的路径（暂停 / 覆盖层 / 手编热重载），这是它们共用的尾巴 ——
        收成一处，免得三处各写一遍、各漏一件事（旧写法就是三处各写一遍，
        其中两处漏了同步「配置是否非法」这一位）。
        **非法时不落盘**（见 ``start``）：错期望态推给 root helper 只会被整份拒绝。
        """
        self._config_problem = self._evaluate_config_problem()
        if not self._config_problem:
            try:
                self.sync_routes()      # 含重算期望态 + 声明出去
            except RouteConfigError as e:
                log.error("同步路由期望态失败：%s", e)
        self._publish_all()

    def _write_keys(self, patch: dict, *, fail: str) -> tuple[bool, str]:
        """写一到多个**已声明键** → 重建期望态 → 逐台机即时广播。返回 ``(成功?, 文案)``。

        走 ``cfg.update``：**等于声明默认值即删键**（用户 YAML 是差异层，留一个与缺省
        同值的键会把那个值钉死 —— 组件日后改缺省就传不到这位用户了），落盘前按声明的
        类型与 ``choices`` 校验。本组件从前是「越界就不写」与「照写不误」两种口径并存，
        现在归一处。

        本组件多条写配置的路径共用这一条尾巴 —— 它们原先各写一遍 try/except 与
        `_sync_after_change()`，多一条路径就多一次「漏了哪一步」的机会。`fail` 是给
        用户看的那半句，各路径自己的措辞仍不同。

        写盘失败时**不改内存里的任何东西**（配置本来就以盘上那份为准），把原因**交回
        调用方**去说 —— 落设置页那块槽、还是交回面板的结果槽，只有调用方知道
        （`_write_config` 讲了为什么）。「界面改了、盘上没动」是本组件最不能出的错。
        """
        return self._write_config(lambda cfg: cfg.update(patch), fail=fail)

    def _write_section(self, mutate, *, fail: str) -> tuple[bool, str]:
        """**逃生舱版**：``mutate`` 直接改用户差异段（嵌套子键 / 框架键），同一条尾巴。

        同一条收尾、同一条失败口径，区别只在那一笔不走逐键规则 —— 子键没有各自的
        ``FieldSpec``，「等于默认即删键」对它无从判定。用它只有一个理由：要动的键
        不是平坦的声明字段（今天只有 ``rule_overrides.<场景>.<位>`` 这一处）。
        """
        return self._write_config(lambda cfg: cfg.mutate(mutate), fail=fail)

    def _write_config(self, write, *, fail: str) -> tuple[bool, str]:
        """上面两个口的**唯一**实现：``write`` 收一个 ``PluginConfig``，自己挑口。

        把失败文案**交出来**而不是直接说完（`(成功?, 文案)`）的理由只有一条：报到哪里去
        是**调用方**才知道的事 —— 面板侧那条路没有 `FeedbackBar` 槽位（设置页关着时
        `self._feedback is None`），文案必须跟着返回值走上去；而设置面的写入口则照旧由
        `_say_error` 当场落在槽里。

        组件侧**只经 ``self.cfg``**：``ctx.config`` 在哪不归组件知道，那条禁令由
        ``tests/test_plugin_config.py`` 的门禁守着。
        """
        try:
            write(self.cfg)
        except Exception as e:  # noqa: BLE001
            return False, f"{fail}：无法写入配置（{e}）"
        # config.changed 会触发 on_config_changed → sync_routes；这里兜底再同步一次
        self._sync_after_change()
        return True, ""
