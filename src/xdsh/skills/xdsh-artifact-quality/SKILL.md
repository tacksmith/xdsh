---
name: xdsh-artifact-quality
description: Produce usable reports and files with a declared audience, structured claims, inspectable outputs, and a final quality check before delivery.
---

# Artifact quality

1. Identify the audience, requested format, acceptance criteria, and size
   budget before generating an artifact. Keep source facts separate from
   commentary and mark inferred values.
2. Prefer a stable structure, meaningful headings, accessible tables, and
   deterministic filenames. Preserve links, citations, and provenance needed
   to inspect important claims.
3. Generate into an isolated workspace using bounded resources. Do not
   overwrite an existing user file, execute an untrusted embedded script, or
   include credentials and hidden reasoning.
4. Re-open or parse the result with an appropriate narrow validator; check
   syntax, required sections, links/paths, rendering limits, and redaction.
   For numeric artifacts, inspect actual array shape, dtype, finite values and
   sample alignment; names such as "embedding" do not establish dimensionality.
   Compare report ratios against saved numerators/denominators with explicit
   units and rounding. For any claimed maximum deviation, align all applicable
   metrics by label and use `max_abs_differences`, not a selected subset. Keep
   fractions and percentages distinct through explicit `scale` and `atol`.
   If available, `xdsh-verify --example` shows the bounded JSON/NPY checker;
   run `xdsh-verify --workspace <dir> --contract <relative-json> --details` on
   declared checks. Read its computed maximum and same-read file hashes; save
   this receipt with the report. After any artifact edit, recheck. A receipt
   identifies checked bytes, not future files or an atomic multi-file snapshot.
   It never establishes scientific validity or that a self-written
   contract is independent. Do not synthesize a host XDSH-VERIFY task header.
   Use `source_json` assertions to compare effective run/figure parameters with
   a method plan declared in `unchanged`. Write actual arguments from program
   variables, not a second handwritten claim. A file check verifies consistency,
   not whether the plan correctly interprets a paper or the program obeyed it.
   View the actual figure when visual claims matter; dimensions/nonblank pixels
   alone are not visual review. Keep one reproducible entry point and durable
   command/exit logs for promised deliverables, not only temporary shell history.
5. Deliver the artifact plus a concise verification result and known
   limitations. If validation fails, retain the partial artifact and explain
   the exact repair needed.
