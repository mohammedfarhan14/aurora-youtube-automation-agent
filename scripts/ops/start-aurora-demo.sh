#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNTIME="$ROOT/.aurora-runtime"
ACTION="${1:-start}"
mkdir -p "$RUNTIME"

# When invoked directly from WSL, hand orchestration to Windows PowerShell so
# Windows-hosted ComfyUI and the selected Windows TTS bridge start alongside
# the Linux Docker stack. The PowerShell launcher sets this guard when it calls
# back into WSL to prevent a dispatch loop.
if [[ "${AURORA_WINDOWS_ORCHESTRATED:-}" != "1" ]] &&
   grep -qi microsoft /proc/version 2>/dev/null &&
   command -v powershell.exe >/dev/null 2>&1; then
  script_windows_path="$(wslpath -w "$ROOT/scripts/ops/start-aurora-demo.ps1")"
  distro="${WSL_DISTRO_NAME:-}"
  if [[ -z "$distro" ]]; then
    echo "Could not determine WSL_DISTRO_NAME; run this in the WSL distribution containing Aurora." >&2
    exit 1
  fi
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$script_windows_path" \
    -Distro "$distro" -LinuxUser "$(id -un)" -ProjectPath "$ROOT" -Action "$ACTION"
  exit $?
fi

if [[ "$ACTION" == "stop" ]]; then
  if [[ -f "$RUNTIME/vite.pid" ]]; then
    pid="$(cat "$RUNTIME/vite.pid")"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
      command_line="$(ps -p "$pid" -o args= 2>/dev/null || true)"
      process_cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
      if [[ "$command_line" == *"vite/bin/vite.js"* && "$process_cwd" == "$ROOT/frontend/web" ]]; then
        kill "$pid"
        echo "Stopped Aurora Studio web UI (pid $pid)."
      else
        echo "Saved UI pid no longer identifies the Aurora Vite process; leaving it untouched."
      fi
    fi
    rm -f "$RUNTIME/vite.pid"
  fi
  docker compose --profile app --profile story --profile media --profile youtube-publish stop
  echo "Stopped Aurora Studio services. Persistent database/media volumes were kept."
  exit 0
fi

if [[ "$ACTION" != "start" ]]; then
  echo "Usage: $0 [start|stop]" >&2
  exit 2
fi

cd "$ROOT"
if [[ ! -f .env ]]; then
  echo "Missing $ROOT/.env. Copy .env.example to .env and configure local providers first." >&2
  exit 1
fi
comfyui_wsl_host_ip="$(ip -4 route show default | awk 'NR == 1 { print $3; exit }')"
if [[ ! "$comfyui_wsl_host_ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
  echo "Could not determine the Windows host IP from the WSL default route; ComfyUI proxy routing cannot be configured." >&2
  exit 1
fi
if grep -q '^COMFYUI_WSL_HOST_IP=' .env; then
  sed -i "s/^COMFYUI_WSL_HOST_IP=.*/COMFYUI_WSL_HOST_IP=$comfyui_wsl_host_ip/" .env
else
  printf '\nCOMFYUI_WSL_HOST_IP=%s\n' "$comfyui_wsl_host_ip" >> .env
fi
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 is required." >&2
  exit 1
fi

if [[ -s "$HOME/.nvm/nvm.sh" ]]; then
  # shellcheck disable=SC1090
  source "$HOME/.nvm/nvm.sh"
  nvm use >/dev/null
fi
if ! command -v node >/dev/null || [[ "$(node -p 'process.versions.node.split(".")[0]')" != "24" ]]; then
  echo "Node.js 24 is required (see .nvmrc). Install it with nvm, then rerun." >&2
  exit 1
fi

echo "Starting database, API, local research/story/media workers…"
docker compose --profile app --profile story --profile media up --pull never --no-build -d --wait --wait-timeout 90 postgres redis media-egress-proxy research-egress-proxy

echo "Applying pending database migrations…"
docker compose --profile app run --rm --no-deps api alembic upgrade head

if [[ "${AURORA_DEMO_ENABLE_YOUTUBE_PUBLISHER:-true}" == "true" ]]; then
  echo "Starting Aurora with the isolated YouTube publisher enabled for approved manual uploads. Automatic publishing consent remains off; use AURORA_DEMO_ENABLE_YOUTUBE_PUBLISHER=false to leave the publisher off."
  YOUTUBE_PUBLISH_WORKER_ENABLED=true docker compose --profile app --profile story --profile media --profile youtube-publish up --pull never --no-build -d --wait --wait-timeout 120
else
  YOUTUBE_PUBLISH_WORKER_ENABLED=false docker compose --profile app --profile story --profile media up --pull never --no-build -d --wait --wait-timeout 120
  docker compose --profile youtube-publish stop youtube-publish-worker youtube-egress-proxy >/dev/null 2>&1 || true
  echo "YouTube publisher is stopped for this local launch."
fi

if curl --silent --fail http://127.0.0.1:5173/ >/dev/null 2>&1; then
  echo "Web UI already responds at http://localhost:5173."
else
  if [[ -f "$RUNTIME/vite.pid" ]] && kill -0 "$(cat "$RUNTIME/vite.pid")" 2>/dev/null; then
    echo "Found a saved Vite process that is not responding yet; not starting a duplicate."
  else
    rm -f "$RUNTIME/vite.pid"
    (
      cd "$ROOT/frontend/web"
      setsid "$(command -v node)" ./node_modules/vite/bin/vite.js --host 0.0.0.0 --port 5173 --strictPort \
        </dev/null >"$RUNTIME/vite.log" 2>&1 &
      echo $! > "$RUNTIME/vite.pid"
    )
  fi
fi

for attempt in $(seq 1 45); do
  if curl --silent --fail http://127.0.0.1:8000/health/ready >/dev/null 2>&1 && curl --silent --fail http://127.0.0.1:5173/ >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

if ! curl --silent --fail http://127.0.0.1:8000/health/ready >/dev/null 2>&1; then
  docker compose ps
  echo "API did not become healthy. Inspect with: docker compose logs --tail=100 api" >&2
  exit 1
fi
if ! curl --silent --fail http://127.0.0.1:5173/ >/dev/null 2>&1; then
  tail -n 60 "$RUNTIME/vite.log" 2>/dev/null || true
  echo "Web UI did not become ready on port 5173." >&2
  exit 1
fi

echo
echo "Aurora Studio is running: http://localhost:5173"
echo "API readiness: http://localhost:8000/health/ready"
echo "Runtime log: $RUNTIME/vite.log"
echo "Stop without deleting persistent data: $0 stop"
echo "Use Settings → Production readiness → Verify providers before creating a demo run."
