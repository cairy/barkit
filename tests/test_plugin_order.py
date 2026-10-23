"""OrderEditor（§5.5 / §6.4）+ PluginManager.reorder_plugin 单测。

分三层：

1. **落库语义**（纯逻辑）：`PluginManager.reorder_plugin` / `move_plugin` 共用
   一条路径，绝对下标 vs 相对位移两种入口必须等价。
2. **编辑器逻辑**（无鼠标）：`set_items` / `reorder` / 键盘微调 / 落库失败回滚。
3. **拖拽流程**（Qt 离屏）：手柄按下 → 越过阈值 → 落位 → 落库；以及拖拽中途
   外部 `set_items` 的重入安全（不留占位残影）。

拖拽用例依赖真实 layout geometry，故显式 `show()` + 等一帧让布局生效；
坐标一律用 `mapToGlobal` 从实际 geometry 推导，不硬编码像素。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from barkit.core.plugin_manager import PluginManager
from barkit.shell.order_editor import OrderEditor, _DragHandle

# ---------- 桩 ----------


class _Registry:
    """只需 ``discovered_classes``（reorder 用它算生效顺序 + 插件默认 order）。"""

    def __init__(self, orders: dict):
        self.discovered_classes = {
            pid: type(pid, (), {"order": order}) for pid, order in orders.items()
        }


class _Config:
    def __init__(self, order=None):
        self._order = list(order or [])
        self.writes: list[list[str]] = []

    def plugin_order(self):
        return list(self._order)

    def set_plugin_order(self, order):
        self._order = [str(x) for x in order]
        self.writes.append(list(self._order))


def _manager(orders: dict, user_order=None):
    """最小 PluginManager：只接 registry/config，其余依赖对 reorder 不可达。"""
    config = _Config(user_order)
    mgr = PluginManager(
        registry=_Registry(orders),
        contributions=None,
        scheduler=None,
        hotkeys=None,
        events=None,
        panel=None,
        config=config,
        on_state_update=lambda *_a: None,
    )
    return mgr, config


# ---------- 1. 落库语义 ----------


def test_effective_order_user_first_then_plugin_order():
    mgr, _ = _manager({"a": 1, "b": 2, "c": 3}, ["c"])
    assert mgr.effective_order() == ["c", "a", "b"]


def test_reorder_plugin_to_index_persists_absolute_index():
    mgr, config = _manager({"a": 1, "b": 2, "c": 3}, ["c"])
    # ["c","a","b"] 里把 b 移到最前：others=["c","a"]，插入下标 0
    assert mgr.reorder_plugin("b", 0) is True
    assert config.writes == [["b", "c", "a"]]
    assert mgr.effective_order() == ["b", "c", "a"]


def test_reorder_plugin_noop_and_unknown():
    mgr, config = _manager({"a": 1, "b": 2}, ["a", "b"])
    assert mgr.reorder_plugin("a", 0) is False      # 位置没变
    assert mgr.reorder_plugin("nope", 0) is False   # 未发现
    assert config.writes == []


def test_reorder_plugin_clamps_out_of_range():
    mgr, config = _manager({"a": 1, "b": 2}, ["a", "b"])
    assert mgr.reorder_plugin("a", 99) is True      # 超出上界 → 夹到末尾
    assert config.writes == [["b", "a"]]
    assert mgr.reorder_plugin("b", -5) is False     # 已在首位 → 夹取后无变化
    assert mgr.reorder_plugin("a", -5) is True      # 负数 → 夹到首位
    assert config.writes[-1] == ["a", "b"]
    assert len(config.writes) == 2


def test_move_plugin_is_reorder_special_case():
    mgr, config = _manager({"a": 1, "b": 2, "c": 3}, ["a", "b", "c"])
    assert mgr.move_plugin("b", -1) is True
    assert config.writes[-1] == ["b", "a", "c"]
    assert mgr.move_plugin("b", +1) is True
    assert config.writes[-1] == ["a", "b", "c"]


def test_move_plugin_multi_step_keeps_relative_order():
    """|delta|>1 是「位移」不是「换位」：其余项相对序不变（防语义漂移回归）。"""
    mgr, config = _manager({"a": 1, "b": 2, "c": 3, "d": 4}, ["a", "b", "c", "d"])
    assert mgr.move_plugin("d", -2) is True
    assert config.writes[-1] == ["a", "d", "b", "c"]
    assert mgr.move_plugin("d", +3) is True
    assert config.writes[-1] == ["a", "b", "c", "d"]


def test_move_plugin_at_edges_is_noop():
    mgr, config = _manager({"a": 1, "b": 2}, ["a", "b"])
    assert mgr.move_plugin("a", -1) is False   # 已在首位
    assert mgr.move_plugin("b", +1) is False   # 已在末位
    assert mgr.move_plugin("ghost", +1) is False
    assert config.writes == []


def test_reorder_plugin_notifies_listeners():
    mgr, _ = _manager({"a": 1, "b": 2}, ["a", "b"])
    calls: list = []
    mgr.on_plugins_changed = lambda: calls.append(1)
    assert mgr.reorder_plugin("b", 0) is True
    assert calls == [1]
    assert mgr.reorder_plugin("b", 0) is False  # no-op 不广播
    assert calls == [1]


# ---------- 2. 编辑器：构建与键盘路径 ----------


def _build_row(pid: str, handle: QWidget) -> QWidget:
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(handle)
    lay.addWidget(QLabel(pid), 1)
    row.setFixedHeight(40)
    return row


def _make_editor(qtbot, pids, on_reorder=None) -> OrderEditor:
    editor = OrderEditor(build_row=_build_row, on_reorder=on_reorder)
    qtbot.addWidget(editor)
    editor.resize(220, 400)
    editor.set_items(pids)
    editor.show()
    qtbot.wait(20)  # 让布局算出真实 geometry（后续拖拽用例依赖）
    return editor


def test_set_items_builds_rows_with_handles(qtbot):
    editor = _make_editor(qtbot, ["a", "b", "c"])
    assert editor.order == ["a", "b", "c"]
    assert set(editor._rows) == {"a", "b", "c"}
    assert all(isinstance(h, _DragHandle) for h in editor._handles.values())
    # 手柄必须在行内（否则真机上「可见但拖不动」）
    for pid, handle in editor._handles.items():
        assert handle.parent() is editor._rows[pid]


def test_keyboard_nudge_reorders_and_persists(qtbot):
    calls: list = []
    editor = _make_editor(
        qtbot, ["a", "b", "c"], on_reorder=lambda pid, i: calls.append((pid, i)) or True
    )
    changed: list = []
    editor.orderChanged.connect(changed.append)

    handle = editor._handles["c"]
    handle.nudgeRequested.emit(-1)  # Alt+↑ 的等价信号
    assert calls == [("c", 1)]
    assert editor.order == ["a", "c", "b"]
    assert changed == [["a", "c", "b"]]
    visual = [editor._layout.itemAt(i).widget() for i in range(editor._layout.count())]
    assert visual == [editor._rows[pid] for pid in editor.order]


def test_keyboard_nudge_reindexes_handles(qtbot):
    editor = _make_editor(qtbot, ["a", "b"], on_reorder=lambda pid, i: True)
    editor._handles["b"].nudgeRequested.emit(-1)
    assert editor.order == ["b", "a"]
    assert editor._handles["b"]._row_index == 0
    assert editor._handles["a"]._row_index == 1


def test_reorder_visual_order_matches_internal(qtbot):
    editor = _make_editor(qtbot, ["a", "b", "c"], on_reorder=lambda pid, i: True)
    editor.reorder("a", 2)
    assert editor.order == ["b", "c", "a"]
    visual = [
        editor._layout.itemAt(i).widget() for i in range(editor._layout.count())
    ]
    assert visual == [editor._rows[pid] for pid in editor.order]


def test_reorder_rolls_back_when_persist_fails(qtbot):
    editor = _make_editor(qtbot, ["a", "b", "c"], on_reorder=lambda pid, i: False)
    changed: list = []
    editor.orderChanged.connect(changed.append)
    assert editor.reorder("a", 2) is False
    assert editor.order == ["a", "b", "c"]
    assert changed == []
    visual = [editor._layout.itemAt(i).widget() for i in range(editor._layout.count())]
    assert visual == [editor._rows[pid] for pid in editor.order]


def test_reorder_without_callback_is_view_only(qtbot):
    editor = _make_editor(qtbot, ["a", "b"])
    assert editor.reorder("b", 0) is True
    assert editor.order == ["b", "a"]


# ---------- 3. 拖拽流程（Qt 离屏，坐标由真实 geometry 推导） ----------


def _drag_press(editor, pid):
    """在行首手柄上模拟按下，返回 (行内按下点, 全局按下点)。"""
    row = editor._rows[pid]
    local = QPoint(row.width() // 2, 8)
    return local, row.mapToGlobal(local)


def test_drag_moves_row_to_end_and_persists(qtbot):
    calls: list = []
    editor = _make_editor(
        qtbot, ["a", "b", "c"], on_reorder=lambda pid, i: calls.append((pid, i)) or True
    )
    _, press = _drag_press(editor, "a")
    editor._begin_drag(0, press)

    # 第一次移动越过阈值 → 进入拖拽态（此时布局刚变化，需等一帧再定位）
    editor._update_drag(press + QPoint(0, 120))
    qtbot.wait(20)
    assert editor._drag is not None and editor._drag.started is True
    assert editor._drag.placeholder is not None

    # 用「当前」geometry 重新算目标：指针落在最后一行中心之下 → 插到末尾
    last_center = editor._rows["c"].geometry().center().y()
    goal = editor.mapToGlobal(QPoint(0, last_center + 5))
    editor._update_drag(goal)
    editor._finish_drag()

    assert calls == [("a", 2)]
    assert editor.order == ["b", "c", "a"]
    assert editor._drag is None
    # 占位块已回收，布局里只剩 3 行
    assert editor._layout.count() == 3


def test_drag_below_threshold_is_a_click(qtbot):
    calls: list = []
    editor = _make_editor(
        qtbot, ["a", "b"], on_reorder=lambda pid, i: calls.append((pid, i)) or True
    )
    _, press = _drag_press(editor, "a")
    editor._begin_drag(0, press)
    editor._update_drag(press + QPoint(0, 2))  # 未越阈值
    # 关键：阈值内不得进入拖拽态（否则行会被摘出布局、留下悬浮残影）
    assert editor._drag is not None and editor._drag.started is False
    assert editor._drag.placeholder is None
    editor._finish_drag()
    assert calls == []
    assert editor.order == ["a", "b"]
    assert editor._layout.count() == 2


def test_set_items_during_drag_aborts_without_residue(qtbot):
    calls: list = []
    editor = _make_editor(
        qtbot, ["a", "b", "c"], on_reorder=lambda pid, i: calls.append((pid, i)) or True
    )
    _, press = _drag_press(editor, "a")
    editor._begin_drag(0, press)
    editor._update_drag(press + QPoint(0, 120))
    qtbot.wait(20)
    assert editor._drag is not None and editor._drag.started is True

    editor.set_items(["c", "b", "a"])  # 外部重建（如插件启停）
    assert editor._drag is None
    assert editor.order == ["c", "b", "a"]
    assert editor._layout.count() == 3
    assert calls == []  # 中止不留落库副作用
    for pid in editor.order:  # 手柄仍可用：按下态已复位
        assert editor._handles[pid].pressed is False


def test_handle_ignores_non_left_button(qtbot):
    editor = _make_editor(qtbot, ["a", "b"])
    handle = editor._handles["a"]
    pressed: list = []
    handle.dragPressed.connect(lambda i, p: pressed.append(i))
    from PySide6.QtGui import QMouseEvent

    ev = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPoint(2, 2),
        handle.mapToGlobal(QPoint(2, 2)),
        Qt.MouseButton.RightButton,
        Qt.MouseButton.RightButton,
        Qt.KeyboardModifier.NoModifier,
    )
    handle.mousePressEvent(ev)
    assert pressed == []
    assert handle.pressed is False
