"""``_VariablesMixin``：顶层 ``variables:`` 段的读写 —— 框架「变量」页的唯一出入口。

**为什么读写同处一个模块**：这两件事守着同一条约束（声明与值分离），而那条约束一旦
在读写两侧各写一遍就会漂 —— 读侧以为「段里只有声明」、写侧顺手把今天填的值也带进去，
症状是配置里出现一份会随时间变的字段，而它每次读回来又覆盖不了运行态那份。

段的形状校验**不在这里**（在 ``core/config/schema.validate``）：写侧经 ``save_user``
必然过那道闸，读侧拿到的一定是过过闸的段 ⇒ 这里再判一次的话，其中一份永远不生效。
"""

from __future__ import annotations


class _VariablesMixin:
    """框架级变量声明的读 / 写（同一份 ``variables:`` 段的两个方向）。"""

    def variables(self) -> list:
        """框架级变量**声明**（``config.variables`` 段的原样内容）。

        声明不是值：运行值一律不进配置（``ask`` 的回答按日落在
        ``variables_state.json``、``derived`` 现算、``component`` 实时推），见
        ``core/variables/spec`` 的模块 docstring。故这里读到的是一份可以安全回写的
        清单，改它（设置页写回）不会碰到用户今天填过的东西。

        段缺省 ⇒ 空列表。形状不合法的段在 :func:`schema.validate` 那一关就被
        ``ConfigError`` 拒了（「哪个声明说的是哪一个变量」没有唯一答案时不该放行），
        故这里**不再判一次** —— 一处判定写两份时，其中一份永远不生效。
        """
        found = self._data.get("variables")
        return list(found) if isinstance(found, list) else []

    def set_variables(self, declarations: list) -> None:
        """整体写顶层 ``variables:`` 段（框架「变量」页的唯一写入口）。

        **为什么是整体写而不是「按 id 增删改」三件**：段的形状是 ``list``、每条声明的
        id 唯一性由 ``variables.spec.check_section`` 一次判完（那条校验本来就吃整段），
        拆成三件就得在每一件里各判一次「id 撞了没」—— 一处判定两份实现，且两份迟早分叉。

        **先校验再落盘**：``save_user`` 自己会走 ``schema.validate``，坏声明到不了盘上
        （抛 ``ConfigError``，调用方接住并把那句话给用户看）。

        空列表 ⇒ **删键**（不留 ``variables: []`` 这种残渣，与 ``set_plugin_ui_visible``
        那条「等于默认就删键」同一口径）。

        **不广播** ``changed``：本方法是纯写入（与 ``set_plugin_poll`` / ``update_plugin_section``
        一致），「写完谁该知道」归调用方 —— 框架侧见 ``AppShell._on_variables_config_changed``。
        """
        user = self._read_user_raw()
        if declarations:
            user["variables"] = [dict(item) for item in declarations]
        else:
            user.pop("variables", None)
        self.save_user(user)
