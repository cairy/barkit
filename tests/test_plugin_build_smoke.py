"""回归：插件「设置页 / 自定义状态行」必须能真实构建（§5.3 迁移守卫）。

背景（2026-09-10 真机启动崩溃）：hosts_guard 的 ``_build_settings_page`` 在迁移
删除 ``_build_panel_row`` 时被越界切掉了方法开头——丢了 ``from PySide6.QtWidgets
import QHBoxLayout, QLabel`` 局部导入，且 ``page, layout = settings_page_shell(...)``
被臆造为 ``page = QWidget()``，导致启动即 ``NameError: name 'QWidget' is not defined``。

该路径此前**零测试覆盖**：单测只断言声明点方法返回的 spec 形状，从不调用
``SettingsPageSpec.build()``，因此崩溃直接漏到真机。本测试在 offscreen 下真实调用
每个设置页与 custom 状态行工厂，把「构建期未定义名 / 逻辑异常」挡在 CI 内。
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from barkit.contribution.specs import (
    ActionSpec,
    ComponentSpec,
    DetailEntrySpec,
    HotkeySpec,
    IconName,
    SettingsPageSpec,
    StateLevel,
    StateOption,
    StateSpec,
    StatusRowSpec,
)
from barkit.core.config import Config
from barkit.core.contributions import gather_specs
from barkit.core.plugin import RUN_PERIODIC, PluginContext

PLUGIN_MODULES = [
    "barkit.plugins.notify_guard.plugin",
    "barkit.plugins.route_guard.plugin",
    "barkit.plugins.hosts_guard.plugin",
    "barkit.plugins.ipad_tunnel.plugin",
    "barkit.plugins.autotype.plugin",
    "barkit.plugins.recognize.plugin",
    "barkit.plugins.audio_guard.plugin",
    "barkit.plugins.bt_guard.plugin",
    "barkit.plugins.todo.plugin",
    "barkit.plugins._template.plugin",
]


def _build_plugin(module_path: str, tmp_path):
    """按插件自身 id 生成最小配置并实例化（与既有插件测试同一构造方式）。"""
    mod = importlib.import_module(module_path)
    pid = mod.Plugin.id
    user = tmp_path / f"{pid}.yaml"
    user.write_text(f"plugins:\n  {pid}:\n    enabled: true\n", encoding="utf-8")
    return mod.Plugin(PluginContext(config=Config(user_path=user), events=None))


def _settings_pages(plugin) -> list:
    pages = plugin.settings_page()
    return [pages] if pages is not None else []


@pytest.mark.parametrize("module_path", PLUGIN_MODULES)
def test_settings_page_builds(module_path, tmp_path, qtbot):
    """设置页必须能真实构建——构建期 NameError 曾导致 app 启动即崩。"""
    plugin = _build_plugin(module_path, tmp_path)
    pages = _settings_pages(plugin)
    assert pages, f"{module_path} 未声明设置页"
    for spec in pages:
        widget = spec.build()
        assert widget is not None
        qtbot.addWidget(widget)


# ──────────────────────────────────────────────
# 设置页里不许有「孤儿控件」（2026-09-22 用户上报）
#
# 症状：音频看护的设置页一打开，页标题那一片糊着一团看不清的东西 —— 状态机段的上方
# 叠着几个控件，看不出是什么。
#
# 根因不在状态机段，在组件自己那一页：``settings_section`` **只造卡、并把 ``card``
# 认作父**，不会把卡挂进页面（入页是调用方那一句 ``layout.addWidget(card)``，其余
# 组件页都写了）。漏掉之后卡**仍然可见、仍然画**，只是停在 Qt 的默认几何
# ``(0,0,100x30)`` 上 —— 正压在页面标题所在的那一片；卡里的每一行也跟着堆在那里。
#
# 这条门禁守的就是这一类：**可见、却在父容器布局之外**的控件。构建期不报错、运行期
# 不崩、日志一声不响，只有人眼看得见 ⇒ 只能靠断言守。
# ──────────────────────────────────────────────


def _widgets_in_layout(lay, out: set) -> set:
    """该层布局树里被收编的控件。

    **必须下钻子布局**：``form_row`` 交出来的就是一层 ``QHBoxLayout``（由调用方
    ``addLayout`` 挂上去），只查 ``QLayout.indexOf`` 收不到那一行的标签与控件本身，
    整行都会被误判成孤儿。
    """
    for i in range(lay.count()):
        item = lay.itemAt(i)
        w = item.widget()
        if w is not None:
            out.add(id(w))
        sub = item.layout()
        if sub is not None:
            _widgets_in_layout(sub, out)
    return out


def orphan_widgets(root) -> list:
    """可见、却没被任何一层父容器布局收编的控件名清单（空 = 合规）。"""
    import shiboken6
    from PySide6.QtWidgets import QAbstractScrollArea, QWidget

    out: list = []
    stack = [root]
    while stack:
        w = stack.pop()
        try:
            if not shiboken6.isValid(w):
                continue        # Qt 已回收的内部件（下拉弹出容器一类）
            # 滚动区自己的内部件（viewport / 滚动条容器）不走布局，别误报
            lay = None if isinstance(w, QAbstractScrollArea) else w.layout()
            taken = _widgets_in_layout(lay, set()) if lay is not None else set()
            kids = list(w.children())
        except RuntimeError:
            continue
        for child in kids:
            if not isinstance(child, QWidget):
                continue
            stack.append(child)
            # **用 isHidden() 而不是 isVisible()**：页面在用例里从未 ``show()``，
            # 拿 isVisible() 判会把每一个子件都跳过 —— 对刚构建的页面恒为绿，等于
            # 什么都没测。isHidden() 只跳过「显式隐藏」的（折叠的卡内容、堆叠页的
            # 非当前页、下拉的弹出列表），那正是该跳过的。
            if lay is None or child.isHidden():
                continue
            if id(child) not in taken:
                out.append(f"{type(child).__name__}(name={child.objectName()!r})")
    return out


@pytest.mark.parametrize("module_path", PLUGIN_MODULES)
def test_settings_page_has_no_orphan_widgets(module_path, tmp_path, qtbot):
    """组件自己那页里，可见控件必须真在布局里 —— 否则它压在页面左上角那一片上。

    2026-09-22 用户上报：音频看护页的标题区糊着一团「看不清是什么」的东西。
    """
    plugin = _build_plugin(module_path, tmp_path)
    for spec in _settings_pages(plugin):
        if spec.build is None:
            continue
        widget = spec.build()
        qtbot.addWidget(widget)
        widget.grab()               # 逼一次布局：孤儿控件的症状正是「布局跑过了它」
        found = orphan_widgets(widget)
        assert found == [], (
            f"{module_path} 的设置页里有控件没进布局：{found}\n"
            "最常见的一种：``settings_section(...)`` 造了卡却漏了 ``layout.addWidget(card)`` "
            "—— 卡会停在 Qt 默认几何 (0,0,100x30) 上压住页标题。"
        )


def test_orphan_widget_gate_selfcheck(qtbot):
    """双自证：喂进**当时那份真实出错的输入**必须抓到；合规的两种写法不误报。

    控制组不是随便造个坏值：下面第一段构造的就是修复前 ``audio_guard`` 那份页面
    （``settings_section`` 之后直接 ``addStretch``，没有 ``addWidget``）。第二段
    是补上那一句之后的同一份输入。
    """
    from barkit.shell.ui_kit import form_row, note, settings_page_shell, settings_section

    broken, broken_lay = settings_page_shell("音频看护")
    card, content = settings_section("通知", parent=broken)
    content.addWidget(note("说明文字", card))
    # ← 这里本该有一句 ``broken_lay.addWidget(card)``：当时就是漏了它
    broken_lay.addStretch(1)
    qtbot.addWidget(broken)

    found = orphan_widgets(broken)
    assert found, "修复前那份输入没被抓到 —— 这条门禁测不到东西"
    assert "Card" in found[0], found
    assert card.geometry().width() <= 100 and card.geometry().top() == 0, (
        f"孤儿卡本该停在 Qt 默认几何，实际 {card.geometry()}"
    )

    fixed, fixed_lay = settings_page_shell("音频看护")
    ok_card, ok_content = settings_section("通知", parent=fixed)
    ok_content.addWidget(note("说明文字", ok_card))
    fixed_lay.addWidget(ok_card)
    fixed_lay.addStretch(1)
    qtbot.addWidget(fixed)
    assert orphan_widgets(fixed) == [], "补上那一句之后仍被判孤儿 —— 判据错了"

    # ② 最容易误伤的一种：``form_row`` 交出来的是一层**子布局**，那一行的标签与
    # 控件并不直接是容器布局的 item。判据若只会 ``indexOf``，每个组件页都会满屏红。
    row_page, row_lay = settings_page_shell("行")
    row_card, row_content = settings_section("组", parent=row_page)
    row_content.addLayout(form_row("一个标签", note("控件", row_card), row_card))
    row_lay.addWidget(row_card)
    qtbot.addWidget(row_page)
    assert orphan_widgets(row_page) == [], "子布局里的控件被误判成孤儿"


# ──────────────────────────────────────────────
# 「重画一整组控件」之后不许留下漂浮控件（2026-09-27 用户上报）
#
# 症状：翻页时钟设置页里「触发场景」那一行与它下面的说明文字**重叠**成一团。
#
# 根因：那个页面按类型重建动态字段时，只删了 ``takeAt`` 交出来的 ``widget()``，而
# ``form_row`` 交出来的是一层 ``QHBoxLayout``（标签与控件挂在它**里面**）⇒ 子布局项
# 的 ``widget()`` 是 ``None``，那些控件一个都没被删掉，停在**上一轮的几何**上原地可见。
# 实测那份页面切两次「上班时间来源」就留下 **22 枚**漂浮控件、**39 处**两两重叠。
#
# 与上面那条门禁同一判定（可见却不在任何布局里），差别是这里要走**运行期重建**那条路
# —— 只构建一次的页面看不见这个病。修法是 ``ui_kit.clear_layout``（连子布局一起摘）。
#
# ◷ 2026-10-08 合并方案批 5：旧翻页钟组件退场，载体换成**待办提醒详情页的编辑屏**
# —— 它每次进出都整体重画（``detail_view._rebuild_dyn``，一条条目十来个键全列出来），
# 是同一形态在本仓的现役落点。**判据不跟着组件走，只跟着形态走**：载体换了，守的那
# 件事一个字没变。
# ──────────────────────────────────────────────


def test_rebuilding_the_entry_form_leaves_no_orphan_widgets(tmp_path, qtbot):
    """待办提醒的编辑屏重建过后，页面上不许有漂浮控件。

    走的是用户路径：进编辑屏（重画一整组字段）→ 返回列表 → 再进一次（重画第二遍）
    —— 清理时若只摘了 ``item.widget()``，第二遍那组就会与第一遍的叠在一起。
    """
    from barkit.plugins.todo.detail_view import build_detail_page

    plugin = _build_plugin("barkit.plugins.todo.plugin", tmp_path)
    assert plugin.save_items(
        [{"id": "td1", "enabled": True, "title": "喝水", "at": "09:30"}]
    ) is True
    page = build_detail_page(plugin)
    qtbot.addWidget(page)
    page.show()

    page._open_edit(0)      # 第一次重画
    page._on_back()
    page._open_edit(0)      # 第二次重画：漏清子布局的话，这里就叠上了
    page.grab()             # 逼一次布局（同上面那条门禁）

    found = orphan_widgets(page)
    assert found == [], (
        "重画编辑屏之后页面上留下了漂浮控件（它们停在上一轮的几何上，与新建的那一组"
        f"重叠）：{found}\n"
        "最常见的一种：清理布局时只删了 ``item.widget()``，而 ``form_row`` 交出来的"
        "是一层 ``QHBoxLayout`` —— 用 ``ui_kit.clear_layout`` 才摘得干净。"
    )


@pytest.mark.parametrize("module_path", PLUGIN_MODULES)
def test_detail_page_builds(module_path, tmp_path, qtbot):
    """声明 DetailEntrySpec 的详情页必须能真实构建（§5.4，同设置页的守卫）。

    详情页是启动后点状态行才走到的路径，若不构建验证，崩溃会一直藏到真机点击。
    """
    plugin = _build_plugin(module_path, tmp_path)
    for spec in [s for s in [plugin.detail_entry()] if s is not None]:
        if spec.build is not None:
            widget = spec.build()
            assert widget is not None
            qtbot.addWidget(widget)


@pytest.mark.parametrize("module_path", PLUGIN_MODULES)
def test_custom_status_row_builds(module_path, tmp_path, qtbot):
    """声明 custom 工厂的状态行必须能构建（autotype 保留的逃生舱）。"""
    plugin = _build_plugin(module_path, tmp_path)
    for spec in plugin.row_specs():
        if spec.custom is not None:
            widget = spec.custom()
            assert widget is not None
            qtbot.addWidget(widget)


# ──────────────────────────────────────────────
# 模板双骨架冒烟（《插件结构规范》§10.1 / §12.2 的运行时侧）
#
# 上面那三条参数化用例只覆盖 `_template/plugin.py`（动作型）。守护型骨架
# `plugin_daemon.py` 不是被发现器加载的入口模块，故单列在这里。
#
# 模板的全部价值在「复制即用」，所以这里验两件事：
#   ① 两个骨架的贡献能过框架的收集期门禁（含 §7.1 决议 4 的 fail-fast）——
#      模板自己过不了的规则，教出来就是错的；
#   ② 守护型骨架的声明面（长驻服务 / 文件事件 / 资源 / 自动化能力 / 配置 schema）
#      真实可用，而不是一堆只有形状的占位。
# ──────────────────────────────────────────────

TEMPLATE_ACTION = "barkit.plugins._template.plugin"
TEMPLATE_DAEMON = "barkit.plugins._template.plugin_daemon"

# 显示位门禁的扫描面：**全部内建组件** ＋ 守护型模板（交付给外部作者的那份骨架，
# 模板自己过不了的规则教出来就是错的）。
#
# 2026-09-22 修订：原来这里是「上面那份 ＋ recognize」，理由是「recognize 没有设置页，
# 进不了逐个构建那条参数化用例」——**那句早已过期**（recognize 于 2026-09-19 转正补上了
# 设置页），于是它的设置页一直没进冒烟。现在它并入上面那份，本列表只多守护型模板。
SLOT_MODULES = [*PLUGIN_MODULES, TEMPLATE_DAEMON]


@pytest.mark.parametrize("module_path", [TEMPLATE_ACTION, TEMPLATE_DAEMON])
def test_template_skeleton_passes_contribution_gate(module_path, tmp_path):
    """骨架的贡献必须过框架收集期门禁（含 SettingsPageSpec fail-fast）。"""
    from barkit.core.contributions import ContributionRegistry

    plugin = _build_plugin(module_path, tmp_path)
    reg = ContributionRegistry([plugin])
    assert reg.rejected == [], reg.rejected
    assert reg.failed == [], reg.failed
    assert reg.status_rows(), f"{module_path} 应贡献状态行"


def test_template_action_skeleton_settings_page_is_tested_above(tmp_path):
    """动作型骨架的设置页经 `settings.build_page` 构建（见上方参数化用例）。

    这条只钉住「设置面确实归了 ``settings.py``」——否则 ``plugin.py`` 里就会出现
    PySide6 import，架构门禁 ``test_plugin_py_has_no_pyside6`` 会红。
    """
    plugin = _build_plugin(TEMPLATE_ACTION, tmp_path)
    pages = _settings_pages(plugin)
    assert len(pages) == 1, pages
    assert pages[0].reason.strip(), "设置了 build 就必须写 reason（§7.1 决议 2）"
    assert pages[0].build is not None


def test_template_daemon_skeleton_surface(tmp_path):
    """守护型骨架的声明面必须真实可用（复制改名后即是一个能跑的 daemon 插件）。"""
    from barkit.core.resident import ResidentService

    plugin = _build_plugin(TEMPLATE_DAEMON, tmp_path)

    # 长驻服务：廉价、可反复调用，且每次返回**同一批对象**（宿主按 name 差量收敛）
    services = plugin.resident_services()
    assert len(services) == 1, services
    assert plugin.resident_services()[0] is services[0], "每次必须返回同一批对象"
    assert isinstance(services[0], ResidentService)
    assert services[0].name == "demo_poller"
    services[0].stop()          # stop 幂等、非阻塞

    # tick：还没收到过守护的事实 → 走「等待守护」降级，不抛；报的取值必须在声明里列举过
    snap = plugin.tick()
    assert snap.state == "waiting", snap
    state_spec = plugin.state_specs()[0]
    assert snap.state in {opt.id for opt in state_spec.states}, "报告了没声明的取值"
    assert snap.updated_at > 0

    # 观测订阅：声明**领域名**（不是文件路径）；handler 只标记脏（ctx 未接线时静默空转）
    declared = plugin.subjects()
    assert declared and all(isinstance(s, str) and s for s in declared), declared
    assert not any("/" in s for s in declared), f"领域名里不该出现路径：{declared}"
    plugin.on_observed(declared[0], {"note": "ok"})

    # 资源声明：data_files 相对用户域（不得含绝对路径）、seed ⊆ data。
    # **默认零文件**（2026-09-29 通道换成会话之后，骨架不再落任何文件）。
    res = plugin.resources()
    assert res.data_files == [], res.data_files
    assert all(not Path(f).is_absolute() for f in res.data_files), res.data_files
    assert set(res.seed_files) <= set(res.data_files), res
    assert res.config_section == plugin.id

    # 自动化能力：徽标由 AutomationCapability 统一提供，开关经 RMW 写用户配置段
    assert plugin.mode_badge_spec().label in {"自动", "手动"}
    assert plugin.automation_active is True
    plugin.set_automation_active(False)
    assert plugin.automation_active is False
    assert plugin.plugin_config().get("auto_enabled") is False, "开关未写回配置段"

    # 配置 schema：默认值单一真源（§9.5）。**没有 poll** —— 框架节拍的用户入口
    # 由框架自动生成（D21），组件只声明 suggested_poll（下面那条门禁守）。
    schema = plugin.config_schema()
    assert {f.key for f in schema.fields} == {"auto_enabled"}, schema.fields
    assert schema.defaults() == {"auto_enabled": True}, schema.defaults()
    assert type(plugin).suggested_poll == 5, "声明还在（框架据此画控件）"


@pytest.mark.parametrize("module_path", PLUGIN_MODULES)
def test_every_builtin_state_machine_enumerates_its_values(module_path, tmp_path):
    """决议 D13：每台状态机的取值必须列举完整，且首屏起点落在其中。

    「取值闭集」不是装饰 —— 级别 / 取值图标 / 默认文案都从它取，列不全就只有
    「未声明取值」一条路（界面按 err 露出）。重名与「initial 拼错」由
    ``StateSpec.__post_init__`` 在声明期拦下，本门禁守的是「到底有没有声明」这一层。

    动作型组件（如 ``autotype``）今天可以一台状态机都没有，故这里跳过而非判失败。
    """
    plugin = _build_plugin(module_path, tmp_path)
    specs = list(plugin.state_specs())
    for spec in specs:
        ids = {opt.id for opt in spec.states}
        assert ids, f"{module_path} 的 {spec.key} 没有列举任何取值"
        assert spec.initial_value in ids, (
            f"{module_path} 的 {spec.key} 首屏起点 {spec.initial_value!r} 不在取值 {sorted(ids)} 里"
        )


# ──────────────────────────────────────────────
# 身份唯一真源（决议 D14 / D16）的两条门禁
#
# 「同一个字符串写三遍」的危害不是多打几个字，而是**改名会漏**：组件改名后那几处
# 各留一个旧名，界面上就会出现「标题栏叫 A、面板行叫 B」这种对不上的状态。
#
# 名字位分两类，**都查**（2026-09-20 扩到入口类，起因是 autotype 改名时设置页那
# 一项仍叫旧名「文字输入」）：
#   - 身份类：``StatusRowSpec.title`` / ``StateSpec.title`` / ``ComponentSpec.name``
#   - 入口类：``SettingsPageSpec.label`` / ``ActionSpec.label`` / ``HotkeySpec.label``
#     / ``DetailEntrySpec.label`` —— 只在**该声明点只有一条贡献**时查：多条时它们
#     各有其事（多页 / 多按钮 / 多热键 / 多详情入口），其中一条与组件名同字是合理的；
#     单条时省略即由框架补组件名，写了就是抄一遍。
# 反过来，入口文案**不等于**组件名是合法的覆盖（显式写的另一个名字，不是第二份真源）。
# 两条门禁合起来才闭合：① 声明里不重复写名字；② 收集后名字**不为空**（空行名）。
# ──────────────────────────────────────────────


def identity_name_duplication(name: str, specs) -> list[str]:
    """返回「与组件名 ``name`` 字面相同」的名字位清单（空 = 合规）。

    两类名字位都查，判据不同：

    - **身份类**（``StatusRowSpec.title`` / ``StateSpec.title`` /
      ``ComponentSpec.name``）：无条件。同组件多台状态机时各台机的状态名本就与
      组件名不同（填它才有信息量），故「等于组件名」即违规这条对状态名仍成立。
    - **入口类**（``SettingsPageSpec.label`` / ``ActionSpec.label`` /
      ``HotkeySpec.label``）：只在该声明点**只有一条贡献**时查 —— 与框架侧
      ``_fill_identity_defaults`` 的补值条件同口径（单条省略即补，多条各自命名）。

    这里只抓「把组件名又抄了一遍」；**写一个别的名字是合法的覆盖**，不抓。
    """
    from barkit.contribution.specs import (
        ActionSpec,
        ComponentSpec,
        HotkeySpec,
        SettingsPageSpec,
        StatusRowSpec,
    )

    per_point: dict[type, int] = {}
    for s in specs:
        per_point[type(s)] = per_point.get(type(s), 0) + 1

    offenders: list[str] = []
    for spec in specs:
        if isinstance(spec, ComponentSpec):
            field = "name"
        elif isinstance(spec, (StatusRowSpec, StateSpec)):
            field = "title"
        elif isinstance(spec, (SettingsPageSpec, ActionSpec, HotkeySpec, DetailEntrySpec)):
            if per_point[type(spec)] != 1:
                continue          # 多条：各自命名，与组件名同字不算重复
            field = "label"
        else:
            continue
        value = getattr(spec, field, "")
        if value and value == name:
            offenders.append(f"{type(spec).__name__}.{field}={value!r}")
    return offenders


def identity_name_gaps(registry, plugin_name: str) -> list[str]:
    """收集后**没被补上组件名**的名字位清单（空 = 合规）。

    口径（决议 D14）：行名与组件名都该等于 ``Plugin.name``（组件省略了它，框架补上）；
    状态名只要求**非空** —— 多台状态机的组件各自命名，本来就不等于组件名。
    """
    gaps: list[str] = []
    for row in registry.status_rows():
        if row.title != plugin_name:
            gaps.append(f"StatusRowSpec.title={row.title!r}")
    for comp in registry.components():
        if comp.name != plugin_name:
            gaps.append(f"ComponentSpec.name={comp.name!r}")
    for state in registry.states():
        if not state.title:
            gaps.append(f"StateSpec({state.key or 'main'}).title 为空")
    return gaps


@pytest.mark.parametrize("module_path", PLUGIN_MODULES)
def test_identity_name_is_not_written_twice(module_path, tmp_path):
    """决议 D14：与 ``Plugin.name`` 相同的名字不许在声明里再写一遍（真实组件上查）。"""
    plugin = _build_plugin(module_path, tmp_path)
    plugin_name = type(plugin).name
    offenders = identity_name_duplication(plugin_name, gather_specs(plugin, plugin.id))
    assert offenders == [], (
        f"{module_path} 把组件名 {plugin_name!r} 又写了一遍：{offenders} —— "
        "省略它，框架会取 Plugin.name"
    )


@pytest.mark.parametrize("module_path", PLUGIN_MODULES)
def test_collected_identity_names_are_never_empty(module_path, tmp_path):
    """决议 D14 的**落地前提**：框架侧补默认值必须真的补上了，否则面板渲染空行名。

    与上一条合起来才闭合：组件不再写名字（上一条），框架就必须把 ``Plugin.name``
    补进三个名字位 —— 漏了这一步，面板上就是一行「没有名字」的条目。
    """
    from barkit.core.contributions import ContributionRegistry

    plugin = _build_plugin(module_path, tmp_path)
    registry = ContributionRegistry([plugin])
    assert registry.failed == [], registry.failed
    gaps = identity_name_gaps(registry, type(plugin).name)
    assert gaps == [], f"{module_path} 收集后名字位仍是空的：{gaps}"


def test_identity_duplication_gate_detects_planted_violation(tmp_path):
    """门禁自证（两半）：合规样本零命中，把组件名写回任一名字位必抓。

    判定函数只看「名字位是否等于组件名」，与插件从哪来无关，故这里用最小桩插件验证
    判定本身；**扫描面**另有真实组件那一半（上面那两条参数化用例，逐个跑过 6 个内建
    组件）。两半都在《验证与自证》的口径内：判定对了、面也覆盖到了。
    """
    clean = [
        StatusRowSpec(plugin_id="stub", key="main"),
        ComponentSpec(plugin_id="stub", icon=IconName.GEAR),
        StateSpec(
            plugin_id="stub", key="a", title="第一台",
            states=(StateOption("on", StateLevel.OK),),
        ),
        StateSpec(
            plugin_id="stub", key="b", title="第二台",
            states=(StateOption("on", StateLevel.OK),),
        ),
        # 入口类：单条但**写了别的名字**（合法覆盖）⇒ 不该被当成重复写
        SettingsPageSpec(plugin_id="stub", key="p", label="设置", build=lambda: None),
        ActionSpec(plugin_id="stub", key="a", label="动作"),
        HotkeySpec(plugin_id="stub", key="h", name="h", label="热键"),
        DetailEntrySpec(plugin_id="stub", key="d", label="详情", build=lambda: None),
    ]
    assert identity_name_duplication("示例组件", clean) == [], "合规写法被误报"

    for planted in (
        StatusRowSpec(plugin_id="stub", key="main", title="示例组件"),
        ComponentSpec(plugin_id="stub", icon=IconName.GEAR, name="示例组件"),
        StateSpec(plugin_id="stub", key="main", title="示例组件"),
        SettingsPageSpec(plugin_id="stub", key="p", label="示例组件", build=lambda: None),
        ActionSpec(plugin_id="stub", key="a", label="示例组件"),
        HotkeySpec(plugin_id="stub", key="h", name="h", label="示例组件"),
        DetailEntrySpec(plugin_id="stub", key="d", label="示例组件", build=lambda: None),
    ):
        assert identity_name_duplication("示例组件", [planted]), (
            f"植入的重复名字没被抓到：{planted!r}"
        )

    # 入口类的**多条**豁免：两条设置页里有一条与组件名同字，不算重复写
    two_pages = [
        SettingsPageSpec(plugin_id="stub", key="p1", label="示例组件", build=lambda: None),
        SettingsPageSpec(plugin_id="stub", key="p2", label="另一个", build=lambda: None),
    ]
    assert identity_name_duplication("示例组件", two_pages) == [], (
        "多条入口里有一条与组件名同字被误报 —— 多条时它们各有其事"
    )


def test_collected_identity_name_gate_detects_bypassed_fill(tmp_path, monkeypatch):
    """门禁自证（另一半）：把「收集期补默认值」旁路掉，门禁必须红。

    模拟的正是这条路走错的样子：组件侧已经删掉名字（决议 D14 的组件那一半做完了）、
    框架侧却没补（另一半漏了）—— 那时行名会是空的，界面上就是一行没有名字的条目。
    门禁若只会绿，就是一条什么都没测到的装饰。
    """
    from barkit.core.contributions import ContributionRegistry

    plugin = _build_plugin(TEMPLATE_ACTION, tmp_path)
    monkeypatch.setattr(
        ContributionRegistry, "_fill_identity_defaults", staticmethod(lambda *a, **k: None)
    )
    registry = ContributionRegistry([plugin])
    gaps = identity_name_gaps(registry, type(plugin).name)
    assert gaps, "旁路掉补默认值后门禁仍全绿 —— 这条门禁测不到东西"


# ──────────────────────────────────────────────
# 框架节拍的用户入口归框架（决议 D21）
#
# 真源：《主框架架构-内核层》§8.1「``poll`` 不是双真源」——
#   · ``Plugin.suggested_poll``（类属性）= 组件声明，**唯一真源**；
#   · ``plugins.<id>.poll`` 的用户入口 = 框架画在**该组件设置页页尾**「运行间隔」卡里
#     那个下拉（2026-09-21 第二批起从「插件」管理页下沉到组件自己的页），
#     由框架按 ``run_mode`` ＋ ``suggested_poll`` **自动生成**（2026-09-17 起）。
#     组件**不再**声明 ``FieldSpec("poll")``：同名的第二个覆盖通道，界面上只看得到
#     框架那一个，而 schema 的 ``default`` 会经默认值合成盖住类属性。
#
# 为什么这条要跑**真实组件**而不是 AST：判据是「``config_schema()`` 产出里有没有
# 这个键」，只有构造出来才看得见（AST 只能看见源码里有没有那个字面量）。
# ──────────────────────────────────────────────


def _framework_poll_fields(plugin) -> list[str]:
    """该组件 schema 里声明了框架节拍键的字段名（正常恒为空）。"""
    return [f.key for f in plugin.config_schema().fields if f.key == "poll"]


@pytest.mark.parametrize("module_path", [*PLUGIN_MODULES, TEMPLATE_DAEMON])
def test_no_plugin_declares_framework_poll_field(module_path, tmp_path):
    """组件**不许**声明 ``poll`` 配置字段：那个键的用户入口归框架。

    组件承认的两种写法：① 周期型只声明 ``suggested_poll``，控件由框架画；
    ② 组件自己的后台节律另起键名（``resolve_poll`` / ``helper_poll``，决议 D27）——
    那是**另一个消费者**，不在此列。
    """
    plugin = _build_plugin(module_path, tmp_path)
    keys = [f.key for f in plugin.config_schema().fields]
    assert _framework_poll_fields(plugin) == [], (
        f"{module_path} 的 config_schema() 声明了 poll 字段——框架节拍的用户入口由框架"
        f"自动生成（D21），组件只声明 suggested_poll；组件自己的节律请另起键名（D27）。"
        f"当前字段：{keys}"
    )


def test_poll_field_gate_selfcheck():
    """双自证：植入违规必抓 ＋ 名字相近的自有键不误报。

    ② 是这条门禁最容易误伤的地方 —— ``resolve_poll`` / ``helper_poll`` 是组件自己的
    业务节律键（D27），**必须**放行；只认精确的 ``poll``。
    """
    from barkit.core.schema import FieldSpec, FormSchema

    class _Planted:
        def config_schema(self):
            return FormSchema(
                plugin_id="planted",
                fields=(FieldSpec(key="poll", label="轮询间隔（秒）"),),
            )

    class _OwnRhythmKeys:
        def config_schema(self):
            return FormSchema(
                plugin_id="own",
                fields=(
                    FieldSpec(key="resolve_poll", label="自动更新间隔（秒）"),
                    FieldSpec(key="helper_poll", label="helper 节律（秒）"),
                ),
            )

    assert _framework_poll_fields(_Planted()) == ["poll"], "植入违规却抓不到 —— 门禁测不到东西"
    assert _framework_poll_fields(_OwnRhythmKeys()) == [], "组件自己的节律键被误伤"


@pytest.mark.parametrize("module_path", [*PLUGIN_MODULES, TEMPLATE_DAEMON])
def test_periodic_plugin_declares_usable_suggested_poll(module_path, tmp_path):
    """周期组件的 ``suggested_poll`` 必须可用（正整数）：框架拿它当节拍默认值。

    与静态门禁（``test_architecture.test_periodic_plugin_declares_suggested_poll``）
    互补：那条查「有没有写」，这条查「写出来的值真能当节拍用」——实例化后读出来的
    是同一个类属性，零值 / 布尔值 / 字符串在这里当场现形。
    """
    plugin = _build_plugin(module_path, tmp_path)
    poll = type(plugin).suggested_poll
    if type(plugin).run_mode != RUN_PERIODIC:
        return                      # 按需触发的组件不进调度器（填了也不生效，D21）
    assert isinstance(poll, int) and not isinstance(poll, bool) and poll > 0, (
        f"{module_path} 是周期组件，suggested_poll={poll!r} 不能当节拍用"
    )


# ──────────────────────────────────────────────
# 显示位声明一致性（决议 D16 的落地前提，2026-09-17 用户裁定）
#
# 「显示位＝声明了什么就出现在哪」这条规则有个前提：**那处的画材组件真能提供**。
# 面板四个位里，两处的画材来自状态机：
#
# - 标题栏那格画的是**代表态的取值图标 ＋ 级别色**。没有状态机就没有代表态，
#   声明了只会永远画同一枚身份图标、永远中性灰、悬停只说组件名 —— 说不出任何事，
#   白占标题栏一格（可见上限 7）。**用户裁定：无状态机的组件不声明标题栏。**
# - 默认渲染的列表行画的是**代表态级别**（角标 ＋ 颜色）＋ 那句副标题。没有状态机时
#   `_recompute_plugin` 走 early-return 分支，把副标题**清空**（连行级 `subtitle`
#   回调求出来的那句也一起清）—— 声明出去就是一行永远空着的副标题。
#
# 自绘的行（``custom``）两处都不在此列：它自己画，框架不往上叠级别与文案。
#
# **行本身不再是准入**（2026-09-22 契约改动）：每条组件都有一行 —— 没声明
# ``row_specs()`` 的由框架合成朴素行（``ContributionRegistry.status_rows``），
# 那行的主位图标有 ``IconName.GEAR`` 兜底（面板 ``_paint_identity_icon``），故
# 「没身份图标 ⇒ 空图标位」这条口子已随之一并关闭，不必再守。
# ──────────────────────────────────────────────


def display_slot_declaration_gaps(specs) -> list:
    """声明了「画材来自状态机」的显示位、却没有那份画材 → 问题清单。

    只认**默认渲染**的行（``custom is None``）；自绘行自己画，不受这条约束。
    """
    from barkit.contribution.specs import ComponentSpec, StatusRowSpec

    has_state = any(isinstance(s, StateSpec) for s in specs)
    comps = [s for s in specs if isinstance(s, ComponentSpec)]
    default_rows = [s for s in specs if isinstance(s, StatusRowSpec) and s.custom is None]

    gaps: list[str] = []
    if not has_state:
        # 只看**真的会出现在标题栏**的那些（``in_header`` 关掉的那一格根本不画，
        # 自然不该因为「画不出代表态」被判违规 —— 它压根没声明要画）。
        for comp in comps:
            if comp.in_header:
                gaps.append(
                    "ComponentSpec（标题栏）：没有状态机 ⇒ 那格画不出代表态取值图标，"
                    "只会永远是一枚中性色的身份图标"
                )
        for row in default_rows:
            # 无状态机时框架走 early-return，把副标题**清空**（连行级 subtitle 回调
            # 求出来的那句也一起清）。故只有「声明了 subtitle ⇒ 期待副标题有内容」
            # 的行才算「声明了却画不出」；不声明副标题的行（图标 ＋ 名称 ＋ 行内控件
            # 就是它的全部）是框架明确支持的一条路径，不算缺口。
            if row.subtitle is not None:
                gaps.append(
                    f"StatusRowSpec({row.key or 'main'})：声明了 subtitle 但没有状态机 ⇒ "
                    "框架的「无状态机」分支会把副标题清空"
                )
    return gaps


@pytest.mark.parametrize("module_path", SLOT_MODULES)
def test_display_slot_declarations_need_a_state_machine(module_path, tmp_path):
    """显示位声明必须与「组件真能提供那份画材」一致（决议 D16）。

    抓两类「声明了却画不出」：① 无状态机却声明标题栏；② 无状态机却声明了副标题的
    **默认渲染**行（框架的「无状态机」分支会把副标题清空）。自绘行（``custom``）豁免。
    """
    plugin = _build_plugin(module_path, tmp_path)
    gaps = display_slot_declaration_gaps(gather_specs(plugin, plugin.id))
    assert gaps == [], (
        f"{module_path} 的显示位声明与组件能力不一致：\n  - " + "\n  - ".join(gaps)
        + "\n（要整行自绘就别让框架叠级别/文案：用 StatusRowSpec.custom）"
    )


def test_display_slot_declaration_gate_selfcheck():
    """双自证：两类违规各植入一次必抓，两种合规写法不误报。

    ② 是本条门禁最容易误伤的地方：**自绘的行**（``custom``）不需要状态机 ——
    `autotype` 正是这个形态（自绘行 ＋ 无状态机 ＋ 无标题栏），必须放行。
    """
    from barkit.contribution.specs import StatusRowSpec

    state = StateSpec(
        plugin_id="stub", key="main", title="第一台",
        states=(StateOption("on", StateLevel.OK),),
    )
    identity = ComponentSpec(plugin_id="stub", icon=IconName.GEAR)

    # 合规：有状态机 ＋ 有身份 ＋ 默认行（绝大多数组件的写法）
    assert display_slot_declaration_gaps(
        [identity, StatusRowSpec(plugin_id="stub", key="main"), state]
    ) == [], "合规写法被误报"

    # 合规：自绘行 ＋ 无状态机 ＋ 无身份（autotype 形态）——豁免要生效
    assert display_slot_declaration_gaps(
        [StatusRowSpec(plugin_id="stub", key="main", custom=lambda *_: None, reason="自定义")]
    ) == [], "自绘行被误伤"

    # 合规（2026-09-22 新形态）：无状态机 ＋ 无身份 ＋ 默认行，且**不声明副标题** ——
    # 这正是框架为 recognize 这类按需组件合成的**朴素行**（图标 ＋ 名称而已），
    # 主位图标由面板的 `IconName.GEAR` 兜底，不算「声明了却画不出」。
    assert display_slot_declaration_gaps(
        [StatusRowSpec(plugin_id="stub", key="main")]
    ) == [], "朴素行（无状态机、无身份、无副标题）被误伤"

    # 违规①：无状态机却声明标题栏
    assert display_slot_declaration_gaps([identity]), "植入「无状态机的标题栏」却抓不到"
    # 违规②：无状态机却声明了副标题的默认行（那句一定会被框架清空）
    planted = display_slot_declaration_gaps(
        [StatusRowSpec(plugin_id="stub", key="main", subtitle=lambda: "一句")]
    )
    assert planted and "副标题" in planted[0], "植入「无状态机的副标题」却抓不到"


