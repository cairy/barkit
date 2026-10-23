"""``_TuningMixin``：状态机优先级与框架节拍的读-改-写。

``config.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations


class _TuningMixin:
    """用户对框架的覆盖键：各状态机优先级与组件节拍；恢复默认一律**删键**。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 状态机优先级：用户覆盖键（声明顺序的可调版本） ----------

    def plugin_state_priorities(self, plugin_id: str) -> dict[str, int]:
        """该组件各状态机**写过**的优先级覆盖：``{状态机 key: priority}``。

        键形 ``plugins.<id>.states.<key>.priority``。**只返回用户显式写过的**——
        没写过的状态机不在字典里，由调用方回落「声明顺序序号」（组件作者写的关注度
        顺序）。这个区分很要紧：合并后的配置给不出「用户改没改过」，而优先级恰好是
        「没改过就该等于声明顺序」——写死一个默认数字会把作者日后调整声明顺序的那份
        意图挡在用户盘外。

        数值**小者优先**（1 = 最该被看见）。

        写坏了（非整数 / 布尔 / 结构损坏）一律**忽略该台**，不静默用一个用户没打算要的
        数字 —— 与 :meth:`plugin_poll` 同一个口径：宁可回落声明顺序。
        """
        states = self.user_plugin_section(plugin_id).get("states")
        if not isinstance(states, dict):
            return {}
        out: dict[str, int] = {}
        for key, cfg in states.items():
            if not isinstance(cfg, dict):
                continue
            raw = cfg.get("priority")
            # bool 是 int 的子类：``priority: true`` 是手改 YAML 写歪了，不算数
            if isinstance(raw, bool) or not isinstance(raw, int):
                continue
            out[str(key)] = raw
        return out

    def set_plugin_state_priority(
        self, plugin_id: str, key: str, priority: "int | None"
    ) -> None:
        """写 / 删某台状态机的优先级（``None`` = 回到声明顺序，**删键**）。

        走 ``update_plugin_section`` 的同一条读-改-写路径（原子写 ＋ chmod 600）。
        恢复默认**删键**而不是写回声明序号：序号会随组件作者增删状态机而漂移，钉一个
        死数字在用户盘上，日后作者调整声明顺序就传不到用户手上 —— 与
        :meth:`set_plugin_poll(None)` 删键同理。

        ``key`` 是该状态机的声明 key（一台机时通常是 ``"main"``），由调用方从
        ``StateSpec.key`` 取；配置层不认识状态机，只管存。

        随之变空的 ``states`` / ``<key>`` 逐层清理，不留 ``states: {}`` 残渣。
        """
        if not key:
            raise ValueError("状态机 key 不得为空（一台机时的保留键是 'main'）")

        def _mutate(section: dict) -> None:
            states = section.get("states")
            if not isinstance(states, dict):
                states = {}
            cfg = states.get(key)
            if not isinstance(cfg, dict):
                cfg = {}
            if priority is None:
                cfg.pop("priority", None)
            else:
                cfg["priority"] = int(priority)
            if cfg:
                states[key] = cfg
            else:
                states.pop(key, None)
            if states:
                section["states"] = states
            else:
                section.pop("states", None)

        self.update_plugin_section(plugin_id, _mutate)

    # ---------- 框架节拍：用户覆盖键（契约 D21 ＋ D27） ----------

    def plugin_poll(self, plugin_id: str) -> float | None:
        """用户写过的框架节拍覆盖值（``plugins.<id>.poll``，秒）；没写过 → ``None``。

        读的是**用户差异层**：「用户改没改过」只存在于那里 —— 合并后的配置里，
        组件建议值与用户写入值长得一样（都是那个数字）。默认值不在这里给：
        它归组件声明（``suggested_poll``），由 ``scheduler.poll_default_of`` 算。

        写坏了（非数字 / ≤0 / 布尔）一律按「没改过」处理 —— 与
        :meth:`plugin_ui_visible` 同一个口径：宁可回落默认值，也不静默用一个
        用户没打算要的数字（调度器那边还会再记一条 warning，两处都不撒谎）。
        """
        raw = self.user_plugin_section(plugin_id).get("poll")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
            return None
        return float(raw)

    def set_plugin_poll(self, plugin_id: str, seconds: float | None) -> None:
        """写 / 删框架节拍覆盖值（``None`` = 恢复默认，**删键**）。

        走 ``update_plugin_section`` 的同一条读-改-写路径（原子写 ＋ chmod 600）。
        恢复默认删键而不是写回默认值：留一个「与默认同值的覆盖」会让组件日后调整
        ``suggested_poll`` 传不到用户手上（用户盘上钉着一个旧数字，界面上看不出
        区别）—— 与 :meth:`set_plugin_ui_visible(visible=True)` 删键同理。

        **不校验** ``seconds`` 是否为该组件的合法节拍：合法性归调度器
        （``Scheduler._interval_for`` 会回退建议值并留痕），配置层只管存。
        """

        def _mutate(section: dict) -> None:
            if seconds is None:
                section.pop("poll", None)
            else:
                section["poll"] = seconds

        self.update_plugin_section(plugin_id, _mutate)
