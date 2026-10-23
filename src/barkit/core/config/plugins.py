"""``_PluginMixin``：插件段启停 / 排序 / 读-改-写 / 恢复默认 / 显示位开关。

``config.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations

from typing import Callable

from .consts import PLUGIN_SECTION_FRAMEWORK_KEYS, PLUGIN_UI_SLOTS, slot_default_visible
from .store import _load_yaml


class _PluginMixin:
    """插件那一段的读写：启停持久化、组件间排序、段级「读—改—写」（规范 §6.3 的唯一路径）、恢复默认配置，以及 D4 的显示位用户层开关。

    与主类同体：方法经 ``self`` 取用实例状态，故拆到混合类不改行为。
    """

    # ---------- §9.5 插件 schema 注入 ----------

    def set_plugin_schemas(self, schemas: dict) -> None:
        """注入各插件的配置 schema（FormSchema），触发一次重载。

        单一真源（§9.5）：注入后插件默认值由 ``schema.defaults()`` 合成，不再依赖
        ``config.default.yaml`` 的插件段。装配根在 ``load_all`` 后调用一次；
        测试可构造 ``FormSchema`` 直接注入，无需真实插件实例。
        """
        self._plugin_schemas = dict(schemas)
        self._reload_from_disk()

    # ---------- 插件启停持久化（E3 ②） ----------

    def _read_user_raw(self) -> dict:
        """读取用户配置原始内容（不做与 defaults 的合并），缺失返回 {}。"""
        if not self.user_path.exists():
            return {}
        return _load_yaml(self.user_path)

    def set_plugin_enabled(self, plugin_id: str, enabled: bool) -> None:
        """持久化单个插件的启用状态：写入 ``plugins.<id>.enabled``。

        只写该插件的 ``enabled`` 键，deep_merge 时不会覆盖 defaults 中同插件的
        其他配置（如 poll / ocr），故禁用后重新启用不会丢失默认设置。
        """
        user = self._read_user_raw()
        section = user.get("plugins")
        if not isinstance(section, dict):
            section = {}
            user["plugins"] = section
        existing = section.get(plugin_id)
        if not isinstance(existing, dict):
            existing = {}
        existing["enabled"] = enabled
        section[plugin_id] = existing
        self.save_user(user)

    def remove_plugin_section(self, plugin_id: str) -> None:
        """卸载插件时删除其配置段（含 enabled 及所有子键）。"""
        user = self._read_user_raw()
        section = user.get("plugins")
        if isinstance(section, dict) and plugin_id in section:
            del section[plugin_id]
            if not section:
                user.pop("plugins", None)
            self.save_user(user)

    # ---------- 插件排序（§6.4，批次 6a） ----------

    def plugin_order(self) -> list[str]:
        """插件间排序唯一数据源：config 顶层 ``plugin_order``（用户拖拽持久化）。

        缺省/非法项过滤后返回；仅作为「用户显式排序」，未列入的插件按
        ``Plugin.order`` 默认插入位排在其后（排序规则见 ContributionRegistry）。
        """
        po = self.get("plugin_order", [])
        if isinstance(po, list):
            return [x for x in po if isinstance(x, str)]
        return []

    def set_plugin_order(self, order: list[str]) -> None:
        """持久化插件间排序（用户在设置页拖拽/移动后调用，原子写顶层键）。"""
        user = self._read_user_raw()
        user["plugin_order"] = [str(x) for x in order]
        self.save_user(user)

    def remove_from_plugin_order(self, plugin_id: str) -> None:
        """卸载插件时移除排序项（§9.1 第 3.5 步），避免孤儿 id 污染排序。"""
        order = self._read_user_raw().get("plugin_order")
        if isinstance(order, list) and plugin_id in order:
            self.set_plugin_order([x for x in order if x != plugin_id])

    # ---------- 插件配置段整体写（导出/导入恢复用，批次 6c） ----------

    def set_plugin_section(self, plugin_id: str, section: dict) -> None:
        """整体写 ``plugins.<id>`` 段（覆盖用户差异层；不触及其他插件）。"""
        user = self._read_user_raw()
        plugins = user.get("plugins")
        if not isinstance(plugins, dict):
            plugins = {}
            user["plugins"] = plugins
        existing = plugins.get(plugin_id)
        merged = existing if isinstance(existing, dict) else {}
        merged.update(section)
        plugins[plugin_id] = merged
        self.save_user(user)

    # ---------- 插件段「读—改—写」（插件结构规范 §6.3，阶段 0 收口） ----------

    def user_plugin_section(self, plugin_id: str) -> dict:
        """``plugins.<id>`` **用户差异段**的只读快照（返回副本，改它不落盘）。

        与 :meth:`update_plugin_section` 是**同一条读-改-写路径**的两半：读侧供
        「先判断要不要写」的场景（如 ipad_tunnel 的一次性升级迁移：只有当用户段
        里真的存在旧键时才写，否则每次启动都白写一遍文件）。

        注意读的是**用户差异层**（不合并 defaults）——「用户有没有显式改过这个键」
        这个信息只存在于差异层，合并后的 ``Config.get()`` 给不出来。
        """
        plugins = self._read_user_raw().get("plugins")
        section = plugins.get(plugin_id) if isinstance(plugins, dict) else None
        return dict(section) if isinstance(section, dict) else {}

    def update_plugin_section(self, plugin_id: str, mutate: Callable[[dict], None]) -> None:
        """对 ``plugins.<id>`` **用户差异段**做「读—改—写」（原子）。

        为什么需要它（规范 §6.3）：插件要改的常是**单个嵌套键**（如
        ``plugins.route_guard.paused``），而 ``set_plugin_section()`` 要求调用方提供
        **完整段落**、读侧的 ``_read_user_raw()`` 又是私有——于是插件侧各自手搓
        「读整份用户 YAML → 定位子段 → 改 → ``save_user``」，全仓散落 18 处。
        本方法与 :meth:`user_plugin_section` 合起来是**唯一**的读-改-写路径：插件
        用户态与将来 ``schema`` 渲染器的写回都必须走它，不得各写一套（两套写回
        迟早分叉）。

        语义：

        - 读**原始用户 YAML**（不合并 defaults）→ 取 ``plugins.<id>``（缺失传空 dict）
          → ``mutate(section)`` 就地修改 → 原子写 + chmod 600 → 重载；
        - ``mutate`` **抛异常则不落盘**（异常向上传播，由调用方决定怎么提示）；
        - 改完段落为空时**删除该键**（不留 ``plugins.<id>: {}`` 这种残渣），
          ``plugins`` 随之变空时一并删除。

        ``mutate`` 的返回值被忽略——它只被用来就地修改传入的 dict。
        """
        user = self._read_user_raw()
        plugins = user.get("plugins")
        if not isinstance(plugins, dict):
            plugins = {}
            user["plugins"] = plugins
        existing = plugins.get(plugin_id)
        section = existing if isinstance(existing, dict) else {}
        mutate(section)
        if section:
            plugins[plugin_id] = section
        else:
            plugins.pop(plugin_id, None)
            if not plugins:
                user.pop("plugins", None)
        self.save_user(user)

    # ---------- 配置初始化：组件配置恢复默认（§9.5 的初始化入口） ----------

    def clearable_plugin_keys(self, plugin_id: str) -> list[str]:
        """「恢复默认配置」会清掉的键名（只读；供界面**如实预告**要动什么）。

        没有它，界面上那句确认就只能是「确定恢复默认？」—— 用户签的是一张空头支票，
        而这一步**不可撤销**（不作备份）。所以清单先算出来给他看。

        顺序＝用户 YAML 里的书写顺序（``_read_user_raw`` 回来的是 dict，Python 3.7+
        保序）：照抄给用户，不替他重排 —— 他看到的那几行与文件里的那几行对得上。
        """
        section = self.user_plugin_section(plugin_id)
        return [k for k in section if k not in PLUGIN_SECTION_FRAMEWORK_KEYS]

    def reset_plugin_section(self, plugin_id: str) -> list[str]:
        """把 ``plugins.<id>`` **用户差异段**恢复为「没写过」；返回被清掉的键名。

        这是「配置初始化」的**唯一**写入口：清空用户差异层之后，该组件的配置就剩
        「框架默认（``config.default.yaml`` 里那一份）＋ 组件 ``config_schema()`` 声明的
        默认值」—— 正是新装用户的起点。它同时是**配置被写脏时的逃生门**：段里留着一个
        新版程序不认识的旧键（旧版本 BarKit 自己写下的那些），此前只能手改 YAML，
        因为在界面里根本看不见它。

        语义边界（三条，都是当场定的口径）：

        - **保留** :data:`PLUGIN_SECTION_FRAMEWORK_KEYS`（今天只有 ``enabled``）：
          见那个常量的说明 —— 清掉它等于替用户改运行状态；
        - **整段清零**而不是「只删不认识的键」：判据是「用户差异层里还有没有东西」。
          只删不认识的键得先把「认识」判定一遍（＝在框架里重造一遍组件的校验），
          两处判据迟早分叉；何况「恢复默认」这四个字本来就说的是全部；
        - **没有可清的东西就不写盘**（返回空列表）：白写一遍文件会改 mtime，把文件监视
          那条路无谓地叫醒一次。

        清完之后段落为空则**删键**（不留 ``plugins.<id>: {}`` 这种残渣），``plugins``
        随之变空时一并删除 —— 与 :meth:`update_plugin_section` 的收尾同一口径。

        **不广播** ``changed``：本方法是纯写入（与 :meth:`set_plugin_poll` 等一致），
        「写完之后界面与组件该知道什么」归调用方 —— 框架侧见
        ``PluginManager.reset_plugin_config``。
        """
        user = self._read_user_raw()
        plugins = user.get("plugins")
        section = plugins.get(plugin_id) if isinstance(plugins, dict) else None
        if not isinstance(section, dict):
            return []
        removed = [k for k in section if k not in PLUGIN_SECTION_FRAMEWORK_KEYS]
        if not removed:
            return []
        kept = {k: v for k, v in section.items() if k in PLUGIN_SECTION_FRAMEWORK_KEYS}
        if kept:
            plugins[plugin_id] = kept
        else:
            del plugins[plugin_id]
            if not plugins:
                user.pop("plugins", None)
        self.save_user(user)
        return removed

    # ---------- 组件显示位：用户层开关（契约规范 §2.4，决议 D4） ----------

    def plugin_ui_visible(self, plugin_id: str, slot: str) -> bool:
        """用户层显示位开关：``plugins.<id>.ui.<slot>.visible``（缺省看**该位的默认**）。

        默认值**逐位不同**（:data:`PLUGIN_UI_SLOT_DEFAULT`，决议 D37）：标题栏 / 列表行 /
        托盘角标沿用「声明即出现」，**快捷区域默认关** —— 它是快捷方式，组件的入口在
        行上已经有了（D36），底部那排摆哪几个由用户自己挑。故只在**用户显式改过**时
        返回盘上的值；非布尔 / 结构损坏一律按该位的默认（既不静默把可见性调高、也不
        静默调低 —— 与 ``tray_min_level`` 同一个口径）。

        三条语义约束（决议 D4）：① 隐藏 ≠ 停用——只影响该显示位的渲染，**不影响
        状态聚合、托盘徽标、轮询与副作用**；② 隐藏不删 ``plugin_order`` 位（重新
        显示时顺序不变）；③ 允许全隐藏，框架不自动回退。

        ``slot`` 取 :data:`PLUGIN_UI_SLOTS`；未知取值一律按可见处理（读侧不抛错，
        避免用户手改 YAML 写错一个键就让面板整体起不来）。
        """
        default = slot_default_visible(slot)
        value = self.get(f"plugins.{plugin_id}.ui.{slot}.visible", default)
        return value if isinstance(value, bool) else default

    def set_plugin_ui_visible(self, plugin_id: str, slot: str, visible: bool) -> None:
        """写显示位开关（走 ``update_plugin_section`` 的同一条读-改-写路径）。

        **等于该位的默认值就删键**，不写 ``true`` 也不写 ``false``：盘上只留与默认不同
        的那一份，用户改回默认后 YAML 干净（与 ``set_tray_base_source`` 传空串删键同理）。
        判据因此不是「``visible`` 为真」而是「``visible`` 是否等于该位的默认」—— 逐位
        不同的默认值（:data:`PLUGIN_UI_SLOT_DEFAULT`）下这两句话不再等价：快捷区域默认
        关，把它**打开**才是要落盘的那一次。删键后随之变空的 ``ui`` / ``<slot>``
        逐层清理。

        ``slot`` 非法在**调用期**报错（写侧宁可早 fail，不静默落一个谁也读不到的键）。
        """
        if slot not in PLUGIN_UI_SLOTS:
            raise ValueError(f"未知显示位：{slot!r}；合法值 {PLUGIN_UI_SLOTS}")
        default = slot_default_visible(slot)

        def _mutate(section: dict) -> None:
            ui = section.get("ui")
            if not isinstance(ui, dict):
                ui = {}
            slot_cfg = ui.get(slot)
            if not isinstance(slot_cfg, dict):
                slot_cfg = {}
            if visible == default:
                slot_cfg.pop("visible", None)
            else:
                slot_cfg["visible"] = visible
            if slot_cfg:
                ui[slot] = slot_cfg
            else:
                ui.pop(slot, None)
            if ui:
                section["ui"] = ui
            else:
                section.pop("ui", None)

        self.update_plugin_section(plugin_id, _mutate)
