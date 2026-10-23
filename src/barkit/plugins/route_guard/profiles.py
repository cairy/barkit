"""``_ProfilesMixin`` —— 场景表的数据面（读 / 写 / 建议项）与配置窗体的保存接线。

``plugin.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import copy
from functools import partial

from barkit.contribution.specs import (
    ActionResult,
    MenuItem,
)
from barkit.plugins.route_guard import rules
from barkit.plugins.route_guard.rules import RouteConfigError


class _ProfilesMixin:
    """场景表的数据面（读 / 写 / 建议项）与配置窗体的保存接线。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 配置窗体（详情页）的数据面（§7.2：UI 只画，配置 IO 留在这里） ----------
    # 设置面（`settings.py`）不碰 ctx / config：读走 editable_profiles，写只经
    # save_profiles —— 跨面禁令 3（门禁 `test_no_config_io_in_ui` 守这条）。

    def editable_profiles(self) -> list[dict]:
        """可编辑的场景列表（**有效配置**：默认 ＋ 用户差异已合并，深拷贝）。

        起点取**合并后**的列表，而不是只取用户差异段：旧配置窗体只读用户段，于是新装
        用户打开窗体看到的是一张**空表**，而面板上明明写着命中 `office` —— 再加一个
        场景，整份默认列表就被悄悄顶掉了（`profiles` 是整表覆盖语义）。用户点开编辑器，
        该看到的就是「此刻在跑的是什么」。
        """
        return copy.deepcopy(self._profiles())

    def save_profiles(self, profiles: list[dict]) -> tuple[bool, str, int | None]:
        """整体写回用户 YAML → 重建期望态 → 刷三台机。

        返回 ``(是否写入, 给用户看的一句话, 出问题的场景下标)``。下标只给配置窗体用：
        它要知道这次失败是不是「刚新增、还没填完」那个场景造成的（那种场景不回滚、
        留在窗体里等用户填完，见 ``settings._SceneEditor``）。

        三条判据：

        - **一条不合法就一条都不写**：半份配置比旧配置更难查 —— 面板上三台机只会说
          「配置非法」，用户不知道是哪一条。
        - **校验与编译只喂 ``profiles`` 这一层**：整段里其它键（``paused`` /
          ``helper_poll`` / ``rule_overrides`` / ``pinned`` / ``fallback``）各有自己的
          落点（设置页 / 面板开关 / 场景选择器），不归本窗体管。拿整段去编译，会把
          「``pinned`` 指向的场景刚被改名」这类与本窗体无关的问题算成「保存失败」，
          用户既修不了也看不懂。
        - 编译一遍是为了**提前暴露跨字段约束**（多网卡 / 多下一跳 / 同一场景内开关不
          一致，见 ``rules`` 模块 docstring）：只写 YAML 不编译，这些错要等下一轮
          ``tick()`` 才由三台机报出来，那时用户已经离开窗体了。
        """
        candidate = copy.deepcopy(list(profiles))
        for i, profile in enumerate(candidate):
            err = rules.validate_profile(profile, i)
            if err:
                return False, err, i
        names = [str(p.get("name", "")).strip() for p in candidate]
        for i, name in enumerate(names):
            if names.count(name) > 1:
                return False, f"场景名重复：「{name}」（列表里只能出现一次）", i
        try:
            rules.build_routes({"profiles": candidate})
        except RouteConfigError as e:
            return False, str(e), None

        try:
            self.cfg.set("profiles", candidate)
        except Exception as e:  # noqa: BLE001 —— 写盘失败要变成一句人话，不能让界面崩
            return False, f"写回配置失败：{e}", None
        # 被删掉（或改名掉的）场景如果正被 `pinned` 指着，就在同一条路径里清掉它：
        # 悬空引用会让**整段配置非法**（`build_routes` 报「pinned 指向不存在的场景」），
        # 而那是用户刚刚做的那件事的反面 —— 他只是删了一个场景，不该顺手把组件停掉。
        # 边界：**只清本次界面动作造成的悬空**；手改 YAML 造成的悬空仍然报错指路
        # （与「旧 rules[] 不自动迁移」同一条判据：组件不替用户猜他想要什么）。
        stale = self._pinned_raw()
        if stale and stale not in set(names):
            ok, problem = self._write_keys({"pinned": ""}, fail="清理失效的固定使用失败")
            if not ok:
                # 场景已经写进去了（上面那句 `cfg.set` 成功），只是「顺手取消固定使用」
                # 这一步没成 —— 结果文案必须说清是哪一半没成，不能报一句假的「一并取消」。
                return True, f"已写入 {len(candidate)} 个场景（「{stale}」已删除，但{problem}）。", None
            return True, f"已写入 {len(candidate)} 个场景（「{stale}」已删除，固定使用一并取消）。", None
        self._sync_after_change()
        return True, f"已写入 {len(candidate)} 个场景。", None

    def detail_status_text(self) -> str:
        """配置窗体顶上那条只读状态（设计 §4.3 L3 草图里的「现在谁在管」）。

        守护出问题时**先说问题并指路**（同 §3.3「异常项优先」）：这一页是点面板整行
        进来的地方，用户在这里发现「改了场景却不生效」的概率最高 —— 而装守护的按钮在
        设置窗（本页只管配置），不指路他就会以为这一页就该有（2026-09-18 实测：用户
        原话就是「过期了只能通过网络切换的设置页面安装，点击组件弹出的是场景配置」）。
        """
        st = self._daemon_status or {}
        if st.get("outdated"):
            ver = st.get("version") or "?"
            exp = st.get("expected") or "?"
            return (f"守护版本过期（v{ver} → v{exp}），规则不会生效；"
                    f"请在设置窗「网络切换」页点「重装守护」。")
        if not st.get("installed"):
            return ("守护尚未安装，规则不会生效；"
                    "请在设置窗「网络切换」页点「安装守护」。")
        auto = "关（保持现状，路由不动）" if self.is_paused else "开"
        scene = self._current_scene()
        if scene is None:
            return f"自动化：{auto}　　当前生效：尚未读取"
        how = "手动指定" if self.pinned_scene() else "自动命中"
        return f"自动化：{auto}　　当前生效：{scene}（{how}）"

    # ---------- 配置窗体的「保存」（决议 D35） ----------

    def attach_editor(self, editor) -> None:
        """配置窗体把**自己**交给插件（框架壳要按它问「怎么保存 / 有没有改动」）。

        同 `attach_feedback`：销毁时摘引用 —— 窗体关掉之后框架就不该再问它，
        而 ``destroyed`` 之后再去调同一个对象就是戳已删的 C++ 对象。
        """
        editor.destroyed.connect(lambda *_args, _ed=editor: self._forget_editor(_ed))
        self._editor = editor

    def _forget_editor(self, editor) -> None:
        """只在「当前引用的正是刚死掉的那个」时清空（重建时新窗体可能已先赋好值）。"""
        if self._editor is editor:
            self._editor = None

    def _editor_commit(self) -> tuple[bool, str]:
        """框架壳那枚「保存」键的回调。窗体已经关掉时**不算失败**（没什么可存）。"""
        editor = self._editor
        if editor is None:
            return True, ""
        return editor.save()

    def _editor_dirty(self) -> bool:
        """框架壳关窗前问的「还有没保存的改动」。窗体不在 ⇒ 没有。"""
        editor = self._editor
        return bool(editor is not None and editor.dirty())

    # ---------- 场景：手动指定（`pinned`）与场景选择器（设计 §2.3 / §4.5 / §4.6） ----------

    def _pinned_raw(self) -> str:
        """配置里 ``pinned`` 的**原值**（不校验场景是否存在）。

        「该不该清掉一个悬空引用」要看原值；而「当前锁着谁」要看它确实存在的那个名字
        （``pinned_scene``）。两个问题，两个读法。

        非字符串的原值由 ``cfg`` 按声明类型回落成缺省（空串 = 按匹配自动选），
        与其它配置键同一口径。
        """
        return str(self.cfg.value("pinned")).strip()

    def pinned_scene(self) -> str:
        """``pinned``：用户手动指定的场景名；空串 = 自动按匹配（§2.3 三态里的 ``auto``）。

        只在名字**确实存在于场景表**里时才认它：写着一个不存在的场景名属于非法配置
        （``rules.build_routes`` 会当场报「pinned 指向不存在的场景」），错误归 ``routes`` 机；
        这里再认它一次，就等于让徽标拿一个不存在的场景名去骗人。
        """
        name = self._pinned_raw()
        if not name:
            return ""
        return name if name in {str(p.get("name", "")).strip() for p in self._profiles()} else ""

    def _manual_scenes(self) -> set[str]:
        """``manual_only: true`` 的场景名（§2.6）：退出自动匹配，只能手动指定。

        为什么它们必须退出：判定依据（网卡 IP / 网关）正是组件自己会写的东西 ——
        判成真的那一刻起**永远为真**，而且离开该网络也退不回来（§2.6 的自证循环）。
        """
        return {
            str(p.get("name", "")).strip()
            for p in self._profiles()
            if p.get("manual_only") is True and str(p.get("name", "")).strip()
        }

    def set_pinned(self, name: str) -> "ActionResult | None":
        """手动指定生效场景（面板徽标 → 场景列表，§4.5）：「**选中即生效**」。

        生效是三层一起动（``_write_keys`` 那条尾巴）：写 YAML → 重算期望态
        （``apply`` 只剩被指定的那一条为真）→ 三台机即时广播。不再弹二次确认、
        不再走配置窗体 —— 把「切场景」从 4 步压到 2 步是这一格的**全部要害**（§4.5）。

        写盘/数据出错时把结局**交回面板结果槽**（``ActionResult``，D32-②）：
        选中即生效那条路上，用户不会再有第二次确认的机会去发现「其实没生效」。
        """
        name = str(name).strip()
        known = {str(p.get("name", "")).strip() for p in self._profiles()}
        if not name or name not in known:
            # 只在配置刚被手改过、而菜单是点开那一刻取的列表时够得着
            return self._failed(f"配置里没有「{name}」这个场景，未改动任何东西。")
        if self.pinned_scene() == name:
            self._sync_after_change()       # 已经锁着它：不白写一次盘（同内容也会落一次）
            return None

        ok, problem = self._write_keys({"pinned": name}, fail="指定场景失败")
        return None if ok else self._failed(problem)

    def clear_pinned(self) -> "ActionResult | None":
        """取消手动指定，交回按网段 / 网关自动挑（§2.3 的 ``auto``）。

        同 `set_pinned`：失败也交回结果槽 —— 设置页开着时归它，否则归面板。
        """
        if not self.pinned_scene():
            self._sync_after_change()
            return None

        ok, problem = self._write_keys({"pinned": ""}, fail="取消手动指定失败")
        return None if ok else self._failed(problem)

    def _scene_hint(self, profile: dict) -> str:
        """候选项的摘要：**命中条件 · 路由数**（＋「仅手动」标记）（§4.5 硬要求 1）。

        组成只取配置里**真实写着**的东西 —— ``match.subnet`` / ``match.gateway`` 都是
        可选项，有什么说什么，不替它编一个网段出来（编出来的那半句话用户核对不了）。
        三样都能在配置窗体里改（生效网段 / 匹配网关 / 仅手动随第 8 步一起进窗体），
        故摘要跟着用户刚改完的配置走。
        """
        match = profile.get("match") or {}
        parts: list[str] = []
        subnet = str(match.get("subnet") or "").strip()
        if subnet:
            parts.append(subnet)
        gateway = str(match.get("gateway") or "").strip()
        if gateway:
            # 一律带「网关」二字：默认配置用的是 `match.gateway`，裸一个 IPv4 摆在首位
            # 用户会当成网段读（§2.4 专门提醒过这两个「网关」/「网段」不能混）
            parts.append(f"网关 {gateway}")
        if profile.get("manual_only") is True:
            parts.append("仅手动")
        block = profile.get("routes") or {}
        count = len(block.get("items") or [])
        if not count:
            parts.append("无路由")
        elif block.get("enabled", True) is False:
            parts.append(f"{count} 条路由（配置里关着）")
        else:
            parts.append(f"{count} 条路由")
        return " · ".join(parts) or "网卡在线即命中"

    def scene_items(self) -> "tuple[MenuItem, ...]":
        """场景选择器的候选列表（设计 §4.5）：**每项必须带摘要**，不给名字就完事。

        「只给名字的菜单等于没有信息」——用户是在「选哪个」的那一刻需要上下文，
        而不是选完之后再去别处查。故 ``hint`` 是必答项，不是装饰。

        「回到自动」那一行**只在真锁着的时候**出现，与设计 §4.5 草图里那个常驻的
        「● 自动（当前）」有意不同：那张草图写在决议 D33 之前，而 D33 定案徽标文案
        **兼任当前项**、当前项又必须是**生效场景名**（§2.3 微调）—— 于是「自动」这一行
        不可能同时是被选中的那一项，它只能是动作。常驻一条永远不被打勾的项，只会让
        「我现在是自动还是锁着」变得更难读。
        """
        items = [
            MenuItem(
                str(profile.get("name", "")).strip(),
                partial(self.set_pinned, str(profile.get("name", "")).strip()),
                self._scene_hint(profile),
            )
            for profile in self._profiles()
            if str(profile.get("name", "")).strip()
        ]
        if self.pinned_scene():
            items.append(MenuItem(
                "自动（按网络匹配）", self.clear_pinned,
                "取消手动指定，交回按网段 / 网关自动挑场景",
            ))
        # 「恢复 DHCP」（§4.7 二的动作）：**只在设置页勾选了网卡时出现**
        # （用户 2026-09-18 裁定：预设为空说明用户还没配置这个功能，摆一个点了只会
        # 被提示「去设置页」的项是噪声），且**固定第一行**——它是与「选场景」并列的
        # 主路径动作，不该沉到菜单底。恢复哪些网卡、是否连带 DNS 都由预设决定。
        #
        # ⚠️ 副行与主行**谁宽取谁**（`_MenuItemWidget.sizeHint`），而菜单宽度由所有项
        # 里最宽的那一项说了算 ⇒ 这句说明别写得比菜单里其它项还长。
        if self.handback_ifaces():
            items.insert(0, MenuItem(
                "恢复 DHCP", self.renew_action,
                "把已选网卡恢复为 DHCP 模式",
            ))
        return tuple(items)

    def _badge_label(self) -> str:
        """徽标文案 = **现在用的是哪一套**（§2.3 / §4.6；停用时「已停用」）。

        §4.6 的根因分析：徽标原先写「自动 / 手动」，那是**默认态** —— 把它放进一个带
        accent 的控件位，就成了开关的同义反复；而「现在用的是哪一套」才是这一行缺的信息。
        来源（自动命中 / 手动指定）挪去副标题首词。
        """
        if self.is_paused:
            return "已停用"
        pinned = self.pinned_scene()
        if pinned:
            return pinned
        state = self._profile_report().state
        if state in ("active", "conflict"):
            # 多场景同时命中时取**顺序上的第一个**：列表顺序即优先级，第 1 个命中者胜（§2.1）
            return self._current_scene() or "无匹配"
        if state == "none":
            return "无匹配"
        return "尚未读取"

    def _badge_accent(self) -> bool:
        """accent 表示「这一行当前有东西在管」（§2.3 微调）：你锁定的、或规则命中，
        都强调；停用时一律 neutral（那一行的重点是「它没在管」，上色自相矛盾）。

        命中却显示成灰色 ＝ 看着像「禁用」，与「规则已经生效」的事实相反（2026-09-18
        真机）：命中（active / 冲突）也是「在管」，应当与锁定同等强调。
        """
        if self.is_paused:
            return False
        if self.pinned_scene():
            return True
        return self._profile_report().state in ("active", "conflict")
