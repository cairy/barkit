"""shell/hotkey_editor：一条快捷键的**详情 / 编辑屏**（页内换屏，不新开窗口）。

**为什么不弹窗**：置顶窗里开嵌套事件循环在本仓有崩溃前科，且零模态是定式 ——
列表行里塞不下命令输入框与测试结果，就**换一屏**而不是叠一层（与 hosts 管理窗
同款：列表屏 → 编辑屏，返回即回列表）。

**为什么这一屏是「草稿 + 保存」而不是改一次落一次盘**：它是**表单**（名称 + 命令 +
键位 + 开关 + 通知），而列表行那颗药丸是**单值微调**（点一下录完就落盘）。表单的
中途态是不该落盘的 —— 新建那条尤其明显：它还没有名字，落什么？故这一屏统一
「改动先记在草稿里，点保存才写」。代价写在界面上（底部那句小字），不靠用户猜。

**内置条目也进这一屏**（不是只有自建能进）：「触发成功要不要通知」是**逐条**的，
列表行塞不下第二颗开关；把这一屏做成所有条目的详情，比在行里再挤一个控件干净。
内置条目在这里只少两样：不能改名称、没有命令与测试。

**版面归框架**（2026-09-23）：这一屏是框架「编辑弹窗」那门语言的第一个消费方 ——
导航带 / 内容区 / 反馈槽 / 动作行四区由框架定死，本文件只往区里填东西（内容区里只有
一组：这一条本身 —— 「测试」内联进命令格，「上次触发」沉到卡底一行小字，不再另起卡）。

◷ 2026-09-27：这一屏改走 `ui_kit.EditScreen`（框架把「编辑弹窗」正式收成的通用口 ——
四区 ＋ 返回/删除/保存三条动作线 ＋ 保存前那道门）。**观感零变更**：同一个
`EditorShell`、同两根按钮、同一个位置。区别只在「接线归谁」：以前是本文件自己
`add_action` 把两根按钮摆上去，现在是 `EditScreen` 给的 `save_button` 与
`add_destructive()`；本文件自己的 `_on_save` 一分为二（`validate` ＋ `commit`），
两端**注入**给 `EditScreen`（它是组合件，在本文件这边覆写是调不到的 —— 见那条类
docstring 的 ⚠️）。

**那一块「面」也是壳给的**（2026-09-24）：整屏是一张 `Card`（比设置页深一档的底 ＋
圆角 ＋ 描边），分组与底栏**都不许自带面** —— 面里有面就又是两层，本屏那句「按钮区域
和其它区域不在一个层」就是这么来的。故本文件用 `ui_kit.form_group`（只有标题），
不用 `settings_section`（那是一张卡）。

**观感返工（同一天下午）**：第一版四区壳只解决了「页脚各排各的」，这一屏**读起来仍
是字的屏而不是控件的屏** —— 卡内常驻两段长释义（命令 60 字 ＋ 反馈 84 字），控件被
灰字淹掉。这一版的口径是**说明按需出现**：

- 命令那段压成一行限制提示，完整版进输入框的 tooltip；
- 反馈那三档释义改成**跟着选中项变的一句话**（`ui_kit.choice_hint`），没选的那两档
  此刻不需要知道，它们进下拉每项的 tooltip；
- 「键位」那一行原本装着**三件事**（药丸 ＋ 开关 ＋ 初始化），标签只能盖住第一个，
  且 stretch 把「初始化」推得离它要改的那枚药丸最远 ⇒ 拆成「键位」与「启用」两行；
- 卡标题从「这条快捷键」（整屏就是它，等于没说）改成**来源身份**（「我的 · 可删」/
  「框架 · 不可删」）—— 删不删得掉看的正是这个；
- 页脚那句提示从常驻文案改成**状态位**：保存按钮由「有没有未保存改动」驱动，提示
  跟着说「有改动没保存」/「名称与命令都得填」/ 干净时一句都不说。
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from barkit.shell.design import metrics
from barkit.shell.hotkey_capture import ComboButton
from barkit.shell.hotkey_catalog import (
    CustomSpec,
    FEEDBACK_HINTS,
    FEEDBACK_LABELS,
    FEEDBACK_MODES,
    FEEDBACK_NONE,
    FEEDBACK_NOTIFY,
    FEEDBACK_OVERLAY,
    HotkeyEntry,
    REBIND_FAILED,
    SOURCE_CUSTOM,
    display_combo,
    remove_custom,
    reset_combo,
    write_combo,
    write_custom,
    write_feedback,
)
from barkit.shell.ui_kit import (
    PLAIN_ZONE,
    ComboField,
    EditScreen,
    Switch,
    apply_text_role,
    choice_hint,
    form_row,
    form_row_stacked,
    note,
    tool_button,
)

#: 测试结果最多显示几行：这一屏的高度有限，而一条命令的输出可能有几百行。
RESULT_LINES = 6
RESULT_CHARS = 300

#: 命令那格的**完整**限制说明（界面上只留一句，剩下的挂在 tooltip 里）。
#: 判据：这些限制是**填完之后**才需要知道的（写的时候没人会先看说明书），常驻在
#: 控件下面就是把 4 个控件埋进 60 个字里 —— 需要时自己指上去看，比摊开便宜。
_COMMAND_TIP = (
    "按空格切分成程序与参数，不走 shell —— 管道、重定向、变量都不认。\n"
    "以当前用户权限跑（root 跑不了：守护不执行从配置读出来的命令），10 秒没跑完就中止。"
)

#: 三档反馈各一句**完整**释义（进下拉每项的 tooltip；界面上只显示当前档那句短的）。
_FEEDBACK_TIPS = {
    FEEDBACK_NONE: "成功时什么都不弹 —— 这条本来就有可见结果时用这一档",
    FEEDBACK_OVERLAY: "成功时弹一个 2 秒自消的轻浮层，不激活 app（不打断手上在做的事）",
    FEEDBACK_NOTIFY: "成功时发一条系统通知（收进通知中心，事后能翻）",
}

#: 开关那一行的状态补充（开关自己只答「开 / 关」，答不了「关了会怎样」）。
_ENABLE_HINTS = {True: "按下去会执行", False: "按下去没反应"}

#: 页脚那句**状态位**的两句文案（见 `EditorShell.set_hint`）。都不是开场说明：
#: 一句答「为什么点不了」，一句答「还有东西没落盘」；干净时一句都不说。
_HINT_INCOMPLETE = "名称与命令都得填，填了才能保存"
_HINT_DIRTY = "有改动没保存"


class HotkeyEditor(QWidget):
    """一条快捷键的编辑屏。``entry`` 为 None = **新增一条自建**。"""

    saved = Signal(str)      # 携带落定的名字
    back = Signal()

    def __init__(
        self,
        *,
        config,
        entry: "HotkeyEntry | None" = None,
        on_rebind=None,
        conflict_of=None,
        run_command=None,
        feedback=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._entry = entry
        self._on_rebind = on_rebind
        self._conflict_of = conflict_of
        self._run_command = run_command
        self._feedback_host = feedback
        self._confirm_delete = False
        self._runner = None
        # 「初始化」只在这一条**改过键位**时才建（与列表行同一条判据）
        self._reset_btn = None

        self._draft = {
            "title": entry.title if entry else "",
            "command": entry.command if entry else "",
            "combo": entry.combo if entry else "",
            "feedback": entry.feedback if entry else FEEDBACK_NOTIFY,
        }
        # 「进屏时的值」：脏标记拿它比，来回改回原样会自动变干净（见 :attr:`_dirty`）
        self._initial = dict(self._draft)
        self._build()

    # ---------- 界面 ----------

    def _build(self) -> None:
        # **不传 hint**：页脚那句是状态位，由 `_sync_actions` 按当前状态改写
        # （一进来就同步一次），不是一句钉在屏幕角上的开场说明。
        self._screen = EditScreen(
            title="新增快捷键" if self._entry is None else self._entry.title,
            on_validate=self.validate,
            on_commit=self.commit,
            parent=self,
        )
        self._screen.back.connect(self.back.emit)
        self._feedback = self._screen.feedback

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._screen)

        self._build_entry_group()

        # 破坏性动作在左、主动作在右（位置是 `EditorShell` 定的，调用方只报档）
        if self._entry is not None and self._entry.deletable:
            self._del_btn = self._screen.add_destructive("删除", self._on_delete)
        self._save_btn = self._screen.save_button
        # 建完就同步一次按钮可用态与提示：新建那条**一进来就是缺字段的**，
        # 「保存」不能等用户动了什么才想起自己该是灰的。
        self._sync_actions()

    def _build_entry_group(self) -> None:
        """卡一：这一条本身（打哪儿来 ＋ 叫什么 ＋ 按什么键 ＋ 响了怎么告诉你）。

        标题是**来源身份**而不是「这条快捷键」：整屏就是这一条，那个标题一个字的
        信息都没给，而「删不删得掉」看的就是来源 —— 正好顺手答了「为什么有的条目
        没有删除键」。
        """
        group, body = self._screen.add_group(self._identity())

        if self._entry is not None and not self._is_custom:
            # 内置条目：名称与说明都是**声明出来的**，这一屏不提供改它们的入口。
            # 只说它是干什么的 —— 来源已经在卡标题上，不必再说一遍。
            if self._entry.description:
                body.addWidget(note(self._entry.description, group))

        if self._is_custom:
            self._title_edit = QLineEdit(self._draft["title"], group)
            self._title_edit.setPlaceholderText("干什么用的")
            self._title_edit.textChanged.connect(self._on_title)
            body.addLayout(form_row("名称", self._title_edit, group))

            self._cmd_edit = QLineEdit(self._draft["command"], group)
            self._cmd_edit.setPlaceholderText("/usr/bin/open -a 日历")
            self._cmd_edit.setToolTip(_COMMAND_TIP)
            self._cmd_edit.textChanged.connect(self._on_command)
            # 命令这一格用堆叠排法：命令普遍比「名称」长，78 的定宽标签放不下。
            # 「测试」是命令格的**行内操作按钮**（与「初始化」之于「键位」同款）：
            # 它试跑的就是这一格里那行命令，故贴在这格右边、不另起一张「执行结果」卡。
            cmd_row = QHBoxLayout()
            cmd_row.setSpacing(metrics.GAP_MD)
            cmd_row.addWidget(self._cmd_edit, stretch=1)
            self._test_btn = tool_button("测试", size="inline", parent=group)
            self._test_btn.setToolTip("用上面的命令原样跑一次，不保存也会跑")
            self._test_btn.clicked.connect(self._on_test)
            cmd_row.addWidget(self._test_btn)
            body.addWidget(form_row_stacked("命令", self._wrap(cmd_row), group))
            # 测试结果落到命令行正下方：它答「刚才那次试跑」，与命令同属一个表单项。
            self._result = note("用上面的命令原样跑一次，不保存也会跑。", group)
            self._result.setWordWrap(True)
            body.addWidget(self._result)

        # 键位这一行**只装键位**（＋ 改它的那枚「初始化」）—— 开关是另一件事，看下一行
        key_row = QHBoxLayout()
        key_row.setSpacing(metrics.GAP_MD)
        self._pill = ComboButton(self._draft["combo"], group)
        self._pill.captured.connect(self._on_combo)
        key_row.addWidget(self._pill)
        if self._entry is not None and self._entry.resettable:
            # 「初始化」紧跟药丸：它动的就是左边那枚药丸里的值，**不放 stretch 之后**
            # —— 上一版把它推到行尾，改的东西与被改的东西隔着一整行空白。它也是这一屏
            # 唯一**立刻落盘**的动作（不是草稿），故把这件事写进 tooltip。
            self._reset_btn = tool_button("初始化", size="inline", parent=group)
            self._reset_btn.setToolTip("键位改回出厂值 "
                                       + display_combo(self._entry.default_combo)
                                       + "（立即生效，不用点保存）")
            self._reset_btn.clicked.connect(self._on_reset)
            key_row.addWidget(self._reset_btn)
        key_row.addStretch(1)
        body.addLayout(form_row("键位", self._wrap(key_row), group))

        # 启用单独一行：它是比键位**高一层的属性**（键位决定按什么、开关决定按了算不算），
        # 挂在同一行里时标签只能盖住药丸，开关就成了没有归属的第二颗控件。
        enable_row = QHBoxLayout()
        enable_row.setSpacing(metrics.GAP_MD)
        self._switch = Switch(group)
        self._switch.setChecked(bool(self._draft["combo"]))
        self._switch.clicked.connect(self._on_toggle)
        enable_row.addWidget(self._switch)
        self._enable_note = note("", group)
        enable_row.addWidget(self._enable_note, stretch=1)
        body.addLayout(form_row("启用", self._wrap(enable_row), group))
        self._sync_enable_note()

        feedback_row = QHBoxLayout()
        feedback_row.setSpacing(metrics.GAP_MD)
        # 走 ``ui_kit.ComboField`` 而不是裸 ``QComboBox``：同一门语言的两件事 —— 滚轮
        # 不许误改这一档（裸 combo 不聚焦时滑过也会改掉选中项），且弹层要钉在**自己
        # 那块屏**上（macOS 会把新顶层窗按主屏重新归位，见 ``placement.place_popup_below``）。
        self._feedback_combo = ComboField(group)
        for mode in FEEDBACK_MODES:
            self._feedback_combo.addItem(FEEDBACK_LABELS[mode], mode)
            self._feedback_combo.setItemData(
                self._feedback_combo.count() - 1, _FEEDBACK_TIPS[mode],
                Qt.ItemDataRole.ToolTipRole,
            )
        idx = self._feedback_combo.findData(self._draft["feedback"])
        if idx >= 0:
            self._feedback_combo.setCurrentIndex(idx)
        self._feedback_combo.currentIndexChanged.connect(self._on_feedback)
        feedback_row.addWidget(self._feedback_combo)
        feedback_row.addStretch(1)
        body.addLayout(form_row("成功后反馈", self._wrap(feedback_row), group))
        # **只说当前选中那一档**（`choice_hint`），三档共有的部分补一句：
        # 摊开三档释义是 84 个字，会把上面四颗控件整个埋掉。
        body.addWidget(choice_hint(self._feedback_combo, FEEDBACK_HINTS, group))
        body.addWidget(note("失败一律发系统通知，不受这里影响。", group))
        # 「上次触发」是这一条**事后**唯一的运行证据（热键触发时设置窗多半关着），
        # 与手动「测试」是两回事，归到卡一底部一行小字，不再单独成卡。
        last = self._last_result()
        if last is not None:
            ok, detail = last
            label = note(f"上次触发：{detail or ('成功' if ok else '失败')}", group)
            apply_text_role(label, "caption" if ok else "caption_warn")
            body.addWidget(label)

    def _last_result(self) -> "tuple[bool, str] | None":
        """这一条**上次触发**的结局（``None`` = 没触发过 / 这一屏不认它）。

        热键触发时设置窗多半是关着的，这条记录是事后唯一能看到的证据 ——
        排查「到底按下去跑没跑」就靠它。
        """
        if self._feedback_host is None or self._entry is None:
            return None
        return self._feedback_host.last_of(self._entry.name)

    def _wrap(self, layout: QHBoxLayout) -> QWidget:
        """把一行控件包成 widget（``form_row`` 要的是控件，不是 layout）。

        这个盒子**必须显式透明**（``ui_kit.PLAIN_ZONE``）：全局那条
        ``QWidget { background: surface }`` 连**裸 QWidget 实例**也管，不声明的话
        「键位」「启用」这些行右侧那一格就自带一块比面更亮的方块
        （2026-09-27 用户上报「『键位』、『启动』等的右侧『控件位』背景是白色的，
        和其他的颜色违和」—— 编辑屏的面是 ``surface2``，比页面底色深一档，
        同样一块 ``surface`` 在设置页看不出来、在这里一眼可见）。
        """
        box = QWidget(self)
        box.setObjectName(PLAIN_ZONE)
        box.setLayout(layout)
        return box

    # ---------- 草稿 ----------

    @property
    def _is_custom(self) -> bool:
        return self._entry is None or self._entry.source == SOURCE_CUSTOM

    @property
    def _dirty(self) -> bool:
        """草稿与**进屏时的值**有没有差别。

        比「动过就算脏」的布尔标记准：来回改回原样之后不该还挂着「有改动没保存」，
        那句话是假的。
        """
        return self._draft != self._initial

    def _on_title(self, text: str) -> None:
        self._draft["title"] = text.strip()
        self._sync_actions()

    def _on_command(self, text: str) -> None:
        self._draft["command"] = text.strip()
        self._sync_actions()

    def _on_combo(self, combo: str) -> None:
        owner = self._conflict_of(self._name(), combo) if self._conflict_of else None
        if owner:
            self._feedback.error(f"{display_combo(combo)} 已被「{owner}」占用，没有改动")
            self._pill.set_combo(self._draft["combo"])
            return
        self._draft["combo"] = combo
        self._switch.setChecked(True)
        self._sync_enable_note()
        self._sync_actions()

    def _on_toggle(self) -> None:
        """关 = 草稿里清掉键位；开 = 恢复出厂键位（自建没有出厂值 ⇒ 让你先录）。"""
        if not self._switch.isChecked():
            self._draft["combo"] = ""
            self._pill.set_combo("")
            self._sync_enable_note()
            self._sync_actions()
            return
        default = self._entry.default_combo if self._entry else ""
        if default:
            self._draft["combo"] = default
            self._pill.set_combo(default)
        else:
            self._switch.setChecked(False)
            self._feedback.warn("这一条没有出厂键位，先点键位那颗药丸录一个")
        self._sync_enable_note()
        self._sync_actions()

    def _on_feedback(self) -> None:
        self._draft["feedback"] = self._feedback_combo.currentData()
        self._sync_actions()

    # ---------- 动作 ----------

    def _on_reset(self) -> None:
        """初始化：**立刻落盘**（它只有一件事要做，不存在「改了一半」）。"""
        if self._entry is None:
            return
        reset_combo(self._config, self._entry.name)
        self._draft["combo"] = self._entry.default_combo
        self._pill.set_combo(self._entry.default_combo)
        self._switch.setChecked(bool(self._entry.default_combo))
        self._entry = _replace(self._entry, combo=self._entry.default_combo)
        # 只有键位落了盘，故只把键位那一项记为「已落」—— 其余草稿还脏着就得还脏着
        self._initial["combo"] = self._entry.default_combo
        self._sync_enable_note()
        self._sync_actions()
        if self._reset_btn is not None:
            # 已经等于出厂值，再点是空的：按钮此时该**禁用**，不是点了回一句「你没改过」
            self._reset_btn.setEnabled(False)
            self._reset_btn.setToolTip("当前就是出厂键位")
        self._after_write(self._entry.name, "已恢复出厂键位 "
                          + display_combo(self._entry.default_combo))

    def _on_test(self) -> None:
        if self._run_command is None:
            self._feedback.warn("当前环境跑不了命令")
            return
        self._test_btn.setEnabled(False)
        self._result.setText("跑着…")
        self._run_command(self._draft["command"], self._on_tested)

    def _on_tested(self, result) -> None:
        self._test_btn.setEnabled(True)
        lines = [result.headline]
        if result.output:
            body = result.output[:RESULT_CHARS]
            lines.append("\n".join(body.splitlines()[:RESULT_LINES]))
        self._result.setText("\n".join(lines))
        apply_text_role(self._result, "caption" if result.ok else "caption_warn")

    def validate(self) -> "str | None":
        """保存的前置门（注入给 `EditScreen` 的 `on_validate`）：缺字段时**一个字都不落盘**。

        「保存」键本来就在缺字段时是灰的（见 :meth:`_sync_actions`），故这道门平时走不到
        —— 留着是因为它才是**真的那道**：按钮的灰亮是**提示**，这里才是**拦截**。
        """
        return None if self._valid() else _HINT_INCOMPLETE

    def commit(self) -> None:
        """保存（`EditScreen` 的落盘口）：自建走「新建 / 覆盖」，内置只写键位与反馈。

        **留在这一屏**（不回列表）：它的时间线是「改 → 存 → 接着看这条」，回列表反而多
        一次点击；另三处编辑屏是「改 → 存 → 回列表」—— 这处差异归消费方，见 `EditScreen`
        的类 docstring。
        """
        name = self._name()
        if self._is_custom:
            name = write_custom(self._config, CustomSpec(
                name="" if self._entry is None else self._entry.name,
                title=self._draft["title"],
                command=self._draft["command"],
                combo=self._draft["combo"],
                feedback=self._draft["feedback"],
            ))
            self._mark_saved()
            self._after_write(name, "已保存")
            self.saved.emit(name)
            return
        write_combo(self._config, name, self._draft["combo"], self._entry.default_combo)
        write_feedback(self._config, name, self._draft["feedback"], self._entry.feedback_default)
        self._mark_saved()
        self._after_write(name, "已保存")

    def _on_delete(self) -> None:
        """两段式：点一次变「确认删除」，再点才删（**零模态**，不用弹框问）。"""
        if self._entry is None:
            return
        if not self._confirm_delete:
            self._confirm_delete = True
            self._del_btn.setText("确认删除")
            self._feedback.warn("再点一次就删掉这一条")
            return
        remove_custom(self._config, self._entry.name)
        self._sync_rebind(self._entry.name)
        self.back.emit()

    # ---------- 内部 ----------

    def _name(self) -> str:
        return self._entry.name if self._entry else ""

    def _valid(self) -> bool:
        if not self._is_custom:
            return True
        return bool(self._draft["title"]) and bool(self._draft["command"])

    def _sync_actions(self) -> None:
        """保存按钮 ＋ 页脚提示同步（**按钮答有没有要保存的，提示答为什么**）。

        旧口径只看字段填没填齐 ⇒ 内置条目一进来「保存」就是亮的，点了什么都没发生
        —— 那是看起来最像坏了的一种。改成看脏标记之后：**灰 ＝ 没有要保存的东西**，
        一眼可读，也顺手省掉了那句常驻的「改动在点保存之后才生效」（它在保存之后
        还挂在那里，说的是已经过期的状态）。
        """
        self._save_btn.setEnabled(self._valid() and self._dirty)
        if not self._valid():
            self._screen.set_hint(_HINT_INCOMPLETE, role="caption_warn")
        elif self._dirty:
            self._screen.set_hint(_HINT_DIRTY, role="caption_warn")
        else:
            self._screen.set_hint("")

    def _mark_saved(self) -> None:
        """落盘之后把**当前草稿**记为「已落」：按钮变灰、页脚那句提示消失。"""
        self._initial = dict(self._draft)
        self._sync_actions()

    def _sync_enable_note(self) -> None:
        """开关那行补一句「关了会怎样」—— 开关自己只答开 / 关，答不了后果。"""
        self._enable_note.setText(_ENABLE_HINTS[self._switch.isChecked()])

    def _identity(self) -> str:
        """卡一标题：这一条的**来源身份**（删不删得掉看的正是它）。"""
        if self._entry is None:
            return "我的 · 可删"
        tail = "可删" if self._entry.deletable else "不可删"
        return f"{self._entry.source_label} · {tail}"

    def _after_write(self, name: str, ok_text: str) -> None:
        if self._sync_rebind(name):
            self._feedback.info(ok_text)
        else:
            self._feedback.error(f"{ok_text}，但没能挂上（检查辅助功能权限）")

    def _sync_rebind(self, name: str) -> bool:
        """写完盘**必须问一句重挂成没成**：写进配置却挂不上，用户看到的是
        「按下去没反应」，而配置里明明有值 —— 最难自查的一类。

        重挂答的是 ``REBIND_*`` 三态：**只有**「配置要它占键却没挂上」才算失败；
        「按你说的撤掉了」是成功，按旧口径（布尔）它也会落进失败那一头。
        """
        if self._on_rebind is None:
            return True
        return self._on_rebind(name) != REBIND_FAILED


def _replace(entry: HotkeyEntry, **kwargs) -> HotkeyEntry:
    """换掉 entry 的几个字段（薄封装：省得每个调用点都要想起它是 frozen 的）。"""
    return replace(entry, **kwargs)
