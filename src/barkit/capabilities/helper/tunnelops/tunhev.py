"""**hev 进程那一族**：yml 渲染（``_HEV_YAML_TEMPLATE`` / ``_gen_hev_yaml``）、
上游探活（``_upstream_ready``）、完整性自检（``_hev_integrity_check``）、起停与保活判定。

「上游在不在」就是「app 的中继在不在」⇒ app 只在链路就绪时监听 ``local_port``，
helper 探那个端口，谁都不用相信一份快照。

``tunnelops.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from .. import rootbin
from ..safeio import (
    _hev_log_path,
    _open_nofollow,
    hev_binary,
    hev_yaml_path,
    log,
)

from . import tunconsts
from . import tunshell
from . import tunwifi


# hev-socks5-tunnel 配置模板（hev commit 9a06bc6，键名已对该二进制 strings 逐一核对）。
#
# ★★ tunnel.name 必须写，否则 hev 在建 utun 时永久阻塞（2026-09-03 实证）★★
# 差分实验（非 root 跑同一份 yml，用 log-level: debug 看它走到哪一步）：
#   · 无 name           → 只打印 "socks5 tunnel init"，随后永久挂起（无报错、无退出）
#   · name: utun90      → 打印 "socks5 tunnel init" → "socks5 tunnel open (Operation
#                          not permitted)"（非 root 的权限错误属预期，说明已走到 open）
#   · name: utun9       → 同上，正常走到 open
# 即：缺 name 时 hev 卡在打开 utun 控制 socket 之前，既不出错也不退出，
# 表现为「进程活着、日志空、永远建不出接口」，helper 遂陷入 20s 超时→重启的循环。
# 成因：hev-tunnel-macos.c 用 sscanf(name, "utun%u", &sc_unit) 解析，缺 name 时
# sc_unit 未初始化。旧注释「不写 name 让系统自动分配」是错的，已按实测纠正。
#
# 另两处不能改，改了就是「hev 起来了但没 utun / 日志全空」：
#   2) misc / mapdns 必须是**顶层**段。缩进到 tunnel 下会被 hev 忽略（它认的
#      顶层段只有 tunnel / socks5 / mapdns / misc），后果是 log-file、log-level、
#      limit-nofile 全部失效——表现为 hev 秒退但日志文件空空如也。
#   3) mapdns **必须**带 address/port（2026-09-03 源码核实，commit 9a06bc6）：
#      hev-socks5-tunnel.c 的 udp_recv_handler 用 `faddr == 包地址 && fport == 端口`
#      做**严格等值**匹配来决定拦截 DNS；缺 address 时 mapdns_address 默认 0
#      （0.0.0.0），而真实包地址不可能是 0.0.0.0 → 匹配永远为假 → mapdns 被
#      静默禁用 → 系统 DNS 设 198.18.0.2 后永远收不到应答（"DNS 无法连通"）。
#      故必须显式写 address: 198.18.0.2 + port: 53，与官方 README / issue #280 一致。
#
# 不写 mtu / ipv4-mask：本构建的配置键表里没有这两个键（strings 核对），
# 写了也是被忽略的死配置，只会误导后续排查。
#
# log-level 用 info 而非 warn：warn 下正常运行完全静默，任何启动期失败都无从
# 取证（hev.log 常年 0 字节）。info 只在启动期打印十余行，代价可忽略。
# 占位符均经 str.format 注入，无 shell 拼接。
_HEV_YAML_TEMPLATE = """\
tunnel:
  name: '{tun_name}'
  ipv4: 198.18.0.1
  ipv6: 'fc00::1'
mapdns:
  address: '198.18.0.2'
  port: 53
  network: '100.64.0.0'
  netmask: '255.192.0.0'
  cache-size: 1024
misc:
  # 不写 task-stack-size：上游 README 里的 24576 是配 tcp-buffer-size 4096 的示例，
  # 本模板的 tcp-buffer-size 是 65536 ⇒ hev 取 max(写入值, 20480 + max(min(65536,
  # lwip 的 TCP_SND_BUF 65528), 1500×udp-copy-buffer-nums 10)) = 86008，写入值从
  # 不生效（只有「以为在调栈尺寸」一条副作用）；删掉即回到上游默认 86016，差 8 字节。
  connect-timeout: 10000
  # 读写超时＝单次读/写等待超过该值仍无字节即杀会话（core 的 hev-socks5-misc.c
  # 打 "io timeout" 并终止）。两值均取上游默认（hev-config.c 未设新键时的回退值）。
  # 勿改回 read-write-timeout: 30000：推送/WebSocket 长连接一静默即被收割，客户
  # 端立刻重连再被收割——2026-10-09 实测 57 分钟 3107 条会话、66% io timeout。
  # 放宽后长连接并发上升 ⇒ 下面的 limit-nofile 必须扛得住 max-session-count。
  tcp-read-write-timeout: 300000
  udp-read-write-timeout: 60000
  log-file: stderr
  log-level: info
  # limit-nofile 才是真正先撞上的天花板：本端每条会话只占 1 个 fd（本地侧走
  # tun 不占 fd，对端就是上面 socks5 那条 TCP —— hev-socks5-session.c 每会话只
  # connect 上游一次），故 512 仅够约 505 条会话，比 max-session-count（1024）
  # 更早生效 —— 两者原本自相矛盾。取 2048 与 iPad 侧 server 的 limit-nofile 对齐
  # （那边每会话 2 fd，折合约 1020 条连接，是两端里最紧的一环）。
  # 撞系统硬上限时 set_limit_nofile 只是失败并打一条 info 日志（hev-main.c），
  # 停留在系统允许的最大值，不会比旧值更差。
  limit-nofile: 2048
  tcp-buffer-size: 65536
  max-session-count: 1024
socks5:
  address: 127.0.0.1
  port: {local_port}
  pipeline: true
  # 显式钉死 FWD UDP（UDP-in-TCP，CMD=0x05：UDP 报文封进已有的 SOCKS5 TCP 连接）。
  # 语义：hev 只在取值恰为 'udp' 时置 udp_in_udp=1（hev-config.c:251），其余 —— 含
  # 键缺失，靠 memset 归零（同文件 467 行）—— 都落到本模式 ⇒ 本键与不写行为逐字节
  # 相同，加它只为不让上游默认值随版本漂移（上游 README:111 把 `udp: 'udp'` 排成
  # 未注释样例，极易被误读成默认值）。真实动机：usbmuxd 只提供 TCP 隧道，标准 UDP
  # ASSOCIATE 要客户端把报文发到服务端返回的中继地址，经 usbmuxd 不可达。
  # 换 hev 二进制时必须重验键名 —— 键不在配置键表里会被静默忽略（同 mtu 教训）。
  udp: 'tcp'
"""

# 隧道接口名。hev-tunnel-macos.c 会 sscanf 出 unit 后 +1（即 utun90 → 请求 unit 91），
# 具体是否 +1 以实测为准；无论最终叫什么，_find_utun() 都是按「哪个 utun 上有
# 198.18.0.1」来发现，不依赖名字匹配，所以取一个高位空闲号即可。
# utun0..5 常被系统 VPN / Back to My Mac 占用，故避开低位。
HEV_TUN_NAME = "utun90"


def _disable_ipv6(state: dict) -> None:
    """禁用目标网络服务的 IPv6（防 v6 经本机网卡泄漏出网，design §13-1）。

    读不出服务名时**不动手**并如实上报：把 IPv6 关到一个无关的服务上，真正在用的
    那个仍在泄漏 —— 看着做了事，实际一点没防住。
    """
    svc = tunwifi._network_service()
    if svc is None:
        state["service_unknown"] = True
        log("IPv6 未禁用：读不出当前网络服务名（IPv6 可能经本机网卡出网）",
            key="tunnel.ipv6_no_service")
        return
    tunshell._run(["/usr/sbin/networksetup", "-setv6off", svc])
    tunconsts._TUNNEL_IPV6_OFF = True


def _gen_hev_yaml(data: dict) -> Path | None:
    """生成 hev 配置。写不了返回 None（原因已记日志）——宁可降级也不退化到
    用户可写目录，否则又把符号链接攻击面打开了。"""
    path = hev_yaml_path()
    # 所有占位符均为已校验过的数值/IP，无 shell 拼接。
    #
    # 注：mapdns 的 network/netmask 已改为模板里的固定 CGNAT 段（100.64.0.0/10），
    # 不再由 dns 地址推导。旧做法把 fake IP 段设成 198.18.0.0/24 —— 与隧道自身
    # 子网（tunnel.ipv4=198.18.0.1）完全重叠，fake IP 会和 tun 地址、DNS 服务地址
    # 撞车，域名解析即使拿到 IP 也无法正确回落。hev ≥2.14.3 官方默认即 100.64.0.0/10。
    text = _HEV_YAML_TEMPLATE.format(
        local_port=data["local_port"],
        tun_name=HEV_TUN_NAME,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _open_nofollow(path, mode=0o644) as f:
            f.write(text)
    except OSError as e:
        log(f"写 hev 配置 {path} 失败：{e}")
        return None
    return path


def _upstream_ready(port: int, timeout: float = 1.0) -> bool:
    """探活上游 SOCKS 代理（app 侧 bridge @ 127.0.0.1:port）。

    hev 启动时要求该端口已在监听，否则会无限阻塞等上游、永远建不出 utun。
    在 _start_hev 之前先确认，避免起一个空等进程 + 每分钟重启的死循环。
    """
    import socket
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _hev_integrity_check(path) -> "tuple[bool, str | None]":
    """hev **执行前**完整性自检（§7.4.5 P1）：存在 + owner/权限 + 在册 + 签名结构。

    **绝不做哈希比对**：hev 安装时经 ``codesign -s -`` ad-hoc 签名，而该命令会
    改写文件字节且**不可复现**（实测同一输入两次签名哈希不同），故发行态 sha256
    只能用于**安装期**校验源文件。运行期守卫是
    ``codesign --verify``（签名结构完整 + identifier + owner/权限）——它防的是
    半成品 / 截断 / 拷贝中断 / 权限漂移 / 升级间隙，**不抗重签**（任何本地进程都能
    对篡改后的二进制重新 ad-hoc 签名，§9.3 已写死该定位，别高估它）。

    失败即**拒绝启动 hev 并拆除隧道**（不降级、不硬起）。
    """
    p = Path(path)
    if not p.exists():
        return False, "hev-socks5-tunnel 二进制未安装（请在 App 内重装守护）"
    ok, reason = rootbin.check_owner_perms(p)
    if not ok:
        return False, reason
    manifest, err = rootbin.load_manifest()
    if err is not None:
        return False, err
    ok, reason = rootbin.verify_registered(p.name, manifest)
    if not ok:
        return False, reason
    try:
        proc = subprocess.run(
            ["/usr/bin/codesign", "--verify", str(p)],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"codesign --verify 无法执行：{exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:200]
        return False, f"codesign --verify 失败（签名结构不完整）：{detail}"
    return True, None


def _start_hev(data: dict) -> bool:
    if _hev_alive():
        return True          # 已在跑：绝不启动第二个，否则旧实例变孤儿抢 198.18.0.1
    yml = _gen_hev_yaml(data)
    if yml is None:
        return False
    # hev-socks5-tunnel 以「位置参数」接收配置文件，不认 -c / -f 等 flag；
    # 误用 -c 会让 hev 尝试打开名为 "-c" 的文件而立即退出（Open -c failed!），
    # 表现成「hev 已启动但未发现 utun 接口」。配置必须作为位置参数传入。
    # 同时把 stdout/stderr 落盘，原 DEVNULL 会吞掉 hev 的真实报错。
    try:
        _hev_log_path().parent.mkdir(parents=True, exist_ok=True)
        logf = _open_nofollow(_hev_log_path(), mode=0o644)
    except OSError as e:
        log(f"打开 hev 日志失败（输出将被丢弃）：{e}")
        logf = subprocess.DEVNULL
    try:
        tunconsts._TUNNEL_PROC = subprocess.Popen(
            [str(hev_binary()), str(yml)],
            stdout=logf, stderr=logf,
        )
    except OSError as e:
        log(f"启动 hev 失败：{e}")
        tunconsts._TUNNEL_PROC = None
        return False
    log(f"hev 已启动（pid={tunconsts._TUNNEL_PROC.pid}），日志见 {_hev_log_path()}")
    return True


def _hev_alive() -> bool:
    return tunconsts._TUNNEL_PROC is not None and tunconsts._TUNNEL_PROC.poll() is None


def _stop_hev() -> None:
    if tunconsts._TUNNEL_PROC is not None:
        try:
            tunconsts._TUNNEL_PROC.terminate()
            tunconsts._TUNNEL_PROC.wait(timeout=5)
        except Exception:
            try:
                tunconsts._TUNNEL_PROC.kill()
            except Exception:
                pass
        tunconsts._TUNNEL_PROC = None
