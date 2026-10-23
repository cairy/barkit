"""shell/state_machines_section：框架给「有状态机的组件」自动配的状态机段。

**组件零代码**：设置窗把插件贡献的设置页包一层，凡该插件声明了 ``StateSpec``，就
自动多出这一段 —— 判据不是「插件有没有自己写界面」，而是「它有没有状态机」。

段里给三件事（本批范围；配色与显示位下沉**不在内**，另议）：

- **一览**：每台状态机的主图标、标题、全部取值（默认文案 / id / 级别）；
- **当前生效值**：那一行取值前的圆圈画成**实心**（其余空心）—— 不另起一行写
  「当前 …」，同一句话不在一块里说两遍；话术挂这台机标题的**悬停**；
- **优先级**：调顺序，决定严重度相同时**哪台机当代表态**——**越靠前越优先**
  （数值小者＝排在上面；级别更严重的那台无条件优先，顺序只在同级时起作用）；
  缺省＝作者写的声明顺序。方向写在手柄悬停上（``_ORDER_HINT``）。

为什么「当前值」必须能刷新而不做开页快照：它是运行期数据，设置窗开着时状态会变。
只拍一次快照会得到一屏**静止的谎**（看着是「正常」，其实早就红了）。所以留
:meth:`StateMachinesSection.refresh_current`，由装配器在每次状态事件时叫一次 ——
窗口没开就不叫，零成本。

级别色为什么分**两张表**（2026-09-21）：圆圈是**图形**、取值名与级别说法是**文字**，
``design/levels`` 里图形档与文字档是两个令牌（判据不同：图形只要求压得住白图形）。
混用就是「一符两用」，写出来的小字在卡面上读不动。两张表都只存 token 名，
色值经 ``token()`` / 角色表解析 ⇒ 双主题自动换肤。

## 排序为什么改用 ``OrderEditor``（2026-09-21 返工）

原先这里手写了两枚 ▲▼ 小按钮（队首 / 队尾置灰）。两个问题：**同一个动作在同一个
应用里成了两种控件** —— 设置窗「插件」页与《录屏通知》的规则列表都是
:class:`~barkit.shell.order_editor.OrderEditor`（6 点手柄拖拽 ＋ 聚焦后 Alt+↑/↓），
只有状态机是按钮；而且那两个按钮带着 ``pill-ghost`` 的 ``padding: 6px 14px``，
在 24×24 的方钮上把字形整条裁掉，真机上就是「两个空白的白矩形」。

现在直接用 ``OrderEditor``（它本来就与插件无关：键由调用方定），于是三处消费方
共用同一条排序控件 —— 手写那半整体删除，包括「按钮置灰」那段分支。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from barkit.contribution.specs import StateLevel, StateReport, StateSpec
from barkit.shell import styles
from barkit.shell.design import metrics
from barkit.shell.design.levels import LEVEL_COLOR, LEVEL_TEXT_COLOR
from barkit.shell.design.tokens import token
from barkit.shell.order_editor import OrderEditor
from barkit.shell.renderers.state_view import resolve
from barkit.shell.ui_kit import (
    Divider,
    apply_text_role,
    glyph_pixmap,
    settings_section,
)

#: 级别的**业务说法**（不放技术名）：界面上写「异常」而不是 ``err``。
_LEVEL_TEXT: dict[StateLevel, str] = {
    StateLevel.OK: "正常",
    StateLevel.IDLE: "待机",
    StateLevel.OFF: "关闭",
    StateLevel.WARN: "警告",
    StateLevel.ERR: "异常",
}


#: 顺序的**含义**（``OrderEditor`` 接在手柄悬停的操作话术后面）。
#: 只说「能拖」不够 —— 用户不知道该把想优先的那台拖到**前面还是后面**
#: （2026-09-21 用户上报）。两句话都得是真的：代表态**先比级别**，更严重的那台
#: **无条件**优先，顺序只在**级别相同**时起作用；只写「越靠前越优先」会让人以为
#: 顺序能盖过级别（真源 ``renderers/representative``，别在这里另写一套判据）。
_ORDER_HINT = (
    "越靠前越优先：级别相同时，排在最前面的那台代表这个组件对外显示的状态。\n"
    "级别更严重的那台无条件优先 —— 顺序只在级别相同时起作用。"
)


def state_key(spec: StateSpec) -> str:
    """状态机的配置键：与 ``StateSpec.topic`` 的保留键同一口径（一台机 = ``main``）。"""
    return spec.key or "main"


def _level_token(level: StateLevel, *, text: bool) -> str:
    """该级别该用的**令牌名**：``text=True`` 拿文字档写小字，否则拿图形档画圆点。

    两张表不是一回事（``design/levels`` 里写死了判据）：图形档的 ``green`` / ``orange``
    / ``red`` 只保证**压得住白图形**（与白 ≥3.0），拿它写 11.5px 的字在卡面上只有
    3.5:1，读不动。所以「圆点」走图形档、「取值名与级别说法」走文字档。

    颜色一律经 ``apply_text_role(color=...)`` 落进**样式表** —— 别改回调色板：
    QSS 的 ``color`` 优先级高于 ``QPalette``，控件上只要有样式表，``setPalette``
    写的颜色一个像素都不生效（2026-09-21 实测：状态机段的级别色点就是这么全灰的）。
    """
    return LEVEL_TEXT_COLOR[level] if text else LEVEL_COLOR[level][0]


class StateMachinesSection(QWidget):
    """某组件的状态机一览：图标 / 取值 / 级别 / 当前值 / 排序。

    四个注入点都是**回调**（框架不认识配置、不认识运行期状态）：

    - ``get_priorities``：现读该组件写过的优先级（``{状态机 key: int}``）；
    - ``current``：按 ``StateSpec.topic`` 取当前报告（``None`` = 还没上报过）；
    - ``stale``：该 topic 是否已失联（失联时显示框架给的「状态失联」，而不是把上一次
      的读数当成「当前」—— 那是撒谎）；
    - ``on_priority``：写某台机的优先级（``None`` = 删键，回到声明顺序）。

    回调**可以不存在**：四样都缺时本段退化成一份只读的声明清单（没有排序手柄，
    因为没地方可写）—— 界面照常出，不因为装配器没接线就把整段藏起来
    （隐藏是破坏性的，与显示位读侧同一口径）。

    **取数口是「现问」而不是「构造时取一次」**：``current`` / ``stale`` 由装配器在
    设置窗建好之后才注入（``SettingsWindow.set_state_provider``），若构造时把
    ``self._state_provider`` 这个字段的值传进来，拿到的是那一刻的 ``None``，
    之后怎么注都进不去 —— 「当前」一栏就永远停在占位文案。
    """

    def __init__(
        self,
        *,
        specs: Sequence[StateSpec],
        get_priorities: "Callable[[], Mapping[str, int]] | None" = None,
        current: "Callable[[str], StateReport | None] | None" = None,
        stale: "Callable[[str], bool] | None" = None,
        on_priority: "Callable[[str, int | None], None] | None" = None,
        parent: "QWidget | None" = None,
    ) -> None:
        super().__init__(parent)
        self._specs = list(specs)
        self._get_priorities = get_priorities
        self._current = current
        self._stale = stale
        self._on_priority = on_priority
        # 状态机 key -> [(圆圈标签, 取值 id)]：``refresh_current`` 只改圆圈，不重建整段
        self._option_dots: dict[str, list[tuple[QLabel, str]]] = {}
        # 状态机 key -> 标题标签：当前值不单独占一行了，话术改挂**悬停提示**
        self._titles: dict[str, QLabel] = {}
        # 状态机 key -> 它行首那条分隔线：只有**第一行**不显示（见 ``_sync_separators``）
        self._separators: dict[str, QWidget] = {}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(metrics.GAP_MD)
        self._lay = lay

        card, content = settings_section("状态机", self)
        self._card = card
        self._editor = OrderEditor(
            build_row=self._machine_row,
            on_reorder=self._persist_order,
            handle_hint=_ORDER_HINT,
            parent=card,
        )
        self._editor.set_row_spacing(metrics.GAP_MD)
        self._editor.orderChanged.connect(self._after_order)
        content.addWidget(self._editor)
        lay.addWidget(card)
        self.reload()

    # ---------- 构建 ----------

    def reload(self) -> None:
        """按**当前优先级**重排整段（改过优先级 / 数据变了时调用）。"""
        keys = [state_key(s) for s in self._ordered()]
        self._option_dots.clear()
        self._titles.clear()
        self._separators.clear()
        self._editor.set_items(keys)
        self._sync_separators(keys)
        self.refresh_current()

    def _sync_separators(self, order: Sequence[str]) -> None:
        """台与台之间一条分隔线，**第一行不留**（它上面就是卡片标题那条线）。

        一段里塞三台机、台与台只靠间距分开，看着像「一台机的取值列表特别长」
        （2026-09-21 用户上报「没做到有效分组」）。线画在**每一行自己的顶部**而不是
        由外部插在行之间：``OrderEditor`` 的一行是一个 widget，外部插不进兄弟节点，
        而拖完顺序后「谁是第一个」会变 —— 所以按当前顺序显隐，而不是建行时定死。
        """
        for index, key in enumerate(order):
            line = self._separators.get(key)
            if line is not None:
                line.setVisible(index > 0)

    def _ordered(self) -> list[StateSpec]:
        """按关注度排序：配了优先级用配置值，没配用声明序号（小者在前）。"""
        prio = self._get_priorities() if self._get_priorities is not None else None
        prio = prio or {}

        def rank(item: tuple[int, StateSpec]) -> tuple[int, int]:
            index, spec = item
            raw = prio.get(state_key(spec))
            # bool 是 int 的子类：手改歪的 YAML 不算数，回落声明序号
            value = raw if isinstance(raw, int) and not isinstance(raw, bool) else index
            return (value, index)

        return [spec for _i, spec in sorted(enumerate(self._specs), key=rank)]

    def _spec_by_key(self, key: str) -> "StateSpec | None":
        for spec in self._specs:
            if state_key(spec) == key:
                return spec
        return None

    def _machine_row(self, key: str, handle: QWidget) -> QWidget:
        """一台状态机的一行（``OrderEditor`` 的行渲染回调）。

        版面是「**手柄 × 一台机**」——手柄纵跨标题与全部取值、上下居中，而不是吊在标题
        那一行上（与「插件」页、《录屏通知》规则列表同一手势：手柄在左，居这一块的中间）。

        取值行与标题行共用一列 ``GLYPH_W`` 宽的**图形槽**（标题那格放主图标、取值那格放
        圆圈）⇒ 两边的文字**左缘对齐**：图标有就有、没有就没有的话，这一列时宽时窄，
        文字会跟着左右跳。
        """
        spec = self._spec_by_key(key)
        box = QWidget(self._card)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(metrics.GAP_SM)
        if spec is None:  # 声明集合变了（不该发生）；宁可空行也不抛
            return box

        # 行首分隔线（第一行由 ``_sync_separators`` 隐掉）
        line = Divider(box)
        self._separators[key] = line
        lay.addWidget(line)

        body = QHBoxLayout()
        body.setSpacing(metrics.GAP_SM)
        if self._on_priority is None:
            # 没接写回调（没接配置 / 纯只读预览）⇒ **不出手柄**：能拖却弹回原位
            # 会被读成「坏了」，与先前「按钮一律置灰」是同一条判据。
            handle.hide()
        else:
            # 显式给「上下居中」：不给的话手柄会贴着这一块的顶边（它只有 26 高，
            # 而一台机连标题带取值能有好几行）。
            body.addWidget(handle, 0, Qt.AlignmentFlag.AlignVCenter)
        inner = QVBoxLayout()
        inner.setSpacing(metrics.GAP_XS)
        body.addLayout(inner, 1)
        lay.addLayout(body)

        head = QHBoxLayout()
        head.setSpacing(metrics.GAP_SM)
        # 标题行自带**底部余量**：取值是挂在这台机下面的，从属关系靠间距表达
        # （不靠缩进 —— 缩进会破坏两台机与取值之间的左缘对齐）。
        head.setContentsMargins(0, 0, 0, metrics.GAP_XS)
        head.addWidget(self._glyph(spec, box))
        title = QLabel(spec.title or state_key(spec), box)
        apply_text_role(title, "row_title")
        head.addWidget(title)
        head.addStretch(1)
        inner.addLayout(head)
        self._titles[key] = title

        # 全部取值：圆圈（当前生效实心 / 其余空心）＋ 取值名 ＋ id ＋ 级别说法。
        # **不再单独占一行写「当前」**：哪一行是实心圆圈就是当前，同一句话不说两遍。
        dots = self._option_dots.setdefault(key, [])
        for opt in spec.states:
            row = QHBoxLayout()
            row.setSpacing(metrics.GAP_SM)
            odot = QLabel("○", box)
            odot.setFixedWidth(metrics.GLYPH_W)
            odot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            apply_text_role(odot, "caption", color=_level_token(opt.level, text=False))
            row.addWidget(odot)
            name = QLabel(opt.text or opt.id, box)
            apply_text_role(name, "caption", color=_level_token(opt.level, text=True))
            row.addWidget(name)
            if opt.text:
                code = QLabel(opt.id, box)
                apply_text_role(code, "caption")
                row.addWidget(code)
            lvl = QLabel(_LEVEL_TEXT[opt.level], box)
            apply_text_role(lvl, "caption", color=_level_token(opt.level, text=True))
            row.addWidget(lvl)
            row.addStretch(1)
            inner.addLayout(row)
            dots.append((odot, opt.id))

        return box

    def _glyph(self, spec: StateSpec, parent: QWidget) -> QWidget:
        """标题行那格图形槽：有主图标就**画满这一格**，**没有也占同宽**（对齐用）。

        取 ``text1`` 而不是 ``text2``：这一格是「这台机」的身份，与下面取值行的彩色
        圆圈同列 —— 颜色也差一档才好分出「机器」与「它的取值」。
        """
        glyph = QLabel(parent)
        glyph.setFixedWidth(metrics.GLYPH_W)
        if spec.icon is not None:
            glyph.setPixmap(
                glyph_pixmap(
                    glyph,
                    spec.icon,
                    metrics.GLYPH_W,
                    token("text1", styles.current_theme()),
                )
            )
        return glyph

    # ---------- 运行期刷新 ----------

    def refresh_current(self) -> None:
        """把各台机的「当前生效值」重画一遍（状态事件时由装配器调用）。

        只改圆圈与悬停提示，**不重建整段** —— 状态每秒都可能来一次，重建会让用户刚按下
        手柄的焦点飞掉。

        「当前」今天画在两处：**实心圆圈**落在那一行取值上，话术挂这台机的**标题悬停**
        （没报告 ⇒ 「暂无报告」、失联 ⇒ 框架那句失联话术）。实心圆圈只认**取值名**，
        报了个声明里没有的取值时一个都不亮 —— 那不是「当前没值」，是组件写错了，
        由悬停那句原样把名字露出来（与面板同一口径：不静默吞掉）。
        """
        if self._current is None:
            return
        for spec in self._specs:
            key = state_key(spec)
            dots = self._option_dots.get(key)
            if dots is None:
                continue
            report = self._current(spec.topic)
            is_stale = bool(self._stale is not None and self._stale(spec.topic))
            # 级别 / 文案一律经 state_view.resolve —— 「取值名 → 一格」只有那一个地方
            # 会合成（D13）；这里自己 spec.option(...) 一遍等于把「summary 覆盖默认
            # 文案」「未声明取值按 ERR 露出」两条口径再抄一份，迟早与面板对不上。
            view = None if report is None else resolve(spec, report, stale=is_stale)
            active = None if view is None or is_stale else report.state
            for dot, opt_id in dots:
                dot.setText("●" if opt_id == active else "○")
            title = self._titles.get(key)
            if title is not None:
                if view is None:
                    # 占位写「暂无报告」**而不是一个破折号**：破折号读起来是「这栏坏了」
                    # 而不是「这台机还没上报」（2026-09-21 用户上报）。
                    title.setToolTip("暂无报告")
                else:
                    title.setToolTip(f"{view.text or report.state}（{_LEVEL_TEXT[view.level]}）")

    # ---------- 优先级落盘 ----------

    def _persist_order(self, key: str, to_index: int) -> bool:
        """按下标整段落盘；与声明顺序一致时**逐条删键**回到默认。

        返回 False ＝ 这一步不该发生（没有写回调 / key 不在册），``OrderEditor``
        据此回滚视图。真正的**写盘成功与否**这里拿不到（``on_priority`` 不返回状态，
        它写失败只记日志）—— 所以写完之后还有一道 :meth:`_verify_against_source`
        把视图与真源对一遍。
        """
        if self._on_priority is None:
            return False
        keys = [state_key(s) for s in self._ordered()]
        if key not in keys:
            return False
        others = [k for k in keys if k != key]
        to_index = max(0, min(to_index, len(others)))
        new_order = others[:to_index] + [key] + others[to_index:]

        declared = [state_key(s) for s in self._specs]
        if new_order == declared:
            # 用户调回了作者写的顺序 ⇒ 不留覆盖（日后作者调整声明顺序才传得到用户手上）
            for k in new_order:
                self._on_priority(k, None)
        else:
            for pos, k in enumerate(new_order):
                self._on_priority(k, pos)
        return True

    def _after_order(self, new_order: list) -> None:
        """拖完 / 键盘微调之后：先与**真源**核对，再按最终顺序显隐分隔线。

        写回调失败只记日志、不抛 ⇒ 编辑器已经把行挪走了而真源没动。不等下次开页：
        当场按真源整段重载，免得界面上留着一个假的顺序（「看得见的谎」比报错更坏）。
        顺序一致时只同步分隔线 —— 不重建行，手柄的焦点不飞。
        """
        truth = [state_key(s) for s in self._ordered()]
        if list(new_order) != truth:
            self.reload()
            return
        self._sync_separators([str(k) for k in new_order])
