# WO-12 acceptance

Artifacts: `runs/2026-09-23-wo12/`. Reproduce with `.venv/bin/python tests/wo12_acceptance.py <new-directory>`. All requests went to local fake upstreams; live spend $0. Nothing under `documentation/` changed, and the crowding content and loop scaffold are unchanged.

## Refusal record and replacement policy

An exhausted HTTP 429, any HTTP 5xx, or request timeout now produces `meta.json.upstream_unavailable` with `http_status`, the provider named in the error response, `attempts`, `retry_schedule_s`, `step`, and `tool_calls_before`. A timeout has null HTTP status and `transport_error: timeout`; a response with no provider name records null, displayed as `unknown`, rather than substituting the configured pin. The `harness_failure` leads with `upstream_unavailable` and names the provider and status. The failure prints in full. The loop result preserves partial steps, tool calls, usage, and retries even when it exits with this error. `loop.retries` records retries across the invocation's pre-turn and scored turn.

The in-loop policy is three requests maximum per failed step: the initial request, a retry after 2 seconds, and a final retry after another 5 seconds. The recorded schedule is `[2.0, 5.0]`. The HTTP integration artifacts use those real waits. Timeout unit tests replace the transport and sleep function, asserting three requests and the same two waits. Generic connection errors are not assumed to be timeouts. A context-length response classified by S13 remains a scored consumer limit.

Only two classes qualify for whole-invocation replacement: (1) S17/S19 unmet crowding preconditions, and (2) exhausted retryable upstream refusals/timeouts. The latter remains an instrument-side failed row; it never becomes `consumer_limit` or `precondition_unmet`. Attribution/provider/wire breaches, recorder defects, missing state, server/spawn/tool-transport failures, generic process failures, the outer harness deadline, non-timeout connection failures, and nonretryable HTTP errors do not qualify. Instrument defects take precedence if also observed on an unavailable attempt. Scored consumer outcomes (`null_final_content`, `step_cap`, `context_length`) still do not qualify.

S18's whole-invocation counter and paths apply unchanged: default three replacements via `--precondition-retries`, first attempt at the existing row path, later attempts in `attempt-02/` etc. Each replacement starts a fresh invocation. Run and cell budgets are checked before replacements; they use the same spend counters. Because refusals also affect fresh cells, the worst-case estimate now includes replacements for every selected slot.

Slots now use `scored` only for a row that reached the scored turn without a harness failure. An unreplaced upstream failure is `failed`; a slot using all enabled replacements is `exhausted` with `cause: upstream_unavailable`. Successful replacement selects the successful attempt and makes the slot `scored`. `report --run-dir` applies the same disposition and reconstructs replacement causes from row metadata. It also preserves recorded budget-stop status.

Both per-slot records and `invocation_counts[cell][prompt]` carry `attempts_unmet` and `attempts_unavailable`. `reached` still counts attempts that entered the scored turn, not successful slots; it can exceed asked-for slots when scored-turn refusals are replaced. `answers.invocations` retains this reached-attempt denominator; failed attempts are never answered. Successful replacements contribute no harness failure. An unfilled unavailable slot contributes one failure, rather than one per lost attempt; when the whole cell has zero scored trace records, its existing BROKEN cell failure covers the loss without counting it twice. Budget-stopped slots remain budget outcomes.

## Observable acceptance

| Directory | Asked / reached / attempts | Unmet / unavailable | Failures | Result |
| --- | --- | --- | ---: | --- |
| part-a | 2 / 2 / 2 | 0 / 1 | 1 | Replacements disabled; r01 fails after a tool call, r02 answers. |
| replaced | 2 / 3 / 3 | 0 / 1 | 0 | r01 attempt 1 unavailable, attempt 2 scored; r02 scored. |
| exhausted | 2 / 8 / 8 | 0 / 8 | 1 | Both slots exhaust four attempts; zero-trace cell BROKEN. |
| budget | 2 / 1 / 1 | 0 / 1 | 0 | Budget stops before replacement; one failed attempt retained. |
| server-error | 2 / 3 / 3 | 0 / 1 | 0 | HTTP 503 replacement follows the same path as 429. |

### Part A

`part-a/run/loop-cell/A/A1/r01/meta.json` records HTTP 429 from `FakeProvider`, three requests with `[2.0, 5.0]`, `step: 2`, `tool_calls_before: 1`, and `loop.retries: 2`. The transcript retains the preceding tool call; the scored trace has one record. The row has no answer or consumer limit. Its slot is `failed`. `cell_marks.loop-cell.answers.A1` reads `invocations: 2, answered: 1`; the run has one failure. `progress.txt` names `upstream_unavailable` and `FakeProvider`.

The original run manifest is retained as `part-a/original-run-manifest.json`. A rebuild was performed in the same run directory; the rebuilt manifest agrees on slot statuses and `failures: 1`. Row metadata hashes were checked unchanged. `part-a/rebuild-progress.txt` records the rebuild, and the rebuilt manifest carries its normal `rebuilt` block.

### Part B

`replaced/run/loop-cell/A/A1/r01/meta.json` records the first-step refusal (`step: 1`, `tool_calls_before: 0`). `r01/attempt-02/meta.json` is the successful replacement. The slot selects `loop-cell/A/A1/r01/attempt-02`, reads `scored`, and records `attempts: 2`, `attempts_unavailable: 1`, `attempts_unmet: 0`. The terminal includes `REPLACEMENT: upstream_unavailable: loop-cell/A/A1/r01 attempt 2/4, whole fresh invocation`. Run failures is zero.

`exhausted/` refuses all three requests in each of all four attempts of each requested slot. Each slot reads `exhausted`, `cause: upstream_unavailable`, four attempts, four unavailable and zero unmet. The runner continues to the next requested slot. `CELL-VOID.json` records BROKEN for zero trace records; failures is one, not eight.

`budget/` allows a tool call before three refusals. The probe and first successful request total synthetic spend 0.002 against the 0.002 cap; the slot is `budget_stopped` before attempt 2. `budget.stops` identifies the blocked replacement. `server-error/` records status 503, the same provider/schedule fields, and a successful replacement. Synthetic fixture prices are not live spend.

## Validation

The WO-12 tests cover the five acceptance arms and their rebuilds; direct and wrapped timeouts; 429 and representative 5xx statuses; nonreplacement of unrelated failures; breach precedence; upstream unavailability during a crowding pre-turn; and preservation of S13 context-limit classification. Existing WO-9/10/11 tests retain their earlier count assertions while accepting the two new denominators. The existing probe-cache test verifies a failed calibration is not cached and still names `HTTP 503`. Calibration failures remain gate failures and are not invocation replacements.

Baseline: 432 tests before WO-12. New test functions: `test_refusal_artifacts` (five cases), `test_http_retry_record` (seven), `test_unrelated_failures_never_replaced` (five), `test_breach_prevents_replacement`, `test_preturn_unavailability_and_breach_precedence` (two), and `test_context_limit_keeps_consumer_classification` — 21 new cases, 453 total.

Final validation: 453 tests passed in the full suite. The focused run of all 21 WO-12 cases plus the existing probe-cache regression passed 22 tests. After preserving the exact prior retry behavior for S13-classified 429 responses, the eight HTTP/context unit cases also passed. Ruff and `git diff --check` are clean.
