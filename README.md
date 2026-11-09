# acsi-agent

Agent d'audit **contraint** pour AuditPro/ACSI (ADR-0003) : tools typés, gateway d'actions, approbation humaine,
budget, journal rejouable, déploiement avec retour arrière. Le modèle propose, la gateway décide.

| Branche | Tag | Contenu |
|---|---|---|
| `phase-1-specs` | `phase-1` | contrats des tools, politique d'actions, 18 scénarios de référence, vérificateur de cohérence |
| `phase-2-runtime` | `phase-2` | runtime, journal chaîné, évaluation de tool correctness, mocks, Docker |
| `phase-3-gateway` | `phase-3` | API + `request_id`, gateway complète, approbation humaine, budget, traçabilité |
| `phase-4-ops` | `phase-4` | métriques, régression par baseline, replay, déploiement/rollback, démo |

`main` = état final (fast-forward de `phase-4-ops`).

## Démarrage

```bash
uv sync
uv run pytest                          # 147 tests
uv run python -m acsi_agent.eval       # scénarios de référence : tool correctness, statut, effets, coût
uv run python -m acsi_agent.regress    # rejeu contre baselines/reference.json
uv run python -m acsi_agent.demo       # tâche complète, replay, permissions, budget (docs/demo-transcript.md)
cp .env.example .env && docker compose up api   # API sur 127.0.0.1:8000
docker compose run --rm eval           # évaluation hermétique : sans réseau, FS en lecture seule
```

![Résultat de `docker compose run --rm eval`](docs/eval-run.png)

## Architecture

```
 client ──HTTP──► API (auth, rôle→portées, débit) ──► Runtime ──► Planner (mock | LLM à brancher)
                                                         │              │ propose {tool, args} ou conclut
                                                         ▼              ▼
                                                 Journal JSONL ◄── GATEWAY ──► Tool ──► Services simulés
                                              (append-only, chaîné)  interdits/allowlist · permission · schéma ·
                                                         ▲           règles d'arguments · plafond/tool · débit ·
                                                         │           APPROBATION HUMAINE · timeout · contrat de sortie
                                       état = rejeu du journal       + BUDGET (étapes, appels, tokens, coût, temps)
```

L'état d'une tâche est **toujours reconstruit depuis son journal** : une tâche en attente d'approbation est un journal qui s'arrête sur
`approval_requested` ; le replay et les métriques lisent la même source.

## Tools (détail : `docs/tools.md`)

| Tool | Effet | Permission | Approbation |
|---|---|---|---|
| `classify_excerpt`, `lookup_article`, `list_nonconformities` | lecture | `audit:read` | non |
| `draft_finding` | écriture réversible (brouillon interne) | `audit:draft` | non |
| `delete_draft` | irréversible local | `audit:delete` | **oui** |
| `submit_dossier` | irréversible externe (envoi à un tiers) | `audit:submit` | **oui** |

Interdits (jamais exécutés) : `shell_exec`, `http_request`, `read_file`, `write_file`, `delete_dossier`, `send_email_raw`, `modify_settings`, `create_account`.
Tout le reste est refusé par défaut. Politique complète : `docs/policy.md`.

## Garanties et leur test

| Garantie | Où | Test |
|---|---|---|
| Aucun appel hors gateway ; deny par défaut | `gateway.py` | `test_gateway_policy.py` |
| Permissions minimales (tâche en lecture seule par défaut, le rôle plafonne les portées) | `api.py`, `gateway.py` | `test_api.py`, mutation « sans contrôle de permission » |
| Un humain avant toute action irréversible, jamais sollicité pour un appel refusé | `agent.py` | `test_budget_approvals.py` |
| Approbation liée aux arguments, à usage unique, expirante, approbateur ≠ demandeur, journal intègre | `agent.py` | auto-approbation, rejeu d'approbation, expiration, journal altéré, redémarrage |
| Destinataire borné (un seul `@`, domaines autorisés) | `gateway.py` | `test_recipient_outside_allowlist…` (7 cas piégés) |
| Budget dur, plafonds réductibles jamais relevables, action proposée non exécutée | `budget.py` | une dimension par test + mutations |
| Chaque tool call tracé (tool, arguments, décision, résultat, erreur, latence) ; tokens/coût par appel de planner | `journal.py` | `test_every_tool_call_is_journaled…` |
| Les scénarios ont des dents : retirer un contrôle fait échouer un scénario | `eval.py` | 3 tests de mutation |
| Les mocks ne touchent pas au réseau | `backends.py` | `socket.connect` interdit pendant l'éval |

## Évaluation et régression

`eval.py` mesure, par scénario : **tool correct** (bon tool, bons arguments, bonne issue — ok / refus / erreur / approbation —, bon résultat),
statut final, **effets observables** (mails, brouillons : un refus doit laisser 0 effet). Agrégats : succès de tâche, sélection / arguments / issue / résultat,
étapes, **récupération après erreur** et **échecs propres**, coût par tâche.
`regress.py` compare à `baselines/reference.json` : succès, statut, **trajectoire**, étapes, tokens, coût ; l'**empreinte** (texte du prompt, modèle, planner, specs,
code des tools) doit correspondre, sinon code 2 jusqu'à régénération explicite (`--update-baseline`, jamais si un scénario échoue) et relecture du diff.
Déclencheurs : hook `pre-push` (`git config core.hooksPath scripts/hooks`), CI, portes de `deploy.sh`.

## Replay

`GET /tasks/{id}/timeline` (lecture humaine) et `GET /tasks/{id}/replay` : ré-exécution **isolée** (journal temporaire, services neufs) avec les sorties du planner
et les approbations enregistrées, passées dans la **vraie** gateway. `identical: false` signale que politique, tool ou données ont changé depuis l'enregistrement.
Les 18 scénarios se rejouent à l'identique. Un journal altéré, une tâche coupée par un redémarrage ou un dépassement de temps ne sont pas rejoués.

## Déploiement

`scripts/deploy.sh` / `scripts/rollback.sh`, voir `docs/DEPLOY.md`.

## Limites assumées

- **Le planner est un mock déterministe.** Les tests valident la plomberie, la gateway, l'approbation, le budget et le replay ; ils ne mesurent pas la qualité d'un LLM.
  Brancher un modèle = implémenter `Planner.plan` (recette : `prompt.build_prompt`) ; les scénarios `scripted` et `runaway` simulent un modèle trompé ou emballé.
- `classify_excerpt` est un substitut à mots-clés au contrat de `classify()` de acsi-audit-classify (à rebrancher). Les résumés d'articles du corpus sont des
  paraphrases internes, **pas du texte juridique** : à vérifier sur EUR-Lex avant tout usage de fond.
- Tokens estimés (~4 caractères/token) ; prix de substitution (`policy.pricing_placeholder`). Le « coût » mesuré est donc relatif, pas une facture.
- Services simulés en mémoire : une tâche en attente d'approbation ne survit pas à un redémarrage (elle est clôturée **sans exécuter** l'action).
- Un timeout ne tue pas le thread du tool : l'issue d'une écriture est alors signalée « indéterminée ».
- Le journal est chaîné par hachage (intégrité) mais non signé (pas d'authenticité). Les arguments y figurent en clair (adresse de destinataire comprise) : politique de rétention à définir.
- Image Docker et CI non exécutées dans l'environnement de livraison (voir `docs/DEPLOY.md`).
