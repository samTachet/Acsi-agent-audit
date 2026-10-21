<!-- Généré par `python -m acsi_agent.docs_gen` à partir de specs/ et scenarios/. Ne pas éditer à la main. -->


# Politique d'actions

Politique par défaut : **deny** (tout tool absent de l'allowlist est refusé).

## Actions interdites (jamais exécutées, même avec toutes les portées)

- `shell_exec` — Exécution arbitraire sur l'hôte : hors périmètre de l'agent d'audit.
- `http_request` — Accès réseau arbitraire : exfiltration possible.
- `read_file` — Lecture de fichiers arbitraires : hors corpus et dossiers autorisés.
- `write_file` — Écriture de fichiers arbitraires.
- `delete_dossier` — Suppression d'un dossier d'audit : conservation et traçabilité obligatoires.
- `send_email_raw` — Envoi libre : seul submit_dossier, borné et approuvé, peut émettre.
- `modify_settings` — Modification de la configuration ou des permissions.
- `create_account` — Création de comptes ou d'identités.

## Actions soumises à approbation humaine

- `submit_dossier` — Action engageante vis-à-vis d'un tiers, non annulable.
- `delete_draft` — Perte définitive de contenu.

Règles d'approbation : expiration 900 s ; usage unique : True ; liée aux arguments exacts : True ; approbateur distinct du demandeur : True ; portée requise : `audit:approve`.

## Règles sur les arguments

- `submit_dossier.recipient` : email_domain_in ['client.example', 'cabinet.example'] → `RECIPIENT_NOT_ALLOWED`

## Rôles et portées

| Rôle | Portées |
|---|---|
| `viewer` | `audit:read` |
| `auditor` | `audit:read`, `audit:draft` |
| `lead_auditor` | `audit:read`, `audit:draft`, `audit:submit`, `audit:delete`, `audit:approve` |

Portées par défaut d'une tâche : `audit:read` (minimum nécessaire ; l'appelant doit en demander davantage explicitement).

## Budget par tâche

- `max_steps` : 12
- `max_tool_calls` : 10
- `max_tokens` : 20000
- `max_cost_usd` : 0.25
- `max_wall_s` : 60

En cas de dépassement : **abort**. Arrêt immédiat : l'action proposée n'est pas exécutée, les approbations en attente sont annulées, les effets déjà produits ne sont pas annulés et sont listés dans l'enregistrement final. Alerte à 80 % du plafond.

## Débit

- `tool_calls_per_minute_per_key` : 60
- `tasks_per_minute_per_key` : 10
