"""网卡那一块（``net``，§2.5）：四个输入框的搭建 / 装载 / 外观，以及「合进 profile」那一步。

三态的换算不在这里（``settings_netform``）—— 这里是**控件**那一半：跟随系统整块隐藏、形状认不出时整块只读并说明原因、「照抄 DHCP」两枚勾选只在固化租约态出现且勾上即置灰。

``settings.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from barkit.plugins.route_guard import rules
from barkit.shell.ui_kit import check_field, form_label

from .settings_netform import _NET_DRAFT_KEY, _NET_FORM_NONE, _NET_FORM_PIN, _NET_FORM_STATIC, _NET_LEASE_LABEL, _net_from_form, _net_to_form, _net_ui_mode


class _SceneNetMixin:
    """网卡那一块（net，§2.5）的控件与外观。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 网卡配置（`net`，§2.5） ----------

    def _build_net_fields(self) -> QWidget:
        """网卡配置那四个输入框 ＋ 两枚「照抄 DHCP」勾选。整块的显隐归 `_refresh_net_ui`。

        四个框共用一条 ``editingFinished`` → 落盘的路子（同名称 / 网卡那几格）：
        **改一次落一次盘**是本窗体的总口径，网卡这一块不搞例外。

        网关 / DNS **各占一行**（不与 IP / 掩码凑「两行两列」）：行尾那枚勾选是给
        「固化租约」那一态用的，挤在两列里两句话都读不全。
        """
        box = QWidget(self)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self._net_ip = QLineEdit(box)
        self._net_mask = QLineEdit(box)
        self._net_gw = QLineEdit(box)
        self._net_dns = QLineEdit(box)
        self._net_gw_lease = check_field(_NET_LEASE_LABEL, parent=box)
        self._net_dns_lease = check_field(_NET_LEASE_LABEL, parent=box)
        for edit in (self._net_ip, self._net_mask, self._net_gw, self._net_dns):
            edit.editingFinished.connect(self._collect_fields)
        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(form_label("IP", box))
        top.addWidget(self._net_ip, stretch=2)
        top.addWidget(form_label("掩码", box))
        top.addWidget(self._net_mask, stretch=2)
        lay.addLayout(top)
        for text, edit, chk in (("网关", self._net_gw, self._net_gw_lease),
                                ("DNS", self._net_dns, self._net_dns_lease)):
            row = QHBoxLayout()
            row.setSpacing(6)
            row.addWidget(form_label(text, box))
            row.addWidget(edit, stretch=2)
            row.addWidget(chk)
            lay.addLayout(row)
            # 显式固化循环变量（`edit` 两轮各不相同；晚绑定会让两枚勾选都去清同一格）
            chk.toggled.connect(lambda on, e=edit: self._on_lease_toggled(on, e))
        return box

    def _load_net(self, profile: dict) -> None:
        """把网卡那一组装上控件：**有草稿就装草稿**（`_NET_DRAFT_KEY`），否则装配置。

        草稿优先是刻意的：用户填了一半、去看了眼别的场景再切回来，半截输入还在 ——
        它从来没写进过配置，丢了不会有任何人知道。这与「刚新增的场景标未保存」
        是同一条判据（半成品不许悄悄消失）。
        """
        mode = _net_ui_mode(profile.get("net"))
        self._net_locked = mode is None
        draft = profile.get(_NET_DRAFT_KEY)
        values = draft if isinstance(draft, dict) else _net_to_form(profile.get("net"))
        self._net_mode.set_current(values["mode"] if mode is not None else _NET_FORM_NONE)
        self._net_ip.setText(str(values.get("ip") or ""))
        self._net_mask.setText(str(values.get("mask") or ""))
        self._net_gw.setText(str(values.get("gateway") or ""))
        self._net_dns.setText(str(values.get("dns") or ""))
        # 勾选状态也要装（`setChecked` 触发的 `toggled` 会被 `_loading` 挡掉，不会回写）
        self._net_gw_lease.setChecked(bool(values.get("gateway_from_lease")))
        self._net_dns_lease.setChecked(bool(values.get("dns_from_lease")))

    def _refresh_match_ui(self) -> None:
        """``manual_only`` 勾上 ⇒ 生效网段与匹配网关**一起**置灰（§2.6）。

        设计的原话是「生效网段置灰」，这里是它的一般化：「生效网段」与「匹配网关」同属
        ``match``，而 profile 的结构写着「``match`` 仅当 ``manual_only = false`` 时
        有效」。只灰网段会让匹配网关那个框看起来还能用，而它一样不参与判定。
        """
        on = self._editable and not self._manual.isChecked()
        self._subnet.setEnabled(on)
        self._gw.setEnabled(on)

    def _refresh_net_ui(self) -> None:
        """按当前模式（以及「形状认不认得出」）调整网卡那一块的外观：显隐 / 占位 / 说明。

        三条口径写在这里，别处不重述：

        - **跟随系统 ⇒ 整块隐藏**（不是灰掉）：那一态下一根手指都不碰网卡，摆着四格
          空输入框只会让人以为「得填点什么」；
        - **形状认不出 ⇒ 只读 ＋ 明说**：连模式选择器一起禁掉，并把原因讲出来（手改
          YAML 是本窗体给出的唯一出路，见 `_net_ui_mode`）；
        - 占位文案承担**留空是什么意思**：``pin`` 的 IP / 掩码留空 = 照抄 DHCP 拿到的，
          网关 / DNS 留空 = 不固化它们（两者语义不同，helper `_target_of` 同口径）；
        - **「照抄 DHCP」只在固化租约态出现，勾上即把那一格置灰**：它写的是
          ``*_from_lease: true``，与同名写死值互斥（`rules.validate_net` 会拒同时给），
          外观上必须让人一眼看出「这一格此刻不由你填」。
        """
        locked = self._net_locked
        mode = self._net_mode.current()
        self._net_mode.setEnabled(self._editable and not locked)
        self._net_box.setEnabled(self._editable)
        if locked:
            self._net_box.setVisible(False)
            self._net_hint.setText(
                "这份「网络配置」不是本窗体认得的三态之一（模式或字段不对），"
                "为避免误改，这里不显示也不改它 —— 请通过配置文件调整。"
            )
            self._net_hint.setVisible(True)
            return
        self._net_box.setVisible(self._editable and mode != _NET_FORM_NONE)
        self._net_hint.setVisible(True)
        pin_mode = mode == _NET_FORM_PIN
        # 两枚勾选只在固化租约态出现；勾上的那一格置灰（值由 DHCP 决定，不由用户填）。
        # 换到别的一态时**顺手取消勾选**：那一态下压根没有租约可抄（静态 IP 没租约、
        # 跟随系统不碰网卡），留着勾选会出现「框灰着、勾却看不见」——用户既填不进去，
        # 也找不到是谁锁的（曾经就是这样：在固化租约态勾一次，切到静态 IP 后网关框
        # 再也点不动）。取消是程序行为，不该再触发一次「攒改动」，故临时挡掉回调。
        prev_loading = self._loading
        self._loading = True
        try:
            for edit, chk in ((self._net_gw, self._net_gw_lease),
                              (self._net_dns, self._net_dns_lease)):
                if not pin_mode:
                    chk.setChecked(False)
                chk.setVisible(pin_mode)
                edit.setEnabled(self._editable and not chk.isChecked())
        finally:
            self._loading = prev_loading
        if pin_mode:
            self._net_ip.setPlaceholderText("留空 = 用 DHCP 拿到的")
            self._net_mask.setPlaceholderText("留空 = 用 DHCP 拿到的")
            self._net_gw.setPlaceholderText("留空 = 不固化")
            self._net_dns.setPlaceholderText("留空 = 不固化（逗号分隔）")
            self._net_hint.setText(
                "命中后把地址固化下来：IP / 掩码留空 = 照抄 DHCP 刚拿到的那个。"
                "网关 / DNS 留空 = 不固化这两位；要一并固化，填具体地址或勾「照抄 DHCP」。"
                "固化会把网卡切成手动配置、DHCP 随之停止，所以留空不等于继续跟随 DHCP。"
                "路由目标那几行的「下一跳」可以留空 —— 用的就是这里固化下来的网关"
                "（只固化地址时是 DHCP 给过的那个，取到过就记着，重启也不丢）。"
                "离开这个场景后是否还原，由设置页的「无匹配时」决定。"
            )
        elif mode == _NET_FORM_STATIC:
            self._net_ip.setPlaceholderText("如 192.168.21.88")
            self._net_mask.setPlaceholderText("如 255.255.255.0")
            self._net_gw.setPlaceholderText("留空 = 不指定网关")
            self._net_dns.setPlaceholderText("留空 = 不改 DNS（逗号分隔）")
            self._net_hint.setText(
                "命中后写死这个地址（IP / 掩码必填）。写入前会先留一份网卡快照，"
                "万一写坏了自动退回；网关若不在本网段内会被当场拒绝（那种配置会让"
                "这块网卡出不去）。"
            )
        else:
            self._net_hint.setText(
                "不写 `net` —— 用的是系统当前的网络配置，本组件一根手指都不碰。"
                "这一态给不出网关，故路由目标的「下一跳」要自己填（或填「匹配网关」）。"
            )

    def _net_form_values(self) -> dict:
        """窗体此刻的网卡配置（模式 ＋ 四个框 ＋ 两枚照抄勾选），**不管填没填完**都收下来。"""
        return {
            "mode": self._net_mode.current(),
            "ip": self._net_ip.text().strip(),
            "mask": self._net_mask.text().strip(),
            "gateway": self._net_gw.text().strip(),
            "dns": self._net_dns.text().strip(),
            "gateway_from_lease": self._net_gw_lease.isChecked(),
            "dns_from_lease": self._net_dns_lease.isChecked(),
        }

    def _merge_net(self, profile: dict, stored: dict) -> tuple[dict | None, str]:
        """把网卡那一块合进 ``profile``（就地改）。返回 ``(草稿, 还没填完的原因)``：

        - ``(None, "")``：写完了 —— ``profile["net"]`` 已是目标值或已被摘掉；
        - ``(表单取值, 原因)``：**还没填完** —— ``profile["net"]`` 保持 ``stored`` 那一份
          不变，表单取值由调用方存进 `_NET_DRAFT_KEY`（它还没资格进配置）。

        形状认不出时**一个字都不动**（``profile`` 是从 ``stored`` 深拷来的，本来就在）。
        """
        if self._net_locked:
            return None, ""
        form = self._net_form_values()
        net, hold = _net_from_form(form)
        if hold:
            return form, hold
        if net is None:
            # 「跟随系统」**只摘掉真会写网卡的那两种形状**（static / 带 pin 的 dhcp）。
            # 写着 `net: {mode: dhcp}` 时**原样留着**：它与不写等价，但抹掉它就是改用户
            # 的配置 —— 而代价不止那一行：`profiles` 是整表覆盖语义，一次改动会把**合并
            # 后**的整份场景表写进用户 YAML，默认场景从此多出一份第二真源。默认配置里
            # 就写着 `net: {mode: dhcp}`（`config.default.yaml`），故这条不是假想：
            # 少了它，「点一下＋新增」都会把默认配置整份抄进用户的 config.yaml。
            if rules.net_writes(stored.get("net")):
                profile.pop("net", None)
        else:
            profile["net"] = net
        return None, ""

    def _on_net_mode(self, _value: str) -> None:
        """模式一改：先按新态调整外观（占位 / 显隐 / 说明），再当一次改动落盘。

        次序不能反：刷新要先把该出现的输入框**露出来**，用户才填得下去；而落盘那一步
        在「静态 IP 还没填地址」时只会挂起并在行内说明（`_net_from_form`）。
        """
        if self._loading:
            return
        self._refresh_net_ui()
        self._collect_fields()

    def _on_lease_toggled(self, checked: bool, edit: QLineEdit) -> None:
        """勾 / 取消「照抄 DHCP」：勾上先把同一行的输入框**清空**，再按新态调整外观、攒改动。

        清空是**必须**的：配置里「照抄租约」与「写死值」互斥（`rules.validate_net` 会拒
        同时给），留着旧值等于同时给了两个答案 —— 那会在**保存**那一刻才报错，而用户刚
        看到的是自己勾了个选项。把它当场清掉，「哪一边说了算」就是看得见的事实。
        """
        if self._loading:
            return
        if checked:
            edit.clear()
        self._refresh_net_ui()
        self._collect_fields()

    def _on_manual_toggled(self, _checked: bool) -> None:
        """勾上「仅手动」⇒ 先把 `match` 两格置灰（让用户当场看见它们不参与判定），再落盘。"""
        self._refresh_match_ui()
        self._collect_fields()
