"""语义提示条（五档）。"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from barkit.contribution.specs import IconName
from barkit.shell import styles
from barkit.shell.design import metrics

from .base import glyph_pixmap
from .buttons import tool_button

#: 确认位的外观档 → ``tool_button`` 的 ``kind``（＝ QSS 里的 ``objectName``）。
#: 三档的语义与颜色见 :meth:`FeedbackBar.ask`；**没有第四档**，传别的名字当场报错。
_CONFIRM_KINDS: dict[str, str] = {
    "ghost": "pill-ghost",
    "primary": "primary",
    "danger": "danger",
}

#: 不传 ``confirm_kind`` 时的档。取最轻的那一档 ⇒ 与加这个参数**之前逐字同行为**，
#: 于是各测试桩零改动。
_DEFAULT_CONFIRM_KIND = "ghost"


class FeedbackBar(QWidget):
    """一页一条的**行内**反馈槽：平时显示一句提示，需要二次确认时原地长出按钮。

    它是「**零模态弹框**」纪律的落点。理由（实测驱动，见《重构-开工手册》§F 第 16/19 条
    与《录屏通知组件设计》§6.4.5）：设置窗被钉在原生层级 201
    （``macos_window.SETTINGS_LEVEL``）且带 ``WindowStaysOnTopHint``；从**置顶窗内部**
    弹一个模态框 —— 尤其是 ``QMessageBox.xxx(None, …)`` 这种**无父窗口**的 —— 等于在它之上
    再开一条**嵌套事件循环**。框一旦被置顶窗盖住就拿不到 key window，用户看到的是
    「点了按钮，窗口不动」，于是**再点一次** ⇒ 又叠一层循环 ⇒ 最终「窗口卡住、关不掉、
    只能强制结束程序」。有崩溃报告为证（``lastExceptionBacktrace`` 逐帧：
    ``QMessageBox.information`` → ``QDialog::exec()`` → CGEventTap 回调 → ``abort()``）。

    - ``info`` / ``warn`` / ``error``：显示**结果**（绿 / 橙 / 红）——
      「成了」「成了但还差一步」「没成」三态各有其色，别拿红的说小事；
      **没发生任何事**（点了按钮但前提不满足，如「先勾一项」）走 ``hint``，
      别拿 ``info`` 给一个没发生的动作发成功回执（2026-10-16 用户上报的那条）；
    - ``hint``：第四档「**中性告知**」—— 没出错、也还没做成，只是要你知道
      （「这条还没填完」）。它故意做得最轻：一页里最响的该是结果，不是提示。
    - ``ask``：显示问句 ＋ 一个确认按钮（文案由调用方给，如「删除」「清空」）；
      走品牌浅底，与四种「结果」档分得开 —— 它要的不是被读懂，是被回应。
      **确认位按「按下去会发生什么」取外观**（``confirm_kind``：不可逆走实心红、
      一般确认走实心紫、其余幽灵档，见 :meth:`ask`）；**取消位恒为幽灵档**；
      取消位默认**什么都不做**（即「我再想想」），也可以给它一个**真动作**
      （``cancel_label`` ＋ ``on_cancel``，如「放弃改动」）—— 两个动作都不是空操作时走这条；
    - ``dismiss``：撤下确认、清空回调 —— **不执行**任何事（即取消路径）。

    **五档都渲染成「提示条」**（语义浅底 ＋ **一道**左侧色条 ＋ 行首图标 ＋ 文字加粗），
    而不是一行裸小字：2026-09-30 之前它只设 ``color`` 与 ``font-size:12px``，于是与旁边的
    静态说明（``note()``）长得一模一样 —— 用户读不出来「这里有件事刚发生」，而这正是
    这个槽存在的全部理由（用户原话：「操作后的反馈太不显眼了」）。底色取 tokens 的
    ``*_soft`` 浅底档（它「只当底」），左侧色条与图标取图形档，文字取前景档。

    ``text()`` / ``tone()`` / ``is_pending()`` / ``accept()`` / ``cancel()`` 是**只读口**
    （测试与调用方走它），别去戳 ``_lbl`` / ``_on_accept`` / ``_on_cancel``。
    """

    #: 档名 →（浅底 / 色条与图标 / 文字 / 行首图标）。色全部来自 tokens 的四档表：
    #: 底色只取 ``*_soft``（那一档的语义就是「只当底」），色条与图标取**图形档**
    #: （那是「画」用的），文字取**前景档**（那一档才是「写字」用的，压 ``*_soft`` ≥4.5）。
    #: 一符两用（拿填充色去写字）是 2026-09-19 量出 33 格对比度不达标的唯一成因。
    _TONES: dict[str, tuple[str, str, str, str]] = {
        "info": ("green_soft", "green", "green_text", IconName.MARK_OK),
        "hint": ("surface3", "text3", "text1", IconName.MARK_IDLE),
        "warn": ("orange_soft", "orange", "orange_text", IconName.MARK_WARN),
        "error": ("red_soft", "red", "red_text", IconName.MARK_ERR),
        "ask": ("accent_soft", "accent", "accent_text", IconName.MARK_IDLE),
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        # 外观全在**内层盒**上（浅底 ＋ 左色条 ＋ 圆角）：外层只负责「露不露」，
        # 于是 ``hide()`` / ``show()`` 的语义一字不变。用 ``QFrame`` 而不是裸
        # ``QWidget`` —— 前者由 QStyle 画底，``border-radius`` 真裁（同 ``QLabel``
        # 那几枚药丸；裸 ``QWidget`` 在 macOS 上不裁，见 styles.py）。
        self._box = QFrame(self)
        outer.addWidget(self._box)
        lay = QHBoxLayout(self._box)
        lay.setContentsMargins(10, 8, 12, 8)
        lay.setSpacing(10)
        # 文字标签**先造**：``findChildren(QLabel)[0]`` 这类既有读法要仍拿到那句话。
        self._lbl = QLabel("", self._box)
        self._lbl.setWordWrap(True)
        self._icon = QLabel("", self._box)
        self._icon.setFixedSize(15, 15)
        # 与可换行 _lbl 并列，图标须顶对齐 —— 否则反馈文案换多行时，图标会按
        # QHBoxLayout 默认的 AlignVCenter 浮到文本块中间（机理 A，同
        # clock_view::_RowWidget 对 dot 的处理）。
        lay.addWidget(self._icon, alignment=Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self._lbl, stretch=1)
        # 确认位**三枚常驻**（幽灵 / 实心品牌紫 / 实心红），一次只显一枚。**不按需改
        # ``objectName``**：QSS 的匹配结果在控件上屏后被缓存，改完不重刷就还是旧外观
        # （``buttons.apply_button_tier`` 那段注释讲的是同一件事）。三枚都挂在布局上、
        # 藏起来的不占位 ⇒ 切档只是「显哪一枚」，与 Qt 的缓存行为无关。
        self._yes_btns: dict[str, QPushButton] = {}
        for kind, object_name in _CONFIRM_KINDS.items():
            btn = tool_button("确认", object_name, size="inline", parent=self._box)
            btn.clicked.connect(self._accept)
            btn.hide()
            lay.addWidget(btn)
            self._yes_btns[kind] = btn
        #: 当前那一枚确认位（:meth:`ask` 按 ``confirm_kind`` 换；只读口 :meth:`confirm_text` 走它）。
        self._yes = self._yes_btns[_DEFAULT_CONFIRM_KIND]
        self._no = tool_button("取消", "pill-ghost", size="inline", parent=self._box)
        lay.addWidget(self._no)
        self._on_accept: Callable[[], None] | None = None
        self._on_cancel: Callable[[], None] | None = None
        #: 当前档位（``None`` = 从没说过话）。换肤要按它重烤颜色，故留档。
        self._tone: str | None = None
        self._no.clicked.connect(self._cancel)
        self.hide()

    def info(self, text: str) -> None:
        """**结果**：这件事做成了（本槽三态的第一态）。

        「已保存 / 已安装 / 已删除」这类回执全归它，走**绿色**语义色 —— 从前这里用的是
        中性灰（``text2``），一行 12px 灰字比报错还不显眼（用户原话：「反馈太不显眼」）。
        没出错、也没做成的**告知**请用 :meth:`hint`，别拿这一档去说提示。
        """
        self._say(text, "info")

    def hint(self, text: str) -> None:
        """**中性告知**：没出错、也还没做成，只是要你知道（如「这条还没填完」）。

        与 :meth:`info` 的分工就是「有没有发生一件事」：发生了 ⇒ ``info``（绿）；
        没有 ⇒ ``hint``。它故意做得最轻（中性浅底 ＋ 最轻的图标）—— 一页里最响的
        该是**结果**，不是提示。
        """
        self._say(text, "hint")

    def warn(self, text: str) -> None:
        """「注意」档：装好了、但还需要你做点什么 —— 既不是失败，也不该被当成普通提示。

        2026-09-21 补这一格的理由：此前只有 ``info``（灰）与 ``error``（红）两态，于是
        「已完成，还差一步」这类话只能二选一 —— 选灰则不够醒目（用户以为没事了），
        选红则过度报警（下一句真失败时就没有升级余地了）。橙色的 ``orange_text``
        早就在语义色表里，只是行内通道没有出口。

        色走**前景档** ``orange_text``（压 surface 系列 ≥ 4.5:1）；``orange`` 是图形档
        （画圆点 / 图形用的），拿来写字只有 1.87，见 ``tokens.py`` 那张表。
        """
        self._say(text, "warn")

    def error(self, text: str) -> None:
        self._say(text, "error")

    def ask(self, text: str, confirm_label: str, on_accept: Callable[[], None],
            cancel_label: str | None = None,
            on_cancel: Callable[[], None] | None = None,
            confirm_kind: str = _DEFAULT_CONFIRM_KIND) -> None:
        """挂起一次待确认。

        ``cancel_label`` 省略即「取消」（点了只撤下问句）；给了就按它上屏，配合
        ``on_cancel`` 承载第二个真动作 —— 例如「保存并关闭 / 放弃改动」这种**两个
        都要干点什么**的询问，用「取消」当第二个动作的名字会让人以为点了没事发生。

        ``confirm_kind`` 给**确认位那一枚**上外观。判据是「按下去会发生什么」，不是
        「哪个好看」（门禁 ``test_destructive_confirm_is_styled_as_danger``）：

        - ``"danger"``：**不可逆 / 破坏性** —— 删除、卸载、清空、覆盖、恢复默认、移除
          ⇒ 实心红 ＋ 白字。此前这类确认位与它右边的「取消」是同一副灰模样
          （2026-10-16 用户上报「按钮没有颜色区分」）：全仓 12 处询问里 **9 处**按下去
          不可逆，而 ``#danger`` 那一族样式早就写好、从没被行内确认位用过；
        - ``"primary"``：**一般确认**，只有主动作、没有破坏（如「保存并关闭 / 放弃改动」
          这种两个都真动作的）⇒ 实心品牌紫；
        - ``"ghost"``（默认）：什么都不变 —— 轻量确认，或两个动作本来就没有主次。

        **取消位恒为幽灵档**：它是「我不做这件事」，不该跟要执行的那个动作抢注意力。
        """
        if confirm_kind not in _CONFIRM_KINDS:
            raise ValueError(
                f"未知的确认位外观：{confirm_kind!r}；合法值 {sorted(_CONFIRM_KINDS)}"
            )
        self._on_accept = on_accept
        self._on_cancel = on_cancel
        self._yes = self._yes_btns[confirm_kind]
        for kind, btn in self._yes_btns.items():
            btn.setVisible(kind == confirm_kind)
        self._yes.setText(confirm_label)
        self._no.setText(cancel_label or "取消")
        self._say(text, "ask")
        # 顺序：先 _say（它会把按钮收起来），再把按钮放出来 —— 否则从「提示」切到
        # 「确认」时会留下上一轮的按钮文案。
        self._yes.show()
        self._no.show()

    def dismiss(self) -> None:
        self._on_accept = None
        self._on_cancel = None
        self.hide()

    def clear(self) -> None:
        """把这一格恢复成**从没说过话**的样子：撤下待确认 ＋ **连文案一起清掉**。

        与 ``dismiss()`` 只差这一点：``dismiss`` 收按钮、留着那句字 —— 设置页的槽位
        跟着页面一起销毁，留着无妨；而**跨动作复用**的槽（面板那一行共享结果槽，
        ``StatusPanel._result_bar``）不行：旧文案留着，``text()`` 就会说谎，
        「这一格现在在讲什么」这个问题的答案随之不准（调用方靠它判断要不要重排高度）。
        """
        self._on_accept = None
        self._on_cancel = None
        self._lbl.setText("")
        self._tone = None
        self.hide()

    # ---------- 只读口（测试与调用方都用它） ----------

    def text(self) -> str:
        """当前那句提示 / 问句（空串 = 本槽没在显示任何东西）。"""
        return self._lbl.text()

    def tone(self) -> "str | None":
        """当前那句的**档位**（:data:`_TONES` 的键；``None`` = 从没说过话）。

        给「这句话是什么性质」这一判用 —— 光看 ``text()`` 读不出「它是不是在报成功」，
        而恰恰是这件事出过问题（2026-10-16：「先勾一项」拿 ``info`` 说，用户读成「成了」）。
        调用方要按语义选档，别按「哪档更好看」选；这条口是给测试钉住那个选择的。
        """
        return self._tone

    def is_pending(self) -> bool:
        """是否挂着一次**待确认**的动作（True ⇒ 屏幕上正等用户点确认/取消）。"""
        return self._on_accept is not None

    def accept(self) -> None:
        """等价于用户点了确认按钮（测试走这条，不必戳控件）。"""
        self._accept()

    def cancel(self) -> None:
        """等价于用户点了取消位（测试走这条）。未挂 ``on_cancel`` 时 == ``dismiss``。"""
        self._cancel()

    def cancel_text(self) -> str:
        """取消位当前上屏的字（默认「取消」）。"""
        return self._no.text()

    def confirm_text(self) -> str:
        """确认位当前上屏的字（默认「确认」，由 :meth:`ask` 的调用方给）。

        与 :meth:`cancel_text` 对称。这个词是**不可逆动作的名字**（「卸载」/「确认恢复」），
        写错比写丑严重 —— 它得能让用户一眼看出按下去会发生什么。
        """
        return self._yes.text()

    # ---------- 内部 ----------

    @staticmethod
    def _tokens() -> dict:
        """取当前**生效**主题的色板。

        用 ``current_theme()`` 而非 ``current_system_theme()``：前者认 ``apply_theme``
        的覆盖（应用里实际在用的那套），后者只读系统外观。
        """
        return styles.TOKENS[styles.current_theme()]

    def _say(self, text: str, tone: str) -> None:
        self._tone = tone
        self._lbl.setText(text)
        self._apply_tone()
        self._yes.hide()
        self._no.hide()
        self.show()

    def _apply_tone(self) -> None:
        """按当前档位把浅底 / 左色条 / 图标 / 字色**烤进**控件（构造与换肤都走这里）。

        内联样式不跟全局 QSS 走，故换肤要有人再喊一次（``refresh_theme``）。

        **加粗**走 ``font-weight``（不是 ``<b>``）：同一条文案既可能进这条槽、也可能
        进日志（``log.info``），带标记的那一份进日志就成了字面星号 —— 本仓有一条
        门禁专门抓这个（``test_no_markdown_in_ui_copy``）。
        """
        if self._tone is None:
            return
        bg_key, mark_key, text_key, icon = self._TONES[self._tone]
        tk = self._tokens()
        self._lbl.setStyleSheet(
            f"color:{tk[text_key]}; font-size:{metrics.FONT_SMALL}px; font-weight:500;"
        )
        self._icon.setPixmap(glyph_pixmap(self._icon, icon, 15, tk[mark_key]))
        # 左色条与浅底一起给内层盒。选择器**必须**是**类选择器** `.QFrame`，不能写裸
        # `QFrame`：类型选择器按 Qt 的规矩连**子类**一起命中，而 `QLabel` 恰好继承自
        # `QFrame` —— 于是图标与文字两个子标签各画上一道自己的左色条，反馈条长成
        # **三道条**（2026-09-30 真机：最左 ＋ 图标处 ＋ 文字左侧）。实测三种选择器的
        # 命中面：`QFrame` 命中 QFrame/QLabel/自定义子类；`.QFrame` 只命中 QFrame 实例；
        # `.自定义子类` 只命中该子类。那条门禁见 `test_feedback_bar_rule_cannot_cascade`。
        self._box.setStyleSheet(
            f".QFrame{{background:{tk[bg_key]};"
            f" border-left:3px solid {tk[mark_key]}; border-radius:{metrics.RADIUS_XS}px;}}"
        )

    def set_body_margins(self, left: int, top: int, right: int, bottom: int) -> None:
        """提示条那块浅底里的**留白**（默认 10/8/12/8）。

        面板那行共享结果槽铺满整条面板，要的是更宽的 14/8；给它一个口，而不是让调用方
        去戳 ``layout()`` —— 2026-09-30 起外层多套了一层浅底盒，戳 ``layout()`` 会静默
        打到**外面**那一层（留白变成外缩、字还挤在原处），不报错。
        """
        self._box.layout().setContentsMargins(left, top, right, bottom)

    def refresh_theme(self) -> None:
        """换肤：重烤本槽的颜色（内联样式不会跟着 ``apply_theme`` 走）。

        与 ``RecordRow`` 一族同款 —— 底色 / 色条 / 字色 / 图标都是**上屏那一刻**烤进去
        的，主题切换只换全局表，没人喊就停在旧主题的配色上（面板 ``_refresh_breach_theme``
        统一喊这一声）。
        """
        self._apply_tone()

    def _accept(self) -> None:
        # 先撤回调再执行：回调里可能再次弹确认（如「删完这条还有别的要删」），
        # 不清干净就会出现「点一次确认跑两遍」。
        cb, self._on_accept = self._on_accept, None
        self._on_cancel = None
        self.hide()
        if cb is not None:
            cb()

    def _cancel(self) -> None:
        """取消位：有 ``on_cancel`` 就跑它，没有就是「我再想想」（撤下问句而已）。"""
        cb = self._on_cancel
        self.dismiss()
        if cb is not None:
            cb()

