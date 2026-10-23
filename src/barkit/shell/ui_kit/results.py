"""动作结果的三个出口（当场 / 晚一拍 / 交给宿主）与布局清理口。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import (
    QTimer,
)
from PySide6.QtWidgets import (
    QLayout,
    QWidget,
)

from barkit.contribution.specs import as_action_result

if TYPE_CHECKING:

    from barkit.contribution.specs import ActionCallback, ActionResult

from .base import qt_alive

def present_to_host(widget: "QWidget | None", result: "ActionResult | None") -> None:
    """把一个**已经拿到的**结局交给所在的面板（沿控件树往上找）。

    与 :func:`relay_action_result` 的分工：这一半只管「往上找宿主」，不跑回调 ——
    需要**晚一拍**才回访宿主的场合（见 :func:`defer_result_to_host`）复用它，免得
    「往上找」的逻辑长出第二份。

    ``present_action_result`` 是宿主侧的**唯一约定**（谁能接结果槽，就看有没有这个方法），
    故这里用鸭子类型而不是 ``isinstance`` —— 面板之外再有人想接，接上即可。
    """
    node = widget
    while node is not None:
        report = getattr(node, "present_action_result", None)
        if callable(report):
            report(result)
            return
        node = node.parent()


def relay_action_result(widget: "QWidget | None", cb: "ActionCallback") -> None:
    """跑一次动作回调，把它的 ``ActionResult`` **当场**交给所在的面板。

    ⚠️ 这一片有一条**尚未定位**的本机崩溃，别照着「已经安全」理解。2026-09-18 实测
    （Windows ＋ 离屏 Qt）：把中继也接到**整行点击**上（即在 ``HoverRow.mousePressEvent``
    这个 Python 覆盖里**当场**回访宿主）后，整仓 pytest 跑到中途 ``Windows fatal exception:
    access violation``，崩点每次不同；HEAD 基线同一条命令跑得完。已实测排除：循环引用本身
    （行与面板在基线里本来就是环）、循环 GC 拆控件树（关掉 GC 照样崩）、面板多两个子对象
    （给基线注入无害子对象不复现）、宿主方法有副作用（``None`` 路径是真空转）。
    现场证据指**一处悬垂引用，能否命中取决于分配地址** —— 两个逻辑等价的版本一个红一个绿，
    故**在这一处换写法只是掷骰子，不是修 bug**。
    **整行点击因此改走 :func:`defer_result_to_host`（晚一拍，把「回访宿主」挪出控件自己的
    事件处理栈 —— 那是结构性改动，不是换个等价写法）**；本函数继续服务行内控件与候选菜单项。
    完整对照实验、0.6 秒复现命令与决策见 `内部记录（反馈机制）`。

    这里**零存储**：``widget`` 是**实参**，不被任何闭包捕获 —— 面板不在任何一条引用链上。
    这只是「别把无关对象拖进生命周期」的卫生习惯，**不是**上面那条崩溃的已证解法。

    配套约定（改这一片时一起守；都是「控件别攥着宿主」的同一件事）：

    - 行内控件（开关 / 按钮 / 菜单）一律连**本行自己的绑定方法**，再用 ``sender()``
      认领是哪一个。PySide6 对「QObject 的绑定方法」是按**接收者**登记的 C++ 连接、
      **不留 Python 强引用**（本机探针实测：子计时器连父的绑定方法不成环），
      换成 ``lambda w=self: ...`` 立刻就成环了。
    - 候选列表项在 ``CandidateMenu.pick`` 里以**弹层自己**为起点中继，``MenuItem.on_click``
      保持插件原回调 —— 冻结值对象里不塞闭包（塞了就会连出一片环）。
    - 起点一律是**发起动作的那个控件本身**（行控件 / 弹层），不是别人传进来的引用。
    """
    present_to_host(widget, as_action_result(cb()))


def defer_result_to_host(owner: "QWidget | None", result: "ActionResult | None") -> None:
    """把一次「回访宿主」**推迟到事件循环下一拍**再走 :func:`present_to_host`。

    为什么不能就地在控件的 ``mousePressEvent`` 里回访宿主（2026-09-18 实测的崩溃，取证见
    :func:`relay_action_result`）：那是在**控件自己的事件处理栈里**跑任意业务逻辑。
    业务逻辑完全可能把发起这次点击的控件拆掉 —— 面板重画状态行就是这么干的
    （``StatusPanel.sync_status_rows`` 把不在新集合里的行 ``deleteLater``）；而
    ``mousePressEvent`` 返回后 Qt 还要继续用这个控件。晚一拍之后：业务先跑完、控件树
    先结算，再回访宿主，**并且在回访前先判活**（行都没了就丢弃这次回执 —— 面板都重画了，
    那条回执也过期了）。

    这不是「换个等价的写法」（那种换法是掷骰子）：被挪走的是**回访宿主的时机**，
    从「控件事件处理栈内」挪到「事件循环里」。

    延迟量取 ``0``：不是「等一会儿再报」，而是「这一轮事件处理完了就报」—— 用户看不出来。
    """
    if result is None:
        return

    def _report() -> None:
        if not qt_alive(owner):
            return
        present_to_host(owner, result)

    QTimer.singleShot(0, _report)


def clear_layout(layout: QLayout) -> None:
    """把一个布局**连带子布局里的控件**全部摘干净 —— 「重画一整组控件」的唯一清理口。

    为什么要有这个口（2026-09-27 用户上报「翻页时钟设置页『触发场景』行控件重叠，
    包括下面的纯文本」）：``takeAt`` 交出来的只是 item，而 ``form_row`` 交出来的是一层
    ``QHBoxLayout``（标签与控件挂在它**里面**）⇒ 只删 ``item.widget()`` 的话，那些
    控件一个都删不掉（子布局项的 ``widget()`` 是 ``None``）。它们仍以容器为父、停在
    **上一轮的几何**上原地可见，于是新建的那一组和它们完全重叠。实测那份页面切两次
    「上班时间来源」就留下 **22 枚**漂浮控件、**39 处**两两重叠。

    摘法是 ``setParent(None)`` ＋ ``deleteLater()``，两件都要：

    - ``setParent(None)`` 立刻把它移出父控件树 —— 下一帧就不再绘制、``findChildren``
      也找不到它。**判据量得到**（``test_settings_page_has_no_orphan_widgets`` 那一族
      就是查「可见却不在布局里」的控件）。
    - ``deleteLater()`` 只是标记，事件循环跑起来之前对象还活着。只写它、不写前者的话，
      重建后的那一帧里新旧两套控件**同时可见**，用户看到的还是重叠。
    """
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
            continue
        sub = item.layout()
        if sub is not None:
            clear_layout(sub)

