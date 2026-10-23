"""配置文件的读与写两个原语：YAML 读取、原子写（tmp ＋ rename ＋ chmod 600）。

``config.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
import yaml


def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


def atomic_write(path: Path, content: str) -> None:
    """崩溃安全的原子写：随机名 tmp ＋ fsync ＋ os.replace，chmod 600。

    旧实现两处短板：
    - 没有 ``fsync``：``rename`` 只保证目录项原子，不保证 tmp 内容已落介质；
      APFS 上写完即崩溃可能留下 0 字节 / 半截 ``config.yaml``，重启后
      ``_load_yaml`` 读空文件 → 静默按「用户什么都没配过」启动（全部自定义配置消失）。
    - tmp 名可预测（``config.yaml.tmp``）：同用户进程可预置它为符号链接，让写入
      截断链接指向的目标。改用 ``mkstemp`` 随机名 ＋ 0600，根上消除这个面。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".cfg.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
