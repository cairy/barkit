"""doctor/checks_plugins：C 组「插件发现」检查。

**只走 ``Registry.discover()``，绝不调 ``Registry.load_all()``。**

``load_all`` 会实例化插件、给外部插件跑 ``provision`` 部署资产、调 ``plugin.start()``
起长驻服务，**并连上守护那条会话**（``core/registry.py``）—— 与 doctor 的
只读铁律直接冲突。代价是「装配期失败 / 声明校验失败」这类问题不在 doctor 重复呈现
（那是设置窗「插件」页与 ``barkit.log`` 的既有面，符合「同一信息只在一处定义」）。

换来的是 ``discover()`` 阶段**能被看见的**全部事实，且比 ``problems()`` 多补一处：
``discover()`` 里插件包导入失败只记 ``log.exception`` 就跳过（不落入 ``rejected``），
UI 上完全看不到。本组经 ``doctor.survey`` 的发现入口跑 discover，由它用临时
logging handler 把那些记录捞回来（``survey.discover`` 的第三个返回值）。
"""

from __future__ import annotations

from barkit.doctor import survey
from barkit.doctor.model import FAIL, PASS, SKIP, WARN, Check, Result, run_checks

GROUP = "plugins"


def orphan_sections(config_data: dict, known_ids: "set[str]") -> list[str]:
    """配置里声明了段、却找不到对应插件的 id（卸载残留 / 手写错 id）。"""
    declared = config_data.get("plugins") or {}
    if not isinstance(declared, dict):
        return []
    return sorted(set(declared) - set(known_ids))


def rejection_results(rejected: "list[tuple[str, str]]") -> list[Result]:
    """``Registry.rejected`` 逐条成 FAIL（原因原文来自版本闸门 / manifest 校验）。"""
    return [
        Result(f"C3.{name}", GROUP, f"被拒插件：{name}", FAIL, reason,
               "按上面的原因处理；契约版本不符时需升级插件（或降级 BarKit）")
        for name, reason in rejected
    ]


def _run_group() -> list[Result]:
    try:
        registry, found, records = survey.discover()
    except Exception as exc:  # noqa: BLE001 —— 配置不可用 ⇒ 整组说不清
        return [
            Result("C0", GROUP, "插件发现", SKIP,
                   f"配置不可用，跳过：{exc.__class__.__name__}: {exc}",
                   "先解决 B 组的配置问题再回头看插件")
        ]

    out: list[Result] = []

    internal = [pid for pid in found if registry.source_of(pid) == "internal"]
    external = [pid for pid in found if registry.source_of(pid) == "external"]
    if found:
        out.append(Result("C1", GROUP, "插件发现", PASS,
                          f"共 {len(found)} 个（内建 {len(internal)} · 外部 {len(external)}）"))
    else:
        out.append(Result("C1", GROUP, "插件发现", FAIL, "一个插件都没发现",
                          "内建插件随包分发：安装不完整时重装；或 `uv sync` 补齐依赖"))

    out.extend(
        Result("C2", GROUP, f"插件包导入失败：{record.getMessage()}", FAIL,
               str(record.exc_info[1]) if record.exc_info else record.getMessage(),
               "见 barkit.log 的完整堆栈；多为该插件依赖的系统框架缺失")
        for record in records
    )

    out.extend(rejection_results(registry.rejected))

    orphans = orphan_sections(registry.config.data, set(found))
    if orphans:
        out.append(Result("C4", GROUP, "孤儿配置段", WARN,
                          "这些 plugins 段找不到对应插件：" + "、".join(orphans),
                          "插件已卸载但配置段还在：删掉这些段（或忽略，不影响运行）"))
    else:
        out.append(Result("C4", GROUP, "孤儿配置段", PASS, "配置段与已发现插件一一对应"))

    return out


CHECKS = (Check("C", "插件发现", _run_group),)


def check_plugins() -> list[Result]:
    return run_checks(GROUP, CHECKS)
