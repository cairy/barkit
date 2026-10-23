"""tasks：编排层（任务表 → 什么时候跑哪条**同步原语**）—— 会话驱动版。

能力与编排的分工（一个字没变）
----------------------------
- **能力**（能做什么）= ``actions.ACTION_SCHEMAS``，helper 源码常量，增删需重装。
- **编排**（何时做）= app 经会话发来的 **``tasks`` 意图**，插件自由增删、零重装。

**变了的是「表从哪来」**：旧形态读用户域 ``helper_tasks.json``；现在它是 ``intent.set``
的一帧。连带消失的约定：`params_from` 的 basename 校验、1 MiB 读取上限、
「读到未知字段整份拒」当版本锁、以及文件不存在时的「沿用内置默认表」那一支 ——
intent 不存在就是「app 没声明过」，按内置默认表跑，不需要再读一次盘去确认。

**主循环从「每秒 stat 一次」变成「按最近一条任务的截止时间阻塞」**（2026-09-29）
--------------------------------------------------------------------------------
旧循环：``while True: 查 trigger → 跑到期 → sleep(1)``，空闲一天醒 86400 次。
新循环：调用方拿 :meth:`Runner.seconds_until_due` 当 ``selectors`` 的超时 ——
**没有活跃意图时返回 ``None``（纯阻塞，一次都不醒）**。

「活跃意图」的定义：某条 task 的 ``subject`` 有意图。这带来一个刻意的行为：
**没有意图的 subject 一次都不跑**。旧形态下 route/hosts 会一直按 30s 轮询、
哪怕 app 根本没在运行（拿的是盘上那份可能过期的期望态）——那是「只能拿文件当通道」
逼出来的。现在没有声明就是没有声明。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import actions
from .safeio import log

#: 内置插件 id（身份绑定红线用）。外部插件的 id 由 task 表的 ``plugins`` 字段追加。
KNOWN_PLUGIN_IDS = ("route_guard", "ipad_tunnel", "hosts_guard")

#: 领域名闭集（与 ``state.SUBJECT_*`` 同一份口径）。路径**永不**出现在这里：
#: 组件声明 subject，路径不出框架。
SUBJECTS = ("route", "tunnel", "hosts")

#: 默认 task 表（编译期常量）。等价迁移前的硬编码调度：
#: route 30s、tunnel 5s、hosts 30s；三条都只在**有意图**时才跑。
BUILTIN_TASKS = (
    {"id": "route_guard.sync", "action": "route.sync", "subject": "route",
     "interval": 30, "enabled": True},
    {"id": "ipad_tunnel.sync", "action": "tunnel.sync", "subject": "tunnel",
     "interval": 5, "enabled": True},
    {"id": "hosts_guard.sync", "action": "hosts.sync", "subject": "hosts",
     "interval": 30, "enabled": True},
)


@dataclass
class Task:
    id: str
    action: str
    subject: str
    interval: float
    enabled: bool = True
    #: 上次跑的时刻，取 :func:`time.monotonic`（**不是墙上时钟**）。它是纯进程内
    #: 记账，只用来跟 :attr:`interval` 相减算「到点没」；用 ``time.time()`` 时系统
    #: 时钟一被校回（NTP 步进 / 休眠唤醒后校正 / RTC 掉电复位），``now - last_run``
    #: 会变成负数，于是**所有轮询任务集体停摆**，直到墙上时钟追回那段差值为止。
    last_run: float = field(default=0.0, compare=False)


def _now() -> float:
    """调度用的「现在」：一律 :func:`time.monotonic`。

    全模块凡是要跟 interval 相减的地方都必须走这里 —— 墙上时钟只回答「这是何时
    的事实」，不回答「过了多久」。
    """
    return time.monotonic()


def build_tasks(table: Optional[dict]) -> list[Task]:
    """内置默认表 ← app 声明的表（按 id 覆盖/追加）。

    表非法时**整份忽略**（保留内置默认表）并记日志 —— 绝不因一张坏表让 root 侧
    失去全部保活能力。这与旧 ``load_tasks`` 的口径逐字相同，只是输入从文件变成了帧。
    """
    merged = {t["id"]: dict(t) for t in BUILTIN_TASKS}
    if isinstance(table, dict):
        plugins = table.get("plugins")
        registered = tuple(plugins) if isinstance(plugins, list) and plugins else KNOWN_PLUGIN_IDS
        declared = table.get("tasks")
        ok, reason = actions.validate_task_table(declared, registered_plugin_ids=registered)
        if not ok:
            log(f"任务表被拒绝（沿用内置默认表）：{reason}")
        else:
            for t in declared:
                merged[t["id"]] = dict(t)
            log(f"任务表生效：{len(declared)} 条声明（合计 {len(merged)} 条 task）")
    out: list[Task] = []
    for t in merged.values():
        try:
            out.append(Task(
                id=t["id"], action=t["action"], subject=t["subject"],
                interval=float(t["interval"]), enabled=bool(t.get("enabled", True)),
            ))
        except (KeyError, TypeError, ValueError) as e:
            log(f"task 声明无法构造（已跳过）：{t!r}（{e}）")
    return out


class Runner:
    """按「有意图 + 到期」跑 task；空闲时调用方可以纯阻塞。

    ``dispatch(action, raw_text)`` 与旧签名逐字相同（``raw_text`` ＝ 意图正文的 JSON
    文本），故 ``primitives`` 与三个 ops 模块一行都不用改。
    """

    def __init__(self, dispatch: Callable[[str, Optional[str]], "tuple[Optional[dict], Optional[float]]"],
                 table: Optional[dict] = None) -> None:
        self._tasks = build_tasks(table)
        self._dispatch = dispatch
        self._intents: dict[str, Any] = {}

    # ---------- 查询 ----------

    @property
    def tasks(self) -> list[Task]:
        return self._tasks

    def intent(self, subject: str) -> Any:
        return self._intents.get(subject)

    def subjects_with_intent(self) -> list[str]:
        return sorted(self._intents)

    # ---------- 意图 ----------

    def set_intent(self, subject: str, payload: Any) -> bool:
        """记下某 subject 的期望态。返回**内容是否变了**（幂等收敛的判据）。

        ``payload=None`` ＝ **撤回**。撤回不是「把这条 task 从表里删掉」——那样收尾
        那一轮就永远不会跑（隧道会一直挂在身上）。它被记成「一次待跑的收尾」：
        相关 task 立刻到期跑一轮，跑完这一轮才真正休眠（见 :meth:`run_due`）。
        """
        if subject in self._intents and self._intents[subject] == payload:
            return False
        if payload is None and subject not in self._intents:
            return False                    # 本来就没声明过：没有收尾可跑
        self._intents[subject] = payload
        for t in self._tasks:
            if t.subject == subject:
                t.last_run = 0.0        # 立刻到期
        return True

    # ---------- 表 ----------

    def apply_table(self, table: Optional[dict]) -> None:
        """换一张任务表；保留同名 task 的上次运行时刻（免得换个表就全部重跑）。"""
        prev = {t.id: t.last_run for t in self._tasks}
        self._tasks = build_tasks(table)
        for t in self._tasks:
            if t.id in prev:
                t.last_run = prev[t.id]

    # ---------- 调度 ----------

    def seconds_until_due(self) -> Optional[float]:
        """离最近一条到期还有多久；**没有任何活跃意图时返回 ``None``**。

        ``None`` 是「空闲零唤醒」的全部实现：调用方把它当 ``selectors`` 的超时，
        于是线程一直阻塞在 ``poll`` 上，一次都不醒（旧循环一天醒 86400 次）。
        """
        now = _now()
        best: Optional[float] = None
        for t in self._tasks:
            if not t.enabled or t.subject not in self._intents:
                continue
            remaining = (t.last_run + t.interval) - now
            best = remaining if best is None else min(best, remaining)
        return None if best is None else max(0.0, best)

    def run_due(self) -> None:
        """跑到期的 task（每条先看它有没有意图）。"""
        now = _now()
        for t in self._tasks:
            if not t.enabled or t.subject not in self._intents:
                continue
            if (now - t.last_run) < t.interval:
                continue
            t.last_run = now
            self._run_one(t)
            # 撤回的 subject：这一轮就是**收尾轮**，跑完即休眠（从此零唤醒）。
            if self._intents.get(t.subject, 0) is None:
                self._intents.pop(t.subject, None)

    def resync(self, subject: str) -> Optional[dict]:
        """现在按**已声明的那份意图**给某 subject 跑一轮（``resync`` 命令的落点）。

        与 :meth:`run_due` 的唯一差别是「谁来喊」：这条命令由 app 当场喊（iPad 链路刚
        就绪、用户按下「立即检查」），不等节拍。跑的仍是 task 表里那条 task 的动作，
        故「命令跑一轮」与「到点跑一轮」必然是同一件事。

        **命令里不带意图**：要收敛到什么早在 ``intent.set`` 里了，再带一份就是第二个
        真源（一个 subject 一个写者）。没声明过就如实报错 —— 不替它猜一份。
        """
        subject = str(subject or "")
        task = next((t for t in self._tasks if t.subject == subject and t.enabled), None)
        if task is None:
            raise ValueError(f"没有挂在 {subject!r} 上的 task（命令名里那个 subject 不存在）")
        if subject not in self._intents:
            raise ValueError(f"{subject!r} 还没有声明过意图（先 intent.set 再 resync）")
        if self._intents[subject] is None:
            raise ValueError(f"{subject!r} 的意图已撤回（收尾轮归节拍跑，不必 resync）")
        task.last_run = _now()
        return self._run_one(task)

    def _run_one(self, t: Task) -> Optional[dict]:
        payload = self._intents.get(t.subject)
        if payload is None:
            raw = None
        else:
            try:
                raw = json.dumps(payload, ensure_ascii=False, default=str)
            except (TypeError, ValueError) as e:
                log(f"task {t.id} 的意图无法序列化：{e}")
                return None
        state, nxt = self._dispatch(t.action, raw)
        if nxt is not None:
            try:
                t.interval = max(actions.MIN_TASK_INTERVAL, float(nxt))
            except (TypeError, ValueError):
                pass
        return state if isinstance(state, dict) else None


__all__ = ["KNOWN_PLUGIN_IDS", "SUBJECTS", "BUILTIN_TASKS", "Task",
           "build_tasks", "Runner"]
