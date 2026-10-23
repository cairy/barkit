"""shell/hotkey_feedback：热键**触发之后**的反馈分派（成功按条目选档、失败一律通知）。

**三种成功反馈，标签即行为**（见 ``hotkey_catalog.FEEDBACK_*``，没有藏着掖着的语义）：

- ``none``（不通知）—— 成功时什么都不弹。选了它**真的就什么都不显示**；
- ``overlay``（轻浮层）—— 成功时弹一个 2 秒自消、不激活 app 的轻提示；
- ``notify``（系统通知）—— 成功时发一条系统通知。

**失败不受上面那个选择影响** —— 失败一律发系统通知：失败必须被看见，且通知会
留在通知中心，用户切回来时还在。没有「失败也不想被通知」这个需求。

**为什么成功要逐条选**：热键按下去的时候面板多半是关着的，而 ``ActionResult`` 今天的
呈现面只有面板里那一行 —— 结果根本送不到用户眼前（``HotkeySpec`` 的注释里点名
过这条缺口）。三类来源的补齐手段不同：框架 / 组件动作执行完自身就有可见结果
（面板弹出、识别浮层），默认 ``none`` 不补；自建跑的是一条静默后台命令，默认
``notify``。每种方式都**字面即行为**，不允许「写着不通知实际弹轻浮层」。

**记录最近一次结果**（:meth:`last_of`）：快捷键页那条详情屏要显示「上次触发」，
而热键触发时设置窗通常是关着的 —— 只能事后给它看。只存内存、不落盘：
跨重启的「上次结果」没有意义（设备状态早变了）。
"""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtWidgets import QLabel

from barkit.contribution.specs import ActionOutcome, ActionResult, as_action_result
from barkit.shell.hotkey_catalog import (
    FEEDBACK_NONE,
    FEEDBACK_NOTIFY,
    FEEDBACK_OVERLAY,
)
from barkit.shell.ui_kit import apply_text_role

log = logging.getLogger(__name__)

#: 轻浮层的停留时长（毫秒）。2 秒 = 读完一行「XXX 已完成」的时间；
#: 再短就成了闪一下，再长就在挡路（浮层是置顶的）。
FLASH_MS = 2000


def outcome_of(result: "ActionResult | None") -> "tuple[bool, str]":
    """一次动作回调的返回值 → ``(ok, detail)``（喂给 :meth:`HotkeyFeedback.report`）。

    ``None`` ＝ **老写法**（回调只管做事、不写回执）⇒ 按「成功、无明细」处理，与扩宽之前
    的表现**逐字相同** —— 现存回调一个字都不必改（这是纯扩宽，零破坏）。

    返回 ``ActionResult`` 时：``FAILED`` 与 ``PARTIAL`` 都算「必须被看见」，故 ``ok=False``
    （``report`` 对失败一律发系统通知，不受逐条档位影响）—— ``PARTIAL`` 是「做了一半」，
    用户同样看不出来，归到这一档是刻意的。今天仓里**还没有** ``PARTIAL`` 的生产方，真出现
    时再决定要不要给它单独一句标题（现在它会得到「没跑成」那套标题）。

    入参先过 ``as_action_result``：返回值是**插件**给的，少写一个 ``return`` 就会漏出
    别的东西（实测 ``Signal.emit`` 返回 ``True``）—— 在这里变成「没话说」而不是崩溃。
    """
    result = as_action_result(result)
    if result is None:
        return True, ""
    if result.outcome in (ActionOutcome.FAILED, ActionOutcome.PARTIAL):
        return False, result.text or ""
    return True, result.text or ""


def wrap_hotkey_callback(
    cb: "Callable[[], ActionResult | None]",
    *,
    name: str,
    feedback: "HotkeyFeedback",
    title_of: "Callable[[], str]",
    mode_of: "Callable[[], str]",
) -> "Callable[[], None]":
    """把一条热键回调包成「跑完就报」，返回可直接登记的**无参**回调。

    为什么做成模块级工厂（而不是留在宿主那个私有方法里）：宿主那条链
    （``shell/app_shell``）牵起 PyObjC，Windows 上 import 不到 ⇒ 判定留在那儿就**只能靠
    真机验**。判定收到这里 ⇒ 本机可测；宿主那边只剩「把 ``title_of`` / ``mode_of`` 两个
    读取口接上」这几行。

    ``title_of`` / ``mode_of`` 是**读取口而不是值**：标题随条目改键位变化、档位是用户在
    设置里逐条选的，两者都该在**触发那一刻**读，不能在登记时定死。

    回调抛异常不再静默消失：捕获后按失败上报（失败一律发系统通知），否则「按了没反应、
    又没有任何提示」最难自查。
    """

    def _wrapped() -> None:
        try:
            result = cb()
        except Exception:  # noqa: BLE001 —— 一条热键回调炸了不该把整条热键链带崩
            log.exception("热键 %s 回调异常", name)
            ok, detail = False, "触发时出错"
        else:
            ok, detail = outcome_of(result)
        feedback.report(
            name=name, title=title_of(), ok=ok, detail=detail, mode=mode_of(),
        )

    return _wrapped


class HotkeyFeedback:
    """把一次触发的结局送到**当下看得到的地方**。两个出口都是注入的：本类不认识
    ``NotificationHub``，也不自己开窗口 —— 装配器给什么就用什么，没给就降级。"""

    def __init__(self, *, notify=None, flash=None) -> None:
        self._notify = notify
        self._flash = flash if flash is not None else default_flash
        self._last: dict[str, tuple[bool, str]] = {}

    def report(
        self,
        *,
        name: str,
        title: str,
        ok: bool,
        detail: str = "",
        mode: str = FEEDBACK_NONE,
    ) -> None:
        """报一次结局。

        ``mode`` 只管**成功**那条（见模块头）：``none`` 什么都不做、
        ``overlay`` 弹轻浮层、``notify`` 发系统通知。失败**一律**发系统通知，
        不受 ``mode`` 影响。
        """
        self._last[name] = (ok, detail)
        if not ok:
            self._send(name, f"{title} 没跑成", detail)
            return
        if mode == FEEDBACK_NOTIFY:
            self._send(name, f"{title} 已执行", detail)
        elif mode == FEEDBACK_OVERLAY:
            self._flash(f"{title}：{detail}" if detail else title)
        # FEEDBACK_NONE —— 成功时不显示任何提示（字面即行为）

    def last_of(self, name: str) -> "tuple[bool, str] | None":
        """这一条最近一次触发的结果（没触发过给 None）。"""
        return self._last.get(name)

    def _send(self, name: str, title: str, body: str) -> None:
        """``name`` 一并带出去：通知按它去重（连按两次同一条要有两条回执）。"""
        if self._notify is None:
            return
        try:
            self._notify(name, title, body)
        except Exception:  # noqa: BLE001 —— 通知失败不该把热键回调带崩
            pass


def default_flash(text: str) -> None:
    """默认轻浮层：一行字、2 秒自散。

    延迟导入 ``Overlay``：它带原生窗口保险（置顶层级 / 不激活 app），import 即
    牵起 ``macos_window`` 那一条链；本模块在离屏 / 非 macOS 下也要能被 import。

    正文走 ``overlay_body`` 这一档（**不是**随手写个角色名）：角色名写错
    ``apply_text_role`` 会**当场抛 KeyError**，而这条路径是热键回调里最靠里的一段 ——
    抛出去的表现是「按了键、日志里一条 traceback、屏幕上什么都没弹」（2026-09-23 的
    实测现场：这里写的 ``"body"`` 不在角色表里，于是「轻浮层」这个选项**从来没弹过**）。

    落位**不在这里定**：走 ``Overlay.show_toast``（框架统一放右上角、多条叠放并自动
    重排，见 ``shell/overlay.py``）—— 提示出现在哪儿是框架的事，本模块只管那一句话。
    引用也由那边（提示栈）持有：退场即出栈，本函数发后不管、不留残引用。
    """
    from barkit.shell.overlay import Overlay

    overlay = Overlay(None, duration_ms=FLASH_MS, draggable=False)
    label = QLabel(text)
    label.setWordWrap(True)
    apply_text_role(label, "overlay_body")
    overlay.content_layout.addWidget(label)
    overlay.adjustSize()
    overlay.show_toast()
