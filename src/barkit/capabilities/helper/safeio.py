"""safeio：root 侧唯一的安全落盘入口 ＋ 用户域/系统域的路径根（方案 §7.4）。

安全模型（务必遵守）
------------------
本模块以 root 身份运行。所有写入都是「root 写」：

- 落盘一律经 ``_open_nofollow()``（O_NOFOLLOW）。/tmp 与用户域 data_dir 都是
  非 root 可写目录，攻击者能预置符号链接（如指向 /etc/sudoers），root 直接
  ``open(path, "wb")`` 就会截断目标（CWE-59）。O_NOFOLLOW 让末级分量是符号
  链接时以 ELOOP 失败：宁可放弃写入，也绝不去写链接指向的目标。
- root 私有的运行数据（hev 日志 / hev 配置 / DNS 备份 / 劫持标记 / hosts 快照）
  一律放 ``SYS_RUN_DIR``（/Library/Application Support/BarKit，root:wheel 0755），
  不放 /tmp、也不放用户域。

路径分两类（易混，写死）
----------------------
- **用户域**（``data_dir()``，plist 注入 BARKIT_DATA_DIR）：会话节点（``helper.sock``）、
  观测快照（``helper_snapshot.json``）、helper 日志。
- **系统域**（``SYS_RUN_DIR``）：root 私有运行数据，普通用户可读不可写。

⚠️ 2026-09-29：**这里不再有「通道类文件」的路径函数**（``routes_file`` / ``state_file`` /
``trigger_file`` / ``tunnel_file`` / ``tunnel_state_file`` / ``hosts_rules_file`` /
``hosts_state_file`` 七个已删）。期望态与事实都走会话，root 侧不再读任何用户域文件 ——
留一个没人调用的路径函数，就是留一条「以后有人接着用它读盘」的路。
"""


from __future__ import annotations

import json
import os
import stat
import time
from datetime import datetime
from pathlib import Path


# ──────────────────────────────────────────────
# 路径（BARKIT_DATA_DIR 由 plist EnvironmentVariables 注入用户目录）
# ──────────────────────────────────────────────

def data_dir() -> Path:
    env = os.environ.get("BARKIT_DATA_DIR")
    if env:
        return Path(env)
    return Path.home() / "Library/Application Support/BarKit"


# ──────────────────────────────────────────────
# 日志（root 写用户域；**一个进程一份文件**，超限就地截尾）
# ──────────────────────────────────────────────

#: 截尾阈值。轮询任务每 30s 写一行，长期无人干预会无限增长（实测 2.11.2 前到 5.3MB）。
_HELPER_LOG_MAX_BYTES = 1 << 20          # 1 MiB


def _open_nofollow(path: Path, *, append: bool = False, mode: int = 0o644):
    """以 O_NOFOLLOW 打开/创建文件——root 侧所有落盘写入的唯一入口。

    /tmp 与用户域 data_dir 都是非 root 可写目录。root 直接 open(..., "wb") 会
    跟随预置的符号链接，从而截断/改写任意文件（CWE-59）。O_NOFOLLOW 让末级
    分量是符号链接时以 ELOOP 失败：宁可放弃这次写入，也绝不去写链接指向的
    目标。文件不存在时 O_CREAT|O_NOFOLLOW 创建的是 root 拥有的普通文件，安全。
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
    flags |= os.O_APPEND if append else os.O_TRUNC
    fd = os.open(str(path), flags, mode)
    return os.fdopen(fd, "a" if append else "w")


def _atomic_write_json(path: Path, payload) -> None:
    """原子写 JSON（tmp + os.replace），全程 O_NOFOLLOW。

    os.replace() 替换的是符号链接本身而非其目标，目标侧天然安全；真正的风险
    在写 tmp —— 用户可预置 tmp 为符号链接让 root 截断任意文件，故 tmp 也走
    _open_nofollow()。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with _open_nofollow(tmp, mode=0o644) as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=2))
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _atomic_write_text(path: Path, content: str, *, mode: int = 0o644) -> None:
    """原子写文本（tmp + fsync + os.replace），全程 O_NOFOLLOW。

    与 :func:`_atomic_write_json` 同一套纪律，供 ``/etc/hosts`` 这类**系统关键文件**
    落盘：断电 / 进程被杀的瞬间若正写到一半，留下半截或空文件会让全机域名解析瘫痪。
    ``fsync`` 保证 tmp 内容先落介质，再 ``os.replace`` 一次性换上（rename 是原子的）。
    原文件有合法 st_mode 时沿用其值，避免把 ``/etc/hosts`` 的权限位改写掉。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    prev_mode = None
    try:
        prev_mode = os.stat(str(path)).st_mode
    except OSError:
        prev_mode = None
    tmp = path.with_name(path.name + ".tmp")
    try:
        with _open_nofollow(tmp, mode=mode) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        if prev_mode is not None:
            try:
                os.chmod(str(tmp), stat.S_IMODE(prev_mode))
            except OSError:
                pass
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _rotate_log(path: Path) -> None:
    """helper.log 超限 ⇒ **就地截尾**（读尾部一半、原子换回），只留这一份。

    为什么不是 ``helper.log`` ＋ ``helper.log.1``：用户 2026-09-29 要求「日志同时
    存在的份数收窄」，而 Python 自带的轮转器只会做「主文件 ＋ N 份备份」。做法很小
    —— 写满上限时读尾部一半、原子换回，接着从末尾往下写。**代价说清楚**：恰好在上限
    那一刻出事，最旧的一半现场会丢掉（上限别压太小就是为了这个）。

    **换回是原子的**（2026-10-13 收紧，与应用侧 ``SingleFileHandler`` 同一套）：
    先整份写进同目录的 ``.tmp`` 再 ``os.replace``。旧写法就地 ``O_TRUNC`` 重写，
    会**先落到 0 字节**再写回 —— 那一刻断电 / 被 kill 丢掉的是**整份日志**，不是
    一半。helper 每次写日志都重开一次 fd，故不存在应用侧那条「流与路径不同源」。

    安全：只碰**普通文件**（``lstat`` 不是 S_IFREG 就直接返回，符号链接不在其列），
    两个落点（读原文件 / 写 tmp）都不跟随符号链接 —— tmp 同样走 ``_open_nofollow``，
    否则用户可预置 tmp 为符号链接让 root 截断任意文件（CWE-59）。换上后沿用原
    ``st_mode``，免得权限位被 umask 收窄到「用户自己读不了」。截尾失败不阻塞写日志
    （宁可日志难看，也不因它出错而丢日志）。
    """
    try:
        st = os.lstat(str(path))
    except OSError:
        return                                   # 不存在 / 不可见：无需截尾
    if not stat.S_ISREG(st.st_mode):
        return                                   # 符号链接等：一律不碰
    if st.st_size < _HELPER_LOG_MAX_BYTES:
        return
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(str(path), "rb") as f:
            f.seek(st.st_size // 2)
            tail = f.read()
        # 从下一个换行处切开，免得留下半行（截出来的是残句比截掉更难看）
        cut = tail.find(b"\n")
        if cut >= 0:
            tail = tail[cut + 1:]
        with _open_nofollow(tmp) as f:
            f.write(tail.decode("utf-8", "replace"))
        try:
            os.chmod(str(tmp), stat.S_IMODE(st.st_mode))
        except OSError:
            pass
        os.replace(str(tmp), str(path))           # rename 换目录项：一次到位，无 0 字节窗口
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


#: 同一 key 的默认节流窗口（秒）。轮询任务 5~30s 一轮，一句「上游未就绪」一天能
#: 写一万多行；节流后同 key 在窗口内只留首条，窗口过后补一句「已合并 N 次」——
#: 「这个问题一直在」与「持续了多久」两条信息都还在，刷屏没了。
THROTTLE_DEFAULT_S = 300.0

# key -> [窗口起点, 窗口内被丢掉的条数]
_LOG_THROTTLE: dict[str, list] = {}


def log(msg: str, *, key: str | None = None, every_s: float | None = None) -> None:
    """写一行 helper 日志（追加，带大小轮转）；失败静默——日志不该拖垮主循环。

    ``key`` 非空时按 key 节流：窗口内只写首条，期间重复的只累加；窗口过后那一条
    补一句「已合并 N 次」。**不带 key 的调用照旧每条都写**——启停、安装这类
    一次性的事不该被合并掉。

    ``every_s=None`` ⇒ 取 :data:`THROTTLE_DEFAULT_S`（**在调用期读**：默认参数
    按值绑定会让「改常量 / 测试里 monkeypatch」都失效，与 ``paths`` 那条教训同源）；
    传 0 或负数 = 不节流。
    """
    if key:
        window = THROTTLE_DEFAULT_S if every_s is None else every_s
        if window > 0:
            now = time.monotonic()
            entry = _LOG_THROTTLE.get(key)
            if entry is not None:
                started, dropped = entry
                if now - started <= window:
                    entry[1] = dropped + 1
                    return
                if dropped:
                    msg = f"{msg}（{window:.0f}s 内已合并 {dropped} 次）"
                entry[0] = now
                entry[1] = 0
            else:
                _LOG_THROTTLE[key] = [now, 0]
    try:
        path = data_dir() / "helper.log"
        _rotate_log(path)
        with _open_nofollow(path, append=True) as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass


# hev 二进制安装位（与 rootbin.ROOT_BINARIES[...].install_path 一致：系统域 /Library/...）。
# 注意：data_dir() 是用户域（plist 注入的 BARKIT_DATA_DIR），故不能只用 data_dir()
# 查找——否则 daemon 装到系统域、helper 在用户域找，永远找不到（见 #12:3x 修复）。
SYS_HEV_DIR = Path("/Library/Application Support/BarKit")


# root 私有运行数据目录：与安装位同目录，root:wheel 0755（用户可读不可写）。
# hev 日志 / hev 配置 / DNS 备份 / 劫持标记一律放这里——不放 /tmp（全局可写
# +sticky，可预置符号链接），也不放用户域 data_dir（root 写用户可写目录同理）。
# 放不下时（非 root 运行、目录不可写）相关功能降级并记日志，绝不退化到可写目录。
SYS_RUN_DIR = SYS_HEV_DIR


def helper_dir() -> Path:
    """helper 自身的部署目录（系统域整目录包）= 本文件所在目录。

    供启动自检用：``rootbin`` 比对「磁盘现状 vs 安装期基线」时需要它。
    """
    return Path(__file__).resolve().parent


def hev_binary() -> Path:
    """hev-socks5-tunnel 二进制位置：**只有系统域一处**，缺失即停机。

    曾在这里保留「用户域 data_dir() 回退」——那是本进程（root）执行一个**普通用户
    可写目录**里的 Mach-O，等于把本地提权路径开在明面上：任何本地进程替换该文件，
    下一轮隧道激活即以 root 身份执行它的代码。这既无签名校验也无所有权校验，
    是唯一一条可直接利用的本地 EoP。

    故回退整条删除：系统域不存在 → `run_tunnel_cycle()` 报「二进制未安装」并拆除
    隧道，**不做任何降级**（降级路径即提权路径）。合规安装由 `helper_client.install()`
    以 root 写入 `SYS_HEV_DIR`（root:wheel 0755），用户改不动。
    """
    return SYS_HEV_DIR / "hev-socks5-tunnel"


def hev_yaml_path() -> Path:
    """hev 配置文件：root 生成、root 消费，放系统域（原来在用户域，等于让普通
    用户能改写 root 即将加载的配置）。"""
    return SYS_RUN_DIR / "hev-tunnel.yml"


def _hev_log_path() -> Path:
    """hev 运行日志（stdout+stderr）落盘位置。

    曾是 /tmp/barkit-hev.log——/tmp 全局可写 + sticky，任何本地进程都能预置
    符号链接，而本进程是 root 且以 "wb" 打开（即截断），一次启动就能把链接
    指向的任意文件清空。改到 SYS_RUN_DIR 后目录本身 root:wheel，用户改不动；
    文件 0644，用户照样能读。
    """
    return SYS_RUN_DIR / "hev.log"


def _hev_log_tail(n: int = 2000) -> str:
    """读 hev 日志的尾部，供失败时写进隧道观测的 error 字段。

    **只读尾部**（2026-09-29）：旧实现是 ``read_bytes()`` 把**整个文件**读进内存再取
    最后 n 个字符 —— 而这条路径只在隧道**建不起来**时才走，那时日志往往正是最大的
    （一次会话能写到近 1 MB），一次失败路径上一次几 MB 的读。改成 ``seek`` 到
    ``size - n`` 再读，读的量与 n 同阶。
    """
    try:
        p = _hev_log_path()
        size = p.stat().st_size
    except OSError:
        return ""
    try:
        with open(str(p), "rb") as f:
            if size > n:
                f.seek(size - n)
            return f.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""


def dns_backup_path() -> Path:
    """原始 DNS 备份。root 写、root 读，且要在崩溃/重启后仍可读到，放系统域。"""
    return SYS_RUN_DIR / "tunnel_dns_backup.txt"


def hijack_marker_path() -> Path:
    """隧道劫持标记：记录「本进程当前劫持了哪些系统网络状态」，供崩溃后回收。"""
    return SYS_RUN_DIR / "tunnel_hijack.json"


# ──────────────────────────────────────────────
# hosts_guard 的两个系统文件路径（渲染逻辑见 core._apply_hosts）
# ──────────────────────────────────────────────

# 被托管的系统文件（root 改写，普通用户不可写）。
HOSTS_PATH = Path("/etc/hosts")


# 首次改动前的快照：root 私有，放 SYS_RUN_DIR 而非 /tmp。
HOSTS_BACKUP = SYS_RUN_DIR / "hosts.bak"
