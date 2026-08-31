# Spec index — generic e2e harness for MCP consumers

This directory is the spec for a harness that drives a model consumer against an arbitrary MCP server and records what happened, so that humans and each server project's spec session can score consumer behavior against criteria pinned in advance. The harness ships from this repo as a library/CLI; the server-specific material (manifest, groundings, checks) is a **suite** living in the server's own repo.

## Files

- `10-harness.md` — the verification contract: division of labor, Layers 1 and 2, run mechanics, attribution, cells, grounding rules.
- `20-manifest.md` — the suite manifest schema: the entire interface between a suite and the harness, including generic MCP server registration.
- `30-checks.md` — the declarative Layer-1 check language and its evaluation semantics.
- `40-instruments.md` — the registry of pinned instrument content (crowding procedures, by name/version/content hash); crowded runs are scoreable only against a pin listed there.
- `90-open-questions.md` — open questions; route answers per `CLAUDE.md`.
- `examples/congressmcp/` — the ancestor suite from congressMCP, kept verbatim as the worked example. Not normative; see its README for what is server-specific and where it deviates from `20-manifest.md`.

## Conventions — these bind

Inherited from the ancestor spec because each was earned by a concrete failure there:

- **Measurement over assertion.** No result stated as fact without a citation to a measurement or an observation recorded in files. A mocked fixture is not evidence.
- **Preregister before a spec change**: expected result and the observation that would falsify it; record the outcome either way.
- **Non-zero denominator.** A claim over a set states the set's size; "all N passed" with N=0 is the vacuous outcome, reported as such (`30-checks.md` builds this into check semantics).
- **A scan that errors must never look like one that found nothing.** Error, fail, vacuous, and pass are four outcomes, not two (`30-checks.md`).
- **Check dead-defensive first.** Before specifying an edge-case contract, confirm the design can reach it.
- **A measurement of a property shares the failure class of its implementation.** An instrument built on the thing it measures cannot clear that thing; hence instrument defects block all other dispositions (`10-harness.md`).
- **Durable state lives in files here, never in conversation.** Rulings, preregistrations, open questions.
- **Commit each ruling as it is made.** This directory's git history is the decision record.

## Settled — do not reopen without new evidence

- **S1 (2026-08-30, maintainer)**: packaging is library/CLI; suites live in server repos. This spec defines the interface contract, not a home for suites.
- **S2 (2026-08-30, maintainer)**: Layer-1 checks are declarative data in the manifest, not code plugins. Pressure valve: an inexpressible contract becomes an open question and a language change, never ad-hoc harness code.
- **S3 (2026-08-30, maintainer)**: the congressMCP material stays as the worked example, verbatim, in `examples/congressmcp/`.
- **S4 (inherited, decided 2026-08-15)**: markdown is one line per paragraph, no hard wrapping — see `CLAUDE.md` for the full rule.
- **S5 (2026-08-30, spec session)**: no warm-by-accident — fresh working directory and fresh server process per cell invocation; carried-in state arrives only via explicit `setup`/`env` (`20-manifest.md`). Rationale: the ancestor's cold/warm cache axis showed state carryover is a cell parameter, so it must be declared, never inherited.
- **S7 (2026-08-31, spec session)**: attribution verification is **wire-first** — tool-surface absence is verified by observation (API-boundary capture per scored invocation where the driver permits interposition; discarded calibration probe per driver version otherwise), never by configuration assertion alone. Grounded in a measurement: a deny-flag left every builtin on the consumer's surface while blocking only invocation (`90-open-questions.md` → Q2, claude-code record). Contract text in `10-harness.md` → attribution.
- **S6 (2026-08-30, maintainer)**: crowding content is **harness-owned, versioned, and task-shaped** — never suite-authored, never trivia padding. A `crowded` cell selects a named harness crowding procedure; the suite attests the procedure is disjoint from the server's domain, and on collision selects a different harness procedure rather than writing its own. Rationale in `10-harness.md` → "Crowding is part of the instrument"; the padding-vs-task assumption is flagged as measurable in `90-open-questions.md` Q5.
