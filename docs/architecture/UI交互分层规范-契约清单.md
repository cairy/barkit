<!-- doc-meta
类型: 架构事实
状态: live
读者: 架构查询 / 改契约
关联: [主框架架构.md]
-->

## 8. 契约清单

### 8.1 新增的扩展点

> 完整的声明点清单（11 个声明点 × 方法 / 返回类型 / 基数）见《组件契约规范》§1.3；字段定义权同在该文档。

在框架方案 v3.0 §5.1 的 `contribution/points.py` 中新增：

```python
POINT_STATE = "state" # 状态声明（StateSpec）
POINT_COMPONENT = "component" # 组件身份（ComponentSpec，供标题栏图标条）
POINT_DETAIL_ENTRY = "detail.entry" # 详情页入口（DetailEntrySpec）
```

### 8.2 新增 / 修订的 Spec

**字段定义见《组件契约规范》§2 / §4.2 字段总表**（`component()` / `state_specs()` / `row_specs()` / `detail_entry()` 各自返回什么、基数多少，那里是唯一真源）。本节只保留**与框架方案既有 Spec 的处置关系**。

**与框架方案既有 Spec 的关系**：

| 既有 | 处置 |
|---|---|
| `ChipSpec` / `POINT_HEADER_CHIP` | **退役**，职责并入 `ComponentSpec`（身份图标）+ `StateSpec`（状态机） |
| `StatusRowSpec` | 保留；健康角标与副标题**不在 Spec 上声明**，由代表态派生（契约规范 §1.4），插件只声明图标 / 标题 / 控件 / 逃生舱 |
| `SettingsPageSpec` | 保留，语义按 §7.2 改判 |
| `widgets.Badge` | 退化为 Shell 内部渲染原语，插件不再直接实例化 |
| `status_item.compute_tray_badge` | 已由渲染层的纯函数 `tray_queue()` 取代（现名，见《主框架架构-渲染层》§6.6） |

### 8.3 注册中心新增

> ◷ 2026-09-19 核对：下表**只有第一行成立**，第二、三行是设计意图（未实装）。

| 方法 | 作用 | 现状 |
|---|---|---|
| `registry.plugin_order()` | 插件间顺序的唯一调用源（三处渲染共用） | ✓ 实装 —— 但在 **`core/registry.py:293`**，不在 `shell/` |
| `registry.worst_level(plugin_id)` | 该插件代表态，驱动标题栏角标与 L2 健康点 | ✗ 未实装 —— 真身是**自由函数** `shell/design/levels.py:56 worst_level(levels)`（吃级别集合，不吃 `plugin_id`）；代表态取最坏级在 `shell/panel.py` 内联派生 |
| `registry.aggregate_tray_badge()` | 托盘徽标聚合 | ✗ 未实装 —— 真身是 `shell/renderers/badge.py::tray_queue(candidates, order, *, min_level)`（数据类 `Candidate`），见《主框架架构-渲染层》§6.6 |

> 落点说明：`plugin_order` 落在 `core/registry.py` —— 它读的是 config 顶层 `plugin_order`，与 UI 无关，
> 放核心层是对的；真正属于渲染侧派生的只有 `worst_level` 与托盘聚合两个，它们在 `shell/`。
> **初稿写的 `shell/registry.py` 从未存在。**

---

## 9. 门禁测试

框架方案 v3.0 §10 已有 30+ 条架构门禁。本节补充**本规范专属**的不变量（同样为待建清单）：

```python
def test_render_order_follows_plugin_order():
  """标题栏图标条 / L2 状态行 / 设置窗插件分页，三处顺序必须一致且等于 plugin_order()。"""

def test_row_not_clickable_without_detail_entry():
  """未声明 DetailEntrySpec 的插件：整行 hover 无可点反馈、点击不弹窗。"""

def test_tray_badge_aggregates_declared_states():
  """托盘角标只由 `StateOption.alerts=True` 且级别 ≥ 全局 `tray.min_level`（默认 warn，`ERR` 强制）的状态驱动；不靠 key 字符串判断。"""

def test_health_dot_matches_titlebar_badge():
  """同一插件的 L2 健康角标与 L1 标题栏角标必须同源（同一 worst_level）。"""

def test_titlebar_overflow_follows_plugin_order():
  """图标条超过 N_VIS 时，保留 PluginOrder 前 7 个，其余进溢出簇（不按问题优先）。"""

def test_detail_form_restores_activation_policy():
  """详情页弹出切 .regular；关闭后若本为 accessory 则复原（可 mock NSApp）。"""

def test_uninstall_removes_plugin_from_order():
  """卸载后 plugin_order 无孤儿 id（与框架方案 §9.1 第 3.5 步配套）。"""
```

**不引入像素级截图对比作为门禁**：offscreen 环境的字体渲染与真机存在差异，噪声会淹没真实回归。真机目测仍是最终判据，但不应该是唯一判据。

---

## 10. 落地顺序与批次落点

### 10.1 批次落点（必须先定）

框架方案 v3.0 §12 待决 18 已定：**本规范是框架方案批次 7 之后的独立一轮**。

理由：框架方案批次 1/2/3/5 的验收标准都写「面板行 / 设置页**视觉等价**」，意味着那几个批次只做**契约迁移、视觉不变**。若本规范与它们混做，那几个批次迁完的 UI 会在本轮全部返工。

因此拆两个阶段：

| 阶段 | 内容 | 依赖 |
|---|---|---|
| **U1 · 契约就绪** | §4 状态模型、§8 契约清单、§6 顺序规则 | 随框架方案批次 0/6 落地（Spec 与 `plugin_order` 属于契约层） |
| **U2 · 视觉落地** | §5 组件规格、§7 入口规则、§5.6 底部栏 | 框架方案批次 7 之后独立执行 |

### 10.2 U2 的执行顺序

1. **契约层**：`StateSpec` / `ComponentSpec` / `DetailEntrySpec` + `plugin_order` 配置 + 三个派生入口（各自的实装状况见 §8.3 的 ◷ 表）
2. **排序编辑器**：`PluginOrderEditor` + 持久化 + 三处渲染接 `registry.plugin_order()`
3. **渲染层**：`TitleBarIcon` 图标条 + `StatusRow`（健康角标 + 模式徽标，去插件自绘 Badge）
4. **样板迁移**：`notify_guard`（chip + switch + 徽标，覆盖最全）+ `ipad_tunnel`（反模式修正），再推广其余插件
5. **入口规整**：ipad_tunnel 的安装/诊断从设置页迁到详情页

### 10.3 与现有折叠机制的关系（必须先处置）

本规范 §5.2 要求「标题栏去掉折叠控件、改为点图标展开列表」，这会重做 `panel.py` 的现有折叠：

- `panel.py:73` `self._folded = True`（启动即折叠）
- `panel.py:120` `_ChevronButton` + `toggle_fold`
- `panel.py:242` `_animate_status` + `_sync_status_cap`（`maximumHeight` 复位逻辑）

**而 `test_panel_fold_toggles_container_height` 正是当前唯一的存量失败测试。**

处置原则：这三项（折叠控件、展开动画、那个失败测试）**作为一个整体重做**——新机制落地时删除旧的折叠实现与该测试，并在 U2 的验收里补上「点图标展开 / 再次点击收起 / 点面板外收起」三条新用例。不允许「新机制上线、旧测试还挂在那」。

---

## 11. 真机验证清单

> 本节是**这次改造专属**的验收项。常驻的真机验证规程（环境前置 / 通用矩阵 / 分场景 /
> 留痕格式）见 `../guides/真机验证清单.md`；**当前待验条目**见 §4。

以下均为**人工验证**项（无 CI 覆盖，其中主题与显示器相关项必须在真机双主题下确认）：

- [ ] Light/Dark 下：popover 卡片、TitleBarIcon、健康点、ModeBadge、Switch 颜色均取自 TOKENS，无硬编码
- [ ] 健康三级 + off/idle 在深浅下对比度 ≥ 4.5:1（正文）/ 3:1（大字）
- [ ] hover 态（行 `accent_soft`、pill-ghost 转 `surface2` + accent 边）双主题均可见
- [ ] 托盘角标红/灰点在菜单栏背景下均清晰（含深色壁纸）
- [ ] 点击外关闭在双主题、多显示器（显示器 A/B）下均生效
- [ ] ModeBadge 点击切换：一次点击只触发一次回调（UniqueConnection 验证）
- [ ] 详情页弹出时 Dock 图标出现；关闭后若本为 accessory 模式则复原隐藏
- [ ] 详情页打开期间 Cmd+Tab / Dock 点击可找回窗口；关闭后焦点回到菜单栏面板
- [ ] 在设置页拖动调整插件顺序后，标题栏图标条、L2 条目、设置窗插件分页三处顺序同步变化
- [ ] 未声明 `DetailEntrySpec` 的插件：整行 hover 无反馈、点击不弹窗
- [ ] 点标题栏图标展开列表 / 再次点击收起 / 点面板外收起，三条路径均生效（替代原折叠机制）
- [ ] 标题栏图标超过 7 个时，溢出簇显示的隐藏数与 `plugin_order` 截断一致
