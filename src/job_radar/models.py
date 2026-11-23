"""Database schema. Migrations are generated from this metadata (alembic revision --autogenerate)."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SOURCES = ("ashby", "greenhouse", "lever", "workable", "himalayas")
EVENT_KINDS = ("opened", "changed", "closed", "reopened")
STEP_STATUSES = ("pending", "running", "done", "failed", "waiting_human", "skipped")
ERROR_CLASSES = ("transient", "permanent", "exhausted")
SKIP_REASONS = ("closed", "superseded", "baseline", "budget")
LLM_CALL_OUTCOMES = (
    "started",  # written before the request: a worker killed mid-call leaves its estimate counted as spend
    "ok",
    "invalid",  # answered, but the output failed the schema or a step check (billed all the same)
    "refusal",
    "transient_error",
    "permanent_error",
    "out_of_credits",  # the provider says the prepaid balance is empty: the budget gate pauses LLM steps
)
LLM_CALL_ROLES = ("primary", "shadow")


class Base(DeclarativeBase):
    pass


class Board(Base):
    """One job board on one ATS. Its row lock serializes concurrent fetches of the same board."""

    __tablename__ = "boards"
    __table_args__ = (
        UniqueConstraint("source", "org"),
        CheckConstraint(f"source IN {SOURCES!r}", name="source_known"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    org: Mapped[str] = mapped_column(String(200))
    # Fetches older than this are stale (e.g. a retried job finishing late) and are ignored.
    last_fetch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # True after a complete fetch returned zero postings. Closing the whole board needs two such fetches in a
    # row (ATS incidents produce one-off empty listings; decisions.md 2026-11-16).
    last_complete_empty: Mapped[bool] = mapped_column(default=False, server_default="false")


class Vacancy(Base):
    """A posting identified by its canonical id: (board = source + org, source_job_id)."""

    __tablename__ = "vacancies"
    __table_args__ = (
        UniqueConstraint("board_id", "source_job_id"),
        Index("ix_vacancies_dedup_key", "dedup_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    board_id: Mapped[int] = mapped_column(ForeignKey("boards.id"))
    source_job_id: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    company: Mapped[str] = mapped_column(Text)
    location_raw: Mapped[str] = mapped_column(Text)
    raw_text: Mapped[str] = mapped_column(Text)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    dedup_key: Mapped[str] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StepRun(Base):
    """One attempt-set of one pipeline step on one input of one vacancy (docs/specs/week-2.md, PR 6).

    The unique key (vacancy_id, step, input_hash) makes creation idempotent: the reconcile inserts missing
    rows with ON CONFLICT DO NOTHING. CHECKs enforce "an error is not a result". `claim_token` fences
    commits: a worker that lost its lease writes nothing. On `pending` rows `lease_until` doubles as
    retry-backoff-until.
    """

    __tablename__ = "step_runs"
    __table_args__ = (
        UniqueConstraint("vacancy_id", "step", "input_hash"),
        CheckConstraint(f"status IN {STEP_STATUSES!r}", name="status_known"),
        CheckConstraint(f"error_class IS NULL OR error_class IN {ERROR_CLASSES!r}", name="error_class_known"),
        CheckConstraint(f"skip_reason IS NULL OR skip_reason IN {SKIP_REASONS!r}", name="skip_reason_known"),
        CheckConstraint(
            "status != 'done' OR (result IS NOT NULL AND error_class IS NULL AND error_message IS NULL)",
            name="done_has_result_no_error",
        ),
        CheckConstraint(
            "status != 'failed' OR (result IS NULL AND error_class IS NOT NULL)",
            name="failed_has_error_no_result",
        ),
        CheckConstraint("status != 'skipped' OR skip_reason IS NOT NULL", name="skipped_has_reason"),
        # The claim query scans only claimable rows; done/failed/skipped stay out of the index.
        Index(
            "ix_step_runs_claim",
            "status",
            "lease_until",
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
        Index("ix_step_runs_vacancy_step", "vacancy_id", "step"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    vacancy_id: Mapped[int] = mapped_column(ForeignKey("vacancies.id"))
    step: Mapped[str] = mapped_column(String(32))
    input_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_class: Mapped[str | None] = mapped_column(String(16))
    error_message: Mapped[str | None] = mapped_column(Text)
    skip_reason: Mapped[str | None] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    claim_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 6, asdecimal=False))
    latency_ms: Mapped[int | None] = mapped_column()
    model: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class VacancyEvent(Base):
    __tablename__ = "vacancy_events"
    __table_args__ = (
        CheckConstraint(f"kind IN {EVENT_KINDS!r}", name="kind_known"),
        Index("ix_vacancy_events_vacancy_at", "vacancy_id", "at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    vacancy_id: Mapped[int] = mapped_column(ForeignKey("vacancies.id"))
    kind: Mapped[str] = mapped_column(String(16))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(String(64))


class LLMCall(Base):
    """Ledger of every LLM request (docs/specs/week-2.md, PR 7) and the source of truth for spend.

    A row is inserted (own transaction) before the request with the estimated cost and updated with the real
    usage after it, so spend survives fenced-off results, failed runs and a worker killed mid-call. The daily
    budget gate sums `cost_usd` over today's rows. Eval scripts write here too (`step_run_id` NULL).
    """

    __tablename__ = "llm_calls"
    __table_args__ = (
        CheckConstraint(f"outcome IN {LLM_CALL_OUTCOMES!r}", name="outcome_known"),
        CheckConstraint(f"role IN {LLM_CALL_ROLES!r}", name="role_known"),
        CheckConstraint("cost_usd >= 0", name="cost_non_negative"),
        Index("ix_llm_calls_created_at", "created_at"),
        Index("ix_llm_calls_step_run", "step_run_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purpose: Mapped[str] = mapped_column(String(64))  # step name, or "eval:<step>" for eval scripts
    step_run_id: Mapped[int | None] = mapped_column(ForeignKey("step_runs.id"))
    vacancy_id: Mapped[int | None] = mapped_column(ForeignKey("vacancies.id"))
    role: Mapped[str] = mapped_column(String(16))
    attempt: Mapped[int] = mapped_column()  # 1, or 2 for the retry with the validation error attached
    model: Mapped[str] = mapped_column(String(64))  # as requested (prices are keyed by it)
    response_model: Mapped[str | None] = mapped_column(String(64))  # as reported (may be a dated snapshot)
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(24))
    input_tokens: Mapped[int | None] = mapped_column()
    cached_input_tokens: Mapped[int | None] = mapped_column()
    output_tokens: Mapped[int | None] = mapped_column()  # includes reasoning tokens
    reasoning_tokens: Mapped[int | None] = mapped_column()
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6, asdecimal=False))
    latency_ms: Mapped[int | None] = mapped_column()
    request_id: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
