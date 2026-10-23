"""helper 侧 ``state`` 的**推送**判据：按变化推，不按节拍推。

真机现象（2026-09-29）：日志里「按需刷新：ipad_tunnel（helper_observed）」每 5 秒
一条 —— 隧道那一轮 5 秒一跑，``record`` 就推一次，订阅方就被叫醒跑一整轮 tick。
换通道只是把「被 30 秒必写的状态文件叫醒」换了张脸，病灶没动：**每轮无条件推**。

所以本组用例盯的是「推几次」，不是「推了什么」：

1. 内容没变（只有 ``checked_at`` 在动）⇒ **一秒都不推**；
2. 内容真变 ⇒ 立刻推；
3. 内容一直没变但超过心跳上限 ⇒ 补推一次（喂住消费方的过期判据）；
4. 首次一定推（没有「上次」可比）。

``state`` 是**进程级**内存（``_OBSERVED`` / ``_PUSHED_*`` / 落盘签名），每条用例都要
清干净 —— 否则上一条的观测变成这一条的事实，而「没变就不推」会让用例静默假绿。
"""

from __future__ import annotations

import json
import time

import pytest

from barkit.capabilities.helper import safeio
from barkit.capabilities.helper import state


@pytest.fixture
def push_probe(tmp_path, monkeypatch):
    """重定向落盘并装一个记录用的推送口；返回「被推过什么」。"""
    monkeypatch.setattr(state, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(safeio, "data_dir", lambda: tmp_path)     # log() 落 tmp
    monkeypatch.setattr(state, "_OBSERVED", {})
    monkeypatch.setattr(state, "_INTENTS", {})
    monkeypatch.setattr(state, "_FLUSHED_OBSERVED", {})
    monkeypatch.setattr(state, "_LAST_SIGNATURE", None)
    monkeypatch.setattr(state, "_PUSHED_SIG", {})
    monkeypatch.setattr(state, "_PUSHED_AT", {})

    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(state, "_PUSHER",
                        lambda subject, observed: seen.append((subject, dict(observed))))
    return seen


def _fact(checked_at: int, **extra) -> dict:
    """一份隧道观测：``checked_at`` 是易动字段（每轮都在变），其余是「看到了什么」。"""
    return {"active": False, "utun": None, "error": None,
            "checked_at": checked_at, **extra}


# ──────────────────────────────────────────────
# 去重
# ──────────────────────────────────────────────

def test_first_round_is_always_pushed(push_probe):
    """首次没有「上次」可比 ⇒ 必须推：订阅方刚连上就该有事实。"""
    state.record("tunnel", _fact(1000))
    assert len(push_probe) == 1


def test_unchanged_fact_is_not_pushed_again(push_probe):
    """稳态：连着十轮结论逐字相同（只有 ``checked_at`` 在动）⇒ 只推第一次。"""
    for i in range(10):
        state.record("tunnel", _fact(1000 + i))
    assert len(push_probe) == 1, "每轮无条件推就是真机那次刷屏"


def test_a_real_change_is_pushed_immediately(push_probe):
    """内容真变了 ⇒ 立刻推（这才是订阅方要的「按需刷新」）。"""
    state.record("tunnel", _fact(1000))
    state.record("tunnel", _fact(1001, active=True, utun="utun9"))
    assert len(push_probe) == 2
    assert push_probe[1][1]["utun"] == "utun9"


def test_subjects_are_deduped_independently(push_probe):
    """隧道没变不该挡住路由的推送（按 subject 分开记，不是一把全局锁）。"""
    state.record("tunnel", _fact(1000))
    state.record("route", {"iface": "en5"})
    state.record("tunnel", _fact(1001))                       # 没变 ⇒ 不推
    state.record("route", {"iface": "en5"})                   # 没变 ⇒ 不推
    state.record("route", {"iface": "en5", "managed": True})  # 变了 ⇒ 推
    assert [(s, ) for s, _ in push_probe] == [("tunnel",), ("route",), ("route",)]


# ──────────────────────────────────────────────
# 心跳（喂住消费方的过期判据）
# ──────────────────────────────────────────────

def test_silent_for_too_long_pushes_anyway(push_probe):
    """内容一直没变，但超过心跳上限 ⇒ 补推一次。

    消费方靠 ``checked_at`` 判「守护还在不在跑这一轮」，而**连接活着只说明进程在**。
    所以这一档不能省 —— 少了它，稳态下 180 秒后就会误报「守护未回报」。
    """
    state.record("tunnel", _fact(1000))
    state._PUSHED_AT["tunnel"] = time.monotonic() - state.PUSH_HEARTBEAT_S - 1
    state.record("tunnel", _fact(1001))
    assert len(push_probe) == 2
    # 补推的那份带的是**此刻**的 checked_at，否则过期判据照样会误报
    assert push_probe[1][1]["checked_at"] == 1001


def test_heartbeat_stays_well_inside_the_consumer_threshold():
    """心跳间隔必须留在消费方过期阈值以内（隧道侧 180 秒），否则稳态下会误报失联。

    两个数字分别在守护侧与插件侧，任何一处单改都不会报错 —— 只能靠这条钉住。
    """
    # 阈值住在插件的 `constants.py`（2026-10-01 拆包前它在 `plugin.py`）
    from barkit.plugins.ipad_tunnel.constants import DAEMON_REPORT_MAX_AGE_S

    assert state.PUSH_HEARTBEAT_S < DAEMON_REPORT_MAX_AGE_S
    assert DAEMON_REPORT_MAX_AGE_S - state.PUSH_HEARTBEAT_S >= 30, "余量不够就太贴了"


def test_heartbeat_does_not_restart_the_clock_on_every_round():
    """「没变就不推」不得顺手把心跳时刻刷成此刻 —— 那等于心跳永不触发。"""
    state.record("tunnel", _fact(1000))
    first_at = state._PUSHED_AT["tunnel"]
    time.sleep(0.01)
    state.record("tunnel", _fact(1001))
    assert state._PUSHED_AT["tunnel"] == first_at


# ──────────────────────────────────────────────
# 失败路径
# ──────────────────────────────────────────────

def test_a_failing_pusher_does_not_stop_the_cycle(tmp_path, monkeypatch):
    """推送失败不该让业务轮停摆（只记日志），落盘照旧。"""
    monkeypatch.setattr(state, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(safeio, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(state, "_OBSERVED", {})
    monkeypatch.setattr(state, "_FLUSHED_OBSERVED", {})
    monkeypatch.setattr(state, "_LAST_SIGNATURE", None)
    monkeypatch.setattr(state, "_PUSHED_SIG", {})
    monkeypatch.setattr(state, "_PUSHED_AT", {})

    def _boom(subject, observed):
        raise OSError("socket is gone")

    monkeypatch.setattr(state, "_PUSHER", _boom)
    state.record("tunnel", _fact(1000))                     # 不抛
    assert state.observed("tunnel")["active"] is False      # 内存那份照旧记下了


# ──────────────────────────────────────────────
# 崩溃恢复：记忆 ≠ 此刻的事实
# ──────────────────────────────────────────────

def test_restored_snapshot_is_a_memory_not_a_fact(tmp_path, monkeypatch):
    """崩溃恢复快照里的那份观测**不能**冒充「此刻的事实」。

    2026-10-17 真机：10-16 18:23 最后一轮 office ``apply=True``（人在办公室），
    18:26 退出；10-17 09:23 开机守护重启，09:40 app 启动。旧写法启动时把快照灌进
    内存观测，app 一订阅（握手「立刻推一份当前的」）就收到**昨天那份** ⇒ 场景变量
    当场读成 ``office`` ⇒ 条件成立 ⇒ 问了一条「今天几点上班」，而那时 en5 根本不在这台
    机器上。第一轮 ``run_cycle`` 跑完（约 1 秒）才推回真相，账已经记了。

    两份东西必须分开：``read_state()``（判「该不该重新施加」）吃快照，
    ``observed()``（答订阅方「现在是什么」）只吃本轮 ``record`` 的事实。
    """
    monkeypatch.setattr(state, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(safeio, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(state, "_OBSERVED", {})
    monkeypatch.setattr(state, "_FLUSHED_OBSERVED", {})
    monkeypatch.setattr(state, "_LAST_SIGNATURE", None)
    monkeypatch.setattr(state, "_PUSHED_SIG", {})
    monkeypatch.setattr(state, "_PUSHED_AT", {})

    yesterday = {"ok": True, "rules": [{"name": "office", "trigger_matched": True}]}
    state.record(state.SUBJECT_ROUTE, yesterday)

    # 模拟守护重启：内存清空（盘上快照还在）
    state._OBSERVED.clear()

    assert state.read_state()["rules"][0]["name"] == "office", \
        "「该不该重新施加」要能吃到快照"
    assert state.observed(state.SUBJECT_ROUTE) == {}, \
        "此刻的事实不能是上次退出前那一刻的世界"


def test_session_close_forgets_the_current_fact(tmp_path, monkeypatch):
    """会话全断 ⇒ 内存那份「此刻」作废，盘上快照照旧可用。

    留着它的后果：下一个 app 连上时，握手推的是**上一次会话结束时的世界**
    （2026-10-17 的误报就发生在这一刻）。守护长跑时这条比「启动灌快照」更容易撞 ——
    app 关了又开，中间没有任何一轮 ``record`` 去覆盖它。
    """
    monkeypatch.setattr(state, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(safeio, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(state, "_OBSERVED", {})
    monkeypatch.setattr(state, "_FLUSHED_OBSERVED", {})
    monkeypatch.setattr(state, "_LAST_SIGNATURE", None)
    monkeypatch.setattr(state, "_PUSHED_SIG", {})
    monkeypatch.setattr(state, "_PUSHED_AT", {})

    fact = {"ok": True, "rules": [{"name": "office", "trigger_matched": True}]}
    state.record(state.SUBJECT_ROUTE, fact)
    state.forget_observed()

    assert state.observed(state.SUBJECT_ROUTE) == {}, "会话没了就没有「此刻」"
    assert state.read_state()["rules"][0]["name"] == "office", "崩溃恢复要的那份还在盘上"
    assert state._PUSHED_SIG == {}, "下一个会话的第一轮必须真推一次"


def test_flush_by_another_subject_keeps_the_forgotten_observation(tmp_path, monkeypatch):
    """route 的内存观测被作废后，**别的 subject** 的 flush 不得把盘上那节写空。

    2026-10-19 真机：app 重启 ⇒ 断连 ``forget_observed()`` ⇒ 几秒后 hosts 那轮
    ``record`` 触发 flush ⇒ 快照里 route 那节（含 ``net_gateways`` 账）被写成
    ``{}`` ⇒ 下一轮 ``run_cycle`` 读 prev 没了账 ⇒ office 命中后三个目标全 ``hold``。
    旧写法 ``subjects`` 取并集、observed 只按内存取 —— 内存没有的就写空，正是这一条。
    """
    monkeypatch.setattr(state, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(safeio, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(state, "_OBSERVED", {})
    monkeypatch.setattr(state, "_INTENTS", {})
    monkeypatch.setattr(state, "_FLUSHED_OBSERVED", {})
    monkeypatch.setattr(state, "_LAST_SIGNATURE", None)
    monkeypatch.setattr(state, "_PUSHED_SIG", {})
    monkeypatch.setattr(state, "_PUSHED_AT", {})
    monkeypatch.setattr(state, "_PUSHER", lambda subject, observed: None)

    fact = {"ok": True, "checked_at": 1000,
            "net_gateways": {"en5": {"gateway": "10.99.1.254"}}}
    state.record(state.SUBJECT_ROUTE, fact)
    state.forget_observed()
    state.record(state.SUBJECT_HOSTS, {"count": 4, "checked_at": 1001})

    on_disk = (state.load_snapshot()["subjects"].get(state.SUBJECT_ROUTE) or {}).get("observed")
    assert on_disk and on_disk["net_gateways"]["en5"]["gateway"] == "10.99.1.254", \
        "账被别的 subject 的 flush 抹掉，守护重启后就再也叠不上路由"
    assert state.read_state().get("net_gateways", {}).get("en5", {}).get("gateway") \
        == "10.99.1.254", "下一轮 run_cycle 的 prev 必须还拿得到账"


def test_restart_keeps_the_gateway_ledger(tmp_path, monkeypatch):
    """守护**进程重启**后第一次 flush 不得把盘上的 ``net_gateways`` 账写空。

    2026-10-20 真机：开机后 app 声明意图 ⇒ ``note_intent`` 触发 flush，而新进程的
    ``_OBSERVED`` / ``_FLUSHED_OBSERVED`` 都是空的 ⇒ ``_composed_observed()`` 对
    route 只能给出 ``{}`` ⇒ 盘上账被抹平 ⇒ 紧接着 ``run_cycle`` 的 ``read_state()``
    读到空账 ⇒ office 三个目标全 ``hold``。修法是 flush 前先认盘上那份 observed
    （``_seed_flushed``）；本用例锁住「跨进程」这一路，上一版只堵了「同进程内别的
    subject 触发 flush」。
    """
    monkeypatch.setattr(state, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(safeio, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(state, "_OBSERVED", {})
    monkeypatch.setattr(state, "_INTENTS", {})
    monkeypatch.setattr(state, "_FLUSHED_OBSERVED", {})
    monkeypatch.setattr(state, "_LAST_SIGNATURE", None)
    monkeypatch.setattr(state, "_PUSHED_SIG", {})
    monkeypatch.setattr(state, "_PUSHED_AT", {})
    monkeypatch.setattr(state, "_SEEDED", False)      # 新进程：什么都没灌过
    monkeypatch.setattr(state, "_PUSHER", lambda subject, observed: None)

    # 上一个进程退出时盘上留下的现场：route 账在。
    ledger = {"ok": True, "checked_at": 1000,
              "net_gateways": {"en5": {"gateway": "10.99.1.254"}},
              "rules": [], "net": {"iface": "en5"}}
    (tmp_path / state.SNAPSHOT_NAME).write_text(
        json.dumps({"version": state.SNAPSHOT_VERSION, "written_at": 1000,
                    "subjects": {state.SUBJECT_ROUTE: {
                        "intent": {"rules": [], "paused": False},
                        "observed": ledger}}}),
        encoding="utf-8")

    # 开机后第一帧：app 声明意图（真机 08:35:15 就是 hosts 先触发 flush）。
    state.note_intent(state.SUBJECT_HOSTS, {"count": 4})

    on_disk = (state.load_snapshot()["subjects"].get(state.SUBJECT_ROUTE) or {}).get("observed")
    assert on_disk and on_disk["net_gateways"]["en5"]["gateway"] == "10.99.1.254", \
        "重启后第一次 flush 把账写空，开机后路由永远叠不上"
    assert state.read_state().get("net_gateways", {}).get("en5", {}).get("gateway") \
        == "10.99.1.254", "run_cycle 的 prev 必须还拿得到账"
