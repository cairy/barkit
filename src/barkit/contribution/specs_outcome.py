"""**一次动作的结局与结果回执**：``ActionOutcome``（三种取值、**没有** ``pending``，理由写在类 docstring 里）、``ActionResult``（让不碰界面的插件把结局告诉宿主），以及全仓唯一的规整函数 ``as_action_result``（把回调返回值里漏出来的 ``True`` / 元组挡在宿主入口之外，记一条 warning 后当「没话说」）。

``as_action_result`` 是全族唯一用 logger 的那个（``log.warning``），故 ``log`` 定义也随它搬进来，**日志名写死 ``barkit.contribution.specs``**（不用 ``__name__``：兄弟模块的 ``__name__`` 会把外观改掉，而 ``%(name)s`` 会写进日志文件；``tests/test_hotkey_feedback.py`` 按这个点名收那条 warning）。

依赖：无（叶子）。``ActionCallback`` 不在这里 —— 它在 ``base``，见 plan 文件头的环说明。

``specs.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum


log = logging.getLogger("barkit.contribution.specs")


# ---- 动作结果（`ActionResult`，决议 D32-② /《网络切换》§5.3-②）----


class ActionOutcome(Enum):
    """一次**动作**（开关 / 按钮 / 整行点击 / 候选菜单项）的结局。

    **只有三种取值，没有 `pending`**（有意偏离两家设计的建议，理由写在下面）：

    两家设计在登记这条缺口时都写了 ``pending`` / ``ok`` / ``failed`` / ``partial``
    四个值，本批只落三个 —— 「已派出、结果待定」要向宿主讲清楚**谁在什么时候把结果
    送回来**，那是一整套异步回执机制；今天全仓没有一个动作能诚实地交出它（``open -a``
    是同步返回码，写 YAML 是同步成败），于是 ``pending`` 只会是一个**永远没人终结的
    中间态**。登记 cited 里那条自己的口径为准：宁可少一台状态机，不声明一张只会撒谎的表
    （《网络切换》第 6 步 ``net`` 机就是这么推迟的）。
    **启动条件**：出现第一个「动作必然异步完成」像素 —— 典型是经 root helper 转一圈才
    知道成败的操作 —— 届时连同回执通道一起补 ``PENDING``，不让组件自己拿着 ``ok`` 报。
    """

    OK = "ok"            # 做成了
    FAILED = "failed"    # 没做成（该做的都没做）
    PARTIAL = "partial"  # 做成了一部分（几条里有几条没走通）


@dataclass(frozen=True)
class ActionResult:
    """动作的结果反馈（契约 §2.6，决议 D32-② /《网络切换》§5.3-②）。

    **它是干什么的**：让**只会做事、不许碰界面**的插件把结局告诉宿主 —— 在此之前，
    ``on_toggle`` / ``on_click`` 一律是 ``Callable[[], None]``，装不下「需授权 / 可能失败」，
    插件想说点什么只能自弹 ``QMessageBox``，与契约 §1.2「组件不碰界面」直接冲突，实机上
    还会开嵌套事件循环把置顶窗卡死（``ui_kit.FeedbackBar`` docstring 有崩溃报告）。

    **什么时候返回它**：**只在有话要说时**返回。什么都不返回（``None``）＝「这次没什么
    值得占一行字」——成功且状态本身就看得见（开关翻了 / 窗体弹出来了）就别再写一句
    「已成功」，那句话的生命周期比它的信息量长。返回 ``ActionResult`` 的两条正当理由：

    1. **失败了，用户却看不出来**（主控制台记了日志、界面毫无变化）；
    2. **只做成了一部分**（``PARTIAL``），得说清哪一半没走通。

    ``text`` 留空视为「没话说」，宿主当作 ``None`` 处理 —— 不说空话比说套话好。

    **宿主怎么呈现**：由**宿主**决定，插件不知道也不该知道。今天的呈现面是面板里那一行
    共享的结果槽（``shell/panel.StatusPanel.present_action_result``）：成功短暂一现，
    失败留到下一次动作或面板重开。将来换牌子地位。
    """

    outcome: ActionOutcome
    text: str = ""


def as_action_result(value: object) -> "ActionResult | None":
    """把一次动作回调的返回值**规整**成 ``ActionResult | None``。

    为什么要这么一层（2026-09-28，随 `ActionSpec.on_click` / `HotkeySpec.callback` 扩宽
    同批落）：返回值从插件代码走到宿主，中间隔着好几个 ``lambda`` —— 少写一个 ``return``
    就会漏出**别的东西**。本仓实测到的两种：

    - ``Signal.emit(...)`` 返回 ``True``（不是 ``None``！panel 里合成详情入口那两处
      ``lambda pid=...: self.detailRequested.emit(pid)`` 就踩在这儿）；
    - ``(a(), b())`` 这种「逗号表达式」返回元组（面板底部栏「⋯ 更多」那两处用它串两件事）。

    宿主侧若照直读 ``result.text``，插件的一个笔误就变成事件处理里的一次
    ``AttributeError`` —— 长相是「点了没反应 ＋ 日志里一句莫名其妙的错」，最难自查。

    故在宿主的**入口**做掉：不是 ``ActionResult`` 也不是 ``None`` 的，记一条 warning
    （点名实际类型）并当作「这次没话说」。判 ``isinstance`` 而不是鸭子类型 —— 这里要问的
    是「是不是我们那个冻结值对象」，不是「有没有 ``.text``」。
    """
    if value is None or isinstance(value, ActionResult):
        return value
    log.warning(
        "动作回调返回了 %s，既不是 ActionResult 也不是 None —— 按「没话说」处理（%r）",
        type(value).__name__, value,
    )
    return None
