"""core/rootbin_reconcile：A 类 root 二进制的**无状态对账**（方案 §9.3，批次 4 · S4）。

口径（写死，勿改）
----------------
- ``wanted``  = ∪ 所有【已安装】插件（**含 disabled / failed**）的
  ``resources().root_binaries``。已安装的判据是「discover 成功且实例化过」，与
  validate / 运行期状态无关 —— 外部插件升级失败时旧版**仍在跑**，若 failed 不计入，
  对账会删掉正在使用的 A 类资产。**资产回收宁可延迟，不可误删。**
- ``deployed`` = 系统域实际存在的、**登记在册**的 A 类文件名。
- 回收对象是差集 ``deployed − wanted``；部署对象是 ``wanted − deployed``。

**对账只在安装与卸载触发**，禁用/启用不触发。按「已启用」推演会出事：禁用
ipad_tunnel（高频可逆操作）→ wanted 掉 hev → 删 hev（需 root + 授权）→ 重新启用
→ 再部署 → **再弹一次授权**。对称表述：**禁用降级的是运行状态，不是资产存在性。**

本模块只做纯计算
--------------
``reconcile`` 是**纯函数**（无 I/O、无外部状态），安装与卸载共用同一口径；
root 动作由唯一授权入口 ``capabilities/helper_client.py`` 执行。分层门禁禁止
``core/`` 依赖 ``capabilities/*``，故此处不 import ``rootbin`` —— ``deployed`` 只按
``installer.KNOWN_ROOT_BINARIES`` 这个**名级副本**过滤；两份名集的相等由
``tests/test_helper_contract.py::test_root_binary_closed_set_single_source`` 强制。
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from typing import Iterable

from barkit.core.installer import KNOWN_ROOT_BINARIES

log = logging.getLogger(__name__)

#: A 类二进制在插件包内的分发目录（``<插件包>/resources/<name>``）。
RESOURCES_DIRNAME = "resources"


def declaration_of(plugin) -> tuple[str, ...]:
    """读一个插件的 ``resources().root_binaries`` 声明（异常 → 空，单插件不阻断汇总）。"""
    try:
        res = plugin.resources()
    except Exception:  # noqa: BLE001 —— 与 registry 的 isolate 口径一致
        log.exception("插件 %s resources() 抛异常，A 类声明跳过",
                      getattr(plugin, "id", "?"))
        return ()
    names = getattr(res, "root_binaries", ()) or ()
    return tuple(n for n in names if isinstance(n, str) and n)


def declared(plugins: Iterable) -> dict[str, str]:
    """``{name: 首个声明它的插件 id}``。多个插件声明同一名字是合法冗余（取并集）。"""
    out: dict[str, str] = {}
    for plugin in plugins:
        pid = getattr(plugin, "id", "") or type(plugin).__name__
        for name in declaration_of(plugin):
            out.setdefault(name, pid)
    return out


def wanted(plugins: Iterable) -> frozenset[str]:
    """全部已安装插件声明的 A 类名集（对账 wanted 侧）。"""
    return frozenset(declared(plugins))


def package_dir(plugin) -> "Path | None":
    """插件包目录（``resources/`` 的父目录）；定位不到返回 None。"""
    try:
        file = inspect.getfile(type(plugin))
    except (TypeError, OSError):  # 桩对象 / 内建类
        return None
    return Path(file).resolve().parent


def resolve_sources(plugins: Iterable) -> "tuple[dict[str, Path], dict[str, str]]":
    """**按声明解析源文件**：返回 ``({name: 源路径}, {name: 缺失原因})``。

    安装器只部署 ``sources``；``missing`` 非空即**拒绝安装**（不做静默降级 ——
    降级路径就是提权路径）。解析规则：``<声明插件的包目录>/resources/<name>``，
    **绝不猜测路径**（猜错 = 让 root 执行一个非预期文件）。
    """
    sources: dict[str, Path] = {}
    missing: dict[str, str] = {}
    found: dict[str, tuple[str, "Path | None"]] = {}
    for plugin in plugins:
        pid = getattr(plugin, "id", "") or type(plugin).__name__
        base = package_dir(plugin)
        for name in declaration_of(plugin):
            found.setdefault(name, (pid, base))
    for name, (pid, base) in sorted(found.items()):
        if name not in KNOWN_ROOT_BINARIES:
            missing[name] = f"未登记的 root 二进制（由 {pid} 声明，封闭集见 installer）"
            continue
        if base is None:
            missing[name] = f"无法定位 {pid} 的包目录，无法解析 {name} 的来源"
            continue
        src = base / RESOURCES_DIRNAME / name
        if not src.is_file():
            missing[name] = f"{pid} 未随包分发 {name}（期望 {src}）"
            continue
        sources[name] = src
    return sources, missing


def deployed(install_dir) -> frozenset[str]:
    """系统域实际存在的、登记在册的 A 类文件名（非 A 类文件不参与对账）。"""
    base = Path(install_dir)
    return frozenset(n for n in KNOWN_ROOT_BINARIES if (base / n).is_file())


def reconcile(want, have) -> "tuple[frozenset[str], frozenset[str]]":
    """**纯函数**：返回 ``(to_deploy, to_reap)`` = ``(wanted − deployed, deployed − wanted)``。

    只做集合差：不读盘、不写盘、不触碰任何外部状态。两侧任一为空 ⇒ 无需 root 授权。
    """
    want_set = frozenset(want or ())
    have_set = frozenset(have or ())
    return frozenset(want_set - have_set), frozenset(have_set - want_set)
