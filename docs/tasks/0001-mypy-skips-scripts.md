---
status: done
found: 2026-11-16, while writing scripts/sample_heldout.py
week: 2
closed_by: w2-eligibility-rules (pyproject `[tool.mypy] files = ["src", "scripts"]`)
---

# `uv run mypy` (and CI) type-checks `src/` only, not `scripts/`

`[tool.mypy] files = ["src"]`, so `scripts/eval_eligibility.py` and `scripts/sample_heldout.py` were never
type-checked by the documented check or by CI. Running `mypy --strict scripts/x.py` alone fails with
`import-untyped` (the package is checked from source only when `src` is in the same run). Fixed by adding
`scripts` to `files`; all scripts pass.
