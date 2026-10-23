"""shell/placement：**窗口该落在屏幕哪儿的唯一实现**（此前散在六处、各写一份兜底）。

调用方只声明两件事：**意图**（落在哪一类位置）与**锚**（贴着谁）。几何算式、认屏、钉屏、
落位后的核对，全在本模块一处。

意图（:class:`Intent`）：

- :attr:`Intent.BELOW_ICON` —— 贴着菜单栏图标：主面板 / 设置窗 / 详情页浮层卡。
  下方优先、放不下翻上方、夹进可用区。
- :attr:`Intent.TOP_RIGHT` —— 可用区右上角：提示浮层与常驻浮层（多条纵向叠放）。
- :attr:`Intent.CENTER_TOP` —— 水平居中、垂直落在可用高度的 :data:`CENTER_TOP_RATIO`
  处：交互浮层（一键输入倒计时卡）。它要注意力，缩在屏角不合适。
- :attr:`Intent.CENTER` —— 所在屏可用区正中：独立窗体态的详情页。

**锚只有一个可靠来源：那一次点击。** 点菜单栏图标那一刻，光标必然在图标上
（:func:`anchor_at_click`）—— 这比回读图标窗口的矩形可靠得多。理由两条，都是实测：

1. 一个 ``NSStatusItem`` 在**每块屏各有一枚按钮窗口**（主屏一枚、扩展屏一枚），
   回读到哪一枚与「用户点了哪块屏」无关；
2. 系统把它摆好之前，那枚窗口停在屏幕外的初始位（``(0, -37)`` 一类的读数），
   换算出来是一个**没有任何屏包含它**的假锚 —— 面板于是被夹到屏幕边沿。
   （2026-09-29：长跑实例读到 ``(0, 969)``、全新实例读到 ``(1085, 13)``，同一份代码。）

**认不出屏，不再静默落屏角。** 此前每一处都自己兜底一份，而那份兜底算出来必然收敛到
屏幕角或 ``(0, 0)``（「认不出屏就当整块屏」的那个矩形是其中最直接的一个）—— 这就是
「所有弹窗都在左上角」的来源。现在只有一条规矩：拿不到屏就用窗口自己那块屏，再拿不到
就**原地不动**并记一条警告，绝不猜一个屏角。

**落位必须在 ``show()`` 之后，落完要核对。** 窗口显示前 ``move()`` 只写进 Qt 自己的
账本，原生窗口建出来时并不按它摆；而 ``QWidget.move()`` 在「Qt 记的坐标已等于目标」时
**直接返回** —— 两边一旦分叉，之后再 ``move()`` 多少次都修不回来（用户看到窗口在角落，
代码却以为自己摆对了，于是每一轮「修定位」都修在算式的层上）。故：

- :func:`place_window` 在原生窗口已经存在时**再走一次原生** ``setFrameOrigin``，绕开
  Qt 那道短路；show 之前先摆一次（避免在默认位闪一帧），show 之后再摆一次（这一次才
  真的落到原生窗口上）。
- :func:`verify_placement` 回读**原生** frame 与目标比对，不一致就原生强制一次并记日志
  （``"refixed"``）；强制之后仍不一致才 ``"mismatch"`` —— 那是要用户带日志来报的那种。
- :func:`verify_on_screen` 是上面那条的**独立眼**：它问的是**窗口服务器**（用户所见），
  不是自己刚写进去的那个值。``NSWindow.frame()`` 与窗口服务器会分叉，而只回读前者
  **结构上不可能**发现分叉（2026-10-14 定案的那次故障就是这样恒判通过）。它**只报不救**：
  这类分叉在进程内没有任何手段改得回来（2026-10-14 真机把十种手段逐条试过，全无效且
  永久），所以别在这里长出「发现偏了就自己纠正」的分支 —— 看见就记一条，真正的出路是
  别在屏变化的窗口期里建窗（见 :mod:`barkit.shell.screen_settle`）。

**系统会把窗口归位到别的屏，本模块不对抗它。** macOS 在「Dock / 屏参数变化」时按
``NSScreen.mainScreen()`` 重新归位顶层窗（实测调用栈：``DockCallback → _reactToDockChanged
→ _reactToScreenInvalidation → _displayChangedSoAdjustWindows: → _screenChanged:``），
而本机那块 ``mainScreen`` **常态是虚拟屏**（与光标位置、有没有 key 窗只是相关，别当因果）。

曾试过「落位后 400ms 再核对一次，被搬走就拉回来」（:data:`RECHECK_DELAY_MS` ＋
``_arm_screen_recheck``）。两个后果都是坏账，**2026-10-09 已删**：

1. 拉回来的那一下本身就是用户看见的一次跳屏；
2. 同族的弹层（``QComboBox`` 的列表是**独立 QNSPanel 顶层窗**）**每拉回一次系统就归位
   一次** —— 真机上表现为下拉在两块屏之间弹跳 10 下。六条抢回手段（事前布置 / 事后
   move / ``orderFront_`` / ``CanJoinAllSpaces`` / cascade point / level 降档）实测全部
   无效，且真机日志里那条延迟复核**一次都没触发过**（纯负担）。

**出路是别弹独立窗口**：选项少的取值控件用 ``ui_kit.ChoiceBar``（摊在行里，系统无从
归位）。别再往这里加对抗代码。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QCursor, QGuiApplication, QScreen

from barkit.shell.design import metrics

log = logging.getLogger(__name__)


#: 交互浮层落在可用高度的这个比例处（不是正中间 —— 正中间会压住用户正在看的那一行）。
CENTER_TOP_RATIO = 0.30

#: 贴图标时，卡片与图标之间留的缝。
ANCHOR_GAP = 4

#: 核对的容差（px）：原生读数与 Qt 读数在缩放屏上会差个零点几像素，不必当分叉。
VERIFY_TOLERANCE = 2


class Intent(str, Enum):
    """窗口想落在哪一类位置（四档，互不重叠）。"""

    BELOW_ICON = "below_icon"
    TOP_RIGHT = "top_right"
    CENTER_TOP = "center_top"
    CENTER = "center"


@dataclass(frozen=True)
class Anchor:
    """贴图标落位的锚：图标矩形 ＋ 它所在的那块屏。

    两项**同生共死**（见 :meth:`of_rect`）：只有矩形、认不出屏的锚没有意义 —— 那正是
    此前把面板夹到屏幕边沿的那半份输入。
    """

    rect: QRect
    screen: "QScreen"

    @classmethod
    def of_rect(cls, rect: "QRect | None", screen: "QScreen | None" = None) -> "Anchor | None":
        """由矩形拟一个锚；**认不出屏就不给**（返回 ``None``，调用方走退路）。

        ``screen`` 是「矩形该属于哪块屏」的提示（来自原生窗口所属屏这类权威读数）；
        它与矩形对不上时按矩形自己的中心重新认一次。对不上就意味着两个读数里有一个
        是错的（多屏下一枚图标有多枚窗口时必现），此时以**能包含这个矩形**的那块屏为准。
        """
        if rect is None or rect.isNull():
            return None
        scr = screen
        if scr is None or not scr.geometry().contains(rect.center()):
            scr = QGuiApplication.screenAt(rect.center())
        if scr is None:
            return None
        return cls(QRect(rect), scr)


def _menu_bar_height(screen: "QScreen", fallback: int) -> int:
    """这块屏菜单栏的高度（Qt y 方向）。

    优先问「可用区比几何少掉的那一条」（有菜单栏的屏就是这个）；问不出来（不带菜单栏的
    扩展屏，两处相等）用图标自身的高度兜底 —— 菜单栏那一格本来就和图标一样高。
    """
    geo, ag = screen.geometry(), screen.availableGeometry()
    reserved = ag.top() - geo.top()
    return reserved if reserved > 0 else max(1, int(fallback))


def anchor_at_click(point: "QPoint | None", size: "tuple[int, int]") -> "Anchor | None":
    """点菜单栏图标那一刻的锚：由**光标点**推出图标矩形。

    点图标时光标必然落在图标上（这是这条路唯一的依据）。横向取光标 x 作图标中心，纵向
    取所在屏菜单栏那一条。**不去回读图标窗口的坐标** —— 那个读数多屏下不可靠，理由见
    模块 docstring。

    ``point`` 不落在任何一块屏上时返回 ``None``（调用方走退路，不猜）。
    """
    if point is None:
        return None
    scr = QGuiApplication.screenAt(point)
    if scr is None:
        return None
    w = max(1, int(size[0]))
    h = max(1, int(size[1]))
    geo = scr.geometry()
    rect = QRect(int(point.x()) - w // 2, geo.top(), w, _menu_bar_height(scr, h))
    return Anchor(rect, scr)


def anchor_of_status_item(status_item) -> "Anchor | None":
    """菜单栏图标的锚 —— 给**没有点击点**的来路用（热键、框架命令开面板）。

    先试原生按钮窗口矩形（:meth:`StatusItem.button_rect_qt` 自带「落在某块屏里」的
    校验），不合法再退到「光标所在的菜单栏那一格」。两条都拿不到返回 ``None``。
    """
    if status_item is None or not getattr(status_item, "available", False):
        return None
    rect = scr = None
    try:
        rect = status_item.button_rect_qt()
        scr = status_item.button_screen()
    except Exception:  # noqa: BLE001 —— 量不到锚不该让开面板这件事失败
        log.debug("状态项锚点读取失败（改退到光标所在屏）", exc_info=True)
    anchor = Anchor.of_rect(rect, scr)
    if anchor is not None:
        return anchor
    try:
        size = status_item.button_size()
    except Exception:  # noqa: BLE001
        size = (24, 24)
    return anchor_at_click(QCursor.pos(), size)


def _screen_alive(scr) -> bool:
    """这块 ``QScreen`` 还是活的吗。

    屏参数一变，此前取到的那枚 ``QScreen`` 包装会变成**死对象**：底下那枚 C++ ``QScreen``
    已被 Qt 销毁，往上碰任何一处（``geometry()`` / ``availableGeometry()`` …）都抛
    ``RuntimeError: libshiboken: Internal C++ object (PySide6.QtGui.QScreen) already deleted``。
    2026-10-14 复现两次，产品路径是「屏变化后再开面板」那条链。故**用之前先问一句**。
    """
    if scr is None:
        return False
    try:
        scr.geometry()
        return True
    except Exception:  # noqa: BLE001 —— 死对象一碰就抛，任何异常都当「死了」
        return False


def resolve_anchor_screen(anchor: "Anchor | None") -> "QScreen | None":
    """锚那块屏**此刻**的 ``QScreen``；缓存那一枚已死就按锚矩形重新认一次。

    锚由「那一次点击」定下、会跨屏变化一直挂着（面板收起再展开、设置窗 / 详情页从面板里
    开出来都读它），而 ``QScreen`` 是会被屏参数变化作废的 —— 缓存锚屏等于缓存一个随时可能
    变成死对象的指针。这里统一收口：活的就用，死的按**锚矩形自己的中心**重认（与
    :meth:`Anchor.of_rect` 同一条规矩：能包含这个矩形的那块屏才算）。重认不出来给 ``None``
    —— 调用方走退路，不猜一块屏。
    """
    if anchor is None:
        return None
    if _screen_alive(anchor.screen):
        return anchor.screen
    log.debug("锚上缓存的 QScreen 已随屏变化失效，按锚矩形重新认屏")
    return QGuiApplication.screenAt(anchor.rect.center())


def resolve_screen(
    window,
    intent: Intent,
    anchor: "Anchor | None" = None,
    screen: "QScreen | None" = None,
) -> "QScreen | None":
    """这一档意图该落在哪块屏（**唯一的认屏规矩**，四条意图按优先级各取一条链）。

    - 显式给了 ``screen``（提示栈记住的那块屏）就用它 —— 重排不许换屏；
    - 给了 ``anchor`` 就用锚那块屏（面板 / 设置窗 / 详情页的开法由用户那一下点击定）——
      经 :func:`resolve_anchor_screen` **现解**，锚上缓存的那枚随屏变化失效了就重认；
      重认不出来则继续往下走，不把一块死屏交给调用方；
    - 其余按意图偏好：钉屏角 / 居中类跟随**光标所在屏**（用户正看着哪块就落哪块），
      贴图标类（没有锚时）跟随窗口自己那块屏；
    - 全拿不到才退回主屏，并且**吵一声** —— 这一档落在哪块屏上不再是用户点的那块。
    """
    if screen is not None:
        return screen
    if anchor is not None:
        anchor_scr = resolve_anchor_screen(anchor)
        if anchor_scr is not None:
            return anchor_scr
    if intent in (Intent.TOP_RIGHT, Intent.CENTER_TOP):
        by_cursor = QGuiApplication.screenAt(QCursor.pos())
        if by_cursor is not None:
            return by_cursor
    own = window.screen()
    if own is not None:
        return own
    by_cursor = QGuiApplication.screenAt(QCursor.pos())
    if by_cursor is not None:
        return by_cursor
    primary = QGuiApplication.primaryScreen()
    if primary is not None:
        log.warning(
            "认不出窗口该落哪块屏，退回主屏 —— 这次落位可能不在用户正看的那块屏上"
        )
    return primary


def target_top_left(
    window,
    intent: Intent,
    *,
    anchor: "Anchor | None" = None,
    screen: "QScreen | None" = None,
    above: int = 0,
    inset: int = 0,
) -> "tuple[QPoint, QScreen | None]":
    """算出窗口左上角该在哪 —— **只算，不动窗口**。

    :param above: 同侧已经占掉的竖向空间（提示栈用：排在前面的那些块高 ＋ 间距）。
    :param inset: 窗口边框与卡片之间的留白（面板的阴影余量）。窗口比卡片大一圈，
        落位按**卡片**算、再把这一圈让出去；夹取仍按整个窗口夹。
    """
    scr = resolve_screen(window, intent, anchor=anchor, screen=screen)
    if scr is None:
        # 认不出屏：**原地不动**。此前这一格各写各的兜底，算出来都收敛到屏角 ——
        # 那才是「所有弹窗都在左上角」的直接来源。
        return window.pos(), None

    ag = scr.availableGeometry()
    w, h = window.width(), window.height()
    cw, ch = max(1, w - 2 * inset), max(1, h - 2 * inset)

    if intent is Intent.BELOW_ICON:
        rect = anchor.rect if anchor is not None else None
        if rect is not None:
            card_x = rect.center().x() - cw // 2
            below = rect.bottom() + 1 + ANCHOR_GAP
            card_y = (
                below if below + ch <= ag.bottom() + 1 else rect.top() - ANCHOR_GAP - ch
            )
        else:
            # 没有锚（非 macOS 回退 / 图标不可用）：靠右上角起一块，与 TOP_RIGHT 同侧。
            card_x = ag.right() + 1 - cw
            card_y = ag.top() + ANCHOR_GAP
    elif intent is Intent.TOP_RIGHT:
        card_x = ag.x() + ag.width() - cw - metrics.OVERLAY_EDGE_GAP
        card_y = ag.y() + metrics.OVERLAY_EDGE_GAP + above
    elif intent is Intent.CENTER_TOP:
        card_x = ag.x() + (ag.width() - cw) // 2
        card_y = ag.y() + int(ag.height() * CENTER_TOP_RATIO) + above
    else:  # Intent.CENTER：按**整窗**居中（带标题栏的窗体要连标题栏一起居中）
        frame = window.frameGeometry()
        frame.moveCenter(ag.center())
        return frame.topLeft(), scr

    x = card_x - inset
    y = card_y - inset
    x = max(ag.left(), min(x, ag.right() + 1 - w))
    y = max(ag.top(), min(y, ag.bottom() + 1 - h))
    return QPoint(x, y), scr


def place_window(
    window,
    intent: Intent,
    *,
    anchor: "Anchor | None" = None,
    screen: "QScreen | None" = None,
    above: int = 0,
    inset: int = 0,
) -> "tuple[QPoint, QScreen | None]":
    """算出来 ＋ 钉屏 ＋ 落到窗口上（**唯一出口**）。返回 ``(目标点, 目标屏)``。

    钉屏只在窗口**还没显示**时才做：对已可见窗口重复 ``setScreen`` 会重建它的原生体
    （折叠动画期间表现为闪烁，且 show() 时设的 NSWindow.level 会丢），而重建出来的新窗
    在屏模型不完整的那几秒里会被窗口服务器落在 ``(0,0)`` —— 那正是「落在左上角」本身。

    原生窗口已经存在时**再走一次原生** ``setFrameOrigin``（:func:`set_native_top_left`）：
    ``QWidget.move()`` 在「Qt 记的坐标已等于目标」时直接返回，光靠它修不回分叉。
    """
    pt, scr = target_top_left(
        window, intent, anchor=anchor, screen=screen, above=above, inset=inset
    )
    ok = _apply(window, pt, scr)
    # 落位没落到原生窗口上必须**出声**：这一步静默的后果是 Qt 账本写着目标点、
    # 而窗口停在它诞生时的默认位（屏幕左上角），且之后 move() 会因「账本已等于目标」
    # 直接短路 —— 分叉再也没人能修回来（2026-09-30 真机）。
    # 窗口**还没显示**时不报：那时原生窗口多半还没建，推不上去是正常的，
    # 真正作数的那一次是显示之后（各出口都已按「先显示再落位」排好）。
    if not ok and window.isVisible() and _native_ready():
        log.warning(
            "落位没落到原生窗口上：目标 %s，Qt 账本已是这里、原生窗口可能没动 —— "
            "窗口多半停在默认位置（屏幕左上角）",
            pt,
        )
    return pt, scr


def _apply(window, pt: QPoint, scr: "QScreen | None") -> bool:
    """把算好的落点真正落到窗口上（钉屏 ＋ Qt 账本 ＋ 原生）；返回原生那一步是否落上。

    **钉屏只在窗口还没显示时做。** 对**已可见**窗口重复 ``setScreen`` 会重建它的原生体，
    而重建出来的那枚新窗在「屏模型不完整」的那几秒里会被窗口服务器落在 ``(0,0)``
    —— 落位这一步于是成了「把窗钉死在屏幕左上角」的成因本身（2026-10-14 真机：只有那扇
    在屏变化窗口期被反复重落的窗被钉住，同进程另两枚只落过一次的浮层窗位置都对）。

    可见窗口跨屏本来就由 ``move()`` 自带的那条路完成（``QCocoaWindow`` 看到新几何落在
    另一块屏上会自己更新 ``QWindow.screen()``），不必在这里再钉一次。
    """
    if scr is not None and not window.isVisible() and window.screen() is not scr:
        window.setScreen(scr)
    window.move(pt)
    return set_native_top_left(window, pt.x(), pt.y())


def verify_placement(
    window, expected: QPoint, *, tolerance: int = VERIFY_TOLERANCE
) -> str:
    """回读**原生** frame 与 ``expected`` 比对；不一致就原生强制一次并记日志。

    这是「算出来的位置和屏幕上的位置不许不一致」这条不变量的守卫。返回值：

    - ``"ok"`` —— 一致（或**非原生环境**：离屏 / 非 macOS，判据不适用，不出声）；
    - ``"refixed"`` —— 不一致，已按目标原生纠正（``place_window`` 之后还会不一致，
      说明有别的东西在落位之后动了窗口，值得记一条）；
    - ``"mismatch"`` —— 纠正之后仍不一致 ⇒ 这条要用户带日志来报；
    - ``"unknown"`` —— **原生环境下却取不到窗口**，这一次落位**没有被核对**。

    ``"unknown"`` ≠ ``"ok"``：把它当「通过」就是 2026-09-30 那条坏法的原样 ——
    面板原生 frame 在 ``(0,0)``、日志写着落位 ``(771,42)``、核对一声不吭。
    判据不适用必须**看得见**，不许伪装成通过。
    """
    actual = native_top_left(window)
    if actual is None:
        if not _native_ready():
            return "ok"
        log.warning(
            "落位核对：取不到这个窗口的原生读数，本次落位没有被核对（目标 %s）—— "
            "它此刻在哪无从确认，若它停在屏幕左上角，就是这条",
            expected,
        )
        return "unknown"
    if (
        abs(actual.x() - expected.x()) <= tolerance
        and abs(actual.y() - expected.y()) <= tolerance
    ):
        return "ok"
    if set_native_top_left(window, expected.x(), expected.y()):
        after = native_top_left(window)
        same_after = after is not None and (
            abs(after.x() - expected.x()) <= tolerance
            and abs(after.y() - expected.y()) <= tolerance
        )
        if same_after:
            log.warning(
                "落位核对：窗口实际在 %s，与目标 %s 不一致，已原生纠正",
                actual, expected,
            )
            return "refixed"
    log.warning(
        "落位核对失败：窗口实际在 %s，目标 %s，原生纠正也没落上",
        actual, expected,
    )
    return "mismatch"


def window_server_top_left(window) -> "QPoint | None":
    """这扇窗**在窗口服务器里**的左上角（Qt 全局坐标）—— 也就是**用户所见**的位置。

    与 :func:`native_top_left` 互相独立：那口读 ``NSWindow.frame()``（AppKit 自己记的
    账），这口问窗口服务器。两条都给真值、却可能分叉，而用户看见的是**这一口** —— 见
    :func:`verify_on_screen`。非原生 / 还没上屏给 ``None``。
    """
    native = _native()
    return native.native_top_left_on_screen(window) if native is not None else None


def verify_on_screen(window, expected: QPoint, *, tolerance: int = VERIFY_TOLERANCE) -> str:
    """用**窗口服务器**读数核对落位 —— :func:`verify_placement` 缺的那只独立眼。

    为什么必须有这一口：``verify_placement`` 回读的是 ``NSWindow.frame()``，也就是它自己
    刚刚 ``setFrameOrigin_`` **写进去的那个值** —— 写读互为逆运算，对这类故障**恒判通过**。
    2026-10-14 定案的形态正是如此：``NSWindow.frame()`` 报 ``(934,42)``、日志写着「落位
    通过」，而窗口服务器把这扇窗摆在 ``(0,0)``、用户看到的也在 ``(0,0)``。

    返回值：

    - ``"ok"`` —— 窗口服务器上就在 ``expected``（容差内），或**非原生环境**（判据不适用）；
    - ``"mismatch"`` —— 窗口服务器上不在 ``expected`` ⇒ 这扇窗**没被真正摆到那儿**。
      **只报不救**：这类分叉在进程内救不回来（2026-10-14 真机把十种手段逐条试过，全无效
      且永久），所以调用方看见这一档只该**记一条**，别在这里推窗 —— 真正的出路是不要在
      屏变化的窗口期里建窗（见 :mod:`barkit.shell.screen_settle`）；
    - ``"unknown"`` —— 窗口服务器里暂无这枚窗（**还没上屏 / 已隐藏**）。这是判据此刻不
      适用，**不是**「确认错了」，故**不出声**；出声的只有下面 ``mismatch`` 那一档。

    **读之前要留沉降时间**：窗口服务器对移动的读数会滞后（实测约 0.35s 才稳定），刚写完就
    读会把「还没生效」误判成分叉。调用方一律延迟调用（见面板的 ``_report_placement_on_screen``）。
    """
    actual = window_server_top_left(window)
    if actual is None:
        return "ok" if not _native_ready() else "unknown"
    if (
        abs(actual.x() - expected.x()) <= tolerance
        and abs(actual.y() - expected.y()) <= tolerance
    ):
        return "ok"
    log.warning(
        "落位核对（窗口服务器）：窗口实际在 %s，目标 %s —— 这扇窗没被真正摆到目标处",
        actual, expected,
    )
    return "mismatch"


def _native():
    """原生桥接模块（非 macOS 返回 ``None``）；延迟 import 避免 shell 层循环依赖。"""
    try:
        from barkit.shell import macos_window

        return macos_window
    except Exception:  # noqa: BLE001 —— 拿不到就没有原生读数，按「判据不适用」处理
        return None


def _native_ready() -> bool:
    """**是不是真的跑在原生窗口下**（离屏 / 非 macOS 为假）。

    「取不到原生读数」有两种，必须分开：跑在离屏下是**判据不适用**（不该出声，
    否则整仓测试刷屏）；跑在真窗口下却取不到窗是**落位没被核对**（必须出声）。
    """
    native = _native()
    if native is None:
        return False
    try:
        return bool(native._native_enabled())
    except Exception:  # noqa: BLE001
        return False


def native_top_left(window) -> "QPoint | None":
    """窗口**原生体**此刻的真实左上角（Qt 全局坐标）；非原生 / 取不到给 ``None``。

    读的是 ``NSWindow.frame()``（进程内、与 Qt 的自述无关）—— Qt 的 ``pos()`` 只是
    它自己的账本，两套都可能与用户所见不同，故以这一口为准。

    （窗口服务器 ``CGWindowListCopyWindowInfo`` 对本进程的窗口**也给真值**，2026-09-30
    自证过；它适合**进程外**取证，进程内就用这一口。）
    """
    native = _native()
    return native.native_top_left(window) if native is not None else None


def set_native_top_left(window, x: int, y: int) -> bool:
    """绕过 Qt 把原生窗口挪到 ``(x, y)``（Qt 全局坐标）；非原生 / 取不到窗返回 ``False``。"""
    native = _native()
    return bool(native.set_native_top_left(window, x, y)) if native is not None else False
