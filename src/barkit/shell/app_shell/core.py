"""装配各段（core 五件套 / 心跳 / 会话 / 交互中枢 / 贡献收集）。

``app_shell.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from barkit.core.audio import AudioDeviceSource
from barkit.core.config import Config
from barkit.core.contributions import ContributionRegistry
from barkit.core.events import EventBus
from barkit.core.heartbeat import Heartbeat
from barkit.core.hotkeys import HotkeyManager
from barkit.core.power import PowerSource
from barkit.core.registry import Registry
from barkit.core.scheduler import Scheduler
from barkit.shell.helper_session import HelperSession
from barkit.shell.interaction import InteractionCenter
from barkit.shell.macos_window import (
    POLICY_ACCESSORY,
    set_activation_policy,
)

from .consts import log


class _CoreMixin:
    """装配各段：core 五件套、单一心跳、与守护的会话、交互中枢与贡献收集（``_build_*`` 那一片）。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 各段装配 ----------

    @staticmethod
    def _set_accessory_policy() -> None:
        """运行时设置无 Dock 图标（LSUIElement 对 python 直接运行不生效）。

        激活策略的切换统一走 ``macos_window.set_activation_policy``（UI 规范 §5.4
        收归：详情页弹出/关闭、识别窗口开/关共用同一入口，不再各写一份）。
        """
        set_activation_policy(POLICY_ACCESSORY)

    @staticmethod
    def _build_core():
        """core 五件套 + 单一心跳 + 与守护的那条会话：配置（含热重载）/ 事件总线 /
        调度器 / 注册表 + 热键管理器 + 会话。

        ``Heartbeat`` 是框架唯一时钟源（批次 8 §8.3.3 层 2）：在此创建并注入
        ``Scheduler``（由它统一 start，幂等），装配器持有以便退出时停表。
        **resident 周期型服务不经它**——其 ``Event.wait(interval)`` 经复核不是
        时钟源（无独立计时、无漂移累积），挂账已在批次 8 撤销，见 core/resident。

        ``HelperSession``（2026-09-29）取代了原先的 ``FileWatchSource``：与 root helper
        那条通道从「写期望态文件 ＋ 监视状态文件目录」换成**一条会话**，四件事（意图 /
        命令 / 事实 / 存活）各有载体。它在 ``registry.load_all()`` **之前**建好并把
        ``link`` 挂上 ctx —— 组件的 ``start()`` 里就会 ``declare`` / ``subscribe``，
        那时还没连上也不要紧：意图与订阅记在框架侧，连上即补发。

        ``ctx.mark_dirty`` 是事件 handler 的按需点火入口（§8.2.2，批次 10）：
        在此把 ``Scheduler.mark_dirty`` 挂到共享 ctx 上。回调用 ``(plugin_id, reason)``
        签名——ctx 是共享单例、拿不到调用者身份，故由 ``Plugin.mark_dirty`` 填 id。
        ``Registry`` 因此**不依赖** ``Scheduler``（保持 core 内两模块互不静态引用）。
        """
        config = Config()
        config.start_watching()
        _CoreMixin._apply_log_level(config)
        events = EventBus()
        heartbeat = Heartbeat()
        scheduler = Scheduler(events, heartbeat)
        registry = Registry(config, events)
        _CoreMixin._wire_context(registry, scheduler)
        # 与守护的那条会话（§3 四类消息的唯一载体）。装配层持有它、并把它挂进 ctx；
        # 组件只经 `declare` / `run_op` / `subscribe` / `observed` 四个包装使用，
        # **永远不碰 socket、不拼协议帧、不认路径**。
        helper_session = HelperSession(events)
        registry.ctx.helper = helper_session.link
        # 电源事件源（§8.2.1）：**四条**通知一个观察者 —— 系统与显示器各自一对睡眠 /
        # 唤醒（`system.power.{sleep,wake,display_sleep,display_wake}`）。唤醒类延迟一个
        # 稳定窗口再投递，睡眠类立刻投（理由见 core/power 的 docstring）。
        # 它**不做懒加载**——电源事件是全局的，没有「谁声明才注册」的粒度可言，
        # 而底层只是一个通知观察者，常驻成本可忽略。非 macOS 上 start()
        # 返回 False，订阅方照常运行、只是收不到这族事件（降级，不是报错）。
        power = PowerSource(events)
        power.start()
        # 音频设备事件源（§8.2.1）：3.5mm 插拔 / 默认设备漂移 → 等稳定窗口后投递
        # system.audio.device_changed。它的比对节律**挂在同一个心跳上**（不新增时钟）；
        # 非 macOS 或读不到现场时 start() 返回 False —— 订阅方照常运行、只是收不到这一类
        # 事件（为什么不用 CoreAudio 原生通知，见 core/audio 的模块 docstring）。
        audio = AudioDeviceSource(events, heartbeat)
        audio.start()
        return (config, events, scheduler, registry, HotkeyManager(), heartbeat,
                helper_session, power, audio)

    @staticmethod
    def _apply_log_level(config) -> None:
        """配置落定后应用 ``log.level``（进程级准备时它还读不到）。

        拆两步是因为顺序：``setup_logging`` 在 ``app.main()`` 里、早于 AppShell，
        那时磁盘上的用户配置还没加载；而常驻实例由 launchd 拉起，拿不到终端环境
        变量 —— 剩下能改它级别的入口只有这一处。

        **用户没配 ``log.level`` 时这里什么都不做**（``log_level()`` 回空串）：
        让 ``setup_logging`` 已应用好的级别（环境变量或出厂档 info）继续生效。
        出厂文件刻意不带这一键，正是为了不让它反过来盖掉环境变量。
        """
        from barkit.core.logging_setup import resolve_level, set_level

        configured = config.log_level()
        if configured:
            set_level(resolve_level(configured))

    @staticmethod
    def _wire_context(registry: Registry, scheduler: Scheduler) -> None:
        """把调度器的「按需点火」入口与通知中枢挂到共享 ctx（§8.2.2 / §8.3）。

        单独成方法而非内联：这是组合根唯一需要 ``Scheduler`` 参与的 ctx 接线，
        抽出来即可在测试里用真件（临时 config + 真 Registry）断言，不必启动整条
        ``run()`` 链路（后者要动真机数据路径，见《重构-开工手册》§4.3 的 ⑧）。

        ``ctx.notify`` 是组件的系统通知出口（``capabilities.notify.NotificationHub``）。
        中枢本身零平台依赖，平台后端在这里接上：今天只有 macOS 一枚
        （``shell/macos_notify``），换平台时加一个后端进列表即可，中枢不动。
        ``resolve_plugin`` 让用户点了通知按钮之后能找回发起它的插件。
        """
        registry.ctx.mark_dirty = scheduler.mark_dirty
        registry.ctx.notify = _CoreMixin._build_notify_hub(registry)

    @staticmethod
    def _build_interaction(registry: Registry, events, heartbeat):
        """装配条件触发询问的中枢，挂到共享 ctx（方案「条件触发询问」批 2）。

        与 ``mark_dirty`` / ``notify`` 同款「框架注入、组件只管用」：组件在 ``start()``
        里交给中枢一条规则 + 一个答案回调，窗口 / 排队 / 去重账一概不碰。未接线时
        ``ctx.interaction`` 为 None，组件登记退化为空操作（测试桩零改动）。

        复检节律挂在框架唯一心跳上（中枢自己不起定时器）。
        """
        center = InteractionCenter(events, heartbeat=heartbeat)
        registry.ctx.interaction = center
        return center

    @staticmethod
    def _build_notify_hub(registry: Registry):
        """装配通知中枢（后端 + 归属解析）。

        延迟导入 ``capabilities``：组合根对能力层是「按需取用」，与本文件里
        ``reconcile_root_binaries`` 那处同款（L3 → L2 是允许方向，延迟导入让
        依赖面在 import 期不可见）。
        """
        from barkit.capabilities.notify import NotificationHub
        from barkit.shell.macos_notify import MacNotifyBackend

        backend = MacNotifyBackend()
        return NotificationHub(
            backends=[backend],
            resolve_plugin=lambda plugin_id: registry.plugins.get(plugin_id),
        )

    def _build_contributions(self, plugins, registry, scheduler) -> ContributionRegistry:
        """统一收集贡献，汇总三道拦截结果，并把 daemon 插件交给调度器。"""
        # §6.4：插件间排序唯一数据源 = registry.plugin_order()（config 顶层
        # plugin_order，用户排序），ContributionRegistry 三处渲染同源消费。
        contributions = ContributionRegistry(plugins, order_provider=registry.plugin_order)
        self._contributions = contributions

        # 版本闸门：被拒绝的插件已不在注册中心
        for plug, reason in contributions.rejected:
            log.error(
                "插件 %s 因契约不兼容被跳过：%s", plug.id or type(plug).__name__, reason
            )
        # 发现期闸门：api_version 不匹配的插件在导入期即被拒（早于实例化），
        # 不会进入 plugins 列表，此处仅汇总以便排查。
        for name, reason in registry.rejected:
            log.error("插件 %s 因契约不兼容在发现期被跳过：%s", name, reason)
        # 贡献期隔离（E3 ①）：运行时抛异常的插件已被隔离，汇总以便排查
        for pid, exc in contributions.failed:
            log.error("插件 %s 在贡献收集中抛异常已被隔离：%r", pid, exc)

        # 两种 run_mode 都交给调度器（2026-09-22 改）：周期型订节拍，按需型只登记，
        # 为的是它的系统事件 handler 能有地儿点火（见 Scheduler.add_plugin）。
        for plugin in plugins:
            scheduler.add_plugin(plugin)
        return contributions
