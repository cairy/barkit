"""core/schema：FormSchema 数据契约测试（批次 0）。

覆盖：默认值合成（含嵌套 DICT 段深拷贝）、类型校验（含 bool≠number 陷阱）、
CHOICE、DICT 宽松校验、必填、保留键透传、版本迁移（逐级执行 / 缺步报错 /
全新安装不迁移）。
"""

from __future__ import annotations

import pytest

from barkit.core.schema import (
    SCHEMA_VERSION_KEY,
    FieldSpec,
    FieldType,
    FormSchema,
    SchemaError,
    SchemaMigrationError,
)


def _schema(**kw) -> FormSchema:
    fields = kw.pop("fields", ())
    return FormSchema(plugin_id="demo", fields=fields, **kw)


# ──────────────────────────────────────────────
# 默认值合成
# ──────────────────────────────────────────────

def test_defaults_synth_and_skip_none():
    schema = _schema(fields=(
        FieldSpec("name", "名称", default="abc"),
        FieldSpec("count", "数量", FieldType.NUMBER, default=3),
        FieldSpec("secret", "密钥", FieldType.SECRET),  # 必填无默认 → 不产出
    ))
    assert schema.defaults() == {"name": "abc", "count": 3}


def test_defaults_deep_copied():
    schema = _schema(fields=(FieldSpec("items", "列表", default=[1, 2]),))
    a, b = schema.defaults(), schema.defaults()
    a["items"].append(3)
    assert b["items"] == [1, 2]


# ──────────────────────────────────────────────
# 校验
# ──────────────────────────────────────────────

def test_validate_number_rejects_bool():
    """bool 是 int 子类，必须显式排除（True 混成 1 的经典坑）。"""
    schema = _schema(fields=(FieldSpec("poll", "轮询", FieldType.NUMBER),))
    _, errors = schema.validate({"poll": True})
    assert errors and "poll" in errors[0]
    assert schema.validate({"poll": 5})[1] == []


def test_validate_bool_strict():
    schema = _schema(fields=(FieldSpec("on", "开关", FieldType.BOOL),))
    assert schema.validate({"on": False})[1] == []
    _, errors = schema.validate({"on": 1})
    assert errors and "on" in errors[0]


def test_validate_choice():
    schema = _schema(fields=(
        FieldSpec("mode", "模式", FieldType.CHOICE, choices=("auto", "manual")),
    ))
    assert schema.validate({"mode": "auto"})[1] == []
    _, errors = schema.validate({"mode": "magic"})
    assert errors and "mode" in errors[0]


def test_validate_choice_without_choices_raises():
    with pytest.raises(SchemaError):
        _schema(fields=(FieldSpec("mode", "模式", FieldType.CHOICE),)).validate(
            {"mode": "auto"}
        )


def test_validate_required_and_unknown_passthrough():
    schema = _schema(fields=(FieldSpec("ssid", "SSID", required=True),))
    # 缺失必填 → 报错；未知/框架键原样透传不拒绝（schema 只管表单字段）
    _, errors = schema.validate({"enabled": True})
    assert errors == ["ssid 不能为空"]
    clean, errors = schema.validate({"ssid": "X", "enabled": True, "_schema_version": 1})
    assert errors == []
    assert clean["enabled"] is True and clean["_schema_version"] == 1


def test_validate_secret_must_be_str():
    schema = _schema(fields=(FieldSpec("key", "密钥", FieldType.SECRET),))
    _, errors = schema.validate({"key": 12345})
    assert errors and "key" in errors[0]


def test_validate_dict_accepts_mapping_rejects_scalar():
    """DICT 是**嵌套对象段**（如 ``wifi``），只做宽松校验：必须是映射，不是标量。

    故意不校验子键 —— 子键取值由消费方（root 侧 / 渲染器）管，schema 只保证
    「这一段是个对象」。守的是搬迁后的那条路：段写成了标量（如 ``wifi: off``）
    要在校验里炸出来，而不是让消费方 ``cfg["wifi"]["on_activate"]`` 时才 TypeError。
    """
    schema = _schema(fields=(FieldSpec("wifi", "Wi-Fi 切换", FieldType.DICT,
                                      default={"on_activate": "iPad-Share"}),))
    assert schema.validate({"wifi": {"on_activate": "X"}})[1] == []
    for bad in (["on_activate"], "off", 1, True):
        _, errors = schema.validate({"wifi": bad})
        assert errors and "wifi" in errors[0], f"{bad!r} 应被拒"


def test_defaults_nested_dict_is_deep_copied():
    """嵌套对象段的默认值要**深拷贝**：浅拷贝会让两次合成共享同一子 dict。

    LIST 那条（``test_defaults_deep_copied``）只到第一层；DICT 的子键在第二层，
    浅拷贝刚好漏掉 —— 一处改动会渗进另一处（重载 / 回滚路径上最阴的一类 bug）。
    """
    schema = _schema(fields=(FieldSpec("wifi", "Wi-Fi 切换", FieldType.DICT,
                                      default={"on_activate": "A"}),))
    a, b = schema.defaults(), schema.defaults()
    a["wifi"]["on_activate"] = "MUTATED"
    assert b["wifi"]["on_activate"] == "A"


# ──────────────────────────────────────────────
# 版本迁移
# ──────────────────────────────────────────────

def test_migrate_runs_stepwise_and_stamps_version():
    def _v1_to_v2(d: dict) -> dict:
        # 示例迁移：title 改名 name，其余键保留
        return {**{k: v for k, v in d.items() if k != "title"},
                "name": d.get("title", "")}

    schema = _schema(
        version=2,
        fields=(FieldSpec("name", "名称"),),
        migrations={1: _v1_to_v2},
    )
    out = schema.migrate({SCHEMA_VERSION_KEY: 1, "title": "旧"})
    assert out == {SCHEMA_VERSION_KEY: 2, "name": "旧"}


def test_migrate_missing_step_raises():
    schema = _schema(version=3, migrations={1: lambda d: d})  # 缺 v2→v3
    with pytest.raises(SchemaMigrationError):
        schema.migrate({SCHEMA_VERSION_KEY: 1})


def test_migrate_fresh_install_passthrough():
    """无 _schema_version 记录 = 全新安装：不迁移（默认值由合成层提供）。"""
    schema = _schema(version=2, migrations={1: lambda d: d})
    data = {"name": "x"}
    assert schema.migrate(data) == data


def test_migrate_noop_when_current():
    schema = _schema(version=2, migrations={})
    out = schema.migrate({SCHEMA_VERSION_KEY: 2, "a": 1})
    assert out[SCHEMA_VERSION_KEY] == 2
