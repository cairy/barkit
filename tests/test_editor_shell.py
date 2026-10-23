"""tests/test_editor_shell：编辑屏四区外壳（2026-09-23 新增）。

来历：用户在用完当天上午的「快捷键」页后报「编辑界面排版太杂乱」，要求**由框架给一个
版面、所有场景共用**。仓里当时已有三处「列表屏 → 编辑屏」，页脚各排各的
（``[删除] <stretch> [保存] 文案`` / ``<stretch> [保存] [取消]`` / ``[导入] [返回] <stretch>``）。
这一批把版面收成 ``ui_kit.EditorShell`` 的四区，并**只迁**快捷键那一屏。

判据分两层，缺一层就会重新长回杂乱：

- **次序 / 位置**（几何）：四区自上而下、破坏性动作在最左、主动作贴右缘；
- **内容区只放卡**（类型）：裸行落在卡外就没有归属 —— 这一条是**判据**不是口头约定，
  故有一个「违规必抓」的控制组（只留一句 `assert` 的话，写漏判据也没人知道）。
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QPushButton, QWidget

from barkit.shell import styles, ui_kit
from barkit.shell.design import metrics
from barkit.shell.hotkey_catalog import SOURCE_FRAMEWORK, HotkeyEntry
from barkit.shell.ui_kit import EditorShell, FormGroup, form_group

#: 页脚提示的判定文案。**测试自己给、不靠调用方传的那句**：提示现在是**状态位**
#: （`set_hint` 按状态改写），写死「改动在点…」去找，哪天调用方换文案这条就空转了。
_HINT = "改动在点「保存」之后才生效"


# ---------- 拼装 ----------


def _shell(qtbot, *, hint=_HINT, size=(440, 360)) -> EditorShell:
    shell = EditorShell("新增快捷键", hint=hint)
    qtbot.addWidget(shell)
    shell.resize(*size)
    return shell


def _group(shell: EditorShell, title: str = "这条快捷键"):
    """往内容区放一个分组（调用方唯一该用的放法）。"""
    box, body = form_group(title, shell)
    shell.content.addWidget(box)
    return box, body


def _y(shell: EditorShell, widget: QWidget) -> int:
    """把 widget 顶边换算到**那一块面**的坐标系（量位置一律先过这一道）。

    这一屏里的东西分属**三层父件**：导航带与底栏长在面上、动作行与反馈槽长在底栏
    容器里、而按钮又长在动作行的左/右组里。各自的 ``y()`` 都是**容器内**坐标，直接
    比就是坐标系对撞 —— 实测按钮 ``y()=9``、分组 ``y()=40``，判据凭空报「动作行跑到
    内容区上面了」（2026-09-23 踩过一次，2026-09-24 面板化后又踩一次）。
    """
    return widget.mapTo(shell.panel(), QPoint(0, 0)).y()


def _x(shell: EditorShell, widget: QWidget) -> int:
    """把 widget 左缘换算到**那一块面**的坐标系（理由同 :func:`_y`）。"""
    return widget.mapTo(shell.panel(), QPoint(0, 0)).x()


def _lay_out(widget: QWidget) -> None:
    """显示并跑一遍事件循环：几何要落定之后才量得到（离屏下同样成立）。"""
    widget.show()
    QApplication.processEvents()


def _nav(shell: EditorShell) -> QWidget:
    nav = shell.findChild(QWidget, "editor_nav")
    assert nav is not None, "找不着导航带（它没有 objectName ⇒ 按名字找是唯一稳定的找法）"
    return nav


def _bare_content(shell: EditorShell) -> list[str]:
    """内容区里**不是分组**的一级子件（判据本体）。

    「分组外的裸行没有归属」不是排版偏好：一屏里飘着几行不知道属于谁的设置项，就是
    2026-09-21「设置页分区卡」与本次「编辑界面杂乱」的同一个病。故这条判据不靠肉眼。

    分组 2026-09-24 起**不带面**（整屏那一块面由壳给，面里不再铺底），于是不能靠
    ``isinstance(w, Card)`` 认它 —— 它跟一行裸控件都是 ``QWidget``。故 :func:`form_group`
    返回一个具名类型 :class:`FormGroup`，判据按类型认（不走 objectName：那是给 QSS
    用的，为判据挂 id 是「设了等于没设」）。
    """
    return [
        type(w).__name__
        for w in shell.content_widgets()
        if not isinstance(w, FormGroup)
    ]


def _hint_label(shell: EditorShell) -> QLabel:
    """页脚那句提示（走壳的**判据读口** ``hint_label()``）。

    不按 objectName 找：QLabel 上的 objectName 必须在 QSS 里真有规则，而这只是测试
    要的抓手 —— 为它加一条 id 规则就是「设了等于没设」。
    """
    lbl = shell.hint_label()
    assert isinstance(lbl, QLabel), "页脚提示不是 QLabel ⇒ 判据量错了对象"
    return lbl


# ---------- 一、四区的次序 ----------


def test_the_four_zones_stack_top_to_bottom_in_a_fixed_order(qtbot):
    """① 导航带 → ② 内容区 → ③ 反馈槽 → ④ 动作行，自上而下不许换位。

    **反馈槽排在动作行之上**（2026-09-24 三度改序 —— 前两版的判决**两两相反**，因为
    前提变了两次，**别当成自相矛盾**）。三条判决躲的是同一件事（点完「保存」按钮不
    许自己挪位置），结论跟着「底栏怎么固定」走：

    - 底栏**贴底**（第一批）⇒ 插在按钮下方的东西会把按钮往上顶 ⇒ 反馈在按钮**之上**；
    - 底栏**顶边固定**、从上往下长（第三批）⇒ 反馈在按钮**之下**才不动；
    - 重新贴底（本批：整屏成了面板，底栏贴在面板底边）⇒ 又回到按钮**之上**。

    量的是**真实几何**而不是布局下标：布局怎么装是壳的实现，用户看到的是四条横带
    的上下关系 —— 换一种装法（比如把动作行塞进一个容器）不该让这条判据失效。
    """
    shell = _shell(qtbot)
    group, _body = _group(shell)
    delete = shell.add_action("删除", lambda: None, kind="destructive")
    save = shell.add_action("保存", lambda: None, kind="primary")
    # 反馈槽平时是隐藏的（FeedbackBar 的定式）：不唤出来就量不到它的位置
    shell.feedback.info("占位")
    _lay_out(shell)

    nav = _nav(shell)
    assert _y(shell, nav) == 0, (
        f"导航带没贴在面的顶边（它顶边在 {_y(shell, nav)}）—— 带子 2026-09-27 起端到端，"
        "此前四周留着面内边距（用户报「标题栏与外部边框之间有空隙」）"
    )
    assert _y(shell, group) >= _y(shell, nav) + nav.height(), "内容区跑到导航带上面去了"
    assert _y(shell, shell.feedback) >= _y(shell, group) + group.height(), (
        "反馈槽跑到内容区里去了 ⇒ 它没在底栏里"
    )
    assert _y(shell, delete) >= _y(shell, shell.feedback) + shell.feedback.height(), (
        "动作行没排在反馈槽之下 ⇒ 唤出反馈会把按钮顶走"
    )
    assert _y(shell, delete) == _y(shell, save), "左端的破坏性动作与右端的主动作不在同一行"
    assert shell.feedback.isVisible(), "反馈槽被唤出来了却不可见 ⇒ 上面那条位置判据是空转的"


def test_the_feedback_bar_grows_without_moving_the_action_row(qtbot):
    """唤出反馈条时**动作行不许动** —— 这是「反馈槽排在动作行之上」那条判据的**理由**。

    只量次序挡不住「两区之间还有别的弹性项」这类排法：那样次序也对，但反馈一出现
    动作行照样被顶。真正要守的是**用户刚点过的那颗按钮不许自己挪位置**。
    """
    shell = _shell(qtbot)
    _group(shell)
    save = shell.add_action("保存", lambda: None, kind="primary")
    _lay_out(shell)
    before = _y(shell, save)

    shell.feedback.info("已保存")
    _lay_out(shell)
    assert shell.feedback.isVisible(), "反馈条没被唤出来 ⇒ 下面那条是空转的"
    assert _y(shell, save) == before, (
        f"唤出反馈条之后主动作从 {before} 挪到了 {_y(shell, save)} —— 点完保存按钮自己跳了"
    )


def test_the_footer_sits_on_the_bottom_edge_of_the_panel(qtbot):
    """底栏贴在**那一块面的底边**（弹窗的按钮本就在底部），不是悬在页面中间。

    这条是「不像弹窗」的正面判据：底栏的位置由**面**定（面的底边），而不是由内容末尾定。
    上一版把弹性空间沉到底栏**之下**想躲开「内容与按钮隔着 215px」，结果按钮悬在页面
    中间、下面吊一片空白（用户判「更丑」）—— 根因是那段空白没有被任何面兜住，不是
    弹性项放错了位置。

    ◷ 2026-09-27：面的内边距不再由根部统一给（导航带要端到端），底栏自带那份下内边距
    ⇒ 量的式子从「面高 − 面内边距」变成「面的底边」，**含义一个字没变**（贴的仍是面的
    底边，按钮离底边仍是 `PAD_CARD[3]`）。
    """
    shell = _shell(qtbot)
    _group(shell)
    _lay_out(shell)

    footer = shell.footer_widget()
    bottom = _y(shell, footer) + footer.height()
    want = shell.panel().height()
    assert bottom == want, (
        f"底栏底边在 {bottom}，该贴在面的底边 {want}（面高 {shell.panel().height()}）"
    )


def test_the_footer_stays_on_the_bottom_edge_whatever_the_shell_height_is(qtbot):
    """换一个壳高，底栏**仍贴着面的底边**，多出来的高度全进**内容区**。

    这条守的是「弹性项的归属」：多出来的高度必须落在内容区与底栏**之间**。跑到别处
    ⇒ 底栏悬在中间（第三批）、或组被拉开、或干脆没有东西吸收余高（实测：卡被拉高、
    一收缩就把底栏整体带上去，按钮位移 −56）。

    ◷ 2026-10-16：量的对象从「内容底边到面底边的空」改成**内容区（滚动区）自身的高**
    —— 内容区自带滚动区之后，余高落进**滚动区内部**（组下方那段留白由内容布局末尾的
    ``addStretch`` 兑现），滚动区外面那段空恒为 GAP_LG ＋ 一条发丝线。旧量法在新结构下
    恒等于一个常数（13），守不住任何东西。
    """
    shell = _shell(qtbot)
    group, _body = _group(shell)
    _lay_out(shell)

    def inset() -> int:
        footer = shell.footer_widget()
        return shell.panel().height() - (_y(shell, footer) + footer.height())

    assert inset() == 0, "底栏没贴住面的底边 ⇒ 下面那条是空转的"
    short_room = shell.content_scroll().height()
    short_group = group.height()
    shell.resize(440, 620)  # 同一屏内容，壳高翻一档
    _lay_out(shell)
    assert inset() == 0, f"壳长到 620 之后底栏离面底边 {inset()} ⇒ 它没跟着面走"
    assert shell.content_scroll().height() > short_room, (
        f"壳长到 620 之后内容区还是 {shell.content_scroll().height()}"
        f"（360 高时是 {short_room}）⇒ 多出来的高度没进内容区"
    )
    assert group.height() == short_group, (
        "多出来的高度把那一组撑长了 —— 余高该落在组**下方**（内容布局末尾那段 "
        "``addStretch``），不是把已经排好的组拉开"
    )


def test_the_footer_keeps_its_distance_when_the_content_fills_the_screen(qtbot):
    """内容把这一屏撑满（``stretch`` 塌成 0）时，底栏与内容区**仍隔着不小于卡间距的空隙**。

    只靠 ``stretch`` 撑开是不够的：挤压态下它塌成 0，底栏就贴到内容上（组底到底栏只剩
    根布局的 ``GAP_MD`` 8，比组与组之间的 12 还近）⇒ 底栏看着像最后一组的延续。
    这条量的是**最坏情况**：拉伸态空隙有 200+，量它等于没量。
    """
    shell = EditorShell("新增快捷键", hint=_HINT)
    qtbot.addWidget(shell)
    for i in range(6):
        _group(shell, f"组{i}")
    shell.add_action("保存", lambda: None, kind="primary")
    # 不 resize：让壳按自己的 sizeHint 收缩，stretch 就没有余量可占
    shell.adjustSize()
    _lay_out(shell)

    groups = shell.content_widgets()
    last = groups[-1]
    gap = _y(shell, shell.footer_widget()) - (_y(shell, last) + last.height())
    assert gap >= metrics.GAP_LG, (
        f"挤压态下内容底边到底栏只剩 {gap}（组与组之间是 {metrics.GAP_LG}）"
        "⇒ 底栏贴到内容上、成了最后一组的延续"
    )


def test_the_footer_brings_no_surface_of_its_own(qtbot):
    """底栏**不许自带面**（既不是 ``Card`` 也不是 ``FormGroup``）—— 面由壳统一给。

    2026-09-23 定的是「底栏不给它套圆角卡」，理由是主次（套上「保存」就降级成卡里的
    一个字段）；2026-09-24 整屏面板化之后多了第二条、更要紧的理由：**自带面就是第二
    层**，底栏这块面与内容那两块面谁也盖不住谁 ⇒ 用户原话「按钮区域和其它区域不在
    一个层」。这条守的是「顺手跟上面统一一下」。
    """
    shell = _shell(qtbot)
    _group(shell)
    shell.add_action("保存", lambda: None, kind="primary")
    _lay_out(shell)

    band = shell.footer_widget()
    assert not isinstance(band, ui_kit.Card), (
        f"底栏被套进了 {type(band).__name__} —— 它自带一块面，与内容区不在一个层"
    )
    assert not isinstance(band, FormGroup), "底栏被当成一组输入项 ⇒ 它该是动作行不是分组"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_footer_paints_no_surface_of_its_own(qtbot, theme, monkeypatch):
    """底栏那块**裸容器**不许自带底色 —— 它的底该由面给。

    与上面那条是同一件事的两层：上面查**类型**（没套 Card），这条查**像素**。全局那条
    ``QWidget { background: surface }`` 连**裸 QWidget 实例**也管（自定义子类不画）⇒
    底栏自己铺了一块比面更亮的底 —— 2026-09-27 用户原话「action 按钮区域整个也有白色
    底色，违和」。修法：把它标成 ``ui_kit.PLAIN_ZONE``（那条 QSS 在 ``styles.py``）。

    量像素而不是「有没有设那个 objectName」：名字设了、QSS 那条却没命中（或哪天被人
    删掉）时，唯一的症状就是这块更亮的方片。
    """
    app = QApplication.instance() or QApplication([])
    before = styles.current_theme()
    styles.apply_theme(app, theme)
    monkeypatch.setattr(ui_kit.base, "_theme_tokens", lambda: styles.TOKENS[theme])
    shell = _shell(qtbot)
    _group(shell)
    shell.add_action("保存", lambda: None, kind="primary")
    _lay_out(shell)
    try:
        panel = shell.panel()
        img = panel.grab().toImage()
        rect = shell.footer_widget().geometry()
        # 底栏左侧那条内边距（12）之内、按钮的左边：那里只该有面底色
        left = rect.left() + 3
        got = [img.pixelColor(left, y).name().lower()
               for y in (rect.top() + 4, rect.top() + rect.height() // 2)]
        want = styles.TOKENS[theme]["surface2"].lower()
        bad = styles.TOKENS[theme]["surface"].lower()
        assert bad not in got, (
            f"底栏自带了一块页面底色 {bad}（量到 {got}）⇒ 它没被标成 PLAIN_ZONE"
        )
        assert got == [want, want], f"底栏那两处该是面底色 {want}，量到 {got}"
    finally:
        styles.apply_theme(app, before)


def test_the_action_region_is_separated_by_a_top_rule(qtbot):
    """动作区（底栏）顶边一条 1px 发丝线（``Divider``）—— 把动作块与表单区在视觉上分开。

    2026-09-24 晚补：整屏已是同一块 ``surface2`` 面板，线与内容同处一面，它只
    demarcate 底部动作块、不会复现 2026-09-23 那次「割裂」（那次根因是底栏裸控件坐
    页面底 ＋ 215px 缝的两层结构，不是线本身）。线走 ``border`` 令牌，与面板描边同色。
    它作为 ``_root`` 独立子项落在 stretch 与底栏之间，故底栏自身高度不变（不误伤
    ``test_the_footer_keeps_its_distance_when_the_content_fills_the_screen``
    与「底栏贴底」两条判据）。
    """
    shell = _shell(qtbot)
    _group(shell)
    shell.add_action("保存", lambda: None, kind="primary")
    _lay_out(shell)

    rules = shell.findChildren(ui_kit.Divider)
    assert len(rules) == 1, f"动作区顶边该有且只有一条发丝线，实际 {len(rules)} 条"
    rule = rules[0]
    footer = shell.footer_widget()
    # 发丝线贴在动作区顶边：它底边到动作区顶边只隔一档以内（GAP_MD）
    gap_to_footer = _y(shell, footer) - (_y(shell, rule) + rule.height())
    assert gap_to_footer <= metrics.GAP_MD + 1, (
        f"发丝线没贴在动作区顶边（线底边到动作区顶 {gap_to_footer}px）"
    )
    # **到得了头**（2026-09-27，用户问「横线分隔为什么不到头」）：面内边距归零之后它作为
    # 根布局的直接子项，左右各铺到面的边缘。此前它跟内容区一样缩在左右 12px 里。
    assert rule.width() == shell.panel().width(), (
        f"发丝线只有 {rule.width()} 宽，面是 {shell.panel().width()} —— 它没到两头"
    )


def test_the_nav_band_is_one_inline_button_plus_its_padding(qtbot):
    """导航带的高度 = **inline 档**按钮 ＋ 上下内边距 —— 守的是「带里那枚按钮是 inline 档」。

    这条判据跟 `metrics` 走（换档位 / 改内边距时它跟着走，不会两头分叉），但它**不是
    复述实现**：把返回键换成 ``action`` 档（22 → 28，一个很容易犯的「跟页脚按钮统一
    一下吧」）时，带子会悄悄长高一档，这条当场红。
    """
    shell = _shell(qtbot)
    _lay_out(shell)
    nav = _nav(shell)
    want = metrics.BTN_INLINE + 2 * metrics.PAD_NAV[1]
    assert nav.height() == want, f"导航带高 {nav.height()}，该是 {want}（inline 档 + 上下内边距）"


# ---------- 二、动作行的位置契约 ----------


def test_the_hint_does_not_make_the_footer_taller_than_its_buttons(qtbot):
    """页脚那一行的高度由按钮定，提示是**一行**边注、不许把页脚撑高。

    上一版提示被挤成两行（13 个字 132px 里折行）⇒ 页脚从 28 涨到 34，按钮边上挂着
    一小段孤字。判据是「提示落在按钮那条带子之内」，不是「提示高度等于 28」——
    后者把「谁撑高了谁」这件事说反了。
    """
    shell = _shell(qtbot)
    _group(shell)
    delete = shell.add_action("删除", lambda: None, kind="destructive")
    _lay_out(shell)

    hint = _hint_label(shell)
    top, bottom = _y(shell, delete), _y(shell, delete) + delete.height()
    assert _y(shell, hint) >= top and _y(shell, hint) + hint.height() <= bottom, (
        f"提示没落在按钮那条带子里（按钮 {top}~{bottom}，提示 "
        f"{_y(shell, hint)}~{_y(shell, hint) + hint.height()}）⇒ 页脚被撑高了"
    )


def test_set_hint_swaps_the_footer_note_and_an_empty_one_hides_it(qtbot):
    """页脚那句是**状态位**：`set_hint` 改写它，传空串则整条藏起来。

    挡的是「保存完了还挂着『改动在点保存之后才生效』」—— 一句常驻的静态说明在它
    描述的那个状态已经过去之后还在，说的就是假话。
    """
    shell = _shell(qtbot, hint="")
    _group(shell)
    shell.add_action("保存", lambda: None, kind="primary")
    _lay_out(shell)
    hint = _hint_label(shell)
    assert not hint.isVisible(), "一进来没什么可说的 ⇒ 提示该藏着（不说比说一句空话好）"

    shell.set_hint("有改动没保存", role="caption_warn")
    _lay_out(shell)
    assert hint.isVisible() and hint.text() == "有改动没保存", "提示没跟着状态改"

    shell.set_hint("")
    _lay_out(shell)
    assert not hint.isVisible(), "状态回到干净了，提示还挂在那儿 ⇒ 它在说过期的状态"


def test_a_destructive_action_sits_on_the_left_and_the_primary_on_the_right(qtbot):
    """破坏性动作贴左缘、主动作贴右缘、提示夹在中间 —— 「离得越远越好」是这一行的全部理由。

    左右缘取的是**那一块面**的内容边界（面宽 － 面内边距），不是窗口边缘：底栏在面
    里，它的左右缘与内容区里表单标签的左右缘是**同一条线**（换壳宽 / 改内边距都不该
    让这三样东西彼此错开）。
    """
    shell = _shell(qtbot)
    _group(shell)
    delete = shell.add_action("删除", lambda: None, kind="destructive")
    save = shell.add_action("保存", lambda: None, kind="primary")
    _lay_out(shell)

    panel = shell.panel()
    left, right = metrics.PAD_CARD[0], panel.width() - metrics.PAD_CARD[2]
    hint = _hint_label(shell)
    assert _x(shell, delete) == left, "破坏性动作没贴住左缘（左端留空会让它读成普通动作之一）"
    assert _x(shell, save) + save.width() == right, (
        "主动作没贴住右缘 —— 右侧还挂着东西（上一版「改动才生效」那句就吊在保存右边）"
    )
    assert _x(shell, delete) < _x(shell, hint) < _x(shell, save), (
        f"提示没夹在两者之间：删除 {_x(shell, delete)}、提示 {_x(shell, hint)}"
        f"、保存 {_x(shell, save)}"
    )


def test_an_unknown_action_kind_is_refused_with_the_legal_ones(qtbot):
    """位置档写错当场报错，并把合法值列出来（别静默变成第四种排法）。"""
    shell = _shell(qtbot)
    with pytest.raises(ValueError) as err:
        shell.add_action("取消", lambda: None, kind="nonsense")
    assert "destructive" in str(err.value) and "primary" in str(err.value), (
        f"报错没列合法值（用户没法照着改）：{err.value}"
    )


def test_the_back_button_emits_back(qtbot):
    """导航带那枚按钮的文案由调用方给，点了发 ``back``（换屏的接线在消费方那边）。"""
    shell = _shell(qtbot)
    _lay_out(shell)
    seen: list[bool] = []
    shell.back.connect(lambda: seen.append(True))
    back = [b for b in shell.findChildren(QPushButton) if b.text() == "返回"]
    assert len(back) == 1, f"导航带上该有一枚「返回」，实际找到 {len(back)} 枚"
    back[0].click()
    assert seen == [True]


# ---------- 三、内容区只放卡 ----------


def test_the_content_zone_takes_groups_and_nothing_else(qtbot):
    shell = _shell(qtbot)
    _group(shell, "这条快捷键")
    _group(shell, "执行结果")
    assert len(shell.content_widgets()) == 2, "内容区没装进去东西 ⇒ 下面的判据是空转的"
    assert _bare_content(shell) == [], f"内容区里混进了非分组的一级子件：{_bare_content(shell)}"


def test_the_content_zone_check_catches_a_bare_row(qtbot):
    """自证（控制组 = 违规形态）：往内容区直接塞一枚裸标签，上一条判据必须红。"""
    shell = _shell(qtbot)
    _group(shell)
    assert _bare_content(shell) == []          # 合规面先成立
    shell.content.addWidget(QLabel("裸行，没有归属", shell))
    assert _bare_content(shell) == ["QLabel"], "裸行没被判出来 ⇒ 上一条判据是摆设"


# ---------- 四、导航带真的画了带子 ----------


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_nav_band_paints_the_band_colour_not_the_page_surface(qtbot, theme, monkeypatch):
    """导航带的底色是 ``surface3``（与分区卡标题栏同一个值），不是页面底色；
    且它**端到端**铺到面的左右上三边。

    这条挡的是「带子静默变成一条平白无奇的行」：自绘一旦没跑到（或被覆盖），量到的
    就是页面底色 —— 那时导航区与内容区之间没有任何分界，而这一屏的「等级感」全靠它。

    ◷ 2026-09-27 加后半句：此前带子缩在面内边距里（左右各 12），用户报「标题栏有灰色
    背景，但没有覆盖整个标题栏区域（与外部边框之间有空隙）」。左端内侧与顶端中线这两
    处是**端到端之后才有带色**的采样点。
    """
    app = QApplication.instance() or QApplication([])
    before = styles.current_theme()
    styles.apply_theme(app, theme)
    # 自绘那条路走 ``ui_kit._theme_tokens()``（只读系统外观），与上面对齐到同一主题，
    # 否则深色那一轮量到的是浅色的带色（本机就是浅色），断言会指向假失败。
    monkeypatch.setattr(ui_kit.base, "_theme_tokens", lambda: styles.TOKENS[theme])
    shell = _shell(qtbot)
    _lay_out(shell)
    img = _nav(shell).grab().toImage()
    try:
        nav = _nav(shell)
        mid = nav.height() // 2
        # 右端内侧：带子右侧只留空（按钮 / 竖线 / 标题都靠左），量到的就是带色本身
        got = [img.pixelColor(nav.width() - 6 - i, mid).name().lower() for i in range(4)]
        # 左端内侧与顶端中线：端到端之后这两处也该是带色
        got += [img.pixelColor(5 + i, mid).name().lower() for i in range(4)]
        got += [img.pixelColor(nav.width() // 2, 3 + i).name().lower() for i in range(4)]
        band = styles.TOKENS[theme]["surface3"].lower()
        surface = styles.TOKENS[theme]["surface"].lower()
        assert band in got, f"带子上没量到 {band}：{got}"
        assert surface not in got, f"量到了页面底色 {surface} ⇒ 带子没画出来：{got}"
    finally:
        styles.apply_theme(app, before)


def test_the_nav_band_reaches_the_panel_edges(qtbot):
    """导航带**端到端**：左右铺满面的宽度、左上角贴在面的左上角。

    2026-09-27 用户上报「标题栏有灰色背景，但没有覆盖整个标题栏区域（与外部边框之间
    有空隙）」：此前带子四周留着 `PAD_CARD` 的面内边距（实测左 12 / 上 10），带子读起来
    是「一条浮在面中间的带子」，而分区卡的标题栏是端到端的 —— 同一门语言的两处落点
    就不该一处到边、一处不到。

    这条量**几何**（`test_the_nav_band_paints_the_band_colour…` 那条量像素），两条各管
    一半：几何对了但画法没跟上（四角圆成整圆、或没内缩）时像素那条红；画法对了但布局
    又给回边距时这条红。
    """
    shell = _shell(qtbot)
    _lay_out(shell)
    nav = _nav(shell)
    panel = shell.panel()
    assert nav.width() == panel.width(), (
        f"导航带只有 {nav.width()} 宽，面是 {panel.width()} —— 它左右没到边"
        "（2026-09-27 之前缩在面内边距里）"
    )
    assert (nav.geometry().left(), nav.geometry().top()) == (0, 0), (
        f"导航带没贴在面的左上角（它左上角在 {nav.geometry().topLeft().toTuple()}）"
    )


# ---------- 五、整屏是「一块面」 ----------


def test_the_whole_screen_is_one_surface_covering_itself(qtbot):
    """这一屏自己就是**一块面**：一张撑满本部件的 ``Card``。

    这条是整个「像弹窗 / 在一个层」的**正面判据**。设置页只给这一屏**位置**、不给
    它**面**，内容那两张卡自带面、底栏是裸控件 ⇒ 字面意义上的两层（用户报「按钮区域
    和其它区域不在一个层」）。面由壳给之后，导航带 / 内容 / 动作行全在同一块底上。

    量的是「面**撑满**这一屏」：只判「有那么一张 Card」挡不住它只是一块小牌子
    （那样内容与底栏照样分属两层）。
    """
    shell = _shell(qtbot)
    _group(shell)
    shell.add_action("保存", lambda: None, kind="primary")
    _lay_out(shell)

    panel = shell.panel()
    assert isinstance(panel, ui_kit.Card), (
        f"这一屏的面是 {type(panel).__name__}，不是 Card ⇒ 它没有自己的面"
    )
    assert panel.geometry() == shell.rect(), (
        f"那块面没撑满这一屏（面 {panel.geometry()}、壳 {shell.rect()}）"
        "⇒ 它只是一块牌子，内容照样分属两层"
    )


def test_no_group_brings_a_surface_of_its_own(qtbot):
    """面里的分组**不许自带面**（不能是 ``Card``）—— 面里有面就是两层。

    这条守 2026-09-24 那次定案：整屏是一块面之后，内容区若继续用 ``settings_section``
    （那是一张卡），就是「面板里再铺一层底」，两层描边两层圆角 —— 而这正是「不在
    一个层」的同一种病换了个位置长出来。
    """
    shell = _shell(qtbot)
    group, _body = _group(shell)
    _lay_out(shell)
    assert not isinstance(group, ui_kit.Card), (
        f"分组是 {type(group).__name__} ⇒ 它自带一块面，面里有面"
    )
    for w in shell.content_widgets():
        assert not isinstance(w, ui_kit.Card), (
            f"内容区里混进了自带面的 {type(w).__name__} ⇒ 面里有面"
        )


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_panel_sits_one_step_above_the_page_surface(qtbot, theme, monkeypatch):
    """那块面的底色是 ``surface2``（比设置页的 ``surface`` 深一档），**不是同一个色**。

    「深一档」不是审美取舍：同底的话这一屏与页面之间只有一条 1px 描边，读起来还是
    「几块部件摆在页面上」，没有「一块浮上来的面」—— 用户那句「不像弹窗」要的正是
    这个。量的是**像素**（同 :func:`test_the_nav_band_paints_the_band_colour` 那一套）：
    自绘一旦没跑到（或底色被改回 surface），这里量到的就是页面底色。
    """
    app = QApplication.instance() or QApplication([])
    before = styles.current_theme()
    styles.apply_theme(app, theme)
    monkeypatch.setattr(ui_kit.base, "_theme_tokens", lambda: styles.TOKENS[theme])
    shell = _shell(qtbot)
    _lay_out(shell)
    try:
        panel = shell.panel()
        img = panel.grab().toImage()
        # **量面自己的左侧内边距那一竖条**（内容区从 x=12 起、导航带也从 12 起，故 x=6
        # 一路都是面）。2026-09-27 之前的量法是「顶边内侧」—— 带子端到端贴顶之后那片被
        # 带色占了，量到的会是 surface3，指向假失败。
        mid = panel.height() // 2
        got = [img.pixelColor(6, mid + i).name().lower() for i in (-2, 0, 2)]
        want = styles.TOKENS[theme]["surface2"].lower()
        page = styles.TOKENS[theme]["surface"].lower()
        assert want in got, f"面上没量到 {want}（量到 {got}）⇒ 底色不是 surface2"
        assert page not in got, f"量到了页面底色 {page} ⇒ 面与页面同底，只有一条框：{got}"
    finally:
        styles.apply_theme(app, before)


# ---------- 六、迁移方：快捷键编辑屏的内容区全是分组 ----------


class _Cfg:
    """最小配置桩：新建那条一进来不碰配置，故只补「读」的那一口。"""

    def get(self, path, default=None):
        return default

    def set_hotkey(self, *_args):
        pass

    def remove_hotkeys(self, *_args):
        pass


def _builtin_entry() -> HotkeyEntry:
    """一条**内置且没改过**的条目（卡数最少的那一种：只有卡一）。"""
    return HotkeyEntry(
        name="framework.panel",
        title="打开面板",
        source=SOURCE_FRAMEWORK,
        owner="框架",
        description="开关菜单栏那块面板，等同于点菜单栏图标",
        combo="cmd+shift+b",
        default_combo="cmd+shift+b",
    )


def _hotkey_editor(qtbot, *, entry=None):
    from barkit.shell.hotkey_editor import HotkeyEditor

    editor = HotkeyEditor(config=_Cfg(), entry=entry)
    qtbot.addWidget(editor)
    editor.resize(520, 420)
    return editor


def _shell_of(editor) -> EditorShell:
    """取一台编辑屏的四区外壳。

    ⚠️ **调用方必须把编辑屏绑在局部变量上**（`editor = _hotkey_editor(qtbot)`），别写成
    `_shell_of(_hotkey_editor(qtbot))`：pytest-qt 的 `addWidget` 只存 **weakref**，而
    PySide6 只保活**直接父件** —— 外壳 2026-09-27 起深了一层（长在 `EditScreen` 里），
    临时对象写法下编辑屏当场被回收、外壳跟着变成空壳，报的是
    `libshiboken: Internal C++ object (QVBoxLayout) already deleted`（看着像壳坏了）。
    """
    shell = editor.findChild(EditorShell)
    assert shell is not None, "编辑屏里没有四区外壳 ⇒ 它还留着上一版那套平铺版面"
    return shell


def _assert_content_is_all_groups(shell: EditorShell) -> None:
    assert _bare_content(shell) == [], f"内容区混进了非分组的一级子件：{_bare_content(shell)}"
    assert shell.content_widgets(), "内容区是空的 ⇒ 上面的判据在空转"


def test_a_new_custom_entry_opens_one_group(qtbot):
    """新建那条：只有一组（这一条本身）。

    「测试」已内联进命令格、「上次触发」沉到卡底一行小字（新建条目从未触发、没有），
    故内容区只剩一组。组数是**结构性判据**：多一组说明有新的裸块挤进了内容区，
    少一组说明内容被搬到了组外。
    """
    editor = _hotkey_editor(qtbot)
    shell = _shell_of(editor)
    _assert_content_is_all_groups(shell)
    assert len(shell.content_widgets()) == 1, (
        f"新建那条该只有一组（这一条本身），实际 {len(shell.content_widgets())} 组"
    )


def test_the_test_button_is_inline_in_card_one_not_a_separate_card(qtbot):
    """「测试」内联进命令格（与「初始化」之于「键位」同款），不另起一张「执行结果」卡。

    拆卡是 2026-09-24 这轮返工要消掉的东西：卡里有卡就是「面里有面」，正是用户报
    「按钮区域和其它区域不在一个层」的根因；测试按钮必须长在卡一里，内容区不得再
    出现第二张「执行结果」分组。
    """
    editor = _hotkey_editor(qtbot)
    _lay_out(editor)
    shell = _shell_of(editor)
    assert len(shell.content_widgets()) == 1, (
        f"「执行结果」卡没拆掉：内容区还有 {len(shell.content_widgets())} 组"
    )
    group = shell.content_widgets()[0]
    assert editor._test_btn in group.findChildren(QPushButton), (
        "测试按钮没长在卡一里（它该内联在命令格）"
    )
    assert not any("执行结果" in lbl.text()
                   for lbl in shell.findChildren(QLabel)), "内容区还有「执行结果」卡标题"


def test_a_builtin_entry_keeps_its_source_line_inside_the_group(qtbot):
    """内置且没触发过的：只剩组一 —— 来源说明必须**在组里**，不许飘到组外。

    这一条专门守「组外的裸行」：来源说明是这一屏上最容易被顺手 ``content.addWidget``
    的一行（它看起来像「这一屏的副标题」），一旦飘出去，这一屏就又有了一段没归属的字。
    """
    editor = _hotkey_editor(qtbot, entry=_builtin_entry())
    shell = _shell_of(editor)
    _assert_content_is_all_groups(shell)
    assert len(shell.content_widgets()) == 1, (
        f"内置且没触发过的那条该只有一组，实际 {len(shell.content_widgets())} 组"
    )
    assert any("框架" in lbl.text() for lbl in shell.findChildren(QLabel)), (
        "卡里没有来源那一行 —— 它被挪到卡外了（判据只管「是不是卡」，内容还得在这儿）"
    )


def test_the_card_title_says_where_the_entry_comes_from(qtbot):
    """卡标题是**来源身份**（删不删得掉看的正是它），不是「这条快捷键」这种空转标题。

    整屏就是这一条，那个标题一个字的信息都没给；改说身份还顺手答了「为什么有的
    条目没有删除键」。
    """
    editor = _hotkey_editor(qtbot, entry=_builtin_entry())
    shell = _shell_of(editor)
    texts = [lbl.text() for lbl in shell.findChildren(QLabel)]
    assert "框架 · 不可删" in texts, f"卡标题没说来源身份：{texts}"
    assert "这条快捷键" not in texts, "卡标题还是那句等于没说的「这条快捷键」"


def test_the_switch_has_its_own_row(qtbot):
    """开关不再寄生在键位行里：它有自己的标签、自己的一行。

    值 / 启停 / 重置三个语义挤在一行时，标签只能盖住第一个 —— 开关就成了没有归属的
    第二颗控件，而它答的是比键位更高一层的问题（按了算不算）。
    """
    editor = _hotkey_editor(qtbot, entry=_builtin_entry())
    _lay_out(editor)
    # 两颗控件各装在自己的行容器里（`_wrap`），故 `y()` 是**容器内**坐标（居中后可能
    # 恰好相等）—— 要判「是不是同一行」得换算到同一个基准上。
    pill_y = editor._pill.mapTo(editor, QPoint(0, 0)).y()
    switch_y = editor._switch.mapTo(editor, QPoint(0, 0)).y()
    assert pill_y != switch_y, (
        f"开关还挂在键位那一行（药丸 y={pill_y}、开关 y={switch_y}）—— 一行三件事"
    )
    assert "启用" in [lbl.text() for lbl in editor.findChildren(QLabel)], (
        "开关那一行没有自己的标签 ⇒ 它又成了键位行的附属"
    )


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_row_boxes_paint_no_surface_of_their_own(qtbot, theme, monkeypatch):
    """表单行右侧那一格（``_wrap`` 造出来的容器）不许自带底色。

    2026-09-27 用户上报：「『键位』、『启动』等的右侧『控件位』背景是白色的，和其他的
    颜色违和」。同一个盒子长在设置页上**看不出来** —— 页面底色恰好也是 ``surface``；
    长在编辑屏那块 ``surface2`` 的面上一眼就是白块。修法与底栏同一条
    （``ui_kit.PLAIN_ZONE``），判据也同一条口径：量像素，不量有没有设那个名字。
    """
    app = QApplication.instance() or QApplication([])
    before = styles.current_theme()
    styles.apply_theme(app, theme)
    monkeypatch.setattr(ui_kit.base, "_theme_tokens", lambda: styles.TOKENS[theme])
    editor = _hotkey_editor(qtbot, entry=_builtin_entry())
    _lay_out(editor)
    try:
        panel = _shell_of(editor).panel()
        img = panel.grab().toImage()
        box = editor._pill.parentWidget()
        top_left = box.mapTo(panel, QPoint(0, 0))
        # 盒子右端那段是 stretch（药丸与「初始化」都在左边）⇒ 量到的只有底色
        x = min(top_left.x() + box.width() - 4, panel.width() - 3)
        y = top_left.y() + box.height() // 2
        got = [img.pixelColor(x - i, y).name().lower() for i in (-1, 0, 1)]
        want = styles.TOKENS[theme]["surface2"].lower()
        bad = styles.TOKENS[theme]["surface"].lower()
        assert bad not in got, (
            f"行容器自带了一块页面底色 {bad}（量到 {got}）⇒ 它没被标成 PLAIN_ZONE"
        )
        assert got == [want] * 3, f"行容器那处该是面底色 {want}，量到 {got}"
    finally:
        styles.apply_theme(app, before)


def test_the_save_button_tracks_whether_there_is_anything_to_save(qtbot):
    """保存按钮由「**有没有未保存改动**」驱动，不是「字段填没填齐」。

    旧口径只看字段 ⇒ 内置条目一进来「保存」就是亮的，点了什么都没发生 —— 那是看起来
    最像坏了的一种。改回原值也要能自己变干净，否则那句「有改动没保存」是假的。
    """
    editor = _hotkey_editor(qtbot, entry=_builtin_entry())
    shell = _shell_of(editor)
    save = [b for b in shell.findChildren(QPushButton) if b.text() == "保存"][0]
    _lay_out(editor)
    assert not save.isEnabled(), "内置条目一进来没什么要保存的 ⇒ 保存该是灰的"
    assert not _hint_label(shell).isVisible(), "没有要保存的东西，页脚也该一句都不说"

    editor._pill.captured.emit("ctrl+alt+x")       # 走正规信号路径，不是直接调内部方法
    assert save.isEnabled(), "改了键位 ⇒ 有东西要保存，保存该亮"
    assert _hint_label(shell).text() == "有改动没保存"

    editor._pill.captured.emit("cmd+shift+b")      # 改回出厂值
    assert not save.isEnabled(), "改回原值之后不该还挂着「有改动没保存」—— 那句话是假的"


def test_a_new_entry_says_why_it_cannot_be_saved(qtbot):
    """新建那条一进来字段是空的：保存是灰的，且页脚要说出**为什么灰**（不是干灰着）。

    一颗灰按钮自己不会说话 —— 用户只能猜是「还没填完」还是「这一条不许改」。
    """
    editor = _hotkey_editor(qtbot)
    shell = _shell_of(editor)
    save = [b for b in shell.findChildren(QPushButton) if b.text() == "保存"][0]
    _lay_out(editor)
    assert not save.isEnabled(), "名称与命令都空 ⇒ 保存该是灰的"
    assert _hint_label(shell).text() == "名称与命令都得填，填了才能保存"

    editor._title_edit.setText("试试")
    editor._cmd_edit.setText("/usr/bin/true")
    assert save.isEnabled(), "填齐了 ⇒ 有东西要保存，保存该亮"


# ---------- 六、选项释义只说当前选中那一项 ----------


def test_choice_hint_shows_only_the_selected_option(qtbot):
    """跟着选中项变的一句话：**没选中的那一档不许留在界面上**。

    三档释义同时摊开是 84 个字，把上面四颗控件整个埋掉（2026-09-23 观感返工）。
    """
    combo = QComboBox()
    combo.addItem("不通知", "none")
    combo.addItem("轻浮层", "overlay")
    lbl = ui_kit.choice_hint(combo, {
        "none": "成功时什么都不弹",
        "overlay": "成功时弹一个轻浮层",
    })
    assert lbl.text() == "成功时什么都不弹"
    combo.setCurrentIndex(1)
    assert lbl.text() == "成功时弹一个轻浮层", "改选之后释义没跟着换"
    assert "什么都不弹" not in lbl.text(), "没选中的那一档还留在界面上"


def test_choice_hint_refuses_a_value_without_its_hint(qtbot):
    """缺一个取值的释义**当场报错**：空释义比没有更糟（用户会以为这一档就是什么都不做）。"""
    combo = QComboBox()
    combo.addItem("没给释义", "nope")
    with pytest.raises(KeyError):
        ui_kit.choice_hint(combo, {})


# ---------- 七、内容区自带滚动区 ----------


def test_the_nav_band_and_the_footer_sit_outside_the_content_scroll(qtbot):
    """导航带与底栏在内容滚动区**外面**，分组在**里面** —— 矮窗下滚内容、头尾不动。

    用户 2026-10-16 报「编辑不能头尾固定」：此前滚动只由外层窗体提供（详情窗的
    ``_scrolled`` / 设置窗的滚动区），滚的是**整个编辑屏**，返回键跟着走 —— 在那套结构
    里「滚动」与「头尾固定」是互斥的。故把滚动收进内容区，头尾留在外面。

    判据落在**祖先链**上，不落在几何上：离屏环境下字体度量随平台变，而「谁是谁的后代」
    不会变（与 ``test_detail_form`` 里那几条动作归属判据同一种量法）。
    """
    shell = _shell(qtbot)
    group, _body = _group(shell)
    _lay_out(shell)

    scroll = shell.content_scroll()
    nav = shell.findChild(QWidget, "editor_nav")
    assert nav is not None, "没找到导航带（objectName 变了？）"
    assert not scroll.isAncestorOf(nav), (
        "导航带落在内容滚动区里 ⇒ 内容一滚，返回键跟着滚走（这正是「头尾固定不住」）"
    )
    assert not scroll.isAncestorOf(shell.footer_widget()), (
        "底栏落在内容滚动区里 ⇒ 内容一滚，动作行跟着滚走"
    )
    assert scroll.isAncestorOf(group), (
        "分组不在滚动区里 ⇒ 内容根本不会滚（滚动条只会长在外层窗体上）"
    )
