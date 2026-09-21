# WO-6 — checks report per cell; failure references name their row; `env` in cell identity

**Issued:** 2026-09-21, spec session. **For:** the implementation session. **Spec authority:** `30-checks.md` → "Report granularity" (ruled 2026-09-21); `10-harness.md` → Cells (`env` joined the identity tuple 2026-09-21); `20-manifest.md` → `cells`, the `env` row. Files win on conflict. The maintainer may challenge any item.

## Background

The uscode-mcp spec session authored eight loop cells as A/B arms: cells identical except for one server environment variable, restricted to one prompt. Two spec gaps surfaced, both the spec session's. First, `30-checks.md` said outcomes are "per run, per check", and the report does exactly that — `runs/2026-09-18-loop-smoke/checks-report.json` covers three cells with one outcome per check — so an arm that fails a check by design hides whether the control arm passed it, and a failure's record `index` is ambiguous across the run's many traces. Second, the identity tuple in `10-harness.md` omitted `env`, and the recorded `cell_id` (`loop/fake/model-1/fresh/full/scaffold:loop-scaffold@1/pin:fakeprov/bf16` in `runs/2026-09-18-wo3-examples/length/loop-cell/A/A1/meta.json`) carries neither knobs, setup, nor env, so two arms differing only in `env` record the same `cell_id`.

## Deliverables

1. **Per-cell check outcomes.** `checks-report.json` reports, for every check, one of the four outcomes per cell with that cell's matched count, beside the run-wide roll-up. Roll-up rule: worst over cells in the order error, fail, pass, vacuous; vacuous only when every cell is vacuous. Key names are yours; report them.
2. **A failure reference names its row:** cell, prompt id, record index, plus the pointer as today. If failure entries already carry the row, say so and show one — no failure entry exists in any run under `runs/`, so the spec session could not read the current shape.
3. **`cell_id` identifies the cell.** Every component of the `10-harness.md` tuple is reflected: knobs, setup, and env join what is there (a short digest of the canonical JSON is fine for the bulky ones, provided the full values stay in the row's meta as they are now). Secret-valued env entries contribute their key name only. Two manifest cells differing only in a non-secret `env` value must record different `cell_id`s; two differing only in `notes` must record the same one.
4. **The row records the cell's `env`.** Non-secret values verbatim, secret ones by key name, in the row's `meta.json`. If this is already recorded, name the key; the spec session found `env_overrides` (the driver's environment) and `server-config.json` but read no row whose cell declared `env`. On a key that `server.transport.env` also sets, the cell's value wins (`20-manifest.md`, ruled 2026-09-21); show a row where it did, or report what happens today.
5. **The loop probe cache is unaffected** — its tuple is (model, provider tag, scaffold version, driver version, knobs) and the server's environment is no part of consumer eligibility. State that arms differing only in `env` share one cached verdict, and show it.
6. Unit tests for 1–4; `ruff` clean; nothing under `documentation/`. Live spend: $0 — the fake upstream is the right instrument for claims about the harness's own report logic.

## Acceptance — observable artifacts

A fake-upstream run directory with two cells identical except for one `env` value, one check that the second cell's server violates on every record and the first satisfies: the `checks-report.json` showing pass for the first cell, fail for the second, fail at the roll-up, and a failure entry naming cell, prompt id, and index; both rows' `meta.json` showing different `cell_id`s and the recorded `env`; the probe-cache directory showing one entry. Then the test names, and the test count before and after.

## Out of scope

Any repeat mechanism — that is Q9 in `90-open-questions.md`, with the maintainer, and it changes the run-directory layout, so nothing here should anticipate it. Cell-scoped checks — ruled out in `30-checks.md`, with the reason.
