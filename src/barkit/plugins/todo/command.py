"""todo/command：任务那条 ``command`` 的**执行**（一行字，不是一段脚本）。

本模块只剩一件自己的事 —— **出声**；「切分 / 跑 / 挪出主线程」三件都归
``core/hotkey_command``，本模块只是它的一个调用点：

1. **切分与跑**借 ``core/hotkey_command`` —— 那一份是仓里「用户在输入框里敲的一行命令」
   的唯一口径（``shlex.split`` ＋ ``shell=False`` ＋ 有界超时 ＋ **不抛**的结果对象）。
   自己再写一份的下场是同一个输入框在热键那边与待办这边切出两个 argv，而两处都不报错。
   它**延迟导入**：那个模块为 ``CommandRunner``（QThread）在模块级 import PySide6，而本
   模块在插件的**运行期**路径上（``plugin.py`` 导入它）。真正要跑命令时 app 早就是 Qt
   进程了，那一天才付这一次导入。
2. **挪出主线程**归 ``hotkey_command.fire`` —— **线程不许由插件起**
   （``tests/test_architecture.py`` 的 ``_plugin_timer_hits``：插件不得私起线程，
   线程归框架）。调用点有两条、落在两种线程上：**到点那一刻**归周期线程
   （``plugin._fire_due_commands`` 在 ``tick()`` 里判边沿），**点「完成」那一刻**归主线程
   （``plugin.set_done``）。同步执行最坏是把整条周期停住 / 把界面卡住。走 ``fire``
   而不是 ``CommandRunner``：这条命令的产物只有日志，不需要回到界面，而
   ``CommandRunner`` 的结局走 Qt 信号队列、**得有事件循环**才收得到。
3. **出声**（本模块唯一自己的口径）：成功也记一条 INFO，**连命令的输出一起记**
   （``_say``）。「没跑成」的线索只有日志能给（浮层那两处是零模态的提醒，跑完的命令
   没有落脚点），而**日志档停在 INFO** 时只在失败时记一条，等于「有没有跑过」在日志里
   分不出来；不记输出则连「跑成什么样」也分不出来 —— 对「到点发一个请求」这类用法，
   HTTP 的错误体是唯一能回答「为什么没成」的东西。

**不代跑 root**：与热键那条路同一条口径（``core/hotkey_command`` 的模块头）——
需要 root 的动作必须在守护里加具名原语，不能从配置里读一串字绕过去。
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def fire(text: str) -> bool:
    """跑一条任务命令；返回「这一条收下了没」（**不是「跑成功了没」**）。

    收下 ＝ 字不是空的且线程起来了。跑的结果在线程里落到日志上 —— 调用点（提醒登场 /
    点完成）在主线程上，**不能**在那儿等一个可能跑十秒的进程。

    空串返回 ``False``：`parse_all` 那一层已经把它规整成空串了，「没配命令」不是失败，
    但也不该起一条空线程。
    """
    command = (text or "").strip()
    if not command:
        return False
    from barkit.core import hotkey_command  # 延迟导入，见模块 docstring

    return hotkey_command.fire(command, lambda result: _say(command, result))


def _say(command: str, result) -> None:
    """**在这条命令自己的线程里**被调：所以只碰日志（不碰 Qt、不碰配置、不碰组件状态）。

    这很要紧：线程里写组件的字段 / 发 Qt 信号都要回到主线程才安全，而这条命令的产物
    只有一件事要告诉人 —— 它跑成没跑成。那件事写日志就够了。

    **输出也一并记下来**（成功与失败都记）：命令做了什么全在它的输出里（请求的错误体、
    ``Could not resolve host`` 这类原因）。只记「已执行」等于把唯一能回答「为什么没成」
    的东西丢掉。截断**不在这里另定一份** —— ``execute`` 已按 ``MAX_OUTPUT`` 收过一道，
    这里再定一个上界就是第二个判据。
    """
    output = (result.output or "").strip()
    if result.ok:
        log.info("命令已执行（%d 毫秒）：%s", result.duration_ms, command)
        if output:
            log.info("它的输出：%s", output)
        return
    if result.started:
        log.warning("命令跑了但没成功：%s —— %s", result.headline, command)
    else:
        log.warning("命令没跑起来：%s —— %s", result.headline, command)
    if output:
        log.warning("它的输出：%s", output)
