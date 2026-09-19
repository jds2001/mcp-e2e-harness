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
    },
    "cross-vendor-floor": {
      "driver": "codex", "model": "gpt-5.6-luna", "knobs": {"reasoning_effort": "medium"},
      "role": "cross-vendor-floor", "context": "fresh",
      "tool_surface": ["search_thing", "get_thing"], "merge_gating": false, "groups": ["A"],
      "notes": "gating is this suite's own choice (S11); a cross-vendor cell never substitutes for a primary-vendor gate"
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

- **Your server command runs from a neutral working directory — never your repo.** Attribution requires it, so cwd-dependent launchers die at spawn: `uv run <script>` resolves its project from cwd and fails with exit 2 from anywhere else. Use `uv run --project /abs/path/to/your-repo <script>`, an absolute path to an installed entry point, or `python -m your_server` against an absolute-path environment. The first real suite lost a run to exactly this.
- **Optional fields may be explicit `null`** — null and absent are equivalent everywhere except `pass`/`fail`, where null means "scored by the named `rubric`" and `rubric` becomes required.
- **Secrets**: values written as `{"$secret": "VAR"}` resolve from the harness environment at launch and never touch an artifact. Name your sensitive keys in `secret_keys` — a literal value under a named key is a load error. Trace/transcript/meta are scanned for resolved secret material; a hit halts the run.
- **`tool_surface`**: `"full"` or an explicit tool-name list. Attribution-dependent conclusions ("that citation is absent from the trace, therefore fabricated") are valid **only** in list-surface cells, where trace scope equals tool surface — the harness enforces the list at the MCP proxy and verifies it on the model-API wire.
- **Fresh by default**: every invocation gets a fresh neutral working directory and a fresh server process. State you want present must arrive explicitly, via `setup` (an ordered list of `{tool, args}` calls the harness makes directly against your server before the prompt — never via a model turn) or `env`. There is no warm-by-accident.
- **Crowded cells**: you *select* a harness-owned crowding procedure by pinned name and version; you never author crowding content (an internals-aware author would be writing part of the instrument they are scored against). `collision_review` is your dated attestation that the procedure's content is disjoint from your server's domain; if it collides, select a different harness procedure. Currently pinned: `neutral-file-triage@2` (a mundane office notes-triage task — collides with note-keeping, filing, and office-facilities domains).
- **Knobs are driver-native and verbatim**: write `thinking` for Claude drivers, another vendor's terms for its cells; never translate between vendors' scales. A cross-vendor cell never silently substitutes for a gating cell of your primary vendor.

### Cross-vendor cells with the `codex` driver

A codex cell is three manifest fields — `"driver": "codex"`, a codex model id, and codex-native knobs (`reasoning_effort`, not `thinking`) — as the skeleton's `cross-vendor-floor` shows. Everything attribution-critical is the driver's job, not yours, and none of it is configurable from the manifest: isolated per-invocation `CODEX_HOME`, web-tool removal via the harness's recording provider, the read-only sandbox, plugin-sync suppression, and per-invocation wire verification all happen automatically, and a violation breaks the cell rather than tainting your data. What you do need to know:

- **Credentials**: the harness environment must carry an OpenAI **API key** for the recording provider to forward with; the driver refuses ChatGPT-login state for attribution cells. Driver credentials live in the harness environment, not in your manifest — the manifest's `{"$secret": …}` mechanism is for *your server's* keys, and it works identically under codex (the harness injects them past codex's env sanitization; you author nothing extra).
- **Role and gating**: give codex cells a `cross-vendor-*` role; whether they gate is your suite's decision like any cell (S11 in the harness spec). The one hard rule is substitution, not gating: a cross-vendor cell never stands in for a gating cell of your primary vendor.
- **Version sensitivity**: the codex driver contract is verified against a pinned codex-cli version (see the harness repo's `50-drivers.md`); on a different local version the harness re-verifies before attribution cells run — expect that, don't fight it.

### Real floor models with the `loop` driver (OpenRouter)

The `loop` driver (`50-drivers.md`) puts the harness's own minimal agent loop in front of any OpenRouter model, so the floor role can be a cheap open-weight model rather than whatever the product CLIs expose. It measures **server × model under the harness scaffold**, a different thing from the product-driver cells, and never pools with them (S11). Selecting models is your call; these are the constraints and a dated starting roster.

**Eligibility is per endpoint, not per model id (S12).** OpenRouter's model-level "supports tools" flag is not enough: on 2026-09-18, 5 of the 24 endpoints serving `openai/gpt-oss-120b` did not advertise tools, and quantization on the rest ranged bf16 to fp4. Read the endpoint listing (`GET /api/v1/models/<id>/endpoints`, free) before pinning, and pin a merge-gating loop cell. A pin candidate must: advertise `tools` on that endpoint; state its quantization, or be the model's first party; show a healthy status; and survive the driver's data-policy preference. The harness's calibration probe then confirms the pair actually calls a tool before any scored turn runs.

**What the ranking you may have seen means.** OpenRouter's category rankings ("top legal model" and so on) are usage share by tokens, not accuracy; the page is client-rendered and was not machine-readable to the spec session, so the standing is a maintainer report here. Popularity is a good reason to *include* a model in the floor — a floor should be what real consumers use — and no reason to trust its answers; the suite measures that.

**Starting roster (catalog snapshot 2026-09-18; list price $/M input / output; endpoints total / advertising tools).** Re-read the catalog before authoring; this table will be stale.

| role | model id | $/M in / out | endpoints (tools) | notes |
|---|---|---|---|---|
| floor, open-weight | `openai/gpt-oss-120b` | 0.15 / 0.60 | 24 (19) | the maintainer's pick; **pin** — bf16 endpoints with tools existed at $0.03–0.04 (AkashML, DekaLLM, DeepInfra); reasoning cannot be disabled, use `{"reasoning": {"effort": "low"}}` |
| floor, second lineage | `deepseek/deepseek-v4-flash` | 0.048 / 0.097 | 16 (16) | fp8 everywhere; a non-OpenAI lineage so the floor is not one family |
| floor, second lineage (alt) | `z-ai/glm-5.3-flash` | 0.09 / 0.30 | 29 (29) | first party is `z-ai/fp8` at 0.15 / 0.50 |
| floor, no pin question | `qwen/qwen3.7-flash` | 0.03 / 0.13 | 1 (1) | single first-party endpoint |
| floor, no pin question | `mistralai/mistral-small-2603` | 0.15 / 0.60 | 3 (3) | first party only, a zero-retention tag exists |
| capability-floor | `openai/gpt-oss-20b` | 0.03 / 0.13 | 13 (9) | pin; bf16 with tools at DekaLLM/DeepInfra ~0.03 |
| vendor mid-tier (optional) | `openai/gpt-5.4-nano` | 0.20 / 1.25 | 4 (4) | first party; the cheapest current OpenAI tier. **No `temperature` knob** — the strict pin refuses it (probed 2026-09-18) |
| vendor mid-tier (optional) | `google/gemini-3.5-flash-lite` | 0.30 / 2.50 | 8 (8) | first party |
| avoid | `meta-llama/llama-4-maverick` | 0.19 / 0.65 | 5 (3) | tools on a minority of endpoints, no reasoning knob |
| avoid | any `:free` id | 0 | — | rate-limited, and the free tier is where prompt logging concentrates |

Every row above except the `:free` line was probed on 2026-09-18, first by the spec session's script (`openrouter-probe-2026-09-18.md`) and then under the harness's own probe gate (`50-drivers.md` → loop, verification record): all returned a well-formed tool call at the stated pin except `mistralai/mistral-small-2603`, which OpenRouter's shared Mistral pool rate-limited on six attempts that day — unmeasured, not struck; re-probe before relying on it. The loop driver is verified for pinned cells as of 2026-09-18; a pin verifies the provider, and the quantization in a pin tag is a request-side preference the router enforces on its own say-so. **Repeats on a pin may replay one draw** — the driver sends no sampling knobs unless you do, and a pinned endpoint returned four byte-identical answers in ten invocations on 2026-09-18 — so state distinct-answer counts beside invocation counts in any rate claim, and set `temperature` or `seed` in knobs (where the endpoint declares them) if you want a distribution rather than a replay. Set `max_tokens` with the answer you expect in mind: a 4,096 cap cut a 20k-character paste three times in ten, and only the finish reason on the wire line tells you that happened. Start with two or three: the maintainer's pick pinned, one second-lineage floor, and the capability-floor. Grow on a question, never on curiosity (`10-harness.md`, grid grows on need).

**Cost, so you can set the cap before the first run.** Measured under the loop driver itself (2026-09-18, `50-drivers.md` → loop, loop-specific cost basis): a crowded floor invocation of a two-call prompt at `openai/gpt-oss-120b` @ `deepinfra/bf16` cost $0.002 and a fresh isolation one under $0.001, so a 40-invocation pass (20 prompts × floor + isolation) is about **$0.06 at that pin, $0.25 at the $0.15/M list tier, and a few dollars at the $1/M vendor tier**. The product-driver numbers are roughly ten times higher because the product CLI's system prompt rides on every request. The unbounded terms are reasoning tokens at higher effort and runaway tool loops, which the driver's budget cap and the scaffold's step cap exist for. Set `budget_usd` per cell or the run-level cap, and read the pre-run estimate the runner prints.

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

While a run is live, the runner reports progress to the terminal and artifacts land per cell as it goes — if anything looks wrong, read before killing: `proxy-meta.json` (did your server die? `server_exit` is the tell), `available-tools.json` (did its tools register?), and the cell's `meta.json` (`trace_records: 0` on a prompt that needs your server means the consumer answered from priors — instrument breach, not data).

Gitignore the run directory — bytes are disposable. What you commit is the manifest, its grounding measurements, and your scored findings, each finding citing the run artifacts it was scored from. Commit each ruling as it is made; the git history is your decision record.

## Recommended starting grid

Two gating cells; grow only when a question needs a new cell.

| role | what it isolates | shape |
|---|---|---|
| floor | the merge-relevant result: a distracted mid-task consumer | mid-tier model, no extended thinking, crowded, full surface |
| ceiling | whether the data supports a correct answer at all | top model, high thinking, fresh, question first |
| capability-floor (optional) | weakest consumers, without conflating chaining limits with tool defects | small model, single-step variants only |
| isolation (optional, needed for attribution claims) | tool selection noise vs tool design defects | surface = exactly the tools under test |
