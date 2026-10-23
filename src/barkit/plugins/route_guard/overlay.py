"""``_OverlayMixin`` —— 覆盖层（「本次」要不要叠路由）与当前场景的判定。

``plugin.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from barkit.contribution.specs import ActionResult
from barkit.plugins.route_guard.rules import RouteConfigError

from .constants import log


class _OverlayMixin:
    """覆盖层（「本次」要不要叠路由）与当前场景的判定。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 覆盖层（面板开关：「本次」要不要叠路由，§4.8） ----------

    def _current_scene(self) -> str | None:
        """开关此刻作用的**场景**（不是「规则」）——回显优先，都没有则回落配置首项。

        两处旧的门控都撤了：

        - **不再要求触发命中**（§4.8）。旧写法是「没命中 ⇒ 开关禁用」，用户在别的地方
          看着开关灰着却不知道为什么，也没法提前说一句「下次进这个网别加」。
        - **不再限「第一条命中规则」**。覆盖层按**场景名**索引，控的是整个场景。

        取数走 ``_scene_rules``（**与三台机里的 ``profile`` 同源**）：「开关作用在哪个
        场景」与「副标题说命中哪个场景」不能是两个答案。兜底的兜底（连意图都没有且
        配置里没有场景）才返回 None ⇒ 开关禁用。

        **手动指定优先**（§2.3）：锁着某个场景时「当前场景」就是它 —— 忽略匹配的意思
        正是别的场景这一轮都不参与。这同时让覆盖层跟着换场景作废
        （``_reap_suppressions`` 读的也是本方法）。
        """
        pinned = self.pinned_scene()
        if pinned:
            return pinned
        rules = self._scene_rules() or []
        for r in rules:
            if r.get("trigger_matched") and r.get("name"):
                return str(r["name"])
        if rules and rules[0].get("name"):
            return str(rules[0]["name"])
        for profile in self._profiles():
            name = str(profile.get("name", "")).strip()
            if name:
                return name
        return None

    def _profiles(self) -> list[dict]:
        """场景列表（读合并后的有效配置，默认值归 ``config_schema()``）。

        只需过滤「不是对象」的条目：段写歪（`profiles: "abc"` 之类）已由 ``cfg``
        按声明类型回落成默认列表 —— 与其它配置键同一口径，不再各写一套兜底。
        真正非法的那一份并不会被吞掉：``rules.build_routes`` 仍会当场报错，
        由 ``routes`` 状态机说给用户听。
        """
        return [p for p in self.cfg.value("profiles") if isinstance(p, dict)]

    def _profile_by_name(self, name: str) -> dict:
        return next(
            (p for p in self._profiles() if str(p.get("name", "")).strip() == name), {}
        )

    def _scene_suppressed(self, name: str | None) -> bool:
        """该场景此刻是不是「不叠加」。覆盖层与配置层都算进显示态。

        两者在这里**看上去一样**，区别写在副标题里：「本次已撤回」（可换场景自动恢复）
        vs「配置已关闭」（回配置窗体改）。
        """
        if not name:
            return False
        overrides = self.cfg.value("rule_overrides")
        entry = overrides.get(name)
        if isinstance(entry, dict) and entry.get("routes_suppressed") is True:
            return True
        routes_block = self._profile_by_name(name).get("routes") or {}
        return routes_block.get("enabled", True) is False

    def _switch_checked(self) -> bool:
        """开关显示态 = 该场景**没有**被「不叠加」（§4.8：开 = 按配置叠加）。

        旧写法读 helper 回显的 ``applied``（实际生效态），于是开关会随下一次探活自己跳，
        用户没法预判它下次显示成什么 —— 他改的是一个「意图」，看到的却是「结果」。
        改成读**意图**后，显示的就是配置/覆盖要求的结果；「有没有真的叠上」归副标题说。
        """
        return not self._scene_suppressed(self._current_scene())

    def set_routes_suppressed(self, scene: str, suppressed: bool) -> "ActionResult | None":
        """面板开关：该场景**本次**要不要叠路由（覆盖层，§4.8）。

        True  ⇒ 撤掉该场景的全部路由（**IP 照常切**——这是路由层的事，与网卡配置无关）
        False ⇒ 恢复按配置叠加

        写入位置是独立的 ``rule_overrides`` 字典（按场景名索引），**不可** inline 写进
        profiles 列表：deep_merge 对 list 是「整体覆盖」，一旦把单个场景拷进用户 YAML 的
        profiles 列表，合并时就会把默认其它场景（如 home）整条抹掉，期望态随之丢失。
        用 dict 则递归合并、保留兄弟键，默认场景定义仍是唯一真相源。
        """
        suppressed = bool(suppressed)

        def _mutate(section: dict) -> None:
            overrides = section.setdefault("rule_overrides", {})
            entry = overrides.setdefault(scene, {})
            entry["routes_suppressed"] = suppressed

        ok, problem = self._write_section(_mutate, fail="操作失败")
        if not ok:
            return self._failed(problem)
        # 记下「这次覆盖属于哪个场景」：换场景即作废（§4.8 的清除时机）。
        self._last_scene = scene
        return None

    @staticmethod
    def _strip_suppressions(section: dict) -> None:
        """清掉段里所有覆盖项（只删覆盖位，别的键留着）。"""
        overrides = section.get("rule_overrides")
        if not isinstance(overrides, dict):
            return
        for name in list(overrides):
            entry = overrides[name]
            if isinstance(entry, dict):
                entry.pop("routes_suppressed", None)
                if not entry:
                    overrides.pop(name, None)
            else:
                overrides.pop(name, None)
        if not overrides:
            section.pop("rule_overrides", None)

    def _reap_suppressions(self) -> None:
        """换了场景就把覆盖层抹掉 —— 覆盖是「本次」的（§4.8 清除时机）。

        不做这件事，持久写在 YAML 里的 ``routes_suppressed`` 就跟配置层的
        ``routes.enabled`` 没有区别了，两套机制互相冒充。

        ⚠️ 本方法跑在 **worker 线程**（``tick``），故**不碰 UI**：不调 ``publish_snapshot``。
        UI 的更新由本轮 ``tick`` 的返回值带走（它在本方法之后才取快照），一个字节都不晚。
        这里写配置是不带锁的（``core/config`` 无锁），但唯一的另一个写者是主线程的用户点击，
        撞上的窗口极小，最坏也只是「这次没清掉」——下次换场景还会再清。
        """
        scene = self._current_scene()
        if scene == self._last_scene or scene is None:
            return
        self._last_scene = scene
        overrides = self.cfg.value("rule_overrides")
        if not any(isinstance(e, dict) and e.get("routes_suppressed")
                   for e in overrides.values()):
            return
        try:
            self.cfg.mutate(self._strip_suppressions)
        except Exception:  # noqa: BLE001 —— 清不掉只是「这次没清」，不该让 tick 挂掉
            log.exception("清除过期覆盖失败（下次换场景会重试）")
            return
        log.info("已切换到场景 %s：清掉上一次的「本次不叠加」覆盖", scene)
        try:
            self.sync_routes()
        except RouteConfigError as e:
            log.error("同步路由期望态失败：%s", e)

    def toggle_route_switch(self) -> "ActionResult | None":
        """面板开关点击：把「本次是否叠加」翻面。

        返回结局（``ActionResult``，D32-②）：写盘失败时开关会**弹回原位**（真值来自
        配置），不说一句用户只看到「点了开关它自己跳回去了」。
        """
        name = self._current_scene()
        if name is None:
            return None
        return self.set_routes_suppressed(name, not self._scene_suppressed(name))
