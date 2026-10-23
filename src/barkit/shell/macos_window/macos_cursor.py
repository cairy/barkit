"""鼠标位置与「点在不在面板/图标上」的判据；全族唯一读鼠标位置的地方。

``macos_window.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations


def cursor_pos():
    """鼠标此刻的位置（Qt 屏幕坐标），读不到给 ``None``。

    单独留一个口，是为了让「鼠标在哪」这件事在用例里可以被钉住（否则判据只能靠真机动
    鼠标才能验，等于没验）。它也是本模块里**唯一**读鼠标位置的地方 —— 失活关闭与点到
    面板外两条路都从这里读，结论不会再分叉。
    """
    try:
        from PySide6.QtGui import QCursor

        return QCursor.pos()
    except Exception:  # noqa: BLE001 —— 无 Qt 应用实例等：没有读数
        return None


def cursor_inside_widget(widget) -> bool:
    """鼠标此刻是否落在 ``widget`` 的矩形内（Qt 屏幕坐标，与 :func:`cursor_pos` 同系）。

    读不到（无 Qt 应用实例 / 平台异常）时返回 ``False`` —— 按「不在面板内」处理，
    即退回加这条之前的行为，不制造新的「面板关不掉」。
    """
    p = cursor_pos()
    if p is None:
        return False
    try:
        return bool(widget.frameGeometry().contains(p))
    except Exception:  # noqa: BLE001
        return False


def cursor_on_panel_or_icon(cursor, panel_rect, icon_rect) -> bool:
    """鼠标此刻是否落在「面板矩形 ∪ 菜单栏图标矩形」内（三样都取 Qt 屏幕坐标）。

    这是**关不关面板的唯一基准**，两条关闭路（点到面板外的全局鼠标按下、失去前台）
    都读它：两条路若各读各的（一条读 Qt 几何、一条读 Cocoa 窗口），就会出现「同一件事
    两个结论」，而其中一个结论错了也不会报错。

    为什么不用全局事件自带的 ``locationInWindow()``（2026-09-27 改）：那个读数的坐标系
    是一条**没有验证过的注释假设**（「Cocoa 全局坐标、左下原点」）。拿「没验过的空间的
    一个点」去比「确定空间的一块矩形」，错了的表现恰好是「点哪儿都判在外面」——面板在
    任何一次点击下都消失，而矩形看上去一个都没错。``QCursor.pos()`` 实测与系统读数一致
    （差 0.5px，本机 2026-09-27 探针），且与 ``cursor_inside_widget`` 同源，两条路的判据
    不会分叉。

    ``icon_rect`` 为 ``None``（取不到图标矩形）时只判面板那一块 —— 那一格不参与判定，
    不假装它「不在图标上」。
    """
    if icon_rect is not None and icon_rect.contains(cursor):
        return True
    return bool(panel_rect.contains(cursor))
