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

## Phase 2 — runtime, journal, évaluation, mocks, Docker (cette branche)

| Module | Rôle |
|---|---|
| `tools_impl.py`, `backends.py` | tools sur services **simulés** (état isolé par tâche, `FakeMailGateway` avec pannes/latence injectables) |
| `gateway.py` | allowlist + interdits, validation JSON Schema des arguments, timeout, contrat de sortie. Les tools à approbation sont **refusés** (échec fermé) tant que la phase 3 n'existe pas |
| `journal.py` | un fichier JSONL par `request_id`, append-only, **chaîné par hachage** (`verify` détecte toute altération) |
| `agent.py` | boucle planner → gateway → tool ; chaque appel journalisé : tool choisi, arguments, décision gateway, résultat, erreur, latence ; tokens et coût par appel du planner |
| `planners.py` | `rule` (modèle compétent), `scripted` (modèle trompé ou maladroit), `runaway` (boucle sans fin) |
| `eval.py` | tool correctness (bon tool / bons paramètres / bonne issue / bon résultat) + statut final + effets observables |

```bash
uv run pytest                      # 40+ tests, dont : l'évaluation détecte un mauvais tool, de mauvais arguments, un appel superflu ;
                                   # les mocks ne font aucun appel réseau (socket.connect interdit pendant l'éval)
uv run python -m acsi_agent.eval   # 8 scénarios exécutables en phase 2 (les 10 autres exigent la phase 3)
docker compose run --rm eval       # même évaluation, sans réseau, FS en lecture seule, utilisateur non-root
```

Limites assumées : `classify_excerpt` est un substitut à mots-clés (à rebrancher sur `classify()` de acsi-audit-classify) ;
le planner est déterministe, donc ces tests valident la **plomberie et la gateway**, pas la qualité d'un LLM ;
tokens = estimation (~4 caractères/token), prix = valeurs de substitution (`policy.pricing_placeholder`).
