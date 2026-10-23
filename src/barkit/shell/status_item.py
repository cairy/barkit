"""ui/status_item：macOS 原生 NSStatusItem 封装。

为什么不用 QSystemTrayIcon：
- 它的图标在 macOS 菜单栏上是「普通彩图」，无法设为 template（单色自适应），
  在深色菜单栏下几乎看不见，与其它 App 的白色系图标格格不入；
- `tray.geometry()` 在 macOS 上常常返回空/错误矩形，弹层无法准确定位到图标下方。

原生 NSStatusItem 解决这两点：
- 图标设为 template 图后由系统渲染为单色，自动适配浅/深菜单栏，大小也由系统管理；
- `button().window()` 能拿到图标按钮真实的屏幕坐标，用于把弹层锚定在图标正下方。

非 macOS 平台 `_item` 为 None，调用方应回退到 QSystemTrayIcon。
"""

from __future__ import annotations

import logging
import platform
from typing import TYPE_CHECKING

import objc
from Foundation import NSData, NSObject
from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QScreen

from barkit.contribution.specs import StateLevel
from barkit.shell import styles
from barkit.shell.design.levels import LEVEL_COLOR, as_level
from barkit.shell.design.tokens import token

if TYPE_CHECKING:
    from barkit.shell.design.icons import IconName

log = logging.getLogger(__name__)


# 托盘角标的「挑谁上屏」已收归 shell/renderers/badge.tray_queue（决议 D17 纯函数：
# 作者 alerts ＋ 全局阈值 → 最坏那一级 → 级内按 plugin_order 排队）；本模块只保留
# 渲染原语（set_badge / _render_badge），不做任何聚合判断。


def flip_constant(
    qt_top: float,
    qt_height: float,
    cocoa_origin_y: float,
    *,
    global_coords: bool = True,
) -> float:
    """返回 Y 翻转常数 const，使 ``qt_y = const - cocoa_y`` 成立。

    坐标定义：
    - Cocoa 全局：原点在主屏左下、y 向上；主屏 frame 的 origin 恒为 (0, 0)。
    - Qt 虚拟  ：原点在主屏左上、y 向下；主屏 geometry 的 top-left 恒为 (0, 0)。

    两系之间只有**一个**全局变换 ``qt_y = H_primary - cocoa_y``（H_primary = 主屏高），
    与屏幕排布无关。把它写成「图标所属屏」的表达式：该屏的 Cocoa 底边是
    ``cocoa_origin_y``、Qt 底边是 ``qt_top + qt_height``，二者是同一条物理边，于是

        const = (qt_top + qt_height) + cocoa_origin_y          # == H_primary

    每屏算出来都应等于主屏高，这本身就是自校验（见 tests/test_status_item_coords.py）。

    ⚠️ 旧实现写成 ``qt_top + qt_height - cocoa_origin_y``，等于上式**减去
    2×cocoa_origin_y**：只在 ``cocoa_origin_y == 0``（主屏、或与主屏等高且y 向对齐的
    左右副屏）时碰巧正确；副屏上下摆放时偏差达 2×|cocoa_origin_y|（1080p 屏 →
    ±2160px，面板直接飞出屏幕）。

    ``global_coords=False`` 用于 ``convertRectToScreen_`` 在个别系统版本上返回**屏内
    局部**坐标（原点=本屏左下）的兜底情形：此时本屏原点不参与，const 退化为
    ``qt_top + qt_height``（X 侧同理需加回 ``cocoa_origin_x``）。
    """
    if global_coords:
        return qt_top + qt_height + cocoa_origin_y
    return qt_top + qt_height


def is_global_cocoa_y(cy: float, cocoa_origin_y: float, cocoa_height: float) -> bool:
    """判别 ``cy`` 是 Cocoa 全局坐标还是所属屏内的局部坐标。

    判据：全局 ``cy`` 必落在所属屏的 Cocoa 纵向区间 ``[origin_y, origin_y+height]``
    内；局部 ``cy`` 落在 ``[0, height]`` 内。当 ``origin_y != 0`` 时两区间只在极端
    边界相交，可区分（菜单栏图标贴近屏幕顶边，不会落在边界上）。

    ``origin_y == 0``（主屏）时两区间重合 —— 但此时 :func:`flip_constant` 的两种
    取值也恰好相等，判别结果不影响正确性。
    """
    return cocoa_origin_y <= cy <= cocoa_origin_y + cocoa_height


def match_qt_screen_by_geometry(
    cocoa_origin_x: float,
    cocoa_origin_y: float,
    width: float,
    height: float,
    qt_screens,
) -> "object | None":
    """按几何（x 原点 ＋ 宽高）把一块 Cocoa 屏映射到对应的 Qt 屏。

    ``NSScreen.screens()`` 与 ``QGuiApplication.screens()`` 的排列顺序**未必一致**
    （副屏热插拔、主屏变更、macOS 版本差异都会让它们错位），此前按下标对齐会把面板
    钉到错误的屏（表现成「从屏幕左上角弹出」的偶发 bug）。X 原点与宽高在两套坐标系里
    完全一致（X 同向、尺寸同值），足以唯一定位物理屏，且与列表顺序无关。

    返回匹配的 ``QScreen``；一个都匹配不上返回 ``None``（调用方退回主屏）。
    """
    for qs in qt_screens:
        g = qs.geometry()
        if (
            g.x() == cocoa_origin_x
            and g.width() == width
            and g.height() == height
        ):
            return qs
    return None


class _ClickTarget(NSObject):
    """NSStatusItem 按钮点击的 target（selector 为 clicked:）。模块级定义，便于 pyobjc 注册。"""

    def initWithCb_(self, cb):
        self = objc.super(_ClickTarget, self).init()
        if self is None:
            return None
        self._cb = cb
        return self

    def clicked_(self, _sender):
        if self._cb:
            self._cb()


def _pixmap_to_nsimage(pixmap) -> "object":
    """把 Qt 的 QPixmap 转成 NSImage（经 PNG 数据），并把**点数归一**。

    Qt 导 PNG 按 96 DPI 记、NSImage 解按 72 DPI ⇒ 自然点数 = 像素 × 0.75。对 DPR-aware
    位图（物理 = 逻辑 × dpr）再除以 dpr，使显示点数与 1x 路径**完全一致** —— 既保住了
    dpr 倍的高分辨率底图（菜单栏不再被放大发虚），又不改变菜单栏那一格的大小。

    不做这步时，dpr=2 的底图会被 NSImage 当点数用 ⇒ 图标直接放大两倍（2026-09-21 实测）。
    """
    from PySide6.QtCore import QBuffer, QIODevice

    from AppKit import NSImage

    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.ReadWrite)
    pixmap.toImage().save(buf, "PNG")
    data = bytes(buf.data())
    nsdata = NSData.dataWithBytes_length_(data, len(data))
    img = NSImage.alloc().initWithData_(nsdata)
    dpr = pixmap.devicePixelRatio()
    if dpr and dpr != 1.0:
        from Foundation import NSSize

        s = img.size()
        img.setSize_(NSSize(s.width / dpr, s.height / dpr))
    return img


def _hi_res_image(size: int, dpr: float) -> "object":
    """建一张点数 == ``size``、像素 == ``size×dpr`` 的 NSImage 画布。

    菜单栏（Retina）下用 ``size`` 点数承载 ``size×dpr`` 像素的绘制，角标图层不再被放大
    发虚；``dpr==1``（测试 / 非 Retina）退化为普通 ``initWithSize_``，与历史完全一致。
    """
    from AppKit import NSBitmapImageRep, NSDeviceRGBColorSpace, NSImage
    from Foundation import NSSize

    img = NSImage.alloc().initWithSize_(NSSize(size, size))
    if dpr and dpr != 1.0:
        px = max(1, int(round(size * dpr)))
        rep = (
            NSBitmapImageRep.alloc()
            .initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
                None, px, px, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0
            )
        )
        if rep is not None:
            rep.setSize_(NSSize(size, size))
            img.addRepresentation_(rep)
    return img


def _nscolor(hex_str: str) -> "object":
    """``#RRGGBB`` → NSColor。级别色由共享色表给出，这里只做格式转换。"""
    from AppKit import NSColor

    h = hex_str.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, 1.0)


def _level_color_hex(level: str, dark: bool) -> str:
    """级别 → 该菜单栏外观下的实际色值。

    唯一来源是共享色表 ``design/levels.LEVEL_COLOR``（面板、标题栏同一张）；认不出的
    级别退回中性 ``text3`` —— 角标宁可画成灰的，也不要因为一个没见过的级别名字就
    整枚图标都画不出来。
    """
    fg, _bg = LEVEL_COLOR[as_level(level) or StateLevel.IDLE]
    return token(fg, "dark" if dark else "light")


class StatusItem:
    """macOS 原生菜单栏图标。创建失败（含非 macOS）时 `available` 为 False。"""

    def __init__(self, on_click, pixmap=None) -> None:
        self._item = None
        self._target = None
        self._base_pixmap = pixmap  # 默认底图：BarKit 条形码（无徽标时用作 template 图）
        self._base_icon: "IconName | None" = None  # 换源后的底图图标（None = 用 _base_pixmap）
        # 角标 = (级别, 取值图标) | None（决议 D17）：级别决定圆盘颜色，图标决定字形。
        self._badge: "tuple[str, IconName | None] | None" = None
        if platform.system() != "Darwin":
            return
        try:
            from AppKit import NSStatusBar, NSVariableStatusItemLength

            bar = NSStatusBar.systemStatusBar()
            self._item = bar.statusItemWithLength_(NSVariableStatusItemLength)
            button = self._item.button()
            self._apply_icon()  # 按当前（无）徽标 + 主题初始化图标
            self._target = _ClickTarget.alloc().initWithCb_(on_click)
            button.setTarget_(self._target)
            button.setAction_("clicked:")
            # 系统外观变化时重渲染图标（徽标模式下需按新主题色重绘）
            styles.theme_changed.connect(self._apply_icon)
        except Exception as e:  # noqa: BLE001
            log.warning("NSStatusItem 创建失败，将回退系统托盘：%s", e)
            self._item = None

    @property
    def available(self) -> bool:
        return self._item is not None

    def button_rect_qt(self) -> QRect | None:
        """图标按钮在 Qt 屏幕坐标系（左上原点）下的矩形；取不到返回 None。

        坐标换算陷阱：Cocoa 全局坐标（原点 = 主屏左下、y 向上）转 Qt 虚拟坐标
        （原点 = 主屏左上、y 向下）时，Y 翻转常数**必须按图标实际所在屏**计算，
        不能统一用主屏高度。多屏下副屏的 Cocoa 原点 y 有非零偏移（如副屏在主屏
        上方时 cy0>0），且副屏菜单栏图标的 Cocoa Y 未必带该偏移 → 用「按钮中心
        落在哪个 Cocoa screen 区间」匹配会误判到主屏，把面板弹到错误显示器。

        正确做法：① 用 NSStatusItem 按钮窗口的所属屏 `win.screen()`（系统权威、
        不受坐标区间误差影响）定位物理屏；② 按该屏的
        ``(Qt_top + Qt_height) + Cocoa_origin_y`` 做 Y 翻转 —— 推导与旧实现的
        具体错误见 :func:`flip_constant`，X 两系一致直接用。

        ⚠️ 多屏钉屏偶发错位（2026-09-24 修正）：Cocoa 屏与 Qt 屏**按下标对齐**的
        假设不可靠 —— ``NSScreen.screens()`` 与 ``QGuiApplication.screens()`` 顺序
        未必一致（副屏热插拔、主屏变更、macOS 版本差异都会让它们错位）。错位后
        ``qt_screens[i]`` 是**另一块屏**，翻转常数与钉屏都按错屏算，面板被夹进错屏
        角落（表现成「从屏幕左上角弹出」：关掉副屏即恢复、重开副屏也恢复，说明是
        偶发的顺序错位而非公式错）。现改为**按几何（x 原点 ＋ 宽高）跨列表匹配**，
        与顺序无关；Y 翻转仍走 :func:`flip_constant`（全局坐标时常数恒等于主屏高，
        本就不依赖所在屏）。
        """
        if self._item is None:
            return None
        try:
            from AppKit import NSScreen
            from PySide6.QtGui import QGuiApplication

            button = self._item.button()
            win = button.window()
            if win is None:
                return None
            rect = win.convertRectToScreen_(button.frame())
            cx = rect.origin.x
            cy = rect.origin.y  # Cocoa 全局：按钮底边
            cw = rect.size.width
            ch = rect.size.height

            # 锁定图标所属 Cocoa 屏（系统权威），再**按几何（x 原点 ＋ 宽高）跨列表匹配
            # Qt 屏** —— 不赌 ``NSScreen.screens()`` 与 ``QGuiApplication.screens()``
            # 顺序一致（副屏热插拔后两列表顺序可能错位，是此前面板偶发弹到错屏角落的根因）。
            cocoa_screens = NSScreen.screens()
            qt_screens = QGuiApplication.screens()
            own = win.screen()
            primary = QGuiApplication.primaryScreen()

            own_cs = None
            for cs in cocoa_screens:
                if cs is own or (
                    own is not None
                    and cs.frame().origin.x == own.frame().origin.x
                    and cs.frame().origin.y == own.frame().origin.y
                ):
                    own_cs = cs
                    break

            target_qt = primary
            f = own_cs.frame() if own_cs is not None else None
            if f is not None:
                # 按几何（x 原点 ＋ 宽高）跨列表匹配 Qt 屏，不赌两列表顺序一致。
                target_qt = match_qt_screen_by_geometry(
                    f.origin.x, f.origin.y, f.size.width, f.size.height, qt_screens
                ) or primary

            # cy 可能是 Cocoa 全局，也可能是本屏局部（个别系统版本）；用「是否落在本屏
            # Cocoa 纵向区间内」判别，两种情形都能覆盖。
            g_coords = (
                is_global_cocoa_y(cy, f.origin.y, f.size.height)
                if f is not None
                else True
            )
            g_geo = target_qt.geometry() if target_qt is not None else None
            if g_geo is not None and f is not None:
                y_const = flip_constant(
                    g_geo.y(), g_geo.height(), f.origin.y, global_coords=g_coords
                )
            else:
                # 取不到屏信息时的兜底：全局坐标常数恒等于主屏高
                y_const = primary.geometry().height() if primary is not None else 0.0
            if not g_coords and f is not None:
                cx += f.origin.x  # 局部坐标：X 同样要加回本屏原点

            log.debug(
                "状态项坐标：匹配 Qt 屏 geom=%s Cocoa origin=(%s,%s) 局部坐标=%s 翻转常数=%s",
                g_geo, f.origin.x if f is not None else "?", f.origin.y if f is not None else "?",
                not g_coords, y_const,
            )

            # Cocoa → Qt：Y 用本屏常数翻转（Y 向下）；X 两系同向（局部时需加回原点）
            qt_x = cx
            qt_y_top = y_const - (cy + ch)  # 按钮顶边在 Qt 坐标的 Y
            rect = QRect(int(qt_x), int(qt_y_top), int(cw), int(ch))
            return self._validated(rect)
        except Exception as e:  # noqa: BLE001
            log.warning("取状态项坐标失败：%s", e)
            return None

    @staticmethod
    def _validated(rect: QRect) -> QRect | None:
        """锚点合法性：矩形中心必须落在**某一块屏**里，否则当没读到。

        多屏下这条校验是必要的，不是保险：一个 ``NSStatusItem`` 在每块屏各有一枚按钮
        窗口，而系统把那枚窗口摆好之前它停在屏幕外的初始位（``(0, -37)`` 一类）——
        换算出来就是一个没有任何屏包含它的矩形。此前这种读数被原样当锚点用掉，面板
        于是被夹到屏幕边沿（用户报「弹窗跑到屏幕角上」），而日志里只看得出「锚点 x=0」。
        """
        from PySide6.QtGui import QGuiApplication

        if QGuiApplication.screenAt(rect.center()) is None:
            log.warning(
                "状态项矩形落在所有屏幕之外 %s —— 图标窗口多半还没被系统摆好"
                "（多屏下一枚图标在每块屏各有一枚窗口），本次当没读到",
                rect,
            )
            return None
        return rect

    def button_size(self) -> tuple[int, int]:
        """图标按钮的**点数尺寸** ``(宽, 高)``；取不到给 ``(24, 24)``。

        **位置不可信时尺寸仍可信**：按钮在自己那个窗口里的大小由系统给，与窗口有没有
        被摆好无关。故本口不经过 :meth:`button_rect_qt` 的合法性校验 —— 它的用途正是
        「位置读数不可用时的兜底」（如由点击点推锚点，见 ``shell/placement``）。
        """
        if self._item is None:
            return (24, 24)
        try:
            f = self._item.button().frame()
            w = int(round(float(f.size.width)))
            h = int(round(float(f.size.height)))
            return (w, h) if w > 0 and h > 0 else (24, 24)
        except Exception:  # noqa: BLE001
            return (24, 24)

    def button_nsview(self) -> "object | None":
        """图标按钮的 PyObjC ``NSButton``（锚定 NSPopover 用）；取不到返回 None。

        ``NSPopover.showRelativeToRect_ofView_preferredEdge_`` 需要一个真实 ``NSView``
        作锚点；状态项按钮本身就是它，直接返回即可（与 :meth:`button_rect_qt` 同源，
        但给的是原生 view 而非换算后的 Qt 矩形）。
        """
        if self._item is None:
            return None
        try:
            return self._item.button()
        except Exception:  # noqa: BLE001
            return None

    def button_screen(self) -> "QScreen | None":
        """图标按钮窗口所属的 QScreen（系统权威，多屏定位用）。

        NSStatusItem 按钮窗口的 `screen()` 直接返回其所属 NSScreen，比手动换算
        Cocoa→Qt 坐标后再 screenAt 更可靠。面板据此 setScreen 钉在图标所在屏。

        ⚠️ 2026-09-24 修正：此前按下标 `qt_screens[i]` 取 Qt 屏，依赖
        `NSScreen.screens()` 与 `QGuiApplication.screens()` 顺序一致 —— 副屏热插拔后
        两列表顺序可能错位，于是钉到错误的屏（面板偶发弹到错屏角落）。现改为**按几何
        （x 原点 ＋ 宽高）匹配**，与顺序无关。
        """
        if self._item is None:
            return None
        try:
            from AppKit import NSScreen
            from PySide6.QtGui import QGuiApplication

            button = self._item.button()
            win = button.window()
            if win is None:
                return None
            own = win.screen()
            if own is None:
                return None
            cocoa_screens = NSScreen.screens()
            qt_screens = QGuiApplication.screens()

            # 锁定所属 Cocoa 屏（identity 优先，其次 frame origin）
            own_cs = None
            for cs in cocoa_screens:
                if cs is own or (
                    cs.frame().origin.x == own.frame().origin.x
                    and cs.frame().origin.y == own.frame().origin.y
                ):
                    own_cs = cs
                    break
            if own_cs is None:
                return None

            # 按几何（x 原点 ＋ 宽高）跨列表匹配 Qt 屏，不赌顺序
            f = own_cs.frame()
            return match_qt_screen_by_geometry(
                f.origin.x, f.origin.y, f.size.width, f.size.height, qt_screens
            )
        except Exception as e:  # noqa: BLE001
            log.warning("取状态项所属屏失败：%s", e)
            return None

    def set_badge(self, level: str | None, icon: "IconName | None" = None) -> None:
        """设置托盘角标（决议 D17）：**级别色圆盘 ＋ 取值图标字形**。

        Args:
            level: 代表态级别（``err`` / ``warn`` / ``off`` / ``idle``）；None = 这一拍
                没有触发者，不画角标（走 template 底图那条路）。
            icon: 触发者的**取值图标**；None → 由 ``icons.badge_name`` 回落该级别的
                通用标记，故永远画得出一枚字形。

        旧的 ``"alert"`` / ``"mute"`` 两个固定符号随 D17 删除：静音不再是特殊通道，
        而是 ``notify_guard`` 的一个普通取值。非 macOS 或原生项创建失败时静默忽略。
        """
        if self._item is None:
            return
        self._badge = None if level is None else (level, icon)
        self._apply_icon()

    def set_base_icon(self, icon: "IconName | None") -> None:
        """指定托盘**底图**的来源图标（P0③ 底图换源；None = BarKit 默认底图）。

        只记来源并重绘，**不做回落判断**：快照缺失 / 该状态没给取值图标 / 状态失联
        这三条回落规则由装配器在调用前判完（``AppShell._apply_tray_base``）。

        未变化时不重绘：状态事件每次都到（含别的插件的事件），而重画要走
        PyObjC 造 NSImage，没必要为「图标没变」付这份成本。
        """
        if self._item is None or icon is self._base_icon:
            return
        self._base_icon = icon
        self._apply_icon()

    def redraw(self) -> None:
        """按当前（实时）外观与徽标状态重绘图标。

        用于换肤监听未可靠触发时作为安全网：用户每次点菜单栏图标都会经过，
        顺手用实时外观重绘一次，避免底图颜色与实际菜单栏错位。
        """
        self._apply_icon()

    def _menu_bar_is_dark(self) -> bool:
        """读菜单栏「实际」外观（而非系统设置），判断是否深色背景。

        关键： macOS 全屏时菜单栏常是深色浮层，但系统外观设置可能仍是浅色，
        二者会不一致。必须读状态项按钮窗口的 effectiveAppearance 才能拿到
        菜单栏真实颜色；这正是 template 图自动上色的依据。
        """
        try:
            from AppKit import (
                NSAppearance,
                NSAppearanceNameAqua,
                NSAppearanceNameDarkAqua,
            )

            button = self._item.button()
            win = button.window() if button is not None else None
            appearance = (
                win.effectiveAppearance()
                if win is not None
                else NSAppearance.currentAppearance()
            )
            matched = appearance.bestMatchFromAppearancesWithNames_(
                [NSAppearanceNameAqua, NSAppearanceNameDarkAqua]
            )
            return matched == NSAppearanceNameDarkAqua
        except Exception:  # noqa: BLE001
            # 取不到就退回系统外观设置（仍可能和全屏菜单栏不一致，但比不处理强）
            return styles.current_system_theme() == "dark"

    def _apply_icon(self) -> None:
        """按当前角标 + 菜单栏实际外观重设状态栏图标。

        - 无角标：用 template 图（系统按菜单栏真实颜色单色渲染，浅/深自动匹配，最可靠）。
        - 有角标：因不能把彩色角标也变单色，需手动绘制——底图颜色严格按「菜单栏
          实际外观」取（深→白、浅→黑），而非系统设置；主题切换 / 菜单栏颜色
          变化时本方法会被 theme_changed（或点击 redraw 安全网）再次调用。
        """
        if self._item is None:
            return
        try:
            from Foundation import NSMakeRect

            from barkit.shell.ui_kit import tray_base_pixmap

            button = self._item.button()
            if button is None:
                return
            # 底图可能已是 DPR-aware（装配时按菜单栏 dpr 渲），故取**逻辑**尺寸与 dpr：
            # 绝不用 width()（dpr>1 时它返回物理像素，会把菜单栏那一格放大两倍）。
            if self._base_pixmap is None:
                return
            dpr = self._base_pixmap.devicePixelRatio()
            logical = max(1, int(round(self._base_pixmap.deviceIndependentSize().width())))
            if self._badge is None:
                # 无角标：template 图，交给系统单色渲染（自动适配菜单栏深浅）。
                # P0③：底图可换源——来源图标按默认底图的**逻辑**尺寸渲染，故菜单栏那一格
                # 的**大小**与历史一致，换的只是字形；dpr 同步底图，Retina 下不再发虚。
                base_pixmap = self._base_pixmap
                if self._base_icon is not None:
                    base_pixmap = tray_base_pixmap(
                        self._base_icon, logical, QColor(0, 0, 0), dpr
                    )
                img = _pixmap_to_nsimage(base_pixmap)
                img.setTemplate_(True)
                button.setImage_(img)
                return

            # 有角标：底图颜色按「菜单栏实际外观」取，保证与背景对比（深→白、浅→黑）。
            # 画布对齐契约（决议 D17 硬要求 1）：24px——与无角标路径的底图同尺寸，
            # 否则同一枚菜单栏项在「有无角标」两态间会跳宽。
            size = logical
            dark = self._menu_bar_is_dark()
            base_color = QColor(255, 255, 255) if dark else QColor(20, 20, 22)
            base = _pixmap_to_nsimage(
                tray_base_pixmap(self._base_icon, size, base_color, dpr)
            )
            badge = self._render_badge(self._badge[0], self._badge[1], size, dark, dpr)
            canvas = _hi_res_image(size, dpr)
            canvas.lockFocus()
            base.drawInRect_(NSMakeRect(0, 0, size, size))
            badge.drawInRect_(NSMakeRect(0, 0, size, size))
            canvas.unlockFocus()
            canvas.setTemplate_(False)  # 保留彩色徽标
            button.setImage_(canvas)
        except Exception as e:  # noqa: BLE001
            log.warning("重绘状态栏图标失败：%s", e)

    @staticmethod
    def _render_badge(
        level: str,
        icon: "IconName | None",
        size: int = 24,
        dark: bool = False,
        dpr: float = 1.0,
    ):
        """生成右上角角标图层（决议 D17），坐标系与图标一致。

        - **圆盘**：级别色实心圆 —— 「有多坏」的唯一表达，色值取共享色表，按菜单栏
          **实际**外观解析（系统外观设置不可信：全屏时菜单栏常是深色浮层）。
        - **字形**：触发者的**取值图标**（12 网格角标字形），一律白描边 —— 与旧实现
          「红底白 !」的配色一脉相承，只是把固定字符换成随状态变的字形。

        旧的 ``state`` 三态（alert / mute / none）随本决议删除：静音降级为
        ``notify_guard`` 的普通取值，托盘不再有硬编码的「静」。

        圆盘直径约定 12px（半径 6）：在 24px 菜单栏画布下约占一半，比最初 14px 收敛，
        不再抢夺底图视觉重量（与面板角标同语言、同比例协调）。

        ``dpr`` 决定角标字形的物理分辨率（``_pixmap_to_nsimage`` 会归一显示点数）。
        """
        from AppKit import NSBezierPath
        from Foundation import NSMakeRect

        from barkit.shell.design.icons import badge_pixmap

        r = 6.0  # 角标直径 12px（契约：画布 24 / 角标 12；比最初 14px 收敛，不抢底图）
        cx, cy = size - r - 1.0, size - r - 1.0  # 右上角
        layer = _hi_res_image(size, dpr)
        layer.lockFocus()

        _nscolor(_level_color_hex(level, dark)).setFill()
        NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(cx - r, cy - r, 2 * r, 2 * r)
        ).fill()

        # 字形取**偶数** 8px：落点 cx−4 / cy−4 = 13 为整数 —— 奇数 9px 的落点是
        # 12.5，系统绘图层会做半像素插值，字形发糊且视觉下沉（实测小尺寸下描边
        # 糊成实心、观感「偏下」）。整数落点 + 字形微上移（见 icons.MARK_WARN）两头修。
        glyph_px = 8
        glyph = _pixmap_to_nsimage(
            badge_pixmap(icon, as_level(level), glyph_px, QColor(255, 255, 255), dpr)
        )
        glyph.drawInRect_(
            NSMakeRect(cx - glyph_px / 2.0, cy - glyph_px / 2.0, glyph_px, glyph_px)
        )

        layer.unlockFocus()
        return layer

    def set_tooltip(self, text: str) -> None:
        if self._item is not None:
            self._item.setToolTip_(text)

    def teardown(self) -> None:
        """退出清理：断主题信号并摘掉原生 NSStatusItem（macOS）。

        原收尾不处理菜单栏图标，遗留两件泄漏：

        - ``styles.theme_changed`` 上的连接（``__init__`` 内 ``connect(self._apply_icon)``）
          收尾不断开，换肤回调会进死对象；
        - 原生 ``NSStatusItem`` 不摘，app 走后菜单栏仍挂着它，且其按钮仍持有
          ``_ClickTarget``（闭包引用 ``on_click`` → 进而引用 AppShell）。

        一并拆掉，并把 ``_item`` / ``_target`` 置空，使本对象彻底与系统解绑。
        """
        if self._item is None:
            return
        try:
            styles.theme_changed.disconnect(self._apply_icon)
        except Exception:  # noqa: BLE001
            pass
        try:
            from AppKit import NSStatusBar

            NSStatusBar.systemStatusBar().removeStatusItem_(self._item)
        except Exception:  # noqa: BLE001
            pass
        self._item = None
        self._target = None
