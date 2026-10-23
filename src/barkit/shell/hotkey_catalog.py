"""shell/hotkey_catalog：快捷键**目录** —— 三类来源合成一份可展示的列表。

**为什么单独一层**：快捷键的名单今天散在三处，且三处的「形状」各不相同 ——

- **框架内置**：框架自己给的（打开面板 / 打开设置），今天**一条都没有**；
- **组件声明**：插件经 ``HotkeySpec`` 贡献（识别 / 一键输入 / 录屏通知各一条）；
- **用户自建**：存在配置里，今天也没有落点。

页面要的是「一份列表、每条知道自己是哪一类、能不能删」，而这三类各自的来源
（代码常量 / 贡献注册中心 / 配置段）页面都不该直接去翻 —— 那样页面就成了第三份
「谁算一条快捷键」的实现，加一类就要改页面。目录这一层把「合成」收在一处，
页面只消费 :class:`HotkeyEntry`。

**「停用」的存法为什么是空串**（不是新增一个 ``enabled`` 字段）：配置段
``hotkeys.<name>`` 是**字符串 → 字符串**的映射（``config.validate`` 强制），
而注册侧早有一条同义规则 —— ``combo`` 为空即**不注册**（``app_shell._register_hotkeys``
里那句 ``if combo and not ...``）。把「停用」写成空串就是复用这条既有语义，
**零形状变更**：用户手改过的 YAML 一个字节都不用动。另起一个布尔字段则要动
形状、且要回答「老配置里没有这个键时算启用还是停用」。
"""

from __future__ import annotations

from dataclasses import dataclass, field

SOURCE_FRAMEWORK = "framework"   # 框架内置：可改键 / 可停用，**不可删**
SOURCE_PLUGIN = "plugin"         # 组件声明：可改键 / 可停用，**不可删**
SOURCE_CUSTOM = "custom"         # 用户自建：可改键 / 可停用 / **可删**

#: 来源 → 显示名（副行里那一个词）
SOURCE_LABELS = {
    SOURCE_FRAMEWORK: "框架",
    SOURCE_PLUGIN: "组件",
    SOURCE_CUSTOM: "我的",
}

#: 筛选取值。**「全部」用空串而不是另造一个哨兵**：来源 / 状态两栏的「不限」都是
#: 同一个意思，各造一个（"all" / "*"）就要各写一次「空串算不算」的判据。
SOURCE_ALL = ""
STATE_ALL = ""
STATE_ON = "on"
STATE_OFF = "off"


#: 重挂的三种结果。**为什么不是布尔**：布尔只能答「成 / 没成」，而「按配置撤掉了绑定」
#: 与「真挂上了」都是达成了配置要求的那一头，只有第三种才是要报给用户的失败 ——
#: 合成一个 ``True`` 的结果就是「界面说已启用、实际没挂上」这种谎的来源
#: （2026-09-23 用户报「OCR 快捷键无效」，见 `log/` 那篇）。
REBIND_BOUND = "bound"        # 配置要它占键，且真的挂上了
REBIND_UNBOUND = "unbound"    # 配置要它不占键，已撤掉（停用 / 删掉都是这一档）
REBIND_FAILED = "failed"      # 配置要它占键，但没挂成（没权限 / 键位非法 / 撞车）


@dataclass
class HotkeyHost:
    """设置页要的**全部宿主能力**，一次注入（构造参数只多一个）。

    为什么不把 ``HotkeyManager`` 直接给页面：页面要的是「改完之后你帮我挂上、并告诉我
    成没成」，而「按当前配置算出该挂什么键」这件事**只有装配器知道**（要合并用户差异
    层与出厂默认值，还要认得框架动作自己的回调）。给 manager 等于让页面去翻
    ``hotkeys.<name>``，页面上就会出现第二份「一条热键由什么组成」的实现。
    """

    actions: "tuple[FrameworkAction, ...]" = ()
    #: 名字 → 重挂结果（``REBIND_*`` 之一，写盘之后由页面调用）。
    #: **不是布尔**：见那三个常量的注释。
    rebind: "object | None" = None
    #: (名字, 键位) → 占用这条键位的热键**显示名**，没撞车给 None
    conflict_of: "object | None" = None
    #: 本机热键能不能用（Quartz 在不在）；False 时页面照出、只加一条警示
    available: bool = True
    #: 触发结果的记录口（``shell.hotkey_feedback.HotkeyFeedback``）。页面靠它显示
    #: 「上次触发」—— 热键触发时设置窗通常是关着的，只能事后给它看。
    feedback: "object | None" = None
    #: 名字 → 失败原因（没挂上才有，挂上了给 None）。**启动那批热键是在没有界面的
    #: 时候注册的**：失败时没人能弹窗，只能等用户自己翻到这一页 —— 那时原因要还在。
    failure_of: "object | None" = None


@dataclass
class FrameworkAction:
    """一条**框架内置**动作：名单、说明、默认键位都在这一处。

    ``run`` 由装配器在构造期填上（``dataclasses.replace``）—— 目录这一层**不 import
    panel / settings**：它只回答「有哪些内置动作、它们叫什么、默认什么键」，
    按下之后该叫谁归装配器接线（框架动作的回调长在 ``AppShell`` 上，目录拿不到）。
    """

    name: str                 # 配置键 hotkeys.<name>
    title: str                # 显示名（列表行主位）
    description: str          # 一句话说明（副行 / 悬停）
    default_combo: str = ""   # 空 = 出厂不占键，由用户自己录
    run: "object | None" = field(default=None)


#: 框架内置动作的**封闭名单**（改这里 = 改出厂行为，需真机目测）。
#: 只有「打开面板」给了默认键位：它是唯一一个「天天用、今天只能点菜单栏」的动作。
#: 打开设置留空 —— 抢占一个用户可能已经派给别家的组合，不划算。
FRAMEWORK_ACTIONS: tuple[FrameworkAction, ...] = (
    FrameworkAction(
        name="framework.panel",
        title="打开面板",
        description="开关菜单栏那块面板，等同于点菜单栏图标",
        default_combo="cmd+shift+b",
    ),
    FrameworkAction(
        name="framework.settings",
        title="打开设置",
        description="打开本设置窗并切到「快捷键」这一页",
    ),
)


#: **触发成功后怎么反馈**，按来源推默认值（用户可逐条改，改了落盘成差异项）。
#:
#: 三种方式**标签即行为**，没有「关了其实是轻浮层」这种藏着掖着的语义：
#:
#: - ``none``（不通知）—— 成功时什么都不弹。框架 / 组件动作执行完都有看得见的结果
#:   （面板弹出、设置窗打开、识别浮层、文本被敲进输入框、录屏提示），再补一条提示
#:   是纯噪音 ⇒ 默认 ``none``。
#: - ``notify``（系统通知）—— 成功时发一条系统通知。自建跑的是一条静默的后台命令，
#:   成没成界面上多半看不见，「按了没反应」最难自查 ⇒ 默认 ``notify``。
#:
#: 失败不受这个影响 —— 失败一律发系统通知（见 ``shell.hotkey_feedback``）。
FEEDBACK_NONE = "none"        # 成功时不显示任何提示
FEEDBACK_OVERLAY = "overlay"   # 成功时弹一个 2 秒自消、不激活 app 的轻浮层
FEEDBACK_NOTIFY = "notify"     # 成功时发一条系统通知

FEEDBACK_MODES: tuple[str, ...] = (FEEDBACK_NONE, FEEDBACK_OVERLAY, FEEDBACK_NOTIFY)
FEEDBACK_LABELS: dict[str, str] = {
    FEEDBACK_NONE: "不通知",
    FEEDBACK_OVERLAY: "轻浮层",
    FEEDBACK_NOTIFY: "系统通知",
}

#: 三档**各一句话**的释义（编辑屏跟着选中项显示，见 ``ui_kit.choice_hint``）。
#:
#: 判据：这是「**你选中那一项**的释义」，不是三档的总说明。三句同时摊在界面上是
#: 84 个字的灰块，把上面那几颗控件整个淹掉（2026-09-23 观感返工）。故每句只说
#: **这一档做了什么**，三档共有的部分（「失败一律发系统通知，不受这里影响」）由
#: 编辑屏自己补一句，不在这里重复三遍。
FEEDBACK_HINTS: dict[str, str] = {
    FEEDBACK_NONE: "成功时什么都不弹",
    FEEDBACK_OVERLAY: "成功时弹一个 2 秒自消、不激活 app 的轻浮层",
    FEEDBACK_NOTIFY: "成功时发一条系统通知",
}

FEEDBACK_DEFAULTS: dict[str, str] = {
    SOURCE_FRAMEWORK: FEEDBACK_NONE,
    SOURCE_PLUGIN: FEEDBACK_NONE,
    SOURCE_CUSTOM: FEEDBACK_NOTIFY,
}

#: 自建条目的配置键前缀 + 起始序号。名字要**稳定且不与组件声明的热键名撞**：
#: 加序号而不是拿标题去 slug（中文标题 slug 出来是空串，改名还要连带改配置键）。
CUSTOM_PREFIX = "custom."


@dataclass
class CustomSpec:
    """一条**用户自建**快捷键（草稿态也是它：编辑屏来回改的就是这一份）。

    ``combo`` 与 ``feedback`` 不在 ``custom_hotkeys`` 段里：键位的真源是
    ``hotkeys.<name>``（注册 / 重挂 / 冲突三条链都读它），反馈方式的真源是
    ``hotkey_feedback.<name>``（三类条目共用那一套读写）。这里把它们凑齐只为了方便
    编辑屏一次拿全 —— 落盘时按各自的真源拆开写。
    """

    name: str = ""
    title: str = ""
    command: str = ""
    combo: str = ""
    feedback: str = FEEDBACK_NOTIFY


@dataclass(frozen=True)
class HotkeyEntry:
    """**一条**快捷键的展示数据（页面只消费这个）。

    ``combo`` 是**当前生效**键位（已合并用户差异层），空串 = 停用 / 未设置；
    ``default_combo`` 是出厂键位 —— 两者相等意味着「用户没改过」，写盘时按
    「默认值不落盘」的纪律**删键**而不是写一遍默认值。
    """

    name: str
    title: str
    source: str
    owner: str            # 归属显示名：框架 / 组件名 / 我的
    description: str
    combo: str
    default_combo: str = ""
    command: str = ""     # 只有自建条目有：触发时跑什么
    feedback: str = FEEDBACK_NONE  # 触发成功后的反馈方式（**当前生效值**，见 FEEDBACK_*）
    #: 组件自己声明的主图标（``ComponentSpec.icon`` 的**取值字符串**，如 ``"scan"``）。
    #: 框架与自建条目留空 —— 那两类的图标由页面按来源挑一枚固定的（见 hotkeys_page
    #: 的 ``_icon_of``）。存字符串不存枚举：目录这一层对贡献侧一律鸭子类型，不 import
    #: 那套声明类型（同 ``getattr(spec, "plugin_id", "")`` 的理由）。
    icon: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.combo)

    @property
    def deletable(self) -> bool:
        """**只有用户自建的能删**：框架与组件的是声明出来的，删了下回启动又长回来
        （组件卸载由它自己的清理流程删配置键，不走这里）。"""
        return self.source == SOURCE_CUSTOM

    @property
    def resettable(self) -> bool:
        """能不能「初始化」（恢复出厂键位）。

        自建条目没有出厂键位可言（它的出厂态就是不存在），所以只有前两类有；
        已经等于出厂值的也没有可恢复的东西 —— 按钮此时该是**禁用**的，
        而不是点了回一句「你没改过」。
        """
        return self.source in (SOURCE_FRAMEWORK, SOURCE_PLUGIN) and self.modified

    @property
    def modified(self) -> bool:
        """键位被改过（含被停用）：初始化按钮的可用性就看这一个判据。"""
        return self.combo != self.default_combo

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source, self.source)

    @property
    def feedback_default(self) -> str:
        return FEEDBACK_DEFAULTS.get(self.source, FEEDBACK_NONE)


def collect(
    *,
    config,
    hotkeys: "tuple | list" = (),
    component_names: "dict[str, str] | None" = None,
    component_icons: "dict[str, str] | None" = None,
    actions: "tuple[FrameworkAction, ...]" = FRAMEWORK_ACTIONS,
) -> list[HotkeyEntry]:
    """把三类来源合成一份列表（**顺序 = 展示顺序**：框架 → 组件 → 自建）。

    组件那条取 ``label`` 作显示名，没写 label 的回落到**组件名**（今天识别与一键输入
    都没写 label，直接把 ``name`` 摆出来会显示成 ``recognize`` 这种内部键名）。

    ``component_icons`` 是「插件 id → 该组件的主图标」，页面拿它让每一行**长成它来源
    的样子**（识别的那几条用扫描图标、蓝牙的用蓝牙图标）；不传就一律空，页面自行
    回落（2026-09-24）。
    """
    names = component_names or {}
    icons = component_icons or {}
    out: list[HotkeyEntry] = []

    # ``config`` 为 None（未装配 / 构造期预览）时**照出内置那几条**，只是拿不到
    # 用户差异：页面要能在一个没有配置对象的环境里被画出来（设置窗的构造顺序
    # 允许它早于配置注入），而不是抛一个 AttributeError 让整个窗起不来。
    def feedback_of(name: str, source: str) -> str:
        default = FEEDBACK_DEFAULTS.get(source, FEEDBACK_NONE)
        return config.hotkey_feedback(name, default) if config is not None else default

    for action in actions:
        out.append(HotkeyEntry(
            name=action.name,
            title=action.title,
            source=SOURCE_FRAMEWORK,
            owner=SOURCE_LABELS[SOURCE_FRAMEWORK],
            description=action.description,
            combo=effective_combo(config, action.name, action.default_combo),
            default_combo=action.default_combo,
            feedback=feedback_of(action.name, SOURCE_FRAMEWORK),
        ))

    for spec in hotkeys:
        owner = names.get(getattr(spec, "plugin_id", ""), "") or getattr(spec, "plugin_id", "")
        out.append(HotkeyEntry(
            name=spec.name,
            # label 是可选的：没写就用组件名，别把配置键名当标题给人看
            title=spec.label or owner or spec.name,
            source=SOURCE_PLUGIN,
            owner=owner,
            description=spec.label or "",
            combo=config.get(f"hotkeys.{spec.name}", spec.default_combo) or "",
            default_combo=spec.default_combo or "",
            feedback=feedback_of(spec.name, SOURCE_PLUGIN),
            icon=icons.get(getattr(spec, "plugin_id", ""), ""),
        ))

    for spec in (read_customs(config) if config is not None else []):
        out.append(HotkeyEntry(
            name=spec.name,
            title=spec.title or spec.name,
            source=SOURCE_CUSTOM,
            owner=SOURCE_LABELS[SOURCE_CUSTOM],
            # 副行放命令：自建条目**除了一条命令**没有别的自我介绍，
            # 而「按下去会跑什么」正是它唯一需要被核对的东西。
            description=spec.command,
            combo=spec.combo,
            command=spec.command,
            feedback=spec.feedback,
        ))
    return out


# ---- 用户自建：读写 ----

def read_customs(config) -> list[CustomSpec]:
    """读出全部自建条目（**按 id 自然序**，不按 YAML 里的书写顺序）。

    排序不是排版偏好：没有它，用户在 YAML 里把两条换个位置，界面列表就跟着重排，
    而用户改的是**文件内容**，不是「我想换个顺序」。
    """
    out = [
        CustomSpec(
            name=name,
            title=item.get("title", ""),
            command=item.get("command", ""),
            combo=config.get(f"hotkeys.{name}", "") or "",
            feedback=config.hotkey_feedback(name, FEEDBACK_DEFAULTS[SOURCE_CUSTOM]),
        )
        for name, item in config.custom_hotkeys().items()
    ]
    out.sort(key=lambda s: _custom_sort_key(s.name))
    return out


def _custom_sort_key(name: str) -> tuple:
    """``custom.2`` 排在 ``custom.10`` 前面（纯字符串序会反过来）。"""
    tail = name[len(CUSTOM_PREFIX):]
    if tail.isdigit():
        return (0, int(tail), "")
    return (1, 0, name)


def next_custom_name(config) -> str:
    """下一个空位 id。**跳号不复用**：删掉 2 之后，下一条仍是 3 而不是回去填 2 ——
    复用空号会让「这条配置键指过别的东西」，日志与旧备份里对不上。"""
    used = set(config.custom_hotkeys())
    i = 1
    while f"{CUSTOM_PREFIX}{i}" in used:
        i += 1
    return f"{CUSTOM_PREFIX}{i}"


def write_custom(config, spec: CustomSpec) -> str:
    """落一条自建条目（``name`` 为空 = 新增），返回落定的配置键名。

    三个字段各写各的真源（见 :class:`CustomSpec`），不塞进一个大对象：
    键位归 ``hotkeys``、反馈方式归 ``hotkey_feedback``，只有标题与命令归本段。
    """
    name = spec.name or next_custom_name(config)
    config.set_custom_hotkey(name, {"title": spec.title, "command": spec.command})
    write_combo(config, name, spec.combo, "")
    config.set_hotkey_feedback(name, spec.feedback, FEEDBACK_DEFAULTS[SOURCE_CUSTOM])
    return name


def remove_custom(config, name: str) -> None:
    """删一条自建条目：**先清键位与反馈，再删本体** —— 反过来的话中间任何一步
    失败都会留下「有名无实」的孤儿配置键。"""
    default = FEEDBACK_DEFAULTS[SOURCE_CUSTOM]
    config.set_hotkey_feedback(name, default, default)
    config.remove_hotkeys([name])
    config.remove_custom_hotkey(name)


def write_feedback(config, name: str, value: str, default: str) -> None:
    """写「成功后反馈方式」（等于来源默认值就删键）。"""
    config.set_hotkey_feedback(name, value, default)


def reset_combo(config, name: str) -> None:
    """初始化：删掉键位差异项，回到出厂键位（不是写一遍默认值）。"""
    config.remove_hotkeys([name])


def write_combo(config, name: str, combo: str, default: str = "") -> None:
    """落一条键位（**等于出厂值就删键**，不让默认值进用户配置文件）。

    空串在这里是「停用」，是**要落盘的**一个值（不删键）—— 它与「没改过」的区别
    就靠「键在不在」表达：键在且为空 = 用户明确停用；键不在 = 用出厂值。

    **读侧必须配 :func:`effective_combo`**：本函数把「恢复出厂键位」落成**删键**，
    而读侧若把「缺键」当成空串，就会把「启用」读成「停用」（见那边的 docstring）。
    """
    if combo and combo == default:
        config.remove_hotkeys([name])
        return
    config.set_hotkey(name, combo)


def effective_combo(config, name: str, default: str = "") -> str:
    """``hotkeys.<name>`` 的**当前生效键位**：没改过就用出厂值，改过就用改的那个。

    **空串与缺键是两件事，全仓只在这一处区分**：

    - 键在且为空串 = 用户**明确停用** ⇒ 不占键（写侧 :func:`write_combo` 会把它落盘）；
    - 键不在 = 没改过 ⇒ 用出厂值（「等于出厂值就删键」正是这么落盘的）。

    三条链（启动注册 / 目录展示 / 改完重挂）**都必须问这一处**。各自写一次兜底就会
    在「恢复出厂键位」这条路上分叉：写侧恢复＝删键，读侧若拿空串当兜底，就把「启用」
    读成「停用」⇒ 撤掉绑定还回报成功，界面说「已启用」而这一条在当前进程里再也没挂上
    ——（2026-09-23 用户报「OCR 快捷键无效」的现场，见 `log/` 那篇）。
    """
    if config is None:
        return default
    value = config.get(f"hotkeys.{name}", None)
    if value is None:
        return default
    return value or ""


# ---- 显示 ----

#: 修饰键 → macOS 字形 + **显示顺序**（⌃⌥⇧⌘ 是系统约定的排法，不是字母序）
_MODIFIER_GLYPHS: tuple[tuple[str, str], ...] = (
    ("ctrl", "⌃"),
    ("alt", "⌥"),
    ("option", "⌥"),
    ("shift", "⇧"),
    ("cmd", "⌘"),
)


#: 键名 → 显示字形。**只列「照抄英文会读不懂」的那几个**（标点与控制键）：
#: 字母数字直接大写即可，写进这张表反而是把一张 60 项的表抄成 60 行。
#: 取值与 `core/hotkeys._KEYCODES` 的键名对齐（那边按名查 keycode，这边按名查字形）。
KEY_GLYPHS: dict[str, str] = {
    "comma": ",", "period": ".", "slash": "/", "semicolon": ";", "quote": "'",
    "lbracket": "[", "rbracket": "]", "backslash": "\\",
    "minus": "-", "equal": "=", "grave": "`",
    "space": "␣", "return": "↩", "esc": "⎋", "delete": "⌫", "tab": "⇥",
    "left": "←", "right": "→", "up": "↑", "down": "↓",
}


def display_combo(combo: str) -> str:
    """``"ctrl+alt+o"`` → ``"⌃⌥O"``；空串给「未设置」。

    字形而不是 "Ctrl+Alt+O"：菜单栏 app 的快捷键一律按系统写法印（与系统设置里
    那种一致），且短 —— 行里留给键位的横向空间只够四五个字符。
    """
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        return "未设置"
    head = ""
    for mod, glyph in _MODIFIER_GLYPHS:
        if mod in parts[:-1] and glyph not in head:
            head += glyph
    key = parts[-1]
    return head + KEY_GLYPHS.get(key, key.upper())
