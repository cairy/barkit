#!/bin/zsh
# 重新构建 BTHelper.app。
#
# 什么时候要跑：改了 helper/bt-helper.m 或 helper/Info.plist 之后。
# **普通使用者不需要跑它** —— 仓库里带了一份编译好的 .app，装完就能用；
# 这个脚本是给「要改助手」的人准备的。前提是本机有 Xcode 命令行工具（clang /
# codesign 都来自它）。
#
# 为什么产物必须入库、而不是安装时现编：现编要求**每一台**目标机器都有 clang，
# 而这个助手跟 hev 那个第三方二进制是同一类东西（随插件分发的可执行文件），
# 仓库里已经有它的先例（plugins/ipad_tunnel/resources/hev-socks5-tunnel）。
set -e
cd "$(dirname "$0")"

APP="BTHelper.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"

echo "==> 编译 bt-helper"
clang -fobjc-arc -O2 \
    -framework Foundation -framework IOBluetooth \
    -o "$APP/Contents/MacOS/bt-helper" helper/bt-helper.m

echo "==> 装配 Info.plist"
cp helper/Info.plist "$APP/Contents/Info.plist"

echo "==> adhoc 签名（不是可选项：没有它，蓝牙私有 API 会 SIGABRT）"
codesign -f -s - "$APP"
codesign -dv "$APP" 2>&1 | grep -E "Identifier|Signature|Format" || true

echo "==> 产物"
ls -la "$APP/Contents/MacOS/bt-helper"
du -sh "$APP"
echo
echo "改完记得核对一遍："
echo "  1) 离屏用例（不碰蓝牙，验的是 app 侧那一半会话实现）："
echo "     PYTHONPATH= .venv/bin/python -m pytest tests/test_bt_guard.py -k session"
echo "  2) 真机只读核对（起一次助手、读当前电源状态，不改任何东西）："
echo "     PYTHONPATH=src .venv/bin/python -c \\"
echo "       \"from barkit.plugins.bt_guard import bt_state, bt_session as s; \\"
echo "        print(s.run_commands(['power'], spawn=bt_state.spawn_helper))\""
