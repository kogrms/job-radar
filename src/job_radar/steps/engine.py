"""The workflow engine: creates step runs from committed state and executes them durably.

Design (docs/specs/week-2.md, PR 6; rationale in docs/decisions.md 2026-11-16):

- **Reconcile, not an event cursor.** Sequence ids become visible in commit order, not id order, so a cursor
  over `vacancy_events.id` can skip events of concurrent board fetches. Instead `reconcile()` computes, from
  committed state, which (vacancy, step, input) rows should exist and inserts the missing ones with
  ON CONFLICT DO NOTHING. Lossless under any commit order, idempotent by the unique key.
- **A cron drain, not one arq job per step.** arq redelivers a killed job only after `job_timeout + 10` s and
  refuses a reused job id for an hour, so arq never decides retries here. `claim()` takes rows with
  FOR UPDATE SKIP LOCKED; a 60 s lease renewed by a heartbeat bounds recovery after `kill -9` to ~2 minutes.
  The drain starts one heartbeat per claimed row up front, so rows waiting their turn in the batch keep their
  leases too (review finding: without this, batch tails expired mid-drain and got double-run).
- **Fenced commits.** Every writing UPDATE carries `claim_token` + `status='running'` in its WHERE: a worker
  whose lease was reclaimed writes nothing. At commit the input is recomputed from current data; a mismatch
  means the content changed mid-run and the row becomes skipped/superseded instead of a result stored under
  the wrong hash.
- **One retry layer.** Transient errors re-queue the row with exponential backoff in `lease_until`; permanent
  errors fail it at once. The hard attempt cap counts lease-expiry reclaims too, so a step that kills its
  worker ends as failed/exhausted instead of looping. Classification is by exception class, never by
  message text.

`failpoint` is a test hook called between transactions; crash tests raise a BaseException from it to simulate
a worker dying at that boundary (nothing here catches BaseException, like a real SIGKILL).
"""

import asyncio
import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy import bindparam, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from job_radar.models import StepRun, Vacancy
from job_radar.steps.base import Step, StepInput, StepOutcome, TransientStepError

log = logging.getLogger(__name__)

CLAIMABLE = (
    "((status = 'pending' AND (lease_until IS NULL OR lease_until <= now())) "
    "OR (status = 'running' AND lease_until <= now()))"
)
# Every write to a claimed row carries this: a worker whose claim was reclaimed writes nothing.
FENCE = "id = :id AND claim_token = :token AND status = 'running'"
# asyncpg allows at most 32767 bind parameters per statement; stay well below it when chunking.
_INSERT_CHUNK = 2000  # 4 parameters per row
_IDS_CHUNK = 10000


@dataclass(frozen=True)
class Claim:
    id: int
    vacancy_id: int
    step: str
    input_hash: str
    claim_token: str
    attempts: int


@dataclass(frozen=True)
class ReconcileResult:
    created: int = 0
    revived: int = 0
    superseded: int = 0
    closed: int = 0


class Engine:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        chain: list[Step],
        *,
        lease_seconds: int = 60,
        batch_size: int = 10,
        attempt_cap: int = 5,
        hard_timeout_seconds: float = 300,
        backoff_base_seconds: float = 30,
        failpoint: Callable[[str], None] | None = None,
    ) -> None:
        if not chain:
            raise ValueError("empty step chain")
        self.sessionmaker = sessionmaker
        self.chain = chain
        self.by_name = {s.name: i for i, s in enumerate(chain)}
        if len(self.by_name) != len(chain):
            raise ValueError("duplicate step names in chain")
        self.lease_seconds = lease_seconds
        self.batch_size = batch_size
        self.attempt_cap = attempt_cap
        self.hard_timeout_seconds = hard_timeout_seconds
        self.backoff_base_seconds = backoff_base_seconds
        self._failpoint = failpoint

    def _fp(self, name: str) -> None:
        if self._failpoint is not None:
            self._failpoint(name)

    # -- creating work ------------------------------------------------------------------------------------

    async def _desired(self, session: AsyncSession, vacancy: Vacancy) -> dict[str, StepInput]:
        """Walk the chain from committed state: each step's desired input, as far as done upstreams allow."""
        desired: dict[str, StepInput] = {}
        upstream: StepRun | None = None
        for step in self.chain:
            inp = step.build_input(vacancy, upstream)
            if inp is None:
                break
            desired[step.name] = inp
            upstream = (
                await session.execute(
                    select(StepRun).where(
                        StepRun.vacancy_id == vacancy.id,
                        StepRun.step == step.name,
                        StepRun.input_hash == inp.hash,
                        StepRun.status == "done",
                    )
                )
            ).scalar_one_or_none()
            if upstream is None:
                break
        return desired

    async def reconcile(self) -> ReconcileResult:
        """One pass: skip work of closed vacancies, create/revive missing rows, supersede stale ones."""
        step_names = list(self.by_name)
        async with self.sessionmaker() as session, session.begin():
            closed = _rowcount(
                await session.execute(
                    text(
                        "UPDATE step_runs sr SET status='skipped', skip_reason='closed', claim_token=NULL,"
                        " lease_until=NULL, finished_at=now(), updated_at=now()"
                        " FROM vacancies v WHERE v.id = sr.vacancy_id AND v.closed_at IS NOT NULL"
                        " AND sr.status IN ('pending', 'waiting_human')"
                    )
                )
            )

            vacancies = (
                (await session.execute(select(Vacancy).where(Vacancy.closed_at.is_(None)))).scalars().all()
            )
            desired_rows: list[dict[str, Any]] = []
            desired_hash: dict[tuple[int, str], str] = {}
            for v in vacancies:
                for name, inp in (await self._desired(session, v)).items():
                    desired_rows.append(
                        {"vacancy_id": v.id, "step": name, "input_hash": inp.hash, "status": "pending"}
                    )
                    desired_hash[(v.id, name)] = inp.hash
            desired_keys = {(vid, name, h) for (vid, name), h in desired_hash.items()}

            created = 0
            for chunk in _chunked(desired_rows, _INSERT_CHUNK):
                # Multi-row INSERTs (not executemany): their rowcount is the number actually created.
                stmt = (
                    insert(StepRun)
                    .values(chunk)
                    .on_conflict_do_nothing(index_elements=["vacancy_id", "step", "input_hash"])
                )
                created += _rowcount(await session.execute(stmt))

            # Reopened with the same content, or content that flapped back: the skipped row is current again.
            # Baseline rows never revive here; `backfill` turns them pending explicitly.
            skipped = (
                await session.execute(
                    select(StepRun.id, StepRun.vacancy_id, StepRun.step, StepRun.input_hash).where(
                        StepRun.status == "skipped",
                        StepRun.skip_reason.in_(["closed", "superseded"]),
                        StepRun.step.in_(step_names),
                    )
                )
            ).all()
            revive_ids = [r.id for r in skipped if (r.vacancy_id, r.step, r.input_hash) in desired_keys]
            revived = 0
            for ids in _chunked(revive_ids, _IDS_CHUNK):
                revived += _rowcount(
                    await session.execute(
                        text(
                            "UPDATE step_runs SET status='pending', skip_reason=NULL, lease_until=NULL,"
                            " finished_at=NULL, updated_at=now() WHERE id IN :ids AND status='skipped'"
                        ).bindparams(bindparam("ids", expanding=True, value=ids))
                    )
                )

            # A pending/waiting_human row is stale when the same (vacancy, step) now wants a DIFFERENT hash.
            # A row whose pair has no desired entry (its upstream is not done for the current content) is
            # left alone: the claim-time and commit-time input checks skip it if it is ever picked up, and a
            # commit-time next-step insert racing this pass is thereby never superseded by mistake.
            stale = (
                await session.execute(
                    select(StepRun.id, StepRun.vacancy_id, StepRun.step, StepRun.input_hash).where(
                        StepRun.status.in_(["pending", "waiting_human"]),
                        StepRun.step.in_(step_names),
                    )
                )
            ).all()
            stale_ids = [
                r.id
                for r in stale
                if (r.vacancy_id, r.step) in desired_hash
                and desired_hash[(r.vacancy_id, r.step)] != r.input_hash
            ]
            superseded = 0
            for ids in _chunked(stale_ids, _IDS_CHUNK):
                superseded += _rowcount(
                    await session.execute(
                        text(
                            "UPDATE step_runs SET status='skipped', skip_reason='superseded',"
                            " claim_token=NULL, lease_until=NULL, finished_at=now(), updated_at=now()"
                            " WHERE id IN :ids AND status IN ('pending', 'waiting_human')"
                        ).bindparams(bindparam("ids", expanding=True, value=ids))
                    )
                )

        return ReconcileResult(created=created, revived=revived, superseded=superseded, closed=closed)

    # -- executing work -----------------------------------------------------------------------------------

    async def claim_batch(self) -> list[Claim]:
        """Fail exhausted rows, then claim up to batch_size runnable ones. Safe to call concurrently.

        MATERIALIZED pins the row-picking subquery to one evaluation; FOR UPDATE SKIP LOCKED makes
        concurrent claimers pass over each other instead of blocking or double-claiming.
        """
        async with self.sessionmaker() as session, session.begin():
            await session.execute(
                text(
                    f"WITH c AS MATERIALIZED (SELECT id FROM step_runs WHERE {CLAIMABLE}"
                    "  AND attempts >= :cap FOR UPDATE SKIP LOCKED)"
                    " UPDATE step_runs sr SET status='failed', error_class='exhausted', result=NULL,"
                    " error_message='attempt cap reached after ' || sr.attempts || ' claims (incl."
                    " lease-expiry reclaims); last error: ' || COALESCE(sr.error_message, 'none'),"
                    " claim_token=NULL, lease_until=NULL, finished_at=now(), updated_at=now()"
                    " FROM c WHERE sr.id = c.id"
                ),
                {"cap": self.attempt_cap},
            )
            rows = (
                await session.execute(
                    text(
                        f"WITH c AS MATERIALIZED (SELECT id FROM step_runs WHERE {CLAIMABLE}"
                        "  AND attempts < :cap ORDER BY id LIMIT :n FOR UPDATE SKIP LOCKED)"
                        " UPDATE step_runs sr SET status='running', claim_token=gen_random_uuid()::text,"
                        " attempts=sr.attempts + 1,"
                        " lease_until=now() + make_interval(secs => :lease),"
                        " started_at=COALESCE(sr.started_at, now()), updated_at=now()"
                        " FROM c WHERE sr.id = c.id"
                        " RETURNING sr.id, sr.vacancy_id, sr.step, sr.input_hash, sr.claim_token, sr.attempts"
                    ),
                    {"cap": self.attempt_cap, "lease": self.lease_seconds, "n": self.batch_size},
                )
            ).all()
        return [Claim(*row) for row in rows]

    async def _fenced(self, claim: Claim, sets: str, params: dict[str, Any]) -> bool:
        """Apply an UPDATE to the claimed row iff the claim still holds. False means the lease was lost."""
        async with self.sessionmaker() as session, session.begin():
            count = _rowcount(
                await session.execute(
                    text(f"UPDATE step_runs SET {sets}, updated_at=now() WHERE {FENCE}"),
                    {**params, "id": claim.id, "token": claim.claim_token},
                )
            )
        return count == 1

    async def _heartbeat(self, claim: Claim, lost: asyncio.Event) -> None:
        interval = max(self.lease_seconds / 3, 1)
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self._fenced(
                    claim,
                    "lease_until = now() + make_interval(secs => :lease)",
                    {"lease": self.lease_seconds},
                )
            except Exception:
                # A DB blip must not kill the heartbeat silently: treat the lease as lost and let the
                # fence discard the result rather than run on unprotected.
                log.exception("heartbeat for step run %s failed; treating the lease as lost", claim.id)
                lost.set()
                return
            if not renewed:
                lost.set()
                return

    async def _current_input(self, claim: Claim, session: AsyncSession) -> StepInput | None:
        vacancy = await session.get(Vacancy, claim.vacancy_id)
        if vacancy is None or vacancy.closed_at is not None:
            return None
        return (await self._desired(session, vacancy)).get(claim.step)

    async def run_claim(self, claim: Claim, lost: asyncio.Event | None = None) -> str:
        """Execute one claimed row to a terminal write (or a backoff re-queue). Returns the outcome kind.

        When `lost` is given, the caller (the drain) already runs a heartbeat for this claim; otherwise one
        is started here.
        """
        step = self.chain[self.by_name[claim.step]]
        self._fp("after_claim")

        async with self.sessionmaker() as session:
            inp = await self._current_input(claim, session)
        if inp is None or inp.hash != claim.input_hash:
            await self._fenced(
                claim,
                "status='skipped', skip_reason='superseded', claim_token=NULL, lease_until=NULL,"
                " finished_at=now()",
                {},
            )
            return "superseded"

        heartbeat: asyncio.Task[None] | None = None
        if lost is None:
            lost = asyncio.Event()
            heartbeat = asyncio.create_task(self._heartbeat(claim, lost))
        started = asyncio.get_running_loop().time()

        def latency_ms() -> int:
            return int((asyncio.get_running_loop().time() - started) * 1000)

        try:
            async with asyncio.timeout(self.hard_timeout_seconds):
                outcome = await step.run(inp.payload)
        except TimeoutError as exc:
            return await self._record_error(claim, TransientStepError(f"hard timeout: {exc}"), latency_ms())
        except Exception as exc:
            return await self._record_error(claim, exc, latency_ms())
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
        if lost.is_set():
            log.warning("step run %s lost its lease mid-run; result discarded", claim.id)
            return "lost"

        self._fp("before_commit")
        return await self._commit(claim, step, outcome, latency_ms())

    async def _commit(self, claim: Claim, step: Step, outcome: StepOutcome, latency_ms: int) -> str:
        """The fenced commit: input check, result write, and the next step's row, in one transaction."""
        idx = self.by_name[step.name]
        async with self.sessionmaker() as session, session.begin():
            inp = await self._current_input(claim, session)
            if inp is None or inp.hash != claim.input_hash:
                # Content changed (or the vacancy closed) while we ran: the result would be stored under the
                # wrong hash. Cost and latency still land on the row.
                count = _rowcount(
                    await session.execute(
                        text(
                            "UPDATE step_runs SET status='skipped', skip_reason='superseded',"
                            " claim_token=NULL, lease_until=NULL, finished_at=now(), updated_at=now(),"
                            f" cost_usd=:cost, latency_ms=:latency WHERE {FENCE}"
                        ),
                        {
                            "cost": outcome.cost_usd,
                            "latency": latency_ms,
                            "id": claim.id,
                            "token": claim.claim_token,
                        },
                    )
                )
                return "superseded" if count else "lost"

            count = _rowcount(
                await session.execute(
                    text(
                        "UPDATE step_runs SET status='done', result=:result, error_class=NULL,"
                        " error_message=NULL, claim_token=NULL, lease_until=NULL, finished_at=now(),"
                        " updated_at=now(), cost_usd=:cost, latency_ms=:latency, model=:model,"
                        f" prompt_version=:pv WHERE {FENCE}"
                    ),
                    {
                        "result": _jsonb(outcome.result),
                        "cost": outcome.cost_usd,
                        "latency": latency_ms,
                        "model": outcome.model,
                        "pv": outcome.prompt_version,
                        "id": claim.id,
                        "token": claim.claim_token,
                    },
                )
            )
            if count == 0:
                return "lost"  # lease reclaimed between the input check and here; nothing written

            # The next step's row is born in the same transaction as the upstream `done`, so a crash cannot
            # leave `done` upstream with no downstream row. The reconcile would recreate it anyway.
            if idx + 1 < len(self.chain):
                vacancy = await session.get(Vacancy, claim.vacancy_id)
                this_run = (await session.execute(select(StepRun).where(StepRun.id == claim.id))).scalar_one()
                nxt = self.chain[idx + 1]
                if vacancy is not None and (n_inp := nxt.build_input(vacancy, this_run)) is not None:
                    await session.execute(
                        insert(StepRun)
                        .values(
                            vacancy_id=claim.vacancy_id,
                            step=nxt.name,
                            input_hash=n_inp.hash,
                            status="pending",
                        )
                        .on_conflict_do_nothing(index_elements=["vacancy_id", "step", "input_hash"])
                    )
        return "done"

    async def _record_error(self, claim: Claim, exc: Exception, latency_ms: int) -> str:
        msg = f"{type(exc).__name__}: {exc}"[:2000]
        if isinstance(exc, TransientStepError):
            backoff = self.backoff_base_seconds * 2 ** (claim.attempts - 1)
            requeued = await self._fenced(
                claim,
                "status='pending', error_class='transient', error_message=:msg, claim_token=NULL,"
                " latency_ms=:latency, lease_until=now() + make_interval(secs => :backoff)",
                {"msg": msg, "backoff": backoff, "latency": latency_ms},
            )
            return "requeued" if requeued else "lost"
        failed = await self._fenced(
            claim,
            "status='failed', error_class='permanent', error_message=:msg, result=NULL, claim_token=NULL,"
            " latency_ms=:latency, lease_until=NULL, finished_at=now()",
            {"msg": msg, "latency": latency_ms},
        )
        return "failed" if failed else "lost"

    async def drain(self) -> dict[str, int]:
        """One drain pass: claim a batch and run it sequentially. Counts by outcome; {} when idle.

        Heartbeats for EVERY claimed row start before the first row runs: rows waiting their turn in the
        batch keep their leases, so an overlapping drain cannot reclaim and double-run them. A row whose own
        heartbeat reports a lost lease is not started at all.
        """
        counts: dict[str, int] = {}
        claims = await self.claim_batch()
        hearts: dict[int, tuple[asyncio.Task[None], asyncio.Event]] = {}
        for claim in claims:
            lost = asyncio.Event()
            hearts[claim.id] = (asyncio.create_task(self._heartbeat(claim, lost)), lost)
        try:
            for claim in claims:
                _, lost = hearts[claim.id]
                if lost.is_set():
                    counts["lost"] = counts.get("lost", 0) + 1
                    continue
                try:
                    kind = await self.run_claim(claim, lost=lost)
                except Exception:
                    # A bug in the engine around one row must not strand the rest of the batch.
                    log.exception("run_claim crashed for step run %s", claim.id)
                    kind = "error"
                counts[kind] = counts.get(kind, 0) + 1
        finally:
            for task, _ in hearts.values():
                task.cancel()
        return counts


def _chunked(items: list[Any], size: int) -> Iterator[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _rowcount(result: Any) -> int:
    return int(result.rowcount)


def _jsonb(value: dict[str, Any]) -> str:
    """asyncpg's jsonb codec (as SQLAlchemy registers it) takes an already-serialized string."""
    return json.dumps(value, ensure_ascii=False)
