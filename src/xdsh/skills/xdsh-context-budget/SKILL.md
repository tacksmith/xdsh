---
name: xdsh-context-budget
description: Keep long conversations within the configured model window using structured usage, safe-boundary compaction, and provenance-preserving continuation.
---

# Context budget

- Read structured usage and `/context` before assuming a task is stuck. The
  displayed value may be the latest turn's peak, not the post-compaction size.
- Treat the configured warning and native-compaction thresholds as the
  decision boundary. Do not wait for a provider hard-limit error when a safe
  step boundary is available.
- Let official DSH compaction prune oversized tool results, preserve balanced
  tool-call/result pairs, and summarize the older surface through the selected
  route. Do not reconstruct events from stdout or invent a summary.
- Keep the source session log and provenance intact. A compatibility fallback
  may use only bounded, clearly labelled user-authority history.
- If the context window or provider capability is unknown, report that fact and
  use the runner's bounded fallback rather than silently increasing limits.
