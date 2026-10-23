"""shell/data_ops：打开数据目录 / 复制诊断这两枚按钮。

**两枚的落点不同**（2026-09-22 用户上报后重划）：

- 打开数据目录是**全局**的（每个组件打开的都是同一个目录）⇒ 唯一落点是设置窗「维护」
  页的「数据」卡，不该在每个组件页各出现一份；
- 复制诊断是**组件自己的**（只有它知道该收集哪些事实）⇒ 长在组件自己那张卡里，与
  「拨测」同排。

形状（放进剪贴板、说一句回执、失败兜底）归框架，内容归组件（``Plugin.diagnostics_text()``）。
"""

from __future__ import annotations

from PySide6.QtWidgets import QApplication

from barkit import paths
from barkit.shell import data_ops


# ---------- 两枚按钮各自独立（不再捆成一个块） ----------

def test_the_two_actions_are_separate_buttons(qtbot):
    """两枚按钮**各自可单独构造** —— 它们的落点不同，不能要求同生同死。"""
    qtbot.addWidget(data_ops.data_dir_button())
    qtbot.addWidget(data_ops.diagnostics_button(None, lambda: "报告正文"))


# ---------- 打开数据目录 ----------

def test_open_data_dir_goes_through_run_cmd(qtbot, monkeypatch):
    """目录真源是 ``paths.APP_SUPPORT``，唤起走 ``core/proc.run_cmd``（禁 shell、带超时）。"""
    calls: list[list[str]] = []

    class _Res:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(
        "barkit.shell.data_ops.run_cmd",
        lambda cmd, timeout=10: calls.append(list(cmd)) or _Res(),
    )
    said: list[tuple[str, bool]] = []
    data_ops.data_dir_button(say=lambda t, e=False: said.append((t, e))).click()
    # 断言取**调用期**的模块属性：conftest 把 ``paths.APP_SUPPORT`` 重定向到 tmp，
    # 生产代码与用例都必须读同一个值（按值导入会各自读到自己那份冻结的路径）
    assert calls == [["open", str(paths.APP_SUPPORT)]]
    assert said == [], "成功 = 没话要说（访达窗口跳出来就是回执）"


def test_open_data_dir_reports_failure_without_raising(qtbot, monkeypatch):
    """``open`` 返回非 0 ⇒ 一句错误回执；**不抛**（UI 线程上抛异常就是崩窗口）。"""
    class _Res:
        returncode = 1
        stderr = "no such file or directory"

    monkeypatch.setattr("barkit.shell.data_ops.run_cmd", lambda cmd, timeout=10: _Res())
    said: list[tuple[str, bool]] = []
    data_ops.data_dir_button(say=lambda t, e=False: said.append((t, e))).click()
    assert len(said) == 1 and said[0][1] is True and "数据目录" in said[0][0]


def test_open_data_dir_swallows_timeout(qtbot, monkeypatch):
    """超时 / 可执行缺失同样只回一句错误 —— 调用方不必写 try。"""
    def boom(cmd, timeout=10):
        raise TimeoutError("超时")

    monkeypatch.setattr("barkit.shell.data_ops.run_cmd", boom)
    said: list[tuple[str, bool]] = []
    data_ops.data_dir_button(say=lambda t, e=False: said.append((t, e))).click()
    assert len(said) == 1 and said[0][1] is True


# ---------- 在访达里揭示（「打开数据目录」与「日志」卡五枚共用这一处实现） ----------

def _capture_open(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    class _Res:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(
        "barkit.shell.data_ops.run_cmd",
        lambda cmd, timeout=10: calls.append(list(cmd)) or _Res(),
    )
    return calls


def test_reveal_opens_a_directory_instead_of_selecting_it(qtbot, monkeypatch, tmp_path):
    """目录 ⇒ ``open <dir>``（进去）；``open -R <dir>`` 只会在父目录里选中它，不是用户要的。"""
    calls = _capture_open(monkeypatch)
    folder = tmp_path / "logs"
    folder.mkdir()
    said: list[tuple[str, bool]] = []
    data_ops.reveal_button(
        "打开日志目录", lambda: folder, say=lambda t, e=False: said.append((t, e)),
        what="日志目录",
    ).click()
    assert calls == [["open", str(folder)]]
    assert said == [], "成功 = 没话要说（访达窗口跳出来就是回执）"


def test_reveal_selects_a_file_in_its_folder(qtbot, monkeypatch, tmp_path):
    """文件 ⇒ ``open -R <file>``：开它所在的目录并选中它（用户要的是**那一份**）。"""
    calls = _capture_open(monkeypatch)
    target = tmp_path / "barkit.log"
    target.write_text("", encoding="utf-8")
    data_ops.reveal_button("应用日志", lambda: target).click()
    assert calls == [["open", "-R", str(target)]]


def test_reveal_points_at_the_folder_when_the_target_is_missing(qtbot, monkeypatch, tmp_path):
    """还没有那份日志 ⇒ 按文件处理，落在父目录上（用户至少看见「本来该在那儿的东西」不在）。"""
    calls = _capture_open(monkeypatch)
    missing = tmp_path / "helper.log"
    data_ops.reveal_button("守护日志", lambda: missing).click()
    assert calls == [["open", "-R", str(missing)]]


def test_reveal_reports_a_failure_without_raising(qtbot, monkeypatch, tmp_path):
    """``open`` 非 0 ⇒ 一句错误回执，回执里的名词是 ``what`` 而不是按钮字样。"""
    class _Res:
        returncode = 1
        stderr = "进不去"

    monkeypatch.setattr("barkit.shell.data_ops.run_cmd", lambda cmd, timeout=10: _Res())
    said: list[tuple[str, bool]] = []
    data_ops.reveal_button(
        "打开日志目录", lambda: tmp_path, say=lambda t, e=False: said.append((t, e)),
        what="日志目录",
    ).click()
    assert len(said) == 1 and said[0][1] is True
    assert said[0][0] == "没能打开日志目录 —— 请重试。"


def test_reveal_survives_a_broken_path_getter(qtbot, monkeypatch, tmp_path):
    """取路径就抛 ⇒ 同样只回一句错误，不冒泡（取值口是别人给的代码）。"""
    _capture_open(monkeypatch)

    def boom():
        raise RuntimeError("算不出来")

    said: list[tuple[str, bool]] = []
    data_ops.reveal_button(
        "崩溃报告", boom, say=lambda t, e=False: said.append((t, e))
    ).click()
    assert len(said) == 1 and said[0][1] is True and "崩溃报告" in said[0][0]


# ---------- 复制诊断 ----------

def test_copy_diagnostics_puts_the_report_on_the_clipboard(qtbot):
    """一段可粘贴的纯文本进剪贴板，回执报**字符数**（用户据此知道复制到了东西）。"""
    QApplication.clipboard().setText("")
    said: list[tuple[str, bool]] = []
    data_ops.diagnostics_button(
        None, lambda: "报告正文", say=lambda t, e=False: said.append((t, e))
    ).click()
    assert QApplication.clipboard().text() == "报告正文"
    assert len(said) == 1 and said[0][1] is False and "已复制到剪贴板" in said[0][0]


def test_copy_diagnostics_reprobes_on_every_click(qtbot, monkeypatch):
    """每次点击**现问一次**：用户点它的场景是他刚发现不对，要的是此刻的事实。"""
    QApplication.clipboard().setText("")
    calls: list[int] = []

    def reader():
        calls.append(1)
        return f"第 {len(calls)} 次"

    btn = data_ops.diagnostics_button(None, reader)
    btn.click()
    btn.click()
    assert calls == [1, 1]           # 生成口每次都被调到（不是画页面那一刻缓存的）
    assert QApplication.clipboard().text() == "第 2 次"


def test_copy_diagnostics_refuses_an_empty_report(qtbot):
    """组件拿不出内容时说一句错误，**不把空串放进剪贴板**（那会悄悄清掉用户剪贴板）。"""
    QApplication.clipboard().setText("用户原来的东西")
    said: list[tuple[str, bool]] = []
    data_ops.diagnostics_button(
        None, lambda: "", say=lambda t, e=False: said.append((t, e))
    ).click()
    assert QApplication.clipboard().text() == "用户原来的东西"
    assert len(said) == 1 and said[0][1] is True and "没有可复制" in said[0][0]


def test_copy_diagnostics_swallows_a_broken_reader(qtbot, monkeypatch):
    """生成口抛异常 ⇒ 一句错误回执，不冒泡（设置窗不该被一个诊断搞崩）。"""
    def boom():
        raise RuntimeError("读不到")

    said: list[tuple[str, bool]] = []
    data_ops.diagnostics_button(
        None, boom, say=lambda t, e=False: said.append((t, e))
    ).click()
    assert len(said) == 1 and said[0][1] is True and "失败" in said[0][0]


# ---------- 取数口：组件「覆没覆写」= 有没有诊断可给 ----------

def test_manager_returns_the_reader_only_when_the_plugin_overrides_it():
    """组件的诊断生成口：**没覆写** ``Plugin.diagnostics_text`` ⇒ ``None``（不画那枚按钮）。

    判据必须**静态**（看类覆没覆写），不能为了决定画不画按钮先跑一次诊断 —— 那要读
    ps 快照与系统值。直接调 manager 那个方法（**不复刻判据** —— 复制一份实现来断言
    等于什么也没守）。
    """
    from barkit.core.plugin import Plugin
    from barkit.core.plugin_manager import PluginManager

    class _Plain(Plugin):
        pass

    class _Diagnosed(Plugin):
        def diagnostics_text(self):
            return "正文"

    def reader_for(plugin):
        mgr = PluginManager.__new__(PluginManager)
        mgr._probe = lambda pid: plugin            # 只换取实例那一步
        return PluginManager.plugin_diagnostics(mgr, "px")

    assert reader_for(None) is None                # 取不到实例（未加载）
    assert reader_for(_Plain.__new__(_Plain)) is None
    assert reader_for(_Diagnosed.__new__(_Diagnosed))() == "正文"
