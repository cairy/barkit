"""shell/detail：L3 详情页托管（UI 交互分层规范 v2 §5.4）。

**分流语义**（§5.4 / §5.5，决定什么东西该放哪里）：

- **详情页（本模块）**：用户**常规要去用的操作面（高频任务流）**——隧道诊断、
  守护安装 / 卸载、规则编辑这类「每次用都要走一遍」的动作；
- **设置页（SettingsPageSpec）**：第一次设置后基本不动的低频一次性配置。

形态：由 ``DetailEntrySpec.presentation`` 声明（决议 D26），内容由插件 ``DetailEntrySpec.build()``
提供 —— **两种形态接收同一个内容 widget**，差别只在框架给多大的壳、以及窗户长什么样：

- ``popover``（默认）：**贴着菜单栏图标弹出的浮层卡**（圆角自绘 ＋ 固定宽），点外部即关；
- ``window``：**原生标题栏的独立窗**（居中、可缩放、关闭键与系统一致）。

**框架托管、插件不参与的四件事**（§5.4「插件只 build()」）：

1. 外壳：浮层卡是圆角卡片 + 标题 + 「完成」；原生窗把这些交给系统（标题栏就是标题）；
2. 显示：浮层卡**置顶（原生浮层层级）+ 点外部关闭**（复用 ``PanelDismissController`` 的
   200ms 竞态处理，与面板同一套，避免点开即关）；**原生窗不做点外部关闭** —— 一个居中的
   独立窗被点一下就没了，比不给关更危险；
3. Dock：弹出时 ``POLICY_REGULAR`` 出现 Dock 图标（模态任务流要能从 Dock /
   Cmd+Tab 找回），关闭后复原 ``POLICY_ACCESSORY``（菜单栏 App 常态无 Dock）；
4. 定位：浮层卡从菜单栏图标附近弹出（``placement.Intent.BELOW_ICON``，与设置窗同源）；
   原生窗**居中**在触发它的那块屏上（``placement.Intent.CENTER``，与识别窗口同一种
   「独立窗」的长相）。算式与认屏都在 ``shell/placement`` 一处。

声明了 ``DetailEntrySpec.save``（决议 D35）的窗体，第 1 条再多一件：多画一颗「保存」键
＋页脚一条行内反馈槽，并在**用户关窗**时拦一下「还有没保存的改动」。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from barkit.contribution import (
    DETAIL_PRESENTATION_POPOVER,
    DETAIL_PRESENTATION_WINDOW,
    FormSaveSpec,
)
from barkit.shell import macos_window, placement
from barkit.shell.design import metrics
from barkit.shell.macos_window import (
    DETAIL_LEVEL,
    POLICY_ACCESSORY,
    POLICY_REGULAR,
    create_dismiss_controller,
    set_window_level,
)
from barkit.shell.ui_kit import (
    PLAIN_ZONE,
    Card,
    Divider,
    EditorActionBar,
    EditScreen,
    EditStack,
    FeedbackBar,
    apply_text_role,
    tool_button,
)

log = logging.getLogger(__name__)

#: 「高度不设上限」的哨兵（``QWIDGETSIZE_MAX``）。`setMaximumHeight` 只能给数字，
#: 要撤销限制就得给这个 —— 给 0 或 None 都不行。
_UNLIMITED = 16777215


def _scrolled(content: QWidget, parent: QWidget) -> QScrollArea:
    """把详情页内容放进滚动区，让**窗口变矮时走滚动、而不是把控件压扁**。

    为什么这层归框架：窗的壳（多宽、多矮、能不能拖）是框架给插件定死的，而内容多高
    由插件自己决定 —— 两边的下限对不上时，Qt 的做法是**把内容一路压缩**。实测
    route_guard 的场景编辑器自然高度约 575px、最小约 500px，而窗口下限只有
    ``WINDOW_MIN_HEIGHT`` = 320px：压到 420px 时四个输入框只剩 13px 高（字被挤掉
    一半），压到 360px 只剩 9px —— **不报错、不断言，只是界面变形**，插件侧既看不到
    也管不着。外包滚动区之后控件保持自己的高度，矮窗只是出现滚动条
    （``widgetResizable`` 只把内容**放大**到视口，不会缩到自然高度以下）。

    窗口够高时这层**逐像素无影响**：``widgetResizable`` 会把内容摊到视口大小，窗口
    默认高度正好取内容的自然高度（两条对得上，故滚动条不出现）。这也是
    ``show_at`` 里那句「内容照常可滚动」的兑现 —— 写那句话时滚动区还不存在。

    页脚（保存 / 完成）**不在这层里**：它是框架画的，必须永远够得着。
    写法与设置窗的插件页同法（``shell/settings.py::_scrolled``）：NoFrame ＋ 关横向
    滚动 ＋ ``widgetResizable``；底色不另设，交给全局 QSS 的 ``QWidget`` 规则，
    与窗口底色同源（不出现「窗里嵌一块异色矩形」）。
    """
    area = QScrollArea(parent)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setWidget(content)
    return area


class DetailForm(QDialog):
    """框架托管的详情页外壳：只接收「标题 + 内容 widget」两样东西。

    两种形态（决议 D26），由 ``DetailEntrySpec.presentation`` 选，**按内容宽度一开始
    就选**（先按 ``popover`` 设计、放不下再改，代价是表单要重排）：

    - ``popover``（默认）：固定宽 ``WIDTH``，尺寸随内容一次定死 —— 「一屏能平铺完」
      的内容用它，零行为变更（与加该字段之前完全一致）。frameless ＋
      ``WA_TranslucentBackground``（圆角由 ``Card`` 自绘：QSS 的 border-radius 在
      macOS 上不裁剪普通 QWidget 背景，永远直角），置顶、点外部即关。
    - ``window``：初宽 ``WINDOW_WIDTH``（**声明可用 ``DetailEntrySpec.width`` 覆盖**：
      内容宽的页面——如七列的 hosts 表——在默认宽里会把工具条挤变形）、下限
      ``WINDOW_MIN_*``、**开放缩放**（右下角拖拽手柄）。
      **走原生标题栏**（2026-09-18 用户裁定）：关闭键、双主题下的窗体底色、窗口阴影
      全归系统，我们不自绘。原先它也是 frameless 浮层卡 —— 那种长相**没有关闭键**，
      而这时候外面还挂着一个「点外部即关」的浮层语义，两个都不像用户见过的编辑器。
      内容直接进窗口布局（不自绘圆角卡片、不再在窗内重复一遍标题：标题栏上有），
      并且**外包一层滚动区**（`_scrolled`）：窗口能拖到 320px，而插件内容有它自己的
      自然高度 —— 不包就会把控件压扁。

    形态只决定这儿给的壳多大，插件侧 ``build()`` 一字不差：两种形态接收同一个内容
    widget。取值本身在 ``DetailEntrySpec`` 的构造期已校验，这里只按两个取值分支。

    ``save``（决议 D35）**只有 window 形态收**（声明组合在 ``DetailEntrySpec`` 的
    构造期已校验）：多一颗「保存」键 ＋ 页脚一条行内槽，并在用户关窗时拦一下。
    """

    WIDTH = 380                     # popover：固定宽（列表行不挤压的前提下的一屏内平铺）
    WINDOW_WIDTH = 620              # window：初宽（「左列表 + 右表单」实测 560–640）
    WINDOW_MIN_WIDTH = 480          # window：缩放下限（再窄就退化成两层叠放，不如不给拖）
    WINDOW_MIN_HEIGHT = 320

    def __init__(self, title: str, content, parent=None,
                 presentation: str = DETAIL_PRESENTATION_POPOVER,
                 save: "FormSaveSpec | None" = None,
                 width: int | None = None,
                 plugin_id: str = "") -> None:
        super().__init__(parent)
        # 归属哪个插件 —— ``DetailHost`` 靠它判「再来一次打开请求时是复用还是重开」
        # （S13：两个入口收敛到同一份内容面）。空串 = 不带归属（直接 new 的场合）。
        self.plugin_id = plugin_id
        self.setWindowTitle(title)
        self._content = content
        self._save = save
        # window 形态的初宽：声明了就用它的（内容宽的页面自己知道该多宽），
        # 否则用 ``WINDOW_WIDTH`` 那个默认值。
        self._window_width = width or self.WINDOW_WIDTH
        self._is_window = presentation == DETAIL_PRESENTATION_WINDOW
        # 关窗守卫的两把锁：`_confirmed` = 这次关窗已经问过用户了（保存 / 放弃）；
        # `_force` = 框架自己关的（换页 / 退出），不拦 —— 拦了会让「点另一行没反应」。
        self._confirmed = False
        self._force = False
        # 浮层卡：无边框 + 透明底 + 点外部即关。原生窗：三样都不要。
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self._dismiss = None
        if self._is_window:
            self.setWindowFlag(Qt.WindowType.FramelessWindowHint, False)
        else:
            self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self._dismiss = create_dismiss_controller(self)
        # 是否真的切到了 Regular（决定 hideEvent 要不要复原 Accessory）
        self._restore_accessory = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        if self._is_window:
            # 原生窗：内容直接进窗口（自绘卡片在系统窗体里是一块突兀的圆角白块）
            inner = QVBoxLayout()
            inner.setContentsMargins(16, 14, 16, 14)
            inner.setSpacing(10)
            outer.addLayout(inner)
            host = self
        else:
            card = Card(radius=10)
            outer.addWidget(card)
            inner = QVBoxLayout(card)
            inner.setContentsMargins(18, 14, 18, 14)
            inner.setSpacing(10)
            head = QLabel(title, card)
            apply_text_role(head, "detail_title")
            inner.addWidget(head)
            inner.addWidget(Divider(card))
            host = card
        # 内容外面那层壳：原生窗**必须**外包滚动区，浮层卡不要（见 ``_scrolled``）。
        self._content_scroll: "QScrollArea | None" = None
        if self._is_window:
            self._content_scroll = _scrolled(content, host)
            inner.addWidget(self._content_scroll, stretch=1)
        else:
            content.setParent(host)
            inner.addWidget(content, stretch=1)

        # 本窗**唯一**那条反馈槽：编辑屏被托管时它指向**台上那一屏**交出的那条（跟着
        # 换屏走，见 :meth:`_sync_editor_row`）；没人托管时才在 ``save`` 声明下自建一条。
        self._fb: FeedbackBar | None = None
        # 认领内容里每一块编辑屏的动作行（详情页只有一行动作位，见 :meth:`_adopt_editor_actions`）。
        self._editor_bars: "dict[EditScreen, EditorActionBar]" = {}
        self._editor_rows: "dict[EditScreen, QWidget]" = {}
        self._editor_stack: "EditStack | None" = None
        self._row_stack: "QStackedWidget | None" = None
        self._adopt_editor_actions(content, host)

        foot = QHBoxLayout()
        if self._row_stack is not None:
            # 台上那一屏的动作行已经含它自己那条反馈槽 ⇒ 本窗不再另建（一屏只许一条）。
            foot.addWidget(self._row_stack, stretch=1)
        elif save is not None:
            # 反馈槽在页脚、与按钮同一行：它承载「已保存 / 保存失败」与关窗那一次询问
            # （`FeedbackBar` 的存在理由见 ui_kit；框架壳里同样禁掉模态框）。
            self._fb = FeedbackBar(host)
            foot.addWidget(self._fb, stretch=1)
        else:
            foot.addStretch(1)
        self._save_btn = None
        if save is not None:
            # ``save``（FormSaveSpec）与编辑屏动作**今天不会同时出现**：声明 save 的
            # route_guard 场景编辑器不是编辑屏。真撞上时框架这枚「保存」排在编辑屏那一行
            # 之后、「完成」之前 —— 次序理由与 ``EditorShell`` 同款（主动作贴右缘）。
            self._save_btn = tool_button("保存", "pill-ghost", parent=host)
            self._save_btn.clicked.connect(self.save_now)
            foot.addWidget(self._save_btn)
        done = tool_button("完成", "pill-ghost", parent=host)
        done.clicked.connect(self.close)
        foot.addWidget(done)
        inner.addLayout(foot)

        if self._is_window:
            # 可缩放：给下限 + 初宽 + 右下角拖拽手柄。**不设上界** —— 谁来拖都行。
            self.setMinimumSize(self.WINDOW_MIN_WIDTH, self.WINDOW_MIN_HEIGHT)
            self.setSizeGripEnabled(True)
            # 初高按**内容自己的自然高度**算，不能拿 `self.sizeHint()`：滚动区报的是
            # 内容下限附近的尺寸（实测只有内容的约 2/3），用它当窗高等于**一开窗就带着
            # 滚动条**（屏幕明明装得下）。
            # chrome = 框架自己画的那几条（上下边距 + 一处间距 + 页脚那一行）。
            margins = inner.contentsMargins()
            chrome = (margins.top() + margins.bottom() + inner.spacing()
                      + foot.sizeHint().height())
            self.resize(
                self._window_width,
                max(self.WINDOW_MIN_HEIGHT, content.sizeHint().height() + chrome),
            )
        else:
            self.setFixedWidth(self.WIDTH)
            self.adjustSize()

    # ---------- 认领内容里的编辑屏动作行 ----------

    def _adopt_editor_actions(self, content: QWidget, host: QWidget) -> None:
        """内容子树里的编辑屏 ⇒ 认领它们各自的动作行，拼成**本窗那唯一一行**。

        用户 2026-10-16 上报「编辑按钮与窗体组件按钮两行叠层」：窗体页脚已经有一行
        （「完成」），编辑屏再画一行（「删除 …… 确定」）就是上下两行、各夹各的反馈条。

        **为什么由窗体壳找屏**，不是屏找窗体壳：``EditScreen`` 在 ``ui_kit``、本类在
        ``shell``，反向 import 撞分层方向门禁；而且**时序上也不成立** —— 内容是插件
        ``build()`` 先造好的（``DetailForm(entry.label, entry.build(), …)``），屏构造
        那一刻父链上还没有本窗体。

        内容里可能挂着**不止一块**编辑屏（hosts 的表单页与导入页同在一棵子树里、换着
        上屏），故每块拼一行、用 ``QStackedWidget`` 摞起来，换屏时切到对应那一页 ——
        一个详情窗只有一处动作位，台上哪一屏就显示哪一屏的动作。
        """
        screens = content.findChildren(EditScreen)
        if not screens:
            return
        rows = QStackedWidget(host)
        self._row_stack = rows
        for screen in screens:
            row = self._row_of(screen, host)
            rows.addWidget(row)
            self._editor_rows[screen] = row
        # 换屏时页脚那行得跟着切：`EditStack` 自己发信号（详情窗不该去连它内层的实现）。
        stack = content.findChild(EditStack)
        if stack is not None:
            self._editor_stack = stack
            stack.screen_changed.connect(self._sync_editor_row)
        self._sync_editor_row()

    def _row_of(self, screen: EditScreen, host: QWidget) -> QWidget:
        """把**一屏**的动作行拼成一行（页脚那一格），返回承载它的 widget。

        次序与 ``EditorShell`` 那条同款：破坏性动作贴左缘（离右端主动作越远越好）、
        反馈槽撑住中间、状态位紧贴主动作左边、保存恒在最右。三格一个不少 —— 屏里怎么
        排，这里就怎么排，变的只是**落点**（这就是「谁提供窗体壳，谁提供动作行」）。
        """
        bar = screen.take_action_bar()
        self._editor_bars[screen] = bar
        row = QWidget(host)
        # 裸容器**必须显式透明**：全局那条 `QWidget { background: surface }` 管的是裸实例
        # （自定义子类不画），不声明的话页脚这一格会自带一块比窗底更亮的方块。
        row.setObjectName(PLAIN_ZONE)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(metrics.GAP_MD)
        for btn in bar.destructive:
            lay.addWidget(btn)
        lay.addWidget(bar.feedback, stretch=1)
        if bar.hint is not None:
            lay.addWidget(bar.hint)
        for btn in bar.primaries:
            lay.addWidget(btn)
        return row

    def _sync_editor_row(self) -> None:
        """把页脚那行切成**台上那一屏**的（``EditStack.screen_changed`` 的接收端）。

        台上**不是**编辑屏时（列表屏，或内容里压根没有换屏容器）把那行收起来：编辑屏的
        动作（删除 / 「⋯ 更多」/ 确定）出现在列表屏上就是**多出来的按钮**，而页脚这个
        位置本来就是给编辑屏用的 —— 列表屏的动作长在它自己里面（hosts 表页那一排过滤 /
        动作也是）。收起来之后页脚只剩「完成」，与「没有编辑屏的详情页」逐像素同款。

        ⚠️ 注意 ``QStackedWidget`` 默认显示**第一页**：不在上屏时一定要显式收起来，
        否则打开详情页第一眼看到的列表屏底下就挂着第一块编辑屏的那行按钮。
        """
        rows = self._row_stack
        if rows is None:
            return
        stack = self._editor_stack
        if stack is None:
            # 只有一块编辑屏（没有换屏容器）⇒ 它就在台上。
            screen = next(iter(self._editor_rows), None)
        else:
            current = stack.current()
            screen = current if current in self._editor_rows else None
        if screen is None:
            rows.setVisible(False)
            self._fb = None
            return
        rows.setCurrentWidget(self._editor_rows[screen])
        rows.setVisible(True)
        self._fb = self._editor_bars[screen].feedback
        self._apply_editor_height_limit()

    def _apply_editor_height_limit(self) -> None:
        """编辑屏在台上时，把内容面的高度**钳在视口里**（头尾固定的另一半）。

        编辑屏自带滚动区（见 ``EditorShell`` ②）之后，它内部已经是「头尾固定、内容滚」
        —— 但那还不够：详情窗那层滚动区滚的是**整个内容面**，只要内容面的高度超过视口
        （2026-10-16 实测：hosts 表单页在台上时内容面报 396、视口只有 254），它照样滚，
        编辑屏的返回键照样滚走。钳住内容面的最大高度，余下的高度就由编辑屏内部那个滚动
        区消化。

        **不在上屏时放开**（列表屏 / 表页）：那些页随条目数长，本来就该由外层滚动区滚。
        """
        area = self._content_scroll
        if area is None or self._row_stack is None:
            return
        holder = area.widget()
        if holder is None:
            return
        if self._row_stack.isVisible():
            # 用滚动区自身的高度而**不是** `viewport().height()`：resizeEvent 里 viewport
            # 有时还是旧值（它比滚动区晚一步更新）。
            holder.setMaximumHeight(area.height())
        else:
            holder.setMaximumHeight(_UNLIMITED)

    # ---------- 「保存」＋未保存关窗（决议 D35） ----------

    def save_now(self) -> bool:
        """跑一次声明的 ``commit``，把结果说到页脚那条行内槽里。返回是否写成功。"""
        if self._save is None:
            return True
        try:
            ok, text = self._save.commit()
        except Exception as exc:  # noqa: BLE001
            # 组件的 commit 抛异常不许变成「点了没反应」：笼住并说明（写盘那侧自己
            # 已有原子写与校验，这里兜的是组件自己的逻辑错）。
            log.warning("详情页 commit 抛异常", exc_info=True)
            ok, text = False, f"保存失败：{exc}"
        if self._fb is not None:
            if ok:
                self._fb.info(text or "已保存。")
            else:
                self._fb.error(text or "保存失败。")
        return ok

    def closeEvent(self, event) -> None:  # noqa: N802
        """用户关窗时有没保存的改动 → **拦一下**（问在窗内，零模态）。

        「自己关」与「框架关」要分开：框架关（``DetailHost.close()``：换一个详情页 /
        退出）不拦 —— 否则用户点另一行会看到「没反应」。用户关（关闭键 / Cmd+W /
        「完成」）才拦，而这两个出路都不是空操作：一个存了再关、一个丢改动再关。
        """
        if (self._save is not None and not self._force and not self._confirmed
                and self._fb is not None and self._save.dirty()):
            event.ignore()
            self._fb.ask(
                "有还没保存的改动", "保存并关闭", self._save_and_close,
                cancel_label="放弃改动", on_cancel=self._discard_and_close,
                # 两个都是真动作、都没有破坏性 ⇒ 主要确认那一档（实心品牌紫）。
                confirm_kind="primary",
            )
            return
        super().closeEvent(event)

    def _save_and_close(self) -> None:
        if not self.save_now():
            return          # 存不下去就留在窗内，红字已经说明了原因
        self._confirmed = True
        self.close()

    def _discard_and_close(self) -> None:
        self._confirmed = True
        self.close()

    def force_close(self) -> None:
        """框架侧关窗（换页 / 退出）：**不拦**（见 ``closeEvent``）。"""
        self._force = True
        self.close()

    # ---------- 显示 / 关闭 ----------

    def show_at(self, anchor: "placement.Anchor | None" = None) -> None:
        """显示并定位：浮层卡贴着菜单栏图标，原生窗居中在触发它的那块屏上。

        顺序关键：先 show() 让 macOS 建好原生 frame，再落位（原生窗的
        `frameGeometry` 要等 frame 建好才含标题栏那一条）。落位算式与认屏都在
        ``shell/placement`` 一处，本文件只声明意图。
        """
        self.show()
        if self._is_window:
            # 别让窗体高过所在屏：否则页脚「保存 / 完成」会落到屏外够不着
            # （内容照常可在右栏滚动，夹高度不影响可编辑性）。
            screen = placement.resolve_screen(
                self, placement.Intent.CENTER, anchor=anchor
            )
            if screen is not None:
                ag = screen.availableGeometry()
                max_h = max(self.WINDOW_MIN_HEIGHT, ag.height() - 48)
                # 宽度同样夹进屏：声明了更宽初宽的页面（多列表格）在小屏上会顶出屏幕，
                # 而用户拖不动窗的左边。夹宽不影响可编辑性（内容横向照样可滚 / 可缩放）。
                max_w = max(self.WINDOW_MIN_WIDTH, ag.width() - 48)
                w = min(self.width(), max_w)
                if self.height() > max_h or w != self.width():
                    self.resize(w, min(self.height(), max_h))
            pt, _ = placement.place_window(self, placement.Intent.CENTER, anchor=anchor)
        else:
            pt, _ = placement.place_window(
                self, placement.Intent.BELOW_ICON, anchor=anchor
            )
        self.raise_()
        self.activateWindow()
        placement.verify_placement(self, pt)

    def showEvent(self, event) -> None:  # noqa: N802
        # 原生浮层层级：高于面板，避免被面板遮挡
        try:
            set_window_level(self, DETAIL_LEVEL)
        except Exception:  # noqa: BLE001
            log.warning("详情页 set_window_level 失败，层级可能偏低", exc_info=True)
        # Dock 图标随详情页出现（§5.4）；关闭时由 hideEvent 复原
        self._restore_accessory = macos_window.set_activation_policy(POLICY_REGULAR)
        if self._dismiss is not None:
            self._dismiss.start()
        super().showEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        if self._restore_accessory:
            macos_window.set_activation_policy(POLICY_ACCESSORY)
            self._restore_accessory = False
        if self._dismiss is not None:
            self._dismiss.stop()
        super().hideEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        """窗口一变高矮就重钳一次内容面的高度（那个上限是**视口**高，跟着走）。"""
        super().resizeEvent(event)
        self._apply_editor_height_limit()


class DetailHost:
    """按 plugin_id 打开其 ``DetailEntrySpec`` 声明的详情页（框架侧唯一入口）。

    之所以要有个托管器而不是让 app 直接 new DetailForm：详情页是**单例**语义
    （同一时刻只应有一个 L3 任务流），且「插件是否声明了详情页」只能在打开时
    按注册中心现查（运行期插件可启用 / 禁用 / 卸载，入口集合会变）。
    """

    def __init__(self, registry) -> None:
        self._registry = registry
        self._form: "DetailForm | None" = None

    def _entry(self, plugin_id: str):
        """现查该插件的详情页入口；注册中心未提供该扩展点时返回 None。"""
        collector = getattr(self._registry, "detail_entries", None)
        if collector is None:
            return None
        for entry in collector():
            if entry.plugin_id == plugin_id:
                return entry
        return None

    def available(self, plugin_id: str) -> bool:
        """该插件是否声明了可构建的详情页入口（决定整行可点性，§5.3）。"""
        entry = self._entry(plugin_id)
        return entry is not None and entry.build is not None

    def open(self, plugin_id: str, anchor: "placement.Anchor | None" = None) -> bool:
        """打开某插件的详情页；**同一个插件已经在开着就复用**（S13）。

        复用那条分支是这次加的全部内容，也是「两个入口各开一份内容面」的解法：
        面板整行、快捷按钮、插件自己（``Plugin.open_detail``，S18）现在指向同一份
        内容面 —— 谁改的对方都看得见。改之前这里无条件 ``close()`` 再重建，于是
        第二个入口一按，第一个入口里**还没保存的改动**当场蒸发，而两个入口都不知道
        对方存在过。

        不同 plugin_id 仍然关掉旧的再开新的：详情页是**单例**语义（同一时刻只应有
        一个 L3 任务流，决议见类 docstring），两扇窗并排会让「点另一行」看着像没反应。

        ``anchor`` 只在**新建**时用（复用时窗已经在那儿了，再挪一次是跟用户的鼠标
        位置过不去）。
        """
        entry = self._entry(plugin_id)
        if entry is None or entry.build is None:
            log.warning("插件 %s 未声明可构建的 DetailEntrySpec，忽略详情页请求", plugin_id)
            return False
        existing = self._form
        if (existing is not None and existing.plugin_id == plugin_id
                and existing.isVisible()):
            existing.raise_()
            existing.activateWindow()
            return True
        self.close()
        # 约定与 SettingsPageSpec.build 一致：无参工厂，返回内容 widget。
        # 壳多大由声明的 presentation 决定（决议 D26）；直接读字段、不做 getattr 回落
        # ——声明点缺字段是契约违约，静默按浮层卡开会让作者以为 window 生效了。
        # `save` 同样是直读 + 直传：声明组合（只有 window 能用）已在构造期校验。
        form = DetailForm(entry.label or plugin_id, entry.build(),
                          presentation=entry.presentation, save=entry.save,
                          width=entry.width, plugin_id=plugin_id)
        self._form = form
        form.show_at(anchor)
        return True

    def close(self) -> None:
        """框架侧关窗（换一个详情页 / 退出）：**不触发未保存询问**。

        拦在这里会让「点另一行」表现为「点了没反应」—— 用户根本看不出为什么。
        用户自己关窗（关闭键 / 「完成」）那一路才问。
        """
        if self._form is not None:
            form, self._form = self._form, None
            form.force_close()
