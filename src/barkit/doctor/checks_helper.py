"""doctor/checks_helper：E 组「helper 与 root 资产」检查。

整组 **macOS 专属**（``darwin_only``）：helper 是 LaunchDaemon，A 类资产部署在
``/Library/Application Support/BarKit``，两者在别的平台上都不存在。

分级取向（改这一组前先读）
----------------------
helper 没装 / 没在跑，在正常使用中**并不罕见**：用户可能根本没启用需要 root 的
插件（路由 / 隧道 / hosts 都要），也可能刚装完还没启动。这类情形一律 WARN 或
SKIP，**不报 FAIL** —— self-check 天天红，用户就不看了，那比不做自检更糟。

FAIL 留给「装了却坏了」与「两侧协议不一致」：outdated（已装版本 ≠ 期望版本）
意味着 helper 会拒绝 app 写出的新契约文件，那是真坏。

只读：本组不做任何安装 / 卸载 / 授权动作（不提权，见 plan §3）。
"""

from __future__ import annotations

from barkit import paths
from barkit.doctor.model import FAIL, PASS, SKIP, WARN, Check, Result, run_checks

GROUP = "helper"

#: helper.log 的上限（``capabilities/helper/safeio._HELPER_LOG_MAX_BYTES``）：**单文件**，
#: 写满就地截尾、不留备份。超限 ⇒ 跑的是不会截尾的旧 helper。
#: ⚠️ 判据 2026-09-29 换过：从前是「有没有轮转产物 ``helper.log.1``」，备份形态下线之后
#: 那条会把**正常**的单文件态误报成旧装（新 helper 永远不会再生出 ``.1``）。
LOG_CAP_BYTES = 1 << 20


def check_helper_status() -> list[Result]:
    """E1：helper 装否 / 跑否 / 已装版本 vs 期望版本。"""
    from barkit.capabilities import helper_client

    st = helper_client.status()
    version = st.get("version") or "（读不到）"
    expected = st.get("expected")
    detail = (f"已装 {version} · 期望 {expected} · "
              f"{'运行中' if st.get('running') else '未在运行'}")

    if not st.get("installed"):
        return [Result("E1", GROUP, "helper 守护", WARN,
                       f"未安装（{detail}）",
                       "启用路由 / 隧道 / hosts 任一插件才需要它：设置 → 插件页安装。"
                       "不需要这些功能的话，这条可以放着")]
    if st.get("outdated"):
        return [Result("E1", GROUP, "helper 守护", FAIL,
                       f"已装版本与 app 不一致（{detail}）",
                       "helper 会拒绝 app 写出的新契约文件（表现为「什么都没发生」）："
                       "设置 → 插件页点「重装」，会再弹一次 root 授权")]
    if not st.get("running"):
        return [Result("E1", GROUP, "helper 守护", WARN,
                       f"已安装但未在运行（{detail}）",
                       f"看 {paths.APP_SUPPORT / 'helper.log'} 的尾部；"
                       "仍起不来就重装一次（设置 → 插件页）")]
    return [Result("E1", GROUP, "helper 守护", PASS, f"运行中（{detail}）")]


def check_install_format() -> list[Result]:
    """E2：安装形态是否过期（老形态直跑 python3 / 缺新启动器）。

    版本号是主判据（E1）；这条补的是「版本号恰好一致但文件形态残缺」的兜底。
    """
    from barkit.capabilities import helper_client

    if not helper_client.HELPER_VERSION_PATH.exists():
        return [Result("E2", GROUP, "helper 安装形态", SKIP,
                       "未安装（无版本锚点）",
                       "先看 E1 那条；不需要 helper 功能的话可忽略")]
    if helper_client.install_format_outdated():
        return [Result("E2", GROUP, "helper 安装形态", WARN,
                       "是老形态安装（启动器缺失或 plist 仍指向 python3 直跑）",
                       "重装一次即可换成新形态：设置 → 插件页「重装」")]
    return [Result("E2", GROUP, "helper 安装形态", PASS, "已是当前安装形态")]


def check_root_binaries() -> list[Result]:
    """E3：A 类 root 资产对账（``wanted`` vs 系统域实际存在）。

    ``wanted`` 走**插件类声明**（``survey.declared_root_binaries``）而非
    ``Registry.wanted_root_binaries()``：后者读的是「已装配实例」，而 doctor
    按只读铁律不跑 ``load_all()``，那份集合恒为空 —— 用它做对账会把**全部已部署
    资产都判成「多余的」并建议回收**，是最危险的一种误报。
    """
    from barkit.capabilities import helper_client
    from barkit.core.rootbin_reconcile import reconcile
    from barkit.doctor import survey

    try:
        _, found, _ = survey.discover()
    except Exception as exc:  # noqa: BLE001
        return [Result("E3", GROUP, "A 类资产对账", SKIP,
                       f"插件发现不可用，无法算 wanted 侧：{exc.__class__.__name__}: {exc}",
                       "先解决 C 组的问题再回头看")]

    want, owners, errors = survey.declared_root_binaries(found)
    out: list[Result] = [
        Result(f"E3.{pid}", GROUP, f"A 类声明读不出来：{pid}", WARN,
               f"该插件的 resources() 在无实例状态下抛异常：{why}",
               "该插件声明未计入对账；若它确有 A 类资产，本条的结论会偏保守（不误删）")
        for pid, why in errors
    ]

    if not helper_client.helper_installed():
        return out + [Result("E3", GROUP, "A 类资产对账", SKIP,
                             f"helper 未安装，系统域不存在（声明了 {len(want)} 个 A 类资产）",
                             "属正常：装 helper 时才会部署它们")]

    have = helper_client.deployed_root_binaries()
    to_deploy, to_reap = reconcile(want, have)

    if to_deploy:
        names = "、".join(sorted(to_deploy))
        owners_txt = "、".join(sorted({owners.get(n, "?") for n in to_deploy}))
        out.append(Result("E3.缺", GROUP, "A 类资产缺失", WARN,
                          f"声明了但系统域没有：{names}（来自 {owners_txt}）",
                          "重装 helper（设置 → 插件页）会把它们一并部署；"
                          "缺 hev 时隧道起不来，路由 / hosts 不受影响"))
    if to_reap:
        names = "、".join(sorted(to_reap))
        out.append(Result("E3.多", GROUP, "A 类资产残留", WARN,
                          f"系统域存在但已无插件声明：{names}",
                          "多为已卸载插件的残留。留着不占事；要回收需 root，"
                          "走一次 helper 重装即可"))
    if not to_deploy and not to_reap:
        out.append(Result("E3", GROUP, "A 类资产对账", PASS,
                          f"声明与实际一致（{len(want)} 个）"))
    return out


def check_helper_log() -> list[Result]:
    """E4：helper 日志大小（**单文件、写满就地截尾**；超限即证明截尾没生效）。"""
    log_path = paths.HELPER_LOG
    if not log_path.exists():
        return [Result("E4", GROUP, "helper 日志", SKIP, "还没有 helper.log",
                       "属正常：helper 跑过之后才会有")]

    try:
        size = log_path.stat().st_size
    except OSError as exc:
        return [Result("E4", GROUP, "helper 日志", WARN,
                       f"取不到大小：{exc.__class__.__name__}: {exc}",
                       "检查该文件的权限 / 属主")]

    human = f"{size / 1024:.0f} KiB"
    if size > LOG_CAP_BYTES:
        return [Result("E4", GROUP, "helper 日志", WARN,
                       f"{human}，已超过 {LOG_CAP_BYTES // (1 << 20)} MiB 上限（截尾没生效）",
                       "装的 helper 可能早于本版（超限时应当就地截尾、只留一份）："
                       "重装一次即可；旧日志可以自行删除，不影响运行")]
    stale = log_path.with_name(log_path.name + ".1")
    if stale.exists():
        # 早先那版轮转留下的备份：只是残渣，不影响判级（数量收窄是「新写的只有一份」）
        return [Result("E4", GROUP, "helper 日志", PASS,
                       f"{human}（另有早先轮转留下的 helper.log.1，可自行删除）")]
    return [Result("E4", GROUP, "helper 日志", PASS, human)]


def check_autostart() -> list[Result]:
    """E5：开机自启（LaunchAgent）状态。

    未安装不算问题 —— 开机自启是可选项，用户完全可以让 BarKit 手动启。
    """
    from barkit.core import autostart

    st = autostart.status()
    detail = ("已安装" if st.get("installed") else "未安装")
    detail += "、" + ("已加载" if st.get("running") else "未加载")

    if not st.get("installed"):
        return [Result("E5", GROUP, "开机自启", SKIP, detail,
                       "属正常：可在设置里开启，不开启也能手动启动 BarKit")]
    if st.get("stale"):
        return [Result("E5", GROUP, "开机自启", WARN,
                       f"{detail}，但启动器指向的 Python 与当前解释器不一致",
                       "venv 重建 / Python 升级后会出现：在设置里重装一次开机自启即可")]
    if not st.get("running"):
        return [Result("E5", GROUP, "开机自启", WARN, detail,
                       "登录项已装但未加载：注销重登或重装一次开机自启")]
    return [Result("E5", GROUP, "开机自启", PASS, detail)]


CHECKS = (
    Check("E1", "helper 守护", check_helper_status),
    Check("E2", "helper 安装形态", check_install_format),
    Check("E3", "A 类资产对账", check_root_binaries),
    Check("E4", "helper 日志", check_helper_log),
    Check("E5", "开机自启", check_autostart),
)


def check_helper() -> list[Result]:
    return run_checks(GROUP, CHECKS)
