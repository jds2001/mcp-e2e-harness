# Writing an e2e suite for your MCP server

You are authoring a **suite**: the server-specific half of an end-to-end verification setup whose other half is the generic harness. The harness's repository is `model-e2e-harness`; the command it installs is `mcp-e2e`. This document is the contract, written so that you can author a manifest from it alone — if you had to open another harness file to write a cell, that is a defect in this document, and the harness spec session wants to hear about it. The harness spec (`documentation/` in the harness repo) is normative if the two ever disagree. First distilled 2026-08-31; last revised 2026-09-23, against harness commit 01cdd33. Three things below need that commit or later — `--repeats`, per-cell check outcomes, and `env` in `cell_id`; if `mcp-e2e run --help` shows no `--repeats`, your harness predates them.

You run the harness from outside its repo. Call the entry point in the harness's own environment by absolute path (`/abs/path/to/model-e2e-harness/.venv/bin/mcp-e2e validate --manifest /abs/path/to/your/manifest.json`). The first outside suite runs it that way from a neutral working directory; the consumer's own working directory is the harness's business and is always neutral, whatever yours is.

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

One JSON file. Top level: `suite`, `server` (required); `fixtures`, `rubrics`, `checks`, `measurements` (optional); `cells`, `prompts` (required).

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
      "notes": "gating is this suite's own choice; a cross-vendor cell never substitutes for a primary-vendor gate"
    },
    "loop-floor": {
      "driver": "loop", "endpoint": "openrouter", "scaffold": "loop-scaffold@1",
      "model": "openai/gpt-oss-120b", "provider": "deepinfra/bf16",
      "knobs": {"reasoning": {"effort": "low"}, "max_tokens": 8192},
      "role": "floor", "context": "fresh",
      "tool_surface": ["search_thing", "get_thing"], "merge_gating": false, "groups": ["A"],
      "budget_usd": 0.50,
      "notes": "non-gating until one run has been scored"
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

### Cell fields

Every cell, whatever its driver. An unknown key is a load error, so write only these (plus the loop fields further down, for loop cells, and `_`-prefixed commentary).

| field | required | what |
|---|---|---|
| `driver` | yes | `claude-code`, `codex`, or `loop` |
| `model` | yes | model id, verbatim in the driver's vocabulary |
| `knobs` | yes | object of driver-native settings, verbatim; the harness never defaults or translates them |
| `role` | yes | `floor`, `ceiling`, `capability-floor`, `isolation`, `cross-vendor-*`, or a name of your own |
| `context` | yes | `fresh` or `crowded` |
| `crowding` | when `context` is `crowded` | `{procedure, collision_review}` — see the crowded-cells note below |
| `tool_surface` | yes | `"full"` or an explicit list of your server's tool names |
| `merge_gating` | yes | boolean: does a failure here block your merge |
| `groups` | yes | the prompt groups that run in this cell |
| `prompts` | no | a list of prompt ids that **narrows** `groups` to exactly those prompts — how you restrict an experiment cell to one prompt |
| `variant` | no | the prompt variant this cell uses (`single_step`); default is the base `prompt` |
| `setup` | no | ordered list of `{tool, args}` calls the harness makes directly against your server before the prompt, never via a model turn; what ran and what it returned is recorded in the row's `meta.json`. Setup runs in **its own server process**, not the scored turn's, so it carries only state that outlives a process (a disk cache, a database) at a location both processes reach; in-memory state does not survive to the scored turn |
| `env` | no | extra environment variables for **your server's process in this cell only**, added to `server.transport.env` (on a key both set, the cell's value wins), with the same `{"$secret": …}` handling; part of the cell's identity |
| `notes` | no | free text: what the cell isolates, a pointer to its preregistration |

Notes on the parts that bite:

- **Your server command runs from a neutral working directory — never your repo.** Attribution requires it, so cwd-dependent launchers die at spawn: `uv run <script>` resolves its project from cwd and fails with exit 2 from anywhere else. Use `uv run --project /abs/path/to/your-repo <script>`, an absolute path to an installed entry point, or `python -m your_server` against an absolute-path environment. The first real suite lost a run to exactly this.
- **Optional fields may be explicit `null`** — null and absent are equivalent everywhere except `pass`/`fail`, where null means "scored by the named `rubric`" and `rubric` becomes required.
- **Secrets**: values written as `{"$secret": "VAR"}` resolve from the harness environment at launch and never touch an artifact. Name your sensitive keys in `secret_keys` — a literal value under a named key is a load error. Trace/transcript/meta are scanned for resolved secret material; a hit halts the run.
- **`tool_surface`**: `"full"` or an explicit tool-name list. Attribution-dependent conclusions ("that citation is absent from the trace, therefore fabricated") are valid **only** in list-surface cells, where trace scope equals tool surface — the harness enforces the list at the MCP proxy and verifies it on the model-API wire.
- **Fresh by default**: every invocation gets a fresh neutral working directory and a fresh server process. State you want present must arrive explicitly, via the cell's `setup` or the cell's `env` (both in the field table above). There is no warm-by-accident.
- **Crowded cells**: you *select* a harness-owned crowding procedure by pinned name and version; you never author crowding content (an internals-aware author would be writing part of the instrument they are scored against). `collision_review` is your dated attestation that the procedure's content is disjoint from your server's domain; if it collides, select a different harness procedure. Currently pinned: `neutral-file-triage@2` (a mundane office notes-triage task — collides with note-keeping, filing, and office-facilities domains).
- **Knobs are driver-native and verbatim**: write `thinking` for Claude drivers, another vendor's terms for its cells; never translate between vendors' scales. A cross-vendor cell never silently substitutes for a gating cell of your primary vendor.

### Cross-vendor cells with the `codex` driver

A codex cell is three manifest fields — `"driver": "codex"`, a codex model id, and codex-native knobs (`reasoning_effort`, not `thinking`) — as the skeleton's `cross-vendor-floor` shows. Everything attribution-critical is the driver's job, not yours, and none of it is configurable from the manifest: isolated per-invocation `CODEX_HOME`, web-tool removal via the harness's recording provider, the read-only sandbox, plugin-sync suppression, and per-invocation wire verification all happen automatically, and a violation breaks the cell rather than tainting your data. What you do need to know:

- **Credentials**: the harness environment must carry an OpenAI **API key** for the recording provider to forward with; the driver refuses ChatGPT-login state for attribution cells. Driver credentials live in the harness environment, not in your manifest — the manifest's `{"$secret": …}` mechanism is for *your server's* keys, and it works identically under codex (the harness injects them past codex's env sanitization; you author nothing extra).
- **Role and gating**: give codex cells a `cross-vendor-*` role; whether they gate is your suite's decision like any cell. The one hard rule is substitution, not gating: a cross-vendor cell never stands in for a gating cell of your primary vendor.
- **Version sensitivity**: the codex driver contract is verified against a pinned codex-cli version (see the harness repo's `50-drivers.md`); on a different local version the harness re-verifies before attribution cells run — expect that, don't fight it.

### Real floor models with the `loop` driver (OpenRouter)

The `loop` driver puts the harness's own minimal agent loop in front of any OpenRouter model, so the floor role can be a cheap open-weight model rather than whatever the product CLIs expose. It measures **server × model under the harness's pinned scaffold**, a different thing from what the product-driver cells (`claude-code`, `codex`) measure, which is server × shipping product. Rows from the two families never pool and never substitute for each other, in either direction. The OpenRouter credential is `OPENROUTER_API_KEY` in the harness's environment; it never appears in your manifest. Selecting models is your call; below are the fields, the constraints, and a dated starting roster.

**Loop cell fields.** The common fields in the table above apply unchanged; a loop cell adds or constrains these. The skeleton's `loop-floor` is a complete example.

| field | required | what |
|---|---|---|
| `endpoint` | yes | the named deployment; the only value today is `"openrouter"`. Hosts and credentials are harness configuration, never manifest content |
| `scaffold` | yes | the harness's loop scaffold as `name@version`; the only pinned one is `"loop-scaffold@1"`. It is the system prompt, tool-loop policy, and step cap (24 requests per turn), and it is part of the cell's identity: cells on different scaffold versions never pool |
| `model` | yes | the OpenRouter model id verbatim (`openai/gpt-oss-120b`), with no routing suffix (`:free` and kin) — routing is expressed in `provider` |
| `provider` | no | **the pin.** One endpoint `tag` copied verbatim from the endpoint listing described below (`deepinfra/bf16`; some tags have no quantization part, such as `openai`). Present: the driver sends it as the sole allowed provider with fallbacks off, it is part of the cell's identity, and a response served by any other provider breaks the cell. Absent: the cell is unpinned — it runs normally, and every row it produces is marked `reproducibility: unpinned` |
| `knobs` | yes | OpenRouter request fields verbatim: the unified `reasoning` object (`{"effort": "low"}`), `temperature`, `seed`, `max_tokens`, and so on. A floor-role reasoning model carries its minimum effort, since most cannot switch reasoning off |
| `data_policy` | no | `"deny"` (the default) or `"allow"`. Under `deny` the driver sends OpenRouter's preference excluding providers that collect prompts. This is a routing preference and a disclosure, not a verified privacy property; the only enforceable instrument is a pin to a provider whose policy you have read |
| `budget_usd` | no | a spend cap for this cell: reaching it stops this cell (its remaining prompts are skipped, completed rows stand) and the run continues. The run-level cap is the `--budget-usd` flag, and reaching that one stops the run. Either stop is recorded in `run-manifest.json` under `budget.stops` |

What a pin verifies, exactly: a response names its provider and nothing finer, so the **provider part** of the tag is verified on every request, and the **quantization part** is a preference the router enforces on its own say-so. Artifacts say this in so many words (`quantization_asserted`, `pin_slug_verified_against`); your findings should not claim more.

**Eligibility is per endpoint, not per model id.** OpenRouter's model-level "supports tools" flag is not enough: on 2026-09-18, 5 of the 24 endpoints serving `openai/gpt-oss-120b` did not advertise tools, and quantization on the rest ranged bf16 to fp4. So before choosing a pin, read the endpoint listing: `GET https://openrouter.ai/api/v1/models/<model id>/endpoints` (free, no key). Each entry of `data.endpoints` is one candidate, and these are the fields to read (re-checked against the live listing on 2026-09-21):

| listing field | what to do with it |
|---|---|
| `tag` | the value you copy verbatim into the cell's `provider` (`akashml/bf16`, `deepinfra/bf16`, `openai`). `provider_name` is the display name (`DeepInfra`) and is what responses report; it is not what the manifest takes |
| `supported_parameters` | must contain `tools`, and must contain **every top-level key you put in `knobs`** (`reasoning`, `temperature`, `seed`, `max_tokens`) — see the knob rule below |
| `quantization` | must be stated (`bf16`, `fp8`, …; not `unknown`), unless the endpoint is the model's first party |
| `status` | `0` is healthy. Anything else is not — on 2026-09-21, 4 of that model's 24 endpoints carried `-2` or `-5` — and is not a pin candidate. `uptime_last_30m` sits beside it |
| `context_length`, `max_completion_tokens` | the endpoint's own limits; your `max_tokens` has to fit under the second |
| `pricing.prompt`, `pricing.completion` | dollars per token on this endpoint, which can differ severalfold from the model's list price |

The harness then confirms the choice itself: before any scored turn, a discarded **calibration probe** runs under the cell's exact scaffold, knobs, and pin against a trivial harness-owned tool, and the pair must return one well-formed tool call. A pair that fails is refused for the run before any scored turn is spent. `mcp-e2e probe-loop --manifest …` runs only the probes, for well under a cent, and is the cheap way to check a new roster.

**The knob rule.** Every loop request is sent with OpenRouter's `require_parameters` flag, so a knob the endpoint does not declare is **refused, never silently dropped**. On a pinned cell, a `knobs` key missing from the pinned endpoint's `supported_parameters` fails the calibration probe: the cell is voided for the run before any scored turn, and `run-manifest.json` → `voided_cells.<cell>` names the undeclared parameter. On an unpinned cell the same flag narrows routing to endpoints that declare every knob you sent, and the probe fails the same way if no endpoint does. Measured example: `openai/gpt-5.4-nano` @ `openai` does not declare `temperature`, so that cell was voided with `temperature` in its knobs and ran normally without it (2026-09-18). Without the flag the router dropped the knob and answered as if nothing were wrong, which is why the flag is not optional. One limit: declared is not honored. If a conclusion of yours depends on a knob's *effect* (reasoning effort above the minimum, most likely), check the effect where it is recorded — reasoning-token counts on the wire lines, for effort.

**Gating.** Whether a loop cell gates your merge is your decision, exactly as for any other cell; the harness neither asks loop cells to gate nor forbids it. The one rule is conditional: **if** a loop cell is merge-gating, it must be pinned, because an unpinned cell has no fixed environment to attribute a failure to. Keeping a new loop cell non-gating until one run has been scored is a sound default.

**What the ranking you may have seen means.** OpenRouter's category rankings ("top legal model" and so on) are usage share by tokens, not accuracy; the page is client-rendered and was not machine-readable to the spec session, so the standing is a maintainer report here. Popularity is a good reason to *include* a model in the floor — a floor should be what real consumers use — and no reason to trust its answers; the suite measures that.

**Starting roster (catalog snapshot 2026-09-18; list price $/M input / output; endpoints total / advertising tools).** Re-read the catalog before authoring; this table will be stale.

| role | model id | $/M in / out | endpoints (tools) | notes |
|---|---|---|---|---|
| floor, open-weight | `openai/gpt-oss-120b` | 0.15 / 0.60 | 24 (19) | the maintainer's pick; **pin** — bf16 endpoints with tools at $0.03–0.04: tags `akashml/bf16`, `dekallm/bf16`, `deepinfra/bf16` (the last is the one the harness's own runs used); reasoning cannot be disabled, use `{"reasoning": {"effort": "low"}}` |
| floor, second lineage | `deepseek/deepseek-v4-flash` | 0.048 / 0.097 | 16 (16) | fp8 everywhere; a non-OpenAI lineage so the floor is not one family |
| floor, second lineage (alt) | `z-ai/glm-5.3-flash` | 0.09 / 0.30 | 29 (29) | first party is `z-ai/fp8` at 0.15 / 0.50 |
| floor, no pin question | `qwen/qwen3.7-flash` | 0.03 / 0.13 | 1 (1) | single first-party endpoint |
| floor, no pin question | `mistralai/mistral-small-2603` | 0.15 / 0.60 | 3 (3) | first party only, tag `mistral`; a zero-retention tag exists |
| capability-floor | `openai/gpt-oss-20b` | 0.03 / 0.13 | 13 (9) | pin; probed at `deepinfra/bf16` (~0.03); DekaLLM listed a bf16 endpoint with tools as well |
| vendor mid-tier (optional) | `openai/gpt-5.4-nano` | 0.20 / 1.25 | 4 (4) | first party, tag `openai`; the cheapest current OpenAI tier. **No `temperature` knob** — the endpoint does not declare it, so the knob rule above voids the cell (probed 2026-09-18) |
| vendor mid-tier (optional) | `google/gemini-3.5-flash-lite` | 0.30 / 2.50 | 8 (8) | first party, probed at tag `google-ai-studio` |
| avoid | `meta-llama/llama-4-maverick` | 0.19 / 0.65 | 5 (3) | tools on a minority of endpoints, no reasoning knob |
| avoid | any `:free` id | 0 | — | rate-limited, and the free tier is where prompt logging concentrates |

Every row above except the `:free` line was probed on 2026-09-18 under the harness's own calibration probe: all returned a well-formed tool call at the stated pin except `mistralai/mistral-small-2603`, which OpenRouter's shared Mistral pool rate-limited on six attempts that day — unmeasured, not struck; re-probe before relying on it. Start with two or three cells: the maintainer's pick pinned, one second-lineage floor, and the capability-floor. Grow on a question, never on curiosity.

**Repeats on a pin may replay one draw.** The driver sends no sampling knobs unless your `knobs` carry them, and a pinned endpoint can be near-deterministic at its defaults: `deepinfra/bf16` returned four byte-identical answers in ten invocations on 2026-09-18, six distinct answers in all, while the unpinned arm gave ten distinct answers. So an invocation count is not a sample count. State the distinct-answer count beside the invocation count in any rate claim ("0 of 10, 6 distinct"). If you want a distribution and not a replay, set `temperature` or `seed` in `knobs` — subject to the knob rule above, so only where the pinned endpoint declares them — say so in the cell's `notes`, and compare that cell only with cells carrying the same knobs.

**Getting N invocations.** Nothing in the manifest repeats a cell, by design: the number of repetitions belongs to the run and not to the instrument, so it never touches your manifest hash. State N in your preregistration, and pass it to the run: `mcp-e2e run --manifest … --cells e17-control,e17-feature-on --repeats 10 --run-dir runs/e17`.

- Every repetition is a whole fresh invocation: new neutral working directory, new server process, `setup` re-run, crowding pre-turn re-run. The calibration probe still runs once per cell.
- The runner makes pass 1 over all selected cells, then pass 2, and so on, so arms in one run are interleaved in time and a budget stop leaves them with counts within one of each other.
- With the flag (including `--repeats 1`) a row's directory gains a level: `<cell>/<group>/<prompt id>/r01/`. Without it the layout has no such level. Read `repetition` in the row's `meta.json` instead of parsing paths.
- The run manifest's per-prompt `answers` block is then the distinct-answer count: `invocations` (rows attempted), `answered` (non-empty answers), and `distinct` with `digests` over the answered rows only, so an answerless row never reads as a replay.
- The pre-run invocation count and estimate multiply by N, and `--dry-run` shows them.
- Rows from a `--repeats` run and rows from separate runs of the same manifest are comparable with each other. If your repeats are spread over several run directories, nothing aggregates them for you: collect `answer_sha256_16` from each row's `meta.json`.

**Set `max_tokens` with the answer you expect in mind.** A 4,096 cap cut a 20k-character paste three times in ten, and the answer text alone cannot show it. The `finish_reason` on the wire line can: `length` is the cap's cut, `stop` is the model's own ending ("What a loop row records", below, has the file and field).

**Cost, so you can set the cap before the first run.** Measured under the loop driver itself (2026-09-18): a crowded floor invocation of a two-call prompt at `openai/gpt-oss-120b` @ `deepinfra/bf16` cost $0.002 and a fresh isolation one under $0.001, so a 40-invocation pass (20 prompts × floor + isolation) is about **$0.06 at that pin, $0.25 at the $0.15/M list tier, and a few dollars at the $1/M vendor tier**. The product-driver numbers are roughly ten times higher because the product CLI's system prompt rides on every request. The unbounded terms are reasoning tokens at higher effort and runaway tool loops, which the budget caps and the scaffold's step cap exist for. Set `budget_usd` per cell or pass `--budget-usd` for the run, and read the pre-run estimate the runner prints.

### A/B arms: comparing two configurations of your server

An **arm** is a cell. Two arms of one experiment are two cells identical in every field except the one under test — for a server-side comparison, one variable in the cell's `env` — and usually narrowed to the experiment's prompt with the cell's `prompts` list:

```json
"e17-control": {
  "driver": "loop", "endpoint": "openrouter", "scaffold": "loop-scaffold@1",
  "model": "openai/gpt-oss-120b", "provider": "deepinfra/bf16",
  "knobs": {"reasoning": {"effort": "low"}, "max_tokens": 8192},
  "role": "isolation", "context": "fresh", "tool_surface": ["get_thing"],
  "merge_gating": false, "groups": ["C"], "prompts": ["C1"],
  "env": {"MY_SERVER_FEATURE": "off"},
  "notes": "E17 control arm; preregistration: <path in your repo>"
},
"e17-feature-on": { "…": "identical, except", "env": {"MY_SERVER_FEATURE": "on"} }
```

- **Identity and the hash.** A cell's `env` is part of the cell's identity: two cells differing only in `env` are different cells and their rows never pool. Separately, the manifest hash is over the file's bytes, so *any* edit — an `env` value, a note — is a new manifest. Put every arm of an experiment in the manifest before the first run, so that all arms run under one hash. The row's `meta.json` shows it twice: `env` records what the cell declared (secret entries by key only), and the `cell_id` string carries an `env:` digest that differs between the arms while `knobs:`, `setup:` and `selection:` match.
- **Set the variable in every arm**, including the control, so that no arm depends on your server's default and the rows record what each arm ran under.
- **Naming.** The cell name is the row's directory name and the label in every report, so make it carry the experiment and the arm: `<experiment>-<arm>` (`e17-control`, `e17-feature-on`). It becomes a directory name, so keep to letters, digits, `-` and `_`. Name the arm by what it sets, never by what you hope it shows. Point `notes` at the preregistration.
- **Arms do not gate.** An experiment arm is `merge_gating: false`; a gate is a statement about your release, an arm is a question.
- **A check cannot be scoped to cells, by design.** Checks select records by tool and by predicates on the record, never by cell. If one arm's server violates a check on every record, that fail is a true observation of that arm, and hiding it would delete it. Pin the expected per-arm outcome in your preregistration ("arm X fails check Y on every record: the arm's signature, not a finding") — the uscode-mcp suite does exactly this. The checks report keeps the arms apart: `checks-report.json` → `checks[].cells.<cell>` gives each cell its own `outcome`, `matched` and `failures` beside the run-wide roll-up (worst over cells: error, then fail, then pass; vacuous only if every cell is), and each failure entry names its `cell`, `prompt_id`, `repetition`, and record `index`. So a by-design fail in one arm no longer hides a pass in the other — read the per-cell outcomes, not the roll-up.
- **Run the arms together.** One `--repeats` run over all arms of an experiment interleaves them in time, which a run per arm does not.

### What a loop row records, and where

A row is one (cell, prompt) invocation; its directory is `<run-dir>/<cell>/<group>/<prompt id>/` (with a further `rNN/` level in a `--repeats` run). Everything in "Runs" below applies to loop rows; these are the loop-specific readings.

| you want | file | field |
|---|---|---|
| why the answer ended (cap cut or the model's own stop) | `api-surface.jsonl`, one line per model request, read in `seq` order | `finish_reason` on each line (`length`, `stop`, `tool_calls`), null with `finish_reason_note` when unreadable. Also `meta.json` → `loop.finish_reason` (the final request) and `loop.finish_reasons` (counts) |
| which provider served each request | `api-surface.jsonl` | `provider`; rolled up in `meta.json` → `loop.served_providers`, with `loop.provider_mismatches` against the pin |
| tokens and cost | `api-surface.jsonl` | `usage_prompt_tokens`, `usage_completion_tokens`, `usage_reasoning_tokens`, `usage_cost`; the row total is `meta.json` → `spend_usd` |
| the answer's digest, for counting distinct answers | `meta.json` | `answer_sha256_16` |
| the distinct-answer count within one run | `run-manifest.json` | `loop_cells.<cell>.answers.<prompt id>` (`invocations`, `answered`, `distinct`, `digests`). A run without `--repeats` invokes each prompt once, so it reads 1 of 1; across run directories, aggregate `answer_sha256_16` yourself |
| why the model did what it did — asked afterwards, in the same context | `interview/NN/` under the row (from harness 7a26eb2): `questions.json`, `interview-session.json`, its own `api-surface.jsonl`, `trace.jsonl`, `meta.json` (per question: `asked`, `finish_reason`, `tool_calls`, `served_providers`, `spend_usd`, `answer`; `reasoning_replayed`; `tool_definitions_source`, which reads unverifiable on rows recorded before 7a26eb2 because their session files kept tool names only) | `mcp-e2e interview --row <row dir> --ask "…"` resumes the row's session under the same scaffold, pin, and knobs with the tools mounted. What comes back is the model's self-report, never an observation: it enters your spec as a claim to preregister a measurement against (S10, S21), and nothing in the run aggregates it |
| a turn that ended with no content at all | `meta.json` | `consumer_limit.cause: "null_final_content"` with `harness_failure: null`, an empty `answer.txt`, `answer_sha256_16: null`, and the row left out of `answered`. Seen when `openai/gpt-oss-120b` wrote its next tool call into its reasoning and stopped (7 of 26 rows in one run). Two marks say so without any text: `reasoning_tail_json` (the final reasoning ended in a JSON object) and `reasoning_tail_matches_tools` (offered tools whose parameter names cover its keys). Score it as a consumer that failed to answer. The harness does not retry such a turn, and `loop-scaffold@1` never will — a rescue would change what the floor measures, so it could only arrive as a new scaffold version. **Rows recorded before harness 36102ab** show this as the text `null` in `answer.txt` with `answer_chars: 4` and digest `74234e98afe7498f`, counted as an answer: read those rows the same way and exclude that digest from distinct-answer counts |
| a run that completed its invocations and then died in reporting, or was killed | the run directory | The rows are the measurement; the run-level files are derived. `mcp-e2e report --run-dir DIR` rebuilds `run-manifest.json` and `checks-report.json` from the rows without touching them, marks the manifest `rebuilt`, and records what only the runner knew (the pre-run estimate, budget events) as null with a note. A reporting-stage exception in a live run is itself recorded: the manifest names the stage under `reporting`, every check reads `error` with the exception, and the terminal prints `REPORTING ERROR`. Runs before harness b93c9b0 could die in the checks stage on a flagless run with a replaced pre-turn; rebuild those the same way |
| a row the endpoint refused (rate limit or outage) until the loop gave up | `meta.json` | A harness failure, never a consumer outcome: `harness_failure` set, `consumer_limit: null`, empty answer, the row counted under `failures` and not in `answered`. Through harness b93c9b0 the failure text begins `runner exited 2:` with the HTTP 429 inside a stderr tail and `loop.consumer_error`. A 429 the loop recovered from mid-turn shows only as `error_responses` entries and `(absent)` finish reasons on those wire lines, and the row is ordinary. From harness 01cdd33 the failure text leads with `upstream_unavailable` and the row carries `upstream_unavailable: {http_status, provider, attempts, retry_schedule_s, step, tool_calls_before}`; the slot is re-run as a whole fresh invocation under the same attempt counter as an unmet pre-turn (`--precondition-retries`, default 3), refused attempts kept under `attempt-NN/`, counted as `attempts_unavailable` apart from `attempts_unmet`. When you compute a rate over rows, the consumer denominator is `reached − attempts_unavailable`: `reached` counts every scored turn that was sent, refused ones included. Before 01cdd33 the slot was not re-run, so an arm could come up short — state the row count per arm when you compare |
| whether a crowded row's pre-turn reached the pinned mid-task state | `meta.json` | `crowding.state_check: {expected, observed, passed}` on every crowded row; a pre-turn whose state fails the predicate never receives the scored prompt — it is `precondition_unmet` with cause `state_mismatch` (or the consumer's own cause, with the state beside it), and the slot is re-run as a whole fresh invocation up to `--precondition-retries` times (default 3), each attempt kept under `attempt-NN/` |
| the consumer's full conversation, reasoning included | `loop-session-*.json` in the row (`loop-session-single.json` on fresh rows; from harness 36102ab, earlier only crowded rows had one) | `messages`, `offered_tools`, `scaffold`. The one artifact with message content; secret-scanned like the rest |
| pinned or not | `meta.json` | `reproducibility` (`pinned` or `unpinned`), with `provider_pin` and `quantization_asserted` |
| a consumer that ran itself out of context or steps | `meta.json` | `consumer_limit` (cause `context_length` or `step_cap`) with `harness_failure: null` — a consumer outcome you score as a failure to answer, never a broken instrument; counted in `run-manifest.json` → `consumer_limits` |
| a no-content turn that made no tool call at all | `meta.json` | `zero_trace_liveness` (from harness e2ae37b). **Read `harness_failure` first:** a broken row keeps its `consumer_limit.cause` for diagnosis, and only a row with `harness_failure: null` is a consumer outcome. A loop row with zero trace records counts as one only when the block reads `exempted: true` — the spawn check passed, every request carried the cell's exact tools (`wire_surface_exact`, out of `wire_requests`), and the final response is on the wire as a 2xx with a `finish_reason` (`final_response`). Anything less, and any product-driver row with an empty answer and zero trace records, is BROKEN: do not score it. Not yet seen in a live run |
| a crowded row whose pre-turn never reached the mid-task state | `meta.json`, `run-manifest.json` | from harness c289b74: `harness_failure` leads with the cause (`null_final_content: crowding precondition unmet: …; scored turn not sent`) and `precondition_unmet` carries `stage`, `cause`, `detail`, and the two reasoning marks; the scored prompt was never sent, so there is nothing to score and nothing to count against your server. The manifest lists these under `preconditions_unmet` (not `failures`), and `invocation_counts.<cell>.<prompt>` reads `asked`, `reached`, `attempts`. Such a slot is re-run as a whole fresh invocation up to three times by default (`--precondition-retries N`, 0 to disable; a run parameter, not part of your manifest hash) — every attempt is kept, replacements under `attempt-02/` … beneath the slot's path, and `slots.<slot>` names the attempt that reached the scored turn or reads `exhausted`. Only these are replaced: a scored turn that ends on null content, a step cap, or a context limit is your measurement and is never re-run. Leaving unmet rows out does not skew the rest — the pre-turn never sees your prompt. Before c289b74 the row read `harness_failure: crowding pre-turn exited 4` with the reason only in `loop-result-crowding.json`. **Check the state yourself until WO-10 lands:** a crowded row is mid-task only if its `distractor-state.json` lists exactly `n01`–`n04` filed; rows before harness 36102ab could reach the scored turn at fewer, and did (three in one uscode-mcp run) |
| a cell that never ran or is BROKEN, and why | `run-manifest.json` | `voided_cells.<cell>` (the reason in words) **and** `zero_trace_cell_failures` (cells with no trace records in any row — these are not listed under `voided_cells`); the cell directory holds a `CELL-VOID.json` either way. Budget stops under `budget.stops`. `failures` counts failure events, not rows |
| the calibration probe | `<run-dir>/<cell>/loop-probe/` | `probe.json` |
| row measurements such as `answer-coverage@1` | `meta.json` | `measurements.<name>`: one entry per matched trace record, with the record's `index` and `tool`, the method name and its hash |

The recorder writes no message content and no headers into `api-surface.jsonl` — names, sizes, and the scalars above only. The answer is `answer.txt`; tool traffic is `trace.jsonl`.

## Checks (Layer 1) — mechanical trace conformance

Declarative rules the harness evaluates over every trace record: this is where your server's *published tool contract* gets asserted (error envelopes, truncation markers, disambiguation totals, provenance fields — whatever your server promises). Selectors match records by tool name and pointer predicates; assertions are `present` / `absent` / `matches` / `not_matches` / `enum` / `forbid_pattern`, combined with `all_of` / `any_of` / `not`. Pointers are RFC 6901, with `~each` to quantify over arrays.

Each check reports one of **four** outcomes, and the distinctions are the point:

- `pass` — matched at least one record, assertion held on all.
- `fail` — assertion failed; offending record indices named.
- `vacuous` — the selector matched **zero** records. Never folded into pass: it means either a dead rule or a run that never exercised the surface, and both deserve eyes.
- `error` — the check itself could not run. Never conflated with fail or vacuous: a scan that errors must not look like one that found nothing.

## Row measurements — recorded numbers, never outcomes

Optional top-level `measurements` list. A row measurement is a mechanical value the harness computes across two artifacts of one row — a trace record and the row's answer — and records beside the row. It is never pass or fail; what the number means is your reading. One method exists, `answer-coverage@1`: how much of a reference string from a tool result the answer reproduces verbatim.

```json
"measurements": [
  {"name": "answer_coverage", "measure": "answer-coverage@1",
   "applies_to": {"tool": ["get_thing"]},
   "reference": "/response/structuredContent/text/content",
   "floor": 64}
]
```

`applies_to` is the checks selector (`tool`, optional `when`). `reference` is an RFC 6901 pointer, relative to the trace record, to the **string** to measure against — point at the payload proper and not at a banner or the whole record. `floor` is the minimum matched-span length in whitespace-normalized characters (default 64). Per matched record the row's `meta.json` gets, under `measurements.<name>`: `reference_chars_raw`, `reference_chars_normalized`, `answer_chars_raw`, `floor`, `spans`, `matched_chars`, `furthest_offset` (the raw reference index one past the furthest-reaching matched span), and `share`; a pointer that does not resolve to a string records a null with a note. The measure sees verbatim reproduction only — a summary answer scores near zero, correctly — so read it on prompts whose expected answer quotes, and read it beside `finish_reason`: a third of the window with `stop` is the model's cut, the whole window with `length` is your `max_tokens`.

## Scoring (Layer 2) and classifying failures

Layer 2 is yours: did the consumer, given honest tool responses, produce an honest answer — caveats propagated, absence reported as absence, no fabricated citations? Classify every failure **before** filing it:

- **Consumer-behavior finding** — the tool told the truth and the model dropped it. A response-shape/prominence question for your server's spec.
- **Tool defect** — the trace shows your server violating its own contract.
- **Instrument defect** — the harness, driver, or cell configuration could not have captured the signal. Fix the instrument first; **no finding of any other class is recorded from a defective instrument**. A zero-trace run is BROKEN, never an abstention.

Score from the artifacts, not from summaries — demand the trace, the before/after sets, the failing input. Agreement with a summary proves nothing; disagreement is the signal.

When the trace shows what the consumer did and you need to know why, interview the row (`mcp-e2e interview`, S21) rather than guessing — and file what it says as a claim, not a finding. A model's account of its own turn is the instrument reporting on itself (S10); its value is in naming the next measurement, and it is worth most on failed rows and on the more capable lineage, least on a floor model's account of how a tool felt.

## Runs

`mcp-e2e validate --manifest …` checks the manifest and runs nothing; `mcp-e2e run --manifest … [--run-dir DIR] [--cells a,b] [--groups A,B] [--prompts A1,B2] [--repeats N] [--dry-run] [--budget-usd N]` executes the selected (cell × prompt) grid, each pair once unless `--repeats` says otherwise. One trap: a prompt id given to `--prompts` runs in **every** selected cell, even a cell whose `groups` exclude it (the row is marked `outside_cell_groups`), so pair it with `--cells` when you mean one arm. At the run root: `run-manifest.json` (the selection, per-cell marks, voided cells, budget stops, the results list) and `checks-report.json`. Per row, in `<run-dir>/<cell>/<group>/<prompt id>/`, the run directory holds: `trace.jsonl` (every tool call, verbatim), `answer.txt`, `meta.json` (knobs, manifest hash, timing, tool-call list, attribution record, api-surface digest, crowding hash), `available-tools.json` (advertised vs exposed), `api-surface.jsonl` (one line per model request: the tool names actually sent, body size, timing — and for loop rows the response scalars listed under "What a loop row records"). `meta.json` also carries `answer_sha256_16`, `harness_failure`, and `consumer_limit`.

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
