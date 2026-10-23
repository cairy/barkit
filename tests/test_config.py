"""core/config 测试（P0 0.3）。

§9.5 配置默认值单一真源：**全部**内置插件的业务默认值都由各自的
``config_schema()`` 合成，出厂文件 ``config.default.yaml`` 只剩框架级键
（2026-09-20 收口，此前 5 个插件还写在出厂文件里）。

本文件的 ``NOTIFY_SCHEMA`` 是**测试自造的替身**（值与真实 notify_guard 的声明
相同，但不依赖插件 import），用来测「注入机制」本身；真实插件 schema 的注入
由 ``conftest`` 的 ``_inject_builtin_plugin_schemas`` 统一兜底。
"""

from pathlib import Path

import pytest
import yaml

from barkit.core.config import (
    DEFAULT_CONFIG_PATH,
    Config,
    ConfigError,
    atomic_write,
    deep_merge,
    validate,
)
from barkit.core.schema import FieldSpec, FieldType, FormSchema


# 已迁移样本插件的 schema：默认值在此声明，取代 config.default.yaml 的插件段。
NOTIFY_SCHEMA = FormSchema(
    plugin_id="notify_guard",
    fields=(
        FieldSpec(key="debounce", label="防抖周期数", type=FieldType.NUMBER, default=2),
        FieldSpec(key="auto_enabled", label="自动化（自动静音）", type=FieldType.BOOL, default=True),
        FieldSpec(key="rules", label="静音规则", type=FieldType.LIST, default=[]),
    ),
)


def _config(tmp_path, schemas=None):
    """构造注入 schema 的 Config（default_path 用仓库真实默认配置）。"""
    user = tmp_path / "config.yaml"
    cfg = Config(user_path=user)
    if schemas:
        cfg.set_plugin_schemas(schemas)
    return cfg


def test_deep_merge_dict_recursive():
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    override = {"a": {"y": 20}}
    assert deep_merge(base, override) == {"a": {"x": 1, "y": 20}, "b": 3}
    assert base == {"a": {"x": 1, "y": 2}, "b": 3}  # 不改入参


def test_deep_merge_list_replaces():
    assert deep_merge({"l": [1, 2]}, {"l": [9]}) == {"l": [9]}


def test_validate_rejects_bad_enabled():
    with pytest.raises(ConfigError):
        validate({"plugins": {"x": {"enabled": "yes"}}})


def test_validate_accepts_matching_schema_version():
    # 缺省视为当前版本，向后兼容旧/无该键的用户配置
    assert validate({}) == {}
    assert validate({"schema_version": 1}) == {"schema_version": 1}


def test_validate_rejects_mismatched_schema_version():
    with pytest.raises(ConfigError):
        validate({"schema_version": 2})


def test_validate_rejects_bad_tray():
    """tray 与 hotkeys 同规格的写法校验：写坏不该让渲染侧拿到非字符串来源。"""
    with pytest.raises(ConfigError):
        validate({"tray": {"base_source": 1}})
    with pytest.raises(ConfigError):
        validate({"tray": []})
    assert validate({"tray": {"base_source": ""}}) == {"tray": {"base_source": ""}}
    with pytest.raises(ConfigError):
        validate({"schema_version": 0})


def test_load_merge_and_get(tmp_path):
    cfg = _config(tmp_path, {"notify_guard": NOTIFY_SCHEMA})
    # 首次生成空用户文件
    assert (tmp_path / "config.yaml").exists()
    # 默认值来自插件 schema（config.default.yaml 已削除 notify 段）
    assert cfg.get("plugins.notify_guard.debounce") == 2
    # 点路径不存在回退默认
    assert cfg.get("no.such.key", "fb") == "fb"


def test_user_override_wins(tmp_path):
    user = tmp_path / "config.yaml"
    user.write_text("plugins:\n  notify_guard:\n    debounce: 10\n", encoding="utf-8")
    cfg = Config(user_path=user, plugin_schemas={"notify_guard": NOTIFY_SCHEMA})
    assert cfg.get("plugins.notify_guard.debounce") == 10
    assert cfg.get("plugins.notify_guard.auto_enabled") is True  # 其余默认仍在（来自 schema）


def test_save_user_atomic_and_600(tmp_path):
    user = tmp_path / "config.yaml"
    cfg = Config(user_path=user)
    cfg.save_user({"plugins": {"autotype": {"enabled": False, "delay": 4, "profiles": []}}})
    assert cfg.get("plugins.autotype.enabled") is False
    assert (user.stat().st_mode & 0o777) == 0o600
    data = yaml.safe_load(user.read_text())
    assert data["plugins"]["autotype"]["enabled"] is False


def test_reload_fallback_on_broken_yaml(tmp_path, qtbot):
    cfg = _config(tmp_path, {"notify_guard": NOTIFY_SCHEMA})
    errors = []
    cfg.error.connect(errors.append)
    # 写坏配置 → 热重载应保留旧快照并报错
    user = tmp_path / "config.yaml"
    user.write_text("plugins: [\nbroken", encoding="utf-8")
    import os
    os.utime(user)
    cfg._check_mtime()
    assert cfg.get("plugins.notify_guard.debounce") == 2  # 旧快照仍在（默认来自 schema）
    assert len(errors) == 1


def test_reload_emits_changed(tmp_path, qtbot):
    user = tmp_path / "config.yaml"
    cfg = Config(user_path=user)
    with qtbot.waitSignal(cfg.changed, timeout=1000):
        user.write_text("plugins:\n  notify_guard:\n    debounce: 7\n", encoding="utf-8")
        import os
        os.utime(user, ns=(0, int(cfg._mtime * 1e9) + 1_000_000_000))
        cfg._check_mtime()
    assert cfg.get("plugins.notify_guard.debounce") == 7


def test_atomic_write(tmp_path):
    p = tmp_path / "sub/a.yaml"
    atomic_write(p, "x: 1\n")
    assert p.read_text() == "x: 1\n"
    assert not p.with_suffix(".yaml.tmp").exists()


# ---------- 菜单栏图标：主图标来源（P0③） ----------


def test_tray_base_source_defaults_empty(tmp_path):
    """出厂默认 = 空串（BarKit 默认底图）：不替任何用户预设别人的图标。"""
    cfg = _config(tmp_path)
    assert cfg.tray_base_source() == ""


def test_set_tray_base_source_round_trip_and_clean_removal(tmp_path):
    """写→读一致；回落默认时**删键**（不留空值残渣），且不削掉用户其他段。"""
    user = tmp_path / "config.yaml"
    user.write_text("hotkeys:\n  recognize: ctrl+alt+o\n", encoding="utf-8")
    cfg = Config(user_path=user)
    state_id = "notify_guard.display"

    cfg.set_tray_base_source(state_id)
    assert cfg.tray_base_source() == state_id
    raw = yaml.safe_load(user.read_text(encoding="utf-8"))
    assert raw["tray"] == {"base_source": state_id}
    assert raw["hotkeys"] == {"recognize": "ctrl+alt+o"}   # 其他段未被削掉

    cfg.set_tray_base_source("")
    assert cfg.tray_base_source() == ""
    raw = yaml.safe_load(user.read_text(encoding="utf-8"))
    assert "tray" not in raw                                # 空值不留残渣
    assert raw["hotkeys"] == {"recognize": "ctrl+alt+o"}


def test_legacy_topic_value_is_folded_to_identity_on_read_only(tmp_path):
    """首版落的盘是**事件总线 topic**；读到它就地折成身份键，**不回写**。

    为什么要兼容：P0③ 首版已发出去过，用户盘上可能正躺着
    ``plugin.notify_guard.status.display``。改口径不能把人家已选的来源变成
    「查无此机」（那会让菜单栏莫名其妙回落默认底图）。
    为什么只读折算：写盘路径只有「用户动下拉」—— 他下次一动就自然覆盖，
    在此之前两种取值读出来等价（同一台机、同一个 key），不必替他改写配置文件。
    """
    user = tmp_path / "config.yaml"
    user.write_text(
        "tray:\n  base_source: plugin.notify_guard.status.display\n", encoding="utf-8"
    )
    cfg = Config(user_path=user)

    assert cfg.tray_base_source() == "notify_guard.display"
    # 原文件一字未动（读路径不产生副作用）
    assert "plugin.notify_guard.status.display" in user.read_text(encoding="utf-8")


def test_topic_fold_leaves_non_topic_values_alone(tmp_path):
    """折算只认 ``plugin.<id>.status.<key>`` 这一种形态，其余原样透传。

    防的是「顺手把别的字符串也切了」：身份键、空串、以及任何用户手写的怪值
    都不得被改名 —— 改名会让「设置窗显示默认、菜单栏找不着」这类不一致更难查。
    """
    from barkit.core.config import _topic_to_state_id

    assert _topic_to_state_id("notify_guard.display") == "notify_guard.display"
    assert _topic_to_state_id("") == ""
    assert _topic_to_state_id("whatever") == "whatever"
    # 形态像但 key 里带点（不是合法 key）⇒ 不折算，原样返回
    assert _topic_to_state_id("plugin.a.status.b.c") == "plugin.a.status.b.c"


def test_bad_tray_value_is_rejected_at_load(tmp_path):
    """写坏 tray 与写坏 hotkeys 同规格：加载期整份拒绝，不静默吞掉。

    （读取侧 ``tray_base_source()`` 另有一道 isinstance 兜底，防的是合成层/桩注入，
    不是给用户配置兜底——用户配置在这里就被 validate 拦住了。）
    """
    user = tmp_path / "config.yaml"
    user.write_text("tray:\n  base_source: [oops]\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        Config(user_path=user)


# ---------- 菜单栏角标：阈值两档（决议 D17，E 批补写侧） ----------


def test_tray_min_level_defaults_to_warn(tmp_path):
    """出厂档 = `warn`：没写过这个键的用户拿到的是「警告及以上都上屏」。"""
    cfg = _config(tmp_path)
    assert cfg.tray_min_level() == "warn"


def test_set_tray_min_level_round_trip_and_clean_removal(tmp_path):
    """写→读一致；改回**出厂档**时删键（默认值不落盘），且不削掉用户其他段。"""
    user = tmp_path / "config.yaml"
    user.write_text("hotkeys:\n  recognize: ctrl+alt+o\n", encoding="utf-8")
    cfg = Config(user_path=user)

    cfg.set_tray_min_level("err")
    assert cfg.tray_min_level() == "err"
    raw = yaml.safe_load(user.read_text(encoding="utf-8"))
    assert raw["tray"] == {"min_level": "err"}

    cfg.set_tray_min_level("warn")                      # 出厂档 = 默认值
    assert cfg.tray_min_level() == "warn"
    raw = yaml.safe_load(user.read_text(encoding="utf-8"))
    assert "tray" not in raw                            # 默认值不留残渣
    assert raw["hotkeys"] == {"recognize": "ctrl+alt+o"}


def test_set_tray_min_level_refuses_a_value_outside_the_domain(tmp_path):
    """写侧早 fail：档位值域归契约（两档），不静默落一个读侧还得兜底的键。

    `off` 是**合法级别**但不是可选档位 —— 这条同时钉住「值域 ≠ 级别词汇表」。
    """
    cfg = _config(tmp_path)
    with pytest.raises(ValueError):
        cfg.set_tray_min_level("off")


def test_set_log_level_round_trip_and_clean_removal(tmp_path):
    """写→读一致；选回首项的空串＝删键回落（不留 ``log: {level: ""}`` 残渣）。

    与 ``set_tray_base_source`` 同口径：落盘「跟随启动环境」是不写而非写空值，
    用户可以随时回到「由 BARKIT_LOG_LEVEL 环境变量 / 出厂档决定」。
    """
    user = tmp_path / "config.yaml"
    user.write_text("hotkeys:\n  recognize: ctrl+alt+o\n", encoding="utf-8")
    cfg = Config(user_path=user)

    cfg.set_log_level("debug")
    assert cfg.log_level() == "debug"
    raw = yaml.safe_load(user.read_text(encoding="utf-8"))
    assert raw["log"] == {"level": "debug"}

    cfg.set_log_level("")                                   # 跟随启动环境
    assert cfg.log_level() == ""
    raw = yaml.safe_load(user.read_text(encoding="utf-8"))
    assert "log" not in raw                                 # 默认值不落残渣
    assert raw["hotkeys"] == {"recognize": "ctrl+alt+o"}


def test_config_single_source_of_truth(tmp_path):
    """§9.5 单一真源：插件默认值来自 config_schema()，而非 config.default.yaml 插件段。

    - default yaml 不含 notify 段（只有 enabled，属框架保留键）→ 默认只能由 schema 提供
    - 用户差异层覆盖生效（优先级：user > schema > framework yaml）
    """
    default = tmp_path / "default.yaml"
    default.write_text(
        "schema_version: 1\n"
        "app:\n  editor: vscode\n"
        "plugins:\n  notify_guard:\n    enabled: true\n",
        encoding="utf-8",
    )
    user = tmp_path / "config.yaml"
    cfg = Config(
        user_path=user,
        default_path=default,
        plugin_schemas={"notify_guard": NOTIFY_SCHEMA},
    )
    # 默认来自 schema（yaml 无 notify 段）
    assert cfg.get("plugins.notify_guard.debounce") == 2
    assert cfg.get("plugins.notify_guard.rules") == []
    # enabled 属框架保留键，来自 yaml
    assert cfg.get("plugins.notify_guard.enabled") is True
    # 用户差异层覆盖生效
    user.write_text("plugins:\n  notify_guard:\n    debounce: 10\n", encoding="utf-8")
    import os
    os.utime(user, ns=(0, int(cfg._mtime * 1e9) + 1_000_000_000))
    cfg._check_mtime()
    assert cfg.get("plugins.notify_guard.debounce") == 10


# ---------- 组件显示位：用户层开关（契约规范 §2.4，决议 D4） ----------


def test_plugin_ui_visible_defaults_are_per_slot(tmp_path):
    """没改过＝按**该位的默认**（决议 D37），而不是一律可见。

    标题栏 / 列表行 / 托盘角标沿用「声明即出现」（它们是组件的**舞台**）；「快捷区域」
    **默认关** —— 它是快捷方式而不是入口：声明了详情入口的组件，行上本来就点得进
    （D36），底部再摆一枚只是加速度，摆哪几个由用户自己挑。
    """
    from barkit.core.config import PLUGIN_UI_SLOTS, PLUGIN_UI_SLOT_DEFAULT

    cfg = _config(tmp_path)
    # 四个显示位：面板内三处 + 菜单栏角标一处（契约 §2.4）。
    # ``action`` 排在 ``row`` 与 ``tray`` 之间 = 设置窗里芯片的呈现顺序。
    assert PLUGIN_UI_SLOTS == ("titlebar", "row", "action", "tray")
    assert PLUGIN_UI_SLOT_DEFAULT == {
        "titlebar": True, "row": True, "action": False, "tray": True,
    }
    for slot in PLUGIN_UI_SLOTS:
        assert cfg.plugin_ui_visible("route_guard", slot) is PLUGIN_UI_SLOT_DEFAULT[slot]


def test_plugin_ui_slot_defaults_cover_contract_slots():
    """默认表与显示位表**一一对应**。

    没有这条门禁，新增一个显示位却忘了在默认表里表态时，``slot_default_visible`` 的
    ``.get(slot, True)`` 会让它**静默按「默认可见」上线** —— 而「默认该不该摆出来」恰好
    是每次新增显示位时唯一要想清楚的那件事（D37 就是这么来的）。
    """
    from barkit.core.config import PLUGIN_UI_SLOTS, PLUGIN_UI_SLOT_DEFAULT

    assert set(PLUGIN_UI_SLOT_DEFAULT) == set(PLUGIN_UI_SLOTS)
    for slot in PLUGIN_UI_SLOTS:
        assert isinstance(PLUGIN_UI_SLOT_DEFAULT[slot], bool), slot


def test_set_plugin_ui_visible_roundtrip_leaves_no_residue(tmp_path):
    """落盘只发生在**偏离该位默认**时：关标题栏写 ``False``，改回默认（可见）删键；
    「快捷区域」反过来 —— 它默认关，**打开**才是要落盘的那一次。

    判据不是「``visible`` 为真 ⇒ 删键」（逐位不同的默认下这句话会反）：用户改回该位
    的默认后 YAML 一律干净，不留残渣。
    """
    cfg = _config(tmp_path)
    user = tmp_path / "config.yaml"

    cfg.set_plugin_ui_visible("route_guard", "row", False)
    assert cfg.plugin_ui_visible("route_guard", "row") is False
    raw = yaml.safe_load(user.read_text(encoding="utf-8"))
    assert raw["plugins"]["route_guard"]["ui"]["row"]["visible"] is False
    # 只动了 row 这一位：其余三位各按自己的默认（逐位独立）
    assert cfg.plugin_ui_visible("route_guard", "titlebar") is True
    assert cfg.plugin_ui_visible("route_guard", "action") is False
    assert cfg.plugin_ui_visible("route_guard", "tray") is True

    cfg.set_plugin_ui_visible("route_guard", "row", True)
    assert cfg.plugin_ui_visible("route_guard", "row") is True
    raw = yaml.safe_load(user.read_text(encoding="utf-8"))
    assert "plugins" not in raw, "改回默认值后仍留着空段（残渣）"

    # 默认关的那一位：打开 ⇒ 落 ``true``；关回默认 ⇒ 又删干净
    cfg.set_plugin_ui_visible("route_guard", "action", True)
    assert cfg.plugin_ui_visible("route_guard", "action") is True
    raw = yaml.safe_load(user.read_text(encoding="utf-8"))
    assert raw["plugins"]["route_guard"]["ui"]["action"]["visible"] is True

    cfg.set_plugin_ui_visible("route_guard", "action", False)
    assert cfg.plugin_ui_visible("route_guard", "action") is False
    raw = yaml.safe_load(user.read_text(encoding="utf-8"))
    assert "plugins" not in raw, "关回默认值后仍留着空段（残渣）"


def test_plugin_ui_visible_falls_back_to_the_slot_default_on_broken_values(tmp_path):
    """读侧遇坏值按**该位的默认**回落，不是一律「偏向可见」。

    多数位的默认是可见（宁可多露一处，也不静默把用户的可见性调低 —— 与
    ``tray_min_level``「非法值回 warn」同一口径）；「快捷区域」的默认是**关**，于是
    一处手滑的引号不会让一个用户没打算要的底部按钮长出来。两件事是同一条判据：
    **与默认一致**。
    """
    user = tmp_path / "config.yaml"
    user.write_text(
        "plugins:\n"
        "  route_guard:\n"
        "    ui:\n"
        "      row:\n"
        "        visible: 'no'\n"
        "      tray: []\n"
        "      action:\n"
        "        visible: 'yes'\n"
        "  hosts_guard:\n"
        "    ui: 3\n",
        encoding="utf-8",
    )
    cfg = Config(user_path=user)
    assert cfg.plugin_ui_visible("route_guard", "row") is True
    assert cfg.plugin_ui_visible("route_guard", "tray") is True
    assert cfg.plugin_ui_visible("route_guard", "action") is False, "坏值也按该位默认"
    assert cfg.plugin_ui_visible("hosts_guard", "row") is True
    assert cfg.plugin_ui_visible("no_such_plugin", "row") is True


def test_set_plugin_ui_visible_rejects_unknown_slot(tmp_path):
    """未知显示位在**写侧**报错：不静默落一个谁也读不到的键。"""
    cfg = _config(tmp_path)
    with pytest.raises(ValueError):
        cfg.set_plugin_ui_visible("route_guard", "toolbar", False)


# ── 默认配置里的节律键（D21 ＋ D27）─────────────────────────────────

def test_log_level_is_unset_in_the_shipped_config(tmp_path):
    """出厂配置**刻意不带** ``log.level`` —— 带上就会盖掉 ``BARKIT_LOG_LEVEL``。

    常驻实例与终端跑是两种场景：前者拿不到终端环境（launchd 拉起），只能靠落盘
    的配置；后者用完就走，靠环境变量更方便。要让后者成立，出厂就必须留空这一键，
    否则每个用户都会合并到一份 "info"，把环境变量反过来盖掉 —— 于是「临时开一次
    debug」在默认状态下永远无效。这条守的是那个「留空」。
    """
    raw = yaml.safe_load(Path(DEFAULT_CONFIG_PATH).read_text(encoding="utf-8"))
    assert "level" not in (raw.get("log") or {})
    assert _config(tmp_path).log_level() == ""


def test_log_level_reads_the_user_override(tmp_path):
    """用户写了才读到；值域不由这里判（交给 logging_setup.resolve_level 兜底）。"""
    (tmp_path / "config.yaml").write_text("log:\n  level: warning\n", encoding="utf-8")
    assert _config(tmp_path).log_level() == "warning"


def test_default_config_ships_no_framework_poll_for_components():
    """默认配置不得替组件给框架节拍 ``poll``（那是 D21 说的「第二真源」）。

    组件声明的 ``suggested_poll`` 才是框架节拍的默认值来源；默认配置再写一份，
    两处迟早分叉，而界面上看不出来（只是「多久刷一次」变得猜不到）。
    """
    raw = yaml.safe_load(Path(DEFAULT_CONFIG_PATH).read_text(encoding="utf-8"))
    shipped = {
        pid: ("poll" in (sec or {}))
        for pid, sec in (raw.get("plugins") or {}).items()
    }
    assert not any(shipped.values()), (
        "默认配置替这些组件写了框架节拍 poll（应由 suggested_poll 给默认值）："
        f"{[k for k, v in shipped.items() if v]}"
    )


def test_default_config_uses_split_business_keys(tmp_path):
    """D27 拆键后：两个组件的业务节律键各占一个键，都能从**有效配置**里读到。

    它们自 2026-09-20 起由插件 schema 给（``hosts_guard`` / ``route_guard`` 的
    ``config_schema()`` 是这两个值的真源，出厂文件只剩 ``enabled``）。这条断言的是
    合并后的有效配置，所以换真源不该改变判据 —— 300 / 30 与搬迁前一致。
    """
    cfg = _config(tmp_path)
    assert cfg.get("plugins.hosts_guard.resolve_poll") == 300
    assert cfg.get("plugins.route_guard.helper_poll") == 30
    assert cfg.get("plugins.route_guard.state_poll") is None   # D27 已判死的键


# ── §9.5 收口：默认值真源在插件（2026-09-20 全量迁移）────────────────


def test_plugin_defaults_need_schema_injection(tmp_path):
    """**契约**：插件业务键的默认值只由 ``config_schema()`` 提供，不注入就看不见。

    §9.5 的单一真源把出厂文件里的插件段清空（只留 ``enabled``）之后，「谁提供插件
    默认值」就只剩 schema 一条路。生产由 ``app_shell.assemble`` 在
    ``registry.load_all()`` 之后注入；测试由 ``conftest`` 的
    ``_inject_builtin_plugin_schemas`` 兜底。

    这条把该事实**写死**，免得将来有人看到「读不到默认值」就回出厂文件再补一份 ——
    那正是本批刚清掉的「两处默认值」。显式传 ``plugin_schemas={}`` 绕开兜底。
    """
    user = tmp_path / "config.yaml"
    user.write_text("plugins:\n  hosts_guard:\n    enabled: true\n", encoding="utf-8")
    cfg = Config(user_path=user, plugin_schemas={})
    # 不注入 ⇒ 该键不可见（不是「有默认值」，是「根本没有这个键」）
    assert cfg.get("plugins.hosts_guard.resolve_poll") is None
    # 而框架保留键仍由出厂文件提供 —— 这正是「出厂文件只留框架级」的落点
    assert cfg.get("plugins.hosts_guard.enabled") is True


def _business_keys_in_default_config(raw: dict) -> dict:
    """出厂配置各插件段里**除框架保留键外**的键（空 dict = 合规）。

    门禁本体物化成函数，才谈得上「植入违规 ⇒ 判得出来」的自证 ——
    恒绿的门禁比没有门禁更糟。
    """
    return {
        pid: sorted(k for k in (sec or {}) if k != "enabled")
        for pid, sec in (raw.get("plugins") or {}).items()
        if [k for k in (sec or {}) if k != "enabled"]
    }


def test_config_defaults_single_source():
    """出厂配置只留框架级默认：``plugins.<id>`` 段除 ``enabled`` 外**不许有业务键**。

    §9.5 的判据落到文件上就是这个 —— 插件业务配置的默认值真源是插件自己的
    ``config_schema()``，出厂文件再写一份就又是「两处默认值」（会漂移，而界面上
    看不出来）。这条替掉《主框架架构-工程与门禁》§10 清单里挂很久的那个「待」。

    守的是**文件内容**，不是运行时：谁把 ``resolve_poll: 300`` 加回出厂文件，这条就红。
    """
    raw = yaml.safe_load(Path(DEFAULT_CONFIG_PATH).read_text(encoding="utf-8"))
    offenders = _business_keys_in_default_config(raw)
    assert not offenders, (
        "出厂配置的插件段混进了业务键 —— 它们的真源是插件的 config_schema()："
        f"{offenders}"
    )


def test_single_source_gate_detects_planted_business_key():
    """门禁自证：往出厂配置**植入**一个业务键 ⇒ 门禁判得出来（防扫描面空转）。

    纯内存植入、不落盘：同一份真实出厂配置，先证现状干净，再证植得进去、报得出键名。
    """
    raw = yaml.safe_load(Path(DEFAULT_CONFIG_PATH).read_text(encoding="utf-8"))
    assert not _business_keys_in_default_config(raw)          # 现状：干净
    raw["plugins"]["hosts_guard"]["resolve_poll"] = 300        # 植入（内存）
    assert _business_keys_in_default_config(raw) == {"hosts_guard": ["resolve_poll"]}


# ── 框架节拍的**用户覆盖键**（D21：默认值归组件声明，覆盖归用户）────────────
#
# 键只有一个消费者（D27：框架调度器的 tick 间隔）；读写都走用户差异层 ——
# 「用户改没改过」这个信息只存在于那里（合并后的配置答不出来）。


def test_plugin_poll_reads_only_the_user_diff_layer(tmp_path):
    """没写过 → ``None``（默认值由组件 ``suggested_poll`` 给，不在这里编）。"""
    cfg = _config(tmp_path)
    assert cfg.plugin_poll("hosts_guard") is None

    cfg.set_plugin_poll("hosts_guard", 60)
    assert cfg.plugin_poll("hosts_guard") == 60.0
    assert cfg.user_plugin_section("hosts_guard")["poll"] == 60


def test_set_plugin_poll_none_removes_the_key(tmp_path):
    """恢复默认 = **删键**，不是写回默认值。

    留一个「与默认同值的覆盖」会让组件日后调整 ``suggested_poll`` 传不到用户手上
    （盘上钉着旧数字，YAML 里看不出区别）—— 与 ``set_plugin_ui_visible(True)`` 同一口径。
    """
    cfg = _config(tmp_path)
    cfg.set_plugin_poll("hosts_guard", 60)
    cfg.set_plugin_poll("hosts_guard", None)

    assert cfg.plugin_poll("hosts_guard") is None
    assert "poll" not in cfg.user_plugin_section("hosts_guard")
    assert cfg.get("plugins.hosts_guard.resolve_poll") == 300, "别人的键不受牵连"


@pytest.mark.parametrize("raw", ["一会儿", 0, -5, True])
def test_plugin_poll_ignores_garbage(tmp_path, raw):
    """写坏了（非数字 / 非正 / 布尔）一律按「没改过」处理。

    宁可回落组件建议值，也不静默拿一个用户没打算要的数字；布尔要显式排除
    （``True`` 是 ``int`` 子类，会被当成 1 混进来）。
    """
    cfg = _config(tmp_path)
    cfg.set_plugin_section("hosts_guard", {"poll": raw})
    assert cfg.plugin_poll("hosts_guard") is None


# ── 配置初始化：组件配置恢复默认（§9.5 的初始化入口，2026-09-18）─────────────
#
# 用户诉求：配置段里留着一个新版程序不认识的旧键时，**界面上看不见它** ⇒ 只能手改
# YAML。这一组钉住框架那个统一入口的语义边界：保留什么、清什么、什么时候不写盘。


def _dirty_config(tmp_path):
    """一份「被写脏了的」用户配置：旧版本的键 ＋ 用户自己的键 ＋ 框架写过的三位。"""
    user = tmp_path / "config.yaml"
    user.write_text(
        "plugins:\n"
        "  demo:\n"
        "    enabled: false\n"
        "    data: 1\n"
        "    rule_overrides:\n"
        "      office:\n"
        "        force_apply: true\n"
        "    poll: 60\n"
        "    ui:\n"
        "      row:\n"
        "        visible: false\n",
        encoding="utf-8",
    )
    return Config(user_path=user), user


def test_clearable_plugin_keys_previews_exactly_what_reset_removes(tmp_path):
    """确认框里那份清单与真被清掉的东西**同源**：预览与结果必须逐字相同。

    用户签的是一份具体清单，不是一句「确定恢复默认？」—— 而这一步不可撤销、不作备份。
    """
    cfg, _user = _dirty_config(tmp_path)

    keys = cfg.clearable_plugin_keys("demo")
    assert keys == ["data", "rule_overrides", "poll", "ui"], "顺序＝文件里的书写顺序"
    assert cfg.reset_plugin_section("demo") == keys


def test_reset_keeps_only_the_enable_switch(tmp_path):
    """恢复之后有一条好用的不变量：**该组件段里只剩下启用状态**。

    旧版本的键、用户自己的键、显示位、框架节拍一并清掉；「启用 / 停用」是运行门控
    而不是配置 —— 清掉它会让一个被用户停用的组件凭空启用。
    """
    cfg, user = _dirty_config(tmp_path)

    cfg.reset_plugin_section("demo")

    assert cfg.user_plugin_section("demo") == {"enabled": False}
    raw = yaml.safe_load(user.read_text(encoding="utf-8"))
    assert raw["plugins"]["demo"] == {"enabled": False}
    assert cfg.get("plugins.demo.data") is None, "清完即回落默认（这里本就没有默认）"


def test_reset_drops_the_section_when_only_resettable_keys_were_left(tmp_path):
    """段里连「启用」这一位都没有时整段删掉（连带空的 ``plugins``）—— 不留空壳残渣。"""
    cfg = _config(tmp_path)
    cfg.update_plugin_section("demo", lambda s: s.update({"data": 1}))

    assert cfg.reset_plugin_section("demo") == ["data"]
    raw = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert "plugins" not in raw


def test_reset_writes_nothing_when_there_is_nothing_to_clear(tmp_path):
    """没有可清的东西 ⇒ **不写盘**（返回空列表）。

    白写一遍会改 mtime，把文件监视那条热重载路无谓地叫醒一次；界面上也只会得到
    「本来就是默认配置」。
    """
    cfg = _config(tmp_path)
    user = tmp_path / "config.yaml"
    assert cfg.clearable_plugin_keys("demo") == []
    assert cfg.reset_plugin_section("demo") == []
    before = user.read_text(encoding="utf-8")

    cfg.set_plugin_enabled("demo", False)          # 只剩「启用」这一位（框架保留键）
    after_enable = user.read_text(encoding="utf-8")
    assert after_enable != before, "上一步确实写过（否则本用例下半段没验到东西）"
    assert cfg.clearable_plugin_keys("demo") == []
    assert cfg.reset_plugin_section("demo") == []
    assert user.read_text(encoding="utf-8") == after_enable


def test_reset_keeps_framework_owned_keys():
    """门禁：框架往 ``plugins.<id>`` 段里写的顶层字面量键，必须**逐个表态**。

    判据只有一条：每个键要么进 ``PLUGIN_SECTION_FRAMEWORK_KEYS``（恢复默认时保留），
    要么是恢复默认时清掉的那几位（``poll`` / ``ui`` / ``states``，与上面几条行为用例
    互为对照 —— 三者都是**用户偏好**，故随「恢复默认」一起清）。
    新增一条框架写入路径却没表态时本用例会红 —— 否则「保留还是清掉」就由「谁先写的」
    决定，而界面上看不出来。

    扫描面是**配置中心这一族**的写入点（AST，不是子串）：只看组件段（或它在本模块里
    的别名 ``existing``）上的字面量下标写入；``slot_cfg["visible"]`` 那种段内嵌套位
    不判（基名不同），``user["plugins"]`` 那类顶层键同理。

    ⚠️ 2026-10-03 拆包后扫描面从**单文件**改成**整族**：原先读
    ``Path(config_mod.__file__)`` 的正文，而拆包后那是 ``__init__.py``（只有再导出，
    一个写入点都没有）⇒ 这条门禁会**恒定地**报「键变了」而看不出真源已搬走。
    判据的对象是「配置中心往组件段里写的键」，从来不是某一个文件名。
    """
    import ast

    from barkit.core import config as config_mod
    from barkit.core.config import PLUGIN_SECTION_FRAMEWORK_KEYS

    family = sorted(p for p in Path(config_mod.__file__).parent.rglob("*.py")
                    if "__pycache__" not in p.parts)
    assert len(family) > 1, (
        f"配置中心这一族只收到 {len(family)} 个文件 ⇒ 扫描面退回单文件，"
        "而写入点早就搬进兄弟模块了"
    )
    written: set[str] = set()
    for path in family:
        src = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Assign):
                continue
            for tgt in node.targets:
                if not isinstance(tgt, ast.Subscript):
                    continue
                base, key = tgt.value, tgt.slice
                if (
                    isinstance(base, ast.Name)
                    and base.id in {"section", "existing"}
                    and isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                ):
                    written.add(key.value)

    assert written == set(PLUGIN_SECTION_FRAMEWORK_KEYS) | {"poll", "ui", "states"}, (
        "配置中心往组件段里写的键变了 —— 请给新键表态（进 "
        "PLUGIN_SECTION_FRAMEWORK_KEYS = 恢复默认时保留，或进本用例里的那份可清集合 "
        f"= 恢复默认时清掉）。实测到：{sorted(written)}"
    )


# ---------- 状态机优先级：plugins.<id>.states.<key>.priority ----------


def _cfg(tmp_path) -> Config:
    """一份干净的用户配置（文件尚不存在 ⇒ 读取侧按「什么都没写过」处理）。"""
    return Config(user_path=tmp_path / "config.yaml")


def test_state_priority_roundtrip(tmp_path):
    """写 → 读回来数值对，且**没写过的状态机不出现在结果里**。

    后半句是要紧的那半：调用方（``representative``）靠「在不在字典里」区分「用户配过」
    与「回落声明顺序」。这里若替没写过的机器填默认值，那个区分就没了 —— 用户日后
    调不动顺序正是从这里开始坏的。
    """
    cfg = _cfg(tmp_path)
    assert cfg.plugin_state_priorities("demo") == {}

    cfg.set_plugin_state_priority("demo", "display", 0)
    cfg.set_plugin_state_priority("demo", "notify", 2)
    assert cfg.plugin_state_priorities("demo") == {"display": 0, "notify": 2}


def test_state_priority_none_deletes_and_cleans_up(tmp_path):
    """``None`` = 回到声明顺序 ⇒ **删键**而不是写回默认数字。

    与 ``set_plugin_poll(None)`` 同一口径：默认值「声明顺序」会随作者增删状态机漂移，
    钉一个死数字在用户盘上，作者日后调整声明顺序就传不到用户手上。
    最后一台删掉后 ``states`` 整个键一并收拾干净，不留 ``states: {}`` 残渣。
    """
    cfg = _cfg(tmp_path)
    cfg.set_plugin_state_priority("demo", "a", 0)
    cfg.set_plugin_state_priority("demo", "b", 1)
    cfg.set_plugin_state_priority("demo", "a", None)
    assert cfg.plugin_state_priorities("demo") == {"b": 1}

    cfg.set_plugin_state_priority("demo", "b", None)
    assert cfg.plugin_state_priorities("demo") == {}
    assert "states" not in cfg.user_plugin_section("demo")


def test_state_priority_rejects_bad_values(tmp_path):
    """手改歪的 YAML（布尔 / 字符串）按「没配过」处理。

    宁可回落声明顺序，也不静默用一个用户没打算要的数字 —— 与 ``plugin_poll`` 读侧
    同口径。bool 是 int 的子类，``priority: true`` 正是那种「看着像写了」的写法。
    """
    user = tmp_path / "config.yaml"
    user.write_text(
        "plugins:\n"
        "  demo:\n"
        "    states:\n"
        "      a:\n"
        "        priority: true\n"
        "      b:\n"
        "        priority: 3\n"
        "      c:\n"
        "        priority: many\n",
        encoding="utf-8",
    )
    assert Config(user_path=user).plugin_state_priorities("demo") == {"b": 3}


def test_state_priority_is_cleared_by_reset(tmp_path):
    """「恢复默认配置」要清掉它 —— 优先级是**用户偏好**，不是框架门控（不像 enabled）。

    同时也是那边 35 个 behavioral 用例的对照：新写的用户态配置键，必须明确站到
    「可清」这一侧。
    """
    cfg = _cfg(tmp_path)
    cfg.set_plugin_enabled("demo", True)
    cfg.set_plugin_state_priority("demo", "a", 0)
    assert "states" in cfg.user_plugin_section("demo")

    assert cfg.reset_plugin_section("demo") == ["states"]
    assert cfg.plugin_state_priorities("demo") == {}
    # enabled 是运行门控，不该被「恢复默认」动掉
    assert cfg.user_plugin_section("demo") == {"enabled": True}


def test_state_priority_requires_a_non_empty_key(tmp_path):
    """空 key 当场报错：往 ``states.<空>`` 落一个谁也读不到的键，等于静默吞掉一次写入。"""
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError):
        cfg.set_plugin_state_priority("demo", "", 0)
