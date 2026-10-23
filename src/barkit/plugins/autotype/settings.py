"""autotype/settings：设置面 —— 设置窗里的「一键输入」页。

**为什么删掉 ``config_dialog.py``**（原 ``AutotypeConfigDialog``，2026-09-19）：

1. 它**直接持有 ``Config``**（构造形参就是 ``Config``，读走 ``user_plugin_section``、
   写走 ``update_plugin_section``）—— 按《插件结构规范》§4 跨面禁令 3，UI 一律不直接
   读写配置文件；配置 IO 只该在 ``plugin.py`` 里有一处（门禁
   ``tests/test_architecture.py::test_no_config_io_in_ui`` 守这条）；
2. 它自带 4 处 ``QMessageBox``（校验失败 / 名称重复 ×2）—— 从**置顶设置窗内部**弹模态框
   等于再开一条嵌套事件循环，实测会「点了没反应 → 再点一次 → 窗口卡死」
   （``ui_kit.FeedbackBar`` 的类 docstring 有崩溃报告）；
3. 它是个**脱离贡献体系**的平行入口（设置页一个按钮开一个私有 ``QDialog``），而设置窗
   本来就有一页的位置给这组配置；
4. 《插件结构规范-UI面》§7.2：设置类 UI（设置页 ＋ 它打开的子对话框）一律归
   ``settings.py``，旧名 ``config_dialog.py`` 一并废除。

**形态**：单栏「输入项列表 → 单条编辑」push 导航（macOS 设置窗惯用），与
``notify_guard/settings.py`` 的规则编辑器同款。旧对话框是 520×380 的双栏（左列表右表单），
而设置窗内容区约 390×330 —— 双栏放不下，这是形态改判的硬约束。

**全局默认三项**（``interval`` / ``enter_delay`` / ``auto_switch_ime``）此前**只有 YAML 能改**：
``engine.resolve_profile`` 读前两项、``plugin.type_profile`` 读第三项，而旧对话框一个控件
都没有 ⇒ 改只能手编 YAML。它们放列表页顶部一张「全局默认」卡里，与 profile 的关系是
**回退默认**（profile 里写了的字段以 profile 为准）。

**卡分组是 2026-09-21 的返工口径**：列表页两张卡（全局默认 / 输入项），编辑页一张卡
（这条输入项）＋ 页脚动作条。此前全局默认那两行毫秒格是**裸行**贴在页面底色上，
看不出它们与那个开关是同组；现在每一组控件都长在一张 ``settings_section`` 卡里
（微圆角 ＋ 标题 / 分隔线 ＋ 内容），卡外不留裸行。

本模块的纪律（同 ``notify_guard/settings.py`` / ``ipad_tunnel/settings.py``）：

- **不碰 ``ctx`` / ``config``**：读走 ``plugin.profiles()`` / ``plugin.autotype_settings()``，
  写走 ``plugin.save_profiles()`` / ``plugin.save_autotype_settings()`` —— 配置 IO 全在
  ``plugin.py``（门禁 ``test_no_config_io_in_ui``）；
- **零模态框**：校验失败走行内 ``FeedbackBar``（门禁
  ``test_plugin_settings_pages_have_no_modal_dialogs`` 扫本文件）；
- **不自建时钟**：本页无轮询、无 ``QTimer``；
- 外壳统一用 ``shell.ui_kit`` 的 ``settings_page_shell`` / ``settings_section`` /
  ``Switch`` / ``HoverRow`` / ``FeedbackBar`` / ``note`` / ``tool_button``；每一组控件
  都长在一张分区卡里（微圆角 ＋ 标题 / 分隔线 ＋ 内容），卡外不留裸行 —— 2026-09-21
  的返工口径，同 ``notify_guard`` / ``ipad_tunnel`` / ``hosts_guard`` / ``recognize``；
- **文字样式一律走 ``apply_text_role``**（`caption` / `section` / `chevron`…），本文件不再
  自拼 QSS，也不再自取令牌 —— 门禁 ``test_no_inline_stylesheet`` 守这一条。

**「保存」键的边界**：设置页本身**没有页级保存**（《录屏通知》§7 第 1 条口径）—— 全局默认
三项改完即写。单条输入项的编辑页有一枚「保存」，那是**单条事务**的确认（名称 / 文本 /
倒计时三样一起校验通过才落盘），与 ``notify_guard`` 规则编辑器同一处理；页级「完成」
仍由外层壳给。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from barkit.plugins.autotype import engine
from barkit.shell.ui_kit import (
    EditScreen,
    EditStack,
    ElideLabel,
    EmptyState,
    FeedbackBar,
    HoverRow,
    NumberField,
    Switch,
    apply_text_role,
    check_field,
    form_row,
    form_row_stacked,
    note,
    settings_page_shell,
    settings_section,
    tool_button,
)

if TYPE_CHECKING:
    from barkit.plugins.autotype.plugin import Plugin


def _eye_icon(open_eye: bool) -> QIcon:
    """程序绘制眼睛图标：open=True 画睁眼，False 画闭眼（上掏弧线）。

    与 ``config_dialog.py`` 里那枚逐像素同款 —— 它只是搬了家，没有重画。
    """
    pm = QPixmap(20, 20)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(120, 120, 120), 1.6))
    if open_eye:
        painter.drawEllipse(2, 6, 16, 8)      # 眼壳
        painter.setBrush(QColor(120, 120, 120))
        painter.drawEllipse(8, 8, 4, 4)       # 瞳孔
    else:
        painter.drawArc(2, 6, 16, 8, 0, 180 * 16)  # 闭眼：上掏弧线
    painter.end()
    return QIcon(pm)


def build_page(plugin: "Plugin") -> QWidget:
    """构建设置页（框架按 ``SettingsPageSpec.build()`` **零参**调用，故插件侧用闭包注入）。"""
    page, layout = settings_page_shell("一键输入")
    layout.addWidget(_ProfileEditor(plugin, page), stretch=1)
    return page


def _ms_row(lay: QVBoxLayout, parent: QWidget, label: str,
            value_ms: int, lo: int, hi: int, step: int) -> NumberField:
    """一行「字段名 ＋ 毫秒整数格子」。

    两个数值项用**毫秒**而不是秒：出厂值是 ``interval: 0.03`` / ``enter_delay: 0.3``，
    按秒就得做成两位小数输入；毫秒是整数，步进直观，写回时除 1000。
    """
    box = NumberField(parent)
    box.setRange(lo, hi)
    box.setSingleStep(step)
    box.setSuffix(" 毫秒")
    box.setValue(value_ms)          # setValue 自带夹取
    # 毫秒格是窄取值控件 ⇒ control_fill=False（拉满会撑成半屏）
    lay.addLayout(form_row(label, box, parent, control_fill=False))
    return box


class _ProfileEditor(QWidget):
    """设置页主体：全局默认三项 ＋ 单栏「输入项列表 → 单条编辑」push 导航。"""

    def __init__(self, plugin: "Plugin", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._plugin = plugin
        self._profiles: list[dict] = plugin.profiles()
        #: 正在编辑的下标；``None`` ＋ ``_adding`` 为假 = 不在编辑态。
        self._editing: int | None = None
        #: 正在新增（草稿还没进列表）。**记这个标志，不拿「名称为空」当判据** ——
        #: 用户完全可能故意清空名字或文本，那种情况该报校验错、不该被当成"没在编辑"。
        self._adding = False

        self._stack = EditStack(self._build_list_page(), self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        # 编辑屏**只造一次**并登记进换屏容器（列表型编辑屏的定式：再进去时状态照旧）。
        self._edit = self._build_edit_page()
        self._stack.push(self._edit)
        self._stack.back()               # 开页停在列表
        outer.addWidget(self._stack)
        self._refresh_list()

    # ---------- 第 1 屏：全局默认 ＋ 输入项列表 ----------

    def _build_list_page(self) -> QWidget:
        page = QWidget(self)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)          # 卡与卡的间隔（卡内段距归 `settings_section`）

        cfg = self._plugin.autotype_settings()

        # ── 全局默认卡：旧对话框一个控件都没有的三个键 ──
        glob_card, glob_lay = settings_section("全局默认", parent=page)
        gl = QHBoxLayout()
        gl.setSpacing(8)
        gl.addWidget(QLabel("非英文布局时自动切 ABC", glob_card), stretch=1)
        self._ime_sw = Switch(glob_card)
        self._ime_sw.setChecked(cfg["auto_switch_ime"])
        self._ime_sw.clicked.connect(self._on_toggle_ime)
        gl.addWidget(self._ime_sw)
        glob_lay.addLayout(gl)

        self._interval_spin = _ms_row(
            glob_lay, glob_card, "逐字符输入间隔", int(round(cfg["interval"] * 1000)), 1, 200, 5)
        self._enter_delay_spin = _ms_row(
            glob_lay, glob_card, "输完到按回车之间的等待",
            int(round(cfg["enter_delay"] * 1000)), 0, 2000, 10)
        # 两个格子共用一个槽（`_on_global_edited` 用 ``sender()`` 认领是谁改的）：
        # ``editingFinished`` 而非 ``valueChanged`` —— 后者打字途中每个中间数字写一次盘。
        for box in (self._interval_spin, self._enter_delay_spin):
            box.editingFinished.connect(self._on_global_edited)
        glob_lay.addWidget(note(
            "输入项没写这两项时回退到这里的值。间隔太小会被开了 Secure Event Input 的"
            "应用吞字符（那是系统限制，与间隔无关）。",
            glob_card,
        ))
        lay.addWidget(glob_card)

        # ── 输入项列表卡 ──
        list_card, list_lay = settings_section("输入项", parent=page)
        head = QHBoxLayout()
        head.setSpacing(6)
        add_btn = tool_button("＋ 新增", "pill-ghost", size="inline", parent=list_card)
        add_btn.clicked.connect(self._on_add)
        head.addWidget(add_btn)
        head.addStretch(1)
        list_lay.addLayout(head)

        self._list_fb = FeedbackBar(list_card)
        list_lay.addWidget(self._list_fb)

        self._rows_box = QWidget(list_card)
        self._rows_lay = QVBoxLayout(self._rows_box)
        self._rows_lay.setContentsMargins(0, 0, 0, 0)
        self._rows_lay.setSpacing(4)
        # **不给输入项清单另套滚动区**（2026-10-06 删）：本页由设置窗统一包进页级滚动区
        # （`shell/settings/helpers.py::_scrolled`）—— 这里再包一层，内容顶出视口时
        # 就是两条竖滚动条并存，滚轮只驱动鼠标底下那一条。内层那条还会被压扁：它的
        # `sizeHint` 是 0，跟卡片一起分剩余高度（实测宿主 490×360、14 条占位行，
        # 内层区只拿到 72px 而内容要 419px）。行随内容自然长高，滚动交给外面那一层。
        list_lay.addWidget(self._rows_box, stretch=1)
        lay.addWidget(list_card, stretch=1)
        return page

    def _build_profile_row(self, idx: int, profile: dict) -> QWidget:
        """一行输入项：名称 ＋ 参数摘要 ＋ ›（设置页的 push 导航语汇）。

        **只显示名称与参数，不显示明文** —— 与面板行副标题同一条口径（密码不进副标题），
        何况这里就在设置窗里、可能有人从背后看屏幕。
        """
        name = str(profile.get("name") or "?")
        row = HoverRow(on_click=lambda i=idx: self._open_edit(i), parent=self._rows_box)
        h = QHBoxLayout(row)
        h.setContentsMargins(10, 6, 10, 6)
        h.setSpacing(8)
        h.addWidget(QLabel(name, row))
        bits: list[str] = []
        delay = profile.get("delay")
        bits.append(f"倒计时 {int(delay)} 秒" if delay is not None else "倒计时跟随全局")
        if profile.get("press_enter"):
            bits.append("输完回车")
        sub = ElideLabel(" · ".join(bits), row)
        apply_text_role(sub, "caption")
        h.addWidget(sub, stretch=1)
        chev = QLabel("›", row)
        apply_text_role(chev, "chevron")
        h.addWidget(chev)
        return row

    def _refresh_list(self) -> None:
        """重建输入项列表（打开本页与每次增删改后各调一次）。"""
        # 局面已变：上一句回执随之过期（挂着的确认这里没有 —— 删除是立即生效的）。
        self._list_fb.dismiss()
        while self._rows_lay.count():
            item = self._rows_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        if self._profiles:
            for i, p in enumerate(self._profiles):
                self._rows_lay.addWidget(self._build_profile_row(i, p))
        else:
            self._rows_lay.addWidget(EmptyState(
                "还没有输入项", "点「＋ 新增」加一条", self._rows_box
            ))
        self._rows_lay.addStretch(1)

    # ---------- 第 2 屏：单条输入项编辑 ----------

    def _build_edit_page(self) -> EditScreen:
        """第 2 屏：单条输入项编辑 —— 版面归框架（`ui_kit.EditScreen`）。

        ◷ 2026-09-27：标题行 / 页脚 / 反馈槽三个自拼件改由 `EditScreen` 给（与
        `notify_guard` 那处同批）。**观感变更**：反馈槽由按钮下方移到上方、内容不再
        自带卡面（整屏已是 ``surface2`` 的一块面）。

        保存那一道**注入**（`EditScreen` 是组合件，覆写调不到）：`_validate_form`
        过不了关就一个字不落盘，过了才走 `_commit_form`。
        """
        edit = EditScreen(
            on_validate=self._validate_form,
            on_commit=self._commit_form,
            parent=self,
        )
        edit.back.connect(self._on_back)
        # 本页的错误提示落这一条槽（零模态框）
        self._edit_fb = edit.feedback

        # 名称 / 文本 / 倒计时 / 回车：都是**这一条输入项**的字段，同组
        basic, basic_body = edit.add_group("这条输入项")
        self._name_edit = QLineEdit(basic)
        self._name_edit.setPlaceholderText("如 cloudclient / wifi")
        basic_body.addWidget(form_row_stacked("名称", self._name_edit, basic))

        self._text_edit = QLineEdit(basic)
        self._text_edit.setPlaceholderText("要注入的文本")
        # 默认不显示明文（旧对话框同款）；眼睛按钮在尾部切换。
        self._text_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._reveal_action = self._text_edit.addAction(
            _eye_icon(False), QLineEdit.ActionPosition.TrailingPosition)
        self._reveal_action.setToolTip("显示明文")
        self._reveal_action.triggered.connect(self._toggle_reveal)
        basic_body.addWidget(form_row_stacked(
            "输入文本（明文，密码或任意文本）", self._text_edit, basic,
        ))

        drow = QHBoxLayout()
        drow.setSpacing(8)
        self._delay_spin = NumberField(basic)
        self._delay_spin.setRange(0, 60)
        self._delay_spin.setSuffix(" 秒")
        self._delay_spin.setEnabled(False)
        # 勾选框 ↔ 秒数格：`on_toggled` 拿到的就是翻转后的新值，直接喂 `setEnabled`
        # （不必回读 `isChecked()` —— 信号发出时控件已是新态）。
        self._delay_check = check_field(
            "自定义倒计时", on_toggled=self._delay_spin.setEnabled, parent=basic,
        )
        drow.addWidget(self._delay_check)
        drow.addWidget(self._delay_spin)
        drow.addStretch(1)
        basic_body.addLayout(drow)

        self._enter_check = check_field("输入后按回车", parent=basic)
        basic_body.addWidget(self._enter_check)
        basic_body.addWidget(note(
            "倒计时是留给你切到目标窗口的时间；不勾「自定义」就用上面的全局默认。", basic
        ))

        edit.add_destructive("删除这条", self._on_delete)
        return edit

    # ---------- 交互 ----------

    def _on_global_edited(self) -> None:
        """两个毫秒格子共用：``sender()`` 认领是谁改的，**只写它那一个键**。

        不把两个键一起写回 —— 那会把用户没碰过的那个也变成显式配置项，用户 YAML 里
        多出一行他没写过的键（同 ``ipad_tunnel`` 「只写 patch 里给出的键」）。
        """
        box = self.sender()
        key = "interval" if box is self._interval_spin else "enter_delay"
        value = box.value() / 1000.0
        if not self._plugin.save_autotype_settings({key: value}):
            self._list_fb.error("写回配置没成功——改动没存下，请重试。")
            return
        self._list_fb.info("全局默认已保存。")

    def _on_toggle_ime(self) -> None:
        """自动切 ABC：``Switch`` 只有 ``clicked``、不自翻自身，故推出目标态再回读同步。"""
        want = not self._ime_sw.isChecked()
        if not self._plugin.save_autotype_settings({"auto_switch_ime": want}):
            self._list_fb.error("写回配置没成功——改动没存下，请重试。")
            return
        self._ime_sw.setChecked(self._plugin.autotype_settings()["auto_switch_ime"])

    def _toggle_reveal(self) -> None:
        """眼睛按钮：在 密文 ↔ 明文 之间切换，图标 / 提示随状态联动。"""
        if self._text_edit.echoMode() == QLineEdit.EchoMode.Password:
            self._text_edit.setEchoMode(QLineEdit.EchoMode.Normal)
            self._reveal_action.setIcon(_eye_icon(True))
            self._reveal_action.setToolTip("隐藏明文")
        else:
            self._text_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self._reveal_action.setIcon(_eye_icon(False))
            self._reveal_action.setToolTip("显示明文")

    def _open_edit(self, idx: int) -> None:
        p = self._profiles[idx]
        self._editing = idx
        self._adding = False
        self._name_edit.setText(str(p.get("name") or ""))
        self._text_edit.setText(str(p.get("text") or ""))
        delay = p.get("delay")
        self._delay_check.setChecked(delay is not None)
        self._delay_spin.setEnabled(delay is not None)
        self._delay_spin.setValue(int(delay) if delay is not None else 4)
        self._enter_check.setChecked(bool(p.get("press_enter", False)))
        self._edit.set_title(str(p.get("name") or "未命名"))
        self._edit_fb.dismiss()          # 换了一条，上一条的提示不再适用
        self._stack.push(self._edit)

    def _on_back(self) -> None:
        """返回列表（丢弃本次未保存的字段改动 —— 保存是显式的「保存」按钮）。"""
        self._editing = None
        self._adding = False
        self._edit_fb.dismiss()
        self._stack.back()
        self._refresh_list()

    def _on_add(self) -> None:
        """新增：进编辑页填草稿，**保存后才进列表、也才落盘**。

        不在这里先往列表塞一条占位 —— ``engine.validate_profile`` 要求文本非空，占位条目
        一旦写回就是一条永远拒绝执行的坏数据（坏在用户看不见的地方，最难查）。
        """
        self._editing = None
        self._adding = True
        # 预填一个不撞车的名字：新装用户的第一个动作不该是「起名」（同《录屏通知》
        # 「常用场景」的道理），而且这样「名称为空」的报错只在用户**故意**清空时出现。
        self._name_edit.setText(self._unique_name("新输入项"))
        self._text_edit.clear()
        self._delay_check.setChecked(False)
        self._delay_spin.setEnabled(False)
        self._delay_spin.setValue(4)
        self._enter_check.setChecked(False)
        self._edit.set_title("新增输入项")
        self._edit_fb.dismiss()
        self._stack.push(self._edit)

    def _validate_form(self) -> "str | None":
        """保存前置门（注入给 `EditScreen` 的 `on_validate`）：名称 / 文本 / 倒计时。

        校验用 ``engine.validate_profile`` —— 与执行路径**同一个纯函数**（两处各写一套
        就会漂）。返回文案 = 一个字都不落盘、红字落本屏。
        """
        name = self._name_edit.text().strip()
        text = self._text_edit.text()
        delay = self._delay_spin.value() if self._delay_check.isChecked() else None
        error = engine.validate_profile(name, text, delay)
        if error:
            return error
        if any(
            str(p.get("name")) == name
            for i, p in enumerate(self._profiles) if i != self._editing
        ):
            return f"已存在名为「{name}」的输入项，名称不能重复。"
        return None

    def _commit_form(self) -> None:
        """保存（注入给 `EditScreen` 的 `on_commit`）：写过盘才回列表并报回执。"""
        if self._editing is None and not self._adding:
            self._stack.back()
            return
        name = self._name_edit.text().strip()
        text = self._text_edit.text()
        delay = self._delay_spin.value() if self._delay_check.isChecked() else None

        profile: dict = {"name": name, "text": text}
        if delay is not None:
            profile["delay"] = delay
        if self._enter_check.isChecked():
            profile["press_enter"] = True

        adding = self._adding
        if adding:
            self._profiles.append(profile)
        else:
            self._profiles[self._editing] = profile   # type: ignore[index]
        if not self._save():
            # 写盘失败 ⇒ 内存态回滚，别让界面显示一条其实没存下的条目。
            if adding:
                self._profiles.pop()
            return
        self._editing = None
        self._adding = False
        self._stack.back()
        self._refresh_list()
        self._list_fb.info(f"已{'添加' if adding else '保存'}「{name}」。")

    def _on_delete(self) -> None:
        """删除当前这条：**立刻落盘**（列表编辑器没有「取消」语义，删了就是删了）。"""
        if self._editing is None:
            return
        removed = self._profiles.pop(self._editing)
        name = str(removed.get("name") or "?")
        if not self._save():
            self._profiles.insert(self._editing, removed)
            return
        self._editing = None
        self._adding = False
        self._stack.back()
        self._refresh_list()
        self._list_fb.info(f"已删除「{name}」。")

    def _save(self) -> bool:
        """写回整表（``profiles`` 是列表，只能整键覆盖）；失败时说话，由调用方回滚内存态。"""
        if self._plugin.save_profiles(self._profiles):
            return True
        self._edit_fb.error("写回配置没成功——改动没存下，请重试。")
        return False

    def _unique_name(self, base: str) -> str:
        """给草稿一个不撞车的默认名。"""
        names = {str(p.get("name")) for p in self._profiles}
        if base not in names:
            return base
        i = 2
        while f"{base}{i}" in names:
            i += 1
        return f"{base}{i}"
