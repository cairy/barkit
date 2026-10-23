"""shell/macos_notify：macOS 通知后端（``NSUserNotificationCenter``）。

**为什么不用新的 ``UNUserNotificationCenter``**（2026-09-22 实测结论，别重走）：

- 它是 ``UserNotifications.framework`` 里的类，PyObjC **没有**为它准备元数据，所以那些
  ``completionHandler:`` 参数（``requestAuthorizationWithOptions:`` 等）在 PyObjC 眼里是
  「block 但没有签名」，直接 ``BadPrototypeError``；
- 手工补 ``objc.registerMetaDataForSelector`` 能过第一关，但 PyObjC 对动态加载的类
  会**优先使用它自己算出来的 ``full_signature``**（实测拿到的是退化的 ``b'@@@'``），
  指数组与显式签名两条路都试过，都没能让它接受「两个参数的 block」；
- 而 ``NSUserNotificationCenter`` PyObjC **有完整绑定**，实测投递成功、
  delegate 回调正常、两枚按钮都在。代价是它已标记废弃——所以本模块把「后端」隔离成
  一层：将来 UN 若变得可用（或打包成真 .app 后改用 UN），换的是这一个文件。

**bundle 身份是硬前提**（实测）：bundle id 为 ``org.python.python`` 时 ``deliverNotification:``
**静默不投递**（连 ``didDeliver`` 都不回调）；把 ``CFBundleIdentifier`` 换成 BarKit 自己的
之后立刻正常。所以本模块会**只在自己没有身份时**补一个（见 ``_ensure_bundle_identity``）——
不动真 .app 自带的 bundle id。

**线程**：``NSUserNotificationCenter`` 必须在主线程投递。``send()`` 可以在任意线程调用，
跨线程时经 Qt 信号排队回主线程（与 ``core/events.py`` 同一套机制）。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, Signal

from barkit.paths import APP_NAME

log = logging.getLogger(__name__)

#: BarKit 通知的归属身份。**只在运行进程尚无自己的 bundle 身份时**才写入。
BUNDLE_ID = "com.barkit.menubar"

# NSUserNotificationActivationType（AppKit 枚举）
_ACTIVATION_CONTENTS = 1          # 点了通知本体
_ACTIVATION_ACTION_BUTTON = 2     # 点了 actionButtonTitle
_ACTIVATION_ADDITIONAL = 4        # 点了 otherButtonTitle / 附加动作


def _load_appkit():
    """延迟导入 PyObjC：非 macOS 或没装 PyObjC 时返回 None，而不是让 import 期炸掉。"""
    try:
        import objc
        from Foundation import NSBundle
        from AppKit import NSUserNotification, NSUserNotificationCenter
    except Exception:  # noqa: BLE001 —— 导入失败即「本平台没有这个后端」
        log.debug("macOS 通知后端不可用（PyObjC 未安装）", exc_info=True)
        return None
    return objc, NSBundle, NSUserNotification, NSUserNotificationCenter


class MacNotifyBackend(QObject):
    """把 ``NotificationRequest`` 投成一条系统通知，并把用户点击送回插件。

    「一次投递」的状态（通知 key、按钮映射）记在 ``self._pending`` 里：用户在几小时后
    才点按钮也是常态，所以这份映射要一直留着，直到该 key 被新的通知覆盖。
    """

    _enqueue = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._api = _load_appkit()
        self._delegate = None
        self._center = None
        self._on_action = None
        #: 通知 key → (标题, 按钮 key 列表)，用户点击时按顺序把索引翻译回动作名。
        self._pending: dict[str, tuple[str, list[str]]] = {}
        self._identity_fixed = False
        self._enqueue.connect(self._deliver)

    # ---------- 后端协议 ----------

    def available(self) -> bool:
        """本平台此刻能不能发通知：有 PyObjC、能拿到通知中心，就算可用。

        必须先补 bundle 身份再取通知中心——否则中心会缓存 ``org.python.python``
        这个身份，之后改写 ``CFBundleIdentifier`` 已无效，系统静默丢弃通知
        （这正是「从源码运行」时收不到通知的根因）。
        """
        if self._api is None:
            return False
        if self._center is not None:
            return True
        _objc, NSBundle, _NSUN, NSUserNotificationCenter = self._api
        info = NSBundle.mainBundle().infoDictionary()
        if info is None:
            # 拿不到 infoDictionary 就没法给通知一个归属身份，投了也是白投（实测）
            return False
        if not self._ensure_bundle_identity():
            return False
        try:
            self._center = NSUserNotificationCenter.defaultUserNotificationCenter()
        except Exception:  # noqa: BLE001
            log.exception("取 NSUserNotificationCenter 失败")
            return False
        return self._center is not None

    def send(self, request, on_action) -> bool:
        """投递一条通知。

        在**主线程**调用时同步投递、返回真实结果；在 worker 线程（``tick()`` 里）调用
        时经信号排队回主线程，返回 ``True`` 表示「已受理」——投递结果以日志为准。
        """
        if not self.available():
            return False
        self._on_action = on_action
        if QThread.currentThread() is self.thread():
            return self._deliver(request)
        self._enqueue.emit(request)
        return True

    # ---------- 主线程侧 ----------

    def _deliver(self, request) -> bool:
        """真正把通知交给系统（必须在主线程）。"""
        objc, NSBundle, NSUserNotification, _NSUNC = self._api
        if not self._ensure_bundle_identity():
            return False
        if self._delegate is None:
            self._delegate = _make_delegate(objc, self)
            self._center.setDelegate_(self._delegate)

        note = NSUserNotification.alloc().init()
        note.setTitle_(request.title)
        if request.body:
            note.setInformativeText_(request.body)
        # 第一枚走 actionButton、第二枚走 otherButton —— 顺序即契约（见 notify.py 的
        # MAX_ACTIONS）；多出来的在契约层已被拒，这里不必再管。
        if request.actions:
            note.setHasActionButton_(True)
            note.setActionButtonTitle_(request.actions[0].label)
            if len(request.actions) > 1:
                note.setOtherButtonTitle_(request.actions[1].label)
        else:
            note.setHasActionButton_(False)

        self._pending[request.key] = (request.title,
                                      [a.key for a in request.actions])
        # ``NSUserNotification`` 没有可写的 identifier（那是新 UNNotification 才有的），
        # 所以归属 key 走 userInfo —— delegate 回调时从它读回来。
        note.setUserInfo_({"bk_key": request.key})
        try:
            self._center.deliverNotification_(note)
        except Exception:  # noqa: BLE001
            log.exception("投递通知「%s」失败", request.title)
            return False
        return True

    def _ensure_bundle_identity(self) -> bool:
        """给进程补一个 bundle 身份 —— **只在自己没有的时候**。

        纯 Python 进程的 bundle id 是 ``org.python.python``，此时系统**不会投递**通知
        （实测：连 ``didDeliver`` 都不回调）。已打包成 .app 时自带 id，这里一概不动。

        **副作用要知情**：macOS 的隐私授权（辅助功能 / 麦克风等）按 bundle 身份归属。
        改身份意味着 BarKit 从「Python 这个集合身份」变成「BarKit 自己的身份」——
        首次改动后，之前授予的权限可能要在系统设置里重新勾一次。这是「能发通知」的
        代价，故只做一次、且只在不覆盖真身份的前提下做。
        """
        if self._identity_fixed:
            return True
        _objc, NSBundle, _NSUN, _NSUNC = self._api
        info = NSBundle.mainBundle().infoDictionary()
        if info is None:
            return False
        current = info.get("CFBundleIdentifier") or ""
        if current and not current.startswith("org.python"):
            self._identity_fixed = True        # 已有自己的身份（.app 打包态），尊重它
            return True
        try:
            info["CFBundleIdentifier"] = BUNDLE_ID
            info["CFBundleName"] = APP_NAME
        except Exception:  # noqa: BLE001
            log.exception("改写 bundle 身份失败，通知可能不会显示")
            return False
        log.info("已为通知设置 bundle 身份：%s → %s", current, BUNDLE_ID)
        self._identity_fixed = True
        return True

    # ---------- delegate 回程 ----------

    def _handle_activation(self, note) -> None:
        """用户与通知交互（主线程）：把按钮翻译成插件认识的**动作名**投回去。"""
        try:
            kind = note.activationType()
        except Exception:  # noqa: BLE001
            log.exception("读取通知激活类型失败")
            return
        if kind == _ACTIVATION_CONTENTS:
            return                       # 点了本体（只是把通知点掉），不是动作
        # 归属 key 从 userInfo 读回来（PyObjC 已把 NSDictionary 桥接成 dict）
        key = None
        try:
            info = note.userInfo()
            key = info.get("bk_key") if info else None
        except Exception:  # noqa: BLE001
            log.exception("读取通知 userInfo 失败")
        if not key:
            log.info("收到通知交互但认不出是哪一条（activationType=%s）", kind)
            return
        entry = self._pending.get(key)
        if entry is None:
            log.info("收到通知交互 %s，但它已被更新的通知覆盖", key)
            return
        _title, action_keys = entry
        if kind == _ACTIVATION_ACTION_BUTTON:
            index = 0
        elif kind == _ACTIVATION_ADDITIONAL:
            index = 1
        else:
            log.info("未识别的通知交互类型 %s（key=%s）", kind, key)
            return
        if index >= len(action_keys):
            return
        action_key = action_keys[index]
        log.info("用户点了通知「%s」上的第 %d 枚按钮 → 动作 %s", _title, index + 1, action_key)
        if self._on_action is not None:
            self._on_action(key, action_key)


def _make_delegate(objc, backend: MacNotifyBackend):
    """造 delegate 对象。

    PyObjC 的 delegate 必须由我们**持引用**，否则会被 GC 掉、回调再也不来
    （``backend._delegate`` 保存它，所以这里只负责造）。
    """
    NSObject = objc.lookUpClass("NSObject")

    class _Delegate(NSObject):
        def userNotificationCenter_didDeliverNotification_(self, center, note):
            log.debug("通知已投递")

        def userNotificationCenter_didActivateNotification_(self, center, note):
            backend._handle_activation(note)
            center.removeDeliveredNotification_(note)

        def userNotificationCenter_shouldPresentNotification_(self, center, note):
            # 返回 True：BarKit 常在前台（菜单栏常驻），不这样的话通知会被自动压掉
            return True

    return _Delegate.alloc().init()
