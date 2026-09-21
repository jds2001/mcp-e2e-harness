# WO-6 — checks report per cell; failure references name their row; `env` in cell identity; `--repeats`

**Drafted** 2026-09-21, spec session; **amended the same day before issue** to add part B on the maintainer's Q9 answer. **For:** the implementation session. **Spec authority:** `30-checks.md` → "Report granularity" (ruled 2026-09-21); `10-harness.md` → Cells (`env` joined the identity tuple 2026-09-21); `20-manifest.md` → `cells`, the `env` row; for part B, `10-harness.md` → run mechanics, "Repeats are a run parameter" (ruling S15) and `50-drivers.md` → loop, requirement 11. Files win on conflict. The maintainer may challenge any item.

## Background

The uscode-mcp spec session authored eight loop cells as A/B arms: cells identical except for one server environment variable, restricted to one prompt. Two spec gaps surfaced, both the spec session's. First, `30-checks.md` said outcomes are "per run, per check", and the report does exactly that — `runs/2026-09-18-loop-smoke/checks-report.json` covers three cells with one outcome per check — so an arm that fails a check by design hides whether the control arm passed it, and a failure's record `index` is ambiguous across the run's many traces. Second, the identity tuple in `10-harness.md` omitted `env`, and the recorded `cell_id` (`loop/fake/model-1/fresh/full/scaffold:loop-scaffold@1/pin:fakeprov/bf16` in `runs/2026-09-18-wo3-examples/length/loop-cell/A/A1/meta.json`) carries neither knobs, setup, nor env, so two arms differing only in `env` record the same `cell_id`.

## Part A — deliverables

1. **Per-cell check outcomes.** `checks-report.json` reports, for every check, one of the four outcomes per cell with that cell's matched count, beside the run-wide roll-up. Roll-up rule: worst over cells in the order error, fail, pass, vacuous; vacuous only when every cell is vacuous. Key names are yours; report them.
2. **A failure reference names its row:** cell, prompt id, repetition (when the run has them — part B), record index, plus the pointer as today. If failure entries already carry the row, say so and show one — no failure entry exists in any run under `runs/`, so the spec session could not read the current shape.
3. **`cell_id` identifies the cell.** Every component of the `10-harness.md` tuple is reflected: knobs, setup, and env join what is there (a short digest of the canonical JSON is fine for the bulky ones, provided the full values stay in the row's meta as they are now). Secret-valued env entries contribute their key name only. Two manifest cells differing only in a non-secret `env` value must record different `cell_id`s; two differing only in `notes` must record the same one.
4. **The row records the cell's `env`.** Non-secret values verbatim, secret ones by key name, in the row's `meta.json`. If this is already recorded, name the key; the spec session found `env_overrides` (the driver's environment) and `server-config.json` but read no row whose cell declared `env`. On a key that `server.transport.env` also sets, the cell's value wins (`20-manifest.md`, ruled 2026-09-21); show a row where it did, or report what happens today.
5. **The loop probe cache is unaffected** — its tuple is (model, provider tag, scaffold version, driver version, knobs) and the server's environment is no part of consumer eligibility. State that arms differing only in `env` share one cached verdict, and show it.
6. Unit tests for 1–4; `ruff` clean; nothing under `documentation/`. Live spend: $0 — the fake upstream is the right instrument for claims about the harness's own report logic.

## Part A — acceptance, observable artifacts

A fake-upstream run directory with two cells identical except for one `env` value, one check that the second cell's server violates on every record and the first satisfies: the `checks-report.json` showing pass for the first cell, fail for the second, fail at the roll-up, and a failure entry naming cell, prompt id, and index; both rows' `meta.json` showing different `cell_id`s and the recorded `env`; the probe-cache directory showing one entry. Then the test names, and the test count before and after.

## Part B — `mcp-e2e run --repeats N`

The uscode-mcp experiments need ten invocations per arm, and the runner invokes each (cell, prompt) pair once; the Q8 repeats were ten separate runs. The maintainer chose a run flag over a manifest field (S15). The contract is `10-harness.md` → run mechanics; what follows is its work-order form, and the file wins on any difference.

7. **The flag.** `--repeats N`, integer ≥ 1; anything else is a usage error before any invocation. Absent means one invocation per pair with today's layout, byte-for-byte: a run without the flag must produce the same paths as before, with no artifact key removed or changed in meaning; the only additions are the ones this work order itself orders (the two null fields in item 10, `answered` in item 12, and part A's row `env` and checks-report fields). *Corrected 2026-09-21 after the implementation's report: as first written this said "plus the two null fields" only, contradicting part A and item 12.*
8. **Every repetition is a whole invocation:** fresh neutral working directory, fresh server process, `setup` re-run and recorded, crowding pre-turn re-run. The loop calibration probe stays once per cell per run.
9. **Repetition is the outer loop:** pass 1 over the whole selected grid, then pass 2, and so on. S9 progress output names the repetition (`r03/10`) beside the cell and prompt.
10. **Layout and marks.** With the flag given, including `--repeats 1`, a row lives at `<cell>/<group>/<prompt id>/rNN/` (two digits, 1-based, wider only if N needs it). Every row's `meta.json` carries `repetition` (null without the flag); `run-manifest.json` → `selection.repeats` (null without the flag); the results list identifies each row's repetition.
11. **Estimate and budget.** The pre-run invocation count and estimate multiply by N, and `--dry-run` shows the multiplied count. Cap semantics unchanged; a cell-cap stop skips that cell's remaining prompts and repetitions, and `budget.stops` says how many were skipped.
12. **`answers`, per prompt within a cell:** `invocations` (rows attempted), `answered` (rows with a non-empty answer), `distinct` and `digests` over the answered rows only. Product cells included, as now.
13. **Checks and measurements** run over every repetition's rows; per-cell matched counts pool across repetitions; row measurements are per row and need nothing new.
14. Unit tests for 7–13; `ruff` clean; nothing under `documentation/`. Live spend: $0.

## Part B — acceptance, observable artifacts

Preregistered in `10-harness.md`. A fake-upstream run, two cells (the part A pair will do) × one prompt × `--repeats 3`, with the fake returning two different answers across the three repetitions of one cell: the directory listing showing `r01`–`r03` under each pair; the runner's progress output showing the interleaved order (both cells at r01 before either at r02); each repetition's `proxy-meta.json` showing its own server spawn and each `meta.json` its own `setup` record and `repetition`; `answers` for that prompt reading `invocations: 3, answered: 3, distinct: 2`, agreeing with the three `answer_sha256_16` values; one probe-cache entry. A second run with one repetition forced answerless (the S13 replay shape from WO-2 will do) showing `invocations: 3, answered: 2`. A third run without the flag, diffed by path list and key union against a pre-change run of the same manifest: identical paths, no key removed, additions only as ordered above. Falsifiers, any of which is reported and not patched around: a reused server process across repetitions, a missing setup record, a count that disagrees with the digests, or a layout change in the flagless run.

## Out of scope

A per-cell repeat count in the manifest (rejected, S15). Any cross-run aggregation tool — rows spread over several run directories remain the scorer's to aggregate by `answer_sha256_16`. Cell-scoped checks — ruled out in `30-checks.md`, with the reason.

## Status

**Accepted 2026-09-21**, both parts, from `runs/2026-09-21-wo6/` with the report read second; commit 5d0364a. Records: `30-checks.md` → report granularity (part A) and `10-harness.md` → run mechanics (part B).
