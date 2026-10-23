"""``_rule_reason``：一条规则的用户可见原因，``reason`` 优先、空串 /
缺失才退到名称，都空则空串。

它是**纯函数**（不碰 ``self``），故不进任何混合类 —— 与 ``self`` 无关的东西住进
混合类只是为了好看。面板副标题、设置页行、拨测报告三处都取这一处（各写一遍必然
漂移），``plugin_words`` 与本模块同处、``plugin_probe`` 从本模块取。

``plugin.py`` 拆包：正文逐字节搬移，零行为变更。
"""
from __future__ import annotations


def _rule_reason(rule: dict) -> str:
    """一条规则的用户可见原因（``reason`` 优先，空串 / 缺失才退到名称；都空则空串）。

    面板副标题、设置页行、拨测报告三处都取这一处 —— 各写一遍必然漂移。

    「退到名称」的判据是 ``or``：``reason`` 为 **空串 / None / 键不存在**才算缺；
    只由空白组成的串算"有"（取完 ``strip`` 得空串，于是调用方按"没原因"渲染）。
    这层语义照抄抽取前 ``_hit_reason`` 里那行内联表达式 —— 本函数是**提取**，不是改口径。
    """
    return str(rule.get("reason") or rule.get("name") or "").strip()
