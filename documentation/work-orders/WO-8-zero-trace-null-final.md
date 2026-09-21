# WO-8 — the zero-trace exemption for null finals: show it, and bound it

**Issued:** 2026-09-21, spec session. **For:** the implementation session. **Spec authority:** `10-harness.md` → Layer 2, the ruling beneath the WO-7 acceptance record; `10-harness.md` → Layer 1, instrument liveness. Files win on conflict. Small.

## Background

WO-7's report says an observed call-free null final is exempt from the zero-trace liveness failure. That was the implementation's decision; the spec has now ruled on it: the exemption holds for **loop rows only**, and only when the row's own artifacts show the spawn check passed, the wire surface equalled the manifest surface on every request, and the final response is on a wire line as a 2xx with a `finish_reason`. A product-driver row with zero trace records and an empty answer stays BROKEN. No artifact of the exempted case exists, so the spec session could not read how the exemption is conditioned.

## Deliverables

1. State the conditions under which the exemption applies today, and conform them to the ruling if they differ. In particular: report what a product-driver row with exit 0, empty output, and zero trace records is recorded as. Under the ruling it is BROKEN.
2. The exempted row says why it was exempted: the three observations, recorded in the row's meta beside the outcome, so a scorer does not have to re-derive them.
3. Unit tests for the loop exemption, for each of the three conditions failing (each must leave the row BROKEN), and for the product-driver case; `ruff` clean; nothing under `documentation/`. Live spend: $0.

## Acceptance — observable artifacts

Three fake-upstream rows: a loop row whose first and only response is null content with a tool call written into its reasoning (zero trace records) — recorded as `null_final_content`, `harness_failure: null`, with the three observations; the same row with a wire-surface mismatch injected — BROKEN; a fake product-driver row with empty output and zero trace records — BROKEN. The run manifests' counts for each.

## Out of scope

Any retry (S16 corollary). Renaming `consumer_limit`.
