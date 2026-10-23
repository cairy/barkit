#!/usr/bin/env python3
"""从公开仓副本中剥离私有引用。

公开仓不含 AGENT.md、docs/current-task.md、docs/agent-routing.md、
docs/ROADMAP.md、docs/log/、docs/plans/ 这些私有文件（同步时由
scripts/sync-public.sh 反选排除）。本脚本把指向它们的引用从导出的文档里删掉
（保留周围正文），保证公开仓自洽、无断链。

两类处理：
  1. 非 .md 的少量文件（scripts/ 下的同步配套）用定点正则处理；
  2. 所有 .md 文档用通用规则处理——覆盖反引号代码段、markdown 链接、
     方括号组、书名号、裸写正文、文档索引表行。

用法：
    python scripts/strip_private_refs.py <public_repo_root>

幂等：重复运行不产生变化。每个改动打印一行报告；无任何改动则静默。

参考 token 形如（前缀 ../ 或 docs/ 可选）：
    current-task.md  ROADMAP.md  agent-routing.md  AGENT.md
    docs/log/...     ../log/...  log/README.md
    docs/plans/...   ../plans/...
仅当 token 前不是字母时才匹配，避免 dialog / catalog 等子串误伤。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# (相对路径, [(正则, 替换值, 说明), ...]) —— 仅限非 .md 的定点处理。
TRANSFORMS: dict[str, list[tuple[re.Pattern[str], str, str]]] = {
    "scripts/doc_lint.py": [
        (
            re.compile(r', "AGENT\.md", '),
            ", ",
            "从公开副本的 ROOT_DOCS 移除 AGENT.md（公开仓不含该文件）",
        ),
        (
            re.compile(r', "ROADMAP\.md", '),
            ", ",
            "从公开副本的 ROOT_DOCS 移除 ROADMAP.md（公开仓不含该文件）",
        ),
        (
            re.compile(r'"current-task\.md"\]'),
            '"]',
            "从公开副本的 ROOT_DOCS 移除 current-task.md（公开仓不含该文件）",
        ),
    ],
    "scripts/doc_pointer_allowlist.txt": [
        (
            re.compile(
                r"# ── 引用写法误伤：提交信息里被引号包住的文件名，不是指针\n"
                r"chore: 引入 AGENT\.md 与 PROJECT_STRUCTURE\.md\n"
            ),
            "",
            "删除公开副本里失效的 AGENT.md 悬空登记",
        ),
    ],
}

# ---------------------------------------------------------------------------
# 通用 .md 处理
# ---------------------------------------------------------------------------

# 一个指向私有文件的引用 token。
REF = re.compile(
    r"(?<![A-Za-z])"                      # 前面不能是字母（防 dialog/catalog 误伤）
    r"(?:\.\./){0,2}(?:docs/)?"          # 可选前缀 ../ 或 docs/
    r"(?:current-task\.md|ROADMAP\.md|agent-routing\.md|AGENT\.md"  # 四个私有文档
    r"|(?:log|plans)/[^\s`)\]]*)"        # 或 log/ plans/ 目录（必须带斜杠）
)

# markdown 链接：[label](url) —— url 指向私有文件时，保留 label 文字（去掉链接）
LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*?" + REF.pattern + r"[^)]*?\)")
# 方括号组（无括号链接）：[a, b, private, c] —— 删掉其中的私有引用
BRACKET_RE = re.compile(r"\[([^\]]*?" + REF.pattern + r"[^\]]*?)\]")
# 反引号代码段：`docs/current-task.md`、../../current-task.md、docs/log/xxx.md 等。
# 关键：只匹配「内容本身就是私有引用路径」的代码段，且不允许跨行——
# 否则会从一个代码段的闭引号配对到后面另一个代码段的开引号，吞掉中间正文。
# 代码段内可带可选的前缀（../ docs/）与可选的 :行号 后缀。
BACKTICK_RE = re.compile(
    r"`(?:\.\./){0,2}(?:docs/)?"
    r"(?:current-task\.md|ROADMAP\.md|agent-routing\.md|AGENT\.md"
    r"|(?:log|plans)/[^\s`)\]]*)"
    r"(?::[^\s`)\]]+)?"
    r"`"
)
# 裸写的正文引用（无反引号/链接/方括号包裹）：AGENT.md §12、见 current-task.md 等
BARE_RE = re.compile(
    r"(?<![A-Za-z./])"
    r"(?:\.\./){0,2}(?:docs/)?"
    r"(?:current-task\.md|ROADMAP\.md|agent-routing\.md|AGENT\.md"
    r"|(?:log|plans)/[^\s`)\]]*)"
    r"(?![A-Za-z./])"
)
# 书名号包裹：见《../../current-task.md》§3（书名号内不允许跨行，避免误吞正文）
BOOKTITLE_RE = re.compile(r"《[^》\n]*?" + REF.pattern + r"[^》\n]*?》")
# 文档索引表里「首格就是私有文件名」的整行
INDEX_ROW_RE = re.compile(
    r"^\s*\|[\s`]*?(?:current-task\.md|ROADMAP\.md|agent-routing\.md|AGENT\.md)[\s`]*\|.*$",
    re.MULTILINE,
)

# 框线字符：含这些字符的行是 ASCII/Unicode 框图，必须原样保留空格。
_BOX = "─│┌┐└┘├┤┬┴┼┏┓┗┛┣┫┳┻╋═"


def _clean_bracket(inner: str) -> str:
    parts = re.split(r"[,、\s]+", inner)
    kept = [p for p in parts if p and not REF.search(p)]
    return ", ".join(kept)


def _cleanup(text: str) -> str:
    # 1) 压缩多余空格，但跳过框图行（保空格对齐），且不碰行首缩进（保列表层级）。
    out_lines = []
    for line in text.split("\n"):
        if any(c in _BOX for c in line):
            out_lines.append(line)
            continue
        out_lines.append(re.sub(r"(?<=.) {2,}", " ", line))
    text = "\n".join(out_lines)
    # 2) 表格里「单元格只剩一个 / 」的残留斜杠清掉。
    text = re.sub(r"\|[ \t]*/[ \t]*\|", "| |", text)
    # 3) 全角标点前后的多余空格（如「见 。」→「见。」、「（ 专用」→「（专用」）。
    text = re.sub(r" (?=[。，、（）！？：；])", "", text)
    text = re.sub(r"（ (?=[\u4e00-\u9fff])", "（", text)
    # 4) 多余的连续空行。
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def strip_text(text: str) -> tuple[str, int]:
    """对一段 .md 正文剥离私有引用，返回 (新文本, 改动计数)。"""
    changed = 0

    def link_repl(m: re.Match) -> str:
        # 保留标签文字；若标签本身就是路径则一并去掉（避免留下裸文件名死链）
        label = m.group(1)
        return "" if REF.search(label) else label

    def br_repl(m: re.Match) -> str:
        cleaned = _clean_bracket(m.group(1))
        return f"[{cleaned}]" if cleaned else ""

    # 顺序很重要：先整行删掉「首格就是私有文件名」的索引表行，
    # 否则 BARE_RE 会先把文件名抠掉、留下空单元格的行。
    new, n = INDEX_ROW_RE.subn("", text); changed += n; text = new
    new, n = BACKTICK_RE.subn("", text); changed += n; text = new
    new, n = LINK_RE.subn(link_repl, text); changed += n; text = new
    new, n = BRACKET_RE.subn(br_repl, text); changed += n; text = new
    new, n = BOOKTITLE_RE.subn("", text); changed += n; text = new
    new, n = BARE_RE.subn("", text); changed += n; text = new

    text = _cleanup(text)
    return text, changed


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def _strip_one(root: Path, rel: str, rules: list[tuple[re.Pattern[str], str, str]]) -> int:
    path = root / rel
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8")
    changed = 0
    for pattern, repl, desc in rules:
        new_text, n = pattern.subn(repl, text)
        if n:
            changed += n
            text = new_text
            print(f"  [{rel}] {desc}（{n} 处）")
    if changed:
        path.write_text(text, encoding="utf-8")
    return changed


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: strip_private_refs.py <public_repo_root>", file=sys.stderr)
        return 2
    root = Path(sys.argv[1]).resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    total = 0
    # 1) 非 .md 定点处理
    for rel, rules in TRANSFORMS.items():
        total += _strip_one(root, rel, rules)
    # 2) 所有 .md 通用处理
    for p in sorted(root.rglob("*.md")):
        text = p.read_text(encoding="utf-8")
        new, n = strip_text(text)
        if n:
            total += n
            p.write_text(new, encoding="utf-8")
            print(f"  {p.relative_to(root)} : {n} 处引用剥离")
    if total:
        print(f"done: {total} 处引用已剥离")
    else:
        print("done: 无改动（已是最简状态）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
