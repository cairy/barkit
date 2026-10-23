"""recognize：截图识别（OCR + 条码二合一）插件（方案 §4.1，P1）。

- run_mode = on_demand：按需触发，无常驻轮询
- 触发：热键 ⌃⌥O（hotkeys.recognize）+ 托盘菜单 + 面板按钮
- 窗口：点「剪贴板」/ 重按热键才载入剪贴板图（否则拾取态）；主线程
  不再自动解码剪贴板大图，避免与窗口激活策略切换死锁（实机卡死）；识别流水线见 window_view.py
- 识别库懒加载：首次识别触发时才 import（§3.4 内存目标），见 recognizer.py
- **UI 全部不在本模块**（门禁 ``test_plugin_py_has_no_pyside6``）：窗口在 ``window_view.py``、
  框选子视图在 ``selview.py``，本模块只**注入配置**并管窗口生命周期（§7.3 正确范式）。
  2026-09-19 清掉 5 个死 import（``Qt`` / ``QHBoxLayout`` / ``QLabel`` / ``QPushButton`` /
  ``QWidget``，全仓零使用），本模块由此**转正**、撤出白名单
- **设置面已补**（2026-09-19）：此前是全仓**唯一没有设置页**的插件（`config.default.yaml`
  给的五项配置只能手改 YAML）——现界面在 ``settings.py``、配置 IO 留在这里（§7.2）
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from barkit.capabilities.inline_feedback import InlineFeedbackCapability
from barkit.contribution.specs import (
    DETAIL_PRESENTATION_WINDOW,
    DetailEntrySpec,
    HotkeySpec,
    IconName,
    SettingsPageSpec,
)
from barkit.core.plugin import RUN_ON_DEMAND
from barkit.core.schema import FieldSpec, FieldType, FormSchema
from barkit.plugins.recognize import settings as settings_ui
from barkit.plugins.recognize.window_view import RecognizeContent, RecognizeWindow

if TYPE_CHECKING:                       # 运行期零 PySide6（门禁 test_plugin_py_has_no_pyside6 守）
    from PySide6.QtWidgets import QWidget

log = logging.getLogger(__name__)


class Plugin(InlineFeedbackCapability):
    # 行内反馈槽（`_say_*` / `attach_feedback`）来自 `InlineFeedbackCapability`；
    # 该能力自己就继承 `core.Plugin`，故不再把那个基类并列写一遍（同 `hosts_guard`）。
    id = "recognize"
    name = "截图识别"
    run_mode = RUN_ON_DEMAND
    required_permissions = []   # 框选是纯 UI 工作，不截屏，无额外权限（§4.1）
    api_version = 3              # 契约版本：框架按精确匹配校验（声明点方法路径）

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._window: RecognizeWindow | None = None

    # ---------- 入口 ----------

    def open_window(self) -> None:
        """打开/唤起识别窗口，用完即关（WA_DeleteOnClose），关闭即销毁释放原图。

        **先问框架要详情窗**（S18）：``Plugin.open_detail`` 走 ``DetailHost``，与面板
        整行 / 快捷按钮是**同一份内容面** —— 改之前热键另 new 一扇 ``RecognizeWindow``，
        于是「面板里刚识完的图」与「热键开的窗」两份互不感知，各自还都以为自己是唯一
        一份。``DetailHost`` 按 plugin_id 单例，窗已开着就把它提到前台（S13）。

        窗口已打开时再触发（托盘/热键）：顺便重新检查剪贴板——用户可能
        刚 ⌃⌘⇧4 截完图，热键提供即时载入路径（不依赖窗口内秒级轮询）。

        **要不到详情窗时退回独立窗口壳**（``ctx.open_detail`` 为 None，即框架没接线的
        场合 —— 单测正是这一态）：那扇独立窗因此不是死代码，而是「没有托管者时的
        降级形态」。
        """
        if self.open_detail():
            return
        if self._window is not None and self._window.isVisible():
            self._window.try_load_clipboard()
            self._window.raise_()
            self._window.activateWindow()
            return
        self._window = RecognizeWindow(self.plugin_config())
        self._window.destroyed.connect(self._on_window_destroyed)
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()

    def _on_window_destroyed(self) -> None:
        self._window = None

    # ---------- 插件契约（声明点方法，决议 D20） ----------
    #
    # **刻意不声明 ``component()``（标题栏图标）**：标题栏那一格的字是「现在什么状态」
    # （代表态的取值图标 ＋ 级别色），本插件是按需型、没有状态机也就没有取值图标可画
    # —— 声明了只会永远画同一枚身份图标、永远中性灰、悬停只说组件名，说不出任何事，
    # 白占标题栏一格（可见上限 7）。它在面板里的入口是**状态行**（框架为每个组件合成的
    # 朴素行，2026-09-22 起）＋ 底部「快捷区域」按钮 ＋ 热键；行主位那枚通用齿轮是框架
    # 的占位（本组件没声明身份图标），不代表它的身份。
    # 门禁 ``test_display_slot_declarations_need_a_state_machine`` 守这条。

    def actions(self):
        """面板操作区按钮：本组件改走 ``DetailEntrySpec``（见 :meth:`detail_entry`），
        由框架合成一枚快捷按钮统一经 ``DetailHost`` 打开，故这里不再单独声明
        ``ActionSpec``。"""
        return ()

    def detail_entry(self):
        """详情入口：统一走框架 ``DetailHost``（**整行点击与快捷区域同一过程**）。

        2026-09-20 从 ``ActionSpec(on_click=open_window)`` 旁路迁到
        ``DetailEntrySpec``：底部快捷按钮的弹窗不再各自 new 窗口、自己管 Dock 显隐，
        而与组件行点击收敛成同一条链（面板收起 → 发 detailRequested →
        ``DetailHost.open`` → ``DetailForm`` 托管）。

        2026-09-22 又去掉 ``trigger="action"``（字段已删）—— 组件**行**从这一端起
        恒在（框架为每个组件合成朴素行），「行点击」与「快捷区域按钮」是同一个过程、
        两个入口各归用户层的显示位开关管，不必再由作者二选一。

        ``label`` 留空——单条入口时框架补 ``Plugin.name``（「截图识别」）；写了反而
        触发名字位门禁 ``test_identity_name_is_not_written_twice``。``icon=SCAN`` 由
        框架合成快捷按钮时一并带过去（``DetailEntrySpec.icon`` 字段专为此加）。
        """
        return DetailEntrySpec(
            presentation=DETAIL_PRESENTATION_WINDOW,
            icon=IconName.SCAN,
            build=lambda: RecognizeContent(self.plugin_config()),
        )

    def hotkeys(self):
        """全局热键。"""
        return (
            HotkeySpec(
                key="recognize",
                order=0,
                name="recognize",
                default_combo="ctrl+alt+o",
                callback=self.open_window,
            ),
        )

    def settings_page(self):
        """设置页：识别总开关与两项子功能的参数（界面在 ``settings.py``）。

        2026-09-19 补上 —— 本插件此前是全仓唯一**没有设置页**的，而
        `config.default.yaml` 给它的五项配置全都被代码真读，用户只能手改 YAML。
        """
        return SettingsPageSpec(
            build=lambda: settings_ui.build_page(self),
            reason="custom: ocr.languages 与 barcode.barcode_types 都是列表型字段，"
                   "而 core/schema.py 的 FieldType 无列表渲染器（契约 §2.8）"
                   "⇒ 自绘是今天唯一可用路径（同 notify_guard 的 rules）",
        )

    def _build_settings_page(self) -> "QWidget":
        """设置页的稳定门面（框架按 ``SettingsPageSpec.build()`` **零参**调用）。

        UI 本体在 ``settings.py``（§7.2：设置面的全部界面归它），本方法只做转发 ——
        与 ``route_guard`` / ``hosts_guard`` / ``ipad_tunnel`` 同一条边界：插件是唯一
        知道该把什么注入界面的一侧。
        """
        return settings_ui.build_page(self)

    # ---------- §9.5 配置默认值单一真源 ----------

    def config_schema(self) -> FormSchema:
        """识别配置的默认值（§9.5 单一真源）。

        2026-09-20 从 ``config.default.yaml`` 迁来，出厂文件从此只留框架保留键
        ``enabled``。这是**纯搬家**：每个键的值逐字相同，有效配置不变
        （``Config`` 的合并顺序是「出厂文件 → schema 默认值 → 用户配置」，
        schema 在后、用户配置最后覆盖）。

        ``ocr`` / ``barcode`` 是**嵌套段**，整段用 ``FieldType.DICT`` 声明。
        **别拆成 ``ocr.enabled`` 这样的平铺字段**：``defaults()`` 会把键名原样
        放进 dict，产出字面键 ``"ocr.enabled"``，而消费方读的是
        ``cfg["ocr"]["enabled"]``（``window_view.py:_start`` /
        ``recognition_settings()``）⇒ 默认值静默失效。

        ``barcode.barcode_types`` **故意不声明**：出厂文件里它是注释
        （「不填 = 全类型」），语义是「缺省即不设限」；声明成 ``[]`` 会把它变成
        「一个类型都不认」。
        """
        return FormSchema(
            plugin_id=self.id,
            version=1,
            fields=(
                FieldSpec(
                    key="ocr", label="OCR", type=FieldType.DICT,
                    default={"enabled": True, "languages": ["zh-Hans", "en-US"],
                             "recognition_level": "accurate"},
                    tooltip="languages 要 BCP-47 完整标签（zh-Hans / en-US），短码 en 会被 "
                            "ocrmac 拒绝；recognition_level 取 accurate | fast。",
                ),
                FieldSpec(
                    key="barcode", label="条码", type=FieldType.DICT,
                    default={"enabled": True},
                    tooltip="barcode_types 不填 = 全类型；填则只认列出的类型"
                            "（如 qrcode / code128）。",
                ),
            ),
        )

    # ---------- 设置面（settings.py）的数据面（§7.2：UI 只画，配置 IO 留在这里）----------

    def recognition_settings(self) -> dict:
        """设置页要读的**合并后有效配置**（页面只管显示，不做兜底）。

        两个嵌套段（``ocr`` / ``barcode``）取 ``cfg`` 的整段口：整段被写成非对象时
        回落**声明默认值**（``config_schema()`` 里那两条 ``FieldType.DICT``），
        与其它配置键同一口径。段内子键仍逐项 ``.get``：``barcode.barcode_types``
        **故意不在声明里**（「缺省即不设限」而不是「一个类型都不认」，见
        ``config_schema()`` 的说明），它没有默认值可以回落。
        """
        ocr = self.cfg.value("ocr")
        barcode = self.cfg.value("barcode")
        return {
            "ocr_enabled": bool(ocr.get("enabled", True)),
            "ocr_recognition_level": str(ocr.get("recognition_level") or "accurate"),
            "ocr_languages": list(ocr.get("languages") or []),
            "barcode_enabled": bool(barcode.get("enabled", True)),
            "barcode_types": list(barcode.get("barcode_types") or []),
        }

    def save_recognition_settings(self, patch: dict) -> bool:
        """写回设置页改动（嵌套一层：``{"ocr": {"enabled": False}}``）。

        **只写 ``patch`` 给出的键**：本页不显示的键（``enabled`` 总开关、用户事后手加的
        项）不该被界面替他删掉 —— 同《网络切换》「用户手写的键一律原样保留」。

        **不通知已开的识别窗**：窗口每次打开都重读 ``plugin_config()``
        （``open_window`` 里那行），改动**下次开窗生效**；正开的窗保持本次识别的参数 ——
        识别是一次性的，中途换语言只会让同一张图的结果前后对不上。

        走的是逃生舱 ``cfg.mutate``：``patch`` 给的是**段内子键**（``ocr`` 里的某几项），
        而子键没有各自的声明，「等于默认即删键」对它无从判定；换成整段 ``cfg.set``
        会把 ``languages`` / ``recognition_level`` 这些用户没提过的子键一并物化进 YAML。
        """
        def _mutate(section: dict) -> None:
            for key, value in patch.items():
                if isinstance(value, dict):
                    sub = section.get(key)
                    if not isinstance(sub, dict):
                        sub = {}
                        section[key] = sub
                    sub.update(value)
                else:
                    section[key] = value

        try:
            self.cfg.mutate(_mutate)
        except Exception as e:  # noqa: BLE001
            log.error("写识别配置失败：%s", e)
            self._say_error("保存失败——写回配置没成功，请重试。")
            return False
        self._say_info("已保存，下次打开识别窗生效。")
        return True