"""core/autostart：开机自启 LaunchAgent（方案 §7.1 定案 + F.2）。

形态：无 bundle 源码运行（D8），SMAppService 不可用 → LaunchAgent plist：

- plist 指向**稳定 wrapper** `~/.local/bin/barkit`：wrapper 生成时写死本机
  venv python 绝对路径与项目根。venv 目录移动 / Python 重建后 wrapper 失效
  （README 提示：重新「安装自启」一次即可）。
- `KeepAlive={SuccessfulExit=false}`：崩溃自动拉起；托盘「退出」是正常退出，
  不会被拉活（开机自启同样覆盖，且避免“退不出去”）。
- LaunchAgent 环境干净（无 PATH/LANG）：wrapper 显式设 PATH；本模块 subprocess
  一律绝对路径（launchctl /usr/bin）。
- 用户域操作免 root：bootstrap/bootout gui/$(id -u) 即可，无需 sudo。

版本/自愈语义（避免 launchctl 杀到运行中的本进程）：
- 已加载且 wrapper 内 python 与当前解释器一致 → 只刷新文件，不动 launchd job
  （launchd 每次拉起都执行 wrapper，下次启动自然用新内容）
- 已加载且 python 已变（venv 重建）→ 需要重载 launchd 定义才能换路径生效，
  用 detached 脚本 bootout→bootstrap，本进程会被终止，UI 预先提示
- 卸载 = 删 plist；若 job 加载中 → detached bootout（运行实例被终止），
  下次开机不再自启
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
from pathlib import Path

from barkit import paths
from barkit.core.proc import run_cmd

log = logging.getLogger(__name__)

PLIST_LABEL = "com.barkit.app"
LAUNCH_AGENTS_DIR = Path.home() / "Library/LaunchAgents"
PLIST_PATH = LAUNCH_AGENTS_DIR / (PLIST_LABEL + ".plist")
WRAPPER_PATH = Path.home() / ".local/bin/barkit"

_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{wrapper}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>{stdout}</string>
    <key>StandardErrorPath</key>
    <string>{stderr}</string>
</dict>
</plist>
"""


# ──────────────────────────────────────────────
# 文件生成
# ──────────────────────────────────────────────

def wrapper_content(python: str | None = None, project_root: Path | None = None) -> str:
    """wrapper 脚本：cd 项目根 + 显式 PATH + 用写死的 venv python 启动。"""
    python = python or sys.executable
    root = project_root or Path(__file__).resolve().parents[3]
    return f"""#!/bin/bash
# BarKit 自启 wrapper（安装自启时生成，写死本机 venv 路径）
# venv 移动 / Python 重建后此处失效：在面板重新「安装自启」即可。
cd {shlex.quote(str(root))}
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:${{PATH:-}}"
export LANG="zh_CN.UTF-8"
exec {shlex.quote(python)} -m barkit.app
"""


def plist_content(wrapper: Path = WRAPPER_PATH) -> str:
    """自启 plist。``Standard{Out,Error}Path`` 指向 ``paths.STARTUP_LOG``（**调用期**读）。

    这两条键是「启动就没了」唯一的现场：logging 起来之前的那段输出走的是 fd 1/2，
    没有它们就交给 launchd 丢弃（改前正是如此），``barkit.log`` 里一个字都没有。
    常态该文件 0 字节 —— Python 层的输出由 ``logging_setup.capture_stdio`` 收走。

    旧装的 plist 没有这两条 ⇒ 需在面板重新「安装自启」一次才生效（wrapper 路径没变
    时那只刷新文件、不动 launchd job）。
    """
    target = str(paths.STARTUP_LOG)
    return _PLIST_TEMPLATE.format(
        label=PLIST_LABEL, wrapper=str(wrapper), stdout=target, stderr=target
    )


def _wrapped_python() -> str | None:
    """从现有 wrapper 解析 venv python 路径，缺/坏返回 None。"""
    try:
        for line in WRAPPER_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("exec "):
                return shlex.split(line)[1]
    except OSError:
        return None
    return None


# ──────────────────────────────────────────────
# 状态检测
# ──────────────────────────────────────────────

def installed() -> bool:
    return PLIST_PATH.exists() and WRAPPER_PATH.exists()


def running() -> bool:
    """launchctl print 成功即视为 job 已加载（非 root 可查自身域）。"""
    res = run_cmd(["/bin/launchctl", "print", f"gui/{os.getuid()}/{PLIST_LABEL}"])
    return res.returncode == 0


def status() -> dict:
    """聚合状态：装否/运行否/是否过期（wrapper 指向的 python 与当前不一致）。"""
    inst = installed()
    wrapped = _wrapped_python()
    return {
        "installed": inst,
        "running": running(),
        "stale": inst and wrapped is not None and wrapped != sys.executable,
    }


# ──────────────────────────────────────────────
# 安装 / 卸载
# ──────────────────────────────────────────────

def _detach_reload() -> None:
    """detached 重载 job（bootout→bootstrap）：本进程会被终止，UI 需先提示。"""
    uid = os.getuid()
    script = (
        f'sleep 1; /bin/launchctl bootout gui/{uid}/{PLIST_LABEL} 2>/dev/null; '
        f'/bin/launchctl bootstrap gui/{uid} {shlex.quote(str(PLIST_PATH))}'
    )
    subprocess.Popen(
        ["/bin/bash", "-c", script],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def install() -> tuple[bool, str]:
    """
    安装自启。返回 (ok, hint)：
    - ok=False 与提示一并给 UI 弹错
    - hint="reload" 表示 job 需重载，本进程即将被终止（venv 重建后的重装路径）
    - 其余成功 hint=""
    """
    WRAPPER_PATH.parent.mkdir(parents=True, exist_ok=True)
    old_python = _wrapped_python()   # 写文件前先取旧 wrapper 指向（判定是否需重载）
    WRAPPER_PATH.write_text(wrapper_content(), encoding="utf-8")
    os.chmod(WRAPPER_PATH, 0o755)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_text(plist_content(), encoding="utf-8")
    os.chmod(PLIST_PATH, 0o644)
    log.info("自启文件已写入：%s / %s", WRAPPER_PATH, PLIST_PATH)

    if running():
        # 已加载：wrapper 未过期则只用文件刷新（下次拉起自然生效）
        if old_python == sys.executable:
            log.info("自启已是最新（wrapper python 未变），无需重载 launchd")
            return True, ""
        log.info("venv 路径已变化，安排 launchd 重载（本进程将被终止）")
        _detach_reload()
        return True, "reload"

    res = run_cmd(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST_PATH)])
    if res.returncode != 0:
        log.error("launchctl bootstrap 失败：%s", (res.stderr or res.stdout).strip()[-300:])
        return False, (res.stderr or res.stdout).strip()[-300:]
    log.info("开机自启已安装：%s", PLIST_LABEL)
    return True, ""


def uninstall() -> tuple[bool, str]:
    """
    卸载自启。返回 (ok, hint)：
    - hint="dying"：job 加载中且就是我们 → detached bootout，本进程将被终止
    - 其余成功 hint=""
    """
    existed = PLIST_PATH.exists()
    try:
        PLIST_PATH.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log.error("删除自启 plist 失败：%s", e)
        return False, f"删除 plist 失败：{e}"

    if running():
        # 卸载后 job 仍加载的话，KeepAlive 还会拉起崩溃的实例——必须 bootout
        log.info("自启 job 运行中，安排 bootout（本进程将被终止）")
        subprocess.Popen(
            ["/bin/bash", "-c",
             f'sleep 1; /bin/launchctl bootout gui/{os.getuid()}/{PLIST_LABEL} 2>/dev/null'],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True, "dying"

    log.info("开机自启已关闭%s", "" if existed else "（原本未安装）")
    return True, ""