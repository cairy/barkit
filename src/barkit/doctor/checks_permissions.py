"""doctor/checks_permissions：F 组「系统权限」检查（**数据驱动**）。

设计要点：**不硬编码任何插件名**。遍历各插件**类上声明的**
``required_permissions``（``core/plugin.py``），逐个查探针表 :data:`PROBES`。
好处是插件新增一条权限声明时，doctor 不需要同步改代码；坏处是探针表会落后于
声明面 —— 所以 :func:`check_permissions` 对**未知权限键**必须报 WARN，
**不许静默跳过**（静默跳过 = 声明面扩容后 doctor 假装一切正常）。

读声明不需要实例化插件（``required_permissions`` 是类属性），因此「某插件实例化
失败」不会让它声明的权限变成一桩看不见的事。

整组 macOS 专属（辅助功能是 macOS 概念）。
"""

from __future__ import annotations

from typing import Callable

from barkit.doctor import survey
from barkit.doctor.model import FAIL, PASS, SKIP, WARN, Check, Result, run_checks

GROUP = "permissions"

Probe = Callable[[str, str], Result]


def _probe_accessibility(key: str, who: str) -> Result:
    """``accessibility``：辅助功能（AX）授权。

    ``available()`` 与 ``accessibility_trusted()`` 必须分开读：
    「问不出来」和「没授权」是两件事，前者 SKIP、后者才算未授权。
    """
    from barkit.core import ax_trust

    title = f"辅助功能授权（{who}）"
    if not ax_trust.available():
        return Result(f"F1.{key}", GROUP, title, SKIP,
                      "本机没有可用的 AX 查询接口（pyobjc 的 ApplicationServices 缺失）",
                      "这是「问不出来」而非「没授权」：先解决 A3 的依赖问题")
    if ax_trust.accessibility_trusted():
        return Result(f"F1.{key}", GROUP, title, PASS, "已授权")
    return Result(f"F1.{key}", GROUP, title, FAIL,
                  f"未授权 —— 声明它的插件（{who}）的核心功能会不可用",
                  "系统设置 → 隐私与安全性 → 辅助功能，勾选实际运行 BarKit 的二进制"
                  "（开发期是终端）。命令行直达："
                  f"`open {ax_trust.ACCESSIBILITY_PANE_URL}`。"
                  "不用「一键输入」这类插件的话可以忽略本条")


#: 已知权限键 → 探针。**新增权限声明必须同步在此登记**，否则该键会走 F2 的
#: 「无探针」分支（WARN，不静默）。
PROBES: dict[str, Probe] = {
    "accessibility": _probe_accessibility,
}


def _declared_by_key() -> "tuple[dict[str, list[str]], str | None]":
    """``{权限键: [声明它的插件 id]}``；失败时返回原因（由调用方转成 SKIP）。"""
    try:
        _, found, _ = survey.discover()
    except Exception as exc:  # noqa: BLE001
        return {}, f"插件发现不可用：{exc.__class__.__name__}: {exc}"
    by_key: dict[str, list[str]] = {}
    for pid, keys in survey.declared_permissions(found).items():
        for key in keys:
            by_key.setdefault(key, []).append(pid)
    return by_key, None


def check_permissions() -> list[Result]:
    by_key, why = _declared_by_key()
    if why is not None:
        return [Result("F0", GROUP, "系统权限", SKIP, why,
                       "先解决 C 组的问题再回头看权限")]
    if not by_key:
        return [Result("F1", GROUP, "系统权限", PASS,
                       "没有任何插件声明额外系统权限")]

    out: list[Result] = []
    for key in sorted(by_key):
        who = "、".join(by_key[key])
        probe = PROBES.get(key)
        if probe is None:
            # 声明面扩容了但探针表没跟上：必须说出来，不许静默跳过。
            out.append(Result(f"F2.{key}", GROUP, f"未知权限键：{key}", WARN,
                              f"{who} 声明了 {key!r}，但 doctor 没有对应探针，无法自检",
                              "多为主程序比插件旧：升级 BarKit；若仍无探针，"
                              "这是 doctor 的缺口，请连同本条一起反馈"))
            continue
        out.append(probe(key, who))
    return out


CHECKS = (Check("F", "系统权限", check_permissions),)


def check_permission_group() -> list[Result]:
    return run_checks(GROUP, CHECKS)
