"""编辑屏与列表屏的切换栈。"""

from __future__ import annotations

from PySide6.QtCore import (
    QSize,
    Signal,
)
from PySide6.QtWidgets import (
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .forms import form_group
from .editor import EditorActionBar, EditorShell


class _TopPageStack(QStackedWidget):
    """**只把高度**按当前页报的 ``QStackedWidget``；宽度照旧取所有页的最大值。

    Qt 原生的 ``QStackedLayout`` 取**所有页的最大值**（含被 ``hide()`` 掉的那几页）——
    2026-10-16 实测：todo 详情页里列表页自然高 572，而编辑屏在台上时 ``sizeHint`` 也被
    顶成 572 ⇒ 详情窗那层滚动区多出一条 318px 的滚动条，编辑屏的返回键跟着滚出窗口。

    详情窗里一个 ``EditStack`` 的几页**高度**差很大（列表页随条目数长、编辑屏是个定高的
    表单），「按最大页占高度」正是「编辑屏头尾固定不住」的根因之一；改成按台上那页报高，
    列表页在台上时照旧按列表页占地方，两页互不牵连。

    ⚠️ **宽度不动**：连宽度一起按当前页报，顶层 widget 就会随台上那页变窄 —— 实测
    （2026-10-16）todo 列表页的行因此被 elide 成空串，`tests/test_todo_detail.py` 七条
    用例转红。宽度上「取最大值」本来就是对的：一个换屏容器不该因为此刻在台上的是哪一页
    而忽宽忽窄。

    ``minimumSizeHint`` 一起改高度，理由相同：``QScrollArea.widgetResizable`` 收缩内部件
    时受的是**最小**尺寸约束，只改 ``sizeHint`` 等于没改。
    """

    def sizeHint(self):  # noqa: N802
        base = super().sizeHint()
        current = self.currentWidget()
        if current is None:
            return base
        return QSize(base.width(), current.sizeHint().height())

    def minimumSizeHint(self):  # noqa: N802
        base = super().minimumSizeHint()
        current = self.currentWidget()
        if current is None:
            return base
        return QSize(base.width(), current.minimumSizeHint().height())

class EditScreen(QWidget):
    """一台「编辑弹窗」的**编辑屏** —— 框架给「点一行进来改这一条」这类场景共用的口。

    **它是什么**：:class:`EditorShell` 那块面（四区）＋ 三条动作线 ＋ 「先过关、再落盘」
    那一道。四处编辑屏此前各写各的这层壳（顶上 ``[‹ 返回] 标题``、页脚
    ``[删除] <stretch> [保存]``、末尾一条 ``FeedbackBar``）：三处逐字相同，口味却各差
    一点（标题走 ``section`` 还是 ``row_title``、反馈槽在按钮**上方**还是**下方**、返回
    键带不带 ``‹``）—— 「同一件事只许有一种排法」正是要把这点差异消掉。

    **消费方只做两件事**：往分组里填字段（:meth:`add_group` 是唯一该用的放法）、把保存
    的两端交给构造参数 —— ``on_validate``（返回错误文案 = 拦下）与 ``on_commit``（落盘）。
    返回键长什么样、删除键贴哪边、反馈条从哪儿长出来，不再由消费方决定 —— 位置在这里
    是**身份**，不是偏好。

    ⚠️ **保存的两端走注入，不走继承覆写**：本类是**组合件**（消费方
    ``self._screen = EditScreen(...)``；本仓四处编辑屏都另有一个宿主 ``QWidget``，列表屏
    与编辑屏同挂在它的换屏容器里），``self`` 永远是本类实例 —— 消费方在自己那边覆写
    出来的 ``commit`` **一次都调不到**，症状是**保存键亮着、点了什么都不发生、盘上没字**
    （2026-09-27 实测）。故本类**没有**可覆写的方法，只有两个构造参数。

    **它不管说哪句话、去哪一屏**：那两件事四处本就不一样（快捷键保存后**留在本屏**、
    另三处保存后回列表，消息落在哪个槽也不同），硬收进基类只会长出开关。消费方在
    ``on_commit`` 里自己说、自己走。

    **保存那一道门**：``on_validate`` 返回非 ``None`` 时**一个字都不落盘**、红字落本屏
    —— 这是四处唯一完全相同的保存逻辑（名称空了 / 重名 / 该填的没填），故由本类统一
    执行（先 ``on_validate``、后 ``on_commit``）。没有额外约束的屏不传它即可。

    明确**不做**（免得它长成「万能窗体基类」）：

    - 不做换屏 —— 那是 :class:`EditStack` 的事；
    - 不管滚动区与窗口 —— 消费方自己给外边距（本壳四边内边距为 0，那块面撑满它）；
    - 不管脏标记 —— 三处编辑屏今天保存键**恒亮**，只有快捷键那屏按脏标记灰亮。
      收进来等于给另三处加一道新行为（保存键从恒亮变灰着），那是**行为变更**，
      要连真机目测一起做，不搭这一趟车（见 ``内部记录（编辑弹窗基准）``）。
    """

    #: 导航带里那枚「返回」被点了（消费方据此回列表屏）。
    back = Signal()

    def __init__(
        self,
        *,
        title: str = "",
        back_label: str = "‹ 返回",
        save_label: str = "保存",
        on_validate=None,
        on_commit=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        #: 保存的两端（**注入**，不是可覆写方法 —— 见类 docstring 那条 ⚠️）。
        #: ``on_validate`` 返回错误文案 = 拦下，``on_commit`` 落盘。
        self._validate_cb = on_validate
        self._commit_cb = on_commit
        self._shell = EditorShell(title, parent=self, back_label=back_label)
        self._shell.back.connect(self.back.emit)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._shell)
        #: 这一屏那条反馈槽（零模态的唯一出口）。消费方自己说消息时用它。
        self.feedback = self._shell.feedback
        #: 右端那枚保存键。**读口**：要在别处按状态灰亮 / 改文案就取它
        #: （快捷键那屏就是这么做的 —— 脏标记归消费方，见类 docstring）。
        self.save_button = self._shell.add_action(save_label, self._on_save, kind="primary")

    # ---------- 内容 ----------

    def add_group(
        self, title: str = "", *, stretch: int = 0, divider_above: bool = False
    ) -> "tuple[QWidget, QVBoxLayout]":
        """往内容区放一个分组，返回 ``(分组, 往它里面放行的布局)``。

        内容区**只放分组**（``EditorShell`` 的那条判据：裸行落在卡外就没有归属），故这是
        唯一该用的放法 —— 直接往 ``content`` 里塞控件就是把裸行放回卡外。

        ``stretch`` 给**组内自带滚动区**的那一种用（如录屏通知的「进程匹配」）：
        不给的话那一组按 ``sizeHint`` 定高，里面的滚动区被压成一小条。
        ``divider_above`` 只是 ``form_group`` 那个参数的透传（2026-10-10 加）：变量编辑屏
        的三个来源组要它与前一组分隔，而此前本方法不收它 ⇒ 那三组被迫绕开本方法、
        用 ``form_group(title, 宿主, …)`` 手搓 —— 造出来的分组**只挂在宿主上、从没进过
        版面**，真机上叠在 (0,0) 把顶栏「返回」盖死、输入框悬空不显示（用户报「返回
        点击无效」「没看到触发条件设置」）。绕开唯一的放法就是**没有放法**。
        """
        group, body = form_group(title, self._shell, divider_above=divider_above)
        # 插在内容区末尾那段伸展**之前**（那段是壳钉的，见 ``EditorShell`` ② 段）：
        # 余高才会落在所有分组**下方**，而不是把已经加进来的那几组拉开。
        content = self._shell.content
        content.insertWidget(content.count() - 1, group, stretch)
        return group, body

    def add_action(
        self, text: str, on_click, *, kind: str = "primary", tier: str = "primary"
    ) -> QPushButton:
        """加一枚**别的**动作（保存与删除之外的第三件事）。

        它排在**保存之前**（保存恒在最右，见 ``EditorShell.add_action``）；次动作传
        ``tier="pill-ghost"``，免得两枚实心按钮抢注意力。
        """
        return self._shell.add_action(text, on_click, kind=kind, tier=tier)

    def add_destructive(self, text: str, on_click) -> QPushButton:
        """加一枚破坏性动作：贴**左缘**（离右端的主动作越远越好）。

        确认的写法仍归消费方：三处是行内问句（``FeedbackBar.ask``）、快捷键那处是按钮
        自己变「确认删除」。两种都零模态，本批不裁它（要统一得连那条钉着它的用例一起改）。
        """
        return self._shell.add_action(text, on_click, kind="destructive")

    # ---------- 两条可改的口 ----------

    def set_title(self, text: str) -> None:
        """换导航带里的标题（正在编辑的那一条换了对象 / 改了名字）。"""
        self._shell.set_title(text)

    def set_hint(self, text: str, *, role: str = "note") -> None:
        """改写页脚那句**状态位**（``EditorShell.set_hint`` 的转达）。"""
        self._shell.set_hint(text, role=role)

    def stretch_content(self) -> None:
        """让内容区吃掉多余高度（内容里自带滚动区 / 多行文本框的屏要它）。

        见 ``EditorShell.stretch_content``：默认那一份高度落在内容区下方的留白里，
        表单类编辑屏正该如此；带滚动区的屏不调它就会被压成一小条。
        """
        self._shell.stretch_content()

    def shell(self) -> EditorShell:
        """那一层版面壳的**判据读口**（量几何走它，别去摸私有属性）。"""
        return self._shell

    # ---------- 交给窗体壳托管 ----------

    def take_action_bar(self) -> EditorActionBar:
        """把这一屏的**动作行整条**交给窗体壳托管（详情页的「一屏只许一处动作行」）。

        ``EditScreen`` 不知道有没有人接管 —— 它只负责「交出去」；什么时候交、交给谁、
        拼成什么顺序，是窗体壳的事（见 ``shell/detail.py::DetailForm``）。屏自己用
        （快捷键页、录屏通知规则页那几处）时没人调它，动作行照旧在屏里。

        交出去之后 ``self.feedback`` 与 ``self.save_button`` **仍指向原来那些对象**：
        消费方说的话、按脏标记灰亮的那枚键都照旧能用 —— 变的只是它们的落点。

        ⚠️ 调过一次就好（第二次返回的是空的一行）：它把动作行**摘走**，不是复制。
        """
        return self._shell.release_action_bar()

    # ---------- 保存那一道 ----------

    def _on_save(self) -> None:
        """保存键的点击口：先过 ``on_validate``，过了才交 ``on_commit``。

        这一屏的判据只有一条：**不过关时一个字都不许落盘**（错误文案红字落本屏）。
        """
        problem = None if self._validate_cb is None else self._validate_cb()
        if problem is not None:
            self.feedback.error(problem)
            return
        if self._commit_cb is not None:
            self._commit_cb()


class EditStack(QWidget):
    """**列表屏 ⇄ 编辑屏** 的换屏容器 —— 「点一行进详情、返回回列表」这一族的框架口。

    这一点只做一件事：把「屏幕上此刻是哪一屏」收成一处。此前四处各写一遍
    （``QStackedWidget`` ＋ 一个 0 边距外层布局 ＋ ``setCurrentIndex(0/1)`` 的魔数下标）
    —— 下标写错一位，症状是「点了返回、屏没变」，而代码里看不出来。

    三句话用法：造的时候给列表屏；进编辑屏调 :meth:`push`；回列表调 :meth:`back`。
    「开一个造一个」的用法（快捷键页就是）收尾用 :meth:`drop` 摘掉并销毁。

    **不做**导航动画、不记历史栈（今天是两级、不嵌套）：真出现三级页时再谈，现在写就是空转。
    """

    #: 台上换了另一屏（进屏 / 回列表 / 摘屏都发）。
    #:
    #: 谁需要它：详情窗把各屏的动作行收进了自己页脚（一屏只许一处动作行，见
    #: ``shell/detail.py``），换屏时得把页脚那行切成台上那一屏的 —— 没有这个信号它
    #: 只能自己去连内层 ``QStackedWidget``（那是实现细节）。
    screen_changed = Signal()

    def __init__(self, list_page: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._list_page = list_page
        self._stack = _TopPageStack(self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._stack)
        self._stack.addWidget(list_page)

    def push(self, screen: QWidget) -> None:
        """显示 ``screen``（首次给到就登记它，之后只切过去）。"""
        if self._stack.indexOf(screen) < 0:
            self._stack.addWidget(screen)
        self._stack.setCurrentWidget(screen)
        # 台上换了页 ⇒ 尺寸也该跟着换（`_TopPageStack` 按当前页报），故重报一次几何。
        self._stack.updateGeometry()
        self.screen_changed.emit()

    def back(self) -> None:
        """回列表屏（二级屏**留着**：再进去时状态照旧，正是列表型编辑屏的期望）。"""
        self._stack.setCurrentWidget(self._list_page)
        self._stack.updateGeometry()
        self.screen_changed.emit()

    def drop(self, screen: QWidget) -> None:
        """摘掉并销毁 ``screen``，回列表屏（「开一个造一个」那条路的收尾）。

        ``setParent(None)`` 与 ``deleteLater()`` **两件都要**：只写后者的话，重建后
        那一帧新旧两套同时可见（2026-09-27 在 ``clear_layout`` 上踩过同一个坑）。
        """
        self._stack.removeWidget(screen)
        screen.setParent(None)
        screen.deleteLater()
        self._stack.setCurrentWidget(self._list_page)
        self._stack.updateGeometry()
        self.screen_changed.emit()

    def current(self) -> QWidget:
        """此刻在台上的那一屏（判据读口）。"""
        return self._stack.currentWidget()

    def list_page(self) -> QWidget:
        """列表屏（判据读口）。"""
        return self._list_page

