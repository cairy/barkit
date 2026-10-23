"""route_guard/settings：设置面 —— 设置窗里的「网络切换」页 ＋ 点面板整行进入的配置窗体。

为什么配置窗体归**详情页**（《网络切换设计》§4.3 / §5.2-④）
----------------------------------------------------------
「场景」是**领域对象集合**：可增删、每项自己一组多字段（还有一层内嵌的路由清单）。
契约 §2.8 的判据里，这类编辑器走 ``DetailEntrySpec.build`` ＋ ``presentation="window"``
（框架托管的可缩放独立窗，决议 D26）；``config_schema()`` 那套平坦表单（``FormSchema``）
表达不了两层嵌套，硬塞进去只能把路由清单拍平成一行文本。形态照设计 §4.3 的 L3 草图：
**左列表（顺序即优先级，可拖）＋ 右表单**。

本模块的纪律（同 ``_template/settings.py`` / ``notify_guard/settings.py``）
--------------------------------------------------------------------------
- **不碰 ``ctx`` / ``config``**：读走 ``plugin.editable_profiles()``，写走
  ``plugin.save_profiles()`` —— 配置 IO 全在 ``plugin.py``（门禁 ``test_no_config_io_in_ui``）。
  本模块不知道自己属于哪个插件，也就无从读它的配置。
- **零模态弹框**（实测驱动，见 ``ui_kit.FeedbackBar`` 的类 docstring）：反馈一律走**行内**。
  从置顶窗内部弹模态 = 再开一条嵌套事件循环 ⇒ 用户看到「点了按钮，窗口不动」⇒ 再点一次
  ⇒ 最终只能强杀。旧 ``config_dialog.py`` 里有 4 处 ``QMessageBox``，本模块一处不留。
- **不自建时钟**：顶部状态条是「打开这一刻」的一次取值（插件禁 QTimer，UI 侧的时钟要有
  正当理由才进白名单）。
- 外壳统一用 ``shell.ui_kit`` 的 ``settings_page_shell`` / ``settings_section`` /
  ``tool_button`` / ``note`` / ``Divider`` / ``FeedbackBar``，不手搓样式。
  **设置页**的分组卡一律走 ``settings_section``（微圆角 ＋ 标题 / 分隔线 ＋ 内容），
  卡外不留裸行；``Divider`` 只留给**配置窗体**内部的自绘分区。

表单只放**今天就真生效**的字段
---------------------------------------------
名称 / 监控网卡 / 匹配网关 / 配置项级路由开关 / 路由清单 —— 这五样改完写入 YAML 就真的
改变行为（§6.3 第 7 步）；第 7b 步补上两处：设置页顶部的**自动化总闸**（`paused`）与
页脚的 `[固定使用]`（`pinned`，§2.3）；第 8 步再补最后一批，**表单至此盖满 schema 允许
的每一个键**：生效网段（`match.subnet`，判定随第 8 步前置落地）、仅手动使用
（`manual_only`，§2.6）、网络配置（`net` 三态，§2.5）、页脚 `[交还系统]`（§4.7 二）、
以及设置页的「无匹配时」（`fallback`）策略行。

第 7 步那张「仍不进表单的四样」清单（`manual_only` / `match.subnet` / `net` /
`[交还系统]`）自此**全部兑现**，一张不剩 —— 它们各自等的都是同一个前置：**helper 的
网卡配置能力**（第 8 步）。在那之前它们没有一样是「摆了就能生效」的（生效网段与
`manual_only` 的判定虽在第 8 步前置落地，但 `manual_only` 要置灰的那个输入框当时还不存在）。

**两处与设计草图的**有意**偏差**（口径真源就在这，别去别处找）：

1. **网卡配置按「三态」给，而不是 §4.3 L3 草图的「模式二值 ＋ 固化勾选框」。**
   草图那个形状（`( ) 手动  (•) DHCP` ＋ `☑ 命中时固化 IP`）要表达的是**同样的三个
   状态**；三态一个控件就说完了，而草图那样要两个控件再加一层「勾了才出现下面 4 行」
   的联动。**没有新增任何概念**：三态落到配置上仍是 ``net.mode`` ＋ 可选的 ``net.pin``
   （见 `_net_ui_mode` / `_net_from_form`），一个字节都不多。
2. **`manual_only` 勾上时，生效网段**与**匹配网关都置灰。** 设计的原话（§2.6）是
   「生效网段置灰」；这里是它的一般化 —— 两个输入框同属 ``match``，而 profile 的结构
   写着「``match`` 仅当 ``manual_only = false`` 时有效」（§2.2）。只灰网段会让匹配网关
   那个框看起来还能用，而它一样不参与判定。

**用户手写的键一律原样保留**：编辑器只改它显示的东西。据此有两条具体处置，都不是
「顺手归一化」：

- **形状认不出的 `net` 一个字都不碰**（`_net_ui_mode` 返回 `None` 时整块只读并说明原因）
  —— 三态里装不下它，硬塞进去再写掉就是替用户改配置；
- **等价写法原样留着**：`net: {mode: dhcp}`、`manual_only: false` 与原样不写在求值路径上
  完全同一，但抹掉它们**就是**改用户的配置，代价还不止那一行 —— `profiles` 是整表覆盖
  语义，一次改动会把**合并后**的整份场景表写进用户 YAML，默认场景就此多一份第二真源。
  故「跟随系统」只摘掉**真会写网卡**的那两种形状（`static` / 带 `pin` 的 `dhcp`），
  那才是用户真的动了这一项。（默认配置里就写着 `net: {mode: dhcp}`，这条不是假想。）


**本文件自 2026-10-02 起只放「入口 ＋ 场景编辑器本体」**：原 1283 行的正文按职责搬进
**同目录的兄弟模块**（插件包不许子目录，见《插件结构规范》§2.3），方法经 ``self`` 取用
实例状态（混合类，零行为变更）：``settings_chrome`` / ``settings_list`` / ``settings_netui`` /
``settings_items`` / ``settings_pin`` / ``settings_draft`` / ``settings_order`` 各一枚混合类，
``settings_netform`` 放网卡配置的纯换算（常量 ＋ 三个函数，无 Qt），``settings_page`` 放设置页。

**为什么不拆成子包**：§2.3「目录白名单（硬）」只允许包根 ``*.py`` / ``*.md`` / ``resources/`` /
``__pycache__/`` —— 插件包里不许有子目录，故这一族是**同目录的平铺兄弟模块**。

``settings.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import copy
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QLabel,
    QLineEdit,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from barkit.shell.ui_kit import (
    Divider,
    apply_text_role,
)

if TYPE_CHECKING:
    from barkit.plugins.route_guard.plugin import Plugin
    from barkit.shell.ui_kit import Switch      # 只用在一处类型标注上（逐条开关列）

from .settings_chrome import _SceneChromeMixin
from .settings_draft import _SceneDraftMixin
from .settings_items import _SceneItemsMixin
from .settings_list import _SceneListMixin
from .settings_netui import _SceneNetMixin
from .settings_order import _SceneOrderMixin
from .settings_page import build_page
from .settings_pin import _ScenePinMixin

# 再导出：调用面（``plugin.py`` 与测试）仍从**入口**取这些名字 —— 正文搬去了兄弟模块
# （``settings_netform`` / ``settings_page``），入口只负责把它们摆在同一张脸上。
# 平铺模式没有 ``__init__.py`` 替我们做这件事，故这一段是**手工**的。
from .settings_netform import (
    _NET_DRAFT_KEY,
    _NET_FORM_NONE,
    _NET_FORM_PIN,
    _NET_FORM_STATIC,
    _NET_LEASE_LABEL,
    _net_from_form,
    _net_to_form,
    _net_ui_mode,
)

__all__ = [
    "build_detail_page",
    "build_page",
    "_SceneEditor",
    "_NET_DRAFT_KEY",
    "_NET_FORM_NONE",
    "_NET_FORM_PIN",
    "_NET_FORM_STATIC",
    "_NET_LEASE_LABEL",
    "_net_from_form",
    "_net_to_form",
    "_net_ui_mode",
]


class _SceneEditor(
    _SceneChromeMixin,
    _SceneListMixin,
    _SceneNetMixin,
    _SceneItemsMixin,
    _ScenePinMixin,
    _SceneDraftMixin,
    _SceneOrderMixin,
    QWidget,
):
    # 混合类按**职责**分开在同目录的兄弟模块里（插件包不许子目录，见《插件结构规范》
    # §2.3）：方法经 ``self`` 取用实例状态，故 MRO 顺序不影响变量解析（混合类之间
    # 无继承、无重名）。``QWidget`` 排在**最后** —— PySide6 的元类按最靠右的 Qt
    # 基类登记虚函数。本类**没有**信号 / ``Property`` / Qt 虚函数重写那三样（拆类的
    # 「三不许」，见《拆包》第 3 步），唯一一处 ``super()`` 在 ``__init__`` 里、留本体。
    """场景编辑器：**改完攒着，点「保存」才写下去**（2026-09-18 用户裁定）。

    翻转了一条老口径，理由也换了：原口径是「改一次落一次盘、没有保存键」，担心的是
    外层壳那个「完成」会让人以为改过就生效了。可是**手动保存把这件事变成显式的**：
    界面上有一颗「保存」，关窗时框架会拦一下「还有没保存的改动」。于是「点了完成到底
    生效没有」这个问题不再靠猜 —— 写盘失败也**不再回滚**（那会连用户刚打的字一起丢掉），
    而是原样留在窗体里、红字说明卡在哪。

    仍然保留「半成品不许进配置」这条底线，只是判据从「先别写它」变成「**保存时拦下来**」：

    - ``self._draft`` = 「刚新增、还没填完」那个**场景**的下标。半份场景一旦写下去，
      列表顺序即优先级、它会直接参与自动匹配；更要紧的是**一处不合法会让整段配置被拒**
      （``rules.build_routes`` 一条不过就整份不写），等于把整个组件的路由都停掉。
    - `_NET_DRAFT_KEY` = 某个场景里**网卡那一块**还没填完（选了静态 IP 却没填地址，
      而 IP / 掩码是必填）。它记的是**那一格的内容**而不是下标，故跟着场景一起被拖序 /
      删除，不必额外搬。

    ``_draft`` 记的是**下标**，所以场景表一变形就得跟着挪（拖序 / 删除，见 ``_shift_draft``）。
    不能拿「网卡为空」当草稿判据：用户把一条**真场景**的网卡清空时长得一模一样，那样就
    把「填错了」当成「还没填完」，既不报错也不拦。

    **页脚那两枚（``[固定使用]`` / ``[交还系统]``）不是编辑**，是**动作**：点一下立刻
    写进配置，不参与「保存」。这不是「同一屏两套语义」—— 两者的分别用户看得见：动作
    改的是**当前生效的那一个值**（哪个场景在生效），而左边这张表改的是**整份场景表**。
    真正两套语义的是旧窗体（增删 / 调序即时落盘、字段要另点保存），那种「同一类改动
    两种待遇」才是没法预判的。
    """

    def __init__(self, plugin: "Plugin", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._plugin = plugin
        self._profiles: list[dict] = plugin.editable_profiles()
        # 「盘上是什么样」的快照：`dirty()` 靠比它与当前表判断有没有没保存的改动。
        # 存**快照**而不是「一个脏标记」，是为了让「改回原样」也自动算干净。
        self._baseline: list[dict] = copy.deepcopy(self._profiles)
        self._editing: int | None = None
        self._draft: int | None = None      # 刚新增、还没填完的场景下标
        self._loading = False       # 装载期的控件回填不得触发攒改动
        self._editable = False      # 当前选中的是不是可编辑的场景（`_load_scene` 维护）
        self._net_locked = False    # 这份 `net` 形状认不出 ⇒ 网卡那一块只读（`_net_ui_mode`）
        # 每条行 = （行容器, 目标, 下一跳, 逐条开关）；开关排在**末尾**，故取 `[0]` / `[1]`
        # 的既有读法不受影响。
        self._item_rows: list[tuple[QWidget, QLineEdit, QLineEdit, Switch]] = []
        self._placeholder: QWidget | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        # 顶部只读状态条（设计 §4.3 的 L3 草图）：「现在谁在管」不必用户去别处找
        self._status = QLabel(plugin.detail_status_text(), self)
        self._status.setWordWrap(True)
        apply_text_role(self._status, "status")
        outer.addWidget(self._status)
        outer.addWidget(Divider(self))

        # 左列表 / 右表单之间用**可拖动分隔条**（2026-09-18 用户要求）。原先写死 2:3 ——
        # 那个比例取决于「场景名与路由地址有多长」，而那是**用户当场才知道**的事：
        # 他这会儿在改哪一格，哪一格就该宽。故给初值、不锁死（识别窗口的左右两栏
        # 早就是同一套写法）。`setChildrenCollapsible(False)`：拖到 0 宽等于把一半
        # 功能藏起来，而那两栏各自都没有别的入口。
        split = QSplitter(Qt.Orientation.Horizontal, self)
        split.setChildrenCollapsible(False)
        left = QWidget(split)
        left.setLayout(self._build_left())
        right = QWidget(split)
        right.setLayout(self._build_right())
        split.addWidget(left)
        split.addWidget(right)
        # 逐栏再声明一次：`setChildrenCollapsible` 管的是拖动逻辑的默认值，而
        # `isCollapsible(i)` 读的是**每栏自己那个位**（Qt 里这两者不是同一份状态）。
        # 只设前者的后果是「测试读不出来、行为也未必一致」——两处都写才是明确意图。
        split.setCollapsible(0, False)
        split.setCollapsible(1, False)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        split.setSizes([190, 380])          # 初值：左侧场景列表收窄（右侧表单才是主战场）
        outer.addWidget(split, stretch=1)

        # 插件侧的 `_say_*` 只有一个出场口（`attach_feedback`），而本窗体是**另一个**
        # 编辑面：不接管的话，从这里按 `[交还系统]` 得到的回复只会进日志，用户看到的是
        # 「点了没反应」。谁最后打开谁持有那个槽位 —— 设置页与本窗不会同时在被操作，
        # 而各自的行内反馈（保存失败 / 二次确认）走的是**自己**那个 `self._fb`，不受影响。
        plugin.attach_feedback(self._fb)
        # 把自己交给插件：框架壳那枚「保存」键走的是组件的 `commit` 回调，而「怎么保存」
        # 只有本窗体知道（它手里才是用户正在编辑的那张表）。同 `attach_feedback`：
        # 销毁时摘引用（`destroyed` 之后再去调就是戳已删的 C++ 对象）。
        plugin.attach_editor(self)

        self._rebuild_list()
        self._select(0 if self._profiles else None)


def build_detail_page(plugin: "Plugin") -> QWidget:
    """配置窗体（框架按 ``DetailEntrySpec.build()`` 零参调用，故插件侧用闭包注入）。

    内容 = 左列表 ＋ 右表单的场景编辑器；壳多大由 ``presentation="window"`` 声明
    （框架给 620px 起、可缩放），本模块只管内容。
    """
    return _SceneEditor(plugin)
