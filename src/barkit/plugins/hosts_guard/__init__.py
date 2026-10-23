"""hosts_guard 插件包。

维护 /etc/hosts 里的「BarKit 托管块」：让指定域名绕过隧道的 fake IP 机制、
解析到真实 IP，并可选跟随 DDNS 自动更新。

数据流（与 route_guard / ipad_tunnel 完全一致，2026-09-29 通道换成会话）：
  app 声明意图 ``intent.set("hosts", …)`` → root helper 按调度收敛
  → 校验 + 落盘标记块写回 /etc/hosts → ``event.push("hosts")`` → 面板
  （中间**没有文件**：从前的 hosts_rules.json / .trigger / hosts_state.json 都删了。）

特权操作复用 route_guard 的 root helper（同一 LaunchDaemon，一次授权）。

⚠️ 常量须与 ``route_guard/helper.py`` 的 hosts 段保持一致：helper 是被复制成
单文件由 /usr/bin/python3 运行的，无法 import 本包，故两侧各持一份定义。
改动 METHOD_* / BLOCK_BEGIN / BLOCK_END 时务必同步修改 helper.py。
"""
