"""hosts_guard/manager_view：hosts 记录管理内容面（**运行期面**，§7.3）。

**零模态**（2026-09-20，排期表 §S1-a）：全部编辑与反馈都在**行内**完成 ——
新增 / 编辑 / 接管共用一个行内表单页，批量导入是另一个行内页，确认与报错走页脚的
``FeedbackBar``。改前这里有三枚模态子对话框（``EntryDialog`` / ``TakeOverDialog`` /
``ImportDialog``，各带 ``exec()``）＋ 四处 ``QMessageBox`` ＋ 一处 ``QInputDialog``
（共 9 处模态，排期表 §S1 逐行点过名）；而本内容面自 2026-09-20 起住在**详情卡**里，
`shell/detail.py::DetailForm` 对两种形态都设 ``WindowStaysOnTopHint`` 并抬到
``DETAIL_LEVEL`` —— 正是「**置顶窗内开嵌套事件循环**」那条崩溃链路（80 帧崩溃报告
抄在 `tests/test_architecture.py` 的 `_MODAL_CLASSES` 门禁 docstring 里）的场合。
反馈出口是**各屏自己那一条** ``FeedbackBar``
（``info`` / ``error`` / ``ask``）—— 表页一条、两个二级屏各一条（“一区一条”）。

**三页结构**（一个 ``ui_kit.EditStack``，切换 = 换一屏，**不开新窗**）：

- 页 0 **表格**（基准屏）：全部记录 ＋ 搜索 / 来源 / 分类过滤 ＋ 一排动作键；
- 页 1 **条目表单**（`ui_kit.EditScreen`）：新增 / 编辑 / 接管**共用**（接管模式整组
  隐去「域名 / IP / 更新 / 解析方式 / 备注」—— 那几条各自保留自己的 IP，接管只让
  用户选分类）；
- 页 2 **批量导入**（`ui_kit.EditScreen`）：粘贴 hosts 文本 ＋ 分类 ＋ 行内「新建分类」。

◷ 2026-09-27：页 1 / 页 2 的版面改归框架（顶上 ``[‹ 返回] 标题``、页脚
``<stretch> [主动作]``、反馈槽在主动作之上）—— 此前两页各拼一份标题行 ＋ 页脚，
页脚还是**主按钮在左**，与仓里其余编辑屏相反。反馈槽也从「三页共用窗口底那一条」
改成**每屏一条**（共用那条会让二级屏上同时出现两条，见 `EditScreen` 的「一区一条」）。

**为什么不拆成「左列表 ＋ 右表单」**（route_guard 详情页那种）：本表是**七列**的
（来源 / 域名 / IP / 分类 / 自动更新 / 解析方式 / 状态），半宽（620 初宽的一半）每一列
都读不全。表格留整宽、编辑换一屏，是这张表能同时保住「一眼看全量」与「改得清楚」的排法
—— 七列还放得下是详情入口**声明了 860**（``DetailEntrySpec.width``）之后的事，
工具条一行十件也是靠这个宽度才摆平的（加「更新」那枚键时按实测从 780 提到 860）。

展示 /etc/hosts 的**全部**记录，但可编辑范围只限 BarKit 托管条目：外部条目（含第三方
软件写入的、用户手写的）一律只读，只能通过「接管」转入托管后才可管理——避免误删
aTrustAgent 之类第三方条目导致其它软件失效且难以排查。

表格只用纯文本单元格（不逐行挂控件），故上千条广告条目也能流畅筛选；
操作走「选中行 ＋ 按钮 / 双击行」。

**「更新」是唯一会发起网络请求的动作键**（2026-09-30 加），与其余几个有两处不同，
都是有意的：

- **结果异步到达**：解析要走网络，而插件目录内禁线程 / 禁时钟 ⇒ 真正的活归长驻服务
  线程（``plugin.refresh_entries`` 只做「登记 ＋ 把服务提前一拍」）。故按下之后本表
  不能立刻改结论 —— 它盯的是那个「待解析」标记（``status_index`` 的 ``pending``），
  服务解析完才划掉。
- **只重画那几格**：等待期间本表**不重建**（重建会掀掉选中与滚动位置、上千行时还卡），
  只把那几条的 IP / 状态两格文本换成最新读数。「翻页时钟条目『测试』」那种「按下去
  立刻能看到一个结论」的即时性，在这里只能用「几秒内自动变」来达到 —— 代价如实写进
  回执（超时就说超时，不谎称完成）。

**入口只有一条**（2026-09-20）：本内容面经 ``plugin.detail_entry().build`` 交给框架，
由 ``DetailHost`` 托管 —— 窗口级事务（标题 / 居中 / Dock 显隐 / 页脚「完成」）全归框架。

设置页那枚「管理条目…」按钮**已删**，原薄壳 ``HostsManagerDialog`` 随之一起没了。理由：
它与面板整行是**两个入口、各开一份内容面**（两份互不感知 —— 一份看不到另一份刚做的改动）。
故入口收敛到面板整行这一处，与 ``route_guard`` 详情页同款：集合编辑器只从面板整行进，
设置页不放启动键（设置页管**配置**，任务流归**详情卡**，两条边别互相串门）。

◷ **2026-10-05**：那个「插件主动打开自己详情页」的框架接口**已经有了**（S18，
``Plugin.open_detail`` → ctx → ``DetailHost.open``），且 ``DetailHost`` 现在按
plugin_id **复用同一份内容面**（S13）。若将来要让设置页也能进这一页，直接调
``self.open_detail()`` 即可 —— 它会把已经开着的那扇提到前台，不会再开出第二份。
今天仍然不放那枚按钮，理由是上面那条边界（设置页管配置），不是技术做不到。

**本文件自 2026-10-03 起只放「入口 ＋ 组合根」**：原 915 行的正文按职责搬进**同目录的兄弟
模块**（插件包不许子目录，见《插件结构规范》§2.3），方法经 ``self`` 取用实例状态（混合类，
零行为变更）：``manager_shell`` / ``manager_data`` / ``manager_table`` / ``manager_form`` /
``manager_import`` / ``manager_refresh`` 各一枚混合类，``manager_consts`` 放展示常量与日志名，
``manager_rows`` 放行数据 ``Row``。

**为什么不拆成子包**：§2.3「目录白名单（硬）」只允许包根 ``*.py`` / ``*.md`` / ``resources/`` /
``__pycache__/`` —— 插件包里不许有子目录，故这一族是**同目录的平铺兄弟模块**。

``manager_view.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QVBoxLayout,
    QWidget,
)

from barkit.shell.ui_kit import (
    EditStack,
    note,
)

from .manager_data import _DataMixin
from .manager_form import _FormMixin
from .manager_import import _ImportMixin
from .manager_refresh import _RefreshMixin
from .manager_rows import Row
from .manager_shell import _ShellMixin
from .manager_table import _TableMixin


class HostsManagerContent(
    _ShellMixin,
    _DataMixin,
    _TableMixin,
    _FormMixin,
    _ImportMixin,
    _RefreshMixin,
    QWidget,
):
    # 混合类按**职责**分开在同目录的兄弟模块里（插件包不许子目录，见《插件结构规范》
    # §2.3）：方法经 ``self`` 取用实例状态，故 MRO 顺序不影响变量解析（混合类之间
    # 无继承、无重名）。``QWidget`` 排在**最后** —— PySide6 的元类按最靠右的 Qt
    # 基类登记虚函数。本类**没有**信号 / ``Property`` / Qt 虚函数重写那三样（拆类的
    # 「三不许」），唯一一处 ``super()`` 在 ``__init__`` 里、留本体。
    """hosts 记录管理内容面（全部 UI 与逻辑，**零模态**）。

    ``on_changed`` 在条目发生变更后回调（用于同步 + 触发 helper）。

    ``status_index`` 是**必填**的参数（插件注入 ``plugin.status_index``）：本内容面要显示
    每条域名的**生效 IP 与解析状态**，而那份数据由插件算、也由插件给（app 侧，§7.4.3
    「状态解释归 app」）—— 取它属「跨面取数」，按《插件结构规范-缘起》§4 跨面禁令 3
    **只能由插件做**（本文件原先那处直接读状态文件的写法已被逐行点名移除，现在只走注入
    进来的索引）。

    做成必填而不是可选、默认空，是因为「忘了注入 / 注入的那一路取不到数」在这里的表现是
    **整列落在默认值上**：自 2026-09-10 起「状态」列恒显「待解析」就是这么来的（那一路读的
    字段名随解析链回迁 app 侧一起作废、默认值又恰好是个看着像「在等」的词）—— 一个看起来
    像「什么也没干」的静默降级，比构造期报错难查得多。

    ``on_refresh`` 是「更新」那枚键的去处（插件注入 ``plugin.refresh_entries``）：它
    **发网络**，故只登记 ＋ 把长驻服务提前一拍，结果异步到（详见模块 docstring 末段）。
    与 ``status_index`` 不同，这一条**允许为空** —— 没有它只少一枚按钮，不是数据缺失；
    故按钮的可用性判据里带上它（``on_refresh is None`` ⇒ 这枚键根本不出现，而不是
    画一个按了没反应的键）。

    条目主数据的读写走 :mod:`barkit.plugins.hosts_guard.store`（本组件自己的数据模块，
    不是 ``ctx`` / ``config``）；**每次动作即写盘**（``store`` 内部是原子写）——
    与设置页「改一次落一次盘」同一条纪律，故这里不需要框架的「保存」键
    （``DetailEntrySpec.save`` / 决议 D35）。
    """

    def __init__(self, status_index, on_changed=None, on_refresh=None, parent=None) -> None:
        super().__init__(parent)
        self._status_index = status_index
        self._on_changed = on_changed
        self._on_refresh = on_refresh
        self._rows: list[Row] = []
        self._shown: list[Row] = []
        self._entries: list[dict] = []
        self._categories: list[str] = []
        self._form_mode = "add"
        self._form_rows: list[Row] = []
        #: 「更新」按下之后还盯着哪几条（空 ＝ 没有在等的事）、盯了几眼、以及这一轮
        #: 盯梢的代号（连按两次时旧链靠它自己停，见 :meth:`_poll_refresh`）。
        #: 它只活在这一个动作的窗口内 —— 不是后台轮询。
        self._refresh_hosts: list[str] = []
        self._refresh_tries = 0
        self._refresh_seq = 0
        self._build_ui()
        self._reload()

    # ---------- 界面 ----------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        root.addWidget(note(
            "只有 BarKit 托管条目可修改；外部条目只读，需先「接管」才纳入管理。"
            "改动保存后由系统守护写入 /etc/hosts。", self))

        # 表页 ＋ 两个二级页（条目表单 / 批量导入）交给框架的 `EditStack` 换屏。
        # 反馈槽**每屏一条**（不再三页共用窗口底那一条）：`EditScreen` 的槽长在主动作
        # 之上，用户点完按钮就在手边看到回执；表页那条归表页（它不在 EditStack 里）。
        self._table_page = self._build_table_page()
        self._stack = EditStack(self._table_page, self)
        self._edit_form = self._build_form_page()
        self._edit_import = self._build_import_page()
        self._stack.push(self._edit_form)      # 登记两屏
        self._stack.push(self._edit_import)
        self._stack.back()                     # 开页停在表页
        root.addWidget(self._stack, stretch=1)
