"""core/variables/host 测试（变量池与任务解耦 · Phase 2 的接线层）。

对应设计文档 §2.3（订阅与传播）、§2.5（ask 的触发条件与询问登记）。这一层**只**负责
四件事 —— 订阅 / 节拍 / 广播 / ask 登记 —— 故每条用例都对着其中一件，另加一条把
「它与交互中枢之间的契约」钉住（``plugin_id`` 是框架，不随组件启停）。

替身都用**显式触发**的桩（总线不自动投递、心跳不给节拍、中枢不弹窗）：这样「哪一步
引发了哪一次广播」在用例里是看得见的一行，而不必去数真实定时器跑了几轮。
"""

from __future__ import annotations

import datetime

import pytest
import yaml

from barkit.core import interaction as core_interaction
from barkit.core.events import VARIABLES_CHANGED_TOPIC
from barkit.core.variables import host as host_mod
from barkit.core.variables.pool import (
    PLACEHOLDER_ASK_UNFILLED,
    PLACEHOLDER_COMPONENT_ABSENT,
    PLACEHOLDER_UNDEFINED,
    status_topic_of,
)

TODAY = "2026-10-10"


# ── 测试替身 ────────────────────────────────────────────────────────


class _Clock:
    """可控时钟：显式改 ``moment``。"""

    def __init__(self, moment: datetime.datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime.datetime:
        return self.moment


class _Bus:
    """总线桩：``publish`` 只记账，**不自动投递**（投递要显式 ``fire``）。

    自动投递会让「订阅面重挂得对不对」这类问题被掩盖：一条挂错的订阅照样能被
    ``publish`` 打到。显式投递则必须点名那条 topic。
    """

    def __init__(self) -> None:
        self.published: list = []
        self.handlers: "dict[str, list]" = {}

    def subscribe(self, topic, handler) -> None:
        self.handlers.setdefault(topic, []).append(handler)

    def unsubscribe(self, topic, handler) -> None:
        if handler in self.handlers.get(topic, []):
            self.handlers[topic].remove(handler)

    def publish(self, topic, payload=None) -> None:
        self.published.append((topic, payload))

    def fire(self, topic, payload=None) -> None:
        for handler in list(self.handlers.get(topic, [])):
            handler(payload)

    def topics(self) -> "set[str]":
        return {t for t, hs in self.handlers.items() if hs}

    def variables_events(self) -> list:
        """收到的 ``app.variables.changed``（值），按先后。"""
        return [p for t, p in self.published if t == VARIABLES_CHANGED_TOPIC]


class _Heartbeat:
    """心跳桩：只记订阅，节拍由用例显式敲。"""

    def __init__(self) -> None:
        self.tickers: "list[dict]" = []

    def add_ticker(self, interval, on_due, *, key="", phase=None):
        tk = {"key": key, "interval": interval, "on_due": on_due}
        self.tickers.append(tk)
        return tk

    def remove_ticker(self, ticker) -> None:
        if ticker in self.tickers:
            self.tickers.remove(ticker)


class _Center:
    """交互中枢桩：只记「谁登记了什么」，并按 ``plugin_id`` 模拟整批撤销。

    ``unregister(plugin_id)`` 必须模拟**真实语义**（按 ``plugin_id`` 撤），否则
    「询问不随组件启停」那条判据在用例里是自证的同义反复：一个什么也不做的桩
    当然撤不掉任何东西。
    """

    def __init__(self) -> None:
        self.rules: "dict[str, tuple]" = {}
        self.manual: list = []
        self.started = 0

    def register(self, spec, on_result=None, on_shown=None) -> None:
        # ``on_shown``（「提醒登场」）只有组件自己的提醒用得上；变量池登记的那条 ask 规则
        # 从来不给它 —— 收下即可（真中枢那边它是可选参数，见 ``shell.interaction``）。
        self.rules[spec.prompt.key] = (spec, on_result)

    def unregister_key(self, key) -> None:
        self.rules.pop(key, None)

    def unregister(self, plugin_id) -> None:
        for key in [k for k, (spec, _cb) in self.rules.items()
                    if spec.plugin_id == plugin_id]:
            self.rules.pop(key, None)

    def ask_now(self, spec, on_result=None) -> bool:
        self.manual.append((spec.prompt.key, on_result))
        return True

    def mark_started(self) -> None:
        self.started += 1


class _Report:
    """状态快照替身（变量池只取 ``state`` / ``summary`` 两个字段）。"""

    def __init__(self, state: str = "", summary: str = "") -> None:
        self.state = state
        self.summary = summary


def _decls() -> list:
    return [
        {"id": "scene", "name": "当前场景", "source": "component",
         "component": "route_guard", "key": "profile", "field": "summary"},
        {"id": "start_time", "name": "上班时间", "source": "ask", "type": "time",
         "trigger": {"when": ["startup"],
                     "conditions": [{"var": "scene", "op": "==", "value": "办公室"}]},
         "prompt": "今天几点上班？"},
        {"id": "off_time", "name": "下班时间", "source": "derived", "expr": "start_time + 9h"},
    ]


def _host(bus=None, beat=None, center=None, moment=None):
    clock = _Clock(moment or datetime.datetime(2026, 10, 10, 8, 27, 0))
    return host_mod.VariableHost(
        bus, beat, clock=clock, interaction=center
    )


# ── 订阅与传播（§2.3）────────────────────────────────────────────────


def test_the_first_pass_broadcasts_nothing():
    """装配那一刻不广播：消费方（设置页、面板）都还没建好，发出去只有日志看得见。"""
    bus, beat = _Bus(), _Heartbeat()
    h = _host(bus, beat)
    h.start(_decls())
    assert bus.variables_events() == []
    assert beat.tickers and beat.tickers[0]["key"] == "variables"


def test_only_declared_topics_are_subscribed():
    bus = _Bus()
    h = _host(bus)
    h.start(_decls())
    assert bus.topics() == {status_topic_of("route_guard", "profile")}


def test_a_component_report_flows_through_and_broadcasts_exactly_once():
    """快照到达 ⇒ 池拿得到值、广播一次；**同一份再来一次不广播**（比的是指纹）。"""
    bus = _Bus()
    h = _host(bus)
    h.start(_decls())
    topic = status_topic_of("route_guard", "profile")

    bus.fire(topic, _Report("active", "办公室"))
    assert h.pool.render("scene") == "办公室"
    assert len(bus.variables_events()) == 1

    bus.fire(topic, _Report("active", "办公室"))
    assert len(bus.variables_events()) == 1, "指纹没变却又广播了一次（消费方会白重画）"

    bus.fire(topic, _Report("active", "家"))
    assert len(bus.variables_events()) == 2


def test_a_report_on_a_topic_nobody_declared_is_ignored():
    bus = _Bus()
    h = _host(bus)
    h.start(_decls())
    h._on_status("plugin.other.status.x", _Report("on", "别的东西"))
    assert bus.variables_events() == []


def test_reloading_moves_the_subscription_surface():
    """声明改了 ⇒ 订阅面**整条重来**（旧 topic 上不许还挂着我们）。"""
    bus = _Bus()
    h = _host(bus)
    h.start(_decls())
    old = status_topic_of("route_guard", "profile")
    assert old in bus.topics()

    h.load([{"id": "vpn", "source": "component",
             "component": "vpn_guard", "key": "link", "field": "state"}])
    assert old not in bus.topics()
    assert status_topic_of("vpn_guard", "link") in bus.topics()


# ── 节拍（跨天归零的落点）──────────────────────────────────────────


def test_the_beat_opens_a_new_pass_so_yesterdays_answer_stops_counting():
    """一拍开一轮新的：跨天时昨天填的 ask 值**自动**不再算数（§2.5「到新的一天回到未填」）。

    这条不在调用方手里：没有任何一处「记得跨天要清」的代码 —— 清的理由只写在
    ``VariablePool.begin_pass`` 里，故宿主这一拍就是它唯一的执行点。
    """
    clock = _Clock(datetime.datetime(2026, 10, 10, 8, 27, 0))
    h = host_mod.VariableHost(_Bus(), _Heartbeat(), clock=clock)
    h.start(_decls())
    h.pool.begin_pass()
    assert h.pool.set_ask_value("start_time", "09:00") is True
    assert h.pool.ask_raw("start_time") == "09:00"

    clock.moment = datetime.datetime(2026, 10, 11, 8, 30, 0)
    h._on_beat()
    assert h.pool.ask_raw("start_time") == "", "跨天了，昨天那个值还在"


# ── ask 的登记（§2.5）────────────────────────────────────────────────


def test_ask_rules_are_framework_owned_and_survive_a_component_disable():
    """裁定③：ask 的询问**不随引用它的组件启停** —— 登记在框架名下。

    判据不是「登记了没」，而是「按组件 id 整批撤之后它还在不在」：登记在某个组件名下
    的规则，禁用那个组件时会被一笔撤走（那正是要避免的行为）。
    """
    center = _Center()
    h = _host(_Bus(), _Heartbeat(), center)
    h.start(_decls())
    key = host_mod.ask_key("start_time")
    assert key in center.rules

    spec, _cb = center.rules[key]
    assert spec.plugin_id == host_mod.FRAMEWORK_OWNER == ""
    center.unregister("todo")
    assert key in center.rules, "组件停用把框架级的询问也撤了"


def test_the_registered_prompt_prefills_from_the_pool_and_carries_no_preview():
    """浮层的预填问**池**（此刻的值），而不是登记时算好的快照；**preview 不挂**
    （2026-10-10 用户裁定：询问就是询问，不带试算 —— 弹「上班时间」写着
    「下班时间 17:27」会被读成这条变量在替人算下班）。"""
    center = _Center()
    h = _host(_Bus(), _Heartbeat(), center)
    h.start(_decls())
    spec, _cb = center.rules[host_mod.ask_key("start_time")]
    assert spec.prompt.title == "今天几点上班？"
    assert spec.prompt.initial() == ""
    assert spec.prompt.preview is None


def test_conditions_turn_into_a_predicate_watching_the_variables_topic():
    """条件那一路是**一个谓词** ＋ ``app.variables.changed`` 的订阅，而不是逐条翻成
    ``state_is``：变量条件比的是值（可能是派生算出来的），绕一层派生就翻不过去了。"""
    center = _Center()
    h = _host(_Bus(), _Heartbeat(), center)
    h.start([_decls()[1]])                       # 只留那条 ask（条件引用 scene）
    spec, _cb = center.rules[host_mod.ask_key("start_time")]

    kinds = [c.kind for c in spec.conditions]
    assert core_interaction.COND_ON_START in kinds     # when: [startup]
    assert core_interaction.COND_PREDICATE in kinds
    assert core_interaction.topics_of(spec) == {VARIABLES_CHANGED_TOPIC}

    pred = [c for c in spec.conditions if c.kind == core_interaction.COND_PREDICATE][0]
    assert pred.predicate() is False, "scene 还没有值 ⇒ 条件判不出来，按不成立"


def test_a_rule_that_would_fire_immediately_is_not_registered():
    """时机与条件都空 ⇒ 空条件集恒真、一登记就弹一次。不登记，并留一条日志。"""
    center = _Center()
    h = _host(_Bus(), _Heartbeat(), center)
    h.start([{"id": "x", "source": "ask", "type": "time", "trigger": {"when": []}}])
    assert center.rules == {}


def test_only_time_typed_asks_get_a_prompt():
    """浮层今天只有「填一个时刻」这一种问法（``core.interaction.PROMPT_TIME``）。

    其它类型登记进去等于弹一个没有输入框的窗 —— 跳过它，别把「不支持」演成「问了但填不了」。
    """
    center = _Center()
    h = _host(_Bus(), _Heartbeat(), center)
    h.start([{"id": "count", "source": "ask", "type": "number",
              "trigger": {"when": ["startup"]}}])
    assert center.rules == {}


def test_deleting_a_declaration_takes_its_rule_away():
    """用户删掉一条 ask 变量 ⇒ 它那条询问规则跟着走（不留孤儿规则）。"""
    center = _Center()
    h = _host(_Bus(), _Heartbeat(), center)
    h.start(_decls())
    key = host_mod.ask_key("start_time")
    assert key in center.rules

    h.load([d for d in _decls() if d["id"] != "start_time"])
    assert key not in center.rules


def test_registering_later_asks_re_arms_the_startup_latch():
    """运行期新加的 ask（``when: [startup]``）必须**当场就能问**。

    中枢那一位「程序已启动」是**一次性**的锁存位，装配收尾就置过 —— 不重跑一遍的话，
    新加 / 改过的变量要等到下次启动才会问，用户当场看不到任何反应。
    """
    center = _Center()
    h = _host(_Bus(), _Heartbeat(), center)
    h.start([])
    before = center.started

    h.load([_decls()[1]])
    assert center.started > before


# ── 作答与手动刷新 ───────────────────────────────────────────────────


def test_the_answer_is_written_to_the_daily_ledger_and_announced():
    """答案回写按日账，并**强制**广播一次（用户刚做的那一次必须看得见）。"""
    bus, center = _Bus(), _Center()
    h = _host(bus, _Heartbeat(), center)
    h.start(_decls())
    _spec, write = center.rules[host_mod.ask_key("start_time")]
    ba = len(bus.variables_events())

    write(core_interaction.InteractionResult(True, "09:00"))
    assert h.pool.ask_raw("start_time") == "09:00"
    assert h.pool.render("off_time") == "18:00"
    assert len(bus.variables_events()) == ba + 1


def test_a_dismissed_answer_writes_nothing():
    bus, center = _Bus(), _Center()
    h = _host(bus, _Heartbeat(), center)
    h.start(_decls())
    _spec, write = center.rules[host_mod.ask_key("start_time")]
    write(core_interaction.InteractionResult(False, None, core_interaction.RESULT_DISMISSED))
    assert h.pool.ask_raw("start_time") == ""
    assert bus.variables_events() == []


def test_the_manual_refresh_goes_through_a_temporary_rule():
    """「刷新询问」走中枢的 ``ask_now``（临时 key）—— **真 key 那笔账一个字不动**。

    账被共用的话，「点一次测试 ⇒ 今天真正的那次不弹了」，而用户只会判成这功能有病。
    """
    center = _Center()
    h = _host(_Bus(), _Heartbeat(), center)
    h.start(_decls())
    real = host_mod.ask_key("start_time")

    assert h.ask_now("start_time") is True
    assert [k for k, _cb in center.manual] == [real], "手动那次用的是真 key"
    assert real in center.rules


def test_the_manual_refresh_refuses_a_variable_that_cannot_be_asked():
    center = _Center()
    h = _host(_Bus(), _Heartbeat(), center)
    h.start(_decls())
    assert h.ask_now("off_time") is False       # 算出型，问不了
    assert h.ask_now("没有这个") is False


def test_without_an_interaction_center_nothing_is_registered_but_values_still_work():
    """没注入中枢（单测 / 轻量装配）⇒ 登记那一路整体不发生，池本身照常取值。"""
    bus = _Bus()
    h = _host(bus)
    h.start(_decls())
    bus.fire(status_topic_of("route_guard", "profile"), _Report("active", "办公室"))
    assert h.pool.render("scene") == "办公室"
    assert h.ask_now("start_time") is False


# ── 收尾 ────────────────────────────────────────────────────────────


def test_dispose_unsubscribes_and_releases_the_ticker():
    """收尾**两件都要**：撤订阅、摘心跳句柄。只做一件的症状分别是「还在收别人的信号」
    与「心跳还在每 30 秒叫一个已经退了的宿主」。"""
    bus, beat = _Bus(), _Heartbeat()
    h = _host(bus, beat)
    h.start(_decls())
    assert bus.topics()

    h.dispose()
    assert bus.topics() == set()
    assert beat.tickers == []


def test_declared_rows_put_the_builtins_last():
    h = _host(_Bus())
    h.start(_decls())
    rows = h.declared_rows()
    assert [row[0].id for row in rows] == ["scene", "start_time", "off_time",
                                           "now", "date", "weekday"]


def test_probe_answers_with_the_moment_it_is_asked_not_the_last_pass():
    """``probe`` 要**现算**，不是复读上一轮 ``tick`` 的结论。

    判据是**两次之间只有时钟变了**：先让列表那份把旧时刻算进缓存（对照组，它本来就只
    答上一轮），再推时钟 —— probe 必须答新的那个。少了 ``begin_pass`` 就会撞缓存，
    答回旧值（用户按「测试」的场景常常正是刚改完声明、还没到下一拍）。
    """
    clock = _Clock(datetime.datetime(2026, 10, 10, 8, 27, 0))
    h = host_mod.VariableHost(None, None, clock=clock)
    h.start(_decls())
    assert h.probe("now") == "08:27"

    clock.moment = datetime.datetime(2026, 10, 10, 9, 27, 0)
    cached = {spec.id: value for spec, value in h.declared_rows()}
    assert cached["now"] == "08:27", "对照组：列表那一份读的就是上一轮的缓存"
    assert h.probe("now") == "09:27"


def test_probe_says_why_a_value_is_missing_instead_of_an_empty_string():
    """取不到值时答的是**为什么**（三个词各不相同），不是一个空串。

    空串会被读成「这个变量就是空的」，而三种情形的下一跳完全不同：还得问 / 等组件 /
    配置写错了（词表见 ``pool.PLACEHOLDER_*``）。
    """
    h = _host()
    h.start(_decls())
    assert h.probe("start_time") == PLACEHOLDER_ASK_UNFILLED
    assert h.probe("off_time") == PLACEHOLDER_UNDEFINED
    assert h.probe("scene") == PLACEHOLDER_COMPONENT_ABSENT


# ── 配置写口（``Config.set_variables``）──────────────────────────────


def _config(tmp_path):
    """一份隔离的 Config：出厂层是一份空文件（**别用仓库那份** —— 它带一条 ``scene``，
    会让「写进去读回来」被默认值污染）。"""
    from barkit.core.config import Config

    default = tmp_path / "default.yaml"
    default.write_text("{}\n", encoding="utf-8")
    return Config(user_path=tmp_path / "config.yaml", default_path=default)


def test_set_variables_round_trips_through_the_user_file(tmp_path):
    cfg = _config(tmp_path)
    cfg.set_variables([{"id": "a", "source": "derived", "expr": "1h"}])
    assert [v["id"] for v in cfg.variables()] == ["a"]
    assert "variables" in (tmp_path / "config.yaml").read_text(encoding="utf-8")


def test_set_variables_with_an_empty_list_removes_the_key(tmp_path):
    """空表 ⇒ **删键**（不留 ``variables: []`` 这种残渣，与显示位那条同口径）。"""
    cfg = _config(tmp_path)
    cfg.set_variables([{"id": "a", "source": "derived", "expr": "1h"}])
    cfg.set_variables([])
    assert cfg.variables() == []
    raw = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8")) or {}
    assert "variables" not in raw


def test_set_variables_rejects_a_bad_section_without_touching_the_disk(tmp_path):
    """坏声明（id 重复）⇒ ``ConfigError``，且**盘上那份没动**（先校验再落盘）。"""
    from barkit.core.config import ConfigError

    cfg = _config(tmp_path)
    cfg.set_variables([{"id": "a", "source": "derived", "expr": "1h"}])
    before = (tmp_path / "config.yaml").read_text(encoding="utf-8")

    with pytest.raises(ConfigError):
        cfg.set_variables([{"id": "a"}, {"id": "a"}])
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == before


def test_set_variables_never_carries_a_running_value(tmp_path):
    """写回去的永远只是声明：用户今天填过的值不进配置（声明与值分离这条硬约束）。"""
    cfg = _config(tmp_path)
    cfg.set_variables([{"id": "start_time", "source": "ask", "type": "time",
                        "trigger": {"when": ["daily"]}}])
    item = cfg.variables()[0]
    assert "value" not in item
    assert item["trigger"] == {"when": ["daily"]}
