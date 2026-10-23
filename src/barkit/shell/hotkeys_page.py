"""shell/hotkeys_page：设置窗「快捷键」页（框架固定页，跨组件）。

**为什么单独一页**：此前快捷键只能在 YAML 里改（``hotkeys.<名字>``），而它**横跨所有
组件** —— 放进任一组件的设置页都会变成「同一个全局开关在 N 个组件页各有一份」。
与「维护」页同款：它是框架级页，不属于任何组件。

**三类来源，三种可改范围**（页面把这条写进说明行，因为它决定了删除键有没有）：

- 框架内置 / 组件声明 —— **可改键、可停用、可初始化（恢复出厂键位）、不可删**
  （删了下回启动又长回来）；
- 用户自建 —— 可改键、可停用、可删，另有命令与测试（在详情屏里）。

**这一页不碰两条东西**：不碰注册表（改键的成败由装配器回话），不碰配置文件
（写盘走 ``hotkey_catalog`` 那一组函数）。它只做「展示 + 收集意图 + 把结局说出来」。

**反馈只有列表卡内那一槽**（零模态）：改键撞车、挂不上去，都在那里红字说，不弹框。
换页 / 重建会整页重造，故不需要「摘引用」那套（与组件页的框架区不同）。

**列表屏的排版与其它设置页同一套**（2026-09-24）：页首一句说明之后是一张
``settings_section`` 分区卡，工具条 / 行 / 反馈槽**都在卡里** —— 卡外的裸行没有
归属，用户看不出哪几行是一件事。这张卡 ``stretch=1`` 铺满本屏，于是列表屏与详情
屏（那一屏自己就是一块面）是同构的，换屏时底不跳层。

**列表屏与详情屏是页内换屏**（``QStackedWidget``）：详情屏要放命令输入框与测试
结果，列表行塞不下；开新窗违反零模态，叠一层会挡住列表 —— 换屏是唯一既不弹窗
又给得下地方的排法（与 hosts 管理窗同款）。
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from barkit.contribution.specs import IconName
from barkit.core import thread_reap
from barkit.core.hotkey_command import CommandRunner
from barkit.shell.design import metrics
from barkit.shell.hotkey_capture import ComboButton
from barkit.shell.hotkey_catalog import (
    FRAMEWORK_ACTIONS,
    REBIND_FAILED,
    SOURCE_ALL,
    SOURCE_CUSTOM,
    SOURCE_FRAMEWORK,
    SOURCE_LABELS,
    SOURCE_PLUGIN,
    STATE_ALL,
    STATE_OFF,
    STATE_ON,
    HotkeyEntry,
    collect,
    display_combo,
    reset_combo,
    write_combo,
)
from barkit.shell.hotkey_editor import HotkeyEditor
from barkit.shell.ui_kit import (
    ICON_TONE_DEFAULT,
    ComboField,
    EditStack,
    FeedbackBar,
    ModeBadge,
    RecordRow,
    Switch,
    apply_text_role,
    note,
    search_field,
    settings_page_shell,
    settings_section,
    tool_button,
)

#: 搜索框与两个筛选下拉的定宽（px）。**都不吃 stretch** —— 一让搜索框拉伸，
#: 富余宽度全归它，窄窗里会拉成一条与旁边下拉完全不成比例的长条（2026-09-22
#: 用户在 hosts 管理窗上判过同一件事）。值取「放得下占位文案」再富余一点。
SEARCH_W = 150
FILTER_W = 96

#: 行的内边距：**左右是 0**，只留上下 —— 行整条铺在分区卡里，左右那一段由卡的
#: 内容区统一给。左右若自带一份，行里的图标盒就比卡标题多缩进一截（同一条竖向基准线
#: 上对不齐），hover 底还会在卡内缩出一圈「框中框」；行与行之间靠间距分，不靠内边距撑。
#: 上下 8 = 骨架图标盒（36）与两侧控件（22 / 28）之间的呼吸位，与主面板的行同档。
PAD_ROW = (0, 8, 0, 8)

#: 来源三档 → **行首图标盒的色档**（形状见 :func:`_icon_of`）。三档 = 中性 / 品牌浅底 /
#: 品牌实底，理由（为什么不用状态色）见 ``ui_kit.ICON_TONES``。
#:
#: 「框架」给最弱的中性：它是系统自带的，不该跟用户自己建的那几条抢眼；「我的」给实心
#: 品牌底：整张列表里它是唯一「这条是你自己加的」，值得一眼跳出来；「组件」居中。
SOURCE_TONES = {
    SOURCE_FRAMEWORK: "neutral",
    SOURCE_PLUGIN: "accent",
    SOURCE_CUSTOM: "solid",
}


def _icon_of(entry: HotkeyEntry) -> IconName:
    """这一行图标盒里画什么 —— **图标表达「是谁」，颜色表达「哪一类」**。

    - 组件声明的键位画**该组件自己的主图标**（识别画扫描、蓝牙画蓝牙）：一眼看出这条
      是哪个组件给的，不必去读副标题；
    - 框架内置画齿轮（框架兜底出页用的也是这枚），用户自建画键盘（他录的就是一组键）。

    组件那条**不留空**：``ComponentSpec.icon`` 自带默认值，取不到才回落键盘。
    """
    if entry.source == SOURCE_PLUGIN and entry.icon:
        try:
            return IconName(entry.icon)
        except ValueError:
            # 组件声明了一枚本仓不认识的图标名（插件与框架版本对不上）：宁可画通用键盘，
            # 也不要让整页起不来 —— 这里抛出去就是整个设置窗打不开。
            pass
    if entry.source == SOURCE_FRAMEWORK:
        return IconName.GEAR
    return IconName.KEYBOARD


class HotkeysPage(QWidget):
    """「快捷键」页。构造参数全是**注入**：页面自己不去翻全局。"""

    def __init__(
        self,
        *,
        config,
        contributions=None,
        actions: "tuple" = FRAMEWORK_ACTIONS,
        on_rebind=None,
        conflict_of=None,
        hotkeys_available: bool = True,
        run_command=None,
        feedback=None,
        failure_of=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._contributions = contributions
        self._actions = actions
        self._on_rebind = on_rebind
        self._conflict_of = conflict_of
        self._available = hotkeys_available
        self._feedback_host = feedback
        #: 名字 → 失败原因（挂上了给 None）。**每次画行都重新问**：启动那批热键
        #: 注册失败时没有界面在场，用户是事后才翻到这一页的 —— 那一刻原因要还查得到。
        self._failure_of = failure_of
        self._run = run_command or self._run_threaded
        self._entries: list[HotkeyEntry] = []
        self._source = SOURCE_ALL
        self._state = STATE_ALL
        self._query = ""
        self._editor: HotkeyEditor | None = None
        #: 在跑的那条命令线程（**只为摘槽用**；生命周期由 ``thread_reap`` 代持，
        #: 见 ``_run_threaded``）——页面销毁时先摘掉指向本页的槽，再要求它停。
        self._runner = None
        self._on_result = None
        self.destroyed.connect(lambda *_a: self._stop_runner())

        page, lay = settings_page_shell("快捷键", self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(page)

        self._list_page, list_lay = self._build_list_page()
        # 换屏交给框架的 `EditStack`（列表屏 ⇄ 编辑屏）：切页不再有「0 / 1 是哪一个」
        # 的魔数下标 —— 写错一位的症状是「点了返回、屏没变」，而代码里看不出来。
        self._stack = EditStack(self._list_page, page)
        lay.addWidget(self._stack, stretch=1)

        if not self._available:
            list_lay.addWidget(_warn("本机热键不可用（缺辅助功能权限或非 macOS），"
                                     "下面的键位都按不响 —— 去「维护 → 权限」授权后重启。",
                                     self._list_page))
        list_lay.addWidget(note(
            "框架与组件给的只能改键 / 停用 / 初始化，不能删；改动立即生效，不用重启。",
            self._list_page))

        # 工具条 / 行 / 反馈槽是**一组**，整组装进分区卡 —— 与设置窗其余各页同款：
        # 卡外的裸行没有归属，用户看不出哪几行是一件事（2026-09-24 用户报「这一页
        # 没按其他页面的排版来」，改前这三者都裸在页面背景上）。
        # 卡 ``stretch=1`` 铺满这一屏：行多时要高度，且这样列表屏与详情屏（那一屏
        # 自己就是一块面）是同构的 —— 换屏时底不跳层。
        card, card_lay = settings_section("键位", self._list_page)
        list_lay.addWidget(card, stretch=1)
        card_lay.addLayout(self._build_bar(card))
        self._list = QWidget(card)
        self._list_lay = QVBoxLayout(self._list)
        self._list_lay.setContentsMargins(0, 0, 0, 0)
        self._list_lay.setSpacing(metrics.GAP_SM)
        card_lay.addWidget(self._list, stretch=1)
        # 反馈槽在**卡内**、紧跟列表（「一区一条」）：挂在页底就成了「整页的消息」，
        # 与它要说的那一行隔着一屏。
        self._fb = FeedbackBar(card)
        card_lay.addWidget(self._fb)
        self.refresh()

    # ---------- 界面 ----------

    def _build_list_page(self) -> tuple[QWidget, QVBoxLayout]:
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        # 与外壳同档（``settings_page_shell`` 的 12）：页首说明到卡的距离，
        # 在本页与其它页是同一个数。
        lay.setSpacing(metrics.GAP_LG)
        return host, lay

    def _failure(self, entry: "HotkeyEntry") -> "str | None":
        """这条此刻是不是没挂上（挂上了 / 查不到都给 None）。

        **只在「配置要它占键」时才问**：键位为空是用户主动停用，那不是失败，
        拿去问会满屏「没挂上」。查不到（没注入 / 还没装配）也算没有 —— 那是判据不适用，
        不是「确认挂上了」。
        """
        if not entry.enabled or self._failure_of is None:
            return None
        try:
            return self._failure_of(entry.name)
        except Exception:  # noqa: BLE001 —— 问原因问崩了不该让整页起不来
            return None

    def _build_bar(self, parent: QWidget) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(metrics.GAP_MD)

        self._search = search_field(
            "搜索名称或键位…", on_changed=self._on_query, parent=parent,
        )
        # 定宽归本页：工具条里它**不许吃 stretch**，组件盒不管尺寸（见下面那段注释）。
        self._search.setFixedWidth(SEARCH_W)
        bar.addWidget(self._search)

        self._source_box = ComboField(parent)
        self._source_box.setFixedWidth(FILTER_W)
        for code in (SOURCE_ALL, SOURCE_FRAMEWORK, SOURCE_PLUGIN, SOURCE_CUSTOM):
            self._source_box.addItem(SOURCE_LABELS.get(code, "全部"), code)
        self._source_box.currentIndexChanged.connect(self._on_source)
        bar.addWidget(self._source_box)

        self._state_box = ComboField(parent)
        self._state_box.setFixedWidth(FILTER_W)
        for label, code in (("全部", STATE_ALL), ("启用", STATE_ON), ("禁用", STATE_OFF)):
            self._state_box.addItem(label, code)
        self._state_box.currentIndexChanged.connect(self._on_state)
        bar.addWidget(self._state_box)

        bar.addStretch(1)
        add = tool_button("+ 新增", kind="primary", size="inline", parent=parent)
        add.clicked.connect(lambda: self._open_editor(None))
        bar.addWidget(add)
        return bar

    def _build_row(self, entry: HotkeyEntry) -> QWidget:
        # 一行 = **主面板组件行那份骨架**（图标盒 ＋ 主标题行 ＋ 副标题 ＋ 尾部控件），
        # 与面板同源（``RecordRow``）—— 于是「一条记录长什么样」全仓只有一处实现。
        #
        # 来源（框架 / 组件名 / 我的）在这一行上出现**两次**，各管一件事：
        # ① 标题后那枚徽标说「打哪儿来」（文字，含具体组件名）；
        # ② 行首图标盒的形状 + 颜色说同一件事的另一半 —— 形状是「谁给的」，
        #    颜色是「哪一类」（三档，见 ``SOURCE_TONES``）。图标只分得出类别，
        #    名字补在它的悬停提示里（``set_icon_tooltip``）。
        #
        # 整行点击 = 进详情屏（``on_click`` 是构造参数，不是信号 —— 行内那几颗
        # 按钮自己吃掉按下，不会误触发）
        row = RecordRow(
            title=entry.title,
            subtitle=entry.description,
            icon=_icon_of(entry),
            icon_tone=SOURCE_TONES.get(entry.source, ICON_TONE_DEFAULT),
            on_click=lambda e=entry: self._open_editor(e),
            clickable=True,
            margins=PAD_ROW,
            parent=self._list,
        )
        row.set_icon_tooltip(f"来源：{entry.owner}")
        # 徽标一律中性色（surface2 底 + text2 字）：它是**出处**不是状态，
        # 用强调色会读成「这一条有什么问题」。
        row.set_badge(ModeBadge(entry.owner, accent=False, clickable=False, parent=row))

        # **没挂上要写在行上**：启动那批热键是在没有界面的时刻注册的，失败时只有一行
        # 日志 —— 用户那天看到的就是「按了没反应」，而翻到这一页时原因必须还在。
        # 写在副标题而不是另起一枚徽标：出处那枚徽标刻意走中性色（它是「打哪儿来」
        # 不是状态），再挂一枚强调色的会让同一行出现两种颜色的徽标、读不出哪个是问题。
        reason = self._failure(entry)
        if reason is not None:
            row.set_subtitle(f"没挂上：{reason}", tooltip=reason)

        pill = ComboButton(entry.combo, row)
        pill.captured.connect(lambda combo, e=entry: self._on_captured(e, combo))
        row.add_trailing(pill)

        sw = Switch(row)
        sw.setChecked(entry.enabled)
        sw.clicked.connect(lambda e=entry: self._on_toggle(e))
        row.add_trailing(sw)

        if entry.source != SOURCE_CUSTOM:
            reset = tool_button("初始化", size="inline", parent=row)
            reset.setEnabled(entry.resettable)
            reset.setToolTip(
                "键位改回出厂值 " + display_combo(entry.default_combo)
                if entry.resettable else "当前就是出厂键位"
            )
            # ``clicked`` 会带一个 checked 参数进来，默认参数要给在它**后面**
            reset.clicked.connect(lambda _checked=False, e=entry: self._on_reset(e))
            row.add_trailing(reset)
        return row

    # ---------- 数据 ----------

    def refresh(self) -> None:
        """重新合成列表并重画（改键 / 启停后走这里，不必重开设置窗）。"""
        names: dict[str, str] = {}
        icons: dict[str, str] = {}
        hotkeys: tuple = ()
        if self._contributions is not None:
            components = tuple(self._contributions.components())
            names = {c.plugin_id: c.name for c in components}
            icons = {c.plugin_id: c.icon.value for c in components}
            hotkeys = tuple(self._contributions.hotkeys())
        self._entries = collect(
            config=self._config, hotkeys=hotkeys, component_names=names,
            component_icons=icons, actions=self._actions,
        )
        self._render()

    def _render(self) -> None:
        _clear(self._list_lay)
        shown = [e for e in self._entries if self._matches(e)]
        for entry in shown:
            self._list_lay.addWidget(self._build_row(entry))
        if not shown:
            self._list_lay.addWidget(note("没有符合条件的快捷键。", self._list))
        self._list_lay.addStretch(1)

    def _matches(self, entry: HotkeyEntry) -> bool:
        if self._source != SOURCE_ALL and entry.source != self._source:
            return False
        if self._state == STATE_ON and not entry.enabled:
            return False
        if self._state == STATE_OFF and entry.enabled:
            return False
        if not self._query:
            return True
        # 键位两种写法都收：用户可能照着界面打「⌃⌥O」，也可能照着配置打「ctrl+alt+o」
        haystack = " ".join((entry.title, entry.description, entry.combo,
                             display_combo(entry.combo))).lower()
        return self._query in haystack

    # ---------- 交互 ----------

    def _on_query(self, text: str) -> None:
        self._query = text.strip().lower()
        self._render()

    def _on_source(self, _index: int) -> None:
        self._source = self._source_box.currentData() or SOURCE_ALL
        self._render()

    def _on_state(self, _index: int) -> None:
        self._state = self._state_box.currentData() or STATE_ALL
        self._render()

    def _on_toggle(self, entry: HotkeyEntry) -> None:
        """开关 = 停用 / 恢复。**停用是空串，恢复是删键**（见 catalog.write_combo）。"""
        if entry.enabled:
            write_combo(self._config, entry.name, "", entry.default_combo)
            self._after_change(entry.name, f"「{entry.title}」已停用")
            return
        if not entry.default_combo:
            self._fb.warn(f"「{entry.title}」出厂没给键位，先点右边那枚键位药丸录一个")
            self._render()
            return
        write_combo(self._config, entry.name, entry.default_combo, entry.default_combo)
        self._after_change(entry.name, f"「{entry.title}」已启用")

    def _on_reset(self, entry: HotkeyEntry) -> None:
        """初始化：删掉差异项，**键位回到出厂值**（不是写一遍默认值）。"""
        if not entry.resettable:
            return
        reset_combo(self._config, entry.name)
        self._after_change(entry.name,
                           f"「{entry.title}」已恢复出厂键位 {display_combo(entry.default_combo)}")

    def _on_captured(self, entry: HotkeyEntry, combo: str) -> None:
        """录到一组键：**先问冲突**（撞了就一个字都不写），再落盘，再重挂。"""
        owner = self._conflict_of(entry.name, combo) if self._conflict_of else None
        if owner:
            self._fb.error(f"{display_combo(combo)} 已被「{owner}」占用，没有改动")
            self._render()
            return
        write_combo(self._config, entry.name, combo, entry.default_combo)
        self._after_change(entry.name, f"「{entry.title}」改为 {display_combo(combo)}")

    def _after_change(self, name: str, ok_text: str) -> None:
        """写盘之后**必须问一句重挂成没成**：写进配置了却挂不上（没权限 / 键位非法），
        用户看到的是「按下去没反应」，而配置里明明有值 —— 是最难自查的一类。

        重挂答的是 ``REBIND_*`` 三态而不是布尔：``REBIND_UNBOUND`` 表示「按你说的
        撤掉了」，那是成功，不能跟「没挂上」混为一谈（混了就是「界面说已启用、
        实际按不响」）。
        """
        if self._on_rebind is None:
            self.refresh()
            return
        result = self._on_rebind(name)
        self.refresh()
        if result == REBIND_FAILED:
            self._fb.error(f"{ok_text}，但没能挂上（检查辅助功能权限）")
        else:
            self._fb.info(ok_text)

    # ---------- 详情屏 ----------

    def _open_editor(self, entry: "HotkeyEntry | None") -> None:
        editor = HotkeyEditor(
            config=self._config,
            entry=entry,
            on_rebind=self._on_rebind,
            conflict_of=self._conflict_of,
            run_command=self._run,
            feedback=self._feedback_host,
        )
        editor.back.connect(self._close_editor)
        editor.saved.connect(lambda _name: self._close_editor())
        self._editor = editor
        self._stack.push(editor)

    def _close_editor(self) -> None:
        editor = self._editor
        self._editor = None
        if editor is None:
            return
        self._stack.drop(editor)
        self.refresh()

    def _run_threaded(self, command: str, on_result) -> None:
        """默认的执行口：挪到后台线程。

        同步跑一条命令最坏要 10 秒（见 ``core.hotkey_command.TIMEOUT_SEC``），
        而这里是主线程 —— 窗子会整个僵住，看着就像崩了。

        **不给 ``parent``**：原先写的是 ``CommandRunner(command, parent=self)``，而本页
        在设置窗关闭时会被销毁 ⇒ Qt 连带析构子对象，若那条命令还没跑完就是
        「线程仍在运行时析构 ``QThread``」⇒ Qt ``qFatal`` ⇒ ``abort()``，**整个 app 死**
        （2026-09-29 崩溃报告；同一形态的诊断线程那次真的崩了）。生命周期改由
        ``thread_reap`` 代持：页面先没了也只不过让线程自己跑完再回收。
        """
        runner = CommandRunner(command)
        runner.result.connect(self._deliver_result)
        self._on_result = on_result
        self._runner = thread_reap.own(runner)
        runner.start()

    def _deliver_result(self, result) -> None:
        """把结果转给调用方给的回调；经本方法是为了销毁时能精确摘掉这一条连接。"""
        if callable(self._on_result):
            self._on_result(result)

    def _stop_runner(self) -> None:
        """页面销毁：先摘槽（回调不许再进已死的页），再要求线程停（不许提前析构它）。"""
        runner = self._runner
        if runner is None:
            return
        self._runner = None
        self._on_result = None
        try:
            runner.result.disconnect(self._deliver_result)
        except (RuntimeError, TypeError):   # 已经断过 / 对象在销毁途中
            pass
        if thread_reap.stop(runner):
            runner.deleteLater()


def _clear(layout: QVBoxLayout) -> None:
    """清空一个纵向布局（连 stretch 一起）。

    先 ``setParent(None)`` 再 ``deleteLater()``：**从布局里 takeAt 不会摘掉父子关系**
    —— 只 deleteLater 的话，旧行在事件循环跑之前仍是列表的子对象（``findChildren``
    能把已经拆掉的行数回来），筛选后「行数没变」就是这样读出来的。
    """
    while layout.count():
        item = layout.takeAt(0)
        if item is None:
            continue
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()


def _warn(text: str, parent: QWidget) -> QLabel:
    """页首那条警示（热键不可用时）。走 ``caption_warn`` 角色，不自己拼颜色。"""
    lbl = note(text, parent)
    apply_text_role(lbl, "caption_warn")
    return lbl
