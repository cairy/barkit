# iPad Tunnel 插件设计文档

> 状态：已实现 + 真机验证（M1–M4 完成；`HELPER_VERSION` 当前见 `capabilities/helper/version.py`）
> **修订（2026-09-29）**：`tunnel.json` 契约升到 **v2** —— 去掉 `link_up` / `ignore_link` 两位
>（**链路事实从那以后由端口承担**，见 §4.1），并加了真锁。本节下列各处已按新口径改写。
> **同日第二处**：面板状态机补两格（`building` 建立中 / `daemon_stale` 守护未回报）、打开
> 框架的失联判定（`stale_after`）、启动时立刻敲门叫 helper 跑一轮 —— 三件事治的都是
> 「面板显示的不是此刻的事实」；`pending` / `checked_at` 的消费口径见 §4.2。
> **同日第三处（2026-09-29）**：**通道换成会话** —— `tunnel.json` 与 `tunnel_state.json`
> 两份文件**都不存在了**。期望态是会话上 ``intent.set("tunnel", …)`` 的一帧，
> 观测由 ``event.push("tunnel", …)`` 推给订阅方；「立刻跑一轮」要么是连接本身、
> 要么是 ``op.run("resync")``。本文下列各处已按新口径改写，架构总览见
> `docs/architecture/主框架架构-通信模型.md`。
> 位置：本文件随插件代码同目录存放（`plugins/ipad_tunnel/design.md`），
> 便于插件整体迁移 / 改造 / 删除时自洽，不依赖仓库 `docs/design/` 的悬空引用。
> 关联：上游 `ipad-iphone-second-monitor-ios12-free` 项目已将 iPad 端 SOCKS5 引擎升级为
> hev-socks5-server（协程版，默认监听 `127.0.0.1:9001`，经 USB/usbmuxd 可达）。
> 本插件让 BarKit 把 **整台 Mac 的流量经 iPad 的 Wi-Fi 出口** 借道转发（全量 `/1` 默认路由）。

> **Wi-Fi 切换约定（已确认）**：`switch_wifi` 动作**仅切换到本机已配置 / 已知的 Wi-Fi 网络**，
> **绝不托管或配置密码**——命令为 `networksetup -setairportnetwork <if> <ssid>`（不带密码）。
> 目标 SSID 必须是 Mac 已在钥匙串 / 首选列表里连过的网络；若未配过，命令会失败并在
> 观测的 `last_action.ok=false` 中体现（用户需先在本机手动连过一次该 Wi-Fi）。密码
> 不写进隧道意图正文或用户 YAML，避免凭证落盘。

---

## 1. 范围与已确认决策

| 维度 | 决策 |
| --- | --- |
| 流量范围 | **全量默认路由经 iPad**（两条 `/1`，最长前缀优先，与 route_guard 的静态规则天然共存） |
| 特权操作执行者 | **扩展现有 `routed-helper`**（route_guard 的 root helper），不新建第二个守护，避免二次授权 |
| 启用方式 | **手动开关 + 插上 iPad USB 自动启用**（复用 bridge 的 usbmuxd `ListDevices` 探测） |
| 二进制分发 | **随 App 打包** `hev-socks5-tunnel`（安装时拷至 helper 目录 + `xattr -d` 解 quarantine） |
| Wi-Fi 切换时序 | 激活：`iPad 链路就绪 → 起隧道 → 最后切 Wi-Fi`；停用：严格反向（见 §7） |
| 切到哪个 SSID | **可配置**（激活 SSID / 恢复 SSID 各一组，写在用户配置里）；**仅切本机已配置/已知的网络，不托管密码** |
| 切换后钩子 | 状态翻转时切 Wi-Fi（**固定 `wifi` 字段**，无 `type` 分发、不可扩展） |
| IPv6 处理 | **fail-closed（可配置，默认开启）**：隧道激活期间可选禁用 Mac Wi-Fi 的 IPv6，杜绝经本机 Wi-Fi 泄漏（iPad 当前无 v6，v6 在隧道期不可用，符合预期）；设置项 `disable_ipv6_on_activate` 关闭时不禁用，但 IPv6 将经本机 Wi-Fi 泄漏（用户知情选择） |

---

## 2. 总体架构

```
┌──────────────────────────────────────────────────────────────────────┐
│  BarKit 进程（用户态，无 root）                                          │
│                                                                        │
│  plugins/ipad_tunnel/plugin.py  (Plugin(AutomationCapability), RUN_PERIODIC/FAIL_LOG) │
│    ├─ tick()：取守护推来的那份观测（住在内存里）→ 刷面板               │
│    ├─ bridge 线程：经 usbmuxd 把 127.0.0.1:1080 ↔ iPad:9001 桥接      │
│    │    （纯 Python selectors 事件循环，移植自 mac_socks_bridge.py）   │
│    └─ declare("tunnel", …)：声明期望态（**不写任何文件**）              │
│                                                                        │
│  面板各显示位：观测由框架分发给订阅方（subjects() / on_observed）        │
└───────────────┬───────────────────────────────────┬──────────────────┘
                │ intent.set("tunnel", …) 一帧          │ event.push("tunnel", …) 推观测
  ▼ ▲
        AF_UNIX 本机会话（用户私有域、模式 0600）        │
        app 是客户端、helper 是服务端；连上即零定时器    │
                │                                      ▲
                ▼                                      │
┌──────────────────────────────────────────────────────────────────────┐
│  barkit-helper（root LaunchDaemon，label com.barkit.helper，三家共用）    │
│                                                                        │
│  tunnel.sync（task 表驱动，<5s 一轮；无活跃意图时线程纯阻塞）：           │
│    1. 取最近那次 intent.set("tunnel", …) 的正文 + 探上游端口在不在听   │
│    2. 要建但上游没人听 → 保持 pending（不起 hev，防「起一个空等进程」） │
│    3. 要建且上游有人在听 → 拉起 hev-socks5-tunnel（生成其 yaml）        │
│                        → 动态发现 utunN → 加两条 /1 路由               │
│                        → 系统 DNS 设 198.18.0.2（mapdns.address）       │
│                        → 状态翻转时切 Wi-Fi（固定 wifi 字段）          │
│    4. 结论进内存并推给订阅方（快照只在内容真变时落盘）                    │
└───────────────┬───────────────────────────────────┬──────────────────┘
                │ 拉起 / 监管                          │ switch_wifi（networksetup）
  ▼ ▼
  hev-socks5-tunnel (utunN) Wi-Fi 接口（en0）
                │ 全流量经 utun                    │
                ▼                                  │
        iPad USB(usbmuxd) → iPad:9001(hev) ────────┘  (iPad 端已就绪)

 注：route_guard / hosts_guard 的意图经**同一条会话**进来，由各自的 task 处理，互不干扰。
```

---

## 3. 组件拆分

### 3.1 App 侧（新增 `src/barkit/plugins/ipad_tunnel/`）

| 文件 | 职责 | 复用/对照 |
| --- | --- | --- |
| `plugin.py` | `class Plugin(AutomationCapability)`，RUN_PERIODIC / FAIL_LOG；`id="ipad_tunnel"`。贡献状态行、`HeaderChip`、设置页；`tick` 取守护推来的观测；`automation_active` 映射隧道 active 态 | 结构镜像 `route_guard/plugin.py` |
| `bridge.py` | 把现有 `tools/mac_socks_bridge.py` 改造成可 import 的模块（`Bridge` / `DeviceResolver` / `list_devices` / `pick_device` / `open_device_tunnel`）。**不删**原脚本，原脚本继续用于手动测试 | 纯标准库，无 root |
| `tunnel_spec.py` | 生成 + 校验**隧道意图正文**（对照 `route_guard/rules.py` 的 `build_routes`） | 意图的生产方，**不落盘** |
| `config_dialog.py` | 设置页：启用开关、端口、**IPv6 禁用开关（fail-closed）**、劫持全量 IPv4 路由开关、劫持系统 DNS 开关、激活/恢复 SSID（可留空）、安装/卸载守护、立即检查；外加 `TunnelDiagnosticDialog`（设置页「运行诊断」打开）：可输入测试 IP/域名（默认 `8.8.8.8`/`example.com`），依次跑 6 步诊断（iPad 就绪 / utun / 路由劫持 / 路由测试 / DNS 劫持 / DNS 测试），流式展示每步的命令·输出·评估。**所有命令只读、用户态可跑，不依赖 root helper**。 | 结构镜像 `route_guard/config_dialog.py`；诊断逻辑在独立 `diagnostics.py`（纯函数 + 可注入 runner，便于单测） |
| `__init__.py` | 暴露 `daemon`（install/uninstall/status 复用 route_guard 的，见 §9） | — |

插件自动发现：registry 用 `pkgutil` 扫 `barkit.plugins.<name>.plugin`，新建 `ipad_tunnel/plugin.py` 即自动注册，无需改注册中心。

### 3.2 Helper 侧（`capabilities/helper/` 框架资产 + `route_guard/daemon.py`）

- `helper.py`：新增 `run_tunnel_cycle()`，在主循环每轮调用；`HELPER_VERSION` 曾为 `2.4`（隧道功能引入时升版，2.4 修复 mapdns 缺 address 导致 DNS 拦截被静默禁用；改了 helper 必须 bump 版本号，否则 app 经 `daemon.status()['outdated']` 检测不到、不会提示重装）。保留 routes 原有逻辑不变。**批次 4 起 helper 是框架资产**：源码整目录迁入 `capabilities/helper/`（`python3 -m helper` 整包部署），隧道业务机制在 `tunnelops.py`、由 task 表（`tunnel.sync`）调度，版本锚点为 `capabilities/helper/version.py::HELPER_VERSION`。
- `daemon.py`：**A 类二进制对账部署（§9.3）**——`install(sources, missing)` 按框架解析出的插件声明并集部署 `hev-socks5-tunnel` 到 `SYS_HELPER_DIR`（`install -m 0755` + `chown root:wheel` + `xattr -d com.apple.quarantine` + ad-hoc `codesign`），回收 `deployed − wanted` 差集；`uninstall()` 走框架级整体拆除（含全部 A 类二进制）。helper 路径/plist/launcher 逻辑完全复用。
- 二进制落点：仓库内 `src/barkit/plugins/ipad_tunnel/resources/hev-socks5-tunnel`（仅 arm64，见 §13 风险）；**批次 4 起由本插件独占声明**（route_guard 不再声明），`daemon.install()` 据声明拷到 `SYS_HELPER_DIR` 并 `xattr -d` 解 quarantine。
- hev 二进制本身的**配置语义、三个致命坑（`tunnel.name` 必写、`mapdns` 必带 `address`、`/1` 需 SOCKS 回环逃生）、`/1` 劫持的验证姿势（§5.1，`route get default` 是显示陷阱）与分层诊断流程**，另见同目录 **`hev-tunnel.md`**（随本目录存放，便于整体迁移时自洽）。

### 3.3 数据在哪（**没有数据流文件**）

- **期望态**：`intent.set("tunnel", …)` 的一帧。app 侧 `tunnel_spec.build_tunnel()` 算出来
  直接经会话发出去；helper 侧常驻内存，不落盘。
- **观测**：`event.push("tunnel", …)` 推给订阅方（组件 `observed("tunnel")` 读内存里那份）。
- **不给谁敲门**：组件 `start()` 一开口就声明意图（会话连上即自动补发），「立刻跑一轮」
  要么是连接本身，要么是 `op.run("resync", {"subject": "tunnel"})`。
- 唯一落盘的是守护那份**崩溃恢复快照** `helper_snapshot.json`（只在内容真变时写）。

---

## 4. 数据协议（会话上的一帧）

### 4.1 隧道**意图**正文（app → helper，`intent.set("tunnel", …)` 的 `payload`）

```json
{
  "version": 2,
  "enabled": true,
  "local_port": 1080,
  "device_port": 9001,
  "dns": "198.18.0.2",
  "disable_ipv6": true,
  "hijack_route": false,
  "hijack_dns": false,
  "actions": {
  "on_activate": [{"type": "switch_wifi", "ssid": "iPad-Share"}],
  "on_deactivate": [{"type": "switch_wifi", "ssid": "HomeNet"}]
  }
}
```

> **v2.6.3 变更（批次 -1 安全热修）**：`actions` 通用动作列表已**降级为固定 `wifi` 字段**，`type` 分发取消。现结构：
>
> ```json
> "wifi": {
> "on_activate": "iPad-Share",
> "on_deactivate": "HomeNet"
> }
> ```
>
> 值为 `null`（不切）或非空 SSID 字符串。未知字段整份拒绝。理由：「type 分发」等于在 root 侧预留可配置执行语义的口子（每加一个 type 只需往封闭集加一个字符串）；收为固定 `wifi` 字段后，root 侧没有 type 解析、没有循环分发，唯一能做的就是 networksetup 切 Wi-Fi。旧动作列表由 **app 侧 `build_tunnel()`** 兼容转换，root 侧无兼容代码。本节以下原文保留旧示例仅供对照，以 `wifi` 结构为准。

- ~~`link_up` 由 app 侧 bridge 线程填入~~ **（v2 起不在契约里）**：「iPad 这条链路此刻通不通」是**运行期事实**，而这份文件是**期望态**。把事实写进期望态有两个后果，2026-09-29 那轮真机上都出过：写方按「我上次写过什么」判变化 ⇒ 文件被第三方改写后**永不纠正**；helper 拿着一句过期的「链路通」去建一条**上游没人听**的隧道 ⇒ 流量黑洞。现在由**端口本身**承担这件事：app 侧中继只在链路就绪（或用户显式开了「忽略链路」手动档）时占用 `127.0.0.1:local_port`，helper 那边现探这个端口就知道上游在不在（`_upstream_ready`）。**判据是「端口有没有人在听」，不是「文件里写了什么」** —— 可观测、无双写、不会过期。
- `link_up` 曾经的语义仍然成立，只是换了载体：「准入成立且没有真断」——准入探测（有设备 ＋ 打通 iPad:9001 ＋ SOCKS5 握手有应答）通过才占端口；运行期**只有「USB 设备消失」**才放掉端口（探测超时 / 引擎无应答只记录不拆，见《2026-09-20-iPad隧道链路判据》）。另有数据面自报兜底：中继连续窗口零成功也算真故障。helper 不直连 usbmuxd，与 USB 探测解耦。
- `hijack_route`（默认 `false`）：是否抢全量 `/1` 路由把整台 Mac 流量经 iPad 转发。关闭（默认）即**旁路模式**：隧道照样建立（hev + utun），但完全不加 `/1` 路由，不影响本机原有直连。
- `hijack_dns`（默认 `false`）：是否把系统 DNS 改为 `198.18.0.2`（hev 内置 mapdns）。关闭后不改系统 DNS。
- 两项均由配置对话框的「劫持全量 IPv4 路由」「劫持系统 DNS」勾选控制，**默认都不勾选 = 旁路**（保持安全默认）。理由：隧道不通时全局劫持 = 整机断网（不对称风险），故默认旁路，待用户用 `dig @198.18.0.2` 确认隧道可用后再显式开启（见 `hev-tunnel.md` §5.1 验证章节）。
- `actions`（现为固定 `wifi` 字段，见上）：helper 只执行固定语义的 Wi-Fi 切换，未知字段整份拒绝、不写错误到 state 再放行。

### 4.2 隧道**观测**正文（helper → app，`event.push("tunnel", …)` 的 `payload`）

```json
{
  "ok": true,
  "active": false,
  "utun": null,
  "dns_set": false,
  "ipad_reachable": true,
  "last_action": {"wifi": "iPad-Share", "ok": true},
  "error": null,
  "checked_at": 1760000000
}
```

字段的**消费口径**（2026-09-29 补；两处判据不是一回事，别混）：

- `checked_at` = helper **得出这份结论那一刻**（由写方自陈，随这一帧一起推来）。app 据此判
  「这份话是不是此刻的」：超过 `DAEMON_REPORT_MAX_AGE_S`（180 秒；依据是 helper 侧「内容不变
  也最多静默 120 秒必刷一次」）即报 `daemon_stale`，**不再照念里面的 `active`** —— 否则守护
  停摆时面板会一直挂着绿字。判据取这个字段而**不是任何文件的 mtime**：谁碰一下文件都会把
  mtime 改新，那不代表守护跑过一轮。
- `pending` = hev 已起、utun 尚未建出（中间态，最多 20 秒）。app 读它，配合本机链路判据
 （`_relay_should_listen()`）分两格：链路已就绪 ⇒ `building`（隧道建立中，通常几秒）；
  否则落 `waiting_ipad`（那才是真在等）。此前 `pending` **没被读**，中间态被念成「等待 iPad」。
- 失联判定：`StateSpec.stale_after = SELF_STALE_AFTER_S`（90 秒 = 三个节拍）判的是
  「**本组件**还在不在上报」（调度器 / worker 死了 ⇒ 整格改判「状态失联」）。

---

## 5. 配置 schema（`config.default.yaml` 新增段）

```yaml
  ipad_tunnel:
  enabled: false # 默认关闭：需先安装守护 + 手动开启
  local_port: 1080 # bridge 本机监听端口
  device_port: 9001 # iPad 端 hev 监听端口（USB 经 usbmuxd）
  disable_ipv6_on_activate: true # 激活时禁用 Mac Wi-Fi 的 IPv6（fail-closed 防泄漏）
  hijack_route: false # 默认旁路：抢全量 /1 路由（全局劫持）开关
  hijack_dns: false # 默认旁路：改系统 DNS 为 198.18.0.2（mapdns）开关
  wifi: # 固定语义的 Wi-Fi 切换字段（v2.6.3 起；root 侧无 type 解析）
  on_activate: "iPad-Share" # 激活时切到的 Wi-Fi（须为本机已配过的网络）；空/null = 不切
  on_deactivate: "HomeNet" # 恢复时切回的 Wi-Fi；空/null = 不切
```

- SSID/密码落在用户 `config.yaml`（`save_user` 原子写 + `chmod 600`），不进默认配置明文。
- 与 route_guard 同用 `plugins.<id>` 深合并；`wifi` 是 dict，按键深合并（只改一个键不影响另一个）。旧版动作列表 `on_activate`/`on_deactivate`（list）仍由 `build_tunnel()` 兼容转换，新写入统一为 `wifi` 字段。

---

## 6. 启用模型（手动 + 自动 on USB）

- **手动**：托盘状态行开关 / 设置页「启用隧道」→ 重算隧道意图正文（`enabled: true`）并声明出去；
  想让守护立刻跑一轮就 `op.run("resync", {"subject": "tunnel"})`（连接本身即已补发，命令只是抢拍）。
- **自动 on USB（无需配置开关，启用即生效）**：bridge 线程内 `DeviceResolver` 复用 `list_devices()`/`pick_device()`；**准入探测**通过（iPad 在 ＋ 9001 能打通 **且 SOCKS5 握手有应答**）才让中继**占上 `local_port`**（前提 `enabled=true`）。比纯轮询 USB 更准：确认 iPad 端 App 真在干活，而不只是端口开着。运行期只轻查「设备还在吗」——完整探测会与数据面抢同一条 USB 通道。
- `automation_active`（AutomationCapability 统一 seam）映射「隧道当前是否 active」，badge 显示 自动/手动。

---

## 7. Wi-Fi 切换时序（已确认）

**激活（正向）：**
1. **确保 iPad 链路** —— bridge 线程确认 usbmuxd 链路 + 9001 可达，随后中继**占上 `local_port`**（这是「链路就绪」的唯一外显信号）。
2. **起隧道** —— helper 拉起 hev + 加两条 `/1` + 设 DNS=198.18.0.2；**若 `disable_ipv6_on_activate=true` 则额外 `setv6off <网络服务>`（<网络服务> 即 helper 的 `_network_service()`，非硬编码 `Wi-Fi`）以防 IPv6 经本机 Wi-Fi 泄漏（见 §9/§13-1）**；◷ 2026-09-28：`_network_service()` **读不出时返回 `None`** ⇒ 本节的 `setv6off` 与 §9 的 `-setdnsservers` **一律不执行**（fail-closed，不猜服务名，以免写错服务并被系统存下来）、状态里以 `service_unknown` 如实点名，**隧道数据面不受影响** —— 判据与代价见。
3. **最后切 Wi-Fi** —— 执行 `on_activate` 动作（`networksetup -setairportnetwork en0 <SSID>`，**不带密码**：仅切本机已配网络）。

> 理由：先把新的默认出口（iPad 隧道）建好，再跳网络，使跳变落在已生效的隧道上，避免「旧 Wi-Fi 已断、隧道还没起」的空窗。

**停用（反向）：**
1. 执行 `on_deactivate` 动作（切回 `HomeNet`）。
2. 拆隧道（hev 停 / `/1` 删 / DNS 还原 `empty` / **若曾禁用则 Wi-Fi 的 IPv6 复原 `setv6automatic`**）。
3. **中继放掉端口**（`local_port` 没人监听 ⇒ helper 下一轮复检即拆；bridge 线程在 **USB 设备消失**时放，探测超时不再放）。

> 安全注记：严格反向会在「切回旧 Wi-Fi」之后才拆隧道，存在数秒「旧 Wi-Fi 已连但流量仍走 iPad」窗口。若实现时发现该窗口引发 DNS 经 iPad 解析的副作用，可微调为「先拆隧道、再切 Wi-Fi」——属实现细节，不影响对外语义。

---

## 8. 动作钩子（切换后触发其他操作）

- 状态翻转（active↔inactive）时 `ctx.events.publish("ipad_tunnel.state", {...})` 广播。
- `wifi.on_activate` / `wifi.on_deactivate` 为**固定语义的 Wi-Fi 切换字段**（v2.6.3 起取消通用动作列表与 type 分发）。
  helper 执行逻辑：解析 Wi-Fi 接口（en0，启动时 `networksetup -listallhardwareports` 缓存）→ `networksetup -setairportnetwork <if> <ssid>`（**不带密码**，仅切本机已配网络；未配过则失败并记 `last_action.ok=false`）；字段为 `null` 即跳过。
- **不再可扩展**：这是刻意收口（批次 -1 安全热修）——「可扩展动作类型」等于在 root 侧预留可配置执行语义的口子。未来若确需新钩子，必须走框架方案 v3 §7.4 的 helper 原语评审路径，不在隧道意图正文里开洞。

---

## 9. Helper 特权操作细节（root）

全部复用 helper 现有安全约束：仅参数化 `subprocess` 调系统命令、目标经 `ipaddress` 严格解析、禁止 shell 拼接、`write_state` 原子写（tmp + `os.replace` + `chmod 0644`）。

| 操作 | 命令 | 幂等/复原 |
| --- | --- | --- |
| 拉起 hev | `Popen([HEV_BIN, yml])`（位置参数；hev 不认 `-c`/`-f`，误用会 `Open -c failed` 立即退出），yml 由 helper 据隧道意图正文现场生成（内存里那份，不读盘） | hev 死亡 → helper 重启（KeepAlive 由 helper 自身循环保证） |
| 发现 utun | `ifconfig -l` 前后 diff（hev 启动前后），取新增 `utunN` | — |
| 两条 `/1` | 仅当 `hijack_route=true`：`route -n add -net 0.0.0.0/1 -interface utunN` + `route -n add -net 128.0.0.0/1 -interface utunN` | 仅当本轮实际加过才拆除（`route -n delete`，受 `_TUNNEL_ROUTE_HIJACKED` 状态位控制）；与 route_guard 的具体前缀（如 `10.99.2.0/24`）最长前缀优先共存 |
| 设 DNS | 仅当 `hijack_dns=true`：`networksetup -setdnsservers <网络服务> 198.18.0.2`（<网络服务> 同上为 `_network_service()`，非硬编码 `Wi-Fi`） | 仅当本轮实际改过才复原：原无自定义 DNS 时存 `"empty"`，复原传字面量 `empty`（避免永久卡死）；`hijack_dns=false` 时完全不碰系统 DNS |
| 切 Wi-Fi | `networksetup -setairportnetwork en0 <ssid>`（不带密码，仅切本机已配网络） | 由 `on_deactivate` 动作负责切回 |
| 版本 | `HELPER_VERSION` 当前 `2.6.3`（改 helper 源码必须 bump，否则 app 判定「已是最新」→ 新代码永不落盘） | app 比对过期（`expected_version() != 已装版本`）→ 提示重装（复用 `daemon.status()`） |
| 禁用 v6 防泄漏（受 `disable_ipv6` 控制） | 仅当 `disable_ipv6=true`：激活 `setv6off <网络服务>`、停用 `setv6automatic <网络服务>`（<网络服务> 同上为 `_network_service()`） | 幂等；确保 IPv6 不泄漏到 Mac 本机 Wi-Fi（iPad 当前无 v6，v6 在隧道期不可用属预期）。`false` 时不执行，IPv6 将经本机 Wi-Fi 出网（用户知情选择） |

**与 route_guard 的共存**：route_guard 的静态规则（更长前缀）优先；其余流量走 iPad。两套路由都由同一 helper 维护，无冲突。

**IPv6 策略（fail-closed，默认开启的可配置项）**：因 iPad 网络暂无 IPv6，隧道无法转发 v6。默认 `disable_ipv6_on_activate=true`，激活期间禁用 Mac Wi-Fi 的 IPv6，使 v6 既不能经 iPad、也不会泄漏到本机 Wi-Fi（§13-1）。设为 `false` 则不禁用——此时 IPv6 将经 Mac 本机 Wi-Fi 出网（属用户明确选择，非缺陷）。备选方案：加 `::/1`/`8000::/1` 两套 v6 黑洞路由（`route -n add -inet6 ... -blackhole`，需 root，保留本地链路 v6），实现更复杂，当前不采用。

---

## 10. 二进制分发（随 App 打包）

- 仓库内置 `resources/hev-socks5-tunnel`（arm64，**批次 4 起归 `ipad_tunnel` 独占声明**）。
- 安装时 `daemon.install()` 把它 `install -m 0755` 到系统域 `SYS_HELPER_DIR` + 解除 quarantine；**二进制缺失/哈希不符即安装失败**（v2.6.3 起不再「尽力而为」——降级路径即提权路径，helper 只认系统域一处）。
- helper 内 `hev_binary() = /Library/Application Support/BarKit/hev-socks5-tunnel`（系统域，root:wheel 0755，用户改不动）；**用户域回退已删除**（v2.6.3 安全热修：曾有 data_dir 回退，等于 root 执行普通用户可写目录里的 Mach-O，是可直接利用的本地提权面）。缺失时 `run_tunnel_cycle` 报「hev-socks5-tunnel 二进制未安装（请在 App 内重装守护）」并确保隧道拆除，**不做任何降级**。
- 卸载时按 §9.4 框架级整体拆除一并 `rm -f`（含 manifest 与全部 A 类二进制）。

---

## 11. 进程监管

- **bridge（app 侧）**：纯 Python selectors 事件循环，无 root。**v2.7（批次 5 后段）起线程与时钟归框架**：插件只经 `Plugin.resident_services()` 声明两个长驻服务（`RelayService` 自持循环型 / `LinkProbeService` 周期型 `interval=3.0`），由 `core/resident.ResidentHost` 起停托管、`join(5s)` 收口、异常记入「插件问题」。**插件内不再有私有线程/定时器**（门禁 `test_no_private_timer_in_plugins` 扫描，bridge.py 必须零命中）。
  - 监管口径随之落定：**不重建线程**，而是**服务内部自愈**——中继循环单轮异常 → 记日志 + `report_problem`（设置页可见）+ 关闭监听后 1s 重试，循环不退出；探针单次失败只记票/去重日志。开关翻转由 `ctx.resync_services()` 触发框架差量收敛（不监听 1080 ↔ 监听）。
  - `core/proc.run_cmd` 只适合一锤子命令（带 timeout/killpg），**不适用**常驻资源——bridge 用 resident service 的托管线程，hev 用 helper 子进程，均不依赖 `run_cmd`。
- **hev（helper 侧）**：由 root helper `Popen` 拉起并循环保活（死亡即重启），app 不直接管；app 经 `observed("tunnel")` 拿到的 `utun`/`dns_set` 判健康。

---

## 12. 与 route_guard 的边界 / 复用总表

| 能力 | 归属 | 说明 |
| --- | --- | --- |
| LaunchDaemon 安装/卸载/版本比对 | **复用** `route_guard.daemon` | 同一 helper，一次授权 |
| 与守护的通道 | **复用** 框架会话（`HelperLink`） | 新增一个领域 `tunnel`：意图 `intent.set` + 观测 `event.push`，零新增文件 |
| root 路由/DNS/Wi-Fi 操作 | **框架** `capabilities/helper/` | 隧道机制在 `tunnelops.py`，task `tunnel.sync` 调度；routes 逻辑不动 |
| 二进制随装 + quarantine 解除 | **A 类对账** `daemon.install()` | hev 由本插件声明，框架按 wanted 并集部署 + 回收差集（§9.3） |
| 状态面板 / HeaderChip / 设置页 | **新建** `ipad_tunnel` 插件 | 结构镜像 route_guard |
| USB 探测 | **移植** `bridge.py` | 来自上游项目 `mac_socks_bridge.py` |
| 自动化开关 badge | **复用** `AutomationCapability` 能力 | `automation_active` 映射 |

---

## 13. 风险与开放问题

1. **IPv6 防泄漏（fail-closed，默认开启的可配置项）**：两条 `/1` 只覆盖 IPv4，IPv6 仍走 Mac 本机网卡。因当前 iPad 网络无 IPv6，默认 `disable_ipv6_on_activate=true` → **隧道激活期间禁用 Mac Wi-Fi 的 IPv6**（`networksetup -setv6off <网络服务>`，<网络服务> 即 `_network_service()`，非硬编码 `Wi-Fi`），停用复原（`setv6automatic <网络服务>`）——IPv6 既不能经 iPad（iPad 无 v6），也不会泄漏到 Mac 本机 Wi-Fi，且 v6 在隧道期间不可用属预期（见 §9）。设为 `false` 则不禁用（用户知情选择，IPv6 将经本机 Wi-Fi 出网，非缺陷）。若日后 iPad 获得 IPv6，再切换为「经隧道转发 v6」模式（加 `::/1`/`8000::/1` 两套 v6 `/1` + iPad 端 hev 配 `ipv6`），作为独立 follow-up。
2. **架构限制**：随包 `hev-socks5-tunnel` 仅 arm64。Intel Mac 不支持（除非改下载 per-arch 或打 fat 包）。当前用户为 Apple Silicon，暂不处理。
3. **停用时 Wi-Fi 时序窗口**：见 §7 安全注记，实现时可微调。
4. **helper 版本耦合**：HELPER_VERSION 升到当前版本（2.4）后，旧 route_guard 用户升级 App 会提示重装守护（一次授权），属预期。
5. **iPad 端前提**：iPad 必须运行本次升级后的构建（默认 hev 引擎，监听 9001）。若 iPad 端仍是 microsocks，端口一致仍可桥，但隧道语义不变（microsocks 只做 SOCKS，不做全流量 utun——全流量依赖 Mac 端 tunnel + iPad 端 hev 的 `bind-interface` 把出站走 Wi-Fi）。务必确认 iPad 端为 hev。

---

## 14. 实现里程碑

- **M1 数据层**：`tunnel_spec.py`（生成/校验隧道意图正文）+ 订阅 `tunnel` 领域读观测；`config.default.yaml` 加段；`plugin.py` 骨架（状态行 + tick 读内存里那份观测，先不接真实操作）。
- **M2 Helper 扩展**：`helper.py` 加 `run_tunnel_cycle()`（hev 拉起 + `/1` + DNS + Wi-Fi 动作）；`daemon.install()` 加二进制拷贝；本机用 `python3 helper.py` 手动跑通（先 `sudo` 直跑验证）。
- **M3 App 侧接线**：`bridge.py` 线程监管 + 链路探活（**结论决定中继要不要占用 `local_port`**，v2 起不再写 `link_up`）；plugin 声明隧道意图 + 订阅「隧道」领域刷 UI；设置页 + 手动/自动启用。
- **M4 二进制与收口**：随包 hev 二进制 + `xattr -d`；pytest（tunnel_spec 校验、helper 路由数学 dry-run、动作解析）；端到端真机验收（§15）。

---

## 15. 真机验收清单

- [ ] 安装守护后 `helper` 版本＝`capabilities/helper/version.py::HELPER_VERSION`，无过期提示（`daemon.status()['outdated']` 为 false）。
- [ ] 插 iPad USB + 起 iPad 端 hev → BarKit 自动拉起隧道（观测里 `active=true`、`utun` 非 null、`dns_set=true`）。
- [ ] `curl -4 https://ifconfig.me` 返回 **iPad 的 Wi-Fi 公网 IP**；`scutil --dns | grep nameserver` 显示 `198.18.0.2`。
- [ ] 激活时自动切到配置 SSID；停用时切回恢复 SSID。
- [ ] 原 `route_guard` 静态规则（如 `10.99.2.0/24`）仍精准生效（最长前缀优先）。
- [ ] 拔 USB → 隧道自动拆除、DNS 还原 `empty`、Wi-Fi 切回（无残留脏路由）。
- [ ] `curl -6` 不泄漏 Mac Wi-Fi 公网 IP（默认 `disable_ipv6_on_activate=true` 时验收：IPv6 流量既不能经 iPad、也不可经本机 Wi-Fi，见 §9/§13-1）；若该项关闭则应观察到 IPv6 经本机 Wi-Fi 出网（用户知情选择）。
- [ ] 手动停用后观测里 `active=false`，`route -n get default` 回到原网关（注意：激活时 `route -n get default` 仍显示原网关 en0，这是 macOS 显示特性，不代表劫持未生效——正确验证见 `hev-tunnel.md` §5.1）。

---

## 16. 设置页诊断功能（用户态、只读）

设置页「运行诊断」打开 `TunnelDiagnosticDialog`，可输入**测试 IP**（默认 `8.8.8.8`）与**测试域名**（默认 `example.com`，带合法性校验），点击运行后依次执行 6 步诊断，每步流式展示「执行的命令 / 命令输出 / 评估结果」：

- 评估着色：绿=正常 / 红=异常 / 橙=配置与运行不一致 / 灰=旁路（符合预期）。

| 步 | 诊断项 | 判定依据 | 对照配置 |
| --- | --- | --- | --- |
| 1 | iPad 端是否就绪 | 探 `127.0.0.1:1080` SOCKS 上游 + 读观测的 `ipad_reachable` | — |
| 2 | utun 是否已创建 | 读观测的 `utun`，显示接口名（如 `utun90`）或「未创建」 | — |
| 3 | 路由是否已劫持 | `route -n get <测试IP>` 是否落到 `utunN` | `hijack_route` |
| 4 | 路由测试 | 路由判定 + `curl -4 https://ifconfig.me` 出口 IP，确认真实流量出口 | — |
| 5 | DNS 是否已劫持 | `scutil --dns` resolver #1 是否为 `198.18.0.2` | `hijack_dns` |
| 6 | DNS 测试 | `dig +short <域名>` 是否回 fake IP `100.64.x.x` | — |

> 第 6 步另以 `dig @198.18.0.2 <域名>` 作为隧道探针——但该探针在**旁路态也返回 fake IP**（因 `198.18.0.2` 的 /32 主机路由由 helper 无条件加），**不能单独判定 DNS 劫持**；判劫持与否仍看第 5 步（系统 resolver 入口）与第 6 步的真实查询返回。详见 `hev-tunnel.md` §5.2。

- **只读、用户态**：所有命令（`route` / `scutil` / `dig` / `curl` / `ifconfig`）均不需 root，诊断在 app 进程内直接驱动，**不依赖特权 helper、不会改动系统网络**。
- **逻辑与 UI 分离**：诊断逻辑在独立 `diagnostics.py`（纯函数 + 可注入 `runner`，便于单测）；UI（`config_dialog.py` 的 `TunnelDiagnosticDialog` + `_DiagWorker` 后台线程）只负责展示。
- **配置/运行交叉印证**：第 3/5 步把「命令实时输出」与「配置开关」比对——配置了劫持但守护还没在这一轮收敛（意图推过去之后最长一个周期）会显式报 WARN，而非误判为已生效。
- 典型用例：分别勾选 / 不勾选劫持，运行诊断——劫持态应 6 步全绿；旁路态应全灰 / info；iPad 未连应第 1 步红。

---

## 17. iOS 端签名到期通告（2026-10-09）

iPad 端 App（上游仓库 `ipad-iphone-second-monitor-ios12-free`）用个人证书签名，免费开发者
描述文件 **7 天一签**；过期后 App 无法再次启动（前台常驻是系统未文档化的例外，伴随降速、
不可依赖）。本节定义 Mac 侧如何拿到「还剩几天」。

### 17.1 通道

- iPad 端 App 解析自己包内的 `embedded.mobileprovision`（取 `<?xml … </plist>` 字节段解
  plist，读 `ExpirationDate`），启动时**解析一次**，payload 拼成**常量 JSON**；
- 设备回环 `127.0.0.1:9002`（`ANNOUNCE_PORT`）上挂一个「**连接即倾倒**」应答点
 （GCD accept 源；不读请求、不解析、send 完 close；空闲零成本；异常自吞 + 有界退避重试；
  与 hev 隧道引擎零共享状态）。SOCKS-CONNECT 借道 9001 的方案降为备选（hev 对回环目标的
  放行策略未验证），Mac 侧主路线是 **usbmuxd 直连 9002** —— 与 9001 同暴露面（都只绑
  设备回环），不新增对外通道。
- Mac 侧 `announce.fetch(on_fail=…)`：`pick_device` → `open_device_tunnel(device_id, 9002)`
  → 读至 EOF（超时 `ANNOUNCE_READ_TIMEOUT_S`）→ `parse()` 归一。**任何失败都归 None
 （未通告）**，并以**短原因**（`FAIL_NO_DEVICE` / `FAIL_REFUSED` / `FAIL_READ` /
  `FAIL_CONTRACT`）回调一次 —— 四种的处置完全不同（去插线 / 去看 iPad 端 App / 去改协议），
  混成一句「拿不到」会把用户指错方向（同 `RESULT_BADDEV` 与 `RESULT_CONNREFUSED` 的分法）。

### 17.2 契约（payload，`version` 从第一天就带）

```json
{"version": 1, "state": "ok|absent|unreadable",
 "expires_at": "2026-10-16T07:59:59Z", "announced_at": "2026-10-09T06:00:00Z"}
```

- `expires_at` 只在 `state=ok` 时出现，**绝对时刻**（ISO8601 带时区）；「还剩几天」由 Mac
  显示时现算（`announce.days_left`，按日历整日向下取整）—— 设备间时钟不同步时相对值必错。
- `absent` = 商店/TestFlight 签名（无档案）；`unreadable` = 有档案但解析失败。读侧比校验侧
  宽：未知字段容忍，缺关键字段才拒（`_normalize`）。

### 17.3 节拍与消费

- **不新起时钟**：拉取挂在探针**已有的那两个口**上 —— ① `on_link_change(True)`（链路翻上来
  = iPad 端 App 活着刚被证实）立刻拉；② `on_poll`（每轮探针 tick）在「链路还在、且手里还
  没有通告」时**低频重试**（`ANNOUNCE_RETRY_INTERVAL_S`，与「运行期每 `PROBE_DEEP_INTERVAL`
  才许碰一次 USB 通道」同一条节律），拿到一份合法应答即停。**为什么必须有 ②**（2026-10-10
  现场）：设备侧应答点可能恰好 ① 那一刻不可用（iPad 端 9002 的 accept 源死掉、无自愈路径），
  只有 ① 则整个会话停在「未通告」，设备侧后来好了也没人去问 —— **「这一次没拿到」不等于
  「永远拿不到」**。重签 + 重装必经 App 重启 ⇒ 数据面断 ⇒ 链路翻转 ⇒ ① 自然重拉，故拿到
  之后不必再定期问。拉取跑在探针线程（非数据面），阻塞上限一次 Connect + 读超时。
  **不 mark_dirty**（那个入口碰调度器主线程 QTimer，跨线程不安全）—— 周期 tick（30s）
  自然拾取，对按天倒计时足够。
- **没拿到不清旧账**：到期时间对同一份构建不变，`None` 不覆盖旧缓存。
- 消费方两处，读**同一份**归一结果（判定只在 announce.py 一处）：
  设置页恒显示（`announce_text()`）；面板状态行**只在剩余 ≤ `EXPIRY_WARN_DAYS`（3 天）时
  挂尾**（`panel_suffix`，`_snapshot` 统一拼接，不新增状态格）。
- ⚠ **设置页那行是活行，不许在构建期定格**（2026-10-09 用户上报「没取到时间」）：
  设置窗在 `AppShell.assemble()` 里**启动期构建一次**（`shell/app_shell/app_shell.py`
  → `shell/settings/pages.py::_build_ui` 当场把每页 `builder()` 跑完），而本插件的通告要
  等探针**首翻**（启动后 ≈6~12s）才拉得到 ⇒ 构建那一刻 `_announce` 必然是 `None`。
  写法：`settings.py` 把 QLabel 交给 `plugin.attach_announce_label()`，由 `tick()`（30s，
  主线程）调 `_refresh_announce_label()` 续写；页面销毁时经 `destroyed` 摘引用（与
  `capabilities/inline_feedback.attach_feedback` 同款纪律，不引入新时钟）。
  **凡「运行期事实」都不许走构建期读值**，配置类字段（页面自己会改并回写）不受此限。
- **失败可诊断，但按故障期出声**（2026-10-11 改）：拉到即记一条 INFO（`_refresh_announce`）；
  拉不到则由 `fetch` 回调短原因，`_note_announce_fail` **同一个原因只记一条**（原因变了再记）
  —— 本组件要连跑数天、每 60s 重试一次，按次记就是上千条/天（长跑进程的日志只记状态翻转）。
  `fetch` 内部各条失败路径只落 debug（细节按次，调试用），包括「拿到了却读不懂」的原文片段。
  日志文件停在 INFO 档 ⇒ 不记「拿到」与「进故障」这两条，「没取到时间」在日志里什么都查不到
 （2026-10-09 现场：整份 barkit.log 零条 announce，成功与失败同样不可见）。
- 「已到期 + announce 通道还在」= 进程仍活着（系统例外现场），文案如实区分
  「已到期（进程仍在运行）」与「今天到期」—— 性能降速**不做因果断言**，只与
  `data_plane_fault` 并列展示。

### 17.4 真机验证记录

- ✅ **usbmuxd 直连 9002 已打通**（2026-10-09 实测，连拉 10/10 成功）：句柄直接落在
  `mux.open_device_tunnel(device_id, 9002)`，SOCKS-CONNECT 借道 9001 这条备选不必启用。
- ✅ **`embedded.mobileprovision` 读得到 `ExpirationDate`**：同一份应答既证明解析成功
 （`state=ok` + 绝对时刻），也证明 App 侧 9002 应答点在跑。
- ⏳ 仍待人工确认的两件（都不是机制问题）：重签安装后**设置页那行**随 30s tick 刷成
  「还剩 N 天」；面板状态行在剩余 ≤3 天时挂尾。
