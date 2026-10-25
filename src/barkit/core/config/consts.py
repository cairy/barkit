"""配置全族的常量与日志名：出厂文件路径、schema 版本、显示位与逐位默认值、框架保留键。

``config.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path


#: 配置全族共用的日志名（`logging_setup` 的 `%(name)s` 会写进日志文件）。
#: 写死父包名而不是 `__name__`：子模块各自的 `__name__` 会把日志外观改掉。
log = logging.getLogger("barkit.core.config")


# 仓库根目录的 config.default.yaml（源码运行形态，见决策 D8）。
# 它是**仓库相对路径**，不属用户数据域，故可安全按值持有。
# 深度 4 而不是 3：拆包前本行住在 ``core/config.py``（深 3 ⇒ 仓库根），
# 现在住在 ``core/config/consts.py``（深 3 ⇒ ``src/``）—— 逐字节搬移会指到
# 一个不存在的文件，而症状是四条用例 ``FileNotFoundError``，不是静默。
# 打包态（PyInstaller）下 ``__file__`` 位于 bundle 内，仓库相对布局失效：
# 改用 ``sys._MEIPASS`` 定位随包附带的 config.default.yaml（spec 放到包根）。
if getattr(sys, "frozen", False):
    DEFAULT_CONFIG_PATH = (
        Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        / "config.default.yaml"
    )
else:
    DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[4] / "config.default.yaml"
# 用户配置 = ``paths.APP_SUPPORT / "config.yaml"``。**刻意不做成模块级常量**：
# 那会诱导别人把它当默认参数用，而默认参数在**函数定义期**求值 → 测试的
# 模块属性补丁（monkeypatch.setattr(paths, "APP_SUPPORT", tmp)）对它无效。
# 路径一律在调用期经 ``paths`` 读取，见 Config.__init__。


# 配置 schema 版本。config.default.yaml 顶层 schema_version 须与之匹配，
# 否则视为不兼容（可能由更新版本的 BarKit 写入），启动即拒绝（早 fail 优于晚崩）。
SUPPORTED_CONFIG_SCHEMA_VERSION = 1


# 组件显示位（契约规范 §2.4）＝ 框架渲染的四个面。**声明层没有对应字段**：
# 「声明了就有」是默认值，用户层才需要开关 —— ``plugins.<id>.ui.<slot>.visible``。
# 顺序即设置窗里芯片的呈现顺序（标题栏 / 列表行 / 快捷区域 / 托盘角标）：面板内
# 三处在前，菜单栏那处在后；``action`` 插在中间而不是追加到尾部，是为了让既有的
# 三枚芯片相对次序不变（插在 ``row`` 与 ``tray`` 之间）。
PLUGIN_UI_SLOTS = ("titlebar", "row", "action", "tray")


# 各显示位的**默认可见性**（决议 D37，2026-09-22 用户裁定）。逐位不同：
#
# - ``titlebar`` / ``row`` / ``tray`` 沿用「声明即出现」—— 声明了 ``ComponentSpec``
#   就占标题栏那一格、有状态机就该在托盘里、行更是框架恒给每个组件合成的（D36）。
#   这三处是**舞台**：组件的身份与状态本来就该在那里出现。
# - ``action``（快捷区域）**默认关**。它是**快捷方式**而不是入口 —— 声明了详情入口的
#   组件，行上本来就点得进（D36：行与快捷区域是同一个过程），底部再摆一枚只是加速度。
#   默认全摆上的实测后果：五个组件各一枚，底部栏 A 区实得 224px 而按钮自然宽合计
#   401px，标签被挤成 48px 的截断字样（2026-09-22 用户报的形态）。**由用户自己挑
#   哪几个值得放进底部栏**。
#
# 新增显示位时必须在这里表态（`test_plugin_ui_slot_defaults_cover_contract_slots` 守）。
PLUGIN_UI_SLOT_DEFAULT: dict[str, bool] = {
    "titlebar": True,
    "row": True,
    "action": False,
    "tray": True,
}


def slot_default_visible(slot: str) -> bool:
    """该显示位的默认可见性；**未知取值按可见**（读侧不抛错，同 ``plugin_ui_visible``）。

    单独出一个函数而不是让两处各写一个 ``.get(slot, True)``：设置窗芯片在没有配置源
    （轻量桩）时也要按同一份默认画，否则「默认是哪一档」这件事就分叉成两份了。
    """
    return PLUGIN_UI_SLOT_DEFAULT.get(slot, True)


#: ``plugins.<id>`` 段里**归框架写**的键：「恢复默认配置」（§9.5 的初始化入口）不碰它们。
#: 今天只有一位 —— ``enabled`` 是「装 / 不装」的运行门控（设置窗那行上的开关就是它），
#: 清掉它会让一个被用户停用的组件凭空启用，那不是「配置回到默认」，是改了它的运行状态。
#: 显示位（``ui``）与框架节拍（``poll``）**不在其列**：它们确实是这个组件的配置，恢复默认
#: 时一并清掉（两者在设置窗行上各有可见控件，用户点完当场看得见变化）。于是恢复之后有一条
#: 好用的不变量：**该组件段里只剩下启用状态** —— 排查配置类故障时这正是要的。
#: 新增框架写入键时，这里与写入点必须一起改（门禁 ``test_reset_keeps_framework_owned_keys``）。
PLUGIN_SECTION_FRAMEWORK_KEYS = ("enabled",)
