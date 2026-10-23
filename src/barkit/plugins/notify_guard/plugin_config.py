"""配置读写门面（设置页与自身共用，避免两处各写一遍）：自动化总闸、
启动拉起开关、防抖轮数、静音规则与 ``config_schema``（§9.5 单一真源）。

写盘一律零授权、失败只返回 ``False`` 由调用方行内提示 —— **插件不弹窗**（零 PySide6）。
写完**立即**同步给 decider：``tick`` 每轮也会重读一遍（那才是权威路径），但要等下一
拍才生效，用户点完下拉之后的那段时间界面就在撒谎。这里没有 ``poll``：框架节拍住在
``plugins.<id>.poll``，而 D27 明确它只服务 ``core/scheduler.py`` 一个读者（配置键单
消费者），组件自己再读一遍等于把「建议周期」变成一份可以分叉的真相。

``plugin.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import logging

from barkit.core.schema import (
    FieldSpec,
    FieldType,
    FormSchema,
)


log = logging.getLogger("barkit.plugins.notify_guard.plugin")


class _ConfigMixin:
    """配置读写门面：自动化总闸、启动拉起开关、防抖轮数、静音规则与 §9.5 默认值真源。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 配置读取（设置页与自身共用，避免两处各写一遍）----------

    def rules(self) -> list[dict]:
        """静音规则（用户 YAML；结构 ``{name/reason, processes}``，本批不改结构）。

        默认值（``[]``）的真源是 ``config_schema()`` 里那条 ``rules``。
        """
        return list(self.cfg.value("rules"))

    @property
    def automation_active(self) -> bool:
        """自动化（自动静音）是否运行中；关闭后状态完全由手动开关控制。"""
        return bool(self.cfg.value("auto_enabled"))

    def set_automation_active(self, enabled: bool) -> None:
        """开 / 关通知守卫自动化（自动静音）。写用户 YAML（零授权）。

        enabled=False：decider 不再按信号决策，当前状态交回手动开关；
        enabled=True：重新开启自动化，按信号重评估。

        落点已从面板徽标移到**设置页顶部**（裁决项 6）——它低频且破坏性，
        与《网络切换》§4.5 同一条判据。面板行只留那枚「临时切换」的 Switch。
        """
        enabled = bool(enabled)
        if self.automation_active == enabled:
            return
        try:
            self.cfg.set("auto_enabled", enabled)
        except Exception as e:  # noqa: BLE001
            log.error("写自动化开关失败：%s", e)
            return
        self.decider.auto_enabled = enabled
        if not enabled:
            # 关闭自动化：交回当前手动状态（不强制任何写入，保持现状）
            self.decider.mode = "manual"
            self.decider.manual_allow = self.decider.applied
            self.decider.combo_at_toggle = None
        else:
            # 重新开启自动化：退出手动，按信号重评估
            self.decider._enter_auto()
        self._publish_all()

    @property
    def open_on_startup(self) -> bool:
        """「启动时自动拉起 OpenDisplay」开关（用户 YAML；**缺省关着**）。

        缺省即「用户没设过」⇒ 不自动拉起 —— 这是设置项的判据本体，不是兜底值。
        """
        return bool(self.cfg.value("open_on_startup"))

    def set_open_on_startup(self, enabled: bool) -> bool:
        """写「启动时自动拉起 OpenDisplay」开关（设置页用；零授权）。

        它只影响**下一次启动**的首拍（:meth:`_launch_on_startup_once`），运行期不产生任何
        动作 —— 所以这里只写盘，**不动** ``_startup_launch_done``：用户刚把它打开、
        BarKit 还没重启的这段时间里，不该突然冒一个 OpenDisplay 出来。

        失败返回 False，由调用方（设置页）行内提示 —— **插件不弹窗**（零 PySide6）。
        """
        enabled = bool(enabled)
        if self.open_on_startup == enabled:
            return True

        try:
            self.cfg.set("open_on_startup", enabled)
        except Exception as e:  # noqa: BLE001
            log.error("写「启动时自动拉起 OpenDisplay」开关失败：%s", e)
            return False
        return True

    # ---------- 装置调参（设置页；`poll` 归框架，本组件只管 `debounce`）----------
    #
    # **为什么这里没有** ``poll_seconds()``：框架节拍住在 ``plugins.<id>.poll`` 里，
    # 而 D27（配置键单消费者）明确它**只服务 ``core/scheduler.py`` 一个读者** ——
    # 组件自己再读一遍，等于把「建议周期」那份声明又变成一份可以分叉的真相。
    # 设置页因此只报轮数、不报秒数（它读不到那个值，报出来就是第二真源）；
    # 门禁 ``test_component_does_not_read_framework_poll_key`` 按 AST 守着这一条。

    @property
    def debounce(self) -> int:
        """防抖轮数：**连续几轮结果一致**才动系统通知开关（出厂默认 2）。

        真源是 ``config_schema()`` 的 ``debounce`` 字段读出来的**合并后**的值，故用户
        手改过 YAML 也照他写的算；下限 1 与 ``tick`` 里那条夹取同一口径。
        """
        return max(1, int(self.cfg.value("debounce")))

    def set_debounce(self, cycles: int) -> bool:
        """写防抖轮数（设置页用；零授权）。失败返回 False，由调用方行内提示。

        写完**立即**同步给 decider：``tick`` 每轮也会重读一遍（那才是权威路径），
        但要是等下一拍才生效，用户点完下拉之后的那段时间界面就在撒谎。
        """
        cycles = max(1, int(cycles))
        if self.debounce == cycles:
            return True

        try:
            self.cfg.set("debounce", cycles)
        except Exception as e:  # noqa: BLE001
            log.error("写防抖轮数失败：%s", e)
            return False
        self.decider.debounce = cycles
        return True

    def save_rules(self, rules: list[dict]) -> bool:
        """写回静音规则（设置页规则编辑器用；零授权）。

        失败返回 False，由调用方（设置页）提示——**插件不弹窗**（零 PySide6）。
        成功后丢弃上一轮命中缓存：规则变了，命中集合就可能不同，下一轮 tick 重读。
        """
        try:
            self.cfg.set("rules", list(rules))
        except Exception as e:  # noqa: BLE001
            log.error("写规则失败：%s", e)
            return False
        self._last_combo = None
        self._last_hits = []
        return True

    # ---------- §9.5 配置默认值单一真源 ----------

    def config_schema(self) -> FormSchema:
        """notify_guard 配置 schema（§9.5 单一真源）。

        声明字段经 Config 注入后合成默认值，取代 config.default.yaml 的
        plugins.notify_guard 段（enabled 属框架保留键，仍由 yaml 提供）。
        插件内 ``.get(key, default)`` 的 fallback 仅为防御性，真源是此处。

        ``rules`` 保留为 ``FieldType.LIST``：它**只承载默认值**（默认 ``[]``），
        编辑器是设置页自绘的规则编辑器（``FieldType.LIST`` 全仓无渲染器，
        ``core/schema.py`` 已注明）。字段与编辑器分居两处**不是双真源**——
        字段是"初值"，编辑器是"改它的唯一入口"。

        **别在这里声明 ``poll``**：框架节拍（设置窗里本组件页尾「运行间隔」卡那一行）
        由**框架**按 ``run_mode`` ＋ ``suggested_poll`` 自动生成控件、写
        ``plugins.<id>.poll``（契约 D21）。组件再声明一个同名字段＝第二条覆盖通道，
        而界面上只看得到框架那一个。门禁 ``test_no_plugin_declares_framework_poll_field`` 守。
        """
        return FormSchema(
            plugin_id=self.id,
            version=1,
            fields=(
                FieldSpec(key="debounce", label="防抖周期数", type=FieldType.NUMBER, default=2),
                FieldSpec(
                    key="auto_enabled", label="自动化（自动静音）",
                    type=FieldType.BOOL, default=True,
                ),
                # 装置开关（不属于静音策略）：缺省 False = 用户没设过就不自动拉起
                FieldSpec(
                    key="open_on_startup", label="启动时自动拉起 OpenDisplay",
                    type=FieldType.BOOL, default=False,
                ),
                FieldSpec(key="rules", label="静音规则", type=FieldType.LIST, default=[]),
            ),
        )
