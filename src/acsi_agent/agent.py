"""Runtime de l'agent : planner → gateway → tool, piloté par le journal (event sourcing).

L'état d'une tâche est *toujours* reconstruit depuis son journal : une tâche en attente d'approbation
est donc simplement un journal qui s'arrête sur `approval_requested`. `resolve_approval` le reprend.

Garanties :
- aucun appel de tool ne contourne la gateway (ni à l'exécution directe, ni après approbation : la gateway
  réévalue l'appel au moment de l'exécution, approbation comprise) ;
- l'approbation est liée aux arguments EXACTS demandés (hachage), à usage unique, expire, et ne peut venir
  que d'un humain distinct du demandeur disposant de la portée `audit:approve` ;
- le budget est vérifié avant chaque appel du planner et avant chaque exécution de tool.

Limite connue : les services simulés vivent en mémoire. Après un redémarrage du processus, une tâche en
attente d'approbation ne peut plus être exécutée et est clôturée en `failed` (RUNTIME_RESTARTED).
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .backends import Services, fixtures_hash
from .budget import Budget, Meter, Violation, check_post_planner, check_pre_planner, check_pre_tool, ratios
from .gateway import Decision, Gateway
from .journal import Journal
from .planners import make_planner
from .prompt import PROMPT_VERSION, SYSTEM_PROMPT, build_prompt, estimate_tokens
from .ratelimit import RateLimiter
from .specs import ToolSpec, load_policy, load_tools, specs_hash
from .tools_impl import build_impls
from .types import CallContext, Finish, PlanContext, Planner, StepRecord, ToolAction, new_request_id

CAPABILITY_PHASE = 3
MODEL_ID = "mock-deterministic"
TERMINAL = {"completed", "refused", "failed", "budget_exceeded", "expired"}


class ApprovalError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class TaskInput:
    task: str
    scopes: list[str] = field(default_factory=lambda: ["audit:read"])
    owner: str = "anonymous"
    planner: dict[str, Any] = field(default_factory=lambda: {"type": "rule"})
    faults: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskResult:
    request_id: str
    status: str  # awaiting_approval | completed | refused | failed | budget_exceeded | expired
    answer: str
    steps: list[StepRecord]
    effects: dict[str, int]
    metrics: dict[str, Any]
    events: list[dict[str, Any]]
    owner: str = ""
    pending_approval: dict[str, Any] | None = None

    def public(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id, "status": self.status, "answer": self.answer,
            "pending_approval": self.pending_approval, "effects": self.effects, "metrics": self.metrics,
            "steps": [{"step": s.step, "tool": s.tool, "status": s.status,
                       "code": (s.error or {}).get("code"), "approval_id": s.approval_id} for s in self.steps],
        }


def fingerprint(planner_id: str) -> dict[str, str]:
    """Ce qui, en changeant, impose de rejouer les scénarios de référence : prompt, modèle, tools (specs et code)."""
    return {
        "prompt_version": PROMPT_VERSION,
        "prompt_hash": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:16],
        "model_id": MODEL_ID,
        "planner_id": planner_id,
        "specs_hash": specs_hash(),
        "tools_code_hash": hashlib.sha256((Path(__file__).parent / "tools_impl.py").read_bytes()).hexdigest()[:16],
    }


def args_hash(args: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


# ------------------------------------------------------------------ état
@dataclass
class State:
    request_id: str
    task: str = ""
    scopes: list[str] = field(default_factory=list)
    owner: str = ""
    budget: Budget | None = None
    steps: list[StepRecord] = field(default_factory=list)
    pending: dict[str, Any] | None = None
    finished: dict[str, Any] | None = None
    planner_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    executed: int = 0
    planner_ms: float = 0.0
    tool_ms: float = 0.0
    human_wait_s: float = 0.0
    warned: set[str] = field(default_factory=set)
    call_counts: dict[str, int] = field(default_factory=dict)
    pending_ts: float = 0.0
    budget_dimension: str | None = None

    def apply(self, ev: dict[str, Any]) -> None:
        t, d = ev["type"], ev["data"]
        if t == "task_started":
            self.task, self.scopes, self.owner = d["task"], list(d["scopes"]), d["owner"]
            self.budget = Budget(**d["budget"]["effective"])
        elif t == "planner_output":
            self.planner_calls += 1
            u = d["usage"]
            self.tokens_in += u["tokens_in"]
            self.tokens_out += u["tokens_out"]
            self.cost += u["cost_usd"]
            self.planner_ms += d["latency_ms"]
        elif t == "tool_call":
            self.steps.append(StepRecord(d["step"], d["tool"], d["args"], d["status"], d["result"], d["error"],
                                         d["approval_id"], d["gateway"]["verdict"]))
            if d["executed"]:
                self.executed += 1
                self.tool_ms += d["latency_ms"]
            if d["gateway"]["verdict"] in {"allow", "needs_approval"}:
                self.call_counts[d["tool"]] = self.call_counts.get(d["tool"], 0) + 1
        elif t == "approval_requested":
            self.pending, self.pending_ts = d, ev["ts"]
        elif t == "approval_resolved":
            self.human_wait_s += max(0.0, ev["ts"] - self.pending_ts)
            self.pending = None
        elif t == "budget_warning":
            self.warned.add(d["dimension"])
        elif t == "budget_exceeded":
            self.budget_dimension = d["dimension"]
        elif t == "task_finished":
            self.finished = d

    def meter(self) -> Meter:
        return Meter(self.planner_calls, self.executed, self.tokens_in + self.tokens_out, self.cost,
                     (self.planner_ms + self.tool_ms) / 1000)

    def metrics(self) -> dict[str, Any]:
        m = {
            "steps": len(self.steps), "planner_calls": self.planner_calls, "tool_calls_executed": self.executed,
            "errors": sum(1 for s in self.steps if s.status == "error"),
            "denied": sum(1 for s in self.steps if s.status in {"denied", "rejected"}),
            "tokens_in": self.tokens_in, "tokens_out": self.tokens_out, "tokens": self.tokens_in + self.tokens_out,
            "cost_usd": round(self.cost, 6), "planner_ms": round(self.planner_ms, 2), "tool_ms": round(self.tool_ms, 2),
            "active_ms": round(self.planner_ms + self.tool_ms, 2), "human_wait_s": round(self.human_wait_s, 3),
        }
        if self.budget_dimension:
            m["budget_dimension"] = self.budget_dimension
        return m


@dataclass
class Live:
    svc: Services
    gateway: Gateway
    planner: Planner


class Runtime:
    def __init__(self, journal_dir: Path, tools: dict[str, ToolSpec] | None = None, policy: dict[str, Any] | None = None,
                 timeout_overrides: dict[str, float] | None = None, clock: Callable[[], float] = time.time,
                 limiter_clock: Callable[[], float] = time.monotonic):
        self.journal = Journal(journal_dir)
        self.tools = tools or load_tools()
        self.policy = policy or load_policy()
        self.timeout_overrides = timeout_overrides
        self.clock = clock
        self.pricing = self.policy["pricing_placeholder"]
        self.tool_limiter = RateLimiter(self.policy["rate_limits"]["tool_calls_per_minute_per_key"], limiter_clock)
        self._live: dict[str, Live] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()

    # ------------------------------------------------------------ outils internes
    def _lock(self, rid: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(rid, threading.RLock())

    def _cost(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in * self.pricing["in_usd_per_mtok"] + tokens_out * self.pricing["out_usd_per_mtok"]) / 1_000_000

    def _emit(self, st: State, type_: str, data: dict[str, Any]) -> dict[str, Any]:
        ev = self.journal.append(st.request_id, type_, data, ts=self.clock())
        st.apply(ev)
        return ev

    def _state(self, rid: str) -> State:
        events = self.journal.read(rid)
        if not events:
            raise KeyError(rid)
        st = State(rid)
        for e in events:
            st.apply(e)
        return st

    def _result(self, st: State) -> TaskResult:
        live = self._live.get(st.request_id)
        if st.finished:
            status, answer, effects = st.finished["status"], st.finished["answer"], st.finished["effects"]
        else:
            status, answer = ("awaiting_approval" if st.pending else "running"), ""
            effects = live.svc.effects() if live else {}
        pending = None
        if st.pending and not st.finished:
            p = st.pending
            pending = {"approval_id": p["approval_id"], "tool": p["tool"], "args": p["args"], "step": p["step"],
                       "expires_at": p["expires_at"], "requested_by": p["requested_by"]}
        return TaskResult(st.request_id, status, answer, list(st.steps), effects, st.metrics(),
                          self.journal.read(st.request_id), st.owner, pending)

    def result(self, rid: str) -> TaskResult:
        return self._result(self._state(rid))

    # ------------------------------------------------------------ cycle de vie
    def start(self, ti: TaskInput, request_id: str | None = None, planner: Planner | None = None) -> TaskResult:
        rid = request_id or new_request_id()
        budget, clamped = Budget.effective(self.policy["budget_default"], ti.budget)
        svc = Services.from_fixtures(ti.faults)
        impls = build_impls(svc)
        lat = float(ti.faults.get("tool_latency_s", 0.0))
        if lat:  # injection de latence pour éprouver le budget de temps
            impls = {n: (lambda f: (lambda a, c: (time.sleep(lat), f(a, c))[1]))(f) for n, f in impls.items()}
        gw = Gateway(self.tools, self.policy, impls, self.timeout_overrides, self.tool_limiter)
        planner = planner or make_planner(ti.planner)
        with self._lock(rid):
            if self.journal.read(rid):
                raise ValueError("request_id déjà utilisé")
            self._live[rid] = Live(svc, gw, planner)
            st = State(rid)
            self._emit(st, "task_started", {
                "task": ti.task, "scopes": ti.scopes, "owner": ti.owner, "planner": ti.planner, "faults": ti.faults,
                "budget": {"effective": budget.as_dict(), "clamped_to_default": clamped},
                "fingerprint": fingerprint(planner.id), "fixtures_hash": fixtures_hash(), "app_version": __version__,
            })
            self._drive(st)
            return self._result(st)

    def run(self, ti: TaskInput, request_id: str | None = None, planner: Planner | None = None) -> TaskResult:
        """Alias : démarre et pilote la tâche jusqu'à sa fin ou jusqu'à la première demande d'approbation."""
        return self.start(ti, request_id, planner)

    def refresh(self, rid: str) -> TaskResult:
        """Applique l'expiration d'une approbation échue (appelé avant toute lecture ou résolution)."""
        with self._lock(rid):
            st = self._state(rid)
            if st.pending and not st.finished and self.clock() > st.pending["expires_at"]:
                self._expire(st)
            return self._result(st)

    def resolve_approval(self, rid: str, approval_id: str, decision: str, approver_id: str,
                         approver_scopes: tuple[str, ...] | list[str]) -> TaskResult:
        if decision not in {"approve", "reject"}:
            raise ApprovalError("INVALID_DECISION", "decision doit valoir approve ou reject")
        pol = self.policy["approval"]
        with self._lock(rid):
            st = self._state(rid)
            if st.finished or not st.pending or st.pending["approval_id"] != approval_id:
                raise ApprovalError("NOT_PENDING", "aucune approbation en attente avec cet identifiant")
            if pol["approver_scope"] not in approver_scopes:
                raise ApprovalError("FORBIDDEN_SCOPE", f"la portée `{pol['approver_scope']}` est requise pour approuver")
            if pol["require_distinct_approver"] and approver_id == st.owner:
                raise ApprovalError("SELF_APPROVAL", "l'approbateur doit être distinct du demandeur")
            ok, why = self.journal.verify(rid)
            if not ok:
                raise ApprovalError("JOURNAL_TAMPERED", f"journal altéré ({why}) : approbation refusée")
            if self.clock() > st.pending["expires_at"]:
                self._expire(st)
                raise ApprovalError("EXPIRED", "l'approbation a expiré")
            pend = dict(st.pending)
            if args_hash(pend["args"]) != pend["args_hash"]:
                raise ApprovalError("ARGS_MISMATCH", "arguments différents de ceux demandés : approbation refusée")
            live = self._live.get(rid)
            if live is None:
                self._emit(st, "approval_resolved", {"approval_id": approval_id, "decision": decision, "approver": approver_id})
                self._finish(st, "failed", "Le runtime a redémarré pendant l'attente : l'action n'a pas été exécutée (RUNTIME_RESTARTED).")
                return self._result(st)
            self._emit(st, "approval_resolved", {"approval_id": approval_id, "decision": decision, "approver": approver_id})
            if decision == "reject":
                self._record(st, pend["step"], pend["tool"], pend["args"], "rejected",
                             Decision("needs_approval", None, "approbation refusée", ["approval:rejected"]),
                             error={"code": "APPROVAL_REJECTED", "message": f"refusé par {approver_id}"},
                             approval_id=approval_id)
            else:
                ctx = self._ctx(st, pend["step"])
                dec = live.gateway.evaluate(ctx, pend["tool"], pend["args"], call_counts=st.call_counts, approved=True)
                if dec.verdict != "allow":  # la politique a pu changer pendant l'attente
                    self._record(st, pend["step"], pend["tool"], pend["args"], live.gateway.status_for(dec.code or ""), dec,
                                 error={"code": dec.code, "message": dec.reason}, approval_id=approval_id)
                else:
                    v = self._execute(st, live, pend["step"], pend["tool"], pend["args"], dec, approval_id)
                    if v:
                        self._budget_stop(st, v, {"tool": pend["tool"], "args": pend["args"]})
                        return self._result(st)
            self._drive(st)
            return self._result(st)

    # ------------------------------------------------------------ boucle
    def _ctx(self, st: State, step: int) -> CallContext:
        return CallContext(request_id=st.request_id, step=step, scopes=tuple(st.scopes), key_id=st.owner)

    def _warn(self, st: State) -> None:
        wr = self.policy["budget_on_exceed"]["warn_ratio"]
        for dim, r in ratios(st.budget, st.meter()).items():  # type: ignore[arg-type]
            if r >= wr and dim not in st.warned:
                self._emit(st, "budget_warning", {"dimension": dim, "ratio": round(r, 3)})

    def _drive(self, st: State) -> None:
        live = self._live.get(st.request_id)
        if live is None:
            self._finish(st, "failed", "Le runtime a redémarré : tâche interrompue (RUNTIME_RESTARTED).")
            return
        b = st.budget
        assert b is not None
        while not st.finished and not st.pending:
            v = check_pre_planner(b, st.meter())
            if v:
                self._budget_stop(st, v, None)
                return
            t0 = time.perf_counter()
            out = live.planner.plan(PlanContext(task=st.task, scopes=list(st.scopes), steps=list(st.steps)))
            prompt = build_prompt(st.task, st.steps, self.tools)
            t_in = estimate_tokens(prompt)
            t_out = estimate_tokens(json.dumps({"tool": out.tool, "args": out.args} if isinstance(out, ToolAction)
                                               else {"finish": out.answer}, ensure_ascii=False))
            usage = {"tokens_in": t_in, "tokens_out": t_out, "cost_usd": self._cost(t_in, t_out)}
            lat = (time.perf_counter() - t0) * 1000
            if isinstance(out, Finish):
                self._emit(st, "planner_output", {"kind": "finish", "status": out.status, "answer": out.answer, "usage": usage, "latency_ms": lat})
                v = check_post_planner(b, st.meter())
                if v:  # plafond strict : même la conclusion est abandonnée si le dernier appel du planner le dépasse
                    self._budget_stop(st, v, {"finish": out.answer})
                    return
                self._finish(st, out.status, out.answer)
                return
            step_no = len(st.steps) + 1
            self._emit(st, "planner_output", {"kind": "tool_call", "step": step_no, "tool": out.tool, "args": out.args,
                                              "usage": usage, "latency_ms": lat})
            v = check_post_planner(b, st.meter())
            if v:
                self._budget_stop(st, v, {"tool": out.tool, "args": out.args})
                return
            self._warn(st)
            dec = live.gateway.evaluate(self._ctx(st, step_no), out.tool, out.args, call_counts=st.call_counts)
            if dec.verdict == "deny":
                self._record(st, step_no, out.tool, out.args, live.gateway.status_for(dec.code or ""), dec,
                             error={"code": dec.code, "message": dec.reason})
            elif dec.verdict == "needs_approval":
                ttl = self.policy["approval"]["ttl_s"]
                self._emit(st, "approval_requested", {
                    "approval_id": _short("apr_", st.request_id, step_no), "step": step_no, "tool": out.tool,
                    "args": out.args, "args_hash": args_hash(out.args), "requested_by": st.owner,
                    "expires_at": self.clock() + ttl, "why": next((a["why"] for a in self.policy["approval_required"] if a["tool"] == out.tool), ""),
                })
                return
            else:
                v = self._execute(st, live, step_no, out.tool, out.args, dec, None)
                if v:
                    self._budget_stop(st, v, {"tool": out.tool, "args": out.args})
                    return

    def _execute(self, st: State, live: Live, step: int, tool: str, args: dict[str, Any], dec: Decision,
                 approval_id: str | None) -> Violation | None:
        v = check_pre_tool(st.budget, st.meter())  # type: ignore[arg-type]
        if v:
            return v
        out = live.gateway.execute(self._ctx(st, step), tool, args)
        self._record(st, step, tool, args, "ok" if out.ok else "error", dec, result=out.result, error=out.error,
                     latency=out.latency_ms, executed=True, approval_id=approval_id)
        return None

    def _record(self, st: State, step: int, tool: str, args: dict[str, Any], status: str, dec: Decision, *,
                result: dict[str, Any] | None = None, error: dict[str, Any] | None = None, latency: float = 0.0,
                executed: bool = False, approval_id: str | None = None) -> None:
        self._emit(st, "tool_call", {
            "step": step, "tool": tool, "args": args, "status": status,
            "gateway": {"verdict": dec.verdict, "code": dec.code, "reason": dec.reason, "checks": dec.checks},
            "result": result, "error": error, "latency_ms": latency, "approval_id": approval_id, "executed": executed,
        })

    # ------------------------------------------------------------ fins de tâche
    def _budget_stop(self, st: State, v: Violation, proposed: dict[str, Any] | None) -> None:
        live = self._live.get(st.request_id)
        self._emit(st, "budget_exceeded", {
            "dimension": v.dimension, "limit": v.limit, "observed": v.observed, "action": self.policy["budget_on_exceed"]["action"],
            "proposed_action_not_executed": proposed, "budget": st.budget.as_dict() if st.budget else None,
        })
        eff = live.svc.effects() if live else {}
        self._finish(st, "budget_exceeded",
                     f"Budget dépassé ({v.dimension} : {v.observed} > {v.limit}). Exécution interrompue ; "
                     f"l'action proposée n'a pas été exécutée. Effets déjà produits : {eff}.")

    def _expire(self, st: State) -> None:
        p = st.pending or {}
        self._emit(st, "approval_resolved", {"approval_id": p.get("approval_id"), "decision": "expired", "approver": None})
        self._record(st, p["step"], p["tool"], p["args"], "rejected",
                     Decision("needs_approval", None, "approbation expirée", ["approval:expired"]),
                     error={"code": "APPROVAL_EXPIRED", "message": "non approuvé avant expiration"}, approval_id=p.get("approval_id"))
        self._finish(st, "expired", f"Approbation de `{p.get('tool')}` expirée : action non exécutée.")

    def _finish(self, st: State, status: str, answer: str) -> None:
        live = self._live.pop(st.request_id, None)
        effects = live.svc.effects() if live else {}
        self._emit(st, "task_finished", {"status": status, "answer": answer, "metrics": st.metrics(), "effects": effects})


def _short(prefix: str, *parts: object) -> str:
    return prefix + hashlib.sha1(":".join(map(str, parts)).encode()).hexdigest()[:12]
