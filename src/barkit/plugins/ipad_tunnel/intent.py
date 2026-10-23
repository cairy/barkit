"""``_IntentMixin`` —— 隧道意图：配置段 → 期望态 → 声明给守护；含旧键迁移、热重载与那枚模式开关。

``plugin.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from barkit.contribution.specs import (
    ActionOutcome,
    ActionResult,
)
from barkit.plugins.ipad_tunnel import tunnel_spec
from barkit.plugins.ipad_tunnel.tunnel_spec import TunnelConfigError

from .constants import TUNNEL_SUBJECT, log


class _IntentMixin:
    """隧道意图：配置段 → 期望态 → 声明给守护；含旧键迁移、热重载与那枚模式开关。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 生命周期 ----------

    def _migrate_legacy_config(self) -> None:
        """升级一次性迁移：隧道功能开关从 framework 的 enabled 键迁到 tunnel_enabled。

        旧版 set_automation_active 把「隧道开关」写进 plugins.ipad_tunnel.enabled，而该键
        同时是框架的「插件加载门控」（core/plugin.Plugin.enabled / registry.load_all 据此
        决定是否实例化插件）。于是关开关 → 下次启动插件被跳过 → 整个组件不加载（本 bug 根因）。

        迁移：把用户配置里旧的 enabled 值转存为 tunnel_enabled，并移除该 enabled（回退默认
        true），使框架重新加载插件；隧道开关改走 tunnel_enabled。仅在「用户配置存在 enabled
        且尚无 tunnel_enabled」时执行一次（幂等），不干扰 PluginManager 对插件的运行时禁用
        （那种场景 tunnel_enabled 已存在，不会进入本分支）。
        """
        section = self.ctx.config.user_plugin_section(self.id)
        if "enabled" not in section or "tunnel_enabled" in section:
            return          # 幂等：无旧键 / 已迁移过，都不落盘
        legacy = bool(section["enabled"])   # 先取值再 pop，避免 mutate 里读已删的键
        # 「关」在 ``tunnel_enabled`` 上就是**声明默认值**，不必落键 —— 与 ``cfg.set``
        # 同一条口径（同值键会把那个值钉死：组件日后把缺省改成 true，这位用户会一直停在
        # false，而他并没有要求过「永远关」）。这一次改动还要连带删掉**框架键**
        # ``enabled``，两笔必须在同一次读—改—写里完成，故只能走逃生舱、在这里自己
        # 应用那条规则 —— 默认值仍问门面要，不抄数字。
        keep_tunnel_key = legacy != self.cfg.default_of("tunnel_enabled")

        def _mutate(section: dict) -> None:
            section.pop("enabled", None)
            if keep_tunnel_key:
                section["tunnel_enabled"] = legacy
            else:
                section.pop("tunnel_enabled", None)

        try:
            self.cfg.mutate(_mutate)
            log.info("已迁移 ipad_tunnel 配置：隧道开关 → tunnel_enabled=%s，"
                      "framework enabled 复位默认 true", legacy)
        except Exception:  # noqa: BLE001
            log.exception("迁移 ipad_tunnel 配置失败（下次启动将重试）")

    # ---------- 隧道意图（零授权） ----------

    def _build_tunnel_cfg(self) -> dict:
        cfg = dict(self.plugin_config())
        # 意图里的 enabled 是「隧道功能开关」= tunnel_enabled，与框架加载门控
        # plugins.ipad_tunnel.enabled 无关；此处显式覆盖，避免 framework 的 enabled
        # （恒为 true，保证插件加载）被当成隧道开关发下去。
        cfg["enabled"] = bool(self.automation_active)
        return cfg

    def sync_tunnel(self) -> None:
        """配置段 → 期望态 → **声明给守护**。校验失败抛 TunnelConfigError。

        **这份意图里只有意图**（要不要隧道、怎么建），没有任何「此刻怎么样」
        —— 链路通不通由中继的监听门在运行期表达（见 ``_relay_should_listen``）。
        因为只有意图，这里可以无脑重声明：无论盘上被谁改成什么样，重声明一次就回到
        正轨（幂等靠内容哈希，内容没变就不发帧）。

        **声明本身就带着「叫守护跑一轮」**：内容一变，编排层立刻把它排成「到期」。
        """
        cfg = self._build_tunnel_cfg()
        self.declare(TUNNEL_SUBJECT, tunnel_spec.build_tunnel(cfg))
        log.info("隧道意图已声明（enabled=%s）", cfg.get("enabled"))

    def on_config_changed(self, _payload=None) -> None:
        # 端口热更新：中继每轮重读配置，端口变更在**同一循环内**「关旧 → 开新」收敛
        # （bridge.RelayService），插件无需重建任何东西——旧实现要停线程再新建实例，
        # 会撞上同端口 bind 的 EADDRINUSE + 1s 退避重试窗口。
        # 声明可能变了（如直接改配置文件里的 tunnel_enabled）→ 请求框架重算服务集合。
        self._request_service_resync()
        try:
            self.sync_tunnel()
        except TunnelConfigError as e:
            log.error("热重载后隧道意图未更新（配置非法）：%s", e)

    def _current_tunnel_intent(self) -> dict:
        """当前隧道意图正文（诊断页判「配置 hijack_* 是真是假」用）；配置非法时给空。

        与 ``sync_tunnel()`` 同一个构造口（``_build_tunnel_cfg`` → ``build_tunnel``），
        故诊断页读到的开关值就是刚刚声明给守护的那一份，不会两处各说一套。
        """
        try:
            return tunnel_spec.build_tunnel(self._build_tunnel_cfg())
        except TunnelConfigError:
            return {}

    # ---------- 建隧道模式（自动 = 等链路 / 手动 = 不等链路直接尝试） ----------

    @property
    def ignore_link(self) -> bool:
        """手动模式：True = 不等链路就绪也让中继监听（`_relay_should_listen`）。

        它**不进意图**（v2 契约）：helper 那侧看到的只是「上游端口在不在听」，
        而这一个开关决定的是 app 侧要不要在那条链路没就绪时也占用端口。
        """
        return bool(self.cfg.value("ignore_link"))

    def set_ignore_link(self, manual: bool) -> "ActionResult | None":
        """写 user config 的 ignore_link + 叫 helper 跑一轮。

        只影响「要不要等链路就绪」，不动隧道总开关（tunnel_enabled），也不起停桥接
        （桥接线程照跑，探针继续给结论，只是中继的监听门不再看它）。

        拨完这个开关**必须**主动叫一轮：开关**不进意图**，所以重声明那份意图一个字都
        没变、发不出去；而中继下一轮（≤1s）就会按新判据占用 / 放开端口，helper 那边
        要知道「上游来了 / 走了」只能靠这一下，否则最坏要等 5s 轮询。这里主动叫一次
        是补上「门开了但门没响」的那一小段；门真变化时中继还会再叫一次。
        """
        manual = bool(manual)
        if self.ignore_link == manual:
            return
        try:
            self.cfg.set("ignore_link", manual)
        except Exception as e:  # noqa: BLE001
            log.error("写隧道模式失败：%s", e)
            return ActionResult(ActionOutcome.FAILED, f"建隧道模式没写成：{e}")
        try:
            self.sync_tunnel()
        except TunnelConfigError as e:
            log.error("同步隧道意图失败：%s", e)
            return ActionResult(ActionOutcome.FAILED, f"模式已存下，但同步给守护失败：{e}")
        self._resync_now()
        self.publish_snapshot()

    def toggle_ignore_link(self) -> "ActionResult | None":
        """在 自动/手动 之间切换（返回值透传给宿主画结果）。"""
        return self.set_ignore_link(not self.ignore_link)
