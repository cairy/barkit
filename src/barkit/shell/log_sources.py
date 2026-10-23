"""shell/log_sources：日志落点表 —— 有哪几份、各在哪、界面上叫什么。

**为什么是一张表而不是四处硬编码**：设置窗「维护」页的「日志」卡按这张表画按钮，
判据（有哪些分类）与落点（各自在哪）因此只有一处。加一份日志时改这里一处。

**为什么取路径的是可调用而不是 ``Path`` 值**：路径常量必须在**调用期**读
（``tests/conftest.py`` 的 ``PATH_REDIRECTS`` 逐用例改 ``barkit.paths`` 的模块属性；
导入期冻成值会让重定向静默失效）。这条与 ``shell/data_ops`` 里那枚「打开数据目录」
按钮同源，故这里也用 ``Callable[[], Path]``。

**为什么分这四类**（每一类的落点由不同的进程写就，用户排查时找的也是不同的东西）：

===========================  ==========================================  ====================
分类                          落点                                        谁在写
===========================  ==========================================  ====================
应用日志                      ``paths.LOG_DIR/barkit.log``                 本进程（菜单栏 App）
守护日志                      ``paths.HELPER_LOG``                         root 守护
隧道日志                      ``safeio._hev_log_path()``                   hev-socks5-tunnel 子进程
崩溃报告                      ``~/Library/Logs/DiagnosticReports``         macOS（系统在进程崩溃时写）
===========================  ==========================================  ====================

**隧道日志那条为什么去问 ``capabilities.helper.safeio``**：那个路径的真源在 helper 侧
（``_hev_log_path()``），它写进**系统域** ``/Library/Application Support/BarKit``。
app 侧另写一遍同样的字面量就是第二份定义 —— 两处一旦漂移，按钮指向的就不是
守护实际在写的那份，而现象是「点了没东西」（本仓最贵的一类故障）。
helper 与 app 是**同一份源码树**（系统域那份是 ``capabilities/helper/`` 整目录的拷贝），
故这里直接引用它的函数，不是跨进程猜路径。

**崩溃报告那条为什么不进 ``paths.py``**：``paths.py`` 只列「BarKit 自己的用户数据」
与「会话节点」两类，而崩溃报告是 **macOS 自己的目录**（里面还躺着别的进程的报告），
我们不写、只揭示，也没有第二个消费方。等真出现第二个消费方再挪。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

#: macOS 崩溃报告目录（``Python-*.ips`` 是本程序那一族；同目录还有别的进程的）。
CRASH_REPORTS_DIR = Path.home() / "Library/Logs/DiagnosticReports"

#: 本程序那一族的崩溃报告文件名前缀。
CRASH_REPORT_GLOB = "Python-*.ips"


@dataclass(frozen=True)
class LogSource:
    """一份日志：稳定标识（给测试与诊断读）＋ 按钮上的字 ＋ 落点取数口。"""

    key: str
    label: str
    target: Callable[[], Path]


def latest_crash_report() -> Path:
    """最新一份崩溃报告；一份都没有（或目录读不到）时返回那个目录本身。

    文件名是 ``Python-YYYY-MM-DD-HHMMSS.ips`` ⇒ 按名字排序即按时间排序。
    直接开目录会让用户在一堆**别的进程**的报告里自己找，故有就精确选中最新那份。
    """
    try:
        found = sorted(CRASH_REPORTS_DIR.glob(CRASH_REPORT_GLOB))
    except OSError:
        found = []
    return found[-1] if found else CRASH_REPORTS_DIR


def log_sources() -> list[LogSource]:
    """全部日志分类（**调用期**算，见模块 docstring）。"""
    from barkit import paths
    from barkit.capabilities.helper import safeio
    from barkit.core import logging_setup

    return [
        LogSource("app", "应用日志",
                  lambda: paths.LOG_DIR / logging_setup.LOG_FILENAME),
        LogSource("helper", "守护日志", lambda: paths.HELPER_LOG),
        LogSource("tunnel", "隧道日志", safeio._hev_log_path),
        LogSource("crash", "崩溃报告", latest_crash_report),
    ]


__all__ = ["CRASH_REPORTS_DIR", "CRASH_REPORT_GLOB", "LogSource",
           "latest_crash_report", "log_sources"]
