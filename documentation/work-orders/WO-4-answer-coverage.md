# WO-4 — Row measurements: `answer-coverage@1`

**Issued:** 2026-09-18, spec session. **For:** the implementation session. **Spec authority:** `30-checks.md` → "Row measurements" (declaration shape and the normative method text), `40-instruments.md` → `answer-coverage@1` reference vectors, `20-manifest.md` → `measurements`, `10-harness.md` → recorded per cell, `00-INDEX.md` S14. Files win on conflict. Sequencing: independent of WO-3, but the suite reads this beside `finish_reason`, so land both before the next scored C1 run.

## Deliverables

1. **Manifest support** for the top-level `measurements` list per `30-checks.md`: `name`, `measure`, `applies_to` (the checks selector, reused, not reimplemented), `reference` (RFC 6901 pointer relative to the trace record, must resolve to a string), method parameters. Validation errors are load errors; explicit `null` ≡ absent (DR-2).
2. **`answer-coverage@1`** implemented exactly as the numbered method text in `30-checks.md`, which the implementation embeds verbatim and hashes (`content_hash()` like a scaffold; report the hash). Whitespace normalization as stated, offset map kept for the reference, longest-common-substring recursion with the stated tie-break and no junk heuristics, floor default 64. A `difflib`-style implementation is acceptable if and only if it reproduces every reference vector exactly.
3. **Recording:** per row, `meta.json` → `measurements.<name>` = list of `{index, tool, method, method_hash, reference_pointer, reference_chars_raw, reference_chars_normalized, answer_chars_raw, floor, spans, matched_chars, furthest_offset, share}`, one per matched record; a null entry with a `note` when the pointer does not resolve to a string. Never in `checks-report.json`'s outcomes; a summary line in the run manifest is fine as long as it is not an outcome.
4. **Reference vectors as tests.** Copy the `answer.txt` and the reference string of each row in the `40-instruments.md` vector table into `tests/fixtures/answer-coverage/` (the public-law text is public domain; the answers are model output from our own runs) and assert every value in the table exactly, including the floor-40 rows and the zero-span row. If any vector does not reproduce, do not adjust the vector: report the discrepancy with the spans you found.
5. **Tests** for the declaration validation, the non-string pointer path, and the selector reuse; `ruff` clean; nothing under `documentation/`. Live spend: $0.

## Addendum (2026-09-18, after WO-3 acceptance)

6. **Key the distinct-answer count by prompt.** WO-3's `answers` block aggregates across a cell's prompts, so its duplicate can be two prompts whose answers matched. Requirement 11 concerns repeats of one prompt: record `answers` per prompt id within the cell (`answers: {"C1": {invocations, distinct, digests}, …}`), and keep a cell-level total only if it is clearly labeled as across prompts. The per-row `answer_sha256_16` stays as is. One test with two invocations of one prompt and one of another.

## Acceptance

The reported method hash; one real `measurements` entry from a fake-upstream run; the vector test's name and its pass; any discrepancy stated as such.

## Out of scope

Any second measure; any pass/fail derived from a measurement; changes to the checks language.
