#!/usr/bin/env bash
# Retour arrière manuel : ./scripts/rollback.sh [tag]   (par défaut : la version précédente enregistrée)
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"

TARGET="${1:-$(read_state previous)}"
if [ -z "$TARGET" ]; then echo "aucune version précédente connue : préciser un tag" >&2; exit 1; fi
if ! valid_tag "$TARGET"; then echo "tag invalide : $TARGET" >&2; exit 1; fi
if ! docker image inspect "acsi-agent:$TARGET" >/dev/null 2>&1; then
  echo "image acsi-agent:$TARGET introuvable localement (rien n'a été modifié)" >&2
  exit 1
fi

CUR="$(read_state current)"
log "retour arrière ${CUR:-?} → $TARGET"
if compose_up "$TARGET" && wait_healthy "${HEALTH_TIMEOUT_S:-60}" && smoke; then
  if [ -n "$CUR" ] && [ "$CUR" != "$TARGET" ]; then write_state previous "$CUR"; fi
  write_state current "$TARGET"
  log "version $TARGET rétablie"
  exit 0
fi
log "ÉCHEC du retour arrière vers $TARGET : intervention manuelle nécessaire"
exit 3
