---
name: xdsh-bounded-delegation
description: Delegate genuinely independent work to isolated subagents with shared budgets, explicit receipts, parent verification, and coordinated cancellation.
---

# Bounded delegation

1. Execute directly by default. Delegate only when independent evidence
   branches, a specialist boundary, or a blocking long-running check has a
   clear benefit greater than its coordination cost.
2. Give each child one bounded question, acceptance condition, workspace
   scope, capability ceiling, deadline, and output contract. Do not pass
   credentials, unrelated conversation history, or hidden reasoning.
3. Respect the runner's shared fan-out, token, time, tool, and recursion
   limits. Do not launch duplicate children or use delegation to bypass a
   denied action.
4. Require a compact receipt containing observed facts, source references,
   changes, verification, and unresolved work. The parent verifies material
   claims and remains responsible for the final result.
5. Cancel descendants when the parent is cancelled, the answer is sufficient,
   or a guard stops the run. Preserve useful partial receipts and clearly mark
   branches that did not finish.
