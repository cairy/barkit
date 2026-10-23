"""PluginContext 注入口的形状契约（``core/plugin.py::verify_context``）。

三个注入口（``interaction`` / ``helper`` / ``variables``）的静态类型是 core 自己声明的
Protocol，运行期那一半由 ``verify_context`` 在装配期查一遍。三条判据缺一条，这个机制就
退化成装饰品：

1. **真实实现类满足各自的 Protocol** —— 防「Protocol 写着 A、实现类早改成 B」；
2. **不合格对象被拒且说清缺什么** —— 防「抛是抛了，接到消息的人不知道缺哪个」；
3. **``PluginContext`` 不许有裸 ``object`` 字段** —— 新加的注入口必须给形状并登记，
   登记了才进 ``verify_context`` 的扫描面（不然新口子静默游离在契约之外）。
"""

from __future__ import annotations

from dataclasses import fields

import pytest

from barkit.core.plugin import (
    HelperPort,
    InteractionPort,
    PluginContext,
    VariablesPort,
    _port_members,
    verify_context,
)


def test_real_implementations_satisfy_their_ports():
    """装配层实际注入的那三个对象必须满足各自 Protocol。

    这一条是「Protocol 与实现同步」的唯一判据：实现类改了方法名而不改 Protocol 时，
    契约注释会继续骗人，而这里当场红。
    """
    from barkit.capabilities.helper_link import HelperLink
    from barkit.core.variables.pool import VariablePool
    from barkit.shell.interaction import InteractionCenter

    pairs = (
        (InteractionCenter, InteractionPort),
        (HelperLink, HelperPort),
        (VariablePool, VariablesPort),
    )
    for impl, port in pairs:
        missing = [
            name for name in _port_members(port)
            if not callable(getattr(impl, name, None))
        ]
        assert not missing, f"{impl.__name__} 不满足 {port.__name__}：缺 {missing}"


def test_verify_context_skips_unwired_fields():
    """未接线（``None``）是契约允许的降级态，不得当成错误。

    测试桩与「该路未起」两种情形都走这条 —— 校验若在这里抛，等于把「可选的注入口」
    变成「必须接线」，与 ``默认 None ⇒ 现有测试桩零改动`` 那条既有约定冲突。
    """
    ctx = PluginContext(config=object())
    verify_context(ctx)   # 三个口全 None

    class Anything:
        pass

    ctx.interaction = Anything()   # 接线了，但不合格
    with pytest.raises(RuntimeError):
        verify_context(ctx)


def test_verify_context_rejects_object_missing_port_methods():
    """不合格对象被拒，且消息里点出缺哪些方法（装配现场能直接照着修）。"""

    class HalfDone:
        """只实现了 register，缺 ask_now。"""

        def register(self, spec, on_result=None, on_shown=None) -> None:
            return None

    ctx = PluginContext(config=object())
    ctx.interaction = HalfDone()
    with pytest.raises(RuntimeError) as exc:
        verify_context(ctx)
    message = str(exc.value)
    assert "interaction" in message
    assert "ask_now" in message
    # 已实现的那个不该被报成缺失
    assert "register" not in message

    # 补上之后即通过（证明拒的是那一个方法，不是一个笼统的「不合格」）
    HalfDone.ask_now = lambda self, spec, on_result=None: True
    verify_context(ctx)


def test_plugin_context_has_no_bare_object_field():
    """``PluginContext`` 不许出现裸 ``object`` 型的字段。

    ``object`` 型注入口的调用面在运行期不可读 —— 组件侧只能 ``getattr`` 探测、探不到
    就静默降级，接错线要到某条业务路径上才显形。新加注入口一律给 Protocol 或
    ``Callable[...]``，并（若是 Protocol）登记进 ``core/plugin._PORT_FIELDS``。

    直接读 ``dataclasses.fields`` 的原始注解字符串，不解析 —— 本模块 ``Config`` 等名字
    是 ``TYPE_CHECKING`` 导入的，``typing.get_type_hints`` 在运行期解析不动。
    """
    # 注解在 ``from __future__ import annotations`` 下整体被字符串化，作者手写的引号
    # （``interaction: "object | None"``）会一并进 ``f.type`` ⇒ 比较前先剥引号与空白。
    def _bare(type_str: str) -> bool:
        return type_str.strip("\"'").replace(" ", "") == "object|None"

    bare = [f.name for f in fields(PluginContext) if _bare(f.type)]
    assert not bare, (
        f"PluginContext 有裸 object 型字段 {bare}：给它一个形状（Protocol / Callable），"
        "否则 verify_context 看不见它"
    )
