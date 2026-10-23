# PROJECT_STRUCTURE.md

本文件是本项目**结构信息的唯一来源**：技术栈、目录约定、依赖方向、常用命令、
文档组织方式都写在这里。

- 行为规范（怎么写代码、怎么测试、怎么汇报）见。
- 本文件只回答"东西放在哪、怎么跑起来"，不放编码规范。
- 结构与命令发生变化时，必须同步更新本文件。

> 本文件基于「项目文档模板」的 PROJECT_STRUCTURE.md 实例化；第 7 节文档组织方式
> 沿用模板，未改动。

---

## 1. 项目概览

- **项目名称**：BarKit
- **一句话描述**：macOS 菜单栏工具集，通过插件机制提供自动输入、通知守护、路由守护等能力
- **产品形态**：桌面应用（macOS 菜单栏）
- **当前阶段**：生产 / 维护

### 已确认的关键产品决策

以下决策定义产品形态，变更须同步本文件并在 记录理由。模块级设计见 `docs/architecture/components/` 各设计文档。

| # | 决策点 | 结论 |
| --- | --- | --- |
| D1 | 技术形态 | PySide6 菜单栏应用（LSUIElement，无 Dock 图标）+ pyobjc 调系统 API |
| D2 | OCR/条码识别 | 复用 `python-skills` 的 `mac-ocr-text` / `mac-barcode-read`（均支持多区域），OCR 与条码二合一为一条识别流水线 |
| D3 | 路由特权操作 | 方案 A：一次性安装 root 权限 LaunchDaemon helper，之后永久免密自动维护路由 |
| D4 | 配置格式 | YAML（默认与用户配置深度合并） |
| D5 | 全局热键 | CGEventTap 会话级监听（Qt 6 不泵 Carbon 事件队列，GUI 进程收不到 Carbon 热键），需辅助功能权限 |
| D6 | 通知守卫逻辑 | `allow = S1 AND (NOT S2)`（S1=第二屏运行，S2=任一规则命中） |
| D7 | route helper 形态 | 常驻 LaunchDaemon + 纯轮询（默认 30s），KeepAlive 崩溃自拉起 |
| D8 | 分发形态 | 源码运行（uv 管理），不打包不签名，权限模型见 `architecture/README.md` 权限摘要 |

> 模块级设计（recognize / autotype / notify_guard / route_guard）见 `architecture/components/` 各设计文档；技术债与风险以 `architecture/README.md` 与 `docs/architecture/主框架架构.md` 为权威。

## 2. 技术栈

| 类别 | 选型 | 版本 |
| --- | --- | --- |
| 语言 | Python | >=3.12 |
| 框架 | PySide6-Essentials（Qt） | >=6.7 |
| 数据 | PyYAML | >=6.0 |
| 测试 | pytest + pytest-qt | pytest>=8.0 / pytest-qt>=4.5.0 |
| 静态检查 | pyflakes |（随环境） |
| 构建/打包 | hatchling |（pyproject 声明） |
| 包管理 | uv |（随环境） |

**选型原则**：引入新依赖前先确认仓库内是否已有同类能力；确需新增时在表格登记并说明用途。

**系统桥接**：通过 pyobjc（`pyobjc-framework-Cocoa` / `Quartz` / `ApplicationServices`）调用 macOS 系统 API；OCR/条码复用 `mac-ocr-text` / `mac-barcode-read`（本地路径依赖，见 `pyproject.toml` 的 `[tool.uv.sources]`）。**依赖清单以 `pyproject.toml` 为唯一权威**——本表仅列主要选型，新增/升级依赖以 `pyproject.toml` 为准并同步本文件。

## 3. 目录结构

```
src/barkit/
  app.py # GUI 入口：只做进程级准备（日志 / 单实例锁 / QApplication / 主题）
  cli.py # 子命令分派（barkit doctor …），在进程级准备之前拦截
  paths.py # 用户目录 / 受管数据文件路径的唯一真源
  scaffold.py # `barkit new-plugin`：生成外部插件骨架（与 cli.py 同一条 Qt-free 的 CLI 之路）
  core/ # UI 无关引擎：配置、事件、调度等核心逻辑（最底层）
  plugins/ # 业务插件：autotype / hosts_guard / ipad_tunnel / notify_guard /
  # recognize / route_guard（依赖 core 接口）
  capabilities/ # 能力层：含 helper/（root daemon 提供的底层系统能力）
  shell/ # 渲染/UI 层：菜单栏面板、渲染器、样式（renderers / design）
  contribution/ # 第三方贡献扩展入口
  doctor/ # `barkit doctor` 自检引擎：只读、Qt-free、可离屏跑（叶子，不被下层依赖）
tests/ # 与 src 模块对应的扁平测试（test_*.py + conftest.py）
docs/ # 文档，见第 7 节
```

**依赖方向（单向，禁止反向与循环；箭头表示「依赖」，一律向下，无回边）**

```
入口（__main__ / app）在顶层组装各层，持有具体实现
shell（渲染）──▶ plugins / core     # 只展示与收集输入，不写业务判断
plugins         ──▶ core            # 业务插件依赖引擎定义的接口
capabilities     ──▶ core            # 能力层依赖引擎接口，helper 由入口组装注入
core（最底层，UI 无关，不依赖任何上层）
doctor（自检）    ──▶ core / capabilities / paths
  # 叶子：不被 core / plugins / capabilities /
  # contribution 反向依赖（shell 可复用其 Report）
```

- 业务插件不直接依赖 shell；需要 UI 时通过 core 定义的接口/事件与 shell 解耦。
- 共享层（通用类型/常量/工具）放在 core 内，不含业务含义。
- 新代码放哪：先判断属于 core / plugin / capability / shell 哪一层，再放进去。

### 3.1 框架相关约定

- 菜单栏面板为 Qt.Popup 无边框窗口，透明背景（WA_TranslucentBackground）。
- 多显示器环境下面板 dismiss 行为需显式处理 click-outside。
- 插件通过 core 定义的契约接口接入，禁止插件间直接互相 import 实现。

## 4. 常用命令

```bash
uv sync # 安装/同步依赖（生成 venv）
uv run barkit # 本地运行
uv run barkit new-plugin <id> # 生成一个外部插件骨架（--daemon 出守护型；见《组件开发指南》§13.1）
uv run pytest tests/ -q # 全量测试
uv run pytest tests/ -q tests/test_x.py # 运行单个测试
pyflakes src/ tests/ # 静态检查
uv build # 生产构建（hatchling wheel）
```

**提交前的检查命令链**（按顺序全部跑通，任一失败不得提交）：

```
pyflakes → pytest
```

**强制机制（门禁不可仅靠自觉）**

- **私有开发仓**（`git@git:barkit`）没有 CI：那里没有任何工作流文件，检查链只能本地跑，
  跑通是提交前提 —— 没有「服务器替你兜」的第二道。
- **公开仓 `cairy/barkit`** 自带一份 GitHub Actions：`.github/workflows/ci.yml`，
  跑 `uv sync --locked` → `pyflakes` → `pytest`。它**没有** `doc_lint` 那一步：该门禁守的
  与 不随公开仓发布，规则失去对象。
- **门禁规则**：`pyflakes`、`pytest` 任一非零退出即不得合并；
  禁止用 `--force` / `skip` / `continue-on-error` 绕过（与 §12 一致）。
- **检查链阶段说明**：Barkit **无独立类型检查阶段**（不引入 mypy 等），
  检查链为 `pyflakes`（静态分析）→ `pytest`（测试），没有中间的 typecheck。
  `scripts/doc_lint.py` 是**文档门禁**，按第 7 节在改文档时单独跑，不属于这条链。
- **AI 协作约束**： §9/§12 要求「未跑通检查链不得提交」「禁止谎称跑过」；
  本地跑通是提交前提。

## 5. 环境与本地启动

1. 安装依赖：`uv sync`
2. 启动：`uv run barkit`
3. 首次运行在 `~/Library/Application Support/BarKit/` 生成 `config.yaml`，
  只写与默认值的差异项，运行时与仓库 `config.default.yaml` 深度合并。
4. 升级 Python / 重建 venv 后需重新绑定「运行 BarKit 的进程身份」权限。

**环境变量**：Barkit 主要通过 `config.yaml` 配置，无强制环境变量；如有新增须登记到
`config.default.yaml` 与文档。

## 6. 测试目录

```
tests/
  conftest.py # 公共 fixtures
  test_*.py # 与 src/barkit 各模块对应的扁平测试
```

测试为扁平结构，按被测模块命名（test_<module>.py），不分子目录分层。

## 7. 文档目录与分类规范

文档按「回答什么问题 + 衰变率」分类，而非按读者身份（人类 / AI agent 都按手头任务在这几类间跳）。
每篇必带元数据头，由 `scripts/doc_lint.py`（文档门禁）强制登记与校验，确保后续文档自动纳入正确体系。

### 7.1 四类内容 + 一层索引

| 类 | 回答的问题 | 衰变率 | 落点 | 禁含内容 |
| --- | --- | --- | --- | --- |
| 架构事实 | 现在是什么、边界是什么 | live / draft | `docs/architecture/` | 决策理由、变更史 |
| 决策记录 | 为什么这么定、权衡是什么 | frozen | | 现行指令（只陈述历史） |
| 任务板 | 现在到哪、下一步 | task | | 设计理由 |
| 操作指南 | 具体怎么干 | live | `docs/guides/` | 大段 why |

**索引层（meta）**：分层 README（`docs/README.md` 及各区 `README.md`）只登记、不复述，每张带一张
分类登记表（见 7.4）。索引文件自身类型标 `索引`、状态 `live`。

> 单一心智模型入口由 `docs/architecture/README.md` 头部承担（skim 友好），不单列「心智模型」类。
> AI 任务切片路由见，不塞进（避免行为准则与检索表混胀）。

### 7.2 元数据头（每篇必带，doc-lint 校验）

每篇文档顶部放一个 HTML 注释块：

  <!-- doc-meta
  类型: 架构事实
  状态: live
  读者: 写插件 / 改契约
  关联: [组件契约规范.md]
  -->

- `类型` ∈ {架构事实, 决策记录, 任务板, 操作指南, 索引}
- `状态` ∈ {live, frozen, task, draft}（`draft` 容纳未采纳的设计提案）
- `读者`：读者意图（非身份），如「接手开发 / 写插件 / 做迁移」
- `关联`：指向相关文档的相对链接，禁止复述其内容

### 7.3 命名与体量

- **主题命名，禁止 `-N` 序号后缀**（如 `docs/architecture/components/组件契约规范-显示位.md` 改为主题名）。多主题用子目录或复合主题名。
- 单文件 ≤ 300 行，超出按主题拆篇。

### 7.4 单源纪律

一条事实只有一个家；别处只能链接不能复述。理由只住「决策记录」，架构事实只陈述「是什么」。
跨文件禁止大段复制。

### 7.5 分层索引登记表（执行面）

每个分区 `README.md` 含一张表，doc-lint 据此校验登记与类型一致：

  | 文档 | 类型 | 状态 | 一句话 |
  | --- | --- | --- | --- |
  | 主框架架构 | 架构事实 | live | 重构方案总纲 |
  | 录屏通知设计 | 架构事实 | draft | 重设计提案(未采纳) |

### 7.6 门禁（doc-lint）

`scripts/doc_lint.py` 校验：doc-meta 存在且类型/状态合法、无 `-N` 文件名、≤300 行、已在分区 README
登记。接 git `pre-commit` 钩子；迁移期用 `--advisory`（只报不卡），全量整改后切严格模式。新增文档未过
doc-lint 不得提交（与 §9 一致）。

- 分类与流转规则以本节为唯一来源；新增文档必须在对应 `README.md` 登记并按类归位。

### 7.7 方案流转（草案 → 中间态 → 归档）

| 阶段 | 落点 | 规则 |
| --- | --- | --- |
| 草案 / 仍在设计 | | **中间态**：不进文档索引、**不带 doc-meta**、不登记（`doc_lint` 的 `SKIP_DIRS` 跳过本目录） |
| 实施完成 | | 移入归档，**补写「实施结果」**（实际做了什么 / 与原方案的偏差 / 踩到的坑）＋「遗留问题」；随文件带上 doc-meta（类型=决策记录、状态=frozen）并登记进 |
| 被否决 / 放弃 | | 同样移入，**必须写明原因**，避免后人重复提出 |

- 归档即**只读封存**：决策后来被推翻时**不回头改归档**，改为更新现行开发文档的结论 ＋ 另写一篇新日志说明变更。
- 移入时若篇幅超 300 行，按主题拆篇（同 §7.3）；**同步全仓指向它的引用**——本项目有在代码 / 测试注释里引用方案的惯例，搬家会一并打断那些指针。

### 7.8 任务板门禁（专用，不许堆日志）

 是唯一常驻任务板，只回答「已经做到哪」与「还有哪些没做」，不排期、不给下一件建议。门禁
（`scripts/doc_lint.py` 的 `lint_taskboard`，按文件名锁定本文件）对它有三道硬约束，违反即非零退出：

- **总字节 ≤ 50KB（51200）**：防 balloon 回早期 151KB。
- **单行 ≤ 800 字符**（Unicode 计）：一条任务一行；超了就是一段叙述压进了一行。
- **禁止堆日志**：命中即拒的过程 / 验收类签名——`N passed` / `N failed` 计数、`RC=` 门禁返回码、
  `pyflakes <数字>` 基线、`门禁自证` / `反向验证` / `节点集` / `全量 **N` 这类自证报告用语。

**家在别处**：根因推演、方案权衡、测试数字、踩坑经过 →；pyflakes 基线 / Windows 跑法 /
历史数字口径 → `docs/guides/验证与自证.md`；每批加一条的模板见 文件头。
合格条目形态（照抄文件头模板，别自由发挥）：

  - **<主题>（<日期>）**：<改了什么> ＋ <意味着什么> → 

> 注： 下的冻结台账与 是路线图 / 历史归档，不在此门禁范围（它们本就允许含测试证据）。

## 8. 维护约定

出现以下变化时，同步更新本文件：

- 新增或移除依赖、升级主版本
- 新增顶层目录，或调整分层与依赖方向
- 新增构建、检查、打包、部署命令
- 新增环境变量 / 配置项
