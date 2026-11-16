#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

usage() {
  cat <<'EOF'
Reset only Aurora's unpublished local demo research and production history.

Preview:
  ./scripts/ops/reset-local-demo-data.sh --dry-run

Delete the matching generated history and local media:
  ./scripts/ops/reset-local-demo-data.sh --execute --confirm Aurora-Studio-Local

The reset aborts if any YouTube publish job exists or if a production run is
outside the local demo's review/cancelled states. Accounts, workspaces,
settings, channel OAuth tokens, and consent records are retained.
EOF
}

mode="${1:-}"
case "$mode" in
  --dry-run|--execute) ;;
  *) usage; exit 2 ;;
esac

expected_name="Aurora-Studio-Local"
if [[ "$mode" == "--execute" && "${2:-}" != "--confirm" ]]; then
  echo "Refusing deletion without --confirm $expected_name." >&2
  usage
  exit 2
fi
if [[ "$mode" == "--execute" && "${3:-}" != "$expected_name" ]]; then
  echo "Confirmation must exactly match the local demo workspace name." >&2
  exit 2
fi

psql_demo() {
  docker compose exec -T postgres sh -lc \
    'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"' sh "$@"
}

org_rows="$(psql_demo -At -F $'\t' -c \
  "select id::text,name from organizations where name = '$expected_name' order by id")"
if [[ "$(printf '%s\n' "$org_rows" | sed '/^$/d' | wc -l)" -ne 1 ]]; then
  echo "Expected exactly one '$expected_name' workspace; found:" >&2
  printf '%s\n' "$org_rows" >&2
  exit 1
fi
IFS=$'\t' read -r organization_id organization_name <<<"$org_rows"
if [[ ! "$organization_id" =~ ^[0-9a-f-]{36}$ ]]; then
  echo "Workspace ID was invalid; refusing reset." >&2
  exit 1
fi

publish_jobs="$(psql_demo -At -c \
  "select count(*) from youtube_publish_jobs where organization_id = '$organization_id'")"
unsafe_runs="$(psql_demo -At -c \
  "select count(*) from production_runs where organization_id = '$organization_id' and status not in ('awaiting_review','cancelled')")"
nonlocal_media="$(psql_demo -At -c \
  "select count(*) from media_artifacts where organization_id = '$organization_id' and storage_backend <> 'local'")"
if [[ "$publish_jobs" != "0" || "$unsafe_runs" != "0" || "$nonlocal_media" != "0" ]]; then
  echo "Reset safety check failed: publish_jobs=$publish_jobs, unexpected_production_states=$unsafe_runs, nonlocal_media=$nonlocal_media." >&2
  exit 1
fi

research_count="$(psql_demo -At -c \
  "select count(*) from research_runs where organization_id = '$organization_id'")"
production_count="$(psql_demo -At -c \
  "select count(*) from production_runs where organization_id = '$organization_id'")"
artifact_count="$(psql_demo -At -c \
  "select count(*) from media_artifacts where organization_id = '$organization_id'")"
artifact_bytes="$(psql_demo -At -c \
  "select coalesce(sum(byte_size),0) from media_artifacts where organization_id = '$organization_id'")"
demo_files="$(find "$ROOT_DIR/var/demo" -maxdepth 1 -type f -name 'aurora-*' -printf '%f\n' 2>/dev/null | sort || true)"

cat <<EOF
Workspace: $organization_name ($organization_id)
Research runs to remove: $research_count
Unpublished production runs to remove: $production_count
Registered media objects to remove: $artifact_count ($artifact_bytes bytes)
Loose Aurora demo files to remove:
${demo_files:-  (none)}
Preserved: user account, workspace, provider settings, channel connection/OAuth,
and channel publishing consent history. No YouTube publish jobs exist.
EOF

if [[ "$mode" == "--dry-run" ]]; then
  exit 0
fi

mapfile -t storage_keys < <(psql_demo -At -c \
  "select storage_key from media_artifacts where organization_id = '$organization_id' and storage_backend = 'local' order by storage_key")

psql_demo -c "begin;
delete from research_source_rights_reviews
 where organization_id = '$organization_id'
   and research_run_id in (select id from research_runs where organization_id = '$organization_id');
delete from production_runs where organization_id = '$organization_id';
delete from research_runs where organization_id = '$organization_id';
commit;"

for key in "${storage_keys[@]}"; do
  docker compose exec -T api python -c '
import asyncio, sys
from app.core.config import get_settings
from app.media.storage import LocalMediaStorage
settings = get_settings()
asyncio.run(LocalMediaStorage(settings.media_storage_root).delete(sys.argv[1]))
' "$key"
done

python3 - "$ROOT_DIR/var/demo" <<'PY'
from pathlib import Path
import sys

directory = Path(sys.argv[1]).resolve()
if directory.exists():
    for path in directory.iterdir():
        if path.is_file() and path.name.startswith("aurora-"):
            path.unlink()
    try:
        directory.rmdir()
    except OSError:
        pass
PY

remaining_runs="$(psql_demo -At -c \
  "select (select count(*) from research_runs where organization_id = '$organization_id') + (select count(*) from production_runs where organization_id = '$organization_id')")"
if [[ "$remaining_runs" != "0" ]]; then
  echo "Reset did not remove all local demo runs; remaining count=$remaining_runs." >&2
  exit 1
fi

echo "Local demo history and generated media were cleared. Account, settings, and YouTube connection were retained."
