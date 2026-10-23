"""shell/renderers/state_view：把「声明 + 报告」解析成可渲染的一格（决议 D13）。

**单一解析点**：级别 / 文案 / 取值图标 / 提醒意图只在这里合成一次。面板行、
标题栏角标的悬停清单、代表态严重度、托盘主图与托盘队列全部从这里取 —— 一处声明、
多处消费，不各算一份（状态同源，§1-4）。

三条政策集中在此，改口径只改这一个文件：

- **文案**：``StateReport.summary`` 非空则覆盖声明的 ``StateOption.text``；
  留空用默认（决议 D18 的「文案只有一份」在运行期就是这一条）。
- **取值图标**：``StateOption.icon`` 留空 → 回落该状态机的**主图标**
  ``StateSpec.icon``（不是别的组件、也不是空图标盒）。
- **未声明取值**：组件报了个没列举的取值 —— 属**写错**，按 ERR 露出并把那个取值名
  原样上屏（用户裁定：不静默吞掉，也不许看不出是哪个名字写错了）。``summary``
  在这条分支上刻意不看：要露出的正是那个名字。

失联（``stale=True``）是**框架**的判断、不是组件声明的取值，故文案
（``STALE_TEXT``）与级别（ERR）都由框架给，图标回落主图标 —— 继续画上一次的
取值图标才是撒谎。

纯函数、零 Qt 依赖，可单测。调用方自带去重日志（见 ``panel._recompute_plugin``）：
本模块不记日志，保持可被任意调用而观察不到副作用。
"""

from __future__ import annotations

from dataclasses import dataclass

from barkit.contribution.specs import IconName, StateLevel, StateReport, StateSpec

#: 失联文案。框架级兜底，不进组件声明 —— 失联说的是「这台机不吭声了」，
#: 不是它的某个业务状态。
STALE_TEXT = "状态失联"

#: 取值名为空时给个能认出来的占位（同样只在「组件写错」这条分支上用得到）。
UNDECLARED_TEXT = "未声明取值"


@dataclass(frozen=True)
class ResolvedState:
    """一格的最终形态：级别 / 文案 / 图标 / 提醒意图。

    ``declared=False`` 表示这条报告引用了一个没在 ``StateSpec.states`` 里列举的
    取值 —— 界面照样画（ERR + 取值名），诊断留痕交给调用方。

    ``alerts`` 是**解析后**的提醒意图（``StateOption.alerts_on``）：省略声明时
    按级别取默认（``WARN`` / ``ERR`` 为真）。失联与未声明取值都按 ERR 露出，
    故一律为真 —— 这两条都是「出事了」，托盘不该替组件瞒着。
    """

    level: StateLevel
    text: str
    icon: "IconName | None"
    declared: bool = True
    alerts: bool = True


def resolve(spec: StateSpec, report: StateReport, *, stale: bool = False) -> ResolvedState:
    """解析一格状态；``stale=True`` 时按「已失联」兜底（不看报告内容）。"""
    if stale:
        return ResolvedState(StateLevel.ERR, STALE_TEXT, spec.icon, alerts=True)
    opt = spec.option(report.state)
    if opt is None:
        return ResolvedState(
            StateLevel.ERR,
            report.state or UNDECLARED_TEXT,
            spec.icon,
            declared=False,
            alerts=True,
        )
    return ResolvedState(
        opt.level,
        report.summary or opt.text,
        opt.icon if opt.icon is not None else spec.icon,
        alerts=opt.alerts_on,
    )
