"""core/exporter：插件数据导出 / 导入（方案 §9.1，批次 6c）。

打包格式：``barkit-plugin-<id>-<ts>.zip``
    manifest.yaml   插件身份 + 导出格式版本（导入时校验 api_version，不匹配拒绝）
    config.json     plugin.export_data() 的载荷（SECRET 默认排除，见下）
    hotkeys.json    {name: combo} 用户差异项
    data/<rel>      resources().data_files 声明的数据文件（含种子——种子已是用户数据）

SECRET 处理（方案待决 7，收缩版）：导出统一 ``include_secret=False`` 起；插件按
``export_data(include_secret: bool = False)`` 签名约定接受该参数（签名不含此参的
插件忽略）。§9.5 config_schema() 接线后，SECRET 判定改由 FieldType.SECRET 驱动，
勾选 UI 随 U 轮次补。

写入约定：数据文件相对 ``paths.APP_SUPPORT``；导入时逐项校验相对路径安全
（拒绝绝对路径 / 穿越），且必须是插件 ``data_files`` 声明过的文件——不多收、
不越界。
"""

from __future__ import annotations

import inspect
import json
import logging
import time
import zipfile
from pathlib import Path

import yaml

from barkit import paths
from barkit.contribution.points import SUPPORTED_API_VERSION
from barkit.core.installer import _unsafe_rel_path
from barkit.core.resources import PluginResources

log = logging.getLogger(__name__)

EXPORT_FORMAT_VERSION = 1


# ---------- 导出 ----------

def _call_export(plugin, include_secret: bool) -> dict:
    """按签名约定调用 export_data：签名含 include_secret 才传参。"""
    try:
        sig = inspect.signature(plugin.export_data)
    except (TypeError, ValueError):
        return dict(plugin.export_data() or {})
    payload = (
        plugin.export_data(include_secret=include_secret)
        if "include_secret" in sig.parameters
        else plugin.export_data()
    )
    return dict(payload or {})


def _collect_hotkeys(plugin, config) -> dict[str, str]:
    """插件声明的热键 → 用户已配置的组合键（未配置项取默认组合）。"""
    out: dict[str, str] = {}
    try:
        specs = list(plugin.hotkeys() or [])
    except Exception:  # noqa: BLE001
        return out
    for s in specs:
        if not s.name:
            continue
        combo = config.get(f"hotkeys.{s.name}", s.default_combo)
        if combo:
            out[s.name] = combo
    return out


def export_plugin_zip(
    plugin, config, dest_dir: Path, include_secret: bool = False
) -> Path:
    """打包插件用户数据；返回 zip 路径。导出失败（OSError 等）向上抛给调用方。"""
    pid = plugin.id
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    zip_path = dest_dir / f"barkit-plugin-{pid}-{ts}.zip"

    resources = plugin.resources() if hasattr(plugin, "resources") else PluginResources()
    hotkeys = _collect_hotkeys(plugin, config)
    manifest = {
        "format_version": EXPORT_FORMAT_VERSION,
        "id": pid,
        "name": getattr(plugin, "name", "") or pid,
        "api_version": getattr(plugin, "api_version", SUPPORTED_API_VERSION),
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "secret_included": bool(include_secret),
    }
    payload = _call_export(plugin, include_secret)

    base = paths.APP_SUPPORT
    data_files: list[tuple[Path, str]] = []
    for rel in (resources.data_files if isinstance(resources, PluginResources) else []):
        if not isinstance(rel, str) or _unsafe_rel_path(rel):
            continue
        f = base / rel
        if f.is_file():
            data_files.append((f, rel))

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.yaml", yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False))
        zf.writestr("config.json", json.dumps(payload, ensure_ascii=False, indent=2))
        zf.writestr("hotkeys.json", json.dumps(hotkeys, ensure_ascii=False, indent=2))
        for f, rel in data_files:
            zf.write(f, f"data/{rel}")
    log.info("插件 %s 已导出：%s（含 secret=%s）", pid, zip_path, include_secret)
    return zip_path


# ---------- 导入 ----------

def read_export_manifest(zip_path: Path) -> "dict | None":
    """读导出包 manifest（导入预检 / UI 展示用）。非法返回 None。"""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            if "manifest.yaml" not in zf.namelist():
                return None
            data = yaml.safe_load(zf.read("manifest.yaml"))
    except (OSError, zipfile.BadZipFile, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def import_plugin_zip(plugin, config, zip_path: Path) -> "tuple[bool, str | None]":
    """把导出包恢复到**已安装**的插件（配置 + 热键 + 数据文件）。

    调用方保证插件处于 stop 状态（PluginManager 编排 disable → import → enable）。
    返回 (是否成功, 错误)。
    """
    pid = plugin.id
    manifest = read_export_manifest(zip_path)
    if manifest is None:
        return False, "导出包缺失或 manifest 无法解析"
    if manifest.get("id") != pid:
        return False, f"导出包身份不匹配：包内 {manifest.get('id')!r} ≠ 目标 {pid!r}"
    if int(manifest.get("api_version", -1)) != SUPPORTED_API_VERSION:
        return False, (
            f"导出包 api_version={manifest.get('api_version')} 与当前框架"
            f" v{SUPPORTED_API_VERSION} 不匹配，拒绝导入"
        )

    try:
        with zipfile.ZipFile(zip_path) as zf:
            # 1. 配置段：交插件 import_data（默认写回 plugins.<id>）
            payload = {}
            if "config.json" in zf.namelist():
                payload = json.loads(zf.read("config.json") or "{}")
            if not isinstance(payload, dict):
                return False, "config.json 非法（顶层必须是 mapping）"

            # 2. 数据文件：只收插件声明过的相对路径，逐项防越界
            resources = plugin.resources() if hasattr(plugin, "resources") else PluginResources()
            allowed = {
                rel for rel in (resources.data_files if isinstance(resources, PluginResources) else [])
                if isinstance(rel, str) and not _unsafe_rel_path(rel)
            }
            base = paths.APP_SUPPORT
            data_entries: list[tuple[str, bytes]] = []
            for name in zf.namelist():
                if not name.startswith("data/"):
                    continue
                rel = name[len("data/"):]
                if not rel or rel not in allowed:
                    return False, f"导出包含未声明的数据文件：{rel!r}（拒绝导入）"
                data_entries.append((rel, zf.read(name)))

            # 3. 热键
            hotkeys = {}
            if "hotkeys.json" in zf.namelist():
                hotkeys = json.loads(zf.read("hotkeys.json") or "{}")
            if not isinstance(hotkeys, dict):
                return False, "hotkeys.json 非法"

            # ---- 校验全部通过，开始落盘 ----
            for rel, blob in data_entries:
                target = (base / rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(blob)
                target.chmod(0o644)
            for name, combo in hotkeys.items():
                if isinstance(name, str) and isinstance(combo, str) and name:
                    config.set_hotkey(name, combo)
            plugin.import_data(payload)
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError, ValueError) as e:
        return False, f"导入失败：{e}"
    log.info("插件 %s 导入完成：%s", pid, zip_path)
    return True, None
