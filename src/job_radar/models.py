"""Database schema. Migrations are generated from this metadata (alembic revision --autogenerate)."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SOURCES = ("ashby", "greenhouse", "lever", "workable", "himalayas")
EVENT_KINDS = ("opened", "changed", "closed", "reopened")


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
