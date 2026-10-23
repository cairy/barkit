"""A 类 root 二进制：封闭命名集 + 发行态 sha256 基线 + 完整性 manifest（§7.4.5 / §9.3）。

为什么必须有基线哈希
------------------
「物理随插件分发、授权归框架」的论证完全建立在**哈希校验**上。没有它，
「随插件携带」就退化成「插件自带即被 root 执行」，A 类与 B 类再无区别
（方案 §10 特别说明）。故新增一个 root 二进制 = 在本文件登记名字与发行态哈希，
与新增一个特权原语同一摩擦力。

manifest 的定位（易被误解，写死）
------------------------------
``manifest.json`` 由安装期（root 上下文）写入系统域、``root:wheel 0600``。
被保护对象与基线**双向都不可被非 root 改写**，校验才有意义（§7.4.5 P1）。
它防的是**故障与误操作**（安装不完整、升级间隙、磁盘错误），不是主动攻击。

single-source 说明
-----------------
``core/installer.py`` 的 ``KNOWN_ROOT_BINARIES`` 是**名级只读副本**：``core/`` 不得
依赖 ``capabilities/*``（分层门禁 `test_layer_dependency_direction_core`）。
两者的相等由 `tests/test_helper_contract.py::test_root_binary_closed_set_single_source`
强制 —— 漂移即测试失败，不存在「两处可各改一半」的空间。

启动 / 执行前自检的两条不同路线（易混，写死）
------------------------------------------
- **helper 自身源码**（整目录 ``*.py``，不签名）→ 可复现，走
  ``check_owner_perms`` + ``verify_tree_against_manifest``（聚合哈希）。
- **A 类二进制**（hev，ad-hoc 签名的 Mach-O）→ 签名会改写文件字节且不可复现，
  哈希**只能**用于安装期校验源文件；运行期的守卫是 ``check_owner_perms`` +
  ``verify_registered`` + ``codesign --verify``（helper 侧在 ``tunnelops`` 里调）。
  两者都防**故障与误操作**（安装不完整 / 权限漂移 / 磁盘错误），不是主动攻击。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

CheckResult = Tuple[bool, Optional[str]]

#: 系统域根目录（root:wheel 0755）。helper 私有的运行数据也落在同一目录。
SYS_DIR = Path("/Library/Application Support/BarKit")
#: 完整性基线 manifest（root:wheel 0600）
MANIFEST_PATH = SYS_DIR / "manifest.json"
#: helper 源码在系统域中的部署名（manifest 里以该名登记其哈希）
HELPER_ARTIFACT_NAME = "helper"


@dataclass(frozen=True)
class RootBinary:
    """一个随插件分发、由框架授权的 root 二进制。"""

    name: str                    # 封闭集内的唯一名（= 源文件名 / 部署名）
    sha256: str                  # 发行态基线哈希：不匹配即安装失败，禁降级
    install_path: Path           # 系统域目标路径（**源码常量，非请求参数**）


#: 封闭命名集。新增条目必须同时更新 `core/installer.KNOWN_ROOT_BINARIES`（由门禁校验相等）。
ROOT_BINARIES: Dict[str, RootBinary] = {
    "hev-socks5-tunnel": RootBinary(
        name="hev-socks5-tunnel",
        sha256="547b0555bdf40b0662de92cd2fa7de14ad4f01d37b54575b9fb3e78134b9a342",
        install_path=SYS_DIR / "hev-socks5-tunnel",
    ),
}


def known_names() -> frozenset:
    """封闭集内的全部名字（唯一调用源：安装期对账 wanted 集、门禁比对）。"""
    return frozenset(ROOT_BINARIES)


def get(name: str) -> Optional[RootBinary]:
    return ROOT_BINARIES.get(name)


def sha256_file(path) -> Optional[str]:
    """文件 sha256；不存在/不可读返回 None（不抛异常，交由调用方决定语义）。"""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def verify_file(name: str, path) -> CheckResult:
    """校验一个 root 二进制：登记在册 + 存在 + 与发行态基线一致。

    不匹配一律**拒绝安装 / 拒绝执行**，绝不静默降级（§7.4.5 P0-1 的教训：
    降级路径就是提权路径）。
    """
    spec = ROOT_BINARIES.get(name)
    if spec is None:
        return False, f"未登记的 root 二进制：{name!r}（封闭集 {sorted(ROOT_BINARIES)}）"
    actual = sha256_file(path)
    if actual is None:
        return False, f"{name} 缺失或不可读：{path}"
    if actual != spec.sha256:
        return False, f"{name} 哈希与发行态基线不符（期望 {spec.sha256[:12]}…，实际 {actual[:12]}…）"
    return True, None


# ──────────────────────────────────────────────
# 完整性基线 manifest（安装期由 root 重写）
# ──────────────────────────────────────────────

def manifest_payload(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """本机应登记的基线：**发行态常量**，与磁盘现状无关。

    安装脚本据此写 manifest；helper 启动自检把磁盘现状与之比对。
    """
    payload = {name: spec.sha256 for name, spec in ROOT_BINARIES.items()}
    if extra:
        payload.update(extra)
    return payload


def write_manifest(path=None, extra: Optional[Dict[str, str]] = None) -> Path:
    """原子写 manifest（O_NOFOLLOW：末级符号链接直接失败，防 CWE-59）。

    仅在 root 上下文调用（安装脚本 / helper 自身）；普通用户写系统域会 PermissionError。
    """
    target = Path(path) if path is not None else MANIFEST_PATH
    payload = manifest_payload(extra)
    tmp = target.with_name(target.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
    os.replace(str(tmp), str(target))
    os.chmod(str(target), 0o600)
    return target


def load_manifest(path=None) -> Tuple[Dict[str, str], Optional[str]]:
    """读 manifest。缺失/半截/非对象 → ``({}, reason)``，调用方据此停机上报。"""
    target = Path(path) if path is not None else MANIFEST_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, "manifest.json 不存在（尚未安装或安装不完整）"
    except json.JSONDecodeError as exc:
        return {}, f"manifest.json 不是合法 JSON：{exc}"
    except OSError as exc:
        return {}, f"读取 manifest.json 失败：{exc}"
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
    ):
        return {}, "manifest.json 结构非法（需 name→sha256 字符串映射）"
    return raw, None


def verify_against_manifest(name: str, path, manifest: Dict[str, str]) -> CheckResult:
    """把磁盘文件与 manifest 基线比对（helper 侧「执行前自检」用）。"""
    expected = manifest.get(name)
    if expected is None:
        return False, f"manifest 未登记 {name!r}（基线缺失 → 拒绝执行）"
    actual = sha256_file(path)
    if actual is None:
        return False, f"{name} 缺失或不可读：{path}"
    if actual != expected:
        return False, f"{name} 与基线不符（期望 {expected[:12]}…，实际 {actual[:12]}…）"
    return True, None


# ──────────────────────────────────────────────
# 启动 / 执行前自检（§7.4.5 P1）
# ──────────────────────────────────────────────

#: 整目录自检只统计源码：``__pycache__`` 等运行期产物不参与（否则首次运行即失败）。
TREE_SUFFIX = ".py"


def hash_tree(root) -> Optional[str]:
    """整目录 ``*.py`` 的**聚合 sha256**（相对路径排序 + 逐文件摘要）。

    安装脚本与 helper 自检**共用同一实现**（单一真源），故两侧结果必然一致 ——
    若各写一份，「算法微调后一侧忘了跟上」会表现成「装完即报完整性失败」。

    只统计 ``*.py``：部署目录在系统域且 root 可写，Python 可能落 ``__pycache__``；
    把 bytecode 计入哈希会让首次运行后自检必然不符。
    """
    base = Path(root)
    try:
        files = sorted(p for p in base.rglob("*" + TREE_SUFFIX) if p.is_file())
    except OSError:
        return None
    if not files:
        return None
    digest = hashlib.sha256()
    for path in files:
        try:
            rel = path.relative_to(base).as_posix()
        except ValueError:          # pragma: no cover - rglob 结果必然在 base 下
            return None
        file_hash = sha256_file(path)
        if file_hash is None:
            return None
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def check_owner_perms(path, *, uid: int = 0, gid: int = 0) -> CheckResult:
    """§7.4.5 P1 自助检查前两项：owner = root:wheel，且无 group / other 写位。

    不做哈希 —— 只回答「谁能改它」。第三项（哈希）由
    ``verify_file`` / ``verify_tree_against_manifest`` 各自负责。
    """
    try:
        st = os.stat(path)
    except OSError as exc:
        return False, f"{path} 不可读：{exc}"
    if st.st_uid != uid or st.st_gid != gid:
        return False, f"{path} 所有者非 root:wheel（uid={st.st_uid} gid={st.st_gid}）"
    if st.st_mode & 0o022:
        return False, f"{path} 含 group/other 写位（mode={oct(st.st_mode & 0o777)}）"
    return True, None


def verify_tree_against_manifest(name: str, root, manifest: Dict[str, str]) -> CheckResult:
    """把整目录（helper 自身源码）与 manifest 基线比对。"""
    expected = manifest.get(name)
    if expected is None:
        return False, f"manifest 未登记 {name!r}（基线缺失 → 拒绝执行）"
    actual = hash_tree(root)
    if actual is None:
        return False, f"{name} 部署目录缺失或无可校验源码：{root}"
    if actual != expected:
        return False, f"{name} 与基线不符（期望 {expected[:12]}…，实际 {actual[:12]}…）"
    return True, None


def is_registered(name: str) -> bool:
    """该名字是否在 A 类封闭集内。"""
    return name in ROOT_BINARIES


def verify_registered(name: str, manifest: Dict[str, str]) -> CheckResult:
    """自检的「在册」一环：封闭集内 + manifest 已登记基线。

    ad-hoc 签名的 Mach-O 不能做哈希比对（签名改写字节、不可复现），其运行期守卫
    改用 ``codesign --verify`` + owner/权限，能证明的只有「它仍是安装时登记过的
    那一个名字」——本函数就是这一环（§9.3 已写死该定位，别指望它抗重签）。
    """
    if name not in ROOT_BINARIES:
        return False, f"未登记的 root 二进制：{name!r}（封闭集 {sorted(ROOT_BINARIES)}）"
    if name not in manifest:
        return False, f"manifest 未登记 {name!r}（基线缺失 → 拒绝执行）"
    return True, None
