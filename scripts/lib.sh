#!/usr/bin/env bash
# Fonctions communes à deploy.sh et rollback.sh (à sourcer).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${DEPLOY_STATE_DIR:-$ROOT/.deploy}"
mkdir -p "$STATE_DIR"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$STATE_DIR/history.log" >&2; }
read_state() { if [ -f "$STATE_DIR/$1" ]; then cat "$STATE_DIR/$1"; fi; }
write_state() { printf '%s' "$2" > "$STATE_DIR/$1"; }
valid_tag() { [[ "$1" =~ ^[A-Za-z0-9_.-]+$ ]]; }

compose_up() { IMAGE_TAG="$1" docker compose -f "$ROOT/compose.yaml" up -d --no-build api; }

# Attend l'état « healthy » du conteneur api (au moins un contrôle, même avec un délai de 0).
wait_healthy() {
  local deadline=$((SECONDS + ${1:-60})) cid status
  while true; do
    cid="$(docker compose -f "$ROOT/compose.yaml" ps -q api 2>/dev/null || true)"
    if [ -n "$cid" ]; then
      status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null || echo unknown)"
      [ "$status" = "healthy" ] && return 0
    fi
    [ "$SECONDS" -ge "$deadline" ] && return 1
    sleep "${HEALTH_POLL_S:-2}"
  done
}

# Smoke test applicatif : santé + une tâche nominale + un refus de politique (la gateway est bien active).
smoke() {
  if [ -n "${ACSI_SMOKE_KEY:-}" ]; then
    python3 "$ROOT/scripts/smoke.py" "${ACSI_SMOKE_URL:-http://127.0.0.1:8000}"
  else
    log "smoke test ignoré (ACSI_SMOKE_KEY absent) : seule la santé du conteneur est vérifiée"
  fi
}
