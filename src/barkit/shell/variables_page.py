"""shell/variables_page：设置窗「变量」页（框架固定页，跨组件；设计文档 §2.6）。

**为什么是框架级页**：变量是**全局共享**的（任何组件的任务都能引用），配置 UI 不进任何
单个组件的设置页 —— 与「通用 / 快捷键」同级，紧挨「快捷键」之后。组件侧（如待办提醒）
只读取变量，这正是「变量不属于任务独享」的落点。

**这一页只编辑「声明」**（id / 来源 / 触发 / 表达式，§2.2 的声明与值分离）：运行值一律
不在这里改 —— ``ask`` 的回答按日落在 ``variables_state.json``（由询问浮层收）、``derived``
现算、``component`` 由组件推。故本页写回配置时**永不带上运行值**，用户今天填过的东西
不会因为改了一条声明就丢。

三件事各有唯一的落点（**不在这里重写第二份**）：

- 「值怎么算」归 ``core/variables``（池 ＋ 表达式引擎）；
- 「声明的形状」归 ``core/variables/spec``（本页只收集意图；两侧的校验口径成对，见
  :meth:`_VariableEditor._validate`）；
- 落盘归 ``core/config`` 的 ``set_variables``（本页不碰 YAML）。

页面拿到的能力一律**注入**（:class:`VariablesHost`）：它不认识事件总线、不认识变量池
那个类，只认识「列出来 / 问一次 / 有没有加载问题 / 我改了要通知谁」这四个回调。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from barkit.contribution.specs import IconName
from barkit.core.variables import expr, spec as spec_mod
from barkit.shell.design import metrics
from barkit.shell.ui_kit import (
    ComboField,
    EditScreen,
    EditStack,
    FeedbackBar,
    ModeBadge,
    RecordRow,
    Switch,
    clear_layout,
    form_label,
    form_row,
    note,
    settings_page_shell,
    settings_section,
    tool_button,
)

log = logging.getLogger(__name__)

#: 四类来源的显示名。``framework`` 不在配置里（内置），但它和另外三类一起列在页上 ——
#: 用户要看得见「``now`` 是哪来的」，否则他只会以为这几个名字是自己忘了声明。
SOURCE_LABELS = {
    "framework": "内置",
    "component": "组件",
    "ask": "询问",
    "derived": "算出",
}

#: 来源 → 行首图标盒的色档（取值来自 ``ui_kit.ICON_TONES``）。三档的语义见该常量：
#: 「内置 / 组件」中性（系统给的东西不该抢眼），「询问」浅底（还有件事要你做），
#: 「算出」实底 —— 它在这张列表里代表「自动来的值」，一眼看得出不是用户填的。
SOURCE_TONES = {
    "framework": "neutral",
    "component": "neutral",
    "ask": "accent",
    "derived": "solid",
}

#: 行的内边距：**左右是 0**，只留上下（与「快捷键」页同款）—— 行整条铺在分区卡里，
#: 左右那一段由卡的内容区统一给，免得行里的图标盒比卡标题多缩进一截。
PAD_ROW = (0, 8, 0, 8)

#: 询问变量今天能收集的类型。**只有时间**（浮层只实现了「填一个时刻」这一种问法，
#: 见 ``core.interaction.PROMPT_TIME``）—— 列在这里的才是真能弹出来的，写出来的选项
#: 就是承诺；别的类型弹出来会是个没有输入框的窗。
ASK_TYPES = ((expr.T_TIME, "时间"),)

#: ``trigger.when`` 三项的显示名（顺序即界面上的顺序，与 §2.5 的落法一一对应）。
WHEN_LABELS = (
    ("startup", "启动时"),
    ("daily", "每天首次"),
    ("variable_changed", "变量变化时"),
)

#: 比较符的显示名。
OP_LABELS = (
    ("==", "等于"), ("!=", "不等于"),
    (">", "大于"), ("<", "小于"),
    (">=", "不小于"), ("<=", "不大于"),
    ("in", "属于（逗号分隔）"),
)

_ID_RE = re.compile(rf"^{expr.ID_PATTERN}$")


@dataclass(frozen=True)
class VariablesHost:
    """页面要的五件事（**注入**：页面自己不去翻全局，也不认识 core 的宿主类）。

    - ``rows``：``() -> [(声明, 此刻的渲染值)]``（含内置那三只，内置排最后）；
    - ``probe``：``(变量 id) -> str``，**现算一次**并答回结果或算不出来的原因
      （用户按「测试」时叫一次）；不给 ⇒ 不画那枚按钮；
    - ``ask_now``：``(变量 id) -> bool``，「测试」撞上 ask 型时叫它弹一次询问；
    - ``problems``：``() -> tuple[str, ...]``，加载时跳过 / 记下的那些（页面顶部如实列出）；
    - ``on_changed``：本页写回配置之后叫一声（装配器据此让变量池按新声明重来一遍）。
    """

    rows: "Callable[[], list]"
    probe: "Callable[[str], str] | None" = None
    ask_now: "Callable[[str], bool] | None" = None
    problems: "Callable[[], tuple] | None" = None
    on_changed: "Callable[[], None] | None" = None


class VariablesPage(QWidget):
    """「变量」页：列表屏（看 / 问一次）⇄ 编辑屏（新增 / 改 / 删）。"""

    def __init__(
        self,
        *,
        config,
        host: "VariablesHost | None" = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._host = host

        page, lay = settings_page_shell("变量", self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(page)

        self._list_page, list_lay = self._build_list_page()
        # 换屏交给框架的 `EditStack`（与「快捷键」页同款）：列表屏 ⇄ 编辑屏。
        # 切页不再有「0 / 1 是哪一个」的魔数下标 —— 写错一位的症状是「点了返回、屏没变」。
        self._stack = EditStack(self._list_page, page)
        lay.addWidget(self._stack, stretch=1)

        self._warn = note("", self._list_page)
        self._warn.setVisible(False)
        list_lay.addWidget(self._warn)
        list_lay.addWidget(note(
            "跨组件共享：这里配一次，引用它的任务都跟着变。此页只存<b>声明</b>"
            "（来源 / 触发 / 表达式），运行值不写进配置 —— 询问的答案按日保存，"
            "改声明不会丢掉今天已经填过的值。",
            self._list_page,
        ))

        card, card_lay = settings_section("变量", self._list_page)
        list_lay.addWidget(card, stretch=1)
        card_lay.addLayout(self._build_bar(card))
        self._list = QWidget(card)
        self._list_lay = QVBoxLayout(self._list)
        self._list_lay.setContentsMargins(0, 0, 0, 0)
        self._list_lay.setSpacing(metrics.GAP_SM)
        card_lay.addWidget(self._list, stretch=1)
        # 反馈槽在**卡内**、紧跟列表（「一区一条」）：挂在页底就成了「整页的消息」，
        # 与它要说的那一行隔着一屏。
        self._fb = FeedbackBar(card)
        card_lay.addWidget(self._fb)
        self.refresh()

    # ---------- 界面 ----------

    def _build_list_page(self) -> "tuple[QWidget, QVBoxLayout]":
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        # 与外壳同档（``settings_page_shell`` 的 12）：页首说明到卡的距离，
        # 在本页与其它页是同一个数。
        lay.setSpacing(metrics.GAP_LG)
        return host, lay

    def _build_bar(self, parent: QWidget) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(metrics.GAP_MD)
        bar.addStretch(1)
        add = tool_button("+ 新增", kind="primary", size="inline", parent=parent)
        add.clicked.connect(lambda: self._open_editor(None))
        bar.addWidget(add)
        return bar

    def _rows(self) -> "list[tuple[object, str]]":
        """要画的那份清单：``[(声明, 值文本)]``。

        有宿主就问它（含内置、且每一行取的是**同一刻**的值）；没有宿主（未装配 / 单测）
        就只列配置里的声明 —— 值那格画一个破折号，**不假装有值**。
        """
        if self._host is not None:
            try:
                return list(self._host.rows())
            except Exception:  # noqa: BLE001 —— 取数口崩了不该让整页起不来
                log.exception("读变量清单失败，本次只列声明")
        specs, _ = spec_mod.parse_all(
            self._config.variables() if self._config is not None else []
        )
        return [(s, "—") for s in specs]

    def refresh(self) -> None:
        """重新合成列表并重画（保存 / 删除 / 测试之后走这里）。"""
        clear_layout(self._list_lay)
        rows = self._rows()
        for spec, value in rows:
            self._list_lay.addWidget(self._build_row(spec, value))
        if not rows:
            self._list_lay.addWidget(note("还没有变量。点右上角新增一个。", self._list))
        self._list_lay.addStretch(1)
        self._render_problems()

    def _render_problems(self) -> None:
        """把「加载时跳过 / 记下的那几条」如实列在页首。

        藏在日志里等于没发生：用户在配置里写错一条，界面上什么也看不出来，只有那一条
        变量静静地不生效（说了没人听得见）。
        """
        if self._host is None or self._host.problems is None:
            self._warn.setVisible(False)
            return
        try:
            problems = tuple(self._host.problems())
        except Exception:  # noqa: BLE001
            log.exception("读变量加载问题失败")
            problems = ()
        self._warn.setVisible(bool(problems))
        if problems:
            self._warn.setText("有几条声明没能生效：\n" + "\n".join(f"· {p}" for p in problems))

    def _build_row(self, spec, value: str) -> QWidget:
        """一行 = **主面板组件行那份骨架**（图标盒 ＋ 主标题 ＋ 副标题 ＋ 尾部控件）。"""
        row = RecordRow(
            title=spec.name,
            subtitle=f"{spec.id} · {_value_text(spec, value)}",
            icon=IconName.GEAR,
            icon_tone=SOURCE_TONES.get(spec.source, "neutral"),
            on_click=lambda s=spec: self._open_editor(s),
            clickable=True,
            margins=PAD_ROW,
            parent=self._list,
        )
        # 徽标走中性色（surface2 底 + text2 字）：它是**出处**不是状态，用强调色会读成
        # 「这一条有什么问题」（与「快捷键」页同一口径）。
        row.set_badge(ModeBadge(
            SOURCE_LABELS.get(spec.source, spec.source),
            accent=False, clickable=False, parent=row,
        ))
        if self._host is not None and self._host.probe is not None:
            test = tool_button("测试", size="inline", parent=row)
            test.setToolTip("现在算一遍这一条：直接给值的报结果，要问的弹询问卡")
            test.clicked.connect(lambda _checked=False, s=spec: self._test(s))
            row.add_trailing(test)
        return row

    def _test(self, spec) -> None:
        """「测试」：**现在**把这一条跑一遍，把结果说出来。

        - ask 型 ⇒ 弹一次询问卡（旧「刷新询问」的行为与理由原样保留，见 :meth:`_ask_now`）；
        - 别的（derived / component / 内置三只）⇒ **现算一次**并把结果落进本页反馈槽
          （走 ``host.probe``，不是复读列表上那份上一轮 ``tick`` 的结论 —— 用户常常是
          刚改完表达式就来按它）。

        合成一枚键、而不是「ask 行一枚、别的行另一枚」：用户想问的是同一件事 ——
        「这条变量现在是什么」；两半的差别只是值从哪儿来，不该让用户先判断这一行属于
        哪一类、再去找对应的那枚按钮（2026-10-10 用户提出）。
        """
        if spec.source == "ask":
            self._ask_now(spec)
            return
        if self._host is None or self._host.probe is None:
            return
        try:
            value = self._host.probe(spec.id)
        except Exception:  # noqa: BLE001 —— 取值口崩了不该把整页带走
            log.exception("试算变量失败：%s", spec.id)
            self._fb.error(f"算「{spec.name}」时出错了，详情见日志")
            return
        self._fb.info(f"{spec.name} 现在是 {value}")

    def _ask_now(self, spec) -> None:
        """立刻弹一次这条变量的询问卡（**不看条件也不看去重账**）。

        它存在的理由：用户把变量改成「只在办公室问」，而现在就想填一个值 —— 等条件是
        等不到的；而「拉」的那条路本就该绕过那两道为「推」设的闸（见
        ``InteractionCenter.ask_now``）。它走的是**临时 key**，真 key 那笔账不动。
        """
        if self._host is None or self._host.ask_now is None:
            return
        if self._host.ask_now(spec.id):
            self._fb.info(f"「{spec.name}」的询问已经弹出来了")
        else:
            self._fb.warn(
                f"「{spec.name}」弹不出来：得是<b>时间型</b>、且写了「什么时候问」。"
            )

    # ---------- 编辑屏 ----------

    def _open_editor(self, spec) -> None:
        """打开编辑屏。``spec=None`` ⇒ 新增；内置变量 ⇒ 只读展示。"""
        readonly = spec is not None and spec.source == "framework"
        editor = _VariableEditor(
            spec=spec,
            known=_known_ids(self._rows()),
            readonly=readonly,
            on_commit=lambda declaration, original: self._commit(editor, declaration, original),
            on_delete=None if (spec is None or readonly) else (
                lambda s=spec: self._confirm_delete(editor, s)
            ),
            parent=self,
        )
        editor.back.connect(lambda e=editor: self._close_editor(e))
        # 本页每次进编辑屏都**新建**一个实例（「开一个造一个」那一路）⇒ 收尾必须把那一个
        # 摘掉，不能只切屏 —— 细节与理由见 `_close_editor`。
        self._stack.push(editor)

    def _close_editor(self, editor) -> None:
        """收掉 ``editor`` 这一屏并回列表。

        **走 `drop` 而不是 `back`**（2026-10-10 用户报「编辑界面返回点了没反应」修）：
        `back` 只切当前页、把那屏**留着**（``EditStack.back`` 的 docstring 明说留着是给
        「同一屏反复用」的用法），而本页每次进都是新实例 —— 留着只会一个叠一个堆在
        ``QStackedWidget`` 里。而 ``QStackedWidget`` 的高度取**所有页的最大值**：编辑屏
        比列表屏高得多，于是「返回」之后列表仍被撑在那个高度上、设置窗外层滚动区的
        内容也不缩回去 —— 用户滚到下面再点返回，看到的是同一片空白，读起来就是
        「这按钮坏了」。与「快捷键」页同款（它本来就走的 `drop`）。
        """
        self._stack.drop(editor)

    def _confirm_delete(self, editor, spec) -> None:
        """删除：**行内确认**（零模态），默认不删 —— 这一步不可撤销。"""
        editor.feedback.ask(
            f"删除变量「{spec.name}」？引用它的任务会立刻变成「未定义」。",
            "删除",
            lambda s=spec: self._delete(editor, s),
            confirm_kind="danger",
        )

    def _delete(self, editor, spec) -> None:
        remaining = [
            dict(item) for item in (self._config.variables() if self._config is not None else [])
            if str(item.get("id") or "").strip() != spec.id
        ]
        if not self._write(editor, remaining):
            return
        self.refresh()
        self._close_editor(editor)
        self._fb.info(f"「{spec.name}」已删除")

    def _commit(self, editor, declaration: dict, original_id: str) -> None:
        """保存一条声明：**在哪一格改的，就还落在那一格**（改名也一样）。

        原地替换而不是「删掉再追加」：列表的次序是用户认行的那根线索 —— 追加会让刚改完
        的那一条跳到末尾，而用户下一次点的是他记得的那个位置，点出来是别的东西。
        """
        items = [
            dict(item) for item in (self._config.variables() if self._config is not None else [])
        ]
        original = str(original_id or "").strip()
        new_id = str(declaration.get("id") or "").strip()
        # 新增时 ``original`` 是空串：空串**不参与**匹配，否则会误伤配置里 id 缺失的坏条目
        # （它也会被当成本次要替换的那一条，顺手抹掉）。
        at = next(
            (i for i, item in enumerate(items)
             if original and str(item.get("id") or "").strip() == original),
            None,
        )
        drop = {original, new_id} - {""}
        items = [item for item in items if str(item.get("id") or "").strip() not in drop]
        if at is None:
            items.append(declaration)
        else:
            items.insert(min(at, len(items)), declaration)
        if not self._write(editor, items):
            return
        self.refresh()
        self._close_editor(editor)
        self._fb.info(f"「{declaration.get('name') or declaration.get('id')}」已保存")

    def _write(self, editor, items: list) -> bool:
        """落盘 ＋ 通知变量池重载。返回「成了没」（失败时错误落在编辑屏自己的槽里）。"""
        if self._config is None:
            editor.feedback.error("没有可写的配置（未装配）")
            return False
        try:
            self._config.set_variables(items)
        except Exception as e:  # noqa: BLE001 —— 坏声明由 ConfigError 拦下，照原话说给用户
            log.exception("写入 variables 段失败")
            editor.feedback.error(f"没能写入配置：{e}")
            return False
        if self._host is not None and self._host.on_changed is not None:
            try:
                self._host.on_changed()
            except Exception:  # noqa: BLE001 —— 重载失败不该把「已经存好了」说成没存
                log.exception("变量池重载失败（配置已写入，重启后生效）")
                editor.feedback.warn("已保存，但变量池重载失败，重启后生效")
        return True


class _VariableEditor(QWidget):
    """一条变量的编辑屏（内部是框架的 :class:`EditScreen`）。

    **组合而不是继承**（``EditScreen`` 的类 docstring 有一条 ⚠️：它是组合件，覆写出来的
    方法一次都调不到）。故本类持有那些控件、把两个回调交给 ``EditScreen``。
    """

    back = Signal()

    def __init__(
        self,
        *,
        spec,
        known: "set[str]",
        readonly: bool,
        on_commit,
        on_delete=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._spec = spec
        self._known = known
        self._readonly = readonly
        #: 保存时把「收上来的声明 ＋ 原来那个 id」交给页面（页面决定怎么并进配置）。
        #: **必须存在实例上**：``_save`` 是 ``EditScreen`` 的回调，那层壳不认识页面。
        self._on_commit = on_commit

        self._screen = EditScreen(
            title=f"变量 · {spec.name}" if spec is not None else "变量 · 新增",
            save_label="保存",
            on_validate=None if readonly else self._validate,
            on_commit=None if readonly else self._save,
            parent=self,
        )
        self._screen.back.connect(self.back.emit)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._screen)
        #: 本屏那条反馈槽（零模态的唯一出口）。删除确认也落在它上面。
        self.feedback = self._screen.feedback

        if readonly:
            self._screen.save_button.setVisible(False)
        self._build()
        if on_delete is not None:
            self._screen.add_destructive("删除这条", on_delete)

    @property
    def screen(self) -> EditScreen:
        """那层版面壳（判据读口）。"""
        return self._screen

    # ---------- 字段 ----------

    def _build(self) -> None:
        spec = self._spec
        _, body = self._screen.add_group("标识")
        self._id = _text_field(
            spec.id if spec is not None else "", "字母 / 数字 / 下划线", self._readonly
        )
        body.addWidget(_row("ID", self._id, self))
        self._name = _text_field(
            spec.name if spec is not None else "", "给它起个名字", self._readonly
        )
        body.addWidget(_row("名称", self._name, self))

        self._source = ComboField(self)
        for code in ("derived", "ask", "component"):
            self._source.addItem(SOURCE_LABELS[code], code)
        if spec is not None and spec.source in ("derived", "ask", "component"):
            _select(self._source, spec.source)
        if self._readonly:
            # 内置那三只：来源固定在「内置」，下拉只剩这一项且禁用 —— 不给一个选了也
            # 存不下去的选项（保存键已经藏了，下拉还能改就是骗人）。
            self._source.clear()
            self._source.addItem(SOURCE_LABELS["framework"], "framework")
            self._source.setEnabled(False)
        self._source.currentIndexChanged.connect(self._sync_visible)
        body.addWidget(_row("来源", self._source, self))

        self._derived = self._build_derived(spec)
        self._component = self._build_component(spec)
        self._ask = self._build_ask(spec)
        self._sync_visible()

    def _build_derived(self, spec) -> QWidget:
        box, body = self._screen.add_group("取值 · 怎么算", divider_above=True)
        self._expr = _text_field(
            spec.expr if spec is not None and spec.source == "derived" else "",
            "如 start_time + 9h",
            self._readonly,
        )
        body.addWidget(_row("表达式", self._expr, self))
        body.addWidget(note("只能用变量名与 + − × ÷、括号、时间/时长/数字/字符串字面量。", box))
        return box

    def _build_component(self, spec) -> QWidget:
        box, body = self._screen.add_group("取值 · 从哪位组件读", divider_above=True)
        is_it = spec is not None and spec.source == "component"
        self._component_id = _text_field(
            spec.component if is_it else "", "组件 id，如 route_guard", self._readonly
        )
        body.addWidget(_row("组件", self._component_id, self))
        self._component_key = _text_field(
            spec.key if is_it else "", "状态机 key，如 profile", self._readonly
        )
        body.addWidget(_row("状态机", self._component_key, self))
        self._component_field = ComboField(self)
        for code in spec_mod.FIELDS:
            self._component_field.addItem("取值名" if code == "state" else "副标题", code)
        _select(self._component_field, spec.field if is_it else "summary")
        self._component_field.setEnabled(not self._readonly)
        body.addWidget(_row("取哪一格", self._component_field, self))
        body.addWidget(note("读的是该组件<b>已经发布的状态快照</b>，不是它的内部函数。", box))
        return box

    def _build_ask(self, spec) -> QWidget:
        box, body = self._screen.add_group("取值 · 什么时候问", divider_above=True)
        is_it = spec is not None and spec.source == "ask"
        trigger = spec.trigger if is_it else None

        self._prompt = _text_field(
            spec.prompt if is_it else "",
            "弹窗问的那句；留空＝按变量名生成「…？」",
            self._readonly,
        )
        body.addWidget(_row("问什么", self._prompt, self))

        self._ask_type = ComboField(self)
        for code, label in ASK_TYPES:
            self._ask_type.addItem(label, code)
        _select(self._ask_type, spec.type if is_it else ASK_TYPES[0][0])
        self._ask_type.setEnabled(not self._readonly)
        body.addWidget(_row("收集", self._ask_type, self))

        self._whens: "dict[str, Switch]" = {}
        when_row = QHBoxLayout()
        when_row.setSpacing(metrics.GAP_MD)
        for code, label in WHEN_LABELS:
            cell = QWidget(box)
            cell_lay = QVBoxLayout(cell)
            cell_lay.setContentsMargins(0, 0, 0, 0)
            cell_lay.setSpacing(metrics.GAP_XS)
            cell_lay.addWidget(QLabel(label, cell))
            sw = Switch(cell)
            sw.setChecked(bool(trigger is not None and code in trigger.when))
            sw.setEnabled(not self._readonly)
            # ``Switch.clicked`` **不翻转自身**（见它的契约）⇒ 谁用它谁负责翻。漏掉这一句
            # 时这几枚时机开关全是死的，而保存那一侧读 ``isChecked()`` —— 表现为
            # 「一个时机都选不上」，且校验门还会回一句「至少选一个时机」。
            sw.clicked.connect(lambda _s=sw: _s.setChecked(not _s.isChecked()))
            cell_lay.addWidget(sw)
            self._whens[code] = sw
            when_row.addWidget(cell)
        when_row.addStretch(1)
        body.addLayout(_labelled("什么时候问", when_row, self))

        self._only_if_unfilled = Switch(self)
        self._only_if_unfilled.setChecked(
            True if trigger is None else bool(trigger.only_if_unfilled)
        )
        self._only_if_unfilled.setEnabled(not self._readonly)
        # 同上：不接这一句它就是个点了不动的方块，而 ``_declaration`` 读的是
        # ``isChecked()`` ⇒ 「已填就不再问」永远只能停在加载时的那个值上。
        self._only_if_unfilled.clicked.connect(
            lambda _s=self._only_if_unfilled: _s.setChecked(not _s.isChecked())
        )
        body.addWidget(_row("已填就不再问", self._only_if_unfilled, self, fill=False))

        self._conditions = _ConditionRows(
            conditions=trigger.conditions if trigger is not None else (),
            known=sorted(self._known),
            readonly=self._readonly,
            parent=self,
        )
        body.addWidget(self._conditions)
        return box

    def _sync_visible(self, _index: int = 0) -> None:
        """按来源显隐那三组字段 —— **只显示此刻用得上的那一组**。

        三组全摊着的话，用户得自己判断「表达式」这格对一条询问变量要不要填；而它不填
        也没关系（那一组压根不出现在保存出去的那份声明里）。
        """
        source = self._source.currentData()
        self._derived.setVisible(source == "derived")
        self._component.setVisible(source == "component")
        self._ask.setVisible(source == "ask")

    # ---------- 校验与落盘 ----------

    def _validate(self) -> "str | None":
        """返回错误文案 = 拦下。**与 ``spec.parse_all`` 的口径成对**（读侧比这里宽）。"""
        var_id = self._id.text().strip()
        if not var_id:
            return "ID 不能空 —— 引用它的任务写的就是这个字"
        if not _ID_RE.match(var_id):
            return "ID 只能是字母 / 数字 / 下划线，且不能以数字开头"
        if var_id in spec_mod.BUILTIN_IDS:
            return f"{var_id} 是框架内置变量，换一个名字"
        if self._spec is None and var_id in self._known:
            return f"已经有叫 {var_id} 的变量了"
        if self._spec is not None and var_id != self._spec.id and var_id in self._known:
            return f"已经有叫 {var_id} 的变量了"
        source = self._source.currentData()
        if source == "derived":
            return self._validate_derived()
        if source == "component":
            if not self._component_id.text().strip():
                return "要写「哪一位组件」"
            if not self._component_key.text().strip():
                return "要写「哪台状态机」（状态 topic 里那一段）"
        if source == "ask":
            if not any(sw.isChecked() for sw in self._whens.values()) \
                    and not self._conditions.rows():
                return "至少选一个时机或加一条条件 —— 否则它一登记就会弹一次"
        return None

    def _validate_derived(self) -> "str | None":
        text = self._expr.text().strip()
        if not text:
            return "算出型变量必须写表达式（怎么算）"
        try:
            used = expr.referenced_variables(text)
        except expr.ExprError as e:
            return f"表达式写错了：{e}"
        unknown = [name for name in used if name not in self._known]
        if unknown:
            return f"表达式里引用了不存在的变量「{unknown[0]}」"
        return None

    def _save(self) -> None:
        self._on_commit(self._declaration(), self._original())

    def _original(self) -> str:
        return self._spec.id if self._spec is not None else ""

    def _declaration(self) -> dict:
        """从控件上收一份声明（**只收声明，永不带上运行值**，见模块 docstring）。"""
        source = self._source.currentData()
        out: dict = {"id": self._id.text().strip(), "source": source}
        name = self._name.text().strip()
        if name and name != out["id"]:
            out["name"] = name
        if source == "derived":
            out["expr"] = self._expr.text().strip()
        elif source == "component":
            out["component"] = self._component_id.text().strip()
            out["key"] = self._component_key.text().strip()
            out["field"] = self._component_field.currentData()
        elif source == "ask":
            out["type"] = self._ask_type.currentData()
            prompt = self._prompt.text().strip()
            # 「问什么」留空 ＝ 弹窗按变量名生成那一句（宿主 ``spec.prompt or name+"？"``）。
            # 另一条随行规则：**问句是按旧名自动生成的那一句、而名字被改了** ⇒ 问句跟着
            # 名字走 —— 用户改的是名不是问句，留着旧名问句会让弹窗问另一个变量（
            # 2026-10-10 用户报：「弹出的还是询问下班时间」）。手写的问句一个字不动。
            orig = self._spec if self._spec is not None else None
            orig_name = (orig.name if orig is not None and orig.name else orig.id) if orig else ""
            if prompt == f"{orig_name}？" and name and name != orig_name:
                prompt = f"{name}？"
            if prompt:
                out["prompt"] = prompt
            trigger: dict = {
                "when": [code for code, sw in self._whens.items() if sw.isChecked()],
            }
            conds = self._conditions.rows()
            if conds:
                trigger["conditions"] = conds
            if not self._only_if_unfilled.isChecked():
                trigger["only_if_unfilled"] = False
            out["trigger"] = trigger
        return out


class _ConditionRows(QWidget):
    """ask 的触发条件行（变量 / 比较 / 值，扁平 AND；§2.5 与任务生效规则同构）。

    结构化而不是让人写表达式：这一格的错法只剩「选错了变量」，而自由文本能错出十种
    写法，且每一种都只在运行时表现为「这条条件判不出来」。
    """

    def __init__(
        self,
        *,
        conditions,
        known: "list[str]",
        readonly: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._known = list(known)
        self._readonly = readonly
        self._rows: "list[tuple[ComboField, ComboField, QLineEdit]]" = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(metrics.GAP_XS)
        outer.addWidget(form_label("生效条件（可依赖其他变量）", self))
        self._box = QVBoxLayout()
        self._box.setContentsMargins(0, 0, 0, 0)
        self._box.setSpacing(metrics.GAP_XS)
        outer.addLayout(self._box)
        if not conditions and not readonly:
            outer.addWidget(note("没有条件 ⇒ 只看「什么时候问」。", self))
        for cond in conditions:
            self._add(cond)
        if not readonly:
            add = tool_button("+ 加一条条件", size="inline", parent=self)
            add.clicked.connect(lambda _checked=False: self._add(None))
            outer.addWidget(add, alignment=Qt.AlignmentFlag.AlignLeft)

    def _add(self, cond) -> None:
        cell = QWidget(self)
        lay = QHBoxLayout(cell)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(metrics.GAP_SM)

        var = ComboField(cell)
        for name in self._known:
            var.addItem(name, name)
        if cond is not None and cond.var in self._known:
            _select(var, cond.var)
        lay.addWidget(var, stretch=1)

        op = ComboField(cell)
        for code, label in OP_LABELS:
            op.addItem(label, code)
        _select(op, cond.op if cond is not None else "==")
        lay.addWidget(op)

        value = _text_field(_value_text_of(cond), "比什么", self._readonly, cell)
        lay.addWidget(value, stretch=1)

        if not self._readonly:
            drop = tool_button("移除", size="inline", parent=cell)
            drop.clicked.connect(lambda _checked=False, c=cell: self._drop(c))
            lay.addWidget(drop)
        else:
            var.setEnabled(False)
            op.setEnabled(False)

        self._rows.append((var, op, value))
        self._box.addWidget(cell)

    def _drop(self, cell: QWidget) -> None:
        """摘掉一行：控件与 ``_rows`` 里那一项**同生共死** —— 只删控件会让读取时多读一行
        已消失的输入框（取到的是它被销毁前那一格的值，而界面上已经看不见它了）。"""
        self._rows = [item for item in self._rows if item[0].parentWidget() is not cell]
        cell.setParent(None)
        cell.deleteLater()

    def rows(self) -> list:
        """此刻填的那几条条件（**没填值的整行丢掉** —— 用户点了「加一条」又没填，不该变成
        一条「变量 X 等于空串」的真条件）。"""
        out = []
        for var, op, value in self._rows:
            if var.parentWidget() is None:
                continue
            text = value.text().strip()
            if not text:
                continue
            code = op.currentData()
            out.append({"var": var.currentData(), "op": code, "value": _parse_value(text, code)})
        return out


# ---------- 小工具 ----------

def _text_field(value: str, placeholder: str, readonly: bool = False, parent=None) -> QLineEdit:
    field = QLineEdit(str(value or ""), parent)
    field.setFixedHeight(metrics.CONTROL_H)
    if placeholder:
        field.setPlaceholderText(placeholder)
    field.setReadOnly(bool(readonly))
    return field


def _row(label: str, control: QWidget, parent: QWidget, *, fill: bool = True) -> QWidget:
    """「标签 + 控件」一行，**包成 widget** —— 那三组字段要整组显隐，而 layout 不能显隐。

    ``fill=False``（开关那几行）让控件贴右、余量给标签：一个 40px 的开关拉满一整行会
    在中间浮着，与上面那些输入框的左缘对不上。
    """
    box = QWidget(parent)
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    lay.addLayout(form_row(label, control, box, control_fill=fill))
    return box


def _labelled(label: str, inner: QHBoxLayout, parent: QWidget) -> QHBoxLayout:
    """把一组**横排的小控件**当成「一个控件」搭上标签（那三个时机开关就是这一种）。"""
    box = QWidget(parent)
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    lay.addLayout(inner)
    return form_row(label, box, parent)


def _select(combo: ComboField, value: str) -> None:
    """把下拉选到某个 ``itemData``（找不到就保持原样 —— 值不认识时别静默跳到第一项）。"""
    for i in range(combo.count()):
        if combo.itemData(i) == value:
            combo.setCurrentIndex(i)
            return


def _known_ids(rows) -> "set[str]":
    """已知的全部变量 id（含内置与**本页列出的每一条**）。

    用它做「引用了不存在的变量」那一格校验：口径比 ``pool`` 最终加载出来的集合**宽**
    （它含那些因别的原因被跳过的声明）—— 本仓那条「读侧必须比校验侧宽」，反过来的
    症状是「存得进去、算不出来」（变量在页面上看着是好的，运行时恒未定义且不报错）。
    """
    ids = set(spec_mod.BUILTIN_IDS)
    for item in rows:
        ids.add(item[0].id)
    return ids


def _value_text(spec, value: str) -> str:
    """一行副标题里的「值」那一格：算出型把表达式一起亮出来（用户要能对上「这値怎么来的」）。"""
    if spec.source == "derived" and spec.expr:
        return f"{spec.expr} → {value}"
    return value


def _value_text_of(cond) -> str:
    """把条件里的值写回输入框（``in`` 的列表用逗号连起来）。"""
    if cond is None:
        return ""
    value = cond.value
    if isinstance(value, (list, tuple)):
        return ", ".join(str(x) for x in value)
    return "" if value is None else str(value)


def _parse_value(text: str, op: str) -> object:
    """输入框里的字 → 条件值：``in`` 拆成列表，数字尽量转数字（其余保持文本）。"""
    if op == "in":
        return [_atom(part.strip()) for part in text.split(",") if part.strip()]
    return _atom(text)


def _atom(text: str) -> object:
    """一个标量：整数 / 小数 / 文本。**布尔不猜** —— 「true」在配置里更可能是个字符串值。"""
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text
