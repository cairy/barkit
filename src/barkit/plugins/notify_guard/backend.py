"""notify_guard/backend：通知开关读写，单点隔离（方案 §4.2，任务 2.1）。

- 写路径：`defaults write -data`（走 cfprefsd IPC，缓存自动一致），失败回退直接写文件
- usernoted 重启（macOS 15 回归结论，2026-08 实测）：
  macOS 上 `launchctl kickstart` 被 SIP 拒绝（150: Operation not permitted），故 macOS
  直接 `killall usernoted`（launchd 自动拉活）；非 macOS 才优先尝试 kickstart。
- 后端只是最小写开关；词义约定：suppress=True → dndMirrored=True → 镜像时静音
"""

from __future__ import annotations

import logging
import os
import platform
import plistlib
import subprocess
import time
from pathlib import Path

from barkit.core.proc import run_cmd

log = logging.getLogger(__name__)

NCPREFS_PATH = Path.home() / "Library/Preferences/com.apple.ncprefs.plist"

#: 读缓存 TTL（秒）。面板每重算一次就要 ``open`` + ``plistlib.load`` 一遍系统这份
#: plist（内嵌二进制 plist，常见几十~几百 KB，纯 Python 解析）：notify_guard 3s
#: 一轮 ⇒ 一天两万多次，全在主线程。系统侧这个值的更新本来就不是亚秒级的，
#: 缓存 3 秒不影响「守卫 ↔ 系统是否 desync」的判定精度。
READ_TTL_S = 3.0


class BackendError(Exception):
    """读写失败（UI 需明确提示，不静默）。"""


class DndMirroredBackend:
    """dndMirrored 读写 backend。单点隔离，可整体替换。"""

    def __init__(self) -> None:
        self._read_at: float | None = None    # None = 从未读过（首次必读，不查缓存）
        self._cached: bool | None = None

    # ---------- 读 ----------

    def read(self) -> bool | None:
        """读取当前 dndMirrored；失败返回 None（调用方按时未知处理）。

        带 TTL 缓存（``READ_TTL_S``）：调用方既有 3s 一轮的 tick，也有面板每次
        重算 —— 后者一天能把这份系统 plist 解析两万多次。写路径会同步更新缓存，
        故「写完立刻读」不会读到旧值。
        """
        now = time.monotonic()
        if self._read_at is not None and now - self._read_at < READ_TTL_S:
            return self._cached
        try:
            with open(NCPREFS_PATH, "rb") as f:
                outer = plistlib.load(f)
            inner = plistlib.loads(outer["dnd_prefs"])
            value = inner.get("dndMirrored")
        except Exception as e:
            log.warning("读取 dnd_prefs 失败：%s", e)
            value = None
        self._read_at = now
        self._cached = value
        return value

    # ---------- 写 ----------

    def apply(self, suppress: bool) -> None:
        """写入 dndMirrored=suppress 并重启 usernoted。失败抛 BackendError。"""
        try:
            with open(NCPREFS_PATH, "rb") as f:
                outer = plistlib.load(f)
        except Exception as e:
            raise BackendError(f"读取 dnd_prefs 失败：{e}") from e
        inner = plistlib.loads(outer["dnd_prefs"])
        inner["dndMirrored"] = suppress
        new_data = plistlib.dumps(inner, fmt=plistlib.FMT_BINARY)

        # 1) defaults write -data（cfprefsd IPC，首选）
        try:
            res = run_cmd(
                ["defaults", "write", "com.apple.ncprefs", "dnd_prefs", "-data", new_data.hex()]
            )
        except subprocess.TimeoutExpired as e:
            raise BackendError(f"defaults write 超时：{e}") from e
        if res.returncode != 0:
            msg = (res.stderr or "").strip() or f"exit {res.returncode}"
            log.warning("defaults write 失败（%s），回退直接写文件", msg)
            self._write_file_direct(outer, new_data)

        # 2) 重启 usernoted：kickstart（旧系统）→ killall（macOS 15 SIP 挡 kickstart）
        self._restart_usernoted()
        # 同步缓存：否则「刚写完的这一轮」会读到 TTL 里的旧值，desync 判定
        # 当场报「系统静音、守卫期望允许」这种假警报。
        self._cached = suppress
        self._read_at = time.monotonic()
        log.info("已写入 dndMirrored=%s，usernoted 已重启", suppress)

    def _write_file_direct(self, outer: dict, new_data: bytes) -> None:
        """回退方案：直接写 plist 文件 + killall cfprefsd 强制落盘重读。"""
        outer["dnd_prefs"] = new_data
        tmp = NCPREFS_PATH.with_name(NCPREFS_PATH.name + ".tmp")
        with open(tmp, "wb") as f:
            plistlib.dump(outer, f, fmt=plistlib.FMT_BINARY)
        os.replace(tmp, NCPREFS_PATH)
        run_cmd(["killall", "cfprefsd"])
        run_cmd(["killall", "usernoted"])

    def _restart_usernoted(self) -> None:
        """重启 usernoted（best-effort）。macOS 上 kickstart 被 SIP 拦截，直接
        killall（launchd 自动拉活）；非 macOS 优先 kickstart，失败再 killall。

        `killall` 退出码语义（macOS）：
          - 0：命中并发送信号，launchd 会重新拉起 → 正常重启
          - 1：未命中任何进程（"No matching processes"）→ usernoted 当前没在
               跑，无需重启；prefs 已由前面的 defaults write 经 cfprefsd IPC
               生效，这**不算失败**，忽略即可（连续切换时此窗口常见）
          - 其他：真实错误（如权限被拒）→ 才抛 BackendError
        """
        if platform.system() != "Darwin":
            uid = os.getuid()
            res = run_cmd(["launchctl", "kickstart", "-k", f"gui/{uid}/com.apple.usernoted"])
            if res.returncode == 0:
                return
            log.warning(
                "kickstart usernoted 失败（%s），降级 killall",
                (res.stderr or "").strip() or res.returncode,
            )
        res2 = run_cmd(["killall", "usernoted"])
        if res2.returncode == 0:
            return
        if res2.returncode == 1:
            # 未命中进程：usernoted 当前未运行，无需重启，忽略
            log.info("usernoted 当前未运行，跳过重启（prefs 已由 defaults write 生效）")
            return
        raise BackendError(
            f"重启 usernoted 失败：{(res2.stderr or '').strip() or res2.returncode}"
        )