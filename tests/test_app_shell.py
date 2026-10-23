"""shell/app_shell 装配器单测（组合根接线 + 全链路冒烟）。

两部分：
- **A. 接线事实**：单点断言（如 ``_wire_context`` 接向 ``Scheduler.mark_dirty``）。
- **B. 全链路冒烟**（《重构-开工手册》§4.3 的 ⑧）：真件走完整条 ``assemble()`` → 断言装配产物
  → ``_shutdown()`` → 断言无残留。原欠账是「路径隔离靠手工矩阵、会静默腐化」，
  已由「生产代码禁止按值导入路径常量」消除——隔离**按构造成立**，见
  ``test_architecture.py`` 的两道路径门禁与 conftest 的单一真源说明。
"""

from __future__ import annotations

import ast
from pathlib import Path

from PySide6.QtCore import QRect

import barkit.shell.app_shell
from barkit.core.config import Config
from barkit.core.events import EventBus
from barkit.core.heartbeat import Heartbeat
from barkit.core.registry import Registry
from barkit.core.scheduler import Scheduler
from barkit.shell.app_shell import AppShell


def _core(tmp_path):
    """最小 core 三件套（config 指向 tmp，绝不触碰真实用户配置）。"""
    user = tmp_path / "config.yaml"
    user.write_text("{}\n", encoding="utf-8")
    events = EventBus()
    scheduler = Scheduler(events, Heartbeat(beat_ms=1000))
    registry = Registry(Config(user_path=user), events)
    return scheduler, registry


def test_wire_context_points_mark_dirty_at_scheduler(qtbot, tmp_path):
    """§8.2.2（批次 10）：组合根把共享 ctx 的 mark_dirty 接向 Scheduler.mark_dirty。

    ctx 默认 None（测试桩零改动的既有契约），接线后必须是**同一个可调用对象**
    ——不是包一层看不出差别的 lambda，否则后续很难断言接线是否真的发生。
    """
    scheduler, registry = _core(tmp_path)
    assert registry.ctx.mark_dirty is None            # 默认不接线
    AppShell._wire_context(registry, scheduler)
    assert registry.ctx.mark_dirty == scheduler.mark_dirty
    scheduler.stop_all()


def test_wire_context_is_idempotent(qtbot, tmp_path):
    """重复接线幂等（启动期只需一次，但重复调用不得产生副作用）。"""
    scheduler, registry = _core(tmp_path)
    AppShell._wire_context(registry, scheduler)
    AppShell._wire_context(registry, scheduler)
    assert registry.ctx.mark_dirty == scheduler.mark_dirty
    scheduler.stop_all()


def test_mark_dirty_wired_end_to_end_toggles_dirty_flag(qtbot, tmp_path):
    """接线后的实际效果：ctx.mark_dirty(id, reason) 真的落到调度器上。"""
    scheduler, registry = _core(tmp_path)
    AppShell._wire_context(registry, scheduler)
    registry.ctx.mark_dirty("no-such-plugin", "x")     # 未注册 id ⇒ 安全空转
    assert scheduler.status("no-such-plugin")["dirty"] is False
    scheduler.stop_all()


def test_panel_open_is_wired_to_the_framework_event(qtbot, qapp):
    """「面板刚被打开」→ 框架事件 ``app.panel_opened``（§8.2.1 第三类 topic，``app.<event>``）。

    面板只报告自己打开了，**发什么、发给谁是组合根这一段接线的事**（面板不认 topic，
    与「面板不读配置」同款）。故判据落在 ``_build_panel`` 这一层，不走 ``assemble()``
    ——那条链要建菜单栏图标与全局热键。

    载荷为空是**约定**而不是省事：这一刻发生的事本身就是全部信息，没有「因为什么」可带；
    顺手塞个时间戳之类的进去，订阅方就会开始依赖一个没人定义过含义的字段。
    """
    from barkit.core.events import PANEL_OPENED_TOPIC

    events = EventBus()
    shell = AppShell(qapp)
    panel = shell._build_panel(Scheduler(events), events)
    qtbot.addWidget(panel)

    seen: list = []
    events.subscribe(PANEL_OPENED_TOPIC, lambda payload: seen.append(payload))

    panel.toggle_at(None)                    # 打开
    assert seen == [None], "面板打开没有发到框架事件上"

    panel.toggle_at(None)                    # 关闭：不是一次「打开」
    assert seen == [None]


# ──────────────────────────────────────────────
# B. 全链路冒烟（《重构-开工手册》§4.3 的 ⑧）
# ──────────────────────────────────────────────

class _StubStatusItem:
    """替代原生 NSStatusItem：真件会往**真机菜单栏**插一个图标。

    菜单栏集成本身由 ``test_status_item_coords`` 等纯函数用例覆盖，本测试关心的是
    装配图，故只保留装配链路会调到的接口。
    """

    created: list = []

    def __init__(self, on_click, pixmap=None) -> None:
        self.available = True
        self.badges: list = []
        self.base_icons: list = []      # 托盘底图来源（P0③）：记录每次换源
        self.tooltip: str | None = None
        self.on_click = on_click
        _StubStatusItem.created.append(self)

    def set_badge(self, level, icon=None) -> None:
        self.badges.append((level, icon))

    def set_base_icon(self, icon) -> None:
        self.base_icons.append(icon)

    def set_tooltip(self, text: str) -> None:
        self.tooltip = text

    def button_rect_qt(self):
        return QRect(0, 0, 24, 24)

    def button_size(self):
        return (24, 24)

    def button_screen(self):
        return None

    def redraw(self) -> None:
        pass

    def teardown(self) -> None:
        """对齐 StatusItem 新接口：真件会断主题信号并摘原生 NSStatusItem。"""
        self.torn_down = True


class _StubHotkeys:
    """替代 HotkeyManager：真件会向系统注册**全局热键**（CGEventTap）。

    装配链路的关注点是「插件声明的热键被收集并注册」这一事实，故记录调用而不碰
    Quartz。热键解析/事件分发由 ``test_hotkeys`` 覆盖。
    """

    def __init__(self) -> None:
        self.registered: dict[str, str] = {}
        self.unregistered = False

    @property
    def available(self) -> bool:
        """真件按「CGEventTap 建不建得起来」报可用（装配期要读它填设置页的取数口）。

        本桩不碰 Quartz，一律报可用 —— 链路用例关心的是「声明被收集并注册」这一事实。
        """
        return True

    def register(self, name: str, combo: str, callback) -> bool:
        self.registered[name] = combo
        return True

    def unregister_all(self) -> None:
        self.unregistered = True
        self.registered.clear()


def test_assemble_full_chain_then_shutdown_is_clean(qtbot, qapp, monkeypatch, tmp_path):
    """⑧：真件走完 ``assemble()`` 全链路 → 断言装配产物 → ``_shutdown()`` 无残留。

    只对两处打桩，理由都是「**不可在测试中执行的原生副作用**」：
    ``StatusItem``（真机菜单栏图标）与 ``HotkeyManager``（系统级全局热键）。
    其余全为真件：Config / EventBus / Heartbeat / Scheduler / Registry /
    HelperSession / PluginManager / ContributionRegistry / StatusPanel /
    SettingsWindow / DetailHost / 六个内置插件。

    数据路径隔离由 conftest 按构造保证（生产代码禁止按值导入路径常量），
    本用例末尾额外断言「所有声明路径都落在 tmp 内」，作为隔离失守的显式信号。
    """
    import barkit.shell.app_shell as app_shell_mod
    from barkit import paths
    from barkit.core.events import HELPER_OBSERVED_TOPIC
    from barkit.core.plugin import RUN_PERIODIC

    # 打桩点落**真身子模块**（形态 G：包属性只是导入时那份快照，打在包上＝进空气）：
    # StatusItem 在 ui 里构造、HotkeyManager 在 core 里构造。
    monkeypatch.setattr(app_shell_mod.ui, "StatusItem", _StubStatusItem)
    monkeypatch.setattr(app_shell_mod.core, "HotkeyManager", _StubHotkeys)
    _StubStatusItem.created.clear()

    shell = AppShell(qapp)
    shell.assemble()

    try:
        # ⓪ 隔离自证：装配链路读到的每个数据路径都必须在 tmp 内。这行若失败，
        #    说明 conftest 的模块属性重定向失效了（按值导入门禁应当已经先拦住）。
        assert str(tmp_path) in str(paths.APP_SUPPORT)
        assert str(tmp_path) in str(paths.HOSTS_ENTRIES_FILE)

        # ① 装配器持有全部运行时对象（类职责：组合根 = 对象持有者）
        core = (shell._config, shell._scheduler, shell._registry, shell._hotkeys,
                shell._heartbeat, shell._helper_session)
        assert all(o is not None for o in core), core
        assert shell._contributions is not None
        assert shell._manager is not None and shell._settings is not None
        assert shell.panel is not None and shell.status_item is not None
        assert shell._assembled is True

        # ② 插件被完整加载并实例化（六个内置插件）
        loaded = set(shell._registry.plugins)
        assert {"ipad_tunnel", "route_guard", "hosts_guard", "notify_guard",
                "autotype", "recognize"} <= loaded, loaded
        assert list(shell._registry.problems()) == [], shell._registry.problems()

        # ③ 贡献收集成立（状态行 / 组件 / 设置页 / 徽标规则都来自插件声明）
        assert shell._contributions.states(), "没有任何状态贡献，装配链路断了"
        assert shell._contributions.components()
        assert shell._contributions.settings_pages()

        # ④ daemon 插件已交给调度器（tick 由此驱动），且都声明了 poll 间隔
        daemon_ids = {
            pid for pid, pl in shell._registry.plugins.items() if pl.run_mode == RUN_PERIODIC
        }
        assert daemon_ids, "没有 daemon 插件被加载"
        assert daemon_ids <= set(shell._scheduler._plugins), shell._scheduler._plugins

        # ⑤ 状态订阅已建立（PluginManager 编排，app 只负责画）
        assert shell._manager._state_handlers, "状态订阅未建立"

        # ⑤b 卸载收尾的**时钟**与**回执**已接线（§9.1 第 4 步，2026-09-17）。
        #     漏了时钟 ⇒ `schedule()` 返回 False，退化成「当场删文件」——那正是
        #     「期望态写完就被删、helper 读到文件不存在而冻结」的老毛病；
        #     漏了回执 ⇒ 收尾没做成时界面毫无信号（只剩一句日志）。
        assert shell._manager._deferred_cleanup.hooked is True, "卸载收尾没接上框架心跳"
        assert shell._manager.on_uninstall_settled == shell._settings.notify_uninstall_settled

        # ⑥ 领域订阅已登记 = 插件声明生效（2026-09-29 起取代文件事件源）
        subscribed = shell._manager._subject_handlers
        assert "ipad_tunnel" in subscribed, subscribed
        for pid, pl in shell._registry.plugins.items():
            for declared in pl.subjects():
                # 声明的是**领域名**（route / tunnel / hosts），不再是文件路径
                assert "/" not in declared, f"{pid} 声明的不是领域名：{declared}"
        # _subject_handlers 以 plugin_id 为键（值 = (handler, 已声明领域集)）
        _, registered = subscribed["ipad_tunnel"]
        assert registered == frozenset({"tunnel"})
        assert shell._manager._events._handlers.get(HELPER_OBSERVED_TOPIC)

        # ⑦ 热键：插件声明的组合已收集（真机热键注册由 test_hotkeys 覆盖）
        assert shell._hotkeys.registered, "插件声明的热键一个都没注册"

        # ⑧ 徽标首屏已画过（至少一次；None = 正常无徽标）
        assert _StubStatusItem.created, "StatusItem 未被装配"
        assert _StubStatusItem.created[0].badges, "首屏徽标未绘制"
        assert _StubStatusItem.created[0].tooltip == "BarKit"

        # ⑨ 详情页托管器已绑定贡献注册中心
        assert shell._detail_host._registry is shell._contributions
    finally:
        shell._shutdown()

    # ⑩ 收尾无残留：顺序即依赖顺序（见 _shutdown 文档）
    assert shell._assembled is False
    assert shell._heartbeat.running is False, "心跳未停"
    assert shell._helper_session.connected is False, "会话收尾未关"
    assert shell._hotkeys.unregistered is True
    assert shell._scheduler._plugins == {} and shell._scheduler._tickers == {}
    assert shell._scheduler._dirty_timer.isActive() is False
    # 面板收尾：3s 失联定时器必须停下（此前 _shutdown 完全没碰 UI 层，
    # 定时器收尾后仍在跑，被 processEvents 调到就访问已死的面板内部 —— §5 第 16 条崩源）
    assert shell.panel._stale_timer.isActive() is False, "面板失联定时器收尾未停"
    assert shell.status_item.torn_down is True, "菜单栏图标收尾未调用 teardown"
    # ⑩b 面板在 styles.theme_changed 上的连接收尾必须断开：否则后续换肤回调会进
    #     已死的面板 widget（§5 第 16 条崩源的另一半）。teardown 已 disconnect，
    #     故收尾后再次 disconnect 应返回 False（PySide6：未连着才返 False）。
    #     这条专门防「teardown 被留下、但里面断开主题信号那行被误删」的静默回退。
    from barkit.shell import styles
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # 已断开后的二次 disconnect 会发 libpyside 警告，非缺陷
        assert styles.theme_changed.disconnect(shell.panel._on_theme_changed) is False, \
            "面板主题信号收尾未断开（泄漏）"
    # 线程池已 join（waitForDone）并丢弃 ⇒ 没有在飞的 tick 工作线程
    assert shell._scheduler._pools == {}, "线程池未释放"
    # `_running` 是主线程侧的簿记：worker 结束后经 queued 信号回填，故需事件循环
    # 排空一次才归零。这里断言「排空后必归零」——若真有 worker 泄漏或报告丢失，
    # waitUntil 会超时失败，而不是被静默放过。
    qtbot.waitUntil(lambda: shell._scheduler._running == set(), timeout=3000)

    # ⑪ 幂等：再次收尾（aboutToQuit 与手动收尾可能都触发）不得抛异常
    shell._shutdown()
    assert shell._assembled is False


def test_assemble_is_repeatable_on_fresh_instances(qtbot, qapp, monkeypatch):
    """装配器可重复构造（模拟「退出后再启动」）：第二实例不受上一实例影响。

    防的是组合根把状态写在**实例之外**（类属性 / 模块全局）而互相串味。
    """
    import barkit.shell.app_shell as app_shell_mod

    monkeypatch.setattr(app_shell_mod.ui, "StatusItem", _StubStatusItem)
    monkeypatch.setattr(app_shell_mod.core, "HotkeyManager", _StubHotkeys)

    for _ in range(2):
        shell = AppShell(qapp)
        shell.assemble()
        try:
            assert shell._assembled is True
            assert shell._registry.plugins
        finally:
            shell._shutdown()


def _quick_buttons(panel) -> list:
    """面板底部栏 A 区的**按钮**（按 widget 取，跳过末尾那个 stretch 与临时控件）。"""
    lay = panel._action_layout
    out = []
    for i in range(lay.count()):
        w = lay.itemAt(i).widget()
        if w is not None:
            out.append(w)
    return out


# ──────────────────────────────────────────────
# 显示位用户开关的接线（契约规范 §2.4，决议 D4）
# ──────────────────────────────────────────────


def test_display_slot_switches_are_wired_end_to_end(qtbot, qapp, monkeypatch):
    """显示位开关的四处接线 ＋ 托盘候选的实际效果（真件装配，配置落 tmp）。

    只断言「接线真的发生」与「关掉后托盘候选少一个」两件事；面板 / 设置窗各自的
    渲染与落盘细则由 ``test_panel`` / ``test_settings`` 覆盖。
    """
    import barkit.shell.app_shell as app_shell_mod

    monkeypatch.setattr(app_shell_mod.ui, "StatusItem", _StubStatusItem)
    monkeypatch.setattr(app_shell_mod.core, "HotkeyManager", _StubHotkeys)
    _StubStatusItem.created.clear()

    shell = AppShell(qapp)
    shell.assemble()
    try:
        # ① 面板注入的是配置的**方法**（用户改完当场生效，不必重新装配）
        assert shell.panel._ui_visibility == shell._config.plugin_ui_visible
        # ② 设置窗的改判回调接到装配器的重画入口（程序内写配置不发 changed）
        assert shell._settings._on_display_changed == shell._refresh_display_slots

        target = "notify_guard"
        assert any(c.plugin_id == target for c in shell._tray_candidates())
        topics_before = {s.topic for s in shell._contributions.states()}

        # ③ 关掉托盘显示位 ⇒ 该组件不再参与角标挑选
        shell._config.set_plugin_ui_visible(target, "tray", False)
        assert all(c.plugin_id != target for c in shell._tray_candidates())

        # ④ 隐藏 ≠ 停用：状态机仍在册、插件仍加载（只少了一处渲染）
        assert {s.topic for s in shell._contributions.states()} == topics_before
        assert target in shell._registry.plugins

        shell._config.set_plugin_ui_visible(target, "tray", True)
        assert any(c.plugin_id == target for c in shell._tray_candidates())
    finally:
        shell._shutdown()


def test_quick_area_is_empty_on_a_fresh_install(qtbot, qapp, monkeypatch):
    """**出厂启动时底部栏 A 区是空的** —— 快捷区域默认关（决议 D37）。

    用户 2026-09-22 的原始报单：启动后「快捷区域」摆了五个组件，只有前两枚渲染成图标、
    后三枚渲染成文字且整排超出面板。修法两条，这条守第一条（默认不摆）：
    声明了详情入口的组件在**行上**本来就点得进（D36），底部那排是可选加速度 ——
    默认全摆上既不必要、又把 224px 的 A 区撑爆。

    第二条（宽度裁决 / 形态整齐）由 ``test_panel`` 那组覆盖。
    """
    import barkit.shell.app_shell as app_shell_mod

    monkeypatch.setattr(app_shell_mod.ui, "StatusItem", _StubStatusItem)
    monkeypatch.setattr(app_shell_mod.core, "HotkeyManager", _StubHotkeys)
    _StubStatusItem.created.clear()

    shell = AppShell(qapp)
    shell.assemble()
    try:
        # 出厂态：A 区一枚都没有（标签态与图标态都不该有）
        assert shell.panel._primary_actions, "五枚快捷动作**已声明**（只是默认不显示）"
        assert _quick_buttons(shell.panel) == [], "默认就不该摆任何快捷按钮"

        # 用户自己挑一个打开 ⇒ 当场出现一枚，且只有一枚
        target = shell.panel._primary_actions[0].plugin_id
        shell._config.set_plugin_ui_visible(target, "action", True)
        shell._refresh_display_slots()
        shown = _quick_buttons(shell.panel)
        assert len(shown) == 1, "只打开一位，就只该多出一枚"
        label = shell.panel._primary_actions[0].label
        # 标签态给文字、图标态给 tooltip（§5.6）：两者必有一份认得出是哪个组件
        assert label in (shown[0].text(), shown[0].toolTip())

        # 关回默认 ⇒ 当场消失
        shell._config.set_plugin_ui_visible(target, "action", False)
        shell._refresh_display_slots()
        assert _quick_buttons(shell.panel) == []
    finally:
        shell._shutdown()


def test_app_shell_package_layout():
    """``shell/app_shell.py``（1168 行）2026-10-02 按职责拆成**包** + 五枚混合类。

    这几条判据分别守**三件不同的事**，缺一条就只看见一半（2026-10-02 门禁自证逐条敲过）：

    · 「旧单文件不许回来」—— 只断言这个会**恒真**：包在 ⇒ 单文件必然不在，而恒真的
      断言抓不到任何事。所以真正掐住的是下面那条「这一族点名」；
    · 「这一族点名」—— 按**子模块集合**点名，不按文件名。写「``app_shell.py`` 不许存在」
      这种收口证明是恒真的（正文搬走后入口本来就没有它），删掉某个**子模块**就一声不响；
    · 「形态 H 退场：core 段不许出现 owner」—— ``_apply_log_level`` / ``_wire_context`` /
      ``_build_notify_hub`` 三处调用与它们的方法体**同在 ``_CoreMixin`` 里**，原先那句
      ``AppShell.`` 前缀只是「单文件时代的类内全名写法」，拆包时原样搬过来就凭空多出一条
      ``.core → .app_shell`` 反向引用，逼出循环导入（owner 必须在定义类那一刻拿到 mixin
      ⇒ 互引不管 import 先后都炸），于是当初只能拿「函数体内延迟导入」去**打破**它。
      更优解是**消除**依赖：去前缀 ⇒ 依赖单向、真 DAG、连延迟导入都不需要。
      故这条分两半各守一半 —— 禁止 owner import、禁止退回 ``AppShell.`` 前缀。
    """
    pkg_dir = Path(barkit.shell.app_shell.__file__).parent
    assert pkg_dir.name == "app_shell" and (pkg_dir / "__init__.py").is_file()
    assert not pkg_dir.with_suffix(".py").is_file(), "旧的单文件 app_shell.py 又回来了"

    family = {"consts", "core", "hotkeys", "feedback", "ui", "tray", "app_shell"}
    have = {p.stem for p in pkg_dir.glob("*.py")}
    assert family <= have, f"app_shell 子模块缺了：{sorted(family - have)}"

    core = pkg_dir / "core.py"
    body = core.read_text(encoding="utf-8")
    top_scoped = [
        n.module for n in ast.parse(body).body
        if isinstance(n, ast.ImportFrom) and n.module == "app_shell"
    ]
    assert top_scoped == [], (
        f"core.py 在**模块作用域** import 了 owner（{top_scoped}）："
        ".core 与 .app_shell 互引是循环导入，AppShell 必须在定义类那一刻拿到 mixin ⇒ "
        "只许保留函数体内的延迟导入"
    )
    assert body.count("from .app_shell import AppShell") == 0, (
        "core.py 引用了 owner：三处 owner 调用（``_apply_log_level`` / ``_wire_context`` / "
        "``_build_notify_hub``）与调用点同在 ``_CoreMixin``，写裸名即可，"
        "不必也不该引入 import；一旦 import 就又回到循环导入那条路上"
    )
    assert "AppShell." not in body, (
        "core.py 里又出现 ``AppShell.`` 前缀：那三处方法就定义在这个类里，"
        "写全名会把 core 段重新钉到 owner 上（循环导入，或得再补延迟导入）—— "
        "两种都是这条门禁要挡的形态 H"
    )
