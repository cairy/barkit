"""``_ShellCfgMixin``：托盘底图、托盘阈值、日志级别与 topic 折算。

``config.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from barkit.contribution.specs import (
    TRAY_MIN_LEVEL_DEFAULT,
    TRAY_MIN_LEVELS,
)


def _topic_to_state_id(value: str) -> str:
    """``plugin.<id>.status.<key>`` → ``<id>.<key>``；其它形态原样返回。

    ``tray.base_source`` 的**读侧兼容**：P0③ 首版把状态 topic 直接落了盘，
    现在配置指向的是状态机**身份**（``StateSpec.state_id``）。只读折算、不写盘 ——
    用户下次动下拉时自然覆盖；在此之前两种取值等价（同一台机、同一个 key）。
    """
    if value.startswith("plugin.") and ".status." in value:
        plugin_id, _, key = value[len("plugin."):].partition(".status.")
        if plugin_id and key and "." not in key:
            return f"{plugin_id}.{key}"
    return value


class _ShellCfgMixin:
    """框架侧的托盘与日志设置：底图来源、托盘最低级别、日志级别，外加 topic→state_id 的读侧折算。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 菜单栏图标：托盘底图来源（P0③） ----------

    def tray_base_source(self) -> str:
        """菜单栏主图标的来源：某台状态机的**身份**（``""`` = BarKit 默认底图）。

        取值 = ``StateSpec.state_id``（``{plugin_id}.{状态机 key}``，key 缺省用保留键
        ``main``）—— 指向的是「某组件的某台状态机」，**不是**事件总线上的 topic，
        故配置项与 pub/sub 层解耦。准入由状态机自己声明：**定义了主图标**
        （``StateSpec.icon is not None``）才可能被用户选到，设置窗「通用」页按此枚举。

        非法类型一律按「未设置」处理——渲染侧据此回落默认底图，且永不因配置写坏丢图标。
        历史值（``plugin.<id>.status.<key>`` 形态的 topic）**就地折算**成身份键后返回，
        不写盘：下一次用户动下拉时自然被覆盖，在此之前读路径行为等价。
        """
        value = self.get("tray.base_source", "")
        if not isinstance(value, str):
            return ""
        return _topic_to_state_id(value)

    def set_tray_base_source(self, source: str) -> None:
        """持久化托盘底图来源（设置窗「通用」页写入）。

        写用户**差异层**的顶层 ``tray.base_source``；传空串表示「回落默认底图」，
        此时删键而非留空值（不留 ``tray: {base_source: ""}`` 这种残渣），``tray``
        随之变空时一并删除。
        """
        user = self._read_user_raw()
        tray = user.get("tray")
        if not isinstance(tray, dict):
            tray = {}
        if source:
            tray["base_source"] = source
        else:
            tray.pop("base_source", None)
        if tray:
            user["tray"] = tray
        else:
            user.pop("tray", None)
        self.save_user(user)

    def log_level(self) -> str:
        """日志级别（``log.level``）：``debug`` / ``info`` / ``warning`` / ``error``；
        **返回空串表示用户没配这一档**。

        与环境变量 ``BARKIT_LOG_LEVEL`` 是同一档位的两个入口，分工按「谁拉起的进程」：

        - 从终端跑 ⇒ 用环境变量（``BARKIT_LOG_LEVEL=debug uv run barkit``），
          跑完就走，不留痕；
        - 常驻实例 ⇒ 用本键。它由 launchd 拉起，**拿不到终端环境**，只有落盘的
          配置改得动它。

        出厂文件**刻意不写**这一键（否则每个用户都会拿到一份 "info"，反过来把
        环境变量盖掉）。故这里返回空串时，装配根应当**什么都不做**，让
        ``setup_logging`` 已经应用好的级别（环境变量或出厂档）继续生效。

        非法值**不在此处报错**：由 ``logging_setup.resolve_level`` 回退默认档——
        日志是兜底设施，一个手滑的级别名不该让整个 App 起不来。
        """
        value = self.get("log.level", "")
        return value if isinstance(value, str) else ""

    def set_log_level(self, level: str) -> None:
        """持久化日志级别（设置窗「维护」页写入）。

        ``level`` 为空串表示「回落跟随启动环境」——此时删键而非留空值
        （不留 ``log: {level: ""}`` 这种残渣），``log`` 随之变空时一并删除。
        非法类型**不在此处报错**：写侧落一个读侧还得兜底的键只会让问题更难查，
        ``log_level`` 读侧已由 ``logging_setup.resolve_level`` 兜回默认档。
        """
        user = self._read_user_raw()
        # 局部变量故意不叫 ``section``：``test_reset_keeps_framework_owned_keys``
        # 是 AST 门禁，扫「名为 section/existing 的变量上的字面量下标写入」来守住
        # 「plugins.<id> 段的新键必须表态」——这里写的是 ``log`` 段，撞名会被误伤。
        log_section = user.get("log")
        if not isinstance(log_section, dict):
            log_section = {}
        if level:
            log_section["level"] = level
        else:
            log_section.pop("level", None)
        if log_section:
            user["log"] = log_section
        else:
            user.pop("log", None)
        self.save_user(user)

    def tray_min_level(self) -> str:
        """托盘角标的**全局最低级别**（决议 D17）：低于它的取值即使标了 ``alerts``
        也不上屏。

        合法值只有 ``warn`` / ``err``（值域归契约：``contribution.specs.TRAY_MIN_LEVELS``）
        —— ``ERR`` 一律露出，用户收紧不到「屏蔽 err」（契约规范 §2.5 的安全不变式）；
        非法 / 缺失一律按出厂档 ``warn`` 处理，宁可多露一点也不静默把可见性调低。
        设置窗「通用 → 菜单栏」的控件经 :meth:`set_tray_min_level` 写这一格。
        """
        value = self.get("tray.min_level", TRAY_MIN_LEVEL_DEFAULT)
        return value if isinstance(value, str) and value else TRAY_MIN_LEVEL_DEFAULT

    def set_tray_min_level(self, level: str) -> None:
        """持久化托盘阈值（设置窗「通用」页写入）。

        ``level`` 非法在**调用期**报错（写侧宁可早 fail，不静默落一个读侧还得兜底的键）。
        写回**出厂档**时删键 —— 默认值落盘只是残渣（与 ``set_tray_base_source`` 传空串
        删键同理），``tray`` 随之变空时一并删除。
        """
        if level not in TRAY_MIN_LEVELS:
            raise ValueError(f"未知托盘阈值：{level!r}；合法值 {TRAY_MIN_LEVELS}")
        user = self._read_user_raw()
        tray = user.get("tray")
        if not isinstance(tray, dict):
            tray = {}
        if level != TRAY_MIN_LEVEL_DEFAULT:
            tray["min_level"] = level
        else:
            tray.pop("min_level", None)
        if tray:
            user["tray"] = tray
        else:
            user.pop("tray", None)
        self.save_user(user)
