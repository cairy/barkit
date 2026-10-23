"""doctor/checks_data：D 组「受管数据文件 ＋ 日志」检查。

这一组从前回答的是**最难自查的一类问题**：app 把「期望态」写进用户域 JSON、root
helper 跨域读走，两边一旦对不上，helper 的处置统一是「整份拒绝 ＋ 保持系统状态不动」
（安全态），而用户侧看到的往往是「什么都没发生」。

**通道换成会话之后（2026-09-29），那一类问题的载体没有了**，D 组据此裁掉三项：

- 「三份期望态文件的 ``version`` 对不对」—— 没有对象了。两侧是否同代改由**握手**
  回答；那与 E 组（已装版本 vs 期望版本）判的是同一件事，不该在 D 组重复判死。
- 「``helper_tasks.json`` 过不过红线」—— 没有对象了。编排表现在是
  ``intent.set("tasks", …)`` 的一帧，校验发生在守护收到它的那一刻；doctor 只读、
  不连会话，拿不到那一帧，也就不该猜。
- 「三份状态快照新不新鲜」—— 没有对象了。事实由 ``event.push`` 推来，新鲜度由
  **连接本身**回答；盘上只剩一份**崩溃恢复快照**，而它按方案 §11.4 本就没有新鲜度
  要求（读它的只有「守护刚启动」与「人 ``cat``」两个场合）。

于是 D 组只剩三件事：**快照读不读得出来、用户数据坏没坏、日志尾巴有没有错**。

检查项与真源
-----------
- ``D1`` ``helper_snapshot.json`` 可解析、形状与版本对。真源是 ``helper.state`` 的
  ``SNAPSHOT_VERSION`` —— 不是 doctor 自己记的数字，否则快照格式一改 doctor 就说谎。
- ``D2`` 用户数据 JSON 可解析（``hosts_entries.json`` / ``interaction_state.json`` /
  ``agenda_log.json``）。它们都是「丢了会看见与事实不符的东西」的东西；清单与真源的
  一致性由 ``tests/test_doctor.py`` 那条对拍守着。
- ``D3`` ``barkit.log`` 尾部体检：把 ERROR / Traceback 顶到眼前。

只读：本组一个字节都不写（连探针都不写）。
"""

from __future__ import annotations

import importlib
import time
from pathlib import Path

from barkit import paths
from barkit.doctor import survey
from barkit.doctor.model import FAIL, PASS, SKIP, WARN, Check, Result, run_checks

GROUP = "data"

#: D1 快照的文件名与版本真源。
#:
#: 文件名在这里**写死一次**（守护那一侧的真源是 ``helper.state.SNAPSHOT_NAME``）：
#: 单文件、单写方，且 doctor 不 import helper 包（那条 import 会让「守护装没装」
#: 变成 doctor 能不能跑的依赖）。版本**必须**现取 —— 它是会随快照格式演进的那个数。
SNAPSHOT_NAME = "helper_snapshot.json"
_SNAPSHOT_MODULE = "barkit.capabilities.helper.state"
_SNAPSHOT_VERSION_CONST = "SNAPSHOT_VERSION"

#: D2 用户数据 JSON：(显示名, 真源)。
#:
#: 都在数据根下、都是「用户亲手填 / 答复过」的东西（§11.1 第一问）。文件名在此写死，
#: 真源是各自模块里那个常量（它们有意**不**在 ``paths.py`` 立常量 —— 各自只有一个
#: 消费者）—— 可这两处是**两处字面量**：改名或换真源时只动一处，这份清单就**静默**
#: 指着不存在的文件（新的那份不再被检查、老的那条悄悄 skip，两件事都不报错）。
#: 故真源一律写**完整的 import 路径**，由
#: ``tests/test_doctor.py::test_user_json_names_match_their_source_constants`` 逐条对拍。
#:
USER_JSON_FILES: tuple[tuple[str, str], ...] = (
    ("hosts_entries.json", "barkit.paths.HOSTS_ENTRIES_FILE"),
    ("interaction_state.json", "barkit.core.interaction.LEDGER_FILENAME"),
    ("agenda_log.json", "barkit.plugins.todo.day_log.AGENDA_FILENAME"),
    # 2026-10-10 新增：全局变量的 ask 运行值（按日）。**框架级**文件 —— 它不属于任何
    # 组件，故不在某个插件的 ``resources()`` 清单里，但仍要有人检查它读不读得出来。
    ("variables_state.json", "barkit.core.variables.store.STATE_FILENAME"),
)

#: D3：日志尾巴体检的取数口径。
LOG_TAIL_LINES = 200
LOG_TAIL_MAX_BYTES = 256 * 1024


def _user_path(filename: str) -> Path:
    """数据根下的一个用户数据文件。**调用期**取根（测试重定向才生效）。"""
    return paths.APP_SUPPORT / filename


def _snapshot_version() -> "tuple[int | None, str | None]":
    """取快照格式的期望版本。取不到**不算文件的问题** —— 如实说「问不到」，不猜。"""
    try:
        module = importlib.import_module(_SNAPSHOT_MODULE)
    except Exception as exc:  # noqa: BLE001 —— 守护包导入失败不该毁掉整条检查
        return None, f"{_SNAPSHOT_MODULE} 无法导入（{exc.__class__.__name__}: {exc}）"
    value = getattr(module, _SNAPSHOT_VERSION_CONST, None)
    if not isinstance(value, int) or isinstance(value, bool):
        return None, f"{_SNAPSHOT_MODULE}.{_SNAPSHOT_VERSION_CONST} 不是整数：{value!r}"
    return value, None


def check_snapshot() -> list[Result]:
    """D1：``helper_snapshot.json`` 可解析、形状与版本对。

    它**不是通道**（没有读者在等它），所以坏了的后果是**有界的**：守护启动时
    ``load_snapshot()`` 读不出来就当「没有」，多收敛一轮。据此分级：

    - 读不出来 / 形状不对 ⇒ WARN（不是 FAIL：它不参与任何在线判据）。
    - 版本不符 ⇒ WARN，并说清「是哪一侧旧」——快照只由守护写，所以版本不符意味着
      装着的守护与现在的代码不同代（那一件事 E1 会 FAIL，这里不重复判死）。
    """
    path = _user_path(SNAPSHOT_NAME)
    if not path.exists():
        return [Result("D1", GROUP, "崩溃恢复快照", SKIP, "还没有 helper_snapshot.json",
                       "属正常：它是守护写的，且只在快照内容真变时才落笔 —— "
                       "稳态下可以整天一次都不写，落盘只发生在意图或观测确实变了的时候")]

    data, err = survey.read_json(path)
    if err is not None:
        return [Result("D1", GROUP, "崩溃恢复快照", WARN, f"读不出来：{err}",
                       "守护启动时会把读不出来的快照当「没有」，最多多收敛一轮，"
                       "不影响在线行为；要清掉它直接删掉该文件即可")]
    if not isinstance(data, dict):
        return [Result("D1", GROUP, "崩溃恢复快照", WARN,
                       f"顶层不是对象：{type(data).__name__}",
                       "同上：守护按「没有快照」处理；删掉该文件即可")]
    if not isinstance(data.get("subjects"), dict):
        return [Result("D1", GROUP, "崩溃恢复快照", WARN, "缺 subjects 对象",
                       "同上：守护按「没有快照」处理；删掉该文件即可")]

    expected, why = _snapshot_version()
    if expected is None:
        return [Result("D1", GROUP, "崩溃恢复快照", WARN,
                       f"文件在，但取不到期望版本：{why}",
                       "先看 C 组「插件发现」；本条的结论此时不可信")]
    actual = data.get("version")
    if actual != expected:
        return [Result("D1", GROUP, "崩溃恢复快照", WARN,
                       f"version = {actual!r}，当前格式是 {expected}",
                       "快照只由守护写 ⇒ 说明装着的守护与现在的代码不同代。"
                       "看 E1 那条（它会 FAIL）；修好之后删掉该文件让守护重写一份")]
    written = data.get("written_at")
    when = (f"，写于 {time.strftime('%Y-%m-%d %H:%M', time.localtime(written))}"
            if isinstance(written, int) and not isinstance(written, bool) else "")
    return [Result("D1", GROUP, "崩溃恢复快照", PASS,
                   f"格式 v{expected}，{len(data['subjects'])} 个领域{when}")]


def check_user_json() -> list[Result]:
    """D2：用户数据 JSON 可解析（坏了就是坏了，没有「安全态」可言）。"""
    issues: list[Result] = []
    checked = 0
    for filename, source in USER_JSON_FILES:
        path = _user_path(filename)
        if not path.exists():
            continue
        _, err = survey.read_json(path)
        if err is None:
            checked += 1
            continue
        issues.append(Result(
            f"D2.{filename}", GROUP, filename, FAIL, err,
            f"{path} 解析失败；它由 app 自己读写（真源：{source}），多半是上次写入被中断。"
            f"删掉后重启 BarKit 会让它重建 —— 但这是用户数据："
            f"先看文件里还留着多少内容，能救则救"))
    if issues:
        return issues
    if checked:
        return [Result("D2", GROUP, "用户数据文件", PASS, f"{checked} 份 JSON 均可解析")]
    return [Result("D2", GROUP, "用户数据文件", SKIP, "尚无用户数据文件",
                   "属正常：没编辑过 hosts 条目、没被问过问题、没填过上班时间时都不会有")]


def _tail_lines(path: Path, limit: int) -> "tuple[list[str], str | None]":
    """只读文件尾部（大日志不整份读进内存）。返回 ``(行, 失败原因)``。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > LOG_TAIL_MAX_BYTES:
                handle.seek(size - LOG_TAIL_MAX_BYTES)
                # 从中间切进来的第一行必然是半行，丢掉。
                handle.readline()
            text = handle.read().decode("utf-8", errors="replace")
    except OSError as exc:
        return [], f"{exc.__class__.__name__}: {exc}"
    return text.splitlines()[-limit:], None


def check_log_tail() -> list[Result]:
    """D3：``barkit.log`` 尾部有没有 ERROR / Traceback。

    这是 ``内部路线图`` §4「复现时保留该时间点的日志段」那条验收的抓手：把首条
    错误连行文一起顶到眼前，用户才知道要保留哪一段。
    """
    path = paths.LOG_DIR / "barkit.log"
    if not path.exists():
        return [Result("D3", GROUP, "应用日志", SKIP, "还没有 barkit.log",
                       "属正常：BarKit 跑过之后才会有")]
    lines, err = _tail_lines(path, LOG_TAIL_LINES)
    if err is not None:
        return [Result("D3", GROUP, "应用日志", WARN, f"读取失败：{err}",
                       "检查日志目录权限（A5 那条）")]

    errors = [ln for ln in lines if " ERROR " in ln or ln.startswith("ERROR")]
    tracebacks = [ln for ln in lines if "Traceback (most recent call last)" in ln]
    if not errors and not tracebacks:
        return [Result("D3", GROUP, "应用日志", PASS,
                       f"末 {len(lines)} 行无 ERROR / Traceback")]
    sample = (errors or tracebacks)[0].strip()
    return [Result("D3", GROUP, "应用日志", WARN,
                   f"末 {len(lines)} 行里有 {len(errors)} 条 ERROR、{len(tracebacks)} 段 "
                   f"Traceback；首条：{sample}",
                   f"完整上下文见 {path}。复现问题时保留这一时间点的日志段，"
                   f"贴 issue 时用 `barkit doctor --json` 一并附上")]


CHECKS = (
    Check("D1", "崩溃恢复快照", check_snapshot),
    Check("D2", "用户数据文件", check_user_json),
    Check("D3", "应用日志", check_log_tail),
)


def check_data() -> list[Result]:
    return run_checks(GROUP, CHECKS)
