"""会话传输层的离屏自证（协议分帧 / 服务端 / app 侧客户端）。

不碰真机、不用 root：全程在 ``tmp_path`` 里建套接字，两侧都是本进程的普通权限。
真机上多出来的是两件（**由第 0 步实测钉住**，本文件测不到）：
- ``LOCAL_PEERCRED`` 读出的 uid 是否真的等于对端用户 —— 这里退化成「双方同 uid」，
  故只能测「允许的 uid 不符时拒绝」这条逻辑，不能测「读出来对不对」；
- root 在用户私有目录里建节点并改属主 —— 这里以当前用户身份建，属主天然正确。
"""

from __future__ import annotations

import ast
import json
import os
import selectors
import shutil
import socket
import struct
import tempfile
import time
from pathlib import Path

import pytest

from barkit.capabilities import helper_link as H
from barkit.capabilities.helper import protocol as P
from barkit.capabilities.helper import session as S


@pytest.fixture
def sockdir():
    """短路径临时目录。

    Unix 域套接字的 ``sun_path`` 在 Darwin 上只有 103 字节，而 pytest 的 ``tmp_path``
    会展开成 ``/private/var/folders/…/pytest-of-…/test_…0``，轻松超限。
    那是**测试环境**的问题，不是产品路径的问题：生产的
    ``~/Library/Application Support/BarKit/helper.sock`` 只有 54 字节。
    """
    d = tempfile.mkdtemp(prefix="bk", dir="/tmp")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ──────────────────────────────────────────────
# 1. 线格式：分帧
# ──────────────────────────────────────────────

def test_frame_roundtrip_keeps_messages_apart():
    """一次喂三条帧 ⇒ 收到三条。"""
    reader = P.FrameReader()
    blob = (P.encode(P.intent_set("route", {"a": 1}))
            + P.encode(P.get("route", "r1"))
            + P.encode(P.event_push("route", {"x": 2})))
    msgs = reader.feed(blob)
    assert [P.message_type(m) for m in msgs] == [P.INTENT_SET, P.GET, P.EVENT_PUSH]
    assert msgs[0]["subject"] == "route" and msgs[-1]["observed"] == {"x": 2}
    assert reader.pending() == 0


def test_intent_set_carries_takeover_only_when_asked():
    """``takeover`` 只在**确实要接管**时才写进帧。

    顺手带上的字段（``takeover=false``）不该出现在帧里 —— 否则守护侧「只有布尔
    True 才算接管」的那半判定被架空：字段存在与否变成新的真源。
    """
    assert "takeover" not in P.intent_set("route", {"v": 1})
    assert "takeover" not in P.intent_set("route", {"v": 1}, takeover=False)
    assert P.intent_set("route", {"v": 1}, takeover=True)["takeover"] is True


def test_declare_can_ask_for_an_explicit_takeover():
    """app 侧 ``declare`` 能把「我要接管」写进帧 —— 否则帧上那个位没人会用。

    不打桩套接字也能测：关心的是「这一帧长什么样」，而 ``_send`` 就是那个出口。
    """
    link = H.HelperLink()
    sent = []

    def _send(message):
        sent.append(message)
        return True

    link._send = _send
    link._state = H.STATE_READY
    link.declare("route", {"v": 1})
    link.declare("route", {"v": 2}, takeover=True)
    assert [m.get("takeover") for m in sent] == [None, True]
    assert [P.message_type(m) for m in sent] == [P.INTENT_SET, P.INTENT_SET]


def test_resend_after_reconnect_keeps_the_takeover_bit():
    """重连重发**必须**把 takeover 一起带上 —— 只记 rev 与 payload 是不够的。

    丢了它，「我声明过要接管」会悄悄降级成「我只是又声明了一次」⇒ 守护按「第二个
    写者」回 ``already_claimed``，而这次被拒在 app 侧**没有任何回执**（意图帧不带
    ``req_id``）。守护重启一次就踩到。
    """
    link = H.HelperLink()
    sent = []
    link._send = lambda m: sent.append(m) or True
    link.declare("route", {"v": 1}, takeover=True)      # 未连接：记下、不送达
    link._on_welcome({"type": P.WELCOME, "ok": True, "helper_version": "0"})
    assert [P.message_type(m) for m in sent] == [P.INTENT_SET]
    assert sent[0].get("takeover") is True


# ──────────────────────────────────────────────
# 1b. 意图被拒：回执（2026-10-02）
# ──────────────────────────────────────────────

def _ready_link():
    """一只握完手、已在等帧的客户端（``_send`` 打成桩，只看出站内容）。"""
    link = H.HelperLink()
    link._state = H.STATE_READY
    link._send = lambda m: True
    return link


def test_rejection_frame_names_the_subject_it_rejected():
    """拒绝帧必须带上被拒的 subject —— 否则 app 侧无从归因。

    意图帧**没有** ``req_id``，配对只能靠这一栏。旧版守护不发它，新版读到空即按
    「无法归因」处理（降级），故这里断言的是「带上时帧里确实有、默认时确实没有」。
    """
    assert P.error(P.REASON_CLAIMED, subject="route")["subject"] == "route"
    assert "subject" not in P.error(P.REASON_CLAIMED)
    assert "subject" not in P.error(P.REASON_BAD_FRAME, subject=None)


def test_rejected_intent_is_recorded_and_queryable():
    """被拒的 subject 要查得到，**不要**只留一行日志。

    ``declare()`` 返回 True 只说明「发出去了」。守护不认的话，组件会一直以为自己声明
    的期望态在生效 —— 面板写着「已固化」，守护其实在替另一条连接管着它。
    """
    link = _ready_link()
    seen = []
    link.on_reject = lambda s, r, d: seen.append((s, r, d))
    link._handle(P.error(P.REASON_CLAIMED, subject="route",
                         detail="写者是另一条会话连接"))
    assert link.intent_rejection("route") == P.REASON_CLAIMED
    assert "另一条会话连接" in link.rejection_detail("route")
    assert seen == [("route", P.REASON_CLAIMED, "写者是另一条会话连接")]
    # 别的 subject 不受牵连
    assert link.intent_rejection("tunnel") == ""


def test_rejection_without_subject_is_not_guessed():
    """读不到 subject（旧版守护）就**不记**，不猜一个出来。

    猜的代价是「某个没被拒的 subject 忽然显示被拒」，比查不到更糟 —— 那是拿猜的
    当事实。此时仍要照旧记一行 warning（通道层面的事本来就归它）。
    """
    link = _ready_link()
    seen = []
    link.on_reject = lambda s, r, d: seen.append(s)
    link._handle(P.error(P.REASON_CLAIMED, detail="写者是另一条会话连接"))
    assert link.intent_rejection("route") == ""
    assert link.diagnostics()["rejections"] == {}
    assert seen == []


def test_only_intent_reasons_land_on_a_subject():
    """帧非法 / 没握手这类**通道层**的拒绝，不该记成「某个 subject 没生效」。

    它们说的是通道本身，硬按 subject 记下去，会把「协议版本不对」读成「我的期望态
    被别人抢了」，处置方向完全相反。
    """
    link = _ready_link()
    link._handle(P.error(P.REASON_NO_HANDSHAKE, subject="route"))
    assert link.intent_rejection("route") == ""
    assert link.diagnostics()["rejections"] == {}


def test_redeclaring_clears_the_previous_verdict():
    """重新声明＝重新主张，上一次的判决不再描述此刻。

    不清的话「已经接管回来了」会一直显示成失败 —— 而这一位是「有没有生效」的唯一
    读数，错了就是静默失效反过来糊住自己。
    """
    link = _ready_link()
    link.declare("route", {"v": 1})
    link._handle(P.error(P.REASON_CLAIMED, subject="route"))
    assert link.intent_rejection("route") == P.REASON_CLAIMED
    link.declare("route", {"v": 2}, takeover=True)
    assert link.intent_rejection("route") == ""


def test_taken_over_counts_as_a_rejection():
    """被接管也是「我声明的期望态不再生效」—— 与「被拒」同一个读数。

    不记的话原写者会一直以为自己还在管着这件事（它下一帧才会发现自己被拒，而那可能
    是几小时之后）。
    """
    link = _ready_link()
    link._handle(P.error(P.REASON_TAKEOVER, subject="hosts"))
    assert link.intent_rejection("hosts") == P.REASON_TAKEOVER


def test_disconnect_forgets_the_verdict():
    """断开后「我被拒了」没有对象了 —— 留着会让重连后成功的那次看上去还像失败。"""
    link = _ready_link()
    link.declare("route", {"v": 1})
    link._handle(P.error(P.REASON_CLAIMED, subject="route"))
    link._teardown("对端关闭")
    assert link.intent_rejection("route") == ""
    assert link.diagnostics()["rejections"] == {}


def test_rejection_is_visible_in_diagnostics():
    """这一段要能贴进 issue：看得出**哪几个** subject 的期望态没生效。"""
    link = _ready_link()
    link._handle(P.error(P.REASON_CLAIMED, subject="route", detail="d1"))
    link._handle(P.error(P.REASON_TAKEOVER, subject="hosts", detail="d2"))
    assert link.diagnostics()["rejections"] == {"hosts": P.REASON_TAKEOVER,
                                                "route": P.REASON_CLAIMED}


def test_frame_reader_waits_for_the_whole_frame():
    """逐字节喂：只在最后一个字节到达时才吐出那一条。"""
    reader = P.FrameReader()
    whole = P.encode(P.op_run("route.renew", {"iface": "en0"}, "q1"))
    delivered = 0
    for i in range(len(whole)):
        delivered += len(reader.feed(whole[i:i + 1]))
    assert delivered == 1
    assert reader.pending() == 0


def test_frame_rejects_oversize_and_garbage():
    """超长帧前缀 / 非 JSON 正文都要当场判非法，**不按那个长度分配内存**。"""
    with pytest.raises(P.FrameError):
        P.FrameReader().feed(b"\xff\xff\xff\xff")
    body = json.dumps([1, 2, 3]).encode()
    with pytest.raises(P.FrameError):
        P.FrameReader().feed(b"\x00\x00\x00\x09" + body)
    with pytest.raises(P.FrameError):
        P.FrameReader().feed(b"\x00\x00\x00\x03" + b"nope")


def test_frame_rejects_deep_nesting_without_crashing():
    """深嵌套 JSON（如 ``[[[…]]]``）的解析异常是 ``RecursionError``（RuntimeError 子类，
    不是 ValueError / FrameError）。

    不接住它，一帧 1 MiB 的嵌套正文就能击穿 ``feed`` 让守护进程崩溃退出、被 launchd
    拉起 ⇒ 崩溃循环。这条测的是「崩不了」—— 收成 FrameError、只断这一条连接。
    """
    nested = ("[" * 200000 + "]" * 200000).encode("utf-8")
    body = b"\x00\x00\x08" + nested  # 长度前缀随便给，feed 在读正文长度前不会崩
    with pytest.raises(P.FrameError):
        P.FrameReader().feed(body)
    # 字面量级别的 RecursionError 也必须是 FrameError，而非冒泡到解释器
    with pytest.raises(P.FrameError):
        P._decode_body(nested)


def test_payload_rev_is_order_insensitive():
    """``rev`` 只认内容：字典键序不影响，值变了才变。"""
    assert P.payload_rev({"b": 1, "a": 2}) == P.payload_rev({"a": 2, "b": 1})
    assert P.payload_rev({"a": 1}) != P.payload_rev({"a": 2})


# ──────────────────────────────────────────────
# 2. 服务端 + 客户端（真套接字，普通权限）
# ──────────────────────────────────────────────

class _MiniServer:
    """一个最小业务端：认 ``hello`` / ``intent.set`` / ``subscribe`` / ``get`` / ``op.run``。"""

    def __init__(self, path, allowed_uid=None):
        self.seen = []
        self.intents = {}
        self.path = str(path)
        self.server = S.SessionServer(self.path, handler=self.on_message,
                                      allowed_uid=allowed_uid)

    def on_message(self, conn, msg):
        self.seen.append(msg)
        kind = P.message_type(msg)
        if kind == P.HELLO:
            self.server.send(conn, P.welcome(ok=True, helper_version="test"))
        elif kind == P.INTENT_SET:
            self.intents[msg["subject"]] = (msg["rev"], msg["payload"])
        elif kind == P.SUBSCRIBE:
            newly = [s for s in msg["subjects"] if s not in conn.subscriptions]
            conn.subscriptions.update(msg["subjects"])
            for s in newly:
                self.server.send(conn, P.event_push(s, {"initial": s}))
        elif kind == P.GET:
            self.server.send(conn, P.reply(msg["req_id"], ok=True,
                                           result={"for": msg["subject"]}))
        elif kind == P.OP_RUN:
            self.server.send(conn, P.reply(msg["req_id"], ok=True,
                                           result={"op": msg["op"]}))


@pytest.fixture
def mini(sockdir):
    m = _MiniServer(sockdir / "helper.sock")
    m.server.open()
    yield m
    m.server.close()


def _handshake(link, m):
    """连上并把握手走完（两侧各推一拍）。"""
    assert link.connect(m.path)
    m.server.serve(0.2)      # 接纳 + 处理 hello
    link.pump()              # 读 welcome
    assert link.connected


def test_public_mutating_link_methods_take_the_lock():
    """改状态机的**公有**出口一律持 ``@_locked``（``declare`` 会由服务线程调用）。

    判据按「这个方法会不会改状态」取，不维护一张方法名单 —— 名单会过期，而服务
    线程与主线程的 ``expire_handshake`` 抢同一份 ``_sock`` / ``_pending`` 是常态
    （守护假死时正是二者同时到场）。
    """
    tree = ast.parse(Path(H.__file__).read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "HelperLink")
    offenders = []
    for fn in cls.body:
        if not isinstance(fn, ast.FunctionDef) or fn.name.startswith("_"):
            continue
        body = ast.unparse(fn)
        mutates = any(t in body for t in ("_teardown(", "_set_state(", "_send("))
        locked = any(ast.unparse(d) == "_locked" for d in fn.decorator_list)
        if mutates and not locked:
            offenders.append(fn.name)
    assert offenders == [], f"这些公有方法改状态却没持锁：{offenders}"


def test_a_raising_push_handler_does_not_drop_the_rest_of_the_batch():
    """订阅方回调炸了，同一批到达的其余帧仍要处理完（事实也不能丢）。

    ``on_push`` 直通主线程的事件总线分发：不隔离时它一抛就冲出 ``pump()``，除了
    丢掉同批剩下的帧，``_expire_pending()`` 那一行也不再执行。
    """
    link = H.HelperLink(client="pytest")
    seen: list = []
    calls = {"n": 0}

    def _flaky(subject, observed):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("订阅方（总线分发）炸了")
        seen.append((subject, observed))

    link.on_push = _flaky
    link._handle(P.event_push("a", {"v": 1}))
    link._handle(P.event_push("b", {"v": 2}))

    assert calls["n"] == 2
    assert seen == [("b", {"v": 2})]
    assert link.snapshot("a") == {"v": 1}, "坏回调不该连这一份事实也丢掉"


def test_server_handshake_intent_and_push(mini):
    link = H.HelperLink(client="pytest")
    _handshake(link, mini)

    assert link.declare("route", {"v": 1})
    mini.server.serve(0.2)
    assert mini.intents["route"][1] == {"v": 1}

    assert link.subscribe(["route"])
    mini.server.serve(0.2)
    link.pump()
    # 订阅即**立刻**推一份当前的 —— 面板打开就有事实，不必等下一次变化。
    assert link.snapshot("route") == {"initial": "route"}

    assert mini.server.push("route", {"v": 2}) == 1
    assert mini.server.push("tunnel", {"v": 3}) == 0   # 没订阅的不推
    link.pump()
    assert link.snapshot("route") == {"v": 2}
    assert link.snapshot("tunnel") is None


def test_helper_version_comes_from_the_handshake_and_is_forgotten_on_disconnect(mini):
    """版本闸门改问本人：连上时读握手自陈的那份，**断开即忘**。

    断开后还留着旧答案，等于在说「此刻跑着的是这一版」—— 而那一刻没人跑，
    下一句「需不需要重装」就会据此说错。
    """
    link = H.HelperLink(client="pytest")
    assert link.helper_version == ""
    _handshake(link, mini)
    assert link.helper_version == "test"
    link.close()
    assert link.helper_version == ""


def test_get_answers_through_the_same_frame_pipe(mini):
    link = H.HelperLink(client="pytest")
    _handshake(link, mini)
    got = []
    link._req_seq += 1
    req_id = "%d-%d" % (os.getpid(), link._req_seq)
    link._send(P.get("route", req_id))
    link._pending[req_id] = (1e18, lambda ok, res, err: got.append((ok, res)))
    mini.server.serve(0.2)
    link.pump()
    assert got == [(True, {"for": "route"})]


def test_op_run_gets_a_frame_level_reply(mini):
    link = H.HelperLink(client="pytest")
    _handshake(link, mini)
    got = []
    assert link.run_op("route.renew", {"iface": "en0"},
                       lambda ok, res, err: got.append((ok, res, err)))
    mini.server.serve(0.2)
    link.pump()
    assert got == [(True, {"op": "route.renew"}, "")]


def test_op_run_without_connection_fails_immediately():
    """没连上就地判失败 —— 旧形态下这会变成「命令排进队列等下一轮」，用户看不到反馈。"""
    link = H.HelperLink()
    got = []
    assert link.run_op("route.renew", None,
                       lambda ok, res, err: got.append((ok, err))) is False
    assert got and got[0][0] is False and "未连接" in got[0][1]


def test_reconnect_reasserts_intents_and_subscriptions(mini):
    """断开重连后**重发全部意图与订阅** —— 幂等靠 rev，不需要水位线。"""
    link = H.HelperLink(client="pytest")
    _handshake(link, mini)
    link.declare("route", {"v": 9})
    link.subscribe(["route"])
    mini.server.serve(0.2)
    link.pump()

    link.close()
    assert not link.connected
    assert link.declare("route", {"v": 9}) is False    # 未连接：记住但不送达

    _handshake(link, mini)
    mini.server.serve(0.2)          # 服务端再读一拍：收下重新断言的意图与订阅
    counts = {}
    for m in mini.seen:
        counts[P.message_type(m)] = counts.get(P.message_type(m), 0) + 1
    assert counts.get(P.INTENT_SET) == 2
    assert counts.get(P.SUBSCRIBE) == 2


def test_declare_same_content_is_not_resent(mini):
    """同一内容重复声明不重复上线（rev 相同即空操作）。"""
    link = H.HelperLink()
    _handshake(link, mini)
    link.declare("route", {"v": 1})
    link.declare("route", {"v": 1})
    mini.server.serve(0.2)
    assert sum(1 for m in mini.seen if P.message_type(m) == P.INTENT_SET) == 1


def test_server_rejects_other_uid(sockdir):
    """允许的 uid 不符 ⇒ 连握手都不给（在 accept 那一刻就关）。"""
    m = _MiniServer(sockdir / "helper.sock", allowed_uid=os.getuid() + 12345)
    m.server.open()
    try:
        link = H.HelperLink()
        # connect() 可能成功（进了 backlog），但服务端接纳时会拒并关闭
        link.connect(m.path)
        m.server.serve(0.3)
        link.pump()
        assert m.server.connections() == []
        assert m.server.rejected_count() == 1
        assert not link.connected
    finally:
        m.server.close()


def test_stale_socket_node_is_replaced(sockdir):
    """上一轮留下的节点会被换掉（Unix 域套接字不会自己消失，不删就 EADDRINUSE）。"""
    p = str(sockdir / "helper.sock")
    first = S.SessionServer(p, handler=lambda c, m: None)
    first.open()
    first.close()
    assert not os.path.exists(p)          # close() 自己收尾，不留空壳
    leftover = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    leftover.bind(p)                      # 模拟被 SIGKILL 的那次残留
    leftover.close()
    assert os.path.exists(p)
    second = S.SessionServer(p, handler=lambda c, m: None)
    second.open()
    try:
        assert os.path.exists(p)
    finally:
        second.close()


def test_serve_without_listener_sleeps_instead_of_spinning(monkeypatch):
    """没有监听的一轮必须**睡过去**，不能立刻返回 —— 否则主循环空转烧满一颗核。

    回归（2026-10-06）：``serve()`` 见 ``_selector is None`` 就 ``return``，而守护的
    主循环是 ``while True: serve(...); run_due()`` ⇒ 建不出节点时（节点路径上留着一个
    普通文件即 ``EADDRINUSE``；路径过长、父目录被锁死同理）守护照旧活着，却以
    100% CPU 一圈圈空转，且**永不重试**，直到重启机器。
    """
    monkeypatch.setattr(S, "LISTENLESS_IDLE_S", 0.2)
    srv = S.SessionServer(str(Path(tempfile.gettempdir()) / "bk-never-opened.sock"),
                          handler=lambda c, m: None)
    started = time.monotonic()
    srv.serve(None)                       # 从没 open() 过
    elapsed = time.monotonic() - started
    assert elapsed >= 0.15, f"没监听时应睡一段再返回，实际只用了 {elapsed:.3f}s（＝空转）"


def test_listen_failure_is_retried_and_heals(sockdir, monkeypatch):
    """建节点失败要**自愈**：障碍被清掉后，不需重启守护也能重新监听。

    触发成本极低 —— 节点路径上放一个普通文件就 ``EADDRINUSE``（``open()`` 只删
    「确实是套接字」的那种，普通文件宁可留着也不误删数据）。旧实现既不重试也不退出：
    守护留在「起不来又不停」的状态里空转。
    """
    monkeypatch.setattr(S, "REOPEN_INTERVAL_S", 0.0)
    monkeypatch.setattr(S, "LISTENLESS_IDLE_S", 0.0)
    p = str(sockdir / "helper.sock")
    Path(p).write_text("不是套接字，别删我")     # 障碍
    srv = S.SessionServer(p, handler=lambda c, m: None)
    with pytest.raises(OSError):
        srv.open()
    assert srv._listen_failed is True, "建节点失败必须记下来，否则 serve() 不会去重试"

    srv.serve(None)                        # 排上重试
    assert srv._reopen_at is not None
    os.unlink(p)                           # 用户把障碍清掉
    srv.serve(None)                        # 重试 → 成功
    try:
        assert srv._listener is not None, "障碍清掉后应当自己重新监听起来"
        assert srv._listen_failed is False
        assert os.path.exists(p)
    finally:
        srv.close()


def test_unknown_message_type_does_not_kill_the_session(mini):
    """不认识的消息类型只该拒**那一帧**（旧契约的「未知字段整份拒」随文件通道一起消失）。"""
    link = H.HelperLink()
    _handshake(link, mini)
    link._send({"type": "no.such.type", "payload": 1})
    mini.server.serve(0.2)
    link.pump()
    assert link.connected
    link.declare("route", {"v": 1})
    mini.server.serve(0.2)
    assert "route" in mini.intents


def test_peer_credentials_are_readable():
    """本机必须**真的**读得到对端 uid/pid —— 读不到就没法在 accept 那一刻拒人。"""
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        peer = S.peer_credentials(a)
        assert peer is not None, "读不到对端凭据：身份校验会退化成不校验"
        assert peer.uid == os.getuid()
        assert peer.pid == os.getpid()
    finally:
        a.close()
        b.close()


# ──────────────────────────────────────────────
# 2b. ``struct xucred`` 的字段偏移（**纯解析**，不需要真套接字）
#
# 为什么单列一组：偏移量写错在本机看不出来 —— uid 读对、身份校验照旧通过，错的
# 那个 ``gid`` 又没人看。而 Darwin 的 ``xucred`` **没有 ``cr_gid`` 字段**，主组是
# ``cr_groups[0]``；早先按「三个 4 字节连排」去解，第三段落在 ``cr_ngroups`` 上，
# 解出来的是**组个数**。两者在小端且填充为 0 时数值恰好相等 ⇒ 错的那个值看着像真的。
# 造字节不需要套接字，所以这组在 Windows 上也能跑。
# ──────────────────────────────────────────────

def _xucred(uid=501, gid=20, ngroups=3, version=0):
    """按 Darwin 的布局造一段 ``xucred`` 字节（布局表在 ``session`` 模块顶部）。"""
    raw = bytearray(S._XUCRED_SIZE)
    struct.pack_into("=I", raw, 0, version)
    struct.pack_into("=I", raw, 4, uid)
    struct.pack_into("=h", raw, S._XUCRED_OFF_NGROUPS, ngroups)
    struct.pack_into("=I", raw, S._XUCRED_OFF_GROUPS, gid)
    return bytes(raw)


def test_xucred_layout_constants_add_up():
    """布局常量必须自洽：``groups`` 起点 + 16 个 gid_t == 结构体长度。

    这条守的是那段注释与 ``_XUCRED_SIZE`` 曾经对不上的那类错（注释写 80、常量写 76）。
    """
    assert S._XUCRED_OFF_GROUPS == 12, "cr_groups 的偏移不是 12 ⇒ 填充算错了"
    assert S._XUCRED_OFF_GROUPS + 16 * 4 == S._XUCRED_SIZE


def test_xucred_gid_is_groups_zero_not_ngroups():
    """gid 取自 ``cr_groups[0]``，不是 ``cr_ngroups``。

    构造上刻意让两者**不相等**（组个数 20、主组 999）：按旧读法解出来是 20，
    按正确读法是 999 —— 只要哪天又退回「第三段当 gid」，这条立刻红。
    """
    parsed = S.parse_xucred(_xucred(uid=501, gid=999, ngroups=20))
    assert parsed is not None
    uid, gid, ngroups = parsed
    assert uid == 501
    assert ngroups == 20
    assert gid == 999, "gid 又读成 cr_ngroups 了（偏移 8 而不是 12）"
    assert gid != 20


def test_xucred_without_groups_reports_gid_as_unknown():
    """``cr_ngroups == 0`` 时 ``cr_groups[0]`` 没有意义 ⇒ 记 ``-1``（未知），不记 0。

    记 0 会把「不知道」说成「root 组」，而 0 是个有含义的值。
    """
    assert S.parse_xucred(_xucred(uid=501, gid=20, ngroups=0)) == (501, -1, 0)


def test_xucred_shorter_than_groups_offset_is_rejected():
    """解不完整就返回 ``None``（**不猜**）—— 调用方按「未知身份」处置。"""
    assert S.parse_xucred(_xucred()[:8]) is None
    assert S.parse_xucred(_xucred()[:12]) is None
    assert S.parse_xucred(b"") is None


# ──────────────────────────────────────────────
# 3. 出站队列：**写不完 ≠ 发送失败**（与缓冲多大无关）
#
# 这组用例刻意**不指定地址族**（用 ``socket.socketpair()`` 的默认族）：测的是
# 「写不完怎么办」，与族无关；而本机（Windows）的 Python 没有 ``socket.AF_UNIX``，
# 一旦指定，整组一行都跑不了、自证就落空了。上面那组需要 AF_UNIX 的照样只在 macOS 跑。
# ──────────────────────────────────────────────

def _sockpair():
    """一对非阻塞流式套接字。"""
    a, b = socket.socketpair()
    a.setblocking(False)
    b.setblocking(False)
    return a, b


def _fill(sock, need=1, chunk=65536):
    """把 ``sock`` 灌到「**连 ``need`` 字节都发不进去**」为止，返回灌进去的字节数。

    灌完就返回是不够的：TCP 的发送窗口会小开小合，``send`` 抛一次
    ``BlockingIOError`` 之后往往还能再挤进几百字节（本机实测）。拿这个「看着满了」
    当前提，用例就会随跑法／相邻用例而红绿摇摆。故每次碰到 ``EWOULDBLOCK`` 都用
    ``need`` 复验一遍，真塞不进才算满。

    灌进去的是填充垃圾（对端读得到），故调用方要按返回的字节数先跳过去。
    """
    total = 0
    for _ in range(200000):
        try:
            n = sock.send(b"x" * chunk)
        except (BlockingIOError, InterruptedError):
            # 复验：窗口可能刚开了一条小缝
            try:
                n = sock.send(b"x" * need)
            except (BlockingIOError, InterruptedError):
                return total
        if n <= 0:
            return total
        total += n
    return total


def _attached(sock):
    """造一个「已连上」的 ``HelperLink``，把套接字直接接上。

    这里只关心**发送侧**，握手走不走进来无关，故直接摆好 ``_sock`` / ``_state``
    —— 与本文件既有的 ``link._req_seq += 1`` 是同一种白盒手法。
    """
    link = H.HelperLink(client="pytest")
    link._sock = sock
    link._state = H.STATE_READY
    return link


def _drain(flush, pending, peer, skip, expect=1, limit=200000):
    """对端开始读，直到**收齐 ``expect`` 条消息**（或确实再没有数据了）。

    ``flush`` 是「再推一次出站队列」的可调用（app 侧是 ``link.flush_out``，守护侧是
    一次 ``serve(0)``），``pending`` 读「还剩多少字节没写出去」。

    终止条件刻意是「收齐了」而不是「队列空了」：队列空只说明字节都**写进了套接字**，
    对端的接收缓冲里可能还压着一大截没被读走（填充字节是 MB 量级）。

    ``skip`` 是 ``_fill`` 灌进去的填充字节数（先跳过再分帧）。
    """
    reader = P.FrameReader()
    msgs = []
    seen = 0
    idle = 0
    for _ in range(limit):
        try:
            data = peer.recv(65536)
        except (BlockingIOError, InterruptedError):
            data = b""
        if data:
            idle = 0
            if seen < skip:
                cut = min(skip - seen, len(data))
                seen += cut
                data = data[cut:]
            if data:
                msgs.extend(reader.feed(data))
        else:
            idle += 1
        flush()
        # 收齐**且**队列排空才算完：只收齐就停的话，队列里的字节可能还压在套接字里
        # 没被读走，后面「pending == 0」的断言就成了判时机。
        if len(msgs) >= expect and pending() == 0:
            break
        if pending() == 0 and idle >= 2:
            break
    return msgs


def _full(sock, frame):
    """灌满 ``sock``，保证 ``frame`` 这一帧**发不进去**；返回要先跳过的填充字节数。

    用「这一帧的实际字节数」当复验尺度，不是拍一个数：帧大一点，能容忍的窗口抖动
    就大一点，用例越稳。
    """
    return _fill(sock, need=len(P.encode(frame)))


def test_premise_a_full_buffer_makes_sendall_raise():
    """前提断言：非阻塞套接字上 ``sendall`` **不等待**，缓冲满即抛。

    ``FrameWriter`` 存在的全部理由就是这一条。哪天它不成立了（例如套接字被改回阻塞
    套接字），本用例会红 —— 而下面两条的论证前提也就没了，该回来重判一次。
    """
    a, b = _sockpair()
    try:
        _fill(a, need=65536)
        with pytest.raises(BlockingIOError):
            a.sendall(b"z" * 65536)
    finally:
        a.close()
        b.close()


def test_frame_writer_counts_only_unsent_bytes_and_keeps_the_stream_exact():
    """``FrameWriter``：分次写之后接着发，字节流与逐帧编码**逐字节相同**。

    两条容易写错的口径也在这里钉住：
    * 积压上限按**未发字节**算（不是 ``len(_buf)``）—— 已写出去的那截不该再占配额；
    * 重试时先把已发出的前缀丢掉，队列内存严格受上限约束。
    """
    class _Slow:
        """一次只肯收 ``budget`` 字节的假套接字（额度用光即「缓冲满」）。"""

        def __init__(self, budget):
            self.budget = budget
            self.got = bytearray()

        def send(self, data):
            take = min(self.budget, len(data))
            if take <= 0:
                raise BlockingIOError(11, "would block")
            self.got.extend(bytes(data[:take]))
            self.budget -= take
            return take

    first = P.event_push("hosts", "甲" * 64)
    second = P.event_push("hosts", "乙" * 64)
    encoded = P.encode(first) + P.encode(second)

    w = P.FrameWriter()
    w.queue(first)
    w.queue(second)
    assert w.pending == len(encoded)

    sock = _Slow(100)                       # 一次只收 100 字节 ⇒ 必然分次
    assert w.flush(sock) is False
    assert w.pending == len(encoded) - 100, "已写出去的字节要从待发里扣掉"
    assert len(w._buf) == len(encoded), "此刻还背着已写出的前缀（入口处才丢）"

    sock.budget = 0                         # 一个字节也不肯收
    assert w.flush(sock) is False           # 入口丢前缀 ⇒ 只剩未发的
    assert len(w._buf) == w.pending, "重试时不该继续背着已经写出去的字节"

    sock.budget = len(encoded)              # 对端腾空，收尾
    assert w.flush(sock) is True
    assert w.pending == 0
    assert bytes(sock.got) == encoded, "对端收到的字节流必须与逐帧编码完全相同"


def test_outbox_queues_instead_of_failing_when_the_peer_is_not_reading():
    """缓冲满时**不许**判成发送失败 —— 字节攒起来等对端来读。

    这是本次改动的承重断言。改前 ``_send`` 是 ``sock.sendall(...)`` 包在
    ``except OSError ⇒ _teardown``：非阻塞套接字缓冲一满，``sendall`` 抛
    ``BlockingIOError``（``OSError`` 的子类）⇒ 一条正常连接被当成「对端坏了」拆掉。
    而真相往往只是「守护是单线程，正跑一轮任务、此刻没在读」。
    """
    a, b = _sockpair()
    try:
        link = _attached(a)
        frame = P.event_push("hosts", "y" * 4096)
        _full(a, frame)                                 # 对端不读 ⇒ 这一帧写不进去
        assert link._send(frame) is True
        assert link.connected is True, "写不完 ≠ 对端坏了"
        assert link._out.pending > 0, "写不出去的字节应当留在出站队列里"
        assert link._want_write is True, "应当登记「等可写」"
    finally:
        a.close()
        b.close()


def test_queued_bytes_arrive_whole_once_the_peer_reads():
    """对端开始读之后队列排空，且帧是**完整的**（不是半截）。"""
    a, b = _sockpair()
    try:
        link = _attached(a)
        payload = "y" * 4096
        frame = P.event_push("hosts", payload)
        skip = _full(a, frame)
        assert link._send(frame) is True
        assert link._out.pending > 0

        msgs = _drain(link.flush_out, lambda: link._out.pending, b, skip)
        assert link._out.pending == 0
        assert link._want_write is False, "排空后应当撤掉「等可写」"
        assert len(msgs) == 1
        assert msgs[0]["subject"] == "hosts"
        assert msgs[0]["observed"] == payload
    finally:
        a.close()
        b.close()


def test_write_interest_is_reported_only_when_it_changes():
    """「等可写」只在**变化**时上报。

    装配层拿它开关写方向的 ``QSocketNotifier``，而那只许在主线程做。每次都上报，
    等于把「积压 / 排空」这一刻的事变成「每次发送」都往主线程塞活。
    """
    a, b = _sockpair()
    try:
        link = _attached(a)
        calls = []
        link.on_writable = calls.append
        first = P.event_push("hosts", "a" * 4096)
        second = P.event_push("hosts", "b" * 4096)
        skip = _full(a, first)
        link._send(first)                               # 写不完 ⇒ 报 True
        link._send(second)                              # 仍写不完 ⇒ 不该再报
        assert calls == [True]

        _drain(link.flush_out, lambda: link._out.pending, b, skip, expect=2)
        assert calls == [True, False]
    finally:
        a.close()
        b.close()


def test_outbox_overflow_drops_the_connection(monkeypatch):
    """积压超过上限 ⇒ 判「对端长时间不读」并断开（**不是无界缓冲**）。

    这一条**不去灌真缓冲**，而是把 ``_flush`` 顶成「一个字节也发不出去」：要验的是
    「上限到了之后怎么处置」，而「缓冲真会满」由
    ``test_premise_a_full_buffer_makes_sendall_raise`` 用真套接字证明。两件事混在一条
    里会随内核窗口小开小合而摇摆 —— 2026-09-30 实测：全量里红（整帧竟全发出去了、
    ``pending`` 为 0）、隔离跑却绿。**判据不能挂在本机此刻的缓冲余量上**。
    """
    monkeypatch.setattr(P, "MAX_OUTBOX", 12000)
    a, b = _sockpair()
    try:
        link = _attached(a)
        monkeypatch.setattr(link, "_flush", lambda: True)   # 对端一个字节都没读
        frame = P.event_push("hosts", "y" * 8192)           # 编出来约 8.2 KB
        assert link._send(frame) is True
        assert link._out.pending > 0
        assert link._send(frame) is False                   # 第二帧顶到上限
        assert link.connected is False
        assert link._out.pending == 0, "断开时队列要清空"
        assert link._want_write is False
    finally:
        a.close()
        b.close()


def test_server_send_queues_and_arms_write_instead_of_dropping():
    """守护侧同款：写不完**不丢连接**，并把该连接的事件扩成「读 ＋ 写」。"""
    a, b = _sockpair()
    try:
        # 身份与本次断言无关（这里不做校验）；不写 ``os.getuid()`` —— 它是 POSIX 专有，
        # Windows 上没有，而本用例要能在 Windows 上跑。
        conn = S.Connection(a, S.PeerInfo(uid=0, gid=0))
        srv = S.SessionServer("unused", handler=lambda c, m: None)
        srv._selector = selectors.DefaultSelector()
        srv._selector.register(a, selectors.EVENT_READ, ("conn", conn))
        try:
            payload = "y" * 4096
            frame = P.event_push("hosts", payload)
            skip = _full(a, frame)
            assert srv.send(conn, frame) is True
            assert conn.alive is True, "写不完 ≠ 客户端坏了"
            assert conn.out.pending > 0
            assert conn.want_write is True, "应当把读事件扩成「读 ＋ 写」"

            msgs = _drain(lambda: srv.serve(0), lambda: conn.out.pending, b, skip)
            assert conn.out.pending == 0
            assert conn.want_write is False, "排空后应当收回只有读"
            assert len(msgs) == 1 and msgs[0]["observed"] == payload
        finally:
            srv._selector.close()
            srv._selector = None
    finally:
        a.close()
        b.close()


def test_widen_send_buffer_is_best_effort_and_never_raises():
    """「调大发送缓冲」是**优化**：设不上去不是错误（分次写兜住送达）。

    故它不许抛，也不许有人拿它当正确性前提来检查 —— 这条断言就是「不许抛」本身。
    """
    class _NoOpts:
        """一个 ``setsockopt`` 必失败的鸭子类型（**不能拼 ``socket.socket``**：
        CPython 的 socket 实例不允许覆盖 ``setsockopt``，写上去直接 AttributeError）。"""

        def setsockopt(self, *_args, **_kwargs):
            raise OSError("内核不支持")

    P.widen_send_buffer(_NoOpts())              # 不抛即通过

    # 「目标值」与「单帧上限」的关系读常量，不抄数字（否则改上限时这里是下一个漂移点）。
    assert P.SEND_BUFFER_BYTES == 2 * P.MAX_FRAME

    a, b = _sockpair()
    try:
        P.widen_send_buffer(a)                  # 真套接字上也不抛
    finally:
        a.close()
        b.close()


# ──────────────────────────────────────────────
# 5. 连接的回收期限（「说了话但没说完」才回收）
# ──────────────────────────────────────────────

def _server_with_conn(sock):
    """一只只挂了这条连接的服务端（不 listen、不 bind —— 这里只测回收）。

    ``deadline`` 在产品里由 ``_admit`` 于接纳那一刻排上；Windows 上没有 ``AF_UNIX``
    （走不到接纳），故这里按同一语义补上。
    """
    conn = S.Connection(sock, S.PeerInfo(uid=0, gid=0))
    conn.deadline = time.monotonic() + S.HELLO_TIMEOUT_S
    srv = S.SessionServer("unused", handler=lambda c, m: None)
    srv._conns.append(conn)          # 接纳时做的另一件事（回收只扫 ``_conns``）
    srv._selector = selectors.DefaultSelector()
    srv._selector.register(sock, selectors.EVENT_READ, ("conn", conn))
    return srv, conn


def test_connection_that_never_speaks_is_reaped():
    """连上了却**一直不发帧**的连接要被回收（占着 fd 与一个 ``Connection``）。

    ``IDLE_READ_TIMEOUT_S`` 从前是**死常量**（定义了、导出、docstring 说它守「半条
    帧卡住」，全仓零处使用）。接上之后这条才真的有人执行。
    """
    a, b = _sockpair()
    try:
        srv, conn = _server_with_conn(a)
        try:
            assert conn.deadline is not None, "接纳即排上「等握手」的期限"
            conn.deadline = time.monotonic() - 0.01      # 期限已过
            srv.serve(0)
            assert conn.alive is False, "到期的连接要被回收"
        finally:
            srv._selector.close()
            srv._selector = None
    finally:
        a.close()
        b.close()


def test_idle_connection_is_not_reaped():
    """**正常空闲不许被拆**：订阅型客户端几小时不讲话是对的。

    「超时」守的是「说了话但没说完」（没握手 / 半条帧），不是「闲」。用闲置时长去
    拆连接会把「订阅着、等着推送」这个正当用法一起拆掉。
    """
    a, b = _sockpair()
    try:
        srv, conn = _server_with_conn(a)
        try:
            conn.client = "app"                 # 握手过了
            srv._refresh_deadline(conn)
            assert conn.deadline is None, "握完手且没有半条帧 ⇒ 不设期限"
            srv.serve(0)
            assert conn.alive is True
        finally:
            srv._selector.close()
            srv._selector = None
    finally:
        a.close()
        b.close()


def test_half_frame_arms_the_idle_deadline():
    """手里卡着半条帧 ⇒ 排上期限（对端发了半个长度前缀就不动了）。"""
    a, b = _sockpair()
    try:
        srv, conn = _server_with_conn(a)
        try:
            conn.client = "app"
            conn.reader.feed(P.encode(P.get("route", "r1"))[:-3])   # 少 3 个字节
            assert conn.reader.pending() > 0
            srv._refresh_deadline(conn)
            assert conn.deadline is not None
            assert conn.deadline > time.monotonic(), "期限要在未来（不是立刻到期）"
        finally:
            srv._selector.close()
            srv._selector = None
    finally:
        a.close()
        b.close()
