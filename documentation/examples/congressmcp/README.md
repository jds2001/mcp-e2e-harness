# The congressMCP suite — ancestor and worked example

These two files are the seed material this spec was generalized from, kept **verbatim** (ruling S3): `60-e2e-harness.md` is congressMCP's e2e verification contract, and `prompts.json.example` is its §17 prompt manifest. They are provenance and pedagogy, not normative — the schema they would have to satisfy today is `../../20-manifest.md`.

## Why it is worth keeping

The grounding rules in `10-harness.md` are not hypothetical caution: three of this manifest's prompts (A3, B3, E3) were invalidated because they were written from plausibility about the corpus instead of from the record — A3 was confounded by an unnoticed section-number collision, B3 exercised nothing because the assumed number reuse did not exist, and E3 asked about an enrolled version that does not exist. The annotations on those entries are the best argument for the rules. The `isolation-warm-a4` cell is the model preregistration: expected result, falsifiers, and the disposition each falsifier triggers, pinned before the run.

## What in here is server-specific (the genericization map)

- Cross-file references — `40-tools.md`, `90-observations.md`, `00-INDEX.md`, rule ids like R8/R10, O-observations, F-defects, §-sections — point into **congressMCP's spec directory**, which is not present in this repo.
- `bill_text_only` → the generic `tool_surface` allowlist.
- `cache: {mode, packages}` and `CONGRESSMCP_CACHE_DIR` → the generic `setup` actions plus the no-warm-by-accident rule (S5).
- `documents` with `sha256_16` → generic `fixtures` with `content_hash`.
- The hard-coded Layer-1 list (error envelope, truncation markers, provenance completeness, …) → suite-supplied declarative `checks`.
- Groups A–F and the four Group F invariants → suite-defined groups; the sourcing discipline survives as the `sourcing` field.
- Knob names (`thinking`, `reasoning_effort`) were already correctly driver-native; that convention is kept as-is.

## Known deviations from `20-manifest.md`

This manifest predates the schema: it has no `suite`/`server`/`checks` blocks, uses `use_single_step_variant` instead of `variant`, `bill_text_only` instead of `tool_surface`, `cache` instead of `setup`, and its Group F entries carry `sourcing: "DERIVED -- NOT a verbatim original"` prose rather than the enum. Do not "fix" it — its value is being the untouched ancestor.
