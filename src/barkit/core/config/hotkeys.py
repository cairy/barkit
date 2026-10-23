"""``_HotkeyMixin``：热键差异项、自建条目与成功反馈方式。

``config.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations


class _HotkeyMixin:
    """热键族：卸载时清用户差异项、写单条、用户自建条目、成功后的反馈方式（含旧版布尔键的兼容迁移）。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 热键配置清理（§9.1 卸载第 5 步） ----------

    def remove_hotkeys(self, names: list[str]) -> None:
        """卸载插件时删除其 ``hotkeys.<name>`` 用户差异项（与 plugins.<id> 同批清理）。"""
        if not names:
            return
        user = self._read_user_raw()
        section = user.get("hotkeys")
        if isinstance(section, dict):
            removed = False
            for name in names:
                if name in section:
                    del section[name]
                    removed = True
            if removed:
                if not section:
                    user.pop("hotkeys", None)
                self.save_user(user)

    def set_hotkey(self, name: str, combo: str) -> None:
        """写单个 ``hotkeys.<name>`` 差异项（导入导出包时恢复用）。"""
        user = self._read_user_raw()
        section = user.get("hotkeys")
        if not isinstance(section, dict):
            section = {}
            user["hotkeys"] = section
        section[name] = combo
        self.save_user(user)

    # ---------- 用户自建快捷键（快捷键页「我的」那一类） ----------

    def custom_hotkeys(self) -> "dict[str, dict]":
        """用户自建条目：``custom_hotkeys.<id>`` → ``{title, command}``。

        **为什么另起一段而不塞进 ``hotkeys``**：那段被校验钉成「字符串 → 字符串」，
        而自建条目要多存「跑什么命令」，塞进去等于动形状（连带所有手改过的 YAML）。
        键位**不**存在这里 —— 它仍在 ``hotkeys.<id>``（注册 / 重挂 / 冲突三条链
        都读那一段，键位就该只有一处真源）。

        读侧一律容错：段不是 mapping、条目不是 mapping、字段类型不对，都当
        「没这一条」—— 手改坏了的表现应当是**这一条不出现**，而不是整个配置加载失败。
        """
        section = self.get("custom_hotkeys")
        if not isinstance(section, dict):
            return {}
        out: dict[str, dict] = {}
        for name, item in section.items():
            if not isinstance(name, str) or not isinstance(item, dict):
                continue
            out[name] = {
                "title": str(item.get("title") or ""),
                "command": str(item.get("command") or ""),
            }
        return out

    def set_custom_hotkey(self, name: str, spec: dict) -> None:
        """写一个自建条目（只存 ``title`` / ``command`` 两个字段，别的多余）。"""
        user = self._read_user_raw()
        section = user.get("custom_hotkeys")
        if not isinstance(section, dict):
            section = {}
        section[name] = {"title": str(spec.get("title") or ""),
                         "command": str(spec.get("command") or "")}
        user["custom_hotkeys"] = section
        self.save_user(user)

    def remove_custom_hotkey(self, name: str) -> None:
        """删一个自建条目；段随之变空时连段一起删（不留空花括号）。"""
        user = self._read_user_raw()
        section = user.get("custom_hotkeys")
        if not isinstance(section, dict) or name not in section:
            return
        del section[name]
        if not section:
            user.pop("custom_hotkeys", None)
        self.save_user(user)

    # ---------- 热键触发成功后的反馈方式（按条目） ----------

    def hotkey_feedback(self, name: str, default: str = "none") -> str:
        """读 ``hotkey_feedback.<name>``；缺失时按**来源推出来的默认值**答。

        三种取值（字面量，避免 core 反向依赖 shell）：``"none"`` 成功不提示、
        ``"overlay"`` 成功弹轻浮层、``"notify"`` 成功发系统通知。

        兼容旧版 ``hotkey_notify.<name>``（布尔）：True→notify、False→overlay；
        新键存在时优先用新键。
        """
        section = self.get("hotkey_feedback")
        if isinstance(section, dict) and name in section:
            value = section[name]
            if value in ("none", "overlay", "notify"):
                return value
        legacy = self.get("hotkey_notify")
        if isinstance(legacy, dict) and name in legacy:
            return "notify" if legacy[name] else "overlay"
        return default

    def set_hotkey_feedback(self, name: str, value: str, default: str = "none") -> None:
        """写一条「成功后反馈方式」；**等于默认值就删键**（默认值不进用户配置）。

        兼容旧版布尔键：写入新键时顺手把同名旧键迁走，不留两份。
        """
        if value not in ("none", "overlay", "notify"):
            value = default
        user = self._read_user_raw()
        section = user.get("hotkey_feedback")
        if not isinstance(section, dict):
            section = {}
        if value == default:
            section.pop(name, None)
        else:
            section[name] = value
        legacy = user.get("hotkey_notify")
        if isinstance(legacy, dict):
            legacy.pop(name, None)
            if legacy:
                user["hotkey_notify"] = legacy
            else:
                user.pop("hotkey_notify", None)
        if section:
            user["hotkey_feedback"] = section
        else:
            user.pop("hotkey_feedback", None)
        self.save_user(user)
