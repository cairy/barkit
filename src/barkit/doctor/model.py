"""doctor/model：自检结果的**唯一数据结构**与逐条隔离的执行器。

零依赖（标准库之外什么都不 import），因此可以被任何层安全引用，也是
``doctor`` 引擎能保持 Qt-free 的地基（见 ``内部 doctor 设计记录`` §4）。

级别只有四个，语义写死：

- ``PASS`` 检查通过；
- ``WARN`` 能用，但不对（用户可自行决定是否处理）；
- ``FAIL`` 功能已坏，必须处理；
- ``SKIP`` 前置缺失，说不清（**不许拿 SKIP 掩盖 FAIL**）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"

#: 严重度排序用（决定「整体结论」取哪一级）。
SEVERITY = {PASS: 0, SKIP: 1, WARN: 2, FAIL: 3}


@dataclass(frozen=True)
class Result:
    """一条检查结果。

    ``id``   稳定标识（``A1`` / ``A3.PySide6`` / ``C2`` …），便于对照文档与 issue；
    ``group`` 所属组 key（``env`` / ``config`` / ``plugins`` …）；
    ``hint`` 人类可读的「下一步做什么」——**没有 hint 的 FAIL 等于没报**。
    """

    id: str
    group: str
    title: str
    level: str
    detail: str = ""
    hint: str = ""

    @property
    def severity(self) -> int:
        return SEVERITY.get(self.level, 0)


@dataclass
class Report:
    """一次自检的全部结果。**只装数据、不做 I/O**，渲染见 ``doctor/render``。"""

    results: list[Result] = field(default_factory=list)
    groups: dict[str, str] = field(default_factory=dict)   # 组 key → 显示名（按声明顺序）

    def add(self, *results: Result) -> None:
        self.results.extend(results)

    def declare_group(self, key: str, title: str) -> None:
        self.groups[key] = title

    def count(self, level: str) -> int:
        return sum(1 for r in self.results if r.level == level)

    @property
    def worst(self) -> str:
        if not self.results:
            return SKIP
        return max(self.results, key=lambda r: r.severity).level

    @property
    def failures(self) -> list[Result]:
        return [r for r in self.results if r.level == FAIL]

    @property
    def exit_code(self) -> int:
        """``0`` 无 FAIL（可含 WARN / SKIP）／``1`` 有 FAIL。"""
        return 1 if self.failures else 0


@dataclass(frozen=True)
class Check:
    """一条命名的检查项（``run`` 返回**结果列表**，便于一条检查产出多条结果）。"""

    id: str
    title: str
    run: Callable[[], list[Result]]


def run_checks(group: str, checks: Iterable[Check]) -> list[Result]:
    """逐条跑，**单条抛异常只毁自己**。

    自检工具崩在半路比不检查更伤信任：一条检查内部炸了（环境怪、签名变了、
    依赖残缺），要变成**那一条的 FAIL**，其余照跑完。
    """

    out: list[Result] = []
    for check in checks:
        try:
            out.extend(check.run())
        except Exception as exc:  # noqa: BLE001 —— 见 docstring：隔离边界就在这一行
            out.append(
                Result(
                    check.id, group, check.title, FAIL,
                    f"检查自身抛异常：{exc.__class__.__name__}: {exc}",
                    "这是 doctor 的缺陷，请把本行连同 barkit.log 一并反馈",
                )
            )
    return out
