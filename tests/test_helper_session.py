"""``shell/helper_session`` 装配层的离屏自证：**生命周期**与**按需定时器**。

只测「装配层自己接的线对不对」，不测通道的行格式与状态机（那些在 ``test_session.py``）：

1. ``stop()`` 之后不许还有任何东西把会话拉起来；
2. 等握手的期限要真排上定时器，离开握手时要撤掉；到点要真的断开。

两条都各对应一条「**看不见**」的故障：前者是「关掉了却又在悄悄重连」（今天被退出
路径掩盖），后者是「连着但永远不可用，界面上表现为一直没连上」。
"""

from __future__ import annotations

import time

import pytest

from PySide6.QtCore import QCoreApplication

from barkit.capabilities import helper_link as H
from barkit.core.events import EventBus
from barkit.shell.helper_session import HelperSession


@pytest.fixture(scope="module")
def qapp():
    """定时器与 notifier 要一个活着的事件循环；没有就建一个（幂等）。"""
    return QCoreApplication.instance() or QCoreApplication([])


def _session() -> HelperSession:
    # 路径故意指向不存在的地方：连不上是本组的常态，重连定时器本来就该在跑。
    return HelperSession(EventBus(), socket_path="/tmp/barkit-not-there.sock")


@pytest.fixture
def cannot_connect(monkeypatch):
    """把「连不上」给定下来。

    Windows 没有 ``AF_UNIX``（真连接只可能在 macOS 上发生），而本组测的是**装配层
    的定时器接线**——「连接失败 ⇒ 起重连」这个结果直接钉住即可，真套接字那部分在
    ``test_session.py`` 里。
    """
    monkeypatch.setattr(H.HelperLink, "connect", lambda self, path: False)


# ──────────────────────────────────────────────
# 1. stop() 是总闸
# ──────────────────────────────────────────────

def test_stop_leaves_no_reconnect_timer_running(qapp, cannot_connect):
    """``stop()`` 之后**不许**还有定时器在试连。

    漏掉判据时这里是一条回路：``stop()`` → ``link.close()`` → ``on_state(OFF)`` →
    无条件 ``_retry.start()``。「关掉会话」于是只关了一瞬间，随后继续每秒试连。
    """
    s = _session()
    s.start()
    assert s._retry.isActive(), "连不上 ⇒ 重连本来就该在跑（本用例的前提）"

    s.link._set_state(H.STATE_READY)      # 假装连上了（重连随之停下）
    assert not s._retry.isActive()

    s.stop()
    assert not s._started
    assert not s._retry.isActive(), "stop() 之后不许再被拉起来"
    assert not s._hs_timer.isActive()


def test_stop_is_idempotent_and_does_not_resurrect(qapp, cannot_connect):
    """连着的时候 ``stop()``：关连接触发的 OFF **不许**重启重连。"""
    s = _session()
    s.start()
    s.link._set_state(H.STATE_READY)
    s.stop()
    s.stop()
    assert not s._retry.isActive()


# ──────────────────────────────────────────────
# 2. 握手期限
# ──────────────────────────────────────────────

def test_handshake_deadline_arms_a_timer_and_disarms_on_ready(qapp):
    """进入 handshake 要排上期限；握手成功要撤掉（**按需**，平时一只定时器都不跑）。"""
    s = _session()
    link = s.link
    assert not s._hs_timer.isActive()

    link._set_state(H.STATE_HANDSHAKE)
    link._handshake_deadline = time.monotonic() + H.HANDSHAKE_TIMEOUT_S
    link._notify_handshake()
    assert s._hs_timer.isActive(), "等 welcome 要有期限"

    link._set_state(H.STATE_READY)        # 握手成功 ⇒ 期限清理只写在 _set_state 里
    assert link._handshake_deadline is None
    assert not s._hs_timer.isActive(), "就绪之后不该还留着一只定时器"


def test_expired_handshake_tears_the_connection_down(qapp):
    """到点仍没就绪 ⇒ 断开（此后走正常重连）。**不永久僵在 handshake**。

    守护 accept 了却不回 ``welcome``（假死 / 被 SIGSTOP / 正卡在一轮长任务里）时，
    既不会有 EOF（``pump()`` 不醒）也不会有回帧 —— 只有这个期限能把它拉出来。
    """
    s = _session()
    link = s.link
    link._set_state(H.STATE_HANDSHAKE)
    link._handshake_deadline = time.monotonic() - 0.01
    link._notify_handshake()

    link.expire_handshake()

    assert link.state == H.STATE_OFF
    assert link._handshake_deadline is None, "断开即撤期限"
    assert not s._hs_timer.isActive()


def test_handshake_deadline_that_has_not_come_does_nothing(qapp):
    """没到点就别动 —— 别把正常握手（本地微秒级，但守护可能正忙）当超时。"""
    s = _session()
    link = s.link
    link._set_state(H.STATE_HANDSHAKE)
    link._handshake_deadline = time.monotonic() + 60.0

    link.expire_handshake()

    assert link.state == H.STATE_HANDSHAKE
