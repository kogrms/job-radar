"""Which boards to poll.

The list is personal (it reveals where the owner applies), so it lives in the private dir; the repo ships
examples/boards.yml with the format.
"""

import logging
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from job_radar.collectors import canonical_org

log = logging.getLogger(__name__)


class BoardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["ashby", "greenhouse", "lever", "workable", "himalayas"]
    org: str  # ATS board name; for himalayas, the ISO country code the search is filtered by
    company: str = ""  # required for ATS boards; aggregators take the company from each job
    region: Literal["eu"] | None = None  # Lever only: EU sites live on a separate API host
    queries: list[str] | None = None  # himalayas only: search terms, results merged into one listing

    @model_validator(mode="after")
    def _source_specific(self) -> "BoardConfig":
        if self.source == "himalayas":
            if not self.queries or any(not q.strip() for q in self.queries):
                raise ValueError("himalayas needs at least one non-empty query")
            if self.region:
                raise ValueError("region is a Lever setting")
        else:
            if not self.company:
                raise ValueError(f"{self.source} board {self.org!r} needs a company name")
            if self.queries:
                raise ValueError(f"queries are for aggregator search, not for {self.source} boards")
        return self


def load_boards(path: Path) -> list[BoardConfig]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    boards: list[BoardConfig] = []
    seen: set[tuple[str, str]] = set()
    for item in raw.get("boards") or []:
        b = BoardConfig.model_validate(item)
        b = b.model_copy(update={"org": canonical_org(b.source, b.org)})
        if (b.source, b.org) in seen:
            log.warning("board %s/%s listed twice, keeping the first", b.source, b.org)
            continue
        seen.add((b.source, b.org))
        boards.append(b)
    return boards
