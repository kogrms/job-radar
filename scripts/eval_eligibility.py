"""Evaluate an eligibility classifier on the owner's hand-labeled location wordings.

Usage: uv run python scripts/eval_eligibility.py [--file eligibility_heldout.csv] [--errors]
Reads a labeled CSV from $PRIVATE_DIR (default eligibility_labels.csv; never copied into the repo).
Prints a confusion matrix and the two costly error types: "yes" predicted for a "no" (a wasted application)
and "no" predicted for a "yes" (a fitting job silently dropped — the worst one, nobody ever sees it).
"unclear" costs human review time.

For the held-out set the sidecar `.meta` file holds the sha256 of rules.py at sampling time; the report says
whether the rules evaluated now are the frozen ones. Rows with `n_postings` are also reported weighted by it
(each open posting counts once), since one wording can stand for hundreds of postings.
"""

import argparse
import csv
import hashlib
import sys
from collections import Counter
from pathlib import Path

from job_radar.config import get_settings
from job_radar.eligibility import rules
from job_radar.eligibility.rules import classify

LABELS = ("yes", "no", "unclear")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--errors", action="store_true", help="print every disagreement (private data, local only)"
    )
    ap.add_argument(
        "--file",
        default="eligibility_labels.csv",
        help="labeled CSV: a path, or a file name inside PRIVATE_DIR (e.g. eligibility_heldout.csv)",
    )
    args = ap.parse_args()
    path = Path(args.file)
    if not path.exists():
        private = get_settings().private_dir
        if private is None:
            return fail(f"{path} not found and PRIVATE_DIR is not set")
        path = private / args.file
    if not path.exists():
        return fail(f"{path} not found")

    meta = path.with_suffix(".meta")
    now = hashlib.sha256(Path(rules.__file__).read_bytes()).hexdigest()
    if meta.exists():
        frozen = dict(line.split("=", 1) for line in meta.read_text().splitlines() if "=" in line)
        state = (
            "FROZEN (same as at sampling)" if frozen.get("rules_sha256") == now else "CHANGED since sampling"
        )
        print(f"rules.py {now[:12]}: {state}\n")
    elif "heldout" in path.name:
        print(f"rules.py {now[:12]}: freeze NOT checked — {meta.name} is missing\n")

    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = {"text", "label"} - set(reader.fieldnames or [])
        if missing:
            return fail(f"{path.name}: missing columns {sorted(missing)}")
        all_rows = list(reader)
    rows = []
    for r in all_rows:
        r["label"] = (r["label"] or "").strip().lower()
        if r["label"] in LABELS:
            rows.append(r)
    skipped = Counter(
        "blank" if not r["label"] else repr(r["label"]) for r in all_rows if r["label"] not in LABELS
    )
    if skipped:
        print(
            f"skipped {sum(skipped.values())} of {len(all_rows)} rows without a valid label: "
            f"{dict(skipped)}\n"
        )
    if not rows:
        return fail("no labeled rows")
    if "n_postings" in rows[0]:
        bad = [r.get("id", "?") for r in rows if not (r["n_postings"] or "").strip().isdigit()]
        if bad:
            return fail(f"n_postings is not a non-negative integer in rows {bad[:10]}")

    report(rows, "all rows", args.errors)
    if "verbatim" in rows[0]:
        # Paraphrased rows carry the owner's notes ("not stated whether Serbia is ok"); real postings do not.
        report([r for r in rows if r["verbatim"] == "yes"], "verbatim quotes only", False)
    if "n_postings" in rows[0]:
        report(rows, "weighted by open postings", False, weight="n_postings")
    return 0


def fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def report(rows: list[dict[str, str]], title: str, show_errors: bool, weight: str | None = None) -> None:
    print(f"===== {title} =====")
    cm: Counter[tuple[str, str]] = Counter()
    fired: Counter[str] = Counter()
    errors = []
    for r in rows:
        v = classify(r["text"])
        w = int(r[weight]) if weight else 1
        cm[(r["label"], v.label)] += w
        fired[v.rule] += w
        if v.label != r["label"]:
            errors.append((r.get("id", "?"), r["label"], v.label, v.rule, v.evidence, r["text"]))

    n = sum(cm.values())
    if n == 0:
        print("no rows\n")
        return
    correct = sum(cm[(label, label)] for label in LABELS)
    print(f"n={n}  accuracy={correct / n:.1%}\n")
    print("gold \\ pred " + "".join(f"{p:>9}" for p in LABELS))
    for g in LABELS:
        print(f"{g:12}" + "".join(f"{cm[(g, p)]:>9}" for p in LABELS))
    print()
    for label in ("yes", "no"):
        pred = sum(cm[(g, label)] for g in LABELS)
        gold = sum(cm[(label, p)] for p in LABELS)
        tp = cm[(label, label)]
        print(f"{label:4} precision={tp / pred if pred else 0:.1%}  recall={tp / gold if gold else 0:.1%}")
    print(f"\nyes predicted for a no (wasted application):  {cm[('no', 'yes')]}")
    print(f"no predicted for a yes (fitting job dropped):  {cm[('yes', 'no')]}")
    print(f"sent to a human (pred unclear):                {sum(cm[(g, 'unclear')] for g in LABELS)} of {n}")
    print("\nrules fired:", dict(fired.most_common()))
    if show_errors:
        print("\nid | gold | pred | rule | evidence | text")
        for e in errors:
            print(" | ".join(str(x)[:90] for x in e))
    print()


if __name__ == "__main__":
    sys.exit(main())
