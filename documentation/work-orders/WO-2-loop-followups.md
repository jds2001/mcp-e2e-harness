# WO-2 — Loop driver follow-ups from the WO-1 verification

**Issued:** 2026-09-18, spec session. **For:** the implementation session. **Spec authority:** `50-drivers.md` → loop (as amended 2026-09-18), `10-harness.md` → Layer 2 (S13) and recording contract (timing), `20-manifest.md` → `budget_usd`, `00-INDEX.md` S11/S13. Files win on conflict; report conflicts rather than resolving them.

## Context

WO-1 is accepted: the loop driver is verified for pinned cells and `loop-scaffold@1` is pinned in `40-instruments.md`. Every finding in the WO-1 report §5 received a ruling; this work order carries the rulings that need code. All are small. No live spend is required; item 5 is parked (see below).

## Deliverables

1. **S13 — answerless consumer outcomes.** A turn that ends because an endpoint refused a request that the consumer's own tool calls grew past the endpoint's listed `context_length` (rule: 4xx on a request whose recorded size exceeds the served endpoint's context length, after ≥1 tool call in the turn), or because the scaffold's step cap ended the loop, is a **consumer outcome**, not a harness failure: `harness_failure` null; a distinct top-level outcome field (suggested `consumer_limit: {"cause": "context_length" | "step_cap", …}` carrying the endpoint's message or the cap); the row counted in the run report beside pass and fail, never under broken; `answer.txt` empty and said so. Any other upstream error keeps today's instrument classification. Re-run `runs/2026-09-18-q8-data-policy/deny-r05`'s shape through the fake upstream in a test; do not re-run it live.
2. **Product-driver family mark.** Every product-driver row carries `driver_family: "product"` wherever loop rows carry `driver_family: "loop"` (`meta.json`, `run-manifest.json`, `checks-report.json.cells`), so S11's family legibility holds in every artifact without a reader inferring it from the driver id.
3. **`duration_ms` per wire line.** From request arrival to end of response, on every line the recorder writes, for all drivers; null with a note when the response never completed. `at` stays request arrival. Amend the allowlist test's key-set assertion accordingly — this is a timing scalar, not a response-body extraction, so it is not an allowlist entry.
4. **Loop-specific cost basis in the pre-run estimate.** For loop cells, estimate input tokens from the measured loop basis (`50-drivers.md` → loop, loop-specific cost basis: crowded ≈ 46.6k prompt tokens per invocation, fresh ≈ 10k) rather than the claude-code byte basis; keep the estimate labeled, keep reasoning tokens named as the unbounded term, and keep the product-driver basis for product cells. State which basis was used in `pre_run`.
5. **Parked (2026-09-18, maintainer):** the Mistral re-probe needs a different day from the 429s, and this work order runs the same day; it is recorded under `90-open-questions.md` → "Parked measurements" and is not part of WO-2.
6. **Pin-identity wording in artifacts.** Where a pinned row reports its pin, the artifact makes the verified part distinguishable from the asserted part (for example `provider_pin: "deepinfra/bf16"`, `provider_verified: "DeepInfra"`, `quantization_asserted: "bf16"`), per the requirement 4 amendment. No new verification is possible at this layer; the deliverable is that no artifact can be read as having verified the quantization.
7. **Tests** for 1–4 and 6; `ruff` clean; nothing under `documentation/`.

## Acceptance

Artifacts, not the summary: one real or fake-upstream example line per new key (the `consumer_limit` row, a product row's family mark, a `duration_ms` line, the `pre_run` basis statement, the pin-identity fields), the test names covering each item, and total live spend (expected $0).

## Out of scope

Anything in `40-instruments.md`; a scaffold v2 (the two notes in the `loop-scaffold@1` pin record are parked, not ordered); direct-vendor endpoints; the http transport.
