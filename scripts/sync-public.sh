#!/usr/bin/env bash
#
# sync-public.sh —— 把私有仓的可发布内容同步到公开仓。
#
# 设计：从私有仓 git archive 出「白名单反选」的内容（排除私有专属文件），
# 覆盖进公开仓工作树，再跑 strip_private_refs.py 把指向私有文件的引用剥掉，
# 最后仅在确有改动时提交。
#
# 公开仓专属、不由 git 树导出的文件（从模板覆盖写入）：
#   - .github/            由 $PRIVATE/public-assets/.github 在同步时写入，
#                         公开仓 CI 与私有仓不同（已去掉 doc_lint 步骤）
#
# 排除清单（私有有、公开不许有）：
#   - AGENT.md            私有协作约定，依赖 current-task.md / agent-routing.md
#   - docs/current-task.md 私有任务板
#   - docs/agent-routing.md 私有 AI 任务路由表
#   - docs/log/           私有日志
#   - docs/plans/         私有计划稿
#   - .python-version     私有工具链标记
#
set -euo pipefail

PRIVATE="${PRIVATE_REPO:-/Users/cairy/Code/barkit}"
PUBLIC="${PUBLIC_REPO:-/Users/cairy/Code/barkit-public}"

if [[ ! -d "$PRIVATE/.git" || ! -d "$PUBLIC/.git" ]]; then
  echo "error: 私有仓或公开仓不存在（$PRIVATE / $PUBLIC）" >&2
  exit 1
fi

# 公开仓有未提交改动时中止，避免覆盖。
if ! git -C "$PUBLIC" diff --quiet || ! git -C "$PUBLIC" diff --cached --quiet; then
  echo "error: 公开仓有未提交改动，先提交或 stash 再同步" >&2
  exit 1
fi

# 反选导出：除列出的路径外全部纳入。
EXCLUDES=(
  ':!AGENT.md'
  ':!docs/current-task.md'
  ':!docs/agent-routing.md'
  ':!docs/ROADMAP.md'
  ':!docs/log'
  ':!docs/plans'
  ':!.python-version'
  ':!.github'
)

echo "==> 从私有仓归档可发布内容…"
git -C "$PRIVATE" archive HEAD -- "${EXCLUDES[@]}" | tar -x -C "$PUBLIC"

echo "==> 剥离私有引用…"
python3 "$PRIVATE/scripts/strip_private_refs.py" "$PUBLIC"

echo "==> 写入公开仓专属 .github（来自 public-assets 模板）…"
# 公开仓的 CI 与私有仓不同（去掉 doc_lint），不由 git 树导出，
# 而是由本脚本从 public-assets/.github 覆盖写入，保证任意机器可重建。
if [[ -d "$PRIVATE/public-assets/.github" ]]; then
  rm -rf "$PUBLIC/.github"
  cp -R "$PRIVATE/public-assets/.github" "$PUBLIC/.github"
else
  echo "warn: public-assets/.github 不存在，跳过 .github 写入" >&2
fi

echo "==> 检查公开仓变更…"
if [[ -z "$(git -C "$PUBLIC" status --porcelain)" ]]; then
  echo "done: 公开仓已是最新，无改动"
  exit 0
fi

git -C "$PUBLIC" add -A
git -C "$PUBLIC" commit -m "sync: 从私有仓同步 $(date +%F)"
echo "done: 已提交公开仓同步"
