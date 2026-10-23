"""**攒改动与保存**：把右表单收进内存里那一条 ``profile``、脏判据（与基线整体比）、写盘那一下，以及场景的增 / 删。

本窗体的总口径是「改完攒着，点『保存』才写下去」：攒改动不会失败（会失败的是写盘那一刻），故这些入口都没有返回值需要调用方接。

``settings.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

import copy

from .settings_netform import _NET_DRAFT_KEY, parse_iface_field


class _SceneDraftMixin:
    """攒改动 / 保存 / 场景的增删。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- 增删改（攒在内存里，点「保存」才写下去） ----------

    def _collect_fields(self) -> None:
        """把右表单收进内存里那一条场景（**不落盘** —— 写盘只发生在 `save`）。

        逐字段就地改在 ``deepcopy`` 出来的场景上：本窗体不认得的键原样带过去 ——
        编辑器只该改它显示的东西，不能「保存一次就掉一半」。

        **网卡那一块可能还没填完**（`_NET_DRAFT_KEY`）：静态 IP 的 ``ip`` / ``mask``
        是必填，而「先选模式、再填地址」是唯一可行的输入顺序 —— 选中的那一刻必然没有
        地址。照写的话，用户一点模式就吃到一条校验失败、控件还被弹回去，那一态**永远
        走不到**；于是先只活在窗体里，**保存时它是拦路虎**（见 `save`）。

        没有返回值：攒改动不会失败（会失败的是写盘那一刻），调用方不必接错误。
        """
        if self._loading:
            return
        idx = self._editing
        if idx is None or not (0 <= idx < len(self._profiles)):
            return
        stored = self._profiles[idx]
        profile = copy.deepcopy(stored)
        name = self._name.text().strip()
        profile["name"] = name
        # 多网卡（第一命中制）：这一格收的是**候选清单**，顺序即优先级 —— 「哪个接上都算」
        # 的典型场合是扩展坞与直插转换器在系统里拿到的名字不同。分隔口径只有一份
        # （``parse_iface_field``），装载那一侧用的是同一份。
        profile["ifaces"] = parse_iface_field(self._iface.text())
        match = dict(profile.get("match") or {})
        gateway = self._gw.text().strip()
        if gateway:
            match["gateway"] = gateway
        else:
            match.pop("gateway", None)
        subnet = self._subnet.text().strip()
        if subnet:
            match["subnet"] = subnet
        else:
            match.pop("subnet", None)
        if match:
            profile["match"] = match
        else:
            profile.pop("match", None)
        # `manual_only: true` 才写；取消勾选才摘。（写着的 `manual_only: false` 与不写
        # 在求值路径上是同一件事，但**抹掉它就是改用户的配置** —— 见 `_merge_net` 里
        # 那条同源的理由：`profiles` 整表覆盖，一次「顺手归一化」会把整份合并结果
        # 写进用户 YAML，默认场景就此多一份第二真源。）
        if self._manual.isChecked():
            profile["manual_only"] = True
        elif stored.get("manual_only") is True:
            profile.pop("manual_only", None)
        block = dict(profile.get("routes") or {})
        block["enabled"] = bool(self._routes_on.isChecked())
        block["items"] = self._collect_items()
        profile["routes"] = block
        draft, hold = self._merge_net(profile, stored)
        if draft is None:
            profile.pop(_NET_DRAFT_KEY, None)
        else:
            profile[_NET_DRAFT_KEY] = draft
        self._profiles[idx] = profile
        # 只在**这一格刚变成半成品**时说一句：每次离开输入框都重放一遍，会把上一次
        # 保存失败的红字挤掉（那条恰恰是用户还要照着改的）。
        if hold and hold != self._fb.text():
            self._fb.hint(hold)
        self._update_row_label(idx)

    # ---------- 保存（框架壳那枚「保存」键走这两条） ----------

    def dirty(self) -> bool:
        """有没有还没写进配置的改动。框架壳**只在关窗那一刻**问它（决议 D35）。

        与 `_baseline`（上次保存 / 装载时那份快照）整体比：改回原样就自动算干净，
        不需要在每一条编辑路径上记得「标记脏」——那种写法漏一处就永久脏。
        """
        return self._profiles != self._baseline

    def save(self) -> tuple[bool, str]:
        """把整张场景表写进配置。返回 ``(成功?, 一句话)``（框架壳原样上屏）。

        **失败不回滚**：攒在窗体里的就是用户刚打的字，写不进去的原因说出来让他改，
        而不是把他打的东西一起收走（旧口径「失败即回滚」在手动保存下等于毁掉草稿）。
        """
        if self._loading:
            return True, ""
        self._collect_fields()
        # 半成品要先拦住，且**不写任何一个字**：这一下的含义是「整张表写进去」，
        # 一半写一半不写会让用户以为全存了。两处点名到格：
        for i, profile in enumerate(self._profiles):
            if profile.get(_NET_DRAFT_KEY):
                name = str(profile.get("name") or "未命名")
                return False, f"「{name}」的网卡配置还没填完（IP / 掩码都是必填），先填上再保存。"
        ok, msg, bad = self._plugin.save_profiles(self._profiles)
        if ok:
            self._baseline = copy.deepcopy(self._profiles)
            self._draft = None
            # 列表上那些「还没填完」的标记要跟着消失 —— 换行文案而不重建列表
            # （重建会动选中行，而用户常常刚填完那一格、正停在那一行上）。
            for i in range(len(self._profiles)):
                self._update_row_label(i)
            return True, "已保存。"
        if bad is not None and bad == self._draft and 0 <= bad < len(self._profiles):
            name = str(self._profiles[bad].get("name") or "新场景")
            return False, f"「{name}」还没填完（至少要选一块「监控网卡」），填好再保存。"
        return False, msg

    def _on_routes_toggled(self, _checked: bool) -> None:
        """复选框翻转 = 一次改动（`toggled` 带 bool，故不能直接接 `_collect_fields`）。"""
        self._collect_fields()

    def _on_item_toggled(self, box) -> None:
        """逐条目标的开关翻转 = 一次改动。

        ``ui_kit.Switch.clicked`` **不翻转自身**（与 ``setChecked`` 解耦，避免「刷新 UI →
        setChecked → 回环」，见该类的 docstring），故这里先推目标态、再收进内存。
        写盘仍然只发生在点「保存」那一刻。
        """
        box.setChecked(not box.isChecked())
        self._collect_fields()

    def _on_add_item(self) -> None:
        """加一行空的：等目标那格填完（焦点离开）才收进内存 —— 空目标不算一条。"""
        self._add_item_row("", "")
        self._item_rows[-1][1].setFocus()

    def _on_add(self) -> None:
        """新增场景 = 先立一张**草稿**（没有监控网卡 ⇒ 保存时会被拦住，见类 docstring）。"""
        self._collect_fields()
        name = self._unique_name("新场景")
        self._profiles.append({
            "name": name,
            "ifaces": [],
            "match": {},
            "routes": {"enabled": True, "items": []},
        })
        self._draft = len(self._profiles) - 1
        self._rebuild_list()
        self._select(self._draft)
        self._fb.hint(f"「{name}」还没填完——填好「监控网卡」，再点「保存」写进配置。")
        self._iface.setFocus()

    def _on_delete(self) -> None:
        """删除场景：**行内**二次确认（零模态框），确认后从表里摘掉（保存才写盘）。

        ⚠ **下标跟着问句一起递下去**，不能在确认时重读 ``self._editing``：问句挂着的那
        一会儿列表照样可点，用户可以先选中别的一条再回头按「删除」—— 那时重读出来的
        是**另一条**的下标，而问句上写着的还是原来那个名字。
        """
        idx = self._editing
        if idx is None or not (0 <= idx < len(self._profiles)):
            return
        name = str(self._profiles[idx].get("name") or "?")
        if self._draft == idx:
            self._fb.ask(f"放弃「{name}」（还没填完，本来也没写进配置）？",
                         "放弃", lambda idx=idx: self._do_delete(idx))
            return
        # ⚠ 删掉配置 ≠ 撤掉已经在路由表里的条目：helper 只对**意图里现存的规则**做增删
        # （`rules.py` 模块 docstring），规则一从意图里消失，它此前加的路由就没人去撤。
        # 所以确认文案必须把这条代价写出来，并给出可执行的替代路径。
        self._fb.ask(
            f"删除场景「{name}」？此操作不可恢复。已经叠加到路由表里的条目<b>不会</b>被自动撤掉"
            f"——想撤干净，先取消勾选「叠加本场景的路由」、等守护下一轮巡查生效后再删。",
            "删除",
            lambda idx=idx: self._do_delete(idx),
            confirm_kind="danger",
        )

    def _do_delete(self, idx: int | None = None) -> None:
        """摘掉**问句点名的那一条**（``idx`` 来自问句；缺省才退回当前选中）。"""
        if idx is None:
            idx = self._editing
        if idx is None or not (0 <= idx < len(self._profiles)):
            return
        name = str(self._profiles[idx].get("name") or "?")
        self._profiles.pop(idx)
        if self._draft == idx:
            self._draft = None
        elif self._draft is not None and idx < self._draft:
            self._draft -= 1
        self._rebuild_list()
        target = min(idx, len(self._profiles) - 1)
        self._select(target if target >= 0 else None)
        if self.dirty():
            self._fb.info(f"「{name}」已从表里摘掉——点「保存」才会写进配置。")
