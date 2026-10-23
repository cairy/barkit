#!/usr/bin/env python3
"""doc_lint - BarKit 文档体系门禁。

校验 docs/ 及根级项目文档是否符合 PROJECT_STRUCTURE.md 第 7 节的文档规范：
  - 每篇文档含 doc-meta 元数据块（类型 / 状态）
  - 类型 ∈ {架构事实, 决策记录, 任务板, 操作指南, 索引}
  - 状态 ∈ {live, frozen, task, draft}
  - 文件名禁止 -N 序号后缀
  - 单文件 ≤ 300 行
  - 每篇内容文档须在所属分区 README 的分类登记表中出现
  - 反引号内的 .md 指针须能解析到真实文档（解析不上的进 allowlist 才算已知债务）

用法:
  python3 scripts/doc_lint.py            # 严格模式，发现违规即非零退出
  python3 scripts/doc_lint.py --advisory # 打印违规但退出 0（迁移期用）
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓库根
VALID_TYPES = {"架构事实", "决策记录", "任务板", "操作指南", "索引"}
VALID_STATUS = {"live", "frozen", "task", "draft"}
MAX_LINES = 300

# 任务板（类型=任务板）专用门禁：本文件只装「改了什么 + 意味着什么」一句话 + 指针，
# 不许堆日志（过程推演 / 根因 / 测试数字 / 门禁自证报告）。详见 PROJECT_STRUCTURE.md §7.8。
MAX_TASKBOARD_BYTES = 51200   # 50KB：防 balloon 回 151KB
MAX_TASKBOARD_LINE = 800      # 单行上限：一条任务一行，超了就是一段叙述压进了行
# 命中即判「堆日志」的过程/验收类签名（合规不误报：只拦客观输出，不拦 §/Dxx/路径指针）
TASKBOARD_LOG_PATTERNS = [
    (re.compile(r"\b\d+\s*(passed|failed)\b", re.I),
     "含测试结论数字（passed/failed 计数）—— 这是日志，搬去 docs/log/"),
    (re.compile(r"\bRC\s*=\s*\d+"),
     "含门禁返回码（RC=）—— 这是日志输出，搬去 docs/log/"),
    (re.compile(r"\b(return code|exit code)\b[^\n]*\d+", re.I),
     "含退出码数字 —— 这是日志输出，搬去 docs/log/"),
    (re.compile(r"pyflakes[\s:*＝=]{0,3}\d+", re.I),
     "含 pyflakes 基线数字 —— 这是日志，搬去 guides/验证与自证.md"),
    (re.compile(r"(门禁自证|反向验证|节点集|全量\s*\*\*\d+)"),
     "含自证/验收报告用语 —— 这是日志，搬去 docs/log/"),
]

# 纳入校验的根级项目文档
ROOT_DOCS = ["README.md", "PROJECT_STRUCTURE.md", "]
DOC_ROOT = ROOT / "docs"
# 不校验的子目录（非文档资源 / 中间态）
SKIP_DIRS = {"plans", "design"}

META_RE = re.compile(r"<!--\s*doc-meta(.*?)-->", re.DOTALL)
KV_RE = re.compile(r"^\s*([\u4e00-\u9fffA-Za-z_]+)\s*[:：]\s*(.+?)\s*$", re.MULTILINE)
NUM_SUFFIX_RE = re.compile(r"-\d+\.md$")

# 文档指针门禁：反引号里的 .md 必须是真存在的文档。
# 解析不上时转去 doc_pointer_allowlist.txt（一行一条，# 起为注释）判定为「已知历史引用」。
PTR_RE = re.compile(r"`([^`\n]*\.md)(?:#[^`\n]*)?`")
ALLOWLIST = ROOT / "scripts" / "doc_pointer_allowlist.txt"


def find_docs():
    docs = []
    for name in ROOT_DOCS:
        p = ROOT / name
        if p.exists():
            docs.append(p)
    if DOC_ROOT.exists():
        for p in sorted(DOC_ROOT.rglob("*.md")):
            parts = p.relative_to(ROOT).parts
            if any(part in SKIP_DIRS for part in parts[1:]):
                continue
            docs.append(p)
    return docs


def parse_meta(text):
    m = META_RE.search(text)
    if not m:
        return None
    meta = {}
    for km in KV_RE.finditer(m.group(1)):
        meta[km.group(1)] = km.group(2)
    return meta


def owning_readme(doc):
    parts = doc.relative_to(ROOT).parts
    if parts[0] != "docs":
        return DOC_ROOT / "README.md"  # 根级文档在 docs/README 登记
    section = parts[1]
    return DOC_ROOT / section / "README.md"


def md_index():
    """全仓 .md 清单（排除依赖与版本库），供裸文件名解析用。"""
    idx = []
    for p in ROOT.rglob("*.md"):
        parts = p.relative_to(ROOT).parts
        if ".venv" in parts or ".git" in parts or "__pycache__" in parts:
            continue
        idx.append(p)
    return idx


def load_allowlist():
    if not ALLOWLIST.exists():
        return set()
    out = set()
    for line in ALLOWLIST.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(line)
    return out


def lint_taskboard(doc, text, errors):
    """任务板专用门禁：只装「改了什么 + 意味着什么」一句话 + 指针，不许堆日志。"""
    size = len(text.encode("utf-8"))
    if size > MAX_TASKBOARD_BYTES:
        errors.append("任务板超 %d 字节（当前 %d）—— 已退回堆日志状态；过程/根因/测试数字搬去 docs/log/"
                      % (MAX_TASKBOARD_BYTES, size))
    for i, line in enumerate(text.splitlines(), 1):
        if len(line) > MAX_TASKBOARD_LINE:
            errors.append("第 %d 行超 %d 字符（%d）—— 疑似把一段叙述压进了一行；拆成「改了什么+意味着什么」一句话 + docs/log/ 指针"
                          % (i, MAX_TASKBOARD_LINE, len(line)))
        for pat, msg in TASKBOARD_LOG_PATTERNS:
            if pat.search(line):
                errors.append("第 %d 行%s" % (i, msg))
                break


def check_registration(doc, errors):
    if doc.name == "README.md":
        return
    readme = owning_readme(doc)
    if not readme.exists():
        return  # 该分区尚无 README，迁移期跳过
    txt = readme.read_text(encoding="utf-8", errors="ignore")
    if doc.name not in txt:
        errors.append("未在 %s 登记（分类表中缺 %s）" % (owning_readme(doc).relative_to(ROOT), doc.name))


def lint_doc(doc, errors):
    text = doc.read_text(encoding="utf-8", errors="ignore")
    meta = parse_meta(text)
    if meta is None:
        errors.append("缺 doc-meta 元数据块")
    else:
        t = meta.get("类型")
        s = meta.get("状态")
        if t is None:
            errors.append("doc-meta 缺「类型」")
        elif t not in VALID_TYPES:
            errors.append("类型非法: %r（应为 %s）" % (t, sorted(VALID_TYPES)))
        if s is None:
            errors.append("doc-meta 缺「状态」")
        elif s not in VALID_STATUS:
            errors.append("状态非法: %r（应为 %s）" % (s, sorted(VALID_STATUS)))
    if NUM_SUFFIX_RE.search(doc.name):
        errors.append("文件名含 -N 序号后缀（禁止）: %s" % doc.name)
    lines = text.count("\n") + (0 if text.endswith("\n") or text == "" else 1)
    if lines > MAX_LINES:
        errors.append("超 300 行（%d 行）" % lines)
    if doc.name == "current-task.md":
        # 唯一常驻任务板：只装「改了什么 + 意味着什么」一句话 + 指针，不许堆日志。
        # （docs/log/ 下的冻结台账与 ROADMAP 是路线图，不在此门禁范围。）
        lint_taskboard(doc, text, errors)
    check_registration(doc, errors)
    check_pointers(doc, errors)


def is_placeholder(ref):
    """模板/占位写法不是引用：`*.md`、`<日期>`、`YYYY-MM-DD-<主题>.md`、裸 `.md`。"""
    name = ref.rsplit("/", 1)[-1]
    return (any(ch in ref for ch in "*<>")
            or name in (".md", "")
            or any(tok in ref for tok in ("YYYY", "MM-DD", "主题")))


def check_pointers(doc, errors):
    """指针解析：同一文件只报一条，免得几十条悬空盖住真正的症状。"""
    allow = load_allowlist()
    outside = True
    bad = []
    for line in doc.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.lstrip().startswith("```"):
            outside = not outside
            continue
        if not outside:
            continue
        for ref in PTR_RE.findall(line):
            if ref in bad or ref in allow or is_placeholder(ref):
                continue
            p = ref.replace("\\", "/")
            # 三级解析：本文件相对 → 仓库根相对 → docs/ 分区相对
            if (doc.parent / p).exists() or (ROOT / p).exists() or (DOC_ROOT / p).exists():
                continue
            hits = [q for q in md_index() if q.name == p.rsplit("/", 1)[-1]]
            if len(hits) == 1:                  # 全仓裸名唯一 ⇒ 指的就是它
                continue
            bad.append(ref)
    if bad:
        errors.append("指针悬空: %s" % "、".join("`%s`" % r for r in bad))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--advisory", action="store_true", help="仅报告不阻断（退出 0）")
    args = ap.parse_args()
    docs = find_docs()
    total_err = 0
    report = []
    for d in docs:
        errs = []
        lint_doc(d, errs)
        if errs:
            total_err += len(errs)
            report.append((str(d.relative_to(ROOT)), errs))
    if report:
        print("doc-lint 发现问题：")
        for name, errs in report:
            print("\n[%s]" % name)
            for e in errs:
                print("  - %s" % e)
        print("\n合计 %d 篇文档，%d 处问题。" % (len(report), total_err))
    else:
        print("doc-lint: 全部通过")
    return 0 if args.advisory else (1 if total_err else 0)


if __name__ == "__main__":
    sys.exit(main())
