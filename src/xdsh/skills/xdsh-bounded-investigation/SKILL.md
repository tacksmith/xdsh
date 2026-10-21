---
name: xdsh-bounded-investigation
description: Investigate repositories or workspace evidence with bounded tool use, parallel branches, explicit source tracking, and a concise evidence-backed conclusion.
---

# Bounded investigation

Use this workflow for codebase investigation, debugging, comparison, and information synthesis.

1. State the concrete question, finish criteria, and at most five evidence branches.
2. Inspect authoritative workspace files with structured filesystem/search tools first. Use Bash only when no narrower tool fits.
3. Run independent branches in parallel when useful. Use at most three child agents, give each one bounded question, and incorporate every child result before finishing.
4. Record facts separately from inferences. Cite workspace-relative paths for material claims and explicitly label missing or conflicting evidence.
5. Stop repeating an identical or denied action. After one materially different retry, preserve partial findings and explain the remaining limitation.

Before the final answer, reconcile the Todo list with actual work, report verification performed, and keep the conclusion proportional to the evidence.
