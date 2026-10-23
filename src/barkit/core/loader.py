"""core/loader：外部插件发现（E1，ROADMAP P1）。

外部插件 = 一个带 ``manifest.yaml`` 的目录，置于外部插件根目录（默认
``~/Library/Application Support/BarKit/plugins/``，可由 ``config.plugins.external_path``
覆盖）。Loader 在「读取 manifest」阶段就做 ``api_version`` 门禁（**无需 import 插件
代码**），不兼容即记日志拒绝、不崩溃；通过后再动态 import entry 模块取 ``Plugin``
类，并用 ``Plugin.api_version`` 二次核对（防 manifest 与代码契约漂移）。清单里的
``version``（组件自身版本）**只登记不对账**——它不进任何门禁，读到后强制对齐
``Plugin.version`` 供展示与覆盖提示（决议 D5）。

外部插件与内部 ``pkgutil`` 插件在「Plugin 类发现层」汇合：两者都产出
``{插件 id: Plugin 类}``，统一交给 ``Registry.load_all`` 实例化、
``ContributionRegistry`` 消费——单一列表，无双轨、核心不感知具体插件。
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from types import ModuleType

import yaml

from barkit.core.contributions import SUPPORTED_API_VERSION, api_version_reject_reason
from barkit.core.plugin import Plugin

log = logging.getLogger(__name__)

# 每次发现用唯一模块名装入 sys.modules，避免跨插件 / 跨次发现同 dirname 冲突
_counter = 0


def _next_module_name(dirname: str) -> str:
    global _counter
    _counter += 1
    return f"barkit_external_{dirname}_{_counter}"


def _load_manifest(manifest_path: Path) -> "dict | None":
    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except OSError:
        log.warning("外部插件 manifest 读取失败：%s", manifest_path)
        return None
    except yaml.YAMLError:
        log.exception("外部插件 manifest 解析失败：%s", manifest_path)
        return None
    return data if isinstance(data, dict) else None


def _safe_plugin_id(pid) -> "str | None":
    """校验插件 id 是否**只能**落在外部插件根目录内。

    返回 ``None`` = 安全；否则是拒绝原因。``id`` 直接拼进
    ``APP_SUPPORT/plugins/<id>`` 与暂存 ``<id>-<dir>`` 路径，不校验就会被
    ``id: "../../.ssh"`` 这类值把资产写到插件根目录之外（任意用户可写路径）。
    """
    if not isinstance(pid, str) or not pid:
        return "id 必须是非空字符串"
    if pid.startswith(("/", "\\")) or "/" in pid or "\\" in pid:
        return "id 含路径分隔符"
    if ":" in pid[:3] or "\x00" in pid:
        return "id 含非法字符"
    if any(p == ".." for p in Path(pid).parts):
        return "id 含父目录穿越"
    if len(pid) > 64:
        return "id 过长"
    return None


def _safe_entry_module(entry) -> "str | None":
    """校验 manifest ``entry`` 的模块名部分不越出插件目录。

    返回 ``None`` = 安全；否则是拒绝原因。``entry: "../../evil:Plugin"`` 或
    ``entry: "/tmp/evil:Plugin"`` 会让框架 import 并执行插件目录之外的任意
    ``.py``（任意代码执行）。``mod_rel`` 不含分隔符 / 父目录分量 / 前导点才安全。
    """
    if not isinstance(entry, str) or ":" not in entry:
        return "entry 必须是 'module:Class' 形式"
    mod_rel, _cls = entry.split(":", 1)
    if not mod_rel or mod_rel.startswith(".") or "/" in mod_rel or "\\" in mod_rel:
        return "entry 模块名非法"
    if any(p == ".." for p in Path(mod_rel).parts):
        return "entry 模块名含父目录穿越"
    return None


def manifest_version(manifest: dict) -> str:
    """清单里的组件版本 → 字符串；缺省 / 写歪一律返回空串（＝未标注）。

    **不校验、不拒载**（契约决议 D5）：版本号只用于展示与覆盖提示，为了一个
    写法问题拒装整个插件没有任何收益。数字也接住 —— ``version: 0.1`` 会被 YAML
    读成 float，直接 ``str()`` 化（``"0.1"``），而不是丢掉。
    """
    raw = manifest.get("version", "")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return str(raw)
    return raw if isinstance(raw, str) else ""


def _import_entry(plugin_dir: Path, entry: str, module_name: str) -> "ModuleType | None":
    """``entry`` 形如 ``'plugin:Plugin'`` → 导入 ``plugin_dir/plugin.py`` 并取模块。

    ``module_name`` 是装入 ``sys.modules`` 的唯一名（以目录名 + 计数器区分）。
    """
    if ":" not in entry:
        log.warning("外部插件 entry 格式错误（应为 'module:Class'）：%s", entry)
        return None
    mod_rel, _cls = entry.split(":", 1)
    mod_path = (plugin_dir / mod_rel).with_suffix(".py")
    if not mod_path.exists():
        log.warning("外部插件 entry 模块不存在：%s", mod_path)
        return None
    spec = importlib.util.spec_from_file_location(module_name, mod_path)
    if spec is None or spec.loader is None:
        log.warning("外部插件 entry 无法构建导入规格：%s", mod_path)
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        log.exception("外部插件 entry 导入失败：%s", mod_path)
        return None
    return module


def discover_external(base_dir: Path) -> "tuple[dict[str, type[Plugin]], list[tuple[str, str]]]":
    """扫描 ``base_dir`` 下每个子目录，返回 (``{插件 id: Plugin 类}``, ``[(id/目录, 拒绝原因)]``)。

    ``base_dir`` 不存在 → 返回空（开发机无外部插件属正常，不报错）。
    """
    found: dict[str, type[Plugin]] = {}
    rejected: list[tuple[str, str]] = []
    if not base_dir.exists():
        return found, rejected

    for entry_path in sorted(base_dir.iterdir()):
        if not entry_path.is_dir():
            continue
        manifest_path = entry_path / "manifest.yaml"
        if not manifest_path.exists():
            continue

        manifest = _load_manifest(manifest_path)
        if manifest is None:
            rejected.append((entry_path.name, "manifest 缺失或无法解析"))
            continue

        pid = manifest.get("id")
        if not isinstance(pid, str) or not pid:
            rejected.append((entry_path.name, "manifest 缺有效 id"))
            continue
        # id 直接拼进插件根目录路径：先挡穿越（P1 路径穿越）。
        bad_pid = _safe_plugin_id(pid)
        if bad_pid is not None:
            rejected.append((pid, bad_pid))
            continue

        # 门禁 0：api_version 必须是**整数**——非数字字符串（``"1.0"``）会让下面的
        # ``int()`` 抛 ``ValueError`` 一路冒到 ``AppShell`` 装配，结果**整个 App 起
        # 不来**（与本条文件自己声明的「§9.2 任一插件装配失败只隔离该插件」矛盾）。
        raw_ver = manifest.get("api_version", SUPPORTED_API_VERSION)
        if not isinstance(raw_ver, int) or isinstance(raw_ver, bool):
            rejected.append((pid, "manifest api_version 必须为整数"))
            continue

        # 门禁 1：读取 manifest 即可判定，无需 import 插件代码（早 fail 优于晚崩）
        reason = api_version_reject_reason(int(raw_ver), pid)
        if reason is not None:
            rejected.append((pid, reason))
            continue

        entry = manifest.get("entry", "plugin:Plugin")
        # entry 的模块名部分决定 import 哪个文件：先挡越界加载（P1 任意代码执行）。
        bad_entry = _safe_entry_module(entry)
        if bad_entry is not None:
            rejected.append((pid, bad_entry))
            continue
        module = _import_entry(entry_path, entry, _next_module_name(entry_path.name))
        if module is None:
            rejected.append((pid, "entry 模块导入失败"))
            continue
        cls_name = entry.split(":", 1)[1]
        cls = getattr(module, cls_name, None)
        if cls is None or not issubclass(cls, Plugin):
            rejected.append((pid, "entry 未暴露有效的 Plugin 子类"))
            continue

        # 门禁 2：Plugin 类 api_version 与 manifest 二次核对（防契约漂移）
        if int(getattr(cls, "api_version", SUPPORTED_API_VERSION)) != int(
            manifest.get("api_version", SUPPORTED_API_VERSION)
        ):
            rejected.append((pid, "manifest api_version 与插件代码 api_version 不一致"))
            continue

        # manifest 为权威身份与元数据：强制对齐 id / version；order 可选覆盖（§6.4
        # 新插件默认插入位；兼容旧 manifest 键名 panel_order）
        cls.id = pid
        cls.version = manifest_version(manifest)
        for key in ("order", "panel_order"):
            if isinstance(manifest.get(key), int):
                cls.order = manifest[key]
                break
        found[pid] = cls
    return found, rejected
