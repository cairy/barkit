"""tests/test_hotkey_rebind：热键「改完重挂」这条链（2026-09-23 新增）。

起因是用户报「OCR 快捷键无效，程序明明在运行」：那条热键的配置键**不在**（＝出厂值，
因为写侧「等于出厂值就删键」就是这么落盘的），而重挂那条链读配置时把「缺键」兜底成
**空串** ⇒ 当成停用 ⇒ 撤掉绑定后**回报成功** ⇒ 界面说「已启用」，实际这一条在当前
进程里再也没挂上，重启才好（启动时那条链是用出厂值兜底的）。

守四件事：

- **空串与缺键是两件事**（写侧靠这个区分「明确停用」与「没改过」）；
- **恢复出厂键位之后要真挂上**（用户撞上的那一条，也是本批唯一的回归判据）；
- **没挂上必须报失败**，不许跟「按你说的撤掉了」混成一个成功；
- **启动与重挂读同一处**，别再各写一份兜底（这次分叉就是两份兜底造成的）。

配置用**真件** ``Config``（落 ``tmp_path``）：这条链的错恰恰出在「写盘之后键到底还在
不在」，用配置桩就测不到那一层。
"""

from __future__ import annotations

import pytest

from barkit.contribution.specs import HotkeySpec
from barkit.core.config import Config
from barkit.core.hotkey_table import HotkeyTable
from barkit.core.hotkeys import parse_combo
from barkit.shell.app_shell import AppShell
from barkit.shell.hotkey_catalog import (
    REBIND_BOUND,
    REBIND_FAILED,
    REBIND_UNBOUND,
    SOURCE_PLUGIN,
    collect,
    effective_combo,
    reset_combo,
    write_combo,
)
from barkit.shell.hotkey_editor import HotkeyEditor

NAME = "recognize"
FACTORY = "ctrl+alt+o"
#: 「键不在」的读数标志：与空串区分开，别拿一个会被当成合法值的字符串当哨兵
MISSING = object()


def _config(tmp_path) -> Config:
    user = tmp_path / "config.yaml"
    user.write_text("{}\n", encoding="utf-8")
    return Config(user_path=user)


class _StubHotkeys:
    """只记「谁挂上了什么」：真件会向系统注册全局热键（CGEventTap），测试碰不得。"""

    def __init__(self, ok: bool = True) -> None:
        self.registered: dict[str, str] = {}
        self.ok = ok

    def register(self, name: str, combo: str, callback) -> bool:
        if not self.ok:
            return False
        self.registered[name] = combo
        return True

    def unregister(self, name: str) -> None:
        self.registered.pop(name, None)

    def unregister_all(self) -> None:
        self.registered.clear()

    @property
    def available(self) -> bool:
        return True


class _Contribs:
    """贡献注册中心的那一个口：重挂只问它「有哪些热键声明」。"""

    def __init__(self, specs) -> None:
        self._specs = list(specs)

    def hotkeys(self):
        return list(self._specs)


class _BoundStubHotkeys:
    """回查 / 冲突都走**真表**（``HotkeyTable``，纯策略，不碰系统）。

    真件会向系统注册全局热键（CGEventTap），测试碰不得；但冲突判据的逻辑全在
    ``HotkeyTable``，用真表跑就能抓到「stored callback != raw callback」那条自冲突。
    """

    def __init__(self) -> None:
        self._table = HotkeyTable()

    def register(self, name: str, combo: str, callback) -> bool:
        try:
            kc, mask = parse_combo(combo)
        except ValueError:
            return False
        try:
            self._table.bind(name, kc, mask, callback)
        except ValueError:
            return False
        return True

    def unregister(self, name: str) -> None:
        self._table.unbind(name)

    def unregister_all(self) -> None:
        self._table.clear()

    def get(self, name: str):
        return self._table.get(name)

    def conflict(self, name: str, combo: str, callback) -> object:
        try:
            kc, mask = parse_combo(combo)
        except ValueError:
            return None
        return self._table.conflict(name, kc, mask, callback)

    @property
    def available(self) -> bool:
        return True


def _shell(qapp, config, *, ok: bool = True) -> AppShell:
    """装配到「能重挂」为止（**不走 assemble()**：那条链要建菜单栏图标与全局热键）。"""
    shell = AppShell(qapp)
    shell._config = config
    shell._contributions = _Contribs([
        HotkeySpec(name=NAME, label="截图识别", default_combo=FACTORY,
                   plugin_id="recognize", callback=lambda: None),
    ])
    shell._hotkeys = _StubHotkeys(ok=ok)
    return shell


def test_a_missing_key_resolves_to_the_factory_combo(tmp_path):
    """缺键 = 没改过 ⇒ 用出厂值（写侧「等于出厂值就删键」正是这么落盘的）。"""
    cfg = _config(tmp_path)
    assert effective_combo(cfg, NAME, FACTORY) == FACTORY


def test_an_empty_string_means_disabled_not_unset(tmp_path):
    """控制组：空串是**用户明确停用**，不许回落成出厂值 —— 两件事合成一件就是这次的病。"""
    cfg = _config(tmp_path)
    write_combo(cfg, NAME, "", FACTORY)
    assert cfg.get(f"hotkeys.{NAME}", MISSING) == "", "空串必须真的落盘（不是删键）"
    assert effective_combo(cfg, NAME, FACTORY) == ""


def test_restoring_the_factory_combo_binds_it_again(qapp, tmp_path):
    """**用户撞上的那一条**：停用 → 恢复出厂键位，重挂必须把它真的挂回去。

    恢复出厂键位在写侧是**删键**（``write_combo`` 的「等于出厂值就删键」），
    旧的重挂把删键读成空串 ⇒ 撤掉绑定还回报成功 ⇒ 界面说「已启用」而按不响。
    """
    cfg = _config(tmp_path)
    shell = _shell(qapp, cfg)

    # ① 出厂态：第一次重挂就该挂上
    assert shell._rebind_hotkey(NAME) == REBIND_BOUND
    assert shell._hotkeys.registered[NAME] == FACTORY

    # ② 停用：空串落盘 ⇒ 撤掉绑定，回报「撤掉了」而不是「失败」
    write_combo(cfg, NAME, "", FACTORY)
    assert shell._rebind_hotkey(NAME) == REBIND_UNBOUND
    assert NAME not in shell._hotkeys.registered

    # ③ 恢复出厂键位（＝删键）⇒ **必须重新挂上**，且回报「挂上了」
    write_combo(cfg, NAME, FACTORY, FACTORY)
    assert cfg.get(f"hotkeys.{NAME}", MISSING) is MISSING, "恢复出厂键位在写侧就是删键"
    assert shell._rebind_hotkey(NAME) == REBIND_BOUND
    assert shell._hotkeys.registered[NAME] == FACTORY


def test_the_reset_button_path_binds_it_again_too(qapp, tmp_path):
    """编辑屏那枚「初始化」走的是 ``reset_combo``（同样是删键），一并锁住。"""
    cfg = _config(tmp_path)
    shell = _shell(qapp, cfg)
    write_combo(cfg, NAME, "", FACTORY)
    assert shell._rebind_hotkey(NAME) == REBIND_UNBOUND

    reset_combo(cfg, NAME)
    assert shell._rebind_hotkey(NAME) == REBIND_BOUND
    assert shell._hotkeys.registered[NAME] == FACTORY


def test_a_bind_that_did_not_happen_is_reported_as_failed(qapp, tmp_path):
    """控制组：配置要它占键却没挂上（没权限 / 键位非法 / 撞车）⇒ **必须**报失败。"""
    cfg = _config(tmp_path)
    shell = _shell(qapp, cfg, ok=False)
    assert shell._rebind_hotkey(NAME) == REBIND_FAILED
    assert NAME not in shell._hotkeys.registered


@pytest.mark.parametrize("result,expect_failure", [
    (REBIND_FAILED, True),
    (REBIND_BOUND, False),
    (REBIND_UNBOUND, False),
])
def test_the_editor_reports_only_a_bind_that_did_not_happen(qtbot, tmp_path, result,
                                                            expect_failure):
    """编辑屏「保存」那条路同款：只有没挂上才报错，撤掉了不算。"""
    cfg = _config(tmp_path)
    entries = collect(config=cfg, hotkeys=[
        HotkeySpec(name=NAME, label="截图识别", default_combo=FACTORY, plugin_id=NAME),
    ])
    entry = next(e for e in entries if e.name == NAME)
    editor = HotkeyEditor(config=cfg, entry=entry, on_rebind=lambda _n: result)
    qtbot.addWidget(editor)

    editor._pill.captured.emit("ctrl+alt+r")
    editor._save_btn.click()
    assert ("没能挂上" in editor._feedback.text()) is expect_failure


def test_startup_and_rebind_read_the_same_resolver(qapp, tmp_path, monkeypatch):
    """启动那条链与重挂那条链**必须问同一处**：各写一份兜底就是这次分叉的成因。

    判据不是「两处代码长一样」（那会跟着实现一起改），而是两条链真的都调用了它。
    """
    import barkit.shell.app_shell as mod

    # 打桩点落**真身子模块**：``effective_combo`` 是 hotkeys 里的方法往模块全局解析的
    # 名字（形态 G：包属性只是导入时那份快照），打在包上三条链都读不到、判据全部假绿。
    real = mod.hotkeys.effective_combo
    seen: list[str] = []

    def spy(config, name, default=""):
        seen.append(name)
        return real(config, name, default)

    monkeypatch.setattr(mod.hotkeys, "effective_combo", spy)
    cfg = _config(tmp_path)
    shell = _shell(qapp, cfg)

    seen.clear()
    shell._register_hotkeys(shell._contributions, cfg, shell._hotkeys)
    assert NAME in seen, "启动注册没走 effective_combo"

    seen.clear()
    shell._register_framework_hotkeys()
    assert seen, "框架热键注册没走 effective_combo"

    seen.clear()
    shell._rebind_hotkey(NAME)
    assert NAME in seen, "重挂没走 effective_combo"


def test_startup_skips_an_empty_combo_but_says_so(qapp, tmp_path, caplog):
    """不占键时**留一条日志**：「按了没反应」最难自查，静默跳过让人无处下手。"""
    cfg = _config(tmp_path)
    shell = _shell(qapp, cfg)
    write_combo(cfg, NAME, "", FACTORY)

    with caplog.at_level("INFO", logger="barkit.shell.app_shell"):
        shell._register_hotkeys(shell._contributions, cfg, shell._hotkeys)
    assert NAME not in shell._hotkeys.registered
    assert any(NAME in r.getMessage() for r in caplog.records), "跳过了却一声不吭"


def test_editing_a_bound_hotkey_reports_no_self_conflict(qapp, tmp_path):
    """改键时冲突检查不能把「自身」当成占用者。

    非自建条目登记时包了一层反馈（``_wrap_with_feedback``），而 ``_hotkey_callbacks()``
    返回原始回调。旧实现把原始回调传给 ``HotkeyTable.conflict``，与表里自身（包裹后）不相等，
    于是 ``kind='name'`` 自冲突，改键永远报「已被自己占用，没有改动」。回查表里真身之后，
    自身恒相等，空闲键位不再误报；真有别的名字占了同一个键才报冲突。
    """
    cfg = _config(tmp_path)
    shell = _shell(qapp, cfg)
    shell._hotkeys = _BoundStubHotkeys()
    # 启动那条链登记（自带反馈包装，模拟非自建条目的真身与表里存的不一致于原始回调）
    wrapped = shell._wrap_with_feedback(NAME, SOURCE_PLUGIN, lambda: None)
    assert shell._hotkeys.register(NAME, FACTORY, wrapped)

    # 改成一个空闲键位：旧实现会报「已被自己占用」，修复后必须给 None
    assert shell._hotkey_conflict(NAME, "cmd+p") is None

    # 控制组：真有别的名字占了同一个键，仍要报冲突（占用者是那个别的名字）
    other = "other_plugin"
    shell._contributions = _Contribs([
        HotkeySpec(name=NAME, label="截图识别", default_combo=FACTORY,
                   plugin_id="recognize", callback=lambda: None),
        HotkeySpec(name=other, label="别的条目", default_combo="cmd+p",
                   plugin_id="other", callback=lambda: None),
    ])
    assert shell._hotkeys.register(other, "cmd+p", lambda: None)
    assert shell._hotkey_conflict(NAME, "cmd+p") == "别的条目"
