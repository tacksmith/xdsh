---
name: xdsh-evidence-reporting
description: Produce concise reports that separate observed facts, inferences, decisions, and unresolved questions with traceable source references.
---

# Evidence-backed reporting

1. Record the question and acceptance criteria before collecting evidence.
2. Prefer authoritative local files and structured tool results; identify the
   exact path, event, or command that supports each material claim.
3. Label an inference as an inference and keep it narrower than the evidence.
4. Report negative results, missing access, and conflicting observations
   explicitly. Never fill a gap with a plausible-sounding fact.
5. Finish with the verified result, remaining risks, and one actionable next
   step.

For measured work, keep the final report compact: result, supported differences,
unverified requirements, artifact locations, and measured timing. Distinguish
end-to-end duration from download/setup/compute time; absent telemetry means
unknown, not an estimated fact. Generate numeric fields from saved values and
check units, rounding and comparison definitions. Do not describe a normalized
squared loss as a percentage-point error. Keep target-guided calibration separate
from independent verification; a passing file check is not scientific approval.
Compute each paired difference and any claimed maximum from the same saved
values; do not overlook a metric with a larger deviation. When available, use
`xdsh-verify --details` with label-aligned `max_abs_differences` assertions and
explicit units; report its computed maximum, not an unchecked estimate. Keep
the receipt's file hashes with the report and recheck after edits. A verified
arithmetic statement is not evidence that the experiment's methods were valid.
Different sample
counts are unresolved until supported by a provenance check, not automatically
rounding or harmless exclusions. Similar outputs cannot certify a historical
software version. Summarize completed work and artifact paths at milestones so
an upstream interruption still leaves a useful handoff, not only a progress bar.

Keep citations and excerpts bounded. Do not include credentials, private
tokens, raw hidden reasoning, or unrelated user data in the report.
