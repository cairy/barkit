"""场景编辑器的**骨架**：左列表那一栏与右表单那一栏的搭建与接线。

只负责「把控件摆出来、把信号接上」；往里填什么值归 ``settings_list``，网卡那一块归 ``settings_netui``，路由清单归 ``settings_items``。

``settings.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QVBoxLayout,
    QWidget,
)

from barkit.shell.ui_kit import (
    ChoiceSelector,
    Divider,
    FeedbackBar,
    check_field,
    form_label,
    form_row_stacked,
    note,
    tool_button,
)

from .settings_netform import _NET_FORM_NONE, _NET_FORM_PIN, _NET_FORM_STATIC


class _SceneChromeMixin:
    """左右两栏的搭建与接线（只画，不装载）。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 左：场景列表 ----------

    def _build_left(self) -> QVBoxLayout:
        left = QVBoxLayout()
        left.setSpacing(6)
        # 文案里**不带星号标记**：这里的标签是 QLabel 纯文本，`**` 会原样上屏（本轮顺手修掉）
        left.addWidget(form_label("场景（顺序即优先级：先命中的赢；拖动可调序）", self))

        self._list = QListWidget(self)
        # 拖序 = 改配置（§2.1）：列表第 1 个命中者胜，「谁在前」本身就是配置的一等公民。
        # 旧窗体用「上移 / 下移」两个按钮代替，调一次序要点好几下，且看不出整体顺序。
        self._list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.currentRowChanged.connect(self._on_pick)
        self._list.model().rowsMoved.connect(self._on_rows_moved)
        left.addWidget(self._list, stretch=1)

        add = tool_button("＋ 新增", "pill-ghost", size="inline", parent=self)
        add.clicked.connect(self._on_add)
        left.addWidget(add)
        return left

    # ---------- 右：场景表单 ----------

    def _build_right(self) -> QVBoxLayout:
        right = QVBoxLayout()
        right.setSpacing(6)

        self._name = QLineEdit(self)
        self._name.setPlaceholderText("如 office / home")
        self._name.editingFinished.connect(self._collect_fields)
        right.addWidget(form_row_stacked("名称", self._name, self))

        right.addWidget(form_label("监控网卡 / 匹配网关（都写 = 同时满足才命中；它也是「下一跳留空」时的来源之一）", self))
        cond = QHBoxLayout()
        cond.setSpacing(6)
        self._iface = QLineEdit(self)
        # 多张用逗号或空格分隔：**顺序即候选优先级**，运行时取第一张命中的那一张
        # （第一命中制）。典型场合是扩展坞与直插转换器在系统里拿到的名字不同。
        self._iface.setPlaceholderText("如 en5（多张：en5, en6）")
        self._gw = QLineEdit(self)
        self._gw.setPlaceholderText("如 10.199.1.254")
        self._iface.editingFinished.connect(self._collect_fields)
        self._gw.editingFinished.connect(self._collect_fields)
        cond.addWidget(self._iface)
        cond.addWidget(self._gw)
        right.addLayout(cond)

        # 生效网段（§2.1 的命中主路径）：判定是「网卡上有一枚地址落在它里面」，实装见
        # `intent.network_probe`（与网关是 **AND**，都写才两个都要满足）。
        self._subnet = QLineEdit(self)
        self._subnet.setPlaceholderText("如 192.168.110.0/24")
        self._subnet.editingFinished.connect(self._collect_fields)
        right.addWidget(form_row_stacked(
            "生效网段（网卡上有一枚地址落在它里面 = 命中；可留空）", self._subnet, self,
        ))

        # 仅手动使用（§2.6）：勾上 = 退出自动匹配，只能从面板徽标切过去。理由是**自证
        # 循环**——判定依据（网卡 IP / 网关）正是组件自己会写进去的东西，判成真的那一刻
        # 起永远为真，而且离开那个网络也退不回来。故勾上后 `match` 两格一起置灰。
        self._manual = check_field(
            "仅手动使用（不参与自动匹配，只能从面板徽标切过去）",
            on_toggled=self._on_manual_toggled,
            parent=self,
        )
        right.addWidget(self._manual)

        # ── 网络配置（§2.5）：三态见 `_net_ui_mode` 的对应表 ──
        right.addWidget(Divider(self))
        self._net_mode = ChoiceSelector(
            [("跟随系统（不碰网卡）", _NET_FORM_NONE),
             ("固化租约（DHCP + 把拿到的地址固化）", _NET_FORM_PIN),
             ("静态 IP（写死地址）", _NET_FORM_STATIC)],
            _NET_FORM_NONE, parent=self,
        )
        self._net_mode.selected.connect(self._on_net_mode)
        right.addWidget(form_row_stacked(
            "网络配置（不改网卡就选「跟随系统」）", self._net_mode, self,
        ))
        self._net_box = self._build_net_fields()
        right.addWidget(self._net_box)
        self._net_hint = note("", self)
        right.addWidget(self._net_hint)

        # 配置项级总开关（§4.7 一）：这套路由「以后都不要」但场景与目标都留着。
        # 与面板那个 Switch 是两层：那个管「**本次**撤不撤」（覆盖层，离开场景即恢复）。
        self._routes_on = check_field(
            "叠加本场景的路由（取消勾选 = 整套不加；场景与目标都留着）",
            on_toggled=self._on_routes_toggled,
            parent=self,
        )
        right.addWidget(self._routes_on)

        head = QHBoxLayout()
        head.setSpacing(6)
        head.addWidget(form_label(
            "路由目标（每行一个网段 / 主机；下一跳留空 = 用「匹配网关」，"
            "本场景是「固化租约」时用它从网卡上取到的网关）", self))
        head.addStretch(1)
        add_item = tool_button("＋ 添加", "pill-ghost", size="inline", parent=self)
        add_item.clicked.connect(self._on_add_item)
        head.addWidget(add_item)
        right.addLayout(head)

        # **不给路由清单另套滚动区**（2026-10-06 删）：本窗体是详情页内容，框架已经
        # 把整页包进一层滚动区（`shell/detail.py::_scrolled`，理由见那里的 docstring）
        # —— 这里再包一层，窗一变矮就同时出现两条竖滚动条，而鼠标滚轮只驱动鼠标底下
        # 那一条，「上面还有内容」看不出来。清单的行随内容自然长高，滚动交给外面那一层。
        # （同在 `settings_items._refill_items` 里收尾的 `addStretch` 保证行贴着顶部。）
        self._items_box = QWidget(self)
        self._items_lay = QVBoxLayout(self._items_box)
        self._items_lay.setContentsMargins(0, 0, 0, 0)
        self._items_lay.setSpacing(4)
        right.addWidget(self._items_box, stretch=1)

        # 撤不掉的那一半必须写出来：helper 只对**意图里现存的规则**做增删
        # （`rules.py` 模块 docstring），一条目标从清单里消失，它此前加进路由表的条目就
        # 没有任何人去撤。给的路子是**可执行**的：先关整套（那会走「保留规则但 enabled=false」
        # 的路径，helper 照旧收敛），等一轮巡查过去再删。
        right.addWidget(note(
            "删掉一条目标（或删掉整个场景）<b>不会</b>自动撤掉已经加进路由表的那条——"
            "要撤干净，先取消勾选上面的「叠加本场景的路由」，等守护下一轮巡查生效后再删。",
            self,
        ))

        # 页脚：固定使用 ＋ 删除。设计 §4.3 草图里这一排还有
        # 一个 `[停止管理并恢复原状]` —— 2026-09-19 已裁**不排**，2026-10-02 落账（排期表 S17）：
        # 「停止管理」＝总闸、「恢复原状」＝交还系统，两件事各有落点；「撤干净系统状态」helper
        # 做不到（只删意图里还存在的规则，见上面那段 note）。另见设置页那段注释。
        # 「恢复 DHCP」是全局配置驱动的动作，入口只在徽标菜单（设置页只管配置），
        # 不放进单个场景编辑窗。
        foot = QHBoxLayout()
        self._pin = tool_button("固定使用", "pill-ghost", parent=self)
        self._pin.clicked.connect(self._on_pin)
        foot.addWidget(self._pin)
        self._del = tool_button("删除这个场景", "pill-ghost", parent=self)
        self._del.clicked.connect(self._on_delete)
        foot.addWidget(self._del)
        foot.addStretch(1)
        right.addLayout(foot)

        self._fb = FeedbackBar(self)
        right.addWidget(self._fb)

        self._refresh_net_ui()
        right.addWidget(note(
            "本窗体认得配置里的每一个键（名称 / 网卡 / 生效网段 / 仅手动 / 网络配置 / "
            "路由 / 固定使用）。不在窗体里的设置（自动化总闸、无匹配时的策略、守护安装）"
            "归设置页 —— 同一个字段不会有两处可改。"
        ))
        return right
