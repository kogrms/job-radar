"""Evaluate an eligibility classifier on the owner's hand-labeled location wordings.

Usage: uv run python scripts/eval_eligibility.py [--errors]
Reads $PRIVATE_DIR/eligibility_labels.csv (never copied into the repo). Prints a confusion matrix and the two
costly error types: "yes" predicted for a "no" (a wasted application) and "no" predicted for a "yes" (a
fitting job silently dropped — the worst one, nobody ever sees it). "unclear" costs human review time.
"""

import argparse
import csv
import sys
from collections import Counter

from job_radar.config import get_settings
from job_radar.eligibility.rules import classify

LABELS = ("yes", "no", "unclear")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--errors", action="store_true", help="print every disagreement (private data, local only)"
    )
    args = ap.parse_args()
    private = get_settings().private_dir
    if private is None:
        print("PRIVATE_DIR is not set", file=sys.stderr)
        return 1
    rows = list(csv.DictReader((private / "eligibility_labels.csv").open(encoding="utf-8")))
    rows = [r for r in rows if r["label"] in LABELS]
    report(rows, "all rows", args.errors)
    # Paraphrased rows carry the owner's own notes ("not stated whether Serbia is ok"); real postings do not.
    report([r for r in rows if r["verbatim"] == "yes"], "verbatim quotes only", False)
    return 0


def report(rows: list[dict[str, str]], title: str, show_errors: bool) -> None:
    print(f"===== {title} =====")
    cm: Counter[tuple[str, str]] = Counter()
    rules: Counter[str] = Counter()
    errors = []
    for r in rows:
        v = classify(r["text"])
        cm[(r["label"], v.label)] += 1
        rules[v.rule] += 1
        if v.label != r["label"]:
            errors.append((r["id"], r["label"], v.label, v.rule, v.evidence, r["text"]))

    n = len(rows)
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
    print("\nrules fired:", dict(rules.most_common()))
    if show_errors:
        print("\nid | gold | pred | rule | evidence | text")
        for e in errors:
            print(" | ".join(str(x)[:90] for x in e))
    print()


if __name__ == "__main__":
    sys.exit(main())
