"""三台状态机的当前快照，与手动操作后的即时广播（``_publish_all``）。

面板 / 徽标 / 托盘底图共用这一份真源；``tick`` 的返回值与 ``_publish_all`` 走同一个
拼装（``_all_snapshots``），不会各说各话。文案只有一份（决议 D18）：取值词条的
``text`` ＋ 该取值自己的补充说明，合成唯一那句 ``summary``；没有补充说明时留空
summary，由框架回退到声明的默认文案。

``plugin.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import time

from barkit.contribution.specs import StateReport

from .plugin_states import _NOTIFY_TEXT


class _SnapshotsMixin:
    """三台状态机的当前快照与手动操作后的即时广播（面板 / 徽标 / 托盘底图共用一份）。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 状态快照（三台机共用一份真源：面板 / 徽标 / 托盘底图）----------

    def _display_snapshot(self) -> StateReport:
        """display：第二屏 / OpenDisplay 装置态（**纯观察，无副作用**）。

        §4.2 的刻意不同：自动化关掉时**取值不变**——第二屏连没连跟本组件开关毫无关系，
        它是世界的状态，不是我造成的——措辞也不说"已冻结"。所以它是本组件唯一
        「纯读世界」的状态机。

        级别归取值声明（决议 D13），故这里不再有「自动化关掉 → 压到 IDLE」那一手：
        第一，那是**运行期改级别**，正是 D13 要消灭的第二条路径；第二，它本来就多余 ——
        「组件归灰」由 ``automation`` 那台机的 ``stopped``（OFF，比 IDLE 更靠后）已经
        一并做到，代表态取最坏仍是灰的。display 这一格现在如实报绿。
        """
        return StateReport(self._display_state, updated_at=time.time())

    def _notify_state(self) -> tuple[str, str]:
        """当前 notify 取值与其**补充说明**（取值闭集见 ``NOTIFY_STATES``）。

        第二项只补「词条没说的那半句」（如静音命中了哪条规则）——它与词条合成唯一那句
        ``summary``（决议 D18），所以词条本身已经说完的事别在这儿重复，否则会拼出
        「手动静音 · 手动覆盖中」这种车轱辘话。

        ``desync`` 判定**优先于**其它一切：系统 ``dndMirrored`` 与守卫期望不符时，
        先把这件事说出来（"状态自证"是 ERR 的唯一出口）。
        """
        st = self.decider.status()
        allow = bool(st["allow"])
        sys_suppress = self.backend.read()
        if sys_suppress is not None and bool(sys_suppress) != (not allow):
            expected = "允许通知" if allow else "静音"
            actual = "静音" if sys_suppress else "允许通知"
            return "desync", f"系统{actual}，守卫期望{expected}"
        if not self.automation_active:
            return "frozen", "通知由手动开关控制"
        # ``manual`` 必须先于 ``allowed`` 判：手动覆盖的**归宿是"谁在管事"**，不是覆盖成什么值
        # （设计 §6.3 场景 4 的副标题与 Switch 位无关）。放在 ``allow`` 之后会出现「刚被用户
        # 手动放行 → 报 allowed → 副标题退回第二屏词条」，覆盖态就此从面板上消失。
        if self.decider.mode == "manual":
            return "manual", ""
        if allow:
            return "allowed", ""
        return "muted", st.get("reason", "")

    def _notify_snapshot(self) -> StateReport:
        """notify：通知态——本组件的**结论**（面板行副标题里那句永不丢的话）。

        文案只有一份（决议 D18）：取值词条（声明里的默认 ``text``）+ 该取值自己的
        补充说明，合成唯一那句 ``summary``；没有补充说明时**留空 summary**，
        由框架回退到声明的默认文案（决议 D13 的覆盖语义就是这么用的）。
        """
        state, reason = self._notify_state()
        summary = f"{_NOTIFY_TEXT[state]} · {reason}" if reason else ""
        return StateReport(state, summary, updated_at=time.time())

    def _automation_snapshot(self) -> StateReport:
        """automation：自动化总闸（面板行的模式徽标已撤，它只在设置页与副标题里出现）。

        取值名之外一句话都不给 —— 两句文案就是声明里的默认 ``text``，
        报告留空即取它（决议 D13）。
        """
        state = "running" if self.automation_active else "stopped"
        return StateReport(state, updated_at=time.time())

    def _all_snapshots(self) -> dict[str, StateReport]:
        """三台机的当前快照（tick 返回值与手动即时广播共用同一份拼装）。"""
        return {
            "display": self._display_snapshot(),
            "notify": self._notify_snapshot(),
            "automation": self._automation_snapshot(),
        }

    def _publish_all(self) -> None:
        """手动操作后即时广播三台机（不等下一拍轮询）。

        走基类 ``publish_snapshot(snap, key=...)``——B 契约下插件唯一的即时广播入口，
        只可能发到本插件自己的 ``status.<key>`` topic。
        """
        for key, snap in self._all_snapshots().items():
            self.publish_snapshot(snap, key=key)
