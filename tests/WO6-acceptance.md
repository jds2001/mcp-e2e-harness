# WO-6 implementation and acceptance

Implemented both parts of [WO-6](../documentation/work-orders/WO-6-check-report-per-cell-and-cell-identity.md). Live spend was **$0**: every experiment used the local fake OpenRouter and a local stdio server. Reported `usage.cost` values are fixture data, not charges. Nothing under `documentation/` was changed.

## Artifact fields

- `checks-report.json → checks[].cells.<cell>` contains `outcome`, `matched`, and `failures` (plus `error` when applicable). The existing check-level `outcome`, `matched`, and `failures` are the run roll-up, with precedence `error > fail > pass > vacuous`. Every selected cell is represented, even when it has no attempted rows.
- Failure references retain `invocation`, `index`, and pointer-bearing `details`, and add `cell`, `prompt_id`, and, on repeated runs, `repetition`.
- Row `meta.json → env` records the cell's declared environment. Non-secret values are verbatim; secret entries have their destination key and a null value. `env_overrides` remains the driver's separate environment record. Cell values override transport values in server configuration, setup, spawn checks, and preflight secret resolution.
- `cell_id` retains its readable driver/model/context/surface and driver-specific components. Canonical JSON SHA-256 prefixes add `knobs`, `setup`, `env`, and `selection` (groups, prompt allowlist, variant). Secret values and secret-source variable names do not enter the identity. Notes and cell display names do not enter it.
- `--repeats N` uses whole-grid passes, with `rNN` directories only when the flag is supplied. `meta.json → repetition` and `run-manifest.json → selection.repeats` are null without the flag. Repeated proxy metadata includes `server_spawn.pid` and `server_spawn.started_at`.
- Per-prompt `answers` contains attempted `invocations`, non-empty `answered`, and `distinct`/`digests` over answered rows. Counts and estimates multiply by N; a cell budget stop's `skipped_in_cell` includes future repetitions.

## Recorded experiments

Artifacts are under [runs/2026-09-21-wo6](../runs/2026-09-21-wo6/) (ignored by Git, consistent with repository conventions). The two cells differ only in `env.WO6_ARM`. Transport sets that key to `transport-default`; each cell overrides it. The server returns its actual environment value, so both setup and scored traces demonstrate precedence.

| Experiment | Evidence | Observed result |
| --- | --- | --- |
| Baseline before implementation | [before/run](../runs/2026-09-21-wo6/before/run/) | Same manifest, no repeat flag |
| Flagless A/B run after implementation | [after/run/checks-report.json](../runs/2026-09-21-wo6/after/run/checks-report.json) | Control pass/1 matched; treatment fail/1; roll-up fail/2 |
| Three repetitions | [repeats/run/checks-report.json](../runs/2026-09-21-wo6/repeats/run/checks-report.json), [progress](../runs/2026-09-21-wo6/repeats/progress.txt) | Control pass/3; treatment fail/3; roll-up fail/6; both arms at r01 before r02, then r03 |
| One answerless control invocation | [answerless/run/run-manifest.json](../runs/2026-09-21-wo6/answerless/run/run-manifest.json) | Control: invocations 3, answered 2, distinct 1; `consumer_limit.cause = context_length`, zero harness failures |
| Flagless compatibility | [flagless-comparison.json](../runs/2026-09-21-wo6/flagless-comparison.json) | Identical 33 file paths; no removed keys; additions described below |

The repeated run records `invocations: 3, answered: 3, distinct: 2` for each arm. The answers have digests `0db8b298431affa5`, `bd8df28cfc87fce9`, and `0db8b298431affa5` in repetition order. [rows.json](../runs/2026-09-21-wo6/rows.json) lists all six row paths, their digests, server spawns, and setup process IDs. The six scored server PIDs are 14866, 14871, 14877, 14881, 14886, and 14891; each setup has a separate recorded process and all six neutral working directories are distinct. No missing setup record, reused server process, or digest/count disagreement was observed.

Both [control metadata](../runs/2026-09-21-wo6/repeats/run/control/A/A1/r01/meta.json) and [treatment metadata](../runs/2026-09-21-wo6/repeats/run/treatment/A/A1/r01/meta.json) record the environment and different `cell_id` strings. Each experiment's `probe-cache/` has exactly one JSON verdict: arms differing only in server environment share the consumer calibration cache. The treatment's cell-gate record says `cached: true`. The probe-cache key implementation was unchanged.

A recorded failure reference is:

```json
{
  "invocation": "treatment/A/A1/r01",
  "index": 0,
  "details": ["/response/content/0/text !~ /^control$/"],
  "cell": "treatment",
  "prompt_id": "A1",
  "repetition": 1,
  "driver_family": "loop",
  "reproducibility": "pinned"
}
```

**Acceptance wording conflict:** Part B's literal request for only two additional keys in a flagless run conflicts with Part A's new per-cell check fields and row environment, and Part B's own `answered` field. The path comparison passes exactly, but the literal two-key-only comparison does not: the added keys are `repetition`, `selection.repeats`, `env`, per-cell check outcomes/explicit failure row fields, and `answered`. No legacy keys were removed. These required reporting additions are retained and the discrepancy is reported rather than hidden.

## Validation and reproduction

Baseline: **291 passed**. After implementation: **324 passed** (33 additional cases), using `.venv/bin/python -m pytest tests/ -q`. The final focused run of `tests/test_wo6.py` also passed all 33 cases. `ruff check .` and `git diff --check` passed. The contribution guide mentions a known-failure gate, but neither `tests/check_known_failures.py` nor `tests/KNOWN_FAILURES.md` exists in this checkout; the full suite passes directly.

The added test names in [test_wo6.py](test_wo6.py) are:

- `test_checks_pool_repetitions_and_name_rows`
- `test_check_rollup_order` (3 cases)
- `test_cell_errors_win_and_other_cells_are_still_evaluated`
- `test_identity_includes_all_components` (11 cases)
- `test_identity_canonical_notes_and_secrets`
- `test_cli_rejects_bad_repeats_before_loading_manifest` (4 cases)
- `test_cli_repeat_layout_and_dry_count` (3 cases)
- `test_repetition_width_and_programmatic_validation`
- `test_answers_count_attempted_and_only_nonempty_digests`
- `test_fake_acceptance_repeats` (2 cases; also checks estimate multiplication, gate counts, process isolation, progress order, and environment precedence)
- `test_cell_cap_skips_remaining_prompts_and_repetitions`
- `test_repeated_zero_trace_rows_are_marked`
- `test_repeated_product_crowding_and_measurements`
- `test_repeated_run_cap_stops_future_passes`
- `test_cell_env_overrides_missing_transport_secret_and_records_keys`

The existing direct answer-aggregation test was updated to count answerless attempts and supply the real row's `answer_chars` field.

To recreate the post-change experiments in unused directories:

```sh
.venv/bin/python tests/wo6_acceptance.py runs/wo6-recheck/repeats 3
.venv/bin/python tests/wo6_acceptance.py runs/wo6-recheck/answerless 3 --answerless
.venv/bin/python tests/wo6_acceptance.py runs/wo6-recheck/flagless
```

The pre-change run was recorded before implementation. Its manifest is identical to the flagless post-change manifest. Raw logs and the path/key comparison accompany the recorded experiments.
