"""todo：待办提醒（合并方案里那个「只有一个组件、一种条目」的组件）。

一条**条目**按需要开三样东西（``docs/plans/2026-10-08-翻页钟与待办合并.md`` §1.2），
任意几样都行：**问面**（条件成立弹一次询问）、**显示面**（在自己那段时间里挂在浮层
上）、**钟面**（浮层里那面钟显示什么）。问面与显示面是**两条独立的线** —— 问由框架的
询问中枢负责（``core/interaction.py`` ＋ ``shell/interaction.py``），显示由本组件负责。

设计要点（改之前先读）：

- **规则形状在代码、条件参数在配置**：条目表（``items``）是配置数据，一条条目在
  ``_sync_interaction()`` 里被翻成一条 ``TriggerSpec``。求值 / 去重 / 排队 / 弹窗全归中枢，
  本组件不碰一个窗口。**三件事都由条目自己推导、不给用户选**：什么时候问
  （:func:`_conditions_of`）、问法（``ask``，:func:`_ask_of`）、去重档（:func:`_dedupe_of`）。
  **新模型（``tasks``）一条规则都不登**（2026-10-11）：任务那一档的「到点弹卡」整条退场，
  显示改由组件自己的节拍算（``tick()`` 算浮层、:meth:`Plugin._fire_due_commands` 判命令
  的边沿跨越）；「什么时候问」在新模型里归**变量**，不归任务。
- **去重档由问法推导，不再是一条写死的常量**（合并方案 §2.5）：问 ``choice``（弹一个
  「完成」按钮）的取 ``once_per_day`` —— **今天没答 ＝ 这件事今天就算过了**，一天问五遍
  同一件没做的事只是骚扰；问 ``time``（填一个时刻，如「今天几点上班」）的取
  ``until_answered_day`` —— 答出来的值还要被显示面读，**答不出来就没有可用值**，必须
  不屈不挠。这仍是两个用例的**真实差异**，只是现在由问法替用户把它判掉。
- **``ask`` 键「不在」与「在且为空串」是两回事**（本仓的无值两态口径）：键**不在** ＝
  旧条目，按 ``choice`` 走（行为保持）；键在且为**空串** ＝ 这一条不问（只开显示面的条目）。
- **显示面是纯函数算出来的**（``scheduler.compute_display``）：本模块只负责喂它四样东西
  —— 条目表、此刻、**当日值**（内存那份）、**此刻的场景名** —— 再把结果写进
  ``self._display`` 交主线程的 ``ClockController`` 增删浮层。**本模块不认识 Qt**。
- **一拍 60 秒**（2026-10-11 由 1 秒改）：待办的时间量**全是分钟粒度**（提醒时刻 / 提前
  登场 / 延后退出），而浮层的秒针由 ``clock_view.FlipClock`` 自带定时器走 ⇒ 秒级轮询没有
  消费者（``scheduler._inside_window`` 与这里同步按分钟判）。当日值却只在三个时机真变
  （自己写、外部改、跨天）⇒ 进内存，按 :data:`DAY_VALUE_RECHECK_S` 每 60 秒从盘上复核一次。
- **完成记录是数据不是配置**：落 ``agenda_log.json``（进 ``resources()``），**不进配置段**
  —— 每天写一次用户配置会触发 ``config.changed`` 广播，还会被「恢复默认配置」冲掉。
  读法收在 ``day_log.py``（合并前 ``clock`` / ``todo`` 各写一份同形状的文件，方案 §6.2
  并成了一份）。条目表也是配置，但条目的**当日值**不是 —— 两样东西别混。
- **合并前那套东西在 ``start()`` 里搬一次**（:meth:`_migrate_legacy`）：旧 ``plugins.clock``
  段并进来、两份旧数据文件并成一份。纯映射在 ``migrate.py``，读写在本模块。
- **本模块不 import PySide6**（门禁 ``test_plugin_py_has_no_pyside6`` 按 AST 判定）：
  设置面在 ``settings.py``、浮层在 ``clock_view.py``，两处都延迟到用的时候才导入。
- **零直连 ``events.publish``**：状态只经 ``tick()`` 的返回值与
  ``publish_snapshot()`` 两条既有的路出去。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from barkit.contribution.specs import (
    ComponentSpec,
    DETAIL_PRESENTATION_WINDOW,
    DetailEntrySpec,
    IconName,
    SettingsPageSpec,
    StateLevel,
    StateOption,
    StateReport,
    StateSpec,
    StatusRowSpec,
)
from barkit.core.events import ROUTE_PROFILE_TOPIC
from barkit.core.interaction import (
    COND_DAILY_AT,
    COND_ON_START,
    COND_STATE_IS,
    DEDUPE_ANSWERED_DAY,
    DEDUPE_DAY,
    PROMPT_CHOICE,
    PROMPT_TIME,
    RESULT_ANSWERED,
    Condition,
    InteractionResult,
    PromptSpec,
    TriggerSpec,
)
from barkit.core.plugin import RUN_PERIODIC, Plugin
from barkit.core.resources import PluginResources
from barkit.core.schema import FieldSpec, FieldType, FormSchema
from barkit.core.variables import rules
from barkit.plugins.todo import command as cmd_mod
from barkit.plugins.todo import day_log, migrate
from barkit.plugins.todo import task as task_mod
from barkit.plugins.todo.scheduler import (
    ACTION_DONE,
    ACTION_SNOOZE,
    DEFAULT_MAX_ROWS,
    FALLBACK_TITLE,
    _ask_of,
    _hhmm_to_sec,
    _valid_hhmm,
    compute_display,
    preview_entry as _kernel_preview,
    preview_text_of,
    row_summary,
    row_time_label as _kernel_row_time_label,
)

log = logging.getLogger(__name__)

#: 浮层上那枚「做完了」的按钮文案。它是 ``PROMPT_CHOICE`` 的选项，也同时是
#: **答案的判定值**（``_on_result`` 拿它比对）—— 故只能有一份，别在两处各写一遍。
DONE_OPTION = "完成"

#: 浮层显示多久自动退场（秒）。与旧翻页钟那条取同一个量级：都长于「看见并点一下」。
PROMPT_TIMEOUT_S = 120

#: 「稍后」按钮的推迟量（分钟）。中枢据它记一个到期时刻，到点自己再弹一次；
#: 浮层那一行收起多久也用它（§3.3）。
LATER_MINUTES = 30

#: 每多少秒从盘上复核一次当日值（方案 §5.1）。**按时间而不是按拍数**（2026-10-11 改）：
#: 当初写「每 60 拍」是因为一拍恰好 1s，节拍一改（1s → 60s）它就悄悄变成一小时 ——
#: 而这个量的原意是「外部改了这份文件，最多一分钟就该被看见」，与框架拍长无关。
#: 当日值只在三个时机真变：自己写完立刻刷（:meth:`_write_day_value`）、日期变更重读、
#: 以及这个慢拍复核。其余每一拍**只读内存** —— 否则就是每拍读一次盘。
DAY_VALUE_RECHECK_S = 60.0

#: 取值闭集（决议 D13）。三档而不是两档：「一条待办都没有」与「有但没做完」是两件
#: 不同的事，挤成一档会让面板在刚装好时显示「还没做完」，那是**编出来的状态**。
#: 三档都不上 ``WARN`` / ``ERR``：待办没做完是常态，不是故障。
STATES: "tuple[StateOption, ...]" = (
    StateOption("empty", StateLevel.IDLE, text="还没有待办"),
    StateOption("pending", StateLevel.IDLE, text="今天的待办还没做完"),
    StateOption("done", StateLevel.OK, text="今天的待办都完成了"),
)


def _dedupe_of(ask: str) -> str:
    """去重档**由问法推导**，不给用户选（合并方案 §2.5）。

    - ``time`` ⇒ ``until_answered_day``：答案（如「今天几点上班」）还要被显示面读，
      **答不出来就没有可用值** ⇒ 关掉 / 超时不算答，下次启动还留一次机会；
    - ``choice`` ⇒ ``once_per_day``：答案只进完成记录 ⇒ 今天没答 ＝ 这件事今天过了。

    交出去做成用户可选的枚举，就是让用户替框架判一件框架自己知道答案的事。
    """
    return DEDUPE_ANSWERED_DAY if ask == PROMPT_TIME else DEDUPE_DAY


def _conditions_of(item: dict, ask: str) -> "tuple[Condition, ...] | None":
    """这一条「什么时候该问」（合并方案 §2.4）。

    :return: ``None`` ＝ **这一条登记不进去**。不是「没有条件」—— 空条件集的规则求值为真，
        下一次心跳立刻弹一次，那比不登记更糟（设置页看着还是好的）。

    两条规则，都是踩过的坑：

    - 问 ``choice`` 的必须有一个合法的 ``at``（到点问）；问 ``time`` 的**没有**「时刻」
      这个条件 —— 它本来就没有一个说得出的固定时刻，触发靠 ``on_start``；
    - 填了「只在某场景」⇒ 多一条 ``state_is``，并**必须一起带上 ``on_start``**：
      启动那一刻场景还是 ``unknown``，只判 ``state_is`` 的条件**永远触发不了**
      → `docs/plans/2026-09-24-条件触发询问机制.md` §2.1。
    """
    scene = str(item.get("scene") or "").strip()
    at = str(item.get("at") or "").strip()
    if ask != PROMPT_TIME and not _valid_hhmm(at):
        return None
    conditions: "list[Condition]" = []
    if ask == PROMPT_TIME or scene:
        # 事件型锁存位：`on_start` 只置一次，之后一直为真（见 Latches）。
        conditions.append(Condition(COND_ON_START))
    if ask != PROMPT_TIME:
        conditions.append(Condition(COND_DAILY_AT, at=at))
    if scene:
        # 场景名由用户在设置里填（**不硬编码 Office**），只读总线上的副标题，
        # 不 import 网络切换插件（插件之间只经总线）。
        conditions.append(
            Condition(COND_STATE_IS, topic=ROUTE_PROFILE_TOPIC, state="active", detail=scene)
        )
    return tuple(conditions)


@dataclass(frozen=True)
class _Count:
    """一条任务此刻的四个事实：生效没、完成没、过期没、以及**算不算「待办」**。

    面板那行的三个数从它来；主窗体那份清单也读它（``active`` 决定「今天」视图列不列
    它，``done`` / ``overdue`` 决定状态点）。**一处判定、两处消费** —— 分开写的话，
    同一时刻在面板与清单里会得出两个结论。

    ``tracked`` 是这条任务**要不要「完成」这个概念**（:attr:`TaskSpec.track_done`）。
    关掉的那些（整点时钟这类「只显示、做不掉」的）照旧进这份表 —— 主窗体清单要列它们
    —— 只是**不参与**「未完成 / 已完成」与「过期」两个判据。判定仍在
    :meth:`Plugin._count_rows` 一处，筛选由消费方各自带上 ``tracked``。
    """

    task_id: str
    active: bool
    done: bool
    overdue: bool
    tracked: bool = True


class Plugin(Plugin):
    id = "todo"
    name = "待办提醒"
    api_version = 3
    run_mode = RUN_PERIODIC
    #: 一拍 60 秒（2026-10-11 由 1 秒改）。**显示面不吃这个节拍**：浮层的秒针由
    #: ``clock_view.FlipClock`` 自己的 250ms 定时器走，本组件的 tick 只负责「哪些条目
    #: 在自己的时间里」这件事 —— 那全是**分钟粒度**的量（提醒 / 提前登场 / 延后退出），
    #: 见 ``scheduler._inside_window``。问询侧同样与它无关（中枢自己的 30s 心跳，方案 §5）。
    #: 一拍 60s 意味着「当日值不许每拍读盘」，见 :data:`DAY_VALUE_RECHECK_S`。
    suggested_poll = 60
    order = 50

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        # 「今天已完成的条目」（id → 完成时刻）。**worker 的 tick() 写、主线程读**
        # （面板那行的进度与详情页的条目清单都读它）：整体替换一个 dict 在 GIL 下是
        # 安全的，与 ``_display`` 同款。不让主线程读盘，是因为面板那行**每拍**都要读它。
        self._done: dict = {}
        # 今天当日值的**内存副本**（方案 §5.1）。不许每拍读盘：自己写完立刻刷
        # （:meth:`_write_day_value`）、跨天重读、每 :data:`DAY_VALUE_RECHECK_S` 秒从盘上
        # 复核一次；其余每一拍只读它。
        # ``_values is None`` ＝ 还没加载过（懒加载：测试里直接造 Plugin 也会自愈）。
        self._values: "dict | None" = None
        self._values_day: str = ""
        #: 下一次允许读盘复核当日值的**单调时刻**（0.0 ＝ 立刻）。按时间而不是按拍数：
        #: 拍长是可配置的，记「还剩几拍」会让这个原意（最多一分钟见一次外部改动）随节拍漂。
        self._recheck_at: float = 0.0
        # 「稍后 N 分钟」收起的条目：id → **单调时钟**的到点时刻。用单调时钟而不是
        # 墙钟，免掉系统对时 / 睡眠的跳变；这份状态只活在进程里 —— 重启之后这一条
        # 重新出现，那不是失败（「稍后」本来就是「先别烦我一会儿」）。
        self._snooze: "dict[str, float]" = {}
        # 此刻命中的网络场景名（空串 ＝ 没有命中 / 还不知道）。主线程的
        # ``on_system_event`` 写、worker 线程的 ``tick()`` 读；一分钟的单字符串替换在
        # GIL 下是安全的，与 ``_display`` 同款。
        self._scene: str = ""
        # 主线程控制器（持有浮层），在 start() 里懒加载（避免此模块导入 PySide6）。
        self._controller = None
        # tick()（worker 线程）写入、主线程控制器读取的整屏显示态。
        self._display: "dict | None" = None
        # 「预览这一条」（详情页编辑屏那枚键）：拿着一份**未落盘**的条目强制显示它的
        # 显示态，直到用户关掉浮层（``clock_view.ClockController._on_user_closed``）为止。
        # 与旧翻页钟的 ``_test`` 是同一件事 —— 但这里**没有**「常驻预览」那个全局
        # 开关（方案 §4.3 已删）：看一眼某一条就够了，没理由让整块浮层常驻。
        self._test: "dict | None" = None
        # 「配了新模型却没接变量池」那条日志只报一次（一拍一次会把日志刷满）。
        self._no_pool_warned: bool = False
        # 上一轮「生效条件判不出来」的任务 id（:meth:`_note_blanks`）：那条 INFO 同样
        # 只该在**判决翻转**时出声 —— 一拍一条会把日志刷满，而它是「它没出现」的唯一线索。
        self._blank_seen: "frozenset[str]" = frozenset()
        # 前几个触发点是**时刻**，判据是「从它前面跨到它上面」（:meth:`_fire_due_commands`）。
        # 这里记每一条任务**上一拍看到的分钟数**（``0..1439``）：没有记录 ⇒ 这一条是这一拍
        # 才出现的，只记基线不补跑（重启后把当天早已过去的那一刻补打一遍比晚一分钟糟）。
        self._cmd_last_m: "dict[str, int]" = {}

    # ---------- 配置（设置面的唯一入口；UI 不碰 Config，门禁 test_no_config_io_in_ui） ----------

    def config_schema(self) -> FormSchema:
        """条目表 ＋ 三格浮层配置；**默认值的真源在这里**，别处不再抄一遍（§9.5）。

        ``size`` / ``close_gesture`` 这两格在合并前是两个组件各一份的（取值域逐字
        相同）；批 5 旧组件退场后只剩这里一份，不再有第二处要跟着改。
        """
        return FormSchema(
            plugin_id=self.id,
            version=1,
            fields=(
                FieldSpec(key="items", label="待办条目", type=FieldType.LIST, default=[]),
                # 新模型的任务表（设计文档 §4.1）。与 ``items`` 的关系是「**有非空的旧表
                # 就旧表说话**，否则新模型」（见 ``uses_tasks``）—— 迁移跑完旧表就被
                # pop 掉，此后 ``items`` 段只剩「用户从没配过」这一种状态。两条路在
                # ``items()`` 那一处合流。
                FieldSpec(key="tasks", label="任务", type=FieldType.LIST, default=[]),
                FieldSpec(
                    key="size",
                    label="浮层大小",
                    type=FieldType.CHOICE,
                    default="medium",
                    choices=("small", "medium", "large"),
                ),
                FieldSpec(
                    key="close_gesture",
                    label="关闭方式",
                    type=FieldType.CHOICE,
                    default="either",
                    choices=("right_click", "double_click", "either"),
                ),
                FieldSpec(
                    key="max_rows",
                    label="最多显示几条",
                    type=FieldType.NUMBER,
                    default=DEFAULT_MAX_ROWS,
                ),
            ),
        )

    def items(self) -> "list[dict]":
        """当前条目表（内核认的那一份）—— **新模型优先，旧表兜底**。

        这是**唯一**的分岔点：浮层（``compute_display``）、详情页、计数都读它。分岔写在
        调用方那一层的话，每一处都得自己判一遍，「新旧两套模型」的分辨就散了 —— 而漏判
        的那一处不会报错，它只是静默地按旧模型跑。

        分阶段：Phase 4 把旧 ``items`` 迁成 ``tasks`` ＋ ``variables`` 之后，下面那一支
        就没人走了（连同 ``_ask_of`` / ``_dedupe_of`` 那一族一起删）。
        """
        if self.uses_tasks():
            return self._task_items()
        return list(self.cfg.value("items"))

    def tasks(self) -> "list[dict]":
        """``tasks:`` 段的**原始声明**（未解析；解析在 :meth:`task_specs`）。"""
        return list(self.cfg.value("tasks"))

    def task_specs(self):
        """解析 ``tasks`` ⇒ ``(声明, 跳过原因)``。宽容：一条写坏的只让这一条不生效。"""
        return task_mod.parse_all(self.tasks())

    def uses_tasks(self) -> bool:
        """这条盘走新模型吗：**有非空的旧表 ⇒ 旧表是权威；否则新模型**。

        ``cfg.value`` 对「键不在」与「键在且空」给同一份默认值（本仓既有口径：
        ``cfg.set`` 等于默认值就删键 ⇒ 两者在盘上同形），故判据只能用「**非空**」这个量：

        - 旧表非空（用户还没迁，或刚从旧版升上来）⇒ **旧模型**，一个字都不许让 ``tasks``
          抢走 —— 那是他此刻真正在用的那张表（:meth:`_migrate_to_tasks` 会在 ``start()``
          里把它迁成新模型，迁完旧表就被 pop 掉）；
        - 旧表空、新表非空 ⇒ 新模型；
        - **两张都空 ⇒ 新模型**。

        最后那一格是 2026-10-10（Phase 5）改的。原判据是「``tasks`` 非空才算新模型」，
        两个后果都不对，而且都是**用户看得见**的：① **全新装出来的盘**永远进不了新模型
        （他从没用过旧表 ⇒ 迁移没东西可迁 ⇒ 判据恒假），第一个「＋ 新增」还得用旧十三格
        那张表，而下一次启动又被迁移换成新模型；② **把最后一条任务删掉**会让判据翻假 ⇒
        「＋ 新增」当场换回旧形状（同一个按钮两次按出两种表单，只因为中间删空过）。
        改成「没有旧表就是新模型」之后这两件事都没有了。
        """
        return bool(self.cfg.value("tasks")) or not self.cfg.value("items")

    def _pool(self):
        """变量池（框架注入）。``None`` ＝ 未接线（测试桩 / 单独起的组件）。"""
        return getattr(self.ctx, "variables", None)

    def _task_items(self) -> "list[dict]":
        """新模型：任务声明 ＋ 此刻的变量池 → 内核认的条目表。

        **一轮一个世界**：先 ``begin_pass()``（清池缓存、取一次时钟）再逐条换算 ——
        不这么做的话，一轮里前后两条任务可能拿到两个不同的「现在」，而跨过秒边界那条
        的表现是「它的时刻偶尔差一秒」（查起来毫无线索）。
        """
        pool = self._pool()
        if pool is None:
            # 配了新模型但框架没接变量池 ⇒ 这几条任务**算不出来**（空表）。这是装配
            # 问题，不是用户配错了，所以出声；但一拍一次会把日志刷满，故只报第一拍。
            if not self._no_pool_warned:
                self._no_pool_warned = True
                log.warning("tasks 段已配置，但变量池未接线 —— 这些任务算不出来（装配问题）")
            return []
        now = datetime.now()
        pool.begin_pass()
        # 生效条件判不出来的那几条先收进一个集合，本轮结束后**只报新出现的**（见
        # ``_note_blanks``）。它是周期型：条件每拍重判一遍，而「判不出来」往往是长期
        # 状态（依赖的变量整天没值）⇒ 不去重的话那条 INFO 按拍刷屏 —— 而它又是
        # 「用户在界面上只看到它没出现」的唯一线索，不能删。
        blanks: "set[str]" = set()
        items = [
            task_mod.resolve_item(spec, pool, now, on_blank=blanks.add)
            for spec in self.task_specs()[0]
        ]
        self._note_blanks(blanks)
        return items

    def _note_blanks(self, blanks: "set[str]") -> None:
        """本轮「生效条件判不出来」的任务 id —— **只对判决翻转的出声**。

        判据是「不在上一轮那个集合里」：条件恢复可判、或任务被删掉，它自然从集合里
        消失，下次再判不出来时会重新说一次；本来好、这轮坏了的同样说话。中间那些
        「一直判不出来」的轮次保持安静 —— 那正是原来刷屏的那一段。

        组件每拍都会走一遍 :meth:`items`（``done_today`` 与 ``_compute_display`` 各一次），
        而一轮里第一次调用就已经把 ``_blank_seen`` 更新掉了 ⇒ 同拍第二次不会再报。
        """
        for task_id in sorted(blanks - self._blank_seen):
            task_mod.report_blank(task_id)
        self._blank_seen = frozenset(blanks)

    def get_size(self) -> str:
        """浮层尺寸档（三档之一；取值域归 ``config_schema()`` 的 ``choices``）。"""
        return str(self.cfg.value("size"))

    def get_close_gesture(self) -> str:
        """浮层的关闭方式（双击 / 右键 / 两者皆可）。"""
        return str(self.cfg.value("close_gesture"))

    def get_max_rows(self) -> int:
        """浮层最多显示几行；``<= 0`` ＝ 不限（用户把那一格填 0 就是「别截断」）。"""
        return int(self.cfg.value("max_rows"))

    # ---------- 详情页列表行那两句话（视图只消费，口径留在这里） ----------

    def row_time_label(self, item: dict, now: "datetime | None" = None) -> str:
        """详情页列表行右端那个时刻（算法在 :func:`scheduler.row_time_label`）。

        **为什么归插件**：它要读「今天答了没有」（当日值那笔账），而那笔账只有本组件
        知道从哪来（内存那份 / 什么时候该重读盘）。视图侧一律不碰数据文件（门禁
        ``test_no_config_io_in_ui``），故它只报「我要哪一条的时刻」，取值由这里算。
        """
        moment = now or datetime.now()
        return _kernel_row_time_label(item, moment, self.day_values(moment))

    def row_summary(self, item: dict) -> str:
        """详情页列表行第二行那句话（算法在 :func:`scheduler.row_summary`）。

        问法的判据由本方法喂（``_ask_of``：**键不在的老条目按 ``choice`` 走**那条无值
        两态口径的家在这里）—— 内核那侧只负责拼句子，不重判一遍。
        """
        return row_summary(item, _ask_of(item))

    def set_size(self, size: str) -> bool:
        """写浮层大小（设置页用）。取值域归 ``config_schema()`` 的 ``choices``。

        越界值 ⇒ ``cfg`` 抛，这里转成 ``False``（设置页的既有契约：改没改成看返回值，
        不靠异常分叉）。
        """
        try:
            self.cfg.set("size", size)
        except Exception as e:  # noqa: BLE001 —— 写盘失败要变成 False，不让设置页崩
            log.error("写回 todo size 失败：%s", e)
            return False
        # 尺寸改的是浮层**此刻**的大小，用户改完多半马上回头看 ⇒ 立刻重刷，不等下一拍。
        self._refresh_now()
        return True

    def set_close_gesture(self, gesture: str) -> bool:
        """写关闭方式（设置页用）；取值域归 ``config_schema()`` 的 ``choices``。

        **不立刻刷浮层**（与 ``set_size`` / ``set_max_rows`` 不同）：关闭方式只在
        **下一次建浮层**那一刻被读走（``ClockController.on_display`` 里建 ``ClockOverlay``
        时），刷一次改不了已经站着的那个浮层 —— 刷了等于假装生效。
        """
        try:
            self.cfg.set("close_gesture", gesture)
        except Exception as e:  # noqa: BLE001
            log.error("写回 todo close_gesture 失败：%s", e)
            return False
        return True

    def set_max_rows(self, rows: int) -> bool:
        """写浮层行数上限（设置页用）。``<= 0`` ＝ 不限（口径同 :meth:`get_max_rows`）。"""
        try:
            self.cfg.set("max_rows", int(rows))
        except Exception as e:  # noqa: BLE001
            log.error("写回 todo max_rows 失败：%s", e)
            return False
        # 与 ``set_size`` 同理：改的是浮层此刻那几行，立刻重刷。
        self._refresh_now()
        return True

    def save_items(self, items: "list[dict]") -> bool:
        """整表写回**旧表**；返回 ``False`` ＝ 没写进去（怎么说话由调用方决定，插件不弹框）。

        **闸的判据是「写了会不会被读」**，不是「此刻走哪条路」：旧表只在 ``tasks`` 为空时
        才是权威（见 :meth:`uses_tasks` 那条「有非空旧表 ⇒ 旧模型」），故 ``tasks`` 非空
        时这次写回**一定不会被读** —— 那就拒掉。写进去的改动静默消失（界面显示「保存了」、
        下次打开还是旧的）比报一句「没写进去」糟得多。

        反过来，``tasks`` 为空时**一律放行**，哪怕此刻判的是新模型（全新装出来的盘就是
        这一格）：写完之后旧表非空 ⇒ 判据当场翻成旧模型，这次写的字立刻就是权威那份。
        若照着 :meth:`uses_tasks` 的值拒，全新的盘上就再也写不出第一张旧表了。
        """
        if self.cfg.value("tasks"):
            log.error("tasks 已有内容，items 不是权威表 —— 这次写回被拒绝")
            return False
        try:
            self.cfg.set("items", list(items))
        except Exception as e:  # noqa: BLE001 —— 写盘失败要变成 False，不让设置页崩
            log.error("写回 todo items 失败：%s", e)
            return False
        # 条目变了 ⇒ 规则跟着变（新增 / 删除 / 改时间 / 换启用都得重登记）。
        self._sync_interaction()
        # 内存那份「今天完成几个」立刻跟上，否则面板那行要等下一拍。
        self._done = self.done_today()
        self.publish_snapshot()
        # 显示面也立刻跟上：刚改完的那一条可能正好在自己的时间里（浮层不该等下一拍）。
        self._refresh_now()
        return True

    def save_tasks(self, tasks: "list[dict]") -> bool:
        """整表写回**任务表**（新模型）；``False`` ＝ 没写进去（与 :meth:`save_items` 互为反面）。

        闸的判据与那一头**同一个**：**写了会不会被读**。旧表非空时它是权威（见
        :meth:`uses_tasks`），这时写 ``tasks`` 一个字都不会被读 —— 用户在新表单里改
        半天，改的是一份没人看的段，而界面照旧显示「保存了」。那就拒掉。

        ``items`` 为空时一律放行；写完之后 :meth:`uses_tasks` 判新模型
        （``bool(tasks)`` 那一半为真），这次写的字立刻就是权威那份。
        """
        if self.cfg.value("items"):
            log.error("旧表 items 还有内容（旧表是权威），tasks 这次写回被拒绝")
            return False
        try:
            self.cfg.set("tasks", [dict(t) for t in tasks])
        except Exception as e:  # noqa: BLE001 —— 写盘失败要变成 False，不让编辑屏崩
            log.error("写回 todo tasks 失败：%s", e)
            return False
        # 任务变了 ⇒ 规则跟着变（新增 / 删除 / 改提醒时刻 / 换启用都得重登记）。
        self._sync_interaction()
        # 内存那份「今天完成几个」立刻跟上，否则面板那行要等下一拍。
        self._done = self.done_today()
        self.publish_snapshot()
        # 显示面也立刻跟上：刚改完的这条可能正好在自己的时间里（浮层不该等下一拍）。
        self._refresh_now()
        return True

    def declared_variables(self) -> "list[dict]":
        """顶层 ``variables:`` 段的**声明**（编辑屏那两个「引用变量」下拉读它）。

        取声明不取值：下拉要回答的是「**可以引用谁**」，而每条的值由主窗体那条变量条
        显示。没接线（测试桩 / 无配置源）⇒ 空表，下拉里就只剩「写死一个时刻」这一项。
        """
        config = getattr(self.ctx, "config", None)
        reader = getattr(config, "variables", None)
        return list(reader()) if callable(reader) else []

    # ---------- 当日完成记录（数据文件；**不进配置段**） ----------

    def resources(self) -> PluginResources:
        """声明数据文件：卸载与「导出 / 导入用户数据」按这份清单走（§9.1）。

        只有合并后那一份 —— 两份旧文件（``clock_workday.json`` / ``todo_log.json``）
        不在这里：它们**只在迁移那一次**被读到，读完就并进这一份并删掉。写进清单会让
        「导出用户数据」把一个已经不存在的文件也列进去，还会让卸载流程去删一个别人
        早就删掉的名字。
        """
        return PluginResources(data_files=[day_log.AGENDA_FILENAME])

    @staticmethod
    def _load_log() -> "dict[str, dict]":
        """这份账**盘上那一份**（读法收在 ``day_log`` 里，本类只认「读」「写」两个动作）。"""
        return day_log.read_days(day_log.AGENDA_FILENAME)

    @staticmethod
    def _save_log(days: dict) -> bool:
        return day_log.write_days(day_log.AGENDA_FILENAME, days)

    @staticmethod
    def _today_key(now: "datetime | None" = None) -> str:
        return (now or datetime.now()).date().isoformat()

    def _today_values(self, force: bool = False) -> "dict[str, str]":
        """今天当日值的**内存那份**（方案 §5.1）—— 不许每拍读盘。

        重读的三个时机：``force``（写完立刻刷）、**日期变了**（跨天归零靠这一问）、
        以及 ``_recheck_at`` 到期（防外部改动 —— 用户在别处改了这份文件）。其余
        每一拍只把它原样交出去。

        **返回的是内部那一份，别改它**；要改走 :meth:`_write_day_value` 这个唯一写口。
        """
        today = self._today_key()
        stale = (
            force
            or self._values is None
            or self._values_day != today
            or time.monotonic() >= self._recheck_at
        )
        if stale:
            day = self._load_log().get(today)
            self._values = (
                {k: v for k, v in day.items() if isinstance(v, str)}
                if isinstance(day, dict)
                else {}
            )
            self._values_day = today
            self._recheck_at = time.monotonic() + DAY_VALUE_RECHECK_S
        return self._values if self._values is not None else {}

    def day_values(self, now: "datetime | None" = None) -> "dict[str, str]":
        """今天的当日值表（条目 id → ``"HH:MM"``），**按天归零**。

        它同时装两样东西 —— 因为两样的形状本来就一样（``{日期: {条目 id: "HH:MM"}}``）：
        问 ``choice`` 的条目存的是**完成时刻**，问 ``time`` 的条目存的是**答案本身**
        （如「今天几点上班」）。哪一条属于哪一样由条目自己的 ``ask`` 决定（见
        :meth:`done_today`），存储不必分两份。

        ``now`` 传别的日子时读盘那一份（问「昨天」是小众路径，不值得为它占内存）；
        传 ``None``（默认）或今天则给内存那份 —— **热路（tick / 面板）走的就是它**。
        """
        if now is not None and self._today_key(now) != self._today_key():
            day = self._load_log().get(self._today_key(now))
            if not isinstance(day, dict):
                return {}
            return {k: v for k, v in day.items() if isinstance(v, str)}
        return dict(self._today_values())

    def done_today(self, now: "datetime | None" = None) -> "dict[str, str]":
        """今天**已完成**的条目（id → 完成时刻）。**昨天的记录不算** —— 与「哪天完成」
        这个事实对齐：跨天归零靠的就是这一问，而不是靠去重账（账管弹窗，不管面板）。

        **按问法筛只对旧模型成立**：新模型的 agenda_log 里只剩「完成」一种语义（ask 的
        答案归变量池了），故新模型下全部任务都可能是它的键 —— 再按 ``ask`` 筛会把
        「只列不弹」的那些任务的完成记录整个丢掉（面板上它们永远做不完）。
        """
        values = self.day_values(now)
        if self.uses_tasks():
            ids = (str(i.get("id") or "") for i in self.items())
        else:
            ids = (str(i.get("id") or "") for i in self._countable_items())
        return {iid: values[iid] for iid in ids if iid in values}

    def day_value_of(self, item_id: str, now: "datetime | None" = None) -> str:
        """这一条今天的当日值；**空串 ＝ 今天还没有值**（空串 ≠ 00:00，别当时间用）。"""
        return self.day_values(now).get(item_id, "")

    def done_at(self, item_id: str, now: "datetime | None" = None) -> str:
        """今天这一条几点完成的；**空串 ＝ 今天还没完成**（不是 00:00）。"""
        return self.done_today(now).get(item_id, "")

    def set_done(self, item_id: str, at: "str | None" = None) -> bool:
        """标记 / 撤销今天的完成。``at`` 留空 ＝ 记当前时刻；显式传空串 ＝ 撤销。

        **这一条是「拉」的那条路**（用户在设置页自己勾）：不查去重账，永远可写 ——
        去重账挡的是弹窗，不挡用户自己动手（方案 §2.2b）。

        **命令挂在这个写口上**（唯一一处）：写盘成功且写的是「完成」而不是撤销时，
        按 ``command_at == done`` 跑一次。挂在这里而不是挂在各个调用点 —— 浮层行那枚
        「完成」、编辑屏的「标记今天完成」、旧卡那条答案三条路都汇到这儿，挂调用点等于
        给同一件事写三份（漏一份的症状是「按钮 A 会跑、按钮 B 不会」）。

        **「不需要完成」的条目（``track_done=False``）拒在这里**：它压根没有「完成」这
        回事（不进那两个数、不画状态点），记一笔只会是一格空账 —— 上游三处已经不画入口，
        这道闸是兜底（撤销也一并拒，它同样不该有账）。
        """
        spec = self._spec_of(item_id)
        if spec is not None and not spec.track_done:
            return False
        value = datetime.now().strftime("%H:%M") if at is None else at
        if not self._write_day_value(item_id, value):
            return False
        if value:
            self._fire_command(item_id, task_mod.COMMAND_AT_DONE)
        return True

    def set_day_value(self, item_id: str, value: str) -> bool:
        """写今天的**当日值**（问 ``time`` 型条目收回来的答案）。空串 ＝ 清除。

        与 :meth:`set_done` 同一份存储、同一个写口 —— 形状本来就一样，分成两份的结果
        是「同一个事实两个写方」。答案收回来的那一刻就该落盘，否则填了等于没填。
        """
        return self._write_day_value(item_id, value)

    def _write_day_value(self, item_id: str, value: str) -> bool:
        """按天记账的唯一写口：完成时刻与当日值共用一份存储，形状也共用。"""
        if not item_id:
            return False
        days = self._load_log()
        today = self._today_key()
        day = days.get(today)
        day = dict(day) if isinstance(day, dict) else {}
        if value:
            day[item_id] = value
        else:
            day.pop(item_id, None)
        if day:
            days[today] = day
        else:
            days.pop(today, None)
        if not self._save_log(days):
            return False
        # 内存那份立刻跟上（面板行 / 整行可点 / 显示面都读它），再广播一次不等下一拍。
        self._values = {k: v for k, v in day.items() if isinstance(v, str)}
        self._values_day = today
        self._done = self.done_today()
        self.publish_snapshot()
        # 显示面也立刻刷（§5.1 第一个时机）：勾了「完成」那一条当晚就该从浮层上消失，
        # 等下一拍（1 秒）虽然也能到，但那条路径是「碰巧」——重排的触发点不止一个。
        self._refresh_now()
        # 记上了就算「今天答过了」：当天到点不应再问同一件事（用户自己在设置页勾的那次
        # 也算，那是「拉」的那条路 —— 去重账只挡弹窗、不挡用户动手，方案 §2.2b）。
        # 不补这一笔就只有真弹过才记账，与旧翻页钟的 set_today_work_start 同口径。
        #
        # **新模型不记**（2026-10-11）：它一条规则都没登（见 :meth:`_sync_interaction`）
        # ⇒ 写了也没人读，只让 ``interaction_state.json`` 随每条任务常年长下去；id 被复用
        # 时新任务还会继承旧任务那本没人读的账（哪一天弹卡那条链路回来，症状就是「新建的
        # 那条当天不弹」）。判据与登记那一侧**同一条**：这一支走哪本书由盘此刻走哪个模型
        # 决定，而新模型此刻没有书。
        center = getattr(self.ctx, "interaction", None)
        if center is not None and not self.uses_tasks():
            center.note_answered(self._dedupe_key(item_id))
        return True

    # ---------- 规则登记（问面那条线） ----------

    def _sync_interaction(self) -> None:
        """按当前规则重登记（改配置 / 重新启用 / 变量变了之后重来一遍）。

        **先整批撤再整批登**：条目删掉之后不该留下孤儿规则 —— 它的答案已经没人接，
        而框架那一侧还在每天弹一次。

        **新模型一条规则都不登**（2026-10-11）：任务的「到点弹卡」整档退场（见
        ``task.Reminder``）⇒ 它不再需要询问中枢的任何一条规则 —— 显示由组件自己的节拍
        算，命令也由同一个节拍判边（:meth:`_fire_due_commands`）。这里只剩**旧模型**
        那一支：问 ``choice`` 的条目到点弹一枚「完成」（``daily_at`` ＋ ``once_per_day``），
        三件事由条目自己推导；旧表在 ``start()`` 里被迁成 ``tasks`` 之后，这一支就没人走了。
        """
        center = getattr(self.ctx, "interaction", None)
        if center is None:
            return  # 未接线（测试桩 / 无中枢）：规则登记退化为空操作
        center.unregister(self.id)
        if not self.uses_tasks():
            self._register_item_rules(center)

    def _register_item_rules(self, center) -> None:
        """旧模型那一批规则（一字未动，Phase 4 迁移后随旧条目表一起删）。"""
        for item in self.items():
            if not item.get("enabled", True):
                continue
            iid = str(item.get("id") or "")
            if not iid:
                continue
            ask = _ask_of(item)
            if not ask:
                continue  # 这一条不问（只开显示面的条目）
            conditions = _conditions_of(item, ask)
            if conditions is None:
                continue  # 登记了也永远触发不了的那种：挡在登记之前
            center.register(
                TriggerSpec(
                    plugin_id=self.id,
                    prompt=self._prompt_spec(item),
                    conditions=conditions,
                    dedupe=_dedupe_of(ask),
                ),
                lambda result, _iid=iid: self._on_result(_iid, result),
            )

    def _dedupe_key(self, item_id: str) -> str:
        """这一条在去重账里的 key；**规则登记与写口共用这一份形状**。

        形状曾经被两处各抄一遍（登记处按模型写 ``todo.task.*`` / ``todo.item.*``、
        :meth:`_write_day_value` 恒定写 ``todo.item.*``），于是新模型下「用户自己在清单
        里勾完成」记的那一笔落在了**没有规则注册**的那个 key 上 —— 到点那张「完成」卡
        当天照旧弹一次，而那条影子刚被他自己划掉。写口想避免的正是这件事（见它的注释），
        错的是笔名。

        故收成一处：**写哪一本书由这张盘此刻走哪个模型决定**，判定也只有这一处。
        """
        return f"{'todo.task' if self.uses_tasks() else 'todo.item'}.{item_id}"

    # ---------- 命令：四个触发点（2026-10-11，取代原先「卡登场那一刻」） ----------

    def _fire_due_commands(self, now: datetime) -> None:
        """一拍一次：把**刚跨过**那个触发点的命令跑掉（见 :meth:`_fire_command`）。

        判据是**边沿跨越**，不是「此刻到点了没有」—— 拿这一刻的分钟数与上一拍记下的比，
        只有**真的从它前面跨到它上面**才算。这一条取舍有三个方向，各自挡住一类事故：

        - **重启后不补跑**：第一拍没有「上一拍」，只记基线 ⇒ 当天早已过去的那一刻不会
          被补打一遍（一条 webhook 重复打两次，比晚一分钟糟得多）。
        - **睡过头不漏掉**：跨了两分钟也照跑 —— 只要那一刻还没被跨过去，醒来后第一拍
          就把它补上。
        - **这一刻不生效的不参与**：用户停用 / 变量条件不满足 / 不在生效时段内，三者任一
          成立就跳过，**并把基线一并清掉** ⇒ 它重新生效那一刻从头开始数。判据直接借
          :func:`task.resolve_item` 的 ``enabled`` 那一格（它就是这三样的合取），不在这里
          另立一套：一条「只在办公室」的任务不该在你回家之后照旧把那个请求打出去，而
          「生效时段」说的也正是「这段时间之外它不算数」。**这一条是接回来的**——原先命令
          挂在中枢那张卡上，条件不成立卡就不弹、命令自然不跑；卡片退场之后这道门得有人
          替它站。

        代价是**错过不补**：机器在那一刻关着、或那一刻不生效，这一天的命令就不跑了。
        「到点执行」说的就是「那一刻执行」，六小时后再补一个请求并不是用户要的（与「浮层
        绝不事后补弹」同一条取舍）。循环任务（每 N 分钟）每一轮都会跨一次 —— 那是它自己的
        节拍，与「每天一次」是两回事。
        """
        specs = self.task_specs()[0]
        live = {spec.id for spec in specs}
        for stale in self._cmd_last_m.keys() - live:
            # 删掉的任务留下一个旧基线：id 被后来那条复用时会拿**很久以前**的分钟数
            # 去算跨越，凭空多跑一次。
            self._cmd_last_m.pop(stale, None)
        pool = self._pool()
        if pool is None:
            return
        # 这一拍真的生效的是谁（见 docstring 第三条）。**自己走一遍**、不复用 ``tick()``
        # 后面那两次 ``items()``：那两次发生在这一句之后，而边沿判据要的正是「跨过去
        # 那一刻生不生效」—— 拿晚一拍的事实去决定早一拍该不该跑，错了也不会有症状。
        working = {item["id"] for item in self._task_items() if item["enabled"]}
        pool.begin_pass()
        now_m = now.hour * 60 + now.minute
        for spec in specs:
            if (
                not spec.command
                or spec.command_at == task_mod.COMMAND_AT_DONE
                or spec.id not in working
            ):
                # 「点完成」那一档不是时刻，没有可跨的边；没配命令 / 这一刻不生效同理 ——
                # 一起丢掉基线，好让它重新生效那一刻从头开始数。
                self._cmd_last_m.pop(spec.id, None)
                continue
            due_s = task_mod.command_due_s(spec, pool)
            prev = self._cmd_last_m.get(spec.id)
            self._cmd_last_m[spec.id] = now_m
            if due_s is None or prev is None:
                continue
            step = (now_m - prev) % 1440
            pos = (due_s // 60 - prev) % 1440
            if 0 < pos <= step:
                self._fire_command(spec.id, spec.command_at)

    def _fire_command(self, task_id: str, why: str) -> bool:
        """跑这一条任务的命令；``why`` ＝ 此刻是哪个触发点（:data:`task.COMMAND_ATS` 之一）。

        **「该不该跑」只在这一处判**（``spec.command_at != why`` ⇒ 不跑）：调用方只报
        「现在是什么时刻」，不自己筛。筛选散到两个调用点就等于同一个判定两处实现，
        而漏掉一处**不会有任何症状** —— 命令只是静默少跑一次。

        ``set_done`` 撤销那一档（写空串）**不许**走到这里 —— 撤销不是做事，是「我刚才
        记错了」，跑一条命令回来不但是多余的、还是不可逆的。
        """
        spec = self._spec_of(task_id)
        if spec is None or not spec.command or spec.command_at != why:
            return False
        pool = self._pool()
        if pool is None:
            # 没有池就不猜：新模型没有它整个不成立（``{变量}`` 全算不出来），这里「原样
            # 拿去跑」只会把装配问题变成一条跑错参数的命令。
            return False
        # 与 ``text`` 同一类：``{变量}`` 先插值再当命令切。**自己开一轮** —— 否则拿的是
        # 上一轮的值（点「完成」那条路上尤其明显：那时离上一次读值可能已过了好几秒）。
        pool.begin_pass()
        return cmd_mod.fire(task_mod.command_of(spec, pool))

    def _spec_of(self, task_id: str):
        """这一条任务的声明（新模型；旧模型没有这一格 ⇒ ``None``）。

        :meth:`task_specs` 每次都整段重解析，故这一个口只服务**单条**查询；
        要遍历就自己拿 ``task_specs()[0]``，别在这上面套一层循环。
        """
        for spec in self.task_specs()[0]:
            if spec.id == task_id:
                return spec
        return None

    def _prompt_spec(self, item: dict) -> PromptSpec:
        """这一条的**问法**（登记规则与「现在问我」共用一份）。

        **只此一份**：两处各写一遍的结果是「手动试出来的样子」与「到点真弹的那个」迟早
        不一样，而用户正是拿前者去推断后者的。

        问哪种由 :func:`_ask_of` 决定：``choice`` 弹一枚「完成」；``time`` 让人填一个
        时刻 —— 后者还带上**预填值**与**预览行**（填了 09:00 就意味着几点下班），
        两个都由这一条自己的 ``work_hours`` 推出来。
        """
        iid = str(item.get("id") or "")
        title = str(item.get("title") or "").strip() or FALLBACK_TITLE
        if _ask_of(item) == PROMPT_TIME:
            return PromptSpec(
                key=self._dedupe_key(iid),
                title=title,
                kind=PROMPT_TIME,
                timeout_s=PROMPT_TIMEOUT_S,
                later_minutes=LATER_MINUTES,
                initial=self._initial_of(item),
                preview=self._preview_of(item),
            )
        at = str(item.get("at") or "").strip()
        return PromptSpec(
            key=self._dedupe_key(iid),
            title=title,
            message=f"计划 {at}" if at else "",
            kind=PROMPT_CHOICE,
            options=(DONE_OPTION,),
            timeout_s=PROMPT_TIMEOUT_S,
            later_minutes=LATER_MINUTES,
        )

    def _initial_of(self, item: dict) -> "Callable[[], str]":
        """浮层**预填**的那个值：今天已经填过的那个，没有就用此刻。

        返回的是「**怎么算**」而不是算好的值（与旧翻页钟同款）：规则在**启动时**登记，
        弹窗可能几小时后才弹 —— 交一个字符串上去，弹出来的就成了「登记那一刻」的值。

        为什么先看今天填过的那个：这一条问的是「今天几点上班」这种**当天的事实**，
        用户多半是回来改它（早上填错了、隔一会儿又开了一次），而不是从零再填一遍。
        """
        eid = str(item.get("id") or "")

        def _value() -> str:
            last = self.day_value_of(eid) if eid else ""
            return last or datetime.now().strftime("%H:%M")

        return _value

    def _preview_of(self, item: dict) -> "Callable[[object], str]":
        """「填这个值之后会显示什么」那一行（算法在 :func:`scheduler.preview_text_of`，
        **纯函数**；这里只把它接到浮层上）。

        **为什么归插件**：只有它知道这一条填了 09:00 之后意味着几点下班（``work_hours``
        在这一条里）。框架那一侧只管画、且**不猜** —— 插件不给这一行就不画。
        """

        def _text(value: "object") -> str:
            return preview_text_of(item, value, datetime.now())

        return _text

    def _on_result(self, item_id: str, result: InteractionResult) -> None:
        """浮层答案落盘。**没理 / 稍后一律不写** —— 那两种的去重账各自由框架记过了
        （超时与被关算「今天不做了」，稍后到点还会再弹一次）。

        落哪儿由这一条自己的问法决定：问 ``choice`` 的记「今天完成」；问 ``time`` 的
        把答案本身写成今天的**当日值**。答非时刻的丢掉 —— 存一个谁都用不上的值，
        只会让显示面多一种要防的脏数据。
        """
        if result.kind != RESULT_ANSWERED:
            return
        item = next((i for i in self.items() if str(i.get("id") or "") == item_id), None)
        if item is None:
            return
        if _ask_of(item) == PROMPT_TIME:
            value = str(result.value or "").strip()
            if _valid_hhmm(value):
                self.set_day_value(item_id, value)
            return
        if result.value != DONE_OPTION:
            return
        self.set_done(item_id)

    # ---------- 显示面（浮层；方案 §3 / §5） ----------

    def event_topics(self) -> list:
        """订阅网络场景命中（§8.3.1 通用订阅）。

        为什么这一条必须订：条目上的「只在某场景」原本只把关**询问**（那一条条件由询问
        中枢替我们求值），而**浮层不归中枢管** —— 不订它，人不在公司也会照常弹出那一条。
        只声明、不接线：框架按声明订阅并转交 :meth:`on_system_event`，本组件禁用 /
        停用时框架反订阅。
        """
        return [ROUTE_PROFILE_TOPIC]

    def on_system_event(self, topic: str, payload) -> None:
        """场景命中变化（跑在主线程）—— **只更新内存 ＋ 点火**，重算留给 ``tick()``。

        取值口径与询问那条条件**同一份**（:func:`barkit.core.interaction.state_matches`）：
        只有 ``state == "active"`` 才算命中，场景名取 ``summary``；``conflict``（两个
        场景同时命中）不算 —— 两处必须给同一个答案，否则会出现「问了却不显示」或者
        「没问却显示了」这类自相矛盾。取不到 / 不是命中一律记成空串（＝ 没有命中）。

        场景**真变了**才 ``mark_dirty``（2026-10-11 加）：本组件一拍 60s，等下一拍的话
        换场景后浮层最多 60 秒才跟着翻；而这一条订阅本来就只在变化时才是「有事发生」，
        故按变化点火 —— 早先不点是因为一拍 1s，下一拍就在一秒之内，点火只是噪声。
        """
        if topic != ROUTE_PROFILE_TOPIC:
            return
        state = str(getattr(payload, "state", "") or "")
        scene = (
            str(getattr(payload, "summary", "") or "").strip() if state == "active" else ""
        )
        if scene != self._scene:
            self._scene = scene
            self.mark_dirty("scene")

    def scene_now(self) -> str:
        """此刻命中的场景名；**空串 ＝ 没有命中 / 还没读到**（两个都不放行门禁）。"""
        return self._scene

    def _hidden_ids(self) -> "frozenset[str]":
        """此刻**被收起**的条目（§3.2 的两种退场）：点了「稍后」的、今天已经勾完的。

        「稍后」的到点判定在这里做（内核只认「这一轮要不要收起来」）：用**单调时钟**，
        免掉系统对时 / 睡眠的跳变；顺手把过期的清掉，别让这张表随天数长。
        """
        now_mono = time.monotonic()
        for eid in [k for k, until in self._snooze.items() if until <= now_mono]:
            self._snooze.pop(eid, None)
        if not self._done:
            return frozenset(self._snooze)
        # **循环条不因「今天勾过完成」而收起**：它的下一轮是按上一次完成时刻算的
        # （``_span_of`` 读当日值那一格），整着放在这张表里 ⇒ 「每 N 分钟」第一次做完之后
        # 就再也不出现，而那正是它唯一的意义。它的退场由窗口自己判（到点之前 ``_span_of``
        # 回 ``None``）—— 与「稍后」那一档合起来仍是「这一刻要不要收起来」这一个问法。
        looping = {
            str(i.get("id") or "")
            for i in self.items()
            if int(i.get("interval_min", 0) or 0) > 0
        }
        return frozenset(self._snooze) | (frozenset(self._done) - looping)

    def snooze(self, item_id: str, minutes: int = LATER_MINUTES) -> bool:
        """把这一条从浮层上收起 ``minutes`` 分钟（行上那枚「稍后」按钮）。

        与询问中枢那条「稍后」（``later_minutes``）是**两件事**：那条管「过一阵再弹一次
        问」，这条管「这一行先别占着浮层」。两者取同一个量级，但各有各的账 —— 中枢的账
        在框架那一侧，这里的账只活在内存（重启即失效，那不算失败：「稍后」本来就是
        「先别烦我一会儿」）。
        """
        if not item_id:
            return False
        self._snooze[item_id] = time.monotonic() + max(1, int(minutes)) * 60
        self._refresh_now()
        return True

    def on_row_action(self, entry_id: str, action: str) -> None:
        """浮层那一行上的动作（§3.3）。控制器只报「谁按了哪个动作」，规则在这边 ——
        浮层不认识任何一条业务规则。"""
        if action == ACTION_DONE:
            self.set_done(entry_id)
        elif action == ACTION_SNOOZE:
            self.snooze(entry_id)

    def _compute_display(self) -> "dict":
        """算一份整屏显示态（worker 线程的 ``tick()`` 里跑），交主线程控制器去画。

        交出去的是**纯数据**（下面那个 dict）：不跨线程递 Qt 控件；行是
        ``scheduler.Row``（frozen dataclass），跨线程只读是安全的。
        """
        anchor_mono = time.monotonic()
        now = datetime.now()
        testing = self._test is not None
        if testing:
            # 测试态**盖过常态**（见 :meth:`end_test`）：它看的是**这一条**（按这条自己
            # 的登场时刻算），与「此刻哪些条目在窗口里」无关 —— 否则此刻一条都没命中时，
            # 点「预览这一条」弹出来的还是空。
            state = _kernel_preview(self._test, now, self.get_size(), self._today_values())
        else:
            state = compute_display(
                self.items(),
                now,
                self.get_size(),
                self._today_values(),
                self._scene,
                self.get_max_rows(),
                self._hidden_ids(),
            )
        # 倒计时与正计时共用一个锚点字段：浮层按**哪一型**决定往哪个方向续算
        # （见 ``clock_view.FlipClock``）—— 内核那边两型分得开，交出去只要一个数。
        anchor_s = state.countdown_s if state.countdown_s is not None else state.countup_s
        return {
            "show": state.show,
            "face": state.face,
            "value": state.value,
            "rows": list(state.rows),
            "more_text": state.more_text,
            "size": state.size,
            "active_ids": list(state.active_ids),
            "anchor_s": anchor_s,
            # 锚点两项**同生共死**：实时钟没有锚点 ⇒ 两个都是 None，浮层据此回落成
            # 「按墙钟刷」，不猜一个 0（那正是钟面会静默显示 00:00 的来源）。
            "anchor_mono": anchor_mono if anchor_s is not None else None,
            # 测试态：与旧翻页钟那边同档「不理关闭抑制，强制重绘」—— 不这么标的话，
            # 用户此前关过这一条的浮层（抑制集合里记着同一批 id）时点「预览这一条」
            # 会什么都不出现。
            "test": testing,
        }

    def _refresh_now(self) -> bool:
        """立即重算并刷浮层（不等下一拍 worker tick）；``False`` ＝ 浮层还没接管。"""
        if self._controller is None:
            return False
        self.tick()
        self._controller.on_display()
        return True

    # ---------- 面板进度 与 「现在问我」 ----------

    def _active_items(self) -> "list[dict]":
        """启用中的条目（列表页与「还没有待办」那句按它算）。"""
        return [i for i in self.items() if i.get("enabled", True)]

    def _countable_items(self) -> "list[dict]":
        """**旧模型**进完成账的条目：启用中、且问法是 ``choice``（答了 ＝ 做完了）。

        问 ``time`` 的（如推算下班）那一格存的是**答案**，不是「做完了没」—— 当成完成
        会让面板凭空多出一条永远做不完的待办。

        新模型不再用它：agenda_log 里只剩「完成」一种语义（ask 的答案归变量池了），
        故 :meth:`done_today` 在新模型下直接按 :meth:`items` 筛。
        """
        return [i for i in self._active_items() if _ask_of(i) == PROMPT_CHOICE]

    def _count_rows(self, now: datetime) -> "list[_Count]":
        """每条任务此刻的四个事实 —— **判定唯一的家**（设计文档 §4.6 A′）。

        两条路各自回答「哪些条目算数」，但**判据的形状只有一份**：生效规则通过且今日
        未完成。新模型那条判 ``conditions`` ＋ ``loop``，**刻意不判 ``window``**：
        只看窗口的话，15:00 时只剩一条 18:00 的周报 ⇒ 副标题留空（用户以为待办没了）；
        而主窗体那份清单本来就「未到期的也列出来」（灰点）—— 计数与清单必须同口径。
        """
        if self.uses_tasks():
            return self._task_counts(now)
        return self._legacy_counts(now)

    def _task_counts(self, now: datetime) -> "list[_Count]":
        """新模型的计数行（``conditions`` ＋ ``loop`` 通过 ⇒ ``active``）。

        **不过滤不生效的那些**：它们要留在返回里（``active=False``）—— 主窗体那个
        「全部」视图就是靠这一格把它们列出来的，而 :meth:`_snapshot` 自己按 ``active`` 筛。
        判定放在一处、筛选由消费方各取所需。
        """
        pool = self._pool()
        if pool is None:
            return []
        pool.begin_pass()
        out: "list[_Count]" = []
        for spec in self.task_specs()[0]:
            active = spec.enabled and task_mod.loop_allows(spec, now)
            if active and spec.conditions:
                active = rules.matches_all(spec.conditions, pool.value, pool.type_of) is True
            done = spec.id in self._done
            if _repeating(spec):
                # 循环条**不认字面 ``at``**：它是四个判定者里唯一还读它的那个（浮层那三条
                # 路走 ``_span_of`` 的循环支），而按它判出来的结果与其他三处不同 ——
                # 上一轮报告的「状态点恒红、而摘要写『每 30 分钟』」就是这么来的。
                # 按口径，这一轮的「到期时刻」＝上一次完成 ＋ N 分钟。
                at_s = None if done else _next_loop_s(spec, self._done)
            else:
                at_s = None if done else task_mod.resolve_time(
                    spec.display.reminder.at, pool
                )
            overdue = (
                spec.track_done and at_s is not None and _moment_passed(at_s, now)
            )
            out.append(_Count(spec.id, active, done, overdue, spec.track_done))
        return out

    def _legacy_counts(self, now: datetime) -> "list[_Count]":
        """旧模型的计数行（``active`` 恒真 —— ``_countable_items`` 已经筛过启用）。

        **计数基仍是旧口径「问 ``choice`` 的启用条目」**，不改：旧模型里 ``ask=time``
        那一条是「问一个值」（今天几点上班），**不是一件事** —— 它没有「完成」这个动作，
        算进「未完成 N 项」会让面板上永远挂着一条做不掉的待办。新模型没有这个问题：
        那个「值」已经是一等公民（变量 ``start_time``），任务只剩引用它的那几条。

        「过期」那一格只认字面 ``at``：算出来的时刻（循环的整点、答后推算的下班）在这里
        没有可用的当日值，一律不算过期 —— 与浮层那边「算不出窗口就不显示」同一态度。
        """
        out: "list[_Count]" = []
        for item in self._countable_items():
            iid = str(item.get("id") or "")
            if not iid:
                continue
            done = iid in self._done
            at_s = None if done else _hhmm_to_sec(item.get("at"))
            out.append(_Count(iid, True, done, at_s is not None and _moment_passed(at_s, now)))
        return out

    def task_states(self, now: "datetime | None" = None) -> "list[dict]":
        """主窗体清单读的每一条状态（**与面板那行同源**）。

        返回 ``{id, active, done, overdue, tracked}`` 的列表。``active`` 是「今天」视图的
        筛选依据（不生效的不列），后两项决定状态点；``tracked`` 给视图判「要不要画那一格
        （状态点 / 行内「完成」）」—— 面板那两个数与这里的 ``overdue`` 都按它筛过，故它必须
        跟着这一条一起出去，否则视图只能自己再猜一遍「这条算不算待办」。
        """
        return [
            {
                "id": r.task_id,
                "active": r.active,
                "done": r.done,
                "overdue": r.overdue,
                "tracked": r.tracked,
            }
            for r in self._count_rows(now or datetime.now())
        ]

    def variables_used(self) -> "list[dict]":
        """本组件的任务引用到的变量 —— 主窗体那条「变量条」读它（设计文档 §4.5.②）。

        每项 ``{id, name, value, source}``（``source`` 供界面标「询问 / 算出 / 组件」）。

        **只列用到的**：把池里全部变量铺在主窗体上，等于把框架设置页搬进组件窗口 ——
        用户在这一页要回答的是「这几条任务依赖的那些值现在是多少」。
        """
        pool = self._pool()
        if pool is None:
            return []
        pool.begin_pass()
        out: "dict[str, dict]" = {}
        for spec in self.task_specs()[0]:
            for var_id in task_mod.referenced_variables(spec):
                if var_id in out:
                    continue
                decl = pool.spec_of(var_id)
                out[var_id] = {
                    "id": var_id,
                    # 没声明过的那一个照原样当名字：编一个中文名会让人以为它存在
                    # （而它其实是个拼错的 id，值那一格会如实说「未定义」）。
                    "name": decl.name if decl is not None else var_id,
                    "value": pool.render(var_id),
                    "source": decl.source if decl is not None else "",
                }
        return list(out.values())

    def task_rows(self, now: "datetime | None" = None) -> "list[dict]":
        """主窗体清单的一行 —— **界面**要的那份形状（设计文档 §4.5.③）。

        与 :meth:`items` 的分工：那个是**内核**认的形状（浮层读 ``at`` / ``end`` /
        ``before_min``），这个是**界面**要的（标题 / 一句摘要 / 状态点）。两边的字段需求
        不同，混成一个的结果是内核形状里长出界面文案（或反过来，界面去认 ``before_min``）。

        每项 ``{id, title, summary, status, active, enabled, tracked, task}``：``task`` 只有新模型
        下才有（它是那份声明，编辑屏与「变量引用」要读），旧模型下是 ``None``。
        ``tracked`` ＝ 这条要不要「完成」这个概念（旧模型恒 ``True``）—— 视图靠它决定
        画不画状态点 / 行内「完成」。
        """
        moment = now or datetime.now()
        states = {row["id"]: row for row in self.task_states(moment)}
        if self.uses_tasks():
            pool = self._pool()
            if pool is not None:
                pool.begin_pass()
            return [
                {
                    "id": spec.id,
                    "title": spec.title,
                    "summary": task_mod.summary_of(spec, pool),
                    "status": task_mod.status_of(states.get(spec.id, {})),
                    "active": bool(states.get(spec.id, {}).get("active", True)),
                    "enabled": spec.enabled,
                    "tracked": spec.track_done,
                    "task": spec,
                }
                for spec in self.task_specs()[0]
            ]
        out: "list[dict]" = []
        for item in self.items():
            iid = str(item.get("id") or "")
            out.append({
                "id": iid,
                "title": str(item.get("title") or "").strip(),
                "summary": self.row_summary(item),
                # 旧模型没有「不生效」这一态（不启用就直接不列）⇒ ``active`` 恒真；
                # 状态点也只可能是未到 / 已完成两色（过期的判据在 ``_legacy_counts`` 里，
                # 只对 ``choice`` 条目算）。
                "status": task_mod.status_of(states.get(iid, {})),
                "active": True,
                "enabled": bool(item.get("enabled", True)),
                # 旧模型没有「不需要完成」这条 —— 一律算待办（``_legacy_counts`` 也是这么传的）。
                "tracked": True,
                "task": None,
            })
        return out

    def header_counts(self, now: "datetime | None" = None) -> "tuple[int, int]":
        """主窗体页眉那两个数 ``(未完成, 已完成)`` —— **与面板摘要同一个源**。

        两处各算一遍的结果是「面板说 3 项、窗口说 2 项」，而两边都看着对 —— 那种不一致
        用户没法判断信谁。故两个数与 ``_snapshot`` 共用 :meth:`_count_rows`。

        分子只数**生效**的任务（不生效的在「今天」视图里也不列，数进来对不上账）；
        ``tracked=False`` 的（不需要完成）同样不进这两个数 —— 它们根本不是「待办」。
        """
        rows = [
            r
            for r in self._count_rows(now or datetime.now())
            if r.active and r.tracked
        ]
        done = sum(1 for r in rows if r.done)
        return len(rows) - done, done

    def window_status(self, effective: object, now: "datetime | None" = None) -> str:
        """一段 ``effective`` 块的生效时段**此刻**算不算数（编辑屏那行状态用）。

        没配时段 ⇒ 空串 —— 「两端留空 ＝ 全天候」那句是界面文案，由编辑屏自己说。
        求值与判据全走内核（``task_mod.window_of`` → ``task_mod.window_status``）：
        界面自己再判一遍的话，「清单说不生效、编辑屏说生效」那种不一致不报错，
        而用户只看得到其中一处。
        """
        window = task_mod.window_of(effective)
        if window is None:
            return ""
        return task_mod.window_status(window, self._pool(), now or datetime.now())

    def time_ref_problem(self, text: str) -> "str | None":
        """这一格填的时刻**读不读得出来**（``None`` ＝ 读得出来）。判据问内核。

        界面不再自己维护一条形状规则：那份规则只要比引擎窄，症状就是「用户写得出、
        却存不下」（这次是 ``{off_time}-30m`` 被白挡），而原因写在代码里、不在界面上。

        池没接线（测试桩 / 单独起组件）⇒ **一律放行**：那种环境下这几条任务本来就一条
        都算不出来，再拦住用户的编辑只会更糟。
        """
        pool = self._pool()
        return None if pool is None else pool.time_ref_problem(text)

    def ask_now(self, item: dict) -> "str | None":
        """**立刻问一次**这一条（不看条件、也不看去重账）—— 给「用户自己按」的入口。

        答案**算数**：走的是与「到点真弹那次」同一个回调 ⇒ 写了完成记录、面板进度跟着变。
        这不与去重账冲突 —— 用户自己按的那一次挂的是**临时 key**（中枢加的 ``#now`` 后缀），
        真 key 那笔账一个字不动，故「点一下试试」不会让今天该弹的那次不弹。

        :param item: 条目，可以是一份**还没保存**的草稿（要给到 id）—— 口径与「测试」
            一致：试的是界面上此刻填的那些值。
        :return: ``None`` ＝ 已弹出；否则一句**事实**（弹不出来的理由，给用户看）。
        """
        center = getattr(self.ctx, "interaction", None)
        if center is None:
            return "询问中枢没接管（现在弹不出来）。"
        iid = str(item.get("id") or "")
        if not iid:
            return "这一条还没保存——浮层填的答案要落到这一条上，先保存再点。"
        center.ask_now(
            TriggerSpec(plugin_id=self.id, prompt=self._prompt_spec(item)),
            lambda result, _iid=iid: self._on_result(_iid, result),
        )
        return None

    # ---------- 预览这一条（详情页编辑屏那枚键） ----------

    def preview_task(self, task: dict) -> "str | None":
        """把这条**任务声明**的显示态立刻弹出来看（新模型编辑屏那枚「预览这一条」）。

        与 :meth:`preview_entry` 是同一件事，只多一步换算：任务声明要**先解析再求值**
        （``parse_all`` → ``resolve_item``）才变成浮层认的那份条目 —— 浮层读的是 ``at`` /
        ``end`` / ``before_min`` / ``face`` / ``actions`` 那一套，任务写的是 ``effective`` /
        ``display``。不换算直接把声明喂进去，拿到的是「一条什么都不认得的条目」：恒不显示
        （而用户看到的是「预览没反应」）。

        :return: 与 :meth:`preview_entry` 同一口径 —— ``None`` ＝ 弹出来了，否则一句事实。
        """
        pool = self._pool()
        if pool is None:
            return "变量池没接线（装配问题）—— 这一条算不出来。"
        specs, skipped = task_mod.parse_all([task])
        if not specs:
            return skipped[0] if skipped else "这一条读不出来（声明形状不对）。"
        pool.begin_pass()
        return self.preview_entry(task_mod.resolve_item(specs[0], pool, datetime.now()))

    def preview_entry(self, item: dict) -> "str | None":
        """把 ``item`` 这一条的显示态**立刻**弹出来看（不必等它到点，也不必先保存）。

        入参是**界面上此刻填的那一份**（可能还没落盘、也可能关着），故与常态显示面不同：
        常态看的是「此刻哪些条目在窗口里」，这里看的是**这一条** —— 文案里的 ``{到点}`` /
        ``{剩余}`` 都按这一条自己的时刻渲染。

        **不受「只在某场景」限制**（见 :func:`scheduler.preview_entry`）：配条目的时候
        人多半不在那条场景里，否则「只在公司显示」的这一条永远测不出来。

        :return: ``None`` ＝ 已经弹出来了；否则**一句事实** ＝ 没弹出来（要么这一条算不出
            可显示的时刻，要么浮层还没接管）。**不返回布尔**：「算不出时刻」与「浮层不在」
            是两件不同的事，挤成一个 ``False`` 之后界面只能说一句「失败了」，用户看不出
            该改条目还是该重启。
        """
        state = _kernel_preview(item, datetime.now(), self.get_size(), self._today_values())
        if not state.show:
            return "这一条算不出可显示的时刻——先把时间 / 时长填完整再试。"
        if self._controller is None:
            return "浮层还没接管（组件不在运行）—— 预览没弹出来。"
        self._test = dict(item)
        if not self._refresh_now():
            self._test = None
            return "浮层没能刷新（组件不在运行）—— 预览没弹出来。"
        return None

    def end_test(self) -> bool:
        """结束测试态（用户关掉浮层时由控制器回调）。

        返回**刚才是不是在测试态**：控制器据此决定这次「关掉」要不要记进关闭抑制 ——
        抑制说的是「这一条今天别再自动弹」，而「预览这一条」是用户自己点开的，与它无关。
        """
        was_testing = self._test is not None
        self._test = None
        return was_testing

    # ---------- 一次性迁移（合并方案 §6；幂等，跑过一次就什么都不做） ----------

    def _migrate_legacy(self) -> None:
        """把合并前那套东西并进来 —— 两半，各自幂等。

        - **配置**（§6.1，:meth:`_migrate_config`）：旧 ``plugins.clock`` 的条目并进本
          组件的 ``items``、``size`` / ``close_gesture`` 在本组件还没设过时接过来，
          最后删掉 ``plugins.clock`` 整段；
        - **数据**（§6.2，``day_log.merge_legacy``）：``clock_workday.json`` /
          ``todo_log.json`` 并进 ``agenda_log.json`` 再删旧。

        两半**互不依赖**（条目的 id 一个字不动 ⇒ 账的键天然接得上），所以一个失败不
        影响另一个。先配置后数据只是读起来顺。

        跑在 :meth:`start` 的**最前面**：后面几步读的都是迁完之后的样子。放在 ``start``
        而不是 ``__init``，是因为它要写盘（配置 + 数据文件）—— 构造一个对象不该写盘。

        **失败不外抛**：没迁成功只意味着「这次启动还是老样子」，下次启动重试；把整个
        组件拉不起来，代价远大于收益（旧数据一条没丢，只是还没搬）。
        """
        try:
            self._migrate_config()
        except Exception:  # noqa: BLE001
            log.exception("合并迁移：配置那一半没做完（下次启动重试）")
        try:
            day_log.merge_legacy()
        except Exception:  # noqa: BLE001
            log.exception("合并迁移：按天记账文件那一半没做完（下次启动重试）")

    def _migrate_config(self) -> None:
        """配置那一半（方案 §6.1）。

        没装过合并前那个组件（盘上没有 ``plugins.clock`` 段）时**什么都不做** —— 这是
        它主要的幂等判据，也是绝大多数盘上的情形。搬条目再按 **id 去重**兜第二层：
        「写自己段」与「删旧段」是两次写盘、不是一个事务，中间断掉的话下次启动会重跑，
        重跑时已经搬过来的那几条会被认出来跳过（见 ``migrate.migrate_items``）。

        :raises PluginConfigError: 写盘失败。上层接住（见 :meth:`_migrate_legacy`）。
        """
        config = getattr(self.ctx, "config", None)
        if config is None or not hasattr(config, "user_plugin_section"):
            return  # 未接线（测试桩 / 无配置源）：迁移退化为空操作
        legacy = config.user_plugin_section(migrate.LEGACY_PLUGIN_ID)
        if not legacy:
            return
        own = config.user_plugin_section(self.id)
        items = migrate.migrate_items(self.items(), legacy.get("entries"))
        # 两个标量键：**本段设过就听用户的**；等于声明默认值不落键（与 ``cfg.set`` 同口径）。
        carried = {}
        for key in ("size", "close_gesture"):
            value = legacy.get(key)
            if isinstance(value, str) and value and key not in own:
                if value != self.cfg.default_of(key):
                    carried[key] = value

        def _apply(section: dict) -> None:
            if items:
                section["items"] = items
            else:
                section.pop("items", None)  # 空表就是声明默认值，不留 ``items: []`` 残渣
            section.update(carried)

        self.cfg.mutate(_apply)
        config.remove_plugin_section(migrate.LEGACY_PLUGIN_ID)
        log.info(
            "已合并 %s 的配置：条目表现有 %d 条、%s；旧段已删",
            migrate.LEGACY_PLUGIN_ID,
            len(items),
            "／".join(f"{k}={v}" for k, v in carried.items()) or "没有标量键要搬",
        )

    def _migrate_to_tasks(self) -> None:
        """第二代一次性迁移：旧条目表 → 顶层 ``variables`` ＋ 本段 ``tasks``（§5）。

        逐条行为守恒的映射在 ``migrate.migrate_to_tasks``（纯函数），这里只做三件**只有
        本模块能做的事**：写顶层变量段、写自己那段、搬今天那几笔答案。

        **幂等靠``tasks`` 非空就返回**：本方法要写两个位置（顶层变量段、本段），而两次
        写盘不是一个事务 —— 中途断掉时变量段里会多出几条声明，下次启动照旧重跑，那些
        id 已在段里 ⇒ 不重复新增（记一条 note），只是把任务段补上去。

        **失败不外抛**：与 :meth:`_migrate_legacy` 同一条口径 —— 没迁成功只意味着「这次
        还是老样子」，下次启动重试；把组件整个拉不起来，代价远大于收益。
        """
        config = getattr(self.ctx, "config", None)
        if config is None or not hasattr(config, "set_variables"):
            return  # 未接线（测试桩 / 无配置源）：迁移退化为空操作
        try:
            if self.uses_tasks():
                return  # 已经迁过（``tasks`` 非空）；旧表留着也不再被读
            items = self.items()
            if not items:
                return  # 没有旧条目可搬（全新盘 / 用户自己已经把 tasks 写好）
            migration = migrate.migrate_to_tasks(items, config.variables())
            if migration.variables:
                # 顶层 ``variables:`` 段是**整段覆盖**的：必须拿着现有那一份去加。
                config.set_variables(
                    list(config.variables()) + [dict(d) for d in migration.variables]
                )

            def _apply(section: dict) -> None:
                section["tasks"] = [dict(t) for t in migration.tasks]
                # 旧表整段删掉：留着它只有一个作用 —— 让「到底哪一份在生效」变成
                # 一个要读代码才能回答的问题（``items()`` 的判据是 ``tasks`` 非空）。
                section.pop("items", None)

            self.cfg.mutate(_apply)
        except Exception:  # noqa: BLE001
            log.exception("第二代迁移：配置那一半没做完（下次启动重试）")
            return
        # 映射过程里该说的话（丢了什么、在哪里退让了一步）**逐条出声** ——
        # 它是用户唯一能知道「迁移动了我什么」的地方。
        for note in migration.notes:
            log.warning("迁移（旧条目 → 任务）：%s", note)
        log.info(
            "已把 %d 条旧条目迁成任务、新增 %d 条变量；%s",
            len(migration.tasks),
            len(migration.variables),
            "／".join(d["id"] for d in migration.variables) or "没有新变量",
        )
        try:
            self._move_ask_answers(migration.answer_moves)
        except Exception:  # noqa: BLE001
            log.exception("第二代迁移：今天的答案那一半没搬完（下次启动重试）")

    def _repair_ask_labels(self) -> None:
        """第一版变量迁移把询问变量的名字与问句写错了（:func:`migrate.repair_ask_labels`）。

        修完**落盘**：装配收尾那一步（``app_shell`` 的 ``_on_variables_config_changed``）
        会重读变量段并重登记 ask 规则 ⇒ 当次启动弹的就是修好的问句，不用等下一次重启。
        没东西可修时不写盘；失败不外抛（同 :meth:`_migrate_legacy` 的口径，下次启动重试）。
        """
        config = getattr(self.ctx, "config", None)
        if config is None or not hasattr(config, "set_variables"):
            return  # 未接线（测试桩 / 无配置源）：修复退化为空操作
        try:
            repaired, notes = migrate.repair_ask_labels(config.variables())
            if not notes:
                return
            config.set_variables(repaired)
        except Exception:  # noqa: BLE001
            log.exception("询问变量的名字与问句修复：没做完（下次启动重试）")
            return
        for note in notes:
            log.warning("迁移补修：%s", note)

    def _move_ask_answers(self, moves) -> None:
        """把「今天填过的那个时刻」从条目账搬到变量账，并把旧那一笔清掉。

        **必须搬**：新模型里 ``ask`` 的答案住在变量池（按日），而条目账只剩「完成」一种
        语义（:meth:`done_today` 认任何一笔当日值）。不搬的话，迁移当天那条任务会被读成
        「今天已完成」—— 面板直接报「全部完成」，浮层也不再显示它，而用户什么都没做。

        **必须清**：同理，旧账那一笔留着就是一枚完成记录。两件事一起做（同一份表、同一次
        写盘），是因为它们必须同时成立：搬成功而没清掉 = 那道假完成；清掉而没搬成功 =
        今天他填的时刻没了（所以要**先写进变量池、再清账**，失败了就不清）。

        多条旧条目合成一个变量时**取第一条**（它们在新模型里本来就是同一个量），不一致
        的那几条出声。
        """
        if not moves:
            return
        pool = self._pool()
        today = self._today_key()
        days = self._load_log()
        day = days.get(today)
        if not isinstance(day, dict):
            return
        day = {k: v for k, v in day.items() if isinstance(v, str)}
        left = dict(day)
        taken: "dict[str, str]" = {}
        moved = 0
        for item_id, var_id in moves:
            value = str(left.pop(item_id, "") or "").strip()
            if not value:
                continue
            if not _valid_hhmm(value):
                # 不是时刻的值（脏数据）本来就不该当答案用；但它留在新模型里会被读成
                # 「今天完成了」，故照样清掉。
                log.warning("迁移：%s 今天那一笔不是个时刻（%r），没有搬、已清掉", item_id, value)
                continue
            if var_id in taken:
                if taken[var_id] != value:
                    log.warning(
                        "迁移：今天有几处填的时刻不一样（%s / %s），共享变量只留了先出现的那一个",
                        taken[var_id],
                        value,
                    )
                continue
            if pool is not None and pool.set_ask_value(var_id, value):
                taken[var_id] = value
                moved += 1
                continue
            # 没搬成 ⇒ **放回原处**：那一笔留在条目账里至少还有账可查（而且清掉它并不能
            # 让任务变得正确，只是把用户的输入抹掉）。
            left[item_id] = value
            log.warning(
                "迁移：今天的「%s」（%s）没能搬进变量池 —— 仍留在条目账里，"
                "下一次启动还会被当成「今天已完成」",
                value,
                item_id,
            )
        if not moved and len(left) == len(day):
            return  # 什么都没有动（今天没有答案 / 一条都没搬成）
        if left:
            days[today] = left
        else:
            days.pop(today, None)
        if not self._save_log(days):
            log.warning("迁移：条目账没写回去 —— 今天那几笔答案下次启动还会被当成「已完成」")
            return
        log.info("迁移：搬走 %d 笔今天的答案、条目账剩 %d 笔", moved, len(left))

    # ---------- 生命周期 ----------

    def start(self) -> None:
        # 两代迁移都在**最前面**：它们要改配置与数据文件，后面几步读的都是迁完之后的样子。
        self._migrate_legacy()
        self._migrate_to_tasks()
        self._repair_ask_labels()
        # 当日值那三份内存状态先归零 ⇒ 第一拍一定重读一次盘（跨重启是全新的进程，
        # 交出去的必须是盘上那份，不是构造函数里那份空的）。
        self._values = None
        self._values_day = ""
        self._recheck_at = 0.0
        # 面板那行不等第一拍（用户一开面板就该看到真数）。
        self._done = self.done_today()
        # 加载时跳过的任务**要出声**：不出声的话，用户在配置里写错一条，界面上什么也
        # 看不出来（那一条只是静静地不生效）。
        for problem in self.task_specs()[1]:
            log.warning("任务声明：%s", problem)
        # 规则登记在 start()（主线程）：此刻中枢已由装配层挂进 ctx。
        # **新模型它一条都不登**（只剩旧条目表那一支），见 :meth:`_sync_interaction`。
        self._sync_interaction()
        # 命令的跨越基线也从头开始数：``start()`` 之后第一拍只记基线（见 ``_cmd_last_m``）。
        self._cmd_last_m.clear()
        # 浮层控制器也在 start()（主线程）建：本模块不 import PySide6（门禁），
        # 故这一句是**延迟导入**。
        from barkit.plugins.todo.clock_view import ClockController

        if self._controller is None:
            self._controller = ClockController(self)
        events = self.ctx.events
        if events is not None:
            events.subscribe(self.status_topic("main"), self._controller.on_display)

    def stop(self) -> None:
        """停用 / 退出：撤规则、摘浮层（留着会在停用后继续挂在右上角）。"""
        events = self.ctx.events
        if events is not None and self._controller is not None:
            events.unsubscribe(self.status_topic("main"), self._controller.on_display)
        center = getattr(self.ctx, "interaction", None)
        if center is not None:
            center.unregister(self.id)
        self._dispose_controller()

    def on_disabled(self) -> None:
        self.stop()

    def _dispose_controller(self) -> None:
        if self._controller is not None:
            self._controller.dispose()
            self._controller = None

    # ---------- 执行体（worker 线程） ----------

    def tick(self) -> StateReport:
        # 当日值只在这一拍**该重读时**才读盘（跨天 / 复核时间到 / 还没加载），
        # 其余每一拍只读内存 —— 一拍 60s（2026-10-11 由 1s 改），按老读法就是每拍读一次盘。
        now = datetime.now()
        self._today_values()
        # 命令那三个「时刻」触发点：与上一拍比，刚跨过去就跑（见 ``_fire_due_commands``）。
        # 放在读显示态之前 —— 它自己判时刻，与``_display`` 无关（不上浮层的任务照样能跑）。
        self._fire_due_commands(now)
        # 面板那行读的是「今天完成了哪些」：跨天归零与外部改动都靠上面那一问。
        self._done = self.done_today()
        # 整屏显示态交主线程控制器读取（避免跨线程直接碰 Qt 控件）。
        self._display = self._compute_display()
        return self._snapshot()

    def _snapshot(self, now: "datetime | None" = None) -> StateReport:
        """未完成几项 / 几项已过期 —— ``summary`` 是**唯一文案**（面板行副标题与两处悬停共用）。

        :param now: 此刻。**可注入**是为了让判据钉得住：这个摘要带「已过期」，而它是
            与真实时钟比出来的 —— 不注入的话，同一条用例在上午跑与下午跑会得到两个
            不同的结论（那不是用例写得松，是判据本来就没被钉住）。

        计数基见 :meth:`_count_rows`（设计文档 §4.6 A′）。文案模板（§4.6 B）四档固定，
        实现时别各写各的：无生效任务 ⇒ 留空、全完成 ⇒ 「全部完成」、有过期 ⇒ 「N 项已过期」
        （**优先于**未完成）、其余 ⇒ 「未完成 N 项」。

        ``overdue`` 走 ``pending`` 这一档，**不新加状态取值**：``STATES`` 三档是既有的
        契约面（取值图标与级别色都挂在它上面），加一档要连带动图标与配色。级别也保持
        ``IDLE`` —— 待办过期同样是常态，不是故障（与「没做完」同一条口径）。

        ``tracked=False`` 的不算数（整点时钟这类不该让副标题挂着「未完成 1 项」）；
        全是它时 ``rows`` 为空 ⇒ 与「没有生效任务」同路（留空）。
        """
        rows = [
            r
            for r in self._count_rows(now or datetime.now())
            if r.active and r.tracked
        ]
        if not rows:
            return StateReport("empty")
        undone = [r for r in rows if not r.done]
        overdue = [r for r in rows if r.overdue]
        if overdue:
            return StateReport("pending", f"{len(overdue)} 项已过期")
        if not undone:
            return StateReport("done", "全部完成")
        return StateReport("pending", f"未完成 {len(undone)} 项")

    # ---------- 声明点（决议 D20） ----------

    def component(self):
        """身份图标。**上标题栏**（``in_header`` 默认 True）：本组件有状态机，
        那一格画的是代表态的取值图标 ＋ 级别色，说得出事情（与旧翻页钟那种
        无状态机、只放行内图标的组件不同）。
        """
        return ComponentSpec(icon=IconName.CHECKLIST)

    def state_specs(self):
        """一台状态机：三档取值见模块顶的 ``STATES``。"""
        return (
            StateSpec(
                key="main",
                icon=IconName.CHECKLIST,
                states=STATES,
                initial="empty",
            ),
        )

    def row_specs(self):
        """面板行：**只报状态，点击归框架合成的「打开详情页」**。

        整行点击不由本行声明（决议 D28）：声明了 ``detail_entry()`` 之后框架会合成
        「打开详情页」那一件事，而 ``on_click`` **优先于**它 ⇒ 在这里再挂一个 ``on_click``
        等于永久放弃详情入口（那正是本批拆掉的旧行为：点整行曾是「立刻问下一条」）。
        同 ``hosts_guard`` / ``route_guard`` 的先例。
        """
        return (StatusRowSpec(key="main", order=self.order),)

    def detail_entry(self):
        """详情页 = **条目管理**（合并方案 §4.2）。

        形态取 ``window``（决议 D26）：左列表 ＋ 右表单实测要 560–640px，面板那个 320px
        的浮层卡装不下。触发方式是默认的 ``row`` —— 点面板整行进来（见 :meth:`row_specs`）。

        **不挂** ``save``：待办是**独立数据管理**，每条改完（编辑屏「确定」／删除）都
        立刻 ``save_items`` 写盘，没有「攒着等窗口那枚保存」这件事 —— 那是设置窗的模型
        （留给 ``route_guard`` 这类真配置面）。所以窗口页脚只有「完成」，关窗也不问
        「还有没保存的改动」。
        """
        from barkit.plugins.todo import detail_view as detail_ui

        return DetailEntrySpec(
            key="main",
            build=lambda: detail_ui.build_detail_page(self),
            presentation=DETAIL_PRESENTATION_WINDOW,
        )

    def settings_page(self):
        """设置页 = **三格配置 ＋ 一个入口**（界面在 ``settings.py``）。

        那三格是浮层大小 / 关闭方式 / 行数上限。入口是「打开任务管理」：任务的增删改
        **不在这里**（那是 :meth:`detail_entry` 的事），本页只放一枚**开窗的按钮** ——
        它走 :meth:`Plugin.open_detail`，进的是框架 ``DetailHost`` 按 plugin_id 托管的
        **同一份**内容面（与点面板整行同一个，已开着就提到前台），所以不是「第二份编辑
        器」。这跟 ``hosts_guard`` 那枚被删掉的「管理条目…」按钮的差别正在这里：那一枚
        自己 ``new`` 了一个私有对话框，两份互不感知、一份看不到另一份刚做的改动。

        ``key`` 从 ``todo-list`` 改成 ``main``：这一页不再是「清单页」，留着旧名字会让
        下一个人来这儿找条目列表。
        """
        from barkit.plugins.todo import settings as settings_ui

        return SettingsPageSpec(
            key="main",
            order=0,
            build=lambda: settings_ui.build_page(self),
            reason="custom: 三格浮层配置（大小 / 关闭方式 / 行数上限）＋「打开任务管理」"
                   "入口（走 Plugin.open_detail，与面板整行同一份内容面）＋ 行内反馈槽；"
                   "条目编辑器已搬详情页（合并方案 §4.3）",
        )


def _repeating(spec) -> bool:
    """这条走循环那一支吗（``loop.kind == interval`` 且间隔为正）。

    **「这条按哪支算法算」只有一个地方说**：浮层走 :func:`scheduler._span_of` 的循环支、
    主窗体拿 :func:`_next_loop_s` 问到期时刻，两边都得先看这一眼。分成两处各判一遍的话，
    用户在界面上会同时看见「摘要说每 30 分钟」与「状态点是红的」（而两者各自都说得通）。
    """
    return spec.loop.kind == task_mod.LOOP_INTERVAL and int(spec.loop.minutes) > 0


def _next_loop_s(spec, today_done: "dict[str, str]") -> int:
    """这一条循环的**下一轮**落在今日第几秒 —— 上一次完成时刻 ＋ N 分钟。

    **今天一次都没做过 ⇒ 返回 0**：这条任务开着就是「还没做」，故它一上来就该到期
    （``_moment_passed(0, now)`` 在零点之后恒真）。与 :func:`scheduler._span_of` 的循环支
    同一条算式 —— 那边算的是「这一轮什么时候登场」，两边漂开的结果是浮层上写着一件事、
    主窗体的状态点说另一件。

    越过今天午夜的那一轮（23:50 做完、30 分钟后再来）在这里**不回来**（返回秒数会超过
    当天），由 :func:`_moment_passed` 自然判成「还没到期」—— 今天确实没有了。
    """
    last = _hhmm_to_sec(today_done.get(spec.id) or "")
    # 同上，今天没做过的那一轮落在零点，不是在「零点 ＋ N 分钟」。
    return 0 if last is None else last + int(spec.loop.minutes) * 60


def _moment_passed(at_s: int, now: datetime) -> bool:
    """当天这个时刻过了没有（``at_s`` 是当日秒数）。**唯一**的一处判定。

    今天有两个地方要问它：面板的 ``overdue`` 计数、主窗体那一行的状态点（红/灰）。
    各写一遍的结果是同一个时刻在两处得出两个结论 —— 而它们各自都「看着对」。
    """
    now_s = now.hour * 3600 + now.minute * 60 + now.second
    return now_s > at_s
