"""core/registry + plugin 测试。"""

from barkit.core.config import Config
from barkit.core.registry import Registry


def _config(tmp_path, user_yaml: str = "{}\n") -> Config:
    user = tmp_path / "config.yaml"
    user.write_text(user_yaml, encoding="utf-8")
    return Config(user_path=user)


def test_discover_finds_builtin_plugins(tmp_path):
    reg = Registry(_config(tmp_path))
    found = reg.discover()
    for pid in ("recognize", "notify_guard", "route_guard", "autotype"):
        assert pid in found


def test_disabled_plugin_not_loaded(tmp_path):
    # route_guard 默认启用；显式禁用后不应加载
    reg = Registry(_config(tmp_path, "plugins:\n  route_guard:\n    enabled: false\n"))
    reg.load_all()
    assert "route_guard" not in reg.plugins


def test_enabled_plugin_loaded(tmp_path):
    reg = Registry(_config(tmp_path))
    reg.load_all()
    assert "recognize" in reg.plugins
