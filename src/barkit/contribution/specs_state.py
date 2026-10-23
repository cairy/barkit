"""``contribution`` 契约层的**状态词汇与状态机声明**：``StateLevel`` 五值（健康 / 模式 / 聚合徽标的共同词汇）、托盘角标阈值 ``TRAY_MIN_LEVELS`` 与它的出厂档，以及 ``StateOption``（状态机里的一个取值）与 ``StateSpec``（一台状态机的整份静态声明）。

拆这里而不是把 ``StateLevel`` 塞进别的族，是因为读托盘阈值的那几处外面（``shell/renderers/badge.py`` / ``shell/settings/general.py`` / ``core/config``）读的是同一组名字，它们随级别词汇走 —— 级别归取值（``StateOption.level``），取值归 ``StateSpec``，三件必须能一起被翻到。

依赖：只向下取 ``icons``（``StateOption.icon`` / ``StateSpec.icon`` 的注解）与 ``base``（``StateSpec`` 继承 ``Contribution``）；``StateLevel`` 与 TRAY 两件零依赖。

``specs.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from dataclasses import (
    dataclass,
    field,
)
from enum import Enum

from .specs_base import Contribution
from .specs_icons import IconName


class StateLevel(Enum):
    """状态级别：健康态、模式态、聚合徽标的共同词汇（状态同源，§1-4）。

    **它不出现在运行期报告里**（决议 D13）：级别是**取值**的属性，声明在
    ``StateOption.level``；``StateReport`` 只报取值名，框架回查声明取级别。
    """

    OK = "ok"
    WARN = "warn"
    ERR = "err"
    IDLE = "idle"
    OFF = "off"


#: 托盘角标阈值（全局配置 ``tray.min_level``）的**值域**，只有这两档（决议 D17 /
#: 契约规范 §2.5）。「屏蔽 ERR」不是可选项 —— ``ERR`` 一律露出，阈值收到最高也拦不住
#: 它（安全不变式）；``OK`` / ``IDLE`` / ``OFF`` 恒低于 ``WARN``（等价于「不提醒」），
#: 故没有第三档。**顺序即严重度**，首个是出厂档。
#:
#: 落点为什么在契约层：它约束的是上面那套级别词汇的**哪几级对用户可调**，与 ``StateLevel``
#: 同源；写侧（``core/config.py``）、UI（``shell/settings.py``）与读侧
#: （``shell/renderers/badge.py``）都读这一份，不各写一遍字面量。
TRAY_MIN_LEVELS: tuple[str, ...] = (StateLevel.WARN.value, StateLevel.ERR.value)

#: 出厂档位：用户没写过 ``tray.min_level`` 时按它算（读侧 / UI 首屏 / 写侧「改回默认」同源）。
TRAY_MIN_LEVEL_DEFAULT: str = TRAY_MIN_LEVELS[0]


@dataclass(frozen=True)
class StateOption:
    """状态机的**一个取值**（声明期静态，决议 D13）。

    组件把「这台状态机可能出现的全部取值」列举出来，运行期只报取值名。五条信息
    一次说清，且只有这一处：

    - ``id``：运行期报告引用的名字（``StateReport.state``）。同一台状态机内唯一。
    - ``level``：**必填**。颜色 / 角标 / 托盘聚合的唯一来源——缺了它，界面就只能
      猜（决议 D2「状态自带颜色」之所以关闭，前提正是级别在声明里齐备）。
    - ``icon``：取值图标，随取值变（如「第二屏在线 → 实心屏 / 离线 → 空心屏」）。
      留空回落该状态机的**主图标** ``StateSpec.icon``，不是回落别的组件。
    - ``text``：默认文案（业务语言，「电量低」）。运行期 ``StateReport.summary``
      非空时被它覆盖；留空则原样上屏。措辞要说人话、不放技术级别名。
    - ``alerts``：这一格**值不值得提醒到托盘**（决议 D17）。省略时按级别取默认：
      ``WARN`` / ``ERR`` 为真，其余为假——「出事一定看得见」因此不必每个取值各写
      一遍。显式 ``False`` 可压掉某个高级别取值（如计划内的重连），显式 ``True``
      可把一个看着不严重的取值抬进托盘（如「通知静音」）。它只表达**作者的**
      提醒意图，最终上不上屏还看用户阈值（``ERR`` 不受用户阈值限制，见契约 §2.5）。
    """

    id: str
    level: StateLevel
    icon: "IconName | None" = None
    text: str = ""
    alerts: "bool | None" = None

    @property
    def alerts_on(self) -> bool:
        """解析后的提醒意图：省略 ``alerts`` 时 ``WARN`` / ``ERR`` 为真，其余为假。"""
        if self.alerts is not None:
            return self.alerts
        return self.level in (StateLevel.WARN, StateLevel.ERR)


@dataclass
class StateSpec(Contribution):
    """一台状态机的**声明**（静态）。运行期数值在 ``StateReport`` 里，勿塞回本类。

    门禁 test_statespec_is_static_only 守护：本类不得出现 level / summary 等
    动态字段——否则状态流出现第二条路径（UI 规范 §4.1）。注意 ``states`` 里的
    ``StateOption.level`` **不违反**这条：级别归**取值**，不随轮次变，是静态的。

    **取值必须列举**（决议 D13）：``states`` 是这台状态机的全部可能取值。
    于是「设置页与文档能列出组件的全部取值」和「报告了没声明的取值可被发现」
    都成了自然结果；作者写的是业务语言（「电量低」），技术级别由框架回查。

    ``initial`` 是**首屏从哪一格开始**（取值名，不是工厂函数）：留空取 ``states``
    的第一个。``stale_after`` 是失联阈值。二者都是状态**源**的节律元数据
    （UI 规范 §8.2），不是动态数值。

    ``icon`` 是这台状态机的**主图标**（静态声明），一物两用：

    ① **资格**：声明了主图标（``icon is not None``）⇒ 可被用户选为**菜单栏主图标**的
       来源（设置窗「通用」页按此枚举），未声明则永不出现在该配置项里；
    ② **兜底**：某个取值没给取值图标（``StateOption.icon is None``）或该状态已失联时，
       菜单栏那一格回落画它（完整回落规则见 ``AppShell._apply_tray_base``）。

    「准入门槛 = 有主图标」而不是一枚单独的「我愿不愿意」布尔：两个字段会产生
    「愿意却没图标可画」这种**无法渲染**的状态（用户选了它，菜单栏那一格无物可画）。
    谁最终上菜单栏仍由**用户**在框架设置里选——「组件只声明事实，框架决定呈现」。

    同一组件声明**多台**状态机时，**声明顺序就是作者写的关注度顺序**：代表态先比
    严重度（越坏越优先），同级取**先声明**的那台（判据落在
    ``shell/renderers/representative.py``）。想把哪台机优先显示，就把它写在最前 ——
    不再有排序字段：多一个数字，读的人无从判断该填几。

    这个顺序是**默认值与兜底值**，不是死规矩：用户可在设置窗给某台机调顺序（拖动行首手柄，
    与「插件」页 / 规则列表同一个排序控件 —— 2026-09-21 起），覆盖落在 ``plugins.<id>.states.<key>.priority``，**不进本类** —— 静态声明保持
    「只有顺序、没有数字」（``test_statespec_is_static_only`` 继续绿）。用户调回作者
    写的顺序时那个键会被删掉，作者日后增删/调整声明顺序照样传得到用户手上。
    """

    point: str = field(default="state", init=False)
    # 全部取值（决议 D13）。声明期校验重名 —— 报告靠取值名索引，重名会让它指错格子。
    states: "tuple[StateOption, ...]" = ()
    # 状态机主图标：准入判据与兜底图标是同一枚，见类 docstring。
    # None = 未声明 ⇒ 不进菜单栏主图标候选（想被选就给图标；不给就是「不参与」）。
    icon: "IconName | None" = None
    # 状态名（tooltip / 无障碍 / 菜单栏来源候选的标签）。**同一组件只有一台状态机时
    # 省略**——省略即取 ``Plugin.name``（收集期框架补上）；同一组件有多台状态机时
    # 各自显式命名（此时名字不再等于组件名，填它才有信息量，决议 D14）。
    title: str = ""
    initial: str = ""                               # 首屏取值名；留空 → states 的第一个
    stale_after: "float | None" = None              # 秒；超时未更新 → 代表态判 err

    def __post_init__(self) -> None:
        """声明期校验（宁可在注册时报错，不要等到界面上才发现）。

        - 取值名为空 / 重复 → 报错：``StateReport.state`` 靠它索引，重名即静默指错格子；
        - ``initial`` 不在声明的取值里 → 报错：拼错一个字母，首屏就落在空格子上，
          而运行期看只剩下「没有这个取值」这一句无信息量的话。
        """
        ids = [opt.id for opt in self.states]
        if any(not i for i in ids):
            raise ValueError(f"StateSpec({self.key or 'main'})：取值名不能为空")
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(
                f"StateSpec({self.key or 'main'})：取值名重复 {dupes} —— "
                "报告靠取值名索引，重名会让它指错格子"
            )
        if self.initial and self.initial not in ids:
            raise ValueError(
                f"StateSpec({self.key or 'main'})：initial={self.initial!r} 不在声明的取值里 "
                f"{ids} —— 首屏会落在空格子上"
            )

    def option(self, state: str) -> "StateOption | None":
        """按取值名取回声明；未声明返回 ``None``。

        返回 None 是**组件写错**的信号（报了没列举的取值），不是正常分支：
        框架据此按 ERR 露出，见 ``shell/renderers/state_view.resolve``。
        """
        for opt in self.states:
            if opt.id == state:
                return opt
        return None

    @property
    def initial_value(self) -> str:
        """首屏取值名（框架 ``_paint_initial_states`` 用）：显式 ``initial`` 优先，
        留空取**第一个**声明的取值（声明顺序即关注度顺序，第一个就是作者最想先说的）。"""
        if self.initial:
            return self.initial
        return self.states[0].id if self.states else ""

    @property
    def state_id(self) -> str:
        """状态机**身份**（配置用）：``{plugin_id}.{key or 'main'}``。

        与 ``topic`` 同源、同保留键，但**不含事件总线前缀** —— ``tray.base_source``
        指向的是「某个组件的某台状态机」这个身份，不是总线上的地址，故配置项
        与 pub/sub 层解耦。

        保留键取 ``"main"``（与 ``topic`` / ``Plugin.status_topic()`` 一致），
        **不用** ``Contribution.identity()`` 的 ``"default"``：同一台状态机
        必须只有一个名字，否则配置键与状态流会各认一套。
        """
        return f"{self.plugin_id}.{self.key or 'main'}"

    @property
    def topic(self) -> str:
        """事件 topic 由 identity 派生，与框架自动 publish 同源。

        兜底键 ``"main"`` 与 ``Plugin.status_topic()`` 取同一个保留键（理由见该方法，
        此处是它的镜像）；未显式声明 ``key`` 的状态机靠这个兜底才能收到框架快照。
        """
        return f"plugin.{self.plugin_id}.status.{self.key or 'main'}"
