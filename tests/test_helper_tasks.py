"""批次 4 · S3：task 调度层（tasks / primitives / core 薄入口）的行为与门禁。

2026-09-29 起这一层的**输入换成了会话**（`intent.set("tasks", …)`），不再是用户域
``helper_tasks.json``。本文覆盖：

  - 默认表等价迁移前的硬编码调度（route 30s / tunnel 5s / hosts 30s），且自身过五条红线
  - 表的覆盖 / 关停 / 追加；坏表整份忽略（不失去保活能力）
  - **有意图才跑** ＋ 撤回跑一轮收尾即休眠
  - **空闲零唤醒**（无活跃意图时 ``seconds_until_due()`` 返回 ``None``）
  - 门禁：core.py 是薄入口（≤700 行且不含业务机制函数）
  - 门禁：同步原语 ⊆ 能力封闭集（ACTION_SCHEMAS）
  - 门禁：core 侧的表是 helper 内置表的**名级副本**（分层禁令 ⇒ 只能靠它兜漂移）
  - 门禁：app 汇总出的表能过 helper 的五条红线（跨进程契约）
"""

from __future__ import annotations

import pathlib

import pytest

from barkit.capabilities.helper import actions, primitives, tasks

HELPER_PKG = pathlib.Path(__file__).resolve().parents[1] / "src/barkit/capabilities/helper"


# ── 默认表 ──────────────────────────────────────────────────────

def test_builtin_table_equivalent_to_legacy_schedule():
    """内置默认表 = 迁移前的硬编码三套调度（route 30s / tunnel 5s / hosts 30s）。"""
    table = {t["id"]: t for t in tasks.BUILTIN_TASKS}
    assert set(table) == {"route_guard.sync", "ipad_tunnel.sync", "hosts_guard.sync"}
    assert table["route_guard.sync"]["action"] == "route.sync"
    # 2026-09-29：`params_from`（文件名）换成了 `subject`（领域名）。
    # 期望态搬进会话之后 root 侧不再读任何文件 ⇒ 连文件名都不接受。
    assert table["route_guard.sync"]["subject"] == "route"
    assert table["route_guard.sync"]["interval"] == 30
    assert table["ipad_tunnel.sync"]["action"] == "tunnel.sync"
    assert table["ipad_tunnel.sync"]["subject"] == "tunnel"
    assert table["ipad_tunnel.sync"]["interval"] == 5
    assert table["hosts_guard.sync"]["action"] == "hosts.sync"
    assert table["hosts_guard.sync"]["subject"] == "hosts"
    # 三条 subject 都在闭集里（与 actions.TASK_SUBJECTS / state.SUBJECT_* 同一份口径）
    assert {t["subject"] for t in tasks.BUILTIN_TASKS} == set(actions.TASK_SUBJECTS)
    assert {t["subject"] for t in tasks.BUILTIN_TASKS} <= set(tasks.SUBJECTS)


def test_builtin_table_passes_redlines():
    ok, reason = actions.validate_task_table(
        list(tasks.BUILTIN_TASKS), registered_plugin_ids=tasks.KNOWN_PLUGIN_IDS
    )
    assert ok, reason


def test_no_table_uses_defaults():
    """没有表（app 还没声明 / 会话未连上）⇒ 内置默认表，不需要「读一次盘去确认」。"""
    assert {t.id for t in tasks.build_tasks(None)} == {
        "route_guard.sync", "ipad_tunnel.sync", "hosts_guard.sync"
    }


# ── app 发来的表 ────────────────────────────────────────────────

def test_table_overrides_and_appends():
    got = {t.id: t for t in tasks.build_tasks({
        "plugins": ["route_guard", "ipad_tunnel"],
        "tasks": [
            {"id": "route_guard.sync", "action": "route.sync", "subject": "route",
             "interval": 60, "enabled": True},
            {"id": "ipad_tunnel.keepalive", "action": "tunnel.sync", "subject": "tunnel",
             "interval": 9, "enabled": True},
        ],
    })}
    assert got["route_guard.sync"].interval == 60        # 覆盖默认
    assert got["ipad_tunnel.keepalive"].interval == 9    # 追加
    assert got["hosts_guard.sync"].interval == 30        # 未声明者保留默认


def test_table_can_disable_a_task():
    got = {t.id: t for t in tasks.build_tasks({
        "plugins": ["hosts_guard"],
        "tasks": [{"id": "hosts_guard.sync", "action": "hosts.sync", "subject": "hosts",
                   "interval": 30, "enabled": False}],
    })}
    assert got["hosts_guard.sync"].enabled is False


def test_not_a_dict_falls_back_to_defaults():
    """表不是对象（None / 列表 / 字符串）⇒ 沿用内置默认表，不猜。"""
    for bad in (None, [], "tasks", 7):
        assert {t.id for t in tasks.build_tasks(bad)} == {
            "route_guard.sync", "ipad_tunnel.sync", "hosts_guard.sync"
        }


@pytest.mark.parametrize("bad", [
    # 红线 1：subject 必须在闭集（旧形态这里放的是 params_from 的文件名）
    {"tasks": [{"id": "route_guard.sync", "action": "route.sync",
                "subject": "../../etc/sudoers", "interval": 30}]},
    {"tasks": [{"id": "route_guard.sync", "action": "route.sync",
                "subject": "tasks", "interval": 30}]},
    # 红线 2：id 前缀必须是已注册插件
    {"tasks": [{"id": "evil.sync", "action": "route.sync",
                "subject": "route", "interval": 30}]},
    # 红线 4：interval 下限
    {"tasks": [{"id": "route_guard.sync", "action": "route.sync",
                "subject": "route", "interval": 1}]},
    # 红线 5：不得内联 params
    {"tasks": [{"id": "route_guard.sync", "action": "route.sync", "subject": "route",
                "interval": 30, "params": {"dest": "0.0.0.0/0"}}]},
    # 未知 action
    {"tasks": [{"id": "route_guard.sync", "action": "route.dance",
                "subject": "route", "interval": 30}]},
    # 旧形态的字段：已不在白名单里 ⇒ 整份拒（换位要换干净）
    {"tasks": [{"id": "route_guard.sync", "action": "route.sync",
                "params_from": "routes.json", "interval": 30}]},
])
def test_bad_table_is_ignored_wholesale(bad):
    """任何一条红线不过 → 整份忽略，退回内置默认表（绝不因坏表失去保活能力）。"""
    got = tasks.build_tasks(bad)
    assert {t.id for t in got} == {
        "route_guard.sync", "ipad_tunnel.sync", "hosts_guard.sync"
    }
    assert {t.interval for t in got} == {5.0, 30.0}


# ── Runner：有意图才跑 ──────────────────────────────────────────

def _collect():
    calls: list = []

    def dispatch(action, raw):
        calls.append((action, raw))
        return {}, None

    return calls, dispatch


def test_runner_runs_nothing_without_intent():
    """**没有意图的 subject 一次都不跑**（旧形态下 route/hosts 会一直按 30s 空转）。

    这是「空闲零唤醒」的前半：没有活跃意图时 ``seconds_until_due()`` 返回 ``None``，
    调用方把它当 ``selectors`` 的超时 ⇒ 线程纯阻塞，一次都不醒（旧循环一天醒 86400 次）。
    """
    calls, dispatch = _collect()
    runner = tasks.Runner(dispatch)
    assert runner.seconds_until_due() is None
    runner.run_due()
    assert calls == []
    assert runner.subjects_with_intent() == []


def test_runner_runs_due_task_with_its_intent_as_payload():
    calls, dispatch = _collect()
    runner = tasks.Runner(dispatch)
    assert runner.set_intent("route", {"poll": 30}) is True

    assert runner.seconds_until_due() == 0.0           # 刚声明 ⇒ 立刻到期
    runner.run_due()
    assert [a for a, _ in calls] == ["route.sync"]
    assert calls[0][1] == '{"poll": 30}', "原语收到的就是这份意图的 JSON 文本"

    runner.run_due()
    assert len(calls) == 1, "未到 interval，不再跑"


def test_runner_declare_is_idempotent_by_content():
    """同内容重复声明 ⇒ 不算变化（不重置 last_run、不重跑）。"""
    calls, dispatch = _collect()
    runner = tasks.Runner(dispatch)
    assert runner.set_intent("route", {"a": 1}) is True
    runner.run_due()
    assert len(calls) == 1
    assert runner.set_intent("route", {"a": 1}) is False
    assert runner.seconds_until_due() > 0              # last_run 没被清零
    runner.run_due()
    assert len(calls) == 1


def test_runner_updates_interval_from_result():
    runner = tasks.Runner(lambda action, raw: ({"poll": 45}, 45.0))
    runner.set_intent("route", {})
    runner.run_due()
    assert runner.tasks[0].interval == 45.0


def test_runner_skips_disabled():
    calls, dispatch = _collect()
    runner = tasks.Runner(dispatch, {
        "plugins": ["route_guard"],
        "tasks": [{"id": "route_guard.sync", "action": "route.sync", "subject": "route",
                   "interval": 30, "enabled": False}],
    })
    runner.set_intent("route", {})
    runner.run_due()
    assert calls == []
    assert runner.seconds_until_due() is None, "关掉的 task 不该撑着一条节拍"


def test_apply_table_keeps_last_run():
    calls, dispatch = _collect()
    runner = tasks.Runner(dispatch)
    runner.set_intent("route", {})
    runner.run_due()
    before = {t.id: t.last_run for t in runner.tasks}
    assert any(v > 0 for v in before.values())
    runner.apply_table(None)                           # 换回默认表
    assert {t.id: t.last_run for t in runner.tasks} == before
    assert len(calls) == 1, "换个表不该把刚跑过的 task 当成又到期"


def test_retract_runs_one_final_round_then_goes_idle():
    """撤回**不是**「把 task 从表里删掉」——那样收尾那一轮永远不跑（隧道会一直挂着）。

    撤回被记成「一次待跑的收尾」：相关 task 立刻到期跑一轮，跑完即休眠。
    """
    calls, dispatch = _collect()
    runner = tasks.Runner(dispatch)
    runner.set_intent("tunnel", {"port": 1080})
    runner.run_due()
    assert [r for _, r in calls] == ['{"port": 1080}']

    assert runner.set_intent("tunnel", None) is True   # 撤回
    runner.run_due()
    assert [r for _, r in calls] == ['{"port": 1080}', None], "收尾轮收到的意图是 None（＝拆）"
    assert runner.subjects_with_intent() == [], "收尾跑完即休眠"
    assert runner.seconds_until_due() is None
    runner.run_due()
    assert len(calls) == 2, "不再有第三轮"


def test_retract_without_prior_intent_is_a_noop():
    calls, dispatch = _collect()
    runner = tasks.Runner(dispatch)
    assert runner.set_intent("route", None) is False
    runner.run_due()
    assert calls == []


# ── 门禁：core 是薄入口 ──────────────────────────────────────────

def test_core_is_thin_entry():
    """core.py 只做入口与装配：≤700 行，且不含任何业务机制函数。"""
    src = (HELPER_PKG / "core.py").read_text(encoding="utf-8")
    assert len(src.splitlines()) <= 700
    for banned in ("def run_cycle", "def run_tunnel_cycle", "def run_hosts_cycle",
                   "def _activate_tunnel", "def _apply_hosts", "def load_routes"):
        assert banned not in src, f"业务机制不得留在 core：{banned}"


def test_sync_actions_are_subset_of_closed_action_set():
    """task 表可驱动的原语必须在能力封闭集内（防「未登记能力」被调度）。"""
    assert set(primitives.SYNC_ACTIONS) <= set(actions.ACTION_SCHEMAS)


def test_sync_action_names_have_no_forbidden_words():
    for name in primitives.SYNC_ACTIONS:
        assert not any(w in name for w in actions.FORBIDDEN_ACTION_WORDS), name


def test_module_layout_split():
    """业务机制必须各自成模块（不再挤在 core）；会话传输层也要各自成模块。"""
    for mod in ("hostsops.py", "tasks.py",
                "primitives.py", "lifecycle.py", "consts.py",
                "protocol.py", "session.py", "state.py", "safeio.py"):
        assert (HELPER_PKG / mod).is_file(), f"缺少模块：{mod}"
    # ``routeops`` 2026-10-02 拆成**包**（同族里只有它也成过包）：包根不再有 routeops.py，
    # 真身是 ``routeops/`` 里的十个子模块。这里按**这一族**点名 —— 只断言「包存在」会把
    # 某个子模块被删掉这件事放过去，而断言回 ``routeops.py`` 的文件名则整个面出圈。
    assert (HELPER_PKG / "routeops" / "__init__.py").is_file()
    subs = {"ipvalid", "netmode", "validate", "routes", "lease",
            "netsvc", "baseline", "converge", "ops", "routeops"}
    have = {p.stem for p in (HELPER_PKG / "routeops").glob("*.py")}
    assert subs <= have, f"routeops 子模块缺了：{sorted(subs - have)}"
    # 同一天 ``tunnelops`` 也拆成**包**：判据与 routeops 完全同形（按「这一族」点名，
    # 不按文件名），否则删掉某个子模块这条门禁一声不响就放过去了。
    assert (HELPER_PKG / "tunnelops" / "__init__.py").is_file()
    subs = {"tunconsts", "tunshell", "tunvalidate", "tunwifi", "tunroute",
            "tundns", "tunhev", "tunlifecycle", "tunnelops"}
    have = {p.stem for p in (HELPER_PKG / "tunnelops").glob("*.py")}
    assert subs <= have, f"tunnelops 子模块缺了：{sorted(subs - have)}"


def _code_strings(path) -> list:
    """模块里**代码中的**字符串常量：剔除文档字符串（注释本来就不在 AST 里）。

    门禁得能区分「代码还在用这个文件名」与「文档在讲述它已经删了」—— 否则删掉一个
    通道之后，任何一处「某某文件已消失」的说明都会把门禁弄红，而修法只剩「把说明
    删掉」，那正好把我们要留下的知识赶走。
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docstrings]


#: 已消失的「通道类文件」名（四个期望态 + 三个状态）。
_BANNED_FILENAMES = ("routes.json", "tunnel.json", "hosts_rules.json",
                     "helper_tasks.json", "state.json", "tunnel_state.json",
                     "hosts_state.json")
#: 已消失的路径函数 / 常量名（含敲门那一个 —— 它没有 ``.json``，只有这个名字）。
_BANNED_PATH_IDENTS = frozenset({
    "routes_file", "state_file", "trigger_file", "tunnel_file",
    "tunnel_state_file", "hosts_rules_file", "hosts_state_file",
    "TRIGGER_FILE", "TRIGGER_PATH",
})


def _scan_file_channel(paths) -> list:
    """扫「还在指名通道类文件」的地方，返回违规描述列表（空 = 合规）。

    两条判据分开，因为两类名字长得不一样：

    - **路径函数名**按**标识符**判（含 ``trigger_file`` —— 敲门那个没有 ``.json``，
      只有一个名字）。三档都要覆盖：``Name``（``routes_file()``）、``Attribute``
      （``safeio.routes_file()``）、``alias``（``from .safeio import routes_file``）
      —— 只判前两档会漏掉「导进来但还没调用」这一档，而那正是迁移途中最常见的形态。
    - **文件名**按代码里的**字符串常量**判。剔除文档字符串是必需的：写「某某文件
      已消失」是**要留下的知识**，若连文档一起扫，门禁就会被自己的说明弄红，
      而唯一的修法是把说明删掉 —— 那正好把我们要留下的知识赶走。
    """
    import ast

    hits: list = []
    for path in paths:
        tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                ident = node.id
            elif isinstance(node, ast.Attribute):
                ident = node.attr
            elif isinstance(node, ast.alias):        # ``from .x import <ident>``
                ident = node.name
            else:
                continue
            if ident in _BANNED_PATH_IDENTS:
                hits.append(f"{path}: 仍引用通道路径名 {ident!r}")
        for text in _code_strings(pathlib.Path(path)):
            for name in _BANNED_FILENAMES:
                if name in text:
                    hits.append(f"{path}: 代码里仍指名通道文件 {name}：{text!r}")
    return hits


def test_helper_package_has_no_file_channel_left():
    """helper 包内**代码**不得再指名「通道类文件」（2026-09-29 通道换成会话）。

    七个路径函数（routes_file / state_file / trigger_file / tunnel_file /
    tunnel_state_file / hosts_rules_file / hosts_state_file）连同它们的文件一起删掉。
    留一个没人调用的路径函数，就是留一条「以后有人接着用它读盘」的路。
    """
    hits = _scan_file_channel(sorted(
        p for p in HELPER_PKG.rglob("*.py") if "__pycache__" not in p.parts
    ))
    assert not hits, "仍存在通道类文件引用：\n" + "\n".join(hits)


def test_file_channel_gate_selfcheck(tmp_path):
    """门禁自证：违规必抓、合规不误报。

    合规那份**故意在文档字符串与注释里**写下 ``routes.json`` / ``state.json`` ——
    这正是真源码里的情形（``helper/__init__`` 与 ``version`` 都列了「已消失的文件」），
    门禁必须放过它。
    """
    # 三档标识符 + 一档字符串，各造一份
    cases = [
        ("alias.py", "from .safeio import routes_file\n", "routes_file"),
        ("name.py", "from .safeio import routes_file\nP = routes_file()\n", "routes_file"),
        ("attr.py", "from . import safeio\nP = safeio.trigger_file()\n", "trigger_file"),
        ("str.py", "Q = 'state.json'\n", "state.json"),
    ]
    for filename, src, expect in cases:
        target = tmp_path / filename
        target.write_text(src, encoding="utf-8")
        hits = _scan_file_channel([target])
        assert hits, f"{filename} 的违规没被抓到"
        assert any(expect in h for h in hits), (filename, hits)

    good = tmp_path / "good.py"
    good.write_text(
        '"""模块说明：旧的 routes.json / .trigger 已随通道一起删除。"""\n'
        "# 注释里提 state.json 也不算（注释不在 AST 里）\n"
        "def f():\n"
        '    """函数说明里写 tunnel.json 同理。"""\n'
        "    return 'subject'\n",
        encoding="utf-8",
    )
    assert _scan_file_channel([good]) == []


# ──────────────────────────────────────────────
# S3b-2c：app 侧汇总任务表（插件声明 → 会话一帧）
# ──────────────────────────────────────────────

class _Stub:
    """最小插件桩：只需 id + resources()。"""

    def __init__(self, pid, task_ids=(), boom=False):
        self.id = pid
        self._task_ids = list(task_ids)
        self._boom = boom

    def resources(self):
        if self._boom:
            raise RuntimeError("boom")
        from barkit.core.resources import PluginResources
        return PluginResources(helper_tasks=list(self._task_ids))


class _Link:
    """最小会话桩：只记下发出了什么。"""

    def __init__(self, ok=True):
        self.sent: list = []
        self._ok = ok

    def declare(self, subject, payload):
        self.sent.append((subject, payload))
        return self._ok


def test_helper_task_table_single_source():
    """``core.helper_tasks.CANONICAL_TASKS`` 是 helper 内置表的**名级副本**，必须逐条相等。

    ``core/`` 不得 import ``capabilities/*``（分层门禁），故只能靠本门禁兜住漂移——
    与 ``installer.KNOWN_ROOT_BINARIES`` 对 ``rootbin.ROOT_BINARIES`` 同一手法。
    """
    from barkit.core import helper_tasks

    assert ([dict(t) for t in helper_tasks.CANONICAL_TASKS]
            == [dict(t) for t in tasks.BUILTIN_TASKS])


def test_builtin_plugins_declare_their_helper_tasks(tmp_path):
    """三插件各自声明自己拥有的 task id（route_guard.sync / ipad_tunnel.sync
    / hosts_guard.sync）——声明缺失等于「卸载后 task 仍在内置表里跑」。"""
    from barkit.core import helper_tasks
    from barkit.core.config import Config
    from barkit.core.plugin import PluginContext
    from barkit.plugins.hosts_guard.plugin import Plugin as HG
    from barkit.plugins.ipad_tunnel.plugin import Plugin as IT
    from barkit.plugins.route_guard.plugin import Plugin as RG

    ctx = PluginContext(config=Config(user_path=tmp_path / "config.yaml"))
    got = helper_tasks.declared_task_ids([RG(ctx), IT(ctx), HG(ctx)])
    assert got == ["route_guard.sync", "ipad_tunnel.sync", "hosts_guard.sync"]
    assert set(got) <= set(helper_tasks.KNOWN_TASK_IDS)   # 打错字即在此失败


def test_build_table_disables_tasks_nobody_declares():
    """未声明的 task 必须**显式** ``enabled=false``。

    helper 的合并口径是「内置表 ← 表里按 id 覆盖」：不发它 = 内置那条照样跑。
    与 route_guard ``on_disabled`` 必须逐条 ``apply=False``（不能写空 rules）是同一个坑。
    """
    from barkit.core import helper_tasks

    table = helper_tasks.build_table([_Stub("route_guard", ["route_guard.sync"])])
    by_id = {t["id"]: t for t in table["tasks"]}
    assert by_id["route_guard.sync"]["enabled"] is True
    assert by_id["ipad_tunnel.sync"]["enabled"] is False
    assert by_id["hosts_guard.sync"]["enabled"] is False
    assert table["plugins"] == ["route_guard"]
    assert by_id["route_guard.sync"]["action"] == "route.sync"   # 完整声明，非仅 id
    assert by_id["route_guard.sync"]["subject"] == "route"


def test_build_table_empty_plugins_disables_everything():
    from barkit.core import helper_tasks

    table = helper_tasks.build_table([])
    assert [t["id"] for t in table["tasks"]] == list(helper_tasks.KNOWN_TASK_IDS)
    assert all(t["enabled"] is False for t in table["tasks"])
    assert table["plugins"] == []


def test_declared_ids_reject_unknown_and_dedup():
    from barkit.core import helper_tasks

    ids = helper_tasks.declared_task_ids(
        [_Stub("p", ["a.b", "a.b", 123, "", "hosts_guard.sync"])])
    assert ids == ["a.b", "hosts_guard.sync"]

    table = helper_tasks.build_table([_Stub("p", ["a.b", "hosts_guard.sync"])])
    assert {t["id"] for t in table["tasks"]} == set(helper_tasks.KNOWN_TASK_IDS)
    # 未知 id 不进表：新增 task 的唯一路径是改 helper 源码 + bump HELPER_VERSION
    assert {t["id"] for t in table["tasks"] if t["enabled"]} == {"hosts_guard.sync"}


def test_declared_ids_survive_broken_plugin():
    """单个插件 resources() 抛异常不得让整张表失去声明（isolate 口径）。"""
    from barkit.core import helper_tasks

    assert helper_tasks.declared_task_ids(
        [_Stub("bad", boom=True), _Stub("ok", ["hosts_guard.sync"])]
    ) == ["hosts_guard.sync"]


def test_sync_task_table_hands_the_table_to_the_session():
    """表不再落盘：它是一条 ``intent.set("tasks", …)`` 消息。

    2026-09-29 之前这里断言的是「用户域 helper_tasks.json 写成什么样、权限 0644、
    落在隔离目录内」。那份文件连同它的读法一起没了 —— 判据随之换成「发出去的帧里
    是什么」。
    """
    from barkit.core import helper_tasks

    link = _Link()
    ok, err = helper_tasks.sync_task_table([_Stub("hosts_guard", ["hosts_guard.sync"])], link)
    assert ok and err is None
    assert [s for s, _ in link.sent] == ["tasks"]
    table = link.sent[-1][1]
    assert table["version"] == helper_tasks.TABLE_VERSION
    assert table["plugins"] == ["hosts_guard"]
    assert {t["id"] for t in table["tasks"] if t["enabled"]} == {"hosts_guard.sync"}
    assert {t["subject"] for t in table["tasks"]} <= set(actions.TASK_SUBJECTS)


def test_sync_task_table_reports_missing_session():
    """会话没接线时**如实报**，不静默当成功（悄悄不发会让「声明了却没生效」查不出来）。"""
    from barkit.core import helper_tasks

    ok, err = helper_tasks.sync_task_table([], None)
    assert not ok and "会话未接线" in err

    ok, err = helper_tasks.sync_task_table([], _Link(ok=False))
    assert not ok and "守护未连接" in err


def test_app_written_table_passes_helper_red_lines():
    """app 汇总出的表必须能过 helper 的**五条红线**，否则 helper 整份忽略、退回内置表
    （安全但静默失效）。跨进程契约由本测试钉住。"""
    from barkit.core import helper_tasks

    link = _Link()
    ok, err = helper_tasks.sync_task_table([
        _Stub("route_guard", ["route_guard.sync"]),
        _Stub("ipad_tunnel", ["ipad_tunnel.sync"]),
        _Stub("hosts_guard", ["hosts_guard.sync"]),
    ], link)
    assert ok and err is None
    table = link.sent[-1][1]
    good, reason = actions.validate_task_table(table["tasks"],
                                              registered_plugin_ids=table["plugins"])
    assert good, reason
    assert len(table["tasks"]) == len(helper_tasks.KNOWN_TASK_IDS)


def test_load_all_syncs_task_table(tmp_path, monkeypatch):
    """load_all 收尾必须送一次表（否则插件声明的 task 永远不生效）。"""
    from barkit.core import registry as reg_mod
    from barkit.core.config import Config

    calls: list = []
    monkeypatch.setattr(reg_mod.Registry, "sync_helper_tasks",
                        lambda self: calls.append(1))
    reg = reg_mod.Registry(Config(user_path=tmp_path / "config.yaml"))
    monkeypatch.setattr(reg, "discover", lambda: {})
    reg.load_all()
    assert calls == [1]


def test_forget_drops_instance_and_resyncs(tmp_path, monkeypatch):
    """§9.1 卸载第 3 步：插件卸载 → 它的 task 从表里摘掉（置 enabled=false）。"""
    from barkit.core import registry as reg_mod
    from barkit.core.config import Config

    reg = reg_mod.Registry(Config(user_path=tmp_path / "config.yaml"))
    reg.register_instance(_Stub("gone", ["hosts_guard.sync"]))
    seen: list = []
    monkeypatch.setattr(reg_mod, "sync_task_table",
                        lambda ps, link: (seen.append([p.id for p in ps]), (True, None))[1])
    reg.forget("gone")
    assert seen[-1] == []


# ── 时钟：调度只认单调钟 ────────────────────────────────────────

class _Clock:
    """可手动推进的时钟替身（只顶替 ``tasks.time``，不动 pytest 自己那份）。

    ``mono`` = 进程内单调钟（开机以来的秒），``wall`` = 墙上时钟。两个分开推进，
    才能造出「系统时钟被校回、而真实时间照常流逝」这种现场。
    """

    def __init__(self, mono: float = 10_000.0, wall: float = 1_700_000_000.0):
        self.mono = mono
        self.wall = wall

    def monotonic(self) -> float:
        return self.mono

    def time(self) -> float:
        return self.wall


def test_schedule_ignores_a_wall_clock_step_back(monkeypatch):
    """系统时钟被校回 ⇒ 调度照旧按 interval 走（墙上时钟只回答「这是何时的事实」）。

    helper 是常驻守护（LaunchDaemon），一跑就是几天：NTP 步进校正、休眠唤醒后的
    校时、RTC 掉电复位都可能让 ``time.time()`` 往回跳。用墙上时钟记 ``last_run``
    时，``now - last_run`` 会变成负数 ⇒ 所有 task 判定「还没到点」⇒ **整条保活
    调度静默停摆**，直到墙上时钟追回那段差值。故调度一律走 ``time.monotonic()``。
    """
    clock = _Clock()
    monkeypatch.setattr(tasks, "time", clock)

    calls, dispatch = _collect()
    runner = tasks.Runner(dispatch)
    runner.set_intent("route", {})
    runner.run_due()
    assert len(calls) == 1

    clock.mono += 31                     # 真实时间过了 31s（> interval 30）
    clock.wall -= 3600                   # 同时墙上时钟被往回校了一小时
    runner.run_due()
    assert len(calls) == 2, "单调钟已过 interval ⇒ 该跑就跑，与墙上时钟无关"


def test_seconds_until_due_ignores_a_wall_clock_step_back(monkeypatch):
    """同一条纪律的另一半：``seconds_until_due()`` 也不受校时影响。

    它喂给 ``selectors`` 当超时 —— 墙上时钟回拨时若它变大，守护线程会真的睡过去
    那么久，空转与停摆是同一个后果的两种说法。
    """
    clock = _Clock()
    monkeypatch.setattr(tasks, "time", clock)

    runner = tasks.Runner(lambda action, raw: ({}, None))
    runner.set_intent("route", {})
    runner.run_due()
    assert runner.seconds_until_due() == pytest.approx(30.0, abs=0.5)

    clock.wall -= 3600
    assert runner.seconds_until_due() == pytest.approx(30.0, abs=0.5), \
        "墙上时钟回拨不改变「还要等多久」"

    clock.mono += 10
    assert runner.seconds_until_due() == pytest.approx(20.0, abs=0.5)
