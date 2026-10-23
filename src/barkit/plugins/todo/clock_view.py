"""todo/clock_view：待办提醒的**运行期面**（翻页钟面 ＋ 浮层 ＋ 主线程控制器）。

- ``FlipDigit`` / ``FlipClock``：纯自绘的翻页钟面（split-flap 两阶段折叠），颜色全取
  ``shell/design/tokens``，**不写死颜色、不拼 QSS**（门禁 ``test_no_hardcoded_colors`` /
  ``test_no_inline_stylesheet`` 扫 ``plugins/**``）。
- ``ClockOverlay``：继承框架 ``Overlay``，落位走框架 ``show_docked``（**右上角常驻**，
  与提示栈共用一列但让提示从其下方排）；基类 ``draggable=False``（拖动手势自己实现）。
  交互分工：**左键拖动**（拖到哪儿停哪儿，脱离钉位后 redock 不再拽回）；**关闭走双击或
  右键**（与拖动互不打架，可在设置里选）。**无投影**。
- ``ClockController``：在主线程接事件总线、增删改浮层 —— 插件 ``tick()`` 跑在 worker
  线程算状态，经事件桥回主线程，这里只做 UI 增删，**不私起后台时钟 / 线程**。浮层内的
  实时节拍由 ``FlipClock`` 自带的 250ms ``QTimer`` 驱动（随浮层生命周期、非后台轮询，
  已进 ``test_architecture`` 的插件时钟白名单）。

**浮层分两个区**（合并方案 §3.1）：上面是钟面（三型之一），下面是**结构化的条目行**
—— 一行由「状态点 ＋ 标题（＋补充文本）＋ 右侧时刻 ＋ 可选动作」构成。旧翻页钟那一版
的 ``texts: list[str]`` 是纯字符串：没 id、没状态、点不了，装不下一条待办；行变成结构
化之后，「完成」「稍后」两枚按钮才有地方挂（§3.3：**按条目配**，不配就不画那枚按钮）。

**钟面数值只有一个判定者**：``FlipClock`` 自己（构造即对齐，之后按节拍往前走）。
``ClockOverlay.apply`` 那条链只交「该不该显示 / 哪一型 / 尺寸 / 行内容」，倒计时与
正计时另交一份**锚点**（是事实、不是算好的显示值）—— 两条 1s 节拍各算一遍同一个
「此刻」，相位差落进 300ms 翻页动画就会把同一个数字翻两次。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING

from PySide6.QtCore import (
    Property,
    QPoint,
    QPropertyAnimation,
    QRectF,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPainterPath,
    QPen,
    QTransform,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from barkit.plugins.todo.scheduler import (
    ACTION_DONE,
    ACTION_SNOOZE,
    FACE_CLOCK,
    FACE_COUNTDOWN,
    FACE_COUNTUP,
    Row,
    fmt_mmss,
)
from barkit.shell import styles
from barkit.shell.design import metrics
from barkit.shell.overlay import (
    DISMISS_CLOSED,
    Overlay,
    live_docked,
    live_toasts,
    overlay_alive,
)
from barkit.shell.ui_kit import apply_text_role, tool_button

if TYPE_CHECKING:
    from barkit.plugins.todo.plugin import Plugin

log = logging.getLogger(__name__)

#: 拖动判定：按下后位移超过这一像素才算「在拖」，否则视为点按。
_DRAG_THRESHOLD_PX = 4
#: 关闭动画：滑出 ＋ 淡出后 dismiss 的时长。
_CLOSE_SLIDE_MS = 160
#: 翻页动画时长（毫秒）。整段拆成「上半下折 / 下半下翻」两半，各占一半。
_FLIP_MS = 300
#: 浮层内实时节拍：每 250ms 核对一次墙钟，让秒针精确卡在秒边界、不滞后系统时钟。
_LIVE_TICK_MS = 250

#: 两个动作按钮的文案。**文案与动作名分开**：动作名（``done`` / ``snooze``）是与插件
#: 之间的约定，按钮上写什么只影响观感 —— 「稍后几分钟」由插件那一侧的常量决定，
#: 浮层不去 import 插件的业务常量（那会把 UI 面绑到插件的配置上）。
_ACTION_LABELS = {ACTION_DONE: "完成", ACTION_SNOOZE: "稍后"}

#: 行首那个状态点：空心 ＝ 未完成（这一行能勾）、实心 ＝ 纯展示（不占面板进度分母）。
_DOT_ACTIONABLE = "○"
_DOT_PLAIN = "●"


def _tokens() -> dict:
    return styles.TOKENS[styles.current_theme()]


def _text_top_margin(font: QFont) -> int:
    """一行文字在行里的**上边距**（像素）：把这一档字体的行盒摆进行高正中。

    行高是 ``metrics.BTN_INLINE``，某档字体的行盒是 ``QFontMetricsF.height()``
    （13px→15.3、12px→14.1、11px→12.9），差值的一半放在上面 —— 配 ``AlignTop`` 用，
    文字顶就只由这条边距决定、与控件盒高无关（见 ``_RowWidget`` 的落位说明）。

    **为什么不用墨迹（glyph ink）算**：``tightBoundingRect`` 给的墨迹带把汉字下方的空白
    也算进去了，按它居中会整体抬高 1.25~1.5px（2026-10-14 真机截图实测）；而「行盒一半」
    这版在 cocoa 上实测残差 ≤ 0.5px、且没有系统性偏向。这三代都试过、前两代都删了 ——
    别再往「手算一条公共基线」的方向退。
    """
    return max(0, round((metrics.BTN_INLINE - QFontMetricsF(font).height()) / 2))


class FlipDigit(QWidget):
    """单个翻页数字：上下两半 ＋ 中线；换数时**整块半圆卡**绕中线折叠（split-flap）。

    与「只缩放文字」不同，这里把半圆卡（背景 ＋ 边框 ＋ 数字一起）作为刚体绕中线铰链
    做纵向透视折叠：上半旧数先向下折（scale 1→0）露出下半已是新数的静态层；下半新数
    再向下翻（scale 0→1）盖住旧数。
    """

    def __init__(
        self, tier: str, parent: "QWidget | None" = None, digit: str = "0"
    ) -> None:
        super().__init__(parent)
        self._m = metrics.FLIPCLOCK[tier]
        self.setFixedSize(self._m["card_w"], self._m["card_h"])
        self._radius = self._m["radius"]
        self._font_px = self._m["font"]
        # 初值由建卡的人给：**这张卡出生时就显示正确的数**，不靠事后喂一次再翻过来
        # （那样「构造」＋「第一次喂值」会被看成两次翻页）。
        self._cur = str(digit)
        self._prev = str(digit)
        self._p = 1.0  # 翻转动画进度（1 = 静止）
        self._anim = QPropertyAnimation(self, b"flip")
        self._anim.setDuration(_FLIP_MS)
        # 必须给定起止值：否则 Qt 报「starting an animation without end value」，且
        # 属性 _p 不推进 → 翻页动画根本不会发生（看起来就是「不会翻」）。
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.finished.connect(lambda: self._finish())

    #: 翻转动画属性（驱动 ``paintEvent`` 重绘），不设 notify —— 由动画逐帧写值。
    def _get_flip(self) -> float:
        return self._p

    def _set_flip(self, v: float) -> None:
        self._p = v
        self.update()

    flip = Property(float, _get_flip, _set_flip)

    def set_value(self, digit: str) -> None:
        """换数（同值 = 什么都不做）。**守卫只看值，不看动画跑到哪了。**

        曾经的写法是「同值 **且** 上一段动画已结束（``_p >= 1.0``）才跳过」——那等于把
        「同值不动作」这件本该无条件成立的事，交给了动画时序：翻页动画 300ms，任何人
        在动画中途再喂同一个值，守卫当场失效、把已经翻好的数字**再翻一次**（``_prev =
        _cur``，数字不变、画面动了一下）。
        """
        digit = str(digit)
        if digit == self._cur:
            return
        self._prev = self._cur
        self._cur = digit
        self._anim.stop()
        self._p = 0.0
        self._anim.start()

    def _finish(self) -> None:
        self._p = 1.0
        self.update()

    def _half_path(self, top: bool, w: int, h: int, r: int) -> QPainterPath:
        """半圆卡路径：上半取上方圆角、下半取下方圆角，接缝侧（中线）为直角。

        折叠动画里只缩放、不重画路径，圆角会随折叠自然压扁，符合真实翻页观感。
        """
        hh = h / 2
        path = QPainterPath()
        if top:
            path.moveTo(0.0, hh)
            path.lineTo(0.0, r)
            path.quadTo(0.0, 0.0, r, 0.0)
            path.lineTo(w - r, 0.0)
            path.quadTo(w, 0.0, w, r)
            path.lineTo(w, hh)
        else:
            path.moveTo(0.0, hh)
            path.lineTo(0.0, h - r)
            path.quadTo(0.0, h, r, h)
            path.lineTo(w - r, h)
            path.quadTo(w, h, w, h - r)
            path.lineTo(w, hh)
        path.closeSubpath()
        return path

    def _draw_half(
        self, painter: QPainter, t: dict, digit: str, top: bool, scale: float
    ) -> None:
        """画一个半圆卡（背景 ＋ 边框 ＋ 数字），并绕中线铰链做纵向透视折叠。"""
        if scale <= 0.002:
            return  # 边沿朝前（edge-on），不可见
        w, h = self.width(), self.height()
        hh = h / 2
        painter.save()
        # 裁剪到半圆，折叠时不许溢出到另一半
        if top:
            painter.setClipRect(0, 0, w, hh)
        else:
            painter.setClipRect(0, hh, w, hh)
        # 绕中线铰链做纵向折叠：上半铰链在底边(y=hh)、下半铰链在顶边(y=hh)
        m = QTransform()
        m.translate(0.0, hh)
        m.scale(1.0, scale)
        m.translate(0.0, -hh)
        painter.setTransform(m, True)
        # 半圆卡背景 ＋ 边框（整块一起翻）
        path = self._half_path(top, w, h, self._radius)
        painter.setBrush(QColor(t["surface3"]))
        painter.setPen(QPen(QColor(t["border"]), 1))
        painter.drawPath(path)
        # 数字（整字绘制后由裁剪落到半圆；折叠时随矩形一起压扁）
        font = QFont()
        font.setPixelSize(self._font_px)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(t["text1"]))
        painter.drawText(QRectF(0.0, 0.0, w, h), Qt.AlignmentFlag.AlignCenter, digit)
        painter.restore()

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        t = _tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        hh = h / 2

        if self._p >= 1.0:
            # 静止：整卡（一笔圆角矩形，不在接缝描边）＋ 数字 ＋ 单独接缝线
            card = QPainterPath()
            card.addRoundedRect(QRectF(0, 0, w, h), self._radius, self._radius)
            painter.setBrush(QColor(t["surface3"]))
            painter.setPen(QPen(QColor(t["border"]), 1))
            painter.drawPath(card)
            font = QFont()
            font.setPixelSize(self._font_px)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor(t["text1"]))
            painter.drawText(QRectF(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, self._cur)
            painter.setPen(QPen(QColor(t["border"]), 1))
            painter.drawLine(1, hh, w - 1, hh)
            return

        p = self._p
        if p < 0.5:
            # 上半先翻：静态上半＝新数（被旧数上半盖住），旧数上半绕中线向下折(1→0)
            self._draw_half(painter, t, self._cur, top=True, scale=1.0)
            self._draw_half(painter, t, self._prev, top=False, scale=1.0)
            self._draw_half(painter, t, self._prev, top=True, scale=1 - 2 * p)
        else:
            # 下半再翻：旧数下半静态（被新数下半盖住），新数下半绕中线向下翻(0→1)盖住旧数
            self._draw_half(painter, t, self._cur, top=True, scale=1.0)
            self._draw_half(painter, t, self._prev, top=False, scale=1.0)
            self._draw_half(painter, t, self._cur, top=False, scale=2 * p - 1)
        # 接缝线盖在最上，统一观感
        painter.setPen(QPen(QColor(t["border"]), 1))
        painter.drawLine(1, hh, w - 1, hh)


class _Colon(QWidget):
    """冒号分隔点（两个圆点）。"""

    def __init__(self, tier: str, parent: "QWidget | None" = None) -> None:
        super().__init__(parent)
        self._m = metrics.FLIPCLOCK[tier]
        self.setFixedSize(self._m["colon"], self._m["card_h"])

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        t = _tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(t["text2"]))
        painter.setPen(Qt.PenStyle.NoPen)
        r = max(2.0, self._m["font"] * 0.06)
        d = r * 2
        cx = self.width() / 2
        hh = self.height()
        painter.drawRoundedRect(QRectF(cx - r, hh * 0.36 - r, d, d), r, r)
        painter.drawRoundedRect(QRectF(cx - r, hh * 0.64 - r, d, d), r, r)


class FlipClock(QWidget):
    """翻页钟面主体：按 ``HH:MM:SS`` / ``MM:SS`` 排列数字卡，换数触发翻转。

    内部带一个 250ms 实时节拍（随控件生命周期，非后台轮询），**三型钟面都自己往前走**，
    不靠框架每拍喂数：

    - 时钟型：每拍核对墙钟、只在「秒」真的变了时才翻 ⇒ 秒针精确卡在系统秒边界；
    - 倒计时 / 正计时：框架每拍只交一个**锚点**（那一刻的秒数 ＋ 同时刻的单调时钟读数），
      本控件按 250ms 往后续算，每次框架调用重新对锚 ⇒ 不累积漂移。

    **边界（别读成「间隔可以调大了」）**：「此刻该不该显示浮层」仍由框架每次 ``tick``
    现算，短窗口照样会漏。
    """

    def __init__(self, tier: str = "medium", parent: "QWidget | None" = None) -> None:
        super().__init__(parent)
        self._tier = tier
        self._mode = FACE_CLOCK
        self._current = datetime.now().strftime("%H:%M:%S")
        self._digits: "dict[int, FlipDigit]" = {}
        #: 锚点：``_anchor_s`` 秒、取自 ``_anchor_at``（单调时钟）那一刻。
        #: ``_direction`` = -1 倒计时（往 0 走）、+1 正计时（往大走）。
        self._anchor_s: "int | None" = None
        self._anchor_at: "float | None" = None
        self._direction = -1
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(metrics.FLIPCLOCK[tier]["gap"])
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lay = lay
        # 建卡时就把每张卡的初值定成首值 ⇒ **构造完这一刻钟面就是对的**，外部不必
        # 再喂一次（那一次喂值就是第二个「此刻几点」的判定者，见 ``set_value``）。
        self._build(self._mode, self._current)
        # 实时节拍：随本控件生命周期（子对象），浮层销毁即停，不占框架节拍。
        self._live = QTimer(self)
        self._live.setInterval(_LIVE_TICK_MS)
        self._live.timeout.connect(self._tick_live)
        self._live.start()

    @property
    def mode(self) -> str:
        """此刻钟面是哪一型（浮层据此决定要不要重新喂锚点）。"""
        return self._mode

    def _anchored_value(self) -> int:
        """由锚点推算此刻的秒数（倒计时不外推为负、正计时不倒退）。

        调用方须先确认锚点是**完整**的两项；缺项时的 ``0`` 只是兜底，不是有意义的读数
        —— 别把它当「没有锚点就等于剩余 0」用（那正是钟面会静默显示 00:00 的来源）。
        """
        if self._anchor_s is None or self._anchor_at is None:
            return 0
        delta = int(time.monotonic() - self._anchor_at)
        if self._direction > 0:
            return max(0, self._anchor_s + delta)
        return max(0, self._anchor_s - delta)

    def _tick_live(self) -> None:
        """实时节拍：只在「该变的数真的变了」时才翻，避免无谓重绘。"""
        if self._mode == FACE_CLOCK:
            now = datetime.now().strftime("%H:%M:%S")
            if now != self._current:
                self._apply_text(self._mode, now)
            return
        if self._anchor_s is not None and self._anchor_at is not None:
            text = fmt_mmss(self._anchored_value())
            if text != self._current:
                self._apply_text(self._mode, text)

    def _build(self, mode: str, value: str) -> None:
        while self._lay.count():
            item = self._lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._digits.clear()
        digits = value.replace(":", "")
        n = len(digits)
        for i in range(0, n, 2):
            for j in (0, 1):
                if i + j < n:
                    d = FlipDigit(self._tier, self, digits[i + j])
                    self._digits[i + j] = d
                    self._lay.addWidget(d)
            if i + 2 < n:
                self._lay.addWidget(_Colon(self._tier, self))

    def set_display(
        self,
        mode: str,
        value: str,
        anchor_s: "int | None" = None,
        anchor_at: "float | None" = None,
    ) -> None:
        """接框架给的显示态。倒计时 / 正计时另给锚点 ⇒ 之后由本控件自己往后续算。

        锚点两项**同生共死**：只来一项（或都不来，如实时钟）就整体作废、回落成直接
        显示 ``value``。**别把半份锚点存下来**——「有秒数、没有时刻」会被实时节拍读成
        0，钟面一路显示 ``00:00``（静默错值，不报错）。
        """
        anchored = mode in (FACE_COUNTDOWN, FACE_COUNTUP) and anchor_s is not None \
            and anchor_at is not None
        self._anchor_s = anchor_s if anchored else None
        self._anchor_at = anchor_at if anchored else None
        self._direction = 1 if mode == FACE_COUNTUP else -1
        if anchored:
            value = fmt_mmss(self._anchored_value())
        self._apply_text(mode, value)

    def set_clock_mode(self) -> None:
        """切回实时钟。**不接外部喂的值**：那一刻几点只许由钟面自己判定。

        与 ``apply`` 在时钟型上刻意不喂值同一条理由 —— 但**模式必须真的切回来**：
        上一条开 ``face`` 的条目退场之后，留下的可能是个还停在倒计时的钟面，不重建
        就会继续往前走一个已经结束的倒计时（静默错值）。值由本控件当场取。
        """
        if self._mode == FACE_CLOCK:
            return
        self._anchor_s = None
        self._anchor_at = None
        self._apply_text(FACE_CLOCK, datetime.now().strftime("%H:%M:%S"))

    def _apply_text(self, mode: str, value: str) -> None:
        """按 ``mode`` / ``value`` 刷新钟面：结构变了就重建卡，否则只翻变了的数字。"""
        digits = value.replace(":", "")
        if mode != self._mode or len(digits) != len(self._current.replace(":", "")):
            self._mode = mode
            self._current = value
            self._build(mode, value)
        self._current = value
        for i, ch in enumerate(digits):
            if i in self._digits:
                self._digits[i].set_value(ch)

    def sizeHint(self) -> "object":  # type: ignore[override]
        from PySide6.QtCore import QSize

        w, h = self._m_size()
        return QSize(w, h)

    def _m_size(self) -> "tuple[int, int]":
        m = metrics.FLIPCLOCK[self._tier]
        digits = self._current.replace(":", "")
        n = len(digits)
        colons = max(0, n // 2 - 1)
        w = n * m["card_w"] + colons * m["colon"] + (n + colons - 1) * m["gap"]
        return w, m["card_h"]


class _RowWidget(QWidget):
    """浮层上的一行条目：状态点 ＋ 标题（＋ 补充文本）＋ 右侧时刻 ＋ 可选动作。

    动作按钮**按这一行自己的 ``actions`` 画**（§3.3）：不配就不画那枚按钮 ——
    画一个点了没用的按钮是骗人。

    **垂直落位：左上对齐 ＋ 上边距 = 行高与自身行盒之差的一半**（:func:`_text_top_margin`）。
    四样文字字号各不相同（状态点与标题 13px、副文本与时刻 11px、按钮 12px），而 ``QLabel``
    的默认对齐是把**整个文本块**摆在盒中央、盒高又随 ``wordWrap`` 变 ⇒ 直接交给 Qt 会有
    毛病：**换行**的标题首行被整体居中、比同一行的状态点 / 时刻低三四个像素（实机观感就是
    「左边这排字和右边时刻不在一条线上」）；反过来把「文字顶」写成一条手算的公共基线，
    又会整行抬高 0.35~1.5px（见下）。这里的做法是 **文字顶只由边距决定**：盒高再怎么变，
    首行都在原处，其余几样也照旧对得上。

    **别退回「手算一条公共基线」那种写法**（三代都试过、前两代都删了，见
    ``内部记录（浮层行文字）``）：把「基线 ＝ 行高一半 − ascent」写进一个
    函数，等于自己重算一遍 Qt 的落位口径。按 ``ascent + descent`` 的行盒算会整体抬高
    0.35px；改按 ``tightBoundingRect`` 的墨迹算反而抬高 1.25~1.5px（那个墨迹带把汉字
    下方的空白也算进去了）。当前这版的净效果（cocoa 2× 渲染实测，各项墨迹中心相对行盒
    中心）：状态点 −0.50、标题 −0.25、副文本与时刻 +0.25 —— 无系统性偏向，残差 ≤ 半像素。
    """

    action = Signal(str, str)   # (entry_id, action)

    def __init__(self, row: Row, parent: "QWidget | None" = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(metrics.GAP_SM)

        def place(label: QLabel, role: str, color: "str | None" = None) -> None:
            """把一枚文本标签摆到行里那条「文字顶」上，并钉住行高。"""
            apply_text_role(label, role, color=color)
            # 必须先 polish：字号来自 QSS，没 polish 时读到的是父级默认字体的度量
            # （那会让上边距静默算错 —— 落位偏一点点，没人报错）。
            label.ensurePolished()
            label.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
            )
            label.setContentsMargins(0, _text_top_margin(label.font()), 0, 0)
            # 一行的高度＝inline 档按钮那一格：钉住最小高度，浮层里每一行才一样高
            # （不给的话「带按钮的行」22px、「不带的」19px，一屏看下来参差）。
            label.setMinimumHeight(metrics.BTN_INLINE)
            lay.addWidget(label, alignment=Qt.AlignmentFlag.AlignTop)

        dot = QLabel(_DOT_ACTIONABLE if row.actions else _DOT_PLAIN, self)
        place(dot, "overlay_body", color="accent_text" if row.actions else "text3")

        title = QLabel(row.title, self)
        title.setWordWrap(True)
        place(title, "overlay_body")

        if row.text:
            extra = QLabel(row.text, self)
            extra.setWordWrap(True)
            place(extra, "overlay_hint")
        lay.addStretch(1)

        if row.time_label:
            when = QLabel(row.time_label, self)
            place(when, "overlay_hint")

        for name in row.actions:
            btn = tool_button(_ACTION_LABELS.get(name, name), "pill-ghost",
                              size="inline", parent=self)
            btn.clicked.connect(
                lambda _checked=False, a=name: self.action.emit(row.entry_id, a)
            )
            lay.addWidget(btn, alignment=Qt.AlignmentFlag.AlignTop)


class ClockOverlay(Overlay):
    """待办提醒的浮层：继承框架 ``Overlay``，**右上角常驻**（走框架 ``show_docked``）。

    交互分工（拖动与关闭互不打架）：**左键拖动**移动；**双击 / 右键关闭**（由
    ``close_gesture`` 决定）。基类 ``draggable=False``，手势全自己实现。行上那两枚
    按钮是子控件，自己吃掉按下事件 ⇒ 点按钮不会把浮层拖走。
    """

    user_closed = Signal()
    row_action = Signal(str, str)   # (entry_id, action)

    def __init__(
        self, tier: str = "medium", close_gesture: str = "either",
        parent: "QWidget | None" = None,
    ) -> None:
        # 自适配尺寸（内容决定大小），不让基类定死；draggable=False，拖动手势自己实现。
        super().__init__(parent, size=None, draggable=False)
        self._tier = tier
        self._close_gesture = close_gesture
        self._clock = FlipClock(tier, self)
        self.content_layout.addWidget(self._clock, alignment=Qt.AlignmentFlag.AlignCenter)
        self._rows = QWidget(self)
        # 行区的最小宽度：钟面只有几枚数字卡宽，按那个宽度排「圆点＋标题＋时刻＋两枚
        # 按钮」会把四字标题挤成两三行（见 ``metrics.OVERLAY_MIN_W``）。
        self._rows.setMinimumWidth(metrics.OVERLAY_MIN_W)
        rl = QVBoxLayout(self._rows)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(metrics.GAP_XS)
        self.content_layout.addWidget(self._rows)
        self._home = None
        self._press = None
        self._dragging = False
        # 用户拖离钉位后，后续 redock 不再把它拽回右上角（否则每拍都会被钉回原处，
        # 拖着没意义）。浮层被关掉再开 → 新建一份，_detached 复位为 False。
        self._detached = False
        # 行内容与上限提示的上一次取值：没变就跳过重建，消除每秒多余的重排抖动。
        self._last_rows: "tuple | None" = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.adjustSize()

    def apply(
        self,
        size: str,
        face: str,
        value: str,
        rows: "list[Row]",
        more_text: str = "",
        anchor_s: "int | None" = None,
        anchor_mono: "float | None" = None,
    ) -> None:
        if size != self._tier:
            self._set_tier(size)
        if face == FACE_CLOCK:
            # **实时钟刻意不喂值**：那一刻几点只许由钟面自己判定（构造即对齐，之后由它
            # 自带的 250ms 节拍卡秒边界往前走）。在这里再喂一遍就是第二个判定者 ——
            # 两条 1s 节拍的相位差会落在翻页动画（300ms）之内，把同一个数字再翻一次。
            # 但**模式要切回来**，否则会继续走完一个已经结束的倒计时。
            self._clock.set_clock_mode()
        else:
            self._clock.set_display(face, value, anchor_s, anchor_mono)
        self._set_rows(rows, more_text)

    def _set_tier(self, tier: str) -> None:
        self._tier = tier
        self.content_layout.removeWidget(self._clock)
        self._clock.deleteLater()
        self._clock = FlipClock(tier, self)
        self.content_layout.insertWidget(0, self._clock, alignment=Qt.AlignmentFlag.AlignCenter)
        # 运行时**新建**的子控件默认隐藏，必须显式 show()：隐藏的控件不参与布局，
        # 少了这一句 adjustSize() 当场量到的是空布局（只有内边距），钉位就会按错宽度算。
        self._clock.show()
        self.adjustSize()
        self.redock()

    def _set_rows(self, rows: "list[Row]", more_text: str) -> None:
        # 没变就跳过：稳态下每秒都进 apply，若不拦截会反复 adjustSize ＋ redock，
        # 造成可见的逐秒抖动；行里的按钮也会被一秒重建一次（点着点着消失）。
        key = (tuple(rows), more_text)
        if key == self._last_rows:
            return
        self._last_rows = key
        lay = self._rows.layout()
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        if not rows and not more_text:
            self._rows.hide()
            self.adjustSize()
            self.redock()
            return
        self._rows.show()
        for row in rows:
            widget = _RowWidget(row, self._rows)
            widget.action.connect(self.row_action.emit)
            lay.addWidget(widget)
            # 同 _set_tier：运行时新建的控件默认隐藏，不 show() 就不参与布局 ⇒
            # 量到的浮层高度会漏掉这几行（钉位高度算错 → 提示栈与行区重叠）。
            widget.show()
        if more_text:
            more = QLabel(more_text, self._rows)
            apply_text_role(more, "overlay_hint")
            lay.addWidget(more)
            more.show()
        self.adjustSize()
        self.redock()

    # ---------- 拖动（左键）＋ 关闭（双击 / 右键） ----------

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = event.globalPosition().toPoint()
            self._home = self.frameGeometry().topLeft()
            self._dragging = False
        elif event.button() == Qt.MouseButton.RightButton:
            # 右键关闭（按设置允许时）
            if self._close_gesture in ("right_click", "either"):
                self._animate_close()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._press is not None:
            d = event.globalPosition().toPoint() - self._press
            # 超过阈值才算「在拖」，避免点按被当成拖动
            if not self._dragging and (
                abs(d.x()) > _DRAG_THRESHOLD_PX or abs(d.y()) > _DRAG_THRESHOLD_PX
            ):
                self._dragging = True
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            if self._dragging:
                self.move(self._home.x() + d.x(), self._home.y() + d.y())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if self._press is not None:
            self._press = None
            if self._dragging:
                self.setCursor(Qt.CursorShape.OpenHandCursor)
                self._dragging = False
                # 拖到哪儿就停哪儿：标记脱离钉位，后续 redock 不再拽回右上角
                self._detached = True
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        # 双击关闭（按设置允许时）；左键双击与「左键拖动」天然不冲突（双击几乎无位移）。
        if event.button() == Qt.MouseButton.LeftButton and self._close_gesture in (
            "double_click", "either",
        ):
            self._animate_close()
        super().mouseDoubleClickEvent(event)

    def _animate_close(self) -> None:
        self.user_closed.emit()
        # 必须挂父对象：局部 QPropertyAnimation 在方法返回后被 GC ⇒ 动画中途夭折、
        # finished 永不触发 ⇒ 浮层「关了但没消失」（实机表现为点了没反应）。
        anim = QPropertyAnimation(self, b"pos", self)
        anim.setDuration(_CLOSE_SLIDE_MS)
        start = self.pos()
        anim.setStartValue(start)
        anim.setEndValue(QPoint(start.x() + self.width() + 40, start.y()))
        anim.finished.connect(lambda: self.dismiss(DISMISS_CLOSED))
        anim.start()

    def redock(self) -> None:
        """尺寸 / 行内容变了要重排整列。但若用户拖离了钉位（``_detached``），这一份留在
        原地，只把列里其余常驻与提示重排 —— 否则每拍 apply() 都会把它拽回右上角。"""
        if self not in live_docked():
            return
        if self._detached:
            for docked in live_docked():
                if docked is not self:
                    docked._move_into_dock()
            for toast in live_toasts():
                toast._move_into_stack()
            return
        super().redock()


class ClockController:
    """主线程控制器：接事件总线、增删改浮层。插件 ``tick()`` 在 worker 线程算状态，
    经事件桥回主线程，这里只做 UI 增删 —— 不私起时钟 / 线程。

    接线约定（见 ``plugin.py``）：构造时拿到 plugin 实例，订阅 ``status_topic`` 收到的
    是轻量唤醒信号 ``StateReport``（由框架自动发布），真正的整屏显示态由 ``plugin._display``
    写入，这里读取它来增删浮层。
    """

    def __init__(self, plugin: "Plugin") -> None:
        self._plugin = plugin
        self._overlay: "ClockOverlay | None" = None
        self._last_ids: "frozenset[str]" = frozenset()
        self._suppressed: "frozenset[str]" = frozenset()

    def on_display(self, _state: "object | None" = None) -> None:
        """订阅 ``status_topic`` 的回调；``_state`` 是唤醒信号（StateReport），不用它，
        改读 ``plugin._display`` 里的整屏显示态（worker 线程在 tick() 里写入）。"""
        # 用户关过浮层（双击/右键）后，dismiss 的 deleteLater 会销毁 C++ 本体、Python 壳
        # 留在 _overlay 里。死壳必须先归零再往下走 —— 否则下面 isVisible()/dismiss() 在
        # 死壳上抛 RuntimeError，这一拍中断、浮层永不重建（实机 2026-09-28「下班通知
        # 不弹、日志每拍报 Internal C++ object already deleted」的根因）。
        if not overlay_alive(self._overlay):
            self._overlay = None
        display = self._plugin._display
        if not display or not display.get("show"):
            self._last_ids = frozenset()
            # 抑制集合必须**带窗口维度**：窗口结束（show 翻回 False）即清掉，否则
            # 固定 / 推算类条目每天的 active_ids 完全相同（``{td1}``），
            # ``ids == self._suppressed`` 会永久命中早退 —— 表现为「关过一次就再也不弹」。
            self._suppressed = frozenset()
            if self._overlay is not None:
                self._overlay.dismiss(DISMISS_CLOSED)
                self._overlay = None
            return
        ids = frozenset(display.get("active_ids") or [])
        if display.get("test"):
            # 「预览这一条」（详情页那枚键）是用户自己点开的：**不理会关闭抑制** ——
            # 否则他此前关过这一条的浮层（抑制集合里记着同一批 id）时，点预览什么都不
            # 出现。抑制说的是「这条今天别再自动弹」，与手动预览无关（同旧翻页钟那边）。
            self._suppressed = frozenset()
        elif ids == self._suppressed:
            # 用户关过（双击/右键）、且这一批条目没变 → 保持关闭，不重开
            return
        self._suppressed = frozenset()
        self._last_ids = ids
        size = display["size"]
        # 从没建过，或被关掉（dismiss → hide，isVisible 变 False）⇒ 重建一份常驻
        # 浮层；否则只刷新内容（不重建，避免每拍闪一下）。
        if self._overlay is None or not self._overlay.isVisible():
            self._overlay = ClockOverlay(size, self._plugin.get_close_gesture())
            self._overlay.user_closed.connect(self._on_user_closed)
            self._overlay.row_action.connect(self._on_row_action)
            self._overlay.show_docked()
        self._overlay.apply(
            size,
            display["face"],
            display["value"],
            display.get("rows") or [],
            display.get("more_text") or "",
            display.get("anchor_s"),
            display.get("anchor_mono"),
        )

    def _on_user_closed(self) -> None:
        # 测试态（详情页编辑屏那枚「预览这一条」）：**关掉浮层就是预览的终点**（用户选的
        # 收尾方式），故直接结束它，不记进抑制集合 —— 抑制说的是「这条今天别再自动弹」，
        # 预览是用户自己点开的，与它无关（同旧翻页钟那边的口径）。
        if self._plugin.end_test():
            return
        # 常态：记下这一批 id，这一轮不再自动重开（窗口结束或条目集合变了再开，见
        # on_display 头两段 —— 抑制必须带窗口维度，否则「关过一次就再也不弹」）。
        self._suppressed = self._last_ids

    def _on_row_action(self, entry_id: str, action: str) -> None:
        """行上那两枚按钮：交回插件落账（写盘 / 记「稍后」），插件刷完浮层再由
        ``on_display`` 把新的显示态画上来 —— 控制器不认识任何一条业务规则。"""
        self._plugin.on_row_action(entry_id, action)

    def dispose(self) -> None:
        if overlay_alive(self._overlay):
            self._overlay.dismiss(DISMISS_CLOSED)
        self._overlay = None
        self._suppressed = frozenset()
        self._last_ids = frozenset()
