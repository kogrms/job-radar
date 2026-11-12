"""Which boards to poll.

The list is personal (it reveals where the owner applies), so it lives in the private dir; the repo ships
examples/boards.yml with the format.
"""

import logging
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

from job_radar.collectors import canonical_org

log = logging.getLogger(__name__)


class BoardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["ashby", "greenhouse", "lever", "workable"]
    org: str
    company: str
    region: Literal["eu"] | None = None  # Lever only: EU sites live on a separate API host


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
