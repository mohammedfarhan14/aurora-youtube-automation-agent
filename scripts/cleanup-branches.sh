#!/usr/bin/env bash
set -Eeuo pipefail

# Conservative branch cleanup driven only by the committed branch inventory.
# Default is dry-run. Remote refs are archived as annotated tags before deletion.
# No GitHub API is used; the caller's local Git credentials perform the pushes.

EXECUTE=0
INCLUDE_SUPERSEDED=0

usage() {
  cat <<'EOF'
Usage:
  scripts/cleanup-branches.sh [--dry-run] [--include-superseded] [--execute]

Options:
  --dry-run              List candidates only (the default).
  --include-superseded   Include inventory class B in addition to class A.
  --execute              Archive candidate refs, then request explicit confirmation
                         before deleting those remote branches.

Safety:
  - Must run from a clean local checkout of the latest develop branch.
  - Inventory names and recorded head SHAs must exactly match live remote refs.
  - Classes C, D and E are never candidates. Open-PR branches are skipped.
  - main, develop and feat/creative-identity-registry (PR #145) are protected.
  - Every candidate is archived to a remote annotated tag before deletion.
EOF
}

while (($#)); do
  case "$1" in
    --dry-run) EXECUTE=0 ;;
    --include-superseded) INCLUDE_SUPERSEDED=1 ;;
    --execute) EXECUTE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "Run this script from inside the aurora-studio Git checkout." >&2
  exit 1
}
cd "$ROOT"

if [[ "$(basename "$ROOT")" != "aurora-studio" ]]; then
  echo "Unexpected repository directory: $ROOT" >&2
  exit 1
fi

if [[ "$(git branch --show-current)" != "develop" ]]; then
  echo "Checkout develop before running cleanup." >&2
  exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree is not clean. Commit/stash changes before cleanup." >&2
  exit 1
fi

git fetch --prune origin
if [[ "$(git rev-parse HEAD)" != "$(git rev-parse origin/develop)" ]]; then
  echo "Local develop is not at origin/develop. Run git pull --ff-only origin develop." >&2
  exit 1
fi

INVENTORY="$ROOT/docs/engineering/branch-comparison.csv"
if [[ ! -f "$INVENTORY" ]]; then
  echo "Missing committed inventory: $INVENTORY" >&2
  exit 1
fi

CANDIDATES_FILE="$(mktemp)"
trap 'rm -f "$CANDIDATES_FILE"' EXIT

python3 - "$INVENTORY" "$INCLUDE_SUPERSEDED" >"$CANDIDATES_FILE" <<'PY'
import csv
import subprocess
import sys

inventory_path, include_superseded = sys.argv[1], sys.argv[2] == "1"
required = {"branch", "head_sha", "classification", "associated_pr", "reason"}
with open(inventory_path, newline="", encoding="utf-8") as source:
    reader = csv.DictReader(source)
    if not required.issubset(set(reader.fieldnames or ())):
        raise SystemExit("Inventory CSV is missing required columns.")
    rows = list(reader)

names = [row["branch"] for row in rows]
if len(names) != len(set(names)):
    raise SystemExit("Inventory contains duplicate branch names.")

result = subprocess.run(
    ["git", "ls-remote", "--heads", "origin"],
    check=True,
    capture_output=True,
    text=True,
)
remote = {}
for line in result.stdout.splitlines():
    sha, ref = line.split("\t", 1)
    if ref.startswith("refs/heads/"):
        remote[ref.removeprefix("refs/heads/")] = sha

if set(names) != set(remote):
    missing = sorted(set(remote) - set(names))
    stale = sorted(set(names) - set(remote))
    print(f"Inventory/live branch mismatch. Missing={missing}; stale={stale}", file=sys.stderr)
    raise SystemExit(1)

protected = {"main", "develop", "feat/creative-identity-registry"}
candidates = []
for row in rows:
    branch = row["branch"]
    classification = row["classification"]
    if branch in protected:
        continue
    if classification not in {"A", "B"}:
        continue
    if "open" in (row.get("associated_pr") or "").lower():
        continue
    if classification == "B" and not include_superseded:
        continue
    recorded_sha = row["head_sha"]
    if len(recorded_sha) != 40 or remote[branch] != recorded_sha:
        print(f"Ref changed or lacks an exact recorded SHA: {branch}", file=sys.stderr)
        raise SystemExit(1)
    candidates.append((classification, branch, recorded_sha))

print(
    f"Validated {len(rows)} inventory rows against {len(remote)} live branches.",
    file=sys.stderr,
)
print(f"Selected {len(candidates)} candidates.", file=sys.stderr)
for classification, branch, sha in candidates:
    print(f"{classification}\t{branch}\t{sha}")
PY

mapfile -t CANDIDATES <"$CANDIDATES_FILE"
if (("${#CANDIDATES[@]}" == 0)); then
  echo "No branches match the selected cleanup policy."
  exit 0
fi

echo
echo "Remote branch cleanup candidates:"
for item in "${CANDIDATES[@]}"; do
  IFS=$'\t' read -r classification branch sha <<<"$item"
  printf '  [%s] %-55s %s\n' "$classification" "$branch" "$sha"
done
echo
echo "Protected and preserved: main, develop, PR #145, and inventory classes C/D/E."

if ((EXECUTE == 0)); then
  echo "Dry-run only. No tags or branches were changed."
  echo "Review the list, then rerun with --execute."
  exit 0
fi

COUNT="${#CANDIDATES[@]}"
read -r -p "Type DELETE $COUNT BRANCHES to archive and delete these refs: " CONFIRM
if [[ "$CONFIRM" != "DELETE $COUNT BRANCHES" ]]; then
  echo "Confirmation did not match. No branches were deleted."
  exit 1
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TAG_PREFIX="archive/branch-cleanup/$STAMP"

# Archive every candidate before deleting any branch. A failed tag push aborts.
for item in "${CANDIDATES[@]}"; do
  IFS=$'\t' read -r classification branch sha <<<"$item"
  encoded_branch="${branch//\//--}"
  tag="$TAG_PREFIX/$encoded_branch"
  if git show-ref --verify --quiet "refs/tags/$tag" ||
     git ls-remote --exit-code --tags origin "refs/tags/$tag" >/dev/null 2>&1; then
    echo "Archive tag already exists; refusing to overwrite: $tag" >&2
    exit 1
  fi
  git tag -a "$tag" "$sha" -m "Archive $branch before Aurora Studio branch cleanup"
  git push origin "refs/tags/$tag"
  printf 'Archived %-55s -> %s\n' "$branch" "$tag"
done

# Expected-old-SHA leases prevent deletion if a branch moved after validation.
for item in "${CANDIDATES[@]}"; do
  IFS=$'\t' read -r classification branch sha <<<"$item"
  actual="$(git ls-remote --heads origin "refs/heads/$branch" | awk 'NR == 1 {print $1}')"
  if [[ "$actual" != "$sha" ]]; then
    echo "Branch changed after archiving; refusing deletion: $branch" >&2
    echo "Its archive tag remains under $TAG_PREFIX." >&2
    exit 1
  fi
  git push --force-with-lease="refs/heads/$branch:$sha" origin ":refs/heads/$branch"
  printf 'Deleted remote branch: %s\n' "$branch"
done

echo
echo "Cleanup completed. Archive tags are under: $TAG_PREFIX"
echo "Recovery: git push origin refs/tags/<archive-tag>:refs/heads/<original-branch>"
