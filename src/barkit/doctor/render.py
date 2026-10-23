"""doctor/render：把 ``Report`` 摆成人能读的样子（文本）与机器能读的样子（JSON）。

**Qt-free**：``barkit doctor`` 全程不建 QApplication，输出只走 stdout。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime

from barkit.doctor.model import FAIL, PASS, Report, Result, SKIP, WARN

SCHEMA = 1


def _sorted_groups(report: Report) -> "list[tuple[str, str]]":
    """按声明顺序分组；未声明的 group key 兜底排在最后（用 key 自身当标题）。"""
    known = list(report.groups.items())
    seen = {key for key, _ in known}
    extra = sorted({r.group for r in report.results} - seen)
    return known + [(key, key) for key in extra]


def _row(result: Result, verbose: bool) -> list[str]:
    head = f"  {result.level:<4}  {result.id}  {result.title}"
    # 默认只细说「有事的」；--verbose 连 PASS 也摊开（贴 issue 用）
    if result.level == PASS and not verbose:
        return [head]
    lines = [head]
    if result.detail:
        lines.append(f"          {result.detail}")
    if result.hint and (result.level != PASS or verbose):
        lines.append(f"          → {result.hint}")
    return lines


def _conclusion(report: Report) -> str:
    fails = report.count(FAIL)
    if fails:
        return f"FAIL —— 有 {fails} 项未通过，逐条的「→」就是下一步"
    if report.count(WARN):
        return f"WARN —— 没有失败，但 {report.count(WARN)} 项值得看一眼"
    if not report.results:
        return "SKIP —— 一项都没跑（检查表为空）"
    return "PASS —— 全部检查通过"


def render_text(report: Report, verbose: bool = False) -> str:
    lines = [
        f"BarKit 自检 · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · "
        f"平台 {sys.platform}",
    ]
    for key, title in _sorted_groups(report):
        rows = [r for r in report.results if r.group == key]
        if not rows:
            continue
        lines.append("")
        lines.append(title)
        for result in rows:
            lines.extend(_row(result, verbose))
    lines.append("")
    lines.append(
        "合计 %d 项：%d PASS · %d WARN · %d FAIL · %d SKIP"
        % (len(report.results), report.count(PASS), report.count(WARN),
           report.count(FAIL), report.count(SKIP))
    )
    lines.append("结论：" + _conclusion(report))
    return "\n".join(lines)


def render_json(report: Report) -> str:
    payload = {
        "tool": "barkit doctor",
        "schema": SCHEMA,
        "platform": sys.platform,
        "summary": {
            PASS: report.count(PASS), WARN: report.count(WARN),
            FAIL: report.count(FAIL), SKIP: report.count(SKIP),
        },
        "conclusion": report.worst,
        "exit_code": report.exit_code,
        "results": [
            {
                "id": r.id,
                "group": r.group,
                "group_title": report.groups.get(r.group, r.group),
                "title": r.title,
                "level": r.level,
                "detail": r.detail,
                "hint": r.hint,
            }
            for r in report.results
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
