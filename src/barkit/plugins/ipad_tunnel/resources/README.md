# hev-socks5-tunnel 二进制放置处（A 类：物理随插件、授权归框架）

本目录是 hev 的**唯一分发位**（批次 4 · S4 起从 `route_guard/resources/` 迁来）。
按方案 §9.3「A 类由『框架提供』改为『**物理随插件、授权归框架**』」：

- **声明**：`ipad_tunnel/plugin.py::resources().root_binaries = ("hev-socks5-tunnel",)`
  —— 只有它一家声明（route_guard 已撤销冗余声明）。框架按「所有已安装插件
  （含 disabled / failed）的声明并集」算出 `wanted` 集。
- **落点**：`daemon.install()` 装 helper 时，按声明把本目录的二进制
  `install -m 0755` 到系统域 `/Library/Application Support/BarKit/`（`root:wheel`），
  解除 quarantine 后做 **ad-hoc 签名**。
- **基线**：发行态 sha256 登记在 `capabilities/helper/rootbin.py::ROOT_BINARIES`
  （记的是**签名前**的仓库内文件哈希）。**哈希不符 → 拒绝安装**，绝不静默降级
  （降级路径即提权路径）。ad-hoc 签名会改写文件字节且不可复现，故运行期守卫不用
  哈希，改用 `codesign --verify` + owner/权限（§9.3 已写死该定位）。
- **回收**：卸载时按**无状态对账**重算差集（`deployed − wanted`），仅差集非空
  才需 root 授权；无插件再声明时该二进制被回收。

## 获取二进制

从 hev-socks5 项目的 Release 下载 **macOS arm64** 版的 `hev-socks5-tunnel`
（注意：不是 `hev-socks5-server`，是 **tunnel**——它创建 utun 虚拟网卡）。

```bash
cp /path/to/hev-socks5-tunnel src/barkit/plugins/ipad_tunnel/resources/hev-socks5-tunnel
chmod 0755 src/barkit/plugins/ipad_tunnel/resources/hev-socks5-tunnel
```

（开发/调试时也可自行放进系统域后重装守护——但那只影响本机，仓库内基线不变。）

## 缺失时行为

**缺失或哈希不符即安装失败，且给出明确原因**（v2.6.3 起不再「尽力而为」）。
这正是批次 -1 安全热修堵掉的那条 P0-1 降级路径：曾经的「缺失不阻断安装」催生了
「helper 去用户域找二进制」的回退，而 root 执行普通用户可写目录里的 Mach-O 就是
一条可直接利用的本地提权面。

## macOS 授权要求（重要 · 已纠正）

**结论先行**：`daemon` 以 **root** 运行 `hev-socks5-tunnel`，而 root 进程在 macOS 上
创建 utun **不需要** `com.apple.vm.networking` 授权。因此安装时**只做 adhoc 签名
（不带任何 entitlements）**即可，切勿给 hev 注入该授权。

**致命坑（2026-09-02 实测）**：`com.apple.vm.networking` 是**受限授权**，必须经
provisioning profile 背书；单独用 `codesign --entitlements` 注入（无论 adhoc 还是
Apple Development 签名）**都会被 AMFI 在内核层 SIGKILL**——表现：

- 守护报来的隧道观测里写着 `hev 已退出（rc=-9）`（面板那条状态行、或 `helper_snapshot.json` 的 `subjects.tunnel.observed`）
- `hev.log`（`/Library/Application Support/BarKit/hev.log`，即 `SYS_RUN_DIR/hev.log`）为空（进程来不及写日志；P1-1 后已不再写 `/tmp`）
- `ps` 里看不到 hev 进程

对照实验：同一份二进制，注入该授权 → 启动即被 SIGKILL；去掉授权（仅 Dev/adhoc 签名、
无 entitlements）→ 进程正常存活。**所以任何「给 hev codesign 注入 vm.networking」的
操作都是错的，会直接让隧道起不来。** `daemon.install()` 已改为只做 adhoc 签名。

### 隧道仍起不来的排查顺序
1. 确认 iPad 端 SOCKS 引擎已监听 `127.0.0.1:9001`（Xcode 日志 `[SocksProxy] 已监听
   127.0.0.1:9001`），且 Mac↔iPad 桥接已连通（`curl -x socks5h://127.0.0.1:1080
   https://ifconfig.me` 应返回 iPad 的 Wi-Fi 公网 IP）。hev 通常要等上游 SOCKS 连通后
   才拉起 tun。
2. 看 `hev.log`（`/Library/Application Support/BarKit/hev.log`，即 `SYS_RUN_DIR/hev.log`）有无 hev 自身报错。
3. 确认安装的 hev **没有** `com.apple.vm.networking` 授权：
   ```bash
   codesign -d --entitlements :- "/Library/Application Support/BarKit/hev-socks5-tunnel"
   # 正确结果：不应出现 com.apple.vm.networking
   ```
4. 确认运行期完整性自检通过（批次 4 · S4 起，hev 起隧道前校验 owner/权限 +
   `codesign --verify`；不通过则报「需重装守护」而不是硬起）：
   ```bash
   ls -l "/Library/Application Support/BarKit/hev-socks5-tunnel"   # root wheel
   codesign --verify "/Library/Application Support/BarKit/hev-socks5-tunnel" && echo ok
   ```
5. **极少数情况**确实需要该授权（例如非 root 运行 hev）：那必须走 provisioning profile
   路线——把 hev 包进 `.app`，内嵌含该授权的 profile，再用 Developer ID 签名。这是一条
   很重的路，一般不必要，优先确认上面 1–4。

> 仅 arm64：当前目标机器为 Apple Silicon。Intel Mac 需另行准备 x86_64 二进制。
