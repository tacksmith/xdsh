---
name: xdsh-deep-research
description: Research multi-source or disputed questions with bounded retrieval, claim-matched sources and explicit uncertainty. Simple facts need no broad investigation.
---

# Deep research

1. Turn the request into a bounded question, decision criteria, and a small
   set of independent sub-questions. Do not search merely to fill space.
2. Official sources first, then other sources. Verify ownership and applicability
   before treating a source as official; search engines and saved links are
   discovery leads. Check official documentation, data or announcements first,
   then other sources as needed for gaps or independent cross-checks. Official
   does not mean infallible: label outdated, blocked or conflicting evidence.
   Identify dependencies before fetching: do not guess downstream queries whose
   inputs depend on an upstream answer. Prioritize that prerequisite. For
   independent queries, rank expected evidence value (authority and relevance,
   then freshness, completeness, retrieval success and cost) and dispatch small
   bounded batches within each source stage. Do not create extra agents just for speed.
   Widen discovery only for an open-ended question. For a narrow fact, go
   directly to applicable primary documentation or measurements. Independent
   public pages can share one `web_fetch` call with `urls` and `independent=true`
   when that schema is advertised; a written parallel plan is not parallel I/O.
3. For each material claim, retain its source, date/version, and whether it is
   observed or inferred. Resolve contradictions explicitly; never merge them
   into an unqualified statement.
   For paper reproduction, first map the applicable methods section to actual
   implementation settings. Dataset-specific exceptions take precedence over
   tutorial defaults; an old notebook is not automatically the paper's exact
   code. A blocked publisher page can have an official PMC/Europe PMC full-text
   or XML alternative: verify the paper identifier, use the advertised endpoint,
   and retain gaps rather than guessing URLs or bypassing access controls.
   Preserve independently obtained results before any target-guided parameter
   search. Label target fitting as exploratory, not independent validation.
   Similar aggregates do not establish identical algorithms, data or cell labels;
   differences do not establish misconduct or an unavailable historical file.
   Freeze critical dataset-specific settings in a small method-plan JSON linked
   to source sections. Generate effective-argument metadata from the running
   program; use the same parameter object for analysis and plotting, or reuse
   saved intermediates. Defaults in a second script are not proof of identical
   preprocessing. Compare run/figure metadata against the frozen plan before
   reporting fidelity. If the user prohibits target fitting, do not create an
   optional fitting audit; preserve the discrepancy and investigate its source.
   Numerical agreement does not identify a historical commit, implementation
   or undisclosed seed. A failed compatibility run does not disprove the original
   algorithm until the port, parameter semantics and dependencies are checked.
4. Use parallel research branches only when they answer distinct questions.
   Cap searches, downloads, pages, and excerpts; stop when the acceptance
   criteria are supported or a concrete gap remains.
5. For model/provider/plan questions, record exact model ID, product and date.
   Distinguish platform API support, plan inclusion and this account's permission.
   Consult relevant official announcements, complete plan lists, model docs and
   changelogs; do not mechanically fetch all four if one decisive source suffices.
   Absence from documentation is normally unconfirmed, not unavailable. A
   definitive negative needs explicit current exclusion or exhaustive applicable
   policy. Old pages may remain valid; missing dates require calibrated uncertainty.
   Account probes require authorization and must not expose credentials. Prefer
   the host's `model_availability` check when advertised. If that safe path is
   unavailable or inconclusive, stop at unconfirmed: do not hunt for keys in
   configs, memory, past sessions, or alternative authentication paths. A route's
   successful execution does not establish upstream identity, plan or quota.
6. Verify fetched pages contain relevant body content, not merely a large HTML
   shell. Truncation, missing extraction keys or an empty result do not establish
   absence. Check the exact schema and documented field semantics; do not invent
   meanings for date fields. Do not equate snippets, byte counts, sibling versions
   or duplicated announcements with independent confirmation. Honor typed
   non-retryable failures; use one different authorized source or state the gap.
   Rendering proxies disclose the requested URL/content to a third party: do not
   silently send authenticated/private pages. Never bypass login or CAPTCHA.
7. Stop once the requested claims are supported or explicitly unconfirmed. Keep
   the requested output length; include only the evidence needed for those claims.
   Update any existing Todo at evidence milestones, not just when writing the
   final answer. Ask a short follow-up only if it changes the next action and
   cannot be handled by a stated safe assumption.
   Do not expose credentials, hidden reasoning, or unbounded copied source text.
