---
status: todo
found: 2026-11-16, workflow comparison with another team's agentic setup
week: 3
---

# The PR reviewer is the same model family as the implementer

`pr-reviewer` runs on Sonnet; the code is written by Opus. Same vendor, same training data: likely the same blind
spots. Add a second reviewer from another vendor (e.g. OpenAI via its CLI or API) for the risky PRs, and record in
`agent-log.md` which reviewer found what — that comparison is itself interview material.
Blocked on the LLM provider / API key decision.
