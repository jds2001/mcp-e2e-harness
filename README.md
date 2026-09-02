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
- `answer.txt`, `runner-stderr.txt`, `meta.json` (knobs verbatim in the driver's vocabulary, manifest hash, argv, asserted attribution record, pinned criteria, and `environment_state` — vendor-pushed state a driver observed in its own invocation's environment after the turn ran, outside anything the harness configured; `null` for drivers with nothing of the kind to report), `mcp-config.json`, `server-config.json`, and — when applicable — `setup.json`, `crowding.json`, `proxy-meta.json`.

Plus, per cell, `spawn-check.json` — the S8 fail-fast liveness gate: before any model turn is spent, the SUT is spawned once under the invocation's own conditions (neutral cwd, cell env) and must initialize and advertise its tools; a dead or toolless server BREAKS the cell immediately (`CELL-VOID.json`, prompts skipped, breach surfaced live), which is how a cwd-dependent server command dies loudly instead of letting the consumer answer from priors (defect DR-1). And `run-manifest.json` and `checks-report.json` at the run root. Layer-1 checks report one of **pass / fail / vacuous / error** per check — the four outcomes are never collapsed.

The runner is never silent (ruling S9): it names the run directory up front and reports live — spawn checks, each cell/prompt as it starts, turn transitions (setup, crowding pre-turn, scored turn), and per-invocation completion with wall clock and trace-record count — so in-flight, hung, and broken are distinguishable from the terminal.

Built-in and not waivable: secret hygiene (no configured secret material in any artifact — the run halts) and instrument liveness (fail-fast at spawn per the above, plus a cell whose every invocation recorded zero trace records is reported BROKEN, never clean).

## Verifying the builtin-tool surface (Q2)

The MCP proxy records and enforces the *MCP-side* surface, but a driver's built-in tools (web, filesystem, shell) never cross it. Two instruments close that gap, ranked per the spec's Q2 ruling:

**API-boundary capture (preferred — wire-level ground truth).** For drivers whose outbound API traffic the harness can redirect (claude-code, via `ANTHROPIC_BASE_URL`), every invocation routes through a local transparent recording proxy that records the actual `tools` array sent to the model — `api-surface.jsonl` beside the trace, summarized in `meta.json` as `api_surface`. Nothing model-mediated sits in the verification path, and recording an outgoing request changes nothing in the consumer's context, so it runs during scored cells. Only tool *names* are recorded — never headers, message content, or schemas. A disallowed builtin observed on the wire is an instrument breach (the row's claims are not tool-attributable); an invocation whose capture recorded zero model requests is *unverified*, never clean.

**Calibration probe (fallback, for drivers without capture).** A separate, discarded invocation under the identical configuration asks the consumer to enumerate its tools; the enumeration must contain the distractor target's tools (positive control — a refusal is `broken`, never "builtins absent") and none of the disallowed builtin names. Model-mediated and valid per recorded `cli_version`, both stated in the artifact's caveat. `mcp-e2e run` probes such a driver automatically before any prompt is spent whenever a selected cell is `merge_gating`; on demand:

```bash
uv run mcp-e2e probe-driver --driver claude-code    # one small model call; no manifest needed
```

The probe already earned its keep once: it caught that claude's `--disallowed-tools` denies invocation but leaves builtins on the consumer's surface, which is why the driver now also passes `--tools ""` (surface removal, with disallow kept as the belt).

## Drivers

- **claude-code** — verified; Q2 settled (documentation/50-drivers.md).
- **codex** — implemented to the 50-drivers.md contract: API-key credentials only (a fresh, harness-authored `CODEX_HOME` per invocation carries no login state; preflight refuses without `OPENAI_API_KEY`), web disabled via a noweb custom provider whose `base_url` **is** the harness recorder (one interposition yields both the web-disable and the wire verification), a spawn-time 0600 secrets file for MCP servers (codex sanitizes their env, so `$secret` inheritance delivers nothing), `reasoning_effort` verbatim, and hard breach semantics: a `web_search` in a wire-captured tools array or a web event in the driver's streams BREAKS the cell. Measured 2026-09-01 (codex-cli 0.147.0, wire capture): the tool roster is **model-dependent** — `gpt-5.2` carries a hosted `web_search` (`external_web_access: false`) that no config knob removes (`tools.web_search=false`, `web_search_mode`, `tools.web_search.mode` all dead at the array level), while `gpt-5.6-luna` offers no web tool — so model choice is load-bearing for codex attribution cells, and the per-invocation capture enforces it. Codex-family models send no `tools` array at all — the served roster travels in `client_metadata["x-codex-turn-metadata"].code_mode_tool_names`, which the recorder reads (found by full-body probe after the array capture came back empty on a working turn). Q2 is settled for codex (spec 069dd8f): for code-mode models the S7 standard is a conjunction — declaration shows no web tool, zero web events, and the noweb provider in place — never the declaration alone; tools-array models keep the array standard, with `tool_source` in every record keeping the two distinguishable. The full evidence set (wire capture, zero web events, secrets-file canary, version pinned) was measured live 2026-09-01 via `examples/smoke/codex-prompts.json`, plus the egress canary — `mcp-e2e probe-egress --driver codex` — which converted the last externally-attributed claim (sandbox blocks shell egress) into a harness-instrument observation: the event stream shows the curl executing and failing DNS, and only the exact honest-failure protocol passes. Scoring of codex attribution cells still reviews the recorded exec events (`runner-stdout.txt`/`runner-stderr.txt`) alongside the trace; a network-touching exec is the same breach class as a web tool on the wire. That canary's own artifacts opened a second residual (50-drivers.md): the isolated `CODEX_HOME` contained a shallow clone of `github.com/openai/plugins` with a `FETCH_HEAD` — the CLI's own network I/O, independent of the sandbox/provider controls that govern the consumer. Measured 2026-09-01 across isolated-home trials (the fetch happens at CLI startup regardless of turn outcome, so it needs no working model call to probe): `-c features.plugins=false` reliably suppresses it; `-c features.remote_plugin=false` and `-c plugins.marketplaces=[]` do not (dead knobs, consistent with this CLI's history of config keys that don't do what they say). The working flag is now in the driver's argv, asserted in `attribution_record`, and — because a config knob measured effective today is not a config knob guaranteed effective on the next version — verified per invocation regardless: every driver exposes `environment_state()`, called after the turn executes and lifted into `meta.json`/`probe.json` as `environment_state`; codex's implementation reads `CODEX_HOME/.tmp/plugins.sha` and reports the fetched SHA or `None`. A future non-null value is drift to investigate, not noise to ignore — re-verified live via `probe-egress` post-fix (`runs/2026-09-01-codex-plugin-sync-fix-verify`, gitignored): no `.tmp` directory at all, `environment_state.plugin_sync.fetched_sha` is `None`, and the canary still passes clean.

## Crowded cells

`context: "crowded"` cells select a harness-owned, versioned crowding procedure by name (e.g. `neutral-file-triage@2`): the harness registers a distractor MCP server beside the server under test and runs the procedure's opening turn in the same session before the scored prompt lands, recording the procedure's name, version, and content hash. Suites never author crowding content (ruling S6); they attest domain disjointness in `crowding.collision_review`.

## Repo layout

- `src/mcp_e2e_harness/` — the harness library and CLI.
- `tests/` — unit and integration tests (`uv run pytest`); the integration tests use the harness's own distractor server as the SUT, so they are hermetic.
- `documentation/` — the spec (exclusive domain of the spec session; see `CONTRIBUTING.md`).
- `reference/congressmcp/` — the congressMCP seed implementation this harness was generalized from, kept verbatim as provenance; not imported by anything.
