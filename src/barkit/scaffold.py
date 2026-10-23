"""barkit.scaffold：``barkit new-plugin`` 的分派与生成引擎 —— 生成一个可加载的外部插件骨架。

为什么生成的是**外部插件**（默认落 ``~/Library/Application Support/BarKit/plugins/<id>/``）：
第三方作者拿不到、也不该改本仓的 ``src/barkit/plugins/``，公开的接入点只有 ``core/loader.py``
那条 manifest 驱动的外部插件之路。本命令把「照内置模板生成一个外部插件骨架」变成一条命令
（模板见本模块的 ``_ACTION_TEMPLATE`` / ``_DAEMON_TEMPLATE`` / ``_MANIFEST_TEMPLATE``；ROADMAP F2 ④）。

三条口径（改本模块前先读，别照着「顺手加个便利」改掉）：

1. **Qt-free**：本模块只 import 标准库与 ``barkit.paths``。CLI 之路不许拉 PySide6 ——
   生成代码这件事与界面无关，界面依赖残缺的环境里它照样要能跑；行为门禁
   ``test_new_plugin_scaffold_works_without_pyside6`` 用子进程挡住 PySide6 后真跑一次。
   推论：**不构造 ``core.config.Config``**（它 import PySide6）⇒ 外部插件根目录取
   ``paths.EXTERNAL_PLUGINS_DIR``（调用期读模块属性）；要放到别处就显式给 ``--dir``。
2. **不覆盖**：目标目录已存在即拒绝 —— 用户目录里的东西不猜、不合并、不覆盖。
3. **生成物自证**：模板不是「写着看看」的文本。``tests/test_scaffold.py`` 会把两套骨架真
   生成出来、经 ``discover_external`` 加载、过 ``ContributionRegistry`` 的收集期门禁 ——
   模板改了而那条用例还是绿的，才算数。
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from string import Template
from typing import Sequence

from barkit import paths          # 调用期读模块属性（tests/conftest.py 的路径隔离靠这一点）
# 契约版本从常量读，不写字面量：它同时也是生成物里 ``api_version`` 的取值（单一真源）。
from barkit.contribution.points import SUPPORTED_API_VERSION

__all__ = ["DEFAULT_ORDER", "DEFAULT_POLL", "ScaffoldError", "render_manifest",
           "render_plugin_py", "run", "scaffold", "validate_plugin_id"]

#: 新插件的默认插入位（``Plugin.order``：仅当 id 未进 ``config.plugin_order`` 时生效）。
DEFAULT_ORDER = 90

#: 守护型骨架给的建议节拍（秒）：框架拿它当 ``plugins.<id>.poll`` 的默认值，用户仍可改。
DEFAULT_POLL = 5

#: id 词法：小写字母开头，后接小写字母 / 数字 / 下划线，总长 ≤ 32。
#: 它会同时成为目录名、配置段键 ``plugins.<id>`` 与日志标签 —— 所以大写、连字符、点号
#: 一律不许：前两个在 YAML / 路径里能混过去，但配置段键一写就错，且错在用户手边。
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class ScaffoldError(Exception):
    """拒绝生成。原因直接给用户看 —— 不回退、不猜、不半做。"""


# ──────────────────────────────────────────────
# 生成前的校验（纯函数，便于用例逐条钉）
# ──────────────────────────────────────────────


def validate_plugin_id(plugin_id: str) -> "str | None":
    """id 合法性 → 拒绝原因；``None`` ＝ 通过。"""
    if not plugin_id:
        return "id 不能为空"
    if not _ID_RE.match(plugin_id):
        return (
            f"id {plugin_id!r} 不合规：须小写字母开头，后接小写字母 / 数字 / 下划线，"
            "总长 ≤ 32（它会成为目录名、配置段键 plugins.<id> 与日志标签）"
        )
    return None


def builtin_plugin_ids() -> set[str]:
    """内建插件 id 集（≈ ``barkit/plugins/`` 下带 ``plugin.py`` 的子包名，``_`` 前缀不计）。

    **按目录名取，不 import 插件模块**取 ``Plugin.id``：导入要拉 PySide6，与本模块的
    Qt-free 口径冲突。插件 id 缺省即目录名（《组件开发者指南》§3.1），实测 6 个内建组件
    两者一致 —— 这里要的只是「这个 id 是不是已经被占了」，不需要精确到类属性。
    """
    root = Path(paths.__file__).resolve().parent / "plugins"
    if not root.is_dir():
        return set()
    return {
        entry.name
        for entry in root.iterdir()
        if entry.is_dir()
        and not entry.name.startswith("_")
        and (entry / "plugin.py").exists()
    }


def _yaml_double_quoted(value: str) -> str:
    """YAML 双引号字符串的转义（只需处理 ``\\`` 与 ``"``，其余字符 YAML 原样收）。"""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ──────────────────────────────────────────────
# 模板
#
# 两套骨架对着 `_template/`（内建插件的官方脚手架）写，差别只有一个：外部插件由
# `core/loader.py` 按**文件路径**当顶层模块加载，没有包上下文 ⇒ `from . import settings`
# 必 ImportError，所以 UI 只能留在同一个文件里（内建插件那条「plugin.py 零 PySide6」的
# 规矩对外部插件今天做不到，是已知边界，不是本模板偷懒）。
# ──────────────────────────────────────────────

_ACTION_TEMPLATE = Template('''"""$name —— 外部插件（由 `barkit new-plugin` 生成的骨架）。

安装位置：外部插件根目录下的 `$id/`。重启 BarKit 即被加载 —— 插件身份在 `manifest.yaml`，
入口是本文件的 `Plugin` 类，**零改框架文件**。

接着改什么（按顺序）：

1. `name` / `order` 改成你自己的（`id` 别动：目录名、配置段键 `plugins.$id` 与清单都以它为准）；
2. `STATES` 与 `_snapshot()`：换成你自己的取值闭集 —— 取值名 / 级别 / 默认文案只写这一处，
   界面上画什么颜色、挂什么角标都由它派生；
3. `_do_action()`：换成真实业务。领域逻辑另起模块（`engine.py` / `decider.py` 这样按职责命名），
   本文件只做编排。

两条会踩的硬约束：

- **本文件就是本插件的全部**：外部插件按文件路径加载，没有包上下文 ⇒ `from . import settings`
  必 ImportError。UI 不能像内建插件那样拆到 `settings.py`（那是内建插件的规矩：`plugin.py`
  零 PySide6、界面归设置面模块）。
- **状态只能从框架指定的两个出口走**：手动操作后用 `self.publish_snapshot()` 即时广播，
  周期兜底靠 `tick()` 的返回值（框架代发）；话题由框架派生，**不要**自己 publish。
"""

from __future__ import annotations

import logging

from PySide6.QtWidgets import QWidget

from barkit.contribution.specs import (
    ActionSpec,
    ComponentSpec,
    HotkeySpec,
    IconName,
    SettingsPageSpec,
    StateLevel,
    StateOption,
    StateReport,
    StateSpec,
    StatusRowSpec,
)
from barkit.core.plugin import RUN_ON_DEMAND, Plugin
from barkit.shell.ui_kit import note, settings_page_shell, tool_button

log = logging.getLogger(__name__)

#: 取值闭集（决议 D13）：取值名 / 级别 / 默认文案只写一次，报告只报取值名。
STATES: tuple[StateOption, ...] = (
    StateOption("unknown", StateLevel.IDLE, text="尚未运行"),
    StateOption("ready", StateLevel.OK, text="就绪"),
)


class Plugin(Plugin):
    id = "$id"
    name = "$name"
    run_mode = RUN_ON_DEMAND     # 按需触发，不进调度器（常驻轮询用 `--daemon` 那套骨架）
    api_version = $api_version  # 契约版本，须与框架支持的版本精确相等（清单里写同一个值）
    order = $order  # 默认插入位：仅当 id 未进 config.plugin_order 时生效

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._state = "unknown"  # 当前取值名；真实插件在动作 / 轮询里更新它

    # ---------- 业务逻辑 ----------

    def _do_action(self) -> None:
        """操作区按钮 / 设置页按钮 / 热键的共同回调（真实插件在这里委托领域模块）。"""
        self._state = "ready"
        self.publish_snapshot()  # 手动改完立刻广播，不等下一拍轮询
        log.info("$id：动作已触发")

    # ---------- 状态快照 ----------

    def _snapshot(self) -> StateReport:
        """只回答「现在是哪一格」（取值名）；级别与默认文案归 `STATES` 声明。

        `summary` 是**唯一文案**：面板行副标题与两处悬停提示共用这一句，要写成人看得懂的
        一句话（如「3 条生效 · 最后检查 12:04」）；留空则用该取值声明的默认文案。
        """
        return StateReport(self._state)

    # ---------- 贡献：一个声明点一个方法 ----------

    def component(self):
        """标题栏那枚图标（身份的唯一真源）。图标只能从 `IconName` 里挑 —— 见
        `barkit/contribution/specs.py`；要自定义字形得先进图标库（决议 D19）。"""
        return ComponentSpec(icon=IconName.GEAR)

    def row_specs(self):
        """状态面板的一行。行名与图标都不在这里写：名字取 `Plugin.name`，
        图标取 `component()` 里那枚身份图标。"""
        return (StatusRowSpec(key="main"),)

    def state_specs(self):
        """状态机声明（静态）：取值闭集 + 首屏起点 + 失联阈值。"""
        return (
            StateSpec(
                key="main", icon=IconName.GEAR,
                states=STATES, initial="unknown",
                stale_after=None,     # 秒；超过这么久没上报 → 框架判失联（周期型必填一个）
            ),
        )

    def actions(self):
        """面板底部操作区的按钮。"""
        return (ActionSpec(label="执行示例动作", on_click=self._do_action),)

    def hotkeys(self):
        """全局热键。`default_combo` 留空 ＝ 先不占键位；键位与别的组件撞车（含内建
        Ctrl+Opt 那几枚）会在启动时被**拒绝登记**并在日志里指名，换个键位即可。"""
        return (
            HotkeySpec(
                name="${id}_action", label="执行示例动作",
                default_combo="", callback=self._do_action,
            ),
        )

    def settings_page(self):
        """设置页（低频一次性配置与说明；每天都用的入口应改用详情页 `DetailEntrySpec`）。
        用了 `build` 就得写 `reason`，讲清「为什么必须自绘」。内建插件那条由门禁
        `test_custom_page_has_reason` 扫，外部插件没有门禁扫你 —— 所以更要说实话。"""
        return SettingsPageSpec(
            label=self.name,
            build=self._build_settings_page,
            reason="动作入口 + 使用说明页，没有字段表单可以声明（声明式渲染器未接线）",
        )

    # ---------- 设置页的界面（UI 归本文件，理由见模块头）----------

    def _build_settings_page(self) -> QWidget:
        page, layout = settings_page_shell(self.name)
        btn = tool_button("执行示例动作", "primary")
        btn.clicked.connect(self._do_action)
        layout.addWidget(btn)
        layout.addWidget(note("这是 $name 的设置页。换成你自己的说明。"))
        layout.addStretch(1)
        return page
''')


_DAEMON_TEMPLATE = Template('''"""$name（外部插件 · 守护型骨架，由 `barkit new-plugin --daemon` 生成）。

守护型 ＝ 「无界面后台收敛 + 状态面板一行」：框架按 `plugins.$id.poll` 周期调 `tick()`，
线程 / 时钟 / 起停 / 合并窗口全归框架 —— 插件侧**禁**自起线程、禁 `QTimer`、禁 `time.sleep`。

接着改什么（按顺序）：

1. `name` / `POLL_S` / `order` 改成你自己的（`id` 别动）；
2. `STATES` 与 `_snapshot()`：换成你自己的取值闭集；
3. `SUBJECT` 与 `subjects()`：换成本组件真正要看的那个**领域** —— 事实由守护经会话
   推来（`observed()` 读内存里那一份），**不落盘、不读盘**：没有「状态文件」这件事，
   也就没有「文件在不在 / mtime 新不新」可判；
4. `resources()`：把你真正持有的文件 / 热键 / helper task 声明全，框架据此做彻底卸载。
   `data_files` **默认空**：新组件要落盘得先答得出「丢了以后用户会看见与事实不符的
   东西吗、或被重复打扰吗」，答不上来的一律留内存。

想要更多：长驻线程与「要哪几个领域的事实」同样只**声明**（`resident_services()` /
`subjects()`），不要自己起。需要设置页时另加 `settings_page()`，界面写在本文件里
（外部插件不能拆文件，理由见 `barkit new-plugin` 那套动作型骨架的模块头）。
"""

from __future__ import annotations

import logging
import time

from barkit.capabilities.automation import AutomationCapability
from barkit.contribution.specs import (
    ComponentSpec,
    HotkeySpec,
    IconName,
    StateLevel,
    StateOption,
    StateReport,
    StateSpec,
    StatusRowSpec,
    SwitchSpec,
)
from barkit.core.plugin import FAIL_LOG, RUN_PERIODIC
from barkit.core.resources import PluginResources
from barkit.core.schema import FieldSpec, FieldType, FormSchema

log = logging.getLogger(__name__)

POLL_S = $poll          # 建议节拍（秒）：框架据此定 plugins.$id.poll 的默认值，用户仍可改
STALE_S = POLL_S * 3    # 失联阈值：连着几拍没上报 → 框架把代表态判成 err

#: 本组件订阅的**领域名**（占位）。真实可订阅的领域名是**闭集** —— 那是 root helper
#: 侧真正会推的东西（一个领域一份事实，与文件路径无关）。订阅闭集外的名字照样被接受、
#: 只是永远推空，所以写占位名时要清楚自己在示范形状，不是申请一个新领域。
SUBJECT = "$id"

#: 取值闭集（决议 D13）：取值名 / 级别 / 默认文案只写一次，报告只报取值名。
STATES: tuple[StateOption, ...] = (
    StateOption("unknown", StateLevel.IDLE, text="尚未读取"),
    StateOption("waiting", StateLevel.IDLE, text="等待后台"),
    StateOption("converged", StateLevel.OK, text="已收敛"),
)


class Plugin(AutomationCapability):
    id = "$id"
    name = "$name"
    run_mode = RUN_PERIODIC      # 框架按 plugins.$id.poll 周期叫 tick()（跑在 worker 线程）
    failure_policy = FAIL_LOG    # 只读刷新用它；有写盘 / 执行动作的插件用 FAIL_BREAK
    suggested_poll = POLL_S      # RUN_PERIODIC 组件必填：组件给的是建议，不是命令
    api_version = $api_version  # 契约版本，须与框架支持的版本精确相等（清单里写同一个值）
    order = $order

    def __init__(self, ctx) -> None:
        super().__init__(ctx)

    # ---------- 与守护的会话（不写文件、不读文件）----------

    def _observed(self) -> dict:
        """取守护报来的那一份事实。

        `observed()` 读的是**会话推到内存里的**最新观测，没收到过返回兜底值 —— 兜底
        说的是「还没有事实」，不是「一切正常」。**别**改回「读一个状态文件 + 判 mtime」：
        事实是推过来的，「文件在不在 / mtime 新不新 / 里面那个时刻戳是不是此刻的」
        这三问都没有对象了。
        """
        state = self.observed(SUBJECT, None)
        if isinstance(state, dict):
            return state
        return {"error": "守护尚未回报（未连接或未运行）"}

    # ---------- 状态快照 ----------

    def _snapshot(self) -> StateReport:
        """只回答「现在是哪一格」（取值名）；级别归取值声明（决议 D13）。
        `summary` 是唯一文案：面板行副标题与两处悬停提示共用它。"""
        state = self._observed()
        if "error" in state:
            return StateReport("waiting", str(state["error"]), updated_at=time.time())
        return StateReport(
            "converged", str(state.get("note", "")) or "已收敛", updated_at=time.time(),
        )

    def tick(self) -> StateReport:
        """每轮轮询体（worker 线程：**不要碰 UI**）。返回的快照由框架广播到
        `plugin.$id.status.main`；抛异常按 `failure_policy` 处置。"""
        return self._snapshot()

    # ---------- 自动化开关（AutomationCapability：自动 / 手动徽标 + 行内开关）----------

    @property
    def automation_active(self) -> bool:
        """自动化是否运行中。暂停时状态完全由手动开关决定。

        取值走 ``self.cfg``（下面的 ``config_schema()`` 是默认值的唯一真源）——
        **别在调用点写 `.get("auto_enabled", True)`**：那会形成第二份默认值，
        改 schema 时行为不动、也没有判据会红。"""
        return bool(self.cfg.value("auto_enabled"))

    def set_automation_active(self, enabled: bool) -> None:
        """写用户配置段（零授权）再施加领域副作用。写配置走 `self.cfg.set`
        —— **别**自己读用户 YAML 再 `save_user`（那会把单键改写退化成整段覆盖），
        也别写 `section["auto_enabled"] = ...` 那种把默认值也物化下去的写法：
        `cfg.set` **等于声明默认值即删键**，用户 YAML 只留差异。"""
        enabled = bool(enabled)
        if self.automation_active == enabled:
            return

        self.cfg.set("auto_enabled", enabled)
        self.publish_snapshot()  # 即时广播，不等下一拍

    # ---------- 领域订阅（事实由守护推来，路径不出框架）----------

    def subjects(self) -> list:
        """要哪几个**领域**的最新事实（不是文件路径）。

        框架把这份声明交给会话（订阅即立刻推一份当前的），推送到达时转交
        `on_observed()`。**没有「监视哪个文件」这件事了**，也不要自己读状态文件 ——
        那正是通道化那套被拆掉的东西。
        """
        return [SUBJECT]

    def on_observed(self, subject: str, observed) -> None:
        """守护推来一份事实（跑在主线程）。**只许「更新内存 + `mark_dirty()`」**：
        读盘 / 起进程 / 网络一律禁止，否则会卡住界面。实际工作留给 `tick()`。"""
        if subject == SUBJECT:
            self.mark_dirty("helper_observed")

    # ---------- 资源与配置声明 ----------

    def resources(self) -> PluginResources:
        """本插件持有的外部资源（框架据此彻底卸载 + A 类对账）。`data_files` 相对
        用户域、不得含绝对路径；`seed_files` 必须是 `data_files` 的子集。

        `data_files` **默认空**是刻意的：要落盘得先答得出「丢了以后用户会看见与事实
        不符的东西吗、或被重复打扰吗」；答不上来的（通道 / 派生 / 缓存）一律留内存。
        """
        return PluginResources(
            data_files=[],
            seed_files=[],
            config_section=self.id,
            hotkeys=["${id}_toggle"],
            helper_tasks=[],
        )

    def config_schema(self) -> FormSchema:
        """本插件的配置字段（**默认值单一真源**）。**别声明 `poll`**：框架节拍的用户
        入口由框架按 `run_mode` + `suggested_poll` 自动生成，组件再声明一遍就是开了第
        二条覆盖通道。"""
        return FormSchema(
            plugin_id=self.id,
            version=1,
            fields=(
                FieldSpec(
                    key="auto_enabled", label="自动化（自动收敛）",
                    type=FieldType.BOOL, default=True,
                ),
            ),
        )

    # ---------- 贡献：一个声明点一个方法 ----------

    def component(self):
        """标题栏那枚图标（身份的唯一真源；图标只能从 `IconName` 里挑）。"""
        return ComponentSpec(icon=IconName.SHIELD)

    def row_specs(self):
        """守护型通常只贡献一行状态 + 自动化徽标（徽标由能力基类统一提供）。"""
        return (
            StatusRowSpec(
                key="main",
                controls=(
                    SwitchSpec(
                        checked=self.automation_active,
                        on_toggle=self.toggle_automation,
                        get_checked=lambda: self.automation_active,
                    ),
                ),
                mode=self.mode_badge_spec(),
            ),
        )

    def state_specs(self):
        """状态机声明（静态）：取值闭集 + 首屏起点 + 失联阈值。"""
        return (
            StateSpec(
                key="main", icon=IconName.SHIELD,
                states=STATES, initial="unknown", stale_after=STALE_S,
            ),
        )

    def hotkeys(self):
        """全局热键。键位留空 ＝ 先不占位；撞车会被拒绝登记并在日志里指名。"""
        return (
            HotkeySpec(
                name="${id}_toggle", label="暂停 / 恢复自动化",
                default_combo="", callback=self.toggle_automation,
            ),
        )
''')


_MANIFEST_TEMPLATE = Template('''# 外部插件清单（manifest）：发现器读它就能判定「这是什么插件 / 契约版本合不合」，
# 无需 import 插件代码。字段真源见《组件契约规范-完整例子》§2.1「外部插件清单」。
#
#   id           插件唯一标识（必填）：目录名、配置段键 plugins.<id>、日志标签都用它
#   name         用户可读名（记录用）。**显示名的真源是 Plugin.name**，这里不生效
#   version      插件自身版本（语义化）：用于「用哪个版本覆盖哪个版本」的提示，不进任何门禁；
#                不写 = 未标注
#   api_version  契约版本（必填）：须与框架支持的版本**精确相等**，否则读清单即拒载
#   entry        "模块:类" 形式，默认 plugin:Plugin
#   order        新插件默认插入位（可省，覆盖类属性；§6.4）
id: $id
name: $name_yaml
version: "0.1.0"
api_version: $api_version
entry: plugin:Plugin
order: $order
''')


def render_manifest(plugin_id: str, name: str, order: int, api_version: int) -> str:
    """``manifest.yaml`` 的内容（纯函数：给用例直接断言，不落盘）。"""
    return _MANIFEST_TEMPLATE.substitute(
        id=plugin_id, name_yaml=_yaml_double_quoted(name), order=order,
        api_version=api_version,
    )


def render_plugin_py(
    plugin_id: str, name: str, order: int, api_version: int, *, daemon: bool, poll: int,
) -> str:
    """``plugin.py`` 的内容。``name`` 会被嵌进 Python 字符串字面量，故先做同类转义。"""
    template = _DAEMON_TEMPLATE if daemon else _ACTION_TEMPLATE
    return template.substitute(
        id=plugin_id,
        name=name.replace("\\", "\\\\").replace('"', '\\"'),
        order=order, api_version=api_version, poll=poll,
    )


# ──────────────────────────────────────────────
# 生成
# ──────────────────────────────────────────────


def scaffold(
    plugin_id: str, *, name: "str | None" = None, order: int = DEFAULT_ORDER,
    daemon: bool = False, base_dir: "Path | None" = None,
) -> "tuple[Path, ...]":
    """把骨架写进 ``<base_dir>/<plugin_id>/``，返回落盘的文件清单（顺序即写入顺序）。

    三条拒绝（都是 ``ScaffoldError``，**不落任何文件**）：id 不合规 / id 已被内建插件占用 /
    目标目录已存在。
    """
    reason = validate_plugin_id(plugin_id)
    if reason is not None:
        raise ScaffoldError(reason)
    if plugin_id in builtin_plugin_ids():
        raise ScaffoldError(
            f"id {plugin_id!r} 是内建插件在用的 id（保留名）—— 外面再放一个同名包会在发现期"
            "**静默顶替**内建的那一个（两者在 registry 里是同一个键），卸载后内建的又回来；"
            "设置页那条安装入口也拒收这种包（`PluginManager` 的护栏）。换个 id。"
        )

    base = Path(base_dir) if base_dir is not None else paths.EXTERNAL_PLUGINS_DIR
    target = base / plugin_id
    if target.exists():
        raise ScaffoldError(
            f"目标目录已存在，不覆盖：{target}（要重新生成先自己删掉，或换个 id / --dir）"
        )

    display_name = name if name is not None else plugin_id
    target.mkdir(parents=True)
    # 写入顺序：先插件代码、最后清单 —— 清单是发现器的入口，它落盘才算「这个包在此」，
    # 于是半截目录不会被当成插件（loader 对无清单的目录静默跳过）。
    plugin_py = target / "plugin.py"
    manifest = target / "manifest.yaml"
    plugin_py.write_text(
        render_plugin_py(plugin_id, display_name, order, SUPPORTED_API_VERSION,
                         daemon=daemon, poll=DEFAULT_POLL),
        encoding="utf-8",
    )
    manifest.write_text(
        render_manifest(plugin_id, display_name, order, SUPPORTED_API_VERSION),
        encoding="utf-8",
    )
    return (plugin_py, manifest)


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="barkit new-plugin",
        description="生成一个外部插件骨架（manifest.yaml + plugin.py），落在外部插件根目录下。",
    )
    parser.add_argument("id", help="插件 id：小写字母开头，后接小写字母 / 数字 / 下划线")
    parser.add_argument("--name", default=None,
                        help="显示名（默认取 id）。真源是 Plugin.name，清单里的 name 只是记录")
    parser.add_argument("--daemon", action="store_true",
                        help="生成守护型骨架（RUN_PERIODIC + tick() + 自动化开关）")
    parser.add_argument("--order", type=int, default=DEFAULT_ORDER,
                        help="默认插入位，越小越靠前（默认 %(default)s）")
    parser.add_argument("--dir", dest="base_dir", default=None,
                        help="外部插件根目录（默认 BarKit 用户目录下的 plugins/，其下新建 <id>/；"
                             "若你在 config.yaml 里改过 external_plugins.path，请用它指过去）")
    return parser


def _next_steps(base: Path, plugin_id: str) -> str:
    return (
        f"下一步：\n"
        f"  1. 重启 BarKit —— 插件在启动时经清单发现，零改框架文件；\n"
        f"  2. 设置窗「插件」页会列出它（可停用 / 卸载），面板出现它贡献的那一行；\n"
        f"  3. 没出现？`barkit doctor` 的「插件发现」组会说原因"
        f"（被拒 / 导入失败都会逐条列出）。\n"
        f"  插件目录：{base / plugin_id}\n"
    )


def run(argv: "Sequence[str]") -> int:
    """执行生成并打印结果。退出码：``0`` 成功 ／ ``1`` 拒绝生成 ／ ``2`` 用法错。"""
    try:
        args = _parser().parse_args(list(argv))
    except SystemExit as exc:            # argparse 自己已打印帮助 / 用法
        return 0 if exc.code in (0, None) else 2

    base = Path(args.base_dir) if args.base_dir else paths.EXTERNAL_PLUGINS_DIR
    try:
        files = scaffold(args.id, name=args.name, order=args.order,
                         daemon=args.daemon, base_dir=base)
    except ScaffoldError as exc:
        print(f"未生成：{exc}")
        return 1

    kind = "守护型" if args.daemon else "动作型"
    print(f"已生成外部插件骨架 {args.id}（{kind}）：\n")
    for path in files:
        print(f"  {path}")
    print()
    print(_next_steps(base, args.id), end="")
    return 0
