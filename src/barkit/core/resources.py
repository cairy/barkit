"""core/resources：可插拔资源契约（方案 §9.1，批次 6a）。

插件经 ``Plugin.resources()`` 声明自己持有的全部外部资源，框架据此完成：
- 卸载七步中的「删数据文件（data_files − seed_files）」「system_mutations 确认」；
- 批次 4 的 A 类对账：``wanted = ∪ 所有已安装插件（含 disabled/failed）的
  resources.root_binaries``（本批只定义字段，不做对账）。

设计要点（方案原文约束）：
- **seed_files ⊆ data_files**：C 类种子 = 随包提供了初始内容的 ``data_files`` 子集，
  不是并列的第二份清单。卸载删 ``data_files − seed_files``，seed 保留（已是用户数据）。
- **分类依据是清单声明，不是运行期状态**：声明在 ``seed_files`` 中的，无论是否被
  改写，卸载一律保留。禁止实现成「运行期检测文件是否被修改」。
- ``data_files`` 相对 ``paths.APP_SUPPORT``（用户域），不得含绝对路径。
- **有外部消费方的数据文件要声明 ``deferred``**（2026-09-17 加）：删它的时机不能由框架
  拍脑袋定，得等读它的那个进程收敛（见 ``DeferredRemoval``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

#: 「延迟删除」的默认等待上限（秒）。被敲门的外部消费方 ≤1s 醒、跑一轮毫秒级，
#: 故 5s 已宽裕；它只决定「等多久放弃」，不影响正确性（超时只是不动，见各实现）。
DEFAULT_DEFER_TIMEOUT_S = 5.0


@dataclass
class DeferredRemoval:
    """卸载收尾「延迟删除」声明：删数据文件之前，须等外部消费方收敛。

    为什么需要（route_guard 实测）
    ----------------------------
    数据文件可能是**外部消费方（root helper：独立进程、周期轮询）在读**的。插件在
    ``teardown()`` 里写完最后一版期望态 + 敲门之后，消费方要一个轮次才消费得到；而框架
    紧接着（毫秒级）把文件删掉，消费方下一轮读到「文件不存在」—— 按既有语义「不存在 =
    冻结、保持上轮状态」，于是**撤销永远不生效**（两步只隔同一函数内相邻两行，消费方
    在另一个进程里 ≤1s 才醒 ⇒ 竞态必输）。

    还有更隐蔽的一层：框架汇总的那份 tasks 意图也得等。删文件之后的 ``forget()``
    会把该插件的 task 写成 ``enabled: false``，消费方即使被敲醒也不会再跑那条 task。
    所以延迟覆盖的是**整段收尾**（删数据文件 + 删配置段 + ``forget()``），而不只是删文件。

    框架只做三件事
    --------------
    **不删** → 按框架心跳轮询 ``ready()`` → 为真则收尾、到期则放弃（文件留着）。
    框架**不解释文件内容**：「消费方收敛了没有」是插件的领域知识，由 ``ready()`` 提供。

    - ``ready(watermark)``：返回 True ＝「现在收尾是安全的」。``watermark`` 由框架在**本次
      卸载开始时**记下（必然早于插件写期望态），供实现方排除**陈旧回报** —— 消费方上一轮
      写回的「早已撤干净」不能当作本轮的确认。
    - ``timeout_s``：等待上限，超时即放弃本轮（不删、不遗忘、只记 ERROR）。
    """

    ready: Callable[[float], bool]
    timeout_s: float = DEFAULT_DEFER_TIMEOUT_S


@dataclass
class SystemMutation:
    """一条系统变更的声明，供卸载前展示与 stop() 决策使用（方案 §9.1）。"""

    description: str                    # 人类可读，如「/etc/hosts 中的 BarKit 标记块」
    survives_app_exit: bool = True      # 自持型(True) / 依附型(False)，见方案 §2.5


@dataclass
class PluginResources:
    """插件持有的全部外部资源清单。框架据此做彻底卸载（方案 §9.1）。"""

    data_files: list[str] = field(default_factory=list)       # 相对 APP_SUPPORT 的文件
    seed_files: list[str] = field(default_factory=list)       # C 类种子，必须是 data_files 的子集
    config_section: str = ""                                  # 配置段名（默认即插件 id，可空）
    hotkeys: list[str] = field(default_factory=list)          # hotkeys.<name> 配置键名
    helper_tasks: list[str] = field(default_factory=list)     # helper task 名（批次 4 接线）
    system_mutations: list[SystemMutation] = field(default_factory=list)
    needs_root: bool = False
    root_binaries: tuple[str, ...] = ()                       # A 类封闭集名（批次 4 对账消费）
    # 延迟删除（**本清单里唯一带行为的字段**，只在卸载路径被读取）：非 None 时，
    # 卸载收尾要等 ``deferred.ready()`` 为真才执行。其余字段一律是纯数据。
    deferred: "DeferredRemoval | None" = None
