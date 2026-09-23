# WO-12 — exhausted upstream refusals carry a cause; slot status follows the row

**Issued:** 2026-09-23, spec session; **amended the same day, before any report, to add part B** on the maintainer's Q12 answer (ruling S20). **For:** the implementation session. **Spec authority:** `10-harness.md` → Layer 2, "An upstream refusal that exhausts the consumer's retries" (2026-09-23). Files win on conflict. Part A is the cause; part B is the replacement.

## Background

In `uscode-mcp/runs/20260923T021835Z-e25`, five rows (`e25-akashml-t1` A2/r03, C4/r05, C4/r08, D3/r03; `e25-deepinfra-t1` C4/r08) ended on three consecutive HTTP 429s. Each records `harness_failure: "runner exited 2: …"` with the cause inside the stderr tail, `loop.retries: null`, `consumer_limit: null`, and slot status `scored`. A reader who does not open the stderr cannot tell the row from a null final except by the absence of a cause.

## Deliverables

1. **Cause-first failure.** When the loop consumer gives up on an upstream refusal, the row's `harness_failure` leads with `upstream_unavailable`, and the row carries `upstream_unavailable: {http_status, provider, attempts, retry_schedule_s, step, tool_calls_before}`; `loop.retries` is recorded. Other upstream statuses the loop gives up on (5xx, timeouts) take the same shape with their status; a 4xx that S13 classifies as a context limit is unchanged.
2. **Slot status from disposition.** A slot whose selected row is a harness failure reads `failed`; `scored` only when a scoreable row exists. Apply to `report --run-dir` rebuilds too.
3. **Terminal line** names the cause and the provider, not the exit code.
4. Unit tests for 1–3 via the fake upstream; `ruff` clean; nothing under `documentation/`. Live spend: $0.

## Part B — replacement (S20)

5. An invocation that ends `upstream_unavailable` is replaced under S18's mechanism unchanged: whole fresh invocation, `--precondition-retries` as the counter and its default, budgets applied, every attempt kept under `attempt-NN/`, the attempt record carrying the cause, the terminal announcing `REPLACEMENT: upstream_unavailable: <slot> attempt k/4, whole fresh invocation`. Nothing else that is a harness failure is replaced; say in the report which failure classes you route to replacement and which not.
6. Per-slot and per-(cell, prompt) counts distinguish attempts lost to an unmet precondition from attempts lost to an unavailable upstream (`attempts_unmet`, `attempts_unavailable`, or equivalent), so the unmet rate stays a consumer figure.
7. The in-loop retry schedule is yours to set; state it in the report and record it in the row (`retry_schedule_s`). A slot that exhausts its replacements on refusals reads `exhausted` with the cause.
8. Unit tests for 5–7; `ruff` clean; nothing under `documentation/`. Live spend: $0.

## Acceptance — observable artifacts

Part B: a fake-upstream run, `--repeats 2`, where the fake refuses one repetition's every request with 429 on attempt 1 and serves attempt 2 normally: attempt 1 under the slot with its cause block, attempt 2 in `attempt-02/` scored, the slot `scored` with `attempts 2`, the counts showing one unavailable attempt and zero unmet, `failures: 0`, the terminal's replacement line; and a run where the fake refuses all four attempts: slot `exhausted` with cause, `failures` counting the cell as S18 does for exhaustion.

Part A: 
A fake-upstream run with `--repeats 2` where the fake answers 429 three times on one repetition's second step: that row's `meta.json` block and failure text, `loop.retries`, the slot reading `failed`, `failures: 1`, `answers` reading `invocations: 2, answered: 1`; a rebuild of the same directory by `report --run-dir` agreeing on the slot status.

## Status

**Accepted 2026-09-23**, both parts, from `runs/2026-09-23-wo12/` with the report read second, rebuilds re-run by the spec session; commit 01cdd33. Record in `10-harness.md` → Layer 2.

## Out of scope

BYOK or direct-endpoint changes (`50-drivers.md` → loop, "Leaving the shared pool"; not ordered).
