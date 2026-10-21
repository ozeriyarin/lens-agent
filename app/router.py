"""Routing: cheap model first, escalate only when needed (constitution III, feature 002).

Why this module exists: feature 001 wired reserve -> scan -> settle straight
into the HTTP handler. With a second tier that choreography happens twice,
with different failure rules for each tier, so it moves here where it can be
tested without FastAPI. `/scan` becomes: gate, cache, `router.run`, respond.

The policy is deliberately *structural*: weakness is read off the typed
result (no objects, low best confidence, question left unanswered). Asking a
second model to judge the first would cost what the escalation saves.

Failure rules differ by tier on purpose:
- Primary fails -> the request fails. A failure is not "low confidence", and
  retrying on a pricier model would turn every outage into a cost spike.
- Escalation refused by the budget, or fails -> return the primary result.
  The person still gets an answer; the ledger keeps the failed charge.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from pydantic_ai import Agent

from app.agent import AgentFailed, scan
from app.budget import Budget, BudgetExceeded
from app.gate import PreparedFrame
from app.schema import ScanResult

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Routed:
    """What the router hands back: the result plus the routing facts the response reports."""

    result: ScanResult
    model: str  # whose result this is
    primary_model: str
    escalated: bool
    cost_usd: float  # both calls, including a failed escalation attempt
    latency_ms: int  # both calls
    request_id: str  # the primary call's ledger id; the cache key for the response


def is_weak(result: ScanResult, question: str | None, threshold: float) -> bool:
    """002 FR-1: should this primary result be escalated?"""
    if not result.objects:
        return True
    if max(o.confidence for o in result.objects) < threshold:
        return True
    if question and question.strip():
        answered = bool(result.answer) or any(o.answer for o in result.objects)
        if not answered:
            return True
    return False


class Router:
    def __init__(
        self,
        budget: Budget,
        primary: Agent[None, ScanResult],
        primary_model: str,
        escalation: Agent[None, ScanResult] | None = None,
        escalation_model: str = "",
        escalate_below: float = 0.6,
    ) -> None:
        self.budget = budget
        self.primary = primary
        self.primary_model = primary_model
        self.escalation = escalation if escalation_model else None
        self.escalation_model = escalation_model
        self.escalate_below = escalate_below

    @property
    def enabled(self) -> bool:
        return self.escalation is not None

    async def _paid_call(self, agent: Agent[None, ScanResult], model: str, frame: PreparedFrame,
                         question: str | None, tier: str, source: str):
        """reserve -> scan -> settle, returning (outcome, ledger entry). Raises on any failure
        after marking the reservation failed; raises BudgetExceeded before any call."""
        request_id = self.budget.reserve(model, self.budget.estimate_for(model), tier=tier, source=source)
        started = time.perf_counter()
        try:
            outcome = await scan(agent, frame, question)
        except AgentFailed as e:
            self.budget.fail(request_id, input_tokens=e.input_tokens, output_tokens=e.output_tokens,
                             latency_ms=int((time.perf_counter() - started) * 1000))
            raise
        except BaseException:
            self.budget.fail(request_id, latency_ms=int((time.perf_counter() - started) * 1000))
            raise
        entry = self.budget.settle(
            request_id,
            input_tokens=outcome.input_tokens,
            output_tokens=outcome.output_tokens,
            latency_ms=outcome.latency_ms,
        )
        return outcome, entry

    async def run(self, frame: PreparedFrame, question: str | None = None, source: str = "tap") -> Routed:
        outcome, entry = await self._paid_call(self.primary, self.primary_model, frame, question, "primary", source)
        routed = Routed(
            result=outcome.result,
            model=self.primary_model,
            primary_model=self.primary_model,
            escalated=False,
            cost_usd=entry.cost_usd,
            latency_ms=entry.latency_ms,
            request_id=entry.request_id,
        )
        if not self.enabled or not is_weak(outcome.result, question, self.escalate_below):
            return routed

        try:
            outcome2, entry2 = await self._paid_call(
                self.escalation, self.escalation_model, frame, question, "escalation", source  # type: ignore[arg-type]
            )
        except BudgetExceeded as e:
            log.info("escalation skipped, budget: %s", e)
            return routed
        except AgentFailed as e:
            # The attempt was billed (see Budget.fail); report it in the cost, keep the primary result.
            failed_cost = self._last_failed_cost()
            log.warning("escalation failed, keeping primary result: %s", e)
            return Routed(**{**routed.__dict__, "cost_usd": routed.cost_usd + failed_cost})

        return Routed(
            result=outcome2.result,
            model=self.escalation_model,
            primary_model=self.primary_model,
            escalated=True,
            cost_usd=entry.cost_usd + entry2.cost_usd,
            latency_ms=entry.latency_ms + entry2.latency_ms,
            request_id=entry.request_id,
        )

    def _last_failed_cost(self) -> float:
        rows = [e for e in self.budget.entries() if e.status == "failed" and e.tier == "escalation"]
        return rows[-1].cost_usd if rows else 0.0
