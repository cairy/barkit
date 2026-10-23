"""todo/detail_view：**条目管理详情页** —— 面板整行点进来的那扇窗（合并方案 §4.2）。

模块名落在 ``*_view.py`` 上不是随手起的：那条禁令（插件运行期路径不得依赖 PySide6）按
**模块名**认 UI 面（`settings.py` / `<name>_view.py`），叫 ``detail.py`` 就会被算进运行期
路径、当场判违规 —— 而这一份**本来就该**有 Qt。

设置页只管**配置**（浮层大小 / 关闭方式 / 行数上限），条目的增删改一律在这里。
同一件事两个落点必然漂（``hosts_guard`` 那枚「管理条目…」按钮就是这么删掉的：
它与面板整行会各开一份内容面，互相看不见对方刚做的事）。

形态与口径（方案 §4.2）：

- ``presentation="window"``：「左列表 ＋ 右表单」实测要 560–640px，浮层卡装不下；
- **独立数据管理（不是设置窗）**：每条待办是**独立的数据**，与「设置窗里攒改动、关窗
  前按「保存」一次写盘」是两码事。这里编辑屏按「确定」就**立刻写盘**、回列表；删一条
  也立即写盘。窗口页脚因此**只有「完成」**，没有「保存」、关窗也不用问「还有没保存的
  改动」——因为根本不存在「没保存的整表改动」。（设置窗那套攒改动的模型留给
  ``route_guard`` 这类真配置面。）
- 三枚次动作（方案 §4.2）：**预览这一条** / **现在问我** / **标记今天完成**。它们
  **立刻生效**（弹一次浮层、写一笔当日记账），本就是独立的一条写路，与「确定」写整条
  待办不是一回事 —— 2026-10-08 起它们**折进动作行末端那枚「⋯ 更多」**（用户裁定「次
  动作收进菜单可以」）：三枚幽灵按钮与「确定」并排时，主次分不出来；折起来之后动作行
  只剩「破坏性 / 溢出 / 主动作」三格，与 ``EditorShell`` 那条版面的理由同一门。菜单项
  的 ``hint`` 承载各自原本的悬停提示（``CandidateMenu`` 的两行长相本就是为这件事准备
  的）。

字段**不许平铺成一长列**（2026-10-08 用户裁定「必须要分组」）：一条条目的十三个键按
「是什么 / 什么时候提醒 / 显示与按钮」分三组（:data:`_EDIT_GROUPS`），**键空即那件事不
发生**是这个模型本来的样子，所以控件恒在；用不上的那几格**灰掉并就地说明**（灰掉 ≠
删掉 ≠ 收起来），判据与内核**同一份口径**（见 :meth:`_TodoManager._sync_enabled` 与
:meth:`_TodoManager._set_live`）。

新模型多两格（``command`` / ``command_at``，**不另开第四组**：那一组在旧模型下会
露出一张只有标题的空卡）—— 它们落在第三组末尾，因为那一组说的本来就是「它长什么样、
抬手能做哪几件」。**两格都恒可编辑**（2026-10-11 用户裁定「「到点执行」应该一直允许
输入」）：命令在哪一刻跑由「执行时机」那四枚芯片自己说，不再由别的格子替它开闸 ——
原先它跟着「到点弹卡」走，于是没选那张卡的人连打一串命令都打不进去。

窄取值控件（开关 / 数字格 / 下拉 / 单选按钮组）一律 ``control_fill=False``、贴行尾：本仓那条既有
口径（``ui_kit.form_row`` 的 ``control_fill`` docstring）。此前 13 格**全部**走的默认
值，于是三个数字被拉成半屏、步进箭头挤到最右边，而开关那两行的标签更是被 Qt 的余量
分配推到了行中间 —— 一屏看下来控件左缘右缘都在跳。

本模块的硬约束（与 ``route_guard`` 的设置面同款）：

- **不碰 ``Config``**（门禁 ``test_no_config_io_in_ui``）：整表读写在
  ``plugin.items()`` / ``plugin.save_items()``（旧模型）与 ``plugin.tasks()`` /
  ``plugin.save_tasks()``（新模型），当日记账读写在 ``plugin.done_at()`` /
  ``plugin.set_done()``。两套写口各自带闸：写了会被读的才放行（``save_items`` 拒
  ``tasks`` 非空、``save_tasks`` 拒 ``items`` 非空）。
- **零模态框**：反馈一律走行内 ``FeedbackBar``。
- **保存那一道走 ``EditScreen`` 的注入**（``on_validate`` / ``on_commit``），不靠覆写
  —— 那是组合件，覆写出来的方法一次都调不到（见 ``ui_kit.EditScreen`` 的 ⚠️）。
"""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QPoint, QRect
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from barkit.contribution import MenuItem
from barkit.core.hotkey_command import split_command
from barkit.core.interaction import PROMPT_CHOICE, PROMPT_TIME
from barkit.shell.design import metrics
from barkit.shell.ui_kit import (
    CandidateMenu,
    ChoiceBar,
    DecimalField,
    EditScreen,
    EditStack,
    ElideLabel,
    FeedbackBar,
    HoverRow,
    NumberField,
    Switch,
    apply_text_role,
    clear_layout,
    form_row,
    note,
    tool_button,
)
# 变量来源那四格的词**只有一份**（框架「变量」页那张表）：这里再写一遍的结果是同一件事
# 在两个页面上叫两个名字，而用户会以为自己看错了。
from barkit.shell.variables_page import SOURCE_LABELS

# 时刻那几格的**校验**在新模型里与内核读值同一份口径 —— 但那一份不在这里：编辑屏问的是
# ``plugin.time_ref_problem``（→ 变量池 → 表达式引擎，与 ``resolve_time`` 同一个引擎）。
# 这里只留旧模型那一支要的 ``_valid_hhmm``：旧内核（``scheduler._hhmm_to_sec``）只认字面
# ``HH:MM``、不接受变量，故那一支的格子也只能填字面 —— 界面比读侧宽的话，症状是
# 「存得进去、实际永远不触发」。
from . import task as task_mod
from .plugin import LATER_MINUTES, _ask_of
from .scheduler import (
    ACTION_DONE,
    ACTION_SNOOZE,
    ASK_WORDS,
    FACE_CLOCK,
    FACE_COUNTDOWN,
    FACE_COUNTUP,
    FACE_WORDS,
    _valid_hhmm,
    item_defaults,
)
from .task import STATUS_DONE, STATUS_OVERDUE, STATUS_PENDING

if TYPE_CHECKING:
    from barkit.plugins.todo.plugin import Plugin

#: 列表行上「这一条还没填内容」时显示的占位文案。与 ``plugin.FALLBACK_TITLE`` 不是
#: 同一件事：那个是**浮层**上问什么（得是一句完整的话），这个是**列表行**占位（一个字
#: 都不该填时告诉用户这一格空着）。
_EMPTY_TITLE = "（未填内容）"

#: 列表行左端那一格的**固定宽**（px）。定宽而不是让文字自己撑：那一格会写
#: ``09:30`` / ``待作答`` / ``今天已过`` 三种长短，自然宽下一列看下来每条标题的起点
#: 都在跳。取最长的「今天已过」（11.5px 四个全角字 ≈ 46）再留一点余量。
_TIME_W = 52

#: 「到点执行」那一格的占位文案。**只此一处**：输入格自己调一次 ``setPlaceholderText``，
#: ``_ref_row`` 还会拿它再覆盖一次 —— 两处各写一遍必然漂开。
#: 举的例子刻意是「发一个请求」：这格最常见的用法是打 webhook（见设计文档 §5.5），
#: ``--fail-with-body`` 也直接示范在里面（不加它 HTTP 4xx / 5xx 不算失败，见该节）。
_COMMAND_PLACEHOLDER = "留空 ＝ 不执行，如 curl -sS --fail-with-body https://…"

#: 「问法」三选，摊成**横向单选按钮组**（``ui_kit.ChoiceBar``）。**第一项就是缺省**
#: （``ChoiceBar`` 取值不在候选里时回落第一项，而 ``_ask_of`` 对「键不在」的老条目也
#: 按 ``choice`` 走 ⇒ 两项必须是同一个）。
#:
#: 文案直接取 ``scheduler.ASK_WORDS`` 那张**名词表**（列表行那句摘要用的是同一批短名，
#: 这里只是把同一批字摊开摆）—— 同一个概念两处各写一遍，改一处就会有一处对不上。
#:
#: **为什么是摊开而不是下拉**：``QComboBox`` 的弹出列表是一个独立顶层窗口，在这台机器上
#: 会被 macOS 归位到 iPad 虚拟屏（机制见 ``ui_kit.ChoiceBar`` 与
#: ``内部记录（下拉弹层）``）。三枚芯片既不弹窗、又一屏看得全，
#: 还比下拉省一次点击。
_ASK_CHOICES = [
    (ASK_WORDS[PROMPT_CHOICE], PROMPT_CHOICE),
    (ASK_WORDS[PROMPT_TIME], PROMPT_TIME),
    (ASK_WORDS[""], ""),
]

#: 每枚芯片的**悬停释义**。展开的话不写进标签（三句一次摊开会把控件淹掉，与
#: ``ui_kit.choice_hint`` 那条裁定同门），留在鼠标停得住的地方。
_ASK_HINTS = {
    PROMPT_CHOICE: "到点弹一张卡，上面一枚「完成」按钮——点它就记下今天做完了。",
    PROMPT_TIME: "程序启动时弹一张卡让你填一个时刻（如今天几点下班），填的值供这条算出后面的时间。",
    "": "不弹任何东西——这条只出现在浮层列表里。",
}

#: 「显示」**五选一**（2026-10-11 由原先的「浮层占一行」＋「钟面」两格并成）。
#:
#: 并成一格的理由是那两格从来不是两个自由度：钟面寄生在浮层行上 —— 内核只从「有窗口」
#: 的条目里选钟面，而窗口由「浮层占一行」决定 ⇒ 关掉浮层那一格时，钟面那一格写了也
#: 没人读。并成一格之后每一档都说全了这条在浮层上是什么样（不显示 / 只占一行 / 上钟面）。
#:
#: 第一项即缺省（``ChoiceBar`` 取值不在候选里时回落第一项，而 ``TaskDisplay.show`` 的
#: 缺省正是 ``none`` ⇒ 两项必须是同一个）。三档钟面的名词取自 ``scheduler.FACE_WORDS``
#: （与列表行摘要用的是同一批短名）；「不显示 / 浮层」两档没有那份名词，标签写在这里。
_SHOW_CHOICES = [
    ("不显示", task_mod.SHOW_NONE),
    ("浮层", task_mod.SHOW_ROW),
    (FACE_WORDS[FACE_CLOCK], FACE_CLOCK),
    (FACE_WORDS[FACE_COUNTDOWN], FACE_COUNTDOWN),
    (FACE_WORDS[FACE_COUNTUP], FACE_COUNTUP),
]

#: 同上，五枚芯片各自的悬停释义（「距结束 / 自登场」从前是标签里的括注，挪到这里）。
_SHOW_HINTS = {
    task_mod.SHOW_NONE: "不上浮层——这条只出现在主窗体清单里，你自己进去处理。",
    task_mod.SHOW_ROW: "在浮层上占一行，不占那块钟面。",
    FACE_CLOCK: "在浮层上占一行，那块钟面显示此刻的时间。",
    FACE_COUNTDOWN: "在浮层上占一行，那块钟面显示距这条结束还剩多久。",
    FACE_COUNTUP: "在浮层上占一行，那块钟面显示这条登场以来过了多久。",
}

#: **旧模型**那一格「钟面」的四选（取值空间与新模型的「显示」不是同一个：这里用 ``""``
#: 表示不占钟面，而新模型那一格用 ``SHOW_NONE``——旧条目表里没有「不显示」这一档，
#: 「上不上浮层」当年由 ``ask`` 与本格一起决定）。两套并存到旧条目表退场为止。
_FACE_CHOICES = [
    ("不占钟面", ""),
    (FACE_WORDS[FACE_CLOCK], FACE_CLOCK),
    (FACE_WORDS[FACE_COUNTDOWN], FACE_COUNTDOWN),
    (FACE_WORDS[FACE_COUNTUP], FACE_COUNTUP),
]

#: 同上，四枚芯片各自的悬停释义。
_FACE_HINTS = {
    "": "这条只出现在浮层列表里，不占那块钟面。",
    FACE_CLOCK: "钟面显示此刻的时间。",
    FACE_COUNTDOWN: "钟面显示距这条结束还剩多久。",
    FACE_COUNTUP: "钟面显示这条登场以来过了多久。",
}

#: 分钟格的上限：一天。循环 / 提前量超过一天在这个模型里没有意义（窗口按当天秒数算）。
_MAX_MINUTES = 24 * 60

#: 灰着的输入格**空着**时，占位字换成这一句（见 :meth:`_TodoManager._set_live`）。
#:
#: 为什么非要动占位字：全局 QSS 给 ``:disabled`` 的输入格换的只是**底色与描边**（深一档），
#: 而占位字原样留着 —— 一个灰格子里写着「如 09:30」，和旁边能填的格子长得一模一样。
#: 用户的视线正落在这一格上，读到的却是「往里填」。换掉它，这一格在灰着的时候就只剩
#: 一句「此刻不用填」，不用去悬停、也不用去读旁边那行交代。
_DEAD_PLACEHOLDER = "（此刻不用填）"

#: 编辑屏那 13 格的**三组**（2026-10-08，用户裁定「必须要分组」）。
#:
#: 平铺 13 格的问题不是「太多」，是**没有可停下来的地方**：读的人从「启用」一路扫到
#: 「只在某场景」，中途没有一处告诉他「上面那几行说的是同一件事」。三组的切法是照
#: 用户的三个问题来的 —— 这条待办**是什么** / 它**什么时候**出现 / 它**长什么样**。
#: 「怎么问」放在第二组**组首**：它决定那组里 `提醒时间` 与 `结束＝作答后` 谁在说话，
#: 是这一组的因。成员与次序见 :meth:`_TodoManager._rebuild_dyn`。
_EDIT_GROUPS = ("这条待办", "什么时候提醒", "显示与按钮")

#: 「到点执行」在哪一刻跑（2026-10-11，取代原先那枚「点完成也执行」开关）。四选一，
#: 第一项即缺省（取值与 ``TaskSpec.command_at`` 的缺省 ``reminder`` 是同一个）。
#:
#: **为什么不是原来的开关**：那枚开关只能表达「到点 ＋ 点完成也跑一次」这一种组合，
#: 而需求是「四个点里挑一个」——「提前登场那一刻跑」在过去根本写不出来（那枚开关管不着
#: 提前量），而它恰恰是最常用的一个（先把东西准备好，到点再提醒）。
_CMD_AT_CHOICES = [
    ("提醒时间", task_mod.COMMAND_AT_REMINDER),
    ("提前登场", task_mod.COMMAND_AT_LEAD),
    ("延后退出", task_mod.COMMAND_AT_TRAIL),
    ("点击完成", task_mod.COMMAND_AT_DONE),
]

#: 同上，四枚芯片各自的悬停释义。前两档与后两档各是一类：前三个是**时刻**（要有「提醒
#: 时间」才算得出来），最后一个是**你那一按**（不需要提醒时刻）。
_CMD_AT_HINTS = {
    task_mod.COMMAND_AT_REMINDER: "在「提醒时间」那一刻执行。",
    task_mod.COMMAND_AT_LEAD: "在「提前登场」那一刻执行（提醒时间减去提前量）。",
    task_mod.COMMAND_AT_TRAIL: "在「延后退出」那一刻执行（提醒时间加上延后量）。",
    task_mod.COMMAND_AT_DONE: "你在清单上点「完成」那一刻执行——这一档不需要「提醒时间」。",
}

#: 变量引用在这条任务的字段里**长什么样**（§4.3）：``{变量名}``。三处「可引用变量」的
#: 格子共用这一句交代 —— 各写一遍的结果是同一件事在三行里叫三个名字。
_VAR_NOTE = (
    "这一格可以<b>引用变量</b>：写 {变量名}（如 <b>{off_time}</b>）或点右边那枚「选变量 ▾」。"
    "变量的值在「设置 → 变量」里统一维护，改一次所有引用它的任务一起跟上。"
)

#: 旧表那一支的场景变量名（新模型里条件写的是 ``{var, op, value}``）。**只有一份**：
#: 编辑屏写进去的那一条与迁移搬过来的那一条必须是同一个变量，否则两条条件并存而只有一条
#: 生效（另一条永远匹配不上，还不报错）。
_SCENE_VAR = "scene"



def _normalize(item: dict) -> dict:
    """把一条条目补成**编辑屏的完整形状**：认识的键留着、缺的补语义缺省、
    **不认识的键原样保留**。

    存在的理由：编辑屏读的是十三格的完整形状，而老条目在盘上只有五六个键 —— 内存里
    先归一，``_rebuild_dyn`` 与 ``_read_form`` 才不会在某些键上落到意外缺省、也不会把
    「用户什么都没动」的那次「确定」写成一个行为不同的形状。

    补的必须是**语义缺省**（``scheduler.item_defaults()``：那条目什么都没配时会怎样），
    不是新条目的初值 —— 见那个函数为什么把 ``actions`` 给成空表。

    不认识的键留着：那是别人的数据（用户手改的、或另一个版本写的），按一次「确定」
    把它清掉就是数据丢失。
    """
    full = dict(item)
    for key, value in item_defaults().items():
        full.setdefault(key, value)
    return full


def _new_item() -> dict:
    """新条目的初值 —— **每个键都写出来**（方案 §2.1 的那张表就是这份清单）。

    初值 ＝ 老条目的语义缺省（:func:`scheduler.item_defaults`）＋ 一处**刻意不同**：新条目
    默认带一枚「完成」按钮（老条目是内核按缺键处理的「没有按钮」）。``ask`` 写显式的
    ``choice``：与「键不在的老条目」行为等价（批 1 的等价性判据钉着这条），但新条目
    从此不必再走那个隐式分支。
    """
    return dict(item_defaults(), actions=[ACTION_DONE])


#: 变量条那枚展开箭头（收起 / 展开两态）。
_CHEV_CLOSED = "▸"
_CHEV_OPEN = "▾"

#: 状态点三态 → （字形, 令牌名）。用**实心圆字符**而不是自绘椭圆：一个字符就能上主题色
#: （``apply_text_role`` 的 ``color`` 收令牌名），自绘则要往 ``styles.build_stylesheet``
#: 里加规则、还要自己处理换肤重放。灰那一档用 ``text3``（装饰档）—— 它本来就
#: 只说「这条还没有动静」，不该与文字抢注意力。
_DOT_WORDS: "dict[str, tuple[str, str]]" = {
    STATUS_DONE: ("●", "green"),
    STATUS_OVERDUE: ("●", "red"),
    STATUS_PENDING: ("●", "text3"),
}


def _status_dot(status: str, parent: QWidget) -> QLabel:
    """状态点（§4.5.③ 的三态）：灰＝未到 / 绿＝今日已完成 / 红＝已过期未处理。"""
    text, token = _DOT_WORDS.get(status, _DOT_WORDS[STATUS_PENDING])
    dot = QLabel(text, parent)
    dot.setFixedWidth(12)
    apply_text_role(dot, "caption", color=token)
    dot.setToolTip({
        STATUS_DONE: "今天已完成",
        STATUS_OVERDUE: "过了提醒时刻还没处理",
    }.get(status, "还没到点"))
    return dot


class _VariableStrip(QWidget):
    """主窗体顶部那条「变量」（§4.5.②）：默认折叠成一行，点开看每一条的值与来源。

    **它不自绘任何变量编辑控件**：那是框架「变量」页的事。展开后那一列只回答两件事 ——
    这个变量现在是多少、它是哪来的。把编辑控件复制一份到这里，等于让同一个值长出两个
    写口，而两份互不感知（``hosts_guard`` 那枚「管理条目…」按钮就是这么被删掉的）。
    """

    def __init__(self, plugin: "Plugin", parent: "QWidget | None" = None) -> None:
        super().__init__(parent)
        self._plugin = plugin
        self._open = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._row = HoverRow(self._toggle, parent=self)
        h = QHBoxLayout(self._row)
        h.setContentsMargins(10, 4, 10, 4)
        h.setSpacing(6)
        self._chev = QLabel(_CHEV_CLOSED, self._row)
        apply_text_role(self._chev, "chevron")
        h.addWidget(self._chev)
        self._summary = ElideLabel("", self._row)
        apply_text_role(self._summary, "caption")
        h.addWidget(self._summary, stretch=1)
        lay.addWidget(self._row)

        self._body = QWidget(self)
        self._body_lay = QVBoxLayout(self._body)
        self._body_lay.setContentsMargins(20, 0, 10, 6)
        self._body_lay.setSpacing(3)
        self._body.setVisible(False)
        lay.addWidget(self._body)
        self.refresh()

    def refresh(self) -> None:
        """重读变量（``plugin.variables_used()``）并重画两态。

        **一条变量都没引用时整条隐身**：一个空壳「变量：」挂在窗口顶上只会让人问
        「这里怎么是空的」，而答案是「这一页的任务本来就不依赖变量」。
        """
        rows = self._plugin.variables_used()
        self.setVisible(bool(rows))
        if not rows:
            return
        if self._open:
            self._summary.setText("变量（点击收起）")
        else:
            brief = " · ".join(f"{r['name']} {r['value']}" for r in rows)
            self._summary.setText(f"变量：{brief}")
        clear_layout(self._body_lay)
        for row in rows:
            self._body_lay.addWidget(self._build_item(row))
        # 「改」按钮**这一批不做**：它要能打开框架「变量」页并定位到那一条，而框架
        # 目前没有「由组件打开设置窗」的出口（``ctx`` 上只有打开**自己**详情窗的那条）。
        # 做不成的按钮不做（§4.5 的原话），改成一句指路。
        self._body_lay.addWidget(note("变量由框架统一维护：改在「设置 → 变量」。", self._body))

    def _build_item(self, row: dict) -> QWidget:
        cell = QWidget(self._body)
        h = QHBoxLayout(cell)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        name = QLabel(str(row.get("name") or row.get("id") or ""), cell)
        h.addWidget(name)
        value = QLabel(str(row.get("value") or ""), cell)
        apply_text_role(value, "caption")
        h.addWidget(value)
        h.addStretch(1)
        tag = QLabel(SOURCE_LABELS.get(str(row.get("source") or ""), ""), cell)
        apply_text_role(tag, "caption")
        h.addWidget(tag)
        return cell

    def _toggle(self) -> None:
        self._open = not self._open
        self._body.setVisible(self._open)
        self._chev.setText(_CHEV_OPEN if self._open else _CHEV_CLOSED)
        self.refresh()


class _TodoManager(QWidget):
    """详情页主体：列表页 ＋ 单条编辑屏（两屏同挂在 ``EditStack`` 里）。"""

    def __init__(self, plugin: "Plugin", parent: "QWidget | None" = None) -> None:
        super().__init__(parent)
        self._plugin = plugin
        #: 内存里那张表 —— 用户正在编辑的那一份，也是**立刻写盘的那一份**（「确定」／
        #: 删除都直接 ``save_items`` 它）。每条进来先补成编辑屏的完整形状（见
        #: :func:`_normalize`）。
        self._items: "list[dict]" = [_normalize(i) for i in plugin.items()]
        #: 新模型那张表（**原始声明**，不是内核形状）：编辑屏增删改都在它上面做，写完
        #: ``plugin.save_tasks()``。与 :attr:`_items` 各存一份而不是共用一个字段 —— 两套
        #: 字段形状不同（``at``/``work_hours`` vs ``effective``/``display``），共用会让
        #: 「此刻这份 draft 是哪一套」变成一个要读代码才能回答的问题。
        self._tasks: "list[dict]" = [dict(t) for t in plugin.tasks()]
        self._editing: "int | None" = None
        self._adding = False
        self._draft: "dict | None" = None
        #: 编辑屏此刻那几个控件（字段名 → 控件），每次进出编辑屏整体重建。
        self._w: "dict[str, object]" = {}
        #: 同上，字段名 → 那一行左端那枚标签。``_set_live`` 要把它一起淡下去（只灰控件
        #: 的话那一行看上去仍是活的）。
        self._labels: "dict[str, object]" = {}
        #: 同上，字段名 → 控件**出厂时那份占位字**。``_set_live`` 会把灰着的格子里的
        #: 占位字换成一句交代（见 :data:`_DEAD_PLACEHOLDER`），亮回来时要照这份还原。
        #: 没有占位字这东西的控件（数字格）在这里是空串，换口那一步按 ``hasattr`` 跳过。
        self._places: "dict[str, str]" = {}
        #: 「⋯ 更多」那枚弹层。**非阻塞 popup ⇒ 必须持引用替它保命**（出作用域就可能被
        #: 回收），与面板底部栏那枚 ``_quick_menu`` 同一条理由。
        self._more_menu: "CandidateMenu | None" = None
        #: 最近一次 ``_refresh_list`` 算出来的那些行（界面形状，见 ``plugin.task_rows``）。
        #: 行内动作与「进编辑屏」都按 **id** 回头找它，不按列表下标 —— 下标会随
        #: 「今天 / 全部」那一档变，今天第 2 条在「全部」里可能是第 5 条。
        self._rows: "list[dict]" = []
        #: 「今天 / 全部」那一档（§4.5 原型那条工具条）；「今天」＝今天在册的任务
        #: （停用 / 星期 / 场景不命中的不列）—— **不含「生效时段」**，见
        #: :meth:`_build_list_page` 里那一行的注释。
        self._scope_value = "today"
        #: 顶部那条变量条与页眉那格（都在 ``_build_list_page`` 里建）。
        self._vstrip: "_VariableStrip | None" = None
        self._head: "QLabel | None" = None
        #: 编辑屏「生效时段」两格下面那一行状态（新模型表单里建；旧表单没有这两格
        #: ⇒ ``None``）。每次进出编辑屏整体重建，故它跟着 ``_w`` 一起换。
        self._win_state: "QLabel | None" = None

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
        # 独立数据管理：每条改完立刻写盘，没有「攒着等窗口保存」这回事，也就不需要把
        # 自己交给插件去接框架壳那枚「保存」回调（那是设置窗的模型）。

    # ---------- 列表页 ----------

    def _new_model(self) -> bool:
        """此刻走的是「任务表」那一套吗。判据**只从 ``plugin`` 拿**，界面这边不许再判一遍
        —— 两份判据必然漂（一边按 ``tasks`` 非空、一边按别的），症状是「在新表单上编、
        插件按旧模型读」，而两边都不报错。
        """
        return self._plugin.uses_tasks()

    def _build_list_page(self) -> QWidget:
        page = QWidget(self)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        # 列表页**不套分区卡**：本页是 window 形态，原生标题栏已给出页面身份（见
        # ``shell/detail.py``：window 形态内容直接进窗、不在窗内重复标题）。再套一张
        # ``settings_section`` 卡只是多一层带标题的框 —— 与原生标题栏上的「待办提醒」
        # 重复，还把列表挤进更小的可视区。这里只留一行轻量页眉。
        #
        # 页眉那格报**今天**：日期 ＋「未完成 N · 已完成 X」（§4.5.①）。两个数与面板那行
        # 摘要同源（``plugin.header_counts`` ⇒ ``_count_rows``），故不会出现「面板说 3 项、
        # 窗口说 2 项」那种用户没法判断信谁的不一致。
        head = QHBoxLayout()
        head.setSpacing(6)
        self._head = QLabel("", page)
        apply_text_role(self._head, "section")
        head.addWidget(self._head)
        head.addStretch(1)
        add_btn = tool_button("＋ 新增", "pill-ghost", size="inline", parent=page)
        add_btn.clicked.connect(self._on_add)
        head.addWidget(add_btn)
        lay.addLayout(head)

        # 变量条（§4.5.②）：**默认折叠**成一行摘要 —— 用户不是每次都改共享变量，常驻
        # 展开是噪音。
        self._vstrip = _VariableStrip(self._plugin, page)
        lay.addWidget(self._vstrip)

        # 「今天 / 全部」：今天不在册的（停用 / 星期不命中 / 场景没命中）默认那一眼
        # 不该看见它，但也不能因此就找不着它。**「生效时段」不在这一筛里** —— 它问的
        # 是「此刻算不算数」，不是「今天在不在册」：晚上打开这扇窗时，白天时段的任务
        # 可能正挂着未完成，筛掉它用户就找不着自己还没做的事了（它此刻算不算数，由
        # 清单行摘要与编辑屏那行状态说）。选项少 ⇒ 用 ``ChoiceBar`` 摊在行里 ——
        # 下拉会弹一个独立顶层窗，而本机系统会把新顶层窗重新归位到那块 iPad 虚拟屏上。
        scope_row = QHBoxLayout()
        scope_row.setSpacing(8)
        self._scope = ChoiceBar(
            [("今天", "today"), ("全部", "all")],
            "today",
            page,
            hints={
                "today": "今天生效的任务（停用 / 星期 / 场景不命中的不列）",
                "all": "连今天不生效的也列出来",
            },
        )
        self._scope.selected.connect(self._on_scope_changed)
        scope_row.addWidget(self._scope)
        scope_row.addStretch(1)
        lay.addLayout(scope_row)

        self._rows_box = QWidget(page)
        self._rows_lay = QVBoxLayout(self._rows_box)
        self._rows_lay.setContentsMargins(0, 0, 0, 0)
        self._rows_lay.setSpacing(4)
        # **不给条目清单另套滚动区**：宿主（窗口形态的详情页）已经有一层
        # （`shell/detail.py::_scrolled`）—— 这里再包一层，内容顶出视口时就是两条竖
        # 滚动条并存，滚轮只驱动鼠标底下那一条。行随内容自然长高，滚动交给外面那一层。
        lay.addWidget(self._rows_box, stretch=1)
        self._list_fb = FeedbackBar(page)
        lay.addWidget(self._list_fb)
        lay.addWidget(note(
            "行内的「完成」立刻记上今天的账；点整行进编辑屏改，按「确定」当场存好。",
            page,
        ))
        return page

    def _build_row(self, row: dict) -> QWidget:
        """列表里一条任务那一行（§4.5.③）：**状态点 ＋ 标题 ＋ 一句摘要 ＋ 行内动作**。

        三枚动作的分工（§1.1.⑤）：「完成」标今日完成、**立刻写当日账**，不进编辑屏；
        「稍后」把这一条从浮层上收起 ``LATER_MINUTES`` 分钟；「修改」推到**同窗**里那块
        编辑屏 —— 不是新窗口。整行点击与「修改」是同一件事（老习惯留着的入口）。
        """
        iid = str(row.get("id") or "")
        wrap = HoverRow(on_click=lambda _i=iid: self._open_edit(_i), parent=self._rows_box)
        h = QHBoxLayout(wrap)
        h.setContentsMargins(10, 6, 10, 6)
        h.setSpacing(8)

        if row.get("tracked", True):
            # 「不需要完成」的那几行**不画状态点**：灰 / 绿 / 红这一族整族以「完成」为轴，
            # 显示类（整点时钟这类）一个都不适用 —— 留个灰点会被读成「还没到点」。
            # 代价是那几行行首少一格，故这一行必须有 ``tracked`` 明说（缺省按要完成画）。
            h.addWidget(_status_dot(str(row.get("status") or ""), wrap))

        main = QVBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(1)
        title = ElideLabel(str(row.get("title") or "").strip() or _EMPTY_TITLE, wrap)
        if row.get("status") == STATUS_DONE:
            # 完成了的行**划掉标题**（§4.5.③ 的原型）：长列表里那 9px 的圆点不够看。
            font = title.font()
            font.setStrikeOut(True)
            title.setFont(font)
        main.addWidget(title)
        # 摘要与标题一样是可压的单行（``ElideLabel`` 空文本自隐）：窄了打省略号让位，
        # 不该为了这一句把整行顶宽。
        summary = ElideLabel(str(row.get("summary") or ""), wrap)
        apply_text_role(summary, "caption")
        main.addWidget(summary)
        h.addLayout(main, stretch=1)

        h.addLayout(self._build_actions(row, wrap))
        return wrap

    def _build_actions(self, row: dict, parent: QWidget) -> QHBoxLayout:
        """行内那几枚动作。已完成的**只留「撤销 / 修改」** —— 「稍后」对一条已经做完的事
        没有意义（它换的是浮层上那一行的去留，而那一行早就收了）。

        「不需要完成」的那几行**不画「完成」**：给它一枚点了只是记一笔、却哪儿都不显示的
        按钮是骗人（那一格本就不该有）。
        """
        acts = QHBoxLayout()
        acts.setContentsMargins(0, 0, 0, 0)
        acts.setSpacing(6)
        tracked = bool(row.get("tracked", True))
        # 「不需要完成」的既不可能已完成（旧账里若还留着一条 done 记录也不认它）——
        # 于是它那行的「稍后」照旧画、「完成」那枚下面按 ``tracked`` 判。
        done = tracked and row.get("status") == STATUS_DONE
        iid = str(row.get("id") or "")

        if tracked:
            toggle = tool_button(
                "撤销" if done else "完成",
                "pill-ghost" if done else "primary",
                size="inline",
                parent=parent,
            )
            toggle.clicked.connect(lambda _=False, _i=iid: self._toggle_done(_i))
            acts.addWidget(toggle)

        if not done:
            later = tool_button("稍后", "pill-ghost", size="inline", parent=parent)
            later.setToolTip(f"从浮层上收起 {LATER_MINUTES} 分钟，到点自己再出现")
            later.clicked.connect(lambda _=False, _i=iid: self._snooze(_i))
            acts.addWidget(later)

        edit = tool_button("修改", "pill-ghost", size="inline", parent=parent)
        edit.clicked.connect(lambda _=False, _i=iid: self._open_edit(_i))
        acts.addWidget(edit)
        return acts

    def _toggle_done(self, task_id: str) -> None:
        """行内「完成 / 撤销」—— 用户自己动手那条路（「拉」的那条），**不查去重账**：
        账挡的是弹窗，不挡用户自己动手。"""
        if not task_id:
            return
        done = self._plugin.done_at(task_id)
        if not self._plugin.set_done(task_id, "" if done else None):
            self._list_fb.error("写完成记录没成功——请重试。")
            return
        self._refresh_list()

    def _snooze(self, task_id: str) -> None:
        """行内「稍后」：把这一条从浮层上收起 ``LATER_MINUTES`` 分钟。"""
        if not task_id:
            return
        self._plugin.snooze(task_id, LATER_MINUTES)
        self._refresh_list()

    def _on_scope_changed(self, value: str) -> None:
        self._scope_value = value
        self._refresh_list()

    def _row_of(self, task_id: str) -> "dict | None":
        return next((r for r in self._rows if r["id"] == task_id), None)

    def _refresh_list(self) -> None:
        self._rows = self._plugin.task_rows()
        shown = [r for r in self._rows if r["active"] or self._scope_value == "all"]
        # 页眉那两个数与面板那行摘要是**同一个源**（都走 ``_count_rows``）：分开算的话，
        # 用户会看到页眉说「未完成 3」而下面只列着 2 条 —— 而两边都看着对。
        pending, done = self._plugin.header_counts()
        if self._head is not None:
            self._head.setText(f"{datetime.now():%Y-%m-%d} · 未完成 {pending} · 已完成 {done}")
        if self._vstrip is not None:
            self._vstrip.refresh()
        clear_layout(self._rows_lay)
        if shown:
            for row in shown:
                self._rows_lay.addWidget(self._build_row(row))
        elif self._rows:
            self._rows_lay.addWidget(
                note("此刻没有到生效期的任务——切「全部」看看。", self._rows_box)
            )
        else:
            self._rows_lay.addWidget(
                note("还没有任务——点「＋ 新增」加一条。", self._rows_box)
            )
        self._rows_lay.addStretch(1)

    # ---------- 编辑屏 ----------

    def _build_edit_page(self) -> EditScreen:
        edit = EditScreen(
            on_validate=self._validate_form,
            on_commit=self._commit_form,
            # 这一枚键就是「写盘」本身：收进列表后**立刻存进配置**、回列表（独立数据
            # 管理，每条各自落盘，没有「攒着等窗口保存」）。叫「确定」是编辑屏主动作的
            # 常规划法。
            save_label="确定",
            parent=self,
        )
        edit.back.connect(self._on_back)
        # 本页的错误提示落这一条槽（零模态框）
        self._edit_fb = edit.feedback

        # 三组的**骨架只造一次**（组在内容区里的位置归 EditorShell），组里的行每次进出
        # 编辑屏整体重画 —— 见 :meth:`_rebuild_dyn`。
        self._box_lays: "list[QVBoxLayout]" = []
        self._boxes: "list[QWidget]" = []
        for title in _EDIT_GROUPS:
            _group, body = edit.add_group(title)
            box = QWidget(self)
            box_lay = QVBoxLayout(box)
            box_lay.setContentsMargins(0, 0, 0, 0)
            box_lay.setSpacing(metrics.GAP_SM)
            body.addWidget(box)
            self._boxes.append(box)
            self._box_lays.append(box_lay)

        edit.add_destructive("删除这条", self._on_delete)
        # 次动作折进末端那枚「⋯ 更多」（见模块顶）：动作行只剩「破坏性 / 溢出 / 主动作」
        # 三格，与 ``EditorShell`` 那条「左端破坏性、右端主动作」的版面理由同一门。
        self._more_btn = edit.add_action("⋯ 更多", self._on_more, tier="pill-ghost")
        self._more_btn.setToolTip("这一条专属的几件事：预览 / 立刻问一次 / 标记今天完成")
        # 页脚那句交代**一次就够**：这一屏的两级写盘关系不随条目变（见模块顶）。它落在
        # 「⋯ 更多 / 确定」左边那一格 —— 正是用户按下去之前会看一眼的地方，说清「确定」
        # 与窗口底部那枚「保存」各管什么（两枚键都在右下角，只说「命名分得开」不够）。
        edit.set_hint("「确定」存好这条就回列表——改动当场写进配置。")
        return edit

    def _rebuild_dyn(self) -> None:
        """按当前 draft 重建字段（每次进出编辑屏都整体重画）。

        成员与次序就是 :data:`_EDIT_GROUPS` 那三组 —— 第一组说这条待办**是什么**、
        第二组说它**什么时候提醒**、第三组说它**长什么样**。次序不是随意的：
        「怎么问」放在第二组组首，因为它决定那一组里「提醒时间」与「结束＝作答后」
        谁在说话。

        **按模型分派**（Phase 5 起）：任务表那一套走 :meth:`_rebuild_task_dyn` —— 两套
        表单不会同时在场（走哪套是整条盘的性质），故共用这一个入口与那一屏控件。
        """
        if self._new_model():
            self._rebuild_task_dyn()
            return
        draft = self._draft
        assert draft is not None
        for box_lay in self._box_lays:
            clear_layout(box_lay)
        self._w = {}
        self._labels = {}
        self._places = {}
        # 旧表单没有「生效时段」那两格 ⇒ 那一行状态跟着下班（持一个已 setParent(None)
        # 的标签没有意义，``_sync_window_status`` 也按 ``None`` 早退）。
        self._win_state = None

        # ── 组 1：这条待办是什么 ──
        self._switch(0, "enabled", "启用", bool(draft.get("enabled", True)))

        title = QLineEdit(self._boxes[0])
        title.setPlaceholderText("如 喝水 / 站起来走走")
        title.setText(str(draft.get("title") or ""))
        self._row(0, "title", "内容", title, fill=True)

        text = QLineEdit(self._boxes[0])
        text.setPlaceholderText("浮层那行显示的句子，可留空")
        text.setText(str(draft.get("text") or ""))
        self._row(0, "text", "显示文本", text, fill=True)
        self._box_lays[0].addWidget(note(
            "显示文本里可写 {到点}（这条算出来的时刻）、{现在}、{剩余}（倒计时还剩多少）。"
            "认不出的占位符会原样显示，方便你看出来写错了。",
            self._boxes[0],
        ))

        # ── 组 2：什么时候提醒 ──
        # 组内次序＝**先说这条按哪种算法算窗口，再依次说窗口的起点、提前量、终点**。
        # 「怎么问」在组首，它是这一组的因（决定组里「提醒时间 / 结束＝作答后」谁在说话）；
        # 接着两个**起点**（字面「提醒时间」、按整点循环的「循环间隔」）＋起点前的提前量；
        # 最后两个**终点**（字面「结束时间」、算出来的「结束＝作答后」）—— 两个「结束」挨着
        # 放，因为它们回答的是同一个问题，而那条会跟着问法变的交代（``_timing_note``）
        # 紧跟在它们下面。
        #
        # 初始值走 ``_ask_of`` 而不是 ``str(draft.get("ask") or "")``：老条目**没有**
        # 这个键，而空串正好命中「不弹提醒」那一枚 ⇒ 打开一条老条目会看见「不弹提醒」，
        # 一按「确定」就把「到点弹『完成』」静默改成「不弹」（用户什么都没改）。
        ask = ChoiceBar(_ASK_CHOICES, _ask_of(draft), self._boxes[1], hints=_ASK_HINTS)
        ask.selected.connect(self._on_ask_changed)
        self._row(1, "ask", "怎么问", ask)

        at = QLineEdit(self._boxes[1])
        at.setPlaceholderText("如 09:30")
        at.setText(str(draft.get("at") or ""))
        self._row(1, "at", "提醒时间", at, fill=True)

        iv = NumberField(self._boxes[1])
        iv.setRange(0, _MAX_MINUTES)
        iv.setSuffix(" 分钟")
        iv.setSpecialValueText("不循环")
        iv.setValue(int(draft.get("interval_min") or 0))
        # 循环间隔一改，组 2 那两格谁在说话就跟着变（``_sync_enabled`` 里判它）。
        iv.valueChanged.connect(lambda _v: self._sync_enabled())
        self._row(1, "interval_min", "循环间隔", iv)

        bf = NumberField(self._boxes[1])
        bf.setRange(0, _MAX_MINUTES)
        bf.setSuffix(" 分钟")
        bf.setSpecialValueText("不提前")
        bf.setValue(int(draft.get("before_min") or 0))
        self._row(1, "before_min", "提前登场", bf)

        end = QLineEdit(self._boxes[1])
        end.setPlaceholderText("留空 ＝ 挂到当天结束")
        end.setText(str(draft.get("end") or ""))
        self._row(1, "end", "结束时间", end, fill=True)

        wh = DecimalField(self._boxes[1])
        wh.setRange(0.0, 24.0)
        wh.setSuffix(" 小时")
        wh.setSpecialValueText("不用")
        wh.setValue(float(draft.get("work_hours") or 0))
        self._row(1, "work_hours", "结束＝作答后", wh)

        # 灰掉的那两格旁边那句交代（会跟着问法变，见 :meth:`_timing_hint`）。它紧跟在
        # 「提醒时间 / 结束＝作答后」这一对下面 —— 读到灰格子时，答案就在下一行。
        self._timing_note = note("", self._boxes[1])
        self._box_lays[1].addWidget(self._timing_note)

        scene = QLineEdit(self._boxes[1])
        scene.setPlaceholderText("留空 ＝ 不限场景")
        scene.setText(str(draft.get("scene") or ""))
        self._row(1, "scene", "只在某场景", scene, fill=True)
        self._box_lays[1].addWidget(note(
            "「只在某场景」既管<b>问</b>、也管这条在不在浮层上出现；留空就是不限。"
            "问法自己决定去重档（问时刻的不屈不挠、问完成的一天一次），不给你选。",
            self._boxes[1],
        ))

        # ── 组 3：长什么样 ──
        face = ChoiceBar(
            _FACE_CHOICES, str(draft.get("face") or ""), self._boxes[2], hints=_FACE_HINTS
        )
        self._row(2, "face", "钟面", face)

        self._switch(2, "act_done", "行上「完成」", ACTION_DONE in _actions_of(draft))

        self._switch(2, "act_snooze", "行上「稍后」", ACTION_SNOOZE in _actions_of(draft))
        self._box_lays[2].addWidget(note(
            "两枚行上按钮<b>按条目配</b>：不配就不画那一枚（画一个点了没用的按钮是骗人）。"
            f"「稍后」把这一条在浮层上收起 {LATER_MINUTES} 分钟，到点自己再弹一次。",
            self._boxes[2],
        ))

        self._sync_enabled()
        self._sync_actions()

    def _row(
        self, group: int, key: str, label: str, control: QWidget, *, fill: bool = False
    ) -> None:
        """往第 ``group`` 组里放一行「标签 ＋ 控件」，并把它登记进 :attr:`_w`。

        ``fill`` 默认 **False**（窄取值控件贴行尾）。本屏十三格里有八格是开关 / 数字格
        / 单选按钮组：它们拉满一行就是「三个数字撑满半屏」，而 ``form_row`` 那条既有
        口径说的正是这件事（见 ``ui_kit.form_row`` 的 ``control_fill`` docstring）。
        只有真正的全宽输入类（``QLineEdit``）传 ``fill=True``。

        标签一并登记：:meth:`_set_live` 要把灰格子的标签也淡下去 —— 只灰控件的话那一行
        看上去仍是活的。``form_row`` 两种排法都把标签放在**第 0 项**，故读它不用分支。
        """
        self._w[key] = control
        row = form_row(label, control, self._boxes[group], control_fill=fill)
        self._labels[key] = row.itemAt(0).widget()
        # 原始占位字留底：:meth:`_set_live` 会把灰着的空格子里的占位字换掉（那是它在
        # 灰着时唯一说得上话的地方），还亮回来时要能还原。不是每种控件都有它 ——
        # 数字格那种靠 ``specialValueText`` 说事。
        self._places[key] = (
            control.placeholderText() if hasattr(control, "placeholderText") else ""
        )
        self._box_lays[group].addLayout(row)

    def _switch(
        self,
        group: int,
        key: str,
        label: str,
        checked: bool,
        *after: "Callable[[], None]",
    ) -> Switch:
        """一行「**点了会翻**」的开关，并登记进 :attr:`_w`。

        为什么要有这一个口：``ui_kit.Switch`` 的 ``clicked`` **不翻转自身**（与
        ``setChecked`` 解耦，避免「刷新 UI → setChecked → 回环」，见它的 docstring），
        谁用它谁负责翻。漏了那一句的开关，鼠标按下去**一点反应都没有** —— 不是「改了
        没保存」，是当场就不动（2026-10-11 用户报「编辑界面所有 Switch 点击无效」）。
        编辑屏里每一枚开关都从这里出，别再手写 ``Switch(...)``。

        翻完再跑 ``after``：那些「这一格一变，别处跟着灰/亮」的重算。注意重算**要在
        翻之后**——反过来的话它读到的仍是旧值（``track_done`` 那枚原来就接在
        ``_sync_task_enabled`` 上而没翻，于是「接了线却仍是死的」）。
        """
        sw = Switch(self._boxes[group])
        sw.setChecked(checked)

        def _flip() -> None:
            sw.setChecked(not sw.isChecked())
            for cb in after:
                cb()

        sw.clicked.connect(_flip)
        self._row(group, key, label, sw)
        return sw

    # ---------- 哪几格此刻说得上话 ----------

    def _sync_enabled(self) -> None:
        """按此刻的取值决定哪几格说得上话（灰掉 ≠ 删掉）。

        三条判据**与内核同一份口径**（不在界面里另立一套）：

        - 问时刻的条目（``ask=time``）靠**作答**算时刻，没有一个说得出的固定时刻
          （``plugin._conditions_of`` 不为它加 ``daily_at``、``scheduler._span_of``
          的 ``work_hours`` 分支也不看 ``at``）；
        - 循环条（``interval_min > 0``）的登场是**算出来的整点**，``at`` 同样不参与；
        - 「结束 ＝ 作答后 N 小时」只在 ``ask=time`` 这条路上有意义（别的问法没有
          「作答那一刻」这个东西）。

        置灰的只是**输入口**，``_read_form`` 仍照原样读出 draft 里的值 —— 用户把问法
        从「到点弹『完成』」切到「程序启动时问我」再切回来，原来填的时刻还留着（那是他
        自己写的字，不该因为我们灰了一下就丢掉）。

        **2026-10-08 补：光灰不够**。此前只 ``setEnabled(False)``，而全局 QSS 里
        **没有**输入控件的 ``:disabled`` 规则（只有按钮有），且不写一个字 —— 用户看到的
        是一个长得和其它格子一模一样的格子，点进去没反应。现在三件一起做：灰控件、
        淡它那枚标签、并把「为什么这格此刻不说话」挂成悬停提示（见 :meth:`_set_live`），
        再加上组内那条会跟着变的交代（:meth:`_timing_hint`）。
        """
        if not self._w:
            return
        ask = self._ask()
        repeat = self._int_of("interval_min") > 0
        self._set_live(
            "at",
            ask != PROMPT_TIME and not repeat,
            "这条的时刻由每天的作答算出来 —— 这一格不参与。"
            if ask == PROMPT_TIME
            else "循环条按整点登场，这一格不决定它几点出现。",
        )
        self._set_live(
            "work_hours",
            ask == PROMPT_TIME,
            f"只有「{ASK_WORDS[PROMPT_TIME]}」的条目有「作答那一刻」，别的问法这一格不参与。",
        )
        self._timing_note.setText(self._timing_hint())

    def _set_live(self, key: str, live: bool, why: str) -> None:
        """一格此刻说不说得上话：灰掉它、淡掉它那枚标签、把「为什么」挂成悬停提示、
        把空格子里的占位字换成一句交代。

        前两件是**同一个信号的两个投影**（控件与它那枚标签），第三件才是用户真正会读到
        的那句话 —— 见 :data:`_DEAD_PLACEHOLDER`。

        标签走 ``apply_text_role(…, color=…)`` 而不是全局 QSS 的 ``QLabel:disabled``：
        标签身上已经烤着一份 ``caption`` 档的样式表（``form_label``），**控件自己的
        样式表压过全局那条** —— 写一条全局规则等于写了不生效。

        占位字只对**有占位字这东西**的控件换（``QLineEdit`` 有，``QDoubleSpinBox`` 没
        有 —— 它靠 ``specialValueText`` 说事）。**不碰控件的值**：灰掉只是「这一格此刻
        不参与」，用户先前填过的字还在（:meth:`_read_form` 照原样读），亮回来时占位字
        也照 :attr:`_places` 里留的那份还原。
        """
        control = self._w[key]
        control.setEnabled(live)
        control.setToolTip("" if live else why)
        label = self._labels.get(key)
        if label is not None:
            apply_text_role(label, "caption", color=None if live else "text3")
        if hasattr(control, "setPlaceholderText"):
            control.setPlaceholderText(
                self._places.get(key, "") if live else _DEAD_PLACEHOLDER
            )

    def _timing_hint(self) -> str:
        """组 2 里那句会跟着变的交代：「提醒时间 / 结束＝作答后」此刻谁在说话。

        它是**状态位**不是开场说明（同 ``EditorShell.set_hint`` 那条）：四档各说各的
        事实，问法一改它就跟着改 —— 一句常驻的「只有某档才用得上」在别的档上说的就是
        已经过期的话。
        """
        if self._int_of("interval_min") > 0:
            return "循环条按整点登场，这一格不决定它几点出现；「结束时间」说循环到几点为止。"
        if self._ask() == PROMPT_TIME:
            return "这条的时刻由每天的作答算出来：填「结束＝作答后」几小时，就是你几点下班。"
        if self._ask() == PROMPT_CHOICE:
            return (
                "「结束＝作答后」只在「"
                f"{ASK_WORDS[PROMPT_TIME]}」的条目上有用（别的问法没有「作答那一刻」）。"
            )
        return f"「{ASK_WORDS['']}」的条目没有「作答那一刻」，「结束＝作答后」不生效。"

    def _sync_actions(self) -> None:
        """那枚「⋯ 更多」按钮按「这一条此刻做不做得出那些事」显隐。

        菜单项在**弹出那一刻**现造（见 :meth:`_on_more`）—— 状态是活的（今天做完了没、
        保存了没），先造好一份就会停在按下它的那一刻。这里只回答一个更粗的问题：
        **有没有**任何一件可做的事（没有就把那枚按钮也收起来，不留一个空菜单）。
        """
        if self._draft is None or not self._w:
            return
        self._more_btn.setVisible(bool(self._more_items()))

    def _more_items(self) -> "list[MenuItem]":
        """这一刻这一条能做的次动作（按「做不做得出」过滤，顺序即菜单顺序）。

        - **预览这一条**：恒在 —— 未保存的条目也预览得出来（内核补一个假 id）；
        - **现在问我**：只在会弹询问的条目上（问法非空 ＋ 已保存）。没保存的条目
          答案没处落，弹出来是骗人（``Plugin.ask_now`` 会拒，但不该先画出来）；
        - **标记今天完成**：只对问 ``choice`` 的条目 —— 「完成」是这一类的概念，
          问 ``time`` 的那一格存的是**答案**不是「做完了没」，写进去只会把那天的答案
          顶掉（``scheduler`` 的 ``_hidden_ids`` 也只收 ``choice`` 型的完成）。

        ``hint`` 装的是这三枚各自原本的**悬停提示文案**（折进菜单之后 tooltip 没地方挂，
        而 ``CandidateMenu`` 的两行长相本来就是为「一句名字不够、要补一句这是什么」准备
        的 —— 决议 D25）。
        """
        if self._draft is None or not self._w:
            return []
        if self._new_model():
            return self._task_more_items()
        saved = bool(self._draft.get("id"))
        ask = _ask_of(dict(self._draft, ask=self._ask()))
        items = [MenuItem(
            label="预览这一条",
            hint="按上面填的值弹一次浮层（不用保存，也不受「只在某场景」限制）",
            on_click=self._on_preview,
        )]
        if saved and ask:
            items.append(MenuItem(
                label="现在问我",
                hint="立刻弹一次询问浮层（不等条件、不管今天问过没）",
                on_click=self._on_ask,
            ))
        if saved and ask == PROMPT_CHOICE:
            done = self._plugin.done_at(str(self._draft["id"]))
            items.append(MenuItem(
                label="撤销今天完成" if done else "标记今天完成",
                hint="不用等浮层 —— 你自己做了就直接记上（这是「拉」的那条路）",
                on_click=self._on_toggle_today,
            ))
        return items

    def _on_more(self) -> None:
        """弹出「⋯ 更多」：现造菜单项 → 非阻塞 ``popup()`` → 持引用替它保命。

        三件都是 ``CandidateMenu`` 那条既有约定（见 ``ui_kit/panel/actions.py`` 的
        「⋯」溢出浮层，同一套写法）：``exec()`` 会在置顶窗上再开一条模态循环；非阻塞
        弹层出作用域就可能被回收；锚点用**按钮自己**的全局矩形。
        """
        items = self._more_items()
        if not items:
            return
        menu = CandidateMenu(items, parent=self._more_btn)
        self._more_menu = menu
        anchor_rect = QRect(
            self._more_btn.mapToGlobal(QPoint(0, 0)), self._more_btn.size()
        )
        menu.popup_at(
            self._more_btn.mapToGlobal(QPoint(0, self._more_btn.height())), anchor_rect
        )

    # ---------- 三格的读口 ----------

    def _ask(self) -> str:
        """编辑屏里此刻选的那个问法（``""`` ＝ 不问）。控件不在 ⇒ 按缺省 ``choice``。"""
        sel = self._w.get("ask")
        return sel.current() if sel is not None else PROMPT_CHOICE   # type: ignore[attr-defined]

    def _text_of(self, key: str) -> str:
        w = self._w.get(key)
        return w.text().strip() if w is not None else ""            # type: ignore[attr-defined]

    def _int_of(self, key: str) -> int:
        w = self._w.get(key)
        return int(w.value()) if w is not None else 0               # type: ignore[attr-defined]

    # ---------- 表单的两个转换口 ----------

    def _validate_form(self) -> "str | None":
        """保存前置门（注入给 ``EditScreen.on_validate``）：返回文案 = 一个字都不落盘。

        **按模型分派**：一屏两套表单，而 ``EditScreen`` 只注入得下一对回调 —— 分派写在这
        两个入口里（而不是在注入点上按模型换回调），是因为那两个闭包是**造屏那一刻**绑死的，
        模型却可能在中途变（用户刚清空旧表 / 刚被迁移）。
        """
        if self._new_model():
            return self._validate_task_form()
        if self._draft is None or not self._w:
            return None
        ask = self._ask()
        repeat = self._int_of("interval_min") > 0
        at = self._text_of("at")
        # 要弹提醒的条目必填时刻（循环条例外：它的登场是算出来的整点）。
        if ask == PROMPT_CHOICE and not repeat and not _valid_hhmm(at):
            return "要弹提醒的条目得填「提醒时间」（HH:MM，如 09:30）。"
        for key, label in (("at", "提醒时间"), ("end", "结束时间")):
            value = self._text_of(key)
            if value and not _valid_hhmm(value):
                return f"{label}格式应为 HH:MM（如 09:30）。"
        return None

    def _read_form(self) -> "dict | None":
        """按**格子里此刻的值**读出一份条目（**不落盘**）。三枚动作与「确定」共用。"""
        if self._draft is None or not self._w:
            return None
        draft = dict(self._draft)
        draft["enabled"] = self._w["enabled"].isChecked()            # type: ignore[attr-defined]
        draft["title"] = self._text_of("title")
        draft["text"] = self._text_of("text")
        draft["at"] = self._text_of("at")
        draft["end"] = self._text_of("end")
        draft["scene"] = self._text_of("scene")
        draft["interval_min"] = self._int_of("interval_min")
        draft["before_min"] = self._int_of("before_min")
        draft["work_hours"] = float(self._w["work_hours"].value())   # type: ignore[attr-defined]
        draft["ask"] = self._ask()
        draft["face"] = self._w["face"].current()                    # type: ignore[attr-defined]
        actions = []
        if self._w["act_done"].isChecked():                          # type: ignore[attr-defined]
            actions.append(ACTION_DONE)
        if self._w["act_snooze"].isChecked():                        # type: ignore[attr-defined]
            actions.append(ACTION_SNOOZE)
        draft["actions"] = actions
        return draft

    def _commit_form(self) -> None:
        """「确定」（注入给 ``EditScreen.on_commit``）：把这一条收进列表并**立刻写盘**。

        独立数据管理：每条待办各写各的，**没有「攒着等窗口那枚保存」这件事**（那是设置
        窗的模型，与这里是两码事）。写盘失败就红字留在编辑屏、不让回列表 —— 内存那一步
        也撤回去，避免「界面说存了、盘上其实没存」。

        **按模型分派**（理由见 :meth:`_validate_form`）。
        """
        if self._new_model():
            self._commit_task_form()
            return
        err = self._validate_form()
        if err:
            self._edit_fb.error(err)
            return
        draft = self._read_form()
        if draft is None:
            return
        # 先在内存里摆好这一条（失败时要能原样撤回去）。
        if self._adding:
            _assign_id(draft, self._items)
            self._items.append(draft)
        elif self._editing is not None:
            self._items[self._editing] = draft
        if not self._plugin.save_items(self._items):
            if self._adding:
                self._items.pop()
            elif self._editing is not None and self._draft is not None:
                self._items[self._editing] = self._draft   # 还原成改之前那份
            self._edit_fb.error("写回配置没成功——改动没存下，请重试。")
            return
        title = str(draft.get("title") or "").strip() or _EMPTY_TITLE
        adding = self._adding
        self._editing = None
        self._adding = False
        self._draft = None
        self._stack.back()
        self._refresh_list()
        self._edit_fb.dismiss()
        # 回执说在**列表屏**那条槽：此刻编辑屏已经切走，说在那边没人看得见。
        # 同款落点见 `autotype/settings.py` 与该文件下面这条新模型路径。
        self._list_fb.info(f"已{'添加' if adding else '保存'}「{title}」。")

    # ---------- 导航 ----------

    def _open_edit(self, task_id: str) -> None:
        """进编辑屏改这一条（**新模型与旧模型各一套表单，都在这一屏里**）。"""
        if self._new_model():
            idx = next(
                (
                    i
                    for i, t in enumerate(self._tasks)
                    if str(t.get("id") or "") == task_id
                ),
                None,
            )
            if idx is None:
                return
            self._editing = idx
            self._adding = False
            self._draft = deepcopy(self._tasks[idx])
            self._edit.set_title("编辑任务")
            self._edit_fb.dismiss()
            self._rebuild_dyn()
            self._stack.push(self._edit)
            return
        idx = next(
            (i for i, item in enumerate(self._items) if str(item.get("id") or "") == task_id),
            None,
        )
        if idx is None:
            return
        self._editing = idx
        self._adding = False
        self._draft = deepcopy(self._items[idx])
        self._edit.set_title("编辑条目")
        self._edit_fb.dismiss()
        self._rebuild_dyn()
        self._stack.push(self._edit)

    def _on_add(self) -> None:
        self._editing = None
        self._adding = True
        if self._new_model():
            self._draft = _new_task()
            self._edit.set_title("新增任务")
        else:
            self._draft = _new_item()
            self._edit.set_title("新增条目")
        self._edit_fb.dismiss()
        self._rebuild_dyn()
        self._stack.push(self._edit)

    def _on_back(self) -> None:
        """返回 ＝ **放弃这一条的改动**（写盘归框架那枚「保存」，这里连内存都不动）。"""
        self._editing = None
        self._adding = False
        self._draft = None
        self._edit_fb.dismiss()
        self._stack.back()
        self._refresh_list()

    def _on_ask_changed(self, value: str) -> None:
        if self._draft is None:
            return
        self._draft["ask"] = value
        self._edit_fb.dismiss()
        self._sync_enabled()
        self._sync_actions()

    # ---------- 三枚立刻生效的动作 ----------

    def _on_delete(self) -> None:
        if self._new_model():
            self._delete_task()
            return
        if self._editing is None or self._draft is None:
            return
        idx = self._editing
        removed = self._items.pop(idx)
        if not self._plugin.save_items(self._items):
            self._items.insert(idx, removed)   # 写盘失败：原样放回，留在编辑屏
            self._edit_fb.error("删除没写进配置——请重试。")
            return
        self._editing = None
        self._adding = False
        self._draft = None
        self._stack.back()
        self._refresh_list()

    def _on_toggle_today(self) -> None:
        """标记 / 撤销**今天**完成 —— 用户自己动手那条路，不经过浮层也不查去重账。"""
        if self._draft is None:
            return
        iid = str(self._draft.get("id") or "")
        if not iid:
            return
        done = self._plugin.done_at(iid)
        if not self._plugin.set_done(iid, "" if done else None):
            self._edit_fb.error("写完成记录没成功——请重试。")
            return
        self._refresh_list()
        self._sync_actions()
        self._edit_fb.info("已撤销今天的完成标记。" if done else "已标记今天完成。")

    def _on_ask(self) -> None:
        """「现在问我」：立刻弹这一条的询问浮层（**按格子里此刻填的值**，不必先保存）。"""
        err = self._validate_form()
        if err:
            self._edit_fb.error(err)
            return
        draft = self._read_form()
        if draft is None:
            return
        reason = self._plugin.ask_now(draft)
        if reason:
            self._edit_fb.error(reason)
            return
        self._edit_fb.info("已弹出询问浮层——填了就写进今天的值（当天不再问）。")

    def _on_preview(self) -> None:
        """「预览这一条」：立刻弹一次这一条的浮层（**按格子里此刻填的值**）。

        两套表单读出来的形状不同（旧表一条目 / 新模型一条**任务声明**），故预览那一头也
        两套 —— ``preview_entry`` 收内核形状，``preview_task`` 先解析求值再转交。把声明
        直接喂给浮层的话它认不出（恒不显示），而用户看到的是「点了预览没反应」。
        """
        if self._new_model():
            err = self._validate_task_form()
            if err:
                self._edit_fb.error(err)
                return
            task = self._read_task_form()
            if task is None:
                return
            reason = self._plugin.preview_task(task)
            if reason:
                self._edit_fb.error(reason)
                return
            self._edit_fb.info("已弹出这一条的显示效果——双击或右键关掉浮层就结束。")
            return
        err = self._validate_form()
        if err:
            self._edit_fb.error(err)
            return
        draft = self._read_form()
        if draft is None:
            return
        reason = self._plugin.preview_entry(draft)
        if reason:
            self._edit_fb.error(reason)
            return
        self._edit_fb.info("已弹出这一条的显示效果——双击或右键关掉浮层就结束。")

    # ---------- 独立数据管理：每条各自落盘（无窗口级「保存」/「未保存」状态） ----------


    # ---------- 新模型那一套表单（Phase 5） ----------
    #
    # 与旧十三格**共用同一个 EditScreen 与同一批行机制**（``_row`` / ``_set_live`` /
    # ``_w`` / ``_labels`` / ``_places``）：两套表单不会同时在场（走哪套是**整条盘**的性质），
    # 而 :meth:`_rebuild_dyn` 每次进出都把登记表清空重来 —— 两套的字段名不重名，一张
    # ``_w`` 就够。**不另建第二块编辑屏**：「返回 / 确定 / 预览 / 删除」这四条路两套表单
    # 一个字都不差，只有「格子长什么样」与「读出来的形状」不同，那两件事正是分派该管的
    # （与「任务不另开窗口」是同一条裁定的落点，§4.1）。

    def _rebuild_task_dyn(self) -> None:
        """按当前 draft（一条**任务声明**）重建字段。

        组与旧表单同标题（「这条待办 / 什么时候提醒 / 显示与按钮」）—— 用户的三个问题在
        新模型里一个字都没变，只有答案的字段换了名字。组内次序＝**先说哪一刻到期，再说它
        前后两个相对量，最后才是生效规则**：提醒与生效在新模型里是**正交**的两件事
        （§4.2 / §4.3，一条循环任务照样可以有自己的提醒时刻），故两者互不决定对方，
        也没有旧模型里「怎么问决定谁在说话」那一段因果。
        """
        draft = self._draft
        assert draft is not None
        for box_lay in self._box_lays:
            clear_layout(box_lay)
        self._w = {}
        self._labels = {}
        self._places = {}

        display = draft.get("display") if isinstance(draft.get("display"), dict) else {}
        effective = (
            draft.get("effective") if isinstance(draft.get("effective"), dict) else {}
        )
        reminder = (
            display.get("reminder") if isinstance(display.get("reminder"), dict) else {}
        )

        # ── 组 1：这条待办是什么 ──
        self._switch(0, "enabled", "启用", bool(draft.get("enabled", True)))

        # 「需要完成」：关掉之后这条照旧列在清单里、到点显示 / 上钟面 / 跑命令，只是
        # 不进「未完成 / 已完成」两个数、不画状态点、也没有「完成」这一动作 ——
        # 整点时钟这类「只显示、做不掉」的用它。**默认开**（＝现在所有条目的行为）。
        # 翻它 ⇒ 组 3 里那枚「行上「完成」」当场跟着亮灭（``_sync_task_enabled`` 要在
        # **翻之后**才读得到新值，故它是 ``_switch`` 的后置回调，不是单独的 connect）。
        self._switch(
            0,
            "track_done",
            "需要完成",
            bool(draft.get("track_done", True)),
            self._sync_task_enabled,
        )
        self._box_lays[0].addWidget(note(
            "关掉「需要完成」就是「只显示、做不掉」的那种（如整点时钟）：照旧列在清单里、"
            "到点照常显示 / 跑命令，但不进「未完成 / 已完成」两个数，也不打状态点、"
            "不给「完成」这一枚动作。",
            self._boxes[0],
        ))

        title = QLineEdit(self._boxes[0])
        title.setPlaceholderText("如 提交日报 / 喝水")
        title.setText(str(draft.get("title") or ""))
        self._row(0, "title", "内容", title, fill=True)

        text = QLineEdit(self._boxes[0])
        text.setPlaceholderText("主窗体那一行右边显示的句子，可留空")
        text.setText(str(draft.get("text") or ""))
        self._row(0, "text", "显示文本", text, fill=True)
        self._box_lays[0].addWidget(note(
            "显示文本里可写 <b>{变量名}</b>（如 <b>{off_time}</b> 会变成算出来的下班时刻），"
            "也可写 {到点} / {现在} / {剩余}。认不出的占位符会原样显示，方便你看出来写错了。",
            self._boxes[0],
        ))

        # ── 组 2：什么时候提醒 ──
        at = QLineEdit(self._boxes[1])
        at.setText(str(reminder.get("at") or ""))
        # 提醒时刻一改，「提前 / 延迟」这两格谁说得上话跟着变（见 ``_sync_task_enabled``）。
        at.textChanged.connect(lambda _t: self._sync_task_enabled())
        self._ref_row(
            1, "at", "提醒时间",
            "留空 ＝ 不提醒；如 09:30、{off_time}、{off_time}-30m", at,
        )
        self._box_lays[1].addWidget(note(_VAR_NOTE, self._boxes[1]))

        lead = NumberField(self._boxes[1])
        lead.setRange(0, _MAX_MINUTES)
        lead.setSuffix(" 分钟")
        lead.setSpecialValueText("不提前")
        lead.setValue(_int_or(display.get("lead_min"), 0))
        self._row(1, "lead_min", "提前登场", lead)

        trail = NumberField(self._boxes[1])
        trail.setRange(0, _MAX_MINUTES)
        trail.setSuffix(" 分钟")
        trail.setSpecialValueText("不延迟")
        trail.setValue(_int_or(display.get("trail_min"), 0))
        self._row(1, "trail_min", "延迟退出", trail)

        window = task_mod.window_of(effective)
        every = NumberField(self._boxes[1])
        every.setRange(0, _MAX_MINUTES)
        every.setSuffix(" 分钟")
        every.setSpecialValueText("不循环")
        every.setValue(task_mod.loop_minutes_of(effective))
        self._row(1, "every", "循环", every)

        win_from = QLineEdit(self._boxes[1])
        win_from.setText(window.start if window else "")
        # 两格一改，底下那行「此刻算不算数」跟着变（见 ``_sync_window_status``）——
        # 不接的话那一行停在开屏那一刻的结论，而用户正盯着格子改。
        win_from.textChanged.connect(lambda _t: self._sync_window_status())
        self._ref_row(
            1, "win_from", "生效时段 · 起",
            "如 09:00、{off_time}-30m；留空 ＝ 从零点起", win_from,
        )

        win_to = QLineEdit(self._boxes[1])
        win_to.setText(window.end if window else "")
        win_to.textChanged.connect(lambda _t: self._sync_window_status())
        self._ref_row(
            1, "win_to", "生效时段 · 止",
            "如 18:00、{off_time}+30m；留空 ＝ 到当天结束", win_to,
        )

        # 「此刻算不算数」：这一条是「生效时段」在界面上唯一说得上「有什么用」的地方 ——
        # 配了 09:00–18:00 的用户在晚上打开这扇窗，从前没有任何字告诉他这条现在不作数。
        # 长相走 ``note()``（竖直策略钉住，见那个函数的 docstring）—— 这一行的文字会
        # 随输入变长变短，自己造一个 QLabel 会把列里余高全喂给它。
        self._win_state = note("", self._boxes[1])
        self._box_lays[1].addWidget(self._win_state)
        self._box_lays[1].addWidget(note(
            "「生效时段」说的是这条<b>只在这段时间里算数</b>：时段之外不上浮层、"
            "不跑命令，主窗体清单里照旧列着（它不因时段而消失）。它和「提醒时间」"
            "各管各的 —— 一条每小时提醒的任务也可以只在白天生效。起晚于止"
            "（如 22:00–02:00）算跨午夜。",
            self._boxes[1],
        ))

        scene = QLineEdit(self._boxes[1])
        scene.setPlaceholderText("留空 ＝ 不限场景")
        scene.setText(_scene_of(effective))
        self._row(1, "scene", "只在某场景", scene, fill=True)
        self._box_lays[1].addWidget(note(
            "「只在某场景」写的是<b>场景名</b>（如 办公室），它来自变量 "
            "<b>{scene}</b>；留空就是不限。",
            self._boxes[1],
        ))

        # ── 组 3：显示与按钮 ──
        # 初值走 ``task.show_of`` 而不是 ``display.get("show")``：盘上那一段可能还是旧的
        # 两键（``overlay`` / ``clock``），直读新键会把「时钟」显示成「不显示」—— 用户
        # 什么都没改、按一次「确定」就真把它改成不显示了。折叠口径与解析层同一份。
        show = ChoiceBar(
            _SHOW_CHOICES,
            task_mod.show_of(display),
            self._boxes[2],
            hints=_SHOW_HINTS,
        )
        self._row(2, "show", "显示", show)

        buttons = tuple(str(b) for b in (display.get("buttons") or ()))
        self._switch(2, "act_done", "行上「完成」", task_mod.BUTTON_DONE in buttons)

        self._switch(2, "act_snooze", "行上「稍后」", task_mod.BUTTON_SNOOZE in buttons)
        self._box_lays[2].addWidget(note(
            "「显示」默认「不显示」：那一档的任务只在主窗体清单里，不上浮层。"
            f"「稍后」把这一条在浮层上收起 {LATER_MINUTES} 分钟，到点自己再弹一次。",
            self._boxes[2],
        ))

        # 组 3 的最后三行：这条任务**做点什么**。留着它们在这一组里（不另开第四组）是
        # 因为「显示与按钮」说的本来就是「它长什么样、抬手能做哪几件」——而这两格是
        # 这一格里唯一**碰进程**的：第三组的空组在旧模型下会露出一张只有标题的空卡，
        # 那个代价比「组名少说了半句」大（组名见 :data:`_EDIT_GROUPS`）。
        command = QLineEdit(self._boxes[2])
        command.setPlaceholderText(_COMMAND_PLACEHOLDER)
        command.setText(str(draft.get("command") or ""))
        self._ref_row(2, "command", "到点执行", _COMMAND_PLACEHOLDER, command)
        # 与「提醒时间」格同一句交代：命令走的也是那个变量插值口（``task.command_of``），
        # 而先前只有这一格没写出来 ⇒ 用户从界面读成「命令不支持变量」。
        self._box_lays[2].addWidget(note(_VAR_NOTE, self._boxes[2]))

        # 「在哪一刻跑」**恒可编辑**（2026-10-11 用户裁定）：它不跟着「有没有提醒时间」
        # 灰 —— 「点击完成」那一档压根不需要提醒时刻，灰着会让用户以为这一格整个不成立。
        # 前三个时刻档真要跑起来还是得有「提醒时间」，那一条由保存前的校验门说清楚
        # （``_validate_task_form``）：当场看得见，比一个灰格子里写着「此刻不用填」准确。
        cmd_at = ChoiceBar(
            _CMD_AT_CHOICES,
            str(draft.get("command_at") or task_mod.COMMAND_AT_REMINDER),
            self._boxes[2],
            hints=_CMD_AT_HINTS,
        )
        self._row(2, "command_at", "执行时机", cmd_at)
        self._box_lays[2].addWidget(note(
            "命令按空格切分参数 —— <b>不走 shell</b>（管不着管道与重定向；好处是那串字里"
            "写什么都没有第二层解释器，网址里的 &amp; 也不用转义）。命令的成败与它的输出"
            "都记在日志里；发请求记得加 --fail-with-body —— 不然 HTTP 报错（4xx / 5xx）"
            "也算「跑成功了」。「执行时机」说的是它在<b>哪一刻</b>跑：提醒时间 / 提前登场 / "
            "延后退出 这三刻都要先填「提醒时间」，「点击完成」不用。机器关着的那一刻不补跑"
            "——「到点执行」说的就是那一刻。",
            self._boxes[2],
        ))

        self._sync_task_enabled()
        self._sync_actions()
        self._sync_window_status()

    def _sync_window_status(self, now: "datetime | None" = None) -> None:
        """「生效时段」两格下面那一行：**此刻这条算不算数**（随两格的输入实时变）。

        三种回答走内核（:meth:`Plugin.window_status`），这里只多做一件事：**正在往里
        打而还没成形的输入**就地说明（与保存前那道门同一句口径）—— 不然打了一半的
        「9:」会显示成「生效时段待定」，像是个求值问题而不是打字问题。

        ``now`` 可注入：这一行带真实时钟，不注入的话同一条用例在上午跑与晚上跑得到
        两个结论（那不是用例写得松，是判据没被钉住）。
        """
        label = self._win_state
        if label is None or "win_from" not in self._w:
            return
        start, end = self._text_of("win_from"), self._text_of("win_to")
        if not start and not end:
            label.setText("两端留空 ＝ 全天候 —— 这一条什么时候都算数。")
            return
        for key, what in (("win_from", "生效时段的起点"), ("win_to", "生效时段的终点")):
            problem = self._time_problem(key, what)
            if problem is not None:
                label.setText(problem)
                return
        label.setText(
            self._plugin.window_status({"window": {"from": start, "to": end}}, now)
        )

    def _time_problem(self, key: str, label: str) -> "str | None":
        """这一格「读不出来」的那句话；``None`` ＝ 读得出来。**判据问内核**。

        界面这一层只负责把话说明白：用户写 ``{off_time}-20``（少了个单位）时，内核回的是
        「「时间 − 数字」没有定义（v1 只有 时间±时长、时间−时间、时长±时长）」—— 而从前
        界面只会说「要填 HH:MM 或 {变量名}」，用户写的正是那两样之一，读完还是不知道该
        补上什么。
        """
        value = self._text_of(key)
        if not value:
            return None
        why = self._plugin.time_ref_problem(value)
        if why is None:
            return None
        return (
            f"{label}读不出来：{why}。"
            f"可以填一个时刻（如 09:30），或在变量上加减时长（如 {{off_time}}-30m）。"
        )

    def _ref_row(
        self, group: int, key: str, label: str, placeholder: str, target: QLineEdit
    ) -> None:
        """一行「可引用变量的输入格」：标签 · 输入格 · 右端一枚「选变量 ▾」。

        变量引用在这条任务里就是**往格子里写 ``{变量名}``**（§4.3：先纯文本插值、再按
        表达式求值），故刻意**不做成「写死 / 引用」两态**——那种两态会让「我填了一半的字
        去哪了」变成一个要读代码才能回答的问题。按钮只做一件事：把当前值是变量的那几个
        变量列出来，选中就把 ``{id}`` 写进格子（替换，不追加）。
        """
        holder = QWidget(self._boxes[group])
        lay = QHBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(metrics.GAP_SM)
        lay.addWidget(target, 1)
        btn = tool_button("选变量 ▾", "pill-ghost", size="inline", parent=holder)
        btn.setToolTip("把 {变量名} 写进左边那一格（变量在「设置 → 变量」里维护）")
        btn.clicked.connect(lambda _checked=False, w=target: self._pick_variable(w))
        lay.addWidget(btn, 0)

        self._w[key] = target
        row = form_row(label, holder, self._boxes[group], control_fill=True)
        self._labels[key] = row.itemAt(0).widget()
        self._places[key] = placeholder
        target.setPlaceholderText(placeholder)
        self._box_lays[group].addLayout(row)

    def _pick_variable(self, target: QLineEdit) -> None:
        """弹出**已声明的**变量让用户挑一个（非阻塞 ``popup()``，与「⋯ 更多」同一套写法）。

        挑的是**声明**不是值：这一格要回答的是「可以引用谁」；每条的当前值在主窗体那条
        变量条上（§4.5②）。声明列表由 ``plugin.declared_variables()`` 给 —— 界面这边
        **不碰 ``Config``**（门禁 ``test_no_config_io_in_ui``）。
        """
        decls = self._plugin.declared_variables()
        if not decls:
            self._edit_fb.warn("还没有声明过变量——变量的家在「设置 → 变量」。")
            return
        menu = CandidateMenu(self._var_items(target), parent=target)
        # 非阻塞 popup ⇒ 出作用域就可能被回收，必须持引用替它保命（与 ``_more_menu`` 同款）。
        self._more_menu = menu
        rect = QRect(target.mapToGlobal(QPoint(0, 0)), target.size())
        menu.popup_at(target.mapToGlobal(QPoint(0, target.height())), rect)

    def _var_items(self, target: QLineEdit) -> "list[MenuItem]":
        """「选变量 ▾」弹层里那几项（**现造**：声明可能在别处刚被改过）。

        抽成一口是为了让判据钉得住 —— 直接问「会列出哪几个变量」，不必去驱动弹层内部
        （同 ``_more_items`` 与 ``_menu_labels`` 那一对的理由）。
        """
        return [
            MenuItem(
                label="{" + str(d.get("id") or "") + "}",
                hint=self._decl_hint(d),
                # 兄弟 lambda 读不到彼此的形参 ⇒ 要捕获的值各自带默认参数（本仓既有口径）。
                on_click=(
                    lambda t=target, i=str(d.get("id") or ""): self._use_variable(t, i)
                ),
            )
            for d in self._plugin.declared_variables()
        ]

    @staticmethod
    def _decl_hint(decl: dict) -> str:
        """变量菜单那一行右端的说明：名字 ＋ 来源（「这是什么」比「它从哪来」更该先读）。"""
        name = str(decl.get("name") or "").strip()
        source = SOURCE_LABELS.get(str(decl.get("source") or ""), "")
        if name and source:
            return f"{name}（{source}）"
        return name or source or "已声明的变量"

    def _use_variable(self, target: QLineEdit, var_id: str) -> None:
        """把 ``{var_id}`` 写进那一格（**替换**，不追加）。"""
        target.setText("{" + var_id + "}")
        self._edit_fb.dismiss()

    def _sync_task_enabled(self) -> None:
        """哪几格此刻说得上话：**没有提醒时刻 ⇒ 提前 / 延后两个偏移量不参与**（§4.3）。

        只有这一条因果：``lead_min`` / ``trail_min`` 框的是 ``[at − lead, at + trail]``
        —— 没有 ``at`` 就没有那个「点」可偏移。生效规则那几格（循环 / 时段 / 场景）
        **不跟着灰**：它们与提醒正交，一条循环任务照样可以有自己的提醒时刻。

        **「到点执行」与「执行时机」不在这条闸里**（2026-10-11 用户裁定：「「到点执行」
        应该一直允许输入」）。原先它跟着「到点弹卡」走，于是没选那张卡的人连一串命令都
        打不进去；而新加的「点击完成」那一档压根不需要提醒时刻 —— 一并灰掉只会把一句
        本来是真话的「此刻不用填」安到一个成立着的格子上。前三个时刻档真缺「提醒时间」
        时报在保存前那道门上（:meth:`_validate_task_form`），不在这里灰。

        行上那枚「完成」（``act_done``）另有一条闸：「不需要完成」的条目不给它 ——
        那枚按钮点下去只会记一笔却哪儿都不显示，留着就是一格空转。
        """
        if not self._w:
            return
        live = bool(self._text_of("at"))
        why = "这条没填「提醒时间」—— 没有「到点」就没有它前后的两个偏移量。"
        for key in ("lead_min", "trail_min"):
            self._set_live(key, live, why)
        if "act_done" in self._w:
            self._set_live(
                "act_done",
                self._tracks_done(),
                "这条关掉了「需要完成」——「完成」这一步对它不存在。",
            )

    def _tracks_done(self) -> bool:
        """此刻那枚「需要完成」的开关状态（控件不在 ⇒ 按默认 **开**）。"""
        widget = self._w.get("track_done")
        return True if widget is None else widget.isChecked()

    def _task_more_items(self) -> "list[MenuItem]":
        """新模型下这一条能做的次动作。

        **没有「现在问我」**：询问在新模型里**不属于任何任务**，它是变量自己的触发条件
        （§4.3）—— 任务的提醒时刻只是引用那个变量，问不问由变量说了算。留一枚「现在问我」
        要么问错对象、要么得在任务里再养一条询问，两条路都与那笔重构的要点相反。
        """
        if self._draft is None or not self._w:
            return []
        items = [MenuItem(
            label="预览这一条",
            hint="按上面填的值弹一次浮层（不用保存）",
            on_click=self._on_preview,
        )]
        iid = str(self._draft.get("id") or "")
        # 「不需要完成」的那条不给这一枚：它压根没有「完成」这回事（与行内不画「完成」、
        # 与它不进那两个数同一条口径）。
        if iid and self._draft.get("track_done", True):
            done = self._plugin.done_at(iid)
            items.append(MenuItem(
                label="撤销今天完成" if done else "标记今天完成",
                hint="不用等浮层 —— 你自己做了就直接记上（这是「拉」的那条路）",
                on_click=self._on_toggle_today,
            ))
        return items

    def _validate_task_form(self) -> "str | None":
        """保存前置门（新模型）：返回文案 = 一个字都不落盘。

        三道门各挡一类**存得进去、却永远不发生**的配置 —— 它们在界面上都读不出来
        （用户看到的是一个填好了的格子），故只能挡在保存这一刻，这也是唯一能把话说给
        用户听的位置。
        """
        if self._draft is None or not self._w:
            return None
        if not self._text_of("title"):
            return "任务得先有个名字——填「内容」那一格（主窗体那一行上显示的就是它）。"
        for key, label in (
            ("at", "提醒时间"),
            ("win_from", "生效时段的起点"),
            ("win_to", "生效时段的终点"),
        ):
            problem = self._time_problem(key, label)
            if problem is not None:
                return problem
        at = self._text_of("at")
        # 显示面（浮层 / 钟面）要有「那一刻」才登得上场：窗口由提醒时刻 ＋ 提前/延后两个
        # 偏移量算出来，循环条则由间隔算出来 —— 两者都没有时 ``resolve_item`` 一个窗口都
        # 不给，这条在浮层上永远不出现。留着它就是让用户读到「明明选好了显示、却从没出现」
        # 而没有任何线索（这一条是原先那句「要弹卡就得先填提醒时间」的直译：那段
        # 「到点弹卡」退场了，它原来守的那件事没有退场）。
        if (
            self._w["show"].current() != task_mod.SHOW_NONE
            and not at
            and not self._int_of("every")
        ):
            return (
                "选了「浮层 / 钟面」就得先说清它什么时候登场 —— 填「提醒时间」，"
                "或者给它一个「循环」间隔。两个都空着的话这条永远不会出现在浮层上。"
            )
        command = self._text_of("command")
        # 命令挑了那三个时刻之一，却没有提醒时刻 ⇒ ``command_due_s`` 恒算不出来，那串字
        # 一次都不会跑（而它在配置里看着是配好的）。这一条是「到点执行恒可编辑」的配套：
        # 不再用灰格拦它，改成保存时说清楚 —— 后者能同时容下「点击完成」那一档。
        if (
            command
            and not at
            and self._w["command_at"].current() != task_mod.COMMAND_AT_DONE
        ):
            return (
                "「执行时机」选了「提醒时间 / 提前登场 / 延后退出」—— 这三个时刻要先填"
                "「提醒时间」，那一格空着就没有可执行的时刻。也可以把它改成「点击完成」。"
            )
        # 「到点执行」那一串字要先切得开 —— 引号不配对的命令跑不起来，而运行期那边只会
        # 记一条 WARNING（离用户很远）。挡在这里的代价是零：切分口径就是真跑时用的那一份
        # （``core.hotkey_command.split_command``），不是界面自己又写一遍的近似。
        if command and split_command(command) is None:
            return "「到点执行」那串命令引号不配对，切不出参数 —— 补上引号（或者整格留空）。"
        return None

    def _read_task_form(self) -> "dict | None":
        """按**格子里此刻的值**读出一份任务声明（**不落盘**）。

        从 draft 出发、**只改这份表单管得着的那几格**：任务声明里允许出现表单没有的字段
        （手写的其它 ``effective.conditions``、将来新加的格子），整份重造会在「确定」那一刻
        把它们静默抹掉。
        """
        if self._draft is None or not self._w:
            return None
        task = deepcopy(self._draft)

        if self._w["enabled"].isChecked():
            task.pop("enabled", None)      # ``true`` 是默认档：写了等于没写
        else:
            task["enabled"] = False
        # 同上：``track_done`` 默认 **开**，只写关掉那一半（方向由默认值定）。
        if self._w["track_done"].isChecked():
            task.pop("track_done", None)
        else:
            task["track_done"] = False
        for key in ("title", "text", "command"):
            value = self._text_of(key)
            if value:
                task[key] = value
            else:
                task.pop(key, None)
        # 同一口径：``command_at`` 默认 ``reminder``，只写非缺省那一半。盘上手写怪值
        # （认不出的字符串）在这里被**归一**成此刻选中那一档 —— ``ChoiceBar`` 对不在候选
        # 里的取值本来就回落第一项，故「确定」一次就落成认得的取值（读侧不再报 problem）。
        cmd_at = self._w["command_at"].current()
        if cmd_at and cmd_at != task_mod.COMMAND_AT_REMINDER:
            task["command_at"] = cmd_at
        else:
            task.pop("command_at", None)

        display = dict(task.get("display") or {})
        at = self._text_of("at")
        if at:
            reminder = dict(display.get("reminder") or {})
            reminder["at"] = at
            # 已退场的 ``card`` 键就地清掉（与下面 ``overlay`` / ``clock`` 同一处置）：
            # 留着它不会有症状，但会让这份文件里同时存在两个「到点做什么」的档位 ——
            # 而其中一个已经没人读，读代码的人要先花一轮才能确认这件事。
            reminder.pop("card", None)
            display["reminder"] = reminder
        else:
            display.pop("reminder", None)
        for key in ("lead_min", "trail_min"):
            value = self._int_of(key)
            if value:
                display[key] = value
            else:
                display.pop(key, None)
        # 同一口径：``show`` 默认 ``none``（＝不上浮层），只写非缺省那一半。
        show = self._w["show"].current()
        if show and show != task_mod.SHOW_NONE:
            display["show"] = show
        else:
            display.pop("show", None)
        # 这两格（``overlay`` / ``clock``）已被 ``show`` 取代 ⇒ 就地清掉，不让新旧两套
        # 并存：解析层只在新键缺席时才看旧键，留着它们会让「下一轮读到的是折叠前的
        # 那一份」，而新写进去的 ``show`` 与旧键说的是两件事（手改文件的人无从判断谁赢）。
        display.pop("overlay", None)
        display.pop("clock", None)
        buttons = [
            label
            for label, key in (
                (task_mod.BUTTON_DONE, "act_done"),
                (task_mod.BUTTON_SNOOZE, "act_snooze"),
            )
            if self._w[key].isChecked()
        ]
        if buttons:
            display["buttons"] = buttons
        else:
            display.pop("buttons", None)
        if display:
            task["display"] = display
        else:
            task.pop("display", None)

        effective = dict(task.get("effective") or {})
        every = self._int_of("every")
        if every:
            effective["loop"] = {"kind": task_mod.LOOP_INTERVAL, "every": f"{every}m"}
        else:
            effective.pop("loop", None)
        start, end = self._text_of("win_from"), self._text_of("win_to")
        if start or end:
            window = dict(effective.get("window") or {})
            for side, value in (("from", start), ("to", end)):
                if value:
                    window[side] = value
                else:
                    window.pop(side, None)
            effective["window"] = window
        else:
            effective.pop("window", None)
        # 场景那一格只认 ``scene`` 这一条条件；别的条件（手写的 ``weekday`` 之类）原样留着。
        conditions = [
            c
            for c in (effective.get("conditions") or [])
            if not (isinstance(c, dict) and str(c.get("var") or "") == _SCENE_VAR)
        ]
        scene = self._text_of("scene")
        if scene:
            conditions.append({"var": _SCENE_VAR, "op": "==", "value": scene})
        if conditions:
            effective["conditions"] = conditions
        else:
            effective.pop("conditions", None)
        if effective:
            task["effective"] = effective
        else:
            task.pop("effective", None)
        return task

    def _commit_task_form(self) -> None:
        """「确定」（新模型）：把这一条收进任务表并**立刻写盘**（与旧表那一条同款）。"""
        err = self._validate_task_form()
        if err:
            self._edit_fb.error(err)
            return
        task = self._read_task_form()
        if task is None:
            return
        if self._adding:
            _assign_id(task, self._tasks)
            self._tasks.append(task)
        elif self._editing is not None:
            self._tasks[self._editing] = task
        if not self._plugin.save_tasks(self._tasks):
            if self._adding:
                self._tasks.pop()
            elif self._editing is not None and self._draft is not None:
                self._tasks[self._editing] = self._draft   # 还原成改之前那份
            self._edit_fb.error("写回配置没成功——改动没存下，请重试。")
            return
        title = str(task.get("title") or "").strip() or _EMPTY_TITLE
        adding = self._adding
        self._editing = None
        self._adding = False
        self._draft = None
        self._stack.back()
        self._refresh_list()
        self._edit_fb.dismiss()
        self._list_fb.info(f"已{'添加' if adding else '保存'}「{title}」。")

    def _delete_task(self) -> None:
        """删掉这一条任务并写盘（失败就原样放回，留在编辑屏）。"""
        if self._editing is None or self._draft is None:
            return
        idx = self._editing
        removed = self._tasks.pop(idx)
        if not self._plugin.save_tasks(self._tasks):
            self._tasks.insert(idx, removed)
            self._edit_fb.error("删除没写进配置——请重试。")
            return
        self._editing = None
        self._adding = False
        self._draft = None
        self._stack.back()
        self._refresh_list()


def _actions_of(item: dict) -> tuple:
    """这一条配了哪几枚行内动作（只用来回填那两个开关）。"""
    raw = item.get("actions")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(a for a in (str(x).strip() for x in raw) if a in (ACTION_DONE, ACTION_SNOOZE))


def _assign_id(item: dict, items: "list[dict]") -> None:
    """给新条目排一个 id（已填就原样留着）。

    id **不进界面**：它只是「答案落到哪一条」的键，用户不需要看见也不需要起名。
    **从现有最大序号 ＋ 1 单调递增**，绝不复用已删条目的 id：复用会让新条目继承旧条目
    在去重账（``interaction_state.json``）里的 ``last_asked=today``，表现为「新增当天
    不再弹提醒」（P1）。
    """
    if item.get("id"):
        return
    taken = {str(i.get("id")) for i in items}
    nums = []
    for t in taken:
        m = re.match(r"^td(\d+)$", str(t))
        if m:
            nums.append(int(m.group(1)))
    nxt = (max(nums) + 1) if nums else 1
    while f"td{nxt}" in taken:
        nxt += 1
    item["id"] = f"td{nxt}"


def build_detail_page(plugin: "Plugin") -> QWidget:
    """构建详情页内容面（框架按 ``DetailEntrySpec.build()`` 零参调用，故用闭包注入插件）。"""
    return _TodoManager(plugin)


# ── 新模型那份表单的几个小工具（Phase 5） ───────────────────────────
#
# 一律模块级并放在文件末尾（本仓纪律：往类体中间插模块级函数会把后面的方法缩进成它的
# 嵌套函数，Python 与 pyflakes 都不报，症状是远处几条用例莫名转红）。


def _scene_of(effective: dict) -> str:
    """``effective.conditions`` 里「场景等于某值」那一条的值（没有就空串）。

    只认 ``var == "scene"`` 且 ``op == "=="`` —— 与 :meth:`_TodoManager._read_task_form`
    写回去的是**同一个判据**（各写一份的话会出现「填进去的值读不回来」，而两处都不报错）。
    """
    conditions = effective.get("conditions") if isinstance(effective, dict) else None
    for cond in conditions if isinstance(conditions, (list, tuple)) else ():
        if not isinstance(cond, dict):
            continue
        if str(cond.get("var") or "") == _SCENE_VAR and str(cond.get("op") or "") == "==":
            return str(cond.get("value") or "")
    return ""


def _int_or(value: object, fallback: int) -> int:
    """读得出整数就用它，读不出用兜底。

    回填那几格用：盘上手写的脏值（``lead_min: "半小时"``）不该让**开屏那一刻**抛 ——
    编辑屏是用户来修配置的地方，它自己先炸掉就没人能修了。``bool`` 要排掉（它是 ``int``
    的子类，``True`` 会变成 1）。
    """
    if isinstance(value, bool):
        return fallback
    if isinstance(value, (int, float)):
        return int(value)
    return fallback


def _new_task() -> dict:
    """一条新任务的初值（§4.1）：四类出口全关、提醒时刻留空。

    与旧模型 :func:`_new_item` 同一条理由：初值决定用户「什么都不改直接按确定」得到什么，
    而一条刚建出来的任务不该自己弹卡 / 占浮层 / 执行命令 —— 那几件事他一件都还没说过。
    提醒时刻留空也正合「设了才提醒」那条口径（§4.3）。
    """
    return {"id": "", "title": "", "display": {}}
