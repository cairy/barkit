"""「复原系统状态」：卸载流程走不到的那条路（§5 A2）。

为什么单列一个文件
------------------
「组件留下了哪些系统状态」这件事其实**早就有两处落点**：声明格 ``system_mutations``
（一句话人话 ＋ 自持型还是依附型）与复原钩子 ``Plugin.teardown``（三个内置组件都已
实现）。卡的只是**没有入口**：唯一调到 ``teardown`` 的那条路是卸载七步，而它第一关
就拒内置组件 —— 恰恰声明了系统变更的多是内置的。于是这三份代码从上线起一次都没被
跑到过。

本文件提供两件：**列清单**（谁留下了什么）与**执行**（逐个复原）。它**不是**卸载：
不删配置、不删数据文件、不动插件集合 —— 那是「插件」页的事（全局动作只留一处落点）。
"""
from __future__ import annotations

from barkit.core.resources import SystemMutation

from .consts import log


class _RevertMixin:
    """列出「谁在系统里留下了东西」并按勾选逐个复原。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    def declared_system_mutations(self) -> "list[tuple[str, list[SystemMutation]]]":
        """已加载组件里**声明过系统变更**的那些：``(plugin_id, 变更清单)``。

        **只认已加载实例**：没在跑的组件此刻也没有可供复原的运行期状态（``teardown``
        只对已加载实例有意义），列出来就是给用户一个必然失败的选择。声明的读取走
        ``_probe_resources``（与卸载同一条口径），异常按「没声明」处理。
        """
        found: list[tuple[str, list[SystemMutation]]] = []
        for plugin_id, instance in sorted((self._registry.plugins or {}).items()):
            resources = self._probe_resources(plugin_id, None, instance)
            mutations = list(getattr(resources, "system_mutations", None) or [])
            if mutations:
                found.append((plugin_id, mutations))
        return found

    def revert_system_state(self, plugin_id: str) -> bool:
        """复原**这一个**组件造成的系统状态（不走卸载流程，内置组件同样可执行）。

        顺序是承重的那条：**先停手（``disable``），再复原（``teardown``）**。反过来做，
        组件下一拍就会把它刚撤掉的再写回去 —— 用户看见的是「撤了、又回来了」，而日志
        里两个动作都显示成功。这与卸载七步里 2b→3 的次序同源。

        返回 False 的两种情形都**如实说清**：组件没在跑（无从复原），或 ``teardown``
        抛异常（撤到一半）。两者都不该被当成「已恢复」。
        """
        instance = (self._registry.plugins or {}).get(plugin_id)
        if instance is None:
            log.warning("复原系统状态跳过 %s：组件未加载，没有可复原的运行期状态",
                        plugin_id)
            return False
        # 长驻服务的卸载语义收尾必须在 disable 之前（disable 内的 stop_services 只
        # 「停」，记录一丢就再也拿不到 service.teardown()）。
        self._registry.teardown_services(instance)
        self.disable(plugin_id)
        try:
            instance.teardown()
        except Exception:  # noqa: BLE001 —— 单个组件撤不干净不该掀翻整批
            log.exception("组件 %s 复原系统状态失败", plugin_id)
            return False
        log.info("已复原组件 %s 造成的系统状态", plugin_id)
        self._notify_changed()
        return True
