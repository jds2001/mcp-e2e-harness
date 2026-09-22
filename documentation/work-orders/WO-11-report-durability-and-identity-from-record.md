# WO-11 — failure references from the record; reporting never loses a run; `report --run-dir`

**Issued:** 2026-09-22, spec session. **For:** the implementation session. **Spec authority:** `90-open-questions.md` → DR-4; `30-checks.md` → report granularity (identity from the record, 2026-09-22); `10-harness.md` → run mechanics, "Reporting never loses a completed run". Files win on conflict. The maintainer may challenge any item. **Urgent in one respect:** item 3 is what makes the maintainer's dead run scoreable.

## Background

`uscode-mcp/runs/20260922T120550Z-e22-secondary` completed all 22 invocations and died in the checks stage: `checks.py:404`, `failure["repetition"] = int(parts[3][1:])`, on the path `e22-floor-both/C/C3/attempt-02` — a flagless run whose replaced slot (S18) put attempts at the fourth path segment. No run manifest or checks report was written. The spec session's WO-9 acceptance matrix declared no checks in any of its eleven runs, so the evaluator never saw an `attempt-NN` path; that is recorded against the spec session in DR-4 and `CLAUDE.md`.

## Deliverables

1. **Identity from the record.** The checks evaluator (and any other stage that names a row) takes `cell`, `prompt_id`, `repetition`, and `attempt` from the row's `meta.json`. No stage parses a row path for meaning; the path is recorded as `invocation` and used only to locate files. Failure references carry `attempt` whenever the row has one. Grep the source for path-splitting on row paths and report every site, changed or justified.
2. **Reporting durability.** The run manifest is written before the checks and measurements stages and rewritten as each completes; a stage that raises leaves a manifest naming the stage and the exception, a non-zero exit, and a terminal line naming the missing artifact. A checks evaluation that cannot run writes a report in which every check is `error` with the exception named.
3. **`mcp-e2e report --run-dir DIR`** rebuilds `run-manifest.json` and `checks-report.json` from the rows on disk, for a run that crashed in reporting or was killed. The rebuilt manifest carries `rebuilt: {at, harness_version, reason}` and records unrecoverable run-level facts as null with a note (the pre-run estimate; budget and replacement events, unless recoverable from rows). It refuses to overwrite an existing run manifest unless told to, and never touches a row. Run it on the maintainer's directory above and report the result: 22 rows, 2 replaced slots (`e22-floor-both/C/C3` at attempt 4, `e22-floor-field/C/C2` at attempt 2), 4 unmet attempts, and the checks report the manifest's checks produce.
4. **Regression matrix.** Fake-upstream runs, each with at least one check that fails in at least one row: flagless with a replacement; `--repeats 2` with a replacement; flagless with none. Each must produce a checks report whose failure references name cell, prompt id, repetition (null where flagless), and attempt.
5. Unit tests for 1–4; `ruff` clean; nothing under `documentation/`. Live spend: $0.

## Acceptance — observable artifacts

The three matrix runs' `checks-report.json` and the failure entries; a run with a deliberately failing checks stage (a bad regex is not enough — inject an exception) showing the manifest's stage record and the all-`error` report; the rebuilt manifest and checks report for the maintainer's run, with the `rebuilt` block and the null-with-note fields; the list of path-parsing sites from item 1. Test names and count before and after.

## Out of scope

Changing the attempt layout: it stands as WO-9 accepted it. Anything about why the pre-turns went unmet (PM-2).
