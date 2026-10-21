#!/usr/bin/env bash
# Déploiement : portes de qualité → image immuable (sha git) → up → santé → smoke.
# En cas d'échec de santé ou de smoke : retour automatique à la version précédente (code 2).
# Codes : 0 déployé · 1 refusé avant déploiement · 2 déploiement échoué, version précédente rétablie · 3 retour arrière échoué
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"

if [ -n "$(git status --porcelain)" ]; then
  echo "arbre git sale : commiter avant de déployer (l'image doit correspondre à un commit)" >&2
  exit 1
fi
TAG="$(git rev-parse --short=12 HEAD)"

GATE="${DEPLOY_GATE_CMD:-uv run pytest -q && uv run python -m acsi_agent.spec_check && uv run python -m acsi_agent.docs_gen --check && uv run python -m acsi_agent.regress}"
log "portes de qualité : $GATE"
if ! bash -c "$GATE"; then
  log "ÉCHEC des portes de qualité : aucun déploiement"
  exit 1
fi

PREV="$(read_state current)"
log "build acsi-agent:$TAG (version en place : ${PREV:-aucune})"
IMAGE_TAG="$TAG" docker compose -f compose.yaml build api

if compose_up "$TAG" && wait_healthy "${HEALTH_TIMEOUT_S:-60}" && smoke; then
  if [ -n "$PREV" ] && [ "$PREV" != "$TAG" ]; then write_state previous "$PREV"; fi
  write_state current "$TAG"
  log "déployé $TAG"
  exit 0
fi

log "ÉCHEC de santé ou de smoke pour $TAG"
if [ -n "$PREV" ]; then
  log "retour arrière automatique vers $PREV"
  if compose_up "$PREV" && wait_healthy "${HEALTH_TIMEOUT_S:-60}"; then
    log "version $PREV rétablie"
    exit 2
  fi
  log "RETOUR ARRIÈRE ÉCHOUÉ : intervention manuelle nécessaire"
  exit 3
fi
log "aucune version précédente : arrêt du service"
docker compose -f compose.yaml stop api || true
exit 2
