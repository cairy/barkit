"""autotype 测试（P2a 2.5/2.6）。

覆盖：
- 引擎纯逻辑（engine.py）：resolve_profile 合并、validate_profile 校验、
  inject 发送时序/回车/中断、ensure_english 各分支（TIS 降级与切换）
- 插件接线（plugin.py）：契约 / 上次项存取 / type_profile 全链路（配置→倒计时→注入）
- 设置页（settings.py，2026-09-19 由 config_dialog.py 收归）：输入项增删改回写 YAML
  （原子写 + chmod 600）、全局默认三项、行内报错（零模态框）
- 面板行（panel_view.py，2026-09-19 由 plugin.py 迁出）：下拉点击不得连带整行点击、
  行图标取图标库字形
"""

from __future__ import annotations

import os

import pytest

from barkit.contribution.specs import ActionOutcome
from barkit.plugins.autotype import engine
from barkit.plugins.autotype.engine import (
    inject,
    resolve_profile,
    validate_profile,
)

# ---------- resolve_profile ----------


def test_resolve_profile_full():
    cfg = {
        "delay": 4,
        "interval": 0.03,
        "press_enter": False,
        "profiles": [
            {"name": "cloudclient", "text": "abc", "press_enter": True, "delay": 6}
        ],
    }
    spec = resolve_profile(cfg, "cloudclient")
    assert spec == {
        "name": "cloudclient",
        "text": "abc",
        "delay": 6,       # profile 专属覆盖全局
        "interval": 0.03,  # 回退全局
        "press_enter": True,
        "enter_delay": 0.3,
    }


def test_resolve_profile_falls_back_to_global():
    cfg = {"delay": 4, "profiles": [{"name": "wifi", "text": "y"}]}
    spec = resolve_profile(cfg, "wifi")
    assert spec["delay"] == 4
    assert spec["press_enter"] is False


def test_resolve_profile_missing_or_empty_text():
    assert resolve_profile({"profiles": [{"name": "a", "text": ""}]}, "a") is None
    assert resolve_profile({"profiles": [{"name": "a"}]}, "a") is None
    assert resolve_profile({"profiles": []}, "nope") is None


def test_resolve_profile_delay_zero_keeps_zero():
    cfg = {"delay": 4, "profiles": [{"name": "a", "text": "x", "delay": 0}]}
    spec = resolve_profile(cfg, "a")
    assert spec["delay"] == 0  # 显式 0 不被 `or` 回退丢弃


def test_resolve_profile_interval_zero_keeps_zero():
    """``interval`` 显式 0（逐字符不等待）同样是合法值，不得被 falsy 回退成全局 0.03。

    与上面那条 ``delay`` 是同一类陷阱的两处 —— 2026-09-19 把 ``interval`` 接上设置页
    时一并收掉：接上 UI 之后用户能明确表达"不要等待"，再悄悄改成 30 ms 就成了界面撒谎。
    """
    cfg = {"interval": 0.03, "profiles": [{"name": "a", "text": "x", "interval": 0}]}
    assert resolve_profile(cfg, "a")["interval"] == 0
    # 全局写 0、profile 没写 ⇒ 也取 0
    cfg2 = {"interval": 0, "profiles": [{"name": "a", "text": "x"}]}
    assert resolve_profile(cfg2, "a")["interval"] == 0
    # 没写任何一处 ⇒ 出厂默认
    assert resolve_profile({"profiles": [{"name": "a", "text": "x"}]}, "a")["interval"] == 0.03


# ---------- validate_profile ----------


def test_validate_profile():
    assert validate_profile("", "x", None) == "名称不能为空"
    assert validate_profile("a", "", None) == "输入文本不能为空"
    assert validate_profile("a", "x", -1) is not None
    assert validate_profile("cloudclient", "pw", 3) is None


# ---------- inject（monkeypatch 不真发键） ----------


@pytest.fixture()
def poster(monkeypatch):
    """替换发送/睡眠原语，记录调用。"""
    sent_chars: list[str] = []
    sent_keys: list[int] = []

    def _pc(ch, interval: float):
        sent_chars.append(ch)

    def _pk(code, interval: float):
        sent_keys.append(code)

    monkeypatch.setattr(engine, "_post_char", _pc)
    monkeypatch.setattr(engine, "_post_keycode", _pk)
    monkeypatch.setattr(engine, "time_sleep", lambda s: None)
    return sent_chars, sent_keys


def test_inject_plain_text(poster):
    sent_chars, sent_keys = poster
    n = inject("hello", interval=0.0)
    assert n == 5
    assert sent_chars == list("hello")
    assert sent_keys == []


def test_inject_with_enter(poster):
    sent_chars, sent_keys = poster
    n = inject("ab", interval=0.0, press_enter=True, enter_delay=0.1)
    assert n == 2
    assert sent_keys == [engine.RETURN]


def test_inject_unicode_multibyte(poster):
    sent_chars, _ = poster
    n = inject("密码123", interval=0.0)
    assert n == 5
    assert sent_chars == ["密", "码", "1", "2", "3"]


def test_inject_per_char_callback(poster):
    sent_chars, _ = poster
    progress: list[int] = []

    def cb(sent: int, ch: str):
        progress.append(sent)

    inject("abc", interval=0.0, per_char=cb)
    assert progress == [1, 2, 3]
    assert sent_chars == list("abc")


def test_inject_stop_interrupts(poster):
    sent_chars, sent_keys = poster
    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] >= 3  # 发完 2 个字符后停

    n = inject("abcdef", interval=0.0, press_enter=True, should_stop=should_stop)
    assert n == 2
    assert sent_chars == ["a", "b"]
    assert sent_keys == []  # 中断后不回车


# ---------- ensure_english ----------


class FakeTIS:
    """可脚本化的 TIS 替身。"""

    def __init__(self, available=True, sid="com.apple.keylayout.ABC", select_ok=True):
        self._avail = available
        self._sid = sid
        self._select_ok = select_ok
        self.select_calls = 0

    @property
    def available(self):
        return self._avail

    def current(self):
        if not self._avail:
            return None, False
        return self._sid, self._sid.startswith("com.apple.keylayout.")

    def select(self, target_id: str) -> bool:
        self.select_calls += 1
        return self._select_ok


def test_ensure_english_already_layout(monkeypatch):
    monkeypatch.setattr(engine, "TIS", FakeTIS(sid="com.apple.keylayout.ABC"))
    ok, msg = engine.ensure_english(auto_switch=True, require=True)
    assert ok is True
    assert "英文布局" in msg


def test_ensure_english_auto_switch_success(monkeypatch):
    class Flip(FakeTIS):
        def select(self, target_id: str) -> bool:
            self.select_calls += 1
            self._sid = "com.apple.keylayout.ABC"
            return True

    monkeypatch.setattr(engine, "TIS", Flip(sid="com.apple.inputmethod.SCIM.ITABC"))
    ok, msg = engine.ensure_english(auto_switch=True, require=True)
    assert ok is True
    assert "ABC" in msg


def test_ensure_english_switch_fail_require(monkeypatch):
    monkeypatch.setattr(
        engine, "TIS", FakeTIS(sid="com.apple.inputmethod.SCIM.ITABC", select_ok=False)
    )
    ok, _ = engine.ensure_english(auto_switch=True, require=True)
    assert ok is False


def test_ensure_english_tis_unavailable(monkeypatch):
    monkeypatch.setattr(engine, "TIS", FakeTIS(available=False))
    ok, _ = engine.ensure_english(auto_switch=True, require=True)
    assert ok is False  # require → 中止
    ok, _ = engine.ensure_english(auto_switch=True, require=False)
    assert ok is True  # 降级继续


# ---------- 插件接线 ----------


def _plugin_with(tmp_path, monkeypatch, user_cfg=None):
    """构造 autotype 插件，权限放行。

    **不再重定向 ``paths.APP_SUPPORT``**（2026-09-29）：本组件一个文件都不落
    ——「上次使用」是内存态（§11.3），别的一切都走配置。数据目录重定向对它就无从生效，
    也不再需要。
    """
    import barkit.plugins.autotype.plugin as mod

    cfg = user_cfg or {}
    ctx = type("Ctx", (), {"config": type("C", (), {"get": lambda self, k, d=None: _get(cfg, k, d)})()})()
    p = mod.Plugin(ctx)
    monkeypatch.setattr("barkit.plugins.autotype.plugin.accessibility_trusted", lambda: True)
    return p, mod


def _get(cfg: dict, dotted: str, default=None):
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def test_plugin_contracts(monkeypatch, tmp_path):
    p, _ = _plugin_with(tmp_path, monkeypatch)
    assert p.id == "autotype"
    from barkit.core.plugin import RUN_ON_DEMAND

    assert p.run_mode == RUN_ON_DEMAND
    assert p.api_version == 3
    
    hks = list(p.hotkeys())
    assert any(s.name == "autotype_last" for s in hks)


def test_last_profile_lives_in_memory_only(monkeypatch, tmp_path):
    """「上次使用」只在内存里（2026-09-29）：**盘上一份文件都不留**。

    它从前落 ``autotype_last.txt``（11 字节），而按准入两问都答不上 —— 丢了只是
    重启后第一次按热键多选一次，那条回落路径本来就有。另一半理由更该删：它当年还
    **没进 ``resources()``**（写了文件却不入账 ⇒ 卸载不删、导出不带），留着比删掉
    更说不清「这个目录里该有哪些文件」。
    """
    p, _ = _plugin_with(tmp_path, monkeypatch)
    assert p.get_last() is None, "新进程没有「上次」—— 这是常态，不是故障"
    p._set_last("cloudclient")
    assert p.get_last() == "cloudclient", "这一次运行里记住它"
    assert not list(tmp_path.rglob("autotype_last.txt")), "不该有任何落盘残留"


def test_type_profile_full_pipeline(monkeypatch, qtbot, tmp_path):
    """验收 2.6 关键路径：配置 → 倒计时浮层 → 逐字符注入 → 完成 → 记上次项。

    防真发键：monkeypatch engine.post_char/post_keycode 记录。
    delay=0 省略倒计时等待，直接进注入阶段。
    """
    sent: list[str] = []
    monkeypatch.setattr(engine, "post_char", lambda ch: sent.append(ch))
    monkeypatch.setattr(engine, "post_keycode", lambda code: sent.append(f"<key:{code}>"))

    cfg = {
        "plugins": {
            "autotype": {
                "enabled": True,
                "delay": 0,
                "interval": 0.01,
                "profiles": [{"name": "demo", "text": "abc", "press_enter": True}],
            }
        }
    }
    p, _ = _plugin_with(tmp_path, monkeypatch, cfg)
    monkeypatch.setattr(
        "barkit.plugins.autotype.plugin.engine.ensure_english",
        lambda auto_switch, require: (True, "ok"),
    )

    p.type_profile("demo")
    assert p._overlay is not None
    assert p._overlay.isVisible()

    # 泵事件循环直到注入完成（QTimer 驱动，interval 10ms + 100ms tick 粒度）
    deadline = 3.0
    qtbot.waitUntil(
        lambda: p._overlay is None or not p._overlay.isVisible(),
        timeout=int(deadline * 1000),
    )
    assert sent == ["a", "b", "c", "<key:36>"]  # 36 = RETURN
    assert p.get_last() == "demo"


def test_type_last_without_a_last_item_points_at_the_panel_row(monkeypatch, tmp_path):
    """无上次项时**不再弹列表选择框** —— 回一句回执，指用户去面板那一行选。

    改前这里弹 ``QInputDialog.getItem``（模态）：选项本来就在面板那一行的下拉里
    （``ChoiceSpec``），同一件事两个入口只是让用户多一层选择；而那个框是本文件能进
    ``PLUGIN_PYSIDE6_WHITELIST`` 的最后一个理由。

    **判据取「一个字符都没打出去」＋「回执指了路」**：只判「没弹框」（例如断言
    ``_pick_profile`` 不存在）挡不住回退，只判「返回了东西」也挡不住
    「返回一句没用的话」—— 用户看到的必须是**下一步该做什么**。
    """
    sent: list[str] = []
    monkeypatch.setattr(engine, "post_char", lambda ch: sent.append(ch))
    cfg = {
        "plugins": {
            "autotype": {
                "enabled": True,
                "delay": 0,
                "interval": 0.01,
                "profiles": [{"name": "first", "text": "1"}, {"name": "second", "text": "2"}],
            }
        }
    }
    p, _ = _plugin_with(tmp_path, monkeypatch, cfg)

    result = p.type_last()

    assert sent == [], "挑不出要打哪一项，就一个字符都不该打出去"
    assert p._overlay is None, "没有倒计时浮层（那意味着已经在打了）"
    assert result is not None and result.outcome is ActionOutcome.FAILED
    assert "面板" in result.text, "要指路：选项在面板那一行的下拉里"
    assert p.get_last() is None, "这次没打成，不该记成「上次用了它」"


def test_one_click_input_works_after_a_restart_when_there_is_only_one_item(
    monkeypatch, qtbot, tmp_path
):
    """重启后（内存里没有「上次项」）点「一键输入」：**只有一项**就该直接用它。

    违规必抓：把 ``type_last`` 写回 ``type_profile(None)``（只认内存里的上次项），
    本用例会红 —— 那正是 2026-09-30 的真机坏法：重启之后按钮一律失败，回执还让用户
    「去面板那一行的下拉选一项」，而那枚按钮根本不看下拉选的是什么。
    """
    sent: list[str] = []
    monkeypatch.setattr(engine, "post_char", lambda ch: sent.append(ch))
    monkeypatch.setattr(
        "barkit.plugins.autotype.plugin.engine.ensure_english",
        lambda auto_switch, require: (True, "ok"),
    )
    cfg = {
        "plugins": {
            "autotype": {
                "enabled": True,
                "delay": 0,
                "interval": 0.01,
                "profiles": [{"name": "only", "text": "xy"}],
            }
        }
    }
    p, _ = _plugin_with(tmp_path, monkeypatch, cfg)
    assert p.get_last() is None, "前提：刚重启，内存里没有上次项"

    result = p.type_last()

    assert result is None, "该交给倒计时浮层去跑，而不是当场回一句失败"
    assert p._overlay is not None, "没有起倒计时浮层（那意味着根本没要打）"
    qtbot.waitUntil(
        lambda: p._overlay is None or not p._overlay.isVisible(), timeout=3000
    )
    assert sent == ["x", "y"], "唯一那一项没被输进去"


def test_the_row_choice_wins_over_the_last_item(monkeypatch, qtbot, tmp_path):
    """面板下拉选中的那一项优先于「上次项」—— 两个入口同一个判定，不是各判一次。

    违规必抓：让「一键输入」按钮只认 ``get_last()``（不看选中项），本用例会红。
    """
    sent: list[str] = []
    monkeypatch.setattr(engine, "post_char", lambda ch: sent.append(ch))
    monkeypatch.setattr(
        "barkit.plugins.autotype.plugin.engine.ensure_english",
        lambda auto_switch, require: (True, "ok"),
    )
    cfg = {
        "plugins": {
            "autotype": {
                "enabled": True,
                "delay": 0,
                "interval": 0.01,
                "profiles": [
                    {"name": "a", "text": "AAA"},
                    {"name": "b", "text": "BBB"},
                ],
            }
        }
    }
    p, _ = _plugin_with(tmp_path, monkeypatch, cfg)
    p._set_last("a")
    p._choice = "b"

    p._type_choice()
    qtbot.waitUntil(
        lambda: p._overlay is None or not p._overlay.isVisible(), timeout=3000
    )
    assert sent == ["B", "B", "B"], f"输的不是下拉选中的那一项：{sent}"


def test_the_target_of_one_click_input_is_decided_in_one_place():
    """「这一次该输哪一项」只许 ``_target_name`` **一处**判定。

    违规必抓：在按钮 / 整行里各写一次判定（``type_profile(None)`` 或
    ``self._choice_current()``），本用例会红 —— 两处判定里错的那处不报错，
    表现正是「用户选了、按钮还是失败」。
    """
    import pathlib
    import re

    from barkit.plugins.autotype import plugin as mod

    src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    calls = re.findall(r"self\.type_profile\((.*)\)", src)
    assert calls, "一个调用都没找到 —— 判定被搬走了，这条门禁也得跟着改"
    assert set(calls) == {"self._target_name()"}, (
        f"「该输哪一项」长出了第二份判定：{sorted(set(calls))}"
    )


def test_type_profile_missing_entry_reports_where_to_fix_it(monkeypatch, tmp_path):
    """点名了不存在的输入项 ⇒ 回执要说清**去哪一页**看（不再弹「输入项不存在」框）。"""
    cfg = {"plugins": {"autotype": {"enabled": True, "profiles": [{"name": "a", "text": "x"}]}}}
    p, _ = _plugin_with(tmp_path, monkeypatch, cfg)

    result = p.type_profile("nope")

    assert result is not None and result.outcome is ActionOutcome.FAILED
    assert "nope" in result.text and "设置窗" in result.text


def test_type_profile_no_entries_at_all_says_where_to_add_one(monkeypatch, tmp_path):
    """一条输入项都没有（面板那一行此时**不可点**）⇒ 回执指设置页，而不是面板行。

    这一格与上一格的分叉必须分开守：两条路的**下一步动作不同**（去设置页加一条 vs.
    去面板选一项）。合成一句「还没有可用的输入项」看着省事，实际是把用户丢在原地。
    """
    p, _ = _plugin_with(tmp_path, monkeypatch, {"plugins": {"autotype": {"enabled": True}}})

    result = p.type_profile(None)

    assert result is not None and result.outcome is ActionOutcome.FAILED
    assert "设置窗" in result.text
    assert "面板" not in result.text, "一条都没有时指去面板那一行是死路（它不可点）"


def test_type_profile_ime_not_switchable_is_reported(monkeypatch, tmp_path):
    """输入法切不回英文 ⇒ 回执带上原因（改前是 ``QMessageBox.warning(None, "输入法需切换", msg)``）。"""
    monkeypatch.setattr(
        "barkit.plugins.autotype.plugin.engine.ensure_english",
        lambda auto_switch, require: (False, "请把输入法切到【英文/ABC】后重试。"),
    )
    cfg = {"plugins": {"autotype": {"enabled": True, "profiles": [{"name": "a", "text": "x"}]}}}
    p, _ = _plugin_with(tmp_path, monkeypatch, cfg)

    result = p.type_profile("a")

    assert result is not None and result.outcome is ActionOutcome.FAILED
    assert "输入法" in result.text and "英文/ABC" in result.text, "原句要带上，别只剩一句「失败了」"
    assert p._overlay is None, "输入法没切好就不该开始注入"


def test_successful_type_profile_says_nothing(monkeypatch, qtbot, tmp_path):
    """打成了就**不说话**（`None`）—— 倒计时浮层本身就是那个「看见了」的凭证。

    这条守着 `ActionResult` 的契约（成功且状态本身看得见就别再占一行字）：把
    ``return None`` 顺手改成 ``ActionResult(OK, "已开始输入")`` 会让每次触发热键
    都多一条系统通知。
    """
    monkeypatch.setattr(engine, "post_char", lambda ch: None)
    monkeypatch.setattr(engine, "post_keycode", lambda code: None)
    monkeypatch.setattr(
        "barkit.plugins.autotype.plugin.engine.ensure_english",
        lambda auto_switch, require: (True, "ok"),
    )
    cfg = {
        "plugins": {
            "autotype": {
                "enabled": True, "delay": 0, "interval": 0.01,
                "profiles": [{"name": "demo", "text": "a"}],
            }
        }
    }
    p, _ = _plugin_with(tmp_path, monkeypatch, cfg)

    assert p.type_profile("demo") is None, "交给浮层去跑的那条路没话要说"
    assert p._overlay is not None, "浮层应当起来了"
    qtbot.waitUntil(lambda: p._overlay is None or not p._overlay.isVisible(), timeout=3000)


# ---------- 设置页（settings.py，2026-09-19 由 config_dialog.py 收归）----------


def _editor_with(tmp_path, user_yaml: str = "# 用户差异项\nplugins:\n  autotype:\n    enabled: true\n"):
    """用 tmp 配置构造真实 ``Config`` ＋ autotype 插件 ＋ 设置页主体。

    这里必须用真 ``Config``（不是 ``_plugin_with`` 那个只会 ``get`` 的假 ctx）：
    设置页要验证的正是**写回 YAML** 这条路径（原子写 ＋ chmod 600）。
    """
    from barkit.core.config import Config

    default_path = tmp_path / "config.default.yaml"
    default_path.write_text(
        "schema_version: 1\nhotkeys: {}\nplugins: {}\n", encoding="utf-8"
    )
    user_path = tmp_path / "config.user.yaml"
    user_path.write_text(user_yaml, encoding="utf-8")

    config = Config(user_path=user_path, default_path=default_path)
    from barkit.plugins.autotype.plugin import Plugin
    from barkit.plugins.autotype.settings import _ProfileEditor

    plugin = Plugin(type("Ctx", (), {"config": config})())
    return _ProfileEditor(plugin), plugin, user_path


def _click_save(ed) -> None:
    """点**真实那枚**「保存」键（不直接调内部方法）。

    `ui_kit.EditScreen` 的保存两端是**注入**的回调（它是组合件，在消费方那边覆写是
    调不到的 —— 2026-09-27 踩过「键亮着、点了没反应」）。只有走按钮才验证得到那条接线。
    """
    ed._edit.save_button.click()


def _user_section(user_path) -> dict:
    import yaml

    return yaml.safe_load(user_path.read_text(encoding="utf-8"))["plugins"]["autotype"]


def test_editor_add_and_write_back(qtbot, tmp_path):
    """新增输入项 → 保存 → 用户 YAML 写入正确（保留其它差异项）＋ chmod 600。"""
    ed, _, user_path = _editor_with(tmp_path)
    qtbot.addWidget(ed)

    ed._on_add()
    ed._name_edit.setText("wifi")
    ed._text_edit.setText("route123")
    ed._delay_check.setChecked(True)
    ed._delay_spin.setValue(6)
    ed._enter_check.setChecked(True)
    _click_save(ed)

    section = _user_section(user_path)
    assert section["profiles"] == [
        {"name": "wifi", "text": "route123", "delay": 6, "press_enter": True}
    ]
    # 原子写 + chmod 600（autotype 存明文，权限必须收紧）
    assert (os.stat(user_path).st_mode & 0o777) == 0o600
    # 用户原来只写了 enabled，写回后仍在（其它差异项不丢）
    assert section["enabled"] is True


def test_editor_add_is_a_draft_until_saved(qtbot, tmp_path):
    """「＋ 新增」只进编辑页，**不落盘也不进列表** —— 保存后才有这一条。

    这条守的是「不拿『字段为空』当判据」的反面：旧实现把新条目直接塞进内存列表，
    中途关窗就走掉了；而若改成"立即写一条空条目"，写下去的是一条永远拒绝执行的坏数据。
    """
    ed, _, user_path = _editor_with(tmp_path)
    qtbot.addWidget(ed)

    ed._on_add()
    assert ed._adding is True
    assert ed._profiles == []
    assert "profiles" not in _user_section(user_path)

    ed._name_edit.setText("wifi")
    ed._text_edit.setText("route123")
    _click_save(ed)
    assert [p["name"] for p in _user_section(user_path)["profiles"]] == ["wifi"]


def test_editor_back_from_add_discards_the_draft(qtbot, tmp_path):
    """新增态点「‹ 返回」＝ 丢弃草稿，盘上不留痕迹。"""
    ed, _, user_path = _editor_with(tmp_path)
    qtbot.addWidget(ed)

    ed._on_add()
    ed._name_edit.setText("half-done")
    ed._on_back()

    assert ed._adding is False
    assert ed._profiles == []
    assert "profiles" not in _user_section(user_path)


def test_editor_edit_profile(qtbot, tmp_path):
    """先加一条、再在编辑页改名改文本；不写 ``delay`` 键就是不覆盖全局默认。"""
    ed, _, user_path = _editor_with(tmp_path)
    qtbot.addWidget(ed)

    ed._on_add()
    ed._name_edit.setText("cloud")
    ed._text_edit.setText("pw1")
    _click_save(ed)
    assert _user_section(user_path)["profiles"] == [{"name": "cloud", "text": "pw1"}]

    # 编辑：改名 + 改文本，delay 保持全局（不写 delay 键）
    ed._open_edit(0)
    ed._name_edit.setText("cloud2")
    ed._text_edit.setText("pw2")
    _click_save(ed)
    assert _user_section(user_path)["profiles"] == [{"name": "cloud2", "text": "pw2"}]


def test_editor_delete_writes_back(qtbot, tmp_path):
    """编辑页「删除这条」立刻落盘，列表与 YAML 同步少一条。"""
    ed, _, user_path = _editor_with(tmp_path)
    qtbot.addWidget(ed)

    for name in ("a", "b"):
        ed._on_add()
        ed._name_edit.setText(name)
        ed._text_edit.setText("1")
        _click_save(ed)
    assert [p["name"] for p in _user_section(user_path)["profiles"]] == ["a", "b"]

    ed._open_edit(0)
    ed._on_delete()
    assert [p["name"] for p in _user_section(user_path)["profiles"]] == ["b"]
    assert ed._profiles == [{"name": "b", "text": "1"}]


def test_editor_duplicate_name_blocked(qtbot, tmp_path):
    """重名被拒，且报错落在**行内反馈槽**里（旧实现是 ``QMessageBox``）。"""
    ed, _, user_path = _editor_with(tmp_path)
    qtbot.addWidget(ed)

    ed._on_add()
    ed._name_edit.setText("a")
    ed._text_edit.setText("1")
    _click_save(ed)

    ed._on_add()
    ed._name_edit.setText("a")
    ed._text_edit.setText("2")
    _click_save(ed)

    assert "重复" in ed._edit_fb.text()
    assert [p["name"] for p in _user_section(user_path)["profiles"]] == ["a"]
    assert ed._adding is True, "被拒之后应仍停在新增态，用户的输入没有被丢"


def test_editor_invalid_input_blocked(qtbot, tmp_path):
    """校验复用执行路径那个纯函数：文本为空 → 行内报错，盘上不留条目。"""
    ed, _, user_path = _editor_with(tmp_path)
    qtbot.addWidget(ed)

    ed._on_add()
    ed._name_edit.setText("x")
    ed._text_edit.setText("")
    _click_save(ed)

    assert "文本" in ed._edit_fb.text()
    assert "profiles" not in _user_section(user_path)
    assert ed._adding is True, "没有进入保存态，用户还得接着填"


def test_editor_write_failure_rolls_back_and_reports_inline(qtbot, tmp_path, monkeypatch):
    """写盘失败：行内报错 ＋ **内存态回滚** —— 界面不许显示一条其实没存下的条目。"""
    ed, plugin, user_path = _editor_with(tmp_path)
    qtbot.addWidget(ed)
    monkeypatch.setattr(plugin, "save_profiles", lambda profiles: False)

    ed._on_add()
    ed._name_edit.setText("wifi")
    ed._text_edit.setText("route123")
    _click_save(ed)

    assert ed._profiles == [], "写盘失败了，列表里不该留下这一条"
    assert ed._adding is True
    assert "没成功" in ed._edit_fb.text()
    assert "profiles" not in _user_section(user_path)


def test_editor_interval_and_enter_delay_write_seconds(qtbot, tmp_path):
    """两个毫秒格子写回的是**秒**（``interval`` 出厂 0.03 s ⟺ 30 ms）。"""
    ed, _, user_path = _editor_with(tmp_path)
    qtbot.addWidget(ed)

    ed._interval_spin.setValue(50)
    ed._interval_spin.editingFinished.emit()
    assert _user_section(user_path)["interval"] == 0.05

    ed._enter_delay_spin.setValue(800)
    ed._enter_delay_spin.editingFinished.emit()
    section = _user_section(user_path)
    assert section["enter_delay"] == 0.8
    # 两个格子各自只写自己那一个键（改写 enter_delay 不该把 interval 重写一遍）
    assert section["interval"] == 0.05


def test_editor_auto_switch_ime_toggle_round_trip(qtbot, tmp_path):
    """自动切 ABC：点开关 → 写盘；再点 → 写回反值（``Switch`` 不自翻自身）。

    拨回**出厂缺省**（true）时用户 YAML 里不留这个键 —— 写侧口径是「等于声明默认值
    即删键」（`cfg.set`）。断言因此看**有效值**（`plugin.autotype_settings()`，走合并层），
    而不是「文件里有没有那一行」：留着同值键会把缺省钉死，组件日后改缺省传不到用户。
    """
    ed, plugin, user_path = _editor_with(tmp_path)
    qtbot.addWidget(ed)
    assert ed._ime_sw.isChecked() is True          # 出厂默认 true

    ed._on_toggle_ime()
    assert _user_section(user_path)["auto_switch_ime"] is False
    assert ed._ime_sw.isChecked() is False          # 回读同步

    ed._on_toggle_ime()
    assert "auto_switch_ime" not in _user_section(user_path), \
        "等于缺省 ⇒ 删键（不是照着写一遍 true）"
    assert plugin.autotype_settings()["auto_switch_ime"] is True, "有效值仍是缺省那一份"
    assert ed._ime_sw.isChecked() is True


def test_editor_text_echo_password_by_default(qtbot, tmp_path):
    """输入文本默认密文显示（不暴露明文），眼睛按钮可切换。"""
    from PySide6.QtWidgets import QLineEdit

    ed, _, _ = _editor_with(tmp_path)
    qtbot.addWidget(ed)
    ed._on_add()
    assert ed._text_edit.echoMode() == QLineEdit.EchoMode.Password
    # 眼睛动作已挂在尾部且带图标
    assert not ed._reveal_action.icon().isNull()


def test_editor_toggle_reveal(qtbot, tmp_path):
    """点击眼睛按钮：密文 ↔ 明文往返切换。"""
    from PySide6.QtWidgets import QLineEdit

    ed, _, _ = _editor_with(tmp_path)
    qtbot.addWidget(ed)
    ed._on_add()
    ed._text_edit.setText("secret")
    assert ed._text_edit.echoMode() == QLineEdit.EchoMode.Password

    ed._reveal_action.trigger()  # 模拟点击
    assert ed._text_edit.echoMode() == QLineEdit.EchoMode.Normal
    assert ed._reveal_action.toolTip() == "隐藏明文"

    ed._reveal_action.trigger()  # 再点回去
    assert ed._text_edit.echoMode() == QLineEdit.EchoMode.Password
    assert ed._reveal_action.toolTip() == "显示明文"


def test_editor_list_row_does_not_leak_the_plaintext(qtbot, tmp_path):
    """列表行只显示名称与参数，**不显示明文**（设置窗里可能有人从背后看屏幕）。"""
    from PySide6.QtWidgets import QLabel

    ed, _, _ = _editor_with(tmp_path)
    qtbot.addWidget(ed)

    ed._on_add()
    ed._name_edit.setText("wifi")
    ed._text_edit.setText("route123")
    ed._delay_check.setChecked(True)
    ed._delay_spin.setValue(6)
    ed._enter_check.setChecked(True)
    _click_save(ed)

    row = ed._rows_lay.itemAt(0).widget()
    texts = [c.text() for c in row.findChildren(QLabel)]
    assert "route123" not in " ".join(texts)
    assert "wifi" in " ".join(texts)
    assert any("6 秒" in t and "回车" in t for t in texts), texts


# ---------- 面板行：下拉与整行点击不得互相连带（任务板 §5 #29 P0 ③）----------


def _framework_row(plugin, qtbot):
    """用**框架**渲染出 autotype 那一行（声明式路径，不再是自绘的 ``custom`` 行）。

    行的身份图标由面板按 ``plugin_id`` 注入（``StatusPanel._component_icons``，生产上
    来自 ``set_components``）；测试里只补这一枚即可，不必凑齐整份组件清单。
    返回 ``(panel, row)`` —— panel 要留在作用域里替行保命。
    """
    from PySide6.QtWidgets import QApplication

    from barkit.contribution.specs import IconName, StatusRowSpec
    from barkit.core.contributions import gather_specs
    from barkit.shell.panel import StatusPanel, _StatusRowView

    QApplication.instance() or QApplication([])
    panel = StatusPanel()
    qtbot.addWidget(panel)
    spec = next(
        s for s in gather_specs(plugin, plugin.id) if isinstance(s, StatusRowSpec)
    )
    panel._component_icons[plugin.id] = IconName.KEYBOARD
    panel.sync_status_rows([spec])
    row = panel._row_widgets[plugin.id][0]
    assert isinstance(row, _StatusRowView), "autotype 那一行应由框架渲染，不是自绘行"
    return panel, row


def test_panel_row_combo_does_not_trigger_the_row_click(monkeypatch, qtbot, tmp_path):
    """点行内那把**下拉**不得连带跑整行点击 —— 这一行的整行点击＝把密码打出去。

    P0 ③ 原文担心的是「行内下拉控件走 ``HoverRow`` 的 ``combo`` 排除路径，而无人传参
    ⇒ 点它必连带触发整行 ``on_click``」。本组件的 profile 下拉正是那个实证：它确实是一个
    「可点整行（输入当前条目）＋ 行内下拉（换默认条目）」的行。

    **核实结论（本用例就是证据）**：不穿透。点击面是 ``ProfileSelector`` 里那个
    ``QComboBox`` —— 它自己处理并 accept 按下，事件不会冒泡到行；而 ``ProfileSelector``
    四周 margin 为 0，不存在「落在选择器上、却由行接住」的空隙。所以 P0 ③ 判**不排**：
    ``HoverRow`` 那条 ``combo`` 分支至今没有任何生产调用方（面板的另一种"下拉"是
    ``MenuSpec`` → ``MenuButton``，``QPushButton`` 同样自己吃掉按下），但行为是对的。

    判据取「没开始输入」而不是「回调没跑」：这一行的 on_click 就是 ``type_profile``，
    而它对用户可见的后果就是开始逐字符注入。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    sent: list[str] = []
    monkeypatch.setattr(engine, "post_char", lambda ch: sent.append(ch))
    # 输入布局那条真实路径要打桩：不桩的话，「万一真穿透了」会去调 TIS（Windows 上直接挂住），
    # 用例就从"红"变成"卡死"—— 守门用例最不该有的失败长相就是卡死。
    monkeypatch.setattr(
        "barkit.plugins.autotype.plugin.engine.ensure_english",
        lambda auto_switch, require: (True, "ok"),
    )
    cfg = {
        "plugins": {
            "autotype": {
                "enabled": True,
                "delay": 0,
                "profiles": [{"name": "demo", "text": "abc"}, {"name": "other", "text": "xyz"}],
            }
        }
    }
    p, _ = _plugin_with(tmp_path, monkeypatch, cfg)
    panel, row = _framework_row(p, qtbot)
    selector = row._choices[0][0]          # 行内下拉（ChoiceSpec → ProfileSelector）
    from barkit.shell.ui_kit import ProfileSelector

    assert isinstance(selector, ProfileSelector)
    assert selector.parent() is row, "下拉挂在行上 —— 「整行点击」与「下拉换项」才是同一行"

    QTest.mouseClick(selector._combo, Qt.MouseButton.LeftButton)

    assert sent == [], "点下拉连带跑起了输入 —— 用户只是想换个默认条目"
    assert p._overlay is None, "整行点击（＝开始输入）被下拉的点击带跑了"
    # 下拉自己该做的还是要做：换选后是它更新 current，并经 on_change 回到插件
    selector._combo.setCurrentText("other")
    assert selector.current() == "other"
    assert p._choice_current() == "other", "改选要经 ChoiceSpec.on_change 回到插件，否则整行点击会输入错项"
    panel.deleteLater()


def test_panel_row_icon_comes_from_the_icon_library(qtbot, tmp_path, monkeypatch):
    """回归：行图标盒画的是**图标库的矢量字形**，不再是 emoji 文本。

    自绘行原先走 ``StatusRow(icon_text: str, …)`` —— 「传什么就画什么」，于是这里塞进了
    一个 emoji 键盘；而声明式那条路早只认 ``ComponentSpec.icon``（``StatusRowSpec`` 连
    ``icon`` 字段都已删）。现在两条路都只认 ``IconName``，图标由 ``shell/design/icons``
    渲成 pixmap（同日入门的 ``test_no_emoji_in_source`` 守另一头：源码里不许再出现 emoji）。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    cfg = {
        "plugins": {
            "autotype": {"enabled": True, "profiles": [{"name": "demo", "text": "abc"}]}
        }
    }
    p, _ = _plugin_with(tmp_path, monkeypatch, cfg)
    panel, row = _framework_row(p, qtbot)
    box = row._ico
    assert box.text() == "", "图标盒不该再有文本 —— emoji 当初正是从这条缝里进来的"
    pm = box.pixmap()
    assert not pm.isNull(), "图标盒没画上图标库的字形"
    # 22px / 36 盒 / 居中不是随手定的：这一行现在与其余组件**同走框架渲染**，三处
    # 常量就是框架那一套。曾经自绘行自己写了一份（30 盒 / 16px / 不设对齐），界面上
    # 表现为那一行图标小一圈且贴左 —— 锁住这三处防复发。
    assert (pm.width(), pm.height()) == (22, 22)
    assert box.width() == box.height() == 36
    # 居中是 QSS 的 ``qproperty-alignment`` 给的，**polish 之后才生效**（之前读到的是
    # 左对齐 129），故必须先 show 再断言。
    panel.show()
    QApplication.processEvents()
    assert box.alignment() & Qt.AlignmentFlag.AlignHCenter
    panel.deleteLater()


def test_retype_only_one_overlay_at_a_time(monkeypatch, qtbot, tmp_path):
    """倒计时未结束再按一次，旧的被撤掉、新的接管；不会有两份浮层同时可见、
    明文被输入两次（P1）。"""
    cfg = {
        "plugins": {
            "autotype": {
                "enabled": True, "delay": 2, "interval": 0.01,
                "profiles": [{"name": "demo", "text": "abc", "press_enter": True}],
            }
        }
    }
    p, _ = _plugin_with(tmp_path, monkeypatch, cfg)
    monkeypatch.setattr(
        "barkit.plugins.autotype.plugin.engine.ensure_english",
        lambda auto_switch, require: (True, "ok"),
    )
    p.type_profile("demo")
    first = p._overlay
    assert first is not None and first.isVisible()
    p.type_profile("demo")          # 倒计时未结束时再按
    assert first.isVisible() is False, "旧浮层应被撤掉"
    assert p._overlay is not None and p._overlay is not first
    assert p._overlay.isVisible()
