"""BarKit 公共路径常量。

数据根由 ``BARKIT_DATA_DIR`` 决定（与 root helper 同名的那个键，同一个意思：
「这一份数据放在哪」）。缺省是真实用户目录 —— 生产路径上一个字节都没变。

为什么必须有这个口子（2026-09-29 真机事故的根因）：测试的隔离原来只在本进程里
给本模块的**模块属性**打补丁，跨进程完全失效 —— 子进程 / ``python -c`` / 任何
非 pytest 入口拿到的仍是真实用户目录，于是「跑一轮测试」就能把 fixtures 写进
用户真实的 ``tunnel.json``（现象：iPad 隧道显示未激活、整机流量改走 Wi-Fi）。
改由环境变量决定之后，隔离退化成「子进程继承一个环境变量」，跨进程一致。
测试侧仍保留模块属性补丁（``tests/conftest.py`` 的 ``PATH_REDIRECTS``）——
两条路都通，按值导入的禁令不变（见 ``test_no_by_value_path_imports``）。

2026-09-29：**通道类文件全部消失**（方案 §11.2）。这个模块从此只列两类路径 ——
① 用户数据（丢了会看见与事实不符的东西），② 会话节点（**两个端点各一个**：root helper
常驻监听的那个，与蓝牙助手那条按需建、用完即删的）。
「期望态 / 状态 / 敲门」那十来份文件连常量一起删掉：不是改名，是没有对象了。
"""

import os
from pathlib import Path

APP_NAME = "BarKit"

#: 数据根。``BARKIT_DATA_DIR`` 为空/未设 ⇒ 真实用户目录（生产路径）。
APP_SUPPORT = Path(os.environ["BARKIT_DATA_DIR"]).expanduser() \
    if os.environ.get("BARKIT_DATA_DIR") else Path.home() / "Library/Application Support/BarKit"
LOG_DIR = Path.home() / "Library/Logs/barkit"

#: 开机自启那条 LaunchAgent 的 ``StandardOutPath`` / ``StandardErrorPath`` 落点
#: （写入方见 ``core/autostart.py`` 的 plist 模板）。
#:
#: 它收的是**还没进 logging 体系**的那一段：解释器起不来、``barkit.app`` 导入期就抛、
#: C 扩展直接往 fd 2 写。这些内容发生时 ``setup_logging`` 还没跑，``barkit.log`` 里
#: 一条都没有 —— 而「启动就没了」恰恰是最需要看日志的场景。
#:
#: **常态 0 字节，不该有第二种情况**：Python 层的输出（含 ``print``）在
#: ``logging_setup.capture_stdio`` 之后就全走 logging 了，且没人看时连原始流都不写。
#: 这里一旦有内容，说明「进程还没跑到接管那一步」或「有人绕开了 Python」。
STARTUP_LOG = LOG_DIR / "barkit-startup.log"

#: 与 root helper 那条会话的**客户端侧节点**。两侧各写一份同名同义的字面量
#: （helper 跑在系统解释器上、拿不到本模块），改动必须同批 —— 名字对不上就是
#: 「守护在听，app 连不上」，而那条故障看起来像「守护没装」。
HELPER_SOCKET = APP_SUPPORT / "helper.sock"

#: hosts 条目主数据（用户数据）：管理窗口读写，不进 config.yaml ——
#: 条目可达上千条，而 config 只留 enabled / resolve_poll 这类设置。
HOSTS_ENTRIES_FILE = APP_SUPPORT / "hosts_entries.json"

#: root 守护自己那份日志（用户域，由守护以 root 身份写，属主 root、普通用户可读）。
#: **同一份源码跑在两处**：守护那份是 ``capabilities/helper/`` 整目录的拷贝，它按
#: 自己的 ``safeio.data_dir()`` 算路径（同读 ``BARKIT_DATA_DIR``，结论与这里一致）。
#: 两处都写这个文件名是跨进程无法共享常量的结果，不是第二份定义 —— 改文件名必须同批。
HELPER_LOG = APP_SUPPORT / "helper.log"

# bt_guard 那条**助手会话**的节点（用户域）。名字与助手源码里的硬编码必须一致 ——
# 助手是 Objective-C 写的、由 `open` 拉起，拿不到 Python 这边的常量，只能两边各写一份
# （`plugins/bt_guard/resources/helper/bt-helper.m` 的 `kSocketRel`；README 里也记了一笔）。
#
# **它不是一个常驻通道**：app 在动手那一拍才建出节点，用完即 unlink —— 稳态下磁盘上
# 不该有它。旧形态那对 `bt_request.json` / `bt_result.json` 已于 2026-10-04 随
# 方案 §8 第 4 步（蓝牙旁路并入同一协议）一起消失；它们曾是剩余清单上仅有的通道类文件。
BT_HELPER_SOCKET = APP_SUPPORT / "bt_helper.sock"

# 下面这些常量**曾经存在**，随通道一起消失（逐条对应方案 §11.2 的消失清单）：
# ``ROUTES_FILE`` / ``STATE_FILE`` / ``TRIGGER_FILE``（route_guard 的期望态、观测、敲门）、
# ``TUNNEL_FILE`` / ``TUNNEL_STATE_FILE``（隧道配置与观测）、
# ``HOSTS_RULES_FILE`` / ``HOSTS_STATE_FILE``（hosts 托管块期望态与观测）、
# ``HOSTS_RESOLVED_FILE``（auto 条目的解析缓存 —— 解析结果本来就是要发给守护的意图
# 正文，在内存里天然存在）、``HELPER_TASKS_FILE``（编排表，现在是 ``intent.set`` 的一帧）。
# **不要为它们留兼容读法**：留下一个「万一还有人读」的分支，就等于承认还有第二个写方。

# 外部插件根目录（E1）：每个子目录是一个带 manifest.yaml 的外部插件。
# 可由 config.plugins.external_path 覆盖（便于开发/测试）。
EXTERNAL_PLUGINS_DIR = APP_SUPPORT / "plugins"

