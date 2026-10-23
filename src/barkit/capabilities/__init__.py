"""capabilities：框架提供的可选能力基类（能力层）。

与 ``core/``（引擎，UI 无关）、``shell/``（渲染原语）、``plugins/``（具体组件）并列，
是显式的「能力层」。每个能力基类 = 单一正交关注点，只暴露该关注点所需最小面；
具体插件**继承它需要的业务能力**（需要什么继承什么），并实现一个或多个抽象钩子。

依赖方向单向：``capabilities`` → ``core`` + ``contribution``；``core``/``shell``/``contribution``
都不反向依赖本包。**反向是禁止的**：本包**不得** ``import`` ``shell`` 或 ``plugins``（L2 在 ``shell``
之下，§3.2），由 ``tests/test_architecture.py::test_layer_dependency_direction_upper_layers``
零白名单守门。本层现有**四个**能力，一律**零 UI 依赖**地只产出规格对象：
``AutomationCapability``（``automation.py``，自动化运行态开关 ＋ 模式徽标）、
``HelperOpsCapability``（``helper_ops.py``，同一台 root helper 的运维三件事）、
``NotifyCapability``（``notify.py``，系统通知）、
``InlineFeedbackCapability``（``inline_feedback.py``，设置页行内反馈槽）。
它们产出什么进 ``contribution``
（**不**进 ``shell``），怎么画由框架决定 —— 这正是把渲染从能力层推回框架的原因；
新能力同样**不许**引入 UI 类型，并在本 docstring 与
``docs/architecture/components/能力目录.md`` §3.1 各登记一行。

约定：
- 能力基类写成**协作式 mixin**——``__init__`` 必须 ``super().__init__(ctx)``，钩子用
  ``super()`` 解析，以支持 ``class Plugin(AutomationCapability, Scheduled, ...)`` 多重组合且 MRO 干净。
- 每个能力只拥有自己的关注点，**不**包含具体插件的领域逻辑（后端写、规则解析、信号）。
- 新增能力：在 ``capabilities/`` 下加一个模块，不动 ``core``、不动现有插件（除非它们
  选择继承）。框架贡献消费完全基于 ``Plugin`` 契约 duck-typing，与新能力无关。

例外（**唯一一个，含两半**）：``capabilities/helper/`` 与 ``capabilities/helper_client.py``
不是能力 mixin，而是同一个 root helper 的**两侧**——

- ``helper/``：**随包部署到系统域的 root 侧源码包**（批次 4 归位；由
  ``/usr/bin/python3 -m helper`` 独立进程运行）。因此它**不 import 任何 barkit 代码**
  （连 ``core``/``shell`` 也不 import），只依赖标准库——这条硬约束由
  ``tests/test_helper_contract.py`` 守护；
- ``helper_client.py``：**app 侧客户端**（安装 / 卸载 / 状态 / 版本对账）。2026-09-14 从
  ``plugins/route_guard/daemon.py`` 上提到此，以消除两处跨插件 import（见《插件结构规范》
  §8.1）。放这里而非 ``core/``：`core` 层禁止 import ``barkit.capabilities``，而本模块要调
  ``rootbin.verify_file()``——那依赖 ``ROOT_BINARIES`` 里硬编码的 **sha256 发行态基线**，
  不是「名级副本」能表达的东西，复制进 core 等于给 A 类数据立第二真源。

阅读本包其余内容时请勿把这两者当能力基类的样板。
"""
