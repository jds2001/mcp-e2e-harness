# Writing an e2e suite for your MCP server

You are authoring a **suite**: the server-specific half of an end-to-end verification setup whose other half is the generic `mcp-e2e` harness. This document is the contract. It is a self-contained distillation of the harness spec (`documentation/` in the harness repo, which is normative if the two ever disagree; distilled 2026-08-31).

A suite is: one manifest file, the measurements grounding it, and the scored findings from runs. It lives in your server's repo, not the harness repo.

## The division of labor — binding

**The harness executes and records; it never scores. You pin criteria before running; you score after.**

- Every prompt carries `pass`/`fail` criteria written **before** the run. A criterion is never edited after seeing a result it would score. Criteria change only between runs, with the rationale committed.
- Scoring is done by a human and/or your spec session, recorded **beside** the raw run artifacts, never instead of them.
- The harness loads your manifest verbatim: no defaults merged in, and any unknown non-`_` key is a load error, not a warning. Keys beginning with `_` are commentary (ignored by the harness, but part of the manifest hash — commentary carries scoring guidance, so changing it is a new manifest version).
- Two runs are comparable only when their manifest hashes match, or your decision record explains the delta.

## Grounding — the rule that has invalidated the most prompts

Every prompt that asserts a property of your server's data must cite its grounding: a named, dated, reproducible measurement. Writing prompts from *plausibility* about your data is the single most common authoring failure — in the ancestor suite it silently invalidated three prompts (one asked about a document version that did not exist; one exercised nothing because the assumed data collision did not occur; one was confounded by a collision the author did not know about).

Every prompt carries a `sourcing` field:

- `measured` — grounded by a measurement you can name and reproduce; `grounding` is required and states what was measured, when, and how to reproduce it.
- `verbatim-original` — a real user's question, verbatim, authored by someone with **no knowledge of your server's internals**. Your spec session is disqualified from writing these by construction.
- `derived` — composed from hints or memory. Scored as **indicative only, never as a measurement**.

If your server fronts a live upstream, pin each grounding against a stated snapshot (edition, date, content hash — whatever your domain offers). When the upstream moves, every dependent grounding is stale: re-measure before scoring any run against it. The harness records the upstream identifiers each run actually hit so staleness is detectable after the fact.

## The manifest

One JSON file. Top level: `suite`, `server` (required); `fixtures`, `rubrics`, `checks` (optional); `cells`, `prompts` (required).

```json
{
  "suite": {"name": "my-server-e2e", "spec": "documentation/", "manifest_version": "1"},
  "server": {
    "name": "my_server",
    "transport": {"type": "stdio", "command": "python", "args": ["-m", "my_server"],
                  "env": {"MY_API_KEY": {"$secret": "MY_API_KEY"}}},
    "secret_keys": ["MY_API_KEY"]
  },
  "fixtures": {
    "DOC-123": {"content_hash": "…", "_note": "domain fields are yours; the harness treats fixtures as opaque"}
  },
  "cells": {
    "floor": {
      "driver": "claude-code", "model": "claude-sonnet-5", "knobs": {"thinking": "none"},
      "role": "floor", "context": "crowded",
      "crowding": {"procedure": "neutral-file-triage@2",
                   "collision_review": "2026-08-31, <name>: our server's domain is X, disjoint from office-notes triage"},
      "tool_surface": "full", "merge_gating": true, "groups": ["A"]
    },
    "isolation": {
      "driver": "claude-code", "model": "claude-sonnet-5", "knobs": {"thinking": "none"},
      "role": "isolation", "context": "fresh",
      "tool_surface": ["search_thing", "get_thing"], "merge_gating": true, "groups": ["A"]
    }
  },
  "checks": [
    {"id": "structured-errors",
     "description": "non-success outcomes carry a machine-readable kind",
     "applies_to": {"tool": "*", "when": [{"pointer": "/response/ok", "equals": false}]},
     "assert": {"present": ["/response/error/kind"]}}
  ],
  "prompts": [
    {"id": "A1", "group": "A", "title": "…",
     "prompt": "the text the consumer receives, verbatim",
     "variants": {"single_step": "pre-navigated form for weak-model cells"},
     "fixture": "DOC-123", "sourcing": "measured",
     "grounding": "Measured 2026-08-31: <what, against which snapshot, reproduce with <command>>",
     "pass": "…pinned before the run…", "fail": "…pinned before the run…",
     "watch": "non-criterion things to look at; editable after runs (it pins attention, not scoring)"}
  ]
}
```

Notes on the parts that bite:

- **Secrets**: values written as `{"$secret": "VAR"}` resolve from the harness environment at launch and never touch an artifact. Name your sensitive keys in `secret_keys` — a literal value under a named key is a load error. Trace/transcript/meta are scanned for resolved secret material; a hit halts the run.
- **`tool_surface`**: `"full"` or an explicit tool-name list. Attribution-dependent conclusions ("that citation is absent from the trace, therefore fabricated") are valid **only** in list-surface cells, where trace scope equals tool surface — the harness enforces the list at the MCP proxy and verifies it on the model-API wire.
- **Fresh by default**: every invocation gets a fresh neutral working directory and a fresh server process. State you want present must arrive explicitly, via `setup` (an ordered list of `{tool, args}` calls the harness makes directly against your server before the prompt — never via a model turn) or `env`. There is no warm-by-accident.
- **Crowded cells**: you *select* a harness-owned crowding procedure by pinned name and version; you never author crowding content (an internals-aware author would be writing part of the instrument they are scored against). `collision_review` is your dated attestation that the procedure's content is disjoint from your server's domain; if it collides, select a different harness procedure. Currently pinned: `neutral-file-triage@2` (a mundane office notes-triage task — collides with note-keeping, filing, and office-facilities domains).
- **Knobs are driver-native and verbatim**: write `thinking` for Claude drivers, another vendor's terms for its cells; never translate between vendors' scales. A cross-vendor cell never silently substitutes for a gating cell of your primary vendor.

## Checks (Layer 1) — mechanical trace conformance

Declarative rules the harness evaluates over every trace record: this is where your server's *published tool contract* gets asserted (error envelopes, truncation markers, disambiguation totals, provenance fields — whatever your server promises). Selectors match records by tool name and pointer predicates; assertions are `present` / `absent` / `matches` / `not_matches` / `enum` / `forbid_pattern`, combined with `all_of` / `any_of` / `not`. Pointers are RFC 6901, with `~each` to quantify over arrays.

Each check reports one of **four** outcomes, and the distinctions are the point:

- `pass` — matched at least one record, assertion held on all.
- `fail` — assertion failed; offending record indices named.
- `vacuous` — the selector matched **zero** records. Never folded into pass: it means either a dead rule or a run that never exercised the surface, and both deserve eyes.
- `error` — the check itself could not run. Never conflated with fail or vacuous: a scan that errors must not look like one that found nothing.

## Scoring (Layer 2) and classifying failures

Layer 2 is yours: did the consumer, given honest tool responses, produce an honest answer — caveats propagated, absence reported as absence, no fabricated citations? Classify every failure **before** filing it:

- **Consumer-behavior finding** — the tool told the truth and the model dropped it. A response-shape/prominence question for your server's spec.
- **Tool defect** — the trace shows your server violating its own contract.
- **Instrument defect** — the harness, driver, or cell configuration could not have captured the signal. Fix the instrument first; **no finding of any other class is recorded from a defective instrument**. A zero-trace run is BROKEN, never an abstention.

Score from the artifacts, not from summaries — demand the trace, the before/after sets, the failing input. Agreement with a summary proves nothing; disagreement is the signal.

## Runs

`mcp-e2e validate --manifest …` checks the manifest; `mcp-e2e run --manifest …` executes cells. Per cell/prompt the run directory holds: `trace.jsonl` (every tool call, verbatim), `answer.txt`, `meta.json` (knobs, manifest hash, timing, tool-call list, attribution record, api-surface digest, crowding hash), `available-tools.json` (advertised vs exposed), `api-surface.jsonl` (the actual tool arrays sent to the model), and the checks report.

Gitignore the run directory — bytes are disposable. What you commit is the manifest, its grounding measurements, and your scored findings, each finding citing the run artifacts it was scored from. Commit each ruling as it is made; the git history is your decision record.

## Recommended starting grid

Two gating cells; grow only when a question needs a new cell.

| role | what it isolates | shape |
|---|---|---|
| floor | the merge-relevant result: a distracted mid-task consumer | mid-tier model, no extended thinking, crowded, full surface |
| ceiling | whether the data supports a correct answer at all | top model, high thinking, fresh, question first |
| capability-floor (optional) | weakest consumers, without conflating chaining limits with tool defects | small model, single-step variants only |
| isolation (optional, needed for attribution claims) | tool selection noise vs tool design defects | surface = exactly the tools under test |
