"""全局测试隔离：任何测试都不得触碰真实的 ~/Library/Application Support/BarKit。

背景（代码评审后发现的真实数据损坏 bug）：
`barkit.paths` 原先在导入时把 ROUTES_FILE / TUNNEL_FILE / STATE_FILE 等硬编码成
真实用户目录下的绝对路径，且**只认本进程里的模块属性补丁** ⇒ 子进程 / ``python -c`` /
任何非 pytest 入口完全逃逸。于是任何调用 `tunnel_spec.write_tunnel()` 或
`intent.write_intent()` 的测试（或它拉起的子进程）都会把测试夹具写进真实的
tunnel.json / routes.json，覆盖用户当前配置——表现为"运行测试后 iPad 隧道显示未激活、
流量改走 Wi-Fi"（2026-09-29 真机复现）。

**两道闸**（2026-09-29 起）：

1. **数据根由 ``BARKIT_DATA_DIR`` 决定**（``barkit.paths`` 导入期读它）—— 本文件顶部把
   它设成会话 tmp，于是**跨进程**也一致：子进程只是继承一个环境变量。
2. **每用例再把 ``barkit.paths`` 的模块属性重定向到 per-test tmp**（见下方
   ``PATH_REDIRECTS``）—— 本进程内的强隔离，语义不变。

**隔离口径（单一真源）**：生产代码一律经 ``from barkit import paths`` + 调用期属性读取
来拿路径，**禁止 ``from barkit.paths import <Path 常量>```**——按值导入会在模块导入期
冻结路径，使第 2 道闸静默失效。该约束由
``tests/test_architecture.py::test_no_by_value_path_imports`` 强制，
新增路径常量无需改本文件（那条门禁从 ``vars(paths)`` 运行时派生受管名单）。

同样禁止把路径写成**函数默认参数值**（``def f(p: Path = paths.X)``）：默认参数在
函数定义期求值，与本固件同归于尽。见 ``test_no_path_constant_default_args``。

**测试专用端口隔离（2026-09-20 重做）**：ipad_tunnel 的中继在测试里会按**配置**去 bind 本机
监听端口（``plugins.ipad_tunnel.local_port``，出厂默认 1080 ＝ 主程序隧道端口）；占着它，测试
进程残留时主程序隧道就起不来（见 2026-09-17 排查）。原先那条固件设环境变量
``BARKIT_IPAD_TUNNEL_TEST_PORT``，而读端口的代码是「配置里没有才看环境变量」——§9.5 之后配置里
**恒有**该键 ⇒ **那条通道从来没生效过**（2026-09-20 实测：env=54321，合并后配置仍 1080、中继
实读 1080）。现改为在**注入内置 schema 时替换该键的默认值**（配置的默认值层），两条注入路径
（``Config.__init__`` / ``Config.set_plugin_schemas``）都覆盖——后者是 ``shell/app_shell.assemble``
走的，也是唯一真会把中继跑起来的那条。自证 →
``tests/test_ipad_tunnel.py::test_test_session_moves_relay_off_the_app_port``。
"""

import os
import tempfile

# ── 数据根的**会话级**兜底（跨进程的那一半，2026-09-29 加）────────────────
#
# ``barkit.paths`` 自 2026-09-29 起在**导入期**读 ``BARKIT_DATA_DIR``（与 helper 同名同义），
# 于是「隔离」对子进程 / ``python -c`` / 任何非 pytest 入口一律生效 —— 它们拿不到本进程
# 的 monkeypatch，只能靠环境变量。这一句必须排在**任何 barkit 导入之前**（本文件顶部）：
# 模块导入期求值的东西，事后设环境变量已经来不及。
#
# 它兜的是「游离在用例之外」的窗口（收集期、session 级 fixture、别的测试文件里的子进程）：
# 那些时刻 `_isolate_barkit_data_dir` 还没跑，而真实用户目录必须一次都碰不到。
# 具体路径由**每个用例**的 fixture 再覆盖一次（per-test tmp，语义不变）。
os.environ.setdefault("BARKIT_DATA_DIR", tempfile.mkdtemp(prefix="barkit-test-home-"))

# ── Qt 平台变量：全仓**唯一**的一处默认值（2026-09-28 收口）─────────────
#
# 必须设在**本文件最顶部**：pytest 在**收集阶段**就 import 所有 test 模块，而 Qt 只在
# 第一个 ``QApplication`` 构造时读一次 ``QT_QPA_PLATFORM``。放在这里 ⇒ 无论跑单文件还是
# 全量，平台都一致；原先由三个测试模块各自 ``os.environ.setdefault`` 兜底（模块级两处 ＋
# 某用例函数体内一处），于是**同一个测试文件跟谁一起跑决定它跑在哪个平台**：
# ``pytest tests/test_settings.py`` 单跑（没人设过）⇒ 真跑 cocoa ⇒ macOS 上 3 条假红、
# Windows 上 ``test_combo_field_lets_the_wheel_scroll_the_page`` 一条假红；与
# ``test_shell_design.py`` 同跑即全绿。
#
# 用 ``setdefault`` 而**不是**直接赋值：显式传入的环境变量仍然优先，故意跑 cocoa 的
# 退路保留（`QT_QPA_PLATFORM=cocoa pytest …`）。单一真源由
# ``test_architecture.py::test_qt_platform_default_has_a_single_source`` 守。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402


# ── 测试专用端口：隔离 ipad_tunnel 中继，别占主程序隧道端口 ─────────────

_TUNNEL_TEST_PORT: int | None = None


def _test_session_tunnel_port() -> int:
    """本次测试会话用的空闲本机端口（会话内固定）。

    ``bind(0)`` 拿内核分配的空闲端口号后**立刻释放**——只借号、不占着，中继到真正要用的
    那一刻才 bind。会话内恒定，免得同一进程里各用例读到不同端口。
    """
    global _TUNNEL_TEST_PORT
    if _TUNNEL_TEST_PORT is None:
        import socket

        s = socket.socket()
        try:
            s.bind(("127.0.0.1", 0))
            _TUNNEL_TEST_PORT = int(s.getsockname()[1])
        finally:
            s.close()
    return _TUNNEL_TEST_PORT


def _retarget_relay_port(schemas: dict) -> dict:
    """把内置 schema 里 ipad_tunnel 的**本机监听端口**默认值换成会话空闲端口。

    为什么改 schema 而不设环境变量：中继读端口的判据是 ``cfg.get("local_port", <兜底>)``，
    而 §9.5 之后配置里**恒有**该键 ⇒ 兜底那一支永远走不到，环境变量形同虚设（见本文件
    模块 docstring）。配置的**默认值层**才是唯一真能生效的地方。

    只碰 ``ipad_tunnel.local_port`` 一个键，其余字段原样；返回新对象，**不改调用方传进来
    的 dict**——生产 schema 若被判成「被改过的」，发货语义就说不清了。
    """
    import dataclasses

    section = schemas.get("ipad_tunnel")
    if section is None:
        return schemas
    port = _test_session_tunnel_port()
    hit = False
    fields = []
    for f in section.fields:
        if f.key == "local_port":
            fields.append(dataclasses.replace(f, default=port))
            hit = True
        else:
            fields.append(f)
    if not hit:                  # 该插件没声明这个键：原样返回（防呆，不静默造字段）
        return schemas
    out = dict(schemas)
    out["ipad_tunnel"] = dataclasses.replace(section, fields=tuple(fields))
    return out


# ── §9.5 默认值真源：测试侧兜底 ────────────────────────────────────

_builtin_schemas_cache = None


# ── 路径重定向表（**覆盖性有门禁守**，见下方 ``PATH_REDIRECTS``）─────────

#: 常量名 → 相对本会话 tmp 根的子路径（``""`` ＝ 根本身）。
#:
#: **必须覆盖 ``barkit.paths`` 里每一个 Path 型常量**（``APP_NAME`` 是 str，天然在外）。
#: 漏一项不会报错 —— 那一条路径在测试里仍指向**真实用户目录**，于是「跑一次测试」就
#: 往用户数据里真写了一份，而多数用例根本不读那条路径，症状要过很久才浮现。
#: 2026-09-23 新增 bt_guard 的两个通信文件时就是这么漏的（当时靠人眼比对才发现）。
#: 覆盖性由 ``tests/test_architecture.py::test_every_path_constant_is_redirected`` 守，
#: **新增路径常量而不登记本表 ⇒ 该门禁红**。
PATH_REDIRECTS: dict[str, str] = {
    # 两个**根**：Config 默认 user_path、A 类 root 二进制部署点、
    # 日志目录等一律在调用期从它们派生，故必须一并重定向。
    "APP_SUPPORT": "",
    "LOG_DIR": "logs",
    #: LaunchAgent 的 stdout/stderr 落点（core/autostart.py 的 plist 里指它）。
    #: 不重定向的话，测试里生成 plist 会把真实用户日志目录的路径写进断言对象。
    "STARTUP_LOG": "logs/barkit-startup.log",
    #: 会话客户端节点（通道换会话之后，本表里唯一的「通道」类路径）。
    "HELPER_SOCKET": "helper.sock",
    "HOSTS_ENTRIES_FILE": "hosts_entries.json",
    #: 守护自己那份日志（root 写、用户可读）。不重定向的话测试里去读它就会读到
    #: 真实守护那份，判据变成「取决于本机装没装守护」。
    "HELPER_LOG": "helper.log",
    # 蓝牙助手那条会话的节点（2026-10-04 起）：它是**跨语言**的（助手源码里硬编码了
    # 同一个路径），在测试里必须重定向，否则会去连真实用户目录下的节点。
    "BT_HELPER_SOCKET": "bt_helper.sock",
    "EXTERNAL_PLUGINS_DIR": "plugins",
}


def _builtin_plugin_schemas() -> dict:
    """全部内置插件的 ``config_schema()``（每次会话只算一次）。

    ``Config(create_user_file=False)``：**只读**构造，不碰真实用户家目录。
    ``cls.__new__(cls)``：``config_schema()`` 只读类属性 ``id``，不需要跑
    ``Plugin.__init__``（那会绑定 ctx）。
    """
    global _builtin_schemas_cache
    if _builtin_schemas_cache is None:
        from barkit.core.config import Config
        from barkit.core.registry import Registry

        found = Registry(Config(create_user_file=False)).discover()
        raw = {cid: cls.__new__(cls).config_schema() for cid, cls in found.items()}
        # 端口改写落在这一处：这份字典既是兜底注入的来源，也是测试侧唯一的内置 schema，
        # 两条注入路径共用它（见 _inject_builtin_plugin_schemas）。
        _builtin_schemas_cache = _retarget_relay_port(raw)
    return _builtin_schemas_cache


@pytest.fixture(autouse=True)
def _inject_builtin_plugin_schemas(monkeypatch):
    """让测试里构造的 ``Config`` 像生产一样带上内置插件的 schema。

    **为什么需要**：§9.5 起，插件业务键的**默认值真源是插件自己的**
    ``config_schema()``，出厂文件 ``config.default.yaml`` 只留框架保留键
    （``plugins.<id>.enabled``）。生产路径由 ``app_shell.assemble`` 在
    ``registry.load_all()`` 之后注入；而测试常直接 ``Config(user_path=tmp)``
    手搓一个（不实例化插件）—— 不注入就整体读不到插件默认值，症状是
    「配置里没有 office 这个场景」这类看着毫不相干的怪错（2026-09-20 那次
    默认值迁移同时转红 33 条用例，根因就是它）。

    **这只是测试侧兜底，不是生产行为**：生产代码读插件配置前必须自己注入，
    漏了会静默丢默认值 —— 那条契约由
    ``test_config.py::test_plugin_defaults_need_schema_injection`` 明示守着。

    显式传了 ``plugin_schemas``（含显式传 ``{}``）的调用方不受影响。

    **两条注入路径都要盖**：除构造期之外，``Config.set_plugin_schemas``（生产装配根
    ``app_shell.assemble`` 走的显式注入，也是唯一真会把中继跑起来的路径）同样过一遍端口
    改写——只补构造期会漏掉它，隔离又是假的。
    """
    from barkit.core.config import Config

    original_init = Config.__init__
    original_set_schemas = Config.set_plugin_schemas
    builtin = _builtin_plugin_schemas()   # 必须在打补丁**之前**算（它会自己构造 Config）

    def patched(self, *args, **kwargs):
        # 签名是 (user_path, default_path, plugin_schemas, create_user_file)：
        # 只在「前三个位置参数没占满、且没用关键字」时兜底。
        if len(args) < 3 and "plugin_schemas" not in kwargs:
            kwargs["plugin_schemas"] = builtin
        original_init(self, *args, **kwargs)

    def patched_set_schemas(self, schemas):
        original_set_schemas(self, _retarget_relay_port(dict(schemas)))

    monkeypatch.setattr(Config, "__init__", patched)
    monkeypatch.setattr(Config, "set_plugin_schemas", patched_set_schemas)
    yield


@pytest.fixture(autouse=True)
def _isolate_barkit_data_dir(monkeypatch, tmp_path):
    base = tmp_path / "barkit-data"
    base.mkdir(parents=True, exist_ok=True)

    # helper.data_dir() 这一支（routes_file / state_file / tunnel_state_file /
    # trigger_file 等惰性函数）走 BARKIT_DATA_DIR。
    monkeypatch.setenv("BARKIT_DATA_DIR", str(base))

    # barkit.paths 的硬编码常量走模块属性补丁（见上方背景说明）。表在上面
    # ``PATH_REDIRECTS``（**模块级**：覆盖性有门禁读它，藏在固件里读不到）。
    import barkit.paths as paths

    for name, rel in PATH_REDIRECTS.items():
        monkeypatch.setattr(paths, name, (base / rel) if rel else base, raising=False)

    yield
