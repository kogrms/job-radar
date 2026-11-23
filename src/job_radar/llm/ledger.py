"""The `llm_calls` ledger: a row before each request (estimated cost), updated after it (real cost).

Each write is its own transaction, independent of the step's fenced commit: spend must be recorded even
when the step's result is discarded (lost lease, superseded input) or the worker dies mid-call.
"""

from dataclasses import dataclass

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from job_radar.llm.provider import Usage
from job_radar.models import LLMCall
from job_radar.steps.base import TransientStepError


class LedgerError(TransientStepError):
    """The ledger could not be written (DB blip). Transient: the run is retried rather than failed, and a
    billed call whose final write failed keeps its estimate in the ledger (over-counted, never lost)."""


@dataclass(frozen=True)
class CallTags:
    """Who spends: the step (or "eval:<step>"), its run and vacancy if any, primary or shadow."""

    purpose: str
    role: str = "primary"
    step_run_id: int | None = None
    vacancy_id: int | None = None


class Ledger:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker

    async def start(
        self, tags: CallTags, *, attempt: int, model: str, prompt_version: str | None, estimate_usd: float
    ) -> int:
        try:
            return await self._start(tags, attempt, model, prompt_version, estimate_usd)
        except (SQLAlchemyError, OSError) as exc:
            raise LedgerError(f"llm_calls insert failed: {type(exc).__name__}") from exc

    async def _start(
        self, tags: CallTags, attempt: int, model: str, prompt_version: str | None, estimate_usd: float
    ) -> int:
        async with self.sessionmaker() as session, session.begin():
            row = LLMCall(
                purpose=tags.purpose,
                role=tags.role,
                step_run_id=tags.step_run_id,
                vacancy_id=tags.vacancy_id,
                attempt=attempt,
                model=model,
                prompt_version=prompt_version,
                outcome="started",
                cost_usd=estimate_usd,
            )
            session.add(row)
            await session.flush()
            return row.id

    async def finish(
        self,
        call_id: int,
        *,
        outcome: str,
        cost_usd: float,
        usage: Usage | None = None,
        latency_ms: int | None = None,
        response_model: str | None = None,
        request_id: str | None = None,
        error_message: str | None = None,
    ) -> None:
        try:
            await self._finish(
                call_id, outcome, cost_usd, usage, latency_ms, response_model, request_id, error_message
            )
        except (SQLAlchemyError, OSError) as exc:
            raise LedgerError(f"llm_calls update failed: {type(exc).__name__}") from exc

    async def _finish(
        self,
        call_id: int,
        outcome: str,
        cost_usd: float,
        usage: Usage | None,
        latency_ms: int | None,
        response_model: str | None,
        request_id: str | None,
        error_message: str | None,
    ) -> None:
        async with self.sessionmaker() as session, session.begin():
            await session.execute(
                update(LLMCall)
                .where(LLMCall.id == call_id)
                .values(
                    outcome=outcome,
                    cost_usd=cost_usd,
                    input_tokens=usage.input_tokens if usage else None,
                    cached_input_tokens=usage.cached_input_tokens if usage else None,
                    output_tokens=usage.output_tokens if usage else None,
                    reasoning_tokens=usage.reasoning_tokens if usage else None,
                    latency_ms=latency_ms,
                    response_model=response_model,
                    request_id=request_id,
                    error_message=error_message[:2000] if error_message else None,
                    finished_at=func.now(),
                )
            )

    async def spent_today(self) -> float:
        async with self.sessionmaker() as session:
            value = await session.scalar(
                select(func.coalesce(func.sum(LLMCall.cost_usd), 0)).where(
                    LLMCall.created_at
                    >= text("date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'")
                )
            )
        return float(value or 0)
