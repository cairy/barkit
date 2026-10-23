"""探针：浮层「一显示」是否抢走前台 app 的焦点（真机 cocoa 下跑）。

背景与结论 → `docs/log/2026-10-15-浮层显示不抢焦点.md`。臂位：

- A 普通 ``Qt.Window`` 顶层窗 —— 对照组，**预期抢**（自证判据有效）
- E 裸 Tool 窗：只 ``show()`` —— 对照组，**Regular 策略下预期抢**（证明 ``show()`` 本身会激活）
- F 裸 Tool 窗：``show()`` ＋ ``raise_()`` —— 对照组，**预期抢**（``raise_`` 是当年的元凶）
- B 现有 ``Overlay.show_and_run`` —— 被测对象，预期不抢（两道口都堵上之后）
- C ``Overlay`` ＋ ``WA_ShowWithoutActivating``（show 之前设）
- D ``Overlay`` ＋ 显示前先 ``apply_nonactivating``（show 之前打 mask）
- G 裸 Tool 窗：``show()`` ＋ 原生 ``orderFront``（不打 mask）
- H 修法链路：``show`` ＋ 原生 ``orderFront`` ＋ 三件套 ＋ 落位
- J 拟修法二：显示**前**先打 mask（第一次上屏就是 nonactivating panel）

每臂前都把前台切到别的 app（Finder），再看显示后 1.5s 内前台 / ``NSApp.isActive`` /
key 窗怎么变，**按变化点记轨迹**。对照组没抢到 ⇒ 判据在本环境无效（SELFTEST FAILED）。

用法::

    PYTHONPATH= .venv/bin/python scripts/probe_overlay_focus.py --selftest   # 只跑 A，须抢到
    PYTHONPATH= .venv/bin/python scripts/probe_overlay_focus.py --policy regular --arms E,B
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QWidget  # noqa: E402

from AppKit import NSApplication, NSWorkspace  # noqa: E402

ME = "org.python.python"
FINDER = "com.apple.finder"


def pump(ms: int) -> None:
    end = time.time() + ms / 1000.0
    while time.time() < end:
        QApplication.processEvents()
        time.sleep(0.005)


def front_bundle() -> str:
    a = NSWorkspace.sharedWorkspace().frontmostApplication()
    if a is None:
        return "?"
    return str(a.bundleIdentifier() or a.localizedName() or "?")


def snap() -> dict:
    app = NSApplication.sharedApplication()
    kw = app.keyWindow()
    return {
        "active": bool(app.isActive()),
        "front": front_bundle(),
        "key": int(kw.windowNumber()) if kw is not None else None,
    }


def cg_win(num: int | None) -> dict | None:
    if num is None:
        return None
    try:
        import Quartz

        infos = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID
        )
        for i in infos or ():
            if int(i.get(Quartz.kCGWindowNumber, -1)) == num:
                b = i.get(Quartz.kCGWindowBounds) or {}
                return {
                    "layer": int(i.get(Quartz.kCGWindowLayer, -1)),
                    "onscreen": i.get(Quartz.kCGWindowIsOnscreen),
                    "x": int(round(float(b.get("X", 0)))),
                    "y": int(round(float(b.get("Y", 0)))),
                }
    except Exception as e:  # noqa: BLE001
        return {"err": str(e)}
    return None


def native_num(w) -> int | None:
    from barkit.shell.macos_window import nswindow_of

    nw = nswindow_of(w)
    return int(nw.windowNumber()) if nw is not None else None


def native_order_front(w) -> bool:
    """原生 ``orderFront:`` —— ``raise_()`` 内部干的第一件事，但不激活进程。"""
    from barkit.shell.macos_window import nswindow_of

    win = nswindow_of(w)
    if win is None:
        return False
    win.orderFront_(win)
    return True


def make_overlay(arm: str):
    from barkit.shell.overlay import Overlay

    class Probe(Overlay):
        def __init__(self) -> None:
            super().__init__(size=(240, 90))
            self.content_layout.addWidget(QLabel("probe", self))

    ov = Probe()
    if arm == "C":
        ov.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    if arm == "D":
        from barkit.shell.macos_window import apply_nonactivating

        print("   D: 显示前打 mask =", apply_nonactivating(ov))
    return ov


def make_plain(kind: str) -> QWidget:
    """对照组的两种裸窗：``window``（普通顶层窗）／ ``tool``（浮层同款 flags，不走 Overlay 类）。"""
    if kind == "window":
        w = QWidget(None, Qt.WindowType.Window)
    else:
        w = QWidget(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        w.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
    w.setFixedSize(240, 90)
    return w


def give_away_front(timeout_s: float = 3.0) -> bool:
    """把前台让给「不是自己」的 app，作为测量前置。

    ``NSRunningApplication.activateWithOptions_`` 实测无效（返回 True 但前台没换）；
    ``osascript`` 有效但生效是异步的，故轮询。判据两条：前台不是本进程，
    且 ``NSApp.isActive()`` 为假。本机常态前台是华为桌面云客户端，它同样满足前置。
    """
    import subprocess

    subprocess.run(
        ["osascript", "-e", 'tell application "Finder" to activate'],
        capture_output=True,
        check=False,
    )
    end = time.time() + timeout_s
    while time.time() < end:
        pump(50)
        if front_bundle() != ME and not snap()["active"]:
            return True
    return False


def trace(ms: int, step: int = 40) -> list[dict]:
    """按变化点记轨迹：只在 active / front / key 任一变化时追加一条。"""
    out: list[dict] = []
    last = None
    t0 = time.time()
    while (time.time() - t0) * 1000 < ms:
        pump(step)
        s = snap()
        cur = (s["active"], s["front"], s["key"])
        if cur != last:
            out.append(
                {"t": int((time.time() - t0) * 1000), "active": cur[0], "front": cur[1], "key": cur[2]}
            )
            last = cur
    return out


def run_arm(arm: str) -> dict:
    ready = give_away_front()
    before = snap()
    if arm == "Q":  # 空臂：regular 策略下**不显示任何窗**，只等 —— 排除「策略切换本身会激活」
        num = None
        tr = trace(1500)
        return {
            "arm": arm,
            "前置成立": ready,
            "显示前": before,
            "轨迹": tr,
            "win_number": num,
            "cg": None,
            "抢了激活": any(p["active"] and p["front"] == ME for p in tr),
            "成了 key": False,
        }
    if arm == "A":
        w = make_plain("window")
        w.show()
    elif arm == "E":  # 裸 Tool 窗：只 show()（对照组，预期 Regular 策略下抢）
        w = make_plain("tool")
        w.show()
    elif arm == "F":  # 裸 Tool 窗：show() + raise_()（对照组，预期都抢）
        w = make_plain("tool")
        w.show()
        w.raise_()
    elif arm == "G":  # 裸 Tool 窗：show() + 原生 orderFront（不打 mask）
        w = make_plain("tool")
        w.show()
        print("   G: 原生 orderFront =", native_order_front(w))
    elif arm == "J":  # 拟修法二：显示**前**先打 mask（第一次上屏就是 nonactivating panel）
        from barkit.shell import placement
        from barkit.shell.macos_window import apply_nonactivating, make_floating_topmost

        w = make_overlay("J")
        print("   J: 显示前打 mask =", apply_nonactivating(w))
        w.show()
        make_floating_topmost(w)
        pt, _ = placement.place_window(w, placement.Intent.CENTER_TOP)
        placement.verify_placement(w, pt)
    elif arm == "K":  # 分离变量：只 show()（构造期属性已在位），不碰任何原生调用
        w = make_overlay("K")
        w.show()
    elif arm == "M":  # 预造原生窗再打 mask：winId → apply_nonactivating → show → 三件套
        from barkit.shell import placement
        from barkit.shell.macos_window import apply_nonactivating, make_floating_topmost

        w = make_overlay("M")
        w.winId()  # 强制先造出原生 NSWindow，mask 才有落点
        print("   M: 显示前打 mask =", apply_nonactivating(w))
        w.show()
        make_floating_topmost(w)
        pt, _ = placement.place_window(w, placement.Intent.CENTER_TOP)
        placement.verify_placement(w, pt)
    elif arm == "N":  # 分离：winId → mask → 只 show()，**不**跑 make_floating_topmost
        from barkit.shell.macos_window import apply_nonactivating

        w = make_overlay("N")
        w.winId()
        print("   N: 显示前打 mask =", apply_nonactivating(w))
        w.show()
    elif arm == "P":  # 分离：winId → mask → 只原生 orderFrontRegardless，**不**走 Qt show()
        from barkit.shell.macos_window import apply_nonactivating, nswindow_of

        w = make_overlay("P")
        w.winId()
        print("   P: 显示前打 mask =", apply_nonactivating(w))
        nw = nswindow_of(w)
        print("   P: orderFrontRegardless =", bool(nw.orderFrontRegardless()) if nw else None)
    elif arm == "H":  # 修法链路：show + 原生 orderFront + 三件套 + 落位
        from barkit.shell import placement
        from barkit.shell.macos_window import make_floating_topmost

        w = make_overlay("H")
        w.show()
        print("   H: 原生 orderFront =", native_order_front(w))
        make_floating_topmost(w)
        pt, _ = placement.place_window(w, placement.Intent.CENTER_TOP)
        placement.verify_placement(w, pt)
    else:
        w = make_overlay(arm)
        w.show_and_run()
    num = native_num(w)
    tr = trace(1500)
    stole = any(p["active"] and p["front"] == ME for p in tr)
    became_key = any(p["key"] == num for p in tr)
    if hasattr(w, "dismiss"):
        w.dismiss("probe")
    else:
        w.hide()
        w.deleteLater()
    pump(400)
    return {
        "arm": arm,
        "前置成立": ready,
        "显示前": before,
        "轨迹": tr,
        "win_number": num,
        "cg": cg_win(num),
        "抢了激活": stole,
        "成了 key": became_key,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--policy", choices=["regular", "accessory"], default="regular")
    ap.add_argument("--arms", default="A,B,C,D")
    ap.add_argument("--stack", action="store_true", help="激活发生时打 C 栈")
    args = ap.parse_args()

    QApplication(sys.argv)
    if args.stack:
        from Foundation import NSNotificationCenter, NSThread

        def _on_active(_n) -> None:
            print("=== NSApplicationDidBecomeActive ===")
            for line in NSThread.callStackSymbols():
                print("    ", line)

        NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
            "NSApplicationDidBecomeActiveNotification", None, None, _on_active
        )
    policy = 0 if args.policy == "regular" else 1
    NSApplication.sharedApplication().setActivationPolicy_(policy)
    pump(300)
    print("policy =", NSApplication.sharedApplication().activationPolicy(), f"({args.policy})")
    names = {
        "A": "普通 Qt.Window（对照，预期抢）",
        "B": "现有 Overlay.show_and_run",
        "C": "Overlay + WA_ShowWithoutActivating",
        "D": "Overlay + 显示前打 mask",
        "E": "裸 Tool 窗：只 show()",
        "F": "裸 Tool 窗：show() + raise_()",
        "G": "裸 Tool 窗：show() + 原生 orderFront",
        "H": "修法链路：show + 原生 orderFront + 三件套 + 落位",
        "J": "拟修法二：显示前先打 mask",
        "K": "只 show()（属性在位、零原生调用）",
        "M": "winId 预造原生窗 → 打 mask → show → 三件套",
        "N": "winId → mask → 只 show()（不跑三件套）",
        "P": "winId → mask → 只原生 orderFrontRegardless（不走 Qt show）",
        "Q": "空臂：不显示任何窗（排除策略切换自身的激活）",
    }
    bad = []
    for arm in (["A"] if args.selftest else args.arms.split(",")):
        r = run_arm(arm)
        print(f"[{arm}] {names[arm]}")
        print("   ", json.dumps(r, ensure_ascii=False))
        if args.selftest and not r["抢了激活"]:
            bad.append(arm)
    if bad:
        print("SELFTEST FAILED：对照组没抢到焦点，判据在本环境无效 →", bad)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
