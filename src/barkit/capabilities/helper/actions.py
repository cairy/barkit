"""特权原语集（ACTION_SCHEMAS）+ 参数校验器 + task 表红线校验器（§7.4.4）。

能力封闭的两半
-------------
- **能力**（能做什么）= ``ACTION_SCHEMAS``，本文件的编译期常量。运行期不注册、
  不发现、不加载。新增一条原语 = 改这里 + bump ``HELPER_VERSION`` + 重装 daemon。
- **编排**（何时做）= app 经会话发来的 ``tasks`` 意图，插件可自由增删，零重装。
  开放的是调用频次与组合，不是能力本身。

五条红线（task 表的信任边界，§7.4.4）
----------------------------------
1. ``subject`` 必须在闭集内（``TASK_SUBJECTS``）—— 它决定这条 task 读哪一份意图
2. ``task.id`` 前缀必须是已注册插件 id（``<plugin_id>.<name>``）
3. 数量上限：单插件 ≤ 8、总量 ≤ 32
4. ``interval`` 下限 5s
5. **task 结构不得含 ``params`` 字段** —— 参数只能经那个 subject 的意图提供

第 5 条是「编排开放不新增能力」的成立条件：一旦允许内联 params，
「造 task = 造参数」，能力边界立刻松动。

红线 1 在 2026-09-29 换过一次对象：旧的它校验 ``params_from`` 是个合法**文件名**
（防 root 被引到数据目录外读东西）。期望态搬进会话之后 root 侧不再读任何文件，
那条校验**没有对象可作用了**；换来的是「subject 只能在闭集里选」。
放宽了？没有 —— 旧形态放行的是「数据目录内的任意文件」，新形态连文件名都不接受。

无路径参数（硬红线）
------------------
请求参数里**不得出现任何路径**。root 侧路径全部是源码内部常量。
``specs`` 刻意不提供 PathSpec/FileSpec，本模块也不允许参数名含路径语义词。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from . import rootbin, specs
from .specs import (
    CheckResult,
    ChoiceSpec,
    CidrSpec,
    IfaceSpec,
    Ipv4Spec,
    ListSpec,
    ServiceSpec,
    SsidSpec,
    TextSpec,
)

# ──────────────────────────────────────────────
# 能力集（编译期常量，运行期只读）
# ──────────────────────────────────────────────

#: ``file.write_block`` 的服务对象是编译期常量：当前只服务 /etc/hosts 一个目标。
#: 让 marker 隐式决定写到哪个文件，等于把 marker 变成路径的替身，「无路径参数」
#: 这条红线会被架空（§7.4.4）。需要第二个受 root 保护的文件时，按新增原语处理。
FILE_BLOCK_TARGET = "/etc/hosts"

#: ``net.resolve`` 允许的解析方法（与 app 侧 ``plugins/hosts_guard/models.METHODS``
#: 的枚举一致；新增方法须两侧同步）。S3b-2 起解析链归 app，helper 侧不再持有
#: 端点表，本常量只用于约束请求取值。
RESOLVE_METHODS: Tuple[str, ...] = (
    "auto", "doh-pub", "doh-ali", "udp-ali", "udp-tx", "system",
)

#: ``proc.manage`` 允许操作的进程名，从 A 类封闭集派生（不另立真源）。
PROC_NAMES: Tuple[str, ...] = tuple(sorted(rootbin.known_names()))

ACTION_SCHEMAS: Dict[str, Dict[str, specs.Spec]] = {
    "file.write_block": {
        "marker": TextSpec(pattern=r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"),
        "content": TextSpec(pattern=r"[^\x00]*", min_len=0, max_len=1 << 20),
    },
    "route.ensure": {
        "dest": CidrSpec(),
        "gateway": Ipv4Spec(),
        "iface": IfaceSpec(),
    },
    "route.remove": {
        "dest": CidrSpec(),
        "gateway": Ipv4Spec(),
    },
    "dns.set": {
        "service": ServiceSpec(),
        "servers": ListSpec(Ipv4Spec(), max_items=4),
    },
    "dns.restore": {},
    "net.iface_query": {
        "iface": IfaceSpec(),
    },
    "net.resolve": {
        "host": specs.HostnameSpec(),
        "method": ChoiceSpec(RESOLVE_METHODS),
    },
    "wifi.select": {
        "ssid": SsidSpec(),
    },
    "proc.manage": {
        "name": ChoiceSpec(PROC_NAMES),
        "action": ChoiceSpec(("start", "stop", "status")),
    },
    # ── 同步原语（task 表驱动的业务入口）──────────────────────────────────
    # 参数 = ``subject`` 那份意图正文（会话里最近一次 ``intent.set`` 的 payload）；
    # 其内部 schema 由各机制模块
    # （routeops.validate_intent / tunnelops.validate_tunnel / hostsops.load_hosts_rules）
    # 负责——那里校验更严，且与业务语义同处一地。此处空 schema 表示「参数形状
    # 不在本层约束」。能力封闭仍成立：action 名是编译期常量，运行期无法引入新的
    # action，subject 也随之固定在闭集里（无「按 type 分发」的可扩展列表）。
    "route.sync": {},
    "tunnel.sync": {},
    "hosts.sync": {},
}

#: 明确禁止出现在能力集里的「命令类」名字（门禁扫描用）。任何形态的
#: run_command / exec / script / eval 一律不存在（§7.4.4）。
FORBIDDEN_ACTION_WORDS = (
    "exec", "eval", "shell", "script", "command", "run", "spawn",
    "system", "popen", "import", "load",
)


def action_names() -> frozenset:
    return frozenset(ACTION_SCHEMAS)


def validate_params(action: str, params: Any) -> CheckResult:
    """校验一条原语的参数：未知 action / 未知字段 / 缺字段 / 值域 —— 任一不过即整份拒绝。"""
    schema = ACTION_SCHEMAS.get(action)
    if schema is None:
        return False, f"未知原语：{action!r}（封闭集 {sorted(ACTION_SCHEMAS)}）"
    return specs.check_fields(schema, params)


# ──────────────────────────────────────────────
# task 表（编排层）红线校验
# ──────────────────────────────────────────────

MAX_TASKS_PER_PLUGIN = 8
MAX_TASKS_TOTAL = 32
MIN_TASK_INTERVAL = 5.0

TASK_KEYS = frozenset({"id", "action", "subject", "interval", "enabled"})
TASK_ID_RE = re.compile(r"([a-z0-9_]+)\.([a-z0-9_]+)")

#: ``subject`` 的闭集（与 ``tasks.SUBJECTS`` / ``state.SUBJECT_*`` 同一份口径）。
#: **它取代了旧的 ``params_from``**：旧形态里 task 指向一个**文件名**、helper 每轮
#: 去读它；现在期望态在会话里，task 只指向一个**领域名**，路径彻底离开 root 侧。
TASK_SUBJECTS = ("route", "tunnel", "hosts")


def validate_task(
    task: Any,
    *,
    registered_plugin_ids: Sequence[str] = (),
    plugin_task_counts: Optional[Mapping[str, int]] = None,
    total_tasks: int = 0,
) -> CheckResult:
    """校验一条 task 声明。返回 ``(ok, reason)``；reason 供状态 / 日志展示。"""
    if not isinstance(task, dict):
        return False, f"task 需为对象：{task!r}"

    # 红线 5：结构不得含内联参数（先于未知字段检查，给出更准确的提示）
    if "params" in task:
        return False, "task 不得含内联 params（参数只能经 subject 的意图提供）"

    unknown = sorted(set(task) - TASK_KEYS)
    if unknown:
        return False, f"task 含未知字段：{unknown}"

    tid = task.get("id")
    if not isinstance(tid, str) or not tid:
        return False, f"task.id 需为非空字符串：{tid!r}"

    # 红线 2：身份绑定 —— 攻击者可造 task，但不能伪造插件身份
    m = TASK_ID_RE.fullmatch(tid)
    if m is None:
        return False, f"task.id 需形如 <plugin_id>.<name>：{tid!r}"
    plugin_id = m.group(1)
    if plugin_id not in set(registered_plugin_ids):
        return False, f"task.id 前缀不是已注册插件：{plugin_id!r}"

    action = task.get("action")
    if not isinstance(action, str) or action not in ACTION_SCHEMAS:
        return False, f"task.action 不在封闭集：{action!r}"

    # 红线 1（取代旧的 basename 校验）：subject 必须在闭集里。
    # 闭集而不是「任意字符串」是刻意的：subject 决定这条 task 去读哪一份意图，
    # 放开它等于让 task 表指向任意内部状态。
    subject = task.get("subject")
    if subject not in TASK_SUBJECTS:
        return False, f"task.subject 不在闭集 {list(TASK_SUBJECTS)}：{subject!r}"

    # 红线 4
    interval = task.get("interval")
    if isinstance(interval, bool) or not isinstance(interval, (int, float)):
        return False, f"task.interval 需为数值：{interval!r}"
    if interval < MIN_TASK_INTERVAL:
        return False, f"task.interval 低于下限 {MIN_TASK_INTERVAL:g}s：{interval!r}"

    if "enabled" in task and not isinstance(task["enabled"], bool):
        return False, f"task.enabled 需为布尔：{task['enabled']!r}"

    # 红线 3
    counts = plugin_task_counts or {}
    if counts.get(plugin_id, 0) >= MAX_TASKS_PER_PLUGIN:
        return False, f"插件 {plugin_id} 的 task 数已达上限 {MAX_TASKS_PER_PLUGIN}"
    if total_tasks >= MAX_TASKS_TOTAL:
        return False, f"task 总量已达上限 {MAX_TASKS_TOTAL}"
    return True, None


def validate_task_table(
    tasks: Any, *, registered_plugin_ids: Sequence[str] = ()
) -> CheckResult:
    """整表校验：逐条过红线 + id 唯一 + 分别累计单插件/总量配额。"""
    if not isinstance(tasks, list):
        return False, f"tasks 需为数组：{tasks!r}"
    counts: Dict[str, int] = {}
    seen = set()
    for idx, task in enumerate(tasks):
        ok, reason = validate_task(
            task,
            registered_plugin_ids=registered_plugin_ids,
            plugin_task_counts=counts,
            total_tasks=idx,
        )
        if not ok:
            return False, f"第 {idx} 条：{reason}"
        tid = task["id"]
        if tid in seen:
            return False, f"第 {idx} 条：task.id 重复：{tid!r}"
        seen.add(tid)
        counts[tid.split(".", 1)[0]] = counts.get(tid.split(".", 1)[0], 0) + 1
    return True, None
