"""E1 loader 测试：外部插件 manifest 门禁 + 与内部插件汇合（ROADMAP P1）。

覆盖：
- 合法外部插件被发现并产出 Plugin 类（manifest 强制对齐 id）
- api_version 超界被拒（可读原因，不崩溃）
- manifest 缺 id 被拒
- entry 模块缺失被拒
- 外部插件根目录不存在 → 返回空（开发机无外部插件属正常）
- 模板插件贡献（声明点方法）经 ContributionRegistry 正常出现
- Registry.discover 聚合外部插件（固定默认路径 + config 覆盖）
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from barkit.core.config import Config
from barkit.core.contributions import ContributionRegistry
from barkit.core.loader import discover_external
from barkit.core.plugin import Plugin
from barkit.core.registry import Registry

EXAMPLE_TEMPLATE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "external-plugin-template"


def _copy_template(dest: Path) -> Path:
    target = dest / "demo_ext"
    shutil.copytree(EXAMPLE_TEMPLATE, target)
    return target


def test_discover_external_accepts_valid(tmp_path):
    _copy_template(tmp_path)
    found, rejected = discover_external(tmp_path)
    assert "demo_ext" in found
    assert issubclass(found["demo_ext"], Plugin)
    # manifest 为权威身份：loader 强制对齐 id / order
    assert found["demo_ext"].id == "demo_ext"
    assert found["demo_ext"].order == 95
    assert rejected == []


def test_discover_external_rejects_api_version_too_new(tmp_path):
    d = _copy_template(tmp_path)
    manifest = d / "manifest.yaml"
    # 与框架支持的具体版本解耦：把清单里的版本替换成任意「过新」值
    text = re.sub(r"api_version: \d+", "api_version: 99", manifest.read_text(encoding="utf-8"))
    assert "api_version: 99" in text, "替换未生效会让本用例退化成空跑"
    manifest.write_text(text, encoding="utf-8")
    found, rejected = discover_external(tmp_path)
    assert "demo_ext" not in found
    reason = dict(rejected).get("demo_ext", "")
    assert reason != ""                       # 被拒
    assert "Barkit" in reason                 # 可读，非崩溃


def test_discover_external_rejects_missing_id(tmp_path):
    d = _copy_template(tmp_path)
    manifest = d / "manifest.yaml"
    text = manifest.read_text(encoding="utf-8")
    text = "\n".join(l for l in text.splitlines() if not l.startswith("id:"))
    manifest.write_text(text, encoding="utf-8")
    found, rejected = discover_external(tmp_path)
    assert "demo_ext" not in found
    assert rejected                          # 因缺 id 被拒


def test_discover_external_rejects_bad_entry(tmp_path):
    d = _copy_template(tmp_path)
    (d / "plugin.py").unlink()               # entry 模块缺失
    found, rejected = discover_external(tmp_path)
    assert "demo_ext" not in found
    assert dict(rejected).get("demo_ext", "").startswith("entry")


def test_discover_external_missing_dir_is_empty(tmp_path):
    assert discover_external(tmp_path / "nope") == ({}, [])


def test_template_contributions_via_registry(tmp_path, qtbot):
    _copy_template(tmp_path)
    found, _ = discover_external(tmp_path)
    cls = found["demo_ext"]
    cfg = Config(user_path=tmp_path / "config.yaml")
    from barkit.core.plugin import PluginContext

    plugin = cls(PluginContext(config=cfg))
    assert plugin.api_version == 3, "外部插件模板应声明现行契约版本"
    assert plugin.row_specs() and plugin.state_specs()
    # 经 ContributionRegistry 聚合：外部插件汇入与内部插件同一消费路径
    reg = ContributionRegistry([plugin])
    assert any(c.plugin_id == "demo_ext" for c in reg.states())
    assert len(reg.status_rows()) == 1


def test_registry_aggregates_external(tmp_path):
    _copy_template(tmp_path)
    cfg = Config(user_path=tmp_path / "config.yaml")
    cfg.save_user({"external_plugins": {"path": str(tmp_path)}})  # 覆盖扫描路径
    reg = Registry(cfg, events=None)
    found = reg.discover()
    assert "demo_ext" in found
    # 内置插件仍正常发现（无双轨）
    assert "recognize" in found


def test_discover_external_registers_plugin_version(tmp_path):
    """清单里的 version → ``Plugin.version``（决议 D5）：**只登记，不对账**。

    版本号不进任何门禁，故这里既不要求它存在、也不校验格式 —— 断言的是
    「清单写了什么，类上就读到什么」。
    """
    d = _copy_template(tmp_path)
    text = (d / "manifest.yaml").read_text(encoding="utf-8")
    declared = re.search(r'^version:\s*"?([^"\n]+)"?\s*$', text, re.M)
    assert declared, "模板清单必须示范 version 字段（否则这条用例是空跑）"

    found, _ = discover_external(tmp_path)

    assert found["demo_ext"].version == declared.group(1)


def test_discover_external_without_version_leaves_it_unset(tmp_path):
    """老清单没有 version → 空串（＝未标注）：**不拒载、不编造版本号**。"""
    d = _copy_template(tmp_path)
    manifest = d / "manifest.yaml"
    manifest.write_text(
        "\n".join(
            line for line in manifest.read_text(encoding="utf-8").splitlines()
            if not line.startswith("version:")
        ),
        encoding="utf-8",
    )

    found, rejected = discover_external(tmp_path)

    assert rejected == [] and "demo_ext" in found
    assert found["demo_ext"].version == ""


def test_refresh_discovery_picks_up_new_external_package(tmp_path):
    """重扫（安装 / 覆盖后调用）：新落盘的包进类表、来源标 external、陈旧来源被摘掉。

    走**真发现**（``Registry.discover``）而不是把它打桩：安装路径的失效方式恰恰是
    「目录里有了、内存里的类表还是旧的」，用桩验不出这一点。
    """
    ext = tmp_path / "ext"
    ext.mkdir()
    cfg = Config(user_path=tmp_path / "config.yaml")
    cfg.save_user({"external_plugins": {"path": str(ext)}})
    reg = Registry(cfg, events=None)
    reg._source = {"gone": "external"}                 # 上一版留下的陈旧来源
    assert "demo_ext" not in reg.discovered_classes    # 还没装

    _copy_template(ext)                                # 相当于安装入口把包落盘
    reg.refresh_discovery()

    assert "demo_ext" in reg.discovered_classes
    assert reg.source_of("demo_ext") == "external"
    assert "gone" not in reg._source                   # 陈旧来源不许留着答非所问


def test_discover_external_rejects_non_integer_api_version(tmp_path):
    """``api_version: "1.0"`` 这种非整数过去会让 ``int()`` 抛 ValueError 直接冒到
    AppShell 装配，整个 App 起不来。现在按「门禁」拒载而非崩溃。"""
    d = _copy_template(tmp_path)
    manifest = d / "manifest.yaml"
    text = re.sub(r"api_version: \d+", "api_version: \"1.0\"",
                  manifest.read_text(encoding="utf-8"))
    manifest.write_text(text, encoding="utf-8")
    found, rejected = discover_external(tmp_path)
    assert "demo_ext" not in found
    assert rejected and any("整数" in r for _, r in rejected)


def test_discover_external_rejects_id_traversal(tmp_path):
    """``id: ../../escape`` 会拼进插件根目录路径 —— 必须按门禁拒载。"""
    d = _copy_template(tmp_path)
    manifest = d / "manifest.yaml"
    text = manifest.read_text(encoding="utf-8").replace("id: demo_ext", "id: ../../escape")
    manifest.write_text(text, encoding="utf-8")
    found, rejected = discover_external(tmp_path)
    assert "demo_ext" not in found
    assert rejected and any("路径" in r or "穿越" in r for _, r in rejected)


def test_discover_external_rejects_entry_traversal(tmp_path):
    """``entry: ../../evil:Plugin`` 会让框架 import 插件目录外的任意 .py —— 必须拒。"""
    d = _copy_template(tmp_path)
    manifest = d / "manifest.yaml"
    text = manifest.read_text(encoding="utf-8").replace(
        "entry: plugin:Plugin", "entry: ../../evil:Plugin")
    manifest.write_text(text, encoding="utf-8")
    found, rejected = discover_external(tmp_path)
    assert "demo_ext" not in found
    assert rejected and any("entry" in r for _, r in rejected)
