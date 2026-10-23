"""barkit.shell：L3 界面骨架层（方案 v3 §3 / §4）。

Shell 独占渲染权：主题、圆角、间距、状态色、图标、字体、hover 态全部由 Shell
决定；插件提供的数据里不得包含任何视觉属性。

批次 0 落 ``shell/design/``（设计令牌真源 + 语义图标 + 状态色映射）；
批次 7 把原 ``ui/`` 整体并入本包——``panel`` / ``settings`` / ``status_item`` /
``styles`` 直接迁入，``ui/widgets.py`` → ``ui_kit.py``（组件库），``ui/`` 不再存在；
入口接线同步拆出 ``app_shell.py``。``ui_kit`` 于 2026-09-30 由单文件拆成**包**
（11 个子模块），``__init__`` 全量再导出 ⇒ 调用方写法不变。

``renderers/``（row / chip / form / action / badge）仍待 UI 规范那一轮落地。
"""
