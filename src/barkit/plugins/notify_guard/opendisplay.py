"""notify_guard/opendisplay：OpenDisplay 的**装置事实与动作**单点（设计 §3.3，本批新增）。

与 ``backend.py``（写通知开关）**正交**：本模块只做「读装置事实 + 唤起窗体」，
不参与任何决策，也不写任何状态。零授权、无 helper、无 root——这是本组件的好运气，
别把它搞复杂（设计 §3.3 原话）。

上游事实（``../opendisplay/Mac/`` 只读勘察，2026-09-14）：

| 事实 | 位置 |
|---|---|
| 唤起窗体 = 「再打开一次 App」⇒ ``applicationShouldHandleReopen`` → ``MainWindow.show()`` | ``OpenSidecarMacApp.swift:61-65`` |
| 菜单栏是否占位由 ``presentation`` 决定 | ``:27`` |
| ``presentation`` 取值 ``menuBar`` / ``dock`` / ``background`` | ``:9`` |
| 持久化在 UserDefaults 键 ``presentation``，**读不到即 ``menuBar``** | ``:169-177`` |
| ``running``（= ``!sessions.isEmpty``）**不落盘、无对外出口** | ``:193`` |

最后一行是零改动方案的**天花板**：真正的会话真值读不到（设计 §2.6 挂了上游改动
方案 A/B，本批不做）。本模块给出的 ``app_state()`` 是「装没装 / 跑没跑」的观察反推。

一条纪律：**不在插件里自建 ``subprocess``**——唤起一律走 ``core/proc.run_cmd``（禁 shell、
超时清进程组，``core/proc.py:28-49``）。
"""

from __future__ import annotations

import logging

from barkit.core.log_throttle import log_throttled
from barkit.core.proc import run_cmd

log = logging.getLogger(__name__)

# App 名（``open -a`` 的实参；设计 §2.4 本机实测用这个名称唤起成功）
OPENDISPLAY_APP_NAME = "OpenDisplay"

# bundle id：主构建 + Debug 构建。Debug 是 ``...mac.debug``（上游 project.yml:96），
# 故匹配一律用**前缀**；顺序 = 优先主构建。
OPENDISPLAY_BUNDLE_IDS = (
    "com.peetzweg.opensidecar.mac",
    "com.peetzweg.opensidecar.mac.debug",
)

# 应用态（三级线索的 ①②），取值与 signals 的 display 四态中的三个同名同义
APP_RUNNING = "running"
APP_IDLE = "idle"        # 装了但没跑
APP_ABSENT = "absent"    # 没装 / 读不到

# ``presentation`` 的合法取值（上游 ``:9`` 的 enum rawValue）；读不到即 ``menuBar``
PRESENTATION_MENU_BAR = "menuBar"
PRESENTATION_DOCK = "dock"
PRESENTATION_BACKGROUND = "background"
_PRESENTATIONS = frozenset(
    {PRESENTATION_MENU_BAR, PRESENTATION_DOCK, PRESENTATION_BACKGROUND}
)


def app_state() -> str:
    """线索 ①②：``running`` / ``idle`` / ``absent``（**纯探测，无缓存**）。

    先问「在跑没跑」再问「装没装」：``runningApplications()`` 的前缀匹配对主构建与
    Debug 构建都成立，比精确查 URL 更宽；「装了没跑」才需要按 id 精确查（两个候选
    各查一次）。降频（TTL 缓存）由调用方 ``signals.detect_app_state`` 负责。

    非 macOS 或 PyObjC 不可用 ⇒ ``absent``：这是零 fork 前提下唯一保守且确定的答案
    （读不到就当没装），调用方据此把 display 报成 IDLE 级，而不是撒谎说"已连接"。
    """
    try:
        from AppKit import NSWorkspace
    except Exception:  # noqa: BLE001 —— 无 AppKit 的环境（CI / 非 macOS）走保守分支
        return APP_ABSENT
    try:
        ws = NSWorkspace.sharedWorkspace()
        for app in ws.runningApplications() or []:
            bid = app.bundleIdentifier() or ""
            if any(bid.startswith(base) for base in OPENDISPLAY_BUNDLE_IDS):
                return APP_RUNNING
        for bid in OPENDISPLAY_BUNDLE_IDS:
            if ws.URLForApplicationWithBundleIdentifier_(bid) is not None:
                return APP_IDLE
    except Exception as e:  # noqa: BLE001
        log_throttled(log, logging.WARNING, "OpenDisplay 应用态探测失败：%s", e)
    return APP_ABSENT


class OpenDisplayBackend:
    """唤起窗体 + 只读装置事实（零授权；``presentation`` 零 fork）。"""

    # ---------- 动作 ----------

    def open_app(self, timeout: float = 10) -> bool:
        """``open -a OpenDisplay`` 唤起窗体，返回是否成功（rc == 0）。

        未安装时 ``open`` 返回非 0：**不抛异常**——「未安装」是 display 状态机的一个
        正常取值（``absent``），面板与设置页会明写，无需靠弹窗告知。
        """
        try:
            res = run_cmd(["open", "-a", OPENDISPLAY_APP_NAME], timeout=timeout)
        except Exception as e:  # noqa: BLE001 —— 超时 / 可执行缺失都不该冒泡到 UI 线程
            log.warning("唤起 OpenDisplay 失败：%s", e)
            return False
        if res.returncode != 0:
            log.warning(
                "唤起 OpenDisplay 失败（rc=%s）：%s",
                res.returncode, (res.stderr or "").strip(),
            )
            return False
        log.info("已唤起 OpenDisplay 窗体")
        return True

    # ---------- 只读事实 ----------

    def presentation(self) -> str:
        """读 ``presentation``（``menuBar`` / ``dock`` / ``background``）。

        ``CFPreferencesCopyAppValue`` 是进程内调用（零 fork）。读不到——**本机实测该键
        从不存在**（说明用户从未改过）——或值非法 ⇒ ``menuBar``，与上游 ``:169-170``
        的默认值一致；非 macOS / PyObjC 不可用同样回落 ``menuBar``（保守：不谎报"已让位"）。
        """
        for bid in OPENDISPLAY_BUNDLE_IDS:
            value = self._pref(bid, "presentation")
            if isinstance(value, str) and value in _PRESENTATIONS:
                return value
        return PRESENTATION_MENU_BAR

    def is_in_menu_bar(self) -> bool:
        """是否仍在菜单栏占位（``presentation != "background"``）。

        设置页据此给一条**只读提示**（设计 §2.5）：`BarKit 不代写别人的偏好`——
        改 ``presentation`` 必须重启 OpenDisplay（当前推流会断一次），且 Debug 构建是
        另一个 bundle id；让用户在原 App 的 Picker 里自己改一次，零越界。
        """
        return self.presentation() != PRESENTATION_BACKGROUND

    @staticmethod
    def _pref(app_id: str, key: str):
        """读某 App 域下的偏好值（失败返回 None，绝不抛）。"""
        try:
            from CoreFoundation import CFPreferencesCopyAppValue
        except Exception:  # noqa: BLE001
            return None
        try:
            return CFPreferencesCopyAppValue(key, app_id)
        except Exception as e:  # noqa: BLE001
            log.warning("读取 %s 的 %s 失败：%s", app_id, key, e)
            return None
