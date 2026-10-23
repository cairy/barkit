"""capabilities.helper：root helper 的**契约层**（方案 §7.4，批次 4）。

定位
----
这不是「一份能力基类」，而是随包部署到系统域的 root helper 源码包。

两条硬约束（由 `tests/test_helper_contract.py` 静态门禁守护）：

1. **只依赖 Python 标准库**。helper 由系统 ``/usr/bin/python3`` 运行（兼容 3.9+），
   部署形态是「整个包复制到系统域 → 以 ``sys.path`` 平铺导入」。因此本包内任何
   模块都**不得** ``import barkit.*``，否则部署后必然 ImportError。
   同理，语法必须 3.9 可解析（用 ``ast.parse(feature_version=(3, 9))`` 门禁）。
2. **能力封闭**。``actions.ACTION_SCHEMAS`` 是编译期常量：root 侧能做什么，是源码
   决定的，运行期不注册、不发现、不加载新能力。新增能力 = 改这里 + bump
   ``HELPER_VERSION`` + 重装 daemon（那道摩擦力是安全特性，见 §7.4.4）。

本包与 app 侧的关系
------------------
app 侧**只读契约**（如安装期用 ``rootbin`` 校验 A 类二进制基线），不参与执行。
``core/`` 不得依赖 ``capabilities/*``（分层门禁 `test_layer_dependency_direction_core`），
故 `core/installer.py` 里的名级副本由单真源一致性门禁强制与 `rootbin` 相等。

模块
----
契约层（app 侧只读）
- ``rootbin``  A 类 root 二进制的封闭命名集 + 发行态 sha256 基线 + 完整性 manifest
                + 启动/执行前自检原语（owner/权限、整目录聚合哈希、在册校验）
- ``specs``    参数值域类型（IPv4 / CIDR / 网卡名 / 网络服务名 / SSID / 主机名 / 端口…）
- ``actions``  特权原语集（ACTION_SCHEMAS）+ 参数校验器 + task 表五条红线校验器

运行期（root LaunchDaemon 本体，``python3 -m helper``）
- ``__main__``   入口（极薄，只转交 core.main）
- ``version``    版本锚点（单一真源；app 侧据此判「是否过期」）
- ``core``       入口与主循环（薄）：**启动完整性自检** + 装配生命周期 + 交给会话/调度器，
                 **不含业务决策**
- ``protocol``   线格式：4 字节大端长度前缀分帧 + 消息构造（**两侧共用**）
- ``session``    守护侧会话服务端：``selectors`` 主循环 + 对端 uid/pid 凭据校验
- ``tasks``      编排层：按 app 发来的任务表 + **内存里的意图** → 有意图才跑，按 interval 调度
- ``primitives`` 能力 → 实现的唯一映射表（三条同步原语 + 两条一次性命令）
- ``routeops`` / ``tunnelops`` / ``hostsops``
                 业务机制：路由增删 / 隧道状态机 / hosts 托管块（系统状态变更的执行）
- ``lifecycle``  系统状态复原：退出钩子 / 信号 / 崩溃残留回收
- ``consts``     跨模块共享常量（系统命令绝对路径 / 节拍）
- ``safeio``     唯一的安全落盘入口 + 用户域/系统域的路径根
- ``state``      观测事实的**内存态** ＋ 一份崩溃恢复快照（不再是通道）

分层的意义（§7.4.3 职责边界）：**能力**（ACTION_SCHEMAS）与**编排**（任务表 ＋ 意图）
是两件事——前者封闭、改源码才动；后者开放、插件零重装即可增删。业务机制与调度/生命周期
分离，使「新增一套 root 业务」= 加一个 ops 模块 + 一条同步原语，而不是往 core 里塞代码。

通道（2026-09-29）
----------------
**root 侧不读任何用户域文件。** 期望态、命令、事实、存活四件事全部走一条本机
Unix 域套接字会话（``本包 protocol`` ＋ ``session``，对端是
``barkit.capabilities.helper_link``）。盘上只剩两样：会话节点本身，和一份
「崩溃恢复＋给人看」的快照（``state``）。旧的 ``routes.json`` / ``tunnel.json`` /
``hosts_rules.json`` / ``helper_tasks.json`` / ``state.json`` / ``tunnel_state.json`` /
``hosts_state.json`` / ``.trigger`` 全部消失。
"""
