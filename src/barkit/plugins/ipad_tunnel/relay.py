"""``RelayService`` —— 中继的常驻服务壳：监听门与事件循环。

线程 / 时钟 / 起停 / join 全归框架 ``core/resident.ResidentHost``，本模块
**不自起线程、不起定时器**（门禁 ``test_no_private_timer_in_plugins``）。

``bridge.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import selectors
import socket

from .bridge_core import Bridge
from .consts import DEFAULT_DEVICE_PORT, DEFAULT_HOST, DEFAULT_LOCAL_PORT, RELAY_RETRY_DELAY, RELAY_SELECT_TIMEOUT, log
from .mux import DeviceResolver


# ──────────────────────────────────────────────
# resident 服务（线程 / 时钟 / 起停 / join 全归框架，见 core/resident.py）
# ──────────────────────────────────────────────

class RelayService:
    """自持循环型长驻服务：本机 127.0.0.1:<local> ↔ iPad:<device> 的中继。

    ``interval = None`` ⇒ 框架直接跑阻塞的 ``run()``（``stop()`` 后 ≤1s 返回）。
    本类零 ``threading`` / 零定时器；一切等待走 ``selectors`` 空 select（``_pause``）。

    端口热改：``run()`` 每轮重读 ``ports_provider()``，与当前监听不符即在**同一循环内**
    「关旧 → 开新」顺序收敛——不再有跨实例 bind 竞争与 EADDRINUSE 退避重试。

    **监听门（2026-09-29 · v2 契约）**：``link_ready_provider()`` 说不该监听时，本服务
    **主动关掉监听**（每轮复检，≤1s 内落地）。这一条不是优化，是契约的承重墙：

    对「链路此刻通不通」这个**运行期事实**，v2 起 ``tunnel.json`` 里不再有 ``link_up``
    这一格（事实住进期望态文件的代价，见 tunnelops 的 ``TUNNEL_WIRE_VERSION``）。新载体
    就是**端口本身** —— helper 那侧探 ``127.0.0.1:local_port`` 有没有人在听
    （``_upstream_ready``），探到的正是「iPad 链路是否已就绪」：中继就是上游，它只在链路
    就绪（或用户显式开了手动模式）时监听。于是「谁都不用相信一份快照」在两个进程里同时
    成立。**别把这道门挪回上层**（比如改成按声明起停本服务）：那样链路一翻就要拆建线程，
    而这个类本来就有一轮一收敛的循环，门放在循环里既便宜又天然有序。

    监听状态一变就回调 ``on_listen_change(是否在听)``：插件据此敲门叫 helper 立刻跑一轮。
    放在本线程里调，顺序天然正确 —— 先关掉端口、再叫 helper，它才不会看到上一次的现场。
    """

    name = "relay"
    interval = None

    def __init__(self, ports_provider, host: str = DEFAULT_HOST, verbose: bool = False,
                 link_ready_provider=None, on_listen_change=None):
        self._ports_provider = ports_provider      # () -> (local_port, device_port)
        #: ``() -> bool``：此刻该不该占用端口（没注入 ⇒ 恒真：CLI / 直连场景不设门）。
        self._link_ready_provider = link_ready_provider
        #: ``(bool) -> None``：监听状态刚变了（起 / 停各一次）。插件用它敲门。
        self.on_listen_change = on_listen_change
        self.host = host
        self.verbose = verbose
        self._stopped = False
        self._bridge: Bridge | None = None
        self._listener: socket.socket | None = None
        self._bound: tuple[int, int] | None = None
        # 等待用选择器（见 _pause）：**惰性创建**——插件会被反复临时实例化
        # （PluginManager._probe / 对禁用插件的 load_all），在 __init__ 里建会白占
        # 一个 kqueue fd；只在 run() 生命周期内存在，退出即关。
        self._pacing: selectors.DefaultSelector | None = None
        # 框架注入（core/resident）：服务内部异常上报为「插件问题」。可选。
        self.report_problem = None

    # ---------- 生命周期（框架调用） ----------

    def run(self) -> None:
        # ``_stopped`` 必须在这里复位：本实例会被框架反复起停（开关翻转 ⇒ 声明变化
        # ⇒ ResidentHost 停掉再重新 spawn 同一批对象），而 stop() 置位后没人清它。
        # 少了这一句，第二次被 spawn 起来时循环条件一上来就是假 ⇒ 线程立刻正常退出、
        # **不报任何错**，表现成「开关关一次再打开，中继就再也不监听了（直到重启 app）」。
        self._stopped = False
        self._pacing = selectors.DefaultSelector()
        try:
            while not self._stopped:
                self._converge()
                bridge = self._bridge
                try:
                    if bridge is None:
                        self._pause(RELAY_RETRY_DELAY)      # 起不来 / 门关着：1s 节拍复检
                    else:
                        bridge.serve_once(timeout=RELAY_SELECT_TIMEOUT)
                except Exception:  # noqa: BLE001 —— 单轮异常不得终止中继（防静默失效）
                    log.exception("[bridge] 中继事件循环异常")
                    self._report("中继事件循环异常（详见日志）")
                    self._discard_listener()
                    self._pause(RELAY_RETRY_DELAY)
        finally:
            self._discard_listener()
            if self._pacing is not None:
                self._pacing.close()
                self._pacing = None

    def stop(self) -> None:
        """请求退出：只置位；``run()`` 在 ≤1s（一次 select 超时）内返回并释放资源。"""
        self._stopped = True

    def teardown(self) -> None:
        """与 ``stop()`` 同义（资源释放由 ``run()`` 的 finally 完成）。"""
        self.stop()

    # ---------- 监听收敛 ----------

    def _converge(self) -> None:
        """把监听收敛到现场该有的样子：**先判该不该听，再判听哪个端口**。

        不该听就关掉（见类 docstring 的「监听门」）：端口是「iPad 链路已就绪」这个
        事实在系统里的唯一载体，helper 侧探的就是它。关掉之后再回调一次
        ``on_listen_change``，插件据此敲门叫 helper 跑一轮 —— 顺序是先关端口、再敲门。

        端口热改照旧在**同一循环内**「关旧 → 开新」收敛（不重建线程，也就没有跨实例
        bind 竞争）。这一步不发通知：端口换了但「有人在听」这件事没变，敲门是空的。
        """
        if not self._link_ready():
            if self._bridge is not None:
                port = self._bound[0] if self._bound else None
                self._discard_listener()
                log.info("[bridge] 链路未就绪：停止监听 %s:%s（上游端口即链路事实的载体）",
                         self.host, port)
                self._notify_listen(False)
            return
        want = self._read_ports()
        if self._bridge is not None and want == self._bound:
            return
        self._discard_listener()
        try:
            listener = self._make_listener(want)
        except OSError as e:
            log.warning("[bridge] 监听 %s:%s 失败：%s（%.0fs 后重试）",
                        self.host, want[0], e, RELAY_RETRY_DELAY)
            return
        bridge = Bridge(DeviceResolver(None), want[1], verbose=self.verbose)
        bridge.register_listener(listener)
        self._listener = listener
        self._bridge = bridge
        self._bound = want
        log.info("[bridge] 开始监听 %s:%s → iPad:%s", self.host, want[0], want[1])
        self._notify_listen(True)

    def _link_ready(self) -> bool:
        """此刻该不该占用端口。**没注入判据就恒真**（CLI / 测试直连场景不设门）。

        判据抛异常按「未就绪」处理（fail-closed）：宁可暂时不监听（helper 那边看到
        上游没了就把隧道拆了，最坏是隧道没生效），也不要拿一个说不清的判据去占端口
        —— 那会让 helper 认定上游在、把整机流量劫持进一条通往黑洞的隧道。
        """
        provider = self._link_ready_provider
        if not callable(provider):
            return True
        try:
            return bool(provider())
        except Exception:  # noqa: BLE001
            log.exception("[bridge] 读取链路就绪判据失败，按未就绪处理（本轮不监听）")
            return False

    def _notify_listen(self, listening: bool) -> None:
        """监听状态刚变了：叫插件去敲门（回调异常只记日志，不外溢到中继循环）。"""
        cb = self.on_listen_change
        if not callable(cb):
            return
        try:
            cb(listening)
        except Exception:  # noqa: BLE001
            log.exception("[bridge] 监听状态变化回调异常")

    def _read_ports(self) -> tuple[int, int]:
        try:
            local, device = self._ports_provider()
            return int(local), int(device)
        except Exception:  # noqa: BLE001 —— 配置读取异常不终止中继（沿用上次监听）
            log.exception("[bridge] 读取端口配置失败，沿用上次监听 %s", self._bound)
            return self._bound or (DEFAULT_LOCAL_PORT, DEFAULT_DEVICE_PORT)

    def _make_listener(self, want: tuple[int, int]) -> socket.socket:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        listener.bind((self.host, want[0]))
        listener.listen(128)
        listener.setblocking(False)
        return listener

    def _discard_listener(self) -> None:
        """关监听 + 关在途连接 + 关选择器（端口重建与服务退出共用）。"""
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        if self._bridge is not None:
            self._bridge.close()
            self._bridge = None
        self._bound = None

    # ---------- 等待 / 上报 ----------

    def _pause(self, seconds: float) -> None:
        """等待 seconds：空 select（不依赖 time.sleep / threading，符合插件零时钟门禁）。

        选择器只在 ``run()`` 生命周期内存在；未运行时直接返回（理论不可达：只有 run()
        与其内部循环会调用本方法）。
        """
        sel = self._pacing
        if sel is None:
            return
        try:
            sel.select(timeout=seconds)
        except OSError:
            pass

    def _report(self, reason: str) -> None:
        reporter = self.report_problem
        if callable(reporter):
            try:
                reporter(reason)
            except Exception:  # noqa: BLE001
                log.exception("[bridge] 上报服务问题失败")

    # ---------- 状态（诊断 / 测试） ----------

    @property
    def listening(self) -> bool:
        return self._listener is not None

    @property
    def local_port(self) -> int | None:
        return self._bound[0] if self._bound else None

    @property
    def device_port(self) -> int | None:
        return self._bound[1] if self._bound else None

    @property
    def data_plane_state(self) -> bool | None:
        """阶梯第 ① 级的读数：最近一次有结论的窗口搬过字节吗（None = 还没结论 / 没在跑）。

        由中继线程在 ``Bridge._tick_window`` 里维护，这里只读一个 bool/None —— 单一属性读
        在 CPython 下是原子的，故探针线程读它无需加锁（不引入跨线程复合操作）。
        """
        bridge = self._bridge
        if bridge is None:
            return None
        return getattr(bridge, "data_plane_state", None)

    @property
    def data_plane_fault(self) -> bool:
        """中继自报：最近一次有结论的窗口「一条连接都没搬动字节」。

        与 ``data_plane_state`` 的区别见 ``Bridge._tick_window``：state 会被链路翻转复位
        （它是判据），本标记不会（只有真搬到字节才解除）⇒ 面板用它表达「这条隧道搬不动
        字节」（取值 ``data_plane_dead``），重开那一瞬间也不会把故障抹掉。
        """
        bridge = self._bridge
        return bool(getattr(bridge, "data_plane_fault", False)) if bridge is not None else False

    def pause_accept(self, seconds: float) -> None:
        """让中继停收新连接一段时间（探针下探「通道 / 握手」前调，见 ``Bridge.pause_accept``）。

        未监听时是无操作。
        """
        bridge = self._bridge
        if bridge is not None:
            bridge.pause_accept(seconds)

    def reset_data_plane(self) -> None:
        """复位「连续零成功窗口」计数（探针在每次链路翻转时调，见 ``LinkProbeService``）。

        未监听 / 已停（``_bridge is None``）时是无操作：新 Bridge 的计数本来就是 0。
        """
        bridge = self._bridge
        if bridge is not None:
            bridge.reset_data_plane()
