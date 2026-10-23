"""``StateReport`` —— 插件对外状态的**唯一表达形式**（动态数据，§5.2）。

它与 ``StateSpec`` 分家是契约口径本身：「静态归 Spec，动态归 StateReport」，这条分界由 ``test_statespec_is_static_only`` 守。23 行一个字段不多，单独成模块是为了让「运行期数值」这一族有个落点，别让哪天有人往 ``StateSpec`` 里加一个 ``summary``。
依赖：无（纯 ``str`` / ``float``，模块级零 import）。

``specs.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StateReport:
    """插件对外状态的唯一表达形式（**动态**数据，§5.2）。

    由 tick() 产出，框架在返回非 None 后自动 publish
    ``plugin.<plugin_id>.status.<key>``；渲染层只消费，不回写。

    **只报取值名**（决议 D13）：``state`` 必须是该状态机 ``StateSpec.states`` 里
    某个 ``StateOption.id``；级别、取值图标、默认文案都归声明，运行期不再重复给
    —— 一套取值三个真源，迟早在界面上分叉。报了**未声明**的取值属组件写错，
    框架按 ERR 露出（不静默吞掉，见 ``shell/renderers/state_view.resolve``）。

    **文案只有一个字段**（决议 D18）：``summary``。它同时出现在三处——列表行副标题
    （未声明行级 ``subtitle`` 时）、副标题的悬停提示、标题栏图标的悬停提示——内容完全
    一致。它**覆盖**取值声明里的默认文案 ``StateOption.text``，留空即用默认。
    原 ``detail`` 字段已删除：它运行期零消费（副标题的真源一直是 ``summary``），
    需要「下一步做什么」的引导语请落详情页（``DetailEntrySpec``）或行内按钮
    （``controls`` 里的 ``ButtonSpec``）。
    """

    state: str                      # 取值名，必须在 StateSpec.states 里声明过
    summary: str = ""               # 唯一文案；留空 → 用该取值声明的默认文案
    updated_at: float = 0.0         # 时间戳（面板另按收到事件的时刻判失联，见遗留项）
