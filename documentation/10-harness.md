# The verification contract

The harness drives a real consumer model against an MCP server under test, through a real driver (an agent harness such as Claude Code headless), and records what happened. Humans and the suite's spec session score the results against criteria pinned in advance. The harness is generic; everything server-specific arrives in a **suite** — a manifest plus its groundings and checks, owned by the server project's own repository (ruling S1, `00-INDEX.md`).

This file is the contract for the harness itself. The interface a suite must satisfy is `20-manifest.md`; the Layer-1 check language is `30-checks.md`. The ancestor of this design is congressMCP's §17 harness, preserved verbatim in `examples/congressmcp/` — historical claims below (the A3/B3/E3 invalidations, the zero-trace incident) refer to that record.

## Division of labor — binding

The harness **executes and records; it never scores.** Every prompt carries its `pass`/`fail` criteria pinned before the run — preregistration-of-scoring — and a criterion is never edited after seeing a result it would score. Criteria change only between runs, with the rationale committed in the suite repo. Scoring is done by a human and/or the suite's spec session, recorded beside (never instead of) the raw artifacts.

The manifest is normative and lives in the suite repo. The harness loads that file verbatim — no harness-side copy, no transcription, no defaults silently merged in. Manifest edits are suite-repo spec commits.

Corollary for this repo: the harness implementation contains **no knowledge of any particular MCP server**. Any server-specific constant, tool name, cache path, fixture, or check found in the harness code is a defect against this contract. The implementation session should treat de-specializing the seeded code as conformance work against this file and `20-manifest.md`.

## Layer 1 — mechanical trace conformance (harness-evaluated)

Assertions a program can check against every trace record, with no judgment involved. The suite supplies them as declarative rules in the manifest (`checks`, ruling S2); the harness evaluates them and reports per-check results beside the run. What those rules assert is the suite's business — they encode the server's own tool contracts (the congressMCP suite asserted error-envelope structure, truncation markers, provenance completeness, and so on against its `40-tools.md`; see the example).

Exactly two Layer-1 behaviors are built into the harness rather than supplied by the suite, because they protect the instrument rather than the server contract:

- **Secret hygiene**: no configured secret material (anything the server registration marked as secret, plus values of env vars the suite names as sensitive) appears in any trace record, transcript, or meta artifact. This is not waivable by a suite.
- **Instrument liveness**: a run of a cell that produced zero trace records is reported as BROKEN, never as an abstention, a pass, or a vacuous result. A single-prompt cell with an empty trace is the canonical case.

## Layer 2 — consumer-behavior findings (human/spec-scored)

What the pinned `pass`/`fail` criteria govern: did the consumer, given honest tool responses, produce an honest answer — completeness caveats propagated, absence reported as absence, no fabricated citations, provenance carried through. A failure is classified before it is filed:

- **Consumer-behavior finding** — the tool told the truth and the model dropped it; a response-shape or prominence question for the server's spec.
- **Tool defect** — the trace shows the server violating its own published contract.
- **Instrument defect** — the harness, driver, or cell configuration could not have captured the signal. Fix the instrument before any disposition; no finding of any other class is recorded from a defective instrument.

## Run mechanics

Live consumer per run; no replay tier. Runs are minimal and manual: high-risk changes and pre-release, at the suite owner's discretion.

**Attribution — the two-source rule.** The cell's environment must make answers attributable to exactly two sources: the model's priors and the server under test. Binding consequences:

- Web/network tools disabled, and **verified absent from the trace's recorded available-tool surface** — disabling is a claim, the tool listing is the evidence.
- No persistent memory, and a neutral working directory outside both the harness repo and the suite repo, so no CLAUDE.md, spec file, or repo context leaks into the consumer. A consumer that learns it is being tested, or what the internals look like, is a different consumer (the no-disclosure principle).
- A driver that cannot verifiably satisfy this (e.g. cannot disable its own web fetching) is refused for attribution-dependent cells; the harness fails the cell as instrument-broken rather than running it unattributable.

**Knobs are recorded in the driver's native vocabulary, verbatim, never translated across vendors.** A Claude cell carries `thinking`; another vendor's cell carries that vendor's terms; no mapping between vendors' scales is recorded or implied, because none is defensible.

**Crowding is part of the instrument** (ruling S6). The `crowded` context condition is produced by a harness-owned, versioned crowding procedure: a coherent, mundane, domain-neutral task the consumer is genuinely mid-way through when the prompt lands, with other tools registered. Suites select a procedure by name; they never author crowding content, because the crowding sits in the consumer's context during the scored turn and a suite's spec session — which knows the internals — would be writing part of the instrument it is scored against (the same disqualification-by-construction that bars it from authoring real-user prompts). Trivia padding is rejected as the default on the design assumption that padding yields tokens without a competing goal and therefore under-crowds; that assumption is empirical and its settling measurement is recorded in `90-open-questions.md` Q5, not asserted here. No fixed corpus is inert for every server, so suite authorship includes a **domain-collision attestation** for the selected procedure; on collision the suite selects a different harness procedure.

**Setup actions, never model turns.** A cell may declare setup actions (see `20-manifest.md`): direct calls the harness makes against the server before the prompt — pre-warming a cache, seeding state. These are performed server-side by the harness, never by a model turn, and exactly what was done is recorded in the cell's meta artifact.

**Recorded per cell, per run:** manifest content hash; cell id and the full knob set; the complete tool-call trace; the consumer transcript; the recorded available-tool surface; setup actions performed; for crowded cells, the crowding procedure's name, version, and content hash; and a meta record (wall clock, tool-call count, and the upstream identifiers actually hit, so staleness of groundings is detectable after the fact rather than assumed away). The minimum trace-record fields are pinned in `30-checks.md`, since checks evaluate against them.

Run bytes land in a gitignored `runs/` directory — bytes are disposable; the scored findings are what gets committed, in the suite repo.

## Cells

A cell is identified by (driver, model, knobs, tool surface, context condition, setup, prompt selection). The grid grows only when a question needs a new cell. Four roles recur and are recommended as the starting grid, but the roster is the suite's choice:

| role | what it isolates | typical shape |
|---|---|---|
| floor | the merge-relevant result: what a distracted mid-task consumer does | mid-tier model, no extended thinking, crowded context, full tool surface |
| ceiling | whether the data supports a correct answer at all | top model, high thinking, fresh context, question first |
| capability-floor | weakest-consumer behavior without conflating chaining limits with tool defects | small model, single-step prompt variants only |
| isolation | tool-selection noise vs tool-design defects | surface restricted to exactly the tools under test |

Attribution-dependent conclusions (e.g. "citation absent from the trace") hold **only** in cells where the trace scope equals the tool surface — the isolation role exists for that.

Cross-vendor cells never silently substitute for a gating cell of the primary vendor: a cross-vendor cell carries a distinct role and is non-gating unless the suite explicitly rules otherwise.

## Grounding rules for manifests

Adopted whole from the ancestor suite, where writing prompts from plausibility instead of the record invalidated three of them (A3, B3, E3 — see the example's annotations):

- Every prompt asserting a property of the server's data cites its grounding: a named, dated, reproducible measurement, or an observation recorded in the suite's spec.
- **Live-upstream staleness**: when the server fronts a live upstream, groundings are pinned against a stated snapshot (edition, date, content hash — whatever the domain offers). When the upstream moves, every grounding that depends on it is stale: re-measure before scoring any run against it. The per-cell meta's record of upstream identifiers actually hit is what makes staleness detectable.
- **Real-user prompts** (the ancestor's Group F): prompts meant to represent real use must be verbatim questions from real sessions, authored by no one who knows the server's internals — the suite's spec session is disqualified by construction. Every prompt carries a `sourcing` field; anything not `verbatim-original` is scored as indicative only, never as a measurement. A prompt with null `pass`/`fail` must name a pinned rubric instead (`20-manifest.md`).
