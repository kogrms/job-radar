"""The LLM layer (docs/specs/week-2.md, PR 7): one structured-output call path for every LLM step.

- `provider` — the Provider protocol and the error classes the engine understands; `openai_provider` is the
  only real implementation (SDK retries off: the engine's retry layer is the only one).
- `client.LLM` — schema validation, one retry with the validation error attached, the call ledger, traces.
- `ledger` — every request is written to `llm_calls` before it is sent; spend is summed from there.
- `pricing` — USD per million tokens, per model id.
"""
