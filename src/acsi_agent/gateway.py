"""Gateway d'actions : point de passage obligé de tout appel de tool.

Phase 2 (cette version) : allowlist + interdits, validation des arguments, timeout, contrat de sortie.
Tout tool soumis à approbation est REFUSÉ tant que l'approbation humaine n'existe pas (échec fermé).
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable

from jsonschema import Draft202012Validator, FormatChecker

from .specs import ToolSpec
from .tools_impl import ToolError
from .types import CallContext

Impl = Callable[[dict[str, Any], CallContext], dict[str, Any]]


@dataclass
class Decision:
    verdict: str  # allow | deny | needs_approval
    code: str | None = None
    reason: str = ""
    checks: list[str] = field(default_factory=list)


@dataclass
class Outcome:
    ok: bool
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    latency_ms: float = 0.0


class Gateway:
    def __init__(self, tools: dict[str, ToolSpec], policy: dict[str, Any], impls: dict[str, Impl],
                 timeout_overrides: dict[str, float] | None = None):
        self.tools = tools
        self.policy = policy
        self.impls = impls
        self.forbidden = {f["name"]: f["reason"] for f in policy["forbidden_actions"]}
        self.kinds = {e["code"]: e["kind"] for e in policy["common_errors"]}
        self.timeout_overrides = timeout_overrides or {}
        fc = FormatChecker()
        self._in = {n: Draft202012Validator(t.input_schema, format_checker=fc) for n, t in tools.items()}
        self._out = {n: Draft202012Validator(t.output_schema) for n, t in tools.items()}
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tool")

    def status_for(self, code: str) -> str:
        """Statut enregistré pour un refus de la gateway : denied (politique) ou error (arguments invalides, etc.)."""
        return "denied" if self.kinds.get(code, "denied") == "denied" else "error"

    # ------------------------------------------------------------ décision
    def evaluate(self, ctx: CallContext, tool: str, args: dict[str, Any]) -> Decision:
        checks: list[str] = []
        if tool in self.forbidden:
            return Decision("deny", "TOOL_FORBIDDEN", f"tool interdit : {self.forbidden[tool]}", checks + ["forbidden:fail"])
        if tool not in self.tools:
            return Decision("deny", "TOOL_NOT_ALLOWED", f"`{tool}` absent de l'allowlist", checks + ["allowlist:fail"])
        checks.append("allowlist:pass")
        spec = self.tools[tool]

        errors = sorted(self._in[tool].iter_errors(args), key=lambda e: list(e.path))
        if errors:
            e = errors[0]
            where = ".".join(str(p) for p in e.path) or "(racine)"
            return Decision("deny", "INVALID_ARGUMENTS", f"{where} : {e.message}"[:300], checks + ["schema:fail"])
        checks.append("schema:pass")

        if spec.requires_approval:
            return Decision("deny", "TOOL_NOT_ALLOWED", "approbation humaine indisponible dans ce runtime : refus par défaut",
                            checks + ["approval:unavailable"])
        return Decision("allow", None, "ok", checks)

    # ----------------------------------------------------------- exécution
    def execute(self, ctx: CallContext, tool: str, args: dict[str, Any]) -> Outcome:
        spec = self.tools[tool]
        timeout = self.timeout_overrides.get(tool, spec.timeout_s)
        t0 = time.perf_counter()
        fut = self._pool.submit(self.impls[tool], args, ctx)
        try:
            result = fut.result(timeout=timeout)
        except FutureTimeout:
            fut.cancel()
            unknown = spec.side_effect != "read"
            msg = f"délai de {timeout:g} s dépassé" + (" ; issue de l'écriture indéterminée" if unknown else "")
            return Outcome(False, error={"code": "TIMEOUT", "message": msg, "outcome_unknown": unknown},
                           latency_ms=(time.perf_counter() - t0) * 1000)
        except ToolError as e:
            return Outcome(False, error={"code": e.code, "message": e.message}, latency_ms=(time.perf_counter() - t0) * 1000)
        except Exception as e:  # noqa: BLE001 — le détail interne n'est pas exposé au planner
            return Outcome(False, error={"code": "INTERNAL_ERROR", "message": f"exception {type(e).__name__}"},
                           latency_ms=(time.perf_counter() - t0) * 1000)
        latency = (time.perf_counter() - t0) * 1000
        if next(self._out[tool].iter_errors(result), None) is not None:
            return Outcome(False, error={"code": "INTERNAL_ERROR", "message": "sortie non conforme au schéma de sortie"}, latency_ms=latency)
        return Outcome(True, result=result, latency_ms=latency)
