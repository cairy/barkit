"""capabilities/helper_client：helper（root LaunchDaemon）的 **app 侧客户端**
（安装 / 卸载 / 状态 / 版本对账；方案 §7.4 / §9.3，批次 4）。

批次 4 起 helper 是**框架资产**——`capabilities/helper/` 的整目录包，不再挂在
route_guard 名下的单文件。本模块是它的 app 侧另一半，被 route_guard /
ipad_tunnel / hosts_guard 共用（三者共享同一个 root helper，只弹一次授权）。

**归属（2026-09-14 改定案，插件结构规范 §8.1）**：本模块从
`plugins/route_guard/daemon.py` 上提到这里。它原先停在插件包里，导致
`ipad_tunnel/__init__.py` 与 `hosts_guard/plugin.py` 各有一处**跨插件 import**
（框架基础设施长在了插件里）。§8.1 原定案放 `core/`，但与分层门禁
`test_layer_dependency_direction_core`（core 禁 import `barkit.capabilities`）冲突：
本模块要调 `rootbin.verify_file()`，靠的是 `ROOT_BINARIES` 里**硬编码的 sha256
发行态基线**——这不是「名字」能当名级副本的东西，复制一份进 core 就等于给 A 类
数据立了第二真源。改定案落在 `capabilities/`：与 `helper/` 同包（依赖天然合规）、
零新增门禁白名单、零第二真源。

✅ **切分点 2 已落地（2026-09-17）**：`uninstall()` 不再内嵌「读 `routes.json` → 逐条
`/sbin/route delete`」。那段是 **route_guard 的领域知识**（框架客户端不该认识某插件的
期望态文件名），而且它读的正是插件卸载时**已经删掉**的那份文件 ⇒ 在最常见的卸载顺序下
静默无效，连兜底都算不上。正确形态已按 §8.1 落地：**期望态由插件在自己的 `teardown()`
里逐条撤，框架等外部消费方确认撤净之后才做收尾**（延迟删除，见 `core/deferred_cleanup`
与 `PluginResources.deferred`）。

由此得到的**调用前提**：`uninstall()` 的调用方必须先确保没有插件还在声明 helper task
（否则那些插件写在用户域的期望态再没人读；守护的退出钩子只复原隧道态，不碰静态路由）。

**本模块是全 app 唯一的 root 授权入口**（`run_admin` → osascript）：A 类 root 二进制
的部署与回收、helper 的安装与卸载都从这里走，不新增第二条提权路径。

- 一次性安装（osascript with administrator privileges）：
  递归复制 `capabilities/helper/` 下全部 `.py`（**保留子包结构**）→
  `/Library/Application Support/BarKit/helper/`
  + 按插件声明部署 A 类二进制 + 写启动器 `barkit-helper`（`exec python3 -m helper`）
  + 写**完整性基线** `manifest.json`（root:wheel 0600）+ plist
  + 清理旧 label（com.barkit.routed）+ launchctl bootstrap
- plist 关键：KeepAlive={SuccessfulExit=false}（崩溃自拉起，正常退出不拉活）
  + ThrottleInterval 防崩溃循环；EnvironmentVariables 注入 BARKIT_DATA_DIR（用户域）、
  BARKIT_UID（**被服务者的 uid**，守护开机先起时推不出这个数）与 PYTHONPATH
  （让 `-m helper` 找得到包）
- A 类对账（§9.3）：`wanted` = 全部已安装插件声明的并集（`Registry.root_binary_sources`），
  `deployed` = 系统域现存；部署恒等于 wanted（`install -m` 幂等覆盖），回收取差集
  `deployed − wanted`。**仅在回收差集非空时才需要第二次授权**。
- 卸载：bootout（新 + 旧 label）→ 删 plist/启动器/helper 目录/manifest/A 类二进制
  （框架级整体拆除，§9.4）。**只拆框架自己的东西**，不替插件撤系统状态（见上文切分点 2）
- 版本锚点：期望版本从仓库 `capabilities/helper/version.py` 提取（单一来源），
  已装版本从系统域同名文件读；两者不符 → 面板提示重装
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from barkit.capabilities.helper import rootbin
from barkit.core.proc import run_cmd
from barkit.core.rootbin_reconcile import deployed, reconcile
from barkit import paths

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# label 与安装位
# ──────────────────────────────────────────────

PLIST_LABEL = "com.barkit.helper"
# 批次 4 之前的 label（单文件 helper 形态）。安装/卸载都必须把它清掉：
# 旧 daemon 若还在跑，会带着旧代码继续改系统网络状态，热修看似生效实则失效。
LEGACY_PLIST_LABELS = ("com.barkit.routed",)

PLIST_PATH = Path("/Library/LaunchDaemons") / (PLIST_LABEL + ".plist")
LEGACY_PLIST_DIR = Path("/Library/LaunchDaemons")

SYS_HELPER_DIR = Path("/Library/Application Support/BarKit")
HELPER_INSTALL_DIR = SYS_HELPER_DIR / "helper"          # 整目录部署位（root:wheel）
HELPER_VERSION_PATH = HELPER_INSTALL_DIR / "version.py"  # 版本锚点（已装）
LAUNCHER_INSTALL_PATH = SYS_HELPER_DIR / "barkit-helper"  # 无扩展名：系统设置里显示名可辨识
LAUNCHER_NAME = "barkit-helper"

#: 完整性基线（真源在 rootbin，此处只是引用，不另立副本）。
MANIFEST_PATH = rootbin.MANIFEST_PATH

# 旧形态残留（单文件 helper + 旧启动器），安装/卸载时一并清掉
LEGACY_HELPER_FILES = (
    SYS_HELPER_DIR / "routed-helper.py",
    SYS_HELPER_DIR / "routed-helper",
)

# 仓库内源码：本模块就住在 `capabilities/` 里，helper 源码是它的**同级目录**。
# 刻意不写「数 .parent 上溯」：那种写法在文件一搬家就会**静默**指错——本模块原在
# `plugins/route_guard/` 时是三级 `.parent`，直接搬进 `core/` 会多上溯一层落到
# `src/capabilities/helper`（不存在），安装器据此报「源缺失」→ 拒绝安装。
# 正确性由单测断言 ``HELPER_SRC_DIR.is_dir()`` 守护（不靠路径算术自证）。
HELPER_SRC_DIR = Path(__file__).resolve().parent / "helper"
HELPER_SRC_VERSION_PATH = HELPER_SRC_DIR / "version.py"

# 降频缓存（长时间运行、最少资源占用）
LAUNCHCTL_TTL = 30.0    # 秒，launchctl print 结果缓存（运行状态低频变化）
STATUS_TTL = 30.0       # 秒，status() 聚合结果缓存

#: ``at`` 一律用 ``None`` 表示「**还没有过任何一次有效观测**」,不用 ``0.0``。
#: 理由：这里的时刻取自 ``time.monotonic()``,而它是**开机以来的秒数**(不是进程启动
#: 以来的秒数) ⇒ 拿 ``0.0`` 当「没缓存」的哨兵时,开机头 30 秒里
#: ``now - 0.0 < TTL`` 恒真、缓存被当成新鲜,于是**一次都不问**就返回默认值
#: ``False`` —— 面板在开机那一小段里写着「守护未运行」,而真值没人去读。
_launchctl_cache: dict = {"at": None, "running": False}
#: ``value is None`` 本身就代表「没缓存」,``at`` 只用 ``None`` 与它保持同一口径
#: (见上一条:同一份表里两种「没有」的写法,下一个人只会照着其中一个写)。
_status_cache: dict = {"at": None, "value": None}
#: 「launchctl 查询失败」降噪位：无论失败多少次只 WARN 一次，恢复后立即清零。
#: 为什么需要它：查询走的是 5s 超时的子进程，而面板每 5s 问一次 —— 一次持续
#: 故障会把同一句 warning 刷成每小时几百行，反而看不清真正的现场。
_launchctl_failed = {"value": False}

_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{launcher}</string>
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
    <key>EnvironmentVariables</key>
    <dict>
        <key>BARKIT_DATA_DIR</key>
        <string>{data_dir}</string>
        <!-- 被服务的那个用户的 uid。安装时就知道（本进程就是它跑起来的），
             而守护开机先起时推不出这个数：它的兜底是「数据目录的属主」，
             而目录若是由守护自己建出来的，属主就是 root —— 拿它当被服务者会把
             数据目录与套接字一起锁成 root:root 0700，app 从此连不上也写不进。
             有了这一项，那条推断就不再是唯一的路。 -->
        <key>BARKIT_UID</key>
        <string>{uid}</string>
        <key>PYTHONPATH</key>
        <string>{pythonpath}</string>
        <!-- 不在系统域落 __pycache__：部署包是 root:wheel 0755，Python 有权写，
             而 bytecode 会让「源码聚合哈希」自检在首次运行后失败。 -->
        <key>PYTHONDONTWRITEBYTECODE</key>
        <string>1</string>
    </dict>
</dict>
</plist>
"""


def launcher_content() -> str:
    """launcher 脚本：固定进程显示名（barkit-helper），exec `python3 -m helper`。

    系统设置/活动监视器显示的是 ProgramArguments 里首个程序的 basename；直接以
    /usr/bin/python3 跑只能认出 "python"，套一层同目录 bash 启动器后显示为
    "barkit-helper"，便于辨识与排查。整目录包形态要求按模块运行（`-m helper`），
    故显式导出 PYTHONPATH 指向安装根目录（plist 里也有，这里是二次保险）。
    """
    return (
        "#!/bin/bash\n"
        "# BarKit helper 启动器：系统设置/活动监视器显示为 barkit-helper 而非 python\n"
        f'export PYTHONPATH="{SYS_HELPER_DIR}"\n'
        "# 系统域不留 __pycache__（否则源码聚合哈希自检首次运行后必然不符）\n"
        "export PYTHONDONTWRITEBYTECODE=1\n"
        "exec /usr/bin/python3 -m helper\n"
    )


# ──────────────────────────────────────────────
# 状态检测（app 侧，无需 root）
# ──────────────────────────────────────────────

def helper_installed() -> bool:
    return PLIST_PATH.exists() and HELPER_VERSION_PATH.exists()


def helper_running() -> bool:
    """launchctl print 成功即视为已注册/运行。非 root 可查。

    结果 30s TTL 缓存（launchctl print 偶发挂起，且运行状态低频变化）；
    显式 5s 超时，**查询失败保持上次结果、且不刷新 TTL**（防 tick 被卡死）。

    「还没问过」用 ``at is None`` 判，**不许**拿 ``0.0`` 当哨兵：``time.monotonic()``
    是开机以来的秒数，开机头 30 秒里 ``now - 0.0 < LAUNCHCTL_TTL`` 恒真 ⇒ 缓存被
    当成新鲜，直接返回默认值 ``False``，一次都不问（面板那一段写着「守护未运行」）。
    改回 ``0.0`` 会让 ``test_launchctl_cache_is_empty_right_after_boot`` 转红。
    """
    now = time.monotonic()
    cached_at = _launchctl_cache["at"]
    if cached_at is not None and now - cached_at < LAUNCHCTL_TTL:
        return _launchctl_cache["running"]
    try:
        res = run_cmd(["launchctl", "print", f"system/{PLIST_LABEL}"], timeout=5)
        running = res.returncode == 0
    except Exception as e:  # noqa: BLE001 —— 超时 / 找不到 label 都不该冒泡被打成崩溃
        # 查询失败**不算一次有效观测**：不写 ``at`` ⇒ 下一个调用方立刻再问一遍，
        # 而不是把这句「没测出来」按 30s TTL 一次次续命 —— 续命的后果是守护已经
        # 不在了而面板永远写着「运行中」，且全程一行日志都没有。
        if not _launchctl_failed["value"]:
            _launchctl_failed["value"] = True
            log.warning("launchctl 查询失败，沿用上次结论（运行中=%s）：%s",
                        _launchctl_cache["running"], e)
        else:
            log.debug("launchctl 查询仍失败：%s", e)
        return _launchctl_cache["running"]
    _launchctl_failed["value"] = False
    _launchctl_cache["at"] = now
    _launchctl_cache["running"] = running
    return running


def _extract_version(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r'HELPER_VERSION\s*=\s*["\']([^"\']+)["\']', text)
    return m.group(1) if m else None


#: 期望版本读不出来时的哨兵值。**它不是版本号**：任何真实版本号都不等于它，
#: 拿它去比就会得到「永远过期」（见 :func:`status` 里对它的排除）。
VERSION_UNKNOWN = "?"


def expected_version() -> str:
    """期望版本：从仓库 `capabilities/helper/version.py` 提取（单一来源）。"""
    return _extract_version(HELPER_SRC_VERSION_PATH) or VERSION_UNKNOWN


def installed_version() -> str | None:
    """已装 helper 的版本（**盘上那份**）；未安装返回 None。"""
    return _extract_version(HELPER_VERSION_PATH)


# 「此刻跑着的守护自陈的版本」——由框架在握手成功那一刻填进来，断开即清空。
# 为什么是模块级而不是参数：版本闸门的调用方有八处、分在三个插件里，让它们各自
# 去够 ``ctx.helper`` 只会把「版本从哪来」这件事摊开成八份。这里收成一个口，
# 谁都能读到同一份，而写方只有一个（装配层）。
_LIVE_VERSION: dict = {"value": ""}


def set_live_version(version: str) -> None:
    """装配层告知「守护自陈的版本」；传空串即「此刻没人在跑」。

    版本变了要顺手废掉 ``status()`` 的缓存，否则重装之后最长 30 秒内仍会念旧结论。
    """
    value = str(version or "")
    if _LIVE_VERSION["value"] == value:
        return
    _LIVE_VERSION["value"] = value
    _invalidate_status_cache()


def live_version() -> str:
    """守护自陈的版本；没连上返回空串。"""
    return _LIVE_VERSION["value"]


def install_format_outdated() -> bool:
    """安装形态过期（老形态直跑 python3 / 缺新启动器）→ 需重装。

    版本号落后由 status() 的 `ver != expected_version()` 主判（版本号是单一权威
    信号，schema 变动必须 bump 版本）；这里只补「版本号意外一致但安装形态/文件残缺」
    的兜底。
    """
    if not HELPER_VERSION_PATH.exists():
        return True  # 新形态锚点缺失 → 未装或装的是老形态 → 重装
    try:
        plist_text = PLIST_PATH.read_text(encoding="utf-8")
    except OSError:
        return False  # plist 缺失单独交给 helper_installed() 处理「未安装」
    # 老形态：ProgramArguments 指向 python3 或旧启动器，而非新的 barkit-helper
    return f"<string>{LAUNCHER_INSTALL_PATH}</string>" not in plist_text


def _invalidate_status_cache() -> None:
    """安装/卸载等状态变更后失效 status() 缓存。

    连带失效 ``launchctl`` 那份：``status()["running"]`` 的答案就在那儿，只清
    一半 ⇒ 刚装完 / 刚卸完的那 30 秒里面板照旧念反话（「已卸载却仍显示运行中」）。

    两份都写 ``at = None``（不是 ``0.0``）——「没有过任何观测」与「观测于第 0 秒」
    必须是两个状态，见 :data:`_launchctl_cache`。
    """
    _status_cache["value"] = None
    _status_cache["at"] = None
    _launchctl_cache["at"] = None
    _launchctl_failed["value"] = False


def status() -> dict:
    """聚合状态：装否/运行否/版本/是否过期（版本不对 或 老形态需重装）。

    30s TTL 缓存：面板每 5s 轮询，无状态变更时零额外 I/O。

    **版本先看守护自陈的那一份**（``live_version``，握手给的），盘上那份只在没连上
    时才算数 —— 前者回答「此刻跑着的代码是哪一版」，后者回答「装了哪一版」。两处
    **任一处不符即过期**：只取其一都会漏掉一种真会发生的事（装了没重启 / 跑着的是
    残留的旧进程）。
    """
    now = time.monotonic()
    cached = _status_cache["value"]
    if cached is not None and now - _status_cache["at"] < STATUS_TTL:
        return cached
    inst = helper_installed()
    ver = installed_version()
    live = live_version()
    want = expected_version()
    # 期望版本读不出来（哨兵值）时**不参与比较**：那是「不知道」，不是一个可以比
    # 的版本号。拿它去比 ⇒ 任何真实版本号都不等于它 ⇒ 三个插件一起永久报「需重装」，
    # 而用户按提示重装多少次都不会变（问题从来不在盘上那份）。
    known = want != VERSION_UNKNOWN
    version_mismatch = known and any(
        v is not None and v != want for v in (ver, live or None)
    )
    st = {
        "installed": inst,
        "running": helper_running(),
        "version": ver,
        "live_version": live or None,
        "expected": want,
        "outdated": inst and (version_mismatch or install_format_outdated()),
    }
    _status_cache["at"] = now
    _status_cache["value"] = st
    return st


# ──────────────────────────────────────────────
# 授权执行（osascript with administrator privileges，一次授权）
# ──────────────────────────────────────────────

def run_admin(script_path: str, timeout: int = 120) -> bool:
    """执行一个已生成的 shell 脚本（root）。返回是否成功（用户取消/超时/失败 → False）。"""
    cmd = [
        "osascript", "-e",
        f'do shell script "/bin/bash \\"{script_path}\\"" with administrator privileges',
    ]
    try:
        res = run_cmd(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.warning("系统授权等待超时（%ds），流程中止", timeout)
        return False
    if res.returncode != 0:
        log.error("系统授权流程失败：%s", (res.stderr or "未知错误").strip()[-400:])
        return False
    return True


def _write_script(name: str, content: str) -> Path:
    """写一份**待 root 执行**的脚本（三件事都是被 root 执行这一性质要求的）。

    1. **名字不可预测**（``mkstemp``）：固定名等于告诉任何人「root 一会儿要执行
       这个路径上的内容」，提前在那里放一份就成了提权跳板；
    2. **``O_EXCL`` 建新文件**：不覆盖既有文件、**不跟随符号链接**（后者会把「写到
       哪」变成由文件名决定，写出去的内容落到别人指的地方）；
    3. **建时即受限**：``mkstemp`` 出生就是 0600，再由 :func:`os.chmod` 提到 0700
       —— 先 ``write_text``（默认 umask 权限）后 chmod 的那版中间有一段是宽权限。

    失败时脚本保留（``_cleanup_script`` 的口径）：名不可预测之后「下次覆盖同一个
    文件」这条路没了，但排查要的是**刚失败那一份的路径**，日志里已经带了。
    """
    root = paths.APP_SUPPORT
    root.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=name.lstrip(".") + ".", suffix=".sh",
                               dir=str(root))
    script = Path(raw)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.chmod(script, 0o700)
    return script


def _cleanup_script(path: Path, ok: bool) -> None:
    """成功删除脚本，失败保留供排查。"""
    if ok:
        try:
            path.unlink()
        except OSError:
            pass
    else:
        log.warning("授权脚本已保留供排查：%s", path)


def _legacy_cleanup_script() -> str:
    """旧 label / 旧形态残留的清理片段（安装与卸载的硬前置）。

    缺了它就会出现「换了 label 但旧 daemon 还在跑」：旧进程带着旧代码继续改系统
    网络状态，用户看到的行为与新版本不符，热修形同失效（方案 §7.4 风险登记）。
    """
    lines = []
    for label in LEGACY_PLIST_LABELS:
        lines.append(f'launchctl bootout "system/{label}" 2>/dev/null || true')
        lines.append(f'rm -f "{LEGACY_PLIST_DIR / (label + ".plist")}"')
    for path in LEGACY_HELPER_FILES:
        lines.append(f'rm -f "{path}"')
    return "\n".join(lines)


# ──────────────────────────────────────────────
# A 类二进制（§9.3：物理随插件、授权归框架）
# ──────────────────────────────────────────────

def deployed_root_binaries() -> frozenset:
    """系统域现存、登记在册的 A 类文件名（对账 deployed 侧；非 A 类文件不参与）。"""
    return deployed(SYS_HELPER_DIR)


def _binary_deploy_script(sources: "dict[str, Path]") -> str:
    """A 类二进制的部署片段（幂等 install + owner/权限 + 解 quarantine + ad-hoc 签名）。

    ⚠️ 关键坑（2026-09-02 实测）：**绝不能**给 hev 注入 `com.apple.vm.networking`
    等「受限授权」——受限授权必须经 provisioning profile 背书，否则 AMFI 会在
    hev 启动瞬间 SIGKILL（现象：hev 进程瞬间消失、hev.log 为空、tunnel_state
    报 rc=-9）。daemon 以 root 运行 hev，而 root 建 utun 按 macOS 规则不需要该
    授权，故只做 adhoc 签名（不带任何 entitlements）。签名失败**即安装失败**。
    """
    lines: list[str] = []
    for name in sorted(sources):
        dst = SYS_HELPER_DIR / name
        lines.append(f'install -m 0755 "{sources[name]}" "{dst}"')
        lines.append(f'chown root:wheel "{dst}"')
        lines.append(f'xattr -d com.apple.quarantine "{dst}" 2>/dev/null || true')
        lines.append(f'/usr/bin/codesign -s - --force "{dst}"')
    return "\n".join(lines)


# 完整性基线 manifest：安装期由 root 写入（root:wheel 0600）。
# helper 是**整目录 .py 包**（不签名、哈希可复现）→ 安装期算聚合哈希作运行期基线；
# hev 是 ad-hoc 签名的 Mach-O（签名改写字节、哈希不可复现）→ 其发行态基线只用于
# **安装期**校验源文件，运行期守卫走 `codesign --verify` + owner/权限（§9.3 写死）。
# 聚合算法与 helper 侧自检共用同一实现（`rootbin.hash_tree`），故必然一致。
_MANIFEST_SNIPPET = """# ── 完整性基线 manifest（root:wheel 0600）──
export PYTHONPATH="__SYS_DIR__"
/usr/bin/python3 - <<'MANIFEST'
from helper import rootbin

extra = {"helper": rootbin.hash_tree(rootbin.SYS_DIR / "helper")}
if extra["helper"] is None:
    raise SystemExit("helper 包聚合哈希计算失败（部署不完整）→ 安装中止")
rootbin.write_manifest(extra=extra)
print("manifest written:", rootbin.MANIFEST_PATH)
MANIFEST
"""


def reconcile_root_binaries(wanted) -> "tuple[bool, str]":
    """按无状态对账回收「已部署但无人需要」的 A 类二进制（§9.1 第 4b 步）。

    **仅当差集非空才弹授权** —— 差集为空是 no-op，这正是「禁用/启用不触发对账」
    的实现基础（避免高频可逆操作反复弹窗）。返回 ``(是否成功, 说明)``。
    """
    _, to_reap = reconcile(set(wanted or ()), deployed_root_binaries())
    if not to_reap:
        return True, "无 A 类资产需要回收"
    paths = [SYS_HELPER_DIR / name for name in sorted(to_reap)]
    script = "#!/bin/bash\nset -e\n" + "\n".join(f'rm -f "{p}"' for p in paths) + "\n"
    path = _write_script(".barkit-reap-rootbin.sh", script)
    ok = run_admin(str(path))
    _cleanup_script(path, ok)
    if ok:
        log.info("A 类资产已回收：%s", ", ".join(sorted(to_reap)))
        _invalidate_status_cache()
        return True, "已回收：" + ", ".join(sorted(to_reap))
    return False, "A 类回收需要系统授权，流程未完成"


# ──────────────────────────────────────────────
# 安装
# ──────────────────────────────────────────────

def helper_deploy_files() -> "list[str]":
    """要部署到系统域的 helper 源文件清单（相对 ``HELPER_SRC_DIR`` 的 POSIX 路径）。

    **为什么清单算在 Python 侧、而不是让 shell 通配**：helper 包自 2026-10-02 起有了
    子包（``routeops/``、``tunnelops/``）。此前安装脚本写的是
    ``for _f in "$src"/*.py``，只匹配顶层 ⇒ 装出来的目录**整个子包都不在**，
    而 ``lifecycle`` 启动即 ``from .tunnelops import …`` ⇒ 守护一起就 ImportError
    退出，launchd 按 KeepAlive 无限重启，路由自动化全停。

    这个错**不会在任何门禁里显形**：脚本里照样有 ``install -m 0644``（原用例只断言
    这串字面量，是代理判据）；安装期写的聚合哈希取自**装完的目录**
    （``rootbin.hash_tree(SYS_DIR / 'helper')``）⇒ 缺文件也自洽。

    递归收集全部 ``.py``，排除 ``__pycache__``（运行期产物，不进基线）。
    """
    root = HELPER_SRC_DIR.resolve()
    out: "list[str]" = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if "__pycache__" in rel.parts:
            continue
        out.append(rel.as_posix())
    return out


def _helper_deploy_script() -> str:
    """生成 helper 包的部署行（显式清单，逐条 ``mkdir -p`` ＋ ``install``）。

    清单来自 ``helper_deploy_files()``，不再用 shell 通配 —— 见那个函数里为什么。
    子目录只在切换时建一次（清单有序，同目录的文件相邻）。
    """
    src_dir = HELPER_SRC_DIR.resolve()
    lines: "list[str]" = []
    last_parent: "Path | None" = None
    for rel in helper_deploy_files():
        dest = HELPER_INSTALL_DIR / rel
        if dest.parent != last_parent:
            lines.append(f'mkdir -p "{dest.parent}"')
            last_parent = dest.parent
        lines.append(f'install -m 0644 "{src_dir}/{rel}" "{dest}"')
    return "\n".join(lines)


def install(sources: "dict[str, Path] | None" = None,
            missing: "dict[str, str] | None" = None) -> bool:
    """一次性安装 / 重装（弹系统授权）。**幂等的对账安装器**（§9.3）。

    ``sources`` = 要部署的 A 类二进制 ``{name: 仓库内源路径}``（框架按插件声明解析，
    见 ``Registry.root_binary_sources``）；``missing`` = 声明了却解析不到源的名字。

    失败语义（**不降级**，§9.3 点名「缺失不阻断安装」正是 P0-1 用户域 fallback 的成因）：
    声明了却拿不到源、或源与发行态基线哈希不符 → **拒绝安装**并给出明确原因。

    批次 4 的形态变更（label 切换 + 整目录部署 + 旧 label 清理 + 基线 manifest）
    **必须打包在同一次重装里**，避免用户被弹两次授权（方案 §12 Q5）。
    """
    sources = {str(k): Path(v) for k, v in dict(sources or {}).items()}
    missing = dict(missing or {})

    # ── 0. 本地预检（不弹授权）：先拦掉，避免用户白弹一次窗口 ──
    if not HELPER_SRC_DIR.is_dir():
        # 源码路径推导错了就别弹授权：用户白输一次密码，还看不出为什么失败
        log.error("helper 源码目录不存在，拒绝安装：%s", HELPER_SRC_DIR)
        return False
    if missing:
        for name, reason in sorted(missing.items()):
            log.error("A 类二进制 %s 无法部署：%s", name, reason)
        return False
    for name, src in sorted(sources.items()):
        ok, reason = rootbin.verify_file(name, src)
        if not ok:
            log.error("A 类二进制 %s 未通过发行态基线校验，拒绝安装：%s", name, reason)
            return False

    # ── 1. 对账：wanted = 要部署的全部；回收 deployed − wanted ──
    wanted = set(sources)
    _, to_reap = reconcile(wanted, deployed_root_binaries())
    reap_lines = "\n".join(
        f'rm -f "{SYS_HELPER_DIR / name}"' for name in sorted(to_reap)
    )

    deploy_lines = _binary_deploy_script(sources)
    manifest_block = _MANIFEST_SNIPPET.replace("__SYS_DIR__", str(SYS_HELPER_DIR))

    helper_deploy_lines = _helper_deploy_script()
    data_dir_val = str(paths.APP_SUPPORT)
    # 安装**此刻**就知道被服务的是谁（本进程归他所有），而守护开机先起时推不出。
    uid = os.getuid()
    plist = _PLIST_TEMPLATE.format(
        label=PLIST_LABEL,
        launcher=str(LAUNCHER_INSTALL_PATH),
        data_dir=data_dir_val,
        uid=uid,
        pythonpath=str(SYS_HELPER_DIR),
    )
    launcher = launcher_content()
    script = f"""#!/bin/bash
set -e
mkdir -p "{SYS_HELPER_DIR}"
# ── 用户域数据目录：建出来 + 归位给那个用户（幂等，重装即自愈）──
# 守护开机先起时若目录还不存在，会以 root 建出一个属主是 root 的目录，app 从此
# 写不进配置、也连不上会话，且自己改不回来（非 root 无权 chown）。这里以 root
# 归位一次，顺手把这种已经锁死的目录修好 —— 重装是用户本来就有的动作。
mkdir -p "{data_dir_val}"
chown {uid} "{data_dir_val}"
chmod 0700 "{data_dir_val}"
# ── 旧 label / 旧形态残留清理（硬前置）──
{_legacy_cleanup_script()}
# ── 整目录部署 helper 包（显式清单，保留子包结构；只拷 .py，不带 __pycache__）──
rm -rf "{HELPER_INSTALL_DIR}"
mkdir -p "{HELPER_INSTALL_DIR}"
{helper_deploy_lines}
chown -R root:wheel "{HELPER_INSTALL_DIR}"
chmod 0755 "{SYS_HELPER_DIR}"
chown root:wheel "{SYS_HELPER_DIR}"
# ── A 类 root 二进制：按声明部署（物理随插件、授权归框架，§9.3）──
{deploy_lines}
# ── A 类回收：deployed − wanted（无插件再声明者）──
{reap_lines}
# ── 启动器：让系统设置/活动监视器显示为 barkit-helper 而非 python ──
cat > "{LAUNCHER_INSTALL_PATH}" <<'LAUNCHER'
{launcher}
LAUNCHER
chmod 0755 "{LAUNCHER_INSTALL_PATH}"
chown root:wheel "{LAUNCHER_INSTALL_PATH}"
{manifest_block}cat > "{PLIST_PATH}" <<'PLIST'
{plist}
PLIST
chown root:wheel "{PLIST_PATH}"
chmod 0644 "{PLIST_PATH}"
# 幂等：先清后装（升级/重装场景）
launchctl bootout system/{PLIST_LABEL} 2>/dev/null || true
# bootout 是异步的：launchd 尚未释放完旧 label 时立刻 bootstrap 同一 label，
# 会偶发 "Bootstrap failed: 5: Input/output error"。等一拍再重试数次。
sleep 1
_bootstrap_ok=0
for _i in 1 2 3 4 5; do
  if launchctl bootstrap system "{PLIST_PATH}"; then
    _bootstrap_ok=1
    break
  fi
  sleep 1
done
if [ "$_bootstrap_ok" -ne 1 ]; then
  echo "launchctl bootstrap system \\"{PLIST_PATH}\\" 连续 5 次失败" >&2
  exit 1
fi
launchctl kickstart system/{PLIST_LABEL} 2>/dev/null || true
"""
    path = _write_script(".barkit-install-helper.sh", script)
    ok = run_admin(str(path))
    _cleanup_script(path, ok)
    if ok:
        log.info("helper 安装成功：%s v%s（A 类 %s；回收 %s）",
                 HELPER_INSTALL_DIR, expected_version(),
                 sorted(sources) or "无", sorted(to_reap) or "无")
        _invalidate_status_cache()
    return ok


# ──────────────────────────────────────────────
# 卸载
# ──────────────────────────────────────────────

def uninstall() -> bool:
    """卸载守护（**框架级整体拆除**，§9.4）：bootout（新 + 旧 label）→ 删 plist / 启动器 /

    helper 目录 / 完整性基线 / 全部 A 类二进制。

    ⚠️ **调用前提**（切分点 2 于 2026-09-17 落地后新增）：本函数**只拆框架自己的东西**，
    不再替任何插件撤系统状态。故调用方必须先确保「没有插件还在声明 helper task」——
    静态路由之类由插件在 ``teardown()`` 里撤、并由框架确认撤净之后才轮到这一步
    （见 ``core/deferred_cleanup`` 与 ``PluginResources.deferred``）。守护一被 bootout
    就再没人撤得掉那些状态：它的退出钩子只复原**隧道态**（``lifecycle._on_exit`` →
    ``tunnelops._teardown_tunnel``），完全不碰 route_guard 的静态路由。

    历史：本函数曾内嵌「读 ``routes.json`` → 逐条 ``/sbin/route delete``」，那是
    route_guard 的领域知识（框架不该认识某插件的期望态文件名），且它读的正是插件卸载时
    会删掉的那份文件 ⇒ 在最常见的卸载顺序下静默无效。已按 §8.1 切分点 2 移除。
    """
    binaries = " ".join(f'"{SYS_HELPER_DIR / name}"' for name in sorted(rootbin.known_names()))
    script = f"""#!/bin/bash
set -e
launchctl bootout "system/{PLIST_LABEL}" 2>/dev/null || true
{_legacy_cleanup_script()}
rm -f "{PLIST_PATH}" "{LAUNCHER_INSTALL_PATH}" "{MANIFEST_PATH}" {binaries}
rm -rf "{HELPER_INSTALL_DIR}"
"""
    path = _write_script(".barkit-uninstall-helper.sh", script)
    ok = run_admin(str(path))
    _cleanup_script(path, ok)
    if ok:
        log.info("helper 已卸载")
        _invalidate_status_cache()
    return ok
