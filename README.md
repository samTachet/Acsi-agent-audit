# acsi-agent

Agent d'audit contraint pour AuditPro/ACSI (ADR-0003) : tools typés, gateway d'actions, approbation humaine,
budget, journal rejouable. Une branche par phase : `phase-1-specs` → `phase-2-runtime` → `phase-3-gateway` → `phase-4-ops`.

## Phase 1 — contrats (cette branche)

Source de vérité = fichiers JSON, vérifiés par un outil de cohérence et documentés par génération.

| Fichier | Rôle |
|---|---|
| `specs/tools.json` | 6 tools : objectif, entrées/sorties (JSON Schema fermé), permission, approbation, timeout, plafond, erreurs |
| `specs/policy.json` | deny par défaut, actions interdites, approbation humaine, règles sur arguments, rôles/portées, budget, débit |
| `scenarios/reference.json` | 18 scénarios : résultat attendu, tools attendus (arguments, résultat), refus, effets observables |
| `docs/*.md` | générés (`python -m acsi_agent.docs_gen`), contrôlés contre la dérive par les tests |

```bash
uv sync
uv run python -m acsi_agent.spec_check     # cohérence tools / politique / scénarios
uv run python -m acsi_agent.docs_gen --check
uv run pytest
```

Invariants vérifiés : toute action irréversible exige une approbation ; un appel refusé ne demande jamais d'approbation ;
chaque tool est couvert par un scénario nominal, chaque tool interdit cité par un scénario de refus, et les refus
laissent les effets à zéro.
