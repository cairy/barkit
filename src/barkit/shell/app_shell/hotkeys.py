"""热键注册、回调表与重挂（框架内置 ＋ 组件声明 ＋ 用户自建）。

``app_shell.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from dataclasses import replace

from barkit.core import thread_reap
from barkit.core.hotkey_command import CommandRunner
from barkit.shell.hotkey_catalog import (
    FEEDBACK_DEFAULTS,
    FEEDBACK_NONE,
    FRAMEWORK_ACTIONS,
    REBIND_BOUND,
    REBIND_FAILED,
    REBIND_UNBOUND,
    FrameworkAction,
    SOURCE_CUSTOM,
    SOURCE_FRAMEWORK,
    SOURCE_PLUGIN,
    effective_combo,
    read_customs,
)
from barkit.shell.hotkey_feedback import wrap_hotkey_callback
from barkit.shell import placement

from .consts import _EXIT_GRACE_MS, log


class _HotkeyMixin:
    """热键的注册 / 回调表 / 重挂 / 撞车判定，以及自建条目（``_run_custom`` 起的命令线程也在这一族）。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    def _register_hotkeys(self, contributions, config, hotkeys) -> None:
        """热键统一从贡献注册中心取（崩溃隔离已在注册中心内完成）。

        组件声明的回调包一层反馈（见 ``_wrap_with_feedback``）：让「逐条反馈方式」
        这个设置对组件条目也真正生效，而不是一个写着 A 实际不做事的下拉框。

        键位一律走 ``effective_combo``（与重挂 / 目录展示同一处）：早先这里写的是
        ``config.get(..., spec.default_combo)``，与重挂那边的空串兜底是**两份口径**，
        「恢复出厂键位」之后两边读出不同的键位（见 :func:`effective_combo`）。
        不占键时**留一条日志**——「按了没反应」最难自查，而静默跳过让人无处下手。
        """
        try:
            specs = contributions.hotkeys()
        except Exception:  # noqa: BLE001
            log.exception("热键贡献收集中抛异常，已跳过")
            return
        for spec in specs:
            combo = effective_combo(config, spec.name, spec.default_combo or "")
            if not combo:
                log.info("热键 %s 未注册：当前键位为空（停用或出厂没给键位）", spec.name)
                continue
            if not hotkeys.register(
                spec.name, combo,
                self._wrap_with_feedback(spec.name, SOURCE_PLUGIN, spec.callback),
            ):
                # 失败原因已由注册表自己存下（``HotkeyManager.failure_of``）：
                # 这一刻没有界面在场，报不出可见的提示，但用户事后翻到「快捷键」页时
                # 那一行的副标题会写着为什么没挂上 —— 比「按了没反应 + 一行日志」强。
                log.warning("热键 %s 注册失败（原因已记进快捷键页）：%s",
                            spec.name, hotkeys.failure_of(spec.name))

    # ---------- 框架内置热键 ----------

    def _framework_actions(self) -> tuple[FrameworkAction, ...]:
        """框架内置动作：给目录里那份**纯名单**接上 ``run``。

        用 ``replace`` 而不是就地改：``FRAMEWORK_ACTIONS`` 是模块级常量，就地改会让它
        在两次装配之间串味（测试里 assemble 两次就互相污染）。
        """
        runs = {
            "framework.panel": self._toggle_panel,
            "framework.settings": self._open_settings,
        }
        return tuple(replace(a, run=runs.get(a.name)) for a in FRAMEWORK_ACTIONS)

    def _custom_specs(self) -> list:
        """当前的自建条目（配置里读出来，每次都读：**它可能刚被设置页改过**）。"""
        if self._config is None:
            return []
        try:
            return read_customs(self._config)
        except Exception:  # noqa: BLE001 —— 一条配置写坏不该让整条热键链塌掉
            log.exception("自建快捷键读取失败，已按「没有自建」继续")
            return []

    def _custom_callback(self, name: str):
        """自建条目的回调（**按名字缓存**，见 ``__init__`` 里那条注释）。"""
        cached = self._custom_callbacks.get(name)
        if cached is None:
            def _fire(n: str = name) -> None:
                self._run_custom(n)
            self._custom_callbacks[name] = _fire
            cached = _fire
        return cached

    def _hotkey_callbacks(self) -> dict:
        """热键名 → 回调（框架动作 + 组件声明 + 用户自建）。重挂与冲突判定都查这一张表。"""
        out = {a.name: a.run for a in self._framework_actions() if a.run is not None}
        if self._contributions is not None:
            for spec in self._contributions.hotkeys():
                if spec.callback is not None:
                    out[spec.name] = spec.callback
        for spec in self._custom_specs():
            out[spec.name] = self._custom_callback(spec.name)
        return out

    def _hotkey_title(self, name: str) -> str:
        """热键名 → 显示名：冲突时要说「被**谁**占了」，内部键名（recognize）不算回答。"""
        for action in self._framework_actions():
            if action.name == name:
                return action.title
        if self._contributions is not None:
            for spec in self._contributions.hotkeys():
                if spec.name == name:
                    return spec.label or spec.name
        for spec in self._custom_specs():
            if spec.name == name:
                return spec.title or name
        return name

    def _register_framework_hotkeys(self) -> None:
        """框架内置热键的注册：**晚于**菜单栏图标 —— 它们的回调要碰面板与按钮矩形。

        回调包一层反馈（默认 ``none``，因为面板弹出本身就是反馈）。
        """
        if self._hotkeys is None:
            return
        for action in self._framework_actions():
            if action.run is None:
                continue
            combo = effective_combo(self._config, action.name, action.default_combo)
            if not combo:
                log.info("框架热键 %s 未注册：当前键位为空（停用或出厂没给键位）",
                         action.name)
                continue
            if not self._hotkeys.register(
                action.name, combo,
                self._wrap_with_feedback(action.name, SOURCE_FRAMEWORK, action.run),
            ):
                # 与组件那批同一条口径：原因存进注册表，等用户翻到「快捷键」页时可见
                # （早先这里连返回值都没看 —— 框架那两条挂不上是彻底无声的）。
                log.warning("框架热键 %s 注册失败（原因已记进快捷键页）：%s",
                            action.name, self._hotkeys.failure_of(action.name))

    def _register_custom_hotkeys(self) -> None:
        """自建条目：配置里有什么键位就挂什么（与组件那批同一条注册口径）。"""
        if self._hotkeys is None:
            return
        for spec in self._custom_specs():
            # 自建条目**没有出厂键位**（它的出厂态就是「不存在」）⇒ 默认值恒为空串
            combo = effective_combo(self._config, spec.name, "")
            if not combo:
                log.info("自建快捷键 %s 未注册：还没录键位", spec.name)
                continue
            if not self._hotkeys.register(spec.name, combo,
                                          self._custom_callback(spec.name)):
                log.warning("自建快捷键 %s 注册失败，请在快捷键页重录一个键位", spec.name)

    def _run_custom(self, name: str) -> None:
        """触发一条自建快捷键：**把命令挪到后台线程**再跑。

        热键回调跑在主线程上，同步跑命令最坏要 10 秒（``TIMEOUT_SEC``）—— 那 10 秒里
        整个 app 是僵的，用户看到的就是「按了没反应」，正好是这一页要消灭的现象。
        """
        spec = next((s for s in self._custom_specs() if s.name == name), None)
        if spec is None or not spec.command:
            return
        runner = CommandRunner(spec.command)
        self._runners.append(runner)   # 持有引用：见 __init__ 里那条注释
        runner.result.connect(lambda result, s=spec: self._report_custom(s, result))
        runner.finished.connect(lambda r=runner: self._forget_runner(r))
        runner.start()

    def _forget_runner(self, runner) -> None:
        """线程自己跑完 ⇒ 从持有表里摘掉。**这条是唯一可以丢引用的时机**：此刻它
        已经结束，析构才安全（早一步就是 2026-09-29 那次崩溃的形态）。"""
        if runner in self._runners:
            self._runners.remove(runner)
        runner.deleteLater()

    def _stop_runners(self) -> None:
        """退出时收还在跑的命令线程。收不住的转 ``thread_reap`` 代持 —— 绝不能在
        这里直接丢引用。"""
        for runner in list(self._runners):
            if thread_reap.stop(runner, _EXIT_GRACE_MS):
                self._forget_runner(runner)

    def _report_custom(self, spec, result) -> None:
        """把一次自建触发的结局送出去：成功按条目选的反馈方式，失败一律通知。"""
        title = spec.title or spec.name
        if not result.ok:
            log.warning("自建快捷键「%s」执行失败：%s %s", title, result.headline, result.output)
        self._feedback.report(
            name=spec.name, title=title, ok=result.ok,
            detail=result.headline, mode=spec.feedback,
        )

    def _rebind_hotkey(self, name: str) -> str:
        """按**当前配置**重挂一条热键（设置页写完盘就调它），返回 ``REBIND_*`` 之一。

        键位**必须**走 :func:`effective_combo`（与启动那条链同一处）。早先这里写的是
        ``config.get(f"hotkeys.{name}", "")`` —— 缺键时兜底成空串，于是「恢复出厂键位」
        （写侧＝删键）被读成「停用」⇒ 撤掉绑定还回报成功，界面说「已启用」而这一条在
        当前进程里再也没挂上，重启才好（启动时那条链用出厂值兜底）。

        三种结果**不再合成一个布尔**：撤掉与真挂上都是达成了配置要求的那一头，
        只有 ``REBIND_FAILED`` 是要说给用户听的失败（见那三个常量）。

        非自建条目要包一层反馈（见 ``_wrap_with_feedback``）；自建那条回调自带
        反馈（``_run_custom`` → ``_report_custom``），不能再包一层，否则双重回执。
        """
        if self._hotkeys is None:
            log.warning("热键 %s 重挂失败：热键管理器尚未装配", name)
            return REBIND_FAILED
        self._hotkeys.unregister(name)
        combo = effective_combo(self._config, name, self._default_combo_of(name))
        if not combo:
            log.info("热键 %s 已撤下：配置里它是停用的", name)
            return REBIND_UNBOUND
        cb = self._hotkey_callbacks().get(name)
        if cb is None:
            log.warning("热键 %s 重挂失败：找不到它的回调（组件已卸载？）", name)
            return REBIND_FAILED
        if self._source_of(name) == SOURCE_CUSTOM:
            ok = self._hotkeys.register(name, combo, cb)
        else:
            ok = self._hotkeys.register(
                name, combo, self._wrap_with_feedback(name, self._source_of(name), cb),
            )
        if not ok:
            log.warning("热键 %s 重挂失败：键位 %s 没挂上（权限 / 键位非法 / 撞车）",
                        name, combo)
            return REBIND_FAILED
        return REBIND_BOUND

    def _default_combo_of(self, name: str) -> str:
        """热键名 → **出厂键位**（自建条目没有出厂键位 ⇒ 空串）。

        重挂要它：配置里缺键时该挂什么，只有出厂值答得出来。
        """
        for action in self._framework_actions():
            if action.name == name:
                return action.default_combo
        if self._contributions is not None:
            for spec in self._contributions.hotkeys():
                if spec.name == name:
                    return spec.default_combo or ""
        return ""

    def _source_of(self, name: str) -> str:
        """热键名 → 来源（框架 / 组件 / 自建），回调包装与默认值靠它。"""
        for action in self._framework_actions():
            if action.name == name:
                return SOURCE_FRAMEWORK
        if self._contributions is not None:
            for spec in self._contributions.hotkeys():
                if spec.name == name:
                    return SOURCE_PLUGIN
        return SOURCE_CUSTOM

    def _wrap_with_feedback(self, name: str, source: str, cb) -> "object":
        """给一条非自建热键的回调包一层反馈分派。

        框架 / 组件动作执行完自身多半就有可见结果（面板弹出、识别浮层），
        默认 ``none`` 不补；这里包一层是把「逐条反馈方式」这个设置真正接上触发链路，
        **并且接收回调返回的 ``ActionResult``** —— 热键按下去时面板多半是关着的，这条
        反馈通道（系统通知 / 轻浮层）就是组件唯一能把结局送到用户眼前的地方。这正是
        ``HotkeySpec.callback`` 那份注释里写的启动条件（2026-09-28 满足后扩宽）。

        判定与「跑完就报」的包装都在 :func:`hotkey_feedback.wrap_hotkey_callback` ——
        那半边本机可测（宿主这条链牵起 PyObjC，Windows 上 import 不到）。这里只负责把
        标题与档位两个**读取口**接上：两者都必须在**触发那一刻**读，不能在登记时定死。
        """
        default = FEEDBACK_DEFAULTS.get(source, FEEDBACK_NONE)
        return wrap_hotkey_callback(
            cb,
            name=name,
            feedback=self._feedback,
            title_of=lambda: self._hotkey_title(name),
            mode_of=lambda: (
                self._config.hotkey_feedback(name, default) if self._config else default
            ),
        )

    def _hotkey_conflict(self, name: str, combo: str) -> "str | None":
        """这条键位被谁占了（占用者**显示名**），没撞车给 None。

        比较基准必须用**表里已登记的回调**：非自建条目登记时包了一层反馈
        （``_wrap_with_feedback``），而 ``_hotkey_callbacks()`` 给的是原始回调，
        两者不相等会让 ``HotkeyTable.conflict`` 把「自身」误判成「同名被别的回调占用」
        —— 表现就是改键永远报「已被自己占用，没有改动」。回查表里那条真身，
        自身恒相等，只有真正撞到别的名字才报冲突；两个插件声明同名那条 genuin
        守卫在 ``register`` 里（它拿的是新回调，与本条目不相等，照常拒绝）。
        """
        if self._hotkeys is None:
            return None
        existing = self._hotkeys.get(name)
        cb = existing.callback if existing is not None else None
        found = self._hotkeys.conflict(name, combo, cb)
        if found is None:
            return None
        return self._hotkey_title(found.owner)

    def _toggle_panel(self) -> None:
        """框架动作「打开面板」：走**点菜单栏图标那条链**，不另写一套显隐。

        这条来路没有点击点（按的是热键 / 命令），故锚只能回读图标窗口那块矩形
        —— 由 ``anchor_of_status_item`` 负责取不到时的退路。
        """
        if self.panel is None:
            return
        self.panel.on_status_item_click(placement.anchor_of_status_item(self.status_item))

    def _open_settings(self) -> None:
        """框架动作「打开设置」：打开并**切到快捷键页** —— 按这个键的人多半要改键。"""
        if self._settings is None:
            return
        # 锚与「打开面板」同一条来路（都没有点击点）⇒ 共用 ``anchor_of_status_item``，
        # 别在这里另取一次矩形：那会绕过「锚必须是 ``placement.Anchor``」这条约定。
        self._settings.show_and_raise(placement.anchor_of_status_item(self.status_item))
        self._settings.select_page("快捷键")
