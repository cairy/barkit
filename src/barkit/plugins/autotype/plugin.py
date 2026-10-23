"""autotype：一键输入（方案 §4.4，P2a）。

- run_mode = on_demand：按需触发，无常驻轮询
- 触发：热键 ⌃⌥P（autotype_last：输入“上次使用的项”，首次弹列表选择）
        + 托盘子菜单直选 + 面板按钮
- 流程：辅助功能权限检查 → TIS 输入法检测/切 ABC → 倒计时浮层（可取消）→ 逐字符注入
- 引擎（CGEvent 直注 + TIS）见 engine.py，均为库模块，无 Qt 依赖
- “上次使用”**只活在内存里**（2026-09-29）：从前它落一份 11 字节的
  ``autotype_last.txt``，丢了只是「重启后第一次按热键多选一次」，而那条回落路径本来就有
- 输入项与全局默认（`interval` / `enter_delay` / `auto_switch_ime`）在**设置页**改
  （`settings.py`，2026-09-19 由 `config_dialog.py` 收归）；本模块只管配置 IO 与运行期编排
- **UI 都不在本模块**（门禁 `test_plugin_py_has_no_pyside6`）：设置面 `settings.py`、
  运行期浮层 `countdown_view.py`；**面板行是声明式**（`row_specs` 返回
  `StatusRowSpec` ＋ 行内 `ChoiceSpec` 下拉，由框架渲染），不再有面板行的 UI 模块
  （原 `panel_view.py` 于 2026-09-20 随「行内下拉控件收归框架」删除）。本模块只
  向 UI 模块**注入数据**
- **本模块零 Qt**（2026-09-28）：此前那 4 处自弹框（`QMessageBox` ×3 ＋ `QInputDialog` ×1）
  已全部改成 `ActionResult` 回执，本文件随之从 `PLUGIN_PYSIDE6_WHITELIST` 迁出，
  白名单**清零**。落点见 `type_profile` 的 docstring。
"""

from __future__ import annotations

import logging

from barkit.contribution.specs import (
    ActionOutcome,
    ActionResult,
    ActionSpec,
    ChoiceSpec,
    ComponentSpec,
    HotkeySpec,
    IconName,
    SettingsPageSpec,
    StatusRowSpec,
)
from barkit.core.permissions import accessibility_trusted, ensure_accessibility
from barkit.core.plugin import RUN_ON_DEMAND, Plugin
from barkit.core.schema import FieldSpec, FieldType, FormSchema
from barkit.plugins.autotype import engine
from barkit.plugins.autotype import settings as settings_ui
from barkit.plugins.autotype.countdown_view import AutotypeOverlay
from barkit.shell.overlay import DISMISS_CLOSED, overlay_alive

log = logging.getLogger(__name__)


class Plugin(Plugin):
    id = "autotype"
    name = "一键输入"
    run_mode = RUN_ON_DEMAND
    required_permissions = ["accessibility"]
    order = 30
    api_version = 3               # 契约版本：框架按精确匹配校验（声明点方法路径）

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._overlay: AutotypeOverlay | None = None
        # 面板行下拉「当前选中哪一项」（运行期状态，不落盘）：初值回落「上次使用」，
        # 用户改选后由 ``ChoiceSpec.on_change`` 更新。**切换只改这里，不触发输入**。
        self._choice: str | None = None
        # 「上次使用」同样只在内存里（2026-09-29 删掉了那份 11 字节的
        # ``autotype_last.txt``）：按「丢了会怎样」两问它都答不上 —— 丢了只是重启后第一次
        # 多选一次，而这条回落路径本来就有。它当年还**没进 ``resources()``**（写了文件却
        # 不入账 ⇒ 卸载不删、导出不带），留着比删掉更说不清。
        self._last: str | None = None

    # ---------- 可插拔协议（§9.1，批次 6c） ----------

    def export_data(self, include_secret: bool = False) -> dict:
        """导出用户数据；profile 的明文 ``text`` 默认置空（待决 7：从源头降低暴露面）。

        勾选包含时原样导出（导出包落盘走框架 0600/600 惯例）。§9.5 config_schema()
        接线后，SECRET 判定改由 FieldType.SECRET 驱动。
        """
        data = super().export_data()
        if not include_secret:
            profiles = data.get("profiles")
            if isinstance(profiles, list):
                data["profiles"] = [
                    {**p, "text": ""} if isinstance(p, dict) else p for p in profiles
                ]
        return data

    # ---------- 上次使用的项（**只在内存里**）----------

    def get_last(self) -> str | None:
        """上次用过的那一项；本进程没记过就是 ``None``（调用方回落「弹列表选」）。

        ``None`` 是**常态**而不是故障：程序每次启动都从这里开始，用户按一下热键落到
        「选一项」，之后这一次运行里就记住了。
        """
        return self._last

    def _set_last(self, name: str) -> None:
        self._last = name

    # ---------- 主流程 ----------

    def type_profile(self, name: str | None = None) -> "ActionResult | None":
        """入口编排：权限 → 输入法 → 倒计时浮层 → 注入。``name=None`` 时用上次项。

        返回值（2026-09-28 起，`ActionSpec.on_click` / `HotkeySpec.callback` 已扩宽）：

        - ``None`` ＝ **已经交给倒计时浮层去跑了**，结局在那条链上（浮层跑完自己退场），
          这里没话要说；
        - ``ActionResult(FAILED, ...)`` ＝ **这一次没打成**，去的三条路各给一句话。

        **为什么不再自弹框**：这四条路以前是 4 处 ``QMessageBox`` / ``QInputDialog``，
        它们是本文件能进 ``PLUGIN_PYSIDE6_WHITELIST`` 的**唯一**理由。麻烦不只是
        「组件不碰界面」（契约 §1.2），而是这两条触发路径**面前没有窗可挂**：热键路径
        多半没有页面开着，而面板路径在按下时就收起了自己。所以当时的结论是「没有落点，
        留待框架侧接通 ActionResult」—— 那半边已经接通：

        - **面板整行点击 / 面板快捷按钮** → 回执进面板结果槽（整行点击晚一拍，见
          ``ui_kit.defer_result_to_host``）；快捷按钮那枚面板已收起，走宿主的
          「面板之外」出口（系统通知）；
        - **全局热键 ⌃⌥P** → 走热键反馈通道（系统通知 / 轻浮层，档位在设置里逐条选）。

        **唯一仍走对话框的是辅助功能授权**（``ensure_accessibility``）：那一个是**系统**
        的引导框，且它自带「打开系统设置 / 触发授权弹窗」两个动作，不是我们能拿回执替掉的
        —— 它已在 ``core/permissions`` 单独挂账（零模态收口的最后一批），本模块不重复处置。
        """
        if not accessibility_trusted():
            if not ensure_accessibility():
                return None
        if name is None:
            name = self.get_last()
        if name is None:
            # 首次使用：不再弹私有选择框（那是个模态框，而且它的选项就在
            # 面板那一行的下拉里，同一件事有两个入口只是让用户多一层选择）。
            # `get_clickable` 保证「一个输入项都没有」时这一行不可点 ⇒ 这条路
            # 只会从热键 / 托盘进来，故文案要指向面板。
            if not self._profile_names():
                return ActionResult(
                    ActionOutcome.FAILED,
                    "还没有输入项，请先到设置窗的「一键输入」页添加一条。",
                )
            return ActionResult(
                ActionOutcome.FAILED,
                "还没有上次用的输入项 —— 请到面板「一键输入」那一行，"
                "用右侧下拉选一项，再点整行即输入。",
            )
        spec = engine.resolve_profile(self.plugin_config(), name)
        if spec is None:
            return ActionResult(
                ActionOutcome.FAILED,
                f"未找到输入项「{name}」，请到设置窗的「一键输入」页检查。",
            )
        ok, message = engine.ensure_english(
            bool(self.cfg.value("auto_switch_ime")), require=True
        )
        if not ok:
            return ActionResult(ActionOutcome.FAILED, f"输入法需切换：{message}")
        log.info("autotype 开始：%s（%d 字符，延迟 %ds）", name, len(spec["text"]), spec["delay"])
        # 入场先撤掉仍存活的旧浮层：倒计时未结束时再按一次会起两份、明文被输入两次；
        # 旧浮层的 destroyed 信号随后清掉引用（见 _on_overlay_destroyed）。
        prev = self._overlay
        if prev is not None and overlay_alive(prev) and prev.isVisible():
            prev.dismiss(DISMISS_CLOSED)
            self._overlay = None
        ov = AutotypeOverlay(spec)
        # destroyed 用闭包捕获具体对象：Plugin 不是 QObject，没有 sender() 可用；
        # 用 ref=ov 区分「撤掉的旧浮层」与「当前这份」，避免误清新浮层引用。
        ov.destroyed.connect(lambda *a, ref=ov: self._clear_if_current(ref))
        ov.finished.connect(lambda done: self._set_last(name) if done else None)
        self._overlay = ov
        ov.show_and_run()
        return None

    def _clear_if_current(self, ov) -> None:
        """该浮层真正销毁时，仅当它仍是当前引用才清 —— 撤掉旧浮层后新建的那一份
        不会被旧浮层延迟的 destroyed 信号误清。"""
        if self._overlay is ov:
            self._overlay = None

    def type_last(self) -> "ActionResult | None":
        """⌃⌥P 与面板「一键输入」按钮：输当前选定的那一项。

        判定走 :meth:`_target_name`（与整行点击**同一处**）—— 此前这里写死 ``None``
        （只认「上次项」），于是重启后按钮一律失败；没有上次记录时仍**给一句回执**
        （不再弹列表选择框）。
        """
        return self.type_profile(self._target_name())

    def _profile_names(self) -> list[str]:
        return [p["name"] for p in self.profiles() if p.get("name")]

    # ---------- §9.5 配置默认值单一真源 ----------

    def config_schema(self) -> FormSchema:
        """自动输入的默认值（§9.5 单一真源）。

        2026-09-20 从 ``config.default.yaml`` 迁来，出厂文件从此只留框架保留键
        ``enabled``；纯搬家，值逐字相同（``engine`` 里那几处 ``.get(key, ...)``
        从此只是防御性写法，真源在此）。

        ``profiles`` 默认空列表：出厂不留示例条目 —— 出厂文件里那两条本来就是
        注释，声明成真有两条会让新用户以为「我已经配好了」。
        """
        return FormSchema(
            plugin_id=self.id,
            version=1,
            fields=(
                FieldSpec(key="delay", label="全局倒计时（秒）",
                          type=FieldType.NUMBER, default=4),
                FieldSpec(key="interval", label="逐字符间隔（秒）",
                          type=FieldType.NUMBER, default=0.03),
                FieldSpec(key="press_enter", label="输完后回车",
                          type=FieldType.BOOL, default=False),
                FieldSpec(key="enter_delay", label="回车前等待（秒）",
                          type=FieldType.NUMBER, default=0.3),
                FieldSpec(key="auto_switch_ime", label="自动切回 ABC 输入法",
                          type=FieldType.BOOL, default=True),
                FieldSpec(key="profiles", label="自动输入项",
                          type=FieldType.LIST, default=[]),
            ),
        )

    # ---------- 配置读写（设置面的唯一入口；UI 不碰 Config，门禁 test_no_config_io_in_ui） ----------

    def profiles(self) -> list[dict]:
        """输入项整表（读的是**合并默认值后**的有效配置）。

        读合并值而不是用户差异段，是设置类编辑器的既有口径（三条口径第 3 条）：差异段
        会让新装用户看到空表。写回是整键覆盖，安全的前提是 ``profiles`` 的出厂默认就是
        ``[]``（``config.default.yaml`` 已如此）—— 否则第一次保存会把默认值物化进用户 YAML。
        """
        return list(self.cfg.value("profiles"))

    def save_profiles(self, profiles: list[dict]) -> bool:
        """把输入项整表写回用户 YAML 的 ``plugins.autotype`` 段。

        走 ``self.cfg.set``（读—改—写 ＋ 原子写 ＋ chmod 600，明文配置必须 600），
        **只动 ``profiles`` 这一键**：用户手写的 ``enabled`` / ``interval`` 等原样保留。
        返回 ``False`` ＝ 写盘失败 —— 由调用方（设置页）决定怎么说话，插件不在这里自作主张
        弹框：设置页自带反馈条（本模块一处自弹框都没有了，见 `type_profile`）。
        """
        try:
            self.cfg.set("profiles", profiles)
        except Exception as e:  # noqa: BLE001
            log.error("写回 autotype profiles 失败：%s", e)
            return False
        log.info("autotype profiles 已写回：%s", [p.get("name") for p in profiles])
        return True

    def autotype_settings(self) -> dict:
        """设置页要显示的三个全局默认。

        「显式 0 合法」这条判据由 ``cfg`` 保证：它只按**声明类型**归一（``isinstance``），
        不做真值判断 —— 用 ``or`` 回退默认会把 ``interval: 0`` 当 falsy 吞掉
        （同 ``engine.resolve_profile`` 给 ``delay`` 写的那条注释，是同一类陷阱）。
        三个默认值（``0.03`` / ``0.3`` / ``True``）的真源是 ``config_schema()``。
        """
        return {
            "interval": float(self.cfg.value("interval")),
            "enter_delay": float(self.cfg.value("enter_delay")),
            "auto_switch_ime": bool(self.cfg.value("auto_switch_ime")),
        }

    def save_autotype_settings(self, patch: dict) -> bool:
        """只写 ``patch`` 里给出的键（不整段覆盖，同 ipad_tunnel 的口径）。

        逐个走声明校验：键必须是本组件 ``config_schema()`` 里声明过的，
        值要过它的类型与 ``choices``。
        """
        try:
            self.cfg.update(patch)
        except Exception as e:  # noqa: BLE001
            log.error("写回 autotype 全局默认失败：%s", e)
            return False
        return True

    # ---------- 插件契约（声明点方法，决议 D20） ----------

    def component(self):
        """组件身份（每插件一枚，U2 §5.2）：**身份图标的唯一真源**（D14），名字省略取
        ``Plugin.name``。

        2026-09-20 前这里**刻意不声明**：本插件按需型、无状态机，而那时面板行是自绘的
        （``custom``），行图标由自绘行自己给 —— 声明身份只会让标题栏多一格「永远中性灰
        的图标」。行收归声明式后，行主位图标改由框架从 ``ComponentSpec.icon`` 取
        （D16），**没有身份图标那一行就是空的**，故必须声明。

        ``in_header=False``：本组件无状态机，标题栏那一格只会永远中性灰、说不出任何事
        （2026-09-17 用户裁定：无状态机的组件不上标题栏）。收归声明式后行主位图标需要
        身份，故身份要声明、标题栏那一格不要 —— 这正是 ``in_header`` 拆开的那两件事。
        """
        return ComponentSpec(icon=IconName.KEYBOARD, in_header=False)

    def row_specs(self):
        """状态行**走声明式**（不再用 ``custom`` 逃生舱自建）。

        原先走逃生舱只因「行内要一个下拉框」，而当时 ``StatusRowSpec.controls`` 只有
        开关 / 按钮 / 弹菜单三种 —— 弹菜单是「选一项就执行一项」，而这里要的是
        「切换只改选中、点整行才输入」，语义对不上。该语义已由第四种行内控件
        ``ChoiceSpec`` 承接，故回到声明式：行的尺寸 / 对齐由框架单点决定，不会再出现
        自绘行与框架行两套常量漂移（2026-09-20：自绘行 30 盒 / 16px / 左对齐，
        框架行 36 盒 / 22px / 居中，界面上表现为那一行图标小一圈且贴左）。

        ``title`` 不写（决议 D14）：行名由框架取组件名（``一键输入``）。自绘行时期
        这一行显示的是「文字输入」，那是逃生舱自己填的、恰好绕开了同一条门禁；收归后
        行名与组件名一致（与面板快捷按钮、全局热键三处同名 —— 2026-09-20 定名）。
        """
        return (
            StatusRowSpec(
                key="main",
                order=self.order,
                controls=(
                    ChoiceSpec(
                        get_items=self._profile_names,
                        get_current=self._choice_current,
                        on_change=self._choice_remember,
                        get_enabled=self._has_profiles,
                        tooltip="选一项，点整行即输入",
                    ),
                ),
                on_click=self._type_choice,
                get_clickable=self._has_profiles,
            ),
        )

    def _has_profiles(self) -> bool:
        """有没有输入项（决定整行可点、下拉可用）。"""
        return bool(self._profile_names())

    def _choice_current(self) -> str:
        """下拉当前项：用户改选过就用改选的，否则回落到「上次使用」。"""
        return self._choice or self.get_last() or ""

    def _sole_profile(self) -> "str | None":
        """**只有一项**时那一项的名字（多于一项给 ``None``：那是有歧义，不能替用户猜）。"""
        names = self._profile_names()
        return names[0] if len(names) == 1 else None

    def _target_name(self) -> "str | None":
        """这一次要输入哪一项 —— **全插件唯一的判定**（面板按钮与整行点击共用）。

        选中项 → 上次项 → 唯一那一项。

        为什么必须有最后一档：上次项 2026-09-29 起**只活在内存**（进程一重启就是空），
        而「一键输入」那枚按钮此前只认它 ⇒ 每次重启后按钮必然失败，且失败文案让用户
        「去面板下拉选一项」—— 可面板下拉选了之后按钮仍然不看它，照旧失败。
        两个入口各判一次「该输哪一项」，是这件事坏掉的根。
        """
        return self._choice or self.get_last() or self._sole_profile()

    def _choice_remember(self, name: str) -> None:
        """``ChoiceSpec.on_change``：**只记选中，不执行输入**。"""
        self._choice = name or None

    def _type_choice(self) -> "ActionResult | None":
        """整行点击：输入当前选定的那一项（判定见 :meth:`_target_name`）。"""
        return self.type_profile(self._target_name())

    def settings_page(self):
        """设置页：全局默认三项 ＋ 输入项增删改（界面在 ``settings.py``）。

        2026-09-19 由「一个按钮开私有 ``QDialog``」收归为设置页本体 —— 那个对话框里
        还留着 4 处 ``QMessageBox``（置顶窗内开嵌套事件循环，实测会让窗口卡死）。
        """
        return SettingsPageSpec(
            key="text-input",
            order=0,
            build=lambda: settings_ui.build_page(self),
            reason="custom: 输入项是「第 N 项的第 M 个字段」，外加两个全局数值项；"
                   "双栏在设置窗内容区（约 390×330）放不下，故用单栏 push 导航",
        )

    def actions(self):
        """面板操作区按钮。"""
        return (
            ActionSpec(
                key="type-last",
                order=0,
                on_click=self.type_last,
                icon=IconName.KEYBOARD,
            ),
        )

    def hotkeys(self):
        """全局热键。"""
        return (
            HotkeySpec(
                key="autotype_last",
                order=0,
                name="autotype_last",
                default_combo="ctrl+alt+p",
                callback=self.type_last,
            ),
        )

