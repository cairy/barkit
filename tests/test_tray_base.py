"""菜单栏主图标换源（P0③）：底图解析 + 回落规则 + 事件接线。

一句话：**菜单栏那一格可以显示「某台状态机当前的取值图标」**（如屏幕字形的实心 / 空心）。

来源由用户在设置窗「通用」页选，落盘 ``tray.base_source`` = 状态机**身份**
（``{plugin_id}.{key}``，如 ``notify_guard.display``）—— 不是事件总线 topic：
配置指向的是「谁」，不是「总线上的哪个地址」。准入判据只有一条：该状态机在声明里
**定义了主图标**（``StateSpec.icon is not None``），没有单独的 ``as_tray_base`` 开关。

这一支全是「不许出空格、不许撒谎」的判据，故回落规则写成硬性表：

| 情形 | 结果 |
|---|---|
| 没配置来源 | BarKit 默认底图 |
| 来源状态机**不在册**（组件被禁用 / 卸载） | BarKit 默认底图 |
| 该状态还没有报告 | 该状态机声明的**主图标** |
| 该取值没给取值图标（``StateOption.icon is None``） | 同上 |
| 该状态已失联（面板判定的唯一执行者） | 同上 |

后三行刻意**不**掉到 BarKit 默认底图：有主图标就该用它 —— 尤其失联时继续画上一次
的取值图标（如「实心屏」）才是撒谎，回落主图标既如实、又仍是**该组件**的信息。
取值图标的解析与回落都在 ``renderers/state_view.resolve`` 一处（决议 D13），
本模块只管「画哪一枚」，不重复实现回落规则。

渲染解析（``ui_kit.tray_base_pixmap``）与决策（``AppShell._apply_tray_base``）刻意分离：
前者只管画，后者只管判——所以这里也分成两组测。
"""

from __future__ import annotations

from PySide6.QtGui import QColor

from barkit.contribution.specs import (
    IconName,
    StateLevel,
    StateOption,
    StateReport,
    StateSpec,
)
from barkit.shell.ui_kit import make_tray_pixmap, tray_base_pixmap

STATE_ID = "notify_guard.display"
TOPIC = "plugin.notify_guard.status.display"
MAIN_ICON = IconName.DISPLAY_OFF          # 状态机声明的主图标 = 取值没给图标时的回落目标
STATE = StateSpec(
    plugin_id="notify_guard",
    key="display",
    title="第二屏",
    icon=MAIN_ICON,
    states=(
        StateOption("extended", StateLevel.OK, IconName.DISPLAY_ON, "第二屏已连接"),
        StateOption("idle", StateLevel.IDLE, text="无第二屏"),   # 没给取值图标 → 回落主图标
    ),
)
SNAPSHOT = StateReport("extended")        # 报告只报取值名（决议 D13）


def _raw(pixmap) -> bytes:
    return bytes(pixmap.toImage().bits())


def _ink(pixmap) -> int:
    """不透明像素数（用来区分「实心屏」与「空心屏」，也用来发现画成空白）。"""
    img = pixmap.toImage()
    return sum(
        1
        for x in range(img.width())
        for y in range(img.height())
        if img.pixelColor(x, y).alpha() > 0
    )


# ──────────────────────────────────────────────
# 渲染解析（只画不判）
# ──────────────────────────────────────────────


def test_tray_base_pixmap_none_delegates_to_default_glyph(qapp, monkeypatch):
    """``None`` = 默认底图 ⇒ **必须委派给 `make_tray_pixmap`**（出厂行为不回归）。

    本用例替代原先那条「与 `make_tray_pixmap` **逐像素**一致」的断言。那条把判据落到了
    offscreen raster 的**像素层**：进程内一旦先产生过别的 QPixmap，`toImage()` 的边缘
    AA 像素就会 ±1 抖动（2026-09-19 实测：最小节点孤立跑 300 次零抖动，一旦进程内先有
    别的 QPixmap，工作区 28/300 中招、**基线 71/300**）⇒「逻辑同一」被误报成回归。
    机制与隔离复跑写法见 skill `barkit-windows-verify` §flaky 登记。

    拦 `make_tray_pixmap` 之后判据反而**更严**：有人把条形码画法复制一份进 ``None``
    分支，像素比较照样绿（画出来一样），这条会红（没走同一条路径）。
    「默认底图长什么样」由 `make_tray_pixmap` 自己负责，本用例不重复替它断言外观。
    """
    calls: list[tuple] = []
    sentinel = object()

    def spy(size, color=None, dpr=1.0):
        calls.append((size, color, dpr))
        return sentinel

    monkeypatch.setattr("barkit.shell.ui_kit.base.make_tray_pixmap", spy)

    got = tray_base_pixmap(None, 22, QColor(255, 255, 255))

    assert calls == [(22, QColor(255, 255, 255), 1.0)], (
        "`None` 分支必须委派 `make_tray_pixmap`，且尺寸/颜色**原样透传**（dpr 默认 1.0）"
    )
    assert got is sentinel, "`None` 分支不该另画一张 —— 必须透传 `make_tray_pixmap` 的返回值"


def test_tray_base_pixmap_renders_semantic_icon_at_same_size(qapp):
    """给了图标必须真换成它：与默认底图不同、实心/空心可区分、尺寸不变。"""
    default = make_tray_pixmap(22, QColor(255, 255, 255))
    on = tray_base_pixmap(IconName.DISPLAY_ON, 22, QColor(255, 255, 255))
    off = tray_base_pixmap(IconName.DISPLAY_OFF, 22, QColor(255, 255, 255))

    assert on.size() == default.size(), "换源只换字形，不该改菜单栏那一格的大小"
    assert _raw(on) != _raw(default), "给了图标却仍画默认底图（换源是空操作）"
    assert _ink(on) > _ink(off) > 0, "实心屏的墨量必须大于空心屏"


# ──────────────────────────────────────────────
# 决策与回落（AppShell._apply_tray_base）
# ──────────────────────────────────────────────


class _FakePanel:
    def __init__(self):
        self.stale: set[str] = set()

    def is_stale(self, topic: str) -> bool:
        return topic in self.stale

    def update_state_snapshot(self, spec, snapshot) -> None:
        pass


class _FakeStatusItem:
    def __init__(self):
        self.applied: list = []
        self.badges: list = []
        self.tooltips: list = []

    def set_base_icon(self, icon) -> None:
        self.applied.append(icon)

    def set_badge(self, level, icon=None) -> None:
        self.badges.append((level, icon))

    def set_tooltip(self, text) -> None:
        self.tooltips.append(text)


class _FakeConfig:
    def __init__(self, source: str = ""):
        self.source = source

    def tray_base_source(self) -> str:
        return self.source

    def tray_min_level(self) -> str:
        return "warn"

    def plugin_ui_visible(self, plugin_id: str, slot: str) -> bool:
        """显示位开关（决议 D4）：本文件不测它，给真实契约的默认值（全可见）。

        必须实现 —— 桩缺新接口会让 `_tray_candidates` 在**装配器冒烟**里炸掉，
        测出来的是「桩过期」而不是「生产代码错」（与 ``tray_min_level`` 同款理由）。
        """
        return True


class _FakeContributions:
    """注册中心桩：只给底图决策用到的那两个门面。

    ``state_by_id`` 必须**真按身份查**（而非忽略入参恒回一台机）——否则
    「配置指向一台已卸载的机」那条回落规则会被这层桩悄悄蒙过去。
    """

    def __init__(self, specs=()):
        self._specs = list(specs)

    def states(self):
        return self._specs

    def state_by_id(self, state_id: str):
        if not state_id:
            return None
        return next((s for s in self._specs if s.state_id == state_id), None)

    def badges(self):
        return []

    def components(self):
        # 底图决策不问组件名；托盘候选的名牌回查兜底用 plugin_id
        return []


def _shell(app, source: str = "", panel=None, status_item=None, specs=(STATE,)):
    """装配器的**单元**环境：只接底图决策用到的那几根线，不跑 assemble。"""
    from barkit.shell.app_shell import AppShell

    shell = AppShell(app)
    shell._config = _FakeConfig(source)
    shell.panel = panel if panel is not None else _FakePanel()
    shell.status_item = status_item if status_item is not None else _FakeStatusItem()
    shell._contributions = _FakeContributions(specs)
    return shell


def test_falls_back_when_source_unconfigured(qapp):
    """没配置来源 → 默认底图，**即使快照就在手边**（不能自作主张换图）。"""
    si = _FakeStatusItem()
    shell = _shell(qapp, source="", status_item=si)
    shell._state_reports[TOPIC] = SNAPSHOT
    shell._apply_tray_base()
    assert si.applied == [None]


def test_falls_back_when_source_state_not_registered(qapp):
    """来源状态机**不在册**（组件被禁用 / 卸载）→ 默认底图，不留空格。

    这是「设置窗显示默认、菜单栏还画着旧图」那条不一致的正解：两边判据同源
    （都问注册中心「这台机在不在」），组件一卸就同时归零。
    """
    si = _FakeStatusItem()
    shell = _shell(qapp, source=STATE_ID, status_item=si, specs=())   # 组件已卸下
    shell._state_reports[TOPIC] = SNAPSHOT                           # 快照还在也不能用
    shell._apply_tray_base()
    assert si.applied == [None]


def test_uses_value_icon_when_configured(qapp):
    """正常路径：来源在册 + 报告新鲜 ⇒ 画该取值的**取值图标**（而非静态主图标）。"""
    si = _FakeStatusItem()
    shell = _shell(qapp, source=STATE_ID, status_item=si)
    shell._state_reports[TOPIC] = SNAPSHOT
    shell._apply_tray_base()
    assert si.applied == [IconName.DISPLAY_ON]


def test_falls_back_to_main_icon_on_missing_or_iconless_snapshot(qapp):
    """报告缺失 / 该取值没给取值图标 → 回落**状态机声明的主图标**，不是默认底图。

    「还没数据」与「这个取值没配图标」是两种情形，但对用户是同一件事：这台机现在画不出
    它的取值图标。有主图标就画主图标 —— 换成 BarKit 自己的牌子反而丢了来源信息。
    """
    si = _FakeStatusItem()
    shell = _shell(qapp, source=STATE_ID, status_item=si)
    shell._apply_tray_base()                                   # 还没有报告
    shell._state_reports[TOPIC] = StateReport("idle")          # 该取值没给图标
    shell._apply_tray_base()
    assert si.applied == [MAIN_ICON, MAIN_ICON]


def test_falls_back_to_main_icon_when_source_state_is_stale(qapp):
    """状态失联 → 回落主图标。否则菜单栏会一直显示「已连接」，比不显示更糟。

    失联判定只问面板（``is_stale``），托盘不另起一套时钟。
    """
    si = _FakeStatusItem()
    panel = _FakePanel()
    shell = _shell(qapp, source=STATE_ID, panel=panel, status_item=si)
    shell._state_reports[TOPIC] = SNAPSHOT
    panel.stale.add(TOPIC)
    shell._apply_tray_base()
    assert si.applied == [MAIN_ICON]


def test_source_without_main_icon_still_uses_fresh_snapshot_icon(qapp):
    """来源状态机**没声明主图标**（手改配置才会有的形态）：有新鲜取值图标就照画。

    准入判据（有主图标）管的是**能不能被选**，不是**能不能画** —— 真拿到一份新鲜
    取值图标时没有理由不画它。这条同时也是「候选过滤」与「渲染决策」互不越界的
    证据：决策点不重复实现准入判据。
    """
    bare = StateSpec(plugin_id="notify_guard", key="display", title="第二屏", states=STATE.states)
    assert bare.icon is None
    si = _FakeStatusItem()
    shell = _shell(qapp, source=STATE_ID, status_item=si, specs=(bare,))
    shell._state_reports[TOPIC] = SNAPSHOT
    shell._apply_tray_base()
    assert si.applied == [IconName.DISPLAY_ON]


def test_falls_back_to_default_when_nothing_to_draw(qapp):
    """兜底兜到底：既没主图标、也没取值图标 ⇒ 默认底图（**绝不画 None 图标**）。

    这是决策点自己的最后一道保险：上游任何一侧出问题（配置手改、组件半迁移、
    报告空转），菜单栏都只会退回 BarKit 自己的牌子，不会变成空白或异常。
    """
    bare = StateSpec(plugin_id="notify_guard", key="display", title="第二屏", states=STATE.states)
    si = _FakeStatusItem()
    shell = _shell(qapp, source=STATE_ID, status_item=si, specs=(bare,))
    shell._apply_tray_base()                                       # 还没有报告
    shell._state_reports[TOPIC] = StateReport("idle")              # 该取值也没给图标
    shell._apply_tray_base()
    assert si.applied == [None, None]


def test_state_event_records_snapshot_and_drives_tray_base(qapp):
    """事件路径端到端：`on_state_update` 必须 ①缓存快照 ②立即按来源换图。"""
    si = _FakeStatusItem()
    shell = _shell(qapp, source=STATE_ID, status_item=si)
    shell.on_state_update(STATE, SNAPSHOT)
    assert shell._state_reports[TOPIC] is SNAPSHOT
    assert si.applied == [IconName.DISPLAY_ON]


def test_stale_flip_reapplies_tray_base(qapp):
    """失联翻转（面板广播 stateLevelChanged）也要重算底图，否则菜单栏慢一拍。"""
    si = _FakeStatusItem()
    panel = _FakePanel()
    shell = _shell(qapp, source=STATE_ID, panel=panel, status_item=si)
    shell._state_reports[TOPIC] = SNAPSHOT
    shell._apply_tray_base()
    panel.stale.add(TOPIC)
    shell._on_state_level_changed(TOPIC, "err")
    assert si.applied == [IconName.DISPLAY_ON, MAIN_ICON]
