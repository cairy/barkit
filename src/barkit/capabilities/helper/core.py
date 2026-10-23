"""core：BarKit root helper 的入口与主循环（会话版，§7.4）。

职责边界（薄入口，S3 档 B）
-------------------------
- ``main()``：启动完整性自检 → 装配生命周期钩子 → 起会话 → 进调度循环
- 会话（收帧 / 对端校验 / 推送）→ ``session``，线格式 → ``protocol``
- 生命周期（退出钩子 / 信号 / 崩溃残留回收）→ ``lifecycle``
- 编排层（哪条 task 什么时候跑）→ ``tasks``
- 原语分发（能力 → 实现）→ ``primitives``
- 业务机制（路由 / 隧道 / hosts 的系统状态变更）→ ``routeops`` / ``tunnelops`` / ``hostsops``

本模块**不含任何业务决策**：它只回答「这一帧该交给谁」。

主循环长什么样（2026-09-29 重写）
--------------------------------
```
while True:
    timeout = runner.seconds_until_due()      # 没有活跃意图 ⇒ None（纯阻塞）
    server.serve(timeout)                     # 阻塞在 selectors 上，一次都不醒
    runner.run_due()                          # 跑到期的 task
```
旧循环是 ``while True: 查 trigger 文件 → 跑到期 → sleep(1)`` —— 空闲一天醒 86400 次。
新循环空闲时**一次都不醒**（内核阻塞在 ``poll``，不耗 CPU、也不把处理器拉出低功耗态）。
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

from . import lifecycle, primitives, protocol, state, tasks
from .safeio import data_dir, helper_dir, log
from .session import SessionServer
from .state import write_state
from .version import HELPER_VERSION

#: 会话节点在用户私有数据根下的名字（与 app 侧 ``paths`` 里那个常量同名同义）。
SOCKET_NAME = "helper.sock"

#: 应用一断连就撤的 subject（**依附型**）。其余（route / hosts / tasks）是自持型：
#: 它们表达的是「系统该保持成这样」，app 死了也不该把用户的网络配置一把推平。
EPHEMERAL_SUBJECTS = ("tunnel",)


def socket_path():
    return data_dir() / SOCKET_NAME


def allowed_uid() -> Optional[int]:
    """允许连进来的 uid。

    优先取安装期写进 plist 的 ``BARKIT_UID``（确定且不依赖目录状态）；没有就退到
    **数据目录的属主** —— 那个目录只有被服务的用户能进，属主就是我们要找的人。
    两者都拿不到就返回 ``None``＝不校验（并**记一条警告**：那等于把身份这道闸关掉，
    只在开发/测试的裸跑场景下才会发生）。

    ⚠️ 第二条只在**目录是被服务者自己建的**时才成立。守护开机先起、目录还不存在时
    调用它，目录属主那一条必然取不到（``os.stat`` 抛错）⇒ 退化成 ``None`` ⇒ 调用方
    不得据此 chown。别把本函数的调用挪到建目录**之后**：那样读到的是本进程刚建出来
    的那个目录（属主＝root＝自己），会把整棵数据目录锁成 root:root 0700 —— app 从此
    连不上会话也写不进配置，且**非 root 改不回来**（只能重装守护自愈）。
    """
    env = os.environ.get("BARKIT_UID")
    if env and env.isdigit():
        return int(env)
    try:
        uid = int(os.stat(data_dir()).st_uid)
    except OSError:
        log("无法确定允许连入的用户（既无 BARKIT_UID，数据目录也不存在）：身份校验停用")
        return None
    if uid == 0:
        # 属主是 root ⇒ 那个目录多半就是本进程刚建出来的（见 ``_ensure_data_dir``）。
        # 把「允许 uid=0」当成答案是**自证**：它读到的正是自己。宁可如实说「不知道」
        # （身份校验停用、app 连不上，重装守护即归位），也不要把推测写成肯定句 ——
        # 那样日志里看着有答案，排查的人会被带到完全错误的方向。
        log("数据目录属主是 root（多半由守护自己建出）⇒ 无法确定被服务的用户："
            "身份校验停用；重装守护即可归位")
        return None
    return uid


def _ensure_data_dir() -> None:
    """数据根不存在就建出来，并**把属主改成被服务的用户**。

    守护在开机时先起（app 可能几小时后才开），此时目录往往还不存在。以 root 建的
    目录属主是 root，app 之后往里写配置就会失败 —— 故建完立刻 chown。

    ⚠️ ``allowed_uid()`` 必须在 ``mkdir`` **之前**调（见它的 docstring）：目录还不
    存在时，「目录属主」那条兜底必然取不到，于是只有 plist 里的 ``BARKIT_UID``
    给得出答案。拿不到就**不 chown**（fail-closed）—— 猜一个 uid 改属主，猜错时
    app 连不上也写不进，而错的那个结果**当场看不出来**（要等 app 起来才发作）。
    属主不对的目录由**重装守护**归位（安装脚本以 root 建并 chown，幂等）。
    """
    d = data_dir()
    if d.exists():
        return
    uid = allowed_uid()
    try:
        d.mkdir(parents=True, exist_ok=True)
        if uid is not None:
            os.chown(str(d), uid, -1)
        else:
            log(f"数据目录由守护建出，但无法确定应归哪个用户（plist 缺 BARKIT_UID）："
                f"{d} 属主为 root，app 将写不进配置 —— 重装守护即可归位")
        os.chmod(str(d), 0o700)
        log(f"数据目录不存在，已由守护建出{'并归位' if uid is not None else ''}：{d}")
    except OSError as e:
        log(f"建数据目录失败：{e}")


def _report_and_stop(reason: str) -> int:
    """自检未过的停机路径：状态上报 + 以 **rc=0** 干净退出。

    为何 rc=0：plist 的 ``KeepAlive={SuccessfulExit: false}`` 只在**非 0 退出**时
    重新拉起。rc=0 = launchd 认定正常结束 → 不拉活，避免「自检失败 → 拉起 → 再
    失败」的崩溃循环（ThrottleInterval 只能把循环拉慢，不能消除）。语义仍是停机：
    daemon 不再运行，面板据此报「需重装守护」。
    """
    message = f"helper 完整性自检未通过：{reason}（请在 App 内重装守护）"
    log(f"启动自检未通过，停止运行（不自动复原、不降级）：{reason}")
    write_state({
        "ok": False,
        "checked_at": int(time.time()),
        "poll": 0,
        "error": message,
        "paused": False,
        "has_intent": False,
        "rules": [],
    })
    return 0


def self_check() -> "tuple[bool, str | None]":
    """启动完整性自检（§7.4.5 P1）：owner/权限 + 源码聚合哈希比对安装期基线。

    只覆盖 **helper 自身源码**（整目录 ``*.py``，不签名 → 哈希可复现）。hev 是
    ad-hoc 签名的 Mach-O，签名改写文件字节使哈希不可复现，故它的守卫是**执行前**
    自检（``tunnelops._hev_integrity_check``）而非启动自检。

    失败语义 = **停机 + 上报 + 提示重装**，**不自动复原**（复原需自带副本，副本
    本身成为新的被攻击资产，且 root 自愈逻辑风险高）。
    """
    from . import rootbin

    manifest, reason = rootbin.load_manifest()
    if reason is not None:
        return False, reason
    ok, reason = rootbin.check_owner_perms(helper_dir())
    if not ok:
        return False, reason
    return rootbin.verify_tree_against_manifest(
        rootbin.HELPER_ARTIFACT_NAME, helper_dir(), manifest
    )


# ──────────────────────────────────────────────
# 会话：一帧交给谁
# ──────────────────────────────────────────────

class _Dispatcher:
    """把会话帧翻成编排层的动作。**这里没有业务判断**，只有「这帧归谁」。"""

    def __init__(self, server: SessionServer, runner: "tasks.Runner") -> None:
        self.server = server
        self.runner = runner
        #: subject → **持有写者身份的那条连接**（方案 §9：一个 subject 一个写者）。
        #: 不立这条账时，「谁在写」只能靠「谁最后发了一帧」⇒ 两个客户端互相覆盖；
        #: 更具体的后果是 **A 断开会把 B 正在用的隧道撤掉** —— A 名下记着 tunnel，
        #: 而那其实是 B 声明的。有了它，第二个写者要么被拒、要么显式接管。
        self._owners: Dict[str, Any] = {}

    # ---------- 连接生命周期 ----------

    def on_close(self, conn) -> None:
        """连接一断：撤掉**它名下**的依附型意图，再交还它的写者身份。

        顺序要紧：``intents`` 只在**自己还是写者**的那段里说得上话，而交还身份之后
        别的连接才能接手 —— 反过来做会让一条刚断开的连接继续挡着新写者。
        （自持型意图不撤，见 ``EPHEMERAL_SUBJECTS``：它们说的是「系统该保持成这样」。）
        """
        for subject in [s for s in getattr(conn, "intents", ()) if s in EPHEMERAL_SUBJECTS]:
            if self.runner.intent(subject) is not None:
                self.runner.set_intent(subject, None)
                log(f"会话断开：撤回依附型意图 {subject}", key=f"intent.withdraw.{subject}")
        self._release_all(conn)
        # 最后一条连接也走了 ⇒ 内存里那份「此刻的观测」不再有效。留着它，下一个 app
        # 连上时会在握手那一刻收到**上一次会话结束时的世界**（2026-10-17：冷启动误报
        # 「当前场景 = office」，问了一条本不该问的上班时间）。快照照旧在盘上，崩溃
        # 恢复判「该不该重新施加」读的是它（``read_state``），不靠内存。
        #
        # 问不到连接集合（离屏桩 / 未来别的 server 实现）时**不清**：多客户端下清早了
        # 会让仍在用的那一个下一拍收到空观测；这一格的价值不值得赌那件事。
        connections = getattr(self.server, "connections", None)
        if callable(connections) and not connections():
            state.forget_observed()

    def _release_all(self, conn) -> None:
        """交还这条连接名下的全部写者身份（断开 / 被接管时调）。"""
        for subject in [s for s, owner in self._owners.items() if owner is conn]:
            del self._owners[subject]
        conn.intents.clear()

    # ---------- 帧分发 ----------

    def handle(self, conn, msg: Dict[str, Any]) -> None:
        kind = protocol.message_type(msg)
        if kind != protocol.HELLO and not conn.client:
            # **握手是唯一一次对契约版本的地方**。没握手的连接版本未知，它发来的
            # 业务帧若照本版协议去解释，等于默认「它说的形状我认得」—— 而意图帧
            # 会真的去改路由 / 隧道 / hosts。不设这道闸，版本闸门就成了「想起来
            # 才对一次」：绕过去的办法只是少发一帧 hello。
            # 只拒这一帧、不断连：不发 hello 的连接另有 ``HELLO_TIMEOUT_S`` 回收。
            self.server.send(conn, protocol.error(
                protocol.REASON_NO_HANDSHAKE,
                detail="%s：尚未握手，请先发 hello（对端协议版本未确认）" % kind,
                req_id=msg.get("req_id")))
            return
        if kind == protocol.HELLO:
            self._on_hello(conn, msg)
        elif kind == protocol.INTENT_SET:
            self._on_intent(conn, msg)
        elif kind == protocol.SUBSCRIBE:
            self._on_subscribe(conn, msg)
        elif kind == protocol.UNSUBSCRIBE:
            subjects = msg.get("subjects")
            if isinstance(subjects, list):
                conn.subscriptions.difference_update(str(s) for s in subjects)
        elif kind == protocol.GET:
            self.server.send(conn, protocol.reply(
                str(msg.get("req_id") or ""), ok=True,
                result=state.observed(str(msg.get("subject") or ""))))
        elif kind == protocol.OP_RUN:
            self._on_op(conn, msg)
        else:
            # 不认识的类型只拒**这一帧** —— 旧契约的「未知字段整份拒」是文件格式的
            # 产物（一份坏文件没有「哪一行不算」的概念），帧级协议不需要它。
            self.server.send(conn, protocol.error(
                protocol.REASON_BAD_TYPE, detail=str(kind),
                req_id=msg.get("req_id")))

    def _on_hello(self, conn, msg) -> None:
        peer_protocol = msg.get("protocol")
        if peer_protocol != protocol.PROTOCOL_VERSION:
            reason = ("协议版本不符：本机守护 v%d，对端 %r —— 两侧版本不一致，"
                      "请重装守护或升级 App" % (protocol.PROTOCOL_VERSION, peer_protocol))
            log(f"拒绝握手：{reason}")
            self.server.send(conn, protocol.welcome(
                ok=False, reason=reason, helper_version=HELPER_VERSION))
            return
        conn.client = str(msg.get("client") or "")
        self.server.send(conn, protocol.welcome(
            ok=True, helper_version=HELPER_VERSION,
            subjects=list(tasks.SUBJECTS) + [state.SUBJECT_TASKS]))

    def _on_intent(self, conn, msg) -> None:
        subject = str(msg.get("subject") or "")
        if subject not in set(tasks.SUBJECTS) | {state.SUBJECT_TASKS}:
            self.server.send(conn, protocol.error(
                protocol.REASON_BAD_REQUEST, detail="未知 subject：%r" % subject))
            return
        if not self._claim(conn, subject, msg):
            return
        payload = msg.get("payload")
        if subject == state.SUBJECT_TASKS:
            self.runner.apply_table(payload if isinstance(payload, dict) else None)
        else:
            self.runner.set_intent(subject, payload)
        state.note_intent(subject, payload)

    def _claim(self, conn, subject: str, msg: Dict[str, Any]) -> bool:
        """判定这一帧能不能写 ``subject``，能就登记写者身份。返回 False 表示已拒。

        一个 subject 只允许一个写者（方案 §9）。五件事在这里说清：

        - **同一个写者反复声明**（改期望态 / 幂等重发）永远放行 —— 那是正常使用，
          把它拒了等于「意图一旦声明就再也改不动」。
        - **第二个写者默认被拒**，回 ``already_claimed`` 并点名当前写者是谁。这一条
          是「不许两个客户端互相覆盖」的落点：不拒的话，谁的期望态算数取决于谁后发。
        - **同一个 client 重连视为同一写者**（自动接管，不用带 ``takeover``）：app
          断线重连时，守护那一轮 ``select`` 可能先处理新连接、后处理旧连接的 EOF，
          「新连接」于是被「还没被回收的自己」挡住 —— 那不是两个客户端，是同一个
          客户端在重新声明，挡住它就等于意图再也声明不上了。``client`` 为空（还没
          握手）不认这条：否则所有不握手的连接会被当成同一个写者。
        - **不同 client 想换人必须显式 ``takeover=true``**，并且**要告诉原写者**它
          已经不是写者了 —— 不告知的话它会一直以为自己还在管着这件事，直到几小时
          后自己发下一帧才发现被拒。
        - **撤回（``payload is None``）即交还身份**：撤回之后那个 subject 没有期望
          态了，不该继续被一条连接占着。
        """
        owner = self._owners.get(subject)
        if owner is not None and owner is not conn:
            same_client = bool(conn.client) and conn.client == owner.client
            if not same_client and msg.get("takeover") is not True:
                self.server.send(conn, protocol.error(
                    protocol.REASON_CLAIMED,
                    detail=("%s 的写者是另一条会话连接（client=%r uid=%d pid=%d）；"
                            "若确要接管请带 takeover=true"
                            % (subject, owner.client, owner.peer.uid, owner.peer.pid)),
                    subject=subject))
                return False
            owner.intents.discard(subject)
            log(f"会话接管：{subject} 的写者换成 client={conn.client!r} 的那条连接",
                key=f"intent.takeover.{subject}")
            self.server.send(owner, protocol.error(
                protocol.REASON_TAKEOVER,
                detail="%s 的写者身份已被另一条连接接管" % subject,
                subject=subject))
        if msg.get("payload") is None:
            # 撤回：交还身份（subject 从此无人认领），但先让它跑到收尾那一轮。
            self._owners.pop(subject, None)
            conn.intents.discard(subject)
            return True
        self._owners[subject] = conn
        conn.intents.add(subject)
        return True

    def _on_subscribe(self, conn, msg) -> None:
        subjects = msg.get("subjects")
        if not isinstance(subjects, list):
            self.server.send(conn, protocol.error(
                protocol.REASON_BAD_REQUEST, detail="subjects 需为数组"))
            return
        for subject in subjects:
            subject = str(subject)
            if not subject:
                continue
            conn.subscriptions.add(subject)
            # 订阅即**立刻**推一份当前的：面板一打开就该有事实，不必等下一次变化。
            # 还没有观测过就推空对象 —— 「没有事实」也是一条事实。
            self.server.send(conn, protocol.event_push(subject, state.observed(subject)))

    def _on_op(self, conn, msg) -> None:
        req_id = str(msg.get("req_id") or "")
        op = str(msg.get("op") or "")
        # ``self.runner`` 只给 ``resync`` 那条通用命令用（「现在按我已声明的意图跑
        # 一轮」）：那份意图只在编排层手里，命令里不重复带一份。
        result, err = primitives.run_op(op, msg.get("params"), self.runner)
        self.server.send(conn, protocol.reply(req_id, ok=err is None,
                                              result=result, error=err or ""))


# ──────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────

def run_loop(runner: "tasks.Runner", server: SessionServer) -> None:
    """会话 + 调度合流的主循环（空闲零唤醒）。"""
    while True:
        server.serve(runner.seconds_until_due())
        runner.run_due()


def main() -> int:
    ok, reason = self_check()
    if not ok:
        return _report_and_stop(reason or "未知原因")
    log(f"BarKit root helper v{HELPER_VERSION} 启动（data={data_dir()}）")
    # 退出钩子必须早于任何网络改动装上：此后即便被 SIGTERM/崩溃打断，
    # 也能把 /1 路由、系统 DNS、IPv6 复原，不至于把用户的网搞坏。
    lifecycle._install_exit_hooks()
    # SIGKILL / 断电等钩子兜不住的场景，靠启动时的标记回收
    lifecycle._recover_stale_tunnel()

    _ensure_data_dir()

    runner = tasks.Runner(primitives.run_sync)
    server = SessionServer(socket_path(), handler=lambda c, m: None,
                           allowed_uid=allowed_uid())
    dispatcher = _Dispatcher(server, runner)
    server.handler = dispatcher.handle
    server.on_close = dispatcher.on_close
    state.set_pusher(server.push)
    lifecycle._register_server(server)
    try:
        server.open()
    except OSError as e:
        # 起不来就没有通道 —— 记清楚原因（路径过长 / 权限），继续空转不如如实报
        log(f"会话节点建立失败，守护将收不到任何声明：{e}")
    else:
        log(f"会话已就绪：{socket_path()}（允许 uid={allowed_uid()}）")
    run_loop(runner, server)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
