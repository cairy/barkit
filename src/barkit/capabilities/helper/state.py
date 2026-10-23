"""state：观测事实的**内存态** ＋ 一份崩溃恢复快照（用户域）。

它替掉了什么
------------
旧形态里有三份状态文件（``state.json`` / ``tunnel_state.json`` / ``hosts_state.json``），
三份都是**通道**：helper 每轮原子写、app 侧用目录监视或轮询去读。通道换成会话之后，
事实由 ``event.push`` 直接推给订阅方，盘上那三份就没有读者了 —— 按方案 §11.2 全删。

于是本模块只剩两件事：

1. **内存里的最新观测**（``record``）。订阅方要什么推什么；``run_cycle`` 要「上轮
   实际生效态」（网卡回滚、暂停冻结）也从这里取，不再读盘。
2. **一份快照**（``helper_snapshot.json``），只给两个场合看：守护自己崩了重启之后
   判「该不该重新施加」，以及人 ``cat`` 它。

「不必要的读写」在这里怎么落地的
--------------------------------
- **一个文件，不是每 subject 一份**：每份一次 ``tmp + rename``，每次都让目录变两次；
  当年把 app 侧监视从 30 秒一轮抬成 5 秒一轮的就是这个。
- **只在内容真变时写**：旧的「最多静默 120 秒兜底写一次」连同 ``checked_at`` 水位线
  一起取消 —— 那条规则存在的唯一理由是喂「mtime / 时刻戳」这个新鲜度判据，而现在
  新鲜度由**连接本身**回答。稳态下这个文件可以整天一次都不写。
- **不是通道，所以不需要新鲜度**：读它的只有「守护刚启动」和「人」。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, Optional

from .safeio import _atomic_write_json, data_dir, log

#: 领域名（**不是文件路径**：路径永不出现在协议里）。
SUBJECT_ROUTE = "route"
SUBJECT_TUNNEL = "tunnel"
SUBJECT_HOSTS = "hosts"
SUBJECT_TASKS = "tasks"

#: 快照文件名（单个）。
SNAPSHOT_NAME = "helper_snapshot.json"

#: 快照格式版本。**变了就是形状变了**，读到别的版本按「读不出来」处理
#: （快照只用于崩溃恢复，读不出来最多是多收敛一轮，代价是有界且安全的）。
SNAPSHOT_VERSION = 1

#: 最新观测：subject → 最近一次 record 的 dict。**这是「此刻的事实」**。
#:
#: 崩溃恢复快照**不灌这里**（2026-10-17 起）：它描述的是上一次会话结束时的世界
#: （人在办公室、网卡还是那个地址），不是此刻。灌进来之后，开机后第一个连上来的
#: 订阅方会在握手那一刻收到「昨天那份」，app 侧把它当成"现在"（实测：冷启动第一次
#: 启动报了「当前场景 = office」，问了一条本不该问的上班时间）。快照只服务
#: :func:`read_state`（判「该不该重新施加」），那里本来就现读盘。
_OBSERVED: Dict[str, dict] = {}
#: 「最近一次生效的意图」：subject → payload。崩溃恢复时用它判要不要重新施加。
_INTENTS: Dict[str, Any] = {}
#: 上一次落盘的稳定签名（内容没变就不写）。
_LAST_SIGNATURE: Optional[str] = None
#: 推送口（由 core 在起会话时装上：``pusher(subject, observed)``）。
_PUSHER: Optional[Callable[[str, dict], None]] = None
#: 每 subject 上一次**推送**的内容签名（剔除易动字段后比对，见 ``record``）。
_PUSHED_SIG: Dict[str, str] = {}
#: 每 subject 上一次推送的时刻（``time.monotonic()``）：心跳的账，只在真推时刷新。
_PUSHED_AT: Dict[str, float] = {}
#: 上一次**真正写进快照**的各 subject 观测。``forget_observed()`` 把内存那份作废之后，
#: 任何**别的 subject** 的 ``record`` 仍会触发 flush：``subjects`` 取并集、observed 若
#: 只按内存取，route 那节（含 ``net_gateways`` 账，崩溃恢复与重启后叠路由全靠它）会被
#: 顺手写成 ``{}`` —— 2026-10-19 真机：app 重启 ⇒ 断连作废 ⇒ hosts 那轮 flush ⇒
#: 下一轮 ``run_cycle`` 读 prev 没了账 ⇒ 命中后全 ``hold``。故 flush 时内存没有的
#: subject 保留这份（盘上本来就是最新现场）。
_FLUSHED_OBSERVED: Dict[str, dict] = {}
#: 新进程是否已把盘上那份 observed 认作「上次写出的那份」（见 :func:`_seed_flushed`）。
_SEEDED = False

#: 心跳上限（秒）：内容一直没变也要隔这么久**补推一次**。消费方（ipad_tunnel）靠
#: ``checked_at`` 判「守护还在不在跑这一轮」，而**连接活着只说明进程在** —— 少了
#: 这档，稳态下超过消费方的过期阈值（180s）就会误报「守护未回报」。须留在阈值
#: 以内且留足余量（门禁 ``test_heartbeat_stays_well_inside_the_consumer_threshold``）。
PUSH_HEARTBEAT_S = 120.0


def set_pusher(pusher: Optional[Callable[[str, dict], None]]) -> None:
    """装上「观测变了 → 推给订阅方」的口（core 起会话时调；None＝不推）。"""
    global _PUSHER
    _PUSHER = pusher


def snapshot_path():
    return data_dir() / SNAPSHOT_NAME


# ──────────────────────────────────────────────
# 记事实 / 记意图
# ──────────────────────────────────────────────

def record(subject: str, observed: dict) -> None:
    """记下某 subject 的最新观测：进内存 → **内容变了（或心跳到点）才**推给订阅方 →
    快照按内容变没变决定写不写。

    推送与快照用同一套「剔除易动字段」的内容比对：观测里带 ``checked_at`` 这类
    「这次是什么时候看的」时间戳，每个节拍都在动 —— 不去重的话，稳态下每个节拍都
    推一份一模一样的事实，app 侧每个节拍都被点火一轮白跑的 tick（2026-09-30：
    ipad_tunnel 每 5s 刷一次就源于此，事件驱动被推回了轮询）。

    两条账都是「真推了才算」：

    - ``_PUSHED_SIG``：内容真变 ⇒ 立刻推。订阅握手时 core 会把当前值**立刻**推一份，
      故「没有订阅方」视同已送达，照样记账。
    - ``_PUSHED_AT``：内容一直没变也不能永远沉默 —— 超过 ``PUSH_HEARTBEAT_S`` 补推
      一次（带的自然是**此刻**这份观测），喂住消费方的新鲜度判据。
    """
    _OBSERVED[subject] = observed
    now_mono = time.monotonic()
    sig = _subject_signature(subject)
    due = (sig != _PUSHED_SIG.get(subject)
           or now_mono - _PUSHED_AT.get(subject, 0.0) >= PUSH_HEARTBEAT_S)
    if due:
        if _PUSHER is None:
            # 还没有订阅方（会话未起）：订阅握手会把当前值送达 ⇒ 视同「已送达」。
            _PUSHED_SIG[subject] = sig
            _PUSHED_AT[subject] = now_mono
        else:
            try:
                _PUSHER(subject, observed)
            except Exception as e:  # noqa: BLE001 —— 推送失败不该让业务轮停摆
                log(f"推送 {subject} 的观测失败：{e}")
            else:
                _PUSHED_SIG[subject] = sig       # 推成功才算送达（失败下一轮重推）
                _PUSHED_AT[subject] = now_mono
    _flush_snapshot()


def note_intent(subject: str, payload: Any) -> None:
    """记下「某 subject 最近一次生效的意图」（进快照，供崩溃恢复判断）。"""
    _INTENTS[subject] = payload
    _flush_snapshot()


def observed(subject: str) -> dict:
    """某 subject 的**此刻**观测（内存那份）。本进程还没 ``record`` 过 ⇒ 空。

    空 ≠ 「一切正常」：它说的是「这一刻还没有事实」。崩溃恢复快照**不在这里**
    （见 :data:`_OBSERVED` 那条说明）—— 这个口是给订阅方答「现在是什么」的，
    拿上次退出前的世界答它，app 侧会把昨天当成今天。
    """
    return dict(_OBSERVED.get(subject) or {})


def forget_observed() -> None:
    """会话**全断**：内存里那份「此刻」作废。

    观测是「这一轮看到了什么」，没有会话就没有「此刻」。留着它的后果与启动时灌快照
    同宗：下一个连上来的 app 在握手那一刻（``event.push`` 一份当前的）收到的是
    **上一次会话结束时的世界** —— 2026-10-17：今天 09:40 启动拿到昨天 18:26 的
    ``office`` 命中，问了一条本不该问的上班时间。守护长跑时这条比启动灌快照更容易撞：
    app 关了又开，中间没有任何一轮 ``record`` 去覆盖它。

    **不 flush**：这一步要作废的只是**推送**语义里的「此刻」。盘上那份本来就该留着
    （崩溃恢复与 ``read_state`` 都吃它），而本函数自己不 flush 还不够 —— 任何**别的
    subject** 的 ``record`` 都会触发 flush，旧写法会把本 subject 那节顺手写成
    ``{}``。现在 flush 按上次写出的一份兜底（``_FLUSHED_OBSERVED``），这里只管清内存。

    ``_PUSHED_SIG`` 一起清：握手推的是空观测，第一轮 ``record`` 若与上一次会话最后一轮
    内容逐字相同，不清就会判成「没变、不推」⇒ app 永远收不到那份事实。
    """
    _OBSERVED.clear()
    _PUSHED_SIG.clear()
    _PUSHED_AT.clear()


def _subject_signature(subject: str) -> str:
    """单 subject 观测的内容签名（``_stripped`` 剔除易动字段，只比「看到了什么」）。"""
    return json.dumps(_stripped(_OBSERVED.get(subject)),
                      ensure_ascii=False, sort_keys=True, default=str)


def write_state(state: dict) -> None:
    """路由观测（沿用旧调用点，语义变成「记事实」）。"""
    record(SUBJECT_ROUTE, state)


def write_tunnel_state(state: dict) -> None:
    """隧道观测。"""
    record(SUBJECT_TUNNEL, state)


def write_hosts_state(state: dict) -> None:
    """hosts 观测。"""
    record(SUBJECT_HOSTS, state)


def read_state() -> dict:
    """上轮写出的路由观测（**给「该不该重新施加」判，不是给订阅方看的**）。

    两级：本轮真记过 → 现读快照。旧调用点（``routeops.run_cycle`` 取 ``prev_net`` /
    ``prior_applied``）读到的语义一个字没变。

    **快照不进 :func:`observed`**：那个口答的是「此刻是什么」。把上次退出前的世界
    当成此刻推给订阅方，app 侧的场景/网卡读数会晚一轮才纠正（2026-10-17 实测：
    冷启动第一次启动误报「当前场景 = office」，问了一条本不该问的上班时间）。
    """
    if _OBSERVED.get(SUBJECT_ROUTE):
        return dict(_OBSERVED[SUBJECT_ROUTE])
    return dict((load_snapshot().get("subjects", {})
                 .get(SUBJECT_ROUTE, {}) or {}).get("observed") or {})


# ──────────────────────────────────────────────
# 快照
# ──────────────────────────────────────────────

#: 落盘签名里要剔除的**易动字段** —— 它们是「这次是什么时候看的」，不是「看到了什么」。
#:
#: 不剔的后果很实在：``checked_at`` 每轮都在动 ⇒ 每轮都判成「内容变了」⇒ 一天上万次
#: 原子写（每次 tmp + rename，还让目录变两次），而写下去的变化只是时间戳 —— 那正是
#: 「不必要的读写」本身。剔除之后，稳态下一次都不写（本轮结论与上轮逐字相同）。
#:
#: 代价如实说：盘上那份的 ``checked_at`` 是**最近一次真发生变化的时刻**，不是「最后一次
#: 观测的时刻」。按 §11.4 这是可接受的 —— 快照不是通道，读它只有「守护刚启动」与「人
#: ``cat``」两个场合，两处都不需要它保持新鲜。文件顶层另有 ``written_at`` 记「这份是什么
#: 时候写的」。
_VOLATILE_KEYS = ("checked_at", "written_at")


def _stripped(value: Any) -> Any:
    """递归剔除易动字段（只用于**比对**，不改写要落盘的内容）。"""
    if isinstance(value, dict):
        return {k: _stripped(v) for k, v in value.items() if k not in _VOLATILE_KEYS}
    if isinstance(value, list):
        return [_stripped(v) for v in value]
    return value


def _seed_flushed() -> None:
    """新进程第一次 flush 之前，把盘上那份 observed 认作「上次写出的那份」。

    ``_FLUSHED_OBSERVED`` 只记**本进程**真写过的观测，进程一重启就空。而盘上那份
    正是上一个进程最后写出的，语义完全一致。不认它的话，开机后第一帧
    ``note_intent``（app 声明意图）触发 flush，``_composed_observed()`` 对还没
    ``record`` 过的 route 只能给出 ``{}``，把盘上的 ``net_gateways`` 账抹平；
    紧接着 ``run_cycle`` 的 ``read_state()`` 读到空账 ⇒ 场景命中却全 ``hold``
    （2026-10-20 真机：开机后 office 三个目标全 hold）。

    只灌 ``_FLUSHED_OBSERVED``，**不灌** ``_OBSERVED``：后者是「此刻的事实」，
    灌进去就回到 2026-10-17 那个冷启动误报 office 的坑（见 ``_OBSERVED`` 说明）。
    """
    global _SEEDED
    if _SEEDED:
        return
    _SEEDED = True
    for subject, seg in (load_snapshot().get("subjects") or {}).items():
        if isinstance(seg, dict) and isinstance(seg.get("observed"), dict):
            _FLUSHED_OBSERVED[subject] = seg["observed"]


def _composed_observed() -> Dict[str, dict]:
    """快照里每个 subject 实际要写的 observed：内存有的用内存；内存没有的（会话断开
    时作废过）保留**上次写出**的那份 —— 那就是盘上现成的最新现场，写成 ``{}`` 等于把
    崩溃恢复要用的东西抹掉（2026-10-19）。"""
    _seed_flushed()
    subjects = sorted(set(_OBSERVED) | set(_INTENTS) | set(_FLUSHED_OBSERVED))
    return {subject: dict(_OBSERVED.get(subject) or _FLUSHED_OBSERVED.get(subject) or {})
            for subject in subjects}


def _signature() -> str:
    payload = {"subjects": {k: {"intent": _stripped(_INTENTS.get(k)),
                                "observed": _stripped(v)}
                            for k, v in sorted(_composed_observed().items())}}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _flush_snapshot() -> None:
    """内容真变才写盘（原子写）。写失败只记日志，绝不拖垮主循环。"""
    global _LAST_SIGNATURE
    # 新进程先认盘上那份 observed（见 ``_seed_flushed``）：必须在早退判据之前，
    # 否则「内存全空」会被判成「没东西可写」直接返回，连那条兜底也走不到。
    _seed_flushed()
    if not _OBSERVED and not _INTENTS and not _FLUSHED_OBSERVED:
        return
    signature = _signature()
    if signature == _LAST_SIGNATURE:
        return
    # 快照要装的是「最近一次生效的意图 ＋ 最近一次观测结果」（§3.4）。observed 按内存
    # 取但**不写空**：内存被 ``forget_observed()`` 作废过的 subject，保留上次写出的
    # 那份（见 ``_composed_observed``）—— 别的 subject 每隔几秒就会触发 flush，
    # 不兜住的话 route 那节每次 app 重启都被抹平一次。
    payload = {
        "version": SNAPSHOT_VERSION,
        "written_at": int(time.time()),
        "subjects": {
            subject: {"intent": _INTENTS.get(subject),
                      "observed": observed}
            for subject, observed in sorted(_composed_observed().items())
        },
    }
    try:
        _atomic_write_json(snapshot_path(), payload)
    except OSError as e:
        log(f"写快照失败（不影响运行）：{e}")
        return
    _FLUSHED_OBSERVED.clear()
    _FLUSHED_OBSERVED.update(
        {subject: dict(seg["observed"]) for subject, seg in payload["subjects"].items()})
    _LAST_SIGNATURE = signature


def load_snapshot() -> dict:
    """读快照。读不出来（缺失 / 半截 / 版本不符）一律返回空 —— 调用方按「没有」处理。"""
    try:
        data = json.loads(snapshot_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != SNAPSHOT_VERSION:
        return {}
    return data


__all__ = [
    "SUBJECT_ROUTE", "SUBJECT_TUNNEL", "SUBJECT_HOSTS", "SUBJECT_TASKS",
    "SNAPSHOT_NAME", "SNAPSHOT_VERSION", "snapshot_path",
    "set_pusher", "record", "note_intent", "observed", "forget_observed",
    "write_state", "write_tunnel_state", "write_hosts_state", "read_state",
    "load_snapshot",
]
