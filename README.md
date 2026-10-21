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

## Phase 2 — runtime, journal, évaluation, mocks, Docker

| Module | Rôle |
|---|---|
| `tools_impl.py`, `backends.py` | tools sur services **simulés** (état isolé par tâche, `FakeMailGateway` avec pannes/latence injectables) |
| `gateway.py` | allowlist + interdits, validation JSON Schema des arguments, timeout, contrat de sortie. Les tools à approbation sont **refusés** (échec fermé) tant que la phase 3 n'existe pas |
| `journal.py` | un fichier JSONL par `request_id`, append-only, **chaîné par hachage** (`verify` détecte toute altération) |
| `agent.py` | boucle planner → gateway → tool ; chaque appel journalisé : tool choisi, arguments, décision gateway, résultat, erreur, latence ; tokens et coût par appel du planner |
| `planners.py` | `rule` (modèle compétent), `scripted` (modèle trompé ou maladroit), `runaway` (boucle sans fin) |
| `eval.py` | tool correctness (bon tool / bons paramètres / bonne issue / bon résultat) + statut final + effets observables |

```bash
uv run pytest                      # l'évaluation détecte un mauvais tool, de mauvais arguments, un appel superflu ;
                                   # les mocks ne font aucun appel réseau (socket.connect interdit pendant l'éval)
uv run python -m acsi_agent.eval   # scénarios de référence (en phase 2 seuls 8 sur 18 étaient exécutables)
docker compose run --rm eval       # même évaluation, sans réseau, FS en lecture seule, utilisateur non-root
```

Limites assumées : `classify_excerpt` est un substitut à mots-clés (à rebrancher sur `classify()` de acsi-audit-classify) ;
le planner est déterministe, donc ces tests valident la **plomberie et la gateway**, pas la qualité d'un LLM ;
tokens = estimation (~4 caractères/token), prix = valeurs de substitution (`policy.pricing_placeholder`).

## Phase 3 — API, gateway d'actions, approbation humaine, budget (cette branche)

**API** (`uvicorn acsi_agent.api:create_app --factory`, clés dans `ACSI_API_KEYS`, voir `.env.example`)

| Route | Rôle |
|---|---|
| `POST /tasks` | crée une tâche → `request_id` (`req_<32 hex>`), exécute jusqu'à la fin ou à la 1re demande d'approbation (201) |
| `GET /tasks/{id}` | état, étapes, métriques, approbation en attente (visible du propriétaire ou d'un approbateur, sinon 404) |
| `GET /tasks/{id}/trajectory` | événements du journal + vérification de la chaîne de hachage |
| `POST /tasks/{id}/approvals/{approval_id}` | `{"decision": "approve"\|"reject"}` ; l'approbateur ne fournit **jamais** d'arguments |

**Gateway** (`gateway.py`), ordre des contrôles : interdits/allowlist → permission (portée de la tâche) → schéma des arguments →
règle sur les arguments (domaine du destinataire, adresse unique) → plafond d'appels par tool et par tâche → débit par clé →
approbation. Deny par défaut. Un appel refusé ne sollicite jamais d'humain. Timeout et contrat de sortie à l'exécution.

**Approbation humaine** : exigée pour `submit_dossier` et `delete_draft`. Liée aux arguments exacts (hachage), à usage unique,
expire (900 s), réservée à `audit:approve`, approbateur ≠ demandeur, journal vérifié avant exécution, gateway réévaluée après approbation.

**Budget** par tâche (`policy.budget_default`, réductible par l'appelant, jamais augmentable) : étapes, appels de tools, tokens,
coût, temps actif. Dépassement = **abort** : l'action proposée n'est pas exécutée, les effets déjà produits sont listés, pas annulés.
Le dépassement maximal est borné par un appel de planner (tokens/coût ne sont connus qu'après).

**Traçabilité** : par appel du planner — tokens, coût, latence ; par tool — décision de gateway (contrôles passés), latence, résultat/erreur ;
par tâche — étapes, appels exécutés, erreurs, refus, attente humaine (exclue du temps actif).

```bash
uv run pytest        # + tests adverses : auto-approbation, rejeu d'approbation, expiration, journal altéré, redémarrage,
                     #   destinataires piégés, budget par dimension, mutations (retirer un contrôle fait échouer un scénario)
docker compose up api
```

Limites assumées : services simulés en mémoire (une tâche en attente d'approbation ne survit pas à un redémarrage : elle est clôturée
sans exécution) ; un timeout ne tue pas le thread du tool (issue d'une écriture « indéterminée », signalée) ; le planner est déterministe,
la garantie de sécurité vient de la gateway, pas du modèle ; la chaîne de hachage prouve l'intégrité, pas l'auteur (pas de signature).
