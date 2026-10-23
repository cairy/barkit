<!-- doc-meta
类型: 索引
状态: live
读者: 找文档 / 检索入口
关联: [PROJECT_STRUCTURE.md]
-->

# 架构说明

本文件描述 BarKit 当前架构事实，是日常查阅的入口。代码变化导致架构变化时须同步更新。
更细的组件子系统文档（契约、决议、插件结构、开发指南、设计提案）见 `components/README.md`。

## 产品概览

BarKit 是 macOS 菜单栏常驻的个人工具集，通过插件机制提供四类能力：

| 模块 | 类型 | 触发方式 | 设计文档 |
| --- | --- | --- | --- |
| recognize（OCR+条码） | on_demand | 热键 ⌃⌥O / 托盘菜单 | [识别设计](components/识别设计.md) |
| autotype（一键输入） | on_demand | 热键 ⌃⌥P / 托盘菜单 | [一键输入设计](components/一键输入设计.md) |
| notify_guard（通知开关联动） | daemon（轮询） | 自动 + 托盘手动 | [录屏通知设计](components/录屏通知设计.md) |
| route_guard（自动路由） | daemon（事件+兜底轮询） | LaunchDaemon + 托盘 | [网络切换设计](components/网络切换设计.md) |

**当前状态**：P0 骨架 / P1 recognize / P2a autotype / P2b notify_guard / P3 route_guard / 收尾 F.1–F.4 均已 ✅（2026-08 落地，阶段史见）。

### UI 形态

| 入口 | 承载内容 |
| --- | --- |
| 状态栏图标（点击） | Popover 状态面板：各插件状态行 + 开关；底部「截图识别 / 一键输入」按钮 + 「设置 / 帮助 / 退出」。**无独立右键菜单**——所有动作都在面板内 |
| 全局热键 | 高频动作直达（⌃⌥O / ⌃⌥P / ⌃⌥M） |
| 临时窗口 | 识别窗口（含图内框选）、autotype 倒计时浮层——用完即关，不常驻 |
| 首次启动 | 权限引导页：缺哪个权限引导去系统设置开哪个（辅助功能） |

### 配置体系

- 默认 YAML 直编，只为「结构化、易写错、高频改」的项做专项 UI（autotype profiles、route 规则）；不做通用配置界面。
- 首次启动生成空配置，运行时按「出厂文件 → 插件 `config_schema()` 默认值 → 用户配置」三层深度合并（后者覆盖前者；默认值不落盘）；秒级 mtime 热重载 → schema 校验 → 广播 `config_changed`。
- 校验失败保留上一份有效快照继续运行 + 托盘提示，不崩溃不丢状态。
- UI 写回收口一律走 `core/config`（原子写 + `chmod 600`），与手编 YAML 同一入口、同一校验。

### 全局热键

CGEventTap 会话级监听（pyobjc-Quartz），进程运行期全局有效；需辅助功能权限（与 autotype 同一权限）。热键回调经 queued signal 回 Qt 主循环再触 UI；tap 被超时禁用时自动重新启用。默认键位（⌃⌥ 前缀，均可 YAML 改）：⌃⌥O 识别 / ⌃⌥P 一键输入上次 profile / ⌃⌥M 通知开关手动切换。详见 `主框架架构.md` 热键相关章节。

### 权限摘要

| 权限 | 用途 | 何时申请 |
| --- | --- | --- |
| 辅助功能 | CGEvent 注入（autotype）+ 全局热键（CGEventTap） | 首次触发 autotype 时引导 |
| 登录项（自启） | 开机常驻 | 面板设置项安装/卸载 LaunchAgent plist |
| root（一次性） | 安装/卸载 route LaunchDaemon | route_guard 面板按钮触发 |

> 权限模型细节（TCC 绑定解释器身份、LaunchAgent wrapper、App Nap 对策、单实例锁）见 `主框架架构.md` §7.1 权限枚举。

### 调度与线程模型

- scheduler 用 QThreadPool：daemon 插件的 `tick()` 跑在 worker 线程，绝不占主线程。
- tick 产出结果经事件总线广播；总线跨线程投递，UI 侧以 Qt queued 连接回主线程渲染。
- 插件崩溃隔离内置于 scheduler：`tick()` 包 try/except 记日志；`execute` 类连续失败达阈值自动停用（熔断），面板提供「重试」；`observe` 类失败只记日志不熔断。
- 所有 subprocess 调用强制 `timeout=`（10s）+ tick 级 watchdog。详见 `主框架架构.md` 调度相关章节。

> 插件接口（`id`/`name`、`run_mode`、`plugin_config()`、`required_permissions`、`start`/`stop`、`row_specs()`、`hotkeys()`）与注册启停（目录扫描、`enabled` 开关）以 `components/组件契约规范.md` 为唯一真源，`components/组件开发指南.md` 提供上手示例。

## 分层与职责

| 层 | 路径 | 职责 | 依赖 |
| --- | --- | --- | --- |
| 引擎 | `core/` | UI 无关核心：配置、事件、调度、插件基类与贡献收集 | 无（最底层） |
| 插件 | `plugins/` | 业务功能组件（autotype / hosts_guard / ipad_tunnel / notify_guard / recognize / route_guard） | → core |
| 能力 | `capabilities/` | 能力层，含 `helper/`（root daemon 提供的底层系统能力） | → core（+ 可选 shell 渲染原语） |
| 渲染 | `shell/` | 菜单栏面板、渲染器、样式（renderers / design） | → core / plugins 输出 |
| 声明点 | `contribution/` | 声明点 ⇄ Spec 类对应表，插件贡献注册 | → core |

入口（`app` / `__main__`）在顶层组装各层，持有具体实现。

## 依赖方向红线

- `core` 最底层，UI 无关，不依赖任何上层。
- `plugins` / `capabilities` 依赖 `core` 定义的接口；`capabilities` 可选择性 import `shell` 渲染原语，
  但 `core` 始终不依赖 `shell`。
- **禁止** `capabilities` / `shell` 依赖 `plugins` 的具体组件实现。
- 业务插件之间禁止直接 import 彼此实现（通过 core 定义的契约接口解耦）。
- 插件由 `pkgutil` 扫描 `barkit.plugins` 子包自动发现，无需注册。

## 关键约定

- **契约唯一真源**：组件契约以 `components/组件契约规范.md` 为准；代码与文档不一致时改代码。
  开发指南、插件结构规范、设计提案只引用不复述。
- **多套版本闸门**（变更任一套都有对应的兼容规则，详见主框架方案 §9.5）：
  - `api_version`（插件契约版本，`SUPPORTED_API_VERSION`）：**目标闸门**为声明 `< 3` 的插件在加载期直接拒载并给原因（D20 裁定）；当前代码落地状态（v2→v3 闸门是否已完成）见。
  - `schema_version`（用户 `config.yaml` 顶层结构）：不匹配即启动拒绝（`ConfigError`）。
  - `SPEC_VERSION`（Spec 数据结构，渲染器读取的字段）：渲染器遇更高版本降级渲染并告警，不崩溃。
  - `HELPER_VERSION`（root helper 的 core、进程协议与原语集合）：app 据此判定 helper 是否需更新。
- **插件隔离**：插件通过 core 定义的契约接口接入，不互相 import 实现。

## 非功能需求

| 属性 | 目标 | 如何度量 | 当前状态 |
| --- | --- | --- | --- |
| 常驻内存 | ≤150MB | 活动监视器常驻后稳态 RSS | 目标值（P0 完成后实测再收紧；OCR/条码模块懒加载，启动不占） |
| 构建体积 | venv ~300MB | `uv sync` 后 venv 体积 | PySide6-Essentials 替代完整 PySide6 已达 |

## 已知技术债

| 问题 | 影响范围 | 处理时机 | 状态 | 关联日志 |
| --- | --- | --- | --- | --- |
| ⑦ 其余四个系统事件源（network/usb/power/screen） | 新增系统能力，零消费方 | 按需另立，不主动立批 | 未做（非缺陷） | 批次开工文档 §4.3 |
| B 类资产目录与外部插件扫描根同根 | 语义混叠（功能安全） | 已注释声明 | 已知 | 批次开工文档 §6 |
| `test_route_intent` 随真机 helper/网络状态而红 | 单测稳定性 | 先核真机再判 | 存量（HEAD 同失败）。**2026-09-18 补观测**：Windows 侧这条的红是 `assert st_mode & 0o777 == 0o644`（Windows 无 POSIX 权限位，读到 `0o666`）—— 属环境差异，与原写的「随真机 helper/网络状态」**不是同一回事**；macOS 上是否另有原因待核 | 批次开工文档 §6 |

> 处理完一项即移入 并标注；本表只登记当前未解决的技术债。来源：2026-09-15 从批次开工文档 §4.3/§6 并入。
> **2026-09-18 移出 1 项**：`ipad_tunnel disconnect RuntimeWarning` —— 已由 `78db09c`（2026-09-17）的 `_cfg_conn` 守卫消解（只 stop 不 start 时不再碰信号，全仓已无该告警的产生点），见。

## 模块设计文档

各插件已实现设计（as-built）与重设计提案集中在 `components/`，本文件只做入口索引：

- [识别设计](components/识别设计.md) — recognize（OCR+条码）已实现设计
- [一键输入设计](components/一键输入设计.md) — autotype 已实现设计
- [录屏通知设计](components/录屏通知设计.md) — notify_guard 重设计提案（含已实现现状）
- [网络切换设计](components/网络切换设计.md) — route_guard 重设计提案（含已实现现状）

## 风险登记

原方案文档（2026-09-15 已分发至各组件设计 /）原 §10 风险备忘已归并：非官方 API（`dndMirrored` / CGEventTap）、root LaunchDaemon、PyObjC/Vision 随系统变化、常驻资源、IPv4-only、规则重叠——以 `主框架架构.md`「风险登记」节为权威来源，本文件仅保留上方「已知技术债」未覆盖项。

## 文档体系

BarKit 文档自 2026-09-15 起套用「项目文档模板」四类模型，当前结构：

- 根级：（AI 协作行为准则）、`../../PROJECT_STRUCTURE.md`（结构事实唯一来源：技术栈 / 检查链 / 文档分类模型）。
- `docs/README.md`：文档索引（仅登记、不复述）。
- `docs/architecture/`：现行架构事实与规范——本文件、`主框架架构.md`、`UI交互分层规范.md`、`components/`（契约规范 / 决议 / 插件结构 / 开发指南 / 设计提案）。
- `docs/guides/`：操作指南（`../guides/验证与自证.md` ／ `../guides/真机验证清单.md`，索引见该区 `README.md`）。
-：只读封存（完成台账、踩坑记录、产品开发史、已归档方案）。
-：进行中任务单一入口（当前为组件契约重构）。
-：活路线图（现行事实）。
-：方案工作区（中间态，不进索引）。

分类与流转规则以 `../../PROJECT_STRUCTURE.md` 第 7 节为唯一来源；新增文档须在**所属分区**的 `README.md`
登记（根级文档在 `docs/README.md`），禁止复制复述，同一信息只在一处定义。

## 分篇与组件文档登记表（doc-lint 登记）

| 文档 | 类型 | 状态 |
| --- | --- | --- |
| UI交互分层规范-契约清单.md | 架构事实 | live |
| UI交互分层规范-导读.md | 架构事实 | live |
| UI交互分层规范.md | 架构事实 | live |
| 一键输入设计.md | 架构事实 | live |
| 主框架架构-内核层.md | 架构事实 | live |
| 主框架架构-分层与目录.md | 架构事实 | live |
| 主框架架构-契约层.md | 架构事实 | live |
| 主框架架构-工程与门禁.md | 架构事实 | live |
| 主框架架构-插件可插拔.md | 架构事实 | live |
| 主框架架构-渲染层.md | 架构事实 | live |
| 主框架架构-能力层.md | 架构事实 | live |
| 主框架架构-迁移与决策.md | 架构事实 | live |
| 主框架架构-通信模型.md | 架构事实 | live |
| 主框架架构.md | 架构事实 | live |
| 录屏通知设计-rules处置.md | 架构事实 | live |
| 录屏通知设计-点击与影响面.md | 架构事实 | live |
| 录屏通知设计-落地顺序.md | 架构事实 | live |
| 录屏通知设计-设计正文.md | 架构事实 | live |
| 录屏通知设计-面板行.md | 架构事实 | live |
| 录屏通知设计.md | 架构事实 | live |
| 插件结构规范-UI面.md | 架构事实 | live |
| 插件结构规范-基类与样板.md | 架构事实 | live |
| 能力目录.md | 架构事实 | live |
| 插件结构规范-开放问题.md | 架构事实 | live |
| 插件结构规范-缘起.md | 架构事实 | live |
| 插件结构规范.md | 架构事实 | live |
| 组件契约决议-D21轮询.md | 决策记录 | frozen |
| 组件契约决议-台账上.md | 决策记录 | frozen |
| 组件契约决议-台账下.md | 决策记录 | frozen |
| 组件契约决议-实测证据.md | 决策记录 | frozen |
| 组件契约决议-总览.md | 决策记录 | frozen |
| 组件契约决议.md | 决策记录 | frozen |
| 组件契约规范-完整例子.md | 架构事实 | live |
| 组件契约规范-报告.md | 架构事实 | live |
| 组件契约规范-显示位.md | 架构事实 | live |
| 组件契约规范-自检.md | 架构事实 | live |
| 组件契约规范-行内控件.md | 架构事实 | live |
| 组件契约规范-认识组件.md | 架构事实 | live |
| 组件契约规范.md | 架构事实 | live |
| 组件开发指南-文档地图.md | 操作指南 | live |
| 组件开发指南-熔断可靠性.md | 操作指南 | live |
| 组件开发指南-配置与热键.md | 操作指南 | live |
| 组件开发指南.md | 操作指南 | live |
| 网络切换设计-影响面.md | 架构事实 | live |
| 网络切换设计-状态机.md | 架构事实 | live |
| 网络切换设计-设计正文.md | 架构事实 | live |
| 网络切换设计-运行模式与状态.md | 架构事实 | live |
| 网络切换设计-面板行.md | 架构事实 | live |
| 网络切换设计-契约核对.md | 架构事实 | live |
| 网络切换设计.md | 架构事实 | live |
| 识别设计.md | 架构事实 | live |
