---
name: xdsh-failure-recovery
description: Recover from task failures with explicit transient, terminal, and unknown states, idempotency checks, bounded retries, and preserved partial work.
---

# Failure recovery

1. Classify the failure from structured events, exit state, timeout, guard
   reason, and durable delivery state. Treat an interrupted or side-effecting
   operation as unknown until checked.
2. Retry only a known-transient failure, at most within the configured budget,
   with a changed condition or backoff. Never repeat an identical denied or
   malformed call just because no answer arrived.
3. Before replay, check ownership, idempotency, workspace state, and the
   current session/generation. A missing confirmation is not permission to
   replay a possibly side-effecting task.
4. Preserve streamed answer text, Todo state, artifacts, usage, and the exact
   blocker in the durable checkpoint. Mark unfinished work honestly.
5. Verify recovery with the narrowest safe probe, then report what recovered,
   what remains unknown, and the explicit operator action required.
