"""ipad_tunnel/settings：设置面 —— 设置窗里的「iPad 隧道」页 ＋ 点组件进入的运行诊断窗体。

**参考《录屏通知》重构后的分工**（2026-09-18）：

- **设置页（本文件 ``build_page``）**：自动化开关 ＋ 建隧道模式 ＋ 隧道配置 ＋ 装置运维。
  这四样都是「配一次就不再动」的东西，与《录屏通知》把「自动化总闸 ＋ 规则编辑器 ＋
  装置事实」收进设置页同一条判据；
- **详情页（本文件 ``build_diag_page``）**：**点面板那一行直接进运行诊断**。诊断是
  「每次用都要跑一遍」的高频任务流（§5.4），它取代了原先那个「安装 / 卸载 / 立即检查 /
  运行诊断」四合一的详情页 —— 那四件事里三件已归设置页，剩下的一件就是这一页本身。
  壳由框架 ``DetailForm`` 托管（``presentation="window"``：原生标题栏、可缩放、居中、
  页脚「完成」、Dock 图标收归框架），本模块只 ``build()`` 内容 —— 不再自持 ``QDialog``。

**为什么删掉 ``config_dialog.py``**（原 ``TunnelConfigDialog`` / ``TunnelDiagnosticDialog``）：

1. 它自带 4 处 ``QMessageBox``（保存失败 / 配置不合法 / 输入有误 ×2）—— 从**置顶窗内部**
   弹模态框等于再开一条嵌套事件循环，实测会「点了没反应 → 再点一次 → 窗口卡死」
   （``ui_kit.FeedbackBar`` 的类 docstring 有崩溃报告）；
2. 它是**脱离贡献体系**的平行入口（设置页一个按钮开一个私有对话框），而 ``DetailEntrySpec``
   已经给了同一件事的托管路径；
3. 《插件结构规范-UI面》§7.2：设置面（设置页 ＋ 详情页）一律归 ``settings.py``。

本模块的纪律（同 ``notify_guard/settings.py`` / ``route_guard/settings.py``）：

- **不碰 ``ctx`` / ``config``**：读走 ``plugin.tunnel_settings()``、写走
  ``plugin.save_tunnel_settings()`` —— 配置 IO 全在 ``plugin.py``（门禁
  ``test_no_config_io_in_ui``）；本模块不知道自己属于哪个插件，也就无从读它的配置；
- **零模态框**：一切反馈与二次确认走行内 ``FeedbackBar``（卸载的二次确认由插件经
  ``plugin._say_*`` / ``fb.ask`` 落到本页那一条槽里）；
- **不自建时钟**：诊断那一条 ``QThread`` 是「用户点了才跑、随页面销毁」的 UI 内部交互，
  已在 ``PLUGIN_TIMER_WHITELIST`` 挂账并写明理由，不是后台常驻；
- 外壳统一用 ``shell.ui_kit`` 的 ``settings_page_shell`` / ``settings_section`` /
  ``Switch`` / ``ChoiceSelector`` / ``FeedbackBar`` / ``note`` / ``tool_button``；
  本页四组控件收进**三张分区卡**（自动化 / 隧道配置 / 装置运维）—— 设置页里每一组控件
  都长在卡里、卡外不留裸行（2026-09-21 的返工口径，同 ``hosts_guard`` / ``recognize``）。

**改一次落一次盘、没有「保存」键**（《录屏通知》§7 第 1 条口径）：外层壳自带「完成」，
再过一道保存会让用户以为改过了 —— 那个陷阱比多写几次 YAML 贵得多。端口两格用
``editingFinished``（打字途中的中间值不该落盘）。
"""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from barkit.core import thread_reap
from barkit.plugins.ipad_tunnel.diagnostics import (
    FAIL,
    INFO,
    PASS,
    WARN,
    DiagResult,
    run_diagnostics,
)
from barkit.shell import styles
from barkit.shell.design import metrics
from barkit.shell.ui_kit import (
    ChoiceSelector,
    FeedbackBar,
    NumberField,
    Switch,
    check_field,
    form_row,
    note,
    settings_page_shell,
    settings_section,
    tool_button,
)

if TYPE_CHECKING:
    from barkit.plugins.ipad_tunnel.plugin import Plugin

#: 建隧道模式的取值（``ChoiceSelector`` 按**取值**选中，显示名只是标签 —— 改名不该让
#: 已存下的值失效）。落到配置上就是 ``ignore_link`` 一位（True = 手动）。
MODE_AUTO = "auto"
MODE_MANUAL = "manual"


def _tokens() -> dict:
    """当前主题令牌（设置窗每次打开重建，故构建期取色即可）。"""
    return styles.TOKENS[styles.current_theme()]


def build_page(plugin: "Plugin") -> QWidget:
    """设置页（框架按 ``SettingsPageSpec.build()`` **零参**调用，故插件侧用闭包注入）。

    三张分区卡：「自动化」（总闸 ＋ 建隧道模式）、「隧道配置」（三个劫持开关 ＋ 端口两格）、
    「装置运维」（立即检查 / 安装 / 卸载 ＋ 守护警示）。分组是 2026-09-21 的返工口径 ——
    卡外的裸行没有归属，用户看不出哪几行是一件事。
    """
    page, layout = settings_page_shell("iPad 隧道")

    # 行内反馈槽：本页所有动作（保存配置 / 安装卸载守护）的结果与二次确认都落在这里。
    # 槽位的生死由插件管（页面销毁时自动摘引用，见 `Plugin.attach_feedback`）。
    # **必须进布局**（2026-09-21 修）：此前只建了槽没往布局里放 ⇒ 它 `_say` 亮起时
    # 没有归属位置，会浮在页面左上角压住标题。
    fb = FeedbackBar(page)
    plugin.attach_feedback(fb)
    layout.addWidget(fb)

    # ── 1. 自动化（隧道总闸 ＋ 建隧道模式）──
    # 总闸它原来是面板那一行右侧的 Switch，搬到设置页的理由与《网络切换》第 7b 步一致：
    # 低频 ＋ 有代价（一开就动整机网络），代价说明必须摆在开关旁边。
    # 「建隧道模式」（原面板徽标「自动 / 手动」）同属「配一次就不再动」，两块合成一张卡。
    auto_card, auto_lay = settings_section("自动化", parent=page)
    gl = QHBoxLayout()
    gl.setSpacing(8)
    # 行标签只说这枚开关管什么（「自动化」这三个字已经由卡标题给了，别重复一遍）
    gl.addWidget(QLabel("自动建隧道", auto_card), stretch=1)
    sw = Switch(auto_card)
    sw.setChecked(plugin.automation_active)
    sw.clicked.connect(lambda: _on_toggle_automation(plugin, sw))
    gl.addWidget(sw)
    auto_lay.addLayout(gl)
    auto_lay.addWidget(note(
        "打开后：iPad 一连上就自动建隧道；按下面的「劫持」开关决定是否把整台 Mac 的流量"
        "经 iPad 转发。关掉后隧道不再自动建立，已生效的会撤掉。",
        auto_card,
    ))

    sel = ChoiceSelector(
        [("自动（等 iPad 链路就绪）", MODE_AUTO),
         ("手动（不等链路，直接尝试）", MODE_MANUAL)],
        MODE_MANUAL if plugin.ignore_link else MODE_AUTO,
        parent=auto_card,
    )
    sel.selected.connect(lambda v: plugin.set_ignore_link(v == MODE_MANUAL))
    auto_lay.addLayout(form_row("建隧道模式", sel, auto_card))
    auto_lay.addWidget(note(
        "「手动」= helper 不等链路信号就去建隧道（iPad 端已在跑、但 USB 偶发掉线时用）；"
        "上游不通时它一样不会拉起一条流量黑洞。",
        auto_card,
    ))
    # iOS 端签名到期（2026-10-09）：iPad 端 App 个人证书 7 天一签，过期即无法启动。
    # 信息经 usbmuxd 从 iPad 端通告拉来（announce.py）。**这行交给组件续写，不在构建期
    # 定格**：设置窗是启动期构建一次的（shell/app_shell 装配时），那一刻通告还没拉到
    # （探针首翻要 ≈6~12s）—— 「读一次」会让这行整个会话停在假话「未通告」上。
    # 未通告时如实说「未通告」，不拿 0 天顶替。
    announce_lbl = QLabel(plugin.announce_text(), auto_card)
    plugin.attach_announce_label(announce_lbl)
    auto_lay.addLayout(form_row("iOS 端签名", announce_lbl, auto_card))
    layout.addWidget(auto_card)

    # ── 2. 隧道配置 ──
    cfg_card, cfg_lay = settings_section("隧道配置", parent=page)
    cfg = plugin.tunnel_settings()

    def _check(text: str, key: str, tooltip: str = "") -> QCheckBox:
        box = check_field(
            text,
            checked=bool(cfg.get(key, False)),
            # 新值直接进 payload —— 不必绕 `box.isChecked()` 回读（见 `check_field`）。
            on_toggled=lambda on, k=key: plugin.save_tunnel_settings({k: on}),
            parent=cfg_card,
        )
        if tooltip:
            box.setToolTip(tooltip)
        return box

    # 默认不勾：隧道不通时全局劫持 = 整机断网，属**不对称**风险（旁路模式下隧道照样建，
    # 可先用 `dig @198.18.0.2` 验证隧道本身通不通，确认可用后再勾）。
    cfg_lay.addWidget(_check(
        "激活时禁用 Mac Wi-Fi 的 IPv6（fail-closed，防泄漏）",
        "disable_ipv6_on_activate",
    ))
    cfg_lay.addWidget(_check(
        "劫持全量 IPv4 路由（/1，整台 Mac 流量经 iPad 转发）",
        "hijack_route",
        "隧道不通时勾选此项会导致整台 Mac 断网。建议先用\n"
        "`dig @198.18.0.2 example.com` 确认隧道可用后再开启。",
    ))
    cfg_lay.addWidget(_check(
        "劫持系统 DNS（改 198.18.0.2，经 iPad 侧递归解析）",
        "hijack_dns",
        "必须与上一项同时开启：只有 DNS 走 mapdns，hev 才能用域名\n"
        "向上游发起 CONNECT（纯 IP 会被 iPad 端拒绝）。",
    ))

    plocal, pdevice = _port_rows(plugin, cfg, cfg_card)
    cfg_lay.addLayout(plocal)
    cfg_lay.addLayout(pdevice)
    cfg_lay.addWidget(note(
        "Wi-Fi 切换（激活 / 恢复 SSID）不再在这里配 —— 组件不托管密码，切网络是靠 helper"
        "按本机已配过的网络名做的，这一项留给用户手改 YAML（`wifi.on_activate` / "
        "`wifi.on_deactivate`），界面不为一个低频且易误伤的动作留入口。",
        cfg_card,
    ))
    layout.addWidget(cfg_card)

    # ── 3. 装置运维**已不在这里**（2026-09-28）──
    # 「立即检查 / 安装守护 / 卸载守护 ＋ 那句守护警示」现在由**框架按声明注入**：
    # 本组件 `resources().helper_tasks` 非空 ⇒ 框架在本页**页尾框架区**画出第 ③ 张卡
    # （卡序契约：运行间隔 → 配置维护 → 装置运维）。文案在 `plugin.helper_ops_texts()`，
    # 动作与接线在 `capabilities.helper_ops`（与网络切换 / hosts 托管共用一份实现），
    # 那句「点面板那一行进运行诊断」的小字也在那儿（`HelperOpsTexts.note`）。
    # 搬走前那句注释说的「共用同一块」其实只到「共用画法」为止 —— 接线仍是各写各的。

    layout.addStretch(1)
    return page


def _on_toggle_automation(plugin: "Plugin", sw: Switch) -> None:
    """隧道总闸：Switch 只有 ``clicked``、不自动翻转自身，故推出目标态再回读同步。"""
    plugin.set_automation_active(not sw.isChecked())
    sw.setChecked(plugin.automation_active)


def _port_rows(plugin: "Plugin", cfg: dict, parent: QWidget) -> "tuple[QHBoxLayout, QHBoxLayout]":
    """本机 / iPad 端口两行（改完即写；打字途中的中间值不落盘）。"""

    def _row(label: str, key: str, fallback: int) -> QHBoxLayout:
        box = NumberField(parent)
        box.setRange(1, 65535)
        box.setValue(int(cfg.get(key, fallback) or fallback))
        # ``editingFinished`` 而非 ``valueChanged``：后者在打字途中每个中间数字都写一次盘
        # （端口是 1..65535，中间值完全可能是非法组合）。
        box.editingFinished.connect(
            lambda: plugin.save_tunnel_settings({key: box.value()})
        )
        return form_row(label, box, parent)

    return (_row("本机端口", "local_port", 1080),
            _row("iPad 端口", "device_port", 9001))


# ──────────────────────────────────────────────
# 运行诊断（点面板那一行进入；壳由框架 DetailForm 托管）
# ──────────────────────────────────────────────

def _valid_ipv4(value: str) -> bool:
    return bool(re.match(r"^\d{1,3}(\.\d{1,3}){3}$", value)) and all(
        0 <= int(p) <= 255 for p in value.split(".")
    )


class _DiagWorker(QThread):
    """后台跑诊断，逐步发射 DiagResult 让 UI 流式刷新（避免网络探测卡 UI）。

    UI 触发、随页面销毁，非后台常驻 —— 已在 ``PLUGIN_TIMER_WHITELIST`` 挂账。
    """

    step_ready = Signal(DiagResult)
    finished = Signal()

    def __init__(self, test_ip: str, test_domain: str, hop_provider=None,
                 state_provider=None, tunnel_provider=None) -> None:
        super().__init__()
        self._ip = test_ip
        self._domain = test_domain
        #: ``() -> str | None``：探针的阶梯结论（最浅失败层）。**现问**，不在构造时取
        #: 值 —— 诊断是跑起来那一刻的事，构造与开跑之间探针随时可能翻结论。
        self._hop_provider = hop_provider
        #: ``() -> dict``：守护推来的那份观测 / 当前声明的意图正文。两者都**现问**，
        #: 诊断要与实时命令输出交叉印证，取构造期那份会慢一个组件节拍。
        self._state_provider = state_provider
        self._tunnel_provider = tunnel_provider

    def run(self) -> None:
        # ``finished`` 走 ``finally``：**任何**出口都要发 —— 代持方靠它回收本线程
        # （不发就等于永不回收，虽然不会崩，但线程对象一直挂着）。
        try:
            hop = self._hop_provider() if callable(self._hop_provider) else None
        except Exception:  # noqa: BLE001 —— 拿不到结论就按「没结论」展示，不拖垮整场诊断
            hop = None
        try:
            state = self._state_provider() if callable(self._state_provider) else None
        except Exception:  # noqa: BLE001
            state = None
        try:
            tunnel = self._tunnel_provider() if callable(self._tunnel_provider) else None
        except Exception:  # noqa: BLE001
            tunnel = None
        try:
            for result in run_diagnostics(self._ip, self._domain, hop=hop,
                                          state=state, tunnel=tunnel):
                # 关窗只要求「别再往下跑」：中断标志只在**步与步之间**查得到，
                # 正卡在某一条外部命令上时要等那一步走完（它自带超时）。
                if self.isInterruptionRequested():
                    return
                self.step_ready.emit(result)
        except Exception as e:  # noqa: BLE001
            self.step_ready.emit(DiagResult(
                0, "诊断异常", "", str(e), FAIL, "诊断过程抛出异常，已中止。"))
        finally:
            self.finished.emit()


class _DiagPane(QWidget):
    """诊断内容：测试 IP / 域名 → 逐步跑 6 步「命令 / 输出 / 评估」。

    所有命令**只读**、无需 root（路由表 / DNS / curl / dig / ifconfig 均用户态可跑），
    故它可以在 app 进程内直接驱动，不必走 root helper。

    **零模态框**：输入不合法之类的提示走页内 ``FeedbackBar``（原先是
    ``QMessageBox.warning``，在置顶的框架窗体里会开嵌套事件循环）。
    """

    #: 状态 → 令牌名。**不写死颜色字面量**：原先那四个值（`#30d158` / `#ff453a` /
    #: `#ff9f0a` / `#8e8e93`）是照抄**深色**令牌抄来的，于是浅色主题下用的是深色的
    #: 绿 / 红 / 橙 / 灰 —— 值看着对、主题其实是错的。取令牌名后按当前主题现取
    #: （门禁 `test_no_hardcoded_colors` 守这一条）。
    #: 三色走**前景档**：`green` / `red` / `orange` 是图形档（压白图形用的），
    #: 当字写在白底上只有 2.01 / 3.55 / 1.87。`INFO` 走 `text2` 而不是 `text3`
    #: （后者是装饰档，浅色下 3.26）。
    _STATUS_TOKEN = {
        PASS: "green_text", FAIL: "red_text", WARN: "orange_text", INFO: "text2",
    }
    # 步骤标题用**文字**标状态，不用 emoji：① 图标一律走 `shell/design/icons.py`
    # （这条由 `test_no_emoji_in_source` 守）；② 这里输出的是 HTML 片段，塞矢量图标
    # 得绕 data URI；③ 颜色只染「评估」那一行，标题用文字标一次，色觉障碍用户也读得出。
    _STATUS_LABEL = {PASS: "通过", FAIL: "失败", WARN: "注意", INFO: "提示"}

    def __init__(self, parent: QWidget | None = None, hop_provider=None,
                 state_provider=None, tunnel_provider=None) -> None:
        super().__init__(parent)
        #: ``() -> str | None``：探针的阶梯结论（见 ``diagnostics.run_diagnostics`` 的
        #: ``hop``）。不传也能跑 —— 第 1 步就退回「只说搬不动、不指认段位」。
        self._hop_provider = hop_provider
        #: ``() -> dict``：观测与意图（同上，现问现取）。
        self._state_provider = state_provider
        self._tunnel_provider = tunnel_provider
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        self._ip = QLineEdit("8.8.8.8", self)
        self._ip.setPlaceholderText("用于路由测试的公网 IPv4（如 8.8.8.8）")
        lay.addLayout(form_row("测试 IP", self._ip, self))
        self._domain = QLineEdit("example.com", self)
        self._domain.setPlaceholderText("用于 DNS 测试的域名（如 example.com）")
        lay.addLayout(form_row("测试域名", self._domain, self))

        row = QHBoxLayout()
        row.setSpacing(8)
        self._run = tool_button("运行诊断", "primary", parent=self)
        self._run.clicked.connect(self._on_run)
        row.addWidget(self._run)
        row.addStretch(1)
        lay.addLayout(row)

        self._fb = FeedbackBar(self)
        lay.addWidget(self._fb)

        lay.addWidget(note(
            "诊断只读不改系统：探测 iPad 上游、utun、路由表、系统 DNS 与解析结果。"
            "评估着色：绿=正常 / 红=异常 / 橙=运行与配置不一致 / 灰=旁路（符合预期）。",
            self,
        ))

        self._out = QTextEdit(self)
        self._out.setReadOnly(True)
        self._out.setFont(QFont("Menlo", 11))
        self._out.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        lay.addWidget(self._out, 1)

        self._worker: "_DiagWorker | None" = None
        self.destroyed.connect(lambda *_a: self._stop_worker())

    def _on_run(self) -> None:
        ip = self._ip.text().strip()
        domain = self._domain.text().strip()
        if not _valid_ipv4(ip):
            self._fb.error("测试 IP 必须是合法 IPv4 地址。")
            return
        if not domain:
            self._fb.error("测试域名不能为空。")
            return
        self._fb.dismiss()
        self._out.clear()
        self._run.setEnabled(False)
        self._worker = _DiagWorker(ip, domain, hop_provider=self._hop_provider,
                                   state_provider=self._state_provider,
                                   tunnel_provider=self._tunnel_provider)
        self._worker.step_ready.connect(self._append_step)
        self._worker.finished.connect(self._on_done)
        self._worker.start()

    def _append_step(self, r: DiagResult) -> None:
        t = _tokens()
        color = t[self._STATUS_TOKEN.get(r.status, "text2")]
        label = self._STATUS_LABEL.get(r.status, self._STATUS_LABEL[INFO])
        self._out.append(
            f"<b>{label} {r.step}. {html.escape(r.title)}</b><br>"
            f"<span style='color:{t['text3']}'>命令：</span>"
            f"<pre style='margin:{metrics.MARGIN_XS}px 0;white-space:pre-wrap'>{html.escape(r.command)}</pre>"
            f"<span style='color:{t['text3']}'>输出：</span>"
            f"<pre style='margin:{metrics.MARGIN_XS}px 0;white-space:pre-wrap'>{html.escape(r.output)}</pre>"
            f"<span style='color:{color}'>评估：{html.escape(r.detail)}</span>"
        )
        self._out.append("")  # 步骤间空行

    def _on_done(self) -> None:
        self._run.setEnabled(True)
        self._out.append("<b>诊断完成。</b>")

    def hideEvent(self, event) -> None:  # noqa: N802
        """关窗即收线程（框架壳是 hide 而非 delete，只挂 ``destroyed`` 收不到这一次）。"""
        self._stop_worker()
        super().hideEvent(event)

    def _stop_worker(self) -> None:
        """页面销毁 / 关窗时收线程（诊断只读，但留一条还在探测的线程没有意义）。

        **两步都不能省，顺序也不能换：**

        1. 先摘掉指向本页的槽 —— 本页随后就销毁，留着等于往一个已死的 C++ 对象上发信号；
        2. 再交 ``thread_reap`` —— 它保证「线程还在跑时绝不析构它」。

        第 2 步是 2026-09-29 那次崩溃的正面：原先这里写的是 ``quit()`` + ``wait(1000)``，
        而 ``run()`` 顺序跑命令、**没有事件循环** ⇒ ``quit()`` 完全无效，一秒后引用一丢，
        ``~QThread()`` 在线程仍运行时执行 ⇒ Qt ``qFatal`` ⇒ ``abort()``，整个 app 当场死。
        所以「要求它停」与「别提前析构它」是两件事，这里一件都不许省。
        """
        worker = self._worker
        if worker is None:
            return
        self._worker = None
        for signal, slot in ((worker.step_ready, self._append_step),
                             (worker.finished, self._on_done)):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):   # 已经断过 / 对象在销毁途中
                pass
        if thread_reap.stop(worker):
            worker.deleteLater()


def build_diag_page(hop_provider=None, state_provider=None, tunnel_provider=None) -> QWidget:
    """运行诊断窗的**内容**（壳由框架 ``DetailForm`` 给，见模块 docstring）。

    ``hop_provider`` 由插件给（``() -> str | None``）：第 1 步据此点名卡在哪一段。
    ``state_provider`` / ``tunnel_provider``（``() -> dict``）：守护推来的观测与当前
    声明的意图，交主流程做「运行/配置一致」的交叉印证。三者都由插件现问现取。
    """
    return _DiagPane(hop_provider=hop_provider,
                     state_provider=state_provider,
                     tunnel_provider=tunnel_provider)
