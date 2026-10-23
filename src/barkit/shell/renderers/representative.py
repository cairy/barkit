"""shell/renderers/representative：每插件代表态派生（UI 规范 §4.3）。

代表态是「标题栏角标」与「L2 健康角标」的**同一份数据源**（§4.3 杜绝两套
健康表达）：取该插件全部状态的报告中最坏级别；严重度相同时按**关注度顺序**取 ——
默认是作者写的**声明顺序**（谁写在前面谁优先），用户可在设置里给某台状态机配
``priority`` 覆盖它（**数值小者优先**）。插件不参与位置决策，全由框架算。

级别不直接从报告读（决议 D13：报告只报取值名），经 ``state_view.resolve`` 回查
该取值的声明 —— 与面板、托盘走**同一个**解析点，三处不可能算出不同级别。

**前置条件**（框架侧保证，改了会静默改变代表态）：``states`` 的迭代顺序必须等于
声明顺序。今天的来源链是 ``ContributionRegistry.states()``（按声明顺序收集、
不排序）→ ``panel._state_specs``（``{s.topic: s for s in …}`` 保序）→ 本函数；
测试 ``test_representative_tie_breaks_by_declaration_order`` 守着这条链。
"""

from __future__ import annotations

from collections.abc import Mapping

from barkit.contribution.specs import StateReport, StateSpec
from barkit.shell.design.levels import SEVERITY
from barkit.shell.renderers.state_view import resolve


def _priority_of(
    priorities: "Mapping[str, int] | None", spec: StateSpec, fallback: int
) -> int:
    """这台状态机的关注度序号：**数值小者优先**。

    ``priorities`` 按状态机 key 键控（一台机时的保留键 ``"main"``）；没配 / 配坏了
    → 回落 ``fallback``（声明序号）。写坏的优先级在这里被**忽略而不是抛错**：代表态
    是「该显示谁」的渲染判据，一个手改歪的 YAML 数字不该让整枚角标画不出来 —— 与
    ``Config.plugin_state_priorities`` 读侧「非法一律忽略」同一口径（那里已滤过一遍
    bool / 非整数，这里是第二道：调用方可能自己传 dict）。
    """
    if not priorities:
        return fallback
    raw = priorities.get(spec.key or "main")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return fallback
    return raw


def representative(
    plugin_id: str,
    reports: Mapping[str, StateReport],
    states: Mapping[str, StateSpec],
    priorities: "Mapping[str, int] | None" = None,
) -> "tuple[StateSpec, StateReport] | None":
    """返回 ``(该插件的代表 StateSpec, 对应报告)``；无报告/无声明返回 None。

    - ``reports`` / ``states``：均按 StateSpec.topic（``plugin.<id>.status.<key>``）键控；
    - 选代表：**最坏级别优先**（级别经声明解析，见模块 docstring）；同级再比关注度
      序号（见 :func:`_priority_of`），**小者胜**；同级同序号时保留先到者 —— 迭代
      顺序 = 声明顺序，故「都没配」与旧行为（平局取先声明）**逐字节等价**；
    - ``priorities`` 省略 / ``None`` ⇒ 纯声明顺序：既有调用点不传即旧行为，零回归；
    - 「无状态」的渲染兜底由调用方决定，本函数不做语义假设。
    """
    best: "tuple[int, int, StateSpec, StateReport] | None" = None
    for index, (identity, spec) in enumerate(states.items()):
        if spec.plugin_id != plugin_id:
            continue
        # 报告与规范用同一 identity（topic）键控：真实为 plugin.<id>.status.<key>，
        # 契约测试可用任意简键（如 p.a）；均以字典键对齐查找。
        report = reports.get(identity)
        if report is None:
            continue
        severity = SEVERITY[resolve(spec, report).level]
        priority = _priority_of(priorities, spec, index)
        # 严重度严格大于才换；同级比关注度序号（小者胜），同序号保留先到者
        if best is None or severity > best[0] or (
            severity == best[0] and priority < best[1]
        ):
            best = (severity, priority, spec, report)
    if best is None:
        return None
    return best[2], best[3]
