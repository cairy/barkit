"""``SettingsWindow`` 本体：类常量、构造、Qt 虚函数重写与对外入口；混合类在包的其余子模块。

``settings.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Callable,
)

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QWidget,
)

from barkit.paths import APP_NAME
from barkit.shell.hotkey_catalog import HotkeyHost
from barkit.shell import placement
from barkit.shell.macos_window import (
    set_window_level,
    SETTINGS_LEVEL,
)
from barkit.shell.state_machines_section import StateMachinesSection
from barkit.shell.ui_kit import (
    FeedbackBar,
    play_entrance,
    qt_alive,
)

if TYPE_CHECKING:
    from barkit.contribution.specs import StateReport
    from barkit.core.config import Config
    from barkit.core.contributions import ContributionRegistry
    from barkit.core.plugin_manager import PluginManager

from .constants import log
from .framework import _FrameworkMixin
from .general import _GeneralMixin
from .maintenance import _MaintenanceMixin
from .pages import _PagesMixin
from .plugin_actions import _PluginActionsMixin
from .plugins import _PluginsMixin
from .search import _SearchMixin
from .state import _StateMixin


class SettingsWindow(
    _PagesMixin,
    _PluginsMixin,
    _PluginActionsMixin,
    _FrameworkMixin,
    _StateMixin,
    _GeneralMixin,
    _MaintenanceMixin,
    _SearchMixin,
    QDialog,
):
    # 混合类按**职责**分开在包内各子模块，方法经 ``self`` 取用实例状态，
    # 故 MRO 顺序不影响变量解析（混合类之间无继承、无重名）。``QDialog``
    # 必须排在最后：PySide6 的元类按最靠右的 Qt 基类登记虚函数。
    #: 初开尺寸（宽, 高）。比 Phase 0 的 560×380 大一档：那时窗口尺寸钉死，
    #: 长页只能靠滚动看；现在给用户一个「一眼能读完整一组」的起点。
    DEFAULT_SIZE = (640, 520)
    #: 最小尺寸：再小就会把侧栏导航（固定 150px）和内容区挤到互相压。
    MIN_SIZE = (520, 360)

    def __init__(
        self,
        parent: QWidget | None = None,
        registry: "ContributionRegistry | None" = None,
        manager: "PluginManager | None" = None,
        config: "Config | None" = None,
        on_tray_base_changed: "Callable[[], None] | None" = None,
        on_badge_level_changed: "Callable[[], None] | None" = None,
        on_display_changed: "Callable[[], None] | None" = None,
        on_state_priority_changed: "Callable[[], None] | None" = None,
        hotkey_host: HotkeyHost | None = None,
        variables_host=None,
    ) -> None:
        super().__init__(parent)
        # 与状态面板同为置顶（面板已 WindowStaysOnTopHint），确保二级窗口不被面板遮挡
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setWindowTitle(f"{APP_NAME} 设置")
        # **窗口可由用户拖动缩放**（2026-09-21）：原先 ``setFixedSize(560, 380)`` 钉死，
        # 而「网络切换」那种页有近 2900px 高 —— 只能靠滚动条爬。现在给一个初值与下限，
        # 上限交给用户（``_place`` 与 ``_clamp_initial_size`` 只负责别超出屏）。
        self.resize(*self.DEFAULT_SIZE)
        self.setMinimumSize(*self.MIN_SIZE)
        self.setObjectName("popover")
        self._registry = registry
        self._manager = manager
        self._config = config
        # 「维护」页「日志」卡的反馈槽（切档后告知）；窗口销毁时置 None
        self._log_feedback: "FeedbackBar | None" = None
        # 托盘底图改选后的回调（装配器 → 立即换菜单栏那一格）；None 时只落盘不换图
        self._on_tray_base_changed = on_tray_base_changed
        # 托盘阈值改档后的回调（装配器 → 立即重算角标队列）；None 时只落盘
        self._on_badge_level_changed = on_badge_level_changed
        # 显示位开关改完后的回调（装配器 → 重画标题栏 / 状态行 / 托盘角标）
        self._on_display_changed = on_display_changed
        # 状态机优先级改完后的回调（装配器 → 重算代表态；程序内写配置不发 changed）
        self._on_state_priority_changed = on_state_priority_changed
        # 运行期「当前生效值」的取数口（装配器注入：topic -> StateReport | None）。
        # 不注入 ⇒ 段里「当前」一栏显示「暂无报告」而不是崩：缺数据只降级，不藏整段。
        self._state_provider: "Callable[[str], StateReport | None] | None" = None
        # 配套：topic -> 是否已失联（失联时「当前」一栏说「状态失联」，不拿旧读数冒充）
        self._state_stale: "Callable[[str], bool] | None" = None
        # 已构建的状态机段（refresh_state_values 逐段刷新；重建分页时随旧页一起作废）
        self._state_sections: list[StateMachinesSection] = []
        # plugin_id -> 该组件**真正声明了**的显示位（见 _collect_slots）
        self._slots_by_plugin: dict[str, list[str]] = {}
        # 「插件」页的行内反馈槽（安装 / 恢复数据的唯一反馈出口；禁模态弹框）。
        # 页面每次重建都会换一个新的，故存引用 + 挂 destroyed 摘引用（见 _forget...）。
        self._plugin_feedback: FeedbackBar | None = None
        # 各组件设置页**页尾框架区**自带的反馈槽：``plugin_id -> FeedbackBar``。
        # 「一页一条行内反馈槽」在此修订为「**一区一条**」——「运行间隔」与「恢复默认配置」
        # 搬到组件页后，触发它们的按钮长在**页尾**，而这一页原有的槽（若有）在页首：
        # 长页（网络切换约 915px）里用户在页尾点了按钮**看不到问句**，正是零模态要防的
        # 「点了没反应」。页面重建即换新槽，故本表在重建开始时整表清空（见 _build_ui /
        # rebuild_pages）—— 别让已 deleteLater 的旧槽留在表里被戳。
        self._zone_feedback: dict[str, FeedbackBar] = {}
        # 装了 root helper 的插件实例（装置运维卡据此出红字）：守护连接 / 版本变化时
        # 逐一共振一次（各插件的 ``refresh_helper_notice()``），让「守护版本过期」及时
        # 退场 / 更新（见 ``refresh_helper_notices``）。
        self._helper_ops_plugins: "list" = []
        # 同上的**就地重算**入口：``plugin_id -> 重算卡片的可调用对象``。
        # 「恢复默认」清完这一格，那张「配置维护」卡该当场消失 —— 但**不走整页重建**：
        # 重建会连带把别的组件页（可能正开着草稿）一起拆了，代价与这件事不相称。
        self._zone_refresh: dict[str, Callable[[], None]] = {}
        # 「开机自启」卡的行内反馈槽（自启开关的唯一反馈出口；禁模态弹框）。
        # 与上面同理：重建页面即换新槽，存引用只为「摘自己那一个」。
        self._autostart_feedback: FeedbackBar | None = None
        # 「权限」卡的行内反馈槽（辅助功能探测 / 引导的唯一出口；禁模态弹框）。
        # 与前两个同理：重建页面即换新槽，存引用只为「摘自己那一个」。
        self._permissions_feedback: FeedbackBar | None = None
        # 「数据」卡的行内反馈槽（打开数据目录的唯一出口）。同上：一卡一条。
        self._data_feedback: FeedbackBar | None = None
        # 「恢复系统状态」卡的行内反馈槽（撤系统状态的唯一出口，含二次确认）。
        # **独立于自启 / 权限 / 数据那三条**：一卡一条，消息不该跑到别的卡去。
        self._revert_feedback: FeedbackBar | None = None
        # 该卡上「哪些组件被勾了」：pid → ``Switch``。**先给空字典** —— 卡可能因为
        # 「没有任何组件声明系统变更」而不出现（空壳不出），那时执行键也不存在。
        self._revert_switches: dict = {}
        # 「快捷键」页要的宿主能力（框架动作名单 / 改键重挂 / 冲突查询），**必须在
        # 构建前注入**：本窗的各页在 __init__ 里就建好了，事后再塞来不及。
        self._hotkey_host = hotkey_host
        # 「变量」页要的五个能力（列清单 / 现算一次 / 弹一次询问 / 加载问题 / 改完通知谁）。
        # 同上，**必须构建前注入**；不给 ⇒ 页面照出，只是不显示运行值、也不出「测试」。
        self._variables_host = variables_host
        #: 已建好的「变量」页（每次 show 刷一次它显示的**运行值** —— 那个值会在窗口
        #: 关着的时候变）。分页重建时被下一次构建覆盖，不会悬空。
        self._variables_page: "QWidget | None" = None
        self._build_ui()

    def showEvent(self, event) -> None:
        # 与状态面板同用原生浮层层级，且级别更高，确保二级窗口不被面板遮挡
        # （面板以 NSPopUpMenuWindowLevel 浮于全屏 Space 之上，设置须高于它）。
        # 防御：原生层级调用失败不应阻断窗口显示流程。
        try:
            set_window_level(self, SETTINGS_LEVEL)
        except Exception:  # noqa: BLE001
            log.warning("set_window_level 失败，设置窗口可能层级偏低", exc_info=True)
        # 每次打开刷新插件列表，反映运行期启停 / 卸载后的最新状态
        if self._manager is not None:
            self._rebuild_plugin_list()
        # 「变量」页显示的是**运行值**（今天填了几点上班、组件报了什么），那个值在窗口
        # 关着的时候照样在变 ⇒ 每次打开重读一遍，别让用户对着上一会话的旧读数做决定。
        self._refresh_variables_page()
        super().showEvent(event)

    def _refresh_variables_page(self) -> None:
        """刷「变量」页显示的当前值（窗口每次打开时叫一次）。

        页面可能已随上次 ``rebuild_pages`` 销毁（那次重建会立刻重新构建并覆盖这里的
        引用，故只在这两者之间的窗口里可能读到旧对象）⇒ 用 ``qt_alive`` 判一下，
        顺手把引用清掉，而不是让它留着一个「点了就崩」的悬空指针。
        """
        page = self._variables_page
        if page is None:
            return
        if not qt_alive(page):
            self._variables_page = None
            return
        try:
            page.refresh()
        except Exception:  # noqa: BLE001 —— 刷新失败不该拦住整个窗口的显示
            log.exception("刷新「变量」页失败")

    # ---------- 对外 ----------

    def select_page(self, label: str) -> bool:
        """切到标签为 ``label`` 的分页；返回是否找到。

        按**标签**查而不是按下标：分页集合会随插件启用 / 禁用 / 卸载变化
        （``rebuild_pages`` 重建后下标会漂移），标签是分页稳定的对外标识 ——
        ``rebuild_pages`` 恢复当前页用的也是标签，两条路同源，不会一处认名字、
        另一处认位置。查无此页时保持当前页并记一条 warning（不抛异常：这只可能
        发生在调用方给了个不存在的标签，不该连带把设置窗弄坏）。
        """
        for i in range(self._nav.count()):
            if self._nav.item(i).text() == label:
                self._nav.setCurrentRow(i)
                # 显式同步主区：setCurrentRow 会发信号、正常路径由信号切页，
                # 但显式再设一次与 rebuild_pages 同法，免去「信号被谁 block 了」的顾虑
                self._stack.setCurrentIndex(i)
                return True
        log.warning("设置窗没有名为 %r 的分页，保持当前页", label)
        return False

    def show_and_raise(self, anchor: "placement.Anchor | None" = None) -> None:
        """显示并定位。

        ``anchor`` 是 ``shell/placement`` 给的锚（贴着菜单栏图标那一档）；为 ``None``
        时落到所在屏可用区右上角 —— **不是屏角**（此前那一格算出来正好是屏幕左上角，
        见 ``shell/placement`` 模块 docstring）。

        **没有「打开哪一页」这个参数**（§5 #26，2026-09-18 用户裁定删）：它当年是给
        面板那枚「? 帮助」用的（一键落到「关于」页），而那枚按钮已按用户裁定删除
        （与「设置」开同一个窗、只差一次点击）。今天唯一的调用方是装配器的
        ``panel.settingsRequested``，它不带页 —— 留着参数就是留着一条没人走的岔路。
        真要「落到某页」时用 :meth:`select_page`（按标签，且它在生产路径上另有调用方）。

        顺序关键：先 show() 让 macOS 建好原生 frame（带标题栏的 QDialog 在此会重建框架、
        覆盖 show 之前的 move），再落位并核对 —— 落位只走 ``placement.place_window``
        一处，算式与认屏都不在本文件。

        窗口尺寸**归用户**（可拖动缩放，2026-09-21 起）：这里只做一件事 —— 打开前把
        尺寸夹进当前屏的可用区（见 ``_clamp_initial_size``）。
        """
        self._clamp_initial_size(anchor)
        self.show()
        pt, _ = placement.place_window(self, placement.Intent.BELOW_ICON, anchor=anchor)
        self.raise_()
        self.activateWindow()
        # 入场淡入（窗口不透明度，由窗口服务器合成）：仅原生 macOS cocoa 生效，
        # 离屏 / Windows no-op。别改回 QGraphicsEffect —— 它会把整窗画成透明，
        # 且每帧都要重画一遍整棵子树。
        play_entrance(self)
        placement.verify_placement(self, pt)

    def _clamp_initial_size(self, anchor: "placement.Anchor | None") -> None:
        """把窗口尺寸夹进触发它的那块屏的可用区（尺寸放开后落位算式管不了高矮）。

        落位只能把窗口**推回**屏内，改不了它的尺寸：窗口一旦比屏幕还高，它顶上去
        之后底部永远在屏外，用户既看不到底、也没法缩小（除非自己拖）。两种来路都会撞上：
        小屏 / 放大的菜单栏，或用户在上一次那把窗口拖得很大。夹的时候保底 ``MIN_SIZE``。
        """
        screen = placement.resolve_screen(self, placement.Intent.BELOW_ICON, anchor=anchor)
        if screen is None:
            return
        ag = screen.availableGeometry()
        w = max(self.MIN_SIZE[0], min(self.width(), ag.width() - 24))
        h = max(self.MIN_SIZE[1], min(self.height(), ag.height() - 24))
        if (w, h) != (self.width(), self.height()):
            self.resize(w, h)
