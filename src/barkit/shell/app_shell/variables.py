"""变量池（框架级）那一段的组合根：建宿主、接总线与心跳、给设置窗注入取值口。

``app_shell.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from barkit.core.variables import VariableHost
from barkit.shell.variables_page import VariablesHost

from .consts import log


class _VariablesMixin:
    """变量池的装配与接线（``_build_variables`` / 配置热重载那一路 / 给界面那个口）。

    与其它 ``_build_*`` 的分工：这里只**接线**（谁订阅谁、谁的回调挂哪儿），
    「变量怎么算」全在 ``core/variables`` —— 组合根不认识表达式引擎，也不该认识。
    """

    def _build_variables(self, config, events, heartbeat, interaction) -> VariableHost:
        """装配变量池宿主并启动。

        **必须在交互中枢 ``mark_started()`` 之前**：ask 规则要在那一刻之前登记完，
        否则 ``when: [startup]`` 那一路的首轮求值收不到它（那一轮就是「程序已启动」
        这一位唯一一次被置位的时刻）。
        """
        host = VariableHost(events, heartbeat, interaction=interaction)
        host.start(config.variables())
        for problem in host.pool.problems():
            # 加载时跳过 / 记下的那些**要出声**：不出声的话用户在配置里写错一条，
            # 界面上什么也看不出来，只有那一条变量静静地不生效。
            log.warning("变量声明：%s", problem)
        self._variables = host
        return host

    def _variables_view(self) -> "VariablesHost | None":
        """给设置窗的取值口。

        设置窗不认识 ``core/variables`` 那个宿主类（也不该认识），它只认识这五个回调 ——
        与「快捷键」页拿 ``HotkeyHost`` 同款。
        """
        host = self._variables
        if host is None:
            return None
        return VariablesHost(
            rows=host.declared_rows,
            probe=host.probe,
            ask_now=host.ask_now,
            problems=host.pool.problems,
            on_changed=self._on_variables_config_changed,
        )

    def _on_variables_config_changed(self) -> None:
        """配置里的 ``variables`` 段变了（设置页写回，或手改 YAML 触发热重载）。

        **两条路都走这里**（热重载那一路由 ``_on_config_changed`` 转达）：重载要做的
        事完全一样 —— 按新声明重挂订阅、重登记询问规则。写在两处迟早只有一处被改。
        """
        host = self._variables
        if host is None:
            return
        declarations = self._config.variables() if self._config is not None else []
        host.load(declarations)
        for problem in host.pool.problems():
            log.warning("变量声明：%s", problem)
