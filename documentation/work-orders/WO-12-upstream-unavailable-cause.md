# WO-12 — exhausted upstream refusals carry a cause; slot status follows the row

**Issued:** 2026-09-23, spec session. **For:** the implementation session. **Spec authority:** `10-harness.md` → Layer 2, "An upstream refusal that exhausts the consumer's retries" (2026-09-23). Files win on conflict. Small; Q12 (replacement) is deliberately not in it.

## Background

In `uscode-mcp/runs/20260923T021835Z-e25`, five rows (`e25-akashml-t1` A2/r03, C4/r05, C4/r08, D3/r03; `e25-deepinfra-t1` C4/r08) ended on three consecutive HTTP 429s. Each records `harness_failure: "runner exited 2: …"` with the cause inside the stderr tail, `loop.retries: null`, `consumer_limit: null`, and slot status `scored`. A reader who does not open the stderr cannot tell the row from a null final except by the absence of a cause.

## Deliverables

1. **Cause-first failure.** When the loop consumer gives up on an upstream refusal, the row's `harness_failure` leads with `upstream_unavailable`, and the row carries `upstream_unavailable: {http_status, provider, attempts, retry_schedule_s, step, tool_calls_before}`; `loop.retries` is recorded. Other upstream statuses the loop gives up on (5xx, timeouts) take the same shape with their status; a 4xx that S13 classifies as a context limit is unchanged.
2. **Slot status from disposition.** A slot whose selected row is a harness failure reads `failed`; `scored` only when a scoreable row exists. Apply to `report --run-dir` rebuilds too.
3. **Terminal line** names the cause and the provider, not the exit code.
4. Unit tests for 1–3 via the fake upstream; `ruff` clean; nothing under `documentation/`. Live spend: $0. Do not add replacement; that is Q12.

## Acceptance — observable artifacts

A fake-upstream run with `--repeats 2` where the fake answers 429 three times on one repetition's second step: that row's `meta.json` block and failure text, `loop.retries`, the slot reading `failed`, `failures: 1`, `answers` reading `invocations: 2, answered: 1`; a rebuild of the same directory by `report --run-dir` agreeing on the slot status.

## Out of scope

Replacement of the slot (Q12). The retry schedule itself (Q12 option c).
