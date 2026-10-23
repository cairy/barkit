"""``_AutomationMixin`` —— 自动化总闸与节律：暂停 / 恢复、helper 巡逻间隔、运行态。

``plugin.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:                      # 只为标注；运行期不 import（契约面由宿主提供）
    from barkit.contribution.specs import ActionResult


class _AutomationMixin:
    """自动化总闸与节律：暂停 / 恢复、helper 巡逻间隔、运行态。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 暂停 / 恢复自动化 ----------

    @property
    def is_paused(self) -> bool:
        return bool(self.cfg.value("paused"))

    def set_paused(self, paused: bool) -> "ActionResult | None":
        """暂停/恢复路由自动化：写用户 YAML（零授权）→ 重建期望态并声明出去。

        paused=true：helper 冻结、保持已生效路由不动（用户可手动去路由）。
        paused=false：交回自动逻辑，helper 按规则重评估。

        写盘失败 ⇒ 把结局**报出去**（`_failed`）：设置页那条路落在它那块槽上、
        返回 ``None``；面板 / 托盘那条路没有槽位，返回 ``ActionResult`` 交回结果槽
        —— 开关会弹回原位，一言不发等于骗人（`AutomationCapability` 契约 §2.6）。
        """
        paused = bool(paused)
        if self.is_paused == paused:
            return None
        # config.changed 会触发 on_config_changed → sync_routes；`_write_keys` 里
        # 再兜底同步一次（两条路都收敛到 `_sync_after_change`，不各写一遍）
        ok, problem = self._write_keys({"paused": paused}, fail="暂停失败")
        return None if ok else self._failed(problem)

    # ---------- helper 巡逻节律（helper_poll） ----------

    def helper_poll_seconds(self) -> int:
        """root helper 多久巡逻一轮路由表（秒）。

        读合并后的有效配置；缺省由 ``config_schema()`` 那条 ``helper_poll`` 字段给出
        （默认值就是 ``rules.DEFAULT_HELPER_POLL``，这里不再抄一遍）。该键与框架节拍
        无关（D27：一个键只服务一个消费者），单独可调、不牵动调度器。
        """
        return int(self.cfg.value("helper_poll"))

    def set_helper_poll(self, seconds: int) -> None:
        """改 helper 巡逻节律：写用户 YAML（零授权）→ 重建期望态并声明出去。

        取值域由设置页 QSpinBox 与 ``rules.build_routes`` 的校验共同约束（1–86400 秒）；
        这里只做幂等早退，越界值本就不会从界面产生。

        写盘失败 ⇒ 当场落在设置页的反馈槽（这条入口只有设置页在用，不像暂停那样
        还有面板那条路）。
        """
        seconds = int(seconds)
        if self.helper_poll_seconds() == seconds:
            return
        ok, problem = self._write_keys({"helper_poll": seconds}, fail="保存巡逻间隔失败")
        if not ok:
            self._say_error(problem)

    def toggle_pause_action(self, silent: bool = False) -> None:
        """托盘动作 / 面板 badge：在「暂停 / 恢复」间切换，并刷新 UI。

        silent=True（badge 点击）时不发提示，仅静默切换；其余情况把提示写进设置页
        的行内反馈槽 —— 从面板 / 托盘进来时没有槽位，只记日志。

        写盘失败 ⇒ 报出去就收手：下面那两句是「已经切成了」的口径，写失败还说
        「已暂停自动路由」是假成功。这里不经 `set_paused`，因为它的失败在设置页
        开着时已经落槽、只回 ``None``，调用方分不出「成了」与「没成」。
        """
        ok, problem = self._write_keys({"paused": not self.is_paused}, fail="暂停失败")
        if not ok:
            self._failed(problem)      # 无槽位时这次的返回值没人接（本口没有宿主结果槽）
            return
        if silent:
            return
        if not self._daemon_status.get("installed"):
            self._say_hint("守护尚未安装，暂停状态已记录，安装后生效。")
        elif self.is_paused:
            self._say_info(
                "已暂停自动路由。已生效的路由不会被自动改动；"
                "可在「配置路由规则」中禁用某条规则以撤掉其路由。"
            )
