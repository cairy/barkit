"""core/helper_tasks：把插件声明的 helper task 汇总成**一张表**送给守护（§7.4.4 / §9.1）。

分工
----
- **能力**（能做什么）= helper 源码常量 ``actions.ACTION_SCHEMAS``，改它要重装。
- **编排**（何时做 / 读哪份意图）= 本模块汇总出的那张表：插件经
  ``Plugin.resources().helper_tasks`` **声明**自己拥有的 task id，框架汇总后
  经会话发给守护（§7.4.4「插件可自由增删的是『何时做』」）。

**2026-09-29 起它不再落盘。**
----------------------------
从前它写的是用户域 ``helper_tasks.json``（一份「通道」文件：app 写、helper 每轮读）。
现在它是一条 ``intent.set("tasks", …)`` 消息 —— helper 侧在内存里按 id 合并到内置
默认表上，盘上那份文件连同它的读法一起消失。这带来两条变化：

- **「摘除」仍然必须显式写 ``enabled: false``**（这条一个字没变）。helper 的合并口径
  是「内置默认表 ← 表里按 id 覆盖」——表里**没有**某条 task 时，内置那条照样跑。
  故「卸载插件 → 摘除 task」不能靠「不发它」，必须显式关掉（与 route_guard
  ``on_disabled`` 必须逐条 ``apply=False``、不能写空 rules 是同一个坑）。
- **「发不出去」的后果变了**。旧形态「写文件失败」是弱一致的（helper 下一轮还会读，
  读到旧表就沿用旧表，行为退回迁移前）；现在守护没连上就是**没声明**，内置默认表
  兜底起作用。两个方向都安全，但**原因不同**，不要拿旧的那句来解释新的行为。

因此本模块的表是**全量枚举**：列出自已认识的全部 task，声明到的 ``enabled: true``、
其余 ``enabled: false``。比 app 认识得更多的 task（helper 新增、app 未升级）不在表
内，于是保持内置默认值 —— 向前兼容。

为什么 core 侧留一份「名级副本」
----------------------------
真源是 ``capabilities/helper/tasks.BUILTIN_TASKS``。但 ``core/`` **不得** import
``capabilities/*``（分层门禁 ``test_layer_dependency_direction_core``），故此处保留
同构副本 ``CANONICAL_TASKS``，两者的相等由
``tests/test_helper_tasks.py::test_helper_task_table_single_source`` 强制 —— 漂移即
测试失败，不存在「两处各改一半」的空间（与 ``installer.KNOWN_ROOT_BINARIES`` 对
``rootbin.ROOT_BINARIES`` 是同一手法）。
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

TABLE_VERSION = 1

#: 已知 task 的完整声明（``capabilities/helper/tasks.BUILTIN_TASKS`` 的名级副本）。
#: ``subject`` 是**领域名**（``route`` / ``tunnel`` / ``hosts``），不是文件名 ——
#: 2026-09-29 起期望态住在会话里，路径彻底离开 root 侧（见 ``actions.TASK_SUBJECTS``）。
CANONICAL_TASKS: tuple[dict, ...] = (
    {"id": "route_guard.sync", "action": "route.sync", "subject": "route",
     "interval": 30, "enabled": True},
    {"id": "ipad_tunnel.sync", "action": "tunnel.sync", "subject": "tunnel",
     "interval": 5, "enabled": True},
    {"id": "hosts_guard.sync", "action": "hosts.sync", "subject": "hosts",
     "interval": 30, "enabled": True},
)

KNOWN_TASK_IDS: tuple[str, ...] = tuple(t["id"] for t in CANONICAL_TASKS)


def declared_task_ids(plugins) -> list[str]:
    """汇总各插件的 ``helper_tasks`` 声明（按传入顺序去重保序）。

    ``resources()`` 被设计为廉价、无副作用的纯声明，但外部插件仍可能抛异常——
    单插件失败不阻断汇总（与 registry 的 isolate 口径一致）。
    """
    out: list[str] = []
    for plugin in plugins:
        try:
            res = plugin.resources()
        except Exception:  # noqa: BLE001
            log.exception("插件 %s resources() 抛异常，helper task 声明跳过",
                          getattr(plugin, "id", "?"))
            continue
        for tid in getattr(res, "helper_tasks", ()) or ():
            if isinstance(tid, str) and tid and tid not in out:
                out.append(tid)
    return out


def build_table(plugins) -> dict:
    """汇总 → ``{"version", "plugins", "tasks"}``（不发送，便于测试与预览）。"""
    declared = set(declared_task_ids(plugins))
    unknown = declared - set(KNOWN_TASK_IDS)
    for tid in sorted(unknown):
        log.warning("插件声明了未知 helper task：%s（已跳过；新增 task 须先改 helper 源码 "
                    "并 bump HELPER_VERSION 重装）", tid)
    tasks = [{**dict(t), "enabled": t["id"] in declared} for t in CANONICAL_TASKS]
    plugin_ids = sorted({pid for pid in (getattr(p, "id", "") for p in plugins) if pid})
    return {"version": TABLE_VERSION, "plugins": plugin_ids, "tasks": tasks}


def sync_task_table(plugins, link) -> "tuple[bool, str | None]":
    """把插件声明的那张表**发给守护**（``intent.set("tasks", …)``）。返回 ``(ok, error)``。

    ``link`` 是框架持有的会话客户端（``capabilities.helper_link.HelperLink``）。

    - 未接线（``link is None``，轻量测试桩 / 装配层还没起会话）⇒ ``(False, 原因)``，
      **不静默当成功**：这块表决定 root 侧跑哪些 task，悄悄不发会让「插件声明了却没生效」
      变成一件查不出来的事。
    - 会话没连上（守护未安装 / 没在跑）⇒ ``declare`` 记下意图并返回 False；守护一起来
      会自动补发（``rev`` 幂等），故这里报 ``(False, 原因)`` 但**不是错误**——
      调用方只记 info。

    幂等由 ``rev``（内容哈希）保证：同一张表重复发不会重复上线。
    """
    if link is None:
        return False, "会话未接线（框架还没起 helper 会话）"
    table = build_table(plugins)
    sent = link.declare("tasks", table)
    enabled = [t["id"] for t in table["tasks"] if t["enabled"]]
    if sent:
        log.info("helper 任务表已送达：%d 条生效（%s）", len(enabled), enabled)
    else:
        log.info("helper 会话未连上，任务表已记下待补发：%d 条生效（%s）",
                 len(enabled), enabled)
    return sent, None if sent else "守护未连接（已记下，连上即补发）"
