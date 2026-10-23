"""`barkit new-plugin` 的用例（ROADMAP F2 ④ 外部插件脚手架）。

三层，缺一层都留着洞：

1. **纯函数层** —— id 词法、渲染出来的清单 / 代码文本（不落盘，逐条可指）；
2. **生成层** —— 落到哪儿、四条拒绝路径（不合规 id / 撞内建 id / 目录已存在 / 用法错），
   且拒绝时**一个文件都不落**；
3. **成品契约层** —— 生成物真经 ``discover_external`` 加载、过 ``ContributionRegistry``
   收集期门禁、实例化后声明面真能用。理由同《插件结构规范》§10.2 对模板冒烟的要求：
   **模板自己过不了的规则，教出来就是错的** —— 生成物自证过不了，这条命令就是在批量生产坏插件。

另有一条**行为门禁**（不是 AST）：挡住 PySide6 后子进程真跑一次。CLI 之路不许拉 Qt，
这条约束靠「文件里没写 import」证明不了（惰性 import 也一样会执行），只有真跑才作数。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from barkit import cli, paths, scaffold
from barkit.contribution.points import SUPPORTED_API_VERSION
from barkit.core.config import Config
from barkit.core.contributions import ContributionRegistry
from barkit.core.installer import validate_plugin
from barkit.core.loader import discover_external
from barkit.core.plugin import PluginContext

SRC = Path(__file__).resolve().parents[1] / "src"


def _generate(base: Path, plugin_id: str = "demo_ext", **kwargs) -> Path:
    """生成一套骨架并返回它的目录（用例统一入口，省得每处重复拼参数）。"""
    scaffold.scaffold(plugin_id, base_dir=base, **kwargs)
    return base / plugin_id


def _instantiate(base: Path, plugin_id: str, tmp_path: Path):
    """按 loader 的真实路径加载生成物并实例化（发现 → 取类 → 构造）。"""
    found, rejected = discover_external(base)
    assert rejected == [], f"生成物被 loader 拒了：{rejected}"
    cls = found[plugin_id]
    return cls(PluginContext(config=Config(user_path=tmp_path / "config.yaml"), events=None))


class _Link:
    """最小会话桩：事实住在**内存**里（取代旧的「后台写一份状态文件」）。"""

    def __init__(self) -> None:
        self.subscribed: list = []
        self._observed: dict = {}

    def subscribe(self, subjects):
        self.subscribed.append(list(subjects))
        return True

    def snapshot(self, subject, default=None):
        return self._observed.get(subject, default)

    def push(self, subject, observed) -> None:
        """模拟守护推来一份事实（组件侧 ``observed()`` 的读口）。"""
        self._observed[subject] = observed


# ──────────────────────────────────────────────
# 1. 纯函数层
# ──────────────────────────────────────────────


@pytest.mark.parametrize("plugin_id", ["demo_ext", "a", "barkit2", "x" * 32, "a_1_b"])
def test_validate_plugin_id_accepts(plugin_id):
    assert scaffold.validate_plugin_id(plugin_id) is None


@pytest.mark.parametrize(
    "plugin_id",
    ["", "Demo", "demo-ext", "demo.ext", "2demo", "_demo", "a" * 33, "示例", "demo ext"],
)
def test_validate_plugin_id_rejects_with_a_reason(plugin_id):
    """不合规一律拒绝，且**必须给原因** —— 只说「不行」的 CLI 等于猜谜。"""
    reason = scaffold.validate_plugin_id(plugin_id)
    assert reason, f"{plugin_id!r} 应被拒绝"
    assert "id" in reason


def test_rendered_manifest_carries_identity_and_contract_version():
    """清单里的 api_version 取自常量（单一真源），不是写死的字面量。"""
    text = scaffold.render_manifest("demo_ext", "示例插件", 77, SUPPORTED_API_VERSION)
    data = yaml.safe_load(text)
    assert data == {
        "id": "demo_ext",
        "name": "示例插件",
        "version": "0.1.0",
        "api_version": SUPPORTED_API_VERSION,
        "entry": "plugin:Plugin",
        "order": 77,
    }


def test_rendered_manifest_quotes_display_names_that_needed_it():
    """显示名带引号 / 冒号也能写回原值（YAML 转义不许让步给「名字好看」）。"""
    name = '带"引号"的: 名字'
    data = yaml.safe_load(scaffold.render_manifest("demo_ext", name, 90, SUPPORTED_API_VERSION))
    assert data["name"] == name


def test_id_is_substituted_everywhere_in_both_skeletons():
    """两套骨架里**不留占位符** —— 漏一处就是生成一个当场坏掉的插件。

    只断言「文本里没有 ``$``」不够：真正的判据是「id 出现在该出现的地方」。这里两件都查。
    """
    for daemon, expected, other in (
        (False, "demo_ext_action", "demo_ext_toggle"),
        (True, "demo_ext_toggle", "demo_ext_action"),
    ):
        code = scaffold.render_plugin_py("demo_ext", "示例", 90, SUPPORTED_API_VERSION,
                                        daemon=daemon, poll=scaffold.DEFAULT_POLL)
        assert "$" not in code, "渲染后仍有未替换的占位符"
        assert 'id = "demo_ext"' in code
        # 热键名带着 id（= 拼接那处占位符没漏），且两套骨架不串味（--daemon 真的换了模板）
        assert expected in code, f"热键名没带上 id：{expected}"
        assert other not in code, f"骨架里混进了另一套骨架的热键名：{other}"
        compile(code, f"<{'daemon' if daemon else 'action'}>", "exec")   # 真能编译


# ──────────────────────────────────────────────
# 2. 生成层
# ──────────────────────────────────────────────


def test_generates_into_the_external_plugins_dir_by_default(tmp_path):
    """不给 ``--dir`` 时落在 ``paths.EXTERNAL_PLUGINS_DIR``（＝loader 真会扫的那个目录）。

    这条同时钉住「路径必须调用期读模块属性」：conftest 是在**用例开始后**才补丁
    ``paths`` 的，按值导入会让本用例写进真实用户目录。
    """
    files = scaffold.scaffold("demo_ext")
    assert [p.name for p in files] == ["plugin.py", "manifest.yaml"]
    assert files[0].parent == paths.EXTERNAL_PLUGINS_DIR / "demo_ext"
    assert paths.EXTERNAL_PLUGINS_DIR.is_relative_to(tmp_path)


def test_refuses_plugin_id_already_taken_by_a_builtin(tmp_path):
    """撞内建 id 直接拒绝：两者在 registry 里是同一个键，同名外部包会**静默顶掉**内建的那个。"""
    with pytest.raises(scaffold.ScaffoldError) as exc:
        scaffold.scaffold("recognize", base_dir=tmp_path)
    assert "内建" in str(exc.value)
    assert not (tmp_path / "recognize").exists()


def test_refuses_existing_dir_and_leaves_it_untouched(tmp_path):
    """目标目录已存在即拒绝，**不改动里面的任何东西**（用户目录不猜、不合并、不覆盖）。"""
    target = tmp_path / "demo_ext"
    target.mkdir()
    keep = target / "plugin.py"
    keep.write_text("# 用户自己写的东西\n", encoding="utf-8")

    with pytest.raises(scaffold.ScaffoldError) as exc:
        scaffold.scaffold("demo_ext", base_dir=tmp_path)

    assert "已存在" in str(exc.value)
    assert keep.read_text(encoding="utf-8") == "# 用户自己写的东西\n"
    assert not (target / "manifest.yaml").exists()


def test_refusal_writes_nothing_at_all(tmp_path):
    """三条拒绝路径都不得留痕：半截目录会被发现器静默跳过，用户根本看不出发生过什么。"""
    base = tmp_path / "ext"          # 用子目录：tmp_path 根上有 conftest 的数据域
    base.mkdir()
    (base / "demo_ext").mkdir()      # 已存在 ⇒ 第三条拒绝
    for plugin_id in ("Demo", "recognize", "demo_ext"):
        with pytest.raises(scaffold.ScaffoldError):
            scaffold.scaffold(plugin_id, base_dir=base)
    assert sorted(p.name for p in base.iterdir()) == ["demo_ext"]
    assert list((base / "demo_ext").iterdir()) == [], "拒绝路径往已有目录里写了东西"


# ──────────────────────────────────────────────
# 3. 成品契约层：生成物必须真能加载、真过门禁
# ──────────────────────────────────────────────


def test_action_skeleton_loads_and_passes_collection_gates(tmp_path):
    """动作型骨架：loader 收得下、收集期门禁过得了、声明面真能用。"""
    base = tmp_path / "ext"
    _generate(base, "demo_ext", name="示例外部插件")
    plugin = _instantiate(base, "demo_ext", tmp_path)

    # manifest 是包元数据的权威：loader 强制对齐 id / order
    assert type(plugin).id == "demo_ext"
    assert type(plugin).order == scaffold.DEFAULT_ORDER
    assert type(plugin).api_version == SUPPORTED_API_VERSION
    assert validate_plugin(plugin) is None, "可插拔声明校验没过"

    reg = ContributionRegistry([plugin])
    assert reg.rejected == [], reg.rejected
    assert reg.failed == [], reg.failed
    assert len(reg.status_rows()) == 1
    assert len(reg.states()) == 1
    assert len(reg.components()) == 1

    # 取值闭集与首屏起点对得上（D13：报告只能报声明过的取值）
    spec = plugin.state_specs()[0]
    assert spec.initial_value in {opt.id for opt in spec.states}
    # 动作改状态后立即广播（不等下一拍），快照仍落在声明的取值里
    plugin.actions()[0].on_click()
    assert plugin._snapshot().state in {opt.id for opt in spec.states}


def test_action_skeleton_settings_page_builds(tmp_path, qtbot):
    """设置页必须能真实构建 —— 构建期 NameError 会一路漏到用户点开设置窗那一刻。"""
    base = tmp_path / "ext"
    _generate(base, "demo_ext")
    plugin = _instantiate(base, "demo_ext", tmp_path)

    page_spec = plugin.settings_page()
    assert page_spec.reason.strip(), "骨架必须示范 reason（§7.1 决议 2：自绘要说明理由）"
    widget = page_spec.build()
    assert widget is not None
    qtbot.addWidget(widget)


def test_daemon_skeleton_ticks_and_declares_its_resources(tmp_path):
    """守护型骨架：tick 报声明过的取值、资源声明合法、自动化开关真写回配置段。"""
    base = tmp_path / "ext"
    _generate(base, "demo_daemon", daemon=True)
    plugin = _instantiate(base, "demo_daemon", tmp_path)

    assert type(plugin).suggested_poll > 0, "周期组件必须给框架一个能用的建议节拍"
    assert validate_plugin(plugin) is None, validate_plugin(plugin)

    # 守护还没回报 ⇒ 走降级，不抛
    snap = plugin.tick()
    spec = plugin.state_specs()[0]
    assert snap.state in {opt.id for opt in spec.states}
    assert snap.updated_at > 0

    # 骨架必须示范「订阅哪个领域」—— 事实由守护经会话推来，不落盘
    assert plugin.subjects(), "守护型骨架必须示范订阅"

    # 守护推来一份事实 ⇒ 读得回来（取代旧的「往状态文件写一份」）
    link = _Link()
    plugin.ctx.helper = link
    link.push("demo_daemon", {"note": "守护已收敛"})
    assert plugin.tick().state == "converged"

    # 资源声明：**默认零文件**（新骨架要落盘须显式声明并答得出为什么）；
    # seed ⊆ data；config_section 是插件 id
    res = plugin.resources()
    assert res.data_files == [], "新骨架默认不落盘"
    assert set(res.seed_files) <= set(res.data_files)
    assert res.config_section == "demo_daemon"

    # 自动化开关（AutomationCapability）：徽标 + RMW 写用户配置段
    assert plugin.mode_badge_spec().label in {"自动", "手动"}
    plugin.set_automation_active(False)
    assert plugin.automation_active is False
    assert plugin.plugin_config().get("auto_enabled") is False, "开关没写回配置段"


def test_generated_plugins_declare_no_framework_poll_field(tmp_path):
    """生成物不得声明 ``poll`` 配置字段（D27）：那个键的用户入口归框架，组件只报建议节拍。"""
    base = tmp_path / "ext"
    _generate(base, "demo_daemon", daemon=True)
    plugin = _instantiate(base, "demo_daemon", tmp_path)
    assert [f.key for f in plugin.config_schema().fields] == ["auto_enabled"]


# ──────────────────────────────────────────────
# CLI 分派与「Qt-free」行为门禁
# ──────────────────────────────────────────────


def test_dispatch_routes_new_plugin(tmp_path, capsys):
    assert cli.dispatch(["new-plugin", "demo_ext", "--dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "已生成" in out and "manifest.yaml" in out
    assert (tmp_path / "demo_ext" / "manifest.yaml").exists()


def test_dispatch_new_plugin_help_and_usage_errors(capsys):
    assert cli.dispatch(["new-plugin", "--help"]) == 0
    assert "new-plugin" in capsys.readouterr().out
    assert cli.dispatch(["new-plugin"]) == 2          # 缺 id ⇒ argparse 用法错
    assert "barkit new-plugin" in cli.USAGE


def test_dispatch_new_plugin_reports_refusal_on_stdout(tmp_path, capsys):
    """拒绝走 stdout + 退出码 1（跟 doctor 的失败码同档），不打整段 traceback。"""
    (tmp_path / "demo_ext").mkdir()
    assert cli.dispatch(["new-plugin", "demo_ext", "--dir", str(tmp_path)]) == 1
    assert "未生成" in capsys.readouterr().out


#: 挡住 PySide6 / shiboken6 后跑一次脚手架（子进程做：本进程里 Qt 早进了 sys.modules）。
_MISSING_QT_PROBE = """
import builtins
import sys

_real = builtins.__import__


def _blocked(name, *args, **kwargs):
    if name.split('.')[0] in ('PySide6', 'shiboken6'):
        raise ImportError('blocked by test')
    return _real(name, *args, **kwargs)


builtins.__import__ = _blocked

from barkit import cli
code = cli.dispatch(['new-plugin', 'demo_ext', '--dir', sys.argv[1]])
print('exit=%d' % code)
"""


def test_new_plugin_scaffold_works_without_pyside6(tmp_path):
    """CLI 之路不许拉 Qt：界面依赖残缺的环境里，生成骨架照样要能跑。

    AST 门禁证明不了这件事（惰性 import 也一样会被执行），故用子进程真跑一次 —— 与
    `test_doctor_survives_missing_pyside6` 同一手法。
    """
    proc = subprocess.run(
        [sys.executable, "-c", _MISSING_QT_PROBE, str(tmp_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
        env={**os.environ, "PYTHONPATH": str(SRC), "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode == 0, f"脚手架在无 Qt 环境里崩了：\n{proc.stderr}"
    assert "exit=0" in proc.stdout, proc.stdout
    assert (tmp_path / "demo_ext" / "manifest.yaml").exists()
