"""hosts_guard/store：条目主数据（hosts_entries.json）读写。

条目与 config.yaml 分离的两个理由：
  1) 条目可达上千条（广告屏蔽列表），塞进用户手改的 config 会让文件无法阅读；
  2) config 热重载会反复解析整份配置，没必要为大体量列表买单。

故 config 只保留 enabled / resolve_poll 等设置，条目主数据落 ``hosts_entries.json``，
由 plugin 把两者合成一份期望态、经会话声明给守护（不再落任何中间文件）。
"""

from __future__ import annotations

import json
import logging

from barkit import paths
from barkit.plugins.hosts_guard.models import (
    DEFAULT_CATEGORY,
    PRESET_CATEGORIES,
    make_entry,
    normalize_host,
    validate_entry,
)

log = logging.getLogger(__name__)


def _empty() -> dict:
    return {
        "version": 1,
        "categories": [key for key, _label in PRESET_CATEGORIES],
        "entries": [],
    }


def load_raw() -> dict:
    """读条目主数据；缺失/损坏时返回空结构（绝不抛给调用方）。

    路径走 ``paths.HOSTS_ENTRIES_FILE``（而非按值导入），以便测试固件重定向
    ``barkit.paths`` 模块属性即可生效，不会写进真实用户目录。
    """
    try:
        data = json.loads(paths.HOSTS_ENTRIES_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty()
    except (OSError, ValueError) as e:
        log.warning("hosts_entries.json 读取失败，按空条目处理：%s", e)
        return _empty()
    if not isinstance(data, dict):
        return _empty()

    entries = []
    for item in data.get("entries") or []:
        if not isinstance(item, dict):
            continue
        err = validate_entry(item)
        if err is not None:
            log.warning("跳过非法条目（%s）：%s", err, item)
            continue
        entries.append(make_entry(
            host=item["host"], ip=item["ip"],
            category=item.get("category") or DEFAULT_CATEGORY,
            auto=bool(item.get("auto", False)),
            method=item.get("method") or "auto",
            note=item.get("note") or "",
        ))

    categories = [c for c in (data.get("categories") or []) if isinstance(c, str) and c]
    for key, _label in PRESET_CATEGORIES:
        if key not in categories:
            categories.append(key)
    return {"version": 1, "categories": categories, "entries": entries}


def load_entries() -> list[dict]:
    return load_raw()["entries"]


def load_categories() -> list[str]:
    return load_raw()["categories"]


def save_raw(data: dict) -> tuple[bool, str | None]:
    """写条目主数据（原子写：tmp + replace）。"""
    paths.APP_SUPPORT.mkdir(parents=True, exist_ok=True)
    target = paths.HOSTS_ENTRIES_FILE
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.chmod(0o644)
        tmp.replace(target)
    except OSError as e:
        log.error("写 hosts_entries.json 失败：%s", e)
        return False, str(e)
    return True, None


def save_entries(entries: list[dict], categories: list[str] | None = None) -> tuple[bool, str | None]:
    data = load_raw() if categories is None else {"version": 1, "categories": categories, "entries": []}
    data["entries"] = entries
    if categories is not None:
        data["categories"] = categories
    return save_raw(data)


def upsert(entry: dict) -> tuple[bool, str | None]:
    """新增或覆盖同名条目。返回 (是否有变化, 错误)。"""
    err = validate_entry(entry)
    if err is not None:
        return False, err
    data = load_raw()
    host = normalize_host(entry["host"])
    norm = make_entry(host=host, ip=entry["ip"], category=entry.get("category") or DEFAULT_CATEGORY,
                      auto=bool(entry.get("auto", False)), method=entry.get("method") or "auto",
                      note=entry.get("note") or "")
    for i, e in enumerate(data["entries"]):
        if e["host"] == host:
            if e == norm:
                return False, None          # 无变化，不写盘
            data["entries"][i] = norm
            break
    else:
        data["entries"].append(norm)
    ok, err = save_raw(data)
    return ok, err


def remove(host: str) -> tuple[bool, str | None]:
    """按域名删除条目。返回 (是否删到, 错误)。"""
    data = load_raw()
    target = normalize_host(host)
    before = len(data["entries"])
    data["entries"] = [e for e in data["entries"] if e["host"] != target]
    if len(data["entries"]) == before:
        return False, None
    ok, err = save_raw(data)
    return ok, err


def add_category(name: str) -> tuple[bool, str | None]:
    """新增自定义分类（不可与预置重名）。"""
    name = (name or "").strip()
    if not name:
        return False, "分类名不能为空"
    data = load_raw()
    if name in data["categories"]:
        return False, "分类已存在"
    data["categories"].append(name)
    ok, err = save_raw(data)
    return ok, err


def import_entries(
    pairs: list[tuple[str, str]],
    category: str = DEFAULT_CATEGORY,
    auto: bool = False,
    method: str = "auto",
) -> tuple[int, int, int]:
    """批量导入 (ip, host) 对。返回 (新增数, 覆盖数, 跳过数)。

    同名条目按「本次导入覆盖」处理（重复导入广告列表是常见操作，覆盖比报错有用）。
    """
    data = load_raw()
    index = {e["host"]: i for i, e in enumerate(data["entries"])}
    added = updated = skipped = 0
    for ip, host in pairs:
        host = normalize_host(host)
        entry = make_entry(host=host, ip=ip, category=category, auto=auto, method=method)
        if validate_entry(entry) is not None:
            skipped += 1
            continue
        if host in index:
            data["entries"][index[host]] = entry
            updated += 1
        else:
            index[host] = len(data["entries"])
            data["entries"].append(entry)
            added += 1
    save_raw(data)
    return added, updated, skipped
