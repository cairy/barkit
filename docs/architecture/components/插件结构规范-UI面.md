<!-- doc-meta
类型: 架构事实
状态: live
读者: 写插件 / 包结构
关联: [组件契约规范.md]
-->

## 7. UI：设置面与运行期面

### 7.1 设置面的两个声明点：`build` 是**唯一可用**路径；`schema` 保留但不接线

> **字段定义见《组件契约规范》§2.8「详情页与设置页」**；本节只讲「这两个声明点在包内怎么放、怎么落地」——即**归哪个文件 + 稳定门面**（§7.2），**不重述字段**（§0 元规则）。

本节要判的只有一件事：**`schema` 与 `build` 两条路，今天哪条能走。**

**四条实测事实：**

1. **渲染器不存在。** `shell/settings.py:81` 与 `:202` 是插件页的唯一消费点，两处都只读 `p.build`：

  ```python
  pages += [(p.label, p.build) for p in plugin_pages]
  ```

  `shell/` 内对 `.schema` 的引用为 **0**。数据契约 `core/schema.py`（`FormSchema` / `FieldSpec` / `FieldType`）与渲染提示 `contribution/form.py`（`WidgetHint`，6 个取值）**都已就位**，但全仓**无任何消费方**——缺的不是契约，只是渲染器。
2. **P0 崩溃路径**：只填 `schema` 不填 `build` → `shell/settings.py:93` 执行 `builder()` → `TypeError: 'NoneType' object is not callable`，**打开设置窗即崩**。
3. **存量使用为 0**：全仓 6 个 `SettingsPageSpec`（`autotype:178` / `route_guard:538` / `ipad_tunnel:665` / `hosts_guard:368` / `notify_guard:277` / `_template:72`）**全部走 `build`，`schema=` 出现次数为 0**。
4. **存量页面没有一个是「配置表单」**——逐页核对，全是「动作入口 + 说明」：`notify_guard:285`（「恢复自动接管」按钮）、`route_guard:541`（打开规则配置 / 立即检查 / 安装卸载守护）、`autotype:228`（打开输入项配置）、`ipad_tunnel:747`（打开隧道配置）、`hosts_guard:371`（守护状态标签 + 管理入口）。
 （**2026-09-14 注**：`notify_guard` 的设置页将在试点中**重建**为规则编辑器——见 §11 阶段 2 的口径。它仍然是**自绘**，所以本结论不变：**渲染器救不了它**。）

**由 3 + 4 得出的关键结论**：自动渲染器今天**零受益者**。把 `FormSchema` 渲染成表单，一个存量页面都替换不掉（它们本来就不是表单），只有「后续新增的纯配置表单型插件」会用到。故接线是**收益待证的挂账项**，不是阶段 0 的止血动作。

因此本规范规定：

1. **设置页必须提供 `build`**；只填 `schema` 是崩溃路径，禁止。
2. **`build` 必须带 `reason`**（字段契约既有要求）。实测 6 个 `SettingsPageSpec(build=...)` 中只有 `autotype/plugin.py:184` 填了，其余 **5 处**未填：`route_guard:538`、`ipad_tunnel:665`、`hosts_guard:368`、`notify_guard:277`、`_template:72`。（第 2 稿此处写「7 个 / 6 处未填」并多列了一个不存在的 `ipad_tunnel:663`，实际总数为 6。）
3. **`schema` 保留字段、不接线**（§13.1 决议）。接线方案已备好口径：渲染器落 `shell/`（提示词表在 `contribution/form.py`），取值读 `plugin.plugin_config()`（已合并默认值），写回复用 §6.3 的 `Config.update_plugin_section()`，校验复用 `FormSchema.validate()` / `FieldSpec.check_value()`，`_` 前缀保留键须跳过（`core/schema.py:24`）。**启动条件**：出现第一个「纯配置表单」型插件，或存量页面中有 ≥1 个被判定为「本质就是若干配置项」。
4. **补 fail-fast（框架侧一行级改动，进阶段 1）**：`SettingsPageSpec` 既无 `build` 又无 `schema` → 在 `core/contributions.ContributionRegistry._collect()` 抛 `ValueError`，走既有崩溃隔离（插件记 `failed` + 可读原因）。把「打开设置窗才崩」前移为「加载期明确拒绝」，并加门禁 `test_settings_page_is_buildable`（§12.2）。
  **（2026-09-14 已落地）**实现是 `_validate_contributions()`（静态方法，由 `_collect` 在 `try` 内调用，与崩溃隔离共用同一条路径）；行为验证是 `test_settings_page_fail_fast_isolates_plugin`（坏页隔离、好页照常）。落地时细化了一条口径：**只拒绝「双缺」，不拒绝「只填 `schema`」**——后者是合法声明，今天崩在「渲染器缺位」而非「声明不完整」，归决议 3 的挂账，不在这里拦。
  **（2026-09-22 补第二条口径）**这条只管**组件声明的**页；框架自出的那一页（组件不贡献内容、框架按「有没有东西可画」自己造外壳，见 `shell/settings/pages.py::_plugin_page` 的 `own is None` 分支）两者都空且**合法**，不适用。

> 一处必须提醒的边界：`contribution/form.py:14-22` 的 `WidgetHint` **没有 `LIST`**，而 `core/schema.py:84-86` 的 `FieldType.LIST` 与之不对称。`notify_guard/config_schema()`（`notify_guard/plugin.py:248`）的 `rules` 正是 `FieldType.LIST`——**将来接线时，含 `LIST` 字段的 schema 必须有显式策略**（跳过并由插件自绘），否则渲染器一上线就在试点插件上崩。
> **2026-09-14 已确认**（《组件契约决议》D30 ＋【契约规范】§2.8）：集合型（含 `LIST`）字段**只能自绘**，
> `FormSchema` 只承载平坦字段。渲染器接线时对 `LIST` 的策略是「**跳过 + 由插件自绘**」，不是「补一个列表编辑器」。

### 7.2 设置面归置

- 所有**设置类** UI（设置页 + 它打开的子对话框）**收进 `settings.py`**；`config_dialog.py` 这类模块名不再使用。**跨组件字面相同的那一块**另抽到 `shell/`（仍按职责命名、不认识任何插件、动作与状态全由回调注入）：装置运维段（立即检查 / 安装守护 / 卸载守护 / 过期重装）在 `shell/daemon_ops.py`，供装同一个 root helper 的组件共用（`ipad_tunnel` 与 `route_guard` 已于 2026-09-18 改用）。**◷ 2026-09-28 再进一步**：那一段**不再由组件自己调** —— 框架按**声明**（`resources().helper_tasks` / `needs_root`）把卡注入到组件设置页的**页尾框架区**，动作与文案由 `capabilities/helper_ops.py::HelperOpsCapability` 给（三个用该 helper 的组件共用一份，`hosts_guard` 自搓的那套随之消失）。
- **设置页与详情页同属设置面**：`settings_page().build`（设置窗左侧一页）与 `detail_entry().build`（用户**点组件列表整行**进入、由框架托管，`presentation="window"`）都是经贡献体系声明、由框架托管的界面，与运行期面「业务时刻自己弹」有本质区别，因此**都落 `settings.py`**；旧名 `config_dialog.py` 一并废除（三份分别于 2026-09-17 `route_guard` / 09-18 `ipad_tunnel` / 09-19 `autotype` 随各自设置面收归**整文件删除**）。
- **集合型配置（可增删的列表、每项自己是一组多字段）可以落这两者中的任一个**——「是不是集合型」只决定**要不要自绘 `build`**（出现"第 N 项的第 M 个字段"这种两层索引就够格），**放在哪**由**使用频次**决定（每天都用 → 详情页；配一次就不再动 → 设置页）。判据真源见《组件契约规范》§2.8 与《组件契约决议》D30。
- **模块名以本文为准**：编辑器的载体是 `settings.py`（过厚时按下面那条拆 `settings_rules.py` / `settings_entries.py`），**不新建 `editor.py` 这类平行模块名**——《录屏通知设计.md》§8.1 已于 2026-09-14 对齐为 `plugins/notify_guard/settings.py`；若将来又出现平行命名，以本文为准。
- **运行期窗口不算设置面**：`manager_view.py` 之类生命周期随窗口的独立窗口归 §7.3 的运行期面（模块名 `<name>_view.py`），**不**并入 `settings.py`——并入只会把设置面变成第二个神文件。
- `settings.py` 过厚时可拆子模块，规则：**按 UI 子职责命名**（`settings_rules.py` / `settings_entries.py`），**保持 `settings.py` 为稳定门面**（只暴露 `build_page()`），`settings_page()` 仍只返回一个 `SettingsPageSpec(build=settings.build_page, reason=...)`。**运行期面同此例**（`manager_view.py` 2026-10-03 拆成 `manager_<职责>.py` 一族，入口仍是唯一门面）；插件包不许子目录（§2.3）⇒ 一律**同目录平铺兄弟模块**，不是子包。
- 简单插件（如 `notify_guard`）单文件 `settings.py` 即可，勿过度拆分。

> **更正第 1 稿的依据**：第 1 稿说「`config_dialog.py` 被禁的根因是有平行入口（脱离贡献体系）」。实测不成立——`route_guard` 的对话框只经贡献页按钮触发（`plugin.py:354-362` ← `open_config()`），`hosts_guard` 的管理窗同样（`plugin.py:396-397` ← `open_manager()`），**不存在平行入口**。真正的理由是 §4 跨面禁令第 3 条：**UI 直接读写 config**。规范不得用一个不存在的根因立硬规则。（◷ 2026-09-20：句中那两处符号**今已不存在** —— 两者的集合编辑器入口都已收敛为**面板整行 → 框架 `DetailHost`**：`open_config()` 随配置窗体迁进详情页而删；`open_manager()` 与设置页那枚「管理条目…」按钮同日删（设置页不再放启动键）。原论证说的是**当时形态**，结论不受影响。）

### 7.3 运行期面

插件在业务时刻自己打开的窗口 / 浮层 / HUD，归**运行期面**，与设置面并列：

| 现状文件 | 性质 | 归置 |
|---|---|---|
| `recognize/window_view.py`(674) | 识别主窗口（`WA_DeleteOnClose`） | 运行期面，**已是正确范式** ✓（2026-09-19：`plugin.py` 清掉 5 个**死 import** 后转正，PySide6 白名单已撤其条目；同日按 §7.2 改名对齐 `*_view.py`） |
| `recognize/selview.py`(336) | 主窗内的框选视图（`SelectionView(QGraphicsView)`，挂 splitter） | 运行期面的**构件**，非独立窗口（故不适用 `<name>_view` 后缀） |
| `autotype/countdown_view.py`(313) | 倒计时 HUD | 运行期面 ✓（2026-09-19 由 `countdown.py` 改名，随阶段 3） |
| `hosts_guard/manager_view.py` | 条目管理窗口（生命周期随窗口） | 运行期面 ✓（2026-09-19 由 `manage_dialog.py` 改名。类名说的是**控件的 Qt 种类**，不跟模块名逐字对齐。**◷ 2026-10-03 已平铺拆成同目录九模块**：入口只剩类声明 ＋ 构造 ＋ 组合根，六枚混合类各管一屏，见 §7.2 末条） |
| ~~`ipad_tunnel/config_dialog.py`~~（**已删**，2026-09-18） | 隧道配置 ＋ 诊断窗 | **两块都归设置面** `settings.py`（§7.2）：配置进设置页；诊断 = 详情页 `presentation="window"`，壳归框架 `DetailForm` —— 详见 |

**运行期面的正确范式（现有合规样本，直接固化）**：`recognize/plugin.py:48` —— `window.py` 只吃 `self.plugin_config()` 返回的 dict，**不碰 config、不碰 ctx**：

```python
self._window = RecognizeWindow(self.plugin_config())
```

即：**运行期 UI 的数据由插件方法注入；UI 模块自己不知道该去哪读配置。**（**面板行已不再是这个范式的样本**：2026-09-20 起 autotype 的行改回声明式、由框架渲染，原 `autotype/panel_view.py` 已删 —— 原因见《组件契约规范-行内控件》。）

### 7.4 运行期 UI 不得私持线程 / 时钟 / 事件源（硬，已有门禁）

沿用「调度权归框架」：插件的线程 / 时钟 / 文件事件源一律经 `resident_services()` / `subjects()` **声明**（事实由框架的会话订阅推来，组件不自己盯任何东西）。现有门禁 `tests/test_architecture.py`（`test_no_private_timer_in_plugins`）已覆盖 `QTimer` / `QThread` / `QThreadPool` / `QFileSystemWatcher` / `threading` / `time.sleep`，并按 `PLUGIN_TIMER_WHITELIST` 逐条豁免；每条都带 reason，且 `test_timer_whitelist_entries_are_live_and_justified` 守着它**不许悬空**（文件不存在、理由过简、或指着已收口那一族，都当场红）。**条数与逐条列举以那份表为准，本页不再抄一遍** —— 抄一遍就是第二份真源：2026-10-08 旧翻页钟组件退场时，这一页写着的「8 条」与实际条数早已对不上，而它自己不会红。**运行期面同样适用，不因「UI 内部」而自动豁免——须走白名单**。

---

## 8. 跨插件边界

### 8.1 禁止插件 → 插件 import（迁移目标）

**现状违规 2 处，都指向 `route_guard.daemon`**：

| 位置 | 内容 |
|---|---|
| `ipad_tunnel/__init__.py:13` | `from barkit.plugins.route_guard import daemon # 复用同一 root helper` |
| `hosts_guard/plugin.py:34` | `from barkit.plugins.route_guard import daemon` |

`ipad_tunnel` 这处还兼有「把包 `__init__` 当别名垫片」的作用：`plugin.py:50` 写的是 `from barkit.plugins.ipad_tunnel import daemon`，而 **`ipad_tunnel/` 下并没有 `daemon.py`**——这行之所以能跑，全靠 `__init__.py:13` 把 `route_guard.daemon` 绑成了本包的 `daemon` 名字。这同时违反 §2.4（`__init__` 只放 docstring）。

**根因**：`route_guard/daemon.py`(493 行) 承担的是**框架级职责**（单一 root helper 的安装 / 卸载 / 状态 / 版本对账，三个插件共用），却是某插件的私有模块。这不是「插件耦合插件」，是**框架基础设施长在了插件里**。

**收口动作（独立原子步，先于门禁落地）**：把 helper 客户端上提到**框架侧**。归属与命名**已定案，2026-09-14 改定案**（见下）：

```
capabilities/helper_client.py # 从 plugins/route_guard/daemon.py 上提；三插件改用框架模块
```

> **改定案（2026-09-14，用户裁定）**：原定案是 `core/helper_client.py`，理由是「`capabilities/`
> 的语义是能力 mixin」。实施时实测发现它**与项目自己的分层门禁冲突**，故改放 `capabilities/`：
>
> | 冲突点 | 证据 |
> |---|---|
> | `core` **禁止** import `barkit.capabilities` | `tests/test_architecture.py:72`，`KNOWN_VIOLATIONS` 现为**空**且纪律「只减不增」 |
> | 项目既有的合规写法是「**名级副本**」，只够表达**名字** | `core/rootbin_reconcile.py:20-21` 原文：「``core/`` 依赖 ``capabilities/*``，故此处**不 import** ``rootbin``——只按 ``installer.KNOWN_ROOT_BINARIES`` 这个名级副本过滤」 |
> | 但本模块要的**不只是名字** | `rootbin.verify_file()` 依赖 `ROOT_BINARIES` 里**硬编码的 sha256 发行态基线**；复制一份进 `core` ＝ A 类数据出现**第二真源**（现有门禁只校验名字集相等，校验不到哈希） |
>
> 放 `capabilities/` 后：与 `helper/` 同包（依赖天然合规）、零新增门禁白名单、零第二真源。
> 代价是 `capabilities/` 的「唯一例外」从 `helper/` 一处变成「helper 的两半」——
> `capabilities/__init__.py` 的 docstring 已同步改写。

收口后 `ipad_tunnel/__init__.py` 恢复为纯 docstring，`plugin.py` 改为
`from barkit.capabilities import helper_client as daemon`；顺带修掉了 `shell/app_shell.py`
里「shell 直接 import 插件模块」这处越层引用（它此前还得靠一条 TODO 挂着）。

**上提时必须同时处理的两个切分点**（都会静默炸，不是「搬文件」那么轻）：

1. **仓库内源码路径的推导深度变了。** `daemon.py:71` 原写的是
  `HELPER_SRC_DIR = Path(__file__).resolve().parent.parent.parent / "capabilities" / "helper"`——三级 `.parent` 相对**自身文件位置**上溯，当时落点是 `src/barkit/`。搬到别处后同一串 `.parent` 会多上溯一层、落到 `src/`，解析成 `src/capabilities/helper`（不存在）→ `resolve_sources` 报「源缺失」→ **安装器拒绝安装**（与 §3.3 完全同一个失效模式）。**处理：改为与自身同级的 `parent / "helper"`，并加两道守卫**——`install()` 本地预检里 `HELPER_SRC_DIR.is_dir()` 不成立即**拒绝安装且不弹授权**（失败点贴近原因），外加单测 `test_helper_src_dir_resolves_to_real_directory` 把它钉死。**（已落地 2026-09-14）**
2. **`uninstall()` 里嵌着 route_guard 的领域知识。** `daemon.py:460-486` 的内嵌脚本会读 `routes.json` 并对每条 destination 执行 `/sbin/route delete`。上提后框架客户端会携带**某个插件的期望态文件名**＝框架依赖插件，方向反了。正确切分：框架侧 `uninstall()` 只做「bootout → 删 plist / 启动器 / helper 目录 / 完整性基线 / A 类二进制」；**期望态由各插件在自己的 `teardown()` 里逐条撤**（与 `on_disabled` 同款口径），且必须在 bootout **之前**完成。顺带：`:66-67`、`:70` 的旧形态残留清单与 `LEGACY_PLIST_LABELS`（`:50`）属于框架级迁移史，随模块一起走，无需拆。

> ✅ **切分点 2 已落地（2026-09-17）**：`uninstall()` 里那段内联路由清理已删除，框架客户端
> 不再认识任何插件的期望态文件名。落点表见《重构完成台账-近期骨架》§A23。
>
> 当初为什么不能顺手切、后来怎么解决的（**这段口径值得留着，因为它是被实测推翻过一次的**）：
> 撤期望态与 bootout 之间**缺一个确认环节**。实测 helper 的退出钩子（`lifecycle._on_exit`，
> SIGTERM/atexit → `tunnelops._teardown_tunnel`）**只复原隧道态**（`/1` 路由 / DNS / IPv6），
> **完全不碰 `route_guard` 的静态路由**；`routeops` 里也没有任何 shutdown / clear 入口 ——
> 只有 `run_cycle()` 按期望态收敛。所以「插件写完全撤期望态 → 立刻删文件 / 立刻 bootout」
> 都**不够**：helper 还没跑完那一轮，状态就泄漏在系统里了。
>
> 而「等 helper 撤净」这个等待**只能落在框架侧**（插件侧禁用 `threading` / `time.sleep`，
> 门禁 `test_no_private_timer_in_plugins` 守着），当初据此判为「会让主线程阻塞数秒、需单独
> 裁决」。裁决结果**不是**阻塞，而是**延迟执行**：收尾挂到框架心跳上
>（`core/deferred_cleanup`），判据由插件声明（`PluginResources.deferred.ready()`），框架只
> 负责「等」与收尾本身、**不解释文件内容**。
>
> ⚠️ 延迟范围必须是**整段收尾**（删数据文件 ＋ 删配置段 ＋ `forget()`）：`forget()` 会摘掉该
> 插件的 helper task，只延迟删文件的话消费方**被敲醒也不会再跑那条 task**（两层坑详见 §A23）。

### 8.2 `__init__.py` 合规样本

`hosts_guard/__init__.py`（15 行纯 docstring、零 import）是合规范例；且它证明了 **`from pkg import submodule` 不依赖 `__init__` 转手**（`hosts_guard/plugin.py:31` 的 `from barkit.plugins.hosts_guard import resolve, store` 照常工作）。所以 `ipad_tunnel/__init__.py` 的那行 import **要么删掉、要么按 §8.1 收口**，没有第三种理由。

---

## 9. 命名约定

- **模块名 = 职责**：`engine` / `resolver` / `decider` / `backend` / `signals` / `intent` / `rules` / `settings` / `state` / `<name>_view`。禁 `utils` / `common` / `helpers` / `misc`。
- **契约方法名**遵循框架既有约定（框架按名调用，**不加** `_` 前缀）。**清单不在本文维护**——以《组件契约规范》§1.3「声明点 ⇄ 方法」表为唯一真源（含 `tick` / `start` / `stop` / `teardown` / `resident_services` / `subjects` 等生命周期方法）。同一份清单只能有一处，否则必然漂移。
- 内部 helper 用 `_` 前缀。
- **包内自引用**用相对导入或直指子模块；不得经包 `__init__`（§2.4）。

---

## 10. 模板（`_template`）

### 10.1 硬性要求

`_template` **必须覆盖两种真实形态**，且两者都要能直接复制成可用插件：

| 骨架 | 必须演示 |
|---|---|
| **动作型** `class Plugin(Plugin)` | `RUN_ON_DEMAND`、声明点方法（`component()` / `row_specs()` / `state_specs()` / `settings_page()` / `actions()` / `hotkeys()`）、`_do_action`、`_snapshot`、`settings.py` 里的 `build_page()` |
| **守护型** `class Plugin(AutomationCapability)` | `RUN_PERIODIC` + `FAIL_*`、`tick()`、`resident_services()`、`subjects()` + `on_observed()`（`observed()` 取最新那份；**不读任何文件**）、`declare()` / `run_op()`、`resources()`（`data_files` / `seed_files` / `helper_tasks`）、`automation_active` / `set_automation_active` / `mode_badge_spec`、`mark_dirty` 用法 |

### 10.2 现状与放置

**2026-09-14 前的现状**（第 1–3 稿的记录，当时的那两个名字今已称作 `AutomationCapability` / `RUN_PERIODIC`）：`_template/plugin.py`（91 行）只有动作型的**退化版**——不含 `Guard` / `KIND_DAEMON` / `resident_services` / `file_watches` / 状态文件读写 / 自动化徽标，全文对 `Guard|KIND_DAEMON|resident_services|tick|file_watches` 的唯一命中是 `plugin.py:13` docstring 里「`tick` 返回」这个词，**零实现**；而且它把 `build` 直接写在 `plugin.py` 里（运行期 `PySide6` import），与新立的门禁 `test_plugin_py_has_no_pyside6` 冲突。

**已交付的骨架（阶段 1，2026-09-14）**——比第 3 稿的建议多一个 `settings.py`，因为门禁要求 `plugin.py` 零 PySide6，设置页必须有别的归处（§7.2）：

```
_template/
  __init__.py # 只放 docstring，指路两个骨架
  plugin.py # 动作型骨架（可直接复制改名）
  plugin_daemon.py # 守护型骨架；文件头注明「复制时改名为 plugin.py」
  settings.py # 设置面（设置页 + 详情页 + 子对话框）；两种骨架共用
```

两个骨架都在 `__init__` 之外零副作用，并且**自带冒烟测试**（`tests/test_plugin_build_smoke.py`）：① 两者的声明点方法都能过 `ContributionRegistry` 的收集期门禁（含 §7.1 决议 4 的 fail-fast）、② 动作型的设置页能真实构建（经 `settings.build_page`）、③ 守护型的声明面（长驻服务 / 文件事件 / 资源 / 自动化能力 / 配置 schema）真实可用。**模板的全部价值在「复制即用」——模板自己过不了的规则，教出来就是错的**，所以这三条不靠人评、进 CI。

> `_template` 以 `_` 前缀被发现器跳过（`core/registry.py:71`），因此内部放几个 `.py` 都安全；但**不要**在 `_template` 下再建子包目录，避免与 §2.3 白名单冲突。

### 10.3 使用铁则

新插件**必须**从 `_template` 派生，禁止从零手写 `plugin.py`。

---

## 11. 迁移路径

每阶段可独立验证、可回滚；**每插件一 PR**。

**阶段 0（止血，零业务风险）** — 进度见 ✅ 标记（2026-09-14 落地）

1. ✅ 新增 `core/plugin_support.py`（`read_json` / `mtime`，§6.2、§6.5）—— **2026-09-30 该模块因零调用方删除（状态文件读盘随会话重构整体消失）**。同时把三套状态文件读取命名
 （读侧 `_read_state_file`×2 + `_read_state`×1；mtime 侧 `_stat_mtime`×1 + `_state_mtime`×2）收敛为
  各自的**薄包装**（只绑路径 + 降级策略），实现归一处。包装**函数名刻意不统一**：三个插件都有
  `self._state_mtime` 实例属性，`route_guard` 的 `_stat_mtime` 是为避同名遮蔽而起，统一反而制造混淆。
2. ✅ `Config.update_plugin_section()`（§6.3）+ 配套**读侧** `Config.user_plugin_section()`
  → 删 3 处 `_load_user_config`；插件侧 RMW 收口到 `plugin.py` 6 处（`notify_guard` 1 / `route_guard` 2 /
  `ipad_tunnel` 3）与三个 `config_dialog.py` 6 处。读侧单独给一个方法的原因：`ipad_tunnel` 的一次性
  升级迁移需要「用户显式改过这个键没有」这个信息，它只存在于**用户差异层**，合并后的 `Config.get()` 给不出来。
3. ✅ `Plugin` 基类上提 `publish_snapshot()` / `status_topic()` / `touch_trigger()`（§6.2）。
  删 4 处 `_publish_state`、4 处 `_status_topic`、3 处 `_touch_trigger`。
  **连带修正（重点）**：保留键统一为 `"main"`。原先存在**同一件事两种拼法**——`StateSpec.topic` 是
  `key or 'default'`，而框架自动发布（`scheduler._publish_status`）对「tick 返回单份快照」硬编码发 `"main"`。
  若 `status_topic()` 缺省取 `'default'`，手动即时广播与周期广播会落到**两条不同 topic**，订阅方只看得见一条。
  全仓 5 条真实 `StateSpec` 全部显式写 `key="main"`，`'default'` 是从未触发的失活分支，故兜底值统一为 `"main"`；
  `scheduler` 也改为调 `Plugin.status_topic()`，不再自己拼串。发布门禁随之扩容：扫描面加上基类文件（否则
  `plugins/` 下零命中＝门禁退化成恒真），新增 `test_publish_gate_is_not_vacuous` 把「扫描面确实覆盖到发布点」也变成不变量。
4. ✅ `capabilities/helper_client.py` 上提（**归属已改定案**，见 §8.1）。**分两步落地，两步均已完成**：
  ① 上提 = 模块搬家 + 导入改向：消除 2 处跨插件 import、修掉 `shell/app_shell.py` 的越层引用、
  `ipad_tunnel/__init__.py` 恢复纯 docstring；切分点 1（源码路径推导）落地并加两道守卫。
  ② 语义切分 = 切分点 2（`uninstall()` 内嵌 route 清理）**已落地（2026-09-17）**：
  框架客户端不再认识任何插件的期望态文件名，期望态撤销改由「插件 `teardown()` 写 ＋ 框架
  延迟收尾等消费方收敛」承担（§8.1 末的 ✅ 段；落点表 §A23）。
  > 仍待**真机冒烟**（AI 做不了）：装一次 / 卸一次 helper，确认 A 类二进制部署正常、
  > 且带静态路由的插件卸载后路由**真撤净**（残留现象与判定见《真机验证清单》§3.4）。
5. ✅ `Guard` → `AutomationCapability` 改名（§5.2，模块名 + 测试文件名一并改）。实际影响面 **7 个代码文件
  + 6 份文档**——规范 §5.2 的清单**漏了 `example/external-plugin-template/plugin.py`**（那是真实 import，
  漏掉即留下坏引用），另多修了 `capabilities/__init__.py` 里「本层可 import `ui.widgets.Badge`」这处
  既失效又误导的样例（`ui/` 早已改名 `shell/`，且 `AutomationCapability` 现在**零 UI 依赖**）。

> 1–3 是一批（纯新增 + 删除重复），5 已单独落地，4 的两半（上提 ＋ 语义切分）也**都已落地**
>（语义切分 2026-09-17，见 §8.1 末）。`test_no_cross_plugin_import` 已随阶段 1 入库（白名单：无）。
> 仍欠**真机冒烟**（只有用户能做）：装一次 / 卸一次 helper，确认 A 类二进制部署与期望态撤销都正常。

**阶段 1（定稿）** — ✅ **2026-09-14 落地**

1. ✅ `_template` 升级为**双骨架**（§10）：`plugin.py`（动作型，零 PySide6）/ `plugin_daemon.py`（守护型，复制时改名为 `plugin.py`）/ `settings.py`（设置面）。附带清掉 `plugins/` 与 `plugins/route_guard/` 下未被 git 跟踪的 `.DS_Store`。
2. ✅ §7.1 决议 4 的 **fail-fast**：`ContributionRegistry._validate_contributions()` + 行为验证用例。
3. ✅ §12.2 七条门禁**全部入库**，每条自带「植入违规必被抓 + 合规样本不误报」两个自证用例：
  `test_plugin_package_files`（白名单：无——OS 产物按文件名精确容忍）/ `test_plugin_py_has_no_pyside6`（白名单 6 条存量，随阶段 2/3 删）/ `test_no_cross_plugin_import`（白名单：无）/ `test_no_handwritten_boilerplate`（白名单：无）/ `test_custom_page_has_reason`（存量 5 处**已补 `reason`**，无白名单）/ `test_settings_page_is_buildable`（白名单：无，防未来）/ `test_template_covers_forms`。
4. ✅ 随门禁顺带补齐的**存量清偿**（都不是白名单挂账，而是直接改对）：5 处 `SettingsPageSpec` 补 `reason`（`route_guard` / `ipad_tunnel` / `hosts_guard` / `notify_guard` + 模板）；`example/external-plugin-template/plugin.py` 同样补 `reason`，并就地写明它为何**不能**把 UI 拆出去（loader 把它当顶层模块加载，无包上下文 → `from . import settings` 必 ImportError）。

> 测试增量：**836 → 859 passed**（架构门禁 +19，模板冒烟 +4）。pyflakes 零新增告警。
> 阶段 1 只动「包怎么组织」与「门禁」，**零行为变更**；`test_no_cross_plugin_import` 之所以能无白名单落地，是因为阶段 0 第 4 步先消除了那两处跨插件 import（§8.1）。

**阶段 2（试点）**：单插件重排。**试点选 `notify_guard`** —— 理由：**包最小（742 行，6 个插件里唯一 <800 行）**，且已预分层（backend / decider / signals），同时又**恰好覆盖全部待迁移形态**（`AutomationCapability` 子类 + `RUN_PERIODIC` + `tick` + `resident_services` + 状态文件 + `_publish_state` + `_load_user_config` + `config_schema()` + `SettingsPageSpec`）。它是「最小完备样本」。

> 更正第 1 稿：第 1 稿写「`notify_guard` 体量最小 295 行」——295 是它的 `plugin.py` 行数，而 `plugin.py` 最小的是 `recognize`(83)、其次 `autotype`(236)。「体量最小」只有在**包级**口径下才对 `notify_guard` 成立。
>
> **口径已更新（2026-09-14）**：试点**会重建** `notify_guard` 的设置页——按《录屏通知设计.md》§6.4，它从「恢复自动接管」动作页改为**规则编辑器**（自绘 `settings_page().build`，单栏「列表 → 编辑」push 导航），自动化总闸移到页顶部。这恰好印证 §7.1 决议 6：它本来就是集合型配置，**声明式渲染器救不了它**（§7.1 事实 4），所以只能自绘。
>
> 落地清单（P0/P1/P2 三批）已并入 **§7 已落地**（批次 13 的落点表见 §A2 / §A3）；契约侧改动见《组件契约决议》**D28–D32**。

**阶段 3（扩散）**：`route_guard` → `hosts_guard` → `ipad_tunnel` → `autotype` → `recognize`。

**文档动作（独立原子步，不与代码 PR 混）**：`design.md` / `hev-tunnel.md` **原地不动**；`resources/` **原地不动**（§3.3、§3.4）。

---

## 12. 门禁

### 12.1 门禁自立四条（沿用仓库既有惯例）

`tests/test_architecture.py` 已建立的门禁自证模式，新门禁**必须**照抄：

1. **用 AST / 文件系统事实判定**，不用子串匹配——注释里的迁移说明（如「不再私持 `QFileSystemWatcher`」）会被子串检查误报。
2. **每条门禁自带两个自证用例**：`test_*_detects_planted_violations(tmp_path)`（种一个违规必须被抓）+ `test_*_passes_on_compliant_sample(tmp_path)`（合规样本必须通过）。
3. **白名单条目必须存活且带 reason**：参照 `test_timer_whitelist_entries_are_live_and_justified`（`:276`）——路径必须存在、`len(reason.strip()) >= 15`、**只减不增**。
4. **跨层共享一律「名级副本 + 门禁守」**（如 `core/installer.KNOWN_ROOT_BINARIES` 之于 `capabilities/helper/rootbin.py`）。

### 12.2 待建门禁

| 门禁 | 断言 | 落地时机 | 预期当前命中 |
|---|---|---|---|
| `test_plugin_package_files` | `plugins/<id>/**`（排除 `__pycache__/`）只允许 `*.py` / `*.md` / `resources/`（§2.3） | 阶段 1 | `route_guard/.DS_Store`。**注意**：另有 `plugins/.DS_Store` 落在扫描根**之外**——门禁须把根上提一层（或单独断言），否则漏一个 |
| `test_plugin_py_has_no_pyside6` | `plugin.py` AST 内无任何 `PySide6` import（含函数内） | 阶段 1：`_template` 直接转正（不挂白名单）；6 个真实插件**白名单挂账**，随阶段 2/3 逐个删 | **7/7** 全中：`recognize:14,15`（5 个名字**全是死 import**）、`_template:18`、`route_guard:30,31`、`notify_guard:23`、`autotype:16`、`hosts_guard:331,372`、`ipad_tunnel:490,501,544,549,670,698,752` |
| `test_no_cross_plugin_import` | 插件内 AST import 目标不得是 `barkit.plugins.<other>`（§8.1） | 阶段 1（先做阶段 0 第 4 步，否则一落地即红） | 2 处（§8.1） |
| `test_no_handwritten_boilerplate` | 插件模块内不得**定义** `_publish_state` / `_touch_trigger` / `_load_user_config` / `_status_topic` | 阶段 1 | `_publish_state`×4、`_touch_trigger`×3、`_load_user_config`×3、`_status_topic`×4 |
| `test_no_config_io_in_ui` | `settings.py` / `*_view.py` / `*dialog*.py` **AST** 内不得出现 `user_path` / `save_user` / `safe_load` / `read_text` / `write_bytes` 等直读直写（§4 禁令 3） | ~~阶段 2 起（白名单挂账）~~ **已入库 2026-09-18，零白名单** | **1 处（`hosts_guard/manage_dialog.py:202`），已当场修掉。** 表里原写「9 处（§6.3）」是 2026-09-14 的预期：被点名的那三个 `config_dialog.py` 已于 2026-09-17 / 09-18 / 09-19 **整文件删除**（`route_guard` / `ipad_tunnel` / `autotype`），扫真文件实测只剩这一处。另：**必须按 AST 判、不能按子串**——`user_path` / `read_text` 这些名字恰恰出现在 docstring 与注释里，子串匹配会把「引用禁令的说明」判红 |
| `test_custom_page_has_reason` | `SettingsPageSpec.build` 非空时 `reason` 非空（§7.1） | 阶段 1 | **5 处**：`route_guard:538`、`ipad_tunnel:665`、`hosts_guard:368`、`notify_guard:277`、`_template:72` |
| `test_settings_page_is_buildable` | 收集期 `SettingsPageSpec` 必须至少有一个可构建来源（`build` 或 `schema`）；配套 §7.1 决议 4 的 `ValueError` | 阶段 1 | **0**（现有 6 处全有 `build`）——这是一条**防未来**门禁，不是存量清偿 |
| `test_template_covers_forms` | `_template` 同时含动作型与守护型骨架（§10.1） | 阶段 1 | 全缺 |

> **阶段 1 落地记录（2026-09-14）**：上表七条全部入库。实际结果与上表「预期当前命中」有**三处出入**，逐条说明——它们都是「实测推翻了预期」，不是漏做：
>
> 1. **`test_plugin_package_files` 不把 `.DS_Store` 当违规**（预期是「命中 `route_guard/.DS_Store`」）。实测确有 `plugins/.DS_Store` 与 `plugins/route_guard/.DS_Store` 两个（都未被 git 跟踪），已删除；但门禁改为**按文件名精确容忍**这一个名字。理由：macOS 会在 Finder 访问目录时**非确定性**地生成/删除它，算违规会让门禁因「Finder 有没有翻过这个目录」而随机变红——那不是能长期挂 CI 的判据。容忍面**只有这一个名字**（`TOLERATED_PACKAGE_FILES`）；`notes.txt` / 随手丢的 `.json` 快照 / `*.pyc.*` 残骸 / 多余子目录照抓，由植入式自证用例守着。扫描根已按上表提醒**上提一层**（`plugins/` 根 + 每个插件包各扫一轮，子目录在根那一轮跳过、交给各自那一轮）。
> 2. **`test_plugin_py_has_no_pyside6` 只覆盖包式内部插件**（`src/barkit/plugins/**/plugin.py`）。`example/external-plugin-template/plugin.py` 同样在 `plugin.py` 里 import PySide6，但**今天做不到改**：`core/loader.py:_import_entry` 用 `spec_from_file_location` 把外部插件的 `plugin.py` 当**顶层独立模块**加载（模块名 `barkit_external_<目录名>_<n>`），它没有包上下文，`from . import settings` 必 ImportError。要覆盖外部插件必须先改 loader 的加载语义——那是独立原子步。这条边界写进了 `tests/test_architecture.py` 的段头注释，防止后来者「顺手扩大扫描面」撞上一个不可能满足的要求。
> 3. **`test_custom_page_has_reason` 的 5 处存量是「改对」而非「挂白名单」**。四处真实插件的设置页本就是「动作入口 + 说明」（§7.1 事实 4），补 `reason` 只是把既有事实写下来，零行为变更；模板那处随 §10.2 重写。
>
> `test_no_cross_plugin_import` 能**无白名单**落地，是因为阶段 0 第 4 步先消掉了仅有的两处（§8.1）。

### 12.3 已被覆盖、勿重造

- **插件不得私持线程 / 时钟 / 事件源**：`test_no_private_timer_in_plugins`（`:233`）+ 白名单校验（`:276`）。
- **插件发布边界**：`test_plugin_publishes_only_own_status_snapshot`（`:586`）——只发自身 topic、只发 `StateReport`。
- **路径按值导入 / 路径常量默认参数**：`test_no_by_value_path_imports`（`:433`）、`test_no_path_constant_default_args`（`:452`）。
- **`app.py` 入口瘦身**（≤80 行有效代码）：`test_app_entry_stays_slim`（`:121`）。

> 第 1 稿 §10 里的 `test_plugin_package_no_docs`（「包内无 `.md`」）**废除**——它与 §3.1 的归属规则冲突，且守的是一块已经被 hatchling 排除干净的地方（§3.2）。

