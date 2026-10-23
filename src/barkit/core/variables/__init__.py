"""core/variables：全局变量池（设计文档 §2 变量池模型 / §3 表达式引擎）。

把「值」抽成框架级的一等公民，任务只引用变量：一处定义、处处引用，问一次、算一次、
处处用。四类来源（``framework`` / ``component`` / ``ask`` / ``derived``）在
:class:`VariablePool` 里各有一个取法；「任务在什么条件下生效」走结构化规则
（:mod:`~barkit.core.variables.rules`），「派生变量怎么算」走表达式引擎
（:mod:`~barkit.core.variables.expr`）——**两者不混用**，是设计文档 §1 的分工。

包内按职责分开，本模块**全量再导出**（与 ``core/config`` 同款），调用方写
``from barkit.core.variables import VariablePool`` 即可。注意本仓那条口径：**包属性＝
导入时那份快照**，要 monkeypatch 某个名字请打**真身子模块**（``pool`` / ``expr`` …），
打在包上等于打进空气。

本包**不认识 Qt，也不认识 Config**：声明由调用方（装配层 / 设置页）从配置里读出来喂给
:meth:`VariablePool.load`，值由本包算。这样它可以在纯命令行下单测，也不给 core 的依赖
方向添一条反向边。
"""

from __future__ import annotations

from .expr import (
    DAY_S,
    ID_PATTERN,
    T_BOOL,
    T_DATE,
    T_DURATION,
    T_ENUM,
    T_NUMBER,
    T_STRING,
    T_TIME,
    T_WEEKDAY,
    TYPES,
    ExprError,
    Value,
    coerce,
    evaluate,
    evaluate_or_none,
    fmt_duration,
    fmt_time,
    referenced_variables,
    result_type,
    time_to_seconds,
    validate as validate_expr,
)
from .host import BEAT_INTERVAL_S, FRAMEWORK_OWNER, VariableHost, ask_key
from .pool import (
    ASK_BLOCKED,
    ASK_FILLED,
    ASK_READY,
    ASK_UNDECIDED,
    PLACEHOLDER_ASK_UNFILLED,
    PLACEHOLDER_COMPONENT_ABSENT,
    PLACEHOLDER_UNDEFINED,
    VariablePool,
    status_topic_of,
    text_variables,
)
from .rules import OPS, RuleCondition, matches, matches_all, parse_conditions, parse_one
from .spec import (
    AskTrigger,
    BUILTIN_IDS,
    DEFAULT_COMPONENT_TYPE,
    FIELDS,
    SOURCES,
    WHEN,
    VariableSpec,
    VariableSpecError,
    builtin_specs,
    check_section,
    parse_all,
    type_map,
)
from .store import KEEP_DAYS, STATE_FILENAME

__all__ = [
    "ASK_BLOCKED",
    "ASK_FILLED",
    "ASK_READY",
    "ASK_UNDECIDED",
    "AskTrigger",
    "BEAT_INTERVAL_S",
    "BUILTIN_IDS",
    "DAY_S",
    "DEFAULT_COMPONENT_TYPE",
    "ExprError",
    "FIELDS",
    "FRAMEWORK_OWNER",
    "ID_PATTERN",
    "KEEP_DAYS",
    "OPS",
    "PLACEHOLDER_ASK_UNFILLED",
    "PLACEHOLDER_COMPONENT_ABSENT",
    "PLACEHOLDER_UNDEFINED",
    "RuleCondition",
    "SOURCES",
    "STATE_FILENAME",
    "TYPES",
    "T_BOOL",
    "T_DATE",
    "T_DURATION",
    "T_ENUM",
    "T_NUMBER",
    "T_STRING",
    "T_TIME",
    "T_WEEKDAY",
    "Value",
    "VariableHost",
    "VariablePool",
    "VariableSpec",
    "VariableSpecError",
    "WHEN",
    "ask_key",
    "builtin_specs",
    "check_section",
    "coerce",
    "evaluate",
    "evaluate_or_none",
    "fmt_duration",
    "fmt_time",
    "matches",
    "matches_all",
    "parse_all",
    "parse_conditions",
    "parse_one",
    "referenced_variables",
    "result_type",
    "status_topic_of",
    "text_variables",
    "time_to_seconds",
    "type_map",
    "validate_expr",
]
