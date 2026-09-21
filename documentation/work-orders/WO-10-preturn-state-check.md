# WO-10 — check the crowding precondition from state, on every driver

**Issued:** 2026-09-21, spec session. **For:** the implementation session. **Spec authority:** `10-harness.md` → Layer 2, ruling S19 (with S17 and S18); `40-instruments.md` → `neutral-file-triage@2` mid-task predicate. Files win on conflict. Small.

## Background

WO-9's product-driver report back showed that a pre-turn which exits 0 and writes nothing is treated as a completed pre-turn: the scored turn runs, and the row has no `distractor-state.json` at all. On the loop driver the same situation is caught only because `loop_consumer` exits 4 on a consumer outcome. The precondition — the consumer is mid-task — is inferred from an exit code and never checked, although the state that defines it is written to every crowded row and the procedure that defines it is the harness's own. A scan of the live record found three scored floor rows that had received the scored prompt at one or two of four notes filed (`uscode-mcp/runs/2026-09-21T01:13:27-04:00-loop-complete/loop-floor` A3, A2, C2), all before WO-7.

## Part A — the state check

1. Each crowding procedure declares its mid-task predicate beside its content (for `neutral-file-triage@2`: the filed set is exactly `{n01, n02, n03, n04}`, folders unconstrained). Whether the predicate is inside the procedure's content hash is your call; say which, and why. If it is inside, the hash changes and the pin record in `40-instruments.md` will be updated by the spec session from your reported value — report the new hash and the value before and after.
2. After every pre-turn, on every driver, before the scored prompt is sent, the harness reads the distractor's recorded state and evaluates the predicate. A failing state is precondition unmet (S17) with cause `state_mismatch`; the row's `precondition_unmet` block carries `expected` and `observed` sets and the stage; the failure text leads with the cause; the slot is replaced under S18 exactly as for a consumer-outcome cause. A pre-turn that reports a consumer outcome *and* fails the predicate records the consumer's cause with the state beside it — one cause, not two rows.
3. A missing or unreadable state file is a harness failure (the instrument did not record), never a pass and never precondition unmet.
4. Product drivers: a pre-turn that exits 0 with empty output and a failing state is precondition unmet by the state, as above. State what a product pre-turn with empty output and a *passing* state is recorded as (it is reachable only if the driver did the task and printed nothing; say whether you treat it as completed).
5. Unit tests: predicate pass; each way to fail it (fewer filed, more filed — v1's post-task shape — wrong ids); missing state file; product driver with empty output on both sides of the predicate; a consumer-outcome exit with a failing state records one cause. `ruff` clean; nothing under `documentation/`. Live spend: $0.

**Acceptance, part A — fake upstream:** (a) a loop pre-turn that ends with a text final after filing two notes (no consumer outcome, exit 0): the row precondition unmet, cause `state_mismatch`, expected and observed sets in the block, replaced, the replacement's state at `n01`–`n04`, manifest counts. (b) The same with the product-family fake driver writing nothing and exiting 0 — the `product-empty` fixture from WO-9 with the state actually empty: no longer `reached 2`. (c) A pre-turn that files five: unmet from the other side. (d) A state file deleted between pre-turn and check: harness failure, counted in `failures`, not replaced. (e) `clean` from WO-9 re-run: unchanged counts and layout.

## Part B — three small things read from the WO-9 artifacts

6. `cell_marks.<cell>.answers.<prompt>.invocations` (and the `loop_cells` copy) counts only attempts that reached the scored turn, so it equals `invocation_counts.<cell>.<prompt>.reached`; in WO-9's `replaced/` it read 3 against `reached 2`.
7. The budget-stop terminal line prints the cap at the precision it was given (`$0.004`, not `$0.00`), and says "not started" only of a cell in which no attempt ran; WO-9's `budget/` said it after two attempts.
8. A slot with replacements disabled that ends unmet gets a status meaning *no replacement allowed* rather than `exhausted`; `exhausted` stays for a slot that used its allowance. Say the names.

**Acceptance, part B:** WO-9's `replaced/`, `budget/`, and `disabled/` cases re-run, with the three fields as ruled.

## Report back

For each crowding procedure the harness ships, the predicate as declared and where it lives. Whether any existing test fixture relied on an under-filed pre-turn passing.

## Out of scope

Any change to the procedures' prompts or notes, or to `loop-scaffold@1`. Retrospective re-marking of rows already on disk — the three live rows are the suite's to disposition, and the predicate is applied to them by hand.
