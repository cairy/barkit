"""core/installer：插件安装四阶段基建（方案 §9.2 / §9.3，批次 6b）。

四阶段：discover（Registry/loader）→ **validate** → **provision** → isolate。
失败隔离原则：任一插件装配失败只隔离该插件（标记 failed + 人类可读原因），
不回滚已落盘资产（交给幂等与对账收敛），不影响其余插件与主程序启动。

范围决策（批次 6 执行计划）：
- **B/C 资产部署只对外部插件执行**：内置插件资产随包直接读，无需复制
  （部署逻辑本身对两者通用，见 provision）。
- **A 类（root 二进制）**：本批只做封闭集**名字**校验（KNOWN_ROOT_BINARIES）；
  sha256 基线 + 部署/回收对账随批次 4 落在 capabilities/helper/rootbin.py，
  届时本集合迁移过去，调用方无需改动。
- **配置默认值合成（§9.5）不在本批**：provision 不写任何配置。
- **禁用插件同样装配资产**（部署先于 enabled 判断）：禁用降级的是运行状态，
  不是资产存在性（§9.3）。

**安装输入侧**（决议 D5）：``open_package`` 把「插件目录 / 插件压缩包」统一成一个可用
的插件目录（压缩包解到临时目录、用毕清理）；``read_package_identity`` 与
``read_installed_version`` 在**不 import 插件代码**的前提下给出「装的是谁、什么版本」
「机器上已装的是什么版本」——安装入口的覆盖确认据此措辞。解包的三条安全线见
``extract_package``。

资产布局约定：
- B 类源 = ``<插件目录>/assets/``，目标 = ``APP_SUPPORT/plugins/<id>/assets/``，
  按内容哈希幂等覆盖（插件升级应生效）。
- C 类种子源 = ``<插件目录>/seeds/<data_files 相对路径>``，
  目标 = ``APP_SUPPORT/<相对路径>``，**仅缺失时写入**（存在即用户数据，永不覆盖）。
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from barkit import paths
from barkit.core.loader import (
    _import_entry,
    _load_manifest,
    _next_module_name,
    _safe_plugin_id,
    manifest_version,
)
from barkit.core.resources import PluginResources

log = logging.getLogger(__name__)

# A 类封闭集（**名级只读副本**，批次 6b 版）。批次 4 起真源为
# `capabilities/helper/rootbin.py::ROOT_BINARIES`（名字 + sha256 发行态基线 +
# 系统域目标路径）；`core/` 不得依赖 `capabilities/*`（分层门禁
# `test_layer_dependency_direction_core`），故此处保留名级副本，两者的相等由
# `tests/test_helper_contract.py::test_root_binary_closed_set_single_source` 强制。
# 新增 root 二进制 = 改框架源码登记，与新增原语同一摩擦力（方案 §9.3）。
KNOWN_ROOT_BINARIES: frozenset[str] = frozenset({"hev-socks5-tunnel"})


# ---------- 阶段 2：validate（纯检查，不写盘） ----------

def _unsafe_rel_path(rel: str) -> bool:
    """data_files 项必须是 APP_SUPPORT 内的相对路径（拒绝绝对路径 / 穿越）。"""
    if not rel or rel.startswith("/") or rel.startswith("\\"):
        return True
    if ":" in rel[:3]:          # 盘符形态防御
        return True
    parts = Path(rel).parts
    return any(p == ".." for p in parts)


def validate_plugin(plugin) -> "str | None":
    """校验插件的可插拔声明（resources 契约）。返回 None = 通过。"""
    try:
        res = plugin.resources()
    except Exception as exc:  # noqa: BLE001 —— 声明读取失败即校验失败
        return f"resources() 抛异常：{exc!r}"
    return _validate_decl(res)


# ---------- 阶段 3：provision（清单计算 + 幂等执行，dry-run 共用清单） ----------

@dataclass
class ProvisionOp:
    """一条装配操作。``action``：cover（B 类覆盖）/ seed（C 类仅缺失写）/ skip。"""

    src: Path
    dst: Path
    action: str                      # cover | seed | skip
    kind: str                        # b | c


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_provision_plan(
    plugin_id: str, plugin_dir: Path, resources: PluginResources
) -> list[ProvisionOp]:
    """计算装配清单（不写盘）。dry-run 与真实 provision 共用本函数（§9.2）。"""
    ops: list[ProvisionOp] = []

    # B 类：assets/** → APP_SUPPORT/plugins/<id>/assets/**，内容哈希不同即覆盖
    assets_src = plugin_dir / "assets"
    assets_dst = paths.APP_SUPPORT / "plugins" / plugin_id / "assets"
    if assets_src.is_dir():
        for src in sorted(assets_src.rglob("*")):
            if not src.is_file():
                continue
            rel = src.relative_to(assets_src)
            dst = assets_dst / rel
            action = "cover" if (not dst.exists() or _sha256(src) != _sha256(dst)) else "skip"
            ops.append(ProvisionOp(src=src, dst=dst, action=action, kind="b"))

    # C 类：seeds/<rel> → APP_SUPPORT/<rel>，仅缺失时写入（永不覆盖用户数据）
    seeds_src = plugin_dir / "seeds"
    for rel in resources.seed_files:
        src = seeds_src / rel
        dst = paths.APP_SUPPORT / rel
        if not src.is_file():
            log.warning("插件 %s 声明的种子缺失，跳过：%s", plugin_id, src)
            continue
        action = "skip" if dst.exists() else "seed"   # 存在即用户数据，永不覆盖
        ops.append(ProvisionOp(src=src, dst=dst, action=action, kind="c"))
    return ops


def provision(
    plugin_id: str, plugin_dir: Path, resources: PluginResources, dry: bool = False
) -> list[ProvisionOp]:
    """执行装配：``dry=True`` 只算清单不写盘（升级预检）；否则幂等写 B/C 资产。"""
    ops = compute_provision_plan(plugin_id, plugin_dir, resources)
    if dry:
        return ops
    for op in ops:
        if op.action == "skip":
            continue
        op.dst.parent.mkdir(parents=True, exist_ok=True)
        if op.kind == "b":
            shutil.copyfile(op.src, op.dst)
            op.dst.chmod(0o644)
        else:  # c：种子，用户数据 0600（§9.3）
            shutil.copyfile(op.src, op.dst)
            op.dst.chmod(0o600)
        log.info("插件 %s 装配资产 [%s]：%s", plugin_id, op.action, op.dst)
    return ops


# ---------- 插件包：认身份 / 解包（决议 D5 安装入口的输入侧） ----------

@dataclass(frozen=True)
class PackageIdentity:
    """插件包的身份：清单里的 id 与组件版本（``version`` 空串 = 未标注）。"""

    plugin_id: str
    version: str


def read_package_identity(src_dir: Path) -> "tuple[PackageIdentity | None, str]":
    """读插件包身份（**不 import 插件代码**，只读清单）。

    与 loader 的发现门禁分开：此处的用途是「动盘之前先知道要装的是谁、什么版本」，
    故只要求 id 可用；api_version 兼容性仍由安装流程的预检（真 import）判定。

    先认**导出包**：数据导出包的文件名同样叫 ``barkit-plugin-<id>-<时间>.zip``、清单里
    也有 ``id``，不拦就会一路走到「entry 模块导入失败」这种看不出所以然的报错。
    """
    manifest = _load_manifest(Path(src_dir) / "manifest.yaml")
    if manifest is None:
        return None, "manifest 缺失或无法解析"
    if "format_version" in manifest:      # 数据导出包的标记（见 core/exporter）
        return None, "这是插件数据导出包，请用「恢复插件数据…」导入；安装插件请选插件包"
    pid = manifest.get("id")
    if not isinstance(pid, str) or not pid:
        return None, "manifest 缺有效 id"
    return PackageIdentity(plugin_id=pid, version=manifest_version(manifest)), ""


def read_installed_version(base_dir: Path, plugin_id: str) -> "str | None":
    """读**已安装**插件目录里清单的版本。目录不存在 → ``None``（＝首次安装）。

    存在但读不动 / 清单没写 → ``""``（＝已装、版本未知）。两种情况在覆盖提示里的
    措辞不同（「首次安装」vs「覆盖一个版本未知的包」），**必须区分**，别都折成 None。

    刻意从**磁盘清单**取而不是从已加载的插件类取：包被契约闸门拒载 / 装配失败时
    类拿不到，但目录还在，用户依然应该能覆盖它。
    """
    manifest_path = Path(base_dir) / plugin_id / "manifest.yaml"
    if not manifest_path.is_file():
        return None
    manifest = _load_manifest(manifest_path)
    return "" if manifest is None else manifest_version(manifest)


#: Finder 压缩产生的噪声（资源分叉目录 / 逐目录的 .DS_Store）——必须丢，否则
#: ``__MACOSX/`` 里那份清单副本会让「包里有几份清单」的判断失效。
_ZIP_NOISE_PREFIXES = ("__MACOSX/",)
_ZIP_NOISE_NAMES = (".DS_Store",)


def _zip_entry_is_symlink(info: zipfile.ZipInfo) -> bool:
    """ZIP 成员是不是符号链接（外部属性高位是 Unix mode；S_IFLNK = 0120000）。"""
    return (info.external_attr >> 16) & 0o170000 == 0o120000


def _unzip_entries(zf: zipfile.ZipFile, dest_dir: Path) -> "str | None":
    """逐条解出成员；返回 None = 通过，否则是拒装原因。"""
    for info in zf.infolist():
        name = info.filename
        if name.startswith(_ZIP_NOISE_PREFIXES) or name.endswith(_ZIP_NOISE_NAMES):
            continue
        if name.endswith("/"):
            continue                          # 目录项：由文件的父级 mkdir 建立
        if _unsafe_rel_path(name):
            return f"压缩包含非法路径：{name!r}"
        if _zip_entry_is_symlink(info):
            return f"压缩包含符号链接（拒装）：{name!r}"
        target = dest_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
    return None


def extract_package(zip_path: Path, dest_dir: Path) -> "tuple[Path | None, str]":
    """把插件压缩包解到 ``dest_dir``，返回**含清单的那一层目录**（失败 → None ＋ 原因）。

    三条安全线（压缩包是外部输入，解包即写盘）：
    ① 逐条校验成员路径，绝对路径 / ``..`` / 盘符一律拒（复用 data_files 的同一把尺）；
    ② 拒绝符号链接 —— 链接能把后续写入引到包外；
    ③ 丢弃 Finder 噪声（``__MACOSX/`` 与 ``.DS_Store``）。

    定位插件根：压缩包常整包套一层目录（Finder 右键压缩的形态），故不假定清单在根上 ——
    根上有清单即用它；否则取**全包唯一**那份清单所在目录；一份都没有 / 有多份都拒绝并
    说明原因（宁可不装，也不猜用户想装哪一个）。
    """
    zip_path = Path(zip_path)
    if not zipfile.is_zipfile(zip_path):
        return None, f"不是有效的压缩包：{zip_path.name}"
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            reason = _unzip_entries(zf, dest_dir)
    except (OSError, zipfile.BadZipFile) as exc:
        return None, f"解压失败：{exc}"
    if reason is not None:
        return None, reason
    return _locate_package_root(dest_dir)


def _locate_package_root(dest_dir: Path) -> "tuple[Path | None, str]":
    if (dest_dir / "manifest.yaml").is_file():
        return dest_dir, ""
    found = sorted(p.parent for p in dest_dir.rglob("manifest.yaml"))
    if not found:
        return None, "压缩包里找不到插件清单（manifest.yaml）"
    if len(found) > 1:
        return None, f"压缩包里有 {len(found)} 份插件清单，请一次只装一个插件"
    return found[0], ""


@contextlib.contextmanager
def open_package(path):
    """统一安装源入口：目录直接用；压缩包解到临时目录，**退出时清理**。

    yield ``(插件目录 | None, 失败原因)``。做成上下文管理器而不是「返回路径、让调用方
    记得删」：调用方中途抛异常时临时目录也必须消失，否则每失败一次就在磁盘上留一份
    解好的插件。
    """
    src = Path(path)
    if src.is_dir():
        yield src, ""
        return
    if not src.is_file():
        yield None, f"路径不存在：{src}"
        return
    tmp = Path(tempfile.mkdtemp(prefix="barkit-pkg-"))
    try:
        yield extract_package(src, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 外部插件安装 / 升级（§9.2 升级流程） ----------

def install_external(
    base_dir: Path,
    src_dir: Path,
    *,
    stop_active: "callable | None" = None,
    activate: "callable | None" = None,
) -> "tuple[bool, str]":
    """安装 / 升级外部插件（源为已解包的插件目录）。

    流程：落暂存 → 预检（validate + provision dry-run，不写盘）→ 原子切换 →
    activate 回调（真实装配 + start）。任一步失败：恢复原目录、暂存保留供排查，
    返回 ``(False, 原因)``。

    ``stop_active(pid)`` / ``activate(pid, plugin_dir)`` 由调用方提供（app 侧为
    PluginManager 编排；测试用桩）。activate 缺省 = 只落盘不激活（用户下次
    启动时经 load_all 正常装配）。
    """
    base_dir = Path(base_dir)
    staging_root = base_dir / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)

    # 1. 读取 manifest 并过基础门禁（id / api_version）
    manifest = _load_manifest(src_dir / "manifest.yaml")
    if manifest is None:
        return False, "manifest 缺失或无法解析"
    pid = manifest.get("id")
    if not isinstance(pid, str) or not pid:
        return False, "manifest 缺有效 id"
    bad_pid = _safe_plugin_id(pid)
    if bad_pid is not None:
        return False, f"manifest id 非法：{bad_pid}"

    # 2. 落暂存
    staging = staging_root / f"{pid}-{Path(src_dir).name}"
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(src_dir, staging)

    # 3. 预检：从暂存目录导入并校验声明 + provision dry-run（不写盘）
    entry = manifest.get("entry", "plugin:Plugin")
    module = _import_entry(staging, entry, _next_module_name(pid))
    if module is None:
        return False, "entry 模块导入失败"
    cls = getattr(module, entry.split(":", 1)[1], None)
    from barkit.core.plugin import Plugin

    if not (isinstance(cls, type) and issubclass(cls, Plugin)):
        return False, "entry 未暴露有效的 Plugin 子类"
    cls.id = pid
    probe = cls.__new__(cls)           # 只读声明，不跑 __init__（可能有副作用）
    probe.ctx = None
    try:
        res = probe.resources()
    except Exception as exc:  # noqa: BLE001
        return False, f"resources() 抛异常：{exc!r}"
    reason = _validate_decl(res)
    if reason is not None:
        return False, reason
    try:
        provision(pid, staging, res, dry=True)
    except Exception as exc:  # noqa: BLE001
        return False, f"装配预检失败：{exc}"

    target = base_dir / pid
    backup = staging_root / f"{pid}.bak"
    if backup.exists():
        shutil.rmtree(backup)

    # 4. 停运行实例（由调用方编排，失败不阻断——升级旧实例停止失败仍可切换）
    if stop_active is not None:
        try:
            stop_active(pid)
        except Exception:  # noqa: BLE001
            log.exception("停止插件 %s 运行实例失败，继续升级", pid)

    # 5. 原子切换（rename 替换；旧版先挪到 .staging/<pid>.bak 备份）
    had_old = target.exists()
    if had_old:
        target.rename(backup)
    staging.rename(target)

    # 6. 激活；失败 → 回滚 rename，恢复旧版（「失败旧版继续可用」承诺），
    #    新版挪回暂存目录保留供排查（§9.2 失败处理）
    if activate is not None:
        try:
            activate(pid, target)
        except Exception as exc:  # noqa: BLE001
            log.exception("插件 %s 激活失败，回滚升级", pid)
            target.rename(staging)
            if had_old:
                backup.rename(target)
            # 措辞随「有没有旧版」分叉：全新安装装不上时说「已恢复旧版」会让人以为
            # 有一个可用版本（实情是什么都没留下）。
            return False, (
                f"激活失败（已恢复旧版）：{exc}" if had_old
                else f"激活失败（未安装，已清理）：{exc}"
            )
    if had_old and backup.exists():
        shutil.rmtree(backup)
    log.info("外部插件 %s 安装/升级完成：%s", pid, target)
    return True, pid


def _validate_decl(res) -> "str | None":
    """validate_plugin 的声明子集（resources 清单校验，不含实例方法依赖）。"""
    if not isinstance(res, PluginResources):
        return "resources() 未返回 PluginResources"
    seeds = set(res.seed_files)
    data = set(res.data_files)
    if not seeds <= data:
        return f"seed_files 必须是 data_files 的子集（§9.1），越界：{sorted(seeds - data)}"
    for rel in data:
        if _unsafe_rel_path(rel):
            return f"data_files 含非法相对路径：{rel!r}"
    unknown = set(res.root_binaries) - KNOWN_ROOT_BINARIES
    if unknown:
        return f"root_binaries 含未登记的二进制 {sorted(unknown)}（§9.3 封闭集）"
    return None
