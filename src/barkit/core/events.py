"""core/events：轻量事件总线。

跨线程安全：publish 走 Qt Signal，工作线程发布的事件自动以 queued
方式回到总线所在线程（主线程）分发，UI 侧无需额外处理。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)

#: 框架事件（§8.2.1 的第三类 topic，形态 ``app.<event>``）：**状态面板被打开**。
#:
#: 发布方是状态面板（``shell``），消费方是各组件（``plugins/*``）。常量落在**这里**
#: 而不是发布方那个模块里：topic 名字是**组件契约**的一部分，而组件的 import 面只应
#: 触及 ``core`` / ``contribution`` / ``capabilities`` —— 为了一个字符串去 import
#: 整个面板 widget 模块，是把「视图实现」拖进「组件契约」。
#:
#: 语义＝「用户刚把面板打开」，载荷无内容（这一刻发生的事本身就是全部信息，没有
#: 「因为什么」可带）。它存在的唯一理由是**按需型组件没有节拍**：面板那一行显示的是
#: 最后一次 ``tick()`` 的结论，而按需型只在启动 / 系统事件时跑 ⇒ 屏幕一直亮着时掉线，
#: 那一行会停在旧结论上（``bt_guard`` 现场：鼠标掉了两小时，界面还写「设备在线」）。
#: 组件按 §8.3.1 声明本 topic 即可「每次开面板重跑一轮」，handler 里照旧只
#: ``mark_dirty()``，读现场与动作仍归 ``tick()``（§8.2.2）。
PANEL_OPENED_TOPIC = "app.panel_opened"

#: 网络场景命中（``route_guard`` 的 ``profile`` 状态机）的 topic。
#:
#: 常量落在**这里**而不是发布方那个模块里，理由与 :data:`PANEL_OPENED_TOPIC` 同款：
#: topic 名是**组件契约**的一部分，为了一个字符串去 import 网络切换插件，等于把
#: 「插件实现」拖进「组件契约」（插件之间只经总线，不许互相 import）。
#:
#: **真源仍在发布方**：这里的字面量由
#: ``test_route_profile_topic_constant_matches_plugin`` 与
#: ``route_guard`` 的 ``status_topic("profile")`` 对拍，改了一边另一边立刻转红。
#: 载荷是 ``StateReport``：取值 ``"active"`` ＋ 副标题＝场景名（``"Office"``）。
ROUTE_PROFILE_TOPIC = "plugin.route_guard.status.profile"

#: 「变量池里有变量变了」的 topic（变量池 → 全体消费方；设计文档 §2.3）。
#:
#: 常量落在**这里**而不是 ``core/variables`` 那个包里，理由与
#: :data:`PANEL_OPENED_TOPIC` 同款：topic 名是**组件契约**的一部分，而组件的 import 面
#: 只应触及 ``core`` / ``contribution`` / ``capabilities``。
#:
#: **何时发**：变量池挂在框架心跳上的那一拍跑完、且「全部变量的渲染值」与上一拍不同时
#: （见 ``core/variables/host.py``）。载荷就是那份指纹
#: （``tuple[tuple[str, str], ...]``，变量 id → 渲染值）。
#:
#: 载荷刻意是**整份指纹**而不是「哪个变量变了」：一份指纹谁都读得起，消费方各自比出
#: 自己关心的那一个（任务比 `{off_time}`、变量页比它列的每一行）；而「谁变了」这件事
#: 一旦由发布方算，它就得多知道一层「谁在关心谁」——那正是订阅方自己的事。
VARIABLES_CHANGED_TOPIC = "app.variables.changed"

#: 「守护推来一份观测」的 topic（2026-09-29 通道换成会话之后新增）。
#:
#: 它取代的是 ``system.file.changed`` 那条文件订阅：从前 helper 每轮写状态文件、
#: 框架用 ``QFileSystemWatcher`` 监视目录；现在观测由会话**推**过来，框架把它转成
#: 一条总线事件，分发规则一字未变（插件经 ``Plugin.subjects()`` 声明要哪几个领域，
#: 框架按声明过滤后再转交 ``Plugin.on_observed()``）。
#:
#: 常量落在这里而不是发布方（``shell/helper_session``）那个模块里，理由与
#: :data:`PANEL_OPENED_TOPIC` 同款：topic 名是**组件契约**的一部分，为了一个字符串去
#: import 会话驱动，等于把「装配实现」拖进「组件契约」。
HELPER_OBSERVED_TOPIC = "system.helper.observed"


@dataclass(frozen=True)
class ObservedPush:
    """``system.helper.observed`` 的载荷：守护刚报来的某领域的一份事实。

    ``subject`` 是**领域名**（``route`` / ``tunnel`` / ``hosts``），不是文件路径 ——
    路径在 2026-09-29 之后彻底离开了两侧的约定。``observed`` 是守护侧那份观测的副本
    （可能为空 dict ＝「还没有事实」，**不等于**「一切正常」）。
    """

    subject: str
    observed: Any


class EventBus(QObject):
    _sig = Signal(str, object)  # (topic, payload)

    def __init__(self) -> None:
        super().__init__()
        self._handlers: dict[str, list] = defaultdict(list)
        self._sig.connect(self._dispatch)

    def publish(self, topic: str, payload: object = None) -> None:
        """任意线程可调。"""
        self._sig.emit(topic, payload)

    def subscribe(self, topic: str, handler) -> None:
        self._handlers[topic].append(handler)

    def unsubscribe(self, topic: str, handler) -> None:
        if handler in self._handlers.get(topic, []):
            self._handlers[topic].remove(handler)

    def _dispatch(self, topic: str, payload: object) -> None:
        for handler in self._handlers.get(topic, []):
            try:
                handler(payload)
            except Exception:
                log.exception("事件处理器异常：topic=%s", topic)
