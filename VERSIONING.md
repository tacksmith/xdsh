# XDSH versioning policy

This document is normative for how XDSH version numbers evolve. It supplements
the validated pairing table in `COMPATIBILITY.md` and the publication rules in
`RELEASE-MATRIX.md`.

## Version identity and spellings

XDSH and DeepSeek Harness (DSH) are released as a **tested pair**. The XDSH
product and its DSH runtime share the same major, minor, patch, and
release-candidate line; the Python protocol client is independently versioned
per line.

| Component | Spelling for the current line |
| --- | --- |
| XDSH Python package (PEP 440) | `0.1.1rc2` |
| XDSH display / DSH runtime | `0.1.1-rc.2` |
| GitHub release / tag | `xdsh-0.1.1-rc.2` |
| Python SDK protocol client | `deepseek-harness-sdk==0.1.1rc1` |

## The alignment invariant

The XDSH version must **exactly** match the paired DSH line. This is enforced
by `versions_align` (strict equality after normalization), asserted at import
time in `src/xdsh/version.py`, and checked in CI ("Check the aligned XDSH and
DSH release contract").

The supported version spellings are a numeric base plus an optional
`alpha`/`beta`/`rc` label (e.g. `0.1.1rc2`, `0.1.1-rc.2`, `0.1.1`). Any
other shape — including a `.post` suffix — is currently rejected by
`normalize_release_version`.

Consequence: changing the version number is never a cosmetic choice. Either the
version stays exactly on the current line, or the product moves to a new
aligned line with a different DSH runtime. Nothing in between is permitted by
the current model.

## Rule 1 — Fixes on a release line keep the version

Updates that stay on the same line (for example bug fixes or compatibility
fixes while DSH remains `0.1.1-rc.2`) **do not change the version number**.
The version remains `0.1.1rc2` (tag `xdsh-0.1.1-rc.2`).

Each updated snapshot is a distinct immutable artifact set, identified by its
`PUBLIC_RELEASE.json` per-file SHA-256 manifest and by the wheel/sdist hashes —
not by the version number. A newer checkout therefore does not reproduce the
hashes of an earlier snapshot; that is expected and by design.

Applies to:

- all iteration before the first public release, and
- in-repository maintenance after a release, where fixes are carried until the
  next line ships.

## Rule 2 — Re-distributing a published fix (requires a deliberate alignment change)

A published version cannot be re-uploaded with different content. If a fix must
be distributed to users of an already-published line, a version change is
unavoidable — and that requires an explicit, documented change to the alignment
model. Neither option below is enabled today; they are templates for a future
decision, each requiring updates to `version.py`, `test_version.py`,
`COMPATIBILITY.md`, and this document, plus a rerun of the release matrix.

| Option | Version spelling | Tag | Ordering | Alignment relaxation |
| --- | --- | --- | --- | --- |
| **A. Post-release (recommended)** | `0.1.1rc2.post1` | `xdsh-0.1.1-rc.2.post1` | `0.1.1rc2 < 0.1.1rc2.post1 < 0.1.1` | Ignore the post suffix when comparing to the DSH line |
| **B. XDSH-side rc iteration** | `0.1.1rc3` | `xdsh-0.1.1-rc.3` | `0.1.1rc3 > 0.1.1rc2` | Same base and label; XDSH rc number ≥ DSH rc number |

Option A has the least ambiguous semantics ("a patch on top of the rc2
candidate"). Option B follows the upstream habit of iterating rc numbers but the
rc number then counts XDSH-side only, so it must be documented to avoid implying
a matching DSH rc.

## Rule 3 — Line freeze and the next line

After publication, the `0.1.1-rc.2` line is **frozen** as the immutable
baseline. XDSH-side fixes and any adoption of a newer DSH runtime move to the
next aligned line — for example `xdsh-0.1.5-rc.1` paired with DSH
`0.1.5-rc.1` — which is a separately versioned release with its own
`DSH_HOME`, runtime command, image tag, smoke root, and compatibility matrix.

The previous line is retained as an immutable archive (like
`xdsh-0.1.0a0-rc2`) and is never overwritten or re-hashed.

## Decision guidance

| Situation | Version | Action |
| --- | --- | --- |
| Iterating before first publication | `0.1.1-rc.2` unchanged | Regenerate the snapshot; verify the manifest |
| Published line needs an urgent patch; next line not ready | Rule 2, Option A | Document and implement the alignment change, then publish `0.1.1rc2.post1` |
| Published line needs a fix; next line ready | Rule 3 | Carry the fix into the next aligned line |
| Adopting a newer DSH runtime | New aligned line (Rule 3) | New `DSH_HOME`, matrix, and publication; never bump the old line |

## Enforcement points

- `src/xdsh/version.py` — constants, `normalize_release_version`,
  `versions_align`, import-time alignment assertion.
- `tests/unit/test_version.py` — active pair identity and normalization
  contract.
- CI "Check the aligned XDSH and DSH release contract" step.
- `COMPATIBILITY.md` validated pairing table and `RELEASE-MATRIX.md`
  publication rules.
