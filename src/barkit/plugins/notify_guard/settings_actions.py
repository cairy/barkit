"""``_RulesActionsMixin``：恢复默认、归还手动覆盖、三个开关与防抖。

``settings.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations


class _RulesActionsMixin:
    """恢复与三个开关：恢复默认、归还手动覆盖、自动化总闸、启动行为、防抖。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    def _on_reset(self) -> None:
        """初始化：清空全部规则，回到出厂默认。

        出厂默认是**「无规则」**（`config_schema()` 里 `rules` 的 `default=[]`），不是四条预设 ——
        预设是"起手式"，不是出厂态。故本按钮 = **恢复出厂**，要清空就得先过一道确认。
        """
        if not self._rules:
            self._list_fb.hint("规则已是初始状态（一条都没有）。")
            return
        self._list_fb.ask(
            f"清空全部 {len(self._rules)} 条规则，回到出厂默认（无规则）？此操作不可恢复。",
            "清空",
            self._do_reset,
            confirm_kind="danger",
        )

    def _do_reset(self) -> None:
        backup = list(self._rules)
        self._rules.clear()
        if not self._save():
            self._rules.extend(backup)       # 落盘失败 → 回滚内存，绝不假装清空了
            return
        self._editing = None
        self._stack.back()
        self._refresh_list()
        self._list_fb.info("已清空全部规则，回到出厂默认。")

    def _on_resume_auto(self) -> None:
        """交回自动逻辑（手动覆盖的唯一归还入口，见 ``_build_list_page`` 那段判据）。

        与「声明式按钮」不同：这里是设置页**直接调插件方法并当场消费返回值** —— 结果落
        本页自己的反馈槽（``FeedbackBar``），零模态（本模块纪律）。成功时插件回 ``None``
        （卡片消失 ＋ 状态条变回"正常/已静音"就是回执，不再补一句客套话）。
        """
        result = self._plugin.resume_auto_action()
        self._refresh_list()          # 先刷新：它会把上一句回执 / 卡片可见性一起更新
        self._say_result(result)

    def _on_toggle_automation(self) -> None:
        # Switch 只有 clicked、不自动翻转自身状态，故用 not isChecked() 推导目标态，
        # 操作成功后再 setChecked 同步（与 shell/settings.py 的插件开关同一写法）。
        target = not self._auto_sw.isChecked()
        self._plugin.set_automation_active(target)
        self._auto_sw.setChecked(self._plugin.automation_active)
        self._refresh_list()

    def _on_toggle_launch(self) -> None:
        """「启动时自动拉起 OpenDisplay」：写回用户配置段（零授权、零模态）。

        它是**启动行为**，不是状态 —— 开关本身已是回执，故成功不另给提示语；
        写盘失败才说话，且**回滚控件位置**（否则界面在撒谎：显示已开、盘上没写）。
        """
        target = not self._launch_sw.isChecked()
        if not self._plugin.set_open_on_startup(target):
            self._launch_sw.setChecked(self._plugin.open_on_startup)
            self._list_fb.error("保存失败——写回配置没成功，请重试。")
            return
        self._launch_sw.setChecked(self._plugin.open_on_startup)

    # ---------- 装置调参 / 运维 / 规则排序 ----------

    def _debounce_choices(self) -> list[tuple[str, str]]:
        """防抖候选：1–5 轮 ＋ **盘上真值**（用户手改过 YAML 时如实列出，绝不显示成别的数）。

        文案只说轮数、**不报秒数**：一轮 = 框架节拍，而那个键（`plugins.<id>.poll`）
        按 D27 只服务调度器一个读者 —— 本组件读不到它，报一个"约 X 秒"就成了第二真源
        （用户在本页页尾改了运行间隔，这里还写着旧数）。单位由旁边那句 note 指向
        本页页尾。**不设「默认」项**：那是 `poll` 的语汇（框架 `_poll_choices` 那套），
        防抖的合法域就是轮数，盘上是几就显示几。
        """
        cur = self._plugin.debounce
        ladder = [1, 2, 3, 4, 5]
        if cur not in ladder:
            ladder.append(cur)
            ladder.sort()
        return [(f"{n} 轮", str(n)) for n in ladder]

    def _on_debounce(self, value: str) -> None:
        """防抖下拉：只写用户改的那一个键；写盘失败**回滚控件**（界面不许撒谎）。"""
        cycles = int(value)
        if not self._plugin.set_debounce(cycles):
            self._debounce_sel.set_current(str(self._plugin.debounce))
            self._list_fb.error("保存失败——写回配置没成功，请重试。")
            return
        self._list_fb.info(f"防抖已设为 {cycles} 轮。")
