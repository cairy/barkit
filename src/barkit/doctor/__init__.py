"""barkit.doctor：`barkit doctor` 的引擎 —— 环境自检。

三条铁律（改本包前先读，动手细节见 ``docs/log/2026-09-17-barkit-doctor.md``）：

1. **只读**。全程唯一写盘是 A 组的可写性探针（写后即删）。不调
   ``Registry.load_all()``（会 provision ＋ start ＋ 连上守护那条会话）、
   不调 ``helper_client.run_admin()``（提权）、不建 QApplication、不抢单实例锁。
2. **不许崩**。自检工具自身的 traceback 最伤信任：顶层模块 / 各检查组一律**惰性
   import ＋ 逐组 try/except**，失败降级成该组的 FAIL；组内每条检查再由
   ``model.run_checks`` 单独隔离。
3. **本模块与 ``probes`` / ``model`` / ``render`` 运行期不得 import PySide6**——
   「PySide6 装没装」本身就是 A 组的检查项，引擎顶层依赖它就没法报出来。
   由 ``tests/test_architecture.py::test_doctor_engine_is_qt_free`` 守。

分组
----
``env`` 地基 → ``config`` → ``plugins``（发现层）→ ``data``（受管文件与日志）
→ ``helper``（特权组件与 A 类资产）→ ``permissions``。C / D / E / F 四组都要
「只读地看一眼插件声明」，那份共用探针在 ``doctor/survey.py``。
"""

from __future__ import annotations

import argparse
import importlib
import sys
from dataclasses import dataclass
from typing import Sequence

from barkit.doctor import probes, render
from barkit.doctor.model import FAIL, SKIP, Report, Result

__all__ = ["GROUPS", "collect", "run"]


@dataclass(frozen=True)
class GroupSpec:
    """一个检查组：显示名 ＋ ``"模块:函数名"`` ＋ 是否只在 macOS 上有意义。"""

    key: str
    title: str
    target: str
    darwin_only: bool


#: 检查表（顺序即输出顺序：先地基、再插件、再数据、最后特权组件与权限）。
#:
#: ``darwin_only`` 只标**真的平台相关**的组：``data`` 组虽然文件都在 macOS 的
#: 用户域下，但它的逻辑是纯文件检查 —— 非 macOS 上跑出来的「文件不存在 ⇒ SKIP」
#: 是**诚实结论**，没必要整组跳过（``config`` 组同理）。
GROUPS: tuple[GroupSpec, ...] = (
    GroupSpec("env", "运行环境", "barkit.doctor.probes:check_env", False),
    GroupSpec("config", "配置", "barkit.doctor.checks_config:check_config", False),
    GroupSpec("plugins", "插件发现", "barkit.doctor.checks_plugins:check_plugins", True),
    GroupSpec("data", "受管数据与日志", "barkit.doctor.checks_data:check_data", False),
    GroupSpec("helper", "helper 与 root 资产",
              "barkit.doctor.checks_helper:check_helper", True),
    GroupSpec("permissions", "系统权限",
              "barkit.doctor.checks_permissions:check_permission_group", True),
)


def _load(target: str):
    module_name, _, func_name = target.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, func_name)


def collect(groups: "Sequence[GroupSpec] | None" = None) -> Report:
    """跑完所有检查组。**本函数不打印、不退出**，纯收集（便于测试与将来复用）。"""
    specs = GROUPS if groups is None else groups
    report = Report()
    darwin = probes.is_darwin()
    for spec in specs:
        report.declare_group(spec.key, spec.title)
        if spec.darwin_only and not darwin:
            report.add(
                Result(spec.key, spec.key, f"{spec.title}（macOS 专属）", SKIP,
                       f"当前平台 {sys.platform}，跳过本组",
                       "在 macOS 上跑才会得到结论")
            )
            continue
        try:
            check = _load(spec.target)
            report.add(*check())
        except Exception as exc:  # noqa: BLE001 —— 见模块 docstring 第 2 条
            report.add(
                Result(spec.key, spec.key, spec.title, FAIL,
                       f"检查组无法运行：{exc.__class__.__name__}: {exc}",
                       "多为依赖残缺或环境异常；完整堆栈见 barkit.log")
            )
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="barkit doctor",
        description="BarKit 自检：只读、不需要 root、不需要 GUI，可与 BarKit 同时运行。",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读的 JSON")
    parser.add_argument("--verbose", action="store_true",
                        help="连 PASS 项也打印明细（默认只细说有问题的）")
    return parser


def run(argv: "Sequence[str]") -> int:
    """执行自检并打印结果。返回进程退出码：``0`` 无 FAIL ／ ``2`` 用法错。"""
    try:
        args = _parser().parse_args(list(argv))
    except SystemExit as exc:            # argparse 自己已打印帮助 / 用法
        return 0 if exc.code in (0, None) else 2

    report = collect()
    if args.json:
        print(render.render_json(report))
    else:
        print(render.render_text(report, verbose=args.verbose))
    return report.exit_code
