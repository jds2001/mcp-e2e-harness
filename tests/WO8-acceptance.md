# WO-8 implementation and acceptance

Implemented [WO-8](../documentation/work-orders/WO-8-zero-trace-null-final.md). Live spend was **$0**. No files under `documentation/` were changed; no retry, scaffold change, or `consumer_limit` rename was introduced.

## Before and after

WO-7 exempted an all-zero-trace cell if any row had `consumer_limit.cause = null_final_content`, no harness failure, and no recorded breach. It did not restrict the driver family or explicitly require spawn success, an exact wire surface, and a completed final response. In particular, the [pre-fix product run](../runs/2026-09-21-wo8/before-product/run/run-manifest.json) records exit 0, empty output, zero trace records, `harness_failure: null`, zero failures, and one `null_final_content` entry in the run's `consumer_limits`. That was broader than the ruling.

The exemption now requires a **loop** row and all three positive observations, with no other harness failure or breach. Every zero-trace null-final row records `zero_trace_liveness` beside `consumer_limit`, including denied exemptions. The observations are:

- `spawn_check_passed`: the recorded cell spawn check explicitly says `ok: true`; `spawn_check_path` identifies its source.
- `wire_surface_exact`: a non-empty, readable wire trace shows the expected tools on every request; `wire_requests` and `expected_wire_surface` state the denominator and expected surface. Missing, malformed, or mismatched evidence does not pass.
- `final_response_observed`: the final wire record shows an integer HTTP status in 200–299, a non-empty `finish_reason`, and a completed response. `final_response` records its sequence, status, and finish reason.

Chat-completion wire lines now include `http_status`, copied from the transport response. The previous recorder did not record status explicitly, so its absence of an error note could not establish the required positive 2xx observation. No response content was added to the wire recorder. Incomplete responses, missing status/finish reason, and non-2xx responses cannot qualify.

A denied zero-trace null final receives a harness failure and is excluded from the run's consumer-outcome count. Product rows cannot qualify even if supplied an exemption-shaped record. Each row is evaluated independently: a qualifying sibling does not lift another row's harness failure. The existing cell liveness gate writes `CELL-VOID.json`; its `cell_void` annotation now also appears consistently in both row metadata and the run manifest's copy of that row.

## Observable artifacts

The three required rows are in [runs/2026-09-21-wo8](../runs/2026-09-21-wo8/), ignored by Git under repository convention. [acceptance-summary.json](../runs/2026-09-21-wo8/acceptance-summary.json) includes the observations and run counts, along with the pre-fix product comparison.

| Row | Spawn passed | Exact wire surface | Completed final response | Exempted | Run counts |
| --- | --- | --- | --- | --- | --- |
| [Loop](../runs/2026-09-21-wo8/loop/run/loop-cell/A/A1/meta.json) | true | true | 200, stop, seq 1 | true | 0 failures, 1 consumer outcome, 0 zero-trace cell failures |
| [Loop with mismatch](../runs/2026-09-21-wo8/mismatch/run/loop-cell/A/A1/meta.json) | true | false | 200, stop, seq 1 | false | 2 failure events, 0 consumer outcomes, 1 zero-trace cell failure |
| [Fake product](../runs/2026-09-21-wo8/product/run/basic/A/A1/meta.json) | true | false | absent | false | 2 failure events, 0 consumer outcomes, 1 zero-trace cell failure |

The two failure events in each broken run are the row's harness failure and the existing cell-level zero-trace failure, not two failed invocations. Each run has exactly one row. The product still exits 0 and its answer remains empty, but the row is BROKEN and its `CELL-VOID.json` agrees. The raw `consumer_limit` cause is retained for diagnosis while the run-level `consumer_limits` map excludes broken rows.

The qualifying loop row's first and only response has null content and a proposed tool call in its reasoning. Its metadata records `null_final_content`, `reasoning_tail_json: true`, and the matching offered `file_note` tool; `harness_failure` is null, its answer is zero bytes with a null digest, and its trace count is zero. The three observations and `exempted: true` are present in that row's metadata. The [wire line](../runs/2026-09-21-wo8/loop/run/loop-cell/A/A1/api-surface.jsonl) records status 200 and finish reason `stop`; the reasoning remains in the [consumer transcript](../runs/2026-09-21-wo8/loop/run/loop-cell/A/A1/loop-session-single.json). There is exactly one scored request and zero retries.

For the mismatch row, the fixture actually restricts the proxy to `read_note` while the manifest and spawn check specify the full three-tool surface. Its wire trace therefore shows two missing tools; this is an injected registration defect, not an edited expected-result assertion. The final response is still 200/stop, but the row is BROKEN because the surface condition failed.

## Tests and reproduction

Validation: **383 tests pass**, up from the 368-test WO-7 baseline. `ruff check .` and `git diff --check` pass. The corrected recorder timing test also passes independently.

The 15 new cases in [test_wo8.py](test_wo8.py) comprise `test_zero_trace_acceptance_rows` (3 cases), `test_each_missing_observation_leaves_row_broken` (10 cases), `test_exemption_is_loop_only`, and `test_valid_sibling_does_not_exempt_a_row_with_missing_evidence`. They cover failed/missing spawn evidence, mismatched/missing/malformed wire evidence, missing/non-2xx status, missing/empty finish reason, incomplete responses, and the product-driver restriction. The existing HTTP retry test also asserts the actual recorded 503 and 200 statuses. The tests also verify that a valid repeated row cannot supply missing evidence for its sibling. A full-suite run exposed an existing timing-test race: `test_duration_ms_is_arrival_to_end_of_response` discarded the HTTP response without consuming its body, allowing a client disconnect before the recorder finished. That test now reads the response body before asserting completion; production response handling was not changed.

Reproduce the artifacts in unused directories:

```sh
.venv/bin/python tests/wo8_acceptance.py runs/wo8-recheck/loop loop
.venv/bin/python tests/wo8_acceptance.py runs/wo8-recheck/mismatch mismatch
.venv/bin/python tests/wo8_acceptance.py runs/wo8-recheck/product product
.venv/bin/python -m pytest tests/ -q
```
