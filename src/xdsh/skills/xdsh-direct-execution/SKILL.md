---
name: xdsh-direct-execution
description: Choose the shortest safe path from the user's goal to a verified result, batching independent work and avoiding speculative detours.
---

# Direct execution

Use this product-level workflow for every non-trivial task:

1. Restate the concrete goal, constraints, and finish condition in one short
   plan.
2. Inspect the smallest authoritative inputs first, then execute the direct
   path that satisfies the goal.
3. Official sources first, then other sources. For search or discovery, verify
   ownership, relevance and freshness; inspect official original documentation
   or data before using other sources for gaps or cross-checks. Official claims
   can be wrong. Batch only within each source stage; form at most four
   non-overlapping queries and prefer one batch-capable call.
   Combine related repository patterns into one
   bounded native search when that preserves meaning.
4. Otherwise, issue at most four independent read-only calls together only
   when the runtime or tool explicitly marks them concurrency-safe. Keep
   writes, edits, installs, state changes, and dependent work serial.
5. Reuse one model turn and one shared evidence budget. Deduplicate results,
   bound output, and never create subagents solely for speed.
6. Keep user-owned data, credentials, and unrelated services outside the task.
7. Verify the result with the narrowest meaningful check and report any
   remaining uncertainty instead of guessing.

This is user-level workflow guidance. The XDSH container, DSH sandbox, runner
guard, and explicit authorization boundaries always take precedence.
