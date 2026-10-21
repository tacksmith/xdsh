---
name: session-workspace-cleanup
description: Safely clean explicitly requested session workspace temporary artifacts while preserving sessions, memory, skills, user files, audit evidence, and active work.
---

# Session workspace cleanup

Use only when the user explicitly asks to clean temporary workspace artifacts.
This Skill does not replace `/session clear` or `/session clear-all`: those
commands manage XDSH session state and do not delete workspace files.

1. Resolve the exact workspace owned by the current chat and refuse paths
   outside its approved boundary. Check for active foreground, queued,
   background, scheduled, or delivery work before touching files.
2. List entries first and classify each as persistent, temporary, or unknown.
   Preserve `.xdsh` skills, memory, configuration, session logs, checkpoints,
   audit records, user-created files, and anything referenced by an active job.
3. Treat generated logs, downloads, build outputs, probes, and scratch files as
   candidates only when their owner and temporary purpose are clear. A name,
   extension, age, or glob alone is insufficient.
4. Show the exact candidate paths and estimated impact, then require an
   explicit confirmation before deletion. Do not delete persistent or unknown
   items through this workflow.
5. Use recoverable, individually scoped removal where available. Re-list the
   workspace and report what was removed, what was retained, and any failure.
   Never follow symlinks, delete an ancestor directory, or use a broad recursive
   glob. Do not expose file contents or credentials in the report.
