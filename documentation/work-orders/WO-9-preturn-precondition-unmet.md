# WO-9 — a pre-turn that ends in a consumer outcome: say so in the row, count it apart, and do not truncate the reason

**Issued:** 2026-09-21, spec session. **For:** the implementation session. **Spec authority:** `10-harness.md` → Layer 2, ruling S17; `10-harness.md` → run mechanics, operator legibility (S9). Files win on conflict. Small.

## Background

In a live uscode-mcp run (`uscode-mcp/runs/20263721183723-loop-complete/loop-floor/A/A2/r01/`) the crowding pre-turn ended on S16's null final after filing one note of four. The harness rightly did not send the scored prompt. But the row's `meta.json` reads `harness_failure: "crowding pre-turn exited 4; …"` with `consumer_limit: null`, the cause (`null_final_content`) is only in `loop-result-crowding.json` and `crowding.json`, the row counts as an instrument failure, and the terminal line was cut before it reached either the cause or the path. The maintainer had to guess the cause, and guessed right.

## Deliverables

1. **The row says why.** When a crowding pre-turn ends in a consumer outcome (any cause of S13's family, S16's included), the row's `meta.json` carries a block for it — suggested `precondition_unmet: {stage: "crowding_preturn", cause, detail, reasoning_tail_json, reasoning_tail_matches_tools}`, copied from what the consumer reported — and the failure text names the cause, not only an exit code. Shape is yours; the content is not.
2. **The row stays unscoreable under the existing scorer rule.** `SUITE-AUTHORING.md` tells a scorer to read `harness_failure` first and to treat only `harness_failure: null` rows as consumer outcomes. Do not break that: either keep `harness_failure` non-null on these rows, or report what you did instead and why a scorer cannot mistake the row for an answerless consumer. The row is never in `answered` and never in `consumer_limits`.
3. **Counted apart.** The run manifest lists these rows under their own key with the cause (suggested `preconditions_unmet: {"<cell>/<prompt>[/rNN]": "<cause>"}`), and they do not add to `failures`. The run-end summary line reports them separately from harness failures. A pre-turn that fails for any other reason (crash, breach, spawn death, non-zero exit without a reported consumer outcome) is a harness failure exactly as today.
4. **Per-(cell, prompt) counts make the shortfall readable:** invocations asked for, invocations that reached the scored turn. Say where you put them.
5. **Cell liveness unchanged.** A cell whose every row is precondition-unmet has zero trace records and is BROKEN as today; show it.
6. **The terminal line.** A failure line leads with the cause and the full text is printed untruncated (its own line is fine). State what the truncation rule was and what it is now.
7. Unit tests for each of the above; `ruff` clean; nothing under `documentation/`. Live spend: $0.

## Acceptance — observable artifacts

Fake-upstream runs, crowded loop cell, `--repeats 2`: (a) repetition 1's pre-turn ends on null content after one tool call, repetition 2 completes — row r01 with the block and the cause in its failure text, no `loop-turn` request for the scored prompt on its wire (the wire line count says so), r02 answered; the manifest's new key, `failures` not incremented by r01, the asked/reached counts; the captured terminal output with the full line. (b) Both repetitions' pre-turns end that way — the cell BROKEN for zero trace records, `CELL-VOID.json`, and the manifest. (c) A pre-turn that dies without a consumer outcome — a harness failure counted in `failures`, as before. A before/after of (a) against e2ae37b.

## Report back

What a product-driver crowded cell does in the same situation today (a pre-turn with empty output, or a non-zero exit), from an artifact and not from the code's description: S17 was read from a loop row only, and the spec session does not know whether product drivers can report a consumer outcome from a pre-turn at all.

## Out of scope

Replacing lost repetitions (Q11, with the maintainer). Any retry or re-prompt inside the pre-turn (S16 corollary). Any change to `neutral-file-triage@2` or `loop-scaffold@1`.
