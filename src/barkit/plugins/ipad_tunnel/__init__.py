"""ipad_tunnel 插件包。

iPad 全流量隧道：整台 Mac 的流量经 iPad 的 Wi-Fi 出口借道转发（全量 /1 默认路由）。
方案见本目录 design.md。

M1 数据层：app 侧经会话**声明隧道意图**（``declare("tunnel", …)``，零授权）
＋ 订阅守护推来的隧道观测刷面板；特权操作走框架的 root helper。

桥接（usbmuxd 探测 + 1080 探活置 link_up）见 bridge.py 的 BridgeController。
守护安装/卸载/状态走 `barkit.capabilities.helper_client`（同一 root helper，一次授权）。
"""
