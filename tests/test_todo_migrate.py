"""tests/test_todo_migrate：合并方案**第 4 批（迁移）**的判据。

这一批把合并前那套东西搬进合并后的组件：``plugins.clock`` 段并进 ``plugins.todo``
（方案 §6.1），两份按天记账文件并成一份（§6.2）。守的是四处**会静默出错**的地方：

- **映射是四型逐型的**：旧 ``type`` 认错一个，那一条要么变成另一型、要么永远不登场
  （新内核按字段推导窗口，不看 ``type``）。四型各自的「旧字段 → 新字段」都要有一条；
- **幂等**：迁移要写两次盘（写自己段、删旧段）且不能是一个事务，中途断掉会重跑 ——
  重跑多出一份重复条目比少搬一条更难收拾；
- **标量键的让位**：``size`` / ``close_gesture`` 只在本组件**没设过**时接过来，
  否则用户在本组件里挑的那档会被旧组件那份悄悄盖掉；
- **当日值接得上**：条目 id 一个字不动 ⇒ 账的键天然对得上（改名会让用户看到
  「迁移完今天的上班时间没了」）。

端到端那两条（``start`` 与「当日值还在」）走真 ``Plugin``；映射细节走
``migrate`` 的纯函数 —— 它们是同一个函数，只是少绕两圈。
"""

from __future__ import annotations

import json
from datetime import date, datetime

import yaml

from barkit import paths
from barkit.core.config import DEFAULT_CONFIG_PATH, Config
from barkit.core.interaction import PROMPT_CHOICE, PROMPT_TIME
from barkit.core.plugin import PluginContext
from barkit.core.variables import VariablePool
from barkit.plugins.todo import day_log, detail_view, migrate
from barkit.plugins.todo import plugin as plugin_mod
from barkit.plugins.todo import scheduler
from barkit.plugins.todo import task as task_mod
from barkit.plugins.todo.plugin import Plugin
from barkit.plugins.todo.scheduler import (
    _hhmm_to_sec,
    _seconds_since_midnight,
    _span_of,
    _valid_hhmm,
    compute_display,
)

CLOCK = migrate.LEGACY_PLUGIN_ID


def _entry(**kw) -> dict:
    """一条旧 ``clock`` 条目（形状照用户盘上那份抄，字段是旧设置面写出来的全集）。"""
    base = {
        "id": "clk1",
        "type": "fixed",
        "enabled": True,
        "text": "",
        "before_min": 0,
        "before_sec": 0,
        "after_min": 0,
        "after_sec": 0,
        "time": "11:40",
        "interval_min": 60,
        "work_start": "09:00",
        "work_hours": 9,
        "work_min": 25,
        "break_min": 5,
        "mode": "timer",
    }
    base.update(kw)
    return base


def _user_config(tmp_path, payload: dict) -> Config:
    path = tmp_path / "user.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return Config(user_path=path)


def _plugin(tmp_path, payload: dict | None = None) -> Plugin:
    ctx = PluginContext(config=_user_config(tmp_path, payload or {}), events=None)
    return Plugin(ctx)


def _with_clock(entries, **section) -> dict:
    return {"plugins": {CLOCK: dict(section, entries=list(entries))}}


def _write_data(name: str, days: dict) -> None:
    (paths.APP_SUPPORT / name).write_text(
        json.dumps({"days": days}, ensure_ascii=False), encoding="utf-8"
    )


def _read_data(name: str) -> dict:
    raw = json.loads((paths.APP_SUPPORT / name).read_text(encoding="utf-8"))
    return raw.get("days") or {}


# ── 映射：旧四型各落一处（方案 §2.3） ──────────────────────────────

def test_fixed_entry_becomes_a_literal_clock_item():
    items = migrate.migrate_items([], [_entry()])
    assert len(items) == 1
    item = items[0]
    assert (item["id"], item["at"], item["face"]) == ("clk1", "11:40", "clock")
    assert item["ask"] == "", "固定时刻条目不问任何东西"
    assert item["end"] == "11:40", "没填 after ⇒ 到点即退场（与旧窗口右端同一个时刻）"


def test_fixed_entry_extra_seconds_round_up_so_the_window_never_shrinks():
    """秒级余量一律**向上**凑整：旧窗口 40 秒，新模型给不出秒，也别给成 0 秒。

    这个方向是刻意的 —— 一条「到点提醒你看一眼」的条目，窗口塌成 0 秒等于它不存在。
    """
    items = migrate.migrate_items([], [_entry(before_sec=30, after_sec=10)])
    item = items[0]
    assert item["before_min"] == 1, "提前 30 秒 ⇒ 向上凑整成 1 分钟"
    assert item["end"] == "11:41", "到点后 10 秒 ⇒ 向上凑整成 1 分钟"


def test_loop_entry_keeps_its_interval():
    items = migrate.migrate_items([], [_entry(type="loop", interval_min=45)])
    item = items[0]
    assert item["interval_min"] == 45
    assert item["at"] == "", "循环条的登场时刻是算出来的整点，没有字面值"
    assert item["face"] == "clock"


def test_a_loop_entry_without_an_interval_cannot_be_moved():
    """没有间隔的循环条永远不登场（旧内核同样给空窗口）⇒ 不留一条哑条目在表里。"""
    assert migrate.migrate_items([], [_entry(type="loop", interval_min=0)]) == []


def test_pomodoro_entry_becomes_a_repeating_countdown():
    items = migrate.migrate_items([], [_entry(type="pomodoro", work_min=25, break_min=5)])
    item = items[0]
    assert item["interval_min"] == 30, "一轮 = 工作 ＋ 休息"
    assert item["face"] == "countdown", "常驻计时那一支钟面走倒计时"


def test_pomodoro_reminder_mode_gets_the_clock_face():
    items = migrate.migrate_items(
        [], [_entry(type="pomodoro", work_min=25, break_min=5, mode="reminder")]
    )
    assert items[0]["face"] == "clock", "到点提醒那一支的钟面是实时钟"


def test_derived_ask_entry_keeps_only_the_hours():
    """「每天问我」那一支：只留时长 ＋ 问面，**兜底上班时间丢掉**（方案 §8）。"""
    items = migrate.migrate_items([], [
        _entry(id="clk2", type="derived", work_start_source="ask",
               work_start="08:30", work_hours=10.5, before_min=8, ask_scene="office",
               ask_initial="offset", ask_initial_offset=-11),
    ])
    item = items[0]
    assert item["work_hours"] == 10.5
    assert item["ask"] == PROMPT_TIME, "问法由「它是一条时间问答」决定"
    assert item["scene"] == "office"
    assert item["at"] == "" and item["end"] == "", "不答就没有窗口（不靠兜底值启动）"
    assert item["face"] == "countdown"
    assert item["before_min"] == 8
    assert "ask_initial" not in item and "work_start" not in item, "旧询问偏好没有新格子"


def test_derived_fixed_entry_becomes_a_plain_countdown():
    """「固定时间」那一支并进普通倒计时条目（字面 at ＋ 字面 end），不再单列。

    **窗口以下班时刻为中心**：旧窗口是 ``[下班 － 提前量, 下班 ＋ 延后]``，``{到点}``
    报的也是下班时刻。落成 ``at = 上班`` 会让这一行写成「下班时间09:00」，并从上班
    一直挂到下班。
    """
    items = migrate.migrate_items([], [
        _entry(type="derived", work_start="09:00", work_hours=9, before_min=8),
    ])
    item = items[0]
    assert (item["at"], item["end"]) == ("18:00", "18:00")
    assert item["before_min"] == 8, "提前量是这个窗口唯一的宽度来源"
    assert item["ask"] == "", "写死上班时间的条目不问"
    assert item["face"] == "countdown"


def test_ask_or_not_does_not_change_the_shape_of_a_derived_item():
    """同一条「推算下班」，问不问只该改 ``ask`` —— **算出来的窗口**必须一样。

    判据落在内核上、不落在字面字段上：问的那一支 ``at`` 本来就是空的（时刻是当日答案
    算出来的），比字面值恒不等。要比的是「同一天答了 09:00 之后，两支各给什么窗口」——
    两支各写一遍落位是这一批最容易分叉的地方（内核算一支、``migrate`` 算另一支）。
    """
    common = dict(type="derived", work_start="09:00", work_hours=9, before_min=8)
    asked = migrate.migrate_items(
        [], [_entry(id="a", work_start_source="ask", **common)]
    )[0]
    fixed = migrate.migrate_items([], [_entry(id="b", **common)])[0]
    assert asked["ask"] == PROMPT_TIME and fixed["ask"] == ""

    noon = _seconds_since_midnight(datetime(2026, 10, 8, 12, 0))
    expected = (18 * 3600, 18 * 3600)          # 18:00 那一刻，窗口两端同一点
    assert _span_of(asked, noon, {"a": "09:00"}) == expected
    assert _span_of(fixed, noon, {}) == expected


def test_derived_without_hours_cannot_be_moved():
    assert migrate.migrate_items([], [_entry(type="derived", work_hours=0)]) == []


def test_no_plain_clock_entry_starts_asking():
    """**旧 ``clock`` 的条目一条都不问** —— 四型各来一条，``ask`` 全是空串。

    旧内核只给「``derived`` ＋ ``work_start_source == "ask"``」登记询问；照新模型的
    ``choice`` 缺省落下去，每一条报时条目都会从迁移那天起每天准点弹一个「完成」框。
    """
    items = migrate.migrate_items([], [
        _entry(id="clk1"),
        _entry(id="clk2", type="loop", interval_min=45),
        _entry(id="clk3", type="pomodoro", work_min=25),
        _entry(id="clk4", type="derived", work_start="09:00", work_hours=9),
    ])
    assert [i["ask"] for i in items] == ["", "", "", ""]


def test_a_disabled_entry_stays_disabled():
    """关着的条目搬完还得关着 —— 搬迁顺手把用户关掉的那条打开，是它会自己动起来。"""
    items = migrate.migrate_items([], [_entry(enabled=False)])
    assert items[0]["enabled"] is False


def test_the_scene_gate_is_carried_over():
    """``ask_scene`` → ``scene``：它在旧内核里**既管询问也管显示**（同义的场景门禁）。"""
    items = migrate.migrate_items([], [_entry(ask_scene="office")])
    assert items[0]["scene"] == "office"


def test_a_migrated_row_has_no_action_buttons():
    """旧浮层的行是纯文本（没 id、点不了）⇒ 新行也不带按钮，别凭空长出一枚「完成」。"""
    items = migrate.migrate_items([], [_entry()])
    assert items[0]["actions"] == []


def test_an_entry_without_an_id_cannot_be_moved():
    """旧设置页在保存时才补 id ⇒ 盘上真有可能有一条没 id 的；新内核算不出它的窗口。"""
    assert migrate.migrate_items([], [_entry(id=""), _entry(id=None)]) == []


def test_plain_text_lands_in_the_title_only():
    """不带占位符的那句话两栏都给会让它在浮层上出现两遍。"""
    items = migrate.migrate_items([], [_entry(text="喝水")])
    assert (items[0]["title"], items[0]["text"]) == ("喝水", "")


def test_text_with_a_placeholder_keeps_both_columns():
    """带占位符的：``title`` 取说得出的那半，``text`` 原样留着让内核去渲染时刻。"""
    items = migrate.migrate_items([], [_entry(text="下班时间{到点}")])
    assert (items[0]["title"], items[0]["text"]) == ("下班时间", "下班时间{到点}")


# ── 幂等 ────────────────────────────────────────────────────────────

def test_running_the_migration_twice_changes_nothing(tmp_path):
    payload = _with_clock([_entry(), _entry(id="clk2", type="loop", interval_min=45)])
    plugin = _plugin(tmp_path, payload)
    plugin._migrate_legacy()
    first = plugin.items()
    plugin._migrate_legacy()
    assert plugin.items() == first, "重跑（写自己段之后、删旧段之前断过）不得多出条目"


def test_an_id_that_is_already_in_the_table_is_skipped():
    """盘上已经有一条同 id 的 ⇒ 跳过，不再搬一份（两种来源在盘上分不开）。"""
    existing = [{"id": "clk1", "at": "08:00", "title": "用户自己那条"}]
    items = migrate.migrate_items(existing, [_entry()])
    assert len(items) == 1 and items[0]["title"] == "用户自己那条"


def test_old_items_get_an_explicit_ask():
    """方案 §6.1：``items`` 里没有 ``ask`` 的补显式 ``choice``，其余一个字不动。"""
    items = migrate.migrate_items([{"id": "td1", "at": "09:30", "title": "喝水"}], None)
    assert items[0]["ask"] == PROMPT_CHOICE
    assert items[0]["title"] == "喝水" and items[0]["at"] == "09:30"


def test_items_that_already_declare_ask_are_untouched():
    items = migrate.migrate_items([{"id": "td1", "ask": PROMPT_TIME}], None)
    assert items[0]["ask"] == PROMPT_TIME


# ── 标量键：只在本组件没设过时接过来 ────────────────────────────────

def test_scalar_keys_are_carried_over(tmp_path):
    plugin = _plugin(tmp_path, _with_clock([], size="small", close_gesture="double_click"))
    plugin._migrate_legacy()
    assert plugin.get_size() == "small"
    assert plugin.get_close_gesture() == "double_click"


def test_scalar_keys_the_component_already_owns_win(tmp_path):
    """用户在本组件里挑过那一档 ⇒ 旧组件那份不许盖回去。"""
    payload = _with_clock([], size="small")
    payload["plugins"]["todo"] = {"size": "large"}
    plugin = _plugin(tmp_path, payload)
    plugin._migrate_legacy()
    assert plugin.get_size() == "large"


def test_a_scalar_key_equal_to_the_default_is_not_written(tmp_path):
    """等于声明默认值不落键（与 ``cfg.set`` 同口径）—— 落下去就把默认值钉死在这一档。"""
    plugin = _plugin(tmp_path, _with_clock([], size="medium"))
    plugin._migrate_legacy()
    assert "size" not in plugin.ctx.config.user_plugin_section("todo")


def test_the_old_section_is_removed_once_moved(tmp_path):
    plugin = _plugin(tmp_path, _with_clock([_entry()]))
    plugin._migrate_legacy()
    assert plugin.ctx.config.user_plugin_section(CLOCK) == {}, (
        "搬完还留着旧段 ⇒ 下次启动再搬一遍"
    )


def test_without_a_legacy_section_the_file_is_not_touched(tmp_path):
    """绝大多数盘上没有旧段 ⇒ 一个字节都不该写（白写会把文件监视那条路叫醒一次）。"""
    config = _user_config(tmp_path, {"plugins": {"todo": {"items": []}}})
    before = (tmp_path / "user.yaml").read_bytes()
    plugin = Plugin(PluginContext(config=config, events=None))
    plugin._migrate_legacy()
    assert (tmp_path / "user.yaml").read_bytes() == before


def test_a_failing_config_write_does_not_escape(tmp_path, monkeypatch, caplog):
    """迁移失败只意味着「这次还是老样子」：它不许把组件拉不起来，**更不许删旧段**。

    删旧段那一步排在写自己段的**后面**（见 ``Plugin._migrate_config``）—— 写没成功就
    删掉旧的，等于把用户那批条目直接扔掉（新段里没有、旧段里也没了）。
    """
    plugin = _plugin(tmp_path, _with_clock([_entry()]))

    def _boom(_fn):
        raise RuntimeError("盘满了")

    monkeypatch.setattr(type(plugin.cfg), "mutate", lambda self, fn: _boom(fn))
    with caplog.at_level("ERROR", logger="barkit.plugins.todo.plugin"):
        plugin._migrate_legacy()  # 不抛

    assert "配置那一半没做完" in caplog.text, "得真走到接住异常那一支，不是碰巧没走到"
    assert plugin.items() == [], "写失败 ⇒ 条目一条都没搬进来"
    assert plugin.ctx.config.user_plugin_section(CLOCK), (
        "写没成功就删旧段 ＝ 用户那批条目直接没了"
    )


# ── 数据文件：两份并一份 ────────────────────────────────────────────

def test_both_legacy_files_merge_into_one_and_are_removed(tmp_path):
    _write_data("clock_workday.json", {"2026-10-08": {"clk2": "08:34"}})
    _write_data("todo_log.json", {"2026-10-08": {"td1": "09:12"}})
    assert day_log.merge_legacy() is True

    merged = _read_data(day_log.AGENDA_FILENAME)
    assert merged["2026-10-08"] == {"clk2": "08:34", "td1": "09:12"}
    assert not (paths.APP_SUPPORT / "clock_workday.json").exists()
    assert not (paths.APP_SUPPORT / "todo_log.json").exists()


def test_merging_twice_is_a_no_op(tmp_path):
    _write_data("todo_log.json", {"2026-10-08": {"td1": "09:12"}})
    assert day_log.merge_legacy() is True
    assert day_log.merge_legacy() is False, "旧文件已经没了 ⇒ 什么都不做"


def test_the_merged_file_wins_over_the_legacy_ones(tmp_path):
    """已经迁到一半：新文件里那份才是新的，旧文件只补空缺。"""
    _write_data(day_log.AGENDA_FILENAME, {"2026-10-08": {"td1": "10:00"}})
    _write_data("todo_log.json", {"2026-10-08": {"td1": "09:12", "td2": "09:30"}})
    day_log.merge_legacy()
    assert _read_data(day_log.AGENDA_FILENAME)["2026-10-08"] == {
        "td1": "10:00", "td2": "09:30",
    }


def test_a_corrupt_legacy_file_does_not_block_the_merge(tmp_path):
    """旧文件坏了 ⇒ 当空，但仍然要删掉它（不删的话每次启动都重试同一件做不成的事）。"""
    (paths.APP_SUPPORT / "todo_log.json").write_text("{不是 JSON", encoding="utf-8")
    _write_data("clock_workday.json", {"2026-10-08": {"clk2": "08:34"}})
    assert day_log.merge_legacy() is True
    assert _read_data(day_log.AGENDA_FILENAME) == {"2026-10-08": {"clk2": "08:34"}}
    assert not (paths.APP_SUPPORT / "todo_log.json").exists()


# ── 端到端：当日值接得上 ────────────────────────────────────────────

def test_todays_values_survive_the_move(tmp_path):
    """条目 id 一个字不动 ⇒ 旧账的键天然对得上（改过名就会落在一个不存在的 id 上）。

    旧文件那一份按**今天**的日期落键（迁移把当日值并进 agenda_log 后，``day_value_of``
    按今天读），写死某个具体日期就会随系统日期推移变成「昨天的值」⇒ 这条在
    2026-10-09 那次跑时就是一颗日期炸弹。故这里用 ``date.today()`` 而不是字面日期。
    """
    _write_data("clock_workday.json", {date.today().isoformat(): {"clk2": "08:34"}})
    plugin = _plugin(tmp_path, _with_clock([
        _entry(id="clk2", type="derived", work_start_source="ask", work_hours=10.5),
    ]))
    plugin._migrate_legacy()
    assert [i["id"] for i in plugin.items()] == ["clk2"]
    assert plugin.day_value_of("clk2") != "", "迁移完今天的上班时间还在"


def test_start_runs_the_migration(qtbot, tmp_path):
    """``start()`` 里跑的是**两代串着**：``clock`` 段先并进 ``items``（第一代），再翻成
    ``tasks``（第二代，§5）。这里看第二代的产物 —— 条目 id 一个字不动是最关键的那一格。"""
    plugin = _plugin(tmp_path, _with_clock([_entry()]))
    plugin.start()
    try:
        assert [s.id for s in plugin.task_specs()[0]] == ["clk1"]
        assert plugin.ctx.config.user_plugin_section(CLOCK) == {}
    finally:
        plugin.stop()


# ── 内核口径：编辑器放行的输入，内核一定读得出来 ────────────────────

def test_the_editor_accepts_a_subset_of_what_the_kernel_reads():
    """两个方向的缺口都要堵住：编辑器放行的值读不出来 ⇒ 界面说能存、实际永不触发。"""
    assert _valid_hhmm("9:30") is True and _hhmm_to_sec("9:30") is not None
    assert _valid_hhmm("11:40:10") is False, "带秒的编辑器不放行（字段表说的是 HH:MM）"
    assert _hhmm_to_sec("11:40:10") is not None, "但内核得读得出来（宽容的一侧）"
    assert _valid_hhmm("25:00") is False and _hhmm_to_sec("25:00") is None


# ── 第二代：旧条目表 → 变量 ＋ 任务（设计文档 §5） ──────────────────────

def _item(**kw) -> dict:
    """一条旧模型的条目（十三格照 ``scheduler.item_defaults`` 给全）。"""
    base = {
        "id": "td1",
        "enabled": True,
        "title": "喝水",
        "text": "",
        "at": "",
        "end": "",
        "before_min": 0,
        "interval_min": 0,
        "work_hours": 0,
        "face": "",
        "actions": [],
        "ask": PROMPT_CHOICE,
        "scene": "",
    }
    base.update(kw)
    return base


def _migrate(*items, declared=()):
    return migrate.migrate_to_tasks(list(items), declared)


def _task(**kw) -> dict:
    return _migrate(_item(**kw)).tasks[0]


def _by_id(rows, wanted: str) -> dict:
    found = [r for r in rows if isinstance(r, dict) and r.get("id") == wanted]
    assert len(found) == 1, f"{wanted} 命中 {len(found)} 条：{[r.get('id') for r in rows]}"
    return found[0]


def _display_of(migration, moment, *, asked: str = ""):
    """迁移结果 → 新内核那一份显示态（与旧内核 :func:`compute_display` 对拍用）。

    模拟装配层那两步：先按新声明装池（``pool.load``），再把「今天填的时刻」喂进去。
    """
    pool = VariablePool(clock=lambda: moment)
    pool.load([dict(v) for v in migration.variables])
    if asked:
        assert pool.set_ask_value("start_time", asked) is True
    pool.begin_pass()
    specs, problems = task_mod.parse_all([dict(t) for t in migration.tasks])
    assert problems == (), problems
    items = [task_mod.resolve_item(s, pool, moment) for s in specs]
    return compute_display(items, moment)


# ---- 字面时刻那一型 ----

def test_a_choice_item_becomes_a_literal_time_task():
    task = _task(at="09:30", end="17:30", before_min=10, face="clock",
                 actions=["done", "snooze"])
    assert (task["id"], task["title"]) == ("td1", "喝水")
    assert task["display"] == {
        "show": "clock",
        "reminder": {"at": "09:30"},
        "lead_min": 10,
        "trail_min": 480,
        "buttons": ["完成", "稍后"],
    }
    assert "effective" not in task, "没有场景门禁时不留空的 effective 段"


def test_an_item_that_does_not_ask_gets_no_card():
    """``ask`` 空 ＝ 只开显示面的条目（旧内核从来不为它登记规则）。"""
    task = _task(at="09:30", end="10:00", ask="")
    assert task["display"]["reminder"] == {"at": "09:30"}


def test_an_item_without_an_end_reaches_the_end_of_the_day():
    """旧：空 ``end`` ＝ 一直挂到当天末（23:59:59）。新模型只有相对量 ⇒ 按分钟折算。

    23:59:60 这个时刻不存在，故只能停在 23:59（短不到一分钟）。这里把那个数钉住：
    它同时是「退场点守恒」与「浮层那一行的时刻标签会变成 23:59」两件事的来源。
    """
    task = _task(at="09:30", face="clock")
    assert task["display"]["trail_min"] == 869


def test_a_disabled_item_stays_disabled():
    assert _task(enabled=False, at="09:30")["enabled"] is False
    assert "enabled" not in _task(at="09:30"), "等于默认值的那一格不写（只剩差异项）"


def test_a_window_that_crosses_midnight_keeps_its_length():
    """``at: 23:00 / end: 01:00`` 在旧内核里是**跨午夜**的窗口（``_inside_window`` 按
    那个圆算），折算 ``trail_min`` 也得走同一个圆 —— 直接相减会得到 −22 小时。"""
    assert _task(at="23:00", end="01:00")["display"]["trail_min"] == 120


def test_a_broken_interval_does_not_stop_the_migration():
    """手写的 ``interval_min: "半小时"`` 会让旧内核 ``int()`` 当场抛；迁移这一侧宽容
    到底（按 0 算）—— 整张表搬不动比那一格按 0 算糟得多。"""
    task = _task(interval_min="半小时", at="09:30")
    assert task["display"]["reminder"] == {"at": "09:30"}
    assert "effective" not in task


# ---- 循环那一型 ----

def test_an_interval_item_keeps_its_rhythm_and_its_stop_time():
    """循环条的 ``end`` 是「今天循环到几点为止」（不是退场时刻）⇒ 落 ``window.to``。

    ``window.from`` 只能补 ``00:00``：旧模型没有「生效起点」这个概念，而新模型的时段
    要求两端都写。
    """
    task = _task(interval_min=60, end="20:00", face="clock", actions=["snooze"])
    assert task["effective"] == {
        "loop": {"kind": "interval", "every": "60m"},
        "window": {"from": "00:00", "to": "20:00"},
    }
    assert task["display"] == {"show": "clock", "buttons": ["稍后"]}


def test_an_interval_item_without_a_stop_time_has_no_window():
    assert "window" not in _task(interval_min=30)["effective"]


def test_a_stop_time_that_is_not_a_time_is_dropped():
    assert "window" not in _task(interval_min=30, end="25:99")["effective"]


# ---- 询问那一型（变量） ----

def test_an_ask_time_item_becomes_two_variables_and_a_shared_reminder():
    migration = _migrate(_item(id="td2", ask=PROMPT_TIME, work_hours=9, before_min=30,
                               face="countdown", title="上班时间", text="{到点} 前提交"))
    assert [v["id"] for v in migration.variables] == ["start_time", "off_time"]
    start = _by_id(migration.variables, "start_time")
    assert start["trigger"] == {"when": ["startup"], "only_if_unfilled": True}
    assert (start["name"], start["prompt"], start["type"]) == ("上班时间", "今天几点上班？", "time")
    assert _by_id(migration.variables, "off_time")["expr"] == "start_time + 540m"

    task = _by_id(migration.tasks, "td2")
    assert task["display"] == {
        "show": "countdown",
        "reminder": {"at": "{off_time}"},
        "lead_min": 30,
    }, "钟面并进了「显示」那一档（旧两键 overlay / clock 已退场）"
    # 要搬的账落到**问的那一个**变量上：``off_time`` 是算出来的，往它里面写一个值会被
    # 下一次求值覆盖掉（而「今天填过的时刻」正是一笔**输入**，不是产物）。
    assert migration.answer_moves == (("td2", "start_time"),)


def test_the_ask_variable_is_named_after_what_it_asks_not_after_the_task():
    """询问变量的名字与问句**不取旧条目的标题**（2026-10-10 用户报）。

    旧条目十有八九叫「下班提醒 / 下班时间」—— 那是它**提醒**的那件事，而这条变量问的是
    **上班时刻**。拿标题当名字 ⇒ 界面上出现一条叫「下班时间」的**询问**变量，而真正由它
    算出来的派生变量也叫「下班时间」：两条同名，该答的那条还挂着错的语义
    （用户原话：「询问变量应该是『上班时间』，而不是『下班时间』」）。

    上一条用例的旧条目**标题恰好就叫「上班时间」**，把这个洞整个盖住了 —— 判据必须用一个
    标题与语义不同的条目来写。
    """
    migration = _migrate(_item(id="td2", ask=PROMPT_TIME, work_hours=9, title="下班提醒"))
    start = _by_id(migration.variables, "start_time")
    off = _by_id(migration.variables, "off_time")
    assert (start["name"], start["prompt"]) == ("上班时间", "今天几点上班？")
    assert off["name"] == "下班时间"
    assert off["source"] == "derived" and start["source"] == "ask"
    assert off["expr"] == "start_time + 540m"


def test_the_same_work_hours_share_one_derived_variable():
    """用户原始痛点就在这一条：两条任务问一次、算一次、处处用。"""
    migration = _migrate(
        _item(id="a", ask=PROMPT_TIME, work_hours=9),
        _item(id="b", ask=PROMPT_TIME, work_hours=9),
    )
    assert [v["id"] for v in migration.variables] == ["start_time", "off_time"]
    for tid in ("a", "b"):
        assert _by_id(migration.tasks, tid)["display"]["reminder"]["at"] == "{off_time}"


def test_different_work_hours_get_their_own_derived_variable():
    """§5：不要硬塞成单一共享 ``off_time``（那会丢失差异）。"""
    migration = _migrate(
        _item(id="a", ask=PROMPT_TIME, work_hours=9),
        _item(id="b", ask=PROMPT_TIME, work_hours=10.5),
    )
    assert [v["id"] for v in migration.variables] == [
        "start_time", "off_time_9", "off_time_10_5",
    ]
    assert _by_id(migration.variables, "off_time_10_5")["expr"] == "start_time + 630m"
    assert _by_id(migration.tasks, "a")["display"]["reminder"]["at"] == "{off_time_9}"
    assert _by_id(migration.tasks, "b")["display"]["reminder"]["at"] == "{off_time_10_5}"


def test_the_minutes_mirror_the_kernels_own_formula():
    """``8.7h`` 必须折算成 ``521m`` ＝ 内核 ``int(8.7 * 3600) // 60``。

    这是**唯一**能把「照抄内核那一步」与「就地写 ``int(hours * 60)``」分开的输入：后者在
    ``8.7 * 60 == 522.0`` 上得 ``522`` —— 下班时刻**晚一分钟**，而且只在 ``4.35 / 8.7 /
    8.95`` 这几个值上晚（``7.5`` 这类整半两式一致，拿它当输入这条用例恒绿：改回旧式子
    照样报通过）。
    """
    migration = _migrate(_item(ask=PROMPT_TIME, work_hours=8.7))
    assert _by_id(migration.variables, "off_time")["expr"] == "start_time + 521m"

    # 与内核那一侧对拍：09:00 上班 ＋ 8.7h ⇒ 旧模型报出来的下班时刻是 17:41。
    _, off_s = scheduler._span_of(_item(ask=PROMPT_TIME, work_hours=8.7), 0, {"td1": "09:00"})
    assert scheduler.fmt_hhmm(off_s) == "17:41"


def test_an_ask_time_item_without_work_hours_says_the_question_is_gone():
    """问了时刻却没填时长 ⇒ 答案没有下家（旧内核里那个值谁都不读）。"""
    migration = _migrate(_item(ask=PROMPT_TIME))
    assert migration.variables == () and migration.answer_moves == ()
    assert any("没有下家" in n for n in migration.notes), migration.notes


def test_work_hours_without_a_time_question_is_reported():
    """旧设置面「切问法时只灰不收」会留下这种残值；它的窗口要当日值，而它没有询问
    ⇒ 旧内核里一次都没显示过。"""
    migration = _migrate(_item(id="td9", work_hours=9, at="09:30"))
    assert migration.variables == ()
    assert any("作答后几小时" in n and "td9" in n for n in migration.notes), migration.notes
    assert "display" not in migration.tasks[0], "它一次都没显示过 ⇒ 也不该凭空长出一个窗口"


# ---- 只列不弹 / 场景门禁 ----

def test_an_item_that_never_had_a_window_becomes_list_only():
    """没有循环、没有作答时长、``at`` 也不合法 ⇒ 旧内核给不出窗口，一次都没上过浮层。
    新模型照搬成「只列不弹」：主窗体清单里还在（旧详情页那行写的是「未设时间」）。"""
    migration = _migrate(_item(at="25:99", face="clock"))
    assert migration.tasks[0]["display"] == {"show": "clock"}
    for hour in (9, 12, 18):
        assert _display_of(migration, datetime(2026, 10, 10, hour, 0)).show is False


def test_the_scene_gate_becomes_a_condition():
    task = _task(scene="办公室", at="09:30")
    assert task["effective"]["conditions"] == [
        {"var": "scene", "op": "==", "value": "办公室"}
    ]


def test_an_ask_that_matches_a_scene_carries_it_into_the_variable_trigger():
    """旧模型里 ``scene`` 同时管「问不问」与「显示不显示」（``_scene_allows`` 与
    ``COND_STATE_IS``），新模型里前者归变量、后者归任务 —— 两处都要落。"""
    migration = _migrate(_item(ask=PROMPT_TIME, work_hours=9, scene="办公室"))
    start = _by_id(migration.variables, "start_time")
    assert start["trigger"]["conditions"] == [
        {"var": "scene", "op": "==", "value": "办公室"}
    ]
    assert _by_id(migration.tasks, "td1")["effective"]["conditions"] == [
        {"var": "scene", "op": "==", "value": "办公室"}
    ]


def test_scenes_that_disagree_drop_the_condition_and_say_so():
    """共享变量只能有一个触发条件。取其中一条都会让另一条**静默地**不再问，故两条都
    不取并出声 —— 宁可多问一次，也不要静默地只在某个场景里问。"""
    migration = _migrate(
        _item(id="a", ask=PROMPT_TIME, work_hours=9, scene="办公室"),
        _item(id="b", ask=PROMPT_TIME, work_hours=9, scene="家里"),
    )
    assert "conditions" not in _by_id(migration.variables, "start_time")["trigger"]
    assert any("办公室" in n and "家里" in n for n in migration.notes), migration.notes
    # 任务自己那条门禁一字不动（它是逐条的，没有「只能有一个」这回事）
    assert _by_id(migration.tasks, "b")["effective"]["conditions"] == [
        {"var": "scene", "op": "==", "value": "家里"}
    ]


def test_a_disk_without_the_scene_variable_gets_it_back():
    """用户的 ``variables:`` 段是**整段覆盖**合并的：没把出厂那条 ``scene`` 抄过去就
    没有它，而缺一条声明的症状是「凡判场景的任务从此恒不生效」（旧模型从总线直接取，
    本来不依赖变量表）⇒ 需要时补上。"""
    migration = _migrate(_item(scene="办公室", at="09:30"))
    assert [v["id"] for v in migration.variables] == ["scene"]
    assert _by_id(migration.variables, "scene") == migrate.SCENE_DECLARATION
    # 已经在段里的那条不重复加（id 重复会让**整份配置**被拒）
    again = _migrate(_item(scene="办公室", at="09:30"), declared=[migrate.SCENE_DECLARATION])
    assert again.variables == ()
    assert any("scene" in n for n in again.notes), again.notes


def test_the_scene_declaration_matches_the_factory_one():
    """补的那一条必须与 ``config.default.yaml`` 逐字段相同：改过出厂那份（如 ``field``）
    而忘了这一份，症状是「补出来的那条变量恒读不出场景」，而两处都不报错。"""
    factory = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    rows = [v for v in factory["variables"] if v.get("id") == migrate.VAR_SCENE]
    assert rows == [dict(migrate.SCENE_DECLARATION)]


def test_an_already_declared_variable_is_not_added_again():
    """撞名时不覆盖、只出声：用户自己声明过的那一条就是他要的那个（两种来源在盘上分不开）。"""
    migration = _migrate(
        _item(ask=PROMPT_TIME, work_hours=9),
        declared=[{"id": "start_time", "name": "我自己的", "source": "ask", "type": "time",
                   "trigger": {"when": ["daily"]}}],
    )
    assert [v["id"] for v in migration.variables] == ["off_time"]
    assert any("start_time" in n for n in migration.notes), migration.notes


def test_the_button_labels_and_the_kernel_action_names_agree():
    """``button_of`` 是 ``actions_of`` 的反向，两边共用 ``BUTTON_ACTIONS`` 那一张表。"""
    for label, action in task_mod.BUTTON_ACTIONS.items():
        assert task_mod.button_of(action) == label
    assert task_mod.button_of("nope") == ""
    assert _task(actions=["snooze", "done", "nope"])["display"]["buttons"] == ["稍后", "完成"]


def test_the_ask_reader_is_one_function():
    """``_ask_of`` 只许有一份实现，三处访问口必须拿到**同一个函数对象**。

    「这一条问什么」是**条目自己的字段**读法，与 ``_face_of`` / ``_actions_of`` 同一类 ⇒
    它住在 ``scheduler``（条目字段那一层），``plugin`` 与 ``detail_view`` 只是转出去。搬回
    插件里再写一遍的后果很具体：「迁移搬到新模型的那一条问不问」与「迁移前它问不问」会给出
    两个答案，而两边都不报错。

    比 ``is`` 而不是比行为，是因为只读一个字段时两份实现**碰巧算得一样**（都返回 ``choice``），
    等字段读法分家才发作 —— 那时比行为已经看不出谁对。
    """
    assert plugin_mod._ask_of is scheduler._ask_of
    assert detail_view._ask_of is scheduler._ask_of
    assert migrate._ask_of is scheduler._ask_of, "迁移读的是同一份（它就是搬家的理由）"


def test_no_variables_are_invented_when_nothing_needs_them():
    """一份只有字面时刻条目的盘：不该凭空往 ``variables:`` 段里写任何东西。"""
    migration = _migrate(_item(at="09:30"), _item(id="b", at="10:00", end="11:00"))
    assert migration.variables == () and migration.notes == ()


# ---- 行为守恒（§5 最后那一条） ----

def test_behaviour_is_conserved_moment_by_moment():
    """老条目 A/B：同一组条目 ＋ 同一组时刻，旧内核与新模型**逐点**给出同一份显示态。

    守恒的是三样：**在不在浮层上**（含登场点 ``at_s`` 与退场点）、**哪一面钟**、
    **哪几枚按钮**。清单的顺序也一起比 —— 它由 ``at_s`` 排出来，顺序错了说明登场点错了。

    时刻表刻意压在每一处边界上（提前量之前 / 那一刻 / 之后，退场那一刻 / 之后，
    循环条的停点前后）。
    """
    items = [
        # 字面时刻那一型：09:20 登场（提前 10 分钟）、10:00 退场，上钟面
        _item(id="a", at="09:30", end="10:00", before_min=10, face="clock", actions=["done"]),
        # 作答时长那一型：08:30 + 9h = 17:30 下班，提前 30 分钟登场，倒计时
        _item(id="b", ask=PROMPT_TIME, work_hours=9, before_min=30, face="countdown"),
        # 循环那一型：每小时一次，今天循环到 20:00
        _item(id="c", interval_min=60, end="20:00", face="clock", actions=["snooze"]),
        # 一个窗口都没有的那一种：永远不上浮层
        _item(id="d"),
    ]
    migration = _migrate(*items)
    assert migration.variables, "这一组里有一条问时刻的 ⇒ 必须建出变量来"

    moments = [
        (8, 29), (8, 30), (9, 19), (9, 20), (9, 29), (9, 30), (9, 59), (10, 0), (10, 1),
        (16, 59), (17, 0), (17, 29), (17, 30), (17, 31), (19, 59), (20, 0), (23, 59),
    ]
    for hour, minute in moments:
        moment = datetime(2026, 10, 10, hour, minute)
        old = compute_display(
            # 旧模型的当日值：问 ``time`` 的那条条目今天填的 08:30
            [dict(i) for i in items],
            moment,
            day_values={"b": "08:30"},
        )
        new = _display_of(migration, moment, asked="08:30")
        where = f"{hour:02d}:{minute:02d}"
        assert new.show == old.show, where
        assert new.face == old.face, where
        assert [(r.entry_id, r.at_s, r.actions) for r in new.rows] == [
            (r.entry_id, r.at_s, r.actions) for r in old.rows
        ], where


def test_the_row_label_follows_the_exit_time_when_the_old_item_had_no_end():
    """**已知差额**（钉在这里，别让它悄悄来去）：旧条目没填 ``end`` ⇒ 一直挂到当天末，
    而浮层那一行右侧的时刻取的是**退场时刻**（``_time_label`` 先看 ``end``）⇒ 那一行
    从「09:30」变成「23:59」。

    取退场点守恒、放任这一个标签：反过来把 ``trail_min`` 留空，那一行就只剩提醒前那
    ``lead_min`` 分钟 —— 比标签错得更明显。
    """
    items = [_item(at="09:30", face="clock")]
    migration = _migrate(*items)
    moment = datetime(2026, 10, 10, 12, 0)
    assert [r.time_label for r in compute_display(items, moment).rows] == ["09:30"]
    assert [r.time_label for r in _display_of(migration, moment).rows] == ["23:59"]
    assert compute_display(items, moment).show is True
    assert _display_of(migration, moment).show is True, "「还在不在」这一项两边一致"


def test_an_empty_table_migrates_to_nothing():
    assert _migrate().tasks == () and _migrate().variables == ()
    assert _migrate("不是 mapping").tasks == (), "认不出的项跳过（与内核同一条口径）"


# ── 第二代 · 端到端：写盘 ＋ 搬今天的答案 ──────────────────────────────

def _legacy_plugin(tmp_path, items, *, pool=None, declared=None) -> Plugin:
    payload: dict = {"plugins": {"todo": {"items": list(items)}}}
    if declared is not None:
        payload["variables"] = list(declared)
    ctx = PluginContext(config=_user_config(tmp_path, payload), events=None)
    ctx.variables = pool if pool is not None else VariablePool()
    return Plugin(ctx)


def _reopen(tmp_path, *, pool=None) -> Plugin:
    """在**同一份盘**上再起一个组件 —— **不重写配置文件**。

    与 :func:`_legacy_plugin` 的区别只有这一点，但它是「重跑一次会多写什么」那类判据的
    全部前提：那个替身每调一次都把盘盖成它给的那一份，于是「第一次迁完的样子」在第二次
    调用时就没了 —— 用例看起来在测幂等，实际测的是「替身写盘两次」。
    """
    ctx = PluginContext(config=Config(user_path=tmp_path / "user.yaml"), events=None)
    ctx.variables = pool if pool is not None else VariablePool()
    return Plugin(ctx)


def test_start_migrates_the_item_table_into_variables_and_tasks(tmp_path):
    plugin = _legacy_plugin(tmp_path, [
        _item(id="a", title="喝水", at="09:30", end="10:00", before_min=5),
        _item(id="b", title="上班时间", ask=PROMPT_TIME, work_hours=9),
    ])
    plugin.start()
    try:
        section = plugin.ctx.config.user_plugin_section("todo")
        assert "items" not in section, "旧表整段删掉（留着它「哪一份在生效」就得读代码才知道）"
        assert [t["id"] for t in section["tasks"]] == ["a", "b"]
        assert [v["id"] for v in plugin.ctx.config.variables()][-2:] == [
            "start_time", "off_time",
        ]
        assert [s.id for s in plugin.task_specs()[0]] == ["a", "b"]
    finally:
        plugin.stop()


def test_migrating_twice_changes_nothing(tmp_path):
    """写两个位置（顶层变量段、本段）不是一个事务 ⇒ 中途断掉会重跑，重跑不许再多出一条。"""
    plugin = _legacy_plugin(tmp_path, [_item(id="b", ask=PROMPT_TIME, work_hours=9)])
    plugin.start()
    plugin.stop()
    after_first = plugin.ctx.config.user_path.read_text(encoding="utf-8")

    again = _reopen(tmp_path)
    again.start()
    try:
        assert again.ctx.config.user_path.read_text(encoding="utf-8") == after_first
        assert [s.id for s in again.task_specs()[0]] == ["b"]
    finally:
        again.stop()


def test_the_item_table_is_left_alone_when_tasks_already_exist(tmp_path):
    """``tasks`` 非空 ＝ 已经迁过 ⇒ 旧表原样留着也不许再动一次（否则手写的新任务会被覆盖）。"""
    payload = {
        "plugins": {"todo": {
            "items": [_item(id="old", at="09:30")],
            "tasks": [{"id": "new", "title": "新任务", "display": {"show": "row",
                                                                  "reminder": {"at": "10:00"}}}],
        }}
    }
    ctx = PluginContext(config=_user_config(tmp_path, payload), events=None)
    ctx.variables = VariablePool()
    plugin = Plugin(ctx)
    plugin.start()
    try:
        assert [s.id for s in plugin.task_specs()[0]] == ["new"]
        section = plugin.ctx.config.user_plugin_section("todo")
        assert [i["id"] for i in section["items"]] == ["old"]
    finally:
        plugin.stop()


def test_todays_answer_moves_into_the_variable_log(tmp_path):
    """搬今天的答案 ＋ 清掉旧那一笔：两件事必须同时成立（理由见 ``_move_ask_answers``）。"""
    today = date.today().isoformat()
    _write_data(day_log.AGENDA_FILENAME, {today: {"b": "08:30", "a": "09:31"}})
    pool = VariablePool()
    plugin = _legacy_plugin(
        tmp_path,
        [_item(id="a", at="09:30", end="10:00"), _item(id="b", ask=PROMPT_TIME, work_hours=9)],
        pool=pool,
    )
    plugin.start()
    try:
        assert pool.ask_raw("start_time") == "08:30"
        assert _read_data(day_log.AGENDA_FILENAME) == {today: {"a": "09:31"}}, (
            "旧那一笔答案清掉了；完成记录（a）一个字不动"
        )
    finally:
        plugin.stop()


def test_a_moved_answer_is_not_read_as_a_completion(tmp_path):
    """**这一批最该抓的那个坑**：新模型的条目账只剩「完成」一种语义，而问时刻的条目在
    旧账里存的是**答案**。不搬不清的话，迁移当天那条任务会被读成「今天已完成」——
    面板直接报「全部完成」、浮层也不再显示它，而用户什么都没做。"""
    today = date.today().isoformat()
    _write_data(day_log.AGENDA_FILENAME, {today: {"b": "08:30"}})
    plugin = _legacy_plugin(tmp_path, [_item(id="b", ask=PROMPT_TIME, work_hours=9)])
    plugin.start()
    try:
        assert plugin.done_today() == {}, "答案不是完成"
        assert plugin._snapshot(datetime.now()).summary == "未完成 1 项"
    finally:
        plugin.stop()


def test_a_completion_record_survives_the_migration(tmp_path):
    """条目 id 一个字不动 ⇒ 今天已完成的那些照旧是完成的（改名会让用户看到「记录没了」）。"""
    today = date.today().isoformat()
    _write_data(day_log.AGENDA_FILENAME, {today: {"a": "09:31"}})
    plugin = _legacy_plugin(tmp_path, [_item(id="a", at="09:30", end="17:30")])
    plugin.start()
    try:
        assert plugin.done_today() == {"a": "09:31"}
        assert plugin._snapshot(datetime.now()).summary == "全部完成"
    finally:
        plugin.stop()


def test_a_failing_variable_write_does_not_lose_the_answer(tmp_path, monkeypatch):
    """变量池写不进去时**不许清掉旧账**：清掉就等于用户填过的东西凭空没了。"""
    today = date.today().isoformat()
    _write_data(day_log.AGENDA_FILENAME, {today: {"b": "08:30"}})
    pool = VariablePool()
    monkeypatch.setattr(pool, "set_ask_value", lambda *_a, **_k: False)
    plugin = _legacy_plugin(tmp_path, [_item(id="b", ask=PROMPT_TIME, work_hours=9)], pool=pool)
    plugin.start()
    try:
        assert _read_data(day_log.AGENDA_FILENAME) == {today: {"b": "08:30"}}
    finally:
        plugin.stop()


def test_a_dirty_value_in_the_item_log_is_cleared(tmp_path):
    """旧账里那一笔不是时刻（手改过 / 旧内核写坏过）：搬不过去，但**必须清掉** ——
    留着它在新模型里就是一枚完成记录。"""
    today = date.today().isoformat()
    _write_data(day_log.AGENDA_FILENAME, {today: {"b": "完成"}})
    plugin = _legacy_plugin(tmp_path, [_item(id="b", ask=PROMPT_TIME, work_hours=9)])
    plugin.start()
    try:
        assert _read_data(day_log.AGENDA_FILENAME) == {}
        assert plugin.done_today() == {}
    finally:
        plugin.stop()


def test_the_first_variables_migration_botched_labels_are_repaired():
    """第一版迁移把旧条目的标题当询问变量的名字与问句（``label = title or …``,
    2026-10-10 用户报）：``start_time`` 该叫「上班时间」、问「今天几点上班？」。

    改映射那版只管**今后**的迁移 —— 已迁过的盘 ``tasks`` 非空、幂等门直接返回，
    错的两格永远留在盘上 ⇒ 补修是独立的一步，只认**精确的错误产物**两种形状。
    """
    decls = [
        {"id": "scene", "name": "当前场景", "source": "component",
         "component": "route_guard", "key": "profile", "field": "summary"},
        {"id": "start_time", "source": "ask", "name": "下班时间", "type": "time",
         "prompt": "下班时间？", "trigger": {"when": ["startup"]}},
        {"id": "off_time", "name": "下班时间", "source": "derived", "type": "time",
         "expr": "start_time + 600m"},
    ]
    out, notes = migrate.repair_ask_labels(decls)
    start = [d for d in out if d["id"] == "start_time"][0]
    assert start["name"] == migrate.ASK_NAME
    assert start["prompt"] == migrate.ASK_PROMPT
    assert len(notes) == 2
    assert out[2]["name"] == "下班时间", "派生那条同名纯属合法，不归补修管"
    assert decls[1]["prompt"] == "下班时间？", "纯函数：传入的列表一个字不动"


def test_the_label_repair_touches_only_the_exact_botched_shapes():
    """问句／名字是用户手写的（不等于错写产物）⇒ 一个字不动、零 note、不落盘。"""
    canonical = {"id": "start_time", "source": "ask", "name": "上班时间", "type": "time",
                 "prompt": "今天几点上班？", "trigger": {"when": ["startup"]}}
    hand_written = {"id": "ask_break", "source": "ask", "name": "下班时间", "type": "time",
                    "prompt": "下班时间？", "trigger": {"when": ["daily"]}}
    out, notes = migrate.repair_ask_labels([canonical, hand_written])
    assert notes == []
    assert out == [canonical, hand_written]


def test_start_repairs_the_botched_ask_labels_on_an_already_migrated_disk(tmp_path):
    """**已迁过的盘**也要被补修扫到（端到端）：``tasks`` 非空 ⇒ 第二代迁移的幂等门
    直接返回，可错写的那两格是它写下去的 —— 不补修，弹窗永远问「下班时间」。"""
    payload = {
        "variables": [
            {"id": "start_time", "source": "ask", "name": "上班时间", "type": "time",
             "prompt": "下班时间？", "trigger": {"when": ["startup"]}},
        ],
        "plugins": {"todo": {"tasks": [{
            "id": "report", "title": "提交日报", "text": "{off_time} 前提交",
            "display": {"show": "row", "reminder": {"at": "{off_time}"},
                        "trail_min": 30, "buttons": ["完成", "稍后"]},
        }]}},
    }
    ctx = PluginContext(config=_user_config(tmp_path, payload), events=None)
    ctx.variables = VariablePool()
    plugin = Plugin(ctx)
    plugin.start()
    try:
        saved = [v for v in ctx.config.variables() if v["id"] == "start_time"][0]
        assert saved["prompt"] == migrate.ASK_PROMPT
        assert saved["name"] == "上班时间", "用户已改好的名字不归补修管"
    finally:
        plugin.stop()
