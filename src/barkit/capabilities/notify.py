"""capabilities.notify：组件的「系统通知」能力（能力层）。

**为什么需要它**：组件发现异常时，用户往往不在面板前面——面板收起时组件什么都说不出来。
本能力把「弹一条系统通知」做成框架服务：组件只说**要说什么**（标题 / 正文 / 几个动作），
「怎么弹、弹完按钮怎么回来」全归框架。

分层与依赖方向（同 ``automation.py``）：

- 本模块**零平台依赖、零 Qt 依赖**：只定义数据、后端协议与路由中枢。
- 真正的平台后端在 ``shell/``（macOS 用 ``NSUserNotificationCenter``）——那里已经允许
  PyObjC 与 Qt；由装配层（``shell/app_shell.py``）建好后经 ``ctx.notify`` 注入。
- 本包**不得** import ``shell`` / ``plugins``（L2 在 ``shell`` 之下），由
  ``tests/test_architecture.py::test_layer_dependency_direction_upper_layers`` 守门。

**为什么回调走「方法名」而不是塞闭包进契约**：通知发出去之后，用户可能在几小时后才点
按钮——那时发起它的那一轮 ``tick()`` 早就结束了，闭包捕获的状态多半已经过期。所以契约里
只放**纯数据**（``NotificationRequest``），用户点击时框架按通知归属回查插件，调用它的
``on_notification_action(key)``——与 ``on_observed`` 同一条「框架把事件投回插件」的
路子。插件据此重新读现场再决定做什么，而不是相信一份几小时前的快照。

**按钮数量上限**：macOS 的通知在「关闭」之外只容得下两枚附加按钮
（``actionButtonTitle`` ＋ ``otherButtonTitle``）。超过两枚在契约层就拒绝——与其让第三枚
静默消失（用户以为界面少画了一个按钮），不如在声明期就报错。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

from barkit.core.plugin import Plugin

log = logging.getLogger(__name__)

#: 通知在「关闭」之外能带的附加按钮数上限（macOS 两枚，见模块 docstring）。
MAX_ACTIONS = 2


@dataclass(frozen=True)
class NotificationAction:
    """通知上的一枚按钮：``key`` 回传给插件，``label`` 是用户看到的字。"""

    key: str
    label: str


@dataclass(frozen=True)
class NotificationRequest:
    """一条待发通知（纯数据，决议「契约只放数据」）。

    ``key`` 有两个作用，都是**同一插件内**的语义：

    1. **归属**：用户点按钮时框架按它回查是哪个插件发的；
    2. **去重**：同一 ``key`` 且内容未变时**不重复弹**——通知是「出事说一声」，
       不是「每次检查都喊一遍」。内容变了（例如从「检测到异常」变成「已恢复」）
       才会再次弹出。
    """

    key: str
    title: str
    body: str = ""
    actions: tuple[NotificationAction, ...] = ()

    def fingerprint(self) -> tuple:
        """内容指纹：用于判断「同一条通知是否变了」。"""
        return (self.title, self.body, tuple((a.key, a.label) for a in self.actions))


class NotificationBackend(Protocol):
    """平台后端协议：做得到就返回 True，做不到返回 False（枢纽会跳过它）。"""

    def available(self) -> bool:
        """本平台此刻能不能发通知。"""

    def send(self, request: NotificationRequest,
             on_action: "Callable[[str, str], None]") -> bool:
        """投递一条通知。

        ``on_action(notification_key, action_key)`` 由后端在用户点击按钮时调用
        （**必须在主线程**调——后端自己负责把回调从系统队列搬回主线程）。
        """


class NullBackend:
    """无通知可发时的兜底：什么都不做，只让链路不报错。

    存在的意义是「Windows 上也能跑」：后端缺席不该让组件的生活史变成异常。
    """

    def available(self) -> bool:
        return False

    def send(self, request: NotificationRequest,
             on_action: "Callable[[str, str], None]") -> bool:
        return False


class NotificationHub:
    """通知中枢：管归属、管去重、把用户点击派回**发起它的那个插件**。

    框架单例，由装配层建好挂到 ``ctx.notify``。刻意不碰 Qt 与平台 API——
    线程跳转归后端（它知道自己的投递 API 要求哪个线程），中枢只做三件纯逻辑的事。
    """

    def __init__(self, backends: "Sequence[NotificationBackend] | None" = None,
                 resolve_plugin: "Callable[[str], object | None] | None" = None) -> None:
        self._backends = list(backends) if backends else []
        #: 通知 key → 发起它的插件 id（用户点击时靠它找回插件）。
        self._routes: dict[str, str] = {}
        #: 通知 key → 上次发出的内容指纹（去重；同 key 同内容不重复弹）。
        self._sent: dict[str, tuple] = {}
        self._resolve = resolve_plugin
        self._delivered = 0

    # ---------- 对外：组件发通知 ----------

    def send(self, plugin_id: str, request: NotificationRequest) -> bool:
        """替 ``plugin_id`` 投递一条通知。

        返回是否**真的发出去了**（没后端、去重命中、声明非法都返回 False）——
        组件据此可以把「通知没发出去」如实记进日志或反映到状态里，而不是以为用户
        已经被告知了。
        """
        if not request.key or not request.title:
            log.error("插件 %s 想发无 key/标题的通知，已丢弃（key 是归属与去重的依据）",
                      plugin_id)
            return False
        if len(request.actions) > MAX_ACTIONS:
            log.error("插件 %s 的通知 %s 带了 %d 枚按钮，超过上限 %d，已丢弃",
                      plugin_id, request.key, len(request.actions), MAX_ACTIONS)
            return False

        backend = self._pick_backend()
        if backend is None:
            log.warning("插件 %s 的通知「%s」没发出去：本平台无可用通知后端",
                        plugin_id, request.title)
            return False

        fingerprint = request.fingerprint()
        if self._sent.get(request.key) == fingerprint:
            log.debug("通知 %s 内容未变，跳过重复投递", request.key)
            return False

        self._routes[request.key] = plugin_id
        ok = backend.send(request, self._on_action)
        if ok:
            self._sent[request.key] = fingerprint
            self._delivered += 1
            log.info("已投递通知「%s」（来自 %s，%d 枚按钮）",
                     request.title, plugin_id, len(request.actions))
        else:
            log.warning("通知「%s」投递失败（后端 %s）", request.title,
                        type(backend).__name__)
        return ok

    def forget(self, plugin_id: str) -> None:
        """插件卸载时清掉它的归属记录（避免点了按钮却找不到插件）。"""
        for key in [k for k, v in self._routes.items() if v == plugin_id]:
            self._routes.pop(key, None)
            self._sent.pop(key, None)

    # ---------- 后端回调 ----------

    def _on_action(self, notification_key: str, action_key: str) -> None:
        """用户点了通知上的按钮：找回插件，把动作名投回去。"""
        plugin_id = self._routes.get(notification_key)
        if plugin_id is None:
            log.info("收到通知动作 %s/%s，但找不到发起它的插件（可能已卸载）",
                     notification_key, action_key)
            return
        plugin = self._resolve(plugin_id) if self._resolve else None
        if plugin is None:
            log.info("收到通知动作 %s/%s，但插件 %s 不在线", notification_key,
                     action_key, plugin_id)
            return
        handler = getattr(plugin, "on_notification_action", None)
        if not callable(handler):
            log.warning("插件 %s 没实现 on_notification_action，动作 %s 被丢弃",
                        plugin_id, action_key)
            return
        try:
            handler(action_key)
        except Exception:  # noqa: BLE001 —— 一个插件的处理不能掀翻整个回调链
            log.exception("插件 %s 处理通知动作 %s 时出错", plugin_id, action_key)

    def _pick_backend(self) -> "NotificationBackend | None":
        """取第一个自报可用的后端（顺序即优先级，由装配层决定）。"""
        for backend in self._backends:
            try:
                if backend.available():
                    return backend
            except Exception:  # noqa: BLE001 —— 可用性探测本身不该让发送路径翻车
                log.exception("通知后端 %s 探测可用性失败，跳过", type(backend).__name__)
        return None


class NotifyCapability(Plugin):
    """通知能力 mixin：给组件一个 ``notify()``，并把点击回程接到 ``on_notification_action``。

    与其他能力一样写成协作式 mixin（``__init__`` 调 ``super().__init__``），
    以便 ``class Plugin(NotifyCapability, AutomationCapability)`` 这样组合而 MRO 干净。
    """

    def __init__(self, ctx) -> None:
        super().__init__(ctx)

    def notify(self, request: NotificationRequest) -> bool:
        """发一条系统通知（**可在 worker 线程调用**：线程跳转归后端）。

        返回是否真的发出去了；``False`` 的几种常见来由：本平台没有通知后端、
        同一条通知内容没变（去重）、按钮数超过上限。想区分可以看日志。
        """
        hub = getattr(self.ctx, "notify", None)
        if hub is None:
            log.debug("%s 想发通知，但框架没装通知中枢（测试桩 / 无界面进程）", self.id)
            return False
        try:
            return bool(hub.send(self.id, request))
        except Exception:  # noqa: BLE001 —— 通知失败不该拖垮组件本身的收敛
            log.exception("%s 发通知时出错", self.id)
            return False

    def on_notification_action(self, key: str) -> None:
        """用户点了本组件某条通知上的按钮（**主线程**调用，覆写它）。

        调用时机可能在通知发出后很久，所以这里应**重新读现场**再动作，
        不要依赖发通知那一刻的缓存——那份缓存早过期了。

        默认空实现：一个组件只发「纯告知」的通知（没有按钮）时不必覆写它。
        """
        return None
