"""contribution 契约层测试（批次 0）。

覆盖：Spec 协议（identity / point 固定 / topic 派生 / 静态性）、
PointRegistry（key 冲突、排序、快照）、U1 契约就绪、PySide6 隔离门禁。
"""

from __future__ import annotations

import subprocess
import sys
from typing import get_args

import pytest

from barkit.contribution import (
    POINT_COMPONENT,
    POINT_DETAIL_ENTRY,
    POINT_PANEL_ACTION,
    POINT_SETTINGS_PAGE,
    POINT_STATE,
    POINT_STATUS_ROW,
    SPEC_VERSION,
    SUPPORTED_API_VERSION,
    ActionSpec,
    ButtonSpec,
    ComponentSpec,
    Contribution,
    ContributionConflictError,
    ControlSpec,
    DetailEntrySpec,
    IconName,
    MenuItem,
    MenuSpec,
    PointRegistry,
    SettingsPageSpec,
    StateSpec,
    StateLevel,
    StateOption,
    StateReport,
    StatusRowSpec,
    SwitchSpec,
)

# 徽标 Spec 未进包级 ``__all__``（生产代码一律走 ``barkit.contribution.specs``，
# 包级导出只有测试在用），故这里从子模块直取。
from barkit.contribution.specs import ModeBadgeSpec  # noqa: E402


# ──────────────────────────────────────────────
# Spec 协议
# ──────────────────────────────────────────────

def test_identity_semantics():
    """identity = plugin_id.key；key 缺省取 default；不得重复写 plugin_id。"""
    assert Contribution(point="x", plugin_id="a").identity() == "a.default"
    assert Contribution(point="x", plugin_id="a", key="main").identity() == "a.main"


def test_spec_points_are_fixed_by_default():
    """各位置 Spec 的 point 由类字段固定（init=False），构造方不可传错。"""
    assert StatusRowSpec(plugin_id="a").point == POINT_STATUS_ROW
    assert SettingsPageSpec(plugin_id="a").point == POINT_SETTINGS_PAGE
    assert ActionSpec(plugin_id="a").point == POINT_PANEL_ACTION
    assert ComponentSpec(plugin_id="a").point == POINT_COMPONENT
    assert StateSpec(plugin_id="a").point == POINT_STATE
    assert DetailEntrySpec(plugin_id="a").point == POINT_DETAIL_ENTRY


def test_state_topic_derived_from_identity():
    """StateSpec.topic 由 identity 派生，必带 key（UI 规范 §4.4）。"""
    spec = StateSpec(plugin_id="notify_guard", key="main")
    assert spec.topic == "plugin.notify_guard.status.main"


def test_statespec_is_static_only():
    """StateSpec 不得含动态字段——状态流唯一路径（UI 规范 §4.1）。"""
    names = {f.name for f in StateSpec.__dataclass_fields__.values()}
    for dynamic in ("level", "summary", "detail", "updated_at"):
        assert dynamic not in names, f"StateSpec 混入了动态字段 {dynamic}"


def test_state_spec_has_no_priority_field():
    """代表态平局不靠排序字段：声明顺序就是关注度顺序（决议 D1）。

    反向确认（同轮另有用例守护）：删的是「填一个数字」这条路径，不是「平局判得出」
    这个能力 —— 判据仍在 ``shell/renderers/representative.py``，见
    ``test_representative_tie_breaks_by_declaration_order``。
    """
    names = {f.name for f in StateSpec.__dataclass_fields__.values()}
    assert "priority" not in names, "StateSpec 又长回了排序字段 priority"


def test_state_report_carries_only_value_name_and_text():
    """报告只报**取值名**（决议 D13）：``state`` + 覆盖用 ``summary`` + 时间戳。

    级别与取值图标已搬到 ``StateOption``（取值声明）——一套取值三个真源，迟早在界面上
    分叉。文案仍是唯一一份（决议 D18）：``summary`` 覆盖声明的默认 ``text``，
    原 ``detail`` 已删除（运行期零消费，留着会让作者以为存在第二条文案路径）。
    """
    names = {f.name for f in StateReport.__dataclass_fields__.values()}
    assert names == {"state", "summary", "updated_at"}
    for moved in ("level", "icon", "detail"):
        assert moved not in names, f"StateReport 又长回了 {moved}（它归 StateOption）"


def test_state_option_level_is_required_icon_and_text_optional():
    """取值自带级别是**硬要求**（决议 D13）：颜色 / 角标 / 托盘聚合的唯一来源。

    图标与默认文案可省：图标省了回落状态机主图标，文案省了就是「只有取值名」。
    """
    with pytest.raises(TypeError):
        StateOption("ready")  # type: ignore[call-arg] —— 缺 level 直接构造失败
    opt = StateOption("ready", StateLevel.OK)
    assert (opt.icon, opt.text) == (None, "")
    full = StateOption("extended", StateLevel.OK, IconName.DISPLAY_ON, "第二屏已连接")
    assert (full.level, full.icon, full.text) == (StateLevel.OK, IconName.DISPLAY_ON, "第二屏已连接")


def test_statespec_rejects_duplicate_state_ids():
    """取值名重名在**声明期**就报错：报告靠它索引，重名会让它静默指错格子。"""
    dup = (StateOption("a", StateLevel.OK), StateOption("a", StateLevel.ERR))
    with pytest.raises(ValueError, match="重复"):
        StateSpec(plugin_id="p", key="main", states=dup)
    with pytest.raises(ValueError, match="不能为空"):
        StateSpec(plugin_id="p", key="main", states=(StateOption("", StateLevel.OK),))


def test_statespec_rejects_unknown_initial():
    """``initial`` 拼错一个字母就会让首屏落在空格子上 —— 声明期拒绝，别等界面上看。"""
    states = (StateOption("idle", StateLevel.IDLE),)
    with pytest.raises(ValueError, match="initial"):
        StateSpec(plugin_id="p", key="main", states=states, initial="idel")


def test_statespec_initial_value_defaults_to_first_declared():
    """``initial`` 可留空：留空取**第一个**声明的取值（决议 D13 的可省默认）。"""
    first, second = StateOption("idle", StateLevel.IDLE), StateOption("ok", StateLevel.OK)
    spec = StateSpec(plugin_id="p", key="main", states=(first, second))
    assert spec.initial_value == "idle"
    assert StateSpec(plugin_id="p", key="main", states=(first, second), initial="ok").initial_value == "ok"
    # 没声明任何取值时首屏没有起点（框架跳过，不替它编一个）
    assert StateSpec(plugin_id="p", key="main").initial_value == ""


def test_state_report_frozen():
    snap = StateReport("ready", "正常")
    with pytest.raises(Exception):
        snap.state = "error"  # type: ignore[misc]


def test_state_report_positional_construction_is_state_first():
    """位置参数顺序锁：``state`` 在最前，后两项都有默认值。

    删除字段会让位置参数前移、静默塞错格子（D18 删除 ``detail`` 时踩过一次），
    故这里把顺序钉住。
    """
    assert StateReport("ready").summary == ""
    assert StateReport("ready").updated_at == 0.0
    assert StateReport("ready", "一句话", 12.5).updated_at == 12.5


def test_status_row_spec_has_no_derived_or_static_text_fields():
    """行声明里不得再长出「第二个真源」字段（契约 §1.4 / 决议 D18）。

    角标级别（``health``）与副标题（``live``）都由**代表态快照**派生，静态备选文案
    （``detail`` / ``live``）全仓 0 填写——三者已一并删除。这类字段的危害不是「多余」，
    而是「填了却不生效」：声明者会以为它管事。
    """
    names = {f.name for f in StatusRowSpec.__dataclass_fields__.values()}
    for stale in ("detail", "live", "health"):
        assert stale not in names, f"StatusRowSpec 又长回了派生 / 备选字段 {stale}"


def test_status_row_spec_has_no_identity_field():
    """行声明里没有身份位（决议 D14 ＋ D16）：名字取 ``Plugin.name``、图标取 ``ComponentSpec.icon``。

    ``StatusRowSpec.icon`` 已删。删它的理由不是「多余」，而是**填了也无处可去**：
    行主位画的是组件身份图标、角标画的是代表态的取值图标（D16），行上给的图标没有
    任何一个位置可落 —— 而声明者会以为它管事。名字同理不必写第二遍（收集期补默认值，
    见 ``core/contributions``）。
    """
    names = {f.name for f in StatusRowSpec.__dataclass_fields__.values()}
    assert "icon" not in names, "行上又长回了身份图标位 —— 图标唯一来源是 ComponentSpec.icon"


def test_state_option_alerts_default_follows_level():
    """提醒意图的默认值跟着级别走（决议 D17）：WARN / ERR 为真，其余为假。

    守卫类工具的核心承诺是「出事一定看得见」，逐个取值去标 `alerts=True` 必然漏 ——
    故默认由级别推出来；作者只在**与默认不同**时才写（计划内的重连标 False、
    「通知静音」这种看着不严重的标 True）。
    """
    assert StateOption("a", StateLevel.ERR).alerts_on is True
    assert StateOption("b", StateLevel.WARN).alerts_on is True
    assert StateOption("c", StateLevel.OK).alerts_on is False
    assert StateOption("d", StateLevel.IDLE).alerts_on is False
    assert StateOption("e", StateLevel.OFF).alerts_on is False
    # 显式值覆盖默认（两个方向都要能覆盖）
    assert StateOption("f", StateLevel.IDLE, alerts=True).alerts_on is True
    assert StateOption("g", StateLevel.ERR, alerts=False).alerts_on is False


def test_state_spec_has_no_tray_slot_field():
    """托盘露出不再由「状态机级开关」表达（决议 D17）：`show_in` 已删。

    它当年的用途是「这台机要不要驱动托盘徽标」，但角标取的是**组件代表态**、
    按组件聚合，与「组件里有几台状态机」无关 —— 这个开关在机制上不可能生效。
    提醒意图改由取值自己声明（`StateOption.alerts`）。
    """
    names = {f.name for f in StateSpec.__dataclass_fields__.values()}
    assert "show_in" not in names, "状态机又长回了托盘显示位开关 —— 提醒归取值（alerts）"


def test_badge_rule_spec_has_been_removed():
    """`BadgeRuleSpec` 已删（决议 D17）：阈值不再由组件声明。

    旧形态是「组件声明一个触发级别」，于是同一件事有了两个声明位（状态机 show_in ＋
    徽标规则）；现在只剩「取值值不值得提醒」这一问，用户侧阈值是框架配置。
    """
    import barkit.contribution.specs as specs_mod

    assert not hasattr(specs_mod, "BadgeRuleSpec")



def test_status_row_spec_derived_fields_come_from_snapshot():
    """反向确认：派生值仍然拿得到（删字段不是把能力删掉）。

    代表态那一格的文案未声明行级 ``subtitle`` 时就是副标题；角标级别由框架按
    全部状态机算（``shell/renderers/representative.py``），二者都不需要 Spec 上的字段。
    """
    from barkit.shell.renderers.representative import representative

    states = (StateOption("busy", StateLevel.WARN, text="代表态一句话"),)
    spec = StateSpec(plugin_id="p", key="main", states=states)
    rep = representative("p", {spec.topic: StateReport("busy")}, {spec.topic: spec})
    assert rep is not None and rep[1].state == "busy"


def test_state_option_icon_is_optional_and_falls_back_to_main_icon():
    """取值图标（决议 D13）在 ``StateOption`` 上，留空即回落状态机**主图标**。

    解析与回落都在 ``shell/renderers/state_view.resolve`` 一处完成，
    面板行与托盘底图共用它 —— 两处不可能算出不同图标。
    """
    from barkit.shell.renderers.state_view import resolve

    states = (
        StateOption("plain", StateLevel.OK, text="没有自己的图标"),
        StateOption("marked", StateLevel.OK, IconName.DISPLAY_ON, "第二屏已连接"),
    )
    spec = StateSpec(
        plugin_id="p", key="display", icon=IconName.DISPLAY_OFF, states=states,
    )
    plain = resolve(spec, StateReport("plain"))
    assert plain.icon is IconName.DISPLAY_OFF, "取值没给图标必须回落主图标，不是空图标盒"
    assert resolve(spec, StateReport("marked")).icon is IconName.DISPLAY_ON
    # 主图标本身没声明时就是 None（框架不替组件编一个）
    bare = StateSpec(plugin_id="p", key="x", states=states)
    assert resolve(bare, StateReport("plain")).icon is None


def test_statespec_main_icon_is_the_only_tray_base_gate():
    """菜单栏主图标的**准入判据就是「有没有声明主图标」**——没有额外的布尔开关。

    ``StateSpec.icon`` 一物两用（见类 docstring）：声明了它 ① 该状态机取得被用户
    选为菜单栏主图标的**资格**（设置窗「通用」页按此枚举）② 快照缺取值图标或已
    失联时**回落到它**。默认 ``None`` ⇒ 存量组件零影响（不进候选、行为不变）。

    首版是一枚独立的 ``as_tray_base`` 义务声明布尔；撤掉它的理由：同一件事写成
    「声明图标」＋「声明意愿」两个字段，就会出现「声明了图标但没声明意愿」与
    「声明了意愿却没有图标可画」两种自相矛盾的状态，而**后者根本无法渲染**。
    """
    assert StateSpec(plugin_id="p", key="display").icon is None        # 未声明 ⇒ 不进候选
    declared = StateSpec(plugin_id="p", key="display", icon=IconName.DISPLAY_OFF)
    assert declared.icon is IconName.DISPLAY_OFF


def test_statespec_state_id_is_identity_without_bus_prefix():
    """``state_id`` = 配置用**身份**：``{plugin_id}.{key}``，保留键与 topic 同取 ``main``。

    刻意不复用 ``Contribution.identity()``（保留键 ``"default"``）：同一台状态机
    只能有一个名字，否则配置键与状态流各认一套。与 ``topic`` 的差别只在**不含事件
    总线前缀** —— 配置指向的是「谁」，不是「总线上的哪个地址」。
    """
    assert StateSpec(plugin_id="ng", key="display").state_id == "ng.display"
    # 保留键必须是 "main"（与 topic / Plugin.status_topic() 同源），不是 "default"
    named = StateSpec(plugin_id="ng")
    assert named.state_id == "ng.main"
    assert named.topic == "plugin.ng.status.main"
    assert named.state_id != named.identity(), "保留键取错会让配置键与状态流各认一套"


def test_versions_declared():
    assert SPEC_VERSION == 1
    assert SUPPORTED_API_VERSION == 3


# ──────────────────────────────────────────────
# 菜单（MenuItem / MenuSpec · 批次 12 · P0②）
# ──────────────────────────────────────────────

def test_menu_item_carries_hint():
    """菜单项必须带得起**副文案**：只给名字的菜单等于没有信息（契约 §2.6 / D25）。"""
    clicked: list[int] = []
    item = MenuItem("office", lambda: clicked.append(1), hint="192.168.110.0/24 · 3 条路由")
    assert item.label == "office"
    assert item.hint == "192.168.110.0/24 · 3 条路由"
    item.on_click()
    assert clicked == [1]

    # hint 可省（省 = 该候选项确实无上下文可讲，不是"来不及填"）
    assert MenuItem("自动", lambda: None).hint == ""
    with pytest.raises(Exception):
        item.hint = "改不了"          # type: ignore[misc]  # frozen：候选项是值对象


def test_menu_spec_static_and_runtime_reads():
    """MenuSpec 两种写法都成立：静态 ``items`` / ``current`` 与运行期重读回调。

    「候选集合会随配置变」（场景列表是典型）时必须给 ``get_items`` —— 只读一次的
    静态声明会让用户看到过期列表。这里只锁契约形状与「回调被真的调用」，
    渲染在 shell 侧（今天无消费方，见契约附录 A）。
    """
    static = MenuSpec(items=(MenuItem("自动", lambda: None),))
    assert static.current == ""
    assert static.get_items is None and static.get_current is None

    calls: list[str] = []

    def _items() -> tuple[MenuItem, ...]:
        calls.append("items")
        return (MenuItem("home", lambda: None, hint="192.168.21.0/24 · 1 条路由"),)

    def _current() -> str:
        calls.append("current")
        return "home"

    runtime = MenuSpec(items=(), get_items=_items, get_current=_current)
    assert runtime.get_items()[0].hint == "192.168.21.0/24 · 1 条路由"
    assert runtime.get_current() == "home"
    assert calls == ["items", "current"]          # 纯读取：不缓存、不产生副作用


def test_menu_spec_is_a_row_control():
    """MenuSpec 与 SwitchSpec / ButtonSpec 同属行内控件联合类型（UI 规范 §5.3）。"""
    assert MenuSpec in get_args(ControlSpec)
    assert SwitchSpec in get_args(ControlSpec) and ButtonSpec in get_args(ControlSpec)
    row = StatusRowSpec(plugin_id="p", controls=(MenuSpec(items=()),))
    assert isinstance(row.controls[0], MenuSpec)


def test_row_controls_is_a_slot_tuple_capped_at_two():
    """行内控件是**多槽**、上限 2（决议 D6）：0 个默认、1~2 个放行、3 个声明期即报错。

    锁住三件事：① 默认是空元组（不是 None —— 单槽时代已废，不留 `control` 别名）；
    ② 声明顺序即从左到右的排布顺序（`SwitchSpec` 与 `ButtonSpec` 混排时不被重排）；
    ③ 上限不是「暂时没做」而是**布局约束**，超了必须当场炸，不能静默取前两个。
    """
    assert StatusRowSpec(plugin_id="p").controls == ()

    mixed = StatusRowSpec(
        plugin_id="p",
        controls=(
            SwitchSpec(checked=True, on_toggle=lambda: None),
            ButtonSpec(label="诊断", on_click=lambda: None),
        ),
    )
    assert [type(c) for c in mixed.controls] == [SwitchSpec, ButtonSpec]

    with pytest.raises(ValueError, match="最多 2 个"):
        StatusRowSpec(
            plugin_id="p",
            controls=(
                SwitchSpec(checked=True, on_toggle=lambda: None),
                SwitchSpec(checked=False, on_toggle=lambda: None),
                SwitchSpec(checked=False, on_toggle=lambda: None),
            ),
        )


def test_mode_badge_spec_binary_toggle_is_unchanged():
    """存量形态（自动 ⇄ 手动）一字不改：只给 ``on_click``，候选列表默认缺席。

    两个真实消费方（``capabilities/automation.py`` 的「自动化」徽标、
    ``plugins/ipad_tunnel/plugin.py`` 的「等链路 / 立即尝试」徽标）都长这样 ——
    本条锁住「补齐候选列表是纯增补，没把它们卷进来」。
    """
    active = [True]

    def _toggle() -> None:
        active[0] = not active[0]

    badge = ModeBadgeSpec(
        label="自动",
        accent=True,
        on_click=_toggle,
        get_state=lambda: ("自动" if active[0] else "手动", active[0]),
    )
    assert badge.items == () and badge.get_items is None    # 新字段缺席 = 老行为
    assert (badge.label, badge.accent) == ("自动", True)
    badge.on_click()
    assert badge.get_state() == ("手动", False)             # 二元切换仍自己走完
    assert StatusRowSpec(plugin_id="p", mode=badge).mode is badge


def test_mode_badge_spec_candidate_list_replaces_on_click():
    """候选列表形态：``label`` **兼任当前项** ⇒ 不设 ``current``（决议 D33）。

    多一个 ``current`` 就是多一个真源。本断言刻意锁死这一点：谁想把它加回来，
    先来读这条为什么不该加——**徽标上写着谁，谁就是当前项**。
    """
    picks: list[str] = []
    badge = ModeBadgeSpec(
        label="自动",
        get_items=lambda: (
            MenuItem("自动", lambda: picks.append("自动"), hint="按网段匹配"),
            MenuItem(
                "office", lambda: picks.append("office"),
                hint="192.168.110.0/24 · 3 条路由",
            ),
        ),
    )
    assert not hasattr(badge, "current")
    items = badge.get_items()
    assert [i.label for i in items] == ["自动", "office"]
    assert badge.label in {i.label for i in items}   # 当前项按 label 匹配，须落在列表里
    assert items[1].hint == "192.168.110.0/24 · 3 条路由"   # 摘要不放松（设计 §4.5 硬要求 1）
    items[1].on_click()
    assert picks == ["office"]
    assert badge.on_click is None                    # 有列表时点击 = 展开，不直达动作


@pytest.mark.parametrize(
    "extra",
    [
        {"items": (MenuItem("a", lambda: None),)},
        {"get_items": lambda: (MenuItem("a", lambda: None),)},
    ],
)
def test_mode_badge_spec_rejects_declaring_both_click_forms(extra):
    """两种点击形态互斥，**构造期就炸**（不静默取优先级）。

    「列表优先」这种隐式规则会让写了 ``on_click`` 的人以为他那条还会跑——声明者
    自己都不知道哪个生效，属编程错误、不是运行时状态。静态 ``items`` 与运行期
    ``get_items`` 两条路径都要拦住（漏一条就有一条能静默生效）。
    """
    with pytest.raises(ValueError, match="互斥"):
        ModeBadgeSpec(label="自动", on_click=lambda: None, **extra)



# ──────────────────────────────────────────────
# PointRegistry
# ──────────────────────────────────────────────

def test_registry_key_conflict_rejected():
    reg = PointRegistry()
    reg.contribute(StateSpec(plugin_id="a", key="main"))
    with pytest.raises(ContributionConflictError):
        reg.contribute(StateSpec(plugin_id="a", key="main"))


def test_registry_same_key_different_point_or_plugin_ok():
    reg = PointRegistry()
    reg.contribute(StateSpec(plugin_id="a", key="main"))
    reg.contribute(SettingsPageSpec(plugin_id="a", key="main", label="x"))
    reg.contribute(StateSpec(plugin_id="b", key="main"))
    assert reg.identities(POINT_STATE) == ["a.main", "b.main"]


def test_registry_collect_sorted_by_order():
    reg = PointRegistry()
    reg.contribute(ActionSpec(plugin_id="a", key="late", order=9))
    reg.contribute(ActionSpec(plugin_id="a", key="early", order=1))
    assert reg.identities(POINT_PANEL_ACTION) == ["a.early", "a.late"]


def test_registry_snapshot_structure():
    reg = PointRegistry()
    reg.contribute(StateSpec(plugin_id="a"))
    reg.contribute(StateSpec(plugin_id="a", key="muted"))
    snap = reg.snapshot()
    assert snap == {
        POINT_STATE: ["a.default", "a.muted"],
    }


def test_registry_renderer_registration():
    reg = PointRegistry()
    renderer = object()
    reg.register_point(POINT_STATUS_ROW, renderer)
    assert reg.renderer(POINT_STATUS_ROW) is renderer
    assert reg.points() == [POINT_STATUS_ROW]


# ──────────────────────────────────────────────
# 隔离门禁
# ──────────────────────────────────────────────

def test_no_pyside6_in_contribution():
    """契约层 import 时不得带入 PySide6（TYPE_CHECKING 守卫生效）。"""
    code = (
        "import sys; import barkit.contribution; "
        "assert not any(m.startswith('PySide6') for m in sys.modules), "
        "[m for m in sys.modules if m.startswith('PySide6')]"
    )
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
