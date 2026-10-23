"""doctor/probes：A 组「运行环境」检查。

**本模块运行期不得 import PySide6 / shiboken6**（门禁
``test_doctor_engine_is_qt_free`` 守），也不许 import ``barkit`` 的重层模块
（只许 ``paths``）。理由：全链路代码都依赖 PySide6 与 pyobjc，检查「它们装没装」
的代码**自己是不能依赖它们的** —— 否则在最需要 doctor 的场景里 doctor 先死了。
"""

from __future__ import annotations

import importlib
import os
import platform
import sys
from pathlib import Path

from barkit import paths
from barkit.doctor.model import FAIL, PASS, WARN, Check, Result, run_checks

GROUP = "env"

MIN_PYTHON = (3, 12)

#: 可写性探针文件名。**doctor 全程唯一会写的盘**，写完立刻删（见 _writable_probe）。
PROBE_NAME = ".doctor-write-probe"

#: 逐个 try-import 的依赖：(模块名, 用途, 缺失时怎么办)
DEPENDENCIES: tuple[tuple[str, str, str], ...] = (
    ("yaml", "配置 YAML 解析", "`uv sync` 重装依赖"),
    ("PySide6.QtWidgets", "界面（菜单栏 / 面板 / 设置窗）", "`uv sync`；装不上多为 Python 版本与 wheel 不匹配"),
    ("objc", "pyobjc 运行时（系统集成的地基）", "`uv sync`（pyobjc 只在 macOS 上有 wheel）"),
    ("Cocoa", "pyobjc · Cocoa（窗口层级 / 应用激活策略）", "`uv sync`"),
    ("Quartz", "pyobjc · Quartz（屏幕与事件）", "`uv sync`"),
    ("ApplicationServices", "辅助功能权限查询", "`uv sync`"),
)


def is_darwin() -> bool:
    """平台判据的**唯一出口**（测试靠它注入非 macOS 场景）。"""
    return sys.platform == "darwin"


def _writable_probe(directory: Path) -> "str | None":
    """在目录里建一个探针文件、**立刻删掉**。返回 None = 可写可清，否则失败原因。

    这是 doctor 唯一的写盘动作，且只写自己家（``APP_SUPPORT`` / ``LOG_DIR``）——
    绝不往别人的目录里试写。

    **两步都要成**（2026-09-29 改）：只「建得出来」还不算可用 —— 建得出、删不掉的目录会
    留下一份 0 字节残留，从此「这个目录里该有哪些文件」就再也说不清（沙箱里实测过这种
    目录：``write_text`` 放行、``unlink`` 被拒）。故**收尾失败也报成不可写**：把话说出来，
    而不是把残渣留在那儿当无主之物。

    文件是**点号开头**的临时名 —— 万一真被拒，它至少不像一份正经数据文件。
    """
    probe = directory / PROBE_NAME
    try:
        probe.write_text("", encoding="utf-8")
    except OSError as exc:
        return f"{exc.__class__.__name__}: {exc}"
    try:
        probe.unlink()
    except OSError as exc:
        return (f"建得出来但删不掉（会留下残留 {PROBE_NAME}）："
                f"{exc.__class__.__name__}: {exc}")
    return None


def check_python() -> list[Result]:
    current = sys.version_info[:3]
    detail = f"当前 {platform.python_version()}（要求 ≥ {'.'.join(map(str, MIN_PYTHON))}）"
    if current >= MIN_PYTHON:
        return [Result("A1", GROUP, "Python 版本", PASS, detail)]
    return [
        Result("A1", GROUP, "Python 版本", FAIL, detail,
               f"装 Python {'+'.join(map(str, MIN_PYTHON))} 以上后重建 venv："
               f"`uv python install {'.'.join(map(str, MIN_PYTHON))}`")
    ]


def check_platform() -> list[Result]:
    if is_darwin():
        return [Result("A2", GROUP, "运行平台", PASS, platform.platform())]
    return [
        Result("A2", GROUP, "运行平台", FAIL,
               f"当前 {sys.platform}，BarKit 是 macOS 菜单栏应用",
               "本机只能写代码、不能跑：界面 / 热键 / 窗口层级 / helper 全部依赖 macOS。"
               "验证请在 macOS 上执行")
    ]


def check_dependencies() -> list[Result]:
    """逐个 try-import。**每个缺的包一条 FAIL**（比一条聚合结果好定位）。"""
    missing: list[Result] = []
    for module, purpose, hint in DEPENDENCIES:
        try:
            importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001 —— 任何导入失败都算这条不过
            missing.append(
                Result(f"A3.{module}", GROUP, f"依赖：{module}", FAIL,
                       f"无法导入（{purpose}）：{exc.__class__.__name__}: {exc}", hint)
            )
    if missing:
        return missing
    return [
        Result("A3", GROUP, "运行期依赖", PASS,
               f"{len(DEPENDENCIES)} 个依赖全部可导入")
    ]


def _check_dir_writable(check_id: str, label: str, directory: Path) -> Result:
    """目录可写性。**目录不存在时不替用户创建** —— 只探最近的已存在祖先。"""
    if directory.exists() and not directory.is_dir():
        return Result(check_id, GROUP, label, FAIL,
                      f"{directory} 存在但不是目录",
                      "把它移走或改名后重新运行")
    if directory.exists():
        reason = _writable_probe(directory)
        if reason is None:
            return Result(check_id, GROUP, label, PASS, f"{directory} 可写")
        return Result(check_id, GROUP, label, FAIL,
                      f"{directory} 不可写：{reason}",
                      f"修正属主 / 权限：`sudo chown -R \"$USER\" '{directory}'`")
    parent = next((p for p in directory.parents if p.exists()), None)
    if parent is None or not os.access(parent, os.W_OK):
        return Result(check_id, GROUP, label, FAIL,
                      f"{directory} 不存在，其祖先目录也不可写",
                      "检查用户主目录的权限")
    return Result(check_id, GROUP, label, WARN,
                  f"{directory} 尚未创建（首次启动会自建；祖先目录 {parent} 可写）",
                  "属正常：BarKit 首次启动会创建它")


def check_app_support() -> list[Result]:
    return [_check_dir_writable("A4", "应用支持目录", paths.APP_SUPPORT)]


def check_log_dir() -> list[Result]:
    return [_check_dir_writable("A5", "日志目录", paths.LOG_DIR)]


def check_lock() -> list[Result]:
    """只有提示性含义：doctor **不抢单实例锁**，BarKit 正跑着也能执行。"""
    lock = paths.APP_SUPPORT / ".lock"
    if not lock.exists():
        return [Result("A6", GROUP, "单实例锁", PASS, "无锁文件，BarKit 未在运行")]
    try:
        pid = lock.read_text(encoding="utf-8").strip() or "（空）"
    except OSError as exc:
        pid = f"读取失败（{exc.__class__.__name__}）"
    return [
        Result("A6", GROUP, "单实例锁", WARN, f"锁文件存在，记录的 pid = {pid}",
               "若 BarKit 正在运行则属正常；doctor 只读且不抢锁，可继续")
    ]


CHECKS = (
    Check("A1", "Python 版本", check_python),
    Check("A2", "运行平台", check_platform),
    Check("A3", "运行期依赖", check_dependencies),
    Check("A4", "应用支持目录", check_app_support),
    Check("A5", "日志目录", check_log_dir),
    Check("A6", "单实例锁", check_lock),
)


def check_env() -> list[Result]:
    return run_checks(GROUP, CHECKS)
