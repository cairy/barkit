"""设置窗 rebuild_pages 单测（Qt，可离屏；用 qtbot 提供 QApplication）。

验证插件启用/禁用/卸载后，设置窗能即时重建分页以反映插件集合变化，且保留当前所在页。
"""

from __future__ import annotations

import logging
import yaml

import pytest

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from barkit.contribution.specs import (
    TRAY_MIN_LEVEL_DEFAULT,
    TRAY_MIN_LEVELS,
    ComponentSpec,
    IconName,
    SettingsPageSpec,
    StateSpec,
)
from barkit.core.plugin import RUN_ON_DEMAND, RUN_PERIODIC
from barkit.core.plugin_manager import InstallPreview, PluginInfo
from barkit.shell import settings
from barkit.shell.autostart_row import AutostartRow
from barkit.shell.design import metrics
from barkit.shell.settings import SettingsWindow
# ``shell/settings`` 拆成包后（2026-10-01），**打桩必须打在真定义模块上**：
# `settings.QFileDialog` 这类名字只是包 `__init__` 里的再导出，改它打不到子模块里那份
# 绑定副本上 —— 补丁静默失效、用例红在一个不相干的地方。真身见下面两个别名。
from barkit.shell.settings import maintenance as settings_maintenance
from barkit.shell.settings import plugin_actions as settings_plugin_actions
from barkit.shell.ui_kit import (
    ChoiceSelector,
    FeedbackBar,
    Switch,
    _SectionHeader,
    settings_page_shell,
)
from barkit.core.config import Config
from barkit.core import logging_setup
from barkit.core.resources import SystemMutation


class _FakeContrib:
    """贡献注册中心轻量桩：只实现设置窗真正消费的门面。

    ``tray_base_candidates()`` / ``components()`` 是「通用」页菜单栏主图标选择器的
    数据源（P0③）——真实 ``ContributionRegistry`` 都有，桩也必须给，否则测的是
    一个不存在的契约。``tray_base_candidates`` 按真实判据（有主图标 = 进候选）
    镜像实现，不另写一套口径。

    ``primary_actions()`` / ``detail_entries()`` 是「插件」页**「快捷区域」芯片**的
    数据源（契约 §2.4 第四位）—— 同样必须给，否则「关了开关却没东西可关」这类
    问题在桩里被静默掩盖。

    ``active_ids`` 是「这位组件现在活不活跃」的判据（2026-09-22 起设置窗用它决定
    「活跃但没自声明设置页的组件要不要由框架出页」）—— 同样必须给，理由见下面那条属性。
    """

    def __init__(self, pages, states=(), components=(), rows=(), actions=(),
                 entries=(), active=(), hotkeys=()):
        self._pages = list(pages)
        self._states = list(states)
        self._components = list(components)
        self._rows = list(rows)
        self._actions = list(actions)
        self._entries = list(entries)
        self._active = set(active)
        self._hotkeys = list(hotkeys)

    @property
    def active_ids(self):
        """当前处于激活（贡献可见）状态的插件 id —— 与真实注册中心同一门面。

        设置窗 2026-09-22 起要拿它判「这位组件现在活不活跃」（活跃但没自声明设置页的
        组件由框架出页）。**桩缺了这个门面，那条准入会静默退回旧行为**，新判据看起来
        全绿其实一条没覆盖 —— 这正是本仓反复吃过的「测了一个不存在的契约」。

        缺省真源＝本桩收集到的所有声明里的 ``plugin_id``：桩的语义就是「这些贡献可见」。
        """
        return self._active | {
            spec.plugin_id
            for group in (self._pages, self._states, self._components,
                          self._rows, self._actions, self._entries)
            for spec in group
        }

    def settings_pages(self):
        return self._pages

    def states(self):
        return self._states

    def status_rows(self):
        return self._rows

    def primary_actions(self):
        return self._actions

    def detail_entries(self):
        return self._entries

    def tray_base_candidates(self):
        return [s for s in self._states if s.icon is not None]

    def components(self):
        return self._components

    def hotkeys(self):
        """快捷键页的数据源（2026-09-23 起设置窗消费它）。

        与 ``components()`` 同理：真实注册中心有这一门面，桩不给 ⇒ 「快捷键」页在
        测试里是**空着也能绿**的，等于没测。
        """
        return self._hotkeys


class _FakeConfig:
    """配置轻量桩：只覆盖设置窗用到的那几个读写（写盘路径另有 test_config 覆盖）。"""

    def __init__(self, source="", min_level=None):
        self.source = source
        self.writes: list[str] = []
        self.min_level = min_level or TRAY_MIN_LEVEL_DEFAULT
        self.level_writes: list[str] = []
        self._level = ""                       # 日志级别：空串 = 跟随启动环境
        self.visible: dict[tuple[str, str], bool] = {}
        self.slot_writes: list[tuple[str, str, bool]] = []
        # 「快捷键」页读写 hotkeys.<名字>（真实 Config 的 get / set_hotkey / remove_hotkeys）
        self.values: dict[str, object] = {}
        self.hotkey_writes: list[tuple[str, str]] = []
        self.hotkey_removes: list[str] = []
        # 「变量」页读写的 variables 段（真实 Config 的 variables / set_variables）
        self._variables: list = []
        self.variable_writes: list[list] = []

    def tray_base_source(self):
        return self.source

    # ---- 快捷键页用的那几个口（与真实 Config 同签名）----

    def get(self, path, default=None):
        return self.values.get(path, default)

    def set_hotkey(self, name, combo):
        self.hotkey_writes.append((name, combo))
        self.values[f"hotkeys.{name}"] = combo

    def remove_hotkeys(self, names):
        for name in names:
            self.hotkey_removes.append(name)
            self.values.pop(f"hotkeys.{name}", None)

    def custom_hotkeys(self):
        """自建条目（本桩不造自建条目；那段由 test_hotkeys_page 覆盖）。"""
        return {}

    # ---- 「变量」页用的那两个口（与真实 Config 同签名）----

    def variables(self):
        """变量**声明**（真实 Config 读的是合并后的 ``variables`` 段）。"""
        return list(self._variables)

    def set_variables(self, declarations):
        """整体写声明（真实 Config 走 save_user；这里只记账，好让用例断言写了什么）。"""
        self._variables = [dict(item) for item in declarations]
        self.variable_writes.append(list(self._variables))

    def hotkey_feedback(self, name, default="none"):
        return default

    def set_tray_base_source(self, value):
        self.writes.append(value)
        self.source = value

    def tray_min_level(self):
        return self.min_level

    def set_tray_min_level(self, value):
        self.level_writes.append(value)
        self.min_level = value

    def log_level(self):
        return self._level

    def set_log_level(self, value):
        self.level_writes.append(value)
        self._level = value

    def plugin_ui_visible(self, plugin_id, slot):
        # 缺省取**该位的默认**（与 ``Config.plugin_ui_visible`` 同一份判据）：默认逐位
        # 不同（快捷区域默认关，D37），桩若一律填 ``True`` 就会画出与实际相反亮着的芯片。
        from barkit.core.config import slot_default_visible

        return self.visible.get((plugin_id, slot), slot_default_visible(slot))

    def set_plugin_ui_visible(self, plugin_id, slot, visible):
        self.slot_writes.append((plugin_id, slot, visible))
        self.visible[(plugin_id, slot)] = visible


class _FakeManager:
    """最小桩：本文件多数用例只需要「一个组件都没有」。

    ``mutations`` 是「恢复系统状态」那张卡的数据源（非空 = 有组件留下了东西），
    ``revert_ok=False`` 用来造「撤到一半」那一档。
    """

    def __init__(self, mutations=None, revert_ok=True):
        self.mutations = list(mutations or [])
        self.revert_ok = revert_ok
        self.reverted: list = []

    def list_plugins(self):
        return []

    def declared_system_mutations(self):
        return list(self.mutations)

    def revert_system_state(self, plugin_id):
        self.reverted.append(plugin_id)
        return self.revert_ok


class _OrderManager:
    """排序相关最小桩：``list_plugins`` / ``effective_order`` / ``reorder_plugin``。

    ``poll_defaults`` / ``polls`` 供「刷新间隔」那组用例用：给了默认值的 id 就是
    **周期型**组件（``poll_default`` 非空 = 框架会给它画控件），没给的是按需型。
    """

    def __init__(self, order, *, poll_defaults=None, polls=None):
        self._order = list(order)
        self.reorders: list[tuple[str, int]] = []
        self._poll_defaults = dict(poll_defaults or {})
        self._polls = dict(polls or {})
        self.poll_writes: list[tuple[str, object]] = []

    def list_plugins(self):
        return [
            PluginInfo(
                id=pid, name=pid.upper(), source="internal",
                run_mode=(
                    RUN_PERIODIC if pid in self._poll_defaults else RUN_ON_DEMAND
                ),
                enabled=True, loaded=True,
                poll_default=self._poll_defaults.get(pid),
            )
            for pid in self._order
        ]

    def plugin_poll(self, pid):
        return self._polls.get(pid)

    def set_plugin_poll(self, pid, seconds):
        self.poll_writes.append((pid, seconds))
        if seconds is None:
            self._polls.pop(pid, None)
        else:
            self._polls[pid] = float(seconds)
        return True

    def effective_order(self):
        return list(self._order)

    def reorder_plugin(self, pid, to_index):
        others = [p for p in self._order if p != pid]
        to_index = max(0, min(to_index, len(others)))
        new = others[:to_index] + [pid] + others[to_index:]
        if new == self._order:
            return False
        self._order = new
        self.reorders.append((pid, to_index))
        return True


def _page_factory(label, plugin_id="fake"):
    def build():
        return QWidget()
    return SettingsPageSpec(plugin_id=plugin_id, label=label, build=build)


def test_rebuild_pages_reflects_plugin_pages_and_preserves_current(qtbot):
    contrib = _FakeContrib([])
    mgr = _FakeManager()
    win = SettingsWindow(parent=None, registry=contrib, manager=mgr)
    qtbot.add_widget(win)

    # 基准分页（无插件贡献页）：通用 + 快捷键 + 变量 + 插件 + 维护 + 关于
    # （「快捷键」是 2026-09-23 加的框架固定页、「变量」是 2026-10-10 加的，都是跨组件的
    # 框架级页 —— 不再只此四页）
    base = win._nav.count()
    assert base == 6

    # 模拟新插件贡献一页 → rebuild 后应多一页
    contrib._pages = [_page_factory("路由守卫")]
    win.rebuild_pages()
    assert win._nav.count() == base + 1
    labels = [win._nav.item(i).text() for i in range(win._nav.count())]
    assert "路由守卫" in labels

    # 保留当前页：切到「关于」再 rebuild，索引应仍指向「关于」
    about_idx = labels.index("关于")
    win._nav.setCurrentRow(about_idx)
    win._stack.setCurrentIndex(about_idx)
    contrib._pages = [_page_factory("路由守卫"), _page_factory("通知守卫")]
    win.rebuild_pages()
    assert win._nav.currentRow() == win._nav.count() - 1  # 关于仍在末位
    assert win._stack.currentIndex() == win._nav.currentRow()


def test_rebuild_pages_unblocks_nav_even_if_a_builder_raises(qtbot):
    """插件页的 builder 抛异常后，侧栏必须照旧能切页。

    ``rebuild_pages`` 用 ``blockSignals`` 压住切页重建，没有 ``finally`` 时异常会把
    block 永久留在 nav 上 —— 症状很糟：设置窗一切正常，只是**点侧栏再也不切页**。
    判据写在信号是否放行上（``signalsBlocked``），不依赖切页后的栈下标。
    """
    contrib = _FakeContrib([])
    mgr = _FakeManager()
    win = SettingsWindow(parent=None, registry=contrib, manager=mgr)
    qtbot.add_widget(win)

    def _boom():
        raise RuntimeError("插件页自己炸了")

    # 构造之后坏页才进来：这样才能走到「重建半途抛异常」那条路
    contrib._pages = [SettingsPageSpec(plugin_id="fake", label="坏页", build=_boom)]
    with pytest.raises(RuntimeError):
        win.rebuild_pages()
    assert win._nav.signalsBlocked() is False, "重建半途失败 ⇒ 侧栏永久失联"

    # 坏页拿下之后，侧栏立刻恢复工作（证明放行的是 nav 本身，不是碰巧）
    contrib._pages = []
    win.rebuild_pages()
    win._nav.setCurrentRow(1)
    assert win._stack.currentIndex() == 1


class _PluginWithSchema:
    """带 ``config_schema()`` 的假组件（侧栏搜索的**唯一**检索面）。"""

    def __init__(self, pid, *labels):
        self._pid = pid
        self._labels = labels

    def config_schema(self):
        from barkit.core.schema import FieldSpec, FormSchema

        return FormSchema(
            plugin_id=self._pid,
            fields=tuple(FieldSpec(key=f"k{i}", label=lab)
                         for i, lab in enumerate(self._labels)),
        )


def _search_window(qtbot, pages, instances):
    """设置窗 ＋ 两页组件页 ＋ 各自的 schema；返回 (win, 页标签顺序)。"""
    class _Mgr(_OrderManager):
        def plugin_instance(self, pid):
            return instances.get(pid)

    contrib = _FakeContrib(pages)
    win = SettingsWindow(parent=None, registry=contrib, manager=_Mgr(["a", "b"]))
    qtbot.add_widget(win)
    return win


def test_sidebar_search_matches_page_labels(qtbot):
    """搜页面名 ⇒ 只留命中的那几页；行号与内容栈下标**不动**（靠隐藏，不靠重建）。"""
    win = _search_window(
        qtbot,
        [_page_factory("路由守卫", "a"), _page_factory("通知守卫", "b")],
        {"a": _PluginWithSchema("a"), "b": _PluginWithSchema("b")},
    )
    total = win._nav.count()
    win._search.setText("路由")
    hidden = [i for i in range(total) if win._nav.isRowHidden(i)]
    assert len(hidden) == total - 1
    assert not win._nav.isRowHidden(
        [win._nav.item(i).text() for i in range(total)].index("路由守卫")
    )


def test_sidebar_search_matches_config_field_labels(qtbot):
    """搜**字段标签**也能找到页 —— 用户要改的是「运行间隔」，不是「某组件页」。

    这是可发现性的主要价值：页面名里没有那几个字，但那一页里确实有这一项。
    """
    win = _search_window(
        qtbot,
        [_page_factory("路由守卫", "a"), _page_factory("通知守卫", "b")],
        {"a": _PluginWithSchema("a", "运行间隔"), "b": _PluginWithSchema("b", "轮询秒数")},
    )
    labels = [win._nav.item(i).text() for i in range(win._nav.count())]
    win._search.setText("运行间隔")
    visible = [labels[i] for i in range(len(labels)) if not win._nav.isRowHidden(i)]
    assert visible == ["路由守卫"]
    assert "运行间隔" in win._nav.item(labels.index("路由守卫")).toolTip()


def test_sidebar_search_empty_state_and_restore(qtbot):
    """一个都不中 ⇒ 出一句空态（不是空列表）；清空 ⇒ 全部回来。"""
    win = _search_window(qtbot, [_page_factory("路由守卫")], {})
    win._search.setText("不存在的设置")
    assert win._no_match.isVisibleTo(win) is True
    win._search.setText("")
    assert win._no_match.isVisibleTo(win) is False
    assert all(not win._nav.isRowHidden(i) for i in range(win._nav.count()))


def test_sidebar_search_moves_selection_off_a_hidden_row(qtbot):
    """当前页被过滤掉 ⇒ 落到第一个看得见的（否则内容区显示的是别的页）。"""
    win = _search_window(
        qtbot,
        [_page_factory("路由守卫", "a"), _page_factory("通知守卫", "b")],
        {"a": _PluginWithSchema("a"), "b": _PluginWithSchema("b")},
    )
    labels = [win._nav.item(i).text() for i in range(win._nav.count())]
    win._nav.setCurrentRow(labels.index("关于"))
    win._search.setText("路由")
    assert win._nav.item(win._nav.currentRow()).text() == "路由守卫"
    assert win._stack.currentIndex() == win._nav.currentRow()


def test_rebuild_pages_removes_disabled_plugin_page(qtbot):
    contrib = _FakeContrib([_page_factory("路由守卫")])
    mgr = _FakeManager()
    win = SettingsWindow(parent=None, registry=contrib, manager=mgr)
    qtbot.add_widget(win)
    assert win._nav.count() == 7  # 6 基准（含「快捷键」「变量」）+ 1 插件页

    # 模拟插件被禁用（贡献页消失）→ rebuild 后回到基准 6 页
    contrib._pages = []
    win.rebuild_pages()
    assert win._nav.count() == 6
    labels = [win._nav.item(i).text() for i in range(win._nav.count())]
    assert "路由守卫" not in labels


# ---------- 「插件」分页：排序编辑器接线（§5.5 / §6.4） ----------


def test_maintenance_log_card_round_trip_and_live_level(qtbot, tmp_path):
    """维护页「日志」卡：选档位即时落盘 + 实时改 root 级别；选回跟随环境删键回落。

    这条卡的是「设置窗里改级别」这条闭环——落盘经 Config、即时生效经
    ``logging_setup.set_level``（老实现只接环境变量，常驻实例改不了级别）。
    """
    user = tmp_path / "user.yaml"
    user.write_text("", encoding="utf-8")
    cfg = Config(user_path=user)
    win = SettingsWindow(
        parent=None, registry=_FakeContrib([]), manager=_FakeManager(), config=cfg
    )
    qtbot.add_widget(win)
    sel = win._log_level_selector
    root = logging.getLogger()
    orig_level = root.level
    orig_handlers = list(root.handlers)
    try:
        sel.selected.emit("debug")
        assert cfg.log_level() == "debug"
        assert root.level == logging.DEBUG

        sel.selected.emit("")
        assert cfg.log_level() == ""
        raw = yaml.safe_load(user.read_text(encoding="utf-8")) or {}
        assert "log" not in raw
        assert root.level == logging.INFO           # resolve_level(None) -> 出厂档
    finally:
        root.setLevel(orig_level)
        for h in list(root.handlers):
            if h not in orig_handlers:
                root.removeHandler(h)
                h.close()
        logging_setup._auto_console = None           # 别把自动加的 handler 漏给别的文件


def test_log_card_has_one_button_per_category_plus_the_folder(qtbot, tmp_path):
    """「日志」卡：分类表有几项就画几枚按钮，另加一枚「打开日志目录」。

    用户 2026-10-13 的要求原话是「有几个分类就加几个按钮，另外再加一个打开目录的
    按钮」。判据从 ``log_sources()`` 反查而不是写死标签 —— 分类表加一项而卡里漏画，
    这条当场红；卡里多画一枚没有落点的按钮也红（``count == 1``）。
    """
    from barkit.shell.log_sources import log_sources

    user = tmp_path / "user.yaml"
    user.write_text("", encoding="utf-8")
    win = SettingsWindow(
        parent=None, registry=_FakeContrib([]), manager=_FakeManager(),
        config=Config(user_path=user),
    )
    qtbot.add_widget(win)

    labels = [b.text() for b in win.findChildren(QPushButton)]
    for src in log_sources():
        assert labels.count(src.label) == 1, "「%s」那一枚没画 / 画重了" % src.label
    assert labels.count("打开日志目录") == 1


def test_log_card_buttons_reveal_their_own_target(qtbot, tmp_path, monkeypatch):
    """每枚按钮揭示的是**自己那一份**的落点：点「守护日志」不能开到应用日志那棵树。

    五枚长得一模一样（同一种 ``pill-ghost``），接线错一格在界面上看不出来 ——
    点下去开到别的目录，用户以为那份日志是空的。
    """
    from barkit import paths
    from barkit.shell import data_ops
    from barkit.shell import log_sources as log_sources_mod

    # 崩溃报告那条默认去 glob 真实 home；这里指到 tmp，免得用例读到本机的东西。
    monkeypatch.setattr(log_sources_mod, "CRASH_REPORTS_DIR", tmp_path / "crashes")
    seen: "list[tuple[str, object]]" = []
    monkeypatch.setattr(
        data_ops, "reveal",
        lambda target, tell, what: seen.append((what, target())),
    )

    user = tmp_path / "user.yaml"
    user.write_text("", encoding="utf-8")
    win = SettingsWindow(
        parent=None, registry=_FakeContrib([]), manager=_FakeManager(),
        config=Config(user_path=user),
    )
    qtbot.add_widget(win)

    by_label = {b.text(): b for b in win.findChildren(QPushButton)}
    cases = [(src.label, src.label, src.target())
             for src in log_sources_mod.log_sources()]
    cases.append(("打开日志目录", "日志目录", paths.LOG_DIR))
    for btn_label, _what, _path in cases:
        by_label[btn_label].click()

    got = dict(seen)                        # what -> 路径（五个 what 互不相同）
    assert set(got) == {what for _b, what, _p in cases}, "有按钮没接上 / 接重了"
    for btn_label, what, path in cases:
        assert got[what] == path, "「%s」揭错了东西" % btn_label


def test_plugin_page_uses_order_editor_with_handles(qtbot):
    contrib = _FakeContrib([])
    mgr = _OrderManager(["route_guard", "hosts_guard", "notify_guard"])
    win = SettingsWindow(parent=None, registry=contrib, manager=mgr)
    qtbot.add_widget(win)

    editor = win._order_editor
    assert editor.order == mgr.effective_order()          # 行序 = 生效顺序（三处同源）
    assert set(editor._handles) == set(mgr.effective_order())
    for pid, handle in editor._handles.items():
        assert handle.parent() is editor._rows[pid]       # 手柄在行内（可拖）
        assert pid in win._plugin_infos                   # 行内容按 id 对齐


def test_plugin_page_reorder_persists_to_manager(qtbot):
    contrib = _FakeContrib([])
    mgr = _OrderManager(["a", "b", "c"])
    win = SettingsWindow(parent=None, registry=contrib, manager=mgr)
    qtbot.add_widget(win)

    editor = win._order_editor
    editor._handles["c"].nudgeRequested.emit(-1)          # Alt+↑ 的等价信号
    assert mgr.reorders == [("c", 1)]
    assert editor.order == ["a", "c", "b"]
    assert mgr.effective_order() == ["a", "c", "b"]


def test_plugin_page_order_falls_back_without_effective_order(qtbot):
    """轻量 manager 桩（仅 list_plugins）不得让「插件」分页构建失败。"""
    contrib = _FakeContrib([])
    win = SettingsWindow(parent=None, registry=contrib, manager=_FakeManager())
    qtbot.add_widget(win)
    assert win._order_editor.order == []


def test_plugin_page_scrolls_when_rows_overflow(qtbot):
    """窗口装不下 8 行（实测每行 54px）→ 列表必须可滚动。

    否则被裁掉的行连行首手柄一起不可达，排序功能对末几行失效
    （用户真实配置已有 6~7 个插件，属常态而非极端）。

    **2026-09-21 起要自己把窗拖小**：设置窗不再定死 560×380，而是可缩放
    （``DEFAULT_SIZE`` 640×520 / ``MIN_SIZE`` 520×360）。默认尺寸下 8 行装得下，
    「溢出」这个前提就不再自动成立 —— 而**用户把窗拖到最小**（或屏幕本身矮）时
    这条滚动路径照样是必需的，故这里显式给一个装不下的高度。
    """
    from PySide6.QtWidgets import QScrollArea

    contrib = _FakeContrib([])
    mgr = _OrderManager([f"p{i}" for i in range(8)])
    win = SettingsWindow(parent=None, registry=contrib, manager=mgr)
    qtbot.add_widget(win)
    win.resize(560, 380)          # 拖到装不下的高度（≥ MIN_SIZE 高度 360）
    win.show()
    labels = [win._nav.item(i).text() for i in range(win._nav.count())]
    win._nav.setCurrentRow(labels.index("插件"))  # 切到该页，布局才有真实可视区尺寸
    qtbot.wait(20)

    editor = win._order_editor
    area = win._stack.widget(labels.index("插件"))   # 该页由外层 _scrolled 统一包滚动区
    assert isinstance(area, QScrollArea)
    viewport = area.viewport()
    assert editor.height() > viewport.height()           # 内容确实超出可视区

    bar = area.verticalScrollBar()
    assert bar.maximum() > 0
    bar.setValue(bar.maximum())
    qtbot.wait(10)
    last = editor._rows[editor.order[-1]]
    bottom = last.mapTo(viewport, QPoint(0, 0)).y() + last.height()
    assert bottom <= viewport.height()                    # 滚到底后末行完整可见


# ---------- 窗口尺寸（2026-09-21：可缩放） ----------


def test_settings_window_is_resizable(qtbot):
    """设置窗**可由用户拖动缩放**（2026-09-21 用户要求：「有些插件设置项特别多」）。

    原先 ``setFixedSize(560, 380)`` 把整窗钉死：长页（「网络切换」近 2900px）
    只能靠滚动条爬，而滚动这条退路能成立的前提是「内容溢出」—— 用户想把窗拖大
    一次看完整一组，就该允许。三条判据：
    """
    contrib = _FakeContrib([])
    win = SettingsWindow(parent=None, registry=contrib, manager=_FakeManager())
    qtbot.add_widget(win)

    def_w, def_h = SettingsWindow.DEFAULT_SIZE
    min_w, min_h = SettingsWindow.MIN_SIZE
    assert (def_w, def_h) == (640, 520), "初开尺寸是量出来的（看一眼能读完整一组）"
    assert (min_w, min_h) == (520, 360), "下限再小就与固定 150px 的侧栏互相压"
    assert def_w >= min_w and def_h >= min_h

    assert (win.minimumWidth(), win.minimumHeight()) == (min_w, min_h)
    assert win.maximumWidth() > min_w, "宽度被钉死 = 不可缩放"
    assert win.maximumHeight() > min_h, "高度被钉死 = 不可缩放"

    win.resize(def_w + 140, def_h + 140)
    win.show()
    qtbot.wait(10)
    assert (win.width(), win.height()) == (def_w + 140, def_h + 140), "拖动后应真的变大"
    win.close()


# ---------- 插件设置页页首的「状态机」段：取数口注入后必须当场生效 ----------


def test_state_section_current_value_follows_late_provider(qtbot):
    """取数口是**建页之后**注入的 ⇒ 状态机段的「当前」一栏必须跟着变。

    守 2026-09-21 用户上报的「当前 —」：装配器当初把 ``self._state_provider`` 的
    **当时值**（建页那一刻还是 ``None``）传给段，于是此后 ``set_state_provider``
    怎么注都进不去，刷新刷的是一段「没有取数口」的段 —— 界面永远停在占位文案。

    故三条一起断：① 没注入时是占位文案（不是破折号）；② 注入后**当场**刷出取值文案；
    ③ 失联注入后走框架失联话术（同一处刷新的另一半）。
    """
    from barkit.contribution.specs import StateLevel, StateOption, StateReport
    from barkit.shell.state_machines_section import StateMachinesSection

    spec = StateSpec(
        plugin_id="fake", key="notify", title="通知",
        states=[
            StateOption(id="allowed", level=StateLevel.OK, text="正常"),
            StateOption(id="muted", level=StateLevel.WARN, text="已静音"),
        ],
    )
    contrib = _FakeContrib([_page_factory("假组件")], states=[spec])
    win = SettingsWindow(parent=None, registry=contrib, manager=_FakeManager())
    qtbot.add_widget(win)

    labels = [win._nav.item(i).text() for i in range(win._nav.count())]
    page = win._stack.widget(labels.index("假组件"))
    section = page.findChild(StateMachinesSection)
    assert section is not None, "声明了状态机的组件，其设置页应有状态机段"

    def filled() -> list[str]:
        """当前**亮着**的那几行取值（实心圆圈）——「当前」今天画在取值行上。"""
        return [
            opt_id
            for dot, opt_id in section._option_dots["notify"]
            if dot.text() == "●"
        ]

    def tip() -> str:
        return section._titles["notify"].toolTip()

    assert filled() == [], "取数口未注入 ⇒ 一行都不亮（不是拿上一次读数冒充当前）"
    assert tip() == "暂无报告", "取数口未注入 ⇒ 占位话术（先前是个破折号）"

    win.set_state_provider(lambda topic: StateReport(state="muted"))
    assert filled() == ["muted"], "注入取数口后当场把那一行取值点成实心"
    assert tip() == "已静音（警告）", "话术挂标题悬停（同一句文案，不在段里说两遍）"

    win.set_state_stale(lambda topic: True)
    assert filled() == [], "失联 ⇒ 不点亮任何取值（拿上一次读数当当前是撒谎）"
    assert tip() == "状态失联（异常）", "失联走框架话术"


def test_state_section_sits_in_the_slot_below_page_title(qtbot):
    """状态机段落在**标题以下**那个插槽，不是抢在标题前、也不是掉到页尾。

    守 2026-09-21 用户上报的两头：早先框架在插件页**外面**再包一层盒子把段塞在最前，
    读起来是「先来一段不知道属于谁的卡，然后才是这个组件的名字」，而且外壳边距被
    套了两遍。
    """
    from barkit.contribution.specs import StateLevel, StateOption
    from barkit.shell.state_machines_section import StateMachinesSection

    spec = StateSpec(
        plugin_id="fake", key="notify", title="通知",
        states=[StateOption(id="muted", level=StateLevel.WARN, text="已静音")],
    )
    contrib = _FakeContrib([_page_factory("假组件")], states=[spec])
    win = SettingsWindow(parent=None, registry=contrib, manager=_FakeManager())
    qtbot.add_widget(win)

    labels = [win._nav.item(i).text() for i in range(win._nav.count())]
    page = _scrolled_widget(win._stack.widget(labels.index("假组件")))
    lay = page.layout()
    titles = [
        lay.itemAt(i).widget()
        for i in range(lay.count())
        if isinstance(lay.itemAt(i).widget(), QLabel)
    ]
    assert titles and titles[0].text() == "假组件", "第 0 项是页面标题"
    assert isinstance(
        lay.itemAt(1).widget(), StateMachinesSection
    ), "标题下面那一格（PAGE_SLOT）才是框架自动段"


def test_state_section_handle_tooltip_says_which_end_wins(qtbot):
    """拖动手柄的悬停必须说清**该把想优先显示的那台拖到哪一头**。

    只写「拖拽调整顺序」不够：用户不知道「前面优先」还是「后面优先」，于是不知道往
    哪拖（2026-09-21 用户上报）。定案＝**越靠前越优先**（与代表态判据同源：priority
    数值小者胜，而列表越靠前数值越小 —— 最后那半句在这里**实测**钉住，不只是文案）。

    还得写「顺序不是万能的」：级别更严重的那台**无条件**优先，顺序只在级别相同时
    起作用（``test_representative_priority_never_outranks_severity`` 守的那条）。
    只写「越靠前越优先」会让人以为顺序能盖过级别。
    """
    from barkit.contribution.specs import StateLevel, StateOption
    from barkit.shell.order_editor import _DragHandle
    from barkit.shell.state_machines_section import StateMachinesSection

    def machine(key: str, level: StateLevel) -> StateSpec:
        return StateSpec(
            plugin_id="fake", key=key, title=key,
            states=[StateOption(id="on", level=level, text="开")],
        )

    written: dict[str, int | None] = {}
    sec = StateMachinesSection(
        specs=[machine("a", StateLevel.OK), machine("b", StateLevel.WARN)],
        get_priorities=lambda: {k: v for k, v in written.items() if v is not None},
        on_priority=lambda k, v: written.__setitem__(k, v),
    )
    qtbot.add_widget(sec)

    handle = sec.findChild(_DragHandle)
    assert handle is not None, "接了写回调 ⇒ 手柄在场（没接就不该出，防止拖了弹回原位）"
    tip = handle.toolTip()
    assert "拖拽调整顺序" in tip, "操作话术不丢（归控件自己）"
    assert "越靠前越优先" in tip, f"得说清方向，不能只说能拖：{tip!r}"
    assert "级别相同时" in tip, f"得说清顺序只在同级时起作用：{tip!r}"

    # 文案说的方向必须就是代码的方向：拖到最前 ⇒ 拿到的优先级数值最小
    sec._editor.reorder("b", 0)
    assert written.get("b") == 0, "「越靠前越优先」＝ 排前面的那台数值更小"

    # 通用件不认识「状态机」：不给 hint 就只有操作话术（既有消费方零行为变更）
    plain = _DragHandle(0)
    assert plain.toolTip() == "拖拽调整顺序（聚焦后 Alt+↑ / Alt+↓ 微调）"


def test_settings_section_header_toggles_the_card(qtbot):
    """分区卡**标题栏可点折叠**：点一下内容收起，再点回来。

    标题栏自己带底色（那一格是 ``_SectionHeader``），底色本身就是分组的边界，
    故卡内不再有那条分隔线 —— 一眼能定位到组靠的是块，不是一条 1px 的线。
    """
    from barkit.shell.ui_kit import _SectionHeader, settings_section

    card, content = settings_section("一组设置")
    qtbot.add_widget(card)
    head = card.findChild(_SectionHeader)
    assert head is not None, "有标题 ⇒ 有一条带底色的标题栏"

    # 看 ``isHidden`` 而不是 ``isVisible``：后者要整条祖先链都可见（这里没 show 窗口），
    # 而折叠表达的是「这一组要不要画」—— 与祖先无关。
    body = content.parentWidget()
    assert not body.isHidden()
    _click(head)
    assert body.isHidden(), "点标题栏 ⇒ 整组收起"
    assert head.expanded is False
    assert _chevron(head) == "▸"
    _click(head)
    assert not body.isHidden(), "再点 ⇒ 展开回来"
    assert _chevron(head) == "▾"


def _collapsible_host(qtbot, *, stretch: int = 1):
    """搭一个与设置页同构的小页面：一行说明 ＋ 一张（可铺满的）分区卡。

    判「收起卡会不会把界面搞乱」**必须有同列的邻居** —— 用户报的那个坏法就是邻居
    把卡让出的余高吃走。只有一张卡孤零零放着，坏法是看不出来的。
    """
    from barkit.shell.ui_kit import note, settings_section

    host = QWidget()
    lay = QVBoxLayout(host)
    lbl = note("说明行", host)
    lay.addWidget(lbl)
    card, _content = settings_section("键位", host)
    lay.addWidget(card, stretch=stretch)
    qtbot.add_widget(host)
    host.resize(420, 300)
    host.show()
    qtbot.wait(20)
    return host, lbl, card


def test_note_row_keeps_its_height_when_the_page_grows(qtbot):
    """说明行的高度**只由文字决定** —— 不许被页面的余高撑开。

    它是 ``QLabel``，默认竖直策略带 ``GrowFlag``；而设置页最常见的形态就是「说明行
    ＋ 若干张卡」上下排。页面比内容高时（卡没折叠、也没铺满，余高没主），这一行会把
    余高整段吃走 —— 从一行 17px 撑成一大块空白，看着像页面被掏空了。
    """
    from barkit.shell.ui_kit import note

    host = QWidget()
    lay = QVBoxLayout(host)
    lbl = note("说明行", host)
    lay.addWidget(lbl)
    qtbot.add_widget(host)
    host.resize(420, 300)
    host.show()
    qtbot.wait(20)
    tall = lbl.height()

    host.resize(420, 600)
    qtbot.wait(20)

    assert lbl.height() == tall, f"说明行被余高撑开了（{tall} → {lbl.height()}）"


def test_folding_a_card_leaves_everything_else_in_place(qtbot):
    """收起分区卡：**只有卡变矮，同列内容一个都不许动**。

    2026-09-24 用户报「点『键位』收起 card，整个界面错乱」。坏法有两处，且第二处
    比第一处更隐蔽：① 标题栏自己带 ``GrowFlag``，卡里只剩它一个子件时被拉满整张
    卡（底色糊成一块）；② 卡让出的余高被同列说明行吃走，卡沉到页底（实测从
    ``y=29`` 掉到 ``y=296``）。三件事缺一不可，所以三处一起断言 —— 只断言「卡变矮」
    的话，第 ② 种坏法照样全绿。
    """
    _host, lbl, card = _collapsible_host(qtbot)
    head = card.findChild(_SectionHeader)
    y_note, h_note, y_card, h_head = lbl.y(), lbl.height(), card.y(), head.height()
    assert h_head < 60, f"前提：标题栏本来只有内容那么高（实测 {h_head}）"

    _click(head)
    qtbot.wait(20)

    assert head.height() == h_head, "标题栏被拉高了（底色会糊满整张卡）"
    assert card.height() == h_head, "卡没缩到标题栏那一截"
    assert (lbl.y(), lbl.height()) == (y_note, h_note), "说明行被卡让出的余高撑开了"
    assert card.y() == y_card, "卡沉到了页底（余高没人接）"


def test_unfolding_a_card_restores_the_page(qtbot):
    """收起再展开 ⇒ **回到收起来之前的样子**（同一页反复收放不许漂）。

    这条单独守着「展开时把伸展与那根空档还回去」：只做收起那一半，收一次页面就
    再也回不到原样（卡不再铺满）。开头那句前提断言不是装饰 —— 少了它，折叠要是
    根本没生效（标题栏收不起来），「展开后 == 收起前」照样成立、用例恒绿。
    """
    _host, lbl, card = _collapsible_host(qtbot)
    head = card.findChild(_SectionHeader)
    before = (lbl.y(), lbl.height(), card.y(), card.height())

    _click(head)
    qtbot.wait(20)
    assert card.height() < before[3], "前提：这一下真的把卡收起来了"
    _click(head)
    qtbot.wait(20)

    assert (lbl.y(), lbl.height(), card.y(), card.height()) == before, \
        "展开后没回到收起来之前的样子"


def test_section_header_lets_its_band_show_through(qtbot):
    """标题栏那条**带子必须透到字底下来** —— 栏内标签不许自带底色。

    全局 QSS 给 ``QWidget`` 铺了 ``surface``，而 Qt 的类型选择器**命中子类** ⇒
    ``QLabel`` 也吃这一条。栏内那两枚标签（标题 / 折叠箭头）于是各填一块 surface
    实底，压在自己画的 surface3 带子上 —— 真机上就是「标题字底下蒙了一层白」
    （2026-09-21 用户上报）。取样点取**标签框内、字形笔画外**（左上角 1px 处）。

    带子色按 ``current_system_theme()`` 取：它与自绘那条路（``_theme_tokens()``）
    同源，于是这条断言在浅 / 深机器上都是同一个判据，不随测试机外观漂移。
    """
    from PySide6.QtGui import QColor
    from barkit.shell import styles
    from barkit.shell.design.tokens import TOKENS
    from barkit.shell.ui_kit import _SectionHeader, settings_section

    styles.apply_theme(QApplication.instance(), "light")
    card, _content = settings_section("一组设置")
    qtbot.add_widget(card)
    card.resize(260, 120)
    card.show()
    qtbot.wait(20)
    head = card.findChild(_SectionHeader)
    img = head.grab().toImage()

    band = QColor(TOKENS[styles.current_system_theme()]["surface3"])
    for lbl in head.findChildren(QLabel):
        if not lbl.text():
            continue
        got = img.pixelColor(lbl.x() + 1, lbl.y() + 1)
        assert got == band, (
            f"{lbl.text()!r} 框内画的是 {got.name()}，应是带子本色 {band.name()}"
        )


def test_section_band_covers_the_card_edge_to_edge(qtbot):
    """标题栏那条带子要**左右对称地铺满卡片内沿**。

    早先右侧比左侧少一列（``QRect.right()`` 已经是 ``w-1``，再 ``adjusted(-1)``
    得 ``w-2`` ⇒ 带子右缘落在 ``w-2``，与描边之间露一道白缝），用户上报「右侧没有
    覆盖到位」。这里用「**显示 / 隐藏标题栏求差**」取带子的真实足迹 —— 直接比颜色
    不行：带子色与「描边半覆盖压在白底上」的混色数值撞车，分不出谁是谁。
    """
    from barkit.shell.ui_kit import _SectionHeader, settings_section

    card, _content = settings_section("一组设置")
    qtbot.add_widget(card)
    card.resize(240, 120)
    card.show()
    qtbot.wait(20)
    head = card.findChild(_SectionHeader)

    with_head = card.grab().toImage()
    head.hide()
    qtbot.wait(20)
    without = card.grab().toImage()
    head.show()

    w = with_head.width()
    mask = [
        [with_head.pixelColor(x, y) != without.pixelColor(x, y) for x in range(w)]
        for y in range(with_head.height())
    ]
    rows = [i for i, row in enumerate(mask) if any(row)]
    cols = [x for x in range(w) if any(mask[y][x] for y in rows)]

    assert rows, "标题栏该画出一条带子"
    assert rows[0] == 1, f"上沿让出卡片描边那 1px（实测 {rows[0]}）"
    assert cols[0] == 1 and cols[-1] == w - 2, (
        f"带子应左右对称地铺满内沿（实测 {cols[0]}…{cols[-1]}，卡宽 {w}）"
    )
    assert sum(mask[rows[-1]]) == w - 2, "带子下沿（展开态）应整行铺满"


def test_card_border_is_one_full_pixel_on_straight_edges(qtbot):
    """卡片描边在**直边**上必须是满 1px 的 border 本色，不是「半像素＋反锯齿」。

    ``QRect.adjusted(0.5, …)`` 会把 0.5 截成 0 ⇒ 1px 的笔一半画到控件外被裁掉，
    直边只剩半像素（浅色下混成 #f1f1f4），而圆角被反锯齿补回来 ⇒ 同一条边两种
    粗细。内缩必须用 **QRectF**，整条描边才落在控件内、处处同宽。

    容差 ≤2/255：**不是**为了放过「差一点」的渲染。实测本机上沿恒读出比令牌亮
    1/255（`#3b3b3f` vs `#3a3a3e`），那是 Qt 预乘 alpha 的取整，四条直边里只有
    上沿这一行有；而它要防的那种回归（去掉 0.5 内缩）会让**四条边一起**变成
    `#323236` —— 差 8/255，容差 2 照样一抓一个准（已用变异核过）。
    """
    from barkit.shell import styles
    from barkit.shell.design import metrics
    from barkit.shell.design.tokens import TOKENS
    from barkit.shell.ui_kit import Card

    card = Card(radius=metrics.RADIUS_SM)
    qtbot.add_widget(card)
    card.resize(120, 80)
    card.show()
    qtbot.wait(20)
    img = card.grab().toImage()

    want = TOKENS[styles.current_system_theme()]["border"].lower()
    wr, wg, wb = (int(want[i:i + 2], 16) for i in (1, 3, 5))
    mid_x, mid_y = img.width() // 2, img.height() // 2
    for x, y, where in (
        (0, mid_y, "左"), (img.width() - 1, mid_y, "右"),
        (mid_x, 0, "上"), (mid_x, img.height() - 1, "下"),
    ):
        got = img.pixelColor(x, y)
        off = max(abs(got.red() - wr), abs(got.green() - wg), abs(got.blue() - wb))
        assert off <= 2, (
            f"{where}边描边是 {got.name().lower()}，应为满 1px 的 {want}"
            f"（每通道偏差 {off}/255，超过 2 就是半像素被裁了）"
        )


def test_section_header_fold_mark_is_big_enough_to_hit(qtbot):
    """折叠箭头要**看得见且点得到**：它是一枚可点的控件，不是装饰。

    字号是量出来的：14px 的 ``▾`` 在 12px 高的带子里只有「一个墨点」，用户上报
    「小三角可以再大一点」。同时它是**交互件**（点了收起整组）⇒ 走 ``text2``
    而不是装饰档 ``text3``（后者压在浅底带子上只有 2.8:1）。
    """
    import re

    from barkit.shell.ui_kit import _SectionHeader, settings_section

    card, _content = settings_section("一组设置")
    qtbot.add_widget(card)
    head = card.findChild(_SectionHeader)
    mark = [c for c in head.findChildren(QLabel) if c.text() == "▾"][0]
    px = int(re.search(r"font-size:(\d+)px", mark.styleSheet()).group(1))
    assert px >= 16, f"折叠箭头 {px}px：比一枚 12px 的正文还小，点不到也看不清"


def test_machine_title_and_icon_outrank_their_options(qtbot):
    """一台状态机的**图标与名字**必须压过它自己的取值行 —— 否则是一张平的表。

    三条差距一起给（2026-09-21 用户上报「图标和文字与枚举差别不够明显」）：
    图标比取值圆圈大一档、名字比取值名大一号且加重、名字与取值之间多一档间距
    （取值是**挂在**这台机下面的，从属关系靠间距表达，不靠缩进 —— 缩进会破坏
    两者**左缘对齐**）。
    """
    import re

    from barkit.contribution.specs import StateLevel, StateOption
    from barkit.shell.design import metrics
    from barkit.shell.state_machines_section import StateMachinesSection, state_key

    spec = StateSpec(
        plugin_id="fake", key="notify", title="通知", icon=IconName.GEAR,
        states=[
            StateOption(id="allowed", level=StateLevel.OK, text="正常"),
            StateOption(id="muted", level=StateLevel.WARN, text="已静音"),
        ],
    )
    sec = StateMachinesSection(specs=[spec])
    qtbot.add_widget(sec)

    def size(widget) -> float:
        return float(re.search(r"font-size:([\d.]+)px", widget.styleSheet()).group(1))

    title = sec._titles[state_key(spec)]
    opt_name = [
        lbl for lbl in sec.findChildren(QLabel) if lbl.text() == "正常"
    ][0]
    assert size(title) >= size(opt_name) + 2, "机器名要比取值名**大一号**，不是同档"
    assert "font-weight:700" in title.styleSheet(), "机器名是这一段的主语 ⇒ 最重一档"

    # 图标：与取值圆圈共用一列图形槽（左缘对齐），但**画满这一格**
    glyph = sec._glyph(spec, sec)
    assert glyph.width() == metrics.GLYPH_W
    assert metrics.GLYPH_W >= 16, "图形槽要比 11.5px 的取值圆圈宽出一档"
    assert glyph.pixmap().width() == metrics.GLYPH_W, "图标画满槽 ⇒ 与圆圈拉开体积差"


def test_settings_section_without_title_has_no_header(qtbot):
    """标题为空 ⇒ **不建标题栏**（没有标题就没有要标出来的那一组）⇒ 也不可折叠。"""
    from barkit.shell.ui_kit import _SectionHeader, settings_section

    card, _content = settings_section("")
    qtbot.add_widget(card)
    assert card.findChild(_SectionHeader) is None


def _wheel_event(target, dy=120):
    """滑过 ``target`` 中心的一格滚轮（离屏合成，用于代替真机滚轮）。"""
    center = target.rect().center()
    return QWheelEvent(
        QPointF(center), target.mapToGlobal(center), QPoint(0, 0), QPoint(0, dy),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )


def test_number_field_lets_the_wheel_scroll_the_page(qtbot):
    """数字格上的滚轮**只滚页面**：不改值、事件交回滚动区。

    同一份断言在原生 ``QSpinBox`` 上**逐条翻掉**（下面当控制组跑一遍），这正是
    2026-09-21 用户上报「滚设置页时端口被改掉」的机制（离屏实测）：

    - **滚轮不需要焦点就能改值**：控制组里焦点正停在同一个窗口的第一枚控件上，
      滚轮滑过原生格子照样 1080 → 1081。用户说的「很容易被聚焦」在真机上是另一层
      —— 原生默认 ``focusPolicy`` 是 ``Qt.WheelFocus``（实测值 15，控制组末条断言），
      即**允许滚轮取得焦点**；离屏合成事件走不到那一步，故本条只钉策略、不钉观感；
    - 事件被 ``accept`` ⇒ 这一次滚动**被吞**（页面不动，用户只当没滚上，再滚 ⇒ 又改一格）；
    - 值改完**会落盘**：设置项一律「改完即写、没有保存键」，而 ``editingFinished``
      在**失焦时补发**（滚轮当次不发）⇒ 一次误滚就是一个写进配置文件的错端口，
      界面上没有任何提示。
    """
    from PySide6.QtWidgets import QLineEdit, QVBoxLayout

    from barkit.shell.ui_kit import NumberField

    # 焦点是「窗口内」的概念，离屏下裸顶层窗拿不到 active ⇒ 三枚控件挂进同一个窗口；
    # 第一枚放 QLineEdit：窗口激活时焦点落在它身上，于是「滚过数字格」时焦点在别处
    # —— 正是用户浏览设置页时的真实状态。
    host = QWidget()
    qtbot.addWidget(host)
    lay = QVBoxLayout(host)
    elsewhere = QLineEdit(host)
    lay.addWidget(elsewhere)

    box = NumberField(host)
    lay.addWidget(box)
    box.setRange(1, 65535)
    box.setValue(1080)
    host.show()
    QApplication.processEvents()   # 离屏下要先走一轮事件，窗口才有 active 态
    assert host.focusWidget() is elsewhere, "前置：焦点在别处（不在数字格上）"

    ev = _wheel_event(box)
    QApplication.sendEvent(box, ev)
    assert box.value() == 1080, "滚轮不该改值"
    assert not box.hasFocus(), "滚轮不该把焦点拿过来"
    assert host.focusWidget() is elsewhere, "滚轮不该动焦点"
    assert not ev.isAccepted(), "事件要留给父级滚动区（被吃掉 ⇒ 页面滚不动）"
    assert box.focusPolicy() == Qt.FocusPolicy.StrongFocus

    # 改值的入口没被一起关掉：步进（右侧箭头与 ↑↓ 都走它）照常有效
    box.stepBy(1)
    assert box.value() == 1081

    raw = QSpinBox(host)
    lay.addWidget(raw)
    raw.setRange(1, 65535)
    raw.setValue(1080)
    elsewhere.setFocus()          # 焦点明确停在别处（上面 stepBy 可能把焦点留在 box 上）
    assert host.focusWidget() is elsewhere, "前置：控制组同样从「焦点在别处」开始"
    rev = _wheel_event(raw)
    QApplication.sendEvent(raw, rev)
    assert raw.value() == 1081, "控制组：原生 QSpinBox 会被滚轮改值（门禁守的就是它）"
    assert rev.isAccepted(), "控制组：原生 QSpinBox 会把这次滚动吃掉"
    assert raw.focusPolicy() == Qt.FocusPolicy.WheelFocus, (
        "控制组：原生默认策略允许滚轮取焦（真机上「一滚就被聚焦」的观感来源）"
    )


def test_combo_field_lets_the_wheel_scroll_the_page(qtbot):
    """下拉格同一条口径：滚轮只滚页面，不改选中项 —— **弹层开着时**除外。

    ``QComboBox`` 是比数字格更隐蔽的同一个坑（2026-09-21 顺带实测）：焦点停在别处时
    滑过它照样把选中项改掉，而它的默认 ``focusPolicy`` 只是 ``TabFocus``（实测值 1），
    连「被聚焦」的迹象都没有 ⇒ 用户只会看见「主图标」之类的一项莫名换了。
    """
    from PySide6.QtWidgets import QComboBox, QLineEdit, QVBoxLayout

    from barkit.shell.ui_kit import ComboField

    host = QWidget()
    qtbot.addWidget(host)
    lay = QVBoxLayout(host)
    elsewhere = QLineEdit(host)
    lay.addWidget(elsewhere)

    box = ComboField(host)
    lay.addWidget(box)
    box.addItems(["a", "b", "c"])
    box.setCurrentIndex(1)
    host.show()
    QApplication.processEvents()
    assert host.focusWidget() is elsewhere, "前置：焦点在别处（不在下拉格上）"

    ev = _wheel_event(box)
    QApplication.sendEvent(box, ev)
    assert box.currentIndex() == 1, "滚轮不该改选中项"
    assert not ev.isAccepted(), "事件要留给父级滚动区"

    # 弹层开着时滚轮是在翻列表 —— 那一趟要还给下拉自己，不能拿去滚页面
    box.showPopup()
    QApplication.processEvents()
    try:
        ev2 = _wheel_event(box)
        QApplication.sendEvent(box, ev2)
        assert ev2.isAccepted(), "弹层开着 ⇒ 滚轮归列表自己用（不再冒泡给页面）"
    finally:
        box.hidePopup()

    raw = QComboBox(host)
    lay.addWidget(raw)
    raw.addItems(["a", "b", "c"])
    raw.setCurrentIndex(1)
    elsewhere.setFocus()
    rev = _wheel_event(raw)
    QApplication.sendEvent(raw, rev)
    assert raw.currentIndex() == 0, "控制组：原生 QComboBox 会被滚轮改掉选中项"
    assert rev.isAccepted(), "控制组：原生 QComboBox 会把这次滚动吃掉"


def test_choice_bar_picks_by_value_and_only_speaks_up_on_a_change(qtbot):
    """单选组：点芯片发**取值**，点当前那枚**什么都不发**，``set_current`` 也不发。

    三条各自对应一次真实的坑：

    - 发的是**取值**（不是显示名）：显示名是给人读的标签，改名不该让已存下的取值失效
      —— 与 ``ChoiceSelector`` 同一条口径，两者可以互换（``detail_view`` 那两处就是在
      它们之间换的）；
    - 点当前那枚不发信号：``selected`` 的含义是「**用户改选了**」，而
      ``detail_view._on_ask_changed`` 那类槽会跟着跑一遍副作用；
    - ``set_current`` 不发：装载 ≠ 用户选择（发出去会让调用方白写一次盘）。
    """
    from barkit.shell.ui_kit import ChoiceBar

    bar = ChoiceBar([("甲", "a"), ("乙", "b")], "a")
    qtbot.addWidget(bar)
    seen = []
    bar.selected.connect(seen.append)

    bar._btns[1][0].click()
    assert seen == ["b"], "点了「乙」却没说出去（或说成了显示名）"
    assert bar.current() == "b"

    bar._btns[1][0].click()
    assert seen == ["b"], "再点当前那枚不该再发一次（那是「改选了」不是「点了一下」）"

    bar.set_current("a")
    assert bar.current() == "a"
    assert seen == ["b"], "程序化装载不许发信号"


def test_choice_bar_marks_the_pick_and_falls_back_to_the_first(qtbot):
    """亮 = 选中（``pill-chip-on``），取值不在候选里时**落第一项**。

    回落这一条是承重的：候选集合变过之后旧取值常常已经不在册（如某一档被删），留一个
    解析不了的值不如落到第一项 —— 与 ``ChoiceSelector`` 同款回落。亮档走 ``objectName``
    （QSS 按它匹配），故这里量的就是它。
    """
    from barkit.shell.ui_kit import ChoiceBar

    bar = ChoiceBar([("甲", "a"), ("乙", "b")], "b")
    qtbot.addWidget(bar)
    assert [b.objectName() for b, _v in bar._btns] == ["pill-chip", "pill-chip-on"]

    bar.set_current("没有这一档")
    assert bar.current() == "a"
    assert [b.objectName() for b, _v in bar._btns] == ["pill-chip-on", "pill-chip"]


def test_choice_bar_hangs_the_hint_on_the_chip_not_in_the_label(qtbot):
    """释义挂**芯片的悬停提示**，不写进标签。

    三个选项的解释一次摊在标签里，控件本身就被字淹掉了 —— 释义属于「你选中的那一项」，
    没选的那几项此刻不需要知道（与 ``ui_kit.choice_hint`` 那条裁定同门）。
    """
    from barkit.shell.ui_kit import ChoiceBar

    bar = ChoiceBar([("甲", "a"), ("乙", "b")], "a", hints={"a": "甲是这么回事"})
    qtbot.addWidget(bar)
    assert bar._btns[0][0].toolTip() == "甲是这么回事"
    assert bar._btns[0][0].text() == "甲", "释义不该跑进标签"
    assert bar._btns[1][0].toolTip() == "", "没给释义就不挂（它只是解释，不是必须有的文案）"


def test_state_option_rows_align_and_tint_by_level(qtbot):
    """取值行两件事：圆圈**与标题共用一列**（文字左缘对齐）、取值名**按级别染色**。

    染色走的是**文字档**令牌（``green_text`` / ``orange_text`` / ``red_text``）：
    图形档那套是「压得住白图形」的，拿来写 11.5px 的小字在卡面上读不动 —— 而
    ``apply_text_role`` 之外的**调色板换色一律无效**（QSS 的 color 盖掉 palette），
    这条顺带把「颜色真的进了样式表」钉死。
    """
    from barkit.contribution.specs import StateLevel, StateOption
    from barkit.shell import styles
    from barkit.shell.design import metrics
    from barkit.shell.design.levels import LEVEL_COLOR, LEVEL_TEXT_COLOR
    from barkit.shell.design.tokens import TOKENS
    from barkit.shell.state_machines_section import StateMachinesSection

    spec = StateSpec(
        plugin_id="fake", key="notify", title="通知",
        states=[
            StateOption(id="allowed", level=StateLevel.OK, text="正常"),
            StateOption(id="muted", level=StateLevel.WARN, text="已静音"),
        ],
    )
    sec = StateMachinesSection(specs=[spec])
    qtbot.add_widget(sec)

    dots = [dot for dot, _id in sec._option_dots["notify"]]
    assert [d.width() for d in dots] == [metrics.GLYPH_W] * 2, "与标题图标同列宽 ⇒ 左缘对齐"
    assert sec._glyph(spec, sec).width() == metrics.GLYPH_W

    t = TOKENS[styles.current_theme()]
    for (dot, _id), opt in zip(sec._option_dots["notify"], spec.states):
        assert f"color:{t[LEVEL_COLOR[opt.level][0]]}" in dot.styleSheet(), "圆圈走图形档"
        assert dot.text() == "○", "非当前 ⇒ 空心"
    for lbl in sec.findChildren(QLabel):
        if lbl.text() == "已静音":
            want = f"color:{t[LEVEL_TEXT_COLOR[StateLevel.WARN]]}"
            assert want in lbl.styleSheet(), "取值名走文字档（不是图形档那个红/橙/绿）"


def _click(widget) -> None:
    """在控件上打一次左键按下（``mousePressEvent`` 那条路，不走事件循环）。"""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    ev = QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(1, 1), QPointF(1, 1),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    widget.mousePressEvent(ev)


def _chevron(header) -> str:
    from PySide6.QtWidgets import QLabel

    marks = [
        c.text() for c in header.findChildren(QLabel) if c.text() in ("▾", "▸")
    ]
    return marks[0] if marks else ""


def _scrolled_widget(widget):
    """``_scrolled`` 把页面套进滚动区；测试要的是**页面本身**。"""
    from PySide6.QtWidgets import QScrollArea

    if isinstance(widget, QScrollArea):
        return widget.widget()
    return widget


# ---------- 「通用」分页：菜单栏（主图标来源 P0③ ＋ 角标阈值 D17） ----------


def _contrib_with_display_state():
    states = [
        # 声明了主图标 ⇒ 取得被选资格（准入判据只有这一条）
        StateSpec(
            plugin_id="notify_guard", key="display", title="第二屏",
            icon=IconName.DISPLAY_OFF,
        ),
        # 未声明主图标（icon 默认 None）⇒ 不进候选：没有图标可画，选它只会得空格
        StateSpec(plugin_id="notify_guard", key="notify", title="通知"),
    ]
    comps = [ComponentSpec(plugin_id="notify_guard", icon=IconName.BELL, name="录屏通知")]
    return _FakeContrib([], states=states, components=comps)


def test_general_page_lists_only_states_with_main_icon(qtbot):
    contrib = _contrib_with_display_state()
    cfg = _FakeConfig()
    win = SettingsWindow(parent=None, registry=contrib, manager=_FakeManager(), config=cfg)
    qtbot.add_widget(win)

    combo = win._tray_base_selector._combo
    labels = [combo.itemText(i) for i in range(combo.count())]
    data = [combo.itemData(i) for i in range(combo.count())]
    assert labels == ["BarKit 默认", "录屏通知 · 第二屏"]
    # 取值是状态机**身份**（`{plugin_id}.{key}`），不是事件总线 topic：配置与 pub/sub 解耦
    assert data == ["", "notify_guard.display"]
    assert win._tray_base_selector.current() == ""      # 出厂默认 = 不预设


def test_general_page_tray_base_selection_persists_and_notifies(qtbot):
    contrib = _contrib_with_display_state()
    cfg = _FakeConfig()
    calls: list[int] = []
    win = SettingsWindow(
        parent=None, registry=contrib, manager=_FakeManager(), config=cfg,
        on_tray_base_changed=lambda: calls.append(1),
    )
    qtbot.add_widget(win)

    combo = win._tray_base_selector._combo
    combo.setCurrentIndex(1)                            # 选「录屏通知 · 第二屏」
    assert cfg.writes == ["notify_guard.display"]
    assert calls == [1]                                 # 必须回调装配器立即换图


def test_general_page_tray_base_selector_degrades_without_registry(qtbot):
    """轻量桩 / 无 config 时不得让「通用」页构建失败（只留默认项、置灰）。"""
    win = SettingsWindow(parent=None, registry=None, manager=None)
    qtbot.add_widget(win)
    combo = win._tray_base_selector._combo
    assert [combo.itemText(i) for i in range(combo.count())] == ["BarKit 默认"]
    assert not combo.isEnabled()                        # 无处可选 → 不假装可点


# ---------- 「通用」分页：托盘角标阈值（决议 D17，E 批） ----------


def test_general_page_badge_level_offers_exactly_the_contract_values(qtbot):
    """候选**由契约值域生成**（``TRAY_MIN_LEVELS``），本页不另抄一份字面量。

    用户看到的是「哪几级会出角标」，不是 `warn` / `err` —— 显示名与取值分家。
    """
    win = SettingsWindow(parent=None, registry=None, manager=None, config=_FakeConfig())
    qtbot.add_widget(win)

    combo = win._badge_level_selector._combo
    assert [combo.itemData(i) for i in range(combo.count())] == list(TRAY_MIN_LEVELS)
    labels = [combo.itemText(i) for i in range(combo.count())]
    assert labels == ["警告与错误（默认）", "仅错误"]
    # 与「主图标」那格不同：这两档任何环境都选得动（不依赖有没有组件声明图标）
    assert combo.isEnabled()
    assert win._badge_level_selector.current() == TRAY_MIN_LEVEL_DEFAULT


def test_general_page_badge_level_starts_from_effective_config(qtbot):
    """起点是**合并后**的有效配置：盘上是 `err`，首屏就得停在「仅错误」。"""
    win = SettingsWindow(
        parent=None, registry=None, manager=None, config=_FakeConfig(min_level="err")
    )
    qtbot.add_widget(win)
    assert win._badge_level_selector.current() == "err"


def test_general_page_badge_level_persists_and_notifies(qtbot):
    """改档即落盘 ＋ 回调装配器；不回调的话角标要等下一个状态事件才跟着变。"""
    cfg = _FakeConfig()
    calls: list[int] = []
    win = SettingsWindow(
        parent=None, registry=None, manager=None, config=cfg,
        on_badge_level_changed=lambda: calls.append(1),
    )
    qtbot.add_widget(win)

    combo = win._badge_level_selector._combo
    combo.setCurrentIndex(combo.findData("err"))        # 收紧到「仅错误」
    assert cfg.level_writes == ["err"]
    assert calls == [1]


def test_general_page_badge_level_degrades_without_config(qtbot):
    """无 config 时不打断「通用」页构建：候选照列、点了只跳过落盘那一半。"""
    win = SettingsWindow(parent=None, registry=None, manager=None)
    qtbot.add_widget(win)
    combo = win._badge_level_selector._combo
    assert [combo.itemData(i) for i in range(combo.count())] == list(TRAY_MIN_LEVELS)
    assert win._badge_level_selector.current() == TRAY_MIN_LEVEL_DEFAULT
    combo.setCurrentIndex(combo.findData("err"))         # 不该抛


# ---------- 「插件」分页：显示位开关芯片（契约规范 §2.4，决议 D4） ----------


def _chips(row):
    """取一行里的显示位芯片（按视觉名筛，不含行内其它按钮）。"""
    from PySide6.QtWidgets import QPushButton

    return [
        b for b in row.findChildren(QPushButton)
        if b.objectName() in ("pill-chip", "pill-chip-on")
    ]


def _slot_contrib(rows=None, actions=(), entries=()):
    """一个「显示位都声明了」的组件；``rows=[]`` 可造出「没有列表行」的组件。

    ``actions`` / ``entries`` 只影响「快捷区域」这一位：两个声明点（``ActionSpec``
    与 ``DetailEntrySpec``）都算数，故留两个口子分别喂。
    """
    from barkit.contribution.specs import StatusRowSpec

    if rows is None:
        rows = [StatusRowSpec(plugin_id="notify_guard", key="main")]
    return _FakeContrib(
        [],
        states=[
            StateSpec(
                plugin_id="notify_guard", key="display", title="第二屏",
                icon=IconName.DISPLAY_OFF,
            )
        ],
        components=[
            ComponentSpec(plugin_id="notify_guard", icon=IconName.BELL, name="录屏通知")
        ],
        rows=list(rows),
        actions=list(actions),
        entries=list(entries),
    )


def test_slot_label_table_matches_contract():
    """中文名表必须与契约里的显示位一一对应（新增一位却忘了补名 ⇒ 芯片显示英文键）。"""
    from barkit.core.config import PLUGIN_UI_SLOTS
    from barkit.shell.settings import _SLOT_LABELS

    assert set(_SLOT_LABELS) == set(PLUGIN_UI_SLOTS)


def test_plugin_row_chips_cover_only_declared_slots(qtbot):
    """芯片只列该组件**真有的**显示位：没声明列表行就没有「列表行」这枚。"""
    contrib = _slot_contrib(rows=[])                    # 只声明了标题栏 + 状态机
    win = SettingsWindow(
        parent=None, registry=contrib, manager=_OrderManager(["notify_guard"]),
        config=_FakeConfig(),
    )
    qtbot.add_widget(win)
    row = win._order_editor._rows["notify_guard"]
    assert [c.text() for c in _chips(row)] == ["标题栏", "托盘角标"]

    contrib2 = _FakeContrib([])
    win2 = SettingsWindow(
        parent=None, registry=contrib2, manager=_OrderManager(["bare"]),
        config=_FakeConfig(),
    )
    qtbot.add_widget(win2)
    assert _chips(win2._order_editor._rows["bare"]) == []   # 一个显示位都没有 → 不占版面


def test_slot_chip_toggles_config_and_notifies_assembler(qtbot):
    """点芯片：翻转 → 落盘 → 通知装配器重画；芯片外观与悬停提示当场跟上。"""
    cfg = _FakeConfig()
    calls: list[int] = []
    win = SettingsWindow(
        parent=None, registry=_slot_contrib(), manager=_OrderManager(["notify_guard"]),
        config=cfg, on_display_changed=lambda: calls.append(1),
    )
    qtbot.add_widget(win)
    chips = _chips(win._order_editor._rows["notify_guard"])
    assert [c.text() for c in chips] == ["标题栏", "列表行", "托盘角标"]
    assert [c.objectName() for c in chips] == ["pill-chip-on"] * 3   # 默认全可见

    chips[1].click()                                     # 关掉「列表行」
    assert cfg.slot_writes == [("notify_guard", "row", False)]
    assert calls == [1]
    assert chips[1].objectName() == "pill-chip"
    assert "已隐藏" in chips[1].toolTip()
    assert "只影响这一处显示" in chips[0].toolTip()        # 未关的那两枚给「隐藏≠停用」的原话

    chips[1].click()                                     # 再点恢复
    assert cfg.slot_writes[-1] == ("notify_guard", "row", True)
    assert chips[1].objectName() == "pill-chip-on"
    assert len(calls) == 2


def test_action_slot_chip_covers_both_declaration_points(qtbot):
    """「快捷区域」有两个声明点（`ActionSpec` / `DetailEntrySpec`），且它**默认不勾**。

    契约 §2.4 把两者写成同一个显示位：用户看到的是面板操作区同一个位置，故**任一**
    写法都该长出这枚芯片。2026-09-22 起 `DetailEntrySpec` **不再分 trigger**（字段已
    删）—— 行点击与快捷按钮是**同一个过程**的两个入口，所以每条入口声明都算这一位。

    同一轮用户裁定（D37）：这一位**默认关**（快捷方式是可选加速度，行上本来就点得进），
    故芯片出厂是暗的、点一下是**打开**它。
    """
    from barkit.contribution.specs import ActionSpec, DetailEntrySpec

    cfg = _FakeConfig()
    calls: list[int] = []
    win = SettingsWindow(
        parent=None,
        registry=_slot_contrib(
            actions=[
                ActionSpec(plugin_id="notify_guard", label="截图识别",
                           on_click=lambda: None)
            ],
            entries=[
                DetailEntrySpec(plugin_id="notify_guard", key="act", label="动作入口",
                                build=None),
            ],
        ),
        manager=_OrderManager(["notify_guard"]),
        config=cfg, on_display_changed=lambda: calls.append(1),
    )
    qtbot.add_widget(win)
    chips = _chips(win._order_editor._rows["notify_guard"])
    assert [c.text() for c in chips] == ["标题栏", "列表行", "快捷区域", "托盘角标"]
    # 出厂态：标题栏 / 列表行 / 托盘角标亮，**快捷区域暗**（D37）
    assert [c.objectName() for c in chips] == [
        "pill-chip-on", "pill-chip-on", "pill-chip", "pill-chip-on",
    ]
    assert "已隐藏" in chips[2].toolTip()

    chips[2].click()                                     # 打开「快捷区域」
    assert cfg.slot_writes == [("notify_guard", "action", True)]
    assert calls == [1]
    assert chips[2].objectName() == "pill-chip-on"

    chips[2].click()                                     # 再点关回默认
    assert cfg.slot_writes[-1] == ("notify_guard", "action", False)
    assert chips[2].objectName() == "pill-chip"


def test_action_slot_chip_absent_without_any_action(qtbot):
    """没声明任何动作的组件不长「快捷区域」：关一个本来就不存在的东西＝开关坏了。"""
    win = SettingsWindow(
        parent=None, registry=_slot_contrib(), manager=_OrderManager(["notify_guard"]),
        config=_FakeConfig(),
    )
    qtbot.add_widget(win)
    labels = [c.text() for c in _chips(win._order_editor._rows["notify_guard"])]
    assert labels == ["标题栏", "列表行", "托盘角标"]


def test_titlebar_chip_absent_when_component_declares_in_header_false(qtbot):
    """``in_header=False`` 的组件（无状态机，如一键输入）不上标题栏，故设置窗
    也不给它出「标题栏」芯片：否则用户会看到一颗能点的开关、点了却没反应
    （那一格本就不存在，panel.py 渲染时按 ``in_header`` 直接排除）。

    用「有行、有状态机」的对照把这条钉死 —— 这种组件**会**长出「列表行」「托盘角标」，
    唯独不该有「标题栏」，从而排除「因为它没声明别的位所以连带没标题栏」这种歧义。
    """
    from barkit.contribution.specs import ComponentSpec, StatusRowSpec

    contrib = _FakeContrib(
        [],
        states=[StateSpec(plugin_id="autotype", key="display", title="输入",
                          icon=IconName.DISPLAY_OFF)],
        components=[ComponentSpec(plugin_id="autotype", icon=IconName.KEYBOARD,
                                  name="一键输入", in_header=False)],
        rows=[StatusRowSpec(plugin_id="autotype", key="main")],
    )
    win = SettingsWindow(
        parent=None, registry=contrib, manager=_OrderManager(["autotype"]),
        config=_FakeConfig(),
    )
    qtbot.add_widget(win)
    labels = [c.text() for c in _chips(win._order_editor._rows["autotype"])]
    assert labels == ["列表行", "托盘角标"]   # 有行、有状态机，但**没有**标题栏


def test_slot_chips_degrade_without_status_rows(qtbot):
    """注册中心桩缺 ``status_rows`` 门面时不得让「插件」页构建失败。"""
    class _BareStub:
        def settings_pages(self):
            return []

        def states(self):
            return []

        def components(self):
            return [ComponentSpec(plugin_id="a", icon=IconName.BELL, name="甲")]

        def tray_base_candidates(self):
            return []

        def hotkeys(self):
            return []

    win = SettingsWindow(
        parent=None, registry=_BareStub(), manager=_OrderManager(["a"]),
        config=_FakeConfig(),
    )
    qtbot.add_widget(win)
    assert [c.text() for c in _chips(win._order_editor._rows["a"])] == ["标题栏"]


# ---------- 「插件」分页：安装入口 + 行内覆盖确认（决议 D5） ----------

class _InstallManager:
    """安装入口最小桩：记录调用次数，预置预检结果。"""

    def __init__(self, preview=None, reason="", ok=True, msg="已安装 demo（v0.2.0）"):
        self._preview = preview
        self._reason = reason
        self._ok = ok
        self._msg = msg
        self.previewed: list[str] = []
        self.installed: list[str] = []

    def preview_install(self, path):
        self.previewed.append(path)
        return self._preview, self._reason

    def install(self, path):
        self.installed.append(path)
        return self._ok, self._msg

    # 列表页需要的两个口子（无行时返回空）
    def list_plugins(self):
        return []

    def effective_order(self):
        return []


def _plugins_page(win):
    labels = [win._nav.item(i).text() for i in range(win._nav.count())]
    return win._stack.widget(labels.index("插件"))


def _install_window(mgr):
    return SettingsWindow(parent=None, registry=_FakeContrib([]), manager=mgr)


class _ToggleManager(_FakeManager):
    """启停桩：``enable`` 的返回值可造（False = 实例化 / ``start()`` 抛异常那一档）。"""

    def __init__(self, enable_ok=True):
        super().__init__()
        self.enable_ok = enable_ok
        self.calls: list = []

    def enable(self, plugin_id):
        self.calls.append(("enable", plugin_id))
        return self.enable_ok

    def disable(self, plugin_id):
        self.calls.append(("disable", plugin_id))
        return True


def test_plugin_toggle_reports_failure_instead_of_doing_nothing(qtbot):
    """启用失败必须出声：开关停在原状而界面一声不响 ⇒ 用户只看到「点了没反应」。"""
    mgr = _ToggleManager(enable_ok=False)
    win = _install_window(mgr)
    qtbot.add_widget(win)
    sw = Switch()

    win._on_plugin_toggle("demo_ext", sw)

    assert mgr.calls == [("enable", "demo_ext")]
    assert sw.isChecked() is False                     # 不谎报成已启用
    assert "启用失败" in win._plugin_feedback.text()   # 与安装/卸载同一条行内槽


def test_plugin_toggle_flips_switch_on_success(qtbot):
    """对照组：成功那一档照旧翻开关、不弹反馈（开关自己就是回执）。"""
    mgr = _ToggleManager(enable_ok=True)
    win = _install_window(mgr)
    qtbot.add_widget(win)
    sw = Switch()

    win._on_plugin_toggle("demo_ext", sw)

    assert sw.isChecked() is True
    assert win._plugin_feedback.text() == ""


def test_plugin_page_offers_install_entry_and_inline_feedback(qtbot):
    """安装入口两个按钮（目录 / 压缩包）＋ 一条行内反馈槽；平时不占版面。"""
    win = _install_window(_InstallManager())
    qtbot.add_widget(win)

    texts = {b.text() for b in _plugins_page(win).findChildren(QPushButton)}

    assert {"从目录安装…", "从压缩包安装…", "恢复插件数据…"} <= texts
    assert win._plugin_feedback is not None
    assert win._plugin_feedback.text() == ""            # 没动作时不显示任何东西


def test_install_first_time_asks_nothing_and_reports_result(qtbot):
    """首次安装不问确认（装到空位没有既有状态可破坏），直接把结果写进反馈槽。"""
    mgr = _InstallManager(preview=InstallPreview("demo", "0.2.0", None))
    win = _install_window(mgr)
    qtbot.add_widget(win)

    win._install_plugin("/tmp/pkg")

    assert mgr.installed == ["/tmp/pkg"]
    assert win._plugin_feedback.is_pending() is False
    assert "已安装" in win._plugin_feedback.text()


def test_install_upgrade_asks_inline_and_writes_both_versions(qtbot):
    """覆盖已有插件：先在行内问，答之前**一个字都不落盘**；点「覆盖」才装。"""
    mgr = _InstallManager(preview=InstallPreview("demo", "0.2.0", "0.1.0"))
    win = _install_window(mgr)
    qtbot.add_widget(win)

    win._install_plugin("/tmp/pkg")

    assert mgr.installed == []                          # 还没装
    fb = win._plugin_feedback
    assert fb.is_pending()                              # 正等用户点「覆盖」
    assert "0.1.0" in fb.text() and "0.2.0" in fb.text()   # 用哪个版本覆盖哪个版本

    fb.accept()

    assert mgr.installed == ["/tmp/pkg"]
    assert "已安装" in win._plugin_feedback.text()


def test_install_upgrade_cancelled_leaves_everything_alone(qtbot):
    mgr = _InstallManager(preview=InstallPreview("demo", "0.2.0", "0.1.0"))
    win = _install_window(mgr)
    qtbot.add_widget(win)

    win._install_plugin("/tmp/pkg")
    win._plugin_feedback.dismiss()                      # 用户点「取消」

    assert mgr.installed == []
    assert win._plugin_feedback.is_pending() is False


def test_install_upgrade_without_feedback_slot_is_refused(qtbot):
    """破坏性动作拿不到槽位就拒绝执行：没有确认口 ＝ 没人确认。"""
    mgr = _InstallManager(preview=InstallPreview("demo", "0.2.0", "0.1.0"))
    win = _install_window(mgr)
    qtbot.add_widget(win)
    win._plugin_feedback = None                         # 槽位不可用

    win._install_plugin("/tmp/pkg")

    assert mgr.installed == []


def test_install_bad_package_reports_reason_not_a_dialog(qtbot):
    mgr = _InstallManager(preview=None, reason="压缩包里找不到插件清单（manifest.yaml）")
    win = _install_window(mgr)
    qtbot.add_widget(win)

    win._install_plugin("/tmp/bad")

    assert mgr.installed == []
    assert "找不到插件清单" in win._plugin_feedback.text()


class _UninstallManager:
    """卸载入口最小桩：只记录被卸载的 id。"""

    def __init__(self):
        self.uninstalled: list[str] = []

    def uninstall(self, plugin_id):
        self.uninstalled.append(plugin_id)

    # 列表页需要的两个口子（无行时返回空）
    def list_plugins(self):
        return []

    def effective_order(self):
        return []


class _TransferManager:
    """导出 / 导入入口最小桩。"""

    def __init__(self, export_path="/tmp/out/demo_ext.zip", import_ok=True, import_err=""):
        self._export_path = export_path
        self._import_ok = import_ok
        self._import_err = import_err
        self.exported: list[tuple[str, str]] = []
        self.imported: list[str] = []

    def export_plugin(self, plugin_id, dest):
        self.exported.append((plugin_id, dest))
        return self._export_path

    def import_zip(self, path):
        self.imported.append(path)
        return self._import_ok, self._import_err

    def list_plugins(self):
        return []

    def effective_order(self):
        return []


class _FileDialogStub:
    """替掉 ``settings_plugin_actions.QFileDialog``：一律回预置路径（``None`` ＝ 用户取消）。

    设置窗的零模态收口只针对 ``QMessageBox`` / ``QDialog.exec()``；``QFileDialog`` 是
    **原生选择器**、不另开嵌套事件循环（判据见 ``_on_plugin_install_dir`` 的 docstring），
    故这里只打桩，不动生产代码。
    """

    def __init__(self, path):
        self.path = path

    def getExistingDirectory(self, _parent, _title):
        return self.path or ""

    def getOpenFileName(self, _parent, _title, _dir, _filter):
        return (self.path or "", "")


def test_uninstall_asks_inline_and_deletes_only_after_confirm(qtbot):
    """卸载**先在行内问**：答之前一行都不删，点下「卸载」才真删（零模态）。"""
    mgr = _UninstallManager()
    win = _install_window(mgr)
    qtbot.add_widget(win)

    win._on_plugin_uninstall("demo_ext")

    assert mgr.uninstalled == []
    fb = win._plugin_feedback
    assert fb.is_pending()                          # 正等用户点「卸载」
    assert "demo_ext" in fb.text()
    assert "不可恢复" in fb.text()                   # 破坏性后果必须写在问句里

    fb.accept()

    assert mgr.uninstalled == ["demo_ext"]
    assert win._plugin_feedback.is_pending() is False


def test_uninstall_cancelled_deletes_nothing(qtbot):
    mgr = _UninstallManager()
    win = _install_window(mgr)
    qtbot.add_widget(win)

    win._on_plugin_uninstall("demo_ext")
    win._plugin_feedback.dismiss()                  # 用户点「取消」

    assert mgr.uninstalled == []
    assert win._plugin_feedback.is_pending() is False


def test_uninstall_without_feedback_slot_is_refused(qtbot):
    """没有确认口 ＝ 没人确认：删目录不可恢复，故拒绝执行（与覆盖安装同纪律）。"""
    mgr = _UninstallManager()
    win = _install_window(mgr)
    qtbot.add_widget(win)
    win._plugin_feedback = None                     # 槽位不可用

    win._on_plugin_uninstall("demo_ext")

    assert mgr.uninstalled == []


def test_export_result_lands_in_feedback_not_a_dialog(qtbot, monkeypatch):
    mgr = _TransferManager(export_path="/tmp/out/demo_ext.zip")
    win = _install_window(mgr)
    qtbot.add_widget(win)
    monkeypatch.setattr(settings_plugin_actions, "QFileDialog", _FileDialogStub("/tmp/out"))

    win._on_plugin_export("demo_ext")

    assert mgr.exported == [("demo_ext", "/tmp/out")]
    assert win._plugin_feedback.is_pending() is False
    assert "/tmp/out/demo_ext.zip" in win._plugin_feedback.text()   # 路径要看得见


def test_export_failure_reports_reason_in_feedback(qtbot, monkeypatch):
    mgr = _TransferManager(export_path=None)        # 编排层判失败
    win = _install_window(mgr)
    qtbot.add_widget(win)
    monkeypatch.setattr(settings_plugin_actions, "QFileDialog", _FileDialogStub("/tmp/out"))

    win._on_plugin_export("demo_ext")

    assert "导出失败" in win._plugin_feedback.text()


def test_export_cancelled_says_nothing(qtbot, monkeypatch):
    """用户在原生选择器里点了取消：不显示任何反馈（那不算一次动作）。"""
    mgr = _TransferManager()
    win = _install_window(mgr)
    qtbot.add_widget(win)
    monkeypatch.setattr(settings_plugin_actions, "QFileDialog", _FileDialogStub(None))

    win._on_plugin_export("demo_ext")

    assert mgr.exported == []
    assert win._plugin_feedback.text() == ""


def test_import_result_lands_in_feedback_not_a_dialog(qtbot, monkeypatch):
    mgr = _TransferManager()
    win = _install_window(mgr)
    qtbot.add_widget(win)
    monkeypatch.setattr(settings_plugin_actions, "QFileDialog", _FileDialogStub("/tmp/pkg.zip"))

    win._on_plugin_import()

    assert mgr.imported == ["/tmp/pkg.zip"]
    assert "已恢复" in win._plugin_feedback.text()


def test_import_failure_reports_reason_in_feedback(qtbot, monkeypatch):
    mgr = _TransferManager(import_ok=False, import_err="导出包版本不兼容")
    win = _install_window(mgr)
    qtbot.add_widget(win)
    monkeypatch.setattr(settings_plugin_actions, "QFileDialog", _FileDialogStub("/tmp/pkg.zip"))

    win._on_plugin_import()

    assert "版本不兼容" in win._plugin_feedback.text()


def test_plugin_row_tooltip_carries_component_version(qtbot):
    """组件版本走悬停提示（不塞进已经很挤的副行）：外部插件有、内置插件没有。"""

    class _Mgr:
        def list_plugins(self):
            return [
                PluginInfo(id="demo_ext", name="示例外部插件", run_mode=RUN_ON_DEMAND,
                           source="external", enabled=True, loaded=True, version="0.1.0"),
                PluginInfo(id="route_guard", name="网络切换", run_mode=RUN_ON_DEMAND,
                           source="internal", enabled=True, loaded=True),
            ]

        def effective_order(self):
            return ["demo_ext", "route_guard"]

    win = SettingsWindow(parent=None, registry=_FakeContrib([]), manager=_Mgr())
    qtbot.add_widget(win)
    rows = win._order_editor._rows
    subs = {pid: [c for c in row.findChildren(QLabel)] for pid, row in rows.items()}

    ext = [c.toolTip() for c in subs["demo_ext"] if c.toolTip()]
    builtin = [c.toolTip() for c in subs["route_guard"] if c.toolTip()]
    assert any("0.1.0" in t for t in ext)
    assert builtin == []                       # 内置插件随 app 版本走，不给版本提示

    # 副行本身**不含**版本（避免把最挤的一类行再加宽）
    assert all("0.1.0" not in c.text() for c in subs["demo_ext"])


# ---------- 「维护」页：三张卡各一条行内反馈槽（2026-09-22 合并开机自启 / 权限 / 数据） ----------


def test_maintenance_page_groups_the_three_framework_only_cards(qtbot):
    """「开机自启」「权限」「数据」合成**一页三卡**，且**打开数据目录只出现一次**。

    此前「打开数据目录」被框架画在**每个组件页的页尾** —— 而它对每个组件打开的都是
    同一个目录，于是同一枚按钮被复制了 N 份（用户 2026-09-22 上报）。现在它只长在
    「维护」页；「复制诊断」不在这里（它复制的是某个组件自己的诊断）。
    """
    win = SettingsWindow(parent=None, registry=_FakeContrib([]))
    qtbot.add_widget(win)

    assert win.select_page("维护") is True
    page = win._stack.currentWidget().widget()
    texts = [lbl.text() for lbl in page.findChildren(QLabel)]
    assert "开机自启" in texts and "权限" in texts and "数据" in texts
    buttons = [b.text() for b in page.findChildren(QPushButton)]
    assert buttons.count("打开数据目录") == 1, "全局动作只该有一处落点"

    # 「开机自启」「权限」不再各占一页
    labels = [win._nav.item(i).text() for i in range(win._nav.count())]
    assert "开机自启" not in labels and "权限" not in labels


def test_data_slot_is_wired_into_the_maintenance_card(qtbot, monkeypatch):
    """「数据」卡自带一条槽，打开数据目录的结果落在**这张卡内**。"""
    win = SettingsWindow(parent=None, registry=_FakeContrib([]))
    qtbot.add_widget(win)
    win.select_page("维护")
    fb = win._data_feedback
    assert isinstance(fb, FeedbackBar)

    monkeypatch.setattr(
        "barkit.shell.data_ops.run_cmd",
        lambda cmd, timeout=10: type("R", (), {"returncode": 1, "stderr": "x"})(),
    )
    page = win._stack.currentWidget().widget()
    for b in page.findChildren(QPushButton):
        if b.text() == "打开数据目录":
            b.click()
    assert "打开数据目录" in fb.text()


def test_forget_data_feedback_only_clears_its_own_slot(qtbot):
    """页面重建：旧槽位的销毁回调不得把**新**槽位一起摘掉。"""
    win = SettingsWindow(parent=None, registry=_FakeContrib([]))
    qtbot.add_widget(win)
    old_fb = win._data_feedback
    fresh = win._page_maintenance()           # 重建（旧页此时尚未销毁）
    qtbot.add_widget(fresh)
    new_fb = win._data_feedback

    assert new_fb is not old_fb
    win._forget_data_feedback(old_fb)
    assert win._data_feedback is new_fb
    win._forget_data_feedback(new_fb)
    assert win._data_feedback is None


def test_plugin_settings_page_is_wrapped_in_a_scroll_area(qtbot):
    """所有分页（含框架页与插件贡献页）统一包进滚动区。

    窗口主区高度固定（380px），而页面内容高度由页面自己决定：route_guard 那页实测要约
    915px、插件列表约 426px —— 不外包滚动区，下半页会连同按钮/手柄一起被静默裁掉（用户
    2026-09-18 报「控件显示不全、没有滚动条、排布很乱」）。2026-09-20 UI 统一重构起改为
    **所有分页统一包一层** ``_scrolled``（NoFrame ＋ 关横向滚动 ＋ widgetResizable），
    各页内部不再自建滚动区（避免滚动套滚动）。
    """
    from PySide6.QtWidgets import QScrollArea

    content = QLabel("插件页内容")
    plugin_page = SettingsPageSpec(
        label="演示页", build=lambda: content, reason="合规样本：动作入口页",
    )
    win = SettingsWindow(parent=None, registry=_FakeContrib([plugin_page]))
    qtbot.add_widget(win)

    labels = [win._nav.item(i).text() for i in range(win._nav.count())]

    area = win._stack.widget(labels.index("演示页"))
    assert isinstance(area, QScrollArea), "插件页必须外包滚动区"
    assert area.widget() is content, "包的是插件自己交出来的那一个 widget"
    assert area.widgetResizable() is True, "随窗口拉伸，不要出现横向滚动条"

    general = win._stack.widget(labels.index("通用"))
    assert isinstance(general, QScrollArea), "框架页同样统一包滚动区"
    assert general.widgetResizable() is True


def test_autostart_card_owns_a_feedback_slot_wired_into_the_row(qtbot):
    """「开机自启」卡有**一条**行内槽，且已注入自启行 —— 本卡零模态的落点。

    改前这页的 5 处提示全是 ``QMessageBox``（且父窗口为 ``None``）；设置窗钉在原生
    层级 201，从它内部弹模态框会另开一条嵌套事件循环（证据见
    ``tests/test_architecture.py`` 零模态门禁的 docstring）。
    """
    win = SettingsWindow(parent=None, registry=_FakeContrib([]))
    qtbot.add_widget(win)

    assert win.select_page("维护") is True
    fb = win._autostart_feedback
    assert fb is not None
    row = win._stack.currentWidget().findChild(AutostartRow)
    assert row is not None
    # 槽是同一个，不是行内各建一份。2026-10-16 起行内改由 `FeedbackSlot` 持有它
    # （`autostart_row.py`），故经 `slot.bar` 取 —— 判据本身没变。
    assert row._slot.bar is fb


def test_autostart_row_has_no_title_of_its_own(qtbot):
    """自启行**不再自带**「开机自启」标题 —— 标题由所在那张卡给出，写了就是两行同名。"""
    win = SettingsWindow(parent=None, registry=_FakeContrib([]))
    qtbot.add_widget(win)
    win.select_page("维护")
    row = win._stack.currentWidget().findChild(AutostartRow)
    assert row is not None
    assert "开机自启" not in [lbl.text() for lbl in row.findChildren(QLabel)]


def test_select_page_by_label_and_about_target(qtbot):
    """``select_page`` 按**标签**切页，查无此页时不动。

    按标签而非下标：插件页增删会让下标漂移（同一个理由在 ``rebuild_pages`` 恢复当前页
    时也成立 —— 那处现在直接调本方法，不再就地再写一遍查找）。
    """
    win = SettingsWindow(parent=None, registry=_FakeContrib([]))
    qtbot.add_widget(win)

    assert win.select_page("关于") is True
    assert win._nav.currentItem().text() == "关于"
    assert win._stack.currentIndex() == win._nav.currentRow()

    # 查无此页：保持当前页、返回 False（不抛异常地把设置窗弄坏）
    assert win.select_page("没有这一页") is False
    assert win._nav.currentItem().text() == "关于"


def test_show_and_raise_only_shows_and_keeps_the_current_page(qtbot):
    """``show_and_raise`` 只做「显示 + 定位」，**没有**「打开哪一页」这个参数。

    §5 #26（2026-09-18 用户裁定）：那个 ``page`` 参数是给面板「? 帮助」用的，而那枚
    按钮已按用户裁定删除（与「设置」开同一个窗、只差一次点击）⇒ 参数在**生产路径**上
    再无调用方（``panel.settingsRequested`` 不带页）。留着它＝留着一条没人走的岔路；
    本用例把口径钉住：开窗**不**顺带切页，要切页另有 :meth:`select_page`。
    """
    win = SettingsWindow(parent=None, registry=_FakeContrib([]))
    qtbot.add_widget(win)

    assert win.select_page("维护") is True

    win.show_and_raise()

    assert win.isVisible()
    assert win._nav.currentItem().text() == "维护"


# ---------- 「维护」页的「权限」卡：行内反馈槽 ＋ 引导语（2026-09-17 零模态收口） ----------


def test_permissions_card_owns_a_feedback_slot(qtbot):
    """「权限」卡有**一条**行内槽 —— 本卡零模态的落点。

    改前那颗「检查辅助功能权限」按钮直达 ``core/permissions.ensure_accessibility``，
    它内部是 ``QMessageBox(parent).exec()``：设置窗钉在原生层级 201，从它内部弹模态框
    会另开一条嵌套事件循环（证据见 ``tests/test_architecture.py`` 零模态门禁的
    docstring），用户看到的是「点了按钮，窗口不动」。
    """
    win = SettingsWindow(parent=None, registry=_FakeContrib([]))
    qtbot.add_widget(win)

    assert win.select_page("维护") is True
    fb = win._permissions_feedback
    assert isinstance(fb, FeedbackBar)     # 用的是共用控件，不是本卡另造一个


def test_check_accessibility_reports_each_case_into_the_slot(qtbot, monkeypatch):
    """三种情形各报各的，且都落在**本页**槽里（不再有弹框）。

    「问不出来」与「没授权」必须分得开：本机没有 ApplicationServices 时报「未授予」，
    等于把用户支去系统设置里白翻一遍列表。
    """
    win = SettingsWindow(parent=None, registry=_FakeContrib([]))
    qtbot.add_widget(win)
    win.select_page("维护")
    fb = win._permissions_feedback

    monkeypatch.setattr(settings_maintenance, "available", lambda: True)
    monkeypatch.setattr(settings_maintenance, "accessibility_trusted", lambda: True)
    win._check_accessibility()
    assert "已授予" in fb.text()

    monkeypatch.setattr(settings_maintenance, "accessibility_trusted", lambda: False)
    win._check_accessibility()
    assert fb.text() == settings.ACCESSIBILITY_GUIDE

    monkeypatch.setattr(settings_maintenance, "available", lambda: False)
    win._check_accessibility()
    assert "ApplicationServices" in fb.text()
    assert fb.text() != settings.ACCESSIBILITY_GUIDE


def test_permission_actions_are_plain_page_buttons(qtbot, monkeypatch):
    """两个动作是页面上的普通按钮：点了直接执行，不经过任何弹框。

    原先它们是对话框里的按钮 —— 框被置顶的设置窗压住，按钮**看得到却点不动**。
    """
    calls: list[str] = []
    monkeypatch.setattr(settings_maintenance, "open_accessibility_pane", lambda: calls.append("open"))
    monkeypatch.setattr(settings_maintenance, "request_accessibility", lambda: calls.append("prompt"))
    monkeypatch.setattr(settings_maintenance, "available", lambda: True)
    monkeypatch.setattr(settings_maintenance, "accessibility_trusted", lambda: False)

    win = SettingsWindow(parent=None, registry=_FakeContrib([]))
    qtbot.add_widget(win)
    win.select_page("维护")

    win._open_accessibility_pane()
    win._request_accessibility()
    assert calls == ["open", "prompt"]
    # 复探：系统那次弹窗本身就是一次授权机会，点完当场把状态再问一遍
    assert win._permissions_feedback.text() == settings.ACCESSIBILITY_GUIDE


def test_forget_permissions_feedback_only_clears_its_own_slot(qtbot):
    """页面重建：旧槽位的销毁回调不得把**新**槽位一起摘掉。"""
    win = SettingsWindow(parent=None, registry=_FakeContrib([]))
    qtbot.add_widget(win)
    old_fb = win._permissions_feedback
    fresh = win._page_maintenance()           # 重建（旧页此时尚未销毁）
    qtbot.add_widget(fresh)
    new_fb = win._permissions_feedback

    assert new_fb is not old_fb
    win._forget_permissions_feedback(old_fb)
    assert win._permissions_feedback is new_fb, "旧槽位的销毁回调不应摘掉新槽位"
    win._forget_permissions_feedback(new_fb)
    assert win._permissions_feedback is None


# ---------- 组件页页尾「框架区」：运行间隔（契约 D21：控件由框架自动生成） ----------
#
# 位置口径（2026-09-21 第二批）：框架给组件画的三样里，**显示位芯片留在「插件」页**
# （四枚格子只有在这一页才横向比得出来），「运行间隔」与「恢复默认配置」**下沉到组件
# 自己的设置页页尾**。故本组用例的宿主从「插件」页那一行换成了组件页的框架区。


def _component_page(plugin_id, label=None, *, tail_stretch=True):
    """造一个**标准外壳**的组件设置页（框架区才有位可落）。

    ``tail_stretch`` 复刻两种收尾：四个真组件页以 ``addStretch(1)`` 收尾、
    ``notify_guard`` 没有 —— 框架区在两种页里都必须落在页尾。
    """
    def build():
        page, lay = settings_page_shell(label or plugin_id)
        lay.addWidget(QLabel("组件自己的内容"))
        if tail_stretch:
            lay.addStretch(1)
        return page

    return SettingsPageSpec(
        plugin_id=plugin_id, label=label or plugin_id, build=build, reason="用例桩",
    )


def _registry_for(order):
    """给 ``order`` 里每个 id 各贡献一个组件设置页。"""
    return _FakeContrib([_component_page(pid) for pid in order])


def _page_widget(win, label):
    labels = [win._nav.item(i).text() for i in range(win._nav.count())]
    return _scrolled_widget(win._stack.widget(labels.index(label)))


def _zone(win, plugin_id):
    """该组件页页尾的**框架区**（＝它那条反馈槽的父亲）；这一页没有区时返回 ``None``。

    不按坐标 / 类名去找，而是顺着「框架区自带那条槽」反查 —— 那正是「一区一条」这个
    契约本身，找出来的必然是被测的那个区。
    """
    fb = win._zone_feedback.get(plugin_id)
    return None if fb is None else fb.parentWidget()


def _zone_card_titles(win, plugin_id):
    """该组件页框架区里各张卡的标题（按版面顺序；没有区 ⇒ 空列表）。

    按**标题**断言而不是按控件类型数：「哪几张卡出、按什么顺序」正是这一区当契约守的
    东西，标题是它对外的稳定标识（卡内控件会随实现换）。
    """
    zone = _zone(win, plugin_id)
    if zone is None:
        return []
    return [h.title_text for h in zone.findChildren(_SectionHeader)]


def _poll_selector(win, plugin_id):
    """取该组件页框架区里的「运行间隔」下拉；按需型组件没有（返回 ``None``）。"""
    zone = _zone(win, plugin_id)
    if zone is None:
        return None
    sels = zone.findChildren(ChoiceSelector)
    return sels[0] if sels else None


def _poll_combo(win, plugin_id):
    sel = _poll_selector(win, plugin_id)
    return sel._combo if sel is not None else None


def _component_window(order, *, poll_defaults=None, polls=None,
                      clearable=None, removed=None):
    """带组件页的设置窗桩（``order`` 里每个 id 一页）。

    「运行间隔」与「恢复默认配置」搬到组件页之后，用例宿主也得搬 —— 没有页就等于
    没有落点。真实仓里周期型组件与有设置页的组件今天是同一批，故这个桩不虚构情形。
    """
    cfg = _ResetConfig(clearable)
    mgr = _ResetManager(
        order, config=cfg, removed=removed,
        poll_defaults=poll_defaults, polls=polls,
    )
    return SettingsWindow(
        parent=None, registry=_registry_for(order), manager=mgr, config=cfg,
    )


def _poll_window(order, **kw):
    return _component_window(order, **kw)


def test_poll_choices_keep_the_default_item_and_drop_its_twin(qtbot):
    """候选构造的纯逻辑：``[默认（X 秒）] ＋ 阶梯 − {与默认同值那档} ＋ 离梯当前值``。

    去掉与默认同值的那一档是**防残渣**：选「30 秒」与选「默认（30 秒）」落盘是
    「写了覆盖」与「没写覆盖」两种配置，同一个数字两种状态，日后分了叉也看不出。
    """
    choices = settings._poll_choices(None, 30.0)
    assert choices[0] == ("默认（30 秒）", "")
    assert ("30 秒", "30") not in choices, "与默认同值的那一档必须去掉"
    assert ("60 秒", "60") in choices
    assert ("1 秒", "1") in choices

    odd = settings._poll_choices(7.0, 30.0)                  # 手改过 YAML 的离梯值
    assert ("7 秒", "7") in odd, "离梯的当前值要如实列出来，不许显示成「默认」"
    assert settings._poll_choices(30.0, 30.0) == choices, "等于默认的覆盖不算离梯值"


def test_interval_card_lives_on_the_component_page_and_only_for_periodic(qtbot):
    """准入只有一条：``poll_default`` 非空（＝周期型）。按需型不长这张卡。

    控件文案是「框架检查间隔」而不是契约里的「框架节拍」（用户看不懂「节拍」）；
    **也不能与组件自己的节律混**：搬到组件页之后这一行与组件自己的间隔设置同页相邻，
    故卡的标题写「运行间隔」、行标签写「框架检查间隔」、下面还有一句划界线的说明。
    """
    win = _poll_window(["poller", "demo"], poll_defaults={"poller": 30.0})
    qtbot.add_widget(win)

    sel = _poll_selector(win, "poller")
    assert sel is not None, "周期型组件：组件页页尾长「运行间隔」卡"
    combo = sel._combo
    labels = [combo.itemText(i) for i in range(combo.count())]
    assert labels[0] == "默认（30 秒）" and combo.itemData(0) == ""
    assert combo.currentData() == "", "没改过 → 选中的是默认那一项"
    assert _poll_selector(win, "demo") is None, "按需型：没有节拍可言"
    assert "组件建议 30 秒" in sel.toolTip()
    assert "立即生效" in sel.toolTip()


def test_interval_card_line_says_it_is_the_framework_check_not_the_component_beats(qtbot):
    """这一行必须说清「框架每隔多久叫一次」，否则用户会问「为什么有两个间隔」。

    网络切换的「helper 巡逻节律」、hosts 托管的「自动更新间隔」与这一行搬到同一页后
    是紧邻的，界面上唯一能区分它们的就是这一句。
    """
    win = _poll_window(["poller"], poll_defaults={"poller": 30.0})
    qtbot.add_widget(win)

    zone = _zone(win, "poller")
    texts = [lbl.text() for lbl in zone.findChildren(QLabel)]
    assert "运行间隔" in texts, "卡的标题"
    assert "框架检查间隔" in texts, "行的标签（不是「刷新间隔」）"
    assert any("组件自己那套节律见上面的卡" in t for t in texts), "划界线的说明"


def test_plugin_row_keeps_only_the_slot_chips(qtbot):
    """「插件」页那一行**只剩显示位芯片**：节拍与清配置都不在这一行了。

    留芯片的理由不是省事：四枚格子只有在这一页才横向比得出来（谁占了哪几格），
    而节拍与清配置是单个组件的事，在管理列表里看不出任何别的东西。
    """
    win = _poll_window(
        ["poller", "dirty"], poll_defaults={"poller": 30.0},
        clearable={"dirty": ["profiles"]},
    )
    qtbot.add_widget(win)

    row = win._order_editor._rows["poller"]
    assert row.findChildren(ChoiceSelector) == [], "运行间隔下拉已经不在插件行上"
    buttons = [b.text() for b in row.findChildren(QPushButton)]
    assert "恢复默认配置" not in buttons, "恢复默认配置已经不在插件行上"
    # 反过来：两样东西确实都还在（在组件页上）—— 不然这条用例会被「两个都删了」骗过
    assert _poll_selector(win, "poller") is not None
    assert _reset_button(win, "dirty") is not None


def test_interval_selector_writes_and_restores_the_default(qtbot):
    """选一档 → 当场落盘；选「默认」→ 送 ``None``（删键，不是写回默认值）。

    回报落在**这一区自己那条槽**上（不是「插件」页的槽）：按钮长在页尾，消息必须
    出现在手边。
    """
    win = _poll_window(["poller"], poll_defaults={"poller": 30.0})
    qtbot.add_widget(win)
    mgr = win._manager
    combo = _poll_combo(win, "poller")

    combo.setCurrentIndex(combo.findData("60"))
    assert mgr.poll_writes == [("poller", 60.0)]
    fb = win._zone_feedback["poller"]
    assert "改为 60 秒" in fb.text()
    assert "立即生效" in fb.text()

    combo.setCurrentIndex(0)                        # 「默认（30 秒）」
    assert mgr.poll_writes[-1] == ("poller", None)
    assert "恢复为组件建议的 30 秒" in fb.text()


def test_interval_selector_shows_the_override_and_its_tooltip(qtbot):
    """用户改过 → 选中的是他那个值（不是「默认」），提示里说明「是你改过的」。"""
    win = _poll_window(
        ["poller"], poll_defaults={"poller": 30.0}, polls={"poller": 7.0}
    )
    qtbot.add_widget(win)
    combo = _poll_combo(win, "poller")
    assert combo.currentData() == "7" and combo.currentText() == "7 秒"
    assert "当前 7 秒是你改过的" in _poll_selector(win, "poller").toolTip()


# ---------- 组件页页尾「框架区」：恢复默认配置（框架统一提供，2026-09-18） ----------
#
# 用户诉求：配置段里留着一个新版程序不认识的旧键时，**界面上看不见它** ⇒ 只能手改
# YAML。这里钉住框架那个统一入口 —— 准入、确认问句、清完之后的说法。
#
# 位置（2026-09-21 第二批）：卡片下沉到组件自己的设置页页尾；问句落在**该区自带**的
# 那条反馈槽上（这一页原有的槽在页首，长页里用户在页尾够不着它）。


def _reset_button(win, plugin_id):
    """取该组件页框架区里的「恢复默认配置」按钮；没改过默认配置的组件不长它。"""
    zone = _zone(win, plugin_id)
    if zone is None:
        return None
    found = [b for b in zone.findChildren(QPushButton) if b.text() == "恢复默认配置"]
    return found[0] if found else None


class _ResetConfig(_FakeConfig):
    """带「可清键」的配置桩：``clearable`` 回答该组件有哪些键会被清掉。

    ``cleared`` 模拟**清完之后**的样子 —— 真配置清完就没有可清的键了。
    这正是「按钮当场消失」那条断言的依据（按钮在不在 ≠ 装饰，它是「还有没有改动」）。
    """

    def __init__(self, clearable=None):
        super().__init__()
        self._clearable = dict(clearable or {})
        self.cleared: set[str] = set()

    def clearable_plugin_keys(self, plugin_id):
        if plugin_id in self.cleared:
            return []
        return list(self._clearable.get(plugin_id, []))


class _ResetManager(_OrderManager):
    def __init__(self, order, config=None, removed=None, **kw):
        super().__init__(order, **kw)
        self._config = config
        self._removed = dict(removed or {})
        self.reset_calls: list[str] = []
        self.fail_reset = False

    def reset_plugin_config(self, plugin_id):
        self.reset_calls.append(plugin_id)
        if self.fail_reset:
            raise OSError("磁盘满")
        removed = list(self._removed.get(plugin_id, []))
        if self._config is not None and removed:
            self._config.cleared.add(plugin_id)
        return removed


def _reset_window(order, clearable=None, removed=None):
    return _component_window(order, clearable=clearable, removed=removed)


# ---------- 装置运维卡（框架按**声明**注入）的桩 ----------


class _HelperResource:
    """``resources()`` 的最小返回：只带「用 root helper」那两位声明。"""

    def __init__(self, declared):
        self.helper_tasks = ["route"] if declared else []
        self.needs_root = declared


class _DeclaringPlugin:
    """带 **root helper 声明**的组件桩（框架据此在页尾框架区画装置运维卡）。

    ``declared=False`` 用来做反向：清掉声明 ⇒ 那一块整卡退场。
    """

    def __init__(self, *, declared=True, note="", notice=("", False)):
        self.declared = declared
        self._note = note
        self._notice = notice
        self.calls: list[str] = []
        self.attached = None
        self.refreshed = 0

    def resources(self):
        return _HelperResource(self.declared)

    def helper_ops_texts(self):
        from barkit.capabilities.helper_ops import HelperOpsTexts

        return HelperOpsTexts(note=self._note)

    def helper_notice(self):
        return self._notice

    def install_action(self):
        self.calls.append("install")

    def uninstall_action(self):
        self.calls.append("uninstall")

    def immediate_check(self):
        self.calls.append("check")

    def attach_helper_ops(self, widget, feedback, refresh):
        self.attached = (widget, feedback, refresh)

    def refresh_helper_notice(self):
        self.refreshed += 1


class _HelperManager(_ResetManager):
    """``_ResetManager`` ＋ ``plugin_instance`` —— 框架画装置运维卡时要问它拿实例。"""

    def __init__(self, order, *, plugins=None, **kw):
        super().__init__(order, **kw)
        self._plugins = dict(plugins or {})

    def plugin_instance(self, pid):
        return self._plugins.get(pid)


def _helper_window(order, plugins, *, clearable=None, removed=None,
                   poll_defaults=None, polls=None):
    """带组件页的设置窗桩，且 ``plugin_instance`` 答得出「用哪个 helper 组件」。"""
    cfg = _ResetConfig(clearable)
    mgr = _HelperManager(
        order, plugins=plugins, config=cfg, removed=removed,
        poll_defaults=poll_defaults, polls=polls,
    )
    return SettingsWindow(
        parent=None, registry=_registry_for(order), manager=mgr, config=cfg,
    )


def test_reset_button_appears_only_for_components_the_user_configured(qtbot):
    """准入 = 用户差异段里真有可清的键。

    没改过默认值的组件不长这张卡（点了也只会说「本来就是默认」，白占版面）；
    配置真被写脏的组件 —— 正是最需要它的那种 —— 必定看得见它。
    """
    win = _reset_window(
        ["dirty", "clean"],
        clearable={"dirty": ["profiles", "rule_overrides"]},
        removed={"dirty": ["profiles", "rule_overrides"]},
    )
    qtbot.add_widget(win)

    btn = _reset_button(win, "dirty")
    assert btn is not None
    assert _reset_button(win, "clean") is None, "没改过默认值的组件不长这张卡"
    tip = btn.toolTip()
    assert "profiles" in tip and "rule_overrides" in tip, "提示要逐条列出将清掉的键"
    assert "保留「启用」开关" in tip


def test_reset_asks_inline_and_clears_only_after_confirm(qtbot):
    """**先在行内问**：答之前一个字都不清，点下「恢复默认」才真清（零模态）。

    问句落在**组件页框架区**那条槽上：按钮长在页尾，长页里页面原有的槽（页首）
    够不着，用户会以为点了没反应。
    """
    win = _reset_window(
        ["dirty"], clearable={"dirty": ["profiles", "rule_overrides"]},
        removed={"dirty": ["profiles", "rule_overrides"]},
    )
    qtbot.add_widget(win)
    mgr = win._manager

    _reset_button(win, "dirty").click()

    assert mgr.reset_calls == []
    fb = win._zone_feedback["dirty"]
    assert fb.is_pending()                         # 正等用户点「恢复默认」
    assert "这 2 项" in fb.text()
    assert "profiles" in fb.text() and "rule_overrides" in fb.text()
    assert "不能撤销" in fb.text()                  # 破坏性后果必须写在问句里

    fb.accept()

    assert mgr.reset_calls == ["dirty"]
    assert fb.is_pending() is False
    assert "已恢复默认" in fb.text() and "profiles" in fb.text()


def test_reset_cancelled_clears_nothing(qtbot):
    win = _reset_window(
        ["dirty"], clearable={"dirty": ["profiles"]}, removed={"dirty": ["profiles"]}
    )
    qtbot.add_widget(win)
    mgr = win._manager

    _reset_button(win, "dirty").click()
    win._zone_feedback["dirty"].dismiss()          # 用户点「取消」

    assert mgr.reset_calls == []
    assert win._zone_feedback["dirty"].is_pending() is False


def test_reset_without_feedback_slot_is_refused(qtbot):
    """没有确认口 ＝ 没人确认：这一步不可撤销、也没有备份，故拒绝执行。"""
    win = _reset_window(
        ["dirty"], clearable={"dirty": ["profiles"]}, removed={"dirty": ["profiles"]}
    )
    qtbot.add_widget(win)
    win._zone_feedback.pop("dirty", None)          # 槽位不可用

    win._on_plugin_reset("dirty", "DIRTY")

    assert win._manager.reset_calls == []


def test_reset_makes_the_card_disappear_and_keeps_the_message(qtbot):
    """清完这张卡当场消失（它本身就是「还有没有改动」的显示位），**消息留在原槽上**。

    清卡走的是**就地重算**（``_refresh_zone``）而不是整页重建：重建会把这条槽一起换掉，
    消息就随旧页蒸发；也会连带拆掉别的组件页（可能正开着草稿）。
    """
    win = _reset_window(
        ["dirty"], clearable={"dirty": ["profiles"]}, removed={"dirty": ["profiles"]}
    )
    qtbot.add_widget(win)
    fb = win._zone_feedback["dirty"]

    _reset_button(win, "dirty").click()
    fb.accept()

    assert _reset_button(win, "dirty") is None
    assert fb.is_pending() is False
    assert "已恢复默认" in fb.text(), "槽位没被重建，消息还在"


def test_reset_failure_reports_the_reason_not_a_dialog(qtbot):
    """写盘失败当场报错，**不假装成功** —— 「界面说清了、盘上没动」是最不能出的错。"""
    win = _reset_window(
        ["dirty"], clearable={"dirty": ["profiles"]}, removed={"dirty": ["profiles"]}
    )
    qtbot.add_widget(win)
    win._manager.fail_reset = True

    _reset_button(win, "dirty").click()
    win._zone_feedback["dirty"].accept()

    assert "恢复默认失败" in win._zone_feedback["dirty"].text()
    assert "磁盘满" in win._zone_feedback["dirty"].text()


def test_reset_on_a_pristine_component_says_so_without_asking(qtbot):
    """没东西可清时不弹问句、也不动盘：直接说「本来就是默认配置」。

    这样的组件页尾**没有「配置维护」卡**（没有可清的键就不出那张卡）：界面上本来也
    没有能触发它的按钮 —— 只有直接调 ``_on_plugin_reset``（如本用例）才会走到这条路，
    故那句话只落到日志里。
    """
    win = _reset_window(["clean"], clearable={})
    qtbot.add_widget(win)

    assert "配置维护" not in _zone_card_titles(win, "clean"), "没有可清的键 ⇒ 不出那张卡"
    win._on_plugin_reset("clean", "CLEAN")

    assert win._manager.reset_calls == []


# ---------- 框架区的位置与卡序（第二批立的契约） ----------


def _layout_children(lay):
    """按顺序列出布局里每一项的 widget（``None`` ＝ stretch 之类的 spacer）。"""
    out = []
    for i in range(lay.count()):
        item = lay.itemAt(i)
        out.append(item.widget() if item is not None else None)
    return out


def test_framework_zone_sits_after_the_content_and_before_the_tail_stretch(qtbot):
    """页尾区必须**在组件内容之后、页尾 stretch 之前** —— 两种收尾的页都得是。

    四个真组件页以 ``addStretch(1)`` 收尾、``notify_guard`` 没有。不跳过 stretch 的话，
    有 stretch 的页里框架区会长在**页面中部**、没 stretch 的页里长在最底下 —— 同一个
    东西每页位置不同，正是这一批要消掉的东西。
    """
    for tail_stretch in (True, False):
        win = SettingsWindow(
            parent=None,
            registry=_FakeContrib([_component_page("poller", tail_stretch=tail_stretch)]),
            manager=_OrderManager(["poller"], poll_defaults={"poller": 30.0}),
            config=_FakeConfig(),
        )
        qtbot.add_widget(win)
        zone = _zone(win, "poller")
        assert zone is not None, f"tail_stretch={tail_stretch}：这一页本该有框架区"
        kids = _layout_children(_page_widget(win, "poller").layout())
        last_widget = max(i for i, w in enumerate(kids) if w is not None)
        assert kids.index(zone) == last_widget, (
            f"tail_stretch={tail_stretch}：框架区必须是最后一个**控件**（在页尾 stretch 之前）"
        )
        if tail_stretch:
            assert kids[-1] is None, "页尾 stretch 仍然收尾，框架区排在它之前"


def test_framework_zone_card_order_is_fixed(qtbot):
    """卡序当契约：**运行间隔 → 配置维护 → 装置运维**。

    准入各自独立 ⇒ **卡可缺、序不变**：少一张可以，次序不许各页自排 —— 否则「同一个
    东西每页位置不同」只是换了个地方继续。

    「数据与诊断」**已不在这里**（2026-09-22 撤）：它把「全局的数据目录」与「组件自己的
    诊断」捆成一卡，前者因此被复制进每个组件页。前者归「维护」页，后者归组件自己那张卡。

    第三批（2026-09-28）在末尾补上**装置运维**：只对**声明了 root helper 依赖**的组件
    出这一张（见 ``test_helper_ops_card_injected_from_declaration``），故这里用带声明的桩。
    """
    plugin = _DeclaringPlugin()
    win = _helper_window(
        ["ops"], {"ops": plugin},
        poll_defaults={"ops": 30.0}, clearable={"ops": ["profiles"]},
    )
    qtbot.add_widget(win)
    assert _zone_card_titles(win, "ops") == ["运行间隔", "配置维护", "装置运维"]

    # 卡可缺、序不变：没声明 helper 的组件照旧只有前两张
    win2 = _component_window(
        ["poller"], poll_defaults={"poller": 30.0}, clearable={"poller": ["profiles"]},
    )
    qtbot.add_widget(win2)
    assert _zone_card_titles(win2, "poller") == ["运行间隔", "配置维护"]


def test_helper_ops_card_injected_from_declaration(qtbot):
    """装置运维卡由**声明**决定画不画（框架注入，组件一行不写）。

    准入＝``resources().helper_tasks`` 非空（或 ``needs_root``）—— 与
    ``helper_tasks.json`` **同一个真源**。此前「谁用 helper」有两个真源：声明 ＋ 组件
    手写的接线，第二个必然漂（hosts 托管正是漂掉的那个：没接上共用块、警示另立一套）。
    """
    declaring = _DeclaringPlugin(note="「立即检查」让守护立刻重跑一轮。")
    win = _helper_window(["ops"], {"ops": declaring})
    qtbot.add_widget(win)

    assert "装置运维" in _zone_card_titles(win, "ops")
    zone = _zone(win, "ops")
    labels = {b.text() for b in zone.findChildren(QPushButton)}
    assert {"立即检查", "安装守护", "卸载守护"} <= labels, labels
    assert declaring.attached is not None, "卡建好就把反馈槽与重算回调交给插件"
    card, fb, _refresh = declaring.attached
    # 反馈收在**卡内**（2026-09-30 用户要求）：不再是页尾区那条共用槽，而是这张卡
    # 自己的槽 —— 触发它的按钮就在卡里。
    assert fb is not win._zone_feedback["ops"], "装置运维的反馈不借页尾区共用槽"
    assert card.isAncestorOf(fb), "反馈槽长在装置运维卡内部"

    # 反向：清掉声明 ⇒ 同一页不再长出这一块
    plain = _DeclaringPlugin(declared=False)
    win2 = _helper_window(["ops"], {"ops": plain})
    qtbot.add_widget(win2)
    assert "装置运维" not in _zone_card_titles(win2, "ops")
    assert plain.attached is None, "没声明 ⇒ 连接线都不该发生"


def test_refresh_helper_notices_recomputes_each_helper_plugin(qtbot):
    """守护连接 / 版本变化 → 逐插件重算装置运维警示。

    走**公开的** ``refresh_helper_notice()``（它内部先重拉状态、再重算），
    不是戳插件私有方法（2026-09-30 复评）。
    """
    declaring = _DeclaringPlugin()
    win = _helper_window(["ops"], {"ops": declaring})
    qtbot.add_widget(win)

    win.refresh_helper_notices()

    assert declaring.refreshed == 1, "必须调插件的公开入口 refresh_helper_notice()"


def test_daemon_ops_reinstall_key_follows_the_status(qtbot):
    """装置运维卡里那几枚键的收放：警示文案与「重装守护」都由 ``status()`` 的元组决定。

    「重装守护」只在「装了但版本过期」时出现：没装时用户该点的是旁边的「安装守护」，
    两个键同时出现会让人以为要先重装才能装；正常态（空文案）**整块警示退场**。

    这块卡由框架按声明注入（``shell/settings.py``），故这里按框架的用法直接调
    ``shell.daemon_ops.build_daemon_ops`` —— 判**可见性**而不是「树里有没有这枚按钮」：
    它恒在，靠 ``setVisible`` 收放。
    """
    from barkit.shell import daemon_ops as ops

    cases = (
        (("系统守护尚未安装。", False), False),   # 未装：只提示安装
        (("守护版本过期。", True), True),          # 过期：多一个「重装守护」
        (("", False), False),                      # 正常：警示整块隐藏
    )
    for notice, expect_reinstall in cases:
        holder = QWidget()
        qtbot.add_widget(holder)
        block, _refresh = ops.build_daemon_ops(
            holder, install=lambda: None, uninstall=lambda: None,
            immediate_check=lambda: None, status=lambda n=notice: n,
        )
        reinstall = next(
            b for b in block.findChildren(QPushButton) if b.text() == "重装守护"
        )
        assert reinstall.isVisibleTo(block) is expect_reinstall, notice
        warn_box = reinstall.parentWidget()
        assert warn_box.findChildren(QLabel)[0].text() == notice[0], notice
        assert warn_box.isVisibleTo(block) is bool(notice[0]), (
            notice, "正常态（空文案）整块警示退场"
        )


def test_feedback_bar_tones_are_semantic_and_distinct(qtbot):
    """四档反馈条各走各的语义色：成功绿 / 中性告知 / 注意橙 / 失败红。

    2026-09-30 之前这条槽只设 ``color`` ＋ ``font-size:12px``：与旁边的静态说明
    （``note()``）长得一模一样，用户读不出来「这里有件事刚发生」（原话：「操作后的反馈
    太不显眼了」）；而成功档用的还是中性灰，比报错更不显眼。

    这里钉两件事：① 它是个**提示条**（语义浅底 ＋ 左侧一道色条），② 四档的浅底与
    tokens 的浅底档一一对应、且互不相同 —— 写死颜色有 ``test_no_hardcoded_colors`` 抓，
    但「挂错了令牌」（如成功挂成中性档）只有这条能抓。
    """
    from barkit.shell import styles
    from barkit.shell.design.tokens import TOKENS

    bar = FeedbackBar()
    qtbot.add_widget(bar)
    tk = TOKENS[styles.current_theme()]
    seen: dict[str, str] = {}
    for call, soft in (("info", "green_soft"), ("hint", "surface3"),
                       ("warn", "orange_soft"), ("error", "red_soft")):
        getattr(bar, call)("探针")
        css = bar._box.styleSheet().lower()
        assert "border-left:3px solid" in css, f"{call} 档没画左侧色条：{css}"
        assert f"background:{tk[soft].lower()}" in css, (call, soft, css)
        seen[call] = css
    assert len(set(seen.values())) == 4, f"四档外观该互不相同：{seen}"
    assert bar.text() == "探针", "判据只该改外观，不该动那一句话"


def _fb_box_rule_violation(bar: FeedbackBar) -> str | None:
    """盒子那条样式规则是否**会漏到子控件**：违规时返回选择器，合规返回 None。

    选择器必须是**类选择器**（``.QFrame``）或 **ID 选择器**（``#…``）。裸 `QFrame` 这种
    **类型选择器**按 Qt 的规矩连**子类**一起命中 —— 而 ``QLabel`` 恰恰继承自 ``QFrame``，
    于是盒子的「浅底 ＋ 左色条」会同时烤到行首图标与文字两个子标签上，反馈条长成
    **三道条**（2026-09-30 真机：最左 ＋ 图标处 ＋ 文字左侧）。实测三种选择器的命中面：
    ``QFrame`` 命中 QFrame / QLabel / 自定义子类；``.QFrame`` 只命中 QFrame 实例；
    ``.自定义子类`` 只命中该子类。
    """
    selector = bar._box.styleSheet().split("{", 1)[0].strip()
    return None if selector.startswith((".", "#")) else selector


def test_feedback_bar_rule_cannot_cascade_into_its_children(qtbot):
    """反馈条只许有**一道**左色条：盒子的规则不得把子控件也画上。

    判据落在**选择器**上，不落在「渲染出来有几道条」—— 渲染判据在这里数不清：行首图标
    用的就是同一个图形档颜色，字形像素与色条同色（``_TONES`` 里色条与图标共用一个 key）。
    """
    bar = FeedbackBar()
    qtbot.add_widget(bar)
    #: 档名 → 写它的那个方法（``ask`` 要额外给确认位与回调）
    for call in ("info", "hint", "warn", "error", "ask"):
        if call == "ask":
            bar.ask("探针？", "确定", lambda: None)
        else:
            getattr(bar, call)("探针")
        # 档位是**能读出来的**（`tone()` 只读口）：调用方按语义选档，选错了光看 `text()`
        # 一个字都看不出来 —— 2026-10-16 那条「先勾一项」拿 info（绿）说，就是这么漏的。
        assert bar.tone() == call, f"{call} 档写进去的不是 {call!r}"
        bad = _fb_box_rule_violation(bar)
        assert bad is None, (
            f"{call} 档的盒子规则用了类型选择器 {bad!r}：QLabel 继承 QFrame，"
            "行首图标与文字会各画一道自己的左色条（反馈条变三道条）"
        )


def test_feedback_bar_selector_gate_detects_planted_violation(qtbot):
    """自证：``_fb_box_rule_violation`` 对**真实出错那一份**必抓，合规写法不误报。"""
    bar = FeedbackBar()
    qtbot.add_widget(bar)
    bar.info("探针")
    # 控制组 ＝ 2026-09-30 之前的写法（真机长三道条的那一份）
    bar._box.setStyleSheet("QFrame{background:#000000; border-left:3px solid #000000;}")
    assert _fb_box_rule_violation(bar) == "QFrame", "真出错的写法必须判违规"
    # 合规：现在这一份，以及等价的 ID 写法
    bar._box.setStyleSheet(".QFrame{background:#000000;}")
    assert _fb_box_rule_violation(bar) is None
    bar._box.setStyleSheet("#fb-tone{background:#000000;}")
    assert _fb_box_rule_violation(bar) is None


def test_daemon_ops_install_button_reports_its_own_outcome(qtbot):
    """装成之后「安装守护」自己变「已安装」并失活；其余三种情形都不许谎报。

    「动作的反馈长在动作自己身上」那一半：只靠行内提示说结果时，用户点完仍看见一枚
    一模一样的「安装守护」，会怀疑刚才那下没生效（用户 2026-09-30 要求）。

    反向三条同样要守：**没装成不标**（判据是装成了，不是被点过）、**闸门仍喊重装不锁死**
    （那时跑着的守护可能还是旧的，用户得能再点一次）、**点卸载就还原**（确认可以取消）。
    """
    from barkit.shell import daemon_ops as ops

    def _build(holder, *, install, status, **kw):
        block, refresh = ops.build_daemon_ops(
            holder, install=install, uninstall=kw.pop("uninstall", lambda: None),
            status=status, **kw,
        )
        inst = next(b for b in block.findChildren(QPushButton) if b.text() == "安装守护")
        return block, refresh, inst

    # ① 装成、且闸门不再喊重装 ⇒ 自报「已安装」并失活
    holder = QWidget()
    qtbot.add_widget(holder)
    _blk, _rf, inst = _build(holder, install=lambda: True, status=lambda: ("", False))
    inst.click()
    assert inst.text() == "已安装" and not inst.isEnabled()

    # ② 装成、但闸门仍喊重装 ⇒ 保持可点（跑了旧代码正是闸门要抓的那一档）
    holder2 = QWidget()
    qtbot.add_widget(holder2)
    _blk2, _rf2, inst2 = _build(
        holder2, install=lambda: True,
        status=lambda: ("守护版本过期：旧版本会拒绝新的规则。", True),
    )
    inst2.click()
    assert inst2.text() == "安装守护" and inst2.isEnabled(), "仍喊重装时不许锁死按钮"

    # ③ 没装成 ⇒ 不标（失败的那一句由行内提示条说）
    holder3 = QWidget()
    qtbot.add_widget(holder3)
    _blk3, _rf3, inst3 = _build(
        holder3, install=lambda: False, status=lambda: ("系统守护尚未安装。", False),
    )
    inst3.click()
    assert inst3.text() == "安装守护" and inst3.isEnabled(), "没装成不许报已安装"

    # ④ 点「卸载守护」 ⇒ 先把「装」那枚还原（确认可以取消）
    holder4 = QWidget()
    qtbot.add_widget(holder4)
    blk4, _rf4, inst4 = _build(holder4, install=lambda: True, status=lambda: ("", False))
    inst4.click()
    assert inst4.text() == "已安装"
    uninst = next(b for b in blk4.findChildren(QPushButton) if b.text() == "卸载守护")
    uninst.click()
    assert inst4.text() == "安装守护" and inst4.isEnabled()


def test_reset_button_uses_the_action_tier(qtbot):
    """「恢复默认配置」是**动作按钮**，取 action 档（与同区其余按钮同高同字号）。

    它曾按 chip 档建（20px / 11px 字）—— 那是给「显示位芯片」那类小控件用的档，
    于是同一页出现两种按钮尺码（用户 2026-09-22 上报「比其它按钮都小」）。
    """
    win = _component_window(["dirty"], clearable={"dirty": ["profiles"]})
    qtbot.add_widget(win)
    btn = _reset_button(win, "dirty")
    assert btn is not None
    assert btn.height() == metrics.BUTTON_HEIGHTS["action"]
    assert btn.property(metrics.TIER_PROPERTY) == "action"


def test_zone_shows_up_for_either_card_alone(qtbot):
    """准入独立：只有运行间隔（周期型、没改过配置）或只有配置维护（改过、按需型）时，
    区都照出 —— 卡可缺，区不跟着缺。"""
    only_interval = _component_window(["poller"], poll_defaults={"poller": 30.0})
    qtbot.add_widget(only_interval)
    assert _poll_selector(only_interval, "poller") is not None
    assert _reset_button(only_interval, "poller") is None

    only_maintenance = _component_window(["dirty"], clearable={"dirty": ["profiles"]})
    qtbot.add_widget(only_maintenance)
    assert _reset_button(only_maintenance, "dirty") is not None
    assert _poll_selector(only_maintenance, "dirty") is None


def test_component_page_gets_no_zone_when_there_is_nothing_to_draw(qtbot):
    """按需型 ＋ 没改过配置 ⇒ 页尾**连区都不出**（不是出个空壳）。

    这一区是「框架按声明给组件画的东西」的唯一落点：准入各自独立，**卡可缺** ——
    「运行间隔」（周期型才出）与「配置维护」（改过配置才出）都不该为它白占版面。
    两张都没有时**区本身也不该出**：出个空壳只会让这一页被套上一层本不需要的外壳。

    （「数据与诊断」2026-09-22 曾是本区唯一恒有的那张卡，它撤走后，本用例从「只剩它」
    变成「一张都没有 ⇒ 无区」—— 少一层恒有卡，正是那次上报要的东西。）
    """
    win = _component_window(["demo"])
    qtbot.add_widget(win)
    assert _zone(win, "demo") is None
    assert _poll_selector(win, "demo") is None
    assert _reset_button(win, "demo") is None


def test_zone_asks_the_manager_instead_of_reusing_the_plugin_rows(qtbot):
    """框架区**现问 manager** 拿清单，不吃「插件」页的构建顺序。

    旧写法只读「插件」页构建时填的 ``_plugin_infos``，能跑通纯属「插件」页恰好先建
    （``_build_ui`` 与 ``rebuild_pages`` 都先建它）—— 换个入口建组件页就静默拿不到
    清单、整块区消失。把那份缓存清空：取数口与建区都必须照旧，正说明真源不是它。
    """
    win = _component_window(["poller"], poll_defaults={"poller": 30.0})
    qtbot.add_widget(win)

    win._plugin_infos = {}                    # 假装「插件」页还没建过
    assert win._plugin_info("poller") is not None, "取数口现问 manager，不读那份缓存"
    assert win._build_framework_zone("poller") is not None, "清空缓存后区仍然长得出"


def test_each_component_page_owns_its_own_feedback_slot(qtbot):
    """「一区一条」：两个组件页各有自己的槽，消息不串页。"""
    win = _component_window(
        ["poller", "ticker"], poll_defaults={"poller": 30.0, "ticker": 30.0}
    )
    qtbot.add_widget(win)

    a, b = win._zone_feedback["poller"], win._zone_feedback["ticker"]
    assert a is not b

    combo = _poll_combo(win, "poller")
    combo.setCurrentIndex(combo.findData("60"))
    assert "改为 60 秒" in a.text()
    assert b.text() == "", "另一页的槽一个字都不该有"


# ---------- 组件页的准入：**声明不再是准入**（2026-09-22 用户上报） ----------
#
# 改前：设置窗找页的依据是「组件自己声明了设置页」。于是「状态机声明齐全、却没写
# settings_page()」的组件（音频看护）**连页都没有** —— 框架要往页里注入的那两段
# （页首状态机段、页尾框架区）在它身上都画得出来，却没有挂点，用户在设置窗里根本
# 找不到这个组件。准入改成「这一页有没有内容」，与本仓「组件行」那一格同源：那个
# 显示位已改为「每个组件恒有一行，没声明由框架合成朴素行」，声明只决定长什么样。


class _SoloManager(_OrderManager):
    """``_OrderManager`` ＋ 一个「问题」字段 —— 桩要能表达**被隔离**的组件。"""

    def __init__(self, order, *, problems=None, **kw):
        super().__init__(order, **kw)
        self._problems = dict(problems or {})

    def list_plugins(self):
        infos = super().list_plugins()
        for info in infos:
            problem = self._problems.get(info.id)
            if problem is not None:
                info.problem = problem
        return infos


def _nav_labels(win):
    return [win._nav.item(i).text() for i in range(win._nav.count())]


def _page_texts(win, label):
    """该页（含框架注入的两段）里所有文字，拼成一个串供 ``in`` 断言。"""
    widget = _scrolled_widget(win._stack.widget(_nav_labels(win).index(label)))
    return "\n".join(w.text() for w in widget.findChildren(QLabel))


def _state_spec_for(plugin_id):
    from barkit.contribution.specs import StateLevel, StateOption

    return StateSpec(
        plugin_id=plugin_id, key="main", title="状态",
        states=[StateOption(id="ok", level=StateLevel.OK, text="取值甲")],
    )


def test_sole_state_machine_earns_a_framework_page(qtbot):
    """只声明了状态机、没自声明设置页 ⇒ 框架替它出一页，且页里真有那一段。"""
    contrib = _FakeContrib([], states=[_state_spec_for("solo")])
    win = SettingsWindow(parent=None, registry=contrib, manager=_OrderManager(["solo"]))
    qtbot.add_widget(win)

    assert "SOLO" in _nav_labels(win), "标签取组件名（框架出页时没有别的名字可用）"
    assert "取值甲" in _page_texts(win, "SOLO"), "页里要有框架注入的状态机段，不是空壳"


def test_component_with_nothing_to_show_earns_no_page(qtbot):
    """控制组：与上一条只差「有没有状态机声明」⇒ 两边都空 ⇒ **真的不出页**。

    准入若写成「活跃即出页」，这一条立刻红 —— 那会多一个点开是空的页。
    """
    win = SettingsWindow(
        parent=None, registry=_FakeContrib([]), manager=_OrderManager(["solo"]),
    )
    qtbot.add_widget(win)
    assert "SOLO" not in _nav_labels(win)
    assert win._nav.count() == 6, "通用 / 快捷键 / 变量 / 插件 / 维护 / 关于"


def test_periodic_component_earns_a_framework_page(qtbot):
    """「运行间隔」那一卡**单独**也能挣到一页（三条准入各自独立，卡可缺、序不变）。

    ``active=`` 是必须显式给的：桩的 ``active_ids`` 默认只由「它收集到的声明」推出来，
    而这条用例的组件**什么声明都没有**（正是要验的形态）—— 活跃与否由 manager 那边的
    「装载了它就是活跃的」决定，不由有没有声明决定。
    """
    win = SettingsWindow(
        parent=None, registry=_FakeContrib([], active={"poller"}),
        manager=_OrderManager(["poller"], poll_defaults={"poller": 30.0}),
    )
    qtbot.add_widget(win)
    assert "POLLER" in _nav_labels(win)
    assert _zone_card_titles(win, "poller") == ["运行间隔"]


def test_configured_component_earns_a_framework_page(qtbot):
    """「配置维护」那一卡**单独**也能挣到一页（用户差异段里有可清的键）。"""
    cfg = _ResetConfig(clearable={"dirty": ["notify_enabled"]})
    win = SettingsWindow(
        parent=None, registry=_FakeContrib([], active={"dirty"}),
        manager=_ResetManager(["dirty"], config=cfg), config=cfg,
    )
    qtbot.add_widget(win)
    assert "DIRTY" in _nav_labels(win)
    assert _zone_card_titles(win, "dirty") == ["配置维护"]


def test_declared_page_is_not_duplicated_by_the_framework(qtbot):
    """自声明了页的组件仍只出**一条**页 —— 合成只补缺，不重复。"""
    contrib = _FakeContrib(
        [_component_page("solo", label="自己那一页")],
        states=[_state_spec_for("solo")],
    )
    win = SettingsWindow(parent=None, registry=contrib, manager=_OrderManager(["solo"]))
    qtbot.add_widget(win)
    labels = _nav_labels(win)
    assert labels.count("自己那一页") == 1
    assert "SOLO" not in labels, "自己声明了页就不该再合成一条"


def test_isolated_component_earns_no_page(qtbot):
    """被隔离（贡献收集抛异常）的组件不出页 —— 只在「插件」页里由「问题」文案交代。

    与「组件行」那一格同一条口径：被隔离的插件**不合成**行。
    """
    contrib = _FakeContrib([], states=[_state_spec_for("solo")])
    mgr = _SoloManager(["solo"], problems={"solo": "贡献收集异常：boom"})
    win = SettingsWindow(parent=None, registry=contrib, manager=mgr)
    qtbot.add_widget(win)
    assert "SOLO" not in _nav_labels(win), "被隔离的组件不该长出一页"


def test_page_admission_falls_back_when_active_set_is_unavailable(qtbot):
    """桩缺 ``active_ids``（拿不到活跃集合）⇒ 退回旧行为，不自作主张。

    这条与 ``test_sole_state_machine_earns_a_framework_page`` 成对：一个证「有活跃集合时
    合成」，一个证「拿不到时只出自声明的页」—— 少了后者，退回路径永不被覆盖。
    """

    class _NoActive(_FakeContrib):
        active_ids = None            # 覆盖基类的 property：桩没有这个门面

    contrib = _NoActive([], states=[_state_spec_for("solo")])
    win = SettingsWindow(parent=None, registry=contrib, manager=_OrderManager(["solo"]))
    qtbot.add_widget(win)
    assert "SOLO" not in _nav_labels(win)
    assert win._nav.count() == 6


def test_decimal_field_carries_half_hours_without_a_trailing_zero(qtbot):
    """小数格：10.5 存得住、整数写成 ``9`` 而不是 ``9.0``、步进走 0.5 那一档。

    2026-09-27 用户要「工作小时数要指出 10.5」：推算那半边本来就按小数算，卡住的只有
    输入口 —— ``QSpinBox`` 会把 ``setValue(10.5)`` 截成 10。这条把它翻掉（换回整数格必红）。
    """
    from barkit.shell.ui_kit import DecimalField

    box = DecimalField()
    qtbot.addWidget(box)
    box.setRange(0.5, 24.0)

    box.setValue(10.5)
    assert box.value() == 10.5, "半小时要存得住（这就是用户要的那一档）"
    assert box.text() == "10.5"

    box.setValue(9.0)
    assert box.value() == 9.0
    assert box.text() == "9", "整数不带小数点（与摘要行的 :g 同一条口径）"

    box.stepBy(1)
    assert box.value() == 9.5, "步进是 0.5（⌘ 里的一枚 ↑ 就是一格半小时）"

    box.setValue(0.1)
    assert box.value() == 0.5, "下限是半小时"
    box.setValue(99)
    assert box.value() == 24.0, "上限 24 小时"


def test_decimal_field_lets_the_wheel_scroll_the_page(qtbot):
    """小数格与整数格**同一条滚轮口径**：滚轮只滚页面，不改值、不抢焦点。

    机制与 ``test_number_field_lets_the_wheel_scroll_the_page`` 逐条对应；这条单独立着，
    是因为小数格是新增的替身 —— 门禁 ``test_plugin_ui_has_no_bare_value_widget`` 只保证
    「不再长裸的」，保证不了「新替身自己也挡了滚轮」。
    """
    from PySide6.QtWidgets import QLineEdit, QVBoxLayout

    from barkit.shell.ui_kit import DecimalField

    host = QWidget()
    qtbot.addWidget(host)
    lay = QVBoxLayout(host)
    elsewhere = QLineEdit(host)
    lay.addWidget(elsewhere)

    box = DecimalField(host)
    lay.addWidget(box)
    box.setRange(0.5, 24.0)
    box.setValue(10.5)
    host.show()
    QApplication.processEvents()   # 离屏下要先走一轮事件，窗口才有 active 态
    assert host.focusWidget() is elsewhere, "前置：焦点在别处（不在小数格上）"

    ev = _wheel_event(box)
    QApplication.sendEvent(box, ev)
    assert box.value() == 10.5, "滚轮不该改值（设置项改完即写 ⇒ 一次误滚就是一条错配置）"
    assert not box.hasFocus(), "滚轮不该把焦点拿过来"
    assert host.focusWidget() is elsewhere, "滚轮不该动焦点"
    assert not ev.isAccepted(), "事件要留给父级滚动区（被吃掉 ⇒ 页面滚不动）"
    assert box.focusPolicy() == Qt.FocusPolicy.StrongFocus

    box.stepBy(1)                   # 改值的入口没被一起关掉
    assert box.value() == 11.0


# ---------- 维护页「恢复系统状态」卡（§5 A2，2026-10-02） ----------

def _revert_button(win):
    """按文案找那颗执行键（卡是按需生成的，没有稳定的对象名可找）。"""
    for btn in win.findChildren(QPushButton):
        if btn.text() == "恢复勾选的这几项":
            return btn
    return None


def test_revert_card_is_absent_when_nothing_was_changed(qtbot):
    """一个组件都没声明系统变更 ⇒ 卡不出现（**空壳不出**）。"""
    win = SettingsWindow(parent=None, registry=_FakeContrib([]), manager=_FakeManager())
    qtbot.add_widget(win)
    assert win._revert_switches == {}
    assert win._revert_feedback is None
    assert _revert_button(win) is None


def test_revert_card_defaults_to_unchecked_and_confirms_before_acting(qtbot):
    """默认**不勾**（不可逆的事不替用户选），且执行前先摆清单、确认了才动手。"""
    mutation = SystemMutation("/etc/hosts 中的 BarKit 托管块")
    mgr = _FakeManager([("hosts_guard", [mutation])])
    win = SettingsWindow(parent=None, registry=_FakeContrib([]), manager=mgr)
    qtbot.add_widget(win)

    assert list(win._revert_switches) == ["hosts_guard"]
    assert win._revert_switches["hosts_guard"].isChecked() is False

    btn = _revert_button(win)
    assert btn is not None
    btn.click()                                   # 一个都没勾 ⇒ 只提示，不动手
    assert mgr.reverted == []
    assert "先勾一项" in win._revert_feedback.text()
    # **不是成功回执**：这一步什么都没发生（没出错、也没做成）⇒ 中性告知那一档。
    # 拿 info（绿）说它，用户读到的是「成了」，而屏幕上什么都没变（2026-10-16 用户上报）。
    assert win._revert_feedback.tone() == "hint"

    win._revert_switches["hosts_guard"].setChecked(True)
    btn.click()
    assert mgr.reverted == [], "未经确认就撤了 —— 不可逆的动作必须走二次确认"
    assert win._revert_feedback.is_pending() is True
    assert "hosts_guard" in win._revert_feedback.text()      # 清单里点名要撤谁

    win._revert_feedback.accept()
    assert mgr.reverted == ["hosts_guard"]
    assert "已恢复 1 项" in win._revert_feedback.text()
    assert win._revert_feedback.tone() == "info"             # 这一档才是「做成了」


def test_revert_question_stays_in_the_card_beside_the_button(qtbot):
    """清单问句**就地**落在按钮下面那条槽里，不弹右上角浮层。

    2026-10-16 真机上报：那个浮层走 ``Overlay.show_toast``（可用区右上角）、窗口层级 25，
    而设置窗是 201 且从菜单栏图标下展开后被夹在屏幕右缘 —— 正好盖住右上角 ⇒ 确认卡被设置窗
    **挡死**，用户在按钮下面只看到上一轮遗留的旧提示，右上角什么也没看见。

    「弹到屏幕正中」也不是解：那仍然是**另一扇窗**（层级照样低于设置窗，且认屏跟着光标走）。
    故宿主面的确认一律就地：位置就是刚才点的那枚按钮下面。**代价明知** —— 多行清单会把这条
    槽撑高（同一张卡、同一页，撑高不影响别的卡）。
    """
    mgr = _FakeManager([("hosts_guard", [SystemMutation("/etc/hosts 中的 BarKit 托管块")])])
    win = SettingsWindow(parent=None, registry=_FakeContrib([]), manager=mgr)
    qtbot.add_widget(win)

    win._revert_switches["hosts_guard"].setChecked(True)
    _revert_button(win).click()

    assert mgr.reverted == [], "没有确认就撤了"
    fb = win._revert_feedback
    assert fb.is_pending() is True, "问句没落在按钮下面那条槽里"
    assert "hosts_guard" in fb.text(), "清单里没点名要撤谁"
    assert fb.tone() == "ask", "确认问句该走 ask 那一档（品牌浅底），别混进结果档"
    assert fb.confirm_text() == "确认恢复", "确认位的字要说清按下去会发生什么"

    fb.accept()
    assert mgr.reverted == ["hosts_guard"]
    assert "已恢复 1 项" in fb.text()


def test_revert_reports_the_failed_half(qtbot):
    """撤到一半要**点名**说清楚，不拿一句「已恢复」盖掉失败的那几个。"""
    mgr = _FakeManager([("hosts_guard", [SystemMutation("hosts 块")]),
                        ("route_guard", [SystemMutation("路由表规则")])],
                       revert_ok=False)
    win = SettingsWindow(parent=None, registry=_FakeContrib([]), manager=mgr)
    qtbot.add_widget(win)
    for sw in win._revert_switches.values():
        sw.setChecked(True)
    btn = _revert_button(win)
    btn.click()
    win._revert_feedback.accept()
    assert mgr.reverted == ["hosts_guard", "route_guard"]
    assert "没撤干净" in win._revert_feedback.text()
    assert "hosts_guard" in win._revert_feedback.text()


# ────────────────────────────────────────────
# 设置页按钮收口（_wire_button）：返回值与异常都要出声
# ────────────────────────────────────────────


def _wire_probe(qtbot):
    """造一枚按钮 + 一个只记不画的 ``say``（收口只认 ``say(text, error=…)`` 一种形状）。"""
    from PySide6.QtWidgets import QPushButton

    seen: list[tuple[str, bool]] = []

    def say(text, error=False):
        seen.append((text, error))

    btn = QPushButton("点")
    qtbot.add_widget(btn)
    return btn, seen, say


def test_wire_button_reports_exception(qtbot):
    """回调抛异常 ⇒ 出声（旧写法里 Qt 槽的异常只落到 stderr，界面等于没发生）。"""
    from barkit.shell.settings.helpers import _wire_button

    btn, seen, say = _wire_probe(qtbot)

    def boom():
        raise RuntimeError("写盘炸了")

    _wire_button(btn, boom, say)
    btn.click()
    assert len(seen) == 1
    assert seen[0][1] is True


def test_wire_button_reports_action_result(qtbot):
    """返回 ``ActionResult`` ⇒ 按 ``outcome`` 出声（FAILED 才 error，PARTIAL 不吓唬人）。"""
    from barkit.contribution.specs_outcome import ActionOutcome, ActionResult
    from barkit.shell.settings.helpers import _wire_button

    btn, seen, say = _wire_probe(qtbot)
    _wire_button(
        btn, lambda: ActionResult(ActionOutcome.FAILED, "没写进去"), say
    )
    btn.click()
    assert seen == [("没写进去", True)]

    btn, seen, say = _wire_probe(qtbot)
    _wire_button(
        btn, lambda: ActionResult(ActionOutcome.PARTIAL, "有一半没做成"), say
    )
    btn.click()
    assert seen == [("有一半没做成", False)]


def test_wire_button_stays_quiet_when_nothing_to_say(qtbot):
    """没话说就不占一行：``None`` / 空 ``text`` / 笔误的 ``True`` 一律静默。

    ``True`` 是 ``Signal.emit`` 的返回值（面板里踩过），``(a(), b())`` 是逗号表达式
    的元组 —— 收口靠 ``as_action_result`` 规整掉，宿主侧不必再 ``isinstance`` 猜。
    """
    from barkit.contribution.specs_outcome import ActionOutcome, ActionResult
    from barkit.shell.settings.helpers import _wire_button

    for wrong in (None, ActionResult(ActionOutcome.OK, ""), True, (1, 2)):
        btn, seen, say = _wire_probe(qtbot)
        _wire_button(btn, lambda _w=wrong: _w, say)
        btn.click()
        assert seen == [], wrong


def test_settings_page_has_no_raw_button_connection():
    """收口纪律：设置页再出现裸 ``clicked.connect`` ⇒ 那条路又有人绕过去了。

    唯一例外是收口自己（``helpers._wire_button`` 内部那一条），故这里按文件排除。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "barkit" / "shell" / "settings"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts or path.name == "helpers.py":
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "clicked.connect" in line:
                offenders.append(f"{path.name}:{n}")
    assert not offenders, "设置页出现裸 clicked.connect：" + ", ".join(offenders)
