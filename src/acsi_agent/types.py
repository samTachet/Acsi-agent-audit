"""Types partagés : actions du planner, étapes enregistrées, contexte d'appel."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

REQUEST_ID_RE = re.compile(r"^req_[0-9a-f]{32}$")


def new_request_id() -> str:
    return "req_" + uuid.uuid4().hex


@dataclass(frozen=True)
class ToolAction:
    tool: str
    args: dict[str, Any]


@dataclass(frozen=True)
class Finish:
    status: str  # completed | refused | failed
    answer: str


@dataclass
class StepRecord:
    """Un appel de tool arrivé à son terme, tel que le planner le voit."""
    step: int
    tool: str
    args: dict[str, Any]
    status: str  # ok | error | denied | rejected
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    approval_id: str | None = None
    verdict: str = "allow"  # allow | needs_approval | deny (décision de la gateway)


@dataclass
class PlanContext:
    task: str
    scopes: list[str]
    steps: list[StepRecord] = field(default_factory=list)


@dataclass(frozen=True)
class CallContext:
    request_id: str
    step: int
    scopes: tuple[str, ...] = ()
    key_id: str = "anonymous"


class Planner(Protocol):
    id: str

    def plan(self, ctx: PlanContext) -> ToolAction | Finish: ...
