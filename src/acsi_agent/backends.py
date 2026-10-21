"""Services simulés (mocks) : aucune dépendance réseau, état isolé par tâche.

Le seul service « externe » du périmètre est l'envoi de courrier de submit_dossier ;
FakeMailGateway le remplace et permet d'injecter pannes et latence.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .specs import ROOT

FIXTURES_PATH = ROOT / "data" / "fixtures.json"


class ExternalUnavailable(Exception):
    """Le service externe ne répond pas."""


@dataclass
class FakeMailGateway:
    sent: list[dict[str, str]] = field(default_factory=list)
    fault: str | None = None  # "unavailable"
    latency_s: float = 0.0

    def send(self, recipient: str, subject: str, body: str) -> str:
        if self.latency_s:
            time.sleep(self.latency_s)
        if self.fault == "unavailable":
            raise ExternalUnavailable("service d'envoi indisponible")
        self.sent.append({"recipient": recipient, "subject": subject, "body": body})
        return f"MSG-{len(self.sent):04d}"


@dataclass
class Services:
    corpus: dict[str, Any]
    dossiers: dict[str, Any]
    drafts: dict[str, Any]
    mail: FakeMailGateway
    submissions: list[dict[str, Any]] = field(default_factory=list)
    drafts_created: int = 0
    drafts_deleted: int = 0

    @classmethod
    def from_fixtures(cls, faults: dict[str, Any] | None = None, path: Path | None = None) -> "Services":
        raw = json.loads((path or FIXTURES_PATH).read_text(encoding="utf-8"))
        faults = faults or {}
        mail = FakeMailGateway(fault=faults.get("mail"), latency_s=float(faults.get("mail_latency_s", 0.0)))
        return cls(
            corpus=copy.deepcopy(raw["corpus"]),
            dossiers=copy.deepcopy(raw["dossiers"]),
            drafts=copy.deepcopy(raw["drafts"]),
            mail=mail,
        )

    def effects(self) -> dict[str, int]:
        """Effets observables, comparés aux attentes des scénarios."""
        return {
            "mail_sent": len(self.mail.sent),
            "drafts_created": self.drafts_created,
            "drafts_deleted": self.drafts_deleted,
        }


def fixtures_hash(path: Path | None = None) -> str:
    return hashlib.sha256((path or FIXTURES_PATH).read_bytes()).hexdigest()[:16]
