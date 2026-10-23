"""``_SettingsDataMixin`` —— 设置页的数据面与它那条行内反馈槽（UI 只画，配置 IO 留在这里）。

``plugin.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from barkit.plugins.ipad_tunnel.tunnel_spec import TunnelConfigError

from .constants import log


class _SettingsDataMixin:
    """设置页的数据面与它那条行内反馈槽（UI 只画，配置 IO 留在这里）。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 设置面（settings.py）的数据面（§7.2：UI 只画，配置 IO 留在这里）----------

    def tunnel_settings(self) -> dict:
        """设置页要显示的那几个键（**有效配置**：默认 ＋ 用户差异已合并）。

        读起点是合并后的整段，而不是用户差异段 —— 差异段会让新装用户看到空表，
        而界面外明明写着某条在生效。
        """
        return {
            "disable_ipv6_on_activate": bool(self.cfg.value("disable_ipv6_on_activate")),
            "hijack_route": bool(self.cfg.value("hijack_route")),
            "hijack_dns": bool(self.cfg.value("hijack_dns")),
            "local_port": int(self.cfg.value("local_port")),
            "device_port": int(self.cfg.value("device_port")),
        }

    def save_tunnel_settings(self, patch: dict) -> bool:
        """写回设置页改动的键 → 重声明意图。零授权。

        **只写 ``patch`` 里给出的键，不整段覆盖**：本页不显示的键（``wifi.on_activate`` /
        ``dns`` / ``ignore_link`` …）用户可能手改过，界面不该替他删掉（同《网络切换》
        「用户手写的键一律原样保留」）。写盘失败与校验失败**分别**说话：前者是「没存下」，
        后者是「存下了但没生效」，给用户看的是两种不同处境。

        **不必额外「叫一轮」**：本页改的每一个键（端口 / 劫持开关）都在意图正文里 ⇒
        内容一变，声明发出去，编排层立刻把它排成到期。不这么做的话 helper 要等最长
        5 秒的下一轮轮询才重读，造成「改了但路由/DNS 没变」的假象（hijack_* 这类运行期
        可切换的开关尤其明显 —— 用户改完立刻 `route -n get 8.8.8.8` 验证会看不到变化）。
        """
        try:
            self.cfg.update(patch)
        except Exception as e:  # noqa: BLE001
            log.error("写隧道配置失败：%s", e)
            self._say_error("保存失败——写回配置没成功，请重试。")
            return False
        try:
            self.sync_tunnel()
        except TunnelConfigError as e:
            self._say_error(f"配置未生效：隧道意图没发出去（{e}）")
            return False
        # 端口变了不用重算服务集合：中继 / 探针每轮自己重读配置（桥接在同一循环内
        # 「关旧 → 开新」收敛），这里只让 helper 立刻应用新的劫持开关。
        self.publish_snapshot()
        return True
