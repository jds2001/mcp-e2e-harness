# WO-9 acceptance

Artifacts: `runs/2026-09-21-wo9/`. Reproduce individual cases with `.venv/bin/python tests/wo9_acceptance.py <new-directory> <case>`; `disabled` reproduces `after`. All model traffic used the local fake OpenRouter server. Live spend: $0. The dollar amounts in the artifacts are synthetic usage costs reported by that fixture.

## Classification and scorer contract

`meta.json.precondition_unmet` copies the consumer's cause, detail, and reasoning-tail fields and adds `stage: crowding_preturn`. Its `harness_failure` stays non-null, so the existing scorer rule rejects the row. These rows have no answer, no scored-turn result file, and no entry in `consumer_limits`. Instrument breaches take precedence and prevent replacement. The run manifest's `preconditions_unmet` maps each attempt's relative path to its cause, slot path, and attempt number.

`before/` ran before the implementation edits, on source identical to e2ae37b (the intervening commits changed documentation only). `after/` runs the same fixture with retries disabled. Both manifest hashes are `d58fd627bb74ac95…`. Before: r01 reports only pre-turn exit 4, the terminal truncates the reason, and failures is 1. After: r01 names `null_final_content`, carries the full consumer outcome, and failures is 0. In both, r01 has two wire requests and one pre-turn tool call, no scored-turn request, and zero scored trace records. r02 has seven wire requests (five pre-turn, two scored), one scored trace record, and a 20-character answer. Read `after/progress.txt` for the complete cause and artifact path.

Previously the completion line cut `harness_failure` at 90 characters; the cell breach explanation also cut its underlying reason at 300 characters. Both display cuts are removed. Failure text now prints on its own line; an unmet precondition leads with the consumer cause.

## Attempts and counts

Attempt 1 retains the existing `<cell>/<group>/<prompt>[/rNN]/` path. Replacements live in `attempt-02/`, `attempt-03/`, and so on beneath that path. Every attempt has its own metadata, neutral cwd, server configuration, transcript, and wire artifacts. `clean/` has no attempt subdirectories. `slots[slot].result` selects the attempt that reached the scored turn; exhausted slots have null results. `invocation_counts[cell][prompt]` records asked slots, reached scored turns, and actual attempts, including attempts that never reached the scored turn. Counts include requested slots skipped by a budget stop in the asked denominator.

| Artifact directory | Asked | Reached | Attempts | Unmet | Harness failures |
| --- | ---: | ---: | ---: | ---: | ---: |
| after | 2 | 1 | 2 | 1 | 0 |
| all-unmet | 2 | 0 | 2 | 2 | 1 |
| crash | 2 | 1 | 2 | 0 | 1 |
| replaced | 2 | 2 | 3 | 1 | 0 |
| exhausted | 2 | 1 | 5 | 4 | 0 |
| scored-null | 2 | 2 | 2 | 0 | 0 |
| budget | 2 | 0 | 2 | 2 | 1 |
| clean | 2 | 2 | 2 | 0 | 0 |

`all-unmet/run/loop-cell/CELL-VOID.json` is BROKEN for zero scored trace records; its single failure is the cell liveness failure. `crash/` retains an ordinary pre-turn error with no reported consumer outcome. `replaced/` selects `loop-cell/A/A1/r01/attempt-02`, retains its failed predecessor, and announces the replacement cause in `progress.txt`. `exhausted/` records r01 exhausted after four attempts and r02 scored. `scored-null/` keeps two scored consumer outcomes, each on attempt 1. No retries occur inside a pre-turn.

Default replacements: 3; `--precondition-retries 0` disables them. The option is recorded under run `selection`, outside the manifest hash and cell identity. Pre-run loop records include `precondition_replacements.expected_usd` and `worst_case_usd`: 0.019974 and 0.079896 for this two-slot crowded fixture. These use the existing token estimate, with one versus four attempts per slot; reasoning-token uncertainty remains as disclosed by the existing estimate. Both amounts are printed before invocation work. Actual replacement usage joins the same run and cell spend counters. `budget/` reaches synthetic spend 0.005 against a 0.004 run cap after two failed attempts (including the 0.001 probe), then stops before attempt 3. The stop is in `budget.stops`, the slot status is `budget_stopped`, and the zero-trace cell is still BROKEN. Unit tests also exercise the cell-cap equivalent.

## Product-driver observation

`product-empty/` uses the product-family fake driver with a pre-turn subprocess that exits 0 and writes no stdout. Each `crowding.json` records exit 0 and an empty answer; both scored turns execute and answer. Counts are 2/2/2, failures 0, and no preconditions are reported. `product-crash/` instead exits 2 with no output: neither scored turn executes, neither attempt is replaced, and both rows retain ordinary harness failures. Failures is 3: two row failures plus the zero-trace cell failure. These artifacts establish the runner behavior with this product-family fixture; they do not establish how any live vendor CLI reports an answerless pre-turn.

Neither `neutral-file-triage@2` nor `loop-scaffold@1` changed. Nothing under `documentation/` was written.

## Validation

407 tests passed (`.venv/bin/python -m pytest tests/ --continue-on-collection-errors -q`), including 24 WO-9 cases; ruff and `git diff --check` are clean. The new tests cover separate accounting, complete failure output, retained attempt paths and fresh working directories, exhausted slots, both budgets, all three scored outcomes refusing replacement, all three reported pre-turn causes, breach precedence, CLI validation, and the product-family observations above.
