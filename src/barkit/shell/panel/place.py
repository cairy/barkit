"""``_PlaceMixin`` —— 落位、显示、激活与收尾。

``shell/panel.py`` 拆包（2026-10-01）：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import time

from PySide6.QtCore import (
    QPoint,
    QTimer,
)

from barkit.shell import (
    placement,
    screen_settle,
    styles,
)
from barkit.shell.macos_window import set_window_level

from .constants import log


#: ``show()`` 之后多久用**独立读数**（窗口服务器）复核一次落位。窗口服务器对移动的读数会
#: 滞后（实测约 0.35s 才稳定），读早了会把「还没生效」误判成分叉。
PLACEMENT_RECHECK_MS = 450


class _PlaceMixin:
    """落位、显示、激活与收尾。

    与 ``StatusPanel`` 同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    @property
    def anchor(self) -> "placement.Anchor | None":
        """本次打开面板用的锚（从未打开过 = ``None``）。

        给「从面板里再开一个二级窗」的来路读：设置窗与详情页跟面板落同一处 —— 它们
        弹出来的那一刻光标在**面板**上、不在图标上，再回读一次图标在哪既多余、又可能
        读到坏值（多屏下一枚图标有多枚原生窗口）。
        """
        return getattr(self, "_anchor", None)

    def toggle_at(self, anchor: "placement.Anchor | None" = None) -> None:
        """在锚（菜单栏图标那一条）正下方显示/隐藏，自动夹进屏幕。

        ``anchor`` 由 ``shell/placement`` 给出：点图标来的那一枚**由点击点推出**
        （点图标时光标必然在图标上），热键 / 命令来的那一枚才回读原生图标窗口。
        取不到锚时 ``place_window`` 会把面板摆到可用区右上角（不是屏幕角）。
        """
        self._anchor = anchor
        if self.isVisible():
            log.info("面板关闭［再点一次菜单栏图标］")
            self.hide()
            return
        self._request_show()

    def _request_show(self) -> None:
        """把「显示面板」排到屏稳定之后 —— **只拦还没上过屏的窗**。

        为什么必须等：屏参数正在变的那一段里**首次上屏**的窗会被窗口服务器钉在 ``(0,0)``，
        而这份错结果**常驻**、进程内救不回来（见 :mod:`barkit.shell.screen_settle`）。
        为什么**只拦新窗**：已经上过屏的窗不受影响（窗口服务器早给它记好位置了），再等它
        只是白让用户多等一次点击。

        ``_show_pending`` 去重：等门期间用户又点了几下图标，也只排一次队（其余那几下按
        「再点一次就是关闭」的语义没有面板可关，直接作废）。
        """
        if getattr(self, "_show_pending", False):
            return
        if getattr(self, "_ever_shown", False) or not screen_settle.is_settling():
            self._show_at_anchor()
            return
        self._show_pending = True
        log.info("屏参数正在变化，等它静默之后再开面板（避让窗口服务器重排的那一段）")
        QTimer.singleShot(screen_settle.WAIT_RETRY_MS, self._retry_show_after_settle)

    def _retry_show_after_settle(self) -> None:
        """门开了就显示；还在变就再等一拍（判据只有 ``screen_settle`` 那一处）。"""
        if getattr(self, "_ever_shown", False) or not screen_settle.is_settling():
            self._show_pending = False
            self._show_at_anchor()
            return
        QTimer.singleShot(screen_settle.WAIT_RETRY_MS, self._retry_show_after_settle)

    def on_status_item_click(self, anchor: "placement.Anchor | None" = None) -> None:
        """状态栏图标点击：与「原生 dismiss 关闭」去重，避免图标点击后重新打开。

        Popup 在 macOS 上点外部（含点状态栏图标）会由 Qt 原生机制关闭面板。若本次
        图标点击先触发了原生 dismiss（面板刚被关），就不应再 toggle 打开。用「最近
        关闭时间 < 0.3s」判断即可：刚关过就不重开（视为本次点击=关闭）；关了很久或
        从未显示则打开。

        ``_last_hide`` 存 :func:`time.monotonic` 刻度：减出来的是「距上次关闭过了多久」。
        用墙上时钟时，时钟被往回校 ⇒ 差值长期为负 ⇒ 这句去重恒真 ⇒ **点图标打不开面板**。
        """
        if not self.isVisible() and (time.monotonic() - self._last_hide) < 0.3:
            return
        self.toggle_at(anchor)

    def dismiss(self) -> None:
        """统一关闭入口：popover 模式走 presenter.close，否则 Qt hide。

        供 app_shell 在打开详情页等场景收起面板（原来直接 ``self.hide()``）；
        两种模式都顺带记 _last_hide 供图标点击去重。
        """
        self._last_hide = time.monotonic()
        log.info("面板关闭［统一入口：让位给详情页 / 设置窗］")
        self.hide()

    def _show_at_anchor(self) -> None:
        """按当前内容重新布局并定位显示（首次打开 / 重新锚定 / 折叠展开共用）。

        无边框 Window 下 adjustSize 实时可靠，故折叠/展开/内容变化都直接按当前
        _folded 计算尺寸并 adjustSize，无需 hide→重 show。显示后把 NSWindow 层级提到
        NSPopUpMenuWindowLevel（浮于全屏 Space 之上），并启动「失活即关闭」控制器
        （点面板外区域关闭）。
        """
        # 上一次打开时留下的那句结果不属于这一次（尤其失败那句：它留着是为了让人
        # 当场看见，而不是跨几次开关悬挂着）。这里一次清干净。
        self._clear_action_result()
        # 清除上一次固定尺寸，让 adjustSize 能重新计算
        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)
        self._apply_fold_state()
        self.layout().activate()
        self.adjustSize()
        # 显示**之前**先落一次位：窗口在默认位（多半是屏幕角）闪一帧再跳过去，
        # 用户是看得见的。这一次落位在原生窗口已存在时才真的生效（重开的情形）。
        self._reposition(self.height())
        self.show()
        # 从这一刻起这枚窗已经有原生体了：此后每次显示都不再走「首次上屏」那条路，
        # 也就不必再为屏变化让路（见 _request_show）。
        self._ever_shown = True
        # 安全网：adjustSize() 在窗口**从未显示过**时拿到的是过期 sizeHint
        # （隐藏期 layout 不真正重排，真实 layout 要等 show() 之后才跑）→ 首开的
        # 高度会按上一份内容算，内容被拉伸填满、看着偏高。show() 之后再量一次。
        # 这一次落位**才是真的落到原生窗口上**的那一次（见 shell/placement 模块 docstring：
        # 显示前的 move 只写进 Qt 自己的账本，原生窗口建出来时并不按它摆）。
        placed_at = self._relayout_after_show()
        set_window_level(self)
        # 开一条与 hideEvent 那条**配对**的记录：只记「谁关了」不记「什么时候开的」，
        # 日志里就只剩一串孤立的关闭 —— 2026-09-27 那次「点图标面板消失」正是这样，
        # 没有开记录就分不清那一下是「开」还是「关」，只能靠推断。
        log.info(
            "面板显示：矩形=%s 落位=%s 锚点=%s %s",
            self.frameGeometry(),
            placed_at,
            # 锚只在 toggle_at 里设（直接调 _show_at_anchor 的路径没有它）。
            getattr(self, "_anchor", None),
            "收起态" if self._folded else "展开态",
        )
        if self._dismiss is not None:
            self._dismiss.start()
        # 落位不变量的守卫：此刻窗口已显示、层级已提、关闭控制器已起 —— 问原生窗口
        # 是不是真在算出来的地方。不一致会原生纠正并记日志（见 verify_placement）。
        placement.verify_placement(self, placed_at)
        # 落位不变量的**独立**复核：上一句读的是自己刚写进去的值（``NSWindow.frame()``），
        # 结构上发现不了「窗口服务器根本没采纳这次落位」。隔一小段再问**窗口服务器**
        # （它对移动的读数有滞后）—— 见 ``_report_placement_on_screen``。
        QTimer.singleShot(PLACEMENT_RECHECK_MS, self._report_placement_on_screen)
        # 落位不变量的二次确认：本轮 show 序列（含 ``set_window_level``）刚跑完，原生窗口
        # 可能在当前同步点尚未完全 realized —— 下一轮事件循环再确认一次，仍分叉就原生纠正
        # 并记日志。这是「落位是要一直成立的状态」的收口，不是又一处补丁（2026-09-30）。
        QTimer.singleShot(0, self._settle_placement)
        # 面板已经打开 ⇒ 报告给框架（框架把它发成 ``app.panel_opened``，按需型组件据此
        # 重跑一轮，见 ``set_open_notifier``）。放在**收尾**：此刻窗口已定位、层级已提、
        # 关闭控制器已起，报文说的「打开了」与用户看到的完全一致。回调只登记意图
        # （契约要求 handler 只 mark_dirty），不会在这里做耗时的事。
        self._notify_opened()

    def _settle_placement(self) -> None:
        """show 序列收尾后的落位二次确认（见 ``_show_at_anchor`` 末注）。

        原生窗口在 ``show()`` 之后未必同一帧就 fully realized，``set_window_level``
        也可能在原生侧异步落定；若此刻原生 frame 与算出来的点分叉，``place_window``
        推原生 ＋ ``verify_placement`` 纠正会把窗口拉回正确位置并记日志。窗口已经到位时
        这一步零副作用（Qt 账本已等于目标，``move`` 短路、原生推的是同一个点）。
        """
        placed_at = self._reposition()
        placement.verify_placement(self, placed_at)

    def _expected_point(self) -> "QPoint":
        """按当前锚与内容算一遍**该落在哪** —— 只算，不动窗口。

        独立核对要拿一个目标点去比，而这里**不能**顺手 ``_reposition``：那会在核对失败时
        先默默动一次窗（用户看得见的一跳），失败之后照样得重建。算式与 ``_reposition``
        同一处（``shell/placement``），所以不会漂。
        """
        pt, _ = placement.target_top_left(
            self,
            placement.Intent.BELOW_ICON,
            anchor=getattr(self, "_anchor", None),
            inset=self.SHADOW_MARGIN,
        )
        return pt

    def _report_placement_on_screen(self) -> None:
        """``show()`` 之后的**独立**核对：问窗口服务器（用户所见）是不是真在算出来的地方。

        为什么必须另有一口：``verify_placement`` 回读的是 ``NSWindow.frame()`` —— 我们自己
        刚 ``setFrameOrigin_`` 写进去的那个值，写读互为逆运算，对「服务器根本没采纳这次
        落位」**恒判通过**。2026-10-14 定案的形态正是如此：NS 报 ``(934,42)``、窗口服务器
        把这扇窗摆在 ``(0,0)``、用户看到的也在 ``(0,0)``。

        **只报不救。** 这一档在进程内救不回来（2026-10-14 真机把十种手段逐条试过，全无效
        且永久），所以这里只让 ``placement.verify_on_screen`` 记一条警告。真正的出路是
        **不在屏变化的窗口期里建窗**（见 :mod:`barkit.shell.screen_settle`）—— 窗一旦在
        那一刻诞生，位置就再回不来了。
        """
        if not self.isVisible():
            return
        placement.verify_on_screen(self, self._expected_point())

    def _notify_opened(self) -> None:
        """调「刚被打开」的通知口；未注入 / 回调出错都只当这一次没有通知。"""
        notifier = self._open_notifier
        if notifier is None:
            return
        try:
            notifier()
        except Exception:  # noqa: BLE001 —— 订阅方出错不得让面板停在半开状态
            log.exception("面板打开通知的回调异常（已隔离）")

    def _relayout_after_show(self) -> "QPoint":
        """show() 之后按真实（post-show）几何重排一次，修正隐藏期算错的尺寸。返回落位点。

        只对首开（窗口此前从未显示）在**尺寸**上有实际作用；之后 sizeHint 已是实时的，
        本方法等价于一次普通 _relayout —— 但**落位那一步每次都要走**：显示前的 move
        落不到原生窗口上，这一次才是真的那一次。
        """
        self.layout().activate()
        # show() 之后才量得到「固定部分」的真实高度（见 _fixed_chrome_height），
        # 故这里再定一次滚动容器的高度，首开也不会多出一帧超高。
        self._apply_status_scroll_size()
        self.adjustSize()
        return self._reposition(self.height())

    def teardown(self) -> None:
        """退出清理：停失联定时器、断主题信号、停原生 dismiss 控制器、隐藏自身。

        ``_shutdown`` 此前完全没碰 UI 层，遗留两件致命泄漏：

        - 3s 的 ``_stale_timer`` 永不停止，收尾后仍在跑，被 ``processEvents`` 调到
          会访问已死的面板内部（``pytest-qt`` 在用例间 ``processEvents`` 即踩崩源）；
        - 面板在 ``styles.theme_changed`` 上的连接收尾不断开，后续任何换肤都回调进死 widget。

        此外 ``_dismiss`` 是原生 ``NSNotificationCenter`` 观察者，不 ``stop()`` 会在
        app 失活时回调进死面板 → 真机 SIGSEGV。三件一并拆掉，再 hide（hide 不停止
        定时器，故必须先 ``stop``）。widget 本体不 ``deleteLater``——保留引用供测试
        在收尾后断言，但已无活跃定时器 / 连接 / 观察者，不会再有回调触发。
        """
        try:
            self._stale_timer.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            styles.theme_changed.disconnect(self._on_theme_changed)
        except Exception:  # noqa: BLE001
            pass
        if self._dismiss is not None:
            try:
                self._dismiss.stop()
            except Exception:  # noqa: BLE001
                pass
            self._dismiss = None
        self.hide()
