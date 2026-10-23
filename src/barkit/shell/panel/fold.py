"""``_FoldMixin`` —— 折叠动画与状态区滚动适配（含 ``statusMaxH`` 存取器）。

``shell/panel.py`` 拆包（2026-10-01）：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtCore import (
    QPoint,
    QTimer,
    Signal,
    QPropertyAnimation,
    QEasingCurve,
)
from PySide6.QtGui import QGuiApplication

from barkit.shell import placement

from .constants import QWIDGETSIZE_MAX, log


class _FoldMixin:
    """折叠动画与状态区滚动适配（含 ``statusMaxH`` 存取器）。

    与 ``StatusPanel`` 同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 折叠（平滑收缩 / 展开）----------

    def _dispatch(self, signal: Signal) -> None:
        """C 区固定动作（设置 / 退出）的统一入口：先关主面板，再发射信号。

        动作由 app 层接线到具体回调，面板本身不持有插件引用；发射前先 hide 自己，
        保证「开新窗口、关主面板」的一致体验。A 区快捷按钮（截图识别 / 一键输入）
        经 set_primary_actions 直接回调，不经过本信号。
        """
        log.info("面板关闭［动作栏：让位给设置窗 / 退出］")
        self.hide()
        signal.emit()

    def toggle_fold(self) -> None:
        self._folded = not self._folded
        self._animate_status(self._folded)

    def _animate_status(self, target: bool) -> None:
        """200ms 高度过渡（设计 §7.2 要求的「过渡 200ms」）。

        无边框 Window 下顶层窗口高度可实时 resize（Popup 在 macOS 上会忽略「可见时」
        的 resize/adjustSize，导致折叠「缩不了」），故动画状态区的 maximumHeight
        （0↔自然高度），每帧 adjustSize 让窗口随内容收缩、并重新钉住顶边（图标下方），
        卡片无空白间隙。
        """
        # 展开的终点高度取「状态区自然高度」，故先给里面的滚动容器定好高度：否则
        # 一大屏装不下的内容会把窗口顶出屏幕，等动画收尾再缩回来（一次可见的跳变）。
        self._apply_status_scroll_size()
        sec = self._status_section
        start = sec.height()
        full = sec.sizeHint().height()
        end = 0 if target else full
        if self._anim is not None and self._anim.state() == QPropertyAnimation.Running:
            self._anim.stop()
        self._anim = QPropertyAnimation(self, b"statusMaxH")
        self._anim.setDuration(200)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.setStartValue(start)
        self._anim.setEndValue(end)
        # 动画结束后解除高度上界（见 _sync_status_cap）：否则 maximumHeight 停在
        # 终点值，之后内容再增长（熔断条/新增行/副标题变长）会被钳住裁掉。
        self._anim.finished.connect(self._on_fold_anim_finished)
        self._anim.start()

    def _on_fold_anim_finished(self) -> None:
        """折叠/展开动画收尾：解除上界并按**当前**内容重排一次。

        动画期间内容可能已变（如刚好熔断），终点高度是按动画开始时的 sizeHint
        算的，收尾必须再按现时内容量一次高度，否则仍会差一截。
        """
        self._sync_status_cap()
        if self.isVisible():
            self.layout().activate()
            self.adjustSize()
            self._reposition(self.height())
        # 下探（§5 #27 尾巴）必须排在重排**之后**，而且还要**再等一拍**：上面这几步
        # 只是把窗口尺寸改掉，滚动区的 viewport 要等 Qt 跑完这一轮布局才落定 —— 当场
        # ``ensureWidgetVisible`` 会按「还没定完」的滚动范围去滚，差的那几像素正好把
        # 目标行留在可视区外（实测末行差 13px，正好差出一行的高度）。一拍＝毫秒级，
        # 用户看不到，却让定位一次到位。取完即清，免得之后任何一次折叠动画收尾都去滚
        # 一遍上次那个组件。
        pending, self._pending_reveal = self._pending_reveal, None
        if pending is not None and not self._folded:
            QTimer.singleShot(0, lambda pid=pending: self._reveal_row(pid))

    def _apply_fold_state(self) -> None:
        """把当前 ``_folded`` 落到控件上：状态区上界 + 分隔线显隐。

        只在「静止期」调用（构造 / 首显 / 内容重排）。动画进行中由
        ``_set_status_max`` 逐帧接管高度，别在这里跟它抢。
        """
        self._status_section.setMaximumHeight(0 if self._folded else QWIDGETSIZE_MAX)
        self._div_status_action.setVisible(not self._folded)

    def _sync_status_cap(self) -> None:
        """静止期把状态区高度上界同步到当前折叠态（展开→解除，折叠→收紧）。

        BUG：折叠动画把 ``maximumHeight`` 停在终点值后**没人复位** → 展开之后再
        增长的内容（熔断红条出现 / 运行期启用插件多一行 / 某行副标题由空变有）
        被钳在动画终点高度，多出来的部分直接被裁掉。

        ⚠️ 边界：``maximumHeight`` 只是**上界，只挡增长、不挡收缩**。所以本修复
        **解不了**「内容减少后面板不收起」那类收缩问题（8-27 那次回退就是把它俩
        混为一谈，看到「没变化」就整包放弃了）。收缩另有根因，别指望一并解决。
        """
        if self._anim is not None and self._anim.state() == QPropertyAnimation.Running:
            return
        self._apply_fold_state()
        # 展开态的内容变化也要顺带重算滚动容器的高度：否则新增的行会先把窗口顶出
        # 屏幕，要等下一次重排才收回滚动区（§5 #28）。
        self._apply_status_scroll_size()

    # ---------- 状态区滚动（§5 #28） ----------

    def _available_panel_height(self) -> int:
        """面板能占的竖直空间；拿不到屏幕时返回 0（＝不设上界）。

        取锚点所在屏的**可用区**高度（已排除菜单栏 / Dock）。用可用区而不是「锚点下方
        那一段」：`place_window` 本来就允许窗口在装不下时上移（clamp 进屏），
        故真正的硬约束是屏幕本身。
        """
        anchor = getattr(self, "_anchor", None)
        if anchor is not None:
            # **现解**锚屏：缓存过的那枚 QScreen 在屏变化后会变成死对象，碰它就抛
            # （见 placement.resolve_anchor_screen）。重认不出来按下面的 0 处理。
            scr = placement.resolve_anchor_screen(anchor)
        else:
            scr = QGuiApplication.primaryScreen()
        if scr is None:
            return 0
        # 两端各留 4px：让卡片与屏幕边沿留出一点缝，别真贴死（与 `placement.ANCHOR_GAP`
        # 同一个口径）。
        return max(0, scr.availableGeometry().height() - 2 * self.SHADOW_MARGIN - 8)

    def _fixed_chrome_height(self) -> int:
        """面板里**不参与滚动**的那一截高度（窗口高 − 滚动容器高）。

        含标题栏 / 两条分割线 / 底部栏 / 各处内外边距，也含状态区内不滚的熔断条 ——
        凡是「滚不走」的就算在这里，这样 `滚动上界 = 屏幕可用高 − 本条` 一算出来，
        窗口总高就正好落在屏幕内。

        量法：滚动容器是唯一会长高的块，故两者高度都非 0 时直接相减。折叠态（滚动
        容器高为 0）与首帧量不出来，按各部件 sizeHint 估 —— 估值只影响「首次展开
        那一瞬间」的高度，展开动画收尾时会被实测值纠正。
        """
        sc = self._status_scroll
        if sc.height() > 0 and self.height() > sc.height():
            return self.height() - sc.height()
        sec = self._status_section
        outer = self._container.layout()
        om = outer.contentsMargins()
        sm = sec.layout().contentsMargins()
        # 动作结果槽：平时 hide（零贡献），有人说话时才占一截（see present_action_result）。
        rb = self._result_bar.sizeHint().height() if self._result_bar.isVisible() else 0
        return (
            om.top()
            + om.bottom()
            + rb
            + self._head.sizeHint().height()
            + 1
            + 1                                     # 标题下 / 状态区下两条 1px 分割线
            + self._footer_bar.sizeHint().height()
            + sm.top()
            + sm.bottom()                           # 状态区自己的上下内边距（不滚）
        )

    def _apply_status_scroll_size(self) -> None:
        """给滚动容器定高：`min(内容自然高度, 屏幕给得起的上限)`。

        **必须是「定高」而不是「只给上界」**（2026-09-18 实测）：只设 `maximumHeight`
        时，滚动容器的 `sizeHint` 仍随「要不要出滚动条」浮动，而窗口高度又是由各部件
        sizeHint 加出来的 —— 两者互相反馈，每趟差 1px（实测：内容 128 / 可视区 127，
        于是冒出一条毫无意义的滚动条，还白吃掉 17px 宽）。定高之后这条环断开：布局
        没有「从滚动区偷 1px」的余地（它的 min == max），高度一次算准。

        上限之外的行走滚动条；屏幕够高时 `min()` 取到内容高度，于是**逐像素等于**
        「没有这层滚动容器」的今天（滚动条不出现）。
        """
        budget = self._available_panel_height()
        if budget <= 0:
            # 拿不到屏幕信息（无 QGuiApplication）：退回「随内容长高」，不设约束
            self._status_scroll.setMinimumHeight(0)
            self._status_scroll.setMaximumHeight(QWIDGETSIZE_MAX)
            return
        cap = max(self.MIN_STATUS_H, budget - self._fixed_chrome_height())
        natural = self._status_list.sizeHint().height()
        self._status_scroll.setFixedHeight(min(natural, cap))

    def _get_status_max(self) -> int:
        return self._status_section.maximumHeight()

    def _set_status_max(self, v: int) -> None:
        v = int(round(v))
        # 折叠（v→0）时一并隐藏「状态↔操作」分隔线，避免与标题下横线形成「双线」。
        self._status_section.setMaximumHeight(v)
        self._div_status_action.setVisible(v > 0)
        # 无边框 Window 下 adjustSize 可靠：窗口随内容收缩/展开，卡片无空白间隙
        # （容器内容自适应高度，窗口高度==卡片高度，不残留空白）。
        self.adjustSize()
        # 顶边钉在图标下方，高度变化时底边内收（不跳动）。
        self._reposition(self.height())

    def _reposition(self, _h: int | None = None) -> "QPoint":
        """按锚点把窗口落到图标下方（顶边钉住、高度变化时底边内收）。返回落位点。

        高度参数**不参与算式**：``place_window`` 读的是窗口**当前**的 ``height()``，而所有
        调用点都在 ``adjustSize()`` 之后 —— 传进来的那个数与 ``self.height()`` 是同一个。
        留着它只是让调用点读起来仍是「按新高度重排」。

        落位算式与认屏全在 ``shell/placement`` 一处（此前本文件自带一份，是「所有弹窗
        各写一份兜底」的其中一份）。
        """
        pt, _ = placement.place_window(
            self,
            placement.Intent.BELOW_ICON,
            anchor=getattr(self, "_anchor", None),
            inset=self.SHADOW_MARGIN,
        )
        return pt

    def _relayout(self) -> None:
        """内容变化时重排：无边框 Window 下 adjustSize 实时可靠，直接重算即可。"""
        self._sync_status_cap()  # 展开态先解除上界，否则增长的内容会被裁
        if self.isVisible():
            self.layout().activate()
            self.adjustSize()
            self._reposition(self.height())
        else:
            self.adjustSize()
