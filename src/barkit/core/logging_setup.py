"""core/logging_setup：统一日志 + 全局异常兜底（方案 §9 定案 + F.1 / F.3）。

日志定案：落 ~/Library/Logs/barkit/barkit.log，**单文件 2 MB、就一份**（写满就地截尾，
不另开备份）。2026-09-29 收窄：从前是 5MB × 3 份，三个进程各留备份 ⇒ 最坏同时 6 个文件、
上限 ~17 MB；现在「每个进程恰好一份」，全局上限 ~5 MB（见
``docs/plans/2026-09-29-通信通道换成会话.md`` §11.7）。

级别两处可配，按「谁先知道」分工：

- 进程环境变量 ``BARKIT_LOG_LEVEL``（debug / info / warning / error）：临时排查用，
  适合从终端 ``BARKIT_LOG_LEVEL=debug uv run barkit`` 跑一次；
- 配置键 ``log.level``（见 ``config.default.yaml``）：常驻实例用 —— 开机自启的
  App 拿不到终端环境变量，只有落盘的配置才改得动它。

默认 ``info``：正常运行只记「发生了什么」，debug 只在要排查时打开。

F.3 全局异常兜底一并落在此处，异常必须可追溯：
- sys.excepthook：主线程未捕获异常 → critical 日志
- threading.excepthook：工作线程（含 QThreadPool worker）未捕获异常 → critical 日志
- qInstallMessageHandler：Qt 内部警告/崩溃消息 → 汇入 logging（否则脱离日志体系）

**标准输出接管**（2026-10-13 新增，见 :func:`capture_stdio`）：``print()`` 与第三方库
直写 ``sys.stderr`` 的内容过去只有一个去向 —— 终端。而本程序常态**不是**从终端启动的
（LaunchAgent / 访达），那一刻没人接管 stdout/stderr，这些内容当场消失。「有没有终端」
于是成了「看不看得到」的唯一前提。接管之后两种启动方式输出同一份内容：一条写原始终端
（**只在有人看的时候**，见 :class:`_StdioTee`），一条写日志文件。

控制台那一份的策略随之改口（见 :func:`wants_console`）：**stderr 是终端就写**，与级别
无关；不是终端时维持原样（只在 debug 档写）。改前是从终端跑也一个字节都不打印，除非
显式设 ``BARKIT_LOG_LEVEL``。

用法：app 入口 main() 里 setup_logging() 之后紧跟 capture_stdio()，替换原 logging.basicConfig。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QtMsgType, qInstallMessageHandler

from barkit import paths

log = logging.getLogger(__name__)

# 轮转定案（2026-09-29 改）：单文件 2 MB，**一份**（写满就地截尾）
MAX_BYTES = 2 * 1024 * 1024
LOG_FILENAME = "barkit.log"


class SingleFileHandler(logging.FileHandler):
    """单文件日志：写满上限就**就地截尾**（读尾部一半、原子换回），不另开备份。

    为什么不用 ``RotatingFileHandler``：它只会做「主文件 ＋ N 份备份」，而用户
    2026-09-29 要求把「日志同时存在的份数」收窄到**每个进程恰好一份**。（连
    ``backupCount=0`` 也不等价：``doRollover`` 在备份数为 0 时跳过整段重命名，只
    close 再以 append 重开 ⇒ 既不截断也不轮转，文件会一直长。）做法很小 ——
    写满上限时读尾部一半、原子换回，接着从末尾继续写。**代价说清楚**：恰好在上限那一刻
    出事，最旧的一半现场会丢掉（上限别压太小，就是为了这个）。

    四处细节是 2026-10-13 收紧的，逐条都有理由：

    - **换回是原子的**：先整份写进同目录的 ``.tmp`` 再 ``os.replace``。旧写法
      ``open(path, "wb")`` 会**先落到 0 字节**再写回 —— 那一刻断电 / 被 kill 丢的是
      **整份日志**，不是一半。``os.replace`` 之后必须重开流（见 :meth:`_reopen`）：
      rename 换的是目录项，老流仍指着已被 unlink 的 inode，不重开则后续写入进空气。
    - **大小读已开的流量**（``os.fstat``）：比按路径 ``os.path.getsize`` 便宜 2.4 倍
      （实测 +0.95 µs/条 对 +2.38），而且「量到的就是即将被截的那一份」，不再有
      「按路径 stat、写的是另一个对象」这条分裂。
    - **被外部删掉能自愈**：``fstat`` 顺手带出的 ``st_nlink == 0`` 说明文件已被
      unlink（用户手删，或将来「清空日志」按 unlink 实现）—— 那一刻重开路径把文件
      建回来。不这么做的话，后续写入会静默落进一个「盘上不存在」的文件里。
    - **失败出声一次**：截尾要是永久失效（权限 / 磁盘满），旧写法 ``except OSError:
      pass`` 会让上限**静默**失效 —— 日志一直长而无人知晓。改为往原始 stderr 报一条，
      只报一次；写原始流不经 logging，所以没有自环。**截尾失败仍不阻塞写日志**：
      宁可日志难看，也不因清理动作出错而把这条日志丢掉。
    """

    def __init__(self, filename, max_bytes: int = MAX_BYTES, encoding: str = "utf-8"):
        super().__init__(filename, mode="a", encoding=encoding)
        self._max_bytes = max(1, int(max_bytes))
        self._truncate_failed = False          # 「失败只报一次」的闸

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._maintain()
        except OSError as exc:
            self._report_truncate_failure(exc)
        super().emit(record)

    def _maintain(self) -> None:
        """写之前维护一次文件状态：被删了重建，超上限了原子截尾。"""
        if self.stream is None:                # FileHandler.emit 会补开，这里无事可做
            return
        st = os.fstat(self.stream.fileno())
        if st.st_nlink == 0:                   # 已被 unlink：重开路径，顺手把文件建回来
            self._reopen()
            self.stream = self._open()
            st = os.fstat(self.stream.fileno())
        if st.st_size < self._max_bytes:
            return
        self._truncate(st.st_size)

    def _truncate(self, size: int) -> None:
        self.stream.flush()                    # 别让缓冲区的旧内容落到截尾之后
        with open(self.baseFilename, "rb") as f:
            f.seek(size // 2)
            tail = f.read()
        # 从下一个换行处切开，免得留下半行（截出一句残句比截掉更难看）
        cut = tail.find(b"\n")
        if cut >= 0:
            tail = tail[cut + 1:]
        # tmp 放同目录 ⇒ 同文件系统，rename 才是原子的；带 pid 免得两个实例同名互踩
        tmp = f"{self.baseFilename}.{os.getpid()}.tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(tail)              # 先整份写好，再一次性换上
            self._reopen()
            os.replace(tmp, self.baseFilename)
        except OSError:
            self._unlink_quietly(tmp)
            raise
        self.stream = self._open()

    def _reopen(self) -> None:
        """松掉老流并置空。

        置空而不是留一个「已关闭」的对象：``FileHandler.emit`` 见 ``stream is None``
        会补开，留个关掉的对象只会让下一次写入抛 ``ValueError``。
        """
        stream, self.stream = self.stream, None
        if stream is not None:
            stream.close()

    def _unlink_quietly(self, path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    def _report_truncate_failure(self, exc: OSError) -> None:
        """截尾失败只报一次，直接写原始 stderr（不经 logging，故无自环）。"""
        if self._truncate_failed:
            return
        self._truncate_failed = True
        try:
            _original_stderr().write(
                f"BarKit 日志截尾失败（{type(exc).__name__}: {exc}）："
                f"{self.baseFilename} 已不受 {self._max_bytes} 字节上限约束\n"
            )
        except Exception:  # noqa: BLE001 —— 报不出来也不该让日志调用抛异常
            pass

# 级别开关：``BARKIT_LOG_LEVEL=debug|info|warning|error``（大小写不敏感）。
# 日志初始化早于配置加载（app.py 在 AppShell 装配之前就调 setup_logging），
# 故级别走进程环境变量而不是 config 键——放 config 就得把启动顺序重排一遍。
# 默认 info：正常运行只记「发生了什么」，debug 只在排查时按需打开。
LEVEL_ENV_VAR = "BARKIT_LOG_LEVEL"
DEFAULT_LEVEL = logging.INFO
CONSOLE_ENV_VAR = "BARKIT_LOG_CONSOLE"
#: 标准输出接管的开关（``0`` / ``false`` 关掉）。正常不必动它 —— 关掉等于退回
#: 「从终端启动才看得到 print」的旧形态。
STDIO_ENV_VAR = "BARKIT_STDIO_CAPTURE"
_LEVEL_NAMES = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}

# 同一条 Qt 消息的合并窗口：窗口内重复只累加，窗口过后补一条汇总。
# 布局 / QSS 类警告会在重绘循环里按帧复现，不去重能把日志刷满。
QT_MUTE_SECONDS = 60.0
_QT_MUTE_MAX_KEYS = 512
_qt_muted: dict[str, tuple[float, int]] = {}

_QT_LEVELS = {
    QtMsgType.QtDebugMsg: logging.DEBUG,
    QtMsgType.QtInfoMsg: logging.INFO,
    QtMsgType.QtWarningMsg: logging.WARNING,
    QtMsgType.QtCriticalMsg: logging.ERROR,
    QtMsgType.QtFatalMsg: logging.CRITICAL,
}

_configured = False
_last_dir: Path | None = None
_last_level: int | None = None
# 由 ``wants_console`` **自动决策**加上的 stderr handler（显式 ``console=`` 的不记）。
# 记着它才能切档时**精确降回去**：从 debug 切回 info 必须把它摘掉，
# 否则「设置窗里把级别调回去了、终端还在刷」会让用户以为没生效。
_auto_console: "logging.Handler | None" = None

# 接管前的原始流（``None`` ＝ 还没接管）。控制台 handler 与「有没有终端」都问它，
# 不能问 ``sys.stdout`` / ``sys.stderr`` —— 那两个在接管之后已经是 Tee。
_saved_stdout = None
_saved_stderr = None
#: 接管后的两个 Tee；``None`` ＝ 未接管（幂等靠它，别用布尔标志：还得拿它们还原）。
_captured: "tuple[_StdioTee, _StdioTee] | None" = None
#: 接管时给两路取的日志名。``%(name)s`` 会把它写进文件，故一眼看得出内容是
#: 「别人直写标准输出/错误」，不是我们自己的 ``log.info``。
_STDOUT_LOGGER = "barkit.stdout"
_STDERR_LOGGER = "barkit.stderr"


def resolve_level(raw: str | None = None, *, default: int = DEFAULT_LEVEL) -> int:
    """把级别名（或数字字符串）解析成 logging 级别；认不出就回退 ``default``。

    认不出的值**不抛异常**：日志是兜底设施，它自己不该成为启动失败的原因。
    """
    text = (raw or "").strip().lower()
    if not text:
        return default
    if text in _LEVEL_NAMES:
        return _LEVEL_NAMES[text]
    if text.isdigit():
        return int(text)
    return default


#: 环境变量里的三态开关：``True`` / ``False`` / ``None``（没写）。
#: 认不出的值按「没写」处理 —— 与 :func:`resolve_level` 同一条口径：日志设施自己
#: 不该成为启动失败的原因。
_TRUE_WORDS = ("1", "true", "yes", "on")
_FALSE_WORDS = ("0", "false", "no", "off")


def _env_flag(name: str) -> bool | None:
    raw = os.environ.get(name, "").strip().lower()
    if raw in _TRUE_WORDS:
        return True
    if raw in _FALSE_WORDS:
        return False
    return None


def wants_console(console: bool | None, level: int) -> bool:
    """console 未显式指定时的策略：**有终端就写，没终端只在排查档写**。

    两条判据分管两种启动方式，改前只有后一条 —— 于是「从终端跑」反而不出东西
    （菜单栏 App 常态静默的代价落到了终端用户头上，排查全靠 ``BARKIT_LOG_LEVEL``）：

    - ``stderr`` 是终端 ⇒ 有人在看，照级别全量写。级别由配置决定，不由「有没有终端」决定。
    - ``stderr`` 不是终端（LaunchAgent / 访达 / 被重定向）⇒ 写出去没人接，常态静默；
      只有排查档（debug）例外 —— 那时用户多半正把 stderr 重定向到文件读。

    强制开关 ``BARKIT_LOG_CONSOLE=1`` / ``=0`` 压过以上两条。
    """
    if console is not None:
        return console
    forced = _env_flag(CONSOLE_ENV_VAR)
    if forced is not None:
        return forced
    return _console_is_tty() or level <= logging.DEBUG


def _original_stderr():
    """控制台 handler 的落点：**始终是原始 stderr**。

    :func:`capture_stdio` 之后 ``sys.stderr`` 是 Tee，若 handler 写回它就成了自环
    （写日志 → 写 stderr → 又变一条日志）。故接管前把原始流存下来，这里优先取它。
    """
    return _saved_stderr if _saved_stderr is not None else sys.stderr


def _console_is_tty() -> bool:
    """原始 stderr 是不是终端。取不到（已被关掉 / 不是流对象）就算不是。"""
    try:
        return bool(_original_stderr().isatty())
    except Exception:  # noqa: BLE001 —— 判断不了就按「没人看」处理，不该冒泡
        return False


#: 一条日志记录的行格式。文件与控制台共用同一份 —— 「终端看到的」与「文件里的」
#: 逐字节相同是这两个落点唯一的纪律（见模块 docstring 的「标准输出接管」）。
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class _DropStdioEcho(logging.Filter):
    """控制台 handler 专用：丢掉「已经原样写到终端上」的那些记录。

    ``print()`` 走 :class:`_StdioTee` 时**自己**往原始终端写了一份，同时也变成一条
    日志记录；控制台 handler 若再写一遍，终端上同一行出现两次。带 ``from_stdio``
    标记的记录因此不进控制台。**文件 handler 不挂这个过滤器** —— 落盘要的就是全量。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(record, "from_stdio", False)


def _make_console_handler() -> logging.StreamHandler:
    """造一个控制台 handler（落点与格式都只在这里定一次）。"""
    handler = logging.StreamHandler(_original_stderr())
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(_DropStdioEcho())
    return handler


def set_level(level: int, *, console: bool | None = None) -> None:
    """配置键落定后改 root 级别（``log.level`` 晚于进程级准备，故分两步）。

    只调级别与可选的 console handler，不动文件 handler——轮转参数在一次进程内
    不该被重设。该不该有 stderr handler 由 :func:`wants_console` 判，
    **切档时两向都调**（从「该开」切到「不该开」也要摘掉，靠 :data:`_auto_console`
    精确追踪，免得摘掉别人显式加的）。
    """
    global _last_level, _auto_console
    root = logging.getLogger()
    root.setLevel(level)
    _last_level = level
    if wants_console(console, level):
        if _auto_console is None or _auto_console not in root.handlers:
            if _auto_console is not None and _auto_console in root.handlers:
                root.removeHandler(_auto_console)
            handler = _make_console_handler()
            root.addHandler(handler)
            _auto_console = handler
    else:
        if _auto_console is not None and _auto_console in root.handlers:
            root.removeHandler(_auto_console)
        _auto_console = None


def setup_logging(
    log_dir: Path | None = None,
    level: int | None = None,
    console: bool | None = None,
) -> None:
    """主进程日志：落 ``paths.LOG_DIR``/barkit.log（单文件 2 MB，写满就地截尾）+ 可选 console。

    - ``level``：None ⇒ 取环境变量 ``BARKIT_LOG_LEVEL``，再缺省 ``DEFAULT_LEVEL``。
      正常运行停在 info；想更安静设 ``warning``，想排查设 ``debug``。
    - ``console``：None ⇒ 按 :func:`wants_console` 判（有终端就写）。

    幂等：已按相同 (目录, 级别) 配置过则跳过。会重置 root logger 的旧 handler
    （app 场景替换 basicConfig；避免开发期日志双写）。

    ``log_dir=None`` ⇒ 调用期读 ``paths.LOG_DIR``（默认参数若按值绑定，
    测试的路径重定向会失效）。
    """
    global _configured, _last_dir, _last_level, _auto_console
    default_dir = paths.LOG_DIR
    log_dir = log_dir if log_dir is not None else default_dir

    raw_level = os.environ.get(LEVEL_ENV_VAR)
    if level is None:
        level = resolve_level(raw_level)
    if _configured and log_dir == _last_dir and level == _last_level:
        return
    _configured = True
    _last_dir = log_dir
    _last_level = level

    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for h in list(root.handlers):      # 清掉 basicConfig 等旧 handler
        root.removeHandler(h)
        h.close()
    root.setLevel(level)

    fmt = logging.Formatter(LOG_FORMAT)
    file_handler = SingleFileHandler(log_dir / LOG_FILENAME, max_bytes=MAX_BYTES)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    if wants_console(console, level):
        console_handler = _make_console_handler()
        root.addHandler(console_handler)
        if console is None:          # 自动决策加的：切档时才允许降回去
            _auto_console = console_handler

    install_global_hooks()
    level_name = logging.getLevelName(level)
    if raw_level and resolve_level(raw_level, default=-1) == -1:
        log.warning("%s=%r 无法识别，回退 %s", LEVEL_ENV_VAR, raw_level, level_name)
    log.info("日志已初始化：%s（级别 %s，单文件 %.0f MB，写满就地截尾）",
             log_dir / LOG_FILENAME, level_name, MAX_BYTES / 1024 / 1024)


# ──────────────────────────────────────────────
# 标准输出接管：print / 第三方 stderr → logging
# ──────────────────────────────────────────────

class _StdioTee:
    """``sys.stdout`` / ``sys.stderr`` 的替身：原文照旧送终端 ＋ 另抄一份进 logging。

    三条规矩，逐条都有理由：

    - **有人看才写原始流**。``isatty()`` 为假说明这份写出去没人接（LaunchAgent 的
      那两个 fd 由 launchd 处置，访达启动的则指向 /dev/null）—— 那时照旧写一份等于
      把同一条内容在 ``barkit.log`` 里放两遍，还会让 ``barkit-startup.log`` 无界增长。
    - **按行落库**。``print("a")`` 是两次 ``write``（``"a"`` 与 ``"\\n"``），按调用次数
      落库会把一条拆成两条；故攒到换行才 emit，``flush()`` 时把残行也吐掉。
    - **防自环**。logging 若因配置异常自己往 ``sys.stderr`` 写错误，而此刻
      ``sys.stderr`` 正是本对象 ⇒ 会无限递归。``_emitting`` 期间只剩「写原始流」。
    """

    def __init__(self, original, level: int, name: str) -> None:
        self._original = original            # 必须最先赋值：__getattr__ 要靠它兜底
        self._level = level
        self._name = name
        self._passthrough = _isatty_of(original)
        self._buf = ""
        self._lock = threading.Lock()
        self._emitting = False

    # ---- 流协议（print / traceback / 第三方库都只用这几个口）----

    def write(self, text) -> int:
        if not text:
            return 0
        if not isinstance(text, str):
            text = str(text)
        if self._emitting:                   # 自环保护：只剩原样转发，不再入日志
            self._write_original(text)
            return len(text)
        self._write_original(text)
        with self._lock:
            self._buf += text
            while "\n" in self._buf:
                line, _, self._buf = self._buf.partition("\n")
                self._emit(line)
        return len(text)

    def flush(self) -> None:
        try:
            self._original.flush()
        except Exception:  # noqa: BLE001 —— 原始流已关：不该因此让 print 抛异常
            pass
        with self._lock:
            if self._buf:
                line, self._buf = self._buf, ""
                self._emit(line)

    def isatty(self) -> bool:
        return self._passthrough

    def writable(self) -> bool:
        return True

    def __getattr__(self, name: str):
        # 只兜底转发（encoding / fileno / errors / buffer …）。下划线名不转发：
        # ``__getattr__`` 是在属性缺失时才被调的，转发私有名会让拷贝/序列化递归。
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._original, name)

    # ---- 内部 ----

    def _write_original(self, text: str) -> None:
        if not self._passthrough:
            return
        try:
            self._original.write(text)
        except Exception:  # noqa: BLE001
            pass

    def _emit(self, line: str) -> None:
        text = line.strip()
        if not text or self._emitting:
            return
        self._emitting = True
        try:
            # extra 里的标记是「这行已经写过终端了」的唯一凭据（见 _DropStdioEcho）
            logging.getLogger(self._name).log(
                self._level, "%s", text, extra={"from_stdio": True}
            )
        except Exception:  # noqa: BLE001 —— 日志设施出错不该让 print 抛异常
            pass
        finally:
            self._emitting = False


def _isatty_of(stream) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:  # noqa: BLE001
        return False


def capture_stdio(enabled: bool | None = None) -> None:
    """把 ``sys.stdout`` / ``sys.stderr`` 接到 logging 上（幂等）。

    ``enabled=None`` ⇒ 取环境变量 ``BARKIT_STDIO_CAPTURE``（``0`` / ``false`` 关掉），
    再缺省为开。

    **为什么不放进 setup_logging**：它改的是解释器的全局流，测试里每调一次
    ``setup_logging`` 就被接管一次，pytest 自己的捕获会跟着错乱。故由 app 入口
    显式调，测试要就得显式调 :func:`restore_stdio` 收尾。

    ``print`` 的级别：**stdout → INFO，stderr → WARNING**。前者是「程序自己说的话」，
    后者是「绕开了 logging 的告警」—— 两者都不该冒充更高级别的故障，但也不该沉到
    debug 而在默认档看不到（那正是这次要修的病）。
    """
    global _captured, _saved_stdout, _saved_stderr
    if _captured is not None:
        return
    if enabled is None:
        enabled = _env_flag(STDIO_ENV_VAR) is not False
    if not enabled:
        return
    _saved_stdout, _saved_stderr = sys.stdout, sys.stderr
    stdout_tee = _StdioTee(_saved_stdout, logging.INFO, _STDOUT_LOGGER)
    stderr_tee = _StdioTee(_saved_stderr, logging.WARNING, _STDERR_LOGGER)
    sys.stdout, sys.stderr = stdout_tee, stderr_tee
    _captured = (stdout_tee, stderr_tee)
    log.info("标准输出已接管：print / 第三方 stderr 一并落日志（原始终端%s）",
             "有人在看，照旧显示" if _console_is_tty() else "没人看，只落文件")


def restore_stdio() -> None:
    """还原 ``sys.stdout`` / ``sys.stderr``（给测试收尾用；生产不调）。"""
    global _captured, _saved_stdout, _saved_stderr
    if _captured is None:
        return
    for tee in _captured:
        try:
            tee.flush()                  # 残行（没有结尾换行的 write）不能丢
        except Exception:  # noqa: BLE001
            pass
    if _saved_stdout is not None:
        sys.stdout = _saved_stdout
    if _saved_stderr is not None:
        sys.stderr = _saved_stderr
    _captured = None
    _saved_stdout = None
    _saved_stderr = None


# ──────────────────────────────────────────────
# 全局异常兜底（F.3）
# ──────────────────────────────────────────────

def _qt_mute(text: str) -> int | None:
    """Qt 消息节流：返回 ``None`` 表示本条应静默，否则返回被合并掉的重复次数。

    同一条消息在 ``QT_MUTE_SECONDS`` 内只落盘一次；窗口过后第一条补一句
    「重复 N 次」。目的是挡住「同一条布局 / QSS 警告按帧复现」这类刷屏，
    而不是减少信息——首次出现永远会落盘，所以定位不受影响。
    """
    now = time.monotonic()
    entry = _qt_muted.get(text)
    if entry is not None:
        at, count = entry
        if now - at <= QT_MUTE_SECONDS:
            _qt_muted[text] = (at, count + 1)
            return None
        repeats = count
    else:
        repeats = 0
        if len(_qt_muted) >= _QT_MUTE_MAX_KEYS:      # 文本各异的消息堆太多：整表丢弃
            _qt_muted.clear()
    _qt_muted[text] = (now, 0)
    return repeats


def handle_uncaught(exc_type, exc_value, exc_tb, source: str = "") -> None:
    """未捕获异常统一落日志（主线程/工作线程钩子共用入口）。"""
    logging.getLogger("barkit").critical(
        "未捕获异常（%s）：%s: %s", source or "主线程", exc_type.__name__, exc_value,
        exc_info=(exc_type, exc_value, exc_tb),
    )


def qt_message_handler(mode: QtMsgType, context, message: str) -> None:
    """Qt 消息 → logging（含重复合并）。

    提到模块级而不是留在 ``install_global_hooks`` 的闭包里：它是「哪些消息会进
    日志、进几次」的唯一裁决点，必须能被直接调用来测（装进 Qt 之后就取不回来
    了，``qInstallMessageHandler`` 没有 getter）。
    """
    level = _QT_LEVELS.get(mode, logging.INFO)
    where = ""
    if context is not None:
        file = getattr(context, "file", "") or ""
        line = getattr(context, "line", 0) or 0
        where = f"{file}:{line} " if file else ""
    text = f"{where}{message}"
    if level < logging.CRITICAL:
        repeats = _qt_mute(text)
        if repeats is None:
            return                      # 窗口内重复：只累加，不落盘
        if repeats:
            text = f"{text}（{QT_MUTE_SECONDS:.0f}s 内重复 {repeats} 次，已合并）"
    logging.getLogger("PySide6").log(level, "%s", text)


def install_global_hooks() -> None:
    """注册全局异常/线程异常/Qt 消息钩子。幂等：只装一次。"""
    if install_global_hooks._installed:
        return
    install_global_hooks._installed = True

    def _excepthook(exc_type, exc_value, exc_tb) -> None:
        handle_uncaught(exc_type, exc_value, exc_tb)

    def _thread_excepthook(args) -> None:
        thread_name = getattr(args.thread, "name", "?")
        handle_uncaught(
            args.exc_type,
            args.exc_value,
            args.exc_traceback,
            source=f"线程 {thread_name}",
        )

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    qInstallMessageHandler(qt_message_handler)
    log.debug("全局异常钩子已安装（主线程/工作线程/Qt 消息）")


install_global_hooks._installed = False