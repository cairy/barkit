"""OpenDisplay 装置：事实回显（``opendisplay_facts``）、整行点击唤起
（``open_opendisplay``）、启动首拍拉起（``_launch_on_startup_once``）。

``app_state`` 在本族只有这一处出口 —— 故意如此：测试按字符串点名 patch 它（
``tests/test_notify_guard.py`` 三处），落点若分散在多族，点名就得跟着改好几处。
启动首拍三段判据缺一不做：开关没开就连 ``app_state()`` 都不探（默认路径不碰 AppKit）；
只有「装了但没跑」才拉起（已经在跑时动它是「再开一次窗口」，那是整行点击那件事）；
整个加载期只做一次（``tick`` 每 3s 一轮，没这道闸会反复拉起）。

``plugin.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from barkit.contribution.specs import (
    ActionOutcome,
    ActionResult,
)
from barkit.plugins.notify_guard.opendisplay import (
    APP_IDLE,
    app_state,
)


class _DeviceMixin:
    """OpenDisplay 装置：事实回显、整行点击唤起、启动首拍拉起（app_state 只有这一族读）。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    def opendisplay_facts(self) -> tuple[str, bool]:
        """设置页用的装置事实：``(应用态, 是否仍在菜单栏占位)``。

        两者都是零 fork 的进程内调用，且**只在打开设置页时**算一次（不进 tick）。
        """
        return app_state(), self.opendisplay_backend.is_in_menu_bar()

    def open_opendisplay(self) -> "ActionResult | None":
        """面板整行点击：唤起 OpenDisplay 窗体（§6.5-A，零框架改动）。

        **失败要说话**（本批 `ActionResult`，D32-②）：从前这里只记一行日志，用户在
        面板上看到的是「点了一下，什么也没发生」。

        ⚠️ **本方法的两个调用点目前都收不到这个结局**（见模块 docstring 的「接通范围」）：
        面板整行点击那一处的中继已停用、设置页按钮不消费返回值。也就是说现在返回失败
        **与改动前等价**（谁也没说话）；接通之后它才会真的出现在面板上。

        为什么**成功时什么都不返回**：``open -a`` 成不成看得见（窗体自己会出来），
        而「未安装」本就是 display 状态机的一个正常取值（``absent``），面板与设置页
        都明写着「未安装 OpenDisplay」—— 再补一句「已成功」只会在面板上多留一行字。
        这条判据是 `ActionResult` 的口径：**只在有话要说时返回**。
        """
        if self.opendisplay_backend.open_app():
            return None
        return ActionResult(
            ActionOutcome.FAILED,
            "没能唤起 OpenDisplay —— 它可能没安装，或应用路径已变。",
        )

    def _launch_on_startup_once(self) -> None:
        """启动首拍：按用户设置把 OpenDisplay **拉起来**（整个加载期只做一次）。

        为什么在 ``tick()`` 而不在 ``start()``：调度器注册即点火（``Scheduler.add_plugin``
        末尾那句 ``_on_beat``），首拍与 ``start()`` 同一时刻到达；但 tick 跑在 worker 线程，
        而 ``open -a`` 是一次 fork/exec —— 放 ``start()`` 会挡在宿主启动路径上。

        **首拍只上膛，第二拍才真拉**（一拍 ≈ 3s）：首拍与宿主装配同一时刻到达，而那一刻
        宿主的窗正在一枚枚建出来 —— ``open -a`` 紧接着引发的屏变化正撞在这上面。屏变化
        窗口里**首次上屏**的窗会被窗口服务器钉在 ``(0,0)`` 且进程内救不回来（见
        ``barkit.shell.screen_settle``）。代价只是 OpenDisplay 晚约 3 秒出来。

        三段判据，缺一不做（顺序即判据）：

        1. 用户没开这个开关 ⇒ **连 ``app_state()`` 都不探**（默认路径不碰 AppKit），
           也保证既有用例 / 无图形环境零额外调用；
        2. 只有「装了但没跑」（``idle``）才拉起。**已经在跑时不动它** —— ``open -a``
           对运行中的 App 是「再打开一次」，会走 ``applicationShouldHandleReopen``
           把窗体弹出来，而「弹出窗体」是**整行点击**那个动作的意思（§6.5-A），
           不是本开关的意思；没装时也不试，那只会换来一次注定失败的 ``open``
           与一行 warning，而面板早就写着「未安装 OpenDisplay」。
        """
        if self._startup_launch_done:
            return
        if not self._startup_launch_armed:
            # 首拍只「上膛」：这一拍与宿主装配**同一时刻**到达，此刻宿主的窗正在一枚枚
            # 建出来（面板 / 设置窗 / 状态项），而 ``open -a`` 会紧接着引发一次屏变化
            # —— 屏变化窗口里**首次上屏**的窗会被窗口服务器钉在 ``(0,0)`` 且进程内救
            # 不回来（见 ``barkit.shell.screen_settle``）。等下一拍再真拉，那边早建完了。
            self._startup_launch_armed = True
            return
        self._startup_launch_done = True
        if not self.open_on_startup:
            return
        if app_state() != APP_IDLE:
            return
        self.opendisplay_backend.open_app()
