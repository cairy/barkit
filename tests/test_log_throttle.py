"""core/log_throttle：同因日志只报一次（首条永远落盘，重复的合并计数）。

**为什么不删掉那些日志而是节流**：同一条信息第一次出现必须落盘 —— 排查全靠它。
要去掉的是同一个原因的第 2 条到第 N 条（周期型服务的 ``poll()`` 按 3s 刷 traceback、
Qt 探测函数每拍一条 warning）。窗口过后补一句「已合并 N 次」，「这个问题一直在」
与「持续了多久」两条信息都保住。
"""

from __future__ import annotations

import logging
import time

import pytest

from barkit.core import log_throttle
from barkit.core.log_throttle import log_throttled

LOG = logging.getLogger("barkit.test.throttle")


@pytest.fixture(autouse=True)
def _clean_memo():
    """合并账是模块级状态，用例之间必须归零（残留会让下一条本该落盘的静默）。"""
    log_throttle.reset_throttle()
    yield
    log_throttle.reset_throttle()


def test_same_message_is_merged(caplog):
    with caplog.at_level(logging.INFO):
        for _ in range(5):
            log_throttled(LOG, logging.WARNING, "同一条：%s", "守护未连接")
    assert len(caplog.records) == 1
    assert caplog.records[0].getMessage() == "同一条：守护未连接"


def test_a_different_cause_is_new_information(caplog):
    """原因变了就是新信息（「超时」与「权限不足」的处置完全不同）⇒ 必须再出声。

    判据按**渲染后的整句话**：这样调用点不必自己维护状态，也不会把两种病合并成一条。
    """
    with caplog.at_level(logging.INFO):
        log_throttled(LOG, logging.WARNING, "失败：%s", "超时")
        log_throttled(LOG, logging.WARNING, "失败：%s", "权限不足")
    assert [r.getMessage() for r in caplog.records] == ["失败：超时", "失败：权限不足"]


def test_explicit_key_merges_across_wording(caplog):
    """文案里带每次都变的计数时，靠显式 ``key`` 把它按同一件事合并。"""
    with caplog.at_level(logging.INFO):
        for n in (1, 2, 3):
            log_throttled(LOG, logging.INFO, "第 %d 轮没送到", n, key="not-delivered")
    assert len(caplog.records) == 1


def test_window_expiry_appends_the_merged_count(caplog, monkeypatch):
    """窗口过后补一条汇总：**不是把「重复了多少次」也吞掉**。

    窗口缩到 50ms、真实睡 150ms —— 3 倍余量，不用把时钟做成生产代码里的测试钩子。
    """
    monkeypatch.setattr(log_throttle, "DEFAULT_WINDOW_S", 0.05)
    with caplog.at_level(logging.INFO):
        log_throttled(LOG, logging.WARNING, "一直在失败")
        log_throttled(LOG, logging.WARNING, "一直在失败")     # 窗口内：静默 ×1
        log_throttled(LOG, logging.WARNING, "一直在失败")     # 窗口内：静默 ×2
        time.sleep(0.15)
        log_throttled(LOG, logging.WARNING, "一直在失败")     # 窗口已过：补汇总
    assert len(caplog.records) == 2
    assert "已合并 2 次" in caplog.records[1].getMessage()


def test_zero_window_never_merges(caplog):
    """``window<=0`` ＝ 不节流（显式关掉的口，与 helper 侧 ``every_s`` 同义）。"""
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            log_throttled(LOG, logging.INFO, "每条都要", window=0)
    assert len(caplog.records) == 3


def test_first_record_carries_the_traceback(caplog):
    """首条带完整堆栈，被合并掉的那些不带 —— 堆栈只在最有用的那一拍写一次。"""
    with caplog.at_level(logging.INFO):
        try:
            raise ValueError("boom")
        except ValueError:
            for _ in range(3):
                log_throttled(LOG, logging.ERROR, "poll 异常", exc_info=True)
    assert len(caplog.records) == 1
    assert caplog.records[0].exc_info is not None


def test_reset_makes_the_next_one_speak_again(caplog):
    with caplog.at_level(logging.INFO):
        log_throttled(LOG, logging.INFO, "同一句")
        log_throttle.reset_throttle()
        log_throttled(LOG, logging.INFO, "同一句")
    assert len(caplog.records) == 2


def test_broken_formatting_still_writes_something(caplog):
    """格式化失败不该让「记日志」这件事本身炸掉 —— 宁可文案难看，不可丢一条。"""
    with caplog.at_level(logging.INFO):
        log_throttled(LOG, logging.INFO, "带 %s 和 %s", "只有一个参数")
    assert len(caplog.records) == 1
    assert "带 %s 和 %s" in caplog.records[0].getMessage()


def test_the_memo_table_is_bounded(caplog):
    """文本各异的失败堆太多时整表丢弃 —— 记忆表不许跟着故障时长一起长。"""
    with caplog.at_level(logging.INFO):
        for i in range(log_throttle._MAX_KEYS + 50):
            log_throttled(LOG, logging.INFO, "失败 %d", i)
    assert len(log_throttle._seen) <= log_throttle._MAX_KEYS
