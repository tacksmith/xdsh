---
name: xdsh-long-task-control
description: Run multi-step work with an explicit Todo, visible milestones, bounded retries, liveness checks, and a useful partial result when stopped.
---

# Long-task control

For work with more than one meaningful step:

1. Create a short Todo with concrete, verifiable steps and one current step.
2. Mark a step complete immediately after its evidence is available; keep the
   final percentage truthful and set every remaining item complete on success.
3. Surface useful intermediate findings, tool purpose, and blockers without
   dumping raw tool output or hidden reasoning.
4. Treat a retry, a long-silent tool, repeated identical calls, and a growing
   token budget as signals to reassess—not as permission to loop forever.
5. On cancellation, timeout, or a guard stop, preserve the partial answer and
   the exact incomplete Todo state, then state the next safe continuation.

Never bypass the runner-neutral time, token, tool, or cancellation guard.
