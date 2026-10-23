"""「面板可见但鼠标没反应」的处置结论、失活关闭三问、以及「鼠标要不要自己补投」的判断，
全部纯函数（不碰 Qt / PyObjC，离屏可逐格钉住）。

``macos_window.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations


#: 「面板可见、但鼠标事件全断」的三种处置结论。判据做成**纯函数**（不碰 Qt / PyObjC）是为了
#: 能在离屏环境下被用例逐格钉住；原生分支只负责把真实读数喂进来。
VERDICT_OK = "ok"                  # 正常：app 活跃，或面板本来就已经不可见
VERDICT_REACTIVATE = "reactivate"  # 首次撞见 ⇒ 重新激活一次，再复核
VERDICT_STUCK = "stuck"            # 复核过了还是不行 ⇒ 报一条能直接对上现象的日志


def panel_mouse_dead_verdict(visible: bool, app_active: bool, retried: bool) -> str:
    """面板可见却「鼠标没反应」时的处置：正常 / 重新激活 / 报错留痕。

    背景：面板可见与「app 是活跃 app」在本项目里是绑定的（见 :func:`app_is_active`）。
    两者同时成立就是正常；只有「可见但本进程不是活跃 app」才是故障态 —— 此时窗口收不到
    任何鼠标事件，用户看到的是「悬停不亮、tooltip 不出」。

    ``retried`` 区分「第一次撞见」与「已经重新激活过一次、复核仍为故障」：
    前者还有救（激活可能只是没生效，重新请求一次就回来了），后者只能留痕 —— 面板被留下，
    但日志里必须说清为什么它没有鼠标反馈，否则下次又只能靠猜。
    """
    if not visible or app_active:
        return VERDICT_OK
    return VERDICT_STUCK if retried else VERDICT_REACTIVATE


def should_close_on_deactivate(visible: bool, armed: bool, cursor_inside: bool) -> bool:
    """本进程失去前台时要不要把面板关掉：三问都要过。

    - ``visible``：面板根本没显示，无所谓；
    - ``armed``：显示后那道屏蔽窗（``_GATE_MS``）还没走完 ⇒ 不关（开面板那一下自身
      就会引起激活切换，那一次失活不是用户点的）；
    - ``cursor_inside``：**鼠标此刻落在面板矩形内 ⇒ 不关**。

    第三问是 2026-09-27 补的（用户报「程序启动后第一次点面板标题栏里的图标，整个面板
    消失」）。前两问问的都是「系统说了什么」，而真机上「系统说法」与「用户手在哪」经常
    对不上 —— 日志里反复出现「面板可见但不是活跃 app（已重新激活过一次仍未生效）」，
    也就是 :func:`app_is_active` 报 False 的那些会话。在这种会话里，任何一次前台跃变
    都只能来自系统或别的 app 抢焦点（用户还没能跟面板交互），可它照样满足前两问、
    把面板关掉。

    换成**鼠标位置**这个与激活状态无关的事实来判：点在面板内 ⇒ 手还在面板上 ⇒ 留下；
    点在面板外（桌面 / 别的 app / 菜单栏图标）⇒ 那才是「用户点别处」，照关。这条与
    ``PanelDismissController._on_global_click`` 读的是同一族判据（都在 Qt 屏幕坐标下比
    面板 / 图标的矩形，见 :func:`cursor_on_panel_or_icon`），两处不会分叉。

    ⚠️ 这条**不算**「点菜单栏图标 ⇒ 关闭」那条路：图标在面板矩形之外，那一格在本判据里
    是「鼠标在面板外」；点图标关面板由 ``_on_global_click``（图标矩形那一格）与图标自身的
    toggle 两条路负责，用户看到的结果一致。
    """
    return bool(visible and armed and not cursor_inside)


def should_reinject_motion(visible: bool, app_active: bool) -> bool:
    """面板可见、且本进程**不是**活跃 app 时，鼠标移动要不要由我们自己补投给 Qt。

    背景（判据与出处见 :func:`macos_base.app_is_active`）：本进程不是活跃 app 时，
    Qt cocoa 插件把鼠标移动 / 进入 / 离开 / 光标更新四类事件全部丢掉，用户看到的是
    「面板点得动、但悬停不亮、不出 tooltip、光标也不变手型」。

    2026-09-28 的诊断（``内部诊断记录``）已经把
    机制钉死，但「为什么程序化激活请求不被批准」没有收敛，而且实测证明**重试激活无效**
    （同一场景连试六次全部落空）。所以这里走另一条路：**不指望激活成功**，改为在
    「不活跃」这个前提下自己把鼠标位置补投给 Qt —— 与 :func:`panel_mouse_dead_verdict`
    那条「重新激活」的路并存，两条各管自己那一段，互不依赖。

    为什么不能无条件补投：app 活跃时 Qt 本来就收得到真实事件，再补一条就是**同一件事
    两个来源** —— 悬停会在两个来源之间闪、还多出一条假的鼠标移动。故只在「不活跃」时
    接管，且这一条是全仓**唯一**的判据出口。
    """
    return bool(visible and not app_active)
