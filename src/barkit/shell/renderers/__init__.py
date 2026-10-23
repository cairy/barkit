"""shell/renderers：UI 派生层（UI 规范 §8.3 落点）。

只放「从 Spec 声明 + 快照派生渲染决策」的纯函数：排序聚合、徽标态、
代表态。核心层（core/）不知道 UI 的排序与聚合语义；本包不知道 Qt——
渲染原语在 ``shell/status_item.py`` / ``shell/ui_kit.py``（U2 轮次）。
"""
