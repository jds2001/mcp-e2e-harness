# E2E harness — verification contract

The harness (implementation-owned code) drives this server through a real consumer model and records what happened; humans and the spec session score the results against criteria pinned in advance. Ruled in R10; the format ancestor is congressMCP's §17 manifest (verbatim in commit 198cd0c).

## Division of labor — binding

The harness **executes and records; it never scores.** Every prompt carries its `pass`/`fail` criteria pinned before the run — preregistration-of-scoring — and a criterion is never edited after seeing a result it would score. Criteria change only between runs, with the rationale committed here. Scoring is done by a human and/or the spec session, recorded beside (never instead of) the raw artifacts.

The prompt manifest is normative at `documentation/e2e-manifest.json`. The harness loads that file verbatim — no implementation-side copy, no transcription. Manifest edits are spec-session commits.

## Layer 1 — mechanical trace conformance (harness-checked)

These are assertions a program can check against every R8 trace line, with no judgment involved. Each maps to a contract in `40-tools.md`:

- **Structured error envelope**: every non-success outcome in a response is a structured object with a machine-readable kind (zero-hit / upstream-failure / out-of-scope / disambiguation / redirect), never prose-only. Zero hits echo the upstream query; upstream failures carry status and body.
- **Outcome distinctness**: no trace line shows an upstream failure or out-of-scope request presented in a zero-hit shape, or vice versa.
- **Normalization disclosure**: whenever the server stripped a subsection or a trailing "note", or redirected an appendix citation, the response says so — the consumer must be able to diagnose what the server did with its input.
- **Truncation markers**: any windowed text payload states total length, window bounds, and continuation; no window without markers.
- **Disambiguation totals**: every candidate list carries the true `count`, with capping stated.
- **Provenance completeness**: every text payload carries packageId/granuleId, edition year, `currentthrough` (or its explicit parse-failure disclosure), `lastModified`, PDF link.
- **Secret hygiene**: no key material anywhere in any trace line.

Rate-limit behavior is excluded (untriggerable live at 36,000/hr, O13) and stays unit-test territory.

## Layer 2 — consumer-behavior findings (human/spec-scored)

What the pinned `pass`/`fail` criteria in the manifest govern: did the consumer, given honest tool responses, produce an honest answer — completeness caveats propagated, currency disclosed, absence reported as absence, no fabricated citations. A failure here is classified before it is filed: **consumer-behavior finding** (the tool told the truth and the model dropped it — a response-shape/prominence question), **tool defect** (the trace shows the server violating a `40-tools.md` contract), or **instrument defect** (the harness or cell configuration could not have captured the signal — fix the instrument before any disposition). A zero-trace run of a single-prompt cell reads as BROKEN, never as abstention.

## Run mechanics

Live consumer per run; no replay tier (R10). Runs are minimal and manual: high-risk changes and pre-release.

Driver: Claude Code headless. The cell's environment must make answers attributable to exactly two sources — the model's priors and this server's tools: web/network tools disabled and verified absent from the trace's available-tool surface; no memory; a **neutral working directory** outside this repo, so no CLAUDE.md, spec file, or repo context leaks into the consumer (a consumer that learns it is being tested, or what the internals look like, is a different consumer — R10's no-disclosure principle). Model knobs are recorded in the driver's native vocabulary, verbatim, never translated across vendors.

Each run records, per cell: manifest content hash, cell id and full knob set, the R8 trace directory contents, the consumer transcript, and a meta record (wall clock, tool-call count, editions/granuleIds actually hit). Runs land in `runs/` (gitignored — bytes are disposable, the scored findings are what gets committed, here).

## Cells

Two gating cells to start; the grid grows only when a question needs a new cell.

| cell | driver | model | knobs | context | gating |
|---|---|---|---|---|---|
| floor | claude-code headless | claude-sonnet-5 | thinking: none | crowded: asked mid-task, other tools registered | yes |
| ceiling | claude-code headless | claude-opus-5 | thinking: high | fresh, question first | yes |

The floor is the merge-relevant result (what a distracted mid-task consumer does); the ceiling separates "the data can't support a correct answer" from "the floor model dropped it". Cross-vendor cells (Codex CLI) are deferred behind E11: if E11 confirms ChatGPT-auth Codex cannot disable web fetching, the harness refuses to run cross-vendor cells without API-key auth — attribution is the whole point of the cell.

## Grounding rules for the manifest

Adopted whole from congressMCP, where writing prompts from plausibility instead of the record invalidated three of them (A3, B3, E3 in the §17 manifest):

- Every prompt asserting a document or API property cites its grounding: an O-observation in `90-observations.md`, or a named, dated, reproducible measurement.
- **Live-API adaptation** (this server has no cached corpus, R1): groundings are pinned against a stated edition — currently the 2024 edition, `currentthrough` 2025-01-06 (O15). When GovInfo publishes a newer annual edition, every grounding that depends on current-edition behavior is stale: re-measure before scoring any run against it. The harness's per-cell meta records the editions actually hit so staleness is detectable after the fact, not assumed away.
- Group F (real-user prompts) must be verbatim questions from real research sessions, authored by no one who knows the internals — the spec session is disqualified by construction. Until the maintainer supplies 8–12 originals (Q8), Group F is empty and nothing is scored as a Group F measurement.
