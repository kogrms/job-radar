"""Evaluate the `extract` step on the owner's 76 private vacancy texts or on the synthetic injection fixtures.

Usage:
  uv run python scripts/eval_extract.py --max-cost 0.50                 # 5-item slice first (default)
  uv run python scripts/eval_extract.py --max-cost 1.00 --all            # all 76
  uv run python scripts/eval_extract.py --max-cost 0.20 --injection      # evals/extract_injection.jsonl
  options: --model gpt-6.1-sol, --push-dataset (Langfuse dataset "extract-76", idempotent by item id)

Reads $PRIVATE_DIR/jd/*.json and $PRIVATE_DIR/eligibility_labels.csv (never copied into the repo). Prints
only aggregate numbers; the per-item report (with texts' quotes and disagreements) goes to
$PRIVATE_DIR/evals/. Every call is in the `llm_calls` ledger as purpose "eval:extract" and counts toward the
pipeline's daily cap.

Cost control: prints a typical and a worst-case estimate first and refuses to start when the typical one is
over --max-cost; stops before any item whose worst case would cross --max-cost.

Location-quote scoring: a labeled quote is scored only when it occurs in the stored text (normalized
whitespace/case). Most "verbatim" labels were copied from the tracker's location field, which the stored text
does not contain and which is not given to the model (it would hand it the answer); those are counted as
"not scorable".
"""

import argparse
import asyncio
import csv
import json
import re
import statistics
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from job_radar.config import Settings, get_settings
from job_radar.db import make_engine, make_sessionmaker
from job_radar.extract.pii import mask_pii
from job_radar.extract.prompt import INSTRUCTIONS, PROMPT_SHA, PROMPT_VERSION, render_posting
from job_radar.extract.schema import VacancyFields
from job_radar.llm.client import LLM
from job_radar.llm.ledger import CallTags
from job_radar.llm.pricing import estimate_tokens, estimate_usd
from job_radar.llm.provider import LLMPermanentError, LLMTransientError
from job_radar.steps.chain import make_llm
from job_radar.steps.extract_step import quote_check

REPO = Path(__file__).resolve().parent.parent
TYPICAL_OUTPUT_TOKENS = 1200


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\u00a0", " ")).strip().strip('"').strip().casefold()


def compare_quote(extracted: str | None, labeled: str) -> str:
    if extracted is None:
        return "missed"
    a, b = norm(extracted), norm(labeled)
    if a == b:
        return "exact"
    if a in b or b in a:
        return "overlapping"
    wa, wb = set(re.findall(r"\w+", a)), set(re.findall(r"\w+", b))
    return "overlapping" if wb and len(wa & wb) / len(wb) >= 0.5 else "missed"


def load_items(private: Path) -> list[dict[str, Any]]:
    labels: dict[int, list[dict[str, str]]] = {}
    with (private / "eligibility_labels.csv").open(newline="") as f:
        for row in csv.DictReader(f):
            if row["source"].startswith("app:"):
                labels.setdefault(int(row["source"].split(":")[1]), []).append(row)
    items = []
    for path in sorted((private / "jd").glob("*.json")):
        jd = json.loads(path.read_text())
        text = mask_pii(jd["text"])
        quotes = [r["text"] for r in labels.get(jd["row"], []) if r["verbatim"] == "yes"]
        items.append(
            {
                "id": f"jd-{jd['row']:03d}",
                "title": jd["role"],
                "location_raw": "",
                "text": text,
                "labeled_quotes": quotes,
                "scorable_quotes": [q for q in quotes if norm(q) in norm(text)],
                "label_wordings": [r["text"] for r in labels.get(jd["row"], [])],
            }
        )
    return items


def load_fixtures() -> list[dict[str, Any]]:
    lines = (REPO / "evals/extract_injection.jsonl").read_text().splitlines()
    return [{**json.loads(line), "labeled_quotes": [], "scorable_quotes": []} for line in lines]


def estimates(items: list[dict[str, Any]], s: Settings, model: str) -> tuple[float, float]:
    typical = worst = 0.0
    for it in items:
        tokens = estimate_tokens(INSTRUCTIONS + render_posting(**_posting(it)))
        typical += estimate_usd(model, input_tokens=tokens, output_tokens=TYPICAL_OUTPUT_TOKENS)
        worst += 2 * estimate_usd(
            model,
            input_tokens=tokens + s.extract_max_output_tokens,
            output_tokens=s.extract_max_output_tokens,
        )
    return typical, worst


def _posting(it: dict[str, Any]) -> dict[str, str]:
    return {"title": it["title"], "location_raw": it["location_raw"], "text": it["text"]}


def violated(fx: dict[str, Any], f: dict[str, Any]) -> list[str]:
    out = []
    for rule in fx.get("forbid", []):
        value = f.get(rule["field"])
        if "equals" in rule and value == rule["equals"]:
            out.append(f"{rule['field']} == {rule['equals']!r}")
        if (
            "contains" in rule
            and value is not None
            and rule["contains"] in (value if isinstance(value, list) else str(value))
        ):
            out.append(f"{rule['field']} contains {rule['contains']!r}")
    return out


async def run(args: argparse.Namespace) -> int:
    s = get_settings()
    model = args.model or s.extract_model
    if args.injection:
        items = load_fixtures()
    else:
        if s.private_dir is None:
            print("PRIVATE_DIR is not set", file=sys.stderr)
            return 1
        items = load_items(s.private_dir)
        if not args.all:
            items = items[:5]
    typical, worst = estimates(items, s, model)
    print(
        f"{len(items)} items on {model}: typical ~${typical:.3f}, worst case ${worst:.3f},"
        f" max ${args.max_cost:.2f}"
    )
    if typical > args.max_cost:
        print("typical estimate is over --max-cost; not starting", file=sys.stderr)
        return 1
    if args.dry_run:
        return 0

    engine = make_engine(s.database_url)
    llm: LLM | None = make_llm(s, make_sessionmaker(engine))
    if llm is None:
        print("OPENAI_API_KEY is not set", file=sys.stderr)
        await engine.dispose()
        return 1
    spent = 0.0
    results: list[dict[str, Any]] = []
    tags = CallTags(purpose="eval:extract")
    try:
        for it in items:
            item_worst = 2 * estimate_usd(
                model,
                input_tokens=estimate_tokens(INSTRUCTIONS + render_posting(**_posting(it)))
                + s.extract_max_output_tokens,
                output_tokens=s.extract_max_output_tokens,
            )
            if spent + item_worst > args.max_cost:
                print(f"stopping: the next item could cross --max-cost (spent ${spent:.4f})")
                break
            started = time.monotonic()
            rec: dict[str, Any] = {"id": it["id"]}
            try:
                out = await llm.structured(
                    tags=tags,
                    model=model,
                    prompt_version=PROMPT_VERSION,
                    instructions=INSTRUCTIONS,
                    user_input=render_posting(**_posting(it)),
                    schema=VacancyFields,
                    check=quote_check(it),
                    reasoning_effort=s.extract_reasoning_effort,
                    max_output_tokens=s.extract_max_output_tokens,
                )
            except (LLMTransientError, LLMPermanentError) as exc:
                rec.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:300], cost=exc.cost_usd or 0.0)
            else:
                rec.update(
                    ok=True, fields=out.value.model_dump(mode="json"), calls=out.calls, cost=out.cost_usd
                )
            rec["latency_ms"] = int((time.monotonic() - started) * 1000)
            spent += rec["cost"]
            if rec["ok"]:
                q = rec["fields"]["location_quote"]
                rec["quote_vs_label"] = [compare_quote(q, lq) for lq in it["scorable_quotes"]]
                if args.injection:
                    rec["violations"] = violated(it, rec["fields"])
            results.append(rec)
            print(
                f"  {it['id'] if args.injection else '#' + str(len(results))}:"
                f" {'ok' if rec['ok'] else 'FAILED'}"
            )
    finally:
        await llm.close()
        await engine.dispose()

    summarize(results, items, model, spent, injection=args.injection)
    if not args.injection and s.private_dir is not None:
        out_dir = s.private_dir / "evals"
        out_dir.mkdir(exist_ok=True)
        path = out_dir / f"extract-{model}-{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
        report = {
            "model": model,
            "prompt": f"{PROMPT_VERSION}:{PROMPT_SHA}",
            "reasoning_effort": s.extract_reasoning_effort,
            "items": [
                {**r, "label_wordings": it["label_wordings"]} for r, it in zip(results, items, strict=False)
            ],
        }
        path.write_text(json.dumps(report, ensure_ascii=False, indent=1))
        print(f"per-item report: {path}")
    if args.push_dataset and not args.injection:
        push_dataset(s, items)
    return 0


def summarize(
    results: list[dict[str, Any]], items: list[dict[str, Any]], model: str, spent: float, *, injection: bool
) -> None:
    ok = [r for r in results if r["ok"]]
    print(f"\nmodel {model}, prompt {PROMPT_VERSION}:{PROMPT_SHA}, {len(results)} of {len(items)} items run")
    print(
        f"valid output: {len(ok)}/{len(results)}; needed the retry: {sum(r.get('calls', 1) > 1 for r in ok)}"
    )
    lat = sorted(r["latency_ms"] for r in results)
    if lat:
        p95 = lat[min(len(lat) - 1, int(0.95 * len(lat)))]
        print(f"latency p50 {statistics.median(lat):.0f} ms, p95 {p95} ms")
    print(f"cost ${spent:.4f} total, ${spent / max(len(results), 1):.5f} per item")
    if injection:
        broken = [r["id"] for r in results if not r["ok"] or r.get("violations")]
        print(
            f"injection fixtures held: {len(results) - len(broken)}/{len(results)};"
            f" broken: {broken or 'none'}"
        )
        return
    quotes = Counter(v for r in ok for v in r.get("quote_vs_label", []))
    labeled = sum(len(it["labeled_quotes"]) for it in items[: len(results)])
    scorable = sum(len(it["scorable_quotes"]) for it in items[: len(results)])
    print(
        f"location quote vs labeled ({scorable} scorable of {labeled} verbatim labels): "
        f"exact {quotes['exact']}, overlapping {quotes['overlapping']}, missed {quotes['missed']}"
    )
    print("workplace type:", dict(Counter(r["fields"]["workplace_type"] for r in ok)))


def push_dataset(s: Settings, items: list[dict[str, Any]]) -> None:
    from langfuse import Langfuse

    if not (s.langfuse_public_key and s.langfuse_secret_key):
        print("Langfuse keys are not set; dataset not pushed", file=sys.stderr)
        return
    lf = Langfuse(
        public_key=s.langfuse_public_key,
        secret_key=s.langfuse_secret_key.get_secret_value(),
        base_url=s.langfuse_base_url,
    )
    name = "extract-76"
    try:
        lf.create_dataset(name=name, description="76 private vacancy texts with location labels")
    except Exception as exc:  # re-runs hit "already exists"; items below are idempotent by id
        print(f"create_dataset: {type(exc).__name__} (continuing)", file=sys.stderr)
    for it in items:
        lf.create_dataset_item(
            dataset_name=name,
            id=it["id"],
            input=_posting(it),
            expected_output={"scorable_quotes": it["scorable_quotes"]},
        )
    lf.flush()
    print(f"pushed {len(items)} items to Langfuse dataset {name!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-cost", type=float, required=True, help="USD; hard stop for this run")
    ap.add_argument("--all", action="store_true", help="all 76 items (default: the first 5)")
    ap.add_argument("--injection", action="store_true", help="run the synthetic injection fixtures instead")
    ap.add_argument("--model", default=None, help="default: EXTRACT_MODEL from config")
    ap.add_argument("--push-dataset", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="print the estimate and stop")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
