"""core/plugin_config：组件配置的 typed 门面（U2 ①）。

方案 → ``内部计划（配置接口统一）``。要守的三条：

1. **默认值只许有一处**：类型 / 默认值 / 取值域全部来自本组件的 ``config_schema()``，
   调用点只报键名（``test_plugins_do_not_read_their_section_by_hand`` 按 AST 守）；
2. **两类失败分开**：读/写了一个没声明的键、或写了一个不合声明的值 = **调用点**写错
   ⇒ 抛；用户手改 YAML 写歪 ⇒ 回落声明默认值（不让面板起不来）。两侧刻意不对称：
   用户手滑要宽待，调用点出错要早 fail；
3. **写回等于声明默认值即删键**（``Config.set_plugin_poll`` 同一口径）：用户 YAML 是
   差异层，留一个同值键等于把那个值钉死 —— 组件日后改缺省就传不到这位用户。写侧
   只经 ``self.cfg``（``set`` / ``update`` / ``mutate``），组件不碰 ``ctx.config``。

四条门禁各自带对照组（``*_scanner_catches_*``）：扫描器要能抓住**植入**的违规，
否则「零命中」可能只是它什么都没看。
"""

from __future__ import annotations

import ast
import importlib
import pathlib

import pytest

from barkit.core.config import Config
from barkit.core.plugin import Plugin, PluginContext
from barkit.core.plugin_config import PluginConfigError, normalize
from barkit.core.schema import FieldSpec, FieldType, FormSchema

REPO = pathlib.Path(__file__).resolve().parents[1]
PLUGINS_DIR = REPO / "src" / "barkit" / "plugins"
SCAFFOLD = REPO / "src" / "barkit" / "scaffold.py"


class _Probe(Plugin):
    """六种 ``FieldType`` 各一栏的探针组件（不参与发现器，只在本文件里用）。"""

    id = "probe"
    name = "探针"

    def config_schema(self) -> FormSchema:
        return FormSchema(
            plugin_id=self.id,
            version=1,
            fields=(
                FieldSpec(key="flag", label="开关", type=FieldType.BOOL, default=True),
                FieldSpec(key="count", label="次数", type=FieldType.NUMBER, default=2),
                FieldSpec(key="title", label="标题", type=FieldType.TEXT, default="t"),
                FieldSpec(key="mode", label="档位", type=FieldType.CHOICE, default="a",
                          choices=("a", "b")),
                FieldSpec(key="items", label="表", type=FieldType.LIST, default=[]),
                FieldSpec(key="blob", label="段", type=FieldType.DICT, default={"k": 1}),
            ),
        )


def _probe(tmp_path, section=None) -> _Probe:
    """带真实 Config 的探针（用户差异层可预置一段）。"""
    cfg = Config(user_path=tmp_path / "config.yaml")
    if section:
        cfg.set_plugin_section("probe", section)
    return _Probe(PluginContext(config=cfg))


# ---------- 取值：声明是唯一真源 ----------


def test_a_missing_key_falls_back_to_the_declared_default(tmp_path):
    """没写过 ⇒ 拿到 ``config_schema()`` 声明的默认值（调用点不再抄一份）。"""
    p = _probe(tmp_path)
    assert p.cfg.value("flag") is True
    assert p.cfg.value("count") == 2
    assert p.cfg.value("title") == "t"
    assert p.cfg.value("mode") == "a"
    assert p.cfg.value("items") == []
    assert p.cfg.value("blob") == {"k": 1}


def test_a_legal_user_value_wins(tmp_path):
    p = _probe(tmp_path, {"flag": False, "count": 9, "title": "zz", "mode": "b"})
    assert p.cfg.value("flag") is False
    assert p.cfg.value("count") == 9
    assert p.cfg.value("title") == "zz"
    assert p.cfg.value("mode") == "b"


@pytest.mark.parametrize("key, broken", [
    ("flag", "yes"),        # BOOL 收到字符串
    ("count", "abc"),       # NUMBER 收到字符串
    ("count", True),        # bool 是 int 子类：``count: true`` 是手改歪了，不算 1
    ("title", 3),           # TEXT 收到数字
    ("mode", "c"),          # CHOICE 落在取值域外
    ("items", "a,b"),       # LIST 收到字符串
    ("blob", ["k"]),        # DICT 收到列表
])
def test_a_hand_broken_value_falls_back_to_the_declared_default(tmp_path, key, broken):
    """用户手滑写歪 ⇒ 回落声明默认值，**不抛**（日志与面板都不该因此起不来）。

    这条与下面的「未声明键必抛」是同一条口径的两侧：作者写错早 fail，用户写歪宽待。
    """
    p = _probe(tmp_path, {key: broken})
    assert p.cfg.value(key) == p.cfg.default_of(key)


def test_a_null_value_means_unset_not_none(tmp_path):
    """``key:`` / ``key: null`` 与「没写过」同等看待（拿到声明默认值，不是 ``None``）。"""
    p = _probe(tmp_path, {"title": None, "count": None})
    assert p.cfg.value("title") == "t"
    assert p.cfg.value("count") == 2


def test_reading_an_undeclared_key_raises_and_names_the_plugin(tmp_path):
    """读了自己没声明的键 ⇒ 抛（作者写错）；文案里要有组件 id 与键名，便于直接去改。"""
    p = _probe(tmp_path)
    with pytest.raises(PluginConfigError) as e:
        p.cfg.value("nope")
    assert "probe" in str(e.value) and "nope" in str(e.value)


def test_framework_owned_keys_are_not_in_the_facade(tmp_path):
    """框架所有的键**不进** ``cfg``：``enabled`` 走 ``Plugin.enabled``，``poll`` 走调度器。

    它们各有唯一读侧（方案 §4）；把 ``enabled`` 也搬进来就会多出第二个读法，
    而两份读法迟早对不上。
    """
    p = _probe(tmp_path, {"enabled": True})
    with pytest.raises(PluginConfigError):
        p.cfg.value("enabled")
    assert p.enabled is True          # 框架那条路照旧


def test_default_of_raises_for_undeclared_and_returns_a_copy(tmp_path):
    """``default_of`` 的用处是「界面提示缺省是多少」——那种地方同样不许自己抄数字。"""
    p = _probe(tmp_path)
    assert p.cfg.default_of("items") == []
    with pytest.raises(PluginConfigError):
        p.cfg.default_of("nope")

    # 拿到的是副本：改它不该污染下一次取值（默认值里带 list / dict 时尤其要紧）
    got = p.cfg.default_of("blob")
    got["k"] = 999
    assert p.cfg.value("blob") == {"k": 1}
    assert p.cfg.value("items") is not p.cfg.value("items")


def test_section_is_the_same_data_as_plugin_config(tmp_path):
    """``section()`` 是整段口：与 ``plugin_config()`` 同一份（含框架键，若有）。

    要整段（交给校验器 / 导出包）时用它 —— 探针组件不在出厂文件里，故这一段只有
    用户层；真组件那一段还会有出厂文件里的 ``enabled``。
    """
    p = _probe(tmp_path, {"count": 9})
    assert p.cfg.section() == p.plugin_config() == {"count": 9}


def test_without_a_config_source_the_declared_defaults_still_apply(tmp_path):
    """没接线（``ctx`` 或 ``config`` 缺位）⇒ 声明默认值顶上，不是 ``AttributeError``。

    这条与 ``Plugin.enabled`` 的兜底口径一致：拿不到配置就按「没写过」办 ——
    接线问题不该表现成「组件读配置崩掉」。
    """
    p = _Probe(PluginContext(config=None))
    assert p.cfg.value("count") == 2
    assert p.cfg.value("flag") is True


def test_normalize_shares_the_validation_with_the_schema():
    """归一的判据就是 ``FieldSpec.check_value`` —— 本模块不另写一套类型判断。"""
    num = FieldSpec(key="n", label="n", type=FieldType.NUMBER, default=7)
    assert normalize(num, 3) == 3
    assert normalize(num, True) == 7          # bool 不算数字
    assert normalize(num, "3") == 7
    assert normalize(num, None) == 7
    assert normalize(num, object()) == 7


# ---------- 门禁一：插件侧不许再手取配置段 ----------


def _hand_reads(source: str) -> list[str]:
    """扫出一份源码里「取配置段 → ``.get(字面键)``」的位置（旧写法）。

    两种形态都算：① 直接 ``self.plugin_config().get("k", …)``；
    ② 在同一个函数里先 ``cfg = self.plugin_config()``（或 ``dict(...)``）再 ``cfg.get("k", …)``。

    **是代理判据**（按 AST 事实判，不是在运行期观测谁在读配置）：它认不出
    「经中间变量绕两跳」这类写法。配合下面那条「键必须有声明」的门禁一起用：
    绕过去的写法拿不到默认值，迟早会在别处露馅。
    """
    found: list[str] = []
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        aliases = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                if "plugin_config()" in ast.unparse(node.value):
                    aliases.update(t.id for t in node.targets if isinstance(t, ast.Name))
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "get" or not node.args:
                continue
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            obj = node.func.value
            if "plugin_config()" in ast.unparse(obj) or (
                isinstance(obj, ast.Name) and obj.id in aliases
            ):
                found.append(f"{fn.name}:{node.lineno}: {ast.unparse(node)}")
    return found


def test_plugins_do_not_read_their_section_by_hand():
    """插件侧**零命中**：取值一律走 ``self.cfg``，默认值不许在调用点再写一遍。

    为什么这条要紧：``.get(k, 兜底)`` 的兜底是**第二份真源**。把 schema 里的默认值
    改成 ``False``、代码里那句 ``True`` 继续生效，界面显示「已关」而行为是「开」——
    两处都合法，没有一条判据会红（方案 §2）。

    扫描面含 ``scaffold.py``：它是外部插件的**生成器**，它写出来的样板会被人照抄，
    所以它自己也必须用新写法。
    """
    offenders: list[str] = []
    for path in sorted(PLUGINS_DIR.rglob("*.py")) + [SCAFFOLD]:
        offenders += [f"{path.relative_to(REPO)} → {hit}"
                      for hit in _hand_reads(path.read_text(encoding="utf-8"))]
    assert not offenders, (
        "插件侧仍在自己取配置段并写兜底 —— 改用 self.cfg.value(键)：\n"
        + "\n".join(offenders)
    )


def test_the_hand_read_scanner_catches_a_planted_violation():
    """门禁自证：植入两类旧写法都必须被扫出来（否则「零命中」可能只是扫描器空转）。"""
    direct = 'def f(self):\n    return self.plugin_config().get("k", True)\n'
    via_alias = 'def g(self):\n    cfg = self.plugin_config()\n    return cfg.get("k", True)\n'
    clean = 'def h(self):\n    return self.cfg.value("k")\n'
    assert len(_hand_reads(direct)) == 1
    assert len(_hand_reads(via_alias)) == 1
    assert _hand_reads(clean) == []


# ---------- 门禁二：读的每个键都得先声明 ----------


def _facade_keys(source: str) -> list[str]:
    """扫出 ``self.cfg.value("k")`` / ``self.cfg.default_of("k")`` 上的字面键。"""
    keys: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in ("value", "default_of"):
            continue
        if not (isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "cfg"):
            continue
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            keys.append(node.args[0].value)
    return keys


def _declared(plugin_dir: pathlib.Path) -> set[str]:
    cls = getattr(importlib.import_module(f"barkit.plugins.{plugin_dir.name}.plugin"), "Plugin")
    return {f.key for f in cls.__new__(cls).config_schema().fields}


def test_every_key_a_plugin_reads_is_declared_in_its_schema():
    """每个 ``cfg.value(键)`` 的键都必须在该组件自己的 ``config_schema()`` 里。

    运行期由 ``PluginConfigError`` 兜着（读未声明的键当场抛），这条是**更早**的一道：
    静态就点出拼错的键名，不必等到那条路径被跑到。同时也把方案 §4 那条边界钉住 ——
    框架所有的键（``enabled`` / ``poll`` / ``ui.*`` / ``states.*``）不许出现在这里。
    """
    problems: list[str] = []
    checked = 0
    for d in sorted(p for p in PLUGINS_DIR.iterdir() if p.is_dir() and p.name[0] != "_"):
        declared = _declared(d)
        for path in sorted(d.rglob("*.py")):
            for key in _facade_keys(path.read_text(encoding="utf-8")):
                checked += 1
                if key not in declared:
                    problems.append(f"{d.name}: {key}（{path.name}）")
    assert checked > 0, "一个都没扫到 ⇒ 扫描面失效，不是「没有违规」"
    assert not problems, (
        "这些键被读了但没在 config_schema() 里声明（默认值与类型只能是空白）：\n"
        + "\n".join(problems)
    )


def test_the_declaration_scanner_catches_a_planted_misspelling():
    """门禁自证：拿**真出错的那份输入**试 —— 拼错的键名必须被点到。"""
    planted = 'def f(self):\n    return self.cfg.value("counnt")\n'
    assert _facade_keys(planted) == ["counnt"]
    # 控制组取一个**真实存在**的组件目录（批 5 起是 todo —— 原先指的 clock 已退场）。
    declared = _declared(PLUGINS_DIR / "todo")
    assert "counnt" not in declared          # 探针命中真实声明表之外
    assert "size" in declared                # 控制组：真键在


# ---------- 写回：等于声明默认值即删键 ----------


class _ProbeNextVersion(_Probe):
    """同一个组件、**缺省值改过**（模拟下个版本）：`count` 的缺省从 2 变成 5。"""

    def config_schema(self) -> FormSchema:
        return FormSchema(
            plugin_id=self.id,
            version=1,
            fields=(FieldSpec(key="count", label="次数", type=FieldType.NUMBER, default=5),),
        )


def test_writing_the_declared_default_removes_the_key(tmp_path):
    """写回一个**与声明默认值相同**的值 ⇒ 段里不留这个键。

    为什么不是「照着写一遍」：用户 YAML 是**差异层**（§9.5：默认值不落盘），
    留一个同值键会把那个值钉死 —— 组件日后把缺省从 2 改成 5，这位用户收不到
    （他并没有要求过「永远 2」）。同 ``Config.set_plugin_poll`` 写回默认值即删键。
    """
    p = _probe(tmp_path)
    p.cfg.set("count", 2)                     # 2 就是声明默认值
    assert p.cfg.section() == {}
    p.cfg.set("count", 9)
    assert p.cfg.section() == {"count": 9}
    p.cfg.set("count", 2)                     # 再拨回缺省 ⇒ 键消失（不是留一个 count: 2）
    assert p.cfg.section() == {}


def test_a_dropped_key_follows_a_later_change_of_the_default(tmp_path):
    """钉住上一条的**后果**：键删掉了，组件日后改缺省才传得到这位用户。

    这条是那条规则存在的**全部理由**的判据（只说「删键」不说为什么，下一个人会
    把它当洁癖删掉）。
    """
    cfg = Config(user_path=tmp_path / "config.yaml")
    _Probe(PluginContext(config=cfg)).cfg.set("count", 2)      # 老缺省
    later = _ProbeNextVersion(PluginContext(config=cfg))
    assert later.cfg.value("count") == 5, (
        "留着「count: 2」的话这里会读到 2 —— 用户被钉死在老缺省上，而他从没要求过这一条"
    )


def test_set_round_trips_through_the_user_file(tmp_path):
    p = _probe(tmp_path)
    p.cfg.set("title", "zz")
    assert p.cfg.value("title") == "zz"
    assert p.cfg.section() == {"title": "zz"}
    p.cfg.set("items", [{"a": 1}])
    assert p.cfg.value("items") == [{"a": 1}]


@pytest.mark.parametrize("key, bad", [
    ("mode", "c"),        # CHOICE 落在 choices 之外
    ("count", "abc"),     # NUMBER 收到字符串
    ("count", True),      # bool 不是数字（同 check_value 那条）
    ("flag", "yes"),      # BOOL 收到字符串
])
def test_set_refuses_a_value_outside_the_declaration(tmp_path, key, bad):
    """类型 / 取值范围不合声明 ⇒ 抛，且**一个字节都不落盘**。

    与读侧刻意相反：读侧遇到用户手改写歪要宽待（回落默认值，不让面板起不来），
    写侧的歪值是**调用点**给出的 —— 悄悄落一个用户没要的值比报错糟得多。
    """
    p = _probe(tmp_path)
    with pytest.raises(PluginConfigError) as e:
        p.cfg.set(key, bad)
    assert key in str(e.value) and "probe" in str(e.value)
    assert p.cfg.section() == {}, "校验没过 ⇒ 一个键都没写下去"


def test_set_on_an_undeclared_key_raises(tmp_path):
    """未声明的键（含 ``enabled`` 这类**框架键**）写不了：它们都不在本组件 schema 里。"""
    p = _probe(tmp_path)
    with pytest.raises(PluginConfigError) as e:
        p.cfg.set("nope", 1)
    assert "nope" in str(e.value)
    with pytest.raises(PluginConfigError):
        p.cfg.set("enabled", True)
    assert p.cfg.section() == {}


def test_update_validates_the_whole_patch_before_writing_anything(tmp_path):
    """多键写**先整批校验，再落盘**：半批写下去等于给用户留了半截状态。"""
    p = _probe(tmp_path)
    with pytest.raises(PluginConfigError):
        p.cfg.update({"count": 7, "mode": "c"})
    assert p.cfg.section() == {}, "有一项不合法 ⇒ 整批都不写（连合法的那一项也不写）"


def test_update_applies_the_drop_rule_per_key(tmp_path):
    """多键写里**逐个**判缺省：同值是缺省的那一栏不留键，另一栏照写。"""
    p = _probe(tmp_path)
    p.cfg.update({"count": 7, "flag": True, "title": "zz"})
    assert p.cfg.section() == {"count": 7, "title": "zz"}, "flag=True 是缺省 ⇒ 不留键"


def test_set_without_a_config_source_raises():
    """没接线（``ctx.config`` 缺位）：读照旧由缺省顶上，**写要抛** —— 没处落盘。

    「写了但没人接」必须出声：静默丢弃会让调用方以为存下了（这正是本仓库反复出现的
    「界面改了、盘上没动」）。
    """
    p = _Probe(PluginContext(config=None))
    assert p.cfg.value("count") == 2
    with pytest.raises(PluginConfigError):
        p.cfg.set("count", 7)


def test_mutate_hands_the_user_diff_section(tmp_path):
    """逃生舱：拿到的是**用户差异段**（不含合并进来的缺省值），改它就落盘。

    「不含缺省」这一条是它的用处所在 —— 若给的是合并后的段，写回会把默认值
    一并物化进用户 YAML（`recognize` 的 ``ocr`` 段就是这么被保护住的）。
    """
    p = _probe(tmp_path, {"blob": {"k": 1}})
    seen: dict = {}

    def _mutate(section: dict) -> None:
        seen["keys"] = sorted(section)
        section.setdefault("blob", {})["extra"] = 2

    p.cfg.mutate(_mutate)
    assert seen["keys"] == ["blob"], "段里只有用户写过的东西（缺省值不在里面）"
    assert p.cfg.section()["blob"] == {"k": 1, "extra": 2}


def test_mutate_persists_nothing_when_it_raises(tmp_path):
    """``mutate`` 抛 ⇒ 不落盘（否则给用户留的是半截状态）。"""
    p = _probe(tmp_path, {"count": 9})

    def _boom(section: dict) -> None:
        section["count"] = 1
        raise RuntimeError("拒绝")

    with pytest.raises(RuntimeError):
        p.cfg.mutate(_boom)
    assert p.cfg.value("count") == 9


# ---------- 门禁三：插件侧写配置只经门面 ----------


def _direct_section_writes(source: str) -> list[str]:
    """扫出「绕开门面、直接调 ``ctx.config.update_plugin_section``」的位置。

    `cfg.mutate` 是所有写入的落点（`set` / `update` 也走它），组件侧旁路它等于
    又开了一条没人知道的写入口。
    """
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "update_plugin_section":
            continue
        if "ctx" in ast.unparse(node.func.value):
            found.append(f"L{node.lineno}: {ast.unparse(node)[:70]}")
    return found


def test_plugins_write_config_only_through_the_facade():
    """插件侧**零命中**：写配置只经 ``self.cfg``，``ctx.config`` 在哪不归组件知道。

    扫描面含两个模板与 ``scaffold.py``：它们写出来的样板会被人照抄（同门禁一）。
    """
    offenders: list[str] = []
    for path in sorted(PLUGINS_DIR.rglob("*.py")) + [SCAFFOLD]:
        offenders += [f"{path.relative_to(REPO)} → {hit}"
                      for hit in _direct_section_writes(path.read_text(encoding="utf-8"))]
    assert not offenders, (
        "插件侧仍在直接调 ctx.config.update_plugin_section —— 改用 self.cfg.set / "
        "cfg.update / cfg.mutate：\n" + "\n".join(offenders)
    )


def test_the_direct_write_scanner_catches_a_planted_violation():
    """门禁自证：旁路写法必须被扫出来，「零命中」才不是扫描器空转。"""
    bad = 'def f(self):\n    self.ctx.config.update_plugin_section(self.id, m)\n'
    good = 'def g(self):\n    self.cfg.set("k", 1)\n'
    assert len(_direct_section_writes(bad)) == 1
    assert _direct_section_writes(good) == []


# ---------- 门禁四：写下去的每个键都得先声明 ----------


def _facade_write_keys(source: str) -> list[str]:
    """扫出「经门面写下去的**字面键**」，三种写法都算：

    ① ``self.cfg.set("k", …)``；
    ② ``self.cfg.update({"k": …})``；
    ③ ``self._write_keys({"k": …}, fail=…)`` —— ``route_guard`` 的转发口，它只是给
       ``cfg.update`` 套了一条「写完重算期望态」的尾巴。

    ③ 是**按名字认的代理判据**（方法一改名它就不认了），钉住这条的是
    ``test_the_write_key_scanner_covers_route_guards_forwarder``：改名即红。
    """
    keys: list[str] = []

    def _from_dict(node: ast.AST) -> None:
        if not isinstance(node, ast.Dict):
            return
        for k in node.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.append(k.value)

    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        attr = node.func.attr
        recv = node.func.value
        on_cfg = isinstance(recv, ast.Attribute) and recv.attr == "cfg"
        if on_cfg and attr == "set":
            if node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                keys.append(node.args[0].value)
        elif (on_cfg and attr == "update") or attr == "_write_keys":
            if node.args:
                _from_dict(node.args[0])
    return keys


def test_every_key_a_plugin_writes_is_declared_in_its_schema():
    """写下去的每个字面键都必须在该组件自己的 ``config_schema()`` 里。

    运行期由 ``cfg`` 兜着（未声明当场抛），这条是**更早**的一道：静态就点出拼错的
    键名，不必等到那条路径被跑到 —— 与读侧那条同理。
    """
    problems: list[str] = []
    checked = 0
    for d in sorted(p for p in PLUGINS_DIR.iterdir() if p.is_dir() and p.name[0] != "_"):
        declared = _declared(d)
        for path in sorted(d.rglob("*.py")):
            for key in _facade_write_keys(path.read_text(encoding="utf-8")):
                checked += 1
                if key not in declared:
                    problems.append(f"{d.name}: {key}（{path.name}）")
    assert checked > 0, "一个都没扫到 ⇒ 扫描面失效，不是「没有违规」"
    assert not problems, (
        "这些键被写了但没在 config_schema() 里声明（类型与取值域只能是空白）：\n"
        + "\n".join(problems)
    )


def test_the_write_key_scanner_covers_route_guards_forwarder():
    """③ 那条**按名字认**的判据的行程碑：route_guard 的转发口必须仍被扫到。

    它把七个写键都走在 ``_write_keys({...})`` 上（不走 ``self.cfg.update``），
    所以那个方法一改名，门禁四对 route_guard 就整片失效 —— 而「零违规」照样是绿的。
    这条就是防那件事：名字一改，这里红。
    """
    files = sorted((PLUGINS_DIR / "route_guard").rglob("*.py"))
    found = {k for p in files for k in _facade_write_keys(p.read_text(encoding="utf-8"))}
    assert {"paused", "helper_poll", "profiles", "pinned", "fallback",
            "handback_ifaces", "handback_dns"} <= found, (
        "route_guard 的写键没有全部被扫到 —— 若 ``_write_keys`` 改了名，"
        "门禁四的第三条形态就失效了（改门禁，别改这条）"
    )


def test_the_write_key_scanner_catches_a_planted_misspelling():
    """门禁自证：三种写法各植入一个拼错的键，都必须被点到。"""
    assert _facade_write_keys('def f(self):\n    self.cfg.set("counnt", 1)\n') == ["counnt"]
    assert _facade_write_keys(
        'def f(self):\n    self.cfg.update({"counnt": 1})\n') == ["counnt"]
    assert _facade_write_keys(
        'def f(self):\n    self._write_keys({"counnt": 1}, fail="x")\n') == ["counnt"]
    assert _facade_write_keys('def f(self):\n    self.cfg.value("count")\n') == [], \
        "读取的键不归这条门禁（门禁二管它），别重复计"
