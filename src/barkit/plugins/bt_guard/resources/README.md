# BTHelper.app —— 蓝牙看护组件的助手（物理随插件、免特权）

本目录是 `bt_guard` 组件的外部可执行文件。它不是「可选依赖」，而是**唯一**能切成
蓝牙控制器电源的路径 —— 下面的实测表说明了原因。

## 为什么必须是 `.app`，而且只能由 `open` 拉起

macOS 的蓝牙私有 API 要求调用者具备「带 `NSBluetoothAlwaysUsageDescription` 的进程
身份」，判据是**代码签名里嵌的那份 Info.plist**。2026-09-23 在本机逐条实测（`exit 134`
= SIGABRT）：

| 调用姿势 | 结果 |
|---|---|
| 裸 Python（barkit 自己的进程） | **崩** |
| 裸 Python，先改内存里的 `CFBundleIdentifier` | **崩** |
| 内嵌 `__TEXT,__info_plist` 段的裸 Mach-O | **崩** |
| 直接 exec `.app/Contents/MacOS/` 里的二进制 | **崩**（躺进 bundle 里不算数） |
| 同一个二进制跑不碰蓝牙的分支 | 正常退出（证明不是二进制本身的问题） |
| 经 `open` 启动这个 `.app` | **正常** |

两条推论：

1. **barkit 主进程不能自己干这件事** —— 它跑的是 Homebrew 的 python，签名里没有蓝牙
   用途说明，怎么补都不行。所以能力必须落在这个随插件分发的 `.app` 上。
2. **不能为了「更轻」把它退化成单文件**。它现在就是 3 个文件、80KB，已经是最小形态。

## 怎么和 app 说话：一条本机会话

`open --args` 在本机实测五种姿势**全部丢参**（进程只拿得到 `argv[0]`），所以助手不靠
参数收命令。它起来之后**连上 app 的会话节点**，命令与回执此后都在那条连接上走：

```
app 先 listen  <APP_SUPPORT>/bt_helper.sock  →  open -n BTHelper.app
   ◄──── 助手 connect 并握手（hello / welcome）
   ────► op.run  {"op":"power-off","req_id":"123-1"}
   ◄──── reply   {"req_id":"123-1","ok":true,"result":{"code":0,"lines":[…]}}
```

**方向与 root helper 那条会话相反**：root helper 常驻、由它 listen；助手是按需拉起的，
拉起之前根本没有进程在 listen，所以只能 app 先 listen。协议本身不受影响 —— 线格式与
消息名**一个字都没改**：真源是 `src/barkit/capabilities/helper/protocol.py`，本目录的
`helper/bt-helper.m` 是它的 Objective-C 镜像，app 侧那一半在
`src/barkit/plugins/bt_guard/bt_session.py`。

`APP_SUPPORT` = `~/Library/Application Support/BarKit`，节点名在**三处**必须一致：
`barkit/paths.py` 的 `BT_HELPER_SOCKET`、`bt-helper.m` 的 `kSocketRel`、以及
`bt_session.py` 读到的那个常量（助手拿不到 Python 常量，只能两边各写一份）。
节点是**用完即删**的：稳态下那个目录里不该有它。

旧形态那对 `bt_request.json` / `bt_result.json`（以及结果里那个 `cmd` 归属标记）已于
2026-10-04 消失 —— 配对改由协议的 `req_id` 保证，手工标记不再需要。

## 为什么只有三条命令

`power` / `power-off` / `power-on`，仅此三条。曾经写过单设备重连（`connect` /
`disconnect`），实测对本机的故障形态**无效**：那种形态下它们全返回 `kIOReturnError`，
HID 层 20 秒零变化。真正有效的只有控制器断电重开这一档（实测 8.1 秒恢复、配对无损、
免 root），所以助手只暴露它用得上的能力 —— 能少一条就少一条。

## 入库的是编译好的产物，不是现编

`plugins/ipad_tunnel/resources/hev-socks5-tunnel` 是同一类东西（随插件分发的可执行
文件）的先例。现编要求每一台目标机器都装 Xcode 命令行工具，而这是个 80KB 的静态小
工具，把产物入库代价更小。

要**改**助手时才需要重新构建：

```bash
zsh build.sh        # clang + adhoc 签名，产物覆盖本目录的 BTHelper.app
```

改完按脚本末尾提示核一遍（写请求文件 → open → 读结果，只读零副作用）。

## 两个已经踩过的坑

- **wheel 打包会静默丢掉可执行位**（Python 打包的通用问题，实测：zip 条目里记着
  `0o100755`，解出来落盘是 `0o644`）。本机是开发态安装（直接指向源码目录）所以看不
  出来，真装到别的机器上就是「助手起不来」。组件侧的对策是**首次使用前自己
  `chmod +x`** —— 文件在这个目录里归用户所有，免 root，而且**不影响签名**（签名覆盖
  的是内容，不是权限位）。
- **第一次冷启动要几秒**：`open` 之后助手不会立刻连上来（LaunchServices 注册 ＋
  dyld 首次加载）。实测预热后单程约 **0.13 秒**，冷启动可到 2 秒以上。app 侧因此按
  「等它连上来」设上限（`bt_session.CONNECT_WAIT_S`），不能 `open` 完就当完事。

## 授权

首次调用可能弹一次蓝牙授权（系统设置 → 隐私与安全性 → 蓝牙）。本机实测直接通过、
没有弹窗。若被拒，助手会在调用私有 API 那一刻 SIGABRT（不是弹窗被驳回那么温和），
症状是「组件一直报读不到状态」。
