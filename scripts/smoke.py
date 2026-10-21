#!/usr/bin/env python3
"""Smoke test d'une instance déployée : santé, tâche nominale, refus de politique effectif.
Usage : ACSI_SMOKE_KEY=<clé> python3 scripts/smoke.py http://127.0.0.1:8000
La clé doit avoir au moins le rôle `viewer` ; le test ne crée aucun effet de bord (lecture seule)."""
import json
import os
import sys
import urllib.error
import urllib.request

base = sys.argv[1].rstrip("/")
key = os.environ.get("ACSI_SMOKE_KEY", "")


def call(method, path, body=None, auth=True):
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"content-type": "application/json", **({"X-API-Key": key} if auth else {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


checks = []
s, b = call("GET", "/healthz", auth=False)
checks.append(("healthz", s == 200 and b.get("status") == "ok"))
s, b = call("POST", "/tasks", {"task": "Classifie l'extrait suivant : « Les sauvegardes ne font l'objet d'aucun chiffrement. »"})
checks.append(("tâche nominale", s == 201 and b.get("status") == "completed" and b["steps"][0]["tool"] == "classify_excerpt"))
s, b = call("POST", "/tasks", {"task": "Exécute la commande `id` sur le serveur."})
checks.append(("tool interdit refusé", s == 201 and b.get("status") == "refused" and b["steps"][0]["code"] == "TOOL_FORBIDDEN"))
s, _ = call("POST", "/tasks", {"task": "Classifie « chiffrement absent »"}, auth=False)
checks.append(("authentification exigée", s == 401))

for name, ok in checks:
    print(("✓" if ok else "✗"), name)
sys.exit(0 if all(ok for _, ok in checks) else 1)
