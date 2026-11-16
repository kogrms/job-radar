# Task cards

One file per task that is not part of the PR in progress: a bug found on the way, a test gap, a follow-up,
a known limitation waiting for a later week. The agent files a card the moment it finds the problem, so nothing
lives only in chat. This replaces a task board: one PR chain runs at a time, files are enough.

File name: `NNNN-short-slug.md`, numbers never reused. Format:

```markdown
---
status: todo | doing | done | wontfix
found: 2026-11-16          # date, and by whom/what if useful (reviewer, live run, owner)
week: 2                    # plan week it belongs to, or "later"
closed_by: <PR/commit>     # when done or wontfix
---

# Title — one line, the problem, not the fix

What is wrong or missing, how it was found, how to reproduce or check it.
For wontfix: why.
```

No personal data in cards (company names from applications, emails): this repo may become public.
