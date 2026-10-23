"""bt_guard/bt_state：蓝牙现场读取 ＋ 助手调用（纯功能模块，无界面、无组件状态）。

**判据读两层，别只读一层**（两层的读法都在本模块）：

- **蓝牙层**（``system_profiler SPBluetoothDataType -json``）：控制器开没开、设备在不在
  已配对列表、哪些设备处于已连接。它回答「蓝牙栈怎么看」。
- **HID 层**（``ioreg -a -c IOHIDDevice``）：系统有没有把这只设备当成输入设备在用。

主判据取 **HID 层**（``Snapshot.present()``）：用户能感觉到的是「鼠标能不能动」，那是
HID 在收输入，不是蓝牙连接表的状态。2026-09-23 的现场实测里两层是一致的（故障态＝
蓝牙层未连 ＋ HID 无枚举；正常态＝都已连），所以取它的理由不是「更准」而是「**直接对应
症状**」—— 报出去的那一句「设备在不在」和用户手上的感觉是同一件事。

**只读纪律**：``probe()`` / ``bluetooth_layer()`` / ``hid_layer()`` 一律不发动作。全模块
唯一的写操作是 :func:`restart_controller`，且它保证「无论中途怎么失败，最后都把蓝牙
开着」—— 把用户的蓝牙留在关着的状态，是这条路上唯一真正危险的失败形态。它经
:func:`spawn_helper` 拉起那个随包助手，在**一条本机会话**里下发命令（会话那一层在
``bt_session``，一次动手 = 一个助手进程 = 一条会话）。
"""

from __future__ import annotations

import json
import logging
import plistlib
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from barkit.core.log_throttle import log_throttled

from . import bt_session

log = logging.getLogger(__name__)

#: 全部走绝对路径：``ioreg`` 在 ``/usr/sbin``，而目标机的 PATH 不由我们决定。
SYSPROF = "/usr/sbin/system_profiler"
IOREG = "/usr/sbin/ioreg"
OPEN = "/usr/bin/open"

#: HID 层探针的命令。**不许再给它加按值的过滤**（曾经的 ``-k DeviceAddress`` 就是这么坏的）。
#:
#: ``ioreg`` 的 ``-c`` 与 ``-k`` 是**与**关系 ⇒ 根集合 = 「class 是 ``IOHIDDevice`` **且**
#: 带那个键的对象」。而「设备不在场」正好让这个集合为空，此时 ``ioreg`` **一个字节都不输出**
#: （退出码仍是 0）—— 在 ``plistlib`` 眼里与「命令半路没写完」完全同形。2026-10-01 实测：
#: 带 ``-k`` 跑 40 次、40 次解析失败且输出恒为 0 字节；去掉它 40 次全成、2.53MB。
#: 后果不是「偶尔读不出」，是**在本组件唯一该动手的现场下把自己判成读不出来、绝不动手**。
#:
#: ``-r -d 1`` 才是省量的那一项（``-a -c IOHIDDevice`` 6.0MB → 2.53MB、约 0.28 秒，
#: ``Transport`` / ``Product`` / ``DeviceAddress`` 照样在里面）。别再把它和 ``-k`` 的功劳
#: 混在一起记账。
IOREG_HID_ARGV = [IOREG, "-a", "-c", "IOHIDDevice", "-r", "-d", "1"]

#: 单次只读命令的超时。两条命令实测都在亚秒级（蓝牙层约 0.06s，HID 层约 0.3s），
#: 给 10 秒是防「系统正忙」时把 worker 线程拖住。
READ_TIMEOUT_S = 10

#: 断电与重开之间的停顿：让蓝牙栈真的把控制器拆完再重建。
POWER_CYCLE_GAP_S = 2.0

#: 一次断电重开的两条命令，**顺序即执行顺序**。写成常量是因为「两条都要跑、顺序不能反」
#: 是这条路的语义（``bt_session`` 按这个顺序下发；``power-off`` 失败也要接着 ``power-on``）。
_POWER_CYCLE = ("power-off", "power-on")

#: 重开后等设备回到 HID 层的上限。实测整轮恢复 8.1 秒，给足余量。
RECOVER_WAIT_S = 60


def norm(addr) -> str:
    """蓝牙地址归一：去掉分隔符、转大写。

    跨接口比对**必须**走它 —— ``system_profiler`` 与 ``ioreg`` 给出的地址格式不同
    （一个带冒号、一个不带），不归一化就会出现「同一个设备被当成两台」。
    """
    return str(addr or "").upper().replace("-", "").replace(":", "")


def pretty(addr: str) -> str:
    """归一化地址 → ``XX:XX:XX:XX:XX:XX``（给人和给诊断文本看）。"""
    n = norm(addr)
    if len(n) != 12:
        return addr or "（未设置）"
    return ":".join(n[i:i + 2] for i in range(0, 12, 2))


def _run(argv: list[str], timeout_s: float = READ_TIMEOUT_S) -> bytes | None:
    """跑一条只读命令，返回 stdout；非零退出 / 起不来 / 超时一律返回 None。"""
    try:
        done = subprocess.run(argv, capture_output=True, timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired):
        log.debug("只读命令失败：%s", argv, exc_info=True)
        return None
    return done.stdout if done.returncode == 0 else None


# ──────────────────────────────────────────────
# 两层现场
# ──────────────────────────────────────────────

def bluetooth_layer() -> tuple[dict, dict]:
    """蓝牙层：``({归一化地址: {"name", "connected"}}, 控制器属性)``。

    读不到时返回两个空字典（调用方据「都空」判探针失败，见 :func:`probe`）。
    """
    out = _run([SYSPROF, "SPBluetoothDataType", "-json"])
    if out is None:
        return {}, {}
    try:
        data = json.loads(out.decode("utf-8", "replace"))["SPBluetoothDataType"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        return {}, {}

    devices: dict[str, dict] = {}
    for key, connected in (("device_connected", True), ("device_not_connected", False)):
        for group in (data.get(key) or []):
            if not isinstance(group, dict):
                continue
            for name, info in group.items():
                if not isinstance(info, dict):
                    continue
                addr = norm(info.get("device_address"))
                if addr:
                    devices[addr] = {"name": name, "connected": connected}
    return devices, (data.get("controller_properties") or {})


def _ioreg_items() -> tuple[object, bool]:
    """跑 ``ioreg`` 并解析成对象；返回 ``(对象, 是否读成功)``。

    **唤醒后 IORegistry 正处于变动期**，``ioreg`` 偶发会吐出读不出完整 plist 的东西
    （退出码是 0）。这种**瞬态读不出**绝不能当成「设备不在」去断电重开（那会无端打断
    AirPods 之类当前连着的设备），所以这里重试几次兜住；都失败才把第二元置 ``False``，
    让上层按「读不出来」处理（见 :meth:`Snapshot.hid_ok` 与 ``plugin`` 的判定）。

    **但「读到了一份里面没有蓝牙设备的现场」不是读不出**：本命令不带按值的过滤，只要
    机器上还有 HID 对象（任何一台 Mac 都有，内建键盘就在其中）就必然有输出 ⇒ 拿到**空
    字节流**才是「命令半路没写完」那一档。这两件事分不开的时候，判据在本组件唯一该动手
    的现场下只能永远报「读不出来」—— 所以命令本身不许按值过滤，见 :data:`IOREG_HID_ARGV`。
    """
    for attempt in range(3):
        out = _run(IOREG_HID_ARGV)
        if out is None:
            continue                                # 命令本身没跑成，重试
        try:
            return plistlib.loads(out), True
        except Exception:                          # noqa: BLE001 —— 输出不是合法 plist
            log.debug("解析 ioreg 输出失败（第 %d/%d 次），重试", attempt + 1, 3,
                      exc_info=True)
            time.sleep(0.3)
    log_throttled(log, logging.WARNING, "ioreg 多次都解析不出合法 plist，HID 层本次读不出来")
    return None, False


def _parse_hid(items) -> dict:
    """把 ioreg 解析出的对象收成 ``{归一化地址: 产品名}``（只收蓝牙传输的）。"""
    if items is None:
        return {}
    if isinstance(items, dict):
        items = [items]
    found: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        if "Bluetooth" not in str(item.get("Transport", "")):
            continue
        addr = norm(item.get("DeviceAddress"))
        if addr:
            found[addr] = item.get("Product", "?")
    return found


def hid_layer() -> dict:
    """HID 层：``{归一化地址: 产品名}``，只收 ``Transport`` 含 Bluetooth 的设备。

    注意 ``ioreg -a`` 产出的是 **plist 而不是 JSON**（同一个 ``-a`` 开关在两个命令上
    是两种格式）—— 用 ``plistlib`` 解析，用 ``json`` 会静默失败。

    ``-r -d 1`` **不是可省的**：不带它时 ``ioreg`` 会把每个对象**及其整棵子树**都吐出来
    （实测 6.0MB、0.6 秒）；带上它只剩每个对象自己的属性表，2.53MB、约 0.28 秒，而
    ``Transport`` / ``Product`` / ``DeviceAddress`` 照样在里面。这条探针在恢复期每 1 秒
    被调一次（见 :func:`wait_present`），所以这半个机制是「唤醒后别把机器再压一轮」的
    实打实的收益。

    **别再加按值的过滤**（``-k``）：省下的量不由它贡献，而它会让「设备不在场」这一档
    连一个字节都收不到 —— 见 :data:`IOREG_HID_ARGV`。

    **空字典 ≠ 「没有设备」**：读不出时本函数也返回 ``{}``（见 :func:`_ioreg_items`）。
    要在「掉线就动手」这种破坏性判定里用它，必须先看 :meth:`Snapshot.hid_ok`——
    读不出来就别动手。
    """
    items, _ = _ioreg_items()
    return _parse_hid(items)


@dataclass(frozen=True)
class Snapshot:
    """一份现场。``ok=False`` 表示这次**根本没读到**（不是「读到了、设备不在」）。

    ``hid_ok=False`` 是另一回事：蓝牙层读到了，但 **HID 主判据读不出来**（ioreg 解析
    失败）。它**不是**「设备不在」—— 是「无权判定它掉了」。破坏性动作（断电重开）必须
    以 ``hid_ok`` 为真前提，否则会把一次读不出当成掉线、无端打断当前连着的设备。
    """

    ok: bool
    reason: str = ""
    controller_on: bool | None = None
    devices: dict = field(default_factory=dict)
    hid: dict = field(default_factory=dict)
    hid_ok: bool = True

    def present(self, addr: str) -> bool:
        """目标设备现在能用吗 —— 主判据（HID 层，见模块 docstring）。"""
        return norm(addr) in self.hid

    def bluetooth_connected(self, addr: str) -> bool:
        """蓝牙层认为它连着吗 —— 只进诊断文本，不参与判定。"""
        entry = self.devices.get(norm(addr))
        return bool(entry and entry["connected"])

    def paired_name(self, addr: str) -> str | None:
        """它在已配对列表里的名字；不在列表里返回 None。"""
        entry = self.devices.get(norm(addr))
        return entry["name"] if entry else None

    def connected_names(self) -> list[str]:
        """当前所有已连设备的名字（诊断用：重开蓝牙会打断的那一批）。"""
        return sorted(d["name"] for d in self.devices.values() if d.get("connected"))


def probe() -> Snapshot:
    """读一次现场。**只读，零副作用。**"""
    devices, controller = bluetooth_layer()
    items, hid_ok = _ioreg_items()
    hid = _parse_hid(items)
    if not devices and not controller:
        return Snapshot(False, "system_profiler 没给出蓝牙数据")

    state = str(controller.get("controller_state", "") or "")
    on: bool | None = None
    if state == "attrib_on":
        on = True
    elif state == "attrib_off":
        on = False
    return Snapshot(True, "", on, devices, hid, hid_ok=hid_ok)


def paired_devices() -> list[tuple[str, str]]:
    """已配对设备清单 ``[(归一化地址, 名字)]``，按**地址**排序；读不到返回空列表。

    设置页用它填「要看的设备」那个下拉 —— 让用户手抄一串十六进制地址是没人会用的。
    """
    devices, _ = bluetooth_layer()
    return sorted((addr, d.get("name") or addr) for addr, d in devices.items()
                  if d.get("name"))


# ──────────────────────────────────────────────
# 助手（唯一的写路径）
# ──────────────────────────────────────────────

def app_path() -> Path:
    """助手 ``.app`` 的绝对路径：就在本插件包的 ``resources/`` 下，随包分发。"""
    return Path(__file__).resolve().parent / "resources" / "BTHelper.app"


def ensure_executable() -> bool:
    """确保助手主二进制带可执行位；缺了就地补上。

    **为什么需要这一步**：wheel 打包会静默丢掉可执行位 —— 实测 zip 条目里**记着**
    ``0o100755``，解出来落盘却是 ``0o644``。本机是开发态安装（直接指向源码目录），
    所以看不出来；真装到别的机器上就是「助手起不来」。文件在这个目录里归用户所有，
    补权限免 root；签名覆盖的是**内容**而不是权限位，所以改权限不会让签名失效。
    """
    binary = app_path() / "Contents" / "MacOS" / "bt-helper"
    try:
        if binary.stat().st_mode & 0o111:
            return True
        binary.chmod(0o755)
        return bool(binary.stat().st_mode & 0o111)
    except OSError:
        log.exception("给助手补可执行位失败：%s", binary)
        return False


def spawn_helper() -> bool:
    """经 ``open`` 把助手 ``.app`` 拉起来。返回是否成功「发起了拉起」。

    **不能直接 exec 它里面那个二进制**：蓝牙私有 API 认的是「躺在 bundle 里、且经
    LaunchServices 拉起」这个身份，直接 exec 会 SIGABRT（``resources/README.md``
    里那张实测表）。``open --args`` 在本机实测五种姿势**全部丢参**，所以这条路也不
    传参 —— 助手需要知道什么，app 在会话里发帧告诉它（见 ``bt_session``）。

    **顺序要紧**：调用方必须先建好会话节点再调本函数（``bt_session`` 是这么做的）。
    反过来的话助手一起来就 connect，而此刻没人在 listen —— 它只重试 5 秒。
    """
    if not ensure_executable():
        return False
    try:
        subprocess.run([OPEN, "-n", str(app_path())], check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        log.exception("拉起蓝牙助手失败：%s", app_path())
        return False
    return True


def wait_present(addr: str, timeout_s: float = RECOVER_WAIT_S) -> bool:
    """等目标设备在 HID 层出现；出现与超时取先到。"""
    want = norm(addr)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if want in hid_layer():
            return True
        time.sleep(1.0)
    return want in hid_layer()


def restart_controller(addr: str) -> tuple[bool, str]:
    """控制器断电重开，然后等目标设备回到 HID 层。返回 ``(是否回来, 给用户看的一句话)``。

    **这是本组件唯一的动作，也只有这一档**：单设备重连（``connect`` / ``disconnect``）
    在本机故障态下实测全返回 ``kIOReturnError``、HID 层 20 秒零变化，对「蓝牙层未连 ＋
    HID 无枚举」这个形态无效。控制器断电重开实测 8.1 秒恢复、配对无损、免 root。

    **两条命令走同一条会话**：``power-off`` → 静默 :data:`POWER_CYCLE_GAP_S` → ``power-on``
    —— 一个助手进程、一条会话（见 ``bt_session``）。旧形态下那是两次 ``open``、
    两次冷启动（实测冷启动可到 2 秒以上）。

    **保证最终是开着的**：``power-off`` 那一步成没成都继续走 ``power-on``；两条命令之间
    会话若断掉，``bt_session`` 会重建一条会话把 ``power-on`` 跑完 —— 正是为了保住这一条
    （把用户的蓝牙留在关着的状态，是这条路上唯一真正危险的失败形态）。注意副作用
    —— 这一档会**打断所有**蓝牙设备，不只目标那一个，所以它只在一轮唤醒里做一次。
    """
    off, on = bt_session.run_commands(
        _POWER_CYCLE, spawn=spawn_helper, gap_s=POWER_CYCLE_GAP_S)
    if off is None or off.get("code") != 0:
        # 关没关成不影响下一步：接着开就是。这里只留痕，不上报成故障 ——
        # 真正的判据是「设备最后回没回来」。但它是**失败**（不是信息），故 warning。
        log_throttled(log, logging.WARNING,
                      "助手 power-off 未确认成功（%s）", off and off.get("lines"))
    if on is None or on.get("code") != 0:
        return False, "助手没能把蓝牙重新打开，请手动检查"

    if wait_present(addr, RECOVER_WAIT_S):
        return True, "已重开蓝牙，设备回来了"
    return False, f"蓝牙重开过了，但 {RECOVER_WAIT_S:.0f} 秒内没等到设备回来"
