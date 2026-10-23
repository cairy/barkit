"""F.1/F.3 测试：统一日志落盘 + 单文件截尾 + 全局异常钩子 + 标准输出接管。"""

import io
import logging
import logging.handlers
import os
import sys
import threading

import pytest

from barkit.core import logging_setup
from barkit.core.logging_setup import (
    LOG_FILENAME,
    MAX_BYTES,
    SingleFileHandler,
    install_global_hooks,
    setup_logging,
)
from PySide6.QtCore import QtMsgType


@pytest.fixture
def logdir(tmp_path):
    """每次用独立日志目录重新配置，恢复 root handler。"""
    yield tmp_path
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()


@pytest.fixture(autouse=True)
def _clean_state():
    """级别开关依赖环境变量与模块级去重表，用例之间必须归零。"""
    logging_setup._configured = False
    logging_setup._last_dir = None
    logging_setup._last_level = None
    logging_setup._auto_console = None
    logging_setup._qt_muted.clear()
    # 标准输出接管也是进程级状态：上一个用例若没收尾，本用例的 capture_stdio 会静默
    # 变成空操作（幂等判据是「已经接管过」），症状是「接管看起来没生效」。
    logging_setup._captured = None
    logging_setup._saved_stdout = None
    logging_setup._saved_stderr = None
    yield
    logging_setup._qt_muted.clear()


def _flush():
    for h in logging.getLogger().handlers:
        h.flush()


def _console_handlers(handlers, before=()):
    """挑出「写到 stderr/stdout 的那几个」handler（可只看本轮新增的）。

    ``RotatingFileHandler`` 也是 ``StreamHandler`` 的子类（经 ``FileHandler``），
    按类型判会把文件 handler 一并算进来 —— 故这里显式排除文件类。
    """
    return [
        h for h in handlers
        if h not in before
        and isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
    ]


def _reset_configured():
    logging_setup._configured = False


def test_set_level_adds_and_removes_the_auto_console(logdir, monkeypatch):
    """升到 debug ⇒ 自动补一个控制台 handler；降回 info ⇒ 精准摘掉。

    只加不减会让「设置窗里把级别调回去了、终端还在刷」被当成没生效（2026-09-28
    把级别做成设置窗控件时暴露：老 ``set_level`` 从不移除自动加的 stderr handler）。
    """
    monkeypatch.delenv("BARKIT_LOG_CONSOLE", raising=False)
    setup_logging(log_dir=logdir, console=False)         # 默认 INFO，无控制台
    assert _console_handlers(logging.getLogger().handlers) == []

    logging_setup.set_level(logging.DEBUG)
    cons = _console_handlers(logging.getLogger().handlers)
    assert len(cons) == 1
    assert logging_setup._auto_console is cons[0]

    logging_setup.set_level(logging.INFO)
    assert _console_handlers(logging.getLogger().handlers) == []
    assert logging_setup._auto_console is None

def test_setup_single_file_handler(logdir):
    """每个进程**恰好一份**日志：不是「主文件 + N 份备份」（2026-09-29 收窄）。"""
    _reset_configured()
    setup_logging(log_dir=logdir, console=False)
    fh = next(h for h in logging.getLogger().handlers
              if isinstance(h, SingleFileHandler))
    assert fh._max_bytes == MAX_BYTES == 2 * 1024 * 1024
    assert not any(isinstance(h, logging.handlers.RotatingFileHandler)
                   for h in logging.getLogger().handlers), "轮转备份形态已下线"


def _oversize(logger, path, cap: int) -> None:
    """把日志写到**已达上限**（下一条 emit 必然触发截尾）。

    截尾判据是「写之前 size 已 >= 上限」，所以不能靠「多写几条」碰运气 —— 补一条
    刚好把文件顶过上限的记录，测试结果才是确定的。
    """
    for i in range(400):                       # 每行约 40 字节 ⇒ 远超 4096
        logger.info("填充行 %03d %s", i, "x" * 30)
    _flush()
    size = path.stat().st_size
    if size < cap:
        logger.info("补到上限 %s", "x" * (cap - size + 10))
        _flush()


def test_log_truncates_and_leaves_one_file(logdir, monkeypatch):
    """写满上限 ⇒ **截尾**：只留 barkit.log 一份，且最新的内容还在。

    判据取两条：① 目录里**只有** ``barkit.log``（没有 ``.1`` 备份）；② 截尾之后
    最后写进去那行仍读得到（「读尾部一半」不是「清空」）。换尾是否原子、流有没有
    跟着走，由下面两条用例分治 —— 那两条此前没有，正是这次漏掉的面。
    """
    _reset_configured()
    monkeypatch.setattr(logging_setup, "MAX_BYTES", 4096)
    setup_logging(log_dir=logdir, console=False)
    logger = logging.getLogger("barkit.test")
    _oversize(logger, logdir / LOG_FILENAME, 4096)
    logger.info("最后一行标记")
    _flush()

    files = sorted(p.name for p in logdir.glob("*.log"))
    assert files == [LOG_FILENAME], f"只该有一份日志，实际 {files}"
    text = (logdir / LOG_FILENAME).read_text(encoding="utf-8")
    assert "最后一行标记" in text, "截尾是读尾部一半，不是清空"
    assert len(text.encode("utf-8")) <= 4096 + 512, "截尾之后不该仍在长"


def test_truncation_swaps_in_a_new_file_and_the_stream_follows_it(logdir, monkeypatch):
    """截尾是**原子换尾**（同目录 rename），不是就地清 0 重写。

    两条判据各治一种坏法：① 截尾前后**路径上的 inode 变了** —— 旧的
    ``open(path, "wb")`` 是就地重写，inode 不变，而那一刻磁盘上是 0 字节
    （断电 / 被 kill 丢的是**整份**日志，不是一半）；② 截尾后 ``stream`` 与路径
    **指向同一个 inode** —— 换上之后忘了重开流的话，后续写入会落进已被 unlink 的
    旧 inode，盘上一个字都看不到。
    """
    _reset_configured()
    monkeypatch.setattr(logging_setup, "MAX_BYTES", 4096)
    setup_logging(log_dir=logdir, console=False)
    handler = next(h for h in logging.getLogger().handlers
                   if isinstance(h, SingleFileHandler))
    logger = logging.getLogger("barkit.test.swap")
    path = logdir / LOG_FILENAME
    _oversize(logger, path, 4096)

    before = path.stat().st_ino
    logger.info("越限这一条")
    _flush()

    after = path.stat().st_ino
    assert after != before, "截尾应当在同目录原子换上（inode 换新），不是就地清 0 重写"
    assert os.fstat(handler.stream.fileno()).st_ino == after, \
        "换尾之后必须重开流：不重开则写入落进已被 unlink 的旧 inode"
    assert "越限这一条" in path.read_text(encoding="utf-8")
    assert not list(logdir.glob("*.tmp")), "换尾用的 tmp 必须清干净"


def test_log_recreates_the_file_when_it_is_removed_underneath(logdir):
    """文件被外部删掉（或将来「清空日志」按 unlink 实现）⇒ 下一次写要把它建回来。

    不这么做的话，流仍握着已 unlink 的 inode：路径不存在，写入静默落进「盘上没有的
    文件」，界面上什么都看不到。自愈的凭据是 ``fstat`` 顺手带出的 ``st_nlink == 0``
    —— 不额外付一次按路径的 stat。
    """
    _reset_configured()
    setup_logging(log_dir=logdir, console=False)
    logger = logging.getLogger("barkit.test.recreate")
    path = logdir / LOG_FILENAME
    logger.info("删之前这一条")
    _flush()
    assert path.exists()

    os.unlink(path)
    assert not path.exists()                   # 前提坐实：文件真的没了
    logger.info("删之后这一条")
    _flush()

    assert path.exists(), "被外部删掉后必须自愈重建，不能静默写进已 unlink 的 inode"
    assert "删之后这一条" in path.read_text(encoding="utf-8")


def test_truncation_failure_is_reported_once_and_never_blocks_logging(logdir, monkeypatch):
    """截尾失败要**出声**（旧的 ``except OSError: pass`` 让上限静默失效），且只报一次。

    出声走原始 stderr，不经 logging ⇒ 没有自环。同时**不阻塞写日志**：清理动作失败
    不该让这条日志跟着丢掉（上限失效了，但那几行现场还在）。
    """
    _reset_configured()
    monkeypatch.setattr(logging_setup, "MAX_BYTES", 4096)
    setup_logging(log_dir=logdir, console=False)
    path = logdir / LOG_FILENAME
    said = _FakeStream(False)
    monkeypatch.setattr(logging_setup, "_original_stderr", lambda: said)
    real_replace = os.replace

    def _no_space(src, dst, *args, **kwargs):
        if str(dst) == str(path):
            raise OSError(28, "No space left on device")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(logging_setup.os, "replace", _no_space)
    logger = logging.getLogger("barkit.test.boom")
    _oversize(logger, path, 4096)

    assert said.getvalue().count("截尾失败") == 1, f"该只报一次，实际：{said.getvalue()!r}"
    assert "上限" in said.getvalue(), "出声要说清后果：上限已经失效"
    assert "填充行 399" in path.read_text(encoding="utf-8"), "截尾失败不该阻塞写日志"


def test_log_lands_in_file(logdir):
    _reset_configured()
    setup_logging(log_dir=logdir, console=False)
    logging.getLogger("barkit.test").info("hello 日志")
    for h in logging.getLogger().handlers:
        h.flush()
    assert (logdir / LOG_FILENAME).exists()
    assert "hello 日志" in (logdir / LOG_FILENAME).read_text(encoding="utf-8")


def test_excepthook_records_crash(logdir):
    _reset_configured()
    setup_logging(log_dir=logdir, console=False)
    # 真实触发路径是 sys.excepthook（解释器退出时）；此处直接调同一入口
    # handle_uncaught 验证落日志，避免 pytest-qt 对未捕获异常的干扰捕获
    try:
        raise ValueError("boom")
    except ValueError:
        exc_info = sys.exc_info()
    logging_setup.handle_uncaught(*exc_info)
    for h in logging.getLogger().handlers:
        h.flush()
    content = (logdir / LOG_FILENAME).read_text(encoding="utf-8")
    assert "CRITICAL" in content and "ValueError" in content and "boom" in content


def test_sys_excepthook_is_installed(logdir):
    _reset_configured()
    setup_logging(log_dir=logdir, console=False)
    assert logging_setup.install_global_hooks._installed is True
    # 全局钩子已替换默认实现（真实未捕获异常会进日志而非 stderr）
    assert sys.excepthook is not sys.__excepthook__
    assert threading.excepthook is not threading.__excepthook__


def test_thread_excepthook_records_crash(logdir):
    _reset_configured()
    setup_logging(log_dir=logdir, console=False)
    try:
        raise RuntimeError("thread boom")
    except RuntimeError:
        exc_info = sys.exc_info()
    t = threading.Thread(target=lambda: None, name="worker")
    args = threading.ExceptHookArgs([exc_info[0], exc_info[1], exc_info[2], t])
    threading.excepthook(args)
    for h in logging.getLogger().handlers:
        h.flush()
    content = (logdir / LOG_FILENAME).read_text(encoding="utf-8")
    assert "thread boom" in content and "线程 worker" in content


def test_install_global_hooks_idempotent(logdir):
    _reset_configured()
    setup_logging(log_dir=logdir, console=False)
    old_handler = sys.excepthook
    install_global_hooks()
    install_global_hooks()   # 幂等：只装一次
    assert sys.excepthook is old_handler


def test_qt_message_forwarded_to_logging(logdir):
    _reset_configured()
    setup_logging(log_dir=logdir, console=False)
    logging.getLogger("PySide6").warning("qt warning forwarded")
    for h in logging.getLogger().handlers:
        h.flush()
    content = (logdir / LOG_FILENAME).read_text(encoding="utf-8")
    assert "qt warning forwarded" in content


# ──────────────────────────────────────────────
# 级别可配（BARKIT_LOG_LEVEL / config.log.level）
# ──────────────────────────────────────────────

def test_resolve_level_names_and_digits():
    assert logging_setup.resolve_level("debug") == logging.DEBUG
    assert logging_setup.resolve_level("WARNING") == logging.WARNING   # 大小写不敏感
    assert logging_setup.resolve_level("warn") == logging.WARNING
    assert logging_setup.resolve_level("30") == 30                     # 数字字符串
    assert logging_setup.resolve_level("") == logging.INFO             # 未配置 ⇒ 出厂档
    assert logging_setup.resolve_level(None) == logging.INFO
    # 认不出的值不抛异常：日志是兜底设施，它自己不该成为启动失败的原因
    assert logging_setup.resolve_level("verbose") == logging.INFO


def test_env_var_sets_root_level(logdir, monkeypatch):
    monkeypatch.setenv("BARKIT_LOG_LEVEL", "warning")
    setup_logging(log_dir=logdir, console=False)
    assert logging.getLogger().level == logging.WARNING
    logging.getLogger("barkit.test").info("不该落盘")
    _flush()
    assert "不该落盘" not in (logdir / LOG_FILENAME).read_text(encoding="utf-8")


def test_unparsable_env_var_falls_back_and_says_so(logdir, monkeypatch):
    monkeypatch.setenv("BARKIT_LOG_LEVEL", "verbose")
    setup_logging(log_dir=logdir, console=False)
    assert logging.getLogger().level == logging.INFO          # 回退出厂档
    _flush()
    content = (logdir / LOG_FILENAME).read_text(encoding="utf-8")
    assert "BARKIT_LOG_LEVEL" in content and "verbose" in content   # 且说清是它没认出来


def test_console_only_on_debug_by_default(logdir):
    """常态不写 stderr（菜单栏 App 的 stderr 没人看），排查档自动开。

    判「新增了什么」而不是「root 上有没有 StreamHandler」—— pytest 的日志插件
    自己会往 root 挂 handler，按类型判会一直绿不了。
    """
    root = logging.getLogger()
    before = list(root.handlers)
    setup_logging(log_dir=logdir, level=logging.INFO)
    assert not _console_handlers(root.handlers, before)

    logging_setup._configured = False
    setup_logging(log_dir=logdir, level=logging.DEBUG)
    assert _console_handlers(root.handlers, before)


def test_set_level_applies_configured_level(logdir):
    """配置键晚于进程级准备落定，故单独一个入口改级别。"""
    setup_logging(log_dir=logdir, level=logging.INFO, console=False)
    logging_setup.set_level(logging.WARNING)
    assert logging.getLogger().level == logging.WARNING
    logging.getLogger("barkit.test").info("不该落盘")
    _flush()
    assert "不该落盘" not in (logdir / LOG_FILENAME).read_text(encoding="utf-8")
    # 调试档还会补一个 stderr handler（与 setup 的自动策略同一判据）
    logging_setup.set_level(logging.DEBUG)
    assert any(isinstance(h, logging.StreamHandler) for h in logging.getLogger().handlers)


# ──────────────────────────────────────────────
# Qt 消息合并（同一条按帧复现时不刷盘）
# ──────────────────────────────────────────────

def test_qt_repeats_collapse_into_one_line(logdir):
    setup_logging(log_dir=logdir, console=False)
    warn = logging_setup.qt_message_handler
    for _ in range(5):
        warn(QtMsgType.QtWarningMsg, None, "布局警告：同一个问题")
    _flush()
    content = (logdir / LOG_FILENAME).read_text(encoding="utf-8")
    assert content.count("布局警告") == 1          # 5 次只落一行


def test_qt_repeat_count_surfaces_after_window(logdir, monkeypatch):
    """窗口过后补一条汇总——去重是挡刷屏，不是把「重复了多少次」也吞掉。

    分两拍：先在窗口内攒重复（静默），再把窗口缩到已过期，下一条即带计数落地。
    只缩窗口（不 sleep）是为了让用例不依赖真实时间流逝。
    """
    monkeypatch.setattr(logging_setup, "QT_MUTE_SECONDS", 60.0)
    setup_logging(log_dir=logdir, console=False)
    warn = logging_setup.qt_message_handler
    warn(QtMsgType.QtWarningMsg, None, "刷屏警告")     # 首条：落盘
    warn(QtMsgType.QtWarningMsg, None, "刷屏警告")     # 窗口内：静默 ×2
    warn(QtMsgType.QtWarningMsg, None, "刷屏警告")
    monkeypatch.setattr(logging_setup, "QT_MUTE_SECONDS", -1.0)
    warn(QtMsgType.QtWarningMsg, None, "刷屏警告")     # 窗口已过：补一条汇总
    _flush()
    content = (logdir / LOG_FILENAME).read_text(encoding="utf-8")
    assert content.count("刷屏警告") == 2             # 首条 + 汇总条
    assert "重复 2 次" in content


def test_qt_fatal_is_never_muted(logdir):
    setup_logging(log_dir=logdir, console=False)
    fatal = logging_setup.qt_message_handler
    for _ in range(3):
        fatal(QtMsgType.QtFatalMsg, None, "致命消息")
    _flush()
    assert (logdir / LOG_FILENAME).read_text(encoding="utf-8").count("致命消息") == 3


# ──────────────────────────────────────────────
# 标准输出接管（2026-10-13）：print / 第三方 stderr → 日志文件
# ──────────────────────────────────────────────

class _FakeStream(io.StringIO):
    """可控 ``isatty`` 的流替身：本机跑测试时 stderr 是管道，真假都由它扮。"""

    def __init__(self, tty: bool):
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _fake_stdio(monkeypatch, *, tty: bool = False):
    """把两路标准流换成可控 ``isatty`` 的替身，返回 ``(out, err)``。

    **必须在用例体内调，不能做成 fixture**：这个 pytest 版本在生成器 fixture 的建立 /
    收尾期间会挂起捕获，随后把自己的 ``EncodedFile`` 装回 ``sys.stdout`` —— fixture 里
    打的补丁会被那一下覆盖（实测：进用例体时 ``sys.stdout`` 已是 ``EncodedFile``）。
    替身的存活期到用例结束（`_clean_state` 会清掉本模块记的接管状态）。
    """
    out, err = _FakeStream(tty), _FakeStream(tty)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    return out, err


def _body(logdir):
    return (logdir / LOG_FILENAME).read_text(encoding="utf-8")


def test_print_and_raw_stderr_land_in_the_log(logdir, monkeypatch):
    """**本次改动的全部意义**：没有终端时 ``print`` 也要落盘。

    改前这两行只有一个去向（终端），而菜单栏 App 常态不是从终端启动的 ⇒ 当场消失。
    """
    _fake_stdio(monkeypatch)
    setup_logging(log_dir=logdir, console=False)
    logging_setup.capture_stdio()
    print("来自 print 的一行")
    sys.stderr.write("第三方直写 stderr 的一行\n")
    _flush()
    body = _body(logdir)
    assert "来自 print 的一行" in body and "barkit.stdout" in body
    assert "第三方直写 stderr 的一行" in body and "barkit.stderr" in body
    assert "WARNING" in body, "stderr 那一份按 WARNING 落（绕开 logging 的告警）"


def test_partial_line_is_emitted_on_flush(logdir, monkeypatch):
    """没有结尾换行的 ``write`` 在收尾时也要吐出来 —— 攒丢半句话比不记更糟。"""
    _fake_stdio(monkeypatch)
    setup_logging(log_dir=logdir, console=False)
    logging_setup.capture_stdio()
    print("后半句本来会丢", end="")
    logging_setup.restore_stdio()
    _flush()
    assert "后半句本来会丢" in _body(logdir)


def test_nobody_watching_means_no_write_to_the_original_stream(logdir, monkeypatch):
    """没人看就不写原始流：否则同一条在 ``barkit.log`` 里出现两遍，且启动期那份无界增长。"""
    out, err = _fake_stdio(monkeypatch)
    setup_logging(log_dir=logdir, console=False)
    logging_setup.capture_stdio()
    print("没人看的一行")
    sys.stdout.flush()
    assert "没人看的一行" in _body(logdir)
    assert out.getvalue() == "" and err.getvalue() == ""


def test_somebody_watching_gets_exactly_one_copy(logdir, monkeypatch):
    """有终端时每行只出现一次：接管自己写一份原样行，控制台 handler 不许再写一遍。"""
    out, err = _FakeStream(True), _FakeStream(True)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    setup_logging(log_dir=logdir, console=True)
    logging_setup.capture_stdio()
    print("终端该看到一次的一行")
    logging.getLogger("barkit.demo").info("正常的日志一行")
    sys.stdout.flush()
    logging_setup.restore_stdio()
    assert out.getvalue().count("终端该看到一次的一行") == 1
    assert "终端该看到一次的一行" not in err.getvalue(), "已被 _DropStdioEcho 挡掉"
    assert err.getvalue().count("正常的日志一行") == 1, "非 stdio 的记录照旧进控制台"


def test_capture_is_idempotent(logdir, monkeypatch):
    """重复接管不许套娃：两层 Tee 会把同一条内容记两遍。"""
    _fake_stdio(monkeypatch)
    setup_logging(log_dir=logdir, console=False)
    logging_setup.capture_stdio()
    first = sys.stdout
    logging_setup.capture_stdio()
    assert sys.stdout is first
    print("只该记一次")
    logging_setup.restore_stdio()
    _flush()
    assert _body(logdir).count("只该记一次") == 1


def test_env_var_can_turn_capture_off(logdir, monkeypatch):
    """``BARKIT_STDIO_CAPTURE=0`` ⇒ 退回旧形态（识别不出接管发生没发生时，得有这个口）。"""
    out, _err = _fake_stdio(monkeypatch)
    monkeypatch.setenv("BARKIT_STDIO_CAPTURE", "0")
    setup_logging(log_dir=logdir, console=False)
    logging_setup.capture_stdio()
    assert sys.stdout is out, "没接管：sys.stdout 还是原样"
    assert logging_setup._captured is None


def test_restore_puts_back_the_original_streams(logdir, monkeypatch):
    out, err = _fake_stdio(monkeypatch)
    setup_logging(log_dir=logdir, console=False)
    logging_setup.capture_stdio()
    assert sys.stdout is not out
    logging_setup.restore_stdio()
    assert sys.stdout is out and sys.stderr is err


def test_tee_forwards_unknown_attributes(logdir, monkeypatch):
    """``encoding`` / ``fileno`` 这些口要透传给原始流，否则用它们的库会当场炸。"""
    out, _err = _fake_stdio(monkeypatch)
    setup_logging(log_dir=logdir, console=False)
    logging_setup.capture_stdio()
    assert sys.stdout.encoding == out.encoding
    assert sys.stdout.writable() is True
    assert not hasattr(sys.stdout, "_tty"), "下划线名不许经 __getattr__ 转发（会与内部状态撞名）"


# ---------- 控制台该不该写：有终端就写 ----------

def test_console_follows_the_terminal_by_default(monkeypatch):
    """有终端 ⇒ 照级别全量写（不看级别）；没终端 ⇒ 常态静默，只有排查档例外。"""
    tty, pipe = _FakeStream(True), _FakeStream(False)
    monkeypatch.delenv("BARKIT_LOG_CONSOLE", raising=False)

    monkeypatch.setattr(sys, "stderr", tty)
    assert logging_setup.wants_console(None, logging.INFO) is True

    monkeypatch.setattr(sys, "stderr", pipe)
    assert logging_setup.wants_console(None, logging.INFO) is False
    assert logging_setup.wants_console(None, logging.DEBUG) is True


def test_env_var_overrides_the_terminal_rule(monkeypatch):
    """``BARKIT_LOG_CONSOLE`` 两个方向都压过终端判定（显式优先于自动）。"""
    monkeypatch.setattr(sys, "stderr", _FakeStream(True))
    monkeypatch.setenv("BARKIT_LOG_CONSOLE", "0")
    assert logging_setup.wants_console(None, logging.INFO) is False

    monkeypatch.setattr(sys, "stderr", _FakeStream(False))
    monkeypatch.setenv("BARKIT_LOG_CONSOLE", "1")
    assert logging_setup.wants_console(None, logging.INFO) is True


def test_console_handler_survives_capture(logdir, monkeypatch):
    """接管之后控制台 handler 必须写**原始** stderr，写回 Tee 就是自环（写日志→写 stderr→又一条日志）。"""
    out, err = _FakeStream(False), _FakeStream(False)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    setup_logging(log_dir=logdir, console=True)
    logging_setup.capture_stdio()
    handler = next(h for h in logging.getLogger().handlers
                   if isinstance(h, logging.StreamHandler)
                   and not isinstance(h, logging.FileHandler))
    assert handler.stream is err
    logging.getLogger("barkit.demo").info("环里不许出现这一行两次")
    logging_setup.restore_stdio()
    assert err.getvalue().count("环里不许出现这一行两次") == 1
