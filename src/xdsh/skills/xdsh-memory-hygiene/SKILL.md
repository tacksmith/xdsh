---
name: xdsh-memory-hygiene
description: Keep durable USER, MEMORY, and session context useful through bounded redaction, provenance, deduplication, and explicit user authority.
---

# Memory hygiene

1. Separate stable user preferences from task or project facts; do not mix
   either layer with system policy, credentials, raw transcripts, or secrets.
2. Save only a short, reusable fact with provenance and a clear confidence
   boundary. Redact tokens, private keys, personal data, and hidden reasoning
   before any durable write or report.
3. Prefer bounded exact/semantic deduplication and replace stale facts rather
   than growing an unbounded log. Keep search-on-demand history outside the
   per-turn prompt budget.
4. Treat learned content as user-owned data: show the proposed change,
   preserve before-state, and require explicit approval before mutation. A
   successful task is evidence for a candidate, not authorization to remember.
5. When context is compacted or a session branches, carry only verified
   provenance and the active goal; state what was omitted instead of guessing.
