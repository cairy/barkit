"""批次 2 渲染器契约：状态行按 identity 复用（禁全量重建）+ 回调不重复触发。

阻断级守护（方案 §6.3）：渲染层靠 identity 就地复用 widget，而非每次全量重建；
否则批次 3/5/7 的插件会在每次 set_contributions 时重建整行，丢失主题/状态并
导致回调重复连接（一次 toggle 跑 N 次）。
"""

from PySide6.QtWidgets import QPushButton

from barkit.contribution.specs import StatusRowSpec
from barkit.shell.panel import StatusPanel


def test_renderer_reuses_widget(qtbot):
    """连续两次 sync() 同一批 Spec → widget 实例不变、工厂只构造一次。"""
    built = []

    def factory(parent=None):
        w = QPushButton("row", parent)
        built.append(w)
        return w

    spec = StatusRowSpec(plugin_id="ng", key="main", custom=factory)
    panel = StatusPanel()
    qtbot.addWidget(panel)

    panel.sync_status_rows([spec], panel)
    first = panel._row_cache[spec.identity()]

    panel.sync_status_rows([spec], panel)  # 再次同步（如运行期启用/禁用）
    second = panel._row_cache[spec.identity()]

    assert first is second, "复用失败：两次同步得到不同实例"
    assert len(built) == 1, f"custom 工厂被重复调用 {len(built)} 次（应只构造一次）"


def test_renderer_callback_fires_once(qtbot):
    """复用后回调只触发一次：工厂内接好的回调不随 sync 重复连接。"""
    fires = []

    def factory(parent=None):
        w = QPushButton("row", parent)
        w.clicked.connect(lambda _=False: fires.append(1))  # 工厂内接一次
        return w

    spec = StatusRowSpec(plugin_id="ng", key="main", custom=factory)
    panel = StatusPanel()
    qtbot.addWidget(panel)

    panel.sync_status_rows([spec], panel)
    panel.sync_status_rows([spec], panel)  # 复用，不应重连

    w = panel._row_cache[spec.identity()]
    w.click()
    assert fires == [1], f"回调被重复触发：{fires}"


def test_renderer_removes_stale_rows(qtbot):
    """不再出现的 Spec 应从布局移除并销毁（运行期禁用后不残留旧行）。"""
    spec_a = StatusRowSpec(plugin_id="ng", key="a", custom=lambda p: QPushButton("a", p))
    spec_b = StatusRowSpec(plugin_id="ng", key="b", custom=lambda p: QPushButton("b", p))
    panel = StatusPanel()
    qtbot.addWidget(panel)

    panel.sync_status_rows([spec_a, spec_b], panel)
    assert len(panel._row_cache) == 2

    panel.sync_status_rows([spec_a], panel)  # b 被移除
    assert set(panel._row_cache.keys()) == {"ng.a"}, f"残留：{panel._row_cache.keys()}"
