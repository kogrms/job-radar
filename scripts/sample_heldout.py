"""Sample a held-out set of live location wordings for an honest eligibility evaluation.

Usage: uv run python scripts/sample_heldout.py [--n 60] [--himalayas 15] [--seed 20261112]
Writes $PRIVATE_DIR/eligibility_heldout.csv (never into the repo) and refuses to overwrite it: it will hold
hand labels. Rows are distinct `location_raw` strings of open vacancies, stratified by source: a fixed number
from Himalayas, the rest from ATS boards in proportion to each source's count of distinct wordings. Wordings
already in eligibility_labels.csv are skipped. `n_postings` lets the evaluation weight a wording by how many
open postings carry it. The sha256 of rules.py at sampling time goes in a sidecar file: the rules must not be
edited between sampling and evaluation, or the number stops being held-out.
"""

import argparse
import asyncio
import csv
import hashlib
import random
import sys
from collections import defaultdict
from pathlib import Path

from sqlalchemy import func, select

from job_radar.config import get_settings
from job_radar.db import make_engine, make_sessionmaker
from job_radar.eligibility import rules
from job_radar.models import Board, Vacancy


def _norm(s: str) -> str:
    return " ".join(s.split()).strip('"«»“” ').lower()


async def _wordings(database_url: str) -> dict[str, dict[str, int]]:
    engine = make_engine(database_url)
    try:
        async with make_sessionmaker(engine)() as session:
            rows = await session.execute(
                select(Board.source, Vacancy.location_raw, func.count())
                .join(Board, Board.id == Vacancy.board_id)
                .where(Vacancy.closed_at.is_(None), Vacancy.location_raw.is_not(None))
                .group_by(Board.source, Vacancy.location_raw)
            )
            out: dict[str, dict[str, int]] = defaultdict(dict)
            for source, text, n in rows:
                if text.strip():
                    out[source][text] = n
            return out
    finally:
        await engine.dispose()


def _allocate(sizes: dict[str, int], total: int) -> dict[str, int]:
    """Largest-remainder split of `total` across sources in proportion to `sizes`."""
    whole = sum(sizes.values())
    exact = {s: total * n / whole for s, n in sizes.items()}
    alloc = {s: int(x) for s, x in exact.items()}
    for s in sorted(exact, key=lambda s: exact[s] - alloc[s], reverse=True)[: total - sum(alloc.values())]:
        alloc[s] += 1
    return alloc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--himalayas", type=int, default=15)
    ap.add_argument("--seed", type=int, default=20261112)
    args = ap.parse_args()
    settings = get_settings()
    if settings.private_dir is None:
        print("PRIVATE_DIR is not set", file=sys.stderr)
        return 1
    out = settings.private_dir / "eligibility_heldout.csv"
    if out.exists():
        print(f"{out} exists, not overwriting (it holds hand labels)", file=sys.stderr)
        return 1

    seen = {
        _norm(r["text"]) for r in csv.DictReader((settings.private_dir / "eligibility_labels.csv").open())
    }
    by_source = asyncio.run(_wordings(settings.database_url))
    pools = {s: sorted(t for t in texts if _norm(t) not in seen) for s, texts in by_source.items()}

    plan = {"himalayas": min(args.himalayas, len(pools.get("himalayas", [])))}
    ats = {s: len(p) for s, p in pools.items() if s != "himalayas" and p}
    plan |= _allocate(ats, args.n - plan["himalayas"])

    rng = random.Random(args.seed)
    picked = [(s, t) for s, k in sorted(plan.items()) for t in rng.sample(pools[s], k)]
    rng.shuffle(picked)

    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id", "source", "n_postings", "text", "label", "note"])
        w.writeheader()
        for i, (s, t) in enumerate(picked, 1):
            w.writerow(
                {"id": i, "source": s, "n_postings": by_source[s][t], "text": t, "label": "", "note": ""}
            )
    digest = hashlib.sha256(Path(rules.__file__).read_bytes()).hexdigest()
    out.with_suffix(".meta").write_text(f"seed={args.seed}\nplan={plan}\nrules_sha256={digest}\n")
    print(len(picked), "rows ->", out)
    print("plan:", plan, " pool sizes:", {s: len(p) for s, p in pools.items()})
    print("rules.py sha256:", digest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
