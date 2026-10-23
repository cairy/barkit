"""tests/test_todo_command：任务那条命令的**落地**（``plugins/todo/command.py``）。

这一层现在只剩一件自己的事 —— **出声**。切分、执行、**挪出主线程**三件都归
``core/hotkey_command``（``fire``），那一边的判据在 ``tests/test_hotkey_command.py`；
本文件只问「结局有没有被接到日志上、措辞分不分得开」：

- **成功也出声**：日志档停在 INFO 时只在失败记一条，等于「有没有跑过」在日志里分不出来
  —— 而这条命令的产物没有别的落脚点（浮层是零模态的提醒，跑完就没影了）。
- **输出也落进日志**（成功与失败都记，见 ``_say``）：命令成了什么样、为什么没成，全在
  它的输出里 —— 对「到点发一个请求」这种用法，HTTP 的错误体是唯一的线索。
- **「没跑起来」与「跑了但失败」分开**：前者要看原因（写错了程序名 / 引号不配对），
  后者要看输出。挤成一句话，用户查的方向就是错的。
- **不是把结果丢掉**：``fire`` 真的把回调交出去了（少交一次就一条日志都没有）。

**这一层不测线程**：插件不得私起线程（``tests/test_architecture.py``），线程归框架 ——
所以「``fire`` 立刻返回」那条判据在框架那边，这里不重复。
"""

from __future__ import annotations

import sys

import pytest

from barkit.core import hotkey_command as hc
from barkit.plugins.todo import command as cmd_mod


@pytest.fixture
def _sync_fire(monkeypatch):
    """把 ``hotkey_command.fire`` 换成**同步**版：真跑一遍 ``execute`` 再把结局交给回调。

    换掉的只是「另一条线程」那一半；切分、执行、结果对象、以及**回调被不被调到**都还是
    真的。真跑是刻意的 —— 换成假结果就测不出「回调确实接在回路上」这件事。
    """
    def _fake(command, on_result=None, timeout=None):
        if not (command or "").strip():
            return False
        if on_result is not None:
            on_result(hc.execute(command))
        return True

    monkeypatch.setattr(hc, "fire", _fake)


def test_fire_refuses_an_empty_command():
    """空串不是失败，但也不该起一条空线程（``parse_all`` 已把没配的规整成空串）。"""
    assert cmd_mod.fire("") is False
    assert cmd_mod.fire("   ") is False
    assert cmd_mod.fire(None) is False


def test_fire_returns_what_the_framework_said(_sync_fire, monkeypatch):
    """收下了就 ``True``；框架说没收下（线程起不来）就照实传回去，不自己改口。"""
    assert cmd_mod.fire("/bin/echo hi") is True
    monkeypatch.setattr(hc, "fire", lambda *_a, **_k: False)
    assert cmd_mod.fire("/bin/echo hi") is False


def test_the_result_actually_reaches_the_log(_sync_fire, caplog):
    """成功也记一条 INFO —— 这是本模块唯一自己的口径。

    只断言「有记录且都是 INFO」，不数条数：成功那条现在会**再**带一行输出
    （见下面 ``test_the_output_of_the_command_lands_in_the_log_too``）。
    """
    with caplog.at_level("INFO", logger="barkit.plugins.todo.command"):
        cmd_mod.fire("/bin/echo hi")
    assert caplog.records, "一条都没记"
    assert all(r.levelname == "INFO" for r in caplog.records), caplog.records
    assert "已执行" in caplog.records[0].message


def test_the_output_of_the_command_lands_in_the_log_too(_sync_fire, caplog):
    """命令的**输出**也要落地，成功与失败都记。

    对「到点发一个请求」这类用法，输出是唯一能回答「成了什么样 / 为什么没成」的东西
    （HTTP 的错误体、``Could not resolve host`` 这类原因都只出现在这里）。只记一句
    「已执行」等于把它丢在 :class:`CommandResult` 里没人看。

    **标记刻意是「算出来的」**（``print('x'*8)`` ⇒ 输出 ``xxxxxxxx``，``print('b'+'oom')``
    ⇒ 输出 ``boom``）：直接写 ``print('xxxxxxxx')`` 会让**命令串本身就含那个词**，于是
    「命令被记下来了」也能让断言变绿 —— 而这条判据要问的是「输出有没有被记下来」。
    下面那句 ``not in command`` 把这条独立性焊在用例里，改标记的人也看得见。

    截断不在这里另判一份：``execute`` 已按 ``MAX_OUTPUT`` 收过一道，本层原样转述。
    """
    ok_command = f'{sys.executable} -c "print(\'x\'*8)"'
    assert "xxxxxxxx" not in ok_command, "标记必须只可能来自输出"
    with caplog.at_level("INFO", logger="barkit.plugins.todo.command"):
        cmd_mod.fire(ok_command)
    assert any("xxxxxxxx" in r.getMessage() for r in caplog.records), caplog.records

    bad_command = f'{sys.executable} -c "import sys; print(\'b\'+\'oom\'); sys.exit(3)"'
    assert "boom" not in bad_command, "标记必须只可能来自输出"
    caplog.clear()
    with caplog.at_level("WARNING", logger="barkit.plugins.todo.command"):
        cmd_mod.fire(bad_command)
    assert any(
        r.levelname == "WARNING" and "boom" in r.getMessage() for r in caplog.records
    ), caplog.records


def test_a_command_that_ran_and_failed_is_not_confused_with_one_that_never_ran(_sync_fire, caplog):
    """三档结局三句话：成功 / 跑了但失败 / 根本没跑起来。

    用解释器本身当「跑得起来但退出码非 0」的那一档：``/bin/false`` 在 macOS 上
    不存在（它住在 ``/usr/bin``），拿它测会把「跑了但失败」和「根本没这个程序」
    混成一条 —— 而这两种恰恰是本用例要分开的两件事。
    """
    caplog.clear()
    with caplog.at_level("WARNING", logger="barkit.plugins.todo.command"):
        cmd_mod.fire(f'{sys.executable} -c "import sys; sys.exit(3)"')
    assert any(r.levelname == "WARNING" and "没成功" in r.message for r in caplog.records), (
        caplog.records
    )

    caplog.clear()
    with caplog.at_level("WARNING", logger="barkit.plugins.todo.command"):
        cmd_mod.fire("/no/such/program-for-barkit-tests")
    assert any("没跑起来" in r.message for r in caplog.records), caplog.records


@pytest.mark.parametrize("text", ['open "https://x', "open 'https://x"])
def test_a_command_that_cannot_be_cut_is_reported_not_silently_skipped(_sync_fire, caplog, text):
    """引号不配对：**出声**（WARNING），不是静默不跑。

    界面上同一件事挡在保存前（``detail_view`` 的校验门），但盘上那份可能是手写的 ——
    到点那一刻它只会得到这一条日志。理由由 ``CommandResult.reason`` 带来，本层不另判
    一遍「切得开吗」（那会变成第二份判据）。
    """
    with caplog.at_level("WARNING", logger="barkit.plugins.todo.command"):
        assert cmd_mod.fire(text) is True, "收下了 —— 切不开是在跑的那一刻才发现的"
    assert any("没跑起来" in r.message and "引号" in r.message for r in caplog.records), (
        caplog.records
    )
