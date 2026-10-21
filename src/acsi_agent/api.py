"""API HTTP : une tâche = un `request_id`, approbation humaine par endpoint dédié.

Lancement : uvicorn acsi_agent.api:create_app --factory
Clés : variable ACSI_API_KEYS = {"<clé>": {"id": "alice", "role": "lead_auditor"}, ...} (aucune clé = tout est refusé).
"""
from __future__ import annotations

import hmac
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .agent import ApprovalError, Runtime, TaskInput, fingerprint
from .ratelimit import RateLimiter
from .specs import load_policy
from .types import REQUEST_ID_RE

STATUS_BY_APPROVAL_ERROR = {
    "INVALID_DECISION": 422, "NOT_PENDING": 409, "FORBIDDEN_SCOPE": 403, "SELF_APPROVAL": 403,
    "JOURNAL_TAMPERED": 409, "EXPIRED": 409, "ARGS_MISMATCH": 409,
}


@dataclass(frozen=True)
class Principal:
    id: str
    role: str
    scopes: tuple[str, ...]


class TaskRequest(BaseModel):
    task: str = Field(min_length=3, max_length=2000)
    scopes: list[str] | None = None
    budget: dict[str, float] | None = None


class ApprovalRequest(BaseModel):
    decision: str


def _err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": {"code": code, "message": message}})


def create_app(runtime: Runtime | None = None, api_keys: dict[str, dict[str, str]] | None = None) -> FastAPI:
    policy = load_policy()
    keys = api_keys if api_keys is not None else json.loads(os.environ.get("ACSI_API_KEYS", "{}"))
    runtime = runtime or Runtime(Path(os.environ.get("ACSI_JOURNAL_DIR", "data/journal")))
    task_limiter = RateLimiter(policy["rate_limits"]["tasks_per_minute_per_key"])
    approver_scope = policy["approval"]["approver_scope"]
    app = FastAPI(title="acsi-agent", version=__version__)
    app.state.runtime = runtime

    def principal(x_api_key: str | None = Header(default=None)) -> Principal:
        if x_api_key:
            for k, v in keys.items():  # comparaison à temps constant sur chaque clé
                if hmac.compare_digest(k.encode(), x_api_key.encode()):
                    role = v["role"]
                    return Principal(v["id"], role, tuple(policy["roles"][role]))
        raise _err(401, "UNAUTHENTICATED", "clé d'API absente ou invalide")

    def visible(p: Principal, rid: str):
        """Une tâche n'est visible que de son propriétaire ou d'un approbateur ; sinon 404 (pas d'énumération)."""
        if not REQUEST_ID_RE.fullmatch(rid):
            raise _err(404, "NOT_FOUND", "tâche inconnue")
        try:
            res = runtime.refresh(rid)
        except KeyError:
            raise _err(404, "NOT_FOUND", "tâche inconnue") from None
        if res.owner != p.id and approver_scope not in p.scopes:
            raise _err(404, "NOT_FOUND", "tâche inconnue")
        return res

    @app.exception_handler(HTTPException)
    async def _http_exc(_: Request, exc: HTTPException):
        body = exc.detail if isinstance(exc.detail, dict) and "error" in exc.detail else {"error": {"code": "HTTP_ERROR", "message": str(exc.detail)}}
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "version": __version__, "fingerprint": fingerprint("rule-planner-v1")}

    @app.post("/tasks", status_code=201)
    def create_task(body: TaskRequest, p: Principal = Depends(principal)):
        if not task_limiter.consume(p.id):
            raise _err(429, "RATE_LIMITED", "trop de tâches par minute pour cette clé")
        scopes = body.scopes if body.scopes is not None else list(policy["default_task_scopes"])
        if approver_scope in scopes or set(scopes) - set(policy["scopes"]):
            raise _err(422, "INVALID_SCOPES", f"portées invalides (`{approver_scope}` n'est pas une portée de tâche)")
        if set(scopes) - set(p.scopes):
            raise _err(403, "SCOPE_NOT_GRANTED", f"le rôle `{p.role}` ne permet pas : {sorted(set(scopes) - set(p.scopes))}")
        try:
            res = runtime.start(TaskInput(task=body.task, scopes=scopes, owner=p.id, budget=body.budget or {}))
        except ValueError as e:
            raise _err(422, "INVALID_BUDGET", str(e)) from None
        return res.public()

    @app.get("/tasks/{request_id}")
    def get_task(request_id: str, p: Principal = Depends(principal)):
        return visible(p, request_id).public()

    @app.get("/tasks/{request_id}/trajectory")
    def trajectory(request_id: str, p: Principal = Depends(principal)):
        res = visible(p, request_id)
        ok, why = runtime.journal.verify(request_id)
        return {"request_id": request_id, "chain_ok": ok, "chain_detail": why, "events": res.events}

    @app.post("/tasks/{request_id}/approvals/{approval_id}")
    def approve(request_id: str, approval_id: str, body: ApprovalRequest, p: Principal = Depends(principal)):
        visible(p, request_id)
        try:
            return runtime.resolve_approval(request_id, approval_id, body.decision, p.id, p.scopes).public()
        except ApprovalError as e:
            raise _err(STATUS_BY_APPROVAL_ERROR.get(e.code, 400), e.code, e.message) from None

    return app
