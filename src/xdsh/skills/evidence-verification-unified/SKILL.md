---
name: evidence-verification-unified
description: Verify current facts about models, software releases, policies, people, organizations, and public web pages using authoritative sources, bounded retrieval, explicit uncertainty, and identity checks.
---

# Evidence verification

Use this as the single primary workflow for fact-checking tasks. Load domain
details only when the request needs them; do not combine several overlapping
verification workflows.

1. Fix the claim before searching: exact entity, attribute, jurisdiction or
   product, time, and what would count as evidence. A short factual question
   usually needs one decisive source or a small independent batch.
2. Official sources first, then other sources. Consult the claim's official
   source first: vendor documentation for a
   model or release, npm/PyPI or the project's official release page for a
   package, government/operator rules for policy, and an institution's official
   directory for a person or organization. Search results and saved links are
   discovery leads; verify ownership before treating a source as official.
   Then consult other sources as needed for gaps or cross-checks. Official
   does not mean infallible: label outdated, inaccessible or conflicting evidence.
3. Preserve exact identity. Distinguish model IDs and plan layers, package
   dist-tags from semantic versions, current rules from superseded editions,
   and same-name people or organizations. Missing evidence is not proof that
   an entity or feature does not exist.
4. Fetch only the pages needed for the claim. Batch genuinely independent
   reads within each source stage when the tool advertises it; keep dependent
   discovery serial. Stop
   after sufficient evidence, two materially failed attempts per source, or a
   bounded gap. Do not keep changing query wording after duplicate or irrelevant
   results; switch to a different authoritative index or report the gap.
5. Check that a page contains the requested entity and fields, rather than a
   JavaScript shell, redirect, search snippet, or copied announcement. Respect
   gateway URL, DNS, credential, MIME, size, rate and CAPTCHA controls.
6. Classify every material statement as observed, inferred, or unconfirmed.
   Report source URL, source date when available, access date, exact ID/value,
   and conflicts. A platform listing, subscription inclusion, and account
   access are separate conclusions.
7. For people and medical topics, report public credentials and public review
   data separately. Never infer surgical quality, safety, or a person's
   existence from a failed search, title, or review volume. Suggest an official
   contact path when identity remains unresolved.

Output: conclusion first; then concise evidence, conflicts, unconfirmed items,
and query date. Do not invent dates, amounts, versions, credentials, ratings,
deadlines, or availability. Do not install, upgrade, submit applications, or
change configuration as a side effect of verification.
