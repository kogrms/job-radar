"""Operator queries and commands over `step_runs` (used by the CLI; spec PR 6)."""

from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from job_radar.steps.engine import _rowcount


@dataclass(frozen=True)
class StepsStatus:
    counts: list[tuple[str, str, int]] = field(default_factory=list)  # (step, status, n)
    stuck_running: int = 0  # running with started_at older than 3 leases: investigate
    oldest_waiting_human_s: float | None = None


async def steps_status(session: AsyncSession, *, lease_seconds: int = 60) -> StepsStatus:
    counts = [
        (r.step, r.status, r.n)
        for r in await session.execute(
            text("SELECT step, status, count(*) AS n FROM step_runs GROUP BY step, status ORDER BY 1, 2")
        )
    ]
    stuck = await session.scalar(
        text(
            "SELECT count(*) FROM step_runs WHERE status = 'running'"
            " AND started_at < now() - make_interval(secs => 3 * :lease)"
        ),
        {"lease": lease_seconds},
    )
    oldest = await session.scalar(
        text(
            "SELECT extract(epoch FROM now() - min(updated_at)) FROM step_runs WHERE status = 'waiting_human'"
        )
    )
    return StepsStatus(
        counts=counts,
        stuck_running=stuck or 0,
        oldest_waiting_human_s=float(oldest) if oldest is not None else None,
    )


async def retry_failed(session: AsyncSession, step: str, *, limit: int | None = None) -> int:
    """Put failed runs of one step back to pending with a fresh attempt budget. Caller commits."""
    limit_sql = " LIMIT :limit" if limit is not None else ""
    result = await session.execute(
        text(
            "UPDATE step_runs SET status='pending', attempts=0, lease_until=NULL, finished_at=NULL,"
            " updated_at=now()"
            " WHERE id IN (SELECT id FROM step_runs WHERE status='failed' AND step=:step"
            f" ORDER BY id{limit_sql} FOR UPDATE SKIP LOCKED)"
        ),
        {"step": step, **({"limit": limit} if limit is not None else {})},
    )
    return _rowcount(result)


async def backfill(
    session: AsyncSession,
    step: str,
    *,
    limit: int,
    source: str | None = None,
    dry_run: bool = False,
) -> int:
    """Turn skipped/baseline rows of open vacancies into pending work, oldest vacancies first.

    Baseline rows are the migration's marker for "existed before the pipeline; not processed on purpose"
    (docs/specs/week-2.md, PR 6: backfill is explicit). Caller commits. Returns the number of rows (that
    would be) released; for LLM steps the CLI multiplies this into a cost estimate before a real run.
    """
    source_sql = " AND b.source = :source" if source is not None else ""
    pick = (
        "SELECT sr.id FROM step_runs sr"
        " JOIN vacancies v ON v.id = sr.vacancy_id JOIN boards b ON b.id = v.board_id"
        " WHERE sr.status = 'skipped' AND sr.skip_reason = 'baseline' AND sr.step = :step"
        f" AND v.closed_at IS NULL{source_sql} ORDER BY sr.vacancy_id LIMIT :limit"
    )
    params: dict[str, object] = {"step": step, "limit": limit}
    if source is not None:
        params["source"] = source
    if dry_run:
        n = await session.scalar(text(f"SELECT count(*) FROM ({pick}) p"), params)
        return n or 0
    result = await session.execute(
        text(
            "UPDATE step_runs SET status='pending', skip_reason=NULL, lease_until=NULL, finished_at=NULL,"
            " updated_at=now()"
            f" WHERE id IN ({pick} FOR UPDATE SKIP LOCKED)"
        ),
        params,
    )
    return _rowcount(result)
