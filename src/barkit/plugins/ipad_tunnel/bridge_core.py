"""``Bridge`` —— 本机 ``127.0.0.1:<local>`` ↔ iPad ``<device>`` 的字节中转：
背压、在途连接（``_Pending``）、数据面的会话窗口统计。

``bridge.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import errno
import logging
import selectors
import socket
import time

from barkit.core.log_throttle import log_throttled

from .consts import BUF_LIMIT, DATA_BAD_WINDOWS, DATA_WINDOW, DATA_WINDOW_MIN_SESSIONS, MuxError, PENDING_TIMEOUT, RECV_SIZE, RESOLVE_RETRY_INTERVAL, _PENDING, log
# ⚠ consts.PENDING_LIMIT 与 mux.begin_device_tunnel 走**模块对象**而不是绑定副本：
# 测试要 monkeypatch 它们（test_ipad_tunnel.py）—— 打成绑定副本的话补丁落在调用方的
# 命名空间上、真身纹丝不动 ⇒ 判据静默失效。见《拆包》第 2 步第 3 条。
from . import consts, mux
from .mux import DeviceResolver, Tunnel, _Pending, parse_connect_reply, tune_tcp


class Bridge:
    def __init__(self, resolver: DeviceResolver, device_port: int, verbose: bool = False):
        self.resolver = resolver
        self.device_port = device_port
        self.verbose = verbose
        self.sel = selectors.DefaultSelector()
        self.tunnels: set[Tunnel] = set()
        self.connections = 0
        self.reconnects = 0
        # ── 接入背压：在途 Connect + 失败计数 + 是否还在 accept ──
        self.pending: dict[socket.socket, _Pending] = {}
        self.connect_failed = 0
        self.rejected = 0                 # 因在途上限被直接拒绝的连接（我们自己的过载）
        self._listener: socket.socket | None = None
        self._accepting = False
        self._last_invalidate = 0.0
        self._probe_pause_until = 0.0     # > 此刻则暂停 accept（探针下探让路，见 pause_accept）
        # ── 数据面自报健康（会话统计窗口；中继线程内读写，见 LinkProbeService） ──
        # 三个字段各有各的消费者，**别合并**（口径与分工见 _tick_window）：
        self.data_plane_bad_streak = 0    # 连续「零成功」窗口数（0 = 健康 / 无结论）
        # 「最近一次有结论的窗口搬过字节吗」——阶梯第 ① 级的读数。None = 还没有结论。
        # **链路一翻转就复位**（reset_data_plane）：旧链路的结论对新链路没有参考价值，
        # 留着它就会「重开第一个周期内又被同一个旧结论拆掉」（2026-09-22 的 18:46:53
        # 开 / 18:46:56 拆）。
        self.data_plane_state: bool | None = None
        # 「这条隧道搬过字节吗」这个**事实**：**只有真搬到字节才解除**，不随重开清零。
        # 面板的严重格据此报 —— 重开那一瞬间把故障抹掉的话，用户就看不到「刚才是空转」。
        self.data_plane_fault = False
        self._win_attempts = 0
        self._win_ok = 0
        self._win_deadline = time.monotonic() + DATA_WINDOW

    def register_listener(self, listener: socket.socket) -> None:
        """把监听 socket 纳入选择器（resident 中继每轮事件循环前调用一次）。"""
        self._listener = listener
        self._set_accepting(True)

    def serve_once(self, timeout: float = 1.0) -> None:
        """跑一轮事件循环；无注册 fd 时即一次 timeout 长的等待（天然节拍）。

        内层与旧 ``serve_forever`` 完全一致；拆出来是为了让 resident 中继循环每轮
        都能重读配置（端口热改在循环内收敛），并保证 ``stop()`` 后 ≤timeout 返回。
        """
        events = self.sel.select(timeout=timeout)
        for key, mask in events:
            data = key.data
            if data is None:
                self._accept(key.fileobj)      # key.fileobj 即监听 socket
            elif isinstance(data, tuple):
                self._poll_pending(key.fileobj)   # ("pending", ...) 标记
            else:
                self._service(data, key.fileobj, mask)
        self._expire_pending()
        self._sync_backpressure()
        self._tick_window()
        self._reap()

    def serve_forever(self, listener: socket.socket) -> None:
        """CLI 路径：注册监听后无限轮询（``main()`` 用，行为与迁移前一致）。"""
        self.register_listener(listener)
        while True:
            self.serve_once()

    def close(self) -> None:
        """关掉在途连接 + 选择器（端口重建 / 服务退出时调用，防 fd 泄漏）。"""
        self.close_all()
        try:
            self.sel.close()
        except OSError:
            pass

    def _set_accepting(self, on: bool) -> None:
        """启/停从监听 socket 收 accept（背压手段：不再收，连接就压在**内核** backlog 里）。

        比「收下来再关掉」温和：被关掉会让客户端立刻重试（重试风暴）；压在 backlog 里
        客户端会按它自己的 connect 超时等待，正是「我们忙不过来」的如实表达。
        """
        listener = self._listener
        if listener is None or on == self._accepting:
            return
        try:
            if on:
                self.sel.register(listener, selectors.EVENT_READ, None)
            else:
                self.sel.unregister(listener)
            self._accepting = on
        except (KeyError, ValueError, OSError):
            pass

    def _sync_backpressure(self) -> None:
        """按在途数切换 accept 开关（外加探针下探期间的让路窗口）。"""
        paused = time.monotonic() < self._probe_pause_until
        self._set_accepting(not paused and len(self.pending) < consts.PENDING_LIMIT)

    def pause_accept(self, seconds: float) -> None:
        """暂停 accept 一段时间：给探针的**下探**让路（见 ``LinkProbeService``）。

        探针要下探「通道 / 握手」两级时必须先让中继停手，否则它量到的是**我们自己的
        噪音** —— 2026-09-20 那场自激回路就是这么来的（探针把「自己太忙」读成「链路
        断了」，于是拆机、负载消失、又自动重开，两分钟一轮）。

        带**截止时间**而不是「开 / 关」两个调用：探针线程若在中途死掉，期限一到 accept
        自己恢复，不会留下一个永久黑洞。只挡**新**连接；在途与已建成的隧道照常搬运。
        """
        self._probe_pause_until = time.monotonic() + max(0.0, float(seconds))

    def _accept(self, listener: socket.socket) -> None:
        try:
            local, _ = listener.accept()
        except OSError:
            return
        local.setblocking(False)
        tune_tcp(local)
        if len(self.pending) >= consts.PENDING_LIMIT:
            # 兜底（本轮 select 之前就满了）：快速失败并计一次我们自己的过载
            self.rejected += 1
            try:
                local.close()
            except OSError:
                pass
            self._sync_backpressure()
            return
        try:
            device_id = self.resolver.get()
            dev = mux.begin_device_tunnel(device_id, self.device_port)
        except (MuxError, OSError, RuntimeError) as e:
            self.connect_failed += 1
            self._warn(f"隧道建立失败，放弃该连接：{e}")
            self._maybe_invalidate_resolver()
            try:
                local.close()
            except OSError:
                pass
            return
        self._win_attempts += 1
        pend = _Pending(local, dev, time.monotonic() + PENDING_TIMEOUT)
        self.pending[dev] = pend
        try:
            self.sel.register(dev, selectors.EVENT_READ, ("pending", dev))
        except (KeyError, ValueError, OSError):
            self.pending.pop(dev, None)
            self._abort_pending(pend)
            return
        self._sync_backpressure()

    def _poll_pending(self, dev: socket.socket) -> None:
        """轮询一条待应答连接：收全应答就升级为 Tunnel，超时/失败就放弃。"""
        pend = self.pending.get(dev)
        if pend is None:
            return
        try:
            chunk = dev.recv(4096)
        except (BlockingIOError, InterruptedError):
            return
        except OSError as e:
            self.connect_failed += 1
            self._warn(f"隧道建立失败，放弃该连接：{e}")
            self._maybe_invalidate_resolver()
            self._drop_pending(pend)
            return
        if not chunk:
            self.connect_failed += 1
            self._warn("隧道建立失败，放弃该连接：usbmuxd 关闭了连接")
            self._maybe_invalidate_resolver()
            self._drop_pending(pend)
            return
        pend.buf += chunk
        state, _rest = parse_connect_reply(pend.buf)
        if state is _PENDING:
            return
        if state is not None:
            self.connect_failed += 1
            self._warn(f"隧道建立失败，放弃该连接：{state}")
            self._maybe_invalidate_resolver()
            self._drop_pending(pend)
            return
        self._promote(pend)

    def _promote(self, pend: _Pending) -> None:
        """应答成功：升级成一条正式隧道（此后与旧实现完全一致）。

        ``dev`` 此刻已在选择器里（带挂起标记），故只登记 ``local``；``dev`` 的标记由
        紧随其后的 ``_update_interest`` 用 ``modify`` 换成真正的 Tunnel（重复 register
        会抛「fd already registered」，那是旧实现没走过的路径）。
        """
        self.pending.pop(pend.dev, None)
        dev = pend.dev
        dev.setblocking(False)
        tune_tcp(dev)
        t = Tunnel(pend.local, dev)
        self.tunnels.add(t)
        self.connections += 1
        try:
            self.sel.register(pend.local, selectors.EVENT_READ, t)
        except (KeyError, ValueError, OSError):
            self._close(t)
            return
        self._update_interest(t)
        if self.verbose:
            self._log(f"桥接新连接（累计 {self.connections}，当前 {len(self.tunnels)}）")
        self._sync_backpressure()

    def _drop_pending(self, pend: _Pending) -> None:
        """放弃一条待应答连接（只在 pending 表里）。"""
        self.pending.pop(pend.dev, None)
        self._abort_pending(pend)
        self._sync_backpressure()

    def _abort_pending(self, pend: _Pending) -> None:
        try:
            self.sel.unregister(pend.dev)
        except (KeyError, ValueError, OSError):
            pass
        for sock in (pend.dev, pend.local):
            try:
                sock.close()
            except OSError:
                pass

    def _expire_pending(self) -> None:
        """超截止时间的待应答连接：放弃（旧实现是 5s × 2 次重试，把循环卡掉 10s）。"""
        if not self.pending:
            return
        now = time.monotonic()
        for pend in [p for p in self.pending.values() if now >= p.deadline]:
            self.connect_failed += 1
            self._warn(f"隧道建立失败，放弃该连接：等 usbmuxd 应答超过 {PENDING_TIMEOUT:.0f}s")
            self._maybe_invalidate_resolver()
            self._drop_pending(pend)

    def _maybe_invalidate_resolver(self) -> None:
        """重建缓存的 DeviceID——但**限速**：失败风暴里每次重解析只会把通道压得更死。

        **五条失败出口一个都不能漏**（发请求报错 / 收包报错 / 对端关连接 / 等应答超时 /
        应答内容本身是失败）。2026-09-22 真机故障的根因就是最后那条没调：它恰恰是
        **设备号过期唯一的出口** —— usbmuxd 对「不认识的 DeviceID」回的正是「应答内容是
        失败」，而收包 / 超时这些出口一个都不会走到。漏了它，缓存设备号永不自愈 ⇒
        隧道每轮建起来、第一条连接被拒、随即被拆，「两分钟一轮的整机断网」。

        别在这条上省：清缓存只是让下一次连接多问一次设备列表，代价远小于让整条隧道一直
        搬不动字节。
        """
        now = time.monotonic()
        if now - self._last_invalidate < RESOLVE_RETRY_INTERVAL:
            return
        self._last_invalidate = now
        self.resolver.invalidate()
        self.reconnects += 1

    def _tick_window(self) -> None:
        """窗口到点：结算会话统计，维护数据面那三个字段。

        口径：**尝试** = 真的把开通道请求发出去了的连接；**成功** = 搬过至少一个字节。
        窗口内尝试数不足（``DATA_WINDOW_MIN_SESSIONS``）视为「无结论」，什么都不改 ——
        免得空闲期被误判成不通。

        **三个输出的分工（一个都不能合并）**：

        - ``data_plane_state`` —— 「最近一次有结论的窗口搬过字节吗」，**阶梯第 ① 级的读数**。
          它在「有结论但还没到 ``DATA_BAD_WINDOWS`` 个坏窗口」时故意仍留旧值 / None：一个
          坏窗口不足以下结论（防抖），这一层由 ``streak`` 把关。链路一翻转就复位。
        - ``data_plane_bad_streak`` —— 「连续坏了几窗」，只服务「够不够格下结论」。
        - ``data_plane_fault`` —— 「这条隧道搬过字节吗」这个**事实**，喂面板的严重格。
          它**只有真搬到字节才解除**（见 ``reset_data_plane``）。
        """
        now = time.monotonic()
        if now < self._win_deadline:
            return
        attempts, ok = self._win_attempts, self._win_ok
        self._win_attempts = self._win_ok = 0
        self._win_deadline = now + DATA_WINDOW
        if attempts < DATA_WINDOW_MIN_SESSIONS:
            return
        if ok == 0:
            self.data_plane_bad_streak += 1
            self.data_plane_fault = True
            if self.data_plane_bad_streak >= DATA_BAD_WINDOWS:
                self.data_plane_state = False
            self._log(f"数据面统计：本窗口 {attempts} 条连接未搬运任何字节"
                      f"（连续 {self.data_plane_bad_streak} 个窗口），疑似上游不通")
        else:
            if self.data_plane_bad_streak:
                self._log(f"数据面统计：本窗口 {ok}/{attempts} 条连接搬过数据，判定恢复正常")
            self.data_plane_bad_streak = 0
            self.data_plane_fault = False
            self.data_plane_state = True

    def reset_data_plane(self) -> None:
        """给数据面三个字段一条**新起跑线**：``streak`` 与 ``state`` 都复位。

        由探针在每次链路翻转时调用（``LinkProbeService._publish``）：旧那条链路的窗口统计
        对新链路没有参考价值。不复位就会「重开后第一个探针周期内被同一个旧结论再次拆
        掉」——实测 2026-09-22 18:46:53 开、18:46:56 拆（**2 秒**），而真机上表现为两分钟
        一轮的整机断网。

        ``state`` 必须跟着复位（它是**判据**，不是事实）：留着旧链路的「搬不动」，新链路
        刚建起来就会被这一层判死。``fault`` 相反 —— 它记的是「这条隧道搬过字节吗」这个
        **事实**，必须由真的搬运成功来解除，面板的严重格靠它说话。

        跨线程：写方是探针线程、读方是本中继线程，单个 int/bool/None 赋值在 CPython 下
        原子（与既有权衡一致，不引入锁）。
        """
        self.data_plane_bad_streak = 0
        self.data_plane_state = None

    def _service(self, t: Tunnel, sock, mask: int) -> None:
        READ, WRITE = selectors.EVENT_READ, selectors.EVENT_WRITE
        if t.finished or t not in self.tunnels:
            return
        try:
            if sock is t.local:
                if mask & READ:
                    self._read(t, t.local, is_local=True)
                if mask & WRITE:
                    if not self._write(t, t.local, is_to_dev=False):
                        self._close(t)
                        return
            else:
                if mask & READ:
                    self._read(t, t.dev, is_local=False)
                if mask & WRITE:
                    if not self._write(t, t.dev, is_to_dev=True):
                        self._close(t)
                        return
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                self._close(t)
                return
        self._propagate_eof(t)
        if t.finished:
            self._close(t)
            return
        self._update_interest(t)

    def _read(self, t: Tunnel, sock, is_local: bool) -> None:
        try:
            data = sock.recv(RECV_SIZE)
        except (ConnectionResetError, BrokenPipeError):
            data = b""
        except OSError as e:
            # EAGAIN/EINTR 是「这一轮没读到」，不是对端关闭：非阻塞 socket 上
            # select 报可读后 recv 仍可能返回 EAGAIN（伪唤醒等），当 EOF 处理会
            # 静默拆掉正在搬运的连接。与 _write / _poll_pending 的放行口径对齐
            # （2026-10-12 评审：同文件只有此处漏了）。
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                return
            data = b""
        if not data:
            if is_local:
                t.eof_local = True
            else:
                t.eof_dev = True
            return
        if not t.moved:
            # 这条会话**搬过字节**了 —— 数据面健康统计的口径就是它（零字节 = 没干活）
            t.moved = 1
            self._win_ok += 1
        if is_local:
            t.buf_ld += data
        else:
            t.buf_dl += data

    def _write(self, t: Tunnel, dst, is_to_dev: bool) -> bool:
        buf, attr = (t.buf_ld, "buf_ld") if is_to_dev else (t.buf_dl, "buf_dl")
        if not buf:
            return True
        try:
            n = dst.send(buf)
        except (BrokenPipeError, ConnectionResetError):
            return False
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                return True
            return False
        setattr(t, attr, buf[n:])
        return True

    def _propagate_eof(self, t: Tunnel) -> None:
        if t.eof_local and not t.buf_ld and not t.flushed_ld:
            try:
                t.dev.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            t.flushed_ld = True
        if t.eof_dev and not t.buf_dl and not t.flushed_dl:
            try:
                t.local.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            t.flushed_dl = True

    def _set_interest(self, sock, ev: int, t: Tunnel) -> None:
        if sock.fileno() < 0:
            return
        try:
            self.sel.get_key(sock)
            registered = True
        except (KeyError, ValueError):
            registered = False
        try:
            if ev:
                if registered:
                    self.sel.modify(sock, ev, t)
                else:
                    self.sel.register(sock, ev, t)
            elif registered:
                self.sel.unregister(sock)
        except (KeyError, ValueError, OSError):
            self._close(t)

    def _update_interest(self, t: Tunnel) -> None:
        READ, WRITE = selectors.EVENT_READ, selectors.EVENT_WRITE
        ev = 0
        if not t.eof_local and len(t.buf_ld) < BUF_LIMIT:
            ev |= READ
        if t.buf_dl:
            ev |= WRITE
        self._set_interest(t.local, ev, t)
        ev = 0
        if not t.eof_dev and len(t.buf_dl) < BUF_LIMIT:
            ev |= READ
        if t.buf_ld:
            ev |= WRITE
        self._set_interest(t.dev, ev, t)

    def _close(self, t: Tunnel) -> None:
        for sock in (t.local, t.dev):
            try:
                self.sel.unregister(sock)
            except (KeyError, ValueError, OSError):
                pass
            try:
                sock.close()
            except OSError:
                pass
        self.tunnels.discard(t)

    def _reap(self) -> None:
        for t in [x for x in self.tunnels if x.finished]:
            self._close(t)

    def close_all(self) -> None:
        """关闭所有在途桥接连接（仅中继线程内调用，无跨线程竞争）。

        resident 中继在端口热改 / 服务退出时会丢弃整个 Bridge 实例；本方法确保已建立
        的 local↔device 两条 socket 被主动关闭，而不是等 GC 被动回收（否则旧
        device_port 上的在途流量会继续走旧路径，直到进程退出）。待应答的挂起连接同理。
        """
        for pend in list(self.pending.values()):
            self._abort_pending(pend)
        self.pending.clear()
        for t in list(self.tunnels):
            self._close(t)

    def _log(self, msg: str) -> None:
        """数据面的**常态**留痕（连接建立、窗口统计）。"""
        log.info("[bridge] %s", msg)

    def _warn(self, msg: str) -> None:
        """数据面的**失败**留痕。两处与 ``_log`` 不同，都有理由：

        - 级别是 ``WARNING``（从前一律 INFO，于是按级别过滤时「建隧道失败」与
          「窗口统计」同进同出）；
        - 按整句话合并：同一个原因（如 ``Connection refused``）在窗口内只落一条。
          隧道断着而浏览器不停重试时，这里过去是一次连接一条，能把日志刷满。
        """
        log_throttled(log, logging.WARNING, "[bridge] %s", msg)
