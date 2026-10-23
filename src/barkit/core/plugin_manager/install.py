"""安装与覆盖（预检 / 原子换目录 / 装完起新实例 / 重扫发现）＋ 两枚数据类。

``plugin_manager.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .consts import log


def version_label(version: str) -> str:
    """版本号 → 给人看的一小段（契约决议 D5：**空串是常态** —— 内置组件与老清单都没有）。

    不编造 ``"0.0.0"``：不知道就说不知道，免得提示里出现一个看起来确定的版本。
    设置为公开名是因为**设置页的覆盖提示与安装结果共用这一句**，两处各写一份必然漂移。
    """
    return f"v{version}" if version else "版本未标注"


@dataclass(frozen=True)
class InstallPreview:
    """安装预检结果（决议 D5）：界面据此措辞「安装 / 覆盖，从哪个版本到哪个版本」。

    ``installed_version`` 三态：``None`` = 首次安装；``""`` = 已装但版本未知（老包没写
    ``version``）；非空 = 已装的版本。**三态必须区分** —— 折成两态会让提示对老包说谎。
    """

    plugin_id: str
    version: str                      # 传入包的版本（空串 = 未标注）
    installed_version: "str | None"


class _InstallMixin:
    """安装与覆盖：预检（认身份、比版本）/ 原子换目录 /装完起新实例 / 重扫发现。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 安装 / 覆盖（仅外部，决议 D5） ----------

    def preview_install(self, path) -> "tuple[InstallPreview | None, str]":
        """安装预检：认出「要装谁、什么版本、机器上现在是什么版本」，**不写任何盘**。

        确认归调用方（界面）：行内反馈条的「确认 / 取消」是**异步**的（点下按钮之后
        才落地），而编排层的确认回调只能是同步的 —— 两者拼不到一起。故这里只把事实
        交出去，由界面在问完之后再调 ``install()``。压缩包在这一步会被解一次到临时
        目录（算完即清理），真正的安装会再解一次；包小、路径本地，这点重算换一个
        干净的接口值得。
        """
        from barkit.core import installer

        with installer.open_package(path) as (pkg_dir, reason):
            if pkg_dir is None:
                return None, reason
            ident, reason = installer.read_package_identity(pkg_dir)
            if ident is None:
                return None, reason
            installed = installer.read_installed_version(Path(self._external_base), ident.plugin_id)
        return (
            InstallPreview(
                plugin_id=ident.plugin_id,
                version=ident.version,
                installed_version=installed,
            ),
            "",
        )

    def install(self, path) -> "tuple[bool, str]":
        """安装 / 覆盖外部插件。``path`` 可以是插件**目录**，也可以是 **zip 压缩包**。

        流程：备源（压缩包解到临时目录）→ 认身份（读清单 id / 版本，**不 import 代码**）
        → 停旧实例 → 底层原子安装（预检 → 换目录 → 激活，失败回滚）→ 重扫发现 →
        按原启用态起新实例 → 通知界面。

        三条不变量：
        - **失败不留半装状态**（底层保证：预检失败不动原目录、激活失败恢复旧版）；
        - **覆盖确认不归本层**（见 ``preview_install``）—— 调用方问过用户再调这里；
        - **版本号只影响提示措辞，不进任何门禁**（契约决议 D5）。
        """
        from barkit.core import installer

        base = Path(self._external_base)
        with installer.open_package(path) as (pkg_dir, reason):
            if pkg_dir is None:
                return False, reason
            ident, reason = installer.read_package_identity(pkg_dir)
            if ident is None:
                return False, reason
            pid = ident.plugin_id
            # 护栏：外部包不得顶替内置插件。发现层是「外部同名者覆盖内部」，默许它
            # 会让用户以为自己改的是内置插件（卸载外部包后内置的又回来），故在入口
            # 就拒掉并说清原因。
            if pid in self._registry.discovered_classes and (
                self._registry.source_of(pid) == "internal"
            ):
                return False, f"{pid} 是内置插件，不能安装同名外部包"
            was_enabled = bool(self._config.get(f"plugins.{pid}.enabled", True))
            # 换包前先停旧实例：它的模块文件正要被换掉。
            if pid in self._registry.plugins:
                self.disable(pid)
            base.mkdir(parents=True, exist_ok=True)
            ok, msg = installer.install_external(
                base,
                pkg_dir,
                activate=lambda p, d: self._activate_installed(p, d, was_enabled),
            )
            if ok:
                # 重扫已在激活那一步做过（那里必须用新类起实例），这里不再扫第二遍。
                self._notify_changed()
                return True, f"已安装 {pid}（{version_label(ident.version)}）"
            # 回滚：目录已换回旧版，类表要跟着退回去，旧实例也得重新起来 ——
            # 「激活失败恢复旧版」这句承诺要包含「旧版仍然在跑」这一层。
            self._refresh_discovery()
            if was_enabled:
                self.enable(pid)
            self._notify_changed()
            return False, msg

    def _activate_installed(self, plugin_id: str, plugin_dir, was_enabled: bool) -> None:
        """新包已就位后的激活：重扫发现 → 按原启用态起实例。

        **抛异常 = 触发底层回滚**（恢复旧版），故「新版本没起来」一律抛 ——
        包括新类压根没被发现有：包写得不对（契约闸门拒了它）也是没起来。
        """
        self._refresh_discovery()
        if not was_enabled:
            return
        if self.enable(plugin_id):
            return
        # 把发现期的拒绝原因带上去：用户要看到的是「为什么这版装不上」，不是一句
        # 「启动失败」。桩注册中心可能没有 rejected，故 getattr 兜底。
        for pid, why in getattr(self._registry, "rejected", None) or []:
            if pid == plugin_id:
                raise RuntimeError(why)
        raise RuntimeError(f"{plugin_id} 新版本未能启动（详见日志）")

    def _refresh_discovery(self) -> None:
        """重扫插件表：装成后认出新类、回滚后换回旧类。失败只记日志。

        ``getattr`` 而非直接调：注册中心的轻量桩没有这个接口；而重扫失败不该把一次
        **已经成功的**安装判成失败 —— 文件已就位，下次启动照样能发现。
        """
        refresh = getattr(self._registry, "refresh_discovery", None)
        if not callable(refresh):
            return
        try:
            refresh()
        except Exception:  # noqa: BLE001
            log.exception("安装后重扫插件失败（下次启动会重新发现）")
