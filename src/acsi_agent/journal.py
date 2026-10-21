"""Journal d'événements append-only, un fichier JSONL par requête, chaîné par hachage.

Chaque événement porte `prev` (hachage du précédent) et `hash` : toute modification
a posteriori d'une ligne est détectée par `verify`. La chaîne prouve l'intégrité du
fichier, pas l'authenticité de son auteur (pas de signature).
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .types import REQUEST_ID_RE

GENESIS = "0" * 64


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class Journal:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def path(self, request_id: str) -> Path:
        # Validation stricte : l'identifiant ne doit jamais pouvoir désigner un autre chemin.
        if not REQUEST_ID_RE.fullmatch(request_id):
            raise ValueError("request_id invalide")
        return self.dir / f"{request_id}.jsonl"

    def read(self, request_id: str) -> list[dict[str, Any]]:
        p = self.path(request_id)
        if not p.exists():
            return []
        with p.open(encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def append(self, request_id: str, type_: str, data: dict[str, Any], ts: float | None = None) -> dict[str, Any]:
        with self._lock:
            events = self.read(request_id)
            prev = events[-1]["hash"] if events else GENESIS
            core = {"seq": len(events) + 1, "ts": ts if ts is not None else time.time(),
                    "request_id": request_id, "type": type_, "data": data, "prev": prev}
            event = {**core, "hash": hashlib.sha256((prev + _canon(core)).encode()).hexdigest()}
            with self.path(request_id).open("a", encoding="utf-8") as f:
                f.write(_canon(event) + "\n")
                f.flush()
                os.fsync(f.fileno())
            return event

    def verify(self, request_id: str) -> tuple[bool, str]:
        prev = GENESIS
        for i, e in enumerate(self.read(request_id), start=1):
            core = {k: v for k, v in e.items() if k != "hash"}
            if e.get("seq") != i or e.get("prev") != prev:
                return False, f"chaîne rompue à l'événement {i}"
            if hashlib.sha256((prev + _canon(core)).encode()).hexdigest() != e.get("hash"):
                return False, f"contenu altéré à l'événement {i}"
            prev = e["hash"]
        return True, "ok"

    def list_ids(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("req_*.jsonl") if REQUEST_ID_RE.fullmatch(p.stem))
