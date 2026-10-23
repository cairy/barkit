"""模块级小件：下拉候选的拼装、显示位芯片刷新、分页滚动区。

``settings.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QScrollArea,
    QWidget,
)

from barkit.contribution.specs import TRAY_MIN_LEVELS
from barkit.contribution.specs_outcome import ActionOutcome, as_action_result

from .constants import _BADGE_LEVEL_LABELS, _POLL_LADDER, _SLOT_LABELS, log


def _poll_text(seconds: float) -> str:
    """节拍的显示文案：整数省掉小数点，非整数照实写（用户手改过 YAML 也要看得懂）。"""
    value = float(seconds)
    return f"{int(value)} 秒" if value.is_integer() else f"{value:g} 秒"


def _same_poll(a: float, b: float) -> bool:
    return abs(float(a) - float(b)) < 1e-9


def _poll_choices(override: float | None, default: float) -> list[tuple[str, str]]:
    """刷新间隔的候选：``[默认（X 秒）] ＋ 阶梯（去掉与默认同值的那一档）＋ 离梯的当前值``。

    去掉同值那一档是**防残渣**：选「30 秒」与选「默认（30 秒）」在界面上是两回事，
    落盘却是「写了覆盖」与「没写覆盖」两种配置 —— 同一个数字两种状态，日后组件调整
    建议值时一种跟着走、一种钉死，而 YAML 里看不出区别。

    离梯值（用户手改过 YAML，如 ``poll: 7``）**如实列出来**，绝不显示成「默认」：
    控件的当前项必须等于真正生效的那个数，否则界面就在撒谎。
    """
    choices: list[tuple[str, str]] = [(f"默认（{_poll_text(default)}）", "")]
    ladder: list[float] = [v for v in _POLL_LADDER if not _same_poll(v, default)]
    if override is not None and not _same_poll(override, default):
        if not any(_same_poll(override, v) for v in ladder):
            ladder.append(float(override))
        ladder.sort()
    # 「取值」就是那个数字（点完要解析回秒数）；显示名才带单位
    choices += [(_poll_text(v), f"{v:g}") for v in ladder]
    return choices


def _badge_level_choices() -> list[tuple[str, str]]:
    """托盘阈值下拉的候选：``[(显示名, 取值), ...]``。

    候选**由契约的值域生成**，不在这里另抄一份 —— 契约哪天多一档，这里跟着长出来；
    查不到文案的档位如实显示裸取值（宁可难看，也不静默漏掉一个合法选项）。
    """
    return [(_BADGE_LEVEL_LABELS.get(v, v), v) for v in TRAY_MIN_LEVELS]


# 「菜单栏」卡内的「标签 + 控件」行已泛化为 ``ui_kit.form_row``（2026-09-20 UI 统一重构）。



def _apply_chip(btn, slot: str, visible: bool) -> None:
    """把显示位芯片刷成当前状态（亮 ＝ 显示中）。

    ``objectName`` 决定视觉（``pill-chip`` / ``pill-chip-on``，见 styles.py）。改
    ``objectName`` 之后必须 ``unpolish + polish`` —— 否则 QSS 还按旧名字画，界面上
    会出现「点了没反应」（状态已改、颜色没变）。悬停提示把「隐藏 ≠ 停用」这句话
    直接给用户，免得他以为关掉就等于把这个组件停了。
    """
    label = _SLOT_LABELS[slot]
    btn.setObjectName("pill-chip-on" if visible else "pill-chip")
    btn.setText(label)
    btn.setToolTip(
        f"{label}：正在显示，点一下隐藏（只影响这一处显示，组件照常运行）"
        if visible
        else f"{label}：已隐藏，点一下恢复"
    )
    style = btn.style()
    style.unpolish(btn)
    style.polish(btn)


def _scrolled(content: QWidget) -> QScrollArea:
    """把设置分页放进滚动区。

    窗口主区高度**由用户拖动决定**（2026-09-21 起可缩放，初值 640×520、下限 520×360），
    而页面内容高度由页面自己决定：route_guard 那一页实测近 2900px、插件列表约 426px ——
    不外包滚动区，下半页会连同按钮/手柄一起被裁掉，页面上还看不出有东西被裁了
    （用户报的「控件显示不全、没有滚动条、排布很乱」）。**用户把窗拖小**时同理。
    **所有分页统一包进滚动区**（2026-09-20 UI 统一重构：去掉逐页 scrollable 补丁），
    短页面不触发滚动、长页面必定可达。写法与「插件」页列表那个滚动区同法
    （NoFrame ＋ 关横向滚动 ＋ widgetResizable）。
    """
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setWidget(content)
    return area


def _wire_button(btn, handler, say=None) -> None:
    """设置页按钮接线的**唯一出口**：回调的返回值与异常都出声。

    **为什么要有这一层**：``clicked`` 信号能把回调的返回值丢进黑洞 —— ``connect``
    从不看返回值，回调无论返回 ``ActionResult`` 还是抛异常，界面都纹丝不动。
    此前各页按钮各写一条 ``clicked.connect(...)``，于是「点了没反应」这种事只能靠
    人去数：写槽的是对的，漏掉的是静默的，两边都不报错。收口后只有一条路能接线，
    漏一句 ``say`` 就会立刻在这一层显形。

    三件由它统一负责：

    1. **异常出声**：``FAILED`` 落到 ``say``（``say(text, error=True)``），同时 ``log.exception``。
       此前 Qt 槽里的 Python 异常只打印到 stderr，界面上等于没发生；
    2. **返回值出声**：回调返回 ``ActionResult`` 就照 ``outcome`` 报（``FAILED`` 才算错，
       ``PARTIAL`` 不吓唬人）。空 ``text`` 视为「没话说」，写了也不占位 —— 与
       ``ActionResult`` 契约同口径；
    3. **笔误兜住**：``as_action_result`` 把 ``True``（``Signal.emit`` 的返回值）、
       逗号表达式的元组都规整掉，宿主侧不再靠 ``isinstance`` 猜。

    ``say`` 的签名统一为 ``say(text, *, error=False)`` —— 各页槽位入口不同（``_say_info`` /
    ``_say_error`` / ``_zone_say`` / ``_say_permission``），调用点用 ``lambda`` 换成同一个形状。
    不传 ``say`` 就只记日志，不再往界面写（窗口已销毁的场合）。
    """
    name = getattr(handler, "__name__", None) or repr(handler)

    def _slot(*args, **kwargs):
        try:
            result = as_action_result(handler(*args, **kwargs))
        except Exception:  # noqa: BLE001  槽里不许让异常冒到事件循环
            log.exception("设置页按钮回调抛异常：%s", name)
            if say is not None:
                say(f"「{name}」没做成，请重试。", error=True)
            return
        if result is None or not result.text:
            return
        if say is not None:
            say(result.text, error=result.outcome == ActionOutcome.FAILED)

    btn.clicked.connect(_slot)
