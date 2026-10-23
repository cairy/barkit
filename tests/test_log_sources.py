"""shell/log_sources：日志分类表（有哪几份、各在哪）。

设置窗「日志」卡按这张表画按钮，故**表就是判据**：分类漏一项、落点指错，用户点下去
得到的是「没东西」——本仓最贵的一类故障（看起来功能在、实际什么也没发生）。
"""

from __future__ import annotations

from barkit import paths
from barkit.capabilities.helper import safeio
from barkit.core import logging_setup
from barkit.shell import log_sources as mod
from barkit.shell.log_sources import log_sources


def test_classification_is_stable_and_labelled():
    """四类各有稳定的 ``key`` 与非空 ``label``（key 给诊断读，label 是按钮上的字）。"""
    sources = log_sources()
    assert [s.key for s in sources] == ["app", "helper", "tunnel", "crash"]
    assert all(s.label and s.label == s.label.strip() for s in sources)
    assert len({s.key for s in sources}) == len(sources)


def test_app_and_helper_paths_come_from_the_shared_constants():
    """应用 / 守护两份的落点必须**取自常量**，不许在表里另写一遍字面量。"""
    by_key = {s.key: s.target() for s in log_sources()}
    assert by_key["app"] == paths.LOG_DIR / logging_setup.LOG_FILENAME
    assert by_key["helper"] == paths.HELPER_LOG


def test_tunnel_path_is_the_helper_side_single_source():
    """隧道日志问 ``safeio._hev_log_path()`` —— app 侧另写一份字面量就是第二份定义。"""
    by_key = {s.key: s.target() for s in log_sources()}
    assert by_key["tunnel"] == safeio._hev_log_path()


def test_paths_are_read_at_call_time(monkeypatch, tmp_path):
    """路径**调用期**读：按值冻住会让 ``PATH_REDIRECTS`` 的逐用例重定向静默失效。

    判据取法：改掉 ``barkit.paths`` 的模块属性，再看**已经建好的那批取数口**给出的
    结果跟着变不变。表在 patch 之前取（生产里表也是启动期建一次、路径现问）。
    """
    sources = log_sources()
    by_key = {s.key: s for s in sources}
    monkeypatch.setattr(paths, "LOG_DIR", tmp_path)
    monkeypatch.setattr(paths, "HELPER_LOG", tmp_path / "helper.log")
    assert by_key["app"].target() == tmp_path / logging_setup.LOG_FILENAME
    assert by_key["helper"].target() == tmp_path / "helper.log"


def test_crash_report_prefers_the_newest_and_falls_back_to_the_dir(monkeypatch, tmp_path):
    """崩溃报告：有就选中**最新一份**，一份都没有就落在目录上。

    同目录还躺着**别的进程**的报告（以及 ``Retired/`` 里的历史归档）⇒ 直接把目录打开
    等于让用户自己在一堆文件名里挑。
    """
    monkeypatch.setattr(mod, "CRASH_REPORTS_DIR", tmp_path)
    assert mod.latest_crash_report() == tmp_path, "一份都没有 ⇒ 目录本身"
    (tmp_path / "Python-2026-01-01-000000.ips").write_text("旧", encoding="utf-8")
    (tmp_path / "别的进程-2026-03-01.ips").write_text("不关我们的事", encoding="utf-8")
    newest = tmp_path / "Python-2026-02-09-101112.ips"
    newest.write_text("新", encoding="utf-8")
    assert mod.latest_crash_report() == newest
