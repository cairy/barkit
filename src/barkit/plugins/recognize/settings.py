"""recognize/settings：设置面 —— 设置窗里的「识别」页。

**为什么有这个页**（2026-09-19）：本插件此前**没有设置页**（全仓六个插件里唯一），
而 `config.default.yaml:31-39` 给了它五项配置、且**五项都被代码真读** ——
`window_view.py:547-549` 读两个 `enabled`、`recognizer.py:96,97,123` 读另三项；
用户此前只能手改 YAML。页里的五项与配置一一对应。

**为什么是自绘 `build` 而不是 `schema`**（`SettingsPageSpec.reason` 那句的展开）：
`ocr.languages` 与 `barcode.barcode_types` 都是**列表型**，而 `core/schema.py` 的
`FieldType` 没有列表渲染器（契约 §2.8，与 `notify_guard` 的 `rules` 同款）
⇒ 自绘是今天唯一可用路径。

纪律（同 `ipad_tunnel/settings.py` / `route_guard/settings.py`）：

- **不碰 `ctx` / `config`**：读走 `plugin.recognition_settings()`、写走
  `plugin.save_recognition_settings()` —— 配置 IO 全在 `plugin.py`（门禁
  `test_no_config_io_in_ui`）；本模块不知道自己属于哪个插件；
- **改一次落一次盘、没有「保存」键**：外层壳自带「完成」，再加一道保存只会让用户
  以为改过了；文本格用 `editingFinished`（打字途中的半截值不落盘）；
- **零模态框**：写盘失败走行内 `FeedbackBar`；
- 外壳统一用 `shell.ui_kit` 的 `settings_page_shell` / `settings_section` / `Switch` /
  `ChoiceSelector` / `FeedbackBar` / `note`；行构件走 `form_row`（标签定宽、`caption`
  档），分区卡一律走 `settings_section`（标题 ＋ 分隔线 ＋ 内容，圆角 / 内边距 / 段距
  全取自 `shell/design/metrics`，不再各写魔法数 —— 2026-09-20 UI 统一重构 ＋
  2026-09-21 收卡返工）。

**语言 / 条码类型为什么是逗号分隔文本框而不是勾选组**：`ocrmac` 支持 30+ 种 BCP-47
标签、合法条码类型集由 `mac-barcode-read` 决定，**枚举必漏**（复述就会过期）；而
`recognizer.py:52` 的 `_LANG_ALIASES` 已能容错常见短码（`en` → `en-US`）。留空 = 写
`null` = 不指定偏好（语言回落系统默认，条码 = 全类型）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import QLineEdit, QVBoxLayout, QWidget

from barkit.shell.ui_kit import (
    ChoiceSelector,
    FeedbackBar,
    Switch,
    form_row,
    note,
    settings_page_shell,
    settings_section,
)

if TYPE_CHECKING:
    from barkit.plugins.recognize.plugin import Plugin

#: 识别级别取值。``ChoiceSelector`` 按**取值**选中（显示名只是标签）—— 落到配置上
#: 就是 ``ocr.recognition_level`` 的字符串，取值域由 ocrmac 定（``accurate`` / ``fast``）。
LEVEL_ACCURATE = "accurate"
LEVEL_FAST = "fast"

_LANG_PLACEHOLDER = "逗号分隔，如 zh-Hans, en-US（大小写敏感）"
_TYPES_PLACEHOLDER = "留空 = 全部类型"


def build_page(plugin: "Plugin") -> QWidget:
    """设置页（框架按 ``SettingsPageSpec.build()`` **零参**调用，故插件侧用闭包注入）。

    **分组**（2026-09-21 返工）：两张卡 —— 「文字识别（OCR）」把开关与它的两个参数
    （级别 / 语言）关在一张卡里，「条码识别」同理。原先开关各自一张卡、参数行却裸在
    页面底色上：开关与它管的参数看着像两件事（用户上报「没做到有效分组」）。
    页脚两句说明是**页面级**的话（不是某个控件的设置），留在卡外。
    """
    page, layout = settings_page_shell("识别")

    # 行内反馈槽：本页唯一的结果出口（写盘失败）。槽位的生死由插件管
    # （页面销毁时自动摘引用，见 `Plugin.attach_feedback`）。
    # **必须进布局**（2026-09-21 修）：此前只建了槽没往布局里放 ⇒ 它 `_say` 亮起时
    # 没有归属位置，会浮在页面左上角压住标题。
    fb = FeedbackBar(page)
    plugin.attach_feedback(fb)
    layout.addWidget(fb)

    cfg = plugin.recognition_settings()

    # ── 1. 文字识别（OCR）：开关 ＋ 它的两个参数 ──
    ocr_card, ocr_lay = _switch_section(
        "文字识别（OCR）", page,
        read=lambda: plugin.recognition_settings()["ocr_enabled"],
        write=lambda on: plugin.save_recognition_settings({"ocr": {"enabled": on}}),
    )
    ocr_lay.addLayout(form_row(
        "识别级别",
        _choice(
            [("准确（慢一点，识别更全）", LEVEL_ACCURATE),
             ("快速（快，适合截图里的短文本）", LEVEL_FAST)],
            cfg["ocr_recognition_level"], ocr_card,
            lambda v: plugin.save_recognition_settings(
                {"ocr": {"recognition_level": v}}),
        ),
        ocr_card,
    ))
    ocr_lay.addLayout(form_row(
        "语言",
        _text(
            _join_list(cfg["ocr_languages"]), _LANG_PLACEHOLDER, ocr_card,
            lambda text: plugin.save_recognition_settings(
                {"ocr": {"languages": _split_list(text)}}),
        ),
        ocr_card,
    ))
    ocr_lay.addWidget(note(
        "语言按偏好顺序排：排在前面的先试。短码（en / zh）会自动补成完整标签；"
        "整个留空 ＝ 不指定，用系统默认语言。关闭上面的开关后，这两项都不生效。",
        ocr_card,
    ))
    layout.addWidget(ocr_card)

    # ── 2. 条码识别：开关 ＋ 类型 ──
    code_card, code_lay = _switch_section(
        "条码识别", page,
        read=lambda: plugin.recognition_settings()["barcode_enabled"],
        write=lambda on: plugin.save_recognition_settings({"barcode": {"enabled": on}}),
    )
    code_lay.addLayout(form_row(
        "类型",
        _text(
            _join_list(cfg["barcode_types"]), _TYPES_PLACEHOLDER, code_card,
            lambda text: plugin.save_recognition_settings(
                {"barcode": {"barcode_types": _split_list(text)}}),
        ),
        code_card,
    ))
    code_lay.addWidget(note(
        "只认指定的条码类型（如 qrcode, code128）。留空 = 全部类型都试 —— "
        "不确定要哪种时留空即可。",
        code_card,
    ))
    layout.addWidget(code_card)

    layout.addWidget(note(
        "两个开关都关掉时，点「识别」不会有任何结果。",
        page,
    ))
    layout.addWidget(note(
        "图片来源只有剪贴板与文件两种 —— 不做内置截屏，少一个系统权限依赖"
        "（用 ⌃⌘⇧4 截图后打开本窗即可）。本页改动在<b>下次打开识别窗</b>时生效。",
        page,
    ))
    layout.addStretch(1)
    return page


def _switch_section(
    title: str, page: QWidget, *, read, write
) -> "tuple[QWidget, QVBoxLayout]":
    """一张「一级开关 ＋ 它管的参数」的分区卡：返回 ``(card, content)``。

    开关行是卡里的第一行（标签写「启用」—— 名字已经由卡片标题给了）。

    ``Switch.clicked`` **不翻转自身**（与 ``setChecked`` 解耦，防「刷新 UI → setChecked
    → 回环」，见 `ui_kit.Switch` 的 docstring），故这里自己推目标态、落盘后再从插件
    **回读真实值**同步 —— 写盘失败时开关注释会弹回原位，而不是留在一个骗人的位置。
    """
    card, lay = settings_section(title, parent=page)
    sw = Switch(card)
    sw.setChecked(read())

    def _on_click() -> None:
        write(not sw.isChecked())
        sw.setChecked(read())   # 回读（写失败即弹回）

    sw.clicked.connect(_on_click)
    lay.addLayout(form_row("启用", sw, card))
    return card, lay


def _choice(
    choices: "list[tuple[str, str]]", current: str, page: QWidget, on_change,
) -> ChoiceSelector:
    sel = ChoiceSelector(choices, current, parent=page)
    sel.selected.connect(on_change)
    return sel


def _text(
    current: str, placeholder: str, page: QWidget, on_change,
) -> QLineEdit:
    edit = QLineEdit(current, page)
    edit.setPlaceholderText(placeholder)
    # `editingFinished` 而非 `textChanged`：打字途中的半截标签（如 `zh-Ha`）不该落盘。
    edit.editingFinished.connect(lambda: on_change(edit.text()))
    return edit


def _split_list(text: str) -> "list[str] | None":
    """逗号分隔 → 列表；空串（或全是逗号/空白）→ ``None`` ＝ 不指定偏好。"""
    items = [part.strip() for part in text.split(",") if part.strip()]
    return items or None


def _join_list(value) -> str:
    """配置里的列表 → 文本框内容（``None`` 显示为空串）。"""
    return ", ".join(str(v) for v in (value or []))
