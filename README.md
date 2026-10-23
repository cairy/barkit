<!-- doc-meta
类型: 索引
状态: live
读者: 找文档 / 检索入口
关联: [PROJECT_STRUCTURE.md]
-->

# BarKit

macOS 菜单栏常驻的个人工具集：OCR/条码识别、通知守卫、自动路由、一键输入。
插件化架构，功能可配置化启用/禁用/增删。详见 [架构说明](docs/architecture/README.md)。

## 安装

```bash
uv sync
```

## 运行

```bash
uv run barkit
```

首次运行自动在 `~/Library/Application Support/BarKit/` 生成用户配置 `config.yaml`
（只写与默认值不同的差异项，运行时与仓库 `config.default.yaml` 深度合并）。
日志落 `~/Library/Logs/barkit/barkit.log`，策略：单文件 2 MB，写满**就地截尾**、不留备份
（每个进程恰好一份日志；不是「主文件 + N 份」）。

## 模块

| 模块 | 触发 | 说明 |
|------|------|------|
| recognize | ⌃⌥O / 托盘 | OCR + 条码二合一识别窗口（截图到剪贴板后 ⌃⌥O 直接载入） |
| autotype | ⌃⌥P / 托盘 | 向任意输入框注入指定文本（密码/口令/固定文本） |
| notify_guard | 常驻 / ⌃⌥M | 通知开关联动（虚拟显示器 + 规则进程存在性检测）；⌃⌥M 手动切换，面板恢复自动 |
| route_guard | 常驻 | 自动路由：root LaunchDaemon helper（安装一次）按规则自动增删路由；面板安装/卸载/立即检查 |

## 开机自启（LaunchAgent）

**面板 → 开机自启 → 安装**：写 LaunchAgent（`~/Library/LaunchAgents/com.barkit.app.plist`），
登录后自动常驻。机制要点：

- plist 指向**稳定 wrapper** `~/.local/bin/barkit`（生成时写死本机 venv python 路径
  与项目根），venv 目录移动 / Python 重建后 wrapper 指向失效——面板会提示「重装」，
  点一次安装即可恢复。
- `KeepAlive={SuccessfulExit=false}`：**崩溃自动拉起**（实测 kill -9 后经
  ThrottleInterval 10s 上限自动重拉）；**托盘「退出」是正常退出，不会被拉活**（实测）。
- LaunchAgent 环境干净：wrapper 显式设置 PATH/LANG，无终端依赖。

**关闭**：面板 → 开机自启 → 关闭，删除 plist 并停止 launchd job（当前实例随之退出）。

## 权限

| 权限 | 用途 | 何时申请 |
|------|------|----------|
| 辅助功能 | CGEvent 注入（autotype）+ 全局热键（CGEventTap） | 首次触发 autotype 时引导 |
| 登录项 / 自启 | 开机常驻（LaunchAgent，用户域，无需 root） | 面板「安装自启」 |
| root（一次性） | 安装/卸载 route LaunchDaemon | route_guard 面板按钮 |

权限绑定"运行 BarKit 的进程身份"：升级 Python / 重建 venv 后需在
系统设置 → 隐私与安全性 → 辅助功能 里重新授权（以 `AXIsProcessTrusted()` 实测为准）。

## 故障恢复（熔断）

daemon 插件按 role 区分熔断策略（见方案 §3.4）：

- **execute 类**（会写入/执行动作，如 notify_guard）：tick 连续失败 5 次 → 自动停用，
  防止反复写坏状态。此时**面板顶部出现红色故障条 + 「重试」按钮**——排除故障后
  点重试清零计数恢复轮询；**重启 app 也会自动重置**。
- **observe 类**（只读状态刷新，如 route_guard 面板侧）：失败只记日志，不熔断。

所有未捕获异常（主线程/工作线程/Qt 内部）统一写入日志文件，可追溯。

## autotype 已知限制

- **Secure Event Input**：开启该保护的应用（1Password、部分银行/密码框）收不到注入
  事件，静默失败，不提示（系统级限制，应用主动拦截）。
- 注入前会检测输入法并自动切换到 ABC；`auto_switch_ime: false` 时需手动切换。
- 输入文本以明文存于 `config.yaml`（`chmod 600` 保护），暂不进 Keychain。

## route_guard 使用

1. 面板或菜单「安装路由守护」（弹系统授权，一次性）
2. 默认配置按 `config.default.yaml` 的示例规则运行（如有不需要的网段请在面板「配置」中改）
3. 卸载：面板「卸载守护」→ 停 daemon、清理规则路由、删守护文件（仍需一次授权）

> 若守护残留需手动清理，命令见下方「卸载 → 完整手动清理」。

## 卸载

删除仓库目录即可（配置残留于 `~/Library/Application Support/BarKit/`，日志残留于
`~/Library/Logs/barkit/`，可一并删除）。

完整手动清理（含自启与 route 守护）：

```bash
launchctl bootout gui/$(id -u)/com.barkit.app 2>/dev/null
rm -f ~/Library/LaunchAgents/com.barkit.app.plist \
      ~/.local/bin/barkit \
      ~/Library/Application Support/BarKit/config.yaml \
      ~/Library/Application Support/BarKit/helper_snapshot.json \
      ~/Library/Application Support/BarKit/helper.sock
rm -rf ~/Library/Logs/barkit
# 当前版本（label com.barkit.helper，helper 为整目录包）
sudo launchctl bootout system/com.barkit.helper 2>/dev/null; sudo rm -f \
  /Library/LaunchDaemons/com.barkit.helper.plist \
  "/Library/Application Support/BarKit/barkit-helper" \
  "/Library/Application Support/BarKit/hev-socks5-tunnel"
sudo rm -rf "/Library/Application Support/BarKit/helper"
# 旧版本残留（label com.barkit.routed，批次 4 之前的单文件形态）
sudo launchctl bootout system/com.barkit.routed 2>/dev/null; sudo rm -f \
  /Library/LaunchDaemons/com.barkit.routed.plist \
  "/Library/Application Support/BarKit/routed-helper.py" \
  "/Library/Application Support/BarKit/routed-helper"
```

## 许可证

[MIT](LICENSE)。随源码分发了一个预编译二进制 `hev-socks5-tunnel`（MIT）及其静态链接的
lwIP（BSD-3-Clause）等组件，来源、哈希与许可证全文见 [THIRD-PARTY.md](THIRD-PARTY.md)。
