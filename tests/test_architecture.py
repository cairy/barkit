"""架构门禁（批次 0 子集，方案 v3 §10 待建清单的落地起点）。

只放当前架构已成立的不变量；批次 1+ 随迁移逐步补齐其余条目
（渲染器复用 / 回调单次触发 / helper 安全 / 可插拔等）。
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "barkit"


def _is_type_checking(node: ast.If) -> bool:
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _import_targets(path: Path) -> list[tuple[str, bool]]:
    """AST 扫描一个文件的全部 import 目标。

    返回 (module, guarded) 元组：guarded=True 表示该 import 位于
    ``if TYPE_CHECKING:`` 块内（运行期不生效，方案 §5.2 允许）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    results: list[tuple[str, bool]] = []

    def _walk(node: ast.AST, guarded: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and _is_type_checking(child):
                _walk(child, True)
                continue
            if isinstance(child, ast.Import):
                results.extend((a.name, guarded) for a in child.names)
            elif isinstance(child, ast.ImportFrom) and child.module:
                results.append((child.module, guarded))
            else:
                _walk(child, guarded)

    _walk(tree, False)
    return results


def _module_level_imports(path: Path) -> list[tuple[str, bool]]:
    """只扫**模块顶层**的 import —— 函数体内的延迟导入不算。

    与 :func:`_import_targets` 的区别是实质性的：延迟导入正是「让某个重层依赖
    只在需要时才生效」的标准手法（doctor 的 ``survey.discover`` 就这么做）。
    拿全文件扫描去守「顶层不得依赖重层」会把它误判成违规。

    **相对导入（``from . import x`` / ``from .consts import y``）一律不返回**：它必然
    指向本包内部，不是外部依赖。这不只是省事 —— ``from .consts import …`` 的 AST 里
    ``module == "consts"``（**那个点记在 ``level`` 上**），不特判就会把包内模块名
    （``consts`` / ``safeio`` / ``state``）当成第三方库报出去。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    results: list[tuple[str, bool]] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            results.extend((a.name, False) for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            results.append((node.module, False))
        elif isinstance(node, ast.If) and _is_type_checking(node):
            for inner in node.body:
                if isinstance(inner, ast.Import):
                    results.extend((a.name, True) for a in inner.names)
                elif isinstance(inner, ast.ImportFrom) and inner.module and not inner.level:
                    results.append((inner.module, True))
    return results


def _py_files(*parts: str) -> list[Path]:
    return sorted((SRC.joinpath(*parts)).rglob("*.py"))


# 已知存量违规：登记在册者不 fail，但每条必须注明收口批次，新条目一律需评审。
# 批次 7 已清零：
# - core/plugin_manager.py import barkit.shell.panel：仅 TYPE_CHECKING 类型标注，
#   门禁此前误把类型期导入计入运行期依赖（_guarded 算出却未使用），改为跳过 guarded；
# - core/registry.py import barkit.plugins：发现器改为按名动态导入
#   （``importlib.import_module``），静态耦合消除。剩余的「按名加载」是插件
#   发现器的固有职责：子模块定位必须走父包 __path__，无法改为纯路径枚举。
KNOWN_VIOLATIONS: dict[Path, set[str]] = {}


# ──────────────────────────────────────────────
# 分层与依赖方向（§3.2）
# ──────────────────────────────────────────────

def test_layer_dependency_direction_core():
    """L1 core 不得 import 上层（shell/ui、plugins、capabilities）。

    已知存量违规须逐条登记在 KNOWN_VIOLATIONS 并注明收口批次，
    未登记的新违规一律 fail。
    """
    banned = ("barkit.shell", "barkit.plugins", "barkit.capabilities")
    for path in _py_files("core"):
        for target, guarded in _import_targets(path):
            # TYPE_CHECKING 块内的导入运行期不生效（§5.2 允许），不计入分层违规
            if guarded or not target.startswith(banned):
                continue
            allowed = KNOWN_VIOLATIONS.get(path, set())
            assert target in allowed, f"{path} 违规依赖 {target}（未登记白名单）"


#: 「谁不得 import 谁」—— 补 :func:`test_layer_dependency_direction_core` 之外的层（§3.2）。
#:
#: **为什么 `plugins` 不在表里**：L5 是最上层，可以依赖任意下层 —— `plugins/*/settings.py`
#: import `shell.ui_kit` / `shell.daemon_ops` 正是设置页三组件定式的复用手段（D19），不是违规。
#: 拿「层号」当规则去禁它，会一口气判红 6 个文件，然后被逼出一张豁免表（＝第二份规范）。
#: **`contribution` 也不在**：它由 :func:`test_contribution_depends_only_on_stdlib_and_self`
#: 守得更严（只准依赖自己）。**`doctor` 也不在**：它是叶子、方向相反，
#: 由 :func:`test_doctor_is_not_imported_from_below` 守。
UPPER_LAYER_BANS: dict[str, tuple[str, ...]] = {
    "capabilities": ("barkit.shell", "barkit.plugins"),   # L2 不得依赖 L3 / L5
    "shell": ("barkit.plugins",),                        # L3 不得依赖 L5
}


def _upper_layer_direction_hits(root: Path) -> list[str]:
    hits: list[str] = []
    for layer, banned in UPPER_LAYER_BANS.items():
        for path in sorted((root / layer).rglob("*.py")):
            for target, guarded in _import_targets(path):
                if guarded or not target.startswith(banned):
                    continue
                hits.append(f"{path.relative_to(root).as_posix()} → {target}")
    return hits


def test_layer_dependency_direction_upper_layers():
    """L2 / L3 不得 import 上层（§3.2），与 `_core` 那条合起来才是「全量」。

    `_core` 只守 L1；这一条补 L2（capabilities 不得依赖 shell / plugins）与 L3
    （shell 不得依赖 plugins）。**不是新规矩** —— §3.2 早写着「下层禁止 import 上层」，
    缺的只是把剩下那些层也纳入扫描面（`tests/test_architecture.py:92` 的 docstring 一直
    自陈是「core 那一半」）。

    `_core` **刻意不改名**：它被 `src/` 里 5 处注释当指路牌引用（`core/installer.py`、
    `core/helper_tasks.py`、`capabilities/helper_client.py`、`capabilities/helper/__init__.py`、
    `capabilities/helper/rootbin.py`），合并成一个名字会让那 5 处变成死引用。
    """
    hits = _upper_layer_direction_hits(SRC)
    assert not hits, (
        "分层违规（下层 import 了上层）：\n" + "\n".join(f"  {h}" for h in hits)
    )


def test_upper_layer_direction_gate_detects_planted_violation(tmp_path):
    """自证：植入违规必抓（口径见 docs/guides/验证与自证.md）。"""
    (tmp_path / "capabilities").mkdir()
    (tmp_path / "capabilities" / "bad.py").write_text(
        "from barkit.shell import styles\n", encoding="utf-8"
    )
    (tmp_path / "shell").mkdir()
    (tmp_path / "shell" / "bad.py").write_text(
        "from barkit.plugins.recognize import plugin\n", encoding="utf-8"
    )
    hits = _upper_layer_direction_hits(tmp_path)
    assert len(hits) == 2, hits


def test_upper_layer_direction_gate_passes_on_compliant_sample(tmp_path):
    """自证：合规写法不误报。两个易误判面各来一条 ——

    ① TYPE_CHECKING 块内的上层导入不算运行期依赖（§5.2）；
    ② `plugins → shell` 是**允许**方向（设置页复用 shell 控件，D19），不得判红。
    """
    (tmp_path / "plugins" / "demo").mkdir(parents=True)
    (tmp_path / "plugins" / "demo" / "settings.py").write_text(
        "from barkit.shell import ui_kit\n", encoding="utf-8"
    )
    (tmp_path / "capabilities").mkdir()
    (tmp_path / "capabilities" / "ok.py").write_text(
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from barkit.shell.panel import StatusPanel\n",
        encoding="utf-8",
    )
    assert _upper_layer_direction_hits(tmp_path) == []


# ──────────────────────────────────────────────
# plugins 不得从框架各层导入「私有名」
# ──────────────────────────────────────────────

def _private_name_import_hits(root: Path) -> list[str]:
    """扫 ``plugins/`` 里从框架各层导入**下划线开头名字**的 import，返回 ``文件 → 名字``。

    为什么单独一条：``UPPER_LAYER_BANS`` 的注释里写明「``plugins → shell`` 是允许的」
    （设置页复用 ``shell.ui_kit``，D19），于是**整个 plugins 方向不受约束**。允许复用
    公开控件是对的；顺手把 ``shell.overlay._TOASTS`` 这类**模块级内部状态**也拿过去是
    另一回事 —— 那一层的内部结构一改，组件静默改坏，没有任何一道闸会说。私有名
    （下划线前缀）正是「这块不对外」的标记，拿它当判据只需要一条规则。

    只管**绝对导入**且目标在 ``barkit`` 下的非 ``plugins`` 包：相对导入（``node.level``）
    是本包内部的事；跨插件导入（``barkit.plugins.*``）另有纪律，不在这条里。
    """
    hits: list[str] = []
    for path in sorted((root / "plugins").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
                continue
            target = node.module
            if not target.startswith("barkit.") or target.startswith("barkit.plugins"):
                continue
            for alias in node.names:
                if alias.name.startswith("_"):
                    hits.append(
                        f"{path.relative_to(root).as_posix()} → {target}.{alias.name}"
                    )
    return hits


def test_plugins_do_not_import_framework_private_names():
    """组件不得从框架各层拿私有名（§3.2 的补丁：那个方向此前完全没有约束）。

    存量实例：``plugins/todo/clock_view.py`` 原先直接 import ``shell.overlay._TOASTS`` /
    ``_DOCKED`` 并原地遍历 —— 那不是复用控件，是伸进另一层的模块级状态。该层已有公开
    读口（``live_toasts()`` / ``live_docked()``），走它即可。
    """
    hits = _private_name_import_hits(SRC)
    assert not hits, (
        "组件导入了框架的私有名（该层不对外的那部分）：\n"
        + "\n".join(f"  {h}" for h in hits)
    )


def test_private_name_import_gate_detects_planted_violation(tmp_path):
    """自证：植入违规必抓（口径见 docs/guides/验证与自证.md）。"""
    (tmp_path / "plugins" / "demo").mkdir(parents=True)
    (tmp_path / "plugins" / "demo" / "bad.py").write_text(
        "from barkit.shell.overlay import _TOASTS\n", encoding="utf-8"
    )
    hits = _private_name_import_hits(tmp_path)
    assert len(hits) == 1, hits


def test_private_name_import_gate_passes_on_compliant_sample(tmp_path):
    """自证：合规写法不误报。三个易误判面各来一条 ——

    ① 公开读口（``live_docked``）是正当复用，不得判红；
    ② 相对导入（``from .sub import _helper``）是本包内部的事；
    ③ 跨插件导入（``from barkit.plugins.other import _x``）不在这条门禁的面上。
    """
    pkg = tmp_path / "plugins" / "demo"
    pkg.mkdir(parents=True)
    (pkg / "ok.py").write_text(
        "from barkit.shell.overlay import live_docked, overlay_alive\n"
        "from .sub import _helper\n"
        "from barkit.plugins.other import _x\n",
        encoding="utf-8",
    )
    assert _private_name_import_hits(tmp_path) == []


# ──────────────────────────────────────────────
# L2 capabilities 的「精简」要求（§3.1 反面清单）
# ──────────────────────────────────────────────

def _capabilities_foreign_imports(root: Path) -> list[str]:
    """扫 `capabilities/` 全树的**模块级**第三方 import，返回 ``路径 → 模块``。

    只算模块顶层（`_module_level_imports`）：函数体内的延迟导入正是「重依赖按需才生效」
    的标准手法，拿全文件扫描去守会把它误判成违规。
    """
    out: list[str] = []
    for path in sorted((root / "capabilities").rglob("*.py")):
        for target, guarded in _module_level_imports(path):
            if guarded:
                continue
            if target.startswith("barkit") or target.split(".")[0] in sys.stdlib_module_names:
                continue
            out.append(f"{path.relative_to(root).as_posix()} → {target}")
    return out


def test_capabilities_lazy():
    """L2 capabilities 的模块级依赖只有标准库与框架自身，零第三方（§3.1）。

    §3.1 的反面清单写得直白：`NetworkRoute` / `DnsResolver` / `UsbTunnel` / `ScreenCapture` /
    `TextInput` / `NotificationObserver` 这些「没装任何插件时也要 import 一堆模块」的框架
    服务**不再内置**。判据就是这一条 —— capabilities 里冒出第三方 import（PySide6 也算），
    说明又有插件业务渗回了横切层。

    扫描面是**整棵** `capabilities/`（含 `helper/` 子包）：root 侧原语同样是零依赖要求 ——
    helper 由一个尽量小的 root 守护进程执行，多一个依赖多一份安装难题（那条另有
    `test_helper_stdlib_only` 从 helper 自己的口径守）。
    """
    hits = _capabilities_foreign_imports(SRC)
    assert not hits, (
        "capabilities 出现第三方依赖（§3.1：框架只提供插件自己干不了的能力，其余留在插件内）：\n"
        + "\n".join(f"  {h}" for h in hits)
    )


def test_capabilities_import_pulls_no_heavy_dependency():
    """运行期佐证：import 完整棵 capabilities 后 `sys.modules` 里不得有 Qt / macOS 桥。

    静态门禁看不住两种写法：`importlib.import_module("PySide6…")` 这种按名动态导入，
    以及把 import 包进 `try/except ImportError` 里。这条在子进程里真 import 一遍再查
    `sys.modules`，与 `test_no_pyside6_in_contribution_at_import_time` 同手法。

    **Windows 上只有 PySide6 那半边有判别力**：`objc` / `AppKit` / `Quartz` / `ocrmac`
    本机没装，它们缺席可能只是「装不上」而非「没 import」—— 那半边属真机验证面。
    """
    code = (
        "import sys;"
        "import barkit.capabilities.automation, barkit.capabilities.helper_client;"
        "heavy=[m for m in sys.modules"
        " if m.split('.')[0] in {'PySide6','shiboken6','objc','AppKit','Quartz','ocrmac'}];"
        "assert not heavy, heavy"
    )
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_production_code_has_no_pyflakes_warnings():
    """生产代码（`src/` ＋ `scripts/`）零 pyflakes 告警 —— 基线 2026-10-05 清零后上门禁。

    **为什么值得一条门禁**：pyflakes 那份 24 条的历史基线里混着两档东西 ——
    无害的「imported but unused」，与**真会炸的 `undefined name`**。2026-10-05 实测：
    `shell/settings/framework.py` 里「恢复默认配置」按钮的异常回执引用了不存在的
    `pid`（两个 lambda 互为兄弟，读不到对方的形参）—— 只在回调抛异常那一瞬炸、
    界面上无声，而那正是上一批「设置页按钮收口」自己引入的。基线不清零，这类告警
    就会被另外二十几条噪音一起淹掉，谁也不会去数。

    只守 `src/` 与 `scripts/`，**不含 `tests/`**：测试里「为副作用而 import」是合法写法，
    硬拦会逼人绕道。`tests/` 那份同日也清零了，但只作现状记录。
    """
    repo = Path(__file__).resolve().parents[1]
    res = subprocess.run(
        [sys.executable, "-m", "pyflakes", str(repo / "src"), str(repo / "scripts")],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"生产代码有 pyflakes 告警：\n{res.stdout}{res.stderr}"


def test_pyflakes_gate_detects_planted_undefined_name(tmp_path):
    """门禁自证：本仓真出过的那类笔误必须被抓（否则这条门禁是恒真的）。

    控制组用**真出错那份输入**：`return pid` —— 与上面那条用例里记的
    `framework.py` 同名同形（一个自由名，谁也没为它提供绑定）。
    """
    bad = tmp_path / "bad.py"
    bad.write_text("def f():\n    return pid\n", encoding="utf-8")
    res = subprocess.run(
        [sys.executable, "-m", "pyflakes", str(bad)],
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0, "pyflakes 放过了未定义名 ⇒ 这条门禁是恒真的"
    assert "undefined name 'pid'" in res.stdout, res.stdout


def test_capabilities_lazy_gate_detects_planted_violation(tmp_path):
    """自证：模块级的第三方 import 必被抓。"""
    (tmp_path / "capabilities").mkdir()
    (tmp_path / "capabilities" / "bad.py").write_text(
        "from PySide6.QtWidgets import QWidget\n", encoding="utf-8"
    )
    assert _capabilities_foreign_imports(tmp_path) != []


def test_capabilities_lazy_gate_passes_on_compliant_sample(tmp_path):
    """自证：标准库 ＋ 框架自身 ＋ 相对导入 ＋ 延迟导入都不得判红。"""
    (tmp_path / "capabilities" / "helper").mkdir(parents=True)
    (tmp_path / "capabilities" / "helper" / "ok.py").write_text(
        "from __future__ import annotations\n"
        "import json\n"
        "from pathlib import Path\n"
        "from barkit.core.proc import run_cmd\n"
        "from . import specs\n",
        encoding="utf-8",
    )
    (tmp_path / "capabilities" / "lazy.py").write_text(
        "def f():\n"
        "    import ocrmac\n"
        "    return ocrmac\n",
        encoding="utf-8",
    )
    assert _capabilities_foreign_imports(tmp_path) == []


def test_contribution_depends_only_on_stdlib_and_self():
    """C 契约层只依赖标准库与自身；PySide6 仅允许出现在 TYPE_CHECKING 块内。"""
    for path in _py_files("contribution"):
        for target, guarded in _import_targets(path):
            if target == "barkit" or target.startswith("barkit."):
                assert target.startswith("barkit.contribution"), (
                    f"{path} 违规依赖 {target}"
                )
            elif target.split(".")[0] in {"PySide6", "shiboken6"}:
                assert guarded, f"{path} 运行期违规依赖 {target}（仅限 TYPE_CHECKING）"
            else:
                assert target != "pytest", f"{path} 违规依赖 pytest"


def test_no_pyside6_in_contribution_at_import_time():
    """运行期验证：import contribution 后 sys.modules 不得出现 PySide6。"""
    code = (
        "import sys; import barkit.contribution; "
        "assert not any(m.startswith('PySide6') for m in sys.modules)"
    )
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_tokens_module_is_qt_free():
    """tokens 是纯数据真源，必须无 Qt 依赖（可在无显示环境下安全加载）。"""
    for path in _py_files("shell", "design"):
        if path.name == "tokens.py":
            for target, _guarded in _import_targets(path):
                assert not target.startswith("PySide6"), f"{path} 依赖 {target}"


def test_new_packages_do_not_import_plugins():
    """contribution / shell/design 不得感知具体插件（Shell 不得感知插件 id，§3.2）。"""
    for path in [*_py_files("contribution"), *_py_files("shell", "design")]:
        for target, _guarded in _import_targets(path):
            assert not target.startswith("barkit.plugins"), f"{path} 违规依赖 {target}"


def test_app_entry_stays_slim():
    """入口只做进程级准备，接线在 shell/app_shell（方案批次 7 验收标准）。

    这条是「上帝接线」不再复辟的护栏：新增 UI 位置应改装配器，而不是往入口堆代码。
    """
    entry = SRC / "app.py"
    assert entry.exists(), "app.py 不应被移走，它是唯一入口"
    lines = [
        ln for ln in entry.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert len(lines) <= 80, f"app.py 已有 {len(lines)} 行有效代码（上限 80）"


# ──────────────────────────────────────────────
# `barkit doctor` 的两条边界（见 docs/log/2026-09-17-barkit-doctor.md §4）
#
# 为什么 doctor 要单独守：它是**在环境坏掉时**才被用到的工具，所以它的入口链
# 恰恰不能依赖那些会坏的东西。两条边界都是「方向性」的，靠自觉守不住。
# ──────────────────────────────────────────────

#: doctor 入口链上必须零运行期 PySide6 的模块（含 CLI 分派）。
#: 「PySide6 装没装」本身就是 doctor 的 A3 检查项 —— 引擎依赖它就没法报出来。
DOCTOR_QT_FREE = (
    "cli.py",
    "doctor/__init__.py",
    "doctor/model.py",
    "doctor/probes.py",
    "doctor/render.py",
)

#: doctor 是**叶子**：它依赖 core / capabilities，不许被它们反向依赖。
#: （shell 不在名单里 —— 设置窗将来加「运行自检」入口要复用同一份 Report。）
DOCTOR_LEAF_LAYERS = ("core", "plugins", "capabilities", "contribution")


def _doctor_qt_free_hits(src_root: Path) -> list[str]:
    hits = []
    for rel in DOCTOR_QT_FREE:
        path = src_root / rel
        if not path.exists():
            continue
        for target, guarded in _import_targets(path):
            if not guarded and target.split(".")[0] in {"PySide6", "shiboken6"}:
                hits.append(f"{rel} → {target}")
    return hits


def _doctor_imported_from_below_hits(src_root: Path) -> list[str]:
    hits = []
    for layer in DOCTOR_LEAF_LAYERS:
        for path in sorted((src_root / layer).rglob("*.py")):
            for target, _guarded in _import_targets(path):
                if target.startswith("barkit.doctor"):
                    hits.append(f"{path.relative_to(src_root)} → {target}")
    return hits


def test_doctor_engine_is_qt_free():
    """doctor 入口链运行期零 PySide6 —— 否则「PySide6 缺失」这条永远报不出来。"""
    hits = _doctor_qt_free_hits(SRC)
    assert not hits, (
        "doctor 入口链出现运行期 PySide6 依赖：\n" + "\n".join(hits)
        + "\n（需要 Qt 的检查请放进 checks_*.py，由 doctor 惰性 import）"
    )


def test_doctor_qt_free_gate_detects_planted_violation(tmp_path):
    """植入违规必抓（门禁自证，口径见 docs/guides/验证与自证.md）。"""
    (tmp_path / "doctor").mkdir()
    (tmp_path / "doctor" / "probes.py").write_text(
        "from PySide6.QtWidgets import QMessageBox\n", encoding="utf-8"
    )
    assert _doctor_qt_free_hits(tmp_path)


def test_doctor_qt_free_gate_passes_on_compliant_sample(tmp_path):
    """合规写法不误报：TYPE_CHECKING 块内的 PySide6 不算运行期依赖。"""
    (tmp_path / "doctor").mkdir()
    (tmp_path / "doctor" / "probes.py").write_text(
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "from barkit import paths\n"
        "if TYPE_CHECKING:\n"
        "    from PySide6.QtWidgets import QWidget\n",
        encoding="utf-8",
    )
    assert _doctor_qt_free_hits(tmp_path) == []


def test_doctor_is_not_imported_from_below():
    """doctor 是叶子：core / plugins / capabilities / contribution 不得反向依赖它。"""
    hits = _doctor_imported_from_below_hits(SRC)
    assert not hits, "下层反向依赖 barkit.doctor：\n" + "\n".join(hits)


def test_doctor_leaf_gate_detects_planted_violation(tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "somewhere.py").write_text(
        "from barkit.doctor import collect\n", encoding="utf-8"
    )
    assert _doctor_imported_from_below_hits(tmp_path)


# ──────────────────────────────────────────────
# doctor 的**只读铁律**（plan §3 / §8 R1）与「轻层可跑」边界
#
# 这两条为什么必须静态守：doctor 是**故障现场**才被用到的工具，而它最危险的
# 失败模式不是崩溃，是**误报**——比如拿 `Registry.wanted_root_binaries()` 去做
# A 类资产对账（那份集合读的是「已装配实例」，而 doctor 不跑 load_all，恒为空），
# 结论会是「系统域里所有资产都是多余的，建议回收」。这类错误靠 code review
# 看不出来，只有在真机上出事，所以钉进门禁。
# ──────────────────────────────────────────────

#: doctor 检查侧禁止出现的符号（AST 级：只看真调用 / 真引用，注释与 docstring 免疫）。
DOCTOR_FORBIDDEN_SYMBOLS = (
    # 装配：实例化 + provision + start，并写 helper_tasks.json
    "load_all",
    # 提权：doctor 全程不弹授权
    "run_admin",
    # 写用户域 task 表
    "sync_helper_tasks",
    # 读「已装配实例」口径的 wanted ⇒ 在 doctor 里恒为空 ⇒ 误报回收（见上）
    "wanted_root_binaries",
    # 资产装配 / 部署 / 回收 / 系统状态复原
    "provision", "install", "uninstall", "teardown", "reconcile_root_binaries",
)

#: 必须保持**轻层**的 doctor 模块：顶层不得 import barkit 的重层。
#: ``survey`` 同时承载纯文件检查（``read_json``）与插件声明读取 —— 若它的顶层
#: 依赖了 registry 链，那么「registry 能不能 import」就会决定「JSON 能不能查」，
#: 而后者恰恰要在依赖残缺时才用得上。
DOCTOR_LIGHT_SOURCES = ("doctor/survey.py",)
DOCTOR_HEAVY_PREFIXES = (
    "barkit.core", "barkit.capabilities", "barkit.plugins",
    "barkit.shell", "barkit.contribution",
)


def _forbidden_symbol_hits(path: Path, forbidden: tuple[str, ...]) -> list[str]:
    """AST 扫描真符号引用（``Name`` / ``Attribute``），注释与字符串免疫。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            hits.append(f"{path.name}:{node.lineno} → .{node.attr}")
        elif isinstance(node, ast.Name) and node.id in forbidden:
            hits.append(f"{path.name}:{node.lineno} → {node.id}")
    return hits


def _doctor_readonly_hits(src_root: Path) -> list[str]:
    hits = []
    for path in sorted((src_root / "doctor").rglob("*.py")):
        hits.extend(_forbidden_symbol_hits(path, DOCTOR_FORBIDDEN_SYMBOLS))
    return hits


def _doctor_light_hits(src_root: Path) -> list[str]:
    hits = []
    for rel in DOCTOR_LIGHT_SOURCES:
        path = src_root / rel
        if not path.exists():
            continue
        for target, guarded in _module_level_imports(path):
            if not guarded and target.startswith(DOCTOR_HEAVY_PREFIXES):
                hits.append(f"{rel} → {target}")
    return hits


def test_doctor_checks_are_read_only():
    """doctor 全程只读：不得出现装配 / 提权 / 写盘 / 回收类符号。"""
    hits = _doctor_readonly_hits(SRC)
    assert not hits, (
        "doctor 出现只读铁律的破坏点：\n" + "\n".join(hits)
        + "\n（需要这些效果的检查请说明理由并更新 docs/log/2026-09-17-barkit-doctor.md §3）"
    )


def test_doctor_readonly_gate_detects_planted_violation(tmp_path):
    """植入违规必抓，且**注释 / docstring 里的同名文字不误报**。"""
    (tmp_path / "doctor").mkdir()
    bad = tmp_path / "doctor" / "checks_bad.py"
    bad.write_text(
        "def go(registry, client):\n"
        "    registry.load_all()\n"
        "    client.run_admin('x')\n"
        "    return registry.wanted_root_binaries()\n",
        encoding="utf-8",
    )
    hits = _doctor_readonly_hits(tmp_path)
    assert sum(".load_all" in h for h in hits) == 1, hits
    assert any("run_admin" in h for h in hits), hits
    assert any("wanted_root_binaries" in h for h in hits), hits

    good = tmp_path / "doctor" / "checks_good.py"
    good.write_text(
        '"""本组绝不调 load_all()，也不碰 run_admin / wanted_root_binaries。"""\n'
        "# 说明：load_all 会写 helper_tasks.json，故只 discover。\n"
        "def go(registry, survey):\n"
        "    return survey.discover()\n",
        encoding="utf-8",
    )
    assert _forbidden_symbol_hits(good, DOCTOR_FORBIDDEN_SYMBOLS) == []


def test_doctor_light_module_has_no_heavy_imports():
    """``survey`` 顶层零 barkit 重层依赖，保证纯文件检查在任何依赖状况下可跑。"""
    hits = _doctor_light_hits(SRC)
    assert not hits, "survey 顶层引入了重层依赖：\n" + "\n".join(hits)


def test_doctor_light_gate_detects_planted_violation(tmp_path):
    """植入必抓 ＋ 三种最容易误伤的情形。

    第三条（函数体内延迟导入）是**实测踩到过的**误报：``survey.discover`` 正是
    用延迟导入把 registry 链挡在「纯文件检查」之外，拿全文件扫描去守会把它判成违规。
    """
    (tmp_path / "doctor").mkdir()
    survey = tmp_path / "doctor" / "survey.py"

    survey.write_text("from barkit.core.registry import Registry\n", encoding="utf-8")
    assert _doctor_light_hits(tmp_path)

    survey.write_text(
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "from barkit import paths\n"
        "if TYPE_CHECKING:\n"
        "    from barkit.core.registry import Registry\n",
        encoding="utf-8",
    )
    assert _doctor_light_hits(tmp_path) == [], "类型期的重层引用不算运行期依赖"

    survey.write_text(
        "from barkit import paths\n"
        "def discover():\n"
        "    from barkit.core.registry import Registry\n"
        "    return Registry\n",
        encoding="utf-8",
    )
    assert _doctor_light_hits(tmp_path) == [], "函数体内的延迟导入不是顶层依赖"


# ──────────────────────────────────────────────
# doctor 的**不连会话**铁律
#
# 为什么不能并入上面那条只读门禁：DOCTOR_FORBIDDEN_SYMBOLS 扫的是 Name / Attribute，
# **扫不到 import** —— 而「连上会话」这件事恰恰从 import 开始，且一 import 就够
# （会话客户端一构造就会握手、重放意图、订阅事实）。
#
# 后果写清楚：方案 §9 的「一个 subject 一个写者」挡的是两个写者互相覆盖，但 doctor
# 一旦连上就是第二个写者 —— 它不改任何东西，却会把 app 声明的意图挤掉，或者反过来
# 被 app 挤掉。**搁着不管，最坏的情形是：用户为了查故障跑一次自检，把自己的网络
# 配置改了。** 这条边界此前只有文档里一句话，没有任何东西守。
# ──────────────────────────────────────────────

#: 通道两侧的模块（app 侧客户端 / 守护侧服务端 / Qt 装配层）。
DOCTOR_SESSION_MODULES = (
    "barkit.capabilities.helper_link",
    "barkit.capabilities.helper.session",
    "barkit.shell.helper_session",
)

#: 通道两侧的入口类名（防「自己另起一个客户端」这种绕过 import 的写法）。
DOCTOR_SESSION_SYMBOLS = ("HelperLink", "HelperSession", "SessionServer")


def _doctor_session_free_hits(src_root: Path) -> list[str]:
    hits = []
    for path in sorted((src_root / "doctor").rglob("*.py")):
        rel = path.relative_to(src_root).as_posix()
        for target, guarded in _import_targets(path):
            if not guarded and target in DOCTOR_SESSION_MODULES:
                hits.append(f"{rel} → import {target}")
        hits.extend(_forbidden_symbol_hits(path, DOCTOR_SESSION_SYMBOLS))
    return hits


def test_doctor_never_opens_a_session():
    """doctor 全程不连守护那条会话（只读工具，连上就是第二个写者）。"""
    hits = _doctor_session_free_hits(SRC)
    assert not hits, (
        "doctor 出现了会话通道的依赖：\n" + "\n".join(hits)
        + "\n（要看守护的在跑状态请读盘上那份快照 / launchctl，不要连会话）"
    )


def test_doctor_session_gate_detects_planted_violation(tmp_path):
    """三种植入都要抓：顶层 import / 函数内延迟 import / 直接构造客户端。"""
    (tmp_path / "doctor").mkdir()
    bad = tmp_path / "doctor" / "checks_bad.py"
    bad.write_text(
        "from barkit.capabilities.helper_link import HelperLink\n"
        "def go():\n"
        "    from barkit.shell.helper_session import HelperSession\n"
        "    return HelperLink(client='doctor'), HelperSession\n",
        encoding="utf-8",
    )
    hits = _doctor_session_free_hits(tmp_path)
    assert sum("import barkit.capabilities.helper_link" in h for h in hits) == 1, hits
    assert sum("import barkit.shell.helper_session" in h for h in hits) == 1, hits
    assert sum("HelperLink" in h for h in hits) >= 1, hits
    assert sum("HelperSession" in h for h in hits) >= 1, hits


def test_doctor_session_gate_passes_on_lazy_state_read(tmp_path):
    """合规写法不误报：延迟读 ``helper.state`` 那个常量是**读盘**，不是连会话。

    ``checks_data._snapshot_version()`` 就是用 ``importlib`` 现取版本号 —— 它必须被
    放行，否则这条门禁会把「唯一正确的取版本方式」判成违规。
    """
    (tmp_path / "doctor").mkdir()
    (tmp_path / "doctor" / "checks_data.py").write_text(
        "import importlib\n"
        "_SNAPSHOT_MODULE = 'barkit.capabilities.helper.state'\n"
        "def _snapshot_version():\n"
        "    module = importlib.import_module(_SNAPSHOT_MODULE)\n"
        "    return getattr(module, 'SNAPSHOT_VERSION', None)\n",
        encoding="utf-8",
    )
    assert _doctor_session_free_hits(tmp_path) == []


# 说明：test_no_emoji_in_source（§10）随批次 1 落地——当前 plugins 与文档注释中
# 尚存 emoji（"🧭"/"✨"），替换随 IconName 接线一起做，先立骨架不立空门槛。


# ──────────────────────────────────────────────
# 插件侧「零私有线程 / 零私有时钟 / 零私有系统事件源」门禁
# （批次 5 后段 S4，批次 9 扩到事件源）
#
# 依据：方案 §8.3.1（调度权归框架）、§8.3.3（四套时钟对齐 / 事件源收归）、
# 待决 12（「bridge.py 不给私有线程例外」）、§8.2.3（事件源由框架统一提供）。
# 插件只经 Plugin.resident_services() **声明**长驻资源、经 Plugin.file_watches()
# **声明**文件监视；线程 / 时钟 / 起停归 core/resident，事件源归 core/filewatch。
# ──────────────────────────────────────────────

PLUGINS = SRC / "plugins"

# 已知豁免：每条必须带 reason，新增条目**需评审**（方案 §6）。
# 豁免口径 = 「UI 内部交互，生命周期随窗口/对话框」或「独立进程节拍」，
# 不是「暂时没空迁移」，**也不是**「事件源」（事件源一律收归框架，无豁免）。
#
# 2026-09-23 补了**第三类**：「**在框架给的 worker 线程上做有界等待**」（首个消费方＝
# ``bt_guard``）。它既不自起线程、也不占节拍 —— 那段代码本就跑在 ``tick()`` 里，而
# ``tick()`` 是框架派给本组件的 worker（每组件 ``maxThreadCount=1``）；等的对象是
# 「一个子进程回话 / 一个设备重新枚举 / 硬件拆装」，都**有上限、到点就返回**。
# 判据：这类 ``sleep`` 删掉之后那段逻辑根本写不出来，或者只能改成「每拍看一眼」——
# 后者把一次有界等待摊成无数拍，反而更坏。与「暂时没空迁移」的区别就在这里：
# 前者是形态使然，后者是欠债。
PLUGIN_TIMER_WHITELIST: dict[str, str] = {
    "autotype/countdown_view.py":
        "UI 倒计时与逐字注入的 QTimer，随窗口生命周期，非后台轮询（§8.3.1 明示豁免）",
    # ◷ 2026-10-08：翻页钟与待办合并方案批 2 —— 浮层（钟面 ＋ 结构化条目行）搬进
    # `todo/clock_view.py`；批 5 旧 `clock/clock_view.py` 退场 ⇒ 同一条实时节拍的豁免
    # 收成一处（`test_timer_whitelist_entries_are_live_and_justified` 会当场抓住悬空项）。
    "todo/clock_view.py":
        "待办提醒浮层的实时节拍 QTimer：随浮层生命周期（子对象），仅每 250ms 核对墙钟 / "
        "续算锚点，让秒针与倒计时精确走在秒边界；非后台轮询、不占框架节拍（§8.3.1 明示豁免）",
    "autotype/engine.py":
        "注入过程的逐键间隔等待（time.sleep），UI 流程内可中断，非后台轮询（同上）",
    # ◷ 2026-10-03：`window_view.py` 拆成同目录一族模块（插件包不许子目录，§2.3）
    # ⇒ 拆成**三条**，豁免跟着代码走（不然这条白名单就悬空、「插件不得私起时钟」
    # 那条又会去点名一份已经没有时钟的文件）。三枚分别在：
    #   content_clipboard —— 剪贴板监听 QTimer；
    #   content_shell     —— QThreadPool 的**建池**（在 __init__ 里）；
    #   content_pipeline  —— 两次 start（真正把任务提交上去）；
    #   content_drag      —— showEvent 里三处 QTimer.singleShot（等渲染完再识别，
    #     一次性、不是节拍源，同 §8.3.1 那种「UI 内部交互」豁免）；
    #   view_task         —— QRunnable 子类**定义**（门禁按 AST 抓「类声明那一行」，
    #     而 QRunnable 的提交点在 content_pipeline，两条都要在）。
    "recognize/content_clipboard.py":
        "剪贴板监听 QTimer（_start_clipboard_watch 里 QTimer(self) ＋ _poll_clipboard），"
        "随内容面生命周期（§8.3.1 明示豁免）",
    "recognize/content_shell.py":
        "OCR/条码并行 QThreadPool 的**建池**（__init__ 里 QThreadPool(self)，跟随内容面销毁）"
        "（§8.3.1 明示豁免）",
    "recognize/content_pipeline.py":
        "把识别任务提交上去：_start 里 _RecognizeTask.make 的两条 QThreadPool.start，"
        "用户点了才跑、非后台常驻（§8.3.1 明示豁免）",
    "recognize/content_drag.py":
        "showEvent 里三处 QTimer.singleShot(120, self._start)：等本轮渲染完再触发识别，"
        "一次性、不是节拍源（同 §8.3.1「UI 内部交互」豁免）",
    "recognize/view_task.py":
        "class _RecognizeTask(QRunnable) 这条**类声明**本身（门禁按 AST 抓类定义那一行；"
        "任务体跑在 content_pipeline 建的池子里、随内容面销毁，非后台常驻）",
    "ipad_tunnel/settings.py":
        "诊断窗的 _DiagWorker(QThread)：用户点了才跑、随页面销毁，非后台常驻",
    "bt_guard/bt_state.py":
        "框架 worker 上的有界等待（第三类，口径见上方注释）：ioreg 输出解析失败后的重试"
        "间隔（≤2×0.3s）、等设备重新出现在 HID 层（≤60s，每 1s 重读一次现场）。"
        "全部跑在 tick() 的执行体里，不自起线程、不占节拍，且都有上限。"
        "注：控制器断电与重开之间那 2s 拆装间隔，2026-10-04 蓝牙助手并入会话时随代码"
        "搬到了 bt_guard/bt_session.py，本文件不再有那一处",
    # ◷ 2026-10-04：蓝牙助手并入会话（方案 §8 第 4 步）新建的模块，独占一处豁免。
    "bt_guard/bt_session.py":
        "同上（第三类）：与随包助手那条会话里的三项有界等待 —— 等它连上来（≤20s，含 "
        "open 之后 LaunchServices 注册与 dyld 首次加载）、等握手帧（≤5s）、等每条命令的"
        "回执（≤20s，须宽于助手侧 waitPower 的 8s），外加两条命令之间那 2s 拆装间隔"
        "（POWER_CYCLE_GAP_S）。run_commands 整个跑在 tick() 的执行体里（框架 worker），"
        "不自起线程、不自起时钟，每项等待都有上限；删掉这些等待就只能退化成「拉起后立刻"
        "读一次」，而助手冷启动实测 2 秒以上 ⇒ 必然把每一次动手都判失败",
    "bt_guard/plugin.py":
        "同上（第三类）：醒后那一次重探的有界等待（_reprobe_hid_until），上限 "
        "WAKE_HID_PATIENCE_S(7s)、每 0.5s 重读一次现场、读到**且**见到目标设备即停，"
        "跑在 tick() 的执行体里（框架 worker，不自起线程、不占节拍）。判据见上方第三类："
        "删掉这段 sleep 就只能退化成「醒后头一读算数」—— 设备刚醒还没枚举回来时那一读"
        "必然报「不在」，于是白断一次正连着的设备；有界等待在这里是形态使然、不是欠债",
    # ◷ 2026-10-03：`manager_view.py` 拆成同目录一族模块（插件包不许子目录，§2.3），
    # 那枚 ``QTimer`` 随「更新」那一族搬进了 ``manager_refresh.py`` ⇒ 豁免跟着改名，
    # 不然这条白名单就悬空、「插件不得私起时钟」那条又会去点名一份已经没有时钟的文件。
    "hosts_guard/manager_refresh.py":
        "管理窗「更新」按下之后的盯梢：一次性 QTimer.singleShot（**不是节拍源**，"
        "receiver 是本控件 ⇒ 窗口一销毁就自动断开），只在这一个动作的窗口内跑、次数有"
        "上限（12 × 0.4s），每次只重画那几格文本。它是「按了键要看见结果」的 UI 交互，"
        "不是后台轮询（§8.3.1 明示豁免）；真正的解析在长驻服务线程上（core/resident）。",
    # 注：route_guard/helper.py（root helper，1s 主循环 sleep）批次 4 已迁出 plugins，
    # 成为框架资产 capabilities/helper/（`python3 -m helper` 的 root LaunchDaemon）。
    # 它不是 app 内插件代码，本门禁（插件不得私起线程/时钟）不再适用于它，故撤除豁免。
}

# 迁移收口证明（批次 5 后段 ＋ 批次 9）：**这一族**是收口对象，**不得**出现在白名单里。
#
# ⚠️ 判据从「两个文件路径」改成「**入口 ＋ 它的同包运行期闭包**」是 2026-10-01 拆包的
# 连带改动，不是顺手扩面：`ipad_tunnel/plugin.py` 那天拆成了同目录一族模块（插件包不许
# 子目录，见 §2.3），正文 —— 也就是当年真会私起线程 / 时钟 / 事件源的那片 —— 全搬进了
# 兄弟模块。只按原路径点名的话 `_plugin_timer_hits` 恒返回空，收口证明**当场恒真**而它
# 照样是绿的（拆包带给路径型门禁的那类失效：判据的「对象」变了，而入口那个文件名还在）。
#
# 闭包**停在 UI 面** ⇒ `ipad_tunnel/settings.py`（诊断窗的 `_DiagWorker`）不在这一族里，
# 它另行挂在 `PLUGIN_TIMER_WHITELIST` 上 —— 这正是这条改动不能简单换成「收整包」的原因。
MIGRATED_PLUGIN_ENTRY = "ipad_tunnel/plugin.py"


def _migrated_family() -> list[str]:
    """收口对象的文件面：入口 ＋ 它在**同包内**的运行期导入闭包（UI 面除外）。

    实测这一族 = `bridge.py` / `constants.py` / `intent.py` / `link.py` / `plugin.py` /
    `settings_data.py` / `snapshot.py` / `tunnel_spec.py` 八个文件。
    """
    return [p.relative_to(PLUGINS).as_posix()
            for p in _plugin_runtime_module_files(PLUGINS / MIGRATED_PLUGIN_ENTRY)]

# 被禁的 Qt 类型名（按点号末段匹配，防限定调用绕过）
_BANNED_CALL_TYPES = frozenset({"QTimer", "QThread", "QThreadPool", "QFileSystemWatcher"})


def _dotted(node: ast.AST) -> str | None:
    """把 Name / Attribute 链拼成点号名（``a`` / ``a.b`` / ``a.b.c``）。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _plugin_timer_hits(path: Path) -> list[str]:
    """AST 扫描单文件的「私有线程 / 私有时钟 / 私有系统事件源」用法；返回命中项。

    覆盖：``import threading`` / ``from threading import ...`` /
    ``from time import sleep``（含裸 ``sleep()`` 的叫法）/ ``threading.Timer(`` /
    ``time.sleep(`` / ``QTimer(`` / ``QTimer.singleShot(`` / ``QThread(`` /
    ``QThreadPool(`` / ``QFileSystemWatcher(`` / ``class X(QThread)`` /
    ``class X(QRunnable)``。

    ``QFileSystemWatcher`` 自批次 9 起纳入：文件事件源由框架统一提供（§8.2.3），
    插件改经 ``Plugin.file_watches()`` 声明路径。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: set[str] = set()

    def _banned_call_name(name: str) -> bool:
        # 精确名（含点号形态，如 QTimer.singleShot）与「末段类型名」两路匹配：
        # 后者防 ``QtCore.QFileSystemWatcher(`` 之类的限定调用绕过。
        if name in {"threading.Timer", "time.sleep", "QTimer.singleShot"}:
            return True
        return name.split(".")[-1] in _BANNED_CALL_TYPES

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "threading" or alias.name.startswith("threading."):
                    hits.add(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "threading" or module.startswith("threading."):
                hits.add(f"from {module} import ...")
            if module == "time" and any(a.name == "sleep" for a in node.names):
                hits.add("from time import sleep")
        elif isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name and _banned_call_name(name):
                hits.add(f"{name}(")
        elif isinstance(node, ast.ClassDef):
            for base in node.bases:
                name = _dotted(base)
                if name and name.split(".")[-1] in {"QThread", "QRunnable"}:
                    hits.add(f"class {node.name}({name})")
    return sorted(hits)


def test_no_private_timer_in_plugins():
    """插件目录内不得出现私有线程 / 私有定时器（白名单除外，见上表）。"""
    violations: dict[str, list[str]] = {}
    for path in sorted(PLUGINS.rglob("*.py")):
        hits = _plugin_timer_hits(path)
        rel = path.relative_to(PLUGINS).as_posix()
        if hits and rel not in PLUGIN_TIMER_WHITELIST:
            violations[rel] = hits
    assert not violations, (
        "插件内不得私起线程/定时器：改为 Plugin.resident_services() 声明服务，"
        "线程/时钟/起停归框架（core/resident）；确属 UI 内部交互的须进白名单并写明理由。\n"
        + "\n".join(f"  src/barkit/plugins/{rel}: {', '.join(hits)}"
                    for rel, hits in violations.items())
    )


# ── QThread 的销毁安全（2026-09-29 真机崩溃：SIGABRT / abort()）────────────

_QTHREAD_BASE = "QThread"


def _qthread_class_names() -> frozenset:
    """全仓 ``QThread`` 子类名（含 ``QThread`` 本身）—— ``parent=`` 要连子类一起查。"""
    names = {_QTHREAD_BASE}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if any((_dotted(b) or "").split(".")[-1] == _QTHREAD_BASE
                       for b in node.bases):
                    names.add(node.name)
    return frozenset(names)


def _qthread_parent_hits(path: Path, classes: frozenset) -> list[str]:
    """AST 扫描单文件：``QThread``（含子类）的构造不得带 ``parent``。

    父对象销毁会连带析构子对象；若那条线程还在跑，就是「线程仍在运行时析构
    ``QThread``」⇒ Qt ``qFatal`` ⇒ ``abort()``，**整个进程死**（2026-09-29 报告
    ``Python-2026-09-29-155751.ips``，触发线程 ``_DiagWorker``；热键页那条
    ``CommandRunner(command, parent=self)`` 是同一形态）。

    线程的生命周期交给 ``core.thread_reap`` 代持 —— 那才是「不会被提前析构」的保证；
    ``parent`` 给的是**相反**的保证。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func)
        if not name or name.split(".")[-1] not in classes:
            continue
        if any(k.arg == "parent" for k in node.keywords):
            hits.add(f"{name}(parent=…)（第 {node.lineno} 行）")
    return sorted(hits)


def test_qthread_is_never_given_a_parent():
    """``QThread`` 不得挂父对象：父销毁 ⇒ 线程还在跑就析构 ⇒ 整进程 abort。"""
    classes = _qthread_class_names()
    violations: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        hits = _qthread_parent_hits(path, classes)
        if hits:
            violations[path.relative_to(SRC).as_posix()] = hits
    assert not violations, (
        "QThread 不得带 parent：父对象销毁会连带析构它，而线程可能还在跑 ⇒ Qt qFatal ⇒ "
        "整个进程 abort（2026-09-29 真机崩溃）。生命周期改由 core.thread_reap 代持。\n"
        + "\n".join(f"  src/barkit/{rel}: {', '.join(hits)}"
                    for rel, hits in violations.items())
    )


#: 中继的接入路径必须走非阻塞版（2026-09-20 自激回路修复）
_RELAY_CLASS = "Bridge"
_BLOCKING_DEVICE_OPEN = "open_device_tunnel"


def _blocking_connect_hits(path: Path) -> list[str]:
    """AST 扫描：``Bridge`` 类内不得调用阻塞版 ``open_device_tunnel``。

    中继是**单线程事件循环**：在那里同步等 usbmuxd 应答（旧实现 5s 超时 × 2 次重试）
    会把整条中继卡住，忙时一分钟只服务 6 条连接 —— 这就是 2026-09-20 那轮「57 条会话
    全部卡在 SOCKS 握手、探针同时超时」的放大器。接入路径只准用非阻塞两段式
    （``begin_device_tunnel`` + ``parse_connect_reply`` + ``_expire_pending`` 的截止时间）；
    阻塞版留给探针 / CLI（各自在自己的线程里等，不占事件循环）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != _RELAY_CLASS:
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                name = _dotted(sub.func)
                if name and name.split(".")[-1] == _BLOCKING_DEVICE_OPEN:
                    hits.add(f"{name}(（第 {sub.lineno} 行）")
    return sorted(hits)


def test_relay_accept_path_stays_non_blocking():
    """中继接入不得退回阻塞式开设备通道（防自激回路复发）。"""
    violations: dict[str, list[str]] = {}
    for path in sorted(PLUGINS.rglob("*.py")):
        hits = _blocking_connect_hits(path)
        if hits:
            violations[path.relative_to(PLUGINS).as_posix()] = hits
    assert not violations, (
        "中继事件循环里不得同步等 usbmuxd 应答：接连接请用 begin_device_tunnel() + "
        "parse_connect_reply() + 截止时间（见 bridge.py 模块 docstring 的 2026-09-20 段）。\n"
        + "\n".join(f"  src/barkit/plugins/{rel}: {', '.join(hits)}"
                    for rel, hits in violations.items())
    )


def _api_uses(path: Path, names: frozenset[str]) -> list[str]:
    """AST 扫描「真正用到」给定 API 名的用法（导入 / 调用 / 属性访问）。

    与 ``_plugin_timer_hits`` 的区别：本函数不看子串，故文档字符串与注释里的
    迁移说明（如「不再私持 QFileSystemWatcher」）不会误报。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[-1] in names:
                    hits.add(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in names:
                    hits.add(f"from {node.module} import {alias.name}")
        elif isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name and name.split(".")[-1] in names:
                hits.add(f"{name}(")
        elif isinstance(node, ast.Attribute):
            if node.attr in names:
                hits.add(f"<expr>.{node.attr}")
    return sorted(hits)


def test_timer_whitelist_entries_are_live_and_justified():
    """白名单不得悬空（文件必须存在）、理由不得为空，且收口对象不得被豁免。

    批次 9 收紧：白名单里**不得**出现 ``QFileSystemWatcher`` —— 事件源由框架统一
    提供（§8.2.3），没有「UI 内部交互」这类豁免口径，收到声明里就不该自建。
    """
    family = _migrated_family()
    for rel, reason in PLUGIN_TIMER_WHITELIST.items():
        assert (PLUGINS / rel).is_file(), f"白名单指向不存在的文件（悬空豁免）：{rel}"
        assert len(reason.strip()) >= 15, f"白名单 {rel} 的 reason 过简（需说明豁免依据）"
        assert rel not in family, f"{rel} 是收口对象（批次 5 后段那一族），不得豁免"
        assert _api_uses(PLUGINS / rel, frozenset({"QFileSystemWatcher"})) == [], (
            f"{rel} 用了 QFileSystemWatcher：事件源须由框架提供"
            "（Plugin.file_watches()），不设豁免（方案 §8.2.3）"
        )


def test_migrated_plugin_family_has_no_private_threads():
    """批次 5 后段收口证明：**这一族**必须零命中（方案待决 12 的落地断言）。"""
    family = _migrated_family()
    # 非恒真控制组：这一族必须**真的**不止入口一个文件。只剩入口时这条门禁就退化成拆包前
    # 的样子（正文早搬走了、入口里本来就没有线程），而它照样绿 —— 那是静默失效，不是通过。
    assert len(family) > 1, f"收口对象的文件面只剩 {family} ⇒ 闭包断了，正文里的线程抓不到"
    bad = {rel: hits for rel in family if (hits := _plugin_timer_hits(PLUGINS / rel))}
    assert not bad, f"这一族仍含私有线程/时钟：{bad}"


def test_migrated_plugin_family_has_no_private_file_watcher():
    """批次 9 收口证明：文件事件源零私持（§8.2.3 的落地断言）。"""
    for rel in _migrated_family():
        hits = _plugin_timer_hits(PLUGINS / rel)
        assert hits == [], f"{rel} 仍含私有事件源/线程/时钟：{hits}"
        uses = _api_uses(PLUGINS / rel, frozenset({"QFileSystemWatcher"}))
        assert uses == [], f"{rel} 仍直接用 QFileSystemWatcher：{uses}"


def test_timer_gate_detects_planted_violations(tmp_path):
    """门禁自测：故意注入各类违规必须被检出（防门禁形同虚设）。"""
    sample = tmp_path / "planted.py"
    sample.write_text(
        "import threading\n"
        "from time import sleep\n"
        "from threading import Timer\n"
        "from PySide6.QtCore import QThread, QThreadPool, QTimer, QRunnable, QFileSystemWatcher\n"
        "from PySide6 import QtCore\n"
        "\n"
        "class Worker(QThread):\n"
        "    pass\n"
        "\n"
        "class Task(QRunnable):\n"
        "    pass\n"
        "\n"
        "def f():\n"
        "    threading.Timer(1.0, f).start()\n"
        "    Timer(1.0, f).start()\n"
        "    sleep(1)\n"
        "    QTimer()\n"
        "    QTimer.singleShot(1, f)\n"
        "    QThread()\n"
        "    QThreadPool()\n"
        "    QFileSystemWatcher(['/'])\n"
        "    QtCore.QFileSystemWatcher(['/'])\n",
        encoding="utf-8",
    )
    hits = _plugin_timer_hits(sample)
    joined = " | ".join(hits)
    for expected in (
        "import threading",
        "from time import sleep",
        "from threading import ...",
        "threading.Timer(",
        "class Worker(QThread)",
        "class Task(QRunnable)",
        "QTimer(",
        "QTimer.singleShot(",
        "QThread(",
        "QThreadPool(",
        "QFileSystemWatcher(",
        "QtCore.QFileSystemWatcher(",
    ):
        assert expected in joined, f"门禁漏检：{expected}（实得：{hits}）"


def test_timer_gate_passes_on_compliant_sample(tmp_path):
    """反向自测：只用 selectors / collections 等合规写法的文件不得被误报。"""
    sample = tmp_path / "compliant.py"
    sample.write_text(
        "import selectors\n"
        "import time\n"
        "\n"
        "def wait(seconds):\n"
        "    sel = selectors.DefaultSelector()\n"
        "    sel.select(timeout=seconds)\n"
        "    return time.monotonic()\n",
        encoding="utf-8",
    )
    assert _plugin_timer_hits(sample) == []


# ──────────────────────────────────────────────
# 路径隔离门禁（批次 11；《重构-开工手册》§4.3 的 ⑧ 的前置）
#
# 依据：conftest 的 ``_isolate_barkit_data_dir`` 只补丁 ``barkit.paths`` 的**模块
# 属性**。任何 ``from barkit.paths import <Path 常量>`` 都会在**导入期**把真实路径
# 冻结进本模块全局名，使该固件静默失效。历史事故：测试直写真实用户的 routes.json /
# tunnel.json，跑完测试后 iPad 隧道显示未激活、流量改走 Wi-Fi。
#
# 受管名单从 ``vars(barkit.paths)`` **运行时派生**（取 Path 型值）：新增路径常量
# 自动纳入，无需改本文件。
#
# **但 conftest 的重定向表是手写的、不会自动纳入** —— 这一点曾被上一版注释写成
# 「也无需改 conftest」，2026-09-23 新增 bt_guard 的两个通信文件时正是照那句做、
# 于是那两条路径在测试里仍指向真实用户目录。覆盖性由下面
# ``test_every_path_constant_is_redirected`` 守，那句误导性的注释同时改掉。
# ``APP_NAME`` 是 str 显示常量，天然豁免。
# ──────────────────────────────────────────────

def _path_constant_names() -> set[str]:
    """``barkit.paths`` 里全部 Path 型常量的名字（运行时派生，自维护）。"""
    from barkit import paths

    return {
        name for name, value in vars(paths).items()
        if not name.startswith("_") and isinstance(value, Path)
    }


def _by_value_path_imports(path: Path) -> list[str]:
    """按值导入的受管路径常量（空 = 合规）。"""
    managed = _path_constant_names()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "barkit.paths":
            hits.extend(
                f"from barkit.paths import {a.name}" for a in node.names if a.name in managed
            )
    return hits


def _path_default_arg_uses(path: Path) -> list[str]:
    """默认参数值引用了路径常量（空 = 合规）。

    默认参数在**函数定义期**求值，等价于按值导入：模块属性补丁对它同样无效。
    覆盖两种写法：``def f(p=paths.X)`` 与 ``def f(p=X)``（X 由按值导入而来）。
    """
    managed = _path_constant_names()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        defaults = [*node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)]
        for default in defaults:
            for sub in ast.walk(default):
                if isinstance(sub, ast.Name) and sub.id in managed:
                    hits.append(f"{node.name}(): 默认参数引用 {sub.id}")
                elif (
                    isinstance(sub, ast.Attribute)
                    and isinstance(sub.value, ast.Name)
                    and sub.value.id == "paths"
                    and sub.attr in managed
                ):
                    hits.append(f"{node.name}(): 默认参数引用 paths.{sub.attr}")
    return hits


def test_no_by_value_path_imports():
    """生产代码不得按值导入 barkit.paths 的路径常量（必须 ``from barkit import paths``）。

    这是 conftest 路径隔离**按构造成立**的前提：只要没人按值导入，模块属性补丁
    就覆盖全部读点。
    """
    violations = {
        str(py.relative_to(SRC)): hits
        for py in sorted(SRC.rglob("*.py"))
        if (hits := _by_value_path_imports(py))
    }
    assert violations == {}, (
        "存在按值导入的路径常量，会让 conftest 的路径重定向静默失效"
        "（测试将写入真实用户目录）：\n"
        + "\n".join(f"  {f}: {h}" for f, h in violations.items())
        + "\n改法：from barkit import paths，然后 paths.<常量> 在调用期读取。"
    )


def test_no_path_constant_default_args():
    """函数默认参数值不得引用路径常量（定义期求值 = 按值导入的等价陷阱）。"""
    violations = {
        str(py.relative_to(SRC)): hits
        for py in sorted(SRC.rglob("*.py"))
        if (hits := _path_default_arg_uses(py))
    }
    assert violations == {}, (
        "默认参数在函数定义期求值，路径重定向对它无效：\n"
        + "\n".join(f"  {f}: {h}" for f, h in violations.items())
        + "\n改法：默认写 None，函数体内 `x = x if x is not None else paths.<常量>`。"
    )


def test_path_gates_detect_planted_violations(tmp_path):
    """植入式自测：两种违规写法都必须被抓到（门禁自身不得退化成恒真）。"""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from barkit import paths\n"
        "from barkit.paths import HOSTS_ENTRIES_FILE, APP_NAME\n"
        "\n"
        "def f(a, b=paths.HELPER_SOCKET, c=HOSTS_ENTRIES_FILE):\n"
        "    return a, b, c\n",
        encoding="utf-8",
    )
    by_value = _by_value_path_imports(bad)
    assert by_value == ["from barkit.paths import HOSTS_ENTRIES_FILE"], by_value
    assert "APP_NAME" not in " ".join(by_value), "str 显示常量不得被误报"
    defaults = _path_default_arg_uses(bad)
    assert any("paths.HELPER_SOCKET" in h for h in defaults), defaults
    assert any("HOSTS_ENTRIES_FILE" in h for h in defaults), defaults


def test_path_gates_pass_on_compliant_sample(tmp_path):
    """反向自测：合规写法（调用期读取 + None 哨兵）不得被误报。"""
    good = tmp_path / "good.py"
    good.write_text(
        "from pathlib import Path\n"
        "from barkit import paths\n"
        "\n"
        "def f(a=None):\n"
        "    a = a if a is not None else paths.HELPER_SOCKET\n"
        "    return Path(a)\n"
        "\n"
        "X = paths.HOSTS_ENTRIES_FILE\n",
        encoding="utf-8",
    )
    assert _by_value_path_imports(good) == []
    assert _path_default_arg_uses(good) == []


# ──────────────────────────────────────────────
# 路径隔离的**另一半**：conftest 的重定向表必须覆盖全部路径常量
#
# 上面两条门禁保证「读点全走模块属性」（补丁能生效）；这一条保证「该补的都补了」。
# 两半合起来，隔离才成立 —— 缺了后半，一条新路径常量会**整条漏在隔离之外**：
# 它在测试里仍指向真实用户目录，而多数用例甚至不读它，于是「跑测试往用户数据里
# 真写了一份」这件事既不报错也不影响断言语义，是最难发现的一类污染。
#
# 真源在手写侧（``tests/conftest.py`` 的 ``PATH_REDIRECTS``）—— 它**不会**自动纳入
# 新常量，这正是本条门禁存在的理由。2026-09-23 新增 bt_guard 的两个通信文件时漏过
# 一次（当时靠人眼比对才发现）。
# ──────────────────────────────────────────────


def unredirected_path_constants(names: set[str], redirects: dict) -> list[str]:
    """清单里有、重定向表里没有的常量名（排序返回，空 = 合规）。"""
    return sorted(name for name in names if name not in redirects)


def _conftest_redirect_table() -> dict:
    """``tests/conftest.py`` 的 ``PATH_REDIRECTS``（按文件加载，不触发任何固件）。"""
    import importlib.util

    path = Path(__file__).with_name("conftest.py")
    spec = importlib.util.spec_from_file_location("_barkit_conftest_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return dict(mod.PATH_REDIRECTS)


def test_every_path_constant_is_redirected():
    """每个 Path 型路径常量都必须在 conftest 的重定向表里（否则它没被隔离）。

    判据是**两份清单是否一致**，不是「某条路径写没写对」：后者无法从源码判（路径由
    ``Path.home()`` 派生），而「漏登记」才是真正会静默发生的那种错。
    """
    misses = unredirected_path_constants(_path_constant_names(), _conftest_redirect_table())
    assert misses == [], (
        "这些路径常量没被 conftest 重定向，测试里仍指向**真实用户目录**：\n"
        + "\n".join(f"  {m}" for m in misses)
        + "\n改法：在 tests/conftest.py 的 PATH_REDIRECTS 里补一行（绝大多数是"
        "「常量名 → 文件名」）。"
    )


def test_path_redirection_gate_detects_a_missing_entry():
    """双自证：漏登记必抓、登记齐不误报，且**真实那一半**确实非空。

    控制组用真实出错的那份输入：正是 2026-09-23 漏掉 ``BT_REQUEST_FILE`` 的那个形态
    —— **清单里有它、表里没有**。注意植入方向必须是「从表里删掉一行」，不是「往清单里
    多加一项」：后者在常量已入仓的今天等于什么都没改，用例会假绿。
    （控制组点名的那个常量随在册常量走：``BT_REQUEST_FILE`` 已随蓝牙旁路并入会话而消失，
    现在用 ``BT_HELPER_SOCKET``。）
    """
    real = _path_constant_names()
    assert "BT_HELPER_SOCKET" in real and "HELPER_SOCKET" in real, (
        "运行时派生的常量清单是空的或不含预期项 —— 这条门禁在测别的东西"
    )

    redirects = {n: "" for n in real}
    assert unredirected_path_constants(real, redirects) == [], "登记齐了却被误报"

    del redirects["BT_HELPER_SOCKET"]         # ← 与那次真实的漏登记同形
    assert unredirected_path_constants(real, redirects) == ["BT_HELPER_SOCKET"], (
        "漏登记的常量没被抓到 —— 这条门禁测不到东西"
    )

    # 表比清单多几项是正常的（常量被删掉后表里留一行），不得误报
    assert unredirected_path_constants(real, {n: "" for n in real} | {"__gone__": ""}) == []


# ──────────────────────────────────────────────
# Qt 平台变量单一真源门禁（2026-09-28）
#
# 背景：``QT_QPA_PLATFORM`` 曾由三个测试模块各自 ``os.environ.setdefault`` 零散兜底
# （``test_shell_design.py`` / ``test_tray_base.py`` 模块级 ＋ ``test_ipad_tunnel.py``
# 某用例的函数体内），而 ``tests/conftest.py`` 里没有。pytest 在**收集阶段**就 import 所有
# test 模块 ⇒ **同一个文件跟谁一起跑，决定它跑在哪个平台**：``pytest tests/test_settings.py``
# 单跑（没人设过）⇒ 真跑 cocoa ⇒ macOS 上 3 条假红、Windows 上
# ``test_combo_field_lets_the_wheel_scroll_the_page`` 一条假红；与 ``test_shell_design.py``
# 同跑即全绿。⇒ 默认值收归 ``conftest.py`` 一处（``setdefault`` ⇒ 显式环境变量仍优先）。
# ──────────────────────────────────────────────

#: 允许给这个变量设默认值的**唯一**文件（相对 ``tests/``）。
QT_PLATFORM_SINGLE_SOURCE: str = "conftest.py"

#: 被收口的变量名。
QT_PLATFORM_VAR: str = "QT_QPA_PLATFORM"


def _is_os_environ(node: ast.expr) -> bool:
    """``os.environ`` 这条属性链（不认别名，也不认别的模块的同名属性）。"""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _qt_platform_set_lines(source: str) -> list[int]:
    """源码里给 ``QT_QPA_PLATFORM`` 设值的行号（AST 判定：注释 / 字符串里提到不算）。

    认两种形态：``os.environ.setdefault("QT_QPA_PLATFORM", …)`` 与
    ``os.environ["QT_QPA_PLATFORM"] = …``。
    """
    hits: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            f = node.func
            if f.attr == "setdefault" and _is_os_environ(f.value) and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and arg.value == QT_PLATFORM_VAR:
                    hits.append(node.lineno)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if (
                    isinstance(t, ast.Subscript)
                    and _is_os_environ(t.value)
                    and isinstance(t.slice, ast.Constant)
                    and t.slice.value == QT_PLATFORM_VAR
                ):
                    hits.append(node.lineno)
    return sorted(hits)


def stray_qt_platform_defaults(
    sources: dict[str, str], *, allow: str = QT_PLATFORM_SINGLE_SOURCE
) -> list[str]:
    """``{文件名: 源码}`` 里**除 ``allow`` 外**给平台变量设值的位置（``文件名:行号``）。

    空 = 合规。收成纯函数是为了能拿合成输入自证它抓得住（见配套用例）。
    """
    return [
        f"{name}:{line}"
        for name, src in sorted(sources.items())
        if name != allow
        for line in _qt_platform_set_lines(src)
    ]


def _test_dir_sources() -> dict[str, str]:
    """``tests/`` 下全部 ``*.py`` 的源码，键是相对 ``tests/`` 的路径。"""
    tests_dir = Path(__file__).parent
    return {
        p.relative_to(tests_dir).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(tests_dir.rglob("*.py"))
    }


def test_qt_platform_default_has_a_single_source():
    """平台变量只许在 ``conftest.py`` 里设默认值（别处零散兜底 ⇒ 平台随跑法漂）。"""
    sources = _test_dir_sources()
    assert len(sources) > 30, (
        f"扫描面只有 {len(sources)} 个测试文件 —— 路径写歪了，这条门禁在测空集"
    )
    strays = stray_qt_platform_defaults(sources)
    assert strays == [], (
        "这些位置绕过 conftest 给 QT_QPA_PLATFORM 设了默认值，会让平台随「跟谁一起跑」漂：\n"
        + "\n".join(f"  {s}" for s in strays)
        + f"\n改法：删掉它 —— 默认值已由 tests/{QT_PLATFORM_SINGLE_SOURCE} 统一提供。"
    )


def test_qt_platform_default_is_set_in_its_single_source():
    """反面半条：收口的那一处**真的在设**。

    必须与上一条**分开**：上一条判「面不多」，这条判「面不少」。合成一条断言时，有人把
    ``conftest.py`` 里那行删干净、整仓再没人设 —— 上一条照样绿，问题反被掩盖成「已收口」。
    """
    src = (Path(__file__).parent / QT_PLATFORM_SINGLE_SOURCE).read_text(encoding="utf-8")
    assert _qt_platform_set_lines(src), (
        f"{QT_PLATFORM_SINGLE_SOURCE} 里没有给 {QT_PLATFORM_VAR} 设默认值 —— "
        "平台一致性没人保证了（别处也不许补，改法是把这行加回来）"
    )


def test_qt_platform_gate_detects_a_stray_default():
    """自证：三种真实形态各必抓，且注释 / 字符串里提到变量名不误报。"""
    allow = QT_PLATFORM_SINGLE_SOURCE
    good = {allow: 'import os\nos.environ.setdefault("QT_QPA_PLATFORM", "offscreen")\n'}
    assert stray_qt_platform_defaults(good, allow=allow) == [], "只 conftest 设却被误报"

    bad = dict(good)

    # ① 模块级 setdefault（test_shell_design / test_tray_base 的原形态）
    bad["test_m.py"] = 'import os\n\nos.environ.setdefault("QT_QPA_PLATFORM", "offscreen")\n'
    assert stray_qt_platform_defaults(bad, allow=allow) == ["test_m.py:3"]

    # ② 函数体内 setdefault（test_ipad_tunnel 的原形态）
    bad["test_m.py"] = (
        "def test_x():\n"
        "    import os\n"
        '    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")\n'
    )
    assert stray_qt_platform_defaults(bad, allow=allow) == ["test_m.py:3"]

    # ③ 下标赋值
    bad["test_m.py"] = 'import os\nos.environ["QT_QPA_PLATFORM"] = "offscreen"\n'
    assert stray_qt_platform_defaults(bad, allow=allow) == ["test_m.py:2"]

    # 反向：注释与字符串里提到它**不算**（AST 判据，不用子串）
    assert _qt_platform_set_lines('# os.environ.setdefault("QT_QPA_PLATFORM", "x")\n') == []
    assert _qt_platform_set_lines('MSG = "QT_QPA_PLATFORM"\n') == []


# ──────────────────────────────────────────────
# 插件状态发布边界门禁（B 契约；方案 §10 门禁 test_plugin_does_not_publish_status 的落地）
#
# 依据：方案/工作记忆原写「插件禁自 publish」，但 shipping 代码（route_guard /
# hosts_guard / notify_guard / ipad_tunnel 四个 DAEMON 插件）均即时广播自身
# StateReport。这与「插件绝对不许发」的字面契约冲突。
#
# 决策（2026-09-11，与用户确认采用 B 契约）：框架仍是状态总线的唯一发言人与
# 周期兜底；插件允许「即时广播自身 StateReport 至自身 status topic」，但边界
# 必须守住——不得：① 广播到非自身 topic（越界 / 污染他人状态）；② 广播裸
# dict / 字面量（绕过 StateReport 单一真相）。``events.publish`` 本身不拦。
#
# 收口后（2026-09-14，插件结构规范 §11 阶段 0）：
# 即时广播的**唯一落点**是基类 ``Plugin.publish_snapshot()``（``core/plugin.py``），
# 四个插件手写的 ``_publish_state()`` / ``_status_topic`` 已删除。于是有三件事必须
# 随之调整，否则本门禁会静默退化成**恒真**：
#   ① 扫描面必须含基类文件——只扫 ``plugins/`` 将零命中；
#   ② topic 的合规形态改为 ``self.status_topic(...)`` **调用**（拼法真源在基类），
#      不再接受 ``_status_topic`` 私有字段（字段已删，留着容忍等于给它开后门）；
#   ③ 增加「非恒真」断言（``test_publish_gate_is_not_vacuous``），把「扫描面确实
#      覆盖到发布点」本身也变成被守护的不变量。
# ──────────────────────────────────────────────

# 扫描面 = 插件目录 + 基类文件（即时广播的唯一落点）
PUBLISH_SCAN: tuple[Path, ...] = (PLUGINS, SRC / "core" / "plugin.py")

def _plugin_id_of(path: Path) -> str | None:
    """从类属性 ``id = "..."`` 静态提取该插件 id（无法判定时返回 None）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if (isinstance(item, ast.Assign) and len(item.targets) == 1
                        and isinstance(item.targets[0], ast.Name)
                        and item.targets[0].id == "id"
                        and isinstance(item.value, ast.Constant)
                        and isinstance(item.value.value, str)):
                    return item.value.value
    return None


def _publish_calls(path: Path) -> list[tuple[ast.AST, ast.AST | None]]:
    """抓一个文件里所有 ``*.events.publish(topic, payload)`` 调用。

    返回 ``(topic_node, payload_node)`` 列表；payload 缺失时为 None。只认链路末段
    为 ``publish`` 且链中含 ``events`` 的调用（即 ``ctx.events.publish``）。
    既用于扫描面（插件目录 + 基类文件），也用于 tmp 文件里的自测用例。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[tuple[ast.AST, ast.AST | None]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted(node.func)
        if not dotted or not dotted.endswith(".publish"):
            continue
        if "events" not in dotted.split("."):
            continue
        args = node.args
        topic = args[0] if len(args) >= 1 else None
        payload = args[1] if len(args) >= 2 else None
        out.append((topic, payload))
    return out


def _topic_ok(topic: ast.AST, plugin_id: str | None) -> str | None:
    """返回违规原因；None = 合规。守两条边界：自身 topic / 非裸数据。

    合规形态只有三种：

    - ``self.status_topic(...)`` —— 拼法真源（``Plugin.status_topic()``），天然只
      可能拼出自身 topic，故直接放行；
    - 字符串字面量 ``"plugin.<id>.status.*"`` —— 校验形态且不得越界到他人 id；
    - f-string —— 动态 id，只做形态校验。
    """
    if isinstance(topic, ast.Call):
        dotted = _dotted(topic.func)
        if dotted and dotted.endswith(".status_topic"):
            return None  # 真源拼法，只可能拼出自身 topic
        return f"topic 须为自身 status_topic(...)（实得 {_dotted(topic.func)}）"
    if isinstance(topic, ast.Constant) and isinstance(topic.value, str):
        text = topic.value
        if not (text.startswith("plugin.") and ".status." in text):
            return f"topic 字面量须形如 plugin.<id>.status.*（实得 {text!r}）"
        if plugin_id and f"plugin.{plugin_id}.status" not in text:
            return f"topic 越界：{text!r} 非 plugin.{plugin_id}.status.*"
        return None
    if isinstance(topic, ast.JoinedStr):  # f-string（动态 id），只做形态校验
        text = ast.unparse(topic)
        if not (text.startswith("plugin.") and ".status." in text):
            return f"topic f-string 须形如 plugin.{{...}}.status.*（实得 {text!r}）"
        return None
    return f"topic 形态不被允许：{type(topic).__name__}"


def _payload_ok(payload: ast.AST | None) -> str | None:
    if payload is None:
        return "publish 缺少 payload（须广播 StateReport）"
    if isinstance(payload, (ast.Dict, ast.List)):
        return "禁止广播裸 dict/list，须广播 StateReport"
    if isinstance(payload, ast.Constant) and not isinstance(payload.value, str):
        return "禁止广播裸字面量，须广播 StateReport"
    return None  # Call（.snapshot()）/ 属性引用等视为 StateReport，放行


def _scan_files(root: Path) -> list[Path]:
    """扫描面条目可能是一个目录（插件目录）或单个文件（基类）；统一成文件列表。"""
    return sorted(root.rglob("*.py")) if root.is_dir() else [root]


def test_plugin_publishes_only_own_status_snapshot():
    """状态发布守边界（B 契约）：只发自身 topic、发 StateReport、不发裸 dict。

    扫描面**含基类文件**（收口后即时广播的唯一落点，见上方注释）；``events.publish``
    本身不拦，越界 topic / 裸数据必须被门禁抓出。扫描面是否真的覆盖到发布点，由
    ``test_publish_gate_is_not_vacuous`` 单独守护。
    """
    violations: dict[str, list[str]] = {}
    for root in PUBLISH_SCAN:
        for path in _scan_files(root):
            plugin_id = _plugin_id_of(path)
            for topic, payload in _publish_calls(path):
                reasons = []
                r = _topic_ok(topic, plugin_id)
                if r:
                    reasons.append("topic: " + r)
                r = _payload_ok(payload)
                if r:
                    reasons.append("payload: " + r)
                if reasons:
                    rel = path.relative_to(SRC).as_posix()
                    violations.setdefault(rel, []).append("; ".join(reasons))
    assert not violations, (
        "状态发布越界/裸数据：\n"
        + "\n".join(f"  src/barkit/{rel}: {' | '.join(rs)}"
                    for rel, rs in violations.items())
    )


def test_publish_gate_is_not_vacuous():
    """门禁不得退化成恒真（防「扫描面搬家 → 零命中 → 永远通过」）。

    守两条：

    1. 基类 ``core/plugin.py`` 里必须真的存在 ``events.publish`` 调用——发布点若被
       搬家/改名，本用例先红，提醒同步扫描面；
    2. 插件目录里**不得**再出现直连 ``events.publish``——即时广播的唯一合规入口是
       ``self.publish_snapshot()``（§11 阶段 0 收口结果）。
    """
    base = SRC / "core" / "plugin.py"
    assert _publish_calls(base), (
        "基类 core/plugin.py 中找不到 events.publish 调用——发布点已搬走，"
        "请同步更新 PUBLISH_SCAN"
    )
    strays = [
        p.relative_to(SRC).as_posix()
        for p in sorted(PLUGINS.rglob("*.py"))
        if _publish_calls(p)
    ]
    assert not strays, (
        "插件侧出现直连 events.publish（应改为 self.publish_snapshot()）：\n  "
        + "\n  ".join(strays)
    )


def test_publish_gate_detects_violations(tmp_path):
    """门禁自测：越界 topic / 裸 dict 必须被抓（防门禁退化成恒真）。"""
    bad = tmp_path / "bad_pub.py"
    bad.write_text(
        "class P:\n"
        "    def _publish_state(self):\n"
        "        self.ctx.events.publish('ipad_tunnel.state', {'level': 'ok'})\n"
        "        self.ctx.events.publish(self.status_topic(), {'x': 1})\n",
        encoding="utf-8",
    )
    reasons: list[str] = []
    for topic, payload in _publish_calls(bad):
        r = _topic_ok(topic, None)
        if r:
            reasons.append(r)
        r = _payload_ok(payload)
        if r:
            reasons.append(r)
    joined = " | ".join(reasons)
    assert "plugin." in joined and "ipad_tunnel.state" in joined, joined
    assert "裸 dict" in joined, joined
    # 第 2 条调用的 topic 走真源、合法，仍须因裸 dict 被抓——证明两条边界互不遮蔽
    assert reasons.count("禁止广播裸 dict/list，须广播 StateReport") == 2, joined


def test_publish_gate_passes_on_compliant_sample(tmp_path):
    """反向自测：``self.ctx.events.publish(self.status_topic(), self._snapshot())`` 不得误报。"""
    good = tmp_path / "good_pub.py"
    good.write_text(
        "class P:\n"
        "    def publish_snapshot(self):\n"
        "        self.ctx.events.publish(self.status_topic(), self._snapshot())\n",
        encoding="utf-8",
    )
    calls = _publish_calls(good)
    assert len(calls) == 1, calls
    assert _topic_ok(calls[0][0], "p") is None
    assert _payload_ok(calls[0][1]) is None


# ──────────────────────────────────────────────
# 插件包结构门禁（《插件结构规范》§12.2，阶段 1 落地）
#
# 七条共同守「一个插件包长什么样」：目录白名单 / plugin.py 无 PySide6 / 插件间不互
# import / 不重抄已上提的样板 / 设置页必须有 reason 且可构建 / 模板双骨架覆盖。
# 每条按 §12.1 自立四条写：AST 或文件系统事实判定（不用子串）、自带正反自证用例、
# 白名单条目必须「存活 + 带 reason + 只减不增」。
#
# **扫描面的边界（重要，别顺手扩大）**：`test_plugin_py_has_no_pyside6` 只扫
# `src/barkit/plugins/**`。`tests/fixtures/external-plugin-template/plugin.py` 不在扫描面内，
# 这不是疏漏：`core/loader._import_entry` 用 `spec_from_file_location` 把外部插件的
# `plugin.py` 当**顶层独立模块**加载（模块名 `barkit_external_<目录名>_<n>`），它没有
# 包上下文，`from . import settings` 会直接 ImportError——也就是说「UI 移出 plugin.py」
# 对外部插件今天**做不到**，除非先改 loader 的加载语义。故本门禁只覆盖包式内部插件；
# 这条边界同时记在 §12.2。
# ──────────────────────────────────────────────

# ---- §2.3 目录白名单 ----

ALLOWED_PACKAGE_SUFFIXES = (".py", ".md")
ALLOWED_PACKAGE_DIRS = frozenset({"resources", "__pycache__"})

# 精确容忍的 OS 产物：macOS 会在 Finder 访问目录时**非确定性地**生成 / 删除
# ``.DS_Store``（已被 .gitignore 覆盖、未被 git 跟踪）。把它当违规的话，门禁会因
# 「Finder 有没有翻过这个目录」而随机变红——那不是能长期挂 CI 的判据。故只按文件名
# 精确豁免这一个；其余任何「非代码非资料」条目（notes.txt / 随手丢的 .json 快照 /
# ``*.pyc.*`` 残骸 / 临时脚本 / 多余子目录）一律照抓，有自证用例守着。
TOLERATED_PACKAGE_FILES = frozenset({".DS_Store"})


def _package_violations(pkg: Path, *, children_are_packages: bool = False) -> list[str]:
    """扫一个插件包目录里的非白名单条目，返回相对该目录的路径（空 = 合规）。

    ``children_are_packages=True`` 用于 ``plugins/`` 根：它的子目录**就是**插件包本身，
    要交给各自那一轮去查，不在此报成「多余子目录」——这正是 §12.2 提醒
    「另有一个 ``plugins/.DS_Store`` 落在扫描根之外」时说的「门禁须把根上提一层」。
    """
    out: list[str] = []
    for entry in sorted(pkg.iterdir()):
        if entry.is_dir():
            if entry.name in ALLOWED_PACKAGE_DIRS:
                continue                      # 唯一允许的子目录；其内部不受本规范约束
            if children_are_packages:
                continue
            out.append(entry.name + "/")      # 多余子目录：报目录本身，不再往下钻
            continue
        if entry.name in TOLERATED_PACKAGE_FILES:
            continue
        if entry.suffix not in ALLOWED_PACKAGE_SUFFIXES:
            out.append(entry.name)
    return out


def _plugin_package_targets() -> list[Path]:
    """全部待检目录：``plugins/`` 根 + 每个**插件包**子目录（含 ``_template``）。

    ``__pycache__`` / ``resources`` 这类「允许的子目录」不是包，不单独成轮
    （它们的内部本就不受 §2.3 约束，单独一轮只会把 ``*.pyc`` 误报成违规）。
    """
    return [
        PLUGINS,
        *(d for d in sorted(PLUGINS.iterdir())
          if d.is_dir() and d.name not in ALLOWED_PACKAGE_DIRS),
    ]


def test_plugin_package_files():
    """插件包内只允许 `*.py` / `*.md` / `resources/` / `__pycache__/`（§2.3）。"""
    violations = {
        target.relative_to(SRC).as_posix(): hits
        for target in _plugin_package_targets()
        if (hits := _package_violations(target, children_are_packages=(target == PLUGINS)))
    }
    assert not violations, (
        "插件包内出现非白名单条目（§2.3 只允许 *.py / *.md / resources/ / __pycache__/）：\n"
        + "\n".join(f"  src/barkit/{k}: {', '.join(v)}" for k, v in violations.items())
        + "\n改法：代码留包根；资料 *.md 留包根（每包 ≤3 个）；二进制进 resources/；其余删掉。"
    )


def test_plugin_package_gate_detects_planted_violations(tmp_path):
    """门禁自测：注入各类非白名单条目必须被逐个检出。"""
    pkg = tmp_path / "demo"
    (pkg / "resources").mkdir(parents=True)
    (pkg / "__pycache__").mkdir()
    (pkg / "plugin.py").write_text("", encoding="utf-8")
    (pkg / "notes.txt").write_text("", encoding="utf-8")
    (pkg / "state.json").write_text("{}", encoding="utf-8")
    (pkg / "plugin.cpython-312.pyc.987654").write_text("", encoding="utf-8")
    (pkg / "editor").mkdir()
    got = _package_violations(pkg)
    assert got == [
        "editor/", "notes.txt", "plugin.cpython-312.pyc.987654", "state.json",
    ], got


def test_plugin_package_gate_passes_on_compliant_sample(tmp_path):
    """反向自测：白名单内的四种条目 + 容忍的 OS 产物不得被误报。"""
    pkg = tmp_path / "demo"
    (pkg / "resources").mkdir(parents=True)
    (pkg / "__pycache__").mkdir()
    (pkg / "plugin.py").write_text("", encoding="utf-8")
    (pkg / "settings.py").write_text("", encoding="utf-8")
    (pkg / "design.md").write_text("", encoding="utf-8")
    (pkg / "resources" / "hev-socks5-tunnel").write_text("", encoding="utf-8")
    (pkg / "__pycache__" / "plugin.cpython-312.pyc").write_text("", encoding="utf-8")
    (pkg / ".DS_Store").write_text("", encoding="utf-8")
    assert _package_violations(pkg) == []


# ---- §4 跨面禁令 1：插件运行期（plugin.py ＋ 其同包模块）不得 import PySide6 ----
#
# 2026-10-01 起判定面从「一个文件」扩到「入口 ＋ 它的同包运行期闭包」：`route_guard`
# 那轮平铺拆包把正文搬进了 9 个兄弟模块，只扫 `plugin.py` 会让整片新面出圈（红不了，
# 也绿不了 —— 静默失效）。详见 `_plugin_runtime_module_files`。

# 白名单只挂**存量**内部插件，随阶段 2/3 逐个删（§11）；`_template` 已在本阶段
# 直接转正（不挂白名单），所以「模板干净」这件事本身也被下面的用例守着。
#
# **2026-09-28 已清零**（最后一条 `autotype/plugin.py` 迁出）：那 4 处自弹框
# （`QMessageBox` ×3 ＋ `QInputDialog` ×1）能留到今天，唯一理由是「热键与整行点击
# 路径都没有反馈槽可挂」—— 宿主那半边接通后（`ui_kit.defer_result_to_host` ／
# `StatusPanel.set_off_panel_feedback` ／ `hotkey_feedback.wrap_hotkey_callback`），
# 四条路各有了落点，插件侧只返回 `ActionResult` 即可。
# 字典**留着不删**：它是这张门禁的豁免面，清零之后继续留一个空的口，下一个人要加
# 条目时才看得见下面那几条「只减不增」的判例。
PLUGIN_PYSIDE6_WHITELIST: dict[str, str] = {
    # 注：2026-09-17 `route_guard/plugin.py` 已迁出（设置面 → `settings.py`，见
    # 《网络切换设计》§6.3 第 7 步），条目随之删除 —— 只减不增。
    # 注：autotype 已于 2026-09-28 迁出（4 处自弹框 → ActionResult 回执）——
    # 它的 run 期路径是「辅助功能权限 → 输入法 → 倒计时浮层 → 注入」，四处提示
    # 分别落在面板结果槽 / 面板之外的通知 / 热键反馈通道上。条目删除 —— 只减不增。
    # 注：ipad_tunnel 已于 2026-09-18 随「设置面收归」迁出（参考《录屏通知》重构：
    # 设置页 + 运行诊断窗都进 settings.py），条目随之删除 —— 只减不增。
    # 注：recognize 已于 2026-09-19 转正 —— 它那 5 个 import（Qt / QHBoxLayout / QLabel /
    # QPushButton / QWidget）经核对**全仓零使用**，是纯死 import，删掉即合规；窗口接线本就
    # 在 window_view.py、由 plugin.py 注入配置（§7.3「正确范式」），无须迁出。条目删除 —— 只减不增。
    # 注：hosts_guard 已于 2026-09-19 迁出 —— 设置页（连同新补的 resolve_poll 控件、以及那句
    # 内联的 `from barkit.shell.ui_kit import ...`）整体搬进 settings.py。旧 reason 写的
    # 「面板行 + 设置页构建」里，**面板行那半句是误述**：`row_specs()` 本就是纯声明式
    # （`StatusRowSpec(key="main", on_click=...)`，零 Qt），只有设置页真的带 PySide6。
    # 条目删除 —— 只减不增。
}

# 本阶段必须已转正（**不得**挂白名单）的模板入口
TEMPLATE_PLUGIN_REL = "_template/plugin.py"


def _runtime_pyside6_imports(path: Path) -> list[str]:
    """文件里**运行期**会执行的 PySide6 / shiboken6 导入目标（空 = 合规）。

    ``if TYPE_CHECKING:`` 块内的导入**不算**——它运行期不生效，与
    ``test_contribution_depends_only_on_stdlib_and_self`` 的口径一致（那一层也只准
    TYPE_CHECKING 里出现 PySide6）。函数内的惰性 import 不算 guarded，照抓。
    """
    return sorted(
        target
        for target, guarded in _import_targets(path)
        if not guarded and target.split(".")[0] in {"PySide6", "shiboken6"}
    )


#: UI 面的两个落点（§4 那句「UI 一律在 `settings.py` / `<name>_view.py`」的落点就是
#: 它俩）。闭包跟踪到它们就停 —— 它们**本来就该**有 PySide6，跟进去等于换了禁令的对象。
_UI_FACE_NAMES = frozenset({"settings.py"})
_UI_FACE_SUFFIX = "_view.py"


def _is_ui_face(path: Path) -> bool:
    return path.name in _UI_FACE_NAMES or path.name.endswith(_UI_FACE_SUFFIX)


def _plugin_local_imports(path: Path, pkg_dotted: str) -> list[Path]:
    """`path` **运行期**导入到的同包模块文件（包外、TYPE_CHECKING 内的都不返回）。

    两种写法都认：相对（``from .constants import log``）与绝对（``from
    barkit.plugins.route_guard import rules`` / ``…route_guard.rules import X``）。
    ``from <包> import rules`` 那种「模块在包里、模块名落在名字位」的写法必须按
    ``names`` 回退才解析得到 —— 只看 ``module`` 会漏掉整片（本仓两种写法都在用）。
    """
    pkg_dir = path.parent
    found: list[Path] = []

    def _resolve(dotted: str) -> None:
        if dotted != pkg_dotted and not dotted.startswith(pkg_dotted + "."):
            return
        rel = dotted[len(pkg_dotted):].strip(".")
        if not rel:
            return
        cand = pkg_dir.joinpath(*rel.split(".")).with_suffix(".py")
        if cand.is_file() and cand != path:
            found.append(cand)

    def _walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and _is_type_checking(child):
                continue                      # 运行期不生效，连同块内一起跳过
            if isinstance(child, ast.Import):
                for alias in child.names:
                    _resolve(alias.name)
            elif isinstance(child, ast.ImportFrom):
                if child.level:
                    base = pkg_dotted.split(".")
                    base = base[: len(base) - (child.level - 1)]
                    dotted = ".".join(base + ([child.module] if child.module else []))
                else:
                    dotted = child.module or ""
                if dotted == pkg_dotted:
                    for alias in child.names:
                        _resolve(f"{pkg_dotted}.{alias.name}")
                else:
                    _resolve(dotted)
            else:
                _walk(child)

    _walk(ast.parse(path.read_text(encoding="utf-8")))
    return sorted(set(found))


def _plugin_runtime_module_files(entry: Path, pkg_dotted: "str | None" = None) -> list[Path]:
    """入口模块 ＋ 它在**同一个插件包内**运行期导入到的模块（UI 面除外）。

    为什么不是「只扫 `plugin.py` 一个文件」：自 2026-10-01 起 `plugin.py` 只放契约面，
    正文按职责搬进了同包的平铺兄弟模块 —— 那些模块和它**同在内核运行期路径上**。禁令的
    对象是**这个插件的运行期**，不是一个文件名；只扫入口会让「Qt 躲进兄弟模块」变成
    静默假绿（拆包带给路径型门禁的典型失效）。

    为什么只看同包：跨包那几条路由各自的面守着（`core` / `shell` 分层门禁、capabilities
    面）；在这里跟着走会把整张依赖图拉进来，判据也就不再是「这个插件」。
    """
    pkg_dotted = pkg_dotted or (
        "barkit.plugins." + entry.parent.relative_to(PLUGINS).as_posix().replace("/", ".")
    )
    seen = [entry]
    queue = [entry]
    while queue:
        for dep in _plugin_local_imports(queue.pop(0), pkg_dotted):
            if dep in seen or _is_ui_face(dep) or dep.name == "__init__.py":
                continue
            seen.append(dep)
            queue.append(dep)
    return sorted(seen)


def test_plugin_py_has_no_pyside6():
    """插件**运行期路径**不得 import PySide6（§4 跨面禁令 1）。

    UI 一律在 `settings.py` / `<name>_view.py`，入口模块只做契约与接线。判定面是「入口
    模块 ＋ 它在同包内导入到的兄弟模块」—— 见 :func:`_plugin_runtime_module_files`。
    """
    entries = sorted(PLUGINS.rglob("plugin.py"))
    violations: dict[str, list[str]] = {}
    scanned = 0
    for entry in entries:
        rel = entry.relative_to(PLUGINS).as_posix()
        if rel in PLUGIN_PYSIDE6_WHITELIST:
            continue
        closure = _plugin_runtime_module_files(entry)
        scanned += len(closure)
        for path in closure:
            # 报**真出问题的那个文件**，不是入口 —— 拆包之后两者经常不是同一个
            # （「route_guard/plugin.py 违规」这种话会把人引到一份干净的文件上）。
            hits = _runtime_pyside6_imports(path)
            if hits:
                violations[path.relative_to(PLUGINS).as_posix()] = hits
    assert not violations, (
        "插件运行期依赖 PySide6（UI 应移到 settings.py / <name>_view.py，§4）：\n"
        + "\n".join(f"  src/barkit/plugins/{k}: {', '.join(v)}"
                    for k, v in violations.items())
        + "\n存量插件请进 PLUGIN_PYSIDE6_WHITELIST 并写明迁移去向（只减不增）。"
    )
    # 非恒真控制组：扫描面必须**真的进到过兄弟模块**。全仓只剩「入口自己」时这条门禁就
    # 退化成拆包前的样子，而那时它照样是绿的 —— 那是静默失效，不是通过。
    assert scanned > len(entries), (
        f"扫描面 {scanned} 个文件 = 入口数 {len(entries)} ⇒ 闭包没进到任何同包兄弟模块，"
        "Qt 藏在兄弟模块里也抓不到"
    )


def test_plugin_pyside6_gate_covers_sibling_modules(tmp_path):
    """自测：Qt 躲进**同包兄弟模块**时仍必须被抓到，且 UI 面不得被拖进来误报。

    控制组用**真实出错那份形状**：入口自己一行 Qt 都没有，整片违规搬进平铺出来的兄弟
    模块 —— 只扫入口的旧写法在这里是绿的。
    """
    pkg = tmp_path / "plug"
    pkg.mkdir()
    (pkg / "plugin.py").write_text(
        "from .side import build\n"
        "\n"
        "def run():\n"
        "    return build()\n",
        encoding="utf-8",
    )
    (pkg / "side.py").write_text(
        "from PySide6.QtWidgets import QWidget\n"
        "\n"
        "def build():\n"
        "    return QWidget()\n",
        encoding="utf-8",
    )
    dotted = "barkit.plugins.plug"
    closure = _plugin_runtime_module_files(pkg / "plugin.py", dotted)
    assert [p.name for p in closure] == ["plugin.py", "side.py"], closure
    hits = sorted({h for p in closure for h in _runtime_pyside6_imports(p)})
    assert hits == ["PySide6.QtWidgets"], hits

    # 反向：UI 面带 Qt 是**允许**的（§4 的落点就是它俩），闭包必须停在那里 —— 两种
    # 写法（`from <包> import settings` 与 `from .settings import …`）都不得把它拖进来。
    (pkg / "settings.py").write_text(
        "from PySide6.QtWidgets import QWidget\n",
        encoding="utf-8",
    )
    for imp in (f"from {dotted} import settings\n", "from .settings import QWidget\n"):
        (pkg / "plugin.py").write_text(
            imp + "from .side import build\n\n\ndef run():\n    return build()\n",
            encoding="utf-8",
        )
        names = [p.name for p in _plugin_runtime_module_files(pkg / "plugin.py", dotted)]
        assert "settings.py" not in names, f"{imp!r} 把 UI 面拖进了运行期面：{names}"

    # TYPE_CHECKING / 包外导入 / 平铺包内的 `from . import x` 都不得误报。
    (pkg / "helpers.py").write_text("VALUE = 1\n", encoding="utf-8")
    (pkg / "plugin.py").write_text(
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "import barkit.core.plugin\n"
        "from . import helpers\n"
        "from .side import build\n"
        "if TYPE_CHECKING:\n"
        "    from PySide6.QtWidgets import QWidget\n"
        "\n"
        "def run():\n"
        "    return build(), helpers.VALUE\n",
        encoding="utf-8",
    )
    closure = _plugin_runtime_module_files(pkg / "plugin.py", dotted)
    assert [p.name for p in closure] == ["helpers.py", "plugin.py", "side.py"], closure
    assert _runtime_pyside6_imports(pkg / "plugin.py") == [], "TYPE_CHECKING 内的不得误报"


def test_pyside6_whitelist_entries_are_live_and_justified():
    """白名单不得悬空、理由不得为空，且本阶段已转正的对象不得被豁免。"""
    for rel, reason in PLUGIN_PYSIDE6_WHITELIST.items():
        assert (PLUGINS / rel).is_file(), f"白名单指向不存在的文件（悬空豁免）：{rel}"
        assert len(reason.strip()) >= 15, f"白名单 {rel} 的 reason 过简（需说明迁移去向）"
        assert rel != TEMPLATE_PLUGIN_REL, (
            f"{rel} 是阶段 1 必须转正的模板，不得挂白名单（§12.2）"
        )
    assert _runtime_pyside6_imports(PLUGINS / TEMPLATE_PLUGIN_REL) == [], (
        "模板 plugin.py 必须零运行期 PySide6——它是 _template 直接转正的证明（§12.2）"
    )


def test_plugin_pyside6_gate_detects_planted_violations(tmp_path):
    """门禁自测：顶层 import / 函数内惰性 import / shiboken6 必须都被检出。"""
    bad = tmp_path / "plugin.py"
    bad.write_text(
        "from PySide6.QtWidgets import QWidget\n"
        "\n"
        "def build():\n"
        "    from PySide6.QtCore import Qt\n"
        "    from shiboken6 import Shiboken\n"
        "    return QWidget, Qt, Shiboken\n",
        encoding="utf-8",
    )
    got = _runtime_pyside6_imports(bad)
    assert got == ["PySide6.QtCore", "PySide6.QtWidgets", "shiboken6"], got


def test_plugin_pyside6_gate_passes_on_compliant_sample(tmp_path):
    """反向自测：TYPE_CHECKING 里的类型标注 import、相对导入、框架 import 不得误报。"""
    good = tmp_path / "plugin.py"
    good.write_text(
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "from barkit.core.plugin import Plugin\n"
        "from . import settings\n"
        "if TYPE_CHECKING:\n"
        "    from PySide6.QtWidgets import QWidget\n"
        "\n"
        "class Plugin(Plugin):\n"
        "    pass\n",
        encoding="utf-8",
    )
    assert _runtime_pyside6_imports(good) == []


# ---- §4 跨面禁令 3：UI 面不得直接读写配置文件 / 状态文件 ----

#: UI 面判定**不按文件名模式**，理由见 `_ui_files()` —— 这里只列**不是** UI 面的两类：
#: `plugin.py` 是**域面**（插件自己读配置的合法位置，同宿主侧 `shell/` 之于框架），
#: `__init__.py` 不承载界面。
_UI_SCAN_EXCLUDED = frozenset({"plugin.py", "__init__.py"})

#: 配置侧的三个名字（§4 禁令 3 逐字点名：``ctx.config.user_path`` 及其 ``read_text`` /
#: ``yaml.safe_load`` / ``save_user``）。``safe_load`` 按属性名抓，故 ``yaml.safe_load``
#: 与 ``from yaml import safe_load`` 两种写法都中。
_BANNED_UI_CONFIG_ATTRS = frozenset({"user_path", "save_user", "safe_load"})

#: 文件读写调用：状态文件在 UI 里的表现形式就是一次 ``read_text``（存量那处正是
#: ``json.loads(paths.HOSTS_STATE_FILE.read_text(...))``）。``open`` 不在此列 —— 它太宽，
#: 会把「UI 里打开一个和配置无关的东西」也判红，而门禁要能长期挂 CI（§12.1）。
_BANNED_UI_FILE_CALLS = frozenset({"read_text", "read_bytes", "write_text", "write_bytes"})


def _ui_io_hits(path: Path) -> list[str]:
    """扫一个 UI 面文件里的「直接读写配置 / 状态文件」，返回 ``行号: 说明``（空 = 合规）。

    **AST 判定而不是子串**：本仓的 UI 模块 docstring 里**就要**提这些名字（例如
    `_template/settings.py` 写「不得自己读用户 YAML 再 ``save_user``」、本组件类
    docstring 里写着原先那处违规长什么样）—— 子串扫描会把说明本身判成违规，
    那是「文档越老实、门禁越红」的荒唐判据。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _BANNED_UI_CONFIG_ATTRS:
            hits.append(f"{node.lineno}: 直接碰配置（{node.attr}）")
        elif isinstance(node, ast.Call):
            fn = node.func
            name = (
                fn.attr if isinstance(fn, ast.Attribute)
                else fn.id if isinstance(fn, ast.Name)
                else ""
            )
            if name in _BANNED_UI_FILE_CALLS:
                hits.append(f"{node.lineno}: 直接读写文件（{name}）")
    return sorted(hits, key=lambda s: int(s.split(":", 1)[0]))


def _ui_files() -> list[Path]:
    """插件包的 UI 面文件 —— **按 AST 事实判**：import 了 PySide6 的模块。

    判据从「文件名模式」换成 AST，因为规范 §7.3 的运行期面有**两小类**：独立窗口
    `<name>_view.py` **与构件（不带 `_view`）**。2026-09-20 实证：`recognize/selview.py`
    是 `QGraphicsView` 子类、规范明文判它「构件不适用后缀」，而旧模式
    `("settings.py", "*_view.py", "*dialog*.py")` 对它**零命中** ⇒ 它落在扫描面外，
    往里写配置直读不会有人红（植入实测：同一行放进 `window_view.py` 会红、放进它不会）。
    按 AST 判之后新写法自动受管，不必记得往模式里补一条 —— 那个已废除的
    `*dialog*.py` 若回潮，只要用 Qt 也照样在内。

    ⚠️ 2026-10-02 补一层**同包相对导入闭包**：`route_guard/settings.py` 平铺拆包后，
    那批混合类各自成了同目录的兄弟模块，而其中 4 个（`settings_draft` / `settings_pin` /
    `settings_order` / `settings_netform`）**一个 Qt 名字都不 import** —— 它们是
    ``_SceneEditor(QWidget)`` 的一部分，却因为「按 AST 判」这条代理判据自动落到了
    扫描面外 ⇒ 往 `save()` 那一段里写一句 `read_text()` 三条门禁（配置 IO / 模态框 /
    裸值控件）**一条都不会红**。这是**第六种**路径型静默失效：判据的代理（「import 了
    PySide6」）被拆包撕开，而拆之前它恰好等价于「是 UI」。

    闭包只跟**相对**导入（§2.4：包内自引用一律相对）：绝对导入拉进来的那个是**领域
    模块**（如 `route_guard/rules.py`），它不是 UI，跟进去等于换了禁令的对象 —— 与
    `_plugin_runtime_module_files` 停在 UI 面是同一条边界，方向相反。
    """
    seeds: list[Path] = []
    for path in sorted(PLUGINS.rglob("*.py")):
        if "__pycache__" in path.parts or path.name in _UI_SCAN_EXCLUDED:
            continue
        if _runtime_pyside6_imports(path):
            seeds.append(path)

    seen: set[Path] = set(seeds)
    stack = list(seeds)
    while stack:
        path = stack.pop()
        pkg = path.parent
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ImportFrom) or node.level < 1:
                continue
            base = pkg if node.level == 1 else path.parents[node.level - 1]
            for cand in ([base / (node.module.replace(".", "/") + ".py")] if node.module else []) \
                    + [base / f"{a.name}.py" for a in node.names]:
                if cand.exists() and cand not in seen and cand.name not in _UI_SCAN_EXCLUDED:
                    seen.add(cand)
                    stack.append(cand)
    return sorted(seen)


def test_no_config_io_in_ui():
    """插件 UI 面**不得**直接读写配置文件 / 状态文件（《插件结构规范》§4 跨面禁令 3）。

    规范原文：UI 一律不直接读写 config 文件（``ctx.config.user_path`` 及其 ``read_text`` /
    ``yaml.safe_load`` / ``save_user``），须经插件方法；**同类问题还有 UI 直读状态文件**。

    这条门禁是 §12.2「待建门禁」表里 `test_no_config_io_in_ui` 的落地，**零白名单**：
    表里记的 9 处存量在落地时已自然消化到 1 处 —— 三份 ``config_dialog.py`` 里的 8 处随
    `route_guard`（2026-09-17 第 7 步）、`ipad_tunnel`（2026-09-18 设置面收归）与
    `autotype`（2026-09-19 设置面收归）**整文件删除**而不存在了，剩下那 1 处
    （`hosts_guard` 的管理窗直读 ``hosts_state.json``，其模块名已于 2026-09-19 改为
    `manager_view.py`）本批改为经 ``plugin.status_index()``，故不需要挂账名单。

    扫描面**只含插件包**（`src/barkit/plugins/**`）：宿主自己的 UI（`shell/settings.py` 等）
    是配置的**合法**消费者（它按 `Config` 的公开接口读写，不是绕过它直读文件），拿这条去
    扫宿主会把正确的设计判红。面内成员由 `_ui_files()` **按 AST 判**（import 了 PySide6
    的模块，除域面 `plugin.py`）—— 不用文件名模式，理由见那个函数。
    """
    violations: dict[str, list[str]] = {}
    for path in _ui_files():
        hits = _ui_io_hits(path)
        if hits:
            violations[path.relative_to(SRC).as_posix()] = hits
    assert not violations, (
        "插件 UI 文件里出现直接读写配置 / 状态文件（须经插件方法，§4 禁令 3）：\n"
        + "\n".join(f"  src/barkit/{k}: {', '.join(v)}" for k, v in violations.items())
        + "\n状态数据请由 plugin.py 提供一个只读访问器（如 `hosts_guard.status_index`），"
          "UI 只消费它的返回值。"
    )


def test_ui_config_io_gate_detects_planted_violations(tmp_path):
    """门禁自测：配置侧三个名字 ＋ 读写文件两种形态都要被抓到（§12.1「植入必抓」）。"""
    bad = tmp_path / "settings.py"
    bad.write_text(
        "import yaml\n"
        "\n"
        "def build(ctx, paths):\n"
        "    ctx.config.save_user({'a': 1})\n"
        "    p = ctx.config.user_path\n"
        "    yaml.safe_load(p.read_text(encoding='utf-8'))\n"
        "    paths.HOSTS_STATE_FILE.write_text('{}', encoding='utf-8')\n",
        encoding="utf-8",
    )
    got = _ui_io_hits(bad)
    # 5 条：save_user / user_path / safe_load（配置侧三个名字）＋ read_text / write_text
    # （读写文件的两种形态；第 6 行同时踩了两个，故行号会重复出现）。
    assert len(got) == 5, got
    for needle in ("save_user", "user_path", "safe_load", "read_text", "write_text"):
        assert any(needle in g for g in got), (needle, got)


def test_ui_config_io_gate_passes_on_compliant_sample(tmp_path):
    """反向自测：**docstring 里提到那些名字不算违规**，插件方法的调用也不算（§12.1「不误报」）。

    这条反向用例是这套判据的命门：门禁若改成子串扫描，本文件立刻会因自己的说明文字变红
    —— 而「照规范做事的人被自己的文档判违规」正是门禁最该避免的失败。
    """
    good = tmp_path / "settings.py"
    good.write_text(
        '"""规范说：UI 不得自己 save_user / 读 user_path / yaml.safe_load —— 那条是禁令本体。"""\n'
        "\n"
        "def build(plugin):\n"
        "    entries = plugin.status_index()          # ✅ 经插件方法\n"
        "    return plugin.save_rules(entries)        # ✅ 经插件方法\n",
        encoding="utf-8",
    )
    assert _ui_io_hits(good) == []


# ---- §4 跨面禁令 4：插件 UI 不得自建滚动区（页级滚动区由框架统一给）----

#: 自建滚动区的类名。本禁令**只扫插件包**：宿主侧 `shell/panel/panel.py` 的
#: 状态区滚动区是合法的（面板是浮层，外面没有页级滚动区），理由见用例 docstring。
_BANNED_UI_SCROLL_CLASSES = frozenset({"QScrollArea"})


def _ui_scroll_hits(path: Path) -> list[str]:
    """扫一个 UI 面文件里「自建滚动区」的实例化，返回 ``行号: 说明``（空 = 合规）。

    只判**调用**（``QScrollArea(...)`` 与 ``QtWidgets.QScrollArea(...)`` 两种写法）。
    光 import 不算自建（那是 pyflakes 的事），写成类型标注也不算 —— 门禁要抓的是
    「页里真多了一层滚动」。注释与 docstring 里提它当然不算（AST 看不见注释），
    而本仓就有几处注释**必须**提它（那正是「这层被删了」的说明）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = (
            fn.attr if isinstance(fn, ast.Attribute)
            else fn.id if isinstance(fn, ast.Name)
            else ""
        )
        if name in _BANNED_UI_SCROLL_CLASSES:
            hits.append(f"{node.lineno}: 自建滚动区（{name}）")
    return sorted(hits, key=lambda s: int(s.split(":", 1)[0]))


def test_plugin_ui_does_not_build_its_own_scroll_area():
    """插件 UI 面**不得**自建 `QScrollArea`：页级滚动区由框架统一给，再包一层就是两层。

    **实测证据**（2026-10-06，用户报「设置里的路由条目不应该再有第二层滚动条」）。
    同一形态在插件面共 6 处，删前逐个量化（宿主取真实窗下限）：

    | 位置 | 宿主 | 删前实测 |
    |---|---|---|
    | `route_guard/settings_chrome.py` | 详情卡（window） | 两根竖条：框架那条 `maximum=357` ＋ 清单那条 `maximum=380`；内层只分到 112px |
    | `clock/settings_list.py` | 设置页分页 | 内层只分到 72px，而内容要 419px |
    | `autotype/settings.py` | 设置页分页 | 同上：72px / 419px |
    | `todo/settings.py` | 设置页分页 | 内层 176px / 315px |
    | `notify_guard/settings_edit.py` | 设置页分页（编辑屏） | **两根竖条**：外层 `maximum=375` ＋ 内层 `maximum=1074`（h=384） |
    | `recognize/view_rows.py` | 详情卡（window） | 视口只有 149px，而 60 条结果要 1409px |

    **为什么「两层」是缺陷**：滚轮只驱动鼠标底下那一条 —— 鼠标停在内层清单上就只滚清单，
    「上面还有内容」看不见。内层那条还会被压成固定的几十像素小窗（它的 `sizeHint` 是 0，
    跟卡片一起分剩余高度），用户永远只看得到两三行。页级那一条本来就够：设置窗每个分页
    由 `shell/settings/helpers.py::_scrolled` 包，`window` 形态的详情卡由
    `shell/detail.py::_scrolled` 包。插件这边要做的是**按内容自然长高**
    （列表收尾那句 `addStretch(1)` 保证行贴着顶部），滚动交给外面那一层。

    **扫描面只含插件包**（`_ui_files()`）：宿主侧 `shell/panel/panel.py` 自己也建了一个
    `QScrollArea`（面板状态区），那是**合法**的 —— 面板是浮层，外面没有页级滚动区。
    拿这条去扫宿主会把正确的设计判红。

    与 `tests/test_settings.py::test_plugin_settings_page_is_wrapped_in_a_scroll_area`
    是一对：那条判「框架包了没」，本条判「插件没自己再包」。
    """
    violations: dict[str, list[str]] = {}
    for path in _ui_files():
        hits = _ui_scroll_hits(path)
        if hits:
            violations[path.relative_to(SRC).as_posix()] = hits
    assert not violations, (
        "插件 UI 文件里出现自建滚动区（页级滚动区已由框架给，再包一层就是两层竖条）：\n"
        + "\n".join(f"  src/barkit/{k}: {', '.join(v)}" for k, v in violations.items())
        + "\n清单按内容自然长高即可（收尾的 `addStretch(1)` 保证行贴顶），滚动交给外面那层。"
    )


def test_ui_scroll_gate_detects_planted_violations(tmp_path):
    """门禁自测：两种实例化写法都要抓到，光 import 与类型标注不算（§12.1「植入必抓」）。"""
    bad = tmp_path / "settings.py"
    bad.write_text(
        "from PySide6.QtWidgets import QScrollArea, QVBoxLayout\n"
        "from PySide6 import QtWidgets\n"
        "\n"
        "def build(card, box):\n"
        "    one = QScrollArea(card)\n"
        "    two = QtWidgets.QScrollArea(card)\n"
        "    return one, two\n",
        encoding="utf-8",
    )
    got = _ui_scroll_hits(bad)
    assert len(got) == 2, got
    assert got[0].startswith("5:"), got
    assert got[1].startswith("6:"), got


def test_ui_scroll_gate_passes_on_compliant_sample(tmp_path):
    """反向自测：注释 / docstring 提到它不算违规，类型标注也不算（§12.1「不误报」）。

    这条是判据的命门：本仓插件里就有注释**必须**写 `QScrollArea`（「原先这里套了一层，
    2026-10-06 删」）—— 门禁若改成子串扫描，照规范做事的人会被自己的说明文字判违规。
    """
    good = tmp_path / "settings.py"
    good.write_text(
        '"""本页不得自建 QScrollArea —— 页级那一条由框架给。"""\n'
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from PySide6.QtWidgets import QScrollArea\n"
        "\n"
        "def build(card, box):\n"
        "    # 原先这里套了个 QScrollArea，2026-10-06 删\n"
        "    card.addWidget(box)\n",
        encoding="utf-8",
    )
    assert _ui_scroll_hits(good) == []


def test_ui_scan_is_by_ast_not_filename_patterns():
    """扫描面自证：**UI 面按 AST 事实定，且真的收得住「运行期构件」**。

    防的是「门禁悄悄失效」，而且防得比旧版更狠 —— 旧版只断言「点名的两个名字在不在」，
    对「**整类文件漏在扫描面外**」是瞎的：2026-09-20 实测 `recognize/selview.py`
    （`QGraphicsView` 子类、规范 §7.3 明文判它是「构件、不带 `_view`」）不匹配旧的三模式，
    往里写 `ctx.config.user_path` 门禁不红；同一行放进 `window_view.py` 会红。

    所以这里有一条**照旧模式反向对照**的断言：扫描面里必须存在**不匹配任何旧模式**的
    文件。没有它，这条用例在扫描面退回「按文件名」时照样绿 —— 而那正是要防的事。
    `plugin.py` 是**读写的合法位置**（域面），必须不在扫描面内。
    """
    files = _ui_files()
    assert files, "扫描面为空 —— 门禁等于失效"
    names = {p.name for p in files}
    assert "settings.py" in names, "设置面必须在内"
    assert any(n.endswith("_view.py") for n in names), "运行期窗口必须在内"

    legacy = ("settings.py", "*_view.py", "*dialog*.py")
    components = [p for p in files if not any(p.match(pat) for pat in legacy)]
    assert components, (
        "扫描面里没有任何「不匹配旧命名模式」的运行期构件 —— 要么构件写法真的没了，"
        "要么扫描面退回了按文件名模式（后者正是本用例要防的）"
    )

    assert all(p.name not in _UI_SCAN_EXCLUDED for p in files), sorted(names)
    assert all(p.parent != PLUGINS for p in files), "不该扫到 plugins/ 根下的散文件"

    # 反向控制组（2026-10-02 形态 F）：扫描面里必须有**不带 Qt 却被 UI 拉进来**的兄弟
    # 模块。没有这一条，相对导入闭包哪天断了（比如 UI 改用绝对导入）照样是绿的 ——
    # 而那正是平铺拆包撕开的那个口子（混合类一个 Qt 名字都不 import）。
    qt_free = [p for p in files if not _runtime_pyside6_imports(p)]
    assert qt_free, (
        "扫描面里没有「不带 Qt 却被 UI 拉进来的兄弟模块」—— 相对导入闭包没起作用，"
        "平铺拆包那批混合类（settings_draft / settings_pin / settings_order / "
        "settings_netform）会悄悄出圈"
    )


# ---- §10 工程纪律：源码字符串里不得出现 emoji（图标一律走 shell/design/icons.py）----

#: emoji 码点（≈ Unicode ``Extended_Pictographic``）。**刻意不含排版符号** —— `→` `↑`
#: `⇒` `●` `◷` `《》` 在本仓的 UI 文案与注释里是合法排版（箭头表流转、`●` 表当前项、
#: `◷` 表「某日修订」），把它们判红只会逼出一张豁免表，而豁免表会长成第二份规范。
_EMOJI_RANGES = (
    (0x203C, 0x203C), (0x2049, 0x2049), (0x2122, 0x2122), (0x2139, 0x2139),
    (0x2194, 0x2199), (0x21A9, 0x21AA),
    (0x231A, 0x231B), (0x2328, 0x2328), (0x23CF, 0x23CF), (0x23E9, 0x23F3),
    (0x23F8, 0x23FA), (0x24C2, 0x24C2),
    (0x25AA, 0x25AB), (0x25B6, 0x25B6), (0x25C0, 0x25C0), (0x25FB, 0x25FE),
    (0x2600, 0x27BF),                                  # 杂项符号 + 装饰符号（⚠ ✅ ❌ ✍ …）
    (0x2934, 0x2935), (0x2B05, 0x2B07), (0x2B1B, 0x2B1C), (0x2B50, 0x2B50),
    (0x2B55, 0x2B55), (0x3030, 0x3030), (0x303D, 0x303D), (0x3297, 0x3297),
    (0x3299, 0x3299), (0xFE0F, 0xFE0F), (0x200D, 0x200D),
    (0x1F000, 0x1FAFF),
)

#: 逐个字符的例外（**只列字符本身与理由，不按文件开豁免**——按文件开就是在挂账）：
#: ``✕`` U+2715 MULTIPLICATION X —— 场景里「删除这一条」的按钮标签，纯字形、无 emoji 属性，
#: 落在上面那个装饰符号区间里纯属相邻。
#: ``↩`` U+21A9 LEFTWARDS ARROW WITH HOOK（2026-09-23）：设置窗「快捷键」页按**系统写法**
#: 印键位（⌃⌥⇧⌘ 加键名），它是**文字**不是图标 —— 换成 "Return" 就得把键位印成英文
#: 单词，与系统偏好里那套对不上。**其余键位字形本就不在任何区间内**（⌘ U+2318 /
#: ⌃ U+2303 / ⌥ U+2325 / ⇧ U+21E7 / ␣ U+2423 / ⎋ U+238B / ⌫ U+232B / ⇥ U+21E5 /
#: ←→↑↓ U+2190–2193），故不列 —— 例外表只收「真会撞上区间的那一个」。
_EMOJI_ALLOWED = frozenset({0x2715, 0x21A9})


def _is_emoji(ch: str) -> bool:
    o = ord(ch)
    if o in _EMOJI_ALLOWED:
        return False
    return any(lo <= o <= hi for lo, hi in _EMOJI_RANGES)


def _docstring_constants(tree: ast.AST) -> set[int]:
    """模块 / 类 / 函数的 docstring 那几个字符串节点（``id()`` 集合），用于排除。"""
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            out.add(id(body[0].value))
    return out


def _emoji_in_strings(path: Path) -> list[str]:
    """扫一个源文件**字符串字面量**里的 emoji，返回 ``行号: 字符``（空 = 合规）。

    **只看字符串字面量、且排除 docstring**：本仓的注释与 docstring 大量用 ``⚠️`` / ``⇒``
    做排版强调（尤其中文技术文档那类「口径真源就在这」的段落），全文扫描会把它们一起判红
    —— 那是「注释越老实、门禁越红」的荒唐判据。真正要拦的是**字符串里的 emoji**：它会原样
    出现在用户眼前，而本仓的规矩是图标一律走 `shell/design/icons.py`。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstring_constants(tree)
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in skip:
            continue
        for ch in node.value:
            if not _is_emoji(ch):
                continue
            desc = f"{node.lineno}: {ch!r} U+{ord(ch):04X} {unicodedata.name(ch, '?')}"
            if desc not in hits:                        # 同一行同一字符只报一次
                hits.append(desc)
    return sorted(hits, key=lambda s: int(s.split(":", 1)[0]))


def test_emoji_gate_spares_the_return_glyph_but_catches_its_neighbour(tmp_path):
    """放行 ``↩`` 是**按字符**放行的，不是把那一段区间整段关掉。

    ``↪`` U+21AA 与它同在一个区间、同为「带钩的箭头」，但不是键位字形 ⇒ **仍要被抓**。
    这条在，例外表才不会长成第二个「装饰符号都行」。
    """
    probe = tmp_path / "glyph.py"
    probe.write_text(
        "A = '↩'\n"      # 键位字形：放行
        "B = '↪'\n",     # 同区间的邻居：仍要报
        encoding="utf-8",
    )
    assert _emoji_in_strings(probe) == [f"2: '↪' U+21AA {unicodedata.name('↪', '?')}"]


def test_no_emoji_in_source():
    """源码**字符串**里不得出现 emoji（《主框架架构-工程与门禁》§10）。

    依据：`contribution/specs.py::IconName` 的类 docstring ——「禁止插件使用 emoji，
    资源由 shell/design/icons.py 统一提供」。同一条纪律对**宿主**一样成立：同一个进程、
    同一套图标库，宿主自己拿 emoji 当图标，等于把那条规矩的示范作用抵消掉。

    扫描面是**整个** `src/barkit`（不是只扫插件）：2026-09-19 清掉的 12 处里有 6 处在宿主
    `shell/`，其中一处还是**行图标**（autotype 自绘行传了个 emoji 键盘）—— 只扫插件会漏掉
    一半，而漏掉的那一半恰好是「给插件看」的那一半。
    """
    violations: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        hits = _emoji_in_strings(path)
        if hits:
            violations[path.relative_to(SRC).as_posix()] = hits
    assert not violations, (
        "源码字符串里出现 emoji（图标请改用 shell/design/icons.py 的 IconName）：\n"
        + "\n".join(
            f"  {p}\n" + "\n".join(f"      {h}" for h in hs)
            for p, hs in violations.items()
        )
    )


def test_emoji_gate_detects_planted_violations(tmp_path):
    """自证：字符串里的 emoji 必被抓，**同文件的 docstring 与注释不误报**。"""
    bad = tmp_path / "bad.py"
    bad.write_text(
        '"""模块 docstring：⚠️ 在这里只是排版强调。"""\n'
        "# 注释里的 ⚠ ✅ 同样不算\n"
        "X = '完成 ✅'\n"
        "Y = '⌨️'\n"
        "Z = '⚠ 提醒'\n",
        encoding="utf-8",
    )
    lines = [h.split(":", 1)[0] for h in _emoji_in_strings(bad)]
    # 只报字符串那三行；`⌨️` 是 键盘 + VS16 两个码点，故第 4 行出现两条。
    assert lines == ["3", "4", "4", "5"], _emoji_in_strings(bad)


def test_emoji_gate_passes_on_compliant_sample(tmp_path):
    """自证：本仓**有意保留**的排版符号（→ ⇒ ● ◷ ✕）不得被判红。"""
    good = tmp_path / "good.py"
    good.write_text(
        "def f():\n"
        "    return {\n"
        "        'sub': '守护 v1.2.0 → v2.0.0',\n"
        "        'mode': '● 自动（当前）',\n"
        "        'note': '◷ 2026-09-19 已落地 ⇒ 见台账',\n"
        "        'del': '✕',\n"
        "    }\n",
        encoding="utf-8",
    )
    assert _emoji_in_strings(good) == []


def test_emoji_gate_scans_the_whole_source_tree():
    """扫描面自证：`src/barkit` **两个面**都在内（宿主 shell ＋ 插件包）。

    只留一个面时它会一路绿灯，而漏掉的那一面（2026-09-19 那 12 处里占 6 处）没人看顾。
    """
    files = [p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts]
    assert len(files) > 100, len(files)
    assert any("shell" in p.parts for p in files), "宿主 UI 必须在内"
    assert any("plugins" in p.parts for p in files), "插件包必须在内"


# ---- §10 工程纪律：不得硬编码颜色（颜色一律走 shell/design/tokens.py 的令牌）----

#: 扫描面的**唯一边界**：令牌定义处。这**不是豁免** —— 豁免是「这个文件虽然违规但放行」，
#: 而它是「判据要指向的那个真源」：把它扫进来等于要求令牌表不许定义令牌，门禁自相矛盾。
#: 写死成**一个常量**而不是一张表，是为了让「还想再排除谁」必须动到这一行附近（见自证
#: `test_hardcoded_color_scan_boundary_is_only_the_token_source`），豁免表长不大。
_COLOR_SOURCE = SRC / "shell" / "design" / "tokens.py"


def _color_scan_targets() -> list[Path]:
    """颜色门禁的扫描面 ＝ **整个产品源码树** `src/barkit/**`，减去唯一的边界 `_COLOR_SOURCE`。

    2026-09-19 前只扫 `plugins/**`，理由是「宿主 `shell/` 是令牌的实现方，扫它没有意义」。
    那条理由已被收口的事实推翻：`styles.py` / `ui_kit.py` / `icons.py` 一路清下来之后，
    全仓的颜色字面量**只剩 `_COLOR_SOURCE` 一个文件** —— 要排除的是**定义处本身**，不是
    某一层的特权，于是「按层划分扫描面」自然收敛成「按角色划分」。

    `tests/**` **不在**扫描面内：那里 21 处字面量（`test_shell_design.py` 的令牌值断言、
    本文件自证样例里的 `#30d158`）**正是被测对象** —— 测试要能写下「令牌现在等于几」，
    这跟产品代码里「绕过令牌写一个色」是两回事，判据不同，不是豁免。
    """
    return [p for p in _py_files() if p != _COLOR_SOURCE]


#: 颜色字面量：`#RGB` / `#RGBA` / `#RRGGBB` / `#RRGGBBAA` ＋ `rgb()/rgba()/hsl()/hsla()`。
#: 长度**逐档枚举**而不是 `{3,8}`：`#abcde`（5 位）这种随手写的片段不该被判红，
#: 而 `\b` 已经挡住了「`#abc` 后面还跟着字母」的粘连情形。
_COLOR_LITERAL_RE = re.compile(
    r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})\b"
    r"|\b(?:rgba?|hsla?)\s*\("
)


def _color_literals_in_strings(path: Path) -> list[str]:
    """扫一个源文件**字符串字面量**里的颜色值，返回 ``行号: 片段``（空 = 合规）。

    **只看字符串字面量、且排除 docstring**（与 emoji 门禁同一条判据）：本仓的注释与
    docstring 里到处是「原先写死 ``#9aa0a6``，现改取令牌」这类说明性举例，全文扫描会把
    它们一起判红 —— 那是「注释越老实、门禁越红」。真正要拦的是会被塞进 QSS /
    内联 HTML 的那种字面量：它会原样出现在用户眼前，而且**绕开换肤**。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstring_constants(tree)
    found: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in skip:
            continue
        for m in _COLOR_LITERAL_RE.finditer(node.value):
            found.add((node.lineno, m.group(0)))
    # 按行号排序再输出：``ast.walk`` 是**广度优先**的，f-string 里的片段（``JoinedStr``
    # 的子节点）会比后面几行的普通字符串**后**访问到 —— 不排序的话报告顺序与代码顺序对不上。
    return [f"{lineno}: {frag!r}" for lineno, frag in sorted(found)]


def test_no_hardcoded_colors():
    """不得写死颜色（《主框架架构-工程与门禁》§10 清单里的那一条）。

    颜色的唯一真源是 `shell/design/tokens.py`（浅/深各一份，`shell/styles.py` 渲成 QSS）。
    写死一个字面量就等于**绕开换肤**：系统切深浅时它不动。本仓真出现过三种形态 ——
    ① 照抄**深色**令牌的值（`ipad_tunnel` 诊断卡片那四个状态色），浅色主题下用的是深色的
    绿 / 红 / 橙 / 灰；② 早期手调、**不在任何一套令牌里**的灰（`recognize` 浮层提示
    `#9aa0a6`）；③ 宿主侧把**浅色**令牌的值当通用灰（`shell/ui_kit.note()` 与 `settings.py`
    共 5 处 `#6E6E73`，深色主题下一直用着浅色值）。三种都不报错、不崩溃，只会「看着有点
    怪」—— 正是最需要门禁的那种缺陷；③ 之所以能潜伏到 2026-09-19，正是因为当时扫描面
    只到 `plugins/**`。

    扫描面 ＝ `_color_scan_targets()`（`src/barkit/**` 除令牌定义处那一个文件）。**别再退回
    「按层划分」**：宿主与插件共用同一套令牌，判据就该同一条。
    """
    violations: dict[str, list[str]] = {}
    for path in _color_scan_targets():
        hits = _color_literals_in_strings(path)
        if hits:
            violations[path.relative_to(SRC).as_posix()] = hits
    assert not violations, (
        "源码里出现硬编码颜色（请改取 shell/design/tokens.py 的令牌）：\n"
        + "\n".join(
            f"  {p}\n" + "\n".join(f"      {h}" for h in hs)
            for p, hs in violations.items()
        )
    )


def test_hardcoded_color_scan_boundary_is_only_the_token_source():
    """自证：扫描面**只**排除一个文件，且被排除的确实是令牌定义处。

    挡两件事：① 「豁免表悄悄长大」—— 想再排掉第二个文件，必须改 `_color_scan_targets()`，
    这条会把「排除集不止一个」当场判红（＝一次显式决定，而非顺手放行）；② 边界**指错地方**
    （文件改名 / 挪位后，老常量会静默跳过不存在的路径，扫描面反而**变宽**却没人发现）——
    故断言它存在、且模块级真的定义了 `TOKENS`。顺带确认扫描面**真覆盖到**了原先不在内的
    宿主层（否则「扩面」是句空话）。
    """
    assert _COLOR_SOURCE.is_file(), f"边界指向了不存在的文件：{_COLOR_SOURCE}"

    # 它必须真定义令牌，而不是「随便指的某处」
    tree = ast.parse(_COLOR_SOURCE.read_text(encoding="utf-8"))
    assigned: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):            # TOKENS = {...}
            assigned |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assigned.add(node.target.id)            # TOKENS: dict[...] = {...}
    assert "TOKENS" in assigned, f"{_COLOR_SOURCE.name} 里没有模块级 TOKENS 赋值"

    # 面既不能多、也不能少。写成一个 `==` 也等价，但**分开报**是因为两种坏法差很远：
    # 合在一起时，「面被收窄」会报出「排除项不止一个文件」—— 报文与实情正相反（第 4 轮
    # 反证实测：把面改成只扫 `shell/` 时，先红的就是那句错话）。另外此处**故意不再逐条
    # 点名**「`shell/styles.py` 等必须在面内」：那样的断言永远轮不到自己红（被下面这条
    # `missing` 完全遮住），是**假守**；要证明「扩面真的扩到了」，看的就是 `missing` 为空。
    targets = _color_scan_targets()
    expected = set(_py_files()) - {_COLOR_SOURCE}
    targets_set = set(targets)
    assert _COLOR_SOURCE not in targets_set, "边界文件落回了扫描面内（真源不该被扫）"
    missing = expected - targets_set
    assert not missing, (
        "扫描面比预期窄 —— 有文件被漏掉（要么多了豁免，要么面被收窄）：\n"
        + "\n".join(f"  {p.relative_to(SRC).as_posix()}" for p in sorted(missing))
    )


def test_hardcoded_color_gate_detects_planted_violations(tmp_path):
    """自证：字符串里的颜色必被抓，**同文件的注释与 docstring 不误报**。"""
    bad = tmp_path / "bad.py"
    bad.write_text(
        '"""模块 docstring：原先写死 #9aa0a6，后改取令牌。"""\n'
        "# 注释里的 #30d158 与 rgb(1,2,3) 同样不算\n"
        "A = '#30d158'\n"
        "B = f\"color:{t['text2']}; background:#fff;\"\n"
        "C = 'rgba(0, 0, 0, 0.3)'\n",
        encoding="utf-8",
    )
    lines = [h.split(":", 1)[0] for h in _color_literals_in_strings(bad)]
    assert lines == ["3", "4", "5"], _color_literals_in_strings(bad)


def test_hardcoded_color_gate_passes_on_compliant_sample(tmp_path):
    """自证：令牌取值与**非颜色**的 `#` 片段（URL 锚点、5 位 hex、`#12`）不得判红。"""
    good = tmp_path / "good.py"
    good.write_text(
        "def f(t):\n"
        "    return (\n"
        "        f\"color:{t['text2']}; font-size:11.5px;\",\n"
        "        'https://example.com/doc#section',\n"
        "        '#abcde',\n"
        "        '#12',\n"
        "    )\n",
        encoding="utf-8",
    )
    assert _color_literals_in_strings(good) == []


# ---- §10 工程纪律：插件侧不得自拼 QSS（文字样式走 shell/ui_kit 的排版角色）----


def _inline_stylesheet_calls(path: Path) -> list[str]:
    """扫一个源文件里**真调用** ``setStyleSheet`` 的位置，返回 ``行号: 目标``（空 = 合规）。

    **判据走 AST 而不是子串**：本仓的注释与 docstring 里**必须**能提 ``setStyleSheet``
    （`ui_kit` 里那些「样式在调用时刻烤进控件」的说明、本门禁自己的 docstring 都要提它），
    子串扫描会把它们一起判红 —— 「注释越老实、门禁越红」。要拦的是**真去设样式表**的调用。

    ``.setStyleSheet(...)`` 与裸名 ``setStyleSheet(...)`` 两种写法都收（后者只可能来自
    ``from … import setStyleSheet``）；``getattr(w, "setStyleSheet")(…)`` 这类绕法不追 ——
    门禁的作用是堵住**顺手写**的那条路，不是防人刻意绕过。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr == "setStyleSheet":
            hits.append((node.lineno, ast.unparse(fn)))
        elif isinstance(fn, ast.Name) and fn.id == "setStyleSheet":
            hits.append((node.lineno, fn.id))
    return [f"{lineno}: {target}" for lineno, target in sorted(hits)]


def test_no_inline_stylesheet():
    """插件侧不得自拼 QSS（《主框架架构-工程与门禁》§10 清单里的那一条）。

    依据：`shell/ui_kit.py` 的排版角色表（``apply_text_role``）与 `shell/styles.py` 的
    ``build_stylesheet`` —— 文字样式各有单一来源。插件里自拼一串 ``font-size:13px;
    font-weight:600;`` 的后果不是报错，而是**同一个「小节标题」在 6 个设置页里各写一遍**：
    改一档字号要满仓 grep，漏掉的那一处没人看得出来（清理前实测：11.5px 的小字 9 处、
    13px 的小节标题 5 处、行尾「›」2 处）。

    扫描面＝`plugins/**` 的**全部** .py：宿主 `shell/` 是样式表的**实现方**（QSS 的家就在
    `styles.py` / `ui_kit.py`），扫它没有意义。宿主侧**也走同一张角色表**（2026-09-19 起
    17 处文字档已收），但它**不进扫描面** —— 纳入/不纳入的判据写在 `ui_kit._TEXT_ROLES`
    上方（自绘控件内部、容器档不属于排版），宿主侧靠评审守。
    """
    violations: dict[str, list[str]] = {}
    for path in _py_files("plugins"):
        hits = _inline_stylesheet_calls(path)
        if hits:
            violations[path.relative_to(SRC).as_posix()] = hits
    assert not violations, (
        "插件源码里自拼 QSS（请改用 shell/ui_kit.py 的 apply_text_role；"
        "带边框 / 背景 / hover 的角色该进 styles.py）：\n"
        + "\n".join(
            f"  {p}\n" + "\n".join(f"      {h}" for h in hs)
            for p, hs in violations.items()
        )
    )


def test_inline_stylesheet_gate_detects_planted_violations(tmp_path):
    """自证：真调用必被抓，**注释 / docstring / 字符串里提到它不误报**。"""
    bad = tmp_path / "bad.py"
    bad.write_text(
        '"""模块 docstring：文字样式走 apply_text_role，别自己 setStyleSheet。"""\n'
        "# 注释里提 setStyleSheet(...) 同样不算违规\n"
        "MSG = '这里原先调 setStyleSheet 拼了字号'\n"
        "lbl.setStyleSheet('font-size:13px; font-weight:600;')\n"
        "QLabel('x').setStyleSheet(f'color:{t};')\n",
        encoding="utf-8",
    )
    lines = [h.split(":", 1)[0] for h in _inline_stylesheet_calls(bad)]
    assert lines == ["4", "5"], _inline_stylesheet_calls(bad)


def test_inline_stylesheet_gate_passes_on_compliant_sample(tmp_path):
    """自证：走排版角色的合规写法不得判红。"""
    good = tmp_path / "good.py"
    good.write_text(
        "def build(lbl):\n"
        "    apply_text_role(lbl, 'caption')\n"
        "    apply_text_role(lbl, 'section')\n"
        "    return lbl\n",
        encoding="utf-8",
    )
    assert _inline_stylesheet_calls(good) == []


def test_inline_stylesheet_gate_scans_every_plugin_module():
    """扫描面自证：`plugins/**` 的**全部** .py 都在内 —— 设置面与运行期面都得看。

    只看 `settings.py` 会漏掉一半：本批清掉的 25 处里有 5 处就在运行期面
    （`autotype/countdown_view.py` 3 处 ＋ `recognize/selview.py` 2 处）。
    """
    files = _py_files("plugins")
    assert len(files) > 20, len(files)
    assert any(p.name == "settings.py" for p in files), "设置面必须在内"
    assert any(p.name.endswith("_view.py") for p in files), "运行期面必须在内"


# ---- §10 工程纪律：几何不得写魔法数（圆角 / 分区卡内边距取自 shell/design/metrics）----


def _geometry_scan_targets() -> list[Path]:
    """几何门禁扫描面 ＝ 所有设置页（`plugins/**/settings.py` ＋ `shell/settings/` 整包）。

    与 `test_no_hardcoded_colors` 同一条判据：宿主与插件共用同一把标尺，扫描面就该
    同一条、零白名单。唯一被排除的是标尺定义处 `shell/design/metrics.py`（几何真源，
    自己当然可以写数字）—— 由 `test_geometry_scan_boundary_is_only_the_metric_source`
    守住「排除集恰好一个文件」。

    宿主那半边收的是**整包**：`shell/settings.py` 已拆成 `shell/settings/`（2026-10-01），
    卡片构造散在 `maintenance` / `general` / `framework` 几个子模块里。若仍按
    `SRC / "shell" / "settings.py"` 点名，拆包那天起 `is_file()` 判假 ⇒ **整个宿主
    设置面静默出圈**、门禁照样绿 —— 这正是本条最该防的失效。
    """
    targets: list[Path] = []
    for p in _py_files("plugins"):
        if p.name == "settings.py":
            targets.append(p)
    targets.extend(sorted((SRC / "shell" / "settings").rglob("*.py")))
    return targets


def _geometry_magic_numbers(path: Path) -> list[str]:
    """扫一个设置页里「圆角 / 分区卡内边距写了魔法数」的位置，返回 ``行号: 描述``。

    判据走 AST（与 `_inline_stylesheet_calls` 同理由：注释 / docstring 提到
    ``Card(radius=`` 不算违规，要拦的是**真去传字面量**的调用）：

    - ``Card(radius=<整数>)`：圆角必须取自 `metrics.RADIUS_*`；传裸 ``10`` 就是绕过标尺。
    - ``setContentsMargins(12, 8, 12, 8)`：分区卡内容区内边距的已知漂移值，必须改成
      `*metrics.PAD_CARD`。其它内边距（如 ``(0, 0, 0, 0)``、子行 ``(10, 6, 10, 6)``）
      是各自布局的局部值，不在本门禁范围内，不误报。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        # Card(radius=<int>) —— 圆角必须取自 metrics.RADIUS_*
        if (isinstance(fn, ast.Name) and fn.id == "Card") or (
            isinstance(fn, ast.Attribute) and fn.attr == "Card"
        ):
            for kw in node.keywords:
                if (
                    kw.arg == "radius"
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, int)
                ):
                    hits.append(
                        f"{node.lineno}: Card(radius={kw.value.value}) 圆角必须取自 metrics.RADIUS_*"
                    )
        # setContentsMargins(12, 8, 12, 8) —— 分区卡内边距的已知漂移值
        if isinstance(fn, ast.Attribute) and fn.attr == "setContentsMargins":
            args = node.args
            if (
                len(args) == 4
                and all(
                    isinstance(a, ast.Constant) and isinstance(a.value, int) for a in args
                )
                and [a.value for a in args] == [12, 8, 12, 8]
            ):
                hits.append(
                    f"{node.lineno}: setContentsMargins(12, 8, 12, 8) 必须改成 *metrics.PAD_CARD"
                )
    return hits


def test_no_geometry_magic_numbers():
    """设置页的几何（圆角 / 分区卡内边距）不得写裸数字，必须取自 `shell/design/metrics`。

    这是 Phase 0「统一标尺」的门禁落点：此前各页手写 `Card(radius=10)` ＋
    `setContentsMargins(12, 8, 12, 8)`，数值略有漂移，是「大小不一 / 层级不统一」的根因。
    改一处标尺全仓生效；写死一个字面量就等于绕过它，系统换肤 / 调间距时它不动。
    """
    violations: dict[str, list[str]] = {}
    for path in _geometry_scan_targets():
        hits = _geometry_magic_numbers(path)
        if hits:
            violations[path.relative_to(SRC).as_posix()] = hits
    assert not violations, (
        "设置页出现几何魔法数（请改取 shell/design/metrics）：\n"
        + "\n".join(
            f"  {p}\n" + "\n".join(f"      {h}" for h in hs)
            for p, hs in violations.items()
        )
    )


def test_geometry_scan_boundary_is_only_the_metric_source():
    """自证：扫描面覆盖所有设置页，且只排除一个文件（几何真源 `metrics.py`）。"""
    _METRIC_SOURCE = SRC / "shell" / "design" / "metrics.py"
    assert _METRIC_SOURCE.is_file(), f"几何真源指向了不存在的文件：{_METRIC_SOURCE}"
    tree = ast.parse(_METRIC_SOURCE.read_text(encoding="utf-8"))
    assigned: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            assigned |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    assert "RADIUS_MD" in assigned and "PAD_CARD" in assigned, (
        "metrics.py 未定义 RADIUS_*/PAD_CARD"
    )

    host_pkg = SRC / "shell" / "settings"
    targets = set(_geometry_scan_targets())
    expected_settings = {p for p in _py_files("plugins") if p.name == "settings.py"}
    expected = expected_settings | {p for p in host_pkg.rglob("*.py")}
    missing = expected - targets
    assert not missing, (
        "扫描面比预期窄 —— 有设置页被漏掉：\n"
        + "\n".join(f"  {p.relative_to(SRC).as_posix()}" for p in sorted(missing))
    )
    # 点名**真身**：`Card(radius=…)` / `setContentsMargins(12, 8, …)` 只可能写在画卡片的那
    # 几个子模块里。宿主设置面拆成包后（2026-10-01）按旧路径点名会落到一个不存在的文件上
    # ⇒ 断言恒假/恒真，整个宿主面静默出圈。
    for name in ("pages.py", "plugins.py", "framework.py", "general.py", "maintenance.py"):
        real = host_pkg / name
        assert real.is_file(), f"宿主设置面缺了真身 {name}（拆包后路径变了？）"
        assert real in targets, f"{name} 是卡片构造的真身，必须在几何扫描面内"
    assert _METRIC_SOURCE not in targets, "几何真源落回了扫描面内"


def test_geometry_gate_detects_planted_violations(tmp_path):
    """自证：字面量圆角 / 漂移内边距必被抓，注释与 docstring 不误报。"""
    bad = tmp_path / "settings.py"
    bad.write_text(
        '"""本页原先 Card(radius=10) ＋ setContentsMargins(12, 8, 12, 8)。"""\n'
        "# 注释里的 Card(radius=10) 不算\n"
        "card = Card(radius=10, parent=page)\n"
        "row.setContentsMargins(12, 8, 12, 8)\n",
        encoding="utf-8",
    )
    hits = _geometry_magic_numbers(bad)
    assert len(hits) == 2, hits
    assert any("Card(radius=10)" in h for h in hits)
    assert any("setContentsMargins(12, 8, 12, 8)" in h for h in hits)


def test_geometry_gate_passes_on_compliant_sample(tmp_path):
    """自证：取自 metrics 的写法不得判红。"""
    good = tmp_path / "settings.py"
    good.write_text(
        "card = Card(radius=metrics.RADIUS_MD, parent=page)\n"
        "row.setContentsMargins(*metrics.PAD_CARD)\n"
        "box.setContentsMargins(0, 0, 0, 0)\n",
        encoding="utf-8",
    )
    assert _geometry_magic_numbers(good) == []


# ---- §设置页外观（2026-09-21）：零软投影 ＋ 分区卡微圆角 ＋ 标题下有分隔线 ----
#
# 上一版这一段守的是「玻璃只在原生 macOS 生效」（两条用例）。Phase 1 玻璃 A 2026-09-21
# 整体删除 —— 删它是**量出来的**，不是审美取舍：软投影（`QGraphicsDropShadowEffect`）
# 每帧要把整棵子树重画进一张离屏位图再合成；半透明面压在设置页那层不透明 `surface` 上
# 只差 3/255（`#FCFCFC` vs `#FFFFFF`）；内描边高光是白压白；大圆角 20 与窗口的 14、
# 卡片自己的 1px 描边三层不同心。原门禁的对象已不存在，故换成下面三条（判据见
# `docs/log/2026-09-21-设置窗观感返工.md`）。


def _drop_shadow_hits(path: Path) -> list[str]:
    """源码里引用 ``QGraphicsDropShadowEffect`` 的落点（AST：只认真的引用，不算注释）。"""
    hits: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        name = getattr(node, "id", None) or getattr(node, "attr", None)
        if name == "QGraphicsDropShadowEffect":
            hits.append(f"{path.name}:{node.lineno}: 引用 QGraphicsDropShadowEffect")
    return hits


def _shadow_scan_targets() -> list[Path]:
    """扫描面：shell ＋ 全部插件 ＋ contribution（外观代码只可能长在这三处）。"""
    return sorted(_py_files("shell") + _py_files("plugins") + _py_files("contribution"))


def test_no_drop_shadow_effect_in_ui():
    """软投影一律不许回来 —— 它是用户判为「非常丑」的那一层，且每帧要离屏合成整棵子树。"""
    violations = {
        p.relative_to(SRC).as_posix(): hits
        for p in _shadow_scan_targets()
        if (hits := _drop_shadow_hits(p))
    }
    assert not violations, (
        "出现软投影（玻璃 A 已删，Card 是实心 ＋ 1px 描边，别改回去）：\n"
        + "\n".join(f"  {p}\n" + "\n".join(f"      {h}" for h in hs)
                    for p, hs in violations.items())
    )


def test_drop_shadow_gate_detects_planted_violation(tmp_path):
    """自证：植入一条 ``QGraphicsDropShadowEffect`` 必被抓。"""
    bad = tmp_path / "settings.py"
    bad.write_text(
        "from PySide6.QtWidgets import QGraphicsDropShadowEffect\n"
        "eff = QGraphicsDropShadowEffect(self)\n",
        encoding="utf-8",
    )
    hits = _drop_shadow_hits(bad)
    assert len(hits) == 1, hits


def test_drop_shadow_gate_passes_on_compliant_sample(tmp_path):
    """自证：实心写法不误报（docstring 里提到它的名字也不算引用）。"""
    good = tmp_path / "settings.py"
    good.write_text(
        '"""docstring 里提到 QGraphicsDropShadowEffect 不算引用。"""\n'
        "from barkit.shell.ui_kit import settings_section\n"
        "card, lay = settings_section('标题')\n",
        encoding="utf-8",
    )
    assert _drop_shadow_hits(good) == []


def test_settings_section_is_micro_rounded_solid_card(qapp):
    """分区卡三件套：**微圆角**、标题下**分隔线**、**不挂图形效果**。

    用户 2026-09-21 的三条原话各对应一条：

    - 「内部控件阴影非常丑」⇒ 卡上不挂图形效果（软投影已随玻璃模式删除）；
    - 「内部的圆角也非常丑」⇒ 圆角与大圆角档位脱钩，取 ``RADIUS_SM``（与按钮同族）；
    - 「分组要一眼定位」⇒ 标题是一条**带底色的标题栏**（2026-09-21 二次返工：那条
      1px 横线扫不出分组，底色本身就是边界 ⇒ 横线一并去掉）。
    """
    from barkit.shell.design import metrics
    from barkit.shell.ui_kit import Divider, _SectionHeader, settings_section

    # 圆角档位：分区卡取「微圆角」那一档，且必须严格小于大圆角档
    assert metrics.RADIUS_SM < metrics.RADIUS_MD < metrics.RADIUS_LG
    assert metrics.RADIUS_SM == 8, "分区卡圆角是量出来的（与按钮同族），别随手改"

    card, lay = settings_section("标题")
    assert card._radius == metrics.RADIUS_SM
    assert card.graphicsEffect() is None, "分区卡不许挂图形效果"

    # 标题栏是卡的直接子控件（它要贴着卡的顶边铺满，故不在内容区里）
    head = card.findChild(_SectionHeader)
    assert head is not None, "有标题 ⇒ 有一条带底色的标题栏"
    assert head.parentWidget() is card
    assert card.layout().indexOf(head) == 0, "标题栏在卡的最上面"

    # 底色**取代了**那条横线：卡内再出现 Divider 就是「一条线加一条带子」两层边界
    def _dividers(layout) -> list:
        return [
            layout.itemAt(i).widget() for i in range(layout.count())
            if isinstance(layout.itemAt(i).widget(), Divider)
        ]

    assert not _dividers(lay), "标题栏带底色后，卡内不该再有分隔线"

    # 无标题卡不建标题栏：没有标题就没有要标出来的那一组（也就没有可折叠的把手）
    bare, bare_lay = settings_section("")
    assert bare.findChild(_SectionHeader) is None
    assert not _dividers(bare_lay)


# ---- §Phase 1 入场淡入：淡完必须回到「看得见」（2026-09-21 真机「空窗」的回归守）----


def _opaque_ratio(widget) -> float:
    """窗口 ``grab()`` 里不透明像素的占比：1.0 = 画出了内容，0.0 = 整窗被画成透明。

    判据只能用 ``QWidget.grab()``——它反映控件级 ``QGraphicsEffect``；AppKit 的
    ``cacheDisplayInRect`` 位图**不反映**它（实测：同一进程里给简单对话框挂
    ``opacity=0`` 的 effect，位图探针照样拍到内容）。
    """
    img = widget.grab().toImage()
    seen = opaque = 0
    for y in range(0, img.height(), 3):
        for x in range(0, img.width(), 3):
            seen += 1
            opaque += img.pixelColor(x, y).alpha() > 200
    return opaque / max(1, seen)


def test_entrance_fades_the_window_back_to_opaque(qapp, qtbot, monkeypatch):
    """伪装 cocoa 跑一遍入场：起点要落在透明侧、终点要不透明且不留效果。

    守 2026-09-21 的真机事故（设置窗「打开一片空」）：入场把 ``QGraphicsOpacityEffect``
    挂在**顶层窗**上，而驱动它的 ``QPropertyAnimation`` 没给 C++ 父对象 ⇒ 归 Python 管
    ⇒ ``play_entrance()`` 一返回就被回收 ⇒ 效果永远停在 opacity 0 ⇒ 整窗渲染成透明
    （原生标题栏是系统画的、还在，所以看着像「打开了但里面什么都没有」）。

    三问各自独立，缺一条就会漏掉一种坏法：

    - **起点**落在透明侧 —— 否则淡入根本没发生，这条门禁成了空转（一个什么都不做的
      实现照样能过「终点不透明」）；
    - **终点**回到不透明 —— 这正是动画被回收时的失效长相；
    - **不留效果** —— 「效果摘不掉」与「动画跑不动」是两种坏法，表现一样、根因不同。
    """
    import barkit.shell.ui_kit as uik
    from PySide6.QtWidgets import QGraphicsOpacityEffect, QLabel, QVBoxLayout, QWidget

    monkeypatch.setattr(uik.base, "_is_native_cocoa", lambda: True)
    win = QWidget()
    QVBoxLayout(win).addWidget(QLabel("内容"))
    win.resize(240, 120)
    win.show()
    qapp.processEvents()
    assert _opaque_ratio(win) >= 0.99, "入场前就没画出来 —— 探针前提不成立"

    uik.play_entrance(win)
    began_hidden = win.windowOpacity() < 1.0 or isinstance(
        win.graphicsEffect(), QGraphicsOpacityEffect
    )
    assert began_hidden, "入场没落在透明起点上 —— 淡入根本没发生"

    qtbot.wait(400)
    assert win.windowOpacity() == 1.0, "入场跑完窗口仍不透明 —— 动画被回收的典型长相"
    assert not isinstance(win.graphicsEffect(), QGraphicsOpacityEffect), (
        "入场结束后仍挂着不透明度效果 —— 整窗会被画成透明"
    )
    assert _opaque_ratio(win) > 0.99, "入场跑完窗口画出来是空的"

    win.close()
    win.deleteLater()
    qtbot.wait(30)


def test_entrance_is_noop_off_cocoa(qapp):
    """本机（离屏 / 非 cocoa）入场是 no-op：不挂效果、也不碰窗口不透明度。

    这是上一条的**反面**——上一条靠 monkeypatch 伪装 cocoa 才跑得起来，真环境下必须
    一动不动，否则「离屏测试不被入场动画干扰」这条前提就没了（Windows 写码端同理）。
    """
    from PySide6.QtWidgets import QWidget

    from barkit.shell.ui_kit import _is_native_cocoa, play_entrance

    assert _is_native_cocoa() is False, "本机应为离屏 / 非 cocoa（否则断言前提变了）"
    win = QWidget()
    win.resize(240, 120)
    win.show()
    qapp.processEvents()

    play_entrance(win)

    assert win.graphicsEffect() is None, "非 cocoa 下不该挂任何效果"
    assert win.windowOpacity() == 1.0, "非 cocoa 下不该动窗口不透明度"
    win.close()


def test_text_roles_are_token_only_and_render_under_both_themes():
    """排版角色表自洽：角色里**只能**有令牌，且两套主题都渲染得出来。

    守两件在别处看不见的事：

    - ① 角色模板里写**颜色字面量** —— 等于给「颜色唯一真源在 tokens.py」开后门，
      而且该角色换肤时不会跟着动（正是这张表存在的理由）；
    - ② 模板里的**令牌名拼错**（``{text20}``）—— ``apply_text_role`` 只在**该角色真被
      调用**时才抛 KeyError，一个当下没人用的角色会带着错名一直躺着。
    """
    from barkit.shell.design.tokens import TOKENS
    from barkit.shell.design import metrics
    from barkit.shell.ui_kit import _TEXT_ROLES

    assert _TEXT_ROLES, "排版角色表空了"
    for role, template in _TEXT_ROLES.items():
        assert "#" not in template, f"{role} 里写了颜色字面量：{template}"
        assert "font-size" in template, f"{role} 不是一档排版（没有字号）：{template}"
        for theme, tokens in TOKENS.items():
            # 角色模板里还有字号占位符（``{FONT_BODY}`` 等），必须与颜色令牌同一次格式化
            # （见 ``apply_text_role``）；门禁 ``test_no_font_size_magic_numbers`` 守裸 px。
            rendered = template.format(**tokens, **metrics.FONT_SCALE)  # 令牌名写错 / 缺档 ⇒ KeyError
            assert rendered.strip(), f"{role} 在 {theme} 下渲染成空串"


def test_text_role_names_written_in_call_sites_exist_in_the_table():
    """调用点写的角色名**字面量**必须是角色表里的键（静态判据，全仓扫）。

    来历（2026-09-23）：``apply_text_role`` 遇到没登记的名字会**当场抛 KeyError**，
    这个设计没问题 —— 问题在**只有那条路径真跑到才报**。现场是热键「轻浮层」的默认出口
    里写了 ``"body"``（表里没有），于是那个选项表现为「按了键、日志一条 traceback、
    屏幕上什么都没弹」，而且**从来没弹出过**。静态查一遍，等于把这条错从「等人踩」
    提前到「写完就红」。

    与上面那条的分工：那条守表的**内容**（只能有令牌、两主题都渲染得出来），
    这条守调用点的**名字**。两条都不在，写错名字就只能靠运气被人发现。
    """
    from barkit.shell.ui_kit import _TEXT_ROLES

    bad: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or len(node.args) < 2:
                continue
            fn = node.func
            # 只认 ``x.apply_text_role(...)`` / 裸 ``apply_text_role(...)``（本仓两种写法）。
            target = fn.attr if isinstance(fn, ast.Attribute) else (
                fn.id if isinstance(fn, ast.Name) else ""
            )
            if target != "apply_text_role":
                continue
            role = node.args[1]
            if isinstance(role, ast.Constant) and isinstance(role.value, str):
                if role.value not in _TEXT_ROLES:
                    bad.append(f"{path.relative_to(SRC)}:{node.lineno} → {role.value!r}")
    assert not bad, (
        "角色名不在 ``_TEXT_ROLES`` 里（真跑到才会抛 KeyError，静态查就没得躲）：\n  "
        + "\n  ".join(bad)
    )


# ---- §8.1 插件间不得互相 import ----

def _other_plugin_in(dotted: str) -> str | None:
    """``barkit.plugins.<id>[.more]`` → ``<id>``；否则 None。"""
    parts = dotted.split(".")
    if len(parts) >= 3 and parts[0] == "barkit" and parts[1] == "plugins":
        return parts[2]
    return None


def _cross_plugin_imports(path: Path, own_id: str | None) -> list[str]:
    """指向**别的**插件包的 import（空 = 合规）。

    覆盖三种写法：``import barkit.plugins.<other>``、
    ``from barkit.plugins.<other>[...] import x``、``from barkit.plugins import <other>``。
    相对导入没有包外目标，天然不跨包；``from barkit.plugins.<自己> import x`` 是 §2.4
    明示允许的「直指子模块」写法，不算跨插件。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                other = _other_plugin_in(alias.name)
                if other and other != own_id:
                    out.add(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            module = node.module or ""
            other = _other_plugin_in(module)
            if other:
                if other != own_id:
                    out.add(f"from {module} import ...")
                continue
            if module == "barkit.plugins":
                for alias in node.names:
                    if alias.name != own_id:
                        out.add(f"from barkit.plugins import {alias.name}")
    return sorted(out)


def test_no_cross_plugin_import():
    """插件不得 import 别的插件包（§8.1）。

    阶段 0 第 4 步（helper 客户端上提 `capabilities/helper_client.py`）消除了仅有的
    两处（都指 `route_guard.daemon`），故本门禁无白名单、零命中。
    """
    violations: dict[str, list[str]] = {}
    for path in sorted(PLUGINS.rglob("*.py")):
        rel = path.relative_to(PLUGINS).as_posix()
        own_id = rel.split("/", 1)[0] if "/" in rel else None
        hits = _cross_plugin_imports(path, own_id)
        if hits:
            violations[rel] = hits
    assert not violations, (
        "插件间不得互相 import（框架基础设施应上提到 capabilities/ 或 core/，§8.1）：\n"
        + "\n".join(f"  src/barkit/plugins/{k}: {', '.join(v)}"
                    for k, v in violations.items())
    )


def test_cross_plugin_gate_detects_planted_violations(tmp_path):
    """门禁自测：三种跨插件写法必须都被检出。"""
    bad = tmp_path / "plugin.py"
    bad.write_text(
        "import barkit.plugins.route_guard.daemon\n"
        "from barkit.plugins.hosts_guard import store\n"
        "from barkit.plugins import ipad_tunnel\n",
        encoding="utf-8",
    )
    got = _cross_plugin_imports(bad, "selfplug")
    assert len(got) == 3, got
    assert any("route_guard.daemon" in g for g in got), got
    assert any("hosts_guard" in g for g in got), got
    assert any("ipad_tunnel" in g for g in got), got


def test_cross_plugin_gate_passes_on_compliant_sample(tmp_path):
    """反向自测：相对导入 / 自身子模块 / 框架 import 不得误报。"""
    good = tmp_path / "plugin.py"
    good.write_text(
        "from barkit.capabilities import helper_client as daemon\n"
        "from barkit.core.plugin import Plugin\n"
        "from barkit.plugins.myplug import engine\n"
        "from . import settings\n"
        "from .engine import run\n",
        encoding="utf-8",
    )
    assert _cross_plugin_imports(good, "myplug") == []


# ---- §6.2 已上提的样板不得在插件里重新定义 ----

BOILERPLATE_METHODS = frozenset({
    "_publish_state",     # → Plugin.publish_snapshot()
    "_status_topic",      # → Plugin.status_topic()
    "_touch_trigger",     # → Plugin.touch_trigger()
    "_load_user_config",  # → Config.user_plugin_section() / update_plugin_section()
})


def _boilerplate_defs(path: Path) -> list[str]:
    """插件模块里**定义**了哪些已上提的样板方法（带行号；空 = 合规）。

    只看定义（``def``），不看使用——注释 / docstring 里提到旧名不会被误报。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in BOILERPLATE_METHODS):
            out.append(f"{node.name}() @L{node.lineno}")
    return out


def test_no_handwritten_boilerplate():
    """插件不得重新定义已上提的样板方法（§6.2）。

    四件已有唯一落点：三件在 ``Plugin`` 基类，配置段读改写在 ``Config``（§6.3）。
    """
    violations = {
        path.relative_to(PLUGINS).as_posix(): hits
        for path in sorted(PLUGINS.rglob("*.py"))
        if (hits := _boilerplate_defs(path))
    }
    assert not violations, (
        "插件内重新定义了已上提的样板方法（§6.2 处置表）：\n"
        + "\n".join(f"  src/barkit/plugins/{k}: {', '.join(v)}"
                    for k, v in violations.items())
        + "\n改法：publish_snapshot() / status_topic() / touch_trigger() 用基类；"
          "配置段读改写用 ctx.config.user_plugin_section() / update_plugin_section()。"
    )


def test_boilerplate_gate_detects_planted_violations(tmp_path):
    """门禁自测：四个旧名任一定义必须被检出，使用与注释不得误报。"""
    bad = tmp_path / "plugin.py"
    bad.write_text(
        "# 迁移说明：不再手写 _touch_trigger\n"
        "class P:\n"
        "    def _publish_state(self):\n"
        "        pass\n"
        "    def _status_topic(self):\n"
        "        pass\n"
        "    def _touch_trigger(self):\n"
        "        pass\n"
        "    def _load_user_config(self):\n"
        "        pass\n",
        encoding="utf-8",
    )
    got = _boilerplate_defs(bad)
    assert len(got) == 4, got
    for name in BOILERPLATE_METHODS:
        assert any(name in g for g in got), (name, got)


def test_boilerplate_gate_passes_on_compliant_sample(tmp_path):
    """反向自测：调用基类方法 / 注释里提旧名不得误报。"""
    good = tmp_path / "plugin.py"
    good.write_text(
        "# 旧名 _status_topic 已删除，改用 self.status_topic()\n"
        "class P:\n"
        "    def notify(self):\n"
        "        self.publish_snapshot()\n"
        "        self.touch_trigger()\n"
        "        return self.status_topic()\n",
        encoding="utf-8",
    )
    assert _boilerplate_defs(good) == []


# ---- §7.1 设置页：必须可构建，用了 build 必须写 reason ----

def _spec_calls(path: Path, spec_name: str) -> list[ast.Call]:
    """文件里所有 ``<spec_name>(...)`` 构造调用。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted(node.func)
        if dotted and dotted.split(".")[-1] == spec_name:
            out.append(node)
    return out


def _kwarg(call: ast.Call, name: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _is_none_literal(node: ast.AST | None) -> bool:
    """缺省（None）或显式 ``None`` 字面量都算「没给这个来源」。"""
    return node is None or (isinstance(node, ast.Constant) and node.value is None)


def _unreasoned_settings_pages(path: Path) -> list[str]:
    """用了 ``build`` 却没写 ``reason`` 的 SettingsPageSpec（空 = 合规）。"""
    out: list[str] = []
    for call in _spec_calls(path, "SettingsPageSpec"):
        if _is_none_literal(_kwarg(call, "build")):
            continue      # 没走 build 逃生舱，无需 reason
        reason = _kwarg(call, "reason")
        if reason is None or (
            isinstance(reason, ast.Constant) and not str(reason.value or "").strip()
        ):
            out.append(f"SettingsPageSpec @L{call.lineno} 用了 build 但没写 reason")
    return out


def _unbuildable_settings_pages(path: Path) -> list[str]:
    """既无 ``build`` 又无 ``schema`` 的 SettingsPageSpec（空 = 合规）。"""
    out: list[str] = []
    for call in _spec_calls(path, "SettingsPageSpec"):
        if (_is_none_literal(_kwarg(call, "build"))
                and _is_none_literal(_kwarg(call, "schema"))):
            out.append(f"SettingsPageSpec @L{call.lineno} 既没 build 也没 schema")
    return out


def test_custom_page_has_reason():
    """`SettingsPageSpec.build` 非空时必须写 `reason`（§7.1 决议 2）——自绘要说明理由。"""
    violations = {
        path.relative_to(PLUGINS).as_posix(): hits
        for path in sorted(PLUGINS.rglob("*.py"))
        if (hits := _unreasoned_settings_pages(path))
    }
    assert not violations, (
        "设置页用了 build（自绘逃生舱）却没写 reason（§7.1 决议 2）：\n"
        + "\n".join(f"  src/barkit/plugins/{k}: {', '.join(v)}"
                    for k, v in violations.items())
    )


def test_settings_page_is_buildable():
    """设置页必须至少有一个可构建来源（`build` 或 `schema`）——防未来，不是存量清偿。

    存量 6 处全有 ``build``（当前 0 命中）。配套的是加载期 fail-fast：
    ``core.contributions._validate_contributions`` 抛 ``ValueError``，其行为由
    :func:`test_settings_page_fail_fast_isolates_plugin` 验证。
    """
    violations = {
        path.relative_to(PLUGINS).as_posix(): hits
        for path in sorted(PLUGINS.rglob("*.py"))
        if (hits := _unbuildable_settings_pages(path))
    }
    assert not violations, (
        "设置页既没 build 也没 schema——打开设置窗会 TypeError（§7.1 决议 4）：\n"
        + "\n".join(f"  src/barkit/plugins/{k}: {', '.join(v)}"
                    for k, v in violations.items())
    )


def test_settings_page_fail_fast_isolates_plugin():
    """收集期 fail-fast 的行为验证：坏页让该插件记 failed，其余插件不受影响。"""
    from barkit.contribution.specs import SettingsPageSpec
    from barkit.core.contributions import ContributionRegistry

    class _Bad:
        id = "bad_page"
        api_version = 3

        def settings_page(self):
            return SettingsPageSpec(label="坏页")

    class _Good:
        id = "good_plugin"
        api_version = 3

        def settings_page(self):
            return SettingsPageSpec(
                label="好页",
                build=lambda: None, reason="合规样本：动作入口页",
            )

    reg = ContributionRegistry([_Bad(), _Good()])
    assert [pid for pid, _ in reg.failed] == ["bad_page"]
    assert isinstance(reg.failed[0][1], ValueError), reg.failed
    assert [p.label for p in reg.settings_pages()] == ["好页"], reg.settings_pages()


def test_settings_page_gates_detect_planted_violations(tmp_path):
    """门禁自测：缺 reason / 双缺来源必须各自被检出，且两条判据互不遮蔽。"""
    bad = tmp_path / "plugin.py"
    bad.write_text(
        "from barkit.contribution.specs import SettingsPageSpec\n"
        "A = SettingsPageSpec(plugin_id='p', label='无理由', build=lambda: None)\n"
        "B = SettingsPageSpec(plugin_id='p', label='无来源')\n"
        "C = SettingsPageSpec(plugin_id='p', label='空理由', build=lambda: None, reason='   ')\n"
        "D = SettingsPageSpec(plugin_id='p', label='合规', schema=object())\n",
        encoding="utf-8",
    )
    assert _unreasoned_settings_pages(bad) == [
        "SettingsPageSpec @L2 用了 build 但没写 reason",
        "SettingsPageSpec @L4 用了 build 但没写 reason",
    ], _unreasoned_settings_pages(bad)
    assert _unbuildable_settings_pages(bad) == [
        "SettingsPageSpec @L3 既没 build 也没 schema",
    ], _unbuildable_settings_pages(bad)


def test_settings_page_gates_pass_on_compliant_sample(tmp_path):
    """反向自测：有 build + reason、或只有 schema 的写法不得误报。"""
    good = tmp_path / "plugin.py"
    good.write_text(
        "from barkit.contribution.specs import SettingsPageSpec\n"
        "A = SettingsPageSpec(plugin_id='p', label='自绘', build=f, reason='集合型规则编辑器')\n"
        "B = SettingsPageSpec(plugin_id='p', label='声明式', schema=FormSchema(plugin_id='p'))\n",
        encoding="utf-8",
    )
    assert _unreasoned_settings_pages(good) == []
    assert _unbuildable_settings_pages(good) == []


# ---- D20 声明入口：一个声明点一个具名方法，plugin_id 由框架注入 ----

#: 七个声明点对应的 Spec 类名（真源：``contribution/points.declaration_points()``）。
_D20_SPEC_CLASSES = ("ComponentSpec", "StateSpec", "StatusRowSpec",
                     "DetailEntrySpec", "SettingsPageSpec", "ActionSpec", "HotkeySpec")


def _plugin_id_writes(path: Path) -> list[str]:
    """文件里「在声明点 Spec 构造时写 ``plugin_id``」的命中（空列表 = 合规）。

    判据走 AST：七个 Spec 类的构造调用里，``plugin_id`` 无论以关键字还是
    **第一个位置参数**出现都算违规——身份由框架在收集期注入（决议 D20 / D14），
    组件一个字都不写。``FormSchema(plugin_id=...)`` 是配置 schema 的合法参数，
    不在 Spec 名单里，天然放行。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name not in _D20_SPEC_CLASSES:
            continue
        for kw in node.keywords:
            if kw.arg == "plugin_id":
                hits.append(f"{name}(plugin_id=…) @L{node.lineno}")
        if node.args:
            # point 声明为 init=False，构造函数第一个位置参数就是 plugin_id
            hits.append(f"{name}(<位置参数 plugin_id>) @L{node.lineno}")
    return sorted(hits, key=lambda s: int(s.rsplit("L", 1)[1]))


def test_gate_no_plugin_id_written_in_declaration_specs():
    """D20 门禁（扫描面）：全部组件（含模板与外部示例）不得在 Spec 里写 plugin_id。"""
    files = sorted((SRC / "plugins").rglob("*.py")) + sorted(
        (Path(__file__).resolve().parents[1] / "example").rglob("*.py")
    )
    assert files, "扫描面为空——门禁等于失效"
    bad = {
        p.relative_to(SRC.parents[1]).as_posix(): hits
        for p in files
        if (hits := _plugin_id_writes(p))
    }
    assert not bad, (
        "组件在声明里写了 plugin_id——身份由框架注入，写了就会盖过别人的 id"
        "（归属漂移），删掉它：\n"
        + "\n".join(f"  {k}: {', '.join(v)}" for k, v in bad.items())
    )


def test_gate_plugin_id_gate_detects_planted_violation(tmp_path):
    """D20 门禁自证（两半）：合规样本零命中，植入 plugin_id 必抓（关键字与位置参数两路）。"""
    good = tmp_path / "good.py"
    good.write_text(
        "# 注释里提 plugin_id 不算违规（AST 判据，不走子串）\n"
        "A = ComponentSpec(icon=IconName.GEAR)\n"
        "B = FormSchema(plugin_id='p')  # 配置 schema 的合法参数，不在 Spec 名单\n",
        encoding="utf-8",
    )
    assert _plugin_id_writes(good) == [], _plugin_id_writes(good)

    bad = tmp_path / "bad.py"
    bad.write_text(
        "A = ComponentSpec(plugin_id='p', icon=IconName.GEAR)\n"
        "B = StateSpec('p', key='main')\n",   # 位置参数：point 不进 __init__，首位就是 plugin_id
        encoding="utf-8",
    )
    hits = _plugin_id_writes(bad)
    assert len(hits) == 2, hits
    assert "ComponentSpec(plugin_id=…) @L1" in hits
    assert "StateSpec(<位置参数 plugin_id>) @L2" in hits


def test_d20_collection_rejects_wrong_spec_for_point():
    """收集期校验（行为面）：把别的 Spec 装进声明点 → 记 failed，不再静默丢弃。"""
    from barkit.contribution.specs import HotkeySpec
    from barkit.core.contributions import ContributionRegistry

    class _Wrong:
        id = "wrong_spec"
        api_version = 3

        def actions(self):
            return (HotkeySpec(name="x", label="热键装进了按钮位"),)

    reg = ContributionRegistry([_Wrong()])
    assert [pid for pid, _ in reg.failed] == ["wrong_spec"], reg.failed
    assert isinstance(reg.failed[0][1], ValueError), reg.failed
    assert reg.primary_actions() == [], "装错的 Spec 不得流入消费面"


def test_d20_collection_rejects_wrong_return_shape():
    """收集期校验（行为面）：n 声明点返回 None / 0/1 声明点返回列表 → 记 failed。"""
    from barkit.core.contributions import ContributionRegistry

    class _NoneState:
        id = "none_state"
        api_version = 3

        def state_specs(self):
            return None

    class _ListComponent:
        id = "list_component"
        api_version = 3

        def component(self):
            return []

    reg = ContributionRegistry([_NoneState(), _ListComponent()])
    assert {pid for pid, _ in reg.failed} == {"none_state", "list_component"}, reg.failed


def test_d20_injection_overwrites_component_written_plugin_id():
    """身份注入是覆写不是补空：组件写错了 plugin_id 也以 ``Plugin.id`` 为准。"""
    from barkit.contribution.specs import ComponentSpec, IconName
    from barkit.core.contributions import ContributionRegistry
    from barkit.core.plugin import Plugin, PluginContext

    class _Cfg:
        def get(self, key, default=None):
            return default

    class _P(Plugin):
        id = "real_id"
        api_version = 3

        def component(self):
            return ComponentSpec(plugin_id="someone_else", icon=IconName.GEAR)

    reg = ContributionRegistry([_P(PluginContext(config=_Cfg()))])
    assert reg.failed == [], reg.failed
    comps = reg.components()
    assert len(comps) == 1 and comps[0].plugin_id == "real_id", comps


# ---- §10 模板双骨架覆盖 ----

TEMPLATE_DIR = PLUGINS / "_template"


def _top_level_symbols(path: Path) -> dict:
    """模块顶层事实：函数名集合 + {类名: {基类名, 类属性源码, 方法名}}。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    funcs = {
        n.name for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    classes: dict[str, dict] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        methods: set[str] = set()
        attrs: dict[str, str] = {}
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.add(item.name)
            elif (isinstance(item, ast.Assign) and len(item.targets) == 1
                    and isinstance(item.targets[0], ast.Name)):
                attrs[item.targets[0].id] = ast.unparse(item.value)
        classes[node.name] = {
            "bases": {_dotted(b) for b in node.bases} - {None},
            "attrs": attrs,
            "methods": methods,
        }
    return {"funcs": funcs, "classes": classes}


def _used_names(path: Path) -> set[str]:
    """模块里出现过的全部 ``Name`` 标识符（含 import 名与被引用的常量名）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


def _used_attrs(path: Path) -> set[str]:
    """模块里出现过的全部属性名（``a.b`` 里的 ``b``）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}


def test_template_covers_forms():
    """`_template` 必须同时含动作型与守护型骨架，且覆盖 §10.1 的演示清单。

    判据用 AST（不匹配子串），所以注释 / docstring 里提到能力名不算覆盖。
    """
    action_src = TEMPLATE_DIR / "plugin.py"
    action = _top_level_symbols(action_src)
    cls = action["classes"].get("Plugin")
    assert cls, "动作型骨架缺少名为 Plugin 的类（发现器按名取用，§2.1）"
    assert "Plugin" in cls["bases"], f"动作型骨架应直接继承 Plugin，实得 {cls['bases']}"
    assert cls["attrs"].get("run_mode") == "RUN_ON_DEMAND", "动作型骨架的 run_mode 应为 RUN_ON_DEMAND"
    for method in ("component", "row_specs", "state_specs", "settings_page",
                   "actions", "hotkeys", "_do_action", "_snapshot"):
        assert method in cls["methods"], f"动作型骨架缺少 {method}()"
    action_used = _used_names(action_src)
    for spec in ("StatusRowSpec", "ComponentSpec", "StateSpec", "SettingsPageSpec",
                 "ActionSpec", "HotkeySpec"):
        assert spec in action_used, f"动作型骨架未演示 {spec}"

    assert "build_page" in _top_level_symbols(TEMPLATE_DIR / "settings.py")["funcs"], (
        "设置面应把 build_page 作为稳定门面（§7.2）"
    )

    daemon_src = TEMPLATE_DIR / "plugin_daemon.py"
    dcls = _top_level_symbols(daemon_src)["classes"].get("Plugin")
    assert dcls, "守护型骨架缺少名为 Plugin 的类"
    assert "AutomationCapability" in dcls["bases"], (
        f"守护型骨架应继承 AutomationCapability，实得 {dcls['bases']}"
    )
    assert dcls["attrs"].get("run_mode") == "RUN_PERIODIC", "守护型骨架的 run_mode 应为 RUN_PERIODIC"
    assert "failure_policy" in dcls["attrs"], "守护型骨架应声明 failure_policy（熔断策略按它分化）"
    for method in ("tick", "resident_services", "subjects", "on_observed",
                   "resources", "automation_active", "set_automation_active",
                   "config_schema", "row_specs", "component", "state_specs", "hotkeys"):
        assert method in dcls["methods"], f"守护型骨架缺少 {method}()"
    daemon_used = _used_names(daemon_src)
    for name in ("RUN_PERIODIC", "FAIL_BREAK", "PluginResources", "FormSchema"):
        assert name in daemon_used, f"守护型骨架未演示 {name}"
    assert "mark_dirty" in _used_attrs(daemon_src), (
        "守护型骨架未演示 mark_dirty()（§8.2.2 handler 只标记脏）"
    )
    assert "observed" in _used_attrs(daemon_src), (
        "守护型骨架未演示 observed()（事实从会话来，不读状态文件）"
    )
    # 守护型骨架是「复制时改名为 plugin.py」的，因此必须与地址无关地零运行期 PySide6
    assert _runtime_pyside6_imports(daemon_src) == [], (
        "守护型骨架会被改名成 plugin.py，不得含运行期 PySide6 import"
    )


# ──────────────────────────────────────────────
# 插件设置页：零模态弹框（2026-09-14 真机崩溃实测）
# ──────────────────────────────────────────────

#: 模态弹框的类名。判据一律走 AST —— 这条规则的说明里**必然**出现 `QMessageBox`
#: 这个名字，用子串会把注释 / docstring 也判成违规（本仓踩过这个坑）。
_MODAL_CLASSES = ("QMessageBox", "QInputDialog")
#: 模态入口的方法名。``exec()`` 是 QDialog / QMessageBox 唯一的阻塞入口。
#: 对照：``popup()`` / ``show()`` 非阻塞，不在禁列（§1.1-F 第 16 条实测）。
_MODAL_ENTRIES = ("exec",)
#: ``.exec()`` 的**合法接收者**名：``QApplication.exec()`` 是主事件循环（必须阻塞，
#: 不是模态框），与 ``QDialog.exec()`` 写法同形。本仓 ``QApplication`` 实例一律叫
#: ``app``（``self.app.exec()``，见 ``shell/app_shell.py`` 的 ``run()``），对话框则叫
#: ``self`` / ``dlg`` / ``box``。AST 拿不到类型，只能靠这条命名约定区分 —— 所以自证
#: 里必须有一条「``app.exec()`` 不误报」（2026-09-17 加：收编 ``app_shell.py`` 时踩到）。
_ENTRY_SAFE_RECEIVERS = ("app", "_app")


def _entry_receiver_name(node: ast.Attribute) -> str | None:
    """``X.exec()`` 里的 ``X`` 叫什么（``self.app`` → ``"app"``；``dlg`` → ``"dlg"``）。

    不是 ``Name`` / ``Attribute`` 形态（``QApplication.instance().exec()`` 之类）返回
    ``None`` —— 那类写法本门禁不认，也不会误判成合规。
    """
    recv = node.value
    if isinstance(recv, ast.Name):
        return recv.id
    if isinstance(recv, ast.Attribute):
        return recv.attr
    return None


#: 「原生文件对话框」—— ``QFileDialog``。本仓纪律原文把它**排除**在本类之外：
#: 「原生 ``QFileDialog``（父为 ``self`` ＋ ``WindowModal``）**不属本类**」
#: （`shell/ui_kit.FeedbackBar` 的类 docstring）。它开的是系统那套选择器，不是
#: 我们自绘的确认框。
_FILE_DIALOG_CLASSES = ("QFileDialog",)


def _file_dialog_bindings(tree: ast.AST) -> set[str]:
    """``X = QFileDialog(...)`` 绑出来的名字（这些名字上的 ``.exec()`` **不算**模态）。

    判据为什么不能沿用 ``_ENTRY_SAFE_RECEIVERS`` 那种「接收者名」名单：``dlg`` /
    ``dialog`` 这类名字既可能是原生文件对话框、也可能是真的 ``QDialog``（本仓刚收掉的
    ``hosts_guard`` 子对话框就叫 ``dlg``）—— 按名字放行等于**给真模态开后门**。
    按**构造点**认才是精确的：只有从 ``QFileDialog(...)`` 赋出来的那个变量算合规。

    「父为 ``self`` ＋ ``WindowModal``」这两个条件 AST 读不到类型信息，留给作者与评审
    守（本仓唯一一处调用 ``recognize/window_view.py::_pick_image`` 两个都写足了，并在
    那里说明了理由）。``self._fd = QFileDialog(...)`` 这种属性赋值也认（取末段名）。
    """
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        fn = node.value.func
        name = (fn.attr if isinstance(fn, ast.Attribute)
                else fn.id if isinstance(fn, ast.Name) else "")
        if name not in _FILE_DIALOG_CLASSES:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                out.add(target.id)
            elif isinstance(target, ast.Attribute):
                out.add(target.attr)
    return out


def _modal_box_uses(path: Path) -> list[str]:
    """文件里出现的模态弹框构造，返回 ``行号: 命中``（空列表 = 合规）。

    两类**合规**写法不误报（每类都对应一条本仓实测过的纪律，别按直觉删）：

    - ``self.app.exec()`` —— 进主事件循环（程序活着的全部时间都在里面），与
      ``QDialog.exec()`` 写法同形，靠 ``_ENTRY_SAFE_RECEIVERS`` 的接收者名区分；
    - ``dlg = QFileDialog(...)`` 之后的 ``dlg.exec()`` —— 原生文件对话框，
      按**构造点**区分（见 `_file_dialog_bindings`）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    safe_receivers = set(_ENTRY_SAFE_RECEIVERS) | _file_dialog_bindings(tree)
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            hits += [f"{node.lineno}: import {a.name}" for a in node.names
                     if a.name in _MODAL_CLASSES]
        elif isinstance(node, ast.Name) and node.id in _MODAL_CLASSES:
            hits.append(f"{node.lineno}: {node.id}")
        elif isinstance(node, ast.Attribute):
            if node.attr in _MODAL_CLASSES:
                hits.append(f"{node.lineno}: .{node.attr}")
            elif node.attr in _MODAL_ENTRIES:
                receiver = _entry_receiver_name(node)
                if receiver is not None and receiver not in safe_receivers:
                    hits.append(f"{node.lineno}: .{node.attr}()")
    return sorted(hits, key=lambda s: int(s.split(":", 1)[0]))


def test_plugin_settings_pages_have_no_modal_dialogs():
    """插件设置页**不得**使用模态弹框（QMessageBox / QDialog.exec()）。

    **实测证据**（2026-09-14，用户报「点了两次『常用场景』窗口卡住、关不掉、只能强杀」）：
    ``~/Library/Logs/DiagnosticReports/Python-2026-09-14-170030.ips`` 的
    ``lastExceptionBacktrace`` 80 帧逐帧可读 ——

    ```text
    QPushButtonWrapper::mouseReleaseEvent        ← 用户点了按钮
      PySide::SignalManager::qtPythonMetacall    ← 进 Python 槽
        Sbk_QMessageBoxFunc_information          ← QMessageBox.information
          QDialog::exec()                        ← 嵌套事件循环（第 2 层）
            CFRunLoopRunSpecific
              __CFMachPortPerform → 我们的 CGEventTap 回调（core/hotkeys.py）
                PyObjCErr_ToObjCWithGILState     ← Python 异常被转成 ObjC 异常
                  objc_exception_throw → 穿出 runloop → abort()（SIGABRT）
    ```

    即：设置窗被钉在原生层级 201（``SETTINGS_LEVEL``）且 ``WindowStaysOnTopHint``，
    从它**内部**弹模态框会在顶上再开一条事件循环；用户在那个状态下想强杀（Ctrl+C），
    SIGINT 落进主 runloop 上的 PyObjC 回调，Python 异常被 PyObjC 转成 ObjC 异常，
    无人接管 ⇒ ``terminate`` ⇒ 整个进程被 abort 掉。反馈一律走**行内**
    （先例：``plugins/notify_guard/settings.py`` 的 ``_Feedback``）。

    **扫描面（2026-09-20 扩到整个插件 UI 面）**：过去只扫 ``plugins/*/settings.py``
    —— 那次崩溃的现场，也是当时唯一有证据的一类。现在扫 ``_ui_files()``（**按 AST 判**
    的插件 UI 面：运行期 import 了 PySide6 的模块，排除域面 ``plugin.py``），于是
    ``*_view.py`` 那类运行期窗口与不带后缀的构件自动受管。理由不是「顺手扫全」：那类
    模块自 2026-09-20 起同样住在**置顶**的详情卡里（``shell/detail.py::DetailForm``
    对两种形态都设 ``WindowStaysOnTopHint`` 并抬到 ``DETAIL_LEVEL``），走的是**同一条**
    嵌套事件循环链路。本轮先收的是 ``hosts_guard/manager_view.py``（9 处模态 → 0 处，
    排期见 ``docs/plans/2026-09-20-待开发项排期.md`` §S1-a）；扩面**不留白名单**。

    **仍在面外的同类写法（如实标注，不塞白名单）**：``core/permissions.py`` 的权限引导框
    （宿主侧 ``core/``，另有账）与 ``plugins/autotype/plugin.py`` 那 4 处 —— 后者按
    ``_UI_SCAN_EXCLUDED`` 属**域面**（``plugin.py`` 是插件自己读配置的合法位置），且那几处
    是从**热键 / 托盘**点出来的、面前没有页面可挂反馈槽 ⇒ 卡在「框架侧动作结果回传通道」
    （排期表 §S1-b / §S2）。

    **名字为什么没跟着改**：它是本门禁 2026-09-17 落地时的原名，全仓 11 处引用着它，
    其中 5 处在**冻结篇**（历史台账 / 设计文档）—— 改正文等于篡改历史，而按本仓惯例
    冻结篇只追加「◷ 后续变更」。故留名，语义以本条 docstring 为准。
    （``route_guard`` / ``ipad_tunnel`` / ``autotype`` 已分别于 2026-09-17 / 09-18 / 09-19
    把设置页迁进 ``settings.py``，自动落入本门禁的扫描面。）

    **宿主那一侧另有门禁**（``test_host_ui_files_have_no_modal_dialogs``，2026-09-17
    加）：扫**整个 ``shell/`` 目录**（扫描面常量 ``_MODAL_FREE_SHELL``），覆盖设置窗
    本体（导出 / 导入结果 ＋ 卸载确认 3 处）、设置窗「维护」页的自启行（5 处）、
    装配器（``QMessageBox.about`` 1 处）与候选菜单弹层（``QMenu.exec()`` 1 处）。它的
    扫描面一开始是**逐文件点名**（当时 ``shell/`` 里还有 ``ui_kit.CandidateMenu.popup_at``
    的 ``self.exec(pos)`` 等未收口的写法，扫全目录就等于给它们开白名单），收完 §5 第 17 条
    后换成目录 —— 经过与范围见 ``_MODAL_FREE_SHELL`` 上方注释。
    """
    files = _ui_files()
    assert files, "没扫到任何插件 UI 面模块 —— 扫描面写错了，门禁等于失效"
    bad = {
        p.relative_to(PLUGINS).as_posix(): hits
        for p in files
        if (hits := _modal_box_uses(p))
    }
    assert not bad, f"插件 UI 面出现模态弹框（真机会卡死/崩溃）：{bad}"


def test_gate_modal_scan_selfcheck(tmp_path):
    """上面两条门禁的**双自证**：植入违规必抓、合规写法绝不误报。

    「合规」特意包含五样最容易误伤的东西：① 注释里提到 `QMessageBox`（本条规则的说明
    自己就要提它）；② docstring 里写 `QMessageBox.exec()`；③ `popup()` / `showPopup()`
    —— 非阻塞弹层是本仓允许的写法（§1.1-F 第 16 条实测：`QMenu.popup()` 正常返回，
    只有 `exec()` 阻塞）；④ `self.app.exec()` —— `QApplication` 的主事件循环，与模态框
    写法同形（2026-09-17 收编 `shell/app_shell.py` 时实测会误报，故判据加了接收者名单）；
    ⑤ `fd = QFileDialog(...)` 之后的 `fd.exec()` —— 原生文件对话框，本仓纪律明文把它
    排除在本类之外（2026-09-20 扩面时由 `recognize/window_view.py::_pick_image` 暴露；
    按**构造点**认，不按接收者名 —— 否则 `dlg` 这个名字就成了真模态的后门）。
    """
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from PySide6.QtWidgets import QMessageBox\n"
        "def f(self, dlg):\n"
        "    QMessageBox.warning(None, 'x', 'y')\n"
        "    dlg.exec()\n",
        encoding="utf-8",
    )
    hits = _modal_box_uses(bad)
    assert any("QMessageBox" in h for h in hits), hits
    assert any(".exec()" in h for h in hits), hits

    good = tmp_path / "good.py"
    good.write_text(
        "# 本页零 QMessageBox：一切确认都走行内\n"
        '"""docstring 里提到 QMessageBox.exec() 不算违规。"""\n'
        "def f(self, menu, combo):\n"
        "    menu.popup(pos)\n"
        "    combo.showPopup()\n"
        "    return self.app.exec()\n"
        "\n"
        "def pick(self):\n"
        "    fd = QFileDialog(self, '选择图片')\n"
        "    fd.setWindowModality(Qt.WindowModality.WindowModal)\n"
        "    return fd.exec()\n",
        encoding="utf-8",
    )
    assert _modal_box_uses(good) == []

    # ⑤ 「按构造点认」不能变成「按名字放行」：`dlg` 只是形参、不是 QFileDialog 绑出来的，
    #    它的 `.exec()` 必须照样被抓（否则 `dlg` 这个名字就是一条真模态的后门）。
    #    上面 `bad.py` 里那个 `dlg.exec()` 正是这一条 —— 两条样本合起来才算自证完整。


# ──────────────────────────────────────────────
# 插件 UI 面：数字格一律 ui_kit.NumberField（2026-09-21 滚轮误改值）
# ──────────────────────────────────────────────

#: 被禁的「裸」控件名 —— 这三个的共同点是「**滚轮改值**」：滑过它们时滚轮既改了
#: 值、又把这一次滚动吃掉（页面不动）。判据按 AST 的 ``Call`` 认，**不认子串** ——
#: 本仓 ``hosts_guard/settings.py`` 的模块 docstring 与 ``route_guard/plugin.py``
#: 的注释都在讲这条纪律（里面必然出现这些名字），子串判会当场误报。
_BARE_VALUE_WIDGETS = ("QSpinBox", "QDoubleSpinBox", "QComboBox")


def _bare_value_widget_uses(path: Path, names=_BARE_VALUE_WIDGETS) -> list[str]:
    """模块里直接构造 ``names`` 里那些控件的位置（``行号: 名字()``）。

    ``names`` 可传别的族 —— 同一套 AST 逻辑服务多条门禁（``QPushButton`` 那条走它），
    别再抄一份 walk 出来。默认值就是取值控件那一族。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name):
            name = fn.id
        elif isinstance(fn, ast.Attribute):
            name = fn.attr
        else:
            name = ""
        if name in names:
            hits.append(f"{node.lineno}: {name}()")
    return sorted(hits, key=lambda s: int(s.split(":", 1)[0]))


def test_plugin_ui_has_no_bare_value_widget():
    """插件 UI 面的**取值控件**一律走 ``ui_kit`` 的 ``NumberField`` / ``ComboField``。

    **实测证据**（2026-09-21 用户上报「滚组件设置页时端口被改掉」）：

    - ``QSpinBox``：滚轮**不需要焦点**就把值改了（焦点停在别处照样 1080 → 1081）；
    - ``QComboBox``：同一类坑且更难察觉 —— 焦点停在别处时滑过它照样把选中项改掉，
      而它的默认 ``focusPolicy`` 只是 ``TabFocus``（实测值 1），连「被聚焦」的迹象
      都没有，用户只会看见「主图标」之类的一项莫名换了；
    - 两者都把滚轮 ``accept`` 掉 ⇒ 页面不动、值已改。而设置项一律「改完即写、
      没有保存键」⇒ 一次误滚就是一个写进配置文件的错值，界面上**没有提示**。

    两个替身做的事只有一件：把滚轮 ``ignore()`` 交回滚动区（机制逐条钉在
    ``test_number_field_lets_the_wheel_scroll_the_page``）。本门禁守的是
    「**不会再长出一枚裸的**」——改值入口一个不少（键入 / ↑↓ / 步进箭头 / 点开下拉），
    少的只是「滚轮也能改」这一条。
    """
    files = _ui_files()
    assert files, "没扫到任何插件 UI 面模块 —— 扫描面写错了，门禁等于失效"
    bad = {
        p.relative_to(PLUGINS).as_posix(): hits
        for p in files
        if (hits := _bare_value_widget_uses(p))
    }
    assert not bad, f"插件 UI 面出现裸取值控件（滚轮会误改值 ⇒ 用 NumberField / ComboField）：{bad}"


def test_gate_value_widget_scan_selfcheck(tmp_path):
    """上面那条门禁的双自证：植入违规必抓、合规写法绝不误报。

    「合规」特意包含三样最容易误伤的：① 模块 docstring / 注释里讲这条纪律
    （必然出现这些控件名）；② ``from ... import QSpinBox`` 本身只是
    ``ImportFrom`` 不是 ``Call``；③ 真正的合规写法 ``NumberField(...)`` /
    ``ComboField(...)`` —— 它们是被禁控件的**子类**，按父类名扫会一起误伤。
    """
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from PySide6.QtWidgets import QComboBox, QSpinBox\n"
        "def f(self):\n"
        "    b = QSpinBox(self)\n"
        "    c = QComboBox(self)\n"
        "    return b, c\n",
        encoding="utf-8",
    )
    hits = _bare_value_widget_uses(bad)
    assert [h for h in hits if "QSpinBox()" in h], hits
    assert [h for h in hits if "QComboBox()" in h], hits

    good = tmp_path / "good.py"
    good.write_text(
        '"""本页取值控件一律 NumberField / ComboField：裸 QSpinBox 的滚轮会误改值。"""\n'
        "# 上面这行 docstring 里出现了那些名字，用子串判就会误报\n"
        "from PySide6.QtWidgets import QComboBox, QSpinBox\n"
        "from barkit.shell.ui_kit import ComboField, NumberField\n"
        "\n"
        "def f(self):\n"
        "    b = NumberField(self)\n"
        "    c = ComboField(self)\n"
        "    return b, c\n",
        encoding="utf-8",
    )
    assert _bare_value_widget_uses(good) == []


#: 插件 UI 面不得直接构造的**按钮**。存量 **0**（2026-10-16 实测）⇒ 零白名单、零迁移。
#: 与取值控件那条的**理由不同**：`QPushButton` 没有「滚轮误改值」的问题，禁它是因为
#: 一枚按钮的观感与尺寸是三样东西共同决定的（见下面那条门禁的 docstring）。
_BARE_BUTTON_WIDGETS = ("QPushButton",)


def test_plugin_ui_has_no_bare_push_button():
    """插件 UI 面的按钮一律走 ``ui_kit`` 的 ``tool_button``（或它的子类）。

    **实测存量 0**（2026-10-16，`grep` 扫 `plugins/**`）⇒ 零白名单、零迁移就能上闸。
    它挡的是「下一个插件作者又手搓一枚裸按钮」。

    裸 ``QPushButton`` 的后果不是报错，是**同一个角色的按钮在三处长得不一样**：
    `tool_button` 同时管三件事 —— ``objectName``（视觉族）、``tier`` 属性（字号与
    内边距）、手型光标。少一样就是「识别窗的按钮没有手型光标」这类漂移
    （2026-09-21 已修过一次，见 `tool_button` 的 docstring）。

    扫描面用 :func:`_ui_files`（插件 UI 面），与取值控件那条同口径 —— 宿主 ``shell/``
    是组件盒的**实现方**（QSS 的家就在那儿），扫它没有意义。
    """
    files = _ui_files()
    assert files, "没扫到任何插件 UI 面模块 —— 扫描面写错了，门禁等于失效"
    bad = {
        p.relative_to(PLUGINS).as_posix(): hits
        for p in files
        if (hits := _bare_value_widget_uses(p, _BARE_BUTTON_WIDGETS))
    }
    assert not bad, (
        "插件 UI 面出现裸按钮（改用 ui_kit.tool_button；档位见 metrics.BUTTON_HEIGHTS）："
        f"{bad}"
    )


def test_gate_bare_push_button_scan_selfcheck(tmp_path):
    """上面那条门禁的双自证：植入违规必抓，合规写法绝不误报。

    两种「合规」写法都是 ``QPushButton`` 的**子类 / 工厂**，按父类名扫会一起误伤 ——
    特意选 ``tool_button``（工厂）与 ``MenuButton``（子类）各一，把两条路都钉住。
    """
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from PySide6.QtWidgets import QPushButton\n"
        "def f(self):\n"
        "    return QPushButton('确定', self)\n",
        encoding="utf-8",
    )
    hits = _bare_value_widget_uses(bad, _BARE_BUTTON_WIDGETS)
    assert hits, hits
    assert "QPushButton()" in hits[0], hits

    good = tmp_path / "good.py"
    good.write_text(
        '"""按钮一律 tool_button —— 裸 QPushButton 会让档位与光标漂走。"""\n'
        "from PySide6.QtWidgets import QPushButton\n"
        "from barkit.shell.ui_kit import tool_button\n"
        "from barkit.shell.ui_kit.menu import MenuButton\n"
        "\n"
        "def f(self):\n"
        "    a = tool_button('确定', 'primary')\n"
        "    b = MenuButton('下拉')\n"
        "    return a, b\n",
        encoding="utf-8",
    )
    assert _bare_value_widget_uses(good, _BARE_BUTTON_WIDGETS) == []


#: 插件 UI 面不得直接构造的**勾选框**。存量 **0**（2026-10-16 迁移完毕后实测）⇒
#: 零白名单。它挡的是「下一个插件作者又手搓一个裸勾选框」。
_BARE_CHECK_BOX_WIDGETS = ("QCheckBox",)


def test_plugin_ui_has_no_bare_check_box():
    """插件 UI 面的勾选框一律走 ``ui_kit`` 的 ``check_field``。

    **实测存量 0**（2026-10-16：`plugins/**` 里 10 处构造点全部迁到 ``check_field``，
    构造调用归零、只剩 ``import QCheckBox`` 与一处返回类型标注）⇒ 零白名单即可上闸。

    它挡的不是「样式丢了」—— QSS 按类名匹配，裸勾选框照样吃到 ``styles.py`` 那段。
    挡的是**装配与默认值**：``checked`` 初值、``on_toggled`` 的接线口，以及**以后**
    任何往这个口里加的东西（如 ``::indicator`` 自绘）都只有一处要改。同
    ``test_plugin_ui_has_no_bare_push_button`` 是姊妹条：那条管按钮族、这条管勾选族。

    扫描面与理由见 :func:`_ui_files` —— 只扫插件包；宿主 ``shell/`` 是组件盒的
    **实现方**，扫它没有意义。
    """
    files = _ui_files()
    assert files, "没扫到任何插件 UI 面模块 —— 扫描面写错了，门禁等于失效"
    bad = {
        p.relative_to(PLUGINS).as_posix(): hits
        for p in files
        if (hits := _bare_value_widget_uses(p, _BARE_CHECK_BOX_WIDGETS))
    }
    assert not bad, (
        "插件 UI 面出现裸勾选框（改用 ui_kit.check_field）："
        f"{bad}"
    )


def test_gate_bare_check_box_scan_selfcheck(tmp_path):
    """上面那条门禁的双自证：植入违规必抓，合规写法绝不误报。

    合规那半特意放**两种会被「扫宽了」误伤的写法**：``check_field``（工厂，是合规
    构造口）与 ``Switch``（同族的拨动开关，名字里不含 ``QCheckBox``）—— 前者防「按
    父类名扫」，后者防有人把扫描面放宽成「任何勾选语义的控件」。
    """
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from PySide6.QtWidgets import QCheckBox\n"
        "def f(self):\n"
        "    return QCheckBox('启用', self)\n",
        encoding="utf-8",
    )
    hits = _bare_value_widget_uses(bad, _BARE_CHECK_BOX_WIDGETS)
    assert hits, hits
    assert "QCheckBox()" in hits[0], hits

    good = tmp_path / "good.py"
    good.write_text(
        '"""勾选框一律 check_field —— 裸 QCheckBox 会把装配口散到各页。"""\n'
        "from PySide6.QtWidgets import QCheckBox\n"
        "from barkit.shell.ui_kit import Switch, check_field\n"
        "\n"
        "def f(self):\n"
        "    a = check_field('启用', checked=True)\n"
        "    b = Switch()\n"
        "    return a, b\n",
        encoding="utf-8",
    )
    assert _bare_value_widget_uses(good, _BARE_CHECK_BOX_WIDGETS) == []


#: 确认位文案里出现这些词 ⇒ 那一枚按下去是**不可逆 / 破坏性**的，必须明说
#: ``confirm_kind="danger"``（实心红）。
#:
#: 判据落在**文案**而不是「调用点清单」上：清单会随调用点增删而漂移（谁新加一处删除
#: 确认，没人记得回来补一行），而「删除」「卸载」这些字正是用户在按钮上真会读到的东西。
#: 「放弃」**故意不在表里** —— 草稿还没写进配置，放弃它没有不可恢复的后果，不该升红。
_DESTRUCTIVE_CONFIRM_WORDS = ("删除", "卸载", "清空", "覆盖", "恢复", "移除", "清除", "重置")


def _ask_confirm_sites(root: Path) -> list[tuple[Path, int, str | None, str | None]]:
    """扫 ``root`` 下所有 ``xxx.ask(…)``，逐个取「确认位文案 ＋ 显式给的 ``confirm_kind``」。

    **扫描面是整个 ``src/``，不是 ``shell/`` ＋ ``plugins/``** —— 第一版只扫了那两处，于是
    漏掉 ``capabilities/helper_ops.py`` 里那枚「卸载守护」（2026-10-16，写用例时才对上数）。
    三个目录同在一个包里，按目录挑等于给自己留盲区。

    确认位是 ``ask(text, confirm_label, …)`` 的第二个**位置**参数。两者都只认字面量字符串
    （非字面量 ⇒ ``None``）：判不了就**跳过**，宁可漏报也不误报 —— 漏报那半由
    :func:`test_gate_destructive_confirm_scan_selfcheck` 的三条路盯着。
    """
    sites: list[tuple[Path, int, str | None, str | None]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "ask"):
                continue
            label = None
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                value = node.args[1].value
                label = value if isinstance(value, str) else None
            kind = None
            for kw in node.keywords:
                if kw.arg == "confirm_kind" and isinstance(kw.value, ast.Constant):
                    kind = kw.value.value
            sites.append((path, node.lineno, label, kind))
    return sites


def test_destructive_confirm_is_styled_as_danger():
    """确认位是「删除 / 卸载 / 清空 / 覆盖 / 恢复」这类词的询问，必须明说 ``danger``。

    **2026-10-16 用户上报**：「按钮没有颜色区分」—— 询问档的两枚按钮此前都是幽灵档
    （透明底 ＋ 1px 灰边框 ＋ 灰字），于是「卸载」和它右边的「取消」是同一副灰模样，
    用户读不出哪一枚按下去会出事。全仓 **13 处**询问里 **10 处**按下去不可逆。

    ``#danger`` 那一族（实心红 ＋ ``on_solid`` 白字，两主题对比度 5.26 / 4.71）在
    ``styles.py`` 里早就写好了 —— 它此前只服务面板/浮层的 ``ButtonSpec.variant``，
    **行内确认位一次也没用过**。本门禁守的就是这条：新加一处删除确认，别又用同一副灰。
    """
    sites = _ask_confirm_sites(SRC)
    assert len(sites) >= 10, (
        f"只扫到 {len(sites)} 处 ask 调用 —— 扫描面或取值方式写错了，门禁等于失效"
    )
    bad = [
        f"{path.relative_to(SRC).as_posix()}:{lineno}: 确认位「{label}」按下去不可逆，"
        '要 confirm_kind="danger"（现在这枚与普通确认同色）'
        for path, lineno, label, kind in sites
        if label and any(w in label for w in _DESTRUCTIVE_CONFIRM_WORDS) and kind != "danger"
    ]
    assert not bad, "不可逆的确认位与普通确认同色了：\n" + "\n".join(bad)


def test_gate_destructive_confirm_scan_selfcheck(tmp_path):
    """上面那条门禁的双自证：植入必抓、合规不误报。**三条路**都要钉住。

    只钉「植入必抓」是不够的 —— 那样把判据写成「任何 ``ask`` 都得给 danger」也照样转绿。
    合规那两条恰好是**最容易被扫宽误伤**的写法：给了 ``danger`` 的、以及文案里没有危险词
    的轻量确认。
    """
    (tmp_path / "sample.py").write_text(
        "def f(fb):\n"
        "    fb.ask('删掉它？', '删除', lambda: None)\n"
        "    fb.ask('删掉它？', '删除', lambda: None, confirm_kind='danger')\n"
        "    fb.ask('有改动', '保存并关闭', lambda: None)\n",
        encoding="utf-8",
    )
    sites = _ask_confirm_sites(tmp_path)
    assert [s[2] for s in sites] == ["删除", "删除", "保存并关闭"], sites

    bad = [
        s for s in sites
        if s[2] and any(w in s[2] for w in _DESTRUCTIVE_CONFIRM_WORDS) and s[3] != "danger"
    ]
    assert len(bad) == 1, bad
    assert bad[0][1] == 2, bad          # 指到**第一个** ask（模块第 2 行）


#: 「宿主 UI」零模态的**扫描面**：整个 ``src/barkit/shell/`` 目录（相对 ``SRC``）。
#:
#: 2026-09-17 之前这里是**逐一点名**的文件清单，理由是当时同目录下还有未收口的写法
#: （``ui_kit.CandidateMenu.popup_at`` 的 ``self.exec(pos)`` 等），一次扫全目录就得为
#: 它们开白名单 —— 那是**用白名单假装合规**。收完 §5 第 17 条后 ``shell/`` 全域实测
#: 零命中，于是按当时写下的约定换成目录扫描：**新文件自动受管，不必记得往清单里补一行**
#: ——「漏补 ⇒ 门禁静默失效」这个坑，本项目在 ``docs/guides`` 的分区索引上已经踩过一次。
#:
#: 收口历史（文件 → 原写法，全部 2026-09-17）：
#:   ``settings.py``      导出 / 导入结果（``information`` / ``warning``）＋ 卸载 ``question``，3 处
#:   ``autostart_row.py`` 自启行：安装失败 / 已更新 / 关闭确认 / 关闭失败 / 已关闭，5 处
#:   ``app_shell.py``     「? 帮助」的 ``QMessageBox.about``，1 处
#:   ``ui_kit.py``        候选菜单 ``popup_at`` 的 ``QMenu.exec()``，1 处（§5 第 17 条）
#:   ``panel.py``         上述弹层的两个调用方（徽标点击 / 行内菜单控件）随之改走回调
#:
#: **扫描面之外仍有同类写法**（不在本门禁范围，各有挂账）：``core/permissions.py`` 的
#: ``ensure_accessibility()``（热键路径调用的权限引导框）；各插件的 ``plugin.py`` /
#: ``*_dialog.py``（由上面那条插件侧门禁按其模块名挂账）。
_MODAL_FREE_SHELL = "shell"


def test_host_ui_files_have_no_modal_dialogs():
    """宿主自己写的 UI 代码（整个 ``src/barkit/shell/``）**不得**用模态弹框。

    与 ``test_plugin_settings_pages_have_no_modal_dialogs`` 同一条判据、同一套实现
    （``_modal_box_uses``），区别只在扫描面：那条扫**插件**设置页，这条扫**宿主自己的
    shell 层**。理由同源 —— 从置顶窗（设置窗钉在原生层级 ``SETTINGS_LEVEL``、面板与
    详情卡同样置顶）内部弹模态框会另开一条嵌套事件循环（实测崩溃链路见上面那条门禁的
    docstring）。

    扫描面从「逐一点名」改成「目录」的经过与范围说明见 ``_MODAL_FREE_SHELL`` 上方注释。
    """
    files = sorted((SRC / _MODAL_FREE_SHELL).rglob("*.py"))
    assert files, "没扫到任何 shell 模块——扫描面写错了，门禁等于失效"
    bad = {
        p.relative_to(SRC).as_posix(): hits
        for p in files
        if (hits := _modal_box_uses(p))
    }
    assert not bad, f"宿主 UI 出现模态弹框（真机会卡死/崩溃）：{bad}"


# ──────────────────────────────────────────────
# 插件声明面命名门禁（《组件契约决议》D10 / D11，2026-09-15 改名后落地）
#
# 两条共同守「那两个声明位只有一个名字、一种比较方式」：
#   ① 旧常量名 `KIND_*` / `ROLE_*` 不得以任何形式回归（引用了不存在的名字就是坏代码）；
#   ② `run_mode` / `failure_policy` 的比较**必须走常量**，不得与字符串字面量比
#      （`== "daemon"` 这类写法会绕过常量改名：常量改了它不报错，只是永远不成立）。
#
# **扫描面边界**：① 扫 `src/barkit` ＋ `tests/fixtures/external-plugin-template` ＋ `tests/`（旧名是**全仓**约定，
# `tests/fixtures/external-plugin-template` 是外部插件的复制源，它写错会被用户抄走）；② 只扫 `src/barkit`
# （测试里允许拿字面量锁取值，那是**断言取值范围**，不是生产路径的判定）。
# ──────────────────────────────────────────────

_REPO = Path(__file__).resolve().parents[1]
_EXAMPLE = _REPO / "tests" / "fixtures" / "external-plugin-template"
_TESTS = _REPO / "tests"

#: D10 / D11 删掉的四个常量名。它们是 `ast.Name`，与同名字符串字面量无关。
_LEGACY_PLUGIN_CONSTANTS = ("KIND_ACTION", "KIND_DAEMON", "ROLE_EXECUTE", "ROLE_OBSERVE")

#: 两个声明位的属性名（属性读写处即消费点）。
_MODE_ATTRS = ("run_mode", "failure_policy")


def _legacy_constant_uses(path: Path) -> list[str]:
    """模块里引用旧常量名（`ast.Name`）的位置，空 = 合规。

    用 AST 而非子串：注释与 docstring 里提到旧名是**允许**的（本门禁自己的说明就要提它），
    只有真的把它当名字用（import / 表达式）才算违规。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        f"{path.name}:{n.lineno}: {n.id}"
        for n in ast.walk(tree)
        if isinstance(n, ast.Name) and n.id in _LEGACY_PLUGIN_CONSTANTS
    ]


def test_no_legacy_kind_role_constant_names():
    """旧常量名 `KIND_*` / `ROLE_*` 在 src ＋ example ＋ tests 里零引用（D10 / D11）。

    它们已从 `core.plugin` 删除，任何引用都必然 ImportError 或 `NameError`；但外部
    复制源（`tests/fixtures/external-plugin-template`）写错了不会当场炸（那是用户复制之后才炸），故一并纳入扫描面。
    """
    files = sorted(SRC.rglob("*.py")) + sorted(_EXAMPLE.rglob("*.py")) + sorted(_TESTS.glob("test_*.py"))
    assert files, "扫描面为空——门禁等于失效"
    bad = {p.relative_to(_REPO).as_posix(): hits for p in files if (hits := _legacy_constant_uses(p))}
    assert not bad, f"仍有旧常量名引用（已删除，见 D10 / D11）：{bad}"


def test_mode_literals_never_used_in_comparisons():
    """`run_mode` / `failure_policy` 的判定只许与常量比，不得与字符串字面量比。

    `plugin.run_mode == "daemon"` 是**改名漏洞**：常量从 `KIND_DAEMON` 改成
    `RUN_PERIODIC` 时它不会报错，只是永远不成立 —— 插件静默不进调度器。
    """
    bad: dict[str, list[str]] = {}
    for p in sorted(SRC.rglob("*.py")):
        hits = _literal_mode_comparisons(p)
        if hits:
            bad[p.relative_to(_REPO).as_posix()] = hits
    assert not bad, f"run_mode / failure_policy 与字面量比较（应改用常量）：{bad}"


def _literal_mode_comparisons(path: Path) -> list[str]:
    """比较表达式里一侧读了声明位属性、另一侧是字面量的位置，空 = 合规。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        reads_mode = any(
            isinstance(o, ast.Attribute) and o.attr in _MODE_ATTRS for o in operands
        )
        if not reads_mode:
            continue
        if any(isinstance(o, ast.Constant) and o.value is not None for o in operands):
            out.append(f"{path.name}:{node.lineno}")
    return out


def test_gate_legacy_plugin_vocabulary_selfcheck(tmp_path):
    """上面两条门禁的**双自证**：植入违规必抓、合规写法绝不误报。

    「合规」特意包含三样最容易误伤的东西：① 注释里提旧名；② docstring 里提旧名；
    ③ 常量与属性名**同形但不是同一物**——`Line(kind=...)`（hosts 文件行类型）、
    `spec.kind`（helper schema 值域）、`tool_button(kind="primary")`（按钮样式）
    全仓都在用，它们与插件执行方式无关，不得误报。
    """
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from barkit.core.plugin import KIND_DAEMON\n"
        "if plugin.kind == KIND_DAEMON:\n"
        "    pass\n"
        "if plugin.run_mode == \"daemon\":\n"
        "    pass\n",
        encoding="utf-8",
    )
    legacy = _legacy_constant_uses(bad)
    assert any("KIND_DAEMON" in h for h in legacy), legacy
    literals = _literal_mode_comparisons(bad)
    assert len(literals) == 1, literals          # 只有 run_mode 那行，常量那行不算

    good = tmp_path / "good.py"
    good.write_text(
        "# 本模块不再有 KIND_DAEMON / ROLE_OBSERVE（D10 / D11 已改名）\n"
        '"""docstring 里提到 KIND_DAEMON 不算违规，只有真的当名字用才算。"""\n'
        "from barkit.core.plugin import RUN_PERIODIC\n"
        "def f(plugin, spec, ln, op):\n"
        "    if plugin.run_mode != RUN_PERIODIC:\n"
        "        return\n"
        "    if plugin.failure_policy != FAIL_BREAK:\n"
        "        return\n"
        "    ln = Line(kind=KIND_ENTRY)\n"
        "    assert spec.kind in allowed\n"
        "    tool_button('x', kind='primary')\n",
        encoding="utf-8",
    )
    assert _legacy_constant_uses(good) == []
    assert _literal_mode_comparisons(good) == []


# ──────────────────────────────────────────────
# 状态词汇 / 取值声明门禁（《组件契约决议》D12 / D13，2026-09-15 落地）
#
# 三条共同守「一套取值只有一个真源」：
#   ① 旧类型名 `StatusSnapshot` / `StatusLevel` 零回归（D12：词根统一成 State）——
#      它们已从 `contribution` 删除，任何引用都必然 ImportError；而外部插件的**复制源**
#      （`tests/fixtures/external-plugin-template`）写错不会当场炸（用户复制之后才炸），故与 D10 同款一并纳入扫描面。
#   ② 「取值名 → 级别 / 文案 / 图标」只许在 `state_view.resolve` 一处合成（D13）——
#      面板行、标题栏、代表态、托盘各自回查一次声明，回落口径迟早分叉（状态同源 §1-4）。
#   ③ 组件运行期不得自己说级别（D13）—— 级别是**取值**的属性、声明在 `StateOption`；
#      `tick()` 里现算级别（旧写法：自动化关了就把显示态压成 IDLE）正是本次消灭的东西，
#      运行期要比就比**取值名**。
# ──────────────────────────────────────────────

#: D12 换掉的旧类型名。已删除，引用了不存在的名字就是坏代码。
_LEGACY_STATE_TYPES = ("StatusSnapshot", "StatusLevel")

#: 允许出现级别字面量的**声明层**构造：取值声明（`StateOption`）。
#: 它在 `contributions()` 里建、不随轮次变，属声明不属运行期。
#: 徽标触发条件（`BadgeRuleSpec`）已随决议 D17 删除 —— 提醒意图现在也是取值的一个属性。
_LEVEL_DECL_CALLS = ("StateOption",)

#: 状态格的唯一解析点（D13）。
_STATE_VIEW = SRC / "shell" / "renderers" / "state_view.py"


def _legacy_state_type_uses(path: Path) -> list[str]:
    """模块里把旧类型名**当名字用**的位置，空 = 合规。

    用 AST 而非子串：注释与 docstring 里提到旧名是允许的（本门禁自己的说明就要提它），
    只有真的当名字用（表达式 / 属性访问）才算违规。`import` 那一行的导入名是
    `ast.alias`，不计入 —— 半改名的文件会在**用**的那处被抓住。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and n.id in _LEGACY_STATE_TYPES:
            out.append(f"{path.name}:{n.lineno}: {n.id}")
        elif isinstance(n, ast.Attribute) and n.attr in _LEGACY_STATE_TYPES:
            out.append(f"{path.name}:{n.lineno}: .{n.attr}")
    return out


def _declaration_lookup_calls(path: Path) -> list[str]:
    """自己回查取值声明的位置（`<spec>.option(...)`），空 = 没有自己的翻译路径。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        f"{path.name}:{n.lineno}"
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "option"
    ]


def _level_uses_outside_declaration(path: Path) -> list[str]:
    """组件在**声明之外**提到级别的位置，空 = 合规（D13）。

    按行去重：`StateLevel.IDLE` 一行里既是 `ast.Name`（`StateLevel`）又是
    `ast.Attribute`（`.IDLE`），两者同一行，报一处即可。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    decl: list[tuple[int, int]] = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        func = n.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name in _LEVEL_DECL_CALLS:
            decl.append((n.lineno, n.end_lineno or n.lineno))
    lines = {
        n.lineno
        for n in ast.walk(tree)
        if (isinstance(n, ast.Name) and n.id == "StateLevel")
        or (
            isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)
            and n.value.id == "StateLevel"
        )
    }
    return [
        f"{path.name}:{ln}"
        for ln in sorted(lines)
        if not any(lo <= ln <= hi for lo, hi in decl)
    ]


def test_no_legacy_status_snapshot_level_type_names():
    """旧类型名 `StatusSnapshot` / `StatusLevel` 在 src ＋ example ＋ tests 里零引用（D12）。

    二者已改名成 `StateReport` / `StateLevel`（词根统一，「状态」只有 State 一个词）。
    旧名从 `contribution` 删干净了，任何引用都必然 ImportError；`tests/fixtures/external-plugin-template` 是外部插件的
    复制源，写错要到用户复制之后才炸，故一并纳入扫描面。
    """
    files = (
        sorted(SRC.rglob("*.py"))
        + sorted(_EXAMPLE.rglob("*.py"))
        + sorted(_TESTS.glob("test_*.py"))
    )
    assert files, "扫描面为空——门禁等于失效"
    bad = {
        p.relative_to(_REPO).as_posix(): hits
        for p in files
        if (hits := _legacy_state_type_uses(p))
    }
    assert not bad, f"仍有旧类型名引用（已删除，见 D12）：{bad}"


def test_state_option_lookup_has_a_single_resolution_point():
    """「取值名 → 级别 / 文案 / 图标」只在 `state_view.resolve` 一处合成（D13）。

    回查声明这件事**只许发生一次**：面板行、标题栏悬停清单、代表态严重度、托盘主图
    若各自 `spec.option(...)` 一遍，`summary` 覆盖默认文案、取值图标回落主图标这两条
    口径就有四处副本 —— 改一处、其余三处照旧。
    """
    bad: dict[str, list[str]] = {}
    for p in sorted(SRC.rglob("*.py")):
        if p == _STATE_VIEW:
            continue
        if hits := _declaration_lookup_calls(p):
            bad[p.relative_to(_REPO).as_posix()] = hits
    assert not bad, f"绕开单一解析点自己回查取值（应改用 state_view.resolve）：{bad}"
    assert _declaration_lookup_calls(_STATE_VIEW), (
        "唯一解析点没在回查声明——门禁形同虚设（它得先自己回查，才能要求别人不回查）"
    )


def test_component_runtime_never_speaks_state_levels():
    """组件运行期不得自己说级别（D13）：级别归取值声明，运行期只报取值名。

    扫描面 = `src/barkit/plugins`（含骨架）＋ `tests/fixtures/external-plugin-template`。允许级别出现在声明层的唯一
    一处（`StateOption`），其余位置一律按「运行期现算级别」论 —— 那是
    「一套取值三个真源」的入口：界面按声明上色，组件心里另有想法。
    """
    files = sorted((SRC / "plugins").rglob("*.py")) + sorted(_EXAMPLE.rglob("*.py"))
    assert files, "扫描面为空——门禁等于失效"
    bad = {
        p.relative_to(_REPO).as_posix(): hits
        for p in files
        if (hits := _level_uses_outside_declaration(p))
    }
    assert not bad, (
        f"组件运行期造了级别（级别归 StateOption 声明；运行期要比就比取值名）：{bad}"
    )


def test_gate_state_vocabulary_selfcheck(tmp_path):
    """上面三条门禁的**双自证**：植入违规必抓、合规写法绝不误报。

    「合规」特意包含四样最容易误伤的东西：① 注释里提旧类型名（本门禁的说明自己就要提）；
    ② docstring 里提旧类型名；③ 取值声明里的 `StateLevel.OK` / `StateLevel.ERR`
    （同属声明层，必须放行）；④ 注释里写到 `spec.option(` 的字样
    （那不是调用）。反过来，`import` 一行**不算**使用 —— 半改名的文件会在真正用到它的
    那一行被抓住，报错行号才指得到问题。
    """
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from barkit.contribution import StateOption, StateLevel, StatusSnapshot\n"
        "# 已废弃的旧名 StatusLevel（注释里提它不算违规）\n"
        "STATES = (StateOption('ok', StateLevel.OK, text='好'),)\n"
        "def tick(self, spec):\n"
        "    level = StateLevel.IDLE if not self.active else StateLevel.OK\n"
        "    opt = spec.option('ok')\n"
        "    if isinstance(level, StatusLevel):\n"
        "        pass\n"
        "    return StatusSnapshot(level=level, text=opt.text)\n",
        encoding="utf-8",
    )
    legacy = _legacy_state_type_uses(bad)
    assert sorted(legacy) == ["bad.py:7: StatusLevel", "bad.py:9: StatusSnapshot"], legacy
    lookups = _declaration_lookup_calls(bad)
    assert lookups == ["bad.py:6"], lookups
    # 第 3 行是声明、必须放行；第 5 行是运行期现算级别、必须抓
    levels = _level_uses_outside_declaration(bad)
    assert levels == ["bad.py:5"], levels

    good = tmp_path / "good.py"
    good.write_text(
        "# 本模块不再有 StatusSnapshot / StatusLevel（D12：词根统一成 State）\n"
        '"""docstring 里提 StatusSnapshot 不算违规，只有真的当名字用才算。"""\n'
        "from barkit.contribution import StateLevel, StateOption, StateReport\n"
        "STATES = (\n"
        "    StateOption('ok', StateLevel.OK, text='好'),\n"
        "    StateOption('bad', StateLevel.ERR, text='坏'),\n"
        ")\n"
        "def contributions(self):\n"
        "    return StateOption('x', StateLevel.ERR, text='坏')\n"
        "def tick(self):\n"
        "    # spec.option(...) 只许在 state_view 里出现，这里只是注释\n"
        "    return StateReport('bad')\n",
        encoding="utf-8",
    )
    assert _legacy_state_type_uses(good) == []
    assert _declaration_lookup_calls(good) == []
    assert _level_uses_outside_declaration(good) == []


# ──────────────────────────────────────────────
# 建议周期声明门禁（《组件契约决议》D21，2026-09-16 落地）
#
# 守「周期组件必须自己说清楚该多久跑一次」：`RUN_PERIODIC` 的组件类必须声明
# `suggested_poll`（正整数秒）。框架按它定 `plugins.<id>.poll` 的默认值，用户仍可
# 覆盖 —— 组件给的是建议，不是命令。
#
# **为什么要门禁**：漏填不会报错，只会静默按兜底值（30s）跑 —— 一个本该 3s 刷新的
# 守卫变成 30s，界面上看不出来，日志里只有一行 warning（`scheduler.suggested_poll_of`）。
# 这类"声明缺席"和 D20 的"拼错声明点"是同一类病：错在没写，而不是写在错的地方。
#
# **扫描面**：`src/barkit/plugins/**` ＋ `tests/fixtures/external-plugin-template**`。`tests/fixtures/external-plugin-template` 是外部插件的复制源，
# 写错要到用户复制之后才炸（与 D10 / D12 两条门禁同款口径）。
# ──────────────────────────────────────────────

_POLL_DECL_ATTR = "suggested_poll"


def _class_attr_assigns(node: ast.ClassDef) -> dict[str, ast.AST]:
    """类体里的类属性赋值 ``{名字: 值节点}``（仅顶层 Assign / 带值的 AnnAssign）。"""
    out: dict[str, ast.AST] = {}
    for item in node.body:
        if (isinstance(item, ast.Assign) and len(item.targets) == 1
                and isinstance(item.targets[0], ast.Name) and item.value is not None):
            out[item.targets[0].id] = item.value
        elif (isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
                and item.value is not None):
            out[item.target.id] = item.value
    return out


def _poll_less_periodic_classes(path: Path) -> list[str]:
    """周期组件却没给出合法建议周期的类（空 = 合规）。

    判据两条：① 类的 ``run_mode`` 赋值为常量 ``RUN_PERIODIC``（**字面量
    ``"periodic"`` 不算**——那是 D10 门禁管的另一件事，本门禁不重复判）；
    ② ``suggested_poll`` 是正整数常量（``0`` / 负数 / ``True`` 都算写坏）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        attrs = _class_attr_assigns(node)
        mode = attrs.get("run_mode")
        if not (isinstance(mode, ast.Name) and mode.id == "RUN_PERIODIC"):
            continue
        poll = attrs.get(_POLL_DECL_ATTR)
        ok = (
            isinstance(poll, ast.Constant)
            and isinstance(poll.value, int)
            and not isinstance(poll.value, bool)
            and poll.value > 0
        )
        if not ok:
            out.append(f"{node.name} @L{node.lineno}")
    return out


def test_periodic_plugin_declares_suggested_poll():
    """`RUN_PERIODIC` 的组件必须声明 ``suggested_poll``（D21）。

    「漏填」等于把节拍决策权静默交回框架兜底值：界面照常、日志只有一行 warning。
    框架侧对漏填只做 warning（不隔离组件——它还能跑，只是节拍不对），所以这条
    静态判据是唯一能把「漏填」挡在提交前的地方。
    """
    files = sorted((SRC / "plugins").rglob("*.py")) + sorted(_EXAMPLE.rglob("*.py"))
    assert files, "扫描面为空——门禁等于失效"
    bad = {
        p.relative_to(_REPO).as_posix(): hits
        for p in files
        if (hits := _poll_less_periodic_classes(p))
    }
    assert not bad, (
        "周期组件没声明建议周期（D21：RUN_PERIODIC 必填 suggested_poll，正整数秒）：\n"
        + "\n".join(f"  {k}: {', '.join(v)}" for k, v in bad.items())
        + "\n改法：在类属性区加 ``suggested_poll = <秒>``（取值＝你希望框架多久跑一次 tick；"
          "用户仍可在配置里覆盖）。"
    )


def test_suggested_poll_gate_selfcheck(tmp_path):
    """上面那条门禁的**双自证**：植入违规必抓、合规写法绝不误报。

    「合规」特意包含四样最容易误伤的东西：① 注释里提 ``suggested_poll``（本条规则的
    说明自己就要提它）；② 按需触发的组件填了它（D21 明示：填了不生效、不报错）；
    ③ 周期组件填了合法正整数；④ ``run_mode`` 是字符串字面量（那是 D10 门禁的对象，
    本门禁只认常量，不重复判、也不误报）。
    """
    bad = tmp_path / "bad.py"
    bad.write_text(
        "class Missing:\n"
        "    run_mode = RUN_PERIODIC\n"
        "class Zero:\n"
        "    run_mode = RUN_PERIODIC\n"
        "    suggested_poll = 0\n"
        "class Bool:\n"
        "    run_mode = RUN_PERIODIC\n"
        "    suggested_poll = True\n",
        encoding="utf-8",
    )
    assert _poll_less_periodic_classes(bad) == [
        "Missing @L1", "Zero @L3", "Bool @L6",
    ], _poll_less_periodic_classes(bad)

    good = tmp_path / "good.py"
    good.write_text(
        "# 本模块的周期组件都填了 suggested_poll（注释里提它不算违规）\n"
        '"""docstring 里提 suggested_poll 不算违规，只有真缺声明才算。"""\n'
        "class OnDemand:\n"
        "    run_mode = RUN_ON_DEMAND\n"
        "    suggested_poll = 3\n"
        "class Periodic:\n"
        "    run_mode = RUN_PERIODIC\n"
        "    suggested_poll = 5\n"
        "class Legacy:\n"
        '    run_mode = \"periodic\"\n',
        encoding="utf-8",
    )
    assert _poll_less_periodic_classes(good) == []


# ──────────────────────────────────────────────
# 配置键单消费者门禁（《组件契约决议》D27，2026-09-16 落地）
#
# 守「一个配置键只服务一个消费者」：`plugins.<id>.poll` 是**框架轮询 tick 的间隔**
# （默认值由组件的 `suggested_poll` 给，D21）。组件自己的后台节律必须另起键名 ——
# `hosts_guard` 的自动更新（重新解析）间隔是 `resolve_poll`，`route_guard` 写进
# `routes.json` 给 root helper 的节律是 `helper_poll`。
#
# **为什么要门禁**：复用 `poll` 不报错 —— 两个消费者各读各的默认值，界面上一切正常，
# 只有「改了一个键、另一件事跟着变」这种怪症状。而且框架节拍的默认值来自组件声明，
# 组件若再读 `poll`，等于把那份声明又变成一份可以分叉的真相。
#
# **扫描面**：`src/barkit/plugins/**`（组件自带模块也算）＋ `tests/fixtures/external-plugin-template**`。
# **只抓「读配置段」这一种形态**（`.get("poll")` / `["poll"]` 且接收者是配置段类名字）：
# 线格式文件里的 `poll`（`routes.json` / `hosts_rules.json`）是**另一层** —— 已装在
# 机器上的 root helper 在读它、改名要重装，而读它拿到的是 `read_json(...)` 的产物，
# 接收者不叫 cfg / section 之类，故不会误报。
# **豁免**：`_migrate_*` 函数（拆键时的**一次性搬家**，读完就不再读），见下面 `_MIGRATION_FN_PREFIX`。
# ──────────────────────────────────────────────

#: 判为「插件配置段」的接收者名字（子串匹配）。命中 + 读 ``poll`` = 违规。
_CFG_RECEIVERS = ("cfg", "config", "section")

#: 豁免：**一次性迁移**函数（``_migrate_*``）里读旧键是它存在的理由 —— 把用户调过的值
#: 从旧键搬到新键，搬完就不再读。判据是**函数名**（不是文件、不是行号）：想让一处读旧键
#: 合法，只能把它放进这样一个具名函数里，理由必须写在函数 docstring 上。
_MIGRATION_FN_PREFIX = "_migrate"


def _walk_with_owner(tree: ast.AST):
    """深度优先遍历，逐个产出 ``(所属函数名, 节点)``；模块级节点归空名。"""
    stack: list[tuple[str, ast.AST]] = [("", tree)]
    while stack:
        owner, node = stack.pop()
        yield owner, node
        child_owner = getattr(node, "name", owner) if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)) else owner
        for child in ast.iter_child_nodes(node):
            stack.append((child_owner, child))


def _poll_reads_from_config(path: Path) -> list[int]:
    """从**配置段**读 ``poll`` 的行号（空 = 合规）。

    判据两种写法：``X.get("poll", ...)`` 与 ``X["poll"]``；``X`` 的源码文本里含
    ``plugin_config`` 或名字类字（cfg / config / section）才算「配置段」——
    读线格式文件的接收者（``routes`` / ``raw`` / ``data``）不在其中。
    位于 ``_migrate_*`` 函数内的读取豁免（那是一次性搬家，见上面的说明）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[int] = []
    for owner, node in _walk_with_owner(tree):
        if owner.startswith(_MIGRATION_FN_PREFIX):
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr != "get" or not node.args:
                continue
            key, recv = node.args[0], node.func.value
        elif isinstance(node, ast.Subscript):
            key, recv = node.slice, node.value
        else:
            continue
        if not (isinstance(key, ast.Constant) and key.value == "poll"):
            continue
        text = ast.unparse(recv).lower()
        if "plugin_config" in text or any(k in text for k in _CFG_RECEIVERS):
            out.append(node.lineno)
    return sorted(out)


def test_component_does_not_read_framework_poll_key():
    """组件不得把配置键 ``poll`` 当自己的业务节律读（D27）。"""
    files = sorted((SRC / "plugins").rglob("*.py")) + sorted(_EXAMPLE.rglob("*.py"))
    assert files, "扫描面为空——门禁等于失效"
    bad = {
        p.relative_to(_REPO).as_posix(): hits
        for p in files
        if (hits := _poll_reads_from_config(p))
    }
    assert not bad, (
        "组件把框架的 ``poll`` 当自己的业务节律读了（D27：一个配置键只服务一个消费者）：\n"
        + "\n".join(f"  {k}: L{', L'.join(str(n) for n in v)}" for k, v in bad.items())
        + "\n改法：另起一个键名（如 ``resolve_poll`` / ``helper_poll``）—— ``poll`` 这条"
          "只留给 ``core/scheduler.py`` 读。"
    )


def test_poll_single_owner_gate_selfcheck(tmp_path):
    """上面那条门禁的**双自证**：植入违规必抓、合规写法绝不误报。

    「合规」特意包含最容易误伤的四样：① 读**线格式**文件的 ``poll``
    （`routes.json` / `hosts_rules.json` 的字段，不是配置键）；② 写线格式文档时的
    dict 字面量；③ 注释 / docstring 里提 ``poll``；④ ``_migrate_*`` 函数里对旧键的
    一次性搬家（豁免的那一种）。
    """
    bad = tmp_path / "bad.py"
    bad.write_text(
        "def f(plugin_cfg, self):\n"
        '    a = plugin_cfg.get("poll", 30)\n'
        '    b = self.plugin_config().get("poll", 30)\n'
        '    c = plugin_cfg["poll"]\n'
        '    d = self.ctx.config.user_plugin_section("x").get("poll")\n'
        "def _migrate_but_then_uses_it(plugin_cfg):\n"
        '    e = plugin_cfg.get("poll", 30)\n',      # 豁免只免「读」，不免「当业务值用」
        encoding="utf-8",
    )
    assert _poll_reads_from_config(bad) == [2, 3, 4, 5], _poll_reads_from_config(bad)

    good = tmp_path / "good.py"
    good.write_text(
        "# 注释里提 poll 不算违规（本门禁的说明自己就要提它）\n"
        '"""docstring 里提 poll 也不算。"""\n'
        "def f(plugin_cfg, routes, raw):\n"
        '    a = plugin_cfg.get("helper_poll", 30)    # 业务节律＝自己的键\n'
        '    b = plugin_cfg.get("resolve_poll", 300)  # 同上\n'
        '    c = routes.get("poll", 30)               # 线格式字段（helper 读）\n'
        '    d = raw.get("poll", 300)                 # 线格式字段\n'
        '    e = plugin_cfg.get("poll_value")\n'      # 前缀相同但不是这条键
        '    g = {"version": 1, "poll": 30, "rules": []}\n'  # 线格式文档（写，不是读）
        "class P:\n"
        "    def _migrate_legacy_config(self):\n"
        "        section = self.ctx.config.user_plugin_section(self.id)\n"
        '        old = section.get("poll")\n'              # 一次性搬家：豁免
        '        dead = "state_poll" in section\n',
        encoding="utf-8",
    )
    assert _poll_reads_from_config(good) == [], _poll_reads_from_config(good)


# ──────────────────────────────────────────────
# D4 显示位开关：用户层开关的**键名只有一个落点**
#
# `plugins.<id>.ui.<slot>.visible` 这条键路径只有配置中心知道
# （读侧 `Config.plugin_ui_visible` / 写侧 `Config.set_plugin_ui_visible`）。
#
# **为什么要门禁**：D4 的前置依赖写的是「插件用户态与设置页写回必须走同一条
# 读-改-写路径，不得各写一套」（插件结构规范 §6.3）。多一份自己拼键名的实现，
# 改键名时必漏改一处，而漏改的那半**不报错** —— 只是那个开关从此永远读不到，
# 界面上一切照常、「点了没反应」。
#
# **扫描面**：`src/barkit/**`，扣掉配置中心**这一族**。只认字符串常量 `"visible"`
# —— 窄而准：`widget.setVisible(False)` 是属性名不是字符串，不会被误伤。
# ──────────────────────────────────────────────

#: 键名的**真身**（2026-10-03 由 `core/config.py` 随拆包改成包内模块）。
#: 拆包前按路径点名那个单文件；拆包后那条键名住在 ``core/config/plugins.py``
#: （``plugin_ui_visible`` / ``set_plugin_ui_visible`` 那两段）。判据要点名**真身模块**、
#: 不要点名旧文件名 —— 后者只剩一条过期线索。
_UI_KEY_OWNER = SRC / "core" / "config" / "plugins.py"

#: 允许持有那条键名的**文件族**（配置中心这一族）。按目录收而不按单文件收：
#: 拆包把正文分给了 11 个文件，而「除配置中心之外都不许有」这条规则的**对象**
#: 从来是配置中心，不是某一个文件名。
_UI_KEY_OWNER_FAMILY = SRC / "core" / "config"

# 「visible」作为**独立词**出现（前后不是字母 / 下划线）才算拼键名。
# 三条边界：`ui_visible`（方法名，前面是下划线）不算；`invisibleness` 不算；
# 而 `"plugins.%s.ui.%s.visible"` / `f"...ui.{slot}.visible"` 都算。
_VISIBLE_WORD = re.compile(r"(?<![A-Za-z_])visible(?![A-Za-z_])")


def _docstring_ids(tree: ast.AST) -> set[int]:
    """三引号说明串的节点 id —— 它们只是**提到**这个词，不是在拼键名。"""
    owner_types = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, owner_types):
            continue
        body = getattr(node, "body", [])
        first = body[0] if body else None
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            out.add(id(first.value))
    return out


def _visible_key_uses(path: Path) -> list[str]:
    """文件里把 ``visible`` 拼进**配置键**的位置（带行号；空 = 合规）。

    三种写法都要抓：``section["visible"]``（原样字面量）、
    ``"plugins.%s.ui.%s.visible" % ...``（% 拼接）、``f"...ui.{slot}.visible"``
    （f-string —— Python 3.12 起是 ``JoinedStr`` 里的常量片段，逐个常量判）。
    只认字面量就漏了后两种，而「换个拼法」正是绕过单点最常见的方式。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstring_ids(tree)
    return [
        f"L{node.lineno}"
        for node in ast.walk(tree)
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in skip
            and _VISIBLE_WORD.search(node.value))
    ]


def test_ui_visible_key_has_a_single_owner():
    """显示位开关的键名只许出现在配置中心这一族（D4 单点读写路径）。"""
    assert _UI_KEY_OWNER.is_file(), (
        f"键名真身模块不在：{_UI_KEY_OWNER} —— 配置中心拆包后它搬到了别的文件，"
        "请同步更新 _UI_KEY_OWNER 与 _UI_KEY_OWNER_FAMILY"
    )
    family = {p.resolve() for p in _UI_KEY_OWNER_FAMILY.rglob("*.py")}
    # ⚠️ 收窄成单文件时 `rglob` 作用在**文件**上返回空集（0 个），而报错文案若写
    # 「只剩 1 个」会把人引向错误的修法（去数文件个数）。这里断言的是「多于一个」。
    assert len(family) > 1, (
        f"配置中心这一族只收到 {len(family)} 个文件（{sorted(f.name for f in family)}）"
        " ⇒ 判据退回了「按单文件点名」的老形状，而正文早就搬进兄弟模块了；"
        "族必须是目录"
    )
    files = [p for p in sorted(SRC.rglob("*.py")) if p.resolve() not in family]
    assert files, "扫描面为空——门禁等于失效"
    bad = {
        p.relative_to(_REPO).as_posix(): hits
        for p in files
        if (hits := _visible_key_uses(p))
    }
    assert not bad, (
        "显示位开关的键名出现在配置中心之外（决议 D4：读-改-写只许一套）：\n"
        + "\n".join(f"  {k}: {', '.join(v)}" for k, v in bad.items())
        + "\n改法：改键名只在 core/config/plugins.py；别处一律经 "
          "Config.plugin_ui_visible() / set_plugin_ui_visible()。"
    )
    # 反恒真：真源那一份必须真的还在，否则「除它之外都不许有」变成「谁都不许有」，
    # 门禁全绿而功能已死。
    assert _visible_key_uses(_UI_KEY_OWNER), "真源里没有这条键了——门禁已失效"


def test_ui_visible_key_gate_selfcheck(tmp_path):
    """上面那条门禁的**双自证**：三种拼法必抓、属性名与别的字符串不误报。"""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "def by_key(section, slot):\n"
        '    return section["ui"][slot]["visible"]\n'
        "def by_percent(pid, slot):\n"
        "    return 'plugins.%s.ui.%s.visible' % (pid, slot)\n"
        "def by_fstring(pid, slot):\n"
        '    return f"plugins.{pid}.ui.{slot}.visible"\n',
        encoding="utf-8",
    )
    assert _visible_key_uses(bad) == ["L2", "L4", "L6"], _visible_key_uses(bad)

    good = tmp_path / "good.py"
    good.write_text(
        "# 注释里提 visible 不算违规\n"
        '"""docstring 里提 visible、以及 ``Config.plugin_ui_visible`` 都不算。"""\n'
        "def hide(widget):\n"
        "    widget.setVisible(False)\n"
        '    widget.setObjectName("invisibleness")\n'
        '    return {"hidden": True}["hidden"]\n',
        encoding="utf-8",
    )
    assert _visible_key_uses(good) == []


# ──────────────────────────────────────────────
# 文档示例 ↔ 图标库：文档代码块里的 IconName.X 必须真的存在
# ──────────────────────────────────────────────

_DOC_ROOT = Path(__file__).resolve().parents[1] / "docs"
_DOC_PY_BLOCK = re.compile(r"^```python[^\n]*\n(.*?)^```", re.S | re.M)
#: 全仓唯一一份声明「可以直接抄」的范例（判据见该文档首段）—— 对它比对别处严。
_CANON_EXAMPLE = "architecture/components/组件契约规范-完整例子.md"


def _iter_doc_python_blocks(root: Path) -> list[tuple[str, int, str]]:
    """`root` 下所有 ```python 代码块 → ``[(相对路径, 围栏起始行号, 代码正文)]``。"""
    out: list[tuple[str, int, str]] = []
    for md in sorted(root.rglob("*.md")):
        text = md.read_text(encoding="utf-8")
        for m in _DOC_PY_BLOCK.finditer(text):
            out.append(
                (
                    md.relative_to(root).as_posix(),
                    text[: m.start()].count("\n") + 1,
                    m.group(1),
                )
            )
    return out


def _undefined_icon_names(code: str) -> list[str]:
    """代码块里引用了、但 `IconName` 没有的名字（走 AST，不走子串）。

    必须按 AST：`IconName.X` 这个词形在本仓**大量**出现在 docstring 与表格说明里
    （讲 D19 命名规范的地方必然会写），子串匹配会把「讲规则的那句话」判成违规。
    代码块内才是会被抄走执行的部分 —— 只有它错了才是真的错。

    **解析不了的块直接返回空并跳过**：本仓确有一批**示意性**代码块不是合法 Python
    （实测 3 处：`录屏通知设计-rules处置.md:109/180`、`主框架架构-工程与门禁.md:17`，
    都是省略式伪码）。那类块由 `test_canonical_example_doc_stays_runnable` 在**唯一
    一份声明「可以直接抄」的范例**上单独管，不在这里一刀切 —— 一刀切会把存量伪码
    全判红，而它们本来就是示意。
    """
    from barkit.contribution.specs import IconName

    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "IconName"
            and node.attr not in IconName.__members__
        ):
            found.append(node.attr)
    return found


def test_doc_examples_reference_real_icons():
    """文档代码块里的 `IconName.X` 必须能在枚举里解析。

    否则那段示例**抄下来就是 AttributeError**，而示例文档的全部意义正是「可以直接抄」。
    当初《完整例子》引用的 `BATTERY` / `BATTERY_LOW` / `BOLT` 在枚举里一个都没有，
    这条缺口就是这么写进台账的。
    """
    blocks = _iter_doc_python_blocks(_DOC_ROOT)
    assert len(blocks) > 20, (
        f"只扫到 {len(blocks)} 个代码块 —— 围栏写法或目录变了，门禁已失效"
    )
    bad = {
        f"{rel}:{line}": names
        for rel, line, code in blocks
        if (names := _undefined_icon_names(code))
    }
    assert not bad, (
        "文档示例引用了不存在的图标（抄下来会 AttributeError）：\n"
        + "\n".join(f"  {k}: {', '.join(v)}" for k, v in bad.items())
        + "\n改法：要么把字形加进 contribution/specs.py::IconName 与 "
        "shell/design/icons.py::_GLYPHS（并按 D19 想清楚有没有真消费方），"
        "要么把示例改用已有图标。"
    )


def test_doc_icon_gate_selfcheck(tmp_path):
    """上面那条门禁的自证：不存在的图标必抓、存在的必放、**非代码块不误报**。"""
    good = tmp_path / "good.md"
    good.write_text(
        "```python\noption = StateOption(id='a', icon=IconName.TABLET_WARN)\n```\n"
        "正文里写 `IconName.NOT_A_REAL_ICON` 不算违规（它不在代码块里）。\n",
        encoding="utf-8",
    )
    assert _iter_doc_python_blocks(tmp_path) == [
        ("good.md", 1, "option = StateOption(id='a', icon=IconName.TABLET_WARN)\n")
    ]
    assert _undefined_icon_names(_iter_doc_python_blocks(tmp_path)[0][2]) == []

    bad_dir = tmp_path / "only_bad"
    bad_dir.mkdir()
    (bad_dir / "bad.md").write_text(
        "```python\nicon = IconName.BOLT  # 曾经的真实缺口：枚举里没有它\n```\n",
        encoding="utf-8",
    )
    bad_blocks = _iter_doc_python_blocks(bad_dir)
    assert [b[0] for b in bad_blocks] == ["bad.md"], "自证用例自己先要能定位到块"
    assert _undefined_icon_names(bad_blocks[0][2]) == ["BOLT"]

    # 解析不了的示意块不得把门禁搞崩（存量确实有，见函数 docstring）
    assert _undefined_icon_names("def f():\n") == []


def test_canonical_example_doc_stays_runnable():
    """《组件契约规范-完整例子》的代码块必须是**真代码**，不许是省略式伪码。

    它是全仓唯一一份声明「可以直接抄」的范例，所以对它比对别处严：`def f():`
    后面空着那种块抄下来跑不起来，而那正是它存在的意义。**别的文档不做这条检查**
    —— 那里确实有用省略号表达的示意块（实测 3 处：`录屏通知设计-rules处置.md:109/180`、
    `主框架架构-工程与门禁.md:17`），一刀切会造出假红。
    """
    blocks = [b for b in _iter_doc_python_blocks(_DOC_ROOT) if b[0] == _CANON_EXAMPLE]
    assert blocks, f"没扫到范例文档的代码块：{_CANON_EXAMPLE}（路径或围栏写法变了）"
    for rel, line, code in blocks:
        try:
            compile(code, rel, "exec")
        except SyntaxError as e:
            raise AssertionError(
                f"{rel}:{line} 的范例代码块不是可运行代码：{e.msg}（块内第 {e.lineno} 行）"
            ) from None


# ──────────────────────────────────────────────
# 图标渲染单点：除 ui_kit / design/icons 外，禁止直接调 icons.pixmap /
# icons.badge_pixmap —— 必须走 ui_kit 的四个统一出口（draw_glyph / glyph_pixmap /
# draw_badge_glyph / badge_glyph_pixmap），它们替你取 dpr 并用**逻辑**尺寸布局。
#
# 依据：2026-09-21 两次回归（缺角 + 尺寸错乱）都出自「直接调 icons.pixmap + 用
# pm.width() 布局 —— 而 dpr>1 时 width() 返回物理像素」。单点 + 门禁让这类错误
# 在物理上写不出来。
# ──────────────────────────────────────────────

# 豁免：统一出口本身（ui_kit 的四个 helper / tray_* 在此调 icons.pixmap）与定义处。
_ICON_RENDER_EXEMPT = {
    "shell/design/icons.py",
}


def _is_ui_kit(rel: str) -> bool:
    """``shell/ui_kit`` 这一族（相对 ``src/barkit``）：单文件或**拆成包**都算。

    出口自己调 ``icons.pixmap`` 是合法的 ⇒ 整族豁免。**别写成点名单个文件** ——
    ui_kit 已是一个包（2026-09-30 拆分），按文件名判会把包内子漏在豁免外（假红），
    或反过来把整族从扫描面里漏掉（假绿）。
    """
    return rel == "shell/ui_kit.py" or rel.startswith("shell/ui_kit/")


def _direct_icon_render_hits(text: str) -> list[str]:
    """AST 找出直接调 ``icons.pixmap(...)`` / ``icons.badge_pixmap(...)`` 的位置。

    按点号名匹配 ``icons.pixmap`` 及其限定形态（``design.icons.pixmap``），
    故 ``self._pixmap_item.pixmap()`` 之类的**方法调用不会误报**。
    """
    hits: list[str] = []
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func)
        if not name:
            continue
        if name in {"icons.pixmap", "icons.badge_pixmap"} or name.endswith(
            (".icons.pixmap", ".icons.badge_pixmap")
        ):
            hits.append(f"line {node.lineno}: {name}(")
    return sorted(hits)


def test_ui_no_direct_icon_render():
    """图标必须经 ui_kit 统一出口渲染，不得直接调 ``icons.pixmap/badge_pixmap``。"""
    violations: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel in _ICON_RENDER_EXEMPT or _is_ui_kit(rel):
            continue
        hits = _direct_icon_render_hits(path.read_text(encoding="utf-8"))
        if hits:
            violations[rel] = hits
    assert not violations, (
        "图标必须经 ui_kit.draw_glyph / glyph_pixmap / draw_badge_glyph / "
        "badge_glyph_pixmap 渲染（它们替你取 dpr 并用逻辑尺寸布局）；直接调 "
        "icons.pixmap/badge_pixmap 会绕过 dpr 与逻辑尺寸约定，导致图标发虚 / 错位。\n"
        + "\n".join(
            f"  src/barkit/{rel}: {', '.join(h)}" for rel, h in violations.items()
        )
    )

    # 双自证：违规必抓（两条）+ 合规不误报（含同名方法调用的反例）
    bad = (
        "from barkit.shell.design import icons\n"
        "pm = icons.pixmap(x, 15, c)\n"
        "q = icons.badge_pixmap(i, lv, 8, w)\n"
    )
    assert len(_direct_icon_render_hits(bad)) == 2, "违规样本必须被抓到两条"
    good = (
        "from barkit.shell import ui_kit\n"
        "pm = ui_kit.glyph_pixmap(self, x, 15, c)\n"
        "m = self._pixmap_item.pixmap()\n"  # 同名方法：不得误报
    )
    assert _direct_icon_render_hits(good) == [], "合规样本 / 同名方法不得误报"


# ──────────────────────────────────────────────
# UI 文案不许带 markdown 强调标记（`**x**`）—— QLabel 不解析 markdown
#
# 来历（2026-09-22）：作者写 `**不会**` 以为会加粗，用户看到的是字面星号。同一批在
# 6 处文案里写过（recognize / route_guard ×3 / hosts_guard / doctor），肉眼看不出来
# —— 它不报错、不崩，只是丑，故落成门禁。
#
# 判据落在**源码字符串**上（不是渲染出来的控件文本）：非 docstring 的字面量里出现
# `**` 即违规。这一点有意选得比「扫已构建页面上的 QLabel」更全：同一句话既可能进
# QLabel，也可能进日志 / 终端 / `StateReport.summary`，后两者根本没有控件可扫
# （doctor 的 `hint` 只打终端，`_UNKNOWN_TAIL` 只喂日志）。
#
# 强调怎么写取决于落点：
# - **不会被省略的纯 QLabel**（`ui_kit.note` / `FeedbackBar` / `setToolTip`）：
#   富文本 `<b>x</b>` —— QLabel 默认 AutoText，认它；
# - **会进省略标签 / 日志 / 终端 / 状态机 summary 的那一份**：什么标记都别写。
#   面板副标题是 `ElideLabel` 单行省略（`panel._StatusRowView.set_subtitle` 同一份文本
#   既上屏又当悬停提示），带标签的串按含标签的宽度算省略，截出来是半截标签；而
#   `summary` 同时喂日志，标签会原样进日志文件。
# ──────────────────────────────────────────────

# 豁免：整文件是 QSS 源串或模板源码，`**` 落在注释 / 生成物的 docstring 里，不落任何控件。
_COPY_MARK_EXEMPT = {
    "barkit/shell/styles.py": "QSS 源串（`**` 在 /* 注释 */ 里）",
    "barkit/scaffold.py": "模板源码 —— `**` 在生成物的 docstring 里，不是控件文案",
}


def _is_ui_kit_from_parent(rel: str) -> bool:
    """``shell/ui_kit`` 这一族（相对 ``src``）：单文件或**拆成包**都算（同上，别点名单个文件）。"""
    return rel == "barkit/shell/ui_kit.py" or rel.startswith("barkit/shell/ui_kit/")


def _markdown_marks_in_copy(text: str) -> list[str]:
    """AST 找出「非 docstring 的字符串字面量」里带 ``**`` 的位置。

    docstring 不算文案：它进不了任何控件，全仓 docstring 里 ``**`` 本就是正常写法。
    路径通配不算强调 —— 判据是串里有没有 ``**/`` 或 ``/**``，不是「有没有两颗星」，
    于是 ``glob("**/*.py")`` 不误报。
    """
    tree = ast.parse(text)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            docstrings.add(id(first.value))
    hits: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in docstrings or "**" not in node.value:
            continue
        if "**/" in node.value or "/**" in node.value:  # 路径通配，不是强调
            continue
        hits.append(f"line {node.lineno}: {node.value[:40]!r}")
    return sorted(hits)


def test_no_markdown_in_ui_copy():
    """UI 文案里不许出现 markdown 强调（``**x**``）—— QLabel 不解析 markdown，会显示字面星号。"""
    violations: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC.parent).as_posix()
        if rel in _COPY_MARK_EXEMPT or _is_ui_kit_from_parent(rel):
            continue
        hits = _markdown_marks_in_copy(path.read_text(encoding="utf-8"))
        if hits:
            violations[rel] = hits
    assert not violations, (
        "UI 文案里的 `**x**` 不会被渲染成粗体（QLabel 不解析 markdown），用户看到的是字面"
        "星号。不被省略的纯 QLabel（note / FeedbackBar / 悬停提示）请改写成 <b>x</b>；"
        "会进省略标签 / 日志 / 终端 / 状态机 summary 的那一份什么标记都别写。\n"
        + "\n".join(f"  {rel}: {', '.join(h)}" for rel, h in violations.items())
    )

    # 双自证：违规必抓 + 合规不误报
    bad = (
        "from barkit.shell.ui_kit import note\n"
        'note("本页改动在**下次打开识别窗**时生效")\n'
    )
    assert len(_markdown_marks_in_copy(bad)) == 1, "markdown 强调必须被抓到"
    good = (
        '"""模块 docstring 里 **这样** 写是正常的。"""\n'
        "from barkit.shell.ui_kit import note\n"
        'note("本页改动在<b>下次打开识别窗</b>时生效")\n'
        'glob("**/*.py")\n'
        'deploy("src/**")\n'
    )
    assert _markdown_marks_in_copy(good) == [], "富文本 / docstring / 路径通配不得误报"

    # 控制组：豁免必须仍然成立 —— 哪天那三处 `**` 没了，豁免就该删掉，别留成盲区
    still = {rel: "**" in (SRC.parent / rel).read_text(encoding="utf-8")
             for rel in _COPY_MARK_EXEMPT}
    assert all(still.values()), f"豁免已过期（文件里不再有 `**`），请从豁免表删掉：{still}"

    # ui_kit 那条是**按目录**豁免的 ⇒ 控制组也得落在目录上：包里得真有 `**`，
    # 且这一行顺带证明扫描面**进到了包内**（`rglob` 若不进子目录，这里会是空 ⇒ 假绿）
    kit = [p.relative_to(SRC.parent).as_posix()
           for p in sorted((SRC / "shell" / "ui_kit").rglob("*.py"))
           if "**" in p.read_text(encoding="utf-8")]
    assert kit, "ui_kit 的豁免已过期（包里不再有 `**`），请删掉 _is_ui_kit_from_parent 这条豁免"


# ---- §10 工程纪律：文字标签的 objectName 必须真有规则（2026-09-21）----
#
# 来历：`shell/daemon_ops.py` 那行守护警示写的是 `setObjectName("err-note")`，而全局 QSS
# **从来没有这条规则** —— 模块注释写「警示区」、调用方注释写「红字」，实现里丢了：实机上
# 它与旁边的说明文字一样是正文色，藏了整整一轮没人发现。id 拼错 / 只改了一半都长这样，
# 肉眼看不出来，故落成门禁。


def _attr_path(node: ast.AST) -> "str | None":
    """把 ``x`` / ``self._x`` 这类访问路径还原成字符串；其它形状返回 ``None``（不猜）。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _attr_path(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _label_objectname_sites(path: Path) -> list[tuple[int, str]]:
    """扫一个文件里「``*Label`` 实例上设的 objectName 字面量」，返回 ``(行号, id)``。

    两步 AST：① 收集绑给 ``*Label(...)`` 构造的名字（``x = QLabel(…)`` /
    ``self._x = ElideLabel(…)``）；② 找这些名字上的 ``setObjectName("<字面量>")``。
    动态 id（``setObjectName(f"pill_{lv}")``）与拼出来的名字**不猜** —— 猜错比漏判更糟。

    **面只收 ``*Label``**：QLabel 的 objectName 只有一个用途 ＝ 上样式。容器 / 滚动区那些 id
    （``status-list`` / ``pop-body`` / ``pop-head`` / ``status-scroll`` / ``breach`` …）是
    **测试定位锚点**，本就该没有规则，收进面内只会逼人给它们编一条假 QSS。
    代价如实登记：控件自身那条路（``self.setObjectName(…)``，如 ``HStatChip`` 的
    ``hstat``）不在面内，由评审守。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    labels: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        fn = node.value.func
        called = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
        if called is None or not called.endswith("Label"):
            continue
        for target in node.targets:
            name = _attr_path(target)
            if name:
                labels.add(name)
    sites: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "setObjectName"):
            continue
        if len(node.args) != 1:
            continue
        arg = node.args[0]
        if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value):
            continue
        if _attr_path(fn.value) in labels:
            sites.append((node.lineno, arg.value))
    return sites


def _label_objectname_scan_targets() -> list[Path]:
    """扫描面 ＝ ``shell/**`` ＋ ``plugins/**`` 的全部 .py（**零白名单**）。

    与几何门禁不同，这条不收窄到设置页：`daemon_ops.py`（装置的警示）与
    `panel.py`（面板的小字）都在面外就白搭 —— `err-note` 那个反例正长在 `daemon_ops.py`。
    """
    return _py_files("shell") + _py_files("plugins")


def _styled_objectname_ids() -> set[str]:
    """``build_stylesheet`` 两个主题产出里**当选择器用**的 ``#id``。

    只在**选择器位置**匹配：QSS 正文里的十六进制色（``color: #FFFFFF;``）与注释里的字样
    都不是选择器 —— 宽松匹配会把它们当成「有规则」而放过真违规（第一版探针就踩了这条）。
    """
    from barkit.shell import styles  # 延迟 import：本文件其余用例是纯 AST，不需要 Qt

    ids: set[str] = set()
    for theme in ("light", "dark"):
        qss = re.sub(r"/\*.*?\*/", "", styles.build_stylesheet(theme), flags=re.S)
        ids |= set(re.findall(r"#([A-Za-z_][A-Za-z0-9_-]*)(?=[\s,:{])", qss))
    return ids


def test_label_objectnames_are_styled_or_none():
    """给 QLabel 设的 objectName 必须在 `build_stylesheet` 里真有规则（现成的反例：`err-note`）。

    守的是「**唯一入口**」那一半：文字样式要么走 ``apply_text_role``（角色表），要么走全局
    QSS 的 id 规则 —— 设一个 QSS 里不存在的 id 等于什么都没设，而代码读起来却像设了。
    """
    styled = _styled_objectname_ids()
    sites: list[tuple[str, int, str]] = []
    for path in _label_objectname_scan_targets():
        rel = path.relative_to(SRC).as_posix()
        sites += [(rel, ln, oid) for ln, oid in _label_objectname_sites(path)]
    assert len(sites) >= 5, f"扫描面疑似失效：只扫到 {len(sites)} 处（预期至少 5）"
    bad = [(rel, ln, oid) for rel, ln, oid in sites if oid not in styled]
    assert not bad, (
        "这些 QLabel 设了 QSS 里不存在的 objectName（样式不会生效）：\n"
        + "\n".join(f"  src/barkit/{rel}:{ln}: {oid!r}" for rel, ln, oid in bad)
    )


def test_label_objectname_gate_detects_planted_violation(tmp_path):
    """自证：``*Label`` 上的死 id 必抓；容器锚点、控件自身、注释里的同名字样不得误报。"""
    probe = tmp_path / "probe.py"
    probe.write_text(
        'warn = QLabel("", None)\n'
        'warn.setObjectName("err-note")\n'
        'tag = ElideLabel("t")\n'
        '# tag.setObjectName("also-dead")  ← 注释里的不算\n'
        "box = QWidget()\n"
        'box.setObjectName("status-list")\n'
        'chip.setObjectName("hstat")\n',
        encoding="utf-8",
    )
    assert _label_objectname_sites(probe) == [(2, "err-note")], _label_objectname_sites(probe)


def test_button_height_gate_rejects_the_free_form_parameter(tmp_path):
    """按钮高度门禁的自证：``tool_button(height=…)`` 必抓，档位写法不得误报。

    生产代码那条由 `test_no_button_height_magic_numbers` 守，这条只证明**判据本身**有效
    （正则式的「有没有 height=」会被注释与字符串命中，故走 AST）。
    """
    probe = tmp_path / "probe.py"
    probe.write_text(
        "a = tool_button(\"x\", \"pill-ghost\", height=26, parent=p)\n"
        "b = tool_button(\"y\", \"primary\")\n"
        "c = tool_button(\"z\", \"pill-chip\", size=\"chip\", parent=p)\n"
        '# d = tool_button("w", height=30)  ← 注释里的不算\n'
        "e.setFixedHeight(26)\n",
        encoding="utf-8",
    )
    assert _button_height_magic_numbers(probe) == [
        '1: tool_button(height=…) —— 按钮高度只能取自档位，改传 size="action|inline|chip"'
    ], _button_height_magic_numbers(probe)


def _button_height_magic_numbers(path: Path) -> list[str]:
    """扫一个模块里 ``tool_button(..., height=<字面量>)`` 的位置。

    ``height`` 这个自由参数 2026-09-21 已从 ``ui_kit.tool_button`` 移除（改按 ``size``
    查 ``metrics.BUTTON_HEIGHTS``）；残留的调用点会在运行期直接 ``TypeError``，这条门禁
    负责在**静态**阶段就把落点指出来，别等真机点按钮才发现。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        called = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
        if called != "tool_button":
            continue
        for kw in node.keywords:
            if kw.arg == "height":
                hits.append(
                    f"{node.lineno}: tool_button(height=…) —— 按钮高度只能取自档位，"
                    '改传 size="action|inline|chip"'
                )
    return hits


def _button_height_scan_targets() -> list[Path]:
    """扫描面 ＝ ``shell/**`` ＋ ``plugins/**``（**零白名单**）。

    原先只有「设置页」那一小片被几何门禁看着（`plugins/**/settings.py` ＋
    `shell/settings.py`），而按钮密度最高的两块 —— `shell/panel/`（面板动作条；真身
    在 `actions.py` 与 `chrome.py`，`panel.py` 拆包后自己一枚按钮都不剩）与
    `shell/daemon_ops.py`（装置运维）—— 都在面外，正是它们用的档位与设置页不一致。
    """
    return _py_files("shell") + _py_files("plugins")


def test_no_button_height_magic_numbers():
    """按钮高度不得在调用点写数字：只能传 ``size=`` 档位（真源在 ``metrics``）。"""
    violations: dict[str, list[str]] = {}
    for path in _button_height_scan_targets():
        hits = _button_height_magic_numbers(path)
        if hits:
            violations[path.relative_to(SRC).as_posix()] = hits
    assert not violations, (
        "按钮高度写了魔法数（请改取 metrics.BUTTON_HEIGHTS 的档位）：\n"
        + "\n".join(
            f"  src/barkit/{p}\n" + "\n".join(f"      {h}" for h in hs)
            for p, hs in violations.items()
        )
    )


def test_button_height_scan_boundary_is_the_whole_ui_surface():
    """自证：扫描面覆盖 UI 的全部 .py，且**一个文件都不排除**。

    与几何门禁那条的区别（它只收设置页）：本条的失效场景是「面板 / 装置的档位偷偷分叉」，
    故边界就是 `shell/` 与 `plugins/` 两棵树 —— 设成具名常量、在这里断言它没被收窄。
    """
    targets = _button_height_scan_targets()
    # 点名**真身**（按钮密度最高那一段的落点）：panel.py 已拆成包，按
    # `p.name == "panel.py"` 判会落到 `shell/panel/panel.py`（那里面只剩常量 / 信号 /
    # 构造 / hideEvent，一枚按钮都没有）⇒ 断言从此恒真、门禁静默失效。
    assert any(p.parts[-2:] == ("panel", "actions.py") for p in targets), (
        "面板动作条必须在内（按钮密度最高；真身是 shell/panel/actions.py）"
    )
    assert any(p.parts[-2:] == ("panel", "chrome.py") for p in targets), (
        "面板头部按钮必须在内（shell/panel/chrome.py）"
    )
    assert any(p.name == "daemon_ops.py" for p in targets), "装置运维块必须在内"
    # 点名**真身**（按钮档位的定义处），不点旧文件名：ui_kit 已拆成包，
    # 按 `p.name == "ui_kit.py"` 判会在拆包当天静默失去意义（断言从此恒假）。
    assert any(p.parts[-2:] == ("ui_kit", "buttons.py") for p in targets), (
        "按钮原语自身必须在内（shell/ui_kit/buttons.py）"
    )
    assert len(targets) == len(_py_files("shell")) + len(_py_files("plugins")), (
        "扫描面比预期窄：有文件被排除了"
    )


# ---- §10 工程纪律：字号不得写死裸 px（2026-10-13，接 `fb9b6f8` 几何收口）----
#
# 来历：颜色已于批次 0 收口到 ``tokens.py``（门禁 ``test_no_hardcoded_colors``），几何已于
# ``fb9b6f8`` 收口到 ``metrics``（圆角 / 间距 / 高度 / 侧栏宽）。**字号是当时显式推迟的下一棒**：
# 全仓的 ``font-size: Npx`` 散在 ``styles.py`` 的全局 QSS、``base._TEXT_ROLES`` 的角色表、以及
# ``rows / primitives / feedback / fields`` 四处的内联样式里，改一档字号要逐处找。现在与几何同口径，
# 全部指到 ``metrics.FONT_*``（单一真源），本门禁守「不得再写裸 px」。
#
# 扫描面 ＝ 整个 UI 源码树 ``src/barkit/**``（**零白名单**，与按钮档位门禁一致）：宿主
# ``shell/`` 与插件 ``plugins/`` 共用同一套字号，判据同一条。``metrics.py`` 里定义的
# ``FONT_* = N`` 是**整数字面量**不是 ``font-size:`` 字符串，天然不命中，无需像颜色门禁那样
# 单独排除令牌定义处。

_FONT_SIZE_RE = re.compile(r"font-size:\s*[0-9]")


def _font_size_literals_in_strings(path: Path) -> list[str]:
    """扫一个源文件**字符串字面量**里的 ``font-size: Npx``，返回 ``行号: 片段``（空 = 合规）。

    **只看字符串字面量、且排除 docstring**（与颜色门禁同判据）：注释 / docstring 里到处是
    「原先写死 ``font-size:13px``」这类举例，全文扫描会把它们一起判红。真正要拦的是会被塞进
    QSS / 内联样式、原样出现在用户眼前的字面量。

    还要剥掉 QSS 里的 ``/* … */`` 注释：``styles.py`` 的按钮段注释举例里写着
    ``font-size: 13px``（说明「原先写死的那套」），那是说明性文字不是真要进样式的字面量，
    剥掉它才不会误伤（Python 的 ``#`` 注释本就被 AST 排除，不用管）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstring_constants(tree)
    found: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in skip:
            continue
        # 剥掉 CSS 注释块（QSS 写在 Python 多行字符串里）
        text = re.sub(r"/\*.*?\*/", "", node.value, flags=re.DOTALL)
        for m in _FONT_SIZE_RE.finditer(text):
            found.add((node.lineno, m.group(0)))
    return [f"{lineno}: {frag!r}" for lineno, frag in sorted(found)]


def test_no_font_size_magic_numbers():
    """字号不得写死裸 px：只能取 ``metrics.FONT_*`` 档（真源在 ``shell/design/metrics.py``）。"""
    violations: dict[str, list[str]] = {}
    for path in _py_files():
        hits = _font_size_literals_in_strings(path)
        if hits:
            violations[path.relative_to(SRC).as_posix()] = hits
    assert not violations, (
        "源码里出现硬编码字号（请改取 shell/design/metrics.py 的 FONT_* 档）：\n"
        + "\n".join(
            f"  src/barkit/{p}\n" + "\n".join(f"      {h}" for h in hs)
            for p, hs in violations.items()
        )
    )


def test_font_size_scan_boundary_is_the_whole_ui_surface():
    """自证：扫描面覆盖字号的所有落点，且**一个文件都不排除**。

    与按钮档位门禁同一条失效场景：字号偷偷分叉的地方正是样式组装层与那四处内联样式，
    故边界就是 ``src/barkit/**`` 整棵树 —— 设成具名常量、在这里断言它没被收窄。
    """
    targets = _py_files()
    rel = {p.relative_to(SRC).as_posix() for p in targets}
    for must in (
        "shell/styles.py",
        "shell/ui_kit/base.py",
        "shell/ui_kit/rows.py",
        "shell/ui_kit/primitives.py",
        "shell/ui_kit/feedback.py",
        "shell/ui_kit/fields.py",
        "shell/design/metrics.py",
    ):
        assert must in rel, f"字号扫描面必须覆盖 {must}"


# ---- §10 工程纪律：全局 QSS 几何不得写死裸 px（2026-10-13，接 `fb9b6f8` 几何收口）----
#
# 来历：颜色已于批次 0 收口到 ``tokens.py``（门禁 ``test_no_hardcoded_colors``），几何已于
# ``fb9b6f8`` 收口到 ``metrics``（圆角 / 间距 / 高度 / 侧栏宽，门禁 ``test_no_geometry_magic_numbers``
# 只扫设置页的 ``Card(radius=…)`` / ``setContentsMargins(…)``）。但**全局 QSS 字符串**里还散着
# 一圈几何裸数（按钮三档、药丸、文本编辑区、设置侧栏、输入清除钮、选项卡、图标芯片、HTML 内联
# margin）—— 那道门禁不扫字符串，于是成了漏网。现在与字号（``test_no_font_size_magic_numbers``）
# 同口径，全部指到 ``metrics`` 的几何常量，本门禁守「QSS / 内联样式 / HTML 内联 style 里不得再写
# 裸 px」。
#
# 扫描面 ＝ 整个 UI 源码树 ``src/barkit/**``（**零白名单**，与字号 / 按钮档位门禁一致）：宿主
# ``shell/`` 与插件 ``plugins/`` 共用同一套几何，判据同一条。``metrics.py`` 里定义的几何常量是
# **整数字面量**不是 ``border-radius:`` 字符串，天然不命中；它的 docstring 也不含几何字面量。

_GEOM_PROPS = ("border-radius", "padding", "margin", "min-height", "max-height", "spacing")
_QSS_PX_RE = re.compile(r"(\d+)px", re.IGNORECASE)


def _qss_geometry_magic_numbers(path: Path) -> list[str]:
    """扫一个源文件**字符串字面量**里的几何裸 ``Npx``，返回 ``行号: 片段``（空 = 合规）。

    **只看字符串字面量、且排除 docstring**（与字号 / 颜色门禁同判据）：注释 / docstring 里
    到处是「原先写死 ``padding: 6px``」这类举例，全文扫描会把它们一起判红。真正要拦的是会被塞进
    QSS / 内联样式 / HTML 内联 style、原样出现在用户眼前的字面量。

    还要剥掉 QSS 里的 ``/* … */`` 注释：``styles.py`` 的按钮段注释举例里写着几何字面量，剥掉它
    才不会误伤（Python 的 ``#`` 注释本就被 AST 排除，不用管）。

    只认几何属性（``border-radius`` / ``padding`` / ``margin`` / ``min-height`` / ``max-height``
    / ``spacing``）：``border: 1px`` 与 ``border-bottom: 2px`` 这种「描边宽度」是通用惯用法、
    不是尺寸漂移的来源，不算；且改一处描边宽度不该触发门禁。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstring_constants(tree)
    found: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in skip:
            continue
        # 剥掉 CSS 注释块（QSS 写在 Python 多行字符串里）
        text = re.sub(r"/\*.*?\*/", "", node.value, flags=re.DOTALL)
        for decl in text.split(";"):
            head = decl.split(":", 1)
            if len(head) < 2:
                continue
            prop = head[0].strip().lower()
            if prop not in _GEOM_PROPS:
                continue
            for m in _QSS_PX_RE.finditer(head[1]):
                found.add((node.lineno, f"{prop}:{m.group(1)}px"))
    return [f"{lineno}: {frag}" for lineno, frag in sorted(found)]


def test_no_qss_geometry_magic_numbers():
    """全局 QSS / 内联样式 / HTML 内联 style 不得写死裸 px：只能取 ``metrics`` 的几何常量。"""
    violations: dict[str, list[str]] = {}
    for path in _py_files():
        hits = _qss_geometry_magic_numbers(path)
        if hits:
            violations[path.relative_to(SRC).as_posix()] = hits
    assert not violations, (
        "源码里出现硬编码几何尺寸（请改取 shell/design/metrics.py 的几何常量）：\n"
        + "\n".join(
            f"  {p}\n" + "\n".join(f"      {h}" for h in hs)
            for p, hs in violations.items()
        )
    )


def test_qss_geometry_scan_boundary_is_the_whole_ui_surface():
    """自证：扫描面覆盖几何的所有落点，且**一个文件都不排除**。

    与字号门禁同一条失效场景：几何偷偷分叉的地方正是样式组装层与那几处内联样式，故边界就是
    ``src/barkit/**`` 整棵树 —— 设成具名常量、在这里断言它没被收窄。
    """
    targets = _py_files()
    rel = {p.relative_to(SRC).as_posix() for p in targets}
    for must in (
        "shell/styles.py",
        "shell/ui_kit/primitives.py",
        "shell/ui_kit/fields.py",
        "plugins/ipad_tunnel/settings.py",
        "shell/design/metrics.py",
    ):
        assert must in rel, f"几何扫描面必须覆盖 {must}"


def test_qss_geometry_gate_detects_planted_violation(tmp_path):
    """自证：植入一条几何裸 px 必被抓（且 docstring 不误报）。"""
    bad = tmp_path / "widget.py"
    bad.write_text(
        '"""举例：原先 padding: 6px。"""\n'   # docstring 里的不算
        'self.setStyleSheet("padding: 6px 8px;")\n',
        encoding="utf-8",
    )
    hits = _qss_geometry_magic_numbers(bad)
    assert any("padding:6px" in h for h in hits), hits
    assert any("padding:8px" in h for h in hits), hits


# ---- §10 工程纪律：「标签 + 控件」行只有一套构造口（2026-09-21）----
#
# 来历：同一条「标签 + 控件」的行在仓里长出了**六套**写法 —— 五份 `_field_label` 私有拷贝
# （网络切换 / iPad 隧道 / hosts 托管 / 自动输入 / 录屏通知 各一份，其中**只有网络切换那份带
# `setWordWrap`**）+ hosts 管理窗的 `_field_row`（自带一套小字样式与 2px 间距）+ iPad 隧道
# 诊断窗的 `QFormLayout`（Qt 默认标签，不用角色表）。五份各自演化，后果是「长标签把窗撑宽、
# 分隔条拖不动」只在其中一页修过（2026-09-18 真机踩到）。
#
# 现在出口只有 `shell/ui_kit` 三个：`form_label`（**唯一的标签构造口**，默认带 setWordWrap）/
# `form_row`（并排）/ `form_row_stacked`（堆叠）。私有拷贝一律删掉，**不许复活** —— 名字层面
# 能守住的就守到名字层面：这几行代码读起来毫无异常，只有门禁认得出来。

#: 已删除的私有「标签 + 控件」行构造口（哪天被重新写出来就转红）。
_ROW_HELPER_NAMES = frozenset({"_field_label", "_field_row"})


def _row_helper_defs(path: Path) -> list[tuple[int, str]]:
    """扫一个模块里定义的那几个私有行构造口，返回 ``(行号, 名字)``。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        (node.lineno, node.name)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in _ROW_HELPER_NAMES
    ]


def _row_helper_scan_targets() -> list[Path]:
    """扫描面 ＝ `shell/**` ＋ `plugins/**` 全部 .py（**零白名单**）。

    `shell/` 必须在内：`ui_kit` 是**出口**，它自己要是又长出一份私有拷贝，插件页照抄一次
    就白收了。零白名单的代价如实登记：将来真有一个「非表单的行」需要独立构造口，得先改这条
    门禁说明理由，不能就地把它加进白名单绕过。
    """
    return _py_files("shell") + _py_files("plugins")


def test_no_private_form_row_helpers():
    """`plugins/**` / `shell/**` 不许再定义私有的「标签 + 控件」行构造口。"""
    found: dict[str, list[str]] = {}
    for path in _row_helper_scan_targets():
        defs = _row_helper_defs(path)
        if defs:
            found[path.relative_to(SRC).as_posix()] = [
                f"{ln}: def {name}(…)" for ln, name in defs
            ]
    assert not found, (
        "又出现了私有的「标签 + 控件」行构造口 —— 它们本该走 shell/ui_kit 的 "
        "form_label / form_row / form_row_stacked：\n"
        + "\n".join(
            f"  src/barkit/{p}\n" + "\n".join(f"      {h}" for h in hs)
            for p, hs in found.items()
        )
    )


def test_no_private_form_row_helpers_selfcheck(tmp_path):
    """自证：植入一份私有拷贝 ⇒ 点名到行；`form_label` 这类正规口不得误报。"""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "def _field_label(text, parent):\n"
        "    lbl = QLabel(text, parent)\n"
        "    return lbl\n"
        "\n"
        "\n"
        "class Editor:\n"
        "    @staticmethod\n"
        "    def _field_row(text, widget, parent):\n"
        "        return form_row_stacked(text, widget, parent)\n"
        "\n"
        "\n"
        "def form_label(text, parent=None):  # 正规口：同名方法不得误报\n"
        "    return None\n"
        "\n"
        "\n"
        "def _field_labels(text):  # 前缀相近的名字不是它\n"
        "    return None\n",
        encoding="utf-8",
    )
    assert _row_helper_defs(probe) == [(1, "_field_label"), (8, "_field_row")], (
        _row_helper_defs(probe)
    )


def test_form_label_is_the_only_label_constructor():
    """`form_label` 全仓**只有一处定义**，且在 `ui_kit`（「唯一入口」的另一半）。

    只查定义处不查调用点：调用点很多、也不该少；能漂的是「有人又抄了一份」，那必然多出一处
    ``def form_label``。同名方法 / 属性（``self.form_label``）不计入 —— 判的是模块级定义。
    """
    sites: list[str] = []
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:  # 只看模块级：类里同名的方法不是这个口
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "form_label":
                    sites.append(path.relative_to(SRC).as_posix())
    # 「面不多 / 面不少」分两条报（合成一条 `==` 时，报文会把「有人又抄了一份」说成
    # 「ui_kit 里那份不见了」，与实情正相反 —— 本仓门禁扩面的既有纪律）。
    # 判据落在「是不是 ui_kit 这一族」，不落在具体文件名 —— ui_kit 已拆成包，
    # 定义处在包内哪个子模块是**可搬的**（搬了不该红，抄到包外才该红）。
    extra = [s for s in sites if not s.startswith("shell/ui_kit/")]
    assert extra == [], f"form_label 在 ui_kit 之外还有定义（应只有 ui_kit 里那一处）：{extra}"
    assert len(sites) == 1, f"form_label 在 ui_kit 里不是唯一一处定义：{sites}"





# ── 样式表必须覆盖 ui_kit 里的每一种输入格替身 ─────────────────────────
#: ui_kit 里输入格替身继承的原生类 —— styles.build_stylesheet 的「输入框 / 文本域」
#: 那一行必须逐个列到。类型选择器命中该类**及其子类**（2026-09-30 实测：``QSpinBox``
#: 覆盖 ``NumberField``），所以列的是**原生基类**，要补的是还没出现过的那一个
#: （``QDoubleSpinBox`` 是 ``QSpinBox`` 的兄弟，不在它的子树里）。
#:
#: 为什么这条要立成门禁（2026-09-27 现场）：询问浮层里那个原生时间格焦点边框不变色，
#: 根因就是「它这一类没进样式表」⇒ 保持 macOS 原生外观；同一天新增 DecimalField 时
#: 若忘了补 QDoubleSpinBox，就是同一个坑原地再来一遍。
_STYLED_INPUT_BASES = ("QLineEdit", "QSpinBox", "QDoubleSpinBox", "QPlainTextEdit")

#: ``ui_kit`` 已是一个**包**（2026-09-30 拆分）⇒ 扫描面是它的目录，不是单个 .py。
_UI_KIT_DIR = SRC / "shell" / "ui_kit"


def _ui_kit_input_subclasses(path: Path) -> "dict[str, str]":
    """扫**一个文件**：``{替身类名: 它直接继承的原生类名}``（只收上面那一族的）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: "dict[str, str]" = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            if isinstance(base, ast.Name):
                name = base.id
            elif isinstance(base, ast.Attribute):
                name = base.attr
            else:
                continue
            if name in _STYLED_INPUT_BASES:
                out[node.name] = name
    return out


def _css_without_comments(css: str) -> str:
    """剥掉 QSS 注释。

    **这一步在承重**：本仓的注释就在讲这些类名（「``QLineEdit`` / ``QSpinBox`` /
    ``QDoubleSpinBox``」那句），不剥的话「选择器里漏了某个类名」照样命中注释 ⇒ 假绿。
    """
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _all_ui_kit_input_subclasses() -> "dict[str, str]":
    """扫整个 ``ui_kit`` 包：把包内每个子模块的替身合成一份。

    判据里带一条「包目录必须存在」—— 哪天 ui_kit 又变回单文件，这条会红而不是
    悄悄扫不到任何东西（`pairs` 为空时下一条也有断言，但那时报的是「扫描面写错了」，
    不如这里直接点名）。
    """
    assert _UI_KIT_DIR.is_dir(), f"ui_kit 不是包目录了：{_UI_KIT_DIR}"
    out: "dict[str, str]" = {}
    for path in sorted(_UI_KIT_DIR.rglob("*.py")):
        out.update(_ui_kit_input_subclasses(path))
    return out


def _unstyled_input_bases(pairs: "dict[str, str]", css: str) -> "dict[str, str]":
    """挑出「原生基类没进样式表」的替身。

    按 **词** 匹配（不用子串），且**不认伪类**：``QDoubleSpinBox:focus`` 只是聚焦那一条，
    普通态照样没有底色 / 描边 —— 单看「这个名字出现过」就放行，等于把「只补了半个样式」
    漏过去。所以要求类名后面既不是字母数字、也不是 ``:``。
    """
    return {
        cls: base
        for cls, base in pairs.items()
        if not re.search(rf"(?<![\w]){re.escape(base)}(?![\w:])", css)
    }


def test_every_ui_kit_input_subclass_is_styled():
    """``ui_kit`` 里每个输入格替身，它的**原生基类**都得在样式表里出现。

    样式表按**原生基类名**列族（``QLineEdit, QSpinBox, QDoubleSpinBox, QPlainTextEdit``），
    所以新增替身时要补的是**它继承的那个原生类**，不是替身自己的名字。
    """
    from barkit.shell import styles

    pairs = _all_ui_kit_input_subclasses()
    assert pairs, "没扫到任何输入格替身 —— 扫描面写错了，门禁等于失效"
    missing = _unstyled_input_bases(pairs, _css_without_comments(styles.build_stylesheet("dark")))
    assert not missing, (
        "这些输入格替身的原生基类没进样式表（它不在已列那些类的子树里 ⇒ 一条规则都"
        f"匹配不到，退回 macOS 原生渲染、聚焦时边框也不变色）：{missing} —— 补进 "
        "build_stylesheet 的输入格一族"
    )


def test_gate_input_subclass_scan_selfcheck(tmp_path):
    """上面那条门禁的双自证：漏一个原生基类必抓、**注释里写着名字不算覆盖**。"""
    kit = tmp_path / "kit.py"
    kit.write_text(
        "from PySide6.QtWidgets import QDoubleSpinBox, QSpinBox\n"
        "class NumberField(QSpinBox):\n"
        "    pass\n"
        "class DecimalField(QDoubleSpinBox):\n"
        "    pass\n",
        encoding="utf-8",
    )
    pairs = _ui_kit_input_subclasses(kit)
    assert pairs == {"NumberField": "QSpinBox", "DecimalField": "QDoubleSpinBox"}, pairs

    # ① 选择器里只列了 QSpinBox（漏 QDoubleSpinBox）⇒ 必红，且只点这一条
    half = "QLineEdit, QSpinBox { background: red; }"
    assert _unstyled_input_bases(pairs, _css_without_comments(half)) == {
        "DecimalField": "QDoubleSpinBox"
    }

    # ② 只写**替身自己的类名**不算合规：门禁判的是「原生基类有没有进那一族选择器」——
    #    本仓的写法是一族一次性列全，替身类名逐个列会随替身增减而漂。
    by_subclass = "NumberField, DecimalField { background: red; }"
    assert _unstyled_input_bases(pairs, _css_without_comments(by_subclass)) == {
        "NumberField": "QSpinBox",
        "DecimalField": "QDoubleSpinBox",
    }

    # ③ **注释里写着**类名不算覆盖（不剥注释就会假绿），而剥注释这一步真的在做功
    commented = "/* 输入格：QSpinBox / QDoubleSpinBox */\nQLineEdit, QSpinBox { color: red; }"
    assert re.search(r"(?<![\w])QDoubleSpinBox(?![\w])", commented), "控制组前提：注释里确实有"
    assert not re.search(r"(?<![\w])QDoubleSpinBox(?![\w])", _css_without_comments(commented))
    assert _unstyled_input_bases(pairs, _css_without_comments(commented)) == {
        "DecimalField": "QDoubleSpinBox"
    }

    # ④ **只写在伪类里不算覆盖**：`:focus` 那半条挡不住普通态（没有底色 / 描边）
    focus_only = "QLineEdit, QSpinBox, QDoubleSpinBox:focus { border: 1px solid red; }"
    assert _unstyled_input_bases(pairs, _css_without_comments(focus_only)) == {
        "DecimalField": "QDoubleSpinBox"
    }

    # ⑤ 补全后必绿（合规不误报）
    ok = "QLineEdit, QSpinBox, QDoubleSpinBox, QPlainTextEdit { color: red; }"
    assert _unstyled_input_bases(pairs, _css_without_comments(ok)) == {}


_NATIVE_INPUT_BASES = _STYLED_INPUT_BASES + (
    "QTimeEdit",
    "QDateEdit",
    "QDateTimeEdit",
    "QTextEdit",
)


def _raw_native_inputs_used(src_dir: Path) -> "dict[str, str]":
    """扫全仓 ``src``：**直接 new** 的原生输入格 ``{类名: 首个文件}``（ast 认调用，注释不算）。

    ``ui_kit`` 的门禁只看替身；这一条兜的是**绕开 ui_kit 直接用原生类**的那条路
    （2026-09-28 撞到：询问浮层的 ``QTimeEdit`` 是全仓唯一一处，漏列样式表 ⇒ 长得
    和别处的输入框不一样、聚焦也不变色，用户一眼就看出「这不是一个系统的」）。
    """
    out: "dict[str, str]" = {}
    for path in sorted(src_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in _NATIVE_INPUT_BASES
            ):
                out.setdefault(node.func.id, str(path.relative_to(src_dir)))
    return out


def test_every_native_input_used_in_src_is_styled():
    """全仓**直接 new** 的每个原生输入格类，都得在样式表的输入格一族里出现。

    与替身那条门禁同一口径（含「注释里写了不算」「只在伪类里不算」），只是扫描面从
    ``ui_kit`` 的类定义扩到**全仓的实例化** —— 替身 / 直接用原生类是同一种丑的两条路。
    """
    from barkit.shell import styles

    used = _raw_native_inputs_used(SRC)
    assert used, "没扫到任何直接实例化的原生输入格 —— 扫描面写错了，门禁等于失效"
    missing = _unstyled_input_bases(
        {name: name for name in used}, _css_without_comments(styles.build_stylesheet("dark"))
    )
    where = "、".join(f"{name}（{file}）" for name, file in missing.items())
    assert not missing, (
        f"这些输入格是绕开 ui_kit 直接 new 的，但没进样式表的输入格一族（会退回 macOS "
        f"原生渲染、聚焦时边框也不变色）：{where} —— 补进 build_stylesheet 的输入格一族"
    )

    # 自证：把这一族里某一个类名从样式表里抠掉 ⇒ 必红，且只点被抠的那个
    css = _css_without_comments(styles.build_stylesheet("dark"))
    m = re.search(r"(?<![\w])QTimeEdit(?![\w:])", css)
    assert m, "控制组前提：QTimeEdit 确实在样式表里（否则这条自证是空转）"
    stripped = css[: m.start()] + css[m.end():]
    assert _unstyled_input_bases({"QTimeEdit": "QTimeEdit"}, stripped) == {"QTimeEdit": "QTimeEdit"}


# ---------------------------------------------------------------------------
# 打桩点写法门禁（2026-10-03）
# ---------------------------------------------------------------------------

def _dotted_string_target_hits(root: Path) -> list[str]:
    """扫出「``target`` 写成了带点的字符串」的三参 monkeypatch 调用。

    ``monkeypatch.setattr`` 有两种写法，语义差得远：

    - ``setattr("pkg.mod.attr", value)`` —— 点号路径，pytest 解析字符串、落点是真对象；
    - ``setattr(obj, "attr", value)`` —— 对象 ＋ 属性名。

    把两者混写成 ``setattr("pkg.mod", "attr", value)`` 时，``target`` **就是一个字符串对象**，
    pytest 不会去 import 它 —— 于是 patch 落在 ``str`` 实例上，很可能 getattr 就炸
    ``AttributeError: 'pkg.mod' has no attribute 'attr'``。看着像「生产代码里没这个属性」，
    其实是打桩点自己写错；配 ``raising=False`` 更是彻底无声（判据纹丝不动的「假绿」）。

    本门禁按 AST 收这类调用，.factory / 别名一概不认，只看该次调用的形状。
    """
    hits: list[str] = []
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 —— 门禁不该因为别人写坏一个文件就整体失效
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in ("setattr", "delattr"):
                continue
            receiver = node.func.value
            owner = getattr(receiver, "id", None) or getattr(receiver, "attr", None)
            if owner != "monkeypatch":
                continue
            args = node.args
            # 形参不全（缺 value）时交给 lint 管，这里不判
            if len(args) < 3:
                continue
            first = args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str) and "." in first.value:
                try:
                    text = ast.unparse(node)
                except Exception:  # noqa: BLE001
                    text = "<unparse failed>"
                hits.append(f"{path.relative_to(root)}:{node.lineno} {text[:120]}")
    return hits


def _compliant_dotted_patch_count(root: Path) -> int:
    """点号路径写法（``setattr("pkg.mod.attr", value)``）的调用条数。

    只做控制组用：证明上面那条门禁的扫描面确实覆盖到了同类调用。
    """
    total = 0
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "setattr":
                continue
            receiver = node.func.value
            owner = getattr(receiver, "id", None) or getattr(receiver, "attr", None)
            if owner != "monkeypatch" or len(node.args) != 2:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str) and "." in first.value:
                total += 1
    return total


def test_monkeypatch_string_targets_are_single_dotted_path():
    """三参 monkeypatch 的 ``target`` 不许是「带点的字符串」。

    这是拆包最典型的踩法：模块变成包之后，打桩点要从 ``macos_window`` 改指真身子模块，
    顺手就写成了三参形式 —— 一行安静地把整条判据变成空操作。
    """
    tests_root = Path(__file__).resolve().parents[1]
    hits = _dotted_string_target_hits(tests_root)

    # 控制组：仓里必须真有「合规的两参点号写法」，否则扫描面可能是空的 ——
    # 扫描面写错时这条自己转红，不许假装通过。
    assert _compliant_dotted_patch_count(tests_root), (
        "一个点号路径打桩都没扫到 —— 扫描面写错了，门禁等于失效"
    )

    assert not hits, (
        "这些 monkeypatch 把 target 写成了带点的字符串（pytest 不会 import 它 ⇒ patch 落在 "
        "str 上，判据纹丝不动）：\n  "
        + "\n  ".join(hits)
        + "\n改法：写成一个完整点号路径 setattr(\"pkg.mod.attr\", value)，或把模块对象 import 进来再传。"
    )


def test_monkeypatch_target_gate_detects_planted_violation(tmp_path):
    """违规必抓：种一段三参字符串 target ⇒ 门禁必须点名它。"""
    (tmp_path / "bad.py").write_text(
        "def test_x(monkeypatch):\n"
        '    monkeypatch.setattr("barkit.shell.panel.place", "anchor_of", lambda: None)\n',
        encoding="utf-8",
    )
    hits = _dotted_string_target_hits(tmp_path)
    assert len(hits) == 1 and "bad.py:2" in hits[0], hits


def test_monkeypatch_target_gate_passes_on_compliant_sample(tmp_path):
    """合规不误报：点号路径 / 对象传参两种正确写法都不许点名。"""
    (tmp_path / "ok.py").write_text(
        "import barkit.shell.panel.place as place\n"
        "def test_x(monkeypatch):\n"
        '    monkeypatch.setattr("barkit.shell.panel.place.anchor_of", lambda: None)\n'
        "    monkeypatch.setattr(place, \"anchor_of\", lambda: None)\n",
        encoding="utf-8",
    )
    assert _dotted_string_target_hits(tmp_path) == []
