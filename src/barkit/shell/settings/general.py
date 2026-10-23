"""``_GeneralMixin`` —— 「通用」页：菜单栏图标卡与角标阈值。

``settings.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtWidgets import QWidget

from barkit.contribution.specs import TRAY_MIN_LEVEL_DEFAULT
from barkit.shell.ui_kit import (
    ChoiceSelector,
    form_row,
    settings_section,
)

from .helpers import _badge_level_choices


class _GeneralMixin:
    """「通用」页：菜单栏图标卡与角标阈值。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    def _build_tray_card(self, parent: QWidget) -> QWidget:
        """菜单栏那一格的两项设置：画哪枚**主图标**、什么**级别**才出角标。

        两件事都只影响菜单栏（面板行与标题栏不受影响），故同处一张卡；
        卡片高度受窗口固定高度约束（本页不外包滚动区），文案要短。
        「主图标」的候选只含**在声明里定义了主图标**的状态机，准入判据见
        :meth:`_tray_base_choices`（本处不另写一份）。分区卡走统一的 ``settings_section``
        原语（标题/内边距/段距单一来源，2026-09-20 UI 统一重构）。
        """
        card, lay = settings_section("菜单栏", parent=parent)

        self._tray_base_selector = ChoiceSelector(
            self._tray_base_choices(), self._config_tray_base_source(), parent=card
        )
        self._tray_base_selector.selected.connect(self._on_tray_base_selected)
        lay.addLayout(form_row("主图标", self._tray_base_selector, card))
        # 配置项必须自述的两句话（§5.2.2-③）：换来的是什么、代价是什么。
        lay.addWidget(self._note(
            "选定后菜单栏那一格改显示这台状态机当前的取值图标（如屏幕字形的实心 / 空心）；"
            "它没给取值图标或已失联时回落它声明的主图标，只有该组件被禁用 / 卸载才回落"
            "BarKit 默认图标。"
        ))

        self._badge_level_selector = ChoiceSelector(
            _badge_level_choices(), self._config_badge_level(), parent=card
        )
        self._badge_level_selector.selected.connect(self._on_badge_level_selected)
        lay.addLayout(form_row("角标最低级别", self._badge_level_selector, card))
        lay.addWidget(self._note(
            "低于这一级的取值不在菜单栏出角标（面板行里照旧看得见）。错误级永远露出，"
            "收多紧都屏蔽不掉。"
        ))
        return card

    def _tray_base_choices(self) -> list[tuple[str, str]]:
        """来源候选：``[("BarKit 默认", ""), ("组件名 · 状态名", 状态机身份), ...]``。

        组件名取 ``ComponentSpec.name``（身份唯一真源，D14），缺失时回落 plugin_id；
        状态名取 ``StateSpec.title``。**取值存状态机身份**（``StateSpec.state_id`` =
        ``{plugin_id}.{状态机 key}``）—— 显示名只是标签，组件改名不会让用户已选的
        来源失效；取值也不是事件总线 topic，配置与 pub/sub 层解耦。

        准入判据不在本处另写一份：直接用注册中心的 ``tray_base_candidates()``，
        与装配器读回来源时用的是同一条判据（组件禁用 ⇒ 两边同时「查无此机」）。
        """
        choices: list[tuple[str, str]] = [("BarKit 默认", "")]
        if self._registry is None:
            return choices
        names = {c.plugin_id: c.name for c in self._registry.components()}
        for spec in self._registry.tray_base_candidates():
            owner = names.get(spec.plugin_id) or spec.plugin_id
            label = spec.title or spec.key or spec.state_id
            choices.append((f"{owner} · {label}", spec.state_id))
        return choices

    def _config_tray_base_source(self) -> str:
        return self._config.tray_base_source() if self._config is not None else ""

    def _on_tray_base_selected(self, source: str) -> None:
        """落盘 + 立即换图（写配置后 Config 不会发 ``changed``，故必须回调装配器）。"""
        if self._config is not None:
            self._config.set_tray_base_source(source)
        if self._on_tray_base_changed is not None:
            self._on_tray_base_changed()

    def _config_badge_level(self) -> str:
        """首屏取值 = **合并后**的有效配置（缺省即出厂档，不假装用户改过）。"""
        return (
            self._config.tray_min_level()
            if self._config is not None
            else TRAY_MIN_LEVEL_DEFAULT
        )

    def _on_badge_level_selected(self, level: str) -> None:
        """落盘 + 立即重算角标队列（同 ``_on_tray_base_selected``：程序内写盘不发 ``changed``）。

        必须回调装配器的那半：阈值变了但队列不重算，用户会看到「档位已经收紧、角标还在
        按旧档画」——要等下一次状态事件才对得上。
        """
        if self._config is not None:
            self._config.set_tray_min_level(level)
        if self._on_badge_level_changed is not None:
            self._on_badge_level_changed()
