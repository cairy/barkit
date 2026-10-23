"""doctor/survey：「只读地看一眼插件声明与受管数据文件」的共享探针。

C / D / E / F 四组都要回答同一类问题——**插件声明了什么**（A 类二进制、权限）
与**用户域那些文件现在是什么样**。本模块把这些动作收在一处，避免四组各写一遍
``discover()``、各写一遍「不跑 ``__init__`` 读声明」的技巧。

两条边界（改动前先读 ``内部 doctor 设计记录`` §3 只读原则）
--------------------------------------------------------------------
1. **只 discover，不装配。** 一律不调 ``Registry.load_all()``：它会实例化插件、
   给外部插件跑 ``provision``、``plugin.start()`` 起长驻服务，**并连上守护那条会话**
   （声明意图、订阅事实）。本模块提供 :func:`discover` 这一个发现入口。
2. **读声明不跑 ``__init__``。** 插件的 ``resources()`` / ``required_permissions``
   都是**声明**（纯数据、基类 docstring 明确要求「廉价、无副作用」），不需要一个
   真实例。用 :func:`bare` 造一个跳过 ``__init__`` 的裸对象即可——这样既能读到
   声明，又不会让 doctor 触发插件的构造副作用（起线程 / 读盘 / 碰事件总线）。

   代价写清楚：若某个插件的 ``resources()`` 意外依赖了 ``__init__`` 建立的实例
   状态，这里会抛 ``AttributeError``。**不许静默吞掉**——由调用方收进
   ``errors`` 并以 WARN 呈现（该插件的声明被排除在结论之外，而不是当成「没声明」）。
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)


class _ErrorCollector(logging.Handler):
    """只收 ERROR 及以上。挂在 ``barkit`` 这个父 logger 上，子记录靠传播上来。"""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def _capture_errors() -> "Iterator[list[logging.LogRecord]]":
    collector = _ErrorCollector()
    logger = logging.getLogger("barkit")
    logger.addHandler(collector)
    try:
        yield collector.records
    finally:
        logger.removeHandler(collector)


def discover() -> "tuple[Any, dict[str, type], list[logging.LogRecord]]":
    """跑一次 ``Registry.discover()``，返回 ``(registry, 类表, 捕获到的 ERROR 记录)``。

    ``discover()`` 只做「扫描 + 导入 + 闸门」，不实例化、不写盘，是 doctor 允许
    触碰的最深一层。第三个返回值捞的是 ``discover()`` 内部「插件包导入失败只记
    ``log.exception`` 就跳过」的那些记录——它们**不进** ``rejected``，UI 上完全
    看不见（C 组据此报 C2）。

    ``Registry`` 在**函数内**导入而非模块顶层：本模块还要承载纯粹的文件检查
    （``read_json``），不该让「registry 的依赖链能不能 import」决定那些检查能不能跑。
    """
    from barkit.core import config as config_mod
    from barkit.core.registry import Registry

    registry = Registry(config_mod.Config(create_user_file=False))
    with _capture_errors() as records:
        found = registry.discover()
    return registry, found, records


def bare(cls: type):
    """造一个**不跑 ``__init__``** 的裸实例，用于读插件声明。

    插件的 ``Plugin.__init__`` 只做 ``self.ctx = ctx``，本可以传个桩进去；但那样
    就等于「替插件决定构造期能碰什么」，而声明读取根本不需要构造。``object.__new__``
    把这件事说白：**我要的只是类上的声明，不是这个插件的实例**。
    """
    return object.__new__(cls)


def declared_instances(found: "dict[str, type]") -> "tuple[list, list[tuple[str, str]]]":
    """``(可读声明的裸实例, [(插件 id, 失败原因)])``。

    ``id`` 按 registry 同款补齐（类属性为空时用包名），否则 ``rootbin_reconcile``
    的 ``declared()`` 会用 ``type().__name__`` 当归属，报出来的名字对不上插件。

    **这里故意多调一次 ``resources()``**：真正读声明的是
    ``rootbin_reconcile.declaration_of``，而它把异常吞成「空声明」（那是安装期
    正确的保守口径 —— 宁可少算一个资产，不可因一个坏插件阻断整次安装）。但对
    自检来说「这个插件读不出声明」是一条**必须报出来的事实**，不能与「它没声明
    任何资产」混为一谈。故在此先探一次，把坏插件挡在 ``ok`` 之外。
    """
    ok: list = []
    errors: list[tuple[str, str]] = []
    for pid, cls in sorted(found.items()):
        try:
            plugin = bare(cls)
            if not getattr(plugin, "id", ""):
                plugin.id = pid
            plugin.resources()          # 探测可读性，见 docstring
            ok.append(plugin)
        except Exception as exc:  # noqa: BLE001 —— 单个插件读不了不阻断汇总
            errors.append((pid, f"{exc.__class__.__name__}: {exc}"))
    return ok, errors


def declared_root_binaries(
    found: "dict[str, type]",
) -> "tuple[frozenset[str], dict[str, str], list[tuple[str, str]]]":
    """A 类二进制声明（wanted 侧）：``(名集, 名→声明插件, [(插件 id, 失败原因)])``。

    口径与安装期**同源**：直接用 ``core.rootbin_reconcile`` 的 ``wanted()`` /
    ``declared()``，不另写一套「把名字收集起来」的逻辑。
    """
    from barkit.core.rootbin_reconcile import declared, wanted

    ok, errors = declared_instances(found)
    return wanted(ok), declared(ok), errors


def declared_permissions(found: "dict[str, type]") -> "dict[str, tuple[str, ...]]":
    """``{插件 id: 声明的权限键}``（F 组的数据源）。

    ``required_permissions`` 是**类属性**（``core/plugin.py``），读它连裸实例都
    不必造——这也让「某插件实例化失败」不影响它的权限声明可见性。
    """
    out: dict[str, tuple[str, ...]] = {}
    for pid, cls in sorted(found.items()):
        names = getattr(cls, "required_permissions", ()) or ()
        out[pid] = tuple(n for n in names if isinstance(n, str) and n)
    return out


def read_json(path: Path) -> "tuple[Any, str | None]":
    """读一份 JSON，返回 ``(数据, 失败原因)``。**不抛异常**（自检工具不许崩）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"{exc.__class__.__name__}: {exc}"
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, f"JSON 解析失败：{exc}"
