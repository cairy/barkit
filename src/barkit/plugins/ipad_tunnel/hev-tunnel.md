# hev-socks5-tunnel 配置与排障参考

> 配套文档：`design.md`（插件架构）。
> 本文聚焦**第三方依赖 hev-socks5-tunnel 本身的配置语义与排障**——这部分 `design.md`
> 不覆盖，却是隧道能否跑通的决定层。本文档随插件同目录存放，便于整体迁移 / 删除时自洽。
>
> **权威来源**：我们随包分发、并由 `daemon.install()` 装到
> `/Library/Application Support/BarKit/hev-socks5-tunnel` 的二进制，对应上游
> **hev-socks5-tunnel `9a06bc6`**（2026-09 基准）。所有「键是否生效 / 行为」结论均对该
> 二进制用 `strings` + 源码交叉核实；本文与之冲突时以 `helper.py` 的 `_HEV_YAML_TEMPLATE`
> 注释为最终权威（注释里有实测证据）。
>
> 上游 SOCKS5 服务端在 **iPad `127.0.0.1:9001`**，经 USB / usbmuxd 由 `bridge.py` 桥到
> Mac 本机 `127.0.0.1:1080`。**不经 `iproxy`**——桥接由 app 侧线程完成。

---

## 1. hev 在我们系统里的位置

| 项 | 值 |
| --- | --- |
| 二进制（仓库内） | `resources/hev-socks5-tunnel`（仅 arm64） |
| 安装位 | `/Library/Application Support/BarKit/hev-socks5-tunnel` |
| 配置（yml） | 由 `helper.py` 的 `_HEV_YAML_TEMPLATE` **现场生成**，不落盘模板 |
| 上游 | `127.0.0.1:1080`（bridge）→ usbmuxd → iPad `127.0.0.1:9001`（iPad 端 hev） |
| 接口 | `utun90`（高位空闲号；实际名由内核返回，见 §3） |
| TUN 侧 DNS | `198.18.0.2`（hev 内置 mapdns，fake IP 段 `100.64.0.0/10`） |

helper 生成 yml 后 `Popen([HEV_BIN, yml])` 拉起，死亡即重启（KeepAlive 由 helper 循环保证）。

---

## 2. 我们生成的 hev 配置（及三个致命坑）

> 完整模板见 `helper.py::_HEV_YAML_TEMPLATE`。以下解释**每个键为什么这么写**，
> 以及改错会发生什么。

```yaml
tunnel:
  name: 'utun90'          # ★ 必须写（坑 A）
  ipv4: 198.18.0.1        # 扁平字符串，TUN 侧地址（用于 _find_utun 发现接口）
  ipv6: 'fc00::1'
mapdns:
  address: '198.18.0.2'   # ★ 必须写（坑 B）—— TUN 侧 DNS 监听地址
  port: 53                # ★ 必须写（坑 B）
  network: '100.64.0.0'   # ★ fake IP 段（坑：不能用 240.0.0.0/4，见下）
  netmask: '255.192.0.0'  #   = /10（CGNAT，hev v2.14.3 起默认）
  cache-size: 1024
misc:                     # ★ 必须顶层（坑 C）
  log-level: info         #   warn 会掩盖启动失败（见 §6）
  log-file: stderr
  # ... connect-timeout / tcp-buffer-size / max-session-count / limit-nofile /
  #     tcp-read-write-timeout / udp-read-write-timeout 等
  #     （task-stack-size 本模板不写：它恒被 hev 推导的下限抬高，见 tunhev.py 注释）
socks5:
  address: 127.0.0.1
  port: 1080              # = tunnel.json.local_port（bridge 本机监听）
  pipeline: true
  udp: 'tcp'              # FWD UDP（UDP-in-TCP）显式声明。**与不写行为逐字节相同**：
                          #   hev 只在取值恰为 'udp' 时才启用标准 UDP ASSOCIATE，键缺失
                          #   落到 memset 的 0 = 本模式。加它是把模式钉在明面上，防上游
                          #   默认值漂移（上游 README 把 udp:'udp' 排成未注释样例）
```

### 坑 A：`tunnel.name` 必须写，否则 hev 永久挂起

2026-09-03 实证（非 root 跑同一份 yml，`log-level: debug` 看它停在哪一步）：

| 配置 | 现象 |
| --- | --- |
| **无 `name`** | 只打印 `socks5 tunnel init`，随后**永久挂起**（无报错、无退出）→ 进程活着、日志空、永远建不出 `utun` → helper 陷入 20s 超时→重启循环 |
| `name: utun90` / `utun9` | `socks5 tunnel init` → `socks5 tunnel open (Operation not permitted)`（非 root 权限错属预期，说明已走到 open） |

成因：`hev-tunnel-macos.c` 用 `sscanf(name, "utun%u", &sc_unit)` 解析，**缺 `name` 时 `sc_unit` 未初始化**，
卡在打开 utun 控制 socket 之前。旧注释「不写 name 让系统自动分配」是错的，已按实测纠正。

### 坑 B：`mapdns` 必须带 `address` / `port`，否则 DNS 静默失效

2026-09-03 源码核实（`9a06bc6`）：`mapdns` 段解析 5 个键
`address / port / network / netmask / cache-size`；**缺 `address` 时 `mapdns_address` 默认 `0`（0.0.0.0）**。

`hev-socks5-tunnel.c::udp_recv_handler` 用**严格等值**
`faddr == 包地址 && fport == 端口` 决定是否拦截 DNS，**无 `faddr==0` 通配兜底**。
→ 缺 `address` 时匹配永远为假 → **mapdns 被静默禁用** → 系统 DNS 设 `198.18.0.2` 后永远收不到应答
（即「DNS 无法连通」）。`address: 198.18.0.2` + `port: 53` 与官方 README / issue #280 一致。

> 这就是 2026-09 初版模板误删 `address`/`port` 后「DNS 不通」的病根。已用
> `tests/test_ipad_tunnel.py::test_hev_yaml_mapdns_has_address_and_port` 锁死该键。

### 坑 C：`misc` / `mapdns` 必须是**顶层**段

缩进到 `tunnel` 下会被 hev 忽略（它只认顶层 `tunnel / socks5 / mapdns / misc`）。
后果：`log-file`、`log-level`、`limit-nofile` 全部失效 → 表现为 **hev 秒退但日志文件空空如也**。

### 其他

- **不要写 `mtu` / `ipv4-mask`**：本构建配置键表里没有这两个键（`strings` 核对），写了也是被忽略的死配置，只误导排查。
- **`log-level: info`（不是 `warn`）**：`warn` 下正常运行完全静默，任何启动期失败都无从取证（hev.log 常年 0 字节）。`info` 只在启动期打印十余行，代价可忽略。
- **`ipv4` 是扁平字符串** `198.18.0.1`，不是旧版 `address/gateway/prefix` 嵌套结构（见 §8）。

---

## 3. `tunnel.name` 的 unit+1 怪癖

`hev-tunnel-macos.c` 核心逻辑：

```c
res = sscanf(name, "utun%u", &sc.sc_unit);
if (res > 0) sc.sc_unit += 1;   // 解析出 unit 后 +1
```

- 写 `utun90` → 请求 unit **91**；写 `utun9` → 请求 unit **10**。`ifconfig` 显示的 `utunN`
  才是内核实际分配名，**配置名 ≠ 接口名**。
- 我们**不依赖名字匹配**：`_find_utun()` 按「哪个 `utun` 上有 `198.18.0.1`」发现接口，所以取高位空闲号 `utun90`（避开 `utun0..5` 常被系统 VPN 占用）即可。
- 指定一个被占用的 unit → `connect()` 失败、hev 启动失败并报告，而非静默复用。

---

## 4. mapdns / fake IP 机制（为什么必须这俩键 + 为什么系统 DNS 指 `198.18.0.2`）

mapdns 是进程内 LwIP 虚拟栈的 DNS，不是 `bind()` 在 `127.0.0.0.0/8` 的普通本机服务：

- `mapdns.address: 198.18.0.2` 是 **TUN 侧 DNS 地址**（不是 `127.0.0.1`）。系统 DNS 客户端必须能把 DNS 包路由到该地址，故系统 DNS 服务器须填 `198.18.0.2`。
- 它返回 **fake IP**（`100.64.0.0/10` 段）；后续连接该 fake IP 时，hev 把 IP 还原成原域名走 SOCKS5。
  **这正是绕开「iPad 上游只接受域名 CONNECT、拒收裸 IP（rep=4）」的关键**——hev 向上游发的是域名，不是解析出的 IP。
- 把系统 DNS 设成 `127.0.0.1` 只会访问 lo0，根本不进 utun，mapdns 收不到（不要这么做）。
- **fake IP 段必须用 `100.64.0.0/10`，不要用 `240.0.0.0/4`**：hev v2.14.3 已把默认改为 CGNAT（issue #281，Windows 对 240/4 兼容性差）。若用 `240.0.0.0/4` 且无对应 TUN 路由，即使 DNS 通、连接也失败。

> 验证（真机）：`dig @198.18.0.2 baidu.com` → `baidu.com. 1 IN A 100.64.0.0`，`NOERROR`，`0ms`
> 即证明 mapdns 已活。

---

## 5. 全局 `/1` 路由必须保证 SOCKS 回环逃生

端到端走真实流量时（`hijack_route=true`），helper 加两条：

```
route -n add -net 0.0.0.0/1   -interface utunN
route -n add -net 128.0.0.0/1 -interface utunN
```

**陷阱**：这两条会把「发往 `127.0.0.1:9001` 的 SOCKS5 流量」也送进隧道，而 macOS 没有 Linux 式
fwmark 便捷逃生 → 死循环 / 黑洞。必须保证 SOCKS5 上游走最具体的 lo0 路由。

**我们的设计天然满足**：上游是 `127.0.0.1:1080`（bridge 监听于 lo0），而 `127.0.0.0/8` 比
`0.0.0.0/1` 更具体 → lo0 优先，SOCKS 流量不进隧道。即 `/1` 不含 `127.0.0.1`，无环。

⚠️ 若将来把 iPad SOCKS5 上游改为**非回环 IP**（如局域网 IP），该 IP 会落入 `/1` 形成环路——
此时必须显式 `route add -host <ip> -interface <原网关>` 把它 bypass 出隧道，不能让它进 `/1`。

### 5.1 验证 `/1` 是否真的生效（macOS 显示特性，别被 `route get default` 骗了）

开 `hijack_route` 后，有人用 `route -n get default` 验证「没生效」——这是**经典误判**：

- `route -n get default` 永远返回字面量 `0.0.0.0/0`（原网关 en0）那条「默认」条目，**不反映最长前缀匹配**。
- 真实发包走 `0.0.0.0/1` + `128.0.0.0/1`（比 `/0` 更具体），全进 `utunN`。这是 `/1` 全流量隧道的通用行为（OpenVPN / sshuttle 皆然）。

**正确验证（任选）**：

| 命令 | 期望值 | 说明 |
| --- | --- | --- |
| `route -n get 8.8.8.8` | `interface: utunN` | 真实流量走 iPad（最长前缀命中 /1） |
| `netstat -rn -f inet \| grep -E '0\.0\.0\.0/1\|128\.0\.0\.0/1'` | 两条都指向 `utunN` | /1 路由确实被加上 |
| `curl -4 https://ifconfig.me` | 返回 **iPad 的 Wi-Fi 公网 IP**（不是 Mac 的） | 端到端真流量经 iPad 出口 |
| `scutil --dns \| grep nameserver` | 出现 `198.18.0.2` | `hijack_dns` 生效（系统 DNS 被改） |

> 实测（2026-09-03）：用户报告「开了开关没生效」，`route -n get default` 仍显示
> `en0`；实查路由表 `0/1`、`128/1` 均已在 `utun90`，`dig @198.18.0.2` 也返回
> `100.64.0.0`，说明劫持其实已生效——是验证命令看错了。保存配置后若立刻验证看不到
> 变化，也可能只是 helper 尚未重跑（最长 30s 轮询）；app 侧 `_on_tunnel_saved` 已
> `touch trigger` 触发秒级重应用，正常 1~2s 内即生效。

### 5.2 验证 DNS 劫持 / 未劫持走的是哪条通道

`hijack_dns` 只决定「系统 resolver 指不指 `198.18.0.2`」，**不决定** `198.18.0.2` 这条 `/32`
主机路由是否存在——后者由 helper **无条件**加（`helper.py:1156`，旁路模式下它是唯一能
验证隧道的入口）。所以光看 `route -n get 198.18.0.2` 永远指向 `utunN`，据此**无法**判断
DNS 有没有被劫持。判据是**系统 resolver 列表 + 真实查询是否拿到 fake IP**：

| 场景 | `scutil --dns` resolver #1 | `dig +short example.com`（走系统 resolver） | `route -n get 198.18.0.2` | 结论 |
| --- | --- | --- | --- | --- |
| 劫持（`hijack_dns=true`） | `198.18.0.2` | `100.64.x.x`（fake IP） | `utunN` | DNS 经 iPad 隧道解析 ✅ |
| 未劫持（`hijack_dns=false`） | 真实 DNS（如 `10.55.55.254`） | 真实公网 IP | `utunN`（主机路由仍在，仅作探针） | DNS 走本机直连 ✅ |
| 半断网（`route=on, dns=off`） | 真实 DNS | 真实 IP，但连接被 iPad 拒裸 IP | `utunN` | 危险错配 ⚠️ |

**逐条判据命令**：

```bash
# ① 系统 resolver 是谁（DNS 通道的入口判据）
scutil --dns | grep -A3 'resolver #1'

# ② 真实查询是否进了隧道：劫持时返回 fake IP，未劫持时返回真实 IP
dig +short example.com          # 不写 @ → 走系统 resolver

# ③ DNS 包从哪个口出（劫持→utunN / 未劫持→en0）
sudo tcpdump -n -i utun90 udp port 53 -c 3   # 劫持时应看到查询
sudo tcpdump -n -i en0   udp port 53 -c 3    # 未劫持时应看到查询

# ④ helper 自己报告的状态（最权威，对应守护报来的那份观测里的 dns_set）
python3 -c "import json,os;p=os.path.expanduser('~/Library/Application Support/BarKit/helper_snapshot.json');print('dns_set =', json.load(open(p))['subjects']['tunnel']['observed']['dns_set'])"
```

> 注意：`dig @198.18.0.2 example.com` 在两种场景下都返回 `100.64.0.0`——因为 `/32` 主机路由
> 无条件存在，它只证明「隧道 / utun 活着」，**不证明系统 DNS 走了隧道**。判劫持与否必须看
> ①②，别用 `dig @198.18.0.2` 单独下结论。

> 配对铁律（见 `design.md §4.1`）：`hijack_route` 与 `hijack_dns` 必须同开或同关。只开路由
> 不开 DNS = 半断网（拿真实 IP 走 SOCKS `CONNECT` 裸 IP，iPad 拒收 `rep=4`）；只开 DNS 不开
> 路由 = DNS 走隧道但连接直连，行为诡异。

---

## 6. 分层诊断流程（贴合本系统）

按概率从低层往高层验证，一层通了再上一层。**不要一开始就加 `/1` 或改 DNS。**

| 层 | 验证 | 成功条件 | 失败 → 查 |
| --- | --- | --- | --- |
| 0. 桥接 / 上游 | `curl -x socks5h://127.0.0.1:1080 https://example.com` | 返回 200 | iPad 端 hev 未起 / USB 断开 / bridge 未监听 1080 |
| 1. hev 进程 | `pgrep -fl hev-socks5-tunnel` | 进程在 | helper 没拉起（看面板那条隧道状态，或上面 §5 ④ 那份观测里的 `error`） |
| 2. TUN 起没起 | `ifconfig utun90 \| grep 198.18.0.1` | 有 `inet 198.18.0.1` | `name` 缺失（坑 A）/ 权限 / 配置版本 |
| 3. DNS（旁路） | `dig @198.18.0.2 example.com` | 返回 `100.64.x.x` | mapdns 缺 `address`（坑 B）/ fake IP 段 |
| 4. 单 IP 路由 | `route add -host <testip> -interface utun90` → `curl <testip>` | curl 成功 | 路由 / ICMP / 服务端 |
| 5. 全局路由 | 开 `hijack_route` 后 `curl -4 https://ifconfig.me` 返回 iPad 公网 IP | 成功且 `127.0.0.1` 仍走 lo0 | SOCKS 逃生（§5） |

- **`log-level: warn / 日志空 ≠ 健康`**：正常运行本就静默；诊断时改 `info`（见 §2 坑 C 注释）。
  判断生死靠 PID + TUN 接口 + 路由 + 退出码，不靠「有没有输出」。
- **ping 不是可靠验收项**：`tunnel.icmp` 可能关闭，icmp 可用性取决于服务端；用经 TUN 的 TCP/UDP 验证。

---

## 7. 一键诊断脚本（改编自上游排障指南）

```bash
#!/usr/bin/env bash
set -u
BIN="/Library/Application Support/BarKit/hev-socks5-tunnel"
PID=$(pgrep -x hev-socks5-tunnel)

echo "== hev 进程 =="; pgrep -fl hev-socks5-tunnel || echo "(未运行)"
echo "== utun90 地址 =="; ifconfig utun90 2>/dev/null | grep 'inet ' || echo "(无 utun90)"
echo "== 上游直连 =="; curl -x socks5h://127.0.0.1:1080 -o /dev/null -s -w 'socks5h http=%{http_code}\n' --max-time 15 https://example.com
echo "== TUN DNS =="; dig +short +time=8 +tries=2 @198.18.0.2 example.com || echo "(mapdns 无应答)"
echo "== 系统 DNS =="; scutil --dns | grep 'nameserver\[' | sort -u | head
echo "== 路由 =="; netstat -nr -f inet | grep -E 'default|0.0.0.0/1|128.0.0.0/1|127.0.0.1|198.18|100.64'
echo "== 隧道观测 =="; cat "$HOME/Library/Application Support/BarKit/helper_snapshot.json" 2>/dev/null
```

---

## 8. 配置版本 / 格式注意事项

- **`ipv4` 是扁平字符串**（当前 master / 我们打包的 `9a06bc6`）：`ipv4: 198.18.0.1`。
  旧版曾要求 `address/gateway/prefix` 嵌套——在 macOS 上**不能照抄**，会解析失败或返回 `-1`。
- **固定二进制版本**：下载旧版本却用当前扁平格式 / 拉 master 却用旧嵌套格式，都可能静默或半静默失败。
  我们以 `resources/` 内固定二进制为准，不混用。
- **`mapdns` 段键**：`9a06bc6` 认 `address/port/network/netmask/cache-size`（见 §2 坑 B）。

---

## 9. 风险边界与未证实项

1. **`100.64.0.0/10` 全应用兼容性**：官方改默认值是强信号，但上游未给 macOS 全应用兼容矩阵；采用官方当前默认，仍需实测。
2. **`utun90` 是否必然可用**：社区有「高位号更安全」说法，源码未证明；我们靠 `_find_utun()` 实际发现接口，不硬依赖名字。
3. **仅 arm64**：随包二进制不支持 Intel Mac（除非改 per-arch 下载或 fat 包）。当前用户为 Apple Silicon。
4. **Apple 私有中继 / iCloud / Handoff**：全局 `/1` 可能改变其行为；全局代理前先观察 `scutil --dns`、`netstat` 与相关服务状态。
5. **pf 高级分流**：上游建议用 pf 隔离 proxy/tunnel 用户，但错误 pf 规则比静态路由更难恢复，本插件不采用。

---

## 引用来源（节选，均为 `9a06bc6` 基准）

- [hev-config.c] mapdns 解析 `address/port/network/netmask/cache-size`；缺 `address` 时默认 `0`。
- [hev-socks5-tunnel.c] `udp_recv_handler` 严格等值匹配 `faddr==包地址 && fport==端口` 拦截 DNS。
- [hev-tunnel-macos.c] `sscanf(name,"utun%u",&sc_unit)` 后 `sc_unit += 1`；按 `UTUN_OPT_IFNAME` 取实际接口名。
- [README] `tunnel.ipv4` 扁平字符串；`log-file: null|stdout|stderr|文件路径`；`log-level: debug|info|warn|error`。
- [issue #280] 启用 mapped DNS 并将系统 DNS 设为 `198.18.0.2`。
- [v2.14.3 / issue #281] MapDNS 默认改为 CGNAT `100.64.0.0/10`（替代 `240.0.0.0/4`）。
- [discussions #104] 全局路由须 bypass SOCKS5 上游，否则死循环。
