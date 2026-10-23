"""``LinkProbeService`` —— 链路四级下探（① 数据面 → ② 设备 → ③ 通道 → ④ SOCKS5）
与 2 票迟滞判定；结论是**最浅的失败层**（``HOP_*``），决定中继要不要占用 local_port。

``bridge.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import time

from .consts import ConnectRefused, DEFAULT_DEVICE_PORT, DeviceNotFound, DeviceUnknown, HOP_DATA, HOP_UNKNOWN, LINK_DEVICE_CHANGED, LINK_ENGINE_SILENT, LINK_MUX_BUSY, LINK_NO_DEVICE, LINK_OK, LINK_PORT_REFUSED, MuxError, PROBE_COOLDOWN_POLLS, PROBE_DEEP_INTERVAL, PROBE_DOWN_COOLDOWN_POLLS, PROBE_FLIP_VOTES, PROBE_INTERVAL, _HOP_BY_KIND, log

# ⚠ 这三个**走模块对象**（``mux.X``）而不是绑定副本（``from .mux import X``）：
# 拆包后它们的真身在 ``mux``，而测试要 monkeypatch 它们（``test_ipad_tunnel.py``
# 二十余处）—— 打成绑定副本的话补丁落在调用方的命名空间上、真身纹丝不动 ⇒ 判据
# **静默失效**（症状在很远的地方：探针量到的 hop 不对）。见《拆包》第 2 步第 3 条。
from . import mux


class LinkProbeService:
    """周期型长驻服务：链路判据 = **逐级回退的阶梯**（成本从低到高，通过就停）+ 迟滞 + 冷却。

    **阶梯**（2026-09-23；`_walk`）：从最便宜的一级开始，通过就**停**，不通过才下探一级，
    最后把「最浅的失败层」（``HOP_*``）交给面板点名。

    - ① **数据面**（``data_plane_state_provider()``，中继的会话窗口统计）：零成本，且数的
      就是真实流量、天然覆盖端到端。**它说搬过字节就到此为止** —— 不再碰任何更贵的探测，
      这才是「不与数据面抢通道」的根本保证（2026-09-20 那版是靠「运行期整条砍掉」实现的，
      代价是运行期没有任何东西在验端到端，2026-09-22 的故障因此完全静默）。
    - ① 没通过才往下：② 设备在不在（纯本地）→ ③ 通道能否打开 → ④ SOCKS5 有没有应答。
    - ③④ 要碰 USB 通道：**下探前先让中继停手**（``descend_pause`` → ``RelayService.pause_accept``），
      否则量到的是我们自己的噪音；运行期限速 ``PROBE_DEEP_INTERVAL``，准入期每次都全量
      （与迁移前一致）。停手带截止时间，探针中途死掉也不会留下永久黑洞。

    **判定**：

    - ``hop is None`` ⇒ 就绪。``HOP_UNKNOWN`` ⇒ 这一轮**量不出来**（usbmuxd 忙）⇒ 不改判定
      （旧版把运行期一切非「设备消失」都当通过，等于默认健康）。
    - 其余层失败 ⇒ 不就绪（迟滞 2 票；准入 / 运行期同一套 —— 因为下探前已让中继停手，
      量出来的不再是我们自己的负载）。
    - ``HOP_DATA`` ⇒ **立即拆、不等票**：那是「②③④ 刚实测全过、① 却搬不动字节」，中继已按
      窗口过滤过，再等票只是让整机多断几秒网。冷却用 ``PROBE_DOWN_COOLDOWN_POLLS``。
    - 发布 false 后进**冷却**（其它层 ≈12s、数据面层 ≈120s），冷却期内不自动重开。

    节律靠类属性 ``interval`` 声明（数据，不是时钟）：框架在托管线程里按
    ``interval`` 周期调用 ``poll()``。首轮在 ``interval`` 秒后执行（与迁移前
    的 ``threading.Timer(3.0, ...)`` 首次触发时机一致，保 ``link_up`` 首翻 ≈6s）。
    """

    name = "probe"

    def __init__(self, device_port_provider, on_link_change=None, udid: str | None = None,
                 interval: float = PROBE_INTERVAL, flip_votes: int = PROBE_FLIP_VOTES,
                 mode_provider=None, data_plane_state_provider=None, data_plane_reset=None,
                 descend_pause=None, deep_interval: float = PROBE_DEEP_INTERVAL,
                 on_poll=None):
        self.interval = float(interval)
        self._device_port_provider = device_port_provider
        self.on_link_change = on_link_change
        #: ``(link_up: bool) -> None``：**每轮** ``poll()`` 结束都调一次（电平），与
        #: ``on_link_change``（只在翻转那一次响，边沿）分工不同。按需型读取（如 iOS 端
        #: 签名通告）靠它做低频重试 —— 重试的节拍**借探针这一个**，插件不必新起时钟；
        #: 稳态下回调方立刻返回，不构成负担。
        self.on_poll = on_poll
        self.udid = udid
        self.flip_votes_required = max(1, int(flip_votes))
        self._flip_votes = 0                  # 与当前状态相反的连续探针计数
        self._link_up = False
        # 是否已给出首次结论：未结论前调用方应沿用磁盘值（不盲写 link_up=false）
        self.concluded = False
        self._last_reason: str | None = None  # 最近一次探针失败原因（去重后记录）
        #: ``() -> bool``：隧道是否正在接管流量（决定 ③④ 两级的节流与是否让路）。
        #: 由插件提供；跨线程只读一个 bool 字段，写方是插件主线程。
        self._mode_provider = mode_provider
        #: ``() -> bool | None``：阶梯第 ① 级的读数 —— 中继的「最近一次有结论的窗口搬过
        #: 字节吗」（None = 还没结论）。True 就此结案；False / None 才往下探。
        self._data_plane_state_provider = data_plane_state_provider
        #: ``() -> None``：给中继的窗口统计一条新起跑线（``RelayService.reset_data_plane``）。
        #: 链路一翻转就调 —— 旧链路的统计对新链路没有参考价值（见 ``_publish``）。
        self._data_plane_reset = data_plane_reset
        #: ``() -> None``：下探 ③④ 之前让中继停手（``RelayService.pause_accept``）。
        #: 不调就会量到我们自己的噪音 —— 2026-09-20 自激回路的根就是它。
        self._descend_pause = descend_pause
        self._deep_interval = max(0.0, float(deep_interval))
        self._last_deep_at = 0.0              # ③④ 上次真下探的时刻（0 = 还没探过，必探）
        self._deep_reason: str | None = None  # ③④ 上次结论（未到点就沿用）
        self._deep_kind: str = LINK_OK
        self._cooldown_polls = 0              # 冷却剩余票数：>0 时不许发布 up
        #: 最近一次测量的种类（``LINK_*``；给诊断与日志用）
        self.last_kind: str | None = None
        #: **最浅的失败层**（``HOP_*``；None = 全过）。面板据此点名「卡在哪一段」。
        self.last_hop: str | None = None

    # ---------- 生命周期（框架调用） ----------

    def poll(self) -> None:
        """一次「逐级回退」判据 + 翻转回调（框架每 interval 秒调用一次）。

        判据主体在 ``_poll_walk``；之后**无条件**给一次 ``on_poll``（写在 ``finally``：
        判据自身出异常也不该让这条链断掉 —— 异常照旧由框架记走）。
        """
        try:
            self._poll_walk()
        finally:
            cb = self.on_poll
            if cb is not None:
                try:
                    cb(self._link_up)
                except Exception:  # noqa: BLE001
                    log.exception("[bridge] on_poll 回调异常")

    def _poll_walk(self) -> None:
        """判据主体（原 ``poll`` 的正文，逐字节搬移）。"""
        hop, reason, kind = self._walk()
        self.last_hop = hop
        self.last_kind = kind

        if hop == HOP_UNKNOWN:
            # 这一轮**量不出来**（usbmuxd 忙）：既不算通过也不算失败，沿用上一轮判定。
            # 旧版在这里按「运行期默认健康 / 准入默认不通」二分，两种默认都是猜。
            if reason and reason != self._last_reason:
                log.info("[bridge] 这一轮量不出来（%s）：沿用上一轮判定，不据此拆机", reason)
                self._last_reason = reason
            return

        # ① 数据面搬不动字节，而 ②③④ 刚实测全过 ⇒ 立即拆，不等票。
        #    中继已按窗口过滤过（连续 DATA_BAD_WINDOWS 窗），再等 2 票只是让整机多断几秒网。
        if hop == HOP_DATA and self._link_up:
            self.concluded = True
            log.warning(
                "[bridge] 数据面搬不动字节（②③④ 各级刚实测全过）：主动拆隧道，"
                "%.0fs 内不自动重开", PROBE_DOWN_COOLDOWN_POLLS * self.interval)
            self._publish(False, PROBE_DOWN_COOLDOWN_POLLS)
            return

        up = hop is None

        # 迟滞：与当前状态相反的探针只计票，连续 flip_votes_required 次同向才翻转；
        # 探针回到当前状态则清票。flip_votes=1 时退化为单次即翻转的旧行为。
        if up == self._link_up:
            self._flip_votes = 0
            changed = False
        else:
            self._flip_votes += 1
            changed = self._flip_votes >= self.flip_votes_required
            if changed and up and self._cooldown_polls > 0:
                # 冷却期内不许重开：清票，下一轮从头计（状态仍是 down）
                self._flip_votes = 0
                changed = False
                log.info("[bridge] 链路已就绪，但仍在拆除后的冷却期内（剩 %d 票），暂不重开",
                         self._cooldown_polls)
        self.concluded = True
        if changed:
            self._publish(up, PROBE_COOLDOWN_POLLS if not up else 0)
        if self._cooldown_polls > 0:
            self._cooldown_polls -= 1
        # 链路没就绪时把原因打出来（与上次原因不同才记，避免每 3s 刷屏），
        # 否则 link_up 永远 False 却无任何线索，用户无从知道卡在哪一段。
        if not up and reason and reason != self._last_reason:
            log.warning("[bridge] 链路未就绪（卡在 %s）：%s", hop, reason)
            self._last_reason = reason

    def _publish(self, up: bool, cooldown_polls: int) -> None:
        """落定一次发布（唯一改 ``_link_up`` 与冷却的入口）。

        每次翻转都顺手把**判据的起跑线**清干净：让中继把窗口统计复位（``reset_data_plane``
        —— ``streak`` 与阶梯第 ① 级的 ``state`` 都归零）。不复位就有后果 —— 旧链路的
        「搬不动」会原样算到新链路头上，于是重开后**第一个探针周期**内第 ① 级就判死 ⇒
        刚建起来的隧道立刻又被拆（实测 2026-09-22 18:46:53 开、18:46:56 拆，只隔一个
        周期），真机上表现为「两分钟一轮的整机断网」。复位之后，新链路至少拿到
        ``DATA_BAD_WINDOWS`` 个完整窗口（≈40s）去自证，而不是一票否决。

        注意**不复位故障标记**（``Bridge.data_plane_fault``）：那记的是「这条隧道搬过字节
        吗」这个事实，得由真的搬运成功来解除 —— 面板的严重格靠它说话。
        """
        self._link_up = bool(up)
        self._flip_votes = 0
        self._cooldown_polls = max(0, int(cooldown_polls))
        resetter = self._data_plane_reset
        if callable(resetter):
            try:
                resetter()
            except Exception:  # noqa: BLE001
                log.exception("[bridge] 复位数据面统计失败（第 ① 级可能仍拿旧结论说话）")
        if self.on_link_change is not None:
            try:
                self.on_link_change(up)
            except Exception:  # noqa: BLE001
                log.exception("[bridge] on_link_change 回调异常")

    def stop(self) -> None:
        """无独立阻塞资源：框架不再调用 ``poll()`` 即为停止（单次探针 ≤ CONNECT_TIMEOUT）。"""

    def teardown(self) -> None:
        """与 ``stop()`` 同义。"""
        self.stop()

    def reset(self) -> None:
        """把判定清回起点：**重新**从「还没有结论」开始判。

        由插件在隧道总开关翻转时调用（``set_automation_active``）。为什么必须清：
        ``concluded`` / ``link_up`` 现在是中继「该不该占用 local_port」的判据
        （``RelayService._link_ready``），而上一次开机留下的「链路就绪」在开关重新
        打开时会被当成**当下的**事实 —— 中继会立刻占上端口，helper 随即把整机流量
        劫持进一条可能通往黑洞的隧道，等探针 6 秒后翻下来才收场。清掉它，开关打开
        后的第一件事就是重新量一次链路。

        一并清掉迟滞票数与冷却：它们同样属于「上一轮的判定」，与新的一轮无关。
        已在跑的探针线程不被中断（它下一轮 poll 会重新计票）。
        """
        self._link_up = False
        self.concluded = False
        self._flip_votes = 0
        self._cooldown_polls = 0
        self._last_reason = None
        self.last_hop = None
        self.last_kind = None

    # ---------- 状态 ----------

    def probe_link(self) -> bool:
        """**完整**探测一次（不看运行期档位）：设备在 ＋ 能打通 ＋ SOCKS5 握手有应答。"""
        return self._measure(force_full=True)[0] is None

    @property
    def link_up(self) -> bool:
        return self._link_up

    @property
    def device_port(self) -> int:
        try:
            return int(self._device_port_provider())
        except Exception:  # noqa: BLE001
            log.exception("[bridge] 读取 device_port 失败，退回 %s", DEFAULT_DEVICE_PORT)
            return DEFAULT_DEVICE_PORT

    # ---------- 测量与提供方 ----------

    def _carrying(self) -> bool:
        provider = self._mode_provider
        if not callable(provider):
            return False
        try:
            return bool(provider())
        except Exception:  # noqa: BLE001
            log.exception("[bridge] 读取运行期档位失败，按未接管流量处理")
            return False

    def _plane_state(self) -> bool | None:
        """阶梯第 ① 级：中继自报「最近一次有结论的窗口搬过字节吗」（None = 还没结论）。"""
        provider = self._data_plane_state_provider
        if not callable(provider):
            return None
        try:
            value = provider()
        except Exception:  # noqa: BLE001
            log.exception("[bridge] 读取数据面状态失败，按无结论处理")
            return None
        return None if value is None else bool(value)

    def _walk(self) -> tuple[str | None, str | None, str]:
        """逐级回退地判一次：**从最便宜的一级开始，通过就停**（不再下探更贵的那几级）。

        返回 ``(hop, reason, kind)``；``hop`` 是**最浅的失败层**，None = 全过或无从证伪。
        """
        plane = self._plane_state()
        if plane is True:
            # ① 通过 ⇒ 停在这儿。数据面数的就是真实流量，它说搬过字节，端到端就已经被证明了；
            # 再往下探只是白花等待、白占通道。
            return None, None, LINK_OK
        reason, kind = self._measure()
        hop = _HOP_BY_KIND.get(kind, HOP_UNKNOWN)
        if hop is None and plane is False:
            # ②③④ 刚实测全过，可 ① 却有结论地搬不动字节 ⇒ 卡在我们这一侧（本机中继）。
            hop = HOP_DATA
        return hop, reason, kind

    def _measure(self, force_full: bool = False) -> tuple[str | None, str]:
        """②③④ 三级：设备在不在 → 通道能否打开 → SOCKS5 有没有应答。

        **通过就停**：设备不在就不必去开通道，通道打不开就不必去握手（省一次 1.5s 等待）。

        ``force_full=True`` 忽略 ③④ 的节流（``probe_link`` 用：它要的是「此刻到底通不通」，
        不是「按节律该看哪一级」）。
        """
        try:
            device_id, _serial = mux.pick_device(self.udid)
        except DeviceNotFound as e:
            return f"未找到 iPad USB 设备：{e}", LINK_NO_DEVICE
        except MuxError as e:
            return f"usbmuxd 不可用（通道可能被占满）：{e}", LINK_MUX_BUSY
        except OSError as e:
            return f"访问 usbmuxd 失败（通道可能被占满）：{e!r}", LINK_MUX_BUSY
        except RuntimeError as e:      # 未知失败：保守当「通道忙」，不据此拆机
            return f"设备探测失败：{e}", LINK_MUX_BUSY

        if force_full:
            self._last_deep_at = 0.0   # 强制探 ③④
        if not self._deep_due():
            return self._deep_reason, self._deep_kind    # ③④ 未到点：沿用上次结论
        reason, kind = self._measure_deep(device_id)
        self._last_deep_at = time.monotonic()
        self._deep_reason, self._deep_kind = reason, kind
        return reason, kind

    def _deep_due(self) -> bool:
        """③④ 这两级这一轮该不该真探。

        - **准入**（未接管流量）：每次都探，与迁移前的完整探测一致 —— 那时没有数据面要保护。
        - **数据面已判不通**：立刻探。此刻本来就没有数据在搬，下探不抢谁；而且要的正是
          「到底卡在哪一段」这个结论。
        - 其余（运行期且数据面没判不通，多半是空闲）：**限速** ``PROBE_DEEP_INTERVAL``。
          空闲期每 3s 开一条通道 + 让中继停手 8s 是不能接受的（中继会被停成常态）。
        """
        if not self._carrying():
            return True
        if self._plane_state() is False:
            return True
        return time.monotonic() - self._last_deep_at >= self._deep_interval

    def _measure_deep(self, device_id: int) -> tuple[str | None, str]:
        """③④：开一条到 iPad:device_port 的通道，再问一次 SOCKS5 握手。**先让中继停手。**"""
        port = self.device_port
        if self._carrying():
            pauser = self._descend_pause
            if callable(pauser):
                try:
                    pauser()
                except Exception:  # noqa: BLE001
                    log.exception("[bridge] 让中继停手失败（这次下探可能含我们自己的噪音）")
        try:
            s = mux.open_device_tunnel(device_id, port)
        except (MuxError, OSError) as e:
            return f"经 usbmuxd 打通 iPad:{port} 超时（通道繁忙，非链路故障）：{e!r}", LINK_MUX_BUSY
        except DeviceUnknown as e:
            # 结果码 2：不认识这个设备号 —— 那是**本机缓存的设备号过期**，与 iPad 那头的
            # 服务在不在听无关 ⇒ 归到 ②（设备这一层要重新解析），不许念成「端口被拒」。
            return f"设备号已失效（iPad 重新枚举过，缓存需要刷新）：{e}", LINK_DEVICE_CHANGED
        except ConnectRefused as e:
            return (f"经 usbmuxd 打通 iPad:{port} 被拒"
                    f"（iPad 端 SOCKS 引擎可能未在 127.0.0.1:{port} 监听）：{e}"), LINK_PORT_REFUSED
        except RuntimeError as e:
            # 结果码不是 2 / 3：说不出是哪一段就别硬指（上层会报「量不出来」）。
            return f"usbmuxd Connect 未成功（原因不明，不指认段位）：{e}", LINK_MUX_BUSY
        try:
            if not mux.socks5_handshake(s):
                return (f"iPad:{port} 端口可连，但 SOCKS5 握手无应答"
                        f"（引擎卡住或未就绪）"), LINK_ENGINE_SILENT
        finally:
            try:
                s.close()
            except OSError:
                pass
        return None, LINK_OK
