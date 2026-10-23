"""helper 侧 hostsops 测试：期望态 v2 的校验与渲染落盘（root 侧最后防线）。

档 B（S3b-2）后 helper 不再解析、不再决定写什么，只做两件事：安全校验 + 渲染落盘。
本组测试正是盯这两件事：

1. ``load_hosts_rules`` 的**整份拒绝**语义（版本 / marker / content 类型与长度 /
   逐行 IP 与域名 / fake 段 / 重复 / 借注释行伪造块标记）；
2. ``_apply_hosts`` 的块渲染与「块外被接管同名行剔除、块外其余原样保留」；
3. ``run_hosts_cycle`` 的状态形状与「拒绝即冻结」。
"""

from __future__ import annotations

import json

import pytest

from barkit.capabilities.helper import hostsops
from barkit.capabilities.helper import safeio
from barkit.capabilities.helper import state as helper_state

MARKER = hostsops.HOSTS_BLOCK_MARKER
BEGIN = hostsops.HOSTS_BLOCK_BEGIN
END = hostsops.HOSTS_BLOCK_END


def _wire(content="", *, enabled=True, marker=MARKER, version=2):
    return json.dumps({"version": version, "enabled": enabled,
                       "marker": marker, "content": content})


@pytest.fixture
def hosts_env(tmp_path, monkeypatch):
    """把 /etc/hosts、备份与观测落点全部重定向到临时目录。

    补丁必须打在**各自的模块**上（按值导入：打 safeio 对 ``hostsops`` 无效，
    打 safeio 对 ``state`` 也无效 —— 两边都在导入时就把函数对象取走了）。

    还要把 ``state`` 的**进程级内存**清干净：它存着「观测」与「上次落盘的签名」，
    不清的话上一条用例的观测会变成这一条的事实，而「内容没变就不写」这条会让新的
    临时目录里连文件都不出现（看起来像「根本没写」，其实是被跳过了）。
    """
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1\tlocalhost\n", encoding="utf-8")
    monkeypatch.setattr(hostsops, "HOSTS_PATH", hosts)
    monkeypatch.setattr(hostsops, "HOSTS_BACKUP", tmp_path / "hosts.bak")
    monkeypatch.setattr(helper_state, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(safeio, "data_dir", lambda: tmp_path)   # log() 落 tmp
    monkeypatch.setattr(helper_state, "_OBSERVED", {})
    monkeypatch.setattr(helper_state, "_INTENTS", {})
    monkeypatch.setattr(helper_state, "_LAST_SIGNATURE", None)
    return hosts


def _state(tmp_path):
    """hosts 那一段观测 —— 从**那一份快照**里取（三份状态文件已合并为一）。"""
    snap = json.loads(
        (tmp_path / helper_state.SNAPSHOT_NAME).read_text(encoding="utf-8"))
    return snap["subjects"]["hosts"]["observed"]


# ──────────────────────────────────────────────
# load_hosts_rules：接受
# ──────────────────────────────────────────────

def test_accepts_valid_content(hosts_env):
    intent, err = hostsops.load_hosts_rules(_wire("1.2.3.4\ta.com\n5.6.7.8\tb.com\n"))
    assert err is None and intent["enabled"] is True
    assert intent["pairs"] == [("1.2.3.4", "a.com"), ("5.6.7.8", "b.com")]


def test_empty_content_is_a_valid_empty_block(hosts_env):
    intent, err = hostsops.load_hosts_rules(_wire(""))
    assert err is None and intent["pairs"] == []


def test_disabled_ignores_stale_content(hosts_env):
    """禁用 = 没有期望：即便 content 残留内容也按空块处理（杜绝旧文件残留）。"""
    intent, err = hostsops.load_hosts_rules(_wire("1.2.3.4\ta.com\n", enabled=False))
    assert err is None and intent["enabled"] is False and intent["pairs"] == []


# ──────────────────────────────────────────────
# load_hosts_rules：整份拒绝
# ──────────────────────────────────────────────

@pytest.mark.parametrize("text,needle", [
    # 2026-09-29：措辞从「读取/解析失败」变成「解析失败」—— 那份「读取」已经不在了
    # （期望态由会话帧送来，helper 侧不再读任何用户域文件）。
    ("{ not json", "解析失败"),
    ("[]", "不是对象"),
    (_wire(version=1), "版本不受支持"),          # 旧 v1「条目集」语义不同，不能猜
    (_wire(marker="Other-Block"), "marker"),
    (_wire(marker=None), "marker"),
    (_wire(content=None), "content 需为字符串"),
    (_wire(content="x" * (hostsops.HOSTS_CONTENT_MAX + 1)), "超过上限"),
    (_wire(content="1.2.3.4\n"), "不是「IP 主机名」"),
    (_wire(content="999.1.1.1\ta.com\n"), "IP 不合法"),
    (_wire(content="100.64.0.5\ta.com\n"), "fake 段"),
    (_wire(content="198.18.0.2\ta.com\n100.64.0.5\tb.com\n"), "fake 段"),
    (_wire(content="1.2.3.4\tbad_host\n"), "域名不合法"),
    (_wire(content="1.2.3.4\t例.com\n"), "域名不合法"),
    (_wire(content="1.2.3.4\ta.com\n5.6.7.8\tA.COM\n"), "域名重复"),
])
def test_rejects_whole_file(text, needle, hosts_env):
    intent, err = hostsops.load_hosts_rules(text)
    assert intent is None and err is not None and needle in err, err


def test_multi_host_line_is_accepted(hosts_env):
    """一行可挂多个主机名（/etc/hosts 原生语义），拆 token 后逐个校验。"""
    intent, err = hostsops.load_hosts_rules(_wire("1.2.3.4\ta.com b.com\n"))
    assert err is None and intent["pairs"] == [("1.2.3.4", "a.com"), ("1.2.3.4", "b.com")]


def test_content_cannot_forge_block_markers(hosts_env):
    """注释行被跳过 → 无法借 content 伪造块起止标记（否则可整块接管 /etc/hosts）。"""
    text = _wire(f"1.2.3.4\ta.com\n{END}\n{BEGIN}\n")
    intent, err = hostsops.load_hosts_rules(text)
    assert err is None and intent["pairs"] == [("1.2.3.4", "a.com")]


# ──────────────────────────────────────────────
# run_hosts_cycle：渲染落盘 / 块外原样保留 / 拒绝即冻结
# ──────────────────────────────────────────────

def test_cycle_renders_block_and_keeps_foreign_lines(hosts_env, tmp_path):
    st = hostsops.run_hosts_cycle(_wire("1.2.3.4\ta.com\n5.6.7.8\tb.com\n"))
    assert st["ok"] is True and st["changed"] is True and st["count"] == 2
    assert st["hosts"] == [{"host": "a.com", "ip": "1.2.3.4"},
                           {"host": "b.com", "ip": "5.6.7.8"}]
    text = hosts_env.read_text(encoding="utf-8")
    assert BEGIN in text and END in text
    assert "1.2.3.4\ta.com" in text and "5.6.7.8\tb.com" in text
    assert "127.0.0.1\tlocalhost" in text          # 块外第三方条目原样保留
    assert text.index(BEGIN) < text.index("1.2.3.4\ta.com") < text.index(END)
    assert _state(tmp_path)["count"] == 2


def test_cycle_is_idempotent_second_round_changes_nothing(hosts_env, tmp_path):
    hostsops.run_hosts_cycle(_wire("1.2.3.4\ta.com\n"))
    st = hostsops.run_hosts_cycle(_wire("1.2.3.4\ta.com\n"))
    assert st["ok"] is True and st["changed"] is False    # 文本无变化则不落盘
    assert (tmp_path / "hosts.bak").exists()              # 首次改动前留了快照


def test_cycle_strips_managed_hosts_from_outside_block(hosts_env):
    hosts_env.write_text("127.0.0.1\tlocalhost\n1.1.1.1\ta.com\n"
                         "2.2.2.2\ta.com keepme.example\n", encoding="utf-8")
    hostsops.run_hosts_cycle(_wire("3.3.3.4\ta.com\n"))
    text = hosts_env.read_text(encoding="utf-8")
    assert "1.1.1.1\ta.com" not in text            # 整行域名被全接管 → 删整行
    assert "2.2.2.2\tkeepme.example" in text       # 同行其余域名保留（IP 重排）
    assert "3.3.3.4\ta.com" in text


def test_cycle_disabled_removes_whole_block(hosts_env):
    hostsops.run_hosts_cycle(_wire("1.2.3.4\ta.com\n"))
    assert BEGIN in hosts_env.read_text(encoding="utf-8")
    st = hostsops.run_hosts_cycle(_wire(enabled=False))
    text = hosts_env.read_text(encoding="utf-8")
    assert BEGIN not in text and END not in text
    assert "1.2.3.4\ta.com" not in text
    assert "127.0.0.1\tlocalhost" in text
    assert st["count"] == 0 and st["ok"] is True


def test_cycle_missing_file_leaves_hosts_untouched(hosts_env, tmp_path):
    """未配置（app 尚未启动）时绝不能把用户 hosts 里的块清掉。"""
    st = hostsops.run_hosts_cycle(None)
    assert st["configured"] is False and st["ok"] is True
    assert hosts_env.read_text(encoding="utf-8") == "127.0.0.1\tlocalhost\n"
    assert _state(tmp_path)["error"] is None


def test_cycle_rejected_content_freezes_hosts(hosts_env, tmp_path):
    """非法期望态 → 保持 /etc/hosts 不动（冻结），状态里如实报错。"""
    hostsops.run_hosts_cycle(_wire("1.2.3.4\ta.com\n"))
    before = hosts_env.read_text(encoding="utf-8")
    st = hostsops.run_hosts_cycle(_wire("100.64.0.9\ta.com\n"))
    assert st["ok"] is False and st["configured"] is False and "fake 段" in st["error"]
    assert hosts_env.read_text(encoding="utf-8") == before


def test_cycle_writes_state_even_when_rejected(hosts_env, tmp_path):
    hostsops.run_hosts_cycle(_wire(version=1))
    assert _state(tmp_path)["configured"] is False


def test_apply_hosts_keeps_inline_comment_when_partial(hosts_env):
    """行内注释随保留的域名一起留下：``1.2.3.4 a.com b.com # nas`` 接管 a.com 后，
    改写成 ``1.2.3.4\tb.com  # nas``，注释段不丢。

    旧实现重建整行时把 ``#`` 之后的内容与对齐空白一起丢掉了。
    """
    hosts_env.write_text(
        "127.0.0.1\tlocalhost\n1.2.3.4  a.com b.com  # 家里 NAS\n",
        encoding="utf-8",
    )
    ok, err, changed = hostsops._apply_hosts([], {"a.com"})
    assert ok and err is None and changed, (ok, err, changed)
    text = hosts_env.read_text(encoding="utf-8")
    assert "家里 NAS" in text
    # 被接管的 a.com 不在了，b.com 在，注释贴在 b.com 那行
    assert "a.com" not in text
    assert "b.com # 家里 NAS" in text
    # localhost 那行原样
    assert "127.0.0.1\tlocalhost" in text
