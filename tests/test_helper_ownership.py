"""守护侧「一个 subject 只允许一个写者」的离屏自证（方案 §9）。

为什么单列一个文件
------------------
这条规则守的不是「谁先发谁算数」这种小事，而是**一条连接断开会拆掉另一条连接正在
用的东西**：隧道是依附型 subject（``EPHEMERAL_SUBJECTS``），连接一断就撤；在
last-write-wins 的旧形态下，A 与 B 都「声明过」tunnel，于是 A 一退出就把 B 的隧道
撤了 —— 而 B 可能正是当前唯一在用它的人。

不立的后果还有更日常的一条：两个客户端互相覆盖，谁的期望态算数取决于谁后发一帧。

测的是 ``core._Dispatcher`` 的**判定**，所以两侧都用替身（连接 / 编排层 / 服务端）：
真套接字与真 task 表归 ``test_session.py`` 与 ``test_helper_tasks.py``，这里只问
「这一帧该不该被拒、拒了之后所有权归谁」。``state.note_intent`` 被顶掉 —— 它会写
崩溃恢复快照，而本组用例一个字节都不该往用户数据目录里落。
"""

from __future__ import annotations

import pytest

from barkit.capabilities.helper import core as C
from barkit.capabilities.helper import protocol as P
from barkit.capabilities.helper import session as S
from barkit.capabilities.helper import state as st


class _Conn:
    """一条连接的替身，只带 ``_Dispatcher`` 真会碰的那几个字段。"""

    def __init__(self, client: str = "app", uid: int = 501, pid: int = 1000) -> None:
        self.client = client
        self.peer = S.PeerInfo(uid=uid, gid=20, pid=pid)
        self.intents: set = set()
        self.subscriptions: set = set()
        self.sent: list = []

    def reasons(self) -> list:
        """这条连接收到的 ``error`` 帧的原因码（**按到达顺序**）。"""
        return [m.get("reason") for m in self.sent if P.message_type(m) == P.ERROR]


class _Runner:
    """编排层的替身：只留「某 subject 的意图现在是什么」这一件事。"""

    def __init__(self) -> None:
        self._intents: dict = {}
        self.tables: list = []

    def intent(self, subject):
        return self._intents.get(subject)

    def set_intent(self, subject, payload):
        self._intents[subject] = payload
        return True

    def apply_table(self, table):
        self.tables.append(table)


class _Server:
    """会话服务端的替身：把每一帧记在**收件人**上。"""

    def send(self, conn, message):
        conn.sent.append(message)
        return True


@pytest.fixture
def rig(monkeypatch):
    """一组干净的 (dispatcher, server, runner)。"""
    # ``note_intent`` 会写用户数据目录下的崩溃恢复快照 —— 本组一个字节都不该落盘。
    monkeypatch.setattr(st, "note_intent", lambda subject, payload: None)
    server, runner = _Server(), _Runner()
    return C._Dispatcher(server, runner), server, runner


def _set(subject, payload, **kw):
    return P.intent_set(subject, payload, **kw)


# ──────────────────────────────────────────────
# 1. 拒：第二个写者不许覆盖
# ──────────────────────────────────────────────

def test_second_writer_is_rejected_and_cannot_overwrite(rig):
    """不同 client 的第二个写者被拒，且**第一个写者的期望态原封不动**。

    后半句是承重的那条：只断言「被拒了」是同义反复（回一帧 error 谁都会），
    真正要守住的是「第二个写者没能把它覆盖掉」。
    """
    d, _server, runner = rig
    a, b = _Conn("app", pid=11), _Conn("cli", pid=22)
    d._on_intent(a, _set("route", {"dns": "1.1.1.1"}))
    d._on_intent(b, _set("route", {"dns": "8.8.8.8"}))
    assert runner.intent("route") == {"dns": "1.1.1.1"}, "第二个写者把它覆盖了"
    assert b.reasons() == [P.REASON_CLAIMED]
    assert a.reasons() == []


def test_the_owning_writer_can_still_update_its_own_subject(rig):
    """同一个写者改自己的期望态永远放行 —— 拒了就等于「声明一次就再也改不动」。"""
    d, _server, runner = rig
    a = _Conn()
    d._on_intent(a, _set("route", {"v": 1}))
    d._on_intent(a, _set("route", {"v": 2}))
    assert runner.intent("route") == {"v": 2}
    assert a.reasons() == []


def test_unknown_subject_is_rejected_before_ownership_is_considered(rig):
    """校验顺序没被新逻辑挪位：不认识的 subject 照旧回 ``bad_request``。"""
    d, _server, runner = rig
    a = _Conn()
    d._on_intent(a, _set("nope", {"v": 1}))
    assert a.reasons() == [P.REASON_BAD_REQUEST]
    assert runner.intent("nope") is None


# ──────────────────────────────────────────────
# 2. 接管：换人要走明路
# ──────────────────────────────────────────────

def test_explicit_takeover_transfers_ownership_and_tells_the_previous_writer(rig):
    """``takeover=true`` 换人：所有权跟着走，**并且原写者被如实告知**。

    不告知的那半是这里真正新增的东西 —— 原写者会一直以为自己还在管着这件事，
    直到几小时后自己发下一帧才发现被拒。
    """
    d, _server, runner = rig
    a, b = _Conn("app", pid=11), _Conn("cli", pid=22)
    d._on_intent(a, _set("route", {"v": 1}))
    d._on_intent(b, _set("route", {"v": 2}, takeover=True))
    assert runner.intent("route") == {"v": 2}
    assert a.reasons() == [P.REASON_TAKEOVER]
    assert "route" not in a.intents
    assert "route" in b.intents


def test_rejection_frames_name_the_subject_they_rejected(rig):
    """两条拒绝帧都要带上被拒的 subject —— app 侧**只能**靠它归因。

    意图帧没有 ``req_id``，这一栏是 app 侧「我声明的期望态算不算数」的唯一读数来源
    （``helper_link.intent_rejection`` 读它）。缺了它，被拒在 app 侧只剩一行 warning，
    组件会一直以为自己声明成功了。
    """
    d, _server, _runner = rig
    a, b = _Conn("app", pid=11), _Conn("cli", pid=22)
    d._on_intent(a, _set("route", {"v": 1}))
    d._on_intent(b, _set("route", {"v": 2}))
    assert [m.get("subject") for m in b.sent
            if P.message_type(m) == P.ERROR] == ["route"]

    c = _Conn("third", pid=33)
    d._on_intent(c, _set("route", {"v": 3}, takeover=True))
    assert [m.get("subject") for m in a.sent
            if P.message_type(m) == P.ERROR] == ["route"]


def test_takeover_must_be_an_explicit_boolean_true(rig):
    """「带了个 takeover 字段」不等于「要接管」：只有布尔 True 才算。

    否则 ``takeover=false`` / ``takeover=''`` 这类顺手带上的字段会把拒的那半架空。
    """
    d, _server, runner = rig
    a, b = _Conn("app", pid=11), _Conn("cli", pid=22)
    d._on_intent(a, _set("route", {"v": 1}))
    d._on_intent(b, dict(_set("route", {"v": 2}), takeover=False))
    d._on_intent(b, dict(_set("route", {"v": 2}), takeover="yes"))
    assert b.reasons() == [P.REASON_CLAIMED, P.REASON_CLAIMED]
    assert runner.intent("route") == {"v": 1}


# ──────────────────────────────────────────────
# 3. 断开：只撤**自己名下**的，且把写者身份交还
# ──────────────────────────────────────────────

def test_a_closing_connection_does_not_tear_down_the_live_owners_tunnel(rig):
    """**本轮要修的那个 bug**：A 断开，不该撤掉此刻归 B 的隧道。

    A 曾经声明过 tunnel（所以旧形态下它的 ``intents`` 里有 tunnel），但写者身份
    已经被 B 接管 —— 交还身份时必须连 ``intents`` 一起摘掉，否则 A 一退出就把
    B 正在用的隧道拆了。
    """
    d, _server, runner = rig
    a, b = _Conn("app", pid=11), _Conn("app", pid=22)
    d._on_intent(a, _set("tunnel", {"peer": "A"}))
    d._on_intent(b, _set("tunnel", {"peer": "B"}, takeover=True))
    assert runner.intent("tunnel") == {"peer": "B"}
    d.on_close(a)
    assert runner.intent("tunnel") == {"peer": "B"}, "A 断开把 B 的隧道撤了"


def test_close_withdraws_ephemeral_subjects_the_connection_still_owns(rig):
    """反过来那半：还**在自己名下**的依附型意图，断开时照旧要撤（不能顺手不撤了）。"""
    d, _server, runner = rig
    a = _Conn()
    d._on_intent(a, _set("tunnel", {"peer": "A"}))
    assert "tunnel" in a.intents
    d.on_close(a)
    assert runner.intent("tunnel") is None, "依附型意图没被撤：守护会一直挂着隧道"
    assert a.intents == set()


def test_close_leaves_self_sustaining_subjects_alone(rig):
    """自持型意图（route / hosts）表达的是「系统该保持成这样」，app 死了也不推平。"""
    d, _server, runner = rig
    a = _Conn()
    d._on_intent(a, _set("route", {"dns": "1.1.1.1"}))
    d.on_close(a)
    assert runner.intent("route") == {"dns": "1.1.1.1"}


def test_close_hands_the_subject_back_to_the_next_writer(rig):
    """断开必须交还写者身份 —— 否则一条死连接会永远挡着下一个写者。"""
    d, _server, runner = rig
    a, b = _Conn("app", pid=11), _Conn("cli", pid=22)
    d._on_intent(a, _set("route", {"v": 1}))
    d.on_close(a)
    d._on_intent(b, _set("route", {"v": 2}))
    assert b.reasons() == [], "死连接还在占着 route ⇒ 新写者永远进不来"
    assert runner.intent("route") == {"v": 2}


# ──────────────────────────────────────────────
# 4. 重连：别把自己挡在门外
# ──────────────────────────────────────────────

def test_same_client_reconnect_is_not_blocked_by_its_own_stale_connection(rig):
    """同一个 client 重连视为**同一写者**，不用带 takeover。

    守护是单线程：它可能在一轮 ``select`` 里先处理新连接、后处理旧连接的 EOF，
    于是「重连后的自己」会被「还没被回收的自己」挡住。那不是两个客户端在抢，
    是同一个客户端在重新声明 —— 挡住它等于意图再也声明不上了。
    """
    d, _server, runner = rig
    a, again = _Conn("app", pid=11), _Conn("app", pid=12)
    d._on_intent(a, _set("route", {"v": 1}))
    d._on_intent(again, _set("route", {"v": 1}))
    assert again.reasons() == [], "重连重发被自己的旧连接挡住了"
    assert runner.intent("route") == {"v": 1}
    assert a.reasons() == [P.REASON_TAKEOVER]       # 旧连接被如实告知


def test_unhandshaked_connection_is_not_treated_as_the_same_writer(rig):
    """还没握手的连接 ``client`` 是空串 —— **不许**把它当成同一个写者。

    否则所有「连上就直接声明意图、不握手」的连接会被判成同一个写者，版本闸门
    （握手那一帧）等于被绕过去了。
    """
    d, _server, runner = rig
    a, quiet = _Conn("app", pid=11), _Conn("", pid=22)
    d._on_intent(a, _set("route", {"v": 1}))
    d._on_intent(quiet, _set("route", {"v": 2}))
    assert quiet.reasons() == [P.REASON_CLAIMED]
    assert runner.intent("route") == {"v": 1}


# ──────────────────────────────────────────────
# 5. 撤回：subject 从此无人认领
# ──────────────────────────────────────────────

def test_retraction_hands_the_subject_back(rig):
    """撤回之后再声明不需要 takeover —— 没有期望态的 subject 不该还被占着。"""
    d, _server, runner = rig
    a, b = _Conn("app", pid=11), _Conn("cli", pid=22)
    d._on_intent(a, _set("route", {"v": 1}))
    d._on_intent(a, _set("route", None))                 # 撤回
    d._on_intent(b, _set("route", {"v": 2}))             # 不同 client，无需接管
    assert b.reasons() == [], "撤回之后 subject 还被占着"
    assert runner.intent("route") == {"v": 2}
    assert "route" not in a.intents


# ──────────────────────────────────────────────
# 6. 握手闸门：没对过版本，不许说话
# ──────────────────────────────────────────────
#
# 上一节测的是「写者身份归谁」，这一节测的是更前面那一道：**这条连接有没有资格
# 发业务帧**。契约版本只在 ``hello`` 那一帧对一次，没握手的连接版本未知 —— 它的
# payload 形状是不是本版认得的那种，谁也不知道；而意图帧会真的去改路由 / 隧道。
# 不设这道闸，版本闸门就成了「想起来才对一次」：绕过去的办法只是少发一帧 hello。

def test_intent_before_hello_changes_nothing(rig):
    """未握手就声明意图：回 ``no_handshake``，**系统期望态一个字节都不动**。"""
    d, _server, runner = rig
    quiet = _Conn("")
    d.handle(quiet, _set("tunnel", {"up": True}))
    assert quiet.reasons() == [P.REASON_NO_HANDSHAKE]
    assert runner.intent("tunnel") is None, "没对过版本就改了系统状态"


def test_the_same_frame_is_accepted_after_hello(rig):
    """反向那半：闸门只挡「没握手」这一种，握过手的同一帧照常放行。

    少了这条，把闸门写成「一律拒」也能全绿 —— 那是把功能关掉而不是守住它。
    """
    d, _server, runner = rig
    conn = _Conn("")
    d.handle(conn, _set("tunnel", {"up": True}))         # 拒
    d.handle(conn, P.hello(client="app"))                # 握手
    d.handle(conn, _set("tunnel", {"up": True}))         # 同一帧，这次放行
    assert conn.reasons() == [P.REASON_NO_HANDSHAKE], "握手之后还在拒"
    assert runner.intent("tunnel") == {"up": True}


def test_the_gate_comes_before_type_dispatch(rig):
    """闸门必须**先于**类型判定 —— 否则「不认识的类型」那条支会先开口。"""
    d, _server, _runner = rig
    quiet, known = _Conn(""), _Conn("app")
    d.handle(quiet, {"type": "nonsense"})
    d.handle(known, {"type": "nonsense"})                # 对照组
    assert quiet.reasons() == [P.REASON_NO_HANDSHAKE]
    assert known.reasons() == [P.REASON_BAD_TYPE]


def test_subscribe_before_hello_is_refused(rig):
    """读/订阅也在闸门之内：它同样按本版协议解析对端发来的东西。"""
    d, _server, _runner = rig
    quiet = _Conn("")
    d.handle(quiet, P.subscribe(["route"]))
    assert quiet.reasons() == [P.REASON_NO_HANDSHAKE]
    assert quiet.subscriptions == set(), "没握手就订阅上了"


def test_op_run_before_hello_does_not_execute(rig, monkeypatch):
    """``op.run`` 是真去动系统的那一类 —— 未握手时**一次都不许执行**。"""
    d, _server, _runner = rig
    seen = []

    def _fake(op, params, runner):
        seen.append(op)
        return {"ok": True}, None

    monkeypatch.setattr(C.primitives, "run_op", _fake)
    quiet = _Conn("")
    d.handle(quiet, P.op_run("tunnel.up", {"x": 1}, req_id="r1"))
    assert quiet.reasons() == [P.REASON_NO_HANDSHAKE]
    assert seen == [], "没握手的连接执行了 root 操作"
