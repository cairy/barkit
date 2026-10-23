"""参数值域类型（能力封闭的「值域白名单」一层，方案 §7.4.5 P2）。

设计要点
--------
- **纯函数、无副作用、零依赖**（仅标准库）：部署到系统域后由 root 进程直接导入。
- 每个 spec 只回答一个问题：``check(value) -> (ok, reason)``。失败 reason 是给人看的
  中文短语，会被拼进状态文件 / 日志，**不含参数原文之外的敏感信息**。
- **没有 PathSpec / FileSpec，也不会有**。root 侧一切路径都是源码内部常量；
  参数里出现路径 = 提权面（§7.4.4 红线「无路径参数」）。
  门禁 `test_helper_no_path_param` 会扫描 ``ACTION_SCHEMAS`` 的字段名与 spec kind，
  出现 ``path`` / ``file`` / ``dir`` 类即 fail —— 本模块刻意不提供对应容器类型。
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

CheckResult = Tuple[bool, Optional[str]]

#: 禁止出现在参数名中的词（路径类语义）。新增原语时由门禁扫描。
FORBIDDEN_PARAM_WORDS = (
    "path", "paths", "file", "files", "filename", "filepath", "dir", "dirs",
    "directory", "folder", "cwd", "target_path", "output", "dest_path",
)


class Spec:
    """值域 spec 基类。``kind`` 是小写短名，用于门禁扫描与错误提示。"""

    kind = "any"

    def check(self, value: Any) -> CheckResult:      # pragma: no cover - 抽象
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"


class TextSpec(Spec):
    """受限文本：长度 + 字符集白名单（正则必须**全长匹配**）。"""

    kind = "text"

    def __init__(
        self,
        *,
        pattern: str,
        min_len: int = 1,
        max_len: int = 255,
    ) -> None:
        self._re = re.compile(pattern)
        self._min = min_len
        self._max = max_len

    def check(self, value: Any) -> CheckResult:
        if not isinstance(value, str):
            return False, f"需为字符串：{value!r}"
        if not (self._min <= len(value) <= self._max):
            return False, f"长度需在 {self._min}~{self._max} 字符之间：{len(value)}"
        if self._re.fullmatch(value) is None:
            return False, f"含非法字符：{value!r}"
        return True, None


class Ipv4Spec(Spec):
    """点分十进制 IPv4（不接受 CIDR、不接受 IPv6）。"""

    kind = "ipv4"

    def check(self, value: Any) -> CheckResult:
        if not isinstance(value, str):
            return False, f"需为字符串：{value!r}"
        try:
            addr = ipaddress.ip_address(value)
        except ValueError:
            return False, f"不是合法 IP：{value!r}"
        if addr.version != 4:
            return False, f"仅支持 IPv4：{value!r}"
        return True, None


class CidrSpec(Spec):
    """IPv4 CIDR（``a.b.c.d/len``）。主机位被容忍（``strict=False``，与 /sbin/route 一致）。"""

    kind = "cidr"

    def check(self, value: Any) -> CheckResult:
        if not isinstance(value, str):
            return False, f"需为字符串：{value!r}"
        if "/" not in value:
            return False, f"缺少前缀长度：{value!r}"
        try:
            net = ipaddress.ip_network(value, strict=False)
        except ValueError:
            return False, f"不是合法 CIDR：{value!r}"
        if net.version != 4:
            return False, f"仅支持 IPv4：{value!r}"
        return True, None


class IfaceSpec(Spec):
    """网络接口名（BSD 风格小写：``en0`` / ``utun3`` / ``bridge100``）。"""

    kind = "iface"

    _RE = re.compile(r"[a-z][a-z0-9]{0,15}")

    def check(self, value: Any) -> CheckResult:
        if not isinstance(value, str):
            return False, f"需为字符串：{value!r}"
        if self._RE.fullmatch(value) is None:
            return False, f"非法接口名：{value!r}"
        return True, None


class ServiceSpec(Spec):
    """networksetup 的「网络服务名」（如 ``Wi-Fi`` / ``USB 10/100/1000 LAN``）。

    参数经 ``subprocess`` 参数列表传递（无 shell），但仍收紧字符集：拒绝引号、
    反斜杠、反引号、``$``、控制字符——任何在 ``-setdnsservers`` 语义里可能被
    二次解释的字符都不放行。
    """

    kind = "service"

    _RE = re.compile(r"[^\x00-\x1f\x7f\"'\\`$;|&<>]{1,64}")

    def check(self, value: Any) -> CheckResult:
        if not isinstance(value, str):
            return False, f"需为字符串：{value!r}"
        if not value.strip():
            return False, "网络服务名为空"
        if self._RE.fullmatch(value) is None:
            return False, f"含非法字符：{value!r}"
        return True, None


class SsidSpec(Spec):
    """Wi-Fi SSID：非空、≤32 字节（UTF-8）、无控制字符、无引号/反斜杠。"""

    kind = "ssid"

    def check(self, value: Any) -> CheckResult:
        if not isinstance(value, str):
            return False, f"需为字符串：{value!r}"
        if not value.strip():
            return False, "SSID 为空"
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
            return False, "SSID 含控制字符"
        if '"' in value or "\\" in value:
            return False, "SSID 含引号或反斜杠"
        if len(value.encode("utf-8")) > 32:
            return False, "SSID 超过 32 字节上限"
        return True, None


class HostnameSpec(Spec):
    """主机名（RFC 1123 标签序列，≤253 字符）。"""

    kind = "hostname"

    _RE = re.compile(
        r"(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
        r"(?:\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*"
    )

    def check(self, value: Any) -> CheckResult:
        if not isinstance(value, str):
            return False, f"需为字符串：{value!r}"
        if self._RE.fullmatch(value) is None:
            return False, f"不是合法主机名：{value!r}"
        return True, None


class PortSpec(Spec):
    """TCP/UDP 端口（1~65535 整数）。"""

    kind = "port"

    def __init__(self, *, min_value: int = 1, max_value: int = 65535) -> None:
        self._min = min_value
        self._max = max_value

    def check(self, value: Any) -> CheckResult:
        # bool 是 int 的子类，必须显式排除（True 会被当成端口 1）
        if isinstance(value, bool) or not isinstance(value, int):
            return False, f"需为整数：{value!r}"
        if not (self._min <= value <= self._max):
            return False, f"超出端口范围 {self._min}~{self._max}：{value}"
        return True, None


class ChoiceSpec(Spec):
    """封闭枚举（能力封闭的最后一环：值必须落在源码常量集合内）。"""

    kind = "choice"

    def __init__(self, options: Sequence[str]) -> None:
        self.options = tuple(options)

    def check(self, value: Any) -> CheckResult:
        if not isinstance(value, str):
            return False, f"需为字符串：{value!r}"
        if value not in self.options:
            return False, f"不在允许集合 {list(self.options)}：{value!r}"
        return True, None


class ListSpec(Spec):
    """定长上限的同质列表。"""

    kind = "list"

    def __init__(self, inner: Spec, *, max_items: int = 64) -> None:
        self.inner = inner
        self._max = max_items

    def check(self, value: Any) -> CheckResult:
        if not isinstance(value, list):
            return False, f"需为数组：{value!r}"
        if len(value) > self._max:
            return False, f"元素过多（上限 {self._max}）：{len(value)}"
        for idx, item in enumerate(value):
            ok, reason = self.inner.check(item)
            if not ok:
                return False, f"第 {idx} 项：{reason}"
        return True, None


def check_fields(schema: Dict[str, Spec], params: Any) -> CheckResult:
    """按 schema 校验一个参数对象：未知字段 / 缺字段 / 值域 —— 任一不过即整份拒绝。

    与「整份拒绝，保持上轮状态不动」的语义一致（§7.4.5 P2）。
    """
    if not isinstance(params, dict):
        return False, f"参数需为对象：{params!r}"
    unknown = sorted(set(params) - set(schema))
    if unknown:
        return False, f"含未知参数：{unknown}"
    for name, spec in schema.items():
        if name not in params:
            return False, f"缺少参数：{name}"
        ok, reason = spec.check(params[name])
        if not ok:
            return False, f"参数 {name}：{reason}"
    return True, None


def path_like_field_names(schema: Dict[str, Spec]) -> List[str]:
    """返回 schema 中「看起来像路径参数」的字段名（门禁用，正常应为空）。"""
    hits: List[str] = []
    for name in schema:
        low = name.lower()
        if any(word in low for word in FORBIDDEN_PARAM_WORDS):
            hits.append(name)
    return hits
