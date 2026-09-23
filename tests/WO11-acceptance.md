# WO-11 acceptance

The maintainer's run is recovered in place: `/Users/jstanley/code/uscode-mcp/runs/20260922T120550Z-e22-secondary/run-manifest.json` and `checks-report.json`. Recovery called no model or server. All 345 pre-existing files were SHA-256 checked before and after and are unchanged. Copies of both derived reports and the verification record are in `runs/2026-09-22-wo11/maintainer-rebuilt/`.

## Recovery result

Executed `.venv/bin/mcp-e2e report --run-dir /Users/jstanley/code/uscode-mcp/runs/20260922T120550Z-e22-secondary`; exit status 0. The command found `uscode-mcp/documentation/e2e-manifest.json` and verified its byte hash against every row: `58df7961da675367ecd3031a7439bde5077b96b3383233479655e248f7bf3605`. It reconstructed 22 rows, 18 observed slots, and 4 unmet attempts. The two replaced slots select `e22-floor-both/C/C3/attempt-04` and `e22-floor-field/C/C2/attempt-02`. Their metadata supplies cell, prompt, null repetition, and attempt 4/2; directory segments supply none of those identities.

The rebuilt manifest has `rebuilt: {at, harness_version, reason}`. Unrecoverable facts are null with explanations in `unrecoverable`: original selection filters/repetition/replacement allowance, requested slot denominators, pre-run estimate, total run spend and budget-stop events, and run-level builtin-probe/void records not retained in a checkpoint. The observed slot count and per-row spend are separately labeled facts, not guesses at requested N or total spend. Slot selection, observed attempts, unmet causes, and replacement start events (from attempt metadata and start timestamps) are recovered from rows, as are served-provider counts, verified provider names, and recorded loop probe verdicts. A modern run also retains `manifest-source.json` and `run-input.json`; recovery uses them to retain the exact manifest and recover selection, asked counts, and replacement allowance after an interruption.

The 16 checks produced 11 passes, 4 vacuous outcomes, 1 failure, and no errors. `banner-leads-truncated-content` matched 21 records and failed on seven: floor-field C2 attempt 2 index 0; floor-field C3 attempt 1 indices 0 and 2; floor-field C4 attempt 1 index 1; isolation-field C2 attempt 1 index 0; isolation-field C3 attempt 1 index 1; isolation-field C4 attempt 1 index 1. Each reference includes its real cell, prompt id, `repetition: null`, and attempt. This is the mechanical check result, not a consumer-score disposition.

| Check | Outcome | Matched |
| --- | --- | ---: |
| outcome-present | pass | 30 |
| not-found-echoes-query | vacuous | 0 |
| normalization-disclosed | pass | 6 |
| uscode-provenance-complete | pass | 14 |
| plaw-provenance-complete | pass | 7 |
| truncation-markers | pass | 21 |
| ambiguous-true-total | vacuous | 0 |
| recall-caveat-always | pass | 2 |
| private-law-scope-not-notfound | vacuous | 0 |
| appendix-redirect-actionable | vacuous | 0 |
| banner-leads-truncated-content | fail | 21 |
| structure-present-or-disclosed | pass | 14 |
| find-reports-true-totals | pass | 5 |
| superseded-present-on-success | pass | 14 |
| superseded-not-checked-surfaces-reason | pass | 5 |
| superseded-fired-accounting | pass | 3 |

The `answer_coverage` measurement summary reports 21 measured records and one null. Recovery writes only the two run-level reports, never a row. It refuses an existing run manifest unless `--overwrite` is supplied. `--manifest FILE` supplies a pinned manifest when automatic discovery cannot find the matching bytes; a hash mismatch refuses recovery before any report write.

## Regression artifacts and durability

Reproduce with `.venv/bin/python tests/wo11_acceptance.py <new-directory>`. The fake-upstream matrix is under `runs/2026-09-22-wo11/`; every arm declares the `must-fail` check and an answer-coverage measurement. Live spend for all WO-11 work was $0.

| Directory | Failure identities in checks-report.json |
| --- | --- |
| flagless-replaced | loop-cell / A1 / repetition null / attempt 2 |
| repeated-replaced | loop-cell / A1 / repetition 1 / attempt 2; repetition 2 / attempt 1 |
| flagless-clean | loop-cell / A1 / repetition null / attempt 1 |

Each identity retains `invocation` as its actual artifact locator. `checks-exception/` injects `RuntimeError: WO-11 injected evaluator exception` into the evaluator. Its checkpoint contains all completed rows, `reporting.checks.status: error`, and the exception. The checks report marks every declared check `error`, including per-cell outcomes; measurement reporting still completes. `measurements-exception/` injects the same exception into measurement computation: checks remain available, `reporting.measurements` records the exception, and the unavailable measurement summary is null. Both return nonzero run outcomes and print a terminal line identifying the missing completed artifact and why. The tests also verify the CLI's nonzero exit.

The runner now writes `run-manifest.json` atomically before either derived stage starts, checkpoints `running` before invoking a stage, and rewrites the manifest after success or exception. Row measurements are computed after this checkpoint; a measurement exception can no longer prevent row metadata from being retained. A killed process can leave a `running` stage, which `report` rebuilds from the retained evidence. Missing trace files that metadata says contain records produce a reporting error, rather than a vacuous check result.

## Path audit

Source audit used `rg` for `split`, `.parts`, and `relative_to` throughout `src/mcp_e2e_harness`.

- `checks.py` formerly grouped by `label.split('/')[0]`: replaced with the row metadata's `cell`.
- `checks.py` formerly split failure paths and parsed the fourth segment as a repetition: removed; `cell`, `prompt_id`, `repetition`, and `attempt` come from metadata.
- `runner.py` formerly parsed a failure locator's first segment to attach cell marks: removed; reporting attaches the recorded row's family/reproducibility directly.
- `runner.py`'s spawn-check `relative_to` only records a file locator. `reporting.py`'s `relative_to` only records each discovered row's locator; grouping uses the metadata tuple `(cell, prompt_id, repetition)`.
- Remaining slash splits in `checks.py` and `measurements.py` implement JSON Pointers; `openrouter.py` splits provider tags. `api_capture.py` parses URLs/HTTP framing. CLI/proxy/driver splits parse command-line lists/options. None derive row identity.

The standalone checks API accepts a metadata map keyed by opaque locator. If omitted for a standalone evaluator call, identity is unknown; it never falls back to parsing a path. The runner and recovery always supply actual row metadata.

## Tests

`test_wo11.py` covers the three checked matrix arms and immutable-row rebuilds; an intentionally misleading path; injected exceptions in both stages with checkpoint assertions; recovery without any checkpoint/startup snapshot; overwrite refusal; manifest-hash refusal; recovery of asked counts from startup snapshots; and a missing recorded trace. Existing WO-6 tests now provide explicit metadata when testing per-cell grouping. Baseline after WO-10: 422 tests.

Nothing under `documentation/` changed. WO-11 has its own commit and this report, separate from WO-10.

Validation: 432 tests passed in the full regression run, up from 422 after WO-10. The final recovery refinements also passed all 10 WO-11 cases in a targeted rerun. Ruff and `git diff --check` are clean. The new test functions are `test_replacement_check_matrix` (three arms), `test_identity_is_not_the_path`, `test_stage_exception_retains_run` (checks and measurements), `test_killed_run_recovery_and_unknown_facts`, `test_rebuild_refuses_wrong_manifest`, `test_killed_run_with_snapshot_recovers_requested_counts`, and `test_missing_recorded_trace_is_reporting_error`.
