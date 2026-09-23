# WO-10 acceptance

Artifacts are under `runs/2026-09-22-wo10/`; reproduce with `.venv/bin/python tests/wo10_acceptance.py <new-directory>`. All traffic used local fake upstreams; live spend $0. Rows recorded before this change were not modified.

## Predicate and identity

Both shipped procedures (`neutral-file-triage@1`, retained for historical identity, and `neutral-file-triage@2`) declare `expected_filed = (n01, n02, n03, n04)` beside their content in `src/mcp_e2e_harness/crowding.py`, with `check_state` comparing the filed keys exactly. Folders are unconstrained. The same guard rejects v1's completed-task shape; it does not rehabilitate v1 for new suites.

The predicate is instrument validation, outside the consumer-visible content hash. This preserves the pins for the unchanged prompts, notes, and folders. Before and after hashes are identical: v1 `d7cf153febdc9497dab62109dd666158162e11fc4596b69dfa57457e0fd400d2`; v2 `3197f43ed3cc8b794ef97bb62945f63cb6a5287e12859cbcf65e946dde52ac3e`. No repin is needed. The applied predicate and observations are recorded in `crowding.json.state_check` and successful/unmet rows' `meta.json.crowding.state_check`; unmet blocks also carry expected and observed sets. The loop scaffold is unchanged.

After every completed pre-turn, the harness reads `distractor-state.json` before launching the scored turn. An instrument read/shape failure remains a harness failure without replacement. A reported consumer outcome retains its cause and gains the state evidence. Otherwise a failed predicate becomes `state_mismatch`. Other nonzero pre-turn exits remain ordinary harness failures under S17. The distractor now records its initial empty state when it starts, even if no note is filed; the runner does not invent state for an instrument that never started.

## Artifact matrix

| Directory | Asked / reached / attempts | Observation |
| --- | --- | --- |
| two | 2 / 2 / 3 | First pre-turn files n01–n02 and returns text with exit 0; `state_mismatch`, replaced; attempt 2 files n01–n04 and scores. |
| five | 2 / 2 / 3 | First pre-turn files n01–n05; rejected from the other side, replacement passes. |
| product-empty | 2 / 0 / 8 | Product fixture records an empty filed set and exits 0 without output; all attempts are unmet, both slots exhaust, zero-trace cell BROKEN. |
| product-pass | 2 / 2 / 2 | Product fixture records exactly four filed notes and exits 0 without output; treated as a completed pre-turn, both scored turns execute. |
| missing | 2 / 0 / 2 | Fixture writes then deletes the state before the check; two ordinary harness failures, no replacements, plus the cell liveness failure. |
| unreadable | 2 / 0 / 2 | Invalid JSON state; same failure accounting and no replacement. |
| clean | 2 / 2 / 2 | WO-9 clean rerun retains the old layout without attempt subdirectories. |
| replaced | 2 / 2 / 3 | Consumer null-final cause preserved with observed n01; answers invocations now 2, matching reached. |
| disabled | 2 / 1 / 2 | Unmet first slot has `replacement_disabled`, rather than `exhausted`. |
| budget | 2 / 0 / 2 | Terminal cap is `$0.004`; later cell line says `stopped`, not `not started`. |

Each directory contains a run manifest, row artifacts, and `progress.txt`. The `two` and `five` first-row failure lines lead with `state_mismatch`. `replaced` records one precondition cause per attempt, not separate state and consumer rows. An unmet row stays unscoreable under the existing non-null `harness_failure` rule.

`cell_marks.<cell>.answers.<prompt>.invocations`, including the `loop_cells` copy, now counts only scored-turn attempts. `invocation_counts` continues to report all attempts separately. Slot statuses are `replacement_disabled` for an unmet slot with zero allowance and `exhausted` after the enabled allowance is used.

## Fixtures and tests

The generic product fake consumer previously ignored the distractor entirely. The generic fake OpenRouter pre-turn called a SUT tool once and returned text without filing notes; the crowded conversation test and WO-7 crowded fixture depended on it passing. Those success fixtures now perform four real distractor calls. The WO-9 product-empty fixture deliberately still writes no state and now correctly produces an instrument failure; the WO-10 product-empty fixture explicitly records the empty state to test the different unmet outcome.

`test_wo10.py` adds predicate pass/fewer/more/wrong-id cases and ten acceptance cases covering both product-output cases, missing/unreadable state, replacement, budget display, clean layout, and disabled status. Existing WO-9 tests now check the consumer cause plus state evidence. All acceptance artifacts are fake-only, and nothing under `documentation/` changed.

Validation: baseline 407 tests; after WO-10, 422 tests pass. The targeted state/WO-9/crowding run passed 46 tests. Ruff and `git diff --check` are clean.
