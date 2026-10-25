# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包规格：把 BarKit 打成 macOS 的 BarKit.app（菜单栏常驻）。

要点：
- 插件是**动态装配**的，PyInstaller 静态分析看不到 → 用 ``collect_submodules``
  显式收集 ``barkit`` 全部子模块，否则打出来的 App 缺插件。
- 插件的外部资源（尤其是 ``plugins/ipad_tunnel/resources/hev-socks5-tunnel``）
  通过 ``collect_data_files("barkit")`` 一并收进包里；运行期
  ``rootbin_reconcile.package_dir()`` 用 ``inspect.getfile`` 定位包目录，
  在 onedir 包里指向 ``_MEIPASS``，故能正确找到 resources。
- 仓库根 ``config.default.yaml`` 不在包内，用 datas 放到 ``_MEIPASS`` 根，
  并配合 ``core/config/consts.py`` 的 frozen 分支定位。
"""
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# 仓库根（本 spec 位于 <root>/packaging/）
ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = collect_data_files("barkit")
datas += [(os.path.join(ROOT, "config.default.yaml"), ".")]

hiddenimports = collect_submodules("barkit")
hiddenimports += [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtNetwork",
    "objc",
    "Foundation",
    "AppKit",
    "Quartz",
    "ApplicationServices",
]

a = Analysis(
    [os.path.join(ROOT, "packaging", "launch.py")],
    pathex=[os.path.join(ROOT, "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BarKit",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="BarKit",
)

app = BUNDLE(
    coll,
    name="BarKit.app",
    icon=None,
    bundle_identifier="com.cairy.barkit",
    info_plist={
        "LSUIElement": True,          # 菜单栏 App：不出现在 Dock / Cmd-Tab
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
    },
)
