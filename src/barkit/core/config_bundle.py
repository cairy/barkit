"""core/config_bundle：把用户域配置打成一个**可校验**的包，并能原样还原。

它与 ``core/exporter`` 的分工：那一份管**单个插件**（配置 + 热键 + 该插件声明的
数据文件），走 ``Plugin.export_data`` 的契约面；这一份管**整台机器**（框架级设置
＋ 各组件落在用户域的原文件），不经过任何插件接口 —— 它要能在一个插件都加载
不起来的环境里照样把数据捞出来。

范围判据是「丢了会看见与事实不符的东西」：

| 收 | 为什么 |
|---|---|
| ``config.yaml`` | 全部用户设置（框架级 ＋ ``plugins.<id>`` 差异层） |
| ``hosts_entries.json`` | hosts 托管条目主数据 |
| ``variables_state.json`` | 全局变量里 ``ask`` 那类按日答复（上班时间等） |
| ``agenda_log.json`` | todo 的日账（打过卡、推过哪几条） |
| ``interaction_state.json`` | 交互账本（哪些提示按过、不再弹） |
| ``plugins/`` | 外部插件整目录（它们是用户数据，不是本仓源码） |

**不收**，且各有理由（别照「数据目录里有什么」来加）：

- ``helper.log`` / ``barkit.log`` —— 日志，另有封顶策略；且它可能几百 KB 到几 MB。
- ``helper_snapshot.json`` —— 守护写的崩溃恢复快照，属于**观测**。还原一份旧快照
  等于让守护拿过期事实收敛，比没有更糟（它在 ``doctor.checks_data`` 里是 WARN 级，
  正是因为它不参与任何在线判据）。
- ``iface_gateways.json`` —— 网关累积账，**属主 root、普通用户不可写**；收进包里
  会让「导入」凭空多一条必然失败项。它由守护自愈重建。
- ``helper_tasks.json`` / ``bt_request.json`` / ``bt_result.json`` —— 已随方案 §11.2
  消失的通道类文件，盘上若还留着是历史残留，不该被备份继承下去。
- ``.lock`` / ``*.sock`` / ``.DS_Store`` —— 运行时节点与系统噪声。

**包里含明文口令**：``config.yaml`` 的 ``plugins.autotype.profiles[].text`` 就是
一键输入要注入的明文。所以导出物一律 ``chmod 600``，且 UI 必须当场说清「别放云盘」。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from barkit.core.installer import _unsafe_rel_path

log = logging.getLogger(__name__)

#: 包格式版本。形状变化（清单键、目录名）必须 +1，导入侧据此拒绝旧包而不是猜。
BUNDLE_FORMAT_VERSION = 1

MANIFEST_NAME = "manifest.yaml"
PAYLOAD_DIR = "payload"

#: 文件名前缀。带时间戳拼成 ``barkit-config-20261022-144500.zip``。
BUNDLE_PREFIX = "barkit-config"

CATEGORY_CONFIG = "config"
CATEGORY_STATE = "state"

#: todo 日账的文件名。真源是 ``barkit.plugins.todo.day_log.AGENDA_FILENAME``，
#: 但 **core 不许 import plugins**（``test_layer_dependency_direction_core`` 守层次
#: 方向：依赖只能自上而下）。故这里写字面量，由
#: ``tests/test_config_bundle.py::test_agenda_filename_matches_its_real_source`` 对拍 ——
#: 真源一改，那条用例立刻红，不会静默指向一个不存在的文件。
AGENDA_FILENAME = "agenda_log.json"


@dataclass(frozen=True)
class Entry:
    """一份要备份的相对路径（相对 ``paths.APP_SUPPORT``）。"""

    rel: str
    category: str


@dataclass
class ExportReport:
    """一次导出的结果。``problems`` 非空即「包已写出，但完整性没通过」。"""

    zip_path: Path
    files: list[str] = field(default_factory=list)
    total_bytes: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def human_size(self) -> str:
        """给人看的体量（回执文案用，免得 UI 自己再算一遍进位）。"""
        return _human_size(self.total_bytes)


def _config_name() -> str:
    """用户配置文件的名。

    真源是 ``core/config/config.py`` 里 ``paths.APP_SUPPORT / "config.yaml"`` 那一行
    —— 它**刻意没有**模块级常量（见 ``core/config/consts.py`` 的注释：常量会诱导
    别人把它当默认参数用，而默认参数在定义期求值，测试的路径重定向就失效了）。
    这里的字面量由 ``test_config_bundle`` 与 ``Config().user_path.name`` 对拍。
    """
    return "config.yaml"


def entries(*, include_state: bool = True) -> list[Entry]:
    """当前该收的清单。

    文件名一律**调用期**从真源取（``paths`` 的模块属性会被测试逐用例重定向，
    导入期冻成值会让备份指向真实用户目录）。
    """
    from barkit import paths
    from barkit.core.interaction import LEDGER_FILENAME
    from barkit.core.variables.store import STATE_FILENAME

    out = [
        Entry(_config_name(), CATEGORY_CONFIG),
        Entry(paths.HOSTS_ENTRIES_FILE.name, CATEGORY_CONFIG),
        Entry(STATE_FILENAME, CATEGORY_CONFIG),
    ]
    if include_state:
        out.append(Entry(AGENDA_FILENAME, CATEGORY_STATE))
        out.append(Entry(LEDGER_FILENAME, CATEGORY_STATE))
    return out


def _walk_dir(base: Path, rel_root: str) -> list[tuple[str, Path]]:
    """递归列出 ``base`` 下的普通文件，返回 ``(相对路径, 绝对路径)``。

    外部插件目录是用户域里唯一一个**整目录**进包的东西。跳过点开头的名字
    （``.DS_Store`` 之类）与符号链接 —— 链接可能指向包外任何地方，跟着走等于
    把导出变成一次任意读取。
    """
    found: list[tuple[str, Path]] = []
    if not base.is_dir():
        return found
    for cur, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            path = Path(cur) / name
            if path.is_symlink() or not path.is_file():
                continue
            found.append((f"{rel_root}/{path.relative_to(base).as_posix()}", path))
    return found


def collect(*, include_state: bool = True) -> list[tuple[str, str, Path | None]]:
    """把清单落成 ``(rel, category, 绝对路径 or None)``。``None`` = 这台上没有。"""
    from barkit import paths

    root = paths.APP_SUPPORT
    out: list[tuple[str, str, Path | None]] = []
    for entry in entries(include_state=include_state):
        path = root / entry.rel
        out.append((entry.rel, entry.category, path if path.is_file() else None))

    plugins_root = paths.EXTERNAL_PLUGINS_DIR
    for rel, path in _walk_dir(plugins_root, plugins_root.name):
        out.append((rel, CATEGORY_CONFIG, path))
    return out


def _sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _atomic_write_bytes(target: Path, blob: bytes, mode: int) -> None:
    """tmp ＋ fsync ＋ replace，权限显式给。

    与 ``core/config/store.atomic_write`` 同款理由（那里写文本、这里写字节）：
    半截落盘比不落盘更坏 —— 一份 0 字节的 ``config.yaml`` 会被读成「用户什么都没配过」。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".import.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, str(target))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _zip_path(dest_dir: Path, prefix: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = dest_dir / f"{prefix}-{stamp}.zip"
    if not candidate.exists():
        return candidate
    # 同一秒内连点两次：后缀递增，不覆盖上一份（覆盖等于把唯一的备份抹了）。
    for i in range(1, 100):
        alt = dest_dir / f"{prefix}-{stamp}-{i}.zip"
        if not alt.exists():
            return alt
    return dest_dir / f"{prefix}-{stamp}-{os.getpid()}.zip"


def export_bundle(
    dest_dir: Path | None = None, *, include_state: bool = True,
    prefix: str = BUNDLE_PREFIX, dest_path: Path | None = None,
) -> ExportReport:
    """打包并**立刻回读校验**。

    ``dest_path`` 与 ``dest_dir`` 二选一：前者是 UI 选定**完整落点**（用户自己起了
    名字），后者是「自动命名，放进这个目录」。两条都走 ``_zip_path`` 之外的同一段
    写出逻辑，差别只在名字怎么定。

    校验不是可选的收尾步骤：导出这个动作的全部价值就是「事后能原样拿回来」，
    而写 zip 恰好是最容易静默出错的一步（磁盘满、被截断、路径编码）。所以本函数
    写完就调 :func:`verify_bundle`，结果并进同一个 :class:`ExportReport` ——
    调用方拿到的 ``ok`` 已经包含了「读得回来且逐字节对得上」。
    """
    from barkit import paths

    if dest_path is not None:
        # UI 选定的落点：尊重用户起的名，只补 .zip 后缀，不改文件名。
        zip_path = Path(dest_path)
        if zip_path.suffix.lower() != ".zip":
            zip_path = zip_path.with_suffix(zip_path.suffix + ".zip")
        zip_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        if dest_dir is None:
            dest_dir = paths.APP_SUPPORT / "backups"
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        zip_path = _zip_path(dest_dir, prefix)

    items = collect(include_state=include_state)
    manifest_files: list[dict] = []
    report = ExportReport(zip_path=Path())

    tmp_path = zip_path.with_name(zip_path.name + ".part")
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel, category, path in items:
                if path is None:
                    continue
                if _unsafe_rel_path(rel):
                    report.problems.append(f"清单里有越界路径，已跳过：{rel}")
                    continue
                try:
                    blob = path.read_bytes()
                except OSError as exc:
                    report.problems.append(f"读不出 {rel}：{exc}")
                    continue
                zf.writestr(f"{PAYLOAD_DIR}/{rel}", blob)
                manifest_files.append({
                    "rel": rel,
                    "category": category,
                    "size": len(blob),
                    "sha256": _sha256(blob),
                })
                report.files.append(rel)
                report.total_bytes += len(blob)

            manifest = {
                "format_version": BUNDLE_FORMAT_VERSION,
                "app": "BarKit",
                "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "includes_secret": True,
                "files": manifest_files,
            }
            zf.writestr(
                MANIFEST_NAME,
                yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
            )
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, str(zip_path))
    except OSError as exc:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        report.problems.append(f"写包失败：{exc}")
        report.zip_path = zip_path
        return report

    report.zip_path = zip_path
    ok, problems = verify_bundle(zip_path)
    if not ok:
        report.problems.extend(problems)
    log.info(
        "配置已导出：%s（%d 个文件，%d 字节，校验%s）",
        zip_path, len(report.files), report.total_bytes, "通过" if ok else "未通过",
    )
    return report


def read_manifest(zip_path: Path) -> "dict | None":
    """读包内清单。读不出 / 顶层不是 mapping 一律 ``None``（调用方按「不是我们的包」处理）。"""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            if MANIFEST_NAME not in zf.namelist():
                return None
            data = yaml.safe_load(zf.read(MANIFEST_NAME))
    except (OSError, zipfile.BadZipFile, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def verify_bundle(zip_path: Path) -> "tuple[bool, list[str]]":
    """逐条比对清单与包内字节。返回 ``(是否全对, 问题清单)``。

    四类问题都要报出来，因为它们对用户意味着不同的下一步：

    - 清单读不出 / 版本不符 ⇒ 这不是本程序写的包（或来自更新版本）。
    - 清单里有、包内缺 ⇒ 包被截断或被人删过成员。
    - 包内有、清单没登记 ⇒ 包被人塞过东西，**不导入**（白名单原则）。
    - 字节哈希不符 ⇒ 文件被改过或写坏了。
    """
    manifest = read_manifest(zip_path)
    if manifest is None:
        return False, ["包里没有可解析的清单（manifest.yaml）"]
    if manifest.get("format_version") != BUNDLE_FORMAT_VERSION:
        return False, [
            f"包格式 v{manifest.get('format_version')}，本程序只认 v{BUNDLE_FORMAT_VERSION}"
        ]
    declared = manifest.get("files")
    if not isinstance(declared, list):
        return False, ["清单缺 files 段"]

    problems: list[str] = []
    expected: dict[str, dict] = {}
    for item in declared:
        if not isinstance(item, dict) or not isinstance(item.get("rel"), str):
            problems.append("清单里有形状不对的条目")
            continue
        rel = item["rel"]
        if _unsafe_rel_path(rel):
            problems.append(f"清单里有越界路径：{rel!r}")
            continue
        expected[rel] = item

    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            extra = {
                n[len(PAYLOAD_DIR) + 1:] for n in names
                if n.startswith(PAYLOAD_DIR + "/")
            } - set(expected)
            if extra:
                problems.append(
                    "包内有清单未登记的文件：%s" % "、".join(sorted(extra)[:5])
                )
            for rel, item in expected.items():
                member = f"{PAYLOAD_DIR}/{rel}"
                if member not in names:
                    problems.append(f"清单里登记了 {rel}，但包里没有")
                    continue
                blob = zf.read(member)
                if len(blob) != item.get("size"):
                    problems.append(
                        f"{rel} 大小不符（包内 {len(blob)}，清单 {item.get('size')}）"
                    )
                    continue
                if _sha256(blob) != item.get("sha256"):
                    problems.append(f"{rel} 内容与清单记录的哈希不符")
    except (OSError, zipfile.BadZipFile) as exc:
        return False, [f"打不开这个包：{exc}"]

    return (not problems), problems


def import_bundle(zip_path: Path, *, backup: bool = True) -> "tuple[bool, str]":
    """把包还原到用户域。返回 ``(是否成功, 给人看的一句话)``。

    顺序是**先校验、再备份、最后落盘**：

    1. 校验不过就一个字节都不动（宁可什么都不做，也不留半套配置）。
    2. 落盘前把**当前**状态也打一份包 —— 导入是一次覆盖写，用户点的可能是错的那个包。
       备份失败即中止：没有回退路径的覆盖写不该发生。
    3. 逐文件原子写。``config.yaml`` 给 600（它含明文口令），其余 644。

    调用方负责提示「重启生效」：这里替换的是盘上字节，运行期实例不会重读。
    """
    from barkit import paths

    zip_path = Path(zip_path)
    ok, problems = verify_bundle(zip_path)
    if not ok:
        return False, "这个包不能导入：" + "；".join(problems[:3])

    manifest = read_manifest(zip_path) or {}
    backup_path: Path | None = None
    if backup:
        prior = export_bundle(
            paths.APP_SUPPORT / "backups", prefix=f"{BUNDLE_PREFIX}-before-import"
        )
        if not prior.ok:
            return False, (
                "导入已中止：没能先备份当前配置（%s）。没有回退路径就不覆盖。"
                % "；".join(prior.problems[:2])
            )
        backup_path = prior.zip_path

    written: list[str] = []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for item in manifest.get("files", []):
                rel = item["rel"]
                if _unsafe_rel_path(rel):
                    return False, f"包里有越界路径：{rel!r}（拒绝导入）"
                target = paths.APP_SUPPORT / rel
                mode = 0o600 if rel == _config_name() else 0o644
                _atomic_write_bytes(target, zf.read(f"{PAYLOAD_DIR}/{rel}"), mode)
                written.append(rel)
    except (OSError, zipfile.BadZipFile) as exc:
        suffix = f"；当前配置已备份到 {backup_path}" if backup_path else ""
        return False, f"写到第 {len(written) + 1} 个文件时失败：{exc}{suffix}"

    note = f"已恢复 {len(written)} 个文件"
    if backup_path is not None:
        note += f"；导入前的配置备份在 {backup_path}"
    log.info("配置已导入：%s → %d 个文件", zip_path, len(written))
    return True, note


def describe_bundle(zip_path: Path) -> "str | None":
    """给 UI 用的一句话摘要：几个文件、多大、什么时候导出、校验过不过。"""
    manifest = read_manifest(zip_path)
    if manifest is None:
        return None
    files = manifest.get("files") or []
    total = sum(int(f.get("size") or 0) for f in files if isinstance(f, dict))
    ok, problems = verify_bundle(zip_path)
    stamp = manifest.get("exported_at") or "未知时间"
    text = "%s 导出，%d 个文件共 %s" % (stamp, len(files), _human_size(total))
    if ok:
        return text + "，校验通过"
    return text + "，校验未通过：" + "；".join(problems[:2])


def _human_size(num: int) -> str:
    for unit in ("B", "KB", "MB"):
        if num < 1024 or unit == "MB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} MB"


def bundle_summary_json(zip_path: Path) -> str:
    """给「复制诊断」/ 排查用的 JSON：清单原样 + 校验结论。"""
    manifest = read_manifest(zip_path) or {}
    ok, problems = verify_bundle(zip_path)
    return json.dumps(
        {"ok": ok, "problems": problems, "manifest": manifest},
        ensure_ascii=False, indent=2,
    )
