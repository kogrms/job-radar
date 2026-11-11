"""Idempotent storage of board fetches.

`apply_fetch` is the only writer of `vacancies`/`vacancy_events`. Guarantees:
- applying the same fetch twice changes nothing the second time (the fetch is stale by then);
- a fetch older than the newest one already applied to that board is ignored as stale;
- only a complete fetch closes postings; a partial or failed fetch never closes anything;
- concurrent fetches of one board are serialized by a row lock on its `boards` row.

Caller contract (collectors, PR 3):
- `seen_at` is the time the fetch STARTED, taken from one clock (the worker's UTC). Two fetches of one board
  with the same `seen_at` count as the same fetch: the second is stale.
- `complete=True` only for a full, successful board listing. An empty complete fetch closes every open posting
  of the board, so an error page or an empty body must be reported as a failed fetch, never as complete.
- Strip NUL characters from text and payload: Postgres rejects them in TEXT and JSONB.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from job_radar.models import Board, Vacancy, VacancyEvent
from job_radar.postings import Posting, content_hash, dedup_key

Outcome = Literal["applied", "stale"]


@dataclass(frozen=True)
class FetchResult:
    outcome: Outcome
    opened: int = 0
    changed: int = 0
    reopened: int = 0
    closed: int = 0
    unchanged: int = 0


async def _lock_board(session: AsyncSession, source: str, org: str) -> Board:
    await session.execute(insert(Board).values(source=source, org=org).on_conflict_do_nothing())
    board = (
        await session.execute(select(Board).where(Board.source == source, Board.org == org).with_for_update())
    ).scalar_one()
    return board


async def apply_fetch(
    session: AsyncSession,
    source: str,
    org: str,
    postings: list[Posting],
    seen_at: datetime,
    *,
    complete: bool,
) -> FetchResult:
    """Apply one board fetch inside the caller's transaction. The caller commits."""
    if seen_at.tzinfo is None:
        raise ValueError("seen_at must be timezone-aware")
    dupes = [jid for jid, n in Counter(p.source_job_id for p in postings).items() if n > 1]
    if dupes:
        raise ValueError(f"duplicate source_job_id in one fetch: {dupes[:5]}")

    board = await _lock_board(session, source, org)
    if board.last_fetch_at is not None and seen_at <= board.last_fetch_at:
        return FetchResult(outcome="stale")
    board.last_fetch_at = seen_at

    existing = {
        v.source_job_id: v
        for v in (
            await session.execute(
                select(Vacancy)
                .where(Vacancy.board_id == board.id)
                .options(defer(Vacancy.raw_text), defer(Vacancy.raw_payload))
            )
        ).scalars()
    }
    counts: Counter[str] = Counter()
    events: list[VacancyEvent] = []

    for p in postings:
        h = content_hash(p)
        v = existing.get(p.source_job_id)
        if v is None:
            v = Vacancy(
                board_id=board.id,
                source_job_id=p.source_job_id,
                url=p.url,
                title=p.title,
                company=p.company,
                location_raw=p.location_raw,
                raw_text=p.raw_text,
                raw_payload=p.raw_payload,
                content_hash=h,
                dedup_key=dedup_key(p.company, p.title),
                first_seen_at=seen_at,
                last_seen_at=seen_at,
            )
            session.add(v)
            # One round trip per new posting to get v.id for its event. Fine for boards of tens to hundreds of
            # postings; batch INSERT ... RETURNING if a board ever brings thousands of new ones at once.
            await session.flush()
            events.append(VacancyEvent(vacancy_id=v.id, kind="opened", at=seen_at, content_hash=h))
            counts["opened"] += 1
            continue

        # A posting can be reopened AND changed in one fetch: both events, so content consumers
        # (extraction runs on opened/changed) never miss new text on a reopened posting.
        kinds: list[str] = []
        if v.closed_at is not None:
            v.closed_at = None
            kinds.append("reopened")
        if v.content_hash != h:
            v.url, v.title, v.company = p.url, p.title, p.company
            v.location_raw, v.raw_text, v.raw_payload = p.location_raw, p.raw_text, p.raw_payload
            v.content_hash, v.dedup_key = h, dedup_key(p.company, p.title)
            kinds.append("changed")
        v.last_seen_at = seen_at
        for kind in kinds:
            events.append(VacancyEvent(vacancy_id=v.id, kind=kind, at=seen_at, content_hash=h))
            counts[kind] += 1
        if not kinds:
            counts["unchanged"] += 1

    if complete:
        present = {p.source_job_id for p in postings}
        gone = [v for jid, v in existing.items() if jid not in present and v.closed_at is None]
        for v in gone:
            v.closed_at = seen_at
            events.append(
                VacancyEvent(vacancy_id=v.id, kind="closed", at=seen_at, content_hash=v.content_hash)
            )
        counts["closed"] = len(gone)

    session.add_all(events)
    await session.flush()
    return FetchResult(
        outcome="applied",
        opened=counts["opened"],
        changed=counts["changed"],
        reopened=counts["reopened"],
        closed=counts["closed"],
        unchanged=counts["unchanged"],
    )
