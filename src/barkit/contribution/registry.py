"""contribution/registry：扩展点注册表（方案 v3 §5.1）。

PointRegistry 是「Shell 注册渲染器 + 插件挂载贡献」的薄机制：

- ``register_point(point, renderer)``  Shell 启动时注册（渲染器契约批次 7 定形）
- ``contribute(spec)``                 插件挂载一条贡献（按 point 分桶）
- ``collect(point)``                   Shell 取贡献，已排序、崩溃隔离

只依赖标准库；渲染层按 ``identity()`` 就地更新的协议见方案 §6.3。
"""

from __future__ import annotations

import logging
from typing import Callable

from barkit.contribution.specs import Contribution

log = logging.getLogger(__name__)


class ContributionConflictError(Exception):
    """同 (point, plugin_id, key) 重复挂载，或同 point 多贡献 key 缺失/撞名。"""


class PointRegistry:
    """扩展点注册表：point 字符串 → 贡献列表。

    设计原则：注册表只做「收集 + 排序 + 校验」，不持有任何具体插件类型；
    新增一个 UI 位置 = points.py 加常量 + Shell 注册渲染器，本类零改动。
    """

    def __init__(self) -> None:
        # point -> {identity: spec}。identity 全局唯一，是增量更新的复用键。
        self._by_point: dict[str, dict[str, Contribution]] = {}
        self._renderers: dict[str, Callable] = {}

    # ---- Shell 侧 ----

    def register_point(self, point: str, renderer: Callable) -> None:
        """Shell 启动时登记某扩展点的渲染器（重复注册覆盖并告警）。"""
        if point in self._renderers:
            log.warning("扩展点 %s 重复注册渲染器，已覆盖", point)
        self._renderers[point] = renderer

    def renderer(self, point: str) -> Callable | None:
        return self._renderers.get(point)

    def points(self) -> list[str]:
        """已注册渲染器的扩展点（发现顺序）。"""
        return list(self._renderers)

    # ---- 插件侧 ----

    def contribute(self, spec: Contribution) -> None:
        """挂载一条贡献。key 冲突（同 point 同插件）即启动报错，不静默覆盖。

        - identity 唯一：同 (point, plugin_id, key) 重复挂载 → 拒绝；
        - 同 point 下有多个贡献时 key 必填（单贡献可缺省为 "default"）。
        """
        bucket = self._by_point.setdefault(spec.point, {})
        identity = spec.identity()
        if identity in bucket:
            raise ContributionConflictError(
                f"扩展点 {spec.point!r} 上贡献 identity 重复：{identity!r}"
                "（key 必须在该插件该 point 内唯一）"
            )
        bucket[identity] = spec

    def collect(self, point: str) -> list[Contribution]:
        """取某扩展点的全部贡献，按 (order, identity) 排序。

        插件间顺序由 registry.plugin_order() 决定（§6.4，随批次 6 接线）；
        本排序只保证插件内多贡献的相对顺序稳定。
        """
        bucket = self._by_point.get(point, {})
        return sorted(bucket.values(), key=lambda s: (s.order, s.identity()))

    def identities(self, point: str) -> list[str]:
        return [s.identity() for s in self.collect(point)]

    def snapshot(self) -> dict:
        """贡献树结构快照（门禁 test_spec_snapshot 用）：point -> [identity]。"""
        return {
            point: [s.identity() for s in bucket.values()]
            for point, bucket in sorted(self._by_point.items())
        }
