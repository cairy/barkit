"""U1 契约接线测试（UI 交互分层规范 v2 §4 / §8 / §9）。

范围：三个 U1 Spec（StateSpec / ComponentSpec / DetailEntrySpec）的运行时
消费方 + §8.3 两个派生入口（tray_queue / representative）。全部纯逻辑、
零 Qt 依赖。

§9 门禁中与本轮相关的条目在此落地；其余（渲染层可点性 / 溢出簇 / 折叠）
属 U2 视觉轮次，不在本文件。
"""

from __future__ import annotations

from barkit.contribution.specs import (
    ComponentSpec,
    DetailEntrySpec,
    IconName,
    StateLevel,
    StateOption,
    StateReport,
    StateSpec,
    TRAY_MIN_LEVEL_DEFAULT,
    TRAY_MIN_LEVELS,
)
from barkit.core.contributions import ContributionRegistry
from barkit.shell.renderers.badge import Candidate, tray_queue
from barkit.shell.renderers.representative import representative
from barkit.shell.renderers.state_view import resolve


def _spec(plugin_id: str, key: str, level: StateLevel = StateLevel.OK) -> StateSpec:
    """造一台只声明**一个取值**（名固定 ``s``）的状态机，供纯逻辑用例使用。

    级别归取值声明（决议 D13）之后，造 Spec 必须连取值一起给 —— 这正是本轮改动
    想让作者看见的：报告不再自带级别。
    """
    return StateSpec(
        plugin_id=plugin_id, key=key,
        states=(StateOption("s", level, text=f"{key} 的默认文案"),),
    )


class _U1Plugin:
    """U1 三 Spec 全声明桩（api_version=3）。"""

    id = "u1p"
    order = 0
    api_version = 3

    def component(self):
        return ComponentSpec(icon=IconName.BELL, name="通知守卫")

    def state_specs(self):
        return (
            StateSpec(
                key="muted", icon=IconName.BELL,
                title="通知已静音",
                states=(StateOption("muted", StateLevel.WARN, text="通知静音"),),
            ),
            StateSpec(
                key="armed", icon=IconName.BELL,
                title="通知守卫生效",
                states=(StateOption("armed", StateLevel.OK, text="通知正常"),),
            ),
        )

    def detail_entry(self):
        return DetailEntrySpec(label="通知守卫详情", build=None)


# ---------- 注册中心收集（§8.1 三个扩展点的运行时消费方） ----------


def test_registry_collects_u1_specs():
    reg = ContributionRegistry([_U1Plugin()])
    states = reg.states()
    assert [s.key for s in states] == ["muted", "armed"]
    assert all(s.point == "state" for s in states)
    comps = reg.components()
    assert len(comps) == 1 and comps[0].name == "通知守卫" and comps[0].point == "component"
    entries = reg.detail_entries()
    assert len(entries) == 1 and entries[0].label == "通知守卫详情"
    assert entries[0].point == "detail.entry"


def test_registry_u1_specs_follow_plugin_order():
    class _A:
        id = "a"
        order = 1
        api_version = 3

        def component(self):
            return ComponentSpec(name="A")

    class _B:
        id = "b"
        order = 0
        api_version = 3

        def component(self):
            return ComponentSpec(name="B")

    # 用户排序把 b 提到 a 前 → 收集顺序随之（三处渲染同源的唯一排序）
    reg = ContributionRegistry([_A(), _B()], order_provider=lambda: ["b", "a"])
    assert [c.plugin_id for c in reg.components()] == ["b", "a"]


def test_registry_u1_specs_empty_when_undeclared():
    class _Plain:
        id = "plain"
        order = 0
        api_version = 3

    reg = ContributionRegistry([_Plain()])
    assert reg.states() == [] and reg.components() == [] and reg.detail_entries() == []


# ---------- tray_queue（决议 D17 纯函数：候选 → 触发 → 取胜 ＋ 级内排序） ----------


def _cand(pid="p", level="warn", alerts=True, broken=False):
    return Candidate(
        plugin_id=pid, level=level, alerts=alerts, icon=None,
        tooltip=f"{pid}：{level}", broken=broken,
    )


def test_tray_queue_empty_when_nothing_triggers():
    """没有触发者就不画角标：作者没标 alerts 的不进队，级别够不到的也不进队。"""
    assert tray_queue([_cand(level="warn", alerts=False)], ["p"]) == ()
    assert tray_queue([_cand(level="ok")], ["p"]) == ()


def test_tray_queue_min_level_defaults_to_warn():
    """默认阈值 `warn`：warn 进得来、idle 进不来（D17「低于阈值不上屏」）。"""
    assert [c.plugin_id for c in tray_queue([_cand(level="warn")], ["p"])] == ["p"]
    assert tray_queue([_cand(level="idle")], ["p"]) == ()


def test_tray_threshold_domain_is_single_sourced():
    """档位值域只有一份（`contribution.specs`）：这里是**同一对象**，不是抄的字面量。

    设置页的候选、``Config.set_tray_min_level`` 的校验、本模块的分支判据都读它；
    谁在本地另写一份 `("warn", "err")`，这条就转红。
    """
    from barkit.shell.renderers import badge

    assert badge.TRAY_MIN_LEVELS is TRAY_MIN_LEVELS
    assert TRAY_MIN_LEVELS == ("warn", "err")           # 契约里就这两档，顺序即严重度
    assert TRAY_MIN_LEVEL_DEFAULT == "warn"


def test_tray_queue_takes_worst_level_only():
    """队列只有一个级别：更轻的触发者被压制，不上屏也不排队（D17 硬要求 4）。"""
    q = tray_queue([_cand("a", "warn"), _cand("b", "err")], ["a", "b"])
    assert [c.plugin_id for c in q] == ["b"]


def test_tray_queue_keeps_peers_and_orders_by_plugin_order():
    """同一级里并列的全部留下，顺序 = `plugin_order`（不随机、不按名字）。"""
    q = tray_queue([_cand("b"), _cand("a"), _cand("c")], ["c", "b", "a"])
    assert [c.plugin_id for c in q] == ["c", "b", "a"]


def test_tray_queue_unknown_plugin_ids_keep_input_order():
    """不在 `plugin_order` 里的组件排到最后，且保持传入顺序（`sorted` 稳定）。"""
    q = tray_queue([_cand("x"), _cand("y")], [])
    assert [c.plugin_id for c in q] == ["x", "y"]


def test_tray_queue_threshold_tightens_but_err_always_shows():
    """阈值收到 `err` 时 warn 被压掉，但 **err 一律露出**（用户屏蔽不掉）。

    非法阈值按默认 `warn` 处理 —— 宁可多露一点，也不静默把可见性调低。
    """
    assert tray_queue([_cand("a", "warn")], ["a"], min_level="err") == ()
    q = tray_queue([_cand("a", "err"), _cand("b", "warn")], ["a", "b"], min_level="err")
    assert [c.plugin_id for c in q] == ["a"]
    assert [c.plugin_id for c in tray_queue([_cand("a", "warn")], ["a"], min_level="乱填")] == ["a"]


def test_tray_queue_broken_plugin_takes_immediate_err_path():
    """熔断走**即时通道**（用户裁定 2026-09-15）：不等 `stale_after` 超时。

    熔断不是「某个取值的作者意图」能表达的状态 ⇒ 级别与提醒意图都按 ERR 论，
    不收 `alerts` 与用户阈值的约束。
    """
    q = tray_queue(
        [_cand("a", level="ok", alerts=False, broken=True)], ["a"], min_level="err"
    )
    assert [c.plugin_id for c in q] == ["a"]


def test_tray_queue_consumes_resolved_states():
    """级别归**声明**（决议 D13）：喂进队列的是 `state_view.resolve` 的产物。

    锁住完整派生链「取值名 → 声明 → 级别 / 提醒意图 → 队列」，确保「翻译」全仓只有
    `resolve` 一处（聚合与渲染都不许自己回查声明）。
    """
    spec = StateSpec(
        plugin_id="p", key="main", initial="s",
        states=(StateOption("s", StateLevel.WARN, text="有事"),),
    )
    rs = resolve(spec, StateReport("s"))
    assert rs.level.value == "warn" and rs.alerts is True
    q = tray_queue([Candidate("p", rs.level.value, rs.alerts, rs.icon, rs.text)], ["p"])
    assert [c.plugin_id for c in q] == ["p"]


# ---------- representative（§4.3 每插件代表态） ----------


def test_representative_picks_worst_level():
    """主序 = 严重度：越坏越优先，与声明顺序无关（顺序只在同级时介入）。"""
    states = {
        "p.armed": _spec("p", "armed", StateLevel.WARN),
        "p.muted": _spec("p", "muted", StateLevel.IDLE),
    }
    reports = {
        "p.armed": StateReport("s"),
        "p.muted": StateReport("s"),
    }
    spec, report = representative("p", reports, states)
    assert spec.key == "armed" and resolve(spec, report).level is StateLevel.WARN


def test_representative_tie_breaks_by_declaration_order():
    """同级取**先声明**者：判据是声明顺序（＝作者写的关注度顺序），不是名字。

    键名**故意反着字典序**（`b` 声明在前、`a` 在后）——若有人把判据退回「按 key
    排序」，本用例立刻红。
    """
    states = {
        "p.b": _spec("p", "b", StateLevel.WARN),
        "p.a": _spec("p", "a", StateLevel.WARN),
    }
    reports = {
        "p.b": StateReport("s"),
        "p.a": StateReport("s"),
    }
    spec, _ = representative("p", reports, states)
    assert spec.key == "b"


def test_representative_priority_overrides_declaration_order():
    """用户配了优先级 ⇒ 同级平局改按**配置值**，不再无条件取先声明者。

    键名仍故意反字典序（``b`` 先声明）：赢的必须是「优先级数值小」的那台，
    而不是「名字排前」或「写在前面」的那台 —— 三者在这里给出不同答案。
    """
    states = {
        "p.b": _spec("p", "b", StateLevel.WARN),
        "p.a": _spec("p", "a", StateLevel.WARN),
    }
    reports = {"p.b": StateReport("s"), "p.a": StateReport("s")}
    spec, _ = representative("p", reports, states, priorities={"a": 0, "b": 1})
    assert spec.key == "a"


def test_representative_ignores_bad_priority_values():
    """写坏的优先级（布尔 / 字符串 / None）**回落声明序号**，不能被当成 0 抢走代表位。

    bool 是 int 的子类 —— 这里若放宽成 ``isinstance(v, int)``，用户手改歪的一行
    ``priority: true`` 会让某台机莫名置顶，而界面上看不出为什么。
    """
    states = {
        "p.b": _spec("p", "b", StateLevel.WARN),
        "p.a": _spec("p", "a", StateLevel.WARN),
    }
    reports = {"p.b": StateReport("s"), "p.a": StateReport("s")}
    for bad in (True, "0", None, 1.5):
        spec, _ = representative("p", reports, states, priorities={"a": bad})
        assert spec.key == "b", f"优先级 {bad!r} 不该改变同级下的代表态"


def test_representative_priority_never_outranks_severity():
    """优先级**只在同级时介入**：再高的关注度也压不过「更坏」。

    这是安全不变式 —— 若优先级能越过严重度，用户把一台 OK 的机置顶后，另一台 ERR
    就永远上不了标题栏与托盘，「出事一定看得见」当场失效。
    """
    states = {
        "p.calm": _spec("p", "calm", StateLevel.OK),
        "p.broken": _spec("p", "broken", StateLevel.ERR),
    }
    reports = {"p.calm": StateReport("s"), "p.broken": StateReport("s")}
    spec, _ = representative(
        "p", reports, states, priorities={"calm": 0, "broken": 9}
    )
    assert spec.key == "broken"


def test_representative_declaration_order_survives_registry_collection():
    """声明顺序必须原样穿过注册中心收集——代表态平局判据的直接前置条件。"""

    class _Two:
        id = "two"
        order = 0
        api_version = 3

        def state_specs(self):
            # notify 声明在前、display 在后：与字典序相反，只有保序收集才选得出 notify
            return (
                StateSpec(key="notify",
                          states=(StateOption("s", StateLevel.OK),)),
                StateSpec(key="display",
                          states=(StateOption("s", StateLevel.OK),)),
            )

    reg = ContributionRegistry([_Two()])
    states = {s.topic: s for s in reg.states()}
    reports = {t: StateReport("s") for t in states}
    spec, _ = representative("two", reports, states)
    assert spec.key == "notify"


def test_representative_isolated_per_plugin():
    states = {"q.main": _spec("q", "main", StateLevel.ERR)}
    reports = {"q.main": StateReport("s")}
    # 查 p 不应看到 q 的状态
    assert representative("p", reports, states) is None


def test_representative_none_without_declaration_or_snapshot():
    states = {"p.a": _spec("p", "a")}
    reports = {"p.a": StateReport("s")}
    # 无声明 → None（调用方决定兜底）
    assert representative("p", reports, {}) is None
    # 无报告 → None
    assert representative("p", {}, states) is None


# ---------- state_view.resolve（决议 D13 的单一解析点） ----------


def test_resolve_uses_declared_level_and_default_text():
    """级别与默认文案都取自取值声明：报告留空 ``summary`` 时用声明的 ``text``。"""
    spec = StateSpec(
        plugin_id="p", key="main", icon=IconName.GEAR,
        states=(StateOption("low", StateLevel.WARN, text="电量低"),),
    )
    rs = resolve(spec, StateReport("low"))
    assert (rs.level, rs.text, rs.declared) == (StateLevel.WARN, "电量低", True)


def test_resolve_summary_overrides_declared_text():
    """声明给默认文案，运行期 ``summary`` 可覆盖（决议 D13 的「动态文案覆盖」规则）。"""
    spec = StateSpec(
        plugin_id="p", key="main",
        states=(StateOption("low", StateLevel.WARN, text="电量低"),),
    )
    assert resolve(spec, StateReport("low", "电量 12%，插上电源")).text == "电量 12%，插上电源"


def test_resolve_undeclared_value_surfaces_as_err_with_the_value_name():
    """组件报了个没声明的取值 ⇒ 按 ERR 露出，**文案就是那个取值名**。

    裁定（2026-09-15）：不静默吞掉，也不许看不出是哪个名字写错了 ——
    这条分支刻意不看 ``summary``，否则拼错的名字会被一句「看起来正常」盖住。
    """
    spec = StateSpec(
        plugin_id="p", key="main", icon=IconName.GEAR,
        states=(StateOption("low", StateLevel.WARN, text="电量低"),),
    )
    rs = resolve(spec, StateReport("lwo", "看起来一切正常"))
    assert (rs.level, rs.text, rs.declared) == (StateLevel.ERR, "lwo", False)
    assert rs.icon is IconName.GEAR          # 图标回落主图标，不留空格
    # 取值名是空串时给个能认出来的占位，不留空文案
    empty = resolve(spec, StateReport(""))
    assert empty.text and empty.declared is False


def test_resolve_stale_is_framework_level_and_falls_back_to_main_icon():
    """失联是**框架**的判断、不是组件声明的取值：ERR + 固定文案 + 回落主图标。

    「用**过期的取值图标**才是撒谎」——所以失联时不许再画上一次那一枚。
    """
    spec = StateSpec(
        plugin_id="p", key="main", icon=IconName.GEAR,
        states=(StateOption("on", StateLevel.OK, IconName.DISPLAY_ON, "在线"),),
    )
    rs = resolve(spec, StateReport("on"), stale=True)
    assert (rs.level, rs.icon, rs.declared) == (StateLevel.ERR, IconName.GEAR, True)
    assert rs.text == "状态失联"
