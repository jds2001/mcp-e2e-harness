# WO-3 — Finish reason on the wire; distinct-answer count per cell

**Issued:** 2026-09-18, spec session. **For:** the implementation session. **Spec authority:** `10-harness.md` → recording contract (sixth allowlist entry), `50-drivers.md` → loop, requirement 11. Files win on conflict.

## Deliverables

1. **`finish_reason` on every loop wire line**, from `choices[0].finish_reason` of the chat-completions response, through the recorder's allowlist like the other five scalars: `finish_reason` / `finish_reason_note`, null with a note when absent. Also surfaced per turn in `meta.json` under `loop` (the scored turn's final finish reason at minimum) so a scorer can see `length` without opening the wire file.
2. **Distinct-answer count per cell** in `run-manifest.json` (`loop_cells.<cell>.answers: {"invocations": N, "distinct": M, "digests": {...}}` or equivalent), computed from a digest of `answer.txt` across the cell's invocations in the run. Product cells may carry the same field; it is cheap and the determinism question is not loop-specific.
3. **Tests** for both, fake upstream; `ruff` clean; nothing under `documentation/`. Live spend: $0 expected.

## Sequencing

The uscde-mcp spec session's E17 waits on deliverable 1: it separates cap-cut rows from self-cut rows by the recorded finish reason rather than by token-count inference. Land this before the next scored C1 run under the loop driver. The derived loop manifest under `runs/2026-09-18-q8-manifest/` is retired (`10-harness.md`, division of labor); scored loop runs use the suite's own manifest once it carries loop cells.

## Acceptance

One example line for `finish_reason` (a `length` case and a `stop` case from the fake), one run-manifest excerpt showing invocations versus distinct with at least one duplicate, and the test names.

## Out of scope

Sending sampling knobs by default (knobs stay verbatim from the manifest); any change to `loop-scaffold@1`.
