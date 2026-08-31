# mcp-e2e-harness

A generic end-to-end test harness that drives a real model consumer against an arbitrary MCP server, through a real driver (currently Claude Code headless), and records what happened. The harness **executes and records; it never scores** — pass/fail against criteria pinned before the run is a human/spec-session judgment made beside the raw artifacts.

The normative spec lives in [documentation/](documentation/00-INDEX.md): the verification contract (`10-harness.md`), the suite manifest schema (`20-manifest.md`), and the declarative Layer-1 check language (`30-checks.md`). Everything server-specific arrives in a **suite** — one manifest JSON file plus its groundings, owned by the server project's own repository. This harness contains no knowledge of any particular MCP server; any server-specific constant found in it is a defect.

## Install and run

```bash
uv sync                                          # or: pip install -e .
uv run mcp-e2e validate --manifest path/to/manifest.json
uv run mcp-e2e run --manifest path/to/manifest.json --dry-run
uv run mcp-e2e run --manifest path/to/manifest.json [--cells floor,ceiling] [--groups A,B] [--prompts A1]
```

Secrets never appear in a manifest or an artifact: the manifest references them as `{"$secret": "VAR"}` and the harness resolves them from its own environment at launch. Export them in the shell that runs the harness.

## Smoke suite — proving the instrument

[examples/smoke/prompts.json](examples/smoke/prompts.json) is a checked-in suite whose SUT is the harness's own distractor MCP server, so it needs no network, credentials, or external server. It exists to prove the instrument live — the claude-code driver's isolation flags, the trace proxy's pinned record fields, list-valued surface enforcement, the crowded-cell session mechanics, and the Layer-1 check outcomes — never to measure anything:

```bash
uv run mcp-e2e run --manifest examples/smoke/prompts.json    # spends a few small model calls
```

Expected shape of a healthy run: every invocation records at least one trace record (`content-is-typed` and `unfiled-listing-shape` pass, `expected-vacuous` reports vacuous), the isolation cell's `available-tools.json` shows three advertised / one exposed, and the crowded cell's directory carries `crowding.json`, a separate `crowding-trace.jsonl`, and the procedure's content hash in `meta.json`. The artifacts stay under the gitignored `runs/` as the local proof of the driver.

## What a run records

Run bytes land in a gitignored `runs/<timestamp>/` tree (disposable; the scored findings are what gets committed, in the suite repo). Per invocation, under `<cell>/<group>/<prompt-id>/`:

- `trace.jsonl` — every tool call that reached the server, recorded by the harness's own stdio proxy with the pinned fields (`index, tool, args, response, is_error, started_at, duration_ms, response_bytes`). For list-valued `tool_surface` cells the proxy also *enforces* the surface, so trace scope equals tool surface by construction.
- `available-tools.json` — the recorded available-tool surface (disabling a tool is a claim; the tool listing is the evidence).
- `answer.txt`, `runner-stderr.txt`, `meta.json` (knobs verbatim in the driver's vocabulary, manifest hash, argv, asserted attribution record, pinned criteria), `mcp-config.json`, `server-config.json`, and — when applicable — `setup.json`, `crowding.json`, `proxy-meta.json`.

Plus `run-manifest.json` and `checks-report.json` at the run root. Layer-1 checks report one of **pass / fail / vacuous / error** per check — the four outcomes are never collapsed.

Built-in and not waivable: secret hygiene (no configured secret material in any artifact — the run halts) and instrument liveness (a cell whose every invocation recorded zero trace records is reported BROKEN, never clean).

## Crowded cells

`context: "crowded"` cells select a harness-owned, versioned crowding procedure by name (e.g. `neutral-file-triage@2`): the harness registers a distractor MCP server beside the server under test and runs the procedure's opening turn in the same session before the scored prompt lands, recording the procedure's name, version, and content hash. Suites never author crowding content (ruling S6); they attest domain disjointness in `crowding.collision_review`.

## Repo layout

- `src/mcp_e2e_harness/` — the harness library and CLI.
- `tests/` — unit and integration tests (`uv run pytest`); the integration tests use the harness's own distractor server as the SUT, so they are hermetic.
- `documentation/` — the spec (exclusive domain of the spec session; see `CONTRIBUTING.md`).
- `reference/congressmcp/` — the congressMCP seed implementation this harness was generalized from, kept verbatim as provenance; not imported by anything.
