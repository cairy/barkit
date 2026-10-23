"""守护侧的**日志节流**与**观测落盘**（2026-09-28 起；2026-09-29 随通道换掉重写）。

旧版盯的是两件事：
- 日志：同一 key 在窗口内只留首条，窗口过后补一句「已合并 N 次」；
- 状态：**三份**状态文件（``state.json`` / ``tunnel_state.json`` / ``hosts_state.json``），
  内容（剔除 ``checked_at``）没变就不重写，但**最多静默 REFRESH_AFTER_S**。

2026-09-29 换了通道，落盘这一半的形态跟着换了：

- **三份合并成一份** ``helper_snapshot.json``（每 subject 一份 ⇒ N 次 ``tmp + rename``，
  每次还让目录变两次）；
- **「最长静默」这条规则整条取消** —— 它存在的唯一理由是喂「mtime / 时刻戳」这个新鲜度
  判据，而新鲜度现在由**连接本身**回答（连接在 = 事实是推过来的、随变的）。于是稳态下
  这个文件可以整天一次都不写；
- **它不再是通道** ⇒ 「文件还在不在」不再构成「该不该写」的理由。

日志那一半一个字没变（节流机制本就不依赖通道）。
"""

from __future__ import annotations

import json

import pytest

from barkit.capabilities.helper import safeio, state

SNAP = state.SNAPSHOT_NAME


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BARKIT_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_module_state():
    """节流表与观测都是**模块级**：用例之间必须归零，否则互相串。

    ``_LAST_SIGNATURE`` 也要清 —— 它是「上一条用例写了什么」的签名，不清的话新用例
    的第一笔会被判成「内容没变」而跳过，``tmp_path`` 里连文件都不出现。
    """
    def _reset():
        safeio._LOG_THROTTLE.clear()
        state._OBSERVED.clear()
        state._INTENTS.clear()
        state._LAST_SIGNATURE = None

    _reset()
    yield
    _reset()


def _log_lines(data_dir) -> list[str]:
    path = data_dir / "helper.log"
    if not path.exists():
        return []
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ── 日志节流 ──────────────────────────────────────────────

def test_log_without_key_writes_every_time(data_dir):
    """不带 key 的调用照旧每条都写——启停、安装这类一次性的事不该被合并。"""
    for i in range(3):
        safeio.log(f"一次性事件 {i}")
    assert len(_log_lines(data_dir)) == 3


def test_same_key_collapses_within_the_window(data_dir):
    """轮询任务每轮一句「还是那样」⇒ 窗口内只留首条。"""
    for _ in range(10):
        safeio.log("上游 SOCKS 代理未就绪", key="tunnel.upstream_not_ready")
    assert len(_log_lines(data_dir)) == 1


class _FakeClock:
    """可手动推进的 ``time.monotonic``（只顶替 ``safeio.time``，不影响 pytest 自己）。

    用来把「等窗口过去」变成一次加法 —— 真睡 300 秒的用例没人跑得起。
    """

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


def test_collapsed_count_surfaces_after_the_window(data_dir, monkeypatch):
    """窗口过后补一句计数：去重是挡刷屏，不是把「重复了多少次」也吞掉。

    注意 ``every_s<=0`` 是「不节流」的意思，与「窗口已过期」是两回事，
    故这里不靠改窗口长度，而是把时钟往前拨。
    """
    clock = _FakeClock()
    monkeypatch.setattr(safeio, "time", clock)
    monkeypatch.setattr(safeio, "THROTTLE_DEFAULT_S", 60.0)
    safeio.log("上游未就绪", key="k")          # 首条
    safeio.log("上游未就绪", key="k")          # 窗口内：静默
    safeio.log("上游未就绪", key="k")          # 窗口内：静默
    clock.now += 120                           # 窗口过去（不真的等）
    safeio.log("上游未就绪", key="k")          # 补一条带计数的
    lines = _log_lines(data_dir)
    assert len(lines) == 2
    assert "已合并 2 次" in lines[1]


def test_different_keys_do_not_shadow_each_other(data_dir):
    safeio.log("A", key="a")
    safeio.log("B", key="b")
    safeio.log("A", key="a")                   # 同 key 第二次：静默
    lines = _log_lines(data_dir)
    assert len(lines) == 2
    assert any("A" in ln for ln in lines) and any("B" in ln for ln in lines)


def test_zero_window_means_no_throttling(data_dir):
    """``every_s<=0`` 视为不节流（调用方显式要求每条都写的退路）。"""
    for _ in range(3):
        safeio.log("照旧", key="k", every_s=0)
    assert len(_log_lines(data_dir)) == 3


# ── 观测落盘：一份快照，内容真变才写 ──────────────────────

@pytest.fixture
def writes(monkeypatch):
    """记录「真落盘了几次」——比看 mtime 可靠。

    Windows 的 ``st_mtime`` 量化到约 15.6ms，同一刻度内「写了」与「没写」读出来
    一样，按 mtime 判会假绿。直接拦 ``_atomic_write_json`` 数调用次数：那才是
    「有没有真的往下写」这件事本身（``state`` 是 ``from .safeio import`` 进来的，
    故拦本模块里的这个全局名即可）。
    """
    calls: list = []

    def _fake(path, payload):
        calls.append(path.name)
        safeio._atomic_write_json(path, payload)      # 真写，保持文件语义不变

    monkeypatch.setattr(state, "_atomic_write_json", _fake)
    return calls


def _snapshot(data_dir) -> dict:
    return json.loads((data_dir / SNAP).read_text(encoding="utf-8"))


def test_identical_observation_is_not_rewritten(data_dir, writes):
    """内容没变（只有 ``checked_at`` 在动）⇒ 不重写。"""
    state.write_tunnel_state({"ok": True, "checked_at": 1})
    assert writes == [SNAP]
    state.write_tunnel_state({"ok": True, "checked_at": 2})    # 只有时间戳变
    assert writes == [SNAP], "时间戳不算内容"


def test_changed_observation_writes_immediately(data_dir, writes):
    """真变了立刻落盘：及时性不能让给省写盘。"""
    state.write_tunnel_state({"ok": True, "checked_at": 1})
    state.write_tunnel_state({"ok": False, "checked_at": 2})
    assert writes == [SNAP, SNAP]
    assert _snapshot(data_dir)["subjects"]["tunnel"]["observed"]["ok"] is False


def test_no_bounded_silence_rule_any_more(data_dir, writes):
    """**没有**「最长静默写一次」这条规则了（2026-09-29 随新鲜度判据一起取消）。

    旧规则存在的唯一理由是喂「mtime / 时刻戳」这个新鲜度判据。现在新鲜度由**连接本身**
    回答（连接在 = 事实是推过来的、随变的；连接不在 = 不必再判新鲜度），于是即使
    ``checked_at`` 一刻不停地动，只要**别的内容**没变，盘上那份就一次都不写。
    稳态下它可以整天零写 —— 这正是「杜绝不必要的读写」要的结果。
    """
    for i in range(1, 1001):
        state.write_tunnel_state({"ok": True, "checked_at": i})
    assert writes == [SNAP], "一千轮里只写了第一次"


def test_one_snapshot_holds_every_subject(data_dir, writes):
    """**一个**文件装全部 subject，且改一台机不会把另外两台顶掉。

    旧形态每 subject 一份 ⇒ N 次 ``tmp + rename``，每次还让目录变两次（当年把 app 侧
    监听从 30 秒一轮抬成 5 秒一轮的就是这个）。
    """
    state.write_state({"ok": True, "checked_at": 1})
    state.write_tunnel_state({"port": 1080, "checked_at": 1})
    state.write_hosts_state({"count": 0, "checked_at": 1})
    assert writes == [SNAP, SNAP, SNAP]

    snap = _snapshot(data_dir)
    assert snap["version"] == state.SNAPSHOT_VERSION
    assert set(snap["subjects"]) == {"route", "tunnel", "hosts"}

    before = len(writes)
    state.write_tunnel_state({"port": 1080, "ok": False, "checked_at": 2})   # 只改隧道这台
    assert len(writes) == before + 1, "整份重写**一次**（不是每 subject 一次）"
    snap = _snapshot(data_dir)
    assert snap["subjects"]["tunnel"]["observed"]["ok"] is False
    assert snap["subjects"]["route"]["observed"]["ok"] is True, "另外两台的内容仍在盘上"


def test_intent_is_recorded_alongside_the_observation(data_dir, writes):
    """「最近一次生效的意图」与观测同处一份 —— 崩溃重启后靠它判「该不该重新施加」。"""
    state.note_intent(state.SUBJECT_ROUTE, {"poll": 30})
    assert writes == [SNAP]
    snap = _snapshot(data_dir)
    assert snap["subjects"][state.SUBJECT_ROUTE]["intent"] == {"poll": 30}


def test_deleted_snapshot_is_not_rebuilt_until_something_changes(data_dir, writes):
    """快照**不是通道** ⇒ 「文件还在不在」不构成「该不该写」的理由。

    人把它删了就是删了（守护不会为了「文件应该在」而重写一份 —— 那条理由是通道时代的
    产物：那时对面在等它）。真有事发生时照常重写。
    """
    state.write_tunnel_state({"ok": True, "checked_at": 1})
    (data_dir / SNAP).unlink()
    state.write_tunnel_state({"ok": True, "checked_at": 2})     # 内容同（时间戳不算内容）
    assert not (data_dir / SNAP).exists(), "内容没变就不该为「文件没了」而重写"
    state.write_tunnel_state({"ok": False, "checked_at": 3})    # 内容真变 ⇒ 重写
    assert (data_dir / SNAP).exists()


def test_snapshot_from_the_future_version_is_ignored(data_dir):
    """形状版本不符按「读不出来」处理 —— 快照只用于崩溃恢复，读不出来最多多收敛一轮。"""
    (data_dir / SNAP).write_text(
        json.dumps({"version": state.SNAPSHOT_VERSION + 1,
                    "subjects": {"route": {"observed": {"ok": True}}}}),
        encoding="utf-8",
    )
    assert state.load_snapshot() == {}


def test_snapshot_serves_memory_not_push(data_dir):
    """快照只喂「该不该重新施加」的判据（``read_state``）。**它不是「此刻的事实」。**

    守护重启后 app 会重连并重新断言全部意图（靠内容哈希幂等）；把盘上那份旧意图
    一起恢复，反而可能把一份早已撤回的期望态又跑一遍。观测同理：它记的是**上一次
    会话结束时的世界**，``observed()``（订阅方问「现在是什么」）拿它作答，
    app 侧就会把昨天当成今天（2026-10-17：冷启动第一次启动误报「当前场景 = office」，
    问了一条本不该问的上班时间）。
    """
    state.note_intent(state.SUBJECT_TUNNEL, {"port": 1080})
    state.write_state({"ok": True, "checked_at": 1})

    # 模拟重启：内存清空（盘上快照还在）
    state._OBSERVED.clear()
    state._INTENTS.clear()

    assert state.observed(state.SUBJECT_ROUTE) == {}, "重启后还没有事实，不是「昨天那份」"
    assert state.read_state()["ok"] is True, "「该不该重新施加」仍要读得到上次那份"
    assert state.SUBJECT_TUNNEL not in state._INTENTS, "意图不从盘上恢复"
